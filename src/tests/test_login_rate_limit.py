"""三个登录口的**温和限流**网（D-0332；业务方 2026-10-30「加上登录限流，不要太高了」）。

口径出处：`apps/core/login_rate_limit.py` 的模块 docstring（阈值/两段式/清零的权威定义，
本文件只钉**可观察行为**，不重抄口径）：

- 三个口（主登录 `/api/auth/login` · App 密码登录 `/api/app/auth/login/` ·
  公众号绑定页 `/wechatmp/bind-confirm` 的 `action=login`）每口两条承重用例：
  **达阈值拒绝**（且拒绝发生在 `authenticate` 之前）+ **成功登录清零**；
- **IP 维**一条：跨口共享（从 App 口灌满 IP 桶，网页口同 IP 被挡、换 IP 不被挡）；
- **正常用户不受影响**两条回归钉：低于阈值的错几次+答对 → 照常进；
  连续**成功**登录永不计数（数的是失败，不是尝试）。

夹具口径：

- 阈值经 `override_settings` 调小（限流模块在**函数体里** `getattr(settings, …)` 读，
  import 期读常量的写法会让覆盖失效——那是 D-0246 族登记过的坑）。
  ⚠️ 装饰必须落在**每个测试方法**上：类级 `override_settings` 在本仓测试运行器
  （共享租户 schema 补丁）下**不生效**——本文件第一版用类级装饰时 4 条阈值用例全红
  （触线后照常放行），改方法级后全绿；同一条坑的既有登记见
  `tests/test_app_auth.py::AppAuthTestCase` 类 docstring；
- cache = 默认 locmem，**跨用例存活** ⇒ 每条用例 setUp/addCleanup 各清一次
  （照 `tests/test_realname_rate_limit.py` 的 `_RateLimitCase`）；
- 业务码与文案**按字面值**断言（E-0024：从被测模块 import 会把「码被改坏/文案被改坏」藏起来）；
  各口既有失败文案也按字面钉住（挡掉「限流文案漂成失败文案」的回归）；
- 全部夹具合成值（E-0077）。
"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import Client, override_settings
from django_tenants.test.cases import TenantTestCase

User = get_user_model()

# ── 字面值（E-0024：⛔ 不从被测模块 import）────────────────────────────────
OK = 0
VALIDATION = 1000
LOGIN_RATE_LIMITED = 1038
RATE_LIMITED_MSG = "尝试过于频繁，请稍后再试"
WEB_FAIL_MSG = "用户名或密码错误"  # 主登录既有失败文案（`core/auth_views.FAIL_MESSAGE`）
APP_FAIL_MSG = "账号或密码错误"  # App 登录既有失败文案
BIND_FAIL_MSG = "账号或密码不正确"  # 绑定页既有失败文案

# ── 温和阈值的**测试值**（形状同生产：IP 阈 > 账号阈值，锁期 ≥ 窗口）────────
GENTLE = {
    "LOGIN_IP_FAILURE_LIMIT": 5,
    "LOGIN_IP_FAILURE_WINDOW_SECONDS": 900,
    "LOGIN_IP_BLOCK_SECONDS": 1800,
    "LOGIN_ACCOUNT_FAILURE_LIMIT": 3,
    "LOGIN_ACCOUNT_FAILURE_WINDOW_SECONDS": 900,
    "LOGIN_ACCOUNT_BLOCK_SECONDS": 900,
}

USERNAME = "rl-user"
PASSWORD = "pw-rl-123456"

WEB_LOGIN = "/api/auth/login"
APP_LOGIN = "/api/app/auth/login/"
BIND_CONFIRM = "/wechatmp/bind-confirm"


class _RateLimitCase(TenantTestCase):
    """公共夹具：真实账号 + 钉到测试域名 + **清过的 cache**。"""

    def setUp(self):
        super().setUp()
        cache.clear()  # locmem 跨用例存活，桶必须每条用例从零起（事务回滚回不去）
        self.addCleanup(cache.clear)
        connection.set_tenant(self.tenant)  # 挡住上一条用例残留的 schema 上下文
        self.user = User.objects.create_user(username=USERNAME, password=PASSWORD)
        self.domain = self.tenant.get_primary_domain().domain

    def _client(self) -> Client:
        # ⚠️ 默认 client 的 `SERVER_NAME='testserver'` 不在域名表 ⇒ 租户中间件直接 404，
        # 必须钉到 TenantTestCase 自带的那个测试域名（`test_wechatmp_bind.py` 同款注释）。
        return Client(SERVER_NAME=self.domain)

    # ── 三个口的薄封装（返回 `django.test.Client` 的 Response）──────────────
    # `ip` 用**请求级** kwarg 传 `HTTP_X_REAL_IP`（本仓既有写法，`test_app_auth.py` 同款
    # —— `client_ip` 的取值第一优先就是 nginx 覆盖写的 `X-Real-IP`）。
    def _web(self, username, password, ip=None):
        extra = {"HTTP_X_REAL_IP": ip} if ip else {}
        return self._client().post(
            WEB_LOGIN, {"username": username, "password": password}, **extra
        )

    def _app(self, username, password, ip=None):
        extra = {"HTTP_X_REAL_IP": ip} if ip else {}
        return self._client().post(
            APP_LOGIN,
            {"username": username, "password": password},
            content_type="application/json",
            **extra,
        )

    def _bind(self, username, password, ip=None):
        extra = {"HTTP_X_REAL_IP": ip} if ip else {}
        return self._client().post(
            BIND_CONFIRM,
            {"action": "login", "username": username, "password": password},
            **extra,
        )


class AppLoginRateLimitTests(_RateLimitCase):
    """口 ②：App 密码登录。拒绝 = 同一个 `api_response` 信封 + 业务码 1038。"""

    @override_settings(**GENTLE)
    def test_account_threshold_blocks_even_with_correct_password(self):
        # 前 3 次（= 账号阈值）按**既有**失败档答（1000 + 既有文案）；
        # 第 4 次带上**正确**口令也被拒 ⇒ 闸在 `authenticate` 之前。
        for _ in range(3):
            res = self._app(USERNAME, "wrong-pw")
            body = res.json()
            self.assertEqual(res.status_code, 200)
            self.assertEqual(body["code"], VALIDATION, body)
            self.assertEqual(body["msg"], APP_FAIL_MSG)
        res = self._app(USERNAME, PASSWORD)
        body = res.json()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(body["code"], LOGIN_RATE_LIMITED, body)
        self.assertEqual(body["msg"], RATE_LIMITED_MSG)
        self.assertIsNone(body["data"])
        # 拒绝发生在发令牌之前：库里一张令牌都没有（终态判据，AGENTS §5-G）。
        from apps.accounts.models import AppToken

        self.assertEqual(AppToken.objects.count(), 0)

    @override_settings(**GENTLE)
    def test_success_clears_the_account_counter(self):
        # f f s f f s：若成功不清零，第二轮第 3 次失败会把账号锁上，最后一次成功会被拒。
        self._app(USERNAME, "wrong-pw")
        self._app(USERNAME, "wrong-pw")
        self.assertEqual(self._app(USERNAME, PASSWORD).json()["code"], OK)
        self._app(USERNAME, "wrong-pw")
        self._app(USERNAME, "wrong-pw")
        body = self._app(USERNAME, PASSWORD).json()
        self.assertEqual(body["code"], OK, body)


class WebLoginRateLimitTests(_RateLimitCase):
    """口 ①：主登录。拒绝 = **与既有失败同一 HTTP 401 形状**（含 `WWW-Authenticate`）+ 码 1038。"""

    @override_settings(**GENTLE)
    def test_account_threshold_blocks_with_the_same_401_shape(self):
        for _ in range(3):
            res = self._web(USERNAME, "wrong-pw")
            self.assertEqual(res.status_code, 401)
            self.assertEqual(res.json()["code"], 401)
            self.assertEqual(res.json()["msg"], WEB_FAIL_MSG)
        res = self._web(USERNAME, PASSWORD)
        self.assertEqual(res.status_code, 401)  # ⛔ 同一 HTTP 形状（不是新契约）
        body = res.json()
        self.assertEqual(body["code"], LOGIN_RATE_LIMITED, body)
        self.assertEqual(body["msg"], RATE_LIMITED_MSG)
        # spec §4.6：视图构造的 401 必须带 `WWW-Authenticate`。
        self.assertEqual(res["WWW-Authenticate"], "Session")

    @override_settings(**GENTLE)
    def test_success_clears_the_account_counter(self):
        self._web(USERNAME, "wrong-pw")
        self._web(USERNAME, "wrong-pw")
        self.assertEqual(self._web(USERNAME, PASSWORD).json()["code"], OK)
        self._web(USERNAME, "wrong-pw")
        self._web(USERNAME, "wrong-pw")
        body = self._web(USERNAME, PASSWORD).json()
        self.assertEqual(body["code"], OK, body)


class BindPageRateLimitTests(_RateLimitCase):
    """口 ③：公众号绑定确认页。拒绝 = **与本页既有失败同一形状**（200 + stage=login + 文案）。"""

    def _page_html(self, res):
        self.assertEqual(res.status_code, 200, res.content[:300])
        return res.content.decode()

    @override_settings(**GENTLE)
    def test_account_threshold_blocks_with_the_login_page(self):
        for _ in range(3):
            html = self._page_html(self._bind(USERNAME, "wrong-pw"))
            self.assertIn(BIND_FAIL_MSG, html)
            self.assertNotIn(RATE_LIMITED_MSG, html)
        html = self._page_html(self._bind(USERNAME, PASSWORD))
        self.assertIn(RATE_LIMITED_MSG, html)
        # 锁定页仍是登录表单（stage=login），⛔ 不是错误页/不是跳转。
        self.assertNotIn(BIND_FAIL_MSG, html)

    @override_settings(**GENTLE)
    def test_success_clears_the_account_counter(self):
        self._bind(USERNAME, "wrong-pw")
        self._bind(USERNAME, "wrong-pw")
        res = self._bind(USERNAME, PASSWORD)
        self.assertEqual(res.status_code, 302, res.content[:300])  # 登录成功 → 302 回 GET
        self._bind(USERNAME, "wrong-pw")
        self._bind(USERNAME, "wrong-pw")
        res = self._bind(USERNAME, PASSWORD)
        self.assertEqual(res.status_code, 302, res.content[:300])


class IpDimensionTests(_RateLimitCase):
    """IP 维：**全口共享**（任意一个口灌满 ⇒ 三个口一起挡），且按 IP 分桶（换 IP 不连坐）。"""

    @override_settings(**GENTLE)
    def test_ip_block_is_shared_across_endpoints_and_keyed_by_ip(self):
        attacker_ip = "203.0.113.9"
        clean_ip = "198.51.100.7"
        # 从 App 口灌 IP 桶：5 个**互不相同**的不存在用户名，各失败 1 次
        # （每个账号桶只有 1 < 账号阈值 3 ⇒ 纯 IP 维触线，与账号维互不沾）。
        for i in range(GENTLE["LOGIN_IP_FAILURE_LIMIT"]):
            res = self._app(f"ghost-{i}", "wrong-pw", ip=attacker_ip)
            self.assertEqual(res.json()["code"], VALIDATION)
        # 同 IP + **真账号 + 正确口令**：App 口被挡（IP 闸在 authenticate 之前）。
        body = self._app(USERNAME, PASSWORD, ip=attacker_ip).json()
        self.assertEqual(body["code"], LOGIN_RATE_LIMITED, body)
        # ⭐ 全口：同一个 IP 改打**网页口**（本用例第一次碰它）照样被挡。
        res = self._web(USERNAME, PASSWORD, ip=attacker_ip)
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["code"], LOGIN_RATE_LIMITED)
        # ⭐ 按 IP 分桶：**同一个账号**换一个干净 IP → 照常登进（不连坐）。
        body = self._web(USERNAME, PASSWORD, ip=clean_ip).json()
        self.assertEqual(body["code"], OK, body)


class NormalUserUnaffectedTests(_RateLimitCase):
    """回归钉（温和档的本意）：低频失败与纯成功流量永远碰不到闸。"""

    @override_settings(**GENTLE)
    def test_below_threshold_failures_then_correct_password_is_never_blocked(self):
        # 同一账号错 2 次（< 3）、同一 IP 共错 2 次（< 5）⇒ 答对照常进；
        # 换一个**同 IP** 的第二个人也照常进（IP 桶还远没满）。
        self._app(USERNAME, "wrong-pw")
        self._web(USERNAME, "wrong-pw")
        self.assertEqual(self._app(USERNAME, PASSWORD).json()["code"], OK)
        other = User.objects.create_user(username="rl-user-2", password=PASSWORD)
        self.assertTrue(other.pk)
        body = self._web("rl-user-2", PASSWORD).json()
        self.assertEqual(body["code"], OK, body)

    @override_settings(**GENTLE)
    def test_successes_never_count_toward_the_limit(self):
        # 数「失败」不数「尝试」（D-0332 口径）：连打 6 次成功登录（超过 IP 阈值 5），
        # 没有任何一次被挡 ⇒ 成功流量不吃额度。
        for i in range(GENTLE["LOGIN_IP_FAILURE_LIMIT"] + 1):
            body = self._app(USERNAME, PASSWORD).json()
            self.assertEqual(body["code"], OK, (i, body))
