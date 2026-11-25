"""账号路由。挂在 `/api/accounts/` 下（由 `apps.core.urls` include）。

与 business 同口径：关掉 API 根视图，避免出现不带 {code,msg,data} 的响应形状。
"""
from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.accounts.app_auth_views import (
    AppLoginView,
    AppLogoutView,
    AppMeView,
    AppRegisterGamesView,
    AppRegisterView,
    AppSmsLoginView,
    AppSmsSendView,
)
from apps.accounts.app_chat_views import AppChatIdentityView
from apps.accounts.app_income_views import (
    AppIncomeSettleRequestsView,
    AppIncomeSummaryView,
)
from apps.accounts.app_wallet_views import (
    AppPayNotifyView,
    AppWalletDepositView,
    AppWxOpenidView,
    AppWalletReleaseView,
    AppWalletTransactionsView,
    AppWalletView,
    AppWalletWithdrawView,
    AppWithdrawListView,
)
from apps.accounts.selfreg_views import (
    InviteListCreateView,
    InviteRevokeView,
    SelfRegisterPublicView,
    SelfRegistrationConfigView,
)
from apps.accounts.views import AccountViewSet

router = DefaultRouter()
router.include_root_view = False
router.register("accounts", AccountViewSet, basename="account")

urlpatterns = [
    *router.urls,
    # 自助注册（D-0237）：配置与链接台账是页面端点（登录 + `system_user.add_edit` 节点门）；
    # `register/` 是**免登录**的公开端点（PeerBindView 先例：`AllowAny` +
    # `authentication_classes=[]`，不发会话所以不需要 CSRF）。
    # ⚠️ 前缀带 `accounts/`：`apps.accounts.urls` 是在 `api/` 下整体 include 的，
    # router 的 `accounts` 前缀只属于 router 注册，显式 `path()` 必须自己带全前缀
    # （漏写的症状是 `/api/accounts/self-registration/…` 整片 404，本次实测踩过）。
    path(
        "accounts/self-registration/config/",
        SelfRegistrationConfigView.as_view(),
        name="self-registration-config",
    ),
    path(
        "accounts/self-registration/invites/",
        InviteListCreateView.as_view(),
        name="self-registration-invites",
    ),
    # ⭐ D-0253：链接「作废」（立即过期）。页面端点（登录 + 同一节点门）；
    # 埋点登记在 `tests/test_operation_log_ledger.py` 的 INSTRUMENTED（同一次改）。
    path(
        "accounts/self-registration/invites/<int:pk>/revoke/",
        InviteRevokeView.as_view(),
        name="self-registration-invite-revoke",
    ),
    path(
        "accounts/self-registration/register/",
        SelfRegisterPublicView.as_view(),
        name="self-registration-register",
    ),
    # ── 打手 App 认证（`feat/taker-app`）──────────────────────────
    # ⚠️ 前缀写全：本模块在 `api/` 下被 include（同上方 self-registration 的说明）。
    # 认证端点与「账号管理」同域是**有意**的：令牌模型 `AppToken` 就住在这个 app 里。
    path("app/auth/login/", AppLoginView.as_view(), name="app-auth-login"),
    # ⭐ 开放注册：免登录、不验短信验证码（业务方拍板「App 注册自己一套」）。
    # 免登录 + 不走会话 ⇒ `authentication_classes=[]` + `AllowAny`（与 login 同形）；
    # 埋点登记在 `tests/test_operation_log_ledger.py` 的 INSTRUMENTED（同一次改）。
    path("app/auth/register/", AppRegisterView.as_view(), name="app-auth-register"),
    # ⭐ 注册页的**匿名只读**游戏下拉：只回本租户未软删的 `{id, name}`，
    # 独立 IP 桶（比注册松）。⛔ 这条路径上不许长出任何写方法 ——
    # `tests/test_operation_log_coverage.py` 的 `MUST_HAVE_NO_WRITE_METHOD` 钉着它。
    path(
        "app/auth/register/games/",
        AppRegisterGamesView.as_view(),
        name="app-auth-register-games",
    ),
    path("app/auth/sms/send/", AppSmsSendView.as_view(), name="app-auth-sms-send"),
    path("app/auth/sms/login/", AppSmsLoginView.as_view(), name="app-auth-sms-login"),
    path("app/auth/logout/", AppLogoutView.as_view(), name="app-auth-logout"),
    path("app/auth/me/", AppMeView.as_view(), name="app-auth-me"),
    # ── 在线客服（Chatwoot 网页小部件）────────────────────────
    # App / 小程序点「联系客服」→ 本端点回一个**后端拼好**的承载页地址（带
    # identifier / 昵称 / identifier_hash）。`identifier_hash` 是 HMAC，密钥只住服务端。
    # 装配 = `IsAuthenticated`（⛔ 不是空列表：空列表会让匿名也放行）+ `AppPlaneMixin`
    # （`public` 平面 404）。spec：`docs/specs/2026-10-21-taker-app-customer-service.md` §2。
    path("app/chat/identity/", AppChatIdentityView.as_view(), name="app-chat-identity"),
    # ── 打手钱包（D-0269 P2）────────────────────────────────────────────
    path("app/wallet/", AppWalletView.as_view(), name="app-wallet"),
    path("app/wallet/release/", AppWalletReleaseView.as_view(), name="app-wallet-release"),
    path("app/wallet/withdraw/", AppWalletWithdrawView.as_view(), name="app-wallet-withdraw"),
    path(
        "app/wallet/withdrawals/",
        AppWithdrawListView.as_view(),
        name="app-wallet-withdrawals",
    ),
    # ── 充值（Jeepay，D-0269 P2 第二批）────────────────────────────────
    # ⭐ 小程序 openid（WX_LITE）：前端拿 `wx.login()` 的 code 来换。
    path("app/wallet/wx-openid/", AppWxOpenidView.as_view(), name="app-wallet-wx-openid"),
    path("app/wallet/deposit/", AppWalletDepositView.as_view(), name="app-wallet-deposit"),
    path(
        "app/wallet/transactions/",
        AppWalletTransactionsView.as_view(),
        name="app-wallet-transactions",
    ),
    # ⚠️ 回调是**免认证**的（它带签名，不带会话/令牌），路径在 `/api/app/pay/` 下。
    path("app/pay/notify/", AppPayNotifyView.as_view(), name="app-pay-notify"),
    # ── 打手「我的收入」/ 提前结算申请（spec §3.2 / §3.4）──────────
    # ⚠️ 前缀写全：本模块在 `api/` 下被 include（同上）。
    # ⚠️ 视图**不放在本 app**：它们读的是 `apps.business` 的绩效与申请行（领域在那里），
    # 只借本模块的 `/api/app/*` 挂载点——与 `app/wallet/*` 的落位（都属于打手 App 面）
    # 一致。⛔ 别为了「就近」把这两条挪到 `apps.business.urls` 下（那会变成
    # `/api/business/app/income/…`，与 App 端的既有前缀契约不符）。
    path(
        "app/income/summary/",
        AppIncomeSummaryView.as_view(),
        name="app-income-summary",
    ),
    path(
        "app/income/settle-requests/",
        AppIncomeSettleRequestsView.as_view(),
        name="app-income-settle-requests",
    ),
]
