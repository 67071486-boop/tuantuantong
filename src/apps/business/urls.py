from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.accounts.wxpusher_admin_views import (
    WxPusherAdminConfigView, WxPusherAdminHistoryView, WxPusherAdminPreviewView,
    WxPusherAdminRecipientsView, WxPusherAdminTestView,
)
from apps.business.api.online_payroll_views import (
    OnlinePayrollBatchCollectionView, OnlinePayrollBatchDetailView, OnlinePayrollCancelView,
    OnlinePayrollMineView, OnlinePayrollPaymentView, OnlinePayrollPreviewView,
)
from apps.business.api.alipay_authorization_views import (
    AlipayAuthorizationActionView, AlipayAuthorizationCollectionView,
)

# ⚠️ **必须 import**：它在模块级 `register_converter(..., "order_no")`，本模块下面两处
# `<order_no:order_no>` 依赖它已登记。见 `apps/business/converters.py` 的 docstring
# （NUL 字节会让 `<str:order_no>` 命中路由并把 `\x00` 送进 PostgreSQL → HTML 500）。
from apps.business import converters  # noqa: F401  （仅为登记转换器）
from apps.business.api.adjustment_views import AdjustmentListView
from apps.business.api.approval_views import (
    ApprovalCollectionView,
    ApprovalDecisionView,
    ApprovalImageReadView,
    ApprovalImageTicketView,
    ApprovalImageUploadView,
    ApprovalOptionsView,
    ApprovalSettingsView,
    ApprovalTargetsView,
)
from apps.business.api.app_stats_views import AppStatsOverviewView
from apps.business.api.article_views import (
    ArticleMediaClaimView,
    ArticleMediaTicketView,
    ArticleViewSet,
    ChannelProfileAdminView,
)
from apps.business.api.config_views import (
    GameServerViewSet,
    GameViewSet,
    ProjectCategoryViewSet,
    ProjectViewSet,
)
from apps.business.api.game_library_views import GameLibraryView
from apps.business.api.feedback_views import (
    FeedbackCollectionView,
    FeedbackHandleView,
    FeedbackTicketView,
)
from apps.business.api.console_views import HomeOverviewView
from apps.business.api.dashboard_views import (
    DashboardGameDistributionView,
    DashboardHallStayView,
    DashboardSyncHealthView,
    DashboardTrendView,
)
from apps.business.api.game_logo_views import (
    GameLogoClaimView,
    GameLogoTicketView,
)
from apps.business.api.hall_claim_target import HallClaimTargetView
from apps.business.api.hall_sim_views import HallSimClaimView
from apps.business.api.jidanpu_member_views import (
    JidanpuMemberBindingView,
    JidanpuMemberPushNowView,
    JidanpuMemberSyncNowView,
    JidanpuMemberTestConnectionView,
)
from apps.business.api.jidanpu_upstream_views import (
    JidanpuUpstreamBindingView,
    JidanpuUpstreamHallClaimView,
    JidanpuUpstreamSyncNowView,
    JidanpuUpstreamTestConnectionView,
)
from apps.business.api.my_orders_views import MyOrdersExportView, MyOrdersViewSet
from apps.business.api.notification_views import (
    NotificationUnreadCountView,
    NotificationViewSet,
)
from apps.business.api.notice_views import SystemNoticeViewSet
from apps.business.api.order_push_views import OrderPushSettingViewSet
from apps.business.api.order_stats_views import OrderStatsView
from apps.business.api.order_views import OrderViewSet
from apps.business.api.service_dashboard_views import ServiceDashboardView
from apps.business.api.qr_code_views import HomeQrCodeViewSet, HomeQrTicketView
from apps.business.api.report_views import BusinessReportView, SupportStatsView
from apps.business.api.peer_direct_taker_views import PeerDirectTakerView
from apps.business.api.peer_views import (
    PeerBindView,
    PeerBindingDetailView,
    PeerBindingListView,
    PeerHallClaimView,
    PeerHallListView,
    PeerInboundEventsView,
    PeerReconcileView,
    PeerUnbindView,
)
from apps.business.api.performance_views import PerformanceViewSet
from apps.business.api.performance_write_views import (
    AlipayDuplicatesView,
    PerformanceDeleteView,
    PerformanceExportView,
    SettlementRevokeView,
)
from apps.business.api.export_job_views import (
    ExportJobCollectionView, ExportJobDownloadView, ExportJobStatusView, Kind as ExportKind,
)
from apps.business.api.order_export_views import OrderExportView
from apps.business.api.platform_integration_views import (
    PlatformIntegrationSettingsView,
    PlatformIntegrationViewSet,
    PushAvailabilityView,
)
from apps.business.api.platform_auto_reply_views import PlatformAutoReplyRuleViewSet
from apps.business.api.dailiantong_push_views import (
    DailiantongAccountSummaryView,
    DailiantongBalanceAlertView,
    DailiantongDefaultZoneView,
    DailiantongPushListView,
    DailiantongPushPreviewView,
    DailiantongPushView,
    DailiantongZoneMapView,
    DailiantongZonesView,
)
from apps.business.api.dailiantong_action_views import (
    DailiantongFinishView,
    DailiantongMessageView,
    DailiantongMessagesView,
    DailiantongPriceView,
    DailiantongProgressView,
    DailiantongRepushView,
    DailiantongTimeView,
    DailiantongWithdrawAllView,
    DailiantongWithdrawView,
)
from apps.business.api.dailiantong_extra_views import (
    DailiantongAgreeCancelView,
    DailiantongCancelWithdrawView,
    DailiantongAmountView,
    DailiantongArbitrationView,
    DailiantongBlacklistView,
    DailiantongEditView,
    DailiantongLockView,
    DailiantongOffSellView,
    DailiantongOrderTemplateDefaultView,
    DailiantongOrderTemplateDetailView,
    DailiantongOrderTemplateListView,
    DailiantongScreenshotView,
    DailiantongStatsView,
    DailiantongUpPriceView,
    DailiantongValueView,
)
from apps.business.api.platform_push_views import (
    PlatformPushCountsView,
    PlatformPushListView,
)
from apps.business.api.platform_push_action_views import PlatformPushActionView
from apps.business.api.platform_order_views import ReadPlatformOrderView
from apps.business.api.recognition_views import (
    RecognitionSettingDetailView,
    RecognitionSettingListView,
)
from apps.business.api.system_switch_views import (
    SystemSwitchDetailView,
    SystemSwitchListView,
)
from apps.business.api.order_qr_config_views import OrderQrConfigView
from apps.business.api.refund_views import RefundView
from apps.business.api.settlement_views import (
    SettlementDailySourcesView,
    SettlementDailySummaryView,
    SettlementExportView,
    SettlementPeopleView,
    SettlementPreviewView,
    SettlementViewSet,
    SettleView,
    UnattributedView,
)
from apps.business.api.screenshot_browser import ScreenshotBrowserBlobView, ScreenshotBrowserViewSet
from apps.business.api.screenshot_views import (
    ScreenshotCollectionView,
    ScreenshotDeleteView,
    ScreenshotTicketView,
)
from apps.business.api.screenshot_zip import ScreenshotZipView
from apps.business.api.customer_views import (
    CustomerAnalysisView,
    CustomerMemberFiltersView,
    CustomerMemberOrdersView,
    CustomerMembersView,
)
from apps.business.api.shop_binding_views import ShopBindingViewSet
from apps.business.api.source_group_views import OrderSourceGroupViewSet
from apps.business.api.source_views import OrderSourceViewSet
from apps.business.api.tb_send_log_views import TaobaoOrderSendLogViewSet
from apps.business.api.upstream_binding_views import (
    JidanpuUpstreamBindingCreateView,
    JidanpuUpstreamBindingDetailView,
    UpstreamBindingListView,
)
from apps.business.api.xgj_receipt_views import (
    XgjReceiptListView,
    XgjReceiptSummaryView,
)
from apps.business.api.upstream_price_log_views import UpstreamPriceChangeLogListView
from apps.business.api.withdraw_admin_views import WithdrawAdminViewSet
from apps.business.api.wallet_admin_views import (
    DepositRuleListView,
    DepositRuleUpdateView,
    TakerDepositQuotaView,
    TakerWalletAdminViewSet,
    WalletTransactionAdminViewSet,
)
from apps.business.api.xgj_views import (
    XgjOrderDetailView,
    XgjShopBindingViewSet,
    XgjShopListViewSet,
)
from apps.business.api.xgj_push_views import XgjOrderPushView, XgjProductPushView

router = DefaultRouter()
# ⚠️ 关掉 DRF 自动生成的 API 根视图：它是个普通 APIView，返回 {"games": url, ...}，
# **不经过 EnvelopeModelViewSet**，于是会给出没有 {code,msg,data} 的响应，
# 在 API 根这一处破坏「所有接口形状一致」的约定。宁可让它 404（有测试钉住）。
router.include_root_view = False
router.register("games", GameViewSet, basename="game")
router.register("servers", GameServerViewSet, basename="server")
router.register("categories", ProjectCategoryViewSet, basename="category")
router.register("projects", ProjectViewSet, basename="project")
router.register("orders", OrderViewSet, basename="order")
# 绩效列表。spec §5 的路径是 `/api/business/performances`，走 router 注册（形状与四个配置集合一致）——⭐ 下同的**结构兜底**只在这里说一遍：
# `test_no_route_500` 的 registry 那条腿会遍历 `router.registry`，
# 走 router 注册的集合自动进入「畸形查询参数不许 500」的覆盖网（⛔ 不要去改那个文件）。
router.register("performances", PerformanceViewSet, basename="performance")
# 订单来源字典。spec §4 的路径是 `/api/business/order-sources/`，**只有这一个入口**：
# 走 router 注册（形状与四个配置集合一致），不另加显式 `path()`——`hall` 的教训正是"同一件事两个入口"，此处照 `performances` 的写法。
router.register("order-sources", OrderSourceViewSet, basename="order-source")
# 店铺对接。spec §6.1 的路径是 `/api/business/shop-bindings/`，走 router 注册
# （形状与四个配置集合一致），**不另加显式 `path()`**。
router.register("shop-bindings", ShopBindingViewSet, basename="shop-binding")
# 闲管家店铺对接。spec §5 的路径是 `/api/business/xgj-bindings/`，走 router 注册
# （形状与四个配置集合一致），**不另加显式 `path()`**。 ⚠️ 前缀用 `xgj-` 而不是 `xianyu-`：`xgj` 是厂商自己用的包名（SDK 的 `xgj/`），
# 与外部文档对得上；`闲管家` 的拼音缩写同名，不是我们发明的词。
router.register("xgj-bindings", XgjShopBindingViewSet, basename="xgj-binding")
# ⭐ 公司店铺分组（计划 `docs/plans/2026-10-26-order-source-groups.md` §3.2）。
# 走 router 注册（与上面几族同形）。权限**复用**来源字典的两条节点（业务方拍板④），不新造节点。
router.register(
    "order-source-groups", OrderSourceGroupViewSet, basename="order-source-group"
)
# ⭐ 跨应用店铺列表（计划 §3.2 / §4）：读端点，只登录（无节点）。
# **只读集合**（`ReadOnlyModelViewSet`）⇒ 路由表只有 list / retrieve，
# 不生成任何写端点（写端点覆盖率网因此不用登记它）。
router.register("xgj-shops", XgjShopListViewSet, basename="xgj-shop")
# 淘宝发货记录（spec `2026-10-03-xgj-auto-ship-design.md` §3.7）。走 router 注册（与上面几族同形）⇒ 两条路一起生成：`GET /tb-send-logs/`（列表）与
# `POST /tb-send-logs/retry/`（重试发货，视图里的 `@action`）。 ⚠️ 只注册这一个集合、只开 list/retry 两个动作（视图是 `GenericViewSet`）：
# `POST /tb-send-logs/`（新增）、`PATCH`/`DELETE` 那些没有业务含义的写入口一个都不生成。
router.register("tb-send-logs", TaobaoOrderSendLogViewSet, basename="tb-send-log")
# 系统内部公告。读 = 登录即可（主页要取最近 3 条）；
# 写 = `system.publish_notice`（逐 action 装在 `SystemNoticeViewSet.get_permissions`）。
router.register("system-notices", SystemNoticeViewSet, basename="system-notice")
# 自动发消息到微信群设置。整个集合共用
# `system.order_push_group_settings` 一个 RBAC 点（含 `<pk>/test/` 那条 `@action`）。
router.register(
    "order-push-settings", OrderPushSettingViewSet, basename="order-push-setting"
)
# 首页二维码横幅（D-0262）。读 = 登录即可（首页人人要渲染，后端按「持不持 home.qr_manage」分档：持点含停用行、不持只含启用行）；
# 写（POST / PUT / PATCH / DELETE）= `home.qr_manage`（逐 action 装在
# `HomeQrCodeViewSet.get_permissions`）。走 router 注册（形状与公告那族一致）。
router.register("home/qr-codes", HomeQrCodeViewSet, basename="home-qr-code")

