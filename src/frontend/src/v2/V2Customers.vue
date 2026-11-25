<script setup lang="ts">
import { orderStatusClass } from '@/v2/orderStatusAppearance'
/**
 * 「客户管理」v2（2026-11-10 客户线；对标页面 = 会员管理 `/Customer/customerlist`）。
 * 旧页 = island 复刻壳页 `customer-customerlist.ts`（**一行不动**——壳页是否接真数据挂账）。
 * 布局骨架照 V2OrderShipLog：筛选 → 统计卡 → 表格面板；数据走 `api/customers.ts`
 * （后端 = `apps/business/api/customer_views.py`）。
 * - 顶部统计卡：**客户总数**（`total` = 客户表全量数，**不随筛选变**，业务方拍板「直接全算」；
 *   翻页用的筛选后行数在 `filtered_total`，页面不展示它、只拿它算页码）。
 * - 筛选：店铺（member-filters 的 sources）/ 录单人（recorders = [{value,label}]，显示显示名、
 *   提交登录账号）/ 电话（customer_phone 包含匹配，第二批），变更即重新请求；空值不发键。
 * - 排序（第二批）：「订单数」「累计消费」两列表头可点切换（`order_by`，一律降序；
 *   默认订单数；点已选中列保持降序不取消），其余列不可排；排序变化即重新请求。
 * - 表格 7 列（第六批 2026-11-11 删「来源单数」列）：昵称 / 电话 / 订单数 / 最近下单 /
 *   累计消费 / 店铺 / 录单人（录单人列显示显示名 `recorder_display`，映射不到回落登录账号；
 *   行集 = 全部建档客户，订单数 / 累计消费 = 系统单 + 有效来源单**合并**统计——同单两面
 *   只算一张、退款/关闭的单不计）。
 *   ⛔ **前端不算钱**：累计消费是后端给的**两位小数字符串**，原样展示（正负号照实，D-0015）。
 * - 客户订单抽屉（第二批；第六批两段并一张表）：点行右侧滑出，**单一合并列表**
 *   （系统单 + 有效来源单，时间倒序；不受外层筛选影响）。状态列：系统单行走 v2 词表
 *   `V2_ORDER_STATUS_LABELS`（2026-11-03「待抢单→待接单」，映射不到兜底后端
 *   `status_text`），来源单行走后端 `status_text`（闲管家中文）。
 *   `is_system` 行点单号打开 `V2OrderDetailPane` 同款原生双栏订单详情
 *   （复用组件，⛔ 不做第二份详情）；来源单行纯文本不可点。抽屉内只读，不做编辑/动作。
 *   第七批 §1.1：抽屉加宽 800px、表格 fixed + colgroup 固定列宽（两客户列形状一致、
 *   无横滚），头部信息卡 = `customer_info` 七项（昵称/电话/客户ID 掩码/累计消费/
 *   订单总数/最近下单/建档时间，合并统计口径）。
 * - 分页默认 50（后端 `CustomerMembersPagination` 同值）。
 * - 权限：路由挂 `customer.member_list` ∪ `customer.member_list_self_only`（第五批起两节点
 *   任一即进，旧路由同批）；读数据由后端 403 兜底、数据范围（限制档只看自己录单的客户）
 *   由服务端收口，页内不再二次判节点（列表页没有写动作，无 fail-closed 控件可收）。
 * - 空态文案说明数据来源：客户来自闲管家买家标识，存量回填 / 新录单后增长。
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import {
  fetchCustomerMemberFilters,
  fetchCustomerMemberOrders,
  fetchCustomerMembers,
  type CustomerInfoCard,
  type CustomerMemberFilters,
  type CustomerMemberRow,
  type CustomerOrderBy,
  type CustomerOrderRow,
} from '@/api/customers'
import { isOrderNoShape } from '@/api/orders'
import { v2OrderStatusLabel } from '@/config/orderStatus'
import OrderDetailDialog from '@/v2/OrderDetailDialog.vue'
import V2OrderDetailPane from '@/v2/V2OrderDetailPane.vue'

function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

/* ── 提示（页面内自绘 toast，同 V2OrderShipLog）──────────────────────────── */

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

/* ── 筛选（空串 = 不发键 = 不筛）────────────────────────────────────────── */

type CustomerFilters = {
  source: string
  recorder: string
  phone: string
}

