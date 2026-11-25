"""开放平台 v2 T2：取令牌端点 / Bearer 认证 / 令牌纯函数。

判据来源 = `docs/specs/2026-10-08-open-platform-v2-parity-design.md` §5.1–§5.4 与 §12。
三档各自独立：**纯函数**那一档是 `SimpleTestCase`（⛔ 不建库 ⇒ 它同时证明令牌的形状 /
过期 / 字段判据一点都不依赖数据库），端点与认证那两档要 `TenantTestCase`（要 `ApiApp` 行）。

## 这一份证明什么、⛔ 不证明什么

- **证明**：换令牌的正常与失败形状（含**键集恰好**）、三档凭据失败的文案**逐字相同**、
  畸形入参不 500 也不是本仓信封、令牌改一个字符 / 过期一秒就废、轮换密钥与停用**立刻**生效、
  IP 白名单与按来源 IP 的失败闸仍然走**同一份**既有判据、`7200` 这个数只有一处出处。
- ⛔ **不证明**（如实登记）：
  1. **跨进程一致**（"两个 worker 都能验通同一个令牌"）——本条不给这条结论：本文件全在
     一个进程里跑（§12 那条判据由总指挥在 T2 之后用多进程对照单独跑）。
     本文件里那条"cache 打桩成碰就抛"的用例只是**进程内**的代理判据，见它的 docstring。
  2. **业务端点已切到 Bearer**——本模块**不切换**业务端点的认证
     ⇒ 文件里那条用例断言的恰恰是"Bearer 令牌打业务端点**仍然** 401"。
  3. **限流**（429）——那是 T3 的 `rate_limit`。
"""
from __future__ import annotations

import ast
import base64
import json
import pathlib
import time
import tokenize
from unittest import mock

import django.core.cache as django_cache_module
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db import connection
from django.test import Client, SimpleTestCase
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIRequestFactory

from apps.openapi import authentication as auth_module
from apps.openapi import tokens
from apps.openapi.authentication import (
    BEARER_EXPIRED_MSG,
    BEARER_INVALID_CREDENTIALS_MSG,
    BEARER_SCHEME,
    IP_FAILURE_MAX,
    IP_NOT_ALLOWED_MSG,
    ApiAppBearerAuthentication,
)
from apps.openapi.api.token_views import (
    ERROR_INVALID_CLIENT,
    ERROR_UNSUPPORTED_GRANT_TYPE,
    GRANT_TYPE_CLIENT_CREDENTIALS,
    UNSUPPORTED_GRANT_TYPE_DESCRIPTION,
)
from apps.openapi.models import ApiApp, ApiAppState
from apps.platform.provisioning import provision_tenant

User = get_user_model()

#: 契约路径（与 `apps/openapi/urls.py` 逐字一致：**不带**尾斜杠）。
TOKEN_PATH = "/api/open/oauth/token"
#: 订单集合路径。认证是 Bearer。
BUSINESS_PATH = "/api/open/v1/orders/"
#: 订单详情路径。认证也是 Bearer。令牌打上去不再是「缺少签名参数」那种 401。
DETAIL_PATH = "/api/open/v1/orders/20261007120000000001/"

#: 合成凭据（`AGENTS.md` §5-C：夹具一律合成值，⛔ 不是任何真实租户的凭据）。
APP_NAME = "令牌探针应用甲"
APP_KEY = "probe-token-key-000001"
APP_SECRET = "probe-token-secret-0000000000001"
DISABLED_NAME = "令牌探针应用乙"
DISABLED_KEY = "probe-token-key-000002"
DISABLED_SECRET = "probe-token-secret-0000000000002"
#: ⛔ 一定不存在的 `client_id` 与一定不对的 secret（§5.4 要证明"应用不存在"与"密钥错"同形）。
UNKNOWN_KEY = "probe-token-key-0000ff"
WRONG_SECRET = "probe-token-secret-00000000000ff"

#: 来源 IP（文档保留段 203.0.113.0/24，⛔ 不是任何真实客户端）。
SOURCE_IP = "203.0.113.31"
OTHER_SOURCE_IP = "198.51.100.31"

#: ⭐ 跨租户判据用的**第二个租户**（域名与 schema 刻意与既有的 `test_openapi_isolation.py`
#: 不同 ⇒ 两张网在同一次运行里各建各的，⛔ 不会撞 `Domain` 的唯一约束）。
DOMAIN_B = "openapi-token-iso-b.test.local"
SCHEMA_B = "openapi_token_iso_b"
#: 租户 B **自己**那把凭据（与 A 的不同 ⇒ 才能证明"B 侧这条路是通的"）。
APP_KEY_B = "probe-token-key-0000b1"
APP_SECRET_B = "probe-token-secret-00000000000b1"

#: 令牌 TTL 的**契约值**（§5.1 取对标正文那个数）。⚠️ 这里有字面量是**有意**的
#: （E-0024：钉值不钉常量）——实现里那个 `7200` 的出处由下面那条扫字面量的用例守。
TTL_SECONDS = 7200


def _payload_of(token: str) -> dict:
    """把令牌的载荷段解成 dict（**用例自己解**，⛔ 不调被测模块的 `_decode_payload`）。

    ⚠️ 自己解是有意的：要断言的正是"线上那一串的**字节形状**"（两段 base64url、载荷是
    JSON 对象、键集恰好 `{ak, exp}`），调被测实现来解就等于用被测对象证明被测对象。
    """
    segment = token.split(".")[0]
    raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    return json.loads(raw.decode("utf-8"))


