import uuid
from datetime import UTC, datetime

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Case, Count, Exists, F, Func, JSONField, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Coalesce
from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.accounts.admin_mutations import lock_tenant_admin_mutations
from apps.accounts.display import user_display_name
from apps.business.models import PeerDownstreamBinding
from apps.business.roles import WORKER

from . import core_bridge, matrix_bridge
from .images import encode_image, image_fields
from .models import Change, ChangeCursor, ContactDirectory, Conversation, ConversationPreference, GroupMember, Message
from .realtime import notify_on_commit
from .serializers import MAX_ALBUM_IMAGES
from .system_notifications import SYSTEM_NAME, SYSTEM_SENDER_ID

PAGE_SIZE = 60
SEARCH_PAGE_SIZE = 30
CHANGE_BATCH_SIZE = 100


def person_payload(user):
    return {"id": user.pk, "name": user_display_name(user), "avatar_url": None}


def _active_contacts():
    return get_user_model().objects.filter(is_active=True).exclude(staff_profile__deleted_at__isnull=False)


def _eligible_directory_staff():
    return _active_contacts().exclude(groups__name=WORKER)


def _uses_worker_directory(user):
    return user.groups.filter(name=WORKER).exists()


def _directory():
    return ContactDirectory.objects.filter(pk=1).first() or ContactDirectory()


def _directory_ids(directory):
    primary = directory.primary_contact_id
    return ([primary] if primary is not None else []) + directory.backup_contact_ids


def _can_start_worker_dm(peer_id):
    return peer_id in _directory_ids(_directory()) and _eligible_directory_staff().filter(pk=peer_id).exists()


def contacts(user, query):
    worker_directory_only = _uses_worker_directory(user)
    rows = (_eligible_directory_staff() if worker_directory_only else _active_contacts()).exclude(pk=user.pk)
    if query:
        rows = rows.filter(Q(first_name__icontains=query) | Q(username__icontains=query))
    rows = rows.only("id", "first_name", "username")
    if worker_directory_only:
        directory = _directory()
        ids = _directory_ids(directory)
        by_id = rows.in_bulk(ids)
        staff = [
            {**person_payload(by_id[pk]), "is_primary": pk == directory.primary_contact_id}
            for pk in ids if pk in by_id
        ]
    else:
        staff = [person_payload(contact) for contact in rows.order_by("first_name", "pk")[:100]]
    return {"worker_directory_only": worker_directory_only, "contacts": staff}


def directory_settings():
    directory = _directory()
    candidates = [
        person_payload(contact)
        for contact in _eligible_directory_staff().only("id", "first_name", "username").order_by("first_name", "pk")
    ]
    eligible_ids = {contact["id"] for contact in candidates}
    return {
        "primary_contact_id": directory.primary_contact_id if directory.primary_contact_id in eligible_ids else None,
        "backup_contact_ids": [pk for pk in directory.backup_contact_ids if pk in eligible_ids],
        "candidates": candidates,
    }


@transaction.atomic
def update_directory_settings(primary_contact_id, backup_contact_ids):
    values = {"primary_contact_id": primary_contact_id, "backup_contact_ids": backup_contact_ids}
    lock_tenant_admin_mutations()
    if not core_bridge.enabled():
        _lock_cursor()
    ids = _directory_ids(ContactDirectory(**values))
    if _eligible_directory_staff().filter(pk__in=ids).count() != len(ids):
        raise ValidationError("请从当前工作室的在职非打手账号中选择联系人")
    previous = _directory()
    labels = {"primary_contact_id": "首选客服", "backup_contact_ids": "备用客服"}
    changed_fields = [labels[key] for key, value in values.items() if getattr(previous, key) != value]
    ContactDirectory.objects.update_or_create(pk=1, defaults=values)
    return directory_settings(), changed_fields


