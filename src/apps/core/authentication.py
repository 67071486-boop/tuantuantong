"""会话认证：在 DRF 的 SessionAuthentication 之上，加两道与租户相关的复查。

为什么不能只靠中间件：中间件顺序写错会**静默失效**（功能看起来完全正常）。
本类必须**独立可证**（spec §5.1-7 有一条摘掉中间件的测试专门证明它）。
"""
from django.db import connection
from django.utils import timezone
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication, SessionAuthentication

from apps.business.peer_sync import hash_token, token_matches
from apps.core.app_version import AndroidUpdateRequired, android_version_code, requires_android_update


class TenantBoundAuthentication(SessionAuthentication):
    def authenticate(self, request):
        if getattr(request._request, "order_finance_browser_authenticated", False):
            return request._request.user, None
        result = super().authenticate(request)
        if result is None:
            return None                     # 匿名：交给权限类产出 401
        user, _ = result
        self._assert_plane(request)
        self._assert_tenant_active(request)
        return user, None

    @staticmethod
    def _assert_plane(request):
        """fail-closed 不变式：缺失 identity_plane 一律拒绝（spec D2/§4.3）。"""
        session = request.session
        plane = session.get("identity_plane")
        if "_auth_user_id" in session and plane != connection.schema_name:
            raise exceptions.AuthenticationFailed("会话与当前租户不匹配")

    @staticmethod
    def _assert_tenant_active(request):
        """停用租户上的**已认证**会话一律拒绝。

        只覆盖已认证这一半：匿名请求在 `super().authenticate` 就返回 None，
        匿名也拒由守卫中间件负责。
        """
        tenant = getattr(request, "tenant", None)
        if tenant is not None and not tenant.is_active:
            raise exceptions.AuthenticationFailed("会话与当前租户不匹配")

    def authenticate_header(self, request):
        """返回非空，401 才会带 `WWW-Authenticate`（spec §4.6）。
        `SessionAuthentication` 没有实现它，基类返回 None。DRF 出厂会在header 为空时把认证失败降成 403；
        本项目的信封按异常类型钉死 401，摘掉本方法时状态码仍是 401，消失的只有 `WWW-Authenticate`。保留它是因为 401 要带挑战头，而这条头来自 `get_authenticate_header()`。"""
        return "Session"


