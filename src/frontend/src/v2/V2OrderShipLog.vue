<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「订单发货记录」v2（对标页面 = **淘宝发货记录** `/Order/tbsendrecordlist`；业务方 2026-11-05
 * 把菜单名改成「订单发货记录」，对标原文保留在 replicaRoutes 的 `benchmarkTitle`；路由接线由主会话做）。
 * 旧页 = island 复刻页 `order-tbsendrecordlist.ts`。布局骨架照 V2OrderManage：筛选 → 工具条 → 表格面板：
 * - 4 筛选：发货时间区间（`sent_at__date` 双端含，默认今天−2天 ~ 今天）/ 发货结果（-1全部·1成功·0失败）/
 *   关键词（系统单号/淘宝单号模糊，trim 后空则不发）
 * - 10 列照 v1 buildCols（列宽随内容自适应＋超长省略 title 看全；2026-11-06 四记录页统一
 *   布局口径：不横滚 / 左对齐不铺满 / 面板不限高）；勾选列保留（重试发货要它；v1 `fixed:'left'` ⇒ sticky 左钉）
 * - page_size 固定 50（后端 TbSendLogPagination 默认 50 = 对标 limits:[50]；分页条无每页选择）；排序固定 -sent_at,-id（后端 Meta.ordering）
 * - 工具条：刷新 + 重试发货（确认弹窗 → `POST /tb-send-logs/retry/` body `{ids}`，按原参数逐条重发、上限 50 条）；
 *   「导入文件发货」v1 即「尚未开放」死按钮 ⇒ v2 不画（登记偏离）
 * - 权限 fail-closed：`session.canNode(['order.taobao_ship_records'])` 没节点不画重试钮（读数据由后端 403 兜底）
 * ⛔ 不 import island 页模块；列表取数页内走共享 `request()` + `TB_SEND_LOGS_PATH`
 * （tbSendLogs.ts 只有重试出口，本线 ⛔ 改共享文件）。三条 V2OrderManage 私有形状
 * （cv2-th-fixed/cv2-td-fixed、cv2-btn--lg、面板限高 calc(100vh-470px)）照例本页 scoped 重声明。
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { request } from '@/api/http'
import {
  TB_SEND_LOGS_PATH,
  retrySendLogs,
  type TbSendLogRow,
  type TbSendRetryResult,
} from '@/api/tbSendLogs'
import type { Page } from '@/api/unwrap'
import { useSessionStore } from '@/stores/session'

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

/* ── 权限门（重试发货钮 fail-closed；列表/重试后端都判同一节点）────────────────── */

const session = useSessionStore()
const canRetry = computed(() => session.canNode(['order.taobao_ship_records']))

/* ── 文案（出处 = island 页同语义常量；本地重写不引模块）──────────────────────── */

/** 一笔都没勾（**逐字**取对标 `tbsendrecordmang.js` L115 的 `layer.msg`）。 */
const SEND_ROW_SELECT_TEXT = '请勾选要操作的数据行'
/** 重试成功但响应里拿不到 `success`/`fail` 计数时的中性反馈（⛔ 不编数字，v1 retryFeedback 同口径）。 */
const SEND_RETRY_DONE_TEXT = '操作完成'
/** 「发货结果」三档（值 = 对标枚举；`-1` 全部由后端解释成「不筛」，原样透传）。 */
const RESULT_OPTIONS = [
  { value: '-1', label: '全部' },
  { value: '1', label: '成功' },
  { value: '0', label: '失败' },
] as const

/* ── 筛选（4 个照 v1；空值丢弃后端也兜「空串按没传」）────────────────────────── */

type ShipLogFilters = {
  sDate: string
  eDate: string
  result: string
  key: string
}

