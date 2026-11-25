import ipaddress
import json
from pathlib import Path
from time import monotonic, sleep
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError

from apps.accounts.display import user_display_name

from .images import encode_image
from .models import ChangeCursor


class ChatServiceUnavailable(APIException):
    status_code = 503
    default_detail = "内部沟通暂时不可用"


def tenant_config():
    filename = settings.TT_CHAT_SERVICE_CONFIG_FILE
    if not filename:
        return None
    try:
        path = Path(filename)
        if path.stat().st_mode & 0o077:
            raise ValueError("configuration permissions must be 0600")
        config = json.loads(path.read_text())
        tenants = config["tenants"]
        if not isinstance(tenants, dict):
            raise ValueError("invalid tenants")
        key_ids = [row["key_id"] for row in tenants.values()]
        if len(key_ids) != len(set(key_ids)):
            raise ValueError("duplicate signing key")
        tenant = tenants.get(connection.schema_name)
        if tenant is None or connection.schema_name == "public":
            return None
        if not all(isinstance(tenant.get(key), str) and tenant[key] for key in ("key_id", "secret")):
            raise ValueError("invalid signing key")
        return {"service_url": config["service_url"], **tenant}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ImproperlyConfigured("聊天服务配置无效") from exc


def latest_cursor():
    return ChangeCursor.objects.filter(pk=1).values_list("latest", flat=True).first() or 0


def conversation_payload(conversation):
    from .order_groups import visible_pinned_message_id

    direct = conversation.first_user_id is not None
    group = None if direct else conversation.order_group
    if direct:
        members = [{
            "id": user.pk, "local_user_id": user.pk, "name": user_display_name(user),
            "left_at": None, "studio_name": "", "order_role": "", "read_seq": read_seq,
        } for user, read_seq in ((conversation.first_user, conversation.first_read_seq),
                                  (conversation.second_user, conversation.second_read_seq))]
    else:
        members = [{
            "id": member.pk, "local_user_id": member.local_user_id, "name": member.name,
            "left_at": member.left_at.isoformat() if member.left_at else None,
            "studio_name": member.studio_name, "order_role": member.order_role, "read_seq": member.read_seq,
        } for member in group.members.all()]
    return {
        "id": conversation.pk, "kind": "direct" if direct else "order",
        "first_user_id": conversation.first_user_id, "second_user_id": conversation.second_user_id,
        "first_read_seq": conversation.first_read_seq, "second_read_seq": conversation.second_read_seq,
        "order_no": "" if direct else group.order_no,
        "title": "" if direct else f"订单-{group.order_no}",
        "pinned_message_id": None if direct else visible_pinned_message_id(group),
        "members": members,
        "preferences": [{"user_id": row.user_id, "pinned": row.pinned, "remark": row.remark,
                         "marked_unread": row.marked_unread}
                        for row in conversation.conversationpreference_set.all()],
    }


def message_payload(message):
    from .service import message_payload as web_payload

    payload = web_payload(message)
    payload["sender_local_user_id"] = (
        message.group_sender.local_user_id if message.group_sender_id is not None else message.sender_id
    )
    if not message.revoked and message.image_data is not None and message.image_metadata is not None:
        payload["image_data"] = encode_image(message.image_metadata, message.image_data)
    return payload


def _service_url(url):
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("invalid service URL")
    if parsed.scheme == "https" and parsed.hostname:
        return url.rstrip("/")
    try:
        host = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        host = None
    if parsed.scheme == "http" and (parsed.hostname == "localhost" or host is not None and
                                   (host.is_loopback or host.is_private)):
        return url.rstrip("/")
    raise ValueError("service URL requires HTTPS or an internal address")


def remote_read(operation, conversation_id, viewer_id, *, readonly=False, **query):
    backend = settings.TT_CHAT_READ_BACKEND
    if backend == "legacy":
        return None
    if backend != "matrix":
        raise ChatServiceUnavailable()
    try:
        config = tenant_config()
    except ImproperlyConfigured as exc:
        raise ChatServiceUnavailable() from exc
    if config is None:
        raise ChatServiceUnavailable()
    tenant_backend = config.get("read_backend", backend)
    if tenant_backend == "legacy":
        return None
    if tenant_backend != "matrix" or viewer_id is None:
        raise ChatServiceUnavailable()
    from chat_service.protocol import sign_request

    deadline = monotonic() + 5
    body = json.dumps({
        "operation": operation, "conversation_id": conversation_id, "viewer_id": viewer_id,
        "readonly": readonly, "minimum_cursor": latest_cursor(),
        "query": {key: value for key, value in query.items() if value is not None},
    }, separators=(",", ":")).encode()
    try:
        url = _service_url(config["service_url"]) + "/v1/read"
        headers = sign_request(config["secret"], config["key_id"], "POST", "/v1/read", body)
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise ChatServiceUnavailable()
            response = httpx.post(
                url, content=body, headers={**headers, "Content-Type": "application/json"}, timeout=remaining,
            )
            envelope = response.json()
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise ChatServiceUnavailable()
            if (response.status_code == 503 and envelope.get("code") == 503 and
                    envelope.get("msg") == "Matrix mirror has not caught up"):
                sleep(min(0.2, remaining))
                continue
            break
        if response.status_code == envelope.get("code"):
            if response.status_code == 404:
                raise NotFound()
            if response.status_code == 403:
                raise PermissionDenied("当前账号无权查看这段聊天")
            if response.status_code == 400:
                raise ValidationError("聊天查询参数有误")
        response.raise_for_status()
        if envelope.get("code") != 0 or not isinstance(envelope.get("data"), dict):
            raise ValueError("invalid service response")
        data = envelope["data"]
        required = {"history": ("messages", "has_more", "has_newer"),
                    "search": ("messages", "count", "has_more"), "image": ("mime", "data")}[operation]
        if any(key not in data for key in required):
            raise ValueError("incomplete service response")
        return data
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
        raise ChatServiceUnavailable() from exc
