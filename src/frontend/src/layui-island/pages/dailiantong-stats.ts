/**
 * 【代练通按日统计】island 页（island key = `dailiantong-stats`，
 * 路由 `/system/dailiantong-stats`，菜单 label「代练通按日统计」）。
 *
 * 后端契约：`src/apps/business/api/dailiantong_extra_views.py::DailiantongStatsView` +
 * `dailiantong_extra_actions.py::platform_order_stats`（代理上游 `LevelOrderStat`，带缓存）。
 * 前端封装与节点码在 `@/api/dailiantongExtra`。
 *
 * ## 这一页是**自有功能**，不是复刻
 *
 * 对标物（鸡蛋铺）没有「代练通按日统计」这一页，也没有证据件可对拍 ⇒ **不进**
 * `config/replicaRoutes.ts`（那张表是对标 89 条渲染菜单的逐条转录），也不进 `REPLICA_SKIP`；
 * island key 登记在 `config/__tests__/replicaRoutes.spec.ts` 的 `OWN_FEATURE_PAGE_KEYS`。
 * 标记由本文件自己写，样式沿用本仓 layui island 的惯例（⛔ 不用 Element Plus）。
 *
 * ## 字段以接口实际返回为准
 *
 * 上游 `LevelOrderStat` 的行是**裸对象**（`docs/references/dailiantong/open-api-v3.4.md`
 * 「订单统计」一节列了 `QDate` / `PubCount` / `PubBal` / … / `SettleRate` 十三个键），
 * 但真实回包可能多、可能少。所以：
 *  - 已知键按文档顺序 + 中文表头渲染；
 *  - 返回里多出来的键**追加**在末尾（表头就是原始键名，⛔ 不猜中文）；
 *  - 某个行没有的键显示「—」（空结果 ≠ 没有数据，见 `AGENTS.md` §5-E）。
 * ⛔ 页面不造数、不补 0、不把「上游没给」说成「就是 0」。
 *
 * ## 三态必须看得见（⛔ 不静默空白）
 *
 * 状态条 `STATUS_ID` 恒在：**加载中…** / **共 N 天数据** / **这个区间里还没有统计数据** /
 * **加载失败：<后端原文>**（失败时带一颗「重试」）。失败文案**原样**来自统一请求通道抛出的
 * `Error.message`（后端中文），页面不另编一句。
 *
 * ## 日期窗口
 *
 * 默认**最近 7 天（含今天）**，与后端 `default_stats_window()` 同一口径，但由页面算好摆进
 * 日期框（⛔ 不靠后端默认值——否则「框里显示的和实际查的对不上」）。
 *
 * ## 四条 island 陷阱（`AGENTS.md` §5-G；本页**没有定时器**）
 *
 * ① 类名与选择器分成两个常量；
 * ② 以 `(` 开头的语句一律前置分号；
 * ③ 顺序：`await bootstrapIsland(...)`（先挂样式）→ 门禁 → `root.innerHTML = buildHtml()`
 *    （再塞标记）→ `initIslandElements(layui)`（最后按标记里的 `lay-*` 初始化）。
 * 三条都有**运行期**判据（`__tests__/dailiantong-stats.spec.ts`）。
 */