const filters = reactive<CustomerFilters>({ source: '', recorder: '', phone: '' })
const sourceOptions = ref<CustomerMemberFilters['sources']>([])
const recorderOptions = ref<CustomerMemberFilters['recorders']>([])

const PAGE_SIZE = 50

/** 排序档位（后端一律降序；点已选中列保持降序，不取消排序）。 */
const sortBy = ref<CustomerOrderBy>('order_count')

type MembersParams = {
  source?: number
  recorder?: string
  phone?: string
  order_by: CustomerOrderBy
  page: number
  page_size: number
}

function buildParams(targetPage: number): MembersParams {
  const params: MembersParams = {
    order_by: sortBy.value,
    page: targetPage,
    page_size: PAGE_SIZE,
  }
  // 下拉的「全部」档是空串：不发键（后端缺键 = 不筛）；选中的 id 才发数字。
  if (filters.source !== '') params.source = Number(filters.source)
  if (filters.recorder !== '') params.recorder = filters.recorder
  if (filters.phone !== '') params.phone = filters.phone
  return params
}

/* ── 取数 ─────────────────────────────────────────────────────────────── */

const rows = ref<CustomerMemberRow[]>([])
const recorderCounts = ref<Array<{ recorder: string; name: string; count: number }>>([])
/** 客户总数 = 客户表全量数（后端 `total`，不随筛选变）。 */
const total = ref(0)
/** 筛选后行数（后端 `filtered_total`；只喂分页器）。 */
const filteredTotal = ref(0)
const loading = ref(false)
const loadError = ref<string | null>(null)
const page = ref(1)

async function reload(targetPage = 1): Promise<void> {
  loading.value = true
  try {
    const result = await fetchCustomerMembers(buildParams(targetPage))
    rows.value = result.items
    recorderCounts.value = result.recorder_counts ?? []
    total.value = result.total
    filteredTotal.value = result.filtered_total
    page.value = result.page
    loadError.value = null
  } catch (e) {
    rows.value = []
    recorderCounts.value = []
    filteredTotal.value = 0
    loadError.value = errText(e)
    showToast(loadError.value, 'error')
  } finally {
    loading.value = false
  }
}

/** 行数变化后当前页可能越界（服务端翻页不会自己夹）；页数按**筛选后**行数算。 */
watch(filteredTotal, (t) => {
  const last = Math.max(1, Math.ceil(t / PAGE_SIZE))
  if (page.value > last) void reload(last)
})

async function loadFilters(): Promise<void> {
  try {
    const result = await fetchCustomerMemberFilters()
    sourceOptions.value = result.sources
    recorderOptions.value = result.recorders
  } catch (e) {
    // 下拉候选拿不到不算致命：列表照常加载，只是筛不了；提示一句。
    showToast(`筛选字典加载失败：${errText(e)}`, 'error')
  }
}

function onPageChange(p: number): void {
  void reload(p)
}

function onSearch(): void {
  void reload(1)
}

function resetFilters(): void {
  filters.source = ''
  filters.recorder = ''
  filters.phone = ''
  void reload(1)
}

/** 表头排序（仅订单数 / 累计消费两列可排）：点已选中列保持降序，点另一列切换。 */
function onSortChange(field: CustomerOrderBy): void {
  if (sortBy.value === field) return
  sortBy.value = field
  void reload(1)
}

/* ── 客户订单抽屉（点行右侧滑出；只读）───────────────────────────────────── */

const drawerVisible = ref(false)
const drawerCustomer = ref<{ eid: string; nick: string; phone: string } | null>(null)
const drawerRows = ref<CustomerOrderRow[]>([])
const drawerTotal = ref(0)
const drawerPage = ref(1)
const drawerLoading = ref(false)
/** 头部信息卡（第七批 §1.1）= 后端 `customer_info`（合并统计，不随外层筛选变）。 */
const drawerInfo = ref<CustomerInfoCard | null>(null)

async function reloadDrawer(targetPage = 1): Promise<void> {
  const customer = drawerCustomer.value
  if (customer === null) return
  drawerLoading.value = true
  try {
    const result = await fetchCustomerMemberOrders({
      eid: customer.eid,
      page: targetPage,
      page_size: PAGE_SIZE,
    })
    drawerRows.value = result.items
    drawerTotal.value = result.total
    drawerPage.value = result.page
    drawerInfo.value = result.customer_info
  } catch (e) {
    drawerRows.value = []
    drawerTotal.value = 0
    showToast(`订单加载失败：${errText(e)}`, 'error')
  } finally {
    drawerLoading.value = false
  }
}

