// @vitest-environment jsdom
import { installFramework7ForTest } from '../../styles/__tests__/framework7Setup'
import { f7ready } from 'framework7-vue'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import type { AxiosRequestConfig } from 'axios'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// 传输层是唯一的桩边界（与 HallPage.spec 同一手法）：myOrders 跑真代码，
// 判据打在 mock 的 request 被调时的 url / method / 有没有 data 上。
// ⚠️ `http` 键必须给：`api/orders.ts` 顶层 `import { http, request }`（isOrderNoShape 用）。
vi.mock('@/api/http', () => ({ request: vi.fn(), http: {} }))

vi.mock('../../framework7Ui', () => ({
  Toast: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn(), warning: vi.fn() }),
}))

import { Toast } from '../../framework7Ui'
import type { Mock } from 'vitest'
import { BizError } from '@/api/errors'
import { request } from '@/api/http'
import { ORDER_STATUS_LABELS } from '@/config/orderStatus'
// 桌面那组文案常量（任务书 T3 测试 4：**把桌面那组文案常量逐字断言**，防抄错）。
// 只 import 常量（模块顶层全是定义、无副作用），不 bootstrap island、不引 layui 运行时。
import {
  CHANGE_STATUS_CANCEL_BTN,
  CHANGE_STATUS_CONFIRM_BTN,
  CHANGE_STATUS_DIALOG_TITLE,
  CHANGE_STATUS_TARGETS,
} from '@/layui-island/pages/order-myorderlist'
import SubmitWorkDialog from '../SubmitWorkDialog.vue'

const requestMock = request as unknown as Mock
const toast = Toast as unknown as { success: Mock; error: Mock }

const ORDER_NO = '10000000000000000001'

/** submit / report-issue 的响应（流转后的完整订单——本测试只造判据要读的两个键）。 */
function transitionedOrder(status: string): Record<string, unknown> {
  return { order_no: ORDER_NO, status }
}

async function mountDialog(props: Record<string, unknown> = {}, attached = false) {
  const wrapper = mount(SubmitWorkDialog, {
    props: { visible: true, orderNo: ORDER_NO, ...props },
    attachTo: attached ? document.getElementById('framework7-root') ?? undefined : undefined,
  })
  await flushPromises()
  return wrapper
}

const dialogSel = '[data-testid="submit-work-dialog"]'
const confirmSel = '.m-submit-dialog__confirm'
const cancelSel = '.m-submit-dialog__cancel'

function requestCallsTo(fragment: string): AxiosRequestConfig[] {
  return requestMock.mock.calls
    .map((c) => c[0] as AxiosRequestConfig)
    .filter((cfg) => String(cfg.url).includes(fragment))
}

/** 取筛选后数组的首元素（不存在即抛——noUncheckedIndexedAccess 下不给断言静默通过的机会）。 */
function firstCall(calls: AxiosRequestConfig[]): AxiosRequestConfig {
  const first = calls[0]
  if (first === undefined) throw new Error('spec: 没有命中该端点的请求')
  return first
}

/** 弹层里两档 radio 的（label 文本, 是否勾中）按 DOM 顺序读出。 */
function radioStates(wrapper: ReturnType<typeof mount>): Array<{ label: string; checked: boolean }> {
  return wrapper.findAll('.m-submit-dialog__option').map((opt) => ({
    label: opt.text(),
    checked: (opt.find('input').element as HTMLInputElement).checked,
  }))
}

enableAutoUnmount(afterEach)

beforeEach(() => {
  vi.clearAllMocks()
  requestMock.mockResolvedValue(transitionedOrder('PENDING_REVIEW'))
})

installFramework7ForTest()

