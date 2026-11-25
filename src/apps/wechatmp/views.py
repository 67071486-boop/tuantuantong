"""微信公众号集成的租户面端点：设置 / 菜单 / 微信服务器回调。

| 方法 | 路径 | 谁 | 干什么 |
|---|---|---|---|
| GET | `/api/wechatmp/config/` | 持 `system.pinduoduo_settings` 节点 | 读配置（appsecret 掩码）+ 服务器配置信息卡 |
| PUT | `/api/wechatmp/config/` | 同上 | 保存（appsecret/token/aes_key **留空 = 保持不变**，S4） |
| POST | `/api/wechatmp/menu/sync/` | 同上 | 同步预设菜单（cgi-bin/menu/create） |
| GET | `/api/wechatmp/menu/` | 同上 | 看当前菜单（cgi-bin/menu/get） |
| DELETE | `/api/wechatmp/menu/` | 同上 | 删菜单（cgi-bin/menu/delete） |
| GET/POST | `/api/wechatmp/callback` | **匿名**（微信服务器） | 验签 / 解密 / 事件分发 |

**权限节点**：复用现成的 `system.pinduoduo_settings`（计划 T6 / H7：第三方平台组现行口径
= ⛔ 不新增节点，企微群码先例）。后端与前端路由门（`routePermissions.ts`）同一个点。

**平面守卫**：本 app 的表只在租户 schema 里 ⇒ `public` 平面请求一律 404
（照 `apps/realname/views.py::_RealNameBaseView._plane_guard` 的既有形态；
不守就是 `ProgrammingError` ⇒ 500）。

**信封**：设置 / 菜单端点走 `apps.core.envelope.api_response`；**回调**回微信原生形状
（echostr / 空串 / 403 纯文本）⇒ 由 `tests/test_no_route_500.py` 的新豁免类
`WECHATMP_SHAPED_ROUTES` 接住（视图类属性 `wechatmp_raw_contract = True`）。
"""
from __future__ import annotations

import logging

from django.conf import settings as django_settings
from django.db import IntegrityError
from django.http import HttpResponse
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.plane import on_public_plane, plane_not_found_response
from apps.rbac.permissions import has_node
from apps.wechatmp import services
from apps.wechatmp.crypto import WechatCryptoError, check_signature, decrypt_message
from apps.wechatmp.models import EncryptionMode, WechatMpConfig

logger = logging.getLogger(__name__)

#: 与「应用管理」「企微群码」同一颗节点（D9 / 计划 H7：⛔ 不新增 RBAC 节点）。
WECHATMP_MANAGE_NODE = "system.pinduoduo_settings"


def mask_secret(value: str) -> str:
    """凭据的掩码展示（照 malailai `_mask` 的形状：≤8 位全星，否则前 4 后 4）。

    ⚠️ 只用于 `appsecret`（本页唯一不回原文的字段）；`token` / `aes_key` 要原样回 ——
    它们是**要复制进微信公众平台「服务器配置」**的值，藏起来设置页的信息卡就白做了。
    """
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


def public_base_url(request=None) -> str:
    """对外可达基址：优先 `PEER_SYNC_LOCAL_BASE_URL`（生产必须配 https 租户域名，
    见 `config/settings.py` 那一段的完整理由），否则回落请求自身（仅本地开发）。
    """
    configured = (django_settings.PEER_SYNC_LOCAL_BASE_URL or "").rstrip("/")
    if configured:
        return configured
    if request is not None:
        return request.build_absolute_uri("/").rstrip("/")
    return ""


def _server_info(config: WechatMpConfig, base_url: str) -> dict:
    """「服务器配置信息卡」载荷：回调 URL / Token / AESKey（一键复制的三样）+ 服务器 IP（S6）。"""
    server_ip = (getattr(django_settings, "MP_SERVER_PUBLIC_IP", "") or "").strip()
    return {
        "callback_url": f"{base_url}/api/wechatmp/callback",
        "token": config.token,
        "aes_key": config.aes_key,
        # 空串 = 部署侧没配 `MP_SERVER_PUBLIC_IP` ⇒ 页面显示"向部署方查询"占位（S6）。
        "server_ip": server_ip,
    }


