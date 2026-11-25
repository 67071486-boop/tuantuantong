// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { vi, afterEach, beforeEach, expect, it } from 'vitest'
import { PAYROLL_CLOSE_HINT, PAYROLL_CREATING_HINT, PAYROLL_WAITING_HINT, type PayrollBatch, type PayrollPreview } from '@/api/onlinePayroll'
import V2OnlinePayrollDialog from '../V2OnlinePayrollDialog.vue'

const api = vi.hoisted(() => ({
  previewPayroll: vi.fn(),
  createPayroll: vi.fn(),
  readPayroll: vi.fn(),
  preparePayrollPayment: vi.fn(),
  cancelPayroll: vi.fn(),
}))
vi.mock('@/api/onlinePayroll', async (original) => ({
  ...await original<typeof import('@/api/onlinePayroll')>(),
  ...api,
}))

const preview: PayrollPreview = {
  fingerprint: 'quote',
  total: '80.00',
  payers: [
    { id: 1, account_masked: '***101', real_name: '付款甲' },
    { id: 2, account_masked: '***102', real_name: '付款乙' },
  ],
  transfers: [{
    recipient_name: '收款甲', account_masked: '***201', amount: '80.00',
    items: [
      { performance_id: 5, order_no: '订单甲', bonus: '60.03', deduction: '3.10', adjustment: '-15.30', amount: '41.63' },
      { performance_id: 6, order_no: '订单乙', bonus: '39.97', deduction: '1.90', adjustment: '0.30', amount: '38.37' },
    ],
  }],
}
const batch: PayrollBatch = {
  id: '原付款单', category: 'customer_service', created_at: '2026-11-15', status: 'ready',
  total: '80.00', payer_masked: '***102', active: true, gateway_batches: [],
}
const dialogStub = { template: '<div><slot /><slot name="footer" /></div>' }
const selection = { performance_ids: [5, 6], category: 'customer_service' } as const

function openDialog() {
  return mount(V2OnlinePayrollDialog, {
    props: { selection: { performance_ids: [...selection.performance_ids], category: selection.category } },
    global: { stubs: { 'el-dialog': dialogStub } },
  })
}

