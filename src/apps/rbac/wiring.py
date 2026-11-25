"""权限接线位点表：每个功能点要么有拦截位点，要么在 `PENDING` 里写明为什么还没接。

条数以 `catalog.EXPECTED_NODE_COUNT` 为准（含自有节点 `system.allow_taker_open_api`）。
`docs/RBAC-WIRING-MAP.md` 是生成物：改这里之后跑 `manage.py render_rbac_wiring_map`。

- **`SITES`**：能指出位点（可多个）。
- **`PENDING`**：只写拦截层为什么还没有，不编一个不存在的位点。
- 只有 `INTERCEPT` 与 `PENDING` 互斥。`EXPERIENCE` 可以并存：菜单声明了权限、
  服务端还没判据时，体验位点留着，原因写在 `PENDING`。
- `SITES ∪ PENDING` 恰好等于 `catalog.NODES`。

`INTERCEPT` 是服务端拦得住，`catalog.available=True` 必须与它同向。
`EXPERIENCE` 只是菜单 / 路由可见性，菜单隐藏不等于数据不可访问。
位点写成 `<仓库相对路径>::<锚点>`。拦截锚点是 `类.方法` 或类体上的 `permission_classes`；
体验锚点是路由名。

`IN_FLIGHT` 保持空集。没做完的点写进 `PENDING`，不要再拿「接线中」当理由。

接上判据时：写入 `INTERCEPT`、从 `PENDING` 删掉该 code（`EXPERIENCE` 保留），
并同一次把 `catalog.available` 翻真。目录新增点也要在这里登记。
"""
from dataclasses import dataclass

INTERCEPT = "intercept"
EXPERIENCE = "experience"

#: 允许的层取值（T5：拼错一个就红）。
LAYERS = (INTERCEPT, EXPERIENCE)


@dataclass(frozen=True)
class Site:
    """一个位点：`layer` 见模块 docstring；`path` = `<仓库相对路径>::<锚点>`。"""

    layer: str
    path: str
    note: str = ""



