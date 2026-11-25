/**
 * 【我的收入】页的 **layui 复刻页 + 接真后端**（对标物 `/Emp/myemp`，**证据件渲染**）。
 *
 * 形状：单表列表页 —— 筛选栏 5 个可见 label（选择游戏 / 结算状态 / 订单状态 / 搜索1 / 搜索2）
 * + 工具栏 4 颗按钮（刷新列表 / 申请结算 / 导出记录 / 同步结算状态到我的系统）
 * + 表格 19 列（**hide 2**：扣点、第二个「系统内置单号」）+ `#toolbarDemo` 里的 `#total` 统计文案。
 *
 * ## 证据
 *
 * - 标记与页内 `<style>` → `public/replica/emp-myemp.html`
 * （源 `.tools/recon/suqing1/html/Emp__myemp.html`，laytpl 已解、非标记 `<script>` 已删）
 * - 列定义 → `.tools/recon/suqing1/assets/JS2019/Emp/myemp.js` 的 `table.render`（L588-747）：
 * `elem:'#tableList'`、`toolbar:'#toolbarDemo'`、`defaultToolbar:[]` 后又写
 * `defaultToolbar:['filter']`（**后者生效**）、`page:true, limit:20, limits:[10,20,30,40,50,100]`
 * - 中文 → 同文件内联 `i18nData.messages['zh-CN']`（L261-354）：本页 HTML 里没有中文，
 * 全是 `{{= i18n.$t('k') }}`，**字典是唯一出处**（不自编）。
 * - 单元格 templet / 工具栏 / 点击委托 → 同文件 L588-747（cols）与 L828-838（`.orderdetails`）。
 *
 * ## ⚠️ 两条只有读源码才能拿到的值
 *
 * 1. **`deductFee`（扣点）是 `hide:true`**：JS 写的是 `hide: !showDeductFee`，而
 * `showDeductFee = deductFeeType == 1 ? true : false`，`deductFeeType` 由本页行内
 * `<script>const deductFeeType = 0</script>`（对标 HTML L187-189）定成 **0**
 * ⇒ `showDeductFee = false` ⇒ **`hide: true`**。草稿把它导成 `hide: null`（读不出运行期取值），
 * 实测对标表头也是 17 个（19 − 扣点 − 第二个单号），本页按实际渲染写 `hide: true`。
 * 2. **i18n 字典里 `orderId` 定义了两次**（`'关联订单号'` L310 后被 `'系统内置单号'` L314 覆盖）
 * ⇒ 两列的表头都是「系统内置单号」，与实测一致。
 *
 * ## ⚠️ 分页
 *
 * 对标是本批里少数**显式** `page: true` 的页（其余多为未写 `page` = 分页关闭）。
 * 但本租户该表 **0 行**，layui 对 0 行的表不渲染 `.layui-laypage`，所以实测两侧都没有分页栏。
 *
 * ## ⚠️ 日期输入的 value（⛔ **改准 2026-11-02，D-0365——「保留」已过期**，原句按本仓惯例保留）
 *
 * （原句）`#sDate`/`#eDate` 的 `value` 是对标服务端按当天算出的默认查询区间（`2026-03-20 00:00:00` …
 * `2026-09-20 23:59:59`）。它不在 PII 清单里（不是姓名/手机号/账号/余额/IP/地点/租户名），
 * 且是这一页默认视图的一部分，故按对标原样保留（出处：对标 HTML L119/L123）。
 * **改准后的行为**：日期三控件从此是**真的发给后端的筛选**（见「本轮接上已实现的后端」的
 * D-0365 段）⇒ 照抄会让默认视图**永远停在过去**（那之后录的单看不见）——与 emplist
 * 「已知会不同」第 20 条**同一理由**，挂载时改用 `@/layui-island/dateRange::defaultDateRange`
 * 重算（今天 − **6 个自然月** ~ 今天；偏移出处 = 证据件 L119/L123 的渲染值，见下面
 * 「日期输入的 value」一段与 `dateRange.ts` 的表格，⛔ 不是 emplist 那页的 3）。
 *
 * ## 本轮：接上**已实现**的后端
 *
 * 本页原来走 `artifactPage` 的声明式渲染（恒空表、所有点击都提示「尚未开放」）。本轮改成
 * **手写渲染器**（与 `order-myorderlist.ts` 同一形状：取证据件 → `root.innerHTML` →
 * `bootstrapIsland` → 自己 `table.render`），因为 `artifactPage` 有两处硬限制：
 * 1. 它的 `buildCol` 对没写 `templet` 的列一律填 `() => ''`（空表下看不出），且
 * `ArtifactCol.templet` 只接受**字符串选择器**——真数据下所有单元格都会是空的；
 * 2. 它的捕获阶段点击收口会把一切按钮都变成「尚未开放」，真动作没有落点。
 *
 * 列声明仍然是**同一份** `COLS` / `buildCols()`（兄弟 spec `emp-myemp.spec.ts` 的列断言读它，
 * **字节不许动**）；运行期列由 `buildRuntimeCols()` 从它派生，按下标补 `templet`。
 * ⚠️ `.tools/parity.mjs` **不读**这份声明（实测：全文只有注释里 `LAYTABLE_COLS` 命中 `COLS` 字样，
 * 它对标的是**渲染出来的 DOM**）⇒ 改坏列声明**不会**被 parity 自动抓到，靠的是那份兄弟 spec。
 *
 * | 对标落点 | 我们的接线 |
 * |---|---|
 * | 主表取数（`/EmpAjax/GetMyEmp`，L591 的 `table.render` url） | `GET /api/business/my-orders/`（`api/myOrders.ts` 的 `fetchAllMyOrders`） |
 * | 工具栏「刷新列表」`refresh` | 重新发上面那条 GET（`reload()`）——纯客户端刷新，不涉及后端能力 |
 * | ⭐ 工具栏「申请结算」`applicationBtn`（D-0365） | **`POST /api/business/my-orders/settle-requests/`**（批量申请提前结算；空选弹对标的 `toolbar` 句、逐条结果汇总、成功 ≥1 条才重取——见 `applySettlement`） |
 * | ⭐ 工具栏「导出记录」`exportBtn`（D-0365） | **`GET /api/business/my-orders/export`**（当前筛选打包成查询串，Blob 下载；**不看勾选**、不关当前页——见 `exportRecords`） |
 * | ⭐ 筛选栏日期三控件（`#dateType`/`#sDate`/`#eDate`，D-0365） | **服务端日期筛选**：四档过 `DATE_TYPE_MAP` 发 `date_type`，两端发 `date_from`/`date_to`（`dateFetchParams`）；改任一控件即重取 |
 * | 工具栏「同步结算状态到我的系统」`snycSettStatusBtn` | **「尚未开放」**：对标的语义是推到打手**自己的外部系统**对账，我方没有对应端点（挂账待业务方拍板）→ 点了提示，不发请求 |
 * | 行内订单号蓝链 `.orderdetails`（L828-843 `viewOrderDetails`） | **原地开层**（T5）：`layer.open` 装我们自己的复刻页（`order-orderview`），本页自己的 `60%×92%`——见「已知差异 14」与 `openOrderDetailLayer`。⛔ 不再走 `ctx.onNavigate` |
 * | 筛选栏 5 个控件（L583-587 的 `selectGame` 等） | **客户端过滤**：筛选在浏览器里做（日期三控件除外——它们是服务端筛选，见上） |
 *
 * ### 为什么「取数」不交给 layui 的 `url`
 *
 * layui 的 `table.render({url})` 自己发 XHR，**绕开 `api/http.ts`**：后端 403（无节点
 * `my_orders.send_to_my_system`）
 * 会变成「表格空了、什么都不说」——正是简报禁止的「点了没反应」。这里改由 `fetchAllMyOrders()`
 * 走 `request()`，403/业务码的 `msg` 能**原样**弹出来（`layer.msg(msg, {time:6000})`，
 * 与 `order-myorderlist.ts` 同值）。代价：分页与筛选都在**客户端**做（`page.limit`/`limits`
 * 照旧生效），数据一次取完（`MY_ORDERS_PAGE_SIZE = 200`；后端分页上限现为 300，见 `api/myOrders.ts`）。
 *
 * ## 本轮单元格复刻：与对标的**已知差异**（逐条登记，不静默改）
 *
 * 1. **5 个缺字段列保持空白**（载荷里没有对应字段，**不造值、不拿别的字段冒充**）：
 * 角色名（下标 8）/ 提交时间（13）/ 记录人（14）/ 清算时间（16）/ 结账方式（17）。
 * 要补必须先让后端序列化器回字段，**不是**前端能凭空造的。
 * （对标这几列的 templet 会渲染 `在线结算` 之类文案——我们**一个字都不写**。）
 * ⛔ **改准（2026-11-02，D-0365）——原句是「6 个」且含「是否已申请结算（18）」**：
 * 那一列从此**接上了**（`settle_requested_at` 自 2026-10-23 就在载荷里，本批接 templet；
 * 文案照对标的 `applicationSettTitcont1/2`：「已申请」/「未申请」，见下标 18 的注释）。
 * 原句按本仓惯例保留在上面。
 * 2. **扣点列（下标 4，`hide:true`）也留空**：`/my-orders/` 载荷没有 `deductFee`。
 * 该列默认隐藏；若用户用右上角「筛选列」把它勾出来，看到的是空列（登记）。
 * 3. **状态徽标色取本页对标的 switch**（`myemp.js` 里 `orderSta` 那个 templet 的
 * `switch (res.orderSta)`，**按枚举**）——**不与
 * `order-myorderlist.ts` 的色表合并**（D-0025 §10.3）：两页的 switch 各自不同
 * （同一「已暂停」在这里是 `#FF5722`、在「我的订单」页是 `#efaaaa`）。
 * 4. **状态文案用后端词表**（`@/config/orderStatus` 的「待抢单」）**而不是对标的「待上号」**
 * （对标那个 `switch` 是按**中文文案** case 的）——业务方裁定：同一状态在**任何页面同一文案**。
 * 筛选下拉那一档也跟着改了，见第 13 条。
 * 5. **结算状态列渲染后端真值（`未结账`/`已结账`）而不是对标的 `未结算`/`已结算`**：
 * 徽标结构（`.lableb.setsta` + `#9ACD32`/`#CDC8B1`）照对标，**文案取真值**。
 * 6. **订单号列只渲染蓝链，没有对标的「删」徽标**：对标按 `res.orderIsDel == "Y"` 插一颗
 * 「删」徽标（`myemp.js` L633-638），我们的载荷没有这个字段 ⇒ 无数据不造徽标。
 * 另外对标的 `data-id` 装的是**订单主键**（`res.orderId`），`/my-orders/` 没有它
 * （载荷的 `id` 是**绩效行主键**，`api/myOrders.ts` 明确写不要拿它当订单标识）⇒
 * `data-id` 一律填 `order_no`（与 `order-myorderlist.ts` 同一登记偏差）。`data-order`
 * 是本页点击委托用的钩子（对标没有这个属性），值同样是 `order_no`。
 * 7. **对标的两颗状态徽标本身没有 click 处理器**（`myemp.js` 全文只有 L828 一处
 * `.orderdetails` 委托）⇒ 按对标**不接**、也不提示（照对标渲染，不无中生有一个动作）。
 * 8. **`#total` 里的 7 个数字恒 0，刻意不改**：对标那几个数来自**服务端聚合**（`myemp.js`
 * 的 `done` 回调里那一串 `res.total.*`，共 7 个），而 `/my-orders/` 只回绩效行、
 * **没有这个出口**——前端拿绩效金额去凑
 * 「共计 N 条账单 / 已结账 N 笔」就是**编数**。所以统计句保持既有骨架（`TOTAL_TEXT`），
 * 真数据只体现在**表格行**里。
 * 9. **没有节点 `my_orders.send_to_my_system` 的账号打开本页会看到后端 403 的 msg 原文**：
 * `/my-orders/` 的闸门是**节点**（`my_orders_views.py` 的 `MyOrdersViewSet.MY_ORDERS_PERMISSIONS`），
 * 这里**原样弹出**，不伪造、不换文案。
 * ⚠️ **2026-09-23 单轴化更正**：原文写「客服（`Role.CS`）打开本页会 403，闸门是 `Role.TAKER`」——
 * **两句都已作废**：职位门已删，而内置**客服**的默认种子含 `my_orders` 栏目 ⇒ 它有这个节点、
 * 能进本页（只是「我接的单」通常为空）。本页的边界仍然是**如实显示后端 msg**，不改判据。
 * 10. ~~**三个日期控件（`#dateType` / `#sDate` / `#eDate`）没有接线，也不会去猜口径**~~
 * **⇒ ⭐ D-0365改准：四档已接上**（原句按本仓惯例保留在下面）：
 * 后端 `GET /my-orders/` 同批收 `date_type`（四档：接手 = `claimed` / 交单 = `submitted` /
 * 录单 = `created` / 结算 = `settled`）+ `date_from` / `date_to`，列映射在后端
 * `MY_ORDER_DATE_FIELDS`——当年「映射有歧义」的前提已由这张列映射表消除
 * （四档各有自己的列，不再是「只有一个 `created_at` 可用」的处境）。
 * 前端映射见 `DATE_TYPE_MAP`，取数参数见 `dateFetchParams`。
 * （原句：对标那四档是**不同的时间语义**……而 `/my-orders/` 只回绩效行的 `created_at`，
 * 其余三档在载荷里**没有对应字段** ⇒ 映射是**真的有歧义**，所以**不做日期筛选**（不猜、
 * 不拿 `created_at` 冒充别的口径）。按 D-0025 §5 照对标渲染（**不置灰**），
 * 用户**改**任一日期控件时提示「尚未开放」，**不发任何请求**。）
 * 11. **`#total` 那句「默认显示最近6个月数据，若需扩大时间范围，可手动选择起始日期」是对标
 * 原文（`myemp.js` L744 的 `dataTotal8`）照搬的。⛔ **改准（2026-11-02，D-0365）——
 * 原句「我们这一轮的实际行为里没有这条：取数不带任何时间窗口、日期控件也没接线」已过期**
 * （原句按本仓惯例保留在下面）：日期控件已接上，取数默认带**今天 − 6 个自然月**的窗口
 * （`defaultDateRange(new Date(), 6)`，偏移出处 = 证据件 L119/L123；评审 F-1 改准——本注记上一版
 * 误写成 3 个月，那是 emplist 的值）⇒ 统计句的「6 个月」现在**恰好是我方默认窗口的
 * 实际长度**（巧合，不是照抄对标那句的服务端聚合语义——我们的窗口挂在四档日期口径上、
 * 由 `#dateType` 选列），统计句保留原文是为了与对标逐字一致，**它仍不是我们行为的
 * 精确描述**（真行为见上一条与 `dateFetchParams`）。
 * （原句：取数不带任何时间窗口（`/my-orders/` 只认 `page`/`page_size`），6 个月是
 * **标称值**，日期控件也没接线（见上一条）。保留原文是为了统计句与对标逐字一致，
 * **但它不是我们的行为描述**。）
 * 12. **「选择游戏」那档里「全部游戏」出现了两次**（证据件 `option value="0"` 与 `value="21714"`
 * 的文案相同）——这是对标自己的脏数据。我们按**文案**判「全部」哨兵：两档都当不过滤。
 * 若只按 `value==='0'` 判，选中第二档会静默过滤掉所有行（登记，见 `selectedGameName`）。
 * 13. **证据件筛选下拉的「待上号」改成「待抢单」**（`public/replica/emp-myemp.html` 的
 * `select[name=orderSta]` 第一档，**文案与 value 都改**）：业务方裁定
 * 「同一个状态在任何页面、任何可见文案里必须同一词」，`order-orderlist` / `order-myorderlist`
 * 两页的证据件已按同一理由改过，本页当时漏了。
 * ⚠️ 与本页**已接**的筛选逻辑的关系：订单状态筛选把 option value（中文文案）**反查**
 * `ORDER_STATUS_LABELS` 得枚举名 ⇒ 改成「待抢单」后**通用反查就能命中**（不再需要别名表），
 * 行为**中性**；这是对证据件的**有意偏差**，不静默改（原先要一条 `待上号 → WAITING_GRAB`
 * 别名，现在删掉了）。有 spec 钉住这一档的文案/value 与 7 档的逐档映射。
 * 14. ⭐ **点订单号 ≡ 原地开层（T5），不再是 vue-router 跳转**：对标出处是**本页自己的**
 * `myemp.js` **L828-843**（`.orderdetails` 委托 L828-831 → `viewOrderDetails(orderid)` L832-843）。
 * 参数逐项照抄：`title: orderid + i18nDataList.viewOrderDetails`（zh-CN 的 `viewOrderDetails`
 * = **「订单详情」**，同文件 L352）、`shade:[0.3,'#333']`、`shadeClose:true`、`type:2`、
 * `skin:'layui-layer-lan'`、`anim:8`、`area:['60%','92%']`。
 * ⚠️ **对标这个函数体里没有 `maxmin` 这个键**（`myemp.js` L833-842 逐行核过）⇒ 我们**也不写**
 * （它落到 layui 的默认值上，与对标同一个默认）。⛔ 别照别的页面的形状顺手补一个 `maxmin:false`。
 * ⛔ **`area` 是每页自己的值，不许抽共用常量**：同一个产品里订单管理页 `85%×92%`、
 * 我的订单 PC `85%×92%`、绩效账单 `1125px|80%×90%`、绩效管理页 `90%×92%`。
 * `content` 换成**我们自己的**复刻页（基址 `import.meta.env.BASE_URL`、单号 `encodeURIComponent`）；
 * 对标那一行是 `content:'/Order/OrderView?orderId=' + orderid`——**没有** `viewType` / `tid`。
 * ⛔ **开层前的单号形状闸** `/^\d{20}$/` + 固定文案；`title` 前缀过 `escapeHtml`（理由与 T4
 * 绩效页同口径：`layer.open` 的 `title` 是 HTML 上下文、`dataset` 会把实体解码回 `<`/`>`）。
 * ⚠️ **一处如实登记的偏离**：对标从**顶层窗口**开层（盖住顶栏整屏）；我们开在 island iframe 内
 * （只盖内容区）——本页跑在 iframe 里、Vue 外壳没有 layui。
 *
 * ## 筛选口径（5 个控件，全部客户端）
 *
 * 选择游戏按 **option 文案**对 `game_name`；结算状态按 option value（`N`/`Y`）翻成后端
 * `SettleStatus` 的 `未结账`/`已结账`；订单状态按 option value（中文文案，**7 档逐档都能**
 * 反查出枚举名）翻成 `OrderStatus`；搜索1/搜索2 分别按
 * `order_no` / `project_text` 子串（大小写不敏感）。5 个条件**同时生效**（AND）。
 *
 * 触发时机：工具栏「刷新列表」只重新取数（不清空筛选）；「搜索」按钮与选择游戏的下拉
 * 变化按当前筛选值重渲染（**回第 1 页但保留用户选的「条/页」**）；其余 3 个下拉
 * （结算状态 / 订单状态 / 搜索2 输入）随「搜索」一起生效——对齐对标（`myemp.js` L583 只给
 * `selectGame` 挂了即时 reload，其余都靠搜索按钮）。
 *
 * ⚠️ **日期控件（`#dateType`/`#sDate`/`#eDate`）不在这 5 个之内**：它们是**服务端**筛选
 * （⭐ D-0365，2026-11-02 接上，见「已知差异」第 10 条）——每次取数（`reload()`）带当前值、
 * 改任一控件即重取；客户端「搜索」按钮只刷 5 个客户端筛选，**刷不到日期**（改日期自己会重取）。
 */
