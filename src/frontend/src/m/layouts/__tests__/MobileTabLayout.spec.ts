// @vitest-environment jsdom
import { installFramework7ForTest } from '../../styles/__tests__/framework7Setup'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { f7 } from 'framework7-vue'
import { ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/auth', () => ({ fetchMe: vi.fn(), login: vi.fn(), logout: vi.fn() }))
vi.mock('@/api/http', () => ({ request: vi.fn(), http: {} }))
const replace = vi.fn()
const push = vi.fn()
/** 当前路由 name（TabBar 的受控值来源）；每个用例自己设。 */
const routeName = ref('m-hall')
vi.mock('vue-router', () => ({
  useRouter: () => ({ replace, push, resolve: ({ name }: { name: string }) => ({ href: '/m/' + name.slice(2) }) }),
  useRoute: () => ({ get name() { return routeName.value }, get fullPath() { return '/' + routeName.value.slice(2) } }),
  RouterView: { name: 'RouterView', template: '<div class="m-routerview-stub" />' },
}))

import { logout } from '@/api/auth'
import { request } from '@/api/http'
import { useSessionStore } from '@/stores/session'
import MobileTabLayout from '../MobileTabLayout.vue'

const apiLogout = vi.mocked(logout)

const boosterOf = (menuPermissions: string[]) => ({
  id: 7,
  username: 'ceshidashoujia',
  display_name: '测试打手甲',
  identity_plane: 'tenant_a',
  is_tenant_admin: false,
  roles: [],
  permissions: [],
  menu_permissions: menuPermissions,
  tenant_name: '测试工作室甲',
})

installFramework7ForTest()
enableAutoUnmount(afterEach)

beforeEach(() => {
  setActivePinia(createPinia())
  apiLogout.mockReset()
  apiLogout.mockResolvedValue(null)
  vi.mocked(request).mockReset().mockResolvedValue({ binding_enabled: true, bound: false })
  replace.mockReset()
  push.mockReset()
  routeName.value = 'm-hall'
})

function mountLayout() {
  return mount(MobileTabLayout)
}

/** tab 集合判据：渲染出的 TabBarItem 文案**恰好等于**（⛔ 不用 toContain 放宽）。 */
function tabLabels(wrapper: ReturnType<typeof mount>): string[] {
  return wrapper.findAll('nav a').map((link) => link.get('.tabbar-label').text())
}

function emitFramework7Touch(type: 'touchstart' | 'touchmove', target: Element, x: number) {
  // jsdom 的合成触摸没有 isTrusted，直接把触点交给 Framework7 的触摸处理器。
  const app = f7 as typeof f7 & { emit: (event: 'touchstart' | 'touchmove', payload: unknown) => void }
  app.emit(type, {
    type, target, isTrusted: true, targetTouches: [{ pageX: x, pageY: 20 }], touches: [],
  })
}

function mountTabDrag(startX = 50, endX = 150) {
  const session = useSessionStore()
  session.status = 'ready'
  session.user = boosterOf(['grab_hall.take_order', 'my_orders', 'my_orders.send_to_my_system'])
  const wrapper = mount(MobileTabLayout, { attachTo: document.body })
  const links = wrapper.findAll('nav a.tab-link')
  links.forEach((link, index) => {
    vi.spyOn(link.element, 'getBoundingClientRect').mockReturnValue(new DOMRect(index * 100, 0, 100, 50))
  })
  vi.spyOn(wrapper.get('.toolbar-pane').element, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 300, 50))
  const target = links[Math.floor(startX / 100)]!.element
  const pointer = (type: string, x: number, y = 20) => target.dispatchEvent(new PointerEvent(type, {
    bubbles: true, pointerType: 'touch', isPrimary: true, pointerId: 1, clientX: x, clientY: y,
  }))
  emitFramework7Touch('touchstart', target, startX)
  pointer('pointerdown', startX)
  if (endX !== startX) {
    emitFramework7Touch('touchmove', target, endX)
    pointer('pointermove', endX)
  }
  return { wrapper, pointer }
}

async function waitForTabHighlight(): Promise<void> {
  await flushPromises()
  await new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))
}

