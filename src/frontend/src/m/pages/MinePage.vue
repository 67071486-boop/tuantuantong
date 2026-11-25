<script setup lang="ts">
import MobileSearch from '../components/MobileSearch.vue'
import MobileSelect from '../components/MobileSelect.vue'
import {
  f7Badge as F7Badge,
  f7Block as F7Block,
  f7Button as F7Button,
  f7Card as F7Card,
  f7CardContent as F7CardContent,
  f7CardFooter as F7CardFooter,
  f7CardHeader as F7CardHeader,
  f7Preloader as F7Preloader,
} from 'framework7-vue'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch, useTemplateRef } from 'vue'
import { useRouter } from 'vue-router'
import { Toast } from '../framework7Ui'
import MobilePullToRefresh from '../components/MobilePullToRefresh.vue'
import { useSessionStore } from '@/stores/session'
import type { Order } from '@/api/orders'
import { fetchAllMyOrders, type MyOrderItem } from '@/api/myOrders'
import { orderStatusLabel, type OrderStatus } from '@/config/orderStatus'
import SubmitWorkDialog from '../components/SubmitWorkDialog.vue'
import PerformancePage from './PerformancePage.vue'
import { canReadWorkerOrders, canReadWorkerPerformance, MINE_PERFORMANCE_HASH } from '../tabs'
import { formatDateTime } from '../utils/datetime'

const SUBMIT_NODE = 'order.submit_work'
/** 前端状态闸：只有代练中的单给两键（后端 1002 兜底，前端这道只是省一趟明知会被拒的请求）。 */
const IN_PROGRESS = 'IN_PROGRESS'
/** 「已完结」档的**排除集**：其余状态（含后端将来新增的）都落「已完结」。 */
const DONE_EXCLUDED = new Set<string>([IN_PROGRESS, 'PENDING_REVIEW', 'ISSUE'])

/** 关键字防抖窗长（与大厅页 `KEYWORD_DEBOUNCE_MS` 同值：300ms）。 */
const KEYWORD_DEBOUNCE_MS = 300

const session = useSessionStore()
const router = useRouter()
const canReadOrders = computed(() => canReadWorkerOrders(session.canNode))
const canReadPerformance = computed(() => canReadWorkerPerformance(session.canNode))
const performance = useTemplateRef<InstanceType<typeof PerformancePage>>('performance')

/** 顶部 Tabs 的档位（value 判据源）；**展示文案**带计数，见 `tabList`。 */
const MINE_TABS: { value: string; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'IN_PROGRESS', label: orderStatusLabel('IN_PROGRESS') },
  { value: 'PENDING_REVIEW', label: orderStatusLabel('PENDING_REVIEW') },
  { value: 'ISSUE', label: orderStatusLabel('ISSUE') },
  { value: 'done', label: '已完结' },
]
const TAB_VALUES = new Set(MINE_TABS.map((t) => t.value))

// 只把筛选状态存在这次列表的浏览历史里，换账号时不沿用。
const account = computed(() => session.user ? `${session.user.identity_plane}:${session.user.id}` : null)
const history = router.options.history
const saved = history.state.mineView
const restored = account.value && saved && typeof saved === 'object' && !Array.isArray(saved)
  && saved.account === account.value ? saved : null

/* ── 列表状态 ── */
const items = ref<MyOrderItem[]>([])
type MineRow = Pick<MyOrderItem, 'id' | 'order_no' | 'source_order_no' | 'status' | 'game_name'
  | 'project_text' | 'is_urgent' | 'temp_amount' | 'bonus_amount' | 'created_at' | 'settle_status' | 'emp_type'
> & { isOrder: boolean; gameRole?: string }