import {
  applyForSettlement,
  downloadMyOrderExport,
  fetchAllMyOrders,
  type FetchAllMyOrdersParams,
  type MyOrderItem,
} from '@/api/myOrders'
import { defaultDateRange } from '@/layui-island/dateRange'
import { ORDER_STATUS_LABELS, type OrderStatus } from '@/config/orderStatus'
import type { ArtifactCol } from '@/layui/artifactPage'
// HTML 转义的**共享唯一实现**（`layui-island/escape.ts`，与 `order-myorderlist.ts` /
// `system-sourelist.ts` / `order-addorder.ts` 同一出处；本页不再自带一份副本）。
import { escapeHtml } from '@/layui-island/escape'
import {
  bootstrapIsland,
  initIslandElements,
  islandMsg,
  type IslandContext,
  type IslandPage,
} from '@/layui/island'
import type { LayuiNamespace } from '@/layui/loader'
import { getFormSelects, initFormSelects } from '@/layui/vendor'

/** 证据件（页内 `<style>` + body 标记，零转义）。 */
const ARTIFACT_URL = 'replica/emp-myemp.html'
/** 对标 `<title>` 是服务端渲染的「我的收入」（`Emp__myemp.html` L5）。 */
const TITLE = '我的收入'
/** 对标 `table.render` 的 `elem`（`myemp.js` L589）。 */
export const TABLE_ELEM = '#tableList'
/** 对标 `toolbar` 选择器（`myemp.js` L593）。 */
export const TOOLBAR_ELEM = '#toolbarDemo'
/** 对标 `limit`（`myemp.js` L740）。 */
export const PAGE_LIMIT = 20
/** 对标 `limits`（`myemp.js` L741，**6 档**）。 */
export const PAGE_LIMITS = [10, 20, 30, 40, 50, 100]

