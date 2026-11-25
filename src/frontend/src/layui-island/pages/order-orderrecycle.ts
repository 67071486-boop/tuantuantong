/**
 * 【已删除的订单】（找回删除的订单）页 —— **自建 island 页 + 接真后端**（对标 `/Order/orderrecycle`，D-0144 / D9）。
 *
 * ⛔ **不再走 `artifactPage`**：那个共享渲染器的契约**逐字**写着「表格恒空、不发任何写请求」，
 * 而它是**几十个** island 页共用的渲染器（本批实测：**39 个**页文件仍在走 `artifactPage(`；
 * spec D9 记的「52 个调用方」是它写下时的口径）⇒ 为**一页**改它的契约，回归面是那几十页（D9 选 ②）。
 * 代价（如实）：本页脱离统一护栏 ⇒ **本页自己的 DOM 断言成为唯一防线**（见 `__tests__/`），
 * 且 `parity` 绿**不构成**本页接对了的证据（空表时表体不可见）。
 *
 * ## 证据（值一律从对标取，不凭记忆猜）
 *
 * - 标记 / 页内 `<style>` / 7 个筛选控件 / 工具栏 `#toolbarDemo` / 行内 `#baroperating`
 *   → `public/replica/order-orderrecycle.html`（源 `.tools/recon/<快照>/html/Order__orderrecycle.html`）
 * - 列定义 → 同快照 `assets/JS2019/Order/orderrecycle.js` **L58-173**
 * - 分页 → 同 JS **L174-176**：`page: true` / `limit: 20` / `limits: [10,20,30]`
 * - 统计文案 → 同 JS **L183**：`'【统计】共' + res.total + '笔被删除的订单'`
 * - 日期控件 → 同 JS **L20-38**（两次 `laydate.render`，`min`/`max` 取 `#minD`/`#maxD`）
 * - 工具栏 / 行内事件 → 同 JS **L191-216**（`refresh` / `refOrder`）
 *
 * ## 12 列（逐条照 `orderrecycle.js` L59-172；⛔ 不搬 `nresize` 这个无效拼写、⛔ 不写 `fixed:''`）
 *
 * | # | field | title | 宽度（桌面分支） |
 * |---|---|---|---|
 * | 1 | `type:'checkbox'` | — | `'2%'`（`fixed:'left'`） |
 * | 2 | `orderIdView` | 订单号 | `'11%'` |
 * | 3 | `roleName` | 角色名 | 130 |
 * | 4 | `gameGroup` | 阵营/平台 | `'4%'`（`align:'center'`） |
 * | 5 | `project` | 代练/陪玩项目 | `'12%'` |
 * | 6 | `gameInfo` | 角色信息 | `'13%'` |
 * | 7 | `orderSta` | 订单状态 | `'4%'`（`align:'center'`） |
 * | 8 | `orderSoure` | 旺旺/来源 | `'5%'`（`align:'center'`） |
 * | 9 | `orderRemark` | 重要备注 | `'5%'` |
 * | 10 | `delTime` | 删除时间 | 140 |
 * | 11 | `delUserName` | 删除人 | 90 |
 * | 12 | `toolbar:'#baroperating'` | 操作 | 120 |
 *
 * 表体的 HTML 由本页自己的 `buildRecycleRow()` 拼（列定义**不含** `templet`，与改造前逐字相同）——
 * 值都来自**请求数据**（角色名 / 备注 / 来源 / 标签 …），拼进 HTML 前一律过 `escapeHtml`。
 *
 * ## 「订单状态」列的配色（对标 `orderrecycle.js` L116-142 的 switch，逐档对照）
 *
 * | 对标档 | 色值 | 我方状态码 | 说明 |
 * |---|---|---|---|
 * | 待上号 | `#585858` | （无对应档） | 我方枚举没有「待上号」（§9-R13 的档位差） |
 * | 代练中 | `#219227` | `IN_PROGRESS` | 同名档 |
 * | 待验收 | `#ff6a00` | `PENDING_REVIEW` | 同名档 |
 * | 已完成 | `#0094ff` | `COMPLETED` | 同名档 |
 * | 已暂停 | `#FF5722` | `PAUSED` | 同名档 |
 * | 问题单 | `#d9764a` | `ISSUE` | 同名档 |
 * | 已退单 | `#efaaaa` | `REFUNDED` | 同名档 |
 * | （无 case） | `#b0aeae;` | `UNASSIGNED` / `WAITING_GRAB` | 对标没有这两档 ⇒ 落它自己的默认色 |
 *
 * ## 与对标不同（每条说清「对标怎样 / 我们怎样 / 依据」；⛔ 不是缺陷，也不是「尚未开放」）
 *
 * | # | 对标怎样 | 我们怎样 | 依据 |
 * |---|---|---|---|
 * | 1 | 走共享 `artifactPage` 渲染器（契约是「表格恒空、不发任何写请求」） | **整页自建** island 页（自己取证据件 / 塞标记 / `table.render`） | D9；见文件头第一段（⛔ 不为**一页**去改那个共享渲染器的契约：本批实测 **39 个**页文件仍在走 `artifactPage(`） |
 * | 2 | 「批量还原钩选的订单」把勾选的单号**逗号串**一次发给 `RefOrderRecycle` | **收窄成一次一笔**：勾 >1 笔只提示、**零请求**；勾 0 笔的文案照对标「请选中要操作的记录」 | spec §5.5-#1 |
 * | 3 | 第 4 列「阵营/平台」渲染阵营值 | **恒空**单元格 | 本仓无阵营字段（§9-R14）；⛔ 不用别的字段冒充 |
 * | 4 | 第 6 列「角色信息」= `gameArea + '/' + gameServer + '/' + job + '/' + levelMark + '/' + itemRemark`（正文少 `levelMark` 那一段） | 只有 `server.name` / `game_role_name` / `game_account`，缺的段**留空** | §5.5-#6；`/` 分隔与「正文少 `levelMark` 那段」的形状照对标的表达式 |
 * | 5 | 第 8 列「旺旺/来源」的中段分支：来源含「微信」/「QQ」且 `qq` 非空 ⇒ 显示 `qq` | 分支照对标留着，但**今天落空**（载荷没有 `customer_qq` 键 ⇒ 走不到那一支） | 后端 `RecycleBinListSerializer.fields` 里没有 `customer_qq`；该键一出现分支就会生效 |
 * | 6 | 「订单状态」筛选下拉的 `<option value>` 是**中文值**，选项集合是它那一页的服务端枚举（比全站少几档） | `<option value>` 用**我方枚举码**，选项集合取全站共享枚举（含对标没有的「待抢单」） | 后端 `status` 是 `ChoiceField(OrderStatus.choices)`；业务方裁定「同一个状态在任何页面、任何可见文案里必须同一词」⇒ 文案取 `@/config/orderStatus`（档位差另登记 spec §9-R13） |
 * | 7 | 「游戏名称」下拉的「全部游戏」`value` 是**它自己的游戏名** | 「全部游戏」用 `value=""` | 我方参数是 id（`game`）；「全部」= 空值 = **不发**该参数。与结算记录页同处置 |
 * | 8 | 删除时间的 4 个值（`#sDate`/`#eDate`/`#minD`/`#maxD`）是**它服务端按取数当天**渲染的死值（快照里 `2026-07-20`/`2026-09-20`） | **运行期算** | 那 4 个值**真的发给后端**当筛选 ⇒ 照抄会让「快照日之后删的单默认看不见」（与绩效管理页同一类真 bug，见 `FRONTEND-REPLICA-STANDARD.md` §7） |
 * | 9 | 第 10 列「删除时间」是它服务端**已格式化**的值（快照里无从对拍） | 按本仓既有约定格式化成 `YYYY-MM-DD HH:mm`（`replace('T',' ').slice(0,16)`） | 载荷是 DRF 的 ISO 串；约定取 `order-orderlist.ts` 的同一处 |
 * | 10 | 三个**已删单上的写动作**（点订单号看详情 / 点状态徽标改状态 / 点备注改备注）都真发请求 | **不接**，点了给固定文案 | 裁定 3 / U11：已删单的详情与写动作必然 `1002` ⇒ 让用户撞业务码更糟；文案 `已删除的订单不能查看详情` 是我方自拟 |
 * | 11 | 还原成功后弹它服务端响应体里的 `msg`，再重取列表 | **不弹 msg**，只重取列表（反馈 = 该行从表里消失） | spec §7.3 写「`layer.msg(响应 msg)` + 重取列表」，而本仓统一请求通道 `request<T>()` **成功只回 `data`**（信封里的 `msg` 被 `unwrap()` 丢掉，读码 `api/unwrap.ts`）⇒ 我们**拿不到**那句文案，自造一句就是「编文案」（标准 §5）⇒ 只保留重取（⛔ 不为弹一句 msg 去换掉统一请求通道）。失败分支的文案照旧来自 `request()` 抛出的错误 |
 * | 12 | 第 2 列订单号的容器与链接 `title` 承诺「鼠标右键看看~」/「点击查看订单详情，也可以鼠标右键试试看」，但**对标本页没有右键行为** | 去掉「右键」那半句：两个 `title` 都改成**左键的真实去向**（已删单看不了详情），与点击出口**同源**（`RECYCLE_DETAIL_NOT_OPEN_TEXT`） | 2026-09-24 修复实测：证据件 `Order__orderrecycle.html` 只引 `layui.js` / `common.js` / `orderrecycle.js`（**没有** jQuery、没有 `jquery.contextmenu.*`），且 `orderrecycle.js` 与 `common.js` 的 `grep -c contextmenu` 均为 **0** ⇒ 那两句是从订单列表页 templet 抄来的。⛔ 不留「说了做不到」的提示；「我的订单」页对标确有 `$.contextMenu`、那页按「尚未开放」收口，与本页无关 |
 *
 * ## 已知限制（如实登记，⛔ 不是「与对标不同」）
 *
 * 表格数据由本页自己取（`fetchRecycleBin` → `table.reloadData`，house 口径：统一信封 / CSRF /
 * 错误映射都在 `api/` 一处，⛔ 不给 layui 传 `url`）。**代价**：layui 的分页条按**本地数据行数**
 * 算 `count`（读码 `public/layui/layui.js` 偏移 ~292215：`o.url ? … : o.data.length`）⇒
 * 分页条上的「共 N 条」是本页行数、翻页到不了下一页；**统计文案 `#total` 不受影响**
 * （它由 `load()` 用服务端 `total` 写）。要根治得让 layui 走它自己的 `url`/`ajax` 通道，
 * 那会绕开统一信封与 CSRF（反面先例见 `order-orderview.ts` 文件头第 20 条）。
 *
 * ⛔ **作废（2026-09-24，本修复，提交信息 `fix: 回收站列表改走 url 模式使分页可用`）**：
 * 上面那一段**已不成立**，现在的事实——本页列表改走 layui 的 **`url` 模式**
 * （与 `order-orderlist.ts` 的 `table.render` 同形）：`table.render` 给
 * `url`（拼自 `import.meta.env.BASE_URL`，⛔ 不写死 `/`）+ `method:'get'` +
 * `request:{pageName:'page', limitName:'page_size'}`，`parseData` 吃统一信封并把
 * `r.data.total` 写进 `count` ⇒ **分页条上的「共 N 条」就是服务端总数、点下一页真的会取数**。
 * 当年那句「会绕开统一信封与 CSRF」不成立：GET 取数不涉及 CSRF，信封由 `parseData` 一处解开；
 * 写请求（还原）仍旧走 `api/orders.ts::restoreOrder`。
 * ⛔ 上面那段原文提到的 `api/orders.ts::fetchRecycleBin` 已随本次修复**删除**（改走 url 通道后
 * 全仓 **0 调用点**）⇒ 别再按那段原文去找它。
 * ⚠️ **代价（如实补记）**：走 layui 的 url 通道后，本页的**列表取数这一路**不再经过
 * `api/http` 的统一错误映射——会话过期时它**不会**触发 `setUnauthorizedHandler` 去跳登录，
 * 而是落到 layui 表格自己的错误视图（读码 `public/layui/layui.js` 的 `table` ajax `error`
 * 分支：`n.errorView(s.$t("table.xhrError",{msg:t}))`；该 i18n 键的**实际文案**是
 * 「**请求异常，错误提示：{msg}**」——⛔ 不是「数据接口请求异常」，那个串在 `layui.js` 里
 * **0 命中**（T10 收口时实测，`layui.js` 的 i18n 表里该键的值是
 * `请求异常，错误提示：{msg}`；同仓 `emp-emplist.ts` 的注释引的就是这一句）。
 * 写请求（还原）仍走 `api/http`，那一跳不受此影响。
 *
 * ⚠️ 12 列的 `field` / `title` / 宽度 / 顺序 / `fixed` / `align` **一个字没动**；表体仍由
 * `buildRecycleRow()` 拼，只是改在 `parseData` 里对 `items` 逐个映射（12 列**没有** `templet`，
 * 不映射则所有单元格恒空——`url` 模式把 items 原样交给表格，不像本地数据模式先映射过）。
 *
 * ## PII
 *
 * 无。本页唯一的静态文本来自证据件（14 个游戏名 / 9 个状态枚举 / 死日期值），行数据一律来自
 * 当前租户的接口；测试夹具全是**合成值**。
 */
