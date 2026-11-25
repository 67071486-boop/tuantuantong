"""打手 App 认证通道（`/api/app/auth/*`，2026-10-19）。

## 钉住什么

1. **Bearer 通道能独立工作**：登录拿令牌 → 用令牌访问受保护端点；
2. **失效路径**：过期 / 撤销 / 无令牌都是 401；
3. **短信通道**：发码 → 校验 → 一次性；
4. **与 Session 通道并存**：现有业务端点也认 Bearer（App 能用的前提）。

## ⚠️ 令牌明文只在登录响应里出现一次

测试从响应的 `data.token` 取明文——**不从数据库取**（库里只有 sha256）。
这正是生产客户端拿到它的方式，测试走的同一条路。

## ⚠️ 业务失败是 HTTP 200 + 非 0 code

本仓的统一信封把「短信发太频繁」这类业务拒绝也放在 200 里（只有认证层失败才是 401）。
所以失败用例断言的是 `body["code"]`，⛔ 不是状态码。
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.app_sms import issue_login_code
from apps.accounts.models import AppToken, SmsCode, StaffProfile
from apps.business.peer_sync import hash_token
from apps.core.error_codes import ErrorCode

User = get_user_model()

LOGIN_URL = "/api/app/auth/login/"
SMS_SEND_URL = "/api/app/auth/sms/send/"
SMS_LOGIN_URL = "/api/app/auth/sms/login/"
LOGOUT_URL = "/api/app/auth/logout/"
ME_URL = "/api/app/auth/me/"

MOBILE = "13800001111"
PASSWORD = "pw-app-123456"


class AppAuthTestCase(TenantTestCase):
    """⚠️ 每个短信用例都显式 `DEBUG=True`：`console` 后端在 `DEBUG=False` 时会**拒绝发送**
    （生产配错要响亮，见 `test_sms_console_refused_in_prod`）。⛔ 不要改成类级装饰器——
    实测类级 `override_settings(DEBUG=True)` 在本仓的测试运行器下**没有生效**（方法级才有）。
    """

    def setUp(self):
        self.user = User.objects.create_user(username=MOBILE, password=PASSWORD)
        self.domain = self.tenant.get_primary_domain().domain
        # 手机号登录要查 `StaffProfile.mobile`：没有档案的账号只能用用户名登录。
        StaffProfile.objects.create(user=self.user, mobile=MOBILE)

    # ── 工具 ──────────────────────────────────────────────────────
    def _client(self):
        return Client(SERVER_NAME=self.domain)

    def _body(self, response):
        """业务响应一律 200（认证层失败才是 401/403），取信封。"""
        self.assertEqual(response.status_code, 200, response.content[:300])
        return response.json()

    def _assert_ok(self, response):
        body = self._body(response)
        self.assertEqual(body["code"], int(ErrorCode.OK), body)
        return body

    def _post(self, url, payload, **headers):
        return self._client().post(url, payload, content_type="application/json", **headers)

    def _login(self):
        return self._post(LOGIN_URL, {"username": MOBILE, "password": PASSWORD})

    def _bearer(self, token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    # ── 用户名 + 密码 ─────────────────────────────────────────────
    def test_login_returns_token_and_user(self):
        body = self._assert_ok(self._login())
        self.assertTrue(body["data"]["token"])
        self.assertEqual(body["data"]["user"]["username"], MOBILE)
        # 库里只有哈希：明文不该出现在任何一列。
        token = AppToken.objects.get()
        self.assertNotEqual(token.key_hash, body["data"]["token"])
        self.assertEqual(token.key_hash, hash_token(body["data"]["token"]))

    @override_settings(APP_ANDROID_MIN_VERSION_CODE=111)
    def test_legacy_android_login_requires_an_upgrade(self):
        response = self._post(
            LOGIN_URL, {"username": MOBILE, "password": PASSWORD}, HTTP_USER_AGENT="uni-app-x/5.26",
        )
        self.assertEqual(response.status_code, 426)
        self.assertEqual(response.json(), {
            "code": 426,
            "msg": "当前版本已停止服务，请到设置的检查更新安装最新版后继续使用。",
            "data": {"minimum_version_code": 111, "download_url": "https://suqing.suqing.chat/download/"},
        })

    @override_settings(APP_ANDROID_MIN_VERSION_CODE=111)
    def test_existing_app_token_cannot_use_shared_endpoint_from_an_old_native_build(self):
        token = self._assert_ok(self._login())["data"]["token"]
        headers = self._bearer(token)
        response = self._client().get("/api/auth/me", **headers, HTTP_USER_AGENT="uni-app-x/5.26")
        self.assertEqual(response.status_code, 426)
        self.assertEqual(response.json()["data"]["minimum_version_code"], 111)

        for user_agent in ("Mozilla/5.0 (Linux; Android) AppleWebKit/537.36", "MicroMessenger/8.0.68"):
            with self.subTest(user_agent=user_agent):
                response = self._client().get("/api/auth/me", **headers, HTTP_USER_AGENT=user_agent)
                self.assertEqual(self._assert_ok(response)["data"]["user"]["username"], MOBILE)

        with override_settings(APP_ANDROID_MIN_VERSION_CODE=0):
            response = self._client().get("/api/auth/me", **headers, HTTP_USER_AGENT="uni-app-x/5.26")
            self._assert_ok(response)

    @override_settings(APP_ANDROID_MIN_VERSION_CODE=111)
    def test_android_version_boundary_and_non_android_login(self):
        payload = {"username": MOBILE, "password": PASSWORD}
        for version in ("", "110", "111.0", "9" * 11, "٤٤٤"):
            with self.subTest(version=version):
                response = self._post(
                    LOGIN_URL, payload, HTTP_X_TT_APP_PLATFORM="android", HTTP_X_TT_APP_VERSION_CODE=version,
                )
                self.assertEqual(response.status_code, 426)
                self.assertIn("no-store", response.headers["Cache-Control"])

        for version in ("111", "112"):
            with self.subTest(version=version):
                response = self._post(
                    LOGIN_URL, payload, HTTP_X_TT_APP_PLATFORM="android", HTTP_X_TT_APP_VERSION_CODE=version,
                )
                self._assert_ok(response)

        self._assert_ok(self._post(
            LOGIN_URL, payload, HTTP_X_TT_APP_PLATFORM="ios", HTTP_X_TT_APP_VERSION_CODE="8",
            HTTP_USER_AGENT="uni-app-x/5.26",
        ))
        self._assert_ok(self._post(LOGIN_URL, payload, HTTP_USER_AGENT="MicroMessenger/8.0.68"))
        self._assert_ok(self._client().get("/api/auth/site", HTTP_USER_AGENT="uni-app-x/5.26"))
        self.assertEqual(self._client().options(LOGIN_URL, HTTP_USER_AGENT="uni-app-x/5.26").status_code, 200)

    def test_the_token_expiry_is_a_project_timezone_string(self):
        """D-0302（2026-10-24）：登录响应的 `expires_at` 是**项目时区**（+08:00）的 ISO 串。

        App 若展示「有效期至」按串截显；旧写法裸 `.isoformat()` 出 UTC 串、差 8 小时。
        判据 = 响应值与 `local_iso(库里那一行)` 逐字相等且以 `+08:00` 结尾（签发时刻是
        `now + TTL`，不可固定 ⇒ 与库行同源对拍 + 形状钉死）。
        """
        from apps.core.datetime_display import local_iso

        body = self._assert_ok(self._login())
        token = AppToken.objects.get()
        self.assertEqual(body["data"]["expires_at"], local_iso(token.expires_at))
        self.assertTrue(body["data"]["expires_at"].endswith("+08:00"))

    def test_login_wrong_password_is_rejected_with_the_same_message(self):
        body = self._body(self._post(LOGIN_URL, {"username": MOBILE, "password": "wrong"}))
        self.assertEqual(body["code"], int(ErrorCode.VALIDATION))
        # 与「用户不存在」同一句：结构上无法据此判断账号是否存在。
        self.assertEqual(body["msg"], "账号或密码错误")

    def test_login_unknown_user_is_rejected_with_the_same_message(self):
        body = self._body(self._post(LOGIN_URL, {"username": "nobody", "password": PASSWORD}))
        self.assertEqual(body["code"], int(ErrorCode.VALIDATION))
        self.assertEqual(body["msg"], "账号或密码错误")

    def test_me_works_with_the_issued_token(self):
        token = self._assert_ok(self._login())["data"]["token"]
        body = self._assert_ok(self._client().get(ME_URL, **self._bearer(token)))
        self.assertEqual(body["data"]["user"]["username"], MOBILE)

    def test_me_without_token_is_401(self):
        self.assertEqual(self._client().get(ME_URL).status_code, 401)

    def test_me_with_a_bogus_token_is_401(self):
        self.assertEqual(
            self._client().get(ME_URL, **self._bearer("not-a-real-token")).status_code, 401
        )

    def test_expired_token_is_401(self):
        token = self._assert_ok(self._login())["data"]["token"]
        AppToken.objects.filter(key_hash=hash_token(token)).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(self._client().get(ME_URL, **self._bearer(token)).status_code, 401)

    def test_logout_revokes_the_token(self):
        token = self._assert_ok(self._login())["data"]["token"]
        self._assert_ok(self._client().post(LOGOUT_URL, **self._bearer(token)))
        self.assertEqual(self._client().get(ME_URL, **self._bearer(token)).status_code, 401)

    def test_logout_does_not_touch_other_tokens(self):
        first = self._assert_ok(self._login())["data"]["token"]
        second = self._assert_ok(self._login())["data"]["token"]
        self._assert_ok(self._client().post(LOGOUT_URL, **self._bearer(first)))
        # 另一枚仍可用（同一个人的两台设备互不影响）。
        self._assert_ok(self._client().get(ME_URL, **self._bearer(second)))

    def test_bearer_works_on_existing_business_endpoints(self):
        """通道不是只对 `/api/app/*` 有效：现有业务端点也认 Bearer（这是 App 能用的前提）。"""
        token = self._assert_ok(self._login())["data"]["token"]
        body = self._assert_ok(self._client().get("/api/auth/me", **self._bearer(token)))
        self.assertEqual(body["data"]["user"]["username"], MOBILE)

    # ── 短信 ──────────────────────────────────────────────────────
    @override_settings(SMS_BACKEND="console", DEBUG=True)
    def test_sms_send_creates_a_code(self):
        body = self._assert_ok(self._post(SMS_SEND_URL, {"mobile": MOBILE}))
        self.assertIs(body["data"]["sent"], True)
        self.assertEqual(SmsCode.objects.filter(mobile=MOBILE).count(), 1)

    @override_settings(SMS_BACKEND="console", DEBUG=True)
    def test_sms_send_is_limited_to_one_per_minute(self):
        self._assert_ok(self._post(SMS_SEND_URL, {"mobile": MOBILE}))
        body = self._body(self._post(SMS_SEND_URL, {"mobile": MOBILE}))
        self.assertEqual(body["code"], int(ErrorCode.APP_SMS_RATE_LIMITED))
        # 被拒的那次**不落行**：限流不该把用户可用额度提前吃掉。
        self.assertEqual(SmsCode.objects.filter(mobile=MOBILE).count(), 1)

    @override_settings(SMS_BACKEND="console", DEBUG=True)
    def test_sms_send_rejects_a_malformed_mobile(self):
        body = self._body(self._post(SMS_SEND_URL, {"mobile": "12345"}))
        self.assertEqual(body["code"], int(ErrorCode.VALIDATION))

    @override_settings(SMS_BACKEND="console", DEBUG=True)
    def test_sms_login_with_the_issued_code(self):
        code = issue_login_code(mobile=MOBILE)
        body = self._assert_ok(self._post(SMS_LOGIN_URL, {"mobile": MOBILE, "code": code}))
        self.assertTrue(body["data"]["token"])
        self.assertEqual(body["data"]["user"]["username"], MOBILE)

    @override_settings(SMS_BACKEND="console", DEBUG=True)
    def test_sms_code_is_single_use(self):
        code = issue_login_code(mobile=MOBILE)
        self._assert_ok(self._post(SMS_LOGIN_URL, {"mobile": MOBILE, "code": code}))
        body = self._body(self._post(SMS_LOGIN_URL, {"mobile": MOBILE, "code": code}))
        self.assertEqual(body["code"], int(ErrorCode.APP_SMS_CODE_INVALID))

    @override_settings(SMS_BACKEND="console", DEBUG=True)
    def test_sms_login_with_a_wrong_code(self):
        issue_login_code(mobile=MOBILE)
        body = self._body(self._post(SMS_LOGIN_URL, {"mobile": MOBILE, "code": "000000"}))
        self.assertEqual(body["code"], int(ErrorCode.APP_SMS_CODE_INVALID))

    @override_settings(SMS_BACKEND="console", DEBUG=True)
    def test_sms_login_never_auto_registers(self):
        """没账号的手机号：验证码本身有效也要拒（注册只能走自助注册链接）。"""
        other = "13900002222"
        code = issue_login_code(mobile=other)
        body = self._body(self._post(SMS_LOGIN_URL, {"mobile": other, "code": code}))
        self.assertEqual(body["code"], int(ErrorCode.VALIDATION))
        self.assertEqual(body["msg"], "该手机号尚未注册")

    @override_settings(SMS_BACKEND="console", DEBUG=True)
    def test_sms_login_rejects_inactive_user(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        code = issue_login_code(mobile=MOBILE)
        body = self._body(self._post(SMS_LOGIN_URL, {"mobile": MOBILE, "code": code}))
        self.assertEqual(body["code"], int(ErrorCode.VALIDATION))

    # ── 补充用例──────────────────────────────────────
    @override_settings(SMS_BACKEND="console", DEBUG=True)
    def test_sms_console_refused_in_prod(self):
        """`DEBUG=False` 时 console 后端必须**失败**，⛔ 不许假装发出去了。"""
        from django.core.exceptions import ImproperlyConfigured

        from apps.core.sms import send_verification_code

        with override_settings(DEBUG=False):
            with self.assertRaises(ImproperlyConfigured):
                send_verification_code(MOBILE, "123456")

    @override_settings(SMS_BACKEND="console", DEBUG=True)
    def test_sms_code_expires(self):
        """评审 Major 5 / M4 存活：过期过滤必须有判别力。"""
        code = issue_login_code(mobile=MOBILE)
        SmsCode.objects.filter(mobile=MOBILE).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        body = self._body(self._post(SMS_LOGIN_URL, {"mobile": MOBILE, "code": code}))
        self.assertEqual(body["code"], int(ErrorCode.APP_SMS_CODE_INVALID))

    @override_settings(SMS_BACKEND="console", DEBUG=True)
    def test_sms_code_locks_after_five_failures(self):
        """评审 Blocking 3：错 5 次后作废，第 6 次即使猜对也不放行。"""
        code = issue_login_code(mobile=MOBILE)
        for _ in range(5):
            body = self._body(self._post(SMS_LOGIN_URL, {"mobile": MOBILE, "code": "000000"}))
            self.assertEqual(body["code"], int(ErrorCode.APP_SMS_CODE_INVALID))
        body = self._body(self._post(SMS_LOGIN_URL, {"mobile": MOBILE, "code": code}))
        self.assertEqual(body["code"], int(ErrorCode.APP_SMS_CODE_INVALID))

    @override_settings(SMS_BACKEND="console", DEBUG=True)
    def test_sms_daily_limit_per_mobile(self):
        """评审 Major 5：24 小时同号 10 条上限（冷却已由另一条用例覆盖）。"""
        now = timezone.now()
        for _ in range(10):
            SmsCode.objects.create(
                mobile=MOBILE,
                code_hash=hash_token("000000"),
                expires_at=now + timedelta(minutes=5),
            )
        # ⚠️ `created_at` 是 `auto_now_add`，传给 `create()` 会被**忽略**（实测：
        # 不 update 的话这次请求命中的是「60 秒冷却」，两条分支同码，测试恒绿）。
        SmsCode.objects.filter(mobile=MOBILE).update(created_at=now - timedelta(hours=2))
        body = self._body(self._post(SMS_SEND_URL, {"mobile": MOBILE}))
        self.assertEqual(body["code"], int(ErrorCode.APP_SMS_RATE_LIMITED))
        # 按文案区分是哪一档：日限说「今日」，冷却说「60 秒」。
        self.assertIn("今日", body["msg"])

    @override_settings(SMS_BACKEND="console", DEBUG=True)
    def test_sms_ip_limit_uses_x_real_ip(self):
        """评审 Blocking 2：限流按 `X-Real-IP` 分桶，⛔ 不是按 `REMOTE_ADDR`（反代下是 127.0.0.1）。"""
        now = timezone.now()
        ip = "203.0.113.7"
        for _ in range(50):
            SmsCode.objects.create(
                mobile="13911112222",
                code_hash=hash_token("000000"),
                expires_at=now + timedelta(minutes=5),
                request_ip=ip,
            )
        blocked = self._body(
            self._post(SMS_SEND_URL, {"mobile": MOBILE}, HTTP_X_REAL_IP=ip)
        )
        self.assertEqual(blocked["code"], int(ErrorCode.APP_SMS_RATE_LIMITED))
        # 换一个 IP 就放行（证明是按 IP 分桶，而不是全局共享额度）。
        allowed = self._assert_ok(
            self._post(SMS_SEND_URL, {"mobile": MOBILE}, HTTP_X_REAL_IP="203.0.113.8")
        )
        self.assertIs(allowed["data"]["sent"], True)

    @override_settings(SMS_BACKEND="console", DEBUG=True)
    def test_sms_login_matches_by_username_not_by_a_duplicate_mobile(self):
        """评审 Major 4 / M8 存活：同号两份档案时，⛔ 不能登进「id 最小」的另一个人。

        ⚠️ 判别力的关键：**手工账号（只有 `StaffProfile.mobile`）必须比 `username=mobile`
        那个账号更早建**（id 更小）——否则旧实现「取 id 最小」恰好也命中正确的人，测试恒绿。
        """
        mobile2 = "13600003333"
        manual = User.objects.create_user(username="older-manual", password=PASSWORD)
        StaffProfile.objects.create(user=manual, mobile=mobile2)          # id 更小
        right = User.objects.create_user(username=mobile2, password=PASSWORD)
        StaffProfile.objects.create(user=right, mobile=mobile2)          # id 更大
        code = issue_login_code(mobile=mobile2)
        body = self._assert_ok(self._post(SMS_LOGIN_URL, {"mobile": mobile2, "code": code}))
        self.assertEqual(body["data"]["user"]["username"], mobile2)

    @override_settings(SMS_BACKEND="console", DEBUG=True)
    def test_sms_login_falls_back_to_a_unique_staff_mobile(self):
        """手工开的老账号（用户名不是手机号）仍能短信登录——但只在该号**唯一命中**时。"""
        mobile2 = "13600004444"
        manual = User.objects.create_user(username="manual-taker", password=PASSWORD)
        StaffProfile.objects.create(user=manual, mobile=mobile2)
        code = issue_login_code(mobile=mobile2)
        body = self._assert_ok(self._post(SMS_LOGIN_URL, {"mobile": mobile2, "code": code}))
        self.assertEqual(body["data"]["user"]["username"], "manual-taker")

    @override_settings(SMS_BACKEND="console", DEBUG=True)
    def test_sms_login_refuses_an_ambiguous_mobile(self):
        """同号两份档案且都没有 `username=mobile` ⇒ 拒绝（⛔ 不猜是谁）。"""
        mobile2 = "13600005555"
        for name in ("dup-a", "dup-b"):
            user = User.objects.create_user(username=name, password=PASSWORD)
            StaffProfile.objects.create(user=user, mobile=mobile2)
        code = issue_login_code(mobile=mobile2)
        body = self._body(self._post(SMS_LOGIN_URL, {"mobile": mobile2, "code": code}))
        self.assertEqual(body["code"], int(ErrorCode.VALIDATION))
        self.assertEqual(body["msg"], "该手机号尚未注册")

    def test_login_rejects_a_json_array_body(self):
        """评审 Major 1：`data=[]` 不能 500。"""
        response = self._client().post(
            LOGIN_URL, [], content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], int(ErrorCode.VALIDATION))

    @override_settings(SMS_BACKEND="console", DEBUG=True)
    def test_sms_send_rejects_a_json_array_body(self):
        response = self._client().post(
            SMS_SEND_URL, [], content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], int(ErrorCode.VALIDATION))

    @override_settings(SMS_BACKEND="console", DEBUG=True)
    def test_sms_send_ignores_a_malformed_x_real_ip(self):
        """评审 Major 2：`X-Real-IP: not-an-ip` 不能 500（那是公开端点）。"""
        body = self._assert_ok(
            self._post(SMS_SEND_URL, {"mobile": MOBILE}, HTTP_X_REAL_IP="not-an-ip")
        )
        self.assertIs(body["data"]["sent"], True)
        # 解析不了 ⇒ 落库为 NULL，而不是把原始字符串塞进 GenericIPAddressField。
        self.assertIsNone(SmsCode.objects.get(mobile=MOBILE).request_ip)

    @override_settings(SMS_BACKEND="console")
    def test_sms_send_endpoint_is_500_when_console_in_production(self):
        """收口 Minor 4：配置错（生产用 console）必须在**端点层**响亮 500，⛔ 不是 1002。"""
        client = Client(
            SERVER_NAME=self.tenant.get_primary_domain().domain,
            raise_request_exception=False,
        )
        with override_settings(DEBUG=False):
            response = client.post(
                SMS_SEND_URL, {"mobile": MOBILE}, content_type="application/json"
            )
        self.assertEqual(response.status_code, 500)

    @override_settings(SMS_BACKEND="console", DEBUG=True)
    def test_sms_login_ignores_a_soft_deleted_staff_profile(self):
        """收口 Minor 5：`deleted_at` 非空的档案不参与手机号匹配（代码已过滤，补回归）。"""
        mobile2 = "13600006666"
        user = User.objects.create_user(username="deleted-taker", password=PASSWORD)
        StaffProfile.objects.create(user=user, mobile=mobile2, deleted_at=timezone.now())
        code = issue_login_code(mobile=mobile2)
        body = self._body(self._post(SMS_LOGIN_URL, {"mobile": mobile2, "code": code}))
        self.assertEqual(body["code"], int(ErrorCode.VALIDATION))
        self.assertEqual(body["msg"], "该手机号尚未注册")


class AppTokenCrossTenantTests(TenantTestCase):
    """评审 Major 6：租户 A 签发的令牌，在租户 B 的域名下必须是死的。

    ⚠️ B 里**故意**建一条 B 自己的有效令牌：要证的是「按哈希精确命中本租户的行」，
    不是「这个 schema 里碰巧有活行就放行」——否则 B 空库时这条判据恒真。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from apps.platform.provisioning import provision_tenant, seed_tenant

        cls.tenant_b, _ = provision_tenant(
            schema_name="apptok_b", name="AppToken 租户 B", domain="apptok-b.test.local"
        )
        seed_tenant(cls.tenant_b)

    def test_a_token_is_dead_on_b(self):
        from django_tenants.utils import tenant_context

        from apps.accounts.models import AppToken

        domain_a = self.tenant.get_primary_domain().domain
        user_a = User.objects.create_user(username=MOBILE, password=PASSWORD)
        StaffProfile.objects.create(user=user_a, mobile=MOBILE)
        body_a = Client(SERVER_NAME=domain_a).post(
            LOGIN_URL, {"username": MOBILE, "password": PASSWORD}, content_type="application/json"
        ).json()
        token_a = body_a["data"]["token"]

        # B 里放一条自己的活令牌：证明拒绝不是「B 恰好空库」。
        with tenant_context(self.tenant_b):
            user_b = User.objects.create_user(username="b-user", password=PASSWORD)
            AppToken.objects.create(
                user=user_b,
                key_hash=hash_token("b-native-token"),
                expires_at=timezone.now() + timedelta(days=1),
            )
            self.assertIsNotNone(
                AppToken.objects.filter(key_hash=hash_token("b-native-token")).first()
            )

        client_a = Client(SERVER_NAME=domain_a)
        client_b = Client(SERVER_NAME="apptok-b.test.local")
        # 正向对照：同一条令牌在 A 自己的域名下是活的。
        self.assertEqual(
            client_a.get(ME_URL, HTTP_AUTHORIZATION=f"Bearer {token_a}").status_code, 200
        )
        # B 域名下同一条令牌必须 401（B 自己的令牌则可用，作为「B 的通道没坏」的对照）。
        self.assertEqual(
            client_b.get(ME_URL, HTTP_AUTHORIZATION=f"Bearer {token_a}").status_code, 401
        )
        self.assertEqual(
            client_b.get(ME_URL, HTTP_AUTHORIZATION="Bearer b-native-token").status_code, 200
        )
        self.assertEqual(client_b.post(
            "/api/auth/wxpusher/binding/progress", {"challenge": "synthetic-other-workspace-proof"},
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token_a}",
        ).status_code, 401)