const rows = computed<MineRow[]>(() => {
  const orderIds = new Set(items.value.map((row) => row.id))
  return [
    ...items.value.map((row) => ({ ...row, isOrder: true, gameRole: row.game_role_name })),
    ...(canReadPerformance.value ? (performance.value?.sales ?? []) : [])
      .filter((row) => !orderIds.has(row.id))
      .map((row) => ({
        id: row.id,
        isOrder: false,
        order_no: row.order_no,
        source_order_no: row.source_order_no,
        status: row.order_status,
        game_name: row.game_name ?? '',
        gameRole: row.game_role,
        project_text: row.dl_project,
        is_urgent: false,
        temp_amount: row.temp_amount,
        bonus_amount: row.bonus_amount,
        created_at: row.created_at,
        settle_status: row.settle_status,
        emp_type: row.emp_type,
      })),
  ]
})
/** 取数状态：`loading`（首屏）→ `ready` / `error`。 */
const loadState = ref<'loading' | 'ready' | 'error'>('loading')
const loadError = ref('')
const salesState = computed(() => canReadPerformance.value ? performance.value?.salesState ?? 'loading' : 'ready')
const awaitingRows = computed(() => loadState.value === 'loading' || salesState.value === 'loading')
const refreshing = ref(false)
const activeTab = ref(typeof restored?.tab === 'string' && TAB_VALUES.has(restored.tab) ? restored.tab : 'all')
/** 全量重取的在途闸（首屏 / 重试 / 下拉共用；同一时刻只允许一条）。 */
let loadBusy = false
let orderVersion = 0

/* ── 本地筛选（真机反馈第一批第 2 条）：搜索 + 游戏，先筛；tab 再分组（叠加关系） ── */

/**
 * 搜索框原值（受控）；真正参与筛选的是防抖后的 `appliedKeyword`。
 * 判据 = 单号 / 项目文本 / 游戏名**任一「包含」**即命中，**不区分大小写**。
 */
const keyword = ref(typeof restored?.keyword === 'string' ? restored.keyword : '')
const appliedKeyword = ref(keyword.value)
let keywordTimer: number | null = null
let unmounted = false

// 关键字 300ms 防抖（大厅页 `watch(keyword)` 同款；本地筛选不发请求，到点只重算）。
watch(keyword, (next) => {
  if (keywordTimer !== null) window.clearTimeout(keywordTimer)
  keywordTimer = window.setTimeout(() => {
    keywordTimer = null
    if (next === appliedKeyword.value) return
    appliedKeyword.value = next
  }, KEYWORD_DEBOUNCE_MS)
})

onBeforeUnmount(() => {
  unmounted = true
  if (keywordTimer !== null) window.clearTimeout(keywordTimer)
})

/** 游戏下拉的当前值；`''` = 「全部游戏」哨兵。 */
const gameFilter = ref(typeof restored?.game === 'string' ? restored.game : '')
const SETTLE_OPTIONS = [
  { value: '', label: '全部结算' },
  { value: '未结账', label: '未结账' },
  { value: '已结账', label: '已结账' },
]
const settleFilter = ref(typeof restored?.settle === 'string' ? restored.settle : '')

/** 游戏下拉的选项 = 已加载数据里的 distinct `game_name`（数据本来就是全量取的 ⇒ 本地 distinct 即全集）。 */
const gameOptions = computed(() => {
  const names = new Set<string>()
  for (const it of rows.value) {
    if (it.game_name !== '') names.add(it.game_name)
  }
  return [
    { value: '', label: '全部游戏' },
    ...Array.from(names, (name) => ({ value: name, label: name })),
  ]
})

// 搜索、游戏、结算状态可以叠加。
const filteredRows = computed(() => {
  const kw = appliedKeyword.value.trim().toLowerCase()
  const game = gameFilter.value
  return rows.value.filter((it) => {
    if (canReadPerformance.value && settleFilter.value !== '' && it.settle_status !== settleFilter.value) return false
    if (game !== '' && it.game_name !== game) return false
    if (kw === '') return true
    return (
      // ⭐ D-0301：单号搜索**两个号都认**（来源号能搜到 + 系统号仍能搜到）。
      it.order_no.toLowerCase().includes(kw) ||
      (it.source_order_no || '').toLowerCase().includes(kw) ||
      it.project_text.toLowerCase().includes(kw) ||
      it.gameRole?.toLowerCase().includes(kw) ||
      it.game_name.toLowerCase().includes(kw)
    )
  })
})

