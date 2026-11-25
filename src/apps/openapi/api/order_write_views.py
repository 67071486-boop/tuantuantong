"""开放平台对外 v2 的写端点：改状态 / 改发单价 / 改单（三条契约与入参见 v2 spec §6.1；改单字段白名单的判据=本模块 `_accepted_update_fields` + `OrderUpdateSerializer`）。
承重：三条端点只做「取单→校验入参→调领域核心→包信封」，状态流转判据与钱的重算全在 `apps/business/order_writes.py` 一份实现里（⛔ 本模块不写状态分支、不算一分钱）；`status` 只收枚举值（`ChoiceField(choices=OrderStatus.choices)`，⛔ 不另抄对照表）；表外的键=**拒**而非忽略（fail-closed：序列化器对只读字段静默忽略=「200+零改变+零信号」的假成功，见 `_accepted_update_fields`）。
装配=`ApiAppBearerAuthentication` + ⛔ 显式空 `permission_classes`（§13.10 一个应用=本租户全部业务数据 ⇒ `ctx.nodes` 必须给全集，代价=状态机闸对外恒真）+ 兜底闸；幂等三方法硬前置 `Idempotency-Key`（四态与事务口径见 `apps/openapi/idempotency.py`）；租户上下文只来自连接、⛔ 不读请求参数；改状态/改单必须是 mixin 的原因见各自 docstring（同一路径第二条路由永远走不到）。"""
from __future__ import annotations

from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from apps.business.api.serializers import OrderUpdateSerializer, _hall_price_amount
from apps.business.models import Order, OrderStatus
from apps.business.order_writes import (
    WriteContext,
    change_hall_price,
    transition_order,
    update_order_fields,
)
from apps.business.validation import ValidationProblem
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.openapi.authentication import ApiAppBearerAuthentication
from apps.openapi.credentials import OPENAPI_SPEC
from apps.openapi.idempotency import idempotent
from apps.openapi.serializers import money, order_payload
from apps.openapi.throttling import ApiAppFallbackThrottle
from apps.rbac.catalog import NODES

#: 全部权限节点 code（运行期从唯一出处 `apps/rbac/catalog.py` 的 `NODES` 取，⛔ 不手抄：手抄副本会在新增节点时静默变旧）。
#: §13.10「一个应用=本租户全部业务数据」在状态机那道闸上的表达：闸只看 `ctx.nodes`、不吃视图权限类
#: ⇒ 不给全集的话对外改状态会撞「缺少所需权限节点」且只在某些目标态出现（很难发现）。代价=该闸对外恒真。
OPEN_API_FULL_NODES = frozenset(entry[0] for entry in NODES)

#: 留痕操作人 / `Order.updated_by` 共用（形如「开放平台应用 测试应用甲」）。两处刻意同源：
#: 这条路上没有"人"，唯一事实是"哪个应用干的"。应用名上限 50、两列 varchar(150)
#: ⇒ 拼出最长 57 字符，不撞列宽、⛔ 不会被静默截断。
OPEN_API_ACTOR_TEMPLATE = "开放平台应用 {}"

#: 金额出口的位数说明只作注释：真正的两位小数由 `apps/openapi/serializers.py::money` 给（唯一出处）。

#: **改单**端点对"表外的键"的那句文案。⚠️ 与 `order_create_views.UNKNOWN_FIELDS_MSG`
#: **逐字相同**（对外只该有一种说法）——各留一份是因为 `order_create_views` import 本模块的
#: `_write_context` ⇒ 反向 import 会成环；由用例把两句字面钉成相等（谁改一边就红）。
UNACCEPTED_FIELDS_MSG = "请求里有本 API 不接受的字段：{}"

#: 非对象请求体的那句文案（与录单端点同一形状；理由见 `OrderUpdateMixin.patch` 的 docstring）。
NOT_A_JSON_OBJECT_MSG = "请求体必须是一个 JSON 对象"


class _OpenApiOrderUpdateSerializer(OrderUpdateSerializer):
    """对外改单保留原字段白名单；内部编辑增加的接单价不向外开放。"""

    total_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True
    )


def _accepted_update_fields() -> frozenset:
    """改单端点接受的键集 = `OrderUpdateSerializer` 的可写字段（运行期取，⛔ 不手抄名字：手抄副本会静默变旧）。
    两个面：「哪些字段能改」的事实只有序列化器一个出处（这里只是读出来）；但序列化器对"传了只读字段"是**静默忽略**
    （DRF 只读字段根本不进 `validated_data`）⇒ 「200+零改变+零信号」假成功 ⇒ 对外必须在它之前自己判**拒还是放行**（⛔ 不是再造一份可写判据）。"""
    serializer = _OpenApiOrderUpdateSerializer()
    return frozenset(
        name for name, field in serializer.fields.items() if not field.read_only
    )