beforeEach(() => {
  vi.resetAllMocks()
  api.previewPayroll.mockResolvedValue(preview)
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

it('按客服分类确认，双击只制单一次，网络失败重试沿用原请求号和支付宝', async () => {
  let reject: (cause: Error) => void = () => undefined
  api.createPayroll.mockReturnValueOnce(new Promise((_, fail) => { reject = fail }))
  api.createPayroll.mockResolvedValueOnce(batch)
  const wrapper = openDialog()
  await flushPromises()
  expect(wrapper.text()).toContain('客服报酬')
  const recipients = wrapper.get('[aria-label="按收款人汇总"]')
  expect(recipients.findAll('tbody tr')).toHaveLength(1)
  expect(recipients.findAll('tbody td').map((cell) => cell.text())).toEqual([
    '收款甲', '***201', '100.00', '5.00', '-15.00', '80.00',
  ])
  expect(wrapper.text()).not.toMatch(/关联订单|订单甲|订单乙/)
  await wrapper.get('[data-v2="payroll-payer"]').setValue('2')
  const submit = wrapper.get('[data-v2="payroll-submit"]')
  const first = submit.trigger('click')
  const second = submit.trigger('click')
  await Promise.all([first, second])
  expect(api.createPayroll).toHaveBeenCalledTimes(1)
  expect(wrapper.get('[role="status"]').text()).toBe(PAYROLL_CREATING_HINT)
  expect(submit.attributes()).toHaveProperty('disabled')
  reject(new Error('网络超时'))
  await flushPromises()
  await submit.trigger('click')
  await flushPromises()
  expect(api.createPayroll).toHaveBeenCalledTimes(2)
  expect(api.createPayroll.mock.calls[1]).toEqual(api.createPayroll.mock.calls[0])
  expect(api.createPayroll.mock.calls[0]?.[0]).toMatchObject({
    category: 'customer_service', performance_ids: [5, 6], payer_id: 2, fingerprint: 'quote',
  })
  await wrapper.findAll('button').find((button) => button.text() === '关闭')!.trigger('click')
  expect(wrapper.emitted('close')?.[0]).toEqual([batch])
  expect(PAYROLL_CLOSE_HINT).toContain('在线结算明细')
  wrapper.unmount()
})

it('付款单保存后自动等待付款码，打款失败后保留明细和窗口', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  const gateway = { id: 32, status: 'payable', can_prepare_payment: true, transfers: [] }
  api.createPayroll.mockResolvedValue({
    ...batch, status: 'preparing', gateway_batches: [{ ...gateway, status: 'sealing', can_prepare_payment: false }],
  })
  const payable = { ...batch, gateway_batches: [gateway] }
  api.readPayroll.mockResolvedValueOnce(payable).mockResolvedValueOnce(payable).mockResolvedValueOnce(payable)
  api.preparePayrollPayment.mockRejectedValueOnce(new Error('网络超时')).mockResolvedValueOnce(payable).mockResolvedValue({
    ...batch, gateway_batches: [{
      ...gateway, payment_qr_png_base64: 'cXI=', payment_url: 'https://example.test/mobile-pay',
    }],
  })
  api.readPayroll.mockResolvedValueOnce({
    ...batch, status: 'failed', active: false,
    gateway_batches: [{ ...gateway, status: 'failed', can_prepare_payment: false }],
  })
  const wrapper = openDialog()
  try {
    await flushPromises()
    await wrapper.get('[data-v2="payroll-payer"]').setValue('2')
    await wrapper.get('[data-v2="payroll-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="status"]').text()).toBe(PAYROLL_WAITING_HINT)
    expect(api.preparePayrollPayment).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(3000)
    expect(wrapper.get('[role="alert"]').text()).toBe('网络超时')
    expect(api.preparePayrollPayment).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(3000)
    expect(wrapper.find('img').exists()).toBe(false)
    expect(api.preparePayrollPayment).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(3000)
    expect(wrapper.get('img[alt="支付宝付款二维码"]').attributes('src')).toBe('data:image/png;base64,cXI=')
    expect(wrapper.find('a').exists()).toBe(false)
    await vi.advanceTimersByTimeAsync(3000)
    expect(wrapper.get('h3').text()).toContain('打款失败')
    expect(wrapper.text()).not.toContain('待扫码付款')
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.emitted('close')).toBeUndefined()
  } finally {
    wrapper.unmount()
  }
})

it('重新取码的子批次失败后，批次有其他在处理也会停止等待该二维码', async () => {
  const gateway = { id: 32, status: 'payable', can_prepare_payment: true, transfers: [] }
  const pending = { id: 33, status: 'sealing', can_prepare_payment: false, transfers: [] }
  api.previewPayroll.mockResolvedValue({ existing_batch_id: batch.id })
  api.readPayroll.mockResolvedValue({
    ...batch, gateway_batches: [{ ...gateway, payment_qr_png_base64: 'cXI=' }, pending],
  })
  api.preparePayrollPayment.mockResolvedValue({
    ...batch, status: 'preparing', gateway_batches: [{ ...gateway, status: 'unknown', can_prepare_payment: false, last_error: '付款码生成失败' }, pending],
  })
  const wrapper = openDialog()
  try {
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text() === '重新获取付款码')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('付款码生成失败')
    expect(wrapper.text()).not.toContain('正在重新生成付款二维码')
    expect(wrapper.find('img').exists()).toBe(false)
  } finally {
    wrapper.unmount()
  }
})