function openCustomer(row: CustomerMemberRow): void {
  drawerCustomer.value = { eid: row.eid, nick: row.buyer_nick, phone: row.phone }
  drawerRows.value = []
  drawerTotal.value = 0
  drawerPage.value = 1
  drawerInfo.value = null
  drawerVisible.value = true
  void reloadDrawer(1)
}

function onDrawerPageChange(p: number): void {
  void reloadDrawer(p)
}

/** 抽屉头两行（昵称可空 → 占位；电话可空 → 不渲染）。 */
const drawerNick = computed(() => {
  const customer = drawerCustomer.value
  if (customer === null) return ''
  return customer.nick !== '' ? customer.nick : '（无昵称）'
})
const drawerPhone = computed(() => drawerCustomer.value?.phone ?? '')

/* ── 订单详情（抽屉行内点订单号；复用 V2OrderDetailPane，⛔ 不做第二份详情）────── */

type DetailTarget = {
  orderNo: string
  viewNo: string
  gameName: string
  projectText: string
}

const detailDlg = ref<DetailTarget | null>(null)

/** 抽屉状态列：系统单行走 v2 词表（2026-11-03「待抢单→待接单」只落 v2，⛔ 不改后端
 * 共享 label）；词表没有的枚举兜底后端 `status_text`；来源单行走后端闲管家中文。 */
function drawerStatusText(order: CustomerOrderRow): string {
  if (!order.is_system) return order.status_text
  const mapped = v2OrderStatusLabel(String(order.status))
  return mapped === String(order.status) ? order.status_text : mapped
}

/** 仅 is_system 行可点：单号 = 系统单号（来源单不在系统、无详情可开）。
 * 状态由 pane 自己取详情回填（行上的状态键只供列表列显示）。 */
function openOrderDetail(row: CustomerOrderRow): void {
  if (!isOrderNoShape(row.no)) {
    showToast('订单号格式不正确，无法打开订单详情', 'error')
    return
  }
  detailDlg.value = {
    orderNo: row.no,
    viewNo: row.no,
    gameName: row.game,
    projectText: row.project,
  }
}

/* ── 单元格展示 ───────────────────────────────────────────────────────── */

/** 下单时刻：ISO 串 → `YYYY-MM-DD HH:mm`（同 V2OrderShipLog 的 fmtTime 口径）。 */
function fmtTime(value: string): string {
  return typeof value === 'string' && value !== '' ? value.replace('T', ' ').slice(0, 16) : ''
}

/** 店铺列：来源字典当前名（后端已回落快照）；无来源的单显示「—」。 */
function sourceText(row: CustomerMemberRow): string {
  return row.source_name !== '' ? row.source_name : '—'
}

/** 录单人列：显示名优先，映射不到（空串）回落登录账号。 */
function recorderText(row: CustomerMemberRow): string {
  return row.recorder_display !== '' ? row.recorder_display : row.recorder
}

/** 空态行的 colspan = 7 数据列（列全集固定）。 */
const COLSPAN_ALL = 7

const pagerTotal = computed(() => filteredTotal.value)

onMounted(() => {
  void loadFilters()
  void reload(1)
})
</script>

