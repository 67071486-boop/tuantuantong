"""Direct assignment on the current taker's already-linked downstream order."""

import json
import logging
from contextlib import contextmanager
from decimal import Decimal
from hashlib import sha256
from time import monotonic

from django.core.cache import cache
from django.db import connection, transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from apps.audit.log import record
from apps.audit.models import LogType
from apps.business import jidanpu, jidanpu_assignment_lock, jidanpu_member
from apps.business.jidanpu import assign_order as send_remote_assignment
from apps.business.models import JidanpuMemberBinding, JidanpuMemberOrderLink, JidanpuSyncEvent, Order, OrderStatus
from apps.business.penalty_amounts import PENALTY_AMOUNT_LOCKED, changes_approved_penalty_price
from apps.business.order_writes import lock_order_row
from apps.business.peer_sync import JIDANPU_DIRECT_PENDING_PLATFORM
from jidanpu.errors import ApiError, AuthError, JidanpuError, RateLimitError, SessionExpiredError, UpstreamBlockedError


KIND = "member.direct_assign"
logger = logging.getLogger(__name__)
AUDIT_ACTION = "order.jidanpu_direct_assign"
MAX_CANDIDATE_PAGES = 10
MAX_SEARCH_RESULTS = 40
OPTIONS_LOOKUP_CACHE_SECONDS = 8
READ_ERROR = "暂时无法核对鸡蛋铺订单和接手人，请重试。"
UNKNOWN = "鸡蛋铺尚未确认指派结果。请核对接手人和金额后重试查询，团团通不会重复提交。"
PENDING_NOT_MATCHED = "鸡蛋铺当前接手人或金额尚未与原指派一致。请在鸡蛋铺核对后再查，团团通不会重复提交。"
CHANGED = "订单或当前接手人已变化，请重新打开指派窗口。"
FEE_CHANGED = "鸡蛋铺上的金额已变化，请重新核对后再指派。"
OWNERSHIP_UNVERIFIED = "鸡蛋铺这张单的录单人无法核实为当前绑定账号。请在下家核对录单人后重新搜索。"


class AssignmentError(Exception):
    pass


class AssignmentRejected(AssignmentError):
    pass


RECEIPT_CONFIRMED = "confirmed"
RECEIPT_REJECTED = "rejected"


@contextmanager
def assignment_stage(event, stage):
    start = monotonic()
    try:
        yield
    finally:
        logger.info(
            "jidanpu_direct_assignment tenant=%s event=%s stage=%s elapsed_ms=%d",
            connection.schema_name, event.pk, stage, round((monotonic() - start) * 1000),
        )


def _context(order, *, lock=False):
    taker_id = jidanpu_member._main_taker_id(order.pk)
    bindings = JidanpuMemberBinding.objects.filter(user_id=taker_id, enabled=True, user__is_active=True)
    links = JidanpuMemberOrderLink.objects.filter(order_id=order.pk, withdrawn_at=None)
    if lock:
        bindings = bindings.select_for_update(of=("self",))
        links = links.select_for_update()
    binding = bindings.first()
    if binding is None:
        return None, None, "当前接手人未启用鸡蛋铺下家绑定。"
    if binding.mode != JidanpuMemberBinding.Mode.PASSWORD or jidanpu_member.credentials_missing(binding):
        return binding, None, "查询和指派打手需要下家启用有指派权限的鸡蛋铺网页账号。"
    link = links.filter(binding_id=binding.pk).first()
    if link is None:
        return binding, None, "订单尚未同步到当前接手人的鸡蛋铺。请等待原有订单同步后重试。"
    return binding, link, ""


def _linked_order(client, order, link):
    remote_id = link.jidanpu_order_id
    row = jidanpu.fetch_order_by_key1(client, key=remote_id, expect_field="orderId", expect_value=remote_id)
    if not row or str(row.get("orderId")) != remote_id:
        raise AssignmentError(READ_ERROR)
    if row.get(jidanpu.OWN_SOURCE_VALUE_FIELD) != jidanpu_member.effective_source_order_no(order):
        raise AssignmentError("鸡蛋铺订单的来源单号尚未核对一致，请确认同步状态后重试。")
    return row


