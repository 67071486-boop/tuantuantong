/**
 * 【内部接单大厅】页的 **layui 复刻页**（对标物 `/Order/insiderecorderlist`，**证据件标记 + 真 API**）。
 *
 * 形状：**单表列表页**（4 个可见筛选项 + 3 个 `layui-hide` 项 + 2 颗手写 `<a class="layui-btn">`
 * + `#total` 统计行 + 表格）。本模块把表格接到后端大厅读端点
 * `/api/business/hall/`（`business/urls.py` 显式注册，**不是** `orders/hall/`）。
 *
 * ## 本模块的行为口径（筛选 + 列表 + 分页 + 刷新 + **抢单**）
 *
 *  - 取数：`table.render({url, method:'get', request:{pageName:'page', limitName:'page_size'}})`
 *    ——分页参数名是后端的 `page` / `page_size`（`EnvelopePagination.page_size_query_param`），
 *    不是 layui 默认的 `page`/`limit`。
 *  - 筛选：页内**有控件**的后端筛选项 4 个（`game` / `server` / `keyword` / `project_text`）；
 *    `price_min`/`price_max` 后端支持但**对标页没有控件**，本页不发（差异表 #10）。
 *  - 刷新：`a.refresh` → `reload(1)`；搜索按钮（`form.on('submit(seachBtn)')`）与
 *    输入框回车（对标 `$(document).keyup(getKey)`）同。切换游戏会重建区服下拉再重载
 *    （对标 `form.on('select(selectGame)')`）。
 *  - **自动刷新频率（spec §3.3；⛔ 对标这一页没有，是新增不是复刻）**：在证据件那颗
 *    `.refresh` **之后运行期注入**一颗 `#autoRefreshBtn`（⛔ 证据件一个字不动），点开 7 档单选
 *    （关闭 / 1 / 2 / 5 / 10 / 30 / 60 秒），到点 `reload(currentPage)`（保持当前页与当前筛选）。
 *    四条副作用都在这里收口：**在途互斥**（上一发未回不重复发 + 30 秒活性兜底）、
 *    **页面隐藏暂停**（`visibilitychange` 变可见立刻补一发）、`localStorage` 记忆（`tt.hall.autoRefreshMs`，
 *    读写都 fail-safe）、**卸载清定时器与文档级监听**。见下面 `onTableDone` / `reload` / `onAutoRefreshTick`。
 *  - **抢单**：两处「接手此单」列（`actioncol` / `actioncol2`）都渲染对标的
 *    `<a class="layui-btn layui-btn-danger layui-btn-xs recThisOrder" data-id="<单号>">接手此单</a>`
 *    （`claimButton()` 一个出处）。点击 → `layer.confirm(CLAIM_CONFIRM_TEXT, fn)`（与对标
 *    `layer.confirm(text, fn)` 同形）→ 按 `data-origin` 分派到**三个不同端点** →
 *    成功 **`reload(currentPage)`**（就地刷新，不跳回第 1 页）＋提示「抢单成功」。
 *      · **原生行**（`data-origin="native"`）→ `POST /api/business/hall/<单号>/claim/`
 *        （`api/hall.ts::claimOrder`）；
 *      ⚠️ **`data-origin="remote"` 分支已整条删除**——后端不再产出 `origin="remote"` 行，
 *        那条 `/hall/remote/<来源>/<远端单号>/claim/` 端点与
 *        `api/hall.ts::claimRemoteOrder` 一并拆除（再留着就是一段命中 404 的死路）。
 *      · ⭐ **镜像行**（`data-origin="upstream"`，鸡蛋铺线）→
 *        `POST /api/business/hall/upstream/<镜像行主键>/claim/`
 *        （`api/jidanpuUpstream.ts::claimUpstreamMirror`）；它的 `data-id` 取行里的
 *        **`mirror_pk`**（⛔ 不是 `remote_order_id`：端点的 `<int:pk>` 按**本表主键**取行）。
 *      · ⭐ **上家团团通行**（`data-origin="peer"`，D-0303）→
 *        `POST /api/business/hall/peer/<镜像行主键>/claim/`（`api/hall.ts::claimPeerMirror`）；
 *        `data-id` 同样取 **`mirror_pk`**（端点的 `<int:pk>` 按 `PeerHallMirror` 的主键取行，
 *        ⛔ 不是上家单号 `remote_order_id`）。它是**确认制**：成功只代表「已受理」，正式订单
 *        要等上家指派 ⇒ 提示「正在受理中」+ **置灰不删卡**（⛔ 不刷新——刷了这张卡会直接消失）；
 *        `1012`（已被接走 / 正在处理中）才刷新；其余失败原样显示后端 `msg`、卡保留。
 *    ⚠️ **fail-closed（数据侧）**：`origin` 是 `upstream` / `peer` 而 `mirror_pk` 不是正整数
 *    ⇒ **一颗按钮都不画**（⛔ 不拿空值 / `null` / 上家单号去拼 URL 发一条注定打不中的请求）。
 *    另几源失败走 `msg(errText(e))`：HTTP 403 与业务码（1012 / 1013…）都**原样显示后端 `msg`**——
 *    403 走项目现有的统一分支（`api/http.ts` → `HttpError`），本页**不特判、不吞、不换文案**。
 *  - **两源行**（spec §4.6）：载荷 = 我方原生单 ∪ 鸡蛋铺镜像行（行形状同键）。渲染按
 *    `HallListItem.origin` 分支：镜像行的订单号列显示**远端展示单号**（回落远端单号），
 *    角色信息列用**远端文本区服**（`server` 是 `null`，⛔ 不查我方区服字典）。
 *  - **权限门（fail-closed，`AGENTS.md` §5-D / D-0154 / E-0098）**：挂载时取一次
 *    `/api/auth/me`，只有 `canNode(me, CLAIM_NODE)` 为真才画「接手此单」；
 *    取不到 / 没节点 ⇒ **一颗都不画**（⛔ 不是置灰、更不是「画了再 403」）。节点是
 *    `grab_hall.take_order`（原生与镜像接单**同一个**功能点），**不是**栏目码 `grab_hall`。
 *
 * 值一律从对标取（证据件与对标原件在 `public/replica/` 与 `.tools/recon/`，后者不进 git）。
 *
 * ## 与对标并排比对的差异（逐条原因，不造数据）
 *
 * | # | 差异 | 原因（证据等级） |
 * |---|---|---|
 * | 1 | 「类型」列恒空 | 载荷 `OrderListSerializer` 无 `levelMark`（读码） |
 * | 2 | 「角色名」列恒空 | 载荷无角色名（读码） |
 * | 3 | 「阵营/平台」列恒空 | 载荷无 `gameGroup`；游戏/区服显示在「角色信息」列（与 `order-orderlist` 同口径） |
 * | 4 | 「时限要求」列恒空 | 载荷无该字段；**对标自己的表头也是空的**（i18n key `actionorderHoursDescStrcol` 不存在） |
 * | 5 | 「保证金要求」列恒空 | 载荷无该字段（读码） |
 * | 6 | 「备注要求」列恒空 | `Order.remark` 在模型上，但不在列表序列化器里（读码） |
 * | 7 | 「参考图」列渲染对标的 `mangeImg` 按钮，点了看这单已上传的截图 | **已实装**（业务方 2026-10-23）：走 `GET /api/business/orders/<单号>/screenshots`（`apps/business/api/screenshot_views.py::ScreenshotCollectionView.list_items`，节点 `order.view_screenshot`；旧句「后端也没有大厅图片端点」已过时——订单截图端点是现成的、按单号取）⇒ `layer.photos` 看签名 URL；空列表提示「这单还没有上传过截图」。两道门：没节点 ⇒ 不画（fail-closed，D-0154/E-0098）；镜像行没有我方单号 ⇒ 也不画（本系统没有它的截图） |
 * | 8 | `#total` 的**总金额**恒 `0` | 分页载荷 `{items,total,page,page_size}` 没有 `TotalRecFee` 汇总（`core/envelope.py:17-24`）→ 订单数用真实 `total`，金额不编 |
 * | 9 | 分页用 layui 表格自带分页 | 对标也是 `page:true, limit:20, limits:[10,20]`；它的 `laypage.render` 是悬空引用（HTML 无该元素） |
 * | 10 | `price_min` / `price_max` **页面不发** | 对标页**没有价格输入控件**（实测 filters 0 个价格、HTML grep `price` 0 命中）→ 自创控件会违反「视觉值从对标取」 |
 * | 11 | 两个隐藏的 xm-select（`#gameServer` / `#dlProject`）不灌数据 | 对标把「区/平台」与「服」拆两级，我们只有一级区服；两者在证据件里都是 `layui-hide` |
 * | 12 | 抢单成功用前端文案「抢单成功」 | 对标弹 `data.msg`（`insiderecorderlist.js:770`），而我们后端成功时 `msg` 是**空串**（`core/envelope.py:8` 默认 `msg=""`）→ 弹一个空 alert 比一句中文更差（读码） |
 * | 17 | 订单号列 `title`（`escAttr`）与「代练/陪玩项目」单元格（`escHtml`）都对自由文本做 HTML 转义 | **单元格**：对标那列**没有 `templet`**，layui 默认渲染**会**走 `util.escape`（读码，vendored `public/layui/layui.js` 偏移 ~203495；**有** `templet` 的列才原样使用 templet 输出）⇒ 对标是**转义过的**；我们因为要用 templet 映射 `project_text` 才自己转义——**忠实复刻，不是偏离**。**`title` 属性**：对标把 `res.project` 裸拼进属性（`insiderecorderlist.js:565` 一带）确实未转义，我们的 `escAttr` 是刻意的非复刻（客户可写入的自由文本） |
 * | 20 | 列表取数走 layui 的 `url`，不直接调用 `api/hall.ts` 的 `fetchHall` | 与 `order-orderlist` 同形（它也把列表交给 layui 的 `url`；`fetchOrders` 同样是「定义+单测」的出口） |
 * | 21 | 「接手价」列的 `title` 去掉对标混进来的「操作」 | 对标拼成 `你接手此单的金额：<价>**操作**元`（`insiderecorderlist.js:631`：`recFeeTit + recFee + i18nDataList.actioncol + dataTotal5`，而 `actioncol` =「操作」）→ 对标的 i18n 取值缺陷，按「对标缺陷不复刻」去掉 |
 * | 22 | 两列「接手此单」的**文案不同形** | **class 同形**；文案：对标 `actioncol` 用 `i18nDataList.actioncolText`（`insiderecorderlist.js:554`），`actioncol2` 用 `i18nDataList.recThisOrder`（`:662`），而 `recThisOrder` 在字典里**重复声明**（`:279` 短标签、`:296` 长确认句，**后者生效**）⇒ 对标第 15 列会在 75px 格里渲染整句长文案。i18n 字典缺陷，按「对标缺陷不复刻」两列都用短标签 |
 *
 * ## ⛔ `toolbar: '#toolbarDemo'` 是**对标自己的悬空引用**——照抄即可
 *
 * 对标引了 `#toolbarDemo`，但它自己 HTML 里**没有**这个模板 ⇒ 对标渲染结果的工具栏也是 0 颗按钮。
 * **本页照抄 `toolbar: '#toolbarDemo'`**（两侧同为「悬空」）——补一个模板反而会多出按钮。
 * 页内那 2 颗按钮（`刷新列表`、`了解如何开通…`）是**手写在 `.layui-inline` 外层的裸 `<a>`**，
 * 不在 `.layui-btn-container` 里，故 `parity` 的工具栏按钮数两侧都是 0；它们随证据件原样渲染。
 *
 * ## 分页：**开启**（逐字照对标）
 *
 * `page: true` + `limit: 20` + `limits: [10, 20]`（注意不是 layui 默认的 `[10,20,30,…]`）。
 * ⚠️ 渲染上看不到分页（对标 0 行 ⇒ layui 不渲染 `.layui-laypage`）——**不能**用渲染结果判 page。
 *
 * ## ⛔ 1 个 i18n key 在对标字典里**不存在**：`i18nDataList.actionorderHoursDescStrcol`
 *
 * 对标那列的 `title` 取了一个字典里没有的 key（实测 `undefined`）→ 对标渲染出的表头也是空的。
 * 本页留 `title: ''`，**不编中文**。⚠️ 不要用 `orderHoursDescStr` 去「修」它——那是另一个 key，
 * 会凭空多出表头文案。
 *
 * ## 3 个 `layui-hide` 筛选项（`gameServer`/`dlProject`/`bussType`）
 *
 * 它们的 `.layui-inline` 带 `layui-hide` → 整块 `display:none`，`parity` 的可见性过滤后两侧都看不见。
 * 但**标记必须原样保留**（对标点「刷新」后会 `form.render()` 再显示；删掉就是另一套 DOM）。
 *
 * ## 不复刻的对标缺陷
 *
 *  - 对标的 `nresize: false` 拼写错（只有 `unresize` 生效）—— 不抄。
 *  - 列里 `sort:false`/`hide` 没写等默认值不写；`unresize:false` 是默认值也不写。
 *
 * ## PII / 凭据（红线）
 *
 *  - `#total` **可见文本里 2 处租户名** → 换成「—」（可见槽约定）。
 *  - `#company` 租户名 → 值置空。
 *  - `#serverTime` 服务端渲染的 epoch 毫秒 → 值置空（运行期值，留着既是死数据，
 *    又会命中「13 位连续数字」的自查正则）。
 *  - `<input name="__RequestVerificationToken">`（在本页 `<form>` 之外）是**真实 CSRF token** →
 *    值置空。**这是凭据，绝不入库**。
 *  其余（静态游戏枚举 13 个 `<option>`、`#tableCount`/`#currentPage` 的 0）原样保留。
 */
