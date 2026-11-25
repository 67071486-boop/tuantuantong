<script setup lang="ts">
/**
 * 「性能分析」v2（`/v2/perfanalysis`；2026-11-06 开放平台批，计划 §2）。
 * 旧页 = island 自建页 `layui-island/pages/system-perfanalysis.ts`（自有功能，无对标物）。
 * 只读看板：时间范围两日期框（默认今天与往前 6 自然日共 7 天）+「查询」一颗钮（⛔ 无
 * 导出/删除/刷新——旧页裁定）；概览引用块（summaryHtml 口径）+ 三张前 50 表（按接口 /
 * 按人 / 最慢请求）。API 只发 `date_from`/`date_to`（route/username/slow_only 有意不实现，
 * 照旧）；畸形日期后端回 1000、前端如实 toast（⛔ 不加前端格式校验门遮掉那条判据）；
 * 空态两句照旧页。无分页；无页内权限门（准入 = 路由两道门，与旧页一致）。⛔ 不 import
 * island 页模块；模板插值自带转义，勿改 v-html。
 */
import { onMounted, ref } from 'vue'
import {
  fetchRequestMetrics,
  type RequestMetricsByRouteRow,
  type RequestMetricsByUserRow,
  type RequestMetricsPayload,
  type RequestMetricsSlowestRow,
} from '@/api/requestMetrics'

/* ── 逐字文案与常量（出处 = island 页同语义常量；本地重写不引模块）────────────── */

/** 默认时间窗天数（island DEFAULT_RANGE_DAYS = 7）。 */
const DEFAULT_RANGE_DAYS = 7
/** 页头辅助句（island buildHtml 的 aux 行逐字）。 */
const RANGE_HINT = `默认最近 ${DEFAULT_RANGE_DAYS} 天；本页只读，数据按小时汇总、慢请求逐条留存。`
/** 两张聚合表的空态与最慢表的空态（island table.render text.none 逐字）。 */
const EMPTY_ROWS_TEXT = '这段时间里没有请求记录'
const EMPTY_SLOW_TEXT = '这段时间里没有慢请求'

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

/* ── 默认时间范围：今天与往前 6 个自然日（island defaultPerfDateRange 同口径本地重写；
 *    一律用本地构造器，⛔ 不用 toISOString——UTC 会把日期推前一天）────────────── */

const pad2 = (n: number): string => String(n).padStart(2, '0')

function formatDateOnly(date: Date): string {
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`
}

function defaultPerfDateRange(now: Date = new Date()): { dateFrom: string; dateTo: string } {
  const from = new Date(now.getFullYear(), now.getMonth(), now.getDate() - (DEFAULT_RANGE_DAYS - 1))
  return { dateFrom: formatDateOnly(from), dateTo: formatDateOnly(now) }
}

const initialRange = defaultPerfDateRange()
const dateFrom = ref(initialRange.dateFrom)
const dateTo = ref(initialRange.dateTo)

function setFrom(v: unknown): void {
  dateFrom.value = typeof v === 'string' ? v : ''
}

function setTo(v: unknown): void {
  dateTo.value = typeof v === 'string' ? v : ''
}

/* ── 概览（summaryHtml 口径的纯数据形态；桶 label 由后端给，前端不镜像词表）────── */

const summary = ref<RequestMetricsPayload | null>(null)
const loading = ref(false)
/* 三张表的行（同一次响应的另三块）。 */
const routeRows = ref<RequestMetricsByRouteRow[]>([])
const userRows = ref<RequestMetricsByUserRow[]>([])
const slowRows = ref<RequestMetricsSlowestRow[]>([])

/** 「慢请求」表的「时间」列：ISO 串截成 `YYYY-MM-DD HH:mm`（island sliceDateTime 同式；
 *  后端已是项目时区 ISO 串，截断安全）。 */
function sliceDateTime(value: string): string {
  return value.length < 16 ? value : value.substring(0, 16).replace('T', ' ')
}

/** 「人员」格：未登录行（username 空串 + user_id 0）照实写「未登录」，⛔ 不留空格
 *  （island personLabel 同式）；slowest 行契约没有 user_id ⇒ 按 0 传。 */
function personLabel(username: string, userId: number): string {
  if (username !== '') return username
  return userId === 0 ? '未登录' : `#${userId}`
}

