"""订单写动作的领域核心：判据的唯一出处。同一件事（「这一笔能不能改价」「改完要怎么留痕」）单笔端点与批量端点各写一份必然漂移、而且只用得对一套（E-0021 的形状）——实测过的后果是「单笔拦得住、批量绕得过」；所以单笔与批量都调这里、靠 dry_run 形参区分「只校验、不写」与「真写」。
契约：dry_run=True ⇒ 跑完全部判据后在写库之前返回 ⇒ 零写入、零副作用（spec §4.3 五条：业务列不变、日志不写、新行不建、钩子不跑、applied 为空）；dry_run=False ⇒ 与搬迁前的单笔动作体判据与副作用逐条等价（update_order_fields 的落库由 serializer.save 改为等价的 setattr + save；写入与留痕同一事务）。
⛔ 请求级校验不进本模块（序列化器、节点门、_actor() 格式、AssignSerializer 的「目标用户不存在/已停用」）：它们对所有单只做一次、留在视图里；⛔ 本模块不 import DRF（不接 request、不返回 Response）：调用方把需要的值装进 WriteContext。"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import DecimalValidator
from django.db.models import Q

from django.db import transaction
from django.contrib.auth.models import User
from django.utils import timezone

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.core.error_codes import ErrorCode
from apps.accounts.display import user_display_name
from apps.accounts.models import StaffProfile
from apps.business import jidanpu_assignment_lock, jidanpu_member
from apps.business import split_group
from apps.business.commission import CommissionError, commission_percent_of
from apps.business.game_scope import assert_game_allowed, assert_game_id_allowed, games_of
from apps.business.models import EmpType, Order, OrderStatus, PaymentStatus, Performance
from apps.business.order_source import source_snapshot_overrides
from apps.business.penalty_amounts import (
    PENALTY_AMOUNT_LOCKED, approved_penalty_price, changes_approved_penalty_price,
)
from apps.business.order_status import (
    CONFLICT_PRONE_FIELDS,
    IllegalTransition,
    apply_transition,
    settled_pool_entry_denial,
    settled_status_change_denial,
    stamp_local_action,
    taker_present_pool_entry_denial,
    transition_denial,
)
from apps.business.peer_dispatch import (
    enqueue_order_assigned,
    enqueue_order_withdrawn,
    notify_order_content_changed,
    notify_order_price_changed,
)
from apps.business.peer_sync import HALL_TAKE_LEAVE_ASSIGNED, PEER_DIRECT_PENDING_MESSAGE
from apps.business.performance import (
    create_customer_performance,
    ensure_main_performance_of,
    recompute_order_amounts,
    reset_taker_performance,
    set_taker,
    write_performance_amounts,
)
from apps.business.performance_amount import AmountError as _AmountError
from apps.business.performance_amount import commission_amount
from apps.business.recycle import soft_delete_order, validate_soft_delete
from apps.business.refunds import (
    RefundError,
    _write_refund_total,
    refund_total_denial,
)
from apps.business.settlement_hooks import (
    recompute_customer_commission,
    refresh_customer_estimate,
)
# ⭐（业务方拍板「换销售客服 = 提成也换人」）：写侧要判「这个人在不在候选里」、而候选口径的唯一出处是 selectors.kefu_candidates_qs（kefu-options 那个端点灌 radio 用的也是它）⇒ 在这里 import 它 ⛔ 不另写一套过滤条件。
# ⚠️ selectors 不 import order_writes（它只 import models / scoping / accounts 那几层）⇒ 这条 import 不构成环；真要变成环、manage.py check 会当场炸。
from apps.business.selectors import kefu_candidates_qs, display_name_expression
from apps.business.validation import (
    ValidationProblem,
    normalize_project_text,
    validate_order_chain,
    validate_required,
)
# ⭐ D-0245（「未实名不允许接单」）：指派闸的判定单一出处在 apps/realname/services.py::claim_gate_blocks（与接单侧 claim.py 同源 ⛔ 不抄第二份）；
# ⚠️ 无环：realname.services 只 import accounts.models / business.roles（纯常量）、不 import 本模块；真要成环、manage.py check 会当场炸。
from apps.realname.services import claim_gate_blocks


def _field_text(value):
    """字段值 → 日志文本：None ⇒ 空串，其余 str()（原 order_views._field_text，逐字搬）。
    ⚠️ 判据是 is None 而不是真值（E-0021）：Decimal("0") / False 都是合法值、真值判断会把「传了 0」静默丢掉。"""
    return "" if value is None else str(value)


def clip_sales_agent(value) -> str:
    """把「销售客服的显示名」裁进 Order.sales_agent 那一列的宽度（⛔ 列宽从模型元数据取、不抄数字）。为什么必须有：sales_agent 是 CharField(max_length=64)、装的是显示名（User.first_name 上限 150）⇒ 超宽时 PostgreSQL 抛 value too long、整笔写单响亮 500 并回滚；同口径先例三处（peer_inbound._clip_to_column / jidanpu_upstream._clip_for_order / audit/log._clip），⛔ 本函数是第 4 处——不去复用现成那个：它属上游同步线、从录单主路径调它会把两条线反向耦合（两行纯函数的重复比这个耦合便宜；真正的清理 = 把四处提升成公共工具并同改 4 个调用点，是一次独立任务）。
    📌 本函数从 api/order_views.py::_clip_sales_agent 逐字搬到这里（改名不加下划线 ⇒ 跨模块可 import）：写侧（change_sales_agent）也要写这一列、让领域核 import 视图层私有函数会把依赖方向反过来；order_views 那侧仍留同名别名给既有调用点与注释引用。None ⇒ 空串（该列 blank=True、非空）。"""
    text = "" if value is None else str(value)
    width = Order._meta.get_field("sales_agent").max_length
    return text if width is None else text[:width]


#: 状态码 → 中文标签。spec §5.1 的 `order.transition` 文案要求前后值是**中文**，
#: 而 §9.1 的旧数据迁移按同一份 `OrderStatus.choices` 翻译（两处同源，不新造表）。
#: 认不出的码**原样保留**、不抛（迁移那条口径的运行时孪生）。
_STATUS_LABELS = dict(OrderStatus.choices)


def _status_text(value):
    return _STATUS_LABELS.get(value, value)


@dataclass(frozen=True)
class WriteContext:
    actor: str = ""
    log_actor: str = ""
    log_actor_id: int | None = None
    nodes: frozenset[str] = frozenset()
    #: 认证态的 `User` 实例。两个动作读它：`transition_order` 把它当 `apply_transition`
    #: 的 `hall_publisher` 漏斗参数（「发单人 = 把订单抛到大厅的人」）；`assign_order`
    #: 在自己的直写点读它（⭐ D-0369：**指派（含改派）也算发单动作**，发单人 = 操作人）。机器 / 开放平台路径造的 ctx `user=None`（`is not None` 判据，E-0021）⇒ 那两处天然不写。⚠️ 带默认值 ⇒ Task 1/2 的既有构造点与用例不受影响（不要去改它们）。
    user: object | None = None
    notify_worker: bool = True
    recheck_external_refunds: bool = True
    allow_financial_writes: bool = True
    sync_personal_orders: bool = True
    sync_peer_orders: bool = True
    assert_game: object | None = None


@dataclass(frozen=True)
class WriteOutcome:
    ok: bool
    code: int = int(ErrorCode.OK)
    msg: str = ""
    detail: dict = field(default_factory=dict)


def _ok(**detail) -> WriteOutcome:
    return WriteOutcome(ok=True, detail=detail)


def _fail(code, msg, **detail) -> WriteOutcome:
    return WriteOutcome(ok=False, code=int(code), msg=msg, detail=detail)


def lock_order_row(order, *, nowait=False):
    """按统一锁序先锁这一张订单的行（SELECT … FOR UPDATE OF self ORDER BY id）。
    ⭐ I4：/assign/ 那条边的顺序是「先锁 Order、再锁 Performance」（apply_transition 的 select_for_update 在前、绩效行锁在后）；/hall-price/、/performance/delete/、/sales-agent/、/adjust-amount/ 这四处原来都是先锁绩效行、随后才拿到订单行锁 ⇒ 与 /assign/ 恰好相反：两笔请求各持一把锁、又都去要另一把时成环（AB-BA 死锁，表现为其中一方被数据库挑出来杀掉 / 500）——本函数让那四处也变成同一个方向。
    锁的形状只此一处（⛔ 别在各视图里手写第二份）：of=("self",) 只锁订单行自己（⛔ 不把 join 上的行一起锁住）；.order_by("id") 单行也用同一条形状、批量扩写时锁序不漂；调用方必须在 transaction.atomic() 里调（select_for_update 在 autocommit 下抛 TransactionManagementError）。回加锁后读回的那一行（最新的价/归属/状态），取不到（并发真删，正常路径不会：调用方已由 get_object() 挡过「不存在」）⇒ None、调用方按既有逻辑处理。"""
    return (
        Order.objects.select_for_update(nowait=nowait, of=("self",))
        .order_by("id")
        .filter(pk=order.pk)
        .first()
    )


def queue_externalpay_recheck(order):
    from apps.wecom.external_pay import enqueue_order_refund_recheck

    enqueue_order_refund_recheck(order)


def lock_order_and_sales_nowait(order):
    """企微同步只尝试锁，冲突交给事务外重试。"""
    if order.split_id:
        members = list(Order.objects.filter(split_id=order.split_id).order_by('id')
                       .select_for_update(nowait=True, of=('self',)))
        locked = next((member for member in members if member.pk == order.pk), None)
    else:
        locked = Order.objects.filter(pk=order.pk).select_for_update(nowait=True, of=('self',)).first()
        members = [locked] if locked is not None else []
    if locked is None or locked.split_id != order.split_id:
        raise RuntimeError('订单拆分组已变化，请稍后重试')
    list(Performance.objects.filter(order_id__in=[member.pk for member in members], emp_type=EmpType.SALES)
         .order_by('id').select_for_update(nowait=True, of=('self',)))
    return locked


def _order_payment_in_progress(order_ids):
    return Performance.objects.filter(
        order_id__in=order_ids, online_payroll_batch__isnull=False, settled_at__isnull=True,
    ).exists()


def change_price(order, *, total_price, reason="", ctx, dry_run, assert_game=None) -> WriteOutcome:
    """改价并留痕；拆分组非锚点改价时由最早活跃成员反向调平。dry-run 与真写共用金额范围、权限和游戏范围判据。"""
    if assert_game is None:
        assert_game = lambda game: assert_game_allowed(ctx.user, game)
    if split_group.is_split_member(order):
        if order.split_kind not in ("", split_group.LEGACY, split_group.PEER):
            return _fail(ErrorCode.BUSINESS, "订单拆分类型不支持改接单价")
        if split_group.is_balance_anchor(order):
            return _fail(ErrorCode.BUSINESS, "拆分组原单接单价由系统调平，不能直接修改")
        old_price = order.total_price
        new_price = total_price
        if dry_run:
            anchor = split_group.balance_anchor_member(order.split_id)
            if anchor is None or anchor.pk == order.pk:
                return _fail(ErrorCode.BUSINESS, "拆分组成员已变化，请刷新后重试")
            try:
                assert_game(anchor.game)
            except ValidationProblem as problem:
                return _fail(problem.code, str(problem))
            anchor_price = anchor.total_price - (new_price - old_price)
            try:
                DecimalValidator(max_digits=12, decimal_places=2)(anchor_price)
            except ValidationError:
                return _fail(ErrorCode.VALIDATION, "调平后的原单接单价超出有效金额范围")
            return _ok(order=order, old=old_price, new=new_price)
        with transaction.atomic():
            members = list(
                Order.objects.select_for_update()
                .filter(split_id=order.split_id)
                .order_by("id")
            )
            locked_by_pk = {member.pk: member for member in members}
            member = locked_by_pk.get(order.pk)
            anchor = next(
                (row for row in members if row.split_kind in ("", split_group.LEGACY, split_group.PEER)),
                None,
            )
            if member is None or anchor is None or member.pk == anchor.pk:
                return _fail(ErrorCode.BUSINESS, "拆分组成员已变化，请刷新后重试")
            try:
                assert_game(anchor.game)
            except ValidationProblem as problem:
                return _fail(problem.code, str(problem))
            if _order_payment_in_progress([member.pk, anchor.pk]):
                return _fail(ErrorCode.BUSINESS, "订单有正在处理的在线付款，暂时不能改价")
            old_price = member.total_price
            new_price = total_price
            anchor_price = anchor.total_price - (new_price - old_price)
            try:
                DecimalValidator(max_digits=12, decimal_places=2)(anchor_price)
            except ValidationError:
                return _fail(ErrorCode.VALIDATION, "调平后的原单接单价超出有效金额范围")
            member.total_price = new_price
            member.updated_by = ctx.actor
            member.save(update_fields=["total_price", "updated_by", "updated_at"])
            anchor_old_price = anchor.total_price
            anchor.total_price = anchor_price
            anchor.updated_by = ctx.actor
            anchor.save(update_fields=["total_price", "updated_by", "updated_at"])
            if new_price != old_price:
                if ctx.recheck_external_refunds:
                    queue_externalpay_recheck(member)
                    queue_externalpay_recheck(anchor)
                _record_taker_price_change(member, old_price, new_price, ctx, reason=reason)
                _record_taker_price_change(
                    anchor, anchor_old_price, anchor_price, ctx, reason="拆分组接单价自动调平"
                )
                if member.split_kind in ("", split_group.LEGACY):
                    refresh_customer_estimate(anchor)
                else:
                    refresh_customer_estimate(member)
                    refresh_customer_estimate(anchor)
        return _ok(order=member, old=old_price, new=new_price)
    old_price = order.total_price
    new_price = total_price
    if dry_run:
        return _ok(order=order, old=old_price, new=new_price)
    with transaction.atomic():
        if lock_order_row(order) is None:
            return _fail(ErrorCode.BUSINESS, "订单已变化，请刷新后重试")
        if _order_payment_in_progress([order.pk]):
            return _fail(ErrorCode.BUSINESS, "订单有正在处理的在线付款，暂时不能改价")
        order.total_price = new_price
        order.updated_by = ctx.actor
        order.save(update_fields=["total_price", "updated_by", "updated_at"])
        # ⚠️ R27.2 同口径（第四处）：新价 == 旧价时不留痕——否则会写下「原订单价格：88.00元，修改后价格：88.00元」一句假话
        #（本线在 performance.assign、账号启停、RBAC R26、配置 R27.2 都已修掉这个形状、这里是 /price/ 那一处漏网的）。
        # 只跳过留痕、仍执行 save：与配置域（_changes_anything 只 gate record()）和账号域（幂等 set 仍落库、只跳过 _record_account）同一口径——本线的口径是「no-op 请求不写日志行」、不是「no-op 请求不做任何写入」；代价（如实登记）：同价请求仍会推 updated_by/updated_at（即「谁最后确认过这个价」也会留痕）、可辩护、且避免为一个假行去改业务写入语义。
        if new_price != old_price:
            if ctx.recheck_external_refunds:
                queue_externalpay_recheck(order)
            _record_taker_price_change(order, old_price, new_price, ctx, reason=reason)
        # ⭐（当场 grep 复核过）：total_price 也是净实收的输入（settlement_amount.net_received = total_price − refund_fee）⇒ 改了它、客服行的「预计」与「生效」就都该跟着走（口径与 /hall-price/ 那一处同源：那条路只是多了「按新发单价重算打手行」这一步）；它是本批 grep 命中里唯一单笔可达、且同时服务批量端点 /orders/batch/price/ 的落点（批量经同一函数 ⇒ 自动覆盖 ⛔ 不需要在视图层再挂一次）。
        # ⚠️ 它只动不受「已结账」保护的行：闸门在 refresh_customer_estimate 里逐行判 is_settled（本动作对已结账的单没有订单级拒改闸——那道闸只在 change_hall_price 里、只管发单价）。
        # ⚠️ refresh_customer_estimate 自己的业务判据不外抛 ⇒ 本动作的返回值/错误码逐字不变（dry_run=True 在上面就返回了 ⇒ 零副作用照旧）。
        refresh_customer_estimate(order)
    return _ok(order=order)


def _record_taker_price_change(order, old_price, new_price, ctx, *, reason=""):
    record(
        log_type=ACTION_LOG_TYPES["order.change_taker_price"],
        action="order.change_taker_price",
        actor=ctx.log_actor,
        actor_id=ctx.log_actor_id,
        system=ctx.log_actor_id is None,
        target_kind="order",
        target_id=order.order_no,
        order_no=order.order_no,
        field_name="total_price",
        from_value=old_price,
        to_value=new_price,
        reason=reason,
    )


def change_payment_status(order, *, payment_status, ctx, dry_run) -> WriteOutcome:
    """改收款状态（原 OrderViewSet.payment_status 的动作体，语句逐条未改）。纯人工标记三档、不联动退款/退单/结账（业务方裁定）；⭐ 业务方再拍板（D-0336）：收款已标「已结款」、之后该订单退款 ⇒ 标记保持不动（不自动回退）——退款链（refunds.py / order_status.py）今天也确实一个字节都不写 payment_status。
    回归钉：tests.test_order_payment_status_api 的 test_a_partial_refund_a_refund_transition_and_a_settlement_do_not_move_it（端点级，部分退款/退单/结账/撤销结账四腿）与 PaymentStatusStaysSettledWhenOrderRefundsTests（域层，apply_transition 直驱）。
    与 /price/ 同形的四件事：留痕的操作人取认证态；新旧同值不留痕但仍 save。"""
    old_status = order.payment_status
    new_status = payment_status
    if dry_run:
        return _ok(order=order, old=old_status, new=new_status)
    with transaction.atomic():
        order.payment_status = new_status
        order.updated_by = ctx.actor
        order.save(update_fields=["payment_status", "updated_by", "updated_at"])
        # 同值口径同 `/price/`（R27.2）：不写「从已结款改为已结款」的假话行，
        # 但仍推 `updated_by` / `updated_at`（「谁最后确认过这一档」也留痕）。
        if new_status != old_status:
            record(
                log_type=ACTION_LOG_TYPES["order.change_payment_status"],
                action="order.change_payment_status",
                actor=ctx.log_actor,
                actor_id=ctx.log_actor_id,
                # ⭐ `WriteContext.log_actor_id` **有默认值 `None`**
                #（同步路径 `jidanpu_converge` 造的 ctx 正是这一档）⇒ 显式声明「没有操作人」，
                #  ⛔ 不沉默（`record()` 的 fail-closed 判据会抛 `ValueError`）。
                system=ctx.log_actor_id is None,
                target_kind="order",
                target_id=order.order_no,
                order_no=order.order_no,
                field_name="payment_status",
                # spec §8 第 4 条：前后值是**中文 label**（与 `order.transition` 同口径）。
                from_value=PaymentStatus(old_status).label,
                to_value=PaymentStatus(new_status).label,
            )
    return _ok(order=order)


def change_hall_price(order, *, price, reason="", ctx, dry_run) -> WriteOutcome:
    """改发单价（原 OrderViewSet.hall_price 的动作体，语句逐条未改）。
    ⚠️ is_settled 闸必须在 dry_run 早返回之前（spec §D5）：批量侧就是靠这一句把「已结账的单」报成 ineligible；⛔ 不要照抄 change_price 的早返回位置——那个动作没有订单级闸、本动作有。
    ⚠️ 写价后的 recompute_order_amounts（含 (CommissionError, _AmountError) 整笔拒绝那一支，D-0254）只在 dry_run=False 时跑——dry_run=True 必须零副作用。"""
    # ⭐ R-6 的统一化（spec §2.1 的保护）：该单的 MAIN 绩效行已结账 ⇒ 本端点一律拒绝、业务码 1014。此前只有 /assign/ 与 /delete/ 有这道闸、/hall-price/ 没有 ⇒ 已结账的单仍能从这里改价（并连带改预计绩效）、「已结账的钱不许动」这条保护因此有一个缺口。
    # ⚠️ 判据是 Performance.is_settled（三字段并集的既有实现）⛔ 不在这里手抄那三个字段（E-0021 的形状：同一事实两套判据、只用对了一套）；⚠️ 位置在 transaction.atomic() 与任何写入之前 ⇒ 被拒时一行不写（E-0022 的反面用法）；文案说明「已结账」并给出下一步（撤销结账）。
    # ⚠️ 本闸在 dry_run 早返回之前：批量侧靠它把已结账的行报成 ineligible。
    perf = Performance.objects.filter(order=order, emp_type=EmpType.MAIN).first()
    if perf is not None and perf.is_settled:
        return _fail(
            ErrorCode.PERFORMANCE_STATE_INVALID,
            "该单的接单人绩效已结账，不能改发单价；请先撤销结账再改",
        )
    if changes_approved_penalty_price(order, price):
        return _fail(ErrorCode.BUSINESS, PENALTY_AMOUNT_LOCKED)
    old_price = order.hall_price
    if dry_run:
        return _ok(order=order, old=old_price, new=price)
    with transaction.atomic():
        # ⭐⭐ I4：**先锁订单行，再锁绩效行**（与 `/assign/` 同向）。
        #     原来这里只锁绩效行、订单行锁要等下面 `order.save()` 才拿到 ⇒ 与
        #     `/assign/`（`apply_transition` 先锁 Order）方向相反，两笔并发会 AB-BA 成环。⚠️ 锁形状的唯一出处是 `lock_order_row`（⛔ 不在视图里手写第二份）。
        locked_order = lock_order_row(order)
        if locked_order is not None and locked_order.peer_direct_pending is not None:
            return _fail(ErrorCode.BUSINESS, PEER_DIRECT_PENDING_MESSAGE)
        if locked_order is not None and changes_approved_penalty_price(locked_order, price):
            return _fail(ErrorCode.BUSINESS, PENALTY_AMOUNT_LOCKED)
        if (locked_order is not None and _needs_jidanpu_order_guard(locked_order)
                and not jidanpu_assignment_lock.lock_order_change(order.pk)):
            return _fail(ErrorCode.BUSINESS, jidanpu_assignment_lock.ORDER_BUSY)
        if _order_payment_in_progress([order.pk]):
            return _fail(ErrorCode.BUSINESS, "订单有正在处理的在线付款，暂时不能改价")
        # ⭐⭐ Z1：上面那句 perf.is_settled 是判定、下面是写入、两段之间不持锁 ⇒ 并发结账能把钱结掉、这边照改价（TOCTOU）。修法照 /adjust-amount/ 已修形状（P1-10）：事务内对绩效行 select_for_update(of=("self",)) 重取并复查 is_settled；
        # of=("self",) 只锁绩效行自己（本处过滤没有 join、仍然显式写、形状与其余四处一致）；.order_by("id") 单行也用同一条形状、以后批量扩写时锁序不漂；复查用加锁后读回来的那一份（locked_perf）⛔ 不看锁前的 perf；被拒时 transaction.set_rollback(True) 再 return（E-0022）；⚠️ 缺 MAIN 行（perf is None）⇒ 只锁到 0 行、照原逻辑走（没有「已结账」可言）。
        locked_perf = None
        if perf is not None:
            locked_perf = (
                Performance.objects.select_for_update(of=("self",))
                .order_by("id")
                .get(pk=perf.pk)
            )
        if locked_perf is not None and locked_perf.online_payroll_batch_id and not locked_perf.is_settled:
            return _fail(ErrorCode.PERFORMANCE_STATE_INVALID, "报酬已在在线付款单中，暂时不能改价")
        if locked_perf is not None and locked_perf.is_settled:
            transaction.set_rollback(True)
            return _fail(
                ErrorCode.PERFORMANCE_STATE_INVALID,
                "该单的接单人绩效已结账，不能改发单价；请先撤销结账再改",
            )
        perf = locked_perf
        # ⚠️ **审计前值取加锁后读回的那一份**（I1）：`order` 是调用方手里可能过期的实例，
        #    锁前别人改过价时，用它的旧值当 `from_value` 会在审计里写一句假话。
        #    ⚠️ `Order` 行锁已经在事务开头由 `lock_order_row` 拿过（I4 的统一锁序），这里只刷新列值；下面的 `save()` 再拿一次同一把锁（同一个事务，天然可重入）。
        order.refresh_from_db(fields=["hall_price"])
        old_price = order.hall_price
        order.hall_price = price
        order.updated_by = ctx.actor
        order.save(update_fields=["hall_price", "updated_by", "updated_at"])
        # ⭐ A8 / L-6：本地改了「上家事件会覆盖」的发单价 ⇒ 同事务
        #    盖「我方最后一次本地动作时刻」（唯一实现 `stamp_local_action`；否则本地改完价，
        #    一条更旧的上家事件照样把它冲掉）。位置在后面那支「提成算不出 ⇒`set_rollback(True)` 整笔拒绝」之前 ⇒ 动作被拒绝时这一笔一起回滚。
        stamp_local_action(order)
        # 改发单价 ⇒ 记一行下行 order.update。写在写价之后、重算之前，载荷取已落库的价。
        if ctx.sync_peer_orders:
            notify_order_price_changed(order)
        # ⭐ T3：**同一处**还要把价跟到**下游鸡蛋铺**那条线（第三方下家）。
        #    ⚠️ 与上一句**不是同一条线**（那一句是团团通租户 ↔ 团团通租户，D-0152）；
        #    本句给「本线已有链路」的启用绑定排 `member.price`（没有链路 ⇒ 0 条，⛔ 不排空转行）。⚠️ 排事件**不调上游**（上游由 `jidanpu_sync` 的 worker 在事务外做）。
        if ctx.sync_personal_orders:
            jidanpu_member.queue_price_update(order)
        # ⭐ 写价后同步预计绩效（spec §4.2 的 ⛔ 更正块、任务1 执行期裁定 R-1）：「预计绩效 = 发单价 × 提成率」是全局不变式、而本动作是一条能改 hall_price 的写路径 ⇒ 不同步就会出现「价变了、预计绩效还按旧价」的行。边界照那张表：无 MAIN 行 / user 为空（池内未指派）⇒ 不动 temp_amount（那时它是「录单即建」的估值、与「按人算」不是一回事）；有 MAIN 行且 user 非空 ⇒ 调共用的 recompute_order_amounts（它自己 save(update_fields=[...])；完成态下那次 save 多带 bonus_amount）；提成算不出（CommissionError / AmountError，如固定金额档）⇒ ⭐ 整笔拒绝改价（D-0254）。
        # 旧口径（spec 差异 V-H）是「跳过重算、不算失败：价照写、仍返回成功」⇒ 留下「价改了、绩效按旧价、完成时按旧口径发钱」的半笔；业务方裁定改为不吞（E-0022：可预期失败 ⇒ set_rollback(True) 再 return 业务错误）；代价 = 提成配置坏期间这张单改不了价（先修配置）——业务方已认；⚠️ 固定金额档的改价入参本身（金额档/比例档解析）不受影响：「固定金额」指接单人提成模式、被拒的是这次改价动作；⚠️ taker_price 覆盖已设时有效基价仍是覆盖价 ⇒ 这次重算是幂等的、不做特判（spec §4.2 明写）。
        # ⭐ 本批（F1，spec §2.1）起改调共用的 recompute_order_amounts：订单当前状态是「已完成」时、它会把 bonus_amount 用同一算式同一基价一起重算；非完成态一个字节都不动。⭐ Z1 起事务内会再取一次：那一份是加锁读回的（select_for_update(of=("self",))）⇒ 与结账闸门看到的是同一行同一时刻的状态、上面的 perf 只是锁外的快速预判。
        if perf is not None and perf.user_id is not None:
            # 显式挂上刚写过价的 order 实例：`effective_taker_price` 读的是
            # `performance.order.hall_price`，避免再去库里取一次那个关联对象。
            perf.order = order
            try:
                recompute_order_amounts(perf)
            except (CommissionError, _AmountError) as exc:
                # ⭐ D-0254：提成算不出 ⇒ 整笔拒绝改价 ⛔ 不再吞掉照写（旧口径 = spec 差异 V-H、上面注释块有它的现场）。
                # ⚠️ 先 set_rollback(True) 再 return（E-0022）：此刻 hall_price 已写、notify_order_price_changed / queue_price_update 已排队；块内直接 return 会把「价已改、绩效没跟 + 两条下行事件」整批提交；回滚让它们一起消失（留痕那行还没写 ⇒ 天然无「改价成功」假话）。
                # ⚠️ 码档对齐同模块同族：assign_order 的 set_taker / 重算两支与 transition_order 的钩子支对这两个异常都取 exc.code（CommissionError 的注册码 = 1000 VALIDATION）；本函数唯一的既有 _fail（1014）语义是「已结账」、与「提成配置坏」不是一回事 ⛔ 不挪用。
                transaction.set_rollback(True)
                return _fail(
                    ErrorCode.VALIDATION,
                    "提成算不出，改价未生效（请先检查提成配置）",
                    exc=exc,
                )
        # 同值口径同 `/price/`（R27.2）：不写「原发单价 44.00 → 新发单价 44.00」的假话行，
        # 但仍推 `updated_by` / `updated_at`（「谁最后确认过这个价」也留痕）。
        if price != old_price:
            record(
                log_type=ACTION_LOG_TYPES["order.change_offer_price"],
                action="order.change_offer_price",
                # ⚠️ R20：留痕的操作人取**认证态**，不取请求体的 `actor`（可伪造）。
                actor=ctx.log_actor,
                actor_id=ctx.log_actor_id,
                # ⭐ `WriteContext.log_actor_id` **有默认值 `None`**
                #（同步路径 `jidanpu_converge` 造的 ctx 正是这一档）⇒ 显式声明「没有操作人」，
                #  ⛔ 不沉默（`record()` 的 fail-closed 判据会抛 `ValueError`）。
                system=ctx.log_actor_id is None,
                target_kind="order",
                target_id=order.order_no,
                order_no=order.order_no,
                field_name="hall_price",
                from_value=old_price,
                to_value=price,
                # `%` 档由服务端填（形如「按接单价 50% 折算」），金额档为空串：
                # 不填的话「这个价是折算出来的」这条事实在日志里就丢了。
                reason=reason,
            )
    return _ok(order=order)


def _needs_jidanpu_order_guard(order):
    return order.status == OrderStatus.IN_PROGRESS or order.penalty_approval_id is not None


def _jidanpu_context_edit_busy(order, data):
    return (
        _needs_jidanpu_order_guard(order)
        and any(field in data and data[field] != getattr(order, field) for field in ("game", "source_order_no"))
        and not jidanpu_assignment_lock.lock_order_change(order.pk)
    )


def update_order_fields(
    order, *, validated, request_user, ctx, dry_run, assert_game=None
) -> WriteOutcome:
    """编辑订单的通用字段（原 OrderViewSet.partial_update 的动作体，判据与副作用逐条未改）。金额、状态与订单号不可经此修改（OrderUpdateSerializer 的只读字段）；留痕是一个被改字段一行（spec §5.1 order.update / §5.9 R8）：field_name 存真实列名、模板里的中文由 FIELD_LABELS 派生（record() 的渲染器做、调用方不得直传显示名）；值没变的字段不写行。⚠️ R21：一次编辑写多行留痕、中途失败会留下「订单改了、留痕只写了一半」的脏态（E-0022 家族）⇒ save() 与全部 record(...) 进同一个 atomic()（要么全成要么全回滚）；块内没有可预期的业务失败分支（三处校验都在块外）⇒ 不需要 set_rollback，万一将来加进来必须 set_rollback(True) 再 return（E-0022）。
    ⚠️ validate_required / validate_order_chain / assert_game_allowed 抛的 ValidationProblem 原样向上抛（spec §D5）：把它翻成业务码是视图的事、⛔ 核心层不吞它（批量侧也要靠同一个异常分流）。⚠️ dry_run=True ⇒ 跑完三处校验后返回 _ok(order=order)、不 save 不 record；本函数不需要序列化器：validated 是 OrderUpdateSerializer.validated_data（或批量侧只含 order_type 的 dict）、落库走 setattr + order.save()（与 DRF ModelSerializer.update 逐条等价，本模型没有 m2m 字段）。
    ⭐ assert_game（可选形参；默认 None = 既有那一句）= 写侧游戏准入的判据本身：None ⇒ 用既有那一句（assert_game_allowed(request_user, game)、两个内部调用点行为逐字节相同）、给了值 ⇒ 改调它；⛔ None 不是"不检查"的开关——它是「用内置那一句」的哨兵，要表达「这条路径不做这一步」必须显式传一个判据、且只有当那条路径的准入判据在更外层已经表达过了才允许（对外路径：v2 §13.10 裁定「一个应用凭据 = 本租户全部业务数据」把"按游戏收窄"整体取消了、这里没有可判的东西 ⛔ 不是因为"懒得判"）；谁会给它：开放平台对外改单端点（apps/openapi/api/order_write_views.py）——那条路上没有"操作人"这个人（Bearer 认证下 request.user 是 AnonymousUser ⇒ games_of(anonymous) 空集 ⇒ 既有那一句会把每一次改单都拒成 1000）；⛔ 不让核心去猜 AnonymousUser、⛔ 不传 None 当跳过、⛔ 不造假 User 去骗 is_tenant_admin（同款先例：order_intake.IntakeActor.assert_game 对外恒 None）。⚠️ 证明力边界：默认支的判据是既有用例（tests/test_order_writes_core.py / test_order_batch_actions.py / test_order_api.py）一个字未改仍然绿；给值支的判据是 tests/test_openapi_order_update.py（把 assert_game 形参连同 _no_game_admission 一起去掉、它的头两条成功用例当场回 1000 ⇒ 红）。"""
    data = dict(validated)
    if "project_text" in data:
        data["project_text"] = normalize_project_text(data["project_text"])
    validate_required(
        game=data.get("game", order.game),
        project_text=data.get("project_text", order.project_text),
    )
    validate_order_chain(
        game=data.get("game", order.game),
        server=data.get("server", order.server),
        category=data.get("category", order.category),
        project=data.get("project", order.project),
    )
    # 写侧准入（同上、顺序理由见 create）：改单也不许把单挂到自己没有权限的游戏上。
    # data.get("game", order.game)：没改游戏的 PATCH 用的是当前游戏、而它必然在我的可见范围内（否则 get_object() 早就 1002）⇒ 不会误伤。
    # ⭐ 判据本体可由调用方给（assert_game、语义与边界见 docstring）：None ⇒ 就是搬迁前这一行（同一个函数 + 同一个实参顺序 ⇒ 两个内部调用点逐字节不变）。
    if assert_game is None:
        assert_game_allowed(request_user, data.get("game", order.game))
    else:
        assert_game(data.get("game", order.game))
    if dry_run:
        return _ok(order=order)
    if "total_price" in data:
        return _update_order_fields_with_price(
            order, data=data, request_user=request_user, ctx=ctx
        )
    # spec §5.1：PATCH 走的是另一个序列化器（OrderUpdateSerializer）、但 order_source ⇄ source 的规则必须与 create 同源——不共用就会出现「FK 指向 A、名字却是随手写的」的分叉（Critical 2）；规则在 apps/business/order_source.py、两处调用同一个纯函数。
    # ⚠️ 必须把当前 FK 传进去（I-1）：前端编辑态在订单已有来源时总是回传同一个 order_source、不传这一项就会退回「编辑即重拍名字快照」、推翻 D4/R7。
    # 改动清单必须在 save() 之前算：save 会把新值写回 order；比较用的是模型实例（FK 按 pk 比）、不是字符串——字符串比较会把 Decimal("88.00") 与 88.00 这类同值不同形判成「变了」。
    # 留痕的操作人取**认证态**（R20）；`record` 与业务写入同事务（R21）。
    with transaction.atomic():
        locked_order = lock_order_row(order)
        if locked_order is None:
            return _fail(ErrorCode.BUSINESS, "记录不存在或已被删除")
        if locked_order.peer_direct_pending is not None:
            return _fail(ErrorCode.BUSINESS, PEER_DIRECT_PENDING_MESSAGE)
        if _jidanpu_context_edit_busy(locked_order, data):
            return _fail(ErrorCode.BUSINESS, jidanpu_assignment_lock.ORDER_BUSY)
        if "source_order_no" in data:
            order.source_order_no = locked_order.source_order_no
        changed = [
            (field, getattr(order, field), value)
            for field, value in data.items()
            if getattr(order, field) != value
        ]
        # ⚠️ 必须在 data 落到 order 之前算：source_snapshot_overrides 读的是 current_order_source_id（I-1 的「同一个 FK ⇒ 不重拍名字」那一支）、而新 FK 一旦 setattr 上去它就变成新 id ⇒ 同一次 PATCH 换绑字典项时会被误判成「同一个 FK」、source 名字快照静默不更新（实测：test_order_source_intake.py 两条红）。
        # 原动作体把它作为 serializer.save(**overrides) 的实参、求值也在 save 之前——同一位次。
        source_overrides = source_snapshot_overrides(
            data, current_order_source_id=order.order_source_id
        )
        for field, value in data.items():
            setattr(order, field, value)
        # `Order.updated_by` 是**业务字段**，仍按 P2 契约取请求体的 `actor`（不在本次收紧范围）。
        order.updated_by = ctx.actor
        for field, value in source_overrides.items():
            setattr(order, field, value)
        # ⭐ E-0251：窄写——update_fields = 请求涉及字段 ∪ source_overrides 键 ∪ updated_by ∪ updated_at。旧行为（不带 update_fields 的整行 save）把陈旧实例上全部已加载列写回库、会把并发刚提交的状态/发单归属/发单时间整批覆盖回装载时的旧快照（双连接探针实锤；同族先例：本模块 change_price 的窄写清单）；changed 的计算与下面全部 record(...) 留痕只读 data 与 save 前的实例值、不依赖整行写 ⇒ 一并不动。
        # ⚠️ Django 5.2 语义（源码核实 ⛔ 不是猜的）：Model._save_table 只对 update_fields 列出的字段调 pre_save、而 auto_now 的取「现在」正发生在 DateTimeField.pre_save ⇒ updated_at 不在清单里就不前进、显式列上才前进；这里永远含 updated_by / updated_at 两列 ⇒ 清单恒非空（空清单会被 Django 当作「无事可做」静默跳过整个 save）。
        order.save(
            update_fields=(
                set(data.keys()) | set(source_overrides.keys()) | {"updated_by", "updated_at"}
            )
        )
        for field_name, old_value, new_value in changed:
            record(
                log_type=ACTION_LOG_TYPES["order.update"],
                action="order.update",
                actor=ctx.log_actor,
                actor_id=ctx.log_actor_id,
                # ⭐ `WriteContext.log_actor_id` **有默认值 `None`**
                #（同步路径 `jidanpu_converge` 造的 ctx 正是这一档）⇒ 显式声明「没有操作人」，
                #  ⛔ 不沉默（`record()` 的 fail-closed 判据会抛 `ValueError`）。
                system=ctx.log_actor_id is None,
                target_kind="order",
                target_id=order.order_no,
                order_no=order.order_no,
                source_order_no=order.source_order_no,
                field_name=field_name,
                from_value=_field_text(old_value),
                to_value=_field_text(new_value),
            )
        _dispatch_content_update(order, changed, ctx=ctx)
    return _ok(order=order)


def _update_order_fields_with_price(order, *, data, request_user, ctx) -> WriteOutcome:
    """改单内容与接单价共用一笔事务；拆分组非锚点的价格差由锚点反向吸收。"""
    with transaction.atomic():
        scope = Order.objects.select_for_update()
        if order.split_id:
            locked_members = list(
                scope.filter(Q(split_id=order.split_id) | Q(pk=order.pk)).order_by("id")
            )
        else:
            locked_members = list(scope.filter(pk=order.pk).order_by("id"))
        by_pk = {member.pk: member for member in locked_members}
        locked_order = by_pk.get(order.pk)
        if locked_order is None:
            return _fail(ErrorCode.BUSINESS, "订单已变化，请刷新后重试")
        if locked_order.peer_direct_pending is not None:
            return _fail(ErrorCode.BUSINESS, PEER_DIRECT_PENDING_MESSAGE)
        if _jidanpu_context_edit_busy(locked_order, data):
            return _fail(ErrorCode.BUSINESS, jidanpu_assignment_lock.ORDER_BUSY)
        if _order_payment_in_progress(by_pk):
            return _fail(ErrorCode.BUSINESS, "订单有正在处理的在线付款，暂时不能改价")
        old_price = locked_order.total_price
        new_price = data["total_price"]
        price_changed = old_price != new_price
        anchor = None
        anchor_price = None
        anchor_old_price = None
        if split_group.is_split_member(locked_order):
            anchor = next(
                (row for row in locked_members if row.split_kind in ("", split_group.LEGACY, split_group.PEER)),
                None,
            )
            if anchor is None:
                return _fail(ErrorCode.BUSINESS, "拆分组原单不存在，请刷新后重试")
            if price_changed and anchor.pk == locked_order.pk:
                return _fail(ErrorCode.BUSINESS, "拆分组原单接单价由系统调平，不能直接修改")
            if price_changed:
                if not ({"order.submit_payout_price", "order.edit_payout_price"} & ctx.nodes):
                    return _fail(403, "你没有修改接单价的权限")
                try:
                    assert_game_allowed(request_user, anchor.game)
                except ValidationProblem as problem:
                    return _fail(problem.code, str(problem))
                anchor_old_price = anchor.total_price
                anchor_price = anchor_old_price - (new_price - old_price)
                try:
                    DecimalValidator(max_digits=12, decimal_places=2)(anchor_price)
                except ValidationError:
                    return _fail(ErrorCode.VALIDATION, "调平后的原单接单价超出有效金额范围")
        elif price_changed:
            return _fail(
                ErrorCode.VALIDATION,
                "普通订单请使用接单价修改入口",
            )

        changed = [
            (field, getattr(locked_order, field), value)
            for field, value in data.items()
            if getattr(locked_order, field) != value
        ]
        source_overrides = source_snapshot_overrides(
            data, current_order_source_id=locked_order.order_source_id
        )
        for field, value in data.items():
            setattr(locked_order, field, value)
        locked_order.updated_by = ctx.actor
        for field, value in source_overrides.items():
            setattr(locked_order, field, value)
        locked_order.save(
            update_fields=set(data.keys()) | set(source_overrides.keys()) | {"updated_by", "updated_at"}
        )
        for field_name, old_value, new_value in changed:
            if field_name == "total_price":
                continue
            record(
                log_type=ACTION_LOG_TYPES["order.update"],
                action="order.update",
                actor=ctx.log_actor,
                actor_id=ctx.log_actor_id,
                system=ctx.log_actor_id is None,
                target_kind="order",
                target_id=locked_order.order_no,
                order_no=locked_order.order_no,
                source_order_no=locked_order.source_order_no,
                field_name=field_name,
                from_value=_field_text(old_value),
                to_value=_field_text(new_value),
            )
        if price_changed:
            if ctx.recheck_external_refunds:
                queue_externalpay_recheck(locked_order)
            _record_taker_price_change(locked_order, old_price, new_price, ctx)
        if anchor is not None and price_changed:
            anchor.total_price = anchor_price
            anchor.updated_by = ctx.actor
            anchor.save(update_fields=["total_price", "updated_by", "updated_at"])
            if ctx.recheck_external_refunds:
                queue_externalpay_recheck(anchor)
            _record_taker_price_change(
                anchor, anchor_old_price, anchor_price, ctx,
                reason="拆分组接单价自动调平",
            )
            from apps.business.settlement_hooks import refresh_customer_estimate

            if locked_order.split_kind in ("", split_group.LEGACY):
                refresh_customer_estimate(anchor)
            else:
                refresh_customer_estimate(locked_order)
                refresh_customer_estimate(anchor)
        _dispatch_content_update(locked_order, changed, ctx=ctx)
    return _ok(order=locked_order)


def _dispatch_content_update(order, changed, *, ctx):
    """在订单及审计已写入的同一事务里走唯一内容同步/本地戳漏斗。"""
    fields = {field for field, _old, _new in changed}
    if ctx.sync_personal_orders:
        if fields.intersection(("project_text", "remark")):
            jidanpu_member.queue_project_update(order)
        if "source_order_no" in fields:
            jidanpu_member.queue_source_order_no_update(order)
    if ctx.sync_peer_orders:
        notify_order_content_changed(order, changed)
    if CONFLICT_PRONE_FIELDS.intersection(fields):
        stamp_local_action(order)


def transition_order(order, *, target, ctx, dry_run, remark="", allow_taker_release=False) -> WriteOutcome:
    """改状态（原 OrderViewSet.transition 的动作体，判据与副作用一字未改）。
    ⚠️ apply_transition 要求调用方在事务里（它内部要 select_for_update 复读状态；autocommit 下 TransactionManagementError ⇒ 响亮失败）；dry_run=True 时根本不调它、改用纯内存判据 transition_denial——后者与 apply_transition 的第一道闸同源（就是同一个函数）⛔ 不许在核心层另写一套「能不能改」的判断。dry_run 的判据放在 apply_transition 之外、两条路径各自的汇合处：第一道闸（边+节点）由 transition_denial 给、dry_run 与真写都走它；第二道闸（R-10a「已结账的绩效不许回池」）dry_run 显式调 settled_pool_entry_denial（真写路径由 apply_transition 内部调同一个判据——spec §4.2 把它列为逐笔合格性、§D5 要求 dry_run 跑完全部判据 ⇒ 两条分支必须给同一个答案 ⛔ 不是第二套判据）；第三道闸（D4b「已结账 ⇒ 拒绝一切状态变更」）dry_run 调 settled_status_change_denial、真写由 apply_transition 调同一个函数（⚠️ 排在第二道闸之后、D2 之前——spec §4.1.3 的固定顺序）；第四道闸（D2「有接手人 ⇒ 不许回池」）dry_run 调 taker_present_pool_entry_denial、真写由 apply_transition 调同一个函数（⚠️ 这一道不带例外开关：批量两段式里没有「显式清除接单人」这个动作）；目标态钩子的预期失败仍只在真写路径上跑（dry_run 本来就不跑钩子）。
    ⚠️ nodes 在核心层归一化一次（None ⇒ frozenset()，与 apply_transition 同口径）：transition_denial(..., nodes=None) 的宽松语义是「这条边存在吗」、照单全收会让 dry_run 在 ctx.nodes=None 时 fail-open 放行而真写路径拒绝。⚠️ 失败时 detail["exc"] 放原异常，翻码规则与搬迁前一致：IllegalTransition ⇒ 1002；RefundError / CommissionError / AmountError ⇒ exc.code（RefundError 会是 1007/1008/1009/1010）。⚠️ apply_transition 的 actor 取 ctx.log_actor（认证态用户名）⛔ 不是 ctx.actor：搬迁前的 /transition/ 明写「actor 取认证态的用户名、不再读请求体的 actor」，而它同时决定 order.updated_by——退款钩子从这个列取操作人写 order.refund 留痕（见下面的行内注释）。⭐ remark（可选形参）= 调用方给的一句「为什么改这个状态」⇒ 落进成功那条留痕行的 OperationLog.reason 列（模板 order.transition 的正文里没有它、但那一列会被操作日志页/导出读出来、与 order.change_offer_price 的 reason 同一列）；谁会给它：开放平台对外改状态端点（apps/openapi/api/order_write_views.py，契约 {"status": …, "remark": ""}）；⚠️ 恒可选：本函数不为任何目标态（含「问题单/已暂停」这两个最常用到它的）加"必须给 remark"的分支——那会让有 remark 的调用方与没有它的调用方在同一条边上得到两种结果、而内网那条路今天不要求它；列宽同源：reason 是 varchar(255)、record() 内部按列宽截断、对外端点那侧序列化器也限长 255（⛔ 不让"请求里能填、库里静默截断"这种组合存在）；默认值不影响既有调用点：默认 "" 与"不传"在 record() 里是同一个值（reason 形参自己的默认值也是 ""）⇒ 内网 /transition/、/report-issue/、批量端点与同步链路的行为逐字节不变（判据：tests/test_operation_log_write_paths.py 里那条「前后值是中文标签、field_name=status」的既有用例一个字未改仍然绿；新用例断言"带 remark ⇒ 库里那一行的 reason 逐字等于传入值"）。"""
    # ⚠️ 归一化必须在**两条分支之前**（Minor #6）：`None` 是「没有身份」⇒ 空集（拒绝），
    #    与 `apply_transition` 内部那句同口径；否则 `dry_run` 会 fail-open。
    nodes = frozenset() if ctx.nodes is None else ctx.nodes
    denied = transition_denial(order.status, target, nodes)
    if denied:
        return _fail(ErrorCode.BUSINESS, denied, exc=IllegalTransition(denied))
    # ⭐ D4（fx-D）：目标 = 待抢单（抛大厅）时，远端已接手的一律拒。
    # 放在 `dry_run` 分支之前 ⇒ 预检与真写给同一个答案。判据本体在 `dailiantong_push`
    # （与 A2「代练通优先」同一份实现）；懒导入避免热模块的循环。
    if str(target) == str(OrderStatus.WAITING_GRAB):
        from apps.business.dailiantong_push import (
            hall_throw_blocked_by_platform_denial,
        )

        platform_denial = hall_throw_blocked_by_platform_denial(order)
        if platform_denial:
            return _fail(
                ErrorCode.BUSINESS,
                platform_denial,
                exc=IllegalTransition(platform_denial),
            )
    if dry_run:
        # ⭐ Important #1（spec §4.2 / §D5）：`dry_run` 也要跑第二道闸——「已结账的绩效行
        #    不许经回池被改钱」。判据**复用** `order_status` 的公开实现（`apply_transition`
        #    内部调的是同一个函数）⇒ 同一份判据、两处调用，⛔ 不是两套。
        settled_denial = settled_pool_entry_denial(order, target)
        if settled_denial:
            return _fail(
                ErrorCode.BUSINESS, settled_denial, exc=IllegalTransition(settled_denial)
            )
        # ⭐ **D4b**（spec §4.1.3 的固定顺序 / §4.4.1 / §7.2 #13）：已结账 ⇒ **一切**目标态都拒。
        #    与真写路径**同一份判据**（`apply_transition` 里调同一个函数）⇒ 两段式不会
        #    「预检说合格、执行被拒」。位置与真写同序：池内那道之后、D2 那道之前。
        status_change_denial = settled_status_change_denial(order, target)
        if status_change_denial:
            return _fail(
                ErrorCode.BUSINESS,
                status_change_denial,
                exc=IllegalTransition(status_change_denial),
            )
        # ⭐ D2（spec §4.1.1 第 2 点 / §7.2 #8）：`dry_run` 与真写**必须同答案**——
        #    漏了这一处就是「预检说合格、执行被拒」，用户在清单层点完「确定生成」才被拒。
        #    ⚠️ 这里**不传** `allow_taker_release`：批量两段式里没有「清除接单人」这个动作（`/clear-taker/` 不在任何批量端点里），⛔ 别为了「万一」把它打开。
        if not allow_taker_release:
            taker_denial = taker_present_pool_entry_denial(order, target)
            if taker_denial:
                return _fail(
                    ErrorCode.BUSINESS, taker_denial, exc=IllegalTransition(taker_denial)
                )
        return _ok(order=order)
    # ⚠️ `from_status` 必须在 `atomic()` **之前**取（与搬迁前同位次）：`apply_transition`
    #    会就地改写 `order.status`，进块之后再取就成了「改完之后的状态」。
    from_status = order.status
    with transaction.atomic():
        try:
            apply_transition(
                # ⚠️ actor 取 ctx.log_actor（认证态用户名）⛔ 不是 ctx.actor（请求体的 actor）：搬迁前的 /transition/ 明确「不再读请求体的 actor」——它同时写 order.updated_by、而退款钩子（on_order_refunded）正是从这个列取操作人写 order.refund 留痕（refunds.py 的钩子契约：func(order, from_status) 没有 actor 形参）；用 ctx.actor 会让那条留痕的操作人静默变成空串。
                order, target, nodes=nodes, actor=ctx.log_actor,
                # ⭐ `/transition/` 是**通用**端点，目标态可以是池内两态
                #    （`REFUNDED → {UNASSIGNED, WAITING_GRAB}`）⇒ 回池冲销的两条留痕是
                #    **人为动作**，把认证态主键一起传下去（⛔ 不是让域层反查 `updated_by`）。`ctx.log_actor_id` 为 `None`（同步路径造的 ctx）⇒ 留痕显式登记成机器行。
                actor_id=ctx.log_actor_id,
                notify_worker=ctx.notify_worker,
                allow_taker_release=allow_taker_release,
                allow_financial_writes=ctx.allow_financial_writes,
                sync_personal_orders=ctx.sync_personal_orders,
                sync_peer_orders=ctx.sync_peer_orders,
                notice_reason=remark,
                # 发单人（F5）：进 `WAITING_GRAB` 就是「抛到大厅」
                # ⇒ 与 `/throw-to-hall/` 走**同一个漏斗**写 `ctx.user`；其余目标态
                # 传 `None`（= 不动它）。判据用 `str(...)`：`target` 可能是 `OrderStatus`，与搬迁前同口径。
                hall_publisher=(
                    ctx.user if str(target) == OrderStatus.WAITING_GRAB else None
                ),
            )
        except IllegalTransition as exc:
            # ⚠️ 这一支**不** `set_rollback`（搬迁前逐字如此）：`apply_transition` 在写任何
            #    东西之前就抛它（边 / 节点不足、复读发现陈旧、已结账回池），没有半笔可回滚。
            return _fail(ErrorCode.BUSINESS, str(exc), exc=exc)
        except (RefundError, CommissionError, _AmountError) as exc:
            # 迁移钩子的预期失败、翻成业务码——不是 HTML 500：RefundError（退款前置校验）、CommissionError（客服提成确定）、AmountError（金额不可计算）都不该被上面那条 IllegalTransition 捕获、否则「算不出提成」会被伪装成「状态非法」（E-0028 同族）。
            # ⚠️ 先 set_rollback(True) 再 return：这个 except 把异常吞在 with 内部、而 apply_transition 在跑钩子之前已经保存过状态 ⇒ 不显式回滚就会把「状态已改、账没记」的半笔提交掉（E-0022）。
            transaction.set_rollback(True)
            return _fail(getattr(exc, "code", ErrorCode.VALIDATION), str(exc), exc=exc)
        # spec §6 硬规则 2：迁移必须留痕。**只在成功之后记**——失败的迁移没有发生。
        # ⚠️ 位置与搬迁前相同：在 `try` **之外**（`record` 冒出的未预期异常要让它响亮地
        #    冒出 `atomic()` 整笔回滚，不能被上面两条 `except` 吞掉）。
        record(
            log_type=ACTION_LOG_TYPES["order.transition"],
            action="order.transition",
            actor=ctx.log_actor,
            actor_id=ctx.log_actor_id,
            # ⭐ 同上面几处：`log_actor_id` 默认 `None` ⇒ 显式声明机器来源。
            system=ctx.log_actor_id is None,
            target_kind="order",
            target_id=order.order_no,
            order_no=order.order_no,
            source_order_no=order.source_order_no,
            field_name="status",
            # 前后值是**中文标签**（spec §5.1；§9.1 的旧数据迁移按同一份 choices 翻译）
            from_value=_status_text(from_status),
            # ⚠️ 与搬迁前**逐字相同**（Minor #5）：搬迁前这里取的是 `order.status`
            #    （不是 `target`）。两者可观测等价——`apply_transition` 已把
            #    `order.status = str(target)`，且只有合法边才会走到这一行 ——但 Global Constraint 要求「参数与搬迁前逐字相同」，故照旧取 `order.status`。
            to_value=_status_text(order.status),
            # ⭐ T5：调用方给的「为什么改」（对外端点的 `remark`）落这一列；不传 ⇒ 空串
            #    （与搬迁前"不传 `reason`"逐字节等价，见本函数 docstring 的 `remark` 一节）。
            reason=remark,
        )
    return _ok(order=order)


def _penalty_assignment_amount_denial(order, *, taker, price, fee):
    approved = approved_penalty_price(order)
    if approved is None:
        return None
    if (order.hall_price if price is None else price) != approved:
        return _fail(ErrorCode.BUSINESS, PENALTY_AMOUNT_LOCKED)
    if fee is not None:
        try:
            expected_fee = commission_amount(approved, commission_percent_of(taker))
        except (CommissionError, _AmountError) as exc:
            return _fail(exc.code, exc.message, exc=exc)
        if fee != expected_fee:
            return _fail(ErrorCode.BUSINESS, PENALTY_AMOUNT_LOCKED)
    return None


def assign_order(order, *, taker, price=None, fee=None, hours=None, project=None,
                 ctx, dry_run, allow_status_change_while_pushed: bool = False,
                 allow_missing_price: bool = False, chat_follow_up=False) -> WriteOutcome:
    """指派 / 改派接单人。⛔ 请求级校验不进本函数（spec §D5）：AssignSerializer、「目标用户不存在/已停用」以及带 price/fee 时的两道 has_node_now("order.submit_payout_price") 闸（失败 403）都留在视图、在调本函数之前做一次；taker 因此是已查好且确认启用的 User 实例。每单判据（⓪ 为 D-0245 追加、在函数入口；⓪′ 为 D-0252 追加、在函数入口；①–③ 顺序照搬迁前的动作体）：⓪实名闸（D-0245）：本租户开着「未实名不允许接单」且 taker 是未实名的代练员 ⇒ 1002、文案「该打手未完成实名认证，不能指派接单」——判定单一出处 realname.services.claim_gate_blocks（与接单侧同源；spec 2026-10-16-realname-claim-gate-design.md §2.3）；taker=None（抛大厅）不受限；dry_run=True 照判（批量预检要能预演出这一拒）。⓪′游戏范围闸（D-0252）：taker 的档案游戏集合不包含这张单的游戏（含无档案 ⇒ 空集）⇒ 1002、文案「该打手没有『<游戏名>』的接单权限」——判定单一出处 game_scope.assert_game_id_allowed（games_of(taker) 为允许集合、与读侧收窄/写侧准入同源）；单笔 /assign/、批量 batch_assign、录单快速指派三条人工入口同闸（都落在本函数）；机器自动接手/镜像单路径不经过本函数、各有自己的闸（D-0252 ③ 不改它们）；taker=None / order.game=None 不受限；dry_run=True 照判。①taker.id == order.sales_user_id ⇒ 1002（同一 (order, user) 只能有一条绩效行、uniq_performance_order_user；文案逐字照抄）。②MAIN 绩效行 is_settled ⇒ 1014（判据是 Performance.is_settled 三字段并集 ⛔ 不手抄那三个字段；⚠️ 它不是全仓唯一的「已结账」判法——order_stats._unsettled_amount 与 settlement._reject_message 单看 settle_status，见 Performance.is_settled 的更正）——锁外先快速预判一次（任何写入之前 ⇒ 被拒时一行不写）；⭐ Z1 起事务内还会对绩效行 select_for_update(of=("self",)) 重取并再判一次（并发结账的 TOCTOU）、被拒时 set_rollback(True) 再 return。③order.status != IN_PROGRESS 时 transition_denial(...) 有原因 ⇒ 1002（msg 用原因原文）；已是 IN_PROGRESS 时跳过（R-3：状态机没有自环 ⛔ 也不给它加）。
    ⚠️ dry_run=True ⇒ 跑完上面三条后 _ok 返回、零副作用：不 reset_taker_performance / 不 set_taker / 不写价 / 不 recompute_order_amounts / 不 record / 不 apply_transition、也不补行不复活软删行（spec §4.3 五条；批量侧就是靠这一支做预检）。⚠️ 真写路径的五条特例逐条保留（spec §4.3 的 ⛔ 更正块）：1. str(order.status) != IN_PROGRESS 时才 apply_transition（R-3）；2. perf.user_id 非空且换人时才跑七列回退 reset_taker_performance（R-2b：同一个人再保存 ⇒ 跳过回退、保住他自己的 taker_price/hours/project）；3. 被软删的 MAIN 行复活（清 deleted_at/deleted_by ⛔ 不新建行——uniq_order_main_taker 只允许一条 MAIN 行）；4. perf is None ⇒ 在事务内、apply_transition 成功之后补行（位置承重：块内 return 会提交它前面的写入（E-0022）⇒ 补行放在前面时一次被拒的指派会凭空留下一条绩效行）；5. ⭐ 钱的目标态钩子不在本入口跑（R10 / R11 / E-0163）：那次 apply_transition 传 skip_amount_recalc=True——那个钩子会按绩效行当前归属人重算、而本函数紧接着就用本次要指派的人 set_taker 重算同一列 ⇒ 让钩子跑是白算、且旧接单人配置坏时会挡掉一次合法改派；真算不出来由紧随其后的 set_taker 那一支接住（R7 未放松）；通知钩子与 apply_transition 尾上的副作用照跑（逐条理由见 apply_transition 的 skip_amount_recalc 段）。 ⚠️ apply_transition 的 actor 取 ctx.log_actor（认证态用户名）⛔ 不是 ctx.actor：搬迁前的 /assign/ 那句 actor 就是认证态用户名、它同时进 order.updated_by、而退款钩子从这个列取操作人写留痕（同 transition_order）；record(...) 的 actor/actor_id 同源取认证态（R20）。⚠️ peer_dispatch 与 jidanpu_hall 两条同事务副作用也在块内、record(...) 之后——搬迁前的位置逐条相同；失败路径走不到那里。
    ## ⭐ `hall_publisher` 直写点（D-0369 业务方口径）「**指派（含改派）也算发单动作：谁把单指派出去，发单人就是谁；保留最后一次** （沿既有口径）；**打手自己抢单不算**（`/claim/` 不走本函数，一行没改）； **机器 / 开放平台路径不算**」。落实：真写路径的 `transaction.atomic()` 块内、迁移支（含 R-3 跳过）与已结账复查都落定之后、`set_taker` 之前，直写： if ctx.user is not None: order.hall_publisher = ctx.user order.save(update_fields=["hall_publisher", "updated_at"]) **谁写谁不写**（行为矩阵）：单笔 `/assign/`、批量 `/batch/assign/`、录单+指派（网页控制台，`ctx.user` = 录单人）三路**写**；开放平台对外录单+指派、代练通占位接手、修复命令 `fix_missing_taker_performance` 三路的 ctx `user=None` ⇒ **不写**（与既有「openapi / 机器抛单不写发单人」同口径；注释照实，见 `apps/openapi/api/order_write_views.py` 与 `apps/business/dailiantong_push.py` 各自的 `_write_context` 说明）。 **为什么直写、不走 `apply_transition` 的 `hall_publisher` 漏斗参数**（形状是刻意选的，⛔ 别「顺手统一」）： 1. 迁移支（`status != IN_PROGRESS`）若经漏斗传 `hall_publisher=ctx.user`，漏斗会触发 `order_time_fields.mark_published(order)`（**无 `moment`**） ⇒ 抢在 `set_taker` 自己那次带 `moment=now` 的 `mark_published` 之前写掉 `published_at`（`_mark` 只在 IS NULL 时写）⇒ 打红既有钉子 `tests/test_order_column_fields.py` 的 `PublishedAtWritePathTests::test_assign_writes_published_at_equal_to_claimed_at` （断言 `published_at` 与 `perf.claimed_at` **逐微秒相等**，同一 `now` 对象）； 2. 改派支（已 `IN_PROGRESS`，迁移被 R-3 跳过）本来就没有漏斗调用可传，直写是必须的 ⇒ 两支统一用同一个直写点覆盖。 ⚠️ **E-0027 回查（2026-11-03，给新写入点补「回退 / 清理 / 重置」路径的结论）**： `hall_publisher` **没有**回退 / 清理 / 重置路径——撤回 / 退款 / 回池都保留（既有钉子：`tests/test_order_column_fields.py` 的「撤回保留」「transition 非大厅目标不动」「claim 不动」三条），再抛一次则覆盖（保留最后一次）。本直写点 **不新增**清理义务；也**不**调 `stamp_local_action`——那是「上家事件会覆盖」列（备注 / 项目 / 电话 / QQ / 发单价）的本地动作时刻戳，发单人不在那组列里（带价指派的既有盖章在下面 `price` 那一支，与本次无关）。"""
    # ⓪ 实名闸（D-0245，spec 2026-10-16-realname-claim-gate-design.md §2.3）：本租户开着「未实名不允许接单」且目标 taker 是未实名的代练员 ⇒ 拒。判定单一出处 realname.services.claim_gate_blocks（开关 + 代练员 Group + 未实名、与接单侧同源 ⛔ 本模块不抄第二份）；错误形状照本函数既有业务失败（_fail + ErrorCode.BUSINESS）、文案逐字（E-0024）；taker=None（抛大厅）不受限——闸函数对 None 放行。
    # ⚠️ 判据只看 taker 这个人、与哪张单无关 ⇒ 批量指派里同一 taker 的每一单各自落 ineligible（逐单语义，spec §3-2d 如实登记：一单被拦的形状在批量侧不存在「其它单照常」、因为整批共用同一个 taker）；⚠️ 位置在①之前：这是对「人」的资格闸、先于人单关系判。
    if claim_gate_blocks(user=taker):
        return _fail(ErrorCode.BUSINESS, "该打手未完成实名认证，不能指派接单")
    # ⓪′ 游戏范围闸（D-0252）：目标打手的档案游戏集合必须包含这张单的游戏；无档案 = 无游戏 = 拒（games_of 的空集语义、fail-closed）。四条「接」的路（打手 App 大厅、镜像单、内部大厅自接、T5 取单口）都有各自的写侧游戏闸、唯独人工指派漏了——本闸补齐（判据本体走 game_scope.assert_game_id_allowed、即该模块 docstring 点名的「不在集合里就拒」原语 ⛔ 不抄第二份 membership 判定；管理员打手经 games_of 的短路拿到全部游戏、与其余各路同口径）。
    # 它抛的 ValidationProblem 文案（「你没有该游戏的权限」）是写给操作人自己的写侧准入；这里是「你替别人选了人」⇒ 按本函数既有的业务失败形状（_fail + ErrorCode.BUSINESS、与上面实名闸同一档）重述、文案带游戏名（D-0252 ④）。
    # ⚠️ 位置在①之前：与⓪同为「目标人的资格」档、先于人单关系判；⓪的既有顺序保持不动（已因实名被拒的请求拿到的还是实名那句）。⚠️ taker=None / order.game=None ⇒ 放行：前者照⓪的「taker 由调用方解析」契约（本函数不替调用方 500）、后者的「游戏必填」由既有校验管（同 assert_game_id_allowed 的 None 口径）；⚠️ 在 dry_run 分流之前判 ⇒ 批量预检能预演出这一拒（与⓪同一条纪律）。
    if taker is not None and order.game is not None:
        try:
            assert_game_id_allowed(games_of(taker), order.game)
        except ValidationProblem:
            return _fail(
                ErrorCode.BUSINESS,
                f"该打手没有「{order.game.name}」的接单权限",
            )
    # ① 目标就是本单的销售提成归属人（C1）：录单已为 `order.sales_user` 建了一条
    #    SALES 绩效行（`performance.py:62-72`），而 `uniq_performance_order_user` 是**不分
    #    `emp_type`** 的 `(order, user)` 唯一约束 ⇒ 放行会撞唯一索引 ⇒ `psycopg` 的`UniqueViolation` ⇒ **DRF 500**（⛔ 不要为让路把约束放宽成 `(order, user, emp_type)`）。
    if taker.id == order.sales_user_id:
        return _fail(
            ErrorCode.BUSINESS,
            "该账号是本单的销售提成归属人，不能同时指派为接单人"
            "（同一订单同一人只能有一条绩效）；若要支持兼岗需先放开约束",
        )
    # ② R-2a 闸门：MAIN 行已结账 ⇒ 一律拒绝（不看换不换人）。位置在 `atomic()` 与任何写入之前。
    # ⚠️ 缺行在这里**不补**（补行会写一行，而被拒的请求原本一行不写）⇒ 缺行等价于
    #    「谈不上已结账」，真正的补行见下面 `perf is None` 那一支（事务内、迁移成功之后）。
    perf = Performance.objects.filter(order=order, emp_type=EmpType.MAIN).first()
    if perf is not None and perf.is_settled:
        return _fail(
            ErrorCode.PERFORMANCE_STATE_INVALID,
            "该单的接单人绩效已结账，不能改派；请先撤销结账再指派",
        )
    # ③ 状态机边（R-3 跳过自环）。`nodes` 归一化：`None` = 没有身份 ⇒ 空集（fail-closed），
    #    与 `apply_transition` 内部那句同口径。
    nodes = frozenset() if ctx.nodes is None else ctx.nodes
    if str(order.status) != OrderStatus.IN_PROGRESS:
        denied = transition_denial(order.status, OrderStatus.IN_PROGRESS, nodes)
        if denied:
            return _fail(ErrorCode.BUSINESS, denied)
    penalty_denial = _penalty_assignment_amount_denial(order, taker=taker, price=price, fee=fee)
    if penalty_denial is not None:
        return penalty_denial
    if dry_run:
        if order.peer_direct_pending is not None:
            return _fail(ErrorCode.BUSINESS, PEER_DIRECT_PENDING_MESSAGE)
        return _ok(order=order)
    with transaction.atomic():
        locked_order = lock_order_row(order)
        if locked_order is None or locked_order.status != order.status:
            return _fail(ErrorCode.BUSINESS, "订单状态已变化，请刷新后重试")
        if locked_order.peer_direct_pending is not None:
            return _fail(ErrorCode.BUSINESS, PEER_DIRECT_PENDING_MESSAGE)
        if not jidanpu_assignment_lock.lock_order_change(order.pk):
            return _fail(ErrorCode.BUSINESS, jidanpu_assignment_lock.ORDER_BUSY)
        penalty_denial = _penalty_assignment_amount_denial(locked_order, taker=taker, price=price, fee=fee)
        if penalty_denial is not None:
            return penalty_denial
        previous_status = order.status
        was_in_hall = previous_status == OrderStatus.WAITING_GRAB
        # 特例 1（R-3）：已是 `IN_PROGRESS` 时跳过迁移（`TRANSITIONS` 没有自环）。
        if str(order.status) != OrderStatus.IN_PROGRESS:
            try:
                apply_transition(
                    order, OrderStatus.IN_PROGRESS, nodes=nodes, actor=ctx.log_actor,
                    # ⭐（R10 / R11 / E-0163）：只跳过「按当前接单人重算金额」那个目标态钩子——它读的是马上要被换掉的旧接单人、而本函数紧接着就用 set_taker 按本次要指派的人重算同一列 ⇒ 让钩子跑是白算、旧人配置坏时（如提成模式是「固定金额」）还会挡掉一次合法改派并 500。
                    # ⛔ 这不是「整段跳过钩子」也不是「吞异常」：通知钩子与尾上的副作用照跑；真算不出来由紧随其后的 set_taker 那一支接住（R7 未放松）。
                    skip_amount_recalc=True,
                    sync_personal_orders=ctx.sync_personal_orders,
                    sync_peer_orders=ctx.sync_peer_orders,
                    # 变更完成后再判断是否该告诉原打手继续做。
                    notify_worker=False,
                    # ⭐ 指派族是 hall.take「为什么离厅」的唯一带因支：下家据此收单
                    #（判定表：抢单受理 / 自抢 / 撤回 / 清人 / 软删发裸 take，不传）。
                    hall_leave_reason=HALL_TAKE_LEAVE_ASSIGNED,
                    # ⭐ A2（计划 Rev.9 §2）：只有代练通同步线传 `True`（见 docstring 末段）。
                    allow_status_change_while_pushed=allow_status_change_while_pushed,
                )
            except IllegalTransition as exc:
                # 这一支**不** `set_rollback`（搬迁前逐字如此）：`apply_transition` 在写任何东西
                # 之前就抛它（边 / 节点不足、复读发现陈旧）⇒ 没有半笔可回滚。
                return _fail(ErrorCode.BUSINESS, str(exc), exc=exc)
        # 特例 4：缺行就在这里补（事务内、迁移成功之后——顺序承重，见 docstring）。
        if perf is None:
            perf = ensure_main_performance_of(order)
        # ⭐⭐ Z1：上面的 perf.is_settled 是判定、下面是写入（reset_taker_performance / set_taker）、两段之间不持锁 ⇒ 并发结账能把钱结掉、这边照指派/改派（TOCTOU）。修法照 /adjust-amount/ 已修形状（P1-10）：对绩效行 select_for_update(of=("self",)) 重取并复查 is_settled；被拒时 transaction.set_rollback(True) 再 return（E-0022）。
        # ⚠️ 位置刻意排在 apply_transition / 补行之后：那一步已经拿了订单行锁（apply_transition 的 Order.objects.select_for_update()，批量侧则由 run_batch 先锁整批订单）⇒ 本函数对「订单 → 绩效」这一条边的锁序不翻向、与 Z2「先锁 Order、再锁钱包」一致；放在事务开头会变成「绩效 → 订单」、正好与抢单侧相反（claim × assign 的 AB-BA 又会回来）。
        # ⚠️ 已结账那一档在 apply_transition 里还有一道 D4b 闸先拦；本条守的是「订单已经是 IN_PROGRESS、跳过迁移」那一档（那里没有 D4b）；of=("self",) 只锁绩效行自己（过滤没有 join；显式写是为了与其余四处同形）；.order_by("id") 单行也用同一条形状、批量侧扩写时锁序不漂；复查用加锁后读回来的那一份（locked_perf）⛔ 不看锁前的 perf。
        locked_perf = (
            Performance.objects.select_for_update(of=("self",))
            .order_by("id")
            .get(pk=perf.pk)
        )
        if locked_perf.is_settled:
            transaction.set_rollback(True)
            return _fail(
                ErrorCode.PERFORMANCE_STATE_INVALID,
                "该单的接单人绩效已结账，不能改派；请先撤销结账再指派",
            )
        perf = locked_perf
        # ⚠️ **审计前值取加锁后读回的那一份**（I1）：`apply_transition` 锁的是 Order 行，
        #    但它只 `only("status")` 复读、**不刷新**调用方手里这个 `order` 实例 ⇒ 并发
        #    改价后 `old_price` 会是锁前缓存（审计里写一句假话、`is_reassign` 也会误判）。
        order.refresh_from_db(fields=["hall_price"])
        # ⚠️ 把刚改过的 `order` 挂到绩效行上：`effective_taker_price` 读的是
        #    `performance.order.hall_price`，不挂就会读到缓存的旧价。
        perf.order = order
        old_price = order.hall_price
        # ⭐ D-0369（业务方口径）：指派（含改派）也写发单人——谁把单指派出去，发单人就是谁；保留最后一次。**直写**、⛔ 不走 `apply_transition` 的 `hall_publisher` 漏斗参数，两条理由见本函数 docstring 的「`hall_publisher` 直写点」一节（漏斗会提前写掉 `published_at`、打红「逐微秒相等」那条钉子；改派支根本没有漏斗调用可传）⇒ 迁移支与 R-3 跳过支由这同一个直写点统一覆盖。
        # ⚠️ 判据 `is not None`（E-0021 铁律）：机器 / 开放平台路径造的 ctx `user=None` 天然不写（负向钉子： `tests/test_order_writes_core.py::AssignCoreTests::test_machine_context_does_not_touch_the_publisher`）。 ⚠️ **位置为什么安全**：① 在迁移支 try/except 与已结账复查**之后**—— 那两处被拒时本句还没执行，被拒的指派一行不写；② 在 `set_taker` **之前**
        # ——它失败时上面的 `transaction.set_rollback(True)` 把这一笔**一起回滚** （留在这里的事务里，不是独立事务）；③ 紧邻上方的 `refresh_from_db(fields=["hall_price"])` **只刷 `hall_price` 一列** （窄列刷新，不带别的列回来），既不会冲掉本句写的 `hall_publisher`、也不被本句影响——把本句排在它**之后**，二者之间没有任何东西可互相覆盖。 ⚠️ E-0027 回查：`hall_publisher` 没有回退 / 清理 / 重置路径（撤回 / 退款 / 回池都保留，既有钉子在 `tests/test_order_column_fields.py`）⇒ 本写入点不新增清理义务；`stamp_local_action` 不适用（它盖的是「上家事件会覆盖」那组列的本地动作时刻，发单人不在其中）。
        if ctx.user is not None:
            order.hall_publisher = ctx.user
            order.save(update_fields=["hall_publisher", "updated_at"])
        was_assigned_to_taker = perf.user_id == taker.pk and perf.deleted_at is None
        # 特例 3：复活软删的 MAIN 行（同一行清两列，不新建）。
        if perf.deleted_at is not None:
            perf.deleted_at = None
            perf.deleted_by = ""
            perf.save(update_fields=["deleted_at", "deleted_by", "updated_at"])
        # 特例 2（R-2b）：真正换人才回退七列；同一个人保存 ⇒ 跳过，保住他自己的覆盖价。
        # 真正换人时，旧接手人必须在 reset_taker_performance 清掉 user 之前取到。
        # 同一个人保存不进这条分支，不写撤单。
        old_taker = None
        if perf.user_id is not None and perf.user_id != taker.id:
            old_taker = perf.user
            reset_taker_performance(perf)
        if price is not None:
            # 指派时的第一次绩效计算也需要使用这次填入的发单价。
            order.hall_price = price
            order.save(update_fields=["hall_price", "updated_at"])
        try:
            # 内部按这个人的提成重算 `temp_amount`；FX2 那一档允许「人先记、金额等补价」。
            set_taker(perf, taker, allow_missing_price=allow_missing_price)
        except (CommissionError, _AmountError) as exc:
            # 显式回滚：此刻状态已经改过（`apply_transition` 在上面），块内 `return` 会提交它（E-0022）。
            transaction.set_rollback(True)
            return _fail(exc.code, exc.message, exc=exc)
        # 三个可选键：只在**带值**时覆盖（`is not None`，E-0021：`fee=0` 是合法边界）。
        touched = []
        if fee is not None:
            write_performance_amounts(perf, temp_amount=fee)
            touched.append("temp_amount")
        if hours is not None:
            perf.hours = hours
            touched.append("hours")
        if project is not None:
            perf.project = project
            touched.append("project")
        if touched:
            perf.save(update_fields=[*touched, "updated_at"])
            # ⭐（客服预计绩效线，计划 §2 T1.2 的显式挂点 10）：显式 fee 直写 temp_amount 排在 set_taker → recompute_order_amounts 之后 ⇒ 主挂点读到的是 set_taker 算出的那个值、不是这次 fee；带 fee 且不带 price 时（下面那一支不进）它就是最终值 ⇒ 必须在这里补刷客服预计。
            # ⚠️ 只在 MAIN 行上调（本处 perf 由 ensure_main_performance_of 取得、本来就是 MAIN；判据照主挂点写、避免「将来有人改取行口径」时静默漂移）；⚠️ 与上面那三个可选键无关的 touched（只有 hours / project）也会走到这里：refresh_customer_estimate 对「值没变」不写库（判据 !=）⇒ 纯读、无副作用。
            if str(perf.emp_type) == str(EmpType.MAIN):
                refresh_customer_estimate(perf.order)
        if price is not None:
            stamp_local_action(order)
            # 同传发单价和绩效金额时，沿用指派后按发单价重算的口径。
            if fee is not None:
                try:
                    recompute_order_amounts(perf)
                except (CommissionError, _AmountError) as exc:
                    transaction.set_rollback(True)
                    return _fail(exc.code, exc.message, exc=exc)
            # 发单价变了 ⇒ 记一行下行 order.update。写在写价和重算之后，载荷取已落库的价。
            if ctx.sync_peer_orders:
                notify_order_price_changed(order)
            # ⭐ T3：**同一处**把价跟到下游鸡蛋铺那条线（第三方下家）——
            #    与上一句不是同一条线（那一句是团团通租户 ↔ 团团通租户，D-0152）。
            if ctx.sync_personal_orders:
                jidanpu_member.queue_price_update(order)
        # 指派 vs 重新指派（spec §5.2）：判据是**发单价真的变了**（换人本身不算 reassign）。
        is_reassign = old_price != order.hall_price
        action = "performance.reassign" if is_reassign else "performance.assign"
        record(
            log_type=ACTION_LOG_TYPES[action],
            action=action,
            actor=ctx.log_actor,
            actor_id=ctx.log_actor_id,
            # ⭐ 同上面几处：`log_actor_id` 默认 `None` ⇒ 显式声明机器来源。
            system=ctx.log_actor_id is None,
            target_kind="user",
            target_id=taker.id,
            # ⭐（订单日志人名线 spec §3-A2）：人名落账号名称（昵称优先、空串回落登录账号；唯一出处 apps/accounts/display.py::user_display_name）。
            # ⛔ 不再写 taker.username——那让正文里的人名显示登录账号（现场观察到正文写成「指派给 <登录账号>」、而那本该是昵称）；⚠️ 这里 ⛔ 不留真实号码/账号：本仓有前端 PII 守卫（pii-guard.spec.ts）会扫 src/**、写实例值会把门禁弄红。
            # ⚠️ 落的是写入那一刻的显示名快照（改昵称后历史行不跟着变，spec §6-1）。
            target_name=user_display_name(taker),
            order_no=order.order_no,
            from_value=old_price if is_reassign else "",
            to_value=order.hall_price,
        )
        if ctx.notify_worker:
            from apps.business.wxpusher_events import queue_order_notice, queue_order_status_notice

            if was_assigned_to_taker:
                queue_order_status_notice(order, previous_status)
            else:
                queue_order_notice(order, taker.pk, "assigned", perf.claimed_at, performance=perf)
        # 换人给旧接手人记一条撤单。同人不写。必须在失败回滚之后，到得了这里说明指派已成功。
        # 必须显式传 occurred_at=timezone.now()：「订单已是 IN_PROGRESS」那一档会跳过 apply_transition、且没带 price 时一次 order.save() 都不发生 ⇒ 入队函数的默认值（order.updated_at）还是上一次订单写入的时刻、与下游 PeerOrderLink.last_applied_at（由先前那条 order.assign 写下）相等 ⇒ 接收侧按「相等 = 重复」判 stale 跳过、下家永远不退单（与 /delete/ 那条同一个理由与同一个写法）。
        #    ⚠️ D-0369 起「一次 `order.save()` 都不发生」只剩机器路径（`user=None`）成立：人工 ctx（`user` 非 None）的指派 / 改派不带价也会在指派直写点 save 一次（`hall_publisher` + `updated_at`）——显式传时刻的判据照旧不变。
        if old_taker is not None:
            if ctx.sync_peer_orders:
                enqueue_order_withdrawn(
                    order, old_taker, "taker_removed", occurred_at=timezone.now()
                )
            # ⭐⭐ 打手侧回调线（T3，spec §4.3 那张表的第 8 行）：同一个位置（⛔ 不新造第二个漏斗）、收件人是前任接手人（spec §4.2 末段）——本处已有的局部变量 old_taker 就是"改之前那个人"（新接手人是 taker）⇒ 直接把它传进去 ⛔ 不另查一次库去"猜前任"；这一支是 assign_order 的真换人支（old_taker is None 的 "A→A / 首次指派" 一支一笔都不写、与左边那条既有事件同口径）；reason 沿用左边那条的取值（taker_removed）。
            # ⚠️ 时刻同样取 timezone.now()（理由与左边那句逐字相同：本档可能一次 order.save() 都不发生、order.updated_at 会是上一次写入的时刻）。
            #    ⚠️ D-0369 起「可能一次都不发生」只剩机器路径（`user=None`）：人工 ctx不带价也会在指派直写点 save 推 `updated_at`——显式传时刻照旧不变。
            from apps.takerapi.api import callback_recipients as taker_callback

            taker_callback.notify_order_reassigned_for_taker(
                order,
                former_taker_id=old_taker.pk,
                reason="taker_removed",
                occurred_at=timezone.now(),
            )
        from apps.internal_chat.order_groups import assignment_changed

        assignment_changed(
            order, taker, follower=ctx.user if chat_follow_up else None, former_worker=old_taker,
        )
        # 同事务登记直连指派和下家鸡蛋铺副本同步。
        # ⛔ occurred_at 必须显式给：本档写价时上面已经先入队了一条 order.update、两条事件同一次请求、共用默认值 order.updated_at（这中间的各次写入都不推 Order 行）⇒ worker 按 created_at 最老优先 ⇒ 下家先应用那条 order.update（D-0377 起它自己也写客户价与内容面，丢不掉的只有「接手事实」那一半）、随后这条 order.assign 命中接收侧 <= 的 stale 闸被良性跳过 ⇒ 接手事实（落「未分配」）与其余白名单静默丢掉、而对账水位正是同一个 last_applied_at、自愈也补不回来 ⇒ 给这条更全的事件一个严格更晚的时刻（不带价时只晚几毫秒、无副作用）。
        if ctx.sync_peer_orders:
            enqueue_order_assigned(order, taker, occurred_at=timezone.now())
        # 指派成功后，同事务给这个代练员的个人绑定排 member.assign（只录单，不抛单）。
        # 没有或停用绑定时 queue_assign 回 0，那类单靠启用时的补推。失败回滚走不到这里。
        if ctx.sync_personal_orders:
            jidanpu_member.queue_assign(order, taker_id=taker.id)
            if was_in_hall:
                jidanpu_member.queue_withdraw(order, hall_only=True, summarize=True)
    return _ok(order=order)