class TokenPureFunctionTests(SimpleTestCase):
    """令牌纯函数（`apps/openapi/tokens.py`）：形状 / 载荷 / 过期 / 畸形语料。

    ⚠️ **`SimpleTestCase` 是判据的一半**：它建库都不建 ⇒ 这个类里的每条断言都在证明
    "令牌的正确性不需要数据库、不需要 cache、不需要租户上下文"（§5.3 的第 1 条理由）。
    """

    def test_issue_then_verify_returns_the_payload(self):
        """✅ 正向：`issue` 出来的令牌能用**同一把** secret 验通，回那个载荷。"""
        token = tokens.issue(APP_KEY, APP_SECRET, now=1_000_000)
        payload = tokens.verify(token, APP_SECRET, now=1_000_000)
        self.assertEqual(payload["ak"], APP_KEY)
        self.assertEqual(payload["exp"], 1_000_000 + TTL_SECONDS)

    def test_the_token_carries_exactly_the_two_payload_keys(self):
        """⭐ 载荷键集**恰好** `{"ak", "exp"}` ⇒ 令牌里**没有租户标识**（`AGENTS.md` §4 红线）。

        ⚠️ 判据是**恰好集合**（⛔ 不是"含 ak / exp"）：后者对"顺手又塞了一个
        `tenant` / `schema`"这种改动**恒绿**，而那种改动正是这条红线要拦的东西。
        """
        token = tokens.issue(APP_KEY, APP_SECRET, now=1_000_000)
        self.assertEqual(set(_payload_of(token)), {"ak", "exp"})
        # 两段（⛔ 不是三段 / 不是一段）：形状本身就是契约的一部分。
        self.assertEqual(len(token.split(".")), 2)

    def test_a_changed_character_is_rejected(self):
        """⭐ 改一个字符 ⇒ `verify` **抛异常**（判据打在纯函数上，⛔ 不是只看 HTTP 码）。

        三个方向各来一次（⛔ 不是只改一个位置就下结论）：
        ① 换掉**载荷段**里的一个字符（HMAC 覆盖的是载荷段 ⇒ 必红）；
        ② 换掉**签名段**里的一个字符（常量时间比较那一档）；
        ③ 在末尾**多**一个字符（长度变了）。
        ⚠️ 先跑一次"原封不动的令牌能验通"当**对照**：少了它，"verify 一律抛异常"的
        退化实现也会让本用例看着绿。
        """
        token = tokens.issue(APP_KEY, APP_SECRET, now=1_000_000)
        self.assertEqual(tokens.verify(token, APP_SECRET, now=1_000_000)["ak"], APP_KEY)

        segment, signature = token.split(".")

        def flip(text: str) -> str:
            """把一个字符换成**另一个 base64url 字符**（⛔ 不是删掉它）。"""
            head, tail = text[0], text[1:]
            return ("A" if head != "A" else "B") + tail

        for mutated in (
            flip(segment) + "." + signature,
            segment + "." + flip(signature),
            token + "A",
        ):
            with self.subTest(mutated=mutated[-12:]):
                with self.assertRaises(tokens.TokenError):
                    tokens.verify(mutated, APP_SECRET, now=1_000_000)

    def test_another_secret_cannot_verify_the_token(self):
        """另一把 secret 验不过（§5.3 第 3 条"轮换密钥即全体失效"在纯函数这一层的判据）。"""
        token = tokens.issue(APP_KEY, APP_SECRET, now=1_000_000)
        with self.assertRaises(tokens.TokenError):
            tokens.verify(token, DISABLED_SECRET, now=1_000_000)

    def test_expired_by_one_second_fails_and_unexpired_succeeds(self):
        """⭐ 过期边界（⛔ 不 `sleep`：`now=` 参数就是为这条判据留的）。

        逐字钉住三档（口径写在 `tokens.verify` 的 docstring 里）：
        - `now == exp - 1` ⇒ 通过；
        - `now == exp` ⇒ **过期**（到点即失效，于是 `expires_in = 7200` 那句话逐字为真）；
        - `now == exp + 1` ⇒ 过期（"过期 1 秒"）。
        ⚠️ 断言的是**异常类型恰好是** `TokenExpiredError`（⛔ 不是笼统的 `TokenError`）：
        认证层要靠这个类型给"令牌已过期"那一句，类型被吞掉会让那句话永远说不出来。
        """
        issued = 1_000_000
        token = tokens.issue(APP_KEY, APP_SECRET, now=issued)
        expires_at = issued + TTL_SECONDS
        self.assertEqual(tokens.verify(token, APP_SECRET, now=expires_at - 1)["ak"], APP_KEY)
        for now in (expires_at, expires_at + 1):
            with self.subTest(now=now):
                with self.assertRaises(tokens.TokenExpiredError):
                    tokens.verify(token, APP_SECRET, now=now)

    def test_malformed_tokens_raise_exactly_our_own_error(self):
        """⭐ 畸形语料逐条 ⇒ **恰好** `TokenError`。

        这一条守的是"⛔ 不许让 `binascii.Error` / `json.JSONDecodeError` /
        `UnicodeDecodeError` / `TypeError` 漏出去变成 500"：`assertRaises(TokenError)` 只有在
        异常**真的是本模块那个类型**时才通过（那三个标准库异常都不是它的子类）。

        语料（每一类失败各一条，⛔ 不是只发一个空串）：
        - 不是 `str`（`None` / `int` / `bytes`）；
        - 段数不对 / 空段；
        - 段里含 base64url 字母表以外的字符；
        - 载荷段解出来不是 UTF-8 / 不是 JSON / 不是 JSON **对象**；
        - 载荷缺 `ak` / `ak` 不是非空串 / 缺 `exp` / `exp` 不是真 `int`（含 `bool` 与浮点）。
        """
        good = tokens.issue(APP_KEY, APP_SECRET, now=1_000_000)
        _segment, signature = good.split(".")

        def segment_of(raw: bytes) -> str:
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

        cases = {
            "非字符串 None": None,
            "非字符串 int": 12345,
            "非字符串 bytes": b"a.b",
            "空串": "",
            "只有一段": good.split(".")[0],
            "三段": good + ".extra",
            "空载荷段": "." + signature,
            "空签名段": good.split(".")[0] + ".",
            "字母表外的字符": "!!!!." + signature,
            "载荷不是 UTF-8": segment_of(b"\xff\xfe\x00") + "." + signature,
            "载荷不是 JSON": segment_of(b"not-json") + "." + signature,
            "载荷不是 JSON 对象": segment_of(b"[1,2]") + "." + signature,
            "缺 ak": segment_of(b'{"exp":1}') + "." + signature,
            "ak 不是字符串": segment_of(b'{"ak":1,"exp":1}') + "." + signature,
            "ak 是空串": segment_of(b'{"ak":"","exp":1}') + "." + signature,
            "缺 exp": segment_of(b'{"ak":"k"}') + "." + signature,
            "exp 是字符串": segment_of(b'{"ak":"k","exp":"1"}') + "." + signature,
            "exp 是浮点": segment_of(b'{"ak":"k","exp":1.5}') + "." + signature,
            "exp 是布尔": segment_of(b'{"ak":"k","exp":true}') + "." + signature,
        }
        for label, token in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(tokens.TokenError) as caught:
                    tokens.verify(token, APP_SECRET, now=1_000_000)
                # ⚠️ 类型**恰好**是本模块那一个（不是它的子类、更不是标准库的解析异常）。
                self.assertIs(type(caught.exception), tokens.TokenError)

    def test_verify_rejects_a_non_string_secret(self):
        """`app_secret` 不是字符串 ⇒ `TokenError`（⛔ 不让 `TypeError` 冒成 500）。"""
        good = tokens.issue(APP_KEY, APP_SECRET, now=1_000_000)
        for bad in (None, 123, b"x", ""):
            with self.subTest(secret=repr(bad)):
                with self.assertRaises(tokens.TokenError):
                    tokens.verify(good, bad, now=1_000_000)

    def test_issue_rejects_non_string_arguments(self):
        """`issue` 的两个入参必须是非空 `str`（理由见它的 docstring）。"""
        for key, secret in ((None, APP_SECRET), ("", APP_SECRET), (APP_KEY, None), (APP_KEY, "")):
            with self.subTest(key=repr(key), secret=repr(secret)):
                with self.assertRaises(tokens.TokenError):
                    tokens.issue(key, secret, now=1_000_000)

    def test_the_ttl_literal_appears_only_once_in_code_sites(self):
        """⭐ `7200` 这个数在 `apps/openapi/**` 的**代码**里只有一处（= `TOKEN_TTL_SECONDS`）。

        判据用 `tokenize` 收**数字字面量**（注释与 docstring 都是 `COMMENT` / `STRING`
        记号 ⇒ 不算"代码里的字面量"），于是这条能失败：在 `token_views` 里直接写
        `"expires_in": 7200`（而不是引用常量）会让它红。
        ⚠️ 边界：扫描面是 `apps/openapi/**`（本任务的地盘），⛔ 不是全仓；
        用例文件自己写字面量**不**受影响（它们不在这个目录里）。
        """
        sites = []
        for path in sorted(pathlib.Path(tokens.__file__).resolve().parent.rglob("*.py")):
            with path.open("rb") as handle:
                for tok in tokenize.tokenize(handle.readline):
                    if tok.type == tokenize.NUMBER and tok.string == "7200":
                        sites.append((path.name, tok.start[0]))
        self.assertEqual(len(sites), 1, "7200 在 apps/openapi 的代码里出现了 %d 处：%r" % (len(sites), sites))
        name, line = sites[0]
        self.assertEqual(name, "tokens.py")
        source_line = (
            pathlib.Path(tokens.__file__).resolve().parent / name
        ).read_text(encoding="utf-8").splitlines()[line - 1]
        self.assertIn("TOKEN_TTL_SECONDS", source_line)