#: 已定：code → 位点（可多个）。**原则上**按 `catalog.NODES` 的顺序书写；历史并入线
#: （P4 等并入线）留下若干逆序（实测过）（例：`order.online_refund` 排在
#: `order.upstream_price_change_logs` 之后、P4/customer 整块顺序不同）。
#: **权威顺序以 `catalog.NODES` 为准**，不要拿本 dict 的键序当顺序依据。
#: 生成方式见模块 docstring；**手改这里之后必须重跑渲染命令**。
SITES: dict[str, tuple[Site, ...]] = {
    "home.approval_apply": (
        Site(INTERCEPT, "src/apps/business/api/approval_views.py::ApprovalCollectionView.post", "发起罚款"),
        Site(INTERCEPT, "src/apps/business/api/approval_views.py::ApprovalTargetsView.get", "查询罚款对象"),
        Site(INTERCEPT, "src/apps/business/api/approval_views.py::ApprovalImageTicketView.post", "签发证据截图上传凭证"),
        Site(INTERCEPT, "src/apps/business/api/approval_views.py::ApprovalImageUploadView.put", "上传审批证据截图"),
    ),
    "home.approval_configure": (
        Site(INTERCEPT, "src/apps/business/api/approval_views.py::ApprovalSettingsView.get", "查看审批配置"),
        Site(INTERCEPT, "src/apps/business/api/approval_views.py::ApprovalSettingsView.patch", "保存审批配置"),
    ),
    # ── 订单截图───────────────────────────────
    # 位点 = 本线落地的四个端点（`screenshot_views.py` / `screenshot_zip.py`）；
    # 节点 code 写成**模块常量**（`SCREENSHOT_*_NODE`），T2 判据向下解析一层常量。
    "order.view_screenshot": (
        Site(INTERCEPT, "src/apps/business/api/screenshot_views.py::ScreenshotCollectionView.get_permissions",
             "GET 动作（列表 + 每项短时效读链接）"),
    ),
    "order.upload_screenshot": (
        Site(INTERCEPT, "src/apps/business/api/screenshot_views.py::ScreenshotTicketView.get_permissions",
             "POST 动作（签发上传凭证）"),
        Site(INTERCEPT, "src/apps/business/api/screenshot_views.py::ScreenshotCollectionView.get_permissions",
             "POST 动作（认领落库）——与 view_screenshot 共用同一个 `get_permissions`，按方法分派"),
    ),
    "order.screenshot_manage": (
        Site(INTERCEPT, "src/apps/business/api/screenshot_views.py::ScreenshotDeleteView.get_permissions",
             "DELETE 动作（删图 + 记一条操作日志）"),
    ),
    "order.download_screenshots_zip": (
        Site(INTERCEPT, "src/apps/business/api/screenshot_zip.py::ScreenshotZipView.get_permissions",
             "GET 动作（打包下载）"),
    ),

    "order.add_edit": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::order-new", "菜单条目声明 permissions"),
        # ⚠️ D-0160 批量线 A11：说明文字补上批量的 `batch_types`（它在 `ORDER_PERMISSIONS`
        # 里与本节点同一个 code）⇒ 地图读者不会再以为这条位点只服务单笔；
        # ⛔ 改完**必须重渲** `docs/RBAC-WIRING-MAP.md`（T4 逐字比对）。
        Site(INTERCEPT, "src/apps/business/api/order_views.py::OrderViewSet.get_permissions", "create / partial_update / update / parse / batch_types 动作"),
    ),
    "order.list": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::orders", "菜单条目声明 permissions"),
        # ⭐ D-0219第二个体验位点：绩效列表的菜单条目（「打手与客服报酬」）
        # 也声明本节点（任一即见，与既有 `finance.payroll` 并列）——业务方裁定「有订单
        # 管理（修改）权限的人就该能进绩效列表、改打手绩效」。路由侧同批：
        # `routePermissions.ts` 的 `'agents-performance'` 改成数组值（本表第一条多节点任一）。
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::agents-performance", "菜单条目声明 permissions（D-0219：与 finance.payroll 任一即见）"),
        # ⚠️ D-0144给本节点加了**第二个消费方**（候选名单端点
        # `filter_options`，spec §5.6）⇒ 说明文字连同改准；⛔ 改完**必须重渲**
        # `docs/RBAC-WIRING-MAP.md`（`test_rbac_wiring` 的 `--check` 逐字比对）。
        # ⭐（「显示人统一成昵称 + 销售客服候选」线）：第三个消费方
        # `kefu_options`（订单管理页「销售客服」候选）同 code ⇒ 一并点名。
        Site(INTERCEPT, "src/apps/business/api/order_views.py::OrderViewSet.get_permissions", "list、filter_options、dictionary_options 和 kefu_options"),
        # ⭐ D-0219第四个消费方：绩效列表端点把本节点接进了「进得了列表」
        # 那道门（任一即进：1163 ∪ 本节点）；本节点同时仍是数据范围的收口
        # （`PERFORMANCE_MANAGEMENT_NODE`：持它看全部行、不持只看自己的行）。
        Site(INTERCEPT, "src/apps/business/api/performance_views.py::PerformanceViewSet.get_permissions", "list 动作（D-0219：任一即进的第二把钥匙 + 数据范围收口）"),
    ),
    # ⭐ 交易平台涉钱写动作线（审计 M5）：本仓**第九条自有节点**。
    # 写端点从 `order.list` 换到本点；**读**端点（推送列表 / 预览 / 区服 / 摘要 /
    # 留言列表 / 进度图 / 按日统计 / 模板列表 GET）仍留在 `order.list` 的消费方里。
    # ⚠️ 位点的节点 code 都走模块常量字面量（`PLATFORM_WRITE_NODE`），T2 会向下解一层。
    "order.platform_write": (
        Site(INTERCEPT, "src/apps/business/api/platform_push_action_views.py::_permission_denial",
             "POST 批量撤回（withdraw）+ 批量确认完单（accept）"),
        Site(INTERCEPT, "src/apps/business/api/dailiantong_push_views.py::DailiantongPushView.permission_classes",
             "POST 抛单（真钱冻结 + 建平台单）"),
        Site(INTERCEPT, "src/apps/business/api/dailiantong_action_views.py::DailiantongWithdrawView.permission_classes",
             "POST 撤回"),
        Site(INTERCEPT, "src/apps/business/api/dailiantong_action_views.py::DailiantongPriceView.permission_classes",
             "POST 改价"),
        Site(INTERCEPT, "src/apps/business/api/dailiantong_action_views.py::DailiantongTimeView.permission_classes",
             "POST 补时"),
        Site(INTERCEPT, "src/apps/business/api/dailiantong_action_views.py::DailiantongMessageView.permission_classes",
             "POST 发留言（上游账户级写）"),
        Site(INTERCEPT, "src/apps/business/api/dailiantong_action_views.py::DailiantongWithdrawAllView.permission_classes",
             "POST 逐平台撤回"),
        Site(INTERCEPT, "src/apps/business/api/dailiantong_action_views.py::DailiantongFinishView.permission_classes",
             "POST 确认完单（扣钱）"),
        Site(INTERCEPT, "src/apps/business/api/dailiantong_action_views.py::DailiantongRepushView.permission_classes",
             "POST 重发（再建一张真钱单）"),
        Site(INTERCEPT, "src/apps/business/api/dailiantong_extra_views.py::DailiantongUpPriceView.permission_classes",
             "POST 加价"),
        Site(INTERCEPT, "src/apps/business/api/dailiantong_extra_views.py::DailiantongArbitrationView.permission_classes",
             "POST 申请客服介入"),
        Site(INTERCEPT, "src/apps/business/api/dailiantong_extra_views.py::DailiantongAgreeCancelView.permission_classes",
             "POST 同意撤销（付代练费 + 赔保证金）"),
        Site(INTERCEPT, "src/apps/business/api/dailiantong_extra_views.py::DailiantongCancelWithdrawView.permission_classes",
             "POST 取消撤销"),
        Site(INTERCEPT, "src/apps/business/api/dailiantong_extra_views.py::DailiantongValueView.permission_classes",
             "POST 评价打手"),
        Site(INTERCEPT, "src/apps/business/api/dailiantong_extra_views.py::DailiantongOffSellView.permission_classes",
             "POST 上架 / 下架"),
        Site(INTERCEPT, "src/apps/business/api/dailiantong_extra_views.py::DailiantongLockView.permission_classes",
             "POST 锁定 / 解锁"),
        Site(INTERCEPT, "src/apps/business/api/dailiantong_extra_views.py::DailiantongBlacklistView.permission_classes",
             "POST 拉黑 / 解拉黑打手"),
        Site(INTERCEPT, "src/apps/business/api/dailiantong_extra_views.py::DailiantongAmountView.permission_classes",
             "POST 补款 / 加款"),
        Site(INTERCEPT, "src/apps/business/api/dailiantong_extra_views.py::DailiantongEditView.permission_classes",
             "POST 编辑平台订单"),
        Site(INTERCEPT, "src/apps/business/api/dailiantong_extra_views.py::DailiantongScreenshotView.permission_classes",
             "POST 追加进度图（上游账户级写）"),
        Site(INTERCEPT, "src/apps/business/api/dailiantong_extra_views.py::DailiantongOrderTemplateDetailView.permission_classes",
             "PATCH / DELETE 模板（上游账户级写）"),
        Site(INTERCEPT, "src/apps/business/api/dailiantong_extra_views.py::DailiantongOrderTemplateDefaultView.permission_classes",
             "POST 设默认模板"),
        Site(INTERCEPT, "src/apps/business/api/dailiantong_extra_views.py::DailiantongOrderTemplateListView.get_permissions",
             "POST 新增模板（GET 只读仍用 order.list，按方法分派）"),
    ),
    "order.delete": (
        # ⭐ D-0144：翻真。拦截位点在 `get_permissions()` 的 `ORDER_PERMISSIONS["delete"]`。
        # ⭐ D-0160 批量线 A11：批量软删 `batch_delete` 与本节点**同一个 code**
        # （批量不是一种新权限，只是同一件事的批形态）⇒ 说明文字点名它（T7 的判据）。
        Site(INTERCEPT, "src/apps/business/api/order_views.py::OrderViewSet.get_permissions", "delete、batch_delete、batch_delete_v2"),
    ),
    "order.recycle": (
        # ⚠️ `EXPERIENCE` **保留**（菜单确实声明了 permissions）+ 新增 `INTERCEPT`
        # （同一个点同时管「进页」「看列表」「还原」三件事——照对标 spec §2.1.4）。
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::orders-recycle", "菜单条目声明 permissions"),
        Site(INTERCEPT, "src/apps/business/api/order_views.py::OrderViewSet.get_permissions", "recycle_bin 与 restore 两个动作"),
    ),
    "order.taobao_ship_records": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::orders-taobao-ship", "菜单条目声明 permissions"),
        # ⭐ 自动发货线（spec `2026-10-03-xgj-auto-ship-design.md` §3.7）：
        # 拦截层**接上了**——两个动作（只读列表 + 重试发货）都挂本点，
        # 装配在 `TaobaoOrderSendLogViewSet.get_permissions` 的
        # `TB_SEND_LOG_PERMISSIONS` 里（类体常量，T2 判据向下解析一层常量）。
        # ⛔ 原 `PENDING` 理由「后端无模型/端点」随本线落地而过期 ⇒ 同批从 `PENDING` 删掉、
        # `catalog.available` 翻真（T3 要求 available ⇔ 有拦截位点）、生成物重渲（T4）。
        Site(
            INTERCEPT,
            "src/apps/business/api/tb_send_log_views.py::TaobaoOrderSendLogViewSet.get_permissions",
            "淘宝发货记录的两个动作：list 列表 + retry 重试发货"
            "（节点 code 在类体常量 `TB_SEND_LOG_PERMISSIONS` 里）",
        ),
    ),
    "order.platform_action_logs": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::orders-platform-action-log", "菜单条目声明 permissions"),
        # ⭐ 交易订单操作记录线（spec `2026-10-04-trade-action-log-design.md` §7-6）：
        # 拦截层**接上了**——本页唯一的读端点（`GET /api/audit/order-action-logs/`）
        # 在**类体**上声明 `permission_classes = [has_node("order.platform_action_logs")]`
        # （T2 判据的「类.类属性」锚点形态，见「执行期修订 6」）。
        # ⛔ 原 `PENDING` 理由「后端无模型/端点」随本线落地而过期 ⇒ 同批从 `PENDING` 删掉、
        # `catalog.available` 翻真（T3 要求 available ⇔ 有拦截位点）、生成物重渲（T4）。
        # ⚠️ 本点**仍是菜单** `orders-platform-action-log` 可见性的承载者（`EXPERIENCE` 保留）。
        Site(
            INTERCEPT,
            "src/apps/audit/api/views.py::OrderActionLogListView.permission_classes",
            '本页唯一的读端点 GET /api/audit/order-action-logs/：'
            'permission_classes 的 has_node("order.platform_action_logs")',
        ),
        # ⭐ 订单审计线（D-0360）：两个新只读端点**复用本节点**（计划 §1.5
        # 拍板①，零新增节点 ⇒ RBAC 计数网零改动）。锚点同「类.类属性」形态。
        Site(
            INTERCEPT,
            "src/apps/audit/api/timeline_views.py::OrderTimelineView.permission_classes",
            'GET /api/audit/order-timeline/：'
            'permission_classes 的 has_node("order.platform_action_logs")',
        ),
        Site(
            INTERCEPT,
            "src/apps/audit/api/timeline_views.py::JidanpuBreakerListView.permission_classes",
            'GET /api/audit/jidanpu-breakers/：'
            'permission_classes 的 has_node("order.platform_action_logs")',
        ),
    ),
    # ⭐ 接单销售客服可改线（计划 `docs/plans/2026-10-08-sales-agent-change.md` §5 T2）：
    # 拦截层**接上了**。业务方 拍板**简化口径**：不复刻对标那条全局设置项
    # （`SK43`），规则就一句「持本点的人才能改这一格」⇒ 原理由「它依赖我们不存在的全局设置项」
    # 随之过期，同批从 `PENDING` 删掉、`catalog.available` 翻真（T3 要求 available ⇔ 有拦截位点）、
    # 生成物 `docs/RBAC-WIRING-MAP.md` 重渲（T4 逐字比对）。
    # ⚠️ 本点**没有 `EXPERIENCE` 位点**：它不对应任何菜单 / 页面入口，只对应对标那一个
    # 编辑弹层里的控件（我们的落点是同一个 `partial_update` 动作体）。
    "order.allow_change_sales_agent": (
        # ⚠️ 锚点是 `OrderViewSet.partial_update`（**不是** `get_permissions`）：本 code 不在
        # `ORDER_PERMISSIONS` 那张逐 action 的类级表里，判据是**动作体**里按请求体字段判的
        # 条件闸——与 `order.submit_payout_price` 在 `_resolve_assign_taker` / `/clear-taker` 里
        # 那两道「带了价格才要节点」同形（T2 只扫锚点那个方法体内出现该 code 与授权判据）。
        Site(
            INTERCEPT,
            "src/apps/business/api/order_views.py::OrderViewSet.partial_update",
            "编辑订单（PATCH /api/business/orders/<单号>/）：按请求体是否带了**变化的** "
            "`sales_agent` 判——值与本单现值不同才要本节点，同值（前端整表单提交带上原值）不判",
        ),
    ),
    # ⭐ 上家订单改价记录线（spec
    # `docs/specs/2026-10-17-order-log-names-and-upstream-price-log-design.md` §3-D5）：
    # 拦截层**接上了**——本页唯一的读端点（`GET /api/business/upstream-price-change-logs/`）
    # 在**类体**上声明 `permission_classes = [has_node("order.upstream_price_change_logs")]`
    # （T2 判据的「类.类属性」锚点形态，见「执行期修订 6」）。
    # ⛔ 原 `PENDING` 理由「功能未实现：…后端无模型/端点」随本线落地而过期
    # ⇒ 同批从 `PENDING` 删掉、`catalog.available` 翻真（T3 要求 available ⇔ 有拦截位点）、
    # 生成物 `docs/RBAC-WIRING-MAP.md` 重渲（T4 逐字比对）。
    # ⚠️ 本点**仍是菜单** `orders-source-price-log` 可见性的承载者（`EXPERIENCE` 保留）。
    # ⚠️ 路径形状与 `order.platform_action_logs` 那一格同源（显式 `path()` + 独立视图，
    # ⛔ 不是 `OrderViewSet` 的 action）。
    "order.upstream_price_change_logs": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::orders-source-price-log", "菜单条目声明 permissions"),
        Site(
            INTERCEPT,
            "src/apps/business/api/upstream_price_log_views.py::UpstreamPriceChangeLogListView.permission_classes",
            "只读列表端点",
        ),
    ),
    "order.online_refund": (
        Site(
            INTERCEPT,
            "src/apps/business/api/refund_views.py::RefundView.get_permissions",
            '部分退款 POST 与退款记录 GET 同一个视图，闸门 = permission_classes 的 has_node("order.online_refund")；本方法给 403 可辨 msg',
        ),
        # 批量记退款是第二个消费方。位点表只核对已登记的 code，漏登记就在地图上消失。
        Site(
            INTERCEPT,
            "src/apps/business/api/order_views.py::OrderViewSet.get_permissions",
            "订单列表批量端点的 batch_refunds（批量记退款）也消费它"
            "（`ORDER_PERMISSIONS` 里那个字面量）",
        ),
    ),
    "system_user.list": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::system-users", "菜单条目声明 permissions"),
        Site(
            INTERCEPT,
            "src/apps/accounts/views.py::AccountViewSet.get_permissions",
            'list 动作 → ACCOUNT_PERMISSIONS["list"] = ("system_user.list",)',
        ),
    ),
    "system_user.add_edit": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::system-user-new", "菜单条目声明 permissions"),
        Site(
            INTERCEPT,
            "src/apps/accounts/views.py::AccountViewSet.get_permissions",
            'create / update / partial_update / retrieve / roles 动作（映射见 accounts/views.py 的 ACCOUNT_PERMISSIONS）',
        ),
    ),
    "system_user.change_status": (
        Site(
            INTERCEPT,
            "src/apps/accounts/views.py::AccountViewSet.get_permissions",
            'status 与 status_batch 两个动作共用该节点',
        ),
    ),
    "system_user.reset_password": (
        Site(
            INTERCEPT,
            "src/apps/accounts/views.py::AccountViewSet.get_permissions",
            'reset_password 动作',
        ),
    ),
    "system_user.login_restriction": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::system-login-restrict",
             "菜单条目声明 permissions；⚠️ 后端未实现（1:1 复刻页）⇒ available=False"),
    ),
    "system_user.blacklist": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::system-blacklist",
             "菜单条目声明 permissions；⚠️ 后端未实现（1:1 复刻页）⇒ available=False"),
    ),
    "system_user.view_order_stats": (
        Site(
            INTERCEPT,
            "src/apps/business/api/order_stats_views.py::OrderStatsView.get_permissions",
            '接单统计（GET /api/business/order-stats）：'
            'permission_classes 的 has_node(ORDER_STATS_NODE)，'
            'ORDER_STATS_NODE = "system_user.view_order_stats"',
        ),
    ),
    "system_user.realname_records": (
        # ⚠️ **体验层位点保留**：菜单条目仍然声明这个 code
        # （T1 的口径：`EXPERIENCE` 位点可与 `INTERCEPT` 并存，只是不能再留 `PENDING`）。
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::system-real-name",
             "菜单条目声明 permissions"),
        Site(
            INTERCEPT,
            "src/apps/accounts/views.py::AccountViewSet.get_permissions",
            "实名记录与配置页那三个端点："
            "`ACCOUNT_PERMISSIONS` 的 `realname_logs` / `realname_setting` / `realname_quota` "
            '三条都指向 `"system_user.realname_records"`，'
            "经 `get_permissions()` 的 `has_node(*codes)` 装配",
        ),
    ),
    "system_user.restrict_view_id_card": (
        Site(
            INTERCEPT,
            "src/apps/accounts/account_flags.py::id_card_visible",
            "flag 读取点：account_payload 的 id_card_no 脱敏；经 viewer= 在各读面上生效（清单见 payloads.py::account_payload）",
        ),
    ),
    "system_user.reset_realname": (
        Site(
            INTERCEPT,
            "src/apps/accounts/views.py::AccountViewSet.get_permissions",
            '重置某人实名（POST api/accounts/<pk>/reset-realname/）：'
            '`ACCOUNT_PERMISSIONS["reset_realname"] = ("system_user.reset_realname",)`，'
            "经 `get_permissions()` 的 `has_node(*codes)` 装配",
        ),
    ),
    "system_user.contract_management": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::system-contract",
             "菜单条目声明 permissions；⚠️ 后端未实现（1:1 复刻页）⇒ available=False"),
    ),
    "system_user.restrict_view_commission_params": (
        Site(
            INTERCEPT,
            "src/apps/accounts/account_flags.py::commission_params_visible",
            "flag 读取点：account_payload 的 base_salary / commission_mode / commission_value 置 None",
        ),
    ),
    "game.server": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::game-config-games", "菜单条目声明 permissions"),
        # 四个写动作都映射到本点；list / retrieve 不接。
        Site(
            INTERCEPT,
            "src/apps/business/api/config_views.py::GameServerViewSet.get_permissions",
            '区服维护写动作（POST / PUT / PATCH / DELETE /api/business/servers/）：'
            'GAME_SERVER_PERMISSIONS 的四个写动作都映射到 "game.server"',
        ),
        Site(
            INTERCEPT,
            "src/apps/business/api/game_library_views.py::GameLibraryView.get_permissions",
            "游戏管理新增弹窗读取平台游戏库（GET /api/business/game-library/）",
        ),
    ),
    "game.common_projects": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::game-config-projects", "菜单条目声明 permissions"),
        # 四个写动作都映射到本点。
        Site(
            INTERCEPT,
            "src/apps/business/api/config_views.py::ProjectViewSet.get_permissions",
            '常用代练项目写动作（POST / PUT / PATCH / DELETE /api/business/projects/）：'
            'PROJECT_PERMISSIONS 的四个写动作都映射到 "game.common_projects"',
        ),
    ),
    "game.order_type_categories": (
        # 分类页与常用项目页合并成一页（`/game-config/projects`，路由声明两个节点
        # 任一即可）⇒ 本点的体验位点随之改指同一路由。判据不放松：菜单那一行确实声明了本 code。
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::game-config-projects", "菜单条目声明 permissions"),
        # 四个写动作都映射到本点。
        Site(
            INTERCEPT,
            "src/apps/business/api/config_views.py::ProjectCategoryViewSet.get_permissions",
            '订单项目分类写动作（POST / PUT / PATCH / DELETE /api/business/categories/）：'
            'PROJECT_CATEGORY_PERMISSIONS 的四个写动作都映射到 "game.order_type_categories"',
        ),
    ),
    "finance.payroll": (
        Site(
            INTERCEPT,
            "src/apps/business/export_jobs.py::FinanceExportAccess.get_permissions",
            "结算后台导出的创建、进度与下载同时校验结算读取及导出权限",
        ),
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::agents-performance", "菜单条目声明 permissions"),
        Site(
            INTERCEPT,
            "src/apps/business/api/settlement_views.py::FinanceReadPermissionMixin.get_permissions",
            '四个读接口（结算列表 / 待归属 / 调整记录链 GET / 结算对象人员 GET）共用 permission_classes = [has_node(FINANCE_READ_NODE)]，FINANCE_READ_NODE = "finance.payroll"',
        ),
        # 检测重复收款支付宝与结算读接口同一批人，共用本节点。
        # 读接口的个数会变，注释里不写死。
        Site(
            INTERCEPT,
            "src/apps/business/api/performance_write_views.py::AlipayDuplicatesView.get_permissions",
            '支付宝查重（只读）：permission_classes 的 has_node(ALIPAY_DUPLICATES_NODE)，'
            'ALIPAY_DUPLICATES_NODE = "finance.payroll"',
        ),
        # ⭐ 图片存储线第五个消费方，而且**这一格才是 A8 的承重判据**：
        # 绩效列表 `/performances/` 的行可见性由 `order.list` 放宽（客服 / 客服主管持它）。
        # ⭐ **D-0219改口**：那个端点的门原先只有 `my_performance.export`
        # （代练员也持），现在改成任一即进（1163 ∪ `order.list`）⇒ 「持 `order.list`
        # 但不持 `finance.payroll`」的账号**现在直接进得了列表**（此前还要再持 1163），
        # 本字段自己的服务端判据（`WECHAT_QR_NODE`）从「兜底」变成**每天会真实踩到的门**：
        # 这类账号看得到行、但**读不到别人的收款码签名链接**。判据就地写在序列化器方法里
        # （不是视图的 `permission_classes`），所以 T2 的授权出口白名单要用到
        # `has_node_now(`（执行期修订 5 已收录）。
        Site(
            INTERCEPT,
            "src/apps/business/api/serializers.py::PerformanceListSerializer.get_wechat_qr_url",
            '绩效列表逐行的收款码短时效链接：方法体内 has_node_now(request.user, '
            'WECHAT_QR_NODE)，WECHAT_QR_NODE = "finance.payroll"（非管理员查库、按实例缓存）',
        ),
        # ⭐（清算绩效弹窗）第六个消费方：结算预览（**只读**）——
        # 「清算绩效」弹窗在点「确定结算或修改」之前拿它填那几格数字，与结算列表同一批
        # 受众、同一颗读节点（⛔ 不是 `finance.change_settlement_status`：它不改任何东西）。
        # ⚠️ 断言锚点是**本类自己声明**的 `get_permissions`（不是继承
        # `FinanceReadPermissionMixin` 的那一份）：T2 判据解析方法体，继承来的方法体
        # 在 AST 里不属于本类 ⇒ 位点会被判成假。
        Site(
            INTERCEPT,
            "src/apps/business/api/settlement_views.py::SettlementPreviewView.get_permissions",
            '结算预览（只读）：permission_classes 的 has_node(FINANCE_READ_NODE)，'
            'FINANCE_READ_NODE = "finance.payroll"',
        ),
    ),
    # ⭐ P4 线并入 main 时补：结账与人工修正**共用**该节点（spec §6「同结账，暂不单开」）。
    "finance.change_settlement_status": (
        Site(INTERCEPT, "src/apps/business/api/online_payroll_views.py::OnlinePayrollPreviewView.get_permissions", "在线结算预览"),
        Site(INTERCEPT, "src/apps/business/api/online_payroll_views.py::OnlinePayrollBatchCollectionView.get_permissions", "确认在线结算制单"),
        Site(INTERCEPT, "src/apps/business/api/online_payroll_views.py::OnlinePayrollPaymentView.get_permissions", "取回同一付款批次的付款码"),
        Site(INTERCEPT, "src/apps/business/api/online_payroll_views.py::OnlinePayrollCancelView.get_permissions", "取消尚未向渠道提交的付款单"),
        Site(
            INTERCEPT,
            "src/apps/business/api/settlement_views.py::SettleView.get_permissions",
            '结账：permission_classes 的 has_node("finance.change_settlement_status")',
        ),
        Site(
            INTERCEPT,
            "src/apps/business/api/adjustment_views.py::AdjustmentListView.get_permissions",
            '人工修正（第二消费方）：get_permissions 按 self.action 分派到同一节点',
        ),
        # ⭐ P4FU-B第三消费方：撤销结账。对标 `settBtn`「更改结算状态」
        # 是一颗按钮，D-0072 把它拆成「正向 = /settle」「反向 = /revoke」，
        # 权限点因此**共用**（新开一个点会要求权限树上维护两个永远同进同退的勾）。
        Site(
            INTERCEPT,
            "src/apps/business/api/performance_write_views.py::SettlementRevokeView.get_permissions",
            '撤销结账：permission_classes 的 has_node("finance.change_settlement_status")',
        ),
        # ⭐ 订单批量线 A10（spec §D6 / §D15）：**`finance.*` 的第一个跨 app 消费方**
        # ——订单列表批量端点的 `batch_recompute_commission`（重算客服销售提成）判本节点，
        # 由 `OrderViewSet.get_permissions` 的 `ORDER_PERMISSIONS["batch_recompute_commission"]`
        # 装配。业务方 裁定 Q1 = 候选 B（与结账 / 撤销结账 / 人工修正同一批人）。
        # ⚠️ 本串是生成物 `docs/RBAC-WIRING-MAP.md` 的**出处**：改这里必须同一次重跑
        # `manage.py render_rbac_wiring_map`（`tests/test_rbac_wiring.py` 的 T4 逐字比对）。
        Site(
            INTERCEPT,
            "src/apps/business/api/order_views.py::OrderViewSet.get_permissions",
            "订单列表批量端点的 batch_recompute_commission（重算客服销售提成）也消费它"
            "（`ORDER_PERMISSIONS` 里那个字面量）",
        ),
    ),
    # ⭐ P4FU-B翻真的两个点：端点落地后它们不再是「未派线」。
    # ⚠️ 翻 `catalog.available` 与登记本表是**两件事**（spec D5），两处都要做：
    # 只翻 available 不登记会在 T3 红；只登记不翻真会在 T3 红（已在位点却没翻真）。
    "finance.delete_performance": (
        Site(
            INTERCEPT,
            "src/apps/business/api/performance_write_views.py::PerformanceDeleteView.get_permissions",
            '删除记录：permission_classes 的 has_node("finance.delete_performance")',
        ),
    ),
    "finance.export_performance": (
        Site(
            INTERCEPT,
            "src/apps/business/export_jobs.py::FinanceExportAccess.get_permissions",
            "结算后台导出的创建、进度与下载同时校验结算读取及导出权限",
        ),
        Site(
            INTERCEPT,
            "src/apps/business/api/performance_write_views.py::PerformanceExportView.get_permissions",
            # 下面这句是生成物出处，改字面量要同一次重渲。
            '导出记录（CSV 文件流）：permission_classes 的 has_node("finance.export_performance")'
            '。⛔ 更正（2026-09-23，Task 3）：导出已是附件那种 .xlsx 文件流（原句保留）',
        ),
    ),
    "finance.support_statistics": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::agents-salesman-stats", "菜单条目声明 permissions"),
        # ⭐（D-0247）：本点翻真——两条读端点的类体判据（各自一个位点）。
        Site(
            INTERCEPT,
            "src/apps/business/api/report_views.py::SupportStatsView.get_permissions",
            '客服接发单统计：permission_classes 的 has_node("finance.support_statistics")',
        ),
        Site(
            INTERCEPT,
            "src/apps/business/api/report_views.py::BusinessReportView.get_permissions",
            '经营报表：permission_classes 的 has_node("finance.support_statistics")',
        ),
        # ⭐（APP 与小程序 → 数据概览）：**复用**本点的第三个读端点
        # （计划 `docs/plans/2026-10-23-app-mp-stats.md` §8 第一条：⛔ 不新增节点）。
        Site(
            INTERCEPT,
            "src/apps/business/api/app_stats_views.py::AppStatsOverviewView.get_permissions",
            "APP 与小程序数据概览：permission_classes 的 has_node(\"finance.support_statistics\")",
        ),
    ),
    "finance.settlement_records": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::agents-settlement", "菜单条目声明 permissions"),
    ),
    "finance.online_settlement_details": (
        Site(INTERCEPT, "src/apps/business/api/online_payroll_views.py::OnlinePayrollPreviewView.get_permissions", "预览前确认可进入在线结算明细"),
        Site(INTERCEPT, "src/apps/business/api/online_payroll_views.py::OnlinePayrollPaymentView.get_permissions", "查看付款码前确认明细权限"),
        Site(INTERCEPT, "src/apps/business/api/online_payroll_views.py::OnlinePayrollCancelView.get_permissions", "取消付款前确认明细权限"),
        Site(INTERCEPT, "src/apps/business/api/online_payroll_views.py::OnlinePayrollBatchCollectionView.get_permissions", "按游戏范围查看在线结算付款单"),
        Site(INTERCEPT, "src/apps/business/api/online_payroll_views.py::OnlinePayrollBatchDetailView.get_permissions", "按游戏范围查看付款明细"),
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::agents-online-pay-detail", "菜单条目声明 permissions"),
    ),
    "finance.platform_fund_details": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::agents-fund-detail", "菜单条目声明 permissions"),
    ),
    "finance.alipay_authorization": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::agents-alipay-auth", "菜单条目声明 permissions"),
        Site(INTERCEPT, "src/apps/business/api/alipay_authorization_views.py::AlipayAuthorizationCollectionView.permission_classes", "付款授权列表与创建"),
        Site(INTERCEPT, "src/apps/business/api/alipay_authorization_views.py::AlipayAuthorizationActionView.permission_classes", "付款授权与刷新"),
    ),
    "system.role_authority": (
        Site(INTERCEPT, "src/apps/rbac/views.py::RoleAuthorityViewSet.get_permissions", "ACTION_NODES 之外的动作回退到 DEFAULT_NODE"),
        Site(INTERCEPT, "src/apps/rbac/views.py::PermissionNodeViewSet.get_permissions", "权限树数据源，与编辑弹层同判据"),
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::system-role-authority", "菜单条目声明 permissions"),
    ),
    "system.position_groups": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::system-role-group", "菜单条目声明 permissions"),
    ),
    "system.auto_discern_settings": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::system-order-recognition", "菜单条目声明 permissions"),
        # ⭐（录单识别设置）：判据**早就在代码里**（两个视图把
        # `permission_classes` 写在**类体**上），只是当时功能未实现、目录标着
        # `available=False`、登记表也没有拦截位点。本线三处同批（位点 + `available`
        # + 删 `PENDING`）。⚠️ 锚点用**类体的权限装配**这一形态（T2 执行期修订 6，
        # 先例 `apps/audit/api/views.py::OperationLogListView.permission_classes`）：
        # 裸类名不是合法锚点（T2 只认 `类.方法` / `类.类属性` / 模块级函数）。
        Site(
            INTERCEPT,
            "src/apps/business/api/recognition_views.py::RecognitionSettingListView.permission_classes",
            '识别设置列表 GET /api/business/recognition-settings/：permission_classes = [has_node("system.auto_discern_settings")]',
        ),
    ),
    "system.edit_auto_discern_keywords": (
        Site(
            INTERCEPT,
            "src/apps/business/api/recognition_views.py::RecognitionSettingDetailView.permission_classes",
            '改一行关键词 PATCH /api/business/recognition-settings/<item_code>/：permission_classes = [has_node("system.edit_auto_discern_keywords")]',
        ),
    ),
    "system.settings": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::system-settings", "菜单条目声明 permissions"),
        # ⭐ 系统开关线（D-0223）：系统设置页接真的第一条端点——开关列表。
        # 判据在 `SystemSwitchListView` 的**类体** `permission_classes` 上（锚点口径同
        # recognition 那两条）；同批：`catalog.available` 翻真 + `PENDING` 摘除（T3）。
        Site(
            INTERCEPT,
            "src/apps/business/api/system_switch_views.py::SystemSwitchListView.permission_classes",
            '开关列表 GET /api/business/system-switches/：permission_classes = [has_node("system.settings")]',
        ),
    ),
    "system.edit_settings": (
        Site(
            INTERCEPT,
            "src/apps/business/api/system_switch_views.py::SystemSwitchDetailView.permission_classes",
            '改一个开关 PATCH /api/business/system-switches/<key>/：permission_classes = [has_node("system.edit_settings")]',
        ),
    ),
    # ⭐ **QQ 机器人线：本仓第三条自有节点**（`benchmark_id = None`）。
    # 拦截层**已接上**（该线全部 REST 端点）⇒ `catalog.available=True` 与它同向（T3）。
    # ⚠️ 锚点是**基类类体上的 `permission_classes`**（⛔ 不是 `get_permissions()`）：
    # `apps/qqbot/api/views.py` 的全部页面端点（binding / groups / push-config / test-push /
    # mutes / join-requests / join-policy / menu / share-link / messages）都**继承**
    # `QQBotSettingsGateView`、逐字复用这一套装配（照 `apps/openapi/api/app_views.py`
    # `ApiAppAdminView` 与它的子类同一手法）——位点一处、语义是「共用」，不是「只有一处生效」。
    # ⚠️ 本点今天**只有** `INTERCEPT` 位点、**没有** `EXPERIENCE` 位点：菜单条目在
    # 菜单侧接进 `config/menu.ts`（T2b 只对**声明了的**体验位点做对拍，不要求每个点都有）。
    # ⚠️ 存量租户拿到这个点靠数据迁移
    # `apps/rbac/migrations/0008_add_qqbot_settings_node.py`
    # （幂等、按 `code` 建、⛔ 不动角色授权）；**默认只有「系统管理员」职位**有它。
    "system.qqbot_settings": (
        Site(
            INTERCEPT,
            "src/apps/qqbot/api/views.py::QQBotSettingsGateView.permission_classes",
            'QQ 机器人设置页的全部端点（api/qqbot/ 前缀下 12 条路由）继承本基类、'
            '共用同一个类体装配：permission_classes = [has_node("system.qqbot_settings")]',
        ),
    ),
    # ⭐ 频道文章作品线：本仓第八条自有节点。
    # 四个端点类**全部端点（含读）**都挂本点 ⇒ 四条位点各登记一条（T2 按**各自的方法体**
    # 解析，⛔ 不能靠 `ArticleViewSet` 那一条代偿另外三个类）。
    # ⚠️ 四条位点锚在**同一个** `_ArticleNodeBase.get_permissions`：四个类确实继承它、
    # 判据真的在**那个方法体**里装配（`has_node(ARTICLE_NODE)`），逐条写出各自的消费面
    # 只是为了让地图读者知道「哪几个类靠它」——⛔ 别把四条读成四份独立判据。
    "system.article_manage": (
        # 体验位点：网页端菜单【APP与小程序】→「发布文章作品」
        # （`routeName: 'app-mp-articles'`，同一行声明
        # `permissions: ['system.article_manage']`；路由侧同批
        # `config/routePermissions.ts` 的 `'app-mp-articles'`）。
        Site(
            EXPERIENCE,
            "src/frontend/src/config/menu.ts::app-mp-articles",
            "菜单条目声明 permissions",
        ),
        Site(
            INTERCEPT,
            "src/apps/business/api/article_views.py::_ArticleNodeBase.get_permissions",
            "`ArticleViewSet` 的八个动作（list / retrieve / create / update / partial_update /"
            " destroy / publish / unpublish）：管理端文章集合全部端点含读都挂本点",
        ),
        Site(
            INTERCEPT,
            "src/apps/business/api/article_views.py::_ArticleNodeBase.get_permissions",
            "`ChannelProfileAdminView` 的 get + put（频道资料：工作室名 / 简介 / 文章数）",
        ),
        Site(
            INTERCEPT,
            "src/apps/business/api/article_views.py::_ArticleNodeBase.get_permissions",
            "`ArticleMediaTicketView` 的 post（签发文章图片的直传凭证）",
        ),
        Site(
            INTERCEPT,
            "src/apps/business/api/article_views.py::_ArticleNodeBase.get_permissions",
            "`ArticleMediaClaimView` 的 post（认领文章图片：归属 / 大小 / 魔数三道校验）",
        ),
    ),
    # ⭐ 企微自建应用线（D-0379）：本仓第十一条自有节点。
    # 体验位点：v2 侧栏【功能维护】→「企微自建应用」（`routeName: 'wecom-settings'`，
    # 同一行声明 `permissions: ['system.wecom_settings']`；v2 路由侧同批
    # `config/routePermissions.ts` 的 `'v2-wecom-settings'`；旧壳无此页、条目置灰）。
    # 拦截层：`apps/wecom/views.py` 的全部端点（配置 GET/PUT、自检、测试发送、
    # 通讯录与客户联系只读 proxy、台账读）**继承** `WecomSettingsGateView`、
    # 逐字复用类体那套装配（照 `QQBotSettingsGateView` 的同一手法）——
    # 位点一处、语义是「共用」；匿名回调 `/api/wecom/callback` **不挂**本点
    # （企微服务器是匿名调用方，门在配置行的 `enabled` + 验签上）。
    # ⚠️ 存量租户拿到这个点靠数据迁移
    # `apps/rbac/migrations/0017_add_wecom_settings_node.py`
    # （幂等、按 `code` 建、⛔ 不动角色授权）。
    "system.wecom_settings": (
        Site(
            EXPERIENCE,
            "src/frontend/src/config/menu.ts::wecom-settings",
            "菜单条目声明 permissions",
        ),
        Site(
            INTERCEPT,
            "src/apps/wecom/views.py::WecomSettingsGateView.permission_classes",
            '企微自建应用页的全部登录端点（api/wecom/ 前缀下 9 条路由）继承本基类、'
            '共用同一个类体装配：permission_classes = [has_node("system.wecom_settings")]',
        ),
    ),
    "system.role_authority_edit": (
        Site(INTERCEPT, "src/apps/rbac/views.py::RoleAuthorityViewSet.get_permissions", "create / update / partial_update / copy 动作"),
    ),
    "system.role_authority_delete": (
        Site(INTERCEPT, "src/apps/rbac/views.py::RoleAuthorityViewSet.get_permissions", "destroy 动作"),
    ),
    "system.publish_notice": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::system-notices", "菜单条目声明 permissions"),
        # ⭐ 系统内部公告实装：写动作（POST/PATCH/DELETE）在
        # `SystemNoticeViewSet.get_permissions` 里逐 action 装配本节点；
        # ⛔ 读列表**不挂**本节点（主页「通知和公告」卡片要对所有登录用户可见）。
        Site(
            INTERCEPT,
            "src/apps/business/api/notice_views.py::SystemNoticeViewSet.get_permissions",
            "写动作（发布 / 编辑 / 删除）逐 action 挂 system.publish_notice；读列表只要求登录",
        ),
    ),
    "system.logs": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::system-operation-log", "菜单条目声明 permissions"),
        # 两个视图把 permission_classes 写在类体上，锚点是类属性，不是 get_permissions。
        Site(
            INTERCEPT,
            "src/apps/audit/api/views.py::OperationLogListView.permission_classes",
            '系统日志列表 GET /api/audit/logs/：permission_classes = [has_node("system.logs")]',
        ),
        Site(
            INTERCEPT,
            "src/apps/audit/api/views.py::OperationLogActorListView.permission_classes",
            '操作人下拉 GET /api/audit/actors/（与日志端点同一节点）：permission_classes = [has_node("system.logs")]',
        ),
    ),
    "system.sources": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::system-order-sources", "菜单条目声明 permissions"),
    ),
    "system.role_authority_view": (
        Site(INTERCEPT, "src/apps/rbac/views.py::RoleAuthorityViewSet.get_permissions", "permissions 动作（权限清单）"),
    ),
    "system.platform_integration_settings": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::system-platform-integration", "菜单条目声明 permissions"),
        # 两个视图都在类体上声明 permission_classes，参数是模块常量。
        Site(INTERCEPT, "src/apps/business/api/platform_integration_views.py::PlatformIntegrationViewSet.permission_classes",
             "账号列表 / 详情 / 新增 / 改 / 删（5 个动作共用本节点）"),
        Site(INTERCEPT, "src/apps/business/api/platform_integration_views.py::PlatformIntegrationSettingsView.permission_classes",
             "全局设置 GET / PUT（与账号集合同一个页面节点）"),
    ),
    "system.pinduoduo_settings": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::system-pdd-ship", "菜单条目声明 permissions"),
        # 拦截位点在开放平台应用管理（类体常量）。租户级总览视图已不存在。
        Site(
            INTERCEPT,
            "src/apps/openapi/api/app_views.py::ApiAppAdminView.permission_classes",
            '开放平台「应用管理」（内部端点，`apps/openapi/urls.py` 的四条路由：'
            '`GET/POST /api/open/apps/`、`PATCH /api/open/apps/<int:pk>/`、'
            '`PUT /api/open/apps/<int:pk>/permissions/`、`PUT /api/open/apps/<int:pk>/games/`'
            '）共用同一个类体装配：permission_classes = [has_node(*APP_MANAGE_NODES)]，'
            'APP_MANAGE_NODES = ("system.pinduoduo_settings",)'
            '（`apps/openapi/api/app_views.py` 的模块常量；2026-10-08 改准：'
            '原 Task 12B 位点随租户级只读总览一并拆除）',
        ),
        # 第二个拦截位点：店铺对接七个动作与前端门同一个点。
        Site(
            INTERCEPT,
            "src/apps/business/api/shop_binding_views.py::ShopBindingViewSet.get_permissions",
            '店铺对接 `/api/business/shop-bindings/`（list / retrieve / create / update / '
            'partial_update / destroy / platform_config 七个动作）→ '
            'SHOP_BINDING_PERMISSIONS 全部动作 = ("system.pinduoduo_settings",)'
            '（2026-10-18 E-0253：原先只吃默认 IsAuthenticated，与前端门同点收口）',
        ),
    ),
    # 本仓自有节点（没有对标 id）。锚点必须是类体上的 permission_classes：
    # 位点解析只做一层模块常量、且只收字符串字面量。别把元组拆成两个常量再拼。
    # 菜单和路由的可见性与拦截层并存。
    # ⚠️ 存量租户拿到这个点靠数据迁移 `apps/rbac/migrations/0006_add_taker_open_api_node.py`
    # （幂等、按 `code` 建、⛔ 不动角色授权）；**默认只有「系统管理员」职位**有它
    # （`seed_rbac` 给该角色全目录）⇒ 要放给打手得老板自己在角色编辑页勾。
    "system.allow_taker_open_api": (
        Site(
            INTERCEPT,
            "src/apps/takerapi/api/app_views.py::TakerAppAdminView.permission_classes",
            '打手对接（**内部**端点，`apps/takerapi/urls.py` 的四条路由：'
            '`GET/POST /api/taker/apps/`、`PATCH /api/taker/apps/<int:pk>/`、'
            '`POST /api/taker/apps/<int:pk>/rotate-secret/`——⛔ **没有 `DELETE`**，'
            '「只能停用不能删」）共用同一个类体装配：'
            'permission_classes = [has_node(*APP_MANAGE_NODES)]，'
            'APP_MANAGE_NODES = ("system.pinduoduo_settings", "system.allow_taker_open_api")'
            '（`apps/takerapi/api/app_views.py` 的模块常量，两个 code 都是**字面量**；'
            '⛔ 本点与上一条**共用**同一个 `permission_classes` ⇒ 位点相同、语义不同：'
            '上一条是「系统管理员能管」，本条是「打手本人能管自己的」）',
        ),
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::taker-apps", "菜单条目声明 permissions（路由 meta.permissions 同源）"),
    ),
    # ⭐（`feat/upstream-downstream-sync` 文档波，**裁决 R22**）：拦截层**已接上**——
    # 两个页面端点现在各自在类体上声明 `permission_classes = [has_node(PEER_BINDING_PAGE_NODE)]`
    # （`GET` / `POST /api/business/peer-bindings/`、`DELETE /api/business/peer-bindings/<pk>/`）。
    # ⛔ 原 `PENDING` 理由「两个视图都吃默认权限类、**没有本点判据**」当场过期
        # ⚠️ 本点**同时**是菜单 `system-source-system` 与路由 `meta.permissions`
    # （`config/routePermissions.ts`）的可见性承载者——拦截层与体验层**并存**，不是替代。
    # ⚠️ 没有本节点的角色读不到绑定列表、更删不动绑定（删绑定会连带停掉这个租户的进单）。
    "system.bind_upstream_system": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::system-source-system", "菜单条目声明 permissions（路由 meta.permissions 同源）"),
        Site(INTERCEPT, "src/apps/business/api/peer_views.py::PeerBindingListView.permission_classes", "页面读 / 写端点（列表 + 新建）的节点门（裁决 R22）"),
        Site(INTERCEPT, "src/apps/business/api/peer_views.py::PeerBindingDetailView.permission_classes", "页面删除端点的节点门（裁决 R22）"),
        # ⭐ 绑定上家系统设置页收口线：本页改走一条**统一列表**（团团通 + 鸡蛋铺
        # 两类行混排），并新增一条鸡蛋铺绑定的**真删**端点 ⇒ 两条新端点各自挂同一个节点门。
        # ⚠️ 删一条鸡蛋铺绑定会级联删掉它名下的同步链路（`on_delete=CASCADE`）⇒ 与上面
        # `PeerBindingDetailView` 同一档：没有本节点的人一律不许删。
        # ⭐（E-0255 / D-0256）：绑定改**多行**资源后，页面新增 / 编辑 / 回填
        # 走**行级**端点（`POST` 集合 + `GET` / `PUT` 明细；明细类里还有既有 DELETE）
        # ⇒ 行级两个类同挂本节点。
        Site(INTERCEPT, "src/apps/business/api/upstream_binding_views.py::UpstreamBindingListView.permission_classes", "统一绑定列表 `GET /api/business/upstream-bindings/` 的节点门（页面读）"),
        Site(INTERCEPT, "src/apps/business/api/upstream_binding_views.py::JidanpuUpstreamBindingCreateView.permission_classes", "鸡蛋铺绑定行级创建 `POST /api/business/upstream-bindings/jidanpu/` 的节点门（页面新增，D-0256）"),
        Site(INTERCEPT, "src/apps/business/api/upstream_binding_views.py::JidanpuUpstreamBindingDetailView.permission_classes", "鸡蛋铺绑定行级读/编辑/删除 `GET|PUT|DELETE /api/business/upstream-bindings/jidanpu/<pk>/` 的节点门（页面编辑/删）"),
    ),
    # ⭐ 交易平台自动回复线（T13）：拦截层**已接上**——规则 CRUD 的整个集合
    # （列表 / 新增 / 单行 / 修改 / 删除）在类体上声明
    # `permission_classes = [IsAuthenticated, has_node(PLATFORM_AUTO_REPLY_NODE)]`
    # （`PLATFORM_AUTO_REPLY_NODE = "system.platform_auto_reply"`）。
    # ⛔ 原 `PENDING` 理由「功能未实现：前端 1:1 渲染页…后端无模型/端点」当场过期
        # ⚠️ `EXPERIENCE` 位点**保留**：本点同时是菜单 / 路由 `meta.permissions` 的可见性承载者。
    "system.platform_auto_reply": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::system-platform-auto-reply", "菜单条目声明 permissions"),
        Site(INTERCEPT, "src/apps/business/api/platform_auto_reply_views.py::PlatformAutoReplyRuleViewSet.permission_classes",
             "规则列表 / 新增 / 单行 / 修改 / 删除，全部挂 system.platform_auto_reply"),
    ),
    "system.platform_push_order_config": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::system-platform-push-order", "菜单条目声明 permissions"),
    ),
    "system.order_push_group_settings": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::system-wechat-group-push", "菜单条目声明 permissions"),
        # ⭐ 自动发消息到微信群设置实装：整个 ViewSet（含 `<pk>/test/`）
        # 共用本节点，逐 action 装在 `OrderPushSettingViewSet.get_permissions`。
        Site(
            INTERCEPT,
            "src/apps/business/api/order_push_views.py::OrderPushSettingViewSet.get_permissions",
            "规则列表 / 新增 / 编辑 / 删除 / 发送测试消息，全部挂 system.order_push_group_settings",
        ),
    ),
    "system.auto_price_adjust_config": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::system-auto-price-adjust", "菜单条目声明 permissions"),
    ),
    "customer.blacklist": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::customers-blacklist", "菜单条目声明 permissions"),
    ),
    # ⭐ 客户线翻真（2026-11-11）：四个客户端点挂 member_list（∨ 自家点 ∨ 限制点任一），
    # analysis 端点同批补上自家门 member_analysis_report（只持它的职位页面可进、接口放行）；
    # 节点 code 走模块常量（T2 向下解一层）。
    "customer.member_list": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::customers", "菜单条目声明 permissions"),
        Site(INTERCEPT, "src/apps/business/api/customer_views.py::CustomerMembersView.get_permissions", "members 端点（限制档行集与 total 强制 scoped）"),
        Site(INTERCEPT, "src/apps/business/api/customer_views.py::CustomerMemberFiltersView.get_permissions", "member-filters 端点（限制档录单人下拉只回自己）"),
        Site(INTERCEPT, "src/apps/business/api/customer_views.py::CustomerMemberOrdersView.get_permissions", "member-orders 端点（限制档抽屉同范围）"),
        Site(INTERCEPT, "src/apps/business/api/customer_views.py::CustomerAnalysisView.get_permissions", "analysis 端点（三节点任一；聚合不按录单人收口，拍板口径）"),
    ),
    "customer.member_analysis_report": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::customers-analysis", "菜单条目声明 permissions"),
        Site(INTERCEPT, "src/apps/business/api/customer_views.py::CustomerAnalysisView.get_permissions", "analysis 端点（本点的自家门；成员/筛选/订单抽屉不认它）"),
    ),
    # ⭐ 第五批（2026-11-11 业务方拍板）：自有**限制**节点——四个客户端点的守卫含本点任一，
    # 限制档的行集 / 抽屉 / total / 录单人下拉由服务端收口（`customer_views._restricted_creator`）。
    # 节点 code 走模块常量 `CUSTOMER_SELF_ONLY_NODE`（T2 向下解一层）。
    "customer.member_list_self_only": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::customers", "菜单条目声明 permissions（与 customer.member_list 任一即见）"),
        Site(INTERCEPT, "src/apps/business/api/customer_views.py::CustomerMembersView.get_permissions", "members 端点（限制档行集与 total 强制 scoped）"),
        Site(INTERCEPT, "src/apps/business/api/customer_views.py::CustomerMemberFiltersView.get_permissions", "member-filters 端点（限制档录单人下拉只回自己）"),
        Site(INTERCEPT, "src/apps/business/api/customer_views.py::CustomerMemberOrdersView.get_permissions", "member-orders 端点（限制档抽屉同范围）"),
        Site(INTERCEPT, "src/apps/business/api/customer_views.py::CustomerAnalysisView.get_permissions", "analysis 端点（过守卫；聚合不按录单人收口，拍板口径）"),
    ),
    # （2026-11-10 第二批：`customer.member_level_settings` / `customer.member_balance_details`
    # 两个节点的 EXPERIENCE 位点已随对应菜单条目与复刻页整页删除一并移除——
    # T2b 要求位点指向的菜单条目必须存在；两节点仍只有下面的 PENDING 登记。）
    # 订单动作与 flag 读取点。锚点指向授权判据所在的函数。
    "order.view_detail": (
        Site(INTERCEPT, "src/apps/business/api/order_views.py::OrderViewSet.get_permissions", "retrieve 动作（详情）；audit 也挂它"),
    ),
    "order.view_content": (
        Site(INTERCEPT, "src/apps/business/api/order_views.py::OrderViewSet.get_permissions", "retrieve 动作（与 view_detail 同为一个 any 元组）；audit 自 M-1 起也在同一个 any 元组里"),
    ),
    # 订单详情页复刻线：对标 27 原本登记为「功能未实现」，本线把对标那个
    # 「添加」按钮的端点 1:1 落地（`POST /orders/<单号>/notes/`，对标 `POST /OrderAjax/SaveOrderPublieMark`）
    # ⇒ 位点成立、`catalog.available` 同步翻真、从 `PENDING` 移出。默认角色不受影响：
    # `seed.BUILTIN_ROLE_MODULES` 按 `order` 栏目批发，客服 / 客服主管 / 系统管理员本来就有 27。
    "order.add_log": (
        Site(INTERCEPT, "src/apps/business/api/order_views.py::OrderViewSet.get_permissions", "notes 动作（`POST /orders/<单号>/notes/`，对标 `/OrderAjax/SaveOrderPublieMark` {orderId, remark}）"),
    ),
    # ⭐ 订单导出线：`order.export`（31，导出订单）翻真——导出端点
    # `GET /api/business/orders/export` 落地，闸门在 `OrderExportView.get_permissions`
    # 的方法体里 `has_node("order.export")()`（与 `PerformanceExportView.get_permissions`
    # 同形）⇒ 位点成立，从 `PENDING` 移出（原先那句「功能未实现」当场过期）。
    # ⛔ 改完**必须重渲** `docs/RBAC-WIRING-MAP.md`（T4 逐字比对）。
    "order.export": (
        Site(
            INTERCEPT,
            "src/apps/business/export_jobs.py::FinanceExportAccess.get_permissions",
            "订单后台导出的创建、进度与下载沿用订单导出权限",
        ),
        Site(
            INTERCEPT,
            "src/apps/business/api/order_export_views.py::OrderExportView.get_permissions",
            "导出订单端点（`GET /api/business/orders/export`）的权限闸；"
            "action = `order.export`",
        ),
    ),
    "order.change_status": (
        Site(INTERCEPT, "src/apps/business/api/order_views.py::OrderViewSet.get_permissions", "transition + batch_transition 动作的**默认** code（target ∉ {已完成, 待抢单}；目标是这两档时分别改判 17 / 36，见下一项与 `order.send_to_grab_hall`）"),
    ),
    "order.change_status_completed": (
        Site(INTERCEPT, "src/apps/business/api/performance_views.py::PerformanceViewSet.get_permissions", "review 动作（绩效审核，**不再是**订单完成的唯一入口）"),
        # ⭐ D-0160 批量线 A11：`batch_transition` 也消费本节点（写形态 target=已完成，
        # 以及**读取形态**——target 缺失时取它与 change_status 的 any 元组）。
        Site(INTERCEPT, "src/apps/business/api/order_views.py::OrderViewSet.get_permissions", "transition / batch_transition 且 target=已完成，或 batch_transition 的读取形态（D-0071 放开；取值在 TRANSITION_TARGET_PERMISSIONS）"),
    ),
    "order.assign_taker": (
        # ⚠️ D-0146 给本节点加了**第二个消费方**（`assignee_options`，指派弹层的候选名单端点）
        # ⇒ 说明文字连同改准；⛔ 改完**必须重渲** `docs/RBAC-WIRING-MAP.md`。
        # ⭐ D-0160 批量线 A11：第三个消费方 `batch_assign`（批量指派打手）同 code ⇒ 一并点名。
        Site(INTERCEPT, "src/apps/business/api/order_views.py::OrderViewSet.get_permissions", "assign + batch_assign + assignee_options 三个动作"),
    ),
    "order.clear_taker": (
        Site(INTERCEPT, "src/apps/business/api/order_views.py::OrderViewSet.get_permissions", "clear_taker + assignee_options 两个动作"),
        # ⭐ D-0147（批 E 任务 3）：绩效垃圾桶（`POST /performances/<id>/delete/`）的门是
        # 「删金额记录 + 清接手人」的并集 ⇒ 本节点多一个消费方（在另一个 ViewSet 上）。
        Site(INTERCEPT, "src/apps/business/api/performance_views.py::PerformanceViewSet.get_permissions", "垃圾桶（`POST /performances/<id>/delete/`：软删绩效 + 同一步清接手人）"),
    ),
    "order.set_offer_price": (
        # ⭐ 本批新增的消费方：`/hall-price/`（改发单价）。
        # ⭐ D-0160 批量线 A11：`batch_hall_price`（批量改发单价）同 code ⇒ 一并点名。
        # ⛔ **（计划 ④）`throw_to_hall` 位点已删**：抛大厅改判独占 36，
        # 本节点不再参与「能不能抛」（它只管「改发单价」这一件事）。
        Site(INTERCEPT, "src/apps/business/api/order_views.py::OrderViewSet.get_permissions", "hall_price + batch_hall_price 两个动作（改发单价）"),
    ),
    # ⭐ 本批：`order.change_payment_status`（22）翻真——PENDING 里那句
    # 「功能未实现（无对应端点/页面）」随 `/payment-status/` 端点落地而作废。
    # 位点锚点是 `get_permissions`（T2 只扫位点方法体内出现的 `ORDER_PERMISSIONS` 取值）。
    "order.change_payment_status": (
        # ⭐ D-0160 批量线 A11：`batch_payment_status`（批量改收款状态）同 code ⇒ 一并点名。
        # ⭐（订单管理页「同步收款」线）：`xgj_receipt_sync`（按时间范围逐笔查上游、
        # 交易成功的未收款单改已结款）**复用**本节点 ⇒ 第三个消费方，一并点名。
        Site(INTERCEPT, "src/apps/business/api/order_views.py::OrderViewSet.get_permissions", "payment_status + batch_payment_status + xgj_receipt_sync 三个动作（改收款状态）"),
    ),
    "order.send_to_grab_hall": (
        Site(INTERCEPT, "src/apps/business/api/order_views.py::OrderViewSet.get_permissions", "throw_to_hall + transition / batch_transition 且目标为待抢单"),
        Site(INTERCEPT, "src/apps/business/order_intake.py::create_order", "录单即抛时在建单前校验大厅发单权限"),
        Site(INTERCEPT, "src/apps/business/selectors.py::default_assignee_payload", "录单时按当前权限显示已记住的大厅选项"),
        Site(INTERCEPT, "src/apps/business/dailiantong_push.py::push_order", "发代练通或重发时决定能否同发大厅"),
    ),
    "order.withdraw_from_grab_hall": (
        Site(INTERCEPT, "src/apps/business/api/order_views.py::OrderViewSet.get_permissions", "withdraw 动作"),
    ),
    "order.submit_edit": (
        # ⭐ D-0160 批量线 A11：`batch_types`（批量改类型）的 any 元组里有本 code ⇒ 一并点名。
        Site(INTERCEPT, "src/apps/business/api/order_views.py::OrderViewSet.get_permissions", "partial_update / update / batch_types 动作（编辑提交 / 改类型）"),
    ),
    "order.submit_payout_price": (
        # ⭐ D-0160 批量线 A11：`batch_price`（批量改接单价）与 `batch_assign`（批量指派打手）
        # 都消费本 code（两者都是 any 元组，与各自单笔端点逐字相同）⇒ 一并点名。
        # ⛔ **（计划 ④）描述已去掉 `throw_to_hall` 这个**动作**：抛大厅已改判独占
        # 36，本节点不再参与「能不能抛」（但仍管抛单**带价**时那一层 403 —— 见下一条
        # `Site` 末尾的「body 层价格判据」）。
        Site(INTERCEPT, "src/apps/business/api/order_views.py::OrderViewSet.get_permissions", "price / assign / assignee_options / batch_price / batch_assign 动作 + throw_to_hall 的 body 层价格判据"),
        # ⭐ D-0147（批 E）：绩效侧三个动作也判本节点——改接单价（`/adjust/`）、
        # 反推发单价（`/adjust-amount/`）与本批的垃圾桶（`/delete/`，与 `order.clear_taker` 取并集）。
        Site(INTERCEPT, "src/apps/business/api/performance_views.py::PerformanceViewSet.get_permissions", "adjust / adjust_amount / soft_delete（URL 为 `/delete/`）三个动作"),
    ),
    # 对标 105 已改造成 `order.submit_work`（提交完单和问题单），不是语义近似。
    "order.submit_work": (
        # ⭐ D-0168：本点**现在管两个动作**——`submit`（交单，目标写死
        # `PENDING_REVIEW`）与新增的 `report_issue`（报问题单，目标写死 `ISSUE`、
        # 来源态写死 `IN_PROGRESS`）。业务方裁定「复用同一个点，名字改成
        # 『提交完单和提交问题单』」⇒ 描述必须把两个动作都点名，否则这张自动生成的
        # map 会漏掉一个真实消费方（下一个读它的人会以为只有 `/submit/`）。
        Site(INTERCEPT, "src/apps/business/api/order_views.py::OrderViewSet.get_permissions", "submit + report_issue 两个动作（105 号点已**重指**为「提交完单和提交问题单」，不再是语义近似）"),
    ),
    "order.edit_payout_price": (
        # ⭐ D-0160 批量线 A11：`batch_price` 的 any 元组里有本 code ⇒ 一并点名。
        Site(INTERCEPT, "src/apps/business/api/order_views.py::OrderViewSet.get_permissions", "price + batch_price 两个动作（对标的「在订单管理列表上修改发单价」）"),
    ),
    "grab_hall.take_order": (
        Site(INTERCEPT, "src/apps/business/api/order_views.py::OrderViewSet.get_permissions", "claim + hall 两个动作"),
    ),
    "my_orders.send_to_my_system": (
        # ⭐ booster-h5：打手受限详情**复用同一颗节点**（URL
        # `GET /my-orders/<order_no>/detail/`；动作名 `order_detail`——`detail` 是
        # DRF ViewSet 的保留 initkwarg，见视图里的实测注释）。
        # ⭐ D-0365：批量申请提前结算（`settle_requests`）**复用同一颗节点**
        # （动作清单句同批补；⛔ 不新增 RBAC 节点，D-0128 冻结）。
        Site(INTERCEPT, "src/apps/business/api/my_orders_views.py::MyOrdersViewSet.get_permissions", "list + order_detail（打手受限详情）+ sync_screenshots（App 按需同步群二维码）+ settle_requests（批量申请提前结算）四个动作"),
        # ⭐ D-0365：「我的订单收入」导出（`GET /my-orders/export`）是独立
        # APIView（照 `PerformanceExportView` 形状）⇒ 位点单独登记（与 ViewSet 那条是
        # **两个**装配点）；同一颗节点，⛔ 不新增 RBAC 节点。
        Site(INTERCEPT, "src/apps/business/api/my_orders_views.py::MyOrdersExportView.get_permissions", "导出端点（收入记录 xlsx；与列表同一颗入口门）"),
        # ⭐ 手动推送：工具栏那两颗按钮的端点（`member/jidanpu-push-now/`）
        # 吃的也是这颗节点 ⇒ ⛔ 不新增 RBAC 节点（D-0128 冻结），位点同批登记。
        Site(INTERCEPT, "src/apps/business/api/jidanpu_member_views.py::JidanpuMemberPushNowView.permission_classes", "手动推送端点（勾选的行录单 / 推状态）"),
    ),
    "my_performance.export": (
        # ⭐ D-0219 改准：本点从「列表的**唯一**门」降为「**自助查看**门」
        # （代练员 / 下家形态：只看自己的行）——列表端点现在任一即进（本点 ∪
        # `order.list`，见 `PERFORMANCE_PERMISSIONS["list"]`）。位点不变：它仍是
        # 那张 any 表里的一把钥匙。
        Site(INTERCEPT, "src/apps/business/api/performance_views.py::PerformanceViewSet.get_permissions", "list 动作（我的效绩页主接口；D-0219 起为任一即进的其中一把钥匙，另一把 = order.list）"),
    ),
    "my_profile.edit": (
        # ⭐ 我的资料线翻真：自助改资料端点上线（spec §5.2）。
        Site(INTERCEPT, "src/apps/core/profile_views.py::MyProfileView.get_permissions",
             "PATCH 动作"),
    ),
    "home.change_password": (
        # ⭐ 我的资料线翻真：自助改密端点上线（spec §5.3）。
        Site(INTERCEPT, "src/apps/core/auth_views.py::ChangePasswordView.get_permissions",
             "POST 改密"),
    ),
    # ⭐ 问题反馈线（spec `2026-10-08-feedback-design.md` §5）：对标 112
    # 「问题反馈及建议」原先是「只有对标 URL、服务端零判据」的待定点，本线把它接上。
    # 拦截位点是**一个共有基类的 `get_permissions()`**：三个端点（签发上传凭证 /
    # 列表 / 提交）都吃同一个节点 `home.feedback`（对标那个点的备注就是
    # 「建议全员开放权限」）⇒ 只登记这一个锚点，方法清单写在 note 里
    # （同型先例：`order.view_screenshot` 与 `order.upload_screenshot` 共用
    # `ScreenshotCollectionView.get_permissions` 那一条的说明写法）。
    # ⚠️ 第四个端点（`PATCH feedback/<pk>/`）的门是 `is_tenant_admin`、**不是本节点**
    # ⇒ ⛔ 它**不登记**到这里（登记了就是一句假陈述）。
    # ⚠️ 本点**同时**是侧栏底部条目 `feedback`（`config/menu.ts` 的 `BOTTOM_ITEMS`）
    # 与路由 `meta.permissions` 的可见性承载者 ⇒ 体验层与拦截层**并存**。
    "home.feedback": (
        Site(
            INTERCEPT,
            "src/apps/business/api/feedback_views.py::FeedbackNodeGatedView.get_permissions",
            'GET 列表 + POST 提交（`FeedbackCollectionView`）与 POST 签发票据'
            '（`FeedbackTicketView`）三个动作共用本基类的节点门；'
            '节点 code 在模块常量 `FEEDBACK_NODE = "home.feedback"` 里',
        ),
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::feedback",
             "菜单条目声明 permissions（侧栏底部条目）"),
    ),
    # ⭐ 首页二维码横幅线（D-0262）：**本仓第三条自有节点**（`benchmark_id = None`，
    # 登记表见 `apps/rbac/catalog.py` 的模块 docstring）。横幅是首页的自造区、没有独立菜单条目
    # ⇒ 本点**没有 `EXPERIENCE` 位点**（首页本身的可见性归 `home.index`，不归本点）。
    # 拦截层**随本线接上**：写动作（POST / PUT / PATCH / DELETE）在
    # `HomeQrCodeViewSet.get_permissions` 里逐 action 装配本节点；
    # ⛔ 读列表**不挂**本节点（首页人人要渲染横幅）——它按「持不持本点」分档：
    # 持点返回全部行（含停用，管理面要用）、不持只返回启用的行。
    # ⚠️ 存量租户拿到这个点靠数据迁移 `apps/rbac/migrations/0008_add_home_qr_manage_node.py`
    # （幂等、按 `code` 建、⛔ 不动角色授权）；新建租户的内置「人事」经 seed 的 `home`
    # 栏目批发自动含它（非 flag 子节点）。
    "home.qr_manage": (
        Site(
            INTERCEPT,
            "src/apps/business/api/qr_code_views.py::HomeQrCodeViewSet.get_permissions",
            "写动作（新增 / 更换图片 / 启停 / 删除 / 排序）逐 action 挂 home.qr_manage；"
            "读列表只要求登录、按持点分档返回（含停用与否）",
        ),
        # 直传票据端点（`POST /api/business/home/qr-codes/media/ticket/`）与写动作**同一颗节点**
        # ——能配横幅的人才需要签发票据；它自己声明 `get_permissions`（T2 按**本类方法体**解析，
        # ⛔ 不能靠 ViewSet 那条位点代偿）。
        Site(
            INTERCEPT,
            "src/apps/business/api/qr_code_views.py::HomeQrTicketView.get_permissions",
            "签发直传凭证（POST …/media/ticket/）的方法体节点门",
        ),
    ),
    "order.restrict_to_self_created": (
        Site(INTERCEPT, "src/apps/business/order_flags.py::scope_by_creator", "flag 读取点：列表按 created_by 收窄（仅 list）"),
    ),
    "order.restrict_view_taker_price": (
        Site(INTERCEPT, "src/apps/business/order_flags.py::can_view_taker_price", "flag 读取点：接单价字段脱敏"),
    ),
    "order.restrict_view_offer_price": (
        Site(INTERCEPT, "src/apps/business/order_flags.py::can_view_offer_price", "flag 读取点：抛单价字段脱敏"),
    ),
    "order.restrict_edit_taker_price": (
        Site(INTERCEPT, "src/apps/business/order_flags.py::can_edit_taker_price", "flag 读取点：/adjust 忽略接单价键"),
    ),
    "order.restrict_view_taker": (
        Site(INTERCEPT, "src/apps/business/order_flags.py::can_view_taker", "flag 读取点：列表载荷 taker_name 回常量「无权查看」（spec §5.3）"),
    ),
    "order.restrict_view_total_stats": (
        Site(INTERCEPT, "src/apps/business/order_flags.py::can_view_total_stats", "flag 读取点：列表头部统计块 summary 回 null（spec §7.1）"),
    ),
    "game.add_edit": (
        Site(
            INTERCEPT,
            "src/apps/business/api/config_views.py::GameViewSet.get_permissions",
            'create / update / partial_update 动作 → GAME_PERMISSIONS["create"/"update"/"partial_update"]'
            ' = ("game.add_edit",)（list / retrieve 不在表里、行为不变）',
        ),
    ),
    "game.delete": (
        Site(
            INTERCEPT,
            "src/apps/business/api/config_views.py::GameViewSet.get_permissions",
            'destroy 动作 → GAME_PERMISSIONS["destroy"] = ("game.delete",)',
        ),
    ),
    "system.add_edit_source": (
        Site(
            INTERCEPT,
            "src/apps/business/api/source_views.py::OrderSourceViewSet.get_permissions",
            'create / update / partial_update 动作 → ORDER_SOURCE_PERMISSIONS["create"/"update"/"partial_update"]'
            ' = ("system.add_edit_source",)（list / retrieve 不在表里、行为不变）',
        ),
        # ⭐ 公司店铺分组（计划 `docs/plans/2026-10-26-order-source-groups.md`
        # §3.1 拍板④）：**复用**本节点，不新造。分组的新增/改名同样要这一条
        # （拒绝侧挂 `system.delete_source` 做同域对照，见 `test_rbac_endpoint_enforcement.py`）。
        Site(
            INTERCEPT,
            "src/apps/business/api/source_group_views.py::OrderSourceGroupViewSet.get_permissions",
            'create / update / partial_update 动作 → ORDER_SOURCE_GROUP_PERMISSIONS'
            '["create"/"update"/"partial_update"] = ("system.add_edit_source",)',
        ),
    ),
    "system.delete_source": (
        Site(
            INTERCEPT,
            "src/apps/business/api/source_views.py::OrderSourceViewSet.get_permissions",
            'destroy 动作 → ORDER_SOURCE_PERMISSIONS["destroy"] = ("system.delete_source",)',
        ),
        # ⭐ 公司店铺分组：删除分组复用本节点（拍板④）。
        Site(
            INTERCEPT,
            "src/apps/business/api/source_group_views.py::OrderSourceGroupViewSet.get_permissions",
            'destroy 动作 → ORDER_SOURCE_GROUP_PERMISSIONS["destroy"] = ("system.delete_source",)',
        ),
    ),
    # ⭐ 客服后台打手钱包管理线：本仓**第六条自有节点**。
    # 六个端点共用一个 code、两半是同一件事（谁能动打手的钱与接单门槛）：
    # 钱包列表 + 调账（`TakerWalletAdminViewSet`）、押金列表 + 改押金（两个 `APIView`）、
    # 流水明细（`WalletTransactionAdminViewSet`）、
    # 个人押金额度读写（`TakerDepositQuotaView`）。
    # 门都写在**类体**的 `permission_classes = [IsAuthenticated, has_node(WALLET_ADJUST_NODE)]`
    # 上（T2 的「类.类属性」形态；节点 code 是模块常量，T2 会向下解引用一层）。
    # ⚠️ 体验位点 = 菜单条目 `agents-taker-wallet`（财务页合并线：原独立条目
    # 「打手钱包管理」与「打手提现审核」并成一页两页签，菜单元潜到合并条目下，
    # 位点随菜单同一次改指；T2b 按菜单声明逐条校验，指到已删除的路由名会当场红）。
    "finance.taker_wallet_adjust": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::agents-taker-wallet", "菜单条目声明 permissions"),
        Site(
            INTERCEPT,
            "src/apps/business/api/wallet_admin_views.py::TakerWalletAdminViewSet.permission_classes",
            "list（GET 钱包列表）+ adjust（POST 调账）共用本节点",
        ),
        Site(
            INTERCEPT,
            "src/apps/business/api/wallet_admin_views.py::DepositRuleListView.permission_classes",
            "get（GET 押金规则列表）",
        ),
        Site(
            INTERCEPT,
            "src/apps/business/api/wallet_admin_views.py::DepositRuleUpdateView.permission_classes",
            "put（PUT 改某游戏押金金额）",
        ),
        Site(
            INTERCEPT,
            "src/apps/business/api/wallet_admin_views.py::WalletTransactionAdminViewSet.permission_classes",
            "list（GET 流水明细）",
        ),
        Site(
            INTERCEPT,
            "src/apps/business/api/wallet_admin_views.py::TakerDepositQuotaView.permission_classes",
            "get（GET 打手个人押金额度）+ put（PUT 保存开关与按游戏金额）共用本节点",
        ),
    ),
    # ⭐ 打手提现审核线（D-0269 第二批）：本仓**第五条自有节点**。
    # 消费方 = 审核后台的整个集合（列表 + 5 个写动作），门写在**类体**的
    # `permission_classes = [IsAuthenticated, has_node(WITHDRAW_REVIEW_NODE)]` 上
    # （T2 的「类.类属性」形态；节点 code 是模块常量，T2 会向下解引用一层）。
    # ⚠️ 体验位点 = 菜单条目 `agents-taker-wallet`（财务页合并线：
    # 原独立条目「打手提现审核」与「打手钱包管理」并成一页两页签，菜单声明**元潜**到
    # 合并条目下，位点随菜单同一次改指；T2b 按菜单声明逐条校验，
    # 指到已删除的路由名会当场红）。
    "finance.taker_withdraw_review": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::agents-taker-wallet", "菜单条目声明 permissions"),
        Site(
            INTERCEPT,
            "src/apps/business/api/withdraw_admin_views.py::WithdrawAdminViewSet.permission_classes",
            "list（GET）+ approve / reject / transfer / refresh / mark_paid 五个写动作共用本节点",
        ),
    ),
    # ⭐ 闲管家收款明细线：本仓**第七条自有节点**。
    # 消费方 = 两个 GET 读端点（分组汇总 + 分页明细），门都写在**类体**的
    # `get_permissions`（返回 `[has_node(XGJ_RECEIPT_NODE)()]`，T2 的「类.方法」形态；
    # 节点 code 是模块常量 `XGJ_RECEIPT_NODE`，T2 会向下解引用一层）。
    # ⚠️ 体验位点 = 菜单条目 `xgj-receipts`（自建 island 页，没有对标证据件）。
    "finance.xgj_receipt_detail": (
        Site(EXPERIENCE, "src/frontend/src/config/menu.ts::xgj-receipts", "菜单条目声明 permissions"),
        Site(
            INTERCEPT,
            "src/apps/business/api/xgj_receipt_views.py::XgjReceiptSummaryView.get_permissions",
            "get（GET 分组汇总）",
        ),
        Site(
            INTERCEPT,
            "src/apps/business/api/xgj_receipt_views.py::XgjReceiptListView.get_permissions",
            "get（GET 分页明细）",
        ),
    ),
}

