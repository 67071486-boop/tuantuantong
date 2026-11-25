// @vitest-environment jsdom
// ↑ 挂载组件需要 DOM（本仓 vitest 默认 environment 是 node，只在本文件覆盖）。
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 「客户分析报表」v2（V2CustomerAnalysis，2026-11-10 客户线第三批；第七批 2026-11-11 +客户数扇形）的判据：
 * ① 默认 range=30d 发参 + KPI 4 卡钉值（含措辞「客户」）；② 四图挂载——来源分布走真
 * buildDonutData + createDonut（负金额来源不进扇形、中心 = 本期消费合计原串、嵌套形态上盘）、
 * 客户数量扇形（count 走同一底座、中心 = 各片之和）、
 * 消费分布纯柱（x 轴 = 后端四档 label 原文）、客户增长双轴柱线（柱 = 当月新增、线 = 累计）；
 * ③ 切换重发（参数逐字 + 活动档高亮）；④ 竞态 latest-wins（旧轮响应落地即弃）；⑤ 空态与失败降级。
 * 全部夹具为合成值（PII 纪律）；后端契约见 `api/customers.ts` 头注。
 */

type FakeInstance = {
  el: HTMLElement
  setOption: ReturnType<typeof vi.fn>
  on: ReturnType<typeof vi.fn>
  handlers: Map<string, (params: unknown) => void>
  dispatchAction: ReturnType<typeof vi.fn>
  resize: ReturnType<typeof vi.fn>
  dispose: ReturnType<typeof vi.fn>
}

const instances: FakeInstance[] = []
const fakeNamespace = {
  init: (el: HTMLElement): FakeInstance => {
    const instance: FakeInstance = {
      el,
      setOption: vi.fn(),
      handlers: new Map<string, (params: unknown) => void>(),
      on: vi.fn((name: string, handler: (params: unknown) => void) => {
        instance.handlers.set(name, handler)
      }),
      dispatchAction: vi.fn(),
      resize: vi.fn(),
      dispose: vi.fn(),
    }
    instances.push(instance)
    return instance
  },
}

function instanceFor(wrapper: VueWrapper, anchor: string): FakeInstance {
  const el = wrapper.find(`[data-v2="${anchor}"]`).element
  const found = instances.find((instance) => instance.el === el)
  expect(found, `${anchor} 没有对应的图表实例`).toBeDefined()
  return found!
}

function lastSetOption(instance: FakeInstance): unknown {
  const calls = instance.setOption.mock.calls
  expect(calls.length, '实例还没有收到任何 setOption').toBeGreaterThan(0)
  return calls[calls.length - 1]![0]
}

/** 中心覆盖层（HTML，不用 echarts title）的两行读数。 */
function centerTextOf(instance: FakeInstance): { value: string; label: string } {
  const value = instance.el.querySelector<HTMLElement>('.tt2-donut-center-value')
  const label = instance.el.querySelector<HTMLElement>('.tt2-donut-center-label')
  expect(value, '中心数值覆盖层没建').not.toBeNull()
  expect(label, '中心标签覆盖层没建').not.toBeNull()
  return { value: value!.textContent ?? '', label: label!.textContent ?? '' }
}

// charts 只换加载器（真控制器 / 真 buildDonutData 保留，V2Dashboard.spec 同款）。
vi.mock('@/v2/charts', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/v2/charts')>()),
  loadECharts: () => Promise.resolve(fakeNamespace),
}))

const fetchCustomerAnalysis = vi.fn()
vi.mock('@/api/customers', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/customers')>()),
  fetchCustomerAnalysis: (...args: unknown[]) => fetchCustomerAnalysis(...args),
}))

import V2CustomerAnalysis from '../V2CustomerAnalysis.vue'
import { COMBO_BAR_COLOR, DONUT_PALETTE, paletteIndexForName } from '@/v2/charts'
import type { CustomerAnalysis } from '@/api/customers'

/* ── 合成夹具（月份/金额/店名全部合成值）───────────────────────────────── */

const GROWTH_30D: CustomerAnalysis['growth'] = [
  '2025-12', '2026-01', '2026-02', '2026-03', '2026-04', '2026-05',
  '2026-06', '2026-07', '2026-08', '2026-09', '2026-10', '2026-11',
].map((month, index) => ({
  month,
  count: index === 10 ? 1 : index === 11 ? 4 : 0,
}))

