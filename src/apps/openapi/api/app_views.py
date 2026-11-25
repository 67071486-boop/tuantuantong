"""应用管理端点（**内部**接口，spec §7.3）：列表/创建/PATCH/轮换密钥/推送台账/手工重投；⛔ 没有 DELETE（只能停用，405 判据=`tests/test_openapi_app_api.py::test_no_delete_route_returns_405`，读数前提=认证+权限已过）。
承重：会话认证（⛔ 不装对外认证类）；节点门复用现成 `system.pinduoduo_settings` ⛔ 不新增 RBAC 节点（`catalog.NODES` 是冻结清单，`tests/test_rbac_wiring` T1）；本模块只管 `kind=general` 行（锁定应用归 `api/wecom_qr_app_views.py` 单例端点，D8）；`app_secret` 明文随时可取但 ⛔ 绝不进留痕/日志/错误消息（判据=`AppSecretNeverLoggedTests`，唯一载荷出口=`app_row_payload`）。
留痕=`system.open_api_app_create/_update/_rotate_secret/_delivery_retry`，no-op 不写留痕不写库（R26），留痕与写入同一个 `atomic()`。"""
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.api.base import EnvelopePagination
from apps.core.envelope import api_response
from apps.openapi.credentials import OPENAPI_SPEC
from apps.openapi.dispatch import DeliveryRetryRefused, prepare_retry
from apps.openapi.models import (
    ApiApp,
    ApiAppKind,
    ApiAppState,
    CallbackState,
    OpenApiDelivery,
)
from apps.openapi.serializers import (
    AppCreateSerializer,
    AppUpdateSerializer,
    DeliveryStatusFilterSerializer,
    app_row_payload,
    delivery_row_payload,
    generate_app_key,
    generate_app_secret,
)
from apps.rbac.permissions import has_node

#: 应用管理的节点门：**复用现成的 `system.pinduoduo_settings`**（D9，⛔ 不新增节点）。
#: 代价（如实登记）：不能单独授权「谁能管开放平台应用」——与「其它平台对接设置」共用一点，
#: 那一点今天已经是**三个页面**的可见性承载者（登记在 `apps/rbac/wiring.py` 的残差里）。
APP_MANAGE_NODES = ("system.pinduoduo_settings",)

#: 取不到应用时的那句（走本仓口径：DRF `ValidationError` ⇒ HTTP 200 + `code 1000`）。
APP_NOT_FOUND_MSG = "应用不存在"

#: 应用名重名时的那句。⚠️ 与账号线/角色线同族：**重名是业务失败（`1000`）而不是 500**
#: （先查一次唯一的名字，撞上就拒；不查的话 `INSERT` 会撞 `ApiApp.name` 的唯一索引
#: 抛 `IntegrityError` ⇒ HTML 500，而这是用户输入造成的**可预期**失败，E-0010）。
APP_NAME_TAKEN_MSG = "应用名已存在：{name}"

#: 留痕里"空值"的写法（`from_value` / `to_value` 是 `CharField`，空串会渲染成
#: 「IP 白名单： → 1.2.3.4」，读起来像漏了一半）。
EMPTY_VALUE_TEXT = "空"

#: 重投端点：那条台账行不在**这个应用**名下时的人话（⛔ 与 `APP_NOT_FOUND_MSG` 分开：
#: 「应用不存在」与「这个应用名下没有这条推送记录」是两件事，混成一句会让排障的人去查错对象）。
DELIVERY_NOT_FOUND_MSG = "推送记录不存在"

# 重投的文案与闸门是共享件：文案=`OPENAPI_SPEC.delivery_retry_only_failed_msg`、
# 闸门=`apps/openapi/dispatch.py::prepare_retry`（与打手侧两族共用）。⛔ 视图里不许再有那句字面量。


