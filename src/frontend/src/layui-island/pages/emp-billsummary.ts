/**
 * 【结算记录】页（island key = `emp-billsummary`，路由 `/agents/settlement`，菜单「结算记录」）。
 *
 * ⭐ **D-0319整页重设计为「按天聚合」审查视图**（业务方三问三答拍板，
 * 计划 `docs/plans/2026-10-28-settlement-daily-view.md`）：
 *
 * - **一行 = 一天**（同日多结算对象合并）：结账日期 / 总单数 / 总结算金额 / 操作「查看明细」；
 *   数据来自新端点 `GET /api/business/settlements/daily-summary`（SQL 层聚合、按
 *   `settled_at` 的 **UTC+8** 日期归天、只聚合已结账行；`order_count` = distinct 订单数，
 *   一单多绩效行计 1；`total_amount` = Σ `settled_amount` 实结快照）。
 * - **点「查看明细」→ 当天明细**：复用 `fetchAllSettlements`（`/settlements` 按该天
 *   `date_from = date_to = 当天` + `settle_status=已结账` 取全），七列保持重设计前的形态
 *   （结算对象人员 / 预计结算金额 / 实际结算金额 / 关联订单 / 项目 / 结算时间 / 结算人）；
 *   分页在**客户端**做（layui 自己按 `limit` 切片，与旧右表同一做法）；订单号搜索框
 *   **精确匹配**（后端 `order__order_no | order__source_order_no` 双列 OR，D-0301）。
 * - **「导出记录」复用 `downloadPerformanceExport`**（`GET /api/business/performances/export`），
 *   带 `date_type: 'settled'` + 当天两端（⚠️ 旧版不带 `date_type`、导出端点默认按**创建时间**
 *   筛 ⇒ 与「当天结的账」对不上；重设计后明细视图锚定**结账日**，导出同一天口径）+
 *   硬编码 `settle_status=已结账`（沿用旧版 D-0090 照对标 `settingSta=Y` 的口径）。
 * - **下线**（业务方已确认，计划 §5）：左卡按对象汇总（`aggregateByUser`）、平铺明细主视图、
 *   选择游戏筛选、结算对象人员下拉（`/settlements/people` 前端不再调用；**端点保留**，
 *   是否下线挂账）。
 * - **路由 / 菜单 / 权限节点全部不变**：菜单可见 `finance.settlement_records`、读闸
 *   `finance.payroll`（后端与 `/settlements` 同一颗）、导出闸 `finance.export_performance`。
 *
 * ⭐ **D-0323：「总结算金额」列点击展开 → 按订单来源分解**（业务方拍板：
 * 「结算一列可以点击展开 展开后按照订单来源展示各自结算了多少笔 多少钱，然后再配置
 * 查看明细 点击后查看单个店铺全部明细」；来源行明细范围 = **当天 + 该来源**）：
 *
 * - 主表金额单元格可点（金额文字保留 + 展开指示，**自拟视觉**，见 `buildDailyCols`）；
 *   点击 toggle 展开 / 收起，展开区以**子行**形态插在该日行下方（复用同一张表、同一套
 *   4 列——来源名 / 笔数 / 金额 / 操作，与 island 表格风格天然一致，不碰分页）。
 * - **lazy 取数**：首次展开才拉 `GET /settlements/daily-summary/sources?date=…`；
 *   收起再展开用缓存；**换页 / 改筛选 / 刷新 ⇒ 全部收起并作废缓存**（`loadDaily` 收口）。
 *   取数中 / 失败在子行里有状态行，失败可点「重试」。
 * - 来源行「查看明细」= 复用明细卡与 `fetchAllSettlements`，参数
 *   `date_from = date_to = 该天` + `order_source = id`（未指定行传 `'none'`）；
 *   明细卡标题带来源上下文（`（来源：X）`，自拟格式）。天级「查看明细」、订单号搜索、
 *   导出**行为一字不动**（导出端点没有 `order_source` 参数 ⇒ 来源视图里导出的仍是**整天**，
 *   D-0323 登记）。
 *
 * ## 页面定性：对标复刻页 → **自建 island 页**
 *
 * 旧版是 `/Emp/billsummary` 的像素级复刻（证据件 `public/replica/emp-billsummary.html`）；
 * 重设计后没有对标物 ⇒ 照既有自建页（`agents-taker-wallet.ts`）的模式登记：
 * 标记由 `buildHtml()` 自建（不再取证据件）、视觉沿用 island 现有的
 * `layui-card` / `layui-form` / `table` 风格。
 *
 * ## 三条 island 陷阱（`AGENTS.md` §5-G；本页**没有定时器**，E-0198 不适用）
 *
 * ① 类名与选择器分成两个常量（`PANEL_CLASS` / `PANEL_SELECTOR` 等）；
 * ② 以 `(` 开头的语句一律前置分号；
 * ③ 顺序：`await bootstrapIsland(...)`（挂样式）→ `root.innerHTML = buildHtml()`（塞标记）
 *    → `initIslandElements(layui)`（必须在塞标记**之后**）。
 * ⚠️ 三条都有运行期判据（`__tests__/emp-billsummary.interaction.spec.ts`）。
 *
 * ## 主表分页在服务端、明细分页在客户端（有意，不是不一致）
 *
 * - 主表（按天列表）行数 = **天数**，随时间无界增长 ⇒ 走端点分页
 *   （`page` / `page_size` → `EnvelopePagination`），layui 的 `page:false` + 页面自己用
 *   `laypage` 画分页栏（`jump` 回调里按页重取）。⚠️ 取数走 `request()` 这一个出口：
 *   不把 `url` 交给 layui 自己发 XHR——那会绕开 `api/http.ts` 的错误分类，
 *   后端 403（无 `finance.payroll`）会变成「表格空了、什么都不说」。
 * - 明细表（某一天）行数有界（一天结的账），走 `fetchAllSettlements` 取全 + 客户端分页
 *   （layui 自己按 `page.limit` 切片）——与重设计前的右表同一做法。
 */
import { escapeHtml } from '@/layui-island/escape'
import { defaultDateRange as sharedDefaultDateRange } from '@/layui-island/dateRange'
import {
  bootstrapIsland,
  initIslandElements,
  islandMsg,
  type IslandContext,
  type IslandPage,
} from '@/layui/island'
import {
  fetchAllSettlements,
  fetchSettlementDailySources,
  fetchSettlementDailySummary,
  type SettlementDailyItem,
  type SettlementListItem,
  type SettlementSourceItem,
} from '@/api/settlements'
import { downloadPerformanceExport } from '@/api/performances'

