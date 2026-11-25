"""打手 App 在线客服身份端点（`GET /api/app/chat/identity/`）的判据。

spec：`docs/specs/2026-10-21-taker-app-customer-service.md` §2 / §4（验收判据 1–4）。

## 本文件证什么（逐条都能失败）

| # | 判据 | spec |
|---|---|---|
| 0 | 用**真实登录令牌**（`/api/app/auth/login/`）走通整链（⛔ 不是手搓 / 换别的平面令牌） | §2 |
| 1 | 不带令牌 ⇒ **401**（⛔ 不是 200 / 500 / 404） | §4-1 |
| 2 | 带令牌 + 四个 env 配齐 ⇒ 200、`enabled is True`，URL 含 `app-chat.html` / `id=taker-<schema>-<主键>` / `c=app` | §4-2 |
| 3 | `h` 是 **64 位小写十六进制**，且用密钥**独立重算**得到同一个值 | §4-2 |
| 4 | 四个 env **任一为空** ⇒ 200 + `enabled is False` + `url == ""`（⛔ 不许 500） | §4-3 |
| 5 | 响应体文本**不含 hmac 密钥本身**（"密钥不出服务器"） | §4-4 |
| 6 | 昵称取 `first_name`；为空时回落登录账号（`apps.accounts.display` 的口径） | §2 |
| 7 | 昵称里的空格 / `&` 被 URL 编码，调用方按 URL 解码拿到原值 | §2（客户端不做编码） |

## ⚠️ 打桩打在哪：`django.conf.settings`，⛔ 不是 `os.environ`

`config/settings.py` 在 **import 时**用 `environ.Env.read_env()` 读一次 `.env`，把值落到
模块级 setting（`CHATWOOT_*`）。视图（`apps/accounts/app_chat_views.py`）每次请求从
`django.conf.settings` 现取 ⇒ **`mock.patch.dict(os.environ, …)` 打不中它**
（那只会改进程环境，setting 已经定值了；`E-0029`：「mock 打在**被查找的那个模块**上」）。
⇒ 本文件的 env 判据一律用 `override_settings`（Django 官方的"替换 settings 值"手法），
它替换的正是视图查找的那个对象。⚠️ 不要在用例里再套一层 `mock.patch.dict(os.environ)`：
那层补丁**不会**被任何断言证明接上了，只会让人误以为被测的是环境变量。

⚠️ **`override_settings` 只能写在 `setUp` / 方法上，⛔ 不能装饰 `TenantTestCase` 子类**：
`django_tenants.test.cases.TenantTestCase.setUpClass()` **没有**调用 `super().setUpClass()`
（实测读源码），而 Django 的类级 `override_settings` 正是在 `SimpleTestCase.setUpClass()`
里才落地的 ⇒ 装饰类**静默无效**（本文件第一次就是这么写的，表现为"配齐了却
`enabled=false`"）。⇒ 在 `setUp` 里用 `self.enterContext(...)`，由框架在收尾时自动还原。

## 夹具

- 令牌**从真路由** `/api/app/auth/login/` 换（⛔ 不手搓），与 `tests/test_app_auth.py` 同款：
  这一条是**承重**的——早先这版跑在 `/api/taker/v1/…`（OAuth 应用凭据平面）时，测试用
  `client_credentials` 换的令牌**通过**、而 App 真实携带的登录令牌**必然 401**：
  两套凭据互不通用（E-0029 同族：测试自证通过、客户端根本进不来）；
- 所有值都是**合成值**（`AGENTS.md` §5-C）；来源 IP 取文档保留段 `203.0.113.0/24`；
- hmac 密钥用一个**可搜索的醒目合成串**，这样"响应体里有没有它"这条断言才真的有判别力。
"""
import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django_tenants.test.cases import TenantTestCase

from django.db import connection


User = get_user_model()

#: 与 `apps/accounts/urls.py` 逐字一致（⚠️ **带尾斜杠**，与 `/api/app/auth/me/` 同形）。
CHAT_PATH = "/api/app/chat/identity/"
LOGIN_PATH = "/api/app/auth/login/"

# ── 合成凭据（⛔ 与任何真实账号无关）──────────────────────────────────────────
MOBILE = "13800002222"
PASSWORD = "pw-chat-123456"

#: 文档保留段 `203.0.113.0/24` 的地址（⛔ 不是任何真实客户端）。
SOURCE_IP = "203.0.113.61"

# ── Chatwoot 的四个 env（合成值；hmac 密钥要"能被搜到"才证明得了"没漏"）───────
CHATWOOT_BASE_URL = "https://chatwoot.example.test"
CHATWOOT_WEBSITE_TOKEN = "website-token-synthetic-0001"
CHATWOOT_HMAC_TOKEN = "hmac-secret-synthetic-please-never-leak-0001"
CHATWOOT_PAGE_PATH = "/app-chat.html"