# ── 序列化器（请求来源的值一律过序列化器，E-0010）────────────────────────────


class WechatMpConfigSerializer(serializers.Serializer):
    """保存配置的入参。`appsecret` / `token` / `aes_key` **留空 = 保持不变**（S4）。"""

    appid = serializers.CharField(max_length=64, allow_blank=False)
    appsecret = serializers.CharField(
        max_length=128, required=False, allow_blank=True, default=""
    )
    token = serializers.CharField(
        max_length=128, required=False, allow_blank=True, default=""
    )
    aes_key = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default=""
    )
    mode = serializers.ChoiceField(choices=EncryptionMode.choices)
    enabled = serializers.BooleanField()
    template_id = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default=""
    )

    def validate(self, attrs):
        config = services.get_config()
        first_save = config is None
        # 首次保存没有任何"旧值"可保持 ⇒ 三个凭据必须当场给全（⛔ 留空语义只对已有一行成立）。
        if first_save:
            for field in ("appsecret", "token"):
                if not (attrs.get(field) or "").strip():
                    raise serializers.ValidationError(
                        {field: "首次保存必须填写，留空保持不变只对已保存过的配置生效"}
                    )
        effective_aes_key = (attrs.get("aes_key") or "").strip() or (
            config.aes_key if config else ""
        )
        if attrs["mode"] != EncryptionMode.PLAIN and not effective_aes_key:
            raise serializers.ValidationError(
                {"aes_key": "安全 / 兼容模式必须先填写 EncodingAESKey（43 位）"}
            )
        if (attrs.get("aes_key") or "").strip() and len(attrs["aes_key"].strip()) != 43:
            raise serializers.ValidationError({"aes_key": "EncodingAESKey 必须是 43 位"})
        return attrs


class CallbackQuerySerializer(serializers.Serializer):
    """回调 URL 上的验签参数（微信「服务器配置」的固定形状）。"""

    signature = serializers.CharField(max_length=128, required=False, allow_blank=True, default="")
    timestamp = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")
    nonce = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    echostr = serializers.CharField(max_length=512, required=False, allow_blank=True, default="")
    msg_signature = serializers.CharField(max_length=128, required=False, allow_blank=True, default="")


# ── 设置 / 菜单（持节点的登录端点）──────────────────────────────────────────


class _WechatMpBaseView(APIView):
    """设置 / 菜单端点的公共装配：节点门 + 平面守卫。"""

    permission_classes = [has_node(WECHATMP_MANAGE_NODE)]

    def _plane_guard(self):
        if on_public_plane():
            return plane_not_found_response()
        return None