def _live_order(order_no):
    """按我方订单号取**在用的**那张单（取不到 ⇒ `None`）。
    走默认 manager ⇒ 已软删取不到（D-0144，与对外读面同口径：软删是租户内部状态、第三方不该改，「改不到」与「不存在」同形 `1002`）；隐藏单（大厅卡/上游卡在挂）⇒ exclude 掉、按「不存在」`1002`。
    本模块只有这一处 `Order.objects` 落点（两条写端点共用；⛔ 各写一份=第二个判据，AGENTS §5-E）。"""
    # ⭐ 评审 Important 4：隐藏单不得被对外改单（与删 / 恢复同一口径：按「不存在」1002）。
    return (
        Order.objects.filter(order_no=order_no)
        .exclude(hall_card__isnull=False)
        .exclude(upstream_hall_card__isnull=False)
        .first()
    )


def _require_order(order_no):
    """取那张在用的单；取不到 ⇒ `NotFound`（经本仓异常处理器变 `code=1002` + HTTP 200）。
    与对外读面**同一个出口**（`NotFound`，⛔ 不是 404、也不是自造文案）。"""
    order = _live_order(order_no)
    if order is None:
        raise NotFound()
    return order


def _write_context(app) -> WriteContext:
    """把「哪个应用在写」装成领域层的 `WriteContext`（逐字段理由见模块 docstring 的表）。"""
    label = OPEN_API_ACTOR_TEMPLATE.format(app.name)
    return WriteContext(
        actor=label,
        log_actor=label,
        # ⚠️ `None` ⇒ 领域核心内部那几处 `system=...` 成立 ⇒ 留痕落成**系统行**。
        # ⛔ 本模块不往 `record()` 传 `system=`（那是 `order_writes` 的事）。
        log_actor_id=None,
        nodes=OPEN_API_FULL_NODES,
        # ⚠️ 恒 `None`（理由见模块 docstring 的表：Bearer 下 `request.user` 是 AnonymousUser）。
        user=None,
    )


def _no_game_admission(game) -> None:
    """对外路径的**写侧游戏准入**判据 = **显式不做**（§13.10「一个应用凭据=本租户全部业务数据」在准入层的表达；⛔ 不是"漏了闸"，准入判据在更外层被整体取消）。
    ⛔ 不用 `request_user=None` 让领域核心跳过——那会让「忘了传用户」与「按裁定不判」同形状（静默 fail-open）；把"不判"写成显式判据、由调用方逐字传下去（领域侧形参语义见 `order_writes.update_order_fields` 的 docstring）。
    ⚠️ 它拦不住的：`validate_order_chain` 仍在领域核心照跑——这条路挡的是"游戏链不自洽"，不是"你没权改这款游戏"。"""
    return None


class OrderStatusChangeSerializer(serializers.Serializer):
    """`POST .../status/` 的入参（唯一校验口）。`status` 用 `ChoiceField(choices=OrderStatus.choices)`：只收枚举值，中文 label/未知串落 `invalid_choice` ⇒ `1000`；
    ⛔ 不用 `CharField` + 自己做 `in OrderStatus.values`（那就是第二份判据）。
    `remark` 限长 255 —— 与落地的 `OperationLog.reason` 列同源（⛔ 不让「请求能填、库里静默截断」存在）。"""

    status = serializers.ChoiceField(
        choices=OrderStatus.choices,
        error_messages={
            "required": "缺少 status",
            "invalid_choice": "状态取值不正确：只收枚举值（例如 IN_PROGRESS）",
        },
    )
    remark = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )


class OrderAssignPriceSerializer(serializers.Serializer):
    """`PUT .../assign-price/` 的入参（唯一校验口）。金额闸**复用**内部实现（`_hall_price_amount`：可解析/≤2 位小数/总位数 ≤12/整数位 ≤10，故意不设下限——`0` 与负数都是合法金额，D-0015）；
    ⛔ 不重写一份 `DecimalField(...)`（第二份判据；先例：对外 `bills/` 复用内部结算筛法，见 `order_extra_views` 模块 docstring）。
    ⚠️ 与内网 `/hall-price/` 的刻意差异：内网还收 `"50%"` 比例形态，对外契约只有金额一支（§6.1）⇒ `"50%"` 在这条路上 `1000`（比例形态要算钱，对外多一种形态=多一份契约）。"""

    price = serializers.CharField(
        max_length=32,
        required=True,
        allow_blank=False,
        error_messages={"required": "缺少 price", "blank": "发单价不能为空"},
    )
    reason = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )

    def validate(self, attrs):
        attrs["price"] = _hall_price_amount(attrs["price"])
        return attrs