def message_payload(message, *, system=False):
    payload = {
        "id": message.pk,
        "seq": message.seq,
        "conversation_id": message.conversation_id,
        "sender_id": message.group_sender_id if message.group_sender_id is not None else message.sender_id,
        "text": "" if message.revoked else message.text,
        "created_at": message.created_at.isoformat(),
        "revoked": message.revoked,
        "reply_to_id": message.reply_to_id,
    }
    if not message.revoked and (system or message.system_order_no):
        payload["sender_id"] = SYSTEM_SENDER_ID
        if message.system_order_no:
            payload["system_order_no"] = message.system_order_no
    if not message.revoked and message.image_metadata is not None:
        payload["image"] = message.image_metadata
        if message.album_id is not None:
            payload["album_id"] = str(message.album_id)
    if not message.revoked and message.mention_member_ids:
        payload["mention_member_ids"] = message.mention_member_ids
    return payload


def _membership(user_id, prefix=""):
    direct = Q(**{f"{prefix}first_user_id": user_id}) | Q(**{f"{prefix}second_user_id": user_id})
    direct |= Q(**{f"{prefix}system_recipient_id": user_id})
    if not settings.TT_CHAT_ORDER_GROUPS_ENABLED:
        return direct
    group_member = Q(**{
        f"{prefix}order_group__members__local_user_id": user_id,
        f"{prefix}order_group__members__left_at__isnull": True,
    })
    binding_account = PeerDownstreamBinding.objects.filter(
        account_id=user_id, state=PeerDownstreamBinding.State.ACTIVE,
    )
    manually_joined = Q(**{f"{prefix}order_group__members__manual_joined": True})
    return direct | (group_member & (manually_joined | ~Exists(binding_account)))


def _member_read_field(conversation, user_id):
    return "first_read_seq" if user_id in (conversation.system_recipient_id, conversation.first_user_id) else "second_read_seq"


def _conversation_payload(conversation, user_id, last_message, unread):
    personal = {
        "pinned": bool(getattr(conversation, "viewer_pinned", None)),
        "remark": getattr(conversation, "viewer_remark", None) or "",
        "marked_unread": bool(getattr(conversation, "viewer_marked_unread", None)),
        "first_unread_message_id": getattr(conversation, "first_unread_message_id", None),
        "first_unread_mention_id": getattr(conversation, "first_unread_mention_id", None),
        "unread_mentions_count": getattr(conversation, "unread_mentions_count", 0),
    }
    if conversation.system_recipient_id is not None:
        return {
            "id": conversation.pk,
            "kind": "system",
            "peer": {"id": SYSTEM_SENDER_ID, "name": SYSTEM_NAME, "avatar_url": None},
            "last_message": message_payload(last_message, system=True) if last_message else None,
            "unread_count": unread,
            "read_seq": conversation.first_read_seq,
            "peer_read_seq": 0,
            **personal,
        }
    if conversation.first_user_id is None:
        from .order_groups import can_pin_order_message, visible_pinned_message_id

        group = conversation.order_group
        members = list(group.members.order_by("left_at", "pk"))
        viewer = next(
            (member for member in members if user_id is not None
             and member.local_user_id == user_id and member.left_at is None),
            None,
        )
        return {
            "id": conversation.pk,
            "kind": "order",
            "title": f"订单-{group.order_no}",
            "order_no": group.order_no,
            "local_order_no": group.order.order_no if group.order_id else None,
            "members": [
                {
                    "id": row.pk, "name": row.name, "avatar_url": None,
                    "studio_name": row.studio_name, "order_role": row.order_role,
                    "left_at": row.left_at.isoformat() if row.left_at is not None else None,
                }
                for row in members
            ],
            "self_member_id": viewer.pk if viewer else None,
            "is_member": viewer is not None,
            "last_message": message_payload(last_message) if last_message else None,
            "unread_count": unread,
            "read_seq": viewer.read_seq if viewer else 0,
            "peer_read_seq": 0,
            "pinned_message_id": visible_pinned_message_id(group),
            "can_pin_messages": can_pin_order_message(group, viewer),
            **personal,
        }
    if conversation.first_user_id == user_id:
        peer = conversation.second_user
        read_seq, peer_read_seq = conversation.first_read_seq, conversation.second_read_seq
    else:
        peer = conversation.first_user
        read_seq, peer_read_seq = conversation.second_read_seq, conversation.first_read_seq
    return {
        "id": conversation.pk,
        "peer": person_payload(peer),
        "last_message": message_payload(last_message) if last_message else None,
        "unread_count": unread,
        "read_seq": read_seq,
        "peer_read_seq": peer_read_seq,
        **personal,
    }


