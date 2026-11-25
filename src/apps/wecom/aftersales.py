"""The tenant settings and WeCom keyword rule for after-sales notifications."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from time import monotonic
from uuid import uuid4

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.accounts.selectors import base_queryset
from apps.wecom import zone_client
from apps.wecom.archive import ArchiveError, archive_program
from apps.wecom.models import WecomAftersalesSettings, WecomAppConfig, WecomGroupChat
from apps.wecom.zone_events import _require_current_config

DEFAULT_KEYWORDS = (
    "退款", "退钱", "退单", "取消订单", "不想打了", "不打了", "别打了", "打不了了", "换人", "换打手",
    "换个打手", "换代练", "换个代练", "售后", "投诉", "赔偿",
)
MAX_KEYWORDS = 20
MAX_GROUPS = 2000
MAX_FIXED_RECIPIENTS = 100
_RULE_SCOPE = {
    "chat_type": {"type_list": [2]}, "session_type": {"session_type_list": [2]}, "msg_scope": 3,
}
_OPTIONAL_RULE_LIMITS = {
    "target_type": "type_list", "department": "id_list", "user": "id_list", "external_contact": "id_list",
    "exclude_mobile": "mobile_list", "exclude_bank_card": "bank_card_list", "exclude_email": "email_list",
    "exclude_keyword": "word_list", "voice_match_msgtype": "type_list",
}
_SYNC_RETRY = timedelta(minutes=5)
_RULE_PAGE_LIMIT = 100
_MAX_RULE_PAGES = 20
_SYNC_TIMEOUT = 6


@dataclass(frozen=True)
class NoticePolicy:
    rule_id: str
    group_ids: tuple[str, ...]
    started_at: datetime
    order_recipient: str
    fixed_recipient_ids: tuple[int, ...] = ()
    legacy: bool = False


def available(config):
    return bool(
        connection.schema_name == "kexing" and config and config.tenant_schema == connection.schema_name
        and config.is_ready() and zone_client.zone_enabled() and settings.TT_CHAT_ORDER_GROUPS_ENABLED
        and settings.TT_CHAT_CORE_BACKEND == "legacy"
    )


def eligible_groups(config):
    return WecomGroupChat.objects.filter(
        identity_revision=config.identity_revision, dismissed=False, pending_dismissal=False,
    ).exclude(pending=True, details={}, name="")


def active_users():
    return base_queryset().filter(is_active=True).select_related(None).prefetch_related(None)


def legacy_policy():
    if not settings.WECOM_REFUND_NOTICE_RULE_ID or not settings.WECOM_REFUND_NOTICE_CHAT_ID:
        return None
    try:
        started_at = parse_datetime(settings.WECOM_REFUND_NOTICE_START_AT)
    except ValueError:
        return None
    if started_at and timezone.is_aware(started_at):
        return NoticePolicy(
            settings.WECOM_REFUND_NOTICE_RULE_ID, (settings.WECOM_REFUND_NOTICE_CHAT_ID,),
            started_at, WecomAftersalesSettings.OrderRecipient.SALES, legacy=True,
        )
    return None


def _same_rule_identity(row, config, program):
    return row.rule_revision == config.identity_revision and row.rule_program == program


def policy_status(row, config):
    if not row.enabled:
        return WecomAftersalesSettings.Status.DISABLED
    if not _same_rule_identity(row, config, archive_program()):
        return WecomAftersalesSettings.Status.PENDING
    return row.status


def current_policy(config):
    if not available(config):
        return None
    row = WecomAftersalesSettings.objects.filter(config=config).first()
    if row is None:
        return legacy_policy()
    if policy_status(row, config) != WecomAftersalesSettings.Status.SYNCED or not (row.rule_id and row.started_at):
        return None
    return NoticePolicy(
        row.rule_id, tuple(row.group_ids), row.started_at, row.order_recipient, tuple(row.fixed_recipient_ids),
    )


def _reserve_rule(row, config, program):
    row.rule_name = f"售后纠纷-{uuid4().hex[:12]}"
    row.rule_id = ""
    row.rule_revision = config.identity_revision
    row.rule_program = program


def set_settings(row, config, values):
    """Prepare a freshly saved policy under the application row lock."""
    scope_changed = (
        row.enabled != values["enabled"] or set(row.keywords) != set(values["keywords"])
        or set(row.group_ids) != set(values["group_ids"])
    )
    for key, value in values.items():
        setattr(row, key, value)
    program = archive_program()
    identity_changed = not _same_rule_identity(row, config, program)
    if row.enabled and (identity_changed or not row.rule_name):
        _reserve_rule(row, config, program)
    if not row.enabled:
        row.status = WecomAftersalesSettings.Status.DISABLED
        row.started_at = row.next_sync_at = None
    elif scope_changed or identity_changed or row.status != WecomAftersalesSettings.Status.SYNCED:
        row.status = WecomAftersalesSettings.Status.PENDING
        row.started_at = row.next_sync_at = None
    row.save()


def _rule_payload(row, *, updating=False):
    scope = {**_RULE_SCOPE}
    # WeCom requires an explicit empty list to clear a chat filter on update.
    if row.group_ids or updating:
        scope["chat"] = {"id_list": row.group_ids}
    return {
        "name": row.rule_name,
        "keyword": {"word_list": row.keywords, "match_rule": 0},
        "applicable_range": scope,
    }


def _owned_rule_id(config, row, deadline):
    params = {"limit": _RULE_PAGE_LIMIT}
    found = set()
    seen_cursors = set()
    for _ in range(_MAX_RULE_PAGES):
        result = zone_client.call_zone_operation(config, "get_rule_list", params, deadline=deadline)
        rules, more = result.get("rule_list"), result.get("has_more")
        if not isinstance(rules, list) or type(more) not in (bool, int) or more not in (0, 1):
            raise ArchiveError("企微关键词规则列表异常")
        for item in rules:
            if not isinstance(item, dict):
                raise ArchiveError("企微关键词规则列表异常")
            if item.get("name") == row.rule_name:
                found.add(_rule_id(item))
        if not more:
            if len(found) > 1:
                raise ArchiveError("企微售后规则存在重复")
            return next(iter(found), "")
        cursor = result.get("next_cursor")
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise ArchiveError("企微关键词规则分页异常")
        seen_cursors.add(cursor)
        params["cursor"] = cursor
    raise ArchiveError("企微关键词规则未能读完")


def _rule_id(result):
    rule_id = result.get("rule_id")
    if not isinstance(rule_id, str) or not (0 < len(rule_id) <= 128) or not rule_id.isprintable():
        raise ArchiveError("企微关键词规则编号异常")
    return rule_id


def _rule_matches(detail, row):
    return (
        detail.get("name") == row.rule_name
        and _same_values(detail.get("keyword", {}), "word_list", row.keywords)
        and detail["keyword"].get("match_rule", 0) == 0
        and _same_scope(detail.get("applicable_range"), row.group_ids)
    )


def _same_values(obj, key, values):
    actual = obj.get(key) if isinstance(obj, dict) else None
    return (
        isinstance(actual, list) and len(actual) == len(values)
        and all(type(item) is type(values[0]) for item in actual) and set(actual) == set(values)
    )


def _same_scope(scope, group_ids):
    if not (
        isinstance(scope, dict) and type(scope.get("msg_scope")) is int and scope["msg_scope"] == 3
        and _same_values(scope.get("chat_type"), "type_list", [2])
        and _same_values(scope.get("session_type"), "session_type_list", [2])
        and all(scope.get(key, {}) in ({}, {field: []}) for key, field in _OPTIONAL_RULE_LIMITS.items())
    ):
        return False
    if group_ids:
        return _same_values(scope.get("chat"), "id_list", group_ids)
    return scope.get("chat", {}) in ({}, {"id_list": []})


def _sync_rule(config, row, deadline):
    rule_id = _owned_rule_id(config, row, deadline)
    if rule_id:
        detail = zone_client.call_zone_operation(config, "get_rule_detail", {"rule_id": rule_id}, deadline=deadline)
        if detail.get("name") != row.rule_name:
            raise ArchiveError("企微售后规则标识不一致")
        if _rule_matches(detail, row):
            return rule_id
        payload = {"rule_id": rule_id, **_rule_payload(row, updating=True)}
        zone_client.call_zone_operation(config, "update_rule", payload, deadline=deadline)
    else:
        result = zone_client.call_zone_operation(config, "create_rule", _rule_payload(row), deadline=deadline)
        rule_id = _rule_id(result)
    confirmed = zone_client.call_zone_operation(config, "get_rule_detail", {"rule_id": rule_id}, deadline=deadline)
    if not _rule_matches(confirmed, row):
        raise ArchiveError("企微未保存所选售后规则")
    return rule_id


@transaction.atomic
def sync_settings(config, now):
    if not available(config):
        return
    program = archive_program()
    try:
        _require_current_config(config, program)
    except ArchiveError:
        return
    row = WecomAftersalesSettings.objects.select_for_update().filter(config=config, enabled=True).first()
    if row is None or policy_status(row, config) == WecomAftersalesSettings.Status.SYNCED:
        return
    if not _same_rule_identity(row, config, program) or not row.rule_name:
        # Persist ownership before the vendor can create a rule.
        _reserve_rule(row, config, program)
        row.status = WecomAftersalesSettings.Status.PENDING
        row.started_at = row.next_sync_at = None
        row.save()
        return
    if row.next_sync_at is not None and row.next_sync_at > now:
        return
    if row.group_ids and eligible_groups(config).filter(chat_id__in=row.group_ids).count() != len(row.group_ids):
        row.status = WecomAftersalesSettings.Status.ERROR
    else:
        try:
            row.rule_id = _sync_rule(config, row, monotonic() + _SYNC_TIMEOUT)
        except ArchiveError:
            row.status = WecomAftersalesSettings.Status.ERROR
        else:
            row.status = WecomAftersalesSettings.Status.SYNCED
            row.started_at = timezone.now()
    row.next_sync_at = None if row.started_at else now + _SYNC_RETRY
    row.save(update_fields=["rule_id", "status", "started_at", "next_sync_at"])
