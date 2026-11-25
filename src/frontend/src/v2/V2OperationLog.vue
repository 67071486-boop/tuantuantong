<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「系统操作日志管理」v2（`/v2/operation-log`；系统维护批次 B 波，计划 §3 页 9）。
 * 旧页 = island 复刻页 `layui-island/pages/system-systemlog.ts`（对标 `/System/systemlog`，
 * 页标题旧版是「系统跟踪日志」）。重建为炭火页：6 筛选（游戏筛选 / 记录日期起止默认
 * 昨天 00:00:00~今天 23:59:59 / 日志分类 8 分类 / 相关人员远程单选显示昵称提交账号名 /
 * 系统订单号 key2 / 搜索关键词 key1）+ 搜索钮 + 工具栏 刷新列表；主表 8 列（来源订单号
 * 来源号优先、内置单号兜底 = D-0341；操作时间列头点击走服务端排序 created_at/-created_at，
 * 第三次点击取消）；服务端分页 limit 30、档位 [10,20,30,40,50,100]。取数走共享
 * fetchAuditLogs（toAuditLogParams 是唯一线上形状出口）。⛔ 不 import island 页模块。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import {
  fetchAuditActors,
  fetchAuditLogs,
  formatCreatedAt,
  LOG_TYPE_LABELS,
  type AuditActorRow,
  type AuditLogOrdering,
  type AuditLogRow,
} from '@/api/auditLogs'
import { fetchGames, type ConfigRow } from '@/api/config'

/* ── 逐字文案与常量（出处 = island 页同语义常量 / 证据件；本地重写不引模块）────── */

const EMPTY_TABLE_TEXT = '暂无数据'
/** 相关人员下拉占位（证据件 `<option value="">选择或输入</option>`）。 */
const ACTOR_PLACEHOLDER = '选择或输入'
/** 日志分类下拉（证据件 select 的 9 个 option 顺序逐字：0=全部 + 8 分类；号库相关在排团相关之后）。 */
const LOG_TYPE_OPTIONS: readonly { value: number; label: string }[] = [
  { value: 0, label: '全部' },
  { value: 10, label: LOG_TYPE_LABELS[10] ?? '' },
  { value: 20, label: LOG_TYPE_LABELS[20] ?? '' },
  { value: 30, label: LOG_TYPE_LABELS[30] ?? '' },
  { value: 40, label: LOG_TYPE_LABELS[40] ?? '' },
  { value: 50, label: LOG_TYPE_LABELS[50] ?? '' },
  { value: 60, label: LOG_TYPE_LABELS[60] ?? '' },
  { value: 5, label: LOG_TYPE_LABELS[5] ?? '' },
  { value: 70, label: LOG_TYPE_LABELS[70] ?? '' },
]
/** 服务端分页（island PAGE_LIMIT / PAGE_LIMITS 复刻值照旧）。 */
const PAGE_LIMIT = 30
const PAGE_LIMITS = [10, 20, 30, 40, 50, 100]

function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

/* ── 提示（页面内自绘 toast；info 1500ms / error 6000ms）────────────────────── */

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

/* ── 默认记录日期：昨天 00:00:00 ~ 今天 23:59:59（island defaultLogDateRange 同口径，
 *    本地重写不引 island 的 dateRange——各页偏移不许互相照抄）──────────────────── */

const pad2 = (n: number): string => String(n).padStart(2, '0')

function defaultLogDateRange(now: Date = new Date()): { sDate: string; eDate: string } {
  const yest = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1)
  return {
    sDate: `${yest.getFullYear()}-${pad2(yest.getMonth() + 1)}-${pad2(yest.getDate())} 00:00:00`,
    eDate: `${now.getFullYear()}-${pad2(now.getMonth() + 1)}-${pad2(now.getDate())} 23:59:59`,
  }
}

const initialRange = defaultLogDateRange()

/* ── 筛选（6 项照旧；DOM 名 sDate/eDate/logType/gameName/recUser/key1/key2 的 v2 化）─ */

const filters = reactive({
  gameName: '',
  sDate: initialRange.sDate,
  eDate: initialRange.eDate,
  logType: 0,
  actor: '',
  key2: '',
  key1: '',
})

function setSDate(v: unknown): void {
  filters.sDate = typeof v === 'string' ? v : ''
}

function setEDate(v: unknown): void {
  filters.eDate = typeof v === 'string' ? v : ''
}

/** 相关人员：远程单选，显示昵称、提交账号名（value = username）。 */
const actorOptions = ref<AuditActorRow[]>([])
const actorLoading = ref(false)

