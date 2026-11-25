// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import V2Approvals from '../V2Approvals.vue'
import V2ApprovalEvidence from '../V2ApprovalEvidence.vue'
import V2OrderDetailPane from '../V2OrderDetailPane.vue'
import V2PenaltyDialog from '../V2PenaltyDialog.vue'
import type { Approval } from '@/api/approvals'

const mocks = vi.hoisted(() => ({
  options: vi.fn(), list: vi.fn(), targets: vi.fn(), submit: vi.fn(), decide: vi.fn(),
  order: vi.fn(), audit: vi.fn(), searchOrders: vi.fn(), sources: vi.fn(), ticket: vi.fn(), upload: vi.fn(), compress: vi.fn(), canNode: vi.fn(),
}))
vi.mock('@/api/approvals', () => ({
  fetchApprovalOptions: mocks.options,
  fetchApprovals: mocks.list,
  fetchApprovalTargets: mocks.targets,
  submitPenalty: mocks.submit,
  decideApproval: mocks.decide,
  requestApprovalImageTicket: mocks.ticket,
}))
vi.mock('@/api/config', () => ({
  fetchGames: async () => [{ id: 7, name: '测试游戏' }],
  fetchServers: async () => [{ id: 8, name: '测试区服' }],
  fetchCategories: async () => [],
  fetchProjects: async () => [],
}))
vi.mock('@/api/orderSources', () => ({ fetchAllOrderSources: mocks.sources }))
vi.mock('@/api/orders', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/orders')>(),
  fetchOrder: mocks.order,
  fetchOrderAudit: mocks.audit,
  fetchOrders: mocks.searchOrders,
}))
vi.mock('@/stores/session', () => ({ useSessionStore: () => ({ canNode: mocks.canNode }) }))
vi.mock('@/layui-island/directUpload', () => ({ putFileToStorage: mocks.upload }))
vi.mock('@/layui-island/imageCompress', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/layui-island/imageCompress')>(),
  compressImage: mocks.compress,
}))

const stubs = {
  OrderTabDialog: { template: '<section><slot /></section>' },
  V2OrderDetailPane: { props: ['orderNo', 'viewNo'], template: '<div />' },
}
const originalNo = '00000000000000001234'
const approval: Approval = {
  id: 9,
  kind: 'refund',
  status: 'pending',
  original_order_no: originalNo,
  generated_order_no: null,
  initiator_name: '系统',
  approver_name: '退款处理人甲',
  target_name: '',
  target_type: null,
  amount: null,
  reason: '代练通已接手',
  objection: '',
  remark: '',
  comment: '',
  created_at: '2026-11-15T12:00:00+08:00',
  decided_at: null,
  images: [],
  cc_names: [],
  can_decide: true,
  requires_assignment: false,
  order: {},
}

beforeEach(() => {
  vi.resetAllMocks()
  mocks.options.mockResolvedValue({ can_apply: true, can_manage: false, approvers: [{ id: 41, name: '组长甲' }], available_cc: [{ id: 42, name: '抄送甲' }, { id: 43, name: '抄送乙' }], default_cc_ids: [42, 99] })
  mocks.list.mockResolvedValue({ items: [], total: 0 })
  mocks.canNode.mockReturnValue(true)
  mocks.order.mockResolvedValue({
    order_no: originalNo, game: 7, server: 8, project_text: '合成项目', order_source: null,
    content: '原单客户需求', demand: '原单项目说明', time_requirement: '八小时',
    total_price: '195.00', received_price: '100.00', platform_price: '89.00', fee: '22.00',
    customer_qq: '原单联系标记', game_sub_account: '原单子账号',
  })
  mocks.searchOrders.mockResolvedValue({ items: [{ order_no: originalNo, source_order_no: '合成来源单' }], total: 1 })
  mocks.sources.mockResolvedValue([{ id: 11, name: '测试来源' }])
  mocks.targets.mockResolvedValue({ items: [{ id: 18, name: '测试打手甲' }], suggested: { id: 18, name: '测试打手甲' } })
  mocks.submit.mockResolvedValue({})
  mocks.compress.mockImplementation(async (file: File) => file)
  mocks.upload.mockResolvedValue(undefined)
})

