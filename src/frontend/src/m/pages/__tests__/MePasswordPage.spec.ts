// @vitest-environment jsdom
import { installFramework7ForTest } from '../../styles/__tests__/framework7Setup'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import type { AxiosRequestConfig } from 'axios'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/http', () => ({ request: vi.fn(), http: {} }))

const replace = vi.fn()
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn(), replace, back: vi.fn() }) }))

vi.mock('../../framework7Ui', () => ({
  Toast: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn(), warning: vi.fn() }),
}))

import { Toast } from '../../framework7Ui'
import type { Mock } from 'vitest'
import { BizError } from '@/api/errors'
import { request } from '@/api/http'
import { useSessionStore } from '@/stores/session'
import MePasswordPage from '../MePasswordPage.vue'

const requestMock = request as unknown as Mock
const toast = Toast as unknown as Mock & { success: Mock; error: Mock }

enableAutoUnmount(afterEach)

installFramework7ForTest()

beforeEach(() => {
  setActivePinia(createPinia())
  requestMock.mockReset()
  replace.mockClear()
  toast.mockClear()
  toast.error.mockClear()
  requestMock.mockImplementation((config: AxiosRequestConfig) => {
    const url = String(config.url)
    const method = String(config.method).toLowerCase()
    if (url === '/api/auth/password' && method === 'post') return Promise.resolve(null)
    if (url === '/api/auth/logout' && method === 'post') return Promise.resolve(null)
    throw new Error(`spec: 未预期的请求 ${method} ${url}`)
  })
})

function mountPage() {
  return mount(MePasswordPage, { attachTo: document.body })
}

async function fill(
  wrapper: ReturnType<typeof mount>,
  oldPassword: string,
  newPassword: string,
  confirmPassword: string,
): Promise<void> {
  await wrapper.find('[data-testid="password-old"] input').setValue(oldPassword)
  await wrapper.find('[data-testid="password-new"] input').setValue(newPassword)
  await wrapper.find('[data-testid="password-confirm"] input').setValue(confirmPassword)
}

describe('修改密码（/m/me/password）', () => {
  it('label 逐字照桌面「修改密码」页签（user-myinfo.html L387-401）', () => {
    const wrapper = mountPage()
    const labels = wrapper.findAll('[data-testid="password-form"] label').map((w) => w.text())
    expect(labels).toEqual(['原登录密码', '新登录密码', '确认新密码'])
    expect(wrapper.find('[data-testid="password-old"] input').attributes('placeholder')).toBe(
      '请输入当前登录密码',
    )
    expect(wrapper.find('[data-testid="password-new"] input').attributes('placeholder')).toBe(
      '请输入新的登录密码',
    )
    expect(wrapper.find('[data-testid="password-confirm"] input').attributes('placeholder')).toBe(
      '请重复输入一遍新密码',
    )
  })

  it('长度规则（<6）：toast「新密码长度需为 6–18 位」，零请求', async () => {
    const wrapper = mountPage()
    await fill(wrapper, 'old123', '12345', '12345')
    await wrapper.find('[data-testid="password-submit"]').trigger('click')
    expect(toast.error).toHaveBeenCalledWith('新密码长度需为 6–18 位')
    expect(requestMock).not.toHaveBeenCalled()
  })

  it('长度规则（>18，setValue 直塞绕过 maxlength 同样被拦）：同一句，零请求', async () => {
    const wrapper = mountPage()
    await fill(wrapper, 'old123', '1234567890123456789', '1234567890123456789')
    await wrapper.find('[data-testid="password-submit"]').trigger('click')
    expect(toast.error).toHaveBeenCalledWith('新密码长度需为 6–18 位')
    expect(requestMock).not.toHaveBeenCalled()
  })

  it('两次不一致：toast「两次输入的新密码不一致」，零请求', async () => {
    const wrapper = mountPage()
    await fill(wrapper, 'old123', 'abcdef1', 'abcdef2')
    await wrapper.find('[data-testid="password-submit"]').trigger('click')
    expect(toast.error).toHaveBeenCalledWith('两次输入的新密码不一致')
    expect(requestMock).not.toHaveBeenCalled()
  })

  it('新旧相同：toast「新密码不能与原密码相同」，零请求', async () => {
    const wrapper = mountPage()
    await fill(wrapper, 'abcdef1', 'abcdef1', 'abcdef1')
    await wrapper.find('[data-testid="password-submit"]').trigger('click')
    expect(toast.error).toHaveBeenCalledWith('新密码不能与原密码相同')
    expect(requestMock).not.toHaveBeenCalled()
  })

  it('成功：POST /api/auth/password 恰好三键 + toast「修改成功」+ logout + 跳 m-login', async () => {
    const wrapper = mountPage()
    await fill(wrapper, 'oldpass1', 'newpass1', 'newpass1')
    await wrapper.find('[data-testid="password-submit"]').trigger('click')
    await flushPromises()

    const posts = requestMock.mock.calls
      .map((c) => c[0] as AxiosRequestConfig)
      .filter((c) => String(c.url) === '/api/auth/password')
    expect(posts).toHaveLength(1)
    expect(posts[0]!.data).toEqual({
      old_password: 'oldpass1',
      new_password: 'newpass1',
      confirm_password: 'newpass1',
    })
    expect(toast).toHaveBeenCalledWith('修改成功')
    const session = useSessionStore()
    expect(session.status).toBe('guest')
    expect(replace).toHaveBeenCalledWith({ name: 'm-login' })
  })

  it('失败（原密码不正确）：toast = 后端 msg 原样、不跳登录、表单保留', async () => {
    requestMock.mockImplementation((config: AxiosRequestConfig) => {
      const url = String(config.url)
      if (url === '/api/auth/password') return Promise.reject(new BizError(1000, '原密码不正确'))
      throw new Error(`spec: 未预期的请求 ${url}`)
    })
    const wrapper = mountPage()
    await fill(wrapper, 'wrong1', 'newpass1', 'newpass1')
    await wrapper.find('[data-testid="password-submit"]').trigger('click')
    await flushPromises()
    expect(toast.error).toHaveBeenCalledWith('原密码不正确')
    expect(toast).not.toHaveBeenCalledWith('修改成功')
    expect(replace).not.toHaveBeenCalled()
    expect((wrapper.find('[data-testid="password-old"] input').element as HTMLInputElement).value)
      .toBe('wrong1')
  })
})