/** `layui.table` 的实例 id（`table.reload` 的第一参；传 `#tableList` 是静默 no-op）。 */
const TABLE_ID = 'tableList'

/**
 * 工具栏里**真的「尚未开放」**的事件名（逐字取证据件 `#toolbarDemo` 的 `lay-event`）。
 * ⛔ **改准（2026-11-02，D-0365）——原先是 3 颗**（`applicationBtn` / `exportBtn` /
 * `snycSettStatusBtn`，原句按本仓惯例保留在下面）：申请结算与导出记录从此**接上了**
 * （后端 `POST /my-orders/settle-requests/` 与 `GET /my-orders/export` 已实装），
 * 「尚未开放」的只剩**「同步结算状态到我的系统」这一颗**——对标的语义是推到打手
 * **自己的外部系统**对账，我方打手没有外部系统、没有端点可接（挂账待业务方拍板）。
 * （原句：`['applicationBtn', 'exportBtn', 'snycSettStatusBtn']`。）
 * 用途见 `toolbar()` 处理器：**只有这一个**会弹提示，其余事件（layui 内置的
 * `LAYTABLE_COLS` 等）静默放行——不要退回 `else` 兜底。
 */
export const TOOLBAR_NOT_OPEN_EVENTS: readonly string[] = ['snycSettStatusBtn']

/**
 * ⭐ D-0365：空选时点「申请结算 / 导出记录 / 同步结算状态」的提示文案。
 * **出处 = 证据件 i18n 字典**（`myemp.js` L351 的 `toolbar: '请选中要操作的记录'`，
 * zh-CN 值；对标三颗按钮的空选分支都弹它）⇒ 逐字照抄，⛔ 不自拟同义句。
 */
export const EMPTY_SELECTION_TEXT = '请选中要操作的记录'

/**
 * ⭐ D-0365：日期口径下拉（`#dateType`）的证据件数字档 → 后端
 * `date_type` 的自描述取值（`MyOrderPageParams.date_type`；列映射在后端
 * `MY_ORDER_DATE_FIELDS`）。⛔ **不是**把对标的数字直接发给后端（那会回业务码 `1000`）。
 *
 * | 档 | `#dateType` | `date_type` | 后端打的列 |
 * |---|---|---|---|
 * | 接手时间 | `10` | `claimed` | `Performance.claimed_at`（**可空**） |
 * | 交单时间 | `20` | `submitted` | `Order.submitted_at`（**可空**） |
 * | 录单时间 | `25` | `created` | `Order.created_at` |
 * | 结算时间 | `30` | `settled` | `Performance.settled_at`（**可空**） |
 *
 * （中文文案出处：`myemp.js` L265-268 的 zh-CN 字典 `dateType1..4`。）
 * ⚠️ 形状照 `emp-emplist.ts::DATE_TYPE_MAP` 同款（两页档位集合不同，刻意各写一份——
 * 那边是绩效页的五档，这边是收入页的四档，共用一张表会把两页绑死）。
 */
export const DATE_TYPE_MAP: Record<
  string,
  'claimed' | 'submitted' | 'created' | 'settled'
> = {
  '10': 'claimed',
  '20': 'submitted',
  '25': 'created',
  '30': 'settled',
}

/** 「非空但取不出日期」那一句的模板（`<档名>` 由调用方填；文案照 emplist 同句）。 */
export const UNPARSABLE_DATE_TEXT = '不是可识别的日期，本次已忽略该筛选'

/** 错误提示的停留时长：**6000ms**，与 `order-myorderlist.ts` 同值（权限/业务失败要看清楚）。 */
const ERROR_MSG_MS = 6000

/**
 * 表头 19 列，**逐条照抄** `myemp.js` L595-738（`field`/`title`/`width`/`align`/`hide`/`fixed`）。
 *
 * `title` 里那几颗 `<i class="layui-icon layui-bangzhu" lay-tips="…">` **照抄**：
 * 实测对标这几颗图标 `::before` 的 `content` 是 `none`（没有字形、宽 0），
 * 所以它们不贡献任何可见文本（`innerText` 仍是「预计收入」），但结构与 `lay-tips` 原文要留着。
 */
export function buildCols(): ArtifactCol[] {
  return [
    // `{ type:'checkbox', fixed:'left', width:30 }`
    { type: 'checkbox', fixed: 'left', width: 30 },
    { field: 'userName', title: '用户名称', width: 90, align: 'center' },
    {
      field: 'tempAmount',
      title:
        '预计收入<i class="layui-icon layui-bangzhu" lay-tips="代表预计会获得的订单收入"></i>',
      width: 80,
      align: 'center',
    },
    {
      field: 'bonusAmount',
      title:
        '实际收入<i class="layui-icon layui-bangzhu" lay-tips="表示最终您获得的订单收入，只有当您亲自提交了完单成后该金额才会生效"></i>',
      width: 80,
      align: 'center',
    },
    // `hide: !showDeductFee` + `deductFeeType = 0` ⇒ **true**（见文件头注释第 1 条）
    { field: 'deductFee', title: '扣点', width: 80, align: 'center', hide: true },
    { field: 'empType', title: '收入类型', width: 70, align: 'center' },
    // 第 7 列：带 templet（关联订单号 + 删除徽标），**可见**，宽 145
    { field: 'orderId', title: '系统内置单号', width: 145 },
    // 第 8 列：**与上一列同 field**（对标原文就这样写），`hide:true`，宽 150
    { field: 'orderId', title: '系统内置单号', width: 150, hide: true },
    { field: 'gameRole', title: '角色名', width: 120 },
    { field: 'gameName', title: '游戏名称', width: 80, align: 'center' },
    // `dlProject` 没有 `width` ⇒ 自动列（由「表宽 − 定宽列之和」兜底），**不要补 width**
    { field: 'dlProject', title: '代练项目', align: 'left' },
    { field: 'orderSta', title: '订单状态', width: 70, align: 'center' },
    { field: 'createTime', title: '记录时间', width: 80, align: 'center' },
    { field: 'completeTime', title: '提交时间', width: 80, align: 'center' },
    { field: 'logUserName', title: '记录人', width: 90, align: 'center' },
    { field: 'settingSta', title: '结算状态', width: 70, align: 'center' },
    { field: 'settingTime', title: '清算时间', width: 70, align: 'center' },
    { field: 'empPayId', title: '结账方式', width: 70, align: 'center' },
    {
      field: 'applicationSett',
      title:
        '是否已申请结算<i class="layui-icon layui-bangzhu" title="你可以主动申请结算哪些订单。操作步骤：1、勾选要清算的订单；2、点击列表头部的申请结算按钮"></i>',
      width: 110,
      align: 'center',
    },
  ]
}