// 状态计数使用已通过其它筛选的记录。
function countOf(tabValue: string): number {
  if (tabValue === 'all') return filteredRows.value.length
  if (tabValue === 'done') {
    return filteredRows.value.filter((it) => !DONE_EXCLUDED.has(it.status)).length
  }
  return filteredRows.value.filter((it) => it.status === tabValue).length
}

/** Tabs 的展示档（label 带计数，如「代练中 (3)」；value 不变 ⇒ 判据与既有切 tab 逻辑共用）。 */
const tabList = computed(() => MINE_TABS.map((t) => ({
  value: t.value,
  label: `${t.label} (${countOf(t.value)})`,
})))

const canSubmit = computed(() => session.canNode([SUBMIT_NODE]))

const visibleRows = computed(() => {
  if (activeTab.value === 'all') return filteredRows.value
  if (activeTab.value === 'done') {
    return filteredRows.value.filter((it) => !DONE_EXCLUDED.has(it.status))
  }
  return filteredRows.value.filter((it) => it.status === activeTab.value)
})

/* ── 取数（全量） ── */

async function load(): Promise<void> {
  if (!canReadOrders.value || loadBusy) return
  loadBusy = true
  const version = ++orderVersion
  if (loadState.value === 'error' && items.value.length === 0) loadState.value = 'loading'
  loadError.value = ''
  try {
    const orders = await fetchAllMyOrders()
    if (version !== orderVersion || unmounted) return
    items.value = orders
    loadState.value = 'ready'
  } catch (e) {
    if (version !== orderVersion || unmounted) return
    loadState.value = 'error'
    loadError.value = errText(e)
    Toast.error(errText(e))
  } finally {
    if (version === orderVersion) loadBusy = false
  }
}

watch([account, canReadOrders], ([nextAccount, allowed], [previousAccount]) => {
  orderVersion += 1
  loadBusy = false
  items.value = []
  loadState.value = 'loading'
  if (nextAccount !== previousAccount) {
    activeTab.value = 'all'
    keyword.value = ''
    if (keywordTimer !== null) window.clearTimeout(keywordTimer)
    appliedKeyword.value = ''
    gameFilter.value = ''
    settleFilter.value = ''
  }
  dialogVisible.value = false
  if (allowed) void load()
})

async function onPullRefresh(): Promise<void> {
  if (refreshing.value) return
  refreshing.value = true
  try {
    await Promise.all([load(), performance.value?.refresh()])
  } finally {
    refreshing.value = false
  }
}

/* ── 卡片 → 详情 ── */

const page = useTemplateRef<HTMLElement>('page')
const scrollContainer = () => page.value?.closest<HTMLElement>('.m-shell__scroll') ?? page.value?.closest<HTMLElement>('#framework7-root')

async function scrollToPerformance(): Promise<void> {
  await nextTick()
  if (unmounted || router.currentRoute.value.hash !== MINE_PERFORMANCE_HASH) return
  performance.value?.focusAnchor()
}

watch(() => router.currentRoute.value.hash, scrollToPerformance, { flush: 'post' })

function openDetail(row: MineRow): void {
  if (!row.isOrder || !canReadOrders.value) return
  if (account.value) {
    history.replace(router.currentRoute.value.fullPath, {
      mineView: {
        account: account.value, keyword: keyword.value, game: gameFilter.value, settle: settleFilter.value,
        tab: activeTab.value, top: scrollContainer()?.scrollTop ?? window.scrollY,
      },
    })
  }
  void router.push({ name: 'm-order-detail', params: { orderNo: row.order_no } })
}

/* ── 交单 / 报问题（共用弹层） ── */

