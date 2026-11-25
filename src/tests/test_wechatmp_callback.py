"""`/api/wechatmp/callback`（微信服务器回调，匿名）的判据 —— 计划 T2 / S3 / S8。

## 判据打在哪

- **终态**：`WechatMpBinding.subscribed` 的**行值**（`AGENTS.md` §5-G：断言留痕断 DB 行）；
- **响应**：状态码 + 原文体（echostr / 空串 / 403 文本）—— 本端点是 raw 契约，
  ⛔ 不按信封断；
- **负向的"没发生"**：绑定行数**不变**（"未绑定用户关注不建行"钉的是行数，
  不是日志）。

## 零网络

回调本身不调微信 API（token 在配置行里）⇒ 本文件不需要 stub；签名全部由
`tests/_wechatmp_helpers.py` 的**独立实现**算（与被测代码两份实现，E-0024 族）。
"""
from __future__ import annotations

from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django_tenants.test.cases import TenantTestCase

from apps.wechatmp.models import WechatMpBinding, WechatMpConfig
from tests._wechatmp_helpers import (
    AES_KEY,
    APPID,
    APPSECRET,
    NONCE,
    TOKEN,
    TIMESTAMP,
    encrypt_message,
    event_xml,
    query_params,
    sha1_signature,
    wrap_encrypted_body,
)

ECHO = "echostr-synthetic-1234"
OPENID_BOUND = "openid-synthetic-aaaabbbbcccc"
OPENID_STRANGER = "openid-synthetic-ddddeeeeffff"

CALLBACK = "/api/wechatmp/callback"


class CallbackTestBase(TenantTestCase):
    """公共夹具：配置行 + 一个已绑定用户 + 浏览器形状的 client。"""

    def setUp(self):
        super().setUp()
        WechatMpConfig.objects.create(
            tenant_schema=connection.schema_name,
            appid=APPID,
            appsecret=APPSECRET,
            token=TOKEN,
            aes_key=AES_KEY,
            mode="plain",
            enabled=True,
        )
        self.user = get_user_model().objects.create_user(
            username="mp-callback-user", password="pw-mp-callback"
        )
        self.binding = WechatMpBinding.objects.create(
            user=self.user, appid=APPID, openid=OPENID_BOUND, subscribed=True
        )
        self.client = Client(SERVER_NAME="tenant.test.com")

    # ── 请求助手 ──────────────────────────────────────────────────────────

    def signed_get(self, **extra):
        return self.client.get(CALLBACK, query_params(TOKEN, **extra))

    def signed_post(self, body: bytes, *, mode_params: dict | None = None):
        from urllib.parse import urlencode

        params = query_params(TOKEN, timestamp=TIMESTAMP, nonce=NONCE)
        params.update(mode_params or {})
        # ⚠️ Django test client 的 `post(path, data, …)` 第二个位置参数就是 `data`：
        # query 参数拼进 URL、body 走 `data=`（raw bytes）。
        return self.client.post(
            f"{CALLBACK}?{urlencode(params)}",
            data=body,
            content_type="text/xml",
        )


class UnconfiguredTests(TenantTestCase):
    """S8：没配置 / 没启用 ⇒ 403（fail-closed，微信侧显示配置失败）。"""

    def setUp(self):
        super().setUp()
        self.client = Client(SERVER_NAME="tenant.test.com")

    def test_get_without_config_is_403(self):
        res = self.client.get(CALLBACK, query_params(TOKEN, echostr=ECHO))
        self.assertEqual(res.status_code, 403)

    def test_post_without_config_is_403(self):
        res = self.client.post(
            f"{CALLBACK}?{urlencode(query_params(TOKEN))}",
            data=b"<xml/>",
            content_type="text/xml",
        )
        self.assertEqual(res.status_code, 403)

    def test_disabled_config_is_403(self):
        # 配置行存在但 enabled=False：同样 403（启用是回调 / 菜单 / 推送的共同门槛）。
        WechatMpConfig.objects.create(
            tenant_schema=connection.schema_name,
            appid=APPID,
            appsecret=APPSECRET,
            token=TOKEN,
            enabled=False,
        )
        res = self.client.get(CALLBACK, query_params(TOKEN, echostr=ECHO))
        self.assertEqual(res.status_code, 403)


class CallbackOnPublicPlaneTests(TenantTestCase):
    """平面守卫：public 平面回 404（本 app 的表只在租户 schema，不守就是 500）。"""

    def test_callback_is_404_on_public_plane(self):
        from apps.platform.bootstrap import ensure_public_tenant

        ensure_public_tenant("admin.test.local")
        client = Client(SERVER_NAME="admin.test.local")
        res = client.get(CALLBACK, query_params(TOKEN, echostr=ECHO))
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["code"], 404)


