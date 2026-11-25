<script setup lang="ts">
import { orderStatusClass } from '@/v2/orderStatusAppearance'
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「找回删除的订单」（回收站）v2（`/v2/order-recycle`；路由 name `v2-order-recycle`）。
 *
 * 旧页 = island 复刻页 `order-orderrecycle.ts`（路由 `orders-recycle`）。12 列 /
 * 统计文案「【统计】共N笔被删除的订单」/ 批量还原一次一笔口径逐条照旧页；分页档 [10,20,30]
 * 默认 30（后端 `RecycleBinSearchSerializer.page_size` 白名单只认 10/20/30）。
 * 布局骨架照 `V2OrderManage.vue`（筛选 → 工具条 → 表格面板）。两处有意不同
 * （DRAFT 登记）：还原**确认弹窗为 v2 新增**（v1 点了直接还原），成功后 toast 是 v2 自绘
 * 反馈的设计选择（v1 成功不弹）。恢复语义出处 = `src/apps/business/recycle.py::restore_order`
 * （幂等；删除时在厅的单恢复后回「未分配」需手工重抛；绩效行不随订单恢复；不回拆单组）。
 * ⛔ 不 import island 页模块；权限门 `session.canNode('order.recycle')`（fail-closed，没节点不画）。
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { restoreOrder, type RecycleBinRow } from '@/api/orders'
import { fetchAllGames, fetchServers, type ConfigRow, type ServerRow } from '@/api/config'
import { fetchOrderSources, type OrderSourceRow } from '@/api/orderSources'
import {
  ORDER_STATUS_VALUES,
  V2_ORDER_STATUS_LABELS,
  v2OrderStatusLabel,
} from '@/config/orderStatus'
import { useSessionStore } from '@/stores/session'
import { fetchRecycleBinPage } from '@/v2/orderRecycleApi'

/* ── 文案（出处 = island 页同语义常量，本地重写不引模块）────────────────────── */

/** 一笔都没勾（**逐字**取对标 `orderrecycle.js:197`）。 */
const RECYCLE_ROW_SELECT_TEXT = '请选中要操作的记录'
/** 勾 >1 笔：批量收窄成一次一笔（照 v1 页面约定，零请求）。 */
const RECYCLE_BATCH_ONE_TEXT = '批量操作尚未开放，请只勾选一笔订单'
/** 已删单看不了详情：订单号格 title 的去向说明（同 v1 口径，不做详情链接）。 */
const RECYCLE_DETAIL_NOT_OPEN_TEXT = '已删除的订单不能查看详情'

// 档位必须在后端 page_size 白名单内（`RecycleBinSearchSerializer` choices=(10,20,30)）；
// 默认取白名单最大档 30（50/100 会被后端拒：「"50" 不是合法选项」）。
const PAGE_LIMITS = [10, 20, 30]
// el-option 的 value prop 类型不含 null；运行时仍以 null 表示「全部」（与 filters.gameId 等同值比较）。
const NULL_OPTION_VALUE = null as unknown as number

function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

/* ── 提示（页面内自绘 toast，同 V2OrderManage；ElMessage 拿不到 .ttv2 皮）──────── */

const toast = ref<{ text: string; kind: 'info' | 'error' } | null>(null)
let toastTimer: ReturnType<typeof setTimeout> | null = null

function showToast(text: string, kind: 'info' | 'error' = 'info'): void {
  if (toastTimer !== null) clearTimeout(toastTimer)
  toast.value = { text, kind }
  toastTimer = setTimeout(() => {
    toast.value = null
    toastTimer = null
  }, kind === 'error' ? 6000 : 1500)
}

/* ── 权限门（还原钮：行内 + 工具条，fail-closed；后端兜底 order_views.py）───────── */

const session = useSessionStore()
const canRestore = computed(() => session.canNode(['order.recycle']))

/* ── 字典：游戏 / 区服 / 订单来源（字典失败也照常渲染：下拉为空、区服列显示空串）── */

const games = ref<ConfigRow[]>([])
const servers = ref<ServerRow[]>([])
const sources = ref<OrderSourceRow[]>([])

/** 区服 id → 名（fetchServers 失败 = 空 Map 兜底，照 v1 mount 的字典降级：该列显示空串）。 */
const serverMap = computed(() => {
  const map = new Map<number, string>()
  for (const s of servers.value) map.set(s.id, s.name)
  return map
})

