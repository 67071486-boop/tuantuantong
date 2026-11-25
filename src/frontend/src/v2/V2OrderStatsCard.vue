<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「接单统计」数据卡片（v2 人员管理；系统人员批次 2026-11-05，计划 §3.1.3，lg 弹窗）。
 *
 * 旧页 = island `system-user-list.ts` 里的 `.recTotalDiv` 卡片（`#tableList2`）。
 * 取数 `GET /api/business/order-stats`，参数 `stime` / `etime` / `gameName`（按**游戏名**）、
 * 分页 `page` / `limit`。行为照旧页：
 * - 筛选：游戏下拉（value = 游戏名，首项全部）+ 接单起止日（默认今天 − 3 自然月，
 *   月底取最后一天的边界照 island `defaultRecTotalRange`）。
 * - 7 个命名列、无勾选列；完单率前端拼 `%`（title 逐字）；最近接单空 →「无」。
 * - 分页 limit 100、档位 [20,30,50,100,500,1000,2000]；**日期变更回第 1 页、
 *   游戏变更 / 刷新留当前页**（依赖后端「越界 page = 成功空页」，⛔ 不自作主张回末页）。
 * - 三态空文案互异：暂无数据 / 失败 = 后端 msg 写进空态 + toast（403 专属那句不得被吃掉）。
 * - 工具条橙字逐字；「刷新列表」真刷（留页），另两颗「尚未开放」。
 */
import { onMounted, ref } from 'vue'
import { request } from '@/api/http'
import type { Page } from '@/api/unwrap'
import type { ConfigRow } from '@/api/config'

const props = defineProps<{
  /** 全量游戏（父页已取好；本卡下拉按游戏名取值）。 */
  games: ConfigRow[]
  notify: (text: string, kind?: 'info' | 'error') => void
}>()

const emit = defineEmits<{ (e: 'close'): void }>()

const ORDER_STATS_URL = '/api/business/order-stats'
const EMPTY_TEXT = '暂无数据'
const REC_TOTAL_NOTE =
  '这里已排除了禁用的子账号，只统计有效状态为正常的子账号。注意，数据有2分钟缓存，禁用账号、删除子账号均会有缓存'
const RATE_TITLE = '订单完成率(只计算待验收+已完成的)'

/** 行形状 = 后端 `OrderStatsListSerializer` 逐键：`complateRate` 不带 %（前端拼）、
 * `lastRecOrderDate` 窗口内没接过单时是字符串「无」。 */
type OrderStatsRow = {
  idx: number
  uid: number
  userName: string
  orderCount: number
  noSettAmount: number
  complateRate: number
  createUserDate: string
  lastRecOrderDate: string
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : EMPTY_TEXT
}

function pad2(v: number): string {
  return String(v).padStart(2, '0')
}

function toDateText(d: Date): string {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
}

/** 今天 − 3 自然月 ~ 今天（含两端）；减法溢出月取**该月最后一天**（island 同口径，⛔ 别用裸 Date 溢出）。 */
function defaultRange(now: Date = new Date()): { stime: string; etime: string } {
  const year = now.getFullYear()
  const month = now.getMonth() - 3
  const lastDayOfTargetMonth = new Date(year, month + 1, 0).getDate()
  const day = Math.min(now.getDate(), lastDayOfTargetMonth)
  return { stime: toDateText(new Date(year, month, day)), etime: toDateText(now) }
}

const first = defaultRange()

const gameName = ref('')
const stime = ref(first.stime)
const etime = ref(first.etime)

const DEFAULT_LIMIT = 100
const LIMITS = [20, 30, 50, 100, 500, 1000, 2000]

