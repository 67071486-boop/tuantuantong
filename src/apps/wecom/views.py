"""企微自建应用的租户面端点：配置 / 自检 / 测试发消息 / 通讯录与客户联系只读 proxy /
消息台账 / 企微服务器回调（D-0379 计划 §1.4）。

| 方法 | 路径 | 谁 | 干什么 |
|---|---|---|---|
| GET/PUT | `/api/wecom/config/` | 持 `system.wecom_settings` 节点 | 读配置（secret 掩码）/ 保存（secret 留空 = 不改） |
| POST | `/api/wecom/config/selfcheck/` | 同上 | 真拉两类 token，逐项 ok/错误（上线排障主入口） |
| POST | `/api/wecom/config/test_send/` | 同上 | 应用消息测试发送（errcode==0 才算成功，E-0087） |
| GET | `/api/wecom/contacts/departments/` | 同上 | proxy `department.list` |
| GET | `/api/wecom/contacts/users/` | 同上 | proxy `user/simplelist` |
| GET | `/api/wecom/messages/` | 同上 | 回调消息台账（倒序 50/页） |
| GET | `/api/wecom/external/follow_users/` | 同上 | proxy `get_follow_user_list` |
| GET | `/api/wecom/external/customers/` | 同上 | proxy `external_contact.list` + `batch_get_by_user` |
| GET | `/api/wecom/external/groupchats/` | 同上 | proxy `get_group_chat_list` / `get_group_chat_info` |
| GET/POST | `/api/wecom/callback` | **匿名**（企微服务器） | 验签 / 解密 / 台账落库 |

**平面守卫**：本 app 的表只在租户 schema ⇒ `public` 平面请求一律 404
（照 `apps/wechatmp/views.py` 的既有形态；不守就是 `ProgrammingError` ⇒ 500）。

**信封**：设置 / proxy / 台账端点走 `apps.core.envelope.api_response`；**回调**回企微
原生形状（echostr / `"success"` / 403 纯文本）⇒ 视图类属性 `wechatmp_raw_contract = True`
让 `tests/test_no_route_500.py` 的既有豁免类接住（同 `WechatMpCallbackView` 手法）。
"""
from __future__ import annotations

import binascii
import logging
import xmltodict
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.core.envelope import api_response, paginated
from apps.core.error_codes import ErrorCode
from apps.core.plane import on_public_plane, plane_not_found_response
from apps.rbac.permissions import has_node
from apps.wecom import sdk
from apps.wecom.models import WecomAppConfig, WecomInboundMessage

logger = logging.getLogger(__name__)

#: 本线全部端点的节点（自有节点，可单独授权；位点见 `apps/rbac/wiring.py` 的 `SITES`）。
WECOM_SETTINGS_NODE = "system.wecom_settings"

#: 台账读的固定页大小（计划 §1.4：倒序、50/页）。
MESSAGE_PAGE_SIZE = 50

#: 客户群列表单页条数（官方上限 100；初版固定值，不暴露分页旋钮）。
GROUP_CHAT_PAGE_LIMIT = 100

#: 「未配置」的一句人话（配置面端点共用，页面按 code 分支）。
WECOM_NOT_CONFIGURED_MSG = "企微应用还没配置或没启用：先在本页填好凭据并打开启用开关"

#: 客户联系面（proxy 三端点共用）的「未配置」人话（contacts_secret 选填后的现行口径）。
WECOM_CONTACTS_NOT_CONFIGURED_MSG = (
    "客户联系还没配置：先在企微后台「客户联系 → 可调用接口的应用」把本应用配进去"
    "（Secret 用应用 Secret 即可，本页可留空）"
)

#: 客户联系自检失败追加的一句人话（60011 这类失败多半是应用没被授权客户联系）。
WECOM_CONTACTS_AUTH_HINT = "；需在企微后台『客户联系→可调用接口的应用』授权本应用"

