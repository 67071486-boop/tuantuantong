// @vitest-environment jsdom
// ↑ 挂载组件需要 DOM（本仓 vitest 默认 environment 是 node，只在本文件覆盖）。
import { readFileSync } from 'node:fs'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/** 「我的订单收入」v2（V2Income）的判据：mock API → **钉值渲染**（列 = 旧页真有值的 10 列 / 状态签 / 金额 / 结算状态签）、真交互（5 个客户端筛选 / 分页 / 单号详情层 / 重置）、空态与失败态、假控件不渲染（恒 0 统计骨架与「尚未开放」按钮的反面不存在）。功能语义的出处 = island 复刻页 `layui-island/pages/emp-myemp.ts`（组件文件头有逐条对照表）；全部夹具都是**合成值**（PII 纪律）。
 * 「我的订单收入」v2（V2Income）的判据：mock API → **钉值渲染**（列 = 旧页真有值的 10 列 / 状态签 / 金额 / 结算状态签）、真交互（5 个客户端筛选 / 分页 / 单号详情层 / 重置）、空态与失败态、假控件不渲染（恒 0 统计骨架与「尚未开放」按钮的反面不存在）。功能语义的出处 = island 复刻页 `layui-island/pages/emp-myemp.ts`（组件文件头有逐条对照表）；全部夹具都是**合成值**（PII 纪律）。 */
import type { MyOrderItem } from '@/api/myOrders'

const fetchAllMyOrders = vi.fn()
const applyForSettlement = vi.fn()
const downloadMyOrderExport = vi.fn()

vi.mock('@/api/myOrders', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/myOrders')>()),
  fetchAllMyOrders: (...args: unknown[]) => fetchAllMyOrders(...args),
  // ⭐ D-0365：两个写/导出出口一并替身（真实现会打 axios，jsdom 里跑不了）。
  applyForSettlement: (...args: unknown[]) => applyForSettlement(...args),
  downloadMyOrderExport: (...args: unknown[]) => downloadMyOrderExport(...args),
}))

import { ElDatePicker, ElSelect } from 'element-plus'

import V2Income from '../V2Income.vue'

/** 三行合成数据（形状照 `MyOrderItem`）：打单工资+已结账 / 销售提成+未结账 / username null。 */
/** 与页面同式：默认起始日 = 今天往前推 6 个月（YYYY-MM-DD；D-0371 第六轮默认窗口）。 */
const SIX_MONTHS_AGO = (() => {
  const d = new Date()
  d.setMonth(d.getMonth() - 6)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
})()

const ROW_A: MyOrderItem = {
  id: 201,
  order_no: '44444444444444444444',
  source_order_no: '',
  status: 'IN_PROGRESS',
  game_name: '示例游戏一',
  project_text: '示例项目-陪玩上分',
  is_urgent: false,
  temp_amount: '120.00',
  bonus_amount: '100.00',
  review_status: null,
  created_at: '2026-10-01T10:30:00+08:00',
  username: 'tester_a',
  taker_name: '测试甲',
  emp_type: '打单工资',
  settle_status: '已结账',
  taker_price: '88.00',
  // ⭐ D-0365：`settle_requested_at`（2026-10-23 起就在载荷里）。
  settle_requested_at: null,
}
/** 有提前结算申请的一行（结算状态格旁「已申请」小标记的那一档）。 */
const ROW_APPLIED: MyOrderItem = {
  ...ROW_A,
  id: 204,
  order_no: '77777777777777777777',
  settle_requested_at: '2026-10-20T09:00:00+08:00',
}
const ROW_B: MyOrderItem = {
  ...ROW_A,
  id: 202,
  order_no: '55555555555555555555',
  status: 'COMPLETED',
  game_name: '示例游戏二',
  project_text: '示例项目-代练升级',
  temp_amount: '60.50',
  bonus_amount: '55.00',
  created_at: '2026-10-02T09:00:00+08:00',
  emp_type: '销售提成',
  settle_status: '未结账',
}
const ROW_C: MyOrderItem = {
  ...ROW_A,
  id: 203,
  order_no: '66666666666666666666',
  status: 'REFUNDED',
  game_name: '示例游戏一',
  project_text: '示例项目-DailyTask',
  created_at: '2026-09-01T08:00:00+08:00',
  username: null,
}

