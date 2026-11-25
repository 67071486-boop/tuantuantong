<script setup lang="ts">
import MobileSearch from '../components/MobileSearch.vue'
import MobileSelect from '../components/MobileSelect.vue'
import {
  f7Badge as F7Badge,
  f7Button as F7Button,
  f7Card as F7Card,
  f7CardContent as F7CardContent,
  f7CardFooter as F7CardFooter,
  f7CardHeader as F7CardHeader,
  f7Preloader as F7Preloader,
} from 'framework7-vue'
import { computed, onBeforeUnmount, ref, useTemplateRef, watch } from 'vue'
import { Toast } from '../framework7Ui'
import MobilePagedList from '../components/MobilePagedList.vue'
import { EMP_TYPE, fetchPerformancesWithSummary, type PerformanceListItem, type PerformanceSummary } from '@/api/performances'
import { ORDER_STATUS_LABELS, ORDER_STATUS_VALUES, orderStatusLabel, type OrderStatus } from '@/config/orderStatus'
import { useSessionStore } from '@/stores/session'
import { canReadWorkerPerformance } from '../tabs'
import { formatDateTime } from '../utils/datetime'
// 卡片轮廓的**共享唯一定义**（真机反馈第一批；`.m-card` 全局类，三页同一出处）。
import '../styles/card.css'

/** 每页条数（后端 `EnvelopePagination` 默认 20，与大厅页同参）。 */
const PAGE_SIZE = 20
const SALES_PAGE_SIZE = 200
const MAX_SALES_PAGES = 100
const props = defineProps<{ summaryOnly?: boolean }>()

/**
 * 「订单金额 / 记录退款金额 / 预计差额利润 / 已生效差额利润」这四格的准入节点（**两节点任一**）。
 * 值 = 后端 `performance_views.py::can_view_order_money` 的那两把钥匙：`order.list` **或** `finance.payroll`（业务方拍板：财务是合法编制，看不到反而会把真金额藏成假数）。`canNode` 本来就是 some 语义 ⇒ 直接给数组，⛔ 不另造一套判据。
 * 前端只决定「画不画这四格」，真正的脱敏在服务端（`summary` 四项 / 行三列给 `null`）：前端隐藏只是体验层，改前端绕不过后端。
 */
const ORDER_MONEY_NODES = ['order.list', 'finance.payroll']

/** 关键字防抖窗长（大厅页 `KEYWORD_DEBOUNCE_MS` 同值：300ms）。 */
const KEYWORD_DEBOUNCE_MS = 300

/**
 * 结算状态的两个**后端存储值**（`apps/business/models.py::SettleStatus` TextChoices——
 * 中文即值；下拉选项与卡片渲染同一出处，⛔ 不自造第三种写法）。
 */
const SETTLE_UNSETTLED = '未结账'
const SETTLE_SETTLED = '已结账'

/** 会话（判据来源：`canNode` 读 `/api/auth/me` 的 `menu_permissions`；管理员按既有口径短路）。 */
const session = useSessionStore()
const userId = computed(() => canReadWorkerPerformance(session.canNode) ? session.user?.id : undefined)
const canRead = computed(() => userId.value !== undefined)
const account = computed(() => session.user?.identity_plane)

/**
 * 那四格画不画：`session.canNode(ORDER_MONEY_NODES)`——照抄 `MePage.vue` / `HallPage.vue`
 * 的现成写法（⛔ 不新造一套 `hasNode`；`canNode` 对数组是 some 语义，与后端
 * `has_node_now` 的「任一即放行」同口径）。取不到 me（`user === null`）⇒ 空集 ⇒ **不画**。
 */
const showOrderMoney = computed(() => session.canNode(ORDER_MONEY_NODES))

/* ── 列表状态 ── */
const items = ref<PerformanceListItem[]>([])
const sales = ref<PerformanceListItem[]>([])
const salesState = ref<'loading' | 'ready' | 'error'>('loading')
/**
 * 顶部统计（后端 `summary`；全集口径——筛选后是**筛后全集**，空集 ⇒ "0.00"）。
 * ⭐ 后端对不持 `order.list` 也不持 `finance.payroll` 的人把那四项给 **`null`**（字段脱敏、不是 403；见 `performance_views.py::can_view_order_money`）⇒ 本页那四格**按节点整格不渲染**（不显示 `—`：那会让人以为是取数失败）。
 */