/** 普通函数（脚本里 infoBodyOf/infoTitleOf 也要调；computed 返回的函数在 script 不会解包）。 */
function serverNameOf(id: number | null): string {
  return id === null ? '' : (serverMap.value.get(id) ?? '')
}

/* ── 筛选 ─────────────────────────────────────────────────────────────── */

type RecycleFilters = {
  gameId: number | null
  status: string
  deletedFrom: string
  deletedTo: string
  sourceId: number | null
  key1: string
}

/**
 * 默认删除时间区间：今天 −2 个自然月 `00:00:00` ~ 今天 `23:59:59`（本地时区）。
 * ⚠️ 偏移量在 v1 就是「推断」级证据（只有一张快照可反推）；⛔ 不 import island 的
 * dateRange——各页偏移不许互相照抄。日要先夹到目标月月末，否则 1/31 会溢出成 12/1。
 */
function defaultDeletedRange(now: Date = new Date()): [string, string] {
  const pad = (n: number): string => String(n).padStart(2, '0')
  const ymd = (d: Date): string => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
  const target = new Date(now.getFullYear(), now.getMonth() - 2, 1)
  const lastDay = new Date(target.getFullYear(), target.getMonth() + 1, 0).getDate()
  target.setDate(Math.min(now.getDate(), lastDay))
  return [`${ymd(target)} 00:00:00`, `${ymd(now)} 23:59:59`]
}

const [defaultFrom, defaultTo] = defaultDeletedRange()

const filters = reactive<RecycleFilters>({
  gameId: null,
  status: '',
  deletedFrom: defaultFrom,
  deletedTo: defaultTo,
  sourceId: null,
  key1: '',
})

function setDeletedFrom(v: unknown): void {
  filters.deletedFrom = typeof v === 'string' ? v : ''
}

function setDeletedTo(v: unknown): void {
  filters.deletedTo = typeof v === 'string' ? v : ''
}

/* ── 取数（服务端分页；统计句 = 服务端 total）────────────────────────────────── */

const rows = ref<RecycleBinRow[]>([])
const total = ref(0)
const loading = ref(false)
const loadError = ref<string | null>(null)
const page = ref(1)
const pageSize = ref(30)

async function reload(targetPage = 1): Promise<void> {
  loading.value = true
  try {
    const result = await fetchRecycleBinPage({
      game: filters.gameId,
      status: filters.status,
      deleted_from: filters.deletedFrom,
      deleted_to: filters.deletedTo,
      order_source: filters.sourceId,
      keyword: filters.key1,
      page: targetPage,
      page_size: pageSize.value,
    })
    rows.value = result.items
    total.value = result.total
    page.value = result.page
    loadError.value = null
  } catch (e) {
    rows.value = []
    total.value = 0
    loadError.value = errText(e)
    showToast(loadError.value, 'error')
  } finally {
    loading.value = false
  }
}

/** 行数变化后当前页可能越界（服务端翻页不会自己夹）。 */
watch(total, (t) => {
  const last = Math.max(1, Math.ceil(t / pageSize.value))
  if (page.value > last) void reload(last)
})

function onPageChange(p: number): void {
  void reload(p)
}

function onPageSizeChange(event: Event): void {
  const size = Number((event.target as HTMLSelectElement).value)
  if (Number.isFinite(size) && size > 0) {
    pageSize.value = size
    void reload(1)
  }
}

function onSearch(): void {
  void reload(1)
}

function resetFilters(): void {
  filters.gameId = null
  filters.status = ''
  filters.deletedFrom = defaultFrom
  filters.deletedTo = defaultTo
  filters.sourceId = null
  filters.key1 = ''
  void reload(1)
}

/* ── 勾选（工具条「还原勾选的订单」只吃勾选的行；表头勾 = 当前页全部）───────────── */

const checkedNos = ref<string[]>([])

function isChecked(no: string): boolean {
  return checkedNos.value.includes(no)
}

function toggleRow(no: string): void {
  const i = checkedNos.value.indexOf(no)
  if (i === -1) checkedNos.value.push(no)
  else checkedNos.value.splice(i, 1)
}