/** island key（= 文件名 = 路由 `meta.islandPage`，三处必须逐字一致）。 */
export const PAGE_KEY = 'emp-billsummary'
export const TITLE = '结算记录'

/** 页面容器的**类名**（拼进标记用）与**选择器**（查节点用）——两者不能混（陷阱 ①）。 */
export const PANEL_CLASS = 'billsummaryPanel'
export const PANEL_SELECTOR = `.${PANEL_CLASS}`

/** 明细卡标题的类名 / 选择器（挂载时清空，点开某天后写「<日期>的结算明细」）。 */
export const DETAIL_TITLE_CLASS = 'detailTitle'
export const DETAIL_TITLE_SELECTOR = `.${DETAIL_TITLE_CLASS}`

/** 行内「查看明细」链接的类名 / 选择器（主表操作列）。 */
export const VIEW_DETAIL_CLASS = 'viewbilldetail'
export const VIEW_DETAIL_SELECTOR = `.${VIEW_DETAIL_CLASS}`

/**
 * 「总结算金额」单元格**展开开关**的类名 / 选择器（D-0323；天行专用——来源子行不挂）。
 * 金额文字保留（红字不动），开关指示（▾/▲）拼在同一格里。
 */
export const AMOUNT_TOGGLE_CLASS = 'amountToggle'
export const AMOUNT_TOGGLE_SELECTOR = `.${AMOUNT_TOGGLE_CLASS}`

/** 来源分解子行「查看明细」链接的类名 / 选择器（与天行的 `viewbilldetail` 分开两个常量）。 */
export const SOURCE_VIEW_CLASS = 'viewsrcdetail'
export const SOURCE_VIEW_SELECTOR = `.${SOURCE_VIEW_CLASS}`

/** 来源分解「失败重试」链接的类名 / 选择器（错误状态行的操作格）。 */
export const SOURCE_RETRY_CLASS = 'srcretry'
export const SOURCE_RETRY_SELECTOR = `.${SOURCE_RETRY_CLASS}`

/**
 * 来源为空（`source_id` / `source_name` 都是 `null`）时的显示文案（D-0323；后端契约
 * ——订单没配来源、或来源被删都落进这一档）。
 */
export const UNSPECIFIED_SOURCE_LABEL = '未指定'

/** 行内「关联订单」链接的类名 / 选择器（明细表第 4 列）。 */
export const ORDER_LINK_CLASS = 'orderdetails'
export const ORDER_LINK_SELECTOR = `.${ORDER_LINK_CLASS}`

/** 主表（按天列表）与明细表（当天明细）的 layui 实例 id / 元素选择器。 */
export const TABLE_ID = 'tableList'
export const TABLE_ELEM = `#${TABLE_ID}`
export const TABLE2_ID = 'tableList2'
export const TABLE2_ELEM = `#${TABLE2_ID}`

/** 主表服务端分页的 laypage 容器 id / 元素选择器。 */
export const PAGER_ID = 'dailyPager'
export const PAGER_ELEM = `#${PAGER_ID}`

/** 主表分页条数（`EnvelopePagination` 默认 20）与档位。 */
export const DAILY_PAGE_SIZE = 20
export const DAILY_PAGE_LIMITS: readonly number[] = [20, 50, 100]

/** 明细表每页条数 / 档位（沿用重设计前右表的 L288-290 值）。 */
export const DETAIL_PAGE_LIMIT = 20
export const DETAIL_PAGE_LIMITS: readonly number[] = [20, 30, 40, 50, 100]

/**
 * 明细的日期形状闸（主表 `date` 来自后端 `YYYY-MM-DD`；`data-date` 经过 DOM 属性，
 * 形状不认识就不取数——它最终会拼进 `/settlements` 的 `date_from/date_to`）。
 */
export const DATE_SHAPE = /^\d{4}-\d{2}-\d{2}$/

/**
 * `layui.table` 的列对象（自建页不挂 `artifactPage`，列类型自己声明；
 * `templet` 是**函数**，返回值被 layui 当 HTML 插入 ⇒ 一律过 `escapeHtml`）。
 */
export type Col<T> = {
  field?: string
  title: string
  width?: number | string
  align?: string
  fixed?: string
  sort?: boolean
  totalRow?: boolean
  totalRowText?: string
  templet?: (row: T) => string
}

/**
 * 主表里的一行（D-0323 起是**三种形态的联合**，`templet` 按 `kind` 分派）：
 *
 * - `day`：天行（D-0319 原形态，字段没变，只是包了一层）；
 * - `source`：某天展开后的**来源子行**（一来源一行；紧跟其天行下方）；
 * - `source-loading` / `source-error`：该天来源分解的**状态行**（取数中 / 失败可重试）。
 */
export type DailyRow =
  | { kind: 'day'; day: SettlementDailyItem }
  | { kind: 'source'; date: string; item: SettlementSourceItem }
  | { kind: 'source-loading'; date: string }
  | { kind: 'source-error'; date: string }

/** 某一天的来源分解取数状态（展开区的缓存值；换页 / 筛选 / 刷新全部作废）。 */
export type SourcesState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ok'; items: SettlementSourceItem[] }

/**
 * 主表 4 列（D-0319 §2.2 的列清单；D-0323 起天行与来源子行**共用同一套列**——
 * 来源名落在「结账日期」列、笔数 / 金额 / 操作各归其列，与 island 表格风格一致）。
 *
 * ⚠️ **不开 `sort`**：主表是**服务端分页**，layui 的排序只排「当前页」，
 * 排出来的样子会骗人（看起来全局有序、其实只排了 20 天里的一页）。
 */
export const DAILY_COLS: Col<DailyRow>[] = [
  { field: 'date', title: '结账日期', align: 'center' },
  { field: 'order_count', title: '总单数', width: '20%', align: 'center' },
  { field: 'total_amount', title: '总结算金额', width: '20%', align: 'center' },
  { field: 'op', title: '操作', width: '20%', align: 'center' },
]

