import logging

from django.conf import settings
from django.db import DatabaseError, transaction
from django.db.models import Exists, OuterRef, Q

from apps.accounts.models import WxPusherNotice
from apps.business.models import PeerDownstreamBinding, PeerUpstreamBinding

from .models import Conversation, GroupMember, Message, OrderGroup
from .order_groups import UPSTREAM, can_join_order_group

logger = logging.getLogger(__name__)


def _available_members(group_id):
    peer_accounts = PeerDownstreamBinding.objects.filter(
        account_id=OuterRef("local_user_id"), state=PeerDownstreamBinding.State.ACTIVE,
    )
    return GroupMember.objects.filter(
        group_id=group_id, local_user__is_active=True, left_at__isnull=True,
    ).filter(Q(manual_joined=True) | ~Exists(peer_accounts))


def queue_group_notice(group, message, *, reference_wire_id=None):
    if not settings.TT_CHAT_ORDER_GROUPS_ENABLED or message.revoked or not can_join_order_group(group):
        return

    from apps.accounts.wxpusher_notice_templates import GROUP_MENTION, GROUP_MESSAGE, GROUP_REPLY
    from apps.accounts.wxpusher_notifications import enqueue_chat_notices

    try:
        with transaction.atomic():
            mentions = set(message.mention_member_ids)
            reply = message.reply_to
            replied_to = reply.group_sender_id if reply and not reply.revoked else None
            matching = _available_members(group.pk).filter(read_seq__lt=message.seq).exclude(pk=message.group_sender_id)
            recipient_kinds = {}
            for member_id, user_id in matching.values_list("pk", "local_user_id"):
                if member_id in mentions:
                    recipient_kinds[user_id] = GROUP_MENTION
                elif member_id == replied_to:
                    recipient_kinds[user_id] = GROUP_REPLY
                else:
                    recipient_kinds[user_id] = GROUP_MESSAGE
            if recipient_kinds:
                enqueue_chat_notices(
                    recipient_kinds=recipient_kinds, reference=f"group-chat:{reference_wire_id or message.wire_id}",
                    order_no=group.order_no,
                    details={
                        "conversation_id": message.conversation_id,
                        "message_id": message.pk,
                        "sender": message.group_sender.name,
                    },
                )
    except DatabaseError as exc:
        logger.warning("订单群提醒登记失败 message=%s error=%s", message.pk, type(exc).__name__)


def queue_direct_notice(conversation, message, *, sender_name):
    if not settings.TT_CHAT_ORDER_GROUPS_ENABLED:
        return

    from apps.accounts.wxpusher_notice_templates import DIRECT_MESSAGE
    from apps.accounts.wxpusher_notifications import enqueue_chat_notices

    recipient = (
        conversation.second_user_id if message.sender_id == conversation.first_user_id else conversation.first_user_id
    )
    try:
        with transaction.atomic():
            enqueue_chat_notices(
                recipient_kinds={recipient: DIRECT_MESSAGE}, reference=f"direct-chat:{message.pk}", order_no="",
                details={"conversation_id": conversation.pk, "message_id": message.pk, "sender": sender_name},
            )
    except DatabaseError as exc:
        logger.warning("单聊提醒登记失败 message=%s error=%s", message.pk, type(exc).__name__)


def _unavailable_notices(notices):
    unavailable = (WxPusherNotice.SkipReason.GROUP_UNAVAILABLE, None)
    return dict.fromkeys((notice.pk for notice in notices), unavailable)


def recheck_group_notices(notices, conversation_id):
    if not settings.TT_CHAT_ORDER_GROUPS_ENABLED or conversation_id is None:
        return _unavailable_notices(notices)
    group = OrderGroup.objects.filter(conversation_id=conversation_id).first()
    if group is None or not can_join_order_group(group):
        return _unavailable_notices(notices)
    if group.binding_role == UPSTREAM and not PeerUpstreamBinding.objects.filter(
        pk=group.binding_id, enabled=True, deleted_at__isnull=True,
    ).exists():
        return _unavailable_notices(notices)
    member = _available_members(group.pk).filter(local_user_id=notices[0].user_id).values_list("pk", "read_seq").first()
    if member is None:
        return _unavailable_notices(notices)
    member_id, read_seq = member
    messages = Message.objects.filter(
        conversation_id=conversation_id, group_sender_id__isnull=False,
    ).exclude(group_sender_id=member_id)
    return _recheck_messages(notices, messages, read_seq, order_no=group.order_no)


def recheck_direct_notices(notices, conversation_id):
    if not settings.TT_CHAT_ORDER_GROUPS_ENABLED or conversation_id is None:
        return _unavailable_notices(notices)
    viewer = notices[0].user_id
    conversation = Conversation.objects.filter(
        Q(first_user_id=viewer) | Q(second_user_id=viewer), pk=conversation_id,
    ).first()
    if conversation is None:
        return _unavailable_notices(notices)
    if conversation.first_user_id == viewer:
        sender_id, read_seq = conversation.second_user_id, conversation.first_read_seq
    else:
        sender_id, read_seq = conversation.first_user_id, conversation.second_read_seq
    messages = Message.objects.filter(conversation_id=conversation_id, sender_id=sender_id, group_sender_id__isnull=True)
    return _recheck_messages(notices, messages, read_seq, order_no="")


def _recheck_messages(notices, messages, read_seq, *, order_no):
    reason = WxPusherNotice.SkipReason
    unavailable = (reason.GROUP_UNAVAILABLE, None)
    message_ids = [notice.details.get("message_id") for notice in notices]
    anchors = {
        row[0]: row[1:] for row in messages.filter(
            pk__in=[pk for pk in message_ids if type(pk) is int and 0 < pk < 2**63],
        ).values_list("pk", "seq", "revoked", "album_id")
    }
    albums = {album for seq, revoked, album in anchors.values() if album and (revoked or seq <= read_seq)}
    live_albums, unread_parts = set(), {}
    if albums:
        for album, part_id, seq in messages.filter(album_id__in=albums, revoked=False).order_by("seq").values_list(
            "album_id", "pk", "seq",
        ):
            live_albums.add(album)
            if seq > read_seq:
                unread_parts.setdefault(album, part_id)

    outcomes = {}
    for notice, message_id in zip(notices, message_ids, strict=True):
        message = anchors.get(message_id) if type(message_id) is int else None
        if message is None or notice.order_no != order_no:
            outcomes[notice.pk] = unavailable
            continue
        seq, revoked, album = message
        if not revoked and seq > read_seq:
            outcomes[notice.pk] = "", message_id
        elif album in unread_parts:
            outcomes[notice.pk] = "", unread_parts[album]
        elif (album is None and not revoked) or album in live_albums:
            outcomes[notice.pk] = reason.GROUP_ALREADY_READ, None
        else:
            outcomes[notice.pk] = unavailable
    return outcomes
