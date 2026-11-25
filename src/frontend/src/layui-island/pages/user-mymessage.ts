/**
 * 【我的消息】页的 layui island（对标 `/User/mymessage`，对标 `<title>我的消息</title>`）。
 *
 * 形状：**三层页签 + 2 张表 + 1 个设置表单**——
 *   外层 tab（消息列表 / 消息设置）→ 侧边 tab（急需处理 / 重要消息 / 一般消息）
 *   → 内层 tab（未读消息 / 已读消息），两张表分别挂在内层两个页签页里；「消息设置」页签里是 2 组单选 + 1 颗按钮。
 *
 * ## 证据出处
 *
 * | 项 | 出处 |
 * |---|---|
 * | 页内 `<style>`（两段）+ body 标记 + 两个工具栏模板 | `public/replica/user-mymessage.html` ← `.tools/recon/suqing1/html/User__mymessage.html` **L24-41 + L52-199**（逐字；只删了 `<script src>`、`<template>` 包裹层与 `{{ const i18n … }}`） |
 * | 页签中文（8 处 `i18n.$t`） | `assets/JS2019/User/mymessage.js` 的 `i18nData.messages['zh-CN'].page`（**L211-234**）—— 逐字解出 |
 * | 表头 `cols`（2 张表） | 同上 **L493-551**（未读）/ **L587-643**（已读） |
 * | `table.render` 参数 | 同上 **L485-554 / L578-646** |
 * | 单元格 templet（链接/配色/title） | 同上 **L497-549 / L591-641**（逐条搬，配色 `#666`/`#bfbfbf`/`#5FB878`/`#FF5722`/`#E03636`/`#C71585`/`#DB9019` 原值） |
 * | 侧边页签徽标 `news20/news10/news0` 的显隐 | 同上 **L557-570**（`total.totalUnreadN > 0` 才 `show()`，否则 `hide()`） |
 * | `.viewMsg` 点击开层 + 顺带标记已读 | 同上 **L724-738**（`layer.open` 的 `skin/anim/shadeClose/area` 原值 + `setRead(ids)`） |
 * | 未勾选提示文案 | 同上 i18n `js.readBtn` = 「请勾选要操作的数据行」 |
 *
 * ## 本轮（接后端）改了什么
 *
 * 1. **未读表接真数据**：`GET /api/business/notifications/`（`api/notifications.ts`）。
 * 2. **标记为已读 / 全部标记为已读 / 点消息** 接真动作：`POST /api/business/notifications/{id}/read/`。
 * 3. **不再走 `artifactPage`**（它把表格写死 `data:[]`、并把所有点击收口成「尚未开放」），改为本页自己
 *    `table.render` + 自己的点击委托；证据件（`public/replica/user-mymessage.html`）仍是同一份、逐字注入。
 *
 * ## 后端支持的边界（逐条核过实现；**不要**照对标物的参数来）
 *
 * 对标发的是 `GetMyMessageList?isRead=N&msgType=20`，本项目的通知端点**一个筛选参数都不解析**
 * （`notification_views.py:53-56` 只分页；`urls.py` 只有 list + 单条 read 两条）。于是：
 *
 * | 对标的能力 | 我们的接法 |
 * |---|---|
 * | `isRead=N/Y` 两张表 | **客户端**按 `read_at == null` 切分（未读表真取数） |
 * | `msgType=0/10/20` 三档分类 | **客户端**按 `kind` 映射到三档（见下面的 ⚠️ 推断） |
 * | `SetMyMessageRead(msgId=逗号列表 / 空=全部)` | 后端只有**单条** ⇒「全部标记为已读」按**逐条调用**实现 |
 *
 * ⚠️ **分类映射是「推断」，不是需求**（照 AGENTS §5：推断必须显式标注，不得当依据引用）：
 * 对标把消息分 急需处理/重要消息/一般消息，我们的后端只有 `kind ∈ {被指派, 被抢走, 待审核}`，
 * **两边的对应关系没有任何一处被业务方定过**（`docs/` 与 spec 里搜不到）。这里按「需不需要我动手」
 * 排序（`CATEGORY_OF_KIND`）：`待审核`→急需处理（审核人不动作就卡住订单完成）、
 * `被抢走`→重要消息、`被指派`→一般消息。**待业务方确认**：改这一张表即可，其余代码不用动。
 * 为了不让「映射表没更新」把消息藏没，**未知 `kind` 在三个页签里都可见**（`matchesTab` 的 fail-open）。
 *
 * ⚠️ **已读表保持恒空**（`data: []`）：对标那张表的 `url` 本来就是**注释掉的**、`data:[]` 写死
 * （`mymessage.js:582-583`，"已读消息"页签在它自己的产品里也是空的），而我们的后端没有
 * `isRead` 筛选 —— 两边都到不了「真取已读数」这一步。故按归档口径**保持现状**（不客户端切分）。
 *
 * ⚠️ **列的数据来自哪里**（后端序列化器只有 `id/kind/order_no/payload/read_at/created_at`）：
 * `消息标题` 用 `kind` 原文（后端**没有** title 字段，不造标题文案）；`消息分类` 用映射后的档位；
 * `消息内容` 是 `payload` 的**如实渲染**（`订单号：…；来源状态：…`，没有就不写）；
 * `发送人` 后端**没有发送人字段** ⇒ 恒空（不造假人）；`是否已读`/`接收时间` 取 `read_at`/`created_at`。
 *
 * ## 要照做但别「修」的地方（复刻口径，与本轮接线无关）
 *
 *  1. **第 2 列的 `field` 也叫 `title`**（对标原文 `L507`，本意是 `msgType` 写错了）。
 *     实测对标渲染结果：该列表头「消息分类」、数据取 `res.msgType`（templet 里自己读的），
 *     所以 `field:'title'` 只是一个**不生效的占位**。照抄即可，改成 `msgType` 反而离开对标。
 *  2. **已读表 `data:[]`、`url` 被注释掉**（`L582-583`）：对标那边这张表**没有接数据源**，
 *     永远空。我们本来就恒空，故照抄 `data:[]` 语义（不传 url）。
 *  3. **`defaultToolbar: []` 与 `['filter']` 在同一个 render 对象里各写两次**（`L490/492`、`L584/586`）：
 *     JS 对象字面量**后写覆盖先写** ⇒ 生效的是 `['filter']`（表格右上角「筛选列」图标，实测对标
 *     `.layui-table-tool-self` 里确有 `title="筛选列"`）。**不要**照抄 `[]`。
 *  4. **`height:'full'`（`L487`/`L580`）**：照抄原文。官方 layui 只认 `full-<n>`，`'full'` 走到
 *     `fullSize()` 里 `parseFloat('full')` = NaN、高度回落成内容自适应——对标实测表格视图高 218，
 *     与空表一致，所以别「修正」成像素值。
 *  5. **分页是开的**（`page:true`、`limit:20`、`limits:[10,20,30,40,50]`，`L552-554`/`L644-646`）。
 *     我们改为**客户端分页**（`data` + `page`，layui 自己切页）：后端虽分页，但分类/未读要在客户端
 *     切分 —— 服务端分页 + 客户端过滤会得到「本页只有 3 行、总数却是 20」这种自相矛盾的视图。
 *
 * ## PII
 *
 * 对标是**当前登录人**的消息中心；我们的端点也按 `user=request.user` 自收窄，只可能显示本人通知。
 */
