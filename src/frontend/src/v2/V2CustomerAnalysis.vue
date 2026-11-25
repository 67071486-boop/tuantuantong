<script setup lang="ts">
/**
 * 「客户分析报表」v2（2026-11-10 客户线第三批；旧复刻壳页零 api 调用，不动）。
 * 数据 = GET /api/business/customers/analysis/（range 三档，一次拉全量五块）：
 * - KPI 4 卡：客户总数（恒全量）/ 复购客户数 / 本期消费合计 / 客单价（后三者随档位）；
 * - 来源分布整环扇形（本期各来源消费合计；负金额来源不进扇形、中心 = 本期消费合计原串）；
 * - 客户数量整环扇形（第七批 §1.3：各店铺有订单的客户数，去重客户、同名店铺合并、随档位；
 *   与来源分布并排一行，消费分布与客户增长排第二行）；
 * - 消费分布柱状（全时段累计四档，不随档位变；纯柱照 V2DayReport 用 loadECharts 直驱）；
 * - 客户增长柱线（固定近 12 月按**首单时间**分月新增；柱 = 当月新增、线 = 窗口内累计）。
 * 图表全部走 v2/charts.ts 底座（⛔ 不引 npm echarts、⛔ 不另写第二套图表加载）。
 * ⛔ 前端不算钱：金额一律后端两位小数字符串原样（负数照实，D-0015）。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  fetchCustomerAnalysis,
  type CustomerAnalysis,
  type CustomerAnalysisRange,
  type CustomerConsumeTierRow,
} from '@/api/customers'
import {
  AXIS_LABEL_COLOR,
  buildDonutData,
  COMBO_BAR_COLOR,
  createBarLineCombo,
  createDonut,
  loadECharts,
  type BarLineComboController,
  type DonutController,
  type DonutData,
  type EChartsInstance,
  type EChartsNamespace,
} from '@/v2/charts'

function errText(e: unknown): string {
  return e instanceof Error ? e.message : '暂时取不到数据，请稍后刷新重试'
}

/** 时间范围三档（缺省近 30 天，业务方拍板；切档 = 整页重查）。 */
const RANGE_OPTIONS: readonly { key: CustomerAnalysisRange; label: string }[] = [
  { key: '30d', label: '近30天' },
  { key: '90d', label: '近90天' },
  { key: '12m', label: '近12月' },
]

const range = ref<CustomerAnalysisRange>('30d')
const data = ref<CustomerAnalysis | null>(null)
const loading = ref(false)
const loadError = ref<string | null>(null)
/** 竞态闸（V2Dashboard latest-wins 同款）：只接受最新一轮请求的落点。 */
let epoch = 0

async function reload(): Promise<void> {
  const seq = ++epoch
  loading.value = true
  try {
    const result = await fetchCustomerAnalysis({ range: range.value })
    if (seq !== epoch) return
    data.value = result
    loadError.value = null
    drawCharts()
  } catch (e) {
    if (seq !== epoch) return
    data.value = null
    loadError.value = errText(e)
  } finally {
    if (seq === epoch) loading.value = false
  }
}

function onRange(key: CustomerAnalysisRange): void {
  if (range.value === key) return
  range.value = key
  void reload()
}

/* ── 图表（全部经 charts.ts 底座；容器恒挂载，数据到了才画）────────────── */

const srcEl = ref<HTMLElement | null>(null)
const custEl = ref<HTMLElement | null>(null)
const tierEl = ref<HTMLElement | null>(null)
const growthEl = ref<HTMLElement | null>(null)
const chartsError = ref(false)
let srcDonut: DonutController | null = null
let custDonut: DonutController | null = null
let growthChart: BarLineComboController | null = null
let tierChart: EChartsInstance | null = null

/** 扇形数据 = 本期各来源消费合计（负/零金额行由 parseDonutAmount 滤掉不进盘面）。 */
const srcDonutData = computed<DonutData | null>(() =>
  buildDonutData(
    (data.value?.source_dist ?? []).map((row) => ({ name: row.name, amount: row.amount })),
  ),
)

/** 中心总计 = 本期消费合计后端原串（含负数冲减；⛔ 前端相加）。 */
const srcTotalText = computed(() => data.value?.kpi.period_amount ?? '')

/** 客户数量扇形（第七批 §1.3）：各店铺有订单的客户数；count 走扇形的 amount 通道
 * （原样字符串进 amountText，展示不走钱语义）。 */
const custDonutData = computed<DonutData | null>(() =>
  buildDonutData(
    (data.value?.source_customer_dist ?? []).map((row) => ({
      name: row.name,
      amount: String(row.count),
    })),
  ),
)

/** 客户数扇形中心 = 各片之和（底座 totalValue；计数不是钱，前端求和不越权）。 */
const custTotalText = computed(() => {
  const donut = custDonutData.value
  return donut === null ? '' : String(donut.totalValue)
})

