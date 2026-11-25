"""结构兜底（spec §10-B）。**这些断言不是测某个功能，是测"没人记得时会不会红"。**

B1 常量不漂移 / B2 `available=True` 有消费方 / B3 `Role`↔`Group` 不漂移 /
B4 无孤儿 `available` 点 / B5 目录与清单文档逐行一致 / B6 树接口透出 `available` 的**值** /
B7 树接口门禁真的接上 / B8 children 顺序 / B9 内置角色的 `flag` 集合 == 显式常量 /
B10 没有任何内置角色持有这两条账号限制 `flag` /
B11 `order.add_log` 由 `order` 栏目批发给三个默认角色（订单详情页复刻线 I-1 的授权不变判据）/
B12 代练员默认权限含「提交完单和提交问题单」 / B13 重指的证据（`benchmark_id` 105 仍是锚点、`code` 已换）。

⚠️ **本清单是人工枚举，不带条数**。**条数以本文件的 `def test_` 方法数为准**，数的时候用行首锚定的
`grep -cE "^    def test_"`——**别用不带 `^` 的写法**：本段注释自己那行也含这个模式，
会**自指多算 1**（E-0024 同族，实测 11 vs 真实 10）。加了新兜底就往上面这行补一个编号，
**不要**在句首写"共 N 条"。
"""
import pathlib

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django_tenants.test.cases import TenantTestCase

from apps.business.roles import DEFAULT_ROLES
from apps.rbac.catalog import NODES
from apps.rbac.choices import NodeKind
from apps.rbac.models import PermissionNode, Role
from apps.rbac.seed import BUILTIN_ROLE_FLAGS, seed_builtin_roles, seed_catalog

APPS_DIR = pathlib.Path(__file__).resolve().parent.parent / "apps"
#: ①线交付的 4 个 **+** ③线第一批翻真的 22 个（③线主计划
#: `docs/plans/2026-09-21-rbac-endpoint-enforcement.md` §1.2(a) 的判定表 + 其执行记录的
#: 22 条逐条清单）——**常量名沿用历史命名**（`CONSUMED_IN_LINE_1`，它早已不只是①线那 4 个）。
#: ⚠️ 下面这些集合都是**硬编码字面量、不从 `catalog.py` 派生**——派生会让"改坏 catalog"不红
#: （E-0024 的自指断言陷阱）。这些集合 = 各条线各自翻真的点，来源分列便于追溯；
#: **不在此写死集合个数或线数**（加一条线就会静默过期）。
CONSUMED_IN_LINE_1 = {
    "system.role_authority",
    "system.role_authority_edit",
    "system.role_authority_delete",
    "system.role_authority_view",
    "order.add_edit",
    "order.list",
    "order.view_detail",
    "order.view_content",
    "order.change_status",
    "order.change_status_completed",
    "order.assign_taker",
    "order.clear_taker",
    "order.set_offer_price",
    "order.send_to_grab_hall",
    "order.withdraw_from_grab_hall",
    "order.submit_edit",
    "order.submit_payout_price",
    "order.submit_work",
    "order.edit_payout_price",
    "grab_hall.take_order",
    "my_orders.send_to_my_system",
    "my_performance.export",
    "order.restrict_to_self_created",
    "order.restrict_view_taker_price",
    "order.restrict_view_offer_price",
    "order.restrict_edit_taker_price",
}

#: ⭐ **P4（结算与财务）消费掉的三个**。每一个都有**真实调用点**：
#: `finance.change_settlement_status`（P4-7 `settle_views.SettleView` + P4-11
#: `adjustment_views.MANUAL_ADJUSTMENT_NODE`——**两个**消费方）、
#: `order.online_refund`（P4-9 `refund_views.RefundView`）、
#: `finance.payroll`（P4-8 `settlement_views.FINANCE_READ_NODE` + P4-11 的读方法）。
#: ⚠️ B2 的 AST 引用检查会逐个在
#: `catalog.py` **之外**找到字符串字面量（`FINANCE_READ_NODE` 与两处 `has_node("…")`）。
CONSUMED_BY_P4 = {
    "finance.change_settlement_status",
    "order.online_refund",
    "finance.payroll",
    "finance.alipay_authorization",
    "finance.online_settlement_details",
}

#: ③线第一批翻真的 22 个点（`order` 链 + 接单大厅 + 我的订单/效绩）。
#: ⚠️ 其中 **4 条是 `flag`**（`order.restrict_*`）：它们的消费方是
#: `apps/business/order_flags.py` 的读取点常量，不是视图上的门；B2 自 ③线 起
#: **不再**把 `flag` 排除在外（见 B2 的 docstring）。
CONSUMED_BY_RBAC_3 = {
    "order.add_edit",
    "order.list",
    "order.view_detail",
    "order.view_content",
    "order.change_status",
    "order.change_status_completed",
    "order.assign_taker",
    "order.clear_taker",
    "order.set_offer_price",
    "order.send_to_grab_hall",
    "order.withdraw_from_grab_hall",
    "order.submit_edit",
    "order.submit_payout_price",
    "order.submit_work",
    "order.edit_payout_price",
    "grab_hall.take_order",
    "my_orders.send_to_my_system",
    "my_performance.export",
    "order.restrict_to_self_created",
    "order.restrict_view_taker_price",
    "order.restrict_view_offer_price",
    "order.restrict_edit_taker_price",
}

#: ③线**第二批**（`system_user`）翻真的 6 个点。
#: 其中 **2 条是 `flag`**（`system_user.restrict_*`）：消费方是
#: `apps/accounts/account_flags.py` 的读取点常量，不是视图上的门。
#: 判定表见 `docs/plans/2026-09-21-rbac-batch2-system-user.md` §1.1。
CONSUMED_BY_RBAC_3_BATCH_2 = {
    "system_user.list",
    "system_user.add_edit",
    "system_user.change_status",
    "system_user.reset_password",
    "system_user.restrict_view_id_card",
    "system_user.restrict_view_commission_params",
}

#: 已翻真的权限点**全集** = 上面全部 `CONSUMED_BY_*` / `CONSUMED_IN_LINE_1` 集合的并集
#: （B4 用这个并集做等号；B2 另写死一个字面量，两边互不引用）。
#: ⛔ 条数与集合个数都不写死（写死必静默过期），也不要按两条分支各自的读数相加。
#: 绩效页「删除记录 / 导出记录」两颗按钮各有真实端点。绩效页的「删除记录」与「导出记录」
#: 两颗按钮各有了真实端点（`api/performance_write_views.py` 的两个视图）。翻真前它们的
#: PENDING 理由是「财务/结算后端未开工」——端点落地后那句已过期。
#: ⚠️ 翻真会连带改 `apps/rbac/wiring.py`（SITES）与 `catalog.py`（available），以及
#: **生成物 `docs/RBAC-WIRING-MAP.md`**（`manage.py render_rbac_wiring_map` 重渲；
#: `test_rbac_wiring` 的 T4 用 `--check` 逐字比对）。
CONSUMED_BY_P4FU_B = {
    "finance.delete_performance",
    "finance.export_performance",
}

#: ⭐ 订单列表第一批（`/orders` 管理订单页）翻真的 **2 条 `flag`**：
#: `order.restrict_view_taker`（列表载荷 `taker_name` 脱敏为常量「无权查看」）与
#: `order.restrict_view_total_stats`（列表头部统计块 `summary` 回 `null`）。
#: 两者的消费方都是 `apps/business/order_flags.py` 的读取点函数（不是视图上的门），
#: 与 ③线第一批那四条 `order.restrict_*` **同一形状**；位点登记在
#: `apps/rbac/wiring.py` 的 `SITES`。
CONSUMED_BY_ORDER_LIST_PHASE1 = {
    "order.restrict_view_taker",
    "order.restrict_view_total_stats",
}

#: ⭐ **订单详情页复刻线翻真的 1 个**：`order.add_log`（27「添加订单日志」）。
#: 对标那个「添加」按钮打的 `POST /OrderAjax/SaveOrderPublieMark {orderId, remark}` 与我方
#: 的 `POST /orders/<单号>/notes/` **1:1 同功能**（I-1 裁定）⇒ 已移进 `wiring.SITES`、
#: `catalog.available` 翻真。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_ORDER_DETAIL_REPLICA = {
    "order.add_log",
}

#: ⭐ **接单统计端点翻真的 1 个**：对标 `/UserAjax/GetRecOrderTotalList`
#: 已实现为我方 `GET /api/business/order-stats`
#: （`apps/business/api/order_stats_views.py`），闸门 =
#: `has_node("system_user.view_order_stats")`。
CONSUMED_BY_P3E_TASK_3 = {
    "system_user.view_order_stats",
}

#: ⭐ **价格与收款状态翻真的 1 个**：`order.change_payment_status`（22）。
#: 对标那个弹窗打的 `POST /OrderAjax/UpSettlementOrderByIDX {orderId, sta}` 与我方的
#: `POST /orders/<单号>/payment-status/` 同功能 ⇒ 它的 PENDING 理由「功能未实现」当场过期。
#: 翻真会连带改 `catalog.py`（available）、`apps/rbac/wiring.py`（PENDING → SITES）、
#: 生成物 `docs/RBAC-WIRING-MAP.md`，以及本文件 B2 的字面量（恰好集合）。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则「改坏 catalog」不红。
CONSUMED_BY_ORDER_PRICE_PAYSTATUS = {
    "order.change_payment_status",
}

