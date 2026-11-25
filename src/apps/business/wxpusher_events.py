"""Create worker notifications from accepted business changes."""

from apps.accounts.wxpusher_notice_templates import safe_notice_order_text
from apps.accounts.wxpusher_notifications import enqueue_worker_notice
from apps.business.models import EmpType, Order, OrderStatus, Performance
from apps.business.performance_amount import MissingPriceError, effective_taker_price


def queue_order_notice(order, user_id, kind, event_at, *, performance=None, reason=""):
    if user_id is None:
        return
    # The status writer may still hold the Order loaded before it acquired the lock.
    snapshot = Order.objects.filter(pk=order.pk).select_related("project").only(
        "order_no", "source_order_no", "project_text", "project__name", "demand", "hall_price",
    ).first()
    details = {"display_order_no": order.order_no}
    if snapshot is not None:
        details["display_order_no"] = snapshot.source_order_no.strip() or snapshot.order_no
        project = snapshot.project_text.strip() or (snapshot.project.name if snapshot.project_id else "")
        if project:
            details["project"] = safe_notice_order_text(project, sensitive_fallback="请在团团通查看", limit=900)
        if kind != "unassigned" and snapshot.demand.strip():
            details["demand"] = safe_notice_order_text(
                snapshot.demand, sensitive_fallback="请在团团通查看", limit=900,
            )
    if snapshot is not None and performance is not None and kind in {"assigned", "completed"}:
        try:
            details["amount"] = format(effective_taker_price(performance, order=snapshot), ".2f")
        except MissingPriceError:
            pass
    if reason:
        details["reason"] = reason
    enqueue_worker_notice(
        user_id=user_id, kind=kind, order_no=order.order_no, details=details,
        reference=f"order:{order.order_no}:{event_at.isoformat()}",
    )


_STATUS_NOTICES = {
    OrderStatus.COMPLETED: "completed",
    OrderStatus.PAUSED: "paused",
    OrderStatus.ISSUE: "issue",
    OrderStatus.REFUNDED: "refunded",
}


def queue_order_status_notice(order, previous_status, *, event_at=None, actor_id=None, reason=""):
    if previous_status == order.status:
        return
    if previous_status == OrderStatus.PAUSED and order.status == OrderStatus.IN_PROGRESS:
        kind = "resumed"
    else:
        kind = _STATUS_NOTICES.get(order.status)
    if kind is None:
        return
    performance = Performance.objects.filter(
        order=order, emp_type=EmpType.MAIN, deleted_at__isnull=True,
    ).only("user_id", "order_id", "taker_price").first()
    if performance is None or (kind == "issue" and performance.user_id == actor_id):
        return
    queue_order_notice(
        order, performance.user_id, kind, event_at or order.updated_at, performance=performance,
        reason=reason if kind in {"paused", "issue", "refunded"} else "",
    )
