/**
 * 【上家订单改价记录】页的 **layui island 页 + 接真后端**（对标物 `/Order/SoureOrderChangePriceLog`）。
 *
 * 形状：**单表列表页**（3 个筛选控件 + 工具栏 1 颗按钮 + 9 列表格）。表格带 `height:'full-110'`。
 *
 * 证据（全部离线，**视觉值唯一出处**）：
 *  - 标记与页内 `<style>` → `public/replica/order-soureorderchangepricelog.html`
 *    （源 `.tools/recon/suqing1/html/Order__SoureOrderChangePriceLog.html` L24-116）
 *  - 列定义 → `.tools/recon/suqing1/assets/JS2019/Order/SoureOrderChangePriceLog.js` **L39-86**
 *  - 工具栏模板 `#toolbarDemo` → 同上 HTML **L108-113**（按钮文案逐字「刷新列表」，`#total` 在里面）
 *  - 分页 → 同 JS **L87-89**：`page: true` / `limit: 20` / `limits: [20,30,40,50,100]`
 *  - `height` → 同 JS **L34**：`'full-110'`
 *  - `#total` 文案模板 → 同 JS **L92**：`'本次搜索统计共' + count + '条记录'`
 *
 * ## `Soure` 少一个 `c` 是全域拼写，照抄不修
 *
 * 路由是 `/Order/SoureOrderChangePriceLog`、页面 JS 是 `SoureOrderChangePriceLog.js`、
 * 端点是 `/OrderAjax/GetSoureOrderChangePriceLogList`（JS L35）——**全部照抄**。island key
 * （`config/replicaRoutes.ts`）也是 `order-soureorderchangepricelog`。
 *
 * ## 为什么不再用 `artifactPage`（2026-10-17 线，spec §D4）
 *
 * `artifactPage` 的契约是「表格恒空、不发任何请求」，而本线要把这一页接上真后端：
 * 取数走 layui 的 `url` 模式、筛选要重取、订单号要开详情层、工具栏「刷新列表」要真的刷新。
 * 照兄弟页 `order-tradingorderactionloglist.ts` 的做法**自挂载**：`bootstrapIsland` →
 * `loadConfigArtifact` → `root.innerHTML` → `initIslandElements` → 自己 `table.render`。
 * 代价：artifactPage 的「点任何按钮都提示尚未开放」兜底没有了 ⇒ 本页显式接管**两个**可点去向
 * （工具栏刷新 / 订单号详情），其余没有可点元素。证据件里的标记**一字未动**。
 *
 * ## `d1` 字段被两列复用（与对标一致，不是笔误）
 *
 * JS **L57-62** 的「金额变动」是 `field:'d1'`，**L69-76** 的「变动金额」**又**是 `field:'d1'`
 * （「本次金额」是 `field:'d2'`）。两列渲染的 `templet` 不同（前者 `d1>>d2`，后者算增减量），
 * 但 `field` 同名是**对标原样**，别「修好」成 `d3`——既有单测逐字钉住这个复刻值。
 *
 * ## 行映射（后端 9 键 → 证据件字段）
 *
 * `orderId ← order_no` / `str2 ← game_name` / `str3 ← game_role_name` /
 * `d1 ← before_amount` / `d2 ← after_amount` / `str5 ← note` /
 * `str4 ← formatCreatedAt(occurred_at)` / `str1 ← ''`（我方不用内部 id）/
 * `str6 ← is_deleted ? 'Y' : ''`（复刻「删」徽标的判据）。
 *
 * ## 金额：一律 `formatMoney`（两位小数，空 ⇒ `—`）
 *
 * 后端的 `before_amount` / `after_amount` 是**十进制字符串或 `null`**（spec §4 的 D3）：
 * - 「金额变动」显示 `{d1}>>{d2}`，任一为空 ⇒ 该侧显示 `—`；
 * - 「本次金额」显示 `{d2}`；
 * - 「变动金额」：`d2>d1 ⇒ +差额`、`d2<d1 ⇒ -差额`、相等 ⇒ `0`；任一为空 ⇒ `—`。
 * ⛔ 不在前端做「按百分比改价」之类的换算——这一页只读后端留痕。
 *
 * ## 筛选（**筛选口径**上的唯一有意差异；渲染方式/取数的差异见上面两段）
 *
 * 证据件里 `#sDate`/`#eDate` 的 `value` 是服务端按快照日渲染的死值（`2026-03-20` / `2026-09-20`，
 * 半年窗口）。那两个值**真的发给后端**当筛选 ⇒ 照抄会让默认视图永远停在过去。
 * 本页运行期算：**今天 − 6 个自然月 ~ 今天**（`@/layui-island/dateRange::dayRangeMonthsAgo(6)`，
 * 只用机制、不另写一份日期算式）。`lay-verify="date"` 与空搜索框照证据件保留。
 *
 * ## PII
 *
 * 无。只有静态中文文案、服务端返回的订单/游戏/角色/说明文本与空搜索框；测试夹具全是合成值。
 */