const pageAllChecked = computed(
  () => rows.value.length > 0 && rows.value.every((r) => isChecked(r.order_no)),
)

function togglePageAll(): void {
  if (pageAllChecked.value) {
    checkedNos.value = checkedNos.value.filter((no) => !rows.value.some((r) => r.order_no === no))
  } else {
    for (const r of rows.value) {
      if (!isChecked(r.order_no)) checkedNos.value.push(r.order_no)
    }
  }
}

const checkedRows = computed(() => rows.value.filter((r) => isChecked(r.order_no)))

/* ── 列显隐（localStorage 键 tt.v2.order-recycle.hidden-cols.v1；check 列不进选择器）── */

const RECYCLE_COLUMNS = [
  'check', 'order', 'role', 'group', 'project', 'info', 'status', 'remark', 'deluser', 'deltime', 'ww', 'actions',
] as const

const COL_TITLES: Record<string, string> = {
  order: '订单号',
  role: '角色名',
  group: '阵营/平台',
  project: '代练/陪玩项目',
  info: '角色信息',
  status: '订单状态',
  remark: '重要备注',
  deluser: '删除人',
  deltime: '删除时间',
  ww: '旺旺/来源',
  actions: '操作',
}

const HIDDEN_COLS_KEY = 'tt.v2.order-recycle.hidden-cols.v1'

/** 默认全显（v1 的 12 列没有隐藏档；键带 .v1 = 默认集将来变了就升版作废旧存储）。 */
const HIDDEN_COLS_DEFAULT: string[] = []

function loadHiddenCols(): string[] {
  try {
    const raw = localStorage.getItem(HIDDEN_COLS_KEY)
    if (raw === null) return [...HIDDEN_COLS_DEFAULT]
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return [...HIDDEN_COLS_DEFAULT]
    // 只认合法列键（存储可能被手改）；操作列不允许藏（还原的入口没了就没有恢复入口）。
    return parsed.filter(
      (k): k is string => typeof k === 'string' && k in COL_TITLES && k !== 'actions',
    )
  } catch {
    return [...HIDDEN_COLS_DEFAULT]
  }
}

const hiddenCols = ref<string[]>(loadHiddenCols())

function saveHiddenCols(): void {
  try {
    localStorage.setItem(HIDDEN_COLS_KEY, JSON.stringify(hiddenCols.value))
  } catch { /* 存储禁写：本次会话仍生效 */ }
}

function colVisible(key: string): boolean {
  return !hiddenCols.value.includes(key)
}

/** 显隐生效后的列序列（空行 colspan 用）。 */
const visibleColumns = computed(() => RECYCLE_COLUMNS.filter((k) => colVisible(k)))

/** 列显隐 = el-checkbox-group：v-model = 可见列键，set 时换算回 hiddenCols。 */
const visibleColKeys = computed<string[]>({
  get: () => (Object.keys(COL_TITLES) as string[]).filter((k) => colVisible(k)),
  set: (keys) => {
    hiddenCols.value = (Object.keys(COL_TITLES) as string[]).filter((k) => !keys.includes(k))
    saveHiddenCols()
  },
})

/* ── 单元格展示（island buildRecycleRow 的同口径重写）────────────────────────── */

function viewNoOf(d: RecycleBinRow): string {
  // ⭐ D-0301：展示值 = source_order_no || order_no；寻址键照旧内置号。
  return d.source_order_no || d.order_no
}

function timeText(iso: string): string {
  return iso.replace('T', ' ').slice(0, 16)
}

/** 角色信息 = `/区服名/角色名/游戏账号`（缺段留空；leading `/` 是 v1 照对标 gameArea
 * 缺位代入空串的结果，照抄它的表达式形状）。 */
function infoBodyOf(d: RecycleBinRow): string {
  return `/${serverNameOf(d.server)}/${d.game_role_name}/${d.game_account ?? ''}`
}

/** 对标 title 比 body 多一段 levelMark（我方恒空）⇒ 中间双斜杠那段。 */
function infoTitleOf(d: RecycleBinRow): string {
  return `/${serverNameOf(d.server)}/${d.game_role_name}//${d.game_account ?? ''}`
}

function statusText(status: string): string {
  return v2OrderStatusLabel(status)
}

