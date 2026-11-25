"""「微信公众号设置」后端端点（`/api/wechatmp/config|menu`）的判据 —— 计划 T3 / S1 / S4 / S6。

## 判据打在哪

- **S1（写端点判成败）**：`menu/create` / `menu/delete` 的**失败信封**（`errcode != 0`、
  HTTP 仍 200）必须被判失败 —— 断言的是响应业务码 `1000` + 微信 errcode 出现在 msg 里；
  40001 ⇒ **强刷 token 重试一次**（判据 = `cgi-bin/token` 被调了**两次**）。
- **S4（留空 = 保持不变）**：PUT 后**库里**的 appsecret/token/aes_key 仍是旧值
  （终态判据，⛔ 不是"响应说成功"）。
- **S6（服务器 IP）**：`server.server_ip` 直通 settings `MP_SERVER_PUBLIC_IP`；
  未配置 ⇒ 空串（页面显示"向部署方查询"）。
- **零真实网络**：`WechatApiStub`（`httpx.MockTransport`）换掉 socket 层，
  URL / query / body 由被测代码自己拼。
"""
from __future__ import annotations

from unittest import mock

from django.conf import settings
from django.db import connection
from django_tenants.test.cases import TenantTestCase

from apps.wechatmp.models import WechatMpConfig
from tests._wechatmp_helpers import (
    AES_KEY,
    APPID,
    APPSECRET,
    TEMPLATE_ID,
    TOKEN,
    WechatApiStub,
)
from tests.p5_auth_helpers import make_logged_in_client

NODE = "system.pinduoduo_settings"
CONFIG = "/api/wechatmp/config/"
MENU_SYNC = "/api/wechatmp/menu/sync/"
MENU = "/api/wechatmp/menu/"

#: 微信对**读端点**的成功回包（没有 errcode 键 —— E-0087 的表格）。
TOKEN_OK = {"access_token": "token-from-stub-1", "expires_in": 7200}


