"""Durable dispatch and read-only recovery of operator-initiated assignments."""

import json
import logging
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.crypto import salted_hmac

from apps.business import jidanpu, jidanpu_assignment_lock, jidanpu_direct_taker as direct
from apps.business import jidanpu_first_assignment as first
from apps.business.game_scope import games_of
from apps.business.models import JidanpuSyncEvent, Order
from apps.business.order_flags import can_view_offer_price, can_view_taker
from apps.business.peer_sync import JIDANPU_DIRECT_PENDING_PLATFORM
from apps.rbac.permissions import has_node_now
from jidanpu.errors import JidanpuError, RateLimitError, ServerError, TransportError, UpstreamBlockedError


WORKING = "指派中，正在等待鸡蛋铺确认。"
CHECK_DELAYS = (2, 5, 15, 30, 60)
MAX_ATTEMPTS = len(CHECK_DELAYS) + 2
ROUND_LIMIT = 20
logger = logging.getLogger(direct.__name__)


class OperationConflict(direct.AssignmentError):
    pass


def _operation(order, operation_id):
    return direct._events(order).filter(payload__operation_id=str(operation_id)).first()


def has_pending_binding(binding):
    return JidanpuSyncEvent.objects.filter(
        direction=JidanpuSyncEvent.Direction.OUT, kind=direct.KIND, status=JidanpuSyncEvent.Status.PENDING,
        payload__has_key="operation_id", payload__member_binding_id=binding.pk,
    ).exists()


def options(order, *, operation_id=None):
    if operation_id is not None:
        event = _operation(order, operation_id)
    else:
        marker = order.peer_direct_pending or {}
        if marker.get("platform") != JIDANPU_DIRECT_PENDING_PLATFORM:
            return None
        event = direct._events(order).filter(pk=marker["event_id"], payload__has_key="operation_id").first()
    return _result(order, event) if event is not None else None


def _result(order, event):
    payload = event.payload
    if event.status == JidanpuSyncEvent.Status.DONE:
        state = "done"
        result = direct._receipt_options(event)
    elif event.status != JidanpuSyncEvent.Status.PENDING:
        state = "failed"
        result = direct._options(bound=True, reason=payload.get("rejection_message") or event.reason)
    elif payload["phase"] == "ready":
        state = "needs_review"
        result = first._ready_options(order, event)
    else:
        state = "working" if event.attempts < MAX_ATTEMPTS else "needs_review"
        reason = WORKING if state == "working" else (event.reason or direct.UNKNOWN)
        if payload.get("queued_linked"):
            result = direct._options(bound=True, reason=reason, pending=event)
        else:
            result = first._options(order, studios=[payload["studio"]], reason=reason, pending=event, fee=payload["fee"])
    return {**result, "operation_id": payload["operation_id"], "operation_status": state}


def _existing_result(order, event, fingerprint):
    if event.payload.get("request_fingerprint") != fingerprint:
        raise OperationConflict("重新提交的内容与原指派不同，正在核对原指派结果。")
    return _result(order, event)


def enqueue(order, *, operation_id, operator, number, name, expected_number, expected_fee, fee=None,
            studio_id=None, studio_token=None, expected_price=None, price=None):
    intent = (operator.pk, studio_id, number, name, expected_number, expected_fee, fee, expected_price, price)
    fingerprint = salted_hmac("jidanpu-direct-request", json.dumps(intent, default=str), algorithm="sha256").hexdigest()
    if (event := _operation(order, operation_id)) is not None:
        return _existing_result(order, event, fingerprint)
    first_request = dict(studio_id=studio_id, studio_token=studio_token, expected_number=expected_number,
                         expected_fee=expected_fee, fee=fee)
    if studio_id is not None:
        binding, _ = first._prepare_request(order, **first_request)
    else:
        binding, link, reason = direct._context(order)
        if link is None:
            raise direct.AssignmentError(reason)
    with jidanpu_assignment_lock.sending(binding_id=binding.pk, order_id=order.pk) as busy, transaction.atomic():
        if (event := _operation(order, operation_id)) is not None:
            return _existing_result(order, event, fingerprint)
        if busy:
            raise direct.AssignmentError(busy)
        payload = {
            "operation_id": str(operation_id), "phase": "queued",
            "expected_fee": str(expected_fee) if expected_fee is not None else None,
            "changes_amount": price is not None or fee is not None,
            "request_fingerprint": fingerprint,
        }
        if studio_id is not None:
            binding, posted_fee = first._prepare_request(order, **first_request)
            order, event, _ = first._reserve(
                order, binding, operator=operator, number=number, name=name,
                expected_price=expected_price, price=price, fee=posted_fee, queued=payload,
            )
        else:
            posted_fee = fee if fee is not None else expected_fee
            if posted_fee is None:
                raise direct.AssignmentError("鸡蛋铺订单没有可用的默认金额，请填写付给下家打手的金额。")
            latest = direct._latest(order, binding, link)
            event = direct._reserve(
                order, binding, link, last_event_id=latest.pk if latest else None, operator=operator,
                target={"number": number, "name": name}, fee=posted_fee,
                queued={**payload, "queued_linked": True, "expected_number": expected_number},
            )
    return _result(order, event)