import {
  fetchAllNotifications,
  markNotificationRead,
  type Notification,
} from '@/api/notifications'
import type { ArtifactCol, ArtifactTable } from '@/layui/artifactPage'
import {
  bootstrapIsland,
  initIslandElements,
  islandMsg,
  type IslandContext,
  type IslandPage,
} from '@/layui/island'

/** 未读表列（`mymessage.js` L493-551）。**结构列（无 templet）**，运行期列由 `buildUnreadCols()` 叠加。 */
export const UNREAD_COLS: ArtifactCol[] = [
  { type: 'checkbox', fixed: 'left', width: 30 },
  { field: 'title', title: '消息标题', width: 250, align: 'left' },
  // ⚠️ 对标原文这里也写 `field:'title'`（L507）——**照抄，别「修」成 msgType**（见文件头第 1 条）。
  { field: 'title', title: '消息分类', width: 80, align: 'left' },
  { field: 'content', title: '消息内容', align: 'left' },
  { field: 'sendUserName', title: '发送人', width: 170, align: 'center' },
  { field: 'isRead', title: '是否已读', width: 80, align: 'center' },
  { field: 'createTime', title: '接收时间', width: 160, align: 'center' },
]

/** 已读表列（`mymessage.js` L587-643）：**没有勾选列**（对标原文如此）。 */
export const READ_COLS: ArtifactCol[] = [
  { field: 'title', title: '消息标题', width: 250, align: 'left' },
  { field: 'title', title: '消息分类', width: 80, align: 'left' },
  { field: 'content', title: '消息内容', align: 'left' },
  { field: 'sendUserName', title: '发送人', width: 170, align: 'center' },
  { field: 'isRead', title: '是否已读', width: 80, align: 'center' },
  { field: 'createTime', title: '接收时间', width: 160, align: 'center' },
]

