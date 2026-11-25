from datetime import timedelta
from hashlib import sha256
from itertools import batched
from secrets import token_urlsafe
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core import signing
from django.db import connection, transaction
from django.db.models import BigIntegerField, Count, F, Max, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.crypto import salted_hmac
from rest_framework.exceptions import NotAuthenticated, PermissionDenied, Throttled, ValidationError

from apps.accounts.admin_mutations import lock_tenant_admin_mutations
from apps.accounts.display import user_display_name
from apps.core.login_rate_limit import login_source_ip

from . import visitor_seats as seats
from .images import encode_image, image_fields
from .models import (
    VisitorChange,
    VisitorConversation,
    VisitorDeskPresence,
    VisitorIdentity,
    VisitorMessage,
    VisitorReceptionSettings,
)
from .realtime import notify_viewers_on_commit
from .service import CHANGE_BATCH_SIZE, PAGE_SIZE, _eligible_directory_staff, _history_page_edge, person_payload

COOKIE_NAME = "tt_chat_visitor"
COOKIE_PATH = "/api/internal-chat/visitor/guest/"
SESSION_SECONDS = 30 * 24 * 60 * 60
REGISTRATION_LIMIT = 20
MESSAGE_LIMIT = 12
MAX_RECEPTION_CONTACTS = 100
GREETING_CLIENT_ID = "visitor-greeting"
VISITOR_ID_OFFSET = 2**40


def _token_signer():
    return signing.TimestampSigner(salt=f"internal-chat.visitor.{connection.schema_name}")


def issue_token():
    return _token_signer().sign(token_urlsafe(32))


def credential_digest(cookie):
    if not cookie or len(cookie) > 160:
        return None
    try:
        token = _token_signer().unsign(cookie, max_age=SESSION_SECONDS)
    except signing.BadSignature:
        return None
    return sha256(token.encode()).hexdigest()


def visitor_for(digest):
    if not digest:
        return None
    return VisitorIdentity.objects.filter(token_digest=digest, expires_at__gt=timezone.now()).first()


def _reception_settings():
    return VisitorReceptionSettings.objects.filter(pk=1).first() or VisitorReceptionSettings()


def _lock_reception():
    # Serializing changes keeps the sync cursor in commit order.
    return VisitorReceptionSettings.objects.select_for_update().get_or_create(pk=1)[0]


def _staff_choices():
    return _eligible_directory_staff().only("id", "username", "first_name").order_by("first_name", "pk")


def _primary_greeting(greetings):
    first = next((rule for rule in greetings if rule["enabled"]), None)
    return (first["contact_id"], first["text"]) if first else (None, "")


def _legacy_greetings(sender_id, text):
    return [{"contact_id": sender_id, "enabled": True, "text": text}] if sender_id is not None and text else []


def _stored_greetings(config):
    sender_id, text = config.greeting_sender_id, config.greeting_text
    if config.greetings is not None:
        mirrored_sender, mirrored_text = _primary_greeting(config.greetings)
        if (sender_id, text) == (mirrored_sender, mirrored_text):
            return config.greetings
        # SET_NULL on a deleted sender preserves the mirrored text.
        if (
            sender_id is None and mirrored_sender is not None and text == mirrored_text
            and not get_user_model().objects.filter(pk=mirrored_sender).exists()
        ):
            return config.greetings
    return _legacy_greetings(sender_id, text)


def _configured_greetings(config):
    by_contact = {rule["contact_id"]: rule for rule in _stored_greetings(config)}
    return [by_contact[pk] for pk in config.contact_ids if pk in by_contact]