def _permitted(order, operator, *, changes_amount):
    return (
        operator is not None and operator.is_active and has_node_now(operator, "order.assign_taker")
        and can_view_taker(operator) and can_view_offer_price(operator)
        and (not changes_amount or has_node_now(operator, "order.submit_payout_price"))
        and games_of(operator).filter(pk=order.game_id).exists()
    )


def _fail_before_send(order, event, operator, reason):
    if first.is_pending(order):
        if operator is not None and event.payload.get("retained_draft"):
            first._reject_assignment(order, event, operator, reason)
            return
        with transaction.atomic():
            Order.objects.filter(pk=order.pk, peer_direct_pending__event_id=event.pk).update(peer_direct_pending=None)
            if direct._finish(event, JidanpuSyncEvent.Status.SKIPPED, reason[:255]) and operator is not None:
                direct._record_assignment_outcome(order, actor=operator.get_username(), actor_id=operator.pk, reason=reason)
    else:
        direct._finish(event, JidanpuSyncEvent.Status.FAILED, reason[:255])


def _linked(order, event, operator, session):
    binding, link, reason = direct._context(order)
    payload = event.payload
    if (
        link is None or (link.pk, link.jidanpu_order_id, binding.pk)
        != (payload["link_id"], payload["remote_order_id"], payload["member_binding_id"])
        or direct._binding_identity(binding) != payload["binding_identity"]
    ):
        raise direct.AssignmentError(reason or direct.CHANGED)
    if payload["phase"] == direct.RECEIPT_CONFIRMED:
        return direct._complete_from_receipt(order, binding, link, event)
    if payload["phase"] == direct.RECEIPT_REJECTED:
        return direct._complete_from_refusal(event)
    with session(binding) as (client, _):
        fee = Decimal(payload["fee"])
        if payload["phase"] == "queued":
            expected_fee = payload["expected_fee"]
            return direct._submit_jidanpu_assignment(
                order, binding, link, client, operator=operator, event=event, number=payload["number"],
                name=payload["name"], expected_number=payload["expected_number"],
                expected_fee=Decimal(expected_fee) if expected_fee is not None else None, fee=fee,
            )
        target = {"number": payload["number"], "name": payload["name"]}
        return direct._recover_assignment(client, order, link, event, target, fee=fee)


def _first(order, event, operator, session):
    payload = event.payload
    may_have_written = payload["phase"] in (
        "unlisting", "sending", direct.RECEIPT_CONFIRMED, direct.RECEIPT_REJECTED,
    ) or (
        payload["phase"] == "recording" and payload.get("recording_source_order_no")
    )
    if may_have_written:
        result = first.verify(order, operator=operator, pending_ref=str(event.pk), session=session)
        if not result.get("reservation"):
            return result
        event.refresh_from_db(fields=["payload"])
    if payload["phase"] != "queued" and not _permitted(order, operator, changes_amount=payload["changes_amount"]):
        raise direct.AssignmentError("指派权限已变化，请重新核对原单。")
    binding = first._binding(order, payload["member_binding_id"])
    if direct._binding_identity(binding) != payload["binding_identity"]:
        raise direct.AssignmentError(first.UNAVAILABLE)
    with session(binding) as (client, _):
        event.payload = {**event.payload, "phase": "recording"}
        event.save(update_fields=["payload"])
        expected_fee = payload["expected_fee"]
        return first._execute(
            order, binding, operator, event, client,
            expected_fee=Decimal(expected_fee) if expected_fee is not None else None,
            new_reservation=payload["new_reservation"],
        )