/** 明细表 7 列（列形态沿用重设计前：标题 / 宽度 / 对齐 / `totalRow` 逐列保持）。 */
export const DETAIL_COLS: Col<SettlementListItem>[] = [
  {
    field: 'userName',
    title: '结算对象人员',
    width: '12%',
    align: 'center',
    fixed: 'left',
    totalRowText: '合计',
  },
  { field: 'tempBonus', title: '预计结算金额', width: '10%', align: 'center', sort: true, totalRow: true },
  { field: 'realAmount', title: '实际结算金额', width: '10%', align: 'center', sort: true, totalRow: true },
  { field: 'orderId', title: '关联订单', width: '15%', align: 'center' },
  { field: 'project', title: '项目', align: 'left' },
  { field: 'billTime', title: '结算时间', width: '12%', align: 'center', sort: true },
  { field: 'billUserName', title: '结算人', width: 100, align: 'center' },
]

/** 主表 `table.render` 的参数（供测试断言）。 */
export const DAILY_TABLE = {
  elem: TABLE_ELEM,
  toolbar: '#toolbarDemo',
  defaultToolbar: ['filter'] as string[],
  // 服务端分页：layui 自己不画分页栏（分页栏由 laypage 管，见文件头）。
  page: false as const,
} as const

/** 明细表 `table.render` 的参数（`totalRow` / `limit` / `limits` 沿用重设计前右表的值）。 */
export const DETAIL_TABLE = {
  elem: TABLE2_ELEM,
  toolbar: '#toolbarDemo2',
  defaultToolbar: ['filter'] as string[],
  totalRow: true,
} as const

/* ───────────────────────────── 纯函数（可单独测） ───────────────────────────── */

/** 筛选栏的原始值（`sDate`/`eDate` 是 `yyyy-MM-dd HH:mm:ss` 或 `yyyy-MM-dd`）。 */
export type BillsummaryFilterValues = {
  sDate: string
  eDate: string
}

/**
 * 「结账时间」框 → 后端的 `date_from` / `date_to`（**只取日期段**，与重设计前同口径：
 * 后端是 `DateField` + `settled_at__date__gte/lte`，两端含）。形状不认识的输入
 * （用户手打了个半截）一律**不发**这个参数：让后端用它自己的判据，
 * 前端不发明「非法日期就当成某天」的规则。
 */
export function datePart(value: string): string | undefined {
  const m = /^(\d{4}-\d{2}-\d{2})/.exec(value.trim())
  return m?.[1]
}

/** 筛选栏 → 主表（`/settlements/daily-summary`）的查询参数（不含分页——分页 laypage 自己管）。 */
export function toDailyParams(values: BillsummaryFilterValues): Record<string, string> {
  const out: Record<string, string> = {}
  const from = datePart(values.sDate)
  const to = datePart(values.eDate)
  if (from !== undefined) out.date_from = from
  if (to !== undefined) out.date_to = to
  return out
}

/**
 * 明细取数参数（某一天 + 可选订单号 + 可选来源，D-0323 给第三参）。
 *
 * ⚠️ **必带 `settle_status: '已结账'`**：主表的聚合口径只算已结账行，明细不带它就会
 * 「汇总说 5 单、明细翻出 7 行」（未结账的行 `settled_at` 为空本来就进不了那一天的窗，
 * 但显式带上让口径**只有一处定义**——后端筛子，不是前端拼巧合）。
 * ⚠️ `orderSource` 只在**来源行**的「查看明细」传（天级路径一个字不动：不传 ⇒ 不带键）；
 * `'none'` 是字面量（后端两态参数的「未指定」档），⛔ 不要 `Number('none')`。
 */
export function toDetailParams(
  day: string,
  orderNo: string,
  orderSource?: number | 'none',
): Record<string, string | number> {
  const out: Record<string, string | number> = {
    settle_status: '已结账',
    date_from: day,
    date_to: day,
  }
  if (orderSource !== undefined) out.order_source = orderSource
  const no = orderNo.trim()
  if (no !== '') out.order_no = no
  return out
}

/** 来源子行的名字位：`null` ⇒ 「未指定」（后端契约；⛔ 不要渲染成空串冒充某家店）。 */
export function sourceDisplayName(item: SettlementSourceItem): string {
  return item.source_name ?? UNSPECIFIED_SOURCE_LABEL
}

/**
 * 来源子行的**取值唯一出口**（D-0323）：来源 id 的参数值——`null` ⇒ `'none'`
 * （后端两态参数的「未指定」档），有 id ⇒ 数字。天级路径不走这里。
 */
export function sourceParamValue(item: SettlementSourceItem): number | 'none' {
  return item.source_id ?? 'none'
}

/**
 * 天行数组 + 展开态 + 分解状态 → 主表要渲染的**行数组**（纯函数，D-0323）。
 *
 * 展开的天行下方依序插它的来源子行（后端已排好序：金额降序 → 未指定垫底 → 同额名称
 * 码点升序，**前端不重排**）；该天取数中 / 失败 ⇒ 插一条状态行（loading / error）。
 * 收起的天 ⇒ 只有天行自己。**未展开但缓存里有数据的天不渲染子行**（展开是唯一开关）。
 */
export function buildDisplayRows(
  days: readonly SettlementDailyItem[],
  expanded: ReadonlySet<string>,
  sources: ReadonlyMap<string, SourcesState>,
): DailyRow[] {
  const out: DailyRow[] = []
  for (const day of days) {
    out.push({ kind: 'day', day })
    if (!expanded.has(day.date)) continue
    const state = sources.get(day.date)
    if (state === undefined) {
      // 理论上到不了（toggle 展开 时无缓存必先落 loading）；保守渲染成取数中。
      out.push({ kind: 'source-loading', date: day.date })
      continue
    }
    if (state.status === 'ok') {
      for (const item of state.items) out.push({ kind: 'source', date: day.date, item })
    } else if (state.status === 'loading') {
      out.push({ kind: 'source-loading', date: day.date })
    } else {
      out.push({ kind: 'source-error', date: day.date })
    }
    // `ok` 且 `items` 为空（该天无已结账行——主表那行本就不会出现，防御形态）⇒ 0 条子行。
  }
  return out
}

/**
 * 两个日期框的**默认值**：`今天 − 3 个自然月 00:00:00` ~ `今天 23:59:59`。
 * 与重设计前同一条口径（出处与两路实测证据见 `@/layui-island/dateRange` 的实现注释；
 * 算式的唯一实现也在那里，这里是再导出）。
 */
