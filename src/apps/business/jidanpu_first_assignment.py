"""Choose a downstream studio before the local order has a taker."""

from copy import copy
from decimal import Decimal
from uuid import uuid4

from django.db import connection, transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from apps.accounts.display import user_display_name
from apps.business import jidanpu, jidanpu_assignment_lock, jidanpu_direct_taker as direct, jidanpu_member
from apps.business import peer_direct_taker as peer
from apps.business import peer_dispatch
from apps.business.commission import CommissionError, commission_percent_of
from apps.business.game_scope import games_of
from apps.business.models import DailiantongPush, JidanpuMemberBinding, JidanpuMemberOrderLink, JidanpuSyncEvent, OrderStatus
from apps.business.order_writes import assign_order, lock_order_row
from apps.business.peer_sync import HALL_TAKE_LEAVE_ASSIGNED, JIDANPU_DIRECT_PENDING_PLATFORM
from apps.business.penalty_amounts import PENALTY_AMOUNT_LOCKED, changes_approved_penalty_price
from apps.business.performance_amount import commission_amount
from jidanpu.errors import ApiError, JidanpuError, ServerError, TransportError, UpstreamBlockedError


AssignmentError = direct.AssignmentError
PENDING_REASON = "鸡蛋铺首次指派尚未确认，请先核对本次结果。"
SYNCING_REASON = "订单正在同步到鸡蛋铺，请稍后重新核对。"
HALL_REASON = "订单仍在鸡蛋铺大厅，请核对两边的订单状态。"
HALL_TAKEN = "鸡蛋铺订单已有打手接手，本次未指派。"
HALL_CLOSED = "鸡蛋铺订单已退单或已撤销，本次未指派。"
HALL_PENDING = "鸡蛋铺尚未确认撤下大厅，请先核对本次结果。"
HALL_VERIFY_STILL_LISTED = "鸡蛋铺尚未确认撤下大厅。请在鸡蛋铺核对原单，确认已撤下后再回来核对。"
UNAVAILABLE = "这个鸡蛋铺工作室不可用，或未开放本单游戏。"
OTHER_STUDIO = "订单已关联另一家鸡蛋铺，请选择现有关联的工作室，或先核对原订单。"
PRICE_CHANGED = "鸡蛋铺接单价与本次发单价不一致，请先核对两边价格。"
RELEASE_BLOCKED = "鸡蛋铺订单已在大厅或已有接手人，请先在下家核对并处理后再解除等待。"


class HallOrderUnavailable(AssignmentError):
    pass


def is_pending(order):
    marker = order.peer_direct_pending or {}
    return marker.get("platform") == JIDANPU_DIRECT_PENDING_PLATFORM and not marker.get("linked")


def _pending_event(order):
    if not is_pending(order):
        return None
    return direct._events(order).filter(pk=order.peer_direct_pending["event_id"]).first()


def retained_drafts(binding, order_nos=None):
    events = JidanpuSyncEvent.objects.filter(
        direction="out", kind=direct.KIND, target_type="order",
        status=JidanpuSyncEvent.Status.FAILED, payload__retained_draft=True,
        payload__member_binding_id=binding.pk, payload__binding_identity=direct._binding_identity(binding),
    ).order_by("-pk")
    if order_nos is not None:
        events = events.filter(target_ref__in=order_nos)
    result = {}
    for event in events:
        result.setdefault(event.target_ref, event)
    return result


def retains_draft(order, link, event, *, taker_id):
    if event is None or link is None or link.withdrawn_at is not None or taker_id is not None:
        return False
    payload = event.payload
    return (
        order.deleted_at is None and order.status in (OrderStatus.UNASSIGNED, OrderStatus.WAITING_GRAB)
        and payload.get("draft_status") == order.status
        and payload.get("draft_source") == jidanpu_member.effective_source_order_no(order)
        and payload.get("draft_game_id") == order.game_id
        and payload.get("draft_price") == (str(order.hall_price) if order.hall_price is not None else None)
        and (payload.get("link_id"), payload.get("remote_order_id")) == (link.pk, link.jidanpu_order_id)
    )


def _retained_event(order, binding, link=None):
    event = retained_drafts(binding, [order.order_no]).get(order.order_no)
    if event is None:
        return None
    link = link or jidanpu_member._link_for(binding, order)
    return event if retains_draft(order, link, event, taker_id=jidanpu_member._main_taker_id(order.pk)) else None


def _bindings(order):
    if order.game_id is None:
        return []
    return [
        binding for binding in JidanpuMemberBinding.objects.filter(
            enabled=True, user__is_active=True, mode=JidanpuMemberBinding.Mode.PASSWORD,
        ).select_related("user").order_by("pk")
        if not jidanpu_member.credentials_missing(binding) and games_of(binding.user).filter(pk=order.game_id).exists()
    ]


def _binding(order, studio_id):
    for binding in _bindings(order):
        if binding.pk == studio_id:
            return binding
    raise AssignmentError(UNAVAILABLE)


def _studio(binding):
    return {"id": binding.pk, "name": user_display_name(binding.user)}