describe('m 端壳（MobileTabLayout）', () => {
  it('底栏第三格是频道，绩效不再独占一格', () => {
    const session = useSessionStore()
    session.status = 'ready'
    session.user = boosterOf(['grab_hall.take_order', 'my_orders', 'my_orders.send_to_my_system', 'my_performance', 'my_performance.export', 'my_profile'])
    const wrapper = mountLayout()
    expect(tabLabels(wrapper)).toEqual(['接单大厅', '我的订单', '频道', '我的'])
    expect(wrapper.get('nav a[href="/m/channel"] .f7-icons').text()).toBe('bubble_left_bubble_right')
  })

  it('缺少我的资料权限时仍能进入频道', () => {
    const session = useSessionStore()
    session.status = 'ready'
    session.user = boosterOf(['grab_hall.take_order', 'my_orders', 'my_orders.send_to_my_system', 'my_performance', 'my_performance.export'])
    const wrapper = mountLayout()
    expect(tabLabels(wrapper)).toEqual(['接单大厅', '我的订单', '频道'])
  })

  it('缺少绩效权限时频道仍可见', () => {
    const session = useSessionStore()
    session.status = 'ready'
    session.user = boosterOf(['grab_hall.take_order', 'my_orders', 'my_orders.send_to_my_system'])
    const wrapper = mountLayout()
    expect(tabLabels(wrapper)).toEqual(['接单大厅', '我的订单', '频道'])
  })

  it('只有订单栏目权限时显示我的订单和频道', () => {
    const session = useSessionStore()
    session.status = 'ready'
    session.user = boosterOf(['my_orders', 'my_orders.send_to_my_system'])
    const wrapper = mountLayout()
    expect(tabLabels(wrapper)).toEqual(['我的订单', '频道'])
  })

  it('只有资料栏目权限时显示频道和我的', () => {
    const session = useSessionStore()
    session.status = 'ready'
    session.user = boosterOf(['my_profile'])
    const wrapper = mountLayout()
    expect(tabLabels(wrapper)).toEqual(['频道', '我的'])
  })

  it('只有绩效栏目权限时也可从我的订单打开绩效', () => {
    const session = useSessionStore()
    session.status = 'ready'
    session.user = boosterOf(['my_performance', 'my_performance.export'])
    const wrapper = mountLayout()
    expect(tabLabels(wrapper)).toEqual(['我的订单', '频道'])
  })

  it('只有大厅权限时显示大厅和频道', () => {
    const session = useSessionStore()
    session.status = 'ready'
    session.user = boosterOf(['grab_hall.take_order'])
    const wrapper = mountLayout()
    expect(tabLabels(wrapper)).toEqual(['接单大厅', '频道'])
  })

  it('只有栏目名权限时不显示无可用内容的我的订单', async () => {
    const session = useSessionStore()
    session.status = 'ready'
    session.user = boosterOf(['my_orders', 'my_performance'])
    const wrapper = mountLayout()
    await flushPromises()
    expect(tabLabels(wrapper)).toEqual(['频道'])
    expect(replace).not.toHaveBeenCalled()
  })

  it('取不到 me 时隐藏标签，由鉴权接管跳转', async () => {
    const wrapper = mountLayout() // 不设 user（unknown 态）
    await flushPromises()
    expect(tabLabels(wrapper)).toEqual([])
    expect(replace).not.toHaveBeenCalled()
  })

  it('会话失效时不抢占登录回跳', async () => {
    const session = useSessionStore()
    session.status = 'ready'
    session.user = boosterOf(['grab_hall.take_order'])
    const wrapper = mountLayout()
    session.clear()
    await flushPromises()
    expect(tabLabels(wrapper)).toEqual([])
    expect(replace).not.toHaveBeenCalled()
  })

  it('大厅右侧帮助打开 iOS 居中说明，去绑定进入既有消息通知页', async () => {
    const session = useSessionStore()
    session.status = 'ready'
    session.user = { ...boosterOf(['grab_hall.take_order', 'my_profile']), roles: ['代练员'] }
    const wrapper = mount(MobileTabLayout, { attachTo: document.body })
    const nav = wrapper.get('.m-navbar')
    expect(nav.get('.title h1').text()).toBe('接单大厅')
    expect(nav.text()).not.toContain('退出')
    const trigger = nav.get('.right button[aria-haspopup="dialog"]')
    expect(trigger.text()).toBe('如何快速抢单')
    await trigger.trigger('click')

    const popup = wrapper.get('.m-hall-help.popup.modal-in')
    const dialog = popup.get('[role="dialog"][aria-modal="true"]')
    expect(dialog.text()).toContain('开启实时新单通知')
    expect(dialog.text()).toContain('价格区间和关键词')
    expect(dialog.text()).toContain('选好要订阅的游戏')
    expect(dialog.text()).toContain('保存订阅')
    expect(push).not.toHaveBeenCalled()
    await dialog.get('button:last-child').trigger('click')
    expect(push).toHaveBeenCalledWith({ name: 'm-me-notifications' })
    expect(popup.classes()).not.toContain('modal-in')
  })

  it('绑定状态读取失败时可以看说明并进入消息通知', async () => {
    const session = useSessionStore()
    session.status = 'ready'
    session.user = { ...boosterOf(['grab_hall.take_order', 'my_profile']), roles: ['代练员'] }
    vi.mocked(request).mockRejectedValueOnce(new Error('状态读取失败'))
    const wrapper = mountLayout()
    await wrapper.get('.m-navbar__hall-help').trigger('click')
    await flushPromises()
    const help = wrapper.get('.m-hall-help.modal-in [role="dialog"]')
    expect(help.text()).toContain('保存订阅')
    expect(push).not.toHaveBeenCalled()
    await help.get('button:last-child').trigger('click')
    expect(push).toHaveBeenCalledWith({ name: 'm-me-notifications' })
  })

  it('工作室未开放绑定时显示说明和关闭按钮', async () => {
    const session = useSessionStore()
    session.status = 'ready'
    session.user = { ...boosterOf(['grab_hall.take_order', 'my_profile']), roles: ['代练员'] }
    vi.mocked(request).mockResolvedValueOnce({ binding_enabled: false, bound: true })
    const wrapper = mountLayout()
    await wrapper.get('.m-navbar__hall-help').trigger('click')
    await flushPromises()
    const help = wrapper.get('.m-hall-help.modal-in [role="dialog"]')
    expect(help.findAll('button').map((button) => button.text())).toEqual(['关闭'])
    expect(push).not.toHaveBeenCalled()
  })

  it.each(['返回大厅', '切换账号'])('查绑定时连点只读一次，%s后丢弃旧响应', async (change) => {
    const session = useSessionStore()
    session.status = 'ready'
    session.user = { ...boosterOf(['grab_hall.take_order', 'my_profile']), roles: ['代练员'] }
    let complete: (guide: { binding_enabled: boolean; bound: boolean }) => void = () => {}
    vi.mocked(request).mockReturnValueOnce(new Promise((resolve) => { complete = resolve }))
    const wrapper = mountLayout()
    await wrapper.get('.m-navbar__hall-help').trigger('click')
    await wrapper.get('.m-navbar__hall-help').trigger('click')
    expect(request).toHaveBeenCalledExactlyOnceWith({
      url: '/api/auth/wxpusher/guide', method: 'GET', skipUnauthorizedHandler: true, timeout: 5000,
    })

    if (change === '返回大厅') {
      routeName.value = 'm-channel'
      await flushPromises()
      routeName.value = 'm-hall'
    } else {
      session.user = { ...session.user!, identity_plane: 'tenant_synthetic_b' }
    }
    complete({ binding_enabled: true, bound: true })
    await flushPromises()
    expect(wrapper.find('.m-hall-help.modal-in').exists()).toBe(false)
    expect(push).not.toHaveBeenCalled()

    await wrapper.get('.m-navbar__hall-help').trigger('click')
    await flushPromises()
    expect(wrapper.get('.m-hall-help.modal-in').text()).toContain('去绑定')
  })

  it.each([
    { worker: true, permissions: ['grab_hall.take_order'] },
    { worker: false, permissions: ['grab_hall.take_order', 'my_profile'] },
  ])('缺少打手身份或我的资料权限时可阅读说明，但不显示去绑定', async ({ worker, permissions }) => {
    const session = useSessionStore()
    session.status = 'ready'
    session.user = { ...boosterOf(permissions), roles: worker ? ['代练员'] : [] }
    const wrapper = mountLayout()
    const trigger = wrapper.get('.m-navbar .right button[aria-haspopup="dialog"]')
    await trigger.trigger('click')
    const dialog = wrapper.get('.m-hall-help.popup.modal-in [role="dialog"]')
    expect(dialog.findAll('button').map((button) => button.text())).toEqual(['关闭'])
    await dialog.get('button').trigger('click')
    expect(trigger.attributes('aria-expanded')).toBe('false')
    expect(request).not.toHaveBeenCalled()
    expect(push).not.toHaveBeenCalled()
  })

  it('大厅内打开帮助后切去其他标签会关闭帮助和遮罩', async () => {
    const session = useSessionStore()
    session.status = 'ready'
    session.user = { ...boosterOf(['grab_hall.take_order', 'my_profile']), roles: ['代练员'] }
    const wrapper = mountLayout()
    await wrapper.get('.m-navbar .right button[aria-haspopup="dialog"]').trigger('click')
    expect(wrapper.find('.m-hall-help.popup.modal-in').exists()).toBe(true)

    routeName.value = 'm-channel'
    await flushPromises()
    expect(wrapper.find('.m-navbar .right button[aria-haspopup="dialog"]').exists()).toBe(false)
    expect(wrapper.find('.m-hall-help.popup.modal-in, .popup-backdrop.backdrop-in').exists()).toBe(false)
  })

  it.each([
    ['m-mine', '我的订单'],
    ['m-channel', '频道'],
  ])('%s 的页面标题在导航栏中，租户名、显示名和退出仍然可用', (name, label) => {
    const session = useSessionStore()
    session.status = 'ready'
    session.user = boosterOf(['grab_hall.take_order', 'my_orders', 'my_orders.send_to_my_system'])
    routeName.value = name
    const wrapper = mountLayout()
    expect(wrapper.get('header .navbar .title h1').text()).toBe(label)
    expect(wrapper.find('main h1').exists()).toBe(false)
    const text = wrapper.find('header').text()
    expect(text).toContain('测试工作室甲')
    expect(text).toContain('测试打手甲')
    expect(text).toContain('退出')
  })

  it('「我的」在导航栏显示标题，账号和退出入口留给页面本身', () => {
    const session = useSessionStore()
    session.status = 'ready'
    session.user = boosterOf(['my_profile'])
    routeName.value = 'm-me'
    const wrapper = mountLayout()
    expect(wrapper.get('header .navbar .title h1').text()).toBe('我的')
    expect(wrapper.find('main h1').exists()).toBe(false)
    expect(wrapper.find('header').text()).toContain('测试工作室甲')
    expect(wrapper.find('header').text()).not.toContain('测试打手甲')
    expect(wrapper.find('header button').exists()).toBe(false)
  })

  it('点击底栏标签进入对应路由', async () => {
    const session = useSessionStore()
    session.status = 'ready'
    session.user = boosterOf(['grab_hall.take_order', 'my_orders', 'my_orders.send_to_my_system'])
    const wrapper = mountLayout()
    await wrapper.get('nav a[href="/m/channel"]').trigger('click')
    await flushPromises()
    expect(replace).toHaveBeenCalledWith({ name: 'm-channel' })
  })

  it.each([[50, 150], [99, 101]])('把玻璃气泡从接单大厅拖到我的订单（%s 到 %s），松手后页面跟着切换一次', async (startX, endX) => {
    replace.mockImplementation(async ({ name }: { name: string }) => { routeName.value = name })
    const { wrapper, pointer } = mountTabDrag(startX, endX)
    const highlight = wrapper.get<HTMLElement>('.tab-link-highlight')
    expect(highlight.classes()).toContain('tab-link-highlight-pressed')
    expect(replace).not.toHaveBeenCalled()
    pointer('pointerup', endX)
    await waitForTabHighlight()

    expect(replace).toHaveBeenCalledExactlyOnceWith({ name: 'm-mine' })
    expect(wrapper.get('header .navbar .title h1').text()).toBe('我的订单')
    expect(wrapper.get('nav a[aria-current="page"]').attributes('href')).toBe('/m/mine')
    expect(highlight.element.style.transform).toContain('100%')
  })

  it('手指静止轻点我的订单仍通过标签切换一次', async () => {
    const { pointer } = mountTabDrag(150, 150)
    pointer('pointerup', 150)
    await flushPromises()

    expect(replace).toHaveBeenCalledExactlyOnceWith({ name: 'm-mine' })
  })

  it('第一根手指按住大厅时，第二根手指在底栏按下和松开不抢占切页', async () => {
    replace.mockImplementation(async ({ name }: { name: string }) => { routeName.value = name })
    const { wrapper, pointer } = mountTabDrag(50, 50)
    const orders = wrapper.get('nav a[href="/m/mine"]').element
    emitFramework7Touch('touchstart', orders, 150)
    for (const [type, x] of [['pointerdown', 150], ['pointermove', 152], ['pointerup', 152]] as const) {
      orders.dispatchEvent(new PointerEvent(type, {
        bubbles: true, pointerType: 'touch', isPrimary: false, pointerId: 2, clientX: x, clientY: 20,
      }))
    }
    await flushPromises()
    expect(replace).not.toHaveBeenCalled()
    expect(wrapper.get('.tab-link-highlight').classes()).toContain('tab-link-highlight-pressed')

    pointer('pointermove', 150)
    pointer('pointerup', 150)
    await waitForTabHighlight()

    expect(replace).toHaveBeenCalledExactlyOnceWith({ name: 'm-mine' })
    expect(wrapper.get('nav a[aria-current="page"]').attributes('href')).toBe('/m/mine')
  })

  it('第二根手指在底栏外触碰时取消拖动，页面仍收到该触碰', async () => {
    const { wrapper, pointer } = mountTabDrag()
    const main = wrapper.get('main').element
    const pagePointer = vi.fn()
    main.addEventListener('pointerdown', pagePointer)
    main.dispatchEvent(new PointerEvent('pointerdown', {
      bubbles: true, pointerType: 'touch', isPrimary: false, pointerId: 2,
    }))
    pointer('pointerup', 150)
    await waitForTabHighlight()

    expect(pagePointer).toHaveBeenCalledTimes(1)
    expect(replace).not.toHaveBeenCalled()
    expect(wrapper.get('nav a[aria-current="page"]').attributes('href')).toBe('/m/hall')
  })

  it('第二根手指按住频道时，静止在我的订单的第一根手指松开仍切到我的订单', async () => {
    const { wrapper, pointer } = mountTabDrag(150, 150)
    const channel = wrapper.get('nav a[href="/m/channel"]').element
    emitFramework7Touch('touchstart', channel, 250)
    channel.dispatchEvent(new PointerEvent('pointerdown', {
      bubbles: true, pointerType: 'touch', isPrimary: false, pointerId: 2, clientX: 250, clientY: 20,
    }))
    pointer('pointerup', 150)
    await waitForTabHighlight()

    expect(replace).toHaveBeenCalledExactlyOnceWith({ name: 'm-mine' })
  })

  it.each([
    ['pointercancel', 20],
    ['lostpointercapture', 20],
    ['pointerup', 80],
  ])('拖动在未选中目标时结束（%s，y=%s），页面和高亮回到大厅', async (event, y) => {
    const { wrapper, pointer } = mountTabDrag()
    pointer(event, 150, y)
    if (event === 'lostpointercapture') pointer('pointerup', 150)
    await waitForTabHighlight()

    expect(replace).not.toHaveBeenCalled()
    expect(wrapper.get('nav a[aria-current="page"]').attributes('href')).toBe('/m/hall')
    expect(wrapper.get<HTMLElement>('.tab-link-highlight').element.style.transform).toMatch(/translate(?:X|3d)\(0%/)
  })

  it('拖到我的订单但路由未切换，高亮仍以实际页面为准', async () => {
    const { wrapper, pointer } = mountTabDrag()
    pointer('pointerup', 150)
    await waitForTabHighlight()

    expect(replace).toHaveBeenCalledExactlyOnceWith({ name: 'm-mine' })
    expect(wrapper.get('nav a[aria-current="page"]').attributes('href')).toBe('/m/hall')
    expect(wrapper.get<HTMLElement>('.tab-link-highlight').element.style.transform).toMatch(/translate(?:X|3d)\(0%/)
  })

  it('我的订单保留退出，结束会话后跳 m-login', async () => {
    const session = useSessionStore()
    session.status = 'ready'
    session.user = boosterOf(['my_orders', 'my_orders.send_to_my_system'])
    routeName.value = 'm-mine'
    const wrapper = mountLayout()
    await wrapper.get('header .m-navbar__exit').trigger('click')
    await flushPromises()
    expect(apiLogout).toHaveBeenCalledTimes(1)
    expect(session.status).toBe('guest') // logout() 在发请求前就把本地终结为 guest
    expect(replace).toHaveBeenCalledWith({ name: 'm-login' })
  })

  it('底栏从当前路由标记所在页面', () => {
    const session = useSessionStore()
    session.status = 'ready'
    session.user = boosterOf(['grab_hall.take_order', 'my_orders', 'my_orders.send_to_my_system'])
    routeName.value = 'm-mine'
    const wrapper = mountLayout()
    expect(wrapper.get('nav a[aria-current="page"]').attributes('href')).toBe('/m/mine')
  })
})
