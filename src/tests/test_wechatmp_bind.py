"""网页授权绑定（计划 T4）与 H4 session 判据钉的用例。

## 本文件钉什么

- **S2**：`bind-callback` 的 `state` 必须与本会话此前生成的一致、且**一次性**
  （判据 = 重放同一链接回 403、session 里**没有** openid）；
- **H4（承重钉）**：session 写 openid → Django `authenticate`+`login` → **openid 仍在**
  （`login()` 保留匿名会话数据；django/contrib/auth/__init__.py 的 `login` docstring 与
  `cycle_key` 实现 —— 同批改准了 `apps/core/auth_views.py` 里相反的旧注释）；
- **终态**：`WechatMpBinding` 行、`OperationLog` 行（绑定 / 解绑留痕）；
- **换绑**：openid 已挂在别人名下时 ⇒ 行**搬家**到新账号（旧账号名下那行删除）；
- **零真实网络**：code 换 openid 走 `WechatApiStub`。
"""
from __future__ import annotations

from django.contrib.auth import authenticate, get_user_model, login
from django.contrib.sessions.backends.db import SessionStore
from django.db import connection
from django.test import Client, RequestFactory
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.wechatmp.bind_views import (
    SESSION_APPID_KEY,
    SESSION_OPENID_KEY,
    SESSION_STATE_KEY,
)
from apps.wechatmp.models import WechatMpBinding, WechatMpConfig
from tests._wechatmp_helpers import (
    APPID,
    APPSECRET,
    OPENID,
    TOKEN,
    WechatApiStub,
)

CONFIRM = "/wechatmp/bind-confirm"
ENTRY = "/api/wechatmp/bind-entry"
CALLBACK = "/api/wechatmp/bind-callback"

USER_NAME = "mp-bind-user"
USER_PW = "pw-mp-bind-user"


class BindTestBase(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.stub = WechatApiStub().install()
        self.addCleanup(self.stub.stop)
        WechatMpConfig.objects.create(
            appid=APPID,
            appsecret=APPSECRET,
            token=TOKEN,
            enabled=True,
            tenant_schema=connection.schema_name,
        )
        self.user = get_user_model().objects.create_user(username=USER_NAME, password=USER_PW)
        # ⚠️ 默认 client 的 `SERVER_NAME='testserver'` 不在域名表 ⇒ 租户中间件直接 404
        # （进不了视图）—— 必须钉到 TenantTestCase 自带的那个测试域名。
        self.client = Client(SERVER_NAME="tenant.test.com")

    def prime_state(self, client: Client, state: str = "state-synthetic-0001") -> None:
        """把 bind-entry 会写的那格 session 直接摆好（不发真 OAuth 跳转）。"""
        session = client.session
        session[SESSION_STATE_KEY] = state
        session.save()


class BindEntryTests(BindTestBase):
    def test_entry_redirects_to_wechat_oauth_with_state_in_session(self):
        res = self.client.get(ENTRY)
        self.assertEqual(res.status_code, 302)
        location = res["Location"]
        self.assertTrue(location.startswith("https://open.weixin.qq.com/connect/oauth2/authorize"))
        self.assertIn(f"appid={APPID}", location)
        self.assertIn("scope=snsapi_base", location)
        self.assertIn(f"redirect_uri=", location)
        # state 已进 session（S2 的凭据）。
        self.assertTrue(self.client.session.get(SESSION_STATE_KEY))

    def test_entry_without_config_is_403_page(self):
        WechatMpConfig.objects.all().delete()
        res = self.client.get(ENTRY)
        self.assertEqual(res.status_code, 403)
        self.assertIn("无法完成绑定".encode(), res.content)


class BindCallbackTests(BindTestBase):
    def test_callback_with_matching_state_stores_openid_and_redirects(self):
        self.prime_state(self.client, "state-ok-1")
        self.stub.enqueue({"access_token": "oauth-token-x", "openid": OPENID, "scope": "snsapi_base"})
        res = self.client.get(CALLBACK, {"code": "code-synthetic", "state": "state-ok-1"})
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res["Location"], CONFIRM)
        self.assertEqual(self.client.session.get(SESSION_OPENID_KEY), OPENID)
        self.assertEqual(self.client.session.get(SESSION_APPID_KEY), APPID)
        # 换 openid 的请求形状：sns 路径 + code + appid（stub 记录的是真请求）。
        self.assertIn("sns/oauth2/access_token", self.stub.last_url())
        sent = self.stub.requests[-1]
        self.assertIn("code-synthetic", str(sent.url))

    def test_callback_replays_same_link_is_403(self):
        # 一次性：state 取出即删；重放同一链接（截获的转发链接）必须失败。
        self.prime_state(self.client, "state-once")
        self.stub.enqueue({"access_token": "oauth-token-x", "openid": OPENID})
        first = self.client.get(CALLBACK, {"code": "c1", "state": "state-once"})
        self.assertEqual(first.status_code, 302)
        second = self.client.get(CALLBACK, {"code": "c1", "state": "state-once"})
        self.assertEqual(second.status_code, 403)

    def test_callback_with_foreign_state_is_403_and_stores_no_openid(self):
        # S2：攻击者把自己 code+state 的链接发给受害者 —— 受害者 session 里的 state 对不上。
        self.prime_state(self.client, "state-victim")
        res = self.client.get(CALLBACK, {"code": "attacker-code", "state": "state-attacker"})
        self.assertEqual(res.status_code, 403)
        self.assertIsNone(self.client.session.get(SESSION_OPENID_KEY))

    def test_callback_without_state_is_403(self):
        res = self.client.get(CALLBACK, {"code": "c", "state": "no-session-state"})
        self.assertEqual(res.status_code, 403)
        self.assertIsNone(self.client.session.get(SESSION_OPENID_KEY))

    def test_callback_wechat_failure_envelope_is_403(self):
        # 微信授权失败信封（HTTP 200 + errcode）⇒ 必须被判失败（E-0087）。
        self.prime_state(self.client, "state-err")
        self.stub.enqueue({"errcode": 40029, "errmsg": "invalid code"})
        res = self.client.get(CALLBACK, {"code": "bad-code", "state": "state-err"})
        self.assertEqual(res.status_code, 403)
        self.assertIsNone(self.client.session.get(SESSION_OPENID_KEY))


