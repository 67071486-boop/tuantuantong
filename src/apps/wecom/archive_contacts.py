"""Resolve external-sender badges through the authorized Customer Contact API."""

import json
from hashlib import sha256
from time import monotonic

from django.core.cache import caches
from django.db import connection
from django_tenants.utils import get_public_schema_name
from requests import RequestException

from apps.wecom.archive import ArchiveError, _ArchiveSession
from apps.wecom.group_sync import _contacts_client
from apps.wecom.sdk import WeChatClientException, retry_rejected_token

_LOOKUP_BUDGET = 2.5
_BADGE_TTL = 600
_UNKNOWN_TTL = 30


def _contact_badge(body, sender_id):
    if not isinstance(body, dict) or type(body.get("errcode")) is not int or body["errcode"] != 0:
        return {}
    contact = body.get("external_contact")
    if not isinstance(contact, dict) or contact.get("external_userid") != sender_id:
        return {}
    contact_type = contact.get("type")
    if type(contact_type) is not int:
        return {}
    if contact_type == 1:
        return {"external_kind": "wechat"}
    if contact_type == 2:
        badge = {"external_kind": "wecom"}
        company = contact.get("corp_name")
        if isinstance(company, str) and company.strip() and company.strip().isprintable():
            badge["corp_name"] = company.strip()[:64]
        return badge
    return {}


def external_sender_badges(config, sender_ids):
    if connection.schema_name == get_public_schema_name() or config.tenant_schema != connection.schema_name:
        return {}
    identity = (
        config.tenant_schema, config.corp_id, config.agent_id, config.identity_revision,
        config.contacts_secret or config.secret,
    )
    keys = {
        sender_id: "wecom:archive:badge:" + sha256(json.dumps((*identity, sender_id)).encode()).hexdigest()
        for sender_id in dict.fromkeys(sender_ids)
    }
    cache = caches["wecom_archive_badges"]
    cached = cache.get_many(keys.values())
    badges = {sender_id: cached[key] for sender_id, key in keys.items() if cached.get(key)}
    pending = [(sender_id, key) for sender_id, key in keys.items() if key not in cached]
    if not pending:
        return badges

    deadline = monotonic() + _LOOKUP_BUDGET
    client = _contacts_client(config)
    client._http.close()
    client._http = _ArchiveSession(deadline)
    client.auto_retry = False
    try:
        for sender_id, key in pending:
            if monotonic() >= deadline:
                break
            try:
                result = retry_rejected_token(client, lambda: client.external_contact.get(sender_id))
            except (ArchiveError, RequestException, WeChatClientException):
                cache.set(key, {}, _UNKNOWN_TTL)
                continue
            except RuntimeError:
                break
            badge = _contact_badge(result, sender_id)
            cache.set(key, badge, _BADGE_TTL if badge else _UNKNOWN_TTL)
            if badge:
                badges[sender_id] = badge
    finally:
        client._http.close()
    return badges
