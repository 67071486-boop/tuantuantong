"""`apps.core` 的 HTTP 路由。P5 的认证端点挂在 `/api/auth/*`（`apps/core/auth_views.py`）。
请求性能分析的只读聚合端点挂在 `/api/system/request-metrics/`（性能分析线，视图在 `apps/audit/api/request_metric_views.py`——登记在这里的理由见文件末尾那段注释）。
图片存储线的本机 blob 端点挂在 `/api/media/blob/<path:key>`——**仅 `MEDIA_STORAGE_BACKEND=local`时登记**（生产直传 COS，不存在这条写入面），见文件末尾。**为什么曾经是空模块**：测试用的 `tests/p5_probe_urls.py` 里有`path("", include("apps.core.urls"))`，而 `include()` 在 urlconf 载入时就会`import_module`（`django/urls/conf.py`）——本模块不存在，会让每个用探针 urlconf的测试直接 ImportError。所以它必须真实存在、可被 include。"""
from django.conf import settings as dj_settings
from django.urls import include, path

from apps.accounts.wxpusher_hall_views import WxPusherHallSubscriptionView
from apps.accounts.wxpusher_inbox_views import WxPusherInboxReadView, WxPusherInboxView
from apps.audit.api.request_metric_views import ENDPOINT_ROUTE, RequestMetricsView
from apps.business import converters  # noqa: F401  （仅登记路由转换器）
from apps.core import auth_views, profile_views
from apps.core.order_finance_browser import OrderFinanceTicketView
from apps.core.profile_views import (
    MyProfileMediaClaimView,
    MyProfileMediaTicketView,
)
from apps.core import wxpusher_views
from apps.core.wxpusher_login import (
    WxPusherChatContinueView, WxPusherHallContinueView, WxPusherLoginView, WxPusherOrderContinueView,
)

