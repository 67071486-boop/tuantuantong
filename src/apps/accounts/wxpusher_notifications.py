"""Create and deliver personal mobile notifications within the current tenant."""

import re
from collections import Counter
from datetime import timedelta
from time import monotonic, sleep

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from django_tenants.utils import get_public_schema_name

from apps.accounts.models import WxPusherBinding, WxPusherNotice, WxPusherPreference
from apps.accounts.wxpusher_config import load_wxpusher_config
from apps.accounts.wxpusher_notice_templates import (
    CHAT_KINDS, DIRECT_MESSAGE, GROUP_CHAT_KINDS, GROUP_MENTION, GROUP_MESSAGE, GROUP_REPLY,
    HALL_NEW_ORDER, NOTICE_TEMPLATES, ORDER_EVENTS, SETTLEMENT_KINDS, STAFF_KINDS,
    order_event_key, render_notice,
)
from apps.business.models import EmpType, Performance
from apps.business.roles import WORKER
from apps.core.wxpusher import WxPusherClient, WxPusherError
from apps.core.wxpusher_app_links import DAIDAITONG_ORIGIN
from apps.rbac.permissions import nodes_of

_HOST = re.compile(
    r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
    re.ASCII | re.IGNORECASE,
)

_HALL_BATCH_SIZE = 1000
_CHAT_WINDOW = timedelta(seconds=45)
_CHAT_PRIORITY = {DIRECT_MESSAGE: 0, GROUP_MESSAGE: 0, GROUP_REPLY: 1, GROUP_MENTION: 2}


class InvalidNoticeDomain(ValueError):
    pass


class QueuedSendPacer:
    """Share a one second request interval across the worker's rounds and tenants."""

    def __init__(self):
        self.next_request_at = 0

    def remaining(self):
        return max(0, self.next_request_at - monotonic())

    def wait(self):
        remaining = self.remaining()
        if remaining > 0:
            sleep(remaining)

    def starting_request(self):
        self.next_request_at = monotonic() + 1


def _preference(user_id):
    return WxPusherPreference.objects.filter(user_id=user_id).first() or WxPusherPreference(user_id=user_id)


def _preference_payload(preference):
    return {
        "order_enabled": preference.order_enabled,
        "settlement_enabled": preference.settlement_enabled,
        "group_chat_enabled": True,
        "order_events": {kind: kind not in preference.disabled_order_events for kind in ORDER_EVENTS},
    }


def get_preferences(user_id):
    return _preference_payload(_preference(user_id))


@transaction.atomic
def update_preferences(user_id, changes):
    preference, _ = WxPusherPreference.objects.select_for_update().get_or_create(user_id=user_id)
    for field in ("order_enabled", "settlement_enabled"):
        if field in changes:
            setattr(preference, field, changes[field])
    disabled = set(preference.disabled_order_events)
    for kind, enabled in changes.get("order_events", {}).items():
        if enabled:
            disabled.discard(kind)
        else:
            disabled.add(kind)
    preference.disabled_order_events = sorted(disabled)
    preference.save(update_fields=["order_enabled", "settlement_enabled", "disabled_order_events"])
    return _preference_payload(preference)


def _wants_notice(user_id, kind, *, staff):
    if kind in CHAT_KINDS:
        return True
    preference = _preference(user_id)
    if staff:
        return preference.staff_enabled and kind not in preference.disabled_staff_events
    if kind == HALL_NEW_ORDER:
        return preference.hall_enabled_since is not None
    if kind in SETTLEMENT_KINDS:
        return preference.settlement_enabled
    return preference.order_enabled and order_event_key(kind) not in preference.disabled_order_events


def _binding(user_id, app_id):
    return WxPusherBinding.objects.filter(
        Q(uid__isnull=False) | Q(scan_bound_uid__isnull=False),
        user_id=user_id, app_id=app_id,
    ).first()


def _eligible_worker(user_id):
    return get_user_model().objects.filter(id=user_id, is_active=True, groups__name=WORKER).exists()