def delete_order_write(order, *, is_del_emp, ctx, dry_run, reason="") -> WriteOutcome:
    """预检与执行使用同一套删单金额校验。"""
    if dry_run:
        try:
            validate_soft_delete(order, ctx.user, assert_game=ctx.assert_game)
        except ValidationProblem as problem:
            return _fail(problem.code, str(problem))
        return _ok(order=order)
    # 删单时接手人必须在 soft_delete_order 之前取到，入队在删除之后。
    #    ⚠️ 用**容错**查询而不是 `main_performance_of`：缺 MAIN 绩效行是真实数据形态
    #    （不经录单端点落库的旧单 / ORM 夹具），而 `main_performance_of` 缺行时**响亮抛**⇒ 本端点会变成 500（与 `/clear-taker/` 取原接单人时的口径一致）。
    perf = Performance.objects.filter(order=order, emp_type=EmpType.MAIN).first()
    released_taker = perf.user if perf is not None else None
    try:
        soft_delete_order(
            order=order, actor=ctx.user, is_del_emp=is_del_emp, reason=reason,
            actor_name=ctx.log_actor if ctx.user is None else None,
            recheck_external_refunds=ctx.recheck_external_refunds,
            sync_personal_orders=ctx.sync_personal_orders,
            sync_peer_orders=ctx.sync_peer_orders,
            assert_game=ctx.assert_game,
        )
    except ValidationProblem as problem:
        return _fail(problem.code, str(problem))
    # ⚠️ 入队排在删除动作之后（同事务）、且显式把「删除时刻」传给入队函数：soft_delete_order 的 update_fields 只有 deleted_at / deleted_by、不推 Order.updated_at
    # ⇒ 若用入队函数默认的 order.updated_at、这条撤单事件会带上上一次订单写入的时刻、与下游已经应用过的事件相等或更旧 ⇒ 可能被按 stale 静默丢掉。
    # ⛔ 为此不改 soft_delete_order 的语义（越界）。
    if released_taker is not None:
        if ctx.sync_peer_orders:
            enqueue_order_withdrawn(
                order, released_taker, "deleted", occurred_at=timezone.now()
            )
        # ⭐⭐ 打手侧回调线（T3，spec §4.3 那张表的第 9 行前半）：同一个位置、收件人是前任接手人（spec §4.2 末段）——本处已有的局部变量 released_taker（上面在 soft_delete_order 之前取到的）就是它。
        # ⚠️ ⛔ 不能改成"现查一次 current_taker_id"：soft_delete_order 已经把这张单软删了、而且本处的语义是"删单那一刻这单归谁"（前任）、不是"现在归谁"（同一个坑，spec §4.2 末段逐字点名）；reason="deleted" 沿用左边那条的取值。
        # ⚠️ 同一张单被删时、recycle.soft_delete_order 那边还会推一条 order.deleted（"这单不存在了"）——两条有意同时推（spec §4.1 末段：对外语义不同）⛔ 不是重复。
        from apps.takerapi.api import callback_recipients as taker_callback

        taker_callback.notify_order_reassigned_for_taker(
            order,
            former_taker_id=released_taker.pk,
            reason="deleted",
            occurred_at=timezone.now(),
        )
    return _ok(order=order)


