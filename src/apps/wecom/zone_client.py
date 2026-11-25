"""工作室自建应用的专区调用与管理员接口。"""
import json
import re
from time import monotonic

from django.conf import settings
from django.core import signing
from django.db import connection
from urllib3.fields import RequestField
from urllib3.filepost import encode_multipart_formdata

from apps.wecom import archive, sdk
from apps.wecom.models import WecomAppConfig

ZONE_ABILITY = archive.ZONE_ABILITY
SYNC_ENDPOINT = "chatdata/sync_call_program"
ASYNC_START_ENDPOINT = "chatdata/async_program_task"
ASYNC_RESULT_ENDPOINT = "chatdata/async_program_result"
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
MAX_UPLOAD_BYTES = 60 * 1024 * 1024
MAX_EXPORT_TEMPLATE_BYTES = 20 * 1024 * 1024
JOB_SIGNING_SALT = "wecom-zone-job-v1"

ZONE_OPERATIONS = frozenset({
    "sync_msg", "get_msg_list_by_page_id", "get_agree_status_single", "get_agree_status_room", "get_group_chat",
    "search_chat", "search_msg", "search_contact_or_customer",
    "create_rule", "get_rule_list", "get_rule_detail", "update_rule", "delete_rule", "get_hit_msg_list",
    "knowledge_base_list", "knowledge_base_create", "knowledge_base_detail", "knowledge_base_add_doc",
    "knowledge_base_remove_doc", "knowledge_base_modify_name", "knowledge_base_delete",
    "create_ww_model_task", "get_ww_model_result", "create_recommend_dialog_task", "get_recommend_dialog_result",
    "create_customer_tag_task", "get_customer_tag_result", "create_summary_task", "get_summary_result",
    "create_sentiment_task", "get_sentiment_result", "create_model_task", "get_model_task_result",
    "create_spam_task", "get_spam_result", "create_chatdata_export_job", "get_chatdata_export_job_status",
    "create_program_task", "get_program_task_result",
})

ADMIN_ENDPOINTS = {
    "set_public_key": "chatdata/set_public_key",
    "conversation_members": "chatdata/get_auth_user_list",
    "document_members": "docdata/get_auth_user_list",
    "set_receive_callback": "chatdata/set_receive_callback",
    "get_masking": "chatdata/get_hide_sensitiveinfo_config",
    "set_masking": "chatdata/set_hide_sensitiveinfo_config",
    "get_log_level": "chatdata/get_log_level",
    "set_log_level": "chatdata/set_log_level",
    "upload_file": "chatdata/upload_media",
    "upload_export_template": "media/upload",
    "check_debug_mode": "chatdata/check_debug_mode",
    "open_debug_mode": "chatdata/open_debug_mode",
    "close_debug_mode": "chatdata/close_debug_mode",
}
PROGRAM_ADMIN_ACTIONS = frozenset({
    "set_receive_callback", "get_log_level", "set_log_level", "check_debug_mode", "open_debug_mode", "close_debug_mode",
})
MASKING_FIELDS = frozenset({"hide_mobile", "hide_idcard", "hide_bankno"})
EXPORT_TEMPLATE_EXTENSIONS = frozenset({"doc", "docx", "xls", "xlsx", "txt"})
NOTIFY_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,128}")


class ZoneError(archive.ArchiveError):
    """专区扩展功能请求或响应异常。"""


def zone_enabled() -> bool:
    return bool(getattr(settings, "WECOM_ZONE_ENABLED", False) and archive.archive_program())


def _require_program(config: WecomAppConfig) -> str:
    if not zone_enabled() or config.tenant_schema != connection.schema_name or not config.is_ready():
        raise ZoneError("当前工作室未启用企微专区扩展")
    if not WecomAppConfig.objects.filter(
        pk=config.pk, tenant_schema=connection.schema_name, enabled=True, corp_id=config.corp_id,
        agent_id=config.agent_id, identity_revision=config.identity_revision, secret=config.secret,
    ).exists():
        raise ZoneError("企微自建应用配置已更新，请重新读取")
    return archive.archive_program()


def _check_code(code, what):
    if type(code) is not int:
        raise ZoneError(f"{what}响应格式异常")
    if code != 0:
        raise ZoneError(f"{what}失败（{code}）")


def _checked(response, what):
    _check_code(response.get("errcode") if isinstance(response, dict) else None, what)
    return response


