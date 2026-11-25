// @vitest-environment jsdom
import { installFramework7ForTest } from '../../styles/__tests__/framework7Setup'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import type { AxiosRequestConfig } from 'axios'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// 传输层是唯一的桩边界：myProfile 跑真代码（聚合页只 GET /api/auth/profile 拿实名徽标）。
vi.mock('@/api/http', () => ({ request: vi.fn(), http: {} }))

import type { Mock } from 'vitest'
import { afterEach } from 'vitest'
import { request } from '@/api/http'
import type { User } from '@/api/auth'
import { useSessionStore } from '@/stores/session'
import MePage from '../MePage.vue'

const requestMock = request as unknown as Mock

enableAutoUnmount(afterEach)

/* ── 会话夹具（合成值，E-0077） ── */

function loginAs(overrides: Partial<User> = {}, menuPermissions: string[] = ['my_profile', 'home.feedback']): void {
  const session = useSessionStore()
  session.status = 'ready'
  session.user = {
    id: 7,
    username: 'ceshidashoujia',
    display_name: '测试打手甲',
    identity_plane: 'tenant_a',
    is_tenant_admin: false,
    roles: ['代练员'],
    permissions: [],
    menu_permissions: menuPermissions,
    tenant_name: '测试工作室甲',
    avatar_url: null,
    ...overrides,
  } as User
}

/* ── mock 的请求路由 ── */

let profileImpl: (config: AxiosRequestConfig) => Promise<unknown>

installFramework7ForTest()

beforeEach(() => {
  setActivePinia(createPinia())
  requestMock.mockReset()
  // 默认：未实名、开关关闭（各用例自覆盖）。
  profileImpl = () =>
    Promise.resolve({
      profile: {
        username: 'ceshidashoujia',
        display_name: '测试打手甲',
        role: '代练员',
        id_card_no: '',
        is_real_name: false,
        realname: { enabled: false, remaining: null },
        base_salary: '0.00',
        alipay_account: '', alipay_name: '', bank_name: '', bank_card_no: '',
        qq: '', mobile: '', live_url: '',
        games: [101],
        avatar_url: null,
        wechat_qr_url: null,
      },
    })
  requestMock.mockImplementation((config: AxiosRequestConfig) => {
    const url = String(config.url)
    const method = String(config.method).toLowerCase()
    if (url === '/api/auth/profile' && method === 'get') return profileImpl(config)
    // 退出登录走会话的 logout()（POST /api/auth/logout，幂等）。
    if (url === '/api/auth/logout' && method === 'post') return Promise.resolve(null)
    throw new Error(`spec: 未预期的请求 ${method} ${url}`)
  })
})

const menuRoutes = [
  { path: '/me/profile', name: 'm-me-profile' },
  { path: '/me/password', name: 'm-me-password' },
  { path: '/me/notifications', name: 'm-me-notifications' },
  { path: '/me/feedback', name: 'm-me-feedback' },
]

function mountMe() {
  const router = createRouter({
    history: createMemoryHistory('/m/'),
    routes: [
      { path: '/', name: 'm-home' },
      { path: '/me', name: 'm-me' },
      { path: '/login', name: 'm-login' },
      ...menuRoutes,
    ].map((route) => ({ ...route, component: { render: () => null } })),
  })
  void router.push({ name: 'm-me' })
  return { wrapper: mount(MePage, { global: { plugins: [router] } }), router }
}