export const defaultDateRange = sharedDefaultDateRange

/**
 * 「实际结算金额」这一列的**取值唯一出口**（明细列渲染用；语义与重设计前逐字相同）：
 * `settled_amount`（结账那一刻的实结净额快照）非 `null`/`undefined` ⇒ 用它；
 * 否则（未结账 / 存量已结账行——快照列上线前结的）⇒ 回落 `payable_net`。
 *
 * ⚠️ 判据是 `is not None` 语义（`??` 只对 `null`/`undefined` 回落）：`'0.00'` 是非空字符串
 * ⇒ 真值 ⇒ 原样取出（防「先 parse 成数字再判真值」把合法的 0.00 回落掉）。
 * ⛔ 这里不算钱：只做「取哪一个字段」的选择。
 */
export function settledOrPayable(
  row: Pick<SettlementListItem, 'settled_amount' | 'payable_net'>,
): string {
  return row.settled_amount ?? row.payable_net
}

/**
 * `settled_at`（后端 DRF `DateTimeField` 出的 ISO-8601 串）→ `YYYY-MM-DD HH:mm:ss`。
 *
 * ⚠️ **取后端渲染串里的墙钟时间，不按浏览器时区二次换算**：后端 `TIME_ZONE` 是
 * `Asia/Shanghai`，串里的钟点就是财务该看到的钟点；空态 / 坏形状 ⇒ 空串（不抛、不造值）。
 */
export function formatSettledAt(value: string | null | undefined): string {
  if (value === null || value === undefined) return ''
  const m = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})/.exec(String(value).trim())
  return m ? `${m[1]} ${m[2]}` : ''
}

/** 单元格插值的唯一出口：`escapeHtml(value ?? '')`（部署窗口里缺键渲染成空单元格，不让整表崩）。 */
function text(value: string | null | undefined): string {
  return escapeHtml(value ?? '')
}

/* ───────────────────────────── 标记（自建） ───────────────────────────── */

/**
 * 页面标记（自建页没有证据件义务；视觉沿用 island 的 `layui-card` / `layui-form` 风格）。
 *
 * ⚠️ 两个工具栏模板（`#toolbarDemo` 刷新 / `#toolbarDemo2` 导出）用 layui 的
 * `type="text/html"` script 模板——模板本身不可见，`table.render` 的 `toolbar` 按引用取。
 */
export function buildHtml(): string {
  return `
<div class="${PANEL_CLASS}" style="padding:10px 15px">
  <div class="layui-card">
    <div class="layui-card-header">${TITLE}（按天汇总）</div>
    <div class="layui-card-body">
      <div class="layui-form layui-form-pane">
        <div class="layui-inline">
          <label class="layui-form-label">结账时间</label>
          <div class="layui-input-inline">
            <input type="text" id="sDate" class="layui-input" placeholder="yyyy-MM-dd" autocomplete="off">
          </div>
          <div class="layui-form-mid">至</div>
          <div class="layui-input-inline">
            <input type="text" id="eDate" class="layui-input" placeholder="yyyy-MM-dd" autocomplete="off">
          </div>
        </div>
        <div class="layui-inline">
          <button type="button" id="searchBtn" class="layui-btn layui-btn-sm">搜索</button>
        </div>
      </div>
      <table id="${TABLE_ID}" lay-filter="${TABLE_ID}"></table>
      <div id="${PAGER_ID}"></div>
    </div>
  </div>
  <div class="layui-card" style="margin-top:10px">
    <div class="layui-card-header ${DETAIL_TITLE_CLASS}"></div>
    <div class="layui-card-body">
      <div class="layui-form layui-form-pane">
        <div class="layui-inline">
          <label class="layui-form-label">订单号</label>
          <div class="layui-input-inline" style="width:240px">
            <input type="text" id="key1" class="layui-input" placeholder="完整订单号（当前明细内精确匹配）" autocomplete="off">
          </div>
        </div>
        <div class="layui-inline">
          <button type="button" id="detailSearchBtn" class="layui-btn layui-btn-sm">搜索明细</button>
        </div>
      </div>
      <table id="${TABLE2_ID}" lay-filter="${TABLE2_ID}"></table>
    </div>
  </div>
</div>
<script type="text/html" id="toolbarDemo">
  <button class="layui-btn layui-btn-sm" lay-event="refresh">刷新数据</button>
</script>
<script type="text/html" id="toolbarDemo2">
  <button class="layui-btn layui-btn-sm" lay-event="exportBtn">导出记录</button>
</script>`
}

/* ───────────────────────────── 运行期列（补函数 templet） ───────────────────────────── */