def _assert_recording_owner(client, order, *, remote_id):
    uid = jidanpu.fetch_login_uid(client)
    recorded = jidanpu.fetch_order_recorded_by(client, remote_order_id=remote_id, user_id=uid)
    if not recorded or recorded.get(jidanpu.OWN_SOURCE_VALUE_FIELD) != jidanpu_member.effective_source_order_no(order):
        raise AssignmentError(OWNERSHIP_UNVERIFIED)
    other_uid = "2147483647" if uid != "2147483647" else "2147483646"
    if jidanpu.fetch_order_recorded_by(client, remote_order_id=remote_id, user_id=other_uid) is not None:
        raise AssignmentError(OWNERSHIP_UNVERIFIED)
    return uid


def _current(client, remote, link, *, expected_name=None):
    state = jidanpu_member._order_sta_token(remote)
    if state == jidanpu.ORDER_UNASSIGNED_LABEL or remote.get("isThrowOrder") == "Y":
        return None, None
    name = remote.get("recManName")
    if (remote.get("isThrowOrder") != "N" or not jidanpu_member._is_remote_assignment_state(state)
            or not isinstance(name, str) or not name.strip()):
        raise AssignmentError(READ_ERROR)
    name = name.strip()
    if expected_name is not None and name != expected_name:
        return None, None
    # The order row has a display name only. A matching name needs the exact order's assignment record.
    logs = jidanpu.fetch_ass_logs(client, remote_order_id=link.jidanpu_order_id)
    row = jidanpu_member._current_taker_row(logs, name)
    if row is None or not (uid := jidanpu._positive_uid(row.get("userId"))):
        raise AssignmentError("暂时无法核对唯一的鸡蛋铺接手人编号，请在下家确认当前接手人。")
    return {"name": name, "number": uid}, _fee_value(row, "fee")


def _write_block(order, remote, *, event=None):
    if order.peer_direct_pending and not _owns_pending_marker(order, event):
        return "正在等待下家工作室确认，请先处理已有指派。"
    if order.status != OrderStatus.IN_PROGRESS:
        return "本地订单须在代练中才能指派下家打手。"
    state = jidanpu_member._order_sta_token(remote)
    if remote.get("isThrowOrder") != "N" or (
        state != jidanpu.ORDER_UNASSIGNED_LABEL and jidanpu_member.map_remote_status(state) != OrderStatus.IN_PROGRESS
    ):
        return "鸡蛋铺订单状态暂不支持指派，请确认订单未在下家大厅且仍可指派。"
    return ""


def _fee_value(row, field):
    amount = jidanpu_member._remote_amount_value(row, field)
    if amount is None or not amount.is_finite() or abs(amount) > Decimal("9999999999.99"):
        return None
    rounded = amount.quantize(Decimal("0.01"))
    return rounded if amount == rounded else None


def _options_cache_key(order, binding, link, latest, *, can_view_price):
    scope = (
        connection.alias, connection.settings_dict["NAME"], connection.schema_name,
        order.pk, order.order_no, order.status, order.updated_at, jidanpu_member.effective_source_order_no(order),
        binding.pk, _binding_identity(binding), binding.password,
        link.pk, link.jidanpu_order_id, link.in_hall, link.last_in_at, link.last_out_at,
        (latest.pk, latest.status, latest.processed_at) if latest is not None else None,
        can_view_price,
    )
    digest = salted_hmac("jidanpu-direct-options", json.dumps(scope, default=str), algorithm="sha256").hexdigest()
    return f"jidanpu-direct-options:{digest}"


def options(order, *, can_view_price, refresh=False):
    binding, link, reason = _context(order)
    if link is None:
        return _options(bound=binding is not None, reason=reason)
    cache_key = None
    if not order.peer_direct_pending:
        latest = _latest(order, binding, link)
        if latest is None or (
            latest.status != JidanpuSyncEvent.Status.PENDING
            and _latest(order, binding, link, pending_only=True) is None
        ):
            cache_key = _options_cache_key(order, binding, link, latest, can_view_price=can_view_price)
            if refresh:
                cache.delete(cache_key)
            elif (cached := cache.get(cache_key)) is not None:
                return cached
    try:
        with jidanpu.member_session(binding) as (client, _):
            remote = _linked_order(client, order, link)
            current, _ = _current(client, remote, link)
            pending = _latest(order, binding, link, pending_only=True)
            result = _available_options(order, remote, current, can_view_price=can_view_price, pending=pending)
            if cache_key is not None and current is not None and pending is None:
                cache.set(cache_key, result, OPTIONS_LOOKUP_CACHE_SECONDS)
            return result
    except (JidanpuError, AssignmentError) as exc:
        if can_view_price and (pending := _latest(order, binding, link, pending_only=True)) is not None:
            return _options(bound=True, reason=UNKNOWN, pending=pending)
        if isinstance(exc, AssignmentError):
            raise
        raise AssignmentError(READ_ERROR) from exc


