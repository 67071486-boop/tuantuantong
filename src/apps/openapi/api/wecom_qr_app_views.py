"""企微群码**锁定应用**的单例管理端点（**内部**接口；spec `docs/specs/2026-10-09-wecom-group-qr-auto-upload-design.md` §7.3，决策 D7 单例形状 / D9 复用现成节点）。
六条路由（GET 不存在 ⇒ `data=null` ⛔ 不是 404/`1002`；并发创建撞条件唯一约束 ⇒ `1000`+人话 ⛔ 不是 500，判据=回查实际冲突面，见 `_translate_create_conflict`）；⛔ 没有 DELETE（沿用「只能停用不能删」）。
承重：⛔ 本模块**不重造**任何既有机制——装配继承 `app_views.ApiAppAdminView`、载荷/分页/入参校验/留痕动作码全部复用（⛔ 不新增动作码）；⛔ 不改 `app_views.py` 既有五条端点；`name` 创建时可给可默认但 ⛔ 不在 PATCH 可改字段里（§7.3 定值）。"""
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.business.api.base import EnvelopePagination
from apps.core.envelope import api_response
from apps.openapi.api import app_views as app_views_module
from apps.openapi.api.app_views import (
    APP_NAME_TAKEN_MSG,
    APP_NOT_FOUND_MSG,
    EMPTY_VALUE_TEXT,
    ApiAppAdminView,
)
from apps.openapi.models import (
    ApiApp,
    ApiAppKind,
    ApiAppState,
    CallbackState,
    OpenApiDelivery,
)
from apps.openapi.serializers import (
    APP_NAME_MAX_LENGTH,
    AppCreateSerializer,
    AppUpdateSerializer,
    DeliveryStatusFilterSerializer,
    app_row_payload,
    delivery_row_payload,
    generate_app_key,
    generate_app_secret,
)
from rest_framework import serializers

#: 创建时 `name` 缺席的默认应用名（§7.3）。它**不是** `ApiApp.name` 的模型默认值（那一列必填无默认）⇒ 只影响本端点；
#: 它同时是**重名**来源：同租户已有同名通用应用时会先撞 `name` 唯一索引 ⇒ 那是"名字被占了"不是"已有一把"
#: （模型级撞库由 `_translate_create_conflict` 归 500 侧，⛔ 不冒充本端点业务结果）。
DEFAULT_APP_NAME = "自动上传企微群码"

#: 已有一把时的那句人话（§7.3 逐字；**唯一**出处，⛔ 不开第二句）。
WECOM_APP_EXISTS_MSG = "本租户已创建过该应用，不能重复创建"

class WecomQrAppCreateSerializer(AppCreateSerializer):
    """创建入参：与 `AppCreateSerializer` 逐字相同，只把 `name` 改成可选。⛔ 必须重声明这一个字段（不是重复校验规则）：
    既有那个 `name` 是 `required=True`，不重声明则 `POST {}` 当场回「缺少参数：name」、默认值分支永远走不到（实测：并发用例两条线程都回缺少参数）。
    其余一切继承（`max_length`/两条文案/https+SSRF 校验器），⛔ 这里一个都不复述（同形两份校验必然漂，E-0021）。"""

    name = serializers.CharField(
        required=False,
        allow_blank=False,
        max_length=APP_NAME_MAX_LENGTH,
        error_messages={"blank": "应用名不能为空"},
    )


#: 本模块两条路径（与 `apps/openapi/urls.py` 逐字一致；⚠️ 都带尾斜杠）。
#: ⚠️ 它们**不带路径转换器** ⇒ 会落进 `tests/test_no_route_500.py` 的显式路由腿
#: （那条腿要求"畸形入参也不许 500、且仍是标准信封"）——本模块三条端点都满足。
WECOM_QR_APP_PATH = "/api/open/wecom-qr-app/"
WECOM_QR_APP_ROTATE_PATH = "/api/open/wecom-qr-app/rotate-secret/"
WECOM_QR_APP_DELIVERIES_PATH = "/api/open/wecom-qr-app/deliveries/"


def _fetch_wecom_app():
    """本租户那把锁定应用；没有 ⇒ `None`。判据 `first()`+`is None`（⛔ 不用 `get()`：一行都没有抛 `DoesNotExist`，会让"正常还没创建"走异常——而它是 `GET` 的正常答复 `data=null`）。
    不需要 `order_by`：条件唯一约束（`uniq_apiapp_wecom_qr`，D2）保证至多一行；调用方不假设一定有一行。"""
    return ApiApp.objects.filter(kind=ApiAppKind.WECOM_GROUP_QR).first()