/** 主表运行期列：从 `DAILY_COLS` 派生（声明值只有一处），templet 按 `kind` 分派（D-0323）。 */
export function buildDailyCols(
  /** 某天是否展开（展开指示 ▾/▲ 用；挂载处传「读活变量」的谓词）。 */
  isExpanded: (date: string) => boolean = () => false,
): Col<DailyRow>[] {
  // 来源子行的缩进（自拟视觉：天行下一级的视觉锚；值是本页自定，无对标出处）。
  const INDENT = 'padding-left:24px'
  return [
    {
      ...DAILY_COLS[0]!,
      // 天行 = 日期原样；来源子行 = 缩进 + 来源名（null ⇒ 「未指定」）；
      // loading / error 状态行的**提示文案也落在这里**（一格说清，其余格留空）。
      templet: (r) => {
        if (r.kind === 'day') return `<span>${text(r.day.date)}</span>`
        if (r.kind === 'source') {
          return `<span style="${INDENT}">${text(sourceDisplayName(r.item))}</span>`
        }
        if (r.kind === 'source-loading') {
          return `<span style="${INDENT};color:#999">正在加载来源分解…</span>`
        }
        return `<span style="${INDENT};color:#FF5722">来源分解加载失败</span>`
      },
    },
    {
      ...DAILY_COLS[1]!,
      templet: (r) => {
        if (r.kind === 'day') return `<span>${text(String(r.day.order_count))}</span>`
        if (r.kind === 'source') return `<span>${text(String(r.item.order_count))}</span>`
        return '<span></span>'
      },
    },
    {
      ...DAILY_COLS[2]!,
      // ⭐ D-0323：天行的金额格 = **展开开关**（金额文字保留红字 + 开关指示 ▾/▲，
      //    整格可点、`cursor:pointer`——指示符配色 #999（自拟视觉，无对标出处）；
      //    排序口径仍来自后端，这里只 toggle 展开态）。来源子行 = 金额红字（不可点）。
      templet: (r) => {
        if (r.kind === 'day') {
          const caret = isExpanded(r.day.date) ? '▲' : '▼'
          return (
            '<span class="' + AMOUNT_TOGGLE_CLASS + '"' +
            ` data-date="${text(r.day.date)}" title="点击展开 / 收起按订单来源分解"` +
            ' style="cursor:pointer;color:#FF5722">' +
            `${text(r.day.total_amount)}<span style="color:#999;font-size:12px"> ${caret}</span></span>`
          )
        }
        if (r.kind === 'source') {
          return `<span style="color:#FF5722">${text(r.item.total_amount)}</span>`
        }
        return '<span></span>'
      },
    },
    {
      ...DAILY_COLS[3]!,
      // 天行 = 原有「查看明细」（一字不动）；来源子行 = 当天+该来源的「查看明细」；
      // error 状态行 = 「重试」；loading 行 = 空。
      templet: (r) => {
        if (r.kind === 'day') {
          return (
            '<a href="javascript:void(0)" class="' + VIEW_DETAIL_CLASS + '" style="color:#1E9FFF"' +
            ` data-date="${text(r.day.date)}" title="查看这一天的结算明细">查看明细</a>`
          )
        }
        if (r.kind === 'source') {
          return (
            '<a href="javascript:void(0)" class="' + SOURCE_VIEW_CLASS + '" style="color:#1E9FFF"' +
            ` data-date="${text(r.date)}" data-source="${text(String(sourceParamValue(r.item)))}"` +
            ` data-label="${text(sourceDisplayName(r.item))}"` +
            ` title="查看这一天该来源的结算明细">查看明细</a>`
          )
        }
        if (r.kind === 'source-error') {
          return (
            '<a href="javascript:void(0)" class="' + SOURCE_RETRY_CLASS + '" style="color:#FF5722"' +
            ` data-date="${text(r.date)}" title="重新拉取来源分解">重试</a>`
          )
        }
        return '<span></span>'
      },
    },
  ]
}

/** 明细表运行期列：从 `DETAIL_COLS` 派生（templet 与重设计前逐列同形，见各列注释）。 */
export function buildDetailCols(): Col<SettlementListItem>[] {
  return [
    {
      ...DETAIL_COLS[0]!,
      // 显示名 = `display_name`（昵称优先），载荷没有该键时回落 `username`（部署窗口兜底）。
      templet: (r) => `<span style="color:#5FB878">${text(r.display_name ?? r.username)}</span>`,
    },
    {
      ...DETAIL_COLS[1]!,
      templet: (r) => `<span style="color:#FF5722">${text(r.temp_amount)}</span>`,
    },
    {
      ...DETAIL_COLS[2]!,
      // 「实际结算金额」取值的唯一出口是 `settledOrPayable`（快照优先、回落活值）。
      templet: (r) => `<span style="color:#FF5722">${text(settledOrPayable(r))}</span>`,
    },
    {
      ...DETAIL_COLS[3]!,
      // 正文 = 来源单号（空则回退内置单号，D-0301）；`data-id` = 系统单号（开层那条路由
      // 只认 20 位内置号）；`data-orderIdView` 带展示值给开层标题。与重设计前逐字同形。
      templet: (r) => {
        const viewNo = text(r.source_order_no) || text(r.order_no)
        const no = text(r.order_no)
        return (
          '<a href="javascript:void(0);" class="' + ORDER_LINK_CLASS + '"' +
          ` data-id="${no}" data-orderIdView="${viewNo}"` +
          ` style="color:#0094ff" title="点击查看订单详情">${viewNo}</a>`
        )
      },
    },
    {
      ...DETAIL_COLS[4]!,
      templet: (r) => `<span>${text(r.project)}</span>`,
    },
    {
      ...DETAIL_COLS[5]!,
      templet: (r) => `<span>${text(formatSettledAt(r.settled_at))}</span>`,
    },
    {
      ...DETAIL_COLS[6]!,
      templet: (r) => `<span>${text(r.settled_by)}</span>`,
    },
  ]
}

/* ───────────────────────────── 挂载 ───────────────────────────── */

/** layui 的模块对象在本仓一律这样窄化（`bootstrapIsland` 的返回值是宽松的命名空间）。 */
type LooseFn = (...args: unknown[]) => unknown

