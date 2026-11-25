"""打手侧**内部**应用管理端点（spec `docs/specs/2026-10-08-taker-open-api-design.md` §6）：
列表 / 创建 / PATCH / 轮换密钥 / 推送台账 / 手工重投；⛔ 没有 DELETE（只能停用，405 判据见 `TakerAppDetailView`）。
承重：全局会话认证（⛔ 不装对外 Bearer，spec §9-2 交叉判据）；`permission_classes` 必须写在类体——`apps/rbac/wiring.py` 的位点锚是 AST 解析（`tests/test_rbac_wiring.py`）；身份限权与 fail-closed 拒绑租户管理员见 `_resolve_taker`；留痕四动作码、`app_secret` 不进留痕。"""
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from apps.accounts.display import user_display_name
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.api.base import EnvelopePagination
from apps.core.envelope import api_response
from apps.core.permissions import is_tenant_admin
from apps.openapi.dispatch import DeliveryRetryRefused, prepare_retry
from apps.openapi.serializers import generate_app_key, generate_app_secret
from apps.rbac.permissions import has_node, has_node_now
from apps.takerapi.credentials import TAKER_SPEC
from apps.takerapi.models import AppState, CallbackState, TakerApiDelivery, TakerApp
from apps.takerapi.serializers import (
    APP_CREATE_WRITABLE_FIELDS,
    APP_UPDATE_WRITABLE_FIELDS,
    AppCreateSerializer,
    AppUpdateSerializer,
    DeliveryStatusFilterSerializer,
    app_row_payload,
    delivery_row_payload,
    reject_unwritable_keys,
)

User = get_user_model()

#: 门节点，任一即可（spec §6 的两个身份各持一个）。⛔ 必须保持「字面量元组」形状：
#: RBAC 接线位点只做一层模块常量解引用、只收字符串字面量（`tests/test_rbac_wiring.py` T2），拆常量再拼 ⇒ 位点判据假红。
#: `system.allow_taker_open_api` 今天还不在权限目录 ⇒ 除管理员外一律 403——fail-closed 是对的，⛔ 别为"让它能过"放宽。
APP_MANAGE_NODES = (
    "system.pinduoduo_settings",  # 系统管理员（既有口径）
    "system.allow_taker_open_api",  # 允许打手使用开发平台应用（节点落权限目录前非管理员一律 403）
)

#: 系统管理员那一个节点（`_resolve_taker` 用来分身份；**必须**是独立常量：
#: 它是"谁是管理员"的判据，与上面那份"谁能进门"的清单是**两件事**）。
SYSTEM_ADMIN_NODE = "system.pinduoduo_settings"

#: 取不到应用时的那句（走本仓口径：DRF `ValidationError` ⇒ HTTP 200 + `code 1000`）。
APP_NOT_FOUND_MSG = "应用不存在"

#: 应用名重名时的那句。⚠️ 与上家侧同款：**重名是业务失败（`1000`）而不是 500**
#: （先查一次唯一的名字，撞上就拒；不查的话 `INSERT` 会撞 `TakerApp.name` 的唯一索引
#: 抛 `IntegrityError` ⇒ HTML 500，而这是用户输入造成的**可预期**失败，`E-0010`）。
APP_NAME_TAKEN_MSG = "应用名已存在：{name}"

#: 管理员分支**缺** `taker` 时的那句（点名那个字段）。
TAKER_REQUIRED_MSG = "缺少参数：taker（创建打手侧应用必须指定绑定的打手账号）"

#: 管理员分支给了不存在的 `taker` 时的那句（点名字段与那个 id）。「别的租户的账号」与「不存在」是同一件事：
#: `auth_user` 每租户 schema 各一份、查询只在当前 schema。⛔ 别为区分两者加跨 schema 查找（租户边界）。
TAKER_NOT_FOUND_MSG = "绑定的打手账号不存在（taker={taker_id}）"

#: 被绑账号是租户管理员时的那句（fail-closed，两条分支共用）。文案点名「taker+系统管理员」是判据的一部分：
#: 调用方要一眼看出错在账号身份。⛔ 不许绑：管理员账号在范围判据与 `nodes_of` 上被短路 ⇒
#: 外部令牌看得到整个大厅、抢得到任何单（理由与残余风险见模块 docstring 与 `_resolve_taker`）。
TAKER_MUST_BE_PLAIN_MSG = "taker 必须是普通打手账号，不能绑定系统管理员"

