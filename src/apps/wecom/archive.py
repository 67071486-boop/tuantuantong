"""企微专区调用、会话展示索引和增量游标。"""
import base64
import binascii
import json
import logging
from datetime import datetime, timedelta, timezone as utc_timezone
from pathlib import Path
from time import monotonic
from uuid import uuid4

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from django_tenants.utils import get_public_schema_name
from urllib3.util import Timeout

from apps.wecom import sdk
from apps.wecom.group_sync import TimedSession
from apps.wecom.models import WecomAppConfig, WecomArchiveMessage, WecomArchiveNotification, WecomArchiveState
from apps.wecom.sdk import retry_rejected_token

logger = logging.getLogger(__name__)

ZONE_ABILITY = "tt_zone_v1"
NOTIFY_SCENE = "93"
PAGE_SIZE = 100
PAGES_PER_RUN = 4
SEARCH_PAGES = 2
SEARCH_TIMEOUT_SECONDS = 7
PROGRAM_TIMEOUT_SECONDS = 24
POLL_INTERVAL = timedelta(minutes=5)
RETRY_DELAY = timedelta(minutes=1)
NOTIFY_LIFETIME = timedelta(minutes=10)
LEASE_LENGTH = timedelta(minutes=3)
CHATDATA_PROGRAM_ENDPOINT = "chatdata/sync_call_program"
CHATDATA_CALLBACK_ENDPOINT = "chatdata/set_receive_callback"
ARCHIVE_ZONE_OPERATIONS = {"search_names": "search_chat", "search": "search_msg", "sync": "sync_msg"}
MISSING_SENDER = Q(sender_id="") | Q(sender_type__isnull=True)


class ArchiveError(Exception):
    """专区调用失败或返回无法安全处理的数据。"""


def configured_archive_tenant() -> str:
    schema = settings.WECOM_ARCHIVE_TENANT_SCHEMA.strip()
    return schema if schema != get_public_schema_name() else ""


def archive_program() -> str:
    schema = configured_archive_tenant()
    if not schema or connection.schema_name != schema:
        return ""
    program_id = settings.WECOM_ARCHIVE_PROGRAM_ID.strip()
    return program_id if len(program_id) <= 128 else ""


def _checked(payload):
    if not isinstance(payload, dict) or type(payload.get("errcode")) is not int:
        raise ArchiveError("企微专区响应格式异常")
    if payload["errcode"] != 0:
        raise ArchiveError(f"企微专区调用失败（{payload['errcode']}）")
    return payload


class _ArchiveSession(TimedSession):
    def __init__(self, deadline):
        self.deadline = deadline
        super().__init__()

    def request(self, method, url, **kwargs):
        remaining = (self.deadline - monotonic()) if self.deadline is not None else 12
        if remaining <= 0:
            raise ArchiveError("企微会话请求超时")
        kwargs["timeout"] = Timeout(total=min(12, remaining), connect=min(4, remaining), read=min(8, remaining))
        return super().request(method, url, **kwargs)


def _archive_post(config, endpoint, request_body, *, deadline=None):
    client = sdk.get_client(config, auto_retry=False)
    client._http.close()
    client._http = _ArchiveSession(deadline if deadline is not None else monotonic() + PROGRAM_TIMEOUT_SECONDS)
    try:
        return _checked(retry_rejected_token(client, lambda: client.post(endpoint, json=request_body)))
    except ArchiveError:
        raise
    except Exception as exc:
        code = getattr(exc, "errcode", None)
        suffix = f"（{code}）" if type(code) is int else ""
        raise ArchiveError(f"企微专区调用失败{suffix}") from None
    finally:
        client._http.close()


def register_archive_callback(config, program_id):
    if (not program_id or program_id != archive_program()
            or config.tenant_schema != connection.schema_name or not config.is_ready()):
        raise ArchiveError("当前工作室未配置企微会话回调")
    _archive_post(config, CHATDATA_CALLBACK_ENDPOINT, {"program_id": program_id})