class _TokenFixture(TenantTestCase):
    """端点与认证共用的夹具：一个启用应用 + 一个停用应用 + 一个租户域名上的 client。"""

    def setUp(self):
        super().setUp()
        # ⚠️ 必须清：§6.3 的按来源 IP 失败计数落 `LocMemCache`，而它**跨用例不清空**
        # （Django 只在测试开始时清一次）⇒ 前面某条用例攒下的失败会把本用例的合法请求挡成 401。
        # ⚠️ 用 `django_cache_module.cache.clear()`（**按名字取**）：本模块 import 期那个
        # `cache` 引用在"打桩"那条用例里是被绕过的那一个（见那条用例的 docstring）。
        django_cache_module.cache.clear()
        self.client = Client(SERVER_NAME=self.domain.domain)
        self.app = self.make_app()
        self.disabled_app = self.make_app(
            name=DISABLED_NAME,
            app_key=DISABLED_KEY,
            app_secret=DISABLED_SECRET,
            state=ApiAppState.DISABLED,
        )

    def make_app(
        self,
        *,
        name=APP_NAME,
        app_key=APP_KEY,
        app_secret=APP_SECRET,
        state=ApiAppState.ENABLED,
        ip_allowlist="",
    ):
        """建一行 `ApiApp`（`created_by` 必填 ⇒ 给本用例自己造的那个账号）。"""
        creator, _ = User.objects.get_or_create(
            username="openapi-token-probe", defaults={"password": "unused-in-this-test"}
        )
        return ApiApp.objects.create(
            name=name,
            app_key=app_key,
            app_secret=app_secret,
            state=state,
            ip_allowlist=ip_allowlist,
            created_by=creator,
        )

    def token_request(self, *, grant_type=GRANT_TYPE_CLIENT_CREDENTIALS, client_id=APP_KEY,
                      client_secret=APP_SECRET, data=None, content_type=None, **extra):
        """发一条 form-urlencoded 的换令牌请求（`data` 给了就整份替换）。"""
        if data is None:
            data = {
                "grant_type": grant_type,
                "client_id": client_id,
                "client_secret": client_secret,
            }
        extra.setdefault("REMOTE_ADDR", SOURCE_IP)
        if content_type is not None:
            extra["content_type"] = content_type
        return self.client.post(TOKEN_PATH, data, **extra)

    def issue_token(self):
        """走真实端点换一个令牌，回**响应体**（判据从"调用方真正看到的东西"取）。"""
        response = self.token_request()
        self.assertEqual(response.status_code, 200, response.content[:200])
        return response.json()

    def bearer_request(self, token, path=BUSINESS_PATH, remote_addr=SOURCE_IP,
                       scheme=BEARER_SCHEME):
        """造一条带 `Authorization` 头的请求（认证类直调那一档用）。"""
        request = APIRequestFactory().get(path, REMOTE_ADDR=remote_addr)
        request.META["HTTP_AUTHORIZATION"] = "%s %s" % (scheme, token)
        return request


