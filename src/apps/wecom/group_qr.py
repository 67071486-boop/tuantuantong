"""进群二维码按群复用，并在闲置一天后清理。"""
from datetime import timedelta
import logging
from uuid import uuid4

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from wechatpy.exceptions import WeChatClientException

from apps.wecom import sdk
from apps.wecom.group_sync import REQUEST_TIMEOUT, TimedSession
from apps.wecom.models import WecomAppConfig, WecomGroupChat, WecomGroupQr

logger = logging.getLogger(__name__)
LEASE = timedelta(minutes=2)
RETENTION = timedelta(days=1)
REJECTED_BEFORE_CREATE = {40001, 40014, 42001, 60011, 60020}


def _reply(status, message="", qr_url=""):
    return {"status": status, "message": message, "qr_url": qr_url}


def _error_code(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else "unknown"


def _client(config):
    client = sdk.get_client(config)
    client._http.close()
    client._http = TimedSession()
    client.timeout = REQUEST_TIMEOUT
    return client


def _call(config, endpoint, data):
    client = _client(config)
    try:
        return client.post("externalcontact/groupchat/" + endpoint, data=data)
    finally:
        client._http.close()


def _mirror(group, config_id):
    details = dict(group.details or {})
    if config_id:
        details["config_id"] = config_id
    else:
        details.pop("config_id", None)
    group.details = details
    group.save(update_fields=["details", "updated_at"])


def _claim(group_id, config, expected_source, *, allow_create=True):
    now = timezone.now()
    with transaction.atomic():
        current = WecomAppConfig.objects.select_for_update().filter(
            pk=config.pk, identity_revision=config.identity_revision, corp_id=config.corp_id,
            agent_id=config.agent_id, secret=config.secret, enabled=True, group_qr_enabled=True,
        ).first()
        if current is None:
            return None, _reply("unavailable", "企微配置已改变，请重试")
        group = WecomGroupChat.objects.select_for_update().get(pk=group_id)
        if group.dismissed or group.identity_revision != config.identity_revision:
            return None, _reply("unavailable", "该群已解散")
        if expected_source is not None and expected_source not in group.name:
            return None, _reply("unavailable", "群信息已变更，请重新打开订单")
        if allow_create:
            WecomGroupQr.objects.get_or_create(group=group)
        row = WecomGroupQr.objects.select_for_update().filter(group=group).first()
        if row is None or (
            not allow_create and not row.config_id and row.state == "idle"
        ):
            return None, _reply("not_found", "尚未生成企微群码")
        row.last_viewed_at = now
        if row.corp_id and (row.config_id or row.state in {"creating", "uncertain", "deleting"}) and (row.corp_id != config.corp_id or row.agent_id != config.agent_id):
            row.save(update_fields=["last_viewed_at", "updated_at"])
            return None, _reply("unavailable", "企微应用已更换，请联系管理员处理原群码")
        stale = row.operation_at is None or row.operation_at <= now - LEASE
        if row.state == "creating" and stale:
            row.state = "uncertain"
        if row.state == "uncertain":
            row.save(update_fields=["state", "last_viewed_at", "updated_at"])
            return None, _reply("uncertain", "群码状态需要管理员核查")
        if row.state == "deleting" or (row.state in {"creating", "reading"} and not stale):
            row.save(update_fields=["last_viewed_at", "updated_at"])
            return None, _reply("pending", "群码正在处理，请稍候")
        if not row.config_id and not allow_create:
            return None, _reply("not_found", "尚未生成企微群码")
        operation_id = uuid4().hex
        row.state = "reading" if row.config_id else "creating"
        row.operation_id = operation_id
        row.operation_at = now
        row.corp_id = config.corp_id
        row.agent_id = config.agent_id
        row.save(update_fields=["state", "operation_id", "operation_at", "corp_id", "agent_id", "last_viewed_at", "updated_at"])
        return {"pk": row.pk, "group_id": group.pk, "chat_id": group.chat_id,
                "config_id": row.config_id, "operation_id": operation_id,
                "identity_revision": config.identity_revision}, None


def _finish_create(claim, config_id):
    with transaction.atomic():
        group = WecomGroupChat.objects.select_for_update().get(pk=claim["group_id"])
        row = WecomGroupQr.objects.select_for_update().get(pk=claim["pk"])
        if row.state not in {"creating", "uncertain"} or row.operation_id != claim["operation_id"]:
            return False
        row.config_id = config_id
        row.state = "reading"
        row.operation_at = timezone.now()
        row.save(update_fields=["config_id", "state", "operation_at", "updated_at"])
        if group.identity_revision == claim["identity_revision"]:
            _mirror(group, config_id)
        return True


def _set_state(claim, state, *, expected=None):
    rows = WecomGroupQr.objects.filter(pk=claim["pk"], operation_id=claim["operation_id"])
    if expected is not None:
        rows = rows.filter(state=expected)
    return rows.update(
        state=state, operation_at=None, updated_at=timezone.now()
    )


def _current_context(claim, config, expected_source, order_still_matches):
    config_ok = WecomAppConfig.objects.filter(
        pk=config.pk, enabled=True, identity_revision=claim["identity_revision"],
        corp_id=config.corp_id, agent_id=config.agent_id, secret=config.secret, group_qr_enabled=True,
    ).exists()
    groups = WecomGroupChat.objects.filter(pk=claim["group_id"], dismissed=False, identity_revision=claim["identity_revision"])
    if expected_source is not None:
        groups = groups.filter(name__contains=expected_source)
    return config_ok and groups.exists() and (order_still_matches is None or order_still_matches())


def _verify_group(config, claim, expected_source, strict_name=None):
    result = _call(config, "get", {"chat_id": claim["chat_id"]})
    group = result.get("group_chat") if isinstance(result, dict) and result.get("errcode") == 0 else None
    return (isinstance(group, dict) and group.get("chat_id") == claim["chat_id"]
            and isinstance(group.get("name"), str) and (expected_source is None or expected_source in group["name"])
            and (strict_name is None or strict_name(group["name"])))


def get_or_create_qr(group, *, expected_source=None, order_still_matches=None, allow_create=True, strict_name=None):
    config = WecomAppConfig.objects.first()
    if config is None or not config.is_ready():
        return _reply("unavailable", "企微应用未启用或配置不完整")
    if not config.group_qr_enabled:
        return _reply("unavailable", "订单群码功能已关闭")
    claim, reply = _claim(group.pk, config, expected_source, allow_create=allow_create)
    if reply is not None:
        return reply
    config_id = claim["config_id"]
    try:
        verified = _verify_group(config, claim, expected_source, strict_name)
    except WeChatClientException as exc:
        logger.warning("企微群码展示前群校验失败 errcode=%s error_type=%s", _error_code(exc.errcode), type(exc).__name__)
        verified = False
    except Exception as exc:
        logger.warning("企微群码展示前群校验失败 error_type=%s", type(exc).__name__)
        verified = False
    if not verified or not _current_context(claim, config, expected_source, order_still_matches):
        _set_state(claim, "ready" if config_id else "idle", expected="reading" if config_id else "creating")
        return _reply("unavailable", "无法确认企微群与订单匹配，请稍后重试")
    if not config_id:
        try:
            result = _call(config, "add_join_way", {"scene": 2, "auto_create_room": 0, "chat_id_list": [claim["chat_id"]]})
            if isinstance(result, dict) and isinstance(result.get("errcode"), int) and result["errcode"] != 0:
                logger.warning("企微创建群码失败 errcode=%s", _error_code(result["errcode"]))
                rejected = result["errcode"] in REJECTED_BEFORE_CREATE
                _set_state(claim, "idle" if rejected else "uncertain")
                return _reply("unavailable" if rejected else "uncertain", "企微创建群码失败，请稍后重试" if rejected else "群码创建结果不确定，请联系管理员核查")
            config_id = result.get("config_id") if isinstance(result, dict) and result.get("errcode") == 0 else None
            if not isinstance(config_id, str) or not config_id or len(config_id) > 256:
                logger.warning("企微群码创建响应缺少可用编号，结果不确定")
                _set_state(claim, "uncertain")
                return _reply("uncertain", "群码创建结果不确定，请联系管理员核查")
            if not _finish_create(claim, config_id):
                logger.error("企微群码已创建但登记状态不一致 config_id=%s", config_id)
                return _reply("uncertain", "群码创建结果需要管理员核查")
        except WeChatClientException as exc:
            logger.warning("企微创建群码失败 errcode=%s error_type=%s", _error_code(exc.errcode), type(exc).__name__)
            rejected = exc.errcode in REJECTED_BEFORE_CREATE
            _set_state(claim, "idle" if rejected else "uncertain")
            return _reply("unavailable" if rejected else "uncertain", "企微创建群码失败，请稍后重试" if rejected else "群码创建结果不确定，请联系管理员核查")
        except Exception as exc:
            if config_id:
                logger.error("企微群码编号保存失败 config_id=%s error_type=%s", config_id, type(exc).__name__)
            else:
                logger.warning("企微群码创建结果不确定 error_type=%s", type(exc).__name__)
            _set_state(claim, "uncertain")
            return _reply("uncertain", "群码创建结果不确定，请联系管理员核查")
        if not _current_context(claim, config, expected_source, order_still_matches):
            _set_state(claim, "ready", expected="reading")
            return _reply("unavailable", "群或订单信息已变更，请重新打开订单")
    try:
        result = _call(config, "get_join_way", {"config_id": config_id})
        way = result.get("join_way") if isinstance(result, dict) and result.get("errcode") == 0 else None
        url = way.get("qr_code") if isinstance(way, dict) else None
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            logger.warning("读取企微群码失败 errcode=%s", _error_code(result.get("errcode") if isinstance(result, dict) else None))
            return _reply("unavailable", "暂时无法获取群码，请稍后重试")
        if not _current_context(claim, config, expected_source, order_still_matches):
            return _reply("unavailable", "群或订单信息已变更，请重新打开订单")
        return _reply("ready", qr_url=url)
    except WeChatClientException as exc:
        logger.warning("读取企微群码失败 errcode=%s error_type=%s", _error_code(exc.errcode), type(exc).__name__)
        if exc.errcode == 41044:
            _set_state(claim, "uncertain", expected="reading")
            return _reply("uncertain", "企微未能识别群码编号，请联系管理员核查")
        return _reply("unavailable", "暂时无法获取群码，请稍后重试")
    except Exception as exc:
        logger.warning("读取企微群码失败 error_type=%s", type(exc).__name__)
        return _reply("unavailable", "暂时无法获取群码，请稍后重试")
    finally:
        _set_state(claim, "ready", expected="reading")


def cleanup_due(limit=100):
    config = WecomAppConfig.objects.first()
    if config is None or not config.is_ready():
        return 0
    now = timezone.now()
    candidates = list(WecomGroupQr.objects.filter(
        config_id__gt="", corp_id=config.corp_id, agent_id=config.agent_id,
        group__identity_revision=config.identity_revision,
    ).filter(
        Q(state="ready", last_viewed_at__lte=now - RETENTION)
        | Q(state="reading", last_viewed_at__lte=now - RETENTION, operation_at__lte=now - LEASE)
        | Q(state="deleting", operation_at__lte=now - LEASE)
    ).order_by("last_viewed_at", "pk")
                      .values_list("pk", "group_id")[:limit])
    for pk, group_id in candidates:
        with transaction.atomic():
            current = WecomAppConfig.objects.select_for_update().filter(
                pk=config.pk, identity_revision=config.identity_revision, corp_id=config.corp_id,
                agent_id=config.agent_id, secret=config.secret, enabled=True,
            ).first()
            if current is None:
                continue
            group = WecomGroupChat.objects.select_for_update().get(pk=group_id)
            row = WecomGroupQr.objects.select_for_update().get(pk=pk)
            if (not row.config_id or not row.last_viewed_at
                    or group.identity_revision != config.identity_revision
                    or row.corp_id != config.corp_id or row.agent_id != config.agent_id):
                continue
            if row.state in {"ready", "reading"} and row.last_viewed_at > timezone.now() - RETENTION:
                continue
            if row.state in {"reading", "deleting"} and row.operation_at and row.operation_at > timezone.now() - LEASE:
                continue
            if row.state not in {"ready", "reading", "deleting"}:
                continue
            resuming = row.state == "deleting"
            op_id, config_id = uuid4().hex, row.config_id
            row.state, row.operation_id, row.operation_at = "deleting", op_id, timezone.now()
            row.save(update_fields=["state", "operation_id", "operation_at", "updated_at"])
        with transaction.atomic():
            current = WecomAppConfig.objects.select_for_update().filter(
                pk=config.pk, identity_revision=config.identity_revision, corp_id=config.corp_id,
                agent_id=config.agent_id, secret=config.secret, enabled=True,
            ).first()
            group = WecomGroupChat.objects.select_for_update().get(pk=group_id)
            row = WecomGroupQr.objects.select_for_update().get(pk=pk)
            if row.state != "deleting" or row.operation_id != op_id or row.config_id != config_id:
                continue
            if current is None or group.identity_revision != config.identity_revision:
                row.state, row.operation_at = "ready", None
                row.save(update_fields=["state", "operation_at", "updated_at"])
                continue
            if not resuming and row.last_viewed_at > timezone.now() - RETENTION:
                row.state, row.operation_at = "ready", None
                row.save(update_fields=["state", "operation_at", "updated_at"])
                continue
        deleted, review = False, False
        try:
            result = _call(config, "del_join_way", {"config_id": config_id})
            deleted = isinstance(result, dict) and result.get("errcode") == 0
            if not deleted:
                logger.warning("企微群码清理失败 errcode=%s", _error_code(result.get("errcode") if isinstance(result, dict) else None))
        except WeChatClientException as exc:
            logger.warning("企微群码清理失败 errcode=%s error_type=%s", _error_code(exc.errcode), type(exc).__name__)
            review = exc.errcode == 41044
        except Exception as exc:
            logger.warning("企微群码清理失败 error_type=%s", type(exc).__name__)
        with transaction.atomic():
            current = WecomAppConfig.objects.select_for_update().filter(
                pk=config.pk, identity_revision=config.identity_revision, corp_id=config.corp_id,
                agent_id=config.agent_id, secret=config.secret, enabled=True,
            ).first()
            group = WecomGroupChat.objects.select_for_update().get(pk=group_id)
            row = WecomGroupQr.objects.select_for_update().get(pk=pk)
            if row.state != "deleting" or row.operation_id != op_id or row.config_id != config_id:
                continue
            if current is None or group.identity_revision != config.identity_revision:
                row.state = "uncertain" if deleted else "ready"
            elif deleted:
                row.config_id, row.state = "", "idle"
                _mirror(group, "")
            elif review:
                row.state = "uncertain"
            else:
                row.state = "ready"
            row.operation_at = None
            row.save(update_fields=["config_id", "state", "operation_at", "updated_at"])
    return len(candidates)