/** 表头的成品（`buildCols()` 的求值结果）——给 spec 用。 */
export const COLS: ArtifactCol[] = buildCols()

/**
 * `#total` 的文案骨架，**逐字**由 `myemp.js` L744 的 `done` 回调拼接
 * （`i18nDataList.dataTotal1..8`，见 `myemp.js` L343-350）。
 *
 * 对标那张表在本租户是 **0 行**，所以 7 个数字槽实测都是 `0`；我们把同一份骨架按 0 写死
 * （后端没有 `res.total.*` 这个出口，见文件头登记第 8 条）。
 */
export const TOTAL_TEXT = [
  '您接手过的订单，都会在此产生账单，本次筛选条件下共计',
  '0',
  '条账单，总计(不含已退单)：预计收入',
  '0',
  '元，实际收入',
  '0',
  '元，已结账',
  '0',
  '笔（',
  '0',
  '元）,还未结账',
  '0',
  '笔（预计有',
  '0',
  '元），默认显示最近6个月数据，若需扩大时间范围，可手动选择起始日期',
].join('')

/**
 * 后端 `SettleStatus`（`apps/business/models.py` 的 TextChoices）的两个值——前端镜像，
 * 与 `@/config/orderStatus` 同一手法。**只用于本页**（仓库里没有全局的 settle 词表）。
 */
export const SETTLE_STATUS_UNSETTLED = '未结账'
export const SETTLE_STATUS_SETTLED = '已结账'

/**
 * 本页状态徽标色表——**按状态枚举**取（不是按中文文案）。
 *
 * 逐条取本页对标 `myemp.js` 里 `orderSta` templet 的 `switch (res.orderSta)`
 * （对标的 case 是中文文案 `i18nDataList.orderStaValue2..8`，我们按枚举对齐）：
 * `orderStaValue2 待上号→WAITING_GRAB`、`3 代练中→IN_PROGRESS`、`4 待验收→PENDING_REVIEW`、
 * `5 已完成→COMPLETED`、`8 已暂停→PAUSED`、`6 问题单→ISSUE`、`7 已退单→REFUNDED`。
 *
 * ⚠️ **不要与 `order-myorderlist.ts` 的 `STATUS_BADGE_COLORS` 合并**
 * （FRONTEND-REPLICA-STANDARD §10.3）：两页的 switch 各自不同，同一状态色值也不同。
 * ⚠️ `UNASSIGNED`（未分配）不在表里：对标那个 `switch` 也没有它 → 走 default。
 */
export const STATUS_BADGE_COLORS: Record<string, string> = {
  WAITING_GRAB: '#585858', // orderStaValue2「待上号」那一档（文案我们统一用「待抢单」）
  IN_PROGRESS: '#219227', // orderStaValue3「代练中」
  PENDING_REVIEW: '#ff6a00', // orderStaValue4「待验收」
  COMPLETED: '#0094ff', // orderStaValue5「已完成」
  PAUSED: '#FF5722', // orderStaValue8「已暂停」
  ISSUE: '#ff0000', // orderStaValue6「问题单」
  REFUNDED: '#efaaaa', // orderStaValue7「已退单」
}

/** 徽标色兜底：对标 `switch` 的 `default`，**带尾分号**（照抄；拼进 `style` 后会出现两个分号）。 */
export const STATUS_BADGE_FALLBACK = '#b0aeae;'

/**
 * 订单状态 → 中文。**本页的**唯一出处是 `@/config/orderStatus`（后端 `OrderStatus` choices 的镜像）；
 * 未知值原样显示。
 *
 * ⚠️ 只说「本页」：仓库里还有别的自带状态文案表（如 `emp-emplist.ts` 的同值副本），
 * 所以「全仓唯一出处」会被一个反例证伪（E-0011），不要那样写。
 */
export function statusText(status: string): string {
  return ORDER_STATUS_LABELS[status as OrderStatus] ?? status
}

// 单元格文本转义用**共享实现** `@/layui-island/escape`（import 在文件头），本页不再自带副本：
// `templet` 的返回值被 layui **当 HTML 插入**，而 `project_text` / `game_name` 来自录单输入，
// 不转义就是存储型 XSS（对标自己也没转义，但那不是我们照抄缺陷的理由）。

/** 记录时间格式化成 `YYYY-MM-DD HH:mm`（与 `order-myorderlist.ts` / 管理列表同口径）。 */
function formatTime(value: string): string {
  return value.replace('T', ' ').slice(0, 16)
}

/**
 * `'2026-03-20 00:00:00'` → `'2026-03-20'`（取不出来返回 `''`，让那个参数不发出）。
 * ⚠️ **与 `emp-emplist.ts::dateTimePart` 有意分叉**（2026-11-07 起）：emplist 两端原样透传
 * `YYYY-MM-DD HH:mm:ss`（绩效列表后端两格式都收）；本页维持只取日期——my-orders 端点的
 * date_from/date_to 是独立 DateField、只收 date-only，本页也没有时间选择器。
 * 两页各自有一份、不互相 import（page 模块间只共享 `PAGE_KEY` 这类键常量）。
 */
function dayPart(value: unknown): string {
  if (typeof value !== 'string') return ''
  const m = /^(\d{4}-\d{2}-\d{2})/.exec(value.trim())
  return m ? m[1]! : ''
}

/**
 * 单元格渲染：**按下标**（不是按 `field`）对应列，因为 `COLS` 里有两个同名字段
 * （`orderId` ×2）。
 *
 * ⚠️ **只填后端 `MyOrderSerializer` 真的回了的列**；下标 4 / 8 / 13 / 14 / 16 / 17
 * （扣点 / 角色名 / 提交时间 / 记录人 / 清算时间 / 结账方式）载荷里没有字段
 * ⇒ **留空**，不猜、不拿别的字段冒充（见文件头登记第 1、2 条）。
 * ⛔ **改准（2026-11-02，D-0365）**：下标 18（是否已申请结算）**不再留空**——
 * 后端 `settle_requested_at` 自 2026-10-23 起就在载荷里，本批接上（见下标 18 的注释）。
 *
 * 对标那几处是**函数 templet**（L600-734），本轮按对标补齐；插值一律过 `escapeHtml`。
 */
