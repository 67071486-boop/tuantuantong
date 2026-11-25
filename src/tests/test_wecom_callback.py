"""`/api/wecom/callback`（企微服务器回调，匿名）的判据 —— 计划 §3。

## 判据打在哪

- **终态**：`WecomInboundMessage` 的**行值**（`AGENTS.md` §5-G：断言留痕断 DB 行）；
- **响应**：状态码 + 原文体（echostr / `"success"` / 403 文本）—— 本端点是 raw 契约，
  ⛔ 不按信封断；
- **负向的"没发生"**：台账行数**不变**（签名错 / corp_id 错 / public 平面钉的是行数，
  不是日志）；
- **跨租户否定断言**：用租户 B 的凭据加密的包打到租户 A 域名必须拒——判据是
  **租户内可区分的字段**（`WecomAppConfig.tenant_schema` 的行值各不同），⛔ 不用主键指认。

## 零网络

回调本身不调企微 API ⇒ 本文件不需要 stub；签名 / 加密全部由
`tests/_wecom_helpers.py` 的**独立实现**算（与 wechatpy 两份实现，E-0024 族）。
"""
from __future__ import annotations

from unittest import mock
from urllib.parse import urlencode

from django.db import connection
from django.test import Client
from django_tenants.test.cases import TenantTestCase

from apps.wecom.models import WecomAppConfig, WecomInboundMessage, WecomGroupChat
from tests._wecom_helpers import (
    AES_KEY,
    AGENT_ID,
    CORP_ID,
    NONCE,
    SECRET,
    TIMESTAMP,
    TOKEN,
    encrypt_message,
    event_message_xml,
    query_params,
    signature,
    text_message_xml,
    wrap_encrypted_body,
)

ECHO = "wecom-echostr-synthetic-1234"
CALLBACK = "/api/wecom/callback"


class CallbackTestBase(TenantTestCase):
    """公共夹具：已启用的配置行 + 浏览器形状的 client。"""

    def setUp(self):
        super().setUp()
        WecomAppConfig.objects.create(
            tenant_schema=connection.schema_name,
            corp_id=CORP_ID,
            agent_id=AGENT_ID,
            secret=SECRET,
            callback_token=TOKEN,
            encoding_aes_key=AES_KEY,
            enabled=True,
        )
        self.client = Client(SERVER_NAME="tenant.test.com")

    def signed_encrypted_post(self, *, xml: str, corp_id: str = CORP_ID):
        encrypt = encrypt_message(aes_key=AES_KEY, corp_id=corp_id, xml=xml)
        params = query_params(timestamp=TIMESTAMP, nonce=NONCE)
        params.pop("signature")  # 企微协议走 msg_signature，公众号形状的三参 signature 不需要
        params["msg_signature"] = signature([TOKEN, TIMESTAMP, NONCE, encrypt])
        return self.client.post(
            f"{CALLBACK}?{urlencode(params)}",
            data=wrap_encrypted_body(encrypt),
            content_type="text/xml",
        )

    def signed_encrypted_get(self, *, echostr_plain: str = ECHO):
        """企微 URL 验证的 GET 形状：`msg_signature` + **密文** echostr（无公众号三参）。"""
        encrypt = encrypt_message(aes_key=AES_KEY, corp_id=CORP_ID, xml=echostr_plain)
        params = query_params(timestamp=TIMESTAMP, nonce=NONCE)
        params.pop("signature")
        params.pop("echostr", None)
        params["msg_signature"] = signature([TOKEN, TIMESTAMP, NONCE, encrypt])
        params["echostr"] = encrypt
        return self.client.get(CALLBACK, params)


class VerifyTests(CallbackTestBase):
    def test_get_verifies_and_echoes_echostr_as_plain_text(self):
        res = self.signed_encrypted_get()
        self.assertEqual(res.status_code, 200)
        # 企微要求的原样明文：无引号、无换行。
        self.assertEqual(res.content.decode(), ECHO)
        self.assertNotIn('"', res.content.decode())

    def test_get_updates_verified_at(self):
        config = WecomAppConfig.objects.get()
        self.assertIsNone(config.verified_at)
        self.signed_encrypted_get()
        config.refresh_from_db()
        self.assertIsNotNone(config.verified_at)

    def test_get_with_wrong_msg_signature_is_403_and_verified_at_untouched(self):
        encrypt = encrypt_message(aes_key=AES_KEY, corp_id=CORP_ID, xml=ECHO)
        params = query_params(timestamp=TIMESTAMP, nonce=NONCE)
        params.pop("signature")
        params["msg_signature"] = signature([TOKEN, TIMESTAMP, NONCE, "tampered-encrypt"])
        params["echostr"] = encrypt
        res = self.client.get(CALLBACK, params)
        self.assertEqual(res.status_code, 403)
        self.assertIsNone(WecomAppConfig.objects.get().verified_at)

    def test_disabled_config_is_403(self):
        WecomAppConfig.objects.update(enabled=False)
        res = self.signed_encrypted_get()
        self.assertEqual(res.status_code, 403)

    def test_unconfigured_tenant_is_403(self):
        WecomAppConfig.objects.all().delete()
        res = self.signed_encrypted_get()
        self.assertEqual(res.status_code, 403)


