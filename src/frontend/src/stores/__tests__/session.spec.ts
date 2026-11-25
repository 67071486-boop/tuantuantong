// @vitest-environment jsdom
/** 会话 store 的判据测试。⚠️ **环境是 jsdom**（线 A 修复轮 P1-1 起）：`clear()` 要真的清掉`sessionStorage` 里的「手机端抑制标记」，而 `node` 环境里没有 `sessionStorage`⇒ 默认实现会静默退化成「什么都没清」，那几条用例就成了**假绿**。 */
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { setActivePinia, createPinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/auth', () => ({
  fetchMe: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
}))

import { fetchMe, login as apiLogin, logout as apiLogout, type User } from '@/api/auth'
import { AuthExpiredError, NetworkError } from '@/api/errors'
import {
  clearMobileRedirectSuppressed,
  readMobileRedirectSuppressed,
  redirectToMobileIfMobile,
  writeMobileRedirectSuppressed,
} from '@/mobileRedirect'
import { WORKER_ROLE, useSessionStore } from '../session'

const me = vi.mocked(fetchMe)
const apiLoginMock = vi.mocked(apiLogin)
const apiLogoutMock = vi.mocked(apiLogout)

const HERE = path.dirname(fileURLToPath(import.meta.url))
/** `src/frontend/src/stores/__tests__` → 仓库 `src/apps/business/roles.py`（后端 `WORKER` 的出处）。 */
const BACKEND_ROLES_PY = path.resolve(HERE, '../../../../apps/business/roles.py')

const IPHONE_UA =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'

const kefu = {
  id: 7,
  username: 'kefu',
  display_name: '小客服',
  identity_plane: 'tenant_a',
  is_tenant_admin: false,
  roles: ['客服'],
  permissions: ['business.add_game'],
  menu_permissions: ['order.list', 'my_orders'],
}

beforeEach(() => {
  setActivePinia(createPinia())
  me.mockReset()
  apiLoginMock.mockReset()
  apiLogoutMock.mockReset()
  // 抑制标记住在真 sessionStorage 里 ⇒ 每个用例自己收口。
  window.sessionStorage.clear()
})