#: 留痕里"空值"的写法（`from_value` / `to_value` 是 `CharField`，空串会渲染成
#: 「IP 白名单： → 1.2.3.4」，读起来像漏了一半）。与上家侧同一句。
EMPTY_VALUE_TEXT = "空"

#: 台账行不在本应用名下时的人话（⛔ 与 `APP_NOT_FOUND_MSG` 混成一句：「应用不存在」与「该应用名下无此记录」是两件事）。
#: 与上家侧那条逐字相同是巧合、不是共享常量——判据不抄第二份，文案是两族各自的数据。
DELIVERY_NOT_FOUND_MSG = "推送记录不存在"

# 重投的文案与闸门是共享件：文案=`TAKER_SPEC.delivery_retry_only_failed_msg`、
# 闸门=`apps/openapi/dispatch.py::prepare_retry`。⛔ 视图里不许再有那句字面量（两族各写一份必漂）。


class TakerAppAdminView(APIView):
    """打手侧应用管理端点的共同装配；推送台账 / 手工重投两个端点类继承本类、逐字复用这套装配（⛔ 不另立第二套认证/权限/取数，先例 `apps/openapi/api/app_views.py::ApiAppAdminView`）。
    不声明 `authentication_classes` ⇒ 全局默认会话认证；权限=节点门 `has_node` 任一命中，匿名 ⇒ 401、登录无节点 ⇒ 403（spec §9-2 交叉判据：合法打手 Bearer 打本组端点必须 401）。
    装配必须写在类体：`apps/rbac/wiring.py` 的 Site 以本类 `permission_classes` 为 RBAC 接线位点（AST 解析，`tests/test_rbac_wiring.py`）。"""

    permission_classes = [has_node(*APP_MANAGE_NODES)]

    def _visible_apps(self, user):
        """调用方可见的应用（spec §6：打手看不到别人的——行载荷有明文 `app_secret`（`D-0201`），看得见=拿得到凭据，是权限边界不是整洁）。
        管理员支看全部；打手支按**绑定账号** `filter(taker=user)` 收窄，⛔ 不是按 `created_by`（管理员替建的那套也是那个打手的凭据）。
        `select_related("taker")` 承重：行载荷要账号显示名（判据=列表查询数不随行数增长）。"""
        rows = TakerApp.objects.filter(integration_binding__isnull=True).select_related("taker").order_by("id")
        if self._is_system_admin(user):
            return rows
        return rows.filter(taker=user)

    def _get_app(self, pk, user):
        """取应用；取不到（**含"不是你的"**）⇒ `1000` 同形（⛔ 不 404；也⛔ 不 403——那会变成可探测信道，把 pk 存在性与主键枚举出来；同族先例：peer 握手口与 spec §5.3-2）。
        判据两条缺一不可：同形业务码 + 别人的那一行逐字段零变化（`tests/test_takerapi_app_api.py::TakerIdentityScopingTests`，各配正对照）。
        刻意无 pk 上界守卫：实测超 `int8` 的 pk 仍走「查不到」一支（psycopg 按 `numeric` 送）⇒ 防御性写法不写成承重逻辑（AGENTS §5-E），行为仍钉在用例里。"""
        app = self._visible_apps(user).filter(pk=pk).first()
        if app is None:
            raise ValidationError({"app": APP_NOT_FOUND_MSG})
        return app

    @staticmethod
    def _record_app(request, action, app, **columns):
        """应用域统一埋点（与上家侧同形同纪律）：`log_type` 一律查登记表 `ACTION_LOG_TYPES` ⛔ 不写死数字；操作人只取认证态 `request.user` ⛔ 不取请求体；名字进 `target_name`（R11）。
        ⛔ 调用方负责把它放进与写入同一个 `transaction.atomic()`，本助手不开事务。"""
        return record(
            log_type=ACTION_LOG_TYPES[action],
            action=action,
            actor=request.user.get_username(),
            actor_id=request.user.pk,
            target_kind="taker_app",
            target_id=app.pk,
            target_name=app.name,
            **columns,
        )

    # ── 身份限权（spec §6；**唯一**实现，创建端点调它）────────────────────
    @staticmethod
    def _is_system_admin(user) -> bool:
        """是否系统管理员：判据=节点（`has_node_now`），⛔ 不查 `groups`/职位名——门与身份必须同一谓词，否则「进得来但被当打手」分叉（`E-0021` 同族）。
        租户管理员（`nodes_of` 短路）在此恒真 ⇒ 恒走管理员分支（代价见模块 docstring）。"""
        return has_node_now(user, SYSTEM_ADMIN_NODE)

    def _resolve_taker(self, request, validated_data):
        """按身份定绑定（spec §6/§9-10 承重判据）：管理员分支必须选 `taker`（缺/非法/查不到 ⇒ `1000`+点名；判据 `is None`，`0` 是合法输入 ⛔ 不用真值）；打手分支**锁死 `request.user`**、完全不看载荷——按载荷查库会留下探测账号存在性的信道，报错文案也会反过来暗示该字段有用。
        两分支共同出口 fail-closed：`is_tenant_admin(taker)` ⇒ `1000`+点名、一行不落库；守卫刻意只写汇合后这一处（两份必漂，漂的方向=外部令牌看整个大厅），判据=`tests/test_takerapi_app_api.py::TakerIdentityScopingTests`（含桩住 `_is_system_admin` 的那条——守卫挪进管理员分支会红）。
        ⚠️ 守卫只挡建时：事后把绑定账号提成管理员 ⇒ 范围静默变宽（⛔ 不许写成"已彻底解决"）；兜底=不做这种提权、出事即停用应用（spec §10-5）。"""
        if self._is_system_admin(request.user):
            raw_taker_id = validated_data.get("taker")
            if raw_taker_id is None:
                raise ValidationError({"taker": TAKER_REQUIRED_MSG})
            taker = User.objects.filter(pk=raw_taker_id).first()
            if taker is None:
                raise ValidationError(
                    {"taker": TAKER_NOT_FOUND_MSG.format(taker_id=raw_taker_id)}
                )
        else:
            taker = request.user
        # ⭐ 两分支的唯一守卫（fail-closed）：不许把外部应用绑到租户管理员账号——该账号在
        # 范围判据与 `nodes_of` 上都被短路（理由见模块 docstring）。判据必须用 `is_tenant_admin`
        # （与 `scope_queryset`/`game_scope`/`nodes_of` 同一谓词，各写一遍必漂，`E-0021` 同族）。
        if is_tenant_admin(taker):
            raise ValidationError({"taker": TAKER_MUST_BE_PLAIN_MSG})
        return taker


