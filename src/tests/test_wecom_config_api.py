"""「企微自建应用」配置面端点（`/api/wecom/config|config/selfcheck|config/test_send`）的判据。

## 判据打在哪

- **掩码回显**：响应里 secret / contacts_secret **任何位置不出现原文**（终态 = 全响应串）；
- **空串不改**：PUT 后**库里**的 secret 仍是旧值（终态，⛔ 不是"响应说成功"）；
- **换 secret ⇒ token 作废**：库里的 `access_token` / `expires_at` 被清；
- **无权限节点 403**：`make_logged_in_client` 不给节点（fail-closed）；
- **selfcheck 失败信封**：mock 打在**被查找模块**（`apps.wecom.sdk.get_client`，E-0029）；
  **mock 层不抛 ≠ 成功**（E-0087）：回包没有 token ⇒ `ok=False`；errcode≠0 ⇒ 人话在
  `message` 里；
- **contacts_secret 选填**（官方现行口径）：为空 ⇒ contacts 项**照常探**（secret 的回退在
  `sdk.get_contacts_client`），失败人话带授权指引；sdk 层回退本身在
  `ContactsClientSecretFallbackTests` 单独钉；
- **test_send 判成败走业务码**：errcode≠0 ⇒ `code=1040`（HTTP 仍 200）；
- **写操作留痕**：PUT 成功 ⇒ `OperationLog` 恰好一行、action 逐字、**值不进日志**。

零真实网络：`WecomApiStub`（拦 `requests.Session`）只用于 test_send 的"真调一次"路径。
"""
from __future__ import annotations

from unittest import mock

from django.db import connection
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.wecom import sdk
from apps.wecom.models import WecomAppConfig
from tests._wecom_helpers import (
    AES_KEY,
    AGENT_ID,
    CORP_ID,
    CONTACTS_SECRET,
    SECRET,
    TOKEN,
    WecomApiStub,
)
from tests.p5_auth_helpers import make_logged_in_client

NODE = "system.wecom_settings"
CONFIG = "/api/wecom/config/"
SELFCHECK = "/api/wecom/config/selfcheck/"
TEST_SEND = "/api/wecom/config/test_send/"


class _StubBase(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.client = make_logged_in_client(
            username="wecom-settings", groups=("客服",), nodes=(NODE,)
        )

    def enable_config(self, **overrides):
        values = {
            "tenant_schema": connection.schema_name,
            "corp_id": CORP_ID,
            "agent_id": AGENT_ID,
            "secret": SECRET,
            "callback_token": TOKEN,
            "encoding_aes_key": AES_KEY,
            "enabled": True,
        }
        values.update(overrides)
        return WecomAppConfig.objects.create(**values)

    @staticmethod
    def _payload(**overrides):
        data = {
            "corp_id": CORP_ID,
            "agent_id": AGENT_ID,
            "secret": SECRET,
            "callback_token": TOKEN,
            "encoding_aes_key": AES_KEY,
            "contacts_secret": CONTACTS_SECRET,
            "enabled": True,
        }
        data.update(overrides)
        return data


class ConfigReadTests(_StubBase):
    def test_get_masks_secrets_but_raw_token_aes_key(self):
        self.enable_config(contacts_secret=CONTACTS_SECRET)
        body = self.client.get(CONFIG).json()
        self.assertEqual(body["code"], 0)
        data = body["data"]
        self.assertTrue(data["secret_masked"].startswith(SECRET[:4]))
        self.assertTrue(data["contacts_secret_masked"].startswith(CONTACTS_SECRET[:4]))
        # ⛔ 全响应任何位置都不出现原文（掩码不是"换个位置泄露"）。
        self.assertNotIn(SECRET, str(body))
        self.assertNotIn(CONTACTS_SECRET, str(body))
        # token / aes_key 原样回：它们是要复制进企微后台「接收消息」配置的值。
        self.assertEqual(data["callback_token"], TOKEN)
        self.assertEqual(data["encoding_aes_key"], AES_KEY)
        self.assertTrue(data["ready"])
        self.assertEqual(data["callback_url"].rsplit("/", 1)[-1], "callback")

    def test_get_without_row_returns_unconfigured_defaults(self):
        body = self.client.get(CONFIG).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["corp_id"], "")
        self.assertFalse(body["data"]["ready"])
        self.assertIsNone(body["data"]["verified_at"])