describe('会话状态机', () => {
  it('unknown → loading → ready', async () => {
    const store = useSessionStore()
    expect(store.status).toBe('unknown')
    me.mockResolvedValue(kefu)
    const p = store.load()
    expect(store.status).toBe('loading')
    await p
    expect(store.status).toBe('ready')
    expect(store.user?.username).toBe('kefu')
  })

  it('401 → 静默 guest，不抛错、不记 error', async () => {
    const store = useSessionStore()
    me.mockRejectedValue(new AuthExpiredError('未认证'))
    await store.load()
    expect(store.status).toBe('guest')
    expect(store.user).toBeNull()
    expect(store.consumeLoadError()).toBeNull()
  })

  it('网络错误 → guest 且留下一次性 error（不得停在 loading）', async () => {
    const store = useSessionStore()
    me.mockRejectedValue(new NetworkError('网络异常，请检查连接'))
    await store.load()
    expect(store.status).toBe('guest')
    expect(store.consumeLoadError()).toBeInstanceOf(NetworkError)
    expect(store.consumeLoadError()).toBeNull() // 只消费一次
  })

  it('clear() 丢弃未消费的引导错误（不漏给下一次消费）', async () => {
    const store = useSessionStore()
    me.mockRejectedValue(new NetworkError('网络异常，请检查连接'))
    await store.load()
    store.clear()
    expect(store.consumeLoadError()).toBeNull()
  })

  it('login 成功后丢弃旧的引导错误', async () => {
    const store = useSessionStore()
    me.mockRejectedValue(new NetworkError('网络异常，请检查连接'))
    await store.load()
    apiLoginMock.mockResolvedValue(kefu)
    await store.login('kefu', 'pw')
    expect(store.status).toBe('ready')
    expect(store.consumeLoadError()).toBeNull()
  })

  it('load 单飞：并发两次只发一次请求', async () => {
    const store = useSessionStore()
    me.mockResolvedValue(kefu)
    await Promise.all([store.load(), store.load()])
    expect(me).toHaveBeenCalledTimes(1)
  })

  it('clear() 的终态是 guest（不是 unknown）', async () => {
    const store = useSessionStore()
    me.mockResolvedValue(kefu)
    await store.load()
    store.clear()
    expect(store.status).toBe('guest')
    expect(store.user).toBeNull()
  })

  it('代数：在途 me 的结果不能跨 clear() 把状态写回 ready', async () => {
    const store = useSessionStore()
    let release!: (u: User) => void
    me.mockImplementation(() => new Promise<User>((resolve) => { release = resolve }))
    const p = store.load()
    store.clear()
    release(kefu)
    await p
    expect(store.status).toBe('guest')
    expect(store.user).toBeNull()
  })

  it('代数：在途 me 的失败结果不能跨 login() 把 ready 清成 guest', async () => {
    const store = useSessionStore()
    let reject!: (e: unknown) => void
    me.mockImplementation(() => new Promise<User>((_resolve, rej) => { reject = rej }))
    const p = store.load()
    // 在途 me 未落地期间登录成功：epoch 被 bump，在途结果必须被丢弃
    apiLoginMock.mockResolvedValue(kefu)
    await store.login('kefu', 'pw')
    expect(store.status).toBe('ready')
    reject(new AuthExpiredError('未认证'))
    await p
    expect(store.status).toBe('ready')
    expect(store.user?.username).toBe('kefu')
  })

  it('load 到达终态后不再重复请求', async () => {
    const store = useSessionStore()
    me.mockResolvedValue(kefu)
    await store.load()
    await store.load()
    expect(me).toHaveBeenCalledTimes(1)
  })

  it('login 失败不改 status（留在 guest）', async () => {
    const store = useSessionStore()
    me.mockRejectedValue(new AuthExpiredError('未认证'))
    await store.load()
    apiLoginMock.mockRejectedValue(new Error('用户名或密码错误'))
    await expect(store.login('kefu', 'bad')).rejects.toThrow('用户名或密码错误')
    expect(store.status).toBe('guest')
  })

  it('logout 先终结本地，且服务端失败不回滚', async () => {
    const store = useSessionStore()
    me.mockResolvedValue(kefu)
    await store.load()
    apiLogoutMock.mockRejectedValue(new NetworkError('断网'))
    await store.logout()
    expect(store.status).toBe('guest')
    expect(store.user).toBeNull()
  })

  it('logout 先终结本地，再发请求（对顺序敏感）', async () => {
    const store = useSessionStore()
    me.mockResolvedValue(kefu)
    await store.load()
    let release!: (v: null) => void
    apiLogoutMock.mockImplementation(() => new Promise<null>((resolve) => { release = resolve }))

    const p = store.logout()
    // 请求仍在途 —— 本地必须**已经**终结（spec §4.1-4）
    expect(store.status).toBe('guest')
    expect(store.user).toBeNull()

    release(null)
    await p
    expect(apiLogoutMock).toHaveBeenCalled()
  })

  /** `refresh()`：顶栏头像同步线新增的取数入口。
   * 与 `load()` 的**关键差异**：`load()` 到 `ready` 后短路，`refresh()` 必须**再取一次**
   * （它的用途就是「已经 ready、但资料在别处变了」）。链接是**合成形状**，⛔ 不是真值。 */
  it('refresh() 在 ready 之后仍然重新取一次，并换上新的 avatar_url', async () => {
    const store = useSessionStore()
    me.mockResolvedValue(kefu)
    await store.load()
    me.mockResolvedValue({
      ...kefu, avatar_url: '/api/media/blob/test/users/9/avatar.test.png',
    })
    await store.refresh()
    // 判据钉**值**：确实是第二次取回的那一个（⛔ 不是 `load` 那次）。
    expect(store.user?.avatar_url).toBe('/api/media/blob/test/users/9/avatar.test.png')
    expect(me).toHaveBeenCalledTimes(2)
  })

  it('refresh() 失败**不动**已有会话（不把已登录的人踢成 guest）', async () => {
    const store = useSessionStore()
    me.mockResolvedValue(kefu)
    await store.load()
    me.mockRejectedValue(new NetworkError('网络异常，请检查连接'))
    await store.refresh()
    expect(store.status).toBe('ready')
    expect(store.user?.username).toBe('kefu')
  })

  it('代数：在途 refresh 的结果不能跨 clear() 把状态写回 ready', async () => {
    const store = useSessionStore()
    me.mockResolvedValue(kefu)
    await store.load()
    let release!: (u: User) => void
    me.mockImplementation(() => new Promise<User>((resolve) => { release = resolve }))
    const p = store.refresh()
    store.clear()
    release(kefu)
    await p
    expect(store.status).toBe('guest')
    expect(store.user).toBeNull()
  })

  /** ⭐ **Minor-8（2026-09-25 评审修复）：`refresh()` 的在途去重。**正常路径只发一次，但同源脚本可以连续 `postMessage`（壳收到 island 的「头像已更新」就调`refresh()`）⇒ 不去重的话**每一次都发一条** `/api/auth/me`。判据：同一轮里连续两次 `refresh()` ⇒ 只发**一次**请求（第二次复用同一条在途 promise）。 */
  it('refresh 单飞：连续两次只发一次 /api/auth/me（复用同一条在途 promise）', async () => {
    const store = useSessionStore()
    me.mockResolvedValue(kefu)
    await store.load()
    me.mockClear()
    const resolvers: Array<(u: User) => void> = []
    me.mockImplementation(() => new Promise<User>((resolve) => { resolvers.push(resolve) }))
    const p1 = store.refresh()
    const p2 = store.refresh()
    // 变异：不在途去重（每次都新建一条）⇒ 这里收到 2 条 ⇒ 红。
    expect({ requests: me.mock.calls.length }).toEqual({ requests: 1 })
    resolvers.forEach((resolve) => resolve(kefu))
    await Promise.all([p1, p2])
    expect(store.user?.username).toBe('kefu')
  })

  it('refresh 单飞不改变失败语义：在途那条失败后，下一次刷新仍会真的再发一次', async () => {
    const store = useSessionStore()
    me.mockResolvedValue(kefu)
    await store.load()
    me.mockClear()
    me.mockRejectedValue(new NetworkError('网络异常，请检查连接'))
    await Promise.all([store.refresh(), store.refresh()])
    // 单飞：两次调用共享**同一条**失败（只发一次请求）。
    expect({ requests: me.mock.calls.length }).toEqual({ requests: 1 })
    // 失败语义不变：旧会话保留（不踢成 guest、不记 error）。
    expect(store.status).toBe('ready')
    expect(store.user?.username).toBe('kefu')
    expect(store.consumeLoadError()).toBeNull()
    // 「该 clear 的还 clear」：失败那条已从在途里出清 ⇒ 下一次刷新**真的**再发一次，
    // 而不是被一条永远挂在在途的 promise 卡成静默 no-op。
    me.mockResolvedValue({ ...kefu, display_name: '小客服乙' })
    await store.refresh()
    expect({ requests: me.mock.calls.length }).toEqual({ requests: 2 })
    expect(store.user?.display_name).toBe('小客服乙')
  })

  /** ⭐ **Minor-5（2026-09-25 评审修复）：在途 `refresh()` 必须随 `clear()` / `login()` 出清。**症状：一条 refresh 在途时发生 login（或 logout ⇒ `clear()`），若不出清那条在途 promise，紧接着的 `refresh()` 会**复用它**（单飞去重）——而它的代数已过期、结果会被丢弃⇒ 静默 no-op（fail-safe，但语义上「刷新被吞掉」）。判据：**真的会再发一次请求**。 */
  it('clear() 出清在途 refresh ⇒ 紧接着的 refresh() 真的再发一次', async () => {
    const store = useSessionStore()
    me.mockResolvedValue(kefu)
    await store.load()
    me.mockClear()
    const resolvers: Array<(u: User) => void> = []
    me.mockImplementation(() => new Promise<User>((resolve) => { resolvers.push(resolve) }))
    const stale = store.refresh()
    store.clear()
    const fresh = store.refresh()
    // 变异：clear() 不出清在途引用 ⇒ 第二次 refresh 复用第一条（已过期）的 promise ⇒ 只发 1 次 ⇒ 红。
    expect({ requests: me.mock.calls.length }).toEqual({ requests: 2 })
    resolvers.forEach((resolve) => resolve(kefu))
    await Promise.all([stale, fresh])
  })

  it('login() 出清在途 refresh ⇒ 紧接着的 refresh() 真的再发一次', async () => {
    const store = useSessionStore()
    me.mockResolvedValue(kefu)
    await store.load()
    me.mockClear()
    const resolvers: Array<(u: User) => void> = []
    me.mockImplementation(() => new Promise<User>((resolve) => { resolvers.push(resolve) }))
    const stale = store.refresh()
    apiLoginMock.mockResolvedValue(kefu)
    await store.login('kefu', 'pw')
    const fresh = store.refresh()
    // 变异：login() 不出清在途引用 ⇒ 第二次 refresh 复用第一条（已过期）的 promise ⇒ 只发 1 次 ⇒ 红。
    expect({ requests: me.mock.calls.length }).toEqual({ requests: 2 })
    resolvers.forEach((resolve) => resolve(kefu))
    await Promise.all([stale, fresh])
    expect(store.status).toBe('ready')
  })

  /** ⭐ **2026-09-25 复评 Important：一条**陈旧**的在途 refresh 落地时，⛔ 不得清掉更新那条的引用。**现场：`finally` 原先**无条件** `refreshInflight = null` ⇒ 复评实测序列refresh#1 在途 → `clear()` → refresh#2（此刻在途是 #2）→ 让 #1 落地 → `refresh()`#3：#1 的 `finally` 把 **#2** 的引用清掉 ⇒ #3 误以为没有在途、又发一条 `/api/auth/me`⇒ **计数 3、两条同时在途**（正是「单飞」要防的重复请求）。判据钉在**请求条数**上：#3 必须复用 #2 ⇒ 计数停在 2。变异：把 `if (refreshInflight === promise) refreshInflight = null` 改回`refreshInflight = null` ⇒ #1 落地后 #3 新发一条 ⇒ 本条以 `{requests: 3}` 变红（已实测）。 */
  it('陈旧 refresh 落地只清自己那条 ⇒ 紧接着的 refresh() 复用更新那条（不多发）', async () => {
    const store = useSessionStore()
    me.mockResolvedValue(kefu)
    await store.load()
    me.mockClear()
    const resolvers: Array<(u: User) => void> = []
    me.mockImplementation(() => new Promise<User>((resolve) => { resolvers.push(resolve) }))

    const stale = store.refresh()          // #1：马上会被 clear() 作废代数（陈旧）
    store.clear()
    const fresh = store.refresh()          // #2：当前在途
    expect({ requests: me.mock.calls.length }).toEqual({ requests: 2 })

    // 自证夹具：#1、#2 都必须在途（否则下面的序列不是我们要测的形状）
    expect({ resolvers: resolvers.length }).toEqual({ resolvers: 2 })
    const staleResolver = resolvers[0]
    if (staleResolver === undefined) throw new Error('夹具没造出 #1 的 resolver')
    staleResolver(kefu)                    // 让 **陈旧** 的 #1 落地
    await stale

    const third = store.refresh()          // #3：必须复用 #2，不得新发
    // 判据钉在**可观测出口**（请求条数）上：只剩一条在途 ⇒ 计数停在 2。
    // ⚠️ ⛔ 不能断言 `third === fresh`：Pinia 会把 action 返回的 promise 再包一层
    // （`pinia/dist/pinia.esm-browser.js` 的 `wrapAction`：`return ret.then(…).catch(…)`）
    // ⇒ `store.refresh()` 每次返回的都是**新的** promise 对象，比身份会恒假（实测）。
    // 变异：`finally` 无条件清空 ⇒ #1 落地时误清 #2 的引用 ⇒ 这里新发一条 ⇒ `{requests: 3}` ⇒ 红。
    expect({ requests: me.mock.calls.length }).toEqual({ requests: 2 })

    resolvers.forEach((resolve) => resolve(kefu))
    await Promise.all([fresh, third])
  })
})