import { formatCreatedAt } from '@/api/auditLogs'
import { formatMoney } from '@/config/money'
import { loadConfigArtifact } from '@/layui-island/config-replica'
import { dayRangeMonthsAgo, type DateRange } from '@/layui-island/dateRange'
import { escapeHtml } from '@/layui-island/escape'
import {
  bootstrapIsland,
  initIslandElements,
  type IslandContext,
  type IslandPage,
} from '@/layui/island'
import type { LayuiModule } from '@/layui/loader'

/** 证据件名（= `public/replica/<key>.html`；也是 island key）。 */
export const ARTIFACT_KEY = 'order-soureorderchangepricelog'

/**
 * 站点基址（Vite 保证以 `/` 结尾；⛔ 不写死 `/`——子路径部署时详情层会指到站外）。
 * 与 `order-orderrecycle.ts` / `order-orderlist.ts` 同一写法。
 */
const BASE_URL = import.meta.env.BASE_URL

/** 后端只读端点（spec §D3；`apps/business/urls.py` 的显式 `path()`）。 */
export const UPSTREAM_PRICE_CHANGE_LOGS_PATH = '/api/business/upstream-price-change-logs/'

/**
 * 交给 layui `table.render` 的**完整 URL**（`url` 模式下取数由 layui 自己发）。
 * ⚠️ 用 `BASE_URL + 'api/…'`（不带前导 `/`）拼，见上面 `BASE_URL` 的注释。
 */
export const UPSTREAM_PRICE_CHANGE_LOGS_URL = `${BASE_URL}api/business/upstream-price-change-logs/`

/** 对标页服务端渲染的 `<title>`（`Order__SoureOrderChangePriceLog.html` L5）。 */
export const PAGE_TITLE = '上家订单改价记录'

/** 对标 `table.render` 的 `elem`（`SoureOrderChangePriceLog.js` L33；HTML L107）。 */
export const TABLE_ELEM = '#tableList'

/**
 * `table.reload` / `table.on` 要的是**实例 id**（不带 `#`），传 `#id` 是静默 no-op
 * （E-0061 ①）——所以选择器与 id 分成两个常量。
 */
export const TABLE_ID = 'tableList'

/** `table.render` 的 `lay-filter`（`table.on('toolbar(tableList)')` 的事件名）。 */
export const TABLE_FILTER = 'tableList'

/** 对标 `toolbar`（同 JS L37；模板在 HTML L108-113）。 */
export const TOOLBAR_ELEM = '#toolbarDemo'

/** 对标 `height`（同 JS L34）。 */
export const TABLE_HEIGHT = 'full-110'

/** 对标 L88-89：`limit: 20` / `limits: [20,30,40,50,100]`。 */
export const PAGE_LIMIT = 20
export const PAGE_LIMITS = [20, 30, 40, 50, 100] as const

/** `#total` 文案模板（**逐字**取 `SoureOrderChangePriceLog.js` L92；`N` = 服务端 total）。 */
export const TOTAL_TEMPLATE = (count: number): string => `本次搜索统计共${count}条记录`

/** 打开详情的单号形状（与后端 `Order.order_no` 的 `lookup_value_regex = r"\d{20}"` 同源）。 */
const ORDER_NO_RE = /^\d{20}$/