def _call_program(config, program_id, operation, parameters, *, notify_id="", deadline=None):
    if operation == "token":
        ability_id = "tt_archive_token"
        payload = parameters
    else:
        ability_id = ZONE_ABILITY
        payload = {"op": ARCHIVE_ZONE_OPERATIONS[operation], "params": parameters}
    if operation == "sync" and deadline is None:
        deadline = monotonic() + PROGRAM_TIMEOUT_SECONDS
    result = _call_program_ability(config, program_id, ability_id, payload, notify_id=notify_id, deadline=deadline)
    code = result.get("errcode") if isinstance(result, dict) else None
    if operation == "sync" and type(code) is int and code == -1:
        # 送审版可能因单条消息结构无法投影而拒绝整页，用旧能力读取同一游标。
        logger.warning("企微新版同步程序拒绝该页，尝试通过旧能力读取同一页")
        result = _call_program_ability(config, program_id, "tt_archive_sync", parameters, deadline=deadline)
    return _checked(result)


def _call_program_ability(config, program_id, ability_id, payload, *, notify_id="", deadline=None):
    request_data = {"input": {"payload": json.dumps(payload, ensure_ascii=False)}}
    request_body = {
        "program_id": program_id,
        "ability_id": ability_id,
        "request_data": json.dumps(request_data, ensure_ascii=False),
    }
    if notify_id:
        request_body["notify_id"] = notify_id
    result = _archive_post(config, CHATDATA_PROGRAM_ENDPOINT, request_body, deadline=deadline)
    try:
        response_data = json.loads(result["response_data"])
        # 官网示例的 output 是应用侧外壳，兼容专区直接回包。
        output = response_data["output"] if "output" in response_data else response_data
        return json.loads(output["payload"])
    except (KeyError, TypeError, ValueError):
        raise ArchiveError("企微专区响应格式异常") from None


def _page(result, *, limit, list_field="msg_list"):
    records = result.get(list_field, [])
    cursor = result.get("next_cursor", "")
    more = result.get("has_more", 0)
    if (not isinstance(records, list) or len(records) > limit or not isinstance(cursor, str)
            or len(cursor.encode()) > 128 or type(more) is not int or more not in (0, 1)
            or (more and not cursor)):
        raise ArchiveError("企微专区分页响应异常")
    return records, cursor, bool(more)


def _record_id(record, name):
    if not isinstance(record, dict):
        raise ArchiveError("企微专区消息记录格式异常")
    value = record.get(name)
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ArchiveError("企微专区消息标识异常")
    return value


def search_message_ids(
    config, source_order_no, *, start_time: int, end_time: int, deadline: float | None = None,
) -> tuple[list[str], bool]:
    if not 0 < end_time - start_time <= 31 * 86400:
        raise ArchiveError("企微会话搜索范围异常")
    return _search_ids(config, "search", source_order_no, {
        "chat_info": {"chat_type": 2, "msg_type_list": [1]},
        "start_time": start_time,
        "end_time": end_time,
        "skip_stop_words": False,
    }, list_field="msg_list", id_field="msgid", deadline=deadline)


def search_chat_ids(config, source_order_no, *, deadline: float | None = None) -> tuple[list[str], bool]:
    return _search_ids(
        config, "search_names", source_order_no, {}, list_field="chat_list", id_field="chatid", deadline=deadline,
    )


def _search_ids(config, operation, query_word, filters, *, list_field, id_field, deadline):
    program_id = archive_program()
    if not program_id or config.tenant_schema != connection.schema_name or not config.is_ready():
        raise ArchiveError("企微会话记录尚未启用")
    if not 2 <= len(query_word) <= 128:
        raise ArchiveError("企微会话搜索关键词异常")
    params = {"query_word": query_word, "limit": PAGE_SIZE, **filters}
    cutoff = monotonic() + SEARCH_TIMEOUT_SECONDS
    if deadline is not None:
        cutoff = min(cutoff, deadline)
    ids = {}
    for page_index in range(SEARCH_PAGES):
        try:
            result = _call_program(config, program_id, operation, params, deadline=cutoff)
        except ArchiveError:
            if page_index:
                return list(ids), False
            raise
        records, cursor, more = _page(result, limit=PAGE_SIZE, list_field=list_field)
        for item in records:
            ids[_record_id(item, id_field)] = None
        if not more:
            return list(ids), True
        if cursor == params.get("cursor"):
            raise ArchiveError("企微会话搜索游标重复")
        if monotonic() >= cutoff:
            break
        params["cursor"] = cursor
    return list(ids), False