CHATWOOT_SETTINGS = {
    "CHATWOOT_BASE_URL": CHATWOOT_BASE_URL,
    "CHATWOOT_WEBSITE_TOKEN": CHATWOOT_WEBSITE_TOKEN,
    "CHATWOOT_HMAC_TOKEN": CHATWOOT_HMAC_TOKEN,
    "CHATWOOT_PAGE_PATH": CHATWOOT_PAGE_PATH,
}

#: 昵称里同时带空格与 `&`（urlencode 的承重判据：不编码会把它拆成另一个查询参数）。
TAKER_NICKNAME = "测试 打手&甲"

# ── 业务码 / HTTP 码字面值（`E-0024`：写数字，让"码值被改掉"在用例里可见）──────
OK = 0
UNAUTHENTICATED = 401


class ChatIdentityTests(TenantTestCase):
    """四个 env 配齐时的正常路径 + 未配置的降级 + 密钥不出服务器。"""

    def setUp(self):
        super().setUp()
        # ⚠️ **必须在实例级进上下文**（理由见模块 docstring 最后一段）：类级
        # `@override_settings` 会被 `TenantTestCase.setUpClass()` 静默丢掉。
        self.enterContext(override_settings(**CHATWOOT_SETTINGS))
        self.domain_name = self.domain.domain
        self.anonymous = Client(SERVER_NAME=self.domain_name)
        self.taker = User.objects.create_user(
            username=MOBILE,
            password=PASSWORD,
            first_name=TAKER_NICKNAME,
        )
        self.token = self.login()

    # ── 造物 / 发请求 ───────────────────────────────────────────────────
    def login(self) -> str:
        """用**打手登录口**换一个令牌（⛔ 不手搓 AppToken）：这是 App 真实携带的那一种。"""
        response = self.anonymous.post(
            LOGIN_PATH,
            {"username": MOBILE, "password": PASSWORD},
            content_type="application/json",
            REMOTE_ADDR=SOURCE_IP,
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = json.loads(response.content)
        self.assertEqual(body["code"], OK, body)
        token = body["data"]["token"]
        self.assertTrue(token)
        return token

    def chat(self, token=None):
        headers = {} if token is None else {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        return self.anonymous.get(
            CHAT_PATH, REMOTE_ADDR=SOURCE_IP, **headers
        )

    @staticmethod
    def body(response):
        return json.loads(response.content)

    def identity(self):
        """发一次带令牌的请求，并把「200 + code=0 + 标准信封」这件事先钉住。"""
        response = self.chat(self.token)
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = self.body(response)
        self.assertEqual(payload["code"], OK, payload)
        self.assertEqual(set(payload), {"code", "msg", "data"})
        return payload["data"]

    @staticmethod
    def query(url: str) -> dict:
        return parse_qs(urlparse(url).query)

    # ── ① 无令牌 ⇒ 401 ─────────────────────────────────────────────────
    def test_without_a_token_it_is_401(self):
        """spec §4-1：未带令牌 ⇒ **401**（⛔ 不是 200 / 500 / 404）。"""
        response = self.chat()
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:300])
        payload = self.body(response)
        self.assertEqual(payload["code"], UNAUTHENTICATED, payload)

    # ── ②③ 配齐 ⇒ 200 + 可独立重算的 URL ────────────────────────────────
    def test_a_configured_endpoint_returns_the_signed_url(self):
        """spec §4-2：200、`enabled is True`，URL 含承载页 / `id=taker-<schema>-<主键>` / `c=app`。"""
        data = self.identity()
        self.assertIs(data["enabled"], True)
        self.assertIsInstance(data["url"], str)
        self.assertEqual(set(data), {"enabled", "url"})
        self.assertIn("app-chat.html", data["url"])

        params = self.query(data["url"])
        self.assertEqual(
            params["id"], [f"taker-{connection.schema_name}-{self.taker.pk}"]
        )
        self.assertEqual(params["n"], [TAKER_NICKNAME])
        self.assertEqual(params["c"], ["app"])
        # ⚠️ 顺序是 spec §2 的示例逐字（`id` / `n` / `h` / `c`）。
        self.assertEqual(
            list(params), ["id", "n", "h", "c"], data["url"]
        )

        # 前缀 = 后端拼好的 base + page path（⛔ 不是客户端自己拼）。
        self.assertTrue(
            data["url"].startswith(CHATWOOT_BASE_URL + CHATWOOT_PAGE_PATH + "?"),
            data["url"],
        )

    def test_the_hash_is_recomputable_from_the_identifier_and_the_secret(self):
        """spec §4-2：`h` 是 64 位小写十六进制，且能**独立重算**出同一个值。

        ⚠️ 重算用的是本文件自己那份合成密钥（⛔ 不从 settings 现取）：从 settings 取会让
        这条断言变成"实现自己跟自己相等"，而它要钉的是"客户端 / 对端按同一公式也能算出来"。
        """
        data = self.identity()
        digest = self.query(data["url"])["h"][0]
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        expected = hmac.new(
            CHATWOOT_HMAC_TOKEN.encode(),
            f"taker-{connection.schema_name}-{self.taker.pk}".encode(),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(digest, expected)

    # ── ④ env 缺任一项 ⇒ 200 + enabled=false ───────────────────────────
    def test_any_missing_setting_degrades_to_disabled_without_an_error(self):
        """spec §4-3：四个 env **任一为空** ⇒ 200 + `enabled is False` + `url == ""`。

        ⚠️ 必须**逐个**缺（⛔ 不是只测一个）：少配一个 key 是最常见的上线形态，
        而"只测 base 为空"会让另外三个键的闸在用例里**没覆盖**。
        """
        for key in CHATWOOT_SETTINGS:
            with self.subTest(missing=key):
                with override_settings(**{**CHATWOOT_SETTINGS, key: ""}):
                    response = self.chat(self.token)
                    self.assertEqual(response.status_code, 200, response.content[:300])
                    payload = self.body(response)
                    self.assertEqual(payload["code"], OK, payload)
                    self.assertIs(payload["data"]["enabled"], False)
                    self.assertEqual(payload["data"]["url"], "")

    def test_an_explicitly_blank_page_path_is_disabled_too(self):
        """`CHATWOOT_PAGE_PATH=`（显式空串）与"键不存在"不同：前者**不**吃默认值。

        settings 的默认值只在**键不存在**时生效（django-environ 的 `get_value`）；
        `.env` 里写 `KEY=` 得到的是空串 ⇒ 视图必须把它当"未配置"（⛔ 不是拼出
        `https://…/?id=…` 那种半开地址）。
        """
        with override_settings(**{**CHATWOOT_SETTINGS, "CHATWOOT_PAGE_PATH": ""}):
            data = self.identity()
        self.assertIs(data["enabled"], False)
        self.assertEqual(data["url"], "")

    # ── ⑤ 密钥不出服务器 ───────────────────────────────────────────────
    def test_the_response_never_contains_the_hmac_secret(self):
        """spec §4-4：响应体文本**不含** hmac 密钥本身。

        ⚠️ 断言打在**原始响应字节**上（⛔ 不是解析后的某个字段）：密钥一旦进了任何一个
        键 / URL 片段，这里都会红。
        """
        response = self.chat(self.token)
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertNotIn(CHATWOOT_HMAC_TOKEN.encode(), response.content)
        # 对照：同一份响应里**确实**带着用那个密钥算出来的 h（否则"没出现"可能只是因为
        # 响应是空的 / 走的是未配置分支，本断言就恒真了）。
        data = self.body(response)["data"]
        self.assertIs(data["enabled"], True)
        self.assertIn("h=", data["url"])

    def test_a_disabled_response_never_contains_the_hmac_secret_either(self):
        """未配置分支也不能漏密钥（⛔ 别只测"配齐"那一支）。"""
        with override_settings(**{**CHATWOOT_SETTINGS, "CHATWOOT_BASE_URL": ""}):
            response = self.chat(self.token)
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertNotIn(CHATWOOT_HMAC_TOKEN.encode(), response.content)

    # ── ⑥ 昵称口径 ─────────────────────────────────────────────────────
    def test_an_empty_nickname_falls_back_to_the_login_account(self):
        """`n` 的口径 = `apps.accounts.display.user_display_name`（昵称空 ⇒ 登录账号）。"""
        self.taker.first_name = ""
        self.taker.save(update_fields=["first_name"])
        data = self.identity()
        self.assertEqual(self.query(data["url"])["n"], [self.taker.get_username()])

    # ── ⑦ URL 编码 ─────────────────────────────────────────────────────
    def test_the_values_are_url_encoded_by_the_backend(self):
        """昵称里的空格 / `&` 必须被编码，⛔ 不能拆成另一个查询参数（客户端不做编码）。"""
        data = self.identity()
        raw_query = urlparse(data["url"]).query
        self.assertNotIn(" ", raw_query)
        self.assertNotIn(f"n={TAKER_NICKNAME}", raw_query)
        # 但解码后必须**原样**拿回昵称（⛔ 不是把值改坏）。
        self.assertEqual(self.query(data["url"])["n"], [TAKER_NICKNAME])
