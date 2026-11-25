import base64
import hashlib
import hmac
import json
import logging
import re
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, transaction
from rest_framework.exceptions import APIException

from apps.business.models import PeerDownstreamBinding
from apps.core.plane import on_public_plane

logger = logging.getLogger(__name__)


class RealtimeUnavailable(APIException):
    status_code = 503
    default_detail = "实时通讯服务未启用"


def realtime_secret():
    if not settings.TT_CHAT_REALTIME_TENANT_KEYS:
        return settings.TT_CHAT_REALTIME_SECRET
    from .matrix_bridge import tenant_config

    try:
        tenant = tenant_config()
    except ImproperlyConfigured:
        return ""
    secret = tenant.get("realtime_secret") if tenant else None
    if not isinstance(secret, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{32,128}", secret):
        return ""
    return secret


def internal_chat_available():
    return not on_public_plane() and bool(
        settings.TT_CHAT_ORDER_GROUPS_ENABLED and settings.TT_CHAT_REALTIME_NOTIFY_URL
        and realtime_secret()
    )


def _base64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def _signed_ticket(user_id, claims):
    secret = realtime_secret()
    if not secret:
        raise RealtimeUnavailable
    payload = json.dumps(
        {"tenant": connection.schema_name, "user": user_id, "exp": int(time.time()) + 300, **claims},
        separators=(",", ":"),
    ).encode()
    part = _base64url(payload)
    signature = _base64url(hmac.digest(secret.encode(), part, hashlib.sha256))
    return b".".join((part, signature)).decode("ascii")


def issue_ticket(user_id):
    return _signed_ticket(user_id, {})


def issue_presence_ticket(user_id, conversation_id, peer_id):
    return _signed_ticket(user_id, {"purpose": "presence", "peer": peer_id, "conversation": conversation_id})


def _notify(tenant, users, url, secret, incoming=None, *, visitor_incoming=None):
    payload = {"tenant": tenant, "users": users}
    if incoming is not None:
        payload["incoming"] = incoming
    if visitor_incoming is not None:
        payload["visitor_incoming"] = visitor_incoming
    request = Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {secret}"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=0.75):
            pass
    except (URLError, TimeoutError):
        logger.warning("内部通讯实时提醒发送失败")


def notify_viewers_on_commit(viewer_ids, *, visitor_incoming=None):
    users = sorted(set(viewer_ids) - {None})
    if not users:
        return
    url, secret = settings.TT_CHAT_REALTIME_NOTIFY_URL, realtime_secret()
    if url and secret:
        tenant = connection.schema_name
        details = {"visitor_incoming": visitor_incoming} if visitor_incoming is not None else {}
        transaction.on_commit(lambda: _notify(tenant, users, url, secret, **details), robust=True)


def notify_on_commit(conversation, *, viewer_id=None, incoming_message=None):
    url = settings.TT_CHAT_REALTIME_NOTIFY_URL
    secret = realtime_secret()
    if not (url and secret):
        return
    tenant = connection.schema_name
    if viewer_id is not None:
        users = [viewer_id]
    elif conversation.system_recipient_id is not None:
        users = [conversation.system_recipient_id]
    elif conversation.first_user_id is not None:
        users = [conversation.first_user_id, conversation.second_user_id]
    else:
        users = list(conversation.order_group.members.filter(
            left_at__isnull=True, local_user__isnull=False,
        ).exclude(
            manual_joined=False, local_user__peer_downstream__state=PeerDownstreamBinding.State.ACTIVE,
        ).values_list("local_user_id", flat=True))
    if not users:
        return
    incoming = None
    if incoming_message is not None:
        author = (
            incoming_message.group_sender.local_user_id
            if incoming_message.group_sender_id is not None else incoming_message.sender_id
        )
        incoming = {"id": incoming_message.pk, "conversation_id": conversation.pk, "author": author}
    transaction.on_commit(lambda: _notify(tenant, users, url, secret, incoming), robust=True)
