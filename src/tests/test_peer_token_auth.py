"""`PeerTokenAuthentication` 的单元测试（spec §8.1 / D8 / §9-S2/S7）。

⚠️ 与 HTTP 状态码有关的那几条在 `test_peer_binding_api.py`（Task 4 打真端点）。
⚠️ 必须用 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里 ⇒ 普通 `TestCase`
的 search_path 里没有 `business_*` 表（照 `tests/test_peer_models.py` 的先例）。
⚠️ 判据打在**真实出口**上：认证类唯一的血缘出口是「返回哪个用户 + 把哪一行挂到
`request._peer_binding`」（Task 4 / Task 7 靠后者取绑定，见计划裁决）。
"""
from django.contrib.auth import get_user_model
from django_tenants.test.cases import TenantTestCase
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIRequestFactory

from apps.business.models import PeerDownstreamBinding, PeerUpstreamBinding
from apps.business.peer_sync import hash_token, new_token
from apps.core.authentication import PeerTokenAuthentication

User = get_user_model()
factory = APIRequestFactory()

INBOUND_URL = "/api/business/peer-sync/inbound/events/"


def _request(token: str | None):
    headers = {} if token is None else {"HTTP_AUTHORIZATION": f"Bearer {token}"}
    return factory.post(INBOUND_URL, {}, **headers)


class PeerTokenAuthenticationTests(TenantTestCase):
    def setUp(self):
        self.account = User.objects.create_user(username="peer-account-a", password="x")
        self.raw = new_token()
        self.binding = PeerDownstreamBinding.objects.create(
            account=self.account,
            peer_base_url="https://peer-b.test.invalid",
            token_in_hash=hash_token(self.raw),
            token_out=new_token(),
        )
        self.auth = PeerTokenAuthentication()

    def test_missing_header_returns_none(self):
        """没有头 ⇒ 返回 `None`（匿名），由权限类产出 401。⛔ 不是抛异常。"""
        request = _request(None)
        self.assertIsNone(self.auth.authenticate(request))
        self.assertFalse(hasattr(request, "_peer_binding"))

    def test_non_bearer_header_returns_none(self):
        request = factory.post(INBOUND_URL, {}, HTTP_AUTHORIZATION="Basic abc")
        self.assertIsNone(self.auth.authenticate(request))
        self.assertFalse(hasattr(request, "_peer_binding"))

    def test_wrong_token_raises(self):
        request = _request(new_token())
        with self.assertRaises(AuthenticationFailed):
            self.auth.authenticate(request)
        self.assertFalse(hasattr(request, "_peer_binding"))

    def test_downstream_binding_returns_the_account_and_records_the_row(self):
        """我是上家、对端是下家 ⇒ 令牌是下家给我的 ⇒ 命中 `token_in_hash`。"""
        request = _request(self.raw)
        user, _ = self.auth.authenticate(request)
        self.assertEqual(user.pk, self.account.pk)
        # 既有两路只读这一行，不再查一次库 ⇒ 挂的必须是**命中的那一行**。
        self.assertIsInstance(request._peer_binding, PeerDownstreamBinding)
        self.assertEqual(request._peer_binding.pk, self.binding.pk)
        self.assertEqual(
            request._peer_binding.peer_base_url, "https://peer-b.test.invalid"
        )

    def test_upstream_binding_returns_the_binder_and_records_the_row(self):
        """我是下家、对端是上家 ⇒ 用的是**建绑定的那个客服**（spec §6.1 的 `bound_by`）。

        ⚠️ 绑定人刻意用**另一个用户**：若与下家那个账号同一个人，这条用例在
        「错命中了 `PeerDownstreamBinding`」时也会绿（`AGENTS.md` §5-G：断言要能失败）。
        ⚠️ `raw` 是**上家推单时带的那套**（`token_a`）⇒ 命中 `token_in_hash`（裁决 R15）。
        """
        binder = User.objects.create_user(username="peer-binder-b", password="x")
        raw = new_token()
        upstream = PeerUpstreamBinding.objects.create(
            peer_name="测试工作室甲",
            peer_base_url="https://peer-a.test.invalid",
            peer_account="peer-account-a",
            token_in_hash=hash_token(raw),
            token_out=new_token(),
            local_base_url="https://peer-b.test.invalid",
            bound_by=binder,
        )
        request = _request(raw)
        user, _ = self.auth.authenticate(request)
        self.assertEqual(user.pk, binder.pk)
        self.assertNotEqual(user.pk, self.account.pk)
        self.assertIsInstance(request._peer_binding, PeerUpstreamBinding)
        self.assertEqual(request._peer_binding.pk, upstream.pk)
        self.assertEqual(
            request._peer_binding.peer_base_url, "https://peer-a.test.invalid"
        )

    def test_revoked_binding_is_rejected(self):
        self.binding.state = PeerDownstreamBinding.State.REVOKED
        self.binding.save(update_fields=["state"])
        with self.assertRaises(AuthenticationFailed):
            self.auth.authenticate(_request(self.raw))

    def test_disabled_upstream_binding_is_rejected(self):
        """`enabled=False`（页面上关掉同步）⇒ 令牌立即失效（spec §6.1）。"""
        raw = new_token()
        PeerUpstreamBinding.objects.create(
            peer_name="测试工作室甲",
            peer_base_url="https://peer-a.test.invalid",
            peer_account="peer-account-a",
            token_in_hash=hash_token(raw),
            token_out=new_token(),
            local_base_url="https://peer-b.test.invalid",
            bound_by=self.account,
            enabled=False,
        )
        with self.assertRaises(AuthenticationFailed):
            self.auth.authenticate(_request(raw))

    def test_inactive_account_is_rejected(self):
        """停用账号即同步断 —— 这是 A4（上家侧无界面）下**唯一**的断开手段（spec §9-S8）。"""
        self.account.is_active = False
        self.account.save(update_fields=["is_active"])
        with self.assertRaises(AuthenticationFailed):
            self.auth.authenticate(_request(self.raw))