def _send_policy(config, user_id, kind, *, staff):
    reason = WxPusherNotice.SkipReason
    if not config.configured:
        return None, reason.APP_UNCONFIGURED
    if not config.push_enabled or (not staff and not config.wants_event(kind)):
        return None, reason.WORKSPACE_DISABLED
    if not _wants_notice(user_id, kind, staff=staff):
        return None, reason.PERSONAL_DISABLED
    binding = _binding(user_id, config.app_id)
    if binding is None or not binding.effective_uid:
        return None, reason.NOT_BOUND
    return binding, ""


def _enqueue_notice(*, user_id, kind, reference, order_no, details, staff):
    if kind not in NOTICE_TEMPLATES or (kind in STAFF_KINDS) != staff:
        raise ValueError(f"Unsupported WxPusher notice kind: {kind}")
    if not user_id or connection.schema_name == get_public_schema_name():
        return
    if staff:
        from apps.accounts.wxpusher_staff import is_wxpusher_staff

        user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
        if user is None or not is_wxpusher_staff(user):
            return
    elif not _eligible_worker(user_id):
        return

    _queue_delivery(user_id=user_id, kind=kind, reference=reference, order_no=order_no, details=details, staff=staff)


def _queue_delivery(*, user_id, kind, reference, order_no, details, staff=False):
    binding, skip_reason = _send_policy(load_wxpusher_config(), user_id, kind, staff=staff)
    WxPusherNotice.objects.get_or_create(
        user_id=user_id, kind=kind, reference=reference,
        defaults={
            "binding": binding, "binding_started_at": binding.last_start_at if binding else None,
            "order_no": order_no or "", "details": details or {}, "skip_reason": skip_reason,
            "status": WxPusherNotice.Status.SKIPPED if skip_reason else WxPusherNotice.Status.PENDING,
        },
    )


def enqueue_worker_notice(*, user_id, kind, reference, order_no=None, details=None):
    return _enqueue_notice(
        user_id=user_id, kind=kind, reference=reference, order_no=order_no, details=details, staff=False,
    )


def enqueue_staff_notice(*, user_id, kind, reference, order_no=None, details=None):
    return _enqueue_notice(
        user_id=user_id, kind=kind, reference=reference, order_no=order_no, details=details, staff=True,
    )


def _eligible_chat_recipient(user):
    from apps.accounts.wxpusher_staff import is_wxpusher_staff

    return _eligible_worker(user.pk) or is_wxpusher_staff(user)


def enqueue_chat_notices(*, recipient_kinds, reference, order_no, details):
    if not recipient_kinds or connection.schema_name == get_public_schema_name():
        return
    if set(recipient_kinds.values()) - CHAT_KINDS:
        raise ValueError("Unsupported WxPusher chat notice kind")

    config = load_wxpusher_config()
    reason = WxPusherNotice.SkipReason
    workspace_reason = ""
    if not config.configured:
        workspace_reason = reason.APP_UNCONFIGURED
    elif not config.push_enabled:
        workspace_reason = reason.WORKSPACE_DISABLED
    bindings = {}
    if not workspace_reason:
        bindings = {
            binding.user_id: binding for binding in WxPusherBinding.objects.filter(
                Q(uid__isnull=False) | Q(scan_bound_uid__isnull=False),
                user_id__in=recipient_kinds, app_id=config.app_id,
            ).only("user_id", "uid", "scan_bound_uid", "last_start_at") if binding.effective_uid
        }

    # The chat path has already filtered membership; role permissions are checked at delivery.
    notices = []
    for user_id, kind in recipient_kinds.items():
        binding = bindings.get(user_id)
        skip_reason = workspace_reason or (reason.NOT_BOUND if binding is None else "")
        notices.append(WxPusherNotice(
            user_id=user_id, kind=kind, reference=reference, order_no=order_no, details=details,
            binding=binding, binding_started_at=binding.last_start_at if binding else None,
            status=WxPusherNotice.Status.SKIPPED if skip_reason else WxPusherNotice.Status.PENDING,
            skip_reason=skip_reason,
        ))
    WxPusherNotice.objects.bulk_create(notices, ignore_conflicts=True)