const THREE_ROWS = [ROW_A, ROW_B, ROW_C]

/** 25 行合成数据（分页用：默认每页 20 ⇒ 第 2 页 5 行）。 */
function manyRows(count: number): MyOrderItem[] {
  return Array.from({ length: count }, (_, i) => ({
    ...ROW_A,
    id: i + 1,
    order_no: String(i + 1).padStart(20, '0'),
  }))
}

function makeRouter(): Router {
  const Blank = { template: '<div />' }
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/v2', name: 'v2', component: Blank, children: [{ path: '', name: 'v2-home', component: Blank }] },
      { path: '/agents/my-income', name: 'agents-my-income', component: Blank },
    ],
  })
}

type Mounted = { wrapper: ReturnType<typeof mount>; router: Router }

async function mountPage(): Promise<Mounted> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const router = makeRouter()
  // ⚠️ vue-router 5：`isReady()` 必须在初始导航落在真实路由之后才 resolve（与 V2MyOrders.spec 同一口径）。
  await router.push('/v2')
  await router.isReady()
  const wrapper = mount(V2Income, { global: { plugins: [router, pinia] } })
  await flushPromises()
  return { wrapper, router }
}

/** 行按数据行主键找 tr。 */
function rowOf(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper'], id: number) {
  return wrapper.find(`[data-v2-row="${id}"]`)
}

/** 按 data-v2-filter 锚点找 el-select 组件（EP 化后原生 setValue 不再适用——emit 模拟选中）。 */
function selectByAnchor(
  wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper'],
  anchor: string,
) {
  const found = wrapper
    .findAllComponents(ElSelect)
    .find((c) => c.attributes('data-v2-filter') === anchor)
  expect(found, `应存在 data-v2-filter="${anchor}" 的 el-select`).toBeDefined()
  return found!
}

beforeEach(() => {
  vi.clearAllMocks()
  fetchAllMyOrders.mockResolvedValue(THREE_ROWS)
})