afterEach(() => vi.unstubAllGlobals())

describe('审批中心', () => {
  it('发起审批先选择现有模板，再打开罚款弹窗', async () => {
    const wrapper = mount(V2Approvals, { global: { stubs } })
    await flushPromises()
    await wrapper.get('[data-v2="start-approval"]').trigger('click')
    expect(wrapper.findComponent(V2PenaltyDialog).exists()).toBe(false)
    const templates = wrapper.get('[aria-label="审批模板"]')
    expect(templates.findAll('button')).toHaveLength(1)
    await templates.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.getComponent(V2PenaltyDialog).props('originalOrderNo')).toBeUndefined()
    expect(wrapper.find('[aria-label="审批模板"]').exists()).toBe(false)
    expect(mocks.order).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('在审批内查看关联订单、罚款订单和证据截图，订单入口遵守订单详情权限', async () => {
    const generatedNo = '20261116000000000002'
    const imageUrl = 'https://example.invalid/evidence.png'
    mocks.list.mockResolvedValue({ items: [{
      ...approval, kind: 'penalty', status: 'approved', can_decide: false, generated_order_no: generatedNo,
      images: [{ url: imageUrl }], amount: '12.00', reason: '超过约定时间',
    }], total: 1 })
    const wrapper = mount(V2Approvals, { global: { stubs } })
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text() === '查看')!.trigger('click')
    const orders = wrapper.findAll('.approval-detail button[aria-haspopup="dialog"]')
    expect(orders.map((button) => button.text())).toEqual([originalNo, generatedNo])
    expect(wrapper.find('.approval-detail a[href^="/orders/"]').exists()).toBe(false)
    expect(mocks.canNode).toHaveBeenCalledWith(['order.view_detail', 'order.view_content'])
    await orders[0]!.trigger('click')
    expect(wrapper.getComponent(V2OrderDetailPane).props('orderNo')).toBe(originalNo)
    await orders[1]!.trigger('click')
    expect(wrapper.getComponent(V2OrderDetailPane).props('orderNo')).toBe(generatedNo)
    await wrapper.get('button[aria-label="放大查看 证据截图 1"]').trigger('click')
    expect(wrapper.get('[title="图片预览"] img').attributes('src')).toBe(imageUrl)
    wrapper.unmount()

    mocks.canNode.mockReturnValue(false)
    const denied = mount(V2Approvals, { global: { stubs } })
    await flushPromises()
    await denied.findAll('button').find((button) => button.text() === '查看')!.trigger('click')
    expect(denied.get('.approval-detail').text()).toContain(originalNo)
    expect(denied.find('.approval-detail button[aria-haspopup="dialog"]').exists()).toBe(false)
    denied.unmount()
  })

  it('审批详情使用来源单号，日志加载失败时仍可看订单内容', async () => {
    mocks.list.mockResolvedValue({ items: [{ ...approval, can_decide: false }], total: 1 })
    mocks.order.mockResolvedValue({ source_order_no: '平台原单-123', project_text: '原单项目' })
    mocks.audit.mockRejectedValue(new Error('日志加载失败'))
    const wrapper = mount(V2Approvals, { global: { stubs: {
      OrderTabDialog: stubs.OrderTabDialog,
      V2DetailFace: { template: '<div><slot /></div>' },
      ElInput: true,
    } } })
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text() === '查看')!.trigger('click')
    await wrapper.get('.approval-detail button[aria-haspopup="dialog"]').trigger('click')
    await flushPromises()
    const dialog = wrapper.get('[data-v2="approval-order-detail"]')
    expect(dialog.text()).toContain('平台原单-123')
    expect(dialog.text()).toContain('原单项目')
    expect(dialog.get('[role="alert"]').text()).toBe('无法读取订单日志')
    wrapper.unmount()
  })

  it('审批已指派时可处理退款，账号未获订单详情权限时只显示单号', async () => {
    mocks.options.mockResolvedValue({ can_apply: false, can_manage: false, approvers: [], available_cc: [], default_cc_ids: [] })
    mocks.canNode.mockReturnValue(false)
    mocks.list.mockResolvedValueOnce({ items: [approval], total: 1 })
    mocks.decide.mockResolvedValue({ ...approval, status: 'approved', can_decide: false })
    const wrapper = mount(V2Approvals, { global: { stubs } })
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text() === '处理')!.trigger('click')
    expect(wrapper.get('.approval-detail').text()).toContain(originalNo)
    expect(wrapper.find('.approval-detail button[aria-haspopup="dialog"]').exists()).toBe(false)
    await wrapper.findAll('button').find((button) => button.text() === '标记已处理')!.trigger('click')
    await flushPromises()
    expect(mocks.decide).toHaveBeenCalledWith(approval.id, 'approved', '')
    expect(mocks.order).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('处理结果已保存')
    wrapper.unmount()
  })

  it('等待审批结果时，审批详情暂时禁止打开关联订单', async () => {
    mocks.list.mockResolvedValue({ items: [approval], total: 1 })
    let finishDecision!: (value: Approval) => void
    mocks.decide.mockReturnValue(new Promise<Approval>(resolve => { finishDecision = resolve }))
    const wrapper = mount(V2Approvals, { global: { stubs } })
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '处理')!.trigger('click')
    await wrapper.findAll('button').find(button => button.text() === '标记已处理')!.trigger('click')
    const order = wrapper.get<HTMLButtonElement>('.approval-detail button[aria-haspopup="dialog"]')
    const disabledWhileDeciding = order.element.disabled
    await order.trigger('click')
    const openedWhileDeciding = wrapper.findComponent(V2OrderDetailPane).exists()
    finishDecision({ ...approval, status: 'approved', can_decide: false })
    await flushPromises()
    expect(disabledWhileDeciding).toBe(true)
    expect(openedWhileDeciding).toBe(false)
    expect(order.element.disabled).toBe(false)
    wrapper.unmount()
  })

  it.each([
    [null, '待分配'],
    ['原处理人', '待重新分配'],
  ])('退款需要重新安排时显示对应状态和配置入口', async (assignee, label) => {
    mocks.options.mockResolvedValue({ can_apply: false, can_manage: true, approvers: [], available_cc: [], default_cc_ids: [] })
    mocks.list.mockResolvedValue({ items: [{ ...approval, can_decide: false, requires_assignment: true, approver_name: assignee, refund_source_no: '退款来源测试' }], total: 1 })
    const wrapper = mount(V2Approvals, { global: { stubs } })
    await flushPromises()
    expect(wrapper.text()).toContain(label)
    await wrapper.findAll('button').find((button) => button.text() === '查看')!.trigger('click')
    expect(wrapper.text()).toContain('配置退款处理人')
    expect(wrapper.text()).toContain('退款来源测试')
    expect(wrapper.text()).not.toContain('标记已处理')
    wrapper.unmount()
  })

  it('抄送人能查记录但不出现审批按钮，退款待办仅提交退款处理意见', async () => {
    mocks.list.mockResolvedValueOnce({ items: [approval], total: 1 })
    const wrapper = mount(V2Approvals, { global: { stubs } })
    await flushPromises()
    expect(wrapper.text()).toContain('退款处理')
    await wrapper.findAll('button').find((button) => button.text() === '处理')!.trigger('click')
    expect(wrapper.text()).not.toContain('通过并生成罚款单')
    await wrapper.get('textarea').setValue('已同打手沟通')
    mocks.decide.mockResolvedValueOnce({ ...approval, status: 'approved', can_decide: false })
    await wrapper.findAll('button').find((button) => button.text() === '标记已处理')!.trigger('click')
    await flushPromises()
    expect(mocks.decide).toHaveBeenCalledExactlyOnceWith(9, 'approved', '已同打手沟通')
    expect(mocks.submit).not.toHaveBeenCalled()
    wrapper.unmount()

    mocks.options.mockResolvedValue({ can_apply: false, can_manage: false, approvers: [], available_cc: [], default_cc_ids: [] })
    mocks.list.mockImplementation(async (mode: string) => ({ items: mode === 'copied' ? [{ ...approval, kind: 'penalty', can_decide: false }] : [], total: mode === 'copied' ? 1 : 0 }))
    const copied = mount(V2Approvals, { global: { stubs } })
    await flushPromises()
    await copied.findAll('button').find((button) => button.text() === '抄送我的')!.trigger('click')
    await flushPromises()
    await copied.findAll('button').find((button) => button.text() === '查看')!.trigger('click')
    expect(copied.text()).not.toContain('通过并生成罚款单')
    expect(copied.find('[data-v2="start-approval"]').exists()).toBe(false)
    copied.unmount()
  })
})