urlpatterns = [
    # 角色端点（`/api/role-authorities/`）。与账号端点同一条前缀（`api/`），
    # 注册顺序不改变匹配结果（两条 router 的 prefix 不同），沿用「业务路由排在认证路由之前」。
    path("api/", include("apps.rbac.urls")),
    # 账号端点（`/api/accounts/`）。放在认证端点**之前**：两条前缀当前不重叠
    # （`api/accounts/` vs `api/auth/`），顺序不影响匹配；先注册是为了将来
    # 前缀出现重叠时，业务路由不会被认证路由抢走。
    path("api/", include("apps.accounts.urls")),
    path("api/auth/login", auth_views.LoginView.as_view(), name="auth-login"),
    path("api/auth/logout", auth_views.LogoutView.as_view(), name="auth-logout"),
    path("api/auth/me", auth_views.MeView.as_view(), name="auth-me"),
    path("api/auth/order-finance-ticket/", OrderFinanceTicketView.as_view(), name="auth-order-finance-ticket"),
    path("api/auth/wxpusher/login", WxPusherLoginView.as_view(), name="auth-wxpusher-login"),
    path(
        "api/auth/wxpusher/orders/<order_no:order_no>/continue", WxPusherOrderContinueView.as_view(),
        name="auth-wxpusher-order-continue",
    ),
    path("api/auth/wxpusher/hall/continue", WxPusherHallContinueView.as_view(), name="auth-wxpusher-hall-continue"),
    path("api/auth/wxpusher/chat/continue", WxPusherChatContinueView.as_view(), name="auth-wxpusher-chat-continue"),
    path("api/auth/wxpusher/guide", wxpusher_views.WxPusherGuideView.as_view(), name="auth-wxpusher-guide"),
    path("api/auth/wxpusher/preferences", wxpusher_views.WxPusherPreferencesView.as_view(), name="auth-wxpusher-preferences"),
    path("api/auth/wxpusher/hall-subscription", WxPusherHallSubscriptionView.as_view(),
         name="auth-wxpusher-hall-subscription"),
    path("api/auth/wxpusher/inbox", WxPusherInboxView.as_view(), name="auth-wxpusher-inbox"),
    path("api/auth/wxpusher/inbox/<int:pk>/read", WxPusherInboxReadView.as_view(), name="auth-wxpusher-inbox-read"),
    path("api/auth/wxpusher/binding", wxpusher_views.WxPusherBindingView.as_view(), name="auth-wxpusher-binding"),
    path("api/auth/wxpusher/binding/start", wxpusher_views.WxPusherBindingStartView.as_view(), name="auth-wxpusher-start"),
    path("api/auth/wxpusher/binding/progress", wxpusher_views.WxPusherBindingProgressView.as_view(), name="auth-wxpusher-progress"),
    path("api/auth/wxpusher/binding/cancel", wxpusher_views.WxPusherLinkCancelView.as_view(), name="auth-wxpusher-link-cancel"),
    path("api/auth/wxpusher/binding/callback", wxpusher_views.WxPusherSubscriptionCallbackView.as_view(),
         name="auth-wxpusher-subscription-callback"),
    path("api/auth/wxpusher/binding/link-code", wxpusher_views.WxPusherLinkCodeView.as_view(),
         name="auth-wxpusher-link-code"),
    path("api/auth/wxpusher/binding/link-confirm", wxpusher_views.WxPusherLinkConfirmView.as_view(),
         name="auth-wxpusher-link-confirm"),
    path("api/auth/wxpusher/binding/scan", wxpusher_views.WxPusherBindingScanView.as_view(), name="auth-wxpusher-scan"),
    path("api/auth/wxpusher/binding/confirm", wxpusher_views.WxPusherBindingConfirmView.as_view(), name="auth-wxpusher-confirm"),
    path("api/auth/wxpusher/binding/test", wxpusher_views.WxPusherBindingTestView.as_view(), name="auth-wxpusher-test"),
    path("api/auth/wxpusher/staff/guide", wxpusher_views.WxPusherGuideView.as_view(
        **wxpusher_views.STAFF_WXPUSHER_VIEW_OPTIONS,
    ), name="auth-wxpusher-staff-guide"),
    path("api/auth/wxpusher/staff/preferences", wxpusher_views.WxPusherStaffPreferencesView.as_view(),
         name="auth-wxpusher-staff-preferences"),
    path("api/auth/wxpusher/staff/binding", wxpusher_views.WxPusherBindingView.as_view(
        **wxpusher_views.STAFF_WXPUSHER_VIEW_OPTIONS,
    ), name="auth-wxpusher-staff-binding"),
    path("api/auth/wxpusher/staff/binding/start", wxpusher_views.WxPusherBindingStartView.as_view(
        **wxpusher_views.STAFF_WXPUSHER_VIEW_OPTIONS,
    ), name="auth-wxpusher-staff-start"),
    path("api/auth/wxpusher/staff/binding/progress", wxpusher_views.WxPusherBindingProgressView.as_view(
        **wxpusher_views.STAFF_WXPUSHER_VIEW_OPTIONS,
    ), name="auth-wxpusher-staff-progress"),
    path("api/auth/wxpusher/staff/binding/test", wxpusher_views.WxPusherBindingTestView.as_view(
        **wxpusher_views.STAFF_WXPUSHER_VIEW_OPTIONS,
    ), name="auth-wxpusher-staff-test"),
    path("api/auth/csrf", auth_views.CsrfView.as_view(), name="auth-csrf"),
    # 站点信息（公开，登录页在登录前要拿工作室名）。与 `csrf` 同属「引导类」端点家族，
    # 放在认证端点之后不改变匹配结果（前缀互不重叠）。
    path("api/auth/site", auth_views.SiteView.as_view(), name="auth-site"),
    # 「我的资料」自助读写（我的资料线，spec §5.1/§5.2）。GET 登录即可，
    # PATCH 另加 `my_profile.edit` 节点门；与认证端点同前缀、互不重叠。
    path("api/auth/profile", profile_views.MyProfileView.as_view(), name="auth-profile"),
    # 自助改密（同线，spec §5.3）。登录 + `home.change_password` 节点门；
    # 平台平面已认证回 404（平面守卫在视图体里，见 `ChangePasswordView.get_permissions`）。
    path("api/auth/password", auth_views.ChangePasswordView.as_view(), name="auth-password"),
    # 图片存储线：头像 / 微信收款码的直传凭证与认领。
    # ⚠️ 两条都是**不带尾斜杠**（与 `api/auth/profile` / `api/auth/password` 同形）。
    # ⚠️ 平面守卫在视图体里 ⇒ `public` 平面回 404（节点门在那里不加，理由见类 docstring）。
    path(
        "api/auth/profile/media/<str:kind>/ticket",
        MyProfileMediaTicketView.as_view(),
        name="auth-profile-media-ticket",
    ),
    path(
        "api/auth/profile/media/<str:kind>",
        MyProfileMediaClaimView.as_view(),
        name="auth-profile-media",
    ),
    # 请求性能分析：`GET /api/system/request-metrics/`——**只读**的请求耗时聚合，准入是 `IsAccountAdmin`（现成判据，⛔ 不新造权限节点）。路径**逐字冻结**（前端 `requestMetrics.ts` 按它写死），而 `api/system/` 不属于任何既有 app 的 urlconf ⇒ 登记在这里。
    # ⚠️ 路由字面量只有 `ENDPOINT_ROUTE` 那一份（在视图模块里）：视图靠它把本端点自己产生的聚合行排除掉 ⇒ 两边**必须**是同一个字符串，⛔ 不要再抄第二份字面量。⚠️ **不能**为它另挂一块 urlconf（`tests/test_operation_log_coverage.py::test_no_undeclared_urlconf_produces_endpoints` 会按「产出 leaf 的父级 include 模块」反查 `COVERAGE_SCOPE`，新模块与「直接写进根 config/urls.py」两条路都会红）；理由的完整口径见 `apps/audit/api/request_metric_views.py` 的模块 docstring。
    path(
        ENDPOINT_ROUTE,
        RequestMetricsView.as_view(),
        name="system-request-metrics",
    ),
]

# 图片存储线的**本机**直传端点。生产（`MEDIA_STORAGE_BACKEND=cos`）下这条 URL **根本不登记** ⇒ 生产上不存在这条写入面：真正的上传是浏览器拿 COS **预签名 PUT** 直传、不经过 Django。
# ⚠️ 判据在**导入期**求值一次（urlconf 只在首次请求时载入）：切换后端必须重启进程、这不是运行期开关——与 settings 里其它后端开关同一条口径。
if dj_settings.MEDIA_STORAGE_BACKEND == "local":
    from apps.core.media_blob_views import MediaBlobView

    urlpatterns += [
        path("api/media/blob/<path:key>", MediaBlobView.as_view(), name="media-blob"),
    ]
