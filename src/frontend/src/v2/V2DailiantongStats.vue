<script setup lang="ts">
/**
 * 「代练通按日统计」v2（`/v2/dailiantong-stats`；路由 name `v2-dailiantong-stats`）。
 *
 * 旧页 = island **自有功能页** `layui-island/pages/dailiantong-stats.ts`（对标物没有这一页，
 * 不是复刻）：按天汇总当前租户绑定代练通账号的订单统计（上游 `LevelOrderStat` 裸对象，
 * 键可多可少）。v2 等价重建：说明句 / 日期窗口（默认最近 7 天含今天，**页面算好摆进框**，
 * ⛔ 不靠后端默认）/ 方向（1 发单 / 0 接单）/ 查询；列动态四条规则照旧页 `columnsFor`——
 * ①已知 13 列按文档顺序；②回包多出的键追加末尾、表头 = 原始键名（⛔ 不猜中文）；
 * ③行缺键显示「—」（⛔ 不补 0）；④已知键在所有行与合计都缺失 → 整列不画，空回包退回
 * 全部 13 列表头；有 `total` 时表尾追加「合计」行。状态行三态（加载中 / 共 N 天数据 /
 * 空区间 / 加载失败 + 重试）逐字照旧页 `MESSAGES`。只读页，无任何写动作。
 * 有意差异（设计稿 §4.3）：金额/数量列右对齐等宽（`cv2-td-num`，旧页无对齐处理）；视觉炭火。
 * ⛔ 不 import island 页模块；纯函数（columnsFor / statValue / defaultStatsRange）本地同式重写。
 * 权限门 fail-closed：`session.canNode(['order.list'])`（后端 DailiantongStatsView 同一道门）。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import {
  fetchDailiantongStats,
  STATS_SIDE_ACCEPT,
  STATS_SIDE_PUBLISH,
  type DailiantongStatRow,
  type DailiantongStats,
  type StatsSide,
} from '@/api/dailiantongExtra'
import { useSessionStore } from '@/stores/session'

/* ── 页内文案（逐字照 island 页；出处 = layui-island/pages/dailiantong-stats.ts）──────── */

const MESSAGES = {
  intro: '数据来自当前租户绑定的代练通账号；按天汇总，日期窗口最多 7 天。', // buildHtml L211-213
  loading: '加载中…', // L82
  empty: '这个区间里还没有统计数据', // L83
  failed: '加载失败', // L84
  needDates: '请选择开始日期和结束日期', // L85
  badRange: '开始日期不能晚于结束日期', // L86
  noAccess: '无权限查看代练通按日统计', // L87
  retry: '重试', // L294
} as const

/** 已知列（island L97-111；顺序照上游文档 open-api-v3.4「订单统计」的十三个键）。
 * 只决定中文表头与顺序；哪些列真的出现由回包决定（见 columnsFor）。 */
type StatColumn = { key: string; label: string }

const STAT_COLUMNS: readonly StatColumn[] = [
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

/** 金额/数量列右对齐等宽（设计稿 §4.3 的 cv2-td-num 档；日期/时长/率不进）。
 * 回包多出的未知键不进这张表（无语义，靠左）。 */
const NUM_COLUMN_KEYS: ReadonlySet<string> = new Set([
  'PubCount',
  'PubBal',
  'PubBaseBal',
  'AcceptCount',
  'SettleCount',
  'SettleBal',
  'CancelCount',
  'ArbitRateCount',
  'PayLevelBal',
  'ReceiveBal',
])

function isNumColumn(key: string): boolean {
  return NUM_COLUMN_KEYS.has(key)
}

/* ── 纯函数（island 同式重写，行为逐条对齐）────────────────────────────────────────── */

/** 日期 → `YYYY-MM-DD`（本地日；island L114-118）。 */
function isoDate(day: Date): string {
  const month = String(day.getMonth() + 1).padStart(2, '0')
  const date = String(day.getDate()).padStart(2, '0')
  return `${day.getFullYear()}-${month}-${date}`
}

/** 默认统计窗口：最近 7 天含今天（与后端 `default_stats_window()` 同口径；island L121-126）。 */
function defaultStatsRange(now: Date = new Date()): { start: string; end: string } {
  const end = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const start = new Date(end)
  start.setDate(start.getDate() - 6)
  return { start: isoDate(start), end: isoDate(end) }
}

/** 单元格显示值：缺失/空白 ⇒「—」，其余原样（⛔ 不补 0、不换算；island L129-133）。 */
function statValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  const text = String(value).trim()
  return text === '' ? '—' : text
}