describe('罚款弹窗', () => {
  async function pickApprover(wrapper: ReturnType<typeof mount>) {
    await wrapper.get('button[aria-label="选择审批人"]').trigger('click')
    await wrapper.findAll('button').find((button) => button.text() === '组长甲')!.trigger('click')
  }

  it('搜索选中原单和罚款对象，保留用户修改的项目内容并仅提交关联字段', async () => {
    mocks.searchOrders.mockResolvedValueOnce({ items: [], total: 0 }).mockResolvedValue({
      items: [
        { order_no: originalNo, source_order_no: '合成来源单' },
        { order_no: '00000000000000005678', source_order_no: '别的来源号', project_text: '合成来源单' },
      ],
      total: 2,
    })
    const wrapper = mount(V2PenaltyDialog, { global: { stubs } })
    await flushPromises()
    const original = wrapper.get<HTMLInputElement>('input[aria-label="关联订单号"]')
    await original.setValue('合成来源单')
    await original.trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(mocks.searchOrders).toHaveBeenNthCalledWith(1, { batch_lookup_nos: '合成来源单', page: 1, page_size: 20 })
    expect(mocks.searchOrders).toHaveBeenNthCalledWith(2, { keyword: '合成来源单', page: 1, page_size: 20 })
    expect(wrapper.findAll('[role="option"]')).toHaveLength(2)
    await wrapper.get('[role="option"]:last-child').trigger('mousedown')
    await wrapper.get('[role="option"]:last-child').trigger('click')
    await flushPromises()
    expect(original.element.value).toBe('合成来源单')
    expect(mocks.order).toHaveBeenCalledWith(originalNo)
    expect(mocks.targets).toHaveBeenCalledWith('', originalNo, 7)
    expect(wrapper.text()).toContain('测试游戏')
    expect(wrapper.find('select[aria-label="游戏"]').exists()).toBe(false)
    expect(wrapper.find('select[aria-label="订单来源"]').exists()).toBe(false)
    expect(mocks.sources).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('罚款对象类型')

    mocks.targets.mockResolvedValueOnce({
      items: [
        { id: 18, name: '测试打手甲', detail: '打手 · #18' },
        { id: 29, name: '测试打手甲', detail: '下家工作室 · #29' },
      ],
      suggested: null,
    })
    const target = wrapper.get<HTMLInputElement>('input[aria-label="罚款对象"]')
    await target.setValue('打手')
    await target.trigger('keydown', { key: 'Enter' })
    await flushPromises()
    const studioOption = wrapper.findAll('[role="option"]').find((item) => item.text().includes('下家工作室 · #29'))!
    await studioOption.trigger('mousedown')
    await studioOption.trigger('click')
    expect(target.element.value).toBe('测试打手甲')
    expect(wrapper.text()).not.toContain('下家工作室 · #29')
    expect(wrapper.get('.penalty-cc-fixed').text()).toContain('抄送甲')
    expect(wrapper.find('.penalty-cc-fixed button').exists()).toBe(false)
    expect(wrapper.text()).toContain('日期')
    await wrapper.get('input[aria-label="罚款金额（元）"]').setValue('30.50')
    await wrapper.get('textarea[aria-label="罚款原因"]').setValue('超时')
    const project = wrapper.get<HTMLTextAreaElement>('textarea[aria-label="项目内容"]')
    expect(project.element.value).toBe('超时')
    await project.setValue('补偿处理')
    await wrapper.get('textarea[aria-label="罚款原因"]').setValue('超时后终止')
    expect(project.element.value).toBe('补偿处理')
    await wrapper.get('input[aria-label="罚款对象是否异议"]').setValue('已沟通')
    await wrapper.get('input[aria-label="备注"]').setValue('复核后处理')
    await pickApprover(wrapper)
    expect(wrapper.get('button[aria-label="选择审批人"]').text()).toBe('组长甲')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(mocks.submit).toHaveBeenCalledExactlyOnceWith({
      original_order_no: originalNo, target_id: 29, amount: '30.50',
      approver_id: 41, cc_ids: [], reason: '超时后终止', objection: '已沟通', remark: '复核后处理',
      image_keys: [], order: { project_text: '补偿处理' },
    })
    expect(wrapper.emitted('submitted')).toHaveLength(1)
    wrapper.unmount()
  })

  it.each([originalNo, '罚-PK2026'])('完整单号 %s 优先精确搜索，可从键盘选中结果', async (orderNumber) => {
    const wrapper = mount(V2PenaltyDialog, { global: { stubs } })
    await flushPromises()
    const original = wrapper.get('input[aria-label="关联订单号"]')
    await original.setValue(orderNumber)
    await original.trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(mocks.searchOrders).toHaveBeenCalledExactlyOnceWith({ batch_lookup_nos: orderNumber, page: 1, page_size: 20 })
    await original.trigger('keydown', { key: 'ArrowUp' })
    await original.trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(mocks.order).toHaveBeenCalledExactlyOnceWith(originalNo)
    wrapper.unmount()
  })

  it('关联订单必须选中，明确选无后才补必要录单字段', async () => {
    const wrapper = mount(V2PenaltyDialog, { global: { stubs } })
    await flushPromises()
    expect(wrapper.find('select[aria-label="游戏"]').exists()).toBe(false)
    await wrapper.get('form').trigger('submit')
    expect(wrapper.text()).toContain('请选择关联订单号，或选择“无”')
    expect(mocks.submit).not.toHaveBeenCalled()

    await wrapper.get('input[aria-label="关联订单号"]').trigger('focus')
    await wrapper.get('[role="option"]').trigger('click')
    expect(wrapper.get<HTMLInputElement>('input[aria-label="关联订单号"]').element.value).toBe('无')
    await wrapper.get('input[aria-label="罚款对象"]').trigger('focus')
    await flushPromises()
    expect(mocks.targets).toHaveBeenCalledWith('', undefined, undefined)
    await wrapper.get('[role="option"]').trigger('click')
    await wrapper.get('select[aria-label="游戏"]').setValue('7')
    await flushPromises()
    expect(wrapper.get<HTMLInputElement>('input[aria-label="罚款对象"]').element.value).toBe('测试打手甲')
    expect(wrapper.get<HTMLSelectElement>('select[aria-label="订单类型"]').element.value).toBe('BOOST')
    expect(wrapper.get<HTMLInputElement>('input[aria-label="接单价（元）"]').element.value).toBe('0')
    await wrapper.get('input[aria-label="接单价（元）"]').setValue('12.00')
    await wrapper.get('input[aria-label="罚款金额（元）"]').setValue('9')
    await wrapper.get('textarea[aria-label="罚款原因"]').setValue('独立罚款')
    expect(wrapper.get<HTMLTextAreaElement>('textarea[aria-label="代练内容"]').element.value).toBe('独立罚款')
    await pickApprover(wrapper)
    await wrapper.get('form').trigger('submit')
    expect(wrapper.text()).toContain('请选择订单来源')
    await wrapper.get('select[aria-label="订单来源"]').setValue('11')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(mocks.submit).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      original_order_no: null, target_id: 18, approver_id: 41,
      order: { project_text: '独立罚款', game: 7, order_source: 11, order_type: 'BOOST', total_price: '12.00' },
    }))
    wrapper.unmount()
  })

  it('来源列表读取失败后可重试，罚款原因过长时保留全文并提示项目内容已缩短', async () => {
    mocks.sources.mockRejectedValueOnce(new Error('测试来源加载失败'))
    const wrapper = mount(V2PenaltyDialog, { global: { stubs } })
    await flushPromises()
    await wrapper.get('input[aria-label="关联订单号"]').trigger('focus')
    await wrapper.get('[role="option"]').trigger('click')
    await flushPromises()
    const source = wrapper.get<HTMLSelectElement>('select[aria-label="订单来源"]')
    expect(source.element.disabled).toBe(true)
    expect(wrapper.text()).toContain('测试来源加载失败')
    await wrapper.findAll('button').find((button) => button.text() === '重试')!.trigger('click')
    await flushPromises()
    expect(source.element.disabled).toBe(false)
    expect(source.text()).toContain('测试来源')

    const fullReason = '罚'.repeat(901)
    const reason = wrapper.get<HTMLTextAreaElement>('textarea[aria-label="罚款原因"]')
    await reason.setValue(fullReason)
    expect(reason.element.value).toBe(fullReason)
    expect(wrapper.get<HTMLTextAreaElement>('textarea[aria-label="代练内容"]').element.value).toBe(fullReason.slice(0, 900))
    expect(wrapper.text()).toContain('罚款原因保留全文')
    wrapper.unmount()
  })

  it('从管理订单发起时带入不可编辑的原单和接手人', async () => {
    const wrapper = mount(V2PenaltyDialog, { props: { originalOrderNo: originalNo }, global: { stubs } })
    await flushPromises()
    const original = wrapper.get<HTMLInputElement>('input[aria-label="关联订单号"]')
    expect(original.element.readOnly).toBe(true)
    expect(original.element.value).toBe(originalNo)
    await wrapper.get('textarea[aria-label="罚款原因"]').setValue('罚款项目')
    expect(wrapper.get<HTMLTextAreaElement>('textarea[aria-label="项目内容"]').element.value).toBe('罚款项目')
    expect(wrapper.get<HTMLInputElement>('input[aria-label="罚款对象"]').element.value).toBe('测试打手甲')
    wrapper.unmount()
  })
})

describe('审批截图', () => {
  it('仅保留粘贴入口，截图走现有压缩上传流程并回传对象键', async () => {
    const wrapper = mount(V2ApprovalEvidence, { props: { disabled: false }, global: { stubs } })
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:preview')
    mocks.ticket.mockResolvedValueOnce({ key: 'first', upload_url: 'https://example.invalid/first' })
    const image = new File(['png'], 'test.png', { type: 'image/png' })
    const paste = new Event('paste', { bubbles: true })
    Object.defineProperty(paste, 'clipboardData', { value: { items: [{ kind: 'file', type: 'image/png', getAsFile: () => image }] } })
    expect(wrapper.find('input[type=file]').exists()).toBe(false)
    wrapper.get('[aria-label="粘贴截图"]').element.dispatchEvent(paste)
    await flushPromises()
    expect(mocks.upload.mock.calls).toEqual([['https://example.invalid/first', image]])
    expect(wrapper.emitted('change')).toEqual([[['first']]])
    await wrapper.get('button[aria-label="放大查看 test.png"]').trigger('click')
    expect(wrapper.get('[title="图片预览"] img').attributes('src')).toBe('blob:preview')
    wrapper.unmount()
    vi.restoreAllMocks()
  })
})