def refund_total_write(order, *, refund_total, reason, request_id, ctx, dry_run, allow_external_refund=False) -> WriteOutcome:
    """把订单的累计退款额设成 refund_total（绝对值；D-0312）。⚠️ 逐笔判据与单笔同一批实现（编排方 Ruling 10 的抽取点 ⛔ 不是复制一份）：①status != COMPLETED(1007) ②refund_total < 0(1008) ③refund_total > 毛收款(1008) ⇒ refunds.refund_total_denial(order, refund_total)；④客服行 commission_rate 为空(1009) ⇒ refunds.assert_sales_commissions_configured(order)（同一条判据也在 refund_total_denial 与 _apply_sales_deductions 里跑、是同一份实现的多次调用）；⑤request_id 撞具名唯一约束(1010) ⇒ 由 refunds._write_refund_total 翻译。
    ⚠️ 幂等冲突只在真写路径上判（如实登记）：dry_run=True 不做第 1 步的 INSERT ⇒ 自然不会撞 uniq_order_refund_request_id；也就是说 dry_run 判不出「这个 request_id 已经用过了」——那一笔会在 dry_run 里显示合格、到 dry_run=False 时才回 1010；这与单笔的「先便宜、后打数据库」是同一条口径（refunds.py 的「第 5 条为什么不做 exists() 预查」）：加一次预查就会让「临时去掉那处唯一约束 ⇒ 测试必须变红」那条判别力实验失效；⚠️ 差额 = 0 也不 INSERT ⇒ 同一 request_id 在「设成同值」时重提不会回 1010。
    ⚠️ dry_run=True ⇒ 零写入：不建 OrderRefund 行、不改 refund_fee、不建 PerformanceAdjustment、不写 order.refund 留痕——由这里显式提前返回保证（⛔ 不依赖 _write_refund_total 去判 dry_run：那样这个写函数就有两条语义）；dry_run 的详情键从旧的 amount 改成 refund_total（与新入参对齐）。⚠️ detail["record"] = 新建的 OrderRefund 行（真写且差额非 0 时才有）；差额为 0 时 record is None——视图必须显式分支（回 {order_no, refund_fee, changed: false}）⛔ 不能无条件 RefundSerializer(detail["record"])（那会 500）；失败时 detail["exc"] 放原异常（RefundError、与退款各步同形）；⚠️ 写入函数可能抛未预期的异常（DB 写坏等）：本函数只接 RefundError、其余照旧向上冒（E-0010：预期失败翻译、未预期的不伪装）；⚠️ actor 取 ctx.log_actor（认证态用户名）、actor_id 取 ctx.log_actor_id（R20 / spec §5.9）⛔ 不读 ctx.actor（请求体值可伪造）。"""
    try:
        if dry_run:
            # `dry_run` 也要跑完**全部**判据（spec §D5）⇒ 判据里已含提成率闸。
            refund_total = refund_total_denial(order, refund_total, allow_external_refund=allow_external_refund)
            return _ok(order=order, refund_total=refund_total)
        # ⚠️ 局部名**不能**叫 `record`：那会遮蔽模块级 `from apps.audit.log import record`
        # ⇒ 本函数里将来任何一次 `record(...)` 都会变成 `UnboundLocalError`。
        refund_row = _write_refund_total(
            order,
            refund_total,
            reason,
            request_id,
            ctx.log_actor,
            actor_id=ctx.log_actor_id,
            allow_external_refund=allow_external_refund,
        )
    except RefundError as exc:
        # 预期失败：码与 msg 直接进信封（与单笔的 `except RefundError` 出口同形）。
        return _fail(exc.code, exc.message, exc=exc)
    return _ok(order=order, record=refund_row)