const CELL_TEMPLETS: Record<number, (d: MyOrderItem) => string> = {
  // 1 用户名称（对标 L601-605 的 `<span>res.userName</span>`）。`username` 可为 `null`
  //（绩效行还没有归属人）⇒ 渲染成空串，不显示 `null`。
  1: (d) => `<span>${escapeHtml(d.username ?? '')}</span>`,
  // 2 预计收入（L606-611）：红色（`#FF5722`）+ 对标 `tempAmountTips`。
  2: (d) =>
    '<span style="color:#FF5722"' +
    ' lay-tips="预计收入金额，注意，如果你没亲自提交完单，此金额可能无效">' +
    `${escapeHtml(d.temp_amount)}</span>`,
  // 3 实际收入（L612-617）：红色 + 对标 `bonusAmountTips`。
  3: (d) =>
    '<span style="color:#FF5722"' +
    ' lay-tips="交单后才作数的收入金额，提示：商家大部分都是以此金额作为结算标准">' +
    `${escapeHtml(d.bonus_amount)}</span>`,
  /**
 * 5 收入类型（L618-631）：对标按**它自己的** `效绩提成`/`接单提成` 分色，而我们的后端值
 * 就是它显示出来的 `打单工资`/`销售提成`（`EmpType`，见 `apps/business/models.py`）。
 * 所以：`打单工资` → 对标 `empType1` 那一支的 `#E03636`；其余（含 `销售提成` 与未知值）
 * → `empType2` / `else` 那一支的 `#5FB878` + 原值（对标 else 也是原值）。
 */
  5: (d) => {
    const text = escapeHtml(d.emp_type)
    if (d.emp_type === '打单工资') return `<span style="color:#E03636">${text}</span>`
    return `<span style="color:#5FB878">${text}</span>`
  },
  /**
 * 6 系统内置单号（可见，L632-641）：蓝链 `.orderdetails`（`#0094ff`，`href` **带尾分号**）。
 * 对标的「删」徽标不渲染（载荷无 `orderIsDel`，见文件头登记第 6 条）；
 * `data-id` 与链接文本都填 `order_no`（对标 `data-id` 是订单主键，我们没有）；
 * `data-order` 是本页点击委托的钩子。
 */
  6: (d) => {
    const no = escapeHtml(d.order_no)
    return (
      `<a href="javascript:void(0);" class="orderdetails" data-id="${no}" data-order="${no}"` +
      ` style="color:#0094ff" title="点击查看订单详情">${no}</a>`
    )
  },
  // 7 第二个「系统内置单号」（`hide:true`，对标 L642-644 是裸 field）：真值仍是 `order_no`。
  7: (d) => escapeHtml(d.order_no),
  // 9 游戏名称（对标 L648 是裸 field，无 templet）。这里补 templet 只为统一走 `escapeHtml`。
  9: (d) => escapeHtml(d.game_name),
  // 10 代练项目（L649-653 的 `<span>res.dlProject</span>`）。
  10: (d) => `<span>${escapeHtml(d.project_text)}</span>`,
  /**
 * 11 订单状态（L654-690）：`.lableb.setdlsta` 徽标。对标 `title` 是
 * `setdlsta + res.orderSta`（「当前订单状态：」+ 值）；文案取 `statusText`（后端词表），
 * 色取**本页**色表（按枚举）。
 *
 * ⚠️ 对标 `data-id` 装的是 `res.orderId`（**订单主键**）——我们没有 ⇒ 填 `order_no`
 * （与 `order-myorderlist.ts` 同一登记偏差）。
 */
  11: (d) => {
    const no = escapeHtml(d.order_no)
    const text = statusText(d.status)
    const color = STATUS_BADGE_COLORS[d.status] ?? STATUS_BADGE_FALLBACK
    return (
      `<a href="javascript:void(0)" class="lableb setdlsta" data-id="${no}"` +
      ` title="当前订单状态：${escapeHtml(text)}" style="background-color:${color};">` +
      `${escapeHtml(text)}</a>`
    )
  },
  // 12 记录时间（L691-695 的 `<span>res.createTime</span>`）：统一格式化成 `YYYY-MM-DD HH:mm`。
  12: (d) => `<span>${escapeHtml(formatTime(d.created_at))}</span>`,
  /**
 * 15 结算状态（L705-711）：`.lableb.setsta` 徽标。对标按 `res.settingSta == 'Y'` 分色
 * （`#9ACD32` / `#CDC8B1`）并显示 `已结算`/`未结算`；我们的后端真值是 `已结账`/`未结账`
 * （`SettleStatus`）⇒ **结构取对标、文案取真值**（文件头登记第 5 条）。
 * 对标 `data-id` 是 `res.empId`，即**绩效行主键**（结算动作的对象）——我们的对应物就是
 * 载荷的 `id`（`Performance.id`）。⚠️ 这里**不是**订单 id 位，所以按它的真实语义填 `d.id`；
 * 不要把 `id` 当订单标识（订单一律 `order_no`，见 `api/myOrders.ts`）。
 */
  15: (d) => {
    const empId = escapeHtml(String(d.id))
    const settled = d.settle_status === SETTLE_STATUS_SETTLED
    const text = escapeHtml(d.settle_status)
    const title = settled ? '该笔订单已结算过' : '该笔订单还未进行结算'
    const color = settled ? '#9ACD32' : '#CDC8B1'
    return (
      `<a href="javascript:void(0)" class="lableb setsta" data-id="${empId}"` +
      ` title="${title}" style="background-color:${color};">${text}</a>`
    )
  },
  /**
 * 18 是否已申请结算（⭐ D-0365，2026-11-02 接上；此前该列留空——载荷没有字段，原句
 * 「一个字都不写」按本仓惯例保留在文件头登记第 1 条的更正注记里）。
 *
 * **产物逐字照对标**（`myemp.js` L731-740 的 `applicationSett` templet）：
 * `applicationSett == 'Y'` ⇒ `<span style="color:#E03636">已申请</span>`、
 * 否则 `<span style="color:#5FB878">未申请</span>`（文案出处 = 同文件 L341-342 的
 * `applicationSettTitcont1/2`，zh-CN 字典是唯一出处）。
 * ⚠️ **判据取我方真值**：`settle_requested_at` **有值 ⇒ 已申请**、`null` ⇒ 未申请
 * （后端对**作废（`cancelled`）的申请回 `null`** ⇒ 作废后这一格如实回到「未申请」，
 * 与 App 端按钮的同一口径——⛔ 不按「这个人有没有申请过」判）。
 * ⚠️ 值是**字面常量**（不含请求数据）⇒ 不转义也安全，但仍走 `escapeHtml` 统一出口。
 */
  18: (d) => {
    const applied = d.settle_requested_at !== null
    const text = escapeHtml(applied ? '已申请' : '未申请')
    const color = applied ? '#E03636' : '#5FB878'
    return `<span style="color:${color}">${text}</span>`
  },
}

/** 运行期列对象（`layui.table` 的 col）。`templet` 是本页相对 `COLS` 的唯一新增。 */
export type MyEmpCol = {
  type?: string
  field?: string
  title?: string
  width?: number | string
  minWidth?: number | string
  hide?: boolean
  align?: string
  fixed?: string
  /** **函数形式**：走我们自己的渲染，因此返回值里的用户输入必须转义（见 `escapeHtml`）。 */
  templet?: (d: MyOrderItem) => string
}

/**
 * 由 `COLS` 派生运行期列：**顺序/宽度/隐藏/对齐/固定一律照抄 `COLS`**（兄弟 spec
 * `emp-myemp.spec.ts` 钉的就是那一份；parity 不读它，见文件头），只按下标补 `templet`
 * （真数据的单元格渲染）。勾选列（下标 0）是 `type` 特殊列，**不补 templet**（layui 自己渲染勾选框）。
 */
export function buildRuntimeCols(): MyEmpCol[] {
  return COLS.map((c, i) => {
    const col: MyEmpCol = {}
    if (c.type !== undefined) col.type = c.type
    else col.templet = CELL_TEMPLETS[i] ?? (() => '')
    if (c.field !== undefined) col.field = c.field
    if (c.title !== undefined) col.title = c.title
    if (c.width !== undefined) col.width = c.width
    if (c.minWidth !== undefined) col.minWidth = c.minWidth
    if (c.hide === true) col.hide = true
    if (c.align !== undefined) col.align = c.align
    if (c.fixed !== undefined) col.fixed = c.fixed
    return col
  })
}

/** 5 个筛选控件的当前值（全部客户端）。空串 / 空数组 = 不设该条件。 */
export type MyEmpFilters = {
  /** 游戏名称（`game_name`）；`''` = 全部游戏。 */
  game: string
  /** 后端 `SettleStatus` 的 `未结账`/`已结账`；`''` = 不筛。 */
  settleStatus: string
  /** 选中的订单状态（枚举名）；空数组 = 全部。 */
  orderStatuses: OrderStatus[]
  /** 搜索1：`order_no` 子串（大小写不敏感）。 */
  key1: string
  /** 搜索2：`project_text` 子串（大小写不敏感）。 */
  key2: string
}

/**
 * 客户端筛选（5 个条件 AND）。
 *
 * ⚠️ 判据是 `!== ''` / `length > 0`，**不是真值**：`0` 与空串在这里语义不同
 * （`AGENTS.md` §5 的 E-0021 同族——「传了 0」不能被当成「没传」）。
 */
export function applyFilters(rows: MyOrderItem[], f: MyEmpFilters): MyOrderItem[] {
  const k1 = f.key1.trim().toLowerCase()
  const k2 = f.key2.trim().toLowerCase()
  return rows.filter((row) => {
    if (f.game !== '' && row.game_name !== f.game) return false
    if (f.settleStatus !== '' && row.settle_status !== f.settleStatus) return false
    if (f.orderStatuses.length > 0 && !f.orderStatuses.some((s) => s === row.status)) {
      return false
    }
    if (k1 !== '' && !row.order_no.toLowerCase().includes(k1)) return false
    if (k2 !== '' && !row.project_text.toLowerCase().includes(k2)) return false
    return true
  })
}

/**
 * 对标下拉的 `option value` 是**中文文案**（证据件 `select[name=orderSta]` 的 7 个状态档），
 * 我们的 `status` 是枚举名 ⇒ 要翻译。反查 `ORDER_STATUS_LABELS`（`@/config/orderStatus`，
 * 后端 `OrderStatus.choices` 的镜像），**不另建映射表**（与 `order-orderlist.ts` 的
 * `statusOfFilterValue` 同一手法）。
 *
 * ⚠️ 这里**没有**「待上号 → `WAITING_GRAB`」的别名了：证据件那一档已按业务方 2026-09-21 的
 * 裁定改成「待抢单」（见文件头登记第 13 条），7 档**逐档**都能被通用反查命中。
 */
function optionValueToStatus(value: string): OrderStatus | null {
  const keys = Object.keys(ORDER_STATUS_LABELS) as OrderStatus[]
  return keys.find((k) => ORDER_STATUS_LABELS[k] === value) ?? null
}

/** 结算状态下拉的 option value（`N`/`Y`）→ 后端 `SettleStatus` 的词。 */
const SETTLE_OPTION_VALUES: Record<string, string> = {
  N: SETTLE_STATUS_UNSETTLED,
  Y: SETTLE_STATUS_SETTLED,
}