def _unavailable_reason(order, *, binding=None):
    if (
        not peer._ready_for_direct_assignment(order)
        or order.hall_card_id is not None or order.upstream_hall_card_id is not None
    ):
        return "这张订单已有接手安排，或仍需上家确认。"
    if DailiantongPush.objects.filter(order=order, state__in=(
        DailiantongPush.State.PENDING, DailiantongPush.State.UNKNOWN, DailiantongPush.State.SENT,
    )).exists():
        return "订单已发往其他平台，请先撤回后再指派。"
    active_links = JidanpuMemberOrderLink.objects.filter(order=order, withdrawn_at=None)
    if binding is not None and active_links.exclude(binding=binding).exists():
        return OTHER_STUDIO
    if order.status != OrderStatus.WAITING_GRAB and active_links.filter(in_hall=True).exists():
        return HALL_REASON
    retained_binding = binding
    if retained_binding is None:
        linked = active_links.select_related("binding").first()
        retained_binding = linked.binding if linked is not None else None
    retained = retained_binding is not None and _retained_event(order, retained_binding) is not None
    if not is_pending(order) and not retained and JidanpuSyncEvent.objects.filter(
        direction="out", kind__in=jidanpu_member.OUTBOX_KINDS,
        target_type="order", target_ref=order.order_no, status__in=("pending", "failed"),
        attempts__lt=jidanpu_member.OUTBOX_MAX_ATTEMPTS,
    ).exists():
        return SYNCING_REASON
    return ""


def _verified_remote(order, link, client, *, event=None):
    if link.withdrawn_at is not None:
        raise AssignmentError("鸡蛋铺原订单已撤回，请先核对下家订单。")
    if event is None:
        direct._assert_recording_owner(client, order, remote_id=link.jidanpu_order_id)
    else:
        if (verified := _check_recording_owner(order, event, client, link.jidanpu_order_id)) is not None:
            return verified
    return direct._linked_order(client, order, link)


def _is_unassigned(remote, *, in_hall=False):
    return (
        remote.get("isThrowOrder") == ("Y" if in_hall else "N")
        and jidanpu_member._order_sta_token(remote) == jidanpu.ORDER_UNASSIGNED_LABEL
        and not remote.get("recManName")
    )


def _hall_block_reason(remote):
    if remote.get("isThrowOrder") != "N":
        return ""
    state = jidanpu_member._order_sta_token(remote)
    if jidanpu_member.map_remote_status(state) == OrderStatus.REFUNDED or state in ("已撤销", "YCX"):
        return HALL_CLOSED
    if jidanpu_member._is_remote_assignment_state(state) and str(remote.get("recManName") or "").strip():
        return HALL_TAKEN
    return ""


def _remote_unassigned(order, link, client, *, event=None, allow_hall=False, verified=None):
    if verified is None or link.withdrawn_at is not None:
        verified = _verified_remote(order, link, client, event=event)
    remote = verified
    if allow_hall and order.status == OrderStatus.WAITING_GRAB:
        if _is_unassigned(remote, in_hall=True):
            return remote
        if reason := _hall_block_reason(remote):
            raise HallOrderUnavailable(reason)
    if remote.get("isThrowOrder") == "Y":
        raise AssignmentError(HALL_REASON)
    if not _is_unassigned(remote):
        raise AssignmentError("鸡蛋铺已有接手安排，请先核对下家的订单状态。")
    return remote


def _waiting_order_preview(order, link, client):
    if link.withdrawn_at is not None:
        return None
    try:
        remote = direct._linked_order(client, order, link)
    except (AssignmentError, ApiError, ServerError, TransportError):
        return None
    if _is_unassigned(remote) or _is_unassigned(remote, in_hall=True):
        return remote
    return None


def options(order, *, studio_id=None):
    pending = _pending_event(order)
    if pending is not None:
        if pending.payload["phase"] == "ready" and pending.status == JidanpuSyncEvent.Status.PENDING:
            return _ready_options(order, pending)
        reason = HALL_PENDING if pending.payload["phase"] == "unlisting" else PENDING_REASON
        return _options(
            order, studios=[pending.payload["studio"]], reason=reason,
            pending=pending, fee=Decimal(pending.payload["fee"]),
        )
    if order.peer_direct_pending or not peer._ready_for_direct_assignment(order):
        return None
    bindings = _bindings(order)
    if not bindings:
        return None
    studio = next((bound for bound in bindings if bound.pk == studio_id), None)
    studios = [_studio(bound) for bound in bindings]
    reason = _unavailable_reason(order, binding=studio)
    fee = None
    if studio is not None and not reason and order.status != OrderStatus.WAITING_GRAB:
        link = jidanpu_member._link_for(studio, order)
        if link is not None:
            try:
                with jidanpu.member_session(studio) as (client, _):
                    remote = _remote_unassigned(order, link, client, event=_retained_event(order, studio, link))
                    fee = direct._fee_value(remote, "recFee")
            except JidanpuError:
                reason = direct.READ_ERROR
            except AssignmentError as exc:
                reason = str(exc)
    result = _options(order, studios=studios, reason=reason, fee=fee)
    retained = _retained_event(order, studio) if studio is not None else None
    if retained is not None:
        result["price"] = retained.payload["price"]
    return result


def _options(order, *, studios, reason, fee, pending=None):
    result = direct._options(bound=True, reason=reason, pending=pending, fee=fee)
    result.update(studios=studios, price=str(order.hall_price) if order.hall_price is not None else None)
    return result