import { fetchGames, fetchServers, type ConfigRow, type ServerRow } from '@/api/config'
import {
  claimOrderWithMessage,
  claimPeerMirror,
  PEER_CLAIM_ACCEPTED_TEXT,
  PEER_CLAIM_TAKEN_CODE,
  toHallParams,
  type HallListItem,
  type HallSearchParams,
} from '@/api/hall'
import { BizError } from '@/api/errors'
import { claimUpstreamMirrorWithMessage } from '@/api/jidanpuUpstream'
import { fetchMe, type User } from '@/api/auth'
import { fetchScreenshots, type ScreenshotItem } from '@/api/screenshots'
import { showBackendSuccessMessage } from '@/config/backendMessage'
import { orderStatusLabel } from '@/config/orderStatus'
import {
  bootstrapIsland,
  initIslandElements,
  islandMsg,
  type IslandContext,
  type IslandPage,
} from '@/layui/island'
import { canNode } from '@/layui-island/perms'
import { escapeHtml } from '@/layui-island/escape'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'
import { initFormSelects } from '@/layui/vendor'
import type { ArtifactCol, ArtifactTable } from '@/layui/artifactPage'

/** 证据件（页内 `<style>` + body 标记，零转义）。 */
const ARTIFACT_URL = 'replica/order-insiderecorderlist.html'
/** 对标服务端渲染的 `<title>`。 */
const TITLE = '内部接单大厅'
/** 表格实例 id（= 证据件里 `<table id="tableList">` 的 id，也是 layui 的实例键）。 */
export const HALL_TABLE_ID = 'tableList'
/** 大厅读端点（`business/urls.py` 显式注册，**不是** `orders/hall/`）。
 *
 * ⚠️ **必须带 app base**：兄弟页 `order-orderlist.ts` 的表格 url 也是
 * 从 `import.meta.env.BASE_URL` 拼的；写死根路径在 `BASE_URL='/'` 时看不出问题，但产物挂到子路径
 * （`TENANT_WEB_BASE`，本项目明确要求的「中性产物」）后就会请求错路径。`BASE_URL='/'` 时结果仍是
 * 逐字 `/api/business/hall/`，测试断言不变。
 */
export const HALL_URL = `${(import.meta.env.BASE_URL || '/').replace(/\/$/, '')}/api/business/hall/`
/** 抢单确认语：逐字取对标 i18n `js.recThisOrder`（zh-CN）。 */
export const CLAIM_CONFIRM_TEXT =
  '确定接下此单吗？【提醒】本系统不属于交易平台，不提供担保，订单结算需要自行和本商户联系'

/** 抢单按钮的 class 原文（照对标：**class 同形**）。 */
export const CLAIM_BUTTON_CLASS = 'layui-btn layui-btn-danger layui-btn-xs recThisOrder'
/** 抢单按钮的选择器（⛔ **不能**由 `CLAIM_BUTTON_CLASS` 拼出来：那会造出 `class=".recThisOrder"`，
 * 永远匹配不到——`AGENTS.md` §5-G 陷阱①；两者是**两个**常量）。 */
export const CLAIM_BUTTON_SELECTOR = '.recThisOrder'
/** 抢单按钮文案：两列**文案不同形**（差异 #22）——对标 `actioncol` 用 `actioncolText`（「接手此单」），
 * `actioncol2` 用 `recThisOrder`（字典里被那句长确认语覆盖）⇒ 对标第 15 列渲染整句。
 * 我们两列都用这个短标签（「对标缺陷不复刻」）。 */
export const CLAIM_BUTTON_TEXT = '接手此单'
/**
 * 「接手此单」的**权限节点**：原生 `/claim/` 与镜像 `/remote/…/claim/` **同一个**节点门
 * （后端 `apps/business/api/order_views.py` 的 `ORDER_PERMISSIONS["claim"]` /
 * `["remote_claim"]`，值 `grab_hall.take_order`）。
 *
 * ⚠️ **不是**栏目码 `grab_hall`（那个只管「进不进这一页」）——画了按钮却拿不到这个功能点，
 * 一点就是 HTTP 403，那正是 D-0154 / E-0098 要禁的「draw-then-403」。
 */
export const CLAIM_NODE = 'grab_hall.take_order'
/**
 * 「参考图」的**权限节点**（后端 `screenshot_views.SCREENSHOT_VIEW_NODE`，
 * `GET /api/business/orders/<单号>/screenshots` 的类级门）。
 *
 * ⚠️ **必须和 `CLAIM_NODE` 一样 fail-closed**（`AGENTS.md` §5-D / D-0154 / E-0098）：
 * 这一页的使用者里有「代练员」，而 `order.view_screenshot` 不是 `grab_hall` 栏目带的
 * ——**没这个节点的人，一颗「参考图」都不许画**（⛔ 不是置灰、更不是画了再 403）。
 * 兄弟页（`order-orderlist` / `order-myorderlist`）的截图按钮都按同一个节点渲染。
 */
export const SCREENSHOT_VIEW_NODE = 'order.view_screenshot'

/* ── ⭐ 上家团团通（`origin="peer"`）接单：成功之后**置灰不删卡**（D-0303）──────
 *
 * 业务口径：「成功『正在受理中』+ 置灰不删卡」。
 *
 * 为什么**不能**像另几路那样 `reload(currentPage)`：这一条是**确认制**——本地预占
 * （`listed → claiming`）+ `hall.claim` 已排队，正式订单还没到（等上家 `order.assign`）。
 * 而大厅读口只投影 `status=listed` 的行 ⇒ 一刷新这张卡就**从列表里消失**，
 * 打手会以为单没了（实际在受理中）。所以：置灰留在原地，等下一次手动/自动刷新再自然消失。
 *
 * 置灰的两种落点（都不新造样式表）：
 *  - 两颗按钮加 layui 现成类 `layui-btn-disabled` + `aria-disabled`（⛔ 不新造 CSS）；
 *  - 这一行打上 `data-peer-claimed="1"`（点击期据它再挡一次），并把 `<tr>` 调暗。
 */
export const PEER_CLAIMED_ROW_ATTR = 'data-peer-claimed'
/** 置灰用的 layui 现成类名（证据件里有这把按钮皮肤的同一个类）。 */
const PEER_DISABLED_BUTTON_CLASS = 'layui-btn-disabled'