class InboundLedgerTests(CallbackTestBase):
    def test_group_event_is_saved_for_background_refresh(self):
        xml = event_message_xml(from_user="sys", event="change_external_chat").replace(
            "</xml>", "<ChatId>synth-group-01</ChatId><ChangeType>create</ChangeType></xml>"
        )
        res = self.signed_encrypted_post(xml=xml)
        self.assertEqual(res.status_code, 200)
        group = WecomGroupChat.objects.get(chat_id="synth-group-01")
        self.assertTrue(group.pending)
        self.assertEqual(group.event_time, 1700000000)

    def test_group_enqueue_failure_returns_500(self):
        xml = event_message_xml(from_user="sys", event="change_external_chat").replace(
            "</xml>", "<ChatId>synth-group-01</ChatId><ChangeType>create</ChangeType></xml>"
        )
        with mock.patch("apps.wecom.group_sync.queue_event", side_effect=RuntimeError("failure")):
            res = self.signed_encrypted_post(xml=xml)
        self.assertEqual(res.status_code, 500)
        self.assertFalse(WecomGroupChat.objects.exists())

    def test_text_message_is_decrypted_and_stored(self):
        xml = text_message_xml(
            from_user="userid-synthetic-a", content="测试消息甲", msg_id="msg-id-synthetic-0001"
        )
        res = self.signed_encrypted_post(xml=xml)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content.decode(), "success")
        row = WecomInboundMessage.objects.get()
        # 终态逐字段钉值（E-0024）。
        self.assertEqual(row.msg_id, "msg-id-synthetic-0001")
        self.assertEqual(row.from_user_name, "userid-synthetic-a")
        self.assertEqual(row.msg_type, "text")
        self.assertEqual(row.content, "测试消息甲")
        self.assertEqual(row.agent_id, AGENT_ID)
        self.assertEqual(row.raw_xml, xml)
        self.assertIsNone(row.event)

    def test_retry_three_times_stores_one_row(self):
        """`msg_id` 去重：企微回调重试最多 3 次 ⇒ 台账只 1 行（partial unique）。"""
        xml = text_message_xml(from_user="u1", content="重复包", msg_id="msg-id-synthetic-dup")
        for _ in range(3):
            res = self.signed_encrypted_post(xml=xml)
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.content.decode(), "success")
        self.assertEqual(WecomInboundMessage.objects.count(), 1)

    def test_event_message_without_msg_id_is_stored_each_time(self):
        """事件类消息没有 MsgId ⇒ 不去重，如实允许重复行（计划 §1.2）。"""
        xml = event_message_xml(from_user="u2", event="click")
        for _ in range(2):
            self.signed_encrypted_post(xml=xml)
        self.assertEqual(WecomInboundMessage.objects.count(), 2)
        row = WecomInboundMessage.objects.first()
        self.assertEqual(row.msg_type, "event")
        self.assertEqual(row.event, "click")
        self.assertIsNone(row.msg_id)

    def test_wrong_msg_signature_is_403_and_stores_nothing(self):
        xml = text_message_xml(from_user="u3", content="伪造包", msg_id="msg-id-synthetic-forged")
        encrypt = encrypt_message(aes_key=AES_KEY, corp_id=CORP_ID, xml=xml)
        params = query_params(timestamp=TIMESTAMP, nonce=NONCE)
        params.pop("signature")
        params["msg_signature"] = signature([TOKEN, TIMESTAMP, NONCE, "tampered"])
        res = self.client.post(
            f"{CALLBACK}?{urlencode(params)}",
            data=wrap_encrypted_body(encrypt),
            content_type="text/xml",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(WecomInboundMessage.objects.count(), 0)

    def test_store_failure_still_returns_200_success(self):
        """官方口径：只有网络失败才重试；台账落库失败记 ERROR 仍回 success（计划 §1.4）。"""
        xml = text_message_xml(from_user="u5", content="落库会炸", msg_id="msg-id-store-fail-001")
        with mock.patch.object(
            WecomInboundMessage, "save", side_effect=RuntimeError("synthetic store failure")
        ):
            res = self.signed_encrypted_post(xml=xml)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content.decode(), "success")

    def test_duplicate_integrity_error_is_swallowed_as_success(self):
        """并发重试撞 partial unique ⇒ 吞掉 IntegrityError、仍回 success（去重口径）。"""
        xml = text_message_xml(from_user="u6", content="并发重复", msg_id="msg-id-race-0001")
        # 预置同一 msg_id 的行 ⇒ 本次 INSERT 撞唯一约束。
        WecomInboundMessage.objects.create(msg_id="msg-id-race-0001", msg_type="text")
        res = self.signed_encrypted_post(xml=xml)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content.decode(), "success")
        self.assertEqual(WecomInboundMessage.objects.count(), 1)

    def test_garbage_body_is_403_not_500(self):
        params = query_params(timestamp=TIMESTAMP, nonce=NONCE)
        params.pop("signature")
        params["msg_signature"] = "0" * 40
        res = self.client.post(
            f"{CALLBACK}?{urlencode(params)}",
            data=b"this is not xml at all <<<>>>",
            content_type="text/xml",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(WecomInboundMessage.objects.count(), 0)


class CrossTenantTests(TenantTestCase):
    """跨租户否定断言（计划 §3）：租户 B 凭据加密的包打到租户 A 域名必须拒。

    ⚠️ 判据用**租户内可区分的字段**：每个租户 schema 各有一行 `WecomAppConfig`，
    `tenant_schema` 值天然不同（⛔ 不用主键指认「另一个租户的那一行」）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from apps.platform.provisioning import provision_tenant

        cls.tenant_b, _ = provision_tenant(
            schema_name="wecom_tenant_b", name="企微线租户乙", domain="wecomb-b.test.com"
        )

    def test_tenant_b_encrypted_packet_to_tenant_a_domain_is_rejected(self):
        from django_tenants.utils import tenant_context

        # 本用例自身就在租户 A 的 schema 里（TenantTestCase 的连接路由）。
        WecomAppConfig.objects.create(
            tenant_schema=connection.schema_name,
            corp_id="wwaaaasynthetic000000",
            agent_id=1000011,
            secret="tenant-a-secret-synthetic-0000000000",
            callback_token="tenant-a-token-synthetic-000",
            encoding_aes_key=AES_KEY,
            enabled=True,
        )
        # 租户 B 自己的回调配置（它自己的 corp_id + 同一把合成 AES key）。
        tenant_a_token = "tenant-a-token-synthetic-000"
        with tenant_context(self.tenant_b):
            WecomAppConfig.objects.create(
                tenant_schema=self.tenant_b.schema_name,
                corp_id="wwbbbbsynthetic000000",
                agent_id=1000022,
                secret="tenant-b-secret-synthetic-0000000000",
                callback_token=TOKEN,
                encoding_aes_key=AES_KEY,
                enabled=True,
            )
        # 用**租户 B 的 corp_id** 加密、签名用**租户 A 的 token**（A 域名上验签用的那份），
        # 打到租户 A 的域名 ⇒ 明文尾 corp_id 与 A 配置不符 ⇒ 必拒、不落 A 的台账。
        client = Client(SERVER_NAME="tenant.test.com")
        xml = text_message_xml(
            from_user="b-user", content="租户乙的消息", msg_id="msg-id-cross-tenant-0001"
        )
        encrypt = encrypt_message(aes_key=AES_KEY, corp_id="wwbbbbsynthetic000000", xml=xml)
        params = query_params(timestamp=TIMESTAMP, nonce=NONCE)
        params.pop("signature")
        params["msg_signature"] = signature([tenant_a_token, TIMESTAMP, NONCE, encrypt])
        res = client.post(
            f"{CALLBACK}?{urlencode(params)}",
            data=wrap_encrypted_body(encrypt),
            content_type="text/xml",
        )
        self.assertEqual(res.status_code, 403)
        # 终态：A 的台账 0 行（本 schema 内可区分字段 = msg_id 前缀，非主键指认）。
        self.assertEqual(
            WecomInboundMessage.objects.filter(msg_id="msg-id-cross-tenant-0001").count(), 0
        )
        # B 的台账也不该有行（包打的是 A 的域名）。
        with tenant_context(self.tenant_b):
            self.assertEqual(WecomInboundMessage.objects.count(), 0)


class PublicPlaneTests(TenantTestCase):
    """平面守卫：public 平面回 404（本 app 的表只在租户 schema，不守就是 500）。"""

    def test_callback_is_404_on_public_plane(self):
        from apps.platform.bootstrap import ensure_public_tenant

        ensure_public_tenant("admin.wecom.test.local")
        client = Client(SERVER_NAME="admin.wecom.test.local")
        res = client.get(CALLBACK, query_params(echostr=ECHO))
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["code"], 404)