def _ready_options(order, event):
    default_fee = event.payload.get("default_fee")
    result = _options(order, studios=[event.payload["studio"]], reason="", fee=default_fee)
    result.update(price=event.payload["price"], reservation=str(event.pk))
    return result


def _ready_event(order, binding):
    event = _pending_event(order)
    if event is None or (
        event.status != JidanpuSyncEvent.Status.PENDING or event.payload["phase"] != "ready"
        or event.payload["member_binding_id"] != binding.pk
        or event.payload["binding_identity"] != direct._binding_identity(binding)
    ):
        raise AssignmentError(PENDING_REASON)
    return event


def _studio_token(order, binding):
    identity = f"{connection.schema_name}:{order.order_no}:{binding.pk}:{direct._binding_identity(binding)}"
    return salted_hmac("jidanpu-first-assignment-studio", identity, algorithm="sha256").hexdigest()


def search(order, query, *, studio_id):
    binding = _binding(order, studio_id)
    if order.peer_direct_pending:
        _ready_event(order, binding)
    if reason := _unavailable_reason(order, binding=binding):
        raise AssignmentError(reason)
    try:
        with jidanpu.member_session(binding) as (client, _):
            link = jidanpu_member._link_for(binding, order)
            remote = None
            fee = None
            if link is not None and order.status == OrderStatus.WAITING_GRAB:
                remote = _waiting_order_preview(order, link, client)
                if remote is not None:
                    if not order.peer_direct_pending:
                        fee = direct._fee_value(remote, "recFee")
                    game_name = remote.get("gameName")
                    if not isinstance(game_name, str) or not game_name.strip():
                        remote = None
            elif link is not None:
                remote = _remote_unassigned(order, link, client, event=_retained_event(order, binding, link))
            result = direct._search_candidates(client, remote or {"gameName": order.game.name}, query)
            if fee is not None:
                result["fee"] = str(fee)
            return {**result, "studio_token": _studio_token(order, binding)}
    except JidanpuError as exc:
        raise AssignmentError("读不到鸡蛋铺打手名单，请重试或核对绑定账号的指派权限。") from exc


@transaction.atomic
def _reserve(order, binding, *, operator, number, name, expected_price, price, fee, queued=None):
    locked = lock_order_row(order)
    if locked is None or not games_of(operator).filter(pk=locked.game_id).exists():
        raise AssignmentError(direct.CHANGED)
    if reason := _unavailable_reason(locked, binding=binding):
        raise AssignmentError(reason)
    if changes_approved_penalty_price(locked, fee):
        raise AssignmentError(PENALTY_AMOUNT_LOCKED)
    if locked.peer_direct_pending:
        event = _ready_event(locked, binding)
        frozen = Decimal(event.payload["price"])
        if changes_approved_penalty_price(locked, frozen):
            raise AssignmentError(PENALTY_AMOUNT_LOCKED)
        if expected_price != frozen or (price is not None and price != frozen):
            raise AssignmentError("鸡蛋铺已录入这张单，发单价须沿用。")
        payload = dict(
            phase="recording", number=number, name=name, fee=str(fee),
            operator=operator.get_username(), operator_id=operator.pk,
        )
        if queued or event.payload.get("operation_id"):
            direct._finish(event, JidanpuSyncEvent.Status.SKIPPED, "已核对原单并重新发起指派。")
            event = _queue_retry(locked, event, {**payload, **(queued or {}), "new_reservation": False})
            return locked, event, False
        event.payload.update(payload)
        jidanpu_member._persist_payload(event, event.payload)
        return locked, event, False
    retained = _retained_event(locked, binding, jidanpu_member._link_for(binding, locked))
    expected_current_price = Decimal(retained.payload["price"]) if retained is not None else locked.hall_price
    if expected_current_price != expected_price:
        raise AssignmentError("团团通发单价已变化，请重新核对后再指派。")
    chosen_price = price if price is not None else expected_current_price
    if chosen_price is None:
        raise AssignmentError("请先设置订单发单价。")
    if changes_approved_penalty_price(locked, chosen_price):
        raise AssignmentError(PENALTY_AMOUNT_LOCKED)
    outcome = assign_order(locked, taker=binding.user, ctx=peer._outgoing_context(operator), dry_run=True)
    if not outcome.ok:
        raise AssignmentError(outcome.msg)
    try:
        commission_amount(chosen_price, commission_percent_of(binding.user))
    except CommissionError as exc:
        raise AssignmentError(exc.message) from exc
    if retained is not None:
        frozen = Decimal(retained.payload["price"])
        if chosen_price != frozen:
            raise AssignmentError("鸡蛋铺已录入这张单，发单价须沿用。")
        payload = dict(
            phase="recording", number=number, name=name, fee=str(fee),
            operator=operator.get_username(), operator_id=operator.pk,
        )
        if queued or retained.payload.get("operation_id"):
            event = _queue_retry(locked, retained, {**payload, **(queued or {}), "new_reservation": True})
        else:
            retained.payload.update(payload)
            retained.status = JidanpuSyncEvent.Status.PENDING
            retained.reason = PENDING_REASON
            retained.processed_at = None
            retained.save(update_fields=["payload", "status", "reason", "processed_at"])
            event = retained
    else:
        event = JidanpuSyncEvent.objects.create(
            direction="out", kind=direct.KIND, target_type="order", target_ref=locked.order_no,
            attempts=1, reason=PENDING_REASON,
            payload={
                "member_binding_id": binding.pk, "binding_identity": direct._binding_identity(binding),
                "studio": _studio(binding), "number": number, "name": name, "fee": str(fee),
                "price": str(chosen_price), "phase": "recording",
                "operator": operator.get_username(), "operator_id": operator.pk,
                **({**queued, "new_reservation": True} if queued else {}),
            },
        )
    # Legacy readers treat the zero studio as a blocked peer assignment.
    locked.peer_direct_pending = {
        "platform": JIDANPU_DIRECT_PENDING_PLATFORM, "event_id": event.pk,
        "studio_id": 0, "studio_name": _studio(binding)["name"], "account_name": "",
        "price": str(chosen_price), "operation_id": str(uuid4()),
        "target_ref": locked.order_no, "binding_base_url": "",
    }
    locked.save(update_fields=["peer_direct_pending"])
    return locked, event, True