def _can_view_worker_performance(codes):
    return "my_performance" in codes and bool(codes.intersection({"my_performance.export", "order.list"}))


def _worker_order_view_fallback(codes):
    if "my_profile" in codes:
        return "/m/me"
    if "grab_hall.take_order" in codes:
        return "/m/hall"
    if _can_view_worker_performance(codes):
        return "/m/performance"
    return "/m/empty"


def worker_notice_targets(user, notices, *, legacy_push_links=False):
    codes = nodes_of(user)
    has_order_access = {"my_orders", "my_orders.send_to_my_system"}.issubset(codes)
    eligible = {
        row.order_no for row in notices
        if has_order_access and row.kind not in SETTLEMENT_KINDS and row.kind != "unassigned"
        and re.fullmatch(r"[0-9]{20}", row.order_no)
    }
    owned = set()
    if eligible:
        owned = set(Performance.objects.filter(
            user_id=user.pk, emp_type=EmpType.MAIN,
            order__order_no__in=eligible, order__deleted_at__isnull=True,
            deleted_at__isnull=True,
        ).values_list("order__order_no", flat=True))
    fallback = "/m/mine" if has_order_access else None
    if fallback is None and legacy_push_links:
        fallback = _worker_order_view_fallback(codes)
    settlement_link = "/m/performance" if legacy_push_links or _can_view_worker_performance(codes) else None
    targets = {}
    for row in notices:
        if row.kind in SETTLEMENT_KINDS:
            targets[row.pk] = settlement_link
        elif row.kind != "unassigned" and row.order_no in owned:
            targets[row.pk] = f"/m/orders/{row.order_no}"
        else:
            targets[row.pk] = fallback
    return targets


def visible_notice_details(notice, worker_target):
    if "demand" not in notice.details or worker_target == f"/m/orders/{notice.order_no}":
        return notice.details
    return {key: value for key, value in notice.details.items() if key != "demand"}


def _order_entry_url(origin, order_no):
    return f"{origin}/m/wxpusher/orders/{order_no}?entry=wxpusher"


def _delivery_links(notice, origin, worker_target):
    if notice.kind in CHAT_KINDS:
        path = f"/m/wxpusher/chat/{notice.details['conversation_id']}?message={notice.details['message_id']}"
        web_url = f"{origin}{path}&entry=wxpusher"
        if not _eligible_worker(notice.user_id):
            web_url += "&open=web"
        return web_url, web_url
    target = "/v2/approvals" if notice.kind in STAFF_KINDS else worker_target
    if origin.lower() == DAIDAITONG_ORIGIN and target == f"/m/orders/{notice.order_no}":
        web_url = _order_entry_url(origin, notice.order_no)
    else:
        web_url = origin + target
    return web_url, web_url


def _reserve_delivery(origin):
    with transaction.atomic(durable=True):
        pending = WxPusherNotice.objects.select_for_update(skip_locked=True).filter(
            status=WxPusherNotice.Status.PENDING,
        ).exclude(kind__in=CHAT_KINDS | {HALL_NEW_ORDER}).order_by("id")
        notice = pending.first()
        if notice is None:
            return None
        config = load_wxpusher_config()
        staff = notice.kind in STAFF_KINDS
        binding, skip_reason = _send_policy(config, notice.user_id, notice.kind, staff=staff)
        if not skip_reason:
            if staff:
                from apps.accounts.wxpusher_staff import is_wxpusher_staff, staff_notice_is_pending
                eligible = is_wxpusher_staff(notice.user) and staff_notice_is_pending(notice)
            else:
                eligible = _eligible_worker(notice.user_id)
            if not eligible:
                skip_reason = WxPusherNotice.SkipReason.RECIPIENT_UNAVAILABLE
            elif notice.binding_id != binding.pk or notice.binding_started_at != binding.last_start_at:
                skip_reason = WxPusherNotice.SkipReason.BINDING_CHANGED
        if skip_reason:
            notice.status = WxPusherNotice.Status.SKIPPED
            notice.skip_reason = skip_reason
            notice.save(update_fields=["status", "skip_reason"])
            return (notice, None)
        notice.status = WxPusherNotice.Status.ATTEMPTED
        notice.attempted_at = timezone.now()
        notice.save(update_fields=["status", "attempted_at"])
        worker_target = None if staff else worker_notice_targets(notice.user, (notice,), legacy_push_links=True)[notice.pk]
        return notice, _personal_destination(origin, config, binding, notice, worker_target)