def reception_settings_payload(config=None):
    config = config or _reception_settings()
    candidates = list(map(person_payload, _staff_choices()))
    eligible = {candidate["id"] for candidate in candidates}
    contact_ids = [pk for pk in config.contact_ids if pk in eligible]
    configured_ids = dict.fromkeys(config.contact_ids)
    for seat in config.seats or []:
        configured_ids.update(dict.fromkeys(seat["contact_ids"]))
    unavailable = get_user_model().objects.only("id", "username", "first_name").in_bulk(
        configured_ids.keys() - eligible,
    )
    configured = {rule["contact_id"]: rule for rule in _configured_greetings(config)}
    return {
        "contact_ids": contact_ids,
        "unavailable_contacts": [
            {"id": pk, "name": user_display_name(unavailable[pk])}
            for pk in configured_ids if pk in unavailable
        ],
        "greetings": [
            configured.get(pk, {"contact_id": pk, "enabled": False, "text": ""}) for pk in contact_ids
        ],
        "greeting_sender_id": config.greeting_sender_id if config.greeting_sender_id in eligible else None,
        "greeting_text": config.greeting_text,
        "candidates": candidates,
        "seats": config.seats or [],
    }


def _append_eviction_changes(rows):
    for batch in batched(rows.iterator(chunk_size=100), 100):
        VisitorChange.objects.bulk_create([
            VisitorChange(conversation_id=pk, former_staff_id=staff_id) for pk, staff_id in batch
        ])


@transaction.atomic
def set_reception_settings(contact_ids, greetings=None, greeting_sender_id=None, greeting_text="", removed_contact_ids=None):
    lock_tenant_admin_mutations()
    config = _lock_reception()
    requested = set(contact_ids)
    eligible = set(_staff_choices().filter(pk__in=requested.union(config.contact_ids)).values_list("pk", flat=True))
    if not requested.issubset(eligible):
        raise ValidationError({"contact_ids": "请只选择当前在职的非打手员工"})
    previous = _configured_greetings(config)
    if greetings is None:
        if len(previous) > 1 or any(not rule["enabled"] for rule in previous):
            raise ValidationError({"greetings": "已配置按人欢迎语，请刷新页面后保存"})
        greetings = _legacy_greetings(greeting_sender_id, greeting_text)
    else:
        # Older callers replace the active list. Explicit removal survives an eligibility change after loading.
        excluded = eligible if removed_contact_ids is None else requested.union(removed_contact_ids)
        retained = set(get_user_model().objects.filter(
            pk__in=set(config.contact_ids) - excluded,
        ).values_list("pk", flat=True))
        contact_ids = [*contact_ids, *(pk for pk in config.contact_ids if pk in retained)]
        if len(contact_ids) > MAX_RECEPTION_CONTACTS:
            raise ValidationError({"contact_ids": f"接待人员最多 {MAX_RECEPTION_CONTACTS} 位，含保留的人员"})
        by_contact = {rule["contact_id"]: rule for rule in previous if rule["contact_id"] in retained}
        by_contact.update((rule["contact_id"], rule) for rule in greetings if rule["enabled"] or rule["text"])
        greetings = [by_contact[pk] for pk in contact_ids if pk in by_contact]
        greeting_sender_id, greeting_text = _primary_greeting(greetings)
    stored = previous if config.greetings is None else config.greetings
    changed = (config.contact_ids, stored, config.greeting_sender_id, config.greeting_text) != (
        contact_ids, greetings, greeting_sender_id, greeting_text,
    )
    if changed:
        previous_contacts = set(config.contact_ids)
        removed = (previous_contacts - set(contact_ids)) & eligible
        config.contact_ids = contact_ids
        config.greetings = greetings
        config.greeting_sender_id = greeting_sender_id
        config.greeting_text = greeting_text
        config.save(update_fields=["contact_ids", "greetings", "greeting_sender", "greeting_text"])
        remaining_desk_staff = {pk for pk in removed if seats.reception_member(config, pk)}
        revoked = VisitorConversation.objects.filter(
            seat_kind="", staff_id__in=remaining_desk_staff, latest_seq__gt=0,
        ).values_list("pk", "staff_id")
        _append_eviction_changes(revoked)
        notify_viewers_on_commit(previous_contacts.symmetric_difference(contact_ids))
    return reception_settings_payload(config), changed


def _is_configured_contact(staff_id, config):
    return staff_id in config.contact_ids and _eligible_directory_staff().filter(pk=staff_id).exists()


