// @vitest-environment jsdom
import { createPinia, setActivePinia } from 'pinia'
import { pickSelect, popupOption, provideFramework7 } from './dialogFixture'
import { claimScreenshot, fetchScreenshots, requestScreenshotTicket } from '@/api/screenshots'
import { assignPeerTaker, fetchPeerDirectAssignment, fetchPeerDraftStudios, searchPeerTakers } from '@/api/peerDirectAssignment'
import { putFileToStorage } from '@/layui-island/directUpload'
import { enableAutoUnmount, flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/orders', () => ({
  fetchOrders: vi.fn(),
  fetchOrder: vi.fn(),
  createOrder: vi.fn(),
  // ⭐ `saveEdit` 走带回信封 msg 的 `updateOrderWithMessage`；
  // `updateOrder` 保留在桩里，专门钉「老出口没人再调」（见编辑出口用例）。
  updateOrder: vi.fn(),
  updateOrderWithMessage: vi.fn(),
  readPlatformOrder: vi.fn(),
  fetchKefuOptions: vi.fn(),
  fetchAssigneeOptions: vi.fn(),
}))
vi.mock('@/api/config', () => ({
  fetchGames: vi.fn(),
  fetchServers: vi.fn(),
  listConfig: vi.fn(),
}))
vi.mock('@/api/orderSources', () => ({ fetchOrderSources: vi.fn() }))
vi.mock('@/api/peerDirectAssignment', () => ({
  assignPeerTaker: vi.fn(),
  fetchPeerDirectAssignment: vi.fn(),
  fetchPeerDraftStudios: vi.fn(),
  searchPeerTakers: vi.fn(),
}))
vi.mock('@/api/screenshots', () => ({
  claimScreenshot: vi.fn(),
  fetchScreenshots: vi.fn(),
  requestScreenshotTicket: vi.fn(),
}))
vi.mock('@/layui-island/directUpload', () => ({ putFileToStorage: vi.fn() }))
vi.mock('@/layui-island/imageCompress', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/layui-island/imageCompress')>(),
  compressImage: (file: File) => Promise.resolve(file),
}))

import {
  createOrder,
  fetchAssigneeOptions,
  fetchKefuOptions,
  fetchOrder,
  readPlatformOrder,
  updateOrder,
  updateOrderWithMessage,
  type Order,
  type PlatformOrderFields,
} from '@/api/orders'
import { fetchGames, fetchServers, listConfig, type ConfigRow } from '@/api/config'
import { fetchOrderSources } from '@/api/orderSources'
import { BizError } from '@/api/errors'
import { useSessionStore } from '@/stores/session'
import KefuOrderForm from '../KefuOrderForm.vue'

const api = {
  assignPeerTaker: vi.mocked(assignPeerTaker),
  fetchPeerDirectAssignment: vi.mocked(fetchPeerDirectAssignment),
  fetchPeerDraftStudios: vi.mocked(fetchPeerDraftStudios),
  searchPeerTakers: vi.mocked(searchPeerTakers),
  createOrder: vi.mocked(createOrder),
  fetchAssigneeOptions: vi.mocked(fetchAssigneeOptions),
  fetchGames: vi.mocked(fetchGames),
  fetchKefuOptions: vi.mocked(fetchKefuOptions),
  fetchOrder: vi.mocked(fetchOrder),
  fetchOrderSources: vi.mocked(fetchOrderSources),
  fetchServers: vi.mocked(fetchServers),
  listConfig: vi.mocked(listConfig),
  readPlatformOrder: vi.mocked(readPlatformOrder),
  updateOrder: vi.mocked(updateOrder),
  updateOrderWithMessage: vi.mocked(updateOrderWithMessage),
  claimScreenshot: vi.mocked(claimScreenshot),
  fetchScreenshots: vi.mocked(fetchScreenshots),
  requestScreenshotTicket: vi.mocked(requestScreenshotTicket),
  putFileToStorage: vi.mocked(putFileToStorage),
}

provideFramework7()
enableAutoUnmount(afterEach)

const ORDER_NO = '20261020123456789012'
const ALL_NODES = [
  'order.list',
  'order.add_edit',
  'order.submit_edit',
  'order.assign_taker',
  'order.send_to_grab_hall',
  'order.upload_screenshot',
  'order.view_detail',
  'order.view_content',
  'order.allow_change_sales_agent',
]

const GAME: ConfigRow = { id: 3, name: '王者荣耀', sort_order: 0, is_active: true, created_at: '' }
const PROJECT: ConfigRow = { id: 31, name: '星耀上王者', sort_order: 0, is_active: true, created_at: '' }

/**
 * 手写一个可外部 resolve 的 promise（测 latest-wins 的「慢响应」用）。
 * ⚠️ 只在**需要受控 promise** 的用例里用（「识别 latest-wins」「识别中…」两条）；
 * ⛔ 不要把这两条改成「不用受控 promise」的形态（§7.1 的判据要求它们真的验 latest-wins）。
 */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

const PLATFORM_FIELDS: PlatformOrderFields = {
  payment: '',
  buyer_nick: '',
  customer_phone: '',
  remark: '',
  order_status: 0,
  pay_time: 0,
  order_source: null,
  order_source_name: '',
  platform: 'xgj',
}

function orderFixture(overrides: Partial<Order> = {}): Order {
  return {
    order_no: ORDER_NO,
    status: 'UNASSIGNED',
    order_type: 'BOOST',
    game: 3,
    server: 11,
    category: 21,
    project: 31,
    project_text: '星耀上王者',
    split_id: '',
    content: '商品内容',
    demand: '',
    time_requirement: '2天3小时',
    game_account: '',
    game_sub_account: '',
    game_role_name: '',
    current_info_1: '',
    current_info_2: '',
    customer_phone: '13800000000',
    customer_qq: '',
    customer_ww: '买家昵称',
    total_price: '88.00',
    received_price: '50.00',
    platform_price: '60.00',
    hall_price: null,
    refund_fee: '0.00',
    fee: '5.00',
    payment_method: '',
    payment_status: 'UNSETTLED',
    source: '闲管家',
    order_source: 1,
    source_order_no: '',
    sales_agent: '客服小九',
    is_urgent: false,
    remark: '',
    tag: '',
    created_by: 'kefu',
    updated_by: 'kefu',
    created_at: '2026-10-20T10:30:00+08:00',
    updated_at: '2026-10-20T11:30:00+08:00',
    allowed_transitions: [],
    completed_requires_review: false,
    ...overrides,
  }
}

/**
 * 挂载录单表单。
 * ⚠️ 签名是 `(nodes, orderNo)`，**不是**对象：`mountForm({ nodes: [...] })` 会把对象塞进
 * `menu_permissions`，`stores/session.ts:82` 的 `new Set(对象)` 会直接 TypeError。
 */