def _post(config, endpoint, body, *, deadline=None):
    try:
        result = archive._archive_post(config, endpoint, body, deadline=deadline)
    except archive.ArchiveError as exc:
        raise ZoneError(str(exc)) from None
    return _checked(result, "企微专区请求")


def _decode_result(result):
    try:
        response = json.loads(result["response_data"])
        output = response.get("output", response)
        payload = json.loads(output["payload"])
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
        raise ZoneError("企微专区程序响应格式异常") from None
    return _checked(payload, "企微专区操作")


def _request_body(program_id, op, params):
    if not isinstance(params, dict):
        raise ZoneError("企微专区参数须为 JSON 对象")
    try:
        payload = json.dumps({"op": op, "params": params}, ensure_ascii=False, allow_nan=False)
        if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ZoneError("企微专区请求超出大小限制")
        request_data = json.dumps({"input": {"payload": payload}}, ensure_ascii=False)
    except (TypeError, ValueError, RecursionError):
        raise ZoneError("企微专区参数格式异常") from None
    return {"program_id": program_id, "ability_id": ZONE_ABILITY, "request_data": request_data}


def _operation_body(config, op, params):
    if not isinstance(op, str) or op not in ZONE_OPERATIONS:
        raise ZoneError("企微专区操作未登记")
    return _request_body(_require_program(config), op, params)


def call_zone_operation(config, op, params, *, deadline=None):
    body = _operation_body(config, op, params)
    return _decode_result(_post(config, SYNC_ENDPOINT, body, deadline=deadline))


def _job_identity(config, program_id):
    return [config.tenant_schema, config.pk, config.identity_revision, config.corp_id, config.agent_id, program_id]


def start_zone_operation(config, op, params, *, deadline=None):
    body = _operation_body(config, op, params)
    result = _post(config, ASYNC_START_ENDPOINT, body, deadline=deadline)
    jobid = _text(result.get("jobid"), "专区任务编号")
    return signing.dumps([_job_identity(config, body["program_id"]), jobid], salt=JOB_SIGNING_SALT)


def read_zone_operation(config, job_reference, *, deadline=None):
    identity = _job_identity(config, _require_program(config))
    reference = _text(job_reference, "专区任务凭据", limit=4096)
    try:
        issued_identity, jobid = signing.loads(reference, salt=JOB_SIGNING_SALT)
    except (signing.BadSignature, TypeError, ValueError):
        raise ZoneError("企微专区任务凭据异常") from None
    if issued_identity != identity:
        raise ZoneError("企微专区任务凭据与当前配置不一致")
    result = _post(config, ASYNC_RESULT_ENDPOINT, {"jobid": _text(jobid, "专区任务编号")}, deadline=deadline)
    _check_code(result.get("response_errcode"), "企微专区异步任务")
    return _decode_result(result)


def read_zone_event(config, notify_id, *, deadline=None):
    if not isinstance(notify_id, str) or not NOTIFY_ID_PATTERN.fullmatch(notify_id) or notify_id in {".", ".."}:
        raise ZoneError("企微专区通知编号异常")
    body = _request_body(_require_program(config), "event.get", {})
    body["notify_id"] = notify_id
    event = _decode_result(_post(config, SYNC_ENDPOINT, body, deadline=deadline)).get("event")
    if not isinstance(event, dict) or not isinstance(event.get("event_type"), str):
        raise ZoneError("企微专区通知数据异常")
    return event


def get_zone_document_ticket(config, code, *, expected_userid, deadline=None) -> str:
    _require_program(config)
    code = _text(code, "文件授权代码", limit=512)
    expected = _text(expected_userid, "成员编号", limit=256)
    result = _request_with_client(
        config, lambda client: client.get("auth/getuserinfo", params={"code": code}),
        "企微专区文件授权", deadline=deadline,
    )
    userid = result.get("userid") or result.get("UserId")
    if userid != expected:
        raise ZoneError("企微文件授权与当前成员不一致")
    return _text(result.get("user_doc_ticket"), "文件授权票据", limit=4096)


def _text(value, field, *, limit=1024):
    if not isinstance(value, str) or not value or not value.isprintable() or len(value.encode("utf-8")) > limit:
        raise ZoneError(f"企微{field}异常")
    return value


def _fields(params, allowed, required=()):
    if params.keys() - set(allowed) or not set(required) <= params.keys():
        raise ZoneError("企微专区管理员请求的字段异常")