class TokenEndpointShapeTests(_TokenFixture):
    """`POST /api/open/oauth/token` 的响应形状（§5.1 / §5.4）。"""

    def test_success_returns_exactly_the_three_bare_fields(self):
        """⭐ 成功 ⇒ 200 ＋ **裸三字段**（⛔ 没有 `code` / `msg` / `data`）。

        ⚠️ 键集判据是**恰好**（`assertEqual(set(body), …)`），⛔ 不是"含这三个键"：
        后者对"顺手包了一层统一信封"**恒绿**，而 §5.4 的选项 A 明确说这条端点不套信封。
        """
        response = self.token_request()
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = response.json()
        self.assertEqual(set(body), {"access_token", "token_type", "expires_in"})
        self.assertIsInstance(body["access_token"], str)
        self.assertTrue(body["access_token"])
        self.assertEqual(body["token_type"], "Bearer")
        self.assertEqual(body["expires_in"], TTL_SECONDS)

    def test_the_announced_ttl_matches_the_token_itself(self):
        """⭐ 响应里那个 `expires_in` 与令牌载荷里的 `exp` **是同一个数**（⛔ 不是两处各说一套）。

        判据：`exp - 签发前时刻 ≥ 7200` 且 `exp - 签发后时刻 ≤ 7200`
        （两个时刻把"签发发生在哪一刻"夹住 ⇒ 差值必须**恰好**是 7200，
        ⛔ 不依赖跑得多快、也不 `sleep`）。
        """
        before = int(time.time())
        body = self.issue_token()
        after = int(time.time())
        self.assertEqual(body["expires_in"], TTL_SECONDS)
        expires_at = _payload_of(body["access_token"])["exp"]
        self.assertGreaterEqual(expires_at - before, TTL_SECONDS)
        self.assertLessEqual(expires_at - after, TTL_SECONDS)

    def test_wrong_secret_missing_app_and_disabled_app_are_word_for_word_identical(self):
        """⭐ 三档凭据失败**逐字相同**（§5.4 的硬要求：这条路不许变成应用枚举口）。

        ⚠️ 判据是**三次调用互比**（`len(set(...)) == 1`），⛔ 不是把同一句期望值抄三遍
        —— 后者在"三处一起被改错"时照样绿，而它想拦的正是"其中一档被改成一句更具体的话"。
        比的是 `(状态码, error, error_description)` 三元组 ⇒ `error` 与 `error_description`
        **两个字段都**逐字相同。
        ⚠️ 另钉两件防"三方都空"的退化：`error` 必须是 `invalid_client` 字面量、
        `error_description` 必须非空。
        """
        disabled_key, disabled_secret = DISABLED_KEY, DISABLED_SECRET
        responses = [
            self.token_request(client_secret=WRONG_SECRET),             # ① 密钥错
            self.token_request(client_id=UNKNOWN_KEY),                  # ② 应用不存在
            self.token_request(
                client_id=disabled_key, client_secret=disabled_secret   # ③ 应用停用
            ),
        ]
        triples = []
        for response in responses:
            body = response.json()
            self.assertEqual(set(body), {"error", "error_description"})
            self.assertEqual(response.status_code, 401, response.content[:200])
            self.assertEqual(body["error"], ERROR_INVALID_CLIENT)
            self.assertTrue(body["error_description"])
            triples.append((response.status_code, body["error"], body["error_description"]))
        self.assertEqual(
            len(set(triples)), 1, "三档凭据失败的答复不逐字相同：%r" % (triples,)
        )
        # 那一句话就是认证层那一句（两处**共用同一个字面量**，⛔ 不各写一份）。
        self.assertEqual(responses[0].json()["error_description"], BEARER_INVALID_CREDENTIALS_MSG)

    def test_missing_credentials_are_rejected_as_invalid_client(self):
        """缺 `client_id` / 缺 `client_secret` ⇒ `invalid_client`（与"密钥错"同一档形状）。"""
        for label, data in (
            ("缺 client_id", {"grant_type": GRANT_TYPE_CLIENT_CREDENTIALS, "client_secret": APP_SECRET}),
            ("缺 client_secret", {"grant_type": GRANT_TYPE_CLIENT_CREDENTIALS, "client_id": APP_KEY}),
            ("两个都缺", {"grant_type": GRANT_TYPE_CLIENT_CREDENTIALS}),
        ):
            with self.subTest(case=label):
                response = self.token_request(data=data)
                body = response.json()
                self.assertEqual(response.status_code, 401)
                self.assertEqual(set(body), {"error", "error_description"})
                self.assertEqual(body["error"], ERROR_INVALID_CLIENT)
                self.assertEqual(body["error_description"], BEARER_INVALID_CREDENTIALS_MSG)

    def test_bad_or_missing_grant_type_is_unsupported_grant_type(self):
        """`grant_type` 不是 `client_credentials`（含没传 / 传空）⇒ `unsupported_grant_type`。

        ⚠️ 判据里**凭据都是对的**：这样才排得掉"其实是被凭据那一档拦下的"这种误读。
        """
        for label, data in (
            ("密码模式", {"grant_type": "password", "client_id": APP_KEY, "client_secret": APP_SECRET}),
            ("空串", {"grant_type": "", "client_id": APP_KEY, "client_secret": APP_SECRET}),
            ("没传", {"client_id": APP_KEY, "client_secret": APP_SECRET}),
        ):
            with self.subTest(case=label):
                response = self.token_request(data=data)
                body = response.json()
                self.assertEqual(response.status_code, 401, response.content[:200])
                self.assertEqual(set(body), {"error", "error_description"})
                self.assertEqual(body["error"], ERROR_UNSUPPORTED_GRANT_TYPE)
                self.assertEqual(body["error_description"], UNSUPPORTED_GRANT_TYPE_DESCRIPTION)

    def test_a_non_form_body_is_not_a_500_and_not_an_envelope(self):
        """⭐ body **不是** form-urlencoded ⇒ 401 + OAuth2 体（⛔ 不 500、⛔ 不是本仓信封）。

        这一条把"入参只读 `request.POST`"这个选择的后果钉死：`request.data` 那条路遇到
        不认识的媒体类型会抛 `UnsupportedMediaType`（**415**，一个**不是本契约**的形态）。
        ⚠️ 三档各来一条：JSON 体 / 纯文本体 / 空体。
        """
        json_body = json.dumps(
            {"grant_type": GRANT_TYPE_CLIENT_CREDENTIALS, "client_id": APP_KEY,
             "client_secret": APP_SECRET}
        )
        for label, kwargs in (
            ("JSON 体", {"data": json_body, "content_type": "application/json"}),
            ("纯文本体", {"data": "grant_type=x", "content_type": "text/plain"}),
            ("空体", {"data": "", "content_type": "text/plain"}),
        ):
            with self.subTest(case=label):
                response = self.token_request(**kwargs)
                body = response.json()
                self.assertEqual(response.status_code, 401, response.content[:200])
                self.assertEqual(set(body), {"error", "error_description"})
                # ⛔ 不是本仓信封：那三个键一个都不许出现。
                self.assertNotIn("code", body)
                self.assertNotIn("msg", body)
                self.assertNotIn("data", body)

    def test_a_session_login_does_not_get_a_token(self):
        """有登录会话、但**没有** form 凭据 ⇒ 仍是 401（⛔ 不复用会话身份换令牌）。

        ⚠️ 这条钉的是 `authentication_classes = []`（本端点**匿名**可达）**不等于**
        "登录了就放行"：凭据只从 form 里读，会话在**这里**一个字节都不算。
        """
        from tests.p5_auth_helpers import make_logged_in_client

        logged_in = make_logged_in_client(username="openapi-token-session")
        # ⚠️ `make_logged_in_client` 返回的是绑定了租户域名的 client（与本文件的 `self.client` 同源）。
        response = logged_in.post(TOKEN_PATH, {"grant_type": GRANT_TYPE_CLIENT_CREDENTIALS})
        self.assertEqual(response.status_code, 401, response.content[:200])
        self.assertEqual(set(response.json()), {"error", "error_description"})

    def test_issuing_and_verifying_a_token_never_touches_the_cache(self):
        """⭐ 签发 + 校验**一次都不碰** `django.core.cache.cache`（"多 worker 下无状态"的代理判据）。

        ## 这条判据由两半组成（各自独立、都能失败）

        1. **运行期代理**：把 `django.core.cache` 模块上那个**名字**换成"一被访问就抛"的对象后，
           走真实端点签发一个令牌、再用 `tokens.verify` 校验它，两步都必须成功。
           ⚠️ 对照（E-0029 要求的"桩接没接上"）：先**在同一个 with 块里自己按名字取一次**，
           必须抛 —— ⛔ 不能拿本模块 import 期绑定的那个 `cache` 去试（那个引用不受 patch 约束，
           试出来的"没抛"是**假**的，本用例第一版就踩了这个）。
        2. **结构判据**：`apps/openapi/tokens.py` 的源码里**没有任何**对 `cache` 的引用
           （按 AST 取名字 / 属性 / import 的模块名，⛔ 不按文本搜——那份 docstring 里
           "不读 cache" 这几个字本身就是文本）。

        ## ⛔ 这条**不是**什么（如实登记边界）
        - ⛔ **不是**"跨进程一致"的证明（那要两个进程对照，本文件做不到，见模块 docstring）；
        - ⛔ **不是**"整条路径不读 cache"的完备证明：`mock.patch` 打的是**被查找的那个名字**
          （E-0029），已经在 **import 期**持有引用的模块（例如 `authentication.py` 的
          `from django.core.cache import cache`）不受它约束 ⇒ 运行期那一半钉住的是
          "这条路上没有任何一处**在调用时按名字去查** cache"；`tokens.py` 那一半由结构判据补齐。
        """
        with mock.patch("django.core.cache.cache", _CacheBoom()):
            # 对照：桩确实生效了（⚠️ 必须按**名字**取，见 docstring）。
            with self.assertRaises(AssertionError):
                django_cache_module.cache.get("probe")

            response = self.token_request()
            self.assertEqual(response.status_code, 200, response.content[:200])
            body = response.json()
            self.assertEqual(set(body), {"access_token", "token_type", "expires_in"})
            # ⚠️ 校验也在这个 with 块里跑（它同样是判据的一半）。
            payload = tokens.verify(body["access_token"], APP_SECRET)
            self.assertEqual(payload["ak"], APP_KEY)

        # 结构判据：纯函数模块的 AST 里没有 cache 这个名字（文本里的"不读 cache"不算）。
        tree = ast.parse(pathlib.Path(tokens.__file__).read_text(encoding="utf-8"))
        references = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                references.add(node.id)
            elif isinstance(node, ast.Attribute):
                references.add(node.attr)
            elif isinstance(node, ast.Import):
                references.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                references.add(node.module or "")
                references.update(alias.name for alias in node.names)
        touched = sorted(name for name in references if "cache" in name.lower())
        self.assertEqual(touched, [], "tokens.py 里出现了对 cache 的引用：%r" % (touched,))