def _options(*, bound, current=None, fee=None, reason="", pending=None):
    return {"bound": bound, "current": current, "assignable": bound and not reason and not pending,
            "reason": reason, "fee": str(fee) if fee is not None else None, "pending": str(pending.pk) if pending else None}


def _available_options(order, remote, current, *, can_view_price=True, pending=None):
    if not can_view_price:
        return _options(bound=True, current=current, reason="需要查看发单价的权限才能指派鸡蛋铺打手。")
    return _options(bound=True, current=current, fee=_fee_value(remote, "recFee"),
                    reason=UNKNOWN if pending else _write_block(order, remote), pending=pending)


def _candidates(client, remote, *, key=""):
    game_name = remote.get("gameName")
    if not isinstance(game_name, str) or not game_name.strip():
        raise AssignmentError("鸡蛋铺订单缺少游戏信息，暂时无法核对可指派人选。")
    task_id = str(remote.get("taskId") or "0")
    limit = jidanpu.ASSIGNABLE_USERS_PAGE_SIZE
    seen = set()
    previous_total = None
    for page in range(1, MAX_CANDIDATE_PAGES + 1):
        rows = jidanpu.fetch_assignable_users(
            client, page=page, page_size=limit, game_name=game_name, task_id=task_id, key=key,
        )
        envelope = client.last_envelope or {}
        total = envelope.get("count")
        if (type(envelope.get("code")) is not int or envelope["code"] != 0
                or type(total) is not int or total < 0 or (previous_total is not None and total != previous_total)
                or len(rows) != min(limit, max(total - (page - 1) * limit, 0))):
            raise AssignmentError("鸡蛋铺未返回完整打手名单，请重试或核对绑定账号的指派权限。")
        previous_total = total
        for row in rows:
            number = jidanpu._positive_uid(row.get("id"))
            name = row.get("userName")
            if not number or not isinstance(name, str) or not name.strip() or row.get(jidanpu.ASSIGNABLE_ACTIVE_FIELD) != "Y":
                continue
            if number in seen:
                raise AssignmentError("读取名单期间打手名单发生变化，请重新搜索。")
            seen.add(number)
            yield {"number": number, "name": name.strip()}
        if total <= page * limit:
            return
    raise AssignmentError("鸡蛋铺打手名单过长，暂未完成整份名单的核对。")


def search(order, query):
    binding, link, reason = _context(order)
    if link is None:
        raise AssignmentError(reason)
    if _latest(order, binding, link, pending_only=True) is not None:
        raise AssignmentError(UNKNOWN)
    try:
        with jidanpu.member_session(binding) as (client, _):
            remote = _linked_order(client, order, link)
            if reason := _write_block(order, remote):
                raise AssignmentError(reason)
            _assert_recording_owner(client, order, remote_id=link.jidanpu_order_id)
            return _search_candidates(client, remote, query)
    except JidanpuError as exc:
        raise AssignmentError("读不到鸡蛋铺打手名单，请重试或核对绑定账号的指派权限。") from exc


def _search_candidates(client, remote, query):
    numeric = query.isascii() and query.isdigit()
    folded = query.casefold()
    items = []
    for item in _candidates(client, remote, key="" if numeric else query):
        if numeric and item["number"] == query:
            return {"items": [item], "more": False}
        if (numeric and query in item["number"]) or folded in item["name"].casefold():
            items.append(item)
            if not numeric and len(items) > MAX_SEARCH_RESULTS:
                break
    items.sort(key=lambda item: (item["name"].casefold(), item["number"]))
    return {"items": items[:MAX_SEARCH_RESULTS], "more": len(items) > MAX_SEARCH_RESULTS}


