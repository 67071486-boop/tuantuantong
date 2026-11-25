/**
 * 【我的订单】页的 **layui 复刻页 + 接真后端**（对标物 `/Order/myorderlist`，**证据件渲染**）。
 *
 * 形状：**单表列表页**（8 个筛选控件 / 5 个 `.layui-form-label` + 工具栏 5 颗按钮
 * （运行期再注入 1 颗「同步订单状态」）+ 表格 + `#total` 统计行）。
 *
 * 证据（值一律从对标取）：
 *  - 标记与页内 `<style>` → `public/replica/order-myorderlist.html`
 *    （源 `.tools/recon/suqing1/html/Order__myorderlist.html` 的 L24-247，逐字；仅删非标记 `<script>`、
 *    解 laytpl、去掉 `<div id="root">` / `<template id="template">` 外壳、去掉 1 个 `<link>` 外链）
 *  - 列定义 → `.tools/recon/suqing1/assets/JS2019/Order/myorderlist.js` **L735-927**
 *  - `table.render` 其余参数 → 同文件 **L732-734**（`defaultToolbar` 两次、后者 `['filter']` 生效）、
 *    **L928-930**（`page:true` / `limit:20` / `limits:[10,20,30,40,50]`）
 *  - `done` 里的 `#total` 文案模板 → 同文件 **L941-944**（i18n 值取同文件
 *    `i18nData.messages['zh-CN'].js` 的 `dataTotal1…dataTotal10`）
 *  - 工具栏模板 → 同上 HTML 的 `#toolbarDemo`（L221-233）、行内按钮 `#baroperating`（L235-237）
 *  - 「提交完成」的确认文案与成功/失败提示时序 → 同文件 **L1198-1230**（`submtEnd` 分支）、
 *    确认文案 i18n **L451**（`js.submtEnd`）
 *
 * ## 分页：**开启**（`myorderlist.js` L928/L930 逐字）
 *
 * `page: true`（不是「未写」）+ `limit: 20` + `limits: [10,20,30,40,50]`。⚠️ 但**渲染上看不到分页**：
 * 对标该租户 0 行，而 layui 对 0 行不渲染 `.layui-laypage`（layui.js offset 296680 的 `0==n` 分支），
 * 所以这里**不能**用渲染结果判 page —— 取值来自页面 JS 源码。这也是本页 `page` 必须显式传
 * `{limit, limits}` 的原因（`ArtifactTable.page` 不传会落到 `artifactPage` 的兜底默认）。
 *
 * ## 两个运行期 `hide` 的人工定值（草稿标了「不是字面量」）
 *
 *  `myorderlist.js` L776/L917 的 `hide: remarkLocation == 'Y' ? … : …` 与 L859 的
 *  `hide: isViewWW == 'Y' ? … : …`，两个变量都来自**本页隐藏域**（HTML L240-241
 *  `value="N"` / `value="N"`）→ 定值后可见列集合 = 勾选列 + 17 列，
 *  **与对标渲染结果逐列等宽同序**（实测 1650 视口：31 / 181 / 121 / 61 / 76 / 261 / 163 / 66 /
 *  81 / 71 / 71 / 71 / 81 / 81 / 81(空文案) / 71 / 81）。
 *
 * ## 1 个 i18n key 在对标字典里**不存在**：`i18nDataList.property`
 *
 * `myorderlist.js` **L917** 的列 `title: i18nDataList.property`，而该文件
 * `i18nData.messages['zh-CN'].js` 里**没有** `property` 这个键（实测：`undefined`）→ 对标自己
 * 渲染出的表头也是**空文案**（实测该列 `th` 文本为 `''`，宽 81）。所以本页该列 `title: ''`，
 * **不编中文**（`impl-brief.md` §「解不出来的 key 不要自己编中文」）。
 *
 * ## 不复刻的对标缺陷（已登记）
 *
 *  - L776 的 `minwidth: 90`（小写 w）是无效属性 —— 不抄（该列本来就 `hide:true`，不可见）。
 *  - L781 的 `nresize: false` 拼写错（只有 `unresize` 生效）—— 不抄。
 *  - L797 的 `unresize:false`/`hide:false`（以及各列 `sort:false`/`unresize:false`）都是 layui 默认值 —— 不写。
 *
 * ## 本轮单元格复刻：与对标的**已知差异**（2026-09-21，逐条登记，不静默改）
 *
 * 1. **订单号列的徽标只渲染了加急那一颗**（2026-09-21 更新：`/my-orders/` 载荷已补
 *  `is_urgent`）：对标 `orderIdView` 的 templet（L738-771）按字段拼徽标，
 *    我们渲染加急 `isJiaJi`（`MyOrderItem.is_urgent` → `#FFBBFF`「急」，标记逐字取 L753）。
 *    **仍缺字段、不渲染的**（`/my-orders/` 载荷里没有这些字段）——标记 `levelMark`、
 *    新 `isRead`、陪玩 `customizeTag`、拆单 `isSplitOrder`、排团 `taskId`。
 *    要补必须先让后端序列化器回字段——**不是**前端能凭空造的。
 * 2. **订单截图两颗按钮各自开一个弹层**（图片存储线 Task 8，2026-09-25，**不再是「尚未开放」**）：
 *    层参数逐项取本页自己的 `myorderlist.js` L1432-1460（PC 分支），见 `openScreenshotLayer`。
 *    另有两处**不可见属性**与标的不同
 *    （对标 `<button … lay-submit="">` 且不写 `type`，落在本页的 `<form class="layui-form">` 里会
 *    触发原生提交与日期框校验）：去掉 `lay-submit=""`、补 `type="button"`，逐条见 `CELL_TEMPLETS[14]`。
 * 3. **状态文案用后端词表**（`@/config/orderStatus` 的「待抢单」）**而不是对标的「待上号」**
 *    （对标 L815-845 的 `switch` 是按**中文文案** case 的）——业务方裁定：同一状态在**任何页面同一文案**。
 *    **色值仍逐条取本页对标的 switch**（`STATUS_BADGE_COLORS`，**按枚举**），
 *    不与 `order-orderlist.ts` 的色表合并（FRONTEND-REPLICA-STANDARD §10.3）。
 * 4. **`#total` 统计句里的「待上号」改成「待抢单」**（`renderTotal`）：对标那一行（L941-944 的
 *    `dataTotal*`）写的是「待上号」，我们改词是**有意的对标偏差**——统计句与状态徽标必须同词。
 * 5. **`.orderdetails` / `.setdlsta` 的点击已接上**（修复轮 1，原先「看着能点、点了没反应」）：
 *    订单号蓝链 `.orderdetails` → **T5 起改成原地开层**（`layer.open` 装我们自己的复刻页，
 *    参数逐项取**本页自己的** `myorderlist.js` L1324-1351，见第 10 条）；状态徽标 `.setdlsta` →
 *    `notOpen('更改订单状态')`——本页**没有**状态弹窗（真正的改状态在管理订单页），
 *    **不置灰、不伪造请求**（D-0025 §5）。
 * 6. **证据件筛选下拉的「待上号」改成「待抢单」**（`public/replica/order-myorderlist.html` 的
 *    `select[name=orderSta]` 第一档，**文案与 value 都改**）：与状态文案单一来源一致
 *    （业务方要求「同一状态在任何页面同一文案」）。本页 5 个 `select[xm-select]` 都**没接任何
 *    筛选逻辑**（没有任何代码读它们），所以 value 改动**是惰性的**；这是对证据件的**有意偏差**，
 *    不静默改。
 * 7. **两条可见 `<a>` 补 `href`**（订单号蓝链 `.orderdetails` 与状态徽标 `.setdlsta`）：对标
 *    `myorderlist.js` **L764** 的 `href="javascript:void(0);"`（**带尾分号**）与 **L840** 的
 *    `href="javascript:void(0)"`（**无尾分号**）逐字照抄。vendored `layui.css` 只有
 *    `a{color:#333;text-decoration:none}` 这种裸规则、**没有** `cursor:pointer`，页内 `.lableb`
 *    样式块也没有 ⇒ 少了 `href` 这两处没有手型光标、键盘也 focus 不到（看着不像能点）。
 * 8. **对标的右键菜单与悬停预览我们都没实现**：对标给 `.orderLine` 挂了
 *    `$.contextMenu({selector:'.orderLine', …})`（`myorderlist.js` L1614，items
 *    `viewDetails` / `copyOrderID` / `viewPic`，读容器的 `data-id`）与
 *    `$(document).on("mouseover", '.orderLine', …)` 的悬停预览 ajax（L1295），本页两者都没有。
 *    而我们照抄了对标的 tooltip：容器 `title="鼠标右键看看~"`（zh-CN `orderLine`，L386）与链接
 *    `title="点击查看订单详情，也可以鼠标右键试试看"`（`orderIdViewTit`，L385）——按原样右键只会
 *    弹浏览器原生菜单，那句承诺就是假的。所以这里拦住 `.orderLine` 上的右键并提示
 *    「右键菜单」尚未开放（D-0025 §5：照对标渲染 + 点了提示「尚未开放」、不置灰、不伪造写入），
 *    悬停预览则**不做**（后端也没有那类端点）。
 *    ⚠️ 只命中 `.orderLine` 时才 `preventDefault`；页面别处的右键保持浏览器原生菜单。
 * 9. **订单 id 位一律填 `order_no`**（**对标字段缺失**的登记偏差，修复轮 3 更正）：对标把
 *    `res.orderId`（**订单主键**）装进容器 `id` / `data-id` / 复制 span 的 `id`，以及状态徽标
 *    与订单截图两颗按钮各自的 `data-id`（共 3 处 `data-id`），把 `res.orderIdView`（展示单号）装进
 *    `data-orderIdView` / 复制文本 / 链接 `data-viewId`。我们的 `/my-orders/` 载荷**没有订单主键**：
 *    它的 `id` 是**绩效行主键**（`Performance.id`，`api/myOrders.ts:46-48` 明确写「**不要**拿它
 *    当订单标识」）⇒ **不拿绩效主键冒充订单 id**，上面每个订单 id 位都写 `order_no`
 *    （对标那几个字段我们确实没有，不是改口径）。`data-order` 仍是我们自己的点击委托钩子
 *    （对标没有这个属性），值同样是 `order_no`。
 * 10. ⭐ **点订单号 ≡ 原地开层（T5），不再是 vue-router 跳转**：对标出处是**本页自己的**
 *    `myorderlist.js` **L1319-1351**（`.orderdetails` 委托 L1319-1323 → `viewOrderDetails(orderid, viewID)`
 *    L1324-1351）。**PC 分支**（`isPc === true`，L1325-1335）逐项照抄：
 *    `title: viewID + i18nDataList.viewOrderDetails`（zh-CN 的 `viewOrderDetails` = **「订单详情」**，
 *    同文件 L456；`viewID` = 链接的 `data-viewId`，我们填的是 `order_no`，见第 9 条）、
 *    `shade:[0.3,'#333']`、`shadeClose:true`、**`maxmin:false`**、`type:2`、
 *    `skin:'layui-layer-lan'`、`anim:8`、`area:['85%','92%']`。
 *    ⛔ **`area` 是每页自己的值，不许抽共用常量**：同一个产品里订单管理页 `85%×92%`、
 *    我的绩效 `60%×92%`、绩效账单 `1125px|80%×90%`、绩效管理页 `90%×92%`。
 *    ⛔ **手机分支（`isPc === false`）我们有意不接**（如实登记）：对标那一支是
 *    `maxmin:true` + `area:['95%','95%']` + `layer.full(fulllayer)`（L1336-1348），
 *    而 `isPc` 来自 `layui.device().android/ios`（L674-677，**读码核实**：`var devices =
 *    layui.device(); var isPc = true; if (devices.android===true|| devices.ios===true) isPc = false;`）。
 *    **不是接不了**——vendored layui 2.13.9 里 `device()` 是核心原型方法（实测存在）；
 *    是本项目的既定口径：`FRONTEND-REPLICA-STANDARD.md` §1 的适用范围只有「租户后台（桌面）」，
 *    且同页/同批页面早就按桌面分支取定值（`order-orderlist.ts` 的列宽注释写「我们只做桌面后台
 *    → 取桌面分支」、`emp-billsummary.ts` / `emp-mybillsummary.ts` 的 `isPc ? … : …` 同理）。
 *    ⚠️ **只给「层」这一处补手机分支会更糟**：本页的列宽等其余 `isPc` 分支仍是桌面值
 *    ⇒ 会造出「层是全屏的、列是桌面的」半吊子，两侧都不对标。
 *    `content` 换成**我们自己的**复刻页（基址 `import.meta.env.BASE_URL`、单号 `encodeURIComponent`）；
 *    对标那一行是 `content:'OrderView?orderId=' + orderid`——注意它**没有** `viewType` / `tid`
 *    （与 `OrderList20191212.js` 那一页不同），所以本页也没有要对齐的额外参数。
 *    ⛔ **开层前的单号形状闸** `/^\d{20}$/` + 固定文案提示；`title` 前缀过 `escapeHtml`
 *    （理由与 T4 绩效页同口径：`layer.open` 的 `title` 是 HTML 上下文，而 `dataset` 会把
 *    attribute 实体解码回 `<`/`>`）。详见 `openOrderDetailLayer` 的注释。
 *    ⚠️ **一处如实登记的偏离**：对标从**顶层窗口**开层（盖住顶栏整屏）；我们开在 island iframe 内
 *    （只盖内容区）——本页跑在 iframe 里、Vue 外壳没有 layui。
 *
 * 11. **工具栏有一颗运行期注入的「同步订单状态」**，对标那一排没有这颗。
 *    无勾选时走 `POST /api/business/member/jidanpu-sync-now/`（空体，只作用当前登录人那一行）。
 *    成功信封的 `msg` 是空串，页面自拟「同步完成，本次更新 N 笔订单」。
 *    按钮注入的是模板不是渲染结果：`table.reload()` 会重渲整张表。
 *    前端不自己做节流：间隔、在途互斥、失败退避都在后端，被挡时原样显示那句人话。
 *    ② 「发到我的管理系统」已接真：勾选的行走 `pushMemberJidanpuNow(nos,'record')`。
 *    「同步订单状态」有勾选推状态，无勾选仍拉一轮。
 *    工具栏是 6 颗：5 颗真功能 + 1 颗「尚未开放」（只剩 `checkOrderByLocal`）。
 *    成功提示在有跳过 / 失败时缀上后端那句原因；推送在途再点只提示「正在推送，请稍候」。
 *
 * 12. ⭐ **业务方裁定的整页改造**（原始要求：「目前页面的功能都没实装，我先把不要的
 *    功能去掉，其他的能实装就实装」）——五件事，**都是有意的对标偏差**，逐条登记：
 *
 *    **(a) 删 8 个筛选控件 + 2 颗工具栏按钮**（证据件里已删，见该文件头）：筛选 `gameArea`（大区/平台）、
 *    `gameServer`（服务器）、`gameJob`（职业）、`gameGroup`（阵营门派）、`bussType`（订单类型）、
 *    `key4`（搜索3）、`gameAcc`（游戏账号）、`viewIsNewOrder`（只看新订单）；
 *    工具栏 `setRecMan`（指派订单给打手）、`setJiWei`（设定机位）。
 *    理由：这些在本仓**没有后端支撑**（`my_orders_views.py` 的列表**不读任何业务筛选参数**），
 *    业务方裁定不做。⚠️ 连带两处：① 「搜索1」的 placeholder 从「订单号/角色名/手机号」收窄成
 *    「订单号」（我们只有 `order_no`；留着原名会承诺一个搜不出来的东西）；②
 *    `TOOLBAR_NOT_OPEN_EVENTS` 从 6 个降到 **2 个**（只剩 `sendToMySystem` / `checkOrderByLocal`）。
 *  ⛔ **改准**：② 里那个数字再降一次 —— `sendToMySystem` 已接真（见第 11 条的改准块），
 *    今天只剩 **1 个**（`checkOrderByLocal`）。
 *
 *    **(b) 筛选改成「前端本地筛已取到的全量行」**（`applyFilters`）：游戏区服 / 订单状态 /
 *    接单日期 / 搜索1（单号）/ 搜索2（代练项目）/ 加急单。⛔ **不改后端**——本页本来就由
 *    `fetchAllMyOrders()` 一次取完、分页也在前端做（见文件头「为什么取数不交给 layui 的 url」），
 *    所以本地筛选与既有架构同源，不是另开一套取数。
 *    ⚠️ 区服下拉的 `<option>` 在挂载时**按我们自己数据里的 `game_name` 重建**（`rebuildGameOptions`）：
 *    对标那些 option 的 value 是**它自己的游戏 id**（`21701` 这类），我们没有那个 id
 *    （先例：`order-orderlist.ts` 的同一处理，D-0093）。
 *    ⚠️ 日期框默认值按对标的**规则**在挂载时算「今天往前 6 个月 ~ 今天」（`defaultDateRange`），
 *    ⛔ 不抄证据件里那几个**冻结在抓取当日**的字面值。业务方 2026-09-28 明确选了「照对标给近 6 个月」。
 *
 *    **(c) 「更改状态」接真，但只给两个目标**（业务方原话：「只能从代练中更改为待验收或者问题单，
 *    不能随便改动其他状态」）：弹层里只有「待验收」「问题单」两档，且只对**代练中**的单开放
 *    （`requestChangeStatus` 先逐行判 `status === IN_PROGRESS`，否则一句固定文案）。
 *    - 「待验收」复用**既有**的 `POST /orders/<no>/submit/`（就是行内那颗「提交完成」，同一权限点）；
 *    - 「问题单」走 `POST /orders/<no>/report-issue/`，带必填原因；两档共用 `order.submit_work`。
 *    ⚠️ **按节点渲染**（`order.submit_work`，没有就不画这颗按钮）：后端那两个端点都要这个节点，
 *    摆一颗点了必 403 的按钮违反本仓既定口径（D-0154 / E-0098，先例「订单截图」两颗）。
 *    ⚠️ **对标那一页是 `SetOrderSta` iframe（13 个状态随便选）**，我们**有意**做成受限版：
 *    层参数（title「更改订单状态」/ `area ['80%','240px']` / `skin layui-layer-lan` / `anim 5` /
 *    `shade [0.3,'#333']` / `shadeClose`）逐条取本页 `myorderlist.js:1356-1366`，
 *    但**内容不是那个 iframe**（对标那种「任意状态」我们既做不出也不该做）。
 *    驱动形状照对标：工具栏那颗对**勾选的行**（未勾选时提示「请选中要操作的记录」，i18n `setRecMan`），
 *    行内状态徽标 `.setdlsta` 对**那一行**。
 *
 *    **(d) 「导出订单」= 前端直接生成 CSV**（业务方 2026-09-28 选定；对标那一支是
 *    `MyExportOrder` iframe 弹层 + 服务端 xlsx，⛔ 我们**没有**那个端点，且对标的
 *    `my_orders.export`(126) 在 `apps/rbac/wiring.py` 里仍登记为「功能未实现」）：
 *    勾了行就导勾选的行、没勾就导**当前筛出来的全部**；列 = 本页**真有值**的那几列
 *    （订单号 / 游戏名称 / 代练-陪玩内容 / 订单状态 / 接手时间 / 加急，见 `CSV_COLUMNS`）。
 *    ⚠️ **三处如实登记**：① 格式是 **CSV**（不是对标的 xlsx，也不弹「导出选项」层）；
 *    ② 带 UTF-8 BOM（Excel 打开中文不乱码）并做**公式注入**前缀转义；
 *    ③ **不消费** `my_orders.export` 这个节点——它是纯前端动作、不会 403，且导出的数据
 *    在这个页面上本来就已经看得见（没有越权面）；代价是「管理员刻意不给某人该节点」时
 *    他仍能导出，这一条留着待业务方裁。
 *
 *    **(e) 仍未接的 2 颗**：`sendToMySystem` / `checkOrderByLocal`——后端确实没有对应端点
 *    （`.tools/audit-frontend-vs-backend.md` 表 B），照 D-0025 §5 提示「尚未开放」、不置灰。
 *    ⛔ **改准（2026-10-23，计划 §2/§4）**：`sendToMySystem` **已接真**（`POST /member/jidanpu-push-now/`，
 *    `action: 'record'`，只对勾选的行）；原句里的「2 颗」今天只剩 **1 颗**（`checkOrderByLocal`）。
 *
 * ## PII（红线）
 *
 *  `#company` 隐藏域（HTML L242）服务端渲染的是**租户名**，已置空（多租户数据绝不入库）；
 *  `#total` 的「…均由{company}负责运营…」按 `myorderlist.js` L947 的原式读该隐藏域的值渲染
 *  （⇒ 我们这里渲染成「均由负责运营」，不是编文案，是同一段代码在 PII 清空后的输出）。
 *  静态游戏枚举（15 个 `<option>`）保留。
 *
 * ## 多选下拉（`xm-select`）用共享 vendor 入口
 *
 * 本页 1 个 `select[xm-select]`（`orderSta`）在对标里由
 * layui 的 `formSelects` 模块渲染；我们用 `@/layui/vendor` 的 `initFormSelects()`（与
 * `pages/system-user-list.ts` / `pages/order-orderlist.ts` 等复刻页同一入口）。
 * ⚠️ 原句写「5 个（`gameArea`/`gameServer`/`gameJob`/`gameGroup`/`orderSta`）」**已作废**
 * （原文按 §4 保留在此行）：前四个已在 2026-09-28 的删减里去掉了（文件头 12-(a)）。
 *
 * ## 本轮：接上**已实现**的后端
 *
 * 本页原来走 `artifactPage` 的声明式渲染（恒空表、所有点击都提示「尚未开放」）。本轮改成
 * **手写渲染器**（与 `order-orderlist.ts` 同一形状：取证据件 → `root.innerHTML` →
 * `bootstrapIsland` → 自己 `table.render`），因为 `artifactPage` 有两处硬限制：
 *  1. 它的 `buildCol` 对没写 `templet` 的列一律填 `() => ''`（空表下看不出），且
 *     `ArtifactCol.templet` 只接受**字符串选择器**——真数据下所有单元格都会是空的；
 *  2. 它的捕获阶段点击收口会把一切按钮都变成「尚未开放」，真动作没有落点。
 * 列声明仍然是**同一份** `COLS` / `TABLE`（防漂移测试与 parity 都读它），运行期列由 `buildCols()`
 * 从它派生，只补 `templet`（单元格渲染）与 1 处 `field`（排序用）。
 *
 * | 对标落点 | 我们的接线 |
 * |---|---|
 * | 主表取数（`/OrderAjax/GetMyOrderList`，L732 的 `table.render` url） | `GET /api/business/my-orders/`（`api/myOrders.ts` 的 `fetchAllMyOrders`） |
 * | 工具栏「刷新列表」`refresh` | 重新发上面那条 GET（`reload()`）——纯客户端刷新，不涉及后端能力 |
 * | 行内「提交完成」`submtEnd`（L1198） | `POST /api/business/orders/<order_no>/submit/`（`submitMyOrder`） |
 * | 进订单详情（单号蓝链 `.orderdetails`，L738-771） | **原地开层**（T5）：`layer.open` 装我们自己的复刻页（`order-orderview`），本页自己的 `85%×92%`——见「已知差异 10」与 `openOrderDetailLayer`。⛔ 不再走 `ctx.onNavigate` |
 * | 改状态（状态徽标 `.setdlsta`，L815-845） | ⭐ **接真（2026-09-28，受限两档）**：与工具栏「更改状态」**同一个**处理器 `requestChangeStatus`（只允许「代练中 → 待验收 / 问题单」）——见文件头 12-(c)。⛔ 原句「**仍「尚未开放」**：本页没有状态弹窗…」已作废（原文按 §4 保留在此处） |
 * | 工具栏「更改状态」`changeStaBtn`（L1072） | **接真**：对勾选的行开受限弹层；「待验收」走既有 `/submit/`、「问题单」走新端点 `/report-issue/`——见文件头 12-(c) |
 * | 工具栏「导出订单」`exportBtn`（L1143） | **接真**：前端生成 CSV（勾了行导勾选的、没勾导当前筛出来的）——见文件头 12-(d) |
 * | 行内「订单截图」两颗按钮（L899-906） | **原地开层**（图片存储线 Task 8）：`layer.open` 装我们自己的两个岛页（`order-screenshots-upload` / `order-screenshots-manage`），参数逐项取本页自己的 `myorderlist.js` L1432-1460 —— 见 `openScreenshotLayer` 与文件头第 2 条 |
 *
 * **仍「尚未开放」的 2 颗工具栏按钮**（`sendToMySystem` / `checkOrderByLocal`；后端没有对应端点，
 * 逐条见工具栏处理器上方的注释）——点了仍提示「尚未开放」，不置灰、不隐藏（D-0025 §5）。
 * ⛔ 原句「**仍「尚未开放」的 6 颗工具栏按钮 + 1 颗搜索**」已作废（原文按 §4 保留在此处）：
 * `setRecMan` / `setJiWei` 连按钮一起删了，`changeStaBtn` / `exportBtn` 接了真，搜索也接了真
 * （见文件头 12-(a)/(b)）。
 * ⛔ **改准（2026-10-23，计划 §4）**：上面那句里的 `sendToMySystem` 也**接了真**
 * （工具栏「发到我的管理系统」⇒ `doPushToMySystem('record', 勾选的行)`）
 * ⇒ 今天只有 **1 颗**仍「尚未开放」（`checkOrderByLocal`）。
 * 另：工具栏「同步订单状态」变成**双档**（有勾选 ⇒ 推状态；无勾选 ⇒ 仍是原来的「拉一轮」）。
 *
 * ### 为什么「取数」不交给 layui 的 `url`
 *
 * layui 的 `table.render({url})` 自己发 XHR，**绕开 `api/http.ts`**：后端 403（无节点
 * `my_orders.send_to_my_system`）
 * 会变成「表格空了、什么都不说」——正是简报禁止的「点了没反应」。这里改由 `fetchAllMyOrders()`
 * 走 `request()`，403/业务码的 `msg` 能**原样**弹出来（`layer.msg(msg, {time:6000})`，
 * 与对标错误分支的 6000ms 同值）。代价：分页在**客户端**做（`TABLE.page` 的 limit/limits 照旧生效），
 * 数据一次取完（`MY_ORDERS_PAGE_SIZE = 200`；后端分页上限现为 300，见 `api/myOrders.ts`）。
 *
 * ### 「提交完成」的判据**不在前端复刻**
 *
 * 后端 `order_views.py` 的 `OrderViewSet.submit` 的真实规则（403 不是代练员 / 403 不是本人 / 200+code1002 来源态不对 /
 * `code=1000/1002` 金额算不出来）**逐条不在前端重写**——页面把请求发出去，失败就把后端的 `msg`
 * 原样显示。前端的唯一闸门是「同一个订单不并发重复提交」（`submitting`）。
 */
