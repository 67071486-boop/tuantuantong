"""网页端 Chatwoot 小部件的身份字段（`/api/auth/me` / `/api/auth/login` 的 `user` 载荷）。

计划：`docs/plans/2026-10-22-web-chatwoot-widget.md` §2.3 / §3（判据 1–5）。

## 本文件证什么（逐条都能失败）

| # | 判据 | 计划 |
|---|---|---|
| 1 | 三个网页客服 env 配齐 ⇒ `/me` 的 `user` 里有 `chat_identifier`、`chat_identifier_hash`、`chat_base_url`、`chat_website_token` | §2.3 |
| 2 | `chat_identifier` = `web-<当前 schema>-<用户主键>`；hash 用**独立重算**的值相等（小写 hex）；base/token 与 env 逐字一致 | §2.3 / §3-1 |
| 3 | 登录响应与 `/me` **同形**（登录成功那一刻就能挂气泡，⛔ 不用等刷新） | §2.2 |
| 4 | 三个 env **逐个缺任一** ⇒ **四个字段一个都不下发**（⛔ 退化匿名） | §3-2 |
| 5 | 跨平面同主键 ⇒ 两个不同的 `chat_identifier`（防 Chatwoot 按 identifier 合并联系人） | §3-1 |
| 6 | 响应体文本**不含 hmac 密钥**（含 `me` / `login` 两条出口） | §2.3 |

## ⚠️ 打桩打在 `django.conf.settings`

与 `tests/test_app_chat_identity.py` 同一条理由（E-0029）：`config/settings.py` 在 import
时读 `.env` 并落成模块级 setting，视图每次请求从 `django.conf.settings` 现取 ⇒
`mock.patch.dict(os.environ, …)` 打不中它。`override_settings` 替换的正是被查找的那个对象。

⚠️ `override_settings` **不能装饰 `TenantTestCase` 子类**（`setUpClass()` 没调
`super().setUpClass()`，类级补丁静默失效）⇒ 一律在 `setUp` 里 `self.enterContext(...)`。

## 夹具

- 登录走**真实端点** `/api/auth/login`（⛔ 不 `force_login`：那种会话没有 `identity_plane`）；
- 所有值都是**合成值**；hmac 密钥用**可搜索的醒目串**，否则「响应里有没有它」这条断言
  没有判别力（响应为空 / 走未配置分支时它会恒真）。
"""
import hashlib
import hmac

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings
from django_tenants.test.cases import TenantTestCase

from apps.accounts.web_chat import web_chat_identity
from apps.platform.models import Tenant

User = get_user_model()

ME_PATH = "/api/auth/me"
LOGIN_PATH = "/api/auth/login"

USERNAME = "web-chat-user"
PASSWORD = "pw-web-chat-0001"
DISPLAY_NAME = "网页端测试账号"

# ── 三个网页客服 env（合成值；hmac 密钥要"能被搜到"才证明得了"没漏"）───────────
BASE_URL = "https://chatwoot-web.example.test"
WEBSITE_TOKEN = "web-website-token-synthetic-0001"
HMAC_TOKEN = "web-hmac-secret-synthetic-please-never-leak-0001"

WEB_SETTINGS = {
    "CHATWOOT_WEB_BASE_URL": BASE_URL,
    "CHATWOOT_WEB_WEBSITE_TOKEN": WEBSITE_TOKEN,
    "CHATWOOT_WEB_HMAC_TOKEN": HMAC_TOKEN,
}

OK = 0