class AppEndpointsOnPublicPlaneTests(TenantTestCase):
    """评审 Major 3 / 收口 Major 1：`/api/app/*` 在平台平面（`public`）上必须回
    **401（匿名 + 受保护端点）/ 404（AllowAny 或已认证）**，⛔ 不是 `ProgrammingError` 500。

    `public` 里没有 `accounts_smscode` / `accounts_apptoken` / `taker_wallet` 这些租户表
    （都在 `TENANT_APPS`）⇒ 不挡就是 500。

    ⚠️ **为什么 patch `on_public_plane` 而不是打真实 public 域名**：`TenantTestCase`
    把连接钉在租户 schema，真实请求的中间件切换在本测试基座下不生效（实测打
    `admin.test.local` 仍落在租户 ⇒ 200）。本用例要证的是**守卫分支**存在且回 404，
    平面判定本身由 `test_login_helper` 的既有用例覆盖。
    """

    def _client(self):
        return Client(SERVER_NAME=self.tenant.get_primary_domain().domain)

    def _patched(self):
        from unittest import mock

        return mock.patch("apps.accounts.app_auth_views.on_public_plane", return_value=True)

    def test_sms_send_on_public_is_404(self):
        """`AllowAny` 端点：认证层不拦，平面判据回 404（与既有五处调用点同约定）。"""
        with self._patched():
            response = self._client().post(
                SMS_SEND_URL, {"mobile": MOBILE}, content_type="application/json"
            )
        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertEqual(body["code"], 404)
        # 文案与 `plane.py::plane_not_found_response` 同源：钉字面量，防两处漂移。
        self.assertEqual(body["msg"], "当前平面没有此功能")

    def test_wallet_on_public_is_401_when_anonymous(self):
        """`IsAuthenticated` 端点：**认证先于平面判据** ⇒ 匿名必须 401（不是 404/403）。"""
        with self._patched():
            response = self._client().get("/api/app/wallet/")
        self.assertEqual(response.status_code, 401)

    def test_bearer_on_public_is_401_not_500(self):
        """带一枚**无效**令牌：认证层先拒（401），不会掉进租户表的 `ProgrammingError`。"""
        with self._patched():
            response = self._client().get(ME_URL, HTTP_AUTHORIZATION="Bearer whatever")
        self.assertEqual(response.status_code, 401)

    @override_settings(SMS_BACKEND="console", DEBUG=True)
    def test_sms_login_rejects_a_json_array_body(self):
        """三个 auth 端点同源：`sms/login` 传数组也不能 500。"""
        response = self._client().post(
            SMS_LOGIN_URL, [], content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], int(ErrorCode.VALIDATION))