/* ── 取数：两个日期框的当前值就是筛选条件（空串不发，toRequestMetricsParams 同判）。
 *    ⛔ 不做前端日期格式校验：手输畸形值由后端回 1000，页面如实 toast。─────────── */

async function load(): Promise<void> {
  loading.value = true
  try {
    const payload = await fetchRequestMetrics({ date_from: dateFrom.value, date_to: dateTo.value })
    // 同一次响应铺四块（概览 + 三张表）——点一次「查询」只发一条请求（island applyPayload 口径）。
    summary.value = payload
    routeRows.value = payload.by_route
    userRows.value = payload.by_user
    slowRows.value = payload.slowest
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void load()
})
</script>

<template>
  <div class="cv2-page" data-v2-page="perfanalysis">
    <!-- 时间范围：两日期框 + 「查询」一颗钮（⛔ 无导出/删除/刷新，旧页裁定） -->
    <section class="cv2-panel pf-filter" aria-label="时间范围" data-v2="filters">
      <span class="cv2-field pf-date" data-v2-filter-group="date-from">
        <span class="cv2-field-label">时间范围</span>
        <el-date-picker
          :model-value="dateFrom"
          type="date"
          value-format="YYYY-MM-DD"
          placeholder="开始日期"
          :teleported="false"
          data-v2-filter="date-from"
          @update:model-value="setFrom"
        />
      </span>
      <span class="pf-dash">~</span>
      <span class="cv2-field pf-date" data-v2-filter-group="date-to">
        <el-date-picker
          :model-value="dateTo"
          type="date"
          value-format="YYYY-MM-DD"
          placeholder="结束日期"
          :teleported="false"
          data-v2-filter="date-to"
          @update:model-value="setTo"
        />
      </span>
      <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--find cv2-btn--fill" data-v2="query" :disabled="loading" @click="load">查询</button>
      <span class="cv2-toolbar-count" data-v2="range-hint">{{ RANGE_HINT }}</span>
    </section>

    <!-- 概览引用块（island summaryHtml 同口径：范围/请求数/慢请求/平均耗时/最慢/耗时分布） -->
    <blockquote class="cv2-note pf-summary" data-v2="summary">
      <template v-if="summary !== null">
        <span class="pf-dim">时间范围：</span><b data-v2="summary-range">{{ summary.range.date_from }} ~ {{ summary.range.date_to }}</b>
        <span class="pf-dim pf-gap">请求数：</span><b data-v2="summary-requests">{{ summary.summary.requests }}</b>
        <span class="pf-dim pf-gap">慢请求：</span><b data-v2="summary-slow">{{ summary.summary.slow_requests }}</b>
        <span class="pf-dim pf-gap">平均耗时：</span><b data-v2="summary-avg">{{ summary.summary.avg_ms }} ms</b>
        <span class="pf-dim pf-gap">最慢：</span><b data-v2="summary-max">{{ summary.summary.max_ms }} ms</b>
        <template v-if="summary.summary.hist.length > 0">
          <br />
          <span class="pf-dim">耗时分布：</span><span data-v2="summary-hist">{{ summary.summary.hist.map((b) => `${b.label}：${b.count}`).join(' · ') }}</span>
        </template>
      </template>
      <template v-else>加载中…</template>
    </blockquote>

    <!-- 按接口（前 50，后端已裁） -->
    <section class="cv2-panel cv2-table-panel pf-panel" aria-label="按接口的耗时聚合">
      <div class="cv2-panel-title">按接口</div>
      <table class="cv2-table">
        <thead>
          <tr>
            <th>接口</th>
            <th class="cv2-th-center">请求数</th>
            <th class="cv2-th-center">平均耗时</th>
            <th class="cv2-th-center">最慢</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in routeRows" :key="row.route" class="cv2-row" :data-v2-row="row.route">
            <td class="pf-route">{{ row.route }}</td>
            <td class="cv2-td-center">{{ row.requests }}</td>
            <td class="cv2-td-center">{{ row.avg_ms }} ms</td>
            <td class="cv2-td-center">{{ row.max_ms }} ms</td>
          </tr>
          <tr v-if="routeRows.length === 0">
            <td colspan="4" class="cv2-empty" data-v2="empty-route">
              <template v-if="loading">正在加载…</template>
              <template v-else>{{ EMPTY_ROWS_TEXT }}</template>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 按人（前 50；未登录行显示「未登录」） -->
    <section class="cv2-panel cv2-table-panel pf-panel" aria-label="按人的耗时聚合">
      <div class="cv2-panel-title">按人</div>
      <table class="cv2-table">
        <thead>
          <tr>
            <th>人员</th>
            <th class="cv2-th-center">请求数</th>
            <th class="cv2-th-center">平均耗时</th>
            <th class="cv2-th-center">最慢</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in userRows" :key="row.user_id" class="cv2-row" :data-v2-row="row.user_id">
            <td>{{ personLabel(row.username, row.user_id) }}</td>
            <td class="cv2-td-center">{{ row.requests }}</td>
            <td class="cv2-td-center">{{ row.avg_ms }} ms</td>
            <td class="cv2-td-center">{{ row.max_ms }} ms</td>
          </tr>
          <tr v-if="userRows.length === 0">
            <td colspan="4" class="cv2-empty" data-v2="empty-user">
              <template v-if="loading">正在加载…</template>
              <template v-else>{{ EMPTY_ROWS_TEXT }}</template>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 最慢的请求（前 50；时间截 YYYY-MM-DD HH:mm） -->
    <section class="cv2-panel cv2-table-panel pf-panel" aria-label="最慢的请求">
      <div class="cv2-panel-title">最慢的请求</div>
      <table class="cv2-table">
        <thead>
          <tr>
            <th>时间</th>
            <th>接口</th>
            <th class="cv2-th-center">方法</th>
            <th class="cv2-th-center">状态码</th>
            <th class="cv2-th-center">人员</th>
            <th class="cv2-th-center">耗时</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, index) in slowRows" :key="`${row.occurred_at}-${index}`" class="cv2-row" :data-v2-row="index">
            <td class="cv2-td-time">{{ sliceDateTime(row.occurred_at) }}</td>
            <td class="pf-route">{{ row.route }}</td>
            <td class="cv2-td-center">{{ row.method }}</td>
            <td class="cv2-td-center">{{ row.status_code }}</td>
            <td class="cv2-td-center">{{ personLabel(row.username, 0) }}</td>
            <td class="cv2-td-center">{{ row.duration_ms }} ms</td>
          </tr>
          <tr v-if="slowRows.length === 0">
            <td colspan="6" class="cv2-empty" data-v2="empty-slow">
              <template v-if="loading">正在加载…</template>
              <template v-else>{{ EMPTY_SLOW_TEXT }}</template>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 炭火 toast -->
    <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
      {{ toast.text }}
    </div>
  </div>