#: 企微高频错误码 → 人话（`selfcheck` 与 proxy 的失败翻译；官方文档核，计划 §1.4）。
ERRCODE_HINTS = {
    60020: "企业可信 IP 未配置：把本服务器出口 IP 加进企微后台「应用 → 企业可信IP」",
    40125: "secret 无效（不匹配或已重置）：到企微后台重新查看 secret 后回本页重存",
    41001: "缺少 access_token（凭据没取到）：先跑一次连通性自检",
    42001: "access_token 已过期：系统会自动刷新，请重试",
    40013: "corp_id 不匹配：核对本页 CorpID 与企微后台「我的企业」的企业 ID",
    60011: "应用没有该接口的权限：到企微后台检查应用的可见范围与接口权限",
    81013: "userids 非法：核对接收人账号（通讯录里成员的 userid）",
    81004: "通讯录权限不足：应用的可见范围没覆盖目标部门 / 成员",
}


def translate_wecom_error(exc: Exception) -> str:
    """企微侧异常 → 一句人话（errcode 优先查表，回落到官方 errmsg / 异常名）。"""
    errcode = getattr(exc, "errcode", None)
    errmsg = str(getattr(exc, "errmsg", "") or "")
    if errcode is not None and int(errcode) in ERRCODE_HINTS:
        return f"企微错误 {errcode}：{ERRCODE_HINTS[int(errcode)]}"
    if errcode is not None:
        return f"企微错误 {errcode}：{errmsg or '未给出原因'}"
    return f"网络 / 服务不可达：{errmsg or exc.__class__.__name__}"


def mask_secret(value: str) -> str:
    """凭据的掩码展示（照 `apps/wechatmp/views.py` 同名函数的形状：≤8 位全星，否则前 4 后 4）。"""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


def public_base_url(request) -> str:
    """对外可达基址：照 `apps/wechatmp/views.py` 同名函数（生产必须配 https 租户域名）。"""
    from django.conf import settings as django_settings

    configured = (django_settings.PEER_SYNC_LOCAL_BASE_URL or "").rstrip("/")
    if configured:
        return configured
    return request.build_absolute_uri("/").rstrip("/")


# ── 配置读取 ────────────────────────────────────────────────────────────────


def get_config() -> WecomAppConfig | None:
    """本租户的配置行（没有 ⇒ None）。租户上下文**只来自连接**（schema 路由）。"""
    return WecomAppConfig.objects.first()


def get_ready_config() -> WecomAppConfig | None:
    """回调 / proxy 的门槛：配置行存在**且** `is_ready()`。"""
    config = get_config()
    if config is None or not config.is_ready():
        return None
    return config


def ensure_config() -> WecomAppConfig:
    """读出配置行，没有就建默认行（保存配置端点用；单行唯一由 `tenant_schema` 约束兜底）。"""
    config = get_config()
    if config is not None:
        return config
    from django.db import connection

    return WecomAppConfig.objects.create(tenant_schema=connection.schema_name)


# ── 序列化器（请求来源的值一律过序列化器，E-0010）────────────────────────────


class WecomConfigSerializer(serializers.Serializer):
    """保存配置的入参。`secret` / `callback_token` / `encoding_aes_key` /
    `contacts_secret` **留空 = 保持不变**（照 wechatmp 的 S4 语义）。"""

    corp_id = serializers.CharField(max_length=64, allow_blank=False)
    agent_id = serializers.IntegerField(min_value=1)
    secret = serializers.CharField(
        max_length=256, required=False, allow_blank=True, default=""
    )
    callback_token = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default=""
    )
    encoding_aes_key = serializers.CharField(
        max_length=43, required=False, allow_blank=True, default=""
    )
    contacts_secret = serializers.CharField(
        max_length=256, required=False, allow_blank=True, default=""
    )
    enabled = serializers.BooleanField()

    def validate(self, attrs):
        config = get_config()
        first_save = config is None
        # 首次保存没有任何"旧值"可保持 ⇒ 凭据必须当场给全（留空语义只对已有一行成立）。
        required_on_first_save = ("secret", "callback_token", "encoding_aes_key")
        if first_save:
            for field in required_on_first_save:
                if not (attrs.get(field) or "").strip():
                    raise serializers.ValidationError(
                        {field: "首次保存必须填写，留空保持不变只对已保存过的配置生效"}
                    )
        aes_key = (attrs.get("encoding_aes_key") or "").strip() or (
            config.encoding_aes_key if config else ""
        )
        if aes_key:
            if len(aes_key) != 43:
                raise serializers.ValidationError({"encoding_aes_key": "EncodingAESKey 必须是 43 位"})
            try:
                import base64

                if len(base64.b64decode(aes_key + "=")) != 32:
                    raise serializers.ValidationError(
                        {"encoding_aes_key": "EncodingAESKey 解码后不是 32 字节"}
                    )
            except (binascii.Error, ValueError):
                raise serializers.ValidationError(
                    {"encoding_aes_key": "EncodingAESKey 不是合法的 base64 串"}
                )
        return attrs