describe('V2Income：钉值渲染（列 = 旧页真有值的那些）', () => {
  it('三行渲染：单号蓝链、状态签（中文+token 类）、结算状态签、金额右对齐等宽', async () => {
    const { wrapper } = await mountPage()
    expect(fetchAllMyOrders).toHaveBeenCalledTimes(1)
    expect(wrapper.findAll('[data-v2-row]')).toHaveLength(3)
    const rowA = rowOf(wrapper, ROW_A.id)
    expect(rowA.find('[data-v2-detail]').text()).toBe(ROW_A.order_no)
    expect(rowA.find('[data-v2-status]').text()).toBe('代练中')
    expect(rowA.find('[data-v2-status]').classes()).toContain('cv2-st--doing')
    expect(rowA.find('[data-v2-settle]').text()).toBe('已结账')
    // 结算状态跟订单状态同一套色点（不套胶囊）。已结账仍用 settled 档，不借已完成的蓝。
    expect(rowA.find('[data-v2-settle]').classes()).toContain('cv2-st--mark')
    expect(rowA.find('[data-v2-settle]').classes()).toContain('cv2-st--settled')
    expect(rowA.find('[data-v2-settle]').classes()).not.toContain('cv2-st--done')
    expect(rowA.find('[data-v2-settle] .cv2-dot').exists()).toBe(true)
    expect(rowA.find('[data-v2-amount]').text()).toBe('120.00') // 后端两位小数字符串原样
    // ⭐ 业务方「预计收入/实际收入/收入类型用红色字体」：金额锚点移到红字 span 上，
    //    右对齐等宽仍在 td（cv2-td-num）。
    expect(rowA.find('[data-v2-amount]').classes()).toContain('cv2-money')
    expect(rowA.find('td.cv2-td-num [data-v2-amount]').exists()).toBe(true)
    const rowB = rowOf(wrapper, ROW_B.id)
    expect(rowB.find('[data-v2-status]').text()).toBe('已完成')
    // ⭐ 未结账 = 浅灰带蓝灰独立档（不借待抢单琥珀）。
    expect(rowB.find('[data-v2-settle]').text()).toBe('未结账')
    expect(rowB.find('[data-v2-settle]').classes()).toContain('cv2-st--mark')
    expect(rowB.find('[data-v2-settle]').classes()).toContain('cv2-st--unsettled')
    const carbonCss = readFileSync(
      path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../carbon.css'),
      'utf8',
    )
    expect(carbonCss).toMatch(
      /\.cv2-st\.cv2-st--mark:where\(:not\(\.cv2-order-status\)\)\.cv2-st--unsettled\s*\{[^}]*--st-ink:\s*var\(--tt2-st-unsettled\)/,
    )
    expect(rowB.find('[data-v2-settle]').classes()).not.toContain('cv2-st--pending')
    // username null ⇒ 空单元格（不显示 "null"）。
    expect(rowOf(wrapper, ROW_C.id).findAll('td')[0]!.text()).toBe('')
  })

  it('金额/收入类型红字（⭐ 2026-11-03 业务方）：cv2-money 在 carbon.css（st-error + 等宽数字）；预计/实际收入、收入类型三格都挂', async () => {
    const { wrapper } = await mountPage()
    const rowA = rowOf(wrapper, ROW_A.id)
    expect(rowA.findAll('.cv2-money').length).toBe(3)
    const carbonCss = readFileSync(
      path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../carbon.css'),
      'utf8',
    )
    expect(carbonCss).toMatch(/\.cv2-money\s*\{[^}]*color:\s*var\(--tt2-money-red\)/)
  })

  it('列宽拖拽手柄：每列 th 右缘一枚（⭐ 2026-11-03 业务方「单号并一列」：11 数据列；拖拽行为钉在 tableResize.spec）', async () => {
    const { wrapper } = await mountPage()
    const headers = wrapper.findAll('.cv2-table thead th')
    // 表头 = 勾选列（无手柄，固定 36px）+ 11 数据列（各一枚）
    expect(headers.length).toBe(12)
    // ⭐ temp/bonus/type/status/time/settle/applied 七列转固定（无手柄）⇒ 手柄 = 4
    expect(wrapper.findAll('.cv2-th-resizer').length).toBe(3)
    // 可拖列恰好三条（订单号/游戏名称/代练项目）；用户名称 2026-11-03 转 110 固定，短列全固定。
    for (const key of ['order', 'game', 'project']) {
      expect(wrapper.find(`[data-v2-col="${key}"]`).exists(), `${key} 可拖`).toBe(true)
    }
    for (const key of ['user', 'temp', 'bonus', 'type', 'status', 'time', 'settle', 'applied']) {
      expect(wrapper.find(`[data-v2-col="${key}"]`).exists(), `${key} 已转固定`).toBe(false)
    }
  })

  it('账单描述句（⭐ D-0371 第六轮）：数字 = 当前筛选行真值聚合——条数 / 不含已退单的预计与实际 / 已结未结笔数与金额 / 默认窗口句', async () => {
    const { wrapper } = await mountPage()
    const t = wrapper.find('[data-v2="total"]').text()
    expect(t).toContain('共计3条账单')
    // 合计排除已退单（ROW_C 已退单 ⇒ 不进 180.50/155.00）；已结账金额取实收、未结账取预计。
    expect(t).toContain('预计收入180.50元')
    expect(t).toContain('实际收入155.00元')
    expect(t).toContain('已结账2笔（200.00元）')
    expect(t).toContain('还未结账1笔（预计有60.50元）')
    expect(t).toContain('默认显示最近6个月数据，若需扩大时间范围，可手动选择起始日期')
  })

  it('假控件不渲染：孤行工具栏不渲染，筛选动作区 = 重置/刷新 + D-0365 真功能（申请结算/导出记录）；「同步结算状态」「尚未开放」不出现', async () => {
    const { wrapper } = await mountPage()
    // ⭐ D-0354 追记 3：孤行工具栏删除，动作钮全住筛选行（同大厅）。
    // ⛔（D-0365）「申请结算 / 导出记录」是真功能（后端端点已实装）⇒ 本用例断它们住进
    expect(wrapper.find('[data-v2="toolbar"]').exists()).toBe(false)
    const actions = wrapper.find('[data-v2="filters"] .cv2-filter-actions')
    const reset = actions.find('[data-v2="reset"]')
    const refresh = actions.find('[data-v2="refresh"]')
    expect(refresh.exists()).toBe(true)
    expect(refresh.classes()).toContain('cv2-btn--find')
    expect(reset.classes()).toContain('cv2-btn--quiet')
    expect(actions.find('[data-v2="apply-settlement"]').classes()).toContain('cv2-btn--commit')
    expect(actions.find('[data-v2="export"]').classes()).toContain('cv2-btn--quiet')
    expect(
      refresh.element.compareDocumentPosition(reset.element) & Node.DOCUMENT_POSITION_FOLLOWING,
      '重置必须紧挨在「刷新列表」右侧',
    ).toBeTruthy()
    const actionsText = actions.text()
    expect(actionsText).toContain('刷新列表')
    expect(actions.find('[data-v2="apply-settlement"]').exists(), '申请结算 = D-0365 真功能').toBe(true)
    expect(actions.find('[data-v2="export"]').exists(), '导出记录 = D-0365 真功能').toBe(true)
    expect(actionsText).not.toContain('同步结算状态')
    expect(wrapper.text()).not.toContain('尚未开放')
  })

  it('是否已申请结算列：`settle_requested_at` 有值 ⇒「已申请」；无 ⇒「未申请」（⭐ 2026-11-03 终审 P2：原格内徽标与整列同义重复，删徽标留列）', async () => {
    fetchAllMyOrders.mockResolvedValue([ROW_A, ROW_APPLIED])
    const { wrapper } = await mountPage()
    expect(rowOf(wrapper, ROW_APPLIED.id).find('[data-v2-applied-col]').text()).toBe('已申请')
    expect(rowOf(wrapper, ROW_A.id).find('[data-v2-applied-col]').text()).toBe('未申请')
    expect(rowOf(wrapper, ROW_A.id).find('.cv2-applied').exists()).toBe(false)
  })
})

