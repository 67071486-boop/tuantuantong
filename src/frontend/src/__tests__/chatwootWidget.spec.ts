// @vitest-environment jsdom
/**
 * 网页端 Chatwoot 小部件模块（`src/chatwootWidget.ts`）的判据。
 *
 * 计划：`docs/plans/2026-10-22-web-chatwoot-widget.md` §2.1 / §2.3 / §3（判据 1–5）。
 *
 * ## 本文件证什么（逐条都能失败）
 *
 * | # | 判据 |
 * |---|---|
 * | 1 | `mountChatwoot(user)` 只注入一次；`locale` 是 `zh_CN`（⛔ 不是 `zh-CN`）、`position: right`、`launcherTitle: 联系客服` |
 * | 2 | `setUser` 的 identifier 走 `chat_identifier`（⛔ 不是裸 `id`），并带 `identifier_hash` / 显示名 |
 * | 3 | 后端没下发公开配置（`chat_base_url` / `chat_website_token`）⇒ **不注入** SDK（fail-closed） |
 * | 4 | `reset()` 抛错 ⇒ 仍然先 `toggleBubbleVisibility('hide')`、且不把调用方带崩 |
 * | 5 | `$chatwoot` 不存在时登出路径**一律不抛**（store 的 `clear()` 不能被带崩） |
 * | 6 | 同页换人：登出 → 另一人登录 ⇒ 立即 `setUser` 新 identifier + `show`（不依赖 `chatwoot:ready`） |
 * | 7 | `chatwoot:ready` 晚于 `setUser` 的时机到达时补一次同步 |
 *
 * ## ⚠️ 为什么要 `vi.resetModules()`
 *
 * 模块里的「已注入 / 上一个用户 / SDK 是否就绪」是**模块级状态**（真实页面里本来就该这样：
 * 一个标签页只注入一次）。用例之间互不影响最省事的做法是每条重新加载模块；配置本身来自
 * `user` 载荷（单一真值源），所以换用户就能换配置。
 *
 * ⚠️ 旧模块实例注册的 `chatwoot:ready` 监听器会留在 `window` 上（DOM 事件没有「清空全部」）；
 * 模块自己会在重新绑定时摘掉上一个实例的监听器（`window.__chatwootReadyHandler`），
 * 用例因此不必手工清。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { User } from '@/api/auth'

const BASE_URL = 'https://chatwoot-web.example.test'
const WEBSITE_TOKEN = 'web-website-token-synthetic-0001'

function makeUser(overrides: Partial<User> = {}): User {
  return {
    id: 42,
    username: 'web-chat-user',
    display_name: '网页端测试账号',
    identity_plane: 'tenant_a',
    is_tenant_admin: false,
    roles: [],
    permissions: [],
    chat_identifier: 'web-tenant_a-42',
    chat_identifier_hash: 'a'.repeat(64),
    chat_base_url: BASE_URL,
    chat_website_token: WEBSITE_TOKEN,
    ...overrides,
  }
}

/** 造一个 `$chatwoot` 假体；`calls` 用来断言「隐藏 → reset」的先后。 */
function fakeChatwoot() {
  const calls: string[] = []
  const api = {
    setUser: vi.fn(),
    toggleBubbleVisibility: vi.fn((state: 'show' | 'hide') => {
      calls.push(`visible:${state}`)
    }),
    reset: vi.fn(() => {
      calls.push('reset')
    }),
  }
  return { api, calls }
}

let widget: typeof import('@/chatwootWidget')

/**
 * 把「SDK 已加载且 iframe 已就绪」这个真实前提摆好：`mountChatwoot(user)`（绑 ready 监听）
 * → 放上 `$chatwoot` → 派发 `chatwoot:ready`。之后清掉准备阶段产生的调用，
 * 让断言只看被测动作本身。
 */
function prepareReadyWidget(
  api: ReturnType<typeof fakeChatwoot>['api'],
  calls?: string[],
): void {
  widget.mountChatwoot(makeUser())
  ;(window as unknown as Record<string, unknown>).$chatwoot = api
  window.dispatchEvent(new Event('chatwoot:ready'))
  api.setUser.mockClear()
  api.toggleBubbleVisibility.mockClear()
  api.reset.mockClear()
  calls?.splice(0, calls.length)
}

beforeEach(async () => {
  vi.resetModules()
  document.head.innerHTML = ''
  document.body.innerHTML = ''
  delete (window as unknown as Record<string, unknown>).$chatwoot
  delete (window as unknown as Record<string, unknown>).chatwootSDK
  delete (window as unknown as Record<string, unknown>).chatwootSettings
  widget = await import('@/chatwootWidget')
})

const scripts = () =>
  Array.from(document.head.querySelectorAll<HTMLScriptElement>('script[data-chatwoot-widget]'))