class ApiAppAdminView(APIView):
    """六条应用管理端点的共同装配（推送台账/手工重投两类继承本类、逐字复用这套装配 ⛔ 不另立第二套）。
    不声明 `authentication_classes` ⇒ 全局会话认证（⛔ 装签名认证会让页面调不通）；权限=节点门 `has_node`：匿名 ⇒ 401、登录无节点 ⇒ 403；与 `RoleAuthorityViewSet` 装配同形。"""

    permission_classes = [has_node(*APP_MANAGE_NODES)]

    @staticmethod
    def _get_app(pk):
        """取应用；取不到 ⇒ `1000`（⛔ 不回 404：业务结果不用 HTTP 状态码表达）。查询按 `kind=general` 收窄（D8）：本模块四条端点只管通用应用，锁定应用归 `api/wecom_qr_app_views.py` 单例端点 ⇒ 拿锁定应用的 pk 打这里 ⇒ `1000`。
        对既有数据零行为变化（`kind` 列默认 `general`、库里没有其它值——加的是此前恒真的过滤）；代价如实（spec §9-6）：应用管理页从此看不到锁定应用。注意 `kind=general` 是取数口径，「每租户一把」是 `ApiApp.Meta.constraints` 的条件唯一约束，两条不要混。
        刻意无 pk 上界守卫：实测超 `int8` 的 pk 仍走「查不到」一支（psycopg 按 `numeric` 送）⇒ 防御性写法不写成承重逻辑（AGENTS §5-E）；行为仍被 `test_huge_pk_is_a_business_error_not_a_500` 钉住。"""
        app = ApiApp.objects.filter(pk=pk, kind=ApiAppKind.GENERAL).first()
        if app is None:
            raise ValidationError({"app": APP_NOT_FOUND_MSG})
        return app

    @staticmethod
    def _record_app(request, action, app, **columns):
        """应用域统一埋点：`log_type` 查 `ACTION_LOG_TYPES` ⛔ 不写死数字；操作人只取认证态；名字进 `target_name`（R11）。
        ⛔ 调用方负责放进与写入同一个 `transaction.atomic()`，本助手不开事务。"""
        return record(
            log_type=ACTION_LOG_TYPES[action],
            action=action,
            actor=request.user.get_username(),
            actor_id=request.user.pk,
            target_kind="api_app",
            target_id=app.pk,
            target_name=app.name,
            **columns,
        )


class ApiAppListCreateView(ApiAppAdminView):
    """`GET`（列表）/ `POST`（创建）——同一个路径两种方法（§7.3 的前两行）。"""

    def get(self, request):
        """列表：每行含 `app_secret` 明文（业务方改口径，spec §34）。
        ⭐ D8：列表按 `kind=general` 收窄——锁定应用不出现在这里（归 `api/wecom_qr_app_views.py` 单例端点管）；对既有数据零行为变化（见 `_get_app` docstring）；⛔ 别据此说"本端点只读通用的那些列"——行载荷仍是 `app_row_payload`。
        不需要预取任何关联：行载荷只回 `ApiApp` 自己那几列（判据=`app_row_payload` 函数体）。"""
        apps = ApiApp.objects.filter(kind=ApiAppKind.GENERAL).order_by("id")
        return api_response([app_row_payload(app) for app in apps])

    def post(self, request):
        """创建：入参只要 `name`；响应含明文 `app_secret`（改口径见 spec §34）。
        重名先查再插（可预期 ⇒ `1000`）；并发下仍可能撞 `IntegrityError`——不可预期的一侧让它响亮 500（E-0010/E-0022，⛔ 不用 `except` 伪装成业务结果）；与角色线 `create_role` 同形。"""
        serializer = AppCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        name = serializer.validated_data["name"]
        if ApiApp.objects.filter(name=name).exists():
            raise ValidationError({"name": APP_NAME_TAKEN_MSG.format(name=name)})
        # ⭐（spec §7.1）：回调配置可建应用时一次填完（两字段可选）。判据用 `"字段" in validated_data`
        # ⛔ 不是真值（`callback_url=""`=没配回调，是合法值，E-0021 同族）。⛔ 这里不做 URL/SSRF 判断：
        # 判据在 `AppCreateSerializer` 字段校验器（与 PATCH 共用 `outbound.validate_callback_url`，两处各写一份必漂）。
        create_fields = {
            key: serializer.validated_data[key]
            for key in ("callback_url", "callback_state")
            if key in serializer.validated_data
        }
        with transaction.atomic():
            app = ApiApp.objects.create(
                name=name,
                app_key=generate_app_key(),
                app_secret=generate_app_secret(),
                created_by=request.user,
                **create_fields,
            )
            # 留痕里给的是 `app_key`（**非机密**，§5.3 允许进日志）；⛔ 密钥一个字都不进。
            self._record_app(request, "system.open_api_app_create", app, to_value=app.app_key)
        return api_response(app_row_payload(app))