const PAYLOAD_30D: CustomerAnalysis = {
  range: '30d',
  kpi: {
    customer_total: 12,
    repeat_customers: 3,
    period_amount: '2880.00',
    period_orders: 7,
    avg_order: '411.43',
  },
  source_dist: [
    { order_source: 3, name: '店铺甲', amount: '2900.00' },
    { order_source: null, name: '闲鱼快照甲', amount: '10.00' },
    { order_source: 7, name: '店铺乙', amount: '-30.00' },
  ],
  // 客户数扇形（第七批 §1.3）：与消费额扇形独立的一组桶（合成值）。
  source_customer_dist: [
    { name: '店铺甲', count: 5 },
    { name: '闲鱼快照甲', count: 1 },
    { name: '店铺乙', count: 2 },
  ],
  consume_tiers: [
    { label: '0-100', count: 2 },
    { label: '100-500', count: 2 },
    { label: '500-2000', count: 1 },
    { label: '2000+', count: 1 },
  ],
  growth: GROWTH_30D,
}

const PAYLOAD_90D: CustomerAnalysis = {
  ...PAYLOAD_30D,
  range: '90d',
  kpi: { customer_total: 12, repeat_customers: 5, period_amount: '2930.00', period_orders: 8, avg_order: '366.25' },
}

beforeEach(() => {
  vi.clearAllMocks()
  instances.length = 0
  fetchCustomerAnalysis.mockResolvedValue(PAYLOAD_30D)
})

async function mountPage(): Promise<VueWrapper> {
  const wrapper = mount(V2CustomerAnalysis)
  await flushPromises()
  return wrapper
}

describe('V2CustomerAnalysis（客户分析报表 v2）：默认档与 KPI', () => {
  it('挂载即按缺省 range=30d 发参；KPI 4 卡钉值；措辞钉「客户」（不是会员）', async () => {
    const wrapper = await mountPage()
    expect(fetchCustomerAnalysis).toHaveBeenCalledTimes(1)
    expect(fetchCustomerAnalysis).toHaveBeenCalledWith({ range: '30d' })
    expect(wrapper.find('[data-v2-kpi="customer_total"] [data-v2="kpi-total"]').text()).toBe('12')
    expect(wrapper.find('[data-v2-kpi="repeat_customers"]').text()).toContain('复购客户数')
    expect(wrapper.find('[data-v2-kpi="repeat_customers"] [data-v2="kpi-repeat"]').text()).toBe('3')
    // ⛔ 前端不算钱：金额是后端字符串原样（两位小数）。
    expect(wrapper.find('[data-v2-kpi="period_amount"] [data-v2="kpi-amount"]').text()).toBe('¥2880.00')
    expect(wrapper.find('[data-v2-kpi="avg_order"] [data-v2="kpi-avg"]').text()).toBe('¥411.43')
    // 措辞（第二批拍板「会员」→「客户」）：全页不出现「会员」。
    expect(wrapper.text()).not.toContain('会员')
    expect(wrapper.text()).toContain('客户总数')
    // 缺省档高亮在近30天。
    expect(wrapper.find('[data-v2-range="30d"]').classes()).toContain('cv2-dsh-rbtn--on')
    expect(wrapper.find('[data-v2-range="90d"]').classes()).not.toContain('cv2-dsh-rbtn--on')
  })
})