def _run(event, *, session):
    order = Order.objects.filter(order_no=event.target_ref).first()
    if order is None:
        direct._finish(event, JidanpuSyncEvent.Status.SKIPPED, direct.CHANGED)
        return
    with jidanpu_assignment_lock.sending(binding_id=event.payload["member_binding_id"], order_id=order.pk) as busy:
        if busy:
            return
        event.refresh_from_db()
        order.refresh_from_db()
        if event.status != JidanpuSyncEvent.Status.PENDING or event.attempts >= MAX_ATTEMPTS:
            return
        if event.processed_at is not None and event.attempts > 1 and timezone.now() < event.processed_at + timedelta(
            seconds=CHECK_DELAYS[event.attempts - 2]
        ):
            return
        marker = order.peer_direct_pending or {}
        if marker.get("platform") != JIDANPU_DIRECT_PENDING_PLATFORM or marker.get("event_id") != event.pk:
            direct._finish(event, JidanpuSyncEvent.Status.SKIPPED, direct.CHANGED)
            return
        operator = get_user_model().objects.filter(pk=event.payload["operator_id"]).first()
        if event.payload["phase"] == "queued" and not _permitted(order, operator, changes_amount=event.payload["changes_amount"]):
            _fail_before_send(order, event, operator, "订单或指派权限已变化，本次指派未发送。")
            return
        if operator is None:
            _schedule_review(event, direct.UNKNOWN, force_manual=True)
            return
        try:
            handler = _linked if event.payload.get("queued_linked") else _first
            handler(order, event, operator, session)
        except (direct.AssignmentError, JidanpuError) as exc:
            event.refresh_from_db(fields=["status", "payload"])
            if event.status != JidanpuSyncEvent.Status.PENDING:
                return
            reason = str(exc) if isinstance(exc, direct.AssignmentError) else direct.READ_ERROR
            retry_read = isinstance(exc, (RateLimitError, ServerError, TransportError, UpstreamBlockedError))
            if event.payload["phase"] == "queued" and (not retry_read or event.attempts >= MAX_ATTEMPTS - 1):
                _fail_before_send(order, event, operator, reason)
            else:
                _schedule_review(event, reason, force_manual=event.payload["phase"] == "ready")


def _schedule_review(event, reason, *, force_manual=False):
    JidanpuSyncEvent.objects.filter(
        pk=event.pk, status=JidanpuSyncEvent.Status.PENDING, attempts=event.attempts,
    ).update(
        attempts=MAX_ATTEMPTS if force_manual else event.attempts + 1,
        processed_at=timezone.now(), reason=reason[:255],
    )


def run_pending(*, session=None, stop=None):
    session = session or jidanpu.member_session
    now = timezone.now()
    due = Q(processed_at__isnull=True)
    for attempts, delay in enumerate(CHECK_DELAYS, start=2):
        due |= Q(attempts=attempts, processed_at__lte=now - timedelta(seconds=delay))
    events = JidanpuSyncEvent.objects.filter(
        due, status=JidanpuSyncEvent.Status.PENDING, kind=direct.KIND,
        direction=JidanpuSyncEvent.Direction.OUT, target_type="order", payload__has_key="operation_id",
        attempts__lt=MAX_ATTEMPTS,
    ).order_by(F("processed_at").asc(nulls_first=True), "id")[:ROUND_LIMIT]
    unexpected = False
    for event in list(events):
        if stop is not None and stop.is_set():
            break
        try:
            _run(event, session=session)
        except Exception as exc:  # noqa: BLE001
            unexpected = True
            logger.error(
                "jidanpu_direct_assignment tenant=%s event=%s error=%s",
                connection.schema_name, event.pk, type(exc).__name__,
            )
            _schedule_review(event, direct.UNKNOWN)
    if unexpected:
        raise RuntimeError("有鸡蛋铺直派事件本轮处理失败")
