import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.accounts.display import user_display_name
from apps.business.models import (
    JidanpuUpstreamOrderLink,
    OrderStatus,
    PeerDownstreamBinding,
    PeerSyncEvent,
    PeerUpstreamBinding,
)
from apps.business.peer_wakeup import notify_hall_take as notify_peer_delivery

from .order_intents import core_requested
from .images import ImageInput, encode_image, image_fields
from .models import Change, Conversation, Message, OrderGroup, OrderMemberRole
from .serializers import MAX_ALBUM_BYTES, MAX_ALBUM_IMAGES, MAX_BIGINT
from .service import _lock_cursor, _record_change

ROSTER_EVENT = "chat.order_roster"
MESSAGE_EVENT = "chat.order_message"
ALBUM_EVENT = "chat.order_album"
PIN_EVENT = "chat.order_pin"
DETACHED_EVENT = "chat.order_detached"
EVENT_KINDS = frozenset({ROSTER_EVENT, MESSAGE_EVENT, ALBUM_EVENT, PIN_EVENT, DETACHED_EVENT})
DOWNSTREAM = "downstream"
UPSTREAM = "upstream"
PUBLISHER = "a"
MAX_PIN_REVISION = MAX_BIGINT - 1


class Invitation(serializers.Serializer):
    group_id = serializers.UUIDField()
    order_no = serializers.RegexField(r"^[0-9]{20}$")
    publisher_name = serializers.CharField(max_length=150)
    publisher_studio_name = serializers.CharField(max_length=100, required=False, allow_blank=True)


class PeerMember(serializers.Serializer):
    key = serializers.RegexField(r"^[ab]:[1-9][0-9]{0,18}$")
    name = serializers.CharField(max_length=150)
    left_at = serializers.DateTimeField(allow_null=True)
    studio_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    order_role = serializers.ChoiceField(OrderMemberRole.values, required=False, allow_blank=True)


class PeerRoster(serializers.Serializer):
    version = serializers.IntegerField(min_value=1, max_value=2**63 - 1)
    members = PeerMember(many=True)

    def validate_members(self, value):
        if len({row["key"] for row in value}) != len(value):
            raise serializers.ValidationError("群成员重复")
        return value


class PeerMessage(serializers.Serializer):
    message_id = serializers.UUIDField()
    sender = serializers.RegexField(r"^(a|[ab]:[1-9][0-9]{0,18})$")
    sender_name = serializers.CharField(max_length=150)
    sender_left_at = serializers.DateTimeField(allow_null=True)
    sender_studio_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    sender_order_role = serializers.ChoiceField(OrderMemberRole.values, required=False, allow_blank=True)
    text = serializers.CharField(max_length=4000, allow_blank=True, trim_whitespace=False)
    created_at = serializers.DateTimeField()
    revoked = serializers.BooleanField()
    reply_to = serializers.UUIDField(allow_null=True)
    roster = PeerRoster(required=False)
    history = serializers.BooleanField(required=False, default=False)
    image = ImageInput(required=False)
    mention_keys = serializers.ListField(
        child=serializers.RegexField(r"^(a|[ab]:[1-9][0-9]{0,18})$"),
        max_length=50, required=False,
    )


class PeerAlbum(serializers.Serializer):
    album_id = serializers.UUIDField()
    messages = PeerMessage(many=True, min_length=2, max_length=MAX_ALBUM_IMAGES)
    history = serializers.BooleanField(required=False, default=False)
    roster = PeerRoster(required=False)

    def validate_messages(self, messages):
        if (len({part["message_id"] for part in messages}) != len(messages)
                or len({part["sender"] for part in messages}) != 1):
            raise serializers.ValidationError("相册的消息标识或发件人不一致")
        if any("roster" in part or (not part["revoked"] and "image" not in part) for part in messages):
            raise serializers.ValidationError("相册内只能包含图片或撤回记录")
        if any(part["text"] or part["reply_to"] or part.get("mention_keys") for part in messages[1:]):
            raise serializers.ValidationError("相册说明、引用和提醒只能写在首张图片")
        if sum(len(part["image"]["data"]) for part in messages if "image" in part) > MAX_ALBUM_BYTES:
            raise serializers.ValidationError("多张图片合计不能超过 5.25 MiB")
        return messages