<template>
  <div class="cv2-page" data-v2-page="customers">
    <!-- 顶部统计卡：客户总数（客户表全量，不随筛选变） -->
    <section class="cv2-panel members-stat" aria-label="客户总数" data-v2="stat">
      <span class="members-stat-label">客户总数</span>
      <b class="members-stat-num" data-v2="stat-total">{{ total }}</b>
      <span class="members-stat-hint">客户来自闲管家买家标识，存量回填 / 新录单后增长</span>
    </section>

    <!-- 筛选区：店铺 + 录单人 + 电话 + 查询/重置（连体网格组形态照 V2OrderShipLog） -->
    <section class="cv2-panel cv2-filter" aria-label="筛选" data-v2="filters">
      <div class="flt-wrap">
        <div class="flt-group" data-v2-filter-group="source">
          <span class="flt-label">店铺</span>
          <span class="flt-cell"><el-select
            v-model="filters.source"
            class="flt-w160"
            data-v2-filter="source"
            placeholder="全部"
            :teleported="false"
          >
            <el-option value="" label="全部" />
            <el-option v-for="s in sourceOptions" :key="s.id" :value="String(s.id)" :label="s.name" />
          </el-select></span>
        </div>
        <div class="flt-group" data-v2-filter-group="recorder">
          <span class="flt-label">录单人</span>
          <span class="flt-cell"><el-select
            v-model="filters.recorder"
            class="flt-w160"
            data-v2-filter="recorder"
            placeholder="全部"
            :teleported="false"
          >
            <el-option value="" label="全部" />
            <el-option v-for="r in recorderOptions" :key="r.value" :value="r.value" :label="r.label" />
          </el-select></span>
        </div>
        <div class="flt-group" data-v2-filter-group="phone">
          <span class="flt-label">电话</span>
          <span class="flt-cell"><el-input
            v-model="filters.phone"
            class="flt-w160"
            data-v2-filter="phone"
            placeholder="按电话筛选"
            clearable
          /></span>
        </div>
        <div class="cv2-filter-actions">
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--lg cv2-btn--find cv2-btn--fill" data-v2="search" @click="onSearch">查询</button>
          <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--lg cv2-btn--quiet" data-v2="reset" @click="resetFilters">重置</button>
        </div>
      </div>
    </section>

    <!-- 客户列表：7 列（第六批删「来源单数」，订单数/累计消费 = 两段合并统计）；
         金额列 = 后端字符串原样展示（前端不算钱）；
         「订单数 / 累计消费」两列表头可点切换排序（一律降序） -->
    <div class="customer-layout">
    <section class="cv2-panel cv2-table-panel" aria-label="客户列表">
      <table class="cv2-table">
        <thead>
          <tr>
            <th>昵称</th>
            <th class="cv2-th-center">电话</th>
            <th class="cv2-th-center">
              <button
                type="button"
                class="cv2-sort-head"
                :class="{ 'cv2-sort-head--on': sortBy === 'order_count' }"
                data-v2-sort="order_count"
                @click="onSortChange('order_count')"
              >订单数<span class="cv2-sort-arrow" aria-hidden="true">↓</span></button>
            </th>
            <th class="cv2-th-center">最近下单</th>
            <th class="cv2-th-center">
              <button
                type="button"
                class="cv2-sort-head"
                :class="{ 'cv2-sort-head--on': sortBy === 'total_amount' }"
                data-v2-sort="total_amount"
                @click="onSortChange('total_amount')"
              >累计消费<span class="cv2-sort-arrow" aria-hidden="true">↓</span></button>
            </th>
            <th class="cv2-th-center">店铺</th>
            <th class="cv2-th-center">录单人</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in rows"
            :key="row.eid"
            class="cv2-row cv2-row--click"
            :data-v2-row="row.eid"
            @click="openCustomer(row)"
          >
            <td class="cut-nick" :title="row.buyer_nick || row.eid">{{ row.buyer_nick !== '' ? row.buyer_nick : '（无昵称）' }}</td>
            <td class="cv2-td-center">{{ row.phone !== '' ? row.phone : '—' }}</td>
            <td class="cv2-td-center" data-v2-row-order-count>{{ row.order_count }}</td>
            <td class="cv2-td-center cv2-td-time">{{ fmtTime(row.last_order_at) }}</td>
            <td class="cv2-td-center cv2-num" data-v2-row-amount>{{ row.total_amount }}</td>
            <td class="cv2-td-center">{{ sourceText(row) }}</td>
            <td class="cv2-td-center" data-v2-row-recorder>{{ recorderText(row) }}</td>
          </tr>
          <tr v-if="rows.length === 0">
            <td :colspan="COLSPAN_ALL" class="cv2-empty" data-v2="empty">
              <template v-if="loading">正在加载…</template>
              <template v-else-if="loadError !== null">加载失败：{{ loadError }}</template>
              <template v-else>还没有客户：客户来自闲管家买家标识，存量回填 / 新录单后增长</template>
            </td>
          </tr>
        </tbody>
      </table>
      <div class="cv2-pager" data-v2="pager">
        <span class="cv2-pager-total" data-v2="pager-total">共 {{ pagerTotal }} 位客户</span>
        <span class="cv2-pager-size" data-v2="page-size">每页 {{ PAGE_SIZE }} 条</span>
        <el-pagination
          v-model:current-page="page"
          :page-size="PAGE_SIZE"
          :total="pagerTotal"
          layout="prev, pager, next"
          background
          @current-change="onPageChange"
        />
      </div>
    </section>
    <aside class="cv2-panel customer-recorders" aria-label="录单人客户数">
      <h2 class="cv2-panel-title">录单人客户数</h2>
      <p class="customer-recorders-note">按当前筛选统计；同一客户可属于多位录单人</p>
      <div v-if="recorderCounts.length === 0" class="cv2-empty">{{ loading ? '正在加载…' : '暂无数据' }}</div>
      <table v-else class="customer-recorder-table cv2-data-grid">
        <thead><tr><th>录单人</th><th>客户数</th></tr></thead>
        <tbody><tr v-for="item in recorderCounts" :key="item.recorder"><td :title="item.recorder">{{ item.name }}</td><td class="cv2-num">{{ item.count }}</td></tr></tbody>
      </table>
    </aside>
    </div>

    <!-- 客户订单抽屉：点行右侧滑出（只读；行内点订单号开同款原生双栏详情）。
         第七批 §1.1：加宽到 800px（720px 装不下 8 列固定宽），表格 fixed 布局 +
         colgroup 显式列宽 ⇒ 两个客户的抽屉列形状一致、内容区无横滚。 -->
    <el-drawer
      v-model="drawerVisible"
      direction="rtl"
      size="800px"
      class="cv2-drawer"
      data-v2="drawer"
    >
      <template #header>
        <span class="cv2-drawer-title" data-v2="drawer-title">
          {{ drawerNick }}
          <span v-if="drawerPhone !== ''" class="cv2-drawer-sub cv2-num">{{ drawerPhone }}</span>
        </span>
      </template>
      <div class="cv2-drawer-body" data-v2="drawer-body">
        <!-- 头部信息卡（第七批 §1.1）：七项档案事实，取 member-orders 的 customer_info
             （合并统计口径、不随外层筛选变）；eid 出参已打掩码。 -->
        <dl class="cv2-drawer-info" data-v2="drawer-info">
          <div class="cv2-info-item"><dt>昵称</dt><dd data-v2="info-nick">{{ drawerInfo === null ? '—' : (drawerInfo.buyer_nick !== '' ? drawerInfo.buyer_nick : '（无昵称）') }}</dd></div>
          <div class="cv2-info-item"><dt>电话</dt><dd class="cv2-num" data-v2="info-phone">{{ drawerInfo !== null && drawerInfo.phone !== '' ? drawerInfo.phone : '—' }}</dd></div>
          <div class="cv2-info-item"><dt>客户ID</dt><dd class="cv2-num" data-v2="info-eid">{{ drawerInfo?.eid_masked ?? '—' }}</dd></div>
          <div class="cv2-info-item"><dt>累计消费</dt><dd class="cv2-num" data-v2="info-amount">{{ drawerInfo?.total_amount ?? '—' }}</dd></div>
          <div class="cv2-info-item"><dt>订单总数</dt><dd class="cv2-num" data-v2="info-count">{{ drawerInfo?.order_count ?? '—' }}</dd></div>
          <div class="cv2-info-item"><dt>最近下单</dt><dd class="cv2-td-time" data-v2="info-last">{{ drawerInfo !== null && drawerInfo.last_order_at !== '' ? fmtTime(drawerInfo.last_order_at) : '—' }}</dd></div>
          <div class="cv2-info-item"><dt>建档时间</dt><dd class="cv2-td-time" data-v2="info-created">{{ drawerInfo !== null && drawerInfo.created_at !== '' ? fmtTime(drawerInfo.created_at) : '—' }}</dd></div>
        </dl>
        <!-- 单一合并列表（第六批）：系统单 + 有效来源单按时间倒序混排；
             状态列 = 系统单走 v2 词表（兜底后端 status_text）、来源单走后端闲管家中文；
             系统单行单号可点开详情、来源单行纯文本。列宽 = colgroup 固定（第七批）。 -->
        <table class="cv2-table cv2-drawer-table">
          <colgroup>
            <col class="dw-col-no" />
            <col class="dw-col-game" />
            <col />
            <col class="dw-col-amount" />
            <col class="dw-col-time" />
            <col class="dw-col-status" />
            <col class="dw-col-shop" />
            <col class="dw-col-recorder" />
          </colgroup>
          <thead>
            <tr>
              <th>单号</th>
              <th class="cv2-th-center">游戏</th>
              <th class="cv2-th-center">项目</th>
              <th class="cv2-th-center">金额</th>
              <th class="cv2-th-center">时间</th>
              <th class="cv2-th-center">状态</th>
              <th class="cv2-th-center">店铺</th>
              <th class="cv2-th-center">录单人</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="order in drawerRows" :key="order.no" class="cv2-row" :data-v2-drawer-row="order.no">
              <td>
                <a
                  v-if="order.is_system"
                  href="#"
                  class="cv2-order-link"
                  :title="order.no"
                  :data-v2-drawer-order="order.no"
                  @click.prevent="openOrderDetail(order)"
                >{{ order.no }}</a>
                <span v-else class="cv2-num" :title="order.no">{{ order.no }}</span>
              </td>
              <td class="cv2-td-center">{{ order.game !== '' ? order.game : '—' }}</td>
              <td class="cv2-td-center cut-nick" :title="order.project">{{ order.project !== '' ? order.project : '—' }}</td>
              <td class="cv2-td-center cv2-num" data-v2-drawer-amount>{{ order.amount }}</td>
              <td class="cv2-td-center cv2-td-time">{{ fmtTime(order.time) }}</td>
              <td class="cv2-td-center" data-v2-drawer-status>
                <span v-if="order.is_system" class="cv2-st cv2-order-status" :class="orderStatusClass(String(order.status))"><i class="cv2-dot" aria-hidden="true"></i>{{ drawerStatusText(order) }}</span>
                <template v-else>{{ drawerStatusText(order) }}</template>
              </td>
              <td class="cv2-td-center">{{ order.shop !== '' ? order.shop : '—' }}</td>
              <td class="cv2-td-center" data-v2-drawer-recorder>{{ order.recorder !== '' ? order.recorder : '—' }}</td>
            </tr>
            <tr v-if="drawerRows.length === 0">
              <td :colspan="8" class="cv2-empty" data-v2="drawer-empty">
                <template v-if="drawerLoading">正在加载…</template>
                <template v-else>该客户还没有订单</template>
              </td>
            </tr>
          </tbody>
        </table>
        <div class="cv2-pager" data-v2="drawer-pager">
          <span class="cv2-pager-total" data-v2="drawer-total">共 {{ drawerTotal }} 张订单</span>
          <el-pagination
            v-model:current-page="drawerPage"
            :page-size="PAGE_SIZE"
            :total="drawerTotal"
            layout="prev, pager, next"
            background
            @current-change="onDrawerPageChange"
          />
        </div>
      </div>
    </el-drawer>

    <!-- 订单详情：V2OrderDetailPane 同款原生双栏（复用组件，⛔ 不做第二份详情） -->
    <OrderDetailDialog
      :model-value="detailDlg !== null"
      :title="detailDlg === null ? '' : `${detailDlg.viewNo}订单详情`"
      class="cv2-dialog cv2-dialog--detail"
      destroy-on-close
      data-v2="dlg-detail"
      @update:model-value="detailDlg = null"
      @chat-handoff="drawerVisible = false"
    >
      <V2OrderDetailPane
        v-if="detailDlg !== null"
        :order-no="detailDlg.orderNo"
        :view-no="detailDlg.viewNo"
        :game-name="detailDlg.gameName"
        :project-text="detailDlg.projectText"
      />
    </OrderDetailDialog>

    <!-- 炭火 toast -->
    <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
      {{ toast.text }}
    </div>
  </div>