export const TABLE_SPECS: ArtifactTable[] = [
    {
      elem: '#unReadTableList',
      toolbar: '#unReadtoolbarDemo',
      cols: UNREAD_COLS,
      cellMinWidth: 150,
      height: 'full',
      defaultToolbar: ['filter'],
      limit: 20,
      pageLimits: [10, 20, 30, 40, 50],
    },
    {
      elem: '#readTableList',
      toolbar: '#readToolbarDemo',
      cols: READ_COLS,
      cellMinWidth: 150,
      height: 'full',
      defaultToolbar: ['filter'],
      limit: 20,
      pageLimits: [10, 20, 30, 40, 50],
    },
]

/* ────────────────────────── 分类映射（⚠️ 推断，见文件头） ────────────────────────── */

export type Category = 'urgent' | 'important' | 'general'

export const CATEGORIES: readonly Category[] = ['urgent', 'important', 'general']

/** 三档的中文（对标 i18n `js.msgType1..3`，`mymessage.js:238-240`）。 */
export const CATEGORY_LABEL: Readonly<Record<Category, string>> = {
  urgent: '急需处理',
  important: '重要消息',
  general: '一般消息',
}

/** 三档的配色（对标 `mymessage.js:508-513` 原值）。 */
export const CATEGORY_COLOR: Readonly<Record<Category, string>> = {
  urgent: '#E03636',
  important: '#C71585',
  general: '#DB9019',
}

/** 侧边页签徽标的类名（对标模板 `user-mymessage.html` L111-113 的 `lay-id` 同值）。 */
export const CATEGORY_BADGE: Readonly<Record<Category, string>> = {
  urgent: '.news20',
  important: '.news10',
  general: '.news0',
}

/** 侧边页签 `lay-id` → 档位（对标模板里的 `lay-id="20/10/0"`）。 */
export const CATEGORY_BY_LAY_ID: Readonly<Record<string, Category>> = {
  '20': 'urgent',
  '10': 'important',
  '0': 'general',
}

/**
 * `kind` → 档位。⚠️ **推断**（理由与待确认项见文件头）。改这张表 = 改三档归属，别处无需改。
 */
export const CATEGORY_OF_KIND: Readonly<Record<string, Category>> = {
  待审核: 'urgent',
  被抢走: 'important',
  被指派: 'general',
}

/** 取档位；未知 `kind` 返回 `null`（不猜它是哪一档）。 */
export function categoryOf(kind: string): Category | null {
  return CATEGORY_OF_KIND[kind] ?? null
}