def _queue_retry(order, previous, updates):
    payload = {**previous.payload, **updates}
    if "operation_id" not in updates:
        payload.pop("operation_id", None)
    payload.pop("rejection_message", None)
    event = JidanpuSyncEvent.objects.create(
        direction="out", kind=direct.KIND, target_type="order", target_ref=order.order_no,
        attempts=1, reason=PENDING_REASON, payload=payload,
    )
    if previous.payload.get("retained_draft"):
        previous.payload["retained_draft"] = False
        previous.save(update_fields=["payload"])
    if is_pending(order):
        order.peer_direct_pending = {**order.peer_direct_pending, "event_id": event.pk}
        order.save(update_fields=["peer_direct_pending"])
    return event


@transaction.atomic
def _resolve(order, event, *, binding=None, operator, link=None, unlisted=False, reason=""):
    locked = lock_order_row(order)
    if locked is None or not is_pending(locked) or locked.peer_direct_pending["event_id"] != event.pk:
        raise AssignmentError(direct.CHANGED)
    active_links = JidanpuMemberOrderLink.objects.filter(order=locked, withdrawn_at=None)
    if active_links.exclude(binding_id=event.payload["member_binding_id"]).exists():
        raise AssignmentError(OTHER_STUDIO)
    if link is not None or unlisted:
        saved_link = active_links.select_for_update().filter(
            pk=event.payload["link_id"], binding=binding, jidanpu_order_id=event.payload["remote_order_id"],
        ).first()
        if saved_link is None:
            raise AssignmentError(direct.CHANGED)
        if unlisted:
            saved_link.in_hall = False
            saved_link.last_out_at = timezone.now()
            saved_link.save(update_fields=["in_hall", "last_out_at"])
    locked.peer_direct_pending = None
    locked.save(update_fields=["peer_direct_pending"])
    if link is None:
        direct._finish(event, JidanpuSyncEvent.Status.SKIPPED, reason)
        direct._record_assignment_outcome(locked, actor=operator.get_username(), actor_id=operator.pk, reason=reason)
        return None
    was_in_hall = locked.status == OrderStatus.WAITING_GRAB
    outcome = assign_order(
        locked, taker=binding.user, price=Decimal(event.payload["price"]),
        ctx=peer._outgoing_context(operator), dry_run=False,
    )
    if not outcome.ok:
        raise AssignmentError(f"{PENDING_REASON} 本地暂未保存：{outcome.msg}")
    direct._confirm_direct_assignment(locked, link, event)
    if was_in_hall:
        peer_dispatch.notify_hall_membership(
            locked, from_status=OrderStatus.WAITING_GRAB, target=locked.status,
            hall_publisher=None, leave_reason=HALL_TAKE_LEAVE_ASSIGNED,
        )
        jidanpu_member.queue_withdraw(locked, hall_only=True, summarize=True)
    jidanpu_member.queue_pic_repush_for_ready_screenshots(locked)
    return locked


def _refuse(order, event, operator, reason):
    _resolve(order, event, operator=operator, reason=reason)
    raise AssignmentError(reason)


@transaction.atomic
def _reject_assignment(order, event, operator, reason):
    _resolve(order, event, operator=operator, reason=reason[:255])
    event.payload.update(
        phase="ready", retained_draft=True, rejection_message=reason,
        draft_status=order.status, draft_source=jidanpu_member.effective_source_order_no(order),
        draft_game_id=order.game_id,
        draft_price=str(order.hall_price) if order.hall_price is not None else None,
    )
    event.status = JidanpuSyncEvent.Status.FAILED
    event.reason = reason[:255]
    event.save(update_fields=["payload", "status", "reason"])


def _offered_fee(remote, event):
    fee = direct._fee_value(remote, "recFee")
    # 鸡蛋铺撤下大厅时会清零报价，重试沿用撤下前已核对的报酬。
    if fee == 0 and _is_unassigned(remote) and "hall_fee" in event.payload:
        frozen = event.payload["hall_fee"]
        return Decimal(frozen) if frozen is not None else None
    return fee


def _ready_for_retry(event, remote):
    fee = _offered_fee(remote, event)
    event.payload.update(phase="ready", default_fee=str(fee) if fee is not None else None)
    jidanpu_member._persist_payload(event, event.payload)