class BindingUserUniqueTests(BindTestBase):
    """⭐ H1（独立评审 2026-10-19）：`user` 唯一是**数据库级**约束（spec T1）。

    防呆判据：同一账号落第二行必须被 DB 拒（IntegrityError）——此前约束只在
    docstring 里、DB 实态没有 ⇒ 并发换绑可落两行，`update_or_create(user=…)`
    随后抛 `MultipleObjectsReturned`（绑定页 500、推送重复发）。
    """

    def test_second_binding_row_for_same_user_is_rejected_by_db(self):
        from django.db import IntegrityError, transaction

        from apps.wechatmp.models import WechatMpBinding

        WechatMpBinding.objects.create(user=self.user, appid=APPID, openid="openid-first")
        with self.assertRaises(IntegrityError):
            # 内层 atomic 把「事务已中毒」吞在本地，外层事务才能继续做终态复核。
            with transaction.atomic():
                WechatMpBinding.objects.create(
                    user=self.user, appid=APPID, openid="openid-second"
                )
        # 终态：该账号名下仍然只有一行（事务回滚由 TestCase 兜底，这里显式复核一次）。
        self.assertEqual(WechatMpBinding.objects.filter(user=self.user).count(), 1)


class SessionSurvivesLoginTests(BindTestBase):
    """⭐ H4 承重钉：session 写 openid → `authenticate`+`login` → openid 仍在。

    ⛔ 这条防的是「有人照 `auth_views.py` 的旧错误注释把设计改成"登录后重存 openid"」
    —— 以 Django 框架源码为准（login() docstring + cycle_key 保留数据）。
    """

    def test_openid_in_session_survives_authenticate_and_login(self):
        request = RequestFactory().post("/wechatmp/bind-confirm")
        request.session = SessionStore()
        request.session[SESSION_OPENID_KEY] = OPENID
        request.session[SESSION_APPID_KEY] = APPID
        request.session.save()

        user = authenticate(request, username=USER_NAME, password=USER_PW)
        self.assertIsNotNone(user)
        login(request, user)
        # login() 内部 cycle_key 换了 session key，但**数据保留** ⇒ 两个键都还在
        # （这就是 bind-confirm「先拿 openid 再登录」能成立的结构依据）。
        self.assertEqual(request.session[SESSION_OPENID_KEY], OPENID)
        self.assertEqual(request.session[SESSION_APPID_KEY], APPID)