const dialogVisible = ref(false)
const dialogOrderNo = ref('')
const dialogTarget = ref<OrderStatus>('PENDING_REVIEW')

function openDialog(row: MineRow, target: OrderStatus): void {
  if (!row.isOrder || !canSubmit.value) return
  dialogOrderNo.value = row.order_no
  dialogTarget.value = target
  dialogVisible.value = true
}

/**
 * 弹层成功回调：用**响应载荷**就地更新卡片状态（载荷是流转后的完整订单，
 * E-0176：判据打在调用方真正看到的载荷上）——⛔ 不重取全量（任务书 T3 明确）。
 * 按**载荷里的 `order_no`** 找行（不是闭包里那张卡片），载荷说了算。
 */
function onDialogSuccess(order: Order): void {
  const row = items.value.find((it) => it.order_no === order.order_no)
  if (row) row.status = order.status
}

/** 失败**一律**原样显示错误文本（业务码与 HTTP 的 msg 都由 `api/http.ts` 映射成 `message`）。 */
function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

onMounted(async () => {
  const top = restored?.top
  const hasSavedPosition = typeof top === 'number' && Number.isFinite(top)
  if (!hasSavedPosition) void scrollToPerformance()
  await load()
  if (!hasSavedPosition || loadState.value !== 'ready') return
  await nextTick()
  if (unmounted) return
  const container = scrollContainer()
  if (container) container.scrollTop = top
  else window.scrollTo(0, top)
})
</script>