/** 形状闸不通过时的固定文案（⛔ 不把输入拼进提示）。 */
export const ORDER_NO_SHAPE_TEXT = '订单号格式不正确，无法打开订单详情'

/** 后端 `GET /api/business/upstream-price-change-logs/` 的一行（**恰好 9 个键**，spec §4）。 */
export type UpstreamPriceChangeLogRow = {
  order_no: string
  /** ⭐ 来源单号（D-0301，2026-10-24）：实时回查 `Order`；订单查不到 ⇒ `null`。 */
  source_order_no: string | null
  game_name: string
  game_role_name: string
  /** 十进制字符串（如 `"12.00"`），可 `null`。 */
  before_amount: string | null
  /** 十进制字符串（如 `"12.00"`），可 `null`。 */
  after_amount: string | null
  source_name: string
  note: string
  /** ISO-8601 带时区；展示前过 `formatCreatedAt`。 */
  occurred_at: string
  /** 该 `order_no` 的订单**当前**是否已被软删（读时回查，spec §6 残差 6）。 */
  is_deleted: boolean
}

/**
 * 表格行：键 = 证据件列定义里的 `field`（**保留 `d1` / `d2` 这些复刻字段名**）。
 *
 * `d1` / `d2` 存**后端原样的十进制字符串**（空串 = `null`），格式化推迟到列的 `templet`：
 * 同一个 `d1` 被「金额变动」与「变动金额」两列用不同的算式读，提前格式化会让后者算错。
 */
export type PriceLogRow = {
  orderId: string
  /** ⭐ 展示单号（D-0301）：`source_order_no || order_no`（回退在映射时算好）。 */
  orderIdView: string
  str2: string
  str3: string
  d1: string
  d2: string
  str5: string
  str4: string
  /** 我方不用内部 id（详情链接走 `orderId`）——照 spec §D4 留空串。 */
  str1: string
  /** `is_deleted ? 'Y' : ''`（「删」徽标的判据，复刻证据件的 `str6 == 'Y'`）。 */
  str6: 'Y' | ''
}

/** 接口行 → 表格行（9 键逐个映射；缺失的可选文本兜空串，⛔ 不把 `undefined` 拼进页面）。 */
export function toPriceLogRow(item: UpstreamPriceChangeLogRow): PriceLogRow {
  return {
    orderId: item.order_no ?? '',
    // ⭐ D-0301：展示值 = 来源号非空取它、空回退内置号；`orderId` 本身
    // 是寻址键（详情开层的形状闸 `/^\d{20}$/` 只认它），⛔ 不跟着换。
    orderIdView: item.source_order_no || (item.order_no ?? ''),
    str2: item.game_name ?? '',
    str3: item.game_role_name ?? '',
    // `null` = 没有改前/改后价（首次从「无价」变有价）⇒ 空串，由 `formatMoney` 显示 `—`。
    d1: item.before_amount ?? '',
    d2: item.after_amount ?? '',
    str5: item.note ?? '',
    str4: formatCreatedAt(item.occurred_at),
    str1: '',
    str6: item.is_deleted ? 'Y' : '',
  }
}

/**
 * 「变动金额」的文案（spec §D4）：`d2 > d1 ⇒ +差额`、`d2 < d1 ⇒ -差额`、相等 ⇒ `0`。
 *
 * 差额取**绝对值**后过 `formatMoney`（固定两位小数）；任一金额为空（`''` / `null` /
 * 非数字）⇒ `—`。⛔ 符号只由比较结果决定，不让 `String(差额)` 自己带 `-`（否则会出现 `--2.00`）。
 */
export function formatDelta(before: string, after: string): string {
  if (before.trim() === '' || after.trim() === '') return '—'
  const from = Number(before)
  const to = Number(after)
  if (!Number.isFinite(from) || !Number.isFinite(to)) return '—'
  const diff = to - from
  if (diff > 0) return `+${formatMoney(diff)}`
  if (diff < 0) return `-${formatMoney(Math.abs(diff))}`
  return '0'
}

