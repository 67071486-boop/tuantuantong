"""鸡蛋铺下家撤回和重抛的轮次判据。"""
from datetime import datetime

from django.db.models import Q
from django.utils import timezone

from apps.business.models import JidanpuSyncEvent

KIND_WITHDRAW = "member.withdraw"
WITHDRAW_ABSENCE_CONFIRMED_KEY = "withdraw_absence_confirmed"
WITHDRAW_NO_LINK_REASON = "本线台账里没有这张单的链路，它从未录到对方系统，本次退单作罢"


def effective_at(event) -> datetime | None:
    """人工复用旧台账时优先认本次操作时间，存量事件仍可用创建时间。"""
    raw = (event.payload or {}).get("rearmed_at")
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            parsed = None
        if parsed is not None:
            if timezone.is_naive(parsed):
                parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
            return parsed
    return event.created_at


def _intent_sort_key(event):
    return effective_at(event), event.pk


def _withdrawals():
    return JidanpuSyncEvent.objects.filter(
        direction="out", kind=KIND_WITHDRAW, target_type="order",
    ).only("target_ref", "created_at", "payload", "status", "reason", "attempts", "processed_at").order_by()


def latest_withdrawal(order_no, binding):
    # 存量撤回可能复用旧行，不能按台账主键认最新。
    rows = _withdrawals().filter(target_ref=order_no, payload__member_binding_id=binding.pk)
    return max(rows, key=_intent_sort_key, default=None)


def confirmed_withdrawal_since(order_no, binding, moment) -> bool:
    """回调收到后确认过的撤回，即使同号副本后来恢复也算旧轮。"""
    confirmed = Q(status="done") | Q(
        status="skipped", **{f"payload__{WITHDRAW_ABSENCE_CONFIRMED_KEY}": True},
    )
    return moment is not None and _withdrawals().filter(
        confirmed, target_ref=order_no, payload__member_binding_id=binding.pk, processed_at__gte=moment,
    ).exists()


def latest_withdrawals(order_nos, binding_ids):
    """一次读取每个订单和下家组合的最新撤回。"""
    latest = {}
    for row in _withdrawals().filter(
        target_ref__in=order_nos, payload__member_binding_id__in=binding_ids,
    ).only("target_ref", "created_at", "payload").iterator():
        key = row.target_ref, row.payload["member_binding_id"]
        previous = latest.get(key)
        if previous is None or _intent_sort_key(row) > _intent_sort_key(previous):
            latest[key] = row
    return latest


def withdrawal_intent(withdrawal):
    return {"id": withdrawal.pk, "rearmed_at": (withdrawal.payload or {}).get("rearmed_at")}


def _withdrawal_reference(launch):
    return (launch.payload or {}).get("after_withdraw")


def superseded_by_withdrawal(launch, withdrawal) -> bool:
    """有配对撤回时认台账行和操作时间，存量任务按操作先后比较。"""
    previous = _withdrawal_reference(launch)
    if previous is not None:
        return withdrawal is None or previous != withdrawal_intent(withdrawal)
    return withdrawal is not None and _intent_sort_key(withdrawal) > _intent_sort_key(launch)


def is_manual_relaunch(launch, *, after_withdraw: bool, withdrawn_at) -> bool:
    return after_withdraw or (
        bool((launch.payload or {}).get("rearmed_at"))
        and (withdrawn_at is None or effective_at(launch) > withdrawn_at)
    )


def predates_confirmed_withdrawal(launch, link, *, after_withdraw: bool) -> bool:
    moment = effective_at(launch)
    return (
        link is not None and link.withdrawn_at is not None
        and moment is not None and moment <= link.withdrawn_at
        and not after_withdraw
    )


def withdrawal_confirmed(withdrawal, *, absent_reason: str) -> bool:
    return withdrawal.status == "done" or (
        withdrawal.status == "skipped" and (
            withdrawal.reason == WITHDRAW_NO_LINK_REASON
            or bool(absent_reason and withdrawal.reason == absent_reason)
        )
    )


def launch_is_current(event) -> bool:
    payload = event.payload or {}
    current = JidanpuSyncEvent.objects.filter(pk=event.pk, status=event.status).first()
    if current is None:
        return False
    current_payload = current.payload or {}
    return all(
        current_payload.get(key) == payload.get(key)
        for key in ("rearmed_at", "after_withdraw")
    )


def confirmed_withdrawal_matches_link(launch, link) -> bool:
    """回补接手前，配对撤回的确认时刻须与当前链路相符。"""
    dependency = _withdrawal_reference(launch)
    return not dependency or JidanpuSyncEvent.objects.filter(
        pk=dependency["id"], status="done", processed_at=link.withdrawn_at,
    ).exists()


def claim_blocked(binding, link) -> bool:
    """新一轮上架尚未确认时，接手提醒不能沿用旧副本的结果。"""
    withdrawal = latest_withdrawal(link.order.order_no, binding)
    if withdrawal is None:
        return False
    # 从未录到对方的配对撤回没有旧副本，首次接手照常。
    if withdrawal.status == "skipped" and withdrawal.reason == WITHDRAW_NO_LINK_REASON:
        return False
    return link.withdrawn_at is not None or (
        link.listed_at is not None and effective_at(withdrawal) >= link.listed_at
    ) or (
        link.listed_at is None and withdrawal.status in ("pending", "failed")
    )


def listing_withdrawal_confirmed_at(binding, link) -> datetime | None:
    """返回已上架副本对应的撤回确认时刻，用于过滤旧接手。"""
    if not link.listed_at:
        return None
    withdrawal = latest_withdrawal(link.order.order_no, binding)
    if (
        withdrawal is not None and withdrawal.status == "done"
        and withdrawal.processed_at and withdrawal.processed_at <= link.listed_at
    ):
        return withdrawal.processed_at
    return None