it('订单状态变化时回到第一页', async () => {
  fetchAllMyOrders.mockResolvedValue(manyRows(25))
  const { wrapper } = await mountPage()
  await wrapper.find('[data-v2="pager"] .btn-next').trigger('click')
  await flushPromises()
  expect(rowOf(wrapper, 21).exists()).toBe(true)
  selectByAnchor(wrapper, 'order-status').vm.$emit('update:modelValue', [ROW_A.status])
  selectByAnchor(wrapper, 'order-status').vm.$emit('change', [ROW_A.status])
  await flushPromises()
  expect(rowOf(wrapper, 1).exists()).toBe(true)
})

it('订单状态筛选与重置同步影响列表', async () => {
  const { wrapper } = await mountPage()
  selectByAnchor(wrapper, 'order-status').vm.$emit('update:modelValue', [ROW_A.status])
  await flushPromises()
  expect(wrapper.findAll('[data-v2-row]')).toHaveLength(1)
  expect(rowOf(wrapper, ROW_A.id).exists()).toBe(true)
  await wrapper.find('[data-v2="reset"]').trigger('click')
  await flushPromises()
  expect(wrapper.findAll('[data-v2-row]')).toHaveLength(3)
})

describe('⭐ D-0365 日期三控件（服务端筛选，改了即重取）', () => {
  it('换日期口径 ⇒ 重取且 `date_type` 发映射值（默认窗口随行；钉字面契约）', async () => {
    const { wrapper } = await mountPage()
    fetchAllMyOrders.mockClear()
    selectByAnchor(wrapper, 'date-type').vm.$emit('update:modelValue', 'settled')
    selectByAnchor(wrapper, 'date-type').vm.$emit('change', 'settled')
    await flushPromises()
    expect(fetchAllMyOrders).toHaveBeenLastCalledWith({ date_type: 'settled', date_from: SIX_MONTHS_AGO })
  })

  it('起止日期 + 口径一起发（`date_from`/`date_to` 是 `YYYY-MM-DD` 原值）', async () => {
    const { wrapper } = await mountPage()
    fetchAllMyOrders.mockClear()
    wrapper.findAllComponents(ElDatePicker)[0]!.vm.$emit('update:modelValue', '2026-01-01')
    await flushPromises()
    wrapper.findAllComponents(ElDatePicker)[1]!.vm.$emit('update:modelValue', '2026-01-31')
    await flushPromises()
    selectByAnchor(wrapper, 'date-type').vm.$emit('update:modelValue', 'claimed')
    selectByAnchor(wrapper, 'date-type').vm.$emit('change', 'claimed')
    await flushPromises()
    expect(fetchAllMyOrders).toHaveBeenLastCalledWith({
      date_type: 'claimed',
      date_from: '2026-01-01',
      date_to: '2026-01-31',
    })
  })

  it('只给日期不给口径 ⇒ 日期**不发**（与后端「缺档不筛」成对）；重置 = 回默认最近 6 个月窗口（D-0371 第六轮）', async () => {
    const { wrapper } = await mountPage()
    fetchAllMyOrders.mockClear()
    // 默认口径已是 created（第六轮默认窗口）⇒ 显式切回「不按日期筛」再验「缺档不筛」契约。
    selectByAnchor(wrapper, 'date-type').vm.$emit('update:modelValue', '')
    selectByAnchor(wrapper, 'date-type').vm.$emit('change', '')
    await flushPromises()
    wrapper.findAllComponents(ElDatePicker)[0]!.vm.$emit('update:modelValue', '2026-01-01')
    await flushPromises()
    // 没有 date_type ⇒ 日期参数整个不发（⛔ 不猜默认列）。
    expect(fetchAllMyOrders).toHaveBeenLastCalledWith({})
    await wrapper.find('[data-v2="reset"]').trigger('click')
    await flushPromises()
    expect(fetchAllMyOrders).toHaveBeenLastCalledWith({ date_type: 'created', date_from: SIX_MONTHS_AGO })
    expect(wrapper.findAllComponents(ElDatePicker)[0]!.props('modelValue')).toBe(SIX_MONTHS_AGO)
  })
})