def _conversation_read_state(user_id):
    member = GroupMember.objects.filter(
        group__conversation_id=OuterRef("pk"), local_user_id=user_id, left_at__isnull=True,
    )
    preferences = ConversationPreference.objects.filter(conversation_id=OuterRef("pk"), user_id=user_id)
    return Conversation.objects.filter(_membership(user_id)).annotate(
        viewer_read_seq=Case(
            When(Q(first_user_id=user_id) | Q(system_recipient_id=user_id), then=F("first_read_seq")),
            When(second_user_id=user_id, then=F("second_read_seq")),
            default=Subquery(member.values("read_seq")[:1]),
        ),
        viewer_member_id=Subquery(member.values("pk")[:1]),
        viewer_pinned=Coalesce(Subquery(preferences.values("pinned")[:1]), Value(False)),
        viewer_remark=Subquery(preferences.values("remark")[:1]),
        viewer_marked_unread=Coalesce(Subquery(preferences.values("marked_unread")[:1]), Value(False)),
    )


def _received_unread_messages(user_id):
    return Message.objects.filter(
        conversation_id=OuterRef("pk"), seq__gt=OuterRef("viewer_read_seq"), revoked=False,
    ).exclude(Q(sender_id=user_id) | Q(group_sender__local_user_id=user_id))


def _conversation_rows(user_id):
    latest = Message.objects.filter(conversation_id=OuterRef("pk")).order_by("-seq")
    unread_filter = Q(messages__seq__gt=F("viewer_read_seq"), messages__revoked=False)
    sent_filter = Q(messages__sender_id=user_id) | Q(messages__group_sender__local_user_id=user_id)
    unread_received = unread_filter & ~sent_filter
    unread_messages = _received_unread_messages(user_id).order_by("seq")
    unread_mentions = unread_messages.filter(
        mention_member_ids__contains=Func(
            OuterRef("viewer_member_id"), function="jsonb_build_array", output_field=JSONField(),
        ),
    )
    return (
        _conversation_read_state(user_id)
        .select_related("first_user", "second_user", "order_group", "order_group__order")
        .annotate(last_message_id=Subquery(latest.values("pk")[:1]))
        .annotate(
            unread=(
                Count("messages", filter=unread_received & Q(messages__album_id__isnull=True))
                + Count("messages__album_id", distinct=True, filter=unread_received)
            ),
            first_unread_message_id=Subquery(unread_messages.values("pk")[:1]),
            first_unread_mention_id=Subquery(unread_mentions.values("pk")[:1]),
            unread_mentions_count=Count(
                "messages", filter=unread_received
                & Q(messages__mention_member_ids__contains=Func(
                    F("viewer_member_id"), function="jsonb_build_array", output_field=JSONField(),
                )),
            ),
        )
    )


def _visible_changes(user_id):
    visible = Q(viewer__isnull=True) & _membership(user_id, "conversation__")
    visible |= Q(viewer_id=user_id, kind=Change.Kind.PREFERENCES_CHANGED) & _membership(user_id, "conversation__")
    if settings.TT_CHAT_ORDER_GROUPS_ENABLED:
        visible |= Q(viewer_id=user_id)
    return Change.objects.filter(visible).distinct()


def unread_total(user_id):
    if core_bridge.enabled():
        return core_bridge.operation('unread', user_id)['unread_count']
    unread_counts = (
        _received_unread_messages(user_id)
        .order_by()
        .values("conversation_id")
        .annotate(total=Count("pk", filter=Q(album_id__isnull=True)) + Count("album_id", distinct=True))
        .values("total")
    )
    rows = _conversation_read_state(user_id).order_by().annotate(unread=Coalesce(Subquery(unread_counts), Value(0)))
    return sum(max(unread, int(marked)) for unread, marked in rows.values_list("unread", "viewer_marked_unread").iterator())