class _CacheBoom:
    """任何属性访问都抛的假 cache（只为证明"这条路一次都没碰 cache"）。"""

    def __getattr__(self, name):
        raise AssertionError("令牌路径访问了 cache.%s" % name)


class BearerAuthenticationTests(_TokenFixture):
    """`ApiAppBearerAuthentication` 的逐条判据（§5.2 / §5.3 / §5.4）。

    ⚠️ 直调 `authenticate()`（不建探针 urlconf）：本仓的结构网按 `get_resolver()` 枚举
    真实端点，为了测认证去挂一条路由会顺带把一批登记项拉进来（同族理由见
    `tests/test_openapi_auth.py` 的模块 docstring）。认证类的装配口径由
    `TokenEndpointShapeTests` / 下面那条**业务端点**用例覆盖。
    """

    def test_valid_token_returns_the_app_and_an_anonymous_user(self):
        """✅ 正向：`(AnonymousUser(), app)`，并写 `last_used_at`（复用既有那一份实现）。

        ⚠️ 判据用 `isinstance(user, AnonymousUser)`（⛔ 不是"不是 `None`"——`(None, app)`
        在那种弱判据下也绿），`last_used_at` 打在**数据库行**上（⛔ 不打 logger，E-0009）。
        """
        body = self.issue_token()
        # ⭐ 头里那个方案名直接用**端点自己宣布的** `token_type`（证明它真的是本服务认的方案）。
        request = self.bearer_request(body["access_token"], scheme=body["token_type"])
        self.assertIsNone(ApiApp.objects.get(pk=self.app.pk).last_used_at)
        user, app = ApiAppBearerAuthentication().authenticate(request)
        self.assertIsInstance(user, AnonymousUser)
        self.assertEqual(app.pk, self.app.pk)
        self.assertEqual(request._api_app.pk, self.app.pk)
        self.assertIsNotNone(ApiApp.objects.get(pk=self.app.pk).last_used_at)
        # 没被调用过的那一行保持 None（否则"每次请求写全部行"也能让上面那句绿）。
        self.assertIsNone(ApiApp.objects.get(pk=self.disabled_app.pk).last_used_at)

    def test_the_three_credential_failures_speak_the_same_sentence(self):
        """⭐ 令牌不对 / 应用不存在 / 应用停用 ⇒ **同一句** `msg`（§5.4 防应用枚举）。

        ⚠️ 三次调用**互比**（`len(set(...)) == 1`），⛔ 不是各写一遍期望值；
        并另钉"那句话就是 `BEARER_INVALID_CREDENTIALS_MSG`"（防三方都空）。
        """
        good = self.issue_token()["access_token"]
        bad_but_well_formed = tokens.issue(APP_KEY, WRONG_SECRET)
        unknown = tokens.issue(UNKNOWN_KEY, APP_SECRET)
        disabled = tokens.issue(DISABLED_KEY, DISABLED_SECRET)

        # 对照：真令牌**必须**能过 —— 少了它，"一律拒绝"的退化实现也会让下面那句绿。
        _user, app = ApiAppBearerAuthentication().authenticate(self.bearer_request(good))
        self.assertEqual(app.pk, self.app.pk)

        messages = []
        for token in (bad_but_well_formed, unknown, disabled):
            with self.assertRaises(AuthenticationFailed) as caught:
                ApiAppBearerAuthentication().authenticate(self.bearer_request(token))
            messages.append((caught.exception.status_code, str(caught.exception.detail)))
        self.assertEqual(len(set(messages)), 1, "三档失败的答复不逐字相同：%r" % (messages,))
        self.assertEqual(messages[0], (401, BEARER_INVALID_CREDENTIALS_MSG))

    def test_an_expired_token_says_so(self):
        """过期 ⇒ 另一句（`令牌已过期`）：这一档排在验签之后 ⇒ 说破不构成枚举面。"""
        expired = tokens.issue(APP_KEY, APP_SECRET, now=int(time.time()) - TTL_SECONDS - 1)
        with self.assertRaises(AuthenticationFailed) as caught:
            ApiAppBearerAuthentication().authenticate(self.bearer_request(expired))
        self.assertEqual(str(caught.exception.detail), BEARER_EXPIRED_MSG)

    def test_shape_errors_come_back_as_the_same_sentence(self):
        """形状非法 / 空令牌的 `msg` 与"签名错"**同一句**（⛔ 不回显异常原文）。"""
        for label, header in (
            ("空令牌", ""),
            ("只有一段", "abc"),
            ("字母表外", "!!!!.!!!!"),
        ):
            with self.subTest(case=label):
                request = APIRequestFactory().get(BUSINESS_PATH, REMOTE_ADDR=SOURCE_IP)
                request.META["HTTP_AUTHORIZATION"] = "%s %s" % (BEARER_SCHEME, header)
                with self.assertRaises(AuthenticationFailed) as caught:
                    ApiAppBearerAuthentication().authenticate(request)
                self.assertEqual(str(caught.exception.detail), BEARER_INVALID_CREDENTIALS_MSG)

    def test_the_scheme_is_case_insensitive_and_whitespace_tolerant(self):
        """⭐ 容错口径逐条：方案名大小写不敏感、词间多空白容忍（⛔ 都**不**进对外文档）。

        反例（⛔ 不能被容忍的三件）一起钉住：
        - 别的方案名（`Basic`）；
        - 只有方案名、没有令牌；
        - 三个词。
        """
        token = self.issue_token()["access_token"]
        for header in (
            "Bearer %s" % token,
            "bearer %s" % token,
            "BEARER %s" % token,
            "Bearer   %s" % token,
            "  Bearer\t%s  " % token,
        ):
            with self.subTest(header=header[:14]):
                request = APIRequestFactory().get(BUSINESS_PATH, REMOTE_ADDR=SOURCE_IP)
                request.META["HTTP_AUTHORIZATION"] = header
                _user, app = ApiAppBearerAuthentication().authenticate(request)
                self.assertEqual(app.pk, self.app.pk)
        for header in ("Basic %s" % token, "Bearer", "Bearer %s extra" % token, ""):
            with self.subTest(header=header[:14]):
                request = APIRequestFactory().get(BUSINESS_PATH, REMOTE_ADDR=SOURCE_IP)
                request.META["HTTP_AUTHORIZATION"] = header
                with self.assertRaises(AuthenticationFailed):
                    ApiAppBearerAuthentication().authenticate(request)

    def test_rotating_the_secret_invalidates_the_old_token_immediately(self):
        """⭐ 轮换密钥 ⇒ 旧令牌**立刻**失效、用新密钥换的令牌可用（§5.3 第 3 条）。

        ⚠️ 两半都要：只有"旧的失效"那一半时，一个"令牌永远验不过"的退化实现也绿。
        """
        old_token = self.issue_token()["access_token"]
        self.app.app_secret = DISABLED_SECRET
        self.app.save(update_fields=["app_secret"])

        with self.assertRaises(AuthenticationFailed) as caught:
            ApiAppBearerAuthentication().authenticate(self.bearer_request(old_token))
        self.assertEqual(str(caught.exception.detail), BEARER_INVALID_CREDENTIALS_MSG)

        fresh = self.token_request(client_secret=DISABLED_SECRET)
        self.assertEqual(fresh.status_code, 200, fresh.content[:200])
        _user, app = ApiAppBearerAuthentication().authenticate(
            self.bearer_request(fresh.json()["access_token"])
        )
        self.assertEqual(app.pk, self.app.pk)

    def test_disabling_the_app_invalidates_the_token_immediately(self):
        """⭐ 停用 ⇒ 令牌立刻失效（应用行**不缓存**），且文案与凭据错**同一句**。"""
        token = self.issue_token()["access_token"]
        self.app.state = ApiAppState.DISABLED
        self.app.save(update_fields=["state"])
        with self.assertRaises(AuthenticationFailed) as caught:
            ApiAppBearerAuthentication().authenticate(self.bearer_request(token))
        self.assertEqual(str(caught.exception.detail), BEARER_INVALID_CREDENTIALS_MSG)

    def test_ip_allowlist_is_enforced_with_the_shared_criteria(self):
        """IP 白名单（空 = 不限制）：不在名单内 ⇒ `来源 IP 不在白名单内`；在名单内 ⇒ 通过。

        ⚠️ 用的是**同一条** `_ip_allowed` 与**同一句** `IP_NOT_ALLOWED_MSG`（复用而不是抄一份）。
        """
        self.app.ip_allowlist = OTHER_SOURCE_IP
        self.app.save(update_fields=["ip_allowlist"])
        token = self.issue_token()["access_token"]
        with self.assertRaises(AuthenticationFailed) as caught:
            ApiAppBearerAuthentication().authenticate(self.bearer_request(token))
        self.assertEqual(str(caught.exception.detail), IP_NOT_ALLOWED_MSG)

        self.app.ip_allowlist = "%s\n%s" % (OTHER_SOURCE_IP, SOURCE_IP)
        self.app.save(update_fields=["ip_allowlist"])
        _user, app = ApiAppBearerAuthentication().authenticate(self.bearer_request(token))
        self.assertEqual(app.pk, self.app.pk)

    def test_the_ip_failure_gate_blocks_even_a_valid_token(self):
        """按来源 IP 的失败闸（**复用**既有那一份）：攒满阈值后，**合法**令牌也被挡。

        ⚠️ 阈值写**字面量**（E-0024）：先钉住常量就是 20，再照 20 次去攒。
        这条同时证明"失败真的被计了数"（⛔ 不是只有拒绝、没有记账）。
        """
        self.assertEqual(IP_FAILURE_MAX, 20)
        for _ in range(20):
            with self.assertRaises(AuthenticationFailed):
                ApiAppBearerAuthentication().authenticate(
                    self.bearer_request(tokens.issue(APP_KEY, WRONG_SECRET))
                )
        good = self.issue_token()["access_token"]
        with self.assertRaises(AuthenticationFailed) as caught:
            ApiAppBearerAuthentication().authenticate(self.bearer_request(good))
        self.assertEqual(str(caught.exception.detail), BEARER_INVALID_CREDENTIALS_MSG)
        # 另一个来源**不受**影响（计数是按 (租户 schema, 来源 IP) 分桶的）。
        _user, app = ApiAppBearerAuthentication().authenticate(
            self.bearer_request(good, remote_addr=OTHER_SOURCE_IP)
        )
        self.assertEqual(app.pk, self.app.pk)

    def test_the_challenge_header_is_bearer(self):
        """`authenticate_header` 回 `Bearer`（非空 ⇒ 401 才带 `WWW-Authenticate`）。"""
        header = ApiAppBearerAuthentication().authenticate_header(
            APIRequestFactory().get(BUSINESS_PATH)
        )
        self.assertEqual(header, "Bearer")
        self.assertNotIn(APP_SECRET, header)