</template>

<style scoped>
/* 本页新增的形状收在这里；色一律走 carbon.css 的 .ttv2 token（⛔ 不写裸色值）。
 * 筛选连体网格（flt-*）与「列表块左对齐 / 不横滚 / 不限高」是各列表页 scoped 的既有做法，
 * 本页照 V2OrderShipLog 同款重声明。 */

/* ── 顶部统计卡 ──────────────────────────────────────────────────────── */
.members-stat {
  display: flex;
  align-items: baseline;
  gap: 12px;
  padding: 14px 16px;
}

.members-stat-label {
  font-size: 13px;
  font-weight: 500;
  color: color-mix(in srgb, var(--tt2-ink) 35%, var(--tt2-dim));
}

.members-stat-num {
  font-family: var(--tt2-mono);
  font-size: 28px;
  line-height: 1;
  color: var(--tt2-primary);
  font-variant-numeric: tabular-nums;
}

.members-stat-hint {
  font-size: 12px;
  color: var(--tt2-dim);
}

/* ── 筛选区连体网格组（照 V2OrderShipLog 同名规则重声明）────────────────── */
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

/* 组内 EP 控件去自身描边/圆角（组框承担边界），高度钉 32px。 */
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

/* 宽度档（挂在 EP 组件根上）。 */
.flt-w160 { width: 160px; }