/**
 * `#total` 的统计行（照 `myemp.js` **L744** 的拼接式重建）。数字恒 0，见文件头登记第 8 条。
 * 落点是工具栏模板里的 `<div style="color:#ff6a00" id="total">`（空 div）。
 */
function renderTotal(root: HTMLElement): void {
  const el = root.querySelector('#total')
  if (!el) return
  el.textContent = TOTAL_TEXT
}

/**
 * 挂载本页。**导出**是为了让测试能挂整页并断言真实出口（与 `order-myorderlist.ts` 的
 * `mountMyOrderListPage` 同形）；`default.mount` 只是它的薄包装。
 */
export async function mountMyEmpPage(
  root: HTMLElement,
  // ⛔ T5 起本页**不用** `ctx`：唯一那个「进订单详情」的入口改成了原地开层，本页没有跳路由的
  // 入口。参数保留是 `IslandPage.mount` 的签名位置（与其它 island 页同形）；下划线 =
  // 本仓 eslint 约定的「有意未用」（`eslint.config.ts` 的 `argsIgnorePattern: '^_'`）。
  _ctx: IslandContext,
): Promise<() => void> {
  document.title = TITLE
  /**
 * ⚠️ **先挂 layui 样式/脚本，再塞证据件标记**（2026-09-22 修）：`layui.css` 由 `bootstrapIsland()`
 * 运行期插 `<link>`，顺序反了会先画出没穿样式的骨架、再闪成正常（FOUC）。
 * 根因、两道兜底网与业务方报障详情见 `order-myorderlist.ts` 里同一处的长注释。
 */
  const layui: LayuiNamespace = await bootstrapIsland(['form', 'table', 'layer'])
  const res = await fetch(`${import.meta.env.BASE_URL}${ARTIFACT_URL.replace(/^\//, '')}`)
  if (!res.ok) throw new Error(`证据件取不到（HTTP ${res.status}）：${ARTIFACT_URL}`)
  root.innerHTML = await res.text()

  // 页签/折叠面板要等标记进来之后再绑定（见 `initIslandElements` 的注释）。
  initIslandElements(layui)
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
  /** 「尚未开放」的统一形状。 */
  const notOpen = (name: string): void => msg(`「${name}」尚未开放`)
  /** 从**页内标记**取按钮文案（不在 TS 里重抄一份中文，改名时不会漂移）。 */
  const labelOf = (selector: string, fallback: string): string => {
    const el = root.querySelector<HTMLElement>(selector)
    if (!el) return fallback
    // ⚠️ 图标字形会进 `textContent`：`<i class="larry-icon …"></i>申请结算` 直接取文本会多出
    // 一个私用区字符。去掉 `<i>` 再取文本，提示才是用户看到的「申请结算」。
    const clone = el.cloneNode(true) as HTMLElement
    for (const icon of Array.from(clone.querySelectorAll('i'))) icon.remove()
    const text = (clone.textContent ?? '').trim().replace(/\s+/g, ' ')
    return text === '' ? fallback : text
  }

  /** 当前表格的全部行（客户端筛选的数据源）。 */
  let rows: MyOrderItem[] = []

  /**
 * ⭐ D-0365：日期三控件**接上真筛选**之后，`#sDate`/`#eDate` 的默认值
 * 改为运行期计算（今天 − **6 个自然月** `00:00:00` ~ 今天 `23:59:59`，实现共用
 * `@/layui-island/dateRange`，偏移取本页自己的值）。
 * ⛔ **偏移是 6、不是 emplist 的 3（D-0365 评审 F-1 改准，原句按本仓惯例保留：本注记
 * 上一版写的「3 个自然月」是把 emplist 的偏移照抄过来了）**：证据件
 * `Emp__myemp.html` L119/L123 的渲染值是 `2026-03-20 00:00:00` / `2026-09-20 23:59:59`
 * （快照日 2026-09-20 ⇒ **6 个自然月**）；`dateRange.ts` 的 docstring 表格也早就
 * 警告过「各页偏移别互相照抄：`Emp__myemp` 是 6 个月」。
 * 理由与 emplist 第 20 条**逐字同款**：证据件里写死的两个 `value` 是对标服务端按**它**
 * 取数当天算的死值——它们过去只是摆设（点了弹提示），
 * 本批起是**真的发给后端的筛选**，照抄会让默认视图**永远停在过去**（那之后的单看不见）。
 * ⚠️ 必须在**首屏取数之前**写：首屏那次 `fetchAllMyOrders` 就吃这组值。
 */
  const defaultRange = defaultDateRange(new Date(), 6)
  const sDateInput = root.querySelector<HTMLInputElement>('input#sDate')
  const eDateInput = root.querySelector<HTMLInputElement>('input#eDate')
  if (sDateInput) sDateInput.value = defaultRange.sDate
  if (eDateInput) eDateInput.value = defaultRange.eDate

  /** 选择游戏：按 option **文案**对 `game_name`。 */
  function selectedGameName(): string {
    const sel = root.querySelector<HTMLSelectElement>('#gameName')
    if (!sel) return ''
    const opt = sel.selectedOptions[0]
    if (!opt) return ''
    const value = opt.value
    const text = (opt.textContent ?? '').trim()
    // 第一档（value `0`）是「全部游戏」哨兵。对标证据件里同一文案出现了两次
    // （value `0` 与 `21714`），按**文案**判哨兵：两档都当「全部」（否则选中第二档会静默过滤掉所有行）。
    const sentinel = (sel.options[0]?.textContent ?? '').trim()
    if (value === '' || value === '0' || text === sentinel) return ''
    return text
  }

  /** 结算状态：option value（`N`/`Y`）→ 后端 `SettleStatus`。未收录的 value 不筛。 */
  function selectedSettleStatus(): string {
    const value = root.querySelector<HTMLSelectElement>('#settingSta')?.value ?? ''
    return SETTLE_OPTION_VALUES[value] ?? ''
  }

  /**
 * 订单状态（多选）：优先读 formSelects 的当前值（对标用 `xm-select` 渲染），
 * 拿不到实例时退回原生 `<select>` 的选中项。取到的中文文案再翻成枚举名。
 */
  function selectedOrderStatuses(): OrderStatus[] {
    const values: string[] = []
    const fs = getFormSelects()
    if (fs) {
      const current = fs.value('orderSta', 'val')
      if (Array.isArray(current)) for (const item of current) values.push(String(item))
    } else {
      const sel = root.querySelector<HTMLSelectElement>('select[xm-select="orderSta"]')
      if (sel) for (const opt of Array.from(sel.selectedOptions)) values.push(opt.value)
    }
    const out: OrderStatus[] = []
    for (const value of values) {
      const status = optionValueToStatus(value)
      if (status !== null && !out.includes(status)) out.push(status)
    }
    return out
  }

  /** 读 5 个筛选控件的当前值。 */
  function readFilters(): MyEmpFilters {
    return {
      game: selectedGameName(),
      settleStatus: selectedSettleStatus(),
      orderStatuses: selectedOrderStatuses(),
      key1: root.querySelector<HTMLInputElement>('#key1')?.value ?? '',
      key2: root.querySelector<HTMLInputElement>('#key2')?.value ?? '',
    }
  }

  /**
 * 按当前筛选值重渲染：**回第 1 页**（换筛选条件后停在第 3 页会看到空表），
 * 但**保留用户选的「条/页」**。
 *
 * ⚠️ 每页条数取 layui 自己维护的**实时值**（`table.getOptions(id)?.page?.limit`），
 * 不要退回常量 `PAGE_LIMIT`：用户选的「条/页」被 laypage 就地写在 `config.page` 上，
 * 传常量会把用户的选择顶掉（E-0037 家族，修法与 `order-orderlist.ts` 的 `reload` 相同）。
 */
  function renderRows(): void {
    const getOptions = table.getOptions as
      | ((id: string) => { page?: { limit?: number } } | null)
      | undefined
    const liveLimit = getOptions?.(TABLE_ID)?.page?.limit ?? PAGE_LIMIT
    ;(table.reload as (id: string, o: unknown) => void)(TABLE_ID, {
      data: applyFilters(rows, readFilters()),
      page: { curr: 1, limit: liveLimit },
    })
  }

  /** 提示里那个档名从**`#dateType` 当前档的文案**取（证据件的 `option` 文本，不在 TS 里重抄中文）。 */
  function dateFilterLabel(): string {
    const sel = root.querySelector<HTMLSelectElement>('#dateType')
    const opt = sel?.selectedOptions[0] ?? sel?.options[0]
    const text = (opt?.textContent ?? '').trim()
    return text === '' ? 'dateType' : text
  }

  /**
 * ⭐ D-0365：读日期三控件的当前值 → **取数参数** + 要弹的警告。
 * - `#dateType` 过 `DATE_TYPE_MAP`（`hasOwnProperty` 判据，理由同 `dateTypeOf` 先例）；
 * 映射不到（怪值）⇒ **日期参数整个不发**（「只给日期不给档 ⇒ 不发日期参数」——
 * 与后端「`date_type` 缺省 ⇒ 日期不生效」的约定成对，⛔ 不猜默认列）。
 * - 两端取**日期段**（`dayPart`）；**非空但取不出日期** ⇒ 那一端会被静默丢掉
 * （E-0021 同族：结果集静默变宽）⇒ 照 emplist `hasUnparsableDate` 先例**多弹一句**
 * 「「<档名>」不是可识别的日期，本次已忽略该筛选」（两句合并进**一次** `msg`，
 * 先例同款——`layer.msg` 后来的会覆盖先前的，分次弹等于只看得到最后一句）。
 * - **只在映射到档时才收日期**：`params.date_type` 恒在（有档）、`date_from` / `date_to`
 * 有值才发（空框 = 用户清空 = 不筛那一端，不提示）。
 */
  function dateFetchParams(): { params: FetchAllMyOrdersParams; warnings: string[] } {
    const select = root.querySelector<HTMLSelectElement>('#dateType')
    const raw = select?.value ?? ''
    const tier = Object.prototype.hasOwnProperty.call(DATE_TYPE_MAP, raw)
      ? DATE_TYPE_MAP[raw]
      : undefined
    if (tier === undefined) return { params: {}, warnings: [] }
    const params: FetchAllMyOrdersParams = { date_type: tier }
    const warnings: string[] = []
    const fromRaw = (root.querySelector<HTMLInputElement>('#sDate')?.value ?? '').trim()
    const toRaw = (root.querySelector<HTMLInputElement>('#eDate')?.value ?? '').trim()
    const from = dayPart(fromRaw)
    const to = dayPart(toRaw)
    if ((fromRaw !== '' && from === '') || (toRaw !== '' && to === '')) {
      warnings.push(`「${dateFilterLabel()}」${UNPARSABLE_DATE_TEXT}`)
    }
    if (from !== '') params.date_from = from
    if (to !== '') params.date_to = to
    return { params, warnings }
  }

  /**
 * 重新取数（工具栏「刷新列表」、日期控件变化、申请结算成功后的重取都走它）。
 * 筛选值**不清空**——刷新数据不是重置筛选。
 * ⭐ D-0365：日期三控件是**服务端筛选** ⇒ 每次取数都带上它们的当前值
 * （「非空但取不出日期」的警告在这里弹，搜索与刷新都会经过本函数）。
 */
  async function reload(): Promise<void> {
    const { params, warnings } = dateFetchParams()
    if (warnings.length > 0) msg(warnings.join('；'))
    try {
      rows = await fetchAllMyOrders(params)
    } catch (e) {
      // ⚠️ 失败也要把表清空并**如实报错**：403（无节点 `my_orders.send_to_my_system`）/ 401 的 msg 由 `request()` 给出。
      rows = []
      errMsg(errText(e))
    }
    renderRows()
  }

  // 首屏取数：**先取再渲染**，避免「先出空表、再闪一下」；取数失败则空表 + 6000ms 错误提示。
  // ⭐ D-0365：首屏同样吃日期筛选——但**先把默认区间写进两个日期框**（见上面
  // `defaultDateRange(new Date(), 6)` 那段），否则证据件里写死的旧区间会把默认视图钉死在过去。
  let loadError: string | null = null
  try {
    rows = await fetchAllMyOrders(dateFetchParams().params)
  } catch (e) {
    rows = []
    loadError = errText(e)
  }

  ;(table.render as (o: unknown) => void)({
    elem: TABLE_ELEM,
    id: TABLE_ID,
    data: rows,
    toolbar: TOOLBAR_ELEM,
    // 对标同一个 render 对象里 `defaultToolbar` 写了两次，后写的 `['filter']` 生效。
    defaultToolbar: ['filter'],
    page: { limit: PAGE_LIMIT, limits: PAGE_LIMITS },
    cols: [buildRuntimeCols()],
    // 对标没写 `text` ⇒ 用 layui 默认的空态文案（**不自编**）。
    done: () => {
      ;(form.render as () => void)()
      renderTotal(root)
    },
  })
  ;(form.render as () => void)()
  if (loadError !== null) errMsg(loadError)
  // 对标 `myemp.js` 由 layui 的 formSelects 模块渲染 `select[name=orderSta][xm-select]`（1 个）。
  initFormSelects('select[xm-select]')

  /* ── 工具栏 4 颗按钮：刷新 / 申请结算 / 导出记录（⭐ D-0365 起三颗真功能）+ 同步（尚未开放）── */

  /**
 * 后端**没有**对应能力的 1 颗（逐条复核，不接）：
 * - `snycSettStatusBtn` 同步结算状态到我的系统 → 对标的语义是推到打手**自己的外部系统**
 * 对账（`myemp.js` 的 `synchEmpOrderToSystem`）；我方打手没有外部系统、没有端点可接
 * ⇒ **本线不实装**，点了照旧提示「尚未开放」（挂账待业务方拍板：裁剪 or 定义同步目标）。
 * ⛔ **改准（2026-11-02，D-0365）——原先 3 颗都「尚未开放」**（原句按本仓惯例保留在
 * `TOOLBAR_NOT_OPEN_EVENTS` 的注释里）：`applicationBtn` 申请结算与 `exportBtn`
 * 导出记录从此**接上**（见下面两个动作）。
 *
 * ⚠️ **只认这四颗 + `refresh`，其余事件一律静默放行**：layui 会用一个 `toolbar(id)` 事件把
 * `defaultToolbar` 里内置图标的点击也交上来（本页是 `['filter']` ⇒ 事件名 `LAYTABLE_COLS`，
 * 真实 DOM 见 `.tools/parity.mjs:255` 的实测注释）。对标那个 handler 的 `switch` **没有 default 分支**，
 * 所以点「筛选列」在对标上是**静默开面板**；我们若用 `else` 兜底弹「尚未开放」，
 * 就会把 layui 自己的功能说成未开放（本页**确实**开了 `defaultToolbar:['filter']`，图标是真的）。
 * 事件名清单就写在这里，别再用 `else`。
 */
  ;(table.on as (e: string, cb: (obj: { event?: string }) => void) => void)(
    `toolbar(${TABLE_ID})`,
    (obj) => {
      const event = obj.event ?? ''
      if (event === 'refresh') {
        void reload()
        return
      }
      if (event === 'applicationBtn') {
        void applySettlement()
        return
      }
      if (event === 'exportBtn') {
        exportRecords()
        return
      }
      if (!TOOLBAR_NOT_OPEN_EVENTS.includes(event)) return
      notOpen(labelOf(`.layui-table-tool [lay-event="${event}"]`, event))
    },
  )

  /** 勾选行的绩效 id（载荷的 `id` 就是 `Performance` 主键；结算动作的对象）。 */
  function checkedIds(): number[] {
    const checkStatus = (
      table as unknown as {
        checkStatus?: (id: string) => { data?: MyOrderItem[] } | undefined
      }
    ).checkStatus
    const checked = checkStatus?.(TABLE_ID)?.data ?? []
    return checked.map((row) => row.id)
  }

  /**
 * ⭐ D-0365：申请结算（`applicationBtn`）——**就是已有的「提前结算申请」**
 * （2026-10-23 spec §3.4，App 已在用；后端逐条过判据链，不改任何金额与状态）。
 *
 * - 空选 ⇒ 对标同款提示（证据件 i18n 字典的 `toolbar` 句，`myemp.js` L754-757 的
 * 空选分支就是弹它）且**零请求**。
 * - 逐条结果**汇总提示**：成功 N 条 + 失败逐条 `msg`（后端中文原文，**去重**后合并）；
 * ⛔ 不自己翻译 1035/1036 的文案。有失败 ⇒ 走 6000ms 的 `errMsg`（要看清）。
 * - **「已申请」的行不前端预拦**：后端逐条拒绝（1035「已申请过…」）即如实呈现
 * （预拦会造出第二套判据，必然漂移；这一条是**已知行为**登记）。
 * - 成功 ≥1 条才 `reload()`——让「是否已申请结算」列立即反映落库结果；全失败不重取
 * （数据没变，重取是假动作）。
 * - 提示句「已提交 N 条提前结算申请」是**我方自拟**（对标那一侧的申请走它自己的
 * `/EmpAjax/ApplicationEmp`，成功文案没有离线证据可抄）。
 */
  async function applySettlement(): Promise<void> {
    const ids = checkedIds()
    if (ids.length === 0) {
      msg(EMPTY_SELECTION_TEXT)
      return
    }
    try {
      const result = await applyForSettlement(ids)
      const okCount = result.items.filter((item) => item.ok).length
      const failureMsgs = [
        ...new Set(result.items.filter((item) => !item.ok).map((item) => item.msg)),
      ]
      const parts: string[] = []
      if (okCount > 0) parts.push(`已提交 ${okCount} 条提前结算申请`)
      parts.push(...failureMsgs)
      const text = parts.join('；')
      if (failureMsgs.length > 0) errMsg(text)
      else msg(text)
      if (okCount > 0) await reload()
    } catch (e) {
      errMsg(errText(e))
    }
  }

  /**
 * ⭐ D-0365：导出记录（`exportBtn`）——当前筛选打包成查询串 → 服务端 xlsx。
 *
 * - **范围 = 当前筛选，不是勾选**（对标 `exportOrder(orderID)` 的空选分支也照发
 * `seachParam` ⇒ 空选**不拦**；这与 emplist 导出的「不看勾选」同一判读）。
 * 5 个客户端筛选的服务端镜像 + 日期三参数（`exportQuery`），与列表取数**同源**。
 * - 触发下载照 `emp-emplist.ts::exportCurrent` 的形状（Blob 而不是 `window.open`，
 * 理由见 `downloadMyOrderExport` 的注释：同一个 URL 两种形状，失败信封的
 * `msg` 要能弹出来）。
 * - **不照抄「导出后关掉当前页」**（对标 `exportEmp` 导出后把自己那个页签关掉——
 * 管理壳 iframe 的用法；emplist「已知会不同」第 8 条同款理由）。
 */
  function exportRecords(): void {
    void downloadMyOrderExport(exportQuery())
      .then(({ blob, filename }) => {
        const url = URL.createObjectURL(blob)
        const link = document.createElement('a')
        link.href = url
        link.download = filename
        // ⚠️ 必须插进文档再 `click()`：Firefox 对游离节点不触发下载
        // （`URL.revokeObjectURL` 要等浏览器读完，所以放在下一轮任务里）。
        document.body.appendChild(link)
        link.click()
        link.remove()
        window.setTimeout(() => URL.revokeObjectURL(url), 0)
      })
      .catch((e: unknown) => errMsg(errText(e)))
  }

  /** 导出查询串：页面 5 个客户端筛选的**服务端镜像** + 日期三参数（键名见后端序列化器）。 */
  function exportQuery(): Record<string, string> {
    const filters = readFilters()
    const query: Record<string, string> = {}
    // 判据是「空」而不是「真值」（E-0021 同族）；前后端都 trim（后端 CharField 的
    // `trim_whitespace` 默认开 ⇒ 两侧同源性登记在 `MyOrdersExportSerializer`）。
    if (filters.game !== '') query.game = filters.game
    if (filters.settleStatus !== '') query.settle_status = filters.settleStatus
    if (filters.orderStatuses.length > 0) query.order_status = filters.orderStatuses.join(',')
    if (filters.key1.trim() !== '') query.key1 = filters.key1.trim()
    if (filters.key2.trim() !== '') query.key2 = filters.key2.trim()
    const { params } = dateFetchParams()
    if (params.date_type !== undefined) query.date_type = params.date_type
    if (params.date_from !== undefined) query.date_from = params.date_from
    if (params.date_to !== undefined) query.date_to = params.date_to
    return query
  }

  /* ── 筛选栏：5 个控件全部客户端过滤 ── */

  /** 「搜索」按钮：按当前筛选值重渲染（5 个条件一起生效）。 */
  ;(form.on as (e: string, cb: () => unknown) => void)('submit(seachBtn)', () => {
    renderRows()
    return false
  })
  /** 选择游戏的下拉变化即时重渲染——对标 `myemp.js` L583 的 `select(selectGame)` 就是这个时机。 */
  ;(form.on as (e: string, cb: () => unknown) => void)('select(selectGame)', () => {
    renderRows()
    return false
  })

  /* ── 日期控件（3 个）：⭐ D-0365 起接上真筛选（改任一控件 ⇒ 按新值重取数）── */

  /**
 * ⛔ **改准（2026-11-02，D-0365）——本段原先整段是「不接线、只弹『尚未开放』」**
 * （原句按本仓惯例保留在文件头登记第 10 条的更正注记里）：后端四档映射
 * （`MY_ORDER_DATE_FIELDS`）已实装 ⇒ 三控件改为真筛选。触发时机是**改了就重取**
 * （`reload()`；取数参数见 `dateFetchParams`）——这是与旧页「游戏下拉即时、其余靠
 * 搜索按钮」不同的一个新粒度：日期是**服务端**筛选，客户端「搜索」那把刷子刷不到它，
 * 改了不重取就会出现「屏幕上的日期与表里数据对不上」（v2 页的筛选全部即时生效，
 * 同一取向）。
 *
 * `#dateType` 是**空 `lay-filter`** 的 select（证据件原文），走 layui 的
 * `select()` 通道。⚠️ 同一个空通道还有「结算状态」下拉 ⇒ **必须按 `obj.elem.id` 认控件**，
 * 否则改结算状态也会误触发重取。
 */
  ;(form.on as (e: string, cb: (obj: { elem?: HTMLElement }) => unknown) => void)(
    'select()',
    (obj) => {
      if (obj.elem?.id === 'dateType') void reload()
      return false
    },
  )
  /**
 * 两个日期框在对标里由 `laydate.render` 接管（`myemp.js` L573-581）；我们**没有**引 laydate，
 * 所以它们就是普通文本框 ⇒ 用原生 `change`（失焦/回车提交时触发）接，同样触发重取。
 */
  const onDateInputChange = (event: Event): void => {
    const id = (event.target as HTMLElement).id
    if (id === 'sDate' || id === 'eDate') void reload()
  }
  root.addEventListener('change', onDateInputChange)

  /* ── 行内订单号蓝链：**原地开层**进订单详情（对标 L828-843 的 `viewOrderDetails`） ── */

  /**
 * ⭐ **「进订单详情」：原地开层装我们自己的复刻页**（T5，2026-09-22；对标
 * `myemp.js` **L832-843**）。
 *
 * ```js
 * function viewOrderDetails(orderid) {
 * layer.open({
 * title: orderid + i18nDataList.viewOrderDetails, // zh-CN = 「订单详情」(L352)
 * shade: [0.3, '#333'], shadeClose: true, type: 2,
 * skin: 'layui-layer-lan', anim: 8, area: ['60%', '92%'],
 * content: '/Order/OrderView?orderId=' + orderid
 * });
 * }
 * ```
 *
 * 参数**逐项取本页自己的值**（含 `area:['60%','92%']`）。
 * ⚠️ **对标函数体里没有 `maxmin` 这个键** ⇒ 我们**也不写**（落到 layui 与对标同一个默认值）。
 * ⛔ **`area` 不许抽共用常量**：同一个产品里订单管理页 `85%×92%`、我的订单 PC `85%×92%`、
 * 绩效账单 `1125px|80%×90%`、绩效管理页 `90%×92%`。
 *
 * `content` 换成**我们自己的**复刻页（基址 `import.meta.env.BASE_URL`，⛔ 不写死 `/`；
 * 单号 `encodeURIComponent`）。⚠️ 对标那一行**没有** `viewType` / `tid`。
 *
 * ⛔ **开层前的「单号形状闸」** `/^\d{20}$/`——**与这条路由自己的 `(\d{20})` 约束同源**
 * （后端 `Order.order_no` 的 `lookup_value_regex = r"\d{20}"`）。**为什么必须有**：
 * `layer.open` 的 `title` 是 **HTML 上下文**，而 `link.dataset.*` 会把 attribute 实体
 * **解码回** `<`/`>` ⇒ templet 里那层 `escapeHtml` 在这条链上**不构成保护**。
 * 不匹配 ⇒ **不开层** + 一句**固定文案**（⛔ 不把那份不可信输入拼进来）。
 *
 * ⚠️ `title` 的前缀（`orderid`，我们手上是 `order_no`）也过一次 `escapeHtml`——
 * HTML 上下文的**边界**上转义才有效。
 *
 * ⚠️ 同页另外两颗徽标（`.setdlsta` / `.setsta`）**不接**：对标自己也没有它们的 click 处理器
 * （`myemp.js` 全文只有这一处委托）⇒ 照对标渲染（见文件头登记第 7 条）。
 */
  const openOrderDetailLayer = (orderNo: string): void => {
    if (!/^\d{20}$/.test(orderNo)) {
      msg('订单号格式不正确，无法打开订单详情')
      return
    }
    ;(layer.open as (o: unknown) => void)({
      title: `${escapeHtml(orderNo)}订单详情`,
      shade: [0.3, '#333'],
      shadeClose: true,
      type: 2,
      skin: 'layui-layer-lan',
      anim: 8,
      area: ['60%', '92%'],
      content:
        `${import.meta.env.BASE_URL}layui-island/page.html?page=order-orderview` +
        `&order_no=${encodeURIComponent(orderNo)}`,
    })
  }

  /**
 * 第 6 列是函数 templet 渲染出的 `<a class="orderdetails">`（`data-id` / `data-order` 都是
 * `order_no`，见文件头第 6 条），不是 layui 的 `lay-event` 按钮，所以走 `root` 上的**事件委托**。
 */
  const onCellClick = (event: Event): void => {
    const link = (event.target as HTMLElement).closest<HTMLElement>('.orderdetails')
    if (!link) return
    const orderNo = link.dataset.order ?? ''
    if (orderNo !== '') openOrderDetailLayer(orderNo)
  }
  root.addEventListener('click', onCellClick)

  return () => {
    root.removeEventListener('click', onCellClick)
    root.removeEventListener('change', onDateInputChange)
    root.innerHTML = ''
  }
}

export default {
  mount: (root: HTMLElement, ctx: IslandContext) => mountMyEmpPage(root, ctx),
} satisfies IslandPage