#: ⭐ **翻真的 4 个**：
#: 游戏配置与订单来源两个 ViewSet 的写端点——各自在 `get_permissions()` 里**逐动作**判
#: （`GAME_PERMISSIONS` / `ORDER_SOURCE_PERMISSIONS`）：
#: `game.add_edit`（create / update / partial_update）、`game.delete`（destroy）、
#: `system.add_edit_source`（create / update / partial_update）、`system.delete_source`（destroy）。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_RBAC_WIRING_REMAINING = {
    "game.add_edit",
    "game.delete",
    "system.add_edit_source",
    "system.delete_source",
}

#: ⭐ **翻真的 3 个**：
#: 区服 / 订单项目分类 / 常用代练项目三个 ViewSet 的写端点——各自在 `get_permissions()` 里
#: **逐动作**判（`GAME_SERVER_PERMISSIONS` / `PROJECT_CATEGORY_PERMISSIONS` /
#: `PROJECT_PERMISSIONS`，四个写动作都映射到本点）。
#: 翻真前它们**只有体验层位点**（菜单可见性）：零权限角色照样能真删/改这三类配置
#: （实测探针可穿过权限层）⇒ 补上拦截位点后 `available` 必须同向翻真（T3）。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_RBAC_WIRING_REMAINING_2 = {
    "game.server",
    "game.common_projects",
    "game.order_type_categories",
}

#: ⭐ **翻真的 1 个**：
#: `system.logs`——它的判据**早就在代码里**（`apps/audit/api/views.py` 的
#: `OperationLogListView` / `OperationLogActorListView` 两个读端点的
#: `permission_classes = [has_node("system.logs")]`），只是 `catalog.available` 还是 `False`
#: 且 `wiring.SITES` 漏了 `INTERCEPT` 位点 ⇒ 图上呈现为「有菜单可见性、服务端却敞着」的**假缺口**。
#: （只补登记 + 翻真；第三处：生成物 `docs/RBAC-WIRING-MAP.md`）。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_RBAC_WIRING_REMAINING_3 = {
    "system.logs",
}

#: ⭐ **录单识别设置翻真的 2 个**：页面点（89）与编辑动作点（96）。
#: 消费方：`apps/business/api/recognition_views.py` 的两个视图（列表 GET / 改行 PATCH，
#: `permission_classes` 写在**类体**上）+ 识别设置页的路由判据
#: （`src/frontend/src/config/routePermissions.ts::system-order-recognition`）
#: 与菜单声明（`config/menu.ts::system-order-recognition`）。
#: 翻真前三处同批：`catalog.available` + `wiring.SITES` 补 `INTERCEPT` 位点 + 删 `PENDING`。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_RECOGNITION_SETTINGS = {
    "system.auto_discern_settings",
    "system.edit_auto_discern_keywords",
}

#: ⭐ D-0144 翻真的两个（软删与回收站，spec §3-D7 / §8-T8 第 41 条）：
#: `order.delete`（23，删单端点 `POST api/business/orders/<order_no>/delete/`）与
#: `order.recycle`（1219，**一个点同时管「进回收站页」「看列表」「还原」三件事**——
#: 对标 §2.1.4 就只有一个点，消费方是 `OrderViewSet.ORDER_PERMISSIONS` 的
#: `recycle_bin` 与 `restore` 两个条目）。
#: 翻真同批三处：`catalog.available` + `wiring.SITES` 补 / 增 `INTERCEPT` 位点 + 删 `PENDING`。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_ORDER_DELETE_AND_RECYCLE = {
    "order.delete",
    "order.recycle",
}

#: ⭐ 我的资料线翻真的 1 个：`my_profile.edit`(121)。
#: 消费方 = `apps/core/profile_views.py::MyProfileView.get_permissions`（PATCH 动作）；
#: 翻真同批三处：`catalog.available` + `wiring.SITES` 补 `INTERCEPT` 位点 + 删 `PENDING`
#: （生成物 `docs/RBAC-WIRING-MAP.md` 重渲）。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
#: ⚠️ `home.change_password` 单独一行登记在下面
#: （`CONSUMED_BY_MY_PROFILE_PASSWORD`）——两个集合都进 `AVAILABLE_NODES`（B4 是等号断言）。
CONSUMED_BY_MY_PROFILE = {
    "my_profile.edit",
}

#: ⭐ 我的资料线翻真的第 2 个：`home.change_password`(101)。
#: 消费方 = `apps/core/auth_views.py::ChangePasswordView.get_permissions`（POST 改密）；
#: 翻真同批三处：`catalog.available` + `wiring.SITES` 补 / 增 `INTERCEPT` 位点 + 删 `PENDING`
#: （生成物 `docs/RBAC-WIRING-MAP.md` 重渲）。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_MY_PROFILE_PASSWORD = {
    "home.change_password",
}

#: ⭐ 上下游订单互通翻真的 1 个：
#: `system.bind_upstream_system`(1229)——它的两个页面端点（`PeerBindingListView` /
#: `PeerBindingDetailView`）现在在类体上声明
#: `permission_classes = [has_node("system.bind_upstream_system")]`（**裁决 R22**）。
#: 翻真同批三处：`catalog.available` + `wiring.SITES` 补 `INTERCEPT` 位点 + 删 `PENDING`
#: （生成物 `docs/RBAC-WIRING-MAP.md` 重渲）。⚠️ 本点**同时**仍是菜单 `system-source-system`
#: 的可见性承载者（D5 不新增节点、复用本点）。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_PEER_ORDER_SYNC = {
    "system.bind_upstream_system",
}

#: ⭐ 图片存储线翻真的 **4 个**：订单截图的签发/认领（共 `order.upload_screenshot`）、
#: 查看、删除、打包下载。消费方 = `apps/business/api/screenshot_views.py` 的三个视图 +
#: `screenshot_zip.py`（各在 `get_permissions()` 方法体里 `has_node(...)`，参数是模块常量）。
CONSUMED_BY_SCREENSHOTS = {
    "order.view_screenshot",
    "order.upload_screenshot",
    "order.screenshot_manage",
    "order.download_screenshots_zip",
}

#: ⭐ 订单导出线翻真的 1 个：`order.export`(31)。
#: 消费方 = `apps/business/api/order_export_views.py::OrderExportView.get_permissions`
#: （`GET /api/business/orders/export` 的权限闸，方法体里 `has_node("order.export")`）；
#: 翻真同批三处：`catalog.available` + `wiring.SITES` 补 `INTERCEPT` 位点 + 删 `PENDING`
#: （生成物 `docs/RBAC-WIRING-MAP.md` 重渲）。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_ORDER_EXPORT = {
    "order.export",
}

#: `system.pinduoduo_settings` 的消费方是开放平台应用管理
#: （`app_views` 的 `has_node(*APP_MANAGE_NODES)`，位点在 `wiring.SITES`）。
#: 本集合硬编码字面量（E-0024）：不从 `catalog.py` 派生，否则改坏 catalog 不红。
#: 租户级鸡蛋铺总览已拆除，不要再把它写成这个点的消费方。
#: 那 4 条只吃 `IsAuthenticated` 的既有端点与闲管家 `shop-bindings` 仍无本点判据，本条不声明它们收口。
CONSUMED_BY_OPENAPI_APP_MANAGE = {
    "system.pinduoduo_settings",
}

#: ⭐ 自动发货线（spec `2026-10-03-xgj-auto-ship-design.md` §3.7）翻真的 1 个：
#: `order.taobao_ship_records`(1227)。消费方 = `apps/business/api/tb_send_log_views.py` 的
#: `TaobaoOrderSendLogViewSet.get_permissions`（两个动作：只读列表 + 重试发货；
#: 节点 code 写在类体常量 `TB_SEND_LOG_PERMISSIONS` 里，T2 判据向下解析一层常量）。
#: 翻真同批三处：`catalog.available` + `wiring.SITES` 补 `INTERCEPT` 位点 + 删 `PENDING`
#: （生成物 `docs/RBAC-WIRING-MAP.md` 重渲）。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_XGJ_AUTO_SHIP = {
    "order.taobao_ship_records",
}

#: ⭐ 交易订单操作记录线（spec `2026-10-04-trade-action-log-design.md` §7-14）翻真的 1 个：
#: `order.platform_action_logs`(1267)。消费方 = `apps/audit/api/views.py::OrderActionLogListView`
#: （本页唯一的读端点，在**类体**上声明
#: `permission_classes = [has_node("order.platform_action_logs")]`）。
#: 翻真同批三处：`catalog.available` + `wiring.SITES` 补 `INTERCEPT` 位点 + 删 `PENDING`
#: （生成物 `docs/RBAC-WIRING-MAP.md` 重渲）。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_TRADE_ACTION_LOG = {
    "order.platform_action_logs",
}