def _check_recording_owner(order, event, client, remote_id, observed=None):
    known_id = event.payload.get("candidate_remote_order_id")
    if known_id is not None and known_id != remote_id:
        raise AssignmentError(direct.CHANGED)
    if known_id is None:
        jidanpu_member._set_event_progress(event, {**event.payload, "candidate_remote_order_id": remote_id})
    payload = event.payload
    verified = None
    if payload.get("recorded") is True and payload.get("recording_remote_oid") is not None and payload.get("direct_recording_owner_id"):
        uid = jidanpu.fetch_login_uid(client)
        remote = observed
        if remote is None or remote.get("oid") in (None, ""):
            remote = jidanpu.fetch_order_by_key1(client, key=remote_id, expect_field="orderId", expect_value=remote_id)
        if (uid != payload["direct_recording_owner_id"] or remote is None
                or str(remote.get("orderId")) != remote_id
                or remote.get(jidanpu.OWN_SOURCE_VALUE_FIELD) != jidanpu_member.effective_source_order_no(order)
                or str(remote.get("oid")) != str(payload["recording_remote_oid"])):
            raise AssignmentError(direct.OWNERSHIP_UNVERIFIED)
        verified = remote
    else:
        uid = direct._assert_recording_owner(client, order, remote_id=remote_id)
    known_uid = event.payload.get("recording_owner_id")
    if known_uid is not None and known_uid != uid:
        raise AssignmentError(direct.OWNERSHIP_UNVERIFIED)
    if known_uid is None:
        jidanpu_member._set_event_progress(event, {**event.payload, "recording_owner_id": uid})
    return verified


def _recorded_link(order, binding, event, client, *, now):
    link = jidanpu_member._link_for(binding, order)
    if link is not None:
        return link, None
    if not any(key in event.payload for key in ("recorded", "recording_source_order_no", "candidate_remote_order_id")):
        uid = jidanpu.fetch_login_uid(client)
        jidanpu_member._set_event_progress(event, {**event.payload, "direct_recording_owner_id": uid})

    verified = None

    def check_link(remote_id, observed=None):
        nonlocal verified
        verified = _check_recording_owner(order, event, client, remote_id, observed)

    recording_order = copy(order)
    recording_order.hall_price = Decimal(event.payload["price"])
    remote_id, reason, _, _ = jidanpu_member._ensure_remote_order(
        event, recording_order, binding, client, now=now,
        check_link=check_link,
        timing=lambda stage: direct.assignment_stage(event, stage),
        raise_recording_refusal=bool(event.payload.get("operation_id")),
    )
    event.refresh_from_db(fields=["payload"])
    if remote_id is None:
        may_have_recorded = event.payload.get("recorded") or event.payload.get("recording_source_order_no")
        raise AssignmentError(PENDING_REASON if may_have_recorded else reason)
    link = jidanpu_member._link_for(binding, order)
    if link is None:
        raise AssignmentError(PENDING_REASON)
    return link, verified


def _assignment_result(order, event, client, link):
    if link.withdrawn_at is not None or (link.pk, link.jidanpu_order_id) != (
        event.payload["link_id"], event.payload["remote_order_id"],
    ):
        raise AssignmentError(direct.CHANGED)
    remote = direct._linked_order(client, order, link)
    recorded_oid = event.payload.get("recording_remote_oid")
    if recorded_oid is not None and str(remote.get("oid")) != str(recorded_oid):
        raise AssignmentError(direct.OWNERSHIP_UNVERIFIED)
    if "hall_fee" in event.payload and _hall_block_reason(remote) == HALL_CLOSED:
        raise HallOrderUnavailable(HALL_CLOSED)
    if direct._fee_value(remote, "price") != Decimal(event.payload["price"]):
        raise AssignmentError(PRICE_CHANGED)
    current, fee = direct._current(client, remote, link, expected_name=event.payload["name"])
    if current is not None and current["number"] == event.payload["number"] and fee == Decimal(event.payload["fee"]):
        return remote, current
    return remote, None


def _complete_from_receipt(order, binding, operator, event):
    link = jidanpu_member._link_for(binding, order)
    if link is None:
        raise AssignmentError(direct.CHANGED)
    _resolve(order, event, binding=binding, operator=operator, link=link)
    return direct._receipt_options(event)


def _complete_from_refusal(order, event, operator):
    reason = event.payload["rejection_message"]
    _reject_assignment(order, event, operator, reason)
    return _options(order, studios=[event.payload["studio"]], reason=reason, fee=None)


@transaction.atomic
def _stage_unlist(event, link, remote):
    if not JidanpuMemberOrderLink.objects.filter(
        pk=link.pk, order_id=link.order_id, binding_id=link.binding_id,
        jidanpu_order_id=link.jidanpu_order_id, withdrawn_at=None,
    ).update(in_hall=True):
        raise AssignmentError(direct.CHANGED)
    fee = direct._fee_value(remote, "recFee")
    event.payload.update(
        phase="unlisting", link_id=link.pk, remote_order_id=link.jidanpu_order_id,
        hall_fee=str(fee) if fee is not None else None,
    )
    jidanpu_member._persist_payload(event, event.payload)