class PeerPin(serializers.Serializer):
    message_id = serializers.UUIDField(allow_null=True)
    revision = serializers.IntegerField(min_value=1, max_value=MAX_PIN_REVISION)
    operation_id = serializers.UUIDField()


def _tenant_studio_name():
    return getattr(connection.tenant, "name", "").strip()


def _can_join_group(user_id):
    return not PeerDownstreamBinding.objects.filter(
        account_id=user_id, state=PeerDownstreamBinding.State.ACTIVE,
    ).exists()


def can_follow_order_chat(order, user_id):
    return _followable_groups(user_id).filter(order=order).exists()


def followable_order_numbers(order_nos, user_id):
    groups = _followable_groups(user_id).filter(order__order_no__in=order_nos)
    return set(groups.values_list("order__order_no", flat=True))


def _followable_groups(user_id):
    if not settings.TT_CHAT_ORDER_GROUPS_ENABLED or not _can_join_group(user_id):
        return OrderGroup.objects.none()
    active_upstream = PeerUpstreamBinding.objects.filter(
        enabled=True, deleted_at__isnull=True,
    ).values("pk")
    return OrderGroup.objects.filter(
        Q(binding_role__in=("", DOWNSTREAM), binding_id__isnull=True)
        | Q(binding_role=UPSTREAM, binding_id__in=active_upstream),
    )


def publish(order, publisher):
    if core_requested():
        return
    if not settings.TT_CHAT_ORDER_GROUPS_ENABLED or not _can_join_group(publisher.pk):
        return
    cursor = _lock_cursor()
    group = OrderGroup.objects.filter(order=order).select_related("conversation").first()
    created = group is None
    if created:
        group = _create_local_group(order)
    elif group.binding_role or group.binding_id is not None or group.members.filter(wire_key=PUBLISHER).exists():
        return
    if created or not group.members.filter(local_user=publisher).update(
        wire_key=PUBLISHER, publisher=True, left_at=None,
    ):
        group.members.create(
            wire_key=PUBLISHER, local_user=publisher, name=user_display_name(publisher),
            studio_name=_tenant_studio_name(), publisher=True,
        )
    kind = Change.Kind.CONVERSATION_CREATED if created else Change.Kind.MEMBERS_CHANGED
    _record_change(cursor, group.conversation, kind)


def _create_local_group(order):
    conversation = Conversation.objects.create()
    return OrderGroup.objects.create(
        conversation=conversation, order=order, order_no=order.order_no, wire_id=uuid.uuid4(),
    )


def authorize_claim(order, binding):
    if not settings.TT_CHAT_ORDER_GROUPS_ENABLED:
        return None
    if core_requested():
        from . import peer_chat

        return peer_chat.authorize_claim(order, binding)
    cursor = _lock_cursor()
    group = OrderGroup.objects.filter(order=order).select_related("conversation").first()
    if group is None or group.binding_role == UPSTREAM:
        return None
    if group.binding_role == DOWNSTREAM and group.binding_id != binding.pk:
        return None
    publisher = group.members.filter(wire_key=PUBLISHER, publisher=True).first()
    if publisher is None:
        return None
    if _retire_workers(cursor, group, None):
        _record_change(cursor, group.conversation, Change.Kind.MEMBERS_CHANGED)
    group.binding_role = DOWNSTREAM
    group.binding_id = binding.pk
    group.save(update_fields=["binding_role", "binding_id"])
    return {
        "group_id": str(group.wire_id), "order_no": group.order_no,
        "publisher_name": publisher.name, "publisher_studio_name": publisher.studio_name,
    }