export async function mountBillsummaryPage(
  root: HTMLElement,
  // 本页没有跳路由的入口（订单详情是原地开层）⇒ 不用 `ctx`；参数保留是
  // `IslandPage.mount` 的签名位置。下划线 = 「有意未用」（eslint 的 `argsIgnorePattern`）。
  _ctx: IslandContext,
): Promise<() => void> {
  document.title = TITLE
  // 陷阱 ③：先挂 layui 样式/脚本，再塞标记（顺序反了会先画没穿样式的骨架再闪成正常）。
  const layui = await bootstrapIsland(['table', 'layer', 'laydate', 'laypage'])
  root.innerHTML = buildHtml()
  const table = layui.table as unknown as Record<string, LooseFn>
  const layer = layui.layer as unknown as Record<string, unknown>
  const laydate = layui.laydate as unknown as Record<string, LooseFn>
  // ⚠️ `laypage` 不在 `LayuiNamespace` 的声明上（同 `group-grouplist2020.ts` 的先例：
  // 动态模块用的时候才窄化），`bootstrapIsland` 真的会把它带上（modules 列表里点名了）。
  const laypage = (layui as unknown as Record<string, Record<string, LooseFn>>).laypage

  /** 中性提示（1500ms，`islandMsg` 的默认）。 */
  const msg = (t: string): void => islandMsg(layer, t)
  /** 错误提示：拉长到 6000ms——权限/业务失败必须看得清，一闪而过等于「点了没反应」。 */
  const errMsg = (t: string): void => {
    ;(layer.msg as (t: string, o?: unknown) => void)(t, { time: 6000 })
  }
  /** 错误对象的可显示文本。**不自造文案**：`request()` 抛出的 message 就是后端 `msg` 原文。 */
  const errText = (e: unknown): string => (e instanceof Error ? e.message : '操作失败')

  /* ── 日期默认值 + laydate（沿用重设计前的口径：挂载时按「今天 − 3 个月」重算）── */
  const { sDate, eDate } = defaultDateRange()
  const sInput = root.querySelector<HTMLInputElement>('input#sDate')
  const eInput = root.querySelector<HTMLInputElement>('input#eDate')
  if (sInput) sInput.value = sDate
  if (eInput) eInput.value = eDate
  laydate.render?.({ elem: '#sDate', type: 'datetime' })
  laydate.render?.({ elem: '#eDate', type: 'datetime' })

  // 陷阱 ③ 的后半：`initIslandElements` 必须在塞标记**之后**。
  initIslandElements(layui)

  /* ── 主表：服务端分页的按天列表（D-0323 起天行下方可插来源子行）── */

  /** 当前页码 / 页大小（laypage 的 `jump` 维护；「刷新数据」工具栏用当前页重取）。 */
  let currentPage = 1
  let currentPageSize = DAILY_PAGE_SIZE
  /** 主表是否已 `table.render` 过（`reload` 只能用在已渲染的实例上）。 */
  let dailyRendered = false
  /** 当前这页的天行（后端原样；渲染行数组由 `buildDisplayRows` 派生）。 */
  let dailyItems: SettlementDailyItem[] = []

  /* ── D-0323：来源分解的展开态 / 缓存 / 取数状态 ── */

  /** 展开的日期集合（`YYYY-MM-DD`）。 */
  let expandedDays = new Set<string>()
  /** 每天的来源分解缓存 / 取数状态（`ok` 的项在收起后**保留**——再展开不重拉）。 */
  const sourcesByDay = new Map<string, SourcesState>()
  /**
   * 主表数据的「代际」：`loadDaily` 每次取数 +1。展开途中翻页 / 搜索 / 刷新会让
   * 缓存整体作废——迟到的取数结果（await 之后）按代际丢弃，**不写回新代际的缓存**
   * （否则新的一页里会凭空多出上一页那天的子行数据）。
   */
  let dailyGeneration = 0

  /** 读筛选栏当前值。 */
  function readFilters(): BillsummaryFilterValues {
    return {
      sDate: root.querySelector<HTMLInputElement>('#sDate')?.value ?? '',
      eDate: root.querySelector<HTMLInputElement>('#eDate')?.value ?? '',
    }
  }

  /** 把「天行 + 展开态 + 分解状态」交给 `buildDisplayRows` 派生渲染行并落表。 */
  function renderDailyTable(): void {
    const display = buildDisplayRows(dailyItems, expandedDays, sourcesByDay)
    if (!dailyRendered) {
      ;(table.render as (o: unknown) => void)({
        ...DAILY_TABLE,
        id: TABLE_ID,
        data: display,
        // ⚠️ 谓词读**活变量** `expandedDays`（loadDaily 里整个换新集合）⇒ 展开指示
        //    在每次 reload 重算 templet 时都是当下值。
        cols: [buildDailyCols((date) => expandedDays.has(date))],
        text: { none: '暂无数据' },
      })
      dailyRendered = true
    } else {
      ;(table.reload as (id: string, o: unknown) => void)(TABLE_ID, { data: display })
    }
  }

  async function loadDaily(page = 1, pageSize = DAILY_PAGE_SIZE): Promise<void> {
    // ⭐ D-0323：主表数据一换（翻页 / 搜索 / 刷新 ⇒ 都走本函数）⇒ 展开态与来源缓存
    //    **全部作废**（新的一页对不上旧展开；缓存过期会渲染出上一轮的数）。
    dailyGeneration += 1
    expandedDays = new Set()
    sourcesByDay.clear()
    let items: SettlementDailyItem[] = []
    let total = 0
    try {
      const data = await fetchSettlementDailySummary({
        ...toDailyParams(readFilters()),
        page,
        page_size: pageSize,
      })
      items = data.items
      total = data.total
    } catch (e) {
      errMsg(errText(e))
      // 失败就出空表：留着上一页的旧数字比空表更容易被误读。
      items = []
      total = 0
    }
    currentPage = page
    currentPageSize = pageSize
    dailyItems = items
    renderDailyTable()
    // laypage：按服务端 total 画分页栏；`jump` 的 `first` 为 true 是初始渲染，不重取。
    laypage?.render?.({
      elem: PAGER_ELEM,
      count: total,
      limit: pageSize,
      limits: [...DAILY_PAGE_LIMITS],
      curr: page,
      layout: ['count', 'prev', 'page', 'next', 'limit', 'skip'],
      jump: (obj: { curr: number; limit: number }, first: boolean) => {
        if (!first) void loadDaily(obj.curr, obj.limit)
      },
    })
  }

  /* ── D-0323：展开 / 收起 + 来源分解取数（lazy、可缓存、可重试）── */

  /** 拉某天的来源分解（展开首拉与失败重试共用；收起状态下迟到结果按代际丢弃）。 */
  async function loadSources(date: string): Promise<void> {
    const generation = dailyGeneration
    sourcesByDay.set(date, { status: 'loading' })
    renderDailyTable()
    try {
      const data = await fetchSettlementDailySources(date)
      if (generation !== dailyGeneration) return // 那一页已经作废，结果不留
      sourcesByDay.set(date, { status: 'ok', items: data.items })
    } catch (e) {
      if (generation !== dailyGeneration) return
      sourcesByDay.set(date, { status: 'error', message: errText(e) })
      errMsg(errText(e)) // 子行有状态行，toast 让「点了没反应」也看得见
    }
    renderDailyTable()
  }

  /** 点击金额格：toggle 展开 / 收起。收起**保留缓存**（再展开不重拉）。 */
  function toggleExpand(date: string): void {
    // 形状闸：`data-date` 过 DOM 属性，形状不认识就不动（与「查看明细」同一条纪律）。
    if (!DATE_SHAPE.test(date)) return
    if (expandedDays.has(date)) {
      expandedDays.delete(date)
      renderDailyTable()
      return
    }
    expandedDays.add(date)
    if (sourcesByDay.has(date)) {
      // 缓存命中（含 error 态——重试入口在子行的「重试」链接）。
      renderDailyTable()
      return
    }
    void loadSources(date)
  }

  /* ── 明细卡：点「查看明细」才取数 ── */

  /**
   * 当前正在看明细的那一天（`YYYY-MM-DD`）；`null` = 还没选天。
   *
   * ⚠️ **只在 `openDayDetail` 取数成功之后**写（取数失败不算「在看这一天的明细」）；
   * `resetDetail()` 里清回 `null`——搜索会把明细卡还原 ⇒ 导出跟的**天**同步失效。
   */
  let currentDetailDay: string | null = null
  /**
   * ⭐ D-0323：正在看的那一天明细的**来源上下文**（来源子行「查看明细」时非空；
   * 天级「查看明细」是 `null`——那条路径一个字不动）。`resetDetail()` 一并清掉；
   * 明细内的订单号搜索**沿用**它（「当前明细内精确匹配」——来源视图里搜单不该跳出
   * 到全量；自拟口径，D-0323 登记）。
   */
  let currentDetailSource: { value: number | 'none'; label: string } | null = null
  /** 明细表是否已 `table.render` 过。 */
  let detailRendered = false

  function setDetailTitle(t: string): void {
    const el = root.querySelector<HTMLElement>(DETAIL_TITLE_SELECTOR)
    if (el) el.textContent = t
  }

  /** 把明细卡还原成「还没选天」的形态（空表 + 空标题 + 清掉订单号框）。 */
  function resetDetail(): void {
    currentDetailDay = null
    currentDetailSource = null
    setDetailTitle('')
    if (detailRendered) {
      ;(table.reload as (id: string, o: unknown) => void)(TABLE2_ID, {
        data: [],
        page: { curr: 1, limit: DETAIL_PAGE_LIMIT },
      })
    }
    const key = root.querySelector<HTMLInputElement>('#key1')
    if (key) key.value = ''
  }

  /**
   * 打开某一天的明细：取全（分页循环）→ 客户端分页交给 layui。
   *
   * ⭐ D-0323 第三参 `source`（来源子行的「查看明细」才传）：取数参数多带
   * `order_source`，标题带来源上下文；不传 = 天级原路径，行为一字不动。
   */
  async function openDayDetail(
    day: string,
    orderNo = '',
    source?: { value: number | 'none'; label: string },
  ): Promise<void> {
    if (!DATE_SHAPE.test(day)) {
      errMsg('日期格式不正确，无法查看结算明细')
      return
    }
    let rows: SettlementListItem[] = []
    try {
      rows = await fetchAllSettlements(toDetailParams(day, orderNo, source?.value))
    } catch (e) {
      // ⭐ F-03（第五轮审计）：失败**只弹错、不动正在看的明细**——旧行为把
      // 「重看同一天失败」也当成「还原」，正在看的明细卡（行 / 标题 / 单号框）
      // 会被整个清空；「失败还原」的口径本意是「别把上一天的行误读成今天的」，
      // 而重看同一天失败时卡里本来就是这一天的行，清空只会把还看得见的东西
      // 弄没。上一天失败的情形天然不动（`currentDetailDay` 还是旧天，下面
      // 什么都没写）⇒ 两种情形统一为「失败保留现场」。
      errMsg(errText(e))
      return
    }
    currentDetailDay = day
    currentDetailSource = source ?? null
    const opts = {
      ...DETAIL_TABLE,
      id: TABLE2_ID,
      data: rows,
      cols: [buildDetailCols()],
      text: { none: '暂无数据' },
      page: { limit: DETAIL_PAGE_LIMIT, limits: [...DETAIL_PAGE_LIMITS] },
    }
    if (!detailRendered) {
      ;(table.render as (o: unknown) => void)(opts)
      detailRendered = true
    } else {
      ;(table.reload as (id: string, o: unknown) => void)(TABLE2_ID, {
        data: rows,
        page: { curr: 1, limit: DETAIL_PAGE_LIMIT },
      })
    }
    // 来源上下文（自拟格式）：`（来源：X）`；未指定行显示「未指定」。
    setDetailTitle(
      source ? `${day}的结算明细（来源：${source.label}）` : `${day}的结算明细`,
    )
  }

  /** 明细内的订单号搜索：没选天先挡一道；取数沿用 `openDayDetail`（成功才认新值）。 */
  function searchDetail(): void {
    if (currentDetailDay === null) {
      msg('请先在上方列表点击「查看明细」选择日期')
      return
    }
    const orderNo = root.querySelector<HTMLInputElement>('#key1')?.value ?? ''
    // ⭐ D-0323：来源视图里搜单**保持来源上下文**（天级视图 currentDetailSource 是 null，
    //    传 undefined ⇒ 参数不带 order_source，天级行为一字不动）。
    void openDayDetail(
      currentDetailDay,
      orderNo,
      currentDetailSource ?? undefined,
    )
  }

  /* ── 工具栏：主表「刷新数据」；明细表「导出记录」── */

  type ToolbarObj = { event?: string }
  const onToolbar = (e: string, cb: (obj: ToolbarObj) => void): void => {
    ;(table.on as unknown as (ev: string, fn: (obj: ToolbarObj) => void) => void)(e, cb)
  }
  onToolbar(`toolbar(${TABLE_ID})`, (obj) => {
    // ⚠️ **只认 `refresh`**：`defaultToolbar:['filter']` 的内置「筛选列」图标
    // （事件 `LAYTABLE_COLS`）交给 layui 自己开面板，不用 else 兜底弹提示。
    if ((obj.event ?? '') === 'refresh') void loadDaily(currentPage, currentPageSize)
  })
  onToolbar(`toolbar(${TABLE2_ID})`, (obj) => {
    if ((obj.event ?? '') !== 'exportBtn') return
    if (currentDetailDay === null) {
      // 防御分支（正常操作碰不到：这张表的工具栏只在明细渲染后才可见）。
      msg('请先点击「查看明细」选择日期')
      return
    }
    /**
     * 导出**当前正在看的那一天**（`currentDetailDay`，不是筛选栏那两个框——
     * 筛选栏可能已经被改过）。
     * ⚠️ `date_type: 'settled'`：导出端点的 `date_from/date_to` 默认按**创建时间**筛，
     * 不带它就与「当天结的账」对不上（旧版就是这么错位的，见文件头）。
     * ⛔ **不带 `order_no`**（业务方裁定「不带就行」，重设计沿用）。
     * ⭐ D-0323：来源视图里导出的**仍是整天**——导出端点（`/performances/export`）
     *    没有 `order_source` 参数，带不了；「一字不动」照旧（自拟登记）。
     */
    void downloadPerformanceExport({
      settle_status: '已结账',
      date_type: 'settled',
      date_from: currentDetailDay,
      date_to: currentDetailDay,
    })
      .then(({ blob, filename }) => {
        // 真落盘：`URL.createObjectURL` + 一个 `<a download>`（为什么不是 `window.open`
        // 见 `api/performances.ts::downloadPerformanceExport` 的注释）。
        // ⚠️ **必须插进文档再 `click()`**：Firefox 对游离节点不触发下载。
        const url = URL.createObjectURL(blob)
        const link = document.createElement('a')
        link.href = url
        link.download = filename
        document.body.appendChild(link)
        link.click()
        link.remove()
        window.setTimeout(() => URL.revokeObjectURL(url), 0)
      })
      .catch((e: unknown) => errMsg(errText(e)))
  })

  /* ── 筛选：搜索按钮 / 回车 / 明细搜索 ── */

  /** 搜索：主表回第 1 页重取，**并**把明细卡还原（旧的一天不再对应新筛选）。 */
  function search(): void {
    resetDetail()
    void loadDaily(1, currentPageSize)
  }
  root.querySelector<HTMLElement>('#searchBtn')?.addEventListener('click', search)
  root.querySelector<HTMLElement>('#detailSearchBtn')?.addEventListener('click', searchDetail)

  /** 回车 = 搜索（在日期框上）或在 `#key1` 上 = 搜明细——按目标分流。 */
  const onKeydown = (event: KeyboardEvent): void => {
    if (event.key !== 'Enter') return
    if ((event.target as HTMLElement).tagName !== 'INPUT') return
    event.preventDefault()
    if ((event.target as HTMLElement).id === 'key1') searchDetail()
    else search()
  }
  root.addEventListener('keydown', onKeydown)

  /* ── 行内链接（主表「查看明细」/ 明细表「关联订单」，一份委托）── */

  /**
   * 「关联订单」原地开层装我们自己的复刻页（沿用重设计前的参数：`1125px×90%`、
   * `type:2`、`skin:'layui-layer-lan'`、`anim:8`、`shadeClose`；对标出处
   * `billsummary.js` L206-217，本页重设计后保留同一交互）。
   *
   * ⛔ 开层前的「单号形状闸」`/^\d{20}$/`——与那条路由自己的 `(\d{20})` 约束同源；
   * `layer.open` 的 `title` 是 HTML 上下文，`dataset` 会把实体解码回 `<`/`>` ⇒ 边界上再转义。
   */
  const openOrderDetailLayer = (orderNo: string, viewNo: string): void => {
    if (!/^\d{20}$/.test(orderNo)) {
      msg('订单号格式不正确，无法打开订单详情')
      return
    }
    ;(layer.open as (o: unknown) => void)({
      // 标题前缀 = 展示单号（可含来源号自由文本 ⇒ 边界上转义一次，D-0301）。
      title: `${escapeHtml(viewNo !== '' ? viewNo : orderNo)}订单详情`,
      shade: [0.3, '#333'],
      shadeClose: true,
      type: 2,
      skin: 'layui-layer-lan',
      anim: 8,
      area: ['1125px', '90%'],
      content:
        `${import.meta.env.BASE_URL}layui-island/page.html?page=order-orderview` +
        `&order_no=${encodeURIComponent(orderNo)}`,
    })
  }

  const onCellClick = (event: Event): void => {
    const target = event.target as HTMLElement
    const detail = target.closest<HTMLElement>(VIEW_DETAIL_SELECTOR)
    if (detail) {
      void openDayDetail(detail.dataset.date ?? '')
      return
    }
    // ⭐ D-0323 三个新入口（同一份委托；类名各自独立常量——陷阱 ①）：
    // ① 金额格 toggle 展开 / 收起；② 来源子行「查看明细」（当天 + 该来源）；
    // ③ 失败状态行「重试」。
    const toggle = target.closest<HTMLElement>(AMOUNT_TOGGLE_SELECTOR)
    if (toggle) {
      toggleExpand(toggle.dataset.date ?? '')
      return
    }
    const sourceDetail = target.closest<HTMLElement>(SOURCE_VIEW_SELECTOR)
    if (sourceDetail) {
      const raw = sourceDetail.dataset.source ?? ''
      const label = sourceDetail.dataset.label ?? UNSPECIFIED_SOURCE_LABEL
      // `data-source` 只有两种形态（templet 造的）：数字串 / `'none'`；别的形状不取数。
      if (raw === 'none' || /^\d+$/.test(raw)) {
        void openDayDetail(sourceDetail.dataset.date ?? '', '', {
          value: raw === 'none' ? 'none' : Number(raw),
          label,
        })
      }
      return
    }
    const retry = target.closest<HTMLElement>(SOURCE_RETRY_SELECTOR)
    if (retry) {
      // ⭐ F-03（第五轮审计）：「重试」入口补**日期形状闸**——与 `toggleExpand` /
      //    `openDayDetail` 对同类 `data-date` DOM 属性的纪律一致（防御一致性；
      //    数据源是自家 templet 造的 `data-date`，形状不认识就不发请求）。
      const retryDate = retry.dataset.date ?? ''
      if (DATE_SHAPE.test(retryDate)) void loadSources(retryDate)
      return
    }
    const order = target.closest<HTMLElement>(ORDER_LINK_SELECTOR)
    if (order) {
      const orderNo = order.dataset.id ?? ''
      // ⭐ D-0301：第二参 = 展示单号（`data-orderIdView`，标题前缀用；缺属性回落内置号）。
      if (orderNo !== '') openOrderDetailLayer(orderNo, order.dataset.orderidview ?? orderNo)
    }
  }
  root.addEventListener('click', onCellClick)

  // 首屏取数（主表第 1 页）。
  await loadDaily(1, DAILY_PAGE_SIZE)

  return () => {
    root.removeEventListener('click', onCellClick)
    root.removeEventListener('keydown', onKeydown)
    root.innerHTML = ''
  }
}

export default {
  mount: (root: HTMLElement, ctx: IslandContext) => mountBillsummaryPage(root, ctx),
} satisfies IslandPage
