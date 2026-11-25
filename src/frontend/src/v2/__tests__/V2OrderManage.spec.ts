// @vitest-environment jsdom
// ↑ 挂载组件需要 DOM（本仓 vitest 默认 environment 是 node，只在本文件覆盖）。
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { ElDatePicker, ElInput } from 'element-plus'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 「管理订单」v2 的**打手 / 发单人拾人面板**远程关键词搜索（2026-11-09，业务方拍板
 * 「打手/下家下拉全改关键词搜，v2 也带上」；旧页 order-orderlist 同款语义的 v2 落法）。
 *
 * 判据三条（照 island order-orderlist.spec 的手法）：
 * ① **不预载**——挂载 / 切游戏 / 重置零 `filter-options` 请求（回归成预载会让本文件红）；
 * ② **关键词请求参数**——面板里输入关键词（400ms 防抖）⇒ `fetchFilterOptions({game, search})`，
 *    taker 槽取 `takers`、publisher 槽取 `publishers`（判别力：两份名单不同形，取错当场红）；
 * ③ **已选值保留**——选中后候选被下一次搜索整体替换，按钮上的名字不丢、提交参数不变
 *    （选人缓存 `pickedNames` 的整链验证：DOM → 状态 → 请求载荷）。
 * 空关键词不发请求（候选清空，面板显示提示句）。全部夹具都是合成值（PII 纪律）。
 */
const fetchFilterOptions = vi.fn()
const fetchKefuOptions = vi.fn()
const fetchOrder = vi.fn()
const fetchOrderAudit = vi.fn()
const fetchPenaltyChildren = vi.fn()
const fetchSplitMembers = vi.fn()
const splitPeerOrder = vi.fn()
const updateOrder = vi.fn()
const updateOrderWithMessage = vi.fn()
const batchOrderAction = vi.fn()
const clearTaker = vi.fn()
const fetchAssigneeOptions = vi.fn()
const batchAssignOrders = vi.fn()
const fetchFavoriteAssignees = vi.fn()
const setFavoriteAssignee = vi.fn()
const startXgjReceiptSync = vi.fn()

vi.mock('@/api/orders', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/orders')>()
  return {
    ...actual,
    fetchFilterOptions: (...args: unknown[]) => fetchFilterOptions(...(args as [])),
    fetchKefuOptions: (...args: unknown[]) => fetchKefuOptions(...(args as [])),
    fetchOrder: (...args: unknown[]) => fetchOrder(...(args as [])),
    fetchOrderAudit: (...args: unknown[]) => fetchOrderAudit(...(args as [])),
    fetchPenaltyChildren: (...args: unknown[]) => fetchPenaltyChildren(...args),
    fetchSplitMembers: (...args: unknown[]) => fetchSplitMembers(...(args as [])),
    splitPeerOrder: (...args: unknown[]) => splitPeerOrder(...(args as [])),
    updateOrder: (...args: unknown[]) => updateOrder(...(args as [])),
    updateOrderWithMessage: (...args: unknown[]) => updateOrderWithMessage(...(args as [])),
    fetchAssigneeOptions: (...args: unknown[]) => fetchAssigneeOptions(...args),
    batchAssignOrders: (...args: unknown[]) => batchAssignOrders(...args),
    batchOrderAction: (...args: unknown[]) => batchOrderAction(...(args as [])),
    clearTaker: (...args: unknown[]) => clearTaker(...(args as [])),
    startXgjReceiptSync: (...args: unknown[]) => startXgjReceiptSync(...(args as [])),
  }
})

vi.mock('@/api/orderAssigneeFavorites', () => ({
  fetchFavoriteAssignees: () => fetchFavoriteAssignees(),
  setFavoriteAssignee: (...args: unknown[]) => setFavoriteAssignee(...args),
}))

// 列表与探针替身：挂载必发一发列表（回空页）+ 探针（fail-closed ⇒ 不可用）。
vi.mock('@/v2/orderManageApi', () => ({
  fetchOrdersWithSummary: vi.fn(async () => ({
    items: [],
    total: 0,
    page: 1,
    page_size: 50,
    summary: null,
  })),
  fetchPushAvailability: vi.fn(async () => false),
}))

// 字典替身（页面自己有「取不到不拦页」口径，替身只为确定性）。
vi.mock('@/api/orderDictionaries', () => ({
  fetchOrderDictionaries: vi.fn(async () => ({ games: [], servers: [], order_sources: [] })),
}))
vi.mock('@/api/config', () => ({
  fetchAllGames: vi.fn(async () => []),
  fetchServers: vi.fn(async () => []),
}))
vi.mock('@/api/orderSources', () => ({
  fetchOrderSources: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 200 })),
  fetchOrderSourceGroups: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 200 })),
}))

import V2OrderManage from '../V2OrderManage.vue'
import V2PenaltyDialog from '../V2PenaltyDialog.vue'
import { ElSelect } from 'element-plus'
import { fetchOrdersWithSummary, fetchPushAvailability } from '../orderManageApi'
import { useSessionStore } from '@/stores/session'
import { assignPeerTaker, fetchPeerDirectAssignment, searchPeerTakers } from '@/api/peerDirectAssignment'
import { assignJidanpuTaker, fetchJidanpuAssignmentStatus, fetchJidanpuDirectAssignment, searchJidanpuTakers, verifyJidanpuAssignment } from '@/api/jidanpuDirectAssignment'
import { BizError, NetworkError } from '@/api/errors'

vi.mock('@/api/peerDirectAssignment', () => ({
  assignPeerTaker: vi.fn(),
  fetchPeerDirectAssignment: vi.fn(),
  searchPeerTakers: vi.fn(),
}))

vi.mock('@/api/jidanpuDirectAssignment', () => ({
  assignJidanpuTaker: vi.fn(),
  fetchJidanpuAssignmentStatus: vi.fn(),
  fetchJidanpuDirectAssignment: vi.fn(),
  searchJidanpuTakers: vi.fn(),
  verifyJidanpuAssignment: vi.fn(),
}))

/** 合成候选（名字与 id 不同形 ⇒ 取错列表/取错键当场红）。 */
const TAKERS = [
  { id: 11, name: '测试打手甲' },
  { id: 12, name: '测试打手乙' },
]
const PUBLISHERS = [{ id: 21, name: '测试发单人甲' }]

function makeRouter(): Router {
  const Blank = { template: '<div />' }
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/v2', name: 'v2', component: Blank, children: [{ path: '', name: 'v2-home', component: Blank }, { path: 'order-manage', name: 'v2-order-manage', component: Blank }] },
    ],
  })
}

async function mountPage(initialUrl = '/v2', currentUserId?: number, stubTransition = true, stubPenalty = false): Promise<{ wrapper: VueWrapper; router: Router }> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const router = makeRouter()
  if (currentUserId !== undefined) {
    const session = useSessionStore()
    session.user = { id: currentUserId, username: 'service-test', display_name: '客服测试', identity_plane: 'tenant', is_tenant_admin: false, is_account_admin: false, roles: [], permissions: [], menu_permissions: ['order.list'], game_ids: [] }
  }
  await router.push(initialUrl)
  await router.isReady()
  const wrapper = mount(V2OrderManage, { attachTo: document.body, global: { plugins: [router, pinia], stubs: { transition: stubTransition, V2PenaltyDialog: stubPenalty } } })
  await flushPromises()
  return { wrapper, router }
}

/** 打开某个拾人面板（taker / publisher / kefu）。 */
async function openPick(wrapper: VueWrapper, slot: 'taker' | 'publisher' | 'kefu'): Promise<void> {
  await wrapper.find(`[data-v2-filter="${slot}"]`).trigger('click')
  await flushPromises()
}

/** 在面板搜索框输入关键词（走 el-input 的真 v-model 链；EP 的透传 attr 落在**内层 input** 本身）。 */
async function typeQuery(wrapper: VueWrapper, slot: 'taker' | 'publisher', text: string): Promise<void> {
  await wrapper.find(`[data-v2-pop-search="${slot}"]`).setValue(text)
}

/** 最近一次列表取数的第一参。 */
function lastListParams(): Record<string, unknown> {
  const calls = vi.mocked(fetchOrdersWithSummary).mock.calls
  expect(calls.length, '应已发生过列表取数').toBeGreaterThan(0)
  return calls[calls.length - 1]![0] as Record<string, unknown>
}

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  vi.mocked(fetchPeerDirectAssignment).mockReset().mockResolvedValue({ studios: [], assignee: null, pending: null, price: null })
  vi.mocked(searchPeerTakers).mockReset()
  vi.mocked(assignPeerTaker).mockReset()
  vi.mocked(fetchJidanpuDirectAssignment).mockReset().mockResolvedValue({ bound: false, current: null, assignable: false, reason: '', fee: null, pending: null })
  vi.mocked(fetchJidanpuAssignmentStatus).mockReset().mockResolvedValue(null)
  vi.mocked(searchJidanpuTakers).mockReset()
  vi.mocked(assignJidanpuTaker).mockReset()
  vi.mocked(verifyJidanpuAssignment).mockReset()
  fetchFilterOptions.mockReset()
  fetchFavoriteAssignees.mockReset().mockResolvedValue([])
  setFavoriteAssignee.mockReset()
  fetchAssigneeOptions.mockReset()
  fetchKefuOptions.mockReset()
  fetchKefuOptions.mockResolvedValue({ items: [], truncated: false })
  vi.mocked(fetchPushAvailability).mockReset().mockResolvedValue(false)
  vi.mocked(fetchOrdersWithSummary).mockReset()
  vi.mocked(fetchOrdersWithSummary).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50, summary: null })
  fetchOrder.mockReset()
  fetchOrderAudit.mockReset()
  fetchPenaltyChildren.mockReset()
  fetchSplitMembers.mockReset()
  splitPeerOrder.mockReset()
  batchOrderAction.mockReset()
  batchOrderAction.mockImplementation(async (_action, { order_nos, dry_run }) => ({
    dry_run, eligible: order_nos, ineligible: [], applied: dry_run ? [] : order_nos, effects: [],
  }))
  updateOrder.mockReset()
  clearTaker.mockReset()
  clearTaker.mockResolvedValue(undefined)
  startXgjReceiptSync.mockReset()
  startXgjReceiptSync.mockResolvedValue(undefined)
  updateOrderWithMessage.mockReset()
  updateOrderWithMessage.mockResolvedValue({ order: {}, message: '' })
})


describe('管理订单日期与时间筛选', () => {
  it('保留全天的默认区间，并按浏览器时区发送用户选定的时分秒', async () => {
    const { wrapper } = await mountPage()
    const initial = lastListParams()
    expect(initial.date_from).toMatch(/T00:00:00[+-]\d{2}:\d{2}$/)
    expect(initial.date_to).toMatch(/T23:59:59[+-]\d{2}:\d{2}$/)
    const [from, to] = wrapper.findAllComponents(ElDatePicker)
    expect(from!.props('type')).toBe('datetime')
    expect(to!.props('type')).toBe('datetime')

    const fromInput = wrapper.get('[data-v2-filter="sdate"] input')
    const toInput = wrapper.get('[data-v2-filter="edate"] input')
    await fromInput.setValue('2026-11-12 08:09:10')
    await toInput.setValue('2026-11-12 15:16:17')
    await wrapper.get('[data-v2="search"]').trigger('click')
    await flushPromises()
    const chosen = lastListParams()
    expect(chosen.date_from).toMatch(/^2026-11-12T08:09:10[+-]\d{2}:\d{2}$/)
    expect(chosen.date_to).toMatch(/^2026-11-12T15:16:17[+-]\d{2}:\d{2}$/)
    expect(Date.parse(chosen.date_from as string)).toBe(new Date(2026, 10, 12, 8, 9, 10).getTime())
    expect(Date.parse(chosen.date_to as string)).toBe(new Date(2026, 10, 12, 15, 16, 17).getTime())
    wrapper.unmount()
  })

  it('清空只移除对应边界，重置恢复首尾默认时间', async () => {
    const { wrapper } = await mountPage()
    const initial = lastListParams()
    const [from] = wrapper.findAllComponents(ElDatePicker)
    from!.vm.$emit('update:modelValue', null)
    await wrapper.get('[data-v2-filter="edate"] input').setValue('2026-11-12')
    await wrapper.get('[data-v2="search"]').trigger('click')
    await flushPromises()
    expect(lastListParams()).not.toHaveProperty('date_from')
    expect(lastListParams().date_to).toMatch(/^2026-11-12T23:59:59[+-]\d{2}:\d{2}$/)
    await wrapper.get('[data-v2="reset"]').trigger('click')
    await flushPromises()
    expect(lastListParams()).toMatchObject({ date_from: initial.date_from, date_to: initial.date_to })
    wrapper.unmount()
  })
})

describe('V2OrderManage：新老客标签与筛选', () => {
  it('「收款状态」后的三档筛选默认全部，选新/老发 customer_kind，重置清掉', async () => {
    const { wrapper } = await mountPage()
    expect(lastListParams()).not.toHaveProperty('customer_kind')

    const kind = wrapper
      .findAllComponents(ElSelect)
      .find((c) => c.attributes('data-v2-filter') === 'customer-kind')
    expect(kind, '应存在 data-v2-filter="customer-kind" 的下拉').toBeDefined()
    expect(kind!.props('modelValue')).toBe('')

    kind!.vm.$emit('update:modelValue', 'new')
    await flushPromises()
    await wrapper.get('[data-v2="search"]').trigger('click')
    await flushPromises()
    expect(lastListParams().customer_kind).toBe('new')

    kind!.vm.$emit('update:modelValue', 'old')
    await flushPromises()
    await wrapper.get('[data-v2="search"]').trigger('click')
    await flushPromises()
    expect(lastListParams().customer_kind).toBe('old')

    await wrapper.get('[data-v2="reset"]').trigger('click')
    await flushPromises()
    expect(lastListParams()).not.toHaveProperty('customer_kind')
    wrapper.unmount()
  })

  it('列表「旺旺/来源」最左侧显示新/老标签，详情行也显示；空标签不画', async () => {
    const oldNo = '20261125000000000001'
    const newNo = '20261125000000000002'
    const blankNo = '20261125000000000003'
    const item = (order_no: string, customer_kind: string) => ({
      order_no,
      status: 'COMPLETED',
      source_order_no: '',
      project_text: '新老客标签项目',
      customer_ww: '旺旺甲',
      customer_kind,
      customer_kind_is_manual: false,
      split_id: '',
      split_members: 0,
      created_at: '2026-11-25T10:00:00+08:00',
      completed_at: null,
      total_price: '10.00',
      refund_fee: '0.00',
      net_received: '10.00',
    })
    vi.mocked(fetchOrdersWithSummary).mockResolvedValue({
      items: [item(oldNo, 'old'), item(newNo, 'new'), item(blankNo, '')],
      total: 3,
      page: 1,
      page_size: 50,
      summary: null,
    } as never)
    fetchOrder.mockResolvedValue({ ...item(oldNo, 'old'), content: '', demand: '', game_account: '', game_sub_account: '', has_dailiantong_history: false })
    fetchOrderAudit.mockResolvedValue([])

    const { wrapper } = await mountPage()
    const oldTag = wrapper.get(`[data-v2-kind-tag="${oldNo}"]`)
    expect(oldTag.text()).toBe('老')
    expect(oldTag.classes()).toContain('cv2-kind-tag--old')
    const oldCell = oldTag.element.closest('td')
    expect(oldCell?.firstElementChild).toBe(oldTag.element)

    const newTag = wrapper.get(`[data-v2-kind-tag="${newNo}"]`)
    expect(newTag.text()).toBe('新')
    expect(newTag.classes()).toContain('cv2-kind-tag--new')
    expect(wrapper.find(`[data-v2-kind-tag="${blankNo}"]`).exists()).toBe(false)

    await wrapper.get(`[data-v2-detail="${oldNo}"]`).trigger('click')
    await flushPromises()
    const detailTag = wrapper.get(`[data-v2-detail-kind="${oldNo}"]`)
    expect(detailTag.text()).toBe('老')
    expect(detailTag.classes()).toContain('cv2-kind-tag--old')
    wrapper.unmount()
  })
})