/** 增长柱线：柱 = 当月新增、线 = 窗口内累计新增（12 点由后端补零，前端不补）。 */
const growthCombo = computed(() => {
  let running = 0
  return {
    points: (data.value?.growth ?? []).map((point) => {
      running += point.count
      return {
        label: point.month.slice(2),
        barValue: point.count,
        lineValue: running,
        lineText: String(running),
      }
    }),
    barName: '当月新增',
    lineName: '累计新增',
  }
})

/** 消费分布 option（纯柱；x 轴 = 后端四档 label 原文，色收口在 charts.ts 常量）。 */
function tierOption(tiers: readonly CustomerConsumeTierRow[]): Record<string, unknown> {
  return {
    grid: { left: 40, right: 16, top: 16, bottom: 28 },
    tooltip: { trigger: 'axis', confine: true },
    xAxis: {
      type: 'category',
      data: tiers.map((tier) => tier.label),
      axisTick: { show: false },
      axisLabel: { color: AXIS_LABEL_COLOR, fontSize: 11 },
    },
    yAxis: {
      type: 'value',
      minInterval: 1,
      axisLabel: { color: AXIS_LABEL_COLOR, fontSize: 10.5 },
    },
    series: [
      {
        type: 'bar',
        barMaxWidth: 40,
        itemStyle: { color: COMBO_BAR_COLOR },
        data: tiers.map((tier) => tier.count),
      },
    ],
  }
}

async function withECharts(draw: (echarts: EChartsNamespace) => void): Promise<void> {
  try {
    const echarts = await loadECharts()
    await nextTick()
    draw(echarts)
  } catch {
    chartsError.value = true
  }
}

function drawCharts(): void {
  void withECharts((echarts) => {
    // 来源分布：整环扇形（空数据拆掉旧控制器，不留上一档的残影）。
    const donutData = srcDonutData.value
    const srcAnchor = srcEl.value
    if (donutData === null || srcAnchor === null) {
      srcDonut?.dispose()
      srcDonut = null
    } else {
      srcDonut ??= createDonut(srcAnchor, echarts, {
        centerLabel: '本期消费',
        hideSliceLabels: true,
        hideTipOnLeave: true,
        applySliceColors: true,
      })
      srcDonut.update(donutData, srcTotalText.value, '本期消费')
      srcDonut.resize()
    }
    // 客户数量：同款整环扇形（第七批 §1.3；空数据同上拆旧实例）。
    const custData = custDonutData.value
    const custAnchor = custEl.value
    if (custData === null || custAnchor === null) {
      custDonut?.dispose()
      custDonut = null
    } else {
      custDonut ??= createDonut(custAnchor, echarts, {
        centerLabel: '客户数',
        hideSliceLabels: true,
        hideTipOnLeave: true,
        applySliceColors: true,
      })
      custDonut.update(custData, custTotalText.value, '客户数量')
      custDonut.resize()
    }
    // 消费分布：纯柱（数据恒有四档，但换租户清空时拆旧实例）。
    const tiers = data.value?.consume_tiers ?? []
    const tierAnchor = tierEl.value
    if (tiers.length === 0 || tierAnchor === null) {
      tierChart?.dispose?.()
      tierChart = null
    } else {
      tierChart ??= echarts.init(tierAnchor)
      tierChart.setOption(tierOption(tiers), true)
      tierChart.resize?.()
    }
    // 客户增长：双轴柱线（后端恒回 12 点）。
    const growthAnchor = growthEl.value
    if (growthAnchor !== null) {
      growthChart ??= createBarLineCombo(growthAnchor, echarts)
      growthChart.update(growthCombo.value)
      growthChart.resize()
    }
  })
}

function onLegendEnter(name: string): void {
  srcDonut?.highlight(name)
}

function onLegendLeave(): void {
  srcDonut?.downplay()
}

function onCustLegendEnter(name: string): void {
  custDonut?.highlight(name)
}

function onCustLegendLeave(): void {
  custDonut?.downplay()
}

function onWindowResize(): void {
  srcDonut?.resize()
  custDonut?.resize()
  tierChart?.resize?.()
  growthChart?.resize()
}

onMounted(() => {
  window.addEventListener('resize', onWindowResize)
  void reload()
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onWindowResize)
  srcDonut?.dispose()
  custDonut?.dispose()
  tierChart?.dispose?.()
  growthChart?.dispose()
})
</script>