def _unlist_for_assignment(order, event, operator, client, link, remote):
    _stage_unlist(event, link, remote)
    rejected = False
    try:
        jidanpu.release_order_to_home(client, remote_order_id=link.jidanpu_order_id)
    except ApiError as exc:
        rejected = type(exc.code) is int and exc.code == 0
    except JidanpuError as exc:
        if event.payload.get("operation_id") and isinstance(exc, UpstreamBlockedError):
            raise
    try:
        confirmed = direct._linked_order(client, order, link)
    except (JidanpuError, AssignmentError) as exc:
        if event.payload.get("operation_id") and isinstance(exc, UpstreamBlockedError):
            raise
        raise AssignmentError(HALL_PENDING) from exc
    if reason := _hall_block_reason(confirmed):
        _refuse(order, event, operator, reason)
    if rejected and _is_unassigned(confirmed, in_hall=True):
        _refuse(order, event, operator, "鸡蛋铺拒绝撤下大厅，本次未指派，请核对绑定账号的权限。")
    if not _is_unassigned(confirmed):
        raise AssignmentError(HALL_PENDING)
    return confirmed


def _recording_not_started(order, binding, event, *, new_reservation, had_link):
    return new_reservation and not had_link and not any(
        key in event.payload for key in ("recording_source_order_no", "recorded", "jidanpu_order_id", "remote_order_id", "link_id", "hall_fee")
    ) and jidanpu_member._link_for(binding, order) is None


def _execute(order, binding, operator, event, client, *, expected_fee, new_reservation):
    remote = None
    link = jidanpu_member._link_for(binding, order)
    had_link = link is not None
    verified = None
    new_remote = not had_link and "recording_source_order_no" not in event.payload
    # 已有原单的首轮预检失败时，本轮尚未对鸡蛋铺写入。
    clear_unstarted = new_reservation and order.status == OrderStatus.WAITING_GRAB and had_link
    retained_retry = new_reservation and had_link and event.payload.get("retained_draft") is True
    try:
        if order.penalty_approval_id is not None and (
            changes_approved_penalty_price(order, Decimal(event.payload["price"]))
            or changes_approved_penalty_price(order, Decimal(event.payload["fee"]))
        ):
            raise AssignmentError(PENALTY_AMOUNT_LOCKED)
        if not had_link:
            link, verified = _recorded_link(order, binding, event, client, now=timezone.now())
        remote = _remote_unassigned(order, link, client, event=event, allow_hall=had_link, verified=verified)
        if direct._fee_value(remote, "price") != Decimal(event.payload["price"]):
            raise AssignmentError(PRICE_CHANGED)
        if expected_fee is not None and _offered_fee(remote, event) != expected_fee:
            raise AssignmentError(direct.FEE_CHANGED)
        target = {"number": event.payload["number"], "name": event.payload["name"]}
    except jidanpu_member.RecordingRefused as exc:
        _refuse(order, event, operator, str(exc)[:255])
    except AssignmentError as exc:
        if retained_retry:
            _reject_assignment(order, event, operator, str(exc))
            raise
        if str(exc) == PENDING_REASON:
            raise
        if _recording_not_started(order, binding, event, new_reservation=new_reservation, had_link=had_link):
            _refuse(order, event, operator, str(exc))
        if clear_unstarted or isinstance(exc, HallOrderUnavailable) or (
            remote is not None and _is_unassigned(remote, in_hall=True)
        ):
            _refuse(order, event, operator, str(exc))
        if remote is not None:
            _ready_for_retry(event, remote)
            raise
        if link is not None or event.payload.get("candidate_remote_order_id"):
            raise
        _refuse(order, event, operator, str(exc))
    except JidanpuError as exc:
        if event.payload.get("operation_id") and isinstance(exc, UpstreamBlockedError):
            raise
        if retained_retry:
            _reject_assignment(order, event, operator, direct.READ_ERROR)
        elif clear_unstarted or _recording_not_started(order, binding, event, new_reservation=new_reservation, had_link=had_link):
            _resolve(order, event, operator=operator, reason=direct.READ_ERROR)
        raise

    try:
        fresh = remote if new_remote and verified is not None else direct._linked_order(client, order, link)
    except (AssignmentError, JidanpuError) as exc:
        if event.payload.get("operation_id") and isinstance(exc, UpstreamBlockedError):
            raise
        if retained_retry:
            reason = str(exc) if isinstance(exc, AssignmentError) else direct.READ_ERROR
            _reject_assignment(order, event, operator, reason)
        elif clear_unstarted:
            reason = str(exc) if isinstance(exc, AssignmentError) else direct.READ_ERROR
            _resolve(order, event, operator=operator, reason=reason)
        raise
    listed = _is_unassigned(remote, in_hall=True)
    if retained_retry and not _is_unassigned(fresh, in_hall=listed):
        reason = _hall_block_reason(fresh) or direct.CHANGED
        _reject_assignment(order, event, operator, reason)
        raise AssignmentError(reason)
    if (listed or clear_unstarted) and (reason := _hall_block_reason(fresh)):
        _refuse(order, event, operator, reason)
    if not _is_unassigned(fresh, in_hall=listed):
        if clear_unstarted:
            _refuse(order, event, operator, direct.CHANGED)
        raise AssignmentError(PENDING_REASON)
    if any(fresh.get(key) != remote.get(key) for key in ("gameName", "taskId", "price", "recFee")):
        if retained_retry:
            _reject_assignment(order, event, operator, direct.CHANGED)
        elif listed or clear_unstarted:
            _refuse(order, event, operator, direct.CHANGED)
        else:
            _ready_for_retry(event, fresh)
        raise AssignmentError(direct.CHANGED)
    if listed:
        confirmed = _unlist_for_assignment(order, event, operator, client, link, fresh)
        if any(confirmed.get(key) != fresh.get(key) for key in ("gameName", "taskId", "price")):
            if retained_retry:
                _reject_assignment(order, event, operator, direct.CHANGED)
            raise AssignmentError(direct.CHANGED)
        fresh = confirmed

    event.payload.update(
        phase="sending", link_id=link.pk, remote_order_id=link.jidanpu_order_id, name=target["name"],
    )
    jidanpu_member._persist_payload(event, event.payload)
    fee = Decimal(event.payload["fee"])
    try:
        with direct.assignment_stage(event, "assign"):
            confirmed = direct._send_assignment(
                client, link, target, fee=fee, remote=fresh, surface_blocked=bool(event.payload.get("operation_id")),
            )
    except direct.AssignmentRejected as exc:
        direct._remember_receipt(event, rejection=str(exc))
        _complete_from_refusal(order, event, operator)
        raise
    if confirmed:
        direct._remember_receipt(event)
        return _complete_from_receipt(order, binding, operator, event)
    try:
        with direct.assignment_stage(event, "readback"):
            remote, current = _assignment_result(order, event, client, link)
    except HallOrderUnavailable as exc:
        _refuse(order, event, operator, str(exc))
    if current is not None:
        assigned = _resolve(order, event, binding=binding, operator=operator, link=link)
        return direct._available_options(assigned, remote, current)
    raise AssignmentError(PENDING_REASON)