describe('客服首页筛选跳转', () => {
  it('只筛当前销售客服及指定状态，不限制默认三个月', async () => {
    const { wrapper, router } = await mountPage('/v2/order-manage?self_sales=1&status=ISSUE%2CPAUSED', 19)
    expect(lastListParams()).toMatchObject({ sales_user: 19, kefu_type: '0', status: 'ISSUE,PAUSED' })
    expect(lastListParams()).not.toHaveProperty('date_from')
    expect(lastListParams()).not.toHaveProperty('date_to')
    expect(wrapper.find('[data-v2-filter="kefu"]').text()).toContain('客服测试')
    await router.push('/v2/order-manage?self_sales=1&status=PAUSED')
    await flushPromises()
    expect(lastListParams()).toMatchObject({ sales_user: 19, status: 'PAUSED' })
    await router.push('/v2/order-manage')
    await flushPromises()
    expect(lastListParams()).not.toHaveProperty('sales_user')
    expect(lastListParams()).not.toHaveProperty('status')
    expect(lastListParams()).toHaveProperty('date_from')
    wrapper.unmount()
  })

  it('从重点跟进打开本人订单的详情', async () => {
    const orderNo = '20261114000000000002'
    const row = { order_no: orderNo, source_order_no: '', status: 'ISSUE', project_text: '示例订单', created_at: '2026-11-14T10:00:00+08:00', completed_at: null, split_id: '', split_members: 0 }
    vi.mocked(fetchOrdersWithSummary).mockResolvedValueOnce({ items: [row], total: 1, page: 1, page_size: 50, summary: null } as never)
    fetchOrder.mockResolvedValue({ ...row, content: '', demand: '', game_account: '', game_sub_account: '', has_dailiantong_history: true })
    fetchOrderAudit.mockResolvedValue([])
    const { wrapper } = await mountPage(`/v2/order-manage?self_sales=1&keyword=${orderNo}&open_order=${orderNo}`, 19)
    expect(lastListParams()).toMatchObject({ sales_user: 19, kefu_type: '0', keyword: orderNo, order_nos: orderNo })
    expect(fetchOrder).toHaveBeenCalledWith(orderNo)
    expect(wrapper.get('[data-v2="tab-dlt"]').text()).toBe('代练通')
    wrapper.unmount()
  })

  it.each(['成功', '失败'])('切换详情后旧请求%s不会覆盖或结束新详情', async (outcome) => {
    const first = '20261114000000000011'
    const second = '20261114000000000012'
    const row = (order_no: string) => ({ order_no, status: 'ISSUE', source_order_no: '', project_text: '测试项目', split_id: '', created_at: '2026-11-14T10:00:00+08:00', completed_at: null })
    vi.mocked(fetchOrdersWithSummary).mockResolvedValue({ items: [row(first), row(second)], total: 2, page: 1, page_size: 50, summary: null } as never)
    let resolveOld!: (value: unknown) => void
    let rejectOld!: (reason: Error) => void
    let resolveNew!: (value: unknown) => void
    fetchOrder.mockImplementationOnce(() => new Promise((resolve, reject) => { resolveOld = resolve; rejectOld = reject }))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveNew = resolve }))
    fetchOrderAudit.mockResolvedValue([])
    const { wrapper } = await mountPage()
    await wrapper.get(`[data-v2-detail="${first}"]`).trigger('click')
    await wrapper.get(`[data-v2-detail="${second}"]`).trigger('click')
    if (outcome === '成功') resolveOld({ ...row(first), content: '过期内容' })
    else rejectOld(new Error('旧详情失败'))
    await flushPromises()
    expect(wrapper.text()).not.toContain('旧详情失败')
    expect(wrapper.text()).not.toContain('过期内容')
    expect(wrapper.text()).toContain('正在加载日志')
    resolveNew({ ...row(second), content: '最新详情' })
    await flushPromises()
    expect(wrapper.text()).toContain('最新详情')
    wrapper.unmount()
  })

  it('关闭再打开同一订单时不使用之前的详情响应', async () => {
    const orderNo = '20261114000000000013'
    const row = { order_no: orderNo, status: 'ISSUE', source_order_no: '', project_text: '测试项目', split_id: '', created_at: '2026-11-14T10:00:00+08:00', completed_at: null }
    vi.mocked(fetchOrdersWithSummary).mockResolvedValue({ items: [row], total: 1, page: 1, page_size: 50, summary: null } as never)
    let resolveOld!: (value: unknown) => void
    fetchOrder.mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve }))
      .mockResolvedValueOnce({ ...row, content: '重新打开的详情' })
    fetchOrderAudit.mockResolvedValue([])
    const url = `/v2/order-manage?self_sales=1&keyword=${orderNo}&open_order=${orderNo}`
    const { wrapper, router } = await mountPage(url, 19)
    await router.push('/v2/order-manage')
    await flushPromises()
    await router.push(url)
    await flushPromises()
    expect(wrapper.text()).toContain('重新打开的详情')
    resolveOld({ ...row, content: '过期的旧详情' })
    await flushPromises()
    expect(wrapper.text()).toContain('重新打开的详情')
    expect(wrapper.text()).not.toContain('过期的旧详情')
    wrapper.unmount()
  })

  it('客服候选加载失败时仍显示当前登录人', async () => {
    fetchKefuOptions.mockRejectedValue(new Error('候选加载失败'))
    const { wrapper } = await mountPage('/v2/order-manage?self_sales=1', 19)
    expect(lastListParams()).toMatchObject({ sales_user: 19 })
    expect(wrapper.get('[data-v2-filter="kefu"]').text()).toContain('客服测试')
    wrapper.unmount()
  })

  it.each(['成功', '失败'])('较早的列表请求%s返回时不覆盖较新的筛选', async (outcome) => {
    const { wrapper, router } = await mountPage('/v2/order-manage?self_sales=1', 19)
    type Page = Awaited<ReturnType<typeof fetchOrdersWithSummary>>
    let resolveOld!: (value: Page) => void
    let rejectOld!: (reason: Error) => void
    const old = new Promise<Page>((resolve, reject) => { resolveOld = resolve; rejectOld = reject })
    const newer = { items: [{ order_no: '20261114000000000002', status: 'PAUSED', project_text: '较新订单', split_id: '', created_at: '2026-11-14T10:00:00+08:00', completed_at: null }], total: 1, page: 1, page_size: 50, summary: null } as unknown as Page
    const older = { ...newer, items: [{ order_no: '20261114000000000001', status: 'ISSUE', project_text: '较旧订单', split_id: '', created_at: '2026-11-14T10:00:00+08:00', completed_at: null }] } as unknown as Page
    vi.mocked(fetchOrdersWithSummary).mockImplementationOnce(() => old).mockResolvedValueOnce(newer)
    await router.push('/v2/order-manage?self_sales=1&status=ISSUE')
    await flushPromises()
    expect(lastListParams()).toMatchObject({ status: 'ISSUE' })
    await router.push('/v2/order-manage?self_sales=1&status=PAUSED')
    await flushPromises()
    expect(lastListParams()).toMatchObject({ status: 'PAUSED' })
    expect(wrapper.find('[data-v2-row="20261114000000000002"]').exists()).toBe(true)
    if (outcome === '成功') resolveOld(older)
    else rejectOld(new Error('过期请求失败'))
    await flushPromises()
    expect(wrapper.find('[data-v2-row="20261114000000000002"]').exists()).toBe(true)
    expect(wrapper.find('[data-v2-row="20261114000000000001"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('过期请求失败')
    expect(wrapper.get<HTMLButtonElement>('[data-v2="refresh"]').element.disabled).toBe(false)
    wrapper.unmount()
  })

  it('旧筛选的拆单请求失败不会在新筛选弹出错误', async () => {
    const { wrapper, router } = await mountPage('/v2/order-manage?self_sales=1', 19)
    let rejectSplit!: (reason: Error) => void
    fetchSplitMembers.mockReturnValueOnce(new Promise((_resolve, reject) => { rejectSplit = reject }))
    const oldRow = { order_no: '20261114000000000003', status: 'ISSUE', project_text: '旧拆单', split_id: 'split-old', split_members: 2, created_at: '2026-11-14T10:00:00+08:00', completed_at: null }
    vi.mocked(fetchOrdersWithSummary).mockResolvedValueOnce({ items: [oldRow], total: 1, page: 1, page_size: 50, summary: null } as never)
    await router.push('/v2/order-manage?self_sales=1&status=ISSUE')
    await flushPromises()
    expect(fetchSplitMembers).toHaveBeenCalledWith('split-old', expect.objectContaining({ sales_user: 19, status: 'ISSUE' }))
    await router.push('/v2/order-manage?self_sales=1&status=PAUSED')
    await flushPromises()
    rejectSplit(new Error('旧拆单读取失败'))
    await flushPromises()
    expect(wrapper.text()).not.toContain('旧拆单读取失败')
    expect(lastListParams()).toMatchObject({ status: 'PAUSED' })
    wrapper.unmount()
  })

  it('首页筛选的拆单组只显示相同客服与状态的成员', async () => {
    const base = { split_id: 'mixed', split_members: 4, project_text: '测试项目', created_at: '2026-11-14T10:00:00+08:00', completed_at: null }
    const members = [
      { ...base, order_no: '20261114000000000021', sales_user: 19, status: 'ISSUE', split_is_balance_anchor: true },
      { ...base, order_no: '20261114000000000022', sales_user: 19, status: 'ISSUE' },
      { ...base, order_no: '20261114000000000023', sales_user: 20, status: 'ISSUE' },
      { ...base, order_no: '20261114000000000024', sales_user: 19, status: 'PAUSED' },
    ]
    vi.mocked(fetchOrdersWithSummary).mockResolvedValue({ items: [members[0]], total: 2, page: 1, page_size: 20, summary: null } as never)
    fetchSplitMembers.mockImplementation(async (_id, params) => members.filter((item) => item.sales_user === params.sales_user && item.status === params.status))
    const { wrapper } = await mountPage('/v2/order-manage?self_sales=1&status=ISSUE', 19)
    expect(fetchSplitMembers).toHaveBeenCalledWith('mixed', expect.objectContaining({ sales_user: 19, kefu_type: '0', status: 'ISSUE' }))
    expect(wrapper.find('[data-v2-row="20261114000000000021"]').exists()).toBe(true)
    expect(wrapper.find('[data-v2-row="20261114000000000022"]').exists()).toBe(true)
    expect(wrapper.find('[data-v2-row="20261114000000000023"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2-row="20261114000000000024"]').exists()).toBe(false)
    wrapper.unmount()
  })
})