async function searchActors(query: string): Promise<void> {
  actorLoading.value = true
  try {
    actorOptions.value = await fetchAuditActors(query)
  } catch {
    actorOptions.value = []
  } finally {
    actorLoading.value = false
  }
}

/** 操作时间服务端排序：'' → asc(created_at) → desc(-created_at) → ''（取消），回第 1 页。 */
const sortState = ref<'' | AuditLogOrdering>('')
const sortTitle = computed(() =>
  sortState.value === 'created_at' ? '升序（旧在前），点击切换' : sortState.value === '-created_at' ? '降序（新在前），点击取消' : '点击按操作时间排序',
)

function onSortHeader(): void {
  sortState.value = sortState.value === '' ? 'created_at' : sortState.value === 'created_at' ? '-created_at' : ''
  void reload(1)
}

/* ── 取数（服务端分页；参数只经 toAuditLogParams 一条链）────────────────────── */

const rows = ref<AuditLogRow[]>([])
const deleteReason = ref<string | null>(null)
const total = ref(0)
const loading = ref(false)
const loadError = ref<string | null>(null)
const page = ref(1)
const pageSize = ref(PAGE_LIMIT)

async function reload(targetPage = 1): Promise<void> {
  loading.value = true
  try {
    const result = await fetchAuditLogs({
      stime: filters.sDate,
      etime: filters.eDate,
      // 0 = 全部：显式不发（toAuditLogParams 同判）。
      log_type: filters.logType === 0 ? undefined : filters.logType,
      game_name: filters.gameName,
      actor: filters.actor === '' ? [] : [filters.actor],
      order_no: filters.key2,
      key: filters.key1,
      include_delete_reason: true,
      ordering: sortState.value === '' ? undefined : sortState.value,
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

/* ── 单元格展示（island toLogRow 同口径重写；Vue 插值自带转义）────────────────── */

/** 日志分类 code → 中文；认不出回退「全部」（island logTypeLabel 同式）。 */
function logTypeLabel(code: number): string {
  return LOG_TYPE_LABELS[code] ?? '全部'
}

/** 来源订单号 = 来源号优先、内置单号兜底（D-0341，island toLogRow 同式）。 */
function soureOrderNo(row: AuditLogRow): string {
  return row.source_order_no || (row.order_no ?? '')
}

/** 操作用户显示昵称；老数据没有 actor_display 时回退账号名（island 同式，兜底保留）。 */
function actorName(row: AuditLogRow): string {
  return row.actor_display ?? row.actor ?? ''
}

/** 空态 colspan = 8 列。 */
const COLSPAN_ALL = 8

const games = ref<ConfigRow[]>([])

onMounted(() => {
  void (async () => {
    // 相关人员首屏先拉一次空 key（后端回前 50 个）；后续输入走远程搜索。
    void searchActors('')
    try {
      // 游戏筛选换成我方游戏（对标那 13 个名字是它的租户数据，照旧不搬）。
      games.value = await fetchGames()
    } catch {
      // 字典取不到：下拉只剩「全部游戏」，列表照常（island 同口径降级）。
    }
    await reload(1)
  })()
})
</script>

<template>
  <div class="cv2-page" data-v2-page="operation-log">
    <!-- 筛选区：6 项 + 搜索钮（连体网格组形态照 V2OrderShipLog） -->
    <section class="cv2-panel cv2-filter" aria-label="筛选" data-v2="filters">
      <div class="lg-wrap">
        <div class="lg-group" data-v2-filter-group="game">
          <span class="lg-label">游戏筛选</span>
          <span class="lg-cell"><el-select v-model="filters.gameName" filterable placeholder="全部游戏" class="lg-w150" data-v2-filter="game" :teleported="false">
            <el-option value="" label="全部游戏" />
            <el-option v-for="g in games" :key="g.id" :value="g.name" :label="g.name" />
          </el-select></span>
        </div>
        <div class="lg-group" data-v2-filter-group="date">
          <span class="lg-label">记录日期</span>
          <span class="lg-cell lg-datetimecell" data-v2-filter="sdate"><el-date-picker
            :model-value="filters.sDate"
            type="datetime"
            value-format="YYYY-MM-DD HH:mm:ss"
            placeholder="开始时间"
            :teleported="false"
            @update:model-value="setSDate"
          /></span>
          <span class="lg-dash">-</span>
          <span class="lg-cell lg-datetimecell" data-v2-filter="edate"><el-date-picker
            :model-value="filters.eDate"
            type="datetime"
            value-format="YYYY-MM-DD HH:mm:ss"
            placeholder="结束时间"
            :teleported="false"
            @update:model-value="setEDate"
          /></span>
        </div>
        <div class="lg-group" data-v2-filter-group="log-type">
          <span class="lg-label">日志分类</span>
          <span class="lg-cell"><el-select v-model="filters.logType" class="lg-w130" data-v2-filter="log-type" :teleported="false">
            <el-option v-for="o in LOG_TYPE_OPTIONS" :key="o.value" :value="o.value" :label="o.label" />
          </el-select></span>
        </div>
        <div class="lg-group" data-v2-filter-group="actor">
          <span class="lg-label">相关人员</span>
          <span class="lg-cell"><el-select
            v-model="filters.actor"
            filterable
            remote
            clearable
            :remote-method="searchActors"
            :loading="actorLoading"
            :placeholder="ACTOR_PLACEHOLDER"
            class="lg-w160"
            data-v2-filter="actor"
            :teleported="false"
          >
            <el-option v-for="a in actorOptions" :key="a.id" :value="a.username" :label="a.display_name" />
          </el-select></span>
        </div>
        <div class="lg-group lg-grow" data-v2-filter-group="key2">
          <span class="lg-label">系统订单号</span>
          <span class="lg-cell lg-cell--grow"><el-input
            v-model="filters.key2"
            placeholder="请输入系统订单号/来源单号"
            data-v2-filter="key2"
            @keydown.enter="onSearch"
          /></span>
        </div>
        <div class="lg-group lg-grow" data-v2-filter-group="key1">
          <span class="lg-label">搜索关键词</span>
          <span class="lg-cell lg-cell--grow"><el-input
            v-model="filters.key1"
            placeholder="事项关键词"
            data-v2-filter="key1"
            @keydown.enter="onSearch"
          /></span>
        </div>
        <div class="cv2-filter-actions">
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--lg cv2-btn--find cv2-btn--fill" data-v2="search" :disabled="loading" @click="onSearch">搜索</button>
        </div>
      </div>
    </section>

    <!-- 工具栏：唯一的「刷新列表」 -->
    <div class="cv2-toolbar" data-v2="toolbar">
      <div class="ttb-strip">
      <button type="button" class="cv2-btn cv2-btn--primary" data-v2="refresh" :disabled="loading" @click="reload(1)">刷新列表</button>
      </div>
    </div>

    <!-- 主表 8 列（列头/宽度/对齐照旧页 buildLogCols；操作时间列头点击 = 服务端排序） -->
    <section class="cv2-panel cv2-table-panel" aria-label="操作日志列表">
      <table class="cv2-table">
        <colgroup>
          <col style="width: 150px" />
          <col style="width: 150px" />
          <col style="width: 200px" />
          <col style="width: 150px" />
          <col style="width: 150px" />
          <col />
          <col style="width: 150px" />
          <col style="width: 160px" />
        </colgroup>
        <thead>
          <tr>
            <th>相关ID</th>
            <th class="cv2-th-center">日志分类</th>
            <th>来源订单号</th>
            <th>订单角色名</th>
            <th class="cv2-th-center">所属游戏</th>
            <th>操作说明</th>
            <th class="cv2-th-center">操作用户</th>
            <th class="cv2-th-center lg-th-sort" :title="sortTitle" data-v2="sort-time" @click="onSortHeader">
              操作时间
              <span v-if="sortState !== ''" class="lg-sort-arrow" data-v2="sort-arrow">{{ sortState === 'created_at' ? '↑' : '↓' }}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, index) in rows" :key="`${row.target_id}-${row.created_at}-${index}`" class="cv2-row" :data-v2-row="index">
            <td class="cv2-num">{{ row.target_id }}</td>
            <td class="cv2-td-center" :data-v2-log-type="row.log_type">{{ logTypeLabel(row.log_type) }}</td>
            <td class="cv2-num" :data-v2-soure-order-no="index">{{ soureOrderNo(row) }}</td>
            <td>{{ row.game_role_name ?? '' }}</td>
            <td class="cv2-td-center">{{ row.game_name ?? '' }}</td>
            <td class="lg-summary" :title="row.summary">
              <div v-if="row.delete_reason" class="lg-summary-with-reason">
                <span>{{ row.summary }}</span>
                <button type="button" class="cv2-btn cv2-btn--quiet lg-reason-btn" @click="deleteReason = row.delete_reason ?? null">
                  查看原因
                </button>
              </div>
              <template v-else>{{ row.summary }}</template>
            </td>
            <td class="cv2-td-center">{{ actorName(row) }}</td>
            <td class="cv2-td-center cv2-td-time">{{ formatCreatedAt(row.created_at) }}</td>
          </tr>
          <tr v-if="rows.length === 0">
            <td :colspan="COLSPAN_ALL" class="cv2-empty" data-v2="empty">
              <template v-if="loading">正在加载…</template>
              <template v-else-if="loadError !== null">加载失败：{{ loadError }}</template>
              <template v-else>{{ EMPTY_TABLE_TEXT }}</template>
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
          :current-page="page"
          :page-size="pageSize"
          :total="total"
          layout="prev, pager, next"
          background
          @current-change="onPageChange"
        />
      </div>
    </section>

    <el-dialog
      :model-value="deleteReason !== null"
      title="删除原因"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      @update:model-value="deleteReason = null"
    >
      <p class="cv2-dlg-text lg-reason-text">{{ deleteReason }}</p>
    </el-dialog>

    <!-- 炭火 toast -->
    <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
      {{ toast.text }}
    </div>
  </div>
</template>

<style scoped>
/* 本页新增的形状收在这里；色一律走 carbon.css 的 .ttv2 token（类或 var 引用，⛔ 不写裸色值）。
 * 筛选连体网格（lg-*）照 V2OrderShipLog 的 flt-* 同款 scoped 重声明。 */

.cv2-num {
  font-family: var(--tt2-mono);
  font-variant-numeric: tabular-nums;
}

/* ── 筛选区连体网格组（紧凑 = 间距收紧：组间 6/8、面板内边距 10/12，不压组高）。── */
.cv2-page .cv2-filter {
  padding: 10px 12px;
}

.lg-wrap {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 8px;
  align-items: center;
}

.lg-group {
  display: inline-flex;
  align-items: stretch;
  height: 32px;
  border: 1px solid var(--tt2-line);
  border-radius: 6px;
  background: var(--tt2-panel);
  transition: border-color 0.15s;
}

.lg-group > :first-child {
  border-radius: 5px 0 0 5px;
}

.lg-group > :last-child {
  border-radius: 0 5px 5px 0;
}

.lg-group:focus-within {
  border-color: var(--tt2-primary);
}

.lg-label {
  display: inline-flex;
  align-items: center;
  padding: 0 0 0 10px;
  font-size: 13px;
  font-weight: 500;
  color: color-mix(in srgb, var(--tt2-ink) 35%, var(--tt2-dim));
  white-space: nowrap;
}

.lg-cell {
  display: inline-flex;
  align-items: center;
  min-width: 0;
}

.lg-cell--grow {
  flex: 1;
}

.lg-grow {
  flex: 1 1 240px;
}

.lg-grow .lg-cell--grow {
  min-width: 200px;
}

.lg-datetimecell {
  width: 158px;
}

.lg-datetimecell :deep(.el-date-editor.el-input) {
  width: 100%;
}

.lg-dash {
  display: inline-flex;
  align-items: center;
  padding: 0 6px;
  font-size: 13px;
  color: var(--tt2-dim);
}

/* 组内 EP 控件去自身描边/圆角（组框承担边界），高度钉 32px。 */
.lg-group :deep(.el-input__wrapper),
.lg-group :deep(.el-select__wrapper) {
  height: 32px;
  min-height: 32px;
  border-radius: 0;
  background: var(--tt2-panel);
  box-shadow: none;
}

.lg-group :deep(.el-select__wrapper.is-focused),
.lg-group :deep(.el-input__wrapper.is-focus) {
  box-shadow: none;
}

.lg-w150 { width: 150px; }
.lg-w130 { width: 130px; }
.lg-w160 { width: 160px; }

/* ── 操作时间排序列头（可点 + 箭头指示）。── */
.lg-th-sort {
  cursor: pointer;
  user-select: none;
}

.lg-sort-arrow {
  margin-left: 2px;
  font-size: 11px;
  color: var(--tt2-primary-deep);
}

/* 操作说明列长文本单行截断（44px 行高家族口径）。 */
.lg-summary {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.lg-summary-with-reason {
  display: flex;
  align-items: center;
  gap: 8px;
}

.lg-summary-with-reason > span {
  overflow: hidden;
  text-overflow: ellipsis;
}

.lg-reason-btn {
  flex-shrink: 0;
}

.lg-reason-text {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

/* 面板放开限高（原 max-height 冻结窗格形态，四记录页同款）：行多往下自然扩，分页条随之落底。 */
.cv2-table-panel {
  min-height: 320px;
}
</style>