const pad2 = (n: number): string => String(n).padStart(2, '0')
const ymd = (d: Date): string => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`

/**
 * 默认发货时间区间：今天 −2 个自然日 ~ 今天（本地时区）。
 * ⚠️ 偏移量 −2 天是 v1 就登记过的「推断」级证据（只有一张对标快照可反推）；⛔ 不 import
 * island 的 dateRange——各页偏移不许互相照抄。按天回退无「月末日溢出」问题（Date 自归一化）。
 */
function defaultShipDateRange(now: Date = new Date()): [string, string] {
  return [ymd(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 2)), ymd(now)]
}

const [defaultFrom, defaultTo] = defaultShipDateRange()

const filters = reactive<ShipLogFilters>({
  sDate: defaultFrom,
  eDate: defaultTo,
  result: '-1',
  key: '',
})

function setSDate(v: unknown): void {
  filters.sDate = typeof v === 'string' ? v : ''
}

function setEDate(v: unknown): void {
  filters.eDate = typeof v === 'string' ? v : ''
}

/* ── 取数（服务端分页；page_size 固定 50，无每页选择）────────────────────────── */

const PAGE_SIZE = 50

type ShipLogListParams = {
  sDate?: string
  eDate?: string
  result?: string
  key?: string
  exclude_missing?: boolean
  page: number
  page_size: number
}

/** 筛选 → 检索参数（v1 toSendLogSearch 同口径重写：空串丢弃、key trim）。 */
function buildParams(targetPage: number): ShipLogListParams {
  const params: ShipLogListParams = { page: targetPage, page_size: PAGE_SIZE }
  if (filters.sDate !== '') params.sDate = filters.sDate
  if (filters.eDate !== '') params.eDate = filters.eDate
  // result 的 '0'（失败）是合法筛选值：判「给没给」，不判真值（E-0021 族）。
  if (filters.result !== '') params.result = filters.result
  const key = filters.key.trim()
  if (key !== '') params.key = key
  return params
}

const rows = ref<TbSendLogRow[]>([])
const total = ref(0)
const loading = ref(false)
const loadError = ref<string | null>(null)
const page = ref(1)
const failureRows = ref<TbSendLogRow[]>([])
const failureTotal = ref(0)
const failurePage = ref(1)
const failureLoading = ref(false)
const failureError = ref<string | null>(null)
let failureRequest = 0

async function reloadFailures(targetPage = 1): Promise<void> {
  const serial = ++failureRequest
  failureLoading.value = true
  try {
    const result = await request<Page<TbSendLogRow>>({
      method: 'get', url: TB_SEND_LOGS_PATH,
      params: { ...buildParams(targetPage), result: '0', exclude_missing: true },
    })
    if (serial !== failureRequest) return
    failureRows.value = result.items
    failureTotal.value = result.total
    failurePage.value = result.page
    failureError.value = null
  } catch (e) {
    if (serial !== failureRequest) return
    failureRows.value = []
    failureTotal.value = 0
    failureError.value = errText(e)
  } finally {
    if (serial === failureRequest) failureLoading.value = false
  }
}

function refreshAll(targetPage = 1): void {
  void reload(targetPage)
  void reloadFailures(1)
}

async function reload(targetPage = 1): Promise<void> {
  loading.value = true
  try {
    const result = await request<Page<TbSendLogRow>>({
      method: 'get',
      url: TB_SEND_LOGS_PATH,
      params: buildParams(targetPage),
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
  const last = Math.max(1, Math.ceil(t / PAGE_SIZE))
  if (page.value > last) void reload(last)
})

function onPageChange(p: number): void {
  void reload(p)
}

function onSearch(): void {
  refreshAll()
}

function resetFilters(): void {
  filters.sDate = defaultFrom
  filters.eDate = defaultTo
  filters.result = '-1'
  filters.key = ''
  refreshAll()
}

/* ── 勾选（「重试发货」只吃勾选的行；表头勾 = 当前页全部）──────────────────────── */

const checkedIds = ref<number[]>([])

function isChecked(id: number): boolean {
  return checkedIds.value.includes(id)
}

function toggleRow(id: number): void {
  const i = checkedIds.value.indexOf(id)
  if (i === -1) checkedIds.value.push(id)
  else checkedIds.value.splice(i, 1)
}

const pageAllChecked = computed(
  () => rows.value.length > 0 && rows.value.every((r) => isChecked(r.id)),
)

function togglePageAll(): void {
  if (pageAllChecked.value) {
    checkedIds.value = checkedIds.value.filter((id) => !rows.value.some((r) => r.id === id))
  } else {
    for (const r of rows.value) {
      if (!isChecked(r.id)) checkedIds.value.push(r.id)
    }
  }
}

const checkedRows = computed(() => rows.value.filter((r) => isChecked(r.id)))

/* ── 单元格展示（v1 buildSendRow 的同口径重写；Vue 插值自带转义，无 XSS 面）────── */

/** 发货时间：DRF ISO 串 → `YYYY-MM-DD HH:mm`（v1 fmtTime 同式；null/空 → 空串）。 */
function fmtTime(value: string | null): string {
  return typeof value === 'string' && value !== '' ? value.replace('T', ' ').slice(0, 16) : ''
}

/** 发货结果：`1` = 成功（绿），非 1 = 失败（红）——判据照对标 templet 的 `!== 1`；色档走 carbon 状态签。 */
function resultClass(result: number): string {
  return result === 1 ? 'cv2-st--doing' : 'cv2-st--error'
}

function resultText(result: number): string {
  return result === 1 ? '成功' : '失败'
}

/* ── 重试发货（工具条 → 确认弹窗 → 逐条原参数重发；上限 50 条）────────────────── */

const retryDlg = ref<{ count: number } | null>(null)
const retrying = ref(false)

function onToolbarRetry(): void {
  if (checkedRows.value.length === 0) {
    showToast(SEND_ROW_SELECT_TEXT)
    return
  }
  // 页大小固定 50 ⇒ 勾选天然 ≤ 50（后端 RETRY_MAX_ITEMS 同值兜底）。
  retryDlg.value = { count: checkedRows.value.length }
}

/** 计数键取对标那句文案的载荷形状（`data.success` / `data.fail`）；缺一个就不编数字。 */
function retryFeedback(result: TbSendRetryResult): { text: string; bad: boolean } {
  if (typeof result.success === 'number' && typeof result.fail === 'number') {
    return { text: `成功${result.success}条，失败${result.fail}条`, bad: result.fail > 0 }
  }
  return { text: SEND_RETRY_DONE_TEXT, bad: false }
}

async function doRetry(): Promise<void> {
  const dlg = retryDlg.value
  if (dlg === null || retrying.value) return
  retrying.value = true
  try {
    const result = await retrySendLogs(checkedRows.value.map((r) => r.id))
    retryDlg.value = null
    const feedback = retryFeedback(result)
    showToast(feedback.text, feedback.bad ? 'error' : 'info')
    await Promise.all([reload(page.value), reloadFailures(failurePage.value)])
  } catch (e) {
    // 失败原样后端中文（信封 msg 经 request 抛出）；弹窗留着可重试。
    showToast(errText(e), 'error')
  } finally {
    retrying.value = false
  }
}

/* ── 其它出口 ─────────────────────────────────────────────────────────────── */

/** 空态行的 colspan = 勾选列 + 9 数据列（列全集固定，无列显隐）。 */
const COLSPAN_ALL = 10

onMounted(() => {
  refreshAll()
})
</script>

<template>
  <div class="cv2-page" data-v2-page="order-ship-log">
    <!-- 筛选区：发货时间区间 + 发货结果 + 关键词 + 查询/重置（连体网格组形态照 V2OrderRecycle） -->
    <section class="cv2-panel cv2-filter" aria-label="筛选" data-v2="filters">
      <div class="flt-wrap">
        <div class="flt-group" data-v2-filter-group="sent-time">
          <span class="flt-label">发货时间</span>
          <span class="flt-cell flt-datecell" data-v2-filter="sdate"><el-date-picker
            :model-value="filters.sDate"
            type="date"
            value-format="YYYY-MM-DD"
            placeholder="开始日期"
            :teleported="false"
            @update:model-value="setSDate"
          /></span>
          <span class="flt-dash">–</span>
          <span class="flt-cell flt-datecell" data-v2-filter="edate"><el-date-picker
            :model-value="filters.eDate"
            type="date"
            value-format="YYYY-MM-DD"
            placeholder="结束日期"
            :teleported="false"
            @update:model-value="setEDate"
          /></span>
        </div>
        <div class="flt-group" data-v2-filter-group="result">
          <span class="flt-label">发货结果</span>
          <span class="flt-cell"><el-select v-model="filters.result" class="flt-w140" data-v2-filter="result" :teleported="false">
            <el-option v-for="o in RESULT_OPTIONS" :key="o.value" :value="o.value" :label="o.label" />
          </el-select></span>
        </div>
        <div class="flt-group flt-grow" data-v2-filter-group="key">
          <span class="flt-cell flt-cell--grow"><el-input
            v-model="filters.key"
            placeholder="系统单号或淘宝单号"
            data-v2-filter="key"
            @keydown.enter="onSearch"
          /></span>
        </div>
        <div class="cv2-filter-actions">
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--lg cv2-btn--find cv2-btn--fill" data-v2="search" @click="onSearch">查询</button>
          <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--lg cv2-btn--quiet" data-v2="reset" @click="resetFilters">重置</button>
        </div>
      </div>
    </section>

    <!-- 工具条：刷新 + 重试发货（fail-closed）；「导入文件发货」v1 即「尚未开放」死按钮，v2 不画 -->
    <div class="cv2-toolbar" data-v2="toolbar">
      <div class="ttb-strip">
      <button type="button" class="cv2-btn cv2-btn--primary" data-v2="refresh" :disabled="loading" @click="refreshAll(page)">刷新</button>
      <button v-if="canRetry" type="button" class="cv2-btn cv2-btn--primary" data-v2="retry" @click="onToolbarRetry">重试发货</button>
      <p class="cv2-total-line" data-v2="total">本次搜索统计共{{ total }}条发货记录</p>
      </div>
      <span class="cv2-toolbar-count" data-v2="checked-count">已选 {{ checkedRows.length }} 行</span>
    </div>

    <div class="ship-columns">
    <section class="cv2-panel cv2-table-panel" aria-label="发货记录列表">
      <table class="cv2-table">
        <thead>
          <tr>
            <th class="cv2-th-check cv2-th-fixed">
              <input type="checkbox" data-v2="check-page-all" :checked="pageAllChecked" aria-label="勾选当前页全部行" @change="togglePageAll" />
            </th>
            <th class="cv2-th-center">记录ID</th>
            <th class="cv2-th-center">系统订单号</th>
            <th class="cv2-th-center">淘宝订单号</th>
            <th>发货原参数</th>
            <th class="cv2-th-center">发货店铺</th>
            <th class="cv2-th-center">发货结果</th>
            <th class="cv2-th-center">发货时间</th>
            <th class="cv2-th-center">发货人</th>
            <th>备注说明</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in rows"
            :key="row.id"
            class="cv2-row"
            :class="{ 'cv2-row--checked': isChecked(row.id) }"
            :data-v2-row="row.id"
          >
            <td class="cv2-th-check cv2-td-fixed">
              <input
                type="checkbox"
                :data-v2-row-check="row.id"
                :checked="isChecked(row.id)"
                :aria-label="`勾选记录 ${row.id}`"
                @change="toggleRow(row.id)"
              />
            </td>
            <td class="cv2-td-center cv2-num" data-v2-row-id>{{ row.id }}</td>
            <td class="cv2-td-center cv2-num cut-order" :title="row.order_no">{{ row.order_no }}</td>
            <td class="cv2-td-center cv2-num cut-tb" :title="row.tb_order_no">{{ row.tb_order_no }}</td>
            <td class="cv2-td-project cut-params" :title="row.send_params">{{ row.send_params }}</td>
            <td class="cv2-td-center">{{ row.store_name }}</td>
            <td class="cv2-td-center">
              <span class="cv2-st cv2-st--mark" :class="resultClass(row.result)" :data-v2-result="row.result">
                <i class="cv2-dot" aria-hidden="true"></i>{{ resultText(row.result) }}
              </span>
            </td>
            <td class="cv2-td-center cv2-td-time">{{ fmtTime(row.sent_at) }}</td>
            <td class="cv2-td-center">{{ row.operator }}</td>
            <td class="cv2-td-project cut-remark" :title="row.remark">{{ row.remark }}</td>
          </tr>
          <tr v-if="rows.length === 0">
            <td :colspan="COLSPAN_ALL" class="cv2-empty" data-v2="empty">
              <template v-if="loading">正在加载…</template>
              <template v-else-if="loadError !== null">加载失败：{{ loadError }}</template>
              <template v-else>暂无数据</template>
            </td>
          </tr>
        </tbody>
      </table>
      <div class="cv2-pager" data-v2="pager">
        <span class="cv2-pager-total" data-v2="pager-total">共 {{ total }} 条</span>
        <!-- 每页档对标 limits:[50] 只有一档 ⇒ 固定文案，无下拉 -->
        <span class="cv2-pager-size" data-v2="page-size">每页 {{ PAGE_SIZE }} 条</span>
        <el-pagination
          v-model:current-page="page"
          :page-size="PAGE_SIZE"
          :total="total"
          layout="prev, pager, next"
          background
          @current-change="onPageChange"
        />
      </div>
    </section>
    <section class="cv2-panel ship-failures" aria-label="需核对的失败记录" data-v2="failure-panel">
      <h2>需核对的失败记录</h2>
      <p class="ship-failures-note">不包含订单不存在的记录；与上方时间和关键词筛选一致。</p>
      <div v-if="failureLoading">正在加载…</div>
      <div v-else-if="failureError !== null">加载失败：{{ failureError }}</div>
      <div v-else-if="failureRows.length === 0">暂无需要核对的失败记录</div>
      <ul v-else class="ship-failure-list">
        <li v-for="row in failureRows" :key="row.id" :data-v2-failure="row.id">
          <div class="ship-failure-head"><span>{{ row.order_no || '—' }}</span><time>{{ fmtTime(row.sent_at) }}</time></div>
          <div>淘宝单号：{{ row.tb_order_no || '—' }}</div>
          <div class="ship-failure-reason">{{ row.remark || '未提供失败原因' }}</div>
        </li>
      </ul>
      <div class="ship-failure-pager">
        <span>共 {{ failureTotal }} 条</span>
        <el-pagination :current-page="failurePage" :page-size="PAGE_SIZE" :total="failureTotal" layout="prev, pager, next" background @current-change="reloadFailures" />
      </div>
    </section>
    </div>

    <!-- 重试发货确认（v2 新增确认步，v1 点了直接发）：说明逐条原参数重发 + 上限 50 条 -->
    <el-dialog
      :model-value="retryDlg !== null"
      title="重试发货"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-retry"
      @update:model-value="retryDlg = null"
    >
      <p class="cv2-dlg-text">将按记录原参数逐条重发，上限 50 条；已勾选 {{ retryDlg?.count ?? 0 }} 条发货记录。</p>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="retryDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="confirm-retry" :disabled="retrying" @click="void doRetry()">确定重发</button>
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

.flt-grow {
  flex: 1 1 240px;
}

.flt-grow .flt-cell--grow {
  min-width: 230px;
}

/* 发货时间格：格子定宽、控件撑满（YYYY-MM-DD 10 字形；13px 下 118px 放得下，照管理订单页）。 */
.flt-group .flt-cell.flt-datecell {
  width: 118px;
}

.flt-group .flt-cell.flt-datecell :deep(.el-date-editor.el-input) {
  width: 100%;
}

.flt-group .flt-cell.flt-datecell :deep(.el-input__inner) {
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
.flt-w106 { width: 106px; }

/* 统计句 = 原版橙（对标证据件字面色 #FF6A00 的 token；carbon.css 基类是红，本页覆盖） */
.cv2-total-line {
  color: var(--tt2-summary-orange);
  margin: 0 0 0 12px;
}

.ttb-strip { flex-wrap: wrap; }
.ship-columns { display: grid; grid-template-columns: minmax(0, 3fr) minmax(280px, 1fr); gap: 12px; align-items: start; }
.ship-failures { padding: 14px; min-width: 0; }
.ship-failures h2 { margin: 0 0 4px; font-size: 15px; }
.ship-failures-note { color: var(--tt2-dim); margin: 0 0 12px; }
.ship-failure-list { list-style: none; padding: 0; margin: 0; }
.ship-failure-list li { padding: 10px 0; border-bottom: 1px solid var(--tt2-line); overflow-wrap: anywhere; }
.ship-failure-head { display: flex; justify-content: space-between; gap: 8px; font-family: var(--tt2-mono); }
.ship-failure-head time { color: var(--tt2-dim); white-space: nowrap; }
.ship-failure-reason { color: var(--tt2-st-error); }
.ship-failure-pager { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.cv2-page .cv2-st--mark { gap: 6px; padding: 2px 8px; border: 1px solid transparent; border-radius: 6px; background: transparent; color: var(--st-ink); font-size: 12px; font-weight: 600; }
.cv2-page .cv2-st--mark .cv2-dot { width: 8px; height: 8px; background: var(--st-dot); box-shadow: 0 0 0 3px color-mix(in srgb, var(--st-dot) 12%, transparent); }
.cv2-page .cv2-st--doing.cv2-st--mark { --st-ink: var(--tt2-st-doing); }
.cv2-page .cv2-st--error.cv2-st--mark { --st-ink: var(--tt2-st-error); }
@media (max-width: 1000px) { .ship-columns { grid-template-columns: minmax(0, 1fr); } }

/* 单号 / 记录ID 等宽字形（尺寸/字体非色值）。 */
.cv2-num {
  font-family: var(--tt2-mono);
  font-variant-numeric: tabular-nums;
}

/* ── 勾选列 sticky 左钉（本页无行内操作列；v1 勾选列 fixed:'left' 的对应物，
 *    形状照 V2OrderManage 的 cv2-th-fixed/cv2-td-fixed 三态底色，方向换左、分隔线换右）── */
.cv2-th-fixed,
.cv2-td-fixed {
  position: sticky;
  left: 0;
  background: var(--tt2-panel);
  border-right: 1px solid var(--tt2-line);
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

/* ── 列表块统一布局（2026-11-06 业务方，四记录页同款）：
 * ① 不横滚——摘百分比 colgroup（原配方的定宽驱动），覆盖共享 .cv2-table 的
 *    width:100%/table-layout:fixed，列宽随内容自适应；超长文本列 cut-* 给 max-width 档
 *    （共享 td 本就单行省略，title 看全）；
 * ② 列表块（含分页条）左对齐、宽度随内容，右侧至少留 32px，不铺满整行；
 * ③ 面板放开限高（原 max-height 冻结窗格形态），行多往下自然扩，分页条随之落底。 */
.cv2-table-panel {
  width: 100%;
  max-width: 100%;
  overflow-x: auto;
  min-height: 320px;
}

.cv2-table {
  width: auto;
  table-layout: auto;
}

/* min-width:0 解掉共享 .cv2-td-project 的 200px 下限（params/remark 两列挂着它）。 */
.cut-order,
.cut-tb,
.cut-params,
.cut-remark {
  min-width: 0;
}

.cut-order { max-width: 110px; }
.cut-tb { max-width: 110px; }
.cut-params { max-width: 170px; }
.cut-remark { max-width: 100px; }
</style>
