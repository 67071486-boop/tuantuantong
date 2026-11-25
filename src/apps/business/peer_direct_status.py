"""Carry downstream status through a direct assignment's acknowledgement window."""
import uuid

from django.utils.dateparse import parse_datetime

from apps.business.models import EmpType, PeerSyncEvent, Performance
from apps.business.peer_sync import DIRECT_TAKER_ASSIGN_KIND, JIDANPU_DIRECT_PENDING_PLATFORM

WIRE_FIELD = "direct_assignment"


def current_claim_stamp(order, account_id):
    claimed_at = Performance.objects.filter(
        order=order, user_id=account_id, emp_type=EmpType.MAIN, deleted_at__isnull=True,
    ).values_list("claimed_at", flat=True).first()
    return claimed_at.isoformat() if claimed_at is not None else None


def outgoing_status_identity(link):
    receipt = PeerSyncEvent.objects.filter(
        binding_role="upstream", binding_id=str(link.upstream_binding_id),
        direction="in", kind=DIRECT_TAKER_ASSIGN_KIND,
        status=PeerSyncEvent.Status.DONE, target_ref=link.peer_order_no,
    ).first()
    if receipt is None or not receipt.meta.get("source_order_no"):
        return {}
    return {WIRE_FIELD: {
        "operation_id": receipt.idempotency_key,
        "order_no": receipt.meta["source_order_no"],
    }}


def parse_identity(value):
    if not isinstance(value, dict):
        return None
    operation_id, order_no = value.get("operation_id"), value.get("order_no")
    if not isinstance(operation_id, str) or not isinstance(order_no, str) or len(order_no) > 64:
        return None
    try:
        if str(uuid.UUID(operation_id)) == operation_id:
            return operation_id, order_no
    except ValueError:
        pass
    return None


def stage_status(order, binding, event, operation_id, occurred_at):
    pending = order.peer_direct_pending
    if (
        pending.get("platform") == JIDANPU_DIRECT_PENDING_PLATFORM
        or pending["operation_id"] != operation_id
        or pending["studio_id"] != binding.pk
        or pending["target_ref"] != event["target_ref"]
        or pending.get("binding_base_url") != binding.peer_base_url
    ):
        return False
    previous = pending.get("downstream_status")
    if previous is None or occurred_at > parse_datetime(previous["occurred_at"]):
        pending["downstream_status"] = {
            "kind": "order.update",
            "target_ref": event["target_ref"],
            "occurred_at": occurred_at.isoformat(),
            "payload": {
                "status": event["payload"]["status"],
                WIRE_FIELD: {"operation_id": operation_id, "order_no": order.order_no},
            },
        }
        order.save(update_fields=["peer_direct_pending"])
    return True


def find_handoff(order, binding, ref, operation_id):
    claim_stamp = current_claim_stamp(order, binding.account_id)
    if claim_stamp is None:
        return None
    return PeerSyncEvent.objects.select_for_update().filter(
        binding_role="downstream", binding_id=str(binding.pk),
        direction="out", kind=DIRECT_TAKER_ASSIGN_KIND,
        status=PeerSyncEvent.Status.DONE, target_ref=order.order_no,
        idempotency_key=operation_id, meta__direct_target_ref=ref,
        meta__source_claim_stamp=claim_stamp,
        meta__direct_peer_base_url=binding.peer_base_url,
    ).first()


def status_is_fresh(order, handoff, occurred_at):
    last_remote = handoff.meta.get("downstream_occurred_at")
    if last_remote is not None and occurred_at <= parse_datetime(last_remote):
        return False
    # The acknowledgement's local timestamp must not hide a real remote change.
    anchor = parse_datetime(handoff.meta["status_anchor_at"])
    return order.updated_at is None or occurred_at > order.updated_at or order.updated_at == anchor


def advance_handoff(order, handoff, occurred_at):
    order.refresh_from_db(fields=["updated_at"])
    handoff.meta["status_anchor_at"] = order.updated_at.isoformat()
    handoff.meta["downstream_occurred_at"] = occurred_at.isoformat()
    handoff.save(update_fields=["meta"])
