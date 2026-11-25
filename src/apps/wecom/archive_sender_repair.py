"""按企微会话元数据补齐已入库消息的发送人。"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as utc_timezone

from django.db import connection, transaction
from django.utils import timezone

from apps.wecom import archive
from apps.wecom.models import WecomArchiveMessage

SOURCE_WINDOW = timedelta(days=5)
SOURCE_PAGE_SIZE = 200
MAX_SOURCE_PAGES = 10
MAX_BATCHES = 60
MAX_TARGETS = 10_000


@dataclass(frozen=True)
class SenderRepairResult:
    expired: int
    eligible: int
    recovered: int
    updated: int
    limited: bool = False


def _history_batch(config, program_id, params):
    def request(operation, parameters):
        return archive._checked(archive._call_program_ability(
            config, program_id, archive.ZONE_ABILITY, {"op": operation, "params": parameters},
        ))

    result = request("sync_msg", params)
    messages, cursor, has_more = archive._page(result, limit=SOURCE_PAGE_SIZE)
    page_ids = result.get("page_id_list", [])
    if (not isinstance(page_ids, list) or len(page_ids) > MAX_SOURCE_PAGES
            or any(not isinstance(page, str) or not page or len(page.encode()) > 256 for page in page_ids)):
        raise archive.ArchiveError("企微历史消息分页响应异常")

    def pages():
        yield messages
        for page_id in page_ids:
            page = request("get_msg_list_by_page_id", {"page_id": page_id})
            records, _, _ = archive._page(page, limit=SOURCE_PAGE_SIZE)
            yield records

    return pages(), cursor, has_more


def _match_senders(records, expected, recovered):
    for record in records:
        if not isinstance(record, dict) or type(record.get("send_time")) is not int:
            raise archive.ArchiveError("企微历史消息时间格式异常")
        try:
            sent_at = datetime.fromtimestamp(record["send_time"], tz=utc_timezone.utc)
        except (ValueError, OSError, OverflowError):
            raise archive.ArchiveError("企微历史消息时间格式异常") from None
        msg_id, chat_id = record.get("msgid"), record.get("chatid")
        if not isinstance(msg_id, str) or not isinstance(chat_id, str):
            continue
        key = (msg_id, chat_id, sent_at)
        previous = expected.get(key)
        if previous is None:
            continue
        sender = archive._message_sender(record)
        if sender[0] and archive._sender_compatible(previous, sender):
            recovered.setdefault(key, sender)


def backfill_senders(config, start, end, *, chat_ids=(), max_batches=12, apply=False):
    program_id = archive.archive_program()
    if (not program_id or config.tenant_schema != connection.schema_name or not config.is_ready()
            or not archive._current_archive_config(config).exists()):
        raise archive.ArchiveError("当前工作室未启用企微会话记录或应用配置已变更")
    now = timezone.now()
    if timezone.is_naive(start) or timezone.is_naive(end) or not start < end <= now:
        raise archive.ArchiveError("请输入带时区且有效的历史补齐时间段")
    if not 1 <= max_batches <= MAX_BATCHES:
        raise archive.ArchiveError(f"最多拉取批数请填写 1 到 {MAX_BATCHES}")
    if any(not chat_id or len(chat_id) > 256 or not chat_id.isprintable() for chat_id in chat_ids):
        raise archive.ArchiveError("企微群 ID 格式异常")

    rows = WecomArchiveMessage.objects.filter(
        archive.MISSING_SENDER, identity_revision=config.identity_revision, sent_at__gte=start, sent_at__lt=end,
    )
    if chat_ids:
        rows = rows.filter(chat_id__in=chat_ids)
    cutoff = now - SOURCE_WINDOW
    expired = rows.filter(sent_at__lt=cutoff).count()
    eligible_rows = rows.filter(sent_at__gte=cutoff)
    expected = {
        (mid, cid, sent): (sid, kind)
        for mid, cid, sent, sid, kind in eligible_rows.order_by("sent_at", "pk").values_list(
            "msg_id", "chat_id", "sent_at", "sender_id", "sender_type",
        )[:MAX_TARGETS + 1]
    }
    if len(expected) > MAX_TARGETS:
        raise archive.ArchiveError(f"待补消息超过 {MAX_TARGETS} 条，请缩小时间段或指定企微群 ID")
    if not expected:
        return SenderRepairResult(expired, 0, 0, 0)

    first_time = next(iter(expected))[2]
    params = {"mode": 1, "limit": SOURCE_PAGE_SIZE, "begin_time": max(int(cutoff.timestamp()), int(first_time.timestamp()) - 1)}
    recovered = {}
    cursors = set()
    limited = False
    for _ in range(max_batches):
        pages, cursor, has_more = _history_batch(config, program_id, params)
        for messages in pages:
            _match_senders(messages, expected, recovered)
            if len(recovered) == len(expected):
                break
        if len(recovered) == len(expected) or not has_more:
            break
        if cursor in cursors:
            raise archive.ArchiveError("企微历史消息游标重复")
        cursors.add(cursor)
        params = {"mode": 1, "limit": SOURCE_PAGE_SIZE, "cursor": cursor}
    else:
        limited = True

    updated = 0
    if apply and recovered:
        with transaction.atomic():
            if (archive.archive_program() != program_id
                    or not archive._current_archive_config(config).select_for_update().exists()):
                raise archive.ArchiveError("企微应用配置已变更，请重新预览")
            updated = archive._fill_missing_senders(config.identity_revision, recovered)
    return SenderRepairResult(expired, len(expected), len(recovered), updated, limited)
