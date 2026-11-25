// @vitest-environment jsdom
// ↑ 挂载组件需要 DOM（本仓 vitest 默认 environment 是 node，只在本文件覆盖）。
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ElDatePicker, ElSelect } from 'element-plus'

/** 「代练通按日统计」v2（V2DailiantongStats）的判据：mock API → **钉值渲染**（已知 13 列
 * 恰好集合按文档顺序 / 金额数量列 cv2-td-num / 合计行）、**列动态四条规则**（多出的键追加
 * 末尾且表头=原始键名；行缺键「—」不补 0；已知键在所有行与合计都缺失 → 整列不画；空回包
 * 退回全部 13 列表头）、真交互（默认窗口页面算好摆进框并发参 / 方向切换发 is_pub / 本地
 * 校验两条 / 失败重试）、状态行三态逐字、权限门 fail-closed。功能语义的出处 = island
 * **自有功能页** `layui-island/pages/dailiantong-stats.ts`（columnsFor / statValue /
 * defaultStatsRange / MESSAGES 逐字对齐）；全部夹具都是**合成值**（PII 纪律）。 */
const fetchDailiantongStats = vi.fn()

vi.mock('@/api/dailiantongExtra', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/dailiantongExtra')>()),
  fetchDailiantongStats: (...args: unknown[]) => fetchDailiantongStats(...args),
}))

import V2DailiantongStats from '../V2DailiantongStats.vue'
import { useSessionStore } from '@/stores/session'

/* ── 合成夹具（键形状照上游 LevelOrderStat 文档；值全部合成）────────────────────────── */

const FULL_ROW: Record<string, unknown> = {
  QDate: '2026-11-01',
  PubCount: 12,
  PubBal: '360.00',
  PubBaseBal: '0.00',
  AcceptCount: 10,
  SettleCount: 8,
  SettleBal: '240.00',
  CancelCount: 1,
  ArbitRateCount: 0,
  PayLevelBal: '12.00',
  ReceiveBal: '0.00',
  QAvgTime: '1.5',
  SettleRate: '80.0%',
}

/** 合计行（上游通常不带 QDate ⇒ 合计首格由页面补字面「合计」）。 */
const FULL_TOTAL: Record<string, unknown> = {
  PubCount: 12,
  PubBal: '360.00',
  PubBaseBal: '0.00',
  AcceptCount: 10,
  SettleCount: 8,
  SettleBal: '240.00',
  CancelCount: 1,
  ArbitRateCount: 0,
  PayLevelBal: '12.00',
  ReceiveBal: '0.00',
  QAvgTime: '1.5',
  SettleRate: '80.0%',
}

function makePayload(overrides: Record<string, unknown> = {}) {
  return {
    items: [{ ...FULL_ROW }],
    total: { ...FULL_TOTAL },
    record_count: 1,
    start: '2026-10-30',
    end: '2026-11-05',
    is_pub: 1,
    ...overrides,
  }
}

/** 已知 13 列表头（组件 STAT_COLUMNS 的中文面，按文档顺序）。 */
const KNOWN_HEADERS = [
  '日期',
  '发单量',
  '发单金额',
  '备注总金额',
  '被接单量',
  '完单量',
  '完单金额',
  '撤单量',
  '介入单量',
  '撤销支付（含介入）',
  '撤销获赔保证金',
  '均单验收时间',
  '完单率',
]

/* ── 默认窗口（与组件 defaultStatsRange 同式：最近 7 天含今天，本地日）────────────────── */

function isoDate(day: Date): string {
  const month = String(day.getMonth() + 1).padStart(2, '0')
  const date = String(day.getDate()).padStart(2, '0')
  return `${day.getFullYear()}-${month}-${date}`
}

function expectedDefaultRange(): { start: string; end: string } {
  const now = new Date()
  const end = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const start = new Date(end)
  start.setDate(start.getDate() - 6)
  return { start: isoDate(start), end: isoDate(end) }
}

/* ── 挂载（mock 会话 + 内存路由；形态照 V2OrderAudit.spec）─────────────────────────── */

function makeRouter(): Router {
  const Blank = { template: '<div />' }
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/v2', name: 'v2', component: Blank, children: [{ path: '', name: 'v2-home', component: Blank }] },
      { path: '/system/dailiantong-stats', name: 'dailiantong-stats', component: Blank },
    ],
  })
}