def recompute_customer_commission_write(order, *, ctx, dry_run) -> WriteOutcome:
    """重算客服销售提成账单（spec §D15；本批唯一的新能力、无单笔对应物）。⛔ 不碰打手那一侧（spec §D15 的「不做」清单）：不补建 MAIN 行、不重算打手 bonus_amount、不改订单状态、不改 sales_user；本函数只读 MAIN 行（算利润的减项、在 settlement_hooks.recompute_customer_commission 里）、只写 SALES 行 + 留痕。
    逐笔判据（spec §D15 第 1–6 条）：①order.sales_user_id is None ⇒ 1002（「该单没有客服归属人，无法生成客服绩效账单」）；②该单 SALES 行 is_settled（Performance.is_settled 三字段并集）⇒ 1014（与既有已结账闸同族：⛔ 不许直接改已结账的钱）；⭐ Z1 起事务内还会对这条 SALES 行 select_for_update(of=("self",)) 重取并再判一次（并发结账的 TOCTOU）、被拒时 set_rollback(True) 再 return；③SALES 行缺失 ⇒ 补建——复用 performance.create_customer_performance（录单那条路径的同一函数 ⛔ 不另写一套建行逻辑）；④订单已完成 ⇒ 复用 recompute_customer_commission（dry_run=False）写 bonus_amount / commission_rate / review_status（与完成时同一套算式）；⑤订单不是已完成 ⇒ 不写金额、只保证行存在（那一档写在 settlement_hooks.recompute_customer_commission 里、见它的 docstring）；⑥CommissionError（提成率算不出）⇒ 逐笔不合格 1000、msg 用异常原文（⛔ 不是静默跳过：这里提成本身就是产物、算不出就没有产物可言）。①/②（以及 dry_run 分支的 ⑥）在写库之前判；真写路径的 ③/④/⑤/⑥ 在 transaction.atomic() 里。
    dry_run=True 的判定面（spec §4.3 / §9-R8~R10 的同一口径）：只跑「纯内存/纯读」的闸（①+②+⑥）⇒ 零副作用：不建行、不写金额、不写日志；⛔ 不试算提成金额（那是「写」那一步的事）：recompute_customer_commission(dry_run=True) 只回答「该不该算、算得出吗」、返回前不调 _persist_customer_commission。留痕与事务（spec §D7/§D4/E-0022）：真写路径在 transaction.atomic() 里——补建 + 写金额 + 一条 record(...) 同事务；CommissionError ⇒ 先 set_rollback(True) 再 return（atomic() 是事务不是回滚点、块内正常 return 会把补建的那一行提交掉 ⇒ 留下「行建了、钱没写」的脏现场）；⛔ record(...) 的参数取 ctx.log_actor / ctx.log_actor_id（认证态，R20）：留痕的操作人不可伪造。⚠️ 幂等：同一笔重复点「重算」⇒ 结果相同（_persist_customer_commission 是覆盖写同一行、不是 create）、第二次仍写一条留痕（与 on_order_completed 的口径一致、spec §D15 明写不加「同值不写日志」的特判）。"""
    if order.sales_user_id is None:
        return _fail(ErrorCode.BUSINESS, "该单没有客服归属人，无法生成客服绩效账单")
    customer = Performance.objects.filter(
        order=order, emp_type=EmpType.SALES, order__deleted_at__isnull=True
    ).first()
    if customer is not None and customer.is_settled:
        return _fail(
            ErrorCode.PERFORMANCE_STATE_INVALID,
            "该单的客服提成已结账，不能重算；请先撤销结账",
        )
    if dry_run:
        # ⑥ 的预检：`dry_run` 也要跑完「算得出吗」那道闸（spec §D5）⇒ 让它抛、翻成逐笔 1000。
        #    ⚠️ 这一支**不建行、不写金额**（`dry_run=True` 在钩子那侧也到不了 `_persist_...`）。
        try:
            recompute_customer_commission(order, dry_run=True)
        except CommissionError as exc:
            return _fail(ErrorCode.VALIDATION, str(exc))
        return _ok(order=order)
    with transaction.atomic():
        # ⭐⭐ Z1：上面②是判定、下面是写入、两段之间不持锁 ⇒ 并发结账能把这条 SALES 行结掉、这边照重算（TOCTOU）。修法照 /adjust-amount/ 已修形状（P1-10）：事务内对 SALES 绩效行 select_for_update(of=("self",)) 重取并复查 is_settled；
        # of=("self",) 只锁 SALES 行自己（这一步按 pk 取、不带 order__ join）；.order_by("id") 单行也用同一条形状、与其余五处一致；复查用加锁后读回来的那一份（locked_customer）⛔ 不看锁前的 customer；被拒时 transaction.set_rollback(True) 再 return（E-0022）。
        # ⚠️ 行不存在（customer is None）⇒ 锁到 0 行；补建与并发建行撞唯一约束那一档不在本函数（它由 run_batch 的订单行锁串行化同一单的批量动作）。
        locked_customer = None
        if customer is not None:
            locked_customer = (
                Performance.objects.select_for_update(of=("self",))
                .order_by("id")
                .get(pk=customer.pk)
            )
        if locked_customer is not None and locked_customer.online_payroll_batch_id and not locked_customer.is_settled:
            return _fail(ErrorCode.PERFORMANCE_STATE_INVALID, "客服报酬已在在线付款单中，暂时不能重算")
        if locked_customer is not None and locked_customer.is_settled:
            transaction.set_rollback(True)
            return _fail(
                ErrorCode.PERFORMANCE_STATE_INVALID,
                "该单的客服提成已结账，不能重算；请先撤销结账",
            )
        created = locked_customer is None
        if created:
            # ③ 补建（与录单同一个函数）。① 已挡掉 `sales_user_id is None` ⇒ 它不会返回 `None`。
            create_customer_performance(order)
        try:
            # ④/⑤ 都在这一个函数里；⑥ 由它抛给这里翻码。
            recompute_customer_commission(order, dry_run=False)
        except CommissionError as exc:
            # 预期失败：先标记回滚（补建那一行必须一起消失，E-0022），再 return 业务码。
            transaction.set_rollback(True)
            return _fail(ErrorCode.VALIDATION, str(exc))
        # ⚠️ field_name 只在真的写了这一列时才带。bonus_amount 在这条路径上有两个写入点：③补建（create_customer_performance 写初始 0.00）与④完成态的 _persist_customer_commission（覆盖写）；
        # 而「未完成 + 行已存在」那一档（§D15 第 5 条）是纯 no-op：既不建行、也不写金额 ⇒ 给它带 field_name 等于让结构化列说一句没发生的事（与「借一个语义不同的审计词」同族，D15 末段）。
        # ⛔ 日志行本身照写：spec §D15 明写不加「同值不写日志」的特判、改的只是这个列。
        wrote_amount = created or str(order.status) == str(OrderStatus.COMPLETED)
        record(
            log_type=ACTION_LOG_TYPES["performance.recompute_customer_commission"],
            action="performance.recompute_customer_commission",
            actor=ctx.log_actor,
            actor_id=ctx.log_actor_id,
            # ⭐ 同上面几处：`log_actor_id` 默认 `None` ⇒ 显式声明机器来源。
            system=ctx.log_actor_id is None,
            target_kind="order",
            target_id=order.order_no,
            order_no=order.order_no,
            field_name="bonus_amount" if wrote_amount else "",
        )
    return _ok(order=order)