def decrypt_message_key(row) -> str:
    key_dir = settings.WECOM_ARCHIVE_PRIVATE_KEY_DIR.strip()
    if not key_dir or not Path(key_dir).is_absolute():
        raise ArchiveError("企微会话密钥尚未配置")
    try:
        key = serialization.load_pem_private_key((Path(key_dir) / f"{row.public_key_version}.pem").read_bytes(), None)
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size != 2048:
            raise ValueError("invalid RSA key")
        ciphertext = base64.b64decode(row.encrypted_secret_key, validate=True)
        secret = key.decrypt(ciphertext, padding.PKCS1v15()).decode("utf-8")
        if not secret or not secret.isascii() or not secret.isprintable():
            raise ValueError("invalid message key")
        return secret
    except (OSError, ValueError, TypeError, binascii.Error, UnsupportedAlgorithm):
        raise ArchiveError("企微会话密钥无法解密") from None


def queue_notification(config, raw):
    if (raw.get("MsgType") != "event" or raw.get("Event") != "program_notify"
            or raw.get("NotifyScene") != NOTIFY_SCENE):
        return
    program_id = archive_program()
    if (not program_id or config.tenant_schema != connection.schema_name
            or not config.is_ready() or raw.get("ToUserName") != config.corp_id):
        return
    notify_id = raw.get("NotifyId")
    if not isinstance(notify_id, str) or not notify_id or len(notify_id.encode()) > 128:
        return
    WecomArchiveNotification.objects.get_or_create(
        identity_revision=config.identity_revision, program_id=program_id, notify_id=notify_id
    )


def _message_sender(record):
    sender = record.get("sender")
    if not isinstance(sender, dict):
        return "", None
    sender_id, sender_type = sender.get("id"), sender.get("type")
    if (type(sender_type) is not int or sender_type not in WecomArchiveMessage.SenderType.values
            or not isinstance(sender_id, str) or not sender_id.isprintable()
            or not sender_id.strip() or len(sender_id) > 256):
        return "", None
    return sender_id, sender_type