def _personal_destination(origin, config, binding, notice, worker_target=None):
    web_url, original_url = _delivery_links(notice, origin, worker_target)
    return config.app_token, binding.effective_uid, web_url, original_url, visible_notice_details(notice, worker_target)


def _reserve_chat_delivery(origin):
    from apps.internal_chat.wxpusher import recheck_direct_notices, recheck_group_notices

    with transaction.atomic(durable=True):
        pending = WxPusherNotice.objects.filter(
            kind__in=CHAT_KINDS, status=WxPusherNotice.Status.PENDING,
        ).order_by("created_at", "id")
        now = timezone.now()
        # Lock the recipient so another sender process cannot split the chat window.
        seed = pending.filter(created_at__lte=now - _CHAT_WINDOW).select_related("user").select_for_update(
            skip_locked=True, of=("user",), no_key=True,
        ).first()
        if seed is None:
            return None
        direct = seed.kind == DIRECT_MESSAGE
        conversation_id = seed.details.get("conversation_id") if isinstance(seed.details, dict) else None
        if type(conversation_id) is int and 0 < conversation_id < 2**63:
            matches = pending.filter(
                user_id=seed.user_id, details__conversation_id=conversation_id,
                kind__in=(DIRECT_MESSAGE,) if direct else GROUP_CHAT_KINDS,
                created_at__lte=seed.created_at + _CHAT_WINDOW,
            )
        else:
            matches = pending.filter(pk=seed.pk)
            conversation_id = None
        notices = list(matches.select_for_update(of=("self",)))
        if not notices:
            return [], None, None, 0

        reason = WxPusherNotice.SkipReason
        config = load_wxpusher_config()
        binding, skip_reason = _send_policy(config, seed.user_id, seed.kind, staff=False)
        if not skip_reason and not _eligible_chat_recipient(seed.user):
            skip_reason = reason.RECIPIENT_UNAVAILABLE
        recheck = recheck_direct_notices if direct else recheck_group_notices
        outcomes = recheck(notices, conversation_id) if not skip_reason else {}
        skipped, reserved = [], []
        for notice in notices:
            notice_reason = skip_reason
            if not notice_reason and (notice.binding_id != binding.pk or notice.binding_started_at != binding.last_start_at):
                notice_reason = reason.BINDING_CHANGED
            if not notice_reason:
                notice_reason, target_id = outcomes[notice.pk]
            if notice_reason:
                notice.status = WxPusherNotice.Status.SKIPPED
                notice.skip_reason = notice_reason
                skipped.append(notice)
            else:
                notice.status = WxPusherNotice.Status.ATTEMPTED
                notice.attempted_at = now
                notice.details = {**notice.details, "message_id": target_id}
                reserved.append(notice)

        WxPusherNotice.objects.bulk_update(skipped, ["status", "skip_reason"], batch_size=100)
        if not reserved:
            return [], None, None, len(skipped)
        WxPusherNotice.objects.bulk_update(reserved, ["status", "attempted_at", "details"], batch_size=100)
        target = max(reversed(reserved), key=lambda notice: _CHAT_PRIORITY[notice.kind])
        return reserved, target, _personal_destination(origin, config, binding, target), len(skipped)