def conversations(user_id, *, limit=None, offset=None):
    if core_bridge.enabled():
        query = {key: value for key, value in {'limit': limit, 'offset': offset}.items() if value is not None}
        return core_bridge.operation('conversations', user_id, query=query)
    cursor = _visible_changes(user_id).order_by("-pk").values_list("pk", flat=True).first() or 0
    rows = list(
        _conversation_rows(user_id).order_by(
            "-viewer_pinned", F("last_message_id").desc(nulls_last=True), "-pk",
        )
    )
    summaries = Message.objects.defer("image_data").in_bulk(row.last_message_id for row in rows if row.last_message_id)
    return {
        "cursor": cursor,
        "conversations": [
            _conversation_payload(row, user_id, summaries.get(row.last_message_id), row.unread)
            for row in rows
        ],
    }


def _lock_cursor():
    # Allocating change IDs under one tenant-local lock makes cursor order match commit order.
    ChangeCursor.objects.get_or_create(pk=1)
    return ChangeCursor.objects.select_for_update().get(pk=1)


def _lock_legacy_cursor():
    cursor = _lock_cursor()
    if core_bridge.enabled():
        raise matrix_bridge.ChatServiceUnavailable()
    return cursor


def _record_change(cursor, conversation, kind, message=None, *, viewer_id=None, notify_message=False):
    change = Change.objects.create(
        conversation=conversation, kind=kind, message=message, viewer_id=viewer_id,
    )
    cursor.latest = change.pk
    cursor.save(update_fields=["latest"])
    notify_on_commit(
        conversation, viewer_id=viewer_id, incoming_message=message if notify_message else None,
    )


def start_conversation(user, peer_id):
    if core_bridge.enabled():
        return core_bridge.start(user, peer_id)
    return _legacy_start_conversation(user, peer_id)


@transaction.atomic
def _legacy_start_conversation(user, peer_id):
    user_id = user.pk
    if user_id == peer_id:
        raise ValidationError({"peer_id": "请选择其他成员"})
    cursor = _lock_legacy_cursor()
    first_id, second_id = sorted((user_id, peer_id))
    pair = {"first_user_id": first_id, "second_user_id": second_id}
    conversation = Conversation.objects.filter(**pair).first()
    if conversation is None and _uses_worker_directory(user):
        if not _can_start_worker_dm(peer_id):
            raise PermissionDenied("请从打手通讯录发起新私聊")
    else:
        get_object_or_404(_active_contacts(), pk=peer_id)
    if conversation is None:
        conversation = Conversation.objects.create(**pair)
        _record_change(cursor, conversation, Change.Kind.CONVERSATION_CREATED)
    row = _conversation_rows(user_id).get(pk=conversation.pk)
    last_message = (
        Message.objects.defer("image_data").filter(pk=row.last_message_id).first()
        if row.last_message_id else None
    )
    return _conversation_payload(row, user_id, last_message, row.unread)


def _get_conversation(user_id, conversation_id):
    return get_object_or_404(Conversation, _membership(user_id), pk=conversation_id)


def direct_conversation_peer(user_id, conversation_id):
    if core_bridge.enabled():
        return core_bridge.operation('peer', user_id, conversation_id)['peer_id']
    conversation = get_object_or_404(
        Conversation, Q(first_user_id=user_id) | Q(second_user_id=user_id), pk=conversation_id,
    )
    return conversation.second_user_id if conversation.first_user_id == user_id else conversation.first_user_id


def order_summary(group, user_id):
    if core_bridge.enabled():
        return core_bridge.call('order', user_id, query={'conversation_id': group.conversation_id}, readonly=True)
    from .order_groups import can_join_order_group

    conversation = _conversation_rows(user_id).filter(pk=group.conversation_id).first()
    if conversation is None:
        conversation = group.conversation
        user_id = None
    last = conversation.messages.defer("image_data").order_by("-seq").first()
    payload = _conversation_payload(conversation, user_id, last, getattr(conversation, "unread", 0))
    payload["can_join"] = can_join_order_group(group)
    return payload