describe('mountChatwoot', () => {
  it('动态注入 SDK 且只注入一次，locale 用 zh_CN（不是 zh-CN）', () => {
    const run = vi.fn()
    ;(window as unknown as Record<string, unknown>).chatwootSDK = { run }

    expect(widget.mountChatwoot(makeUser())).toBe(true)
    expect(widget.mountChatwoot(makeUser())).toBe(true)

    expect(scripts()).toHaveLength(1)
    const script = scripts()[0]
    if (!script) throw new Error('预期注入一个 SDK <script>')
    expect(script.src).toBe(`${BASE_URL}/packs/js/sdk.js`)
    // `launcherTitle` 只对 `expanded_bubble` 生效（计划 §4），但设置值必须是「联系客服」。
    expect((window as unknown as Record<string, unknown>).chatwootSettings).toEqual({
      locale: 'zh_CN',
      position: 'right',
      launcherTitle: '联系客服',
    })
    // run 必须等脚本加载完再调（SDK 源码里 `run` 是那个脚本定义的）。
    expect(run).not.toHaveBeenCalled()
    script.dispatchEvent(new Event('load'))
    expect(run).toHaveBeenCalledWith({ websiteToken: WEBSITE_TOKEN, baseUrl: BASE_URL })
  })

  it('脚本加载完但 iframe 未就绪时**不** setUser（否则 cookie 写死、ready 补发会失效）', () => {
    // 这是对端 SDK 的实测行为：`setUser` 先写 `cw_user_<token>` cookie，再 postMessage；
    // 同值的下一次调用会被 SDK 直接跳过 ⇒ 过早 setUser 会让联系人永远匿名。
    const { api } = fakeChatwoot()
    ;(window as unknown as Record<string, unknown>).chatwootSDK = {
      run: vi.fn(() => {
        ;(window as unknown as Record<string, unknown>).$chatwoot = api
      }),
    }
    widget.mountChatwoot(makeUser())

    const script = scripts()[0]
    if (!script) throw new Error('预期注入一个 SDK <script>')
    script.dispatchEvent(new Event('load')) // run() 已执行 ⇒ $chatwoot 存在，但 iframe 未就绪
    expect((window as unknown as Record<string, unknown>).$chatwoot).toBe(api)

    // 关键时机：SDK 已注入、iframe 还没 ready 时来了用户（例如登录/会话刷新）。
    widget.syncChatwootUser(makeUser())
    expect(api.setUser).not.toHaveBeenCalled()

    window.dispatchEvent(new Event('chatwoot:ready'))
    expect(api.setUser).toHaveBeenCalledWith(
      'web-tenant_a-42',
      expect.objectContaining({ identifier_hash: 'a'.repeat(64) }),
    )
  })

  it('后端没下发公开配置（缺任一个）⇒ 不注入 SDK（fail-closed）', () => {
    expect(widget.mountChatwoot(makeUser({ chat_base_url: undefined }))).toBe(false)
    expect(widget.mountChatwoot(makeUser({ chat_website_token: undefined }))).toBe(false)
    // 四个键同生同灭：后面这条是「后端未配置」的真实形状。
    expect(
      widget.mountChatwoot(
        makeUser({
          chat_identifier: undefined,
          chat_identifier_hash: undefined,
          chat_base_url: undefined,
          chat_website_token: undefined,
        }),
      ),
    ).toBe(false)
    expect(widget.mountChatwoot(null)).toBe(false)
    expect(scripts()).toHaveLength(0)
    expect((window as unknown as Record<string, unknown>).chatwootSettings).toBeUndefined()
  })
})