class CallbackVerifyTests(CallbackTestBase):
    def test_get_verifies_and_echoes_echostr(self):
        res = self.signed_get(echostr=ECHO)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content.decode(), ECHO)

    def test_get_with_wrong_signature_is_403(self):
        params = query_params(TOKEN, echostr=ECHO)
        params["signature"] = sha1_signature([TOKEN, TIMESTAMP, "other"])
        res = self.client.get(CALLBACK, params)
        self.assertEqual(res.status_code, 403)

    def test_get_with_missing_params_is_403_not_500(self):
        # 畸形 query（缺 timestamp）：走序列化器默认值 ⇒ 验签不过 ⇒ 403（no_route_500 那条腿的形状）。
        res = self.client.get(CALLBACK, {"signature": "x", "echostr": ECHO})
        self.assertEqual(res.status_code, 403)

    def test_post_with_wrong_signature_is_403(self):
        params = query_params(TOKEN)
        params["signature"] = "0" * 40
        res = self.client.post(
            f"{CALLBACK}?{urlencode(params)}", data=b"<xml/>", content_type="text/xml"
        )
        self.assertEqual(res.status_code, 403)


class CallbackEventTests(CallbackTestBase):
    def test_subscribe_flips_binding_subscribed_to_true(self):
        # 起点 False（用户先取关过）：事件到达 ⇒ 行值翻回 True（终态判据，不是"调了函数"）。
        WechatMpBinding.objects.filter(pk=self.binding.pk).update(subscribed=False)
        body = event_xml(event="subscribe", openid=OPENID_BOUND).encode()
        res = self.signed_post(body)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content, b"")  # 被动不回复（空串）
        self.binding.refresh_from_db()
        self.assertTrue(self.binding.subscribed)

    def test_unsubscribe_flips_binding_subscribed_to_false(self):
        body = event_xml(event="unsubscribe", openid=OPENID_BOUND).encode()
        res = self.signed_post(body)
        self.assertEqual(res.status_code, 200)
        self.binding.refresh_from_db()
        self.assertFalse(self.binding.subscribed)

    def test_subscribe_from_unbound_openid_creates_no_row(self):
        # v1 口径：未绑定用户关注只记日志不建行（判据 = 行数不变，⛔ 不是"没报错"）。
        before = WechatMpBinding.objects.count()
        body = event_xml(event="subscribe", openid=OPENID_STRANGER).encode()
        res = self.signed_post(body)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(WechatMpBinding.objects.count(), before)

    def test_view_event_is_200_empty_and_touches_nothing(self):
        body = event_xml(event="VIEW", openid=OPENID_BOUND).replace(
            "<MsgType><![CDATA[event]]></MsgType>", "<MsgType><![CDATA[event]]></MsgType>"
        ).encode()
        before = WechatMpBinding.objects.count()
        res = self.signed_post(body)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content, b"")
        self.assertEqual(WechatMpBinding.objects.count(), before)
        self.binding.refresh_from_db()
        self.assertTrue(self.binding.subscribed)  # VIEW 不翻 subscribed 位

    def test_garbage_body_is_200_empty(self):
        # 解析失败 ⇒ 200 空串（防微信重试风暴），⛔ 不是 400 / 500。
        res = self.signed_post(b"this is not xml at all <<<>>>")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content, b"")

    def test_oversized_body_is_200_empty(self):
        res = self.signed_post(b"x" * (64 * 1024 + 1))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content, b"")


class SafeModeCallbackTests(CallbackTestBase):
    """safe / compatible：先验 `msg_signature` 再解密（S3）。"""

    def _enable(self, mode: str) -> None:
        WechatMpConfig.objects.update(mode=mode)

    def _signed_encrypted_post(self, *, xml: str, sig_over: str | None = None):
        encrypt = encrypt_message(aes_key=AES_KEY, appid=APPID, xml=xml)
        msg_signature = sig_over or sha1_signature([TOKEN, TIMESTAMP, NONCE, encrypt])
        return self.signed_post(
            wrap_encrypted_body(encrypt),
            mode_params={"msg_signature": msg_signature},
        )

    def test_safe_mode_subscribe_is_applied(self):
        self._enable("safe")
        res = self._signed_encrypted_post(xml=event_xml(event="subscribe", openid=OPENID_BOUND))
        self.assertEqual(res.status_code, 200)
        self.binding.refresh_from_db()
        self.assertTrue(self.binding.subscribed)

    def test_compatible_mode_subscribe_is_applied(self):
        self._enable("compatible")
        res = self._signed_encrypted_post(xml=event_xml(event="unsubscribe", openid=OPENID_BOUND))
        self.assertEqual(res.status_code, 200)
        self.binding.refresh_from_db()
        self.assertFalse(self.binding.subscribed)

    def test_safe_mode_with_wrong_msg_signature_is_403_and_not_applied(self):
        # ⛔ 双重判据：403 **且** subscribed 没被翻（"拒了就真没发生"）。
        self._enable("safe")
        xml = event_xml(event="unsubscribe", openid=OPENID_BOUND)
        encrypt = encrypt_message(aes_key=AES_KEY, appid=APPID, xml=xml)
        wrong_sig = sha1_signature([TOKEN, TIMESTAMP, NONCE, "tampered-encrypt"])
        res = self._signed_encrypted_post(xml=xml, sig_over=wrong_sig)
        self.assertEqual(res.status_code, 403)
        self.binding.refresh_from_db()
        self.assertTrue(self.binding.subscribed)

    def test_safe_mode_without_aes_key_is_403(self):
        self._enable("safe")
        WechatMpConfig.objects.update(aes_key="")
        res = self._signed_encrypted_post(xml=event_xml(event="subscribe", openid=OPENID_BOUND))
        self.assertEqual(res.status_code, 403)