import { fetchGames, fetchServers, type ConfigRow } from '@/api/config'
import { fetchOrderSources, type OrderSourceRow } from '@/api/orderSources'
import { restoreOrder, type RecycleBinRow, type RecycleBinSearchParams } from '@/api/orders'
import { ORDER_STATUS_LABELS, ORDER_STATUS_VALUES } from '@/config/orderStatus'
import {
  bootstrapIsland,
  initIslandElements,
  islandMsg,
  type IslandContext,
  type IslandPage,
} from '@/layui/island'
import { escapeHtml } from '@/layui-island/escape'
import { withRawCells } from '@/layui-island/rawCell'
import { getFormSelects, initFormSelects } from '@/layui/vendor'
import type { LayuiModule } from '@/layui/loader'

/** 证据件（对标页 `<style>` 段 + body 标记，零转义，可直接与对标原文逐行对拍）。 */
export const RECYCLE_ARTIFACT_URL = 'replica/order-orderrecycle.html'

/**
 * 站点基址（子路径部署时不是 `/`；与 `order-orderlist.ts:702` 同一写法）。
 *
 * ⛔ 不写死 `/`：本页的列表取数改走 layui 的 `url` 模式后，URL 由**这里**拼出来交给 layui，
 * 写死会让子路径部署的页面取到 404（`order-billsummary.ts` 的基址守卫是同一族）。
 */
