"""对标权限树，外加本仓自有节点（`benchmark_id = None`，目录因此不等于对标全表）。

新增一条自有节点要在这里加一行，并同一次改 `EXPECTED_NODE_COUNT`：

| code | `benchmark_id` | 为什么对标里没有它 |
|---|---|---|
| `system.allow_taker_open_api` | `None` | 打手侧开放接口（`docs/specs/2026-10-08-taker-open-api-design.md` §7.1）是本仓**自有功能**，鸡蛋铺没有这一页/这一项 |
| `system.edit_settings` | `None` | 系统设置页「保存开关」（D-0223）的保存端点 `/SystemAjax/SystemOrderSetting` 在对标权限清单文档里**没有对应行**（实测 grep 0 命中，`system` 栏目 197 行里也没有它）⇒ 按自有节点登记；`url` 落实测到的对标端点（`.tools/recon/suqing1/assets/JS2019/System/systemsetting.js` L113-115） |
| `home.qr_manage` | `None` | 后台首页「二维码横幅」（D-0262）是业务方在本仓**点名新增的自造区**（对标首页没有这个功能）⇒ `benchmark_id = None`；`url` 落我方自己的端点路径（`/api/business/home/qr-codes/`） |
| `system.qqbot_settings` | `None` | QQ 机器人设置（`feat/qqbot-line`）是本仓**自有功能**——对标物没有机器人对接这一页 ⇒ 按自有节点登记；`url` 自拟（对标 `system` 栏目的设置页形态 `/System/…`） |
| `finance.taker_withdraw_review` | `None` | 打手提现审核（`feat/taker-withdraw`）是本仓**自有功能**——对标物只有「鸡蛋铺官方账号资金明细与提现」（平台方自己的钱），没有打手钱包 / 提现审核 ⇒ 按自有节点登记；`url` 落我方端点（`/api/business/withdrawals/`） |
| `finance.taker_wallet_adjust` | `None` | 客服后台的打手钱包管理与调账（`feat/taker-wallet-adjust`）是本仓**自有功能**——对标物没有「客服给打手加/减余额或押金」这一块，也没有「按游戏改押金」这一页 ⇒ 按自有节点登记；`url` 落我方端点（`/api/business/wallets/`） |
| `finance.xgj_receipt_detail` | `None` | 闲管家收款明细是本仓**自有功能**——我们接了闲管家推送、对标不接 ⇒ 对标物里没有「闲管家收款明细」这一页；`url` 落我方端点（`/api/business/xgj-receipts/`） |
| `system.article_manage` | `None` | 频道文章作品（`feat/channel-articles`）是本仓**自有功能**——对标物没有「工作室频道 / 发布文章作品」这一块 ⇒ 按自有节点登记；`url` 落我方前端路由（`/app-mp/articles`，网页端「APP与小程序 → 发布文章作品」页） |
| `order.platform_write` | `None` | 交易平台（代练通）对接是本仓自有功能，对标物里没有这一整套；本点是「涉钱 / 写动作」的专属门（审计 M5），对标也没有可对应的权限行 |
| `customer.member_list_self_only` | `None` | 「只看自己录单的客户」（2026-11-11 业务方拍板新增的限制权限）是本仓自有功能，对标物没有这一项；本点是**限制语义的 flag**（勾上后行集收窄），code 按业务方口径命名 |
| `system.wecom_settings` | `None` | 企微自建应用设置（`feat/wecom-selfbuilt-app`，D-0379）是本仓**自有功能**——对标物没有企业微信对接这一页 ⇒ 按自有节点登记；`url` 落我方自己的 v2 路由（本页 v2-only，旧壳无此页） |

| `home.approval_apply` | `None` | 审批中心：可以从审批中心或管理订单发起罚款审批 |
| `home.approval_penalty_approve` | `None` | 审批中心：罚款审批按指派处理，旧权限已停用 |
| `home.approval_refund_handle` | `None` | 审批中心：平台退款待办按指派处理，旧权限已停用 |
| `home.approval_configure` | `None` | 审批中心：可以配置罚款审批人、抄送人和退款处理人 |

⚠️ **自有节点与对标点是两类东西，判据也不同**：对标点靠 `benchmark_id` 溯源到清单文档；
自有节点 `benchmark_id = None` ⇒ 它**不参与** `test_rbac_structure` 的「代码侧 == 文档 − 被裁」
逐字对拍（那三处按 `benchmark_id is None` 过滤），改由「`benchmark_id is None` 的节点
**恰好**等于自有节点清单」那条判据守着。

数据来源：`docs/JIDANPU-PERMISSION-CATALOG.md`（2026-09-20 活取实测，210 点）。
**唯一出处**——种子从本模块 upsert，不从数据库手改。

命名规则（spec §4.3，**冻结**）：`<module>.<action>`，全小写 ASCII snake_case。
- 13 个 `module` 见 `MODULES`，与对标 13 个一级栏目一一对应；
- `action` 用英文，**禁止拼音**（E-0005：拼音猜中文原文，猜中三个、猜错一个）；
- **`flag` 类 code 的极性与对标 `IsChecked` 一致**——判据是「**勾上之后会发生什么**」：
  勾上后**新增能力** ⇒ 许可性命名（`allow_*` / `override_*`）；勾上后**施加限制** ⇒
  限制性命名（`restrict_*` / `forbid_*`）。例：对标 `IsOnlyViewSelfOrder` ⇒
  `order.restrict_to_self_created`。反向命名 = 反转语义，必错（同型事故见 E-0011）。
  对标 flag 里有 2 条本身是放开语义（`order.allow_change_sales_agent` /
  `finance.override_global_settlement`），强行写成 `restrict_*` 会把语义反向。
  不变式是「持有该 code ⇔ 对标 note 描述的状态成立」：22 条限制型 + 2 条放开型。
  行内注释只标这两条例外；其余按各条 `note` 核对，不逐条再写一遍。

`name` / `note` / `url` **原文照抄**，不修对标自己的文案错误（例：[8] 系统用户的
Note 写"对游戏账号库进行管理"，实际是子账号管理——照抄，勘误记在清单文档里）。

flag 类不落 `url`：对标 URL 列给的是语义开关名，不是地址。`url` 留空，
开关名由 `benchmark_id` 溯源。结构断言钉住这一点。

裁掉的点不会被 `seed_catalog` 删掉，存量行靠数据迁移清。计数以下面两个常量为准。
"""
from apps.rbac.choices import NodeKind

#: 二级功能点条数（与 `NODES` 一致）。测试与种子都用它，避免两处各写一个数。
#: 自有节点 `benchmark_id is None`。存量租户靠数据迁移拿到新点，不靠重灌目录。
#: ⚠️ 2026-11-11 客户维护组收敛（业务方拍板）：删 8 个无功能 / 已删功能点后 159 → 151；
#: 同批新增自有限制节点 `customer.member_list_self_only`（第十条自有节点）⇒ 152；
#: 同日企微自建应用线新增自有页面节点 `system.wecom_settings`（第十一条）⇒ 153。
EXPECTED_NODE_COUNT = 157

#: 三类形态的当前计数（与 `NODES` 的 `kind` 一致；增减节点时这里必须一起改）。
EXPECTED_KIND_COUNTS = {NodeKind.PAGE: 83, NodeKind.API: 52, NodeKind.FLAG: 22}

#: (module_code, benchmark_id, name, note, url)
#: 顺序 = 对标 `GetUserRole` 的返回顺序（Menu_ID 升序）。
MODULES = (
    ("my_orders", 1, "我的订单", "分配给自己的订单", "/Order/MyOrderList"),
    ("my_performance", 2, "我的效绩", "我的效绩", "/Emp/MyEmp"),
    ("my_profile", 3, "我的资料", "我的资料", "/User/MyInfo"),
    ("grab_hall", 4, "接单大厅(内)", "内部接单大厅", "/Order/InsideRecOrderList"),
    ("order", 5, "订单业务", "订单业务管理", "/Order/"),
    ("system_user", 8, "系统用户", "对游戏账号库进行管理", "/User/"),
    ("game", 9, "游戏维护", "游戏信息维护", "/Game/"),
    ("finance", 10, "财务运营", "内部运营分析管理", "/Emp/"),
    ("system", 11, "系统维护", "管理系统设置", "/System/"),
    ("my_group", 13, "我的排团", "我的排团任务", "/Group/MyGroup"),
    ("home", 99, "主界面", "进入系统后的首页", "/Home/"),
    ("my_account", 129, "我的账号", "分配给自己的游戏账号", "/GameAccounts/MyAccountList"),
    ("customer", 137, "客户维护", "客户维护模块", "/Customer/"),
)