def _prepare_request(order, *, studio_id, studio_token, expected_number, expected_fee, fee):
    if expected_number:
        raise AssignmentError(direct.CHANGED)
    binding = _binding(order, studio_id)
    posted_fee = fee if fee is not None else expected_fee
    if posted_fee is None:
        raise AssignmentError("请填写付给鸡蛋铺打手的金额。")
    if not constant_time_compare(_studio_token(order, binding), studio_token):
        raise AssignmentError("鸡蛋铺工作室绑定已变化，请重新搜索打手。")
    return binding, posted_fee


def assign(order, *, operator, studio_id, studio_token, number, name, expected_number, expected_fee, expected_price,
           price=None, fee=None):
    request = dict(studio_id=studio_id, studio_token=studio_token, expected_number=expected_number,
                   expected_fee=expected_fee, fee=fee)
    binding, _ = _prepare_request(order, **request)
    with jidanpu_assignment_lock.sending(binding_id=binding.pk, order_id=order.pk) as busy:
        if busy:
            raise AssignmentError(busy)
        binding, posted_fee = _prepare_request(order, **request)
        try:
            with jidanpu.member_session(binding) as (client, _):
                order, event, new_reservation = _reserve(
                    order, binding, operator=operator, number=number, name=name, expected_price=expected_price,
                    price=price, fee=posted_fee,
                )
                return _execute(
                    order, binding, operator, event, client,
                    expected_fee=expected_fee, new_reservation=new_reservation,
                )
        except JidanpuError as exc:
            pending = _pending_event(order)
            if pending is not None and pending.status == JidanpuSyncEvent.Status.PENDING:
                raise AssignmentError(PENDING_REASON) from exc
            raise AssignmentError(direct.READ_ERROR) from exc


def _recorded_draft(order, event, client):
    payload = event.payload
    remote_id = payload.get("candidate_remote_order_id")
    if payload.get("phase") != "recording" or payload.get("recorded") is not True or not remote_id:
        return None
    if any(key in payload for key in ("remote_order_id", "link_id", "hall_fee")):
        return None
    source = jidanpu_member.effective_source_order_no(order)
    if payload.get("recording_source_order_no") != source:
        raise AssignmentError(direct.CHANGED)
    remote = jidanpu.fetch_order_by_key1(
        client, key=str(remote_id), expect_field="orderId", expect_value=str(remote_id),
    )
    if remote is None:
        raise AssignmentError(direct.READ_ERROR)
    if remote.get(jidanpu.OWN_SOURCE_VALUE_FIELD) != source:
        raise AssignmentError(direct.CHANGED)
    if payload.get("recording_remote_oid") is not None and str(remote.get("oid")) != str(payload["recording_remote_oid"]):
        raise AssignmentError(direct.OWNERSHIP_UNVERIFIED)
    return remote