class ApiOrderWriteAssemblyMixin:
    """两条写端点的共同装配（与只读侧 `ApiOrderExtraView` 逐字相同）：① 认证=`ApiAppBearerAuthentication`（通过时 `request.auth` 是那个 `ApiApp`，幂等台账与留痕读它）；
    ② ⛔ 显式空 `permission_classes`（§13.10 裁定；空列表="不做权限检查"，与"没写属性"吃 `IsAuthenticated` 不是一回事）；
    ③ 兜底闸 `[ApiAppFallbackThrottle]`（极宽兜底 ⛔ 不是配额；装类属性上 ⇒ `initial()` 顺序保证 401 先于 429、被挡请求不进桶）。"""

    authentication_classes = [ApiAppBearerAuthentication]
    permission_classes = []
    throttle_classes = [ApiAppFallbackThrottle]


class OrderStatusChangeMixin(ApiOrderWriteAssemblyMixin):
    """`POST /api/open/v1/orders/<order_no>/status/`（改状态）。**必须是 mixin**：`GET /status/` 已挂只读处理器（`order_extra_views.OrderStatusView`），契约要求 POST 同一条路径——
    Django `URLResolver.resolve` 按 `urlpatterns` 顺序取第一个匹配 ⇒ 同一路径上注册第二条路由永远走不到（第一个视图对 POST 回 405）⇒ 两方法必须同住一个视图类。
    ⭐ 本 mixin 自带装配（继承 `ApiOrderWriteAssemblyMixin`）而⛔ 不依赖宿主：宿主与本 mixin 装配值相同 ⇒ 无漂移风险。"""

    @idempotent(spec=OPENAPI_SPEC)
    def post(self, request, order_no):
        """改状态：`{"status": <枚举值>, "remark": ""}` ⇒ `{order_no, status, status_text}`。
        响应的 `status`/`status_text` 取自**领域核心改完后**那份对象（`apply_transition` 就地改写，只有合法边走得到这里）。"""
        serializer = OrderStatusChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = _require_order(order_no)
        outcome = transition_order(
            order,
            target=serializer.validated_data["status"],
            # ⚠️ `remark` 一路透传到留痕行的 `reason` 列（列宽 255，与入参上限同源）；
            # ⛔ 不传 `system=`（那是 `order_writes` 内部按 `log_actor_id is None` 判的）。
            remark=serializer.validated_data["remark"],
            ctx=_write_context(request.auth),
            dry_run=False,
        )
        if not outcome.ok:
            # 可预期的业务失败（非法流转/已结账/钩子前置…）：不写任何东西；幂等占位行还在 ⇒
            # 由 `idempotency._run` 按「业务码 != 0」整笔回滚（`set_rollback(True)`），键下次可重试
            # （AGENTS §5-E / E-0022：⛔ 不在这里裸 `return` 退出事务）。
            return api_response(data=None, msg=outcome.msg, code=outcome.code)
        order = outcome.detail["order"]
        return api_response(
            {
                "order_no": order.order_no,
                "status": order.status,
                "status_text": order.get_status_display(),
            }
        )