const summary = ref<PerformanceSummary | null>(null)
const total = ref(0)
const received = ref(0)
const page = ref(1)
/** 取数状态：`loading`（首屏）→ `ready` / `error`。 */
const loadState = ref<'loading' | 'ready' | 'error'>('loading')
const loadError = ref('')
/** 分页列表的 `asyncLoading`：'loading' 转圈 / 'load-more' 可点加载 / 其余不渲染。 */
const listLoading = ref(false)
const hasMore = computed(() => received.value < total.value)
// 新筛选或刷新返回后，旧请求不能改写汇总或混入明细。
let requestVersion = 0

/* ── 服务端筛选状态 ── */
/** 关键字：输入原值（受控）+ 防抖后真正发请求的 `appliedKeyword`。 */
const keyword = ref('')
const appliedKeyword = ref('')
let keywordTimer: number | null = null
/** 结算状态下拉；`''` = 全部（不送参数）。 */
const settleFilter = ref('')
/** 订单状态下拉；`''` = 全部（不送参数）。 */
const statusFilter = ref('')

// 关键字 300ms 防抖（大厅页 `watch(keyword)` 同款）：到点重置回第 1 页重取。
watch(keyword, (next) => {
  if (keywordTimer !== null) window.clearTimeout(keywordTimer)
  keywordTimer = window.setTimeout(() => {
    keywordTimer = null
    appliedKeyword.value = next.trim()
  }, KEYWORD_DEBOUNCE_MS)
})

// 换筛选后清掉旧结果；下拉刷新和重试使用原筛选，可以保留原结果。
watch([userId, account, () => props.summaryOnly, appliedKeyword, settleFilter, statusFilter], ([user]) => {
  items.value = []
  sales.value = []
  salesState.value = 'loading'
  summary.value = null
  total.value = 0
  received.value = 0
  if (user !== undefined) {
    void load()
  } else {
    requestVersion += 1
  }
}, { immediate: true })

onBeforeUnmount(() => {
  requestVersion += 1
  if (keywordTimer !== null) window.clearTimeout(keywordTimer)
})

/** 订单状态下拉的选项 = 全站共享枚举（`@/config/orderStatus`，后端 choices 的镜像；⛔ 不自造）。 */
const STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  ...ORDER_STATUS_VALUES.map((value) => ({ value, label: ORDER_STATUS_LABELS[value as OrderStatus] })),
]
const SETTLE_OPTIONS = [
  { value: '', label: '全部结算' },
  { value: SETTLE_UNSETTLED, label: SETTLE_UNSETTLED },
  { value: SETTLE_SETTLED, label: SETTLE_SETTLED },
]

const asyncLoading = computed<'loading' | 'load-more' | undefined>(() => {
  if (listLoading.value) return 'loading'
  if (loadState.value === 'ready' && hasMore.value) return 'load-more'
  return undefined
})

/** 本次取数的筛选参数（空值**不送**——`toPerformanceParams` 也会丢，这里显式收窄便于恰好断言）。 */
function filterParams(): { settle_status?: string; order_status?: string; keyword?: string } {
  const kw = appliedKeyword.value
  return {
    ...(settleFilter.value !== '' ? { settle_status: settleFilter.value } : {}),
    ...(statusFilter.value !== '' ? { order_status: statusFilter.value } : {}),
    ...(kw !== '' ? { keyword: kw } : {}),
  }
}

/** 首屏 / 重试 / 下拉刷新 / 筛选变更：重回第 1 页、整表替换（统计随刷新更新）。 */
async function loadSales(user: number, isCurrent: () => boolean): Promise<void> {
  salesState.value = 'loading'
  const rows = new Map<number, PerformanceListItem>()
  try {
    for (let page = 1; page <= MAX_SALES_PAGES; page += 1) {
      if (!isCurrent()) return
      const data = await fetchPerformancesWithSummary({ user, emp_type: EMP_TYPE.SALES, page, page_size: SALES_PAGE_SIZE })
      if (!isCurrent()) return
      for (const row of data.items) {
        if (row.user === user && row.emp_type === EMP_TYPE.SALES) rows.set(row.id, row)
      }
      sales.value = [...rows.values()]
      if (data.items.length === 0 || page * SALES_PAGE_SIZE >= data.total) {
        salesState.value = 'ready'
        return
      }
    }
    throw new Error('已显示本次可加载的销售提成，仍有更早的记录')
  } catch (error) {
    if (isCurrent()) salesState.value = 'error'
    throw error
  }
}