def require_desk_account(staff_id):
    config = _reception_settings()
    member = seats.reception_member(config, staff_id) or staff_id in config.contact_ids
    if not member or not _eligible_directory_staff().filter(pk=staff_id).exists():
        raise PermissionDenied("当前账号不是访客接待人员")
    return config


def seat_mode():
    return _reception_settings().seats is not None


@transaction.atomic
def set_reception_seats(seat_settings):
    lock_tenant_admin_mutations()
    config = _lock_reception()
    requested = {pk for seat in seat_settings for pk in seat["contact_ids"]}
    eligible = set(_staff_choices().filter(pk__in=requested).values_list("pk", flat=True))
    if requested != eligible:
        raise ValidationError({"seats": "接待人员须为当前工作室的在职非打手员工"})
    changed = config.seats != seat_settings
    if changed:
        previous = seats.configured_seats(config)
        config.seats = seat_settings
        config.save(update_fields=["seats"])
        now = timezone.now()
        affected = set()
        for seat in seat_settings:
            removed = VisitorConversation.objects.filter(seat_kind=seat["kind"], service_status=seats.Status.ASSIGNED).exclude(
                assigned_staff_id__in=seat["contact_ids"],
            )
            for conversation in removed.iterator(chunk_size=100):
                seats.queue(conversation, now)
            old_staff = set(previous.get(seat["kind"], {}).get("contact_ids", []))
            former_staff = old_staff - set(seat["contact_ids"])
            closed = VisitorConversation.objects.filter(
                seat_kind=seat["kind"], service_status=seats.Status.CLOSED, assigned_staff_id__in=former_staff,
            ).values_list("pk", "assigned_staff_id")
            _append_eviction_changes(closed)
            affected.update(old_staff.symmetric_difference(seat["contact_ids"]))
            affected.update(seats.drain(config, seat, now))
        notify_viewers_on_commit(affected)
    return reception_settings_payload(config), changed


def desk_availability(staff_id):
    config = require_desk_account(staff_id)
    enabled = seats.reception_member(config, staff_id) and VisitorDeskPresence.objects.filter(
        staff_id=staff_id, enabled=True,
    ).exists()
    if enabled:
        refresh_desk_availability(staff_id)
    return {"enabled": enabled}


@transaction.atomic
def set_desk_availability(staff_id, enabled):
    config = _lock_reception()
    if not seats.reception_member(config, staff_id) or not _eligible_directory_staff().filter(pk=staff_id).exists():
        raise PermissionDenied("当前账号未配置为客服席位的接待人员")
    was_enabled = VisitorDeskPresence.objects.filter(staff_id=staff_id, enabled=True).exists()
    now = timezone.now()
    VisitorDeskPresence.objects.update_or_create(staff_id=staff_id, defaults={"enabled": enabled, "last_seen_at": now})
    if enabled:
        affected = seats.drain_for_staff(config, staff_id, now)
    else:
        affected = seats.release_for_staff(config, staff_id, now)
    changed = was_enabled != enabled
    if changed:
        affected.add(staff_id)
    notify_viewers_on_commit(affected)
    return {"enabled": enabled}, changed


@transaction.atomic
def refresh_desk_availability(staff_id):
    now = timezone.now()
    due = VisitorDeskPresence.objects.filter(staff_id=staff_id, enabled=True, last_seen_at__lt=now - seats.HEARTBEAT_INTERVAL)
    if not due.exists():
        return
    config = _lock_reception()
    if seats.reception_member(config, staff_id) and due.update(last_seen_at=now):
        notify_viewers_on_commit(seats.drain_for_staff(config, staff_id, now))


def guest_contacts(query):
    config = _reception_settings()
    if config.seats is not None:
        statuses = seats.seat_presence(config)
        contacts = [seats.public_peer(kind, statuses[kind]) for kind in seats.Seat.values]
    else:
        by_id = _staff_choices().in_bulk(config.contact_ids)
        contacts = [person_payload(by_id[pk]) for pk in config.contact_ids if pk in by_id]
    return {
        "worker_directory_only": True,
        "contacts": [person for person in contacts if query.casefold() in person["name"].casefold()],
    }