#: ⭐ 接单销售客服可改线（计划 `docs/plans/2026-10-08-sales-agent-change.md` §5 T2）翻真的 1 个：
#: `order.allow_change_sales_agent`(1272)。消费方 =
#: `apps/business/api/order_views.py::OrderViewSet.partial_update` 的**动作体**里那道条件闸
#: （`has_node_now(request.user, "order.allow_change_sales_agent")`，带了**变化的** `sales_agent`
#: 才判；位点见 `apps/rbac/wiring.py` 的 `SITES`，锚点不是 `get_permissions`）。
#: 裁定口径 =「持本点的人才能改这一格」。
#: 翻真同批三处：`catalog.available` + `wiring.SITES` 补 `INTERCEPT` 位点 + 删 `PENDING`
#: （生成物 `docs/RBAC-WIRING-MAP.md` 重渲）。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_SALES_AGENT_CHANGE = {
    "order.allow_change_sales_agent",
}

#: ⭐ 问题反馈线（spec `2026-10-08-feedback-design.md` §5）翻真的 1 个：
#: `home.feedback`(112)「问题反馈及建议」。消费方 = `apps/business/api/feedback_views.py`
#: 的 `FeedbackNodeGatedView.get_permissions`（三个动作共用本基类的节点门：列表 GET /
#: 提交 POST / 签发票据 POST；节点 code 在模块常量 `FEEDBACK_NODE` 里，T2 向下解析一层）。
#: 翻真同批三处：`catalog.available` + `wiring.SITES` 补 `INTERCEPT` 位点 + 删 `PENDING`
#: （生成物 `docs/RBAC-WIRING-MAP.md` 重渲）。
#: ⚠️ 第四个端点（`PATCH feedback/<pk>/`）的门是 `is_tenant_admin`、**不是本节点**
#: ⇒ 它的存在**不**构成本点的消费方。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_FEEDBACK = {
    "home.feedback",
}

#: ⭐ **打手侧开放接口**：自有节点
#: `system.allow_taker_open_api`（`benchmark_id = None`，spec
#: `docs/specs/2026-10-08-taker-open-api-design.md` §7.1）。消费方 = 内部端点
#: `/api/taker/apps/…`（`apps/takerapi/api/app_views.py::TakerAppAdminView` 的**类体**装配
#: `permission_classes = [has_node(*APP_MANAGE_NODES)]`，`APP_MANAGE_NODES` 是**字面量元组**、
#: 里面就有本 code；位点见 `apps/rbac/wiring.py` 的 `SITES`）。
#: ⚠️ 与其它 `CONSUMED_BY_*` 不同，它**不是**从「`available=False` 翻真」来的：这条点
#: **随本线新建**（`catalog.py` 里第一次出现）⇒ 它的 `available=True` 与
#: 「数据迁移 `0006_add_taker_open_api_node` 把点落到存量租户」是**同一件事的两半**。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_TAKER_OPEN_API = {
    "system.allow_taker_open_api",
}

#: ⭐ **实名认证（T8）**：两条对标点翻真。
#: 消费方 = 管理页那四个端点（`AccountViewSet.ACCOUNT_PERMISSIONS` 的
#: `realname_logs` / `realname_setting` / `realname_quota` / `reset_realname`），
#: 经该类的 `get_permissions()` 装配（位点见 `apps/rbac/wiring.py` 的 `SITES`）。
#: ⚠️ 两条都是**翻真**（不是新建）⇒ `EXPECTED_NODE_COUNT`(151) 与
#: `EXPECTED_KIND_COUNTS` **一个字都不动**。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_REALNAME_ADMIN = {
    "system_user.realname_records",
    "system_user.reset_realname",
}

#: **已从 `catalog.py` 裁剪掉**的对标 benchmark_id（**55** 条，字面量）。
#: 它们对应的功能由业务方明文裁定不做（D-0036 / 排团整栏剔除 / D-0050 商城 / D-0014 P4 /
#: D-0074；2026-11-11 客户维护组收敛 8 点），证据与逐条清单见
#: `docs/specs/2026-09-23-rbac-roles-and-catalog-consolidation-design.md` §3.5。
#: ⚠️ **清单文档 `JIDANPU-PERMISSION-CATALOG.md` 一行都不删**（那是数据基线，`AGENTS.md` §4
#: 「docs 只增不改」）⇒ B5 的口径是「**目录 = 对标全表 − 本集合（业务方裁定不做清单）**」，
#: 而不是偷偷把对拍放宽。多裁/少裁/裁了又加回来都必须红。
#: ⚠️ 其中 4 条由**迁移 `0004`** 删（业务方裁定下线的 4 个系统设置页：
#: 录单修正设置 / 订单标签维护 / 微信公众号设置 / 淘宝店对接设置，见
#: `docs/specs/2026-09-23-order-recognition-settings-design.md` §10）。
#: id↔code 的对拍见 `tests/test_rbac_catalog_migration.py`：
#: 与原 43 条（迁移 0002）、0004 的 4 条、2026-11-11 的 8 条分开登记，三份映射的 id
#: **并集**与本集合比**等号**。
PRUNED_BENCHMARK_IDS = frozenset({
    28, 29, 30, 34, 35, 59, 60, 64, 81, 90, 106, 139, 144, 145, 146, 147,
    1160, 1167, 1168, 1170, 1171, 1174, 1178, 1179, 1180, 1181, 1182, 1183,
    1184, 1188, 1191, 1202, 1203, 1204, 1217, 1218, 1232, 1236, 1243,
    1260, 1261, 1268, 1280,
    # 4 个已下线系统设置页的权限点（全是 PAGE、原本 `available=False`）：
    # `system.wechat_settings`(123) / `system.taobao_ship_settings`(134) /
    # `system.order_labels`(1257) / `system.order_correction_settings`(1271)。
    # 先例：短信 4 页下线后由目录裁剪任务裁 `system.sms_settings`(90) / `order.send_sms`(30)。
    123, 134, 1257, 1271,
    # ⭐ 2026-11-11 业务方拍板「客户维护组收敛到 3 个页面」：余额明细 / 等级设置等
    # 8 个无功能 / 已删功能点从目录与库删除（迁移 `0015_prune_customer_module_nodes`）；
    # 保留 blacklist(138) / member_list(141) / member_analysis_report(1226)。
    # （D-0036 同族先例：对标有、业务方裁定不做。）
    143, 1211, 1224, 1225, 1251, 1252, 1254, 1282,
})

#: **不是被裁、而是被"改造"**的对标 benchmark_id。
#: 对标 105 「订单关联排团操作」在代码里保留 `benchmark_id`（业务方裁定），但
#: `code` / `name` / `note` 已改作**我方自有的「提交完单（和提交问题单）」**、`url` 按裁定值改写
#: ⇒ 它与清单文档那一行**必然不同**。B5 的「存活行逐字对拍」必须把它**显式**排除，
#: 而排除本身要被钉住（下面 ④b 钉死改造后的字面值 + 文档侧仍是原文），
#: 否则这个例外就成了"105 随便改都不红"的后门。
#: ⭐ 141 / 1226（2026-11-11 业务方拍板）：客户两点 `name` / `note` 改**客户口径**
#（「会员管理→客户管理」「会员分析报表→客户分析报表」；`benchmark_id` / `url` 不变，
# spec §4.3 的改名场景）⇒ 同属本集合，同受 ④b 钉死。
RETOOLED_BENCHMARK_IDS = frozenset({105, 141, 1226})

#: 105 改造后的**精确**目标值（spec §3.5.2 的「执行期修订」）——字面值，不从别处派生。
#: ⚠️ `name` / `note` 是「复用 105 并改名」裁定后的值
#: （「提交完单和提交问题单」；`note` 含「或提交问题单」），
#: 见 `apps/rbac/catalog.py` 的改名注释与 `order_status.py` 的 `ISSUE` 边注释。
#: ⛔ `code`（`order.submit_work`）与 `url` 仍然不变。
RETOOLED_105_FROM_CODE = (
    "提交完单和提交问题单",
    "把订单从「代练中」推进到「待验收」（代练员交单）或提交问题单",
    "/OrderAjax/ConfirmLinkGroup",
)
#: 105 在清单文档里的**原始对标文本**（文档一行不改 ⇒ 这仍然是它）。
RETOOLED_105_FROM_DOC = (
    "订单关联排团操作",
    "在订单关联排团时请求确认关联操作",
    "/PveTaskAjax/JoinOrderByGroupID",
)

#: 改造行的「代码侧现值 / 文档侧原文」**按 benchmark_id 的字面映射**（105 之外，
#: 2026-11-11 起含客户两点 141 / 1226）。字面值（E-0024），不从 `catalog.py` 派生。
RETOOLED_FROM_CODE = {
    105: RETOOLED_105_FROM_CODE,
    141: ("客户管理", "勾选此项后代表此职位的人可以进入客户管理列表", "/Customer/CustomerList"),
    1226: ("客户分析报表", "进入客户分析报表的页面", "/Customer/HyAnalysisReport"),
}
RETOOLED_FROM_DOC = {
    105: RETOOLED_105_FROM_DOC,
    141: ("会员管理", "勾选此项后代表此职位的人可以进入会员客户管理列表", "/Customer/CustomerList"),
    1226: ("会员分析报表", "进入会员分析的页面", "/Customer/HyAnalysisReport"),
}