def _hall_delivery_skips(notices, config):
    from apps.accounts.wxpusher_hall import recheck_hall_notices

    reason = WxPusherNotice.SkipReason
    if not config.configured:
        return {notice.pk: reason.APP_UNCONFIGURED for notice in notices}, {}
    if not config.push_enabled:
        return {notice.pk: reason.WORKSPACE_DISABLED for notice in notices}, {}

    user_ids = [notice.user_id for notice in notices]
    wanted = set(WxPusherPreference.objects.filter(
        user_id__in=user_ids, hall_enabled_since__isnull=False,
    ).values_list("user_id", flat=True))
    eligible = set(get_user_model().objects.filter(
        pk__in=wanted, is_active=True, groups__name=WORKER,
    ).values_list("pk", flat=True))
    bindings = {
        binding.user_id: binding for binding in WxPusherBinding.objects.filter(
            Q(uid__isnull=False) | Q(scan_bound_uid__isnull=False),
            app_id=config.app_id, user_id__in=wanted,
        ).only("user_id", "uid", "scan_bound_uid", "last_start_at") if binding.effective_uid
    }
    skipped = {}
    for notice in notices:
        binding = bindings.get(notice.user_id)
        if notice.user_id not in wanted:
            skipped[notice.pk] = reason.PERSONAL_DISABLED
        elif binding is None:
            skipped[notice.pk] = reason.NOT_BOUND
        elif notice.user_id not in eligible:
            skipped[notice.pk] = reason.RECIPIENT_UNAVAILABLE
        elif notice.binding_id != binding.pk or notice.binding_started_at != binding.last_start_at:
            skipped[notice.pk] = reason.BINDING_CHANGED
    skipped.update(recheck_hall_notices(notice for notice in notices if notice.pk not in skipped))
    return skipped, bindings


def _reserve_hall_delivery(origin):
    with transaction.atomic(durable=True):
        pending = WxPusherNotice.objects.filter(
            kind=HALL_NEW_ORDER, status=WxPusherNotice.Status.PENDING,
        ).order_by("id")
        reference = pending.values_list("reference", flat=True).first()
        if reference is None:
            return None
        # Serialize same-order drains before either worker can lock a subset of the recipients.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                [f"wxpusher-hall:{connection.schema_name}:{reference}"],
            )
            if not cursor.fetchone()[0]:
                return None

        from apps.business.api.hall_claim_target import hall_claim_target_is_available
        from apps.core.wxpusher_login import make_shared_hall_push_path

        locked = pending.select_for_update(skip_locked=True, of=("self",))
        notices = list(locked.filter(reference=reference)[:_HALL_BATCH_SIZE])
        if not notices:
            return None
        config = load_wxpusher_config()
        skip_reasons, bindings = _hall_delivery_skips(notices, config)
        skipped = []
        reserved = {}
        payload = None
        body_key = None
        for notice in notices:
            if notice.pk in skip_reasons:
                notice.skip_reason = skip_reasons[notice.pk]
                skipped.append(notice)
                continue

            candidate_key = notice.order_no, notice.details
            if body_key is None:
                body_key = candidate_key
                url = origin + make_shared_hall_push_path(notice.details["source"], notice.details["ref"])
                title, content = render_notice(
                    HALL_NEW_ORDER, notice.order_no, visible_notice_details(notice, None), url, link_label="前往抢单",
                )
                payload = title, content, url
            elif body_key != candidate_key:
                # A provider request has one body and URL; leave other payloads pending.
                continue
            reserved[bindings[notice.user_id].effective_uid] = notice

        if reserved:
            details = next(iter(reserved.values())).details
            if not hall_claim_target_is_available(details["source"], details["ref"], details["game_id"]):
                for notice in reserved.values():
                    notice.skip_reason = WxPusherNotice.SkipReason.HALL_UNAVAILABLE
                    skipped.append(notice)
                reserved.clear()

        for notice in skipped:
            notice.status = WxPusherNotice.Status.SKIPPED
        WxPusherNotice.objects.bulk_update(skipped, ["status", "skip_reason", "details"], batch_size=100)
        attempted_at = timezone.now()
        for notice in reserved.values():
            notice.status = WxPusherNotice.Status.ATTEMPTED
            notice.attempted_at = attempted_at
        WxPusherNotice.objects.bulk_update(reserved.values(), ["status", "attempted_at", "details"], batch_size=100)
        return config.app_token, reserved, payload, len(skipped)