/** 旺旺/来源三支照 v1：有旺旺显旺旺；含微信/QQ 且有 qq 显 qq（载荷今天没有 customer_qq
 * ⇒ 该支落空，键一出现即生效）；否则显来源名。 */
function wwTextOf(d: RecycleBinRow): string {
  if (d.customer_ww !== '') return d.customer_ww
  const qq = d.customer_qq ?? ''
  if (qq !== '' && (d.source.includes('微信') || d.source.includes('QQ'))) return qq
  return d.source
}

/* ── 还原（一次一笔口径：行内 / 工具条共用同一个确认弹窗；v2 新增确认步）────────── */

const restoreDlg = ref<{ orderNo: string; viewNo: string } | null>(null)
const restoring = ref(false)

function openRestoreDlg(orderNo: string, viewNo: string): void {
  restoreDlg.value = { orderNo, viewNo }
}

function openRowRestore(d: RecycleBinRow): void {
  openRestoreDlg(d.order_no, viewNoOf(d))
}

function onToolbarRestore(): void {
  const picked = checkedRows.value
  if (picked.length === 0) {
    showToast(RECYCLE_ROW_SELECT_TEXT)
    return
  }
  // 勾 >1 只提示、零请求（照 v1 一次一笔口径）。
  if (picked.length > 1) {
    showToast(RECYCLE_BATCH_ONE_TEXT)
    return
  }
  openRestoreDlg(picked[0]!.order_no, viewNoOf(picked[0]!))
}

async function doRestore(): Promise<void> {
  const dlg = restoreDlg.value
  if (dlg === null || restoring.value) return
  restoring.value = true
  try {
    await restoreOrder(dlg.orderNo)
    checkedNos.value = checkedNos.value.filter((no) => no !== dlg.orderNo)
    restoreDlg.value = null
    showToast('还原成功')
    await reload(page.value)
  } catch (e) {
    // 失败原样后端中文（信封 msg 经 BizError 抛出）；弹窗留着可重试。
    showToast(errText(e), 'error')
  } finally {
    restoring.value = false
  }
}

/* ── 其它出口 ─────────────────────────────────────────────────────────────── */

onMounted(() => {
  void (async () => {
    try {
      const [gameRows, serverRows] = await Promise.all([fetchAllGames(), fetchServers()])
      games.value = gameRows
      servers.value = serverRows
    } catch { /* 字典取不到：下拉为空、区服列显示空串，列表照常（照 v1 mount 的降级） */ }
    try {
      sources.value = (await fetchOrderSources()).items
    } catch { sources.value = [] }
    await reload(1)
  })()
})
</script>

