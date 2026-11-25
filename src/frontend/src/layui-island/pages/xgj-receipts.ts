/**
 * 【财务相关 → 闲管家收款明细】island 页（island key = `xgj-receipts`，
 * 路由 `/finance/xgj-receipts`，菜单 label「闲管家收款明细」）。
 *
 * 计划：spec `docs/specs/2026-10-22-xgj-receipt-detail-and-finance-merge-design.md` §8.1。
 * 端点封装与行类型在 `@/api/xgjReceipts`（那里逐键登记了契约出处）。
 *
 * ## 这一页是**自有功能**，不是复刻
 *
 * 对标物（鸡蛋铺）不接闲管家推送、没有「收款明细」这一页 ⇒ 本页**没有证据件**、不走
 * `artifactPage` / `mountConfigLike` 那条复刻链路。标记由本文件自己写，风格照同类的
 * **自建** island 页（`agents-wallet-adjust.ts` / `agents-withdraw-review.ts`）。
 * ⛔ 不用 Element Plus。
 *
 * ## 页面形状（spec §8.1 / 业务方 R4–R7）
 *
 * 1. **筛选条**：`date_from` / `date_to` 两个原生日期输入（缺省 = **前一天..前一天**），
 *    **改选即重查**（R7「自动重新统计」）——不设「查询」按钮，`change` 事件就是触发器。
 * 2. **主表**（按店铺分组，后端排好序：实收总额降序）：店铺名称 / 交易成功笔数 /
 *    实收总金额 / 金额缺失笔数。**底部合计行** = 独立的一张单行小表（数据来自响应的
 *    `totals` 块，**由后端算**——前端不做任何金额加法，⛔ 不引入浮点）。
 * 3. **次级视图**：点主表的一行 → 下方明细区拉**这家店**的逐笔明细（单号 / 会员名 /
 *    金额 / 时间 / 匹配结果；服务端分页 = 上一页/下一页）。
 *    统计口径声明（`caliberNote`）= spec §12 F4-D 的如实口径（买家实付减已退、
 *    按详情回查时点；交易成功口径 ≠ 净收款口径，防误读）。
 *
 * ## 已知会不同（本页是自有功能，对标物没有这一页）
 *
 * ⭐ **筛选条多一颗「订单分组」**（2026-10-26，计划 `docs/plans/2026-10-26-order-source-groups.md` §5）：
 * 原生 `<select>`（与本页日期输入同族，不引 xm-select），首项「全部分组」；
 * **改选即查**（与日期同一口径：不设查询按钮）。
 * ⚠️ **语义 = 店铺当前所属来源的分组**：历史行会随归组变化，未识别店铺不属于任何分组
 * （选分组时被排除）——这一句同时写进 `caliberNote` 上屏，并登记在交付说明里。
 * ⚠️ 改日期时 `applyRange` 会**整个重建** `range` ⇒ 分组必须显式带进新对象
 * （页测试「改日期后分组不丢」钉住）。
 *
 * ## 权限门：fail-closed，且只有一道
 *
 * 路由 `meta.permissions` 已经把这一页挡住（节点 `finance.xgj_receipt_detail`）；
 * 页内 `fetchMe()` + `canNode()` 是**同一道门**在 iframe 深链那一侧的落实
 * （先例：`agents-wallet-adjust.ts`）。拿不到节点 ⇒ 提示一句、**不注入标记、
 * 不发任何请求**（`fetchMe()` 那一次身份请求照发——它就是这道门）。
 *
 * ## 四条 island 陷阱（`AGENTS.md` §5-G；本页**没有定时器**）
 *
 * ① 类名与选择器分成两个常量；
 * ② 以 `(` 开头的语句一律前置分号；
 * ③ 顺序：`await bootstrapIsland(...)`（先挂样式）→ `root.innerHTML = buildHtml()`
 *    （再塞标记）→ `initIslandElements(layui)`（最后按标记里的 `lay-*` 初始化）。
 * ⚠️ 三条都有**运行期**判据（`__tests__/xgj-receipts.interaction.spec.ts`）。
 */