describe('m 端交单弹层（SubmitWorkDialog）', () => {
  it('文案与桌面逐字一致：标题 / 确认 / 取消取桌面常量，两档 label 取后端词表且顺序 = 桌面 TARGETS', async () => {
    const wrapper = await mountDialog()
    expect(wrapper.find(`${dialogSel} .m-submit-dialog__title`).text()).toBe(
      CHANGE_STATUS_DIALOG_TITLE,
    )
    expect(wrapper.find(confirmSel).text()).toBe(CHANGE_STATUS_CONFIRM_BTN)
    expect(wrapper.find(cancelSel).text()).toBe(CHANGE_STATUS_CANCEL_BTN)
    expect(radioStates(wrapper).map((r) => r.label)).toEqual(
      CHANGE_STATUS_TARGETS.map((code) => ORDER_STATUS_LABELS[code]),
    )
  })

  it('每次打开重置为该次的目标，清除上一次的原因', async () => {
    const wrapper = mount(SubmitWorkDialog, {
      props: { visible: false, orderNo: ORDER_NO, initialTarget: 'PENDING_REVIEW' },
    })
    await wrapper.setProps({ visible: true })
    expect(radioStates(wrapper)).toEqual([
      { label: '待验收', checked: true },
      { label: '问题单', checked: false },
    ])
    // 这次会话里切到问题档，然后关闭弹层。
    await wrapper.find('input[value="ISSUE"]').setValue()
    await wrapper.get('textarea[aria-label="说明原因"]').setValue('旧订单的原因')
    await wrapper.setProps({ visible: false })
    // 重开：回到本次的 initialTarget，不是上次留下的 ISSUE。
    await wrapper.setProps({ visible: true })
    expect(radioStates(wrapper)).toEqual([
      { label: '待验收', checked: true },
      { label: '问题单', checked: false },
    ])
    await wrapper.find('input[value="ISSUE"]').setValue()
    expect(wrapper.get('textarea').element.value).toBe('')
  })

  it('确认（默认待验收档）→ POST /submit/ 且请求 config 没有 data；toast「提交成功」+ success 载荷 + 关弹层', async () => {
    const wrapper = await mountDialog()
    await wrapper.find(confirmSel).trigger('click')
    await flushPromises()

    const cfg = firstCall(requestCallsTo('/submit/'))
    expect(cfg).toMatchObject({ method: 'post', url: `/api/business/orders/${ORDER_NO}/submit/` })
    expect('data' in cfg).toBe(false)

    expect(toast.success).toHaveBeenCalledWith('提交成功')
    expect(wrapper.emitted('success')).toEqual([[transitionedOrder('PENDING_REVIEW')]])
    expect(wrapper.emitted('update:visible')).toEqual([[false]])
  })

  it('问题档拒绝空白原因，再提交去掉首尾空白的原因', async () => {
    requestMock.mockResolvedValue(transitionedOrder('ISSUE'))
    const wrapper = await mountDialog({ initialTarget: 'ISSUE' })
    const reason = wrapper.get('textarea[aria-label="说明原因"]')
    await reason.setValue(' 　')
    await wrapper.find(confirmSel).trigger('click')
    expect(requestCallsTo('/report-issue/')).toHaveLength(0)
    expect(toast.error).toHaveBeenCalledWith('请输入问题原因')
    expect(wrapper.emitted('update:visible')).toBeUndefined()

    await reason.setValue(' 账号掉线 ')
    await wrapper.find(confirmSel).trigger('click')
    await flushPromises()

    const cfg = firstCall(requestCallsTo('/report-issue/'))
    expect(cfg).toMatchObject({
      method: 'post',
      url: `/api/business/orders/${ORDER_NO}/report-issue/`,
      data: { reason: '账号掉线' },
    })
    expect(toast.success).toHaveBeenCalledWith('已报问题单')
    expect(wrapper.emitted('success')).toEqual([[transitionedOrder('ISSUE')]])
  })

  it('DOM 点选切档也生效（radio v-model 是真交互）：勾问题档后确认走 /report-issue/', async () => {
    const wrapper = await mountDialog()
    expect(wrapper.find('textarea').exists()).toBe(false)
    await wrapper.find('input[value="ISSUE"]').setValue()
    await wrapper.get('textarea[aria-label="说明原因"]').setValue('账号掉线')
    await wrapper.find(confirmSel).trigger('click')
    await flushPromises()

    expect(requestCallsTo('/report-issue/')).toHaveLength(1)
    expect(requestCallsTo('/submit/')).toHaveLength(0)
    expect(toast.success).toHaveBeenCalledWith('已报问题单')
  })

  it('失败：toast = 后端 msg 原样（1002 也是它）；不 emit success、弹层留着（可重试 / 取消）', async () => {
    requestMock.mockRejectedValue(new BizError(1002, '只有代练中的单可以提交，当前状态：PENDING_REVIEW'))
    const wrapper = await mountDialog()
    await wrapper.find(confirmSel).trigger('click')
    await flushPromises()

    expect(toast.error).toHaveBeenCalledWith('只有代练中的单可以提交，当前状态：PENDING_REVIEW')
    expect(toast.success).not.toHaveBeenCalled()
    expect(wrapper.emitted('success')).toBeUndefined()
    expect(wrapper.emitted('update:visible')).toBeUndefined()
    // 在途闸已复位：确认键恢复可点。
    expect(wrapper.find(confirmSel).attributes('disabled')).toBeUndefined()
  })

  it('在途闸：确认在途禁点 / 禁取消；再点不发第二条；落地后复位', async () => {
    let resolveSubmit: (v: unknown) => void = () => {}
    requestMock.mockImplementation(() => new Promise((res) => { resolveSubmit = res }))
    const wrapper = await mountDialog()

    await wrapper.find(confirmSel).trigger('click')
    await flushPromises()
    expect(wrapper.find(confirmSel).attributes('disabled')).toBeDefined()
    expect(wrapper.find(cancelSel).attributes('disabled')).toBeDefined()

    // 在途再点确认：闸早返回，不发第二条；点取消也不关。
    await wrapper.find(confirmSel).trigger('click')
    await wrapper.find(cancelSel).trigger('click')
    expect(requestCallsTo('/submit/')).toHaveLength(1)
    expect(wrapper.emitted('update:visible')).toBeUndefined()

    resolveSubmit(transitionedOrder('PENDING_REVIEW'))
    await flushPromises()
    expect(wrapper.emitted('success')).toHaveLength(1)
    expect(wrapper.find(confirmSel).attributes('disabled')).toBeUndefined()
  })

  it('遮罩与 Escape 空闲可关闭，在途不能关闭', async () => {
    let resolveSubmit: (v: unknown) => void = () => {}
    requestMock.mockImplementation(() => new Promise((res) => { resolveSubmit = res }))
    const wrapper = await mountDialog({}, true)
    const dismissWithEscape = () => document.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', keyCode: 27 }),
    )
    const clickBackdrop = () => wrapper.get('.sheet-backdrop.backdrop-in').trigger('click')

    await clickBackdrop()
    expect(wrapper.emitted('update:visible')).toEqual([[false]])
    await wrapper.setProps({ visible: false })
    expect(wrapper.get('.sheet-modal').classes()).not.toContain('modal-in')
    expect(wrapper.find('.sheet-backdrop.backdrop-in').exists()).toBe(false)
    await wrapper.setProps({ visible: true })
    dismissWithEscape()
    expect(wrapper.emitted('update:visible')).toEqual([[false], [false]])
    await wrapper.setProps({ visible: false })
    expect(wrapper.get('.sheet-modal').classes()).not.toContain('modal-in')
    expect(wrapper.find('.sheet-backdrop.backdrop-in').exists()).toBe(false)
    await wrapper.setProps({ visible: true })

    await wrapper.get(confirmSel).trigger('click')
    await clickBackdrop()
    dismissWithEscape()
    expect(wrapper.emitted('update:visible')).toHaveLength(2)
    expect(wrapper.get('.sheet-modal').classes()).toContain('modal-in')
    expect(requestCallsTo('/submit/')).toHaveLength(1)

    resolveSubmit(transitionedOrder('PENDING_REVIEW'))
    await flushPromises()
    expect(wrapper.emitted('update:visible')).toEqual([[false], [false], [false]])
  })

  it.each(['离开页面', '提交成功后关闭'])('%s时会解除遮罩和键盘监听', async (exit) => {
    let resolveSubmit: (v: unknown) => void = () => {}
    requestMock.mockImplementation(() => new Promise((res) => { resolveSubmit = res }))
    const wrapper = await mountDialog({
      visible: false,
      'onUpdate:visible': () => wrapper.unmount(),
    }, true)
    await wrapper.setProps({ visible: true })
    expect(wrapper.get('.sheet-modal').classes()).toContain('modal-in')
    expect(wrapper.find('.sheet-backdrop.backdrop-in').exists()).toBe(true)
    await wrapper.get(confirmSel).trigger('click')

    if (exit === '离开页面') wrapper.unmount()
    else {
      resolveSubmit(transitionedOrder('PENDING_REVIEW'))
      await flushPromises()
    }
    expect(toast.success).toHaveBeenCalledTimes(exit === '离开页面' ? 0 : 1)

    const report = vi.fn((event: ErrorEvent) => event.preventDefault())
    window.addEventListener('error', report)
    try {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', keyCode: 27 }))
      expect(report).not.toHaveBeenCalled()
      expect(() => f7ready((app) => app.emit('click', new MouseEvent('click')))).not.toThrow()
    } finally {
      window.removeEventListener('error', report)
    }
    expect(document.querySelector('.sheet-backdrop.backdrop-in')).toBeNull()
    if (exit === '离开页面') resolveSubmit(transitionedOrder('PENDING_REVIEW'))
  })

  it('不在途时取消：emit update:visible false、零请求；空单号 fail-closed：确认零请求', async () => {
    const wrapper = await mountDialog({ visible: false })
    await wrapper.setProps({ visible: true })
    await wrapper.find(cancelSel).trigger('click')
    expect(wrapper.emitted('update:visible')).toEqual([[false]])
    expect(requestMock).not.toHaveBeenCalled()

    const blank = await mountDialog({ orderNo: '' })
    await blank.find(confirmSel).trigger('click')
    await flushPromises()
    expect(requestMock).not.toHaveBeenCalled()
    expect(blank.emitted('success')).toBeUndefined()
  })
})