<template>
  <div class="cv2-page" data-v2-page="order-recycle">
    <!-- 筛选区使用连体网格组。 -->
    <section class="cv2-panel cv2-filter" aria-label="筛选" data-v2="filters">
      <div class="flt-wrap">
        <div class="flt-group" data-v2-filter-group="game">
          <span class="flt-label">游戏</span>
          <span class="flt-cell"><el-select v-model="filters.gameId" filterable placeholder="全部游戏" class="flt-w150" :class="{ 'flt-ph': filters.gameId === null }" data-v2-filter="game" :teleported="false">
            <el-option :value="NULL_OPTION_VALUE" label="全部游戏" />
            <el-option v-for="g in games" :key="g.id" :value="g.id" :label="g.name" />
          </el-select></span>
        </div>
        <div class="flt-group" data-v2-filter-group="status">
          <span class="flt-label">订单状态</span>
          <span class="flt-cell"><el-select v-model="filters.status" placeholder="全部" class="flt-w150" :class="{ 'flt-ph': filters.status === '' }" data-v2-filter="status" :teleported="false">
            <el-option value="" label="全部" />
            <el-option v-for="s in ORDER_STATUS_VALUES" :key="s" :value="s" :label="V2_ORDER_STATUS_LABELS[s]" />
          </el-select></span>
        </div>
        <div class="flt-group" data-v2-filter-group="deltime">
          <span class="flt-label">删除时间</span>
          <span class="flt-cell flt-datetimecell" data-v2-filter="deleted-from"><el-date-picker
            :model-value="filters.deletedFrom"
            type="datetime"
            value-format="YYYY-MM-DD HH:mm:ss"
            placeholder="开始时间"
            :teleported="false"
            @update:model-value="setDeletedFrom"
          /></span>
          <span class="flt-dash">–</span>
          <span class="flt-cell flt-datetimecell" data-v2-filter="deleted-to"><el-date-picker
            :model-value="filters.deletedTo"
            type="datetime"
            value-format="YYYY-MM-DD HH:mm:ss"
            placeholder="结束时间"
            :teleported="false"
            @update:model-value="setDeletedTo"
          /></span>
        </div>
        <div class="flt-group" data-v2-filter-group="source">
          <span class="flt-label">订单来源</span>
          <span class="flt-cell"><el-select v-model="filters.sourceId" filterable placeholder="全部来源" class="flt-w150" :class="{ 'flt-ph': filters.sourceId === null }" data-v2-filter="source" :teleported="false">
            <el-option :value="NULL_OPTION_VALUE" label="全部来源" />
            <el-option v-for="s in sources" :key="s.id" :value="s.id" :label="s.name" />
          </el-select></span>
        </div>
        <div class="flt-group flt-grow" data-v2-filter-group="keyword">
          <span class="flt-cell flt-cell--grow"><el-input
            v-model="filters.key1"
            placeholder="订单号/角色名/手机号"
            data-v2-filter="key1"
            @keydown.enter="onSearch"
          /></span>
        </div>
        <div class="cv2-filter-actions">
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--lg cv2-btn--find cv2-btn--fill" data-v2="search" @click="onSearch">查询</button>
          <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--lg cv2-btn--quiet" data-v2="reset" @click="resetFilters">重置</button>
        </div>
      </div>
    </section>

    <!-- 工具条：还原钮 fail-closed（没 order.recycle 不画）；列显隐照管理订单页形态 -->
    <div class="cv2-toolbar" data-v2="toolbar">
      <div class="ttb-strip">
      <button type="button" class="cv2-btn cv2-btn--primary" data-v2="refresh" :disabled="loading" @click="reload(page)">刷新</button>
      <button v-if="canRestore" type="button" class="cv2-btn cv2-btn--primary" data-v2="restore-checked" @click="onToolbarRestore">还原勾选的订单</button>
      </div>
      <p class="cv2-total-line" data-v2="total">【统计】共{{ total }}笔被删除的订单</p>
      <span class="cv2-toolbar-count" data-v2="checked-count">已选 {{ checkedRows.length }} 行</span>
      <div class="cv2-colpick" data-v2="colpick">
        <el-popover trigger="click" :teleported="false" :show-arrow="false" :width="188" popper-class="cv2-colpick-pop">
          <template #reference>
            <button
              type="button"
              class="cv2-btn cv2-btn--primary cv2-colpick-btn"
              title="选择展现的字段"
              aria-label="选择展现的字段"
            >
              <svg viewBox="0 0 16 16" aria-hidden="true" class="cv2-colpick-ico">
                <rect x="2" y="2.5" width="5" height="4.5" rx="1" />
                <rect x="9" y="2.5" width="5" height="4.5" rx="1" />
                <rect x="2" y="9" width="5" height="4.5" rx="1" />
                <rect x="9" y="9" width="5" height="4.5" rx="1" />
              </svg>
            </button>
          </template>
          <p class="cv2-colpick-title">选择展现的字段</p>
          <el-checkbox-group v-model="visibleColKeys" class="cv2-colpick-group" data-v2="colpick-group">
            <el-checkbox
              v-for="key in (Object.keys(COL_TITLES) as (keyof typeof COL_TITLES)[])"
              :key="key"
              :value="key"
              :disabled="key === 'actions'"
              :data-v2-colcheck="key"
            >{{ COL_TITLES[key] }}</el-checkbox>
          </el-checkbox-group>
        </el-popover>
      </div>
    </div>

    <!-- 回收站列表：12 列照 v1；列宽随内容自适应（统一布局口径见脚本头），超长列 cut-* 省略 -->
    <section class="cv2-panel cv2-table-panel" aria-label="已删除的订单列表">
      <table class="cv2-table">
        <thead>
          <tr>
            <th class="cv2-th-check">
              <input type="checkbox" data-v2="check-page-all" :checked="pageAllChecked" aria-label="勾选当前页全部行" @change="togglePageAll" />
            </th>
            <th v-if="colVisible('order')">订单号</th>
            <th v-if="colVisible('role')">角色名</th>
            <th v-if="colVisible('group')" class="cv2-th-center">阵营/平台</th>
            <th v-if="colVisible('project')">代练/陪玩项目</th>
            <th v-if="colVisible('info')">角色信息</th>
            <th v-if="colVisible('status')" class="cv2-th-center">订单状态</th>
            <th v-if="colVisible('remark')">重要备注</th>
            <th v-if="colVisible('deluser')" class="cv2-th-center">删除人</th>
            <th v-if="colVisible('deltime')">删除时间</th>
            <th v-if="colVisible('ww')" class="cv2-th-center">旺旺/来源</th>
            <th class="cv2-th-center cv2-th-fixed">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in rows"
            :key="row.order_no"
            class="cv2-row"
            :class="{ 'cv2-row--checked': isChecked(row.order_no) }"
            :data-v2-row="row.order_no"
          >
            <td class="cv2-th-check">
              <input
                type="checkbox"
                :data-v2-row-check="row.order_no"
                :checked="isChecked(row.order_no)"
                :aria-label="`勾选 ${viewNoOf(row)}`"
                @change="toggleRow(row.order_no)"
              />
            </td>
            <!-- 已删单不做详情链接：title = 全号 + 去向说明（同 v1 口径 + 统一布局的「悬停看全」），纯文本展示 -->
            <td v-if="colVisible('order')" class="cv2-td-order cut-order">
              <span class="cv2-ono" :title="`${viewNoOf(row)} · ${RECYCLE_DETAIL_NOT_OPEN_TEXT}`" :data-v2-view-no="row.order_no">{{ viewNoOf(row) }}</span>
            </td>
            <td v-if="colVisible('role')" class="cut-role" :title="row.game_role_name">{{ row.game_role_name }}</td>
            <!-- 第 4 列「阵营/平台」恒空（本仓无阵营字段，照 v1 处置：不用别的字段冒充） -->
            <td v-if="colVisible('group')" class="cv2-td-center" />
            <td v-if="colVisible('project')" class="cv2-td-project cut-project" :title="row.project_text">{{ row.project_text }}</td>
            <td v-if="colVisible('info')" class="cut-info" :title="infoTitleOf(row)">{{ infoBodyOf(row) }}</td>
            <td v-if="colVisible('status')" class="cv2-td-center">
              <!-- 状态签照 V2OrderManage 同款（cv2-st + 墨点 cv2-dot；本页只读，用它的 span 形态、不挂点击） -->
              <span class="cv2-st cv2-order-status cv2-st--mark" :class="orderStatusClass(row.status)" :data-status="row.status"><i class="cv2-dot" aria-hidden="true"></i>{{ statusText(row.status) }}</span>
            </td>
            <td v-if="colVisible('remark')" class="cv2-td-project cut-remark" :title="row.remark">{{ row.remark === '' ? '无' : row.remark }}</td>
            <!-- 昵称优先、空串回落登录账号（部署窗口「前端新后端旧」时 deleted_by_name 缺席） -->
            <td v-if="colVisible('deluser')" class="cv2-td-center cut-deluser" :title="row.deleted_by_name ?? row.deleted_by">{{ row.deleted_by_name ?? row.deleted_by }}</td>
            <td v-if="colVisible('deltime')" class="cv2-td-time">{{ timeText(row.deleted_at) }}</td>
            <td v-if="colVisible('ww')" class="cv2-td-center cut-ww" :title="`${wwTextOf(row)} · 该订单来源：${row.source}`">{{ wwTextOf(row) }}</td>
            <td class="cv2-td-center cv2-td-fixed">
              <!-- 还原走琥珀字。solid-yellow 留下，字色档更具体，悬停不再被压暗。 -->
              <button
                v-if="canRestore"
                type="button"
                class="cv2-btn cv2-btn--solid-yellow cv2-btn--sm cv2-btn--back"
                :data-v2-restore="row.order_no"
                :title="`还原 ${viewNoOf(row)}`"
                @click="openRowRestore(row)"
              >还原该订单</button>
              <span v-else>—</span>
            </td>
          </tr>
          <tr v-if="rows.length === 0">
            <td :colspan="visibleColumns.length" class="cv2-empty" data-v2="empty">
              <template v-if="loading">正在加载…</template>
              <template v-else-if="loadError !== null">加载失败：{{ loadError }}</template>
              <template v-else>暂无数据</template>
            </td>
          </tr>
        </tbody>
      </table>
      <div class="cv2-pager" data-v2="pager">
        <span class="cv2-pager-total" data-v2="pager-total">共 {{ total }} 条</span>
        <label class="cv2-pager-size">
          每页
          <select class="cv2-input cv2-input--inline" data-v2="page-size" :value="pageSize" @change="onPageSizeChange">
            <option v-for="n in PAGE_LIMITS" :key="n" :value="n">{{ n }}</option>
          </select>
          条
        </label>
        <el-pagination
          v-model:current-page="page"
          :page-size="pageSize"
          :total="total"
          layout="prev, pager, next"
          background
          @current-change="onPageChange"
        />
      </div>
    </section>

    <!-- 还原确认弹窗（v2 新增，v1 点了直接还原）：说明恢复语义后一次一笔还原 -->
    <el-dialog
      :model-value="restoreDlg !== null"
      title="还原订单"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-restore"
      @update:model-value="restoreDlg = null"
    >
      <p v-if="restoreDlg !== null" class="cv2-dlg-text" data-v2="restore-semantics">确定还原 {{ restoreDlg.viewNo }} 吗？在接单大厅里删掉的，还原后变为未分配。绩效不会恢复。已经退出拆单组的，还原后仍是独立订单。</p>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="restoreDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--back" data-v2="confirm-restore" :disabled="restoring" @click="void doRestore()">确定还原</button>
        </div>
      </template>
    </el-dialog>

    <!-- 炭火 toast -->
    <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
      {{ toast.text }}
    </div>
  </div>