/**
 * 该 `kind` 在某个侧边页签下是否**可见**。
 *
 * ⚠️ **未知 `kind` 一律可见**（fail-open）：将来后端加第四类通知而这张映射表没跟上时，
 * 消息会出现在**每个**页签里，而不是从所有页签里消失。反向（未知 ⇒ 某一档）会让消息静默消失。
 */
export function matchesTab(kind: string, tab: Category): boolean {
  const category = categoryOf(kind)
  return category === null || category === tab
}

/* ────────────────────────── 行数据（后端字段 → 对标列） ────────────────────────── */

export type MyMessageRow = {
  id: number
  /** 后端 `kind` 原文（`被指派`/`被抢走`/`待审核`）。 */
  kind: string
  category: Category | null
  /** 消息标题：后端**没有** title 字段 ⇒ 用 `kind` 原文（见文件头）。 */
  title: string
  /** 消息内容：`payload` 的如实渲染。 */
  content: string
  /** 发送人：后端没有这个字段 ⇒ 恒空串。 */
  sendUserName: string
  /** 对标 `isRead` 的取值域是 `'Y' | 'N'`。 */
  isRead: 'Y' | 'N'
  createTime: string
}

/** 未读 = `read_at` 为 `null`（**不是**真值判断；`read_at` 是时间戳串）。 */
export function isUnread(n: Pick<Notification, 'read_at'>): boolean {
  return n.read_at === null
}

/**
 * `created_at`（DRF 回的 ISO 8601，带时区偏移）→ 本地 `YYYY-MM-DD HH:mm:ss`。
 *
 * 解析不了就原样返回：宁可显示原文，也不要因为格式怪就抛错把整张表带崩。
 */
export function formatTime(value: string): string {
  const at = new Date(value)
  if (Number.isNaN(at.getTime())) return value
  const pad = (n: number): string => String(n).padStart(2, '0')
  return (
    `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())} ` +
    `${pad(at.getHours())}:${pad(at.getMinutes())}:${pad(at.getSeconds())}`
  )
}

/**
 * `payload` → 消息内容文案。**只用真实字段**（`order_no` / `from_status`），没有就不写。
 * 对标那句内容是服务端下发的整句；我们没有那句话，所以这里渲染的是载荷本身（见文件头）。
 */
export function payloadText(
  payload: Notification['payload'],
  fallbackOrderNo: string | null,
  // ⭐ D-0301：调用方把行上的 `source_order_no`（实时读 FK）传进来当第二兜底。
  fallbackSourceOrderNo?: string | null,
): string {
  const parts: string[] = []
  // ⭐ D-0301：展示值 = 来源号优先（payload 键 → 行上实时读的 `source_order_no`
  // 兜底 → `order_no`）——来源号非空取它、空回退内置号。
  const sourceNo =
    typeof payload.source_order_no === 'string' && payload.source_order_no
      ? payload.source_order_no
      : (fallbackSourceOrderNo ?? '')
  const orderNo = sourceNo || (typeof payload.order_no === 'string' && payload.order_no ? payload.order_no : fallbackOrderNo)
  if (orderNo) parts.push(`订单号：${orderNo}`)
  const from = payload.from_status
  if (typeof from === 'string' && from) parts.push(`来源状态：${from}`)
  return parts.join('；')
}

export function toRow(n: Notification): MyMessageRow {
  return {
    id: n.id,
    kind: n.kind,
    category: categoryOf(n.kind),
    title: n.kind,
    content: payloadText(n.payload ?? {}, n.order_no, n.source_order_no),
    sendUserName: '',
    isRead: isUnread(n) ? 'N' : 'Y',
    createTime: formatTime(n.created_at),
  }
}

/* ────────────────────────── 单元格 templet（逐条搬对标） ────────────────────────── */