/* ── 「自动刷新频率」（spec §3.3） ─────────────────────────────────────────────
 *
 * ⚠️ **对标这一页没有自动刷新**（对标源码零 `setInterval`，只有手动的 `.refresh`）
 * ⇒ 这是**新增功能、不是复刻**。因此：
 *  - 视觉值只能取**页内已有的那一份**（`.layui-btn .layui-btn-xs .layui-btn-normal`，与证据件里
 *    那颗「了解如何开通…」同形），⛔ 不新造样式表、不发明新类名；
 *  - 按钮**运行期注入**在证据件那颗 `.refresh` 之后，⛔ 证据件
 *    `public/replica/order-insiderecorderlist.html` 一个字节都不动
 *    （先例：`D-0161` 那颗「同步订单状态」也是运行期注入；证据件是对标渲染结果的留档，改它等于伪造留档）。
 */

/** 「自动刷新频率」按钮的类名（⛔ 不含 `.`；与证据件里那颗「了解如何开通…」同形）。
 *
 * ⚠️ 类名与选择器分成**两个**常量（`AGENTS.md` §5-G 陷阱① / E-0061）：由类名拼选择器会造出
 * `class=".layui-btn"` 那种永远匹配不到的东西。本按钮的查询出口用 **id**，不用类名。
 */
export const AUTO_REFRESH_BUTTON_CLASS = 'layui-btn layui-btn-xs layui-btn-normal'
/** 「自动刷新频率」按钮的 id（证据件里**没有**它 —— 实测 `grep` 零命中，不会撞）。 */
export const AUTO_REFRESH_BUTTON_ID = 'autoRefreshBtn'
/** 查询出口（与上面那个**类名**常量分开，见它的注释）。 */
export const AUTO_REFRESH_BUTTON_SELECTOR = `#${AUTO_REFRESH_BUTTON_ID}`
/** 按钮注入的锚点：证据件里那颗手动「刷新列表」。 */
export const REFRESH_ANCHOR_SELECTOR = '.refresh'
/** 按钮文案前缀（后缀是档位，见 `autoRefreshLabel`）。 */
export const AUTO_REFRESH_LABEL_PREFIX = '自动刷新频率：'

/** 设置面板的标题（`layer.open({title})`）。 */
export const AUTO_REFRESH_PANEL_TITLE = '自动刷新频率'
/** 设置面板的宽度（`layer.open({area})` 的第一个值）。 */
export const AUTO_REFRESH_PANEL_AREA_WIDTH = '320px'
/** 设置面板里那组单选的 `lay-filter`（`form.on('radio(autoRefreshMs)')` 用它认领事件）。 */
export const AUTO_REFRESH_RADIO_FILTER = 'autoRefreshMs'
/** 设置面板容器的 id（`layer.open({type:1, content})` 装的就是它）。 */
export const AUTO_REFRESH_PANEL_ID = 'autoRefreshPanel'

/** 「关闭」那一档的值（`0` = 关闭；`AUTO_REFRESH_STEPS_MS` 里的值都是**启用**的档位）。 */
export const AUTO_REFRESH_OFF = 0

/**
 * 在途互斥的**活性兜底**（毫秒，spec §3.3 第 5 条）：`inFlight` 为真、且距那一发发出已超过这个
 * 时长 ⇒ 视为那一发**已经死了**（`done` 永远不会回来），允许下一发。
 *
 * ⚠️ 它不是「超时重试」的优化项，而是**失败形态的兜底**：没有它时，一次被掐掉的请求会让自动刷新
 * **永久静默**——用户看到的仍是「自动刷新频率：1秒」，列表却再也不动了。
 */
export const AUTO_REFRESH_ALIVE_MS = 30000

/**
 * 允许的自动刷新档位（毫秒）。业务方裁定：**要有 1 秒 / 2 秒的快档**
 * ⇒ ⛔ 不设「最低 5 秒」那种人为下限（spec §1 的 R2 登记了它的真实代价）。
 */
export const AUTO_REFRESH_STEPS_MS = [1000, 2000, 5000, 10000, 30000, 60000] as const

/**
 * 缓存里那个值能算哪一档（**fail-safe**）：不在允许集合里（`500` / `'abc'` / 负数 / 小数 /
 * `null`）⇒ 一律返回 `AUTO_REFRESH_OFF`，⛔ 不抛、⛔ **不截断成邻近档**（把 `500` 当 `1000`
 * 就是替用户改了设置，那比「当关闭」更糟：用户看到的是「我明明设过」）。
 *
 * ⚠️ 判据是「**转成数字后与某个档位严格相等**」，所以：
 *  - `'1000'`（`localStorage` 里恒是字符串）**算** 1000 —— 这是唯一的正常读入形状；
 *  - `'1000.0'` / `' 1000'` 这种带尾巴的一律**不算**（`Number` 化的宽松口径只服务读入，
 *    不服务「用户手改 localStorage」的容错 —— 那种情况按关闭处置更安全）。
 */
export function normalizeAutoRefreshMs(raw: unknown): number {
  if (typeof raw !== 'number' && typeof raw !== 'string') return AUTO_REFRESH_OFF
  // ⚠️ `Number('')` 是 0、`Number(null)` 是 0 ⇒ 先按「空」挡掉（E-0021 同族：0 是有意义的边界值，
  //    不能与「空」混在一句真值判据里，但这里的 0 恰好是「关闭」，两者结果一致）。
  if (typeof raw === 'string' && raw.trim() === '') return AUTO_REFRESH_OFF
  const n = Number(raw)
  if (!Number.isFinite(n)) return AUTO_REFRESH_OFF
  return (AUTO_REFRESH_STEPS_MS as readonly number[]).includes(n) ? n : AUTO_REFRESH_OFF
}

/** 按钮文案：`自动刷新频率：关闭` / `自动刷新频率：10秒`（spec §3.3 第 8 条，始终反映当前状态）。 */
export function autoRefreshLabel(ms: number): string {
  const step = normalizeAutoRefreshMs(ms)
  return step === AUTO_REFRESH_OFF
    ? `${AUTO_REFRESH_LABEL_PREFIX}关闭`
    : `${AUTO_REFRESH_LABEL_PREFIX}${step / 1000}秒`
}

/** 按钮标记（**唯一出处**：注入与用例都读它，⛔ 不要在两处各拼一份）。 */
export const AUTO_REFRESH_BUTTON_HTML =
  `<a class="${AUTO_REFRESH_BUTTON_CLASS}" id="${AUTO_REFRESH_BUTTON_ID}">` +
  `${autoRefreshLabel(AUTO_REFRESH_OFF)}</a>`

/**
 * `localStorage` 的键。⚠️ 读 / 写都 try/catch：隐私模式 / 存储被禁用时 `localStorage` 的
 * **取属性本身**就会抛（`SecurityError`）⇒ 失败当「关闭」，⛔ 不让一个记不住的偏好把整页打白。
 */
export const AUTO_REFRESH_STORAGE_KEY = 'tt.hall.autoRefreshMs'

/** 安全读 `localStorage`（抛异常时当「没存过」⇒ 调用方按关闭处置）。 */
function readStoredAutoRefreshMs(): number {
  try {
    return normalizeAutoRefreshMs(window.localStorage.getItem(AUTO_REFRESH_STORAGE_KEY))
  } catch {
    return AUTO_REFRESH_OFF
  }
}

/** 安全写 `localStorage`（抛异常时静默放弃 —— 记不住偏好远好过打断用户操作）。 */
function writeStoredAutoRefreshMs(ms: number): void {
  try {
    window.localStorage.setItem(AUTO_REFRESH_STORAGE_KEY, String(ms))
  } catch {
    /* 隐私模式 / 配额满：只影响「下次还记得」，不影响本次设置 */
  }
}

/** 行的**业务标识**：原生 = 我方单号；镜像 = 远端单号（镜像行没有 `order_no`）。 */
const rowKey = (d: HallListItem): string =>
  d.origin === 'native' ? d.order_no : d.remote_order_id

/**
 * 镜像行的主键是否是**能用的正整数**（fail-closed 的那条判据）。
 *
 * ⚠️ 判据写成「是不是正整数」而不是真值（E-0021 同族）：`null` / `undefined` / `0` / `NaN` /
 * 负数字符串都不是合法主键，⛔ 不许拿它们拼 URL 去发一条注定打不中的请求。
 * ⚠️ 对 `origin === 'upstream'`（鸡蛋铺镜像行）与 ⭐ `origin === 'peer'`（团团通镜像行）
 * 都有意义：两条镜像线各用**自己那张表**的主键，另几路本来就是 `null`。
 */
export function isClaimableMirrorPk(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value > 0
}

/**
 * 抢单按钮 HTML 的**唯一出处**（class / 文案取对标，见上面的常量；**加**两个 `data-*` 用来区分来源）。
 *
 * ⚠️ 类名与选择器分成两个常量（`AGENTS.md` §5-G 的陷阱①）：`CLAIM_BUTTON_CLASS` 是**类名**、
 * 查询用的是 `CLAIM_BUTTON_SELECTOR`，⛔ 不要把它拼成 `class=".recThisOrder"`。
 *
 * ⚠️ `data-id` 的取值**按来源分两档**：两条镜像线（`upstream` / ⭐ `peer`）取 **`mirror_pk`**
 * （端点按本表主键取行）；另几路取各自的行键（我方单号 / 远端单号）。
 */
const claimButton = (d: HallListItem): string => {
  const origin = d.origin
  const source = d.origin === 'native' ? '' : d.source_system
  const byMirrorPk = origin === 'upstream' || origin === 'peer'
  const id = byMirrorPk ? String(d.mirror_pk) : rowKey(d)
  return (
    `<a class="${CLAIM_BUTTON_CLASS}" data-id="${escAttr(id)}" ` +
    `data-origin="${escAttr(origin)}" data-source="${escAttr(source)}">${CLAIM_BUTTON_TEXT}</a>`
  )
}

/** 对标的 `page.limits`（只有两项）。 */
export const PAGE_LIMITS = [10, 20]
/** 对标的 `page.limit`。 */
export const PAGE_LIMIT = 20