def _events(order):
    return JidanpuSyncEvent.objects.filter(
        direction=JidanpuSyncEvent.Direction.OUT, kind=KIND, target_type="order", target_ref=order.order_no,
    )


def _binding_identity(binding):
    # The binding settings use site, account, and mode to identify a rebind.
    identity = json.dumps([binding.jidanpu_base_url, binding.account, binding.mode])
    return sha256(identity.encode()).hexdigest()


def _latest(order, binding, link, *, pending_only=False):
    events = _events(order).filter(
        payload__member_binding_id=binding.pk, payload__binding_identity=_binding_identity(binding),
        payload__link_id=link.pk, payload__remote_order_id=link.jidanpu_order_id,
    )
    if pending_only:
        events = events.filter(status=JidanpuSyncEvent.Status.PENDING)
    return events.first()


@transaction.atomic
def _finish(event, status, reason):
    changed = JidanpuSyncEvent.objects.filter(pk=event.pk, status=JidanpuSyncEvent.Status.PENDING).update(
        status=status, reason=reason, processed_at=timezone.now(),
    )
    if changed and event.payload.get("queued_linked"):
        Order.all_objects.filter(
            order_no=event.target_ref, peer_direct_pending__platform=JIDANPU_DIRECT_PENDING_PLATFORM,
            peer_direct_pending__event_id=event.pk, peer_direct_pending__linked=True,
        ).update(peer_direct_pending=None)
    return changed


def _record_assignment_outcome(order, *, actor, actor_id, reason=""):
    record(log_type=LogType.ORDER, action=AUDIT_ACTION, actor=actor, actor_id=actor_id,
           order_no=order.order_no, target_kind="order", target_id=order.order_no, reason=reason)


@transaction.atomic
def _confirm_direct_assignment(order, link, event):
    reason = "鸡蛋铺已返回指派成功。" if event.payload["phase"] == RECEIPT_CONFIRMED else "鸡蛋铺已回读确认接手人和金额。"
    if not _finish(event, JidanpuSyncEvent.Status.DONE, reason):
        if not JidanpuSyncEvent.objects.filter(pk=event.pk, status=JidanpuSyncEvent.Status.DONE).exists():
            raise AssignmentError(CHANGED)
        return
    JidanpuMemberOrderLink.objects.filter(
        pk=link.pk, jidanpu_order_id=link.jidanpu_order_id, withdrawn_at=None,
    ).update(in_hall=False, last_out_at=timezone.now())
    _record_assignment_outcome(order, actor=event.payload["operator"], actor_id=event.payload["operator_id"])


def _remember_receipt(event, *, rejection=None):
    phase = RECEIPT_CONFIRMED if rejection is None else RECEIPT_REJECTED
    payload = {**event.payload, "phase": phase}
    if rejection is not None:
        payload["rejection_message"] = rejection
    saved = JidanpuSyncEvent.objects.filter(
        pk=event.pk, status=JidanpuSyncEvent.Status.PENDING, payload__phase="sending",
    ).update(payload=payload)
    if not saved:
        raise AssignmentError(CHANGED)
    event.payload = payload


def _receipt_options(event):
    current = {"number": event.payload["number"], "name": event.payload["name"]}
    return {**_options(bound=True, current=current), "assignable": False}


def _complete_from_refusal(event):
    reason = event.payload["rejection_message"]
    _finish(event, JidanpuSyncEvent.Status.FAILED, reason[:255])
    return _options(bound=True, reason=reason)


def _owns_pending_marker(order, event):
    marker = order.peer_direct_pending or {}
    return (
        event is not None and marker.get("platform") == JIDANPU_DIRECT_PENDING_PLATFORM
        and marker.get("linked") is True and marker.get("event_id") == event.pk
    )