import { fetchMe } from '@/api/auth'
import { fetchOrderSourceGroups } from '@/api/orderSources'
import {
  fetchReceiptList,
  fetchReceiptSummary,
  XGJ_RECEIPT_NODE,
  type ReceiptDetailRow,
  type ReceiptSummaryRow,
} from '@/api/xgjReceipts'
import { escapeHtml } from '@/layui-island/escape'
import { canNode } from '@/layui-island/perms'
import { bootstrapIsland, initIslandElements, islandMsg, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

/** island key（= 文件名 = 路由 `meta.islandPage`，三处必须逐字一致）。 */
export const PAGE_KEY = 'xgj-receipts'
export const TITLE = '闲管家收款明细'

/** 页面容器的**类名**（拼进标记用）与**选择器**（查节点用）——两者不能混（陷阱 ①）。 */
export const PANEL_CLASS = 'xgjReceiptsPanel'
export const PANEL_SELECTOR = `.${PANEL_CLASS}`

/** 两张表的 id / elem（layui 的 `reloadData` / `on` 要的是 **id**，带 `#` 是静默 no-op）。 */
export const SUMMARY_TABLE_ID = 'xgjReceiptSummaryTable'
export const SUMMARY_TABLE_ELEM = `#${SUMMARY_TABLE_ID}`
export const DETAIL_TABLE_ID = 'xgjReceiptDetailTable'
export const DETAIL_TABLE_ELEM = `#${DETAIL_TABLE_ID}`

/** 筛选条 / 合计行 / 明细区各节点的 id。 */
export const DATE_FROM_ID = 'xgjReceiptDateFrom'
export const DATE_TO_ID = 'xgjReceiptDateTo'
export const TOTALS_ID = 'xgjReceiptTotals'
export const DETAIL_TITLE_ID = 'xgjReceiptDetailTitle'
export const DETAIL_HINT_ID = 'xgjReceiptDetailHint'
export const PREV_PAGE_ID = 'xgjReceiptPrevPage'
export const NEXT_PAGE_ID = 'xgjReceiptNextPage'
/** ⭐ 「订单分组」下拉（2026-10-26，计划 §5；原生 select，与本页筛条同族）。 */
export const SOURCE_GROUP_ID = 'xgjReceiptSourceGroup'

/** 无节点时的自检提示。 */
export const NO_ACCESS_MESSAGE = '无权限查看闲管家收款明细'

/** 「未识别店铺」桶（`shop_identity` 为空的行，补数收敛前可见）的展示名兜底文案。 */
export const UNKNOWN_SHOP_LABEL = '未识别店铺'

/** 页内文案（抽成常量只为让 spec 钉住字面值）。 */
export const MESSAGES = {
  noAccess: NO_ACCESS_MESSAGE,
  failed: '加载失败',
  /** 主表空（窗口内一笔都没有）。 */
  summaryEmpty: '这个区间里还没有交易成功的记录',
  /** 明细区未选店时的占位。 */
  detailIdle: '点上面的一行店铺，看它的逐笔明细',
  /** 明细区空。 */
  detailEmpty: '这家店在这个区间里没有明细',
  /**
   * ⭐ 统计口径声明（spec §12 F4-D 的如实口径：金额=买家实付减已退、按详情回查时点；
   * 交易成功口径 ≠ 净收款口径，防误读）。
   */
  caliberNote:
    '统计口径：按「交易成功」记录；金额=买家实付减已退（按详情回查时点），'
    + '不含交易成功之后又退款的冲减（退款走自动退单线）。'
    // ⭐（计划 §4 #7）分组筛的是**店铺当前所属来源**，不是记账当时——
    //    历史行会随归组变化；未识别店铺不属于任何分组（选分组时被排除）。
    + '「订单分组」按店铺当前所属来源统计（历史行会随归组变化，未识别店铺不计入任何分组）。',
  /** 合计行的四个标签。 */
  totalsLabels: { shops: '家店铺', count: '笔', missing: '笔金额缺失' },
} as const

/** 明细区一页取多少条（后端默认 50；本页固定用它，翻页由 prev/next 驱动）。 */
export const DETAIL_PAGE_SIZE = 50

/**
 * 「前一天」的 `yyyy-MM-dd` 字符串（**本地日**；纯函数、可测）。
 * ⚠️ 缺省窗口的**唯一出处**：与后端的缺省口径（本地日前一天）一致，
 * 两边各算各的（页面要把值摆进输入框，不能靠后端默认）。
 */
export function yesterdayStr(now: Date = new Date()): string {
  const day = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  day.setDate(day.getDate() - 1)
  const month = String(day.getMonth() + 1).padStart(2, '0')
  const date = String(day.getDate()).padStart(2, '0')
  return `${day.getFullYear()}-${month}-${date}`
}

/** 金额格显示：缺失（空串）⇒ 灰色「缺失」；有值原样上屏（字符串，⛔ 不转数字）。 */
export function amountCell(row: Pick<ReceiptDetailRow, 'amount' | 'amount_missing'>): string {
  if (row.amount_missing) return '<span style="color:#999">缺失</span>'
  return escapeHtml(row.amount)
}

/** 明细区标题：选中的店铺名 + 单号数（该店汇总行的笔数，仅作展示参考）。 */
export function detailTitle(shopName: string): string {
  return `明细：${shopName}`
}

/** 错误对象 → 用户可见文案（后端 `msg` 优先；兜底那句不带字段名，仅供意外分支）。 */
function errorText(error: unknown): string {
  return error instanceof Error && error.message !== '' ? error.message : MESSAGES.failed
}

/** 合计行的标记（数据来自 `totals`；四格与主表四列同序）。 */
export function totalsHtml(totals: {
  shop_count: number
  count: number
  amount: string
  missing_count: number
}): string {
  return (
    `<table class="layui-table" id="${TOTALS_ID}" style="margin:8px 0 0">` +
    `<tbody><tr>` +
    `<td>合计（${totals.shop_count} ${MESSAGES.totalsLabels.shops}）</td>` +
    `<td>${totals.count} ${MESSAGES.totalsLabels.count}</td>` +
    `<td>${escapeHtml(totals.amount)} 元</td>` +
    `<td>${totals.missing_count} ${MESSAGES.totalsLabels.missing}</td>` +
    `</tr></tbody></table>`
  )
}

/**
 * 页面标记（筛选条 + 主表 + 合计 + 明细区）。
 *
 * ⚠️ 筛选输入用**原生** `<input type="date">`：`change` 事件即「改选即重查」的触发器
 * （R7），且 jsdom 里可直接驱动。 ⚠️ 明细区初始是占位文案（`detailIdle`）。
 */
export function buildHtml(defaultFrom: string, defaultTo: string): string {
  return `
<div class="${PANEL_CLASS}" style="padding:10px 15px">
  <div style="margin:8px 0">
    <input type="date" id="${DATE_FROM_ID}" class="layui-input" style="width:150px;display:inline-block" value="${escapeHtml(defaultFrom)}">
    <span style="margin:0 6px">至</span>
    <input type="date" id="${DATE_TO_ID}" class="layui-input" style="width:150px;display:inline-block" value="${escapeHtml(defaultTo)}">
    <select id="${SOURCE_GROUP_ID}" class="layui-input" style="width:150px;display:inline-block;margin-left:8px">
      <option value="">全部分组</option>
    </select>
    <span id="${DETAIL_HINT_ID}" style="color:#999;margin-left:10px"></span>
  </div>
  <div style="color:#999;margin:4px 0 8px">${MESSAGES.caliberNote}</div>
  <table id="${SUMMARY_TABLE_ID}" lay-filter="${SUMMARY_TABLE_ID}"></table>
  <div id="${TOTALS_ID}" style="margin:8px 0 0"></div>
  <div id="${DETAIL_TITLE_ID}" style="font-weight:bold;margin:14px 0 6px">${MESSAGES.detailIdle}</div>
  <table id="${DETAIL_TABLE_ID}" lay-filter="${DETAIL_TABLE_ID}"></table>
  <div style="margin-top:6px">
    <button id="${PREV_PAGE_ID}" class="layui-btn layui-btn-sm layui-btn-primary">上一页</button>
    <button id="${NEXT_PAGE_ID}" class="layui-btn layui-btn-sm layui-btn-primary">下一页</button>
  </div>
</div>`
}

/**
 * 把分组字典填进「订单分组」下拉（**保留首项「全部分组」**；只 append、不重写）。
 *
 * ⚠️ 字典取不到时调用方传空数组 ⇒ 只剩首项（这一项筛不了，页面照常可用）。
 */
export function fillSourceGroupOptions(
  root: HTMLElement,
  groups: readonly { id: number; name: string }[],
): void {
  const select = root.querySelector<HTMLSelectElement>(`#${SOURCE_GROUP_ID}`)
  if (select === null) return
  for (const group of groups) {
    const option = document.createElement('option')
    option.value = String(group.id)
    option.textContent = group.name
    select.appendChild(option)
  }
}

/**
 * 汇总行的**展示名**：后端已给 `shop_name` 兜到组键原文；这里只补最后一层——
 * 名字与组键**都空**（未识别桶里连 `seller_id` 都没有的极端行）⇒「未识别店铺」。
 */
export function shopDisplayName(
  row: Pick<ReceiptSummaryRow, 'shop_identity' | 'shop_name'>,
): string {
  if (row.shop_name !== '') return row.shop_name
  return row.shop_identity === '' ? UNKNOWN_SHOP_LABEL : row.shop_identity
}

/** 汇总行 → 主表数据（展示名过 `shopDisplayName` 兜底；金额字符串原样）。 */
export function summaryTableData(rows: ReceiptSummaryRow[]): Array<Record<string, unknown>> {
  return rows.map((row) => ({ ...row, shop_name: shopDisplayName(row) }))
}

export default {
  mount: async (root: HTMLElement, _ctx): Promise<() => void> => {
    document.title = TITLE
    // ⚠️ 顺序是硬要求（陷阱 ③）：先挂样式，再塞标记，最后 `initIslandElements`。
    const layui: LayuiNamespace = await bootstrapIsland(['table', 'layer'])
    const table = layui.table as LayuiModule
    const layer = layui.layer as LayuiModule
    const msg = (text: string): void => islandMsg(layer as unknown as Record<string, unknown>, text)

    // ⚠️ 门禁必须排在注入标记**之前**（理由见文件头「权限门」段）。
    const me = await fetchMe()
    if (!canNode(me, XGJ_RECEIPT_NODE)) {
      msg(NO_ACCESS_MESSAGE)
      return () => {}
    }

    const defaultDay = yesterdayStr()
    // ⭐ 分组下拉的候选（字典取不到**不拦页**：只剩「全部分组」，这一项筛不了）。
    // ⚠️ 在权限门**之后**取：无节点时一个业务请求都不许发（门是 fail-closed 的）。
    const groupItems = await fetchOrderSourceGroups()
      .then((page) => page.items ?? [])
      .catch(() => [] as { id: number; name: string }[])
    root.innerHTML = buildHtml(defaultDay, defaultDay)
    initIslandElements(layui)
    fillSourceGroupOptions(root, groupItems)

    /** 当前筛选（改选日期即变）；明细区的当前店铺与页码。 */
    let range: { date_from: string; date_to: string; source_group?: number } = {
      date_from: defaultDay,
      date_to: defaultDay,
    }
    let currentShop: { shop_identity: string; shop_name: string } | null = null
    let detailPage = 1
    let detailTotal = 0

    ;(table.render as (o: unknown) => void)({
      elem: SUMMARY_TABLE_ELEM,
      id: SUMMARY_TABLE_ID,
      data: [] as ReceiptSummaryRow[],
      defaultToolbar: [],
      page: false,
      cols: [
        [
          { field: 'shop_name', title: '店铺名称', minWidth: 180 },
          { field: 'count', title: '交易成功笔数', width: 120 },
          { field: 'amount', title: '实收总金额（元）', width: 150 },
          { field: 'missing_count', title: '金额缺失笔数', width: 130 },
        ],
      ],
      text: { none: MESSAGES.summaryEmpty },
    })
    ;(table.render as (o: unknown) => void)({
      elem: DETAIL_TABLE_ELEM,
      id: DETAIL_TABLE_ID,
      data: [] as ReceiptDetailRow[],
      defaultToolbar: [],
      page: false,
      cols: [
        [
          { field: 'order_no', title: '订单号', minWidth: 170 },
          { field: 'user_name', title: '买家会员名', width: 140 },
          {
            field: 'amount',
            title: '实收金额（元）',
            width: 130,
            templet: (d: ReceiptDetailRow) => amountCell(d),
          },
          {
            field: 'success_time',
            title: '交易成功时间',
            width: 180,
            templet: (d: ReceiptDetailRow) =>
              escapeHtml((d.success_time ?? '').replace('T', ' ').slice(0, 19)),
          },
          { field: 'match_summary', title: '匹配结果', minWidth: 200 },
        ],
      ],
      text: { none: MESSAGES.detailEmpty },
    })

    const hint = (text: string): void => {
      const node = root.querySelector<HTMLElement>(`#${DETAIL_HINT_ID}`)
      if (node !== null) node.textContent = text
    }
    const detailTitleNode = (): HTMLElement | null =>
      root.querySelector<HTMLElement>(`#${DETAIL_TITLE_ID}`)
    const reloadSummary = (rows: ReceiptSummaryRow[]): void =>
      (table.reloadData as (id: string, o: unknown) => void)(SUMMARY_TABLE_ID, { data: rows })
    const reloadDetail = (rows: ReceiptDetailRow[]): void =>
      (table.reloadData as (id: string, o: unknown) => void)(DETAIL_TABLE_ID, { data: rows })

    /** 拉汇总 + 合计（唯一出口；改日期成功后走它）。 */
    async function loadSummary(): Promise<void> {
      const data = await fetchReceiptSummary(range)
      reloadSummary(data.items)
      const totalsNode = root.querySelector<HTMLElement>(`#${TOTALS_ID}`)
      if (totalsNode !== null) totalsNode.innerHTML = totalsHtml(data.totals)
      hint(
        `共 ${data.totals.shop_count} 家店铺 / ${data.totals.count} 笔 / ` +
          `${data.totals.amount} 元（缺失 ${data.totals.missing_count} 笔）`,
      )
    }

    /** 拉明细（当前店铺 + 当前页；空串 = 未识别桶，参数照样发——后端按 identity 为空筛）。 */
    async function loadDetail(): Promise<void> {
      if (currentShop === null) return
      const page = await fetchReceiptList({
        ...range,
        shop_identity: currentShop.shop_identity,
        page: detailPage,
        page_size: DETAIL_PAGE_SIZE,
      })
      detailTotal = page.total
      reloadDetail(page.items)
      const title = detailTitleNode()
      if (title !== null) {
        title.textContent =
          `${detailTitle(currentShop.shop_name)}（第 ${detailPage} 页 / 共 ${detailTotal} 笔）`
      }
    }

    /** 读两个日期输入并重查（改选即查；非法组合由后端回 `1000`，原样显示后端 msg）。 */
    async function applyRange(): Promise<void> {
      const fromNode = root.querySelector<HTMLInputElement>(`#${DATE_FROM_ID}`)
      const toNode = root.querySelector<HTMLInputElement>(`#${DATE_TO_ID}`)
      // ⚠️ **整个重建 `range`** ⇒ 每个筛选键都必须显式带进新对象（漏一个 = 改日期后那项
      //    静默失效；计划 §5 点名过这条）。分组：空值 ⇒ **不发键**（⛔ 不送空串）。
      const groupValue =
        root.querySelector<HTMLSelectElement>(`#${SOURCE_GROUP_ID}`)?.value ?? ''
      const next: { date_from: string; date_to: string; source_group?: number } = {
        date_from: fromNode?.value ?? '',
        date_to: toNode?.value ?? '',
      }
      const groupId = Number(groupValue)
      if (groupValue !== '' && Number.isFinite(groupId) && groupId > 0) {
        next.source_group = groupId
      }
      range = next
      // 区间变了 ⇒ 明细区回到未选中状态（旧店铺的明细与新口径不是一回事）。
      currentShop = null
      detailPage = 1
      const title = detailTitleNode()
      if (title !== null) title.textContent = MESSAGES.detailIdle
      try {
        await loadSummary()
      } catch (error) {
        msg(errorText(error))
      }
    }

    // ── 事件接线 ────────────────────────────────────────────────
    const onDateChange = (): void => {
      void applyRange()
    }
    root.querySelector<HTMLElement>(`#${DATE_FROM_ID}`)?.addEventListener('change', onDateChange)
    root.querySelector<HTMLElement>(`#${DATE_TO_ID}`)?.addEventListener('change', onDateChange)
    // ⭐ 分组「改选即查」（与本页日期同一条口径：不设查询按钮，R7）。
    root.querySelector<HTMLElement>(`#${SOURCE_GROUP_ID}`)?.addEventListener('change', onDateChange)

    // 点主表一行 ⇒ 选店 + 重置页码 + 拉该店明细（`table.on('row(<id>)')`）。
    ;(table.on as (event: string, cb: (obj: unknown) => void) => void)(
      `row(${SUMMARY_TABLE_ID})`,
      (obj) => {
        const row = (obj as { data: ReceiptSummaryRow }).data
        currentShop = { shop_identity: row.shop_identity, shop_name: shopDisplayName(row) }
        detailPage = 1
        void loadDetail().catch((error: unknown) => {
          msg(errorText(error))
        })
      },
    )

    const goPage = (delta: number): void => {
      const next = detailPage + delta
      if (next < 1 || (detailTotal > 0 && next > Math.ceil(detailTotal / DETAIL_PAGE_SIZE))) return
      detailPage = next
      void loadDetail().catch((error: unknown) => {
        msg(errorText(error))
      })
    }
    root.querySelector<HTMLElement>(`#${PREV_PAGE_ID}`)?.addEventListener('click', () => goPage(-1))
    root.querySelector<HTMLElement>(`#${NEXT_PAGE_ID}`)?.addEventListener('click', () => goPage(1))

    // 首屏：缺省窗口（前一天..前一天）。
    try {
      await loadSummary()
    } catch (error) {
      msg(errorText(error))
    }

    return () => {
      root.innerHTML = ''
    }
  },
} satisfies IslandPage