describe('syncChatwootUser', () => {
  it('setUser 用 chat_identifier 而不是裸 id，并带上 hash / 显示名', () => {
    const { api } = fakeChatwoot()
    prepareReadyWidget(api)

    widget.syncChatwootUser(
      makeUser({ display_name: '张三', avatar_url: 'https://img.example.test/a.png' }),
    )

    expect(api.setUser).toHaveBeenCalledTimes(1)
    const firstCall = api.setUser.mock.calls[0]
    if (!firstCall) throw new Error('预期调用过一次 setUser')
    const [identifier, attributes] = firstCall
    expect(identifier).toBe('web-tenant_a-42')
    // 承重：裸 id 会跨租户合并联系人（计划 §2.3-1）。
    expect(identifier).not.toBe('42')
    expect(attributes).toEqual({
      name: '张三',
      identifier_hash: 'a'.repeat(64),
      avatar_url: 'https://img.example.test/a.png',
    })
    expect(api.toggleBubbleVisibility).toHaveBeenCalledWith('show')
  })

  it('没有 chat_identifier（后端未配置）⇒ 不 setUser，退化路径也不留匿名会话', () => {
    const { api } = fakeChatwoot()
    prepareReadyWidget(api)

    widget.syncChatwootUser(
      makeUser({ chat_identifier: undefined, chat_identifier_hash: undefined }),
    )

    expect(api.setUser).not.toHaveBeenCalled()
    expect(api.toggleBubbleVisibility).toHaveBeenCalledWith('hide')
  })

  it('$chatwoot 不存在时登出 / 未就绪一律不抛（store 的 clear 不能被带崩）', () => {
    expect(() => widget.syncChatwootUser(null)).not.toThrow()
    expect(() => widget.unmountChatwoot()).not.toThrow()
  })

  it('同页换人：立即 setUser 新 identifier + show，不依赖 chatwoot:ready', () => {
    const { api } = fakeChatwoot()
    prepareReadyWidget(api)

    widget.syncChatwootUser(makeUser())
    widget.unmountChatwoot()
    // 这里**故意**不派发 `chatwoot:ready`（reset 之后它不再触发，计划 §2.1）。
    widget.syncChatwootUser(
      makeUser({
        id: 43,
        chat_identifier: 'web-tenant_a-43',
        chat_identifier_hash: 'b'.repeat(64),
        display_name: '李四',
      }),
    )

    expect(api.setUser).toHaveBeenLastCalledWith(
      'web-tenant_a-43',
      expect.objectContaining({ name: '李四', identifier_hash: 'b'.repeat(64) }),
    )
    const setUserOrder = api.setUser.mock.invocationCallOrder
    const showOrder = api.toggleBubbleVisibility.mock.invocationCallOrder
    const lastSetUser = setUserOrder[setUserOrder.length - 1]
    const lastShow = showOrder[showOrder.length - 1]
    if (lastSetUser === undefined || lastShow === undefined) {
      throw new Error('预期 setUser 与 toggleBubbleVisibility 都被调用过')
    }
    expect(lastShow).toBeGreaterThan(lastSetUser)
  })

  it('chatwoot:ready 到达后补一次同步（setUser 早于就绪的时机）', () => {
    widget.mountChatwoot(makeUser()) // SDK 还没就绪：此刻 $chatwoot 不存在
    widget.syncChatwootUser(makeUser())

    const { api } = fakeChatwoot()
    ;(window as unknown as Record<string, unknown>).$chatwoot = api
    window.dispatchEvent(new Event('chatwoot:ready'))

    expect(api.setUser).toHaveBeenCalledWith(
      'web-tenant_a-42',
      expect.objectContaining({ identifier_hash: 'a'.repeat(64) }),
    )
  })
})

describe('unmountChatwoot', () => {
  it('先隐藏、后 reset；reset 抛错也被吞掉，调用方不崩', () => {
    const { api, calls } = fakeChatwoot()
    api.reset.mockImplementation(() => {
      calls.push('reset')
      throw new Error('reset boom')
    })
    prepareReadyWidget(api, calls)

    expect(() => widget.unmountChatwoot()).not.toThrow()

    expect(calls).toEqual(['visible:hide', 'reset'])
  })

  it('隐藏自己抛错时 reset 仍会被尝试，且调用方不崩', () => {
    const { api } = fakeChatwoot()
    api.toggleBubbleVisibility.mockImplementation(() => {
      throw new Error('bubble holder not ready')
    })
    prepareReadyWidget(api)

    expect(() => widget.unmountChatwoot()).not.toThrow()
    expect(api.reset).toHaveBeenCalledTimes(1)
  })

  it('iframe 就绪前登出：只隐藏、不 reset（否则本标签页之后再也同步不了）', () => {
    // SDK 的 `reset()` 会置 `resetTriggered=true` ⇒ `chatwoot:ready` 不再触发。
    // 就绪前 reset 会把「首次同步必须等 ready」的闸永久焊死。
    const { api } = fakeChatwoot()
    ;(window as unknown as Record<string, unknown>).chatwootSDK = {
      run: vi.fn(() => {
        ;(window as unknown as Record<string, unknown>).$chatwoot = api
      }),
    }
    widget.mountChatwoot(makeUser())
    const script = scripts()[0]
    if (!script) throw new Error('预期注入一个 SDK <script>')
    script.dispatchEvent(new Event('load')) // 已 run，iframe 尚未 ready

    widget.syncChatwootUser(makeUser())
    widget.unmountChatwoot()
    expect(api.toggleBubbleVisibility).toHaveBeenCalledWith('hide')
    expect(api.reset).not.toHaveBeenCalled()

    // ready 到了：这时才允许 reset（清掉上一个会话的 cookie）。
    window.dispatchEvent(new Event('chatwoot:ready'))
    expect(api.reset).toHaveBeenCalledTimes(1)

    // 闸没有被焊死：ready 之后再换人登录，setUser + show 照常。
    api.setUser.mockClear()
    api.toggleBubbleVisibility.mockClear()
    widget.syncChatwootUser(makeUser({ chat_identifier: 'web-tenant_a-99' }))
    expect(api.setUser).toHaveBeenCalledWith('web-tenant_a-99', expect.anything())
    expect(api.toggleBubbleVisibility).toHaveBeenCalledWith('show')
  })
})