async function load(): Promise<void> {
  const user = userId.value
  if (user === undefined) return
  const version = ++requestVersion
  loadError.value = ''
  loadState.value = 'loading'
  listLoading.value = false
  try {
    if (props.summaryOnly) {
      const isCurrent = () => version === requestVersion && userId.value === user
      const results = await Promise.allSettled([
        fetchPerformancesWithSummary({ user, page: 1, page_size: 1, include_settlement_summary: 1 }).then((data) => {
          if (isCurrent()) summary.value = data.summary
        }),
        loadSales(user, isCurrent),
      ])
      if (!isCurrent()) return
      const failure = results.find((result) => result.status === 'rejected')
      if (failure?.status === 'rejected') throw failure.reason
      loadState.value = 'ready'
      return
    }
    const data = await fetchPerformancesWithSummary({ page: 1, page_size: PAGE_SIZE, ...filterParams() })
    if (version !== requestVersion || userId.value !== user) return
    items.value = data.items
    received.value = data.items.length
    total.value = data.total
    summary.value = data.summary
    page.value = 1
    loadState.value = 'ready'
  } catch (e) {
    if (version !== requestVersion || userId.value !== user) return
    loadState.value = 'error'
    loadError.value = errText(e)
    Toast.error(loadError.value)
  }
}

/** 上拉加载更多：追加下一页（统计不重算——后端 summary 是筛选后全集口径，翻页不改变它）。 */
async function onLoadMore(): Promise<void> {
  const user = userId.value
  if (user === undefined || loadState.value !== 'ready' || listLoading.value || !hasMore.value) return
  const version = requestVersion
  listLoading.value = true
  try {
    const next = page.value + 1
    const data = await fetchPerformancesWithSummary({ page: next, page_size: PAGE_SIZE, ...filterParams() })
    if (version !== requestVersion || userId.value !== user) return
    items.value = [...items.value, ...data.items]
    received.value += data.items.length
    total.value = data.total
    page.value = next
  } catch (e) {
    if (version === requestVersion && userId.value === user) Toast.error(errText(e))
  } finally {
    if (version === requestVersion) listLoading.value = false
  }
}

const details = useTemplateRef<HTMLElement>('details')
const summaryCard = useTemplateRef<InstanceType<typeof F7Card>>('summaryCard')

function focusAnchor(): void {
  const target = props.summaryOnly ? summaryCard.value?.$el as HTMLElement | undefined : details.value
  target?.scrollIntoView?.()
  target?.focus({ preventScroll: true })
}

defineExpose({ refresh: load, focusAnchor, sales, salesState })

/** 失败一律原样显示错误文本（`request()` 抛出的 message 就是后端 `msg` 原文）。 */
function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}
</script>