#: ⭐ 系统开关（D-0223）：一条翻真 + 一条新生自有节点。
#: `system.settings`(91) 的消费方 = `GET /api/business/system-switches/`
#: （`SystemSwitchListView` 类体节点门）；`system.edit_settings`（自有节点，
#: `benchmark_id=None`）的消费方 = `PATCH /api/business/system-switches/<key>/`
#: （`SystemSwitchDetailView` 类体节点门）。存量租户拿到新点靠数据迁移
#: `0007_add_system_edit_settings_node`；位点见 `apps/rbac/wiring.py` 的 `SITES`。
CONSUMED_BY_SYSTEM_SWITCHES = {
    "system.settings",
    "system.edit_settings",
}

#: ⭐ 上家订单改价记录线（spec
#: `docs/specs/2026-10-17-order-log-names-and-upstream-price-log-design.md` §3-D5）翻真的 1 个：
#: `order.upstream_price_change_logs`(1273)。消费方 = `apps/business/api/upstream_price_log_views.py`
#: 的 `UpstreamPriceChangeLogListView`（本页唯一的读端点，在**类体**上声明
#: `permission_classes = [has_node("order.upstream_price_change_logs")]`）。
#: 翻真同批三处：`catalog.available` + `wiring.SITES` 补 `INTERCEPT` 位点 + 删 `PENDING`
#: （生成物 `docs/RBAC-WIRING-MAP.md` 重渲）。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_UPSTREAM_PRICE_LOG = {
    "order.upstream_price_change_logs",
}

#: ⭐ 客服接发单统计 / 经营报表线（D-0247）：`finance.support_statistics`（1192）
#: **翻真**——两个读端点
#: `GET /api/business/support-stats` 与 `GET /api/business/business-report`
#: （`apps/business/api/report_views.py` 的 `SupportStatsView` / `BusinessReportView`，
#: 类体 `get_permissions`）是它的真实消费方。翻真同批三处：`catalog.available` +
#: `wiring.SITES` 补两条 INTERCEPT + `PENDING` 删该 code。
CONSUMED_BY_SUPPORT_STATS = {
    "finance.support_statistics",
}

#: ⭐ 首页二维码横幅线（D-0262）：自有节点（`benchmark_id = None`，
#: 登记表见 `apps/rbac/catalog.py` 的模块 docstring）。消费方 =
#: `apps/business/api/qr_code_views.py` 的 `HomeQrCodeViewSet.get_permissions`
#: （写动作 POST / PUT / PATCH / DELETE 逐 action）与 `HomeQrTicketView.get_permissions`
#: （直传票据）；位点见 `apps/rbac/wiring.py` 的 `SITES["home.qr_manage"]`。
#: 存量租户拿到它靠数据迁移 `0008_add_home_qr_manage_node`（幂等、按 `code` 建、
#: ⛔ 不动角色授权）；新建租户的内置「人事」经 seed 的 `home` 栏目批发自动含它。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_HOME_QR = {
    "home.qr_manage",
}

#: ⭐ 系统内部公告 + 自动发消息到微信群翻真的 2 个：
#: - `system.publish_notice`(97)：写动作判据在 `SystemNoticeViewSet.get_permissions`；
#: - `system.order_push_group_settings`(1255)：整个 `OrderPushSettingViewSet`（含 `<pk>/test/`）。
#: 翻真同批三处：`catalog.available` + `wiring.SITES` 补 `INTERCEPT` 位点 + 删 `PENDING`
#: （生成物 `docs/RBAC-WIRING-MAP.md` 重渲）。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_NOTICE_AND_PUSH = {
    "system.publish_notice",
    "system.order_push_group_settings",
}

#: ⭐ QQ 机器人线：**新生自有节点** `system.qqbot_settings`。
#: 消费方 = `apps/qqbot/api/views.py::QQBotSettingsGateView` 的**基类类体**节点门
#: （`permission_classes = [has_node("system.qqbot_settings")]`，全部 `api/qqbot/`
#: 端点继承共用）。存量租户拿到它靠数据迁移 `0008_add_qqbot_settings_node`；
#: 位点见 `apps/rbac/wiring.py` 的 `SITES`。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_QQBOT = {
    "system.qqbot_settings",
}

#: ⭐ 打手钱包 / 提现审核线（D-0269）：自有节点
#: `finance.taker_withdraw_review`（`benchmark_id = None`，登记表见 `apps/rbac/catalog.py`
#: 的模块 docstring）。消费方 = `apps/business/api/withdraw_admin_views.py` 的
#: `WithdrawAdminViewSet.permission_classes`（**类体**装配：`IsAuthenticated` +
#: `has_node(...)`），列表与 5 个写动作共用；位点见 `apps/rbac/wiring.py` 的 `SITES`。
#: 存量租户拿到它靠数据迁移 `0010_add_taker_withdraw_review_node`（幂等、按 `code` 建、
#: ⛔ 不动角色授权）。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_WITHDRAW_REVIEW = {
    "finance.taker_withdraw_review",
}

#: ⭐ 客服后台打手钱包管理线：自有节点
#: `finance.taker_wallet_adjust`（`benchmark_id = None`，登记表见 `apps/rbac/catalog.py`
#: 的模块 docstring）。消费方 = `apps/business/api/wallet_admin_views.py` 的
#: `TakerWalletAdminViewSet` / `DepositRuleListView` / `DepositRuleUpdateView` 的**类体**节点门
#: （四个端点共用一个 code）；位点见 `apps/rbac/wiring.py` 的 `SITES`。
#: 存量租户拿到它靠数据迁移 `0011_add_taker_wallet_adjust_node`（幂等、按 `code` 建、
#: ⛔ 不动角色授权）。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_WALLET_ADJUST = {
    "finance.taker_wallet_adjust",
}

#: ⭐ 闲管家收款明细线：自有节点
#: `finance.xgj_receipt_detail`（`benchmark_id = None`，登记表见 `apps/rbac/catalog.py`
#: 的模块 docstring）。消费方 = `apps/business/api/xgj_receipt_views.py` 的
#: `XgjReceiptSummaryView` / `XgjReceiptListView` 的**类体** `get_permissions`
#: （两个 GET 读端点共用一个 code）；位点见 `apps/rbac/wiring.py` 的 `SITES`。
#: 存量租户拿到它靠数据迁移 `0012_add_xgj_receipt_detail_node`（幂等、按 `code` 建、
#: ⛔ 不动角色授权）。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_XGJ_RECEIPT = {
    "finance.xgj_receipt_detail",
}

#: ⭐ 频道文章作品线：自有节点 `system.article_manage`
#: （`benchmark_id = None`，登记表见 `apps/rbac/catalog.py` 的模块 docstring）。
#: 消费方 = `apps/business/api/article_views.py` 的 `ArticleViewSet` /
#: `ChannelProfileAdminView` / `ArticleMediaTicketView` / `ArticleMediaClaimView` 的
#: `get_permissions`（**全部端点含读**共用一个 code）；位点见 `apps/rbac/wiring.py` 的 `SITES`。
#: 存量租户拿到它靠数据迁移 `0013_add_system_article_manage_node`（幂等、按 `code` 建、
#: ⛔ 不动角色授权）。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_CHANNEL_ARTICLES = {
    "system.article_manage",
}

#: ⭐ 交易平台对接设置线（计划 `2026-10-26-platform-integration-settings.md` T2）
#: 翻真的 1 个：`system.platform_integration_settings`(1189)。
#: 消费方 = `apps/business/api/platform_integration_views.py` 的两个视图
#: （`PlatformIntegrationViewSet` 账号 CRUD + `PlatformIntegrationSettingsView` 全局设置）
#: 的**类体** `permission_classes`，参数是模块常量
#: `PLATFORM_INTEGRATION_NODE = "system.platform_integration_settings"`；
#: 位点见 `apps/rbac/wiring.py` 的 `SITES`。翻真同批三处：`catalog.available` +
#: `wiring.SITES` 补两个 `INTERCEPT` 位点 + 删 `PENDING`（生成物重渲）。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_PLATFORM_INTEGRATION = {
    "system.platform_integration_settings",
}

#: ⭐ 交易平台自动回复线（T13）翻真的 1 个：`system.platform_auto_reply`(1238)。
#: 消费方 = `apps/business/api/platform_auto_reply_views.py::PlatformAutoReplyRuleViewSet`
#: 的**类体** `permission_classes`，参数是模块常量
#: `PLATFORM_AUTO_REPLY_NODE = "system.platform_auto_reply"`；位点见 `apps/rbac/wiring.py`
#: 的 `SITES`。翻真同批：`catalog.available` + `wiring.SITES` 补 `INTERCEPT` 位点 + 删
#: `PENDING`（生成物 `docs/RBAC-WIRING-MAP.md` 重渲）+ 本清单（B2 的等号断言）。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_PLATFORM_AUTO_REPLY = {
    "system.platform_auto_reply",
}

#: ⭐ 交易平台涉钱写动作线：自有节点。
#: 消费方 = `dailiantong_push_views.py` / `dailiantong_action_views.py` /
#: `dailiantong_extra_views.py` 的**写**端点（读端点仍在 `order.list` 的消费方里；
#: 位点见 `apps/rbac/wiring.py` 的 `SITES`）。
CONSUMED_BY_PLATFORM_WRITE = {
    "order.platform_write",
}

