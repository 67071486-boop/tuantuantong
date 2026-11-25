"""接收工作室专区事件并保存可核查的回执。"""
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from time import monotonic

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from apps.wecom.archive import ArchiveError, archive_program
from apps.wecom.models import WecomAppConfig, WecomZoneKeywordHit, WecomZoneKeywordState, WecomZoneNotification
from apps.wecom.zone_client import call_zone_operation, read_zone_event, zone_enabled

logger = logging.getLogger(__name__)

NOTIFY_SCENE = "94"
NOTIFY_LIFETIME = timedelta(minutes=10)
RETRY_DELAY = timedelta(seconds=45)
RUN_BUDGET_SECONDS = 12
MAX_ITEMS_PER_RUN = 8
PAGE_SIZE = 100
RECOVERY_PAGE_SIZE = 1000
KEYWORD_CURSOR_BYTES = 64

_EVENT_FIELDS = {
    "chat_archive_audit_approved_single": ("chat_archive_audit_approved", ("userid", "external_userid")),
    "chat_archive_audit_approved_room": ("chat_archive_audit_approved", ("chatid", "external_userid")),
    "hit_keyword": ("hit_keyword", ("token",)),
    "auth_knowledge_base": ("auth_knowledge_base", ("knowledge_base_id", "knowledge_base_name")),
    "unauth_knowledge_base": ("unauth_knowledge_base", ("knowledge_base_id", "knowledge_base_name")),
    "delete_knowledge_base": ("delete_knowledge_base", ("knowledge_base_id", "knowledge_base_name")),
    "knowledge_base_learn_done": (
        "knowledge_base_learn_done", ("knowledge_base_id", "knowledge_base_name", "doc_id", "learn_status"),
    ),
    "chat_archive_export_finished": ("chat_archive_export_finished", ("jobid",)),
}