import { fetchMe } from '@/api/auth'
import { pushMemberJidanpuNow, syncMemberJidanpuNow } from '@/api/jidanpuMember'
import {
  ISSUE_REASON_MAX_LENGTH, ISSUE_REASON_REQUIRED_TEXT, fetchAllMyOrders,
  reportIssueOrder, submitMyOrder, type MyOrderItem,
} from '@/api/myOrders'
import { ORDER_STATUS_LABELS, type OrderStatus } from '@/config/orderStatus'
import type { ArtifactCol, ArtifactTable } from '@/layui/artifactPage'
import { escapeHtml } from '@/layui-island/escape'
import { canNode, type NodeViewerInput } from '@/layui-island/perms'
import {
  bootstrapIsland,
  initIslandElements,
  islandMsg,
  type IslandContext,
  type IslandPage,
} from '@/layui/island'
import type { LayuiNamespace } from '@/layui/loader'
import { initFormSelects } from '@/layui/vendor'

/** 证据件（页内 `<style>` + body 标记，零转义）。 */
const ARTIFACT_URL = 'replica/order-myorderlist.html'

/** 对标服务端渲染的 `<title>`（`Order__myorderlist.html` L5）。 */
const TITLE = '我的订单'

/** 对标 `page.limits`（`myorderlist.js` L930）。 */
export const PAGE_LIMITS = [10, 20, 30, 40, 50]
/** 对标 `page.limit`（`myorderlist.js` L929）。 */
export const PAGE_LIMIT = 20

/** 表格 `elem`（`myorderlist.js` L731 的 `elem: '#tableList'`）。 */
const TABLE_ELEM = '#tableList'

// ── 「订单截图」列的两颗按钮（图片存储线 Task 8）────────────────────────────
// ⚠️ **E-0061 陷阱①：类名与选择器分成两个常量**（写成 `class="${SELECTOR}"` 会造出
// `class=".upImg"`、永远匹配不到，而「点了没反应」是**静默**的）。
/** 对标的类名（`myorderlist.js` L902）。 */
export const SHOT_UPLOAD_CLASS = 'upImg'
/** 对标的类名（`myorderlist.js` L903）。 */
export const SHOT_MANAGE_CLASS = 'mangeImg'
/** 行内点击委托的选择器（由上面两个类名拼出，⛔ 不许另抄一份字面量）。 */
const SHOT_BUTTONS = `button.${SHOT_UPLOAD_CLASS}, button.${SHOT_MANAGE_CLASS}`
/** 两个弹层的 island 页 key。 */
export const SHOT_UPLOAD_PAGE = 'order-screenshots-upload'
export const SHOT_MANAGE_PAGE = 'order-screenshots-manage'

/**
 * 当前用户能不能用「订单截图」两颗按钮（节点 `order.view_screenshot`(32) / `order.upload_screenshot`(33)）。
 *
 * 实测：这两颗按钮原来无条件渲染，而内置「代练员」
 * 的预设模块里**没有 `order` 栏目**（`apps/rbac/seed.py::BUILTIN_ROLE_MODULES`）⇒ 它点
 * 「查看截图」会打 `GET /api/business/orders/<20 位单号>/screenshots` 吃 **403**
 * （职位对拍的 `findings` 当场红，见 `docs/specs/2026-09-25-write-mode-audit-design.md` §9）。
 * 本仓的既定口径是「**没权限的不渲染**」（先例：首页快捷操作 `index-console.ts::visibleShortcuts`，
 * 业务方 S2/S3 裁定），且判据②「看得见 ⇒ 必须用得了」不允许把一颗点了必 403 的按钮摆在页面上。
 *
 * ⛔ **默认 `false`（fail-closed）**：身份取不到（401/网络失败）时**不渲染**——渲染出来又点不动
 * 才是缺陷；隐藏一颗用不了的控件不会放开任何东西。
 */