#: ⭐ 客户线第五批（2026-11-11 业务方拍板）：自有限制节点 `customer.member_list_self_only`
#（`benchmark_id = None`，第十条自有节点）。消费方 =
# `apps/business/api/customer_views.py` 四端点的守卫与行集收口
#（`_restricted_creator`；位点见 `apps/rbac/wiring.py` 的 `SITES`）。
# 存量租户拿到它靠数据迁移 `0016_add_customer_member_list_self_only_node`（幂等、按 `code` 建、
# ⛔ 不动角色授权——限制权限默认谁都不给才是安全态）。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_CUSTOMER_SELF_ONLY = {
    "customer.member_list_self_only",
}

#: ⭐ 客户线翻真（2026-11-11 业务方拍板）：两个页面点翻真 + 改名
#（141「会员管理→客户管理」/ 1226「会员分析报表→客户分析报表」；对标 id 不变，
# spec §4.3 的改名场景）。消费方 = `apps/business/api/customer_views.py` 的四端点守卫
#（模块常量 `CUSTOMER_MEMBER_NODE` / `CUSTOMER_ANALYSIS_NODE`；位点见
# `apps/rbac/wiring.py` 的 `SITES`）。存量租户靠数据迁移
# `0017_rename_and_flip_customer_member_nodes` 传播。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_CUSTOMER_FLIP = {
    "customer.member_list",
    "customer.member_analysis_report",
}

#: ⭐ 企微自建应用线（D-0379）：自有页面节点 `system.wecom_settings`
#（`benchmark_id = None`，第十一条自有节点）。消费方 =
# `apps/wecom/views.py` 全部登录端点共用的基类装配（位点见 `apps/rbac/wiring.py` 的 `SITES`）。
# 存量租户拿到它靠数据迁移 `0017_add_wecom_settings_node`（幂等、按 `code` 建、不动角色授权）。
#: ⚠️ 本集合**硬编码字面量**（E-0024）：不从 `catalog.py` 派生，否则"改坏 catalog"不红。
CONSUMED_BY_WECOM = {
    "system.wecom_settings",
}

CONSUMED_BY_APPROVAL_CENTER = {
    "home.approval_apply", "home.approval_configure",
}

AVAILABLE_NODES = (
    CONSUMED_BY_APPROVAL_CENTER |
    CONSUMED_IN_LINE_1
    | CONSUMED_BY_P4
    | CONSUMED_BY_RBAC_3
    | CONSUMED_BY_RBAC_3_BATCH_2
    | CONSUMED_BY_P4FU_B
    | CONSUMED_BY_ORDER_LIST_PHASE1
    | CONSUMED_BY_ORDER_DETAIL_REPLICA
    | CONSUMED_BY_P3E_TASK_3
    | CONSUMED_BY_ORDER_PRICE_PAYSTATUS
    | CONSUMED_BY_RBAC_WIRING_REMAINING
    | CONSUMED_BY_RBAC_WIRING_REMAINING_2
    | CONSUMED_BY_RBAC_WIRING_REMAINING_3
    | CONSUMED_BY_RECOGNITION_SETTINGS
    | CONSUMED_BY_ORDER_DELETE_AND_RECYCLE
    | CONSUMED_BY_MY_PROFILE
    | CONSUMED_BY_MY_PROFILE_PASSWORD
    | CONSUMED_BY_PEER_ORDER_SYNC
    | CONSUMED_BY_SCREENSHOTS
    | CONSUMED_BY_ORDER_EXPORT
    | CONSUMED_BY_OPENAPI_APP_MANAGE
    | CONSUMED_BY_XGJ_AUTO_SHIP
    | CONSUMED_BY_TRADE_ACTION_LOG
    | CONSUMED_BY_SALES_AGENT_CHANGE
    | CONSUMED_BY_FEEDBACK
    | CONSUMED_BY_TAKER_OPEN_API
    | CONSUMED_BY_REALNAME_ADMIN
    | CONSUMED_BY_SYSTEM_SWITCHES
    | CONSUMED_BY_UPSTREAM_PRICE_LOG
    | CONSUMED_BY_SUPPORT_STATS
    | CONSUMED_BY_NOTICE_AND_PUSH
    | CONSUMED_BY_HOME_QR
    | CONSUMED_BY_QQBOT
    | CONSUMED_BY_WITHDRAW_REVIEW
    | CONSUMED_BY_WALLET_ADJUST
    | CONSUMED_BY_XGJ_RECEIPT
    | CONSUMED_BY_CHANNEL_ARTICLES
    | CONSUMED_BY_PLATFORM_INTEGRATION
    | CONSUMED_BY_PLATFORM_AUTO_REPLY
    | CONSUMED_BY_PLATFORM_WRITE
    | CONSUMED_BY_CUSTOMER_SELF_ONLY
    | CONSUMED_BY_CUSTOMER_FLIP
    | CONSUMED_BY_WECOM
)

#: 105 的「文档侧原文 / 代码侧新值」由 `RETOOLED_BENCHMARK_IDS` 表达，不另开例外表。
#: B5 把两边都钉死。