class TakerAppListCreateView(TakerAppAdminView):
    """`GET`（列表）/ `POST`（创建）——同一个路径两种方法（spec §6）。"""

    def get(self, request):
        """列表：每行**含 `app_secret` 明文**（`D-0201` 同口径）；不带分页（照上家侧形状直接给数组）。
        可见范围唯一由 `_visible_apps` 决定（spec §6，理由见其 docstring）。
        `select_related("taker")` 承重：行载荷要账号显示名，不预取=每行一次额外查询（判据=查询数不随行数增长）。"""
        apps = self._visible_apps(request.user)
        return api_response([app_row_payload(app) for app in apps])

    def post(self, request):
        """创建：入参 `name`/打手账号/可选 `ip_allowlist`/回调两键，响应含明文 `app_secret`；重名先查再插（`1000`），并发 `IntegrityError` 属不可预期、让它响亮 500（`E-0010`/`E-0022`）。
        回调两键与 `ip_allowlist` 同档——请求里给了就**真落库**（⛔ 不许白名单收了却静默丢；判据打在库行，
        `tests/test_takerapi_app_api.py::TakerAppCreateTests`）；顺序=判据：白名单 fail-closed → 序列化器 → 写入。"""
        reject_unwritable_keys(request.data, APP_CREATE_WRITABLE_FIELDS)
        serializer = AppCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        name = serializer.validated_data["name"]
        if TakerApp.objects.filter(name=name).exists():
            raise ValidationError({"name": APP_NAME_TAKEN_MSG.format(name=name)})
        # ⭐ 身份限权：绑谁由**认证态**决定（spec §6），⛔ 不由载荷决定。
        taker = self._resolve_taker(request, serializer.validated_data)
        # ⭐ 回调两键真落库（第二期 spec §7）：形状照上家侧创建端点（`apps/openapi/api/app_views.py` 的 `post`）——
        # 只有请求里真给了才写，缺席落模型默认值；⛔ 别自创第二套（两族各写一种，改一处就漂）。
        # ⛔ 这里不做 URL/SSRF 判断：判据在 `AppCreateSerializer.callback_url` 校验器（与 PATCH 共用同一函数），再写一份=第二份判据（漂移侧=真 SSRF 通道）。
        create_fields = {
            key: serializer.validated_data[key]
            for key in ("callback_url", "callback_state")
            if key in serializer.validated_data
        }
        # ⚠️ 判据用 `"字段" in validated_data`（**不是**真值）：`ip_allowlist=""` 是**合法值**
        # （= 不限制）⇒ 拿真值判会把它当成"没传"而静默不生效（`E-0021` 同族）。
        if "ip_allowlist" in serializer.validated_data:
            create_fields["ip_allowlist"] = serializer.validated_data["ip_allowlist"]
        with transaction.atomic():
            app = TakerApp.objects.create(
                name=name,
                app_key=generate_app_key(),
                app_secret=generate_app_secret(),
                taker=taker,
                created_by=request.user,
                **create_fields,
            )
            # 留痕里给的是 `app_key`（**非机密**）；⛔ 密钥一个字都不进。
            # ⚠️ 绑定的是谁也要记（换人不走本表，所以"这条凭据是谁的"事后只能从这里读）。
            self._record_app(
                request,
                "system.taker_api_app_create",
                app,
                to_value=app.app_key,
                reason=user_display_name(taker),
            )
        return api_response(app_row_payload(app))