</template>

<style scoped>
/* 本页新增的形状收在这里；色一律走 carbon.css 的 .ttv2 token（类或 var 引用，⛔ 不写裸色值）。
 * 「只在 V2OrderManage.vue scoped 里」的形状（固定列 sticky / cv2-btn--lg）与本页用到的
 * 筛选连体网格（flt-*）都在 carbon.css 零命中，各自 scoped 重新声明；
 * 面板限高原也在此列，2026-11-06 统一布局口径放开（见下方列表块规则）。 */

/* ── 筛选区连体网格组（照 V2OrderManage 同名规则重声明；组框接管边界，EP 控件去描边）── */
/* 紧凑 = 间距收紧（管理订单 2026-11 同档：组间 6/8、面板内边距 10/12），不压组高。 */
.cv2-page .cv2-filter {
  padding: 10px 12px;
}

.flt-wrap {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 8px;
  align-items: center;
}

.flt-group {
  display: inline-flex;
  align-items: stretch;
  height: 32px;
  border: 1px solid var(--tt2-line);
  border-radius: 6px;
  background: var(--tt2-panel);
  transition: border-color 0.15s;
}

/* 不用 overflow:hidden 裁圆角——它会把下拉展开面板一起裁掉；首尾单元格自带圆角。 */
.flt-group > :first-child {
  border-radius: 5px 0 0 5px;
}