class BindConfirmPageTests(BindTestBase):
    def _logged_in_client(self):
        client = Client(SERVER_NAME="tenant.test.com")
        ok = client.post(
            "/api/auth/login",
            data={"username": USER_NAME, "password": USER_PW},
            content_type="application/json",
        )
        self.assertEqual(ok.status_code, 200, ok.content[:200])
        return client

    def test_anonymous_sees_login_form(self):
        res = self.client.get(CONFIRM)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"action", res.content)
        self.assertIn("登录团团通".encode(), res.content)
        self.assertIn(b"csrfmiddlewaretoken", res.content)

    def test_post_login_without_csrf_token_is_403(self):
        strict = Client(SERVER_NAME="tenant.test.com", enforce_csrf_checks=True)
        res = strict.post(CONFIRM, {"action": "login", "username": USER_NAME, "password": USER_PW})
        self.assertEqual(res.status_code, 403)

    def test_wrong_password_stays_on_login_form_with_message(self):
        res = self.client.post(
            CONFIRM, {"action": "login", "username": USER_NAME, "password": "wrong-pw"}
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("账号或密码不正确".encode(), res.content)
        # ⛔ 终态判据：会话里没有登录态（`_auth_user_id` 不在），不是"响应没说成功"。
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_full_flow_login_then_bind_then_unbind(self):
        client = self._logged_in_client()
        # 授权信息已在 session（bind-callback 写入）。
        session = client.session
        session[SESSION_OPENID_KEY] = OPENID
        session[SESSION_APPID_KEY] = APPID
        session.save()

        # ① GET：确认绑定卡片。
        page = client.get(CONFIRM)
        self.assertIn("确认绑定".encode(), page.content)

        # ② POST bind：落库 + 操作日志。
        res = client.post(CONFIRM, {"action": "bind"})
        self.assertEqual(res.status_code, 302)
        binding = WechatMpBinding.objects.get(user=self.user)
        self.assertEqual(binding.openid, OPENID)
        self.assertEqual(binding.appid, APPID)
        self.assertTrue(binding.subscribed)
        self.assertTrue(
            OperationLog.objects.filter(
                action="system.wechatmp_binding_create", target_id=str(self.user.pk)
            ).exists()
        )

        # ③ 页面变为「已绑定」+ 解绑按钮。
        page = client.get(CONFIRM)
        self.assertIn("已绑定".encode(), page.content)
        self.assertIn("解除绑定".encode(), page.content)

        # ④ 解绑：删行 + 解绑日志。
        res = client.post(CONFIRM, {"action": "unbind"})
        self.assertEqual(res.status_code, 302)
        self.assertFalse(WechatMpBinding.objects.filter(user=self.user).exists())
        self.assertTrue(
            OperationLog.objects.filter(
                action="system.wechatmp_binding_delete", target_id=str(self.user.pk)
            ).exists()
        )

    def test_bind_without_oauth_in_session_shows_error(self):
        client = self._logged_in_client()
        res = client.post(CONFIRM, {"action": "bind"})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(WechatMpBinding.objects.count(), 0)

    def test_rebind_moves_openid_row_to_new_account(self):
        # (appid, openid) 唯一 ⇒ 同一微信号只能挂一个账号；绑到新账号 = 旧行搬家。
        other = get_user_model().objects.create_user(username="mp-bind-other", password="pw-other")
        WechatMpBinding.objects.create(user=other, appid=APPID, openid=OPENID, subscribed=True)
        client = self._logged_in_client()
        session = client.session
        session[SESSION_OPENID_KEY] = OPENID
        session[SESSION_APPID_KEY] = APPID
        session.save()
        res = client.post(CONFIRM, {"action": "bind"})
        self.assertEqual(res.status_code, 302)
        binding = WechatMpBinding.objects.get(openid=OPENID)
        self.assertEqual(binding.user_id, self.user.pk)  # ⛔ 判行主键之外还要判"旧行没了"：
        self.assertFalse(WechatMpBinding.objects.filter(user=other).exists())

    def test_bound_page_shows_rebind_hint_when_openid_differs(self):
        WechatMpBinding.objects.create(
            user=self.user, appid=APPID, openid="openid-synthetic-oldoldold", subscribed=True
        )
        client = self._logged_in_client()
        session = client.session
        session[SESSION_OPENID_KEY] = OPENID  # 另一个微信号
        session[SESSION_APPID_KEY] = APPID
        session.save()
        page = client.get(CONFIRM)
        self.assertIn("重新绑定".encode(), page.content)