let canViewScreenshots = false
let canUploadScreenshots = false

/**
 * 「更改状态」那颗工具栏按钮的节点门。
 *
 * 后端那两个端点（`/submit/` 与 `/report-issue/`）的端点门**都是** `order.submit_work`
 * （后者由本线的 `ORDER_PERMISSIONS["report_issue"]` 声明），所以「有没有这个节点」就等于
 * 「这颗按钮点了会不会 403」。⛔ **默认 `false`（fail-closed）**：身份取不到时**不画**这颗按钮
 * ——渲染出来又点不动才是缺陷（先例与完整理由见 `canViewScreenshots` 上面那段）。
 */
let canChangeStatus = false

/** 「更改状态」依赖的节点 code（字面量；与后端 `ORDER_PERMISSIONS` 的两条同值）。 */
export const CHANGE_STATUS_NODE = 'order.submit_work'

/**
 * 按 `/api/auth/me` 的载荷设定本页三个按钮级开关（截图两颗 + 更改状态）。
 * 导出给**测试**用（页面挂载时自己也会覆盖）；⛔ 业务代码里除 `mountMyOrderListPage` 外不要调它。
 *
 * ⚠️ 原名 `setScreenshotPermissions`——2026-09-28 加了第三个开关后**改名**
 * （名字里只剩「截图」会与它实际管的事不符）。
 */
export function setMyOrdersPermissions(me: NodeViewerInput | undefined): void {
  const viewer: NodeViewerInput = me ?? {}
  canViewScreenshots = canNode(viewer, 'order.view_screenshot')
  canUploadScreenshots = canNode(viewer, 'order.upload_screenshot')
  canChangeStatus = canNode(viewer, CHANGE_STATUS_NODE)
}

/**
 * 把工具栏**模板**里当前用户用不了的那颗按钮摘掉（fail-closed）。
 *
 * ⚠️ 与 `injectSyncToolbarButton` 同一个手法、同一个理由：改的是
 * `#toolbarDemo` 那段 `<script type="text/html">`，**不是**渲染出来的 `.layui-table-tool`——
 * layui 是编译模板再渲染的，`table.reload()` 会重渲整张表，塞进渲染结果的节点下次就没了。
 * ⚠️ 解析成 DOM 再删（⛔ 不做字符串替换）：字符串锚点一旦与证据件漂移就是**静默**不生效。
 * ⛔ **必须在 `table.render` 之前调**（layui 到那时才编译模板）。
 *
 * 今天只有一颗受它管（`changeStaBtn`）。⚠️ 截图那两颗**不在这里**：它们由
 * `CELL_TEMPLETS[14]` 按行渲染，本来就是「没权限就不画」。
 */
export function applyToolbarPermissionGates(root: HTMLElement): void {
  if (canChangeStatus) return
  const template = root.querySelector<HTMLScriptElement>('#toolbarDemo')
  if (template === null) return
  const holder = document.createElement('div')
  holder.innerHTML = template.textContent ?? ''
  for (const btn of Array.from(holder.querySelectorAll(`[lay-event="${CHANGE_STATUS_EVENT}"]`))) {
    btn.remove()
  }
  template.textContent = holder.innerHTML
}
/**
 * `layui.table` 的实例 id。`mountArtifact` 那版没传 `id`，layui 从 `elem` 里推；这里显式写同名值，
 * `table.reload` 的第一参要的就是它（传 `#tableList` 是**静默 no-op**，见 `system-sourelist.ts` 的注释）。
 */
const TABLE_ID = 'tableList'

/**
 * 工具栏里**真的「尚未开放」**的事件名（逐字取证据件 `#toolbarDemo` 的 `lay-event`）。
 * 用途见 `toolbar()` 处理器：**只有这几个**会弹提示，
 * 其余事件（layui 内置的 `LAYTABLE_COLS` 等）静默放行。
 *
 * ⚠️ **2026-09-28 从 6 个降到 2 个**（业务方裁定，见文件头第 12 条）：
 * `setRecMan`（指派订单给打手）与 `setJiWei`（设定机位）**连按钮带事件一起删掉**
 * （本仓没有这两件事的后端能力，业务方裁定不做）；`changeStaBtn`（更改状态）与
 * `exportBtn`（导出订单）**接了真**（各自见 `requestChangeStatus` / `exportOrders`）。
 *
 * ⭐ **2026-10-23 从 2 个降到 1 个**（计划 `2026-10-23-taker-my-orders-push.md` §4）：
 * `sendToMySystem`（发到我的管理系统）**接了真** —— 对勾选的行调
 * `POST /api/business/member/jidanpu-push-now/`（`action: 'record'`，见 `doPushToMySystem`）。
 * ⇒ 只剩 `checkOrderByLocal`（检查发送失败订单）仍是「尚未开放」（后端确实没有对应端点）。
 * ⛔ 别再往这张表里加「其实有落点」的事件——它一旦命中就会把真功能说成「尚未开放」。
 *
 * ⚠️ 对标 `myorderlist.js` 里那个 toolbar `switch (obj.event)` **没有 default 分支**
 *（只列了 `refresh` 与 6 个应用事件；行内的 `submtEnd` 在另一个 handler 里），
 * 所以点右上角「筛选列」（`defaultToolbar:['filter']` 渲染出来的内置图标，
 * 事件名 `LAYTABLE_COLS`，真实 DOM 见 `.tools/parity.mjs:255`）在对标上是**静默开面板**。
 * 我们若用 `else` 兜底弹「尚未开放」，就会把 layui 自己的功能说成未开放——2026-09-21 评审抓到的
 * 「emp-myemp 同形缺陷」同款，这里一并收口。
 */
export const TOOLBAR_NOT_OPEN_EVENTS: readonly string[] = ['checkOrderByLocal']

/**
 * 对标工具栏那颗「发到我的管理系统」的 `lay-event`（证据件 `#toolbarDemo` **逐字**）。
 *
 * ⚠️ 与 `SYNC_TOOLBAR_EVENT` 同一条纪律：**标记**（证据件里那个 `lay-event`）与**处理器**
 * 必须共用一个常量，⛔ 两边各抄一份字面值（那就是「按钮点了没反应」的静默失败）。
 * ⚠️ 它**不在** `TOOLBAR_NOT_OPEN_EVENTS` 里（2026-10-23 起已接真）。
 */
export const SEND_TO_MY_SYSTEM_EVENT = 'sendToMySystem'

/* ── ⭐ 工具栏「同步订单状态」（下游鸡蛋铺线 Task 9，spec §9.3；业务方原话）─────────────
 *
 * 位置：本页工具栏（对标那一排按钮里本来就有「发到我的管理系统」，形状有依据）。
 *
 * ⭐ **2026-10-23 起是一颗「双档」按钮**（计划 `2026-10-23-taker-my-orders-push.md` §4）：
 * - **有勾选** ⇒ 把勾选的行**推过去**（`pushMemberJidanpuNow(nos, 'status')`）；
 * - **无勾选** ⇒ 保留原行为：**往下拉一轮**（`syncMemberJidanpuNow`，从下家把已完成拉成待验收）。
 * ⇒ 两种方向（往外推 / 往里拉）**同住一颗按钮**，由「有没有勾选」分流（业务方口径）。
 * ⚠️ 标题文案同步改成两条都说，否则旧文案（只说「同步回来」）在勾选之后就是一句假话。
 *
 * ⚠️ **E-0061 陷阱①：类名与选择器分成两个常量**（`class="${SELECTOR}"` 会造出 `class=".x"`、
 * 永远匹配不到，而症状是「点了没反应」这种**静默**失败）。
 * ⚠️ 图标用 **stock** 的 `&#xe669;`（与证据件里「刷新列表」那颗同一个字形）——
 * 对标其余按钮用的 `larry-*` 是它自己的 iconfont，我们没有（`FRONTEND-REPLICA-STANDARD.md` §7
 * 第一行已登记）。
 */

/** 新按钮的类名（⚠️ **不带点**）。 */
export const SYNC_TOOLBAR_BUTTON_CLASS = 'syncOrderStatusBtn'
/** 新按钮的文案（业务方 2026-09-26 的原话）。 */
export const SYNC_TOOLBAR_BUTTON_LABEL = '同步订单状态'
/**
 * 新按钮的 `lay-event`（layui 用它把点击交回 `toolbar(tableList)`）。
 *
 * ⚠️ 这个名字同时出现在**标记**与**处理器**两处 ⇒ 只有「一个值、两处用」才接得上。
 * `order-myorderlist.spec.ts` 里有一条**钉字面值**的判据（`toBe('syncOrderStatus')`
 * 与模板里那句 `lay-event="…"`）：⛔ 单独看「用常量查模板」是**自指**的（改名时两边一起变、
 * 照样绿），钉字面值那条才能把它弄红；而两条点击用例是**从标记里读**事件名去驱动的
 * ⇒ 它们抓的是「标记与处理器不一致」那一类（那正是「按钮点了没反应」）。
 */
export const SYNC_TOOLBAR_EVENT = 'syncOrderStatus'

/** 工具栏模板里那颗按钮的**标记**（形状照证据件里同排的 `layui-btn-xs` 按钮）。 */
const SYNC_TOOLBAR_BUTTON_HTML =
  `<button class="layui-btn layui-btn-xs layui-btn-normal ${SYNC_TOOLBAR_BUTTON_CLASS}"` +
  ` lay-event="${SYNC_TOOLBAR_EVENT}" type="button"` +
  // 文案说清**两个方向**：有勾选 ⇒ 把勾选的行推到你绑定的下家；无勾选 ⇒ 把对方状态同步回来。
  ` title="勾选的行推送到你绑定的下家鸡蛋铺；不勾选则把对方订单状态同步回来">` +
  `<i class="layui-icon">&#xe669;</i>${SYNC_TOOLBAR_BUTTON_LABEL}</button>`

/**
 * 把新按钮注入**工具栏模板**（`#toolbarDemo` 那段 `<script type="text/html">`）。
 *
 * ⚠️ **为什么注入模板、而不是往渲染好的 `.layui-table-tool` 里塞 DOM**：
 * layui 是**编译模板**再渲染工具栏的，而 `table.reload()` 会重渲整张表；
 * 塞进渲染结果的节点会在下一次重渲时消失（症状：刷新一次列表后按钮不见了）。
 * 往模板里注入则跟着模板走。
 * ⚠️ 解析成 DOM 再改（⛔ 不做字符串替换）：字符串锚点一旦与证据件漂移就是**静默**不生效。
 * @returns 真的注进去了没有（`false` = 证据件的结构变了，`order-myorderlist.spec.ts` 会当场红）。
 */
export function injectSyncToolbarButton(root: HTMLElement): boolean {
  const template = root.querySelector<HTMLScriptElement>('#toolbarDemo')
  if (template === null) return false
  const holder = document.createElement('div')
  holder.innerHTML = template.textContent ?? ''
  const container = holder.querySelector<HTMLElement>('.layui-btn-container')
  if (container === null) return false
  container.insertAdjacentHTML('beforeend', SYNC_TOOLBAR_BUTTON_HTML)
  template.textContent = holder.innerHTML
  return true
}

/* ── ⭐ 工具栏「更改状态」与「导出订单」（业务方 2026-09-28 裁定；见文件头第 12 条）────────
 *
 * 这两颗对标**本来就有**（`myorderlist.js:1072` 的 `changeStaBtn` / `:1143` 的 `exportBtn`），
 * 本线把它们的落点从「尚未开放」换成真功能：
 *  - `changeStaBtn` → `requestChangeStatus`（**受限两档**：待验收 / 问题单）；
 *  - `exportBtn` → `exportOrders`（前端生成 CSV）。
 * ⛔ 事件名逐字取证据件 `#toolbarDemo` 的 `lay-event`，且与处理器**共用一个常量**
 * （与 `SYNC_TOOLBAR_EVENT` 同一条纪律：两边各抄一份字面值 = 「按钮点了没反应」那种静默失败）。
 */

/** 「更改状态」的 `lay-event`（证据件 `#toolbarDemo` 逐字）。 */
export const CHANGE_STATUS_EVENT = 'changeStaBtn'
/** 「导出订单」的 `lay-event`（证据件 `#toolbarDemo` 逐字）。 */
export const EXPORT_EVENT = 'exportBtn'

/** 未勾选任何行时的提示：逐字取对标 zh-CN i18n 的 `setRecMan`（`myorderlist.js:448`）。 */
export const SELECT_RECORDS_FIRST_TEXT = '请选中要操作的记录'

/**
 * 这次推送提示里**有没有原因可读**（决定提示停留多久：有 ⇒ 6000ms，见 `doPushToMySystem`）。
 *
 * ⚠️ 判据与 `pushResultText` 的后缀**同一处**（两边都调它）⇒ 不会出现「提示里没原因、
 * 却按 6000ms 停」或反过来的漂移。
 */
export function pushResultHasReason(
  skipped: number,
  failed: number,
  reasons: readonly string[],
): boolean {
  return skipped + failed > 0 && reasons.some((reason) => reason !== '')
}

/**
 * 手动推送的结果提示（**自拟**：后端成功信封的 `msg` 是空串，照抄就什么都不显示）。
 *
 * 口径（2026-10-23，计划 §4 + 修复轮 Important-2）：
 * - 主体是「已推送 N 笔，跳过 M 笔」——`skipped` 是**正常的业务结论**（不是错误）；
 * - `failed > 0` 时**追加**一句（⛔ 不许只报「推送 0 笔」而把失败吞掉：那些单已经进了台账
 *   退避重试，但用户此刻必须知道「这一下没全成」）；
 * - **有跳过 / 失败时把原因说出来**：取第一条**不同**的 `reason` 接在后面
 *   （「已推送 0 笔，跳过 2 笔：本线台账里没有这张单的链路」）；有多条不同原因时**只在末尾加
 *   「等」**（⛔ 不把五句话拼成一坨——提示是一闪而过的）。原因来自**后端逐单结果**（`items[].reason`），
 *   ⛔ 前端不自造文案、也不自己数（两套判据必然漂）。
 * ⚠️ 空串原因不进后缀（`done` 那几单的 `reason` 就是空串）。
 */
export function pushResultText(
  pushed: number,
  skipped: number,
  failed: number,
  reasons: readonly string[] = [],
): string {
  const base = `已推送 ${pushed} 笔，跳过 ${skipped} 笔`
  const head = failed > 0 ? `${base}，失败 ${failed} 笔` : base
  if (!pushResultHasReason(skipped, failed, reasons)) return head
  const distinct = Array.from(new Set(reasons.filter((reason) => reason !== '')))
  return `${head}：${distinct[0]}${distinct.length > 1 ? '等' : ''}`
}

/**
 * 手动推送**在途**时再点那一颗的提示（**自拟**；修复轮 Blocking-1 的前端那一半）。
 *
 * ⚠️ 它只是**省一趟明知会被挡的请求**：真正的互斥在**后端共享引擎**的串行闸（按绑定）。
 * 被挡时后端回的那句现在是「已排队：另一轮推送正在跑，几秒后会自动推出去」（复评 Minor-3：
 * 不诱导用户重点）——前端**原样透传**它。
 */