.flt-group > :last-child {
  border-radius: 0 5px 5px 0;
}

.flt-group:focus-within {
  border-color: var(--tt2-primary);
}

.flt-label {
  display: inline-flex;
  align-items: center;
  padding: 0 0 0 10px;
  font-size: 13px;
  font-weight: 500;
  color: color-mix(in srgb, var(--tt2-ink) 35%, var(--tt2-dim));
  white-space: nowrap;
}

.flt-cell {
  display: inline-flex;
  align-items: center;
  min-width: 0;
}

.flt-cell + .flt-cell,
.flt-cell + .flt-dash,
.flt-dash + .flt-cell {
  border-left: none;
}

.flt-cell--grow {
  flex: 1;
}

/* 关键词组拉宽占满余量（弹回行时保持可用宽度）。 */
.flt-grow {
  flex: 1 1 240px;
}

.flt-grow .flt-cell--grow {
  min-width: 230px;
}

/* 删除时间格：格子定宽、控件撑满（YYYY-MM-DD HH:mm:ss 19 字形 + 控件图标）。 */
.flt-group .flt-cell.flt-datetimecell {
  width: 186px;
}

.flt-group .flt-cell.flt-datetimecell :deep(.el-date-editor.el-input) {
  width: 100%;
}

.flt-group .flt-cell.flt-datetimecell :deep(.el-input__inner) {
  font-size: 13px;
}