class BearerOnBusinessEndpointTests(_TokenFixture):
    """令牌打已切到 Bearer 的详情路径，不再是 401，也不是「缺少签名参数」。

    不断言 200：能不能读还看节点门和游戏范围。判据打在响应体上。
    把这条路径的认证改回签名，会拿到那句 401，这条用例红。
    """

    def test_a_bearer_token_is_still_rejected_by_the_signature_only_business_endpoint(self):
        """令牌打详情路径不再是 401，也不是「缺少签名参数」。

        不断言 200：能不能读还看节点门和游戏范围。
        把认证改回签名，这条会红。
        """
        body = self.issue_token()
        response = self.client.get(
            DETAIL_PATH,
            REMOTE_ADDR=SOURCE_IP,
            HTTP_AUTHORIZATION="%s %s" % (body["token_type"], body["access_token"]),
        )
        self.assertNotEqual(response.status_code, 401, response.content[:200])
        payload = response.json()
        # 业务端点的答复仍是**本仓信封**（恰好 code/msg/data），⛔ 不是 OAuth2 体。
        self.assertEqual(set(payload), {"code", "msg", "data"})
        # ⚠️ 比的是**今天在服役**的那句文案（签名那句常量已删）。
        self.assertNotEqual(payload.get("msg"), "缺少签名参数", payload)

    def test_a_bearer_token_passes_authentication_on_the_switched_collection_path(self):
        """⭐ 对照（⛔ 不是陪衬）：拿真令牌打集合路径 ⇒ **不是 401**。

        判别力：把集合路径的认证改回签名（或两套并存）⇒ 这一条会拿到签名那句 401 ⇒ 红。
        ⚠️ 它**不断言 200**：能不能读取决于业务授权面；这里只钉"认证这一层"⇒
        判据是 `code != 401` 且**不是**签名那五句之一。
        """
        body = self.issue_token()
        response = self.client.get(
            BUSINESS_PATH,
            REMOTE_ADDR=SOURCE_IP,
            HTTP_AUTHORIZATION="%s %s" % (body["token_type"], body["access_token"]),
        )
        self.assertNotEqual(response.status_code, 401, response.content[:200])
        payload = response.json()
        # ⚠️ 比的是**今天在服役**的那句文案（签名那句常量已删）。
        self.assertNotEqual(payload.get("msg"), "缺少签名参数", payload)