<template>
  <F7Card
    v-if="canRead"
    :id="summaryOnly ? 'performance' : undefined"
    ref="summaryCard"
    :tabindex="summaryOnly ? -1 : undefined"
    role="region"
    aria-label="绩效汇总"
    class="m-perf-summary"
    :class="{ 'm-perf-summary--orders': summaryOnly }"
    data-testid="perf-summary"
  >
    <div class="m-perf-summary__item">
      <span class="m-perf-summary__label">{{ summaryOnly ? '预算' : '预计绩效' }}</span>
      <span class="m-perf-summary__value" data-testid="perf-summary-temp">
        {{ summary?.temp_amount ?? '—' }}
      </span>
    </div>
    <div class="m-perf-summary__item">
      <span class="m-perf-summary__label">{{ summaryOnly ? '实算' : '生效绩效' }}</span>
      <span class="m-perf-summary__value" data-testid="perf-summary-bonus">
        {{ summary?.bonus_amount ?? '—' }}
      </span>
    </div>
    <template v-if="summaryOnly">
      <div class="m-perf-summary__item">
        <span class="m-perf-summary__label">未结</span>
        <span class="m-perf-summary__value">
          {{ summary?.unsettled_bonus_amount ?? '—' }}
        </span>
      </div>
      <div class="m-perf-summary__item">
        <span class="m-perf-summary__label">已结</span>
        <span class="m-perf-summary__value">
          {{ summary?.settled_bonus_amount ?? '—' }}
        </span>
      </div>
    </template>
    <template v-else-if="showOrderMoney">
      <div class="m-perf-summary__item">
        <span class="m-perf-summary__label">订单金额</span>
        <span class="m-perf-summary__value" data-testid="perf-summary-order-price">
          {{ summary?.order_price ?? '—' }}
        </span>
      </div>
      <div class="m-perf-summary__item">
        <span class="m-perf-summary__label">记录退款金额</span>
        <span class="m-perf-summary__value" data-testid="perf-summary-refund">
          {{ summary?.refund_fee ?? '—' }}
        </span>
      </div>
      <div class="m-perf-summary__item">
        <span class="m-perf-summary__label">预计差额利润</span>
        <span class="m-perf-summary__value" data-testid="perf-summary-profit-expected">
          {{ summary?.profit_expected ?? '—' }}
        </span>
      </div>
      <div class="m-perf-summary__item">
        <span class="m-perf-summary__label">已生效差额利润</span>
        <span class="m-perf-summary__value" data-testid="perf-summary-profit-actual">
          {{ summary?.profit_actual ?? '—' }}
        </span>
      </div>
    </template>
    <div v-if="summaryOnly && loadState === 'error'" class="m-perf-summary__error" role="alert">
      <p class="m-perf__retry-text">{{ loadError }}</p>
      <F7Button type="button" round outline small @click="load">重试加载绩效</F7Button>
    </div>
  </F7Card>

  <slot />

  <section v-if="canRead && !summaryOnly" id="performance" ref="details" class="m-perf" tabindex="-1" aria-labelledby="m-perf-title">
    <h2 id="m-perf-title" class="m-perf__title">绩效明细</h2>
    <!-- 服务端筛选行：筛选变更 ⇒ 页码重置回 1 重取；summary 吃同一份筛后 queryset ⇒ 统计自动跟着变 -->
    <div class="m-perf-filters">
      <MobileSearch v-model="keyword" placeholder="搜单号 / 角色名" data-testid="perf-keyword" />
      <MobileSelect
        v-model="settleFilter"
        :options="SETTLE_OPTIONS"
        label="结算状态"
        data-testid="perf-settle-filter"
      />
      <MobileSelect
        v-model="statusFilter"
        :options="STATUS_OPTIONS"
        label="订单状态"
        data-testid="perf-status-filter"
      />
    </div>

    <!-- 首屏加载中 -->
    <div v-if="loadState === 'loading'" class="m-perf__loading" data-testid="perf-loading" role="status">
      <F7Preloader :size="18" aria-hidden="true" /> 加载中…
    </div>

    <template v-else-if="loadState === 'ready' || items.length > 0">
      <MobilePagedList :async-loading="asyncLoading" data-testid="perf-list" @load-more="onLoadMore">
        <F7Card
          v-for="row in items"
          :key="row.id"
          class="m-perf-card m-card"
          :data-order-no="row.order_no"
        >
          <F7CardHeader class="m-perf-card__head">
            <!-- ⭐ D-0301：单号 = 展示单号（来源号非空取它、空回退内置号）。 -->
            <span class="m-perf-card__no">单号 {{ row.source_order_no || row.order_no }}</span>
            <F7Badge class="m-perf-card__status" data-testid="perf-row-status">
              {{ orderStatusLabel(row.order_status) ?? row.order_status }}
            </F7Badge>
          </F7CardHeader>
          <F7CardContent :padding="false">
            <p class="m-perf-card__project">{{ row.dl_project }}</p>
            <div class="m-perf-card__amounts">
              <span class="m-perf-card__amount">
                预计 <b data-testid="perf-row-temp">{{ row.temp_amount }}</b> 元
              </span>
              <span class="m-perf-card__amount">
                实际 <b data-testid="perf-row-bonus">{{ row.bonus_amount }}</b> 元
              </span>
            </div>
          </F7CardContent>
          <F7CardFooter class="m-perf-card__foot">
            <span class="m-perf-card__meta">{{ row.emp_type }}</span>
            <span
              class="m-perf-card__meta"
              :class="{ 'm-perf-card__meta--settled': row.settle_status === '已结账' }"
              data-testid="perf-row-settle"
            >
              {{ row.settle_status }}
            </span>
            <span class="m-perf-card__time">{{ formatDateTime(row.created_at) }}</span>
          </F7CardFooter>
        </F7Card>
      </MobilePagedList>

      <p v-if="items.length === 0" class="m-perf__end" data-testid="perf-empty">暂无绩效记录</p>
    </template>

    <!-- 取数失败：错误原样 + 重试（M2 spec §5「失败重试」） -->
    <section v-if="loadState === 'error'" class="m-perf__retry" data-testid="perf-error">
      <p class="m-perf__retry-text">{{ loadError }}</p>
      <F7Button type="button"
        class="m-perf__retry-btn" round outline small
        data-testid="perf-retry"
        @click="load"
      >
        重试
      </F7Button>
    </section>
  </section>