export const PUSH_IN_PROGRESS_TEXT = '正在推送，请稍候'

/** 弹层标题：逐字取对标 zh-CN i18n 的 `changeOrderSta`（`myorderlist.js:457`）。 */
export const CHANGE_STATUS_DIALOG_TITLE = '更改订单状态'
/**
 * 两个可选目标（**业务方裁定的全部**：「只能从代练中更改为待验收或者问题单」）。
 * ⚠️ 顺序 = 弹层里 radio 的顺序；文案取自 `ORDER_STATUS_LABELS`（后端词表），
 * ⛔ 不在这里另抄一份中文（同一个状态在任何页面同一文案是业务方定的）。
 */
export const CHANGE_STATUS_TARGETS: readonly OrderStatus[] = ['PENDING_REVIEW', 'ISSUE']
/** 弹层的两个按钮文案（对标那个 iframe 里也是「确定/取消」，这里用通用值）。 */
export const CHANGE_STATUS_CONFIRM_BTN = '确定'
export const CHANGE_STATUS_CANCEL_BTN = '取消'
/** 报问题单的成功提示（**自拟**：后端成功信封的 `msg` 是空串，照抄就什么都不显示）。 */
export const REPORT_ISSUE_SUCCESS_TEXT = '已标记为问题单'
/**
 * 「不是代练中的单」时那句提示的**前缀**（**自拟**）。
 * ⚠️ 形状对齐后端 `/submit/` 与 `/report-issue/` 的同类文案，但**前端这一句不替代后端**：
 * 后端那两道判据照旧各判一次（这里只是省一趟明知会被拒的请求，不是复刻业务规则）。
 * 状态值一律走 `statusText()`（后端词表），⛔ 不写裸枚举名。
 */
export const CHANGE_STATUS_REQUIRES_IN_PROGRESS = '只有代练中的单可以更改状态，当前状态：'
/** 「导出」时一行都没有的提示（**自拟**）。 */
export const EXPORT_EMPTY_TEXT = '没有可导出的订单'

/** 错误提示的停留时长：**6000ms**，与对标 `submtEnd` 失败分支（`myorderlist.js` L1217）同值。 */
const ERROR_MSG_MS = 6000

/** 确认文案：逐字取对标 zh-CN i18n（`myorderlist.js` L451 的 `js.submtEnd`）。 */
export const SUBMIT_CONFIRM_TEXT = '确定打完了吗？提交前请再三确认哦'

/**
 * 提交成功提示。⚠️ **不是从对标抄的**：对标成功后弹的是**服务端** `data.msg`
 * （`myorderlist.js` L1219-1221），而我们的 `/submit/` 成功信封 `msg` 是**空串**
 * （`api_response(data=…)` 的默认 `msg=""`）——照抄就什么都不显示。所以这里自拟一句，
 * 口径与 `order-orderlist.ts` 的 `msg('接单价已更新')` 同类。
 */
export const SUBMIT_SUCCESS_TEXT = '提交成功，已送交验收'

/**
 * 表头：逐条照抄 `myorderlist.js` L735-927（20 列，含勾选列）。
 *
 * `hide` 两处运行期表达式的人工定值见文件头注释；`fixed: ''`（L926）是 falsy，
 * 等价于不写（`ArtifactCol.fixed` 只接受 `'left'|'right'`）。
 */
export const COLS: ArtifactCol[] = [
  // L736：`{ type:'checkbox', fixed:'left', width:30 }` —— `type` 必须显式声明，漏了表头勾选框会静默消失。
  { type: 'checkbox', fixed: 'left', width: 30 },
  // L738：`templet` 是函数（订单号 + 各种徽标），空表下不渲染，故只抄 field/title/width。
  { field: 'orderIdView', title: '订单号', width: 180 },
  // L772：`hide: true`（对标自己就藏了；实测 1650 视口可见列里没有它）。
  { field: 'orderId', title: '系统内置单号', width: 150, hide: true },
  { field: 'roleName', title: '角色名', width: 120 },
  // L776：`minwidth:90`（无效）不抄；`remarkLocation == 'Y' ? false : true`，隐藏域值为 N → true。
  { field: 'orderRemark', title: '重要备注', hide: true },
  // L781：`nresize:false` 拼写错，不抄。
  { field: 'gameGroup', title: '阵营/平台', align: 'center', width: 55 },
  { field: 'gameName', title: '游戏名称', align: 'center', width: 75 },
  // L800：`minWidth:260`（大写 W，真属性）——自动列，实测渲染 261px，**不要**换成 width。
  { field: 'project', title: '代练/陪玩内容', minWidth: 260 },
  // L806：既无 width 也无 minWidth → 纯自动列，实测 163px。
  { field: 'gameInfo', title: '角色信息' },
  { field: 'orderSta', title: '订单状态', align: 'center', width: 65 },
  { field: 'orderHoursDescStr', title: '时间', width: 80, align: 'center' },
  // L859：`hide: isViewWW == 'Y' ? false : true`，隐藏域 `#isViewWW` 值为 N → true。
  { field: 'orderSoure', title: '旺旺/来源', width: 90, hide: true, align: 'center' },
  { field: 'recMan', title: '接手人', align: 'center', width: 70 },
  { field: 'recFee', title: '接手价', align: 'center', width: 70 },
  { field: '', title: '订单截图', align: 'center', width: 70 },
  { field: 'recOrderTime', title: '接手时间', width: 80, align: 'center', sort: true },
  { field: 'jiwei', title: '机位', width: 80, align: 'center' },
  // L917：`title: i18nDataList.property` —— 该 key 在对标 zh-CN 字典里**不存在**（实测 undefined），
  // 对标渲染出的表头也是空的（实测 th 文本 ''）→ 留空，不编中文。
  { field: 'orderRemark', title: '', width: 80 },
  { field: 'dynamicRemark', title: '日志', width: 70 },
  // L926：`{ fixed: '', title:'操作', width:80, toolbar:'#baroperating' }` —— `fixed:''` 是 falsy，不写。
  { field: '', title: '操作', width: 80, toolbar: '#baroperating' },
]

/** 表的声明（`elem` / `toolbar` / `page` / `defaultToolbar` 与对标同值）。 */
export const TABLE: ArtifactTable = {
  elem: TABLE_ELEM,
  toolbar: '#toolbarDemo',
  // L733 写 `[]`、L734 又写 `['filter']` —— **后者生效**（表格右上角 1 个「筛选列」图标，实测）。
  defaultToolbar: ['filter'],
  // L928-930 逐字。
  page: { limit: PAGE_LIMIT, limits: PAGE_LIMITS },
  cols: COLS,
}

/**
 * 订单状态 → 中文。**本页的**唯一出处是 `@/config/orderStatus`（后端 `OrderStatus` choices 的镜像）；
 * 未知值原样显示。
 *
 * ⚠️ 只说「本页」：仓库里还有别的自带状态文案表（如 `emp-emplist.ts:334` 的同值副本，属另一条线），
 * 所以「全仓唯一出处」会被一个反例证伪（E-0011），不要那样写。
 */
export function statusText(status: string): string {
  return ORDER_STATUS_LABELS[status as OrderStatus] ?? status
}

/**
 * **本页自己的**状态徽标色表——**按状态枚举**取值（不是按中文文案）。
 *
 * 逐条取本页对标 `myorderlist.js` **L815-845** 的 `switch`（对标的 case 是**中文文案**
 * `i18nDataList.orderStaus2..8`，我们按枚举对齐；文案本身用后端词表，见 `statusText`）：
 * `orderStaus2 待上号→WAITING_GRAB`、`3 代练中→IN_PROGRESS`、`4 待验收→PENDING_REVIEW`、
 * `5 已完成→COMPLETED`、`6 问题单→ISSUE`、`7 已退单→REFUNDED`、`8 已暂停→PAUSED`。
 *
 * ⚠️ **不要与 `order-orderlist.ts` 的 `STATUS_COLORS` 合并**（FRONTEND-REPLICA-STANDARD §10.3）：
 * 两页的对标 JS 各自不同（那一页 15 档、这一页 7 档；同一状态两页色值也不同——
 * 例如「待抢单」在大厅页是 `#D15FE8`、在本页对标里没有这一档）。
 * ⚠️ `UNASSIGNED`（未分配）**不在表里**：对标那个 `switch` 也没有它 → 走 default。
 */
export const STATUS_BADGE_COLORS: Record<string, string> = {
  WAITING_GRAB: '#585858', // 对标 orderStaus2「待上号」那一档（文案我们统一用「待抢单」）
  IN_PROGRESS: '#219227', // orderStaus3「代练中」
  PENDING_REVIEW: '#ff6a00', // orderStaus4「待验收」
  COMPLETED: '#0094ff', // orderStaus5「已完成」
  ISSUE: '#FF5722', // orderStaus6「问题单」
  REFUNDED: '#ff0000', // orderStaus7「已退单」
  PAUSED: '#efaaaa', // orderStaus8「已暂停」
}

/** 徽标色兜底：对标 `switch` 的 `default`，**带尾分号**（照抄；拼进 `style` 后会出现两个分号）。 */
export const STATUS_BADGE_FALLBACK = '#b0aeae;'

/**
 * 单元格渲染：**按下标**（不是按 `field`）对应列，因为 `COLS` 里有两个同名字段
 * （`orderRemark` ×2、`''` ×2），按 `field` 找会串列。
 *
 * ⚠️ **只填后端 `MyOrderSerializer` 真的回了的列**；其余列（角色名 / 阵营 / 角色信息 / 时间 /
 * 旺旺来源 / 机位 / 日志…）在对标那里来自另一个响应，本轮**保持空白**——
 * 不猜、不拿绩效金额冒充「接手价」。
 *
 * ⭐ **2026-10-23 起接手人（下标 12）/ 接手价（下标 13）也有值了**：后端
 * `MyOrderSerializer` 补了 `taker_name` / `taker_price` 两个只读字段（计划 §1），
 * 本文件只负责渲染（`null` ⇒ 空串，值一律 `escapeHtml`）。
 *
 * 对标那 4 处是**函数 templet**（订单号 L738-771、状态 L815-845、订单截图 L899-906、
 * 接手时间 L907-911），本轮按对标补齐；插值一律过 `escapeHtml`。
 */
const CELL_TEMPLETS: Record<number, (d: MyOrderItem) => string> = {
  /**
   * 1 订单号（对标 `orderIdView` 的 templet，L738-771）：`.orderLine` 容器 + 隐藏复制 span +
   * **加急徽标（加急时）** + 蓝链 `.orderdetails`（`#0094ff`，L764）。
   *
   * 加急徽标标记**逐字**取 `myorderlist.js:753`（色值 `#FFBBFF`、title 与文案取同文件
   * zh-CN 字典 L377-378 的 `isJiaJiTit` / `isJiaJi`）；位置照 L763-766 的顺序——
   * **复制 span 之后、订单号蓝链之前**（其余 5 类徽标我们没数据，不占位）。
   *
   * 对标的其余徽标（`levelMark` 标记 / `isRead` 新 / `customizeTag` 陪玩标签 /
   * `isSplitOrder` 拆单 / `taskId` 排团）**不渲染**：`MyOrderItem` 没有这些字段
   * （后端 `MyOrderSerializer` 的 fields 里也没有）→ 无数据不造徽标（见文件头登记）。
   *
   * ⚠️ 对标的映射（`myorderlist.js` L763-766 的链接 / 复制 span / 容器 return）：对标把
   * `res.orderId`（**订单主键**）装进容器 `id` 与 `data-id`、复制 span 的 `id`，把
   * `res.orderIdView`（**展示单号**）装进 `data-orderIdView`、复制文本、链接 `data-viewId`。
   *
   * ⚠️ 我们的 `/my-orders/` 载荷**没有订单主键**：它的 `id` 是**绩效行主键**（`Performance.id`，
   * `api/myOrders.ts:46-48` 明确写「**不要**拿它当订单标识」）⇒ 所有订单 id 位一律填 `order_no`
   * （我们手上那个订单标识）。这是**已登记的偏差**（对标字段缺失，不拿绩效主键冒充订单 id，
   * 见文件头「已知差异」第 9 条）。
   * 另加**本页点击委托用的** `data-order`（对标没有这个属性，值同样是 `order_no`，因为它是对外
   * 标识、被行内点击读）。`order_no` 过 `escapeHtml`。
   *
   * ⭐ **D-0301展示回退**：**展示值 = `source_order_no || order_no`**（来源号
   * 非空取它）——落点：`data-orderIdView` / 复制 span 的 `data-clipboard-text` / 蓝链正文 /
   * `data-viewId`（对标语义本就是「展示单号」，点击委托拿它开层的**标题**）。⛔ 寻址键
   * `id="order_<no>"` / `data-id` / `data-order` **照旧内置号**（右键目标、行内点击、详情
   * 取数都靠它们）。
   */
  1: (d) => {
    const no = escapeHtml(d.order_no)
    // 展示单号：来源号非空取它、空回退内置号（D-0301）。两处都过 escapeHtml（自由文本）。
    const viewNo = escapeHtml(d.source_order_no || d.order_no)
    // 加急徽标：标记逐字取对标 `myorderlist.js:753`。`is_urgent` 是布尔（后端 default=False），
    // 只在 **`true`** 时渲染——不做真值以外的额外口径（文件头登记）。
    const urgent =
      d.is_urgent === true
        ? '<span class="lableb" style="background-color:#FFBBFF"' +
          ' title="加急订单，优先安排">急</span>'
        : ''
    return (
      `<div class="orderLine" id="order_${no}" data-id="${no}" data-orderIdView="${viewNo}"` +
      ` data-order="${no}" title="鼠标右键看看~">` +
      `<span id="orderid_span_${no}" style="display:none" class="orderIDcopy"` +
      ` data-clipboard-text="${viewNo}"></span>` +
      urgent +
      `<a href="javascript:void(0);" class="orderdetails" data-viewId="${viewNo}" data-id="${no}"` +
      ` data-order="${no}" style="color:#0094ff"` +
      ` title="点击查看订单详情，也可以鼠标右键试试看">${viewNo}</a>` +
      `</div>`
    )
  },
  // 2 系统内置单号：我们的 `order_no` 就是系统内置单号（页面头「两列单号规则」同口径）。
  2: (d) => escapeHtml(d.order_no),
  6: (d) => escapeHtml(d.game_name), // 游戏名称
  7: (d) => escapeHtml(d.project_text), // 代练/陪玩内容
  /**
   * 9 订单状态（对标 L815-845）：`<a class="lableb setdlsta" … style="background-color:<色>;">文案</a>`。
   * 文案取 `statusText`（后端词表，「待抢单」而不是对标的「待上号」），**色取本页色表**（按枚举）。
   *
   * ⚠️ 对标那个 `data-id` 装的也是 **`res.orderId`（订单主键）**（`myorderlist.js` L840）——我们
   * 载荷里没有它 ⇒ 同样填 `order_no`（已登记偏差，见文件头第 9 条）。另保留 `data-order`
   * （= `order_no`，与 `order-orderlist.ts` 的行内点击委托同一属性名）供本页的事件委托读；
   * `href` 逐字取对标（**无尾分号**）。
   */
  9: (d) => {
    const no = escapeHtml(d.order_no)
    const color = STATUS_BADGE_COLORS[d.status] ?? STATUS_BADGE_FALLBACK
    return (
      `<a href="javascript:void(0)" class="lableb setdlsta" data-id="${no}" data-order="${no}"` +
      // ⭐ `data-status`（**对标没有这个属性**，是我们自己的钩子，同 `data-order`）：
      // 点这颗徽标会开「更改状态」弹层，而弹层必须先判**这一行**的状态是不是
      // 代练中（业务方裁定只允许「代练中 → 待验收 / 问题单」）。徽标上只有中文文案，
      // 拿文案反查枚举会多一道易漂的映射 ⇒ 直接带上原始枚举值（过 `escapeHtml`）。
      ` data-status="${escapeHtml(d.status)}"` +
      ` title="${no}点击可以更改订单状态" style="background-color:${color};">` +
      `${escapeHtml(statusText(d.status))}</a>`
    )
  },
  /**
   * 12 接手人：后端 `taker_name`（**显示名**，昵称优先）。
   *
   * ⚠️ **下标是 12 不是 11**：`COLS` 的第 0 项是勾选列（`{type:'checkbox'}`，也算一个下标）
   * ⇒ 接手人 = 12、接手价 = 13（11 是隐藏的「旺旺/来源」）。
   * ⚠️ `null` = 这一行还没有归属人 ⇒ **空串**（⛔ 不写「—」之类的自造占位：对标的空值就是空）。
   * ⚠️ 值过 `escapeHtml`：昵称是**用户输入**（`User.first_name`），而 `templet` 的返回值被
   * layui 当 HTML 插入。
   */
  12: (d) => (d.taker_name === null ? '' : escapeHtml(d.taker_name)),
  /**
   * 13 接手价：后端 `taker_price`（覆盖价优先，否则大厅接单价）。
   *
   * ⚠️ 后端回的是**两位小数字符串**（`"88.00"`）或 `null`（两边都没有价）⇒ `null` 渲染空串。
   * ⚠️ ⛔ 不在前端做金额格式化 / 换算 / 补 `¥`：金额的出口只有后端那一处（`money()`），
   * 前端再算一次就会与别的页面漂移。
   */
  13: (d) => (d.taker_price === null ? '' : escapeHtml(d.taker_price)),
  /**
   * 14 订单截图（对标 L899-906）：`.layui-btn-group` 里两颗 `layui-btn-xs layui-btn-normal`
   * 图标按钮（`upImg` / `mangeImg`），`title` 取对标 zh-CN 的 `upImg` / `mangeImg`。
   * 对标两颗的 `data-id` 是 **`res.orderId`（订单主键）**（`myorderlist.js` L902/L903）——我们
   * 载荷里没有它 ⇒ 同样填 `order_no`（已登记偏差，见文件头第 9 条）。
   *
   * ⚠️ 两处**有意与对标不同**（见文件头登记）：① 去掉 `lay-submit=""`（本页表格在
   * `<form class="layui-form">` 内，`lay-submit` 会对日期框跑一遍与本次点击无关的校验）；
   * ② 补 `type="button"`（对标没写，浏览器默认 `submit`，在表单里会触发原生提交）。
   */
  14: (d) => {
    const no = escapeHtml(d.order_no)
    // 权限门：无 `order.upload_screenshot` / `order.view_screenshot`
    //    就**不渲染**对应那颗——否则点它必 403（代练员实测）。见 `setMyOrdersPermissions`。
    const buttons =
      (canUploadScreenshots
        ? `<button type="button" class="layui-btn layui-btn-xs layui-btn-normal upImg"` +
          ` data-id="${no}" title="上传截图"><i class="layui-icon">&#xe681;</i></button>`
        : '') +
      (canViewScreenshots
        ? `<button type="button" class="layui-btn layui-btn-xs layui-btn-normal mangeImg"` +
          ` data-id="${no}" title="查看截图"><i class="layui-icon">&#xe64a;</i></button>`
        : '')
    if (buttons === '') return ''
    return `<div class="layui-btn-group" style="background: #fff;">${buttons}</div>`
  },
  // 15 接手时间（= 绩效行创建时间）：对标 L907-911 是 `<span title="订单接手时间">值</span>`。
  // 值格式化成 `YYYY-MM-DD HH:mm`（与 `order-orderlist.ts` 的 `createRow` 同口径，管理列表亦同），不直接渲染
  // 后端原样的 ISO 串（`2026-09-21T12:07:58+08:00`）——对标渲染的也是格式化后的 `recOrderTime`。
  15: (d) =>
    `<span title="订单接手时间">${escapeHtml(d.created_at.replace('T', ' ').slice(0, 16))}</span>`,
}