def confirm_claim(order, binding, acceptor, invitation):
    from apps.business.models import EmpType

    if (
        not settings.TT_CHAT_ORDER_GROUPS_ENABLED or not isinstance(invitation, dict)
        or order.deleted_at is not None or order.status == OrderStatus.REFUNDED
        or not _can_join_group(acceptor.pk)
    ):
        return
    parsed = Invitation(data=invitation)
    if not parsed.is_valid():
        return
    fields = parsed.validated_data
    if core_requested():
        from . import peer_chat

        peer_chat.confirm_claim(order, binding, acceptor, fields)
        return
    cursor = _lock_cursor()
    if OrderGroup.objects.filter(order=order).exists():
        return
    if OrderGroup.objects.filter(wire_id=fields["group_id"]).exists():
        return
    conversation = Conversation.objects.create()
    group = OrderGroup.objects.create(
        conversation=conversation, order=order, wire_id=fields["group_id"],
        order_no=fields["order_no"], binding_role=UPSTREAM, binding_id=binding.pk,
    )
    group.members.create(
        wire_key=PUBLISHER, name=fields["publisher_name"],
        studio_name=fields.get("publisher_studio_name", ""), publisher=True,
    )
    member = _local_member(group, acceptor)
    member.acceptor = True
    member.save(update_fields=["acceptor"])
    assigned = order.performances.filter(emp_type=EmpType.MAIN).select_related("user").first()
    if assigned is not None and assigned.user_id is not None and _can_join_group(assigned.user_id):
        worker = _local_member(group, assigned.user)
        worker.worker = True
        worker.save(update_fields=["worker"])
    _record_change(cursor, conversation, Change.Kind.CONVERSATION_CREATED)
    _enqueue_roster(group)


def _local_member(group, user):
    member, _ = group.members.get_or_create(
        local_user=user,
        defaults={
            "wire_key": f"{'b' if group.binding_role == UPSTREAM else 'a'}:{user.pk}",
            "name": user_display_name(user),
            "studio_name": _tenant_studio_name(),
        },
    )
    return member


def can_join_order_group(group):
    return group.binding_role != UPSTREAM or group.binding_id is not None


def _pin_allowed_in_group(group):
    return bool(group.order_id and can_join_order_group(group) and
                group.order.deleted_at is None and group.order.status != OrderStatus.REFUNDED)


def can_pin_order_message(group, member):
    return bool(member is not None and (member.publisher or member.acceptor or member.designated_follower)
                and _pin_allowed_in_group(group))


def visible_pinned_message_id(group):
    if group.pinned_message_wire_id is not None:
        return group.conversation.messages.filter(
            wire_id=group.pinned_message_wire_id, revoked=False,
        ).values_list("pk", flat=True).first()
    return None


def _enqueue_pin(group):
    _enqueue_chat_event(group, {
        "message_id": str(group.pinned_message_wire_id) if group.pinned_message_wire_id else None,
        "revision": group.pin_revision,
        "operation_id": str(group.pin_operation_id),
    }, kind=PIN_EVENT)


def _save_pin(cursor, group, wire_id, revision, operation_id):
    group.pinned_message_wire_id = wire_id
    group.pin_revision = revision
    group.pin_operation_id = operation_id
    group.save(update_fields=["pinned_message_wire_id", "pin_revision", "pin_operation_id"])
    _record_change(cursor, group.conversation, Change.Kind.PIN_CHANGED)


def change_pin(cursor, group, wire_id):
    if group.pinned_message_wire_id != wire_id:
        if group.pin_revision >= MAX_PIN_REVISION:
            raise ValidationError("置顶变更已达到版本上限")
        _save_pin(cursor, group, wire_id, group.pin_revision + 1, uuid.uuid4())
        if group.binding_role == UPSTREAM or group.roster_version:
            _enqueue_pin(group)


def clear_revoked_pin(cursor, group, message):
    if message.wire_id is not None and group.pinned_message_wire_id == message.wire_id:
        _save_pin(cursor, group, None, group.pin_revision, group.pin_operation_id)


def _receive_pin(cursor, group, data):
    if not _pin_allowed_in_group(group):
        return "群聊已结束，不能同步置顶消息"
    revision, operation_id = data["revision"], data["operation_id"]
    current = group.pin_operation_id or uuid.UUID(int=0)
    if (revision, operation_id) <= (group.pin_revision, current):
        return None
    wire_id = data["message_id"]
    if wire_id is not None:
        target = Message.objects.filter(wire_id=wire_id).values("conversation_id", "revoked").first()
        if target is not None and target["conversation_id"] != group.conversation_id:
            return "置顶消息属于其他会话"
        if target is not None and target["revoked"]:
            wire_id = None
    _save_pin(cursor, group, wire_id, revision, operation_id)
    return None


