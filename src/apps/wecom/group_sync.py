"""持久化群变更任务，并在后台刷新客户群快照。"""
from datetime import timedelta
import logging
import requests

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.wecom import sdk
from apps.wecom.models import WecomAppConfig, WecomGroupChat

logger = logging.getLogger(__name__)
RETRY_DELAY = timedelta(minutes=1)
SETTLE_DELAY = timedelta(seconds=5)
CONFIRM_DELAY = timedelta(minutes=1)
REQUEST_TIMEOUT = (5, 10)


class TimedSession(requests.Session):
    def request(self, method, url, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = REQUEST_TIMEOUT
        return super().request(method, url, **kwargs)


def _contacts_client(config):
    client = sdk.get_contacts_client(config)
    client._http.close()
    client._http = TimedSession()
    client.timeout = REQUEST_TIMEOUT
    return client


def group_detail_with_names(client, chat_id):
    return client.post("externalcontact/groupchat/get", json={"chat_id": chat_id, "need_name": 1})


def queue_event(raw, *, identity_revision=None):
    if raw.get("MsgType") != "event" or raw.get("Event") != "change_external_chat":
        return
    chat_id = raw.get("ChatId")
    kind = raw.get("ChangeType")
    if not isinstance(chat_id, str) or not chat_id or len(chat_id) > 256:
        raise ValueError("invalid group chat id")
    if kind not in {"create", "update", "dismiss"}:
        return
    try:
        event_time = int(raw.get("CreateTime") or 0)
    except (TypeError, ValueError):
        raise ValueError("invalid event time") from None
    with transaction.atomic():
        config = WecomAppConfig.objects.select_for_update().first()
        if config is None or (identity_revision is not None and config.identity_revision != identity_revision):
            raise ValueError("企微配置已改变")
        group, _ = WecomGroupChat.objects.get_or_create(chat_id=chat_id)
        group = WecomGroupChat.objects.select_for_update().get(pk=group.pk)
        if group.identity_revision != config.identity_revision:
            group.identity_revision = config.identity_revision
            group.event_time, group.name, group.details = 0, "", {}
            group.dismissed = False
            group.pending_dismissal = False
        if event_time < group.event_time or (event_time == group.event_time and (group.dismissed or group.pending_dismissal) and kind != "dismiss"):
            return
        group.event_time = event_time
        group.revision += 1
        group.pending_dismissal = kind == "dismiss" and not config.group_sync_enabled
        if config.group_sync_enabled:
            group.dismissed = kind == "dismiss"
        group.pending = kind != "dismiss" or group.pending_dismissal
        group.confirming = False
        group.retry_at = timezone.now() + SETTLE_DELAY if group.pending else None
        group.save(update_fields=["identity_revision", "event_time", "name", "details", "revision", "dismissed", "pending_dismissal", "pending", "confirming", "retry_at", "updated_at"])


def run_due(limit=200):
    config = WecomAppConfig.objects.first()
    if config is None or not config.contacts_ready() or not config.group_sync_enabled:
        return 0
    now = timezone.now()
    rows = list(WecomGroupChat.objects.filter(pending=True, identity_revision=config.identity_revision).filter(
        Q(retry_at__isnull=True) | Q(retry_at__lte=now)
    ).order_by("retry_at", "pk").values("pk", "chat_id", "revision", "confirming", "pending_dismissal")[:limit])
    for row in rows:
        if not WecomAppConfig.objects.filter(pk=config.pk, group_sync_enabled=True, identity_revision=config.identity_revision).exists():
            break
        match = WecomGroupChat.objects.filter(pk=row["pk"], revision=row["revision"], pending=True)
        if row['pending_dismissal']:
            with transaction.atomic():
                current = WecomAppConfig.objects.select_for_update().filter(pk=config.pk, identity_revision=config.identity_revision, group_sync_enabled=True).first()
                if current is None:
                    break
                match.update(dismissed=True, pending=False, pending_dismissal=False, confirming=False, retry_at=None)
            continue
        client = None
        try:
            client = _contacts_client(config)
            result = group_detail_with_names(client, row["chat_id"])
            if result.get("errcode") != 0 or not isinstance(result.get("group_chat"), dict):
                raise ValueError("企微群详情响应无效")
            details = {key: value for key, value in result["group_chat"].items() if key != "notice"}
            details.pop("config_id", None)
            name = details.get("name") or ""
            if not isinstance(name, str) or len(name) > 512:
                raise ValueError("企微群名称无效")
            confirming = row["confirming"]
            with transaction.atomic():
                current = WecomAppConfig.objects.select_for_update().filter(pk=config.pk, identity_revision=config.identity_revision).first()
                if current is None or not current.group_sync_enabled:
                    break
                group = WecomGroupChat.objects.select_for_update().filter(pk=row["pk"], revision=row["revision"], pending=True, identity_revision=config.identity_revision).first() if current else None
                if group is not None:
                    qr = getattr(group, "qr_config", None)
                    if qr and qr.config_id and qr.corp_id == config.corp_id and qr.agent_id == config.agent_id:
                        details["config_id"] = qr.config_id
                    group.name, group.details = name, details
                    group.dismissed = False
                    group.pending, group.confirming = not confirming, True
                    group.retry_at = None if confirming else timezone.now() + CONFIRM_DELAY
                    group.save(update_fields=["name", "details", "dismissed", "pending", "confirming", "retry_at", "updated_at"])
        except Exception as exc:
            logger.warning("企微群详情同步失败 chat_id=%s error_type=%s", row["chat_id"], type(exc).__name__)
            with transaction.atomic():
                current = WecomAppConfig.objects.select_for_update().filter(pk=config.pk, identity_revision=config.identity_revision, group_sync_enabled=True).first()
                if current is None:
                    break
                match.update(retry_at=timezone.now() + RETRY_DELAY)
        finally:
            if client is not None:
                client._http.close()
    return len(rows)