class ApiAppDetailView(ApiAppAdminView):
    """`PATCH /api/open/apps/<pk>/`：改名/停用启用/改 IP 白名单/回调配置（§7.3/§7.1）。只定义 `patch`：`PUT`/`DELETE`/`GET` 都是 **405**（「只能停用不能删」，用例钉住）；405 读数前提=认证+权限已过（DRF 405 判定在 `initial()` 之后，匿名/无节点先吃 401/403）。
    回调两字段是 §7.1「保存时校验」那一半：仅 https+SSRF 判据由序列化器字段校验器承担（与投递前同一个 `outbound.validate_callback_url`），⛔ 本视图不复述。"""

    def patch(self, request, pk):
        app = self._get_app(pk)
        serializer = AppUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # ⚠️ 判据一律 `"字段" in data`（**不是**真值）：`ip_allowlist=""` 是**合法值**
        # （空 = 不限制）⇒ 用真值判断会把"清空白名单"当成"没传"而静默不生效。
        changes = []  # [(field_name, 变更前, 变更后)]，每个**真正变化**的字段一条
        if "name" in data and data["name"] != app.name:
            new_name = data["name"]
            if ApiApp.objects.filter(name=new_name).exclude(pk=app.pk).exists():
                raise ValidationError({"name": APP_NAME_TAKEN_MSG.format(name=new_name)})
            changes.append(("name", app.name, new_name))
        if "state" in data and data["state"] != app.state:
            changes.append(
                (
                    "state",
                    app.get_state_display(),
                    # 中文名取枚举自身（⛔ 不抄一份 `dict(ApiAppState.choices)`）
                    ApiAppState(data["state"]).label,
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
        # ⭐（spec §7.1/§8）：回调两键。URL/SSRF 判据不在视图——在 `AppUpdateSerializer` 字段校验器
        # （与投递前同一个 `outbound.validate_callback_url`），⛔ 本视图只负责"真正变了才写、才留痕"；
        # 空串=清回调、合法 ⇒ 判据用 `in data`+值比较 ⛔ 真值（E-0021）。
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
                    # 中文名取枚举自身（⛔ 不抄一份 `dict(CallbackState.choices)`）
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
                    "system.open_api_app_update",
                    app,
                    field_name=field_name,
                    from_value=before,
                    to_value=after,
                )
        return api_response(app_row_payload(app))


class ApiAppRotateSecretView(ApiAppAdminView):
    """`POST /api/open/apps/<pk>/rotate-secret/`：轮换密钥（§7.3/§5.3）。单密钥**立即失效**（无宽限期）⇒ 第三方瞬时断线（代价 §9-12）；验签读的就是这一列，"立即失效"是同一事实的两面、无需额外机制。"""

    def post(self, request, pk):
        app = self._get_app(pk)
        with transaction.atomic():
            app.app_secret = generate_app_secret()
            app.secret_rotated_at = timezone.now()
            app.save(update_fields=["app_secret", "secret_rotated_at"])
            # ⛔ 新旧密钥都不进留痕：只记"轮换了"这件事本身（凭据纪律，§5.3）。
            self._record_app(request, "system.open_api_app_rotate_secret", app)
        return api_response(app_row_payload(app))


# ═════════════════════════════════════════════════════════════════════════════
# 推送台账的两个**内部管理**端点（设计文档 §7.4 的「页面可见、可手工重投」那一半）
# ═════════════════════════════════════════════════════════════════════════════

class ApiAppDeliveriesView(ApiAppAdminView):
    """`GET /api/open/apps/<pk>/deliveries/`：推送台账（最近在前、分页）——outbox 交付后为 §8「最近推送记录」补的读面。装配继承本模块其余端点（会话认证+节点门+`_get_app`，⛔ 不另立第二套）。
    分页=`EnvelopePagination`（内部页面端形状 `items/total/page/page_size`，⛔ 不是对外 `count/results` 契约；默认/上限取既有值）；键集唯一出自 `delivery_row_payload` ⛔ 本类不拼载荷；排序显式 `-created_at,-id`（本端点契约，与 `Meta.ordering` 各自可调）。
    `status` 非法 ⇒ `1000` 带字段名（`DeliveryStatusFilterSerializer`，⛔ 不静默忽略）；`?page` 畸形走 DRF 既有行为（信封翻 `1002`）⛔ 不特判——同输入不同列表必须同码；判据=`tests/test_openapi_delivery_admin.py`。"""

    def get(self, request, pk):
        app = self._get_app(pk)
        # ⚠️ 筛选值先过序列化器（AGENTS §5-E）；判据 `is not None` ⛔ 真值（`E-0021` 同族）。
        # ⚠️ 实测 `?status=`（空串）=没传 ⇒ 不筛——DRF 框架事实（`Field.get_value`），⛔ 别在本类特判（见序列化器 docstring）。
        params = DeliveryStatusFilterSerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        status = params.validated_data.get("status")

        rows = OpenApiDelivery.objects.filter(app=app)
        if status is not None:
            rows = rows.filter(status=status)
        rows = rows.order_by("-created_at", "-id")

        paginator = EnvelopePagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        return paginator.get_paginated_response(
            [delivery_row_payload(row) for row in page]
        )


class ApiAppDeliveryRetryView(ApiAppAdminView):
    """`POST …/deliveries/<delivery_pk>/retry/`：手工重投一条 `failed` 行（重排 `pending`/`attempts=0`/`next_attempt_at=None` ⇒ 常驻进程 10 秒内再投；`reason`/`response_status`/`processed_at` 保留不清——页面要同时看到「刚重投」与「上次为何失败」）。
    ⛔ 只允许 `failed`：`done`=重复推送（对方收两次，闸去掉后没有任何机制拦得住）、`pending`=已在队列（重排只是清退避表）；两态都回 `1000`+`spec.delivery_retry_only_failed_msg` 一句。闸门与字段重置唯一实现=`apps/openapi/dispatch.py::prepare_retry`（与打手侧两族共用，⛔ 视图不再有字面量）。
    响应=重排后的行（同 `delivery_row_payload`，页面就地刷新）；留痕 `system.open_api_delivery_retry` 与写入同一个 `atomic()`；⛔ `reason` 不进留痕（可能装对方响应体片段）。"""

    @staticmethod
    def _get_delivery(app, delivery_pk):
        """取该应用名下的台账行；取不到 ⇒ `1000`（⛔ 不 404，与 `_get_app` 同口径）。查询同时按 `app` 收窄：
        `deliveries/<delivery_pk>/` 是子资源，拿 A 应用的 pk 重投 B 应用的行必须失败（否则父 pk 只是装饰、同租户内越权）。"""
        delivery = OpenApiDelivery.objects.filter(pk=delivery_pk, app=app).first()
        if delivery is None:
            raise ValidationError({"delivery": DELIVERY_NOT_FOUND_MSG})
        return delivery

    def post(self, request, pk, delivery_pk):
        app = self._get_app(pk)
        delivery = self._get_delivery(app, delivery_pk)
        # ⭐ 闸门与逐字段重置唯一实现=`apps/openapi/dispatch.py::prepare_retry`，本族口径（台账表/状态值/文案）从
        # `OPENAPI_SPEC` 现取——⛔ 视图里不再写字面量或状态判据（两族各写一份，谁被单独放松都没有症状）。
        # ⚠️ `prepare_retry` 不开事务：它与留痕必须同一个 `atomic()`；被拒抛 `DeliveryRetryRefused` ⇒ 在此翻成本族 `1000`。
        try:
            with transaction.atomic():
                prepare_retry(delivery, spec=OPENAPI_SPEC)
                self._record_app(
                    request,
                    "system.open_api_delivery_retry",
                    app,
                    to_value=delivery.get_event_display(),
                    order_no=delivery.order_no,
                )
        except DeliveryRetryRefused as exc:
            # ⛔ `reason` 不进留痕；这里也⛔ 不回 404 —— 业务结果不用 HTTP 状态码表达。
            raise ValidationError({"delivery": str(exc)}) from exc
        return api_response(delivery_row_payload(delivery))