import {
  DAILIANTONG_EXTRA_NODE,
  fetchDailiantongStats,
  STATS_SIDE_PUBLISH,
  type DailiantongStatRow,
  type DailiantongStats,
  type StatsSide,
} from '@/api/dailiantongExtra'
import { fetchMe } from '@/api/auth'
import { escapeHtml } from '@/layui-island/escape'
import { canNode } from '@/layui-island/perms'
import { bootstrapIsland, initIslandElements, islandMsg, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

/** island key（= 文件名 = 路由 `meta.islandPage`，三处必须逐字一致）。 */
export const PAGE_KEY = 'dailiantong-stats'
export const TITLE = '代练通按日统计'

/** 页面容器的**类名**（拼进标记用）与**选择器**（查节点用）——两者不能混（陷阱 ①）。 */
export const PANEL_CLASS = 'dltStatsPanel'
export const PANEL_SELECTOR = `.${PANEL_CLASS}`

/** 筛选条 / 状态条 / 表格节点。 */
export const DATE_FROM_ID = 'dltStatsDateFrom'
export const DATE_TO_ID = 'dltStatsDateTo'
export const SIDE_SELECT_ID = 'dltStatsSide'
export const QUERY_BUTTON_ID = 'dltStatsQuery'
export const STATUS_ID = 'dltStatsStatus'
export const TABLE_HEAD_ID = 'dltStatsTableHead'
export const TABLE_BODY_ID = 'dltStatsTableBody'

/** 失败态里「重试」按钮的属性（值 = 动作名；选择器由常量拼，⛔ 不写字面串）。 */
export const RETRY_ATTR = 'data-dlt-stats-retry'
export const RETRY_SELECTOR = `[${RETRY_ATTR}]`

/** 页内文案（抽成常量只为让 spec 钉住字面值）。 */
export const MESSAGES = {
  loading: '加载中…',
  empty: '这个区间里还没有统计数据',
  failed: '加载失败',
  needDates: '请选择开始日期和结束日期',
  badRange: '开始日期不能晚于结束日期',
  noAccess: '无权限查看代练通按日统计',
} as const

/** 一行统计的列定义（已知键的中文表头；顺序照上游文档）。 */
export type StatColumn = { key: string; label: string }

/**
 * 已知列（`docs/references/dailiantong/open-api-v3.4.md`「订单统计」一节的十三个键）。
 * ⚠️ 只决定**中文表头与顺序**；哪些列真的出现由回包决定（见 `columnsFor`）。
 */
export const STAT_COLUMNS: readonly StatColumn[] = [
  { key: 'QDate', label: '日期' },
  { key: 'PubCount', label: '发单量' },
  { key: 'PubBal', label: '发单金额' },
  { key: 'PubBaseBal', label: '备注总金额' },
  { key: 'AcceptCount', label: '被接单量' },
  { key: 'SettleCount', label: '完单量' },
  { key: 'SettleBal', label: '完单金额' },
  { key: 'CancelCount', label: '撤单量' },
  { key: 'ArbitRateCount', label: '介入单量' },
  { key: 'PayLevelBal', label: '撤销支付（含介入）' },
  { key: 'ReceiveBal', label: '撤销获赔保证金' },
  { key: 'QAvgTime', label: '均单验收时间' },
  { key: 'SettleRate', label: '完单率' },
]

/** 日期 → `YYYY-MM-DD`（**本地日**；纯函数、可测）。 */
export function isoDate(day: Date): string {
  const month = String(day.getMonth() + 1).padStart(2, '0')
  const date = String(day.getDate()).padStart(2, '0')
  return `${day.getFullYear()}-${month}-${date}`
}

/** 默认统计窗口：**最近 7 天（含今天）**，与后端 `default_stats_window()` 同口径。 */
export function defaultStatsRange(now: Date = new Date()): { start: string; end: string } {
  const end = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const start = new Date(end)
  start.setDate(start.getDate() - 6)
  return { start: isoDate(start), end: isoDate(end) }
}

/** 一个单元格的显示值：缺失 / 空白 ⇒「—」，其余原样转字符串（⛔ 不补 0、不换算）。 */
export function statValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  const text = String(value).trim()
  return text === '' ? '—' : text
}

/**
 * 由**实际返回**决定列：已知键按文档顺序、未知键按首次出现顺序追加。
 *
 * ⚠️ 空结果（`items` 与 `total` 都空）时退回**全部已知列**：否则空态只剩一张没有表头的空表，
 * 用户看不出「这一页本来是查什么」。
 */
export function columnsFor(
  rows: readonly DailiantongStatRow[],
  total: DailiantongStatRow = {},
): StatColumn[] {
  const present = new Set<string>()
  for (const row of rows) for (const key of Object.keys(row)) present.add(key)
  for (const key of Object.keys(total)) present.add(key)
  if (present.size === 0) return [...STAT_COLUMNS]

  const known = new Set(STAT_COLUMNS.map((column) => column.key))
  const columns = STAT_COLUMNS.filter((column) => present.has(column.key))
  const extras: StatColumn[] = []
  const seen = new Set(columns.map((column) => column.key))
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      if (known.has(key) || seen.has(key)) continue
      seen.add(key)
      extras.push({ key, label: key })
    }
  }
  return [...columns, ...extras]
}