def history(user_id, conversation_id, *, before=None, around=None):
    if core_bridge.enabled():
        return core_bridge.operation('history', user_id, conversation_id, query={key: value for key, value in {'before': before, 'around': around}.items() if value is not None})
    conversation = _get_conversation(user_id, conversation_id)
    if conversation.system_recipient_id is None:
        remote = matrix_bridge.remote_read("history", conversation.pk, user_id, before=before, around=around)
        if remote is not None:
            return remote
    rows = conversation.messages.filter(_membership(user_id, "conversation__"))
    return _history_page(conversation, rows, before=before, around=around)


def order_history(group, *, before=None, around=None, viewer_id=None):
    if core_bridge.enabled():
        return core_bridge.call('history', viewer_id, query={key: value for key, value in {'conversation_id': group.conversation_id, 'before': before, 'around': around}.items() if value is not None}, readonly=True)
    conversation = group.conversation
    remote = matrix_bridge.remote_read("history", conversation.pk, viewer_id, readonly=True, before=before, around=around)
    if remote is not None:
        return remote
    return _history_page(conversation, conversation.messages.all(), before=before, around=around)


def _history_page_edge(rows, limit):
    candidates = list(rows[:limit + MAX_ALBUM_IMAGES])
    end = limit
    if 0 < limit < len(candidates) and (album_id := candidates[limit - 1].album_id) is not None:
        max_end = min(len(candidates), limit + MAX_ALBUM_IMAGES - 1)
        while end < max_end and candidates[end].album_id == album_id:
            end += 1
    return candidates[:end], len(candidates) > end


def _history_page(conversation, rows, *, before, around):
    rows = rows.defer("image_data")
    system = conversation.system_recipient_id is not None
    if around is not None:
        target = get_object_or_404(rows, pk=around)
        half = PAGE_SIZE // 2
        older, has_older = _history_page_edge(rows.filter(seq__lt=target.seq).order_by("-seq"), half)
        newer, has_newer = _history_page_edge(rows.filter(seq__gt=target.seq).order_by("seq"), half)
        page = [*reversed(older), target, *newer]
        return {
            "messages": [message_payload(row, system=system) for row in page],
            "has_more": has_older,
            "has_newer": has_newer,
        }
    if before is not None:
        rows = rows.filter(seq__lt=before)
    page, has_older = _history_page_edge(rows.order_by("-seq"), PAGE_SIZE)
    return {
        "messages": [message_payload(row, system=system) for row in reversed(page)],
        "has_more": has_older,
        "has_newer": bool(page and page[0].seq < conversation.latest_seq),
    }


def search_messages(user_id, conversation_id, q, *, before=None, sender_id=None,
                    date_from=None, date_to=None, image_only=False):
    if core_bridge.enabled():
        query = {key: value for key, value in {'q': q, 'before': before, 'sender_id': sender_id,
                 'date_from': date_from, 'date_to': date_to, 'image_only': image_only}.items() if value is not None}
        return core_bridge.operation('search', user_id, conversation_id, query=query)
    conversation = _get_conversation(user_id, conversation_id)
    if conversation.system_recipient_id is None:
        remote = matrix_bridge.remote_read(
            "search", conversation.pk, user_id, q=q, before=before, sender_id=sender_id,
            date_from=date_from, date_to=date_to, image_only=image_only,
        )
        if remote is not None:
            return remote
    matches = conversation.messages.filter(
        _membership(user_id, "conversation__"), revoked=False,
    ).defer("image_data")
    if q:
        matches = matches.filter(text__icontains=q)
    if sender_id is not None:
        if conversation.system_recipient_id is not None:
            if sender_id != SYSTEM_SENDER_ID:
                matches = matches.none()
        else:
            sender_field = "group_sender_id" if conversation.first_user_id is None else "sender_id"
            matches = matches.filter(**{sender_field: sender_id})
    if date_from is not None:
        matches = matches.filter(created_at__gte=datetime.fromtimestamp(date_from, UTC))
    if date_to is not None:
        matches = matches.filter(created_at__lte=datetime.fromtimestamp(date_to, UTC).replace(microsecond=999999))
    if image_only:
        matches = matches.filter(image_metadata__isnull=False, image_data__isnull=False)
    rows = matches if before is None else matches.filter(seq__lt=before)
    page = list(rows.order_by("-seq")[:SEARCH_PAGE_SIZE + 1])
    system = conversation.system_recipient_id is not None
    return {
        "messages": [message_payload(row, system=system) for row in page[:SEARCH_PAGE_SIZE]],
        "count": matches.count(),
        "has_more": len(page) > SEARCH_PAGE_SIZE,
    }


