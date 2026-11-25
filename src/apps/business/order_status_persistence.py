"""Persist an accepted order status inside the caller's transaction."""

from apps.business import order_time_fields
from apps.business.wxpusher_events import queue_order_status_notice


def persist_order_status(
    order, status, *, transition_fields: tuple[str, ...] = (), event_at=None,
    published: bool = False, actor_id=None, notify_worker: bool = True, notice_reason: str = "",
) -> None:
    """Save the status and attached notice before marking the first action times."""
    from apps.wecom.notifications import prepare_order_status

    previous_status = order.status
    order.status = status
    update_fields = ["status", *transition_fields, "updated_at"]
    if published:
        update_fields.append("hall_publisher")
    if prepare_order_status(order, previous_status, event_at=event_at):
        update_fields.extend(("wecom_notice_journal", "wecom_notice_pending"))
    # An earlier writer may already have set these timestamps on the database row.
    order.save(update_fields=update_fields)
    if notify_worker:
        queue_order_status_notice(order, previous_status, event_at=event_at, actor_id=actor_id, reason=notice_reason)
    if published:
        order_time_fields.mark_published(order)
    order_time_fields.mark_submitted(order, moment=event_at)