def _locked_context(order, binding, link, *, event=None):
    locked = lock_order_row(order)
    if (locked is None or locked.status != OrderStatus.IN_PROGRESS or locked.hall_price != order.hall_price
            or locked.source_order_no != order.source_order_no
            or (locked.peer_direct_pending and not _owns_pending_marker(locked, event))
            or locked.game_id != order.game_id or locked.created_by != order.created_by
            or locked.hall_card_id is not None or locked.upstream_hall_card_id is not None):
        raise AssignmentError(CHANGED)
    current_binding, current_link, _ = _context(locked, lock=True)
    if current_link is None or (
        current_link.pk, current_link.jidanpu_order_id, current_binding.pk,
        current_binding.jidanpu_base_url, current_binding.account, current_binding.password,
    ) != (link.pk, link.jidanpu_order_id, binding.pk, binding.jidanpu_base_url, binding.account, binding.password):
        raise AssignmentError(CHANGED)
    return locked


@transaction.atomic
def _complete_from_receipt(order, binding, link, event):
    locked = _locked_context(order, binding, link, event=event)
    _confirm_direct_assignment(locked, link, event)
    return _receipt_options(event)


@transaction.atomic
def _reserve(order, binding, link, *, last_event_id, operator, target, fee, queued=None):
    locked = _locked_context(order, binding, link)
    if changes_approved_penalty_price(locked, fee):
        raise AssignmentError(PENALTY_AMOUNT_LOCKED)
    latest = _latest(order, binding, link)
    if latest is not None and latest.status == JidanpuSyncEvent.Status.PENDING:
        raise AssignmentError(UNKNOWN)
    if (latest.pk if latest is not None else None) != last_event_id:
        raise AssignmentError(CHANGED)
    # Commit the intent before the remote call so a lost response cannot trigger a second write.
    event = JidanpuSyncEvent.objects.create(
        direction=JidanpuSyncEvent.Direction.OUT, kind=KIND, target_type="order", target_ref=order.order_no,
        attempts=1, reason="等待鸡蛋铺确认指派结果。",
        payload={
            "member_binding_id": binding.pk, "binding_identity": _binding_identity(binding),
            "link_id": link.pk, "remote_order_id": link.jidanpu_order_id,
            "number": target["number"], "name": target["name"], "fee": str(fee),
            "operator_id": operator.pk, "operator": operator.username, "phase": "reserved",
            **(queued or {}),
        },
    )
    if queued:
        locked.peer_direct_pending = {
            "platform": JIDANPU_DIRECT_PENDING_PLATFORM, "event_id": event.pk, "linked": True,
            "studio_id": 0, "studio_name": "", "account_name": "", "price": str(order.hall_price),
            "operation_id": queued["operation_id"], "target_ref": locked.order_no, "binding_base_url": "",
        }
        locked.save(update_fields=["peer_direct_pending"])
    return event


@transaction.atomic
def _start_sending(event, order, binding, link):
    if event.payload["phase"] not in ("reserved", "queued"):
        return False
    try:
        locked = _locked_context(order, binding, link, event=event if event.payload.get("queued_linked") else None)
    except AssignmentError:
        _finish(event, JidanpuSyncEvent.Status.SKIPPED, "订单关联已变化，请求未发出。")
        return False
    if locked.penalty_approval_id is not None and changes_approved_penalty_price(
        locked, Decimal(event.payload["fee"]),
    ):
        _finish(event, JidanpuSyncEvent.Status.SKIPPED, PENALTY_AMOUNT_LOCKED)
        return False
    payload = {**event.payload, "phase": "sending"}
    updated = JidanpuSyncEvent.objects.filter(
        pk=event.pk, status=JidanpuSyncEvent.Status.PENDING, payload__phase=event.payload["phase"],
    ).update(payload=payload)
    if updated:
        event.payload = payload
    return bool(updated)


def _check_expected(order, remote, current_number, expected_number, expected_fee, *, event=None):
    if current_number != expected_number:
        raise AssignmentError(CHANGED)
    if reason := _write_block(order, remote, event=event):
        raise AssignmentError(reason)
    if _fee_value(remote, "recFee") != expected_fee:
        raise AssignmentError(FEE_CHANGED)