type Perms = { menu_permissions: string[] }

async function mountPage(
  perms: Perms = { menu_permissions: ['order.list'] },
  flush = true,
) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const session = useSessionStore()
  session.user = {
    id: 9,
    username: 'tester_a',
    display_name: '测试甲',
    identity_plane: 'tenant',
    is_tenant_admin: false,
    is_account_admin: false,
    roles: ['客服'],
    role: '客服',
    permissions: [],
    menu_permissions: perms.menu_permissions,
    tenant_name: '示例工作室甲',
  }
  session.status = 'ready'
  const router = makeRouter()
  await router.push('/v2')
  await router.isReady()
  const wrapper = mount(V2DailiantongStats, { global: { plugins: [router, pinia] } })
  if (flush) await flushPromises()
  return { wrapper, router }
}

function headerTexts(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper']): string[] {
  return wrapper.findAll('[data-v2="stats-table"] thead th').map((th) => th.text())
}

function rowOf(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper'], index: number) {
  return wrapper.find(`[data-v2-row="${index}"]`)
}

/** 按 data-v2-filter 锚点找 el-select（EP 化后 setValue 不再适用——emit 模拟选中）。 */
function selectByAnchor(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper'], anchor: string) {
  const found = wrapper
    .findAllComponents(ElSelect)
    .find((c) => c.attributes('data-v2-filter') === anchor)
  expect(found, `应存在 data-v2-filter="${anchor}" 的 el-select`).toBeDefined()
  return found!
}

/** 起止日期的 ElDatePicker（挂载顺序 = [开始, 结束]；emit 模拟用户改日期）。 */
function datePickers(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper']) {
  const pickers = wrapper.findAllComponents(ElDatePicker)
  expect(pickers.length, '应有两颗日期选择器（开始/结束）').toBe(2)
  return pickers
}

beforeEach(() => {
  vi.clearAllMocks()
  fetchDailiantongStats.mockResolvedValue(makePayload())
})

describe('V2DailiantongStats：权限门（fail-closed）', () => {
  it('无 order.list 节点 ⇒ 只画无权限空态（文案逐字），筛选与表格不画、零请求', async () => {
    const denied = await mountPage({ menu_permissions: ['my_orders'] })
    expect(denied.wrapper.find('[data-v2="no-permission"]').text()).toBe('无权限查看代练通按日统计')
    expect(denied.wrapper.find('[data-v2="filters"]').exists()).toBe(false)
    expect(denied.wrapper.find('[data-v2="stats-table"]').exists()).toBe(false)
    expect(fetchDailiantongStats).not.toHaveBeenCalled()

    const allowed = await mountPage()
    expect(allowed.wrapper.find('[data-v2="no-permission"]').exists()).toBe(false)
    expect(allowed.wrapper.find('[data-v2="filters"]').exists()).toBe(true)
  })
})

describe('V2DailiantongStats：默认窗口与首屏', () => {
  it('默认窗口 = 最近 7 天含今天，由页面算好摆进日期框；首屏发参 {start, end, is_pub:1}', async () => {
    const expected = expectedDefaultRange()
    const { wrapper } = await mountPage()
    expect(fetchDailiantongStats).toHaveBeenCalledTimes(1)
    expect(fetchDailiantongStats).toHaveBeenCalledWith({ start: expected.start, end: expected.end, is_pub: 1 })
    expect(
      (wrapper.find('[data-v2-filter="date-from"] input').element as HTMLInputElement).value,
    ).toBe(expected.start)
    expect(
      (wrapper.find('[data-v2-filter="date-to"] input').element as HTMLInputElement).value,
    ).toBe(expected.end)
  })

  it('首屏先画全部 13 已知列表头 + 状态「加载中…」（回包没到不是白板）', async () => {
    const { wrapper } = await mountPage(undefined, false)
    expect(headerTexts(wrapper)).toEqual(KNOWN_HEADERS)
    expect(wrapper.find('[data-v2="status"]').text()).toBe('加载中…')
    await flushPromises()
    expect(wrapper.find('[data-v2="status"]').text()).toContain('共 1 天数据')
  })

  it('说明句逐字（数据来自当前租户绑定的代练通账号…）', async () => {
    const { wrapper } = await mountPage()
    expect(wrapper.find('[data-v2="intro"]').text()).toBe(
      '数据来自当前租户绑定的代练通账号；按天汇总，日期窗口最多 7 天。',
    )
  })
})