def _message_row(record, revision):
    if not isinstance(record, dict):
        raise ArchiveError("企微会话记录数据异常")
    if not record.get("chatid"):
        return None
    info = record.get("service_encrypt_info")
    if info is None:
        return None
    if not isinstance(info, dict):
        raise ArchiveError("企微会话加密信息异常")
    encrypted_key = info.get("encrypted_secret_key")
    if encrypted_key is None or encrypted_key == "":
        return None
    version = info.get("public_key_ver")
    timestamp = record.get("send_time")
    if (not isinstance(encrypted_key, str) or len(encrypted_key) > 512
            or type(version) is not int or not 0 < version <= 2147483647 or type(timestamp) is not int):
        raise ArchiveError("企微会话加密信息或时间异常")
    try:
        sent_at = datetime.fromtimestamp(timestamp, tz=utc_timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise ArchiveError("企微会话时间异常") from None
    sender_id, sender_type = _message_sender(record)
    return WecomArchiveMessage(
        identity_revision=revision, msg_id=_record_id(record, "msgid"), chat_id=_record_id(record, "chatid"),
        sent_at=sent_at, encrypted_secret_key=encrypted_key, public_key_version=version,
        sender_type=sender_type, sender_id=sender_id,
    )


def _claim(config, program_id, now):
    with transaction.atomic():
        state, _ = WecomArchiveState.objects.select_for_update().get_or_create(
            identity_revision=config.identity_revision, program_id=program_id
        )
        if state.lease_until is not None and state.lease_until > now:
            return None
        expired = WecomArchiveNotification.objects.filter(received_at__lte=now - NOTIFY_LIFETIME)
        expired_ids = list(expired.values_list("pk", flat=True)[:1000])
        if expired_ids:
            expired.filter(pk__in=expired_ids).delete()
        notifications = WecomArchiveNotification.objects.filter(
            identity_revision=config.identity_revision, program_id=program_id, received_at__gt=now - NOTIFY_LIFETIME,
        )
        notice = notifications.order_by("-received_at", "-pk").first()
        if state.next_poll_at is not None and state.next_poll_at > now and notice is None:
            return None
        if state.retry_at is not None and state.retry_at > now:
            return None
        state.lease_id = uuid4().hex
        state.lease_until = now + LEASE_LENGTH
        state.save(update_fields=["lease_id", "lease_until"])
        return state, notice


def _current_archive_config(config):
    return WecomAppConfig.objects.filter(
        pk=config.pk, tenant_schema=connection.schema_name, enabled=True, identity_revision=config.identity_revision,
        corp_id=config.corp_id, agent_id=config.agent_id, secret=config.secret,
    )


def _sender_compatible(previous, returned):
    current_id, current_type = previous
    sender_id, sender_type = returned
    return (not current_id or current_id == sender_id) and (current_type is None or current_type == sender_type)


def _fill_missing_senders(revision, senders):
    if not senders:
        return 0
    missing = WecomArchiveMessage.objects.select_for_update().filter(
        MISSING_SENDER, identity_revision=revision, msg_id__in={key[0] for key in senders},
    ).only("msg_id", "chat_id", "sent_at", "sender_id", "sender_type").order_by("pk")
    repaired = []
    for row in missing:
        sender = senders.get((row.msg_id, row.chat_id, row.sent_at))
        if sender and _sender_compatible((row.sender_id, row.sender_type), sender):
            row.sender_id, row.sender_type = sender
            repaired.append(row)
    return WecomArchiveMessage.objects.bulk_update(repaired, ["sender_id", "sender_type"], batch_size=200)


def _commit_page(config, state, rows, new_cursor):
    with transaction.atomic():
        current = _current_archive_config(config).select_for_update().exists()
        guarded = WecomArchiveState.objects.filter(pk=state.pk, lease_id=state.lease_id, cursor=state.cursor)
        if not current or not guarded.update(cursor=new_cursor):
            raise ArchiveError("企微会话任务配置或游标已改变")
        if rows:
            WecomArchiveMessage.objects.bulk_create(rows, ignore_conflicts=True)
            _fill_missing_senders(config.identity_revision, {
                (row.msg_id, row.chat_id, row.sent_at): (row.sender_id, row.sender_type)
                for row in rows if row.sender_id and row.sender_type is not None
            })
    state.cursor = new_cursor


def run_due():
    program_id = archive_program()
    if not program_id:
        return 0
    config = WecomAppConfig.objects.filter(tenant_schema=connection.schema_name).first()
    if not config or not config.is_ready():
        return 0
    claim = _claim(config, program_id, timezone.now())
    if claim is None:
        return 0
    state, notice = claim
    count = 0
    more = False
    retry_at = None
    try:
        token = ""
        if notice is not None:
            try:
                token = _call_program(config, program_id, "token", {}, notify_id=notice.notify_id).get("token", "")
                if not isinstance(token, str) or len(token.encode()) > 128:
                    raise ArchiveError("企微会话回调凭据异常")
            except ArchiveError:
                logger.warning("企微会话回调凭据暂不可用，尝试按游标同步")
                token = ""
        for _ in range(PAGES_PER_RUN):
            params = {"limit": PAGE_SIZE, "mode": 0}
            if state.cursor:
                params["cursor"] = state.cursor
            if token:
                params["token"] = token
            result = _call_program(config, program_id, "sync", params)
            records, cursor, more = _page(result, limit=PAGE_SIZE)
            rows = [row for record in records if (row := _message_row(record, config.identity_revision)) is not None]
            if (rows or more) and (not cursor or cursor == state.cursor):
                raise ArchiveError("企微会话同步缺少新的游标")
            _commit_page(config, state, rows, cursor or state.cursor)
            count += len(rows)
            if not more:
                if notice is not None:
                    older = WecomArchiveNotification.objects.filter(
                        identity_revision=config.identity_revision, program_id=program_id,
                        received_at__lte=notice.received_at,
                    )
                    finished_ids = list(older.values_list("pk", flat=True)[:1000])
                    if finished_ids:
                        older.filter(pk__in=finished_ids).delete()
                break
    except ArchiveError as exc:
        logger.warning("企微会话同步失败：%s", exc)
        retry_at = timezone.now() + RETRY_DELAY
    finally:
        next_poll = timezone.now() + POLL_INTERVAL
        if more or retry_at is not None:
            next_poll = timezone.now()
        WecomArchiveState.objects.filter(pk=state.pk, lease_id=state.lease_id).update(
            lease_id="", lease_until=None, next_poll_at=next_poll, retry_at=retry_at,
        )
    retention = min(730, max(1, settings.WECOM_ARCHIVE_RETENTION_DAYS))
    expired = WecomArchiveMessage.objects.filter(sent_at__lt=timezone.now() - timedelta(days=retention))
    expired_ids = list(expired.values_list("pk", flat=True)[:1000])
    if expired_ids:
        expired.filter(pk__in=expired_ids).delete()
    return count