def guest_peer(guest, *, show_phone=False):
    phone = guest.phone if show_phone else f"{guest.phone[:3]}****{guest.phone[-4:]}"
    return {
        "id": VISITOR_ID_OFFSET + guest.pk,
        "name": f"访客 {phone}",
        "avatar_url": None,
    }


def _append_messages(conversation, *, is_guest, text, client_id, reply=None, staff_sender=None, image=None, images=None):
    parts = images if images is not None else [image]
    album_id = uuid4() if images is not None else None
    sender_id = staff_sender.pk if staff_sender is not None else None
    sender_name = user_display_name(staff_sender) if staff_sender is not None else ""
    messages = []
    for index, part in enumerate(parts):
        message = VisitorMessage.objects.create(
            conversation=conversation, seq=conversation.latest_seq + index + 1,
            sender_is_guest=is_guest, staff_sender_id=sender_id, staff_sender_name=sender_name,
            text=text if index == 0 else "", reply_to=reply if index == 0 else None,
            client_id=client_id if index == 0 else f"album:{album_id.hex}:{index}",
            album_id=album_id, **image_fields(part),
        )
        VisitorChange.objects.create(conversation=conversation, message=message)
        messages.append(message)
    conversation.latest_seq += len(parts)
    conversation.save(update_fields=["latest_seq"])
    return messages


@transaction.atomic
def register(request, digest, phone):
    config = _lock_reception()
    visitor = visitor_for(digest)
    if visitor is not None:
        if config.seats is not None:
            return visitor, None
        first = visitor.conversations.filter(messages__sender_is_guest=False, messages__client_id=GREETING_CLIENT_ID)
        return visitor, first.order_by("pk").values_list("pk", flat=True).first()
    if VisitorIdentity.objects.filter(token_digest=digest).exists():
        raise NotAuthenticated("访客注册已过期，请重新进入")
    fingerprint = salted_hmac("internal-chat.visitor.registration", login_source_ip(request), algorithm="sha256").hexdigest()
    now = timezone.now()
    if VisitorIdentity.objects.filter(
        registration_fingerprint=fingerprint, created_at__gte=now - timedelta(minutes=1),
    ).count() >= REGISTRATION_LIMIT:
        raise Throttled(wait=60)
    visitor = VisitorIdentity.objects.create(
        token_digest=digest, phone=phone, registration_fingerprint=fingerprint,
        expires_at=now + timedelta(seconds=SESSION_SECONDS),
    )
    if config.seats is not None:
        return visitor, None
    enabled = {rule["contact_id"]: rule["text"] for rule in _configured_greetings(config) if rule["enabled"]}
    eligible = set(_eligible_directory_staff().filter(pk__in=enabled).values_list("pk", flat=True))
    first_id = None
    for staff_id, text in enabled.items():
        if staff_id in eligible:
            conversation = VisitorConversation.objects.create(visitor=visitor, staff_id=staff_id)
            _append_messages(conversation, is_guest=False, client_id=GREETING_CLIENT_ID, text=text)
            if first_id is None:
                first_id = conversation.pk
    notify_viewers_on_commit(eligible)
    return visitor, first_id


def _summary_rows():
    return VisitorConversation.objects.select_related("visitor", "staff", "assigned_staff").only(
        "id", "latest_seq", "visitor_read_seq", "staff_read_seq", "seat_kind", "service_status",
        "visitor__id", "visitor__phone", "staff__id", "staff__first_name", "staff__username",
        "assigned_staff__id", "assigned_staff__first_name", "assigned_staff__username",
    )


def visible_conversations(*, visitor, staff_id):
    rows = _summary_rows()
    if visitor is not None:
        return rows.filter(visitor=visitor)
    config = _reception_settings()
    seat_kinds = [kind for kind, seat in seats.configured_seats(config).items() if staff_id in seat["contact_ids"]]
    assigned = Q(
        seat_kind__in=seat_kinds, assigned_staff_id=staff_id,
        service_status__in=(seats.Status.ASSIGNED, seats.Status.CLOSED),
    )
    if _is_configured_contact(staff_id, config):
        assigned |= Q(staff_id=staff_id)
    return rows.filter(assigned, latest_seq__gt=0)