describe('V2DailiantongStats：列动态四条规则（照 island columnsFor）', () => {
  it('① 全 13 键回包 ⇒ 13 列恰好集合、按文档顺序（中文表头逐字）', async () => {
    const { wrapper } = await mountPage()
    expect(headerTexts(wrapper)).toEqual(KNOWN_HEADERS)
  })

  it('② 回包多出的键追加末尾、表头=原始键名（不猜中文）；单元格取值钉值', async () => {
    fetchDailiantongStats.mockResolvedValue(
      makePayload({ items: [{ ...FULL_ROW, BonusField: 5 }] }),
    )
    const { wrapper } = await mountPage()
    expect(headerTexts(wrapper)).toEqual([...KNOWN_HEADERS, 'BonusField'])
    expect(rowOf(wrapper, 0).find('[data-v2-cell="BonusField"]').text()).toBe('5')
  })

  it('③ 行缺键显示「—」（⛔ 不补 0）：另一行有 ReceiveBal，缺键那行画破折号', async () => {
    const rowWithout = { ...FULL_ROW }
    delete rowWithout.ReceiveBal
    fetchDailiantongStats.mockResolvedValue(makePayload({ items: [{ ...FULL_ROW }, rowWithout] }))
    const { wrapper } = await mountPage()
    expect(rowOf(wrapper, 1).find('[data-v2-cell="ReceiveBal"]').text()).toBe('—')
    expect(rowOf(wrapper, 0).find('[data-v2-cell="ReceiveBal"]').text()).toBe('0.00')
  })

  it('④ 已知键在所有行与合计都缺失 ⇒ 整列不画（12 列，撤销获赔保证金消失）', async () => {
    const rowWithout = { ...FULL_ROW }
    delete rowWithout.ReceiveBal
    const totalWithout = { ...FULL_TOTAL }
    delete totalWithout.ReceiveBal
    fetchDailiantongStats.mockResolvedValue(
      makePayload({ items: [rowWithout], total: totalWithout }),
    )
    const { wrapper } = await mountPage()
    const headers = headerTexts(wrapper)
    expect(headers).toHaveLength(12)
    expect(headers).not.toContain('撤销获赔保证金')
    expect(headers[0]).toBe('日期')
  })

  it('④′ 空回包（items 与 total 都空）退回全部 13 列表头 + 空态行/状态行逐字', async () => {
    fetchDailiantongStats.mockResolvedValue(makePayload({ items: [], total: {} }))
    const { wrapper } = await mountPage()
    expect(headerTexts(wrapper)).toEqual(KNOWN_HEADERS)
    expect(wrapper.find('[data-v2="empty"]').text()).toBe('这个区间里还没有统计数据')
    expect(wrapper.find('[data-v2="status"]').text()).toBe('这个区间里还没有统计数据')
  })
})

describe('V2DailiantongStats：状态行三态与合计行', () => {
  it('ready 态：共 N 天数据（回包 start 至 end），N 与窗口都来自回包', async () => {
    fetchDailiantongStats.mockResolvedValue(
      makePayload({
        items: [
          { ...FULL_ROW },
          { ...FULL_ROW, QDate: '2026-11-02' },
          { ...FULL_ROW, QDate: '2026-11-03' },
        ],
        start: '2026-10-30',
        end: '2026-11-05',
      }),
    )
    const { wrapper } = await mountPage()
    expect(wrapper.find('[data-v2="status"]').text()).toBe('共 3 天数据（2026-10-30 至 2026-11-05）')
    expect(wrapper.findAll('[data-v2-row]')).toHaveLength(3)
  })

  it('有 total ⇒ 表尾「合计」行（首格字面「合计」，数值格钉值）；total 空 ⇒ 不画', async () => {
    const { wrapper } = await mountPage()
    const totalRow = wrapper.find('[data-v2="total-row"]')
    expect(totalRow.exists()).toBe(true)
    const cells = totalRow.findAll('td')
    expect(cells[0]!.text()).toBe('合计')
    expect(cells[1]!.text()).toBe('12')
    expect(cells[2]!.text()).toBe('360.00')

    fetchDailiantongStats.mockResolvedValue(makePayload({ total: {} }))
    const second = await mountPage()
    expect(second.wrapper.find('[data-v2="total-row"]').exists()).toBe(false)
  })

  it('加载失败 ⇒ 状态行「加载失败：<后端原文>」+ 重试钮；点重试重发请求', async () => {
    fetchDailiantongStats.mockRejectedValue(new Error('合成错误：代练通账号未绑定'))
    const { wrapper } = await mountPage()
    expect(wrapper.find('[data-v2="status"]').text()).toBe(
      '加载失败：合成错误：代练通账号未绑定重试',
    )
    expect(wrapper.find('[data-v2="status"]').classes()).toContain('cv2-status--error')
    expect(fetchDailiantongStats).toHaveBeenCalledTimes(1)

    fetchDailiantongStats.mockResolvedValue(makePayload())
    await wrapper.find('[data-v2="retry"]').trigger('click')
    await flushPromises()
    expect(fetchDailiantongStats).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[data-v2="status"]').text()).toContain('共 1 天数据')
  })
})