@transaction.atomic
def join_order_group(user, resolve_group):
    cursor = _lock_cursor()
    group = resolve_group()
    if not can_join_order_group(group):
        raise PermissionDenied("群聊已结束，仅可查看历史消息")
    member = group.members.filter(local_user=user).first()
    if member is not None and member.left_at is None and (member.manual_joined or _can_join_group(user.pk)):
        return group
    if member is None:
        member = _local_member(group, user)
    member.follower = True
    member.manual_joined = True
    member.left_at = None
    member.save(update_fields=["follower", "manual_joined", "left_at"])
    _record_change(cursor, group.conversation, Change.Kind.MEMBERS_CHANGED)
    if group.binding_role == UPSTREAM:
        _enqueue_roster(group)
    elif group.binding_role == DOWNSTREAM and group.roster_version:
        _enqueue_source_roster(group)
    return group


def _retire_workers(cursor, group, current_worker_id):
    former_workers = list(group.members.filter(worker=True).exclude(local_user_id=current_worker_id))
    for former in former_workers:
        former.worker = False
        if not (former.publisher or former.acceptor or former.follower):
            _retire_member(cursor, group, former, timezone.now())
        former.save(update_fields=["worker", "left_at"])
    return bool(former_workers)


def _retire_member(cursor, group, member, moment):
    member.left_at = moment
    if member.local_user_id is not None:
        _record_change(cursor, group.conversation, Change.Kind.CONVERSATION_REMOVED,
                       viewer_id=member.local_user_id)


def _disconnect_group(cursor, group):
    group.binding_id = None
    group.save(update_fields=["binding_id"])
    moment = timezone.now()
    for member in group.members.filter(left_at__isnull=True, wire_key__startswith="b:"):
        _retire_member(cursor, group, member, moment)
        member.save(update_fields=["left_at"])
    _record_change(cursor, group.conversation, Change.Kind.MEMBERS_CHANGED)


def disconnect_from_upstream(order, binding):
    if not settings.TT_CHAT_ORDER_GROUPS_ENABLED:
        return
    if core_requested():
        from . import peer_chat

        peer_chat.disconnect_from_upstream(order, binding)
        return
    cursor = _lock_cursor()
    group = OrderGroup.objects.select_related("conversation").filter(
        order=order, binding_role=UPSTREAM, binding_id=binding.pk,
    ).first()
    if group is not None:
        _disconnect_group(cursor, group)


def _mark_designated_follower(member):
    if member.designated_follower:
        return False
    member.follower = True
    member.designated_follower = True
    member.left_at = None
    member.save(update_fields=["follower", "designated_follower", "left_at"])
    return True


def _upstream_service_contact(order, binding_user_id):
    contact = order.sales_user
    if contact is not None and contact.is_active and _can_join_group(contact.pk):
        return contact
    if order.sales_user_id != binding_user_id:
        owner = get_user_model().objects.filter(pk=binding_user_id, is_active=True).first()
        if owner is not None and _can_join_group(owner.pk):
            return owner
    return None