/** 属性/文本转义。对标的 templet 直接拼串；我们拼的是后端字段，转义是正确性措施（不改任何视觉值）。 */
function esc(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/** 对标 `L498-500`：未读 `#666`、已读 `#bfbfbf`。 */
function cellColor(d: MyMessageRow): string {
  return d.isRead === 'Y' ? '#bfbfbf' : '#666'
}

/** `viewMsg` 链接：文案 + `data-id/title/content` 三个属性（对标 `L502`/`L524`，`.viewMsg` 点击开层）。 */
function viewLink(d: MyMessageRow, text: string, extraStyle = ''): string {
  return (
    `<a href="javascript:void(0);" class="viewMsg" title="${esc(text)}" style="color:${cellColor(d)}${extraStyle}"` +
    ` data-id="${d.id}" data-content="${esc(d.content)}" data-title="${esc(d.title)}">${esc(text)}</a>`
  )
}

const titleCell = (d: MyMessageRow): string => viewLink(d, d.title)

/** 对标 `L507-516`：`msgType` 上色 + 中文档位。未知 `kind` 如实显示原文（不硬塞进某一档）。 */
const categoryCell = (d: MyMessageRow): string => {
  if (d.category === null) return `<span style="color:#DB9019">${esc(d.kind)}</span>`
  return `<span style="color:${CATEGORY_COLOR[d.category]}">${CATEGORY_LABEL[d.category]}</span>`
}

const contentCell = (d: MyMessageRow): string => viewLink(d, d.content)

/** 对标 `L529-532` 渲染 `sendUserName`；我们的后端没有该字段 ⇒ 恒空（见文件头）。 */
const senderCell = (): string => ''

/** 对标 `L535-543`：已读 `#FF5722`、未读 `#5FB878`。 */
const readCell = (d: MyMessageRow): string =>
  d.isRead === 'Y'
    ? '<a href="javascript:void(0);" style="color:#FF5722" title="已读">已读</a>'
    : '<a href="javascript:void(0);" style="color:#5FB878" title="未读">未读</a>'

/** 对标 `L546-548`：`style=""`、`title="发自：<时间>"`。 */
const timeCell = (d: MyMessageRow): string =>
  `<a href="javascript:void(0);" style="" title="发自：${esc(d.createTime)}">${esc(d.createTime)}</a>`

const UNREAD_TEMPLETS: readonly (CellTemplet | null)[] = [
  null, // 勾选列
  titleCell,
  categoryCell,
  contentCell,
  senderCell,
  readCell,
  timeCell,
]

const READ_TEMPLETS: readonly (CellTemplet | null)[] = [
  titleCell,
  categoryCell,
  contentCell,
  senderCell,
  readCell,
  timeCell,
]

type CellTemplet = (d: MyMessageRow) => string

/** 运行期列 = 结构列（`UNREAD_COLS` 的顺序/文案/宽度，被 `user-replica.spec.ts` 钉住）+ 逐列 templet。 */
export type MyMessageCol = Omit<ArtifactCol, 'templet'> & { templet?: CellTemplet }

export function withTemplets(
  cols: ArtifactCol[],
  temples: readonly (CellTemplet | null)[],
): MyMessageCol[] {
  return cols.map((col, i) => {
    const templet = temples[i]
    // `ArtifactCol.templet` 只允许**字符串**选择器；运行期列一律用**函数** templet，
    // 所以这里把结构列上那个键显式剔除（`UNREAD_COLS`/`READ_COLS` 本来也没写它，
    // 剔除是为了让「两种形状不会同时出现」成为类型上的事实而不是约定）。
    const base = Object.fromEntries(
      Object.entries(col).filter(([key]) => key !== 'templet'),
    ) as Omit<ArtifactCol, 'templet'>
    return templet ? { ...base, templet } : base
  })
}

export function buildUnreadCols(): MyMessageCol[] {
  return withTemplets(UNREAD_COLS, UNREAD_TEMPLETS)
}

export function buildReadCols(): MyMessageCol[] {
  return withTemplets(READ_COLS, READ_TEMPLETS)
}

/* ────────────────────────── 徽标 / 页签（纯函数，便于单测） ────────────────────────── */

/**
 * 侧边三个徽标按**未读数**显隐（对标 `mymessage.js:557-570`：`>0` 才 `show()`，否则 `hide()`）。
 *
 * 未读数由**已加载的通知**在客户端数出来（后端不提供 `totalUnreadN`）——所以它只覆盖
 * `fetchAllNotifications()` 取回的那一批（即全部，见该函数的翻页）。
 */
export function updateBadges(root: ParentNode, rows: MyMessageRow[]): void {
  for (const category of CATEGORIES) {
    const el = root.querySelector<HTMLElement>(CATEGORY_BADGE[category])
    if (!el) continue
    const unread = rows.filter((r) => r.isRead === 'N' && r.category === category).length
    // jQuery 的 `show()`/`hide()` 等价物：清掉行内 display（回落到 CSS 的 inline-block）/ 置 none。
    el.style.display = unread > 0 ? '' : 'none'
    el.textContent = unread > 0 ? String(unread) : ''
  }
}

/** 当前侧边页签的档位（读 `layui-this`；读不到按对标的默认页签「急需处理」= `lay-id="20"`）。 */
export function activeTabOf(root: ParentNode): Category {
  const li = root.querySelector('.sidetab > .layui-tab-title > li.layui-this')
  return CATEGORY_BY_LAY_ID[li?.getAttribute('lay-id') ?? '20'] ?? 'urgent'
}

/** 未读表在当前页签下要显示的行（未读 + 档位匹配）。 */
export function unreadRowsFor(rows: MyMessageRow[], tab: Category): MyMessageRow[] {
  return rows.filter((r) => r.isRead === 'N' && matchesTab(r.kind, tab))
}

/* ────────────────────────── 挂载 ────────────────────────── */

/** layui 自己的 `defaultToolbar` 默认值（同 `artifactPage` 的兜底，来自 vendored layui 的 config）。 */
const DEFAULT_TOOLBAR = ['filter', 'exports', 'print']
const DEFAULT_LIMIT = 10
const DEFAULT_LIMITS = [10, 20, 30, 40, 50, 60, 70, 80, 90]
const ARTIFACT_URL = 'replica/user-mymessage.html'
/** 对标 `layer.open` 的参数原值（`mymessage.js:730-736`）。 */
const VIEW_LAYER = { skin: 'layui-layer-lan', closeBtn: 1, anim: 2, shadeClose: true, area: ['80%', '400px'] }
/** 对标未勾选时的提示（i18n `js.readBtn`）。 */
const NO_SELECTION_MSG = '请勾选要操作的数据行'

type LayuiTable = {
  render: (o: unknown) => void
  on: (event: string, cb: (obj: { event?: string; config?: { id?: string } }) => void) => void
  checkStatus: (id: string) => { data?: MyMessageRow[] }
  reload: (id: string, o?: unknown) => void
}

export async function mountMyMessage(root: HTMLElement): Promise<() => void> {
  const layui = await bootstrapIsland(['form', 'table', 'layer'])
  const form = layui.form as unknown as { render: (t?: string) => void }
  const table = layui.table as unknown as LayuiTable & Record<string, unknown>
  const layer = layui.layer as Record<string, unknown>

  // 证据件是静态 HTML（`public/replica/`），取回后整段注入（与 `artifactPage` 同一手法与理由）。
  const res = await fetch(`${import.meta.env.BASE_URL}${ARTIFACT_URL}`)
  if (!res.ok) throw new Error(`证据件取不到（HTTP ${res.status}）：${ARTIFACT_URL}`)
  root.innerHTML = await res.text()
  // 页签/折叠面板要等标记进来之后再绑定（`initIslandElements` 自己接管页签点击）。
  initIslandElements(layui)

  const msg = (text: string): void => islandMsg(layer, text)
  const errorText = (e: unknown): string => (e instanceof Error && e.message ? e.message : '操作失败')

  let rows: MyMessageRow[] = []

  const renderTable = (spec: ArtifactTable, cols: MyMessageCol[], data: MyMessageRow[]): void => {
    table.render({
      elem: spec.elem,
      data,
      cols: [cols],
      ...(spec.toolbar ? { toolbar: spec.toolbar } : {}),
      defaultToolbar: spec.defaultToolbar ?? DEFAULT_TOOLBAR,
      page: { limit: spec.limit ?? DEFAULT_LIMIT, limits: spec.pageLimits ?? DEFAULT_LIMITS },
      ...(spec.cellMinWidth !== undefined ? { cellMinWidth: spec.cellMinWidth } : {}),
      ...(spec.height !== undefined ? { height: spec.height } : {}),
      text: { none: '暂无数据' },
      done: () => form.render(),
    })
  }

  const renderUnread = (): void => {
    renderTable(TABLE_SPECS[0]!, buildUnreadCols(), unreadRowsFor(rows, activeTabOf(root)))
  }

  const renderRead = (): void => {
    // 对标那张已读表 `data:[]`、`url` 被注释（`mymessage.js:582-583`），后端也没有 isRead 筛选
    // ⇒ 保持恒空（见文件头）。这里仍照常 render 一次，保持两层表格结构与对标一致。
    renderTable(TABLE_SPECS[1]!, buildReadCols(), [])
  }

  /** 取数 → 重画两张表 + 三个未读徽标。失败**如实提示后端 msg**，并把表格清空（不留假数据）。 */
  async function refresh(): Promise<void> {
    let list: Notification[]
    try {
      list = await fetchAllNotifications()
    } catch (e) {
      rows = []
      renderUnread()
      renderRead()
      updateBadges(root, rows)
      msg(errorText(e))
      return
    }
    rows = list.map(toRow)
    renderUnread()
    renderRead()
    updateBadges(root, rows)
  }

  /**
   * 逐条标记已读。后端**只有单条**端点（文件头条表）：`Promise.allSettled` 让「第 N 条失败」
   * 不影响其余条目，然后**无论成败都重取一次** —— 表格显示的是服务端真实状态，而不是我们猜的。
   */
  async function markRead(ids: number[]): Promise<void> {
    if (ids.length === 0) return
    const results = await Promise.allSettled(ids.map((id) => markNotificationRead(id)))
    const failed = results.find((r): r is PromiseRejectedResult => r.status === 'rejected')
    if (failed) msg(errorText(failed.reason))
    await refresh()
  }

  const checkedRows = (): MyMessageRow[] =>
    table.checkStatus('unReadTableList').data ?? []

  // 工具栏：未读表（刷新 / 标记为已读 / 全部标记为已读）+ 已读表（刷新）。
  // ⚠️ 用 layui 自己的 `toolbar(<lay-filter>)` 事件，**不要**在这里另挂捕获阶段的点击委托：
  //    layui 的表格模块对 `.layui-table-tool *[lay-event]` 自己做了委托并 `layui.stope(e)`。
  table.on('toolbar(unReadTableList)', (obj) => {
    switch (obj.event) {
      case 'refresh':
        void refresh()
        break
      case 'readBtn': {
        const checked = checkedRows()
        if (checked.length === 0) {
          msg(NO_SELECTION_MSG)
          return
        }
        void markRead(checked.map((r) => r.id))
        break
      }
      case 'readAll':
        // 「全部」= 当前已加载的**所有未读**（不分页签档位）——对标 `setRead("")` 也是全量口径。
        void markRead(rows.filter((r) => r.isRead === 'N').map((r) => r.id))
        break
      default:
        break
    }
  })
  table.on('toolbar(readTableList)', (obj) => {
    if (obj.event === 'refresh') void refresh()
  })

  // 点消息（标题/内容链）：开层显示内容 **并** 标记该条已读（对标 `mymessage.js:724-738`）。
  const onViewMsgClick = (event: Event): void => {
    const el = (event.target as HTMLElement).closest<HTMLElement>('.viewMsg')
    if (!el) return
    const id = Number(el.dataset.id ?? 0)
    const content = el.dataset.content ?? ''
    ;(layer.open as (o: unknown) => void)({
      title: el.dataset.title ?? '',
      type: 1,
      ...VIEW_LAYER,
      content: `<div style="margin:10px 10px 10px 10px;font-size:16px">${esc(content)}</div>`,
    })
    if (id > 0) void markRead([id])
  }
  root.addEventListener('click', onViewMsgClick)

  /**
   * 「消息设置」页签里的两组单选与「一键重置」没有后端（对标打的是
   * `/UserAjax/SaveUserConfigForMsgSound*` / `…ResetPushWechatMsgOrder`，本项目没有这三条），
   * 按 D-0025 §5 提示「尚未开放」，**不置灰**。
   *
   * 捕获阶段 + `stopPropagation`：与 `artifactPage` 同一手法——先于 layui 自己的委托，
   * 因此点单选**不会**把 UI 切过去（切了就是「看起来生效了，其实没存」）。
   */
  const NOT_OPEN_SELECTOR = '.layui-form-radio, .resetPushWechatMsgOrder'
  const onNotOpenClick = (event: Event): void => {
    const el = (event.target as HTMLElement).closest<HTMLElement>(NOT_OPEN_SELECTOR)
    if (!el) return
    event.preventDefault()
    event.stopPropagation()
    const label =
      el.closest('.layui-form-item')?.querySelector('.layui-form-label')?.textContent?.trim() ||
      el.textContent?.trim() ||
      '该功能'
    msg(`「${label}」尚未开放`)
  }
  root.addEventListener('click', onNotOpenClick, true)

  /**
   * 页签切换后按**新页签**重取/重画。
   *
   * 必须挂在 `document` 捕获阶段，并且在 `initIslandElements()` 之后注册：
   * 页签处理器先把 `layui-this` 切好，这里读到的才是切换后的状态。
   * 页签标题不命中本页 root 上的点击选择器，所以点页签不会误触「尚未开放」。
   *
   * 侧边 tab（分类）→ 重取（数据按当前页签取）；内层 tab 切到「未读」→ 重取；切到「已读」则
   * 什么都不发（那张表本来就没有数据源，见文件头）。
   */
  const onTabClick = (event: Event): void => {
    const li = (event.target as HTMLElement | null)?.closest?.('.layui-tab-title > li')
    const tab = li?.parentElement?.parentElement
    if (!tab) return
    if (tab.classList.contains('sidetab')) {
      // ⚠️ 显式把唯一的面板显示回来。**这一句现在是冗余兜底**：`initIslandElements` 的 fail-safe
      // （索引越界时**什么都不做**）已经覆盖「3 个标题 / 1 个内容面板」这种情况，layui 自己的
      // 冒泡委托在 `idx` 越界时也落在空集上（`.eq(idx)`）。保留它，是因为它零成本、且不依赖
      // 共享渲染器的内部行为。对标的实测渲染与之一致（suqing1 `/User/mymessage`：点
      // 「重要消息/一般消息」后 `.sidetab > .layui-tab-content > .layui-tab-item` 仍带
      // `layui-show` 且可见）。
      tab
        .querySelector(':scope > .layui-tab-content > .layui-tab-item')
        ?.classList.add('layui-show')
      void refresh()
      return
    }
    if (tab.getAttribute('lay-filter') === 'threetab' && li?.getAttribute('lay-id') === 'N') {
      void refresh()
    }
  }
  document.addEventListener('click', onTabClick, true)

  await refresh()

  return () => {
    root.removeEventListener('click', onViewMsgClick)
    root.removeEventListener('click', onNotOpenClick, true)
    document.removeEventListener('click', onTabClick, true)
    root.innerHTML = ''
  }
}

const page: IslandPage = {
  mount: async (root: HTMLElement, _ctx: IslandContext) => {
    document.title = '我的消息'
    return mountMyMessage(root)
  },
}

export default page