class StructureBackstopTests(TenantTestCase):
    def setUp(self):
        seed_catalog()
        seed_builtin_roles()

    def test_b1_default_roles_do_not_drift_from_the_seeded_roles(self):
        """常量与库里的内置角色必须逐字一致——否则新租户与存量租户的职位名会两套。

        ⚠️ 新建租户的 Role 表 = `DEFAULT_ROLES` ∪ `{"人事"}` **恰好**：内置「人事」进
        `rbac.seed.BUILTIN_ROLE_MODULES`，而 `apps/business/roles.py::DEFAULT_ROLES`
        （**供给时**的 Group 种子）刻意不动——加进去会让每个新租户多一个职位、
        并改掉 `DEFAULT_ROLES` 的顺序契约。
        判据仍是**恰好集合**（⛔ 没有放宽成 `toContain`）：
        - 从 `seed.py` 里删掉「人事」⇒ 这里红；
        - 往 `seed.py` 里再塞一个 `DEFAULT_ROLES` 之外的角色 ⇒ 这里也红。
        ⚠️ 期望值写**字面量**（E-0024）：引 `BUILTIN_ROLE_MODULES` 就是自指，改坏两侧一起变。
        ⚠️ 存量租户**不会**自动多出「人事」（`seed_builtin_roles` 对已存在角色 `continue`）
        ——已知代价，不是本断言能覆盖的事。
        """
        self.assertEqual(
            sorted(Role.objects.values_list("name", flat=True)),
            sorted(set(DEFAULT_ROLES) | {"人事"}),
        )

    def test_b2_every_available_node_is_referenced_outside_the_catalog(self):
        """`available=True` 的点必须在 `catalog.py` **之外**被引用（spec §4.4）。

        **判据的两种形态**：`has_node("order.delete")` 直接写，或经常量表间接传参
        （`views.py` 的 `ACTION_NODES = {"create": "system.role_authority_edit", …}`）。
        所以抽取规则取"**code 作为字符串字面量出现在 `catalog.py` 之外**"，
        而不是只认 `has_node("…")` 这一种写法——只认字面调用时本断言**恒红**：
        常量表写法下一个字面调用都没有（执行前冲突扫描实测）。

        **为什么排除 `catalog.py`**：那是定义处，不排除会让本断言自我满足。

        **覆盖哪些点**：**全部** `available=True` 的点，含 `flag`——
        `flag` 的消费方是读取点（如 `apps/business/order_flags.py` 的节点 code 常量），
        不是视图上的门。⛔ **不排除 `flag`**：排除会让「某条 `flag` 标了 `available=True`、
        但消费方后来被删掉」落在这个兜底之外。

        ⚠️ **必须走 AST、不能读文本**（变异实测过的假保证路径）：
        读**文本**时，一句**注释或 docstring** 就能满足本断言——把 `order.list` 翻成 `available=True`
        （真孤儿点）后，全仓唯一命中是 `apps/rbac/permissions.py` 的 **docstring** 里那句
        `has_node("order.list")`，B2 照样绿。而本断言的全部意义就是"**没人记得时也会红**"，
        被一句注释满足等于假保证（E-0009 / E-0024 同族）。
        注释根本不在 AST 里；docstring 是 `Expr(Constant)`，按"函数 / 类 / 模块体的第一个表达式"认领排除。
        """
        import ast

        codes = {n.code for n in PermissionNode.objects.filter(available=True)}
        # ⚠️ **恰好断言的自证**：`codes` 为空时下面的 `missing` 恒为空，
        # 本断言就真空绿——而"孤儿 available 点"的全部意义正是"没人记得时也会红"。
        # 字面量写死，不写集合长度：删掉已翻真的点时，长度会跟着缩、这条不红。
        # 点数变了先核消费方清单再改数字，不要按两条分支的读数相加。
        # ⭐ 合并树实数（2026-11-11 合并）：86 → **89** = 客户线翻真 +2
        #（`customer.member_list` / `customer.member_analysis_report`）+ 企微线 +1
        #（`system.wecom_settings`，消费方 = `apps/wecom/views.py::WecomSettingsGateView` 的类体装配）。
        self.assertEqual(
            len(codes), 93, "available=True 的点数变了——先核 B4 的消费方清单再改这个字面量"
        )
        referenced = set()
        for path in APPS_DIR.rglob("*.py"):
            if path.name == "catalog.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstrings = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    body = getattr(node, "body", None)
                    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                            and isinstance(body[0].value.value, str):
                        docstrings.add(id(body[0].value))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                        and id(node) not in docstrings:
                    referenced.add(node.value)
        missing = sorted(codes - referenced)
        self.assertEqual(missing, [], f"这些点标了 available=True，但 catalog.py 之外没有代码引用它们：{missing}")

    def test_b5_catalog_matches_the_reference_document_row_by_row(self):
        """目录与 `docs/JIDANPU-PERMISSION-CATALOG.md` **逐行一致**。

        **为什么需要一条持久的护栏**：清单文档是机器解析进 `catalog.py` 的，当时的校验器
        一次性、没进仓库——此后任何人手改 `catalog.py` 里的一条 `note`，没有任何东西会红。
        这条测试把清单文档重新解析一次当**唯一权威**，对拍全部 197 条功能点的
        `(name, note, url)`——**唯一例外是 `flag` 行**（代码侧 21 条，见下）：
        它们的 `url` 在代码侧按既定口径留空、在文档侧是对标开关名，比较时两边同时归一到 `""`
        （理由与判据见下）。

        解析规则（只认明细表的形状，天然跳过文档里其它表）：
        以 `|` 开头、恰好 **5** 个单元格、首格是纯数字、末格属于 `Y` / `N` / 空。

        ⚠️ 判据口径：文档仍 197 行**一行不删**，`catalog.py` 按业务方裁定裁掉了
        `PRUNED_BENCHMARK_IDS` 那些对标 id（现 **55** 条；2026-11-11 业务方拍板
        客户维护组收敛 +8）
        ⇒ 判据是「**代码侧恰好 = 文档 − 被裁集合**」+「被裁的确实不在代码里」
        +「存活行仍逐字对拍」。这不是放宽：多裁一条、少裁一条、裁了又加回来都会红
        （见方法尾部 ①–④b 那几段断言）。
        """
        doc = (pathlib.Path(__file__).resolve().parent.parent.parent
               / "docs" / "JIDANPU-PERMISSION-CATALOG.md")
        assert doc.exists(), f"清单文档不在预期位置：{doc}"
        from_doc = {}
        for line in doc.read_text(encoding="utf-8").splitlines():
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) != 5 or not cells[0].isdigit() or cells[4] not in ("Y", "N", ""):
                continue
            from_doc[int(cells[0])] = (cells[1], cells[2], cells[3].strip("`").strip())
        # ⚠️ **flag 类的 `url` 按既定口径留空**，比较键必须带上这条口径：
        # `catalog.py` 的模块 docstring 写明"对标在 URL 列给的是语义开关名（`IsOnlyViewSelfOrder`
        # 这类），那不是 URL"，`test_rbac_catalog.py::test_api_kind_has_url_and_flag_kind_has_none`
        # 把"flag 无 URL"钉成不变式。若拿 `(name, note, url)` 直接对拍全部 197 行，
        # 24 条 flag 会以"代码侧 `''` vs 文档侧开关名"全红（差异恰好只有 `url` 一列）。
        # 处理方式：两边**同时**把 flag 行的 url 归一到 `""`——比的是"两边都有权威的那部分"，
        # 而不是单方面放宽；文档解析规则与 197 的期望值一个字没动。
        flag_ids = {b for _c, b, _n, _note, _u, k, _a in NODES if k == NodeKind.FLAG}
        # 豁免本身要有判据，否则上面那次归一化会变成"flag 的 url 随便改都不红"：
        # 代码侧 flag 条数写死，且文档侧给它们的确实是对标开关名（不以 `/` 开头），不是 URL。
        # ⚠️ 被裁的 55 条中 3 条是**对标** `flag`（`order.restrict_agree_revoke` /
        # `order.restrict_manual_accept_platform` / `order.restrict_reopen_completed`）
        # ⇒ 对标 flag 代码侧 **21** 条；文档侧仍有那 24 行（一行不删），子集判据仍成立。
        # ⭐ 2026-11-11 第五批起新增**自有** flag `customer.member_list_self_only`
        #（`benchmark_id=None`，文档侧没有它的行）⇒ 代码侧 flag 合计 **22** 条；
        # 文档侧的子集 / 开关名判据只盯**非 None** 的那 21 条（`doc_flag_ids`）。
        self.assertEqual(len(flag_ids), 22, "flag 类条数变了——豁免口径要重核")
        doc_flag_ids = {b for b in flag_ids if b is not None}
        self.assertEqual(len(doc_flag_ids), 21, "对标 flag 条数变了——豁免口径要重核")
        self.assertTrue(
            doc_flag_ids <= set(from_doc)
            and all(from_doc[b][2] and not from_doc[b][2].startswith("/") for b in doc_flag_ids),
            "文档里 flag 行的 URL 列应是对标开关名（非 URL）——形态变了就要重核豁免",
        )
        # ⚠️ **文档侧开关名另有缺口要钉**：上面的归一化把
        # flag 的 url 两边都抹成 `""`，于是两个开关名互换、或复制粘贴成同名，仍然全绿。
        # 最低成本的一条判据是它们**互不相同**——它挡得住"两条写成同一个名字"，
        # **挡不住**两行之间的互换（代码侧按 spec §4.2 `flag` 不落 url，没有逐行锚点可分）。
        # 那个残留缺口如实登记在 task-final-report.md 的未覆盖表里。
        # ⚠️ 自有 flag（benchmark_id=None）不在 from_doc ⇒ 这份互异判据只盯 21 条对标 flag。
        flag_names = [from_doc[b][2] for b in doc_flag_ids]
        self.assertEqual(
            len(flag_names), 21,
            "对标 flag 的条数变了——豁免口径要重核",
        )
        self.assertEqual(
            len(set(flag_names)), 21,
            "21 个 flag 的对标开关名不再互不相同——清单文档或豁免口径被抄错了",
        )
        # ⛔⭐ **过滤必须下在"字典构造处"**——自有节点
        # `system.allow_taker_open_api` 的 `benchmark_id` 是 `None`，而 `None` **没有**对标行
        # ⇒ 不过滤的话下面 ②③④ 三条等号断言**全都红**（而且报的是"catalog 与清单文档不一致"
        # 这种**误导性**的话）。⚠️ **只改 ② 那条等号不够**：④ 的"存活行逐字对拍"遍历同一个字典。
        # ⚠️ 自有节点本身的判据**不在这里**，而是下面那条
        # `test_own_nodes_are_exactly_the_self_owned_list`（它按清单**恰好**断言）。
        from_code = {
            b: ((n, note, u) if b not in flag_ids else (n, note, ""))
            for _c, b, n, note, u, _k, _a in NODES
            if b is not None
        }
        from_doc = {
            b: ((n, note, u) if b not in flag_ids else (n, note, ""))
            for b, (n, note, u) in from_doc.items()
        }
        # ① 文档基线不动：197 行一条不少（对标清单的权威转录）。
        # 改造行用 `RETOOLED_BENCHMARK_IDS` 表达代码侧新值，不改文档基线。
        self.assertEqual(len(from_doc), 197, "清单文档的明细行数变了——先核文档再核代码")
        # ② 代码侧恰好 = 文档 − 被裁集合（**等号，不是 `>=`**）。多裁/少裁都红。
        self.assertEqual(
            set(from_code), set(from_doc) - PRUNED_BENCHMARK_IDS,
            "catalog.py 的 benchmark_id 集合与「文档 − 被裁集合」不相等",
        )
        # ③ 被裁的那些**确实不在**代码里（防「裁了又加回来」）。
        self.assertEqual(set(from_code) & PRUNED_BENCHMARK_IDS, set())
        # ④ 存活行仍然逐字对拍（这一条是原来那条网的全部价值）。
        #    ⚠️ `RETOOLED_BENCHMARK_IDS`（105 / 141 / 1226）要显式排除：它们是被**改造**的，
        #    不是被裁的，文档侧仍是对标原文 ⇒ 逐字对拍必然红。排除由 ④b 自证，不是放宽。
        survivors = {
            b: v for b, v in from_doc.items()
            if b not in PRUNED_BENCHMARK_IDS and b not in RETOOLED_BENCHMARK_IDS
        }
        self.assertEqual(
            {b: v for b, v in from_code.items() if b not in RETOOLED_BENCHMARK_IDS},
            survivors,
            "catalog.py 与清单文档不一致（改文档要重跑生成器，别手改 code）",
        )
        # ④b 改造的那些行：**确实还在代码里**、值 == 裁定的新值、且文档侧仍是原来的对标文本。
        #    三条合起来把「排除」这个例外钉死——改错值或顺手改文档都会红。
        self.assertEqual(
            set(from_code) & RETOOLED_BENCHMARK_IDS, set(RETOOLED_BENCHMARK_IDS),
            "被改造的点从代码里消失了（那是裁剪，不是改造）——口径要重核",
        )
        self.assertEqual(
            {b: from_code[b] for b in RETOOLED_BENCHMARK_IDS},
            RETOOLED_FROM_CODE,
            "改造行的代码侧现值与裁定不符（105 = spec §3.5.2 / 附录 A2；"
            "141 / 1226 = 2026-11-11 客户口径改名裁定）",
        )
        self.assertEqual(
            {b: from_doc[b] for b in RETOOLED_BENCHMARK_IDS},
            RETOOLED_FROM_DOC,
            "清单文档里改造行被改动了——文档是对标转录，一行都不删/不改",
        )

    def test_own_nodes_are_exactly_the_self_owned_list(self):
        """⭐ `benchmark_id is None` 的节点**恰好** = 自有节点清单。

        **为什么单独一条**：上面 B5 的对拍**刻意把 `None` 过滤掉**（那份清单文档里没有自有
        节点，留着会让三条等号断言以"catalog 与文档不一致"的**误导性**话变红）⇒
        过滤那一刀之后，"自有节点"这件事**一条判据都没有**了。这一条把它补回来，且是**等号**
        （⛔ 不是"包含"）：想加第二条自有节点就必须**显式**改这里、并同一次在
        `catalog.py` 的模块 docstring 登记表与 `EXPECTED_NODE_COUNT` / `EXPECTED_KIND_COUNTS`
        上各改一处。

        ⚠️ 断言的是 **code 集合的字面量**（E-0024），⛔ 不从别处 import 一份"自有节点清单"
        ——那样"清单被改坏"时这条会跟着坏、恒真。
        ⚠️ 顺带钉住**两个**容易静默漂的性质：这些点**必须** `available=True`（有真消费方，
        本仓第一条就是内部端点 `/api/taker/apps/…` 的类体节点门）、且**必须**是 `page` 形态
        （`url` 以 `/` 开头——它是页面，不是 API 点）。
        """
        own = {n[0]: n for n in NODES if n[1] is None}
        self.assertEqual(
            set(own),
            {
                "system.allow_taker_open_api",
                "system.edit_settings",
                "home.qr_manage",
                "home.approval_apply",
                "home.approval_penalty_approve",
                "home.approval_refund_handle",
                "home.approval_configure",
                "system.qqbot_settings",
                "finance.taker_withdraw_review",
                "finance.taker_wallet_adjust",
                "finance.xgj_receipt_detail",
                "system.article_manage",
                "order.platform_write",
                "customer.member_list_self_only",
                "system.wecom_settings",
            },
            "`benchmark_id is None` 的节点集合变了：自有节点是**逐条登记**的"
            "（`catalog.py` 模块 docstring 的登记表 + 本条判据 + 两处计数，同一次改）",
        )
        _code, _bid, _name, _note, url, kind, available = own["system.allow_taker_open_api"]
        self.assertTrue(available, "自有节点必须有真消费方才允许 available=True（T3 的合取口径）")
        self.assertEqual(kind, NodeKind.PAGE, "自有节点的形态是页面（`url` 是页面路由）")
        self.assertTrue(url.startswith("/"), "页面形态的 `url` 必须以 `/` 开头（网钉形态）")
        # ⭐ `system.edit_settings` 是**保存端点**（`api` 形态），
        #    与上一条（页面形态）刻意不同 ⇒ 上面那句「自有节点的形态是页面」只钉第一条。
        _code, _bid, _name, _note, url, kind, available = own["system.edit_settings"]
        self.assertTrue(available, "自有节点必须有真消费方才允许 available=True（T3 的合取口径）")
        self.assertEqual(kind, NodeKind.API, "本点的形态是保存端点（对标 `/SystemAjax/SystemOrderSetting`）")
        self.assertEqual(url, "/SystemAjax/SystemOrderSetting", "端点形态的 `url` 是实测到的对标保存端点")
        # ⭐ `home.qr_manage` 是**写端点门**（`api` 形态），
        #    `url` 落**我方自己的**端点路径（自造功能没有对标端点可溯源）。
        _code, _bid, _name, _note, url, kind, available = own["home.qr_manage"]
        self.assertTrue(available, "自有节点必须有真消费方才允许 available=True（T3 的合取口径）")
        self.assertEqual(kind, NodeKind.API, "本点的形态是写端点门（`/api/business/home/qr-codes/…`）")
        self.assertEqual(url, "/api/business/home/qr-codes/", "端点形态的 `url` 是我方自己的端点路径")
        # ⭐ `system.qqbot_settings` 是**设置页**（`page` 形态，与第一条同形）；
        #    `url` 是自拟的对标形态页路由（对标没有机器人对接这一项）。
        _code, _bid, _name, _note, url, kind, available = own["system.qqbot_settings"]
        self.assertTrue(available, "自有节点必须有真消费方才允许 available=True（T3 的合取口径）")
        self.assertEqual(kind, NodeKind.PAGE, "本点的形态是设置页（QQ 机器人设置的页面路由）")
        self.assertTrue(url.startswith("/"), "页面形态的 `url` 必须以 `/` 开头（网钉形态）")
        self.assertTrue(
            url.startswith("/System/"),
            "本点的 `url` 落对标 `system` 栏目的设置页形态 `/System/…`（与登记表一致）",
        )
        # ⭐ `finance.xgj_receipt_detail` 是**读端点门**（`api` 形态），
        #    `url` 落**我方自己的**端点路径（同 `home.qr_manage` 的口径）。
        _code, _bid, _name, _note, url, kind, available = own["finance.xgj_receipt_detail"]
        self.assertTrue(available, "自有节点必须有真消费方才允许 available=True（T3 的合取口径）")
        self.assertEqual(kind, NodeKind.API, "本点的形态是读端点门（`/api/business/xgj-receipts/…`）")
        self.assertEqual(url, "/api/business/xgj-receipts/", "端点形态的 `url` 是我方自己的端点路径")
        # ⭐ `customer.member_list_self_only` 是**限制语义的 flag**（客户线第五批，2026-11-11）：
        #    本仓自有节点里第一条 flag 形态——`url` 落空（flag 不落 URL，与对标 flag 同口径）。
        _code, _bid, _name, _note, url, kind, available = own["customer.member_list_self_only"]
        self.assertTrue(available, "自有节点必须有真消费方才允许 available=True（T3 的合取口径）")
        self.assertEqual(kind, NodeKind.FLAG, "本点是限制语义的 flag（勾上后行集收窄）")
        self.assertEqual(url, "", "flag 形态不落 URL（与对标 flag 同一口径）")
        # ⭐ `system.wecom_settings` 是**设置页**（`page` 形态，与 `system.qqbot_settings` 同形）；
        #    `url` 落**我方自己的** v2 前端路由（本页 v2-only，D-0344：旧壳无此页）。
        _code, _bid, _name, _note, url, kind, available = own["system.wecom_settings"]
        self.assertTrue(available, "自有节点必须有真消费方才允许 available=True（T3 的合取口径）")
        self.assertEqual(kind, NodeKind.PAGE, "本点的形态是设置页（企微自建应用设置的页面路由）")
        self.assertEqual(url, "/v2/wecom-settings", "本点的 `url` 是我方 v2 路由（登记表同值）")

    def test_b6_node_tree_returns_available_with_its_real_value(self):
        """树接口真的把 `available` 的**值**透出来了。

        早前的判据只有 `assertIn("available", row)`，而 `row` 取的是**子节点**——
        把 selectors 里那行删掉或写死 `False`，那几条测试照样绿。
        这里钉**两端**（字面值，不是引用常量 ⇒ 改坏 `catalog.py` 会红）：
        `order.list` 是③线第一批翻真过的点、必须是 `True`；
        `order.toggle_urgent` 明确**不**翻（「订单加急或取消加急」功能未实现 ⇒
        没有消费方）、必须是 `False`。
        ⚠️ 夹具用 `order.toggle_urgent`：同为 `available=False` 且仍在 `wiring.PENDING`
        （理由「功能未实现」）。断言强度不变：仍是「order 栏目下必须有这么一条 `False`」。
        """
        from apps.rbac.selectors import node_tree

        by_code = {m["code"]: m for m in node_tree()}
        order = by_code["order"]
        self.assertIs(order["available"], False)          # 栏目行恒 False（见 spec §5.3 追加之二）
        child = {c["code"]: c for c in order["children"]}
        self.assertIs(child["order.list"]["available"], True)
        self.assertIs(child["order.toggle_urgent"]["available"], False)
        flipped = {c["code"]: c for c in by_code["system"]["children"]}
        self.assertIs(flipped["system.role_authority"]["available"], True)

    def test_b7_node_tree_is_gated_not_allow_any(self):
        """树接口的判据**真的接上了**。

        只用 superuser 的用例测不出这个：判据对 superuser **短路**，
        即使 `get_permissions` 返回 `AllowAny` 也照样绿。这里用**非** superuser 双向钉：
        只持无关节点 ⇒ 403；持 `system.role_authority` ⇒ 200。
        """
        from django.contrib.auth.models import Group
        from tests.p5_auth_helpers import make_logged_in_client

        from apps.rbac.models import PermissionNode, Role

        def _client_with(username, *codes):
            client = make_logged_in_client(username=username, password="pw-123456")
            # ⚠️ `Role.name` 的 `max_length=5` 是**要保住的一侧**（spec §3.3 照抄对标表单
            # maxlength）：形如 `r-{username}` 的名字（13 字）会撞 Postgres 的
            # `varchar(5)` 抛 `DataError`。取用户名前 5 字：两个用户名仍然各自唯一，
            # 而本用例的判据是**门禁接没接上**，与角色名长度无关。
            role_name = username[:5]
            role = Role.objects.create(
                name=role_name, group=Group.objects.create(name=role_name)
            )
            role.permissions.set(PermissionNode.objects.filter(code__in=codes))
            get_user_model().objects.get(username=username).groups.add(role.group)
            return client

        self.assertEqual(
            _client_with("g-unrelated", "order.toggle_urgent").get("/api/permission-nodes/").status_code, 403
        )
        self.assertEqual(
            _client_with("g-entry", "system.role_authority")
            .get("/api/permission-nodes/").status_code, 200
        )

    def test_b8_node_tree_children_follow_the_reference_order(self):
        """children 的**前三个**顺序 = 对标顺序（本断言只钉 `[:3]`，不主张全量）。

        顺序由 `PermissionNode.Meta.ordering = ["benchmark_id"]` 与 Django 多值反向 FK 的
        prefetch 路径共同决定（该路径**不** `clear_ordering`）。钉**字面 code 列表**。

        ⚠️ **brief 原文给的 `order.add_or_edit` / `order.show_content` 是转写错误**：
        这两个 code 在 `catalog.py` 里不存在（`git grep` 全仓只有计划与 brief 命中），
        冻结的 code 是 `order.add_edit`（对标 Menu_ID 12）/ `order.view_content`（15），
        与 `docs/JIDANPU-PERMISSION-CATALOG.md` 第 [5] 订单业务 表的 12/14/15 三行逐字对应。
        照抄 brief 会让本断言**恒红**，且红的原因不是"顺序漂了"而是"期望值抄错了"。
        """
        from apps.rbac.selectors import node_tree

        order = next(m for m in node_tree() if m["code"] == "order")
        self.assertEqual(
            [c["code"] for c in order["children"]][:3],
            ["order.add_edit", "order.list", "order.view_content"],
        )

    def test_b3_role_and_group_never_drift(self):
        """无孤儿 `Role`（没有同名 Group）、无孤儿内置 Group（没有同名 Role）。"""
        for role in Role.objects.all():
            self.assertEqual(role.group.name, role.name)
        for name in DEFAULT_ROLES:
            self.assertTrue(
                Role.objects.filter(name=name).exists(), f"{name} 有 Group 没有 Role"
            )

    def test_b4_no_orphan_available_node(self):
        """孤儿 `available` 点 = 0：全集**恰好** = `AVAILABLE_NODES`（**等号，不是 `>=`**）。

        全集 = 上面全部 `CONSUMED_BY_*` / `CONSUMED_IN_LINE_1` 集合的并集，
        全是**硬编码字面量**（E-0024）。
        ⚠️ 条数不写在这里（写死必过期）：以 `AVAILABLE_NODES` 的等号断言与本网自报为准。
        少翻一个 ⇒ 红；多翻一个 ⇒ 也红；
        某条线把集合改小却不同步 ⇒ 也红。
        """
        self.assertEqual(
            set(PermissionNode.objects.filter(available=True).values_list("code", flat=True)),
            AVAILABLE_NODES,
        )

    def test_b9_builtin_role_flags_match_the_explicit_constant(self):
        """每个内置角色的 `flag` 集合 == `BUILTIN_ROLE_FLAGS`（逐条相等）；客服为空。

        ⚠️ **为什么需要它**：`seed_builtin_roles` 的模块批发
        **排除** `flag` 点（`flag` 的极性逐条不同——按模块给等于把一批限制型 flag 一起打开，
        会让客服被"禁止录入新订单"套住；放开型的批量给出同样是越权。口径见 spec §4.3）。
        可是"排除了"与"排多了 / 排少了"别处看不出来——`test_rbac_seed.py` 只钉几个计数，
        **相同的计数可以由不同的集合凑出来**。所以把一处常量的逐条相等钉在这里：
        把某条 flag 重新塞回模块批发、或从常量里偷偷加一条，都会红。

        ⚠️ 客户端的 flag 集合为空是**单独**再钉一遍的（不靠"常量是空"传递）：
        它是 spec §6.1 唯一被点名会与限制冲突的角色（"负责订单操作，如**录单**…"）。
        """
        def flags_of(name):
            return set(
                Role.objects.get(name=name).permissions
                .filter(kind=NodeKind.FLAG)
                .values_list("code", flat=True)
            )

        for name, expected in BUILTIN_ROLE_FLAGS.items():
            self.assertEqual(
                flags_of(name), set(expected),
                f"{name} 的 flag 集合与 BUILTIN_ROLE_FLAGS 不一致",
            )
        self.assertEqual(flags_of("客服"), set(), "客服被种上了限制性 flag")
        # 常量本身的覆盖面自证：每个内置角色都有一份决策（漏一个 ⇒ 上面循环漏测一个）。
        self.assertEqual(
            sorted(BUILTIN_ROLE_FLAGS), sorted(Role.objects.values_list("name", flat=True))
        )

    def test_b10_no_builtin_role_is_restricted_by_the_two_account_restrict_flags(self):
        """逐条决策（业务方拍定）：**五个内置角色都不给**这两条限制。

        ⚠️ **为什么不能只靠 B9**（变异实测指出）：B9 拿 `BUILTIN_ROLE_FLAGS` 与
        它**自己派生出来的**库内授权比（`seed_builtin_roles` 正是从那个常量授权的）
        ⇒ 对 4 个角色是**自指**，改坏"谁被限制"这个决策它不会红（E-0024 族）。
        本用例把"都不给"这个**决策**写成字面量：哪天有人把某条 flag 塞进某个角色，
        它会红，而不是靠一句注释守着。⚠️ 它**不替代** B9：B9 管"常量与库不漂移"，本用例管"决策没被改"。
        """
        two = {
            "system_user.restrict_view_id_card",
            "system_user.restrict_view_commission_params",
        }
        offenders = {
            name: sorted(set(codes) & two)
            for name, codes in BUILTIN_ROLE_FLAGS.items()
            if set(codes) & two
        }
        self.assertEqual(
            offenders,
            {},
            "有内置角色被这两条账号限制 flag 限制了——这与 2026-09-21 业务方的裁定不符"
            "（裁定原文见 docs/plans/2026-09-21-rbac-batch2-system-user.md §2 分叉 A）",
        )

    def test_b11_order_add_log_is_granted_by_the_module_wholesale(self):
        """「**默认角色不受影响**」判据（I-1）。

        「添加订单日志」的挂载节点是专门节点 `order.add_log`（27，不是 `order.view_detail`），
        `catalog.available` 已翻真。**授权有没有变**要靠一条能跑的判据回答，
        不能只写注释（E-0011「登记与实现必须一致」）：

        - `seed_builtin_roles` 对 `order` 栏目**全批**非 flag 子节点（`BUILTIN_ROLE_MODULES`）
          ⇒ 客服 / 客服主管 / 系统管理员**本来就有** 27 —— 翻真前后这四行逐字不变；
        - 代练员 / 系统开发员的批发栏目里**没有** `order` ⇒ 不给（不是"全站默认给"）。

        ⚠️ 判据写**字面量**（`order.add_log` 与被点名的角色名），不引 `BUILTIN_ROLE_MODULES`
        派生——否则把 `order` 从客服的批发栏目里删掉时，本用例会跟着变、不红（E-0024 族）。
        """
        def codes_of(name):
            return set(
                Role.objects.get(name=name).permissions.values_list("code", flat=True)
            )

        for name in ("客服", "客服主管", "系统管理员"):
            self.assertIn(
                "order.add_log", codes_of(name),
                f"{name} 按 order 栏目批发，应含 order.add_log（默认角色被这次换节点影响了）",
            )
        for name in ("代练员", "系统开发员"):
            self.assertNotIn(
                "order.add_log", codes_of(name),
                f"{name} 的批发栏目里没有 order，不应含 order.add_log",
            )

    def test_b12_taker_defaults_include_the_submit_node(self):
        """代练员默认权限必须含「提交完单和提交问题单」点——否则非超管代练员点它是 403（spec §7.1 的现存 bug）。

        代练员默认拿到 `order.submit_work`，来源是 `BUILTIN_ROLE_MODULES["代练员"]` 元组末尾的显式单点。
        这个 code 也是「报问题单」的门。判据钉的是这个 code 本身。
        """
        role = Role.objects.get(name="代练员")
        self.assertIn(
            "order.submit_work",
            set(role.permissions.values_list("code", flat=True)),
        )

    def test_b13_the_submit_node_is_the_retooled_105(self):
        """改造的证据：`benchmark_id` 仍是 105，`code` 已是新的。

        ⚠️ `name` 期望值是「提交完单和提交问题单」（字面值，E-0024：
        不从 `catalog.py` 派生，否则改坏 catalog 本条不红）。
        """
        node = PermissionNode.objects.get(benchmark_id=105)
        self.assertEqual(node.code, "order.submit_work")
        self.assertEqual(node.name, "提交完单和提交问题单")