def message_payload(message, *, as_guest):
    conversation = message.conversation
    payload = {
        "id": message.pk,
        "seq": message.seq,
        "conversation_id": message.conversation_id,
        "text": message.text,
        "client_id": message.client_id,
        "created_at": message.created_at.isoformat(),
        "revoked": False,
        "reply_to_id": message.reply_to_id,
    }
    if message.sender_is_guest:
        payload["sender_id"] = (
            seats.DESK_PEER_OFFSET + conversation.pk if conversation.seat_kind and not as_guest
            else VISITOR_ID_OFFSET + conversation.visitor_id
        )
    elif conversation.seat_kind and as_guest:
        payload["sender_id"] = seats.SEAT_IDS[conversation.seat_kind]
    else:
        payload["sender_id"] = message.staff_sender_id or conversation.staff_id
        if conversation.seat_kind:
            payload["sender_name"] = message.staff_sender_name
    if message.image_metadata is not None:
        payload["image"] = message.image_metadata
        if message.album_id is not None:
            payload["album_id"] = str(message.album_id)
    return payload


def _read_fields(as_guest):
    return ("visitor_read_seq", "staff_read_seq") if as_guest else ("staff_read_seq", "visitor_read_seq")


def _annotated_conversations(rows, as_guest):
    own_read, _ = _read_fields(as_guest)
    incoming = VisitorMessage.objects.filter(
        conversation_id=OuterRef("pk"), sender_is_guest=not as_guest, seq__gt=OuterRef(own_read),
    )
    unread_count = incoming.order_by().values("conversation_id").annotate(
        count=Count("pk", filter=Q(album_id__isnull=True)) + Count("album_id", distinct=True),
    ).values("count")
    return rows.annotate(
        last_message_id=Subquery(VisitorMessage.objects.filter(
            conversation_id=OuterRef("pk"), seq=OuterRef("latest_seq"),
        ).values("pk")[:1]),
        first_unread_message_id=Subquery(incoming.order_by("seq").values("pk")[:1]),
        unread_count=Coalesce(Subquery(unread_count), 0, output_field=BigIntegerField()),
    )


def _summary(conversation, last, as_guest, presence):
    own_read, peer_read = _read_fields(as_guest)
    peer = guest_peer(conversation.visitor, show_phone=True) if not as_guest else None
    payload = {
        "id": conversation.pk,
        "kind": "direct",
        "last_message": message_payload(last, as_guest=as_guest) if last else None,
        "read_seq": getattr(conversation, own_read),
        "peer_read_seq": getattr(conversation, peer_read),
        "unread_count": conversation.unread_count,
        "first_unread_message_id": conversation.first_unread_message_id,
        "marked_unread": False,
    }
    kind = conversation.seat_kind
    if kind:
        payload.update(seat_kind=kind, service_status=conversation.service_status)
        if as_guest:
            peer = seats.public_peer(kind, presence.get(kind, "offline"))
        else:
            peer.update(id=seats.DESK_PEER_OFFSET + conversation.pk, seat_kind=kind)
            staff = conversation.assigned_staff
            payload.update(
                assignee_id=conversation.assigned_staff_id, assignee_name=user_display_name(staff) if staff else None,
            )
    elif as_guest:
        peer = person_payload(conversation.staff)
    payload["peer"] = peer
    return payload


def _change_rows(conversations, staff_id=None):
    visible = Q(conversation_id__in=conversations.values("pk"))
    if staff_id is not None:
        visible |= Q(former_staff_id=staff_id, message_id__isnull=True)
    return VisitorChange.objects.filter(visible)


def _summaries(rows, as_guest):
    last_messages = VisitorMessage.objects.select_related("conversation").defer("image_data").in_bulk(
        row.last_message_id for row in rows if row.last_message_id is not None
    )
    presence = seats.seat_presence(_reception_settings()) if as_guest and any(row.seat_kind for row in rows) else {}
    return [_summary(row, last_messages.get(row.last_message_id), as_guest, presence) for row in rows]