/** 表头：逐条照抄对标（15 列，无勾选列）。 */
export const COLS: ArtifactCol[] = [
  // 行内「接手此单」按钮（函数 templet，空表下不渲染）。
  { field: 'actioncol', title: '操作', width: 75 },
  { field: 'orderIdView', title: '订单号', width: 170 },
  { field: 'levelMark', title: '类型', align: 'center', width: 55 },
  { field: 'roleName', title: '角色名', width: 150 },
  // 对标的 `nresize:false`（拼写错）不抄。
  { field: 'gameGroup', title: '阵营/平台', align: 'center', width: 70 },
  // 对标 `minWidth:290`（自动列）。
  { field: 'project', title: '代练/陪玩项目', minWidth: 290 },
  // 对标 `minWidth:180`（自动列）。
  { field: 'gameInfo', title: '角色信息', minWidth: 180 },
  { field: 'orderSta', title: '订单状态', align: 'center', width: 60 },
  // 对标这一列的 `title` 取了字典里不存在的 key → 表头也是空的，留空不编中文（见文件头注释）。
  { field: 'orderHoursDescStr', title: '', width: 70, align: 'center' },
  { field: 'recFee', title: '接手价', align: 'center', width: 70, sort: true },
  { field: 'recOrderInsureAmount', title: '保证金要求', align: 'center', width: 90 },
  { field: 'img', title: '参考图', width: 70, align: 'center' },
  { field: 'orderRemark', title: '备注要求', width: 120, align: 'left' },
  { field: 'recOrderTime', title: '发布时间', width: 90, sort: true },
  // 行内「确定接下此单吗？」按钮（函数 templet，空表下不渲染）。
  { field: 'actioncol2', title: '操作', width: 75 },
]

/** 表的声明（`elem` / `toolbar` / `page` / `defaultToolbar` 与对标同值）。 */
export const TABLE: ArtifactTable = {
  elem: '#tableList',
  // 对标的悬空引用：照抄（对标 HTML 里没有这个模板，见文件头注释）。
  toolbar: '#toolbarDemo',
  // 对标把 `defaultToolbar` 写了两次、后写的 `['filter']` 生效（右上角 1 个「筛选列」图标）。
  defaultToolbar: ['filter'],
  // 逐字照对标。
  page: { limit: PAGE_LIMIT, limits: PAGE_LIMITS },
  cols: COLS,
}

/* ── 取值纪律：下列文案/色值**逐字**取对标 i18n ── */
const BADGE_URGENT =
  '<span class="lableb" style="background-color:#FFBBFF" title="加急订单，优先安排">急</span>'
const ORDER_DETAILS_TIT = '，只有接下此单才能看详情'
const STA_TITLE = '等待接单'
/** 大厅的订单状态徽标**恒绿**（对标这一列没有 switch，`colorStr` 就是一个常量）。 */
const STA_COLOR = '#219227;'
const REC_FEE_TIT = '你接手此单的金额：'
const REC_ORDER_TIME_TIT = '发布时间'
const IMG_TEXT = '参考图'
const IMG_TIT = '查看参考图'
/** 空列表的提示（人话点名没图，而不是坏了；逐字钉在测试里）。 */
export const NO_SCREENSHOTS_TEXT = '这单还没有上传过截图'
/** 「参考图」按钮的类名（复刻对标的 `mangeImg`；⛔ 不含 `.`）。 */
export const SHOT_BUTTON_CLASS = 'mangeImg'
/** 点击委托用的选择器——与上面的**类名**分成两个常量（`AGENTS.md` §5-G 陷阱① / E-0061：
 * 由类名拼选择器会造出 `class=".mangeImg"` 那种永远匹配不到的东西）。 */
export const SHOT_BUTTON_SELECTOR = '.mangeImg'

function inputValue(root: HTMLElement, name: string): string {
  return root.querySelector<HTMLInputElement>(`[name="${name}"]`)?.value ?? ''
}

function selectValue(root: HTMLElement, id: string): string {
  return root.querySelector<HTMLSelectElement>(`#${id}`)?.value ?? ''
}

/** 数字型下拉的空值统一归成 `null`（与 `order-addorder.ts` 的 `toId` 同口径）。 */
function toId(raw: string | number | null | undefined): number | null {
  if (raw === '' || raw === null || raw === undefined) return null
  const n = Number(raw)
  return Number.isFinite(n) ? n : null
}