def assignment_changed(order, worker, *, follower=None, former_worker=None, jidanpu_only=False):
    if not settings.TT_CHAT_ORDER_GROUPS_ENABLED:
        return
    if core_requested():
        if former_worker is not None and (worker is None or former_worker.pk != worker.pk):
            from . import peer_chat

            peer_chat.disconnect_from_downstream(order, former_worker)
        return
    binding_user_id = None
    if worker is not None:
        binding_user_id = JidanpuUpstreamOrderLink.objects.filter(order=order).order_by("pk").values_list(
            "binding__user_id", flat=True,
        ).first()
    if jidanpu_only and binding_user_id is None:
        return
    cursor = _lock_cursor()
    group = OrderGroup.objects.filter(order=order).select_related("conversation").first()
    if group is not None and group.binding_role == DOWNSTREAM and group.binding_id is not None:
        if former_worker is None or not PeerDownstreamBinding.objects.filter(
            pk=group.binding_id, account=former_worker,
        ).exists():
            return
        _enqueue_chat_event(group, {}, kind=DETACHED_EVENT)
        _disconnect_group(cursor, group)
    if group is not None and group.binding_role == UPSTREAM and group.binding_id is None:
        return
    worker_id = worker.pk if worker is not None and _can_join_group(worker.pk) else None
    service_contact = None
    if binding_user_id is not None and worker_id is not None and (group is None or group.binding_id is None):
        service_contact = _upstream_service_contact(order, binding_user_id)
    created = group is None
    if created:
        if binding_user_id is None or worker_id is None:
            return
        group = _create_local_group(order)
    changed = False
    if service_contact is not None:
        member = _local_member(group, service_contact)
        if not member.publisher:
            changed |= _mark_designated_follower(member)
    if follower is not None and can_follow_order_chat(order, follower.pk):
        changed |= _mark_designated_follower(_local_member(group, follower))
    changed |= _retire_workers(cursor, group, worker_id)
    if worker_id is not None:
        member = _local_member(group, worker)
        if not member.worker:
            member.worker = True
            member.left_at = None
            member.save(update_fields=["worker", "left_at"])
            changed = True
    if created:
        _record_change(cursor, group.conversation, Change.Kind.CONVERSATION_CREATED)
    elif changed:
        _record_change(cursor, group.conversation, Change.Kind.MEMBERS_CHANGED)
        if group.binding_role == UPSTREAM:
            _enqueue_roster(group)


def _enqueue_chat_event(group, payload, *, kind):
    if group.binding_id is None:
        return
    PeerSyncEvent.objects.create(
        binding_role=group.binding_role, binding_id=str(group.binding_id), direction="out",
        kind=kind, target_type="chat", target_ref=str(group.wire_id), payload=payload,
        idempotency_key=uuid.uuid4().hex,
    )
    notify_peer_delivery()


def can_deliver(event, binding):
    if not settings.TT_CHAT_ORDER_GROUPS_ENABLED:
        return False
    if event.kind == DETACHED_EVENT:
        return event.binding_role == DOWNSTREAM
    return OrderGroup.objects.filter(
        wire_id=event.target_ref, binding_role=event.binding_role, binding_id=binding.pk,
        order__isnull=False,
    ).exists()


def materialize_outbound_payload(event, binding):
    payload = event.payload
    if event.kind == ALBUM_EVENT:
        parts = [_materialize_message(event, binding, part) for part in payload["messages"]]
        return None if any(part is None for part in parts) else {**payload, "messages": parts}
    if event.kind == MESSAGE_EVENT:
        return _materialize_message(event, binding, payload)
    return payload


def _materialize_message(event, binding, payload):
    if not (payload.get("image_pending") is True or "image" in payload):
        return payload

    # Queue image references so a revoke can clear the bytes before a retry.
    current = Message.objects.filter(
        wire_id=payload["message_id"],
        conversation__order_group__wire_id=event.target_ref,
        conversation__order_group__binding_role=event.binding_role,
        conversation__order_group__binding_id=binding.pk,
        conversation__order_group__order__isnull=False,
    ).values_list("revoked", "image_metadata", "image_data").first()
    if current is None:
        return None
    revoked, metadata, content = current
    payload = dict(payload)
    payload.pop("image_pending", None)
    payload.pop("image", None)
    if revoked:
        payload.update(revoked=True, text="")
        payload.pop("mention_keys", None)
    elif metadata is None or content is None:
        raise RuntimeError("待同步的订单群图片缺失")
    else:
        payload["image"] = encode_image(metadata, content)
    return payload


def _roster(group, *, source=False):
    return {
        "version": group.source_roster_version if source else group.roster_version,
        "members": [
            {
                "key": member.wire_key, "name": member.name,
                "studio_name": member.studio_name, "order_role": member.order_role,
                "left_at": member.left_at.isoformat() if member.left_at is not None else None,
            }
            for member in group.members.filter(wire_key__startswith="a:" if source else "b:").order_by("pk")
        ],
    }