def resolve_sales_agent_candidate(*, user_id, viewer, order=None):
    """把「换销售客服」的目标用户解析出来；不合格时回一句能分辨的错误。这是什么（业务方拍板的新能力）：需求原话是「换销售客服 = 提成也换人」——把 Order.sales_user（决定客服提成归谁的 FK）连同那张 SALES 绩效行一起搬到新人、金额按新人的费率立刻重算（订单未完成则按既有口径不写金额，见 change_sales_agent）；⚠️ 对标没有这个能力：鸡蛋铺只有订单列表页一颗手动的「重算客服提成」按钮、而且它不改归属人（只重算同一个人头上的金额）⇒「换人 + 换提成」是我方新造的；本能力与它的取舍由业务方拍板、决策登记见 docs/DECISIONS.md。
    三条判据（顺序承重 ⛔ 别调换）：①用户不存在 ⇒ 1000「指定的用户不存在」（与 /assign/ 的 _resolve_assign_taker 同款文案）；②不在候选里 ⇒ 1000——候选口径同源 selectors.kefu_candidates_qs（带 can_be_sales_agent 标记的角色的人 ∪ 当前登录人）⛔ 不在这里另写一套判据；③这个人在本单上已经有绩效行（任意 emp_type）⇒ 1000 + 一句指名道姓的提示——③的机制理由（不是风格问题）：Performance 的唯一约束 uniq_performance_order_user = UniqueConstraint(fields=["order", "user"])（apps/business/models.py，不含 emp_type）⇒ 把 SALES 行的 user 直接改成「本单已经有一条 MAIN 行的那个人」会撞约束、抛 IntegrityError ⇒ HTTP 500、所以这条闸必须在写之前判（E-0010：预期失败要翻译成业务码、不许响亮 500）。
    返回 (user, display_name, error)：合格 ⇒ (User 实例、该用户显示名、None)；不合格 ⇒ (None, "", WriteOutcome)——调用方直接 return 那个 outcome；⚠️ 显示名在这里顺带解析出来、正是为了「名字与归属不分叉」：名字来自这一行的同一个查询、调用方没有机会从别处（例如请求体）拿一个名字。⚠️ 两个来源与 kefu_options 逐字同源：候选集合 = kefu_candidates_qs；显示名 = display_name_expression(prefix="") 的 Python 侧对应物 apps.accounts.display.user_display_name（first_name or username）；用 values() 把注解和用户行一次取回——user_display_name 也是同一条口径、两者是「同一条口径的 SQL / Python 两种表达」（那条注释在 selectors.py 里写明：不许各自漂移）。⚠️ viewer 是必填的：它不只是拿来判「是不是我自己」、它就是候选集合的一半（∪ 当前登录人）⇒ 漏传会让「我是客服但角色没打标记」这一档被误拒。⚠️ order 可以不传（默认 None）：录单那一路用它（单还没建、判据③天然不成立）；两个调用点、一份判据——⛔ 别在录单那一路另写一套「候选范围」的过滤条件（那正是 E-0021 说的「同一事实两套判据」）；判别用例：tests/test_order_sales_agent.py 的录单三档 + 端点那几条。"""
    candidates = kefu_candidates_qs(viewer=viewer)
    row = (
        candidates.filter(pk=user_id)
        .annotate(display_name=display_name_expression(prefix=""))
        .values("pk", "display_name")
        .first()
    )
    if row is None:
        # ① 与 ② 合成一个出口：**对调用方而言是同一件事**（这个 id 用不了），
        #    而 `candidates` 已经按数据权限收过（`list_accounts` 是租户内、未软删）
        #    ⇒ 「不存在」与「不在候选里」在这里无从区分、也不该区分（区分会让候选名单变成一个可枚举租户账号的旁路）。
        if not User.objects.filter(pk=user_id).exists():
            return None, "", _fail(ErrorCode.VALIDATION, "指定的用户不存在")
        return None, "", _fail(ErrorCode.VALIDATION, "该用户不在销售客服候选范围内")
    # ③ 已有绩效行（任意 emp_type）⇒ 撞唯一约束，必须在写之前拒。
    #    ⚠️ `order=None`（**录单那一路**：单还没建、必然没有绩效行）⇒ 这一条**天然不成立**
    #    ⇒ 直接跳过，⛔ 不去查一个不存在的订单（`filter(order=None)` 在 ORM 上会变成`order_id IS NULL` 的查询，读起来像「查了」，其实是在问一个恒为空的问题）。
    if order is not None and Performance.objects.filter(order=order, user_id=user_id).exists():
        return (
            None,
            "",
            _fail(
                ErrorCode.VALIDATION,
                f"{row['display_name']}已是这单的接单人，不能同时作为销售客服",
            ),
        )
    user = User.objects.get(pk=row["pk"])
    return user, row["display_name"], None