/** 表头标记（纯函数）。 */
export function headHtml(columns: readonly StatColumn[]): string {
  return columns.map((column) => `<th>${escapeHtml(column.label)}</th>`).join('')
}

/**
 * 表体标记（纯函数）：每天一行 + 有汇总时一行「合计」；空列表时一行空态文案。
 * ⚠️ 所有来自上游的值都过 `escapeHtml`（它们走 `innerHTML`）。
 */
export function bodyHtml(payload: DailiantongStats): string {
  const columns = columnsFor(payload.items, payload.total)
  const span = Math.max(columns.length, 1)
  if (payload.items.length === 0) {
    return (
      `<tr><td colspan="${span}" style="text-align:center;color:#999">` +
      `${escapeHtml(MESSAGES.empty)}</td></tr>`
    )
  }
  const rows = payload.items
    .map(
      (row) =>
        '<tr>' +
        columns
          .map((column) => `<td>${escapeHtml(statValue(row[column.key]))}</td>`)
          .join('') +
        '</tr>',
    )
    .join('')
  const hasTotal = columns.some((column) => column.key in payload.total)
  const totalRow = !hasTotal
    ? ''
    : '<tr>' +
      columns
        .map((column, index) => {
          const text =
            index === 0 ? '合计' : statValue(payload.total[column.key])
          return `<td><b>${escapeHtml(text)}</b></td>`
        })
        .join('') +
      '</tr>'
  return rows + totalRow
}

/** 页面标记（筛选条 + 状态条 + 表格）。 */
export function buildHtml(range: { start: string; end: string }): string {
  return `
<div class="${PANEL_CLASS}" style="padding:10px 15px">
  <div style="margin:4px 0 10px;color:#666">
    数据来自当前租户绑定的代练通账号；按天汇总，日期窗口最多 7 天。
  </div>
  <div style="margin:4px 0 10px">
    <input type="date" id="${DATE_FROM_ID}" class="layui-input" style="width:150px;display:inline-block" value="${escapeHtml(range.start)}">
    <span style="margin:0 6px">至</span>
    <input type="date" id="${DATE_TO_ID}" class="layui-input" style="width:150px;display:inline-block" value="${escapeHtml(range.end)}">
    <select id="${SIDE_SELECT_ID}" class="layui-input" style="width:110px;display:inline-block;margin-left:8px">
      <option value="${STATS_SIDE_PUBLISH}">发单</option>
      <option value="0">接单</option>
    </select>
    <button type="button" class="layui-btn layui-btn-sm" id="${QUERY_BUTTON_ID}" style="margin-left:8px">查询</button>
  </div>
  <div id="${STATUS_ID}" style="margin:6px 0;min-height:20px">${MESSAGES.loading}</div>
  <table class="layui-table" style="margin:0">
    <thead><tr id="${TABLE_HEAD_ID}"></tr></thead>
    <tbody id="${TABLE_BODY_ID}"></tbody>
  </table>
</div>`
}

/** 错误对象 → 用户可见文案（后端 `msg` 优先；兜底那句只在意外分支用）。 */
function errorText(error: unknown): string {
  return error instanceof Error && error.message !== '' ? error.message : MESSAGES.failed
}

export const __testables = {
  bodyHtml,
  buildHtml,
  columnsFor,
  defaultStatsRange,
  headHtml,
  isoDate,
  statValue,
}