class PeerTokenAuthentication(BaseAuthentication):
    """上下游同步端点的认证（spec §8.1 / D8）。## 与 `TenantBoundAuthentication` 的分工⛔ **不是**它的子类、也**不装配到全局**：本类只给同步端点用（逐视图 `authentication_classes`）。会话请求打这些端点必须 **401** —— 这是**有意**的（spec §9-S2）。
    ## 为什么租户边界天然成立令牌的查找发生在**当前请求解析出的租户 schema** 里（`PeerDownstreamBinding.objects`只含本租户的行）⇒ ⛔ **不需要**、也**不允许**再从请求参数里读任何租户标识（`AGENTS.md` §4 / spec §9-S1）。
    ## 一个 `authenticate()` 覆盖两个方向上家侧收到的是**下家给它**的令牌 ⇒ 命中 `PeerDownstreamBinding`（`account` 是人）。下家侧收到的是**上家给它**的令牌 ⇒ 命中 `PeerUpstreamBinding`（`bound_by` 是那个建绑定的客服）。两者都返回一个**真实用户**，这样全局默认的 `IsAuthenticated` 继续有效、不必给同步端点开口子。## 命中的那一行要挂到请求上两个方向都产出 `user` 和 `request._peer_binding`：端点直接读后者取绑定，不再按令牌查第二次（命中的是哪一行只算一遍）。"""

    keyword = "Bearer"

    def authenticate(self, request):
        raw = self._raw_token(request)
        if not raw:
            return None                      # 匿名 ⇒ 交给权限类产出 401
        from apps.business.models import PeerDownstreamBinding, PeerUpstreamBinding

        binding = None
        user = None
        for candidate in PeerDownstreamBinding.objects.filter(
            state=PeerDownstreamBinding.State.ACTIVE
        ).select_related("account"):
            if token_matches(raw, candidate.token_in_hash):
                binding, user = candidate, candidate.account
                break
        if binding is None:
            for candidate in PeerUpstreamBinding.objects.filter(
                deleted_at__isnull=True, enabled=True
            ).select_related("bound_by"):
                # 裁决 R15（取代 R7）：我是下家 ⇒ 收到的令牌是**上家推单时带的 `token_a`** ⇒ 它对应本表的**验证侧** `token_in_hash`（对方向我带的那套，存哈希）。
                # ⛔ `token_out` 是我方**外发**的明文（`token_b`，解绑时带上家），拿它比对等于验证了错的那一列。
                if token_matches(raw, candidate.token_in_hash):
                    binding, user = candidate, candidate.bound_by
                    break
        if binding is None or not user.is_active:
            raise exceptions.AuthenticationFailed("令牌无效")
        # 成功路径上赋值：上面两个分支谁命中，这里挂的就是谁（失败时不留半截状态）。
        request._peer_binding = binding
        return user, None

    @staticmethod
    def _raw_token(request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return ""
        return header[len(prefix):].strip()

    def authenticate_header(self, request):
        """返回非空，401 才会带 `WWW-Authenticate`（与 `TenantBoundAuthentication` 同款理由）。"""
        return self.keyword


class AppTokenAuthentication(BaseAuthentication):
    """打手 App 的 Bearer 令牌认证。## 与 `TenantBoundAuthentication` 的分工⛔ **不是**它的子类，但**装配到全局**（`DEFAULT_AUTHENTICATION_CLASSES` 第一位）：先认 `Authorization: Bearer <token>`，没有这个头就返回 `None`，由后面的 Session 认证接手。两条通道**并存**——现有 Web 端一字不改，App 端走这条。
    ## 为什么不会顺手把 CSRF 打开DRF 只在 `SessionAuthentication` 成功时强制 CSRF。本类不是它的子类 ⇒ Bearer 请求**天然豁免 CSRF**，与「App 不带 Cookie」的现状一致。⚠️ 若客户端同时带会话 cookie 与Bearer，本类排第一位 ⇒ 先按令牌认，不会落到 Session 分支去触发 CSRF 校验。
    ## 租户边界令牌的查找发生在**当前请求解析出的租户 schema** 里（`AppToken.objects` 只含本租户的行）⇒ ⛔ 不需要、也不允许从请求参数读任何租户标识（`AGENTS.md` §4）。另外复查租户是否停用：停用租户上的令牌一律拒绝（与 `TenantBoundAuthentication` 同口径）。## `last_used_at` 的写库节流每次请求都写一行会把读请求变成写请求（SQLite/Postgres 都吃亏）。这里只在「从未记录」或「距上次记录超过 5 分钟」时更新——够用，且不影响任何判据。"""

    keyword = "Bearer"
    #: `last_used_at` 的写库间隔（秒）。改这个不影响认证结果，只影响这一列的精度。
    TOUCH_INTERVAL_SECONDS = 300

    def authenticate(self, request):
        raw = self._raw_token(request)
        if not raw:
            return None                      # 匿名 ⇒ 交给权限类产出 401
        # `AppToken` 只存在于租户库（`TENANT_APPS`）⇒ public 平面上查它会
        # `ProgrammingError: relation "accounts_apptoken" does not exist`（评审 Minor 2）。
        # 平台平面没有 App 客户端，直接按「令牌无效」处理。
        from apps.core.plane import on_public_plane

        if on_public_plane():
            raise exceptions.AuthenticationFailed("令牌无效或已过期")

        from apps.accounts.models import AppToken

        now = timezone.now()
        token = (
            AppToken.objects.select_related("user")
            .filter(
                key_hash=hash_token(raw),
                revoked_at__isnull=True,
                expires_at__gt=now,
            )
            .first()
        )
        if token is None:
            # 「不存在 / 已过期 / 已撤销」对外**同一个文案**：区分它们等于告诉攻击者
            # 哪一枚令牌曾经存在（与 peer 线同一口径）。
            raise exceptions.AuthenticationFailed("令牌无效或已过期")
        if not token.user.is_active:
            raise exceptions.AuthenticationFailed("账号已停用")
        self._assert_tenant_active(request)
        if request.method != "OPTIONS" and requires_android_update(android_version_code(request)):
            raise AndroidUpdateRequired()
        self._touch(token, now)
        return token.user, token

    def authenticate_header(self, request):
        return "Bearer"

    @staticmethod
    def _assert_tenant_active(request):
        tenant = getattr(request, "tenant", None)
        if tenant is not None and not tenant.is_active:
            raise exceptions.AuthenticationFailed("会话与当前租户不匹配")

    def _touch(self, token, now):
        last = token.last_used_at
        if last is not None and (now - last).total_seconds() < self.TOUCH_INTERVAL_SECONDS:
            return
        # `update()` 只写一列，不碰 `expires_at` / `revoked_at`（并发退出登录时不会被本请求覆盖）。
        type(token).objects.filter(pk=token.pk).update(last_used_at=now)

    @staticmethod
    def _raw_token(request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        parts = header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return ""
        return parts[1].strip()
