<script setup lang="ts">
/**
 * 「数据概览」v2（`/v2/app-stats`；APP与小程序+问题反馈批 A 波，计划 §2）。旧页 =
 * island 自建页 `layui-island/pages/app-stats.ts`（自有功能、无证据件）。四块卡片
 * （小程序访问 / 注册数 / 每日接单 / 押金冻结）+ 近 7/30 天切换；行内上屏字面量
 * （卡片标题、表头、三条合计句、押金句、T+1 说明）逐字照旧页 app-stats.ts:87-205。
 *
 * 口径照旧页：微信块 `available=false` 只显示「暂不可用：原因」（⛔ 不画表不写 0）；
 * 微信 T+1、注册/接单/押金窗口含今天；错误透出后端 message、兜底「加载失败」；
 * 无表单无弹窗无导出。权限门 fail-closed：会话取不到 `finance.support_statistics`
 * ⇒ toast「无权限查看 APP 与小程序数据概览」、不发业务请求（⛔ 不 import island perms）。
 */
import { computed, onMounted, ref } from 'vue'
import {
  fetchAppStatsOverview,
  type AppStatsOverview,
  type ClaimBlock,
  type DepositBlock,
  type RegistrationBlock,
  type WechatBlock,
} from '@/api/appStats'
import { useSessionStore } from '@/stores/session'

const session = useSessionStore()

/* ── 逐字文案（出处 = island 页 MESSAGES 与各 render 函数；本地重写不引模块）────── */

const NO_ACCESS_TEXT = '无权限查看 APP 与小程序数据概览'
const FAILED_TEXT = '加载失败'
const LOADING_TEXT = '加载中…'
const UNAVAILABLE_TEXT = '暂不可用'
const WECHAT_T1_TEXT = '小程序数据由微信提供，T+1（只到昨天）'
const RANGE_DAYS = [7, 30] as const

/** 数字/字符串上屏兜底：`null`/`undefined` 不当 0（照旧页 text()）。 */
function text(value: unknown): string {
  return value === null || value === undefined ? '—' : String(value)
}

/** 渠道明细格：`渠道 X、渠道 Y`；空对象照旧页显示「—」。 */
function claimDetail(day: ClaimBlock['series'][number]): string {
  const entries = Object.entries(day.by_reason)
  if (entries.length === 0) return '—'
  return entries.map(([reason, count]) => `${reason} ${text(count)}`).join('、')
}

/* ── 提示（页面内自绘 toast，同 V2Notices；info 1500ms / error 6000ms）─────────── */

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

/* ── 权限门（fail-closed：会话取不到 = 不画数据面、不发业务请求）───────────────── */

const canView = computed(() => session.canNode(['finance.support_statistics']))

/* ── 取数（7/30 天切换；竞态守卫 = 只认最后一次请求的响应）────────────────────── */

const days = ref<number>(7)
const loading = ref(false)
const statusLine = ref('')
const data = ref<AppStatsOverview | null>(null)
/** 竞态守卫：切换档位时旧响应晚到必须被丢弃（响应落地前核对序号）。 */
let reqSeq = 0

async function load(): Promise<void> {
  const seq = ++reqSeq
  loading.value = true
  statusLine.value = LOADING_TEXT
  try {
    const overview = await fetchAppStatsOverview(days.value)
    if (seq !== reqSeq) return
    data.value = overview
    statusLine.value = ''
  } catch (e) {
    if (seq !== reqSeq) return
    data.value = null
    statusLine.value = e instanceof Error && e.message !== '' ? e.message : FAILED_TEXT
  } finally {
    if (seq === reqSeq) loading.value = false
  }
}

function onRange(next: number): void {
  if (next === days.value || loading.value) return
  days.value = next
  void load()
}

onMounted(() => {
  if (!canView.value) {
    showToast(NO_ACCESS_TEXT)
    return
  }
  void load()
})

/* ── 各块的合计行（模板里的长句收进函数，字面量逐字照旧页）────────────────────── */