def conversation_summary(rows, as_guest):
    room = get_object_or_404(_annotated_conversations(rows, as_guest))
    return _summaries([room], as_guest)[0]


def list_conversations(rows, as_guest, *, limit=100, offset=0, staff_id=None):
    cursor = _change_rows(rows, staff_id).aggregate(latest=Max("pk"))["latest"] or 0
    rows = list(_annotated_conversations(rows, as_guest).order_by(
        F("last_message_id").desc(nulls_last=True), "-pk",
    )[offset:offset + limit + 1])
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "conversations": _summaries(rows, as_guest),
        "cursor": cursor,
        "has_more": has_more,
        "next_offset": offset + len(rows),
    }


def unread_count(rows, as_guest):
    return _annotated_conversations(rows, as_guest).aggregate(total=Coalesce(
        Sum("unread_count"), 0, output_field=BigIntegerField(),
    ))["total"]


@transaction.atomic
def start_conversation(visitor, peer_id):
    config = _lock_reception()
    if config.seats is not None:
        kind = seats.SEATS_BY_ID.get(peer_id)
        if kind not in seats.configured_seats(config):
            raise PermissionDenied("该客服暂不可联系")
        conversation, _ = VisitorConversation.objects.get_or_create(visitor=visitor, seat_kind=kind)
    else:
        if not _is_configured_contact(peer_id, config):
            raise PermissionDenied("该接待人员暂不可联系")
        conversation, _ = VisitorConversation.objects.get_or_create(visitor=visitor, staff_id=peer_id)
    rows = visible_conversations(visitor=visitor, staff_id=None).filter(pk=conversation.pk)
    return list_conversations(rows, True, limit=1)["conversations"][0]


def _message_page(conversation, **filters):
    return conversation.messages.filter(**filters).select_related("conversation").defer("image_data")


@transaction.atomic
def history(conversation, *, as_guest, staff_id=None, before=None, around=None):
    if not as_guest:
        # A handoff can change ownership between resolving the URL and reading messages.
        conversation = _locked_room(conversation, staff_id)
        _require_desk_history(conversation, _reception_settings(), staff_id)
    if around is not None:
        target = _message_page(conversation, pk=around).first()
        if target is None:
            raise Http404
        half = PAGE_SIZE // 2
        older, has_older = _history_page_edge(_message_page(conversation, seq__lt=target.seq).order_by("-seq"), half)
        newer, has_newer = _history_page_edge(_message_page(conversation, seq__gt=target.seq).order_by("seq"), half)
        page = [*reversed(older), target, *newer]
        return {
            "messages": [message_payload(message, as_guest=as_guest) for message in page],
            "has_more": has_older,
            "has_newer": has_newer,
        }
    rows = _message_page(conversation, **({"seq__lt": before} if before is not None else {}))
    rows, has_older = _history_page_edge(rows.order_by("-seq"), PAGE_SIZE)
    return {
        "messages": [message_payload(message, as_guest=as_guest) for message in reversed(rows)],
        "has_more": has_older,
        "has_newer": bool(rows and rows[0].seq < conversation.latest_seq),
    }


def _locked_room(conversation, staff_id=None):
    locked = get_object_or_404(VisitorConversation.objects.select_for_update(), pk=conversation.pk)
    if staff_id is not None and staff_id != (locked.assigned_staff_id if locked.seat_kind else locked.staff_id):
        raise PermissionDenied("当前会话未由你接待")
    return locked


def _desk_seat(conversation, config, staff_id):
    seat = seats.configured_seats(config).get(conversation.seat_kind)
    if seat is None or staff_id not in seat["contact_ids"] or not _eligible_directory_staff().filter(pk=staff_id).exists():
        raise PermissionDenied("当前账号不是该席位的接待人员")
    return seat