def _submit_jidanpu_assignment(
    order, binding, link, client, *, operator, number, name, expected_number, expected_fee, fee, event=None,
):
    latest = _latest(order, binding, link)
    if latest is not None and latest.status == JidanpuSyncEvent.Status.PENDING and event != latest:
        raise AssignmentError(UNKNOWN)
    remote = _linked_order(client, order, link)
    current, current_fee = _current(client, remote, link)
    current_number = current["number"] if current else ""
    posted_fee = fee if fee is not None else expected_fee
    if current_number == number:
        if current_number == expected_number:
            raise AssignmentError("该打手已经是当前接手人，无需再次指派。")
        if current_fee is None or current_fee != posted_fee:
            raise AssignmentError(FEE_CHANGED)
        if event is not None:
            _confirm_direct_assignment(order, link, event)
            order.peer_direct_pending = None
        return _available_options(order, remote, current)
    _check_expected(order, remote, current_number, expected_number, expected_fee, event=event)
    if posted_fee is None:
        raise AssignmentError("鸡蛋铺订单没有可用的默认金额，请填写付给下家打手的金额。")
    _assert_recording_owner(client, order, remote_id=link.jidanpu_order_id)
    target = {"number": number, "name": name}

    fresh = _linked_order(client, order, link)
    fresh_current, _ = _current(client, fresh, link)
    _check_expected(order, fresh, fresh_current["number"] if fresh_current else "", expected_number, expected_fee, event=event)
    if any(fresh.get(key) != remote.get(key) for key in ("gameName", "taskId")):
        raise AssignmentError(CHANGED)
    with jidanpu_assignment_lock.sending(binding_id=binding.pk, order_id=order.pk) as busy_reason:
        if busy_reason:
            raise AssignmentError(busy_reason)
        if event is None:
            event = _reserve(
                order, binding, link, last_event_id=latest.pk if latest else None, operator=operator,
                target=target, fee=posted_fee,
            )
        if not _start_sending(event, order, binding, link):
            raise AssignmentError(CHANGED)
        try:
            with assignment_stage(event, "assign"):
                confirmed = _send_assignment(
                    client, link, target, fee=posted_fee, remote=fresh, surface_blocked=bool(event.payload.get("operation_id")),
                )
        except AssignmentRejected as exc:
            _remember_receipt(event, rejection=str(exc))
            _complete_from_refusal(event)
            raise
        if confirmed:
            _remember_receipt(event)
            return _complete_from_receipt(order, binding, link, event)
        return _recover_assignment(client, order, link, event, target, fee=posted_fee)


def _send_assignment(client, link, target, *, fee, remote, surface_blocked=False) -> bool:
    try:
        hours = remote.get("hours")
        project = remote.get("project")
        send_remote_assignment(
            client, remote_order_id=link.jidanpu_order_id, user_id=target["number"],
            user_name=target["name"], fee=fee, hours=hours if type(hours) in (str, int, float) else None,
            project=project if isinstance(project, str) else "",
        )
    except SessionExpiredError as exc:
        if exc.upstream_message:
            raise AssignmentRejected(exc.upstream_message) from None
        return False
    except AuthError as exc:
        raise AssignmentRejected(exc.upstream_message or "鸡蛋铺拒绝授权，请核对下家账号权限。") from None
    except RateLimitError as exc:
        raise AssignmentRejected(exc.upstream_message or "鸡蛋铺限制了本次请求，请稍后重试。") from None
    except ApiError as exc:
        body = exc.body if isinstance(exc.body, dict) else {}
        message = next(
            (body[key].strip() for key in ("msg", "message", "errMsg", "error")
             if isinstance(body.get(key), str) and body[key].strip()), "",
        )
        rejected = type(exc.code) is int and exc.code == 0 and exc.status_code in (None, 200)
        if rejected or (message and exc.status_code is not None and 400 <= exc.status_code < 500 and exc.status_code != 408):
            raise AssignmentRejected(
                message or "鸡蛋铺拒绝了这次指派，请核对下家账号权限和选中打手的接单资格。"
            ) from None
    except JidanpuError as exc:
        if surface_blocked and isinstance(exc, UpstreamBlockedError):
            raise
    else:
        return True
    return False


def _recover_assignment(client, order, link, event, target, *, fee):
    with assignment_stage(event, "readback"):
        try:
            remote = _linked_order(client, order, link)
            current, current_fee = _current(client, remote, link, expected_name=target["name"])
        except (JidanpuError, AssignmentError) as exc:
            if event.payload.get("operation_id") and isinstance(exc, UpstreamBlockedError):
                raise
            raise AssignmentError(UNKNOWN) from exc
    if current is not None and current["number"] == target["number"]:
        if current_fee != fee:
            raise AssignmentError(UNKNOWN)
        _confirm_direct_assignment(order, link, event)
        if event.payload.get("queued_linked"):
            order.peer_direct_pending = None
        return _available_options(order, remote, current)
    raise AssignmentError(UNKNOWN)