describe('V2DailiantongStats：真交互（方向 / 本地校验）', () => {
  it('方向切「接单」再查询 ⇒ is_pub 发 0；默认查询发 1', async () => {
    const expected = expectedDefaultRange()
    const { wrapper } = await mountPage()
    fetchDailiantongStats.mockClear()
    selectByAnchor(wrapper, 'side').vm.$emit('update:modelValue', 0)
    await wrapper.find('[data-v2="search"]').trigger('click')
    await flushPromises()
    expect(fetchDailiantongStats).toHaveBeenLastCalledWith({ start: expected.start, end: expected.end, is_pub: 0 })
  })

  it('本地校验①：两格日期都清空 ⇒ 零请求，状态行「加载失败：请选择开始日期和结束日期」', async () => {
    const { wrapper } = await mountPage()
    fetchDailiantongStats.mockClear()
    const pickers = datePickers(wrapper)
    pickers[0]!.vm.$emit('update:modelValue', '')
    pickers[1]!.vm.$emit('update:modelValue', '')
    await flushPromises()
    await wrapper.find('[data-v2="search"]').trigger('click')
    await flushPromises()
    expect(fetchDailiantongStats).not.toHaveBeenCalled()
    expect(wrapper.find('[data-v2="status"]').text()).toContain('请选择开始日期和结束日期')
    expect(wrapper.find('[data-v2="retry"]').exists()).toBe(true)
  })

  it('本地校验②：开始晚于结束 ⇒ 零请求，状态行「加载失败：开始日期不能晚于结束日期」', async () => {
    const { wrapper } = await mountPage()
    fetchDailiantongStats.mockClear()
    const pickers = datePickers(wrapper)
    pickers[0]!.vm.$emit('update:modelValue', '2026-11-10')
    pickers[1]!.vm.$emit('update:modelValue', '2026-11-01')
    await flushPromises()
    await wrapper.find('[data-v2="search"]').trigger('click')
    await flushPromises()
    expect(fetchDailiantongStats).not.toHaveBeenCalled()
    expect(wrapper.find('[data-v2="status"]').text()).toContain('开始日期不能晚于结束日期')
  })
})

describe('V2DailiantongStats：渲染形态与出口', () => {
  it('金额/数量列右对齐等宽（cv2-td-num 恰好 10 列）；日期/时长/率不进', async () => {
    const { wrapper } = await mountPage()
    const row = rowOf(wrapper, 0)
    for (const key of ['PubCount', 'PubBal', 'PubBaseBal', 'AcceptCount', 'SettleCount', 'SettleBal', 'CancelCount', 'ArbitRateCount', 'PayLevelBal', 'ReceiveBal']) {
      expect(row.find(`[data-v2-cell="${key}"]`).classes()).toContain('cv2-td-num')
    }
    for (const key of ['QDate', 'QAvgTime', 'SettleRate']) {
      expect(row.find(`[data-v2-cell="${key}"]`).classes()).not.toContain('cv2-td-num')
    }
    // 合计行的数值格同样右对齐。
    expect(wrapper.find('[data-v2="total-row"] td:nth-child(2)').classes()).toContain('cv2-td-num')
  })

  it('只读页：没有写动作按钮（新增/删除/编辑类锚点一律不存在）', async () => {
    const { wrapper } = await mountPage()
    expect(wrapper.find('[data-v2="create"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2-delete]').exists()).toBe(false)
    expect(wrapper.find('[data-v2-edit]').exists()).toBe(false)
    expect(wrapper.find('button').text()).not.toContain('删除')
  })
})