/** 属性值转义：对标把 `res.project` 裸拼进 `title`（确实未转义），我们不复刻（差异表 #17 的 `title` 半条）。 */
function escAttr(value: string): string {
  return value.replace(/&/g, '&amp;').replace(/"/g, '&quot;')
}

/** 单元格文本转义：对标 `project` 列**没有 `templet`**，layui 默认渲染**会** `util.escape`
 * ⇒ 对标那一列是**转义过的**；我们用 templet 映射 `project_text`（layui 对有 templet 的列原样输出），
 * 所以必须自己转义——**忠实复刻**（差异表 #17）。 */
function escHtml(value: string): string {
  return value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

/** `2026-09-20T21:32:15+08:00` → `2026-09-20 21:32`（与 `order-orderlist` 的 `createRow` 同口径）。 */
function fmtTime(iso: string): string {
  return iso.replace('T', ' ').slice(0, 16)
}

/**
 * 一张截图在 `layer.photos` 里的形状（`{alt, src}`；`src` = 后端签发的短时效读链接）。
 *
 * 说明文字取 `original_name`（原文件名），有 `remark`（上传备注）时括注在后面。
 *
 * ⚠️ **两个字段都过 `escapeHtml`**：vendored layui 把它们**字符串拼进 HTML 属性**；
 * `original_name` / `remark` 是上传人写的自由文本 ⇒ 不转义就是一条存储型 XSS。
 * 先例与修法同 `emp-settlementemp.ts::qrPhotos`。
 */
function screenshotPhoto(item: ScreenshotItem): { alt: string; src: string } {
  const name = item.original_name.trim()
  const remark = item.remark.trim()
  const alt = remark === '' ? name : name === '' ? remark : `${name}（${remark}）`
  return { alt: escapeHtml(alt), src: escapeHtml(item.url) }
}

/**
 * DOM → 我们的检索参数。
 *
 * 本页**有控件**的后端筛选项是 4 个：`gameName`→`game`、`gameArea`→`server`、
 * `key1`→`keyword`、`project`→`project_text`。`price_min`/`price_max` 后端支持但**对标页没有控件**
 * （差异表 #10），所以这里不出现。
 *
 * `readServer` 是**注入的接缝**：`gameArea` 是对标的 xm-select（隐藏的 `<select id="gameArea">`
 * 只是证据件里的标记，真人点不到它）。本页**不再**把 xm-select 的选中值同步回那个 `<select>`
 * （formSelects v4 的 `on(id, vals, …)` 给的 `vals` 是**改动前**的值——同步会把**上一次**的选择
 * 写进去）。于是判据是：**隐藏 `<select>` 非空时读它**（测试直接赋值 / jsdom 的接缝），**为空时读
 * widget 的当前值**（真人点选取自 widget，`readServer()` 的回落分支）。
 */
export function toHallSearch(
  root: HTMLElement,
  readServer: () => number | null = () => toId(selectValue(root, 'gameArea')),
): HallSearchParams {
  return {
    game: toId(selectValue(root, 'gameName')),
    server: readServer(),
    keyword: inputValue(root, 'key1').trim(),
    project_text: inputValue(root, 'project').trim(),
  }
}

/**
 * 把证据件 `#total` 句子里的**订单数**换成真实值。
 *
 * 总金额保持证据件里的 `0`：我们的分页载荷**没有**对标的 `TotalRecFee` 汇总
 * （分页载荷只有 `items/total/page/page_size`），不编数字（差异表 #8）。
 * 认不出句子时**原样返回**——不抛也不改写（改坏模板应当由测试发现，而不是静默造一句假的）。
 */
export function renderHallTotal(template: string, count: number): string {
  return template.replace(
    /当前共有\d+笔订单等待接单，总金额[^元]*元/,
    `当前共有${count}笔订单等待接单，总金额0元`,
  )
}

/** 一列的 templet 入参是 layui 交回来的当前行。 */
type HallCell = (row: HallListItem) => string

/**
 * 15 列的**真实渲染**声明：骨架（顺序/标题/宽度/对齐/hide/sort）一律来自 `COLS`，
 * 这里只按 `field` 挂数据渲染。`COLS` 是唯一出处，所以声明不会与对标漂移
 * （`__tests__` 逐项对拍）。
 *
 * 载荷里没有的字段（角色名/阵营/类型/时限/保证金/备注）**恒空**，不造数据（差异表 #1–#6）。
 *
 * `canClaim` 是**权限门**（`AGENTS.md` §5-D / D-0154 / E-0098）：**必填**（没有默认值，
 * 漏传是编译错误，不是静默 fail-open）。为假 ⇒ 两颗「接手此单」**一颗都不画**——
 * 镜像单接单端点与原生接单同节点（`CLAIM_NODE`），没节点的人画了就是「点了 403」。
 *
 * `canViewShots` 同理，管的是「参考图」那颗（节点 `SCREENSHOT_VIEW_NODE`）：**必填**、为假 ⇒
 * 整格为空。⚠️ 与它并列的还有一道**数据门**：镜像行没有我方单号（`order_no` 是空串），
 * 本系统不会有它的截图 ⇒ 同样不画（⛔ 不画一颗只会换来「记录不存在」的按钮）。
 */
export function buildHallCols(opts: {
  gameName: (id: number) => string
  serverName: (id: number | null) => string
  canClaim: boolean
  canViewShots: boolean
}): Record<string, unknown>[] {
  /**
   * 游戏名查询的**空值兜底**：原生行的 `game` 类型是 `number`，但**镜像行**的游戏按后端
   * 投影可以是 `NULL`（对不上我方游戏的镜像行如此；今天的 `listed` 状态机不产出这种行，
   * 但那是状态机的事、不是行形状的保证）。⛔ 直接把 `null` 交给字典会渲染出 `[null]`——
   * 契约一旦漂移，打手看到的就是这四个字母。
   */
  const gameLabel = (id: number | null | undefined): string =>
    id === null || id === undefined ? '' : opts.gameName(id)
  const cells: Record<string, HallCell> = {
    orderIdView: (d) => {
      // 「是不是镜像行」= **非原生**（两条镜像线**同形**：都没有我方单号、
      // 区服都是远端文本）。⛔ 别写 `origin === 'remote'`——那会让镜像行渲染空的 `order_no`。
      const isMirror = d.origin !== 'native'
      // 镜像行**没有**我方单号：显示远端展示单号，展示单号缺失时回落到远端单号
      // （⛔ 不回落到空的 `order_no`）——镜像行口径**不动**（D-0301 只换原生行）。
      // ⭐ D-0301：原生行 = `source_order_no || order_no`（来源号非空取它）。
      const no = isMirror
        ? d.remote_order_view || d.remote_order_id
        : d.source_order_no || d.order_no
      const tit = isMirror ? d.remote_order_id : d.project_text
      return (
        `<div class="orderLine" id="order_${escAttr(rowKey(d))}" data-id="${escAttr(rowKey(d))}" ` +
        `data-orderIdView="${escAttr(rowKey(d))}" title="~">` +
        `${!isMirror && d.is_urgent ? BADGE_URGENT : ''}` +
        `<a href="javascript:void(0);" class="orderdetails" data-viewId="${escAttr(rowKey(d))}" ` +
        `data-id="${escAttr(rowKey(d))}" style="color:#0094ff" ` +
        `title="${escAttr(tit)}${ORDER_DETAILS_TIT}">${escHtml(no)}</a></div>`
      )
    },
    project: (d) => escHtml(d.project_text),
    // 镜像行的区服是**远端文本**（`server` 是 `null`，不映射我方 FK）⇒ 按 `origin` 分支，
    // ⛔ 不要拿 `serverName(null)` 去查我方字典（那会渲染出裸主键 / 空）。
    // ⚠️ 「非原生」= 镜像（两条镜像线**同一支**）：`remote_game_area` / `remote_game_server`
    //    都是后端投影的文本，写法逐字相同。
    gameInfo: (d) =>
      d.origin !== 'native'
        ? `<span title="">[${gameLabel(d.game)}]${escHtml(d.remote_game_area)}${escHtml(d.remote_game_server)}</span>`
        : `<span title="">[${gameLabel(d.game)}]${opts.serverName(d.server)}</span>`,
    orderSta: (d) =>
      `<a href="javascript:void(0)" class="lableb setdlsta" data-id="${escAttr(rowKey(d))}" ` +
      `title="${STA_TITLE}" style="background-color:${STA_COLOR}">${orderStatusLabel(d.status)}</a>`,
    recFee: (d) =>
      `<a href="javascript:void(0)" style="color:#ff6a00;font-weight:600" ` +
      `title="${REC_FEE_TIT}${d.hall_price ?? ''}元">${d.hall_price ?? ''}</a>`,
    recOrderTime: (d) => `<span title="${REC_ORDER_TIME_TIT}">${fmtTime(d.created_at)}</span>`,
    // 「参考图」（差异表 #7）：**两道门**——
    //  ① 权限门：没 `order.view_screenshot` ⇒ 整格为空（fail-closed，`AGENTS.md` §5-D）；
    //  ② 数据门：镜像行没有我方单号（`order_no` 是空串）⇒ 本系统没有它的截图，也不画。
    // 原生行的 `data-id` = 我方单号（= `rowKey`），点击期用它打截图端点。
    img: (d) =>
      !opts.canViewShots || d.origin !== 'native'
        ? ''
        : `<button class="layui-btn layui-btn-xs layui-btn-normal ${SHOT_BUTTON_CLASS}" style="margin-right: 2px" ` +
          `data-id="${escAttr(rowKey(d))}" data-origin="native" title="${IMG_TIT}">${IMG_TEXT}</button>`,
    // 两颗「接手此单」：class 与 data-id 同形，**文案不同形**（差异 #22；见 COLS 的
    // actioncol / actioncol2 注释）。**两道门在这里**：
    //  ① 权限门：没 `grab_hall.take_order` 节点 ⇒ 返回空串（不画，不是置灰）；
    //  ② **数据门（fail-closed）**：两条镜像线（`upstream` / `peer`）拿不到正整数
    //     `mirror_pk` ⇒ 也**不画**（⛔ 不拿 `null` / 上家单号去拼 URL：那条请求注定打不中那一行）。
    actioncol: (d) => (opts.canClaim && claimTargetOf(d) !== null ? claimButton(d) : ''),
    actioncol2: (d) => (opts.canClaim && claimTargetOf(d) !== null ? claimButton(d) : ''),
  }
  return COLS.map((c) => ({ ...c, templet: cells[c.field ?? ''] ?? (() => '') }))
}

/**
 * 这一行**能不能接、接的时候 `data-id` 是什么**（渲染期与点击期**共用同一处判据**）。
 *
 * - 原生行 ⇒ 我方单号（非空串）；
 * - ⛔ **`remote`（旧路）⇒ `null`**：后端不再产出 `origin="remote"` 行，
 *   接单端点与 API 出口已拆除 ⇒ **连按钮都不画**（fail-closed，⛔ 不许落进原生端点）；
 * - **镜像行（`upstream`）⇒ `mirror_pk` 的十进制串**；不是正整数 ⇒ `null`；
 * - ⭐ **上家团团通行（`peer`）⇒ 也是 `mirror_pk` 的十进制串**（它自己那张表的主键，
 *   接单端点 `POST /api/business/hall/peer/<mirror_pk>/claim/` 的路径参数就是它）；
 *   不是正整数 ⇒ `null`（**fail-closed**：渲染期不画按钮，点击期也不会发请求）。
 *   ⚠️ 必须与 `upstream` 走**同一处正整数判据**、但**各自**由 `data-origin` 分辨端点——
 *   两张镜像表的主键空间互不相干（`peer` 的 7 号与 `upstream` 的 7 号是两行不同的卡）。
 *
 * ⚠️ 抽成纯函数是**承重**的：渲染期与点击期各写一份判据必然漂移，而漂移的表现是
 * 「画了按钮、点了静默没反应」或「按钮在、请求打到 `/peer/null/claim/`」。
 */
export function claimTargetOf(d: HallListItem): string | null {
  if (d.origin === 'upstream' || d.origin === 'peer') {
    return isClaimableMirrorPk(d.mirror_pk) ? String(d.mirror_pk) : null
  }
  // ⚠️ `remote` 已拆除：不画按钮、点击期也不会发请求（判据只此一处，渲染 / 点击共用）。
  if (d.origin === 'remote') return null
  const key = rowKey(d)
  return key === '' ? null : key
}

export async function mountHallPage(root: HTMLElement, _ctx: IslandContext): Promise<() => void> {
  const layui: LayuiNamespace = await bootstrapIsland(['form', 'table', 'layer'])
  const form = layui.form as LayuiModule
  const table = layui.table as LayuiModule
  const layer = layui.layer as LayuiModule

  document.title = TITLE

  const msg = (text: string): void => islandMsg(layer, text)
  const errText = (e: unknown): string => (e instanceof Error ? e.message : '操作失败')
  /** 抢单成功提示出口（后端 `msg` 优先，「未能撤回」用 `layer.alert`）。 */
  const backendMessageActions = {
    toast: (text: string, durationMs?: number): void => {
      ;(layer.msg as (t: string, o?: unknown) => void)(text, { time: durationMs ?? 1500 })
    },
    alert: (text: string): void => {
      ;(layer.alert as (t: string, o?: unknown) => void)(text, {
        title: '注意：代练通订单未能撤回',
        icon: 0,
        shadeClose: false,
      })
    },
  }

  // 字典先取（失败了照常渲染：下拉为空、区服显示主键）——与 `order-orderlist` 同一处置。
  let games: ConfigRow[] = []
  let servers: ServerRow[] = []
  try {
    games = await fetchGames()
  } catch (e) {
    msg(errText(e))
  }
  // 权限门（`AGENTS.md` §5-D / D-0154 / E-0098）：岛页跑在 iframe 里、**不引 Pinia**，
  // 所以按 `index-console.ts::visibleShortcuts` 的先例自己取一次 `/api/auth/me`，再用
  // `canNode` 判 `CLAIM_NODE`（与壳层 `stores/session.canNode` 同口径的纯函数）。
  // ⚠️ **失败一律 fail-closed**（`?? {}` ⇒ `canNode` 为假 ⇒ 不画按钮）：宁可少画一颗按钮，
  //    也不许「画了再 403」。取不到就只影响这一颗按钮，页面其余部分照常渲染（与 `fetchGames`
  //    同一处置：不弹错、不阻塞——否则一个 /me 抖动会把整页打白）。
  const me: User | null = await fetchMe().catch((): User | null => null)
  const canClaim = canNode(me ?? {}, CLAIM_NODE)
  // 「参考图」的权限门（`SCREENSHOT_VIEW_NODE`）与接单门**各自独立**：一个持
  // `grab_hall.take_order` 却没 `order.view_screenshot` 的人，能接单但看不了图
  // （存量租户可能没有这个节点）。
  const canViewShots = canNode(me ?? {}, SCREENSHOT_VIEW_NODE)
  const gameNames = new Map(games.map((g) => [g.id, g.name] as const))
  const serverNames = new Map(servers.map((s) => [s.id, s.name] as const))

  const res = await fetch(`${import.meta.env.BASE_URL}${ARTIFACT_URL}`)
  if (!res.ok) throw new Error(`证据件取不到（HTTP ${res.status}）：${ARTIFACT_URL}`)
  root.innerHTML = await res.text()
  initIslandElements(layui)

  /* ── 筛选栏：游戏下拉灌真数据（对标是服务端内联 `<option>`，我们只能运行期灌） ── */
  const gameSelect = root.querySelector<HTMLSelectElement>('select#gameName')
  if (gameSelect) {
    // ⚠️ 游戏名来自 API（租户自建字典）⇒ 进 `innerHTML` 前过 `escapeHtml`。
    gameSelect.innerHTML = games
      .map((g) => `<option value="${g.id}">${escapeHtml(g.name)}</option>`)
      .join('')
    gameSelect.selectedIndex = games.length > 0 ? 0 : -1
  }
  ;(form.render as (t?: string) => void)('select')

  /* ── 区服（`gameArea` 是 formSelects 的单选）：按当前游戏灌选项 ── */
  // ⚠️ **不登记 `on` 回调**：formSelects v4 的 `on(id, vals, …)` 里 `vals` 是
  //    **改动前**的选中值，用它同步回隐藏 `<select>` 会把**上一次**的选择
  //    写进去 ⇒ `readServer()` 读到过期的非空值、静默按错误的区服筛。`setServerOptions()` 每次换游戏
  //    都把隐藏 `<select>` 重置成空占位，故这里没有任何需要同步的状态。
  type FormSelectsLike = {
    data?: (id: string, type: string, opts: unknown) => void
    value?: (id: string, type?: string) => unknown
  }
  const formSelects = (layui as unknown as { formSelects?: FormSelectsLike }).formSelects

  function setServerOptions(list: ServerRow[]): void {
    const select = root.querySelector<HTMLSelectElement>('select#gameArea')
    if (select) {
      // ⚠️ **保留证据件里那个空占位并「不预选」任何区服**：
      //    给 `innerHTML` 赋值时浏览器会自动选中第 0 项，于是出现「界面折叠态显示未选、请求却按
      //    第一个区服筛」——同一个事实两套判据（E-0021 同型），而「刷新 / 搜索 / 回车」三条路径
      //    都会因此把大厅**静默收窄到一个区服**。证据件里 `#gameArea` 的第一项本来就是
      //    `<option value="">请选择或输入</option>`。
      //    对标也是「有选项、但未选中」（只灌数据，`xm-select` 的折叠态仍显示「请选择」）。
      //    ⚠️ 区服名同样来自 API（租户自建字典）⇒ 与游戏下拉同一条纪律，过 `escapeHtml`。
      select.innerHTML =
        '<option value="">请选择或输入</option>' +
        list.map((s) => `<option value="${s.id}">${escapeHtml(s.name)}</option>`).join('')
      select.selectedIndex = 0
    }
    formSelects?.data?.('gameArea', 'local', {
      arr: list.map((s) => ({ name: s.name, value: s.id })),
    })
  }

  /** 区服的读取出口：隐藏 `<select>` 非空时用它的值（测试赋值 / 注入的接缝），
   * 为空时用 widget 的当前值——**widget 才是真人点选的来源**（见 `toHallSearch` 的文件头注释）。 */
  function readServer(): number | null {
    const fromSelect = toId(selectValue(root, 'gameArea'))
    if (fromSelect !== null) return fromSelect
    const raw = formSelects?.value?.('gameArea')
    const first = Array.isArray(raw) ? (raw[0] as unknown) : raw
    const value =
      typeof first === 'object' && first !== null
        ? (first as { value?: unknown }).value
        : (first as unknown)
    return toId(value === null || value === undefined ? '' : String(value))
  }

  async function onGameChange(): Promise<void> {
    const gameId = toId(selectValue(root, 'gameName'))
    if (gameId === null) return
    try {
      servers = await fetchServers(gameId)
    } catch (e) {
      msg(errText(e))
      return
    }
    for (const s of servers) serverNames.set(s.id, s.name)
    setServerOptions(servers)
  }

  // ⚠️ 顺序与 `order-addorder.ts` 一致：**先 `initFormSelects`（渲染出 xm-select 壳），
  //    再灌数据**——反过来时 `formSelects.data()` 可能作用在尚未 render 的实例上。
  initFormSelects('select[xm-select]')
  await onGameChange()

  /* ── 取数与刷新 ── */
  // 当前页码：**写入点**是下面 render 配置的 `done(res, curr, count)`（真人翻页时 layui 会把真实
  // `curr` 交回来）；**读取点**是抢单成功后的 `reload(currentPage)`（就地刷新，不跳回第 1 页）。
  let currentPage = 1

  /* ── 自动刷新的状态（spec §3.3；本页此前**零定时器**） ── */
  /** 自动刷新那一发**是否在途**（发出前置真、`onTableDone` 里清）。 */
  let inFlight = false
  /** 那一发的发出时刻（配 30 秒活性兜底用）。 */
  let issuedAt = 0
  /** 当前档位（`0` = 关闭），初值在下面从 `localStorage` 读。 */
  let autoRefreshValue = AUTO_REFRESH_OFF
  /** 定时器句柄；**唯一写入口**是 `restartAutoRefresh()`（`null` = 没在跑）。 */
  let timer: number | null = null

  /**
   * 表格「这一发回来了」的**唯一收口**（spec §3.3 第 5 条）：内容与原先内联在 `table.render` 里的
   * `done` **逐字相同**（更新 `currentPage` + 改写 `#total` + `form.render()`），只是多了一件事——
   * 把自动刷新的**在途标记**清掉。
   *
   * ⚠️ **必须是一个具名函数、两处传同一个引用**（`table.render` 与**自动刷新那一发**的 `reload`）：
   * 各写一份就是漂移源，而漂移的表现是「自动刷新发出去一次之后就再也不发了」——**静默**。
   * ⚠️ `done` **只在自动刷新那一发里传**：给每一次 `reload` 都加这个键，
   * 会让既有 6 处 `toHaveBeenCalledWith('tableList', {where, page})`（`order-insiderecorderlist.spec.ts`
   * :482/499/511/529/557/696）一起变红，而它们要钉的**不是**这件事。
   */
  function onTableDone(
    r: { count?: number } | undefined,
    curr?: number,
    count?: number,
  ): void {
    // 判据是「是不是数字」而不是真值（E-0021）：`curr` 只会是正整数，`typeof` 更直白。
    if (typeof curr === 'number') currentPage = curr
    inFlight = false
    ;(form.render as () => void)()
    const total = root.querySelector<HTMLElement>('#total')
    if (total) {
      // `count`（done 的第 3 参）就是 layui 交回来的 `res[countName]`；保留 `r?.count` 回落，
      // 让「只给响应体」的调用形状也成立。
      total.textContent = renderHallTotal(total.textContent ?? '', count ?? r?.count ?? 0)
    }
  }

  /**
   * `table.reload` 的**唯一出口**。
   *
   * `opts.done` 是**可选**的第三个参数（⛔ 不塞进 `where`/`page`：那会改掉既有 6 处断言的形状）：
   * 只有**自动刷新那一支**传 `{ done: onTableDone }`。
   *
   * ⚠️ **在途互斥的落点**（spec §3.3 第 5 条）：传了 `done` 的那一发才算「自动刷新在途」，
   * 发出前置 `inFlight=true` + `issuedAt=Date.now()`；没有 `done` 的一发（手动刷新 / 搜索 / 抢单后
   * 就地刷新）**不碰这两个变量** —— 它们描述的是**自动刷新那一发**是否在途，不是「表格忙不忙」。
   */
  const reload = (
    page = 1,
    size?: number,
    opts?: { done?: typeof onTableDone },
  ): void => {
    const where = toHallParams(toHallSearch(root, readServer))
    // ⚠️ **没传 `size` 时不要塞 `limit`**：layui 自己那份实例 `options.limit` 由
    //    它的分页条 `jump` 更新（读码 vendored `public/layui/layui.js` 偏移 ~297822：
    //    `jump:function(e,t){t||(a.page=e.curr,i.limit=e.limit,a.pullData(e.curr))}`）——我们塞一个
    //    常量（原来是 `currentSize:20`）会把用户选的「条/页」盖回 20。只有调用方**显式**给 `size`
    //    时才带 `limit`（本页今天没有这样的调用点，这个分支是对标 `reload(…, limit)` 的等价出口）。
    // 在途互斥：**只有带 `done` 的那一发**（= 自动刷新）标记在途，因为只有它会回调 `onTableDone`。
    if (opts?.done) {
      inFlight = true
      issuedAt = Date.now()
    }
    ;(table.reload as (id: string, o: unknown) => void)(HALL_TABLE_ID, {
      where,
      page: size === undefined ? { curr: page } : { curr: page, limit: size },
      ...(opts?.done ? { done: opts.done } : {}),
    })
  }

  ;(table.render as (o: unknown) => void)({
    elem: `#${HALL_TABLE_ID}`,
    id: HALL_TABLE_ID,
    url: HALL_URL,
    method: 'get',
    // ⚠️ **首屏必须带 `game`**：对标首屏的 URL 也带选中的游戏，
    //    而我们的 `#gameName` 挂载时**已选中第一条**。不带 `where` 会把**所有游戏**的待抢单都拉回来，
    //    而 `serverNames` 只装了**当前游戏**的区服 ⇒ 别的游戏的行会把区服渲染成裸主键（`[原神]7`）。
    //    同一事实两套判据（下拉显示某游戏、请求不筛）——正是 `#gameArea` 那条裁决的同型问题。
    where: toHallParams({ game: toId(selectValue(root, 'gameName')) }),
    // 后端分页参数名是 `page` / `page_size`（`EnvelopePagination.page_size_query_param`）。
    request: { pageName: 'page', limitName: 'page_size' },
    parseData: (r: { code: number; msg: string; data?: { items?: HallListItem[]; total?: number } }) => ({
      code: r.code,
      msg: r.msg,
      count: r.data?.total ?? 0,
      data: r.data?.items ?? [],
    }),
    toolbar: TABLE.toolbar,
    defaultToolbar: [...(TABLE.defaultToolbar ?? [])],
    // ⚠️ **必须拷贝 `TABLE.page`**：layui 会往传进去的 `page`
    //    对象上写 `curr`、删 `elem`/`jump` ⇒ 直接传导出的 `TABLE.page`
    //    会**改掉导出常量**，让 `order-replica.spec.ts` 的 `pageOf(INSIDE_TABLE)` 断言在复用同一模块
    //    实例时漂移（今天被 vitest 的文件级隔离掩盖着）。兄弟页传的就是新字面量。
    page: { ...(TABLE.page as { limit: number; limits: number[] }) },
    cols: [
      buildHallCols({
        gameName: (id) => gameNames.get(id) ?? String(id),
        serverName: (id) => (id === null ? '' : (serverNames.get(id) ?? String(id))),
        canClaim,
        canViewShots,
      }),
    ],
    text: { none: '暂无数据' },
    // ⚠️ **页码钩子是真的 `done(res, curr, count, origin)`**：layui 的 bundle 里
    //    `page(<id>)` 事件**不存在**，它调的是 `done` ⇒ 页码必须从 `done` 的第 2 参取。
    //    登记幻影 `table.on('page(...)')` 的后果：真人翻页后 `currentPage` 恒 1
    //    （抢单刷新回第 1 页）、页大小恒 20。
    // ⚠️ 内容抽成了具名函数 `onTableDone`（spec §3.3 第 5 条）：**同一份**要传给自动刷新那一发
    //    的 `reload`（vendored layui 的 `reload(options)` 会把传进去的 `done` 合并进实例配置、
    //    渲染时真的会回调）。各写一份 = 漂移源。
    done: onTableDone,
  })

  // 搜索按钮（对标 `form.on('submit(seachBtn)')`）。
  ;(form.on as (e: string, cb: () => void) => void)('submit(seachBtn)', () => {
    reload(1)
  })
  // 切游戏 → 重建区服 → 重载（对标 `form.on('select(selectGame)')`）。
  ;(form.on as (e: string, cb: () => void) => void)('select(selectGame)', () => {
    void onGameChange().then(() => reload(1))
  })

  /**
   * 「参考图」点开之后的三条出口。
   *
   * 取数走**已有的**订单截图端点（`api/screenshots.ts::fetchScreenshots` →
   * `GET /api/business/orders/<单号>/screenshots`，后端 `ScreenshotCollectionView.list_items`：
   * 只回 `status=ready` 的图，`url` 是服务端签发的短时效链接）。⚠️ **不要再造第二套 fetch 封装**
   * ——`api/screenshots.ts` 就是这条端点的唯一前端出口（上传页 / 管理弹层都走它）。
   *
   * - **空列表** ⇒ 一句人话点名「没图」（⛔ 不开一个空看图层，也不能像"坏了"那样只弹错误）；
   * - **有图** ⇒ `layer.photos`（先例 `emp-settlementemp.ts` 的收款码弹层），标题 `IMG_TIT`；
   * - **失败（403 / 网络 / 业务码）** ⇒ 原样弹 `errText(e)`：`api/http.ts` 已把后端 `msg` 放进
   *   `HttpError` / `BizError` 的 message（403 =「你没有订单截图相关的权限」），⛔ 不静默、
   *   ⛔ 不换一句自己编的文案。
   */
  async function openScreenshots(orderNo: string): Promise<void> {
    // 空串是**防御**（渲染期只会画带 `data-id` 的原生行）：别拿空单号去打一条注定不中的请求。
    if (orderNo === '') return
    try {
      const data = await fetchScreenshots(orderNo)
      if (data.items.length === 0) {
        msg(NO_SCREENSHOTS_TEXT)
        return
      }
      ;(layer.photos as (o: unknown) => void)({
        photos: { title: IMG_TIT, data: data.items.map(screenshotPhoto) },
      })
    } catch (e) {
      msg(errText(e))
    }
  }

  /* ── ⭐ 上家团团通接单（`origin="peer"`；D-0303） ──────────────
   *
   * 在途互斥的键 = **镜像行主键 `mirror_pk`**（两张镜像表的主键空间互不相干 ⇒ 按它分卡；
   * ⛔ 不用全页一个布尔：一张卡在等上家判决时，别家上家的卡不该被连坐禁点）。
   * ⚠️ 用 `Set` + `finally` 复位（漏了复位，那张卡以后**再也接不了**，E-0079 同族）。
   */
  const peerClaimsInFlight = new Set<number>()

  /** 这一行的两颗按钮是不是**已经按下、还在等受理**（点了不该再弹确认框、更不该再发）。 */
  function peerRowAlreadyClaimed(button: HTMLElement): boolean {
    if (button.classList.contains(PEER_DISABLED_BUTTON_CLASS)) return true
    const tr = button.closest('tr')
    return tr?.getAttribute(PEER_CLAIMED_ROW_ATTR) === '1'
  }

  /**
   * 受理成功 ⇒ 把这一行**置灰留在原地**（业务口径「置灰不删卡」，理由见 `PEER_CLAIMED_ROW_ATTR`）。
   *
   * ⚠️ 用 layui 现成的 `layui-btn-disabled`（⛔ 不新造样式表）：证据件里没有这颗按钮，
   * 视觉值只能取 layui 自带那一份。行上再打 `data-peer-claimed="1"`，即使有人把 class 抹掉，
   * 点击期那道闸（`peerRowAlreadyClaimed`）仍拦得住。
   */
  function markPeerRowClaimed(button: HTMLElement): void {
    const tr = button.closest('tr')
    if (tr) {
      tr.setAttribute(PEER_CLAIMED_ROW_ATTR, '1')
      tr.style.opacity = '0.55'
    }
    const scope: ParentNode = tr ?? root
    for (const other of Array.from(scope.querySelectorAll<HTMLElement>(CLAIM_BUTTON_SELECTOR))) {
      other.classList.add(PEER_DISABLED_BUTTON_CLASS)
      other.setAttribute('aria-disabled', 'true')
    }
  }

  /**
   * 发这一发接单请求（**已经在途集合里**才会走到这里）。
   *
   * 结局映射（后端业务码 → 页面动作，`api/hall.ts::claimPeerMirror` 的 docstring 有码表）：
   *  - 成功（`code 0`）⇒ 「正在受理中」+ 置灰不删卡、**不刷新**；
   *  - `1012`（被接走 / 本人正在处理中）⇒ 原样显示后端 `msg` + **就地刷新**
   *    （这一张的本地状态已变，刷新后它会以「已不在厅」的形态消失）；
   *  - 其余（`1015` 未开同步 / 抢单闸 / 押金 / 实名 / 403 / 网络）⇒ 原样显示 `msg`、卡保留。
   *
   * ⛔ **不自己编文案**：除了成功那一句（确认制的「还没接到单」必须说清楚），
   * 其余结局都用后端 `msg`——那才是「为什么接不了」的唯一出处（与另几路同一条纪律）。
   */
  function claimPeerRow(button: HTMLElement, mirrorPk: number): void {
    peerClaimsInFlight.add(mirrorPk)
    void claimPeerMirror(mirrorPk)
      .then(
        () => {
          markPeerRowClaimed(button)
          msg(PEER_CLAIM_ACCEPTED_TEXT)
        },
        (e: unknown) => {
          msg(errText(e))
          if (e instanceof BizError && e.code === PEER_CLAIM_TAKEN_CODE) {
            reload(currentPage)
          }
        },
      )
      .finally(() => {
        peerClaimsInFlight.delete(mirrorPk)
      })
  }

  /* ── 点击委托（页内按钮） ── */
  const onPageClick = (event: Event): void => {
    const target = event.target as HTMLElement
    if (target.closest('.refresh')) {
      event.preventDefault()
      reload(1)
      return
    }
    const shotBtn = target.closest<HTMLElement>(SHOT_BUTTON_SELECTOR)
    if (shotBtn) {
      event.preventDefault()
      // 异步取数（不阻塞委托）；成败都由 `openScreenshots` 自己收口——正文里**不吞错**。
      void openScreenshots(shotBtn.dataset.id ?? '')
      return
    }
    const claim = target.closest<HTMLElement>(CLAIM_BUTTON_SELECTOR)
    if (claim) {
      event.preventDefault()
      const rowId = claim.dataset.id ?? ''
      // 各源各打**自己的**端点（⛔ 不要拿远端单号去打原生接单端点，反之亦然）。
      const origin = claim.dataset.origin ?? 'native'
      // 「空」判据，不是真值判据（E-0021 同型纪律）：订单号 / 远端单号都是非空串。
      if (rowId === '') return
      // 两条镜像线走**按主键取行**的端点、参数是**镜像行主键**（不是上家单号）：
      //    `data-id` 已被渲染期写成主键的十进制串，这里再按正整数**收窄一次**
      //    （按钮是渲染期画的，但 data-* 是可以被人为改的 DOM ⇒ 形状先行拒绝，
      //    与 `api/jidanpuUpstream.ts::claimUpstreamMirror` / `claimPeerMirror` 里那道闸同一条纪律）。
      let mirrorPk: number | null = null
      if (origin === 'upstream' || origin === 'peer') {
        const parsed = Number(rowId)
        if (!isClaimableMirrorPk(parsed)) return
        mirrorPk = parsed
      }
      // ⭐ peer 行：在途 / 已受理时**连确认框都不弹**（双击的那第二下在这里就被吃掉）。
      if (origin === 'peer') {
        const pk = mirrorPk as number
        if (peerClaimsInFlight.has(pk) || peerRowAlreadyClaimed(claim)) return
      }
      ;(layer.confirm as (t: string, cb: (index: number) => void) => void)(
        CLAIM_CONFIRM_TEXT,
        (index: number) => {
          ;(layer.close as (i: number) => void)(index)
          // ⭐ peer 行在确认回调里**再判一次**（承重）：双击会先弹**两个**确认框，
          //    用户两个都点「确定」时，点击期那道闸（它在弹框之前）已经过了 ⇒ 只判一次会发两发。
          //    受理成功后卡片置灰同理 —— 确认框是点开那一刻拍的快照，之后状态可能已经变了。
          if (origin === 'peer') {
            const pk = mirrorPk as number
            if (peerClaimsInFlight.has(pk) || peerRowAlreadyClaimed(claim)) return
            claimPeerRow(claim, pk)
            return
          }
          // 失败**一律**原样显示错误文本（`errText(e)` → `e.message`）：HTTP 403 与业务码都由
          // `api/http.ts` 的现有统一分支映射成 `HttpError` / `BizError` 的 `msg`，
          // 本页**不特判 403、不吞、不换文案**（镜像单那两句业务文案 spec §6.4 也走这里）。
          // ⚠️ 权限门 / 数据门在**渲染期**（`buildHallCols` 的 `canClaim` + `claimTargetOf`），
          //    不在这里再判一次：这里再判一次会变成「按钮在、点了静默没反应」，比 403 更难排查。
          // ⚠️ 镜像行只剩 `upstream` 一条：`remote`（旧路）已整条拆除
          //    （后端不再产出 `origin="remote"` 行）⇒ 未知来源**一律不发请求**（fail-closed），
          //    ⛔ 不许把它落进 `claimOrder` 去拿远端单号打原生端点。
          if (origin !== 'native' && origin !== 'upstream') return
          const call: Promise<{ message: string }> =
            origin === 'upstream'
              ? claimUpstreamMirrorWithMessage(mirrorPk as number)
              : claimOrderWithMessage(rowId)
          void call.then(
            ({ message }) => {
              // 就地刷新（对标 `reloadOrderList()`）；页码取**当前页**——抢掉一行后本页少一行，
              // 不跳回第 1 页（业务方口径「就地刷新」）。
              reload(currentPage)
              // ⭐ 后端成功 `msg` 可能带「代练通订单未能撤回」警告 ⇒ 原文显示；
              // 空串才回退默认「抢单成功」；带警告用 `layer.alert`，⛔ 不一闪而过。
              showBackendSuccessMessage(backendMessageActions, message, '抢单成功')
            },
            (e: unknown) => msg(errText(e)),
          )
        },
      )
    }
  }
  root.addEventListener('click', onPageClick)

  // 回车 = 搜索（对标 `$(document).keyup(getKey)` → 触发 `.sechbtn` 点击）。
  const onKeydown = (event: KeyboardEvent): void => {
    if (event.key !== 'Enter') return
    if ((event.target as HTMLElement).tagName !== 'INPUT') return
    event.preventDefault()
    reload(1)
  }
  root.addEventListener('keydown', onKeydown)

  ;(form.render as () => void)()

  /* ── 自动刷新频率（spec §3.3；运行期注入，⛔ 证据件一个字不动） ── */

  /**
   * 把按钮插在证据件那颗 `.refresh` **之后**（`afterend` ⇒ 同一层级的紧随兄弟，与「刷新列表旁边」
   * 的视觉位置一致；⛔ 不改证据件、⛔ 也不塞进它内部）。
   *
   * ⚠️ 幂等：找不到锚点（证据件被人改坏了）时**什么都不做** —— 宁可少一颗按钮，也不许把按钮插到
   * 页面别处去（那样用户会在一排无关按钮中间看到它）。
   */
  const refreshAnchor = root.querySelector(REFRESH_ANCHOR_SELECTOR)
  if (refreshAnchor) refreshAnchor.insertAdjacentHTML('afterend', AUTO_REFRESH_BUTTON_HTML)
  const autoBtn = root.querySelector<HTMLElement>(AUTO_REFRESH_BUTTON_SELECTOR)

  /** 按钮文案 = 当前档位（spec §3.3 第 8 条）。 */
  function renderAutoRefreshButton(): void {
    if (autoBtn) autoBtn.textContent = autoRefreshLabel(autoRefreshValue)
  }

  /**
   * 7 档单选（`关闭 / 1 秒 / … / 60 秒`）。视觉值取**页内已有的那一份**（`layui-form` + 证据件里
   * 那种 `layui-btn`/`layui-input` 体系），⛔ 不新造样式表。
   *
   * ⚠️ 面板要包一层 `.layui-form` 并带 `lay-filter`：layui 的 `form.on('radio(name)')` 认的是
   * `<input lay-filter>`，而 `form.render('radio')` 要靠 `.layui-form` 定位（见 `layer.open` 的 `success`）。
   */
  function autoRefreshPanelHtml(): string {
    const item = (value: number, text: string): string =>
      `<div class="layui-form-item" style="margin-bottom:6px">` +
      `<input type="radio" name="${AUTO_REFRESH_RADIO_FILTER}" value="${value}" ` +
      `lay-filter="${AUTO_REFRESH_RADIO_FILTER}" title="${text}"` +
      `${value === autoRefreshValue ? ' checked="checked"' : ''}></div>`
    return (
      `<div class="layui-form" lay-filter="${AUTO_REFRESH_RADIO_FILTER}" ` +
      `style="padding:12px 15px" id="${AUTO_REFRESH_PANEL_ID}">` +
      item(AUTO_REFRESH_OFF, '关闭') +
      AUTO_REFRESH_STEPS_MS.map((ms) => item(ms, `${ms / 1000} 秒`)).join('') +
      `</div>`
    )
  }

  /** 打开设置面板（`layer.open({type:1, title, area:['320px','auto']})`，spec §3.3 第 2 条）。 */
  function openAutoRefreshPanel(): void {
    ;(layer.open as (o: unknown) => void)({
      type: 1,
      title: AUTO_REFRESH_PANEL_TITLE,
      area: [AUTO_REFRESH_PANEL_AREA_WIDTH, 'auto'],
      // 每次开层都用**新**节点（`autoRefreshPanelHtml` 里带上了当前选中档）⇒ 不缓存节点、不残留
      // 上一次的勾选状态。真 layui 会把它塞进 `.layui-layer-content`，故里面的点击**不经过**
      // `root` 的委托（`root` 之外）—— 选中档靠下面 `form.on(...)`，不需要额外的 DOM 监听。
      content: autoRefreshPanelHtml(),
      success: () => {
        // 必须 `form.render('radio')`：layui 的皮肤（那颗圆点）是渲染期画出来的，不 render 时
        // 原生 `<input type=radio>` 是隐藏的 ⇒ 面板里「一个都点不到」。
        ;(form.render as (t?: string) => void)('radio')
      },
    })
  }

  /**
   * 重开（或首次开）定时器。**唯一写入口**：档位变化、页面由隐藏转可见、卸载前都要经过它，
   * 这样「有几个定时器在跑」永远只有一个答案（⛔ 不留 interval 泄漏）。
   */
  function restartAutoRefresh(): void {
    if (timer !== null) {
      window.clearInterval(timer)
      timer = null
    }
    if (autoRefreshValue === AUTO_REFRESH_OFF) return
    timer = window.setInterval(onAutoRefreshTick, autoRefreshValue)
  }

  /**
   * 到点做什么（spec §3.3 第 4/5/6 条）：`reload(currentPage)` —— **保持当前页与当前筛选**
   * （正在看第 3 页就刷第 3 页，不跳回第 1 页）。
   *
   * 三道闸，缺一条都会给出**静默**的坏行为：
   *  ① 页面不可见 ⇒ 跳过这一拍（后台标签页不该每秒打一次三路查询）；
   *  ② 上一发未回 ⇒ 不重复发（在途互斥）；
   *  ③ 30 秒活性兜底 ⇒ 那一发**死了**（`done` 永远不会跑，例如请求被中间层掐掉）时允许下一发。
   *     ⚠️ 缺了 ③ 的后果是自动刷新**发出去一次就永久静默**——那是不可接受的失败形态
   *     （比「多发一次」贵得多）。判据写成 `> 30000`（严格大于）且用 `Math.abs`，这样系统时间
   *     被人为往前调也不会把兜底算成负数。
   */
  function onAutoRefreshTick(): void {
    if (document.hidden) return
    if (inFlight && Math.abs(Date.now() - issuedAt) <= AUTO_REFRESH_ALIVE_MS) return
    reload(currentPage, undefined, { done: onTableDone })
  }

  // 档位切换（`form.on('radio(autoRefreshMs)')`：layui 交回 `{value, elem}`）。
  ;(
    form.on as (e: string, cb: (obj: { value?: string }) => void) => void
  )(`radio(${AUTO_REFRESH_RADIO_FILTER})`, (obj) => {
    const next = normalizeAutoRefreshMs(obj?.value)
    autoRefreshValue = next
    writeStoredAutoRefreshMs(next)
    renderAutoRefreshButton()
    // 「关闭」⇒ 定时器停；换档 ⇒ 从**现在**重新计时（不沿用上一档剩下的零头）。
    restartAutoRefresh()
  })

  // 点按钮 = 开设置面板（挂在 `root` 上，与页内其余委托同一处）。
  if (autoBtn) {
    autoBtn.addEventListener('click', (event: Event) => {
      event.preventDefault()
      openAutoRefreshPanel()
    })
  }

  // 页面不可见 ⇒ 跳过这一拍；**变可见且已启用 ⇒ 立刻补刷一次**并把计时重置（spec §3.3 第 6 条）。
  // ⚠️ 挂在 `document` 上、不是 `root`：`visibilitychange` 是文档级事件，而岛页跑在 iframe 里，
  //    `document` 就是那个 iframe 的文档（隐藏判定与事件同源）。
  const onVisibilityChange = (): void => {
    if (document.hidden) return
    if (autoRefreshValue === AUTO_REFRESH_OFF) return
    restartAutoRefresh()
    onAutoRefreshTick()
  }
  document.addEventListener('visibilitychange', onVisibilityChange)

  // 初值从 `localStorage` 读（**fail-safe**：读失败 / 值非法都当「关闭」，见 `normalizeAutoRefreshMs`）。
  autoRefreshValue = readStoredAutoRefreshMs()
  renderAutoRefreshButton()
  restartAutoRefresh()

  return () => {
    // ⚠️ **必须清定时器 + 摘文档级监听**（有意偏离「对标整页刷新」的那一处）：我们挂在 iframe 里
    // 挂载 / 卸载，漏了就是泄漏 —— 已卸载的页面继续每秒发请求（先例：订单详情页的 2 分钟自动刷新
    // 在卸载函数里 `clearInterval`）。spec §3.3 第 9 条。
    if (timer !== null) {
      window.clearInterval(timer)
      timer = null
    }
    document.removeEventListener('visibilitychange', onVisibilityChange)
    root.removeEventListener('click', onPageClick)
    root.removeEventListener('keydown', onKeydown)
    root.innerHTML = ''
  }
}

export function mount(root: HTMLElement, ctx: IslandContext): Promise<() => void> {
  return mountHallPage(root, ctx)
}

export default { mount } satisfies IslandPage