describe('canNode（节点 code 判据；D-0020 结案后的唯一一套）', () => {
  it('is_tenant_admin 全通（menu_permissions 为空——部署空窗期的那个窗口）', async () => {
    const store = useSessionStore()
    me.mockResolvedValue({ ...kefu, is_tenant_admin: true, menu_permissions: [] })
    await store.load()
    expect(store.canNode(['anything.at_all'])).toBe(true)
  })

  it('账号管理员全通（is_account_admin 与 is_tenant_admin 是两个判据，任一为真即短路）', async () => {
    const store = useSessionStore()
    me.mockResolvedValue({ ...kefu, is_tenant_admin: false, is_account_admin: true, menu_permissions: [] })
    await store.load()
    expect(store.canNode(['system.role_authority'])).toBe(true)
  })

  it('未声明（空数组）通过', async () => {
    const store = useSessionStore()
    me.mockResolvedValue(kefu)
    await store.load()
    expect(store.canNode([])).toBe(true)
  })

  it('与 menu_permissions 有交集通过、无交集拒绝', async () => {
    const store = useSessionStore()
    me.mockResolvedValue(kefu)
    await store.load()
    expect(store.canNode(['order.list'])).toBe(true)
    expect(store.canNode(['order.list', 'system.settings'])).toBe(true)
    expect(store.canNode(['system.settings'])).toBe(false)
  })

  it('menu_permissions 缺席 ⇒ 按空集（fail-closed，不许当"全通"）', async () => {
    const store = useSessionStore()
    me.mockResolvedValue({ ...kefu, menu_permissions: undefined })
    await store.load()
    expect(store.canNode(['order.list'])).toBe(false)
  })
})