<template>
  <section ref="page" class="m-mine">
    <MobilePullToRefresh :refreshing="refreshing" @refresh="onPullRefresh">
      <MobileSearch
        v-if="canReadOrders"
        v-model="keyword"
        class="m-mine-search"
        placeholder="搜单号 / 项目 / 游戏 / 角色名"
        data-testid="mine-search"
      />
      <PerformancePage ref="performance" :summary-only="canReadOrders">
        <section v-if="canReadOrders" aria-label="订单列表">
          <div class="m-mine-filters" role="group" aria-label="游戏和结算筛选">
            <MobileSelect
              v-model="gameFilter"
              :options="gameOptions"
              label="筛选游戏"
              presentation="popover"
              data-testid="mine-game-filter"
            />
            <MobileSelect
              v-if="canReadPerformance"
              v-model="settleFilter"
              :options="SETTLE_OPTIONS"
              label="结算状态"
              presentation="popover"
              data-testid="mine-settle-filter"
            />
          </div>

          <div class="m-mine-segments" role="group" aria-label="订单状态">
            <F7Button
              v-for="tab in tabList"
              :key="tab.value"
              :data-value="tab.value"
              type="button"
              round
              :active="activeTab === tab.value"
              :aria-pressed="activeTab === tab.value"
              @click="activeTab = tab.value"
            >{{ tab.label }}</F7Button>
          </div>

          <div>
            <F7Card
              v-for="row in visibleRows"
              :key="row.id"
              outline-ios
              footer-divider
              class="m-mine-card"
              :class="{ 'm-mine-card--order': row.isOrder }"
              :data-order-no="row.order_no"
              @click="openDetail(row)"
            >
              <F7CardHeader class="m-mine-card__head">
                <F7Badge v-if="row.game_name" class="m-mine-card__game">
                  <span>{{ row.game_name }}</span>
                </F7Badge>
                <F7Badge v-if="!row.isOrder" class="m-mine-card__game">{{ row.emp_type }}</F7Badge>
                <F7Badge v-if="row.is_urgent" class="m-mine-card__urgent">加急</F7Badge>
                <F7Badge class="m-mine-card__status">{{ orderStatusLabel(row.status) }}</F7Badge>
              </F7CardHeader>
              <F7CardContent class="m-mine-card__content">
                <h3>
                  <F7Button v-if="row.isOrder" type="button" class="m-mine-card__project" @click.stop="openDetail(row)">
                    {{ row.project_text || '查看订单' }}
                  </F7Button>
                  <span v-else class="m-mine-card__project">{{ row.project_text || row.emp_type }}</span>
                </h3>
                <!-- ⭐ D-0301：单号行 = 展示单号（来源号非空取它、空回退内置号）。 -->
                <p class="m-mine-card__no">单号 {{ row.source_order_no || row.order_no }}</p>
              </F7CardContent>
              <F7CardFooter class="m-mine-card__foot">
                <div class="m-mine-card__meta">
                  <div v-if="canReadPerformance" class="m-mine-card__earnings">
                    <span>预计 <b>{{ row.temp_amount }}</b> 元</span>
                    <span>生效 <b>{{ row.bonus_amount }}</b> 元</span>
                  </div>
                  <span v-else class="m-mine-card__amount">{{ row.temp_amount }} 元</span>
                  <F7Badge
                    v-if="canReadPerformance"
                    class="m-mine-card__settlement"
                    :class="{ 'm-mine-card__settlement--paid': row.settle_status === '已结账' }"
                  >{{ row.settle_status }}</F7Badge>
                  <span class="m-mine-card__time">{{ row.isOrder ? '接手' : '记录' }} {{ formatDateTime(row.created_at) }}</span>
                </div>
                <!-- 权限和状态都满足时才显示操作。按钮点击与卡片跳转各走自己的入口。 -->
                <div v-if="row.isOrder && canSubmit && row.status === IN_PROGRESS" class="m-mine-card__actions">
                  <F7Button
                    type="button"
                    class="m-mine-card__report"
                    round
                    outline
                    color="red"
                    small
                    @click.stop="openDialog(row, 'ISSUE')"
                  >报问题</F7Button>
                  <F7Button
                    type="button"
                    class="m-mine-card__submit"
                    round
                    fill
                    small
                    @click.stop="openDialog(row, 'PENDING_REVIEW')"
                  >提交完成</F7Button>
                </div>
              </F7CardFooter>
            </F7Card>

            <F7Block v-if="awaitingRows && !refreshing" class="m-mine__loading" role="status">
              <F7Preloader :size="18" aria-hidden="true" />
              {{ loadState === 'loading' ? '正在加载订单…' : '正在加载绩效记录…' }}
            </F7Block>
            <F7Block v-else-if="loadState === 'ready' && salesState === 'ready' && !refreshing && visibleRows.length === 0" class="m-mine__end" role="status">
              {{ canReadPerformance ? '暂无符合条件的记录' : '暂无订单' }}
            </F7Block>
          </div>

          <!-- 取数失败：错误原样 + 重试（任务书 T3；msg 已 toast 过一遍，这里再留一条可重试的错误态） -->
          <F7Block v-if="loadState === 'error' && loadError" class="m-mine__retry" role="alert">
            <p class="m-mine__retry-text">{{ loadError }}</p>
            <F7Button type="button" class="m-mine__retry-btn" round outline small @click="load">重试</F7Button>
          </F7Block>

          <SubmitWorkDialog
            v-model:visible="dialogVisible"
            :order-no="dialogOrderNo"
            :initial-target="dialogTarget"
            @success="onDialogSuccess"
          />
        </section>
      </PerformancePage>
    </MobilePullToRefresh>
  </section>
</template>

<style scoped>
.m-mine-search.searchbar {
  --f7-searchbar-height: 44px;
  --f7-searchbar-input-height: 44px;
  margin-block-end: 8px;
}

.m-mine-filters {
  --m-select-max-width: min(145px, 45vw);
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
}

.m-mine-filters :deep(.m-select .m-select__trigger) {
  --f7-button-height: 38px;
}

.m-mine-card {
  --f7-card-outline-border-color: var(--m-border);
  --f7-card-header-border-color: var(--m-border);
  --f7-card-content-padding-horizontal: 14px;
  --f7-card-header-padding-horizontal: var(--f7-card-content-padding-horizontal);
  --f7-card-header-padding-vertical: 0px;
  --f7-card-header-min-height: 0px;
  --f7-card-footer-padding-horizontal: var(--f7-card-content-padding-horizontal);
  --f7-card-footer-padding-vertical: 10px;
  --f7-card-footer-min-height: 42px;
  margin-block-end: 10px;
}