const API_BASE = import.meta.env.BASE_URL || '/'

/** 回收站列表端点（后端 `RecycleBinSearchSerializer` 对应的 viewset 动作的 path）。 */
export const RECYCLE_API_PATH = '/api/business/orders/recycle-bin/'

/**
 * 交给 layui `table.render` 的**完整 URL**（`url` 模式下取数由 layui 自己发）。
 *
 * ⛔ 本页**没有**（也不再需要）`api/orders.ts` 里的列表取数函数：原 `fetchRecycleBin`
 * 已随本次修复删除（**0 个可执行引用**；`grep -rn "fetchRecycleBin" src/` 命中的都是
 * 「它已删除」这一类说明注释，没有 import / 调用）。
 * 新增列表参数时改这里交给 layui 的 `where`（`toRecycleSearch`），⛔ 别再包一个前端函数——
 * 那会造出第二处取数出口，与 layui 的 `url` 通道抢同一格。
 */
export const RECYCLE_API_URL = `${API_BASE.replace(/\/$/, '')}${RECYCLE_API_PATH}`

/** 对标页服务端渲染的 `<title>`（`Order__orderrecycle.html:5`）。 */
export const RECYCLE_TITLE = '已删除的订单'

/** 对标 `table.render` 的 `elem`（`orderrecycle.js:53`；HTML L169 的 `id="tableList"`）。 */
export const RECYCLE_TABLE_ELEM = '#tableList'

/**
 * `table.reload` / `table.on` 要的是**实例 id**（不带 `#`），传 `#id` 是静默 no-op
 * （E-0061 ③）——所以选择器与 id 分成两个常量，⛔ 不要用 `RECYCLE_TABLE_ELEM.slice(1)` 现算。
 */
export const RECYCLE_TABLE_ID = 'tableList'

/** 对标 `toolbar`（`orderrecycle.js:56`；模板在 HTML L170-176）。 */
export const RECYCLE_TOOLBAR_ELEM = '#toolbarDemo'

/** 对标行内模板（HTML L178-180）——末列的 `toolbar` 指向它。 */
export const RECYCLE_OPERATING_TOOLBAR = '#baroperating'

/** 对标 L175-176：`limit: 20` / `limits: [10,20,30]`（后端 `page_size` 的允许集同值）。 */
export const RECYCLE_PAGE_LIMIT = 20
export const RECYCLE_PAGE_LIMITS = [10, 20, 30] as const

/** 统计文案模板（**逐字**取 `orderrecycle.js:183`，写进工具栏里的 `#total`）。 */
export const RECYCLE_TOTAL_TEMPLATE = (count: number): string => `【统计】共${count}笔被删除的订单`

/** 勾了多笔时的提示（我方文案：批量还原未开放，spec §5.5-#1）。 */
export const RECYCLE_BATCH_NOT_OPEN_TEXT = '批量操作尚未开放，请只勾选一笔订单'

/** 一笔都没勾时的提示（**逐字**取对标 `orderrecycle.js:197`）。 */
export const RECYCLE_ROW_SELECT_TEXT = '请选中要操作的记录'

/** 已删单的三个写动作都不接，统一这句（我方自拟；理由见文件头差异 #10）。 */
export const RECYCLE_STATUS_NOT_OPEN_TEXT = '尚未开放'

/** 点订单号那一下的固定文案（**不跳转**；已有详情必然 `1002`）。 */
export const RECYCLE_DETAIL_NOT_OPEN_TEXT = '已删除的订单不能查看详情'

/** 状态码 → 徽标底色（对标那个 switch 的逐档对照表见文件头）。`Map` 避开原型链键。 */
const RECYCLE_STATUS_COLORS = new Map<string, string>([
  ['IN_PROGRESS', '#219227'],
  ['PENDING_REVIEW', '#ff6a00'],
  ['COMPLETED', '#0094ff'],
  ['ISSUE', '#d9764a'],
  ['PAUSED', '#FF5722'],
  ['REFUNDED', '#efaaaa'],
])

/**
 * 未知 / 未覆盖档走对标自己的默认色。
 *
 * ⚠️ 末尾那个**分号是逐字照抄对标的笔误**（`orderrecycle.js:117` 的字符串里就带着它）：
 * 拼进 `background-color:` 会得到 `#b0aeae;;`——**无害**（浏览器丢弃多余的空声明），
 * ⛔ 别「顺手修掉」它：它是与对标逐字对拍的证据，改了之后这一格就对不上证据件了。
 */
const RECYCLE_STATUS_DEFAULT_COLOR = '#b0aeae;'

/** 状态码 → 徽标底色（未覆盖的两档 `UNASSIGNED` / `WAITING_GRAB` 也落默认色）。 */
export function recycleStatusColor(status: string): string {
  return RECYCLE_STATUS_COLORS.get(status) ?? RECYCLE_STATUS_DEFAULT_COLOR
}

/**
 * 12 列（**逐条**照抄 `orderrecycle.js:58-173`；与改造前的 `buildCols()` 内容**完全一致**，
 * 只有函数名改了：它**不含 `templet`**，表体的渲染由本页自己的 `buildRecycleRow()` 负责）。
 */
export function buildRecycleCols(): Array<Record<string, unknown>> {
  return [
    { type: 'checkbox', fixed: 'left', width: '2%' },
    { field: 'orderIdView', title: '订单号', width: '11%' },
    { field: 'roleName', title: '角色名', width: 130 },
    { field: 'gameGroup', title: '阵营/平台', align: 'center', width: '4%' },
    { field: 'project', title: '代练/陪玩项目', width: '12%' },
    { field: 'gameInfo', title: '角色信息', width: '13%' },
    { field: 'orderSta', title: '订单状态', align: 'center', width: '4%' },
    { field: 'orderSoure', title: '旺旺/来源', align: 'center', width: '5%' },
    { field: 'orderRemark', title: '重要备注', width: '5%' },
    { field: 'delTime', title: '删除时间', width: 140 },
    { field: 'delUserName', title: '删除人', width: 90 },
    // `toolbar` 少了它这一列会**静默变成空列**（空表下不可见）。
    { title: '操作', width: 120, toolbar: RECYCLE_OPERATING_TOOLBAR },
  ]
}