<template>
  <div class="cv2-page" data-v2-page="customer-analysis">
    <!-- 时间范围三档（缺省近 30 天；切档整页重查，KPI 与来源分布随档位变） -->
    <div class="cv2-dsh-range ana-range" data-v2="range">
      <button
        v-for="opt in RANGE_OPTIONS"
        :key="opt.key"
        type="button"
        class="cv2-dsh-rbtn"
        :class="{ 'cv2-dsh-rbtn--on': range === opt.key }"
        :data-v2-range="opt.key"
        @click="onRange(opt.key)"
      >{{ opt.label }}</button>
      <span class="ana-note" data-v2="range-note">消费 / 复购 / 来源 / 客户数随档位；消费分布是全时段累计，客户增长固定近 12 月</span>
    </div>

    <p v-if="loadError !== null" class="cv2-dsh-note cv2-dsh-fail ana-fail" data-v2="load-fail">
      加载失败：{{ loadError }}
    </p>

    <!-- KPI 4 卡（首次加载出骨架，切档时保留旧读数到新读数落地） -->
    <div class="ana-kpis" data-v2="kpis">
      <section class="cv2-dsh-card" data-v2-kpi="customer_total">
        <h3 class="cv2-dsh-card-title">客户总数</h3>
        <div v-if="loading && data === null" class="cv2-dsh-skel cv2-dsh-skel--short"></div>
        <b v-else class="cv2-dsh-kpi-num" data-v2="kpi-total">{{ data?.kpi.customer_total ?? '—' }}</b>
        <div class="cv2-dsh-kpi-sub">全部客户 · 不随档位变</div>
      </section>
      <section class="cv2-dsh-card" data-v2-kpi="repeat_customers">
        <h3 class="cv2-dsh-card-title">复购客户数<span class="cv2-dsh-tag">随档位</span></h3>
        <div v-if="loading && data === null" class="cv2-dsh-skel cv2-dsh-skel--short"></div>
        <b v-else class="cv2-dsh-kpi-num" data-v2="kpi-repeat">{{ data?.kpi.repeat_customers ?? '—' }}</b>
        <div class="cv2-dsh-kpi-sub">档位窗口内 ≥2 单的客户</div>
      </section>
      <section class="cv2-dsh-card" data-v2-kpi="period_amount">
        <h3 class="cv2-dsh-card-title">本期消费合计<span class="cv2-dsh-tag">随档位</span></h3>
        <div v-if="loading && data === null" class="cv2-dsh-skel cv2-dsh-skel--short"></div>
        <b v-else class="cv2-dsh-kpi-num" data-v2="kpi-amount"><span v-if="data !== null" class="ana-cny">¥</span>{{ data?.kpi.period_amount ?? '—' }}</b>
        <div class="cv2-dsh-kpi-sub">档位窗口内订单总额（负数 = 退款冲减）</div>
      </section>
      <section class="cv2-dsh-card" data-v2-kpi="avg_order">
        <h3 class="cv2-dsh-card-title">客单价<span class="cv2-dsh-tag">随档位</span></h3>
        <div v-if="loading && data === null" class="cv2-dsh-skel cv2-dsh-skel--short"></div>
        <b v-else class="cv2-dsh-kpi-num" data-v2="kpi-avg"><span v-if="data !== null" class="ana-cny">¥</span>{{ data?.kpi.avg_order ?? '—' }}</b>
        <div class="cv2-dsh-kpi-sub">本期消费 ÷ 本期订单数</div>
      </section>
    </div>

    <!-- 三块图（容器恒挂载 v-show：v-if 链会让 ECharts 绑到已脱离的旧节点） -->
    <div class="ana-charts">
      <section class="cv2-dsh-card" data-v2="src-card">
        <h3 class="cv2-dsh-card-title">来源分布<span class="cv2-dsh-tag">随档位</span></h3>
        <p class="cv2-dsh-note">档位窗口内各来源的消费合计 · 未挂字典归快照名或「未指定」</p>
        <p v-if="chartsError" class="cv2-dsh-note cv2-dsh-fail">图表库加载失败，请刷新页面重试</p>
        <div v-else-if="loading && data === null" class="cv2-dsh-skel" data-v2="src-skel"></div>
        <p v-else-if="srcDonutData === null" class="cv2-dsh-note" data-v2="src-empty">档位内暂无来源消费数据</p>
        <div v-show="!chartsError && srcDonutData !== null" class="cv2-dsh-dl">
          <div ref="srcEl" class="cv2-dsh-chart cv2-dsh-chart--donut" data-v2="src-chart"></div>
          <div class="cv2-dsh-legend" data-v2="src-legend">
            <button
              v-for="slice in (srcDonutData ? srcDonutData.slices : [])"
              :key="slice.name"
              type="button"
              class="cv2-dsh-legend-row"
              :data-v2-legend="slice.name"
              @mouseenter="onLegendEnter(slice.name)"
              @mouseleave="onLegendLeave"
            >
              <i class="cv2-dsh-legend-dot" :style="{ background: slice.color }"></i>
              <span class="cv2-dsh-legend-name">{{ slice.name }}</span>
              <span class="cv2-dsh-legend-val">{{ slice.percentText }}</span>
            </button>
          </div>
        </div>
      </section>

      <!-- 客户数量扇形（第七批 §1.3）：各店铺有订单的客户数（去重 eid、同名合并、随档位）；
           与来源分布并排成两扇形一行，消费分布与客户增长排第二行。 -->
      <section class="cv2-dsh-card" data-v2="cust-card">
        <h3 class="cv2-dsh-card-title">客户数量<span class="cv2-dsh-tag">随档位</span></h3>
        <p class="cv2-dsh-note">档位窗口内各店铺有订单的客户数 · 去重客户、同名店铺合并</p>
        <p v-if="chartsError" class="cv2-dsh-note cv2-dsh-fail">图表库加载失败，请刷新页面重试</p>
        <div v-else-if="loading && data === null" class="cv2-dsh-skel" data-v2="cust-skel"></div>
        <p v-else-if="custDonutData === null" class="cv2-dsh-note" data-v2="cust-empty">档位内暂无客户数据</p>
        <div v-show="!chartsError && custDonutData !== null" class="cv2-dsh-dl">
          <div ref="custEl" class="cv2-dsh-chart cv2-dsh-chart--donut" data-v2="cust-chart"></div>
          <div class="cv2-dsh-legend" data-v2="cust-legend">
            <button
              v-for="slice in (custDonutData ? custDonutData.slices : [])"
              :key="slice.name"
              type="button"
              class="cv2-dsh-legend-row"
              :data-v2-cust-legend="slice.name"
              @mouseenter="onCustLegendEnter(slice.name)"
              @mouseleave="onCustLegendLeave"
            >
              <i class="cv2-dsh-legend-dot" :style="{ background: slice.color }"></i>
              <span class="cv2-dsh-legend-name">{{ slice.name }}</span>
              <span class="cv2-dsh-legend-val">{{ slice.percentText }}</span>
            </button>
          </div>
        </div>
      </section>

      <section class="cv2-dsh-card" data-v2="tier-card">
        <h3 class="cv2-dsh-card-title">消费分布<span class="cv2-dsh-tag cv2-dsh-tag--gray">全时段累计</span></h3>
        <p class="cv2-dsh-note">按客户累计消费分四档 · 不随档位变</p>
        <p v-if="chartsError" class="cv2-dsh-note cv2-dsh-fail">图表库加载失败，请刷新页面重试</p>
        <div v-else-if="loading && data === null" class="cv2-dsh-skel" data-v2="tier-skel"></div>
        <div v-show="!chartsError" ref="tierEl" class="ana-chart" data-v2="tier-chart"></div>
      </section>

      <section class="cv2-dsh-card ana-growth" data-v2="growth-card">
        <h3 class="cv2-dsh-card-title">客户增长<span class="cv2-dsh-tag cv2-dsh-tag--gray">固定近 12 月</span></h3>
        <p class="cv2-dsh-note">按客户首单时间分月新增（建档时间不参与——存量建档全是回填同一天）</p>
        <div class="cv2-dsh-mini-legend" aria-hidden="true">
          <span><i class="cv2-dsh-dot-bar"></i>当月新增</span>
          <span><i class="cv2-dsh-dot-line"></i>累计新增</span>
        </div>
        <p v-if="chartsError" class="cv2-dsh-note cv2-dsh-fail">图表库加载失败，请刷新页面重试</p>
        <div v-else-if="loading && data === null" class="cv2-dsh-skel cv2-dsh-skel--wide" data-v2="growth-skel"></div>
        <div v-show="!chartsError" ref="growthEl" class="ana-chart" data-v2="growth-chart"></div>
      </section>
    </div>
  </div>
</template>

<style scoped>
/* 本页布局形状收在这里；色一律走 carbon.css 的 .ttv2 token 与 charts.ts 常量（⛔ 不写裸色值）。
 * 范围按钮 / 卡片 / 图例复用 carbon.css 的 .cv2-dsh-* 全局类（后台首页看板同款）。 */

/* ── 范围行 ─────────────────────────────────────────────────────────── */
.ana-range {
  align-items: baseline;
}

.ana-note {
  font-size: 12px;
  color: var(--tt2-dim);
}

.ana-fail {
  margin-top: 10px;
}

/* ── KPI 4 卡 ───────────────────────────────────────────────────────── */
.ana-kpis {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
}

.ana-cny {
  font-size: 0.55em;
  font-weight: 600;
  margin-right: 2px;
}

/* ── 图区（第七批起 2×2：来源 / 客户数量两扇形一行，消费 / 增长第二行）── */
.ana-charts {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
  margin-top: 14px;
}

.ana-chart {
  width: 100%;
  height: 220px;
  margin-top: 8px;
}

@media (max-width: 900px) {
  .ana-kpis {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .ana-charts {
    grid-template-columns: 1fr;
  }
}
</style>