def assign_jidanpu_taker(order, *, operator, number, name, expected_number, expected_fee, fee=None):
    binding, link, reason = _context(order)
    if link is None:
        raise AssignmentError(reason)
    try:
        with jidanpu.member_session(binding) as (client, _):
            return _submit_jidanpu_assignment(
                order, binding, link, client, operator=operator, number=number, name=name,
                expected_number=expected_number, expected_fee=expected_fee, fee=fee,
            )
    except JidanpuError as exc:
        raise AssignmentError(READ_ERROR) from exc


@transaction.atomic
def _resolve_pending(order, binding, link, pending, current, fee, *, operator, close_unconfirmed):
    if not jidanpu_assignment_lock.lock_order_change(order.pk):
        raise AssignmentError(jidanpu_assignment_lock.ORDER_BUSY)
    pending.refresh_from_db(fields=["status", "payload"])
    if pending.status == JidanpuSyncEvent.Status.DONE:
        return _receipt_options(pending) if pending.payload["phase"] == RECEIPT_CONFIRMED else None
    if pending.status != JidanpuSyncEvent.Status.PENDING:
        raise AssignmentError(CHANGED)
    if pending.payload["phase"] == RECEIPT_CONFIRMED:
        return _complete_from_receipt(order, binding, link, pending)
    if pending.payload["phase"] == RECEIPT_REJECTED:
        return _complete_from_refusal(pending)
    if pending.payload["phase"] in ("reserved", "queued"):
        reason = "原请求未发出，已结束等待。"
    elif current is not None and current["number"] == pending.payload["number"] and fee == Decimal(pending.payload["fee"]):
        _confirm_direct_assignment(order, link, pending)
        return
    elif close_unconfirmed:
        reason = "人工核对后结束等待，未确认远端成功。"
    else:
        raise AssignmentError(PENDING_NOT_MATCHED)
    if _finish(pending, JidanpuSyncEvent.Status.SKIPPED, reason):
        _record_assignment_outcome(order, actor=operator.username, actor_id=operator.pk, reason=reason)


def verify_jidanpu_assignment(order, *, operator, pending_ref, close_unconfirmed=False):
    binding, link, reason = _context(order)
    if link is None:
        raise AssignmentError(reason)
    pending = _latest(order, binding, link, pending_only=True)
    if pending is None or str(pending.pk) != pending_ref:
        raise AssignmentError("当前订单关联没有待核对的鸡蛋铺指派，请重新打开指派窗口。")
    if pending.payload["phase"] in (RECEIPT_CONFIRMED, RECEIPT_REJECTED):
        with jidanpu_assignment_lock.sending(binding_id=binding.pk, order_id=order.pk) as busy:
            if busy:
                raise AssignmentError(busy)
            if pending.payload["phase"] == RECEIPT_REJECTED:
                return _complete_from_refusal(pending)
            return _complete_from_receipt(order, binding, link, pending)
    if pending.payload["phase"] in ("reserved", "queued"):
        saved = _resolve_pending(order, binding, link, pending, None, None, operator=operator, close_unconfirmed=False)
        if saved is not None:
            return saved
        try:
            return options(order, can_view_price=True, refresh=True)
        except AssignmentError:
            return _options(bound=True, reason=READ_ERROR)
    try:
        with jidanpu.member_session(binding) as (client, _):
            remote = _linked_order(client, order, link)
            expected_name = None if close_unconfirmed else pending.payload["name"]
            current, fee = _current(client, remote, link, expected_name=expected_name)
    except JidanpuError as exc:
        raise AssignmentError(READ_ERROR) from exc
    saved = _resolve_pending(order, binding, link, pending, current, fee, operator=operator, close_unconfirmed=close_unconfirmed)
    if saved is not None:
        return saved
    if pending.payload.get("queued_linked"):
        order.refresh_from_db(fields=["peer_direct_pending"])
    return _available_options(order, remote, current)