class TakerAppDetailView(TakerAppAdminView):
    """`PATCH /api/taker/apps/<pk>/`：改名/停用启用/改 IP 白名单/回调两键（spec §6）。只定义 `patch`：`PUT`/`DELETE`/`GET` 在本路径都是 **405**（「只能停用不能删」，用例钉住 ⛔ 不是"没测"）。
    405 读数的前提是认证+权限已过：DRF 的 405 判定排在 `initial()` 之后，匿名/无节点先吃 401/403（⛔ 别据此说"路径不存在"）。
    ⛔ `taker` 不可改：理由见 `AppUpdateSerializer` docstring（它是应用可见范围的唯一依据），本视图只按白名单 fail-closed 拒。"""

    def patch(self, request, pk):
        reject_unwritable_keys(request.data, APP_UPDATE_WRITABLE_FIELDS)
        app = self._get_app(pk, request.user)
        serializer = AppUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # ⚠️ 判据一律 `"字段" in data`（**不是**真值）：`ip_allowlist=""` 是**合法值**
        # （空 = 不限制）⇒ 用真值判断会把"清空白名单"当成"没传"而静默不生效。
        changes = []  # [(field_name, 变更前, 变更后)]，每个**真正变化**的字段一条
        if "name" in data and data["name"] != app.name:
            new_name = data["name"]
            if TakerApp.objects.filter(name=new_name).exclude(pk=app.pk).exists():
                raise ValidationError({"name": APP_NAME_TAKEN_MSG.format(name=new_name)})
            changes.append(("name", app.name, new_name))
        if "state" in data and data["state"] != app.state:
            changes.append(
                (
                    "state",
                    app.get_state_display(),
                    # 中文名取枚举自身（⛔ 不抄一份 `dict(AppState.choices)`）
                    AppState(data["state"]).label,
                )
            )
        if "ip_allowlist" in data and data["ip_allowlist"] != app.ip_allowlist:
            changes.append(
                (
                    "ip_allowlist",
                    app.ip_allowlist or EMPTY_VALUE_TEXT,
                    data["ip_allowlist"] or EMPTY_VALUE_TEXT,
                )
            )
        # ⭐ 回调两键（第二期 spec §7）：URL/SSRF 判据不在视图——在 `AppUpdateSerializer` 字段校验器
        # （与投递前共用 `apps/openapi/outbound.validate_callback_url`），再写一份=第二份判据（漂移侧=真 SSRF 通道）。
        # 空串=清回调、合法 ⇒ 判据用 `in data`+值比较 ⛔ 真值（`E-0021`）；比较对象是 `app.callback_url` 原始列值（序列化器已归一化首尾空白）。
        if "callback_url" in data and data["callback_url"] != app.callback_url:
            changes.append(
                (
                    "callback_url",
                    app.callback_url or EMPTY_VALUE_TEXT,
                    data["callback_url"] or EMPTY_VALUE_TEXT,
                )
            )
        if "callback_state" in data and data["callback_state"] != app.callback_state:
            changes.append(
                (
                    "callback_state",
                    # 中文名取**打手侧自己**那个枚举（⛔ 不抄一份 `dict(CallbackState.choices)`，
                    # ⛔ 也不用上家侧那个同名枚举：两族各一份，见 models.py 的 docstring）。
                    CallbackState(app.callback_state).label,
                    CallbackState(data["callback_state"]).label,
                )
            )

        if not changes:
            # 纯 no-op（`PATCH {}` 或重发当前值）：**不写库、不写留痕**（R26 同款口径）。
            return api_response(app_row_payload(app))

        with transaction.atomic():
            for field_name, _before, _after in changes:
                setattr(app, field_name, data[field_name])
            app.save(update_fields=[field for field, _b, _a in changes])
            for field_name, before, after in changes:
                self._record_app(
                    request,
                    "system.taker_api_app_update",
                    app,
                    field_name=field_name,
                    from_value=before,
                    to_value=after,
                )
        return api_response(app_row_payload(app))