class WechatMpConfigView(_WechatMpBaseView):
    def get(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        config = services.ensure_config()
        return api_response(
            {
                "appid": config.appid,
                # ⛔ appsecret 是全页最贵的字符串：只回掩码（S4；token/aes_key 见 _server_info 注）。
                "appsecret_masked": mask_secret(config.appsecret),
                "token": config.token,
                "aes_key": config.aes_key,
                "mode": config.mode,
                "enabled": config.enabled,
                "template_id": config.template_id,
                "ready": config.is_ready(),
                "server": _server_info(config, public_base_url(request)),
            }
        )

    def put(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        serializer = WechatMpConfigSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        attrs = serializer.validated_data
        try:
            config = services.ensure_config()
        except IntegrityError:
            # 并发首保存撞 `tenant_schema` 唯一约束（S7 的 DB 兜底）⇒ 业务码，不是 500。
            # （评审建议4：只收 IntegrityError——DB 不可达等未预期异常照 §5-E 响亮 500。）
            return api_response(
                code=ErrorCode.VALIDATION, msg="配置行创建冲突，请刷新后重试"
            )
        # S4：三个凭据留空 = 保持不变（掩码回写防呆——malailai routes/wechatmp.py:144-146 现成语义）。
        # ⭐ M2（独立评审）：换 appid = 换了一个公众号 ⇒ 旧 access_token 作废
        # （不清的话 ≤约 2h 内推送拿旧 appid 的 token 打接口，template/send 失败只记日志）。
        appid_changed = config.appid != attrs["appid"].strip()
        config.appid = attrs["appid"].strip()
        if appid_changed:
            config.access_token = ""
            config.token_expires_at = None
        if (attrs.get("appsecret") or "").strip():
            config.appsecret = attrs["appsecret"].strip()
        if (attrs.get("token") or "").strip():
            config.token = attrs["token"].strip()
        if (attrs.get("aes_key") or "").strip():
            config.aes_key = attrs["aes_key"].strip()
        config.mode = attrs["mode"]
        config.enabled = attrs["enabled"]
        config.template_id = attrs.get("template_id", "").strip()
        config.save()
        return api_response({"ready": config.is_ready()})


class WechatMpMenuSyncView(_WechatMpBaseView):
    def post(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        config = services.get_ready_config()
        if config is None:
            return api_response(
                code=ErrorCode.VALIDATION, msg="请先填好 AppID / AppSecret / Token 并启用后再同步菜单"
            )
        menu = services.build_preset_menu(public_base_url(request))
        body, err = services.sync_preset_menu(menu)
        if err:
            # 失败信封必须被判失败（S1）：errcode != 0 一律不落"成功"分支。
            return api_response(code=ErrorCode.VALIDATION, msg=f"同步预设菜单失败：{err}")
        return api_response({"menu": menu, "wechat": body})


class WechatMpMenuView(_WechatMpBaseView):
    def get(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        config = services.get_ready_config()
        if config is None:
            return api_response(code=ErrorCode.VALIDATION, msg="公众号未启用或配置不完整")
        body, err = services.fetch_menu()
        if err:
            return api_response(code=ErrorCode.VALIDATION, msg=f"读取菜单失败：{err}")
        return api_response({"wechat": body})

    def delete(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        config = services.get_ready_config()
        if config is None:
            return api_response(code=ErrorCode.VALIDATION, msg="公众号未启用或配置不完整")
        body, err = services.delete_menu()
        if err:
            return api_response(code=ErrorCode.VALIDATION, msg=f"删除菜单失败：{err}")
        return api_response({"wechat": body})


# ── 微信服务器回调（匿名；raw 契约，进 no_route_500 的新豁免类）──────────────


class WechatMpCallbackView(APIView):
    """`GET/POST /api/wechatmp/callback` —— 微信服务器的验签与事件推送入口。

    **逐视图匿名**（先例 `xgj_push_views.py`：`authentication_classes=[]` +
    `AllowAny`；H5 订正：⛔ 不存在"集中 auth 白名单"那回事）。
    **仍只在租户面**：public 平面回 404（本 app 的表只在租户 schema）。

    判据逐条（计划 T2 / S3 / S8）：
    - 未配置或 `enabled=False` ⇒ **403**（fail-closed：微信侧显示配置失败，先去设置页配好）；
    - GET 验签过 ⇒ 原样回 `echostr`；
    - safe / compatible ⇒ **先验 `msg_signature` 再解密**（顺序承重，`crypto.decrypt_message`）；
    - 事件只翻 `WechatMpBinding.subscribed` 位；其余 / 解析失败 ⇒ 200 空串（防重试风暴）；
    - ⛔ 事件处理禁止查租户业务库（回调只碰 wechatmp 两张表）。
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]
    #: `tests/test_no_route_500.py` 的框架事实判据：本端点回微信原生形状（echostr / 空串 /
    #: 403 纯文本），不是本仓信封 —— 新豁免类按它对拍（照 `vendor_response_contract` 手法）。
    wechatmp_raw_contract = True

    def _plain(self, body: str, status: int) -> HttpResponse:
        """raw 契约的响应体：**纯文本**（echostr / 空串 / 错误短语）。

        ⚠️ 用 Django 的 `HttpResponse` 而不是 DRF 的 `Response`：后者要过渲染器协商
        （JSONRenderer 对裸字符串不认）——先例 `xgj_push_views.py` 的 `JsonResponse` 同理，
        第三方形状的端点直接回 Django 原生响应，绕开信封链路。
        """
        return HttpResponse(body, status=status, content_type="text/plain; charset=utf-8")

    def _guard_and_config(self, request):
        """公共前置：平面守卫 404 → 配置门槛 403。返回 `(None, config)` 或 `(response, None)`。"""
        if on_public_plane():
            return plane_not_found_response(), None
        config = services.get_ready_config()
        if config is None:
            # S8：未配置 / 未启用 ⇒ 403（微信侧显示"服务器配置失败"，提示先到设置页配好）。
            return self._plain("wechatmp not configured", 403), None
        return None, config

    def get(self, request):
        response, config = self._guard_and_config(request)
        if response is not None:
            return response
        serializer = CallbackQuerySerializer(data=request.query_params)
        if not serializer.is_valid():
            return self._plain("invalid signature params", 403)
        query = serializer.validated_data
        if not check_signature(config.token, query["signature"], query["timestamp"], query["nonce"]):
            return self._plain("invalid signature", 403)
        # 微信服务器配置的 URL 验证：原样回 echostr。
        return self._plain(query["echostr"], 200)

    def post(self, request):
        response, config = self._guard_and_config(request)
        if response is not None:
            return response
        serializer = CallbackQuerySerializer(data=request.query_params)
        if not serializer.is_valid():
            return self._plain("invalid signature params", 403)
        query = serializer.validated_data
        if not check_signature(config.token, query["signature"], query["timestamp"], query["nonce"]):
            return self._plain("invalid signature", 403)

        raw = request.body
        if len(raw) > services.CALLBACK_BODY_MAX_BYTES:
            # 不是微信发来的形状：按"解析失败"处理（200 空串，防重试风暴），⛔ 不 500 / 413。
            return self._plain("", 200)

        xml_text = raw.decode("utf-8", errors="replace")
        if config.mode in (EncryptionMode.SAFE, EncryptionMode.COMPATIBLE):
            if not config.aes_key:
                # 模式要求密文但密钥没配：fail-closed 403（微信侧会显示失败）。
                return self._plain("aes key missing", 403)
            encrypt = services.extract_xml_tag(xml_text, "Encrypt")
            try:
                xml_text = decrypt_message(
                    aes_key=config.aes_key,
                    appid=config.appid,
                    encrypt=encrypt,
                    msg_signature=query["msg_signature"],
                    timestamp=query["timestamp"],
                    nonce=query["nonce"],
                    token=config.token,
                )
            except WechatCryptoError:
                # ⛔ 不回 500（微信会重试风暴）；403 让微信侧显示失败、便于排障。
                logger.warning("微信公众号回调解密失败 appid=%s", config.appid)
                return self._plain("decrypt error", 403)

        event = services.extract_xml_tag(xml_text, "Event")
        from_user = services.extract_xml_tag(xml_text, "FromUserName")
        if event in ("subscribe", "unsubscribe") and from_user:
            # ⭐ 事件处理只碰 wechatmp 两张表（计划 T2：⛔ 回调线程禁止查租户业务库重活）。
            services.apply_subscribe_event(
                appid=config.appid,
                openid=from_user,
                subscribed=event == "subscribe",
            )
        # subscribe / unsubscribe / VIEW / 其余消息 / 解析失败 ⇒ 200 空串（被动不回复）。
        return self._plain("", 200)