/**
 * 有 `sort: true` 的列必须让 layui 能拿到**排序列的值**——它按 `field` 排序，不看 `templet`。
 * 第 15 列（接手时间）的 `field` 是对标的 `recOrderTime`，我们的载荷里叫 `created_at`，
 * 不改的话点排序**看似无反应**（所有行该字段都是 `undefined`）。
 */
const DATA_FIELD_OVERRIDES: Record<number, string> = {
  15: 'created_at',
}

/** 运行期列对象（`layui.table` 的 col）。只列本页用到的键；`templet` 是本页相对 `COLS` 的唯一新增。 */
export type MyOrderCol = {
  type?: string
  field?: string
  title?: string
  width?: number | string
  minWidth?: number | string
  hide?: boolean
  align?: string
  fixed?: string
  sort?: boolean
  unresize?: boolean
  toolbar?: string
  /** **函数形式**：走我们自己的渲染，因此返回值里的用户输入必须转义（见 `escapeHtml`）。 */
  templet: (d: MyOrderItem) => string
}

/**
 * 单元格文本转义。⚠️ `templet` 的返回值被 layui **当 HTML 插入**，而 `project_text` / `game_name`
 * 来自录单输入——不转义就是存储型 XSS（对标自己也没转义，但那不是我们照抄缺陷的理由）。
 *
 * 实现已抽到 `@/layui-island/escape`（整体评审 I-4：来源管理页与录单页也要同一份），
 * 这里**只 re-export**、不再留第二份实现——对外签名（含本文件既有 spec 的 import）不变。
 */
export { escapeHtml } from '../escape'

/**
 * 由 `COLS` 派生运行期列：**顺序/宽度/隐藏/对齐/固定/toolbar 一律照抄 `COLS`**
 * （parity 与 `order-replica.spec.ts` 钉的就是这一份），只补两处：
 *  - `templet`：真数据的单元格渲染（`artifactPage` 那版对每列都填 `() => ''`）；
 *  - 1 处 `field`（见 `DATA_FIELD_OVERRIDES`）。
 */
export function buildCols(): MyOrderCol[] {
  return COLS.map((c, i) => {
    const col: MyOrderCol = { templet: CELL_TEMPLETS[i] ?? (() => '') }
    if (c.type !== undefined) col.type = c.type
    const field = DATA_FIELD_OVERRIDES[i] ?? c.field
    if (field !== undefined) col.field = field
    if (c.title !== undefined) col.title = c.title
    if (c.width !== undefined) col.width = c.width
    if (c.minWidth !== undefined) col.minWidth = c.minWidth
    if (c.hide === true) col.hide = true
    if (c.align !== undefined) col.align = c.align
    if (c.fixed !== undefined) col.fixed = c.fixed
    if (c.sort === true) col.sort = true
    if (c.unresize === true) col.unresize = true
    if (c.toolbar !== undefined) col.toolbar = c.toolbar
    return col
  })
}

/**
 * `#total` 的统计行（照 `myorderlist.js` **L941-944** 的拼接式重建，i18n 值取同文件
 * `dataTotal1…dataTotal10`）。
 *
 * ⚠️ **不是逐字照抄**（唯一一处改词，已登记在文件头）：i18n 那句写的是「其中**待上号**0笔」，
 * 我们写「其中**待抢单**0笔」——与状态徽标同词（业务方裁定「同一状态在任何页面同一文案」）。
 *
 * ⚠️ **这里的数字恒 0，刻意不改**：对标那几个数（`TotalCount` / `TotalRecFee` / `DSH` / `DLZ` /
 * `DYS` / `WTD` / `YTD`）来自**服务端聚合** `res.total.*`——我们后端没有这个出口
 * （`/my-orders/` 只回绩效行，连「接手价」都没回），前端用绩效金额去凑「接手总金额」就是**编数**。
 * 所以统计行保持既有骨架，真数据只体现在**表格行**里。这是本轮明确留的缺口（见报告「不确定处」）。
 * `#total` 在工具栏模板里是**空 div**（HTML L232），不写这一段 `parity` 会报 `#total` 存在性差异。
 */
function renderTotal(root: HTMLElement): void {
  const el = root.querySelector('#total')
  if (!el) return
  // L943 读的就是这个隐藏域（PII 已清空 → 空串）。
  const company = root.querySelector<HTMLInputElement>('#company')?.value ?? ''
  el.innerHTML =
    '【统计】共0笔订单，接手总金额0元，其中待抢单0笔，代练中0笔待验收0笔，问题单0笔，已退单0笔,' +
    '<span style="color:#E03636;font-size:14px">【声明】该系统并非交易平台，无担保服务，' +
    `系统内所有订单业务及结算均由${company}负责运营，本系统一切业务和结算与软件开发商鸡蛋铺无关</span>`
}

/* ── ⭐ 本页筛选：**前端本地筛已取到的全量行**（业务方 2026-09-28 裁定「不动后端」；文件头 12-(b)）──
 *
 * ⛔ **为什么不给 `/my-orders/` 加筛选参数**：业务方明确选了「不动后端」，而且本页本来就由
 * `fetchAllMyOrders()` 一次取完、分页也在前端做（见文件头「为什么取数不交给 layui 的 url」）
 * ⇒ 本地筛选与既有架构**同源**，不是另开一套取数。
 * ⚠️ 代价（如实登记）：筛选只作用于**已经取回来的行**。`fetchAllMyOrders` 的上限是
 * `100 × 200 = 20000` 行（`api/myOrders.ts` 的 `MAX_PAGES`），超过它本来就会**响亮报错**，
 * 所以不存在「悄悄少筛几行」这种静默形态。
 */

/** 页面筛选条件（`applyFilters` 的输入）。 */
export type MyOrderFilters = {
  /** 游戏区服（= 我们的 `game_name`；空串 = 全部）。 */
  gameName: string
  /** 订单状态（多选题；空数组 = 全部）。 */
  statuses: readonly OrderStatus[]
  /** 搜索1：订单号（子串匹配；空串 = 不筛）。 */
  key1: string
  /** 搜索2：代练/陪玩内容（子串匹配；空串 = 不筛）。 */
  key2: string
  /** 接单日期起（`YYYY-MM-DD`；空串 = 不筛）。 */
  sDate: string
  /** 接单日期止（`YYYY-MM-DD`；空串 = 不筛）。 */
  eDate: string
  /** 只看加急单（checkbox）。 */
  urgent: boolean
}

/** 一条都不筛的筛选（测试与兜底用；避免各处手搓一个空对象）。 */
export const EMPTY_FILTERS: MyOrderFilters = {
  gameName: '',
  statuses: [],
  key1: '',
  key2: '',
  sDate: '',
  eDate: '',
  urgent: false,
}

/**
 * 证据件「订单状态」那一档的**中文 label** → 我们的枚举名（反查 `ORDER_STATUS_LABELS`）。
 * 映射不到返回 `undefined`（⛔ 不猜、不静默当成某个状态）。
 */
export function statusOfFilterLabel(label: string): OrderStatus | undefined {
  return (Object.keys(ORDER_STATUS_LABELS) as OrderStatus[]).find(
    (code) => ORDER_STATUS_LABELS[code] === label,
  )
}

/**
 * 把 `select[name=orderSta]` 的值拆成枚举名数组。
 * ⚠️ formSelects 多选时写回隐藏 `<select>` 的是**逗号拼串**（`order-orderlist.ts` 的
 * `statusOfFilterValue` 同口径）；映射不到的档位**丢弃**。
 */
export function statusesOfFilterValue(value: string): OrderStatus[] {
  const out: OrderStatus[] = []
  for (const piece of value.split(',')) {
    const code = statusOfFilterLabel(piece.trim())
    if (code !== undefined && !out.includes(code)) out.push(code)
  }
  return out
}

/**
 * 本地筛选（**纯函数**：导出给测试直接钉判据，页面挂载路径只是它的一个调用方）。
 *
 * 判据「空值丢弃、`0`/`false` 不丢」在这里天然成立：子串筛选只认**空串**为「不筛」
 * （`AGENTS.md` §5-E 的 E-0021 同族——别把 `0` 那种合法值当真值判断）。
 */
export function applyFilters(rows: MyOrderItem[], f: MyOrderFilters): MyOrderItem[] {
  return rows.filter((row) => {
    if (f.gameName !== '' && row.game_name !== f.gameName) return false
    if (f.statuses.length > 0 && !f.statuses.includes(row.status as OrderStatus)) return false
    if (f.urgent && row.is_urgent !== true) return false
    // ⭐ D-0301：单号搜索**两个号都认**（来源号能搜到 + 系统号仍能搜到）。
    if (
      f.key1 !== '' &&
      !row.order_no.includes(f.key1) &&
      !(row.source_order_no || '').includes(f.key1)
    )
      return false
    if (f.key2 !== '' && !row.project_text.includes(f.key2)) return false
    // 日期比的是**那一行 `created_at` 的本地日期部分**（后端回的是带时区的 ISO 串）。
    // ⚠️ `YYYY-MM-DD` 定长字典序比较 = 日期比较；⛔ 不要 `new Date()` 再比——时区会漂一天。
    const day = row.created_at.slice(0, 10)
    if (f.sDate !== '' && day < f.sDate) return false
    if (f.eDate !== '' && day > f.eDate) return false
    return true
  })
}