def _enqueue_roster(group):
    group.roster_version += 1
    group.save(update_fields=["roster_version"])
    _enqueue_chat_event(group, _roster(group), kind=ROSTER_EVENT)


def _enqueue_source_roster(group):
    group.source_roster_version += 1
    group.save(update_fields=["source_roster_version"])
    _enqueue_chat_event(group, _roster(group, source=True), kind=ROSTER_EVENT)


def enqueue_message(group, message, sender, *, history=False):
    if not _can_enqueue_messages(group):
        return
    payload = _message_wire_payload(group, message, sender)
    if history:
        payload["history"] = True
    roster = _outbound_roster(group)
    if roster is not None:
        payload["roster"] = roster
    _enqueue_chat_event(group, payload, kind=MESSAGE_EVENT)


def enqueue_album(group, album_id, *, history=False):
    if not _can_enqueue_messages(group):
        return
    messages = (group.conversation.messages.filter(album_id=album_id).select_related("group_sender", "reply_to")
                .defer("image_data", "reply_to__image_data").order_by("seq"))
    payload = {
        "album_id": str(album_id),
        "messages": [_message_wire_payload(group, part, part.group_sender) for part in messages],
    }
    if history:
        payload["history"] = True
    roster = _outbound_roster(group)
    if roster is not None:
        payload["roster"] = roster
    _enqueue_chat_event(group, payload, kind=ALBUM_EVENT)


def _can_enqueue_messages(group):
    return group.binding_id is not None and (
        group.binding_role == UPSTREAM or (group.binding_role == DOWNSTREAM and group.roster_version > 0)
    )


def _outbound_roster(group):
    if group.binding_role == UPSTREAM:
        return _roster(group)
    if group.source_roster_version:
        return _roster(group, source=True)
    return None


def _message_wire_payload(group, message, sender):
    payload = {
        "message_id": str(message.wire_id), "sender": sender.wire_key, "sender_name": sender.name,
        "sender_studio_name": sender.studio_name, "sender_order_role": sender.order_role,
        "sender_left_at": sender.left_at.isoformat() if sender.left_at is not None else None,
        "text": "" if message.revoked else message.text, "created_at": message.created_at.isoformat(),
        "revoked": message.revoked, "reply_to": str(message.reply_to.wire_id) if message.reply_to_id else None,
    }
    if not message.revoked and message.image_metadata is not None:
        payload["image_pending"] = True
    if not message.revoked and message.mention_member_ids:
        payload["mention_keys"] = list(group.members.filter(
            pk__in=message.mention_member_ids,
        ).values_list("wire_key", flat=True))
    return payload


def _enrich_roster_identity(group, items):
    identities = {item["key"]: item for item in items if item.get("studio_name") or item.get("order_role")}
    candidates = group.members.filter(local_user__isnull=True, wire_key__in=identities).filter(
        Q(studio_name="") | Q(remote_order_role=""),
    )
    changed = []
    for member in candidates:
        data = identities[member.wire_key]
        studio = member.studio_name or data.get("studio_name", "")
        role = member.remote_order_role or data.get("order_role", "")
        if studio != member.studio_name or role != member.remote_order_role:
            member.studio_name = studio
            member.remote_order_role = role
            changed.append(member)
    if changed:
        group.members.bulk_update(changed, ["studio_name", "remote_order_role"])
    return bool(changed)