/** 「订单号」列：已删单前面加红色「删」徽标且**不可点**；正常单是蓝色链接（`data-order-no`）。
 *
 * ⭐ D-0301：**正文 = 展示单号**（`orderIdView = source_order_no || order_no`）；
 * `data-order-no` 是寻址键（点击开层按它取数 + `/^\d{20}$/` 形状闸），照旧内置号；
 * `data-order-id-view` 带展示值给开层的**标题**用（HTML 上下文再转义一次，见点击处的注释）。
 */
export function orderNoTemplet(d: PriceLogRow): string {
  const orderNo = escapeHtml(d.orderId)
  const viewNo = escapeHtml(d.orderIdView)
  if (d.str6 === 'Y') {
    return (
      '<span class="lableb" style="background-color:#FF534D" title="该订单已被删除了">删</span>' +
      viewNo
    )
  }
  return (
    `<a href="javascript:void(0);" class="orderdetails" data-order-no="${orderNo}"` +
    ` data-order-id-view="${viewNo}" ` +
    `style="color:#0094ff" title="点击查看订单详情">${viewNo}</a>`
  )
}

/** 「金额变动」列：`{d1}>>{d2}`，任一为空 ⇒ 该侧 `—`（`formatMoney` 的既有口径）。 */
export function amountChangeTemplet(d: PriceLogRow): string {
  const before = formatMoney(d.d1)
  const after = formatMoney(d.d2)
  return `<span style="color:#2f363c" title="从${before}元变成${after}元">${before}>>${after}</span>`
}

/** 「本次金额」列：`{d2}`，空 ⇒ `—`。 */
export function currentAmountTemplet(d: PriceLogRow): string {
  return `<span style="color:#FF5722">${formatMoney(d.d2)}</span>`
}

/** 「变动金额」列：见 `formatDelta`。 */
export function deltaTemplet(d: PriceLogRow): string {
  return `<span style="color:#FF5722">${formatDelta(d.d1, d.d2)}</span>`
}

/** 列的宽松类型（`templet` 是函数，其余是 layui 的键值）。 */
export type PriceCol = Record<string, unknown>

/**
 * 9 列（= 表头序列；第 1 列是 checkbox）——标题/宽度/对齐**逐条**照 `SoureOrderChangePriceLog.js`
 * **L39-86**。`d1` 被「金额变动」（L57-62）与「变动金额」（L69-76）**两列复用**，见文件头。
 */
export function buildCols(): PriceCol[] {
  return [
    { type: 'checkbox', fixed: 'left', width: 30 },
    // `templet`（L42-49）：未删 ⇒ 蓝链；已删 ⇒ 前置「删」徽标且不可点。
    { field: 'orderId', title: '订单号', width: 180, templet: orderNoTemplet },
    { field: 'str2', title: '游戏名称', width: 120, align: 'center' },
    { field: 'str3', title: '角色名', width: 150 },
    // 「金额变动」= `d1>>d2`（L58-62）。
    {
      field: 'd1',
      title: '金额变动',
      width: 120,
      align: 'center',
      templet: amountChangeTemplet,
    },
    // 「本次金额」是 **d2**（L64-68）。
    { field: 'd2', title: '本次金额', width: 100, align: 'center', templet: currentAmountTemplet },
    // ⚠️ 与「金额变动」列**同名 `d1`**——对标 L70 原样，别改成 d3（见文件头）。
    {
      field: 'd1',
      title: '变动金额',
      width: 100,
      align: 'center',
      templet: deltaTemplet,
    },
    { field: 'str5', title: '说明', width: 150, align: 'center' },
    { field: 'str4', title: '变动时间', width: 150, align: 'center' },
  ]
}

/** 导出给单测对拍表头序列（与 `SoureOrderChangePriceLog.js` 的 `cols` 一致）。 */
export const COLS_FOR_TEST: PriceCol[] = buildCols()

/**
 * `table.render` 里**不随每次取数变化**的那部分。
 *
 * 单测钉它、`mount` 里 `{...TABLE_OPTIONS, where, parseData, done}` 展开它 ⇒ 这几条
 * **静默出错**的键（`url`/`method`/`request`/`toolbar`/`defaultToolbar`/`page`/`height`）
 * 不是「只在测试里对」，就是线上那一份。
 */