class WebChatIdentityPayloadTests(TenantTestCase):
    """配齐 / 逐个缺 / 跨平面 / 密钥不出服务器。"""

    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(**WEB_SETTINGS))
        self.domain_name = self.domain.domain
        self.user = User.objects.create_user(
            username=USERNAME, password=PASSWORD, first_name=DISPLAY_NAME
        )
        self.client = Client(SERVER_NAME=self.domain_name)
        self.login_response = self.login()

    # ── 工具 ────────────────────────────────────────────────────────────
    def login(self):
        response = self.client.post(
            LOGIN_PATH,
            {"username": USERNAME, "password": PASSWORD},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], OK, body)
        return response

    def me(self):
        response = self.client.get(ME_PATH)
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], OK, body)
        return response

    def user_payload(self):
        return self.me().json()["data"]["user"]

    @staticmethod
    def expected_hash(identifier: str) -> str:
        # ⚠️ 用本文件自己的合成密钥重算（⛔ 不从 settings 现取）：从 settings 取会让这条
        # 断言变成"实现自己跟自己相等"，而它要钉的是"对端按同一公式也能算出来"。
        return hmac.new(
            HMAC_TOKEN.encode(), identifier.encode(), hashlib.sha256
        ).hexdigest()

    # ── ① 配齐 ⇒ 两个字段都在 ───────────────────────────────────────────
    def test_a_configured_me_payload_carries_the_signed_identifier(self):
        user = self.user_payload()
        expected_identifier = f"web-{connection.schema_name}-{self.user.pk}"

        self.assertEqual(user["chat_identifier"], expected_identifier)
        self.assertRegex(user["chat_identifier_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            user["chat_identifier_hash"], self.expected_hash(expected_identifier)
        )
        # 公开的 SDK 配置也由后端下发（单一真值源；⛔ 前端不再有自己的构建期配置）。
        self.assertEqual(user["chat_base_url"], BASE_URL)
        self.assertEqual(user["chat_website_token"], WEBSITE_TOKEN)

    # ── ② 登录响应同形（登录即挂，不等刷新）────────────────────────────
    def test_the_login_payload_has_the_same_two_fields(self):
        user = self.login_response.json()["data"]["user"]
        expected_identifier = f"web-{connection.schema_name}-{self.user.pk}"
        self.assertEqual(user["chat_identifier"], expected_identifier)
        self.assertEqual(
            user["chat_identifier_hash"], self.expected_hash(expected_identifier)
        )
        self.assertEqual(user["chat_base_url"], BASE_URL)
        self.assertEqual(user["chat_website_token"], WEBSITE_TOKEN)

    # ── ③ 逐个缺 ⇒ 两字段都不下发 ──────────────────────────────────────
    def test_each_missing_setting_removes_both_fields(self):
        """⛔ 不许退化匿名：缺一个键也不能只下发 hash / 只下发 identifier。"""
        for key in WEB_SETTINGS:
            with self.subTest(missing=key):
                with override_settings(**{**WEB_SETTINGS, key: ""}):
                    user = self.user_payload()
                    self.assertNotIn("chat_identifier", user)
                    self.assertNotIn("chat_identifier_hash", user)
                    self.assertNotIn("chat_base_url", user)
                    self.assertNotIn("chat_website_token", user)
                    # 对照：同一份载荷仍在正常返回（否则"没有两字段"可能只是因为响应坏了）。
                    self.assertEqual(user["username"], USERNAME)

    def test_a_missing_setting_also_removes_them_from_the_login_payload(self):
        """登录是另一条出口（`LoginView` 自己调 `_user_payload`），⛔ 别只测 `/me`。"""
        with override_settings(**{**WEB_SETTINGS, "CHATWOOT_WEB_HMAC_TOKEN": ""}):
            response = self.client.post(
                LOGIN_PATH,
                {"username": USERNAME, "password": PASSWORD},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200, response.content[:300])
        user = response.json()["data"]["user"]
        self.assertNotIn("chat_identifier", user)
        self.assertNotIn("chat_identifier_hash", user)
        self.assertNotIn("chat_base_url", user)
        self.assertNotIn("chat_website_token", user)

    # ── ④ 跨平面同主键 ⇒ 两个 identifier ───────────────────────────────
    def test_the_same_primary_key_in_two_planes_gets_two_identifiers(self):
        """Chatwoot 按 `identifier` 合并联系人 ⇒ 裸主键会把两个租户的人并成一个。

        ⚠️ 这里直接调 `web_chat_identity`、并用 `connection.set_tenant` 切平面：
        本用例只钉"identifier 的平面成分真的来自连接上的当前 schema"，不值得为它
        再供给一整套租户库（`provision_tenant` 会跑全部迁移）。端到端接线由上面几条覆盖。
        """
        here = web_chat_identity(self.user)["chat_identifier"]

        other_plane = Tenant(schema_name="web_chat_other_plane", name="另一个平面")
        original = connection.tenant
        try:
            connection.set_tenant(other_plane)
            there = web_chat_identity(self.user)["chat_identifier"]
        finally:
            connection.set_tenant(original)

        self.assertEqual(
            there, f"web-web_chat_other_plane-{self.user.pk}"
        )
        self.assertNotEqual(here, there)

    # ── ⑤ 密钥不出服务器 ───────────────────────────────────────────────
    def test_no_response_contains_the_hmac_secret(self):
        me_response = self.me()
        self.assertNotIn(HMAC_TOKEN.encode(), me_response.content)
        self.assertNotIn(HMAC_TOKEN.encode(), self.login_response.content)
        # 对照：同一批响应里**确实**带着用那个密钥算出来的 hash（否则"没出现"可能只是
        # 因为响应为空 / 走的是未配置分支，断言就恒真了）。
        user = me_response.json()["data"]["user"]
        self.assertTrue(user["chat_identifier_hash"])

    def test_the_disabled_branch_does_not_leak_the_secret_either(self):
        with override_settings(**{**WEB_SETTINGS, "CHATWOOT_WEB_BASE_URL": ""}):
            response = self.me()
        self.assertNotIn(HMAC_TOKEN.encode(), response.content)