def _apply_roster(cursor, group, data):
    source = group.binding_role == UPSTREAM
    field = "source_roster_version" if source else "roster_version"
    current = getattr(group, field)
    if data["version"] < current:
        return
    if data["version"] == current:
        # Mixed-version peers can send the same roster with newly available identity labels.
        if _enrich_roster_identity(group, data["members"]):
            _record_change(cursor, group.conversation, Change.Kind.MEMBERS_CHANGED)
        return
    new_transfer = not source and current == 0
    for item in data["members"]:
        defaults = {"name": item["name"], "left_at": item["left_at"]}
        if item.get("studio_name"):
            defaults["studio_name"] = item["studio_name"]
        if "order_role" in item:
            defaults["remote_order_role"] = item["order_role"]
        group.members.update_or_create(
            wire_key=item["key"], local_user__isnull=True,
            defaults=defaults,
        )
    setattr(group, field, data["version"])
    group.save(update_fields=[field])
    _record_change(cursor, group.conversation, Change.Kind.MEMBERS_CHANGED)
    if new_transfer:
        if group.members.filter(wire_key__startswith="a:").exists():
            _enqueue_source_roster(group)
        history = (
            group.conversation.messages.select_related("group_sender", "reply_to")
            .defer("image_data", "reply_to__image_data").order_by("seq")
        )
        sent_albums = set()
        for message in history:
            if message.album_id is None:
                enqueue_message(group, message, message.group_sender, history=True)
            elif message.album_id not in sent_albums:
                enqueue_album(group, message.album_id, history=True)
                sent_albums.add(message.album_id)
        if group.pin_revision > 0:
            _enqueue_pin(group)


def _apply_message(cursor, group, data, *, notice_wire_id):
    message = group.conversation.messages.defer("image_data").filter(wire_id=data["message_id"]).first()
    author = group.members.filter(wire_key=data["sender"], local_user__isnull=True).first()
    if author is None and group.binding_role == UPSTREAM and data["sender"].startswith("a:"):
        author = group.members.create(
            wire_key=data["sender"], name=data["sender_name"], left_at=data["sender_left_at"],
            studio_name=data.get("sender_studio_name", ""), remote_order_role=data.get("sender_order_role", ""),
        )
        _record_change(cursor, group.conversation, Change.Kind.MEMBERS_CHANGED)
    if author is None:
        return "群内找不到此消息的发件人"
    identity_fields = []
    if not author.studio_name and data.get("sender_studio_name"):
        author.studio_name = data["sender_studio_name"]
        identity_fields.append("studio_name")
    if group.binding_role == UPSTREAM and not author.order_role and data.get("sender_order_role"):
        author.remote_order_role = data["sender_order_role"]
        identity_fields.append("remote_order_role")
    if identity_fields:
        author.save(update_fields=identity_fields)
        _record_change(cursor, group.conversation, Change.Kind.MEMBERS_CHANGED)
    if message is not None:
        if message.group_sender_id != author.pk:
            return "消息发件人与历史记录不一致"
        if message.album_id != data.get("album_id"):
            return "相册标识与历史记录不一致"
        if data["revoked"] and not message.revoked:
            message.revoke_content()
            _record_change(cursor, group.conversation, Change.Kind.MESSAGE_REVOKED, message)
            clear_revoked_pin(cursor, group, message)
        return None
    reply_id = data["reply_to"]
    reply = (group.conversation.messages.only("group_sender_id", "revoked").filter(wire_id=reply_id).first()
             if reply_id is not None else None)
    if reply_id is not None and reply is None:
        return "引用的原消息尚未同步"
    conversation = group.conversation
    conversation.latest_seq += 1
    conversation.save(update_fields=["latest_seq"])
    mentions = [] if data["revoked"] else list(group.members.filter(
        wire_key__in=data.get("mention_keys", ()),
    ).exclude(pk=author.pk).values_list("pk", flat=True))
    message = conversation.messages.create(
        wire_id=data["message_id"], group_sender=author, seq=conversation.latest_seq,
        client_id=str(data["message_id"]), text="" if data["revoked"] else data["text"],
        revoked=data["revoked"], reply_to=reply,
        album_id=data.get("album_id"),
        mention_member_ids=mentions,
        **image_fields(data.get("image") if not data["revoked"] else None),
    )
    Message.objects.filter(pk=message.pk).update(created_at=data["created_at"])
    _record_change(
        cursor, conversation, Change.Kind.MESSAGE_CREATED, message, notify_message=notice_wire_id is not None,
    )
    if message.revoked:
        clear_revoked_pin(cursor, group, message)
    else:
        if group.pinned_message_wire_id == message.wire_id:
            _record_change(cursor, conversation, Change.Kind.PIN_CHANGED)
        if notice_wire_id is not None:
            from .wxpusher import queue_group_notice

            queue_group_notice(group, message, reference_wire_id=notice_wire_id)
    return None