def change_sales_agent(order, *, target, target_name, ctx, dry_run) -> WriteOutcome:
    """把这一单的销售客服归属换成 target（业务方拍板的新能力）。一个事务里做三件事 + 一条留痕：1. Order.sales_user = target；2. 该单的 SALES 绩效行 user 改成 target、行不存在就补建（复用 performance.create_customer_performance ⛔ 不另写一套建行逻辑）；3. Order.sales_agent（文本快照）同步成 target 的显示名——口径与候选/录单兜底同源（target_name 由 resolve_sales_agent_candidate 从同一个查询里取出 ⛔ 不接受调用方另传一个名字）；4. 调 settlement_hooks.recompute_customer_commission(order, dry_run=False) 重算金额——这一句就是「立刻按新人重算」的全部实现：它自己保证「订单不是已完成 ⇒ 不写金额」「没有 SALES 行 / sales_user 为空 ⇒ 早退」⛔ 不要在这里再写一套金额逻辑。
    调用方必须先过两道闸（本函数不再判它们）：已结账（该单 SALES 行 Performance.is_settled）⇒ 拒绝——判据在动作体里、在任何写入之前判，放进来判太晚了：dry_run=False 时本函数一进来就已经在 transaction.atomic() 里改 sales_user 了、再拒会留下半成品（E-0022 家族）；目标不在候选 / 已是本单接单人 / 不存在 ⇒ 由 resolve_sales_agent_candidate 判；⛔ 别在本函数里「顺手补一道」——同一事实两套判据必然漂移（E-0021）。
    幂等（目标 == 现有归属人）：调用方（动作体）在过了「已结账」闸之后、改任何东西之前判它、命中就直接成功返回；⚠️ 那时它没有调本函数：dry_run=False 下本函数一定会写一条 sales_user 的 order.update 留痕、而「把 A 改成 A」不是一次变更（与 /price/ 的 R27.2「同值不留痕」同口径）；判别用例见 tests/test_order_sales_agent.py；⭐（F4.3）：那一支仍不写留痕、但不止「什么都不做」——它调 sync_sales_agent_name 把 sales_agent 对齐回归属人的显示名（不变式修复）、「没有发生变更」这件事因此仍然成立（日志只记「换了人」）。dry_run：本能力没有批量对应物（一次只能换一单）、dry_run=True 不是给批量用的；保留它只为与同族核心函数同形（dry_run=True ⇒ 只校验、零写入零留痕）。"""
    old_user = order.sales_user
    old_name = "" if old_user is None else user_display_name(old_user)
    # ⚠️ **日志与落库用同一个裁剪后的值**（S3）：`Order.sales_agent` 是 `varchar(64)`，
    #    而显示名上限 150 ⇒ 不裁的话「正文里说的名字」与「库里那一列的名字」分叉
    #    （超长时前者是原文、后者是前 64 个字），而这正是本线要消掉的那类分叉。`old_name` 一侧同理：它读的是**别人**的显示名，而库里存的是当年裁过的值。
    clipped_target_name = clip_sales_agent(target_name)
    clipped_old_name = clip_sales_agent(old_name)
    if dry_run:
        return _ok(order=order, old_name=old_name, new_name=target_name)
    with transaction.atomic():
        # ⚠️ 行锁（S4）：先锁住这张单的订单行与它的 SALES 行、再改。它挡的是「本动作与结账动作并发」那一档（TOCTOU：结账读「未结账」、本动作读「未结账」⇒ 两边都成功）。
        # ⚠️ 这只缩小窗口、不是全局互斥：结账那一侧（settlement.settle_performances）今天没有 select_for_update、所以反过来（本动作先提交、结账后读）仍然要靠那道「已结账 ⇒ 拒绝」的闸。
        # ⛔ 不在这里重判「已结账」：同一事实两套判据必然漂移（E-0021）、那道闸在动作体里。
        Order.objects.select_for_update().filter(pk=order.pk).first()
        order.sales_user = target
        order.sales_agent = clipped_target_name
        order.updated_by = ctx.actor
        order.save(update_fields=["sales_user", "sales_agent", "updated_by", "updated_at"])
        customer = (
            Performance.objects.select_for_update()
            .filter(order=order, emp_type=EmpType.SALES, order__deleted_at__isnull=True)
            .first()
        )
        if customer is not None and customer.online_payroll_batch_id and not customer.is_settled:
            transaction.set_rollback(True)
            return _fail(ErrorCode.PERFORMANCE_STATE_INVALID, "客服报酬已在在线付款单中，暂时不能改归属")
        if customer is None:
            # ③ 补建（与录单同一个函数）。⛔ 不复用 `recompute_customer_commission_write`：
            #    那一个函数是**批量端点**的动作体（它自己再判一遍已结账、自己写一条
            #    `performance.recompute_customer_commission` 留痕）⇒ 在这里调它会写出**两条**动作码的日志，而本动作只发生了一件事（换归属）。
            create_customer_performance(order)
        else:
            customer.user = target
            customer.save(update_fields=["user", "updated_at"])
        # 「立刻按新人重算」的**唯一实现**（含「未完成 ⇒ 不写金额」那一档，见函数 docstring）。
        try:
            recompute_customer_commission(order, dry_run=False)
        except CommissionError as exc:
            # ⭐ S1：提成率算不出（费率配置缺失 / 模式不匹配一类）是可预期的失败、翻成业务码；⛔ 先 set_rollback(True) 再 return——atomic() 是事务不是回滚点、块内正常 return 会把上面那几笔写入提交掉（「归属换了、名字也换了、钱没算」的半成品，E-0022）；⛔ 未预期异常不接、仍然响亮 500（E-0010）。
            # 先例：本模块 recompute_customer_commission_write 的同一个 except。
            transaction.set_rollback(True)
            return _fail(ErrorCode.VALIDATION, str(exc))
        record(
            log_type=ACTION_LOG_TYPES["order.change_sales_agent"],
            action="order.change_sales_agent",
            actor=ctx.log_actor,
            actor_id=ctx.log_actor_id,
            system=ctx.log_actor_id is None,
            target_kind="order",
            target_id=order.order_no,
            order_no=order.order_no,
            field_name="sales_agent",
            # 前后值都是显示名（人读的那一列）、且都是裁剪后的值（S3）。
            # ⚠️ 空归属人由写入侧兜底成「（无）」：模板 ORDER_CHANGE_SALES_AGENT 里没有、也不该有「未设置」这层兜底（模板的纪律是「渲染输入只来自行内列」、apps/audit/log.py 的模块 docstring）⇒ 否则正文会渲染成病句「…从改为Y」。
            # ⚠️ 用 or 而不是 is not None 判空是刻意的、也只在这里：这里问的是「这一列有没有一个可读的人名」、空串与 None 是同一件事（clip_sales_agent 已经把 None 归一成空串）⇒ E-0021 那条「边界值用 is not None」管的是数值/布尔列、人名没有「合法的空值」这一档。
            from_value=clipped_old_name or "（无）",
            to_value=clipped_target_name,
        )
    return _ok(order=order, old_name=old_name, new_name=target_name)