def _require_desk_history(conversation, config, staff_id):
    if staff_id is None:
        raise PermissionDenied("当前账号不是访客接待人员")
    if conversation.seat_kind:
        _desk_seat(conversation, config, staff_id)
        if conversation.service_status not in (seats.Status.ASSIGNED, seats.Status.CLOSED):
            raise PermissionDenied("当前会话未由你接待")
    elif not _is_configured_contact(staff_id, config):
        raise PermissionDenied("当前账号不是访客接待人员")


@transaction.atomic
def read_image(conversation, message_id, *, as_guest, staff_id=None):
    if not as_guest:
        conversation = _locked_room(conversation, staff_id)
        _require_desk_history(conversation, _reception_settings(), staff_id)
    media = conversation.messages.filter(
        image_metadata__isnull=False, image_data__isnull=False,
    ).values_list("image_metadata", "image_data")
    metadata, data = get_object_or_404(media, pk=message_id)
    return encode_image(metadata, data)


def _desk_recipient(conversation):
    if not conversation.seat_kind:
        return conversation.staff_id
    if conversation.service_status in (seats.Status.ASSIGNED, seats.Status.CLOSED):
        return conversation.assigned_staff_id
    return None


def _message_seat(conversation, config, *, as_guest, staff_id=None):
    if not conversation.seat_kind:
        if not _is_configured_contact(conversation.staff_id, config):
            raise PermissionDenied("该接待人员暂不可联系")
        return None
    if not as_guest:
        _desk_seat(conversation, config, staff_id)
        return None
    seat = seats.configured_seats(config).get(conversation.seat_kind)
    if seat is None:
        raise PermissionDenied("该客服暂不可联系")
    return seat


def _sent_message(conversation, *, as_guest, client_id, is_album):
    sent = conversation.messages.filter(sender_is_guest=as_guest).defer("image_data")
    existing = sent.filter(client_id=client_id).first()
    if existing is None:
        return None
    if not is_album and existing.album_id is None:
        return message_payload(existing, as_guest=as_guest)
    if is_album and existing.album_id is not None:
        album = list(sent.filter(album_id=existing.album_id).order_by("seq"))
        if album[0].pk == existing.pk:
            return {"messages": [message_payload(message, as_guest=as_guest) for message in album]}
    raise ValidationError({"client_id": "此发送编号已用于其他消息类型"})


def _require_message_budget(conversation, *, as_guest, parts, now):
    cutoff = now - timedelta(seconds=10)
    if as_guest:
        recent = VisitorMessage.objects.filter(
            conversation__visitor_id=conversation.visitor_id, sender_is_guest=True, created_at__gte=cutoff,
        )
    else:
        recent = conversation.messages.filter(sender_is_guest=False, created_at__gte=cutoff)
    if recent.count() + parts > MESSAGE_LIMIT:
        raise Throttled(wait=10)


def preflight_guest_image(conversation, *, client_id, is_album, parts):
    # Fast rejection happens before decoding. The write checks the budget again under its lock.
    try:
        _require_message_budget(conversation, as_guest=True, parts=parts, now=timezone.now())
    except Throttled:
        retry = _sent_message(conversation, as_guest=True, client_id=client_id, is_album=is_album)
        if retry is None:
            raise
        _message_seat(conversation, _reception_settings(), as_guest=True)
        return retry
    return None