describe('「我的」聚合页（/m/me）', () => {
  it('身份卡：显示名 / 账号名 / 头像色块（无 avatar_url ⇒ data URI）；实名徽标取 /profile 的 is_real_name', async () => {
    loginAs()
    const { wrapper } = mountMe()
    await flushPromises()
    expect(wrapper.find('[data-testid="me-display-name"]').text()).toBe('测试打手甲')
    expect(wrapper.find('[data-testid="me-username"]').text()).toBe('ceshidashoujia')
    expect(wrapper.find('[data-testid="me-avatar"]').attributes('src')).toMatch(/^data:/)
    expect(requestMock).toHaveBeenCalledTimes(1)
    const config = requestMock.mock.calls[0]![0] as AxiosRequestConfig
    expect(config.url).toBe('/api/auth/profile')
    // 未实名 ⇒ 红色「未实名」徽标。
    expect(wrapper.find('[data-testid="me-realname-badge"]').text()).toBe('未实名')
  })

  it('已实名 ⇒ 徽标带 ✓ 与「已通过实名认证」；头像**优先用 /profile 的**（比会话快照新）', async () => {
    // 会话快照是旧头像（b.jpg）；资料里已经是新头像（a.jpg）⇒ 页面显示 a。
    loginAs({ avatar_url: 'https://avatar-synth.test.invalid/b.jpg' })
    profileImpl = () =>
      Promise.resolve({
        profile: {
          username: 'ceshidashoujia', display_name: '测试打手甲', role: '代练员',
          id_card_no: '110********00', is_real_name: true,
          realname: { enabled: true, remaining: 3 },
          base_salary: '0.00', alipay_account: '', alipay_name: '', bank_name: '',
          bank_card_no: '', qq: '', mobile: '', live_url: '', games: [],
          avatar_url: 'https://avatar-synth.test.invalid/a.jpg', wechat_qr_url: null,
        },
      })
    const { wrapper } = mountMe()
    await flushPromises()
    expect(wrapper.find('[data-testid="me-realname-badge"]').text()).toBe('✓ 已通过实名认证')
    // profile.avatar_url 优先于会话载荷（评审 Low-1：换完头像回聚合页要看到新的）。
    expect(wrapper.find('[data-testid="me-avatar"]').attributes('src')).toBe(
      'https://avatar-synth.test.invalid/a.jpg',
    )
    expect(wrapper.find('[data-testid="me-avatar"]').attributes('src')).not.toBe(
      'https://avatar-synth.test.invalid/b.jpg',
    )
  })

  it('资料取数失败 ⇒ 实名徽标**不渲染**（fail-closed：拿不到就不假装是哪一态）', async () => {
    loginAs()
    profileImpl = () => Promise.reject(new Error('网络异常，请检查连接'))
    const { wrapper } = mountMe()
    await flushPromises()
    expect(wrapper.find('[data-testid="me-realname-badge"]').exists()).toBe(false)
    // 身份卡与其余页面照常渲染（失败只影响徽标）。
    expect(wrapper.find('[data-testid="me-display-name"]').exists()).toBe(true)
  })

  it('菜单：有 home.feedback 才渲染反馈入口', async () => {
    loginAs()
    let { wrapper } = mountMe()
    await flushPromises()
    expect(wrapper.findAll('[data-testid="me-menu"] .item-title').map((w) => w.text().trim())).toEqual([
      '我的资料',
      '修改密码',
      '消息通知',
      '问题反馈',
      '下载 App（安卓）',
    ])
    wrapper.unmount()

    loginAs({}, ['my_profile'])
    wrapper = mountMe().wrapper
    await flushPromises()
    expect(wrapper.findAll('[data-testid="me-menu"] .item-title').map((w) => w.text().trim())).toEqual([
      '我的资料',
      '修改密码',
      '消息通知',
      '下载 App（安卓）',
    ])
  })

  it('即使未开通消息通知与反馈，也保留一个安卓 App 下载入口', async () => {
    loginAs({}, [])
    const { wrapper } = mountMe()
    await flushPromises()

    const downloads = wrapper.findAll('a[href="/download/"]')
    expect(downloads).toHaveLength(1)
    expect(downloads[0]?.get('.item-title').text()).toBe('下载 App（安卓）')
  })

  it.each([
    [{ roles: ['系统管理员'], is_tenant_admin: true }, ['my_profile']],
    [{ roles: ['代练员'] }, []],
  ] satisfies [Partial<User>, string[]][])('不是代练员或没有「我的」权限时不显示消息通知入口', async (user, nodes) => {
    loginAs(user, nodes)
    const { wrapper } = mountMe()
    await flushPromises()
    expect(wrapper.find('[data-testid="me-entry-notifications"]').exists()).toBe(false)
  })

  it('各设置入口显示正确地址，点击后进入对应子页面', async () => {
    loginAs()
    const { wrapper, router } = mountMe()
    await flushPromises()
    for (const { name, path } of menuRoutes) {
      const key = name.slice('m-me-'.length)
      const link = wrapper.get(`[data-testid="me-entry-${key}"] a`)
      expect(link.attributes('href')).toBe(`/m${path}`)
      await link.trigger('click')
      await flushPromises()
      expect(router.currentRoute.value.name).toBe(name)
    }
  })

  it('退出登录：session.logout()（含服务端调用）后 replace 回 m-login', async () => {
    loginAs()
    const session = useSessionStore()
    const { wrapper, router } = mountMe()
    await flushPromises()
    await wrapper.find('[data-testid="me-logout"]').trigger('click')
    await flushPromises()
    expect(session.status).toBe('guest')
    expect(router.currentRoute.value.name).toBe('m-login')
  })
})