describe('⭐ D-0365 申请结算（勾选批量；后端逐条判，前端只汇总）', () => {
  it('无勾选 ⇒ toast「请先勾选要申请的行」，零请求', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="apply-settlement"]').trigger('click')
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('请先勾选要申请的行')
    expect(applyForSettlement).not.toHaveBeenCalled()
  })

  it('勾两行申请（一成一败）⇒ POST 绩效 id；toast 成功数 + 失败 msg 原样；成功后清空选中并重取', async () => {
    const { wrapper } = await mountPage()
    await rowOf(wrapper, ROW_A.id).find('[data-v2-check]').setValue(true)
    await rowOf(wrapper, ROW_B.id).find('[data-v2-check]').setValue(true)
    applyForSettlement.mockResolvedValue({
      items: [
        { performance_id: ROW_A.id, ok: true, code: 0, msg: '', settle_request: {} },
        {
          performance_id: ROW_B.id,
          ok: false,
          code: 1035,
          msg: '该绩效已申请过提前结算，请等待财务处理',
        },
      ],
      early_settlement: { used: 1, limit: 3 },
    })
    await wrapper.find('[data-v2="apply-settlement"]').trigger('click')
    await flushPromises()
    expect(applyForSettlement).toHaveBeenCalledWith([ROW_A.id, ROW_B.id])
    expect(wrapper.find('[data-v2="toast"]').text()).toContain(
      '已提交 1 条提前结算申请；该绩效已申请过提前结算，请等待财务处理',
    )
    // 成功 ≥1 ⇒ 清空选中 + 重取。
    expect(fetchAllMyOrders).toHaveBeenCalledTimes(2)
    expect(
      (rowOf(wrapper, ROW_A.id).find('[data-v2-check]').element as HTMLInputElement).checked,
    ).toBe(false)
  })

  it('选中集**跨页保留**（翻页不清空；表头全选只动本页），全失败不重取', async () => {
    fetchAllMyOrders.mockResolvedValue(manyRows(25))
    const { wrapper } = await mountPage()
    await rowOf(wrapper, 1).find('[data-v2-check]').setValue(true)
    await wrapper.find('[data-v2="pager"] .btn-next').trigger('click')
    await flushPromises()
    await rowOf(wrapper, 21).find('[data-v2-check]').setValue(true)
    // 翻回第 1 页 ⇒ 第 1 行仍勾着（跨页保留）。
    await wrapper.find('[data-v2="pager"] .btn-prev').trigger('click')
    await flushPromises()
    expect(
      (rowOf(wrapper, 1).find('[data-v2-check]').element as HTMLInputElement).checked,
    ).toBe(true)
    // 表头全选 = 本页 20 行补齐（第 21 行在别的页，不动它）。
    await wrapper.find('[data-v2="check-all"]').setValue(true)
    applyForSettlement.mockResolvedValue({
      items: [{ performance_id: 1, ok: false, code: 1036, msg: '提前结算申请额度已用完（3/3）' }],
      early_settlement: { used: 3, limit: 3 },
    })
    await wrapper.find('[data-v2="apply-settlement"]').trigger('click')
    await flushPromises()
    const sent = applyForSettlement.mock.calls[0]?.[0] as number[]
    expect(sent).toContain(21)
    expect(sent).toHaveLength(21) // 第 1 页 20 行 + 第 2 页勾的第 21 行
    // 全失败 ⇒ 不重取（mount 1 次，无第二次）。
    expect(fetchAllMyOrders).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('提前结算申请额度已用完（3/3）')
  })
})

