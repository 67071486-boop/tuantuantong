"""录单的**领域核心**（唯一实现；开放平台 v2 设计文档 §6.2 铁律 1 / `AGENTS.md` §5-E）。录单原先整段长在 `OrderViewSet.create` 里，开放平台对外录单端点要求**复用同一份**建单逻辑（⛔ 不许在 `apps/openapi/**` 抄第二份）⇒ 两个调用方（内部页面 / `POST /api/open/v1/orders/`）调同一个 `create_order`；**行为逐字不变**（函数体是搬过来的；判据 = `tests/test_order_api.py` / `test_order_create_throw_to_hall.py` / `test_order_quick_assign.py` / `test_openapi_order_create.py::CreateParityTests`）。⛔ **三道闸都在建单之前判完、一行不落库**（都回 1000）：`throw_to_hall` 与 `assignee` 互斥；抛单时 `hall_price` **解析后的数值** `> 0`（E-0021：`'0.00'` 是真值却是非法价）；`remember_assignee == "hall"` 必须本次 `throw_to_hall=true`。
`IntakeActor` 把「谁在录」显式化（⛔ 不是把 `request` 传进来）：内部与对外路径在 `created_by` / `sales_user` / `sales_agent` 兜底 / 留痕与状态机节点集四处不同，逐字段理由见 dataclass 的注释。⭐ 指定销售客服归属：可选键 `sales_user` ⇒ `sales_user`（提成归属）与 `sales_agent`（名字快照）同时指向那个人，权限门 = `has_node_now(actor.user, "order.allow_change_sales_agent")`（与换归属专用端点同一份判据，⛔ 不是 `in actor.nodes`——那一句在对外路上恒真）；对外录单走不到它（两键都不在对外字段白名单里）。
事务边界（⛔ 别合并）：① 建单 + 绩效 + 留痕同一个 `atomic()`（spec §7.4 同生共死）；② 抛单在**第二个** `atomic()`（失败带不走订单）；③ 指派在建单事务之外（`assign_order` 自己管）。连带要求：建单成功之后才可能出现非 0 业务码（1022 / 1023）⇒ 调用方要能声明「副作用已落库」（`apps/openapi/idempotency.py::mark_side_effect`；本模块不 import 它）。"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import re
from typing import Callable

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business import jidanpu_member, split_group
from apps.business.api.serializers import PriceChangeSerializer
from apps.business.commission import CommissionError
from apps.business.customers import upsert_customer
from apps.business.models import Customer, CustomerKind, MONEY_FIELDS, Order, OrderStatus
from apps.business.order_no import generate_order_no
from apps.business.order_source import source_snapshot_overrides
from apps.business.order_status import IllegalTransition, apply_transition
from apps.business.order_writes import (
    WriteContext,
    assign_order,
    clip_sales_agent,
    resolve_sales_agent_candidate,
    set_default_assign_preference,
)
from apps.business.performance import create_performance_for_order
from apps.business.performance_amount import AmountError as _AmountError
from apps.business.system_switches import switch_enabled
from apps.business.validation import (
    ValidationProblem,
    normalize_project_text,
    validate_order_chain,
    validate_required,
)
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
# ⚠️ 本模块只借这一个判据（`order.allow_change_sales_agent` 节点门）：它与换销售客服专用端点必须是同一份判据
#    （§5-E「同一事实不要两套判据」）。⛔ 别改成 `"…" in actor.nodes`：那一句在对外录单路上恒真
#    （`OPEN_API_FULL_NODES` 是全部节点），这里要的语义是「**人**持不持这个点」——`actor.user is None` 时 fail-closed。
from apps.rbac.permissions import has_node_now


@dataclass(frozen=True)
class IntakeActor:
    """「谁在录这一单」这一组事实（逐字段理由见模块 docstring 的那张表）。"""

    #: 认证态用户；**对外录单恒 `None`**（那条路上没有 `User`）。
    user: User | None
    #: 审计记录的操作人，也是未指定订单归属时的录单人。
    log_actor: str
    #: 留痕的 `actor_id` 列（`None` ⇒ 系统行；非 None 时必须是一个真实用户主键）。
    log_actor_id: int | None
    #: 状态机那道闸的节点集。
    nodes: frozenset
    #: 指派 / 抛单交给领域核心的上下文。
    write_ctx: WriteContext
    #: 请求没给 `sales_agent` 时的兜底（对外 = 空串）。⚠️ 它还是「缺 `sales_user` 键时服务端将写入的那个名字」的
    #: **唯一取值口**——名字闸拿载荷带的非空 `sales_agent` 与它逐字比、不等就回 `1000`（⛔ 别在别处另算一遍显示名；
    #: 指定了 `sales_user` 时那个名字改由 `resolve_sales_agent_candidate` 回，同样是唯一取值口）。
    sales_agent_fallback: str = ""
    #: 录单人的游戏准入判据（内部 = `assert_game_allowed`；对外 = `None`，见模块 docstring）。
    assert_game: Callable[[object], None] | None = None
    #: ⭐ **D-0223**：这一路录单受不受「禁止录入相同的来源单号」开关管。内部（管理端）= `True`；对外 = 不传（默认 `False`）。
    #: ⚠️ `False` **不是「忘了传」**：业务方裁定本开关**只拦手工**——自动通道（peer / 鸡蛋铺上家同步，不进本函数）
    #: 与开放平台建单不受开关管 ⇒ 与 `assert_game` 的 `None` 同一条纪律：路径差异显式化，⛔ 不让核心去猜 `actor.user is None`。
    guard_duplicate_source_order_no: bool = False
    #: 仅服务端设置。审批自动建单可将订单归给发起人，审计仍记录系统。
    created_by: str | None = None


@dataclass(frozen=True)
class IntakeOutcome:
    """录单核心的返回：**与搬迁前逐字相同的信封** + 那张单（没建成 ⇒ `None`）。⚠️ `order` 这一格存在的理由只有一个：调用方要能分辨「单**真的**建成了」与「前置闸 / 校验拒了」——`1022` / `1023` 的 `code` 非 0 而单**已经**落库，开放平台幂等入口按它决定台账去留（`idempotency.mark_side_effect`）。⛔ 别拿它当「这一单成功了」用：抛单 / 指派成没成由 `response` 的码与 `order.status` 说。
    """

    response: Response
    order: Order | None


def resolve_assign_taker(user_id):
    """按 `user_id` 取一个**可被指派**的用户；不可用 ⇒ `(None, 那个 1000 信封)`。本函数是「指定的接手人能不能用」这条判据的**唯一实现**（内部 `/assign/` 与录单核心共用；⛔ 别再抄一份文案——两份必然漂移，后果是某个入口悄悄绕过一道闸）。⚠️ 「改钱要 `order.submit_payout_price` 节点」那一半不在这里（读 `request.user`、属视图层授权判据，留在 `OrderViewSet._resolve_assign_taker`；录单路不带 `price` / `fee` 走不到那两支）。
    ⚠️ **停用号不可被指派**（业务方裁定：停用号登不进来、单子落到它头上就卡死）⇒ 回 `1000`（⛔ 不是新造码也不是 `1002`）；刻意**不限定职位**（同一次裁定：兼岗账号允许被指派）。
    """
    taker = User.objects.filter(pk=user_id).first()
    if taker is None:
        return None, api_response(data=None, msg="指定的用户不存在", code=ErrorCode.VALIDATION)
    if not taker.is_active:
        return None, api_response(data=None, msg="指定的账号已停用", code=ErrorCode.VALIDATION)
    return taker, None


#: ⭐ **拆单（线 B）**：拆单的**切分口径唯一出处**（按 `/` 切、去空白、丢空段）。
#: ⚠️ 只服务**录单**那条入口：详情页那条入参本来就是列表（`SplitOrderSerializer.projects`），
#: ⛔ 不再套一层切分（两层切分必然漂移）。
def split_project_text(project_text) -> list[str]:
    """`'a / b//c'` ⇒ `['a', 'b', 'c']`；`''` / `'a'` ⇒ 长度 0 / 1。"""
    return [segment.strip() for segment in (project_text or "").split("/") if segment.strip()]


#: 子单金额默认归零。新版手填拆单仅覆盖子单接单价，并从原单接单价扣除相应金额。
SPLIT_MEMBER_ZERO_PRICES = {field: Decimal("0.00") for field in MONEY_FIELDS}


def _duplicate_source_rejection(source_no):
    if source_no and Order.objects.filter(source_order_no=source_no, deleted_at__isnull=True).exists():
        return IntakeOutcome(
            response=api_response(
                data=None,
                msg=f"来源单号 {source_no} 已存在（系统设置已开启禁止重复录入），不能重复录入",
                code=ErrorCode.VALIDATION,
            ),
            order=None,
        )
    return None


def _phone_keys(phone):
    """电话的几种常见写法（原始值 / 纯数字 / 带 +86 / 86）去重；判据只做同来源匹配。

    ⚠️ 业务方确认：电话只作 buyer_eid 的兜底，二者有其一命中就算老客；不做跨来源。
    """
    raw = (phone or "").strip()
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 13 and digits.startswith("86"):
        digits = digits[2:]
    keys = {raw, digits, f"+86{digits}", f"86{digits}"} if digits else {raw}
    return {key for key in keys if key}


def _auto_customer_kind(*, customer, order_source_id, customer_phone=""):
    """同来源 + 同客户**或**同电话有未删除历史单 ⇒ 老客，否则新客；都判不出 ⇒ 空串。

    只读判定；调用方已在同一事务里锁住 Customer 行（没有客户档案时没有可锁的行）。
    ⚠️ 历史单不含当前单（此时还没落库）；电话是 buyer_eid 的兜底，二者有其一即命中。
    """
    if order_source_id is None:
        return ""
    identity = Q()
    if customer is not None:
        identity |= Q(customer_id=customer.pk)
    phone_keys = _phone_keys(customer_phone)
    if phone_keys:
        identity |= Q(customer_phone__in=phone_keys)
    if not identity:
        return ""
    exists = Order.objects.filter(identity, order_source_id=order_source_id).exists()
    return CustomerKind.OLD if exists else CustomerKind.NEW


def create_order(*, serializer, assign_input, actor, payload_for_order):
    """创建订单并按请求指派或发厅。录单即抛在建单前检查发厅权限，
    拆单时仅首笔执行后续动作。业务规则见 docs/code-comment-notes.md。"""
    data = serializer.validated_data
    assignee_id = assign_input.validated_data.get("assignee")
    remember_assignee = assign_input.validated_data.get("remember_assignee")
    # ⭐ 2026-11-10 客户线（W1）：可选买家身份键。非空 ⇒ 建单事务里 upsert 客户档案并连单
    #    （拆单时组内各笔挂同一个客户：同一次购买）；空 / 缺省 ⇒ 不建不连（手工 / 对外录单没有标识）。
    #    判据用 strip 后的真值：`"  "` 过了 trim_whitespace 后本就是空串，这里不再裁第二次。
    buyer_eid = assign_input.validated_data.get("buyer_eid") or ""
    buyer_nick = assign_input.validated_data.get("buyer_nick") or ""
    split_orders = assign_input.validated_data.get("split_orders")
    split_kind = ""
    members = [{"project_text": normalize_project_text(data.get("project_text"))}]
    if split_orders is not None:
        original_total = data.get(
            "total_price", Order._meta.get_field("total_price").get_default()
        )
        child_total = sum((item["total_price"] for item in split_orders), Decimal("0.00"))
        remaining = PriceChangeSerializer(data={"total_price": original_total - child_total})
        if not remaining.is_valid():
            return IntakeOutcome(
                response=api_response(
                    data=None, msg="拆分后原订单接单价超出有效金额范围", code=ErrorCode.VALIDATION
                ),
                order=None,
            )
        members[0]["total_price"] = remaining.validated_data["total_price"]
        members.extend(
            {
                **SPLIT_MEMBER_ZERO_PRICES,
                "project_text": normalize_project_text(item["project_text"]),
                "total_price": item["total_price"],
                # 标签和查码退避只属于来源单，与管理订单拆分一致。
                "order_label": "",
                "qr_fetch_suppress_until": None,
            }
            for item in split_orders
        )
        split_kind = split_group.PEER
    elif assign_input.validated_data.get("split"):
        segments = split_project_text(data.get("project_text"))
        if len(segments) >= 2:
            members = [{"project_text": segments[0]}]
            members.extend(
                {"project_text": segment, **SPLIT_MEMBER_ZERO_PRICES} for segment in segments[1:]
            )
            split_kind = split_group.LEGACY
    split_group_id = generate_order_no() if split_kind else ""

    # 「建单即抛单」那一支的开关（缺省 `False`；它是**布尔开关**不是三态，
    # 所以缺键与显式 `false` 是同一件事，见 `CreateOrderAssignSerializer` 的说明）。
    throw_to_hall = bool(assign_input.validated_data.get("throw_to_hall"))
    if throw_to_hall and "order.send_to_grab_hall" not in actor.nodes:
        raise PermissionDenied("你没有将订单发到内部抢单大厅的权限")
    # ⭐ **业务方拍板「换销售客服 = 提成也换人」**：录单页选了**别人**时前端把目标用户主键放在这里 ⇒ `sales_user`（提成归属）与 `sales_agent`（名字快照）同时指向那个人。缺键 = 维持现状；带了键却不持 `order.allow_change_sales_agent` ⇒ 拒绝（HTTP 403，与换归属端点同一道门，⛔ 不是静默忽略，理由见 docstring）；判据用 `is not None`（`0` 过不了 `min_value=1`，那一档由 DRF 回 1000）。
    #    ⚠️ 位置刻意：排在下面那批校验之前、与搬迁前逐字同一处顺序；本行只声明顺序（既带键又缺必填列的载荷在上游就被 `OrderSerializer` 字段校验挡住，走不到这里）。
    intake_sales_user_id = assign_input.validated_data.get("sales_user")
    if intake_sales_user_id is not None and not has_node_now(
        actor.user, "order.allow_change_sales_agent"
    ):
        raise PermissionDenied("你没有指定销售客服的权限")
    # 客服录单可省略分类（D-0317）；只补缺省，保留显式填写的订单分类。
    if data.get("project") is not None and data.get("category") is None:
        data["category"] = data["project"].category
    try:
        validate_required(game=data.get("game"), project_text=data.get("project_text"))
        validate_order_chain(
            game=data.get("game"),
            server=data.get("server"),
            category=data.get("category"),
            project=data.get("project"),
        )
        # 写侧准入：只能录自己有权限的游戏（业务方裁定）。⚠️ **顺序刻意：参数完整性 / 引用一致性在前、权限在后**——① 有权限的请求（绝大多数）错误文案与行为逐字不变；② 链校验文案更具体、比粗粒度的「你没有该游戏的权限」更可操作；③ 两条都是业务码 1000，顺序只影响 msg。反面（如实）：先判权限可少暴露一点引用关系，但游戏 / 区服配置面对所有人是全量的（防死锁的刻意决定），不构成换序理由。⚠️ 对外路径不做这一步（§13.10：一个应用凭据 = 本租户全部业务数据）；⛔ 别把 `is not None` 改成真值判断或「顺手补一道」。
        if actor.assert_game is not None:
            actor.assert_game(data.get("game"))
    except ValidationProblem as problem:
        return IntakeOutcome(
            response=api_response(data=None, msg=str(problem), code=problem.code), order=None
        )

    # ── `throw_to_hall` 的两道闸（业务方裁定），**都在建单之前**（「载荷自相矛盾 / 价不合法」不该留下任何一行）。⚠️ 位置刻意：在「指派目标解析」之前——两键同时给时先回「只能二选一」（载荷形状错），否则同一份自相矛盾的载荷会因为里面那个人恰好不存在而给出另一句话，文案就不唯一了。
    # ⚠️ 判据是「`assignee` **非空**」不是真值：`0` / 负数过不了 `min_value=1`（DRF 先拒），这里问的是「这份载荷到底有没有指定一个人」。
    if throw_to_hall and assignee_id is not None:
        return IntakeOutcome(
            response=api_response(
                data=None,
                msg="内部抢单与指派接手人只能二选一",
                code=ErrorCode.VALIDATION,
            ),
            order=None,
        )
    # ⚠️ 价闸判据 = `hall_price` **解析后的数值** `<= 0`（E-0021 族）：`validated_data` 里它已是 `Decimal`（`null=True` ⇒ 缺省 / 显式 null 都是 `None`）⇒ 缺省档归到 `<= 0`。⛔ 不许改真值判断——`'0.00'` 是真值却是非法价，会让它静默通过并建出一张 0 元抛单（正是这道闸要拦的）；空串 / 非数字由 DRF 先拒 ⇒ 本闸只处理「解析出来了、数值不合法」这一档。
    if throw_to_hall:
        hall_price = data.get("hall_price")
        if hall_price is None or hall_price <= 0:
            return IntakeOutcome(
                response=api_response(
                    data=None,
                    msg="内部抢单的发单价必须大于 0",
                    code=ErrorCode.VALIDATION,
                ),
                order=None,
            )

    # ⚠️ **指派目标的校验在建单之前**（「载荷指向了不可用的目标」不该留下任何一行；与 `/assign/` 共用 `resolve_assign_taker`，判据只有一处实现）。`price` / `fee` 都**不传**（docstring ①）：价格已由本请求的 `hall_price` 落在订单上，再传一次是同一列的第二次写；也因此这里不额外要求 `order.submit_payout_price`（改价那扇门由 `hall_price` 字段自己的准入负责，与 `/assign/` 的 `price` 键无关）。
    taker = None
    if assignee_id is not None:
        taker, error = resolve_assign_taker(assignee_id)
        if error is not None:
            return IntakeOutcome(response=error, order=None)
    if remember_assignee == "Y" and assignee_id is None:
        return IntakeOutcome(
            response=api_response(
                data=None,
                msg="勾选「默认指派此人」时必须同时选择接手人",
                code=ErrorCode.VALIDATION,
            ),
            order=None,
        )
    # ⭐ **（业务方裁定 R1/R2）**：`"hall"` 档要求本单**真的**在抛内部抢单——「记着默认抛单」而这一单没抛是自相矛盾的载荷（页面不会这么发，但接口要对直接打接口的人也说真话）。与 `"Y"` 那道闸并列、同样建单之前 ⇒ 一行不写。⚠️ 对外录单走不到这一档（`remember_assignee` 是内部用户偏好、不在对外白名单里，由缺省 `None` 带过）；闸本身在这一份实现里，谁调都同一份。
    if remember_assignee == "hall" and not throw_to_hall:
        return IntakeOutcome(
            response=api_response(
                data=None,
                msg="勾选「默认抛内部抢单」时必须选择内部抢单",
                code=ErrorCode.VALIDATION,
            ),
            order=None,
        )

    # ⭐ 显式指定了销售客服归属 ⇒ 在**建单之前**把目标解析出来（候选范围那道闸与专用端点**同一份判据**，见 `resolve_sales_agent_candidate`；不持节点那一档已在更上面 `raise PermissionDenied` ⇒ HTTP 403）。⚠️ `order=None`：单还没建 ⇒ 「目标已是本单接单人」天然不成立，只有候选范围那一条在做功；与上面那几道闸同一条纪律：建单之前判完 ⇒ 被拒一行不写。
    intake_sales_user = None
    intake_sales_agent = ""
    if intake_sales_user_id is not None:
        intake_sales_user, intake_sales_agent, error = resolve_sales_agent_candidate(
            user_id=intake_sales_user_id, viewer=actor.user
        )
        if error is not None:
            return IntakeOutcome(
                response=api_response(data=None, msg=error.msg, code=error.code),
                order=None,
            )
        # ⚠️ **候选那一侧回的是没裁过的显示名**（`first_name` 上限 150，而这一列是 `varchar(64)`）⇒ 在这里裁一次
        #    再进 `sales_agent=`，否则一个长昵称的用户会把这笔录单以 `DataError: value too long` 响亮 500
        #    （与 `order_writes.clip_sales_agent` 同一口径、同一 helper）。
        intake_sales_agent = clip_sales_agent(intake_sales_agent)

    # ⭐ **F4.2**：录单这一路**也不收**「客户端自己填的销售客服名字」。`Order.sales_agent` 是归属人的**派生快照** ⇒ 只有服务端按**将要写入的那个归属人**算出的名字才算数；请求体带**别的**名字 ⇒ 明确拒绝（1000），⛔ 不许静默忽略（那会让调用方以为名字被采纳了）。判据 = 两个名字**逐字相等**（两边都先过同一个裁剪 helper：库里存的是裁过的值，拿原文比会造出反向的假拒绝）。**空串与「没带键」同档**（既有口径：空串 = 这一列没有值 ⇒ 兜底录单人显示名，`test_order_api.py::test_create_fills_blank_sales_agent_…` 钉着；空串不会造出分叉所以不拒）；位置与上面那几道闸同一条纪律：建单之前判完 ⇒ 被拒一行不写。「服务端将写入的那个名字」两个分支各只有一个取值口（指定归属 ⇒ `intake_sales_agent`；缺键 ⇒ `actor.sales_agent_fallback`）——与下面写入那一行**读同一个表达式**，⛔ 别在两处各算一遍。
    server_sales_agent = (
        intake_sales_agent if intake_sales_user is not None else actor.sales_agent_fallback
    )
    submitted_sales_agent = data.get("sales_agent")
    if submitted_sales_agent is not None and submitted_sales_agent != "":
        if clip_sales_agent(submitted_sales_agent) != server_sales_agent:
            return IntakeOutcome(
                response=api_response(
                    data=None,
                    msg=(
                        "接单销售客服由服务端按归属人写入，不能用请求体指定；"
                        "要指定归属人请用 sales_user（需换销售客服权限）"
                    ),
                    code=ErrorCode.VALIDATION,
                ),
                order=None,
            )

    # 普通录单仍按现有开关检查原单的有效来源号。分配子单号单独保留回收站占用。
    guard_duplicate_source = actor.guard_duplicate_source_order_no and switch_enabled(
        "forbid_duplicate_source_order_no"
    )
    source_no = data.get("source_order_no")
    if guard_duplicate_source and (rejection := _duplicate_source_rejection(source_no)):
        return rejection

    # spec §7.4：订单与绩效必须同生共死，否则会出现「没有绩效的孤儿订单」——
    # 而孤儿订单的抢单会表现为 rowcount == 0，与「已被别人接走」完全无法区分。
    with transaction.atomic():
        # ⭐ W1（客户线）：客户 upsert 与建单同生共死——绩效失败 set_rollback 时连客户行
        #    一起回滚、不留「无单的客户」（判据 tests.test_customer_members 的回滚用例）。
        customer = (
            upsert_customer(eid=buyer_eid, buyer_nick=buyer_nick)[0] if buyer_eid else None
        )
        # ⭐ 新老客标签（录单时快照）：手动覆盖优先；否则同来源 + 同客户有未删除历史单
        #    ⇒ 老客，否则新客。缺客户 / 缺来源 ⇒ 空串（无买家 ID 的手工单不显示标签）。
        #    拆单组各成员共用这一个值（同一次购买），所以整组在循环外算一次。
        #    手动分支也要拿同一把 Customer 行锁：否则并发「手动单 + 自动单」时，
        #    自动单可能看不到尚未提交的手动单，两笔都判新客。
        if customer is not None:
            Customer.objects.select_for_update().get(pk=customer.pk)
        kind_override = assign_input.validated_data.get("customer_kind_override") or ""
        if kind_override:
            customer_kind = kind_override
            customer_kind_is_manual = True
        else:
            order_source = data.get("order_source")
            customer_kind = _auto_customer_kind(
                customer=customer,
                order_source_id=getattr(order_source, "pk", None),
                customer_phone=data.get("customer_phone") or "",
            )
            customer_kind_is_manual = False
        if split_kind:
            split_group.lock_child_source_numbers()
            if guard_duplicate_source and (rejection := _duplicate_source_rejection(source_no)):
                transaction.set_rollback(True)
                return rejection
        orders = []
        child_source_nos = None
        for fields in members:
            if child_source_nos is not None:
                fields["source_order_no"] = next(child_source_nos)
            # ⚠️ **每一轮都必须把 `serializer.instance` 清回 `None`**（拆单这条路的承重语句）：DRF 的 `save()` 在 `self.instance is not None` 时走 **update** ⇒ 不清的话第 2 轮会**改写第 1 笔**，第 2 笔的绩效行再建一次会撞部分唯一索引 `uniq_order_main_taker`（实测 `IntegrityError`）。循环结束后它停在最后一笔上——本函数**不再读它**（成功载荷由 `payload_for_order(order)` 装配）。
            serializer.instance = None
            order_no = generate_order_no()
            order = serializer.save(
                order_no=order_no,
                split_id=split_group_id,
                split_kind=split_kind,
                split_ship_main_order_no=(orders[0].order_no if orders else order_no) if split_group_id else None,
                **fields,
                # ⭐ W1（客户线）：`customer` 列在两个写序列化器里只读（SERVER_WRITTEN_ORDER_FIELDS），
                #    只能由这个 kwarg 写（W2 回填走命令自己的 UPDATE）。
                customer=customer,
                # ⭐ 新老客标签（整组同值；只由本核心写，两个写序列化器里只读）。
                customer_kind=customer_kind,
                customer_kind_is_manual=customer_kind_is_manual,
                # D6/spec §5.1：`order_source` 非空时由**后端**把字典项名字回填进 `source`（前端只传 id），
                # 规则与 PATCH 同源（同一个纯函数）；`current_order_source_id` 走默认 `None`：新单 FK 必然从空变某 id
                # ⇒ 一定覆盖名字（I-1 的「同一个 FK 才不覆盖」在这里不适用）。
                **source_snapshot_overrides(data),
                # 按录单人筛选和通知都用此快照，只取服务端建立的身份。
                created_by=actor.log_actor if actor.created_by is None else actor.created_by,
                # ⚠️ **Ruling Q**：`Order.sales_user` 在此之前全仓没有写入点（只有读）⇒ 恒为 NULL ⇒ 录单永不建客服行、完成钩子也早退 ⇒ 整条客服提成路径在生产上是**死代码**；spec §3.8 既定口径「录单时由视图按 `request.user` 写入」，这里照办。归属人是**认证态事实**，不用请求体里的（客户端不能自选归属人）；未认证显式给 `None`（让 `AnonymousUser` 不进 FK；对外路径恒 `None`、与未认证同一支，⛔ 不写不存在的用户）。持节点时载荷 `sales_user` 优先、缺键回落认证态（静默忽略会让名字和提成归属分叉还显示保存成功）。⛔ 通用 PATCH 仍不收 `sales_user`（两个序列化器的 `read_only_fields` 都有它）——「钱」只走专用端点与这条录单路。
                sales_user=(
                    intake_sales_user
                    if intake_sales_user is not None
                    else (
                        actor.user
                        if (actor.user is not None and actor.user.is_authenticated)
                        else None
                    )
                ),
                # ⚠️ **接单销售客服（`sales_agent`）的兜底**（业务方裁定）：对标那个控件是运行期由 `getCustomerService()`（`addorder.js` L1154-1166，候选 = `groupType=20` 的客服 + 当前登录人，读码推断未实测）灌的 radio，本仓录单页没实现 ⇒ 请求体没有这个键 ⇒ 「销售客服」列在录单这一路恒为空串（线上实测 `sales_agent=''`）。边界别写大：上下游同步那两条路本来就会写它、不受本兜底影响；对标候选带 `isSelectCurrentUser=Y`（「默认勾中他」是读码推断）⇒ 按**录单人**兜底（对外兜底空串，那条路没有人）。
                #    客户端显式给了值就用客户端的（该列语义仍是「可手填的展示快照」）；指定 `sales_user` 时名字用服务端算出的显示名、不让请求体盖掉（否则名字与提成归属分叉）；缺 `sales_user` 时先看请求体再回落录单人——别删 `or` 的右半（空串和缺键都要落到录单人显示名）。
                sales_agent=(
                    intake_sales_agent
                    if intake_sales_user is not None
                    else (data.get("sales_agent") or actor.sales_agent_fallback)
                ),
            )
            # ⚠️ **P4-6**：本调用现在还会建**客服行**。Ruling P 之后录单不读提成率（`temp_amount` 无条件 0.00）⇒ 这个 except 的 `CommissionError` / `AmountError` **当前已无可触发路径**；保留是刻意的最小改动（P4-7 若让录单重新依赖薪酬配置，这里是唯一的翻译点）；真正未预期的错误仍按 E-0010 响亮 500。
            # ⚠️ `record(...)` 必须在 `try` **之后**：绩效没建成就 `set_rollback` + 返回业务码，此时**不能**留下「订单已录、日志说已录」的行（E-0063 那类「动作没做成却写了成功日志」的方向）。
            try:
                create_performance_for_order(order)
            except (CommissionError, _AmountError) as exc:
                transaction.set_rollback(True)
                return IntakeOutcome(
                    response=api_response(data=None, msg=exc.message, code=exc.code),
                    # ⚠️ **订单在这一档不存在**（整个建单事务被回滚）⇒ 调用方据此判"没写成"。
                    order=None,
                )
            record(
                log_type=ACTION_LOG_TYPES["order.create"],
                action="order.create",
                # 明确区分操作者和录单归属。机器建单留系统行，不借用订单归属人的身份。
                system=actor.log_actor_id is None,
                actor=actor.log_actor,
                actor_id=actor.log_actor_id,
                target_kind="order",
                target_id=order.order_no,
                order_no=order.order_no,
                source_order_no=order.source_order_no,
                to_value=order.hall_price,
            )
            if split_kind and not orders:
                child_source_nos = split_group.child_source_numbers(order.source_order_no, order.order_no)
            orders.append(order)
        # 显示层主单 = 组内**第一笔**（它也是组内 `id` 最小者，因为它最先落库）。
        # 「指派 / 抛内部抢单」那两段后续动作**只对它**做——⛔ 不给其余各笔也做一遍：
        # 那是「一次录单最多一个后续动作」的既有语义，拆单只改变**落了几笔单**。
        order = orders[0]
    # ── 建单事务**已提交**后，再开**第二个**事务把这张单抛进内部抢单大厅 ────────
    # 建单和发厅使用两个事务。发厅失败时保留已经建好的订单。
    # ⚠️ 只接 `IllegalTransition`（可预期域层拒绝）⇒ `1023` + 已建单号 + 原因，订单**不回滚**（与 `1022` 同形状）；⛔ 其它异常不许翻译成这个码：未预期的照旧响亮 500（E-0010）。
    if throw_to_hall:
        # 抛单失败的原因（`None` = 没失败）。⚠️ 它存在的理由**只有一个**：`"N"` 的清除
        # 必须发生在**这个事务之外**（见下面 `if throw_reason is not None` 分支的注释）。
        # ⛔ 别拿它当别的状态用：「抛单成没成」这件事由 `order.status` 说了算。
        throw_reason: str | None = None
        with transaction.atomic():
            try:
                apply_transition(
                    order, OrderStatus.WAITING_GRAB,
                    nodes=actor.nodes,
                    actor=actor.log_actor,
                    # 池内目标态 ⇒ 回池钩子会跑，它写的冲销留痕是**人为动作**、必须带主键。⚠️ 对外路径这一格是 `None`：`record()` 对 `actor_id=None` 要求显式 `system=`，而这里**不传** ⇒ 该行的操作人取 `actor.nodes` 那条链上的 `actor` / `actor_id` 原样（见 `order_status.apply_transition`）。
                    actor_id=actor.log_actor_id,
                    # 发单人 = 抛单的人（认证态事实，spec §6）：**走同一个漏斗**（写入由 `apply_transition` 的 `update_fields` 负责，本函数不再自己 save）。⚠️ 对外路径恒 `None` ⇒ 那一列保持原值（T5 已登记的同一代价：开放平台路径抛进大厅时不写发单人，「谁干的」由留痕行回答）。
                    hall_publisher=actor.user,
                )
            except IllegalTransition as exc:
                # ⚠️ 照旧先显式标记回滚（E-0022）：`atomic()` 是事务不是回滚点——块内正常走到块尾就是**提交**。本路径今天在抛出点之前一行都没写 ⇒ 这个标记是**防御性**的（保证将来在这里加写入不会静默变「一半成功」）。⚠️ 环境依赖（实测）：今天 `ATOMIC_REQUESTS` 0 命中 ⇒ 本句只覆盖这个事务；若将来打开它，会把请求级事务整体回滚、连建单事务一起卷走而 msg 仍宣称「单已建好」⇒ 那时必须重判。
                # ⭐ **T6——幂等层那一侧的同一条语义**：开放平台录单外面套着 `idempotency._run` 的**外层事务**（本函数的两层 `atomic()` 在那里面只是 savepoint）。实测 `Atomic.__exit__`：带着 `needs_rollback` 退出的带 savepoint 的块会 `savepoint_rollback` 并**清掉**标记 ⇒ 本句不污染外层；外层整体回不回滚由 `_run` 自己判——**建单成了就绝不能回滚**（见 `idempotency.py::mark_side_effect`）。
                transaction.set_rollback(True)
                # ⚠️ **块内不 `return`**：`"N"` 的清除必须写在块**外**
                #    （理由见下面那个分支）——块内 `return` 就没法在块外补那一笔。
                throw_reason = str(exc)
            if throw_reason is None:
                record(
                    log_type=ACTION_LOG_TYPES["order.throw_to_hall"],
                    action="order.throw_to_hall",
                    # 留痕的操作人取**录单人那一侧**（与上面 `order.create` 同一条纪律；R20）。
                    # ⚠️ `system=` 的理由与上面那一处**逐字相同**（内部恒 `False`、对外恒 `True`）。
                    system=actor.log_actor_id is None,
                    actor=actor.log_actor,
                    actor_id=actor.log_actor_id,
                    target_kind="order",
                    target_id=order.order_no,
                    order_no=order.order_no,
                    # 发单价 = `Order.hall_price`（建单那一笔已经写下；本路径不再改它）
                    to_value=order.hall_price,
                )
                # 留痕和入队都在抛单成功之后。抛单没成走不到这里。
                # 只入队下家线 `jidanpu_member.queue_hall_launch`。
                jidanpu_member.queue_hall_launch(order)
        if throw_reason is not None:
            # ⭐ **`"N"` 在这条失败路径上也照清**（业务方裁定）：「清默认」是操作人对自己偏好的要求、与这一单成不成无关（与 `1022` 路径同一语义，缺口已补齐加用例钉住）。⚠️ **位置承重：必须在 `with` 块之外**（块已退出、`set_rollback` 已生效）；写在块内会被那个回滚一起卷走，「清了又没了」且**没有任何异常**（E-0022 同族）。⚠️ 这笔写库若抛异常，用户拿到 500 而非 `1023`——与既有 `1022` 路径同一形状，⛔ 不是新引入的失败类别；裁决 = 照旧响亮 500（E-0010），代价登记在新 spec §5 第 10 条。
            #    ⚠️ 对外路径走不到这一档（`remember_assignee` 不在白名单）⇒ 那三句 `set_default_assign_preference` 都不会被调到；⛔ 不写「跳过」的防御分支（那会把未来的契约错误静默藏起来）。
            if remember_assignee == "N":
                set_default_assign_preference(actor.user, mode="none")
            return IntakeOutcome(
                response=api_response(
                    # ⚠️ `data.order_no` 是消费方定位那张单的**寻址值**——照旧内置号（D-0301）；
                    #    只有**人读的 msg** 换展示口径（来源号非空取它、空回退内置号）。
                    data={"order_no": order.order_no, "reason": throw_reason},
                    msg=(
                        f"单已建好（单号 {order.source_order_no or order.order_no}），"
                        f"抛入内部抢单大厅没成功：{throw_reason}"
                    ),
                    code=ErrorCode.ORDER_CREATED_THROW_FAILED,
                ),
                order=order,
            )
    # ── 「建单 + 指派」串成一次（D-0179）────────────────────────────────────────
    # ⚠️ **在 `atomic()` 块之外**（= 建单事务已提交）：指派无论怎样失败都**带不走订单**——块内调它的话一个未预期异常会连已建好的单一起回滚，业务方要求相反（「中间态给个提示就行」）。可预期失败由 `assign_order` 以 `WriteOutcome` 返回（不抛），这里只翻译成业务码。
    if taker is not None:
        outcome = assign_order(
            order,
            taker=taker,
            # `price` / `fee` 都不传：价格就是本请求已经写下的 `hall_price`（docstring 第 1 条）。
            ctx=actor.write_ctx,
            dry_run=False,
        )
        if not outcome.ok:
            # 操作人明确要求「取消默认」时照清（那是他对自己偏好的要求，与这一单成不成无关）。
            if remember_assignee == "N":
                set_default_assign_preference(actor.user, mode="none")
            reason = outcome.msg
            return IntakeOutcome(
                response=api_response(
                    data={
                        "order_no": order.order_no,
                        "reason": reason,
                        "assign_code": int(outcome.code),
                    },
                    # ⚠️ 同上：`data.order_no` 寻址值照旧内置号；msg 是人读面，用展示口径（D-0301）。
                    msg=(
                        f"单已建好（单号 {order.source_order_no or order.order_no}），"
                        f"指派没成功：{reason}"
                    ),
                    code=ErrorCode.ORDER_CREATED_ASSIGN_FAILED,
                ),
                order=order,
            )
        order = outcome.detail["order"]
    # 「默认指派此人」：`"Y"` 只在**指派成功之后**落库；`"hall"` 只在**抛单成功之后**落库；
    # `"N"` 是无条件清除（见 docstring 第 3 条）。⚠️ 对外路径走不到这里（见上）。
    if remember_assignee == "Y":
        set_default_assign_preference(actor.user, mode="person", taker=taker)
    elif remember_assignee == "hall":
        set_default_assign_preference(actor.user, mode="hall")
    elif remember_assignee == "N":
        set_default_assign_preference(actor.user, mode="none")
    return IntakeOutcome(
        response=api_response(data=payload_for_order(order)), order=order
    )