def queue_notification(config, raw):
    if (not zone_enabled() or config.tenant_schema != connection.schema_name or not config.is_ready()
            or raw.get("MsgType") != "event" or raw.get("Event") != "program_notify"
            or raw.get("NotifyScene") != NOTIFY_SCENE or raw.get("ToUserName") != config.corp_id):
        return
    notify_id = raw.get("NotifyId")
    if not isinstance(notify_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", notify_id) or notify_id in {".", ".."}:
        return
    agent_id = raw.get("AgentID")
    if agent_id is not None and str(agent_id) != str(config.agent_id):
        return
    WecomZoneNotification.objects.get_or_create(
        identity_revision=config.identity_revision, program_id=archive_program(), notify_id=notify_id,
    )


def _event_details(event):
    kind, stamp = event.get("event_type"), event.get("timestamp")
    if not isinstance(kind, str) or kind not in _EVENT_FIELDS or type(stamp) is not int or stamp < 0:
        raise ArchiveError("企微专区通知格式异常")
    nested, fields = _EVENT_FIELDS[kind]
    values = event.get(nested)
    if not isinstance(values, dict):
        raise ArchiveError("企微专区通知缺少事件信息")
    details = {"timestamp": stamp}
    for field in fields:
        if field == "token":
            continue
        value = values.get(field)
        if isinstance(value, str) and value.isprintable() and 0 < len(value) <= 256:
            details[field] = value
        elif type(value) is int and field in {"doc_id", "learn_status"} and 0 <= value < 2 ** 64:
            details[field] = value
        elif field != "knowledge_base_name":
            raise ArchiveError("企微专区通知缺少事件标识")
    token = values.get("token") if kind == "hit_keyword" else None
    if kind == "hit_keyword" and (not isinstance(token, str) or not 0 < len(token.encode()) <= 128):
        raise ArchiveError("企微专区关键词通知缺少凭据")
    return kind, details, token


@dataclass
class _KeywordPage:
    hits: list[WecomZoneKeywordHit]
    message_ids: set[str]
    cursor: str | None
    more: bool


def _read_keyword_page(config, state, params, deadline):
    payload = call_zone_operation(config, "get_hit_msg_list", params, deadline=deadline)
    page, more = payload.get("msg_list"), payload.get("has_more")
    cursor = payload.get("next_cursor", "")
    if (not isinstance(page, list) or len(page) > params["limit"] or type(more) is not int or more not in (0, 1)
            or not isinstance(cursor, str) or "\0" in cursor or (more and not cursor)):
        raise ArchiveError("企微专区关键词命中分页异常")
    try:
        cursor_bytes = len(cursor.encode())
    except UnicodeEncodeError:
        raise ArchiveError("企微专区关键词命中分页异常") from None
    matches, message_ids = {}, set()
    for message in page:
        if not isinstance(message, dict):
            raise ArchiveError("企微专区关键词命中记录异常")
        msg_id, rules = message.get("msgid"), message.get("hit_rule_list")
        if not isinstance(msg_id, str) or not 0 < len(msg_id) <= 256 or not msg_id.isprintable() or not isinstance(rules, list):
            raise ArchiveError("企微专区关键词命中记录异常")
        message_ids.add(msg_id)
        for rule in rules:
            if not isinstance(rule, dict):
                raise ArchiveError("企微专区关键词命中规则异常")
            rule_id, hit, semantics = rule.get("rule_id"), rule.get("has_hit_keyword"), rule.get("semantics_list", [])
            if (not isinstance(rule_id, str) or not 0 < len(rule_id) <= 128 or not rule_id.isprintable()
                    or type(hit) is not bool or not isinstance(semantics, list) or len(semantics) > 32
                    or any(type(item) is not int or not 0 < item < 65536 for item in semantics)):
                raise ArchiveError("企微专区关键词命中规则异常")
            matches[msg_id, rule_id] = WecomZoneKeywordHit(
                identity_revision=state.identity_revision, program_id=state.program_id,
                msg_id=msg_id, rule_id=rule_id, has_hit_keyword=hit, semantics=semantics,
            )
    return _KeywordPage(
        hits=list(matches.values()), message_ids=message_ids, cursor=cursor if cursor_bytes <= KEYWORD_CURSOR_BYTES else None,
        more=bool(more),
    )


def _claim(pending, now):
    with transaction.atomic():
        row = pending.filter(Q(retry_at__isnull=True) | Q(retry_at__lte=now)).select_for_update(
            skip_locked=True,
        ).order_by("received_at", "pk").first()
        if row is not None:
            row.retry_at = now + RETRY_DELAY
            row.save(update_fields=["retry_at"])
        return row


def _require_current_config(config, program_id):
    current = WecomAppConfig.objects.select_for_update().filter(
        pk=config.pk, tenant_schema=connection.schema_name, enabled=True, identity_revision=config.identity_revision,
        corp_id=config.corp_id, agent_id=config.agent_id, secret=config.secret,
    ).exists()
    if not current or program_id != archive_program():
        raise ArchiveError("企微专区应用身份已变化")


def _store_notice(notice, kind, details, *, more=False, retry=False):
    changed = WecomZoneNotification.objects.filter(
        pk=notice.pk, retry_at=notice.retry_at, completed_at__isnull=True, expired_at__isnull=True,
    ).update(
        event_type=kind, details=details, retry_at=notice.retry_at if retry else None,
        completed_at=None if more else timezone.now(),
    )
    if not changed:
        raise ArchiveError("企微专区通知处理状态已变化")


def _claim_keyword(config, program_id, *, notified):
    now = timezone.now()
    with transaction.atomic():
        states = WecomZoneKeywordState.objects.select_for_update().filter(
            identity_revision=config.identity_revision, program_id=program_id,
        )
        if notified:
            state, _ = states.get_or_create(identity_revision=config.identity_revision, program_id=program_id)
        else:
            state = states.filter(needs_pull=True).first()
        if state is None or (state.retry_at is not None and state.retry_at > now):
            return None
        state.retry_at, state.needs_pull = now + RETRY_DELAY, True
        state.save(update_fields=["retry_at", "needs_pull"])
        return state


def _pull_keywords(config, program_id, deadline, *, notice=None, token=None, details=None):
    state = _claim_keyword(config, program_id, notified=notice is not None)
    if state is None:
        return False
    params = {"limit": PAGE_SIZE}
    if state.cursor and len(state.cursor.encode()) <= KEYWORD_CURSOR_BYTES:
        params["cursor"] = state.cursor
    if token is not None:
        params["token"] = token
    requested_cursor = params.get("cursor", "")
    page = _read_keyword_page(config, state, params, deadline)
    hits = page.hits
    blocked = None
    if page.more and page.cursor is None:
        # 命中可能延迟入库，扩大同一起点的页宽可以保留旧消息补读的机会。
        try:
            expanded = _read_keyword_page(config, state, {**params, "limit": RECOVERY_PAGE_SIZE}, deadline)
        except ArchiveError:
            blocked = "扩大读取范围失败"
        else:
            hits.extend(expanded.hits)
            if page.message_ids <= expanded.message_ids:
                page = expanded
            else:
                blocked = "扩大后的结果未覆盖原分页"
    if blocked is None and page.more:
        if page.cursor is None:
            blocked = "读取最大页后仍无可用游标"
        elif page.cursor == requested_cursor:
            blocked = "返回的游标与请求相同"
    cursor = requested_cursor if blocked else (page.cursor or requested_cursor)
    more = page.more or blocked is not None
    with transaction.atomic():
        _require_current_config(config, program_id)
        changed = WecomZoneKeywordState.objects.filter(pk=state.pk, retry_at=state.retry_at).update(
            cursor=cursor, needs_pull=more, retry_at=state.retry_at if blocked else None,
        )
        if not changed:
            raise ArchiveError("企微专区关键词同步状态已变化")
        if hits:
            WecomZoneKeywordHit.objects.bulk_create(hits, ignore_conflicts=True, batch_size=1000)
        if notice is not None:
            _store_notice(notice, "hit_keyword", details, more=more, retry=blocked is not None)
    if blocked:
        logger.warning("企微专区关键词命中补拉未完成：%s，等待重试", blocked)
    return not more


def _read_consent(config, details, deadline):
    if "chatid" in details:
        operation, params = "get_agree_status_room", {"chatid": details["chatid"]}
    else:
        actor = {field: details[field] for field in ("userid", "external_userid")}
        operation, params = "get_agree_status_single", {"item": [actor]}
    results = call_zone_operation(config, operation, params, deadline=deadline).get("agreeinfo")
    if not isinstance(results, list):
        raise ArchiveError("企微专区会话同意状态异常")
    for result in results:
        if (isinstance(result, dict) and result.get("external_userid") == details["external_userid"]
                and ("userid" not in details or result.get("userid") == details["userid"])):
            status, changed_at = result.get("agree_status"), result.get("status_change_time")
            if status in ("Agree", "Disagree") and type(changed_at) is int and changed_at >= 0:
                return {"agree_status": status, "status_change_time": changed_at, "consent_observed": True}
            raise ArchiveError("企微专区会话同意状态异常")
    return {"consent_observed": False}


def _read_export(config, jobid, deadline):
    result = call_zone_operation(config, "get_chatdata_export_job_status", {"jobid": jobid}, deadline=deadline)
    if type(result.get("status")) is not int or result["status"] != 3 or type(result.get("result_errcode")) is not int:
        raise ArchiveError("企微专区导出任务结果暂不可用")
    details = {"status": 3, "result_errcode": result["result_errcode"]}
    if result["result_errcode"] == 0:
        result_id = result.get("result_id")
        if not isinstance(result_id, str) or not 0 < len(result_id) <= 256 or not result_id.isprintable():
            raise ArchiveError("企微专区导出结果编号异常")
        details["result_id"] = result_id
    return details


def _process(config, notice, deadline):
    event = read_zone_event(config, notice.notify_id, deadline=deadline)
    if not isinstance(event, dict):
        raise ArchiveError("企微专区通知格式异常")
    kind, details, token = _event_details(event)
    if token is not None:
        return _pull_keywords(config, notice.program_id, deadline, notice=notice, token=token, details=details)
    if kind in {"chat_archive_audit_approved_single", "chat_archive_audit_approved_room"}:
        details.update(_read_consent(config, details, deadline))
    elif kind == "chat_archive_export_finished":
        details.update(_read_export(config, details["jobid"], deadline))
    with transaction.atomic():
        _require_current_config(config, notice.program_id)
        _store_notice(notice, kind, details)
    return True


def _prune(now):
    days = min(730, max(1, settings.WECOM_ARCHIVE_RETENTION_DAYS))
    cutoff = now - timedelta(days=days)
    for model in (WecomZoneNotification, WecomZoneKeywordHit):
        old_ids = list(model.objects.filter(received_at__lt=cutoff).order_by("pk").values_list("pk", flat=True)[:1000])
        if old_ids:
            model.objects.filter(pk__in=old_ids).delete()


def run_due():
    if not zone_enabled():
        return 0
    config = WecomAppConfig.objects.filter(tenant_schema=connection.schema_name).first()
    if config is None or not config.is_ready():
        return 0
    program_id = archive_program()
    pending = WecomZoneNotification.objects.filter(
        identity_revision=config.identity_revision, program_id=program_id,
        completed_at__isnull=True, expired_at__isnull=True,
    )
    now = timezone.now()
    pending.filter(received_at__lte=now - NOTIFY_LIFETIME).update(expired_at=now)
    _prune(now)
    deadline = monotonic() + RUN_BUDGET_SECONDS
    completed = 0
    for _ in range(MAX_ITEMS_PER_RUN):
        if monotonic() >= deadline:
            break
        notice = _claim(pending, timezone.now())
        if notice is None:
            break
        try:
            completed += _process(config, notice, deadline)
        except ArchiveError:
            logger.warning("企微专区通知暂未处理 notice_pk=%s", notice.pk)
    if monotonic() < deadline:
        try:
            _pull_keywords(config, program_id, deadline)
        except ArchiveError:
            logger.warning("企微专区关键词命中补拉暂未完成")
    return completed
