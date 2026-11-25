import re

from django.conf import settings
from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.plane import on_public_plane

from .images import image_fields
from .matrix_bridge import ChatServiceUnavailable
from .models import Change, Conversation

SYSTEM_NAME = "系统通知"
SYSTEM_SENDER_ID = 2**31


def _require_chat_available():
    if on_public_plane() or not settings.TT_CHAT_ORDER_GROUPS_ENABLED or settings.TT_CHAT_MAINTENANCE:
        raise ChatServiceUnavailable()


def send_order_notification(recipient, *, order_no, text, client_id, image=None):
    _require_chat_available()
    if not isinstance(order_no, str) or not re.fullmatch(r"[0-9]{20}", order_no):
        raise ValidationError("系统通知的订单号无效")
    return _send_notification(recipient, order_no=order_no, text=text, client_id=client_id, image=image)


def send_system_notification(recipient, *, text, client_id):
    _require_chat_available()
    return _send_notification(recipient, order_no="", text=text, client_id=client_id)


@transaction.atomic
def _send_notification(recipient, *, order_no, text, client_id, image=None):
    from . import service

    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise ValidationError("系统通知内容无效")
    if not isinstance(client_id, str) or not client_id.strip() or len(client_id) > 100:
        raise ValidationError("系统通知编号无效")
    if not service._active_contacts().filter(pk=recipient.pk).exists():
        raise PermissionDenied("系统通知接收人不可用")

    cursor = service._lock_legacy_cursor()
    conversation, created = Conversation.objects.get_or_create(system_recipient_id=recipient.pk)
    # The tenant cursor serializes lookup and insertion for notices without an order number.
    existing = conversation.messages.filter(client_id=client_id).first()
    if existing is not None:
        image_data = image["data"] if image is not None else None
        if existing.system_order_no != order_no or existing.text != text or existing.image_data != image_data:
            raise ValidationError("系统通知编号已用于其他消息")
        return service.message_payload(existing, system=True)

    if created:
        service._record_change(cursor, conversation, Change.Kind.CONVERSATION_CREATED)
    conversation.latest_seq += 1
    conversation.save(update_fields=["latest_seq"])
    message = conversation.messages.create(
        seq=conversation.latest_seq, client_id=client_id, text=text, system_order_no=order_no,
        **image_fields(image),
    )
    service._record_change(cursor, conversation, Change.Kind.MESSAGE_CREATED, message, notify_message=True)
    return service.message_payload(message, system=True)