class TokenTenantBoundaryTests(_TokenFixture):
    """⭐ 租户边界：**租户 A 的令牌/凭据打租户 B 的域名 ⇒ 401**（`AGENTS.md` §4 的硬规则）。

    ## 为什么本任务必须有这一条

    §4 写着「**任何新增查询都必须携带租户上下文，并补一条「以租户 A 的凭据访问 B 的数据
    必须失败」的测试**」；本任务新增的查询正是「按 `app_key` 查 `ApiApp`」
    （取令牌端点与 Bearer 认证各一处）。租户上下文**只**来自请求的 `Host` → 连接 schema
    （§5.3：⛔ 令牌里不放租户标识，载荷键集恰好 `{ak, exp}` 那条用例是它的另一半判据）。

    ⚠️ **判别力边界（如实写清，别读大）**：跨 schema 的隔离**主要由连接承担**
    （请求进来时 `TenantMainMiddleware` 换 schema），⛔ 不是由这条查询的过滤条件承担
    ⇒ 它能抓的是「这条查询不再落在当前租户的 schema 上」与「令牌里被塞了租户标识、
    于是能跨库用」那两类 —— 后者正是本任务最该防的形状。
    ⚠️ 两条**正向对照**（A 的令牌打 A、B 的令牌打 B）是必需的：少了它们，那个 401 可能由
    「这条路对谁都不生效」凑出来（E-0004 同族）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # ⚠️ 第二个租户必须在 `setUpClass` 里建：schema 的 DDL **不能**回滚在 `TestCase`
        # 的事务里（同款先例：`tests/test_openapi_isolation.py`）。
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="令牌隔离租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        # ⚠️ 第一行把连接钉回租户 A（上一条用例的跨域名请求会把连接留在那个租户的 schema 上）。
        connection.set_tenant(self.tenant)
        super().setUp()
        try:
            with tenant_context(self.tenant_b):
                self.app_b = self.make_app(
                    name="令牌探针应用丙", app_key=APP_KEY_B, app_secret=APP_SECRET_B
                )
        finally:
            connection.set_tenant(self.tenant)

    def test_a_token_issued_for_tenant_a_is_rejected_on_tenant_bs_domain(self):
        """A 的令牌打 B 的域名 ⇒ 与"凭据错"**同一句**；两侧各有一次正向对照。"""
        token_a = self.issue_token()["access_token"]
        # 对照①：A 的令牌 + A 的域名 ⇒ 通过。
        _user, app = ApiAppBearerAuthentication().authenticate(self.bearer_request(token_a))
        self.assertEqual(app.pk, self.app.pk)
        try:
            connection.set_tenant(self.tenant_b)
            # 负断言：B 的库里没有这个 `app_key` ⇒ 与"令牌不对"同一句（§5.4 的防枚举口径）。
            with self.assertRaises(AuthenticationFailed) as caught:
                ApiAppBearerAuthentication().authenticate(self.bearer_request(token_a))
            self.assertEqual(str(caught.exception.detail), BEARER_INVALID_CREDENTIALS_MSG)
            # 对照②：B **自己**的令牌 + B 的域名 ⇒ 通过（证明 B 侧这条路是通的）。
            _user_b, app_b = ApiAppBearerAuthentication().authenticate(
                self.bearer_request(tokens.issue(APP_KEY_B, APP_SECRET_B))
            )
            self.assertEqual(app_b.pk, self.app_b.pk)
        finally:
            connection.set_tenant(self.tenant)

    def test_the_token_endpoint_is_bound_to_the_request_host(self):
        """取令牌端点同样按 `Host` 定租户：A 的凭据打 B 的域名 ⇒ 401 `invalid_client`。

        ⚠️ B 自己的凭据打 B 的域名是**正向对照**（它 200 ⇒ 上面那个 401 只可能是"B 里没有
        这个应用"，⛔ 不是"B 侧这条路整个不通"）。
        """
        self.assertEqual(self.token_request().status_code, 200)
        try:
            crossed = Client(SERVER_NAME=DOMAIN_B).post(
                TOKEN_PATH,
                {
                    "grant_type": GRANT_TYPE_CLIENT_CREDENTIALS,
                    "client_id": APP_KEY,
                    "client_secret": APP_SECRET,
                },
                REMOTE_ADDR=SOURCE_IP,
            )
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(crossed.status_code, 401, crossed.content[:200])
        body = crossed.json()
        self.assertEqual(set(body), {"error", "error_description"})
        self.assertEqual(body["error"], ERROR_INVALID_CLIENT)

        try:
            own = Client(SERVER_NAME=DOMAIN_B).post(
                TOKEN_PATH,
                {
                    "grant_type": GRANT_TYPE_CLIENT_CREDENTIALS,
                    "client_id": APP_KEY_B,
                    "client_secret": APP_SECRET_B,
                },
                REMOTE_ADDR=SOURCE_IP,
            )
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(own.status_code, 200, own.content[:200])
        self.assertEqual(set(own.json()), {"access_token", "token_type", "expires_in"})