/** 由实际返回决定列（island L141-162）：已知键按文档顺序、未知键按首次出现顺序追加；
 * 空结果退回全部已知列（否则空态只剩一张没有表头的白板）。 */
function columnsFor(rows: readonly DailiantongStatRow[], total: DailiantongStatRow = {}): StatColumn[] {
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

/** 错误对象 → 用户可见文案（后端 `msg` 原文优先；兜底那句只在意外分支用；island L233-235）。 */
function errText(e: unknown): string {
  return e instanceof Error && e.message !== '' ? e.message : MESSAGES.failed
}

/* ── 权限门（fail-closed：无节点只画空态，不发请求）────────────────────────────────── */

const session = useSessionStore()
const canView = computed(() => session.canNode(['order.list']))

/* ── 筛选（日期窗口页面算好摆进框；方向默认发单）────────────────────────────────────── */

const defaultRange = defaultStatsRange()

const filters = reactive<{ start: string; end: string; side: StatsSide }>({
  start: defaultRange.start,
  end: defaultRange.end,
  side: STATS_SIDE_PUBLISH,
})

function setStart(value: unknown): void {
  filters.start = typeof value === 'string' ? value : ''
}

function setEnd(value: unknown): void {
  filters.end = typeof value === 'string' ? value : ''
}

/* ── 状态行（唯一出口；island setState 的三态 + 失败「重试」）────────────────────────── */

type StateKind = 'loading' | 'ready' | 'empty' | 'error'

const state = ref<StateKind>('loading')
const stateText = ref<string>(MESSAGES.loading)

/* ── 表格数据（首屏先画全部已知列表头，回包到了按实际键重画）────────────────────────── */

const columns = ref<StatColumn[]>([...STAT_COLUMNS])
const payload = ref<DailiantongStats>({
  items: [],
  total: {},
  record_count: null,
  start: '',
  end: '',
  is_pub: STATS_SIDE_PUBLISH,
})

/** 表尾「合计」行：任一列键出现在 total 里才画（island bodyHtml 的 hasTotal 判据）。 */
const hasTotal = computed(() => columns.value.some((column) => column.key in payload.value.total))

/** 读当前筛选并发请求（查询 / 重试共用；本地校验失败也走状态行的错误态，照旧页）。 */
async function load(): Promise<void> {
  if (filters.start === '' || filters.end === '') {
    state.value = 'error'
    stateText.value = MESSAGES.needDates
    return
  }
  if (filters.start > filters.end) {
    state.value = 'error'
    stateText.value = MESSAGES.badRange
    return
  }
  state.value = 'loading'
  stateText.value = MESSAGES.loading
  try {
    const result = await fetchDailiantongStats({
      start: filters.start,
      end: filters.end,
      is_pub: filters.side,
    })
    payload.value = result
    columns.value = columnsFor(result.items, result.total)
    if (result.items.length === 0) {
      state.value = 'empty'
      stateText.value = MESSAGES.empty
    } else {
      state.value = 'ready'
      // 状态句用**回包**的实际窗口（payload.start/end），不是框里的值（island L309）。
      stateText.value = `共 ${result.items.length} 天数据（${result.start} 至 ${result.end}）`
    }
  } catch (e) {
    // 失败不清旧表（照旧页：bodyEl 只在 render 时重铺）；只把状态行拨到错误态。
    state.value = 'error'
    stateText.value = errText(e)
  }
}

onMounted(() => {
  // fail-closed：无权限连首屏请求都不发（设计稿 §0-7），只画空态。
  if (canView.value) void load()
})

/* ── 其它出口 ─────────────────────────────────────────────────────────────────────── */

</script>

<template>
  <div class="cv2-page" data-v2-page="dailiantong-stats">
    <div v-if="!canView" class="cv2-empty" data-v2="no-permission">{{ MESSAGES.noAccess }}</div>
    <template v-else>
      <!-- 筛选行：说明句 + 开始/结束日期（默认最近 7 天已摆进框）+ 方向 + 查询 -->
      <section class="cv2-panel cv2-filter" aria-label="统计筛选" data-v2="filters">
        <p class="cv2-intro" data-v2="intro">{{ MESSAGES.intro }}</p>
        <div class="cv2-filter-row">
          <label class="cv2-field">
            <span class="cv2-field-label">开始日期</span>
            <span class="cv2-date" data-v2-filter="date-from">
              <el-date-picker
                :model-value="filters.start"
                type="date"
                value-format="YYYY-MM-DD"
                placeholder="开始日期"
                :teleported="false"
                @update:model-value="setStart"
              />
            </span>
          </label>
          <label class="cv2-field">
            <span class="cv2-field-label">结束日期</span>
            <span class="cv2-date" data-v2-filter="date-to">
              <el-date-picker
                :model-value="filters.end"
                type="date"
                value-format="YYYY-MM-DD"
                placeholder="结束日期"
                :teleported="false"
                @update:model-value="setEnd"
              />
            </span>
          </label>
          <label class="cv2-field">
            <span class="cv2-field-label">方向</span>
            <el-select v-model="filters.side" data-v2-filter="side" :teleported="false">
              <el-option :value="STATS_SIDE_PUBLISH" label="发单" />
              <el-option :value="STATS_SIDE_ACCEPT" label="接单" />
            </el-select>
          </label>
          <div class="cv2-filter-actions">
            <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--find cv2-btn--fill" data-v2="search" @click="load">查询</button>
          </div>
        </div>
      </section>

      <!-- 状态行：加载中 / 共 N 天数据 / 空区间 / 加载失败：<后端原文> + 重试（逐字照旧页） -->
      <p class="cv2-status" :class="{ 'cv2-status--error': state === 'error' }" data-v2="status">
        <template v-if="state === 'error'">
          <span>加载失败：{{ stateText }}</span>
          <button type="button" class="cv2-btn cv2-btn--mini" data-v2="retry" @click="load">{{ MESSAGES.retry }}</button>
        </template>
        <template v-else>{{ stateText }}</template>
      </p>

      <!-- 按日统计表：列由回包决定；金额/数量列右对齐等宽；有 total 追加「合计」表尾 -->
      <section class="cv2-panel cv2-table-panel" aria-label="按日统计表">
        <table class="cv2-table" data-v2="stats-table">
          <thead>
            <tr>
              <th v-for="column in columns" :key="column.key" :data-v2-col="column.key">{{ column.label }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="state === 'empty'">
              <td :colspan="columns.length" class="cv2-empty" data-v2="empty">{{ MESSAGES.empty }}</td>
            </tr>
            <template v-else-if="state === 'ready'">
              <tr v-for="(row, index) in payload.items" :key="index" :data-v2-row="index">
                <td
                  v-for="column in columns"
                  :key="column.key"
                  :class="{ 'cv2-td-num': isNumColumn(column.key) }"
                  :data-v2-cell="column.key"
                >{{ statValue(row[column.key]) }}</td>
              </tr>
            </template>
          </tbody>
          <tfoot v-if="state === 'ready' && hasTotal">
            <tr data-v2="total-row">
              <td
                v-for="(column, index) in columns"
                :key="column.key"
                :class="{ 'cv2-td-num': isNumColumn(column.key) }"
              >{{ index === 0 ? '合计' : statValue(payload.total[column.key]) }}</td>
            </tr>
          </tfoot>
        </table>
      </section>
    </template>
  </div>
</template>

<style scoped>
/* 本页新增的形状收在这里；色一律 var(--tt2-*) token（⛔ 不写裸色值）。 */
.cv2-intro {
  margin: 0 0 12px;
  font-size: 13px;
  color: var(--tt2-dim);
}

/* 状态行（旧页 STATUS_ID 的炭火对应物；错误红 + 重试小钮）。 */
.cv2-status {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 10px 2px 0;
  font-size: 13px;
  color: var(--tt2-dim);
}

.cv2-status--error {
  color: var(--tt2-st-error);
}

/* 13+ 列挤不下一屏：面板横向滚、表格给最小宽（fixed 布局按列均分）。 */
.cv2-table-panel {
  overflow-x: auto;
}

.cv2-table {
  min-width: 1180px;
}

/* 合计行加粗（照旧页 bodyHtml 的 <b>）。 */
.cv2-table tfoot td {
  font-weight: 600;
}
</style>