def _receive_detachment(identity, binding, binding_role):
    if binding_role != UPSTREAM:
        return "这条绑定无权关闭订单群"
    cursor = _lock_cursor()
    group = OrderGroup.objects.select_related("conversation").filter(
        wire_id=identity, binding_role=UPSTREAM, order__peer_links__upstream_binding=binding,
    ).first()
    if group is None or group.binding_id not in (None, binding.pk):
        return "这条绑定没有该订单群"
    if group.binding_id is not None:
        _disconnect_group(cursor, group)
    return None


def _receive_messages(cursor, group, parts, album_id, *, history):
    part_ids = [part["message_id"] for part in parts]
    if album_id is not None:
        known_parts = list(Message.objects.filter(album_id=album_id).order_by("seq").values_list("wire_id", flat=True))
        if known_parts and known_parts != part_ids:
            return "相册标识已用于不同的消息"
    if Message.objects.filter(wire_id__in=part_ids).exclude(
        conversation=group.conversation,
    ).exists():
        return "消息标识已在其他会话使用"
    notify_index = None if history else next((i for i, part in enumerate(parts) if not part["revoked"]), None)
    for index, part in enumerate(parts):
        data = {**part, "album_id": album_id} if album_id is not None else part
        notice_wire_id = part_ids[0] if index == notify_index else None
        error = _apply_message(cursor, group, data, notice_wire_id=notice_wire_id)
        if error is not None:
            return error
    return None


def validate_peer_event(kind, payload, binding_role):
    if kind == DETACHED_EVENT:
        return {}, None
    schema = {ROSTER_EVENT: PeerRoster, MESSAGE_EVENT: PeerMessage, ALBUM_EVENT: PeerAlbum, PIN_EVENT: PeerPin}[kind]
    parsed = schema(data=payload)
    if not parsed.is_valid():
        return None, "订单群事件格式有误"
    data = parsed.validated_data
    roster = data if kind == ROSTER_EVENT else data.get("roster")
    parts = data["messages"] if kind == ALBUM_EVENT else [data] if kind == MESSAGE_EVENT else []
    if roster is not None:
        prefix = "b:" if binding_role == DOWNSTREAM else "a:"
        if any(not member["key"].startswith(prefix) for member in roster["members"]):
            return None, "这条绑定无权同步订单群成员"
    if parts and (parts[0]["sender"].startswith("b:") != (binding_role == DOWNSTREAM)):
        return None, "这条绑定无权使用该发件人"
    return data, None


@transaction.atomic
def receive(kind, payload, *, binding, binding_role, target_ref, event_key=None):
    if not settings.TT_CHAT_ORDER_GROUPS_ENABLED:
        return "订单群聊未启用"
    if core_requested():
        from . import peer_chat

        return peer_chat.receive(
            kind, payload, binding=binding, binding_role=binding_role,
            target_ref=target_ref, event_key=event_key,
        )
    try:
        identity = uuid.UUID(target_ref)
    except ValueError:
        return "订单群标识无效"
    if kind == DETACHED_EVENT:
        return _receive_detachment(identity, binding, binding_role)
    data, error = validate_peer_event(kind, payload, binding_role)
    if error is not None:
        return error
    roster = data if kind == ROSTER_EVENT else data.get("roster")
    parts = data["messages"] if kind == ALBUM_EVENT else [data] if kind == MESSAGE_EVENT else []
    cursor = _lock_cursor()
    group = OrderGroup.objects.select_related("conversation").filter(
        wire_id=identity, binding_role=binding_role, binding_id=binding.pk, order__isnull=False,
    ).first()
    if group is None:
        return "这条绑定没有该订单群"
    if roster is not None:
        _apply_roster(cursor, group, roster)
    if kind == ROSTER_EVENT:
        return None
    if kind == PIN_EVENT:
        return _receive_pin(cursor, group, data)
    error = _receive_messages(cursor, group, parts, data.get("album_id"), history=data["history"])
    if error is not None:
        transaction.set_rollback(True)
    return error