class TakerAppRotateSecretView(TakerAppAdminView):
    """`POST /api/taker/apps/<pk>/rotate-secret/`：轮换密钥。单密钥**立即失效**（无宽限期，spec §6/§10-5）⇒ 外部工作室瞬时断线；验签读的就是这一列，"立即失效"是同一事实的两面、无需额外机制。
    本端点不读请求体（无入参）：多余的 body 既不 500、也不被误判成 `1000`。"""

    def post(self, request, pk):
        app = self._get_app(pk, request.user)
        with transaction.atomic():
            app.app_secret = generate_app_secret()
            app.secret_rotated_at = timezone.now()
            app.save(update_fields=["app_secret", "secret_rotated_at"])
            # ⛔ 新旧密钥都不进留痕：只记"轮换了"这件事本身（凭据纪律）。
            self._record_app(request, "system.taker_api_app_rotate_secret", app)
        return api_response(app_row_payload(app))


# ═════════════════════════════════════════════════════════════════════════════
# 推送台账的两个**内部**端点（第二期 spec §7）
# ═════════════════════════════════════════════════════════════════════════════


class TakerAppDeliveriesView(TakerAppAdminView):
    """`GET /api/taker/apps/<pk>/deliveries/`：推送台账（最近在前、分页）——spec §7「查看推送台账」的读面（第二期交付 outbox 后原本没有读接口）。
    装配继承 `TakerAppAdminView`：会话认证+节点门+`_get_app`（身份限权同次带过来，别人的 pk ⇒ `1000` 同形，⛔ 不另立第二套）。
    分页/键集/排序/筛选全走既有件：`EnvelopePagination`（内部页面端形状 `items/total/page/page_size`，⛔ 不是对外 v1 的 `count/results`，上限取既有值 ⛔ 不自己写算术）；键集唯一出自 `delivery_row_payload` ⛔ 本类不拼载荷；排序显式 `-created_at,-id`（本端点契约，与 `Meta.ordering` 各自可调）；`status` 非法 ⇒ `1000` ⛔ 不静默忽略；`?page` 畸形走 DRF 既有行为（信封翻 `1002`）⛔ 不特判——同一输入在不同列表上码必须一致。"""

    def get(self, request, pk):
        app = self._get_app(pk, request.user)
        # ⚠️ 筛选值先过序列化器（AGENTS §5-E）再进 `filter`；判据 `is not None` ⛔ 真值（`E-0021` 同族）。
        # ⚠️ 实测 `?status=`（空串）=没传 ⇒ 不筛——DRF 框架事实（`Field.get_value`），⛔ 别在本类特判（见序列化器 docstring）。
        params = DeliveryStatusFilterSerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        status = params.validated_data.get("status")

        # ⚠️ 取数**同时按 `app` 收窄**（这是身份限权那条判据在**数据面**上的落点：
        # `_get_app` 已经把"不是你的"挡在门外，这里再收一次是"子资源必须属于父资源"）。
        rows = TakerApiDelivery.objects.filter(app=app)
        if status is not None:
            rows = rows.filter(status=status)
        rows = rows.order_by("-created_at", "-id")

        paginator = EnvelopePagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        return paginator.get_paginated_response(
            [delivery_row_payload(row) for row in page]
        )