# 打手提现审核：计划 §4.3 的六条路径全部由这一个集合生成（`withdrawals/`、`.../<pk>/approve|reject|transfer|refresh|mark-paid/`）。
# ⚠️ 读 = 列表（`GET`）；5 个写动作都挂在 `WithdrawAdminViewSet` 上、类体节点门
# `finance.taker_withdraw_review`（fail-closed）。
router.register("withdrawals", WithdrawAdminViewSet, basename="withdrawal-admin")

# 客服后台打手钱包：`GET wallets/` 列表 +
# `POST wallets/<pk>/adjust/`（`@action` 生成，`<pk>` 就是打手的 user id）。
# ⚠️ 两个动作都挂在 `finance.taker_wallet_adjust` 的类体节点门上（fail-closed）。
router.register("wallets", TakerWalletAdminViewSet, basename="wallet-admin")

# 打手钱包**流水明细**读端点：`GET wallet-transactions/`。参数一律过 `_WalletTransactionQuerySerializer` ⇒ 非法值回 200 + `1000`。
# ⚠️ 只注册 list 一个动作（视图是 `GenericViewSet`）⇒ 不生成「有主键」的假形态。 ⚠️ 闸门复用 `finance.taker_wallet_adjust`（同一个类体节点门；⛔ 不新增节点）。
router.register(
    "wallet-transactions",
    WalletTransactionAdminViewSet,
    basename="wallet-transaction-admin",
)

# 频道文章作品：`articles/`、`articles/<pk>/`、
# `articles/<pk>/publish/`、`articles/<pk>/unpublish/`。
# ⚠️ 整个集合（含读）都在 `system.article_manage` 节点门上（`ArticleViewSet.get_permissions`）。
router.register("articles", ArticleViewSet, basename="article")

# 交易平台对接设置（计划 §3-T2）：账号 CRUD 走 router 注册（形状与上面几族一致）。 ⚠️ `GET/PATCH/DELETE …/<id>/` 的主键**只认数字**（视图的 `lookup_value_regex`）：
# `settings/` 是一条字面路径，⛔ 不能被 router 的 detail 模式当成主键吃掉。 ⚠️ `PUT` 在视图上关掉（`http_method_names`），本页只用 PATCH。
router.register(
    "platform-integrations",
    PlatformIntegrationViewSet,
    basename="platform-integration",
)

# 交易平台自动回复规则：`platform-auto-replies/` 与 `…/<id>/` 走 router
# 注册（形状与上面几族一致）。 ⚠️ `PUT` 在视图上关掉（`http_method_names`），页面只用 PATCH（含行内启用开关与批量改内容）。
# ⚠️ 主键只认数字（视图的 `lookup_value_regex`）。
router.register(
    "platform-auto-replies",
    PlatformAutoReplyRuleViewSet,
    basename="platform-auto-reply-rule",
)