it.each([['desktop', true], ['Android Mobile', false]])('付款链接按设备展示（%s）', async (ua, desktop) => {
  vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(ua)
  const tab = { opener: window, closed: false, location: { replace: vi.fn() }, close: vi.fn() }
  const open = vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
  const gateway = { id: 32, status: 'payable', can_prepare_payment: true, transfers: [] }
  api.previewPayroll.mockResolvedValue({ existing_batch_id: batch.id })
  api.readPayroll.mockResolvedValue({ ...batch, gateway_batches: [gateway] })
  api.preparePayrollPayment.mockResolvedValueOnce({
    ...batch, gateway_batches: [{ ...gateway, payment_qr_png_base64: 'cXI=' }],
  }).mockResolvedValueOnce({
    ...batch, gateway_batches: [{ ...gateway, payment_pc_url: 'https://example.test/desktop-pay' }],
  })
  const wrapper = openDialog()
  try {
    await flushPromises()
    expect(wrapper.find('[aria-label="手机支付宝扫码"] img').exists()).toBe(true)
    expect(wrapper.find('[aria-label="电脑浏览器付款"]').exists()).toBe(desktop)
    const pcButton = wrapper.findAll('button').find((button) => button.text() === '跳转至浏览器付款')
    expect(Boolean(pcButton)).toBe(desktop)
    expect(wrapper.find('a').exists()).toBe(false)
    await pcButton?.trigger('click')
    await flushPromises()
    expect(api.preparePayrollPayment.mock.calls).toEqual(desktop ? [[batch.id, 32], [batch.id, 32, false, true]] : [[batch.id, 32]])
    expect(wrapper.findAll('a').map((link) => link.attributes('href'))).toEqual(desktop ? ['https://example.test/desktop-pay'] : [])
    expect(wrapper.find('img[alt="支付宝付款二维码"]').exists()).toBe(true)
    expect(open.mock.calls).toEqual(desktop ? [['', '_blank']] : [])
    expect(tab.opener === null).toBe(desktop)
    expect(tab.location.replace.mock.calls).toEqual(desktop ? [['https://example.test/desktop-pay']] : [])
    expect(tab.close).not.toHaveBeenCalled()
  } finally {
    wrapper.unmount()
  }
})

it('相同选择已在处理时，直接打开原付款单，显示已有付款码且不重新制单', async () => {
  const gateway = { id: 32, status: 'payable', can_prepare_payment: true, transfers: [] }
  api.previewPayroll.mockResolvedValue({ existing_batch_id: batch.id })
  api.readPayroll.mockResolvedValue({ ...batch, gateway_batches: [gateway] })
  api.preparePayrollPayment.mockResolvedValue({
    ...batch,
    gateway_batches: [{ ...gateway, payment_qr_png_base64: 'cXI=' }],
  })
  const wrapper = openDialog()
  await flushPromises()
  expect(api.readPayroll).toHaveBeenCalledWith(batch.id)
  expect(api.createPayroll).not.toHaveBeenCalled()
  expect(api.preparePayrollPayment).toHaveBeenCalledWith(batch.id, 32)
  expect(wrapper.get('img[alt="支付宝付款二维码"]').attributes('src')).toBe('data:image/png;base64,cXI=')
  wrapper.unmount()
})

it('自动查询未返回时可以获取电脑付款链接，之前的查询结果不会覆盖', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('desktop')
  vi.spyOn(window, 'open').mockReturnValue(null)
  const gateway = { id: 32, status: 'payable', can_prepare_payment: true, transfers: [] }
  const payable = { ...batch, gateway_batches: [{ ...gateway, payment_qr_png_base64: 'cXI=' }] }
  let resolvePoll!: (result: PayrollBatch) => void
  api.previewPayroll.mockResolvedValue({ existing_batch_id: batch.id })
  api.readPayroll.mockResolvedValueOnce(payable).mockReturnValueOnce(new Promise((resolve) => { resolvePoll = resolve }))
  api.preparePayrollPayment.mockResolvedValue({
    ...batch, gateway_batches: [{ ...gateway, payment_pc_url: 'https://example.test/desktop-pay' }],
  })
  const wrapper = openDialog()
  try {
    await flushPromises()
    await vi.advanceTimersByTimeAsync(3000)
    expect(api.readPayroll).toHaveBeenCalledTimes(2)
    const desktopButton = wrapper.findAll('button').find((button) => button.text() === '跳转至浏览器付款')!
    expect(desktopButton.attributes()).not.toHaveProperty('disabled')
    await desktopButton.trigger('click')
    await flushPromises()
    expect(api.preparePayrollPayment).toHaveBeenCalledExactlyOnceWith(batch.id, 32, false, true)
    resolvePoll({
      ...batch, status: 'preparing', gateway_batches: [{ ...gateway, status: 'sealing', can_prepare_payment: false }],
    })
    await flushPromises()
    expect(wrapper.get('a').attributes('href')).toBe('https://example.test/desktop-pay')
    expect(wrapper.get('[role="alert"]').text()).toContain('浏览器未打开付款页')
    expect(wrapper.find('img[alt="支付宝付款二维码"]').exists()).toBe(true)
  } finally {
    wrapper.unmount()
  }
})