.m-mine-card--order {
  cursor: pointer;
}

.m-mine-card__head {
  justify-content: start;
  flex-wrap: wrap;
  gap: 6px;
  padding-block-start: 12px;
}

.m-mine-card__game {
  --f7-badge-bg-color: #f3f1f6;
  --f7-badge-text-color: var(--m-muted);
  min-width: 0;
  max-width: 100%;
}

.m-mine-card__game span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.m-mine-card__urgent {
  --f7-badge-bg-color: #fff1e9;
  --f7-badge-text-color: #a84525;
}

.m-mine-card__status {
  --f7-badge-bg-color: var(--m-accent-surface);
  --f7-badge-text-color: var(--f7-theme-color);
  margin-inline-start: auto;
}

.m-mine-card__content {
  --f7-card-content-padding-vertical: 9px;
}

.m-mine-card__content h3 {
  margin: 0;
}

.m-mine-card__project {
  color: var(--f7-text-color);
  --f7-button-text-color: var(--f7-text-color);
  --f7-button-padding-horizontal: 0px;
  display: -webkit-box;
  overflow: hidden;
  width: auto;
  height: auto;
  font-size: 17px;
  font-weight: 660;
  line-height: 1.35;
  text-align: start;
  white-space: normal;
  overflow-wrap: anywhere;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.m-mine-card__no {
  margin: 5px 0 0;
  font-size: 12px;
  color: var(--m-muted);
  overflow-wrap: anywhere;
}

.m-mine-card__foot {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 8px;
}

.m-mine-card__meta {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: 2px 8px;
}

.m-mine-card__amount {
  font-size: 18px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
  color: var(--f7-text-color);
}

.m-mine-card__earnings {
  display: flex;
  flex-wrap: wrap;
  column-gap: 12px;
  color: var(--m-muted);
  font-size: 12px;
}

.m-mine-card__earnings b {
  font-size: 16px;
  font-variant-numeric: tabular-nums;
  color: var(--f7-text-color);
}

.m-mine-card__settlement {
  --f7-badge-bg-color: #f3f1f6;
  --f7-badge-text-color: var(--m-muted);
}

.m-mine-card__settlement--paid {
  --f7-badge-bg-color: #e4f4e9;
  --f7-badge-text-color: #236345;
}

.m-mine-card__time {
  font-size: 12px;
  color: var(--m-muted);
}

.m-mine-card__actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.m-mine-card__actions :deep(.button) {
  width: auto;
}

.m-mine__loading,
.m-mine__end,
.m-mine__retry {
  margin: 0;
  padding-block: 28px;
  font-size: 13px;
  color: var(--m-muted);
  text-align: center;
}

.m-mine__loading {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 8px;
}

.m-mine__retry {
  display: grid;
  justify-items: center;
  gap: 8px;
}

.m-mine__retry-text {
  margin: 0;
  color: var(--f7-color-red);
}

.m-mine__retry-btn {
  width: auto;
}

.m-mine-segments {
  display: flex;
  gap: 5px;
  margin-bottom: 13px;
  padding-block: 2px 4px;
  overflow-x: auto;
  scrollbar-width: none;
}

.m-mine-segments :deep(.button) {
  --f7-button-height: 34px;
  --f7-button-font-weight: 550;
  --f7-button-text-color: var(--m-muted);
  --f7-button-bg-color: #f3f1f6;
  flex: 0 0 auto;
  width: auto;
  padding-inline: 9px;
  font-size: 12px;
  white-space: nowrap;
}

.m-mine-segments :deep(.button[aria-pressed="true"]) {
  --f7-button-bg-color: var(--m-accent-surface);
  --f7-button-text-color: var(--f7-theme-color);
}
</style>