</template>

<style scoped>
/* 本页新增的形状收在这里；色一律走 carbon.css 的 token（⛔ 不写裸色值）。 */

/* 筛选行：日期框 + 查询 + 辅助句一行排开。 */
.pf-filter {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 12px;
  align-items: center;
}

.pf-date :deep(.el-date-editor.el-input) {
  width: 150px;
}

.pf-dash {
  color: var(--tt2-dim);
}

.pf-gap {
  margin-left: 16px;
}

.pf-dim {
  color: var(--tt2-dim);
}

/* 概览引用块（island blockquote 的炭火对应档 = cv2-note）。 */
.pf-summary {
  line-height: 28px;
}

/* 接口模板列：等宽、可折行（长模板不截断，截断会让"哪条接口"看不全）。 */
.pf-route {
  word-break: break-all;
}

/* 三张表逐块排列；面板自身不限高（前 50 是后端裁过的量）。 */
.pf-panel {
  max-height: none;
}

/* 全局提示 = 对标 layer.msg 居中深条（V2Notices 同款）。 */
.cv2-page :deep(.cv2-toast) {
  top: 45%;
  left: 50%;
  transform: translate(-50%, -50%);
  z-index: 3000;
  max-width: 56vw;
  padding: 12px 22px;
  border: none;
  border-radius: 6px;
  background: var(--tt2-toast-bg);
  color: var(--tt2-panel);
  font-size: 14px;
  text-align: center;
}
</style>