class SelfCheckSerializer(serializers.Serializer):
    """自检入参：两类 token 都真拉一次；没有可选参数（形状占位，防裸 request.data）。"""


class TestSendSerializer(serializers.Serializer):
    """测试发消息的入参（计划 §1.4：`{to_user, content}`）。"""

    to_user = serializers.CharField(max_length=64, allow_blank=False)
    content = serializers.CharField(max_length=2048, allow_blank=False)


class DepartmentListSerializer(serializers.Serializer):
    """`department.list` 的入参（不传 = 全量组织架构）。"""

    department_id = serializers.IntegerField(required=False, min_value=1)


class UsersListSerializer(serializers.Serializer):
    """成员列表的入参：部门 id 必填（企微按部门拉成员）。"""

    department_id = serializers.IntegerField(min_value=1)


class CustomersListSerializer(serializers.Serializer):
    """客户列表的入参：成员 userid 必填（企微按成员拉他的客户）。"""

    userid = serializers.CharField(max_length=64, allow_blank=False)


class GroupChatsSerializer(serializers.Serializer):
    """客户群的入参：带 `chat_id` = 查详情；不带 = 翻列表（cursor 翻页）。"""

    chat_id = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    cursor = serializers.CharField(max_length=128, required=False, allow_blank=True, default="")


class MessagesPageSerializer(serializers.Serializer):
    """台账读的入参：页码（`?page=`）。"""

    page = serializers.IntegerField(required=False, min_value=1, default=1)


# ── 设置端点（持节点的登录端点）──────────────────────────────────────────────


class WecomSettingsGateView(APIView):
    """企微设置页全部登录端点的共同装配（权限门只在这一个类体上声明）。

    ⚠️ 子类不要再声明 `permission_classes`——那会把基类的门整条覆盖。
    """

    permission_classes = [has_node(WECOM_SETTINGS_NODE)]

    def _plane_guard(self):
        if on_public_plane():
            return plane_not_found_response()
        return None


