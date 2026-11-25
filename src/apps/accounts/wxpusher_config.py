"""Resolve WxPusher settings within the active tenant schema."""

import base64
import logging
import re
from dataclasses import dataclass, field

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.db import connection
from django.urls import reverse
from django.utils.crypto import salted_hmac
from django.views.decorators.debug import sensitive_variables
from django_tenants.utils import get_public_schema_name

from apps.accounts.models import WxPusherTenantSettings
from apps.accounts.wxpusher_notice_templates import (
    CHAT_KINDS, HALL_NEW_ORDER, NOTICE_TEMPLATES, ORDER_EVENTS, SETTLEMENT_KINDS, order_event_key,
)
from apps.core.wxpusher import is_wxpusher_qr, public_follow_url

logger = logging.getLogger(__name__)
APP_ID_PATTERN = r"[1-9][0-9]{0,18}"

_HOST = re.compile(
    r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
    re.ASCII | re.IGNORECASE,
)
_SWITCHES = ("push_enabled", "login_enabled", "binding_prompt_enabled", "order_enabled", "settlement_enabled")


class WxPusherConfigError(ValueError):
    pass


@dataclass(frozen=True)
class WxPusherConfig:
    source: str = "none"
    app_id: str = ""
    app_token: str = field(default="", repr=False)
    login_signing_key: str = field(default="", repr=False)
    subscribe_url: str = ""
    qr_url: str = ""
    push_enabled: bool = False
    login_enabled: bool = False
    binding_prompt_enabled: bool = False
    order_enabled: bool = True
    settlement_enabled: bool = True
    disabled_order_events: tuple[str, ...] = ()

    @property
    def configured(self):
        return bool(self.app_id and self.app_token)

    def wants_event(self, kind):
        if not (self.configured and self.push_enabled) or kind not in NOTICE_TEMPLATES:
            return False
        if kind in SETTLEMENT_KINDS:
            return self.settlement_enabled
        if kind == HALL_NEW_ORDER or kind in CHAT_KINDS:
            return True
        return self.order_enabled and order_event_key(kind) not in self.disabled_order_events

    def public_payload(self, tenant):
        origin = tenant_origin(tenant)
        return {
            "configured": self.configured,
            "source": self.source,
            "app_id": self.app_id,
            "subscribe_url": self.subscribe_url,
            "system_url": origin + reverse("auth-wxpusher-login") if origin else "",
            "has_app_token": bool(self.app_token),
            "has_login_signing_key": bool(self.login_signing_key),
            "push_enabled": self.push_enabled,
            "login_enabled": self.login_enabled,
            "binding_prompt_enabled": self.binding_prompt_enabled,
            "order_enabled": self.order_enabled,
            "settlement_enabled": self.settlement_enabled,
            "order_events": {kind: kind not in self.disabled_order_events for kind in ORDER_EVENTS},
        }


def tenant_origin(tenant):
    primary = tenant.get_primary_domain()
    if primary and _HOST.fullmatch(primary.domain):
        return f"https://{primary.domain}"
    return ""


@sensitive_variables()
def _environment_config(schema):
    if not schema or schema == get_public_schema_name():
        return WxPusherConfig()
    if schema != settings.WXPUSHER_SUBSCRIBE_TENANT_SCHEMA.strip():
        return WxPusherConfig()
    qr_url = settings.WXPUSHER_SUBSCRIBE_QR_URL.strip()
    return WxPusherConfig(
        source="environment",
        app_id=settings.WXPUSHER_APP_ID.strip(),
        app_token=settings.WXPUSHER_APP_TOKEN.strip(),
        login_signing_key=settings.WXPUSHER_LOGIN_SIGNING_KEY,
        subscribe_url=public_follow_url(settings.WXPUSHER_SUBSCRIBE_URL.strip()) or "",
        qr_url=qr_url if is_wxpusher_qr(qr_url) else "",
        push_enabled=True,
        login_enabled=bool(settings.WXPUSHER_LOGIN_SIGNING_KEY.strip()),
    )


@sensitive_variables()
def _cipher():
    keys = [settings.SECRET_KEY, *settings.SECRET_KEY_FALLBACKS]
    return MultiFernet([
        Fernet(base64.urlsafe_b64encode(
            salted_hmac("apps.accounts.WxPusherTenantSettings", connection.schema_name, secret=key, algorithm="sha256").digest(),
        ))
        for key in keys
    ])


@sensitive_variables()
def _encrypt(value):
    return _cipher().encrypt(value.encode()).decode() if value else ""