class OrderUpdateMixin:
    """`PATCH /api/open/v1/orders/<order_no>/`（改单；契约与白名单见模块 docstring）。**必须是 mixin**（框架事实）：`GET` 详情已挂 `order_query_views.OrderDetailView`、契约要求 PATCH 同一条路径，同一路径第二条路由永远走不到 ⇒ 两方法同住一个视图类。
    ⚠️ 本 mixin **刻意不自带装配**（与 `OrderStatusChangeMixin` 相反）：宿主两方法授权面不同——`GET` 走 v1 节点门、`PATCH` ⛔ 不装权限类（§13.10）⇒ 装配必须由宿主按方法给（`OrderDetailView.get_permissions`）。先例：`order_create_views.OrderCreateMixin`。
    成功=HTTP 200+`code=0`+改完后那张单的查单载荷（`order_payload`，与 `GET` 同一出口）；幂等必带 `Idempotency-Key`（缺失/不合规 ⇒ `1000`）。"""

    @idempotent(spec=OPENAPI_SPEC)
    def patch(self, request, order_no):
        """改单：入参=序列化器可写字段（白名单见模块 docstring）。**入参判据只有一处**：`OrderUpdateSerializer`（与内部 `PATCH /api/business/orders/` 同一个）——只读字段（`status`/金额/`sales_user`/审计列）传了不生效（DRF 只读字段不进 `validated_data`），判据打在库里的值上。
        `update_order_fields` 把 `ValidationProblem` 原样上抛（spec §D5），这里统一翻成信封（`problem.code`）；⛔ 本方法不自己开 `atomic()`、⛔ 不在 `except` 里 return 退出事务——外层事务是幂等入口 `_run` 的（业务码≠0 且无 `mark_side_effect` ⇒ 占位行整笔回滚、键可重试），⛔ 不在这里裸写 `set_rollback`（与 `_run` 抢同一件事，E-0022 孪生）。
        判据顺序（顺序本身是判据，与录单端点同形）：①非对象体 ⇒ 拒（`request.data` 对 JSON 数组/标量给 `list`/标量，`.items()` 抛 `AttributeError` ⇒ 500；AGENTS §5-E 判"这类请求还会不会 500"）→ ②表外的键 ⇒ 拒（`1000`+msg 点名）→ ③序列化器 → ④取单（取不到 ⇒ `1002` 同形）→ 领域核心。"""
        raw = request.data
        if not isinstance(raw, dict):
            return api_response(
                data=None, msg=NOT_A_JSON_OBJECT_MSG, code=ErrorCode.VALIDATION
            )
        # ⚠️ `sorted()` 只为**文案稳定**（同一份报文给出同一句 msg ⇒ 判据可以写死那句话）。
        unaccepted = sorted(set(raw) - _accepted_update_fields())
        if unaccepted:
            return api_response(
                data=None,
                msg=UNACCEPTED_FIELDS_MSG.format("、".join(unaccepted)),
                code=ErrorCode.VALIDATION,
            )
        order = _require_order(order_no)
        serializer = _OpenApiOrderUpdateSerializer(order, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        try:
            outcome = update_order_fields(
                order,
                validated=serializer.validated_data,
                # ⚠️ 恒 `None`：这条路上**没有"操作人"这个人**（Bearer 下 `request.user`
                # 是 `AnonymousUser`，`games_of(anonymous)` 是空集）⇒ ⛔ 不能拿它当准入主体
                # （那会把每一次改单都拒成 `1000`）。准入判据由下面那个显式形参给。
                request_user=None,
                ctx=_write_context(request.auth),
                dry_run=False,
                # ⭐ §13.10「一个应用凭据 = 本租户全部业务数据」在写侧准入这一层的表达
                # （逐条理由见 `_no_game_admission`）。
                assert_game=_no_game_admission,
            )
        except ValidationProblem as problem:
            # 逐字照内网单笔那条路（`order_views.partial_update`）：同一个 `problem.code`
            # 与同一句 `str(problem)`，⛔ 不新造文案、⛔ 不把它伪装成 500。
            return api_response(data=None, msg=str(problem), code=problem.code)
        return api_response(order_payload(outcome.detail["order"]))


class OrderPriceAssignView(ApiOrderWriteAssemblyMixin, APIView):
    """`PUT /api/open/v1/orders/<order_no>/assign-price/` —— 改发单价。入参 `{"price": "<字符串两位小数>", "reason": ""}`；出参 `{order_no, hall_price}`；金额一律字符串两位小数（`money()` 唯一出口；⛔ 不用 float、⛔ 把"没值"渲染成 `"0.00"`——`null` 与 `0` 是两件事，D-0015）。
    已结账闸（`1014`，⛔ 别改码）在领域核心 `change_hall_price`（`Performance.is_settled`：打单工资行已结账 ⇒ 拒、一行不写）；本模块不复述判据、只原样回 `outcome.code`；闸排在写入之前 ⇒ 「被拒时库里一字未变」由领域层保证，用例打在库里的值上。
    写价连带（重算预计绩效/下游排事件/留痕）都是领域核心分内事，⛔ 本模块不逐个复述、也⛔ 不自己补一行留痕。"""

    @idempotent(spec=OPENAPI_SPEC)
    def put(self, request, order_no):
        serializer = OrderAssignPriceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = _require_order(order_no)
        outcome = change_hall_price(
            order,
            price=serializer.validated_data["price"],
            reason=serializer.validated_data["reason"],
            ctx=_write_context(request.auth),
            dry_run=False,
        )
        if not outcome.ok:
            # 已结账那一档（`1014`）与其它可预期失败都从这里原样出去（同上：不留占位行）。
            return api_response(data=None, msg=outcome.msg, code=outcome.code)
        order = outcome.detail["order"]
        return api_response(
            {
                "order_no": order.order_no,
                # ⚠️ `money()` 是仓内**唯一**的金额字符串出口（两位小数；`None` ⇒ `null`）。
                "hall_price": money(order.hall_price),
            }
        )


#: 供用例 import 的「两个写端点路径」常量（⛔ 不放进 `urls.py`：那里只管路由）。
STATUS_PATH_TEMPLATE = "/api/open/v1/orders/{order_no}/status/"
ASSIGN_PRICE_PATH_TEMPLATE = "/api/open/v1/orders/{order_no}/assign-price/"


# ⚠️ 本模块**不新开错误码**：幂等四态用的是既有的 `1000` / `1001`
# （`apps/openapi/idempotency.py`），业务失败用的是领域核心给的 `outcome.code`
# （非法流转 `1002`、已结账改价 `1014`、钩子的 `exc.code`）。