it('自动取码期间仍可打开电脑付款，批次结束时关闭空白页', async () => {
  vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('desktop')
  const tab = { opener: window, closed: false, location: { replace: vi.fn() }, close: vi.fn() }
  vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
  const gateway = { id: 32, status: 'payable', can_prepare_payment: true, transfers: [] }
  const payable = { ...batch, gateway_batches: [gateway] }
  let resolveQr!: (result: PayrollBatch) => void
  api.previewPayroll.mockResolvedValue({ existing_batch_id: batch.id })
  api.readPayroll.mockResolvedValue(payable)
  api.preparePayrollPayment.mockReturnValueOnce(new Promise((resolve) => { resolveQr = resolve })).mockResolvedValueOnce({
    ...batch, status: 'failed', active: false,
    gateway_batches: [{ ...gateway, status: 'failed', can_prepare_payment: false }],
  })
  const wrapper = openDialog()
  try {
    await flushPromises()
    expect(wrapper.get('[aria-label="手机支付宝扫码"] [role="status"]').text()).toContain('正在获取付款二维码')
    const desktopButton = wrapper.get('[aria-label="电脑浏览器付款"] button')
    expect(desktopButton.attributes()).not.toHaveProperty('disabled')
    await desktopButton.trigger('click')
    await flushPromises()
    resolveQr({ ...batch, gateway_batches: [{ ...gateway, payment_qr_png_base64: 'cXI=' }] })
    await flushPromises()
    expect(tab.close).toHaveBeenCalledOnce()
    expect(tab.location.replace).not.toHaveBeenCalled()
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.find('[aria-label="电脑浏览器付款"]').exists()).toBe(false)
  } finally {
    wrapper.unmount()
  }
})

it('取消请求只交给服务端处理，部分成功后仍展示原成功流水和其余处理中明细', async () => {
  const part = {
    id: 31, status: 'payable', transfers: [{
      id: 1, recipient_name: '打手甲', amount: '30.00', status: 'succeeded',
      channel_order_no: '已到账流水甲', finalized_at: '2026-11-15T11:00:00+08:00', items: [],
    }, {
      id: 2, recipient_name: '打手乙', amount: '20.00', status: 'processing', items: [],
    }, {
      id: 3, recipient_name: '打手丙', amount: '30.00', status: 'staged', items: [],
    }],
  }
  api.previewPayroll.mockResolvedValue({ existing_batch_id: batch.id })
  api.readPayroll.mockResolvedValue({ ...batch, can_cancel: true, gateway_batches: [part] })
  api.cancelPayroll.mockResolvedValue({
    ...batch, status: 'processing', can_cancel: false,
    gateway_batches: [{ ...part, transfers: [part.transfers[0], part.transfers[1], { ...part.transfers[2], status: 'closed' }] }],
  })
  const wrapper = openDialog()
  await flushPromises()
  await wrapper.findAll('button').find((button) => button.text() === '取消本次付款')!.trigger('click')
  expect(wrapper.text()).toContain('只会取消尚未向计全付制单的明细')
  expect(api.cancelPayroll).not.toHaveBeenCalled()
  await wrapper.findAll('button').find((button) => button.text() === '确认取消')!.trigger('click')
  await flushPromises()
  expect(api.cancelPayroll).toHaveBeenCalledExactlyOnceWith(batch.id)
  expect(wrapper.text()).toContain('已到账流水甲')
  expect(wrapper.text()).toContain('处理中')
  expect(wrapper.text()).toContain('已关闭')
  wrapper.unmount()
})