#: (code, benchmark_id, name, note, url, kind, available)
#: 书写顺序 = 清单文档的节序（13 个栏目）× 节内 `Menu_ID` 升序。
#: ⚠️ **书写顺序不参与排序**：出厂顺序由 `PermissionNode.Meta.ordering = ["benchmark_id"]`
#: 与 reverse FK 的 prefetch 路径共同决定；"= 对标页面渲染顺序"是未经实测的绝对句
#: （终局评审 D2），已删——真要对拍顺序得比对标的实际响应，本文件只保证 `benchmark_id` 可溯源。
#: `available` 默认 False：功能落地并且判据接上才翻 True。
#: 真清单以结构网的恰好集合为准，本处不另做一份会过期的枚举。
NODES = (
    # ── my_orders（5）
    ("my_orders.import_to_my_system", 110, "导入到我的系统",
     "在我的订单列表里选择导入到我的系统", "/Order/ImportToMySystem", NodeKind.PAGE, False),
    ("my_orders.send_to_my_system", 111, "发送订单到我的系统",
     "在我的订单列表里一键发送订单到自己的系统", "/OrderAjax/SendOrderToMySystem", NodeKind.API, True),
    ("my_orders.export", 126, "导出我的订单",
     "导出我的订单", "/Order/MyExportByOrderByIDX", NodeKind.PAGE, False),
    ("my_orders.set_seat", 128, "设定机位(我的订单)",
     "在“我的订单”列表里设定机位", "/OrderAjax/SetOrderJiWei", NodeKind.API, False),
    ("my_orders.restrict_order_logs_to_own", 1164, "不允许查看订单日志",
     "勾选此项后，在订单日志里只能看到自己添加的内容，看不到其它内容", "", NodeKind.FLAG, False),
    # ── my_performance（1）
    ("my_performance.export", 1163, "允许代练员或下家导出自己的绩效记录",
     "允许代练员或下家导出自己的绩效记录", "/Emp/ExportMyEmp", NodeKind.PAGE, True),
    # ── my_profile（1）
    # ⭐ 我的资料线翻真：自助改资料端点
    # `PATCH /api/auth/profile`（`apps/core/profile_views.py::MyProfileView`）上线。
    ("my_profile.edit", 121, "修改我的资料",
     "修改我的资料", "/UserAjax/EditMyInfo", NodeKind.API, True),
    # ── grab_hall（1）
    ("grab_hall.take_order", 50, "允许自助接单",
     "允许在接单大厅(内)自助接单", "/OrderAjax/RecOrderByOrderID", NodeKind.API, True),
    # ── order（57）
    ("order.add_edit", 12, "添加和编辑订单",
     "添加和编辑订单", "/Order/AddOrder", NodeKind.PAGE, True),
    ("order.list", 14, "订单管理列表",
     "订单管理列表", "/Order/OrderList", NodeKind.PAGE, True),
    ("order.view_content", 15, "显示订单内容项",
     "鼠标放在订单号上时显示的订单内容", "/OrderAjax/GetOrderContentById", NodeKind.API, True),
    ("order.change_status", 16, "更改订单状态（非已完成）",
     "更改订单状态（除了变为已完成之外的操作）", "/OrderAjax/UpOrderDLStaByIdx", NodeKind.API, True),
    ("order.change_status_completed", 17, "变更订单状态为已完成",
     "变更订单状态为已完成", "/OrderAjax/UpOrderDLStaToCompaletByIdx", NodeKind.API, True),
    ("order.assign_taker", 18, "指派订单给接手人",
     "指派订单给接手人", "/OrderAjax/SetOrderZhiPaiByIDX", NodeKind.API, True),
    ("order.clear_taker", 19, "清除指派人",
     "清除指派人", "/OrderAjax/CleanOrderZhiPaiByIDX", NodeKind.API, True),
    ("order.set_offer_price", 20, "设置或更新抛单价",
     "设置或更新扔到接单大厅（内）的抛单价", "/OrderAjax/UpdateDLOrderRevPriceByOrderID", NodeKind.API, True),
    ("order.toggle_urgent", 21, "订单加急或取消加急",
     "将订单加急或取消加急", "/OrderAjax/UrgentOrderByIDX", NodeKind.API, False),
    ("order.change_payment_status", 22, "更改订单收款状态",
     "在管理订单列表更改订单的收款状态(有没有收到钱)", "/OrderAjax/UpSettlementOrderByIDX", NodeKind.API, True),
    ("order.delete", 23, "删除订单",
     "删除订单", "/OrderAjax/DelOrderByIDX", NodeKind.API, True),
    ("order.view_important_note", 24, "订单列表查看重要备注",
     "勾选后，就能在管理订单列表或我的订单列表上看到订单的备注内容", "/OrderAjax/GetOrderGreMark", NodeKind.API, False),
    ("order.edit_important_note", 25, "修改订单列表重要备注",
     "勾选后，就能在管理订单列表或我的订单列表上点击备注栏快速修改备注内容", "/OrderAjax/SaveOrderGreMark", NodeKind.API, False),
    ("order.view_detail", 26, "查看代练订单详情",
     "查看代练订单详情", "/Order/OrderView", NodeKind.PAGE, True),
    ("order.add_log", 27, "添加订单日志",
     "允许手动添加订单日志", "/OrderAjax/SaveOrderPublieMark", NodeKind.API, True),
    ("order.export", 31, "导出订单",
     "允许从管理订单页面导出订单，重要权限请谨慎分配", "/Order/ExportByOrderByIDX", NodeKind.PAGE, True),
    ("order.view_screenshot", 32, "查看订单截图",
     "查看订单截图", "/OrderAjax/getOrderPicJson", NodeKind.API, True),
    ("order.upload_screenshot", 33, "上传订单图片",
     "上传订单图片", "/Order/UploadPic", NodeKind.PAGE, True),
    ("order.send_to_grab_hall", 36, "将订单发到内部抢单大厅",
     "允许将订单发到内部抢单大厅", "/OrderAjax/sendDLOrderRevPriceByOrderID", NodeKind.API, True),
    # ⚠️ **本点的语义已改造**（本线 D5）：对标 105 是「订单关联排团操作」，
    # 而排团整块已被业务方指令剔除（见本文件 §「已剔除」与 spec §3.5.2）。
    # `benchmark_id` **保留 105**（业务方裁定）、`url` 保留对标的端点串以便溯源，
    # 但 `code` / `name` / `note` 已改作**我方自有的「提交完单」**。
    # ⛔ **不许**把它改挂到 `order.change_status`(16)：`/transition/` 不校验归属，
    # 而代练员在状态机里有 `待上号 → 代练中` ⇒ 那会让代练员绕过 `/claim` 的三道闸门
    # （价格区间 / 持有上限 / 禁自助接单）。理由全文见 spec §3.6。
    # 名字必须同时说出「提交完单」和「提交问题单」：同一个点盖这两条端点。
    # 改 code 会让存量租户丢掉授权。
    # ⛔ **`code` 一个字都没动**（`order.submit_work`）：它带数据迁移
    # `0002_remove_pruned_nodes_and_rename_submit`、被 `seed.py` / `wiring.py`
    # 与多处测试按字面锚定 ⇒ 改 `code` 会让存量租户丢授权（迁移那支的 warnings 见文件头）。
    # ⚠️ 「改 `catalog.py` 对**已有**租户是空操作」这条老口径对本行**不成立**：
    # `seed_catalog()` 按 `benchmark_id`（105）upsert，`name` / `note` 都在 `defaults` 里
    # ⇒ 重跑 `seed_catalog` 就能把这行改名传播进库（与 `code` 的传播同一条路）。
    ("order.submit_work", 105, "提交完单和提交问题单",
     "把订单从「代练中」推进到「待验收」（代练员交单）或提交问题单",
     "/OrderAjax/ConfirmLinkGroup", NodeKind.API, True),
    ("order.assignation", 109, "订单指派接手人",
     "订单管理列表指派接手人", "/Order/Assignation", NodeKind.PAGE, False),
    ("order.submit_edit", 116, "订单编辑订单提交操作",
     "进行订单编辑订单提交操作", "/OrderAjax/AddOrderAjax", NodeKind.API, True),
    ("order.withdraw_from_grab_hall", 117, "从接单大厅(内)撤销",
     "在订单管理列表中选择某笔订单从接单大厅(内)撤销", "/OrderAjax/RelesOrderToHomeByOrderID", NodeKind.API, True),
    ("order.screenshot_manage", 120, "订单截图管理",
     "可以查看、删除上传的截图", "/Order/PicMange", NodeKind.PAGE, True),
    ("order.quick_note", 122, "快速订单重要备注",
     "在订单管理列表或我的订单列表点击订单备注时弹出快速备注窗体", "/Order/OrderRemark", NodeKind.PAGE, False),
    ("order.restrict_view_taker_price", 125, "不允许查看接单价",
     "不允许查看接单价，勾选则代表不让该人员在订单管理列表查看接单价，不勾则代表可看", "", NodeKind.FLAG, True),
    ("order.set_seat", 127, "设定机位(订单管理)",
     "在订单管理列表设定机位", "/OrderAjax/SetOrderJiWei", NodeKind.API, False),
    ("order.download_screenshots_zip", 132, "打包下载订单截图",
     "在订单截图管理页面点击打包下载的按钮", "/Order/ImportPicRar", NodeKind.PAGE, True),
    ("order.web_order_list", 135, "官网订单",
     "查看来自于自助录单的订单，可按需收纳到系统内", "/Order/WebOrderList", NodeKind.PAGE, False),
    ("order.view_related_split", 136, "查看拆分相关订单",
     "可以查看拆分订单的关联或相关订单列表", "/Order/AboutOrderList", NodeKind.PAGE, False),
    ("order.restrict_to_self_created", 148, "只能看自己录的订单",
     "勾选此项后，在订单管理列表就只能看到自己录的订单而不能看其它任何人录的订单", "", NodeKind.FLAG, True),
    ("order.batch_update_tags", 149, "批量快速更新订单标记",
     "勾选后则允许用户在订单管理列表批量或单笔快速更新订单的标记", "/OrderAjax/upOrderTips", NodeKind.API, False),
    ("order.restrict_view_offer_price", 150, "不能看抛单价",
     "勾选此项后，在订单管理列表就不能看任何订单的抛单价", "", NodeKind.FLAG, True),
    ("order.restrict_view_taker", 1156, "不能看接手人",
     "勾选此项后，在订单管理列表就不能看任何订单的接手人了，也无法进入指派接手人界面", "", NodeKind.FLAG, True),
    ("order.edit_payout_price", 1161, "在订单管理列表上修改发单价",
     "在订单管理列表修改发单价", "/Emp/ChangeRecPriceByOrder", NodeKind.PAGE, True),
    ("order.submit_payout_price", 1162, "在订单管理列表上修改发单价提交操作",
     "在订单管理列表上修改发单价时的提交请求", "/EmpAjax/ChangeOrderRecPrice", NodeKind.API, True),
    ("order.restrict_edit_taker_price", 1165, "不允许编辑订单接单价格",
     "对于已录入系统的订单，不允许编辑订单接单价格，只能编辑其它的内容", "", NodeKind.FLAG, True),
    ("order.online_order_list", 1172, "在线下单订单管理",
     "访问在线下单订单管理页面以及操作这个页面的内容", "/Order/OnlineOrderList", NodeKind.PAGE, False),
    ("order.restrict_to_own_group_leader", 1193, "管理订单列表是否仅可以查看团长为自己的订单",
     "在管理订单列表里，是否只能看到订单团长为自己的订单，其它订单无法看到，该功能特别适用于团长职位", "", NodeKind.FLAG, False),
    ("order.restrict_view_total_stats", 1201, "不允许查看管理订单上的统计值",
     "不允许查看管理订单页面数据列表头部上的统计数值", "", NodeKind.FLAG, True),
    ("order.online_refund", 1205, "在线订单退款管理界面及操作",
     "可以进入在线订单退款管理界面并进行退款操作", "/Order/OnlineRefundOrder", NodeKind.PAGE, True),
    ("order.restrict_view_source", 1207, "禁止查看订单来源",
     "勾选后将无法看到订单来源名称", "", NodeKind.FLAG, False),
    ("order.restrict_view_credentials", 1209, "禁止查看订单的账号密码、电话部分",
     "勾选后将不会看到订单的账号、密码、电话部分，其它信息可看，但订单接手人为自己时则不受限制", "", NodeKind.FLAG, False),
    ("order.restrict_view_source_options", 1210, "禁止查看订单来源下拉列表",
     "勾选后将无法看到订单来源下拉列表数据", "", NodeKind.FLAG, False),
    ("order.recycle", 1219, "找回删除的订单",
     "钩选则代表可以进入找回已删除订单界面，并且拥有还原已删除的订单权限", "/Order/OrderRecycle", NodeKind.PAGE, True),
    # ⭐ 自动发货线（spec `2026-10-03-xgj-auto-ship-design.md` §3.7）：**判据接上**
    # ⇒ `available=True`。消费方 = `TaobaoOrderSendLogViewSet.get_permissions`
    # （淘宝发货记录的**列表**与**重试发货**两个动作都判本点）。
    # 翻真同批三处：本行 + `apps/rbac/wiring.py` 的 `SITES` 补 `INTERCEPT` 位点 +
    # 从 `PENDING` 删掉该 code（原理由「后端无模型/端点」随本线落地而过期）；
    # ⚠️ 生成物 `docs/RBAC-WIRING-MAP.md` 同批重渲（`tests/test_rbac_wiring.py` 的 T4 逐字比对）。
    # ⚠️ 本点同时仍是菜单 `orders-taobao-ship` 的可见性承载者（`D5` 不新增节点、复用本点）。
    ("order.taobao_ship_records", 1227, "淘宝发货记录",
     "进入淘宝发货记录页面并且可以导入淘宝单号批量发货", "/Order/TBSendRecordList", NodeKind.PAGE, True),
    ("order.online_payment_records", 1235, "进入系统在线收款记录页面",
     "勾选后可以进入系统在线收款记录页面", "/Order/ReplaSystemPayDetails", NodeKind.PAGE, False),
    ("order.restrict_sync_taker_price", 1237, "不允许点击管理订单列表头部的“同步接单价”按钮",
     "勾选后代表此职位的人不允许点击管理订单列表头部的“同步接单价”按钮", "", NodeKind.FLAG, False),
    ("order.record_refund", 1239, "记录订单退款操作",
     "勾选后代表此职位的人可以在管理订单列表头部点击记退款按钮，将勾选的订单进行记录或编辑退款金额", "/OrderAjax/AddOrderRefundLogs", NodeKind.API, False),
    ("order.restrict_change_source", 1247, "编辑订单时不允许更换订单来源",
     "勾选后，在编辑订单时将无法更换订单来源，录新单子不受影响", "", NodeKind.FLAG, False),
    ("order.update_tags", 1258, "更改或清除订单标签",
     "勾选后代表此职位的人可以操作更改、删除订单上的订单标签值", "/OrderAjax/UpdateOrderLabel", NodeKind.API, False),
    # ⭐ 交易订单操作记录线（spec `2026-10-04-trade-action-log-design.md` §7-5）：
    # **判据接上** ⇒ `available=True`。消费方 =
    # `apps/audit/api/views.py::OrderActionLogListView`（类体上的
    # `permission_classes = [has_node("order.platform_action_logs")]`，本页唯一的读端点）。
    # 翻真同批三处：本行 + `apps/rbac/wiring.py` 的 `SITES` 补 `INTERCEPT` 位点 +
    # 从 `PENDING` 删掉该 code（原理由「后端无模型/端点」随本线落地而过期）；
    # ⚠️ 生成物 `docs/RBAC-WIRING-MAP.md` 同批重渲（`tests/test_rbac_wiring.py` 的 T4 逐字比对）。
    ("order.platform_action_logs", 1267, "交易订单操作记录",
     "勾选后代表此职位的人可以进入查看针对平台订单进行操作的记录列表", "/Order/TradingOrderActionLogList", NodeKind.PAGE, True),
    ("order.restrict_create", 1270, "禁止录入新订单",
     "勾选后代表此职位的人无法录入新单，包括无法拆单、无法一键复制新单等操作。此权限和录单/编辑订单冲突，此权限优先级最高", "", NodeKind.FLAG, False),
    # ⚠️ 放开型 flag（对标 AllowChangeCustomerService）：True = 允许更换销售客服，不是"限制更换"
    # ⭐ **判据接上**（接单销售客服可改线）⇒ `available=True`。消费方 =
    # `apps/business/api/order_views.py::OrderViewSet.partial_update` 的动作体里那道按请求体判的闸
    # （**带了发生变化的** `sales_agent` 才要本节点，值没变不判）。
    # ⚠️ 业务方 **简化裁定**：不复刻对标那条全局设置项 —— 本点的语义就是
    # 「持本点的人才能改这一格」，判据里**不读任何全局设置**（对标那句「不受系统全局设置项影响」
    # 因此在本仓是天然成立的，不是靠实现去兑现）。
    # 翻真同批三处：本行 + `apps/rbac/wiring.py` 的 `SITES` 补 `INTERCEPT` 位点 +
    # 从 `PENDING` 删掉该 code（原理由「它依赖我们不存在的全局设置项」随本裁定而过期）；
    # ⚠️ 生成物 `docs/RBAC-WIRING-MAP.md` 同批重渲（`tests/test_rbac_wiring.py` 的 T4 逐字比对）。
    ("order.allow_change_sales_agent", 1272, "允许任何时候都可以更换订单的销售客服",
     "勾选后代表此职位的人可以在编辑订单页面更换销售客服，此权限不受系统全局设置项影响", "", NodeKind.FLAG, True),
    # ⭐ 上家订单改价记录线（spec
    # `docs/specs/2026-10-17-order-log-names-and-upstream-price-log-design.md` §3-D5）：
    # **判据接上** ⇒ `available=True`。消费方 = 本页唯一的读端点
    # `GET /api/business/upstream-price-change-logs/`
    # （`apps/business/api/upstream_price_log_views.py::UpstreamPriceChangeLogListView`
    # 类体上的 `permission_classes`）。原 `PENDING` 理由「后端无模型/端点」随本线落地而过期
    # （后端有了模型、一套入向写入与这条只读端点）。
    # 翻真同批三处：本行 + `apps/rbac/wiring.py` 的 `SITES` 补 `INTERCEPT` 位点 +
    # 从 `PENDING` 删掉该 code；⚠️ 生成物 `docs/RBAC-WIRING-MAP.md` 同批重渲
    # （`tests/test_rbac_wiring.py` 的 T4 逐字比对）。
    # ⚠️ 页面路由 / 菜单的既有权限声明**不改**（判据 18 的后半句）：
    # `src/frontend/src/config/menu.ts::orders-source-price-log` 与这条 `NodeKind.PAGE`
    # 的 `/Order/SoureOrderChangePriceLog` 都保持原样。
    ("order.upstream_price_change_logs", 1273, "上家订单改价记录",
     "勾选后代表此职位的人可进入上家订单改价记录页面", "/Order/SoureOrderChangePriceLog", NodeKind.PAGE, True),
    ("order.restrict_edit_existing_tags", 1277, "不允许修改订单已有的标签，但允许给订单打新标签",
     "不允许在订单管理列表上修改已有的标签，但允许给订单打新的标签", "", NodeKind.FLAG, False),
    # ⭐ 交易平台涉钱写动作线（审计 M5）：本仓**第九条自有节点**
    # （`benchmark_id = None`）。对标物里没有「代练通 / 交易平台」这套对接 ⇒ 没有可溯源
    # 的 `benchmark_id`，按自有节点登记（既有惯例 = 上面八条自有节点的整套口径）。
    # ⭐ **为什么单独一个点**（审计 M5 原话）：这批 ≈23 个写端点（确认完单 = 扣钱、
    # 同意撤销 = 付代练费 + 赔保证金、补款 / 加价 / 改价、重发 = 再建真钱单、
    # 模板 CRUD = 上游账户级写…）原先**全部只挂 `order.list` 一个门**——「能看订单」
    # 就能对平台单做真钱写动作；同批本地涉钱动作（退款 / 改接单价）都有自己的专属节点，
    # 口径不一致。⇒ 读端点继续用 `order.list`，**写端点换成本点**。
    # ⭐ 消费方 = `dailiantong_push_views.py`（抛单 POST）、`dailiantong_action_views.py`
    # （撤回 / 改价 / 改时间 / 留言 / 全部撤回 / 确认完单 / 重发）、
    # `dailiantong_extra_views.py`（加价 / 仲裁 / 同意撤销 / 取消撤销 / 改值 / 下架 /
    # 锁定 / 拉黑 / 模板写 / 加款 / 编辑 / 截图）的写端点；位点见 `apps/rbac/wiring.py`
    # 的 `SITES`（读端点仍登记在 `order.list` 的同一条里）。
    # ⚠️ 存量租户拿到它靠数据迁移 `0014_add_order_platform_write_node`：
    # **建点之外还自动补授给「已拥有 `order.list` 的角色」**——否则上线瞬间现有客服
    # 会被新门锁死（写端点的门从 `order.list` 换成它，而存量角色都没有它）。
    # 补授是逐租户、幂等（M2M `add`）、只认「已有 `order.list`」这一条判据；
    # 反向不撤权（管理员在角色编辑页按需收紧）。**新建**租户的「客服 / 客服主管 /
    # 系统管理员」经 `seed.py` 的 `order` 栏目批发自动含它。
    # ⚠️ `url` 落**我方自己的**端点路径（自造节点没有对标端点可溯源，同 `home.qr_manage`）。
    ("order.platform_write", None, "交易平台涉钱写操作",
     "对交易平台订单执行涉钱 / 写动作（确认完单、同意撤销、补款、重发、加价改价、"
     "编辑、撤回、模板维护等）的提交请求；只读查询仍用订单列表权限",
     "/api/business/orders/<order_no>/platform-push/", NodeKind.API, True),
    # ── system_user（17）
    ("system_user.list", 58, "用户管理列表",
     "系统用户管理列表", "/User/UserList", NodeKind.PAGE, True),
    ("system_user.add_edit", 61, "添加编辑用户",
     "添加或编辑系统用户", "/User/AddUser", NodeKind.PAGE, True),
    ("system_user.change_status", 62, "更改用户状态",
     "在用户管理列表更改用户状态", "/UserAjax/UpUserSta", NodeKind.API, True),
    ("system_user.reset_password", 102, "重置登录密码",
     "重置用户的登录密码", "/UserAjax/RefChangePwd", NodeKind.API, True),
    ("system_user.batch_set_game_permissions", 1157, "批量设定系统用户游戏权限",
     "在系统用户列表批量设定系统用户的游戏分组权限", "/User/SetGames", NodeKind.PAGE, False),
    ("system_user.login_restriction", 1241, "登录限制设置",
     "勾选后代表此职位的人可以进入系统用户/登录限制设置页面查看、新增、编辑、删除", "/User/UserLoginRestrict", NodeKind.PAGE, False),
    ("system_user.add_edit_login_restriction", 1242, "添加编辑用户登录限制设置",
     "勾选后代表此职位的人可以进入系统用户/登录限制设置页/添加编辑用户登录限制设置", "/User/AddUserLoginRestrict", NodeKind.PAGE, False),
    ("system_user.blacklist", 1248, "系统用户黑名单管理",
     "勾选后代表此职位的人可以进入系统用户黑名单管理页面", "/User/BackUserList", NodeKind.PAGE, False),
    ("system_user.add_edit_blacklist", 1249, "新增或编辑系统用户黑名单",
     "勾选后代表此职位的人可以新增或编辑系统黑名单", "/UserAjax/AddModitySystemUserBlack", NodeKind.API, False),
    ("system_user.delete_blacklist", 1250, "删除系统用户黑名单",
     "勾选后代表此职位的人可以删除系统黑名单", "/UserAjax/DelSystemUserBlack", NodeKind.API, False),
    # ⭐ P3e：**判据已接上** ⇒ `available=True`。
    # 落点 = `apps/business/api/order_stats_views.py::OrderStatsView.get_permissions`
    # （`GET /api/business/order-stats` 的 `has_node("system_user.view_order_stats")`）；
    # 接线位点表 `apps/rbac/wiring.py` 的 `SITES` 同批登记（T3 要求 available ⇔ 有拦截位点）。
    ("system_user.view_order_stats", 1253, "查看接单统计",
     "勾选后代表此职位的人可以从系统用户管理页面查看系统所有子账号接单统计", "/UserAjax/GetRecOrderTotalList", NodeKind.API, True),
    # ⭐ T8（实名认证线）：**翻真**两条（此前 `available=False`）。
    # 判据落点（合取口径，T3：`available=True` ⇔ 有 `INTERCEPT` 位点）：
    # - 1263 ⇒ `apps/accounts/views.py::AccountViewSet.get_permissions`
    # 的逐 action 节点表（`realname_logs` / `realname_setting` / `realname_quota` 三条）；
    # - 1264 ⇒ 同一张表的 `reset_realname`。
    # ⛐ 不新增节点（`EXPECTED_NODE_COUNT` / `EXPECTED_KIND_COUNTS` **一个字都不动**）。
    ("system_user.realname_records", 1263, "系统用户实名认证记录与配置",
     "勾选后代表此职位的人可以查看系统子账号用户实名认证的记录和做一些配置、购买", "/User/RnameCertLogAndSetting", NodeKind.PAGE, True),
    ("system_user.reset_realname", 1264, "重置系统子账户的实名认证",
     "勾选后代表此职位的人可以重置系统子账号用户的实名认证", "/UserAjax/ResetRnameCert", NodeKind.API, True),
    ("system_user.restrict_view_id_card", 1265, "禁止查看子账号身份证号码",
     "勾选后代表此职位的人无法查看系统子账号身份证号码", "", NodeKind.FLAG, True),
    ("system_user.restrict_mobile_app_access", 1266, "禁止在APP或小程序端使用系统接单查单",
     "勾选后代表此职位的人无法使用鸡蛋铺APP或小程序访问系统的内部接单、我的订单等功能(只能用浏览器访问系统)，其它功能不受影响", "", NodeKind.FLAG, False),
    ("system_user.contract_management", 1278, "员工电子合同管理",
     "允许进入员工电子合同管理的页面并设置功能", "/User/UserContractSigning", NodeKind.PAGE, False),
    ("system_user.restrict_view_commission_params", 1281, "禁止查看其他人的提成数值参数",
     "勾选此项后，将无法查看所有子账号的提成参数，并且也无法修改自己的提成参数", "", NodeKind.FLAG, True),
    # ── game（7）
    # 区服 / 常用项目 / 项目分类：写动作挂在各自 ViewSet 上，所以 available=True。
    # 这三条是 PAGE，只拦写；列表和详情仍只要求登录。
    ("game.server", 63, "区服维护",
     "游戏区服职业阵营门派代练项目等维护", "/Game/GameServer", NodeKind.PAGE, True),
    ("game.add_edit", 68, "添加编辑游戏",
     "新增编辑游戏", "/Game/AddEditGame", NodeKind.PAGE, True),
    ("game.delete", 69, "删除游戏",
     "删除游戏维护的任意项", "/GameAjax/DelGame", NodeKind.API, True),
    ("game.reorder_boost_types", 70, "调整代练项目顺序",
     "调整代练项目顺序", "/GameAjax/UpDLType", NodeKind.API, False),
    ("game.pin", 71, "游戏置顶",
     "将指定游戏名称设为默认", "/GameAjax/TopGameByGameID", NodeKind.API, False),
    ("game.common_projects", 1166, "常用项目管理",
     "维护常用代练项目管理列表", "/Game/ProjectSetting", NodeKind.PAGE, True),
    ("game.order_type_categories", 1256, "订单项目分类管理",
     "勾选后代表此职位的人可以进入游戏维护/订单项目分类管理设置页面，并且可以操作此页面的参数设置", "/Game/OrderTypsSetting", NodeKind.PAGE, True),
    # ── finance（15）
    ("finance.payroll", 80, "打手与客服工资",
     "允许进入打手与客服工资管理列表页面，可以浏览所有人的提成明细", "/Emp/EmpList", NodeKind.PAGE, True),
    ("finance.source_analysis", 82, "来源分析",
     "订单来源分析数据", "/Emp/Operation", NodeKind.PAGE, False),
    ("finance.add_edit_performance", 83, "新增编辑效绩数据",
     "新增编辑效绩数据", "/Emp/AddEditEmp", NodeKind.PAGE, False),
    ("finance.change_settlement_status", 84, "更改结算状态",
     "更改账单结算状态", "/EmpAjax/UpEmpSta", NodeKind.API, True),
    ("finance.delete_performance", 85, "删除效绩数据",
     "删除效绩数据", "/EmpAjax/DelEmp", NodeKind.API, True),
    # ⭐ P4FU-B把它翻真：`POST /api/business/performances/delete`
    # （`api/performance_write_views.PerformanceDeleteView`）是它的真实消费方，
    # 位点登记在 `apps/rbac/wiring.py` 的 SITES。翻真前它是 PENDING 的
    # 「未派线：财务/结算后端未开工」——那句在端点落地后已过期。
    ("finance.support_order_report", 107, "客服接单统计报表",
     "客服接单统计报表", "/Emp/KeFuOperation", NodeKind.PAGE, False),
    # available=True：消费方是绩效导出（成功是 xlsx 字节，失败仍走信封）。
    ("finance.export_performance", 133, "导出效绩记录",
     "在效绩管理列表上导出查询的数据记录", "/Emp/ExportEmp", NodeKind.PAGE, True),
    # ⭐（D-0247）：本点**翻真**——两个读端点
    # `GET /api/business/support-stats` 与 `GET /api/business/business-report`
    # （`apps/business/api/report_views.py`，类体 `get_permissions`）是它的真实消费方。
    # 翻真前三处同批：`available` False→True、`wiring.SITES` 补 INTERCEPT、`PENDING` 删该 code。
    # ⚠️ 两页（客服接发单统计 / 经营报表）共用一个点 ⇒ 不能单独授权经营报表（D-0247 代价）。
    ("finance.support_statistics", 1192, "客服接单统计",
     "进入客服接单统计页面的权限", "/Emp/SalesmanStatistics", NodeKind.PAGE, True),
    ("finance.settlement_records", 1200, "结算记录",
     "进入财务运营下的结算记录页面", "/Emp/BillSummary", NodeKind.PAGE, False),
    ("finance.online_settlement_details", 1206, "在线结算明细",
     "可以进入在线结算明细管理界面", "/Emp/OnlinePayToRecDetail", NodeKind.PAGE, True),
    ("finance.platform_fund_details", 1228, "鸡蛋铺官方账号资金明细与提现",
     "进入鸡蛋铺官方账号资金明细与提现的页面", "/Emp/FundDetailsList", NodeKind.PAGE, False),
    # ⚠️ 放开型 flag（对标 MandatoryModifySettOrderJs）：True = 允许不受全局设置约束地改账单
    ("finance.override_global_settlement", 1259, "更改财务绩效账单不受全局控制影响",
     "勾选后代表此职位的人可以任意修改财务工资/打手与客服工资的绩效账单金额，不受全局系统设置的影响，请勿随意给此权限", "", NodeKind.FLAG, False),
    ("finance.alipay_authorization", 1269, "在线结算付款支付宝授权管理列表",
     "勾选后代表此职位的人可以进入在线结算付款支付宝授权管理列表页面，并且可以操作此页面的参数设置", "/Emp/BindAlipaySettAuth", NodeKind.PAGE, True),
    ("finance.entrusted_commission_details", 1279, "委托代发佣金明细",
     "允许进入委托代发佣金明细管理的页面", "/Emp/CommissionDetailsEntrustedAgency", NodeKind.PAGE, False),
    # ⭐ 打手钱包 / 提现审核线（D-0269 第二批）：本仓**第五条自有节点**
    # （`benchmark_id = None`）。对标物里没有「打手提现审核」这一页 / 这一项
    # ⇒ 无可溯源的 `benchmark_id`，按自有节点登记（既有惯例 = 上面四条自有节点的整套口径）。
    # ⭐ 消费方 = `apps/business/api/withdraw_admin_views.py::WithdrawAdminViewSet` 的
    # **类体** `permission_classes = [IsAuthenticated, has_node(WITHDRAW_REVIEW_NODE)]`
    # （列表 + 5 个写动作共用同一个 code）；位点见 `apps/rbac/wiring.py` 的 `SITES`。
    # ⚠️ 存量租户拿到它靠数据迁移 `0010_add_taker_withdraw_review_node`
    # （幂等、按 `code` 建、⛔ 不动角色授权）。
    # ⚠️ `url` 落**我方自己的**端点路径（自造功能没有对标端点可溯源，同 `home.qr_manage`）。
    ("finance.taker_withdraw_review", None, "打手提现审核",
     "打手提现审核：查看提现申请、通过 / 驳回、发起支付宝代付、登记人工打款",
     "/api/business/withdrawals/", NodeKind.API, True),
    # ⭐ 客服后台打手钱包管理线（`feat/taker-wallet-adjust`）：本仓**第六条自有节点**
    # （`benchmark_id = None`）。对标物里没有「客服给打手加/减余额或押金」这一块、也没有
    # 「按游戏改押金金额」这一页 ⇒ 无可溯源的 `benchmark_id`，按自有节点登记。
    # ⭐ 消费方 = `apps/business/api/wallet_admin_views.py` 的
    # `TakerWalletAdminViewSet.permission_classes` 与 `DepositRuleListView` /
    # `DepositRuleUpdateView` 的类体节点门（**四个端点共用一个 code**：钱包列表 / 调账 /
    # 押金列表 / 改押金——它们是同一件事「谁能动打手的钱与接单门槛」的两半）。
    # 位点见 `apps/rbac/wiring.py` 的 `SITES`。
    # ⚠️ 存量租户拿到它靠数据迁移 `0011_add_taker_wallet_adjust_node`
    # （幂等、按 `code` 建、⛔ 不动角色授权）。
    # ⚠️ `url` 落**我方自己的**端点路径（自造功能没有对标端点可溯源，同 `home.qr_manage`）。
    ("finance.taker_wallet_adjust", None, "打手钱包与押金管理",
     "打手钱包管理：查看余额 / 押金冻结、手工加减款；按游戏维护押金金额（影响谁能接单）",
     "/api/business/wallets/", NodeKind.API, True),
    # ⭐ 闲管家收款明细线：本仓**第七条自有节点**（`benchmark_id = None`）。
    # 对标物里没有「闲管家收款明细」这一页（我们接了闲管家推送、对标不接）⇒ 无可溯源的
    # `benchmark_id`，按自有节点登记（既有惯例 = 上面六条自有节点的整套口径）。
    # ⭐ 消费方 = `apps/business/api/xgj_receipt_views.py` 的
    # `XgjReceiptSummaryView` / `XgjReceiptListView`（**两个 GET 读端点共用一个 code**）；
    # 位点见 `apps/rbac/wiring.py` 的 `SITES`。
    # ⚠️ 存量租户拿到它靠数据迁移 `0012_add_xgj_receipt_detail_node`
    # （幂等、按 `code` 建、⛔ 不动角色授权）。
    # ⚠️ `url` 落**我方自己的**端点路径（自造功能没有对标端点可溯源，同 `home.qr_manage`）。
    ("finance.xgj_receipt_detail", None, "闲管家收款明细",
     "闲管家收款明细：查看各店铺交易成功笔数与实收金额统计、逐笔收款明细",
     "/api/business/xgj-receipts/", NodeKind.API, True),
    # ── system（29）
    ("system.role_authority", 87, "角色职位权限",
     "允许进入角色职位权限的管理页面，也能编辑权限", "/System/RoleAuthority", NodeKind.PAGE, True),
    ("system.position_groups", 88, "职位分组",
     "职位分组管理", "/User/UserGroup", NodeKind.PAGE, False),
    # ⭐ 本线（录单识别设置）：两个点已翻真——消费方是
    # `apps/business/api/recognition_views.py` 的两个视图（列表 GET / 改行 PATCH，
    # `permission_classes` 写在**类体**上，位点见 `apps/rbac/wiring.py` 的 `SITES`）
    # + 识别设置页路由（`src/frontend/src/config/routePermissions.ts::system-order-recognition`）。
    ("system.auto_discern_settings", 89, "录单自动识别设置",
     "录单自动识别设置", "/System/AutoDiscernSet", NodeKind.PAGE, True),
    # ⭐ **系统开关线（D-0223）翻真**：系统设置页的开关列表端点
    # `GET /api/business/system-switches/`（`apps/business/api/system_switch_views.py`
    # 的 `SystemSwitchListView`，类体 `permission_classes = [has_node("system.settings")]`）
    # ⇒ 位点写进 `wiring.SITES`、从 `PENDING` 摘掉、`available` 翻真（T3 三处同批）。
    # ⚠️ 本点同时仍是菜单条目 `system-settings`（`config/menu.ts`）与路由
    # （`config/routePermissions.ts`）可见性的承载者（`EXPERIENCE` 位点）。
    ("system.settings", 91, "系统设置",
     "系统设置", "/System/SystemSetting", NodeKind.PAGE, True),
    ("system.role_authority_edit", 93, "新增编辑角色职位",
     "新增编辑角色职位", "/System/AddEditRoleAuthority", NodeKind.PAGE, True),
    ("system.role_authority_delete", 94, "删除角色职位",
     "删除角色职位", "/SystemAjax/DelRole", NodeKind.API, True),
    ("system.edit_position_group", 95, "编辑职位分组",
     "编辑职位分组", "/User/EditUserGroup", NodeKind.PAGE, False),
    ("system.edit_auto_discern_keywords", 96, "编辑自动识别关键词",
     "编辑录单时的自动识别关键词", "/System/EditAutoDiscernSet", NodeKind.PAGE, True),
    ("system.publish_notice", 97, "发布公告",
     "发布公告", "/System/SystemNotice", NodeKind.PAGE, True),
    ("system.add_edit_menu", 103, "新增编辑栏目操作",
     "新增编辑栏目操作", "/SystemAjax/AddEditMenu", NodeKind.API, False),
    # available=True：日志列表与操作人两个读端点的类体门就是本点。
    ("system.logs", 108, "系统日志",
     "系统日志列表", "/System/SystemLog", NodeKind.PAGE, True),
    ("system.sources", 113, "来源管理",
     "订单来源管理", "/System/SoureList", NodeKind.PAGE, False),
    ("system.add_edit_source", 114, "新增编辑订单来源",
     "新增编辑订单来源", "/System/AddEditSoure", NodeKind.PAGE, True),
    ("system.delete_source", 115, "删除订单来源",
     "删除订单来源", "/SystemAjax/DelSoure", NodeKind.API, True),
    ("system.order_guide_templates", 1158, "订单附加要求模板管理",
     "进入订单生成完善信息引导模板管理列表页面", "/System/OrderGuideTemplate", NodeKind.PAGE, False),
    ("system.add_edit_order_guide_template", 1159, "新增编辑订单附加要求模板",
     "进入新增编辑订单生成完善信息引导模板页面", "/System/AddEditOrderGuideTemplate", NodeKind.PAGE, False),
    ("system.role_authority_view", 1169, "角色权限预览清单界面",
     "“角色职位”列表里可以点击角色名称查看权限清单", "/System/RoleAuthorityView", NodeKind.PAGE, True),
    ("system.platform_integration_settings", 1189, "交易平台对接设置",
     "允许进入第三方交易平台对接设置页面", "/System/TripartitePlatformSetting", NodeKind.PAGE, True),
    # ⭐ 交易平台对接设置线（T2）：判据已接上 ⇒ `available=True`。
    # 消费方 = `apps/business/api/platform_integration_views.py` 的两个视图
    # （账号集合与全局设置）的**类体** `permission_classes`，参数是模块常量
    # `PLATFORM_INTEGRATION_NODE = "system.platform_integration_settings"`
    # （`wiring.SITES` 的 T2 判据向下解析一层常量）。
    # 翻真同批三处：本行 `available` + `wiring.SITES` 补两个 `INTERCEPT` 位点 +
    # 删 `wiring.PENDING` 该 code，并重渲 `docs/RBAC-WIRING-MAP.md`。
    # available=True 的消费方是开放平台应用管理（节点元组里有本点）。
    # 店铺绑定的写动作也收本点。不要拿它当上家绑定页的门。
    # has_node 不看 available，有消费方就不能把它翻回 False。
    ("system.pinduoduo_settings", 1212, "拼多多对接设置",
     "可以进入拼多多对接设置页面并且进行设置项操作", "/System/PddShipSetting", NodeKind.PAGE, True),
    # ⭐ （`feat/upstream-downstream-sync` 文档波，**裁决 R22**）：**判据已接上** ⇒
    # `available=True`。两个页面端点（`PeerBindingListView` / `PeerBindingDetailView`）现在
    # 在类体上声明 `permission_classes = [has_node("system.bind_upstream_system")]`。
    # 翻真同批三处：`wiring.SITES` 补 `INTERCEPT` 位点 + 删 `PENDING` +
    # 生成物 `docs/RBAC-WIRING-MAP.md` 重渲（T3 要求 available ⇔ 有拦截位点）。
    # ⚠️ 本点仍**同时**是菜单 `system-source-system` 的可见性承载者（D5 不新增节点、复用本点）。
    ("system.bind_upstream_system", 1229, "绑定上家系统设置",
     "进入绑定上家系统设置的页面", "/System/HomeSystemSetting", NodeKind.PAGE, True),
    # ⭐ 交易平台自动回复线（T13）：**判据已接上** ⇒ `available=True`。
    # 规则 CRUD 的整个集合（读 + 写）在
    # `apps/business/api/platform_auto_reply_views.py::PlatformAutoReplyRuleViewSet.permission_classes`
    # 上声明本点。翻真同批四处：`wiring.SITES` 补 `INTERCEPT` 位点 + 删 `PENDING` +
    # 重渲 `docs/RBAC-WIRING-MAP.md` + `tests/test_rbac_structure.py` 的消费方清单（B2 等号断言）。
    # ⚠️ 本点**同时**仍是菜单 `system-platform-auto-reply` 与路由 `meta.permissions`
    # （`config/routePermissions.ts`）的可见性承载者——拦截层与体验层并存，不是替代。
    ("system.platform_auto_reply", 1238, "交易平台自动回复打手设置",
     "勾选后代表此职位的人可以进入交易平台自动回复打手设置页面并进行相关设置", "/System/PlatformAutoReply", NodeKind.PAGE, True),
    ("system.platform_push_order_config", 1240, "交易平台默认发单配置",
     "勾选后代表此职位的人可以进入交易平台发单配置页面，并且有权可以操作配置", "/System/PlatformPushOrderConfig", NodeKind.PAGE, False),
    ("system.edit_online_consultation_staff", 1244, "编辑在线咨询接待人员",
     "勾选后代表此职位的人可以操作在线咨询相关设置页面的接待人员", "/System/ModityOnlineConsultationSet", NodeKind.PAGE, False),
    ("system.order_push_group_settings", 1255, "发单消息推送微信群设置",
     "勾选后代表此职位的人可以进入系统维护/发单消息推送微信群设置页面，并且可以操作此页面的参数设置", "/System/OrderPushGroupMsgSet", NodeKind.PAGE, True),
    ("system.auto_price_adjust_config", 1262, "交易单自动调价配置",
     "勾选后代表此职位的人可以进入并配置交易订单自动调价机制", "/System/AutomaticAdJustPriceSet", NodeKind.PAGE, False),
    # ⭐ **T4（打手侧开放接口）：本仓第一条自有节点**（spec §7.1）。
    # ⚠️ `benchmark_id = None` —— 对标物里**没有**这一项（打手侧开放接口是本仓自有功能），
    # 所以它**不参与**「代码侧 == 文档 − 被裁」的逐字对拍（那三处按 `None` 过滤），
    # 改由「`benchmark_id is None` 的节点恰好 == 自有节点清单」那条判据守着。
    # ⛔ **不按 `benchmark_id` upsert**：`benchmark_id=None` 会退化成 `isnull` 查找
    # （`apps/rbac/seed.py::seed_catalog` 已按 `code` 走，理由见那里的 docstring）。
    # ⛔ **存量租户**拿到它靠数据迁移 `0006_add_taker_open_api_node`（幂等、按 `code` 建、
    # ⛔ 不动角色授权），⛔ 不是靠重灌目录（`seed_catalog` 不重配已有角色的权限）。
    # ⭐ `available=True` 是**合取**：本点已有真实消费方 —— 内部端点
    # `/api/taker/apps/…`（`apps/takerapi/api/app_views.py::TakerAppAdminView` 的类体
    # `permission_classes = [has_node(*APP_MANAGE_NODES)]`，`APP_MANAGE_NODES` 里有本 code
    # **字面量**）；位点登记见 `apps/rbac/wiring.py` 的 `SITES`（T3 要求 available ⇔ 有拦截位点）。
    # ⚠️ 本点**同时**是菜单条目 `taker-apps`（`config/menu.ts`）与路由
    # `taker-apps`（`config/routePermissions.ts`）可见性的承载者（`EXPERIENCE` 位点）。
    ("system.allow_taker_open_api", None, "允许打手使用开发平台应用",
     "打手可进入「打手对接」页并为自己创建一套接入凭据", "/open-platform/taker-apps", NodeKind.PAGE, True),
    # ⭐ **系统开关线（D-0223）：本仓第二条自有节点**（`benchmark_id = None`）。
    # 为什么是对标里没有的：SK 开关的**保存端点** `/SystemAjax/SystemOrderSetting`
    # 在对标权限清单文档里没有行（实测 grep 0 命中）⇒ 没有可溯源的 `benchmark_id`，
    # 按自有节点登记（既有惯例 = `system.allow_taker_open_api` 那一条的整套口径）。
    # ⭐ `available=True` 是**合取**：本点已有真实消费方 —— `PATCH /api/business/system-switches/<key>/`
    # （`apps/business/api/system_switch_views.py::SystemSwitchDetailView` 的类体
    # `permission_classes = [has_node("system.edit_settings")]`）；位点见
    # `apps/rbac/wiring.py` 的 `SITES`。⚠️ 存量租户拿到它靠数据迁移
    # `0007_add_system_edit_settings_node`（幂等、按 `code` 建、不动角色授权）。
    ("system.edit_settings", None, "编辑系统设置",
     "编辑系统设置里的开关（保存设置）", "/SystemAjax/SystemOrderSetting", NodeKind.API, True),
    # ⭐ **QQ 机器人线：本仓第三条自有节点**（`benchmark_id = None`）。
    # 为什么是对标里没有的：QQ 机器人对接是本仓自有功能，对标物没有「机器人设置」
    # 这一项 ⇒ 没有可溯源的 `benchmark_id`，按自有节点登记（既有惯例 =
    # `system.allow_taker_open_api` / `system.edit_settings` 那两条的整套口径）。
    # ⭐ `available=True` 是**合取**：本点已有真实消费方 —— `apps/qqbot/api/views.py`
    # 的全部页面端点（`GET/PATCH /api/qqbot/binding/`、群列表 / 推送配置 / 测试推送 /
    # 入群审批 / 群菜单 / 分享链接 / 发件箱列表）共用基类
    # `QQBotSettingsGateView` 的类体 `permission_classes = [has_node("system.qqbot_settings")]`；
    # 位点见 `apps/rbac/wiring.py` 的 `SITES`。⚠️ 存量租户拿到它靠数据迁移
    # `0008_add_qqbot_settings_node`（幂等、按 `code` 建、不动角色授权）。
    # ⚠️ 菜单条目尚未接（`config/menu.ts`）⇒ 本点今天**只有** `INTERCEPT` 位点、
    # 没有 `EXPERIENCE` 位点（T2b 只对**声明了的**体验位点做对拍，不要求每个点都有）。
    ("system.qqbot_settings", None, "QQ 机器人设置",
     "QQ 机器人设置：机器人密钥、群与推送配置、入群审批、菜单", "/System/QQBotSetting", NodeKind.PAGE, True),
    # ⭐ 频道文章作品线（`feat/channel-articles`）：本仓**第八条自有节点**
    # （`benchmark_id = None`）。对标物里没有「工作室频道 / 发布文章作品」这一块
    # ⇒ 无可溯源的 `benchmark_id`，按自有节点登记（既有惯例 = 上面七条自有节点的整套口径）。
    # ⭐ 消费方 = `apps/business/api/article_views.py` 的 `ArticleViewSet`（CRUD + 发布 /
    # 下架）与 `ChannelProfileAdminView` / `ArticleMediaTicketView` / `ArticleMediaClaimView`
    # （**全部端点含读共用一个 code**：网页端「发布文章作品」是一个独立入口）；
    # 位点见 `apps/rbac/wiring.py` 的 `SITES`。
    # ⚠️ 存量租户拿到它靠数据迁移 `0013_add_system_article_manage_node`
    # （幂等、按 `code` 建、⛔ 不动角色授权）。
    # ⚠️ `url` 落**我方自己的**前端路由（自造功能没有对标端点可溯源，同 `home.qr_manage`）。
    # ⚠️ 公开读端点（`/api/channel/…`，匿名可读）**不挂**本点：它们是给未登录读者看的，
    # 挂上会把「没登录也能看频道」这条产品口径直接破掉。
    # ⚠️ 形态是 `page` 而不是 `api`（与「端点门=api 形态」的一般惯例不同）：这一页的
    # 管理端 11 条端点（含读）**同属一个页面动作**，拆成 page + api 两个点会让管理员在
    # 角色编辑页看到两行、且只勾一半就会「菜单进得去、点保存 403」；按页面一个点收口，
    # 授权语义与其余页面一致。`wiring.SITES` 已登记四个拦截位点，`available=True` 成立。
    ("system.article_manage", None, "发布文章作品",
     "编辑、发布、下架工作室频道文章", "/app-mp/articles", NodeKind.PAGE, True),
    # ⭐ 企微自建应用线（D-0379）：本仓**第十一条自有节点**（`benchmark_id = None`）。
    # 为什么是对标里没有的：企业微信对接是本仓自有功能，对标物没有「企微设置」这一项
    # ⇒ 没有可溯源的 `benchmark_id`，按自有节点登记（既有惯例 = 前十条自有节点的整套口径）。
    # ⭐ `available=True` 是**合取**：本点已有真实消费方 —— `apps/wecom/views.py`
    # 的全部页面端点（配置 / 自检 / 测试发送 / 通讯录与客户联系只读 proxy / 台账）
    # 共用基类 `WecomSettingsGateView` 的类体 `permission_classes = [has_node("system.wecom_settings")]`；
    # 位点见 `apps/rbac/wiring.py` 的 `SITES`。⚠️ 存量租户拿到它靠数据迁移
    # `0017_add_wecom_settings_node`（幂等、按 `code` 建、⛔ 不动角色授权）。
    # ⚠️ `url` 落**我方自己的**前端路由（自造功能没有对标端点可溯源，同 `system.article_manage`）；
    #    本页是 **v2-only**（D-0344：新前端独立路由 `/v2`，旧壳无此页）。
    # ⚠️ 形态是 `page`（整页端点含读共用一个 code，按页面一个点收口——同 `system.article_manage` 的理由）。
    ("system.wecom_settings", None, "企微自建应用设置",
     "企微自建应用：应用凭据、回调验证、通讯录与客户联系只读拉取、测试发消息、消息台账",
     "/v2/wecom-settings", NodeKind.PAGE, True),
    # ── my_group（0）
    # ⚠️ ：本栏目**保留**（`MODULES` 行不动——6 条 `groups-*` 路由的判据是它），
    # 但唯一的子点 `my_group.add_items`(106) 已按 spec §3.5 裁剪 ⇒ 子节点数为 **0**。
    # ── home（10）
    ("home.index", 98, "主界面访问",
     "进入系统后的主界面，请将所有人设置此权限", "/index/index", NodeKind.PAGE, False),
    ("home.enter", 100, "进入系统",
     "进入系统后的页面，请将所有人都设置该权限", "/", NodeKind.PAGE, False),
    # ⭐ 我的资料线翻真：自助改密端点
    # `POST /api/auth/password`（`apps/core/auth_views.py::ChangePasswordView`）上线。
    ("home.change_password", 101, "修改登录密码",
     "修改登录密码，请将所有人都设置该权限", "/User/ChangePwd", NodeKind.PAGE, True),
    # ⭐ 问题反馈线翻真：本点原先只有对标的一个 URL、服务端零判据
    # （`wiring.PENDING` 里写着「功能未实现：对标 `/Index/QuestionBack` 无对应端点/页面」）。
    # 本线落地了四个端点（`apps/business/api/feedback_views.py`），三个吃本点的节点门
    # ⇒ 位点写进 `wiring.SITES`、从 `PENDING` 摘掉、`available` 翻真（T3：两者必须同向）。
    ("home.feedback", 112, "问题反馈及建议",
     "对开发者提出问题反馈及建议，建议全员开放权限", "/Index/QuestionBack", NodeKind.PAGE, True),
    # ⭐ **首页二维码横幅线（D-0262）：本仓第三条自有节点**（`benchmark_id = None`，
    # 登记表见模块 docstring）。⚠️ **自造点、无对标**：横幅是业务方点名新增的首页自造区，
    # 对标 `/Index/console` 没有对应功能。⭐ `available=True` 是**合取**：本点已有真实消费方
    # —— `POST / PUT / PATCH / DELETE /api/business/home/qr-codes/…`（写动作逐 action 挂本点，
    # 读列表只要求登录、按持点分档返回，位点见 `apps/rbac/wiring.py` 的 `SITES`）。
    # ⚠️ 存量租户拿到它靠数据迁移 `0008_add_home_qr_manage_node`（幂等、按 `code` 建、
    # ⛔ 不动角色授权）；**新建**租户的内置「人事」经 `seed.py` 的 `home` 栏目批发**自动含它**
    # （非 flag 的栏目子节点），存量租户的人事要管理员在「角色职位权限」页手工勾。
    ("home.qr_manage", None, "首页二维码横幅管理",
     "自造点（D-0262，无对标）：勾选后代表此职位的人可以在后台首页配置二维码横幅"
     "（添加 / 更换图片 / 启用停用 / 删除 / 排序）；无此点的人只能看启用的二维码",
     "/api/business/home/qr-codes/", NodeKind.API, True),
    ("home.order_ranking", 1190, "浏览接单排行榜界面",
     "浏览接单排行榜界面", "/Emp/RecOrderRanking", NodeKind.PAGE, False),
    ("home.restrict_upstream_market_entry", 1208, "关闭页面顶部找上下家入口",
     "勾选后将不会看到系统顶部的找上下家入口", "", NodeKind.FLAG, False),
    ("home.system_circle", 1215, "系统圈子以及相关操作",
     "钩选则代表可以进入系统圈子界面，并且拥有查看别人发布的业务消息权限", "/Home/SystemCircle", NodeKind.PAGE, False),
    ("home.system_circle_publish", 1216, "系统圈子发布新消息操作",
     "钩选则代表可以进入系统圈子界面，并且拥有发送业务消息权限", "/Home/SystemCircleRelease", NodeKind.PAGE, False),
    ("home.blacklist_board", 1230, "避坑板查询页面",
     "进入避坑板查询界面的权限", "/Home/blacklistplan", NodeKind.PAGE, False),
    ("home.blacklist_report", 1231, "避坑板举报",
     "勾选此项表示可以提交举报信息", "/Home/addblacklistplan", NodeKind.PAGE, False),
    ("home.approval_apply", None, "发起罚款审批",
     "可以从审批中心或管理订单发起罚款审批", "/api/business/approvals/", NodeKind.API, True),
    ("home.approval_penalty_approve", None, "审批罚款（旧权限已停用）",
     "罚款审批按指派处理", "/api/business/approvals/", NodeKind.API, False),
    ("home.approval_refund_handle", None, "处理退款审批（旧权限已停用）",
     "平台退款待办按指派处理", "/api/business/approvals/", NodeKind.API, False),
    ("home.approval_configure", None, "配置审批中心",
     "可以配置罚款审批人、抄送人和退款处理人", "/api/business/approvals/settings/", NodeKind.API, True),
    # ── my_account（2）
    ("my_account.edit_item_level", 130, "编辑账号装备等级信息",
     "适用于在预览内部、临时、借用账号详情时编辑装备等级信息", "/GameAccountsAjax/EditGameAccItemLevel", NodeKind.API, False),
    ("my_account.edit_artifact_info", 131, "编辑账号装备神器与其它信息",
     "适用于在预览内部、临时、借用账号详情时编辑装备神器与其它装备信息", "/GameAccountsAjax/EditGameAccOhterLevel", NodeKind.API, False),
    # ── customer（3；2026-11-11 业务方拍板「客户维护组收敛到 3 个页面」：余额明细 /
    # 等级设置等 8 个无功能 / 已删功能的点从目录删除（对标有、裁定不做，D-0036 同族），
    # 存量行由数据迁移 `0015_prune_customer_module_nodes` 删）
    ("customer.blacklist", 138, "黑名单",
     "客户黑名单列表", "/Customer/BackList", NodeKind.PAGE, False),
    # ⭐ 翻真 + 改名（2026-11-11 业务方拍板，spec §4.3）：功能早已上线（四端点守卫，
    # 位点见 wiring.SITES）⇒ available 翻真；name / note 改客户口径（对标 id 不变）。
    # 存量租户靠数据迁移 `0017_rename_and_flip_customer_member_nodes` 传播。
    ("customer.member_list", 141, "客户管理",
     "勾选此项后代表此职位的人可以进入客户管理列表", "/Customer/CustomerList", NodeKind.PAGE, True),
    ("customer.member_analysis_report", 1226, "客户分析报表",
     "进入客户分析报表的页面", "/Customer/HyAnalysisReport", NodeKind.PAGE, True),
    # ⭐ 第十条自有节点（2026-11-11 业务方拍板）：**限制语义的 flag**——勾上后
    # 「客户管理」行集收窄到「录单人=自己的系统订单所连的客户」；与 `customer.member_list`
    # **任一**即可进页面（守卫 OR），只持本点看不全量。`available=True` 的消费方 =
    # `apps/business/api/customer_views.py` 四端点的守卫与行集收口（位点见 wiring.SITES）；
    # 存量租户靠数据迁移 `0016_add_customer_member_list_self_only_node` 落行。
    ("customer.member_list_self_only", None, "只看自己录单的客户",
     "勾选后客户管理列表只显示录单人为自己的系统订单所连的客户；"
     "与会员管理权限任一即可进入页面，只持本点看不到全部客户",
     "", NodeKind.FLAG, True),
)

#: 栏目 code 与功能点 code 的并集。管理员短路用它、不查库：
#: 租户管理员等价于持有全目录，口径只在这一处定义。本表是唯一出处，不从数据库现取。
ALL_CODES = frozenset([m[0] for m in MODULES] + [n[0] for n in NODES])