class _StubBase(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.stub = WechatApiStub().install()
        self.addCleanup(self.stub.stop)
        # ⚠️ `nodes=` 只会挂到**用户所在组的 Role** 上 ⇒ 必须同时给一个组
    # （与 test_order_push 的 `groups=("客服",), nodes=(NODE,)` 同一口径）。
        self.client = make_logged_in_client(
            username="mp-settings", groups=("客服",), nodes=(NODE,)
        )

    def enable_config(self, **overrides):
        values = {
            "tenant_schema": connection.schema_name,
            "appid": APPID,
            "appsecret": APPSECRET,
            "token": TOKEN,
            "enabled": True,
        }
        values.update(overrides)
        return WechatMpConfig.objects.create(**values)


class ConfigReadTests(_StubBase):
    def test_get_returns_masked_appsecret_but_raw_token_aeskey(self):
        self.enable_config(aes_key=AES_KEY)
        body = self.client.get(CONFIG).json()
        self.assertEqual(body["code"], 0)
        data = body["data"]
        self.assertTrue(data["appsecret_masked"].startswith(APPSECRET[:4]))
        self.assertNotIn(APPSECRET, str(body))  # ⛔ 全响应任何位置都不出现原文
        self.assertEqual(data["token"], TOKEN)  # 信息卡要原样复制进公众平台
        self.assertEqual(data["aes_key"], AES_KEY)
        self.assertTrue(data["ready"])

    def test_server_info_card_has_callback_url_and_server_ip(self):
        self.enable_config()
        with mock.patch.object(settings, "MP_SERVER_PUBLIC_IP", "203.0.113.10"):
            data = self.client.get(CONFIG).json()["data"]
        self.assertEqual(data["server"]["callback_url"].rsplit("/", 1)[-1], "callback")
        self.assertEqual(data["server"]["server_ip"], "203.0.113.10")

    def test_server_ip_missing_is_empty_string_placeholder(self):
        self.enable_config()
        with mock.patch.object(settings, "MP_SERVER_PUBLIC_IP", ""):
            data = self.client.get(CONFIG).json()["data"]
        self.assertEqual(data["server"]["server_ip"], "")  # 页面显示"向部署方查询"


class ConfigWriteTests(_StubBase):
    def _payload(self, **overrides):
        data = {
            "appid": APPID,
            "appsecret": APPSECRET,
            "token": TOKEN,
            "aes_key": "",
            "mode": "plain",
            "enabled": True,
            "template_id": TEMPLATE_ID,
        }
        data.update(overrides)
        return data

    def test_first_save_creates_the_single_row(self):
        body = self.client.put(CONFIG, self._payload(), content_type="application/json").json()
        self.assertEqual(body["code"], 0)
        row = WechatMpConfig.objects.get()
        self.assertEqual(row.appsecret, APPSECRET)
        self.assertEqual(row.template_id, TEMPLATE_ID)
        self.assertTrue(body["data"]["ready"])

    def test_first_save_requires_appsecret_and_token(self):
        body = self.client.put(
            CONFIG, self._payload(appsecret="", token=""), content_type="application/json"
        ).json()
        self.assertEqual(body["code"], 1000)
        self.assertEqual(WechatMpConfig.objects.count(), 0)  # ⛔ 没写进去（终态）

    def test_blank_credentials_keep_old_values(self):
        # S4：留空 = 保持不变（掩码回写防呆）。判据 = 库里的旧值原封不动。
        self.enable_config()
        body = self.client.put(
            CONFIG,
            self._payload(appsecret="", token="", aes_key="", appid="wx9999other0000"),
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        row = WechatMpConfig.objects.get()
        self.assertEqual(row.appsecret, APPSECRET)
        self.assertEqual(row.token, TOKEN)
        self.assertEqual(row.appid, "wx9999other0000")  # appid 是普通字段：照写

    def test_safe_mode_without_aes_key_is_1000(self):
        self.enable_config()
        body = self.client.put(
            CONFIG, self._payload(mode="safe", aes_key=""), content_type="application/json"
        ).json()
        self.assertEqual(body["code"], 1000)
        self.assertIn("EncodingAESKey", body["msg"])

    def test_wrong_aes_key_length_is_1000(self):
        body = self.client.put(
            CONFIG, self._payload(mode="safe", aes_key="short"), content_type="application/json"
        ).json()
        self.assertEqual(body["code"], 1000)

    def test_without_node_is_403(self):
        # ⚠️ 必须用**别的组**：setUp 那个"客服"组的 Role 已被授予本节点（同组同 Role
        # ⇒ 同节点），同组的"无节点"用户会拿到一模一样的权限 —— 那不是 403 的形状。
        nobody = make_logged_in_client(username="mp-settings-nobody", groups=("打手",))
        self.assertEqual(nobody.get(CONFIG).status_code, 403)


class MenuSyncTests(_StubBase):
    def setUp(self):
        super().setUp()
        self.enable_config()

    def test_sync_posts_preset_menu_and_reports_success(self):
        self.stub.enqueue(dict(TOKEN_OK))
        self.stub.enqueue({"errcode": 0, "errmsg": "ok"})
        body = self.client.post(MENU_SYNC).json()
        self.assertEqual(body["code"], 0)
        # 发出去的形状（真请求，MockTransport 记录的）：
        token_calls = [r for r in self.stub.requests if "cgi-bin/token" in str(r.url)]
        self.assertEqual(len(token_calls), 1)
        create = [r for r in self.stub.requests if "cgi-bin/menu/create" in str(r.url)]
        self.assertEqual(len(create), 1)
        sent = self.stub.last_json()
        self.assertEqual([b["name"] for b in sent["button"]], ["绑定账号", "我的订单"])
        bind_url = sent["button"][0]["url"]
        self.assertTrue(bind_url.endswith("/api/wechatmp/bind-entry"))
        self.assertTrue(sent["button"][1]["url"].endswith("/m/"))

    def test_failure_envelope_must_be_judged_failure(self):
        # S1 / E-0087：errcode 40013（HTTP 200）⇒ 业务码 1000，⛔ 不许落"成功"分支。
        self.stub.enqueue(dict(TOKEN_OK))
        self.stub.enqueue({"errcode": 40013, "errmsg": "invalid appsecret"})
        body = self.client.post(MENU_SYNC).json()
        self.assertEqual(body["code"], 1000)
        self.assertIn("40013", body["msg"])

    def test_errcode_40001_forces_token_refresh_and_retries_once(self):
        # 第一轮 token → menu/create 回 40001 ⇒ 强刷（第二次 token）→ menu/create 成功。
        self.stub.enqueue(dict(TOKEN_OK))
        self.stub.enqueue({"errcode": 40001, "errmsg": "invalid credential"})
        self.stub.enqueue(dict(TOKEN_OK))
        self.stub.enqueue({"errcode": 0, "errmsg": "ok"})
        body = self.client.post(MENU_SYNC).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(self.stub.call_count_for("cgi-bin/token"), 2)

    def test_disabled_config_is_rejected_without_any_http_call(self):
        WechatMpConfig.objects.update(enabled=False)
        body = self.client.post(MENU_SYNC).json()
        self.assertEqual(body["code"], 1000)
        self.assertEqual(self.stub.requests, [])  # ⛔ 零出网


class MenuReadDeleteTests(_StubBase):
    def setUp(self):
        super().setUp()
        self.enable_config()

    def test_get_menu_returns_wechat_body(self):
        self.stub.enqueue(dict(TOKEN_OK))
        # 读端点成功回包**没有 errcode 键**（E-0087 的表）。
        self.stub.enqueue({"menu": {"button": [{"name": "绑定账号"}]}, "is_menu_open": 1})
        body = self.client.get(MENU).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["wechat"]["is_menu_open"], 1)

    def test_get_menu_failure_envelope_is_1000(self):
        self.stub.enqueue(dict(TOKEN_OK))
        self.stub.enqueue({"errcode": 46003, "errmsg": "menu no exist"})
        body = self.client.get(MENU).json()
        self.assertEqual(body["code"], 1000)
        self.assertIn("46003", body["msg"])

    def test_delete_menu_success_and_failure(self):
        self.stub.enqueue(dict(TOKEN_OK))
        self.stub.enqueue({"errcode": 0, "errmsg": "ok"})
        self.assertEqual(self.client.delete(MENU).json()["code"], 0)
        # 第一次调用后 token 已缓存（services.acquire_access_token 的 expires_at 双查）⇒
        # 第二次**不会**再取 token：只排失败信封；token 取数计数钉它没有翻倍。
        self.stub.enqueue({"errcode": 40048, "errmsg": "invalid url"})
        body = self.client.delete(MENU).json()
        self.assertEqual(body["code"], 1000)  # 失败信封必须被判失败（S1）
        self.assertIn("40048", body["msg"])
        self.assertEqual(self.stub.call_count_for("cgi-bin/token"), 1)