describe('V2OrderManage：罚款子单', () => {
  it('从同一页响应展开拆单组和多层罚款，并保留独立操作行与预览的位置', async () => {
    const original = '20261116000000003111'
    const member = '20261116000000003112'
    const child = '20261116000000003113'
    const leaf = '20261116000000003114'
    const splitId = '20261116000000003199'
    const base = {
      status: 'COMPLETED', created_at: '2026-11-16T10:00:00+08:00', project_text: '合成项目',
    }
    const root = { ...base, order_no: original, split_id: splitId, split_members: 2, split_is_balance_anchor: true }
    const sibling = { ...base, order_no: member, split_id: splitId, penalty_child_count: 1 }
    const penalty = { ...base, order_no: child, is_penalty_child: true, penalty_child_count: 1 }
    const preview = { ...base, order_no: leaf, is_penalty_child: true }
    vi.mocked(fetchOrdersWithSummary).mockResolvedValue({
      items: [root, preview] as never, total: 2, page: 1, page_size: 20, summary: null,
      page_relations: {
        split_members_by_id: { [splitId]: [sibling, root] as never },
        penalty_children_by_parent: { [member]: [penalty] as never, [child]: [preview] as never },
      },
    })
    const { wrapper } = await mountPage('/v2/order-manage', 19)
    expect(lastListParams()).toMatchObject({ group_penalty_children: true, include_page_relations: true })
    expect(wrapper.findAll('[data-v2-row], [data-v2-penalty-preview]').map(row =>
      row.attributes('data-v2-row') ?? row.attributes('data-v2-penalty-preview'),
    )).toEqual([original, member, child, leaf, leaf])
    expect(wrapper.get(`[data-v2-penalty-preview="${leaf}"]`).find('input, button').exists()).toBe(false)
    expect(wrapper.get(`[data-v2-row="${leaf}"]`).find('input[type="checkbox"]').exists()).toBe(true)
    expect(fetchSplitMembers).not.toHaveBeenCalled()
    expect(fetchPenaltyChildren).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('无需点击即在原单下用拆单样式展示子单，保留逐行勾选与编辑', async () => {
    const originalNo = '20261116000000003101'
    const childNo = '20261116000000003102'
    const grandchildNo = '20261116000000003103'
    const order = {
      source_order_no: '', status: 'COMPLETED', game: 1, server: null, project_text: '合成项目',
      created_at: '2026-11-16T10:00:00+08:00', completed_at: null,
    }
    vi.mocked(fetchOrdersWithSummary).mockResolvedValue({
      items: [{ ...order, order_no: originalNo, penalty_child_count: 1 }] as never,
      total: 1, page: 1, page_size: 20, summary: null,
    })
    fetchPenaltyChildren.mockResolvedValueOnce([
      { ...order, order_no: childNo, is_penalty_child: true, penalty_child_count: 1 },
    ]).mockResolvedValueOnce([
      { ...order, order_no: grandchildNo, is_penalty_child: true, penalty_child_count: 0 },
    ])
    const { wrapper } = await mountPage('/v2/order-manage', 19)
    useSessionStore().user!.menu_permissions = ['order.list', 'order.submit_edit']
    const row = (no: string) => wrapper.get(`[data-v2-row="${no}"]`)
    const visibleOrders = () => wrapper.findAll('[data-v2-row]').map((row) => row.attributes('data-v2-row'))
    const checkbox = (no: string) => wrapper.get<HTMLInputElement>(`[data-v2-row-check="${no}"]`)

    expect(lastListParams()).toMatchObject({ group_penalty_children: true })
    expect(visibleOrders()).toEqual([originalNo, childNo, grandchildNo])
    expect(wrapper.find('[data-v2-penalty-expand], .cv2-penalty-label').exists()).toBe(false)
    expect(row(originalNo).get('.cv2-td-order').attributes('data-split-line')).toBe('start')
    expect(row(childNo).get('.cv2-td-order').attributes('data-split-line')).toBe('branch')
    expect(row(grandchildNo).get('.cv2-td-order').attributes('data-split-line')).toBe('end')
    for (const no of [childNo, grandchildNo]) {
      expect(row(no).classes()).toContain('cv2-row--split-child')
      expect(row(no).get('.cv2-td-order').find('.cv2-split-child-connector').exists()).toBe(true)
      expect(row(no).get('.cv2-order-line').attributes('style')).toBeUndefined()
    }
    expect(wrapper.get(`[data-v2-detail="${childNo}"]`).text()).toBe(childNo)
    expect(fetchPenaltyChildren.mock.calls.map(([no]) => no)).toEqual([originalNo, childNo])

    updateOrder.mockResolvedValue({ project_text: '已更新子单项目' })
    const project = (no: string) => wrapper.get(`[data-v2-project-open="${no}"]`)
    await project(childNo).trigger('click')
    await wrapper.get<HTMLTextAreaElement>('textarea[data-v2="order-text-input"]').setValue('已更新子单项目')
    await wrapper.get('[data-v2="order-text-save"]').trigger('click')
    await flushPromises()
    expect(updateOrder).toHaveBeenCalledExactlyOnceWith(childNo, { project_text: '已更新子单项目' })
    expect(project(childNo).text()).toBe('已更新子单项目')
    expect(project(originalNo).text()).toBe('合成项目')

    await wrapper.get<HTMLInputElement>('[data-v2="check-page-all"]').setValue(true)
    expect(checkbox(originalNo).element.checked).toBe(true)
    expect(checkbox(childNo).element.checked).toBe(false)
    await checkbox(childNo).setValue(true)
    await checkbox(grandchildNo).setValue(true)
    expect(wrapper.get('[data-v2="checked-count"]').text()).toBe('已选 3 行')
    expect(fetchPenaltyChildren).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('重叠订单用同样的拆单连线关联，操作和勾选保留在原有位置', async () => {
    const originalNo = '20261116000000003201'
    const childNo = '20261116000000003202'
    const grandchildNo = '20261116000000003203'
    const splitNo = '20261116000000003204'
    const splitMemberNo = '20261116000000003205'
    const order = {
      source_order_no: '', status: 'COMPLETED', game: 1, server: null, project_text: '合成项目',
      created_at: '2026-11-16T10:00:00+08:00', completed_at: null,
    }
    const original = { ...order, order_no: originalNo, penalty_child_count: 2, split_id: 'original-split', split_members: 2, split_is_balance_anchor: true }
    const originalSplitMemberNo = '20261116000000003206'
    const grandchild = { ...order, order_no: grandchildNo, is_penalty_child: true, penalty_parent_order_no: childNo }
    const split = {
      ...order, order_no: splitNo, split_id: 'penalty-split', split_members: 2, split_is_balance_anchor: true,
      is_penalty_child: true, penalty_parent_order_no: originalNo,
    }
    vi.mocked(fetchOrdersWithSummary).mockResolvedValue({
      items: [grandchild, original, split] as never, total: 3, page: 1, page_size: 20, summary: null,
    })
    fetchSplitMembers.mockImplementation(async (id: string) => id === 'original-split'
      ? [original, { ...original, order_no: originalSplitMemberNo, split_is_balance_anchor: false, penalty_child_count: 0 }]
      : [split, { ...split, order_no: splitMemberNo, split_is_balance_anchor: false }])
    fetchPenaltyChildren.mockResolvedValueOnce([
      { ...order, order_no: childNo, is_penalty_child: true, penalty_child_count: 1 }, split,
    ]).mockResolvedValueOnce([grandchild])
    const { wrapper } = await mountPage()
    const row = (no: string) => wrapper.get(`[data-v2-row="${no}"]`)
    const preview = (no: string) => wrapper.get(`[data-v2-penalty-preview="${no}"]`)

    expect(row(grandchildNo).text()).toContain(`原单 ${childNo}`)
    expect(row(childNo).element.previousElementSibling).toBe(row(originalNo).element)
    expect(preview(splitNo).text()).toContain(splitNo)
    expect(preview(splitNo).find('input, button').exists()).toBe(false)
    expect(preview(grandchildNo).element.previousElementSibling).toBe(row(childNo).element)
    expect(preview(grandchildNo).find('input, button').exists()).toBe(false)
    expect(preview(grandchildNo).get('.cv2-td-order').attributes('data-split-line')).toBe('branch')
    expect(preview(splitNo).get('.cv2-td-order').attributes('data-split-line')).toBe('branch')
    expect(preview(splitNo).classes()).toContain('cv2-row--split-child')
    expect(row(originalSplitMemberNo).element.previousElementSibling).toBe(preview(splitNo).element)
    expect(row(originalSplitMemberNo).get('.cv2-td-order').attributes('data-split-line')).toBe('end')
    expect(row(splitMemberNo).element.previousElementSibling).toBe(row(splitNo).element)
    expect(row(splitMemberNo).find('input[type="checkbox"]').exists()).toBe(true)
    await row(grandchildNo).get<HTMLInputElement>('input[type="checkbox"]').setValue(true)
    expect(row(grandchildNo).get<HTMLInputElement>('input[type="checkbox"]').element.checked).toBe(true)
    expect(wrapper.get('[data-v2="checked-count"]').text()).toBe('已选 1 行')
    wrapper.unmount()
  })

  it('隐藏订单号列后单独查到的子单仍在操作列显示原单号', async () => {
    const childNo = '20261116000000003402'
    const originalNo = '20261116000000003401'
    const child = {
      order_no: childNo, status: 'COMPLETED', project_text: '合成项目', completed_at: null,
      penalty_parent_order_no: originalNo, created_at: '2026-11-16T10:00:00+08:00',
    }
    localStorage.setItem('tt.v2.order-manage.hidden-cols.v3', JSON.stringify(['order']))
    vi.mocked(fetchOrdersWithSummary).mockResolvedValue({
      items: [child] as never, total: 1, page: 1, page_size: 20, summary: null,
    })

    const { wrapper } = await mountPage()
    const row = wrapper.get(`[data-v2-row="${childNo}"]`)
    expect(row.find('.cv2-td-order').exists()).toBe(false)
    expect(row.get('.cv2-td-fixed').text()).toBe(`原单 ${originalNo}`)
    wrapper.unmount()
  })

  it('等子单时仍可勾选和刷新，换筛选后丢弃旧结果', async () => {
    const order = { status: 'COMPLETED', project_text: '合成项目', created_at: '2026-11-16T10:00:00+08:00', completed_at: null }
    const oldParents = Array.from({ length: 6 }, (_, i) => ({
      ...order, order_no: `2026111600000000330${i}`, penalty_child_count: 1,
    }))
    const unrelated = { ...order, order_no: '20261116000000003309' }
    const oldChild = '20261116000000003310'
    const current = { ...order, order_no: '20261116000000003311', penalty_child_count: 1 }
    const currentChild = '20261116000000003312'
    const page = { total: 1, page: 1, page_size: 20, summary: null }
    vi.mocked(fetchOrdersWithSummary)
      .mockResolvedValueOnce({ ...page, items: [...oldParents, unrelated] as never, total: oldParents.length + 1 })
      .mockResolvedValue({ ...page, items: [current] as never })
    let resolveOld!: (value: unknown[]) => void
    const pendingOld = new Promise<unknown[]>((resolve) => { resolveOld = resolve })
    let currentAttempts = 0
    fetchPenaltyChildren.mockImplementation((no: string) => {
      if (no !== current.order_no) return pendingOld
      if (++currentAttempts === 1) return Promise.reject(new Error('读取失败'))
      return Promise.resolve([{ ...order, order_no: currentChild }])
    })

    const { wrapper, router } = await mountPage('/v2/order-manage', 19)
    expect(fetchPenaltyChildren.mock.calls.map(([no]) => no)).toEqual(oldParents.slice(0, 5).map((row) => row.order_no))
    const checkbox = wrapper.get<HTMLInputElement>(`[data-v2-row-check="${unrelated.order_no}"]`)
    expect(checkbox.element.disabled).toBe(false)
    expect(wrapper.get<HTMLInputElement>('[data-v2="check-page-all"]').element.disabled).toBe(false)
    expect(wrapper.get<HTMLButtonElement>('[data-v2="refresh"]').element.disabled).toBe(false)
    await checkbox.setValue(true)
    expect(wrapper.get('[data-v2="checked-count"]').text()).toBe('已选 1 行')

    await router.push('/v2/order-manage?self_sales=1&status=COMPLETED')
    await flushPromises()
    expect(fetchPenaltyChildren).toHaveBeenLastCalledWith(current.order_no, { sales_user: 19, status: 'COMPLETED', kefu_type: '0', show_price_type: '0' })
    expect(wrapper.text()).toContain('部分子单加载失败，请刷新重试。')

    resolveOld([{ ...order, order_no: oldChild, penalty_child_count: 1 }])
    await flushPromises()
    expect(fetchPenaltyChildren).toHaveBeenCalledTimes(6)
    expect(wrapper.find(`[data-v2-row="${oldChild}"]`).exists()).toBe(false)

    await wrapper.get('[data-v2="refresh"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain('部分子单加载失败，请刷新重试。')
    expect(wrapper.get(`[data-v2-row="${currentChild}"]`).element.previousElementSibling)
      .toBe(wrapper.get(`[data-v2-row="${current.order_no}"]`).element)
    wrapper.unmount()
  })
})

describe('V2OrderManage：详情继续拆分（真实点击与保存载荷）', () => {
  const splitOrderRow = {
    order_no: '20261112000000000001',
    source_order_no: '',
    source: '微信',
    taker_name: '',
    game: 1,
    server: null,
    project_text: '原项目甲；原项目乙',
    total_price: '80.00',
    refund_fee: '0.00',
    customer_phone: '',
    is_urgent: false,
    created_at: '2026-11-12T10:00:00+08:00',
    game_role_name: '',
    customer_ww: '',
    hall_price: null,
    sales_agent: '',
    created_by: '',
    remark: '',
    net_received: '80.00',
    hall_publisher_name: '',
    completed_at: null,
    time_requirement: '',
    payment_status: 'UNSETTLED',
    payment_method: '',
    split_id: '',
    split_kind: '',
    split_members: 0,
    taker_settled: false,
  }

  it.each(['IN_PROGRESS', 'COMPLETED'])('订单 %s 打开详情并提交拆分金额', async (status) => {
    const row = { ...splitOrderRow, status }
    const orderNo = row.order_no
    vi.mocked(fetchOrdersWithSummary).mockResolvedValue({
      items: [row] as never,
      total: 1,
      page: 1,
      page_size: 50,
      summary: null,
    })
    const order = { ...row, can_continue_split: status === 'COMPLETED', content: '', demand: '', game_account: '', game_sub_account: '' }
    fetchOrder.mockResolvedValue(order)
    fetchOrderAudit.mockResolvedValue([])
    splitPeerOrder.mockResolvedValue({ split_id: 'peer-1', source_total_price: '60.00', created: [] })
    const { wrapper } = await mountPage()
    const session = useSessionStore()
    session.user = { menu_permissions: ['order.add_edit', 'order.submit_payout_price'] } as never
    expect(wrapper.find(`[data-v2-detail="${orderNo}"]`).exists()).toBe(true)
    expect(wrapper.find(`[data-v2-detail="${orderNo}"]`).element.previousElementSibling?.classList.contains('cv2-split-slot')).toBe(true)
    await wrapper.find(`[data-v2-detail="${orderNo}"]`).trigger('click')
    await flushPromises()
    expect(fetchOrder).toHaveBeenCalledWith(orderNo)
    expect(wrapper.find('[data-v2="continue-split"]').exists()).toBe(true)
    await wrapper.find('[data-v2="continue-split"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="dlg-split"]').exists()).toBe(true)
    const remaining = wrapper.find<HTMLTextAreaElement>('textarea[data-v2="split-remaining"]')
    expect(wrapper.findAll('.cv2-split-new-row')).toHaveLength(0)
    await wrapper.find('[data-v2="split-add-row"]').trigger('click')
    const extracted = wrapper.find<HTMLTextAreaElement>('textarea[data-v2="split-project-0"]')
    const price = wrapper.find<HTMLInputElement>('input[data-v2="split-price-0"]')
    expect(remaining.exists()).toBe(true)
    expect(extracted.exists()).toBe(true)
    expect(price.exists()).toBe(true)
    const textareas = wrapper.findAllComponents(ElInput)
      .filter(input => input.props('type') === 'textarea')
    expect(textareas.map(input => input.props('autosize'))).toEqual([
      { minRows: 1, maxRows: 6 },
      { minRows: 1, maxRows: 6 },
    ])
    await remaining.setValue('原项目甲')
    await extracted.setValue('原项目乙')
    await price.setValue('20.00')
    await wrapper.find('[data-v2="split-save"]').trigger('click')
    await flushPromises()
    expect(splitPeerOrder).toHaveBeenCalledWith(orderNo, {
      remaining_project_text: '原项目甲',
      new_orders: [{ project_text: '原项目乙', total_price: '20.00' }],
    })
  })

  it.each([
    { eligibility: false, canEdit: true },
    { eligibility: undefined, canEdit: true },
    { eligibility: true, canEdit: false },
  ])('已完成订单缺少资格或权限时隐藏拆分入口（%j）', async ({ eligibility, canEdit }) => {
    const orderNo = '20261112000000000002'
    const row = { ...splitOrderRow, order_no: orderNo, status: 'COMPLETED' }
    vi.mocked(fetchOrdersWithSummary).mockResolvedValue({ items: [row] as never, total: 1, page: 1, page_size: 50, summary: null })
    fetchOrder.mockResolvedValue({ ...row, can_continue_split: eligibility })
    fetchOrderAudit.mockResolvedValue([])
    const { wrapper } = await mountPage()
    useSessionStore().user = { menu_permissions: canEdit ? ['order.view_detail', 'order.add_edit'] : ['order.view_detail'] } as never
    await wrapper.get(`[data-v2-detail="${orderNo}"]`).trigger('click')
    await flushPromises()
    expect(fetchOrder).toHaveBeenCalledWith(orderNo)
    expect(wrapper.find('[data-v2="continue-split"]').exists()).toBe(false)
  })

  it('LEGACY 组使用统一金额表单和金额拆分接口', async () => {
    const orderNo = '20261112000000000021'
    const row = {
      order_no: orderNo, source_order_no: '', source: '微信', taker_name: '', status: 'IN_PROGRESS',
      game: 1, server: null, project_text: '旧组来源项目', total_price: '80.00', refund_fee: '0.00',
      customer_phone: '', is_urgent: false, created_at: '2026-11-12T10:00:00+08:00', game_role_name: '',
      customer_ww: '', hall_price: null, sales_agent: '', created_by: '', remark: '', net_received: '80.00',
      hall_publisher_name: '', completed_at: null, time_requirement: '', payment_status: 'UNSETTLED',
      payment_method: '', split_id: 'legacy-group-v2', split_kind: 'LEGACY', split_members: 2, taker_settled: false,
    }
    vi.mocked(fetchOrdersWithSummary).mockResolvedValue({
      items: [row] as never, total: 1, page: 1, page_size: 50, summary: null,
    })
    fetchOrder.mockResolvedValue({ ...row, content: '', demand: '', game_account: '', game_sub_account: '' } as never)
    fetchOrderAudit.mockResolvedValue([])
    fetchSplitMembers.mockResolvedValue([
      { ...row, split_is_balance_anchor: true },
      { ...row, order_no: '20261112000000000022', project_text: '已存在的旧子单', total_price: '20.00' },
      { ...row, order_no: '20261112000000000023', project_text: '未修改的已有订单', total_price: '15.00' },
    ] as never)
    splitPeerOrder.mockResolvedValue({ split_id: 'legacy-group-v2', source_total_price: '60.00', created: [] })
    const { wrapper } = await mountPage()
    useSessionStore().user = { menu_permissions: ['order.view_detail', 'order.add_edit', 'order.submit_payout_price'] } as never
    await wrapper.find(`[data-v2-detail="${orderNo}"]`).trigger('click')
    await flushPromises()
    await wrapper.find('[data-v2="continue-split"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-v2="dlg-split"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('组总价')
    expect(wrapper.find('[data-v2="split-source-price"]').exists()).toBe(false)
    expect((wrapper.find<HTMLTextAreaElement>('[data-v2="split-existing-20261112000000000022"]').element).value).toBe('已存在的旧子单')
    expect(wrapper.find('[data-v2="legacy-split-projects"]').exists()).toBe(false)
    expect(wrapper.find(`[data-v2="split-existing-20261112000000000022"]`).exists()).toBe(true)
    expect(wrapper.findAll('.cv2-split-new-row')).toHaveLength(0)
    await wrapper.find<HTMLTextAreaElement>('[data-v2="split-existing-20261112000000000022"]').setValue('修订旧组项目')
    await wrapper.find<HTMLInputElement>('[data-v2="split-existing-price-20261112000000000022"]').setValue('25.00')
    await wrapper.find('[data-v2="split-save"]').trigger('click')
    await flushPromises()
    expect(splitPeerOrder).toHaveBeenCalledWith(orderNo, {
      remaining_project_text: '旧组来源项目',
      new_orders: [],
      existing_orders: [{ order_no: '20261112000000000022', project_text: '修订旧组项目', total_price: '25.00' }],
    })
  })

  it('已拆组只修改顶部发起单内容时允许保存', async () => {
    const orderNo = '20261112000000000031'
    const row = {
      order_no: orderNo, source_order_no: '', source: '微信', taker_name: '', status: 'IN_PROGRESS',
      game: 1, server: null, project_text: '原发起单内容', total_price: '80.00', refund_fee: '0.00',
      customer_phone: '', is_urgent: false, created_at: '2026-11-12T10:00:00+08:00', game_role_name: '',
      customer_ww: '', hall_price: null, sales_agent: '', created_by: '', remark: '', net_received: '80.00',
      hall_publisher_name: '', completed_at: null, time_requirement: '', payment_status: 'UNSETTLED',
      payment_method: '', split_id: 'peer-source-only-v2', split_kind: 'PEER', split_members: 2, taker_settled: false,
    }
    vi.mocked(fetchOrdersWithSummary).mockResolvedValue({
      items: [row] as never, total: 1, page: 1, page_size: 50, summary: null,
    })
    fetchOrder.mockResolvedValue({ ...row, content: '', demand: '', game_account: '', game_sub_account: '' } as never)
    fetchOrderAudit.mockResolvedValue([])
    fetchSplitMembers.mockResolvedValue([
      row,
      { ...row, order_no: '20261112000000000032', project_text: '其他成员原内容', total_price: '20.00' },
    ] as never)
    splitPeerOrder.mockResolvedValue({ split_id: 'peer-source-only-v2', source_total_price: '80.00', created: [] })
    const { wrapper } = await mountPage()
    useSessionStore().user = { menu_permissions: ['order.view_detail', 'order.add_edit'] } as never
    await wrapper.find(`[data-v2-detail="${orderNo}"]`).trigger('click')
    await flushPromises()
    await wrapper.find('[data-v2="continue-split"]').trigger('click')
    await flushPromises()
    await wrapper.find<HTMLTextAreaElement>('[data-v2="split-remaining"]').setValue('仅修改发起单')
    await wrapper.find('[data-v2="split-save"]').trigger('click')
    await flushPromises()
    expect(splitPeerOrder).toHaveBeenCalledWith(orderNo, {
      remaining_project_text: '仅修改发起单',
      new_orders: [],
    })
  })

  it('从子单拆分仍先显示锚点，新增价从发起子单扣并守恒预览', async () => {
    const anchorNo = '20261112000000000041'
    const childNo = '20261112000000000042'
    const groupId = 'peer-child-source'
    const base = {
      source_order_no: '', source: '微信', taker_name: '', status: 'IN_PROGRESS', game: 1,
      server: null, refund_fee: '0.00', customer_phone: '', is_urgent: false,
      created_at: '2026-11-12T10:00:00+08:00', game_role_name: '', customer_ww: '', hall_price: null,
      sales_agent: '', created_by: '', remark: '', net_received: '45.00', hall_publisher_name: '',
      completed_at: null, time_requirement: '', payment_status: 'UNSETTLED', payment_method: '',
      split_id: groupId, split_kind: 'PEER', split_members: 2, taker_settled: false,
    }
    const anchor = { ...base, order_no: anchorNo, project_text: '主单项目', total_price: '35.00', split_is_balance_anchor: true }
    const child = { ...base, order_no: childNo, project_text: '子单项目', total_price: '10.00', split_is_balance_anchor: false }
    vi.mocked(fetchOrdersWithSummary).mockResolvedValue({ items: [child] as never, total: 1, page: 1, page_size: 50, summary: null })
    fetchSplitMembers.mockResolvedValue([child, anchor] as never)
    fetchOrder.mockResolvedValue({ ...child, content: '', demand: '', game_account: '', game_sub_account: '' } as never)
    fetchOrderAudit.mockResolvedValue([])
    const { wrapper } = await mountPage()
    useSessionStore().user = { menu_permissions: ['order.view_detail', 'order.add_edit', 'order.submit_payout_price'] } as never
    await wrapper.find(`[data-v2-detail="${childNo}"]`).trigger('click')
    await flushPromises()
    await wrapper.find('[data-v2="continue-split"]').trigger('click')
    await flushPromises()
    const mainProject = wrapper.findAll('.cv2-split-member')[0]!
      .get<HTMLTextAreaElement>(`textarea[data-v2="split-existing-${anchorNo}"]`)
    const existingInputs = wrapper.findAllComponents(ElInput)
      .filter(input => input.props('type') === 'textarea')
    expect(existingInputs.map(input => input.props('autosize'))).toEqual([
      { minRows: 1, maxRows: 6 },
      { minRows: 1, maxRows: 6 },
    ])
    expect(mainProject.element.value).toBe('主单项目')
    await mainProject.setValue('主单项目已改')
    await wrapper.find<HTMLInputElement>('[data-v2="split-source-price"]').setValue('12.00')
    await wrapper.find('[data-v2="split-add-row"]').trigger('click')
    await wrapper.find<HTMLInputElement>('[data-v2="split-price-0"]').setValue('5.00')
    await wrapper.find<HTMLTextAreaElement>('[data-v2="split-project-0"]').setValue('新增项目')
    expect(wrapper.text()).toContain('原单预计 33.00')
    expect(wrapper.text()).toContain('发起单预计 7.00')
    await wrapper.find('[data-v2="split-save"]').trigger('click')
    await flushPromises()
    expect(splitPeerOrder).toHaveBeenCalledWith(childNo, {
      remaining_project_text: '子单项目',
      source_total_price: '12.00',
      new_orders: [{ project_text: '新增项目', total_price: '5.00' }],
      existing_orders: [{ order_no: anchorNo, project_text: '主单项目已改' }],
    })
  })

  it('展开拆单后保存代练内容只更新所点子单', async () => {
    const group = {
      split_id: 'peer-inline-text', split_kind: 'PEER', split_members: 2, status: 'IN_PROGRESS',
      remark: '保留备注', created_at: '2026-11-12T10:00:00+08:00', completed_at: null,
    }
    const anchor = { ...group, order_no: '20261112000000000051', project_text: '主单项目', split_is_balance_anchor: true }
    const child = { ...group, order_no: '20261112000000000052', project_text: '子单项目', split_is_balance_anchor: false }
    vi.mocked(fetchOrdersWithSummary).mockResolvedValue({ items: [anchor] as never, total: 1, page: 1, page_size: 50, summary: null })
    fetchSplitMembers.mockResolvedValue([anchor, child])
    updateOrder.mockResolvedValue({ ...child, project_text: '已更新子单项目', remark: '不要覆盖列表备注' })
    const { wrapper } = await mountPage('/v2/order-manage', 19)
    useSessionStore().user!.menu_permissions = ['order.list', 'order.submit_edit']
    const childProject = wrapper.get(`[data-v2-project-open="${child.order_no}"]`)
    await childProject.trigger('click')
    const input = wrapper.get<HTMLTextAreaElement>('textarea[data-v2="order-text-input"]')
    expect(input.element.value).toBe('子单项目')
    await input.setValue('  已更新子单项目  ')
    await wrapper.get('[data-v2="order-text-save"]').trigger('click')
    await flushPromises()
    expect(updateOrder).toHaveBeenCalledExactlyOnceWith(child.order_no, { project_text: '  已更新子单项目  ' })
    expect(childProject.text()).toBe('已更新子单项目')
    expect(wrapper.get(`[data-v2-project-open="${anchor.order_no}"]`).text()).toBe('主单项目')
    expect(wrapper.get(`[data-v2-remark-open="${child.order_no}"]`).text()).toBe('保留备注')
    wrapper.unmount()
  })

  it('展开后子行仍可进详情、编辑、抛单、指派、拉回和勾选删除', async () => {
    const groupId = 'peer-group-actions'
    const source = {
      order_no: '20261112000000000011', source_order_no: '', source: '微信', taker_name: '',
      status: 'IN_PROGRESS', game: 1, server: null, project_text: '源项目', total_price: '60.00',
      refund_fee: '0.00', customer_phone: '', is_urgent: false, created_at: '2026-11-12T10:00:00+08:00',
      game_role_name: '', customer_ww: '', hall_price: '45.00', sales_agent: '', created_by: '', remark: '',
      net_received: '60.00', hall_publisher_name: '', completed_at: null, time_requirement: '',
      payment_status: 'UNSETTLED', payment_method: '', split_id: groupId, split_kind: 'PEER',
      split_members: 3, taker_settled: false, split_is_balance_anchor: true,
    }
    const childA = { ...source, order_no: '20261112000000000012', project_text: '拆出项目甲', total_price: '20.00', split_is_balance_anchor: false }
    const childB = { ...source, order_no: '20261112000000000013', project_text: '拆出项目乙', status: 'WAITING_GRAB' }
    vi.mocked(fetchOrdersWithSummary).mockResolvedValue({
      items: [source] as never, total: 1, page: 1, page_size: 50, summary: null,
    })
    fetchSplitMembers.mockResolvedValue([source, childA, childB] as never)
    fetchOrder.mockImplementation(async (orderNo: string) => ({
      ...(orderNo === childA.order_no ? childA : childB),
      content: '', demand: '', game_account: '', game_sub_account: '',
    }))
    fetchOrderAudit.mockResolvedValue([])
    const { wrapper } = await mountPage()
    useSessionStore().user = {
      menu_permissions: [
        'order.view_detail', 'order.add_edit', 'order.send_to_grab_hall',
        'order.withdraw_from_grab_hall', 'order.assign_taker', 'order.submit_payout_price', 'order.delete',
      ],
    } as never

    expect(wrapper.findAll('[data-v2-row]').map((tr) => tr.attributes('data-v2-row'))).toEqual([
      source.order_no, childA.order_no, childB.order_no,
    ])
    expect(wrapper.find(`[data-v2-row="${source.order_no}"] [data-split-line="start"]`).exists()).toBe(true)
    expect(wrapper.find(`[data-v2-row="${childA.order_no}"] [data-split-line="branch"]`).exists()).toBe(true)
    expect(wrapper.find(`[data-v2-row="${childB.order_no}"] [data-split-line="end"]`).exists()).toBe(true)
    expect(wrapper.find(`[data-v2-row="${source.order_no}"] .cv2-split-slot`).exists()).toBe(true)
    expect(wrapper.find(`[data-v2-row="${childA.order_no}"] .cv2-split-slot`).exists()).toBe(true)
    expect(wrapper.find(`[data-v2-row="${childA.order_no}"] .cv2-split-child-connector`).exists()).toBe(true)
    expect(wrapper.find(`[data-v2-row="${childA.order_no}"] .cv2-split-slot button`).exists()).toBe(false)
    expect(wrapper.find('th .cv2-order-header').text()).toBe('订单号')

    await wrapper.find(`[data-v2-detail="${childA.order_no}"]`).trigger('click')
    await flushPromises()
    expect(fetchOrder).toHaveBeenCalledWith(childA.order_no)

    await wrapper.find(`[data-v2-edit="${childA.order_no}"]`).trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="dlg-split"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2="dlg-edit"]').exists()).toBe(true)
    const editPrice = wrapper.find<HTMLInputElement>('[data-v2-field="total_price"]')
    expect(editPrice.exists()).toBe(true)
    expect(editPrice.element.disabled).toBe(false)
    await editPrice.setValue('25.00')
    await wrapper.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    expect(updateOrderWithMessage).toHaveBeenCalledWith(childA.order_no, expect.objectContaining({ total_price: '25.00' }))

    await wrapper.find(`[data-v2-throw="${childA.order_no}"]`).trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="dlg-throw"]').exists()).toBe(true)

    await wrapper.find(`[data-v2-assign="${childA.order_no}"]`).trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="dlg-assign"]').exists()).toBe(true)

    await wrapper.find(`[data-v2-withdraw="${childB.order_no}"]`).trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="dlg-withdraw"]').exists()).toBe(true)

    const childCheckbox = wrapper.get<HTMLInputElement>(`[data-v2-row-check="${childA.order_no}"]`)
    const pageCheckbox = wrapper.get<HTMLInputElement>('[data-v2="check-page-all"]')
    await childCheckbox.setValue(true)
    await pageCheckbox.setValue(true)
    await pageCheckbox.setValue(false)
    expect(childCheckbox.element.checked).toBe(false)
    await childCheckbox.setValue(true)
    expect(wrapper.get('[data-v2="checked-count"]').text()).toBe('已选 1 行')
    await wrapper.get('[data-v2="delete"]').trigger('click')
    expect(wrapper.get('[data-v2="dlg-delete"]').text()).toContain('将删除选中的 1 笔订单')
    await wrapper.get('[data-v2="confirm-delete"]').trigger('click')
    await flushPromises()
    expect(batchOrderAction).toHaveBeenCalledExactlyOnceWith('delete-v2', {
      order_nos: [childA.order_no], dry_run: true, isDelEmp: 'Y', confirmed_effects: [],
    })
    fetchSplitMembers.mockResolvedValue([source, childB] as never)
    await wrapper.get('[data-v2="confirm-batch-preview"]').trigger('click')
    await flushPromises()
    expect(batchOrderAction).toHaveBeenLastCalledWith('delete-v2', {
      order_nos: [childA.order_no], dry_run: false, isDelEmp: 'Y', confirmed_effects: [],
    })
    expect(wrapper.find(`[data-v2-row="${childA.order_no}"]`).exists()).toBe(false)
    expect(wrapper.find(`[data-v2-row="${source.order_no}"]`).exists()).toBe(true)
    expect(wrapper.find(`[data-v2-row="${childB.order_no}"]`).exists()).toBe(true)
  })
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('管理订单权限与同步收款', () => {
  const orderNo = '20261113000000000001'

  async function mountWithNodes(codes: string[], stubTransition = true, stubPenalty = false, unassigned = false, status = unassigned ? 'UNASSIGNED' : 'IN_PROGRESS') {
    const item = {
      order_no: orderNo, status, split_id: '',
      taker_name: unassigned ? '' : '合成打手', project_text: '测试项目',
      total_price: '30.00', hall_price: '20.00', refund_fee: '0.00', net_received: '30.00', payment_status: 'UNSETTLED',
      created_at: '2026-11-13T10:00:00+08:00', completed_at: null,
    }
    vi.mocked(fetchOrdersWithSummary).mockResolvedValue({
      items: [item] as never, total: 1, page: 1, page_size: 50, summary: null,
    })
    const { wrapper } = await mountPage('/v2/order-manage', 19, stubTransition, stubPenalty)
    useSessionStore().user!.menu_permissions = ['order.list', ...codes]
    await flushPromises()
    return wrapper
  }

  it('打开旧单指派弹窗即可核对下家当前接手人的账号名称', async () => {
    vi.mocked(fetchPeerDirectAssignment).mockResolvedValue({
      studios: [], assignee: { studio_name: '测试下家', account_name: '旧单接手人' }, pending: null, price: null,
    })
    const wrapper = await mountWithNodes(['order.assign_taker'])
    try {
      await wrapper.get(`[data-v2-reassign="${orderNo}"]`).trigger('click')
      await flushPromises()
      expect(wrapper.get('[data-v2="peer-assignee"]').text()).toContain('测试下家 · 旧单接手人')
      expect(wrapper.find('[data-v2="peer-assign-confirm"]').exists()).toBe(false)
    } finally {
      wrapper.unmount()
    }
  })

  it('查询下家接手人期间显示加载，失败后可在弹窗顶部重试', async () => {
    let failLookup!: (error: Error) => void
    const lookup = new Promise<Awaited<ReturnType<typeof fetchJidanpuDirectAssignment>>>((_, reject) => {
      failLookup = reject
    })
    const current = { name: '合成接手人', number: '7586' }
    vi.mocked(fetchJidanpuDirectAssignment).mockReturnValueOnce(lookup)
      .mockResolvedValueOnce({ bound: true, current, assignable: true, reason: '', fee: '23.45', pending: null })
    const wrapper = await mountWithNodes(['order.assign_taker'])
    try {
      await wrapper.get(`[data-v2-reassign="${orderNo}"]`).trigger('click')
      expect(wrapper.get('[data-v2="jidanpu-assignee-loading"]').text()).toBe('正在加载下家接手人…')
      expect(wrapper.find('[data-v2="jidanpu-current-assignee"]').exists()).toBe(false)

      failLookup(new Error('鸡蛋铺暂时读取失败'))
      await flushPromises()
      expect(wrapper.find('[data-v2="jidanpu-assignee-loading"]').exists()).toBe(false)
      expect(wrapper.get('[data-v2="jidanpu-assignee-error"]').text()).toContain('暂时无法加载下家接手人')
      await wrapper.get('[data-v2="jidanpu-assignee-error"] button').trigger('click')
      await flushPromises()
      expect(fetchJidanpuDirectAssignment).toHaveBeenCalledTimes(2)
      expect(fetchJidanpuDirectAssignment).toHaveBeenLastCalledWith(orderNo, undefined, true)
      expect(wrapper.find('[data-v2="jidanpu-assignee-error"]').exists()).toBe(false)
      expect(wrapper.get('[data-v2="jidanpu-current-assignee"]').text()).toContain('合成接手人（编号 7586）')
    } finally {
      wrapper.unmount()
    }
  })

  it('打开指派弹窗直接显示鸡蛋铺编号，搜索选中并回读确认后才更新接手人', async () => {
    const current = { name: '合成旧打手', number: '9010405060708090101' }
    const candidate = { name: '合成新打手', number: '9010405060708090102' }
    const ready = { bound: true, current, assignable: true, reason: '', fee: '23.45', pending: null }
    vi.mocked(fetchJidanpuDirectAssignment).mockResolvedValueOnce(ready)
      .mockResolvedValue({ ...ready, current: candidate, assignable: false, pending: '71' })
    vi.mocked(searchJidanpuTakers).mockRejectedValueOnce(new Error('名单暂时不可用'))
      .mockResolvedValueOnce({ items: [candidate], more: false })
    vi.mocked(assignJidanpuTaker).mockRejectedValueOnce(new Error('鸡蛋铺尚未确认指派'))
    vi.mocked(verifyJidanpuAssignment).mockRejectedValueOnce(new Error('鸡蛋铺当前金额尚未与原指派一致'))
      .mockResolvedValueOnce({ ...ready, current: candidate, fee: '28.30' })
    let wrapper = await mountWithNodes(['order.assign_taker'])
    try {
      await wrapper.get(`[data-v2-reassign="${orderNo}"]`).trigger('click')
      await flushPromises()
      expect(wrapper.get('[data-v2="jidanpu-current-assignee"]').text()).toContain(current.number)
      await wrapper.get('[data-v2="jidanpu-assign-tab"]').trigger('click')
      const confirm = wrapper.get<HTMLButtonElement>('[data-v2="jidanpu-assign-confirm"]')
      expect(confirm.element.disabled).toBe(true)
      await wrapper.get('[data-v2="jidanpu-assign-query"]').setValue(candidate.number)
      await wrapper.get('[data-v2="jidanpu-assign-search"]').trigger('click')
      await flushPromises()
      expect(wrapper.get('[role="alert"]').text()).toContain('名单暂时不可用')
      await wrapper.get('[data-v2="jidanpu-assign-search"]').trigger('click')
      await flushPromises()
      expect(searchJidanpuTakers).toHaveBeenLastCalledWith(orderNo, candidate.number)
      expect(assignJidanpuTaker).not.toHaveBeenCalled()
      await wrapper.get('[data-v2="jidanpu-assign-match"]').trigger('click')
      await confirm.trigger('click')
      expect(assignJidanpuTaker).not.toHaveBeenCalled()
      expect(wrapper.get('[data-v2="dlg-assign"]').text()).toContain('付给该打手 23.45 元')
      await confirm.trigger('click')
      await flushPromises()
      expect(wrapper.get('[role="alert"]').text()).toContain('尚未确认')
      await flushPromises()
      expect(assignJidanpuTaker).toHaveBeenCalledExactlyOnceWith(orderNo, {
        operation_id: expect.any(String), number: candidate.number, name: candidate.name, expected_number: current.number, expected_fee: '23.45',
      })
      expect(fetchJidanpuDirectAssignment).toHaveBeenLastCalledWith(orderNo, undefined, true)
      wrapper.unmount()
      wrapper = await mountWithNodes(['order.assign_taker'])
      await wrapper.get(`[data-v2-reassign="${orderNo}"]`).trigger('click')
      await flushPromises()
      await wrapper.get('[data-v2="jidanpu-assign-tab"]').trigger('click')
      expect(wrapper.get('[data-v2="jidanpu-current-assignee"]').text()).toContain(candidate.number)
      expect(wrapper.find('[data-v2="jidanpu-assign-confirm"]').exists()).toBe(false)
      await wrapper.get('[data-v2="jidanpu-verify"]').trigger('click')
      await flushPromises()
      expect(wrapper.get('[role="alert"]').text()).toContain('金额尚未与原指派一致')
      await wrapper.get('[data-v2="jidanpu-verify"]').trigger('click')
      await flushPromises()
      expect(verifyJidanpuAssignment).toHaveBeenLastCalledWith(orderNo, '71', false)
      expect(wrapper.find('[data-v2="jidanpu-verify"]').exists()).toBe(false)
      vi.mocked(searchJidanpuTakers).mockResolvedValueOnce({ items: [current], more: false })
      await wrapper.get('[data-v2="jidanpu-assign-query"]').setValue(current.number)
      await wrapper.get('[data-v2="jidanpu-assign-search"]').trigger('click')
      await flushPromises()
      await wrapper.get('[data-v2="jidanpu-assign-match"]').trigger('click')
      await wrapper.get('[data-v2="jidanpu-assign-confirm"]').trigger('click')
      expect(wrapper.get('[data-v2="dlg-assign"]').text()).toContain('付给该打手 28.30 元')
    } finally {
      wrapper.unmount()
    }
  })

  it('点击立刻显示指派中，提交响应超时后按原请求编号查询且不会重复提交', async () => {
    const candidate = { name: '合成待接单打手', number: '7586' }
    const studio = { id: 31, name: '测试鸡蛋铺工作室' }
    const available = { bound: true, current: null, assignable: true, reason: '', fee: null, pending: null, studios: [studio], price: '20.00' }
    vi.mocked(fetchJidanpuDirectAssignment).mockResolvedValue(available)
    vi.mocked(searchJidanpuTakers).mockResolvedValue({ items: [candidate], more: false, studio_token: 'test-studio-token', fee: '0.00' })
    let loseResponse!: (error: Error) => void
    vi.mocked(assignJidanpuTaker).mockReturnValue(new Promise((_, reject) => { loseResponse = reject }))
    const wrapper = await mountWithNodes(['order.assign_taker'], true, false, true, 'WAITING_GRAB')
    try {
      await wrapper.get(`[data-v2-assign="${orderNo}"]`).trigger('click')
      await flushPromises()
      await wrapper.get('[data-v2="jidanpu-assign-tab"]').trigger('click')
      await wrapper.get('[data-v2="jidanpu-assign-query"]').setValue(candidate.number)
      await wrapper.get('[data-v2="jidanpu-assign-search"]').trigger('click')
      await flushPromises()
      await wrapper.get('[data-v2="jidanpu-assign-match"]').trigger('click')

      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
      await wrapper.get('[data-v2="jidanpu-assign-confirm"]').trigger('click')
      expect(wrapper.get('#assign-jidanpu-panel').text()).toContain('指派中')
      const operationId = vi.mocked(assignJidanpuTaker).mock.calls[0]![1].operation_id
      vi.mocked(fetchJidanpuAssignmentStatus)
        .mockResolvedValueOnce({ ...available, assignable: false, pending: '71', operation_id: operationId, operation_status: 'working' })
        .mockResolvedValueOnce({ ...available, current: candidate, assignable: false, operation_id: operationId, operation_status: 'done' })
      loseResponse(new NetworkError('请求超时'))
      await flushPromises()
      await vi.advanceTimersByTimeAsync(1200)
      expect(wrapper.get('#assign-jidanpu-panel').text()).toContain('指派中')
      expect(wrapper.find('[data-v2="jidanpu-verify"]').exists()).toBe(false)
      await vi.advanceTimersByTimeAsync(1200)
      expect(fetchJidanpuAssignmentStatus).toHaveBeenNthCalledWith(1, orderNo, operationId)
      expect(fetchJidanpuAssignmentStatus).toHaveBeenNthCalledWith(2, orderNo, operationId)
      expect(assignJidanpuTaker).toHaveBeenCalledTimes(1)
      expect(fetchJidanpuDirectAssignment).toHaveBeenCalledTimes(2)
      expect(wrapper.text()).toContain('已指派给鸡蛋铺打手')
      vi.mocked(fetchJidanpuDirectAssignment).mockResolvedValueOnce({
        bound: true, current: candidate, assignable: true, reason: '', fee: '0.00', pending: null,
      })
      await wrapper.get('[data-v2="jidanpu-assign-again"]').trigger('click')
      await flushPromises()
      expect(fetchJidanpuDirectAssignment).toHaveBeenLastCalledWith(orderNo, undefined, true)
      expect(wrapper.find('[data-v2="jidanpu-assign-query"]').exists()).toBe(true)
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  it('显示鸡蛋铺明确拒绝的原原因，主动重新选择前不刷新原单', async () => {
    const current = { name: '合成原打手', number: '9010405060708090101' }
    const candidate = { name: '合成新打手', number: '9010405060708090102' }
    const ready = { bound: true, current, assignable: true, reason: '', fee: '23.45', pending: null }
    vi.mocked(fetchJidanpuDirectAssignment).mockResolvedValue(ready)
    vi.mocked(searchJidanpuTakers).mockResolvedValue({ items: [candidate], more: false })
    vi.mocked(assignJidanpuTaker).mockImplementation(async (_orderNo, { operation_id }) => ({
      ...ready, assignable: false, operation_id, operation_status: 'working', pending: '71',
    }))
    const reason = '鸡蛋铺拒绝原因：所选打手已停用'
    const wrapper = await mountWithNodes(['order.assign_taker'])
    try {
      await wrapper.get(`[data-v2-reassign="${orderNo}"]`).trigger('click')
      await flushPromises()
      await wrapper.get('[data-v2="jidanpu-assign-tab"]').trigger('click')
      await wrapper.get('[data-v2="jidanpu-assign-query"]').setValue(candidate.number)
      await wrapper.get('[data-v2="jidanpu-assign-search"]').trigger('click')
      await flushPromises()
      await wrapper.get('[data-v2="jidanpu-assign-match"]').trigger('click')
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
      await wrapper.get('[data-v2="jidanpu-assign-confirm"]').trigger('click')
      await wrapper.get('[data-v2="jidanpu-assign-confirm"]').trigger('click')
      await flushPromises()
      const operationId = vi.mocked(assignJidanpuTaker).mock.calls[0]![1].operation_id
      vi.mocked(fetchJidanpuAssignmentStatus).mockResolvedValueOnce({
        ...ready, current: null, assignable: false, reason, operation_id: operationId, operation_status: 'failed',
      })
      await vi.advanceTimersByTimeAsync(1200)
      expect(wrapper.get('#assign-jidanpu-panel [role="alert"]').text()).toContain(reason)
      expect(fetchJidanpuDirectAssignment).toHaveBeenCalledTimes(1)
      expect(assignJidanpuTaker).toHaveBeenCalledTimes(1)
      await wrapper.get('[data-v2="jidanpu-assign-retry"]').trigger('click')
      await flushPromises()
      expect(fetchJidanpuDirectAssignment).toHaveBeenCalledTimes(2)
      expect(wrapper.find('[data-v2="jidanpu-assign-query"]').exists()).toBe(true)
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  it.each([false, true])('重新提交沿用原请求编号，内容冲突时回查原指派（%s）', async (differentTarget) => {
    const current = { name: '合成旧打手', number: '9010405060708090101' }
    const candidate = { name: '合成新打手', number: '9010405060708090102' }
    const available = { bound: true, current, assignable: true, reason: '', fee: '23.45', pending: null }
    vi.mocked(fetchJidanpuDirectAssignment).mockResolvedValue(available)
    vi.mocked(searchJidanpuTakers).mockResolvedValue({ items: [candidate], more: false })
    const submit = vi.mocked(assignJidanpuTaker).mockRejectedValueOnce(new NetworkError('请求超时'))
    if (differentTarget) {
      submit.mockRejectedValueOnce(new BizError(1000, '重新提交的内容与原指派不同，正在核对原指派结果。', {
        operation_conflict: true,
      }))
    } else submit.mockResolvedValueOnce({ ...available, current: candidate })
    const wrapper = await mountWithNodes(['order.assign_taker'])
    try {
      await wrapper.get(`[data-v2-reassign="${orderNo}"]`).trigger('click')
      await flushPromises()
      await wrapper.get('[data-v2="jidanpu-assign-tab"]').trigger('click')
      await wrapper.get('[data-v2="jidanpu-assign-query"]').setValue(candidate.number)
      await wrapper.get('[data-v2="jidanpu-assign-search"]').trigger('click')
      await flushPromises()
      await wrapper.get('[data-v2="jidanpu-assign-match"]').trigger('click')
      const confirm = () => wrapper.get('[data-v2="jidanpu-assign-confirm"]')
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
      await confirm().trigger('click')
      await confirm().trigger('click')
      await flushPromises()
      await vi.advanceTimersByTimeAsync(1200 * 8)
      expect(wrapper.get('[role="alert"]').text()).toContain('可以重新提交')
      expect(assignJidanpuTaker).toHaveBeenCalledTimes(1)
      const originalId = submit.mock.calls[0]![1].operation_id
      if (differentTarget) {
        const another = { name: '合成另外一位打手', number: '9010405060708090103' }
        vi.mocked(searchJidanpuTakers).mockResolvedValueOnce({ items: [another], more: false })
        await wrapper.get('[data-v2="jidanpu-assign-query"]').setValue(another.number)
        await wrapper.get('[data-v2="jidanpu-assign-search"]').trigger('click')
        await flushPromises()
        await wrapper.get('[data-v2="jidanpu-assign-match"]').trigger('click')
        vi.mocked(fetchJidanpuAssignmentStatus).mockResolvedValueOnce({
          ...available, current: candidate, operation_id: originalId, operation_status: 'done', assignable: false,
        })
      }
      await confirm().trigger('click')
      await confirm().trigger('click')
      await flushPromises()
      const submissions = vi.mocked(assignJidanpuTaker).mock.calls
      expect(submissions).toHaveLength(2)
      expect(submissions[1]![1].operation_id).toBe(originalId)
      const alert = wrapper.find('[role="alert"]')
      const conflictShown = alert.exists() && alert.text().includes('原指派不同')
      expect(conflictShown).toBe(differentTarget)
      if (differentTarget) await vi.advanceTimersByTimeAsync(1200)
      expect(fetchJidanpuAssignmentStatus).toHaveBeenCalledTimes(differentTarget ? 9 : 8)
      expect(fetchJidanpuAssignmentStatus).toHaveBeenLastCalledWith(orderNo, originalId)
      expect(wrapper.text()).toContain(`已指派给鸡蛋铺打手 ${candidate.name}`)
      expect(assignJidanpuTaker).toHaveBeenCalledTimes(2)
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  it('待接单可以先选鸡蛋铺打手，再用搜索返回的默认金额指派', async () => {
    const candidate = { name: '合成待接单打手', number: '7586' }
    const studio = { id: 31, name: '测试鸡蛋铺工作室' }
    const available = { bound: true, current: null, assignable: true, reason: '', fee: null, pending: null, studios: [studio], price: '20.00' }
    vi.mocked(fetchJidanpuDirectAssignment).mockResolvedValue(available)
    vi.mocked(searchJidanpuTakers).mockResolvedValue({ items: [candidate], more: false, studio_token: 'test-studio-token', fee: '0.00' })
    vi.mocked(assignJidanpuTaker).mockResolvedValue({ ...available, current: candidate, fee: '0.00', studios: undefined })

    const wrapper = await mountWithNodes(['order.assign_taker'], true, false, true, 'WAITING_GRAB')
    try {
      await wrapper.get(`[data-v2-assign="${orderNo}"]`).trigger('click')
      await flushPromises()
      await wrapper.get('[data-v2="jidanpu-assign-tab"]').trigger('click')
      expect(wrapper.get('#assign-jidanpu-panel').text()).not.toContain('重新核对')
      await wrapper.get('[data-v2="jidanpu-assign-query"]').setValue(candidate.number)
      await wrapper.get('[data-v2="jidanpu-assign-search"]').trigger('click')
      await flushPromises()
      await wrapper.get('[data-v2="jidanpu-assign-match"]').trigger('click')
      expect(wrapper.get('.as-peer-preview div:last-child dd').text()).toBe('0.00 元')
      const submit = wrapper.get<HTMLButtonElement>('[data-v2="jidanpu-assign-confirm"]')
      expect(submit.element.disabled).toBe(false)
      await submit.trigger('click')
      await flushPromises()
      expect(assignJidanpuTaker).toHaveBeenCalledExactlyOnceWith(orderNo, {
        operation_id: expect.any(String), number: candidate.number, name: candidate.name, expected_number: '', expected_fee: '0.00', studio_id: studio.id,
        studio_token: 'test-studio-token', expected_price: '20.00',
      })
    } finally {
      wrapper.unmount()
    }
  })

  it('两种下家各默认第一家，切换鸡蛋铺工作室后只指派该工作室返回的打手', async () => {
    const studios = [{ id: 31, name: '青禾工作室' }, { id: 32, name: '山风工作室' }]
    const available = { bound: true, current: null, assignable: true, reason: '', fee: null, pending: null, studios, price: '20.00' }
    const oldTaker = { name: '旧工作室打手', number: '10001' }
    const finalTaker = { name: '新工作室打手', number: '10002' }
    vi.mocked(fetchPeerDirectAssignment).mockResolvedValue({
      studios: [{ id: 23, name: '团团通第一家' }, { id: 24, name: '团团通第二家' }],
      assignee: null, pending: null, price: '20.00',
    })
    vi.mocked(fetchJidanpuDirectAssignment).mockResolvedValue(available)
    let finishOldSearch: () => void = () => undefined
    vi.mocked(searchJidanpuTakers).mockResolvedValueOnce({ items: [oldTaker], more: false, studio_token: 'a'.repeat(64) })
      .mockImplementationOnce(() => new Promise((resolve) => {
        finishOldSearch = () => resolve({ items: [oldTaker], more: false, studio_token: 'a'.repeat(64) })
      }))
      .mockResolvedValueOnce({ items: [finalTaker], more: false, studio_token: 'b'.repeat(64) })
    vi.mocked(assignJidanpuTaker).mockResolvedValue({ ...available, studios: undefined, current: finalTaker })
    const wrapper = await mountWithNodes(['order.assign_taker', 'order.submit_payout_price'], true, false, true)
    try {
      await wrapper.get(`[data-v2-assign="${orderNo}"]`).trigger('click')
      await flushPromises()
      const peerTab = wrapper.get('[data-v2="peer-assign-tab"]')
      expect(peerTab.text()).toBe('团团通下家')
      await peerTab.trigger('click')
      expect(wrapper.get<HTMLSelectElement>('[data-v2="peer-assign-studio"]').element.value).toBe('23')
      const jidanpuTab = wrapper.get('[data-v2="jidanpu-assign-tab"]')
      expect(jidanpuTab.text()).toBe('鸡蛋铺下家')
      await jidanpuTab.trigger('click')
      const studio = wrapper.get<HTMLSelectElement>('[data-v2="jidanpu-assign-studio"]')
      const search = wrapper.get('[data-v2="jidanpu-assign-search"]')
      const query = wrapper.get('[data-v2="jidanpu-assign-query"]')
      expect(studio.element.value).toBe('31')
      expect(fetchJidanpuDirectAssignment).toHaveBeenLastCalledWith(orderNo, 31)
      await query.setValue('旧人')
      await search.trigger('click')
      await flushPromises()
      await wrapper.get('[data-v2="jidanpu-assign-match"]').trigger('click')
      await wrapper.get('#jidanpu-assignment-price').setValue('25.00')
      await wrapper.get('#jidanpu-assignment-fee').setValue('5.50')
      await query.setValue('另找旧店')
      await search.trigger('click')
      await studio.setValue('32')
      await flushPromises()
      expect(wrapper.find('[data-v2="jidanpu-assign-match"]').exists()).toBe(false)
      expect(wrapper.get<HTMLInputElement>('[data-v2="jidanpu-assign-query"]').element.value).toBe('')
      expect(fetchJidanpuDirectAssignment).toHaveBeenLastCalledWith(orderNo, 32)
      expect(fetchJidanpuDirectAssignment).toHaveBeenCalledTimes(3)
      await wrapper.get('[data-v2="jidanpu-assign-query"]').setValue('新人')
      await wrapper.get('[data-v2="jidanpu-assign-search"]').trigger('click')
      await flushPromises()
      finishOldSearch()
      await flushPromises()
      expect(wrapper.get('[data-v2="jidanpu-assign-match"]').text()).toContain(finalTaker.name)
      await wrapper.get('[data-v2="jidanpu-assign-match"]').trigger('click')
      expect(wrapper.get<HTMLInputElement>('#jidanpu-assignment-price').element.value).toBe('')
      expect(wrapper.get<HTMLInputElement>('#jidanpu-assignment-fee').element.value).toBe('')
      await wrapper.get('#jidanpu-assignment-fee').setValue('8.20')
      const submit = wrapper.get('[data-v2="jidanpu-assign-confirm"]')
      expect(submit.text()).toBe('确认指派')
      await submit.trigger('click')
      await flushPromises()
      expect(assignJidanpuTaker).toHaveBeenCalledExactlyOnceWith(orderNo, {
        operation_id: expect.any(String), number: finalTaker.number, name: finalTaker.name, expected_number: '', expected_fee: null, fee: '8.20',
        studio_id: 32, studio_token: 'b'.repeat(64), expected_price: '20.00',
      })
    } finally {
      wrapper.unmount()
    }
  })

  it('下家按账号片段匹配，选中具体打手后才允许指派', async () => {
    vi.mocked(fetchPeerDirectAssignment).mockResolvedValue({
      studios: [{ id: 23, name: '测试下家' }], assignee: null, pending: null, price: '0.00',
    })
    vi.mocked(searchPeerTakers).mockRejectedValueOnce(new Error('有重名打手，请联系下家核对'))
      .mockResolvedValueOnce({ items: [
        { account_name: 'demo48263', match_token: 'test-worker-proof' },
        { account_name: 'sample48263', match_token: 'other-worker-proof' },
      ] })
    vi.mocked(assignPeerTaker).mockResolvedValue({ studio_name: '测试下家', account_name: 'demo48263' })
    const wrapper = await mountWithNodes(['order.assign_taker', 'order.submit_payout_price'], true, false, true)
    try {
      await wrapper.get(`[data-v2-assign="${orderNo}"]`).trigger('click')
      await wrapper.get('[data-v2="peer-assign-tab"]').trigger('click')
      await wrapper.get('[data-v2="peer-assign-studio"]').setValue('23')
      const account = wrapper.get('[data-v2="peer-assign-account"]')
      const search = wrapper.get('[data-v2="peer-assign-search"]')
      const confirm = wrapper.get<HTMLButtonElement>('[data-v2="peer-assign-confirm"]')
      await account.setValue('同名测试')
      await search.trigger('click')
      await flushPromises()
      expect(wrapper.get('[role="alert"]').text()).toContain('重名')
      expect(confirm.element.disabled).toBe(true)
      await account.setValue('48263')
      await search.trigger('click')
      await flushPromises()
      expect(searchPeerTakers).toHaveBeenLastCalledWith(orderNo, { studio_id: 23, query: '48263' })
      expect(confirm.element.disabled).toBe(true)
      const matches = wrapper.findAll('[data-v2="peer-assign-match"]')
      expect(matches.map((match) => match.text())).toEqual(['demo48263', 'sample48263'])
      await matches[0]!.trigger('click')
      const price = wrapper.get('[data-v2="peer-assign-price"]')
      expect(price.attributes('placeholder')).toBe('0.00 元，留空沿用')
      await price.setValue('100.00')
      await confirm.trigger('click')
      await flushPromises()
      expect(assignPeerTaker).toHaveBeenCalledExactlyOnceWith(orderNo, {
        studio_id: 23, account_name: 'demo48263', expected_price: '0.00', price: '100.00', match_token: 'test-worker-proof',
      })
      expect(wrapper.get('[data-v2="toast"]').text()).toContain('已指派给测试下家 · demo48263')
    } finally {
      wrapper.unmount()
    }
  })

  it('重新打开待确认的订单时只重试原工作室、打手和金额', async () => {
    vi.mocked(fetchPeerDirectAssignment).mockResolvedValue({
      studios: [], assignee: null, price: '80.00',
      pending: { studio_id: 23, studio_name: '测试下家', account_name: '合成打手甲', price: '100.00' },
    })
    vi.mocked(assignPeerTaker).mockRejectedValue(new Error('下家正在确认，请重试'))
    const wrapper = await mountWithNodes(['order.assign_taker'], true, false, true)
    try {
      await wrapper.get(`[data-v2-assign="${orderNo}"]`).trigger('click')
      await flushPromises()
      expect(wrapper.get('[data-v2="peer-assign-tab"]').attributes('aria-selected')).toBe('true')
      expect(wrapper.find('[data-v2="peer-assign-account"]').exists()).toBe(false)
      expect(wrapper.get('[data-v2="peer-assign-preview"]').text()).toContain('100.00 元')
      await wrapper.get('[data-v2="peer-assign-confirm"]').trigger('click')
      await flushPromises()
      expect(assignPeerTaker).toHaveBeenCalledExactlyOnceWith(orderNo, {
        studio_id: 23, account_name: '合成打手甲', expected_price: '100.00',
      })
      expect(wrapper.get('[data-v2="dlg-assign"]').text()).toContain('下家正在确认，请重试')
      expect(wrapper.find('[data-v2="toast"]').exists()).toBe(false)
    } finally {
      wrapper.unmount()
    }
  })

  it('只读账号保留订单值，工具条与行内没有写入口', async () => {
    const wrapper = await mountWithNodes([])
    const tools = wrapper.get('[data-v2="toolbar-strip"]')
    expect(tools.findAll('button').map(button => button.attributes('data-v2'))).toEqual(['refresh'])
    for (const action of ['status', 'price', 'hall-price', 'refund', 'pay-badge']) {
      expect(wrapper.get(`[data-v2-${action}="${orderNo}"]`).element.tagName).toBe('SPAN')
    }
    expect(wrapper.find(`[data-v2-reassign="${orderNo}"]`).exists()).toBe(false)
    expect(wrapper.get(`[data-v2-price="${orderNo}"]`).text()).toBe('30.00')
    wrapper.unmount()
  })

  it('有发起权限时在删除左边显示实底罚款按钮，选中原单后打开共用弹窗', async () => {
    const wrapper = await mountWithNodes(['home.approval_apply', 'order.delete'], true, true)
    const toolbar = wrapper.get('[data-v2="toolbar-strip"]')
    const penalty = toolbar.get('[data-v2="penalty"]')
    expect(penalty.element.nextElementSibling).toBe(toolbar.get('[data-v2="delete"]').element)
    expect(penalty.classes()).toContain('cv2-btn--solid-penalty')
    await penalty.trigger('click')
    expect(wrapper.findComponent(V2PenaltyDialog).exists()).toBe(false)
    await wrapper.get(`[data-v2-row-check="${orderNo}"]`).setValue(true)
    await penalty.trigger('click')
    expect(wrapper.getComponent(V2PenaltyDialog).props('originalOrderNo')).toBe(orderNo)
    wrapper.unmount()
  })

  it('在原单详情显示罚款子单并链接到普通订单详情', async () => {
    const childNo = '20261113000000000002'
    fetchOrder.mockResolvedValue({ penalty_children: [{ order_no: childNo, status: 'COMPLETED' }] })
    fetchOrderAudit.mockResolvedValue([])
    const wrapper = await mountWithNodes([])
    await wrapper.get(`[data-v2-detail="${orderNo}"]`).trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-v2="dlg-detail"]').text()).toContain('罚款子单')
    expect(wrapper.get('[data-v2="dlg-detail"]').get(`a[href="/orders/${childNo}"]`).text()).toBe(childNo)
    wrapper.unmount()
  })

  it('只有交易平台写权限的账号能看到可用的发单入口', async () => {
    vi.mocked(fetchPushAvailability).mockResolvedValue(true)
    const wrapper = await mountWithNodes([])
    const platformPush = `[data-v2-push-jdp="${orderNo}"]`
    expect(wrapper.find(platformPush).exists()).toBe(false)

    useSessionStore().user!.menu_permissions = ['order.list', 'order.platform_write']
    await flushPromises()
    expect(wrapper.find(platformPush).exists()).toBe(true)
    wrapper.unmount()
  })

  it('只读账号可复制代练内容，剪贴板失败仍可手动复制', async () => {
    const wrapper = await mountWithNodes([])
    const writeText = vi.fn().mockRejectedValueOnce(new Error('clipboard denied')).mockResolvedValueOnce(undefined)
    const clipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    try {
      await wrapper.get(`[data-v2-project-open="${orderNo}"]`).trigger('click')
      const input = wrapper.get<HTMLTextAreaElement>('textarea[data-v2="order-text-input"]')
      expect(input.element.readOnly).toBe(true)
      expect(wrapper.find('[data-v2="order-text-save"]').exists()).toBe(false)
      const copy = wrapper.get('[data-v2="order-text-copy"]')
      await copy.trigger('click')
      await flushPromises()
      expect(wrapper.get('[data-v2="toast"]').text()).toBe('复制失败，请手动选中复制')
      expect(input.element.value).toBe('测试项目')
      await copy.trigger('click')
      await flushPromises()
      expect(writeText).toHaveBeenLastCalledWith('测试项目')
      expect(wrapper.get('[data-v2="toast"]').text()).toBe('已复制')
      expect(updateOrder).not.toHaveBeenCalled()
    } finally {
      if (clipboard) Object.defineProperty(navigator, 'clipboard', clipboard)
      else Reflect.deleteProperty(navigator, 'clipboard')
      wrapper.unmount()
    }
  })

  it('重要备注为空时可填写，保存失败保留草稿，重试可保存和清空', async () => {
    const wrapper = await mountWithNodes(['order.add_edit'])
    const remark = wrapper.get(`[data-v2-remark-open="${orderNo}"]`)
    const inputSelector = 'textarea[data-v2="order-text-input"]'
    const saveSelector = '[data-v2="order-text-save"]'
    updateOrder.mockRejectedValueOnce(new Error('保存失败，请重试'))
      .mockResolvedValueOnce({ remark: '新备注' })
      .mockResolvedValueOnce({ remark: '' })
    await remark.trigger('click')
    expect(wrapper.get<HTMLTextAreaElement>(inputSelector).element.value).toBe('')
    await wrapper.get(inputSelector).setValue('  新备注  ')
    await wrapper.get(saveSelector).trigger('click')
    await flushPromises()
    expect(wrapper.get<HTMLTextAreaElement>(inputSelector).element.value).toBe('  新备注  ')
    expect(wrapper.get('[data-v2="toast"]').text()).toBe('保存失败，请重试')
    expect(remark.text()).toBe('无')
    await wrapper.get(saveSelector).trigger('click')
    await flushPromises()
    expect(wrapper.find(inputSelector).exists()).toBe(false)
    expect(remark.text()).toBe('新备注')
    await remark.trigger('click')
    await wrapper.get(inputSelector).setValue('')
    await wrapper.get(saveSelector).trigger('click')
    await flushPromises()
    expect(remark.text()).toBe('无')
    expect(updateOrder.mock.calls).toEqual([
      [orderNo, { remark: '  新备注  ' }],
      [orderNo, { remark: '  新备注  ' }],
      [orderNo, { remark: '' }],
    ])
    wrapper.unmount()
  })

  it('点击弹窗外关闭后可以重新打开指派', async () => {
    const wrapper = await mountWithNodes(['order.assign_taker'], false)
    try {
      const assign = wrapper.get(`[data-v2-reassign="${orderNo}"]`)
      const dialog = '[data-v2="dlg-assign"]'
      await assign.trigger('click')
      expect(wrapper.get(dialog).isVisible()).toBe(true)
      const backdrop = wrapper.get(dialog).element.closest('.el-overlay-dialog')!
      for (const type of ['mousedown', 'mouseup', 'click']) {
        backdrop.dispatchEvent(new MouseEvent(type, { bubbles: true }))
      }
      await vi.waitFor(() => expect(wrapper.find(dialog).exists()).toBe(false))
      await assign.trigger('click')
      expect(wrapper.get(dialog).isVisible()).toBe(true)
    } finally {
      wrapper.unmount()
    }
  })

  it('默认只读常用名单，搜索后添加，重开可选并可移除', async () => {
    const favorite = { id: 11, name: '合成候选', role: '测试职位', tag: '', remark: '' }
    fetchFavoriteAssignees.mockResolvedValueOnce([]).mockResolvedValueOnce([favorite])
    fetchAssigneeOptions.mockResolvedValue({ items: [favorite] })
    setFavoriteAssignee.mockResolvedValueOnce([favorite]).mockResolvedValueOnce([])
    const wrapper = await mountWithNodes(['order.assign_taker'], false)
    const dialog = '[data-v2="dlg-assign"]'
    const row = `[data-v2-reassign="${orderNo}"]`
    try {
      await wrapper.get(row).trigger('click')
      await flushPromises()
      expect(wrapper.get(dialog).text()).toContain('暂无常用打手')
      const picker = wrapper.get('[aria-label="选择打手"]')
      const performance = wrapper.get('[aria-label="订单绩效"]')
      expect(picker.element.parentElement?.lastElementChild).toBe(performance.element)
      expect(performance.text()).toContain('打手绩效')
      expect(performance.text()).toContain('客服提成')
      expect(fetchAssigneeOptions).not.toHaveBeenCalled()

      const input = wrapper.get('input[data-v2="assign-key"]')
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
      try {
        await input.setValue('合成')
        await vi.advanceTimersByTimeAsync(200)
        await input.setValue(' 合成候选 ')
        await vi.advanceTimersByTimeAsync(399)
        expect(fetchAssigneeOptions).not.toHaveBeenCalled()
        await vi.advanceTimersByTimeAsync(1)
      } finally {
        vi.useRealTimers()
      }
      await flushPromises()
      expect(fetchAssigneeOptions).toHaveBeenCalledExactlyOnceWith({ key: '合成候选', limit: 200 })
      await wrapper.get('[data-v2-toggle-favorite="11"]').trigger('click')
      await flushPromises()
      expect(setFavoriteAssignee).toHaveBeenCalledExactlyOnceWith(11, true)
      await wrapper.get('[data-v2="assign-show-favorites"]').trigger('click')
      const favorites = wrapper.get('[data-v2="assign-favorites"]')
      expect(favorites.text()).toContain(favorite.name)
      expect(favorites.classes()).toContain('as-favorite-grid')
      expect(favorites.get('[data-v2-toggle-favorite="11"]').attributes('aria-label')).toBe('移除常用打手 合成候选')
      expect(wrapper.find('[data-v2="assign-picked"]').exists()).toBe(false)

      await wrapper.get(`${dialog} .el-dialog__headerbtn`).trigger('click')
      await vi.waitFor(() => expect(wrapper.find(dialog).exists()).toBe(false))
      await wrapper.get(row).trigger('click')
      await flushPromises()
      expect(fetchFavoriteAssignees).toHaveBeenCalledTimes(2)
      await wrapper.get('[data-v2-assignee="11"]').trigger('click')
      expect(wrapper.get('[data-v2="assign-picked"]').text()).toContain(favorite.name)
      await wrapper.get('[data-v2-toggle-favorite="11"]').trigger('click')
      await flushPromises()
      expect(setFavoriteAssignee).toHaveBeenLastCalledWith(11, false)
      expect(wrapper.get(dialog).text()).toContain('暂无常用打手')
    } finally {
      wrapper.unmount()
    }
  })

  it('输入下一关键词时立即丢弃旧结果，清空后显示常用打手', async () => {
    const old = { id: 11, name: '较早候选', role: null, tag: '', remark: '' }
    const newest = { ...old, id: 12, name: '较新候选' }
    let resolveOld!: (value: { items: typeof old[] }) => void
    let resolveNewest!: (value: { items: typeof newest[] }) => void
    fetchAssigneeOptions
      .mockReturnValueOnce(new Promise((resolve) => { resolveOld = resolve }))
      .mockReturnValueOnce(new Promise((resolve) => { resolveNewest = resolve }))
    const wrapper = await mountWithNodes(['order.assign_taker'])
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      await wrapper.get(`[data-v2-reassign="${orderNo}"]`).trigger('click')
      const input = wrapper.get('input[data-v2="assign-key"]')
      await input.setValue(old.name)
      await vi.advanceTimersByTimeAsync(400)
      await input.setValue(newest.name)
      resolveOld({ items: [old] })
      await flushPromises()
      expect(wrapper.find('[data-v2-assignee="11"]').exists()).toBe(false)
      expect(wrapper.get('[data-v2="dlg-assign"] [role="status"]').text()).toBe('正在搜索…')
      await vi.advanceTimersByTimeAsync(400)
      expect(fetchAssigneeOptions.mock.calls.map(([arg]) => arg.key)).toEqual([old.name, newest.name])
      await input.setValue('')
      resolveNewest({ items: [newest] })
      await flushPromises()
      expect(wrapper.get('[data-v2="dlg-assign"]').text()).toContain('暂无常用打手')
      expect(wrapper.find('[data-v2="assign-candidates"]').exists()).toBe(false)
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  it('输入法确认前不搜索，关窗取消尚未发出的搜索', async () => {
    fetchAssigneeOptions.mockResolvedValue({ items: [] })
    const wrapper = await mountWithNodes(['order.assign_taker'], false)
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const dialog = '[data-v2="dlg-assign"]'
    try {
      const row = wrapper.get(`[data-v2-reassign="${orderNo}"]`)
      await row.trigger('click')
      const input = wrapper.get<HTMLInputElement>('input[data-v2="assign-key"]')
      input.element.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }))
      input.element.value = 'da'
      input.element.dispatchEvent(new InputEvent('input', { bubbles: true, isComposing: true, data: 'da' }))
      await vi.advanceTimersByTimeAsync(500)
      expect(fetchAssigneeOptions).not.toHaveBeenCalled()
      input.element.value = '打手'
      input.element.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: '打手' }))
      await vi.advanceTimersByTimeAsync(100)
      expect(wrapper.get(dialog).text()).toContain('正在搜索')
      await wrapper.get(`${dialog} .el-dialog__headerbtn`).trigger('click')
      await vi.advanceTimersByTimeAsync(1000)
      expect(fetchAssigneeOptions).not.toHaveBeenCalled()
      vi.useRealTimers()
      await vi.waitFor(() => expect(wrapper.find(dialog).exists()).toBe(false))
      await row.trigger('click')
      expect(wrapper.get<HTMLInputElement>('input[data-v2="assign-key"]').element.value).toBe('')
      expect(wrapper.get(dialog).text()).toContain('常用打手')
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  it.each([
    { canPrice: true, price: '25.50', expected: { price: '25.50' } },
    { canPrice: true, price: '0', expected: { price: '0' } },
    { canPrice: true, price: '', expected: {} },
    { canPrice: false, price: '', expected: {} },
  ])('同一弹窗指派和设置发单价，按权限处理零价和空值 $canPrice $price', async ({ canPrice, price, expected }) => {
    fetchAssigneeOptions.mockResolvedValue({ items: [{ id: 11, name: '合成候选', role: null, tag: '', remark: '' }] })
    batchAssignOrders.mockReset().mockResolvedValue({ applied: [orderNo], ineligible: [] })
    const wrapper = await mountWithNodes(canPrice ? ['order.assign_taker', 'order.submit_payout_price'] : ['order.assign_taker'])
    try {
      await wrapper.get(`[data-v2-reassign="${orderNo}"]`).trigger('click')
      const input = 'input[data-v2="assign-price"]'
      expect(wrapper.find(input).exists()).toBe(false)
      await wrapper.get('input[data-v2="assign-key"]').setValue('合成候选')
      await vi.waitFor(() => expect(wrapper.find('[data-v2-assignee="11"]').exists()).toBe(true))
      await wrapper.get('[data-v2-assignee="11"]').trigger('click')
      expect(wrapper.find(input).exists()).toBe(canPrice)
      if (canPrice) await wrapper.get(input).setValue(price)
      await wrapper.get('[data-v2="confirm-assign"]').trigger('click')
      await flushPromises()
      expect(batchAssignOrders).toHaveBeenCalledExactlyOnceWith({
        user_id: 11, order_nos: [orderNo], dry_run: false, ...expected,
      })
    } finally {
      wrapper.unmount()
    }
  })

  it('批量指派列出明确订单，刷新后阻止按旧名单提交', async () => {
    fetchAssigneeOptions.mockResolvedValue({ items: [{ id: 11, name: '合成候选', role: null, tag: '', remark: '' }] })
    batchAssignOrders.mockReset()
    const wrapper = await mountWithNodes(['order.assign_taker'])
    try {
      await wrapper.get(`[data-v2-row-check="${orderNo}"]`).setValue(true)
      await wrapper.get('[data-v2="assign"]').trigger('click')
      expect(wrapper.get('[aria-label="待指派订单"]').text()).toBe(orderNo)
      await wrapper.get('input[data-v2="assign-key"]').setValue('合成候选')
      await vi.waitFor(() => expect(wrapper.find('[data-v2-assignee="11"]').exists()).toBe(true))
      await wrapper.get('[data-v2-assignee="11"]').trigger('click')
      await wrapper.get('[data-v2="refresh"]').trigger('click')
      await flushPromises()
      await wrapper.get('[data-v2="confirm-assign"]').trigger('click')
      expect(batchAssignOrders).not.toHaveBeenCalled()
      expect(wrapper.get('[data-v2="toast"]').text()).toContain('请重新勾选')
    } finally {
      wrapper.unmount()
    }
  })

  it('同一弹窗指派遇到无效发单价或提交失败时保留输入', async () => {
    fetchAssigneeOptions.mockResolvedValue({ items: [{ id: 11, name: '合成候选', role: null, tag: '', remark: '' }] })
    batchAssignOrders.mockReset().mockRejectedValueOnce(new Error('提交失败'))
    const wrapper = await mountWithNodes(['order.assign_taker', 'order.submit_payout_price'])
    try {
      await wrapper.get(`[data-v2-reassign="${orderNo}"]`).trigger('click')
      await wrapper.get('input[data-v2="assign-key"]').setValue('合成候选')
      await vi.waitFor(() => expect(wrapper.find('[data-v2-assignee="11"]').exists()).toBe(true))
      await wrapper.get('[data-v2-assignee="11"]').trigger('click')
      const input = wrapper.get<HTMLInputElement>('input[data-v2="assign-price"]')
      await input.setValue('3.14159')
      await wrapper.get('[data-v2="confirm-assign"]').trigger('click')
      expect(batchAssignOrders).not.toHaveBeenCalled()
      await input.setValue('12.50')
      await wrapper.get('[data-v2="confirm-assign"]').trigger('click')
      await flushPromises()
      expect(batchAssignOrders).toHaveBeenCalledOnce()
      expect(input.element.value).toBe('12.50')
      expect(wrapper.get('[data-v2="confirm-assign"]').isVisible()).toBe(true)
    } finally {
      wrapper.unmount()
    }
  })

  it.each([
    { codes: ['order.clear_taker'], canAssign: false, removePerformance: false, expected: 'N' },
    { codes: ['order.clear_taker', 'order.submit_payout_price'], canAssign: true, removePerformance: true, expected: 'Y' },
  ])('清除接手人的确认按权限提供保留与归零选项，提交 $expected', async ({ codes, canAssign, removePerformance, expected }) => {
    const wrapper = await mountWithNodes(codes)
    await wrapper.get(`[data-v2-reassign="${orderNo}"]`).trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="confirm-assign"]').exists()).toBe(canAssign)
    expect(fetchFavoriteAssignees).toHaveBeenCalledTimes(canAssign ? 1 : 0)
    expect(wrapper.find('[data-v2="confirm-assign"].cv2-btn--fill').exists()).toBe(canAssign)
    const clear = wrapper.get('[data-v2="assign-clear"]')
    expect(clear.classes()).not.toContain('cv2-btn--danger')
    expect(clearTaker).not.toHaveBeenCalled()
    await clear.trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="confirm-clear-and-remove-performance"]').exists()).toBe(removePerformance)
    expect(wrapper.get('[data-v2="confirm-clear"]').text()).toContain('保留绩效金额')
    await wrapper.get(`[data-v2="${removePerformance ? 'confirm-clear-and-remove-performance' : 'confirm-clear'}"]`).trigger('click')
    await flushPromises()
    expect(clearTaker).toHaveBeenCalledExactlyOnceWith(orderNo, { isDelEmp: expected })
    wrapper.unmount()
  })

  it('同步收款打开独立七天范围，超过 31 天先拦，边界 31 天走真实提交分支', async () => {
    const wrapper = await mountWithNodes(['order.change_payment_status'])
    const listRange = lastListParams()
    await wrapper.get('[data-v2="receipt-sync"]').trigger('click')
    await flushPromises()
    const from = wrapper.get<HTMLInputElement>('[data-v2="receipt-sync-from"]')
    const to = wrapper.get<HTMLInputElement>('[data-v2="receipt-sync-to"]')
    const submit = wrapper.get('[data-v2="confirm-receipt-sync"]')
    const days = (Date.parse(to.element.value) - Date.parse(from.element.value)) / 86_400_000 + 1
    expect(days).toBe(7)
    expect(startXgjReceiptSync).not.toHaveBeenCalled()
    await to.setValue('2026-11-14')
    await from.setValue('2026-10-14')
    await submit.trigger('click')
    expect(wrapper.get('[role="alert"]').text()).toContain('当前 32 天')
    expect(startXgjReceiptSync).not.toHaveBeenCalled()
    await from.setValue('2026-10-15')
    await submit.trigger('click')
    await flushPromises()
    expect(startXgjReceiptSync).toHaveBeenCalledExactlyOnceWith({ date_from: '2026-10-15', date_to: '2026-11-14' })
    expect(lastListParams()).toEqual(listRange)
    wrapper.unmount()
  })
})

describe('V2OrderManage：批量改价订单名单', () => {
  const firstNo = '20261113000000000010'
  const secondNo = '20261113000000000020'
  const linkedNo = '20261113000000000030'
  const hiddenNo = '20261113000000000040'
  const item = (order_no: string) => ({
    order_no, status: 'UNASSIGNED', split_id: '', project_text: '测试项目',
    total_price: '10.00', refund_fee: '0.00', net_received: '10.00',
    created_at: '2026-11-13T10:00:00+08:00', completed_at: null,
  })
  const result = (orderNos: string[], page = 1) => ({
    items: orderNos.map(item) as never, total: 51, page, page_size: 50, summary: null,
  })
  const checkbox = (wrapper: VueWrapper, no: string) =>
    wrapper.get<HTMLInputElement>(`[data-v2-row-check="${no}"]`)

  async function openPricePreview(wrapper: VueWrapper, action: 'price' | 'hall-price' = 'price') {
    await wrapper.get(`[data-v2="change-${action}"]`).trigger('click')
    await wrapper.get<HTMLInputElement>(`[data-v2="${action}-value"]`).setValue('0.00')
    await wrapper.get(`[data-v2="confirm-${action}"]`).trigger('click')
    await flushPromises()
  }

  it('批量查询兼容混合单号，分页和拆单展开沿用已提交的名单', async () => {
    const sourceNo = '1234567890'.repeat(2).slice(0, 19)
    const scopedResult = result([firstNo, secondNo])
    scopedResult.items[0] = { ...item(firstNo), split_id: 'test-group', split_members: 2 } as never
    vi.mocked(fetchOrdersWithSummary).mockImplementation(async (params) =>
      params.page === 2 ? result([secondNo], 2) : scopedResult)
    fetchSplitMembers.mockResolvedValue([item(firstNo)])
    const { wrapper } = await mountPage()
    const mode = wrapper.findAllComponents(ElSelect).find((select) => select.attributes('data-v2-filter') === 'search1-mode')!
    await wrapper.get('[data-v2-filter="key1"]').setValue('上一轮关键词')
    mode.vm.$emit('update:modelValue', '10')
    await wrapper.get('[data-v2="search"]').trigger('click')
    const unknownNo = 'x'.repeat(129)
    await wrapper.get('[aria-label="批量单号"]').setValue(` ${sourceNo}，${secondNo}\r\n${sourceNo}； SRC-TEST-1；${unknownNo} `)
    await wrapper.get('[data-v2="confirm-batch-search"]').trigger('click')
    await flushPromises()
    const batch_lookup_nos = `${sourceNo},${secondNo},SRC-TEST-1,${unknownNo}`
    expect(lastListParams()).toMatchObject({ batch_lookup_nos, page: 1 })
    expect(lastListParams()).not.toHaveProperty('keyword')
    expect(fetchSplitMembers).toHaveBeenLastCalledWith('test-group', expect.objectContaining({ batch_lookup_nos }))
    expect(wrapper.get('[data-v2="open-batch-search"]').text()).toContain('已输入 4 个单号')
    await checkbox(wrapper, firstNo).setValue(true)
    await wrapper.get('[data-v2="pager"] .btn-next').trigger('click')
    await flushPromises()
    expect(lastListParams()).toMatchObject({ batch_lookup_nos, page: 2 })
    expect(checkbox(wrapper, secondNo).element.checked).toBe(false)
    await wrapper.get('[data-v2="refresh"]').trigger('click')
    await flushPromises()
    expect(lastListParams()).toMatchObject({ batch_lookup_nos, page: 2 })
    mode.vm.$emit('update:modelValue', '0')
    await wrapper.get('[data-v2="search"]').trigger('click')
    await flushPromises()
    expect(lastListParams()).toMatchObject({ keyword: '上一轮关键词', page: 1 })
    expect(lastListParams()).not.toHaveProperty('batch_lookup_nos')
    wrapper.unmount()
  })

  it('空单号和超出搜索接口范围时保留输入并提示原因', async () => {
    const { wrapper } = await mountPage()
    const mode = wrapper.findAllComponents(ElSelect).find((select) => select.attributes('data-v2-filter') === 'search1-mode')!
    mode.vm.$emit('update:modelValue', '10')
    await wrapper.get('[data-v2="search"]').trigger('click')
    const input = wrapper.get<HTMLTextAreaElement>('[aria-label="批量单号"]')
    for (const [text, message] of [
      ['  ，\n', '请输入要搜索的订单号'],
      [Array.from({ length: 201 }, (_, i) => `SRC-${i}`).join(','), '批量单号一次最多 200 个'],
    ] as const) {
      await input.setValue(text)
      await wrapper.get('[data-v2="confirm-batch-search"]').trigger('click')
      expect(wrapper.get('[data-v2="toast"]').text()).toBe(message)
      expect(input.element.value).toBe(text)
    }
    expect(fetchOrdersWithSummary).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('翻页和重新查询后必须重新勾选；预检和实写只使用本次明示的单号', async () => {
    vi.mocked(fetchOrdersWithSummary).mockImplementation(async (params) =>
      params.page === 2 ? result([hiddenNo], 2) : result([firstNo, secondNo]))
    const { wrapper } = await mountPage()
    useSessionStore().user = { menu_permissions: ['order.edit_payout_price'] } as never
    await flushPromises()
    await checkbox(wrapper, firstNo).setValue(true)
    await wrapper.get('[data-v2="pager"] .btn-next').trigger('click')
    await flushPromises()
    expect(checkbox(wrapper, hiddenNo).element.checked).toBe(false)
    await checkbox(wrapper, hiddenNo).setValue(true)
    await wrapper.get('[data-v2="pager"] .btn-prev').trigger('click')
    await flushPromises()
    expect(checkbox(wrapper, firstNo).element.checked).toBe(false)
    await checkbox(wrapper, firstNo).setValue(true)
    await wrapper.get('[data-v2="search"]').trigger('click')
    await flushPromises()
    expect(checkbox(wrapper, firstNo).element.checked).toBe(false)

    await checkbox(wrapper, secondNo).setValue(true)
    await openPricePreview(wrapper)
    const preview = wrapper.get('[data-v2="dlg-batch-preview"]')
    expect(preview.text()).toContain(secondNo)
    expect(preview.text()).toContain('接单价改为 ¥0.00')
    expect(preview.text()).not.toContain(firstNo)
    expect(preview.text()).not.toContain(hiddenNo)
    expect(batchOrderAction).toHaveBeenCalledExactlyOnceWith('price', {
      order_nos: [secondNo], total_price: '0.00', dry_run: true, confirmed_effects: [],
    })
    await wrapper.get('[data-v2="confirm-batch-preview"]').trigger('click')
    await flushPromises()
    expect(batchOrderAction).toHaveBeenLastCalledWith('price', {
      order_nos: [secondNo], total_price: '0.00', dry_run: false, confirmed_effects: [],
    })
    expect(checkbox(wrapper, secondNo).element.checked).toBe(false)
    wrapper.unmount()
  })

  it('明确列出联动与跳过单；取消时不写，确认后仅提交合格单及确认过的联动', async () => {
    vi.mocked(fetchOrdersWithSummary).mockResolvedValue(result([firstNo, secondNo]))
    const effects = [{ order_no: linkedNo, source_order_no: '测试来源', operation: '自动调平接单价' }]
    batchOrderAction.mockImplementation(async (_action, { dry_run }) => ({
      dry_run, eligible: [firstNo], applied: dry_run ? [] : [firstNo], effects,
      ineligible: [{ order_no: secondNo, msg: '测试失败原因', code: 1002 }],
    }))
    const { wrapper } = await mountPage()
    useSessionStore().user = { menu_permissions: ['order.edit_payout_price'] } as never
    await flushPromises()
    await checkbox(wrapper, firstNo).setValue(true)
    await checkbox(wrapper, secondNo).setValue(true)

    await openPricePreview(wrapper)
    const preview = wrapper.get('[data-v2="dlg-batch-preview"]')
    expect(preview.text()).toContain(firstNo)
    expect(preview.text()).toContain(linkedNo)
    expect(preview.text()).toContain('自动调平接单价')
    expect(preview.text()).toContain(secondNo)
    expect(preview.text()).toContain('测试失败原因')
    await wrapper.get('[data-v2="cancel-batch-preview"]').trigger('click')
    expect(batchOrderAction).toHaveBeenCalledTimes(1)

    await openPricePreview(wrapper)
    await wrapper.get('[data-v2="confirm-batch-preview"]').trigger('click')
    await flushPromises()
    expect(batchOrderAction).toHaveBeenLastCalledWith('price', {
      order_nos: [firstNo], total_price: '0.00', dry_run: false, confirmed_effects: effects,
    })
    wrapper.unmount()
  })

  it('预检后刷新列表会作废旧确认，包括修改发单价', async () => {
    vi.mocked(fetchOrdersWithSummary).mockResolvedValue(result([firstNo]))
    const { wrapper } = await mountPage()
    useSessionStore().user = { menu_permissions: ['order.set_offer_price'] } as never
    await flushPromises()
    await checkbox(wrapper, firstNo).setValue(true)
    await openPricePreview(wrapper, 'hall-price')
    expect(batchOrderAction).toHaveBeenCalledExactlyOnceWith('hall-price', {
      order_nos: [firstNo], price: '0.00', dry_run: true,
    })
    await wrapper.get('[data-v2="refresh"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-v2="confirm-batch-preview"]').trigger('click')
    await flushPromises()
    expect(batchOrderAction).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('订单列表或勾选已变化')
    wrapper.unmount()
  })
})

describe('V2OrderManage：批量删除确认', () => {
  async function selectOrders(count: number): Promise<{ wrapper: VueWrapper; orderNos: string[] }> {
    const orderNos = Array.from({ length: count }, (_, index) => `2026111300000000000${index + 1}`)
    const items = orderNos.map((order_no) => ({
      order_no, status: 'UNASSIGNED', split_id: '', project_text: '测试项目',
      total_price: '10.00', refund_fee: '0.00', net_received: '10.00',
      created_at: '2026-11-13T10:00:00+08:00', completed_at: null,
    }))
    vi.mocked(fetchOrdersWithSummary).mockResolvedValueOnce({
      items: items as never, total: count, page: 1, page_size: 50, summary: null,
    })
    const { wrapper } = await mountPage()
    useSessionStore().user = { menu_permissions: ['order.delete'] } as never
    await flushPromises()
    await wrapper.get<HTMLInputElement>('[data-v2="check-page-all"]').setValue(true)
    await wrapper.get('[data-v2="delete"]').trigger('click')
    return { wrapper, orderNos }
  }

  it('三笔订单确认名单后提交，不要求填写原因', async () => {
    const { wrapper, orderNos } = await selectOrders(3)
    expect(wrapper.get('[data-v2="confirm-delete"]').text()).toBe('确定删除')
    await wrapper.get('[data-v2="confirm-delete"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="dlg-delete-reason"]').exists()).toBe(false)
    expect(batchOrderAction).toHaveBeenCalledExactlyOnceWith('delete-v2', {
      order_nos: orderNos, dry_run: true, isDelEmp: 'Y', confirmed_effects: [],
    })
    await wrapper.get('[data-v2="confirm-batch-preview"]').trigger('click')
    await flushPromises()
    expect(batchOrderAction).toHaveBeenLastCalledWith('delete-v2', {
      order_nos: orderNos, dry_run: false, isDelEmp: 'Y', confirmed_effects: [],
    })
  })

  it('四笔订单必须二次确认，原因去掉首尾空白后才提交', async () => {
    const { wrapper, orderNos } = await selectOrders(4)
    expect(wrapper.get('[data-v2="confirm-delete"]').text()).toBe('下一步')
    await wrapper.get('[data-v2="confirm-delete"]').trigger('click')
    await flushPromises()
    const dialog = wrapper.get('[data-v2="dlg-delete-reason"]')
    expect(dialog.text()).toContain('将删除选中的 4 笔订单')
    const input = dialog.get<HTMLTextAreaElement>('textarea')
    const submit = dialog.get<HTMLButtonElement>('[data-v2="confirm-delete-reason"]')
    await input.setValue('  ')
    expect(submit.element.disabled).toBe(true)
    expect(batchOrderAction).not.toHaveBeenCalled()
    await input.setValue('  清理测试订单  ')
    expect(submit.element.disabled).toBe(false)
    await submit.trigger('click')
    await flushPromises()
    expect(batchOrderAction).toHaveBeenCalledExactlyOnceWith('delete-v2', {
      order_nos: orderNos, dry_run: true, isDelEmp: 'Y', reason: '清理测试订单', confirmed_effects: [],
    })
    await wrapper.get('[data-v2="confirm-batch-preview"]').trigger('click')
    await flushPromises()
    expect(batchOrderAction).toHaveBeenLastCalledWith('delete-v2', {
      order_nos: orderNos, dry_run: false, isDelEmp: 'Y', reason: '清理测试订单', confirmed_effects: [],
    })
  })

  it('取消二次确认时保留勾选，不提交删除', async () => {
    const { wrapper } = await selectOrders(4)
    await wrapper.get('[data-v2="confirm-delete"]').trigger('click')
    await flushPromises()
    const dialog = wrapper.get('[data-v2="dlg-delete-reason"]')
    await dialog.get('textarea').setValue('测试取消')
    await dialog.get('.cv2-dlg-actions .cv2-btn--quiet').trigger('click')
    expect(batchOrderAction).not.toHaveBeenCalled()
    expect(wrapper.get('[data-v2="checked-count"]').text()).toBe('已选 4 行')
    await wrapper.get('[data-v2="delete"]').trigger('click')
    await wrapper.get('[data-v2="confirm-delete"]').trigger('click')
    expect(wrapper.get<HTMLTextAreaElement>('[data-v2="dlg-delete-reason"] textarea').element.value).toBe('')
  })
})

describe('V2OrderManage：打手 / 发单人候选改关键词远程搜索（2026-11-09）', () => {
  it('① 挂载**零** filter-options 请求（候选不再预载；回归成预载会让本条红）', async () => {
    const { wrapper } = await mountPage()
    await flushPromises()
    expect(fetchFilterOptions).not.toHaveBeenCalled()
    // 面板还没开过：候选池是空的。
    expect(wrapper.find('[data-v2-pop="taker"]').exists()).toBe(false)
  })

  it('① 切游戏 / 点重置也**零**候选请求（旧页 onGameChange 会重取名单的那条路已删）', async () => {
    const { wrapper } = await mountPage()
    await flushPromises()
    // 切游戏（el-select 的 game 档 emit change）。
    const gameSelect = wrapper
      .findAllComponents(ElSelect)
      .find((c) => c.attributes('data-v2-filter') === 'game')
    expect(gameSelect, '游戏档的 el-select 应当在（data-v2-filter="game"）').toBeDefined()
    gameSelect!.vm.$emit('change', 7)
    await flushPromises()
    // 重置。
    await wrapper.find('[data-v2="reset"]').trigger('click')
    await flushPromises()
    expect(fetchFilterOptions).not.toHaveBeenCalled()
  })

  it('② 空关键词不发请求：打开面板即清候选，显示「输入关键词搜索」提示句', async () => {
    const { wrapper } = await mountPage()
    // 先造一次搜索残留（证明「打开面板清残留」与「空词不发请求」两件事同时成立）。
    fetchFilterOptions.mockResolvedValue({
      takers: TAKERS,
      publishers: PUBLISHERS,
      takers_truncated: false,
      publishers_truncated: false,
    })
    await openPick(wrapper, 'taker')
    await typeQuery(wrapper, 'taker', '测试')
    await vi.waitFor(() => expect(fetchFilterOptions).toHaveBeenCalledTimes(1))
    await openPick(wrapper, 'taker') // 关闭
    await openPick(wrapper, 'taker') // 再开：pickQuery 复位成空串 ⇒ 候选清空、零新请求
    expect(fetchFilterOptions).toHaveBeenCalledTimes(1)
    const pop = wrapper.find('[data-v2-pop="taker"]')
    expect(pop.exists()).toBe(true)
    expect(pop.text()).toContain('输入关键词搜索')
    expect(pop.text()).not.toContain('测试打手甲')
  })

  it('② 输入关键词 ⇒ 防抖请求 `fetchFilterOptions({game, search})`；taker 槽取 `takers` 那份', async () => {
    const { wrapper } = await mountPage()
    fetchFilterOptions.mockResolvedValue({
      takers: TAKERS,
      publishers: PUBLISHERS,
      takers_truncated: false,
      publishers_truncated: false,
    })
    await openPick(wrapper, 'taker')
    await typeQuery(wrapper, 'taker', '测试打手')
    // 400ms 防抖：vi.waitFor 的轮询窗口覆盖它（本文件不用假定时器，如实等真实窗口）。
    await vi.waitFor(() => expect(fetchFilterOptions).toHaveBeenCalledTimes(1))
    expect(fetchFilterOptions).toHaveBeenCalledWith({ game: null, search: '测试打手' })
    await flushPromises()
    const pop = wrapper.find('[data-v2-pop="taker"]')
    expect(pop.text()).toContain('测试打手甲')
    expect(pop.text()).toContain('测试打手乙')
    expect(pop.text()).not.toContain('测试发单人甲')
  })

  it('② publisher 槽取 `publishers` 那份（判别力：两份名单不同形，取错当场红）', async () => {
    const { wrapper } = await mountPage()
    fetchFilterOptions.mockResolvedValue({
      takers: TAKERS,
      publishers: PUBLISHERS,
      takers_truncated: false,
      publishers_truncated: false,
    })
    await openPick(wrapper, 'publisher')
    await typeQuery(wrapper, 'publisher', '测试发单')
    await vi.waitFor(() => expect(fetchFilterOptions).toHaveBeenCalledTimes(1))
    expect(fetchFilterOptions).toHaveBeenCalledWith({ game: null, search: '测试发单' })
    await flushPromises()
    const pop = wrapper.find('[data-v2-pop="publisher"]')
    expect(pop.text()).toContain('测试发单人甲')
    expect(pop.text()).not.toContain('测试打手甲')
  })

  it('② 请求失败 ⇒ 候选清空、面板落「没有匹配的人」，不弹全局错误', async () => {
    const { wrapper } = await mountPage()
    fetchFilterOptions.mockRejectedValue(new Error('网络故障（用例构造）'))
    await openPick(wrapper, 'taker')
    await typeQuery(wrapper, 'taker', '测试')
    await vi.waitFor(() => expect(fetchFilterOptions).toHaveBeenCalledTimes(1))
    await flushPromises()
    const pop = wrapper.find('[data-v2-pop="taker"]')
    expect(pop.text()).toContain('没有匹配的人')
  })

  it('③ 已选值保留：选中后再次搜索替换候选，按钮名字不丢、提交参数不变（整链）', async () => {
    const { wrapper } = await mountPage()
    fetchFilterOptions.mockResolvedValue({
      takers: TAKERS,
      publishers: PUBLISHERS,
      takers_truncated: false,
      publishers_truncated: false,
    })
    // ① 打开面板 → 搜索 → 点「测试打手甲」。
    await openPick(wrapper, 'taker')
    await typeQuery(wrapper, 'taker', '测试打手')
    await vi.waitFor(() => expect(fetchFilterOptions).toHaveBeenCalledTimes(1))
    await flushPromises()
    const option = wrapper
      .findAll('[data-v2-pop="taker"] .flt-pop-item')
      .find((b) => b.text() === '测试打手甲')
    expect(option, '候选里应有「测试打手甲」').toBeDefined()
    await option!.trigger('click')
    await flushPromises()
    // 选中后面板关闭，按钮显示那个人。
    const pickBtn = wrapper.find('[data-v2-filter="taker"]')
    expect(pickBtn.text()).toContain('测试打手甲')
    // ② 换一个搜索词把候选整体替换掉（回包不再含甲）。
    fetchFilterOptions.mockResolvedValue({
      takers: [{ id: 99, name: '测试打手丙' }],
      publishers: [],
      takers_truncated: false,
      publishers_truncated: false,
    })
    await openPick(wrapper, 'taker')
    await typeQuery(wrapper, 'taker', '丙')
    await vi.waitFor(() => expect(fetchFilterOptions).toHaveBeenCalledTimes(2))
    await flushPromises()
    await openPick(wrapper, 'taker') // 关面板
    // ⭐ 候选已换 ⇒ 按钮上的名字必须还在（pickedNames 缓存；退化成「从 options 反查名字」的实现此处即红）。
    expect(wrapper.find('[data-v2-filter="taker"]').text()).toContain('测试打手甲')
    // ③ 提交参数整链：点查询 ⇒ 列表请求带 `taker_user: 11`。
    await wrapper.find('[data-v2="search"]').trigger('click')
    await flushPromises()
    expect(lastListParams()).toMatchObject({ taker_user: 11 })
    expect('hall_publisher_user' in lastListParams()).toBe(false)
  })

  it('③ publisher 槽选中 ⇒ `hall_publisher_user` 真发参（recType 切到 10 那一档）', async () => {
    const { wrapper } = await mountPage()
    fetchFilterOptions.mockResolvedValue({
      takers: TAKERS,
      publishers: PUBLISHERS,
      takers_truncated: false,
      publishers_truncated: false,
    })
    await openPick(wrapper, 'publisher')
    await typeQuery(wrapper, 'publisher', '测试发单')
    await vi.waitFor(() => expect(fetchFilterOptions).toHaveBeenCalledTimes(1))
    await flushPromises()
    const option = wrapper
      .findAll('[data-v2-pop="publisher"] .flt-pop-item')
      .find((b) => b.text() === '测试发单人甲')
    expect(option, '候选里应有「测试发单人甲」').toBeDefined()
    await option!.trigger('click')
    await flushPromises()
    await wrapper.find('[data-v2="search"]').trigger('click')
    await flushPromises()
    expect(lastListParams()).toMatchObject({ hall_publisher_user: 21 })
    expect('taker_user' in lastListParams()).toBe(false)
  })

  it('③ 清空（点「清空」/「全部」）⇒ 参数里两个键都不发', async () => {
    const { wrapper } = await mountPage()
    fetchFilterOptions.mockResolvedValue({
      takers: TAKERS,
      publishers: PUBLISHERS,
      takers_truncated: false,
      publishers_truncated: false,
    })
    await openPick(wrapper, 'taker')
    await typeQuery(wrapper, 'taker', '测试打手')
    await vi.waitFor(() => expect(fetchFilterOptions).toHaveBeenCalledTimes(1))
    await flushPromises()
    const option = wrapper
      .findAll('[data-v2-pop="taker"] .flt-pop-item')
      .find((b) => b.text() === '测试打手甲')
    await option!.trigger('click')
    await flushPromises()
    await openPick(wrapper, 'taker')
    const clearBtn = wrapper.find('[data-v2-pop="taker"] .flt-pop-clear')
    expect(clearBtn.exists()).toBe(true)
    await clearBtn.trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2-filter="taker"]').text()).toContain('请选择')
    await wrapper.find('[data-v2="search"]').trigger('click')
    await flushPromises()
    expect('taker_user' in lastListParams()).toBe(false)
    expect('hall_publisher_user' in lastListParams()).toBe(false)
  })
})

it('销售客服筛选通过全部清除选中值，列表底部没有清空按钮', async () => {
  fetchKefuOptions.mockResolvedValue({ items: [{ id: 51, name: '合成客服甲' }], truncated: false })
  const { wrapper } = await mountPage()
  try {
    await openPick(wrapper, 'kefu')
    const pop = '[data-v2-pop="kefu"]'
    expect(wrapper.find(`${pop} .flt-pop-clear`).exists()).toBe(false)
    const list = wrapper.get(`${pop} [aria-label="客服候选"]`)
    expect(list.attributes('tabindex')).toBe('0')
    await list.findAll('button').find((button) => button.text() === '合成客服甲')!.trigger('click')
    await wrapper.get('[data-v2="search"]').trigger('click')
    await flushPromises()
    expect(lastListParams().sales_user).toBe(51)

    await openPick(wrapper, 'kefu')
    await wrapper.get(pop).findAll('button').find((button) => button.text() === '全部')!.trigger('click')
    await wrapper.get('[data-v2="search"]').trigger('click')
    await flushPromises()
    expect(lastListParams()).not.toHaveProperty('sales_user')
  } finally {
    wrapper.unmount()
  }
})

describe('V2OrderManage：默认列布局', () => {
  it('实收款默认隐藏，录单人排在录单时间前，价格相邻列采用收窄默认宽', async () => {
    const { wrapper } = await mountPage()
    const headers = wrapper.findAll('thead th').map((th) => th.text().trim())
    expect(headers).not.toContain('实收款')
    expect(headers.indexOf('录单人')).toBeGreaterThan(-1)
    expect(headers.indexOf('录单人')).toBeLessThan(headers.indexOf('录单时间'))
    expect(headers[headers.indexOf('退款') + 1]).toBe('重要备注')
    expect(headers).not.toContain('订单截图')
    expect(wrapper.findAll('colgroup col')).toHaveLength(headers.length)

    const widths = new Map(
      wrapper.findAll('colgroup col').map((col) => [col.attributes('data-v2-col'), col.element.getAttribute('style')]),
    )
    expect(widths.get('ww')).toContain('84px')
    expect(widths.get('source')).toContain('84px')
    expect(widths.get('taker')).toContain('84px')
    expect(widths.get('hallprice')).toContain('88px')
    expect(widths.get('price')).toContain('96px')
    expect(widths.get('refund')).toContain('60px')
    const colKeys = [...widths.keys()]
    expect(colKeys[colKeys.indexOf('refund') + 1]).toBe('remark')
    expect(widths.get('project')).toContain('320px')
    expect(widths.get('actions')).toContain('114px')
  })

  it('重要备注随原列显隐设置移动，和退款单元格对齐', async () => {
    const orderNo = '20261113000000000010'
    const item = {
      order_no: orderNo, status: 'UNASSIGNED', split_id: '', project_text: '测试项目', remark: '测试备注',
      total_price: '10.00', refund_fee: '0.00', net_received: '10.00',
      created_at: '2026-11-13T10:00:00+08:00', completed_at: null,
    }
    const list = { items: [item] as never, total: 1, page: 1, page_size: 20, summary: null }
    vi.mocked(fetchOrdersWithSummary).mockResolvedValue(list)
    const { wrapper } = await mountPage()
    const row = wrapper.get(`[data-v2-row="${orderNo}"]`)
    const refundCell = row.get(`[data-v2-refund="${orderNo}"]`).element.closest('td')
    expect(refundCell?.nextElementSibling?.textContent).toContain('测试备注')
    wrapper.unmount()

    localStorage.setItem('tt.v2.order-manage.hidden-cols.v3', JSON.stringify(['remark']))
    const hidden = (await mountPage()).wrapper
    expect(hidden.find('col[data-v2-col="remark"]').exists()).toBe(false)
    expect(hidden.find(`[data-v2-remark-open="${orderNo}"]`).exists()).toBe(false)
    hidden.unmount()
  })

  it('保留已保存的列宽，即使数值与旧默认值相同', async () => {
    localStorage.setItem('tt.v2.table.v2.order-manage', JSON.stringify({
      ww: 100, source: 100, taker: 110, hallprice: 124, price: 136, refund: 80, project: 348,
    }))
    await mountPage()
    const saved = JSON.parse(localStorage.getItem('tt.v2.table.v2.order-manage') ?? '{}') as Record<string, number>
    expect(saved).toMatchObject({ ww: 100, source: 100, taker: 110, hallprice: 124, price: 136, refund: 80, project: 348 })
  })

  it('用户调回旧默认列宽后重新打开也会保留', async () => {
    const first = await mountPage()
    first.wrapper.unmount()
    localStorage.setItem('tt.v2.table.v2.order-manage', JSON.stringify({ taker: 100, info: 150, actions: 176 }))
    const second = await mountPage()
    const widths = new Map(second.wrapper.findAll('colgroup col').map((col) => [col.attributes('data-v2-col'), col.element.getAttribute('style')]))
    expect(widths.get('taker')).toContain('100px')
    expect(widths.get('info')).toContain('150px')
    expect(widths.get('actions')).toContain('176px')
    second.wrapper.unmount()
  })

  it('代练内容立即悬停预览，带练通单的实收变色但状态和接单价不变色', async () => {
    localStorage.setItem('tt.v2.order-manage.hidden-cols.v3', '[]')
    const orderNo = '20261113000000000001'
    const item = {
      order_no: orderNo, status: 'IN_PROGRESS', split_id: '', project_text: '测试代练内容',
      total_price: '10.00', refund_fee: '0.00', net_received: '10.00', taker_name: '测试接手人',
      created_at: '2026-11-13T10:00:00+08:00', completed_at: null, platform_push: { is_active: true },
    }
    vi.mocked(fetchOrdersWithSummary).mockResolvedValueOnce({ items: [item] as never, total: 1, page: 1, page_size: 50, summary: null })
    const { wrapper } = await mountPage()
    const row = wrapper.get(`[data-v2-row="${orderNo}"]`)
    expect(row.classes()).toContain('cv2-row--platform')
    expect(row.get('[data-v2-net]').classes()).toContain('cv2-push-text')
    expect(row.get(`[data-v2-status="${orderNo}"]`).element.closest('td')?.classList.contains('cv2-push-text')).toBe(false)
    expect(row.get(`[data-v2-price="${orderNo}"]`).element.closest('td')?.classList.contains('cv2-push-text')).toBe(false)
    const project = row.get(`[data-v2-project="${orderNo}"]`)
    await project.trigger('mouseenter', { clientX: 80, clientY: 100 })
    expect(wrapper.get('[data-v2="hover-preview"]').text()).toBe('测试代练内容')
    vi.useFakeTimers()
    await project.trigger('mouseleave')
    vi.advanceTimersByTime(150)
    vi.useRealTimers()
    await flushPromises()
    expect(wrapper.find('[data-v2="hover-preview"]').exists()).toBe(false)
    await project.trigger('mouseenter', { clientX: 80, clientY: 100 })
    await project.get('button').trigger('click')
    expect(wrapper.find('[data-v2="hover-preview"]').exists()).toBe(false)
    expect(wrapper.get('[data-v2="dlg-order-text"]').isVisible()).toBe(true)
    wrapper.unmount()
  })

  it('长内容预览靠近右下角时给点击留位置，可移入卡片滚动', async () => {
    const orderNo = '20261113000000000002'
    const item = {
      order_no: orderNo, status: 'IN_PROGRESS', split_id: '', project_text: '长内容'.repeat(200),
      total_price: '10.00', refund_fee: '0.00', net_received: '10.00', taker_name: '测试接手人',
      created_at: '2026-11-13T10:00:00+08:00', completed_at: null,
    }
    vi.mocked(fetchOrdersWithSummary).mockResolvedValueOnce({ items: [item] as never, total: 1, page: 1, page_size: 50, summary: null })
    const { wrapper } = await mountPage()
    const rect = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ width: 200, height: 400 } as DOMRect)
    const project = wrapper.get(`[data-v2-project="${orderNo}"]`)
    await project.trigger('mouseenter', { clientX: window.innerWidth - 10, clientY: window.innerHeight - 10 })
    const card = wrapper.get<HTMLElement>('[data-v2="hover-preview"]')
    expect(card.element.style.left).toBe(`${window.innerWidth - 224}px`)
    expect(card.element.style.top).toBe(`${window.innerHeight - 408}px`)
    vi.useFakeTimers()
    await project.trigger('mouseleave')
    await card.trigger('mouseenter')
    vi.advanceTimersByTime(150)
    expect(wrapper.find('[data-v2="hover-preview"]').exists()).toBe(true)
    await card.trigger('mouseleave')
    vi.advanceTimersByTime(150)
    vi.useRealTimers()
    await flushPromises()
    expect(wrapper.find('[data-v2="hover-preview"]').exists()).toBe(false)
    rect.mockRestore()
    wrapper.unmount()
  })
})

describe('V2 订单状态签', () => {
  it('管理订单和平台单的系统状态共用样式，平台交易状态保留自己的标记', () => {
    const manage = readFileSync(resolve(process.cwd(), 'src/v2/V2OrderManage.vue'), 'utf8')
    const platform = readFileSync(resolve(process.cwd(), 'src/v2/V2PlatformOrders.vue'), 'utf8')

    expect(manage).toMatch(/class="cv2-st cv2-order-status"\s+:class="\[orderStatusClass\(row.status\)/)
    expect(platform).toMatch(/class="cv2-st cv2-order-status cv2-st--static cv2-st--mark"\s+:class="orderStatusClass\(row.orderStatus\)/)
    expect(platform).toMatch(/class="cv2-st cv2-st--static cv2-st--mark"\s+:class="dispStatusBadge\(row.push\)/)
  })

  it('第三方平台订单的系统状态紧跟接手人列，清单、选择器与表格顺序一致', () => {
    const source = readFileSync(resolve(process.cwd(), 'src/v2/V2PlatformOrders.vue'), 'utf8')
    const columns = source.match(/const PLATFORM_COLUMNS = \[([\s\S]*?)\] as const/)?.[1] ?? ''
    const titles = source.match(/const COL_TITLES: Record<string, string> = \{([\s\S]*?)\n\}/)?.[1] ?? ''
    const header = source.match(/<thead>([\s\S]*?)<\/thead>/)?.[1] ?? ''
    const body = source.match(/<tbody>([\s\S]*?)<\/tbody>/)?.[1] ?? ''
    const ordered = (text: string, left: string, right: string): boolean =>
      text.indexOf(left) >= 0 && text.indexOf(left) < text.indexOf(right)

    for (const text of [columns, titles, header, body]) {
      expect(ordered(text, 'accepter', 'orderstatus')).toBe(true)
    }
    expect(ordered(columns, 'orderstatus', 'over')).toBe(true)
    expect(ordered(header, '系统状态', '完成单数')).toBe(true)
    expect(ordered(body, "colVisible('orderstatus')", "colVisible('over')")).toBe(true)
  })
})