/**
 * **运行期**渲染用的列 = `buildRecycleCols()` **原样** + 给每个数据列挂一个原样透传的
 * `templet`（`rawCell.ts` 的 `withRawCells`）。
 *
 * ⛔ **不动 `buildRecycleCols()`**：它是与证据件 `orderrecycle.js:58-173` 对拍的对象、
 * 被单测按「12 列 / 列宽 / 标题」钉住（同 `order-tbsendrecordlist.ts::buildRenderCols` 的处置）。
 *
 * ⚠️ **为什么必须挂**（读码，见 `rawCell.ts` 的模块 docstring）：layui 2.13.9 的表原型配置是
 * `escape:!0`，而列上没有 `escape` 键时取表配置 ⇒ **不挂 `templet` 的数据列会被当纯文本转义**；
 * 本页的单元格值是 `buildRecycleRow()` 拼好的 HTML ⇒ 那正是「单元格里显示 HTML 源码」的现象。
 * 挂了 `templet` 的列，templet 的返回值被**原样**使用。
 */
export function buildRecycleRenderCols(): Array<Record<string, unknown>> {
  return withRawCells(buildRecycleCols())
}

/** 筛选栏读出来的**原始**值（键名 = 证据件里的 `name`，值一律是字符串）。 */
export type RawRecycleFilterValues = {
  gameName?: string
  orderSta?: string
  sDate?: string
  eDate?: string
  orderSoures?: string
  key1?: string
  gameAcc?: string
}

/**
 * 筛选表单 → 后端参数（spec §5.2 的 7 个）。
 *
 * **空值不发**（与 `toOrderParams` 同口径）：空串 / 纯空白一律丢掉，于是「全部游戏」「全部」
 * 这些首项不会变成筛选条件。
 *
 * ⚠️ **数字参数的判据是「有没有给」，不是真值**（E-0021）：初版写成
 * `Number.isInteger(v) && v > 0`，`'0'` 会被当「没给」**静默丢掉**一个筛选条件。现在只要
 * 非空串且能解析成整数就照发——`0` 是合法边界值（本页控件平时选不出它：option 来自真实主键，
 * 但这不该是判据的**承重逻辑**）。畸形串（`abc` / 小数）仍然不发：那是送出去只会撞后端 `1000`
 * 的值，本页也不产生它。`min_value=1` 那道校验留在后端（`RecycleBinSearchSerializer`），
 * ⛔ 不在前端用「悄悄丢掉」冒充校验。
 */
export function toRecycleSearch(values: RawRecycleFilterValues): RecycleBinSearchParams {
  const params: RecycleBinSearchParams = {}
  const game = Number(values.gameName)
  if (
    values.gameName !== undefined &&
    values.gameName.trim() !== '' &&
    Number.isInteger(game)
  ) {
    params.game = game
  }
  // 「订单状态」下拉的 value 就是**枚举码**（本页在挂载时按共享枚举重建了 options）。
  if (values.orderSta !== undefined && values.orderSta !== '') params.status = values.orderSta
  if (values.sDate !== undefined && values.sDate !== '') params.deleted_from = values.sDate
  if (values.eDate !== undefined && values.eDate !== '') params.deleted_to = values.eDate
  const source = Number(values.orderSoures)
  if (
    values.orderSoures !== undefined &&
    values.orderSoures.trim() !== '' &&
    Number.isInteger(source)
  ) {
    params.order_source = source
  }
  const keyword = values.key1?.trim() ?? ''
  if (keyword !== '') params.keyword = keyword
  const account = values.gameAcc?.trim() ?? ''
  if (account !== '') params.game_account = account
  return params
}

/** 删除时间默认区间的四个值（`#minD` / `#maxD` 是日期控件上下界，不是筛选条件）。 */
export type RecycleDateRange = { minD: string; maxD: string; sDate: string; eDate: string }

const pad2 = (value: number): string => String(value).padStart(2, '0')
const ymd = (date: Date): string =>
  `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`

/**
 * 默认删除时间区间：**今天 −2 个自然月 `00:00:00`** ~ **今天 `23:59:59`**（本地时区）。
 *
 * ⚠️ **偏移量是「推断」级证据，不是事实**（`AGENTS.md` §5-C 的三级标注）：可得的证据件只有
 * **一张快照**——取数当天 2026-09-20 的服务端渲染里 `#sDate` = `2026-07-20 00:00:00`、
 * `#minD` = `2026-07-20`。从「2026-09-20 → 2026-07-20」这**一对值**反推出「今天减 2 个自然月」，
 * 这是个**推断**：证据件没有第二张不同月份的渲染结果，无法排除「上上个月 20 号」「减 62 天」
 * 等同值写法。⛔ 不要把它当既定事实引用；要坐实得再取一张跨月的快照对拍。
 * ⛔ 也不 import `@/layui-island/dateRange`：那是**别的几页**的偏移（3 个月 / 昨天），
 * 各页偏移不许互相照抄（那个文件的注释自己写着这一条）。
 *
 * ⚠️ **日期要先夹到目标月的月末**（下面的 `lastDay`）：朴素写法
 * `new Date(now.getFullYear(), now.getMonth() - 2, now.getDate())` 在目标月**没有那一天**时
 * 会**溢出**到下月 1 日（`1/31 → 12/1`、`8/31 → 7/1`、`4/29 → 3/1`）⇒ 默认区间整体偏一天多，
 * 把本该落在默认视图里的已删单挡在外面。判据见页测试的同名夹取用例。
 */
export function defaultRecycleDateRange(now: Date = new Date()): RecycleDateRange {
  // 先用**1 号**构造目标月（1 号在任何月份都存在 ⇒ 不会溢出），再把日夹到该月最后一天。
  const target = new Date(now.getFullYear(), now.getMonth() - 2, 1)
  const lastDay = new Date(target.getFullYear(), target.getMonth() + 1, 0).getDate()
  target.setDate(Math.min(now.getDate(), lastDay))
  return {
    minD: ymd(target),
    maxD: ymd(now),
    sDate: `${ymd(target)} 00:00:00`,
    eDate: `${ymd(now)} 23:59:59`,
  }
}

/** 表格行（键 = 列定义里的 `field`；值 = 已渲染好的单元格 HTML），外加定位用的 `order_no`。 */
export type RecycleCellRow = Record<string, string> & { order_no: string }

/**
 * 把一条接口行映射成表格行（第 2/5/6/7/8/9/10/11 列的 HTML 都在这里拼）。
 *
 * ⚠️ **本函数是转义出口**：所有来自请求数据的字面都过 `escapeHtml`。
 * ⛔ 它**不是**渲染层——单元格能不能原样显示由**列上的 `templet`** 决定
 * （spec §2.1 的真浏览器探针 + 读码 `public/layui/layui.js` 偏移 279727 的 `escape:!0`、
 * 偏移 272879 的 `("escape"in a?a:t).escape&&(i=m.escape(i))`：列上没有 `escape` 键时取
 * 表配置、默认**转义**；紧接着 `t=e.text&&a.exportTemplet||a.templet||a.toolbar` ⇒ **挂了
 * `templet` 的列**才原样使用）。运行期列由 `buildRecycleRenderCols()` 派生（挂 templet）。
 *
 * ⚠️ **改准**：本函数原先那句「layui 的默认单元格渲染**不转义**」是**错的**
 * ——按上面两处读码事实，默认是**转义**。正是那句错的读码让本页以为「拼好 HTML」就够了、
 * 漏了挂 `templet`，于是用户在回收站页看到的是 `<div class="orderLine" …` 这串源码。
 * 转义仍然是必须的：`templet` 把返回值原样插入，未转义的用户输入就是存储型 XSS。
 */