describe('⭐ D-0365 导出记录（当前筛选打包 → Blob 下载）', () => {
  it('点导出 ⇒ `downloadMyOrderExport` 收到当前筛选 + 日期的服务端镜像；文件落下载', async () => {
    const { wrapper } = await mountPage()
    const createObjectURL = vi.fn(() => 'blob:mock')
    Object.defineProperty(URL, 'createObjectURL', { value: createObjectURL, writable: true })
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    selectByAnchor(wrapper, 'settle').vm.$emit('update:modelValue', '未结账')
    selectByAnchor(wrapper, 'settle').vm.$emit('change', '未结账')
    selectByAnchor(wrapper, 'order-status').vm.$emit('update:modelValue', ['COMPLETED', 'IN_PROGRESS'])
    selectByAnchor(wrapper, 'date-type').vm.$emit('update:modelValue', 'submitted')
    selectByAnchor(wrapper, 'date-type').vm.$emit('change', 'submitted')
    await flushPromises()
    const blob = new Blob(['xlsx-bytes'])
    downloadMyOrderExport.mockResolvedValue({ blob, filename: '收入记录20261102.xlsx' })
    await wrapper.find('[data-v2="export"]').trigger('click')
    await flushPromises()
    expect(downloadMyOrderExport).toHaveBeenCalledWith({
      settle_status: '未结账',
      order_status: 'COMPLETED,IN_PROGRESS',
      date_type: 'submitted',
      date_from: SIX_MONTHS_AGO,
    })
    expect(createObjectURL).toHaveBeenCalledWith(blob)
    expect(clickSpy).toHaveBeenCalled()
    clickSpy.mockRestore()
    Object.defineProperty(URL, 'createObjectURL', { value: undefined, writable: true })
  })

  it('导出失败 ⇒ 错误 toast 显示后端 msg（6000ms 那一档，文字原样）', async () => {
    const { wrapper } = await mountPage()
    downloadMyOrderExport.mockRejectedValue(new Error('本次导出 2 行，超过上限 1 行，请先收窄筛选条件'))
    await wrapper.find('[data-v2="export"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('超过上限')
    expect(wrapper.find('[data-v2="toast"]').classes()).toContain('cv2-toast--error')
  })
})

describe('V2Income：筛选（本地筛全量行，判据 = island 页 applyFilters 的 5 条 AND）', () => {
  it('游戏下拉按游戏名筛；结算状态筛「未结账」⇒ 只剩行 B', async () => {
    const { wrapper } = await mountPage()
    // EP 化：下拉选中 = emit update:modelValue（v-model）+ change（@change → page = 1）。
    // ⚠️ 裸 $emit 不像 setValue/trigger 那样内置等 nextTick ⇒ 每次emit后 flush 再断言 DOM。
    selectByAnchor(wrapper, 'game').vm.$emit('update:modelValue', '示例游戏二')
    selectByAnchor(wrapper, 'game').vm.$emit('change', '示例游戏二')
    await flushPromises()
    expect(wrapper.findAll('[data-v2-row]')).toHaveLength(1)
    expect(rowOf(wrapper, ROW_B.id).exists()).toBe(true)
    selectByAnchor(wrapper, 'game').vm.$emit('update:modelValue', '')
    selectByAnchor(wrapper, 'game').vm.$emit('change', '')
    await flushPromises()
    selectByAnchor(wrapper, 'settle').vm.$emit('update:modelValue', '未结账')
    selectByAnchor(wrapper, 'settle').vm.$emit('change', '未结账')
    await flushPromises()
    expect(wrapper.findAll('[data-v2-row]')).toHaveLength(1)
    expect(rowOf(wrapper, ROW_B.id).exists()).toBe(true)
  })

  it('顶部状态卡片已撤，三行都还在', async () => {
    const { wrapper } = await mountPage()
    expect(wrapper.find('[data-v2="status-cards"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2-scard]').exists()).toBe(false)
    expect(wrapper.find('[data-v2="status-chips"]').exists()).toBe(false)
    expect(wrapper.findAll('[data-v2-row]')).toHaveLength(3)
  })

  it('筛选栏保持横排一行、标签左置（业务方 D-0354 重申不回退）：字段 = cv2-field 且小字标签是第一个子元素；状态签行不复存在', async () => {
    const { wrapper } = await mountPage()
    const row = wrapper.find('[data-v2="filters"] .cv2-filter-row')
    const fields = row.findAll('.cv2-field')
    expect(fields.length).toBe(5) // 选择游戏 / 结算状态 / 搜索1 / 搜索2 + ⭐ D-0365 日期三控件（口径/起/止）
    for (const f of fields) {
      expect(f.element.firstElementChild!.classList.contains('cv2-field-label')).toBe(true)
    }
    // 横排载体（flex wrap 一行摊开）= cv2-filter-row 的 CSS 形状，钉在 carbon.css（V2MyOrders.spec 同钉）。
    expect(row.classes()).toContain('cv2-filter-row')
  })

  it('搜索1（单号）/搜索2（内容）子串**大小写不敏感**（island 页同一行为）；重置按钮恢复全部', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-filter="key1"]').setValue('5555') // el-input：锚点即内层原生 input
    expect(wrapper.findAll('[data-v2-row]')).toHaveLength(1)
    await wrapper.find('[data-v2-filter="key1"]').setValue('')
    await wrapper.find('[data-v2-filter="key2"]').setValue('dailytask') // 数据里是 DailyTask
    expect(wrapper.findAll('[data-v2-row]')).toHaveLength(1)
    expect(rowOf(wrapper, ROW_C.id).exists()).toBe(true)
    await wrapper.find('[data-v2="reset"]').trigger('click')
    expect(wrapper.findAll('[data-v2-row]')).toHaveLength(3)
  })

  it('筛出 0 行 ⇒ 空态一句话（暂无数据）', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-filter="key1"]').setValue('不存在的单号')
    expect(wrapper.find('[data-v2="empty"]').text()).toContain('暂无数据')
  })

  it('取数失败 ⇒ 表清空 + 错误 toast（后端 msg 原样显示，不拿空表冒充正常）', async () => {
    fetchAllMyOrders.mockRejectedValue(new Error('合成错误：没有 my_orders 权限'))
    const { wrapper } = await mountPage()
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('合成错误：没有 my_orders 权限')
    expect(wrapper.find('[data-v2="empty"]').exists()).toBe(true)
  })
})