function wechatSummary(block: WechatBlock): string {
  const t = block.totals
  if (t === null) return ''
  return (
    `近 ${block.series.length} 天日值相加：打开 ${text(t.session_cnt)} 次 / ` +
    `访问页面 ${text(t.visit_pv)} 次 / 访问人数 ${text(t.visit_uv)} 人 / ` +
    `新增 ${text(t.visit_uv_new)} 人（人数是当日去重，跨天相加会重复计同一个人）`
  )
}

function registrationTotal(block: RegistrationBlock): string {
  const t = block.totals
  return `合计：小程序 ${text(t.mp_weixin)} / App ${text(t.app_android)} / 未识别 ${text(t.unknown)}（共 ${text(t.total)} 人）`
}

function claimTotal(block: ClaimBlock): string {
  const t = block.totals
  const channels = block.mobile_channels.join('、')
  return `合计：移动端 ${text(t.mobile)} 单 / 全部 ${text(t.total)} 单（移动端 = ${channels}）`
}

function depositLine(block: DepositBlock): string {
  return `当前冻结总额 ${text(block.total_frozen)} 元，涉及 ${text(block.taker_count)} 人。`
}
</script>

<template>
  <div class="cv2-page" data-v2-page="app-stats">
    <!-- 无权限：不画数据面、不发业务请求（照旧页 fail-closed 口径） -->
    <section v-if="!canView" class="cv2-panel" data-v2="no-access">
      <p class="cv2-dlg-text">{{ NO_ACCESS_TEXT }}</p>
    </section>

    <template v-else>
      <!-- 顶部：时间范围两档（点击重载）+ T+1 灰字 -->
      <div class="cv2-toolbar" data-v2="toolbar">
        <button
          v-for="d in RANGE_DAYS"
          :key="d"
          type="button"
          class="cv2-btn"
          :class="d === days ? 'cv2-btn--primary' : 'cv2-btn--tool'"
          :data-v2-days="d"
          :disabled="loading"
          @click="onRange(d)"
        >
          近 {{ d }} 天
        </button>
        <span class="as-t1" data-v2="t1-note">{{ WECHAT_T1_TEXT }}</span>
        <span v-if="statusLine" class="as-status" :data-v2="loading ? 'loading' : 'error'">{{ statusLine }}</span>
      </div>

      <template v-if="data">
        <!-- ① 小程序访问：available=false 只显示「暂不可用：原因」，⛔ 不画表不写 0 -->
        <section class="cv2-panel cv2-table-panel" aria-label="小程序访问">
          <h3 class="cv2-panel-title" data-v2="block-wechat">小程序访问</h3>
          <p v-if="!data.wechat.available" class="as-unavailable" data-v2="wechat-unavailable">
            {{ UNAVAILABLE_TEXT }}：{{ data.wechat.reason }}
          </p>
          <template v-else>
            <p v-if="wechatSummary(data.wechat)" class="as-summary" data-v2="wechat-summary">
              {{ wechatSummary(data.wechat) }}
            </p>
            <table class="cv2-table">
              <thead>
                <tr>
                  <th>日期</th>
                  <th class="cv2-th-center">打开次数</th>
                  <th class="cv2-th-center">访问页面次数</th>
                  <th class="cv2-th-center">访问人数</th>
                  <th class="cv2-th-center">新增用户</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="day in data.wechat.series" :key="day.date">
                  <td class="cv2-td-time">{{ day.date }}</td>
                  <td class="cv2-td-center" :data-v2-session="day.date">{{ text(day.session_cnt) }}</td>
                  <td class="cv2-td-center">{{ text(day.visit_pv) }}</td>
                  <td class="cv2-td-center">{{ text(day.visit_uv) }}</td>
                  <td class="cv2-td-center">{{ text(day.visit_uv_new) }}</td>
                </tr>
              </tbody>
            </table>
          </template>
        </section>

        <!-- ② 注册数（App / 小程序）：三桶 + 合计 + 「历史未区分」口径注 -->
        <section class="cv2-panel cv2-table-panel" aria-label="注册数">
          <h3 class="cv2-panel-title" data-v2="block-registration">注册数（App / 小程序）</h3>
          <p class="as-summary" data-v2="registration-total">{{ registrationTotal(data.registrations) }}</p>
          <p class="as-note" data-v2="registration-note">历史未区分：{{ data.registrations.note }}</p>
          <table class="cv2-table">
            <thead>
              <tr>
                <th>日期</th>
                <th class="cv2-th-center">小程序</th>
                <th class="cv2-th-center">App</th>
                <th class="cv2-th-center">未识别</th>
                <th class="cv2-th-center">合计</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="day in data.registrations.series" :key="day.date">
                <td class="cv2-td-time">{{ day.date }}</td>
                <td class="cv2-td-center">{{ text(day.mp_weixin) }}</td>
                <td class="cv2-td-center">{{ text(day.app_android) }}</td>
                <td class="cv2-td-center">{{ text(day.unknown) }}</td>
                <td class="cv2-td-center">{{ text(day.total) }}</td>
              </tr>
            </tbody>
          </table>
        </section>

        <!-- ③ 每日接单（移动端 / 全部）：两行口径 + 每日渠道明细（空 = 「—」） -->
        <section class="cv2-panel cv2-table-panel" aria-label="每日接单">
          <h3 class="cv2-panel-title" data-v2="block-claims">每日接单（移动端 / 全部）</h3>
          <p class="as-summary" data-v2="claim-total">{{ claimTotal(data.claims) }}</p>
          <table class="cv2-table">
            <thead>
              <tr>
                <th>日期</th>
                <th class="cv2-th-center">移动端接单</th>
                <th class="cv2-th-center">接单合计</th>
                <th>渠道明细</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="day in data.claims.series" :key="day.date">
                <td class="cv2-td-time">{{ day.date }}</td>
                <td class="cv2-td-center">{{ text(day.mobile) }}</td>
                <td class="cv2-td-center">{{ text(day.total) }}</td>
                <td>{{ claimDetail(day) }}</td>
              </tr>
            </tbody>
          </table>
        </section>

        <!-- ④ 押金冻结：金额字符串原样上屏（⛔ 前端不做金额换算） -->
        <section class="cv2-panel cv2-table-panel" aria-label="押金冻结">
          <h3 class="cv2-panel-title" data-v2="block-deposit">押金冻结</h3>
          <p class="as-summary" data-v2="deposit-line">
            {{ depositLine(data.deposit) }}
            <span class="as-note">{{ data.deposit.note }}</span>
          </p>
          <table class="cv2-table">
            <thead>
              <tr>
                <th>日期</th>
                <th class="cv2-th-center">冻结额变动（元）</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="day in data.deposit.series" :key="day.date">
                <td class="cv2-td-time">{{ day.date }}</td>
                <td class="cv2-td-center">{{ text(day.delta) }}</td>
              </tr>
            </tbody>
          </table>
        </section>
      </template>
    </template>

    <!-- 炭火 toast（居中深条，同 V2Notices） -->
    <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
      {{ toast.text }}
    </div>
  </div>
</template>

<style scoped>
/* 本页形状收在这里；色一律走 carbon.css 的 token（⛔ 不写裸色值）。 */

/* T+1 说明与口径注：灰字小号（旧页 #999 的炭火映射）。 */
.as-t1,
.as-note {
  color: var(--tt2-dim);
  font-size: 12px;
}

.as-t1 {
  margin-left: 10px;
}

/* 取数状态行（加载中… / 失败文案透出）。 */
.as-status {
  margin-left: 10px;
  font-size: 12px;
  color: var(--tt2-ink);
}

/* 「暂不可用：原因」整行（旧页 #999 的炭火映射；只此一句，无表无 0）。 */
.as-unavailable {
  margin: 4px 0;
  color: var(--tt2-dim);
}

/* 各块合计句：灰字、块内第一行（旧页 #666 的炭火映射）。 */
.as-summary {
  margin: 0 0 6px;
  color: var(--tt2-dim);
}

.as-summary .as-note {
  margin-left: 6px;
}
</style>