describe('V2CustomerAnalysis：四图挂载（charts.ts 底座）', () => {
  it('来源分布：真 buildDonutData 上盘——负金额来源不进扇形、占比钉值、中心 = 本期消费合计原串、嵌套形态取色', async () => {
    const wrapper = await mountPage()
    const src = instanceFor(wrapper, 'src-chart')
    const option = lastSetOption(src) as {
      series: {
        radius: string[]
        data: { name: string; itemStyle: { normal: { color: string } } }[]
      }[]
    }
    // 店铺乙 −30.00 被滤掉不进扇形（正金额行才进盘面）。
    expect(option.series[0]!.radius).toEqual(['60%', '100%'])
    expect(option.series[0]!.data.map((item) => item.name)).toEqual(['店铺甲', '闲鱼快照甲'])
    // 切片色 = 名字哈希（applySliceColors 的嵌套形态上盘）。
    for (const item of option.series[0]!.data) {
      expect(item.itemStyle.normal.color).toBe(DONUT_PALETTE[paletteIndexForName(item.name)])
    }
    // 中心 = 后端本期消费合计原串（含负数冲减，⛔ 前端相加 = 2880 不是 2910）。
    expect(centerTextOf(src)).toEqual({ value: '2880.00', label: '本期消费' })
    // 图例（页侧）：名字 + 占比（0.3% 是单个小片候选 → 不并「其他」）。
    const names = wrapper.findAll('[data-v2="src-legend"] .cv2-dsh-legend-name').map((n) => n.text())
    const vals = wrapper.findAll('[data-v2="src-legend"] .cv2-dsh-legend-val').map((n) => n.text())
    expect(names).toEqual(['店铺甲', '闲鱼快照甲'])
    expect(vals).toEqual(['99.7%', '0.3%'])
    // 悬停联动注册（mouseover/mouseout 走控制器）。
    expect(src.on).toHaveBeenCalledWith('mouseover', expect.any(Function))
  })

  it('客户数量扇形（第七批）：count 走 buildDonutData 上盘（值降序、哈希取色）、中心 = 各片之和、图例钉值', async () => {
    const wrapper = await mountPage()
    const cust = instanceFor(wrapper, 'cust-chart')
    const option = lastSetOption(cust) as {
      series: {
        radius: string[]
        data: { name: string; itemStyle: { normal: { color: string } } }[]
      }[]
    }
    // 值降序：5 → 2 → 1；全部 ≥2.5% 不并「其他」。
    expect(option.series[0]!.radius).toEqual(['60%', '100%'])
    expect(option.series[0]!.data.map((item) => item.name)).toEqual([
      '店铺甲',
      '店铺乙',
      '闲鱼快照甲',
    ])
    for (const item of option.series[0]!.data) {
      expect(item.itemStyle.normal.color).toBe(DONUT_PALETTE[paletteIndexForName(item.name)])
    }
    // 中心 = 各片客户数之和（计数不是钱，前端求和合规；标签钉「客户数」）。
    expect(centerTextOf(cust)).toEqual({ value: '8', label: '客户数' })
    // 图例（页侧）钉值：名字 + 占比（5/8=62.5%、2/8=25%、1/8=12.5%）。
    const names = wrapper.findAll('[data-v2="cust-legend"] .cv2-dsh-legend-name').map((n) => n.text())
    const vals = wrapper.findAll('[data-v2="cust-legend"] .cv2-dsh-legend-val').map((n) => n.text())
    expect(names).toEqual(['店铺甲', '店铺乙', '闲鱼快照甲'])
    expect(vals).toEqual(['62.5%', '25.0%', '12.5%'])
    // 悬停联动注册（与来源分布同款）。
    expect(cust.on).toHaveBeenCalledWith('mouseover', expect.any(Function))
  })

  it('消费分布：纯柱四档（x 轴 = 后端 label 原文、柱值钉死、柱色 = 底座常量）', async () => {
    const wrapper = await mountPage()
    const tier = instanceFor(wrapper, 'tier-chart')
    const option = lastSetOption(tier) as {
      xAxis: { data: string[] }
      series: { type: string; itemStyle: { color: string }; data: number[] }[]
    }
    expect(option.xAxis.data).toEqual(['0-100', '100-500', '500-2000', '2000+'])
    expect(option.series).toHaveLength(1)
    expect(option.series[0]!.type).toBe('bar')
    expect(option.series[0]!.itemStyle.color).toBe(COMBO_BAR_COLOR)
    expect(option.series[0]!.data).toEqual([2, 2, 1, 1])
  })

  it('客户增长：双轴柱线 12 点（柱 = 当月新增、线 = 窗口内累计新增、tooltip 行名钉值）', async () => {
    const wrapper = await mountPage()
    const growth = instanceFor(wrapper, 'growth-chart')
    const option = lastSetOption(growth) as {
      xAxis: { data: string[] }
      series: { name: string; type: string; yAxisIndex: number; data: number[] }[]
    }
    expect(option.xAxis.data).toEqual(GROWTH_30D.map((point) => point.month.slice(2)))
    expect(option.series).toHaveLength(2)
    expect(option.series[0]!.name).toBe('当月新增')
    expect(option.series[0]!.type).toBe('bar')
    expect(option.series[0]!.yAxisIndex).toBe(0)
    expect(option.series[0]!.data).toEqual(GROWTH_30D.map((point) => point.count))
    expect(option.series[1]!.name).toBe('累计新增')
    expect(option.series[1]!.type).toBe('line')
    expect(option.series[1]!.yAxisIndex).toBe(1)
    // 线 = 逐月累计（10 月 1、11 月 5）。
    expect(option.series[1]!.data[10]).toBe(1)
    expect(option.series[1]!.data[11]).toBe(5)
  })
})

