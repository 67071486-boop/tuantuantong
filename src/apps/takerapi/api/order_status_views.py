"""改状态（打手侧对外端点，`POST /api/taker/v1/orders/<order_no>/status/`，spec `docs/specs/2026-10-08-taker-open-api-design.md` §5 端点表第 5 行/§5.3/§9-7）。⛔⭐ 范围判据 = 「他是这张单的当前接手人」（不是游戏范围）：`claimed_order_of` 是唯一出处，⛔ 不 import 大厅那套游戏范围集（会让"范围内、别人接的单"被改到）。判据顺序承重：① 幂等入口（缺键 ⇒ `1000`）→ ② 枚举（序列化器全枚举）→ ③ 授权面（`1002`，必须排在 ④ 之前——否则答复会泄露"这张单存在"，§5.3-2 同形）→ ④ P3 目标态白名单（只许对外写 `{PENDING_REVIEW, ISSUE, PAUSED}`，「已完成」由我方验收，spec §5.3-4/§1；白名单是枚举字段之后的显式闸，判据 = "白名单外的合法枚举值"必须 `1000` 且能过枚举校验，见 `tests/test_takerapi_order_status_api.py`）→ ⑤ 领域口 `transition_order`（⛔ 本模块不写状态、不本地判边；节点门 = `nodes_of(taker)` 不传常量集；`remark` 透传到留痕 `reason` 列）。
留痕：成功后恰好一条 `order.transition`、失败 = 0 行（判据打在 `OperationLog` 的行上，E-0009）；⛔ 本 handler 不调用 `mark_side_effect`（一次失败必须能同键重试）。⚠️ 如实登记：幂等回放不重查范围判据，旧接手人拿旧键重发可能拿到旧成功信封（不写库、不泄漏新信息；单现在还是不是他的以查单/我的单为准；⛔ 不改共享承重的 `idempotency.py`）。"""
from __future__ import annotations

from rest_framework import serializers
from rest_framework.views import APIView

from apps.business.models import OrderStatus
from apps.business.order_status import ISSUE_REASON_REQUIRED_MSG
from apps.business.order_writes import WriteContext, transition_order
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.openapi.idempotency import idempotent
from apps.rbac.permissions import nodes_of
from apps.takerapi.api.order_scope import TAKER_ORDER_NOT_FOUND_MSG, claimed_order_of
from apps.takerapi.authentication import TakerAppBearerAuthentication
from apps.takerapi.credentials import TAKER_SPEC
from apps.takerapi.throttling import TakerAppFallbackThrottle

#: 留痕的操作人（形如「打手侧应用 测试应用甲」）。⚠️ 与上家侧那句（`开放平台应用 {}`）
#: **刻意不同**：读日志的人要能一眼看出这是**哪一族**的凭据干的（T5 已把这条口径立起来）。
#: 长度：`OperationLog.actor` 是 varchar(150)，应用名上限 50 ⇒ 拼出来最长 56，撞不到列宽。
TAKER_API_ACTOR_TEMPLATE = "打手侧应用 {}"

#: ⭐⭐ P3 的目标态白名单（spec §5.3-4/§9-7/§1 裁定）。⛔ 这是一份字面量、不是「枚举减掉几个」的派生式：
#: 派生会让 `OrderStatus` 新增成员时自动对外可写而没有任何网会红；⛔ 也不许加键（「已完成」只能由我方验收）。
#: ⚠️ 它是白名单、⛔ 不是"状态机"：三个以外的目标态即使状态机上有边（D-0147 整体放开）也一律拒——两道闸都要在、判据各一条。
TAKER_STATUS_WHITELIST = (
    OrderStatus.PENDING_REVIEW,
    OrderStatus.ISSUE,
    OrderStatus.PAUSED,
)

#: 白名单外那一档的对外文案（spec §5.3-4 要求点名白名单）。⚠️ 中文名取自既有唯一出处
#: `OrderStatus.choices`（⛔ 不新抄中文表，与 `order_status.self_transition_message` 同口径）：
#: 由 `_whitelist_msg()` 现拼；形状稳定（成员顺序 = 白名单顺序）⇒ 用例可写死整句。
WHITELIST_MSG_TEMPLATE = "对外只允许把状态改成：{}"


def _whitelist_msg() -> str:
    """「只允许改成哪三个」那一句（中文名走 `OrderStatus.choices`，见常量的注释）。"""
    labels = dict(OrderStatus.choices)
    return WHITELIST_MSG_TEMPLATE.format(
        "、".join(f"「{labels[str(value)]}」" for value in TAKER_STATUS_WHITELIST)
    )