async function mountForm(nodes: string[] = ALL_NODES, orderNo = ''): Promise<VueWrapper> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const session = useSessionStore()
  session.status = 'ready'
  session.user = {
    id: 1,
    username: 'kefu',
    display_name: '小客服',
    identity_plane: 'tenant_a',
    is_tenant_admin: false,
    is_account_admin: false,
    roles: ['客服'],
    permissions: [],
    menu_permissions: nodes,
  }
  const wrapper = mount(KefuOrderForm, {
    props: { orderNo },
    attachTo: document.body,
    global: { plugins: [pinia] },
  })
  await flushPromises()
  return wrapper
}


/** 填最常用的三格并保存（项目框的选择器兼容 Task 4 前后的 input / textarea）。 */
async function fillRequiredAndSave(wrapper: VueWrapper): Promise<void> {
  await pickSelect(wrapper, '.kefu-game', 3)
  await flushPromises()
  await wrapper
    .find('textarea[name="project_text"], input[name="project_text"]')
    .setValue('王者荣耀/星耀上王者')
  await wrapper.find('input[name="total_price"]').setValue('88')
  const hallPrice = wrapper.find<HTMLInputElement>('input[name="hall_unit_price"]')
  if (hallPrice.exists() && !hallPrice.element.value) await hallPrice.setValue('54')
  await wrapper.find('.kefu-submit').trigger('click')
  await flushPromises()
}

beforeAll(() => {
  if (!('ResizeObserver' in globalThis)) {
    class ResizeObserverStub {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    }
    Object.defineProperty(globalThis, 'ResizeObserver', {
      value: ResizeObserverStub,
      configurable: true,
    })
  }
})

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset()
  api.fetchPeerDirectAssignment.mockResolvedValue({ studios: [], assignee: null, pending: null, price: null })
  api.fetchPeerDraftStudios.mockResolvedValue({ studios: [] })
  api.fetchGames.mockResolvedValue([GAME])
  api.fetchServers.mockResolvedValue([])
  api.listConfig.mockResolvedValue({ items: [PROJECT], total: 1, page: 1, page_size: 200 })
  api.fetchOrderSources.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 200 })
  api.fetchKefuOptions.mockResolvedValue({ items: [], truncated: false })
  api.fetchAssigneeOptions.mockResolvedValue({
    items: [{ id: 7, name: '打手甲', role: null, tag: '', remark: '' }],
    truncated: false,
    roles: [],
    defAssRecManUid: 0,
    defAssRecMan: '',
    defAssIsHall: false,
  })
  api.readPlatformOrder.mockResolvedValue(PLATFORM_FIELDS)
  api.fetchOrder.mockResolvedValue(orderFixture())
  api.createOrder.mockResolvedValue(orderFixture())
  api.updateOrder.mockResolvedValue(orderFixture())
  api.updateOrderWithMessage.mockResolvedValue({ order: orderFixture(), message: '' })
})

