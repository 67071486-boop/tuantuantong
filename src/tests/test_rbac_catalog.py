"""权限目录全表的结构校验（spec §4.1 / §4.3）。**这个文件是权限目录登记的验收器**：150 行数据是人工誊写的，
所以断言不写"某一条对不对"（那要抄 150 遍），而是写**可机械核对的形状**：条数、每模块条数、三类形态的分布、code 唯一性与命名规则。"""
import re
from django.test import SimpleTestCase

from apps.rbac.catalog import EXPECTED_KIND_COUNTS, EXPECTED_NODE_COUNT, MODULES, NODES
from apps.rbac.choices import NodeKind

#: 与 `docs/JIDANPU-PERMISSION-CATALOG.md` §1「栏目速查」的二级节点数逐条一致。
#: ⚠️ 本仓有**自有**节点（`benchmark_id = None`，对标物里没有的点），逐条登记在
#: `catalog.py` 的模块 docstring；各栏目读数以本表与下面断言为准。
EXPECTED_PER_MODULE = {
    "my_orders": 5, "my_performance": 1, "my_profile": 1, "grab_hall": 1,
# ⭐ 自有节点落点：`finance.xgj_receipt_detail`、`system.article_manage` 落各自栏目，
# `order.platform_write` 落 `order` 栏目下（自有节点全清单见 `test_benchmark_ids_are_unique`）。
    # ⭐ 企微自建应用线（D-0379）：system 29 → **30**（第十一条自有节点落 system 栏目）。
    "order": 57, "system_user": 17, "game": 7, "finance": 17, "system": 30,
    # ⚠️ customer 11 → 4：2026-11-11 业务方拍板客户维护组收敛到 3 个页面
    # （member_list / member_analysis_report / blacklist），其余 8 点删目录 + 删库（迁移 0015）；
    # 同批新增自有限制节点 `member_list_self_only`（迁移 0016）。
    "my_group": 0, "home": 15, "my_account": 2, "customer": 4,
}

CODE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?$")