def read_image(user_id, conversation_id, message_id):
    if core_bridge.enabled():
        return core_bridge.operation('image', user_id, conversation_id, query={'message_id': message_id})
    conversation = _get_conversation(user_id, conversation_id)
    if conversation.system_recipient_id is None:
        remote = matrix_bridge.remote_read("image", conversation.pk, user_id, message_id=message_id)
        if remote is not None:
            return remote
    rows = Message.objects.filter(_membership(user_id, "conversation__"), conversation_id=conversation_id)
    return _image_from_rows(rows, message_id)


def order_image(group, message_id, *, viewer_id=None):
    if core_bridge.enabled():
        return core_bridge.call('image', viewer_id, query={'conversation_id': group.conversation_id, 'message_id': message_id}, readonly=True)
    remote = matrix_bridge.remote_read("image", group.conversation_id, viewer_id, readonly=True, message_id=message_id)
    if remote is not None:
        return remote
    return _image_from_rows(group.conversation.messages.all(), message_id)


def _image_from_rows(rows, message_id):
    rows = rows.filter(
        revoked=False,
        image_metadata__isnull=False, image_data__isnull=False,
    ).values_list("image_metadata", "image_data")
    metadata, data = get_object_or_404(rows, pk=message_id)
    return encode_image(metadata, data)


def update_preferences(user_id, conversation_id, **values):
    if core_bridge.enabled():
        return core_bridge.operation('preferences', user_id, conversation_id, payload=values)
    return _legacy_update_preferences(user_id, conversation_id, **values)


@transaction.atomic
def _legacy_update_preferences(user_id, conversation_id, **values):
    cursor = _lock_legacy_cursor()
    conversation = _get_conversation(user_id, conversation_id)
    row, created = ConversationPreference.objects.get_or_create(
        conversation=conversation, user_id=user_id, defaults=values,
    )
    changed = [field for field, value in values.items() if getattr(row, field) != value]
    for field in changed:
        setattr(row, field, values[field])
    if changed:
        row.save(update_fields=changed)
    if created or changed:
        _record_change(cursor, conversation, Change.Kind.PREFERENCES_CHANGED, viewer_id=user_id)
    return {"pinned": row.pinned, "remark": row.remark, "marked_unread": row.marked_unread}


def _validate_mentions(group_sender, member_ids):
    if not member_ids:
        return []
    ids = list(dict.fromkeys(member_ids))
    if group_sender is None or group_sender.group.members.filter(
        pk__in=ids, left_at__isnull=True,
    ).exclude(pk=group_sender.pk).count() != len(ids):
        raise ValidationError({"mention_member_ids": "只能点名当前群聊的其他在群成员"})
    return ids


def send(user, conversation_id, *, text, client_id, reply_to_id=None, image=None, images=None, mention_member_ids=None):
    if core_bridge.enabled():
        return core_bridge.send(user, conversation_id, text=text, client_id=client_id, reply_to_id=reply_to_id, image=image, images=images, mention_member_ids=mention_member_ids)
    return _legacy_send(user, conversation_id, text=text, client_id=client_id, reply_to_id=reply_to_id, image=image, images=images, mention_member_ids=mention_member_ids)