/** `YYYY-MM-DD`（本地时区）。 */
function toDateInputValue(d: Date): string {
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/**
 * 日期框的默认值 = 「今天往前 6 个月 ~ 今天」。
 *
 * ⚠️ 这是**对标的规则**，不是证据件里那两个字面值：对标那两格是**服务端按当天渲染**的
 * （证据件冻结的是抓取当日 `2026-03-20` ~ `2026-09-20`）⇒ 照抄字面值会让页面永远停在抓取那天。
 * 业务方 2026-09-28 明确选了「照对标给近 6 个月」（代价：默认就会把更早的单藏起来）。
 */
export function defaultDateRange(now: Date = new Date()): [string, string] {
  return [
    toDateInputValue(new Date(now.getFullYear(), now.getMonth() - 6, now.getDate())),
    toDateInputValue(now),
  ]
}

/* ── ⭐ 「导出订单」→ CSV（见文件头 12-(d)）────────────────────────────────────── */

/** 导出的一列：表头**逐字取本页 `COLS` 的 `title`**（同一份文案，不另编一套）。 */
export type CsvColumn = { title: string; value: (d: MyOrderItem) => string }

/**
 * 只导出**本页真有值**的那几列（后端载荷里没有的列不导空列——那只会制造「这一列怎么全是空的」）。
 * 「加急」不是表格里的一列，但它是第 1 列那颗徽标承载的可见事实，所以单独给一列「是/否」。
 */
export const CSV_COLUMNS: readonly CsvColumn[] = [
  // ⭐ D-0301：与列渲染同一口径——来源号非空取它、空回退内置号。
  { title: '订单号', value: (d) => d.source_order_no || d.order_no },
  { title: '游戏名称', value: (d) => d.game_name },
  { title: '代练/陪玩内容', value: (d) => d.project_text },
  { title: '订单状态', value: (d) => statusText(d.status) },
  { title: '接手时间', value: (d) => d.created_at.replace('T', ' ').slice(0, 16) },
  { title: '加急', value: (d) => (d.is_urgent === true ? '是' : '否') },
]

/**
 * 会被 Excel / WPS 当**公式**解释的开头字符（CSV 注入）。
 * ⚠️ `\t` / `\r` 也在内：它们会被某些表格软件当分隔 / 续行处理。
 */
const CSV_FORMULA_PREFIX = /^[=+\-@\t\r]/

/** 一个 CSV 单元格：**先做公式注入前缀转义、再按 RFC4180 加引号**（内层引号翻倍）。 */
export function csvCell(value: string): string {
  const guarded = CSV_FORMULA_PREFIX.test(value) ? `'${value}` : value
  return `"${guarded.replace(/"/g, '""')}"`
}

/** 整份 CSV 正文（⛔ 不含 BOM；BOM 由下载那一层加，见 `downloadCsv`）。 */
export function buildCsv(rows: MyOrderItem[], columns: readonly CsvColumn[] = CSV_COLUMNS): string {
  const lines = [columns.map((c) => csvCell(c.title)).join(',')]
  for (const row of rows) lines.push(columns.map((c) => csvCell(c.value(row))).join(','))
  // RFC4180 的换行是 CRLF（Excel 对它最稳）。
  return lines.join('\r\n')
}

/** 文件名：`我的订单_YYYYMMDDHHMMSS.csv`（本地时间）。 */
export function csvFilename(now: Date = new Date()): string {
  const p = (n: number): string => String(n).padStart(2, '0')
  return (
    `我的订单_${now.getFullYear()}${p(now.getMonth() + 1)}${p(now.getDate())}` +
    `${p(now.getHours())}${p(now.getMinutes())}${p(now.getSeconds())}.csv`
  )
}

/**
 * 落盘。⚠️ 正文前置 **UTF-8 BOM**（`\uFEFF`）：没有它 Excel 会按本地码页解码中文 ⇒ 乱码。
 * ⛔ 不用 `window.open`（`order-orderlist.ts` 的 `downloadOrderExport` 里记过同一坑）。
 */
function downloadCsv(filename: string, csv: string): void {
  const blob = new Blob([`\uFEFF${csv}`], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

/**
 * 挂载本页。**导出**是为了让测试能挂整页并断言真实出口（与 `order-orderlist.ts` 的
 * `mountOrderListPage` 同形）；`default.mount` 只是它的薄包装。
 */
export async function mountMyOrderListPage(
  root: HTMLElement,
  // ⛔ T5 起本页**不用** `ctx`：唯一那个「进订单详情」的入口改成了原地开层，本页没有跳路由的
  //    入口。参数保留是 `IslandPage.mount` 的签名位置（与其它 island 页同形）；下划线 =
  //    本仓 eslint 约定的「有意未用」（`eslint.config.ts` 的 `argsIgnorePattern: '^_'`）。
  _ctx: IslandContext,
): Promise<() => void> {
  document.title = TITLE
  /**
   * ⚠️ **顺序不可颠倒：先挂 layui 样式/脚本，再塞证据件标记**（2026-09-22 修，业务方报障）。
   *
   * `layui.css` 不是写死在外壳 `layui-island/page.html` 里的，而是 `bootstrapIsland()` 运行期插
   * `<link>`（见 `@/layui/loader` 的 `injectCss`）。先前这里先 `root.innerHTML = 证据件`、
   * 再 `bootstrapIsland()`，于是浏览器先按「没穿样式」的样子画一遍骨架（默认字体、没边框的
   * 输入框/按钮/表格），等 131 KB 的样式表到了才变正常 ⇒ 业务方看到的
   * 「从内部抢单大厅切到本页时，框架从简陋闪一下变正常」。
   * 同批的 `order-insiderecorderlist`（大厅页）本来就是先挂样式，所以「有的页面闪、有的不闪」。
   *
   * 两道兜底：静态网 `src/layui-island/__tests__/page-mount-order.spec.ts`（全站页面），
   * 运行期钉子 `__tests__/order-myorderlist.spec.ts` 的 `LAYUI.order`（本页）。别只挪下面一行。
   */
  const layui: LayuiNamespace = await bootstrapIsland(['form', 'table', 'layer'])
  const res = await fetch(`${import.meta.env.BASE_URL}${ARTIFACT_URL.replace(/^\//, '')}`)
  if (!res.ok) throw new Error(`证据件取不到（HTTP ${res.status}）：${ARTIFACT_URL}`)
  root.innerHTML = await res.text()

  // 页签/折叠面板要等标记进来之后再绑定（见 `initIslandElements` 的注释）。
  initIslandElements(layui)
  // ⭐ 往工具栏**模板**里注入「同步订单状态」：必须赶在下面 `table.render` **之前**
  //    （layui 到那时才编译模板）；⛔ 不改证据件（见 `injectSyncToolbarButton` 的注释）。
  injectSyncToolbarButton(root)
  const form = layui.form as unknown as Record<string, unknown>
  const table = layui.table as unknown as Record<string, unknown>
  const layer = layui.layer as unknown as Record<string, unknown>

  /** 中性提示（对标 `layer.msg` 的 1500ms 默认）。 */
  const msg = (text: string): void => islandMsg(layer, text)
  /** 错误提示：拉长到 6000ms——权限/业务失败必须**看得清**，一闪而过等于"点了没反应"。 */
  const errMsg = (text: string): void => {
    ;(layer.msg as (t: string, o?: unknown) => void)(text, { time: ERROR_MSG_MS })
  }
  /** 错误对象的可显示文本。**不自造文案**：`request()` 抛出的 message 就是后端 `msg` 原文。 */
  const errText = (e: unknown): string => (e instanceof Error ? e.message : '操作失败')
  /** 「尚未开放」的统一形状（与 `artifactPage` 旧口径逐字一致，旧断言按此改写）。 */
  const notOpen = (name: string): void => msg(`「${name}」尚未开放`)
  /** 从**页内标记**取按钮文案（不在 TS 里重抄一份中文，改名时不会漂移）。 */
  const labelOf = (selector: string, fallback: string): string => {
    const el = root.querySelector<HTMLElement>(selector)
    if (!el) return fallback
    // ⚠️ 图标字形会进 `textContent`：`<i class="layui-icon">&#xe66f;</i>设定机位` 直接取文本会得到
    // 一个私用区字符打头的串（旧 `artifactPage` 的 `clickLabel` 就有这个毛病，提示里混着方块）。
    // 去掉 `<i>` 再取文本，提示才是用户看到的「设定机位」。
    const clone = el.cloneNode(true) as HTMLElement
    for (const icon of Array.from(clone.querySelectorAll('i'))) icon.remove()
    const text = (clone.textContent ?? '').trim().replace(/\s+/g, ' ')
    return text === '' ? fallback : text
  }
  /** 同一时刻只允许一次提交 / 一次更改状态（前端唯一闸门；业务规则全在后端）。 */
  let submitting = false
  let changing = false
  /**
   * 手动推送是否在途（前端唯一闸门，修复轮 Blocking-1）。
   * ⚠️ 真正挡住「两轮同时消费同一绑定」的是**后端共享引擎**的串行闸；这里只省一趟
   * 明知会被挡的请求（同一标签页连点）。⛔ 别把它当承重判据。
   */
  let pushing = false

  /** 一次取回来的**全量行**（筛选在它上面做，见文件头 12-(b)）。 */
  let allRows: MyOrderItem[] = []
  /** 当前**筛出来**的行（「导出」没勾选时导的就是它）。 */
  let visibleRows: MyOrderItem[] = []

  /** 从表单读当前筛选条件。⚠️ 判据是「空串才不筛」，不是真值（E-0021 同族）。 */
  function currentFilters(): MyOrderFilters {
    const values = (form.val as (f: string) => Record<string, string>)('fm')
    return {
      gameName: (values.gameName ?? '').trim(),
      statuses: statusesOfFilterValue(values.orderSta ?? ''),
      key1: (values.key1 ?? '').trim(),
      key2: (values.key2 ?? '').trim(),
      sDate: (values.sDate ?? '').trim(),
      eDate: (values.eDate ?? '').trim(),
      // ⚠️ checkbox 不读 `form.val`：layui 的 `form.val` 对**未勾选**的 checkbox 不回键
      //（`order-orderlist.ts` 的 `checkedNames` 是同一条理由），直接读 DOM 的 `checked` 最稳。
      urgent: root.querySelector<HTMLInputElement>('input[name="isJiajiCheck"]')?.checked === true,
    }
  }

  /** 按当前筛选条件重渲表格（搜索按钮 / 加急单勾选 / 区服切换 / 刷新之后都走它）。 */
  function renderFiltered(): void {
    visibleRows = applyFilters(allRows, currentFilters())
    ;(table.reload as (id: string, o: unknown) => void)(TABLE_ID, {
      data: visibleRows,
      page: { curr: 1, limit: PAGE_LIMIT },
    })
  }

  /**
   * 把区服下拉的选项换成**我们自己数据里的游戏名**。
   *
   * ⚠️ 证据件那些 `<option>` 的 value 是**对标自己的游戏 id**（`21701` 这类），我们没有那个 id
   * ⇒ 按 `game_name` 重建（先例：`order-orderlist.ts` 的同一处理，D-0093）。
   * ⚠️ 重建后**尽量保住用户当前选的那一项**（刷新列表不该把刚设的筛选悄悄重置）。
   * ⚠️ 必须再 `form.render('select')`：layui 的 select 是渲染出来的，改了原生 `<option>` 不重渲
   * 就还是旧样子（而 `form.val` 读的又是原生值 ⇒ 会造出「显示的和筛的对不上」）。
   */
  function rebuildGameOptions(): void {
    const select = root.querySelector<HTMLSelectElement>('#gameName')
    if (select === null) return
    const names = Array.from(new Set(allRows.map((r) => r.game_name).filter((n) => n !== ''))).sort(
      (a, b) => a.localeCompare(b, 'zh-Hans-CN'),
    )
    const previous = select.value
    select.innerHTML =
      '<option value="">全部游戏</option>' +
      names.map((n) => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join('')
    select.value = names.includes(previous) ? previous : ''
    ;(form.render as (t?: string) => void)('select')
  }

  /** 重新取数（工具栏「刷新列表」与提交 / 改状态成功后的刷新都走它）。 */
  async function reload(): Promise<void> {
    let rows: MyOrderItem[] = []
    try {
      rows = await fetchAllMyOrders()
    } catch (e) {
      // ⚠️ 失败也要把表清空并**如实报错**：403（无节点 `my_orders.send_to_my_system`）/ 401 的 msg 由 `request()` 给出。
      errMsg(errText(e))
    }
    allRows = rows
    rebuildGameOptions()
    // ⚠️ 刷新**保留当前筛选条件**（对标 `reloadOrderList` 也是带着 `seachParam` 重取）：
    // 刷一次就把用户刚设的筛选清掉，会让人以为「筛选根本没生效」。
    renderFiltered()
  }

  /**
   * 提交完单。**规则不在这里**：403（不是代练员 / 不是本人）与 200+业务码（来源态不对 /
   * 金额算不出来）都由后端判，前端把 `msg` 原样显示。顺序：**先刷新再提示成功**——
   * `reload()` 内部的失败提示若排在后面会把成功提示盖掉（同 `order-orderlist.ts` 的时序坑）。
   */
  async function doSubmit(orderNo: string): Promise<void> {
    if (submitting) return
    submitting = true
    try {
      await submitMyOrder(orderNo)
      await reload()
      msg(SUBMIT_SUCCESS_TEXT)
    } catch (e) {
      errMsg(errText(e))
    } finally {
      submitting = false
    }
  }

  /** 对标 `submtEnd` 先 `layer.confirm`（`myorderlist.js` L1200），确认后才发请求。 */
  function confirmSubmit(orderNo: string): void {
    if (orderNo === '') return
    ;(layer.confirm as (t: string, yes: (i: number) => void) => void)(
      SUBMIT_CONFIRM_TEXT,
      (index) => {
        ;(layer.close as (i: number) => void)(index)
        void doSubmit(orderNo)
      },
    )
  }

  /**
   * ⭐ 「同步订单状态」（spec §9.3；**2026-10-23 起双档**，计划 §4）：
   *
   * - **有勾选** ⇒ 把勾选的行**推过去**（`pushMemberJidanpuNow(nos, 'status')`，
   *   与「发到我的管理系统」共用同一个处理器 `doPushToMySystem`，只是 action 不同）；
   * - **无勾选** ⇒ **保留原行为**：调 `POST member/jidanpu-sync-now/` 往下**拉一轮**
   *   （从下家把已完成拉成待验收）——⛔ 别把这条路删掉（那是既有能力，业务方只要求「有勾选时」改推送）。
   *
   * 两个方向都只作用**当前登录人**那一行绑定（越权面 = 0）。
   *
   * ⛔ **前端不写节流判据**（拉取那一档）：最小间隔 / 在途互斥 / 失败退避全在后端，被挡时后端回
   * **人话（含剩余秒数）** ⇒ 这里把它**原样显示**。自己再判一次就会与后端漂移（用户看到「还能点」，
   * 点了却被拒；或者反过来把一次合法的点击拦掉）。
   * ⚠️ 成功文案是**自拟**的（后端成功信封的 `msg` 是空串，照抄就什么都不显示），
   * 但它必须把「这一轮更新了几笔」说出来——否则用户看不出同步到底干了什么。
   * ⚠️ 顺序：**先刷新列表、再提示成功**（`reload()` 内部的失败提示若排在后面会把成功提示
   * 盖掉，同 `doSubmit` 的时序坑）；失败时**不刷新**（没改任何东西，白打一次请求）。
   */
  async function doSyncOrderStatus(): Promise<void> {
    const checked = checkedRows()
    if (checked.length > 0) {
      await doPushToMySystem('status', checked)
      return
    }
    try {
      const result = await syncMemberJidanpuNow()
      await reload()
      msg(`同步完成，本次更新 ${result.applied} 笔订单`)
    } catch (e) {
      errMsg(errText(e))
    }
  }

  /**
   * ⭐ 把**勾选的行**推到我的下家鸡蛋铺（2026-10-23，计划 §2 / §4）。
   *
   * - `record` = 「发到我的管理系统」：把这些单**录到**我绑定的下家（只录单，⛔ 不抛大厅）；
   * - `status` = 「同步订单状态」**有勾选时**：把我方**当前状态**推给下家那张单。
   *
   * ⛔ **不勾选 ⇒ 只提示、不发请求**（对标 zh-CN `setRecMan` 那句「请选中要操作的记录」；
   * 与「更改状态」同一口径）——按业务口径这两颗按钮只作用于勾选的行。
   * ⛔ **不在这里复刻后端的归属 / 状态判据**：逐单「是不是他当前接手的单」「这一档推不推」
   * 全在后端（`claimed_perf_of` + 消费者），前端把结果**如实**说出来即可。
   * ⚠️ 失败（无绑定 1002 / 入参 1000 / 网络）⇒ 后端 `msg` **原样**显示（`errMsg`，6000ms）。
   * ⚠️ 顺序同 `doSubmit`：先刷新、再提示（刷新里的失败提示若排在后面会盖掉结果）。
   *
   * ⭐ **在途互斥**（修复轮 Blocking-1 的前端那一半）：请求没回来时再点 ⇒ 只提示
   * 「正在推送，请稍候」、**不再发第二次**。⛔ 这不是承重判据——两个标签页 / 两个浏览器
   * 同时点仍会打到后端，真正的互斥在**共享引擎**的按绑定串行闸里。
   */
  async function doPushToMySystem(
    action: 'record' | 'status',
    rows: readonly MyOrderItem[],
  ): Promise<void> {
    if (rows.length === 0) {
      msg(SELECT_RECORDS_FIRST_TEXT)
      return
    }
    if (pushing) {
      // ⚠️ 用 `msg`（1500ms）而不是 `errMsg`：这不是失败，是「刚点过、等一下」。
      msg(PUSH_IN_PROGRESS_TEXT)
      return
    }
    pushing = true
    // ⚠️ 单号按勾选顺序发出去（后端去重保序，逐单结果的顺序 = 这里这个顺序）。
    const orderNos = rows.map((row) => row.order_no)
    try {
      const result = await pushMemberJidanpuNow(orderNos, action)
      await reload()
      // ⚠️ 用**后端计数**说话（⛔ 不自己数一遍：两套判据必然漂）；
      //    跳过 / 失败时把**后端那句原因**缀在后面（多条不同只在末尾加「等」）。
      const reasons = result.items.map((item) => item.reason)
      const text = pushResultText(
        result.pushed,
        result.skipped,
        result.failed,
        reasons,
      )
      // ⚠️ 时长（复评 Minor-5）：带原因时用 `errMsg` 的 **6000ms**（那句话要来得及读完），
      //    没有原因仍 1500ms（「已推送 N 笔」一眼就能看完）。
      if (pushResultHasReason(result.skipped, result.failed, reasons)) errMsg(text)
      else msg(text)
    } catch (e) {
      errMsg(errText(e))
    } finally {
      // ⚠️ `finally`：请求抛了也必须复位，否则一次网络失败会把这张页面卡成「永远在推送」。
      pushing = false
    }
  }

  /* ── ⭐ 「更改状态」的两档（业务方 2026-09-28 裁定；见文件头 12-(c)）────────────────── */

  /** 当前勾选的行（layui 的 `checkStatus`；没勾就是空数组）。 */
  function checkedRows(): MyOrderItem[] {
    const checkStatus = table.checkStatus as ((id: string) => { data?: MyOrderItem[] }) | undefined
    return checkStatus?.(TABLE_ID)?.data ?? []
  }

  /**
   * 点「更改状态」：先**逐行**校验来源态，再开弹层。
   *
   * ⚠️ 业务方裁定的受限语义：只有**代练中**的单能改、且只能改到「待验收 / 问题单」。
   * ⚠️ 前端这一道只是**省一趟明知会被拒的请求**，真正的判据在后端那两个端点里（各判一次）；
   * ⛔ 别在这里「复刻」后端的金额 / 归属判据（那必然与后端漂移）。
   * ⚠️ 入参只要求 `{order_no, status}` 两列：行内状态徽标那条路手上只有这两个值
   * （见 `onCellClick`），而工具栏那条路给的是完整的 `MyOrderItem`——结构化类型两头都收。
   */
  function requestChangeStatus(rows: readonly { order_no: string; status: string }[]): void {
    if (rows.length === 0) {
      msg(SELECT_RECORDS_FIRST_TEXT)
      return
    }
    const notInProgress = rows.find((r) => r.status !== 'IN_PROGRESS')
    if (notInProgress !== undefined) {
      errMsg(`${CHANGE_STATUS_REQUIRES_IN_PROGRESS}${statusText(notInProgress.status)}`)
      return
    }
    openChangeStatusDialog(rows.map((r) => r.order_no))
  }

  /**
   * 开「更改状态」弹层。层参数逐项取本页 `myorderlist.js:1356-1366`（对标那支 `changeOrderSta`）：
   * `title` = i18n `changeOrderSta`（=「更改订单状态」）、`shade [0.3,'#333']`、`shadeClose:true`、
   * `skin 'layui-layer-lan'`、`anim 5`、`area ['80%','240px']`。
   * ⚠️ **`type` 是唯一一处有意偏离**：对标是 `type: 2`（装 `SetOrderSta` iframe、13 个状态随便选），
   * 我们没有那个页、也不该做那种「任意状态」⇒ 这里是 `type: 1` + 自己两个 radio
   *（完整理由见文件头 12-(c)）。
   * ⚠️ 内容用 **DOM 元素**（不是 outerHTML 字符串）：`yes` 回调要能读到它里面选中的那一档，
   * 拼成字符串就丢了引用（再去 `document.querySelector` 找它 = 给同文档里的多层层埋雷）。
   */
  function openChangeStatusDialog(orderNos: readonly string[]): void {
    if (orderNos.length === 0) return
    const box = document.createElement('div')
    box.className = 'layui-form'
    box.style.padding = '20px 30px'
    box.innerHTML = CHANGE_STATUS_TARGETS.map(
      (code, i) =>
        `<input type="radio" name="target" value="${escapeHtml(code)}"` +
        ` title="${escapeHtml(statusText(code))}"${i === 0 ? ' checked' : ''}>`,
    ).join('') +
      '<label class="layui-form-item" style="display: none; margin-top: 8px">' +
      `<input type="text" class="layui-input" aria-label="说明原因" maxlength="${ISSUE_REASON_MAX_LENGTH}" ` +
      `placeholder="${ISSUE_REASON_REQUIRED_TEXT}"></label>`
    const reasonInput = box.querySelector<HTMLInputElement>('input[aria-label="说明原因"]')!
    const reasonRow = reasonInput.parentElement!
    const selectedTarget = (): OrderStatus =>
      box.querySelector<HTMLInputElement>('input[name="target"]:checked')?.value === 'ISSUE'
        ? 'ISSUE'
        : 'PENDING_REVIEW'
    const updateReasonVisibility = () => {
      reasonRow.style.display = selectedTarget() === 'ISSUE' ? 'block' : 'none'
    }
    // layui 改选后会更新原始 radio，点击代理与键盘 change 都留在当前弹窗内。
    box.addEventListener('click', updateReasonVisibility)
    box.addEventListener('change', updateReasonVisibility)
    ;(layer.open as (o: unknown) => void)({
      type: 1,
      title: CHANGE_STATUS_DIALOG_TITLE,
      shade: [0.3, '#333'],
      shadeClose: true,
      skin: 'layui-layer-lan',
      anim: 5,
      area: ['80%', '240px'],
      content: box,
      btn: [CHANGE_STATUS_CONFIRM_BTN, CHANGE_STATUS_CANCEL_BTN],
      // layui 只在 `form.render()` 时把裸 radio 渲染成它自己的样式；内容塞进层之后要再渲一次，
      // 否则用户看到的是两个没有样式的原生圆点。
      success: () => {
        ;(form.render as () => void)()
      },
      yes: (index: number) => {
        const target = selectedTarget()
        const reason = reasonInput.value.trim()
        if (target === 'ISSUE' && reason === '') {
          errMsg(ISSUE_REASON_REQUIRED_TEXT)
          updateReasonVisibility()
          reasonInput.focus()
          return
        }
        ;(layer.close as (i: number) => void)(index)
        void runChangeStatus(target, orderNos, reason)
      },
    })
  }

  /**
   * 真发请求：**逐单串行**（一笔一笔来，避免并发把同一张单推两次），成功数与第一条失败原因都报出来。
   *
   * ⚠️ **失败也照旧刷新**：先前发出去的那几笔可能已经生效，不刷新页面就会显示旧状态
   *（「一半成功」最容易被看成「全没成功」）。
   * ⚠️ 顺序：**先刷新、再提示**（`reload()` 内部的失败提示若排在后面会把结果提示盖掉，
   * 同 `doSubmit` / `doSyncOrderStatus` 的时序坑）。
   */
  async function runChangeStatus(target: OrderStatus, orderNos: readonly string[], reason: string): Promise<void> {
    if (changing) return
    changing = true
    let done = 0
    let firstError = ''
    try {
      for (const orderNo of orderNos) {
        try {
          if (target === 'ISSUE') await reportIssueOrder(orderNo, reason)
          else await submitMyOrder(orderNo)
          done += 1
        } catch (e) {
          if (firstError === '') firstError = errText(e)
        }
      }
    } finally {
      changing = false
    }
    await reload()
    if (firstError !== '') {
      errMsg(done > 0 ? `已更改 ${done} 笔；${firstError}` : firstError)
      return
    }
    msg(target === 'ISSUE' ? REPORT_ISSUE_SUCCESS_TEXT : SUBMIT_SUCCESS_TEXT)
  }

  /* ── ⭐ 「导出订单」→ CSV（见文件头 12-(d)）────────────────────────────────────── */

  /**
   * 导出给定的行。
   * ⚠️ 一行都没有时**不产出一个空文件**（那会让人以为「导出成功了但没内容」）⇒ 一句提示。
   */
  function exportOrders(rows: MyOrderItem[]): void {
    if (rows.length === 0) {
      msg(EXPORT_EMPTY_TEXT)
      return
    }
    downloadCsv(csvFilename(), buildCsv(rows))
    msg(`已导出 ${rows.length} 笔订单`)
  }

  // 首屏取数：**先取再渲染**，避免「先出空表、再闪一下」；取数失败则空表 + 6000ms 错误提示。
  // 权限门与首屏取数并发：身份取不到 ⇒ fail-closed（不渲染截图按钮）。
  const [meResult, rowsResult] = await Promise.allSettled([fetchMe(), fetchAllMyOrders()])
  setMyOrdersPermissions(meResult.status === 'fulfilled' ? meResult.value : undefined)
  // ⚠️ 权限门必须赶在 `table.render` **之前**摘按钮：layui 到那时才编译工具栏模板
  //（见 `applyToolbarPermissionGates` 的注释）。
  applyToolbarPermissionGates(root)
  let loadError: string | null = null
  if (rowsResult.status === 'fulfilled') {
    allRows = rowsResult.value
  } else {
    loadError = errText(rowsResult.reason)
  }
  // 日期框默认值：按对标的**规则**算「今天往前 6 个月 ~ 今天」（见 `defaultDateRange`）。
  // ⚠️ 写在 `table.render` 之前：首屏那一次筛选就要用上它（业务方 2026-09-28 选了「照对标给近 6 个月」）。
  const [defaultFrom, defaultTo] = defaultDateRange()
  const setInputValue = (selector: string, value: string): void => {
    const el = root.querySelector<HTMLInputElement>(selector)
    if (el !== null) el.value = value
  }
  setInputValue('#sDate', defaultFrom)
  setInputValue('#eDate', defaultTo)
  rebuildGameOptions()
  visibleRows = applyFilters(allRows, currentFilters())

  ;(table.render as (o: unknown) => void)({
    elem: TABLE.elem,
    id: TABLE_ID,
    data: visibleRows,
    ...(TABLE.toolbar !== undefined ? { toolbar: TABLE.toolbar } : {}),
    defaultToolbar: TABLE.defaultToolbar,
    page: TABLE.page,
    cols: [buildCols()],
    text: { none: '暂无数据' },
    done: () => {
      ;(form.render as () => void)()
      // L931-944 的 `done`：往工具栏模板里的空 `#total` 写统计行（标记见 `renderTotal`）。
      renderTotal(root)
    },
  })
  ;(form.render as () => void)()
  if (loadError !== null) errMsg(loadError)
  // 对标 `myorderlist.js` 由 layui 的 `formSelects` 模块自动扫 `select[xm-select]`。
  // ⚠️ **2026-09-28 起只剩 1 个**（`orderSta`）：`gameArea` / `gameServer` / `gameJob` / `gameGroup`
  // 四个 xm-select 控件已随那批删减一起没了（见文件头 12-(a)）⇒ 别再把「5 个」当现状。
  initFormSelects('select[xm-select]')

  /* ── 工具栏 6 颗按钮：5 颗真功能（刷新 / 发到我的管理系统 / 同步 / 更改状态 / 导出）+ 1 颗「尚未开放」── */

  /**
   * 2026-09-28（业务方裁定，见文件头第 12 条）＋ **2026-10-23**（手动推送线，计划 §4）之后：
   *  - `refresh` → `reload()`（真动作）；
   *  - `sendToMySystem` 发到我的管理系统 → `doPushToMySystem('record', 勾选的行)`
   *    （⭐ 2026-10-23 **接真**：只对勾选的行，未勾选 ⇒ 提示「请选中要操作的记录」）；
   *  - `syncOrderStatus` → `doSyncOrderStatus()`（**双档**：有勾选 ⇒ 推状态；无勾选 ⇒ 往下拉一轮）；
   *  - `changeStaBtn` 更改状态 → `requestChangeStatus()`（**受限两档**：待验收 / 问题单）；
   *  - `exportBtn` 导出订单 → `exportOrders()`（前端生成 CSV）；
   *  - `checkOrderByLocal` 检查发送失败订单 → 后端**确实没有**这个端点
   *    （`.tools/audit-frontend-vs-backend.md` 表 B）⇒ 仍是「尚未开放」。
   *  ⛔ 已删的两颗（`setRecMan` 指派订单给打手 / `setJiWei` 设定机位）**连按钮带事件一起没了**：
   *    本仓没有这两件事的后端能力，业务方裁定不做（⛔ 别只删处理器、把按钮留在标记里——
   *    那会变成「点了没反应」，比「尚未开放」更糟）。
   *
   * ⚠️ **只认 `TOOLBAR_NOT_OPEN_EVENTS` 里那 1 个 + 上面 5 个真落点，其余事件一律静默放行**
   *（`TOOLBAR_NOT_OPEN_EVENTS` 的注释里有理由）：layui 会用同一个 `toolbar(id)` 事件把
   * `defaultToolbar` 内置图标的点击也送上来。
   */
  ;(table.on as (e: string, cb: (obj: { event?: string }) => void) => void)(
    `toolbar(${TABLE_ID})`,
    (obj) => {
      const event = obj.event ?? ''
      if (event === 'refresh') {
        void reload()
        return
      }
      // ⭐ 工具栏「发到我的管理系统」（2026-10-23 接真）：**只对勾选的行**录到我的下家；
      //    ⛔ 未勾选不发请求（`doPushToMySystem` 里那句「请选中要操作的记录」）。
      if (event === SEND_TO_MY_SYSTEM_EVENT) {
        void doPushToMySystem('record', checkedRows())
        return
      }
      // ⭐ 工具栏「同步订单状态」（Task 9；2026-10-23 起双档）：**真动作**，不走下面那条「尚未开放」的路。
      if (event === SYNC_TOOLBAR_EVENT) {
        void doSyncOrderStatus()
        return
      }
      // ⭐ 「更改状态」：对**勾选的行**（未勾选 ⇒ 对标那句「请选中要操作的记录」）。
      if (event === CHANGE_STATUS_EVENT) {
        requestChangeStatus(checkedRows())
        return
      }
      // ⭐ 「导出订单」：勾了行导勾选的那些，没勾就导当前筛出来的全部。
      if (event === EXPORT_EVENT) {
        const checked = checkedRows()
        exportOrders(checked.length > 0 ? checked : visibleRows)
        return
      }
      if (!TOOLBAR_NOT_OPEN_EVENTS.includes(event)) return
      notOpen(labelOf(`.layui-table-tool [lay-event="${event}"]`, event))
    },
  )

  /* ── 行内「提交完成」：唯一真动作 ── */
  ;(table.on as (
    e: string,
    cb: (obj: { event?: string; data?: MyOrderItem }) => void,
  ) => void)(`tool(${TABLE_ID})`, (obj) => {
    const event = obj.event ?? ''
    if (event === 'submtEnd') {
      confirmSubmit(obj.data?.order_no ?? '')
      return
    }
    notOpen(labelOf(`[lay-event="${event}"]`, event))
  })

  /* ── 行内「订单截图」两颗按钮：各自开一个真弹层（图片存储线 Task 8，2026-09-25） ── */

  /**
   * 第 14 列那两颗按钮不是 layui 的 `lay-event` 按钮（对标的 templet 里没有 `lay-event`），
   * 所以走 `root` 上的**事件委托**，而不是 `table.on('tool')`。
   *
   * 点图标 `<i>` 也会命中（`closest` 往上找 `button`）。
   * ⚠️ `title` 属性（`上传截图` / `查看截图`，证据件原文）**保留不动**——它是按钮的 tooltip，
   * 与弹层的 `title`（`上传订单截图` / `查看订单截图`）是两个不同的值，⛔ 别改成同一个。
   * ⚠️ 同列另外两处可点击落点（`.orderdetails` / `.setdlsta`）**不在这里**：它们各是一个 `<a>`，
   * 由下面的 `onCellClick` 处理（见文件头第 5 条）。
   *
   * **层参数逐项取本页自己的** `myorderlist.js` **L1432-1460** 的 PC 分支：
   *
   * ```js
   * $(document).on('click', '.upImg', function () {
   *     var ids = $(this).attr('data-id');
   *     layer.open({ title: i18nDataList.upImgPop, shade: [0.3,'#333'], shadeClose: true, type: 2,
   *                  skin: 'layui-layer-lan', anim: 8,
   *                  area: [device ? '70%' : '90%', device ? '85%' : '90%'],
   *                  content: 'UploadPic?orderId=' + ids });
   *     return false;
   * });
   * $(document).on('click', '.mangeImg', function () {
   *     var ids = $(this).attr('data-id');
   *     layer.open({ title: i18nDataList.mangeImgPop, shade: [0.3,'#333'], shadeClose: true, type: 2,
   *                  skin: 'layui-layer-lan', anim: 1,
   *                  area: ['80%', device ? '85%' : '500px'],
   *                  content: 'PicMange?orderId=' + ids });
   *     return false;
   * });
   * ```
   *
   * ⚠️ 标题是**本页的 i18n**（`upImgPop` / `mangeImgPop`，zh-CN 原文在 L460-461）＝
   * 「上传订单截图」/「查看订单截图」——**与订单管理页那两句硬编码中文一致**，但引用方式不同
   * （⛔ 别把两页的值抽成共用常量：那是两个文件各自的字面量）。
   * ⚠️ 与本页「进订单详情」那处**不同**：这两支**没有** `maxmin` 键（`myorderlist.js` 这里没写）
   * ⇒ ⛔ 不要照 `openOrderDetailLayer` 补一个 `maxmin: false`。
   * `device = isPc`（L680）⇒ 取 PC 值：上传 `['70%','85%']`、管理 `['80%','85%']`；
   * ⛔ **手机分支不接**（同本文件头第 10 条的口径：本项目只做桌面后台）。
   *
   * ⛔ **开层前的「单号形状闸」** `/^\d{20}$/`：`layer.open` 的 `title` 是 **HTML 上下文**，
   * `dataset` 会把实体解码回 `<`/`>`（与 `openOrderDetailLayer` 同一条理由）。
   * 不匹配 ⇒ 不开层 + 一句**固定文案**（⛔ 不拼那份不可信输入）。
   *
   * ⚠️ **一处如实登记的偏离**：对标从**顶层窗口**开层（盖住整栏），我们开在 island iframe 内。
   */
  const openScreenshotLayer = (
    page: string, title: string, anim: number, area: [string, string], orderNo: string,
  ): void => {
    if (!/^\d{20}$/.test(orderNo)) {
      msg('订单号格式不正确，无法打开订单截图')
      return
    }
    ;(layer.open as (o: unknown) => void)({
      title,
      shade: [0.3, '#333'],
      shadeClose: true,
      type: 2,
      skin: 'layui-layer-lan',
      anim,
      area,
      content:
        `${import.meta.env.BASE_URL}layui-island/page.html?page=${page}` +
        `&order_no=${encodeURIComponent(orderNo)}`,
    })
  }

  const onShotButtonClick = (event: Event): void => {
    const btn = (event.target as HTMLElement).closest<HTMLElement>(SHOT_BUTTONS)
    if (!btn) return
    const orderNo = btn.dataset.id ?? ''
    if (orderNo === '') return
    if (btn.classList.contains(SHOT_UPLOAD_CLASS)) {
      openScreenshotLayer(SHOT_UPLOAD_PAGE, '上传订单截图', 8, ['70%', '85%'], orderNo)
      return
    }
    openScreenshotLayer(SHOT_MANAGE_PAGE, '查看订单截图', 1, ['80%', '85%'], orderNo)
  }
  root.addEventListener('click', onShotButtonClick)

  /* ── 行内两处链接：订单详情**原地开层** + 改状态（修复轮 1 接上，见文件头第 5/10 条） ── */

  /**
   * ⭐ **「进订单详情」：原地开层装我们自己的复刻页**（T5，2026-09-22；对标
   * `myorderlist.js` **L1324-1351** 的 `viewOrderDetails(orderid, viewID)`）。
   *
   * ```js
   * function viewOrderDetails(orderid, viewID) {
   *     if (isPc) {
   *         layer.open({
   *             title: viewID + i18nDataList.viewOrderDetails,   // zh-CN = 「订单详情」(L456)
   *             shade: [0.3, '#333'], shadeClose: true, maxmin: false, type: 2,
   *             skin: 'layui-layer-lan', anim: 8, area: ['85%', '92%'],
   *             content: 'OrderView?orderId=' + orderid
   *         });
   *     } else { … maxmin:true + area ['95%','95%'] + layer.full(fulllayer) … }
   * }
   * ```
   *
   * 参数**逐项取本页自己的值**（PC 分支，L1326-1334）。⛔ **`area` 不许抽共用常量**：
   * 同一个产品里订单管理页 `85%×92%`、我的绩效 `60%×92%`、绩效账单 `1125px|80%×90%`、
   * 绩效管理页 `90%×92%`。
   *
   * ⛔ **手机分支有意不接**（见文件头第 10 条）：`isPc` 由 `layui.device().android/ios` 决定
   * （L674-677）；本项目适用范围只有桌面后台（`FRONTEND-REPLICA-STANDARD.md` §1），
   * 只给「层」补手机分支会造出「层全屏、列桌面的」半吊子。
   *
   * `content` 换成**我们自己的**复刻页（基址 `import.meta.env.BASE_URL`，⛔ 不写死 `/`；
   * 单号 `encodeURIComponent`）。⚠️ 对标那一行**没有** `viewType` / `tid`（与订单管理页不同）
   * ⇒ 本页没有要对齐的额外参数。
   *
   * ⛔ **开层前的「单号形状闸」** `/^\d{20}$/`——**与这条路由自己的 `(\d{20})` 约束同源**
   * （后端 `Order.order_no` 的 `lookup_value_regex = r"\d{20}"`）。**为什么必须有**：
   * `layer.open` 的 `title` 是 **HTML 上下文**，而 `link.dataset.*` 会把 attribute 实体
   * **解码回** `<`/`>` ⇒ templet 里那层 `escapeHtml` 在这条链上**不构成保护**。
   * 不匹配 ⇒ **不开层** + 一句**固定文案**（⛔ 不把那份不可信输入拼进来）。
   *
   * ⚠️ `title` 前缀 = `viewID`（本页填 `order_no`，见文件头第 9 条）也过一次 `escapeHtml`
   * ——HTML 上下文的**边界**上转义才有效。
   */
  const openOrderDetailLayer = (orderNo: string, viewNo: string): void => {
    if (!/^\d{20}$/.test(orderNo)) {
      msg('订单号格式不正确，无法打开订单详情')
      return
    }
    ;(layer.open as (o: unknown) => void)({
      title: `${escapeHtml(viewNo !== '' ? viewNo : orderNo)}订单详情`,
      shade: [0.3, '#333'],
      shadeClose: true,
      maxmin: false,
      type: 2,
      skin: 'layui-layer-lan',
      anim: 8,
      area: ['85%', '92%'],
      content:
        `${import.meta.env.BASE_URL}layui-island/page.html?page=order-orderview` +
        `&order_no=${encodeURIComponent(orderNo)}`,
    })
  }

  /**
   * `.orderdetails`（第 1 列的订单号蓝链）与 `.setdlsta`（第 9 列的状态徽标）都走 `root` 上的
   * 事件委托——它们是对标函数 templet 渲染出的 `<a>`，不是 layui 的 `lay-event` 按钮。
   *
   *  - `.orderdetails` → `openOrderDetailLayer(order_no, data-viewId)`：**原地开层**（T5）。
   *  - `.setdlsta` → `requestChangeStatus([那一行])`（⭐ 2026-09-28 接上，见文件头 12-(c)）：
   *    与工具栏那颗**同一个处理器**，所以「只有代练中的单、只能改待验收/问题单」两条判据只有一处。
   *    ⚠️ 这一行手上只有单号与状态两个值（徽标是我们自己的 templet 渲染的，`data-status` 是
   *    为此加的钩子）⇒ `requestChangeStatus` 的入参就只要求这两列。
   *    ⛔ 原句「`.setdlsta` → `notOpen('更改订单状态')`（本页没有状态弹窗）」**已作废**
   *    （原文按 §4 保留在此行）：现在有弹窗了，只是**受限两档**。
   */
  const onCellClick = (event: Event): void => {
    const target = event.target as HTMLElement
    const statusEl = target.closest<HTMLElement>('.setdlsta')
    if (statusEl) {
      const orderNo = statusEl.dataset.order ?? ''
      if (orderNo !== '') {
        requestChangeStatus([{ order_no: orderNo, status: statusEl.dataset.status ?? '' }])
      }
      return
    }
    const detailEl = target.closest<HTMLElement>('.orderdetails')
    if (detailEl) {
      const orderNo = detailEl.dataset.order ?? ''
      // `data-viewId`（对标 `viewID` = `res.orderIdView`）——属性名 HTML 解析后是小写
      // ⇒ dataset 的键是 `viewid`，不是 `viewId`。
      if (orderNo !== '') openOrderDetailLayer(orderNo, detailEl.dataset.viewid ?? '')
    }
  }
  root.addEventListener('click', onCellClick)

  /* ── 右键 `.orderLine`：对标那个 `$.contextMenu` 我们没实现 → 提示「尚未开放」 ── */

  /**
   * 对标 `myorderlist.js` L1614 给 `.orderLine` 挂了 `$.contextMenu`（items `viewDetails` /
   * `copyOrderID` / `viewPic`），本页没有这个菜单；而容器与链接的 `title` 又逐字照抄了对标的
   * 「鼠标右键看看~」。所以右键**拦下浏览器原生菜单**并提示「尚未开放」，不让那句 tooltip
   * 变成假承诺（见文件头第 8 条 / D-0025 §5：不置灰、不伪造写入）。
   *
   * ⚠️ 只在命中 `.orderLine` 时 `preventDefault`；页面别处的右键保持浏览器原生菜单。
   */
  const onContextMenu = (event: Event): void => {
    const target = event.target as HTMLElement
    if (!target.closest('.orderLine')) return
    event.preventDefault()
    notOpen('右键菜单')
  }
  root.addEventListener('contextmenu', onContextMenu)

  /* ── 筛选栏：**全部接上本地筛选**（业务方 2026-09-28 裁定；见文件头 12-(b)） ── */

  /**
   * 「搜索」按钮：按表单当前值重筛。
   * ⚠️ 对标 `form.on('submit(seachBtn)')` 做的是 `page = 1; reloadOrderList()`——那边真的发一次请求；
   * 我们是**本地重筛**（数据早在手上，见文件头 12-(b)），所以没有网络那一跳。
   * ⛔ 原句「筛选栏「搜索」提示尚未开放（视图不读业务筛选参数）」**已作废**（原文按 §4 保留在此行）。
   */
  ;(form.on as (e: string, cb: () => unknown) => void)('submit(seachBtn)', () => {
    renderFiltered()
    return false
  })

  /**
   * 加急单勾选：**即时生效**（对标 `myorderlist.js:1290-1293` 勾选后立刻 `reloadOrderList`），
   * 并照对标弹一句 `layer.tips`。
   * ⚠️ tips 的文案从元素的 `lay-tips` 读（证据件原文），⛔ **不在 TS 里重抄一遍中文**——
   * 两处各写一份就会漂移（`labelOf` 是同一条纪律）。
   * ⚠️ `#isJiajiCheck` 是**那个空的 `<span>`**（证据件 L230），不是 checkbox 自己——
   * 对标 `layer.tips(..., '#isJiajiCheck', ...)` 指的也是它（勾选框贴着一个尖角提示）。
   */
  ;(form.on as (e: string, cb: () => unknown) => void)('checkbox(viewIsJiaJi)', () => {
    const tip = root
      .querySelector<HTMLInputElement>('input[lay-filter="viewIsJiaJi"]')
      ?.getAttribute('lay-tips')
    if (tip !== null && tip !== undefined && tip !== '') {
      ;(layer.tips as (t: string, s: string, o?: unknown) => void)(tip, '#isJiajiCheck', {
        tips: 1,
        time: 2000,
      })
    }
    renderFiltered()
  })

  /**
   * 区服切换：**即时生效**（对标 `form.on('select(selectGame)')` 同样直接 `reloadOrderList`）。
   * ⚠️ 不返回 `false` 的话 layui 会继续走它自己的默认行为（这里没有别的手续，但保持一致）。
   */
  ;(form.on as (e: string, cb: () => unknown) => void)('select(selectGame)', () => {
    renderFiltered()
    return false
  })

  return () => {
    root.removeEventListener('click', onShotButtonClick)
    root.removeEventListener('click', onCellClick)
    root.removeEventListener('contextmenu', onContextMenu)
    root.innerHTML = ''
  }
}

export default {
  mount: (root: HTMLElement, ctx: IslandContext) => mountMyOrderListPage(root, ctx),
} satisfies IslandPage