class CatalogShapeTests(SimpleTestCase):
    def test_thirteen_modules(self):
        self.assertEqual(len(MODULES), 13)
        self.assertEqual({m[0] for m in MODULES}, set(EXPECTED_PER_MODULE))

    def test_150_nodes_and_total_163(self):
        # ⚠️ 方法名里的 150 / 163 是写下时的读数（方法名不改，一次改名会牵动全仓引用）；
        # 真值由下面两条字面断言守着（自有节点逐条入表、客户维护组 2026-11-11 收敛删 8 点，
        # 读数随之变化）。
        self.assertEqual(len(NODES), EXPECTED_NODE_COUNT)
        # ⭐ 企微自建应用线（D-0379）：152 → **153**（第十一条自有节点 `system.wecom_settings`，
        # 非 flag 的 PAGE 点）；13 栏目不变 ⇒ 166。
        self.assertEqual(len(NODES), 157)
        self.assertEqual(len(NODES) + len(MODULES), 170)

    def test_per_module_counts_match_the_reference(self):
        actual = {m[0]: 0 for m in MODULES}          # ⚠️ 先给 13 个栏目各一个 0
        for code, *_ in NODES:
            actual[code.split(".")[0]] += 1          # `my_group` 是 0 ⇒ 必须靠初始化兜住
        self.assertEqual(actual, EXPECTED_PER_MODULE)

    def test_kind_distribution(self):
        """三类形态的条数来自对标 URL 形态的**实测**计数（spec §4.1）。"""
        counts = {}
        for _code, _bid, _name, _note, _url, kind, _avail in NODES:
            counts[kind] = counts.get(kind, 0) + 1
        self.assertEqual(counts, EXPECTED_KIND_COUNTS)
        # 2026-11-11 客户维护组收敛删 8 点（6 page + 2 api）+ 新增自有限制 flag + 同日
        # 企微线第十一条自有节点（PAGE）后的读数。
        self.assertEqual(counts, {NodeKind.PAGE: 83, NodeKind.API: 52, NodeKind.FLAG: 22})

    def test_codes_are_unique_and_well_formed(self):
        codes = [code for code, *_ in NODES] + [m[0] for m in MODULES]
        self.assertEqual(len(codes), len(set(codes)), "code 有重复")
        for code in codes:
            self.assertRegex(code, CODE_RE)

    def test_benchmark_ids_are_unique(self):
        """对标 id 唯一；⭐ **自有节点**（`benchmark_id is None`）**至多一条并点名**。
        ⚠️ **为什么这里不能直接 `assertEqual(len(ids), len(set(ids)))`**：自有节点（`benchmark_id=None`）进来之后 `None` 就在这个列表里；若将来再加一条自有节点，上面那条等号会以「**benchmark_id 有重复**」的**假象**变红——而真正该红的地方是「自有节点清单」那条判据（`test_rbac_structure.py` 的 B5 侧）。
        ⇒ 这一条改成：**先摘掉 `None`**，再钉「对标 id 互不相同」，并**单独**把自有节点钉成「**恰好一条**、且就是它」。"""
        ids = [bid for _c, bid, *_ in NODES if bid is not None] + [m[1] for m in MODULES]
        self.assertEqual(len(ids), len(set(ids)), "benchmark_id 有重复")
        own = [n[0] for n in NODES if n[1] is None]
        # 自有节点的**序位**被下面断言逐条钉住（清单与理由见 `allowed` 登记表）。
        self.assertEqual(
            own,
            [
                "order.platform_write",
                "finance.taker_withdraw_review",
                "finance.taker_wallet_adjust",
                "finance.xgj_receipt_detail",
                "system.allow_taker_open_api",
                "system.edit_settings",
                "system.qqbot_settings",
                "system.article_manage",
                "system.wecom_settings",
                "home.qr_manage",
                "home.approval_apply",
                "home.approval_penalty_approve",
                "home.approval_refund_handle",
                "home.approval_configure",
                "customer.member_list_self_only",
            ],
            "`benchmark_id is None` 的节点集合变了：自有节点是**逐条登记**的"
            "（`catalog.py` 的模块 docstring 有那张表），加/删一条就要同一次改这里",
        )
        self.assertNotIn(6, ids, "排团业务(6) 已剔除")
        self.assertNotIn(7, ids, "游戏号库(7) 已剔除")
        self.assertNotIn(1151, ids, "短信业务(1151) 已剔除")

    def test_api_kind_has_url_and_flag_kind_has_none(self):
        """形态与对标 URL 的对应关系是**实测**的（spec §1.1）：flag 类**没有** URL。"""
        for code, _bid, _name, _note, url, kind, _avail in NODES:
            if kind == NodeKind.FLAG:
                self.assertEqual(url, "", f"{code} 是 flag 类，不应有 URL")
            else:
                self.assertTrue(url.startswith("/"), f"{code} 的 URL 形态与 kind 不符")

    def test_available_true_is_only_the_nodes_consumed_by_wired_lines(self):
        """`available=True` 的点只可能是**已经有真实消费方**的那些（spec §4.4 的合取语义）。名单在下面这个集合里，多一个就红。`system.pinduoduo_settings` 的消费方是开放平台应用管理（`apps/openapi/api/app_views.py` 的 `has_node(*APP_MANAGE_NODES)`），店铺对接的写动作也收这个点；租户级只读总览已经删掉。
        这里写成**上界**（允许为空、不允许越界）才能同时容纳不同时刻的状态；把它钉成等号的分别是 `test_rbac_structure.py::test_b4_no_orphan_available_node`（集合）与 B2 的**字面量 66**（数量，B2 也是等号断言，不是下界）。"""
        allowed = {
            "home.approval_apply",
            "home.approval_configure",
            "system.role_authority", "system.role_authority_edit",
            "system.role_authority_delete", "system.role_authority_view",
            # ⭐ P4-12：P4 的三个消费方（各自的落点见 catalog.py 的 NODES 注释）
            "finance.change_settlement_status", "order.online_refund", "finance.payroll", "finance.alipay_authorization",
            "finance.online_settlement_details",
            # 订单链 / 接单大厅 / 我的订单 / 我的绩效 / 四条 restrict flag
            "order.add_edit", "order.list", "order.view_detail", "order.view_content",
            "order.change_status", "order.change_status_completed", "order.assign_taker",
            "order.clear_taker", "order.set_offer_price", "order.send_to_grab_hall",
            "order.withdraw_from_grab_hall", "order.submit_edit",
            "order.submit_payout_price", "order.submit_work",
            "order.edit_payout_price", "grab_hall.take_order",
            "my_orders.send_to_my_system", "my_performance.export",
            "order.restrict_to_self_created", "order.restrict_view_taker_price",
            "order.restrict_view_offer_price", "order.restrict_edit_taker_price",
            # 账号管理四个写点 + 两条 restrict flag
            "system_user.list", "system_user.add_edit", "system_user.change_status",
            "system_user.reset_password", "system_user.restrict_view_id_card",
            "system_user.restrict_view_commission_params",
            # ⭐ P4FU-B 的 2 个（绩效页「删除记录」+「导出记录」的真实端点）
            "finance.delete_performance", "finance.export_performance",
            # ⭐ 订单列表第一批的 2 个 `flag`：列表载荷的 `taker_name`
            # 脱敏与统计块 `summary`（消费方是 `apps/business/order_flags.py` 的读取点，
            # 见 `apps/rbac/wiring.py` 的 `SITES`）。
            "order.restrict_view_taker", "order.restrict_view_total_stats",
            # ⭐ 订单详情页复刻线的 1 个（`notes` 动作挂的对标节点 27）
            "order.add_log",
            # ⭐ 接单统计端点
            "system_user.view_order_stats",
            # ⭐ 改收款状态（22）。消费方是
            # `OrderViewSet.ORDER_PERMISSIONS["payment_status"]`（视图上的门）。
            "order.change_payment_status",
            # 游戏配置 / 订单来源写端点的逐动作节点门
            "game.add_edit", "game.delete",
            "system.add_edit_source", "system.delete_source",
            # 区服 / 订单项目分类 / 常用代练项目。翻真前只有体验层位点，零权限角色也能真删改。
            "game.server", "game.order_type_categories", "game.common_projects",
            # 操作日志。判据在两个读端点上；漏登记 INTERCEPT 会让 available 和位点对不上。
            "system.logs",
            # ⭐ 录单识别设置线的 2 个：页面点 89 与编辑动作点 96，
            # 消费方是 `apps/business/api/recognition_views.py` 的两个视图（类体上的
            # `permission_classes`）+ 识别设置页的路由/菜单声明。
            "system.auto_discern_settings", "system.edit_auto_discern_keywords",
            # ⭐ D-0144 的 2 个：删单 `order.delete`（23）与回收站
            # `order.recycle`（1219）。消费方是 `OrderViewSet.ORDER_PERMISSIONS` 的
            # `delete` 与 `recycle_bin` / `restore` 三个条目（位点见 `apps/rbac/wiring.py`）。
            "order.delete", "order.recycle",
            # ⭐ 我的资料线的 1 个：`my_profile.edit`（121）。消费方是
            # `apps/core/profile_views.py::MyProfileView.get_permissions`（PATCH 动作，
            # 位点见 `apps/rbac/wiring.py` 的 `SITES`）。
            "my_profile.edit",
            # ⭐ 同线的第 2 个：`home.change_password`（101）。消费方是
            # `apps/core/auth_views.py::ChangePasswordView.get_permissions`（POST 改密）。
            "home.change_password",
            # ⭐ 上下游订单互通（裁决 R22）的 1 个：`system.bind_upstream_system`
            # （1229）。消费方是两个页面端点的类体 `permission_classes`（位点见
            # `apps/rbac/wiring.py` 的 `SITES`）；本点同时仍是菜单可见性的承载者。
            "system.bind_upstream_system",
            # ⭐ 图片存储线的 4 个：订单截图的签发/认领、查看、删除、打包下载。
            # 消费方 = `apps/business/api/screenshot_views.py` 的三个视图 +
            # `screenshot_zip.py::ScreenshotZipView`（位点见 `apps/rbac/wiring.py` 的 `SITES`）。
            "order.upload_screenshot", "order.view_screenshot",
            "order.screenshot_manage", "order.download_screenshots_zip",
            # ⭐ 订单导出线的 1 个：`order.export`(31)。消费方是
            # `apps/business/api/order_export_views.py::OrderExportView.get_permissions`
            # （`GET /api/business/orders/export` 的权限闸，位点见 `apps/rbac/wiring.py`）。
            "order.export",
            # `system.pinduoduo_settings`：开放平台应用管理，以及店铺对接的逐动作门。
            # 租户级只读总览已经删掉。不要再写回这一格。
            "system.pinduoduo_settings",
            # ⭐ 自动发货线（spec `2026-10-03-xgj-auto-ship-design.md` §3.7）的 1 个：`order.taobao_ship_records`(1227)。消费方 = 新增的 `apps/business/api/tb_send_log_views.py::TaobaoOrderSendLogViewSet.get_permissions`
            # ——**两个动作共用本点**（`list` 只读列表 + `retry` 重试发货）。位点见 `apps/rbac/wiring.py` 的 `SITES` 里那条 `INTERCEPT`（同一次从 `PENDING` 摘掉，互斥判据：有 `INTERCEPT` 位点的 code 不许留在 `PENDING`）。
            # ⚠️ 本点**同时仍是菜单** `orders-taobao-ship` 可见性的承载者（`EXPERIENCE` 位点）。
            "order.taobao_ship_records",
            # ⭐ 交易订单操作记录线（spec `2026-10-04-trade-action-log-design.md` §7-15）的 1 个：`order.platform_action_logs`(1267)。消费方 = 本页唯一的读端点 `apps/audit/api/views.py::OrderActionLogListView`（**类体**上的 `permission_classes = [has_node("order.platform_action_logs")]`）；
            # 同批从 `wiring.PENDING` 摘掉（互斥判据：有 `INTERCEPT` 位点的不许留在 `PENDING`）、`catalog.available` 翻真（位点见 `apps/rbac/wiring.py` 的 `SITES`）。
            # ⚠️ 本点**同时仍是菜单** `orders-platform-action-log` 可见性的承载者（`EXPERIENCE` 位点）。
            "order.platform_action_logs",
            # ⭐ 接单销售客服可改线（计划 `docs/plans/2026-10-08-sales-agent-change.md` §5 T2）的 1 个：`order.allow_change_sales_agent`(1272)。消费方 = `OrderViewSet.partial_update` 的**动作体**里那道条件闸（带了**变化的** `sales_agent` 才要本节点）；
            # 同批从 `wiring.PENDING` 摘掉（互斥判据：有 `INTERCEPT` 位点的不许留在 `PENDING`）、`catalog.available` 翻真（位点见 `apps/rbac/wiring.py` 的 `SITES`，锚点不是 `get_permissions`）。
            "order.allow_change_sales_agent",
            # ⭐ 问题反馈线（spec `2026-10-08-feedback-design.md` §5）的 1 个：`home.feedback`(112)「问题反馈及建议」（对标默认勾选 = Y）。消费方 = `apps/business/api/feedback_views.py::FeedbackNodeGatedView.get_permissions`——**三个动作共用本基类的节点门**（列表 GET / 提交 POST / 签发票据 POST），节点 code 在模块常量 `FEEDBACK_NODE` 里（位点见 `apps/rbac/wiring.py` 的 `SITES`，同一次从 `PENDING` 摘掉：互斥判据要求有 `INTERCEPT` 位点的不许留在 `PENDING`）。
            # ⚠️ 第四个端点（`PATCH feedback/<pk>/`）的门是 `is_tenant_admin`、**不是本节点** ⇒ 它不构成本点的消费方，也不登记进 `SITES`。
            # ⚠️ 本点**同时仍是侧栏底部条目** `feedback`（`config/menu.ts`）可见性的承载者（`EXPERIENCE` 位点）。该集合是**上界**（`assertLessEqual`），真值由 B4 的等号断言钉住。
            "home.feedback",
            # ⭐ 打手侧开放接口：本仓自有节点 `system.allow_taker_open_api`（`benchmark_id = None`）。消费方 = 内部端点 `/api/taker/apps/…`（`apps/takerapi/api/app_views.py::TakerAppAdminView` 的**类体**装配 `permission_classes = [has_node(*APP_MANAGE_NODES)]`，而 `APP_MANAGE_NODES` 里有本 code 的**字面量**；位点见 `apps/rbac/wiring.py` 的 `SITES`）。
            # ⚠️ 它**同时**是菜单 `taker-apps` 与路由 `taker-apps` 的可见性承载者。
            # ⚠️ 与上面那些对标的点**性质不同**：它没有对标来源（对标物里没有这一项），靠数据迁移 `0006_add_taker_open_api_node` 落到存量租户。
            "system.allow_taker_open_api",
            # ⭐ 实名认证线：两条原本 `available=False` 的对标点翻真。消费方 = `AccountViewSet.ACCOUNT_PERMISSIONS` 的 `realname_logs` / `realname_setting` / `realname_quota`（共用 1263）与 `reset_realname`（1264），经该类的 `get_permissions()` 装配（位点见 `apps/rbac/wiring.py` 的 `SITES`；同一次从 `PENDING` 摘掉——互斥判据：有 `INTERCEPT` 位点的不许留在 `PENDING`）。
            # ⚠️ 两条都是**翻真**：`EXPECTED_NODE_COUNT` 与 `EXPECTED_KIND_COUNTS` 一个字都不动。
            "system_user.realname_records", "system_user.reset_realname",
            # ⭐ 系统开关线（D-0223）：一条翻真（`system.settings`(91)，消费方 = `GET /api/business/system-switches/` 的类体节点门）+ 一条新生自有节点（`system.edit_settings`，消费方 = `PATCH …/<key>/`；存量租户靠数据迁移 `0007_add_system_edit_settings_node` 落行）。位点见 `apps/rbac/wiring.py` 的 `SITES`。
            "system.settings", "system.edit_settings",
            # ⭐ QQ 机器人线：**新生自有节点**（`benchmark_id=None`，消费方 = `apps/qqbot/api/views.py::QQBotSettingsGateView` 的基类类体节点门，全部 `api/qqbot/` 端点继承；存量租户靠数据迁移 `0008_add_qqbot_settings_node` 落行）。位点见 `apps/rbac/wiring.py` 的 `SITES`。
            "system.qqbot_settings",
            # ⭐ 上家订单改价记录线（spec `docs/specs/2026-10-17-order-log-names-and-upstream-price-log-design.md` §3-D5）：一条翻真（`order.upstream_price_change_logs`(1273)，消费方 = `apps/business/api/upstream_price_log_views.py::UpstreamPriceChangeLogListView` 的**类体** `permission_classes = [has_node(...)]`）。
            # 同批：`SITES` 补 `INTERCEPT` 位点、从 `PENDING` 摘掉、生成物 `docs/RBAC-WIRING-MAP.md` 重渲；`EXPECTED_NODE_COUNT` / `EXPECTED_KIND_COUNTS` 一个字都不动（只翻真、不新增点）。
            "order.upstream_price_change_logs",
            # ⭐ 系统内部公告 + 自动发消息到微信群：两条翻真，
            # 消费方分别是 `SystemNoticeViewSet.get_permissions`（写动作）与
            # `OrderPushSettingViewSet.get_permissions`（整个集合，含 `<pk>/test/`）。
            "system.publish_notice", "system.order_push_group_settings",
            # ⭐ D-0247：客服接发单统计 / 经营报表两页共用的点翻真。
            "finance.support_statistics",
            # ⭐ 首页二维码横幅线（D-0262）：自有节点 `home.qr_manage`（`benchmark_id = None`）。消费方 = `apps/business/api/qr_code_views.py` 的 `HomeQrCodeViewSet.get_permissions`（写动作逐 action）与 `HomeQrTicketView.get_permissions`（直传票据）；存量租户靠数据迁移 `0008_add_home_qr_manage_node` 落行。
            # ⚠️ 读列表**不挂**本节点（首页人人要渲染）——按持点分档返回。
            "home.qr_manage",
            # ⭐ 打手钱包 / 提现审核线：**新生自有节点**（`benchmark_id=None`，消费方 = `apps/business/api/withdraw_admin_views.py::WithdrawAdminViewSet` 的类体节点门；列表 + 5 个写动作共用）。
            # 存量租户靠数据迁移 `0010_add_taker_withdraw_review_node` 落行。
            "finance.taker_withdraw_review",
            # ⭐ 客服后台打手钱包管理线：**新生自有节点**（`benchmark_id=None`，消费方 = `apps/business/api/wallet_admin_views.py` 的 `TakerWalletAdminViewSet` / `DepositRuleListView` / `DepositRuleUpdateView` 的**类体**节点门，四个端点共用）。
            # 存量租户靠数据迁移 `0011_add_taker_wallet_adjust_node` 落行。
            "finance.taker_wallet_adjust",
            # ⭐ 闲管家收款明细线：**新生自有节点**（`benchmark_id=None`，消费方 = `apps/business/api/xgj_receipt_views.py` 的 `XgjReceiptSummaryView` / `XgjReceiptListView` 两个读端点的类体 `get_permissions`）。
            # 存量租户靠数据迁移 `0012_add_xgj_receipt_detail_node` 落行。
            "finance.xgj_receipt_detail",
            # ⭐ 频道文章作品线：**新生自有节点**（`benchmark_id=None`，消费方 = `apps/business/api/article_views.py` 的 `ArticleViewSet` / `ChannelProfileAdminView` / `ArticleMediaTicketView` / `ArticleMediaClaimView` 的 `get_permissions`，**全部端点含读**共用）。
            # 存量租户靠数据迁移 `0013_add_system_article_manage_node` 落行。
            # ⚠️ 公开读端点（`/api/channel/…`，匿名）**不挂**本点。
            "system.article_manage",
            # ⭐ 企微自建应用线：**新生自有节点**（`benchmark_id=None`，消费方 =
            # `apps/wecom/views.py::WecomSettingsGateView` 的类体 `permission_classes`，
            # `api/wecom/` 前缀下全部登录端点共用）。存量租户靠迁移 `0017` 落行。
            "system.wecom_settings",
            # ⭐ 交易平台对接设置线：对标点 1189 翻真。消费方 = `apps/business/api/platform_integration_views.py` 的 `PlatformIntegrationViewSet`（账号 CRUD）与 `PlatformIntegrationSettingsView`（全局设置 GET / PUT）两个视图的类体 `permission_classes`（节点 code 走模块常量 `PLATFORM_INTEGRATION_NODE`，位点见 `apps/rbac/wiring.py` 的 `SITES`）。
            "system.platform_integration_settings",
            # ⭐ 交易平台自动回复线：对标点 1238 翻真。消费方 = `apps/business/api/platform_auto_reply_views.py` 的 `PlatformAutoReplyRuleViewSet`（规则 CRUD，**含读**）的类体 `permission_classes`
            # （节点 code 走模块常量 `PLATFORM_AUTO_REPLY_NODE`，位点见 `apps/rbac/wiring.py` 的 `SITES`）。
            "system.platform_auto_reply",
            # ⭐ 交易平台涉钱写动作线：自有节点
            # `order.platform_write`（消费方 = 三个 dailiantong 视图模块的写端点，
            # 读端点仍挂 `order.list`；位点见 `apps/rbac/wiring.py` 的 `SITES`）。
            "order.platform_write",
            # ⭐ 客户线第五批（2026-11-11）：自有限制节点 `customer.member_list_self_only`。
            # 消费方 = `apps/business/api/customer_views.py` 四端点的守卫与行集收口
            # （位点见 `apps/rbac/wiring.py` 的 `SITES`）；存量租户靠数据迁移 `0016` 落行。
            "customer.member_list_self_only",
            # ⭐ 客户线翻真（2026-11-11 业务方拍板）：两个页面点翻真 + 改名（客户管理 /
            # 客户分析报表）。消费方 = `apps/business/api/customer_views.py` 的四端点守卫
            # （member_list ∨ analysis 自家门 ∨ 限制点任一；位点见 `apps/rbac/wiring.py`
            # 的 `SITES`）；存量租户靠数据迁移 `0017` 传播。
            "customer.member_list", "customer.member_analysis_report",
        }
        self.assertLessEqual({n[0] for n in NODES if n[6]}, allowed)