def sync_sales_agent_name(order, *, actor: str) -> bool:
    """把 Order.sales_agent（显示名快照）对齐到 Order.sales_user 的显示名——不变式的修复点。
    它守的是什么：Order.sales_agent 是展示列（订单列表「销售客服」那一格读它）、Order.sales_user 是归属人（客服提成归谁）；两者必须指向同一个人：名字是甲、归属是乙 ⇒ 列表显示乙、钱归甲、而页面上没有任何信号。写侧的两个入口（change_sales_agent 与录单）都从同一个人身上取两列的值 ⇒ 不会分叉；本函数是第三个入口：专门收拾已经被改脏的行（通用 PATCH 曾能单独改 sales_agent）、以及「显示名被改过（改名）」这一类历史漂移。
    为什么它不写留痕：调用场景是「换归属端点收到 目标 == 现归属人」（幂等档）——那不是一个变更动作（与 /price/ 的 R27.2「同值不留痕」同口径）；日志动作 order.change_sales_agent 的语义是「把甲换成乙」、为一次对齐写它会让正文说一句没发生的事。返回与边界：True = 真的写了（sales_agent 与目标显示名不一致、已对齐）；False = 无需写（本来就一致，或 order.sales_user 为空 ⇒ 没有「目标显示名」可比）——⚠️ 后一档不把这一列清空：没有归属人时那一列装的是历史文本、清它是一次没人要求的破坏性写入（Order.sales_agent 与 sales_user 的对应关系只在「有归属人」时成立）；⚠️ 它不写 Order.sales_user、不碰绩效行、不写任何日志：那是 change_sales_agent 的事 ⛔ 别把它当「轻量版换人」用——那会让「换人 = 提成也换人」这条判据多出一个绕得过的口子；⚠️ actor 是必填 keyword：Order.updated_by 是「最后动过这单的人」、缺省成 "" 等于让这一列说一句没来源的话。"""
    owner = order.sales_user
    if owner is None:
        return False
    want = clip_sales_agent(user_display_name(owner))
    if order.sales_agent == want:
        return False
    order.sales_agent = want
    order.updated_by = actor
    order.save(update_fields=["sales_agent", "updated_by", "updated_at"])
    return True