def _admin_body(action, params, program_id):
    if action in PROGRAM_ADMIN_ACTIONS:
        required = {"set_log_level": {"log_level"}, "open_debug_mode": {"debug_token"}}.get(action, set())
        log_action = action in ("get_log_level", "set_log_level")
        _fields(params, required | ({"program_id"} if log_action else set()), required)
        if action == "set_log_level" and (type(params["log_level"]) is not int or params["log_level"] not in (1, 2, 3)):
            raise ZoneError("企微专区日志等级异常")
        if action == "open_debug_mode":
            _text(params["debug_token"], "专区调试凭证")
        if log_action and "program_id" in params:
            target = _text(params["program_id"], "专区日志目标", limit=128)
            approved = getattr(settings, "WECOM_ZONE_LOG_TARGETS", ())
            if target != program_id and (not isinstance(approved, (list, tuple, set, frozenset)) or target not in approved):
                raise ZoneError("该企微专区日志目标未登记")
        return {"program_id": program_id, **params}
    if action in ("conversation_members", "document_members"):
        _fields(params, {"cursor", "limit"})
        if "cursor" in params:
            _text(params["cursor"], "成员列表游标")
        if "limit" in params and (type(params["limit"]) is not int or not 1 <= params["limit"] <= 1000):
            raise ZoneError("企微专区成员查询条数异常")
        return params
    fields = {"userid", "config"} if action == "set_masking" else {"userid"}
    _fields(params, fields, fields)
    _text(params["userid"], "成员编号", limit=256)
    if action == "set_masking":
        masking = params["config"]
        if (not isinstance(masking, dict) or masking.keys() - MASKING_FIELDS
                or not all(type(value) is bool for value in masking.values())):
            raise ZoneError("企微会话组件打码设置异常")
    return params


def _upload_file(config, action, filename, data, *, deadline):
    _text(filename, "上传文件名", limit=200)
    if any(char in filename for char in ("/", "\\", '"')) or filename in {".", ".."}:
        raise ZoneError("企微上传文件名异常")
    max_bytes = MAX_UPLOAD_BYTES
    if action == "upload_export_template":
        stem, _, extension = filename.rpartition(".")
        if not stem or extension.lower() not in EXPORT_TEMPLATE_EXTENSIONS:
            raise ZoneError("企微会话导出模板需为 Word、Excel 或 TXT 文件")
        max_bytes = MAX_EXPORT_TEMPLATE_BYTES
    if not isinstance(data, bytes) or not 6 <= len(data) <= max_bytes:
        raise ZoneError(f"企微临时文件需介于 6 字节与 {max_bytes // (1024 * 1024)} MB")
    field = RequestField("media", data, filename=filename)
    field.make_multipart(content_type="application/octet-stream")
    field.headers["Content-Disposition"] += f"; filelength={len(data)}"
    body, content_type = encode_multipart_formdata([field])
    result = _request_with_client(
        config, lambda client: client.post(
            ADMIN_ENDPOINTS[action], params={"type": "file"}, data=body, headers={"Content-Type": content_type},
        ), "企微专区临时文件上传", deadline=deadline,
    )
    _text(result.get("media_id"), "专区临时文件编号")
    return result


def _request_with_client(config, send, what, *, deadline):
    client = sdk.get_client(config, auto_retry=False)
    client._http.close()
    client._http = archive._ArchiveSession(deadline if deadline is not None else monotonic() + 24)
    try:
        return _checked(sdk.retry_rejected_token(client, lambda: send(client)), what)
    except ZoneError:
        raise
    except Exception as exc:
        code = getattr(exc, "errcode", None)
        suffix = f"（{code}）" if type(code) is int else ""
        raise ZoneError(f"{what}失败{suffix}") from None
    finally:
        client._http.close()


def call_zone_admin(config, action, params=None, *, upload=None, deadline=None):
    if not isinstance(action, str) or action not in ADMIN_ENDPOINTS:
        raise ZoneError("企微专区管理员操作未登记")
    program_id = _require_program(config)
    if action == "set_public_key":
        raise ZoneError("自建应用请在企业微信管理后台的会话内容页设置公钥")
    params = {} if params is None else params
    if not isinstance(params, dict):
        raise ZoneError("企微专区参数须为 JSON 对象")
    if action in ("upload_file", "upload_export_template"):
        if params or not isinstance(upload, tuple) or len(upload) != 2:
            raise ZoneError("企微专区临时文件上传参数异常")
        return _upload_file(config, action, *upload, deadline=deadline)
    if upload is not None:
        raise ZoneError("该企微专区管理员操作不接收文件")
    return _post(config, ADMIN_ENDPOINTS[action], _admin_body(action, params, program_id), deadline=deadline)