const rows = ref<OrderStatsRow[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(DEFAULT_LIMIT)
const loading = ref(false)
/** 空态文案：成功空 = 暂无数据；失败 = 后端 msg（三态互异）。 */
const emptyText = ref(EMPTY_TEXT)

async function load(resetPage: boolean): Promise<void> {
  if (resetPage) page.value = 1
  loading.value = true
  try {
    const params: Record<string, string | number> = {
      page: page.value,
      limit: pageSize.value,
    }
    if (stime.value !== '') params.stime = stime.value
    if (etime.value !== '') params.etime = etime.value
    if (gameName.value !== '') params.gameName = gameName.value
    const res = await request<Page<OrderStatsRow>>({ method: 'get', url: ORDER_STATS_URL, params })
    rows.value = res.items
    total.value = res.total
    emptyText.value = EMPTY_TEXT
  } catch (e) {
    rows.value = []
    total.value = 0
    emptyText.value = errText(e)
    props.notify(errText(e), 'error')
  } finally {
    loading.value = false
  }
}

function onDateChange(): void {
  void load(true)
}

function onPageChange(p: number): void {
  page.value = p
  void load(false)
}

function onPageSizeChange(event: Event): void {
  const size = Number((event.target as HTMLSelectElement).value)
  if (Number.isFinite(size) && size > 0) {
    pageSize.value = size
    void load(true)
  }
}

function notOpen(label: string): void {
  props.notify(`「${label}」尚未开放`)
}

onMounted(() => {
  void load(true)
})
</script>

<template>
  <el-dialog
    model-value
    title="接单统计"
    class="cv2-dialog cv2-dialog--lg"
    data-v2="dlg-order-stats"
    @update:model-value="emit('close')"
  >
    <div class="os-filters">
      <label class="cv2-field">
        <span class="cv2-field-label">游戏权限</span>
        <el-select v-model="gameName" :teleported="false" data-v2="os-game" @change="load(false)">
          <el-option value="" label="全部" />
          <el-option v-for="g in games" :key="g.id" :value="g.name" :label="g.name" />
        </el-select>
      </label>
      <label class="cv2-field">
        <span class="cv2-field-label">接单开始日</span>
        <input v-model="stime" class="cv2-input" type="date" data-v2="os-stime" @change="onDateChange" />
      </label>
      <label class="cv2-field">
        <span class="cv2-field-label">接单结束日</span>
        <input v-model="etime" class="cv2-input" type="date" data-v2="os-etime" @change="onDateChange" />
      </label>
    </div>

    <div class="cv2-toolbar os-toolbar">
      <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--find" data-v2="os-refresh" :disabled="loading" @click="load(false)">
        刷新列表
      </button>
      <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--danger" data-v2="os-disable" @click="notOpen('禁用子账号')">
        禁用子账号
      </button>
      <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--danger" data-v2="os-delete" @click="notOpen('删除用户')">
        删除用户
      </button>
      <p class="os-note" data-v2="os-note">{{ REC_TOTAL_NOTE }}</p>
    </div>

    <section class="cv2-table-panel os-panel">
      <table class="cv2-table">
        <thead>
          <tr>
            <th class="cv2-th-center">排名</th>
            <th>姓名/名称</th>
            <th class="cv2-th-center">接单量</th>
            <th class="cv2-th-center">还未结算</th>
            <th class="cv2-th-center">
              <span :title="RATE_TITLE">完单率</span>
            </th>
            <th class="cv2-th-center">开通日期</th>
            <th class="cv2-th-center">最近接单</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.uid" class="cv2-row" :data-v2-os-row="row.uid">
            <td class="cv2-td-center cv2-td-num">{{ row.idx }}</td>
            <td>{{ row.userName }}</td>
            <td class="cv2-td-center cv2-td-num">{{ row.orderCount }}</td>
            <td class="cv2-td-center cv2-td-num">{{ row.noSettAmount }}</td>
            <td class="cv2-td-center cv2-td-num" :title="RATE_TITLE">{{ row.complateRate }}%</td>
            <td class="cv2-td-center cv2-td-time">{{ row.createUserDate }}</td>
            <td class="cv2-td-center">{{ row.lastRecOrderDate || '无' }}</td>
          </tr>
          <tr v-if="rows.length === 0">
            <td colspan="7" class="cv2-empty" data-v2="os-empty">
              <template v-if="loading">正在加载…</template>
              <template v-else>{{ emptyText }}</template>
            </td>
          </tr>
        </tbody>
      </table>
      <div class="cv2-pager">
        <span class="cv2-pager-total">共 {{ total }} 条</span>
        <label class="cv2-pager-size">
          每页
          <select class="cv2-input cv2-input--inline" data-v2="os-page-size" :value="pageSize" @change="onPageSizeChange">
            <option v-for="n in LIMITS" :key="n" :value="n">{{ n }}</option>
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
  </el-dialog>
</template>

<style scoped>
.os-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 16px;
  align-items: center;
  margin-bottom: 10px;
}
.os-toolbar {
  margin-bottom: 8px;
}
.os-note {
  margin: 0;
  color: var(--tt2-summary-orange);
  font-size: 12px;
}
.os-panel {
  max-height: 52vh;
  overflow: auto;
}
</style>