@transaction.atomic
def _legacy_send(user, conversation_id, *, text, client_id, reply_to_id=None, image=None, images=None, mention_member_ids=None):
    user_id = user.pk
    cursor = _lock_legacy_cursor()
    conversation = _get_conversation(user_id, conversation_id)
    if conversation.system_recipient_id is not None:
        raise PermissionDenied("系统通知会话仅可查看")
    group_sender = None
    if conversation.first_user_id is None:
        from .order_groups import can_join_order_group

        group = conversation.order_group
        if not can_join_order_group(group):
            raise PermissionDenied("群聊已结束，仅可查看历史消息")
        group_sender = group.members.get(local_user_id=user_id, left_at__isnull=True)
    sender_filter = (
        {"group_sender": group_sender} if group_sender is not None else {"sender_id": user_id}
    )
    existing = conversation.messages.defer("image_data").filter(**sender_filter, client_id=client_id).first()
    if existing:
        if images is not None and existing.album_id is not None:
            album = list(conversation.messages.defer("image_data").filter(
                **sender_filter, album_id=existing.album_id,
            ).order_by("seq"))
            if album[0].pk == existing.pk:
                return {"messages": [message_payload(part) for part in album]}
        elif images is None and existing.album_id is None:
            return message_payload(existing)
        raise ValidationError({"client_id": "此发送编号已用于其他消息类型"})
    if group_sender is None and conversation.latest_seq == 0 and _uses_worker_directory(user):
        peer_id = conversation.second_user_id if conversation.first_user_id == user_id else conversation.first_user_id
        if not _can_start_worker_dm(peer_id):
            raise PermissionDenied("请从打手通讯录发起新私聊")
    mentions = _validate_mentions(group_sender, mention_member_ids)
    if reply_to_id is not None:
        get_object_or_404(conversation.messages.filter(revoked=False).only("pk"), pk=reply_to_id)
    latest_seq = conversation.latest_seq
    album_id = uuid.uuid4() if images is not None else None
    parts = images if images is not None else [image]
    conversation.latest_seq += len(parts)
    conversation.save(update_fields=["latest_seq"])
    messages = []
    for index, part in enumerate(parts):
        message = conversation.messages.create(
            **sender_filter,
            wire_id=uuid.uuid4() if group_sender is not None else None,
            seq=latest_seq + index + 1,
            client_id=client_id if index == 0 else f"album:{album_id.hex}:{index}",
            album_id=album_id,
            text=text if index == 0 else "",
            reply_to_id=reply_to_id if index == 0 else None,
            mention_member_ids=mentions if index == 0 else [],
            **image_fields(part),
        )
        messages.append(message)
        _record_change(
            cursor, conversation, Change.Kind.MESSAGE_CREATED, message, notify_message=index == 0,
        )
    if group_sender is not None:
        from .order_groups import enqueue_album, enqueue_message
        from .wxpusher import queue_group_notice

        queue_group_notice(conversation.order_group, messages[0])
        if album_id is not None:
            enqueue_album(conversation.order_group, album_id)
        else:
            enqueue_message(conversation.order_group, messages[0], group_sender)
    else:
        from .wxpusher import queue_direct_notice

        queue_direct_notice(conversation, messages[0], sender_name=user_display_name(user))
    if album_id is not None:
        return {"messages": [message_payload(message) for message in messages]}
    return message_payload(messages[0])


def pin_order_message(user_id, conversation_id, *, message_id):
    if core_bridge.enabled():
        return core_bridge.operation('pin', user_id, conversation_id, payload={'message_id': message_id})
    return _legacy_pin_order_message(user_id, conversation_id, message_id=message_id)


@transaction.atomic
def _legacy_pin_order_message(user_id, conversation_id, *, message_id):
    from .order_groups import can_pin_order_message, change_pin, visible_pinned_message_id

    cursor = _lock_legacy_cursor()
    conversation = _get_conversation(user_id, conversation_id)
    if conversation.first_user_id is not None or conversation.system_recipient_id is not None:
        raise PermissionDenied("只有订单群支持置顶消息")
    group = conversation.order_group
    member = group.members.get(local_user_id=user_id, left_at__isnull=True)
    if not can_pin_order_message(group, member):
        raise PermissionDenied("当前成员不能管理订单群置顶消息")
    wire_id = None
    if message_id is not None:
        message = get_object_or_404(conversation.messages.only("wire_id"), pk=message_id, revoked=False)
        wire_id = message.wire_id
    change_pin(cursor, group, wire_id)
    return {"pinned_message_id": visible_pinned_message_id(group)}