describe('isWorker（打手 = 持「代练员」职位；线 A / D-A1，与 D-0228 同一口径）', () => {
  it('roles 含「代练员」⇒ true（哪怕同时还有别的职位）', async () => {
    const store = useSessionStore()
    me.mockResolvedValue({ ...kefu, roles: [WORKER_ROLE] })
    await store.load()
    expect(store.isWorker).toBe(true)

    // 多职位（客服 + 代练员）：只要**含**代练员就是打手（判据是「含」，不是「唯一职位是」）。
    setActivePinia(createPinia())
    const both = useSessionStore()
    me.mockResolvedValue({ ...kefu, roles: ['客服', WORKER_ROLE] })
    await both.load()
    expect(both.isWorker).toBe(true)
  })

  it('非打手职位（客服 / 客服主管 / 系统管理员）⇒ false', async () => {
    for (const role of ['客服', '客服主管', '系统管理员']) {
      setActivePinia(createPinia())
      const store = useSessionStore()
      me.mockResolvedValue({ ...kefu, roles: [role] })
      await store.load()
      expect({ role, isWorker: store.isWorker }).toEqual({ role, isWorker: false })
    }
  })

  it('roles 缺席 ⇒ false（fail-closed：判不了职位就不算打手）', async () => {
    const store = useSessionStore()
    me.mockResolvedValue({ ...kefu, roles: undefined as unknown as string[] })
    await store.load()
    expect(store.isWorker).toBe(false)
  })

  it('clear() 之后 ⇒ false（新会话重新判定，⛔ 不缓存上一个人的身份）', async () => {
    const store = useSessionStore()
    me.mockResolvedValue({ ...kefu, roles: [WORKER_ROLE] })
    await store.load()
    expect(store.isWorker).toBe(true)
    store.clear()
    expect(store.isWorker).toBe(false)
  })

  it('⛔ 不受管理员短路影响：系统管理员不是打手（spec §1.5 的代价，手机端也看不到 /m/）', async () => {
    const store = useSessionStore()
    me.mockResolvedValue({
      ...kefu,
      roles: ['系统管理员'],
      is_tenant_admin: true,
      is_account_admin: true,
      menu_permissions: [],
    })
    await store.load()
    expect(store.canNode(['system.role_authority'])).toBe(true)
    expect(store.isWorker).toBe(false)
  })

  it('⭐ WORKER_ROLE 与后端常量逐字相同（apps/business/roles.py::WORKER）——前端不写第二份字面量', () => {
    const source = readFileSync(BACKEND_ROLES_PY, 'utf8')
    const matched = /^WORKER\s*=\s*(['"])(.*?)\1/m.exec(source)
    // 自证夹具：先在 roles.py 里真的找到那一行，否则下面的比较是假绿。
    expect(matched, `未在 ${BACKEND_ROLES_PY} 里找到 \`WORKER = "…"\``).not.toBeNull()
    expect(matched?.[2]).toBe(WORKER_ROLE)
  })
})

/** 线 A 修复轮 P1-1：**会话终结必须清掉「手机端抑制标记」**。标记的语义是「**当前这个人**不要再被跳进 `/m/`」⇒ 会话一终结它就不再代表任何人。不清的后果：同一标签页里非打手被弹回、随后换人登录（或是打手登录），会被上一个人的标记挡在桌面版外。落点是 `clear()`（**唯一**会话终结点：`logout()` 调它，两端 401 处理器也调它）。 */
describe('会话终结清掉「手机端抑制标记」（P1-1）', () => {
  /** jsdom 的 location 是只读的 ⇒ 换可控替身跑一段，跑完还原（同 `mobileRedirect.spec.ts` 的写法）。 */
  function withStubbedLocation(fn: (replace: ReturnType<typeof vi.fn>) => void): void {
    const replace = vi.fn()
    const original = window.location
    Object.defineProperty(window, 'location', {
      configurable: true,
      writable: true,
      value: { ...original, replace },
    })
    try {
      fn(replace)
    } finally {
      Object.defineProperty(window, 'location', {
        configurable: true,
        writable: true,
        value: original,
      })
    }
  }

  it('clear() ⇒ 标记被清掉（非打手被弹回后留下的标记不跨会话）', async () => {
    const store = useSessionStore()
    me.mockResolvedValue({ ...kefu, roles: ['客服'] })
    await store.load()
    writeMobileRedirectSuppressed() // = 守卫把非打手弹回桌面时写的那一下
    expect(readMobileRedirectSuppressed()).toBe(true)

    store.clear()

    expect(readMobileRedirectSuppressed()).toBe(false)
  })

  it('logout() ⇒ 标记同样被清掉（logout 的第一件事就是 clear()）', async () => {
    const store = useSessionStore()
    me.mockResolvedValue({ ...kefu, roles: ['客服'] })
    await store.load()
    writeMobileRedirectSuppressed()
    expect(readMobileRedirectSuppressed()).toBe(true)

    await store.logout()

    expect(readMobileRedirectSuppressed()).toBe(false)
  })

  it('⭐ 真实出口：标记清掉后，桌面入口会**再次**跳 /m/', () => {
    withStubbedLocation((replace) => {
      writeMobileRedirectSuppressed()
      redirectToMobileIfMobile(IPHONE_UA, '/')
      // 对照：标记还在 ⇒ 不跳（这正是 D-A3 要的压住来回）。
      expect(replace).not.toHaveBeenCalled()

      clearMobileRedirectSuppressed() // = `clear()` 里那一下
      redirectToMobileIfMobile(IPHONE_UA, '/')
      // 标记清了 ⇒ 桌面入口恢复原行为（再次引到 /m/），证明「清」是真的生效、不是自我安慰。
      expect(replace).toHaveBeenCalledWith('/m/')
    })
  })
})