def _decrypt(cipher, value):
    if not value:
        return ""
    try:
        return cipher.decrypt(value.encode()).decode()
    except (InvalidToken, UnicodeError):
        logger.warning("WxPusher settings secret could not be decrypted in tenant %s", connection.schema_name)
        return ""


def _read_row(row):
    cipher = _cipher()
    return WxPusherConfig(
        source="tenant", app_id=row.app_id,
        app_token=_decrypt(cipher, row.app_token_encrypted),
        login_signing_key=_decrypt(cipher, row.signing_key_encrypted),
        subscribe_url=public_follow_url(row.subscribe_url, app_id=row.app_id) or "",
        qr_url=row.qr_url if is_wxpusher_qr(row.qr_url) else "",
        disabled_order_events=tuple(row.disabled_order_events),
        **{name: getattr(row, name) for name in _SWITCHES},
    )


def load_wxpusher_config():
    schema = connection.schema_name
    if not schema or schema == get_public_schema_name():
        return WxPusherConfig()
    row = WxPusherTenantSettings.objects.filter(pk=1).first()
    return _read_row(row) if row else _environment_config(schema)


def legacy_rollback_would_restore_old_settings():
    legacy = _environment_config(connection.schema_name)
    if not legacy.configured:
        return False
    current = load_wxpusher_config()
    if current.source != "tenant":
        return False
    return (
        (current.app_id, current.app_token) != (legacy.app_id, legacy.app_token)
        or not current.push_enabled or not current.order_enabled or not current.settlement_enabled
        or bool(current.disabled_order_events)
        or (legacy.login_enabled and (
            not current.login_enabled or current.login_signing_key != legacy.login_signing_key
        ))
    )


@sensitive_variables()
def update_wxpusher_config(changes, *, expected_app_id=None):
    """Apply a validated partial update within the caller's transaction."""
    schema = connection.schema_name
    if not schema or schema == get_public_schema_name():
        raise WxPusherConfigError("此处无法配置工作室消息推送。")
    if not changes:
        return load_wxpusher_config()

    previous = _environment_config(schema)
    row, _ = WxPusherTenantSettings.objects.select_for_update().get_or_create(pk=1, defaults={
        "app_id": previous.app_id,
        "app_token_encrypted": lambda: _encrypt(previous.app_token),
        "signing_key_encrypted": lambda: _encrypt(previous.login_signing_key),
        "subscribe_url": previous.subscribe_url,
        "qr_url": previous.qr_url,
        **{name: getattr(previous, name) for name in _SWITCHES},
    })
    if expected_app_id is not None and expected_app_id != row.app_id:
        raise WxPusherConfigError("WxPusher 应用已更新，请刷新页面后再试。")
    current = _read_row(row)
    app_id = changes.get("app_id", row.app_id)
    app_changed = app_id != row.app_id
    token = changes.get("app_token", "")
    key = changes.get("login_signing_key", "")
    if app_changed and not token:
        raise WxPusherConfigError("更换 WxPusher 应用时，请填写新应用的 appToken。")
    if token and not app_id:
        raise WxPusherConfigError("请填写与 appToken 对应的应用 ID。")
    if key and not app_id:
        raise WxPusherConfigError("请填写要对接的应用 ID。")

    row.app_id = app_id
    if token:
        row.app_token_encrypted = _encrypt(token)
    if key or app_changed:
        row.signing_key_encrypted = _encrypt(key)
    if app_changed:
        row.qr_url = ""
        row.subscribe_url = ""
        row.login_enabled = False
    if "subscribe_url" in changes:
        new_url = changes["subscribe_url"]
        verified = public_follow_url(new_url, app_id=app_id) if app_id else None
        if new_url and not verified:
            raise WxPusherConfigError("请填写当前应用的 WxPusher 关注链接。")
        row.subscribe_url = verified or ""

    for name in _SWITCHES:
        if name in changes:
            setattr(row, name, changes[name])
    has_app_token = bool(token or current.app_token)
    has_signing_key = bool(key or (current.login_signing_key and not app_changed))
    if "push_enabled" in changes and row.push_enabled and not (app_id and has_app_token):
        raise WxPusherConfigError("请先填写应用 ID 和 appToken，再开启消息推送。")
    if "login_enabled" in changes and row.login_enabled and not (app_id and has_app_token and has_signing_key):
        raise WxPusherConfigError("请先填写应用 ID、appToken 和系统对接验证密钥，再开启一键登录。")

    disabled = set(row.disabled_order_events)
    for kind, enabled in changes.get("order_events", {}).items():
        if enabled:
            disabled.discard(kind)
        else:
            disabled.add(kind)
    row.disabled_order_events = sorted(disabled)
    row.save()
    return _read_row(row)