export function buildRecycleRow(
  row: RecycleBinRow,
  serverName: (id: number | null) => string,
): RecycleCellRow {
  const orderNo = row.order_no
  const safeNo = escapeHtml(orderNo)
  // ⭐ D-0301：展示值 = `source_order_no || order_no`（回收站载荷早就有
  // `source_order_no` 键，本页此前没用它）。只换**展示位**；寻址键（见下）照旧内置号。
  const safeViewNo = escapeHtml(row.source_order_no || orderNo)
  // ⚠️ **先兜空再判空**（`?? ''` 之后才看 `!== ''`）：`tag` / `game_account` 是**请求数据**，
  // 旧形状的载荷（后端补这两列之前）里它们缺席 ⇒ `escapeHtml(undefined)` 会抛
  // `TypeError: Cannot read properties of undefined (reading 'replace')`（`escape.ts:17`），
  // 而这里在 `parseData` 里跑 ⇒ layui 的 success 回调整条抛、`done` 到不了、
  // **表格一行都不渲染、`#total` 也不写**（T6 抓到的致命缺陷）。
  // 兜空后缺键 = 该段为空，⛔ 不是把字面 `undefined` 拼进模板串。
  const tag = row.tag ?? ''
  const safeTag = escapeHtml(tag)
  const safeRole = escapeHtml(row.game_role_name)
  const safeRemark = escapeHtml(row.remark)
  const safeSource = escapeHtml(row.source)

  // 第 2 列「订单号」：对标 `orderIdView` 的 templet（`orderrecycle.js:61-88`）。
  // 徽标只渲染**我们有数据**的两种：「急」（`is_urgent`）与标记（`tag` 取首字）；
  // 「新」/「拆」/「团」不渲染（载荷没有 `isRead` / `isSplitOrder` / `taskId`，见文件头差异）。
  const urgent = row.is_urgent
    ? '<span class="lableb" style="background-color:#FFBBFF" title="加急订单，优先安排">急</span>'
    : ''
  const tagBadge =
    tag !== ''
      ? `<a href="javascript:void(0);" class="lableb" style="background-color:#1E9FFF" title="标记：${safeTag}">${escapeHtml(tag.substring(0, 1))}</a>`
      : ''
  // ⚠️ **两处 `title` 的文案是本页改准的**（对标原文：容器 `title="鼠标右键看看~"`、
  // 链接 `title="点击查看订单详情，也可以鼠标右键试试看"`，`orderrecycle.js:83/87`）。
  // 那两句在**本页对标侧就没有右键行为**，三路取证（2026-09-24 修复）：
  //  ① 对标 `Order__orderrecycle.html` 只引 `layui.js` / `common.js` / `orderrecycle.js`，
  //     **没有** jQuery、也**没有** `jquery.contextmenu.*`（那两样是订单列表页的依赖）；
  //  ② `orderrecycle.js` 里 `contextmenu|右键` **0 命中**（本页只绑了 4 个事件：文件头证据段）；
  //  ③ `common.js`（8.7KB、只有校验 helper）里 `contextmenu` 也 **0 命中**。
  //  ⇒ 那两句是从**订单列表页**的 `orderIdView` templet 抄来的（列表页才有 `$.contextMenu`；
  //  「我的订单」页对标确有 `$.contextMenu`、本仓那页按「尚未开放」收口——本页不属于那两页）。
  //  本页左键的真实去向是 `onRootClick` 收口的那句固定文案（已删单看不了详情）⇒ `title` 与它
  //  **同源**（同一个常量），⛔ 不留「说了做不到」的提示。差异登记见文件头「与对标不同」表。
  const orderTip = RECYCLE_DETAIL_NOT_OPEN_TEXT
  const orderIdView =
    `<div class="orderLine" id="order_${safeNo}" data-id="${safeNo}" data-orderIdView="${safeViewNo}" title="${orderTip}">` +
    `<span id="orderid_span_${safeNo}" style="display:none" class="orderIDcopy" data-clipboard-text="${safeViewNo}"></span>` +
    urgent +
    tagBadge +
    `<a href="javascript:void(0);" class="orderdetails" data-viewId="${safeViewNo}" data-id="${safeNo}" style="color:#0094ff" title="${orderTip}">${safeViewNo}</a>` +
    `</div>`

  // 第 6 列「角色信息」：逐字照 `orderrecycle.js:106-113` 的表达式——
  // `title = area + server + '/' + job + '/' + level + '/' + itemRemark`、
  // `正文 = area + server + '/' + job + '/' + itemRemark`（**正文少 `levelMark` 那一段**），
  // 其中 `area = gameArea + '/'`。我方没有 `gameArea` / `levelMark` ⇒ 以空串代入
  // （`area` 因此是 `/`）、`gameServer` 取区服名、`gameJob` 取角色名、`itemRemark` 取游戏账号。
  const area = '/'
  const server = serverName(row.server)
  const job = row.game_role_name
  const level = ''
  // 同 `tag`：缺键时兜空串（见上面那段注释）——`gameInfo` 的该段因此为空，
  // ⛔ 绝不出现字面 `undefined`。
  const item = row.game_account ?? ''
  const infoTitle = `${area}${server}/${job}/${level}/${item}`
  const infoBody = `${area}${server}/${job}/${item}`
  const gameInfo = `<span title="${escapeHtml(infoTitle)}">${escapeHtml(infoBody)}</span>`

  // 第 7 列「订单状态」：可点 `.setdlsta`（**不接**，见 `mount` 里的收口）。
  const label = ORDER_STATUS_LABELS[row.status as keyof typeof ORDER_STATUS_LABELS] ?? row.status
  const orderSta = `<a href="javascript:void(0)" class="lableb setdlsta" data-id="${safeNo}" title="点击可以更改订单状态" style="background-color:${recycleStatusColor(row.status)};">${escapeHtml(label)}</a>`

  // 第 8 列「旺旺/来源」：分支逐字照 `orderrecycle.js:147-163`。中段（含「微信」/「QQ」
  // 且有 `qq`）今天落空——载荷没有 `customer_qq` 键（见 `RecycleBinRow` 的类型注释）。
  const qq = row.customer_qq ?? ''
  let sourceInner: string
  if (row.customer_ww !== '') {
    // ⚠️ 有意与对标不同（我方**更安全**）：对标把旺旺 uid **原样拼进 URL**，我们对它过一遍
    // `encodeURIComponent`——`customer_ww` 是录入的自由文本，含 `&` / `#` / 空格时会改掉这条
    // 链接的参数结构（对标那种拼法在这一格上是注入面）。链接形状与文案其余部分逐字照对标。
    sourceInner =
      `<a href="http://amos.alicdn.com/getcid.aw?v=2&uid=${encodeURIComponent(row.customer_ww)}&site=cntaobao&s=2&groupid=0&charset=utf-8" target="_blank" title="点击可以启动旺旺与该买家对话">` +
      `${escapeHtml(row.customer_ww)}</a>`
  } else if (qq !== '' && (row.source.includes('微信') || row.source.includes('QQ'))) {
    sourceInner = escapeHtml(qq)
  } else {
    sourceInner = safeSource
  }
  const orderSoure = `<span title="该订单来源：${safeSource}">${sourceInner}</span>`

  // 第 9 列「重要备注」：逐字照 `orderrecycle.js:165-171`——`title` 对 `null` 才用默认文案
  // （本仓载荷恒为字符串 ⇒ 只有空串那一支），正文空值显示「无」。
  const orderRemark =
    `<a href="javascript:void(0)" title="${safeRemark}" style="color:#E03636" class="orderRemark" data-id="${safeNo}" data-remark="${safeRemark}">` +
    `${safeRemark === '' ? '无' : safeRemark}</a>`

  return {
    order_no: orderNo,
    orderIdView,
    roleName: safeRole,
    // 第 4 列：本仓无阵营字段（§9-R14）⇒ **空单元格**，⛔ 不用别的字段冒充。
    gameGroup: '',
    project: `<span title="${escapeHtml(row.project_text)}">${escapeHtml(row.project_text)}</span>`,
    gameInfo,
    orderSta,
    orderSoure,
    orderRemark,
    // 第 10 列：载荷是 DRF 的 ISO 串（`2026-09-24T10:00:00+08:00`），按本页既有约定取前 16 位。
    delTime: escapeHtml(row.deleted_at.replace('T', ' ').slice(0, 16)),
    // 「删除人」显示**账号名称**（昵称优先、空串回落登录账号）：后端 2026-10-17 线新增
    // `deleted_by_name`（读面解析），旧后端不回它 ⇒ 回落 `deleted_by` 这个登录账号快照。
    delUserName: escapeHtml(row.deleted_by_name ?? row.deleted_by),
  }
}