#: 待定：code → 阻塞原因（**不写位置**）。书写顺序 = `catalog.NODES` 的顺序。
PENDING: dict[str, str] = {
    "home.approval_penalty_approve": "旧权限已停用，罚款审批按指派处理，保留已有角色授权用于回滚",
    "home.approval_refund_handle": "旧权限已停用，平台退款待办按指派处理，保留已有角色授权用于回滚",
    "my_orders.import_to_my_system": "功能未实现（无对应端点/页面）；③线第一批判定见 docs/plans/2026-09-21-rbac-endpoint-enforcement.md §1.2(b)",
    "my_orders.export": "功能未实现（无对应端点/页面）；③线第一批判定见 docs/plans/2026-09-21-rbac-endpoint-enforcement.md §1.2(b)",
    "my_orders.set_seat": "功能未实现（无对应端点/页面）；③线第一批判定见 docs/plans/2026-09-21-rbac-endpoint-enforcement.md §1.2(b)",
    "my_orders.restrict_order_logs_to_own": "功能未实现（无对应端点/页面）；③线第一批判定见 docs/plans/2026-09-21-rbac-endpoint-enforcement.md §1.2(b)",
    "order.toggle_urgent": "功能未实现（无对应端点/页面）；③线第一批判定见 docs/plans/2026-09-21-rbac-endpoint-enforcement.md §1.2(b)",
    "order.view_important_note": "功能未实现（无对应端点/页面）；③线第一批判定见 docs/plans/2026-09-21-rbac-endpoint-enforcement.md §1.2(b)",
    "order.edit_important_note": "功能未实现（无对应端点/页面）；③线第一批判定见 docs/plans/2026-09-21-rbac-endpoint-enforcement.md §1.2(b)",
    "order.assignation": "功能未实现（无对应端点/页面）；③线第一批判定见 docs/plans/2026-09-21-rbac-endpoint-enforcement.md §1.2(b)",
    "order.quick_note": "功能未实现（无对应端点/页面）；③线第一批判定见 docs/plans/2026-09-21-rbac-endpoint-enforcement.md §1.2(b)",
    "order.set_seat": "功能未实现（无对应端点/页面）；③线第一批判定见 docs/plans/2026-09-21-rbac-endpoint-enforcement.md §1.2(b)",
    "order.web_order_list": "功能未实现（无对应端点/页面）；③线第一批判定见 docs/plans/2026-09-21-rbac-endpoint-enforcement.md §1.2(b)",
    "order.view_related_split": "功能未实现（无对应端点/页面）；③线第一批判定见 docs/plans/2026-09-21-rbac-endpoint-enforcement.md §1.2(b)",
    "order.batch_update_tags": "功能未实现（无对应端点/页面）；③线第一批判定见 docs/plans/2026-09-21-rbac-endpoint-enforcement.md §1.2(b)",
    "order.online_order_list": "功能未实现（无对应端点/页面）；③线第一批判定见 docs/plans/2026-09-21-rbac-endpoint-enforcement.md §1.2(b)",
    "order.restrict_to_own_group_leader": "功能未实现（无对应端点/页面）；③线第一批判定见 docs/plans/2026-09-21-rbac-endpoint-enforcement.md §1.2(b)",
    "order.restrict_view_source": "功能未实现（无对应端点/页面）；③线第一批判定见 docs/plans/2026-09-21-rbac-endpoint-enforcement.md §1.2(b)",
    "order.restrict_view_credentials": "功能未实现（无对应端点/页面）；③线第一批判定见 docs/plans/2026-09-21-rbac-endpoint-enforcement.md §1.2(b)",
    "order.restrict_view_source_options": "功能未实现（无对应端点/页面）；③线第一批判定见 docs/plans/2026-09-21-rbac-endpoint-enforcement.md §1.2(b)",
    # ⛔ `order.taobao_ship_records` 原先在这里写着「功能未实现：前端 1:1 渲染页……后端无模型/端点」
    # —— 自动发货线落地后**两个半句都过期**（后端有了模型与两个端点、
    # 前端页也已接活）⇒ 按维护规程第 1 条从本表删除、`INTERCEPT` 位点写进上面的 `SITES`、
    # `catalog.available` 翻真（T1 的互斥判据：有 `INTERCEPT` 位点的 code 不许留在 `PENDING`）。
    "order.online_payment_records": "功能未实现（无对应端点/页面）；③线第一批判定见 docs/plans/2026-09-21-rbac-endpoint-enforcement.md §1.2(b)",
    "order.restrict_sync_taker_price": "功能未实现（无对应端点/页面）；③线第一批判定见 docs/plans/2026-09-21-rbac-endpoint-enforcement.md §1.2(b)",
    "order.record_refund": "功能未实现（无对应端点/页面）；③线第一批判定见 docs/plans/2026-09-21-rbac-endpoint-enforcement.md §1.2(b)",
    "order.restrict_change_source": "功能未实现（无对应端点/页面）；③线第一批判定见 docs/plans/2026-09-21-rbac-endpoint-enforcement.md §1.2(b)",
    "order.update_tags": "功能未实现（无对应端点/页面）；③线第一批判定见 docs/plans/2026-09-21-rbac-endpoint-enforcement.md §1.2(b)",
    "order.restrict_create": "③线裁定不接线：属产品决策（会让新角色一条单都录不了），待业务方拍",
    "order.restrict_edit_existing_tags": "功能未实现（无对应端点/页面）；③线第一批判定见 docs/plans/2026-09-21-rbac-endpoint-enforcement.md §1.2(b)",
    "system_user.batch_set_game_permissions": "③线第二批判定不接线：没有批量端点，前端是对选中行逐个 PATCH games（system-user-list.ts:736-752），写门由 system_user.add_edit 承担；判定见 docs/plans/2026-09-21-rbac-batch2-system-user.md §1.2（2026-09-22 更新：**功能面已上线**——本线新增 src/frontend/src/layui-island/pages/system-setgames.ts 子页 + 父页 system-user-list.ts 的 type:2 iframe；原句里那个 system-user-list.ts:736-752 行号已随本线改动失效，逐个 PATCH 的写路径现在在子页的 save()。写门仍归 add_edit、本点仍无独立端点 ⇒ 仍 available=False：翻真要求该点有一个可达的代码位点，而 B1/B2 守这条，硬翻真会造出一个没有 INTERCEPT 位点的 True 点、B2 会红）",
    "system_user.login_restriction": "功能未实现：前端 1:1 渲染页（`pages/user-userloginrestrict.ts`，零 api 调用），后端无模型/端点",
    # ── 功能未实现：账号线的逐 action 节点表见 `ACCOUNT_PERMISSIONS`（apps/accounts/views.py），
    # 其中没有本点对应的端点。**刻意不写"只有 N 个动作"**：加一个 action 就会静默过期 ──
    "system_user.add_edit_login_restriction": "功能未实现：无对应端点，连独立页面都没有（登录限制页的弹层动作）",
    "system_user.blacklist": "功能未实现：前端 1:1 渲染页（`pages/user-backuserlist.ts`，零 api 调用），后端无模型/端点",
    "system_user.add_edit_blacklist": "功能未实现：无对应端点",
    "system_user.delete_blacklist": "功能未实现：无对应端点",
    # 没有可施加限制的面
    "system_user.restrict_mobile_app_access": "功能未实现：我们没有 APP / 小程序客户端，后端也没有任何客户端类型判据 ⇒ 无可施加限制的面",
    "system_user.contract_management": "功能未实现：前端 1:1 渲染页（`pages/user-usercontractsigning.ts`，零 api 调用），后端无模型/端点",
    "game.reorder_boost_types": "功能未实现：对标 `/GameAjax/UpDLType` 在本仓无对应动作/端点（全仓 `reorder`/`boost` 零命中）",
    "game.pin": "功能未实现：对标 `/GameAjax/TopGameByGameID` 在本仓无对应动作/端点（`GameViewSet`/`ProjectViewSet` 无 pin）",
    "finance.source_analysis": "功能未实现：对标 `/Emp/Operation` 在本仓无对应端点与页面",
    "finance.add_edit_performance": "功能未实现：对标 `/Emp/AddEditEmp`；`PerformanceViewSet` 只有 `list`/`adjust`/`review`，**无 create/update**",
    "finance.support_order_report": "功能未实现：对标 `/Emp/KeFuOperation` 无对应端点；⛔ 不挂到 `GET /api/business/order-stats`（那是 `system_user.view_order_stats` 的位点，Ruling 10）",
    "finance.settlement_records": "同一批端点已由别的点覆盖（Ruling 10：同一端点不重复挂第二个点）：`pages/emp-billsummary.ts` 真调 `GET /api/business/settlements`（判据 `finance.payroll`）与 `GET /api/business/performances/export`（判据 `finance.export_performance`）⇒ 本点只有菜单可见性、拦截层无独立位点",
    "finance.platform_fund_details": "功能未实现：前端 1:1 渲染页（`pages/emp-funddetailslist.ts`，零 api 调用），后端无模型/端点",
    "finance.override_global_settlement": "功能未实现（flag）：全仓只有 `catalog.py`/`seed.py` 提到它，**没有任何实现处** ⇒ 无面可施加",
    "finance.entrusted_commission_details": "功能未实现：对标 `/Emp/CommissionDetailsEntrustedAgency` 无对应端点/页面",
    "system.position_groups": "功能未实现：前端 1:1 渲染页（`pages/user-usergroup.ts`，零 api 调用），后端无模型/端点",
    "system.edit_position_group": "只有前端 1:1 职位分组页（菜单 `system-role-group`），后端无 `PositionGroup` 模型/端点 ⇒ 拦截层无位点（这是「编辑」动作，读页归 `system.position_groups` 的体验位点）",
    "system.add_edit_menu": "功能未实现：菜单是前端配置（`config/menu.ts`），后端无菜单表/端点",
    "system.sources": "只有体验层（菜单 `system-order-sources`）：读端点 `GET /api/business/order-sources/` 同时是录单页 `pages/order-addorder.ts` 以及 `pages/emp-emplist.ts` / `pages/system-pddshipsetting.ts` 的下拉数据源 ⇒ 挂 `system.sources` 会让没有该节点的角色读不到来源、**录不了单**（业务行为变更）⇒ 需要产品裁定（已登记残差）；写动作已由 `system.add_edit_source` / `system.delete_source` 覆盖",
    "system.order_guide_templates": "功能未实现：对标 `/System/OrderGuideTemplate` 无对应模型/端点",
    "system.add_edit_order_guide_template": "功能未实现：同上（`/System/AddEditOrderGuideTemplate`）",
    "system.platform_push_order_config": "功能未实现：前端 1:1 渲染页（`pages/system-platformpushorderconfig.ts`，零 api 调用），后端无模型/端点",
    "system.edit_online_consultation_staff": "功能未实现：对标 `/System/ModityOnlineConsultationSet` 无对应端点/页面",
    "system.auto_price_adjust_config": "功能未实现：前端 1:1 渲染页（`pages/system-automaticadjustpriceset.ts`，零 api 调用），后端无模型/端点",
    # 首页概览端点的门是「系统管理员」职位，不是本节点。不要补 INTERCEPT 位点，
    # 也不要翻 available：那会把「管理员进得了首页」说成「本节点拦得住」。
    "home.index": "前端 shell 首页（`layui-island/pages/index-console.ts`，1:1 复刻对标 `/Index/console`）+ 后端 `GET /api/business/home/overview/`（`apps/business/api/console_views.py`，首页「数据概览」12 格）。⚠️ 该端点的判据是 `apps.accounts.permissions.is_account_admin`（持「系统管理员」职位）**不是 RBAC 节点** ⇒ 本点拦截层无位点（可见性归 `menu_permissions`）",
    "home.enter": "「进入系统」= 前端 shell 自带，无独立端点 ⇒ 拦截层无位点",
    "home.order_ranking": "功能未实现：对标 `/Emp/RecOrderRanking` 无对应端点/页面",
    "home.restrict_upstream_market_entry": "功能未实现（flag）：前端也没有「找上下家入口」这个面 ⇒ 无面可施加",
    "home.system_circle": "功能未实现：对标 `/Home/SystemCircle` 无对应端点/页面",
    "home.system_circle_publish": "功能未实现：对标 `/Home/SystemCircleRelease` 无对应端点/页面",
    "home.blacklist_board": "功能未实现：对标 `/Home/blacklistplan` 无对应端点/页面（`system_user.*` 的黑名单是另一个栏目，不是首页避坑板）",
    "home.blacklist_report": "功能未实现：对标 `/Home/addblacklistplan` 无对应端点/页面",
    "my_account.edit_item_level": "只有前端 1:1 页（`pages/gameaccounts-myaccountlist.ts`），后端无端点 ⇒ 拦截层无位点",
    "my_account.edit_artifact_info": "只有前端 1:1 页（同 `gameaccounts-myaccountlist.ts`），后端无端点 ⇒ 拦截层无位点",
    "customer.blacklist": "功能未实现：前端 1:1 渲染页（`pages/customer-backlist.ts`，零 api 调用），后端无模型/端点",
    # ⛔ `customer.member_list` / `customer.member_analysis_report` 原先在这里登记
    # 「授权面翻真挂账」——翻真同批（2026-11-11）：`INTERCEPT` 位点写进上面的 `SITES`、
    # `catalog.available` 翻真（T1 的互斥判据：有 `INTERCEPT` 位点的 code 不许留在 `PENDING`）。
}

#: 保持空集。没做完的点写进 PENDING，不要再往这里塞。
IN_FLIGHT: frozenset[str] = frozenset()

#: 「**只有体验层位点、且合法**」的显式豁免集合（`tests/test_rbac_wiring.py` 的 T6 用）。
#:
#: 背景：T3 只查单向（`available=True` ⇒ 有 `INTERCEPT`），于是「有菜单可见性、服务端却
#: 敞着」这一类**静默**。T6 补反向：只有体验层位点、又不在 `PENDING`、且 `available=False`
#: 的 code 集合必须为空。
#:
#: 进这个集合 = **产品/技术裁定为「拦截层本就不该有位点」**，代价是该点从此不再被 T6 盯住
#: ⇒ 必须逐条在下面写理由，**不要**用「原因文本里含某个词」那种后门（那等于永久豁免，
#: 提醒会静默失效，E-0065）。**空集是稳态**——今天每一条只有体验层的点都给了 `PENDING`
#: 原因（功能未实现）或补上了 `INTERCEPT` 位点。
EXPERIENCE_ONLY_ALLOWED: frozenset[str] = frozenset()