urlpatterns = router.urls + [
    path("wxpusher-config/", WxPusherAdminConfigView.as_view(), name="wxpusher-config"),
    path("wxpusher-config/recipients/", WxPusherAdminRecipientsView.as_view(), name="wxpusher-config-recipients"),
    path("wxpusher-config/test/", WxPusherAdminTestView.as_view(), name="wxpusher-config-test"),
    path("wxpusher-config/history/", WxPusherAdminHistoryView.as_view(), name="wxpusher-config-history"),
    path("wxpusher-config/preview/", WxPusherAdminPreviewView.as_view(), name="wxpusher-config-preview"),
    path("approvals/", ApprovalCollectionView.as_view(), name="approval-list"),
    path("approvals/options/", ApprovalOptionsView.as_view(), name="approval-options"),
    path("approvals/targets/", ApprovalTargetsView.as_view(), name="approval-targets"),
    path("approvals/settings/", ApprovalSettingsView.as_view(), name="approval-settings"),
    path("approvals/media/ticket/", ApprovalImageTicketView.as_view(), name="approval-image-ticket"),
    path("approvals/media/upload/<str:ticket>/", ApprovalImageUploadView.as_view(), name="approval-image-upload"),
    path("approvals/<int:pk>/media/<int:index>/", ApprovalImageReadView.as_view(), name="approval-image-read"),
    path("approvals/<int:pk>/decision/", ApprovalDecisionView.as_view(), name="approval-decision"),
    path("online-payroll/preview/", OnlinePayrollPreviewView.as_view()),
    path("online-payroll/batches/", OnlinePayrollBatchCollectionView.as_view()),
    path("online-payroll/batches/<uuid:batch_id>/", OnlinePayrollBatchDetailView.as_view()),
    path("online-payroll/batches/<uuid:batch_id>/payment/", OnlinePayrollPaymentView.as_view()),
    path("online-payroll/batches/<uuid:batch_id>/cancel/", OnlinePayrollCancelView.as_view()),
    path("online-payroll/mine/", OnlinePayrollMineView.as_view()),
    # ── 「一键发单」入口可见性探针 ──────────────────────────────────────────
    # 订单列表那颗地球按钮的**权限门**：只要登录 + `order.list` 就回 `{"available": true}`。 ⛔ 不读平台配置列表（那个端点要页面节点 `system.platform_integration_settings`，
    # 生产上所有客服角色都没有它 ⇒ 按钮一直不画）。 ⚠️ 显式 `path()`、不带路径转换器 ⇒ 自动进入 `test_no_route_500` 的 explicit-routes 腿（未登录时必须是 401 + 信封，不是 500）。 ⚠️ router 的 detail 路由主键只认数字 ⇒ `push-availability/` 不会被当成主键。
    path(
        "platform-integrations/push-availability/",
        PushAvailabilityView.as_view(),
        name="platform-integration-push-availability",
    ),
    # ── 交易平台对接设置 · 9 组全局设置（计划 §3-T2）───────────────────────
    # 显式 `path()`：它是一条单例资源（没有主键、没有增删），挂 router 会多出「有列表、有主键」的假形态。
    # ⚠️ 不带路径转换器 ⇒ 自动进入 `test_no_route_500` 的 explicit-routes 腿（未登录时必须是 401 + 信封，不是 500）。
    path(
        "platform-integrations/settings/",
        PlatformIntegrationSettingsView.as_view(),
        name="platform-integration-settings",
    ),
    # ── 代练通区服映射读口 ────────────────────────────────────────────────
    # 发单页「上次选过的区服自动带出」：按我们自己的 `(game_id, server_id)` 反查记住的代练通 `zone_server_id` + 我方区服名。 ⚠️ 显式 `path()`、不带路径转换器 ⇒ 自动进入 `test_no_route_500` 的 explicit-routes 腿
    # （未登录时必须是 401 + 信封，不是 500）。放在 `zones/` 之前只是为了读起来顺（Django 的 `path()` 按整段匹配，两条不会互相吃掉，顺序不影响结果）。
    path(
        "platform-integrations/zones/map/",
        DailiantongZoneMapView.as_view(),
        name="platform-integration-zone-map",
    ),
    # ── 发单页默认区服（一键抛单计划 §4 / B5）──────────────────────────────
    # 按我方游戏名在代练通区服字典里取第一条（已有 `DailiantongZoneMap` 优先）；匹配不到是**业务结果** `source="none"`，不是错误。
    # ⚠️ 显式 `path()`、不带路径转换器 ⇒ 自动进入 `test_no_route_500` 的 explicit-routes 腿（畸形查询参数必须是 200 + 业务码，不是 500）。
    path(
        "platform-integrations/zones/default/",
        DailiantongDefaultZoneView.as_view(),
        name="platform-integration-default-zone",
    ),
    # ── 发单页右面板账户摘要（一键抛单计划 §4 / B6）───────────────────────
    # 平台 / 账号 / 可用余额 / 冻结余额：余额实时查、30 秒缓存；无配置或查询失败都给
    # 中文原因（⛔ 不 500、⛔ 不带凭据）。
    path(
        "platform-integrations/summary/",
        DailiantongAccountSummaryView.as_view(),
        name="platform-integration-summary",
    ),
    # ── 余额不足预警（运营配套）────────────────────────────────────────────
    # `GET` = 只读：可用余额 vs 「未来 24h 待发单所需」（口径见 `balance_alert` 的 docstring）。
    # ⚠️ 显式 `path()`、不带路径转换器 ⇒ 自动进 `test_no_route_500` 的 explicit-routes 腿。
    path(
        "platform-integrations/balance-alert/",
        DailiantongBalanceAlertView.as_view(),
        name="platform-integration-balance-alert",
    ),
    # ── 代练通区服字典 ────────────────────────────────────────────────────
    # 代理上游 `gamezoneserver`（4327 条，无分页参数）+ 模块级 10 分钟缓存。 ⚠️ 显式 `path()`、不带路径转换器 ⇒ 自动进入 `test_no_route_500` 的 explicit-routes 腿
    # （未登录时必须是 401 + 信封，不是 500）。 ⚠️ router 的 detail 路由主键**只认数字**（`lookup_value_regex`）⇒ `zones/` 不会被当成主键。
    path(
        "platform-integrations/zones/",
        DailiantongZonesView.as_view(),
        name="platform-integration-zones",
    ),
    # ── 第三方平台订单管理页 ──────────────────────────────────────────────
    # `GET` = 本租户的平台推送行（分页 + 平台 / 上游状态 / 关键字筛选）。 ⚠️ 显式 `path()`、不带路径转换器 ⇒ 自动进入 `test_no_route_500` 的 explicit-routes 腿
    # （畸形查询参数必须是 200 + 业务码，不是 500）。
    path(
        "platform-pushes/",
        PlatformPushListView.as_view(),
        name="platform-push-list",
    ),
    # ⭐ fx-D：14 个页签的括号数字一次取回（只读、不调上游）。
    # ⚠️ 必须排在 `platform-pushes/` **之前**？不需要：两条都是精确匹配，互不遮挡。
    # ⚠️ 显式 `path()`、不带路径转换器 ⇒ 自动进 `test_no_route_500` 的 explicit-routes 腿。
    path(
        "platform-pushes/counts/",
        PlatformPushCountsView.as_view(),
        name="platform-push-counts",
    ),
    # ── 第三方平台订单管理页的工具栏动作 ──────────────────────────────────
    # `POST` = 11 颗按钮的批量执行（`action` 分派；逐笔尽力而为、逐笔回原因）。 ⚠️ **显式 `path()`、不带路径转换器** ⇒ 自动进入 `test_no_route_500` 的 explicit-routes 腿（畸形请求体必须是 200 + 业务码，不是 500）。
    # ⚠️ 路径刻意挂在 `platform-pushes/` 之下（页面自己的一批动作），而不是 `orders/batch/<动作>/`：那 4 条是**通用**批量端点（两段式 / 整批回滚），而这里有一半是**不可回滚的上游写动作**（见视图 docstring）。
    path(
        "platform-pushes/actions/",
        PlatformPushActionView.as_view(),
        name="platform-push-actions",
    ),
    # ── 代练通抛单 ────────────────────────────────────────────────────────
    # `POST` = 真发单、`GET` = 读该单全部链路行；两条都带 `<order_no:order_no>` 转换器 ⇒ `test_no_route_500` 的 explicit-routes 腿会跳过它们，畸形入参由
    # `tests/test_dailiantong_push.py` 自己钉（同 `orders/<no>/refunds` 的既有处置）。
    path(
        "orders/<order_no:order_no>/push-to-platform/",
        DailiantongPushView.as_view(),
        name="order-push-to-platform",
    ),
    path(
        "orders/<order_no:order_no>/platform-push/",
        DailiantongPushListView.as_view(),
        name="order-platform-push",
    ),
    # ── 发单页默认值预览（一键抛单 B8）────────────────────────────────────
    # `GET` = 页面默认值的**唯一来源**（与真发单同源；⛔ 不查余额）。 ⚠️ 带 `<order_no:order_no>` 共享转换器 ⇒ `test_no_route_500` 的 explicit-routes 腿
    # 会跳过它，NUL 兜底由 `tests/test_p4_no_500.py` 的「共享转换器路由**恰好**集合」守（同上面两条的处置）。
    path(
        "orders/<order_no:order_no>/platform-push/preview/",
        DailiantongPushPreviewView.as_view(),
        name="order-platform-push-preview",
    ),
    # ── 代练通平台动作 ───────────────────────────────────────────────────
    # 四条都是 `POST`、都带 `<order_no:order_no>` 共享转换器（与上面两条同口径 ⇒ 自动进 `test_p4_no_500` 的「用共享转换器的 order_no 路由**恰好**这些」集合网）。
    # 撤回 = 未接手删除（即时退款）/ 已接手协商撤销；改价只在未接手可用；补时是**增量**；留言 ≤512 字。成败判据都在服务层回读，视图只翻信封。
    path(
        "orders/<order_no:order_no>/platform-push/withdraw/",
        DailiantongWithdrawView.as_view(),
        name="order-platform-push-withdraw",
    ),
    path(
        "orders/<order_no:order_no>/platform-push/price/",
        DailiantongPriceView.as_view(),
        name="order-platform-push-price",
    ),
    path(
        "orders/<order_no:order_no>/platform-push/time/",
        DailiantongTimeView.as_view(),
        name="order-platform-push-time",
    ),
    path(
        "orders/<order_no:order_no>/platform-push/message/",
        DailiantongMessageView.as_view(),
        name="order-platform-push-message",
    ),
    # ⭐ 详情页补充动作 + P3：确认完单 / 重发 / 留言读 / 进度读 /
    #   从所有平台撤回。全部吃共享 `<order_no>` 转换器（与上面几条同口径 ⇒ 自动进
    #   `test_p4_no_500` 的「共享转换器路由恰好集合」，要同一次登记）。
    path(
        "orders/<order_no:order_no>/platform-push/finish/",
        DailiantongFinishView.as_view(),
        name="order-platform-push-finish",
    ),
    path(
        "orders/<order_no:order_no>/platform-push/repush/",
        DailiantongRepushView.as_view(),
        name="order-platform-push-repush",
    ),
    path(
        "orders/<order_no:order_no>/platform-push/messages/",
        DailiantongMessagesView.as_view(),
        name="order-platform-push-messages",
    ),
    path(
        "orders/<order_no:order_no>/platform-push/progress/",
        DailiantongProgressView.as_view(),
        name="order-platform-push-progress",
    ),
    # ⚠️ 路径是 `platform-pushes/`（复数，与列表页的 `/platform-pushes/` 同族），
    #    动作名 `withdraw-all/` 照业务方口径。
    path(
        "orders/<order_no:order_no>/platform-pushes/withdraw-all/",
        DailiantongWithdrawAllView.as_view(),
        name="order-platform-pushes-withdraw-all",
    ),
    # ── 代练通「有、我们没用上」的动作 ─────────────────────────────────────
    # 六条订单动作都吃共享 `<order_no>` 转换器（与上面四条同口径 ⇒ 自动进 `test_p4_no_500` 的「共享转换器路由恰好集合」）。前置状态与回读判据都在服务层：
    # 加价仅未接手 / 锁单仅正在代练 / 上下架仅未接手 / 评价仅已结算 / 拉黑用推送行的 accept_uid。
    path(
        "orders/<order_no:order_no>/platform-push/up-price/",
        DailiantongUpPriceView.as_view(),
        name="order-platform-push-up-price",
    ),
    path(
        "orders/<order_no:order_no>/platform-push/arbitration/",
        DailiantongArbitrationView.as_view(),
        name="order-platform-push-arbitration",
    ),
    # ⭐ 取消撤销（仅我方发起；上游 `LevelOrderCancel(Flag=1)`，无金额/无密码）。
    path(
        "orders/<order_no:order_no>/platform-push/cancel-request/",
        DailiantongCancelWithdrawView.as_view(),
        name="order-platform-push-cancel-request",
    ),
    # ⭐ 同意撤销（打手申请撤销后我方同意；上游 `LevelOrderCancel(Flag=2)`）。
    # ⚠️ 上游**没有拒绝档**（只有 0/1/2）⇒ 这里刻意**不加** reject/cancel-negotiation 的路由。
    path(
        "orders/<order_no:order_no>/platform-push/agree-cancel/",
        DailiantongAgreeCancelView.as_view(),
        name="order-platform-push-agree-cancel",
    ),
    path(
        "orders/<order_no:order_no>/platform-push/value/",
        DailiantongValueView.as_view(),
        name="order-platform-push-value",
    ),
    path(
        "orders/<order_no:order_no>/platform-push/off-sell/",
        DailiantongOffSellView.as_view(),
        name="order-platform-push-off-sell",
    ),
    path(
        "orders/<order_no:order_no>/platform-push/lock/",
        DailiantongLockView.as_view(),
        name="order-platform-push-lock",
    ),
    path(
        "orders/<order_no:order_no>/platform-push/blacklist/",
        DailiantongBlacklistView.as_view(),
        name="order-platform-push-blacklist",
    ),
    # ⭐ 详情页三个补充动作：补款 / 编辑平台单（只非金额字段）/
    #   追加进度截图（`multipart` 上传到代练通 OSS，再回读进度确认）。
    path(
        "orders/<order_no:order_no>/platform-push/amount/",
        DailiantongAmountView.as_view(),
        name="order-platform-push-amount",
    ),
    path(
        "orders/<order_no:order_no>/platform-push/edit/",
        DailiantongEditView.as_view(),
        name="order-platform-push-edit",
    ),
    # ⚠️ 同名端点两个拼写：前端两条线分别按 `edit/` 与 `modify/` 报过契约。
    #    两个都留 ⇒ 谁先接上都能用；行为逐字相同（同一个视图）。
    #    ⚠️ 新增路由必须同一次进 `test_p4_no_500` 的「共享转换器路由恰好集合」。
    path(
        "orders/<order_no:order_no>/platform-push/modify/",
        DailiantongEditView.as_view(),
        name="order-platform-push-modify",
    ),
    path(
        "orders/<order_no:order_no>/platform-push/screenshot/",
        DailiantongScreenshotView.as_view(),
        name="order-platform-push-screenshot",
    ),
    # ── 代练通按日统计（dlt-d）──────────────────────────────────────────
    # 代理上游 `LevelOrderStat`；按「租户 schema + 日期 + 方向」缓存 60 秒。 ⚠️ 显式 `path()`、不带路径转换器 ⇒ 自动进 `test_no_route_500` 的 explicit-routes 腿
    # （畸形 `?start=abc` 必须是 200 + 业务码，不是 500）。
    path(
        "platform-integrations/stats/",
        DailiantongStatsView.as_view(),
        name="platform-integration-stats",
    ),
    # ── 代练通侧订单要求模板（dlt-d）────────────────────────────────────
    # ⚠️ 这是**代练通侧**的模板（`OrderRequireTemp*`），与本仓自己的「代练要求模板」无关。
    # 集合：GET 列表 / POST 新增；明细：PATCH 改 / DELETE 删；`<id>/default/`：POST 设默认。
    path(
        "platform-integrations/order-templates/",
        DailiantongOrderTemplateListView.as_view(),
        name="platform-integration-order-templates",
    ),
    path(
        "platform-integrations/order-templates/<int:temp_id>/",
        DailiantongOrderTemplateDetailView.as_view(),
        name="platform-integration-order-template-detail",
    ),
    path(
        "platform-integrations/order-templates/<int:temp_id>/default/",
        DailiantongOrderTemplateDefaultView.as_view(),
        name="platform-integration-order-template-default",
    ),
    # ── 频道文章：媒体两条 + 频道资料 ────────────────────────────────────────
    # ⚠️ 媒体两条**用显式 `path()`、不用 `@action`**：计划给的路径**没有尾斜杠** （`articles/media/ticket`，与 `games/<id>/logo/ticket` 同形）。`@action` 生成的是
    # 带尾斜杠的那一套，而 POST 被 301 到带斜杠地址时浏览器会丢掉请求体。 ⚠️ 两条都**不带路径转换器** ⇒ 会自动进 `test_no_route_500` 的 explicit-routes 腿（未认证时必须是 401 + 信封，不是 500）。
    path(
        "articles/media/ticket",
        ArticleMediaTicketView.as_view(),
        name="article-media-ticket",
    ),
    path(
        "articles/media/claim",
        ArticleMediaClaimView.as_view(),
        name="article-media-claim",
    ),
    # 频道简介 / 工作室名（写端点只有 `PUT`，读端点只有 `GET`；`name` 取租户名、不落库）。
    path(
        "channel-profile/",
        ChannelProfileAdminView.as_view(),
        name="channel-profile-admin",
    ),
    # ── 游戏 logo 上传 ──────────────────────────────────────────────────────
    # 三步：取票（只签发）→ 客户端 `PUT <upload_url>` 直传 → 认领（写 `Game.logo_key`）。 ⚠️ **显式 `path()`、不挂 `@action`**：`GameViewSet` 是 router 集合，给它加 action 会额外生成 `games/<pk>/logo/` 那套带尾斜杠的形状；本线契约是**没有**尾斜杠
    # （与 `orders/<no>/screenshots` 同形）。 ⚠️ 两条都带 `<int:pk>` 转换器 ⇒ `test_no_route_500` 的 explicit-routes 腿会跳过它们（那条腿自己登记的盲区），畸形入参由 `tests/test_game_logo_api.py` 自己钉。
    path(
        "games/<int:pk>/logo/ticket",
        GameLogoTicketView.as_view(),
        name="game-logo-ticket",
    ),
    path(
        "games/<int:pk>/logo",
        GameLogoClaimView.as_view(),
        name="game-logo-claim",
    ),
    # 游戏管理「新增游戏」的候选清单（只读；游戏库写数据不走 API）。
    path("game-library/", GameLibraryView.as_view(), name="game-library"),
    # 游戏押金金额：读列表 + 改一款。**显式 `path()`**——`GameDepositRule`
    # 是 `Game` 上的一对一规则、没有自己的列表语义，用 ViewSet 只会造出没有意义的 CRUD 入口。
    # ⚠️ 两条都挂同一个节点 `finance.taker_wallet_adjust`（与钱包那两页同一个权限面）。
    path(
        "deposit-rules/",
        DepositRuleListView.as_view(),
        name="deposit-rule-list",
    ),
    path(
        "deposit-rules/<int:game_id>/",
        DepositRuleUpdateView.as_view(),
        name="deposit-rule-update",
    ),
    # ── 打手**个人押金额度**（计划
    # `docs/plans/2026-10-23-taker-deposit-quota-plan.md` §5.1）────────────── 读写同一条路径（GET 看 / PUT 存），**带尾斜杠**（计划给的端点逐字）。 ⚠️ 用显式 `path()`、⛔ 不挂 router：这不是一个集合资源，挂上去会多出 `takers/`、`takers/<pk>/` 那套「有列表、有主键」的假形态。
    # ⚠️ 闸门**复用** `finance.taker_wallet_adjust`（与打手钱包 / 全局押金规则同一个权限面； ⛔ 不新增 RBAC 节点，计划 §5.2），位点见 `apps/rbac/wiring.py` 的 `SITES`。 ⚠️ 主键带 `<int:…>` 转换器 ⇒ `test_no_route_500` 的 explicit-routes 腿会跳过它（那条腿只发 GET 且跳过带转换器的路径），畸形入参由 `tests/test_taker_deposit_quota.py` 自己钉（1002 / 1000，不是 500）。
    path(
        "takers/<int:user_id>/deposit-quota/",
        TakerDepositQuotaView.as_view(),
        name="taker-deposit-quota",
    ),
    # 大厅列表。路径取自 **spec §5 端点表**：`/api/business/hall`。 ⚠️ 计划正文写的是 `GET /api/business/orders/hall/`——两处冲突，按「冲突时以 spec
    # 为准」取了 spec 的路径；但**实现形状**仍按计划的意图：逻辑就是 `OrderViewSet.hall`，复用 `OrderListPagination`（`EnvelopePagination` 的子类，只多一个可选统计块）与 `search_hall`，
    # 没有另建 ViewSet、也没有复制筛选风格。这里显式注册（而不是给 `hall` 加 `@action(detail=False)`）正是为了**只**暴露 spec 的那一个路径——`@action` 会额外生成一个 `orders/hall/`，同一件事两个入口。
    path("hall/", OrderViewSet.as_view({"get": "hall"}), name="order-hall"),
    path("hall-claim-target/", HallClaimTargetView.as_view(), name="hall-claim-target"),
    # 抢单。路径同样取自 **spec §5 的端点表**：`/api/business/hall/{order_no}/claim`。 ⚠️ 与大厅**同一手法**：显式 `path()`，而不是给 `claim` 加 `@action(detail=True)`——
    # `@action` 会额外生成 `orders/{order_no}/claim/`（计划初稿的写法），同一件事两个入口，其中一个与 spec 冲突（控制方 **Ruling 33**）。
    # ⚠️ 标识符用 `order_no` 而不是主键：全线都用订单号作对外标识，大厅列表本来就带它（`OrderListSerializer` 是显式 fields，不带 `id`），用主键只会白暴露一个内部 id。
    path(
        "hall/<order_no:order_no>/claim/",
        OrderViewSet.as_view({"post": "claim"}),
        name="order-claim",
    ),
    # ⛔ **P2（租户级）那条「镜像单接单」路由已不存在**： `hall/remote/<source_system>/<remote_order_id>/claim/` 与它的 `OrderViewSet.remote_claim` 随**租户级**鸡蛋铺线一起拆除（见 `docs/plans/2026-10-08-remove-thirdparty-jidanpu-sync.md`）。
    # ⚠️ 下面这条 `hall/upstream/<pk>/claim/` **不是**它：那是上家线**个人绑定**自己的独立视图，照旧。上家抢单大厅**镜像单**接单（上家鸡蛋铺线；spec §6.1 / §11-29）。 ⚠️ 与上面那条 `hall/<order_no>/claim/` **同一手法 + 同一道节点门**（`grab_hall.take_order`），
    # 但**不是** `OrderViewSet` 的 action：它是本线自建的入口（spec B2，⛔ 不复用 P2 的 `claim_remote`），落在 `JidanpuUpstreamHallClaimView`（独立 `APIView`）。 ⚠️ 用 `<int:pk>`（本线镜像表的主键）：带路径转换器 ⇒ `test_no_route_500` 的 explicit-routes 腿会跳过它（那条腿只发 GET），所以「只有 POST」由 `tests/test_jidanpu_upstream_hall.py` 自己钉。
    path(
        "hall/upstream/<int:pk>/claim/",
        JidanpuUpstreamHallClaimView.as_view(),
        name="order-upstream-claim",
    ),
    # ⭐ 团团通上家大厅**镜像单**接单（D-0303；spec §4.1 / §7）。 ⚠️ 与上面两条 hall claim **同一手法**（显式 `path()` + `<int:pk>` 路径转换器，
    # explicit-routes 腿只发 GET ⇒ 「只有 POST」由 `tests/test_peer_hall_claim.py` 自己钉）；同一道节点门（`grab_hall.take_order`）。
    # ⛔ **不是**鸡蛋铺那条 `hall/upstream/<pk>/claim/` 的别名：那是鸡蛋铺线（同步上游 HTTP + 转正后才返回），本条是团团通绑定线（确认制：预占 + 排 `hall.claim` 事件就返回「接单中」，正式订单由上家 `order.assign` 推回来）。
    path(
        "hall/peer/<int:pk>/claim/",
        PeerHallClaimView.as_view(),
        name="order-peer-claim",
    ),
    # ⭐ 大厅**模拟单**假接手（D-0296）。 ⚠️ 与上面两条 hall claim **同一手法**（显式 `path()` + `<int:pk>` 路径转换器，
    # explicit-routes 腿只发 GET ⇒ 「只有 POST」由 `tests/test_hall_simulated.py` 自己钉）；
    # 同一道节点门（`grab_hall.take_order`）。**无论结果都回 1012**——那是本端点的设计（演示卡的「接手」就是下架 + 一句「已被接手」），理由见视图 docstring。
    path(
        "hall/sim/<int:pk>/claim/",
        HallSimClaimView.as_view(),
        name="order-hall-sim-claim",
    ),
    # 站内通知（B2-1）。路径取自 **spec §5 端点表**：`/api/business/notifications`
    # ⚠️ 与大厅/抢单**同一手法**：显式 `path()`，不给 ViewSet 加 `@action`——
    #    `@action` 会额外生成一套 `notifications/{pk}/...` 路由。
    path(
        "notifications/",
        NotificationViewSet.as_view({"get": "list"}),
        name="notification-list",
    ),
    # 未读通知数（打手 App 底部「消息」角标）。只读、认证态。
    # ⚠️ 与下面那条 `notifications/<int:notification_id>/read/` **不冲突**（那条的主键段是
    #    `<int:…>`，`unread-count` 匹配不上），顺序放在列表后面只为读起来「列表 → 计数 → 已读」。
    path(
        "notifications/unread-count/",
        NotificationUnreadCountView.as_view(),
        name="notification-unread-count",
    ),
    path(
        "notifications/<int:notification_id>/read/",
        NotificationViewSet.as_view({"post": "read"}),
        name="notification-read",
    ),
    # 我接的单（B2-2）。路径取自 **spec §5 端点表**：`/api/business/my-orders`。
    # ⚠️ 与大厅/抢单/通知**同一手法**：显式 `path()`，不给 ViewSet 加 `@action`
    #    （`@action` 会额外生成 `performances/...` 之类的第二套入口）。
    path(
        "my-orders/",
        MyOrdersViewSet.as_view({"get": "list"}),
        name="my-orders",
    ),
    # ── 打手受限订单详情（booster-h5，spec §6.2）────────────────────────
    # ⚠️ 与上面那条**同一手法**：显式 `path()` 挂同一个 ViewSet 的详情动作， ⛔ 不给 `my-orders/` 挂 router（那会生成一整套 `{pk}` 假形态）。 ⚠️ **动作名是 `order_detail`、URL 仍叫 `…/detail/`**：`detail` 是 DRF ViewSet 的保留 initkwarg（`ViewSetMixin.as_view` 里 `cls.detail = None` 会把类上的同名
    # 方法覆盖成 `None` ⇒ 每个请求 `TypeError`，本线实测踩到）。 ⚠️ 标识符用 `order_no`（共享转换器 `<order_no:order_no>`，与抢单 / 退款同款）：只挡控制字符、不强制 20 位形状（形状闸在前端路由，spec §6.2 的更正块）；带转换器 ⇒ `test_no_route_500` 的 explicit-routes 腿跳过它（假红防护），并进 `tests/test_p4_no_500.py` 的「共享转换器路由**恰好**集合」。 ⚠️ **带尾斜杠**（spec §6.2 逐字：`/api/business/my-orders/<order_no>/detail/`）。
    path(
        "my-orders/<order_no:order_no>/detail/",
        MyOrdersViewSet.as_view({"get": "order_detail"}),
        name="my-order-detail",
    ),
    # ── 群二维码按需同步（打手 App）──────────────────────────────────────
    # 打手点开自己的上家单、本地**没有**群二维码时调一次（`POST`，空请求体）。 ⚠️ 仍是**显式 `path()` + 同一个 ViewSet 的命名方法**（⛔ 不给 ViewSet 加 `@action`，理由同上面两条：`@action` 会额外生成第二套入口）。
    # ⚠️ 它只**入队**一条 `upstream.pull_pics_in`，请求里一次上游都不打（口径见视图 docstring）。 ⚠️ 标识符同样用共享转换器 `<order_no:order_no>` ⇒ `test_no_route_500` 的 explicit-routes 腿跳过它，但必须同一次登记进 `tests/test_p4_no_500.py` 的「共享转换器路由**恰好**集合」。
    path(
        "my-orders/<order_no:order_no>/screenshots/sync/",
        MyOrdersViewSet.as_view({"post": "sync_screenshots"}),
        name="my-order-screenshots-sync",
    ),
    # ── 批量申请提前结算（⭐ D-0365；网页端「我的收入」页工具栏）────
    # ⚠️ 仍是**显式 `path()` + 同一个 ViewSet 的命名方法**（⛔ 不给 ViewSet 加 `@action`，理由同上面三条：`@action` 会额外生成第二套入口）。 ⚠️ **带尾斜杠**（与同族三条 `my-orders/…` 一致；无路径转换器 ⇒ 进
    # `test_no_route_500` 的 explicit-routes 腿，POST-only 的形状由该腿的 `_request_method` 按框架事实推导——`settlements/settle` 先例）。
    path(
        "my-orders/settle-requests/",
        MyOrdersViewSet.as_view({"post": "settle_requests"}),
        name="my-order-settle-requests",
    ),
    # ── 「我的订单收入」导出（⭐ D-0365）──────────────────────
    # ⚠️ 独立 `APIView`（`MyOrdersExportView`），照 `PerformanceExportView` 的形状—— 成功回 xlsx 字节、失败回信封 + HTTP 200（同一个 URL 两种形状）。
    # ⚠️ **没有尾斜杠**（照 `performances/export` / `orders/export` 先例）；无路径转换器 ⇒ 进 `test_no_route_500` 的 explicit-routes 腿（畸形查询参数必须 200 + 信封）。
    path(
        "my-orders/export",
        MyOrdersExportView.as_view(),
        name="my-orders-export",
    ),
    # 结账（P4-7）。路径取自 **spec §5 端点表**：`/api/business/settlements/settle` （⚠️ spec 写的就是**没有尾斜杠**的形状，前端契约按它）。挂 `SettleView.post`。
    # ⚠️ 这是全仓**第一条「只接受 POST 的字面显式路由」**——`test_no_route_500` 的 explicit-routes 腿原先假定「非转换器的显式路由都接受 GET」，
    # 见该文件 `_explicit_routes()` 的 `accepts_get` 判据（那条腿已按这条事实收窄）。
    path(
        "settlements/settle",
        SettleView.as_view(),
        name="settlement-settle",
    ),
    # ── 撤销结账（D-0072 ②）──────────────────────────────────
    # ⚠️ 路径取自 brief §1 A，**没有**尾斜杠（与 `/settlements/settle` 同形）。 ⚠️ 与正向结账**共用**节点 `finance.change_settlement_status` （对标 `settBtn` 就是同一颗「更改结算状态」按钮的两个方向）。
    # ⚠️ 只接受 POST ⇒ 它会**逃出** `test_no_route_500` 的 explicit-routes 腿（那条腿只发 GET，判据见该文件），所以畸形入参由 `tests/test_p4fu_performance_writes.py` 自己钉。
    path(
        "settlements/revoke",
        SettlementRevokeView.as_view(),
        name="settlement-revoke",
    ),
    # ── 本线（T2.1）：结算预览（**只读**）────────────────────────────
    # ⚠️ **没有**尾斜杠（与 `/settlements/settle`、`/settlements/revoke` 同形）：路径逐字取自计划 §3 T2.1，前端契约按它。 ⚠️ 闸门 = `finance.payroll`（读节点，与下面三条只读接口同一颗）；
    # `settle` / `revoke` 两个写端点的 `finance.change_settlement_status` 一个字没动。 ⚠️ 它接受 GET ⇒ 自动进入 `test_no_route_500` 的 explicit-routes 腿（畸形查询参数必须 200 + 信封）——本视图过序列化器，天然满足。
    path(
        "settlements/preview",
        SettlementPreviewView.as_view(),
        name="settlement-preview",
    ),
    # ── 结算的三个**只读**接口（spec §7）──────────────────────────────
    # ⚠️ 三条路径都取自 spec §7 的端点表，**没有**尾斜杠（与 `/settlements/settle` 同形）。 ⚠️ 与大厅/抢单/通知**同一手法**：显式 `path()`，不给 ViewSet 加 `@action`——
    # `@action` 会额外生成 `settlements/{pk}/...` 之类的第二套入口。 ⚠️ 这两条字面路径会**自动进入** `test_no_route_500` 的 explicit-routes 腿（它们接受 GET）；那条腿的账号前提已随本任务调整为超级管理员（它还要覆盖 `/hall/` 的代练员闸门与这里的 `has_node` 闸门）。
    path(
        "settlements",
        SettlementViewSet.as_view({"get": "list"}),
        name="settlement-list",
    ),
    path("settlements/export", SettlementExportView.as_view({"get": "list"}), name="settlement-export"),
    path(
        "settlements/unattributed",
        UnattributedView.as_view({"get": "list"}),
        name="settlement-unattributed",
    ),
    # ── D-0089：「结账对象人员」候选（只读）────────────────────────────
    # ⚠️ **没有**尾斜杠（与 `/settlements`、`/settlements/settle` 同形）；与 `path("settlements", …)` 是两条精确路径，**不冲突**。
    # ⚠️ 它接受 GET ⇒ 自动进入 `test_no_route_500` 的 explicit-routes 腿（畸形查询参数必须 200 + 信封）——本视图过序列化器，天然满足。
    path(
        "settlements/people",
        SettlementPeopleView.as_view(),
        name="settlement-people",
    ),
    # ── D-0319：结算记录页主视图的按天聚合（只读）──────────────
    # ⚠️ **没有尾斜杠**（与 `/settlements`、`/settlements/people` 同形）；与 `path("settlements", …)` 是两条精确路径，**不冲突**。 ⚠️ 闸门与数据范围与 `/settlements` 完全同闸（`finance.payroll` + 同一套
    # `scope_queryset`）；显式 `path()`、不给 ViewSet 加 `@action`（同一手法）。 ⚠️ 它接受 GET ⇒ 自动进入 `test_no_route_500` 的 explicit-routes 腿（畸形查询参数必须 200 + 信封）——本视图过序列化器，天然满足。
    path(
        "settlements/daily-summary",
        SettlementDailySummaryView.as_view({"get": "list"}),
        name="settlement-daily-summary",
    ),
    # ── D-0323：某一天的总结算金额按订单来源分解（只读）──────────────
    # ⚠️ **没有尾斜杠**（与 `/settlements/daily-summary` 同形）；与它、与 `path("settlements", …)` 都是**字面精确路径**，互不冲突。 ⚠️ 闸门与数据范围与 `/settlements` / `daily-summary` 完全同闸（`finance.payroll` +
    # 同一套 `scope_queryset`）；聚合基集逐字复用 daily-summary 的（那边拍板过的口径， ⛔ 不在这里自创）。显式 `path()`、不给 ViewSet 加 `@action`（同一手法）。 ⚠️ 它接受 GET ⇒ 自动进入 `test_no_route_500` 的 explicit-routes 腿（畸形查询参数必须 200 + 信封）——本视图过序列化器，天然满足。
    path(
        "settlements/daily-summary/sources",
        SettlementDailySourcesView.as_view(),
        name="settlement-daily-sources",
    ),
    # ⚠️ 带路径转换器 ⇒ `test_no_route_500` 的 explicit-routes 腿**跳过它**（那是假红防护），所以畸形参数由 `tests/test_settlement_api.py` 自己钉一次（同 `/adjust/` 的处置）。
    # ── `POST` 同一条路径 = 人工修正（spec §7 把 GET/POST 画成同一行）────── ⚠️ **同一条路径一个 view 两个方法**（裁定 W）；⛔ 不要为它再放第二个 `path()`。 ⚠️ 两个方法的权限节点不同（读 `finance.payroll` / 写 `finance.change_settlement_status`），
    # 由 `AdjustmentListView.get_permissions()` 按 `self.action` 分派。 ⚠️ 畸形入参（`amount` / `reason` / `request_id`）由 `tests/test_adjustment_api.py` 的 M7 钉。
    path(
        "performances/<int:performance_id>/adjustments",
        AdjustmentListView.as_view({"get": "list", "post": "create"}),
        name="performance-adjustments",
    ),
    # ── 删除记录 / 导出记录（D-0072 ① + brief §1 C）────────────
    # ⚠️ 两条路径取自 brief，**没有**尾斜杠（与 `/settlements/settle` 同形）。 ⚠️ **与 router 的 `performances/<pk>/` 的边界**：router 的 detail 路由**带**尾斜杠，而这两条字面路径**不带** ⇒ 精确路径不冲突。反过来，客户端若多打一个尾斜杠（`/performances/delete/`）会命中 router 的 detail 路由（`pk="delete"`）而 404/405——
    # 这是本仓「字面路径不带尾斜杠」的既有形状（`/settlements/settle` 同），前端契约按 brief 给的两串原样调。 ⚠️ **`/performances/export` 是本仓第一条「成功时不是信封」的端点** （`text/csv` 文件流，见视图的 docstring）：它**接受 GET** ⇒ 会进入 `test_no_route_500` 的 explicit-routes 腿（畸形查询参数必须 200 + 信封）。导出是 xlsx 附件，不是 csv。GET 和畸形参数仍是 200 + 信封。
    path(
        "performances/delete",
        PerformanceDeleteView.as_view(),
        name="performance-delete",
    ),
    path(
        "performances/export",
        PerformanceExportView.as_view(),
        name="performance-export",
    ),
    # ⚠️ **`/orders/export` 与 `/performances/export` 同形**：字面 `path()`、**不带尾斜杠**
    #    （spec §2-4 实测：router 的 `@action` 形态都带尾斜杠，本线刻意不用它）。
    # ⚠️ 它**接受 GET** ⇒ 会进入 `test_no_route_500` 的 explicit-routes 腿（畸形查询参数必须 200 + 信封）。
    path(
        "orders/export",
        OrderExportView.as_view(),
        name="order-export",
    ),
    path(
        "orders/export-jobs/",
        ExportJobCollectionView.as_view(kind=ExportKind.ORDERS),
        name="orders-export-job-collection",
    ),
    path(
        "orders/export-jobs/<uuid:job_id>/",
        ExportJobStatusView.as_view(kind=ExportKind.ORDERS),
        name="orders-export-job-status",
    ),
    path(
        "orders/export-jobs/<uuid:job_id>/download/",
        ExportJobDownloadView.as_view(kind=ExportKind.ORDERS),
        name="orders-export-job-download",
    ),
    path(
        "settlements/export-jobs/",
        ExportJobCollectionView.as_view(kind=ExportKind.SETTLEMENTS),
        name="settlements-export-job-collection",
    ),
    path(
        "settlements/export-jobs/<uuid:job_id>/",
        ExportJobStatusView.as_view(kind=ExportKind.SETTLEMENTS),
        name="settlements-export-job-status",
    ),
    path(
        "settlements/export-jobs/<uuid:job_id>/download/",
        ExportJobDownloadView.as_view(kind=ExportKind.SETTLEMENTS),
        name="settlements-export-job-download",
    ),
    # ── 检测重复收款支付宝（只读）──────────────────────────────
    # ⚠️ 路径取自 brief §2 C 的**默认建议** `/api/business/performances/alipay-duplicates`：对标的「重复收款支付宝子账号检测」弹层就长在绩效页上 ⇒ 挂在 `performances/` 下与页面归属一致；**没有**尾斜杠（与 `export` / `delete` / `/settlements/settle` 同形）。
    # ⚠️ 与 router 的 detail 路由（`performances/<pk>/`，带尾斜杠）不冲突：精确路径不带尾斜杠，客户端多打一个尾斜杠会命中 router 的 detail 路由而 404/405（同 export 的既有形状）。 ⚠️ 它**接受 GET** ⇒ 自动进入 `test_no_route_500` 的 explicit-routes 腿（畸形查询参数必须 200 + 信封）——本端点**不读**任何查询参数，天然满足。
    path(
        "performances/alipay-duplicates",
        AlipayDuplicatesView.as_view(),
        name="performance-alipay-duplicates",
    ),
    # ── P4-9：部分退款（spec §7 的端点表）────────────────────────────
    # `POST`＝部分退款、`GET`＝该订单的退款记录；**同一条路径两个方法**（spec 表里就是一行）。 ⚠️ 路径取自 spec §7，**没有**尾斜杠（与 `/settlements/settle` 同形）。 ⚠️ 标识符用 `order_no` 而不是主键：全线都用订单号作对外标识。 ⚠️ 与大厅/抢单/通知**同一手法**：显式 `path()`，不给 ViewSet 加 `@action`——
    # `@action` 会额外生成 `orders/{pk}/refunds/` 之类的第二套入口。 ⚠️ 带路径转换器 ⇒ `test_no_route_500` 的 explicit-routes 腿**跳过它**（假红防护），所以畸形入参由 `tests/test_refund_partial.py` 自己钉一次（同 `/adjust/` 与 `/performances/{id}/adjustments` 的处置）。
    path(
        "orders/<order_no:order_no>/refunds",
        RefundView.as_view({"get": "list", "post": "refund"}),
        name="order-refunds",
    ),
    # ── 复制成新订单（管理订单页右键菜单，spec §8）────────────────────
    # ⚠️ 与大厅/抢单/退款**同一手法**：显式 `path()`，不给 ViewSet 加 `@action` （`@action` 会额外生成一条 `orders/<pk>/copy/` 的别名路径）。 ⚠️ 路径**不带尾斜杠**（与 `/settlements/settle`、`/orders/<no>/refunds` 同形）。
    # ⚠️ 带路径转换器 ⇒ `test_no_route_500` 的 explicit-routes 腿会跳过它，所以畸形入参由 `tests/test_order_copy_api.py` 自己钉。
    path(
        "orders/<order_no:order_no>/copy",
        OrderViewSet.as_view({"post": "copy"}),
        name="order-copy",
    ),
    # ── 读单。路径 = `/api/business/read-platform-order/`（独立 ViewSet + 显式 `path()`）。
    # ⚠️ 实现形状照 `hall/` 的手法：**显式 `path()` + `GenericViewSet.as_view({...})`**，不给它加 `@action`——那会额外生成一条别名路径，同一件事两个入口（见本文件 `hall/<order_no:order_no>/claim/` 那条注释记的同一教训）。
    # ⚠️ 这是**只接受 POST 的字面路径** ⇒ `test_no_route_500` 的 explicit-routes 腿会跳过它（该腿只发 GET，判据见它自己的注释），所以**畸形入参由 `tests/test_read_platform_order.py` 自己钉**（spec §10-10）——不要指望那张结构兜底网覆盖到它。
    path(
        "read-platform-order/",
        ReadPlatformOrderView.as_view({"post": "read_platform_order"}),
        name="read-platform-order",
    ),
    # ── 闲管家订单详情（spec §5）──────────────────────────
    # 路径 = `/api/business/xgj/order-detail/`（独立 ViewSet + 显式 `path()`）。 ⚠️ 与读单/大厅/抢单**同一手法**：显式 `path()` + `GenericViewSet.as_view({...})`，
    # 不给它加 `@action`——那会额外生成 `xgj-bindings/{pk}/order-detail/` 这样的第二套入口。 ⚠️ 它是**只接受 POST 的字面路径** ⇒ `test_no_route_500` 的 explicit-routes 腿会跳过它（该腿只发 GET），所以畸形入参由 `tests/test_xgj_shop_binding.py` 自己钉。
    path(
        "xgj/order-detail/",
        XgjOrderDetailView.as_view({"post": "order_detail"}),
        name="xgj-order-detail",
    ),
    # ── 闲管家**推送接收端**（spec `2026-10-04-xgj-push-inbound-design.md` §4）──
    # 两条都由**闲管家的服务器**调（不是浏览器）：URL 由对接页生成并只读展示（`XGJ_ORDER_PUSH_PATH` / `XGJ_PRODUCT_PUSH_PATH` 是**同一对**字面量， ⚠️ 两处必须同时改；本线刻意**不**为「只留一份字面量」去挪那两个常量，理由见 spec §7 的 ⚠️ 段）。 ⚠️ 它们**免登录、免 CSRF**（视图类里 `authentication_classes = []` + `AllowAny`；
    # DRF 的 `APIView.as_view()` 自套 `csrf_exempt`）⇒ 安全边界完全在领域层的验签上。 ⚠️ 两条都是**只收 POST 的字面路由** ⇒ `test_no_route_500` 的 explicit-routes 腿按框架事实给它们发 POST；而它们回的是**第三方形状**（`{"result", "msg"}`、没有 `code`） ⇒ 该腿为这一类路由单独走 `VENDOR_SHAPED_ROUTES` 那条分支（见那个文件）。
    path(
        "xgj/push/order/",
        XgjOrderPushView.as_view(),
        name="xgj-push-order",
    ),
    path(
        "xgj/push/product/",
        XgjProductPushView.as_view(),
        name="xgj-push-product",
    ),
    # ── 闲管家**收款明细**读端点（spec §6）────────────
    # 路径 = `/api/business/xgj-receipts/summary/` 与 `/api/business/xgj-receipts/` （照本文件既有的 kebab-case + 字面 `path()` 习惯，**带尾斜杠**、与推送端点同段）。 ⚠️ 与 order-stats **同一手法**：显式 `path()` + `APIView.as_view()`，不挂 router
    # （router 会额外生成「有列表、有主键」的假形态）。 ⚠️ 两条都**接受 GET** ⇒ 自动进入 `test_no_route_500` 的 explicit-routes 腿（畸形查询参数必须 200 + 信封）——日期参数一律过序列化器（缺参/畸形 ⇒ 200 + `1000`）。 ⚠️ 闸门 = 第七条自有节点 `finance.xgj_receipt_detail`（spec §4-D8；位点见 `apps/rbac/wiring.py` 的 `SITES`）。
    path(
        "xgj-receipts/summary/",
        XgjReceiptSummaryView.as_view(),
        name="xgj-receipt-summary",
    ),
    path(
        "xgj-receipts/",
        XgjReceiptListView.as_view(),
        name="xgj-receipt-list",
    ),
    # ── 客户（会员）读端点（2026-11-10 客户线）────────────
    # 路径 = `/api/business/customers/members/` 与 `/customers/member-filters/`（kebab-case +
    # 字面 `path()`，带尾斜杠——照 xgj-receipts 同段习惯）。 ⚠️ 与 order-stats **同一手法**：
    # 显式 `path()` + `APIView.as_view()`，不挂 router（router 会额外生成「有主键」的假形态）。
    # 两条都**接受 GET** ⇒ 自动进入 `test_no_route_500` 的 explicit-routes 腿（畸形查询参数必须
    # 200 + 信封）——入参一律过序列化器（缺参/畸形 ⇒ 200 + `1000`），天然满足。
    # ⚠️ 闸门 = RBAC 节点 `customer.member_list`（目录现 `available=False` ⇒ 实际只有管理员能过；
    # 翻真挂账，冻结目录未动）。
    path(
        "customers/members/",
        CustomerMembersView.as_view(),
        name="customer-members",
    ),
    path(
        "customers/member-filters/",
        CustomerMemberFiltersView.as_view(),
        name="customer-member-filters",
    ),
    # ── 客户订单抽屉（2026-11-10 第二批）────────────
    # 同一手法（显式 `path()` + APIView、GET 进 explicit-routes 腿、同一节点闸）。
    path(
        "customers/member-orders/",
        CustomerMemberOrdersView.as_view(),
        name="customer-member-orders",
    ),
    # ── 客户分析报表（2026-11-10 第三批）────────────
    # 同一手法（显式 `path()` + APIView、GET 进 explicit-routes 腿、同一节点闸；
    # `range` 过序列化器 ⇒ 畸形值回 200 + `1000`）。钉点在 test_no_route_500.py。
    path(
        "customers/analysis/",
        CustomerAnalysisView.as_view(),
        name="customer-analysis",
    ),
    # ── 接单统计（对标 `GetRecOrderTotalList`）─────────────
    # 路径 = `/api/business/order-stats`（照本文件既有的 kebab-case + 字面 `path()` 习惯； **没有**尾斜杠，与 `settlements` / `performances/export` 同形）。 ⚠️ 与大厅/抢单/通知**同一手法**：显式 `path()` + `APIView.as_view()`，不挂 `@action`
    # （那会额外生成第二套入口）。 ⚠️ 它**接受 GET** ⇒ 自动进入 `test_no_route_500` 的 explicit-routes 腿（畸形查询参数必须 200 + 信封）——本端点对缺参/畸形参给的是 **200 + `1000`**（刻意不静默），所以那条腿对它是「真的被覆盖」，不是「恰好通过」。
    path("order-stats", OrderStatsView.as_view(), name="order-stats"),
    # ── 客服接发单统计 / 经营报表（D-0247）────────────────
    # 两条都**不带尾斜杠**（与 `order-stats` 同形；`APPEND_SLASH` 只补不剥）。闸门都是 `finance.support_statistics`（本线把它从「功能未实现」翻真）。
    # ⚠️ 它们**接受 GET** ⇒ 自动进入 `test_no_route_500` 的 explicit-routes 腿；参数一律过序列化器 ⇒ 畸形值回 200 + `1000`（不是 500）。
    path(
        "support-stats",
        SupportStatsView.as_view(),
        name="support-stats",
    ),
    path(
        "business-report",
        BusinessReportView.as_view(),
        name="business-report",
    ),
    # ── 「APP 与小程序 → 数据概览」────────────────────────
    # 路径 = `/api/business/app-stats/overview/`（业务方给的端点表逐字，**带尾斜杠**）。闸门**复用** `finance.support_statistics`（计划 §8 第一条：⛔ 不新增节点）。
    # ⚠️ 它**接受 GET** ⇒ 自动进入 `test_no_route_500` 的 explicit-routes 腿； `days` 过序列化器 ⇒ 畸形值 / 越界回 200 + `1000`（不是 500）。
    path(
        "app-stats/overview/",
        AppStatsOverviewView.as_view(),
        name="app-stats-overview",
    ),
    # ── 后台首页「数据概览」（本线 T2；对标 `/Index/console` 的 12 格）──────────
    # 路径 = `/api/business/home/overview/`（照计划 §4-T2 的端点表逐字；`home/` 前缀让「这是首页那一屏的数据」一眼可辨）。⚠️ **带尾斜杠**（与 `my-orders/`、 `notifications/` 同形；`order-stats` 那条不带是它自己的历史形状，别互相「统一」）。 ⚠️ 与大厅/抢单/通知**同一手法**：显式 `path()` + `APIView.as_view()`，不挂 router
    # （router 会额外生成 `/home/overviews/{pk}/…` 那套「有列表、有主键」的假形态）。 ⚠️ 它**接受 GET** ⇒ 自动进入 `test_no_route_500` 的 explicit-routes 腿（畸形查询参数必须 200 + 信封）——本视图过序列化器（缺参 / 形状错 ⇒ 200 + `1000`），天然满足。 ⚠️ 闸门 = `IsAccountAdmin`（`is_account_admin`，**不是** RBAC 节点）：接线图里 `home.index` 仍登记在 `PENDING`（见 `apps/rbac/wiring.py` 那条理由）。
    path(
        "home/overview/",
        HomeOverviewView.as_view(),
        name="home-overview",
    ),
    # ── 后台首页 v2 看板 · 四条聚合只读端点（V2Dashboard 接真数据）──────────
    # 路径照 `home/overview/` 的同段同形：`dashboard/` 前缀 + kebab-case + 尾斜杠 + 显式
    path(
        "dashboard/service/",
        ServiceDashboardView.as_view(),
        name="dashboard-service",
    ),
    # `path()`（不挂 router——聚合读端点没有「列表/主键」形态）。⚠️ 四条都**接受 GET**
    # ⇒ 自动进入 `test_no_route_500` 的 explicit-routes 腿（畸形查询参数必须 200 + 信封）：
    # trend / game-distribution 过序列化器（缺参/畸形 ⇒ 200 + `1000`），hall-stay /
    # sync-health 不读参数，天然满足。⚠️ 闸门统一 = `IsAccountAdmin`（与 `home/overview/`
    # 同一份判据）。实现与逐键口径见 `api/dashboard_views.py`。
    path(
        "dashboard/trend/",
        DashboardTrendView.as_view(),
        name="dashboard-trend",
    ),
    path(
        "dashboard/game-distribution/",
        DashboardGameDistributionView.as_view(),
        name="dashboard-game-distribution",
    ),
    path(
        "dashboard/hall-stay/",
        DashboardHallStayView.as_view(),
        name="dashboard-hall-stay",
    ),
    path(
        "dashboard/sync-health/",
        DashboardSyncHealthView.as_view(),
        name="dashboard-sync-health",
    ),
    # ── 首页二维码横幅 · 直传票据（D-0262）────────────────────
    # ⚠️ **带尾斜杠**、显式 `path()`（照 `feedback/media/ticket/` 的先例：签发票据只有一个动作，挂 router 会额外生成「有列表、有主键」的假形态）。 ⚠️ 它只收 POST ⇒ `test_no_route_500` 的 explicit-routes 腿会**给它发 POST**
    # （`_request_method` 按视图允许的方法选，无需任何豁免登记）；入参过 `HomeQrTicketSerializer` ⇒ 畸形 body 回 200 + `1000`（不是 500）。
    path(
        "home/qr-codes/media/ticket/",
        HomeQrTicketView.as_view(),
        name="home-qr-ticket",
    ),
    # ── 录单识别设置（spec §5）────────────────────────────
    # 页面只有"看列表 / 改一行"，没有增删 ⇒ **两个显式 `path()`**，不挂 router ViewSet （那样会多出 POST / DELETE / PUT 三条 405 路由，把"没有这个功能"说成"方法不对"）。
    # ⚠️ `item_code` 只来自 URL：请求体不接受它，端点也不接受任何 tenant 参数（AGENTS.md §4）。 ⚠️ GET 那条**不带**转换器 ⇒ 自动进入 `test_no_route_500` 的 explicit-routes 腿（畸形查询参数必须 200 + 信封）——本视图不读任何查询参数，天然满足。
    path("alipay-authorizations/", AlipayAuthorizationCollectionView.as_view()),
    path("alipay-authorizations/<int:pk>/authorize/", AlipayAuthorizationActionView.as_view(), {"operation": "authorize"}),
    path("alipay-authorizations/<int:pk>/refresh/", AlipayAuthorizationActionView.as_view(), {"operation": "refresh"}),
    path("alipay-authorizations/<int:pk>/identity/", AlipayAuthorizationActionView.as_view(), {"operation": "identity"}),
    path("alipay-authorizations/<int:pk>/identity-refresh/", AlipayAuthorizationActionView.as_view(), {"operation": "identity-refresh"}),
    path("alipay-authorizations/<int:pk>/revoke/", AlipayAuthorizationActionView.as_view(), {"operation": "revoke"}),
    path("recognition-settings/", RecognitionSettingListView.as_view()),
    path("recognition-settings/<str:item_code>/", RecognitionSettingDetailView.as_view()),
    # ── 系统设置开关（D-0223）──────────────────────────────
    # 与上面识别设置同一形状：页面只有"看列表 / 改一行"，没有增删 ⇒ 两个显式 `path()`。 ⚠️ `key` 只来自 URL：请求体不接受它，端点也不接受任何 tenant 参数（AGENTS.md §4）。
    # ⚠️ GET 那条不带转换器 ⇒ 自动进入 `test_no_route_500` 的 explicit-routes 腿（本视图不读任何查询参数，天然满足）。
    path("system-switches/", SystemSwitchListView.as_view()),
    path("system-switches/<str:key>/", SystemSwitchDetailView.as_view()),
    # ── 马上来查码配置（D-0348）────────────────────────────
    # 单行配置只有"看 / 存"两跳 ⇒ 两个方法住同一个显式 `path()`（RefundView 同款先例）。
    # ⚠️ GET 那条不带转换器 ⇒ 自动进入 `test_no_route_500` 的 explicit-routes 腿（本视图不读任何查询参数，天然满足）。
    path("order-qr/config/", OrderQrConfigView.as_view()),
    # ── 鸡蛋铺 · 两条**个人绑定线**（下家 / 上家；spec §9.x）────────────────
    # ⛔ **下面四条「租户级」路由已整组拆除**（`jidanpu-binding/`、 `jidanpu-binding/test-connection/`、`jidanpu-sync/events/`、`jidanpu-sync/links/`）：它们与其视图 / 序列化器随租户级鸡蛋铺线一起拆除（见 `docs/plans/2026-10-08-remove-thirdparty-jidanpu-sync.md`）。 ⚠️ 留下来的两条个人绑定线（下面 `member/*` 与 `member/upstream-*`）**不受影响**。
    # ── 下游鸡蛋铺 · **代练员个人绑定**（「我的资料 → 功能设置」；spec §9.2）────── ⚠️ 与已拆除的租户级那条 `jidanpu-binding/` **本来就零关系**（spec §4.1）：两条线各自读写自己的表，⛔ 不共享行 / 开关。路径多一层 `member/` 让两者一眼可分。 ⚠️ 与大厅/抢单/读单同一手法：显式 `path()` + `APIView.as_view()`，不挂 router （router 会额外生成 `/member/jidanpu-bindings/{pk}/…` 那套「有列表、有主键」的假形态）。 ⚠️ 两个动词分在两个视图上（GET/PUT 一条、`test-connection` 的 POST 一条），与已拆除的租户级那条同一理由：合成一个类会让「POST 这条路径到底干什么」在路由层歧义。
    path(
        "member/jidanpu-binding/",
        JidanpuMemberBindingView.as_view(),
        name="member-jidanpu-binding",
    ),
    path(
        "member/jidanpu-binding/test-connection/",
        JidanpuMemberTestConnectionView.as_view(),
        name="member-jidanpu-binding-test-connection",
    ),
    # ⭐ 下游鸡蛋铺线（spec §9.3）：「指派给我的订单」页工具栏那颗「立即同步」。 ⚠️ 独立一条路径（⛔ 不挂在 `member/jidanpu-binding/` 上）：那条路径只收 GET/PUT，
    # 再给它加 POST 会让「POST 这条路径到底干什么」在路由层歧义（同 T3 的理由）。 ⚠️ 它**只作用于 `request.user` 那一行**、⛔ 不接受任何用户 / 租户参数；
    # 内部走的是慢路那段代码本身（`jidanpu_member.pull_statuses`）。
    path(
        "member/jidanpu-sync-now/",
        JidanpuMemberSyncNowView.as_view(),
        name="member-jidanpu-sync-now",
    ),
    # ⭐ 下游鸡蛋铺线（计划 `2026-10-23-taker-my-orders-push.md` §2）：「指派给我的订单」工具栏那两颗按钮的**手动推送**（`record` 录单 / `status` 推状态）。
    # ⚠️ 独立一条路径（⛔ 不挂在 `member/jidanpu-binding/` 上，理由同上面那条）：那条路径只收 GET/PUT，再给它加 POST 会让「POST 这条路径到底干什么」在路由层歧义。
    # ⚠️ 它**只作用于 `request.user` 那一行**、⛔ 不接受任何用户 / 租户参数；逐单归属判据走既有的 `order_scope.claimed_perf_of`，推送走既有的 outbox 引擎（本视图只做入参校验 → 排事件 → 跑一轮 → 汇总）。
    path(
        "member/jidanpu-push-now/",
        JidanpuMemberPushNowView.as_view(),
        name="member-jidanpu-push-now",
    ),
    # ── 上家鸡蛋铺（**客服个人绑定**）· 「我的资料 → 功能设置」（spec §9.1 / §9.3）──
    # ⚠️ 与上面两条 `member/*` 是**两条独立产品线**（spec §4.1）：各自读写自己的表， ⛔ 不共享行 / 开关；路径多一段 `upstream-` 让两者一眼可分。 ⚠️ 与大厅/抢单/读单同一手法：显式 `path()` + `APIView.as_view()`，不挂 router
    # （router 会额外生成 `/member/upstream-jidanpu-bindings/{pk}/…` 那套假形态）。 ⚠️ 两个动词分在两个视图上（GET/PUT 一条、`test-connection` 的 POST 一条），与上面两条同一理由：合成一个类会让「POST 这条路径到底干什么」在路由层歧义。
    path(
        "member/upstream-jidanpu-binding/",
        JidanpuUpstreamBindingView.as_view(),
        name="member-upstream-jidanpu-binding",
    ),
    path(
        "member/upstream-jidanpu-binding/test-connection/",
        JidanpuUpstreamTestConnectionView.as_view(),
        name="member-upstream-jidanpu-binding-test-connection",
    ),
    # ⭐ 上家鸡蛋铺线（spec §9.3）：本人那一行的「立即同步」。 ⚠️ 独立一条路径（⛔ 不挂在 `member/upstream-jidanpu-binding/` 上）：那条路径只收 GET/PUT，
    # 再给它加 POST 会让「POST 这条路径到底干什么」在路由层歧义（同上面两条的理由）。 ⚠️ 它**只作用于 `request.user` 那一行**、⛔ 不接受任何用户 / 租户参数；
    # 内部走的是慢路那段代码本身（`jidanpu_upstream.pull_statuses`）。
    path(
        "member/upstream-jidanpu-sync-now/",
        JidanpuUpstreamSyncNowView.as_view(),
        name="member-upstream-jidanpu-sync-now",
    ),
    # ⛔ **`jidanpu-upstream/overview/` 这条路由已不存在**：它是上家的 **租户级只读总览**（管理员视角的跨客服汇总），业务方裁定随租户级鸡蛋铺线一并拆除（视图 `JidanpuUpstreamOverviewView` 同一次删；见 `docs/plans/2026-10-08-remove-thirdparty-jidanpu-sync.md`）。
    # ⚠️ 上面那三条 `member/*` 是**个人绑定线**，照旧（只作用于 `request.user` 那一行）。 ── 上下游订单互通（spec §8.1/§8.2）──────────────────────────────── 上家侧握手端点。⚠️ 两条都**带尾斜杠**（spec §8 的端点表）。 ⚠️ 与大厅/抢单/读单同一手法：显式 `path()` + `APIView.as_view()`，不挂 router/
    # 不加 `@action`——那会额外生成第二套入口。 ⚠️ 两条都靠 `PeerTokenAuthentication` / `bind/` 的账号密码，**都不认会话 cookie** （spec §9-S2）⇒ `test_no_route_500` 的 explicit-routes 腿对它们是**特例** （无令牌时正确答复是 401，不是它通用的 200）；该腿里已按框架事实显式登记。
    path(
        "peer-sync/bind/",
        PeerBindView.as_view(),
        name="peer-bind",
    ),
    path(
        "peer-sync/unbind/",
        PeerUnbindView.as_view(),
        name="peer-unbind",
    ),
    # 下家侧**接收**端点（spec §8.3）。⚠️ 带尾斜杠。 ⚠️ 与上面两条**同组**：只认 Bearer 令牌、⛔ 不认会话 cookie（spec §9-S2）
    # ⇒ 它在 `test_no_route_500` 的 explicit-routes 腿里也是「特例」（无令牌时正确答复是 401，不是那条腿通用的 200），已在该腿的 `TOKEN_ONLY_ROUTES` 里显式登记。
    # ⚠️ 与大厅/抢单/读单同一手法：显式 `path()` + `APIView.as_view()`，不挂 `@action` （那会额外生成第二套入口）。
    path(
        "orders/<order_no:order_no>/peer-direct-assignment/",
        OrderViewSet.as_view({"get": "peer_direct_assignment_options", "post": "peer_direct_assignment"}),
        name="order-peer-direct-assignment",
    ),
    path(
        "orders/<order_no:order_no>/peer-direct-assignment/search/",
        OrderViewSet.as_view({"post": "peer_direct_assignment_search"}),
        name="order-peer-direct-assignment-search",
    ),
    path(
        "orders/<order_no:order_no>/jidanpu-direct-assignment/",
        OrderViewSet.as_view({"get": "jidanpu_direct_assignment_options", "post": "jidanpu_direct_assignment"}),
        name="order-jidanpu-direct-assignment",
    ),
    path(
        "orders/<order_no:order_no>/jidanpu-direct-assignment/search/",
        OrderViewSet.as_view({"post": "jidanpu_direct_assignment_search"}),
        name="order-jidanpu-direct-assignment-search",
    ),
    path(
        "orders/<order_no:order_no>/jidanpu-direct-assignment/verify/",
        OrderViewSet.as_view({"post": "jidanpu_direct_assignment_verify"}),
        name="order-jidanpu-direct-assignment-verify",
    ),
    path(
        "orders/peer-direct-assignment/options/",
        OrderViewSet.as_view({"get": "peer_direct_assignment_draft_options"}),
        name="order-peer-direct-assignment-draft-options",
    ),
    path(
        "orders/peer-direct-assignment/search/",
        OrderViewSet.as_view({"post": "peer_direct_assignment_draft_search"}),
        name="order-peer-direct-assignment-draft-search",
    ),
    path("peer-sync/direct-taker/", PeerDirectTakerView.as_view(), name="peer-direct-taker"),
    path(
        "peer-sync/inbound/events/",
        PeerInboundEventsView.as_view(),
        name="peer-inbound-events",
    ),
    # 上家侧**对账**端点（spec §16.8）。⚠️ 带尾斜杠。 ⚠️ 与上面三条**同组**：只认 Bearer 令牌、⛔ 不认会话 cookie（spec §9-S2）
    # ⇒ 它在 `test_no_route_500` 的 explicit-routes 腿里也是「特例」（无令牌时正确答复是 401，不是那条腿通用的 200），已在该腿的 `TOKEN_ONLY_ROUTES` 里显式登记。
    # ⚠️ 服务对象是**被问的一方**：下家答 `PeerOrderLink.last_applied_at`，上家回 `null` （本期只覆盖「上家问下家」一个方向，见视图 docstring 的残差段）。
    path(
        "peer-sync/reconcile/",
        PeerReconcileView.as_view(),
        name="peer-reconcile",
    ),
    # 上家侧**大厅全量拉取**端点（计划 `2026-10-25-peer-hall-pull` §1）。⚠️ 带尾斜杠。
    # ⚠️ 与上面三条同组：只认 Bearer 令牌、⛔ 不认会话 cookie（spec §9-S2） ⇒ 登记在 `test_no_route_500.TOKEN_ONLY_ROUTES` 里。
    # ⚠️ 机器对机器、无页面操作人 ⇒ 登记在 `DELIBERATELY_NOT_LOGGED`（理由同 reconcile）。
    path(
        "peer-sync/hall/list/",
        PeerHallListView.as_view(),
        name="peer-hall-list",
    ),
    # 下家侧**页面**端点（spec §8.4）。⚠️ 两条路由都带尾斜杠（spec §8.4 的三个端点：GET / POST 同住集合路径，DELETE 在明细路径）。
    # ⚠️ 与本组上面两条**刻意不同**：这两条走全局默认的**会话认证**（人在页面上操作），因此**不需要** `test_no_route_500` 的令牌豁免（那条豁免只给只认 Bearer 的路由）。
    path(
        "peer-bindings/",
        PeerBindingListView.as_view(),
        name="peer-binding-list",
    ),
    path(
        "peer-bindings/<int:pk>/",
        PeerBindingDetailView.as_view(),
        name="peer-binding-detail",
    ),
    # ── 绑定上家系统设置页的**统一列表**与鸡蛋铺删除（计划 §3.1）────
    # ⚠️ 两条都是**显式 `path()`**、⛔ **不挂 router**（挂上去会多出 `POST` / `PATCH` 之类的假形态，计划 §3.1 第 2 条）。
    # ⚠️ 两条都走全局默认的**会话认证**（人在页面上操作），也都在类体上声明 `permission_classes = [has_node(UPSTREAM_BINDING_NODE)]`（fail-closed）。
    path(
        "upstream-bindings/",
        UpstreamBindingListView.as_view(),
        name="upstream-binding-list",
    ),
    # ⭐ 行级创建（E-0255 / D-0256）：绑定改**多行**资源后，页面的「新增」走这里（旧写法 = 弹窗打按用户的 member PUT，会把本人已有行静默覆盖掉）。
    # 节点门与列表 / 删除同一个（`UPSTREAM_BINDING_NODE`）。 ⚠️ 显式 `path()`、集合路径**不带路径转换器** ⇒ 它会自动进
    # `tests/test_no_route_500.py` 的 explicit-routes 腿（畸形入参必须是 200 + 信封）。
    path(
        "upstream-bindings/jidanpu/",
        JidanpuUpstreamBindingCreateView.as_view(),
        name="upstream-binding-jidanpu-create",
    ),
    # ⭐ 行级读 / 编辑（GET / PUT，D-0256）与既有真删（DELETE，计划 §2.2）
    #    同住明细路径：同一个视图类（`JidanpuUpstreamBindingDetailView`）三支方法。
    path(
        "upstream-bindings/jidanpu/<int:pk>/",
        JidanpuUpstreamBindingDetailView.as_view(),
        name="upstream-binding-jidanpu-detail",
    ),
    # ── 上家订单改价记录（订单日志/改价记录线 T2，spec §3-D3）──────────────────
    # ⚠️ 与大厅/抢单/截图**同一手法**：显式 `path()` + 独立 `ListAPIView`， ⛔ 不给 `OrderViewSet` 加 `@action`（那会多生成一个 `orders/…` 入口，且本线同时有人改那个文件）。准入节点 `order.upstream_price_change_logs`
    # 写在视图类体上（RBAC 接线网 T2 的锚点）。 ⚠️ 不带路径转换器 ⇒ 它会**自动进入** `tests/test_no_route_500.py` 的 explicit-routes 腿（畸形查询参数必须是 200 + 信封，⛔ 不能 500）。
    path(
        "upstream-price-change-logs/",
        UpstreamPriceChangeLogListView.as_view(),
        name="upstream-price-change-log-list",
    ),
    # ── 订单截图（图片存储线，spec §7）────────────────────────────────
    # ⚠️ 与退款/复制/大厅**同一手法**：显式 `path()`，不给 ViewSet 加 `@action` （`@action` 会额外生成 `orders/<pk>/screenshots/` 之类的第二套入口）。
    # ⚠️ 路径**不带尾斜杠**（与 `orders/<order_no>/refunds` 同形）。 ⚠️ 带路径转换器 ⇒ `test_no_route_500` 的 explicit-routes 腿会跳过它们，所以畸形入参由 `tests/test_order_screenshots_api.py` 自己钉一次。
    path("screenshot-browser/start", ScreenshotBrowserViewSet.as_view({"post": "start"}),
         name="screenshot-browser-start"),
    path("screenshot-browser/exchange", ScreenshotBrowserViewSet.as_view({"post": "exchange"}),
         name="screenshot-browser-exchange"),
    path("screenshot-browser/status", ScreenshotBrowserViewSet.as_view({"get": "status"}),
         name="screenshot-browser-status"),
    path("screenshot-browser/ticket", ScreenshotBrowserViewSet.as_view({"post": "ticket"}),
         name="screenshot-browser-ticket"),
    path("screenshot-browser/claim", ScreenshotBrowserViewSet.as_view({"post": "claim"}),
         name="screenshot-browser-claim"),
    path(
        "screenshot-browser/blob/<int:screenshot_id>",
        ScreenshotBrowserBlobView.as_view(),
        name="screenshot-browser-blob",
    ),
    path(
        "orders/<order_no:order_no>/screenshots/ticket",
        ScreenshotTicketView.as_view({"post": "issue_ticket"}),
        name="order-screenshot-ticket",
    ),
    path(
        "orders/<order_no:order_no>/screenshots",
        ScreenshotCollectionView.as_view({"get": "list_items", "post": "claim"}),
        name="order-screenshots",
    ),
    path(
        "orders/<order_no:order_no>/screenshots/zip",
        ScreenshotZipView.as_view({"get": "download"}),
        name="order-screenshots-zip",
    ),
    path(
        "orders/screenshots/<int:pk>",
        ScreenshotDeleteView.as_view({"delete": "destroy"}),
        name="order-screenshot-delete",
    ),
    # ── 问题反馈（spec `2026-10-08-feedback-design.md` §4）────────────
    # ⚠️ 三条都**带尾斜杠**（spec §4 的路径逐字如此；与 `home/overview/` 同形，与 `orders/<no>/screenshots` 那条不带尾斜杠的历史形状不同）。 ⚠️ 与大厅/抢单/截图**同一手法**：显式 `path()` + `as_view()`，⛔ 不挂 router （router 会额外生成 `/feedback/{pk}/` 那套「有列表、有主键」的假形态）。
    # ⚠️ 两个没有路径转换器的（`feedback/` 与 `feedback/media/ticket/`）会**自动进入** `tests/test_no_route_500.py` 的 explicit-routes 腿（未认证时必须是 401 + 信封，不是 500）；带 `<int:pk>` 的那条被那条腿跳过，畸形入参由 `tests/test_feedback_api.py` 自己钉。
    path(
        "feedback/media/ticket/",
        FeedbackTicketView.as_view(),
        name="feedback-media-ticket",
    ),
    path(
        "feedback/",
        FeedbackCollectionView.as_view(),
        name="feedback",
    ),
    path(
        "feedback/<int:pk>/",
        FeedbackHandleView.as_view(),
        name="feedback-detail",
    ),
]