/** 把证据件里「游戏名称」下拉重建成**我方游戏**（首项「全部游戏」用空值 = 不发该参数）。 */
function fillGameSelect(root: HTMLElement, games: readonly ConfigRow[]): void {
  const select = root.querySelector<HTMLSelectElement>('select#gameName')
  if (!select) return
  select.innerHTML = ['<option value="">全部游戏</option>']
    .concat(games.map((game) => `<option value="${game.id}">${escapeHtml(game.name)}</option>`))
    .join('')
}

/**
 * 「订单状态」下拉：**按全站共享枚举重建**（值 = 枚举码，文案 = 共享 label）。
 *
 * ⚠️ 这不是「改对标标记」而是**接线必需**：证据件里 9 个 `<option>` 的 value 是中文
 * （`未分配`/`待上号`/…），而后端 `status` 是 `ChoiceField(OrderStatus.choices)`——送中文回 `1000`。
 * 集合取全站枚举（含对标没有的「待抢单」）⇒ 档位差登记 §9-R13；文案与全站同一词（业务方裁定）。
 */
function fillStatusSelect(root: HTMLElement): void {
  const select = root.querySelector<HTMLSelectElement>('select#orderSta')
  if (!select) return
  select.innerHTML = ['<option value="">全部</option>']
    .concat(
      ORDER_STATUS_VALUES.map(
        (status) => `<option value="${status}">${ORDER_STATUS_LABELS[status]}</option>`,
      ),
    )
    .join('')
}

/** 「订单来源」下拉：证据件里是空 `<select>`（对标运行期填）⇒ 用我方来源字典重建。 */
function fillSourceSelect(root: HTMLElement, sources: readonly OrderSourceRow[]): void {
  const select = root.querySelector<HTMLSelectElement>('select#orderSoures')
  if (!select) return
  select.innerHTML = sources
    .map((source) => `<option value="${source.id}">${escapeHtml(source.name)}</option>`)
    .join('')
}

/** 写回 4 个删除时间值（`#minD`/`#maxD` 是日期控件的上下界，`#sDate`/`#eDate` 是默认区间）。 */
function applyDefaultDateRange(root: HTMLElement): RecycleDateRange {
  const range = defaultRecycleDateRange()
  const setValue = (id: string, value: string): void => {
    const input = root.querySelector<HTMLInputElement>(`#${id}`)
    if (input) input.value = value
  }
  setValue('minD', range.minD)
  setValue('maxD', range.maxD)
  setValue('sDate', range.sDate)
  setValue('eDate', range.eDate)
  return range
}