export const TABLE_OPTIONS: Record<string, unknown> = {
  elem: TABLE_ELEM,
  // ⚠️ 服务端分页必须走 layui 的 `url`：`data` 模式只会在本地分页当前这一页的数组，
  // 「共 N 条 / 总页数」全错。
  url: UPSTREAM_PRICE_CHANGE_LOGS_URL,
  method: 'get',
  // layui 默认发 `page`/`limit`；后端分页器认的是 `page`/`page_size`（`EnvelopePagination`）。
  request: { pageName: 'page', limitName: 'page_size' },
  toolbar: TOOLBAR_ELEM,
  // 同对象里写了两次，后写的 `['filter']` 生效（L36 → L38）。
  defaultToolbar: ['filter'],
  page: { limit: PAGE_LIMIT, limits: [...PAGE_LIMITS] },
  height: TABLE_HEIGHT,
  cols: [buildCols()],
  // 沿用原 `artifactPage` 渲染器给空表写的文案（本页换渲染器时**不该变**的可见值）。
  text: { none: '暂无数据' },
}

/** 筛选栏里那 3 个控件的值（**DOM 的 name/id**：`sDate` / `eDate` / `key1`）。 */
export type PriceLogFilterValues = {
  sDate?: string
  eDate?: string
  key1?: string
}

/** 后端只读端点接受的筛选参数（spec §D3）。 */
export type UpstreamPriceChangeSearchParams = {
  /** `yyyy-MM-dd`（本页日期框是 laydate `type:'date'`）。 */
  stime?: string
  /** `yyyy-MM-dd`；后端 `etime` **含当天**（`InclusiveEndDateTimeField`）。 */
  etime?: string
  /** 订单号模糊（后端 `order_no__icontains`）。 */
  key?: string
}

/** DOM 名 → 接口参数名（`sDate→stime`、`eDate→etime`、`key1→key`；只改名字）。 */
export function toUpstreamPriceChangeSearchParams(
  values: PriceLogFilterValues,
): UpstreamPriceChangeSearchParams {
  return {
    stime: (values.sDate ?? '').trim(),
    etime: (values.eDate ?? '').trim(),
    key: (values.key1 ?? '').trim(),
  }
}

/**
 * 组装**线上查询参数**（唯一形状出口）：空值 / 空串一律不发。
 *
 * ⚠️ 判据是「空」而不是真值（E-0021）：本页三个参数都是字符串，但保留这条口径与
 * `toOrderActionLogParams` 同形，将来加数字参数时不会静默丢掉 `0`。
 */
export function toUpstreamPriceChangeParams(
  params: UpstreamPriceChangeSearchParams,
): Record<string, string | number> {
  const out: Record<string, string | number> = {}
  if (params.stime !== undefined && params.stime !== '') out.stime = params.stime
  if (params.etime !== undefined && params.etime !== '') out.etime = params.etime
  if (params.key !== undefined && params.key !== '') out.key = params.key
  return out
}

/**
 * 「记录日期」的默认区间：**今天 − 6 个自然月 ~ 今天**（`yyyy-MM-dd`，本地时区）。
 *
 * 证据件 HTML 的 `value` 是快照日渲染的 `2026-03-20` / `2026-09-20`（半年窗口）；
 * 那是**真的发给后端**的筛选 ⇒ 必须运行期算，否则默认视图永远停在快照日之前。
 * 算式与机制在 `@/layui-island/dateRange::dayRangeMonthsAgo`，这里只传偏移 6。
 */
export function defaultPriceLogDateRange(now: Date = new Date()): DateRange {
  return dayRangeMonthsAgo(6, now)
}

/** 后端响应的信封形状（`parseData` 吃的那一份；统一信封 `{code,msg,data}`）。 */
export type UpstreamPriceChangeEnvelope = {
  code: number
  msg: string
  data?: {
    items?: UpstreamPriceChangeLogRow[]
    total?: number
    page?: number
    page_size?: number
  }
}

