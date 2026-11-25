// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import { BizError, HttpError } from '@/api/errors'
import { request } from '@/api/http'
import V2InternalChatSettings from '../V2InternalChatSettings.vue'

vi.mock('@/api/http', () => ({ request: vi.fn() }))

const send = vi.mocked(request)
const settingsUrl = '/api/internal-chat/system-notification-settings/'
const defaults = { approval_submitted: true, approval_approved: true, wecom_aftersales: true, order_grab_qr: false }

beforeEach(() => {
  send.mockReset()
  send.mockImplementation(async ({ url }) => {
    if (url === settingsUrl) return defaults
    if (url === '/api/internal-chat/settings/') return { primary_contact_id: null, backup_contact_ids: [], candidates: [] }
    throw new HttpError(404, '未开放')
  })
})

it('在内部沟通设置只提交改过的规则，并展示服务器保存后的完整配置', async () => {
  const page = mount(V2InternalChatSettings)
  await flushPromises()
  expect(send).toHaveBeenCalledWith({ method: 'GET', url: settingsUrl })
  const panel = page.get('section[aria-labelledby="chat-system-notices-title"]')
  const boxes = panel.findAll<HTMLInputElement>('input[type="checkbox"]')
  expect(boxes.map(row => row.element.checked)).toEqual([true, true, true, false])
  expect(panel.text()).toContain('接单成功后发送群二维码')
  expect(panel.text()).toContain('联系通讯录客服')
  const submit = panel.get<HTMLButtonElement>('button[type="submit"]')
  expect(submit.element.disabled).toBe(true)

  await panel.get('#chat-system-notice-order_grab_qr').setValue(true)
  expect(submit.element.disabled).toBe(false)
  send.mockResolvedValueOnce({ ...defaults, approval_approved: false, order_grab_qr: true })
  await panel.get('form').trigger('submit')
  await flushPromises()

  expect(send).toHaveBeenLastCalledWith({ url: settingsUrl, method: 'PUT', data: { order_grab_qr: true } })
  expect(boxes.map(row => row.element.checked)).toEqual([true, false, true, true])
  expect(submit.element.disabled).toBe(true)
  expect(panel.get('[role="status"]').text()).toBe('系统通知规则已保存')
  page.unmount()
})

it('保存失败保留尚未保存的开关，并允许重试', async () => {
  const page = mount(V2InternalChatSettings)
  await flushPromises()
  const panel = page.get('section[aria-labelledby="chat-system-notices-title"]')
  await panel.get('#chat-system-notice-wecom_aftersales').setValue(false)
  send.mockRejectedValueOnce(new BizError(1000, '配置暂时无法保存'))
  await panel.get('form').trigger('submit')
  await flushPromises()
  expect(panel.get('[role="alert"]').text()).toBe('配置暂时无法保存')
  expect(panel.get<HTMLInputElement>('#chat-system-notice-wecom_aftersales').element.checked).toBe(false)
  expect(panel.get<HTMLButtonElement>('button[type="submit"]').element.disabled).toBe(false)

  send.mockResolvedValueOnce({ ...defaults, wecom_aftersales: false })
  await panel.get('form').trigger('submit')
  await flushPromises()
  expect(send).toHaveBeenLastCalledWith({ url: settingsUrl, method: 'PUT', data: { wecom_aftersales: false } })
  expect(panel.get('[role="status"]').text()).toBe('系统通知规则已保存')
  page.unmount()
})

it('聊天服务不支持系统通知时保留已配置的值并禁止保存', async () => {
  send.mockImplementation(async ({ url }) => {
    if (url === settingsUrl) return { ...defaults, order_grab_qr: true, available: false }
    if (url === '/api/internal-chat/settings/') return { primary_contact_id: null, backup_contact_ids: [], candidates: [] }
    throw new HttpError(404, '未开放')
  })
  const page = mount(V2InternalChatSettings)
  await flushPromises()
  const panel = page.get('section[aria-labelledby="chat-system-notices-title"]')
  const boxes = panel.findAll<HTMLInputElement>('input[type="checkbox"]')
  expect(boxes.map(row => row.element.checked)).toEqual([true, true, true, true])
  expect(boxes.every(row => row.element.disabled)).toBe(true)
  expect(panel.text()).toContain('当前聊天服务暂不支持发送系统通知')
  expect(panel.get<HTMLButtonElement>('button[type="submit"]').element.disabled).toBe(true)
  await panel.get('form').trigger('submit')
  expect(send.mock.calls.some(([config]) => config.method === 'PUT')).toBe(false)
  page.unmount()
})