class WecomConfigView(WecomSettingsGateView):
    def get(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        config = ensure_config()
        return api_response(
            {
                "corp_id": config.corp_id,
                "agent_id": config.agent_id,
                # ⛔ secret / contacts_secret 是全页最贵的字符串：只回掩码。
                "secret_masked": mask_secret(config.secret),
                "contacts_secret_masked": mask_secret(config.contacts_secret),
                # token / aes_key 原样回：它们是要复制进企微后台「接收消息」配置的值。
                "callback_token": config.callback_token,
                "encoding_aes_key": config.encoding_aes_key,
                "enabled": config.enabled,
                "ready": config.is_ready(),
                "contacts_ready": config.contacts_ready(),
                "verified_at": config.verified_at.isoformat() if config.verified_at else None,
                "callback_url": f"{public_base_url(request)}/api/wecom/callback",
            }
        )

    def put(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        if not isinstance(request.data, dict):
            return api_response(code=ErrorCode.VALIDATION, msg="请求体必须是 JSON 对象")
        serializer = WecomConfigSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        attrs = serializer.validated_data
        try:
            config = ensure_config()
        except IntegrityError:
            # 并发首保存撞 `tenant_schema` 唯一约束 ⇒ 业务码，不是 500（照 wechatmp 口径）。
            return api_response(code=ErrorCode.VALIDATION, msg="配置行创建冲突，请刷新后重试")
        with transaction.atomic():
            from apps.wecom.notifications import lock_send_gate
            lock_send_gate()
            config = WecomAppConfig.objects.select_for_update().get(pk=config.pk)
            if not config.enabled and attrs['enabled']:
                if config.order_status_enabled:
                    config.order_status_enabled_at = timezone.now()
                if config.payment_mismatch_enabled:
                    config.payment_mismatch_enabled_at = timezone.now()
            secret_changed = bool((attrs.get("secret") or "").strip()) and config.secret != attrs["secret"].strip()
            contacts_secret_changed = bool((attrs.get("contacts_secret") or "").strip()) and config.contacts_secret != attrs["contacts_secret"].strip()
            identity_changed = config.corp_id != attrs["corp_id"].strip() or config.agent_id != attrs["agent_id"]
            if identity_changed and (config.corp_id or config.agent_id):
                config.identity_revision += 1
            config.corp_id = attrs["corp_id"].strip()
            config.agent_id = attrs["agent_id"]
            if (attrs.get("secret") or "").strip():
                config.secret = attrs["secret"].strip()
            if (attrs.get("callback_token") or "").strip():
                config.callback_token = attrs["callback_token"].strip()
            if (attrs.get("encoding_aes_key") or "").strip():
                config.encoding_aes_key = attrs["encoding_aes_key"].strip()
            if (attrs.get("contacts_secret") or "").strip():
                config.contacts_secret = attrs["contacts_secret"].strip()
            if secret_changed or identity_changed:
                config.access_token = ""
                config.access_token_expires_at = None
            if contacts_secret_changed or identity_changed or (secret_changed and not config.contacts_secret):
                config.contacts_token = ""
                config.contacts_token_expires_at = None
            config.enabled = attrs["enabled"]
            config.save()
            # 留痕（计划 §1.4：写操作记 OperationLog，照 `system.qqbot_binding_update` 同族）。
            # ⛔ 值一概不进日志：secret 只写不读，这次动了哪几格凭据走 `field_name` 列
            # （corp_id / agent_id / enabled 由 target_name 与信封承载；列宽 64，全拼必截断）。
            changed = [
                field
                for field in ("secret", "callback_token", "encoding_aes_key", "contacts_secret")
                if (attrs.get(field) or "").strip()
            ]
            record(
                log_type=ACTION_LOG_TYPES["system.wecom_config_update"],
                action="system.wecom_config_update",
                actor=request.user.get_username(),
                actor_id=request.user.pk,
                target_kind="wecom_app_config",
                target_id=str(config.pk),
                target_name=config.corp_id,
                field_name=",".join(changed),
            )
        return api_response({"ready": config.is_ready()})


class WecomSelfCheckView(WecomSettingsGateView):
    """连通性自检：真拉一次两类 token，逐项返回 ok / 错误（上线排障主入口）。

    ⚠️ mock 层不抛 ≠ 成功（E-0087）：判据是 `fetch_access_token()` 正常返回且响应里
    有非空 token；企微非零 errcode 会抛 `WeChatClientException` ⇒ 翻译成人话。
    """

    def post(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        serializer = SelfCheckSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        config = get_config()
        if config is None or not config.corp_id or not config.secret:
            return api_response(code=ErrorCode.WECOM_NOT_CONFIGURED, msg="企微应用还没配置：先填好 CorpID 与 Secret")
        report = {"main": self._probe(config, "main")}
        contacts = self._probe(config, "contacts")
        if not contacts["ok"] and not contacts["skipped"]:
            # contacts_secret 选填（空 = 用应用 secret）：失败多半是应用没被授权客户联系，
            # 把这一句人话补在错误后面（排障主入口）。
            contacts["message"] += WECOM_CONTACTS_AUTH_HINT
        report["contacts"] = contacts
        return api_response(report)

    @staticmethod
    def _probe(config: WecomAppConfig, kind: str) -> dict:
        if kind == "contacts" and not config.contacts_ready():
            # contacts_secret 为空会回退应用 secret ⇒ 走到这里只剩「没启用」这一种情形。
            return {"ok": False, "skipped": True, "message": "企微集成没启用，客户联系自检跳过"}
        try:
            client = sdk.get_client(config) if kind == "main" else sdk.get_contacts_client(config)
            client.fetch_access_token()
        except Exception as exc:  # noqa: BLE001 —— 企微侧全部失败都要翻译成人话，⛔ 裸 500
            return {"ok": False, "skipped": False, "message": translate_wecom_error(exc)}
        token = client.session.get(client.access_token_key) or ""
        if not token:
            # 调用不抛 ≠ 成功（E-0087）：没有 token 的"成功"不是成功。
            return {"ok": False, "skipped": False, "message": "企微回包没有 access_token，无法确认成功"}
        return {"ok": True, "skipped": False, "message": "token 获取成功"}


class WecomTestSendView(WecomSettingsGateView):
    """应用消息测试发送。判成败**必须看企微返回 `errcode==0`**（E-0087）。"""

    def post(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        serializer = TestSendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        config = get_ready_config()
        if config is None:
            return api_response(code=ErrorCode.WECOM_NOT_CONFIGURED, msg=WECOM_NOT_CONFIGURED_MSG)
        attrs = serializer.validated_data
        try:
            client = sdk.get_client(config)
            resp = client.message.send_text(
                config.agent_id, [attrs["to_user"]], attrs["content"]
            )
        except Exception as exc:  # noqa: BLE001 —— 企微侧失败翻译业务码，⛔ 裸 500
            return api_response(code=ErrorCode.WECOM_REMOTE_FAILED, msg=translate_wecom_error(exc))
        if not isinstance(resp, dict) or resp.get("errcode") != 0:
            # 缺 errcode 键的回包不能被当成成功（失败信封必须被判失败）。
            errcode = resp.get("errcode") if isinstance(resp, dict) else None
            detail = translate_wecom_error(
                type("_RemoteShape", (), {"errcode": errcode, "errmsg": str(resp)})()
            )
            return api_response(code=ErrorCode.WECOM_REMOTE_FAILED, msg=detail)
        return api_response({"sent": True})


# ── 通讯录 / 客户联系 / 台账（只读面）────────────────────────────────────────


class WecomDepartmentsView(WecomSettingsGateView):
    """`GET /api/wecom/contacts/departments/` —— proxy `department.list`。"""

    def get(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        serializer = DepartmentListSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        config = get_ready_config()
        if config is None:
            return api_response(code=ErrorCode.WECOM_NOT_CONFIGURED, msg=WECOM_NOT_CONFIGURED_MSG)
        department_id = serializer.validated_data.get("department_id")
        try:
            # wechatpy 的 `department.list` 在方法体里自己 `return res["department"]`
            # （work/client/api/department.py 末行），回的就是部门列表——不是信封 dict。
            departments = sdk.get_client(config).department.list(department_id)
        except Exception as exc:  # noqa: BLE001
            return api_response(code=ErrorCode.WECOM_REMOTE_FAILED, msg=translate_wecom_error(exc))
        return api_response({"departments": departments or []})


class WecomUsersView(WecomSettingsGateView):
    """`GET /api/wecom/contacts/users/?department_id=` —— proxy `user/simplelist`。"""

    def get(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        serializer = UsersListSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        config = get_ready_config()
        if config is None:
            return api_response(code=ErrorCode.WECOM_NOT_CONFIGURED, msg=WECOM_NOT_CONFIGURED_MSG)
        department_id = serializer.validated_data["department_id"]
        try:
            userlist = sdk.get_client(config).user.list(department_id, simple=True)
        except Exception as exc:  # noqa: BLE001
            return api_response(code=ErrorCode.WECOM_REMOTE_FAILED, msg=translate_wecom_error(exc))
        return api_response({"users": userlist})


class WecomMessagesView(WecomSettingsGateView):
    """`GET /api/wecom/messages/?page=` —— 回调消息台账读（倒序、50/页）。"""

    def get(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        serializer = MessagesPageSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        page = serializer.validated_data["page"]
        qs = WecomInboundMessage.objects.order_by("-received_at")
        total = qs.count()
        rows = qs[(page - 1) * MESSAGE_PAGE_SIZE : page * MESSAGE_PAGE_SIZE]
        items = [
            {
                "id": row.pk,
                "msg_id": row.msg_id,
                "from_user_name": row.from_user_name,
                "msg_type": row.msg_type,
                "event": row.event,
                "agent_id": row.agent_id,
                "content": row.content,
                "received_at": row.received_at.isoformat(),
            }
            for row in rows
        ]
        return api_response(
            paginated(items, total=total, page=page, page_size=MESSAGE_PAGE_SIZE)
        )


class WecomFollowUsersView(WecomSettingsGateView):
    """`GET /api/wecom/external/follow_users/` —— proxy `get_follow_user_list`（客户联系面）。"""

    def get(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        config = get_config()
        if config is None or not config.contacts_ready():
            return api_response(
                code=ErrorCode.WECOM_NOT_CONFIGURED,
                msg=WECOM_CONTACTS_NOT_CONFIGURED_MSG,
            )
        try:
            body = sdk.get_contacts_client(config).external_contact.get_follow_user_list()
        except Exception as exc:  # noqa: BLE001
            return api_response(code=ErrorCode.WECOM_REMOTE_FAILED, msg=translate_wecom_error(exc))
        return api_response({"follow_users": body.get("follow_user", [])})


class WecomCustomersView(WecomSettingsGateView):
    """`GET /api/wecom/external/customers/?userid=` —— proxy `external_contact.list`
    + `batch_get_by_user`（列表 + 详情一次拿全）。"""

    def get(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        serializer = CustomersListSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        config = get_config()
        if config is None or not config.contacts_ready():
            return api_response(
                code=ErrorCode.WECOM_NOT_CONFIGURED,
                msg=WECOM_CONTACTS_NOT_CONFIGURED_MSG,
            )
        userid = serializer.validated_data["userid"]
        try:
            client = sdk.get_contacts_client(config)
            listed = client.external_contact.list(userid)
            userids = listed.get("external_userid", []) or []
            details = client.external_contact.batch_get_by_user(userid)
        except Exception as exc:  # noqa: BLE001
            return api_response(code=ErrorCode.WECOM_REMOTE_FAILED, msg=translate_wecom_error(exc))
        detail_list = (details.get("external_contact_list") or []) if isinstance(details, dict) else []
        return api_response({"external_userids": userids, "contacts": detail_list})


class WecomGroupChatsView(WecomSettingsGateView):
    """`GET /api/wecom/external/groupchats/` —— proxy `get_group_chat_list`（带 `chat_id`
    则 `get_group_chat_info`）。"""

    def get(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        serializer = GroupChatsSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        config = get_config()
        if config is None or not config.contacts_ready():
            return api_response(
                code=ErrorCode.WECOM_NOT_CONFIGURED,
                msg=WECOM_CONTACTS_NOT_CONFIGURED_MSG,
            )
        chat_id = serializer.validated_data["chat_id"]
        try:
            client = sdk.get_contacts_client(config)
            if chat_id:
                body = client.external_contact.get_group_chat_info(chat_id)
                return api_response({"group_chat": body.get("group_chat")})
            body = client.external_contact.get_group_chat_list(
                GROUP_CHAT_PAGE_LIMIT, cursor=serializer.validated_data["cursor"] or None
            )
        except Exception as exc:  # noqa: BLE001
            return api_response(code=ErrorCode.WECOM_REMOTE_FAILED, msg=translate_wecom_error(exc))
        return api_response(
            {
                "group_chat_list": body.get("group_chat_list", []) or [],
                "next_cursor": body.get("next_cursor") or "",
            }
        )


# ── 企微服务器回调（匿名；raw 契约）──────────────────────────────────────────


class WecomCallbackView(APIView):
    """`GET/POST /api/wecom/callback` —— 企微服务器的验签与消息推送入口。

    **逐视图匿名**（照 `WechatMpCallbackView`：`authentication_classes=[]` + `AllowAny`）。
    **仍只在租户面**：public 平面回 404（本 app 的表只在租户 schema）。

    - GET：`check_signature` + 解 `echostr`，原样回明文；成功更新 `verified_at`；
    - POST：解密 → 解析 → 台账落库 → 立即回 `"success"`；解密失败 403；
    - ⛔ 落库失败记 ERROR 日志仍回 200：官方口径只有网络失败才重试，业务失败重试无益
      （台账非资金数据）。
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]
    #: `tests/test_no_route_500.py` 的框架事实判据：本端点回企微原生形状（echostr /
    #: `"success"` / 403 纯文本），不是本仓信封 —— 豁免类按它对拍。
    wechatmp_raw_contract = True

    def _plain(self, body: str, status: int) -> HttpResponse:
        """raw 契约的响应体：**纯文本**（照 `WechatMpCallbackView._plain`）。"""
        return HttpResponse(body, status=status, content_type="text/plain; charset=utf-8")

    def _usable_crypto(self, config):
        """配置齐 + aes_key 可用才构造 crypto（wechatpy 对坏 key 抛裸异常 ⇒ 500）。"""
        if config is None or not config.is_ready() or not config.callback_token:
            return None
        if len(config.encoding_aes_key) != 43:
            return None
        try:
            return sdk.build_crypto(config)
        except (binascii.Error, ValueError, AssertionError):
            return None

    def get(self, request):
        if on_public_plane():
            return plane_not_found_response()
        config = get_config()
        crypto = self._usable_crypto(config)
        if crypto is None:
            return self._plain("wecom not configured", 403)
        query = request.query_params
        try:
            # 企微的 URL 验证协议：GET 带 `msg_signature` + **密文** echostr，
            # `check_signature` 验签 + 解密一步完成、回**明文**（与公众号的三参 signature 不同）。
            echo = crypto.check_signature(
                query.get("msg_signature", ""),
                query.get("timestamp", ""),
                query.get("nonce", ""),
                query.get("echostr", ""),
            )
        except Exception:  # noqa: BLE001 —— 验签 / 解密失败对企微侧统一 403
            return self._plain("invalid signature", 403)
        WecomAppConfig.objects.filter(pk=config.pk).update(verified_at=timezone.now())
        return self._plain(str(echo), 200)

    def post(self, request):
        if on_public_plane():
            return plane_not_found_response()
        config = get_config()
        crypto = self._usable_crypto(config)
        if crypto is None:
            return self._plain("wecom not configured", 403)
        query = request.query_params
        try:
            xml_text = crypto.decrypt_message(
                request.body,
                query.get("msg_signature", ""),
                query.get("timestamp", ""),
                query.get("nonce", ""),
            )
        except Exception:  # noqa: BLE001 —— 解密失败 403（企微侧会显示失败，便于排障）
            logger.warning("企微回调解密失败 corp_id=%s", config.corp_id)
            return self._plain("decrypt error", 403)
        try:
            from apps.wecom.group_sync import queue_event

            raw = xmltodict.parse(xml_text)["xml"]
            queue_event(raw, identity_revision=config.identity_revision)
        except Exception:  # noqa: BLE001
            logger.exception("企微群事件入队失败 corp_id=%s", config.corp_id)
            return self._plain("group event enqueue error", 500)
        try:
            from apps.wecom.archive import queue_notification
            from apps.wecom.zone_events import queue_notification as queue_zone_notification

            queue_notification(config, raw)
            queue_zone_notification(config, raw)
        except Exception:
            logger.exception("企微会话通知入队失败")
            return self._plain("archive event enqueue error", 500)
        try:
            self._store(config, xml_text)
        except Exception:  # noqa: BLE001 —— 台账非资金数据：落库失败只记日志，仍回 200
            logger.exception("企微回调落库失败 corp_id=%s", config.corp_id)
        return self._plain("success", 200)

    @staticmethod
    def _store(config, xml_text: str) -> None:
        """解密后的 XML → 台账行。`msg_id` 非空时靠 partial unique 去重（重试最多 3 次）。"""
        raw = xmltodict.parse(xml_text)["xml"]
        msg_id = str(raw.get("MsgId") or "") or None
        agent_id = raw.get("AgentID")
        try:
            agent_id = int(agent_id)
        except (TypeError, ValueError):
            agent_id = None
        msg_type = str(raw.get("MsgType") or "")
        row = WecomInboundMessage(
            msg_id=msg_id,
            from_user_name=str(raw.get("FromUserName") or ""),
            msg_type=msg_type,
            event=str(raw.get("Event")) if msg_type == "event" and raw.get("Event") else None,
            agent_id=agent_id,
            content=str(raw.get("Content") or ""),
            raw_xml=xml_text,
        )
        try:
            with transaction.atomic():
                row.save()
        except IntegrityError:
            # 重试的重复包（msg_id 已在）：如实吞掉，仍回 success。
            logger.info("企微回调重复 msg_id=%s 已忽略", msg_id)