/** 挂载整页；返回卸载函数。 */
export async function mountUpstreamPriceChangeLogPage(
  root: HTMLElement,
  _ctx: IslandContext,
): Promise<() => void> {
  // ⚠️ 挂载顺序（E-0067）：`bootstrapIsland` 挂样式 → 取证据件 → `root.innerHTML` →
  // `initIslandElements`（它绑的是标记里的页签），顺序反了会 FOUC / 绑定落空。
  const layui = await bootstrapIsland(['form', 'table', 'layer', 'laydate'])
  const form = layui.form as LayuiModule
  const table = layui.table as LayuiModule
  const layer = layui.layer as LayuiModule
  const laydate = layui.laydate as LayuiModule

  document.title = PAGE_TITLE
  root.innerHTML = await loadConfigArtifact(ARTIFACT_KEY)
  initIslandElements(layui)

  const msg = (text: string): void => {
    ;(layer.msg as (t: string, o?: unknown) => void)(text, { time: 1500 })
  }

  // —— 记录日期：填默认区间（今天 − 6 个自然月 ~ 今天）+ 交给 laydate。
  //    证据件的框是 laydate `type:'date'`（JS L24-31）⇒ 值是 `yyyy-MM-dd`（⛔ 不塞时分秒）。
  const { sDate, eDate } = defaultPriceLogDateRange()
  const sInput = root.querySelector<HTMLInputElement>('input#sDate')
  const eInput = root.querySelector<HTMLInputElement>('input#eDate')
  if (sInput) sInput.value = sDate
  if (eInput) eInput.value = eDate
  ;(laydate.render as (o: unknown) => void)({ elem: '#sDate', type: 'date' })
  ;(laydate.render as (o: unknown) => void)({ elem: '#eDate', type: 'date' })

  /** 读筛选栏（DOM 名原样；证据件的 `<form>` 没有 `lay-filter`，所以直接读 DOM）。 */
  function readFilterValues(): PriceLogFilterValues {
    return {
      sDate: root.querySelector<HTMLInputElement>('input#sDate')?.value ?? '',
      eDate: root.querySelector<HTMLInputElement>('input#eDate')?.value ?? '',
      key1: root.querySelector<HTMLInputElement>('input#key1')?.value ?? '',
    }
  }

  /** 当前筛选对应的**线上参数**（唯一形状出口）。 */
  const currentWhere = (): Record<string, string | number> =>
    toUpstreamPriceChangeParams(toUpstreamPriceChangeSearchParams(readFilterValues()))

  /**
   * 最近一发响应里的服务端 `total`（`parseData` 写、`done` 读）。
   *
   * layui 只认 `{code,msg,count,data}` 四个键，「服务端 total」塞不进返回值给 `done`
   * ⇒ 走闭包（同 `order-orderrecycle.ts` 的 `lastTotal` 手法）。
   */
  let lastTotal = 0

  /** 重新取数。默认回到第 1 页（搜索 / 刷新都是「重看一遍」）。 */
  function reload(page = 1): void {
    ;(table.reload as (id: string, o: unknown) => void)(TABLE_ID, {
      where: currentWhere(),
      page: { curr: page },
    })
  }

  // —— 搜索：`form.on('submit(seachBtn)')`（对标 L152-156）。layui 的 form 模块对
  //    `*[lay-submit]` 做了文档级委托，所以这里不需要额外的点击监听。
  ;(form.on as (e: string, cb: () => void) => void)('submit(seachBtn)', () => {
    reload(1)
    return false
  })

  // —— 「回车 = 搜索」：只绑 `#key1`（自由文本）。⛔ 不绑两个日期框——laydate 面板自己把
  //    Enter 当「确定」（vendored `public/layui/layui.js`），再绑一次会与它抢同一个键。
  const key1Input = root.querySelector<HTMLInputElement>('input#key1')
  const onKey1Enter = (event: KeyboardEvent): void => {
    if (event.key !== 'Enter') return
    event.preventDefault()
    reload(1)
  }
  key1Input?.addEventListener('keydown', onKey1Enter)

  // —— 工具栏唯一的「刷新列表」（证据件 L108-113 的 `lay-event="refresh"`）⇒ 回第 1 页重取。
  ;(table.on as (e: string, cb: (obj: { event?: string }) => void) => void)(
    `toolbar(${TABLE_FILTER})`,
    (obj) => {
      if (obj.event === 'refresh') reload(1)
    },
  )

  // —— 主表：`url` 模式（服务端分页 / 筛选都由 layui 自己的通道发）。
  ;(table.render as (o: unknown) => void)({
    ...TABLE_OPTIONS,
    // 首屏就带上默认日期区间（否则会拉全表最新 20 条，与筛选栏显示的值不一致）。
    where: currentWhere(),
    parseData: (res: UpstreamPriceChangeEnvelope) => {
      // 赋值发生在 layui 读返回值**之前**（`done` 总在 `parseData` 之后被调）。
      lastTotal = res.data?.total ?? 0
      return {
        code: res.code,
        msg: res.msg,
        // 分页条拿到的 `count` 就是**服务端 total**（⛔ 不是本页那 20 行）。
        count: lastTotal,
        data: (res.data?.items ?? []).map(toPriceLogRow),
      }
    },
    done: () => {
      // 对标 L92-93：`'本次搜索统计共' + count + '条记录'` 写进 `#total`
      // （layui 渲染工具栏之后它才存在；缺了就什么都不做）。
      const totalEl = root.querySelector<HTMLElement>('#total')
      if (totalEl) totalEl.innerHTML = TOTAL_TEMPLATE(lastTotal)
      ;(form.render as (type?: string) => void)()
    },
  })
  ;(form.render as (type?: string) => void)()

  /**
   * 「订单号」点开**我们自己的**订单详情岛页（对标 `.orderdetails` → `layer.open`）。
   *
   * ⛔ **开层前的「单号形状闸」`/^\d{20}$/`**——`layer.open` 的 `title` 是 **HTML 上下文**
   * （vendored `layui.js` 把 `title` 直接拼进 `<div class="layui-layer-title">`），而 `dataset`
   * 会把 attribute 实体**解码回** `<`/`>` ⇒ templet 里那次 `escapeHtml` 在这条链上**不构成保护**。
   * 不匹配 ⇒ **不开层**，只给一句**固定文案**（⛔ 不把那份不可信输入拼进提示）。
   * 与 `order-tradingorderactionloglist.ts` / `order-orderlist.ts` 同一处置。
   */
  function openOrderDetailLayer(orderNo: string, viewNo: string): void {
    if (!ORDER_NO_RE.test(orderNo)) {
      msg(ORDER_NO_SHAPE_TEXT)
      return
    }
    ;(layer.open as (o: unknown) => void)({
      // `orderNo` 已过形状闸（**纯数字**），仍过一遍 `escapeHtml` 是因为这里的上下文是 HTML。
      // ⭐ D-0301：标题前缀用**展示单号**（`data-order-id-view`，templet 里转义过一次，
      // 但 dataset 会把实体解码回原文 ⇒ HTML 上下文的边界上再转一次才有效）。
      title: `${escapeHtml(viewNo !== '' ? viewNo : orderNo)}订单详情`,
      shade: [0.3, '#333'],
      shadeClose: true,
      type: 2,
      skin: 'layui-layer-lan',
      anim: 8,
      area: ['85%', '92%'],
      // 基址用 `BASE_URL`，⛔ 不写死 `/`。
      content:
        `${BASE_URL}layui-island/page.html?page=order-orderview` +
        `&order_no=${encodeURIComponent(orderNo)}`,
    })
  }

  // —— 行内唯一的可点元素（订单号链接）。已删单那一格**没有锚**（只有「删」徽标 + 纯文本），
  //    所以点不到、也不会走到这里。
  const onClick = (event: Event): void => {
    const target = event.target as HTMLElement
    const link = target.closest<HTMLElement>('a.orderdetails')
    if (link && root.contains(link)) {
      event.preventDefault()
      openOrderDetailLayer(
        link.dataset.orderNo ?? '',
        link.dataset.orderIdView ?? '',
      )
    }
  }
  root.addEventListener('click', onClick)

  return () => {
    key1Input?.removeEventListener('keydown', onKey1Enter)
    root.removeEventListener('click', onClick)
    // 只清我们注入的标记；`layer` 的弹层挂在 `document.body`，由 layui 自己收。
    root.innerHTML = ''
  }
}

export default {
  mount: mountUpstreamPriceChangeLogPage,
} satisfies IslandPage