</template>

<style scoped>
.m-perf__title {
  margin: 22px 0 10px;
  font-size: 17px;
}

.m-perf-summary.card {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0;
  padding: 16px 0;
  row-gap: 12px;
  border-radius: 20px;
  border: 1px solid var(--m-border);
  background: #fff;
  margin-bottom: 14px;
}

.m-perf-summary__item:nth-of-type(even) {
  border-left: 1px solid var(--m-border);
}

.m-perf-summary--orders.card {
  grid-template-columns: repeat(4, minmax(0, 1fr));
  padding: 12px 0;
}

.m-perf-summary--orders .m-perf-summary__item {
  flex-direction: column-reverse;
  justify-content: flex-end;
  padding-inline: 4px;
}

.m-perf-summary--orders .m-perf-summary__item + .m-perf-summary__item {
  border-left: 1px solid var(--m-border);
}

.m-perf-summary--orders .m-perf-summary__value {
  font-size: clamp(14px, 4vw, 20px);
  max-width: 100%;
  overflow-wrap: anywhere;
  text-align: center;
}

.m-perf-summary__error {
  grid-column: 1 / -1;
  justify-self: center;
  text-align: center;
}

.m-perf-summary__item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  min-width: 0;
}

.m-perf-summary__label {
  font-size: 11px;
  color: var(--m-muted);
  white-space: nowrap;
}

.m-perf-summary__value {
  font-size: clamp(21px, 6vw, 30px);
  font-weight: 700;
  line-height: 1.2;
  color: var(--f7-text-color);
  letter-spacing: -.035em;
}

.m-perf-filters {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
}

.m-perf__loading,
.m-perf__end {
  margin: 0;
  padding: 32px 0;
  font-size: 13px;
  color: #9ca3af;
  text-align: center;
}

.m-perf-card {
  --f7-card-header-padding-horizontal: 0px;
  --f7-card-header-padding-vertical: 0px;
  --f7-card-header-min-height: 0px;
  --f7-card-footer-padding-horizontal: 0px;
  --f7-card-footer-padding-vertical: 0px;
  --f7-card-footer-min-height: 0px;
}

.m-perf-card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.m-perf-card__no {
  font-size: 12px;
  color: #6b7280;
  word-break: break-all;
}

.m-perf-card__status {
  --f7-badge-bg-color: var(--m-accent-surface);
  --f7-badge-text-color: var(--f7-theme-color);
}

.m-perf-card__project {
  margin: 8px 0 4px;
  font-size: 14px;
  color: #1f2329;
}

.m-perf-card__amounts {
  display: flex;
  gap: 16px;
  margin-top: 4px;
  font-size: 12px;
  color: #6b7280;
}

.m-perf-card__amount b {
  font-size: 14px;
  color: #ff6a00;
}

.m-perf-card__foot {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin-top: 8px;
}

.m-perf-card__meta {
  font-size: 12px;
  color: #6b7280;
}

.m-perf-card__meta--settled {
  color: #2ba471;
}

.m-perf-card__time {
  margin-left: auto;
  font-size: 12px;
  color: #9ca3af;
}

.m-perf__retry {
  margin-top: 8px;
  text-align: center;
}

.m-perf__retry-text {
  margin: 0 0 6px;
  font-size: 13px;
  color: #d54941;
}
</style>