export async function mountOrderRecyclePage(
  root: HTMLElement,
  _ctx: IslandContext,
): Promise<() => void> {
  // ⚠️ 三条陷阱之一：**先**挂样式（`bootstrapIsland` 会运行期插 `layui.css` 与覆盖样式），
  // **再**塞页面标记——顺序反了会 FOUC。
  const layui = await bootstrapIsland(['form', 'table', 'layer', 'laydate'])
  const form = layui.form as LayuiModule
  const table = layui.table as LayuiModule
  const layer = layui.layer as LayuiModule
  const laydate = layui.laydate as LayuiModule | undefined

  // 对标 `<title>` 是服务端渲染的（`Order__orderrecycle.html:5`）。
  document.title = RECYCLE_TITLE

  // 证据件与三份字典一起取（字典失败也照常渲染：下拉为空、区服显示空串）。
  const [evidence, games, servers, sources] = await Promise.all([
    fetch(`${import.meta.env.BASE_URL}${RECYCLE_ARTIFACT_URL}`).then(async (response) => {
      if (!response.ok) {
        throw new Error(`证据件取不到（HTTP ${response.status}）：${RECYCLE_ARTIFACT_URL}`)
      }
      return response.text()
    }),
    fetchGames().catch(() => [] as ConfigRow[]),
    fetchServers().catch(() => []),
    fetchOrderSources()
      .then((page) => page.items)
      .catch(() => [] as OrderSourceRow[]),
  ])
  root.innerHTML = evidence
  const serverNames = new Map(servers.map((server) => [server.id, server.name] as const))
  const serverName = (id: number | null): string => (id === null ? '' : (serverNames.get(id) ?? ''))

  fillGameSelect(root, games)
  fillStatusSelect(root)
  fillSourceSelect(root, sources)
  const range = applyDefaultDateRange(root)
  // 对标的 formselects-v4 由 `bootstrapIsland()` 的 `ensureBenchmarkVendor()` 注入；
  // 必须在**选项灌好之后**渲染，否则它渲染的是空下拉（先例 `order-orderlist.ts` 同处置）。
  initFormSelects('select[xm-select]')
  // ⚠️ 三条陷阱之三：`initIslandElements(layui)` 相反**必须**留在塞标记**之后**。
  initIslandElements(layui)

  // 日期控件（对标 `orderrecycle.js:20-38`：两次 `laydate.render`，`min`/`max` 取 `#minD`/`#maxD`）。
  const renderDate = (elem: string): void => {
    const render = laydate?.render as ((options: unknown) => void) | undefined
    if (render) {
      render({ elem, type: 'datetime', min: `${range.minD} 00:00:00`, max: `${range.maxD} 23:59:59` })
    }
  }
  renderDate('#sDate')
  renderDate('#eDate')

  const msg = (text: string): void => islandMsg(layer, text)
  const errText = (error: unknown): string =>
    error instanceof Error ? error.message : '操作失败'

  /**
   * 当前页码与最近一发响应里的服务端 `total`。
   *
   * ⚠️ 页码的**真出处是 layui 交回 `done` 的 `curr`**（用户点页码 / 改「条/页」后 layui 会带着
   * 真实页码重取数，见 render 配置里的 `done`）；`reload()` 自己也写它。它**不在** `where` 里
   * （`where` 里放 `page` 会盖掉 layui 自己那份，翻页永远发 `page=1`）。
   *
   * `lastTotal` 由 `parseData` 写、`done` 读——layui 只认 `{code,msg,count,data}` 四键，
   * 「服务端 total」塞不进返回值给 `done`（同 `order-orderlist.ts` 的 `lastSummary` 手法）。
   */
  let currentPage = 1
  let lastTotal = 0

  /** 用户当前选的「条/页」（laypage 就地写在 `table.render` 那份配置的 `page` 子对象上）。 */
  const currentLimit = (): number => {
    const options = (
      table.getOptions as (id: string) => { page?: { limit?: number } } | undefined
    )(RECYCLE_TABLE_ID)
    return options?.page?.limit ?? RECYCLE_PAGE_LIMIT
  }

  /** 工具栏里的统计容器（layui 渲染 `#toolbarDemo` 之后才存在；缺了就什么都不做）。 */
  const writeTotal = (count: number): void => {
    const total = root.querySelector<HTMLElement>('#total')
    if (total) total.innerHTML = RECYCLE_TOTAL_TEMPLATE(count)
  }

  /** 「订单来源」的当前值：优先问 formSelects（它才是选中态的真出处），退化读底层 `<select>`。 */
  const sourceValue = (): string => {
    const picked = getFormSelects()?.value('orderSoures', 'valStr')
    if (typeof picked === 'string' && picked !== '') return picked
    return root.querySelector<HTMLSelectElement>('select[name="orderSoures"]')?.value ?? ''
  }

  /** 读筛选栏（键名 = 证据件里的 `name`；`minD`/`maxD` 不是筛选条件，不读）。 */
  const readFilters = (): RawRecycleFilterValues => {
    const value = (name: string): string =>
      root.querySelector<HTMLInputElement | HTMLSelectElement>(`[name="${name}"]`)?.value ?? ''
    return {
      gameName: value('gameName'),
      orderSta: value('orderSta'),
      sDate: value('sDate'),
      eDate: value('eDate'),
      orderSoures: sourceValue(),
      key1: value('key1'),
      gameAcc: value('gameAcc'),
    }
  }

  /** 勾选中的行（工具栏「批量还原」判笔数用它）。 */
  const checkedRows = (): RecycleCellRow[] =>
    ((table.checkStatus as (id: string) => { data?: RecycleCellRow[] })(RECYCLE_TABLE_ID).data ??
      []) as RecycleCellRow[]

  /**
   * 把当前筛选交给 layui 重新取数（`url` 模式：取数与分页都由 layui 自己的通道发）。
   *
   * ⚠️ **`where` 里绝不能放 `page`/`page_size`**：它会被 `table.reload` 记进实例配置，而
   * layui 自己维护的 `page.curr` 会被它盖掉 ⇒ 「翻到第 2 页」永远发 `page=1`（同
   * `order-orderlist.ts` 的 `collectSearch` 注释）。页码 / 页长的唯一出口是
   * `table.render` 的 `request:{pageName,limitName}` + 这里的 `page:{curr, limit}`。
   *
   * ⚠️ **每页条数取 layui 维护的实时值**（`currentLimit()`），不要退回常量 20：用户改过
   * 「条/页」后任何一次重新取数都会把选择顶掉（E-0037 家族，订单列表同款注释）。
   */
  const reload = (page = 1): void => {
    currentPage = page
    ;(table.reload as (id: string, options: unknown) => void)(RECYCLE_TABLE_ID, {
      where: toRecycleSearch(readFilters()) as Record<string, unknown>,
      page: { curr: page, limit: currentLimit() },
    })
  }

  /**
   * 还原一笔（页面约定**一次一笔**）。成功后就地重取（对标 `orderrecycle.js:253-275`
   * 也是在成功回调里 `reloadOrderList()`）。
   *
   * ⚠️ **成功不弹提示**：对标的提示文案来自它服务端的 `msg`，而 `request<T>()` 成功只回 `data`
   * （信封里的 `msg` 被 `unwrap` 丢掉）⇒ 自造一句就是「编文案」。反馈 = 该行从列表里消失。
   */
  async function doRestore(orderNo: string): Promise<void> {
    try {
      await restoreOrder(orderNo)
    } catch (error) {
      msg(errText(error))
      return
    }
    reload(currentPage)
  }

  // 主表：**先 `table.render` 一次**（`reload` 按实例 id 查表，不先 render 会静默 no-op）。
  ;(table.render as (options: unknown) => void)({
    elem: RECYCLE_TABLE_ELEM,
    id: RECYCLE_TABLE_ID,
    // `url` 模式：取数请求由 layui 自己发（分页条因此按**服务端总数**画，⛔ 不再是本地数据模式
    // 下那个「本页行数」）。`request` 把 layui 的 `page`/`limit` 映射成我们后端的
    // `page`/`page_size`（逐字取 `RecycleBinSearchSerializer`）。
    url: RECYCLE_API_URL,
    method: 'get',
    request: { pageName: 'page', limitName: 'page_size' },
    // 首屏就带默认筛选（删除时间区间在 mount 时已写进 `#sDate`/`#eDate`）——⛔ 不像订单列表那样
    // 在 `done` 里补一次 `reload(1)`：那会先用**无筛选**的结果画一帧，而对标首屏的请求就带
    // seachParam（spec §2.1.9 实测）。
    where: toRecycleSearch(readFilters()) as Record<string, unknown>,
    // 统一信封 `{code,msg,data:{items,total,…}}` → layui 认的 `{code,msg,count,data}`。
    // `total` 给两处用：`count`（分页条的总数）与**走闭包**带给 `done` 的 `lastTotal`（统计文案
    // `#total` 要的值塞在返回值里 `done` 也拿不到——读码 `public/layui/layui.js` 偏移 ~292034
    // `o.done(e, i, e[r.countName], t)`，`e` 就是这里的返回值）。
    parseData: (r: {
      code: number
      msg: string
      data?: { items?: RecycleBinRow[]; total?: number }
    }) => {
      lastTotal = r.data?.total ?? 0
      return {
        code: r.code,
        msg: r.msg,
        count: r.data?.total ?? 0,
        // ⚠️ **证据列（`buildRecycleCols`）没有 `templet`、渲染列（`buildRecycleRenderCols`）有**
        // ⇒ 单元格直接取 `field` 对应的键，所以这里必须把接口行映射成 `buildRecycleRow` 的
        // 单元格行；不映射的话所有单元格恒空（`url` 模式把 items 原样交给表格，不像原来的
        // 本地数据模式先映射过）。
        data: (r.data?.items ?? []).map((row) => buildRecycleRow(row, serverName)),
      }
    },
    toolbar: RECYCLE_TOOLBAR_ELEM,
    // 对标在同一个 `render` 对象里写了两次 `defaultToolbar`（L55 `[]` 在前、**L57 `['filter']` 生效**）。
    defaultToolbar: ['filter'],
    page: { limit: RECYCLE_PAGE_LIMIT, limits: [...RECYCLE_PAGE_LIMITS] },
    // ⛔ **`cols` 必须是「数组的数组」**（layui 的契约）：这里原先直接传 `buildRecycleCols()`
    //    （扁平数组）⇒ layui 解析列时把首列当成「一行」处理，抛
    //    `Cannot create property 'key' on string 'checkbox'` ⇒ **整页白屏**（`#app` 里只剩这句错）。
    //    同目录的正常页都是 `cols: [COLS]`（`customer-backlist.ts:58`）或 `cols: [[…]]`（`order-orderlist.ts:5050`）。
    //    ⚠️ 修**调用点**、不动 `buildRecycleCols()`：它的单测钉的就是「返回扁平数组」
    //    （`__tests__/order-orderrecycle.spec.ts:376,393`）⇒ 改函数会连带打红那些单测。
    //    发现者 = E2E 线（`feat/e2e-golden-path`）的全站冒烟：真浏览器里这一页 `#app` 只有上面那句错，
    //    而 vitest/jsdom 单测**不跑 layui 的真实渲染** ⇒ 抓不到。
    //    ⚠️ 用 `buildRecycleRenderCols()`（= `buildRecycleCols()` + 原样
    //    `templet`）：证据列没有 `templet`、被 layui 的 `escape:!0` 当纯文本转义 ⇒ 单元格里
    //    显示 HTML 源码（见 `buildRecycleRenderCols()` 的 docstring）。
    cols: [buildRecycleRenderCols()],
    // ⛔ 这里**不再有**本地 `data: []`：带 `data` 会让 layui 走本地数据分支，分页条的 `count`
    // 就变成本页行数（`layui.js` 偏移 ~292215 的 `o.url ? … : o.data.length`）——那正是本修复的缺口。
    text: { none: '暂无数据' },
    // `done` 是 layui **每次取数后**的出口（首帧 / 翻页 / 改「条/页」都算），`curr` 就是刚加载的
    // 页码。统计文案用**服务端 total**（`parseData` 写的 `lastTotal`），⛔ 不用 `count` 参数
    // 去别处再取一遍（同一事实两处来源会漂）。
    done: (_res: unknown, curr?: number) => {
      writeTotal(lastTotal)
      ;(form.render as () => void)()
      if (typeof curr === 'number') currentPage = curr
    },
  })

  /* ── 筛选栏 ── */
  ;(form.on as (event: string, callback: (data: unknown) => unknown) => void)(
    'submit(seachBtn)',
    () => {
      reload(1)
      return false
    },
  )
  // 对标 `form.on('select(selectGame)')`（`orderrecycle.js:188-191`）：换游戏即重搜第 1 页。
  ;(form.on as (event: string, callback: (data: unknown) => unknown) => void)(
    'select(selectGame)',
    () => {
      reload(1)
    },
  )

  /* ── 表格事件（工具栏 / 行内） ── */
  ;(table.on as (event: string, callback: (obj: unknown) => void) => void)(
    `toolbar(${RECYCLE_TABLE_ID})`,
    (obj: unknown) => {
      const event = (obj as { event?: string }).event
      if (event === 'refresh') {
        reload(currentPage)
        return
      }
      if (event !== 'refOrder') return
      const rows = checkedRows()
      if (rows.length === 0) {
        msg(RECYCLE_ROW_SELECT_TEXT)
        return
      }
      // 对标那条是逗号串批量（`orderrecycle.js:199-211`）；我方页面约定**一次一笔**（spec §5.5-#1）
      // ⇒ 多勾只提示、**零请求**。
      if (rows.length > 1) {
        msg(RECYCLE_BATCH_NOT_OPEN_TEXT)
        return
      }
      void doRestore(rows[0]!.order_no)
    },
  )
  ;(table.on as (event: string, callback: (obj: unknown) => void) => void)(
    `tool(${RECYCLE_TABLE_ID})`,
    (obj: unknown) => {
      const typed = obj as { event?: string; data?: { order_no?: string } }
      if (typed.event !== 'refOrder') return
      const orderNo = typed.data?.order_no
      if (typeof orderNo === 'string' && orderNo !== '') void doRestore(orderNo)
    },
  )

  /* ── 页内点击的显式收口 ──
   *
   * 自建页脱离了 `artifactPage` 的「点任何按钮都提示尚未开放」总闸（D9 代价②）⇒ 每个可点元素
   * 必须在这里有一条**明确去向**。三类：
   *  1. 第 2 列整格（`.orderLine`：订单号链接、`急` / 标记徽标都在里面）⇒ 固定文案、**不跳转**
   *     （已删单详情必然 `1002`）。⚠️ 对标只有 `.orderdetails` 那条链接有 `title`、徽标与空白点了
   *     什么都不发生 ⇒ 我方把**整格**收口到同一句，不留「点了没反应」；
   *  2. `.setdlsta`（状态徽标）/ `.orderRemark`（重要备注）⇒ 「尚未开放」
   *     （已删单上不做写动作；`order.edit_important_note` 今天 `available=False`）；
   *  3. 其余（工具栏 / 行内 `[lay-event]`）交给 layui 的委派与上面两个 `table.on` —— 不拦。
   */
  const onRootClick = (event: Event): void => {
    const target = event.target as HTMLElement | null
    if (!target?.closest) return
    if (target.closest('.orderLine')) {
      event.preventDefault()
      msg(RECYCLE_DETAIL_NOT_OPEN_TEXT)
      return
    }
    if (target.closest('.setdlsta') || target.closest('.orderRemark')) {
      event.preventDefault()
      msg(RECYCLE_STATUS_NOT_OPEN_TEXT)
    }
  }
  root.addEventListener('click', onRootClick)

  // 回车 = 搜索（对标 `orderrecycle.js:39-44` 的 `$(document).keydown`，重置到第 1 页）。
  const onKeydown = (event: KeyboardEvent): void => {
    if (event.key !== 'Enter') return
    if ((event.target as HTMLElement | null)?.tagName !== 'INPUT') return
    event.preventDefault()
    reload(1)
  }
  root.addEventListener('keydown', onKeydown)

  // ⛔ 这里**没有**首屏那发 `load()`：`table.render` 自带 `url` + `where`，layui 渲染时就会取数
  //    （本地数据模式下才需要自己先灌一次）。

  return () => {
    root.removeEventListener('click', onRootClick)
    root.removeEventListener('keydown', onKeydown)
    root.innerHTML = ''
  }
}

export function mount(root: HTMLElement, ctx: IslandContext): Promise<() => void> {
  return mountOrderRecyclePage(root, ctx)
}

export default { mount } satisfies IslandPage