def set_default_assign_preference(operator, *, mode, taker=None):
    """把操作人自己的录单偏好档位写成 mode 说的那一档（StaffProfile 上那两列）。mode 三档（⭐ 业务方裁定 R1/R2）：'person' = 记住 taker 这个人 ⇒ default_assignee = taker、default_throw_to_hall = False；'hall' = 记住「默认抛内部抢单」⇒ default_throw_to_hall = True、default_assignee = None（下次录单默认选中「内部抢单」档；价格每次用户自己填、本函数 ⛔ 不碰任何金额列）；'none' = 两个都清（对标录单页那句 lay-tips：「当你取消打勾后提交，将自动清除该设置」）。
    ⚠️ 互斥只有这一处实现（本线把原来的 set_default_assignee 收成它的薄封装）：两列不许同时为真是模型上的一条业务不变量（accounts/models.py::default_throw_to_hall）、而它由这里保证——⛔ 别在调用方再写一份「写 hall 之前先清人」的补偿逻辑、那必然漂移。⚠️ 只写操作人自己的档案行（operator = 认证态用户）：这是「跟录单的客服走」这条语义的落点、也顺带把它变成一个没有越权面的写动作（没有任何参数能指定「写谁的偏好」）。
    ⚠️ 缺档案行时补一行（get_or_create）：没有档案的人（例如供给出来的租户 admin）勾了「默认指派此人」也要存得住；这一点在本模块里是安全的——games_of 对租户管理员短路为「全部游戏」、而对普通账号来说「没有档案 ⇒ 空集 ⇒ 连建单都过不了 assert_game_allowed」⇒ 走不到这里（代价与理由登记在实施报告 §⑧）。⚠️ 同值不写库（update_fields 只在真的变了才 save）：这条路径每个带清除语义的建单都会调一次、不做这个判定就是每次录单多一次无意义的写（还会推 updated_at）。⚠️ mode='person' 而不给 taker ⇒ ValueError：那是编程错误（调用方自己拼错了档位）、不是业务失败——刻意不把它翻译成一个业务码（E-0010：未预期的照旧响亮）。"""
    if mode not in ("person", "hall", "none"):
        raise ValueError(f"未知的默认指派档位：{mode!r}")
    if mode == "person" and taker is None:
        raise ValueError("mode='person' 必须给 taker")
    profile, _ = StaffProfile.objects.get_or_create(user=operator)
    target_taker = taker if mode == "person" else None
    target_id = target_taker.pk if target_taker is not None else None
    want_hall = mode == "hall"
    changed: list[str] = []
    if profile.default_assignee_id != target_id:
        profile.default_assignee = target_taker
        changed.append("default_assignee")
    if profile.default_throw_to_hall != want_hall:
        profile.default_throw_to_hall = want_hall
        changed.append("default_throw_to_hall")
    if changed:
        profile.save(update_fields=[*changed, "updated_at"])


def set_default_assignee(operator, taker):
    """set_default_assign_preference 的薄封装（记人 / 清除那一侧；D-0179 的既有调用形状）。taker is None ⇒ mode='none'（两列都清）、否则 mode='person'。
    ⚠️ 互斥逻辑已收进上面那个函数：本函数只有转发——⛔ 别把任何判定写回这里（两份互斥逻辑必然漂移）。
    ⚠️ 今天全仓没有生产调用点（E-0011：能被一个反例证伪的绝对句比不写更糟）：D-0179 时代那两处调用点（order_views.create 的两条路径）本线已改成直接调 set_default_assign_preference；本文件里没有别的调用者、src/apps/** 与 src/tests/** 也一处都没有（判据：grep -rn 'set_default_assignee\b' src/ 只命中这个定义处本身——连它自己的注释一共 3 行；docs/ 下的引用见下一段）。⚠️ 那为什么还留着它（真实理由 ⛔ 不是「防御性保留」）：历史文档按这个名字引用它、而这些文档按 AGENTS.md §4「只增不改」不许回改——docs/DECISIONS.md 的 D-0179 那一行、.agents/notes/implemented/architecture/2026-10-03-order-quick-assign.md（:99）、docs/specs/2026-10-08-default-assign-hall-design.md（§2.2 与 §3.2）与 docs/STATUS.md 的本线台账都写着这个名字；删掉它会让那些句子指向一个不存在的符号（读者会以为文档过期、而它指的语义今天仍然成立、只是入口改了名）。代价（如实）：它是一个没有调用点的转发壳、将来若有人只改它不改上面那个函数、不会有任何用例变红——⛔ 所以别把它当入口用、新代码一律调 set_default_assign_preference。"""
    set_default_assign_preference(operator, mode="person" if taker is not None else "none", taker=taker)