describe('V2Income：分页（客户端；limit 20 / limits [10,20,30,40,50,100] = 本页对标自己的值）', () => {
  it('25 行时翻到第 2 页，首行换成第 21 行；每页条数切换生效并拨回第 1 页', async () => {
    fetchAllMyOrders.mockResolvedValue(manyRows(25))
    const { wrapper } = await mountPage()
    expect(wrapper.find('[data-v2="pager-total"]').text()).toBe('共 25 条')
    expect(rowOf(wrapper, 1).exists()).toBe(true)
    await wrapper.find('[data-v2="pager"] .btn-next').trigger('click')
    await flushPromises()
    expect(rowOf(wrapper, 21).exists()).toBe(true)
    expect(rowOf(wrapper, 1).exists()).toBe(false)
    await wrapper.find('[data-v2="page-size"]').setValue('10')
    await flushPromises()
    expect(rowOf(wrapper, 1).exists()).toBe(true)
    await wrapper.find('[data-v2="pager"] .btn-next').trigger('click')
    await flushPromises()
    expect(rowOf(wrapper, 11).exists()).toBe(true)
  })
})

describe('V2Income：单号蓝链 → 订单详情弹层（零新端点；20 位形状闸照搬 island 页）', () => {
  it('合法 20 位单号 ⇒ 弹层装 order-orderview island 页（标题 = 单号）；⭐ 弹窗档 = ⭐ xl 1400（D-0354「详情要左右布局」+ 2026-11-03 业务方「详情页面也拉宽一点」）', async () => {
    const { wrapper } = await mountPage()
    await rowOf(wrapper, ROW_A.id).find('[data-v2-detail]').trigger('click')
    const dlg = wrapper.find('[data-v2="dlg-detail"]')
    expect(dlg.exists()).toBe(true)
    expect(dlg.classes()).toContain('cv2-dialog--detail')
    expect(dlg.find('iframe').exists()).toBe(false)
    expect(dlg.find('[data-v2="order-detail-pane"]').exists()).toBe(true)
    expect(dlg.text()).toContain(ROW_A.order_no)
  })

  it('非 20 位单号 ⇒ 不开层 + 固定文案（⛔ 不把不可信输入拼进提示）', async () => {
    fetchAllMyOrders.mockResolvedValue([{ ...ROW_A, id: 999, order_no: 'SHORT-NO' }])
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-detail]').trigger('click')
    expect(wrapper.find('[data-v2="dlg-detail"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('订单号格式不正确，无法打开订单详情')
  })
})