def mark_read(user_id, conversation_id, seq):
    if core_bridge.enabled():
        return core_bridge.operation('read', user_id, conversation_id, payload={'seq': seq})['read_seq']
    return _legacy_mark_read(user_id, conversation_id, seq)


@transaction.atomic
def _legacy_mark_read(user_id, conversation_id, seq):
    cursor = _lock_legacy_cursor()
    conversation = _get_conversation(user_id, conversation_id)
    if seq > conversation.latest_seq:
        raise ValidationError({"seq": "该会话没有此消息位置"})
    read_owner = conversation
    if conversation.first_user_id is None and conversation.system_recipient_id is None:
        read_owner = conversation.order_group.members.get(local_user_id=user_id, left_at__isnull=True)
    field = "read_seq" if isinstance(read_owner, GroupMember) else _member_read_field(conversation, user_id)
    current = getattr(read_owner, field)
    if ConversationPreference.objects.filter(
        conversation=conversation, user_id=user_id, marked_unread=True,
    ).update(marked_unread=False):
        _record_change(cursor, conversation, Change.Kind.PREFERENCES_CHANGED, viewer_id=user_id)
    if seq > current:
        setattr(read_owner, field, seq)
        read_owner.save(update_fields=[field])
        _record_change(
            cursor, conversation, Change.Kind.CONVERSATION_READ,
            viewer_id=user_id if isinstance(read_owner, GroupMember) else None,
        )
        return seq
    return current


def revoke(user_id, conversation_id, message_id):
    if core_bridge.enabled():
        return core_bridge.operation('revoke', user_id, conversation_id, query={'message_id': message_id})
    return _legacy_revoke(user_id, conversation_id, message_id)


@transaction.atomic
def _legacy_revoke(user_id, conversation_id, message_id):
    cursor = _lock_legacy_cursor()
    conversation = _get_conversation(user_id, conversation_id)
    message = get_object_or_404(conversation.messages.defer("image_data"), pk=message_id)
    sender = message.group_sender
    author_user_id = sender.local_user_id if sender is not None else message.sender_id
    if author_user_id != user_id:
        raise PermissionDenied("只能撤回自己发送的消息")
    if not message.revoked:
        message.revoke_content()
        _record_change(cursor, conversation, Change.Kind.MESSAGE_REVOKED, message)
        if sender is not None:
            from .order_groups import clear_revoked_pin, enqueue_album, enqueue_message

            group = conversation.order_group
            clear_revoked_pin(cursor, group, message)
            if message.album_id is not None:
                enqueue_album(group, message.album_id)
            else:
                enqueue_message(group, message, sender)
    return message_payload(message)


def changes_since(user_id, after):
    if core_bridge.enabled():
        return core_bridge.operation('sync', user_id, query={'after': after})
    high_water = ChangeCursor.objects.filter(pk=1).values_list("latest", flat=True).first() or 0
    batch = list(
        _visible_changes(user_id)
        .filter(pk__gt=after, pk__lte=high_water)
        .annotate(system_recipient_id=F("conversation__system_recipient_id"))
        .select_related("message")
        .defer("message__image_data")
        .order_by("pk")[:CHANGE_BATCH_SIZE + 1]
    )
    has_more = len(batch) > CHANGE_BATCH_SIZE
    changes = batch[:CHANGE_BATCH_SIZE]
    return {
        "cursor": changes[-1].pk if changes else after,
        "has_more": has_more,
        "changes": [
            {
                "kind": change.kind,
                "conversation_id": change.conversation_id,
                "message": message_payload(
                    change.message, system=change.system_recipient_id is not None,
                ) if change.message_id else None,
            }
            for change in changes
        ],
    }