describe('V2CustomerAnalysis：切档重发与竞态', () => {
  it('点近90天：按 90d 重发、活动档高亮切换、KPI 与中心总计换 90d 读数', async () => {
    const wrapper = await mountPage()
    fetchCustomerAnalysis.mockClear()
    fetchCustomerAnalysis.mockResolvedValue(PAYLOAD_90D)
    await wrapper.find('[data-v2-range="90d"]').trigger('click')
    await flushPromises()
    expect(fetchCustomerAnalysis).toHaveBeenCalledTimes(1)
    expect(fetchCustomerAnalysis).toHaveBeenCalledWith({ range: '90d' })
    expect(wrapper.find('[data-v2-range="90d"]').classes()).toContain('cv2-dsh-rbtn--on')
    expect(wrapper.find('[data-v2-range="30d"]').classes()).not.toContain('cv2-dsh-rbtn--on')
    expect(wrapper.find('[data-v2="kpi-repeat"]').text()).toBe('5')
    expect(wrapper.find('[data-v2="kpi-amount"]').text()).toBe('¥2930.00')
    // 图例换 90d 档（页侧图例吃同一 computed，数据到了才重画）。
    expect(wrapper.find('[data-v2-kpi="avg_order"] [data-v2="kpi-avg"]').text()).toBe('¥366.25')
  })

  it('快速连切（90d 挂起 → 30d 先回）：最终渲染 30d 的数据，旧轮响应落地即弃', async () => {
    let resolveSlow!: (value: CustomerAnalysis) => void
    fetchCustomerAnalysis.mockImplementation((params: { range: string }) => {
      if (params.range === '90d') {
        return new Promise((resolve) => {
          resolveSlow = resolve
        })
      }
      return Promise.resolve(PAYLOAD_30D)
    })
    const wrapper = await mountPage()
    await wrapper.find('[data-v2-range="90d"]').trigger('click')
    await wrapper.find('[data-v2-range="30d"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="kpi-repeat"]').text()).toBe('3')
    resolveSlow(PAYLOAD_90D)
    await flushPromises()
    expect(wrapper.find('[data-v2="kpi-repeat"]').text()).toBe('3')
    expect(wrapper.find('[data-v2="kpi-amount"]').text()).toBe('¥2880.00')
  })
})

describe('V2CustomerAnalysis：空态与失败降级', () => {
  it('来源分布空：给一句空态（不白屏）；增长柱线照画（12 点全零由后端补齐）', async () => {
    fetchCustomerAnalysis.mockResolvedValue({
      ...PAYLOAD_30D,
      source_dist: [],
      source_customer_dist: [],
      kpi: { ...PAYLOAD_30D.kpi, period_amount: '0.00', avg_order: '0.00' },
    })
    const wrapper = await mountPage()
    expect(wrapper.find('[data-v2="src-empty"]').text()).toContain('暂无来源消费数据')
    // 客户数扇形（第七批）空数据同样给空态，不白屏。
    expect(wrapper.find('[data-v2="cust-empty"]').text()).toContain('暂无客户数据')
    expect(instanceFor(wrapper, 'growth-chart')).toBeDefined()
    expect(wrapper.find('[data-v2="kpi-amount"]').text()).toBe('¥0.00')
  })

  it('取数失败：整页错误文案 + KPI 显 —（不给假数）', async () => {
    fetchCustomerAnalysis.mockRejectedValue(new Error('分析端点挂了'))
    const wrapper = await mountPage()
    expect(wrapper.find('[data-v2="load-fail"]').text()).toContain('分析端点挂了')
    expect(wrapper.find('[data-v2="kpi-total"]').text()).toBe('—')
    expect(wrapper.find('[data-v2="kpi-amount"]').text()).toBe('—')
  })
})