class TakerAppDeliveryRetryView(TakerAppAdminView):
    """`POST …/deliveries/<delivery_pk>/retry/`：手工重投一条 `failed` 行（重排 `pending`/`attempts=0`/`next_attempt_at=None` ⇒ 常驻进程 10 秒内再投；`reason`/`response_status`/`processed_at` 保留不清——页面要同时看到「刚重投」与「上次为何失败」）。
    ⛔ 只允许 `failed`：`done`=重复推送（对方收两次，这道闸去掉后没有任何机制拦得住）、`pending`=已在队列（重排只是清退避表）；两态都回 `1000`+`TAKER_SPEC` 那一句。闸门与字段重置唯一实现=`apps/openapi/dispatch.py::prepare_retry`，⛔ 视图不再有字面量（跨族对拍 `tests/test_takerapi_callback_api.py::RetryGateParityTests`）。
    响应=重排后的行（同 `delivery_row_payload`，页面就地刷新）；留痕 `system.taker_api_delivery_retry` 与写入同一个 `atomic()`；⛔ `reason` 不进留痕（可能装对方响应体片段）。"""

    @staticmethod
    def _get_delivery(app, delivery_pk):
        """取该应用名下的台账行；取不到 ⇒ `1000`（⛔ 不 404，与 `_get_app` 同口径）。查询必须同时按 `app` 收窄：
        `deliveries/<delivery_pk>/` 是 `apps/<pk>/` 的子资源，拿 A 应用的 pk 重投 B 应用的行必须失败（同租户内越权，否则父 pk 只是装饰）。
        真正的身份限权在 `_get_app` 层：别人的应用 pk 走「应用不存在」出口（判据见 `tests/test_takerapi_callback_api.py` 逐列快照反例）。"""
        delivery = TakerApiDelivery.objects.filter(pk=delivery_pk, app=app).first()
        if delivery is None:
            raise ValidationError({"delivery": DELIVERY_NOT_FOUND_MSG})
        return delivery

    def post(self, request, pk, delivery_pk):
        # ⭐ 顺序是判据的一部分：**先取父资源**（含身份限权）⇒ 再取子资源。
        # 反过来会让"别人的应用 + 一个不存在的 delivery pk"与"别人的应用 + 一条存在的行"
        # 给出**不同的**文案 ⇒ 那条差别本身就是一条可探测的信道（同族理由见 `_get_app`）。
        app = self._get_app(pk, request.user)
        delivery = self._get_delivery(app, delivery_pk)
        # ⭐ 闸门与逐字段重置唯一实现=`apps/openapi/dispatch.py::prepare_retry`，本族口径（台账表/状态值/文案）从
        # `TAKER_SPEC` 现取——⛔ 视图里不再写字面量或状态判据（两族各写一份，谁被单独放松都没有症状）。
        # ⚠️ `prepare_retry` 不开事务：它与留痕必须同一个 `atomic()`；被拒抛 `DeliveryRetryRefused` ⇒ 在此翻成本族 `1000`。
        try:
            with transaction.atomic():
                prepare_retry(delivery, spec=TAKER_SPEC)
                self._record_app(
                    request,
                    "system.taker_api_delivery_retry",
                    app,
                    to_value=delivery.get_event_display(),
                    order_no=delivery.order_no,
                )
        except DeliveryRetryRefused as exc:
            raise ValidationError({"delivery": str(exc)}) from exc
        return api_response(delivery_row_payload(delivery))