def _translate_create_conflict(name):
    """`IntegrityError` 翻译口：**按回查到的实际冲突面**分档，都不是 ⇒ 原样抛（未预期照样 500，E-0010；⛔ 不是 `except IntegrityError: 1000`——那会把真正该 500 的错一起藏掉）。
    分辨顺序：① `kind=wecom_group_qr` 档**有**一行 ⇒ 条件唯一约束（`WECOM_APP_EXISTS_MSG`）——必须排在名字检查之前（撞 `name` 的失败也可能因为库里已有一把默认名的锁定应用，那时正确的话是"已有一把"）；② 无 wecom 行而 `name` 被占 ⇒ 名字冲突（`APP_NAME_TAKEN_MSG`，并发都用默认名时先撞这条）。
    ⛔ 调用方必须把它放在**保存点之外**：失败 INSERT 会把事务标脏（`current transaction is aborted`），内层 `atomic()`（保存点）只包那一笔 `create()`、事务本身仍可用，两条回查才跑得通（E-0022 同族：补偿路径事务边界写错=「一半成功」或「响亮但归因错误」）。"""
    if _fetch_wecom_app() is not None:
        raise ValidationError({"kind": WECOM_APP_EXISTS_MSG})
    if ApiApp.objects.filter(name=name).exists():
        raise ValidationError({"name": APP_NAME_TAKEN_MSG.format(name=name)})
    raise


class WecomQrAppAdminView(ApiAppAdminView):
    """本模块四条端点的共同装配：**逐字继承** `app_views.ApiAppAdminView`（认证/权限/取数全部来自父类，⛔ 不声明第二套；类体只有 docstring 是有意的）。⛔ 不要在这里重写 `_get_app`：本模块取数走 `_fetch_wecom_app`（"按 kind 取、取不到 ⇒ 正常答复"），与父类"按 pk 取、取不到 ⇒ `1000`"是两件事。
    ⛔ 留痕为什么有一个**本模块的模块级** `_record_app` 直通函数（实测逼出来的，别删）：覆盖网 `test_operation_log_coverage._certified_as_instrumented` 解析助手用**裸名+本模块**、不跟 `self.` 接收者跳出——`self._record_app` / 本类直通方法 / 未登记的转发函数三种写法**实测全红**；唯一活法=模块级转发+在覆盖网 `_CROSS_MODULE_FORWARDERS` 登记 `_record_app → apps.openapi.api.app_views`。
    ⛔ 这不是放宽：解析到父类后仍走同一套 `_node_writes_log` 判据（父类 `record(...)` 被删网照样红）；"真的在写日志"另由 `tests/test_openapi_wecom_qr_app.py` 三条留痕断言（行为判据）承担。"""


def _record_app(request, action, app, **columns):
    """本模块的**留痕直通口**（模块级函数，存在的唯一理由=覆盖网按「本模块裸名+具名接收者」解析助手链，见类 docstring）。
    ⛔ 这里不写任何业务逻辑：形状（`log_type` 查表/操作人取认证态/`target_*`）只有 `app_views.ApiAppAdminView._record_app` 一份出处。"""
    return app_views_module.ApiAppAdminView._record_app(request, action, app, **columns)


