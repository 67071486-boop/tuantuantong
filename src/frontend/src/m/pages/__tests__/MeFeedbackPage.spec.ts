// @vitest-environment jsdom
import { installFramework7ForTest } from '../../styles/__tests__/framework7Setup'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import type { AxiosRequestConfig } from 'axios'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../router/useWorkerBack', () => ({ useWorkerBack: () => vi.fn() }))
vi.mock('@/api/http', () => ({ request: vi.fn(), http: {} }))

vi.mock('../../framework7Ui', () => ({
  Toast: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn(), warning: vi.fn() }),
}))

import { Toast } from '../../framework7Ui'
import type { Mock } from 'vitest'
import { BizError } from '@/api/errors'
import { request } from '@/api/http'
import type { FeedbackRow } from '@/api/feedback'
import MeFeedbackPage from '../MeFeedbackPage.vue'

const requestMock = request as unknown as Mock
const toast = Toast as unknown as Mock & { success: Mock; error: Mock }

enableAutoUnmount(afterEach)

/* ── 合成夹具（E-0077） ── */

function rowFixture(overrides: Partial<FeedbackRow> = {}): FeedbackRow {
  return {
    id: 1,
    content: '合成反馈内容：大厅列表偶尔加载不出',
    status: 'pending',
    status_label: '未处理',
    admin_note: '',
    created_by_name: '测试打手甲',
    created_at: '2026-10-11T09:30:00+08:00',
    handled_by_name: null,
    handled_at: null,
    images: [],
    ...overrides,
  }
}

/* ── mock 的请求路由 ── */

let listImpl: (config: AxiosRequestConfig) => Promise<unknown>
let createImpl: (config: AxiosRequestConfig) => Promise<unknown>

const callsOf = (pred: (config: AxiosRequestConfig) => boolean): AxiosRequestConfig[] =>
  requestMock.mock.calls.map((c) => c[0] as AxiosRequestConfig).filter(pred)

installFramework7ForTest()

beforeEach(() => {
  setActivePinia(createPinia())
  requestMock.mockReset()
  toast.mockClear()
  toast.error.mockClear()
  listImpl = () => Promise.resolve({ items: [], total: 0 })
  createImpl = () => Promise.resolve({ id: 99 })
  requestMock.mockImplementation((config: AxiosRequestConfig) => {
    const url = String(config.url)
    const method = String(config.method).toLowerCase()
    if (url === '/api/business/feedback/' && method === 'get') return listImpl(config)
    if (url === '/api/business/feedback/' && method === 'post') return createImpl(config)
    throw new Error(`spec: 未预期的请求 ${method} ${url}`)
  })
})

function mountPage() {
  return mount(MeFeedbackPage)
}

describe('问题反馈（/m/me/feedback）', () => {
  it('首屏取列表：GET /api/business/feedback/；空 ⇒ 「还没有反馈记录」', async () => {
    const wrapper = mountPage()
    await flushPromises()
    const gets = callsOf((c) => String(c.url) === '/api/business/feedback/')
    expect(gets).toHaveLength(1)
    expect(wrapper.find('[data-testid="feedback-empty"]').text()).toBe('还没有反馈记录')
  })

  it('内容为空（去空白）⇒ toast「请描述你遇到的问题」，零请求', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.find('[data-testid="feedback-content"] textarea').setValue('   ')
    await wrapper.find('[data-testid="feedback-submit"]').trigger('click')
    expect(toast.error).toHaveBeenCalledWith('请描述你遇到的问题')
    expect(callsOf((c) => String(c.method).toLowerCase() === 'post')).toHaveLength(0)
  })

  it('提交成功：POST 恰好 {content, image_keys:[]}（显式空数组）+ toast 逐字 + 清空 + 重取列表', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.find('[data-testid="feedback-content"] textarea').setValue('  合成反馈内容  ')
    await wrapper.find('[data-testid="feedback-submit"]').trigger('click')
    await flushPromises()

    const posts = callsOf(
      (c) => String(c.url) === '/api/business/feedback/' && String(c.method).toLowerCase() === 'post',
    )
    expect(posts).toHaveLength(1)
    // ⛔ 恰好断言：`image_keys` 显式给空数组（「忘了传」与「刻意不传」要分得开）。
    expect(posts[0]!.data).toEqual({ content: '合成反馈内容', image_keys: [] })
    expect(toast).toHaveBeenCalledWith('反馈已提交，感谢反馈')
    // 表单清空 + 列表重取（第 2 发 GET）。
    expect((wrapper.find('[data-testid="feedback-content"] textarea').element as HTMLTextAreaElement).value)
      .toBe('')
    expect(
      callsOf(
        (c) => String(c.url) === '/api/business/feedback/' && String(c.method).toLowerCase() === 'get',
      ),
    ).toHaveLength(2)
  })

  it('提交失败：toast = 后端 msg 原样（兜底「操作失败」不出场）、表单保留', async () => {
    createImpl = () => Promise.reject(new BizError(1000, '反馈内容过长'))
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.find('[data-testid="feedback-content"] textarea').setValue('合成反馈内容')
    await wrapper.find('[data-testid="feedback-submit"]').trigger('click')
    await flushPromises()
    expect(toast.error).toHaveBeenCalledWith('反馈内容过长')
    expect((wrapper.find('[data-testid="feedback-content"] textarea').element as HTMLTextAreaElement).value)
      .toBe('合成反馈内容')
  })

  it('列表渲染：内容 / 状态中文 / 时间（按串内偏移）/ 管理员备注（有才显示）', async () => {
    listImpl = () =>
      Promise.resolve({
        items: [
          rowFixture(),
          rowFixture({
            id: 2,
            status: 'handled',
            status_label: '已处理',
            admin_note: '已安排处理，请留意大厅',
            created_at: '2026-10-10T08:05:00+08:00',
            created_by_name: '测试打手乙',
          }),
        ],
        total: 2,
      })
    const wrapper = mountPage()
    await flushPromises()
    const rows = wrapper.findAll('.m-fb-row')
    expect(rows).toHaveLength(2)
    expect(rows[0]!.text()).toContain('合成反馈内容：大厅列表偶尔加载不出')
    expect(rows[0]!.find('[data-testid="feedback-row-status"]').text()).toBe('未处理')
    expect(rows[0]!.text()).toContain('2026-10-11 09:30')
    expect(rows[0]!.find('[data-testid="feedback-row-note"]').exists()).toBe(false)
    expect(rows[1]!.find('[data-testid="feedback-row-status"]').text()).toBe('已处理')
    // 提交人标注（评审必修 2）：每行取自己的 created_by_name（管理员会收到全租户的行）。
    expect(rows[0]!.find('[data-testid="feedback-row-creator"]').text()).toBe('提交人：测试打手甲')
    expect(rows[1]!.find('[data-testid="feedback-row-creator"]').text()).toBe('提交人：测试打手乙')
    expect(rows[1]!.find('[data-testid="feedback-row-note"]').text()).toBe(
      '管理员备注：已安排处理，请留意大厅',
    )
  })

  it('列表取数失败：错误态显示 msg 原样 + 重试后恢复', async () => {
    listImpl = () => Promise.reject(new BizError(1000, '您没有该功能的访问权限'))
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('[data-testid="feedback-error"]').text()).toContain('您没有该功能的访问权限')
    listImpl = () => Promise.resolve({ items: [rowFixture()], total: 1 })
    await wrapper.find('[data-testid="feedback-retry"]').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.m-fb-row')).toHaveLength(1)
  })
})