class ConfigWriteTests(_StubBase):
    def test_first_save_creates_the_single_row_and_logs_it(self):
        body = self.client.put(CONFIG, self._payload(), content_type="application/json").json()
        self.assertEqual(body["code"], 0)
        row = WecomAppConfig.objects.get()
        self.assertEqual(row.secret, SECRET)
        self.assertTrue(body["data"]["ready"])
        # 留痕恰好一行（断言 DB 行，E-0009）：action 逐字、操作人在、⛔ 值不进日志。
        log = OperationLog.objects.get()
        self.assertEqual(log.action, "system.wecom_config_update")
        self.assertEqual(log.field_name, "secret,callback_token,encoding_aes_key,contacts_secret")
        self.assertNotIn(SECRET, log.field_name)
        self.assertNotIn(SECRET, log.summary)

    def test_blank_secret_keeps_the_old_value(self):
        self.enable_config(contacts_secret=CONTACTS_SECRET)
        body = self.client.put(
            CONFIG,
            self._payload(secret="", contacts_secret="", agent_id=AGENT_ID),
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        row = WecomAppConfig.objects.get()
        # 终态：库里还是旧值（空串 = 不改，⛔ 不是"清掉"）。
        self.assertEqual(row.secret, SECRET)
        self.assertEqual(row.contacts_secret, CONTACTS_SECRET)

    def test_secret_change_invalidates_cached_tokens(self):
        config = self.enable_config(
            access_token="stale-token", access_token_expires_at="2030-01-01T00:00:00Z"
        )
        new_secret = "wecom-secret-synthetic-ffffffffffffffff"
        body = self.client.put(
            CONFIG, self._payload(secret=new_secret), content_type="application/json"
        ).json()
        self.assertEqual(body["code"], 0)
        config.refresh_from_db()
        self.assertEqual(config.secret, new_secret)
        self.assertEqual(config.access_token, "")
        self.assertIsNone(config.access_token_expires_at)

    def test_corp_or_agent_change_invalidates_both_token_caches_and_identity(self):
        config = self.enable_config(
            access_token="old-main", access_token_expires_at="2030-01-01T00:00:00Z",
            contacts_token="old-contacts", contacts_token_expires_at="2030-01-01T00:00:00Z",
        )
        original_revision = config.identity_revision
        for field, value in (("corp_id", "wwsynthetic-other"), ("agent_id", AGENT_ID + 1)):
            payload = self._payload(secret="", contacts_secret="")
            payload[field] = value
            body = self.client.put(CONFIG, payload, content_type="application/json").json()
            self.assertEqual(body["code"], 0, body)
            config.refresh_from_db()
            self.assertEqual((config.access_token, config.contacts_token), ("", ""))
            self.assertGreater(config.identity_revision, original_revision)
            original_revision = config.identity_revision
            config.access_token, config.contacts_token = "stale-main", "stale-contacts"
            config.save(update_fields=["access_token", "contacts_token"])

    def test_old_session_cannot_repopulate_token_after_identity_change(self):
        from apps.wecom.sdk import DBSessionStorage
        config = self.enable_config()
        session = DBSessionStorage(config, kind="main")
        WecomAppConfig.objects.filter(pk=config.pk).update(identity_revision=config.identity_revision + 1)
        with self.assertRaises(RuntimeError):
            session.set("synthetic_access_token", "stale-token")
        config.refresh_from_db()
        self.assertNotEqual(config.access_token, "stale-token")

    def test_bad_aes_key_is_rejected_and_writes_nothing(self):
        body = self.client.put(
            CONFIG,
            self._payload(encoding_aes_key="too-short"),
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 1000)
        # ⛔ 没写进去（终态）。
        self.assertEqual(WecomAppConfig.objects.count(), 0)

    def test_first_save_requires_secret_token_and_aes_key(self):
        body = self.client.put(
            CONFIG,
            self._payload(secret="", callback_token="", encoding_aes_key=""),
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 1000)
        self.assertEqual(WecomAppConfig.objects.count(), 0)


class ConfigPermissionTests(TenantTestCase):
    def test_without_node_is_403(self):
        client = make_logged_in_client(username="wecom-nobody", groups=("客服",))
        res = client.get(CONFIG)
        self.assertEqual(res.status_code, 403)

    def test_without_node_put_is_403_and_writes_nothing(self):
        client = make_logged_in_client(username="wecom-nobody-2", groups=("客服",))
        res = client.put(CONFIG, {"corp_id": "x"}, content_type="application/json")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(WecomAppConfig.objects.count(), 0)


class _FakeClient:
    """假 wechatpy 客户端：`fetch_access_token` 按构造参数决定抛/不抛；
    `session.get(access_token_key)` 回预设 token（E-0087：**mock 层不抛 ≠ 成功**，
    判据是那个 token 值本身）。"""

    def __init__(self, *, token: str = "", exc: Exception | None = None):
        self.access_token_key = "fake_access_token_key"
        self._token = token
        self._exc = exc

        class _Session:
            def get(_self, key, default=None):
                return self._token or default

        self.session = _Session()

    def fetch_access_token(self):
        if self._exc is not None:
            raise self._exc
        return {"access_token": self._token}


def _remote_error(errcode: int, errmsg: str) -> Exception:
    from wechatpy.exceptions import WeChatClientException

    return WeChatClientException(errcode, errmsg, client=None, request=None, response=None)


class SelfCheckTests(_StubBase):
    def test_not_configured_is_business_code_1039(self):
        body = self.client.post(SELFCHECK, content_type="application/json").json()
        self.assertEqual(body["code"], 1039)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_both_tokens_ok(self):
        self.enable_config(contacts_secret=CONTACTS_SECRET)
        fake_main = _FakeClient(token="token-main-ok")
        fake_contacts = _FakeClient(token="token-contacts-ok")
        with (
            mock.patch("apps.wecom.sdk.get_client", return_value=fake_main) as m_main,
            mock.patch(
                "apps.wecom.sdk.get_contacts_client", return_value=fake_contacts
            ) as m_contacts,
        ):
            body = self.client.post(SELFCHECK, content_type="application/json").json()
        self.assertEqual(body["code"], 0)
        self.assertTrue(body["data"]["main"]["ok"])
        self.assertTrue(body["data"]["contacts"]["ok"])
        # ⭐ mock 打在被查找模块上还要**真的被用到**（E-0029 的对照半边：桩没接上就是假绿）。
        m_main.assert_called_once()
        m_contacts.assert_called_once()

    def test_remote_errcode_is_translated_to_plain_chinese(self):
        self.enable_config()
        fake = _FakeClient(exc=_remote_error(60020, "api unauthorized ip"))
        with mock.patch("apps.wecom.sdk.get_client", return_value=fake):
            body = self.client.post(SELFCHECK, content_type="application/json").json()
        self.assertEqual(body["code"], 0)
        self.assertFalse(body["data"]["main"]["ok"])
        # 企微错误码翻译成人话（60020 = 可信 IP 未配），errmsg 原文也在。
        self.assertIn("60020", body["data"]["main"]["message"])
        self.assertIn("可信 IP", body["data"]["main"]["message"])

    def test_exception_without_errcode_is_unreachable_not_500(self):
        self.enable_config()
        fake = _FakeClient(exc=_remote_error(None, "connection timed out"))
        with mock.patch("apps.wecom.sdk.get_client", return_value=fake):
            body = self.client.post(SELFCHECK, content_type="application/json").json()
        self.assertEqual(body["code"], 0)
        self.assertFalse(body["data"]["main"]["ok"])
        self.assertIn("网络", body["data"]["main"]["message"])

    def test_no_token_in_success_shape_is_not_ok(self):
        """E-0087：调用不抛 ≠ 成功——回包没有 token 的"成功"必须被判失败。"""
        self.enable_config()
        fake = _FakeClient(token="")
        with mock.patch("apps.wecom.sdk.get_client", return_value=fake):
            body = self.client.post(SELFCHECK, content_type="application/json").json()
        self.assertFalse(body["data"]["main"]["ok"])
        self.assertIn("access_token", body["data"]["main"]["message"])

    def test_blank_contacts_secret_falls_back_to_main_secret_and_probes(self):
        """contacts_secret 选填：为空 ⇒ 客户联系项不再跳过，照常走 contacts 客户端
        （secret 的回退发生在 `sdk.get_contacts_client` 内部）。"""
        self.enable_config(contacts_secret="")
        fake_main = _FakeClient(token="token-main-ok")
        fake_contacts = _FakeClient(token="token-contacts-fallback-ok")
        with (
            mock.patch("apps.wecom.sdk.get_client", return_value=fake_main),
            mock.patch(
                "apps.wecom.sdk.get_contacts_client", return_value=fake_contacts
            ) as m_contacts,
        ):
            body = self.client.post(SELFCHECK, content_type="application/json").json()
        self.assertEqual(body["code"], 0)
        self.assertTrue(body["data"]["main"]["ok"])
        self.assertTrue(body["data"]["contacts"]["ok"])
        # ⭐ 桩接上且真的被用到（E-0029）；传进去的就是「contacts_secret 为空」的那行配置。
        m_contacts.assert_called_once()
        self.assertEqual(m_contacts.call_args[0][0].contacts_secret, "")

    def test_contacts_failure_message_carries_the_authorization_hint(self):
        self.enable_config(contacts_secret="")
        fake = _FakeClient(exc=_remote_error(60011, "no privilege"))
        with mock.patch("apps.wecom.sdk.get_contacts_client", return_value=fake):
            body = self.client.post(SELFCHECK, content_type="application/json").json()
        self.assertEqual(body["code"], 0)
        self.assertFalse(body["data"]["contacts"]["ok"])
        # 企微错误人话在前，授权指引在后（同一句 message 里）。
        self.assertIn("60011", body["data"]["contacts"]["message"])
        self.assertIn("客户联系→可调用接口的应用", body["data"]["contacts"]["message"])

    def test_contacts_not_enabled_is_skipped_not_failed(self):
        """只剩「没启用」这一种跳过（corp_id/secret 在视图闸已保证非空）。"""
        self.enable_config(contacts_secret="", enabled=False)
        fake = _FakeClient(token="token-main-ok")
        with mock.patch("apps.wecom.sdk.get_client", return_value=fake):
            body = self.client.post(SELFCHECK, content_type="application/json").json()
        self.assertTrue(body["data"]["contacts"]["skipped"])


class TestSendTests(_StubBase):
    def test_send_success_requires_errcode_zero(self):
        config = self.enable_config()
        self.stub = WecomApiStub().install()
        self.addCleanup(self.stub.stop)
        # wechatpy 先取 token（gettoken）再发消息（message/send）⇒ 两段回包按序排队。
        self.stub.enqueue({"access_token": "stub-token-1", "expires_in": 7200})
        self.stub.enqueue({"errcode": 0, "errmsg": "ok"})
        body = self.client.post(
            TEST_SEND,
            {"to_user": "userid-synthetic-a", "content": "测试内容甲"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        self.assertTrue(body["data"]["sent"])
        # 调用面钉住：走应用消息发送端点、agent_id 是配置行那份（E-0176：参数来自调用方载荷）。
        self.assertIn("message/send", self.stub.last_url())

    def test_second_call_reuses_cached_token_without_typeerror(self):
        """回归钉（2026-11-11 kexing 生产）：token 落库后 wechatpy 的 `expires_at`
        属性按 `*_expires_at` key 读存储；get() 曾把该 key 也回成 token 串 ⇒
        第二次调用在 `expires_at - time.time()` 抛 TypeError（自检成功、此后全崩）。
        连续两次 test_send：第二次必须走缓存 token 正常送达，且不再 gettoken。
        """
        self.enable_config()
        self.stub = WecomApiStub().install()
        self.addCleanup(self.stub.stop)
        # 第一次：gettoken + message/send；第二次只有 message/send（缓存 token 够用）。
        self.stub.enqueue({"access_token": "stub-token-1", "expires_in": 7200})
        self.stub.enqueue({"errcode": 0, "errmsg": "ok"})
        self.stub.enqueue({"errcode": 0, "errmsg": "ok"})
        payload = {"to_user": "userid-synthetic-a", "content": "测试内容甲"}
        first = self.client.post(TEST_SEND, payload, content_type="application/json").json()
        second = self.client.post(TEST_SEND, payload, content_type="application/json").json()
        self.assertEqual(first["code"], 0)
        self.assertEqual(second["code"], 0)
        self.assertTrue(second["data"]["sent"])
        self.assertEqual(self.stub.call_count_for("gettoken"), 1)
        self.assertEqual(self.stub.call_count_for("message/send"), 2)

    def test_send_remote_errcode_maps_to_business_code_1040(self):
        self.enable_config()
        self.stub = WecomApiStub().install()
        self.addCleanup(self.stub.stop)
        self.stub.enqueue({"access_token": "stub-token-1", "expires_in": 7200})
        self.stub.enqueue({"errcode": 81013, "errmsg": "invalid userid"})
        body = self.client.post(
            TEST_SEND,
            {"to_user": "no-such-user", "content": "x"},
            content_type="application/json",
        ).json()
        # 失败信封必须被判失败（E-0087）：code=1040 + 人话，HTTP 仍 200。
        self.assertEqual(body["code"], 1040)
        self.assertIn("81013", body["msg"])

    def test_send_without_config_is_business_code_1039(self):
        body = self.client.post(
            TEST_SEND,
            {"to_user": "u", "content": "x"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 1039)

    def test_send_payload_is_validated(self):
        self.enable_config()
        body = self.client.post(
            TEST_SEND, {"to_user": "", "content": ""}, content_type="application/json"
        ).json()
        self.assertEqual(body["code"], 1000)


class ContactsClientSecretFallbackTests(TenantTestCase):
    """sdk 层的回退判据：`get_contacts_client` 给 wechatpy 的 secret 取值
    （contacts_secret 为空 ⇒ 用主 secret；设了 ⇒ 以它优先）。mock 打在
    **被查找模块**（`apps.wecom.sdk.WeChatClient`，E-0029）。"""

    def _client_secret(self, *, contacts_secret: str) -> str:
        WecomAppConfig.objects.create(
            tenant_schema=connection.schema_name,
            corp_id=CORP_ID,
            agent_id=AGENT_ID,
            secret=SECRET,
            contacts_secret=contacts_secret,
            enabled=True,
        )
        config = WecomAppConfig.objects.get()
        with mock.patch("apps.wecom.sdk.WeChatClient") as m_client:
            sdk.get_contacts_client(config)
        # wechatpy 构造签名 `(corp_id, secret, …)` ⇒ 第 2 个位置参数就是生效的 secret。
        return m_client.call_args.args[1]

    def test_blank_contacts_secret_uses_the_main_secret(self):
        self.assertEqual(self._client_secret(contacts_secret=""), SECRET)

    def test_set_contacts_secret_wins_over_the_main_secret(self):
        self.assertEqual(
            self._client_secret(contacts_secret=CONTACTS_SECRET), CONTACTS_SECRET
        )