export default {
  mount: async (root: HTMLElement, _ctx): Promise<() => void> => {
    document.title = TITLE
    // ⚠️ 顺序是硬要求（`AGENTS.md` §5-G 陷阱 ③）：先挂样式（`bootstrapIsland`），再塞页面标记，
    //    最后 `initIslandElements`（它要按标记里的 `lay-*` 初始化）。
    const layui: LayuiNamespace = await bootstrapIsland(['layer'])
    const layer = layui.layer as LayuiModule
    const msg = (text: string): void => islandMsg(layer as unknown as Record<string, unknown>, text)

    // ⚠️ **本页自己的门禁**：iframe 可被深链（`page.html?page=dailiantong-stats`）打开，
    //    那一刻路由的 `meta.permissions` 不生效（先例 `pages/feedback.ts`）。
    //    ⛔ 注入标记必须排在门禁**之后**（被拒的深链不该看到整个页面壳）。
    const me = await fetchMe()
    if (!canNode(me, DAILIANTONG_EXTRA_NODE)) {
      msg(MESSAGES.noAccess)
      return () => {}
    }

    const range = defaultStatsRange()
    root.innerHTML = buildHtml(range)
    initIslandElements(layui)

    const statusEl = root.querySelector<HTMLElement>(`#${STATUS_ID}`)
    const headEl = root.querySelector<HTMLElement>(`#${TABLE_HEAD_ID}`)
    const bodyEl = root.querySelector<HTMLElement>(`#${TABLE_BODY_ID}`)
    const fromEl = root.querySelector<HTMLInputElement>(`#${DATE_FROM_ID}`)
    const toEl = root.querySelector<HTMLInputElement>(`#${DATE_TO_ID}`)
    const sideEl = root.querySelector<HTMLSelectElement>(`#${SIDE_SELECT_ID}`)

    type StateKind = 'loading' | 'ready' | 'empty' | 'error'

    /**
     * 状态条的**唯一**出口。失败态用 DOM 节点拼（后端原文可能含 `<`），⛔ 不拼 HTML。
     */
    function setState(kind: StateKind, text: string): void {
      if (!statusEl) return
      statusEl.textContent = ''
      if (kind === 'error') statusEl.style.color = '#ff0000'
      else statusEl.style.color = '#666'
      if (kind === 'error') {
        const span = document.createElement('span')
        span.textContent = `${MESSAGES.failed}：${text}`
        const retry = document.createElement('button')
        retry.type = 'button'
        retry.className = 'layui-btn layui-btn-xs layui-btn-normal'
        retry.setAttribute(RETRY_ATTR, '1')
        retry.style.marginLeft = '8px'
        retry.textContent = '重试'
        statusEl.appendChild(span)
        statusEl.appendChild(retry)
        return
      }
      statusEl.textContent = text
    }

    function render(payload: DailiantongStats): void {
      const columns = columnsFor(payload.items, payload.total)
      if (headEl) headEl.innerHTML = headHtml(columns)
      if (bodyEl) bodyEl.innerHTML = bodyHtml(payload)
      if (payload.items.length === 0) {
        setState('empty', MESSAGES.empty)
      } else {
        setState('ready', `共 ${payload.items.length} 天数据（${payload.start} 至 ${payload.end}）`)
      }
    }

    /** 读当前筛选并发请求（唯一取数出口；查询 / 重试都走它）。 */
    async function load(): Promise<void> {
      const start = fromEl?.value ?? ''
      const end = toEl?.value ?? ''
      if (start === '' || end === '') {
        setState('error', MESSAGES.needDates)
        return
      }
      if (start > end) {
        setState('error', MESSAGES.badRange)
        return
      }
      const side: StatsSide = sideEl?.value === '0' ? 0 : STATS_SIDE_PUBLISH
      setState('loading', MESSAGES.loading)
      try {
        const payload = await fetchDailiantongStats({ start, end, is_pub: side })
        render(payload)
      } catch (error) {
        setState('error', errorText(error))
      }
    }

    root.querySelector<HTMLElement>(`#${QUERY_BUTTON_ID}`)?.addEventListener('click', () => {
      void load()
    })
    // 「重试」走状态条上的**事件委托**（状态条会被 `setState` 反复重铺）。
    statusEl?.addEventListener('click', (event: MouseEvent) => {
      const target = event.target as HTMLElement | null
      if (!target?.closest(RETRY_SELECTOR)) return
      void load()
    })

    // 首屏先画**全部已知列**的表头：加载中 / 空态都不是一张没有表头的白板；
    // 回包到了之后 `render()` 会按实际返回的键重画。
    if (headEl) headEl.innerHTML = headHtml(STAT_COLUMNS)

    // 首屏：标记里已经写着「加载中…」，这里发第一次请求。
    await load()

    return () => {
      root.innerHTML = ''
    }
  },
} satisfies IslandPage
