"""拆单组**行为侧**的组判据（D-0357 拆单组协作机制重设计）。 ## 为什么单独成模块「这单是不是拆单成员」「是不是组内主单」「组里还剩几笔 / 是否全完成」这一组判据被 **四条线**共用（D-0357 spec §4）： | 判据 | 消费方 | |---|---| | `is_split_member` / `is_main_member` | `order_writes.change_price`（G2 子单拒改接单价） | | 同上 + `group_all_completed` | `settlement_hooks` 两个本体（G11/G12 绩效组闸）、跨单触发（G12/G15） |
| `active_members` / `main_member` | `recycle.soft_delete_order`（G9 出组 / G10 级联 / G8 收口） | | 组互斥锁 + 完成度 | `xgj_ship` 发货闸（G6/G7/G8） | 按 `AGENTS.md` §4「不重复同一事实」与 E-0021（同一事实两套判据必然漂移），它们**只**在这里实现一份；各消费方⛔ 不许自己再写「`split_id != ""`」的第二份判据。 ## 口径（全部来自 spec §3，逐字）
- **成员**：`Order.split_id` 非空串即成员；空串 = 不是拆单（`models.py` 的语义开关）。 - **主单**：组内**活跃**成员里 `id` 最小者。⚠️ 活跃 = 默认 manager（已软删成员已出组、不在组内——G9 删除时清 `split_id`）⇒ 「主单」随删除自动转移，判据不需要特判。 - **组齐**：组内**全部活跃成员**状态都是 `COMPLETED`（含本单自己；本单刚完成时它的行已在当前事务里写成 `COMPLETED` ⇒ 同事务查询读得到）。REFUNDED / 未完成的成员都挡「组齐」；它们的出口是删除（G9）+ 收口补发（G8）。 ## 本模块不写库只读查询。写动作（清 `split_id`、级联软删、补发货、重算提成）全在各自消费方—— 本模块只回答「组现在是什么形状」。"""
from itertools import count

from django.db import DatabaseError, connection, transaction
from django.db.models import Exists, F, OuterRef, Q
from apps.core.exceptions import SplitGroupBusy

from apps.business.models import (
    EmpType, LegacySplitRefundPolicy, Order, OrderRefund, OrderStatus, PeerOrderLink,
    Performance, PerformanceAdjustment, SettleStatus,
)

LEGACY = "LEGACY"
PEER = "PEER"
_SOURCE_NUMBER_LOCK_NAMESPACE = 0x53504C54


def lock_main_and_sales_nowait(main):
    from apps.business.models import EmpType, Performance
    from apps.business.order_writes import lock_order_row

    if not connection.in_atomic_block:
        raise RuntimeError('拆单组刷新必须在事务内执行')
    try:
        with transaction.atomic():
            locked = lock_order_row(main, nowait=True)
            if locked is None:
                raise SplitGroupBusy()
            list(Performance.objects.filter(order_id=locked.pk, emp_type=EmpType.SALES)
                 .order_by('id').select_for_update(nowait=True, of=('self',)))
            return locked
    except DatabaseError as exc:
        if getattr(exc.__cause__, 'sqlstate', None) == '55P03':
            raise SplitGroupBusy() from exc
        raise


def lock_performance_nowait(performance_id):
    from apps.business.models import Performance

    try:
        with transaction.atomic():
            return Performance.objects.select_for_update(nowait=True).get(pk=performance_id)
    except DatabaseError as exc:
        if getattr(exc.__cause__, 'sqlstate', None) == '55P03':
            raise SplitGroupBusy() from exc
        raise


def shipping_main_order(order):
    """发货认原主单，仍读它当前的来源号，允许修正错号后重新完成。"""
    main_no = order.split_ship_main_order_no
    if main_no is None or main_no == order.order_no:
        return order
    return Order.all_objects.filter(order_no=main_no).first()


def shipping_lineage_order_nos(main_no):
    """已软删成员的历史发货回执也属于原主单。"""
    return Order.all_objects.filter(split_ship_main_order_no=main_no).values("order_no")


def shipping_members(members):
    """自动发货只等待未软删、未退单的组员。"""
    return [
        member for member in members
        if member.deleted_at is None and member.status != OrderStatus.REFUNDED
    ]


def has_split_payment_lock(orders, *, legacy_anchor=None, lock=False):
    affected = Q(order_id__in={order.pk for order in orders})
    if legacy_anchor is not None:
        affected |= Q(order_id=legacy_anchor.pk, emp_type=EmpType.SALES)
    # Restoring an order can leave its paid performances soft-deleted.
    rows = Performance.objects.filter(affected).only(
        "settle_status", "settled_at", "settled_amount", "online_payroll_batch",
    )
    if lock:
        rows = rows.order_by("pk").select_for_update(of=("self",))
    return any(row.is_settled or row.online_payroll_batch_id is not None for row in rows)


def unsettled_legacy_sales_waiting_ids(performance_ids):
    """Split-group sales commission becomes payable only when every active member is complete."""
    unfinished = active_members(OuterRef("order__split_id")).exclude(status=OrderStatus.COMPLETED)
    return (
        Performance.objects.filter(
            pk__in=performance_ids, emp_type=EmpType.SALES, settle_status=SettleStatus.UNSETTLED,
            settled_at__isnull=True, settled_amount__isnull=True, deleted_at__isnull=True,
            order__deleted_at__isnull=True,
            order__split_kind__in=("", LEGACY),
        )
        .exclude(order__split_id="")
        .filter(Exists(unfinished)).order_by().values("pk")
    )