@transaction.atomic
def send_message(conversation, *, as_guest, text, client_id, reply_to_id=None, staff_sender=None, image=None, images=None):
    config = _lock_reception()
    staff_id = staff_sender.pk if staff_sender else None
    conversation = _locked_room(conversation, staff_id)
    seat = _message_seat(conversation, config, as_guest=as_guest, staff_id=staff_id)
    existing = _sent_message(conversation, as_guest=as_guest, client_id=client_id, is_album=images is not None)
    if existing is not None:
        return existing
    if conversation.seat_kind and not as_guest and conversation.service_status != seats.Status.ASSIGNED:
        raise PermissionDenied("该会话已结束，请等待访客再次发来消息")
    now = timezone.now()
    _require_message_budget(conversation, as_guest=as_guest, parts=len(images) if images is not None else 1, now=now)
    reply = None
    if reply_to_id is not None:
        reply = conversation.messages.only("pk").filter(pk=reply_to_id).first()
        if reply is None:
            raise ValidationError({"reply_to_id": "引用消息不在该会话中"})
    messages = _append_messages(
        conversation, is_guest=as_guest, client_id=client_id, text=text, reply=reply,
        staff_sender=staff_sender, image=image, images=images,
    )
    affected = {_desk_recipient(conversation)}
    if seat is not None:
        seats.queue_if_needed(conversation, seat, now)
        if conversation.service_status == seats.Status.WAITING:
            affected.update(seats.drain(config, seat, now, current_conversation=conversation))
    recipient = _desk_recipient(conversation)
    if as_guest and recipient is not None:
        affected.discard(recipient)
        notify_viewers_on_commit(
            [recipient], visitor_incoming={"id": messages[0].pk, "conversation_id": conversation.pk},
        )
    notify_viewers_on_commit(affected)
    if images is not None:
        return {"messages": [message_payload(message, as_guest=as_guest) for message in messages]}
    return message_payload(messages[0], as_guest=as_guest)


@transaction.atomic
def mark_read(conversation, as_guest, seq, staff_id=None):
    config = _lock_reception()
    field, _ = _read_fields(as_guest)
    locked = _locked_room(conversation, staff_id)
    if not as_guest:
        _require_desk_history(locked, config, staff_id)
    if seq > locked.latest_seq:
        raise ValidationError({"seq": "该会话没有此消息位置"})
    current = getattr(locked, field)
    read_seq = max(current, seq)
    if read_seq != current:
        setattr(locked, field, read_seq)
        locked.save(update_fields=[field])
        VisitorChange.objects.create(conversation=locked)
        notify_viewers_on_commit({_desk_recipient(locked)})
    return {"read_seq": read_seq}


@transaction.atomic
def desk_transfer_info(conversation, staff_id):
    conversation = _locked_room(conversation, staff_id)
    config = _reception_settings()
    seat = _desk_seat(conversation, config, staff_id)
    targets = seats.transfer_options(conversation, seat, timezone.now())
    choices = _staff_choices().in_bulk(targets)
    return {
        "candidates": [person_payload(choices[pk]) for pk in targets if pk in choices],
        "history": seats.transfer_history(conversation),
    }


@transaction.atomic
def update_desk_conversation(conversation, staff_id, target_id=None):
    config = _lock_reception()
    conversation = _locked_room(conversation, staff_id)
    seat = _desk_seat(conversation, config, staff_id)
    now = timezone.now()
    if target_id is None:
        affected = seats.close(conversation, config, seat, now)
    else:
        affected = seats.transfer(conversation, config, seat, target_id, now)
    notify_viewers_on_commit(affected)
    rows = _summary_rows().filter(pk=conversation.pk)
    return list_conversations(rows, False, limit=1)["conversations"][0], bool(affected)


def _change_payload(change, *, as_guest):
    payload = {"kind": "conversation.read", "conversation_id": change.conversation_id}
    if change.message_id is not None:
        payload["kind"] = "message.created"
        payload["message"] = message_payload(change.message, as_guest=as_guest)
    return payload


def sync(rows, after, *, as_guest, staff_id=None):
    changes = list(_change_rows(rows, staff_id).filter(pk__gt=after).order_by("pk").select_related(
        "message__conversation",
    ).defer("message__image_data")[:CHANGE_BATCH_SIZE + 1])
    batch = changes[:CHANGE_BATCH_SIZE]
    payload = {
        "cursor": batch[-1].pk if batch else after,
        "has_more": len(changes) > CHANGE_BATCH_SIZE,
        "changes": [_change_payload(change, as_guest=as_guest) for change in batch],
    }
    if staff_id is not None:
        former = {change.conversation_id for change in batch if change.former_staff_id == staff_id}
        if former:
            revoked = former.difference(rows.filter(pk__in=former).values_list("pk", flat=True))
            if revoked:
                payload["revoked_conversation_ids"] = sorted(revoked)
    return payload