class WecomQrAppSingletonView(WecomQrAppAdminView):
    """`wecom-qr-app/` 路径上的三个方法同住一个视图类（§7.3 前三行是同一条路径）：Django 路由先匹配先赢 ⇒ 为同 pattern 再注册一条 `path()` 永远走不到（先例：`order_query_views.OrderDetailView` 的 GET/PATCH 同住一个类）。"""

    def get(self, request):
        """读本租户那把锁定应用；**没有 ⇒ `data=None`**（⛔ 不是 404、⛔ 不是 `1002`）。
        空答复必须是 `null` 而不是空对象/空列表：页面据此区分「还没创建」（显示创建按钮）与「已创建」，拿空对象兜会把"没有"渲染成"有一把各项都空的钥匙"。
        404/`1002` 那条口径属于"按 pk 取一条"的端点（`app_views._get_app`）；本端点没有"你给的那个 pk 不存在"这回事。"""
        app = _fetch_wecom_app()
        return api_response(None if app is None else app_row_payload(app))

    def post(self, request):
        """创建那把锁定应用；**已有一把 ⇒ `1000`**（`name` 可选，默认见 `DEFAULT_APP_NAME`）。两层闸缺一不可：① 先查一次（绝大多数请求走这条，写入之前回 `1000`）；② 库级条件唯一约束+`IntegrityError` 翻译（并发穿过第 1 层时收成同一个 `1000`；⛔ 第 1 层替不掉第 2 层——先查后建在并发下必被穿过，症状=页面出现两把；⛔ 第 2 层不许写成 `except IntegrityError: 1000`，会吞掉别的库级错）。
        两条约束都会撞且答复不同：并发都用默认名时后到的先撞 `name` 唯一索引 ⇒ 翻译口按回查到的实际冲突面分别给「已有一把」/「名字被占」（见 `_translate_create_conflict`）。
        ⚠️ 事务边界=**内层** `atomic()` 只包那一笔 `create()`（保存点：失败只回滚那一笔、事务仍可用，块后回查才跑得通）；⛔ 不能一个大 `atomic()` 块内 `except` 后继续查库（读到 `transaction is aborted`）、⛔ 也不能块内 `return`（`atomic()` 是事务不是回滚点 ⇒ 提交前几笔+返回 200 业务码，"一半成功"无异常）。留痕与写入同一内层 `atomic()`，给 `app_key` ⛔ 密钥不进留痕。"""
        serializer = WecomQrAppCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        name = serializer.validated_data.get("name") or DEFAULT_APP_NAME

        if _fetch_wecom_app() is not None:
            raise ValidationError({"kind": WECOM_APP_EXISTS_MSG})

        try:
            # ⚠️ **保存点**（内层 `atomic()`）：只把这一笔插入圈进来 —— 它失败时回滚的
            # 只是这一笔，外层事务（本视图没有；见 settings 无 `ATOMIC_REQUESTS`）
            # 与下面的回查都还是可用的。
            with transaction.atomic():
                app = ApiApp.objects.create(
                    name=name,
                    kind=ApiAppKind.WECOM_GROUP_QR,
                    app_key=generate_app_key(),
                    app_secret=generate_app_secret(),
                    created_by=request.user,
                )
                _record_app(
                    request, "system.open_api_app_create", app, to_value=app.app_key
                )
        except IntegrityError:
            # ⚠️ **保存点之外**：失败那一笔已经回滚干净，这里的回查看得见已提交的赢家。
            _translate_create_conflict(name)
        return api_response(app_row_payload(app))

    def patch(self, request):
        """`PATCH`：改 `state`/`callback_url`/`callback_state`/`ip_allowlist`（§7.3）。缺席=保持原值；纯 no-op 不写库不留痕（R26）；形状与 `app_views.ApiAppDetailView.patch` 同一套（每个真正变化的字段各一条留痕）。
        差别两处（§7.3 定）：取数走 `_fetch_wecom_app()`、**没创建 ⇒ `1000`**（与 `GET` 的"不存在 ⇒ `data=null`"刻意不同——读的空是正常答复、写的没资源没定义）；`name` 不在可改字段里，带了 ⛔ 一概不应用。
        判据一律 `"字段" in data` ⛔ 真值（空串是合法值，E-0021）；URL/SSRF 判据在 `AppUpdateSerializer` 字段校验器（与投递前同一函数），⛔ 本视图不复述。"""
        app = _fetch_wecom_app()
        if app is None:
            raise ValidationError({"app": APP_NOT_FOUND_MSG})

        serializer = AppUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        changes = []  # [(field_name, 变更前, 变更后)]，每个**真正变化**的字段一条
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
            # 纯 no-op（`PATCH {}` / 重发当前值）：**不写库、不写留痕**（R26 同款口径）。
            return api_response(app_row_payload(app))

        with transaction.atomic():
            for field_name, _before, _after in changes:
                setattr(app, field_name, data[field_name])
            app.save(update_fields=[field for field, _b, _a in changes])
            for field_name, before, after in changes:
                _record_app(
                    request,
                    "system.open_api_app_update",
                    app,
                    field_name=field_name,
                    from_value=before,
                    to_value=after,
                )
        return api_response(app_row_payload(app))


class WecomQrAppRotateSecretView(WecomQrAppAdminView):
    """`POST /api/open/wecom-qr-app/rotate-secret/`：轮换密钥（§7.3/§5.3）。单密钥**立即失效**（与 `/api/open/apps/<pk>/rotate-secret/` 逐字同口径）：新密钥写同一列、旧密钥当场作废。
    留痕复用 `system.open_api_app_rotate_secret`，**新旧密钥都不进留痕**（只记"轮换了"，凭据纪律）。"""

    def post(self, request):
        app = _fetch_wecom_app()
        if app is None:
            raise ValidationError({"app": APP_NOT_FOUND_MSG})
        with transaction.atomic():
            app.app_secret = generate_app_secret()
            app.secret_rotated_at = timezone.now()
            app.save(update_fields=["app_secret", "secret_rotated_at"])
            _record_app(request, "system.open_api_app_rotate_secret", app)
        return api_response(app_row_payload(app))


class WecomQrAppDeliveriesView(WecomQrAppAdminView):
    """`GET /api/open/wecom-qr-app/deliveries/`：这把锁定应用的推送台账（§7.3 末行）。口径与 `/api/open/apps/<pk>/deliveries/` 逐字相同（同一 `delivery_row_payload`/`EnvelopePagination`/`status` 筛选与排序）。
    差别只有取数：取**本租户那把锁定应用**（按 kind），不是 URL 里的 pk——D7 的 URL 形状没有 pk，也就没有"拿别人的 pk 来打"这条面。
    ⚠️ 没有这把应用 ⇒ `1000`「应用不存在」⛔ 不回空台账——「一把都没有」与「有一把但没推过」是两件事，混成一句会让页面把"还没创建"渲染成"建好了但一条推送都没有"。"""

    def get(self, request):
        app = _fetch_wecom_app()
        if app is None:
            raise ValidationError({"app": APP_NOT_FOUND_MSG})

        params = DeliveryStatusFilterSerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        status = params.validated_data.get("status")

        rows = OpenApiDelivery.objects.filter(app=app)
        if status is not None:
            rows = rows.filter(status=status)
        # 排序 = `created_at` 倒序、同刻再按 `id` 倒序（本端点的契约：分页要求顺序确定）。
        rows = rows.order_by("-created_at", "-id")

        paginator = EnvelopePagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        return paginator.get_paginated_response(
            [delivery_row_payload(row) for row in page]
        )