def lock_child_source_numbers():
    """同一租户的拆单事务串行分配来源号。"""
    if not connection.in_atomic_block:
        raise RuntimeError("拆单来源单号须在建单事务内分配")
    # 不同拆单组也可能共享来源号，或在截断后得到同一个候选号。
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
            [_SOURCE_NUMBER_LOCK_NAMESPACE, connection.schema_name],
        )


def child_source_numbers(source_no, main_order_no):
    """持拆单编号锁时，跳过本租户已占用的来源号。"""
    source_no = (source_no or "").strip() or main_order_no
    max_length = min(
        Order._meta.get_field("source_order_no").max_length,
        PeerOrderLink._meta.get_field("peer_order_no").max_length,
    )
    # 未规范化的历史号不能走精确索引。只预取同前缀、可能带边缘空白的行。
    prefix = source_no[:max_length // 2]
    possibly_padded = (
        Order.all_objects.filter(source_order_no__contains=prefix)
        .exclude(source_order_no__startswith=prefix, source_order_no__regex=r"[0-9]\Z")
        .values_list("source_order_no", flat=True)
    )
    padded_numbers = {number.strip() for number in possibly_padded}
    for index in count(1):
        suffix = f"-{index}"
        candidate = f"{source_no[:max_length - len(suffix)]}{suffix}"
        if candidate not in padded_numbers and not Order.all_objects.filter(source_order_no=candidate).exists():
            yield candidate


def is_split_member(order) -> bool:
    """这单是不是拆单组成员（`split_id` 非空串；空串 = 不是拆单，`models.py` 的语义开关）。"""
    return order.split_id != ""


def is_legacy_member(order) -> bool:
    # Empty kind is the compatibility spelling for pre-migration/legacy fixture rows.
    return is_split_member(order) and order.split_kind in ("", LEGACY)


def is_peer_member(order) -> bool:
    return is_split_member(order) and order.split_kind == PEER


def cascades_on_delete(order, anchor) -> bool:
    return anchor is not None and not is_peer_member(order) and anchor.pk == order.pk


def active_members(split_id):
    """组内**活跃**成员（默认 manager ⇒ 已软删成员不在），`id` 升序 ⇒ 首行即主单。
    ⚠️ `split_id` 上有 `db_index`（`models.py`）；组员是个位数，全取不贵。"""
    return Order.objects.filter(split_id=split_id).order_by("id")


def main_member(split_id):
    """组内主单（活跃成员 `id` 最小者）；组已空（竞态 / 残留）⇒ `None`。"""
    return active_members(split_id).filter(Q(split_kind=LEGACY) | Q(split_kind="")).first()


def legacy_children_with_historical_deductions(split_id, main_id):
    children = active_members(split_id).exclude(pk=main_id).values('pk')
    adjusted = set(PerformanceAdjustment.objects.filter(
        refund__order_id__in=children,
        performance__order_id=F('refund__order_id'),
        kind=PerformanceAdjustment.Kind.PARTIAL_REFUND,
    ).values_list('refund__order_id', flat=True))
    unmarked = active_members(split_id).exclude(pk=main_id).annotate(
        has_refund_record=Exists(OrderRefund.objects.filter(order_id=OuterRef('pk'))),
        has_policy=Exists(LegacySplitRefundPolicy.objects.filter(order_id=OuterRef('pk'), split_id=split_id)),
    ).filter(Q(has_refund_record=True) | ~Q(refund_fee=0), has_policy=False).values_list('pk', flat=True)
    return list(adjusted | set(unmarked))


def is_main_member(order) -> bool:
    """这单是不是**组内主单**（`split_id` 非空且没有比它 `id` 更小的活跃成员）。
    ⛔ 非拆单**恒 `False`**（G16 的 `split_is_main` 注解与这里是同一口径）。
    用「存在更小 id 的成员吗」这条 `EXISTS` 判断，不把整组取回来（G2 在 `/price/` 热路径上，一索引查询即可）。"""
    if order.split_id == "" or order.split_kind not in ("", LEGACY):
        return False
    return not Order.objects.filter(
        split_id=order.split_id, id__lt=order.id
    ).exists()


def balance_anchor_member(split_id):
    """最早的活跃 PEER/LEGACY 成员，作为接单价调平锚点。"""
    return active_members(split_id).filter(split_kind__in=("", LEGACY, PEER)).first()


def is_balance_anchor(order) -> bool:
    if not is_split_member(order) or order.split_kind not in ("", LEGACY, PEER):
        return False
    return not Order.objects.filter(
        split_id=order.split_id,
        split_kind__in=("", LEGACY, PEER),
        id__lt=order.id,
    ).exists()


def completed_count(split_id) -> int:
    """组内已完成（`COMPLETED`）的活跃成员数（G6 日志里的 `X`）。"""
    return active_members(split_id).filter(status=OrderStatus.COMPLETED).count()


def member_count(split_id) -> int:
    """组内活跃成员总数（G6 日志里的 `N`）。"""
    return active_members(split_id).count()


def group_all_completed(order) -> bool:
    """组内**全部**活跃成员都已 `COMPLETED`（含本单自己；G6/G12 的「组齐」判据）。 ⚠️ 本单刚完成的那一跳：`apply_transition` 在跑钩子**之前**已把状态写进当前事务
    ⇒ 同事务的这条查询读得到 `COMPLETED`（spec §2.1 的机制注记）。 ⚠️ 非 `split_id` 非空时**不应调用**（调用方先判 `is_split_member`）；
    空串在这里会查出一堆无关行，防御不是契约——调用方契约见模块 docstring。"""
    return not active_members(order.split_id).exclude(
        status=OrderStatus.COMPLETED
    ).exists()