/* ── 表头排序钮（仅两列可排）：长得像表头文字，选中档高亮 ───────────────── */
.cv2-sort-head {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 0;
  border: none;
  background: none;
  cursor: pointer;
  font: inherit;
  color: inherit;
}

.cv2-sort-head--on {
  color: var(--tt2-primary);
}

.cv2-sort-arrow {
  font-size: 11px;
  line-height: 1;
}

/* 行可点（打开抽屉）。 */
.cv2-row--click {
  cursor: pointer;
}

/* ── 客户订单抽屉（炭火皮：面板色 / 细线，形状同列表面板）────────────────── */
.cv2-drawer :deep(.el-drawer__header) {
  margin-bottom: 0;
  padding: 14px 16px;
  border-bottom: 1px solid var(--tt2-line);
  color: var(--tt2-ink);
}

.cv2-drawer :deep(.el-drawer__body) {
  padding: 12px 16px;
}

.cv2-drawer-title {
  display: inline-flex;
  align-items: baseline;
  gap: 10px;
  font-size: 14px;
  font-weight: 600;
  color: var(--tt2-ink);
}

.cv2-drawer-sub {
  font-size: 12px;
  font-weight: 400;
  color: var(--tt2-dim);
}

.cv2-drawer-body {
  display: block;
}