class TakerOrderStatusChangeSerializer(serializers.Serializer):
    """`POST .../status/` 的入参（唯一的校验口）。⚠️ `status` 用 `ChoiceField(choices=OrderStatus.choices)` = 全枚举：它只保证"是个合法枚举值"，⛔ 不是白名单——白名单判据必须打在白名单那一层（退化成"字段先拦"则用例测的是字段校验，删掉白名单照样绿）。
    中文 label 与未知串都落 `invalid_choice` ⇒ `1000`；⛔ 不用 `CharField`+`in OrderStatus.values`（第二份枚举判据）。
    ⚠️ `remark` 限长 255——与最终落地的 `OperationLog.reason` 列同源（⛔ 不让"请求能填、库里静默截断"存在）。"""

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


def _write_context(app) -> WriteContext:
    """把「哪个应用在写」装成领域层的 `WriteContext`（与上家侧同形，⛔ 不抄判据）。⚠️ `nodes` 来自绑定账号（`nodes_of(app.taker)`）而不是常量集：spec §5.3-4 要求「他有没有那条边的权限，仍由他那套节点回答」（传 `OPEN_API_FULL_NODES` 会让身份维度恒真；判据 = 撤掉 `order.change_status` ⇒ 改状态不通）。
    ⚠️ `log_actor_id=None` + `system=True` 必须成对：这条路上没有"人"这个操作人，`record()` 对"没有操作人却没声明 system"是 fail-closed 抛错。"""
    label = TAKER_API_ACTOR_TEMPLATE.format(app.name)
    return WriteContext(
        actor=label,
        log_actor=label,
        log_actor_id=None,
        nodes=nodes_of(app.taker),
        # ⚠️ 恒 `None`：Bearer 下 `request.user` 是 `AnonymousUser`，
        # ⛔ 不能拿它当准入主体（`games_of(匿名)` 是空集）。
        user=None,
        # 已核对接单人，操作来自打手自己的应用。
        notify_worker=False,
    )


class TakerOrderStatusChangeView(APIView):
    """`POST /api/taker/v1/orders/<order_no>/status/` —— 改状态（spec §5 端点表第 5 行）。"""

    authentication_classes = [TakerAppBearerAuthentication]
    #: ⛔ 显式空列表 = **不做权限检查**（不写它会吃全局默认的 `IsAuthenticated`
    #: ⇒ 合法令牌反被判 401；与 T5 两条对外端点同款）。
    permission_classes = []
    #: 兜底闸（DRF 的 `initial()` 顺序 = 认证 → 权限 → 限流 ⇒ 401 先于 429，且不进桶）。
    throttle_classes = [TakerAppFallbackThrottle]

    def get_claimed_order(self, taker, order_no):
        return claimed_order_of(taker, order_no)

    def write_context(self, app):
        return _write_context(app)

    @idempotent(spec=TAKER_SPEC)
    def post(self, request, order_no):
        """改状态：`{"status": …, "remark": ""}` ⇒ `{order_no, status, status_text}`。"""
        serializer = TakerOrderStatusChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        app = request.auth
        taker = app.taker
        # ⭐ ③ 授权面（**排在白名单之前**，见模块 docstring 的「判据顺序」）：
        # 不是他的单 ⇒ `1002`，与"查不到"**同一句文案**（spec §5.3-2 的"同形"）。
        order = self.get_claimed_order(taker, order_no)
        if order is None:
            return api_response(
                data=None, msg=TAKER_ORDER_NOT_FOUND_MSG, code=ErrorCode.BUSINESS
            )
        target = serializer.validated_data["status"]
        # ⭐⭐ ④ P3 白名单：白名单外的合法枚举值也在这里被拒（⛔ 不是被枚举字段拦）。走到这一行的 `target`
        # 一定是合法枚举值（序列化器已过）⇒ 本条判据的判别力独立于字段校验：把白名单删掉/换成别的集合，
        # `tests/test_takerapi_order_status_api.py` 的"白名单外合法枚举值"那条会红。
        if target not in TAKER_STATUS_WHITELIST:
            return api_response(
                data=None, msg=_whitelist_msg(), code=ErrorCode.VALIDATION
            )
        remark = serializer.validated_data["remark"]
        if target == OrderStatus.ISSUE and not remark:
            return api_response(msg=ISSUE_REASON_REQUIRED_MSG, code=ErrorCode.VALIDATION)
        outcome = transition_order(
            order,
            target=target,
            remark=remark,
            ctx=self.write_context(app),
            dry_run=False,
        )
        if not outcome.ok:
            # 可预期的业务失败（非法流转 / 已结账 / 退款钩子前置校验…）：一行不写。此时幂等占位行还在
            # ⇒ 由 `idempotency._run` 按"业务码 != 0"整笔回滚（`transaction.set_rollback(True)`）⇒ 这个键下次可重试
            # （`AGENTS.md` §5-E / E-0022：⛔ 不在这里裸 `return` 退出事务）。
            return api_response(data=None, msg=outcome.msg, code=outcome.code)
        order = outcome.detail["order"]
        return api_response(
            {
                "order_no": order.order_no,
                "status": order.status,
                "status_text": order.get_status_display(),
            }
        )