def _recover_link(order, binding, event, client):
    link = jidanpu_member._link_for(binding, order)
    if "hall_fee" in event.payload and (
        link is None or (link.pk, link.jidanpu_order_id) != (event.payload["link_id"], event.payload["remote_order_id"])
    ):
        raise AssignmentError(direct.CHANGED)
    if link is not None:
        return link
    remote = _recorded_draft(order, event, client)
    if remote is not None and _hall_block_reason(remote) == HALL_CLOSED:
        raise HallOrderUnavailable(HALL_CLOSED)
    source = jidanpu_member.effective_source_order_no(order)
    if event.payload.get("recording_source_order_no", source) != source:
        raise AssignmentError(direct.CHANGED)
    saved = jidanpu.find_order_for_repair(
        client, remote_order_id=None, source_order_no=source,
        window=jidanpu.order_list_window(today=timezone.localdate()),
    )
    if saved is None:
        if any(event.payload.get(key) for key in (
            "recording_source_order_no", "recorded", "candidate_remote_order_id", "remote_order_id",
        )):
            raise AssignmentError(direct.READ_ERROR)
        return None
    remote_id = str(saved.get("orderId") or "")
    if not remote_id or saved.get("isThrowOrder") not in ("Y", "N"):
        raise AssignmentError(direct.READ_ERROR)
    _check_recording_owner(order, event, client, remote_id, saved)
    link, _ = jidanpu_member._ensure_link(
        binding, order, remote_id=remote_id, in_hall=saved["isThrowOrder"] == "Y", now=timezone.now(),
    )
    if link is None:
        raise AssignmentError(direct.CHANGED)
    return link


def verify(order, *, operator, pending_ref, close_unconfirmed=False, session=None):
    session = session or jidanpu.member_session
    event = _pending_event(order)
    if event is None or str(event.pk) != pending_ref:
        raise AssignmentError(direct.CHANGED)
    studio_id = event.payload["member_binding_id"]
    with jidanpu_assignment_lock.sending(binding_id=studio_id, order_id=order.pk) as busy:
        if busy:
            raise AssignmentError(busy)
        order.refresh_from_db(fields=["peer_direct_pending"])
        if not is_pending(order) or order.peer_direct_pending["event_id"] != event.pk:
            raise AssignmentError(direct.CHANGED)
        event.refresh_from_db(fields=["payload", "status"])
        unlisted = False
        try:
            binding = _binding(order, studio_id)
            if direct._binding_identity(binding) != event.payload["binding_identity"]:
                raise AssignmentError(UNAVAILABLE)
            if event.payload["phase"] == direct.RECEIPT_CONFIRMED:
                return _complete_from_receipt(order, binding, operator, event)
            if event.payload["phase"] == direct.RECEIPT_REJECTED:
                return _complete_from_refusal(order, event, operator)
            with session(binding) as (client, _):
                phase = event.payload["phase"]
                if close_unconfirmed and jidanpu_member._link_for(binding, order) is None:
                    draft = _recorded_draft(order, event, client)
                    if draft is not None and _hall_block_reason(draft) == HALL_CLOSED:
                        raise HallOrderUnavailable(HALL_CLOSED)
                    if draft is not None and _is_unassigned(draft):
                        logs = jidanpu.fetch_ass_logs(client, remote_order_id=str(draft["orderId"]))
                        if logs:
                            raise AssignmentError(RELEASE_BLOCKED)
                        _resolve(order, event, operator=operator, reason="已结束等待，未发送指派，鸡蛋铺原单已保留。")
                        return _options(order, studios=[_studio(binding)], reason="已结束等待，鸡蛋铺原单已保留，可返回本工作室重新指派或删除。", fee=None)
                if phase == "sending":
                    link = _recover_link(order, binding, event, client)
                else:
                    with direct.assignment_stage(event, "readback"):
                        link = _recover_link(order, binding, event, client)
                        remote = _remote_unassigned(order, link, client, event=event, allow_hall=True) if link else None
                if phase == "sending":
                    if link is None:
                        raise AssignmentError(direct.READ_ERROR)
                    with direct.assignment_stage(event, "readback"):
                        remote, current = _assignment_result(order, event, client, link)
                    if current is not None:
                        assigned = _resolve(order, event, binding=binding, operator=operator, link=link)
                        return direct._available_options(assigned, remote, current)
                    if not close_unconfirmed:
                        raise AssignmentError(direct.PENDING_NOT_MATCHED)
                    if not _is_unassigned(remote):
                        raise AssignmentError(RELEASE_BLOCKED)
                    _ready_for_retry(event, remote)
                    return _ready_options(order, event)
                if link is not None:
                    listed = _is_unassigned(remote, in_hall=True)
                    if phase == "unlisting" and listed:
                        raise AssignmentError(HALL_VERIFY_STILL_LISTED)
                    if (not close_unconfirmed or "hall_fee" not in event.payload) and (
                        direct._fee_value(remote, "price") != Decimal(event.payload["price"])
                    ):
                        raise AssignmentError(PRICE_CHANGED)
                    if not close_unconfirmed:
                        _ready_for_retry(event, remote)
                        return _ready_options(order, event)
                    unlisted = "hall_fee" in event.payload and not listed
        except HallOrderUnavailable as exc:
            _resolve(order, event, operator=operator, reason=str(exc))
            return _options(order, studios=[_studio(binding)], reason=str(exc), fee=None)
        except JidanpuError as exc:
            raise AssignmentError(direct.READ_ERROR) from exc
        if not close_unconfirmed:
            raise AssignmentError(direct.PENDING_NOT_MATCHED)
        _resolve(order, event, binding=binding, operator=operator, unlisted=unlisted,
                 reason="已结束等待，未确认鸡蛋铺指派成功。")
        order.refresh_from_db(fields=["peer_direct_pending"])
    return options(order) or direct.options(order, can_view_price=True)