describe('小窗录单布局', () => {
  it('把来源与游戏、总价与销售客服各放同排，并只显示一个代练内容输入框', async () => {
    const wrapper = await mountForm()
    const source = wrapper.get('.kefu-order-source').element
    const game = wrapper.get('.kefu-game').element
    const sourceNumber = wrapper.get('input[name=source_order_no]').element
    const price = wrapper.get('input[name=total_price]').element
    const sales = wrapper.get('.kefu-sales-btn').element
    expect(source.closest('.kefu-pair')).toBe(game.closest('.kefu-pair'))
    expect(source.closest('.kefu-pair')).not.toBeNull()
    expect(source.compareDocumentPosition(sourceNumber) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(price.closest('.kefu-pair')).toBe(sales.closest('.kefu-pair'))
    expect(price.closest('.kefu-pair')).not.toBeNull()
    expect(wrapper.get('textarea[name=project_text]').isVisible()).toBe(true)
    expect(wrapper.find('textarea[name=content]').exists()).toBe(false)
    expect(wrapper.findAll('.kefu-fields-list')).toHaveLength(1)
    const more = wrapper.get('.kefu-more-toggle')
    expect(more.attributes('aria-expanded')).toBe('false')
    const qq = wrapper.get<HTMLInputElement>('input[name=customer_qq]')
    expect(qq.element.closest('.accordion-item-content')?.getAttribute('aria-hidden')).toBe('true')
    await more.trigger('click')
    expect(more.attributes('aria-expanded')).toBe('true')
    expect(qq.element.closest('.accordion-item-content')?.getAttribute('aria-hidden')).toBe('false')
    expect(qq.element.closest('.kefu-fields-list')).toBe(source.closest('.kefu-fields-list'))
    expect(wrapper.findAll('.kefu-seg-btn').map((button) => button.text())).toEqual([
      '内部抢单', '内部指派', '不派单',
    ])
  })
})

describe('录单表单 · 搬迁基线（旧页面 spec 删掉后仍要守的活代码）', () => {
  it('有大厅权限时每张新单默认选择内部抢单，并在保存时发大厅', async () => {
    const wrapper = await mountForm()
    expect(wrapper.get('.kefu-seg-btn').attributes('aria-pressed')).toBe('true')
    await wrapper.get('input[name="hall_unit_price"]').setValue('54')
    await fillRequiredAndSave(wrapper)
    expect(api.createOrder).toHaveBeenCalledWith(expect.objectContaining({ throw_to_hall: true, hall_price: '54' }))
  })
  it('无大厅发单权限时隐藏内部抢单并忽略旧的默认选项', async () => {
    api.fetchAssigneeOptions.mockResolvedValue({
      items: [], roles: [], truncated: false,
      defAssRecManUid: 0, defAssRecMan: '', defAssIsHall: true,
    })
    const wrapper = await mountForm(['order.add_edit', 'order.assign_taker'])

    const modes = wrapper.findAll('.kefu-seg-btn')
    expect(modes.map((mode) => mode.text())).toEqual(['内部指派', '不派单'])
    expect(modes[1]?.attributes('aria-pressed')).toBe('true')
    expect(wrapper.find('input[name="hall_unit_price"]').exists()).toBe(false)
  })

  it('内部抢单：保存载荷带 throw_to_hall / hall_price / remember_assignee', async () => {
    const wrapper = await mountForm()
    await pickSelect(wrapper, '.kefu-game', 3)
    await flushPromises()
    await wrapper
      .find('textarea[name="project_text"], input[name="project_text"]')
      .setValue('王者荣耀/星耀上王者')
    await wrapper.find('input[name="total_price"]').setValue('88')

    await wrapper.findAll('.kefu-seg-btn')[0]!.trigger('click')
    await flushPromises()
    await wrapper.find('input[name="hall_unit_price"]').setValue('88')
    await wrapper.find('input[name="remember_assignee"]').setValue(true)
    await wrapper.find('.kefu-submit').trigger('click')
    await flushPromises()

    const payload = api.createOrder.mock.calls[0]?.[0] as Record<string, unknown>
    expect(payload).toMatchObject({
      game: 3,
      project_text: '王者荣耀/星耀上王者',
      total_price: '88',
      throw_to_hall: true,
      hall_price: '88',
      remember_assignee: 'hall',
    })
    // 内部抢单与「指派给某人」互斥（后端收到两个键会在建单前回 1000）。
    expect(payload.assignee).toBeUndefined()
  })

  it('内部指派：保存载荷带 assignee；默认偏好为 0 时不发 remember_assignee', async () => {
    const wrapper = await mountForm()
    await pickSelect(wrapper, '.kefu-game', 3)
    await flushPromises()
    await wrapper
      .find('textarea[name="project_text"], input[name="project_text"]')
      .setValue('王者荣耀/星耀上王者')
    await wrapper.find('input[name="total_price"]').setValue('88')

    await wrapper.findAll('.kefu-seg-btn')[1]!.trigger('click')
    await flushPromises()
    await pickSelect(wrapper, '.kefu-assignee', 7)
    await flushPromises()
    await wrapper.find('input[name="hall_unit_price"]').setValue('50')
    await wrapper.find('.kefu-submit').trigger('click')
    await flushPromises()

    const payload = api.createOrder.mock.calls[0]?.[0] as Record<string, unknown>
    expect(payload.assignee).toBe(7)
    expect(payload.throw_to_hall).toBeUndefined()
    expect(payload.remember_assignee).toBeUndefined()
  })

  it.each([false, true])('首次指派失败仍操作原单（需要重选打手：%s）', async (reselect) => {
    api.fetchPeerDraftStudios.mockResolvedValue({ studios: [{ id: 11, name: '下家一' }] })
    api.searchPeerTakers.mockResolvedValue({ items: [{ account_name: '荣耀打手一', match_token: 'match-11' }] })
    api.createOrder.mockResolvedValue(orderFixture({ hall_price: '54.00' }))
    api.assignPeerTaker
      .mockRejectedValueOnce(new Error('下家暂时未响应'))
      .mockResolvedValueOnce({ account_name: '荣耀打手一', studio_name: '下家一' })
    if (reselect) {
      api.fetchPeerDirectAssignment.mockResolvedValue({
        studios: [{ id: 11, name: '下家一' }], pending: null, assignee: null, price: '54.00',
      })
    }
    const wrapper = await mountForm()
    await pickSelect(wrapper, '.kefu-game', 3)
    await wrapper.findAll('.kefu-seg-btn')[1]!.trigger('click')
    await wrapper.get('[aria-label="指派范围"] button:nth-child(2)').trigger('click')
    await flushPromises()
    await pickSelect(wrapper, '.kefu-peer-dispatch .kefu-picker-button', 11)
    await wrapper.get('input[placeholder="输入部分账号名称"]').setValue('荣耀')
    await wrapper.get('.kefu-peer-search button').trigger('click')
    await flushPromises()
    expect(api.searchPeerTakers).toHaveBeenCalledWith(undefined, { studio_id: 11, query: '荣耀', game_id: 3 })
    expect(api.assignPeerTaker).not.toHaveBeenCalled()
    await wrapper.get('.kefu-peer-results button').trigger('click')
    await fillRequiredAndSave(wrapper)

    expect(api.createOrder).toHaveBeenCalledOnce()
    expect(api.createOrder.mock.calls[0]?.[0]).not.toHaveProperty('throw_to_hall')
    expect(api.createOrder.mock.calls[0]?.[0]).not.toHaveProperty('assignee')
    expect(api.assignPeerTaker).toHaveBeenCalledExactlyOnceWith(ORDER_NO, {
      studio_id: 11, account_name: '荣耀打手一', match_token: 'match-11', expected_price: '54.00',
    })

    if (reselect) {
      api.searchPeerTakers.mockResolvedValue({ items: [{ account_name: '荣耀打手二', match_token: 'match-12' }] })
      await pickSelect(wrapper, '.kefu-peer-dispatch .kefu-picker-button', 11)
      await wrapper.get('input[placeholder="输入部分账号名称"]').setValue('荣耀')
      await wrapper.get('.kefu-peer-search button').trigger('click')
      await flushPromises()
      await wrapper.get('.kefu-peer-results button').trigger('click')
    }
    await wrapper.get('.kefu-submit').trigger('click')
    await flushPromises()
    expect(api.createOrder).toHaveBeenCalledOnce()
    expect(api.assignPeerTaker).toHaveBeenCalledTimes(2)
    expect(api.assignPeerTaker.mock.calls[1]?.[1].match_token).toBe(reselect ? 'match-12' : 'match-11')
    const searchCalls = api.searchPeerTakers.mock.calls
    expect(searchCalls[searchCalls.length - 1]?.[0]).toBe(reselect ? ORDER_NO : undefined)
    expect(wrapper.emitted('saved')).toEqual([[
      { orderNo: ORDER_NO, message: '录单成功；下家指派成功' },
    ]])
  })

  it('选择「不派单」时建单载荷不带任何派单键', async () => {
    const wrapper = await mountForm()
    await wrapper.findAll('.kefu-seg-btn')[2]!.trigger('click')
    await fillRequiredAndSave(wrapper)
    expect(api.createOrder).toHaveBeenCalledTimes(1)
    const payload = api.createOrder.mock.calls[0]?.[0] as Record<string, unknown>
    expect(payload).toMatchObject({
      game: 3,
      project_text: '王者荣耀/星耀上王者',
      total_price: '88',
    })
    expect(payload.assignee).toBeUndefined()
    expect(payload.throw_to_hall).toBeUndefined()
    expect(payload.remember_assignee).toBeUndefined()
    expect(payload.hall_price).toBeUndefined()
  })

  it('项目名称为空 ⇒ 不发请求、底部红字提示补填（不静默失败）', async () => {
    const wrapper = await mountForm()
    await pickSelect(wrapper, '.kefu-game', 3)
    await flushPromises()
    await wrapper.find('.kefu-submit').trigger('click')
    await flushPromises()

    expect(api.createOrder).not.toHaveBeenCalled()
    const alert = wrapper.find('.kefu-footer-hint[role="alert"]')
    expect(alert.exists()).toBe(true)
    // 只钉「拦住了 + 文案带字段语义」；Task 4 会把具体文案改成新标签
    // 「请填写代练内容」，届时由 Task 4 自己的精确用例接管，这条不用改。
    expect(alert.text()).toContain('请填写代练内容')
    expect(wrapper.find('.kefu-form').exists()).toBe(true)
  })

  it('没有保存权限 ⇒ 不画保存、仍有取消、红字说明（fail-closed）', async () => {
    const wrapper = await mountForm(['order.list'])
    expect(wrapper.find('.kefu-submit').exists()).toBe(false)
    expect(wrapper.find('.kefu-cancel').exists()).toBe(true)
    expect(wrapper.text()).toContain('没有保存订单的权限')
  })

  it('编辑态：PATCH 不含金额/分类/归属；换游戏时显式发 server/project = null', async () => {
    api.fetchGames.mockResolvedValue([GAME, { ...GAME, id: 4, name: '和平精英' }])
    const wrapper = await mountForm(ALL_NODES, ORDER_NO)
    await flushPromises()
    await pickSelect(wrapper, '.kefu-game', 4)
    await flushPromises()
    await wrapper.find('.kefu-submit').trigger('click')
    await flushPromises()

    // saveEdit 走带回信封 msg 的出口（H1 同族），载荷形状不变。
    expect(api.updateOrderWithMessage).toHaveBeenCalledTimes(1)
    const payload = api.updateOrderWithMessage.mock.calls[0]?.[1] as Record<string, unknown>
    expect(payload.game).toBe(4)
    expect(payload.project_text).toBe('星耀上王者')
    expect(payload.server).toBeNull()
    expect(payload.project).toBeNull()
    for (const key of [
      'total_price',
      'received_price',
      'platform_price',
      'hall_price',
      'refund_fee',
      'fee',
      'category',
      'sales_agent',
    ]) {
      expect(payload, `编辑载荷不应含 ${key}`).not.toHaveProperty(key)
    }
  })

  it('hasDraft：派单档位 / 指派打手 / 「默认指派此人」都算草稿（I10）', async () => {
    const hall = await mountForm()
    const hallVm = hall.vm as unknown as { hasDraft: () => boolean }
    expect(hallVm.hasDraft()).toBe(false)
    await hall.findAll('.kefu-seg-btn')[2]!.trigger('click')
    expect(hallVm.hasDraft()).toBe(true)

    const assign = await mountForm()
    const assignVm = assign.vm as unknown as { hasDraft: () => boolean }
    await assign.findAll('.kefu-seg-btn')[1]!.trigger('click')
    await flushPromises()
    await pickSelect(assign, '.kefu-assignee', 7)
    await flushPromises()
    expect(assignVm.hasDraft()).toBe(true)

    const remember = await mountForm()
    const rememberVm = remember.vm as unknown as { hasDraft: () => boolean }
    await remember.findAll('.kefu-seg-btn')[1]!.trigger('click')
    await flushPromises()
    await remember.find('input[name="remember_assignee"]').setValue(true)
    expect(rememberVm.hasDraft()).toBe(true)
  })
})

describe('自动识别平台订单', () => {
  function tidInput(wrapper: VueWrapper) {
    return wrapper.find('input[name="source_order_no"]')
  }

  it('只输入来源单号就自动识别并回填，无需离开输入框', async () => {
    api.readPlatformOrder.mockResolvedValue({
      ...PLATFORM_FIELDS,
      payment: '128.00',
      buyer_nick: '买家甲',
      order_source: 1,
      order_source_name: '淘宝',
    })
    const wrapper = await mountForm()
    expect(wrapper.find('.kefu-read-order').exists()).toBe(false)
    const input = wrapper.get<HTMLInputElement>('input[name="source_order_no"]')
    input.element.focus()
    input.element.value = 'TB2026102700001'
    await input.trigger('input')
    await vi.waitFor(() => expect(api.readPlatformOrder).toHaveBeenCalledExactlyOnceWith(null, 'TB2026102700001'))
    await flushPromises()
    expect(document.activeElement).toBe(input.element)
    expect(
      (wrapper.find('input[name="total_price"]').element as HTMLInputElement).value,
    ).toBe('128.00')
    expect(wrapper.text()).toContain('已读取平台订单并回填')
  })

  it('读完平台订单建单时把 buyer_eid / buyer_nick 一起回传', async () => {
    api.readPlatformOrder.mockResolvedValue({
      ...PLATFORM_FIELDS,
      buyer_eid: 'SYNTH-EID-0001',
      buyer_nick: '买家甲',
      order_source: 1,
      order_source_name: '东东大王',
    })
    const wrapper = await mountForm()
    const input = tidInput(wrapper)
    await input.setValue('TB2026102700001')
    await input.trigger('input')
    await vi.waitFor(() => expect(api.readPlatformOrder).toHaveBeenCalled())
    await flushPromises()
    await fillRequiredAndSave(wrapper)
    const payload = api.createOrder.mock.calls[0]?.[0]
    expect(payload).toMatchObject({
      buyer_eid: 'SYNTH-EID-0001',
      buyer_nick: '买家甲',
    })
  })

  it('录单时手动选新客/老客会随建单载荷发送；不选则不发', async () => {
    const wrapper = await mountForm()
    expect(wrapper.find('[data-kefu-customer-kind="none"]').exists()).toBe(true)
    await wrapper.find('[data-kefu-customer-kind="old"]').trigger('click')
    await flushPromises()
    await fillRequiredAndSave(wrapper)
    expect(api.createOrder.mock.calls[0]?.[0]).toMatchObject({ customer_kind_override: 'old' })

    api.createOrder.mockClear()
    const second = await mountForm()
    await fillRequiredAndSave(second)
    expect(api.createOrder.mock.calls[0]?.[0]).not.toHaveProperty('customer_kind_override')
  })

  it('平台商品标题不覆盖代练内容，也不写成另一份订单内容', async () => {
    api.readPlatformOrder.mockResolvedValue({ ...PLATFORM_FIELDS, remark: '平台商品标题甲' })
    const wrapper = await mountForm()
    await pickSelect(wrapper, '.kefu-game', 3)
    const input = wrapper.get<HTMLTextAreaElement>('textarea[name="project_text"]')
    const expected = '客服填写的代练内容'
    await input.setValue(expected)
    await tidInput(wrapper).setValue('TB2026102700001')
    await tidInput(wrapper).trigger('change')
    await flushPromises()
    expect(input.element.value).toBe(expected)
    expect(wrapper.find('textarea[name="content"]').exists()).toBe(false)
    await wrapper.findAll('.kefu-seg-btn')[2]!.trigger('click')
    await wrapper.get('.kefu-submit').trigger('click')
    await flushPromises()
    const payload = api.createOrder.mock.calls[0]?.[0]
    expect(payload).toMatchObject({ project_text: expected })
    expect(payload).not.toHaveProperty('content')
  })

  it('编辑代练内容时保留旧单另外保存的订单内容', async () => {
    api.fetchOrder.mockResolvedValue(orderFixture({ content: '原订单已有的详细内容' }))
    const wrapper = await mountForm(ALL_NODES, ORDER_NO)
    await wrapper.get('textarea[name="project_text"]').setValue('修改后的代练内容')
    await wrapper.get('.kefu-submit').trigger('click')
    await flushPromises()
    expect(api.updateOrderWithMessage).toHaveBeenCalledWith(ORDER_NO, expect.objectContaining({
      project_text: '修改后的代练内容',
    }))
    expect(api.updateOrderWithMessage.mock.calls[0]?.[1]).not.toHaveProperty('content')
    expect(wrapper.find('textarea[name="content"]').exists()).toBe(false)
  })

  it('来源单号不足 15 位 ⇒ 一次请求都不发，并给提示', async () => {
    const wrapper = await mountForm()
    await tidInput(wrapper).setValue('123')
    await tidInput(wrapper).trigger('change')
    await flushPromises()
    expect(api.readPlatformOrder).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('来源单号不足 15 位')
  })

  it('选具体来源 ⇒ 带该来源读；清空来源 ⇒ 不发请求', async () => {
    api.fetchOrderSources.mockResolvedValue({
      items: [{
        id: 2, name: '测试来源甲', remark: '', order_by: 0, shop_type: 0,
        group: null, group_name: '', created_at: '', updated_at: '',
      }],
      total: 1, page: 1, page_size: 200,
    })
    const wrapper = await mountForm()
    await tidInput(wrapper).setValue('TB2026102700001')
    await pickSelect(wrapper, '.kefu-order-source', 2)
    await flushPromises()
    expect(api.readPlatformOrder).toHaveBeenCalledWith(2, 'TB2026102700001')

    api.readPlatformOrder.mockClear()
    await pickSelect(wrapper, '.kefu-order-source', null)
    await flushPromises()
    expect(api.readPlatformOrder).not.toHaveBeenCalled()
  })

  it('同一 (单号, 来源) 成功过 ⇒ 不重发；改单号才重发（去重）', async () => {
    const wrapper = await mountForm()
    await tidInput(wrapper).setValue('TB2026102700001')
    await tidInput(wrapper).trigger('change')
    await flushPromises()
    expect(api.readPlatformOrder).toHaveBeenCalledTimes(1)

    await tidInput(wrapper).trigger('change')
    await flushPromises()
    expect(api.readPlatformOrder).toHaveBeenCalledTimes(1)

    await tidInput(wrapper).setValue('TB2026102700002')
    await tidInput(wrapper).trigger('change')
    await flushPromises()
    expect(api.readPlatformOrder).toHaveBeenCalledTimes(2)
  })

  it('识别 latest-wins：旧响应回来不覆盖新单号读到的表单', async () => {
    const slow = deferred<PlatformOrderFields>()
    api.readPlatformOrder
      .mockReturnValueOnce(slow.promise)
      .mockResolvedValueOnce({ ...PLATFORM_FIELDS, payment: '200.00' })
    const wrapper = await mountForm()

    await tidInput(wrapper).setValue('TB2026102700001')
    await tidInput(wrapper).trigger('change')
    await flushPromises()
    await tidInput(wrapper).setValue('TB2026102700002')
    await tidInput(wrapper).trigger('change')
    await flushPromises()

    slow.resolve({ ...PLATFORM_FIELDS, payment: '100.00' })
    await flushPromises()
    expect(
      (wrapper.find('input[name="total_price"]').element as HTMLInputElement).value,
    ).toBe('200.00')
  })

  it('订单号改短时丢弃已发出的识别结果，不覆盖手填价格', async () => {
    const read = deferred<PlatformOrderFields>()
    api.readPlatformOrder.mockReturnValueOnce(read.promise)
    const wrapper = await mountForm()
    const input = wrapper.get<HTMLInputElement>('input[name="source_order_no"]')
    await input.setValue('TB2026102700001')
    expect(api.readPlatformOrder).toHaveBeenCalledOnce()
    input.element.value = 'TB20'
    await input.trigger('input')
    const price = wrapper.get<HTMLInputElement>('input[name="total_price"]')
    await price.setValue('88')
    read.resolve({ ...PLATFORM_FIELDS, payment: '100.00' })
    await flushPromises()
    expect(price.element.value).toBe('88')
    expect(api.readPlatformOrder).toHaveBeenCalledOnce()
  })

  it('识别中显示「识别中…」，没有按钮（M/§2.3）', async () => {
    const slow = deferred<PlatformOrderFields>()
    api.readPlatformOrder.mockReturnValue(slow.promise)
    const wrapper = await mountForm()
    await tidInput(wrapper).setValue('TB2026102700001')
    await tidInput(wrapper).trigger('change')
    expect(wrapper.text()).toContain('识别中…')
    expect(wrapper.find('.kefu-read-order').exists()).toBe(false)
    slow.resolve(PLATFORM_FIELDS)
    await flushPromises()
    expect(wrapper.text()).not.toContain('识别中…')
  })

  it('1015 / 1028 ⇒ 原样显示后端 msg（不包一层「读取失败」）', async () => {
    const wrapper = await mountForm()
    api.readPlatformOrder.mockRejectedValueOnce(new BizError(1015, '这家店没配对，去后台配置'))
    await tidInput(wrapper).setValue('TB2026102700001')
    await tidInput(wrapper).trigger('change')
    await flushPromises()
    expect(wrapper.text()).toContain('这家店没配对，去后台配置')

    api.readPlatformOrder.mockRejectedValueOnce(new BizError(1028, '订单还没付款，稍后再试'))
    await tidInput(wrapper).setValue('TB2026102700002')
    await tidInput(wrapper).trigger('change')
    await flushPromises()
    expect(wrapper.text()).toContain('订单还没付款，稍后再试')
  })

  it('1016 / 1017 ⇒ 只说「没读到平台订单，可手动填写」', async () => {
    const wrapper = await mountForm()
    for (const [index, code] of [1016, 1017].entries()) {
      api.readPlatformOrder.mockRejectedValueOnce(new BizError(code, '上游内部错误'))
      await tidInput(wrapper).setValue(`TB202610270000${index}`)
      await tidInput(wrapper).trigger('change')
      await flushPromises()
      expect(wrapper.text()).toContain('没读到平台订单，可手动填写')
      expect(wrapper.text()).not.toContain('上游内部错误')
    }
  })

  it('「重新识别」：成功过也能强制作废去重键再读（I9）', async () => {
    const wrapper = await mountForm()
    await tidInput(wrapper).setValue('TB2026102700001')
    await tidInput(wrapper).trigger('change')
    await flushPromises()
    expect(api.readPlatformOrder).toHaveBeenCalledTimes(1)

    await tidInput(wrapper).trigger('change')
    await flushPromises()
    expect(api.readPlatformOrder).toHaveBeenCalledTimes(1)

    await wrapper.find('.kefu-read-retry').trigger('click')
    await flushPromises()
    expect(api.readPlatformOrder).toHaveBeenCalledTimes(2)
  })
})

describe('识别后自动选游戏', () => {
  it('商品内容里的游戏名 ⇒ 自动切游戏并加载该游戏的常用项目', async () => {
    api.fetchPeerDirectAssignment.mockResolvedValue({ studios: [], assignee: null, pending: null, price: null })
  api.fetchPeerDraftStudios.mockResolvedValue({ studios: [] })
  api.fetchGames.mockResolvedValue([GAME])
    api.listConfig.mockResolvedValue({
      items: [{ id: 9, name: '星耀上王者', sort_order: 1, is_active: true, created_at: '' }],
      total: 1,
      page: 1,
      page_size: 200,
    })
    api.readPlatformOrder.mockResolvedValue({
      ...PLATFORM_FIELDS,
      remark: '王者荣耀 钻石→星耀',
    })
    const wrapper = await mountForm()
    const input = wrapper.find('input[name="source_order_no"]')
    await input.setValue('TB2026102700001')
    await input.trigger('change')
    await flushPromises()

    expect(wrapper.text()).toContain('已按商品内容自动选择游戏「王者荣耀」')
    expect(api.listConfig).toHaveBeenCalledWith('projects', { game: 3, page_size: 200 })
  })

  it('商品内容出现多款游戏 ⇒ 不切，提示手动选', async () => {
    api.fetchGames.mockResolvedValue([
      { ...GAME, id: 3 },
      { ...GAME, id: 4, name: '和平精英' },
    ])
    api.readPlatformOrder.mockResolvedValue({
      ...PLATFORM_FIELDS,
      remark: '王者荣耀 / 和平精英',
    })
    const wrapper = await mountForm()
    const input = wrapper.find('input[name="source_order_no"]')
    await input.setValue('TB2026102700001')
    await input.trigger('change')
    await flushPromises()

    expect(wrapper.text()).toContain('出现了多款游戏')
    expect(wrapper.text()).not.toContain('自动选择游戏')
  })

  it('商品内容没提游戏 ⇒ 静默（不提示、不切）', async () => {
    api.readPlatformOrder.mockResolvedValue({ ...PLATFORM_FIELDS, remark: '随便写点什么' })
    const wrapper = await mountForm()
    const input = wrapper.find('input[name="source_order_no"]')
    await input.setValue('TB2026102700001')
    await input.trigger('change')
    await flushPromises()
    expect(wrapper.text()).toContain('已读取平台订单并回填')
    expect(wrapper.text()).not.toContain('自动选择游戏')
    expect(wrapper.text()).not.toContain('多款游戏')
  })
})

/** `KefuOrderForm.vue` 的源文件：样式规则要**从真源里读**，⛔ 不是把值抄成第二份常量。 */
async function mountFormWithGame(count = 2): Promise<VueWrapper> {
  const items: ConfigRow[] = Array.from({ length: count }, (_, i) => ({
    id: i + 1,
    name: `项目${i + 1}`,
    sort_order: i + 1,
    is_active: true,
    created_at: '',
  }))
  api.listConfig.mockResolvedValue({ items, total: count, page: 1, page_size: 200 })
  const wrapper = await mountForm()
  await pickSelect(wrapper, '.kefu-game', 3)
  await flushPromises()
  return wrapper
}

describe('常用项目块与折叠', () => {
  it('点一下拼进「代练项目/标题」，再点删掉', async () => {
    const wrapper = await mountFormWithGame()
    await wrapper.findAll('.kefu-project-chip')[0]!.trigger('click')
    const value = () =>
      (wrapper.find('textarea[name="project_text"]').element as HTMLTextAreaElement).value
    expect(value()).toBe('项目1')
    await wrapper.findAll('.kefu-project-chip')[0]!.trigger('click')
    expect(value()).toBe('')
  })

  it('跨行项目默认折叠，展开可选择第二行，收起后仍可取消选中项', async () => {
    const rectangles = vi.spyOn(HTMLElement.prototype, 'offsetTop', 'get')
      .mockImplementation(function (this: HTMLElement) {
        const elements = this.parentElement?.children
        const index = elements ? Array.from(elements).indexOf(this) : 0
        return index < 3 ? 0 : 32
      })
    try {
      const wrapper = await mountFormWithGame(5)
      const visibleChips = () => wrapper.findAll('.kefu-project-chip:not([aria-hidden="true"])')
      const toggle = () => wrapper.get('.kefu-projects-toggle')
      expect(visibleChips().map((item) => item.text())).toEqual(['项目1', '项目2', '项目3'])
      expect(toggle().text()).toBe('展开(2)')

      await toggle().trigger('click')
      expect(toggle().text()).toBe('收起')
      expect(visibleChips()).toHaveLength(5)
      await visibleChips()[4]!.trigger('click')

      const list = wrapper.get<HTMLElement>('.kefu-project-chips').element
      list.scrollTop = 32
      await toggle().trigger('click')
      expect(list.scrollTop).toBe(0)
      expect(visibleChips().map((item) => item.text())).toEqual(['项目5', '项目1', '项目2'])
      await visibleChips()[0]!.trigger('click')
      expect(wrapper.get<HTMLTextAreaElement>('textarea[name="project_text"]').element.value).toBe('')
    } finally {
      rectangles.mockRestore()
    }
  })

  it('代练内容用带标签的必填多行输入框', async () => {
    const wrapper = await mountForm()
    const input = wrapper.get('textarea[name="project_text"]')
    expect((input.element as HTMLTextAreaElement).required).toBe(true)
    expect(wrapper.get(`label[for="${input.attributes('id')}"]`).text()).toContain('代练内容')
  })

  it('常用项目：取消勾选按整词删——前缀同名（星耀 / 星耀上王者）不会误删', async () => {
    api.listConfig.mockResolvedValue({
      items: [
        { ...PROJECT, id: 31, name: '星耀' },
        { ...PROJECT, id: 32, name: '星耀上王者' },
      ],
      total: 2,
      page: 1,
      page_size: 200,
    })
    const wrapper = await mountForm()
    await pickSelect(wrapper, '.kefu-game', 3)
    await flushPromises()

    await wrapper.findAll('.kefu-project-chip')[0]!.trigger('click')
    await wrapper.findAll('.kefu-project-chip')[1]!.trigger('click')
    const value = () =>
      (wrapper.find('textarea[name="project_text"]').element as HTMLTextAreaElement).value
    expect(value()).toBe('星耀/星耀上王者')

    await wrapper.findAll('.kefu-project-chip')[0]!.trigger('click')
    expect(value()).toBe('星耀上王者')
  })

  it('项目名称为空 ⇒ 红字用新标签「请填写代练内容」（M8）', async () => {
    const wrapper = await mountForm()
    await pickSelect(wrapper, '.kefu-game', 3)
    await flushPromises()
    await wrapper.find('.kefu-submit').trigger('click')
    await flushPromises()
    expect(api.createOrder).not.toHaveBeenCalled()
    expect(wrapper.find('.kefu-footer-hint[role="alert"]').text()).toContain(
      '请填写代练内容',
    )
  })
})

describe('接单销售客服按钮', () => {
  it('默认显示「我自己」；选别人后保存载荷带 sales_user', async () => {
    api.fetchKefuOptions.mockResolvedValue({
      items: [
        { id: 7, name: '客服小九' },
        { id: 8, name: '客服阿甲' },
      ],
      truncated: false,
    })
    const wrapper = await mountForm()
    expect(wrapper.get('.kefu-sales-btn').text()).toContain('我自己')

    await wrapper.find('.kefu-sales-btn').trigger('click')
    await flushPromises()
    await popupOption(8).trigger('click')
    await flushPromises()
    expect(wrapper.get('.kefu-sales-btn').text()).toBe('客服阿甲')

    await fillRequiredAndSave(wrapper)
    expect(api.createOrder).toHaveBeenCalledTimes(1)
    expect(api.createOrder.mock.calls[0]?.[0]).toEqual(
      expect.objectContaining({ sales_user: 8 }),
    )
  })

  it('选回自己 ⇒ 保存载荷不带 sales_user', async () => {
    api.fetchKefuOptions.mockResolvedValue({
      items: [
        { id: 7, name: '客服小九' },
        { id: 8, name: '客服阿甲' },
      ],
      truncated: false,
    })
    const wrapper = await mountForm()
    await wrapper.find('.kefu-sales-btn').trigger('click')
    await flushPromises()
    await popupOption(8).trigger('click')
    await flushPromises()

    await wrapper.find('.kefu-sales-btn').trigger('click')
    await flushPromises()
    await popupOption(null).trigger('click')
    await flushPromises()
    await fillRequiredAndSave(wrapper)

    expect(api.createOrder.mock.calls[0]?.[0]).not.toHaveProperty('sales_user')
  })

  it('没有 order.allow_change_sales_agent ⇒ 不画按钮，只显示本人', async () => {
    const wrapper = await mountForm(['order.add_edit'])
    expect(wrapper.find('.kefu-sales-btn').exists()).toBe(false)
    expect(wrapper.get('.kefu-readonly').text()).toBe('我自己')
  })

  it('候选加载失败 ⇒ 仍可选自己，并提示只能选自己', async () => {
    api.fetchKefuOptions.mockRejectedValue(new Error('名单炸了'))
    const wrapper = await mountForm()
    await wrapper.find('.kefu-sales-btn').trigger('click')
    await flushPromises()
    expect(document.querySelector('.popup.modal-in')?.textContent).toContain('候选加载失败，只能选自己')
    await popupOption(null).trigger('click')
    await flushPromises()
    expect(wrapper.get('.kefu-sales-btn').text()).toContain('我自己')
  })

  it('候选还没回来 ⇒ 显示「候选加载中…」，至少能选自己（M10）', async () => {
    const slow = deferred<{ items: { id: number; name: string }[]; truncated: boolean }>()
    api.fetchKefuOptions.mockReturnValue(slow.promise)
    const wrapper = await mountForm()
    await wrapper.find('.kefu-sales-btn').trigger('click')
    await flushPromises()
    expect(document.querySelector('.popup.modal-in')?.textContent).toContain('候选加载中…')
    expect(popupOption(null).exists()).toBe(true)
    slow.resolve({ items: [], truncated: false })
    await flushPromises()
    expect(document.querySelector('.popup.modal-in')?.textContent).not.toContain('候选加载中…')
  })
})

describe('新单截图上传', () => {
  beforeAll(() => {
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:order-screenshot')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    vi.stubGlobal('Image', class { src = ''; decode(): Promise<void> { return Promise.resolve() } })
  })
  afterAll(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

  const png = Uint8Array.from(atob(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLttAAAAABJRU5ErkJggg==',
  ), (char) => char.charCodeAt(0))
  const ticket = {
    screenshot_id: 77, key: 'test/order-shot.png', upload_url: '/local/test.png', expires_in: 900,
  }

  async function chooseScreenshot(wrapper: VueWrapper, names = ['测试截图.png']): Promise<void> {
    const input = wrapper.get('input[type="file"]')
    Object.defineProperty(input.element, 'files', {
      value: names.map((name) => new File([png], name, { type: 'image/png' })),
    })
    await input.trigger('change')
    await vi.waitFor(() => expect(wrapper.findAll('.kefu-shot-previews img')).toHaveLength(names.length))
  }

  it('内部抢单发出后挂截图，认领重试只补图片，仍是同一张订单', async () => {
    api.requestScreenshotTicket.mockResolvedValue(ticket)
    api.claimScreenshot.mockRejectedValueOnce(new Error('测试网络中断')).mockResolvedValue({ id: 77 })
    const wrapper = await mountForm()
    await chooseScreenshot(wrapper)
    await wrapper.get('.kefu-seg-btn').trigger('click')
    await wrapper.get('input[name="hall_unit_price"]').setValue('54')
    await fillRequiredAndSave(wrapper)

    expect(api.createOrder).toHaveBeenCalledOnce()
    expect(api.createOrder.mock.calls[0]?.[0]).toMatchObject({ throw_to_hall: true, hall_price: '54' })
    expect(api.requestScreenshotTicket).toHaveBeenCalledExactlyOnceWith(ORDER_NO, 'png', 0)
    expect(api.putFileToStorage).toHaveBeenCalledExactlyOnceWith(
      ticket.upload_url, expect.any(File), expect.any(AbortSignal),
    )
    expect(api.claimScreenshot).toHaveBeenCalledExactlyOnceWith(ORDER_NO, {
      screenshot_id: ticket.screenshot_id, original_name: '测试截图.png', remark: '',
    })
    expect(wrapper.get('.kefu-submit').text()).toBe('重试上传截图')
    expect(wrapper.get('[role="alert"]').text()).toContain('录单成功，已发内部大厅')
    expect(wrapper.emitted('saved')).toBeUndefined()

    await wrapper.get('.kefu-submit').trigger('click')
    await flushPromises()
    expect(api.createOrder).toHaveBeenCalledOnce()
    expect(api.requestScreenshotTicket).toHaveBeenCalledOnce()
    expect(api.putFileToStorage).toHaveBeenCalledOnce()
    expect(api.claimScreenshot).toHaveBeenCalledTimes(2)
    expect(wrapper.emitted('saved')).toEqual([[
      { orderNo: ORDER_NO, message: '录单成功，已发内部大厅；截图已上传' },
    ]])
  })

  it('编辑补传多张截图，重试第二张时不重复改订单或上传第一张', async () => {
    api.requestScreenshotTicket
      .mockResolvedValueOnce(ticket)
      .mockResolvedValueOnce({ ...ticket, screenshot_id: 78, upload_url: '/local/second.png' })
    api.claimScreenshot
      .mockResolvedValueOnce({ id: 77 })
      .mockRejectedValueOnce(new Error('第二张未确认'))
      .mockResolvedValueOnce({ id: 78 })
    const wrapper = await mountForm(ALL_NODES, ORDER_NO)
    await chooseScreenshot(wrapper, ['第一张.png', '第二张.png'])
    await wrapper.get('.kefu-submit').trigger('click')
    await flushPromises()

    expect(api.updateOrderWithMessage).toHaveBeenCalledOnce()
    expect(api.requestScreenshotTicket).toHaveBeenCalledTimes(2)
    expect(wrapper.findAll('.kefu-shot-previews img')).toHaveLength(1)
    expect(wrapper.get('.kefu-shot-previews img').attributes('title')).toBe('第二张.png')
    expect(wrapper.get('.kefu-submit').text()).toBe('重试上传截图')

    await wrapper.get('.kefu-submit').trigger('click')
    await flushPromises()
    expect(api.updateOrderWithMessage).toHaveBeenCalledOnce()
    expect(api.requestScreenshotTicket).toHaveBeenCalledTimes(2)
    expect(api.putFileToStorage).toHaveBeenCalledTimes(2)
    expect(api.claimScreenshot.mock.calls.map(([_, claim]) => claim.original_name)).toEqual([
      '第一张.png', '第二张.png', '第二张.png',
    ])
    expect(wrapper.emitted('saved')).toEqual([[
      { orderNo: ORDER_NO, message: '保存成功；截图已上传' },
    ]])
  })

  it('发内部大厅失败且订单已存在时，仍给该订单挂图后提示补发', async () => {
    api.createOrder.mockRejectedValue(new BizError(1023, '单已建好，发大厅失败', { order_no: ORDER_NO }))
    api.requestScreenshotTicket.mockResolvedValue(ticket)
    api.claimScreenshot.mockResolvedValue({ id: 77 })
    const wrapper = await mountForm()
    await chooseScreenshot(wrapper)
    await wrapper.get('.kefu-seg-btn').trigger('click')
    await wrapper.get('input[name="hall_unit_price"]').setValue('54')
    await fillRequiredAndSave(wrapper)

    expect(api.claimScreenshot).toHaveBeenCalledWith(ORDER_NO, expect.objectContaining({ screenshot_id: 77 }))
    expect(wrapper.emitted('createdFailed')).toEqual([[
      '单已建好，发大厅失败；请到团团通后台重试；截图已上传',
    ]])
  })
})

describe('编辑保存出口（2026-10-29：updateOrderWithMessage + 信封 msg）', () => {
  it('saveEdit 走带回信封的出口；老出口 updateOrder 一个都不发', async () => {
    const wrapper = await mountForm(ALL_NODES, ORDER_NO)
    await wrapper.find('.kefu-submit').trigger('click')
    await flushPromises()
    expect(api.updateOrderWithMessage).toHaveBeenCalledTimes(1)
    expect(api.updateOrderWithMessage.mock.calls[0]?.[0]).toBe(ORDER_NO)
    expect(api.updateOrder).not.toHaveBeenCalled()
  })

  it('信封 msg 为空 ⇒ 回退「保存成功」；msg 非空 ⇒ 原样带出（H1：成功码里的警告不许吞）', async () => {
    api.updateOrderWithMessage.mockResolvedValue({ order: orderFixture(), message: '' })
    const wrapper = await mountForm(ALL_NODES, ORDER_NO)
    await wrapper.find('.kefu-submit').trigger('click')
    await flushPromises()
    expect(wrapper.emitted('saved')).toEqual([[{ orderNo: ORDER_NO, message: '保存成功' }]])

    api.updateOrderWithMessage.mockResolvedValue({
      order: orderFixture(),
      message: '保存成功；代练通订单未能撤回（对方已下架），请到「第三方平台订单管理」处理',
    })
    const savesBefore = wrapper.emitted('saved')?.length ?? 0
    await wrapper.find('.kefu-submit').trigger('click')
    await flushPromises()
    expect(wrapper.emitted('saved')?.[savesBefore]).toEqual([
      {
        orderNo: ORDER_NO,
        message: '保存成功；代练通订单未能撤回（对方已下架），请到「第三方平台订单管理」处理',
      },
    ])
  })
})

/**
 * ⭐ 「代练项目」700 字节同步上限闸（2026-10-15 业务方裁定 E-0242；本页 2026-10-29 补上）。
 * 边界样本与文案**逐字照** `order-addorder.spec.ts` 的用例形态：233 个汉字 = 699 B（UTF-8
 * 中文一字符 3 字节），+ 1 个 ASCII = 700 B（≤700 放行的右边界），+ 2 个 = 701 B（最小超限样本）。
 */
describe('录单/编辑两态：代练项目 700 字节同步上限闸（E-0242）', () => {
  const OVER_LIMIT_MESSAGE =
    '代练项目超出鸡蛋铺同步上限（当前 701 字节，上限 700 字节），请精简后再保存；完整细节可放订单截图'

  it('录单态：701 字节 ⇒ 不发建单、逐字上限文案；恰好 700 字节放行且载荷带原文', async () => {
    const wrapper = await mountForm()
    await pickSelect(wrapper, '.kefu-game', 3)
    await flushPromises()
    await wrapper.find('input[name="total_price"]').setValue('88')
    await wrapper.get('input[name="hall_unit_price"]').setValue('54')
    await wrapper.find('textarea[name="project_text"]').setValue('深'.repeat(233) + 'ab')
    await wrapper.find('.kefu-submit').trigger('click')
    await flushPromises()

    expect(api.createOrder).not.toHaveBeenCalled()
    expect(wrapper.find('.kefu-footer-hint[role="alert"]').text()).toContain(OVER_LIMIT_MESSAGE)

    // 边界左：恰好 700 B 放行——POST 真的发出、载荷带原文（⛔ 不是静默截断后再发）。
    await wrapper.find('textarea[name="project_text"]').setValue('深'.repeat(233) + 'a')
    await wrapper.find('.kefu-submit').trigger('click')
    await flushPromises()
    expect(api.createOrder).toHaveBeenCalledTimes(1)
    expect((api.createOrder.mock.calls[0]?.[0] as Record<string, unknown>).project_text).toBe(
      '深'.repeat(233) + 'a',
    )
  })

  it('编辑态：701 字节 ⇒ 一个 PATCH 都不发、逐字上限文案', async () => {
    const wrapper = await mountForm(ALL_NODES, ORDER_NO)
    await wrapper.find('textarea[name="project_text"]').setValue('深'.repeat(233) + 'ab')
    await wrapper.find('.kefu-submit').trigger('click')
    await flushPromises()

    expect(api.updateOrderWithMessage).not.toHaveBeenCalled()
    expect(wrapper.find('.kefu-footer-hint[role="alert"]').text()).toContain(OVER_LIMIT_MESSAGE)
  })
})