.flt-dash {
  display: inline-flex;
  align-items: center;
  padding: 0 6px;
  font-size: 13px;
  color: var(--tt2-dim);
}

/* 组内 EP 控件去自身描边/圆角（组框承担边界），高度钉 32px（34 − 上下边线）。 */
.flt-group :deep(.el-input__wrapper),
.flt-group :deep(.el-select__wrapper) {
  height: 32px;
  min-height: 32px;
  border-radius: 0;
  background: var(--tt2-panel);
  box-shadow: none;
}

.flt-group :deep(.el-select__wrapper.is-focused),
.flt-group :deep(.el-input__wrapper.is-focus) {
  box-shadow: none;
}

/* 宽度档（挂在 EP 组件根上，根继承本组件 scoped 属性）。 */
.flt-w150 { width: 150px; }

/* 统计句 = 原版橙（对标证据件字面色 #FF6A00 的 token；carbon.css 基类是红，本页覆盖） */
.cv2-total-line {
  color: var(--tt2-summary-orange);
  margin: 0;
}

/* ── 操作列 sticky 右钉（照 V2OrderManage 同名规则重声明）：底色随行状态补齐
 *    （面板白 / hover 晕 / 勾选底）── */
.cv2-th-fixed,
.cv2-td-fixed {
  position: sticky;
  right: 0;
  background: var(--tt2-panel);
  border-left: 1px solid var(--tt2-line);
}

.cv2-th-fixed {
  z-index: 3;
}

.cv2-td-fixed {
  z-index: 2;
}

tbody tr:hover .cv2-td-fixed {
  /* 晕是 rgba：垫白底再叠同色晕 = 与其它单元格的复合结果一致 */
  background: var(--tt2-panel);
  background-image: linear-gradient(var(--tt2-hover-wash), var(--tt2-hover-wash));
}

tbody tr.cv2-row--checked .cv2-td-fixed {
  background: var(--tt2-row-checked);
}

td.cv2-td-fixed {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
}

/* ── 列显隐选择器（照 V2OrderManage 同名规则重声明；popper teleported=false 留在组件树内） */
.cv2-colpick {
  position: relative;
  margin-left: 4px;
}

.cv2-colpick-btn {
  width: 34px;
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.cv2-colpick-ico {
  width: 15px;
  height: 15px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.4;
}

.cv2-colpick-title {
  margin: 0 0 6px;
  font-size: 12px;
  color: var(--tt2-dim);
}

.cv2-colpick-group {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 420px;
  overflow-y: auto;
}

.cv2-colpick-group :deep(.el-checkbox) {
  margin-right: 0;
  height: 28px;
}

/* 表格填满内容区，窄屏保留横向滚动以免列被压扁。 */
.cv2-table-panel {
  width: 100%;
  max-width: 100%;
  overflow-x: auto;
  min-height: 320px;
}

.cv2-table {
  width: 100%;
  min-width: 1100px;
  table-layout: auto;
}

.cv2-table th {
  padding-left: 6px;
  padding-right: 6px;
}

.cv2-table td {
  padding-left: 6px;
  padding-right: 6px;
}

/* min-width:0 解掉共享 .cv2-td-project 的 200px 下限（project/remark 两列挂着它）。 */
.cut-order,
.cut-role,
.cut-project,
.cut-info,
.cut-ww,
.cut-remark,
.cut-deluser {
  min-width: 0;
}

.cut-order { max-width: 180px; }
.cut-role { max-width: 80px; }
.cut-project { max-width: 180px; }
.cut-info { max-width: 160px; }
.cut-ww { max-width: 80px; }
.cut-remark { max-width: 150px; }
.cut-deluser { max-width: 80px; }

/* 还原确认弹窗的语义清单（弹窗 sm 档；文案出处见脚本头 restore_order 指针）。 */
.cv2-dlg-list {
  margin: 10px 0 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.9;
  color: var(--tt2-ink);
}
</style>