def _send_hall_delivery(reservation, counts, pacer):
    app_token, reserved, payload, skipped = reservation
    if skipped:
        counts[WxPusherNotice.Status.SKIPPED] += skipped
    if not reserved:
        return
    title, content, url = payload
    try:
        with WxPusherClient(timeout=6) as client:
            if pacer is not None:
                pacer.starting_request()
            accepted = client.send_many(
                app_token=app_token, uids=list(reserved), summary=title, content=content, url=url, html=True,
            )
    except WxPusherError:
        accepted = {}

    for uid, notice in reserved.items():
        notice.status = WxPusherNotice.Status.SENT if uid in accepted else WxPusherNotice.Status.UNCONFIRMED
        counts[notice.status] += 1
    WxPusherNotice.objects.bulk_update(reserved.values(), ["status"], batch_size=100)


def _send_personal_delivery(notice, destination, counts, *, origin, pacer, chat_notices=None):
    app_token, uid, web_url, original_url, details = destination
    link_label = "打开订单" if web_url == _order_entry_url(origin, notice.order_no) else None
    title, content = render_notice(
        notice.kind, notice.order_no, details, web_url,
        link_label=link_label, unread_count=len(chat_notices) if chat_notices is not None else None,
    )
    try:
        with WxPusherClient(timeout=6) as client:
            if pacer is not None:
                pacer.starting_request()
            client.send(
                app_token=app_token, uid=uid, summary=title, content=content, url=original_url, html=True,
            )
    except WxPusherError:
        status = WxPusherNotice.Status.UNCONFIRMED
    else:
        status = WxPusherNotice.Status.SENT
    delivered = [notice] if chat_notices is None else chat_notices
    WxPusherNotice.objects.filter(pk__in=[row.pk for row in delivered]).update(status=status)
    counts[status] += len(delivered)


def send_notification_batch(domain, *, limit=50, should_stop=None, pacer=None, mode="all"):
    from apps.accounts.wxpusher_hall import process_hall_publications

    counts = Counter()
    config = load_wxpusher_config()
    if not config.configured:
        return counts
    if not _HOST.fullmatch(domain):
        raise InvalidNoticeDomain("工作室主域名无法用于手机推送链接。")

    # Commit each reservation before I/O. Ambiguous provider responses are not retried.
    origin = f"https://{domain}"
    for index in range(limit):
        if should_stop is not None and should_stop():
            break
        if pacer is not None:
            pacer.wait()
            if should_stop is not None and should_stop():
                break
        if mode != "hall":
            reserved = _reserve_delivery(origin)
            if reserved is not None:
                notice, destination = reserved
                if destination is None:
                    counts[notice.status] += 1
                else:
                    _send_personal_delivery(notice, destination, counts, origin=origin, pacer=pacer)
                continue

        if mode != "regular":
            hall_reservation = _reserve_hall_delivery(origin)
            if hall_reservation is None:
                process_hall_publications(config, limit=limit - index, should_stop=should_stop)
                if should_stop is not None and should_stop():
                    break
                hall_reservation = _reserve_hall_delivery(origin)
            if hall_reservation is not None:
                _send_hall_delivery(hall_reservation, counts, pacer)
                continue
        if mode == "hall":
            break

        chat_reservation = _reserve_chat_delivery(origin)
        if chat_reservation is None:
            break
        chat_notices, target, destination, skipped = chat_reservation
        if skipped:
            counts[WxPusherNotice.Status.SKIPPED] += skipped
        if chat_notices:
            _send_personal_delivery(target, destination, counts, origin=origin, pacer=pacer, chat_notices=chat_notices)
    return counts