/* ── 列表块统一布局（四记录页同款：不横滚 / 左对齐 / 不限高）────────────── */
.cv2-table-panel {
  width: fit-content;
  max-width: calc(100% - 32px);
  min-height: 320px;
}

.cv2-table {
  width: auto;
  table-layout: auto;
}

/* ── 抽屉表（第七批 §1.1）：fixed 布局 + colgroup 显式列宽 ⇒ 列形状恒定、
 *    内容区无横滚；单元格内边距收紧给 8 列腾位。fixed 求和（684px）< 容器
 *    （800 − 32 内边距 = 768px），差值全给「项目」列自适应 + ellipsis。 */
.cv2-drawer-table {
  width: 100%;
  table-layout: fixed;
}

.cv2-drawer-table th,
.cv2-drawer-table td {
  padding: 0 8px;
}

.dw-col-no { width: 160px; }
.dw-col-game { width: 84px; }
.dw-col-amount { width: 76px; }
.dw-col-time { width: 132px; }
.dw-col-status { width: 96px; }
.dw-col-shop { width: 88px; }
.dw-col-recorder { width: 80px; }

/* ── 抽屉头部信息卡（第七批 §1.1）：七项事实一行四列的紧凑网格 ─────────────── */
.cv2-drawer-info {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px 16px;
  margin: 0 0 12px;
  padding: 10px 12px;
  border: 1px solid var(--tt2-line);
  border-radius: 8px;
  background: var(--tt2-panel);
}

.cv2-info-item dt {
  font-size: 12px;
  color: var(--tt2-dim);
}

.cv2-info-item dd {
  margin: 2px 0 0;
  font-size: 13px;
  color: var(--tt2-ink);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.cut-nick {
  min-width: 0;
  max-width: 180px;
}

.customer-layout { display: grid; grid-template-columns: minmax(0, 1fr) 260px; gap: 12px; align-items: start; }
.customer-layout > .cv2-table-panel { min-width: 0; overflow-x: auto; }
.customer-recorders { padding: 16px; min-width: 0; }
.customer-recorders-note { color: var(--tt2-dim); font-size: 12px; margin: 8px 0; }
.customer-recorder-table { width: 100%; border-collapse: collapse; }
.customer-recorder-table th, .customer-recorder-table td { padding: 8px 4px; text-align: left; }
.customer-recorder-table th:last-child, .customer-recorder-table td:last-child { text-align: right; }
@media (max-width: 900px) { .customer-layout { grid-template-columns: minmax(0, 1fr); } }

/* 金额等宽字形（尺寸/字体非色值）。 */
.cv2-num {
  font-family: var(--tt2-mono);
  font-variant-numeric: tabular-nums;
}
</style>
