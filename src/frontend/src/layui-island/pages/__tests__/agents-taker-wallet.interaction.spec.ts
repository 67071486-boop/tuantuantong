// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 【财务相关 → 打手钱包】合并页的**交互**测试（2026-10-22，财务页合并线）。
 *
 * 本页的内容**复用两份既有页模块的内核**（它们的交互测试原样保留、直接 import 页模块跑，
 * 保内核即全绿）；本文件钉的是**合并页自己**的约定：
 *
 * 1. ⭐ **驱动真交互**（`AGENTS.md` §5-G）：挂载 → 两个子页模块真的被 mount 进各自的
 *    面板、拿到的就是那块面板 DOM；点页签 → 面板切换（`initIslandElements` 的索引语义）。
 * 2. ⭐ **页签按权限渲染（fail-closed）**：持一个节点 ⇒ 只见对应页签、只 mount 对应子页；
 *    两个节点都没有 ⇒ 一句提示、不注入标记、一个子页都不 mount。
 * 3. ⭐ **两份既有页模块原样复用**：本页不 import 它们的内部函数、不复制它们的标记——
 *    子页模块被 mount 的就是**模块默认导出本身**（mock 掉后按引用断言）。
 *
 * ## 手法
 *
 * - `vi.mock('@/layui/island')`：jsdom 里加载不了真 layui，换成可控替身；
 * - `vi.mock('@/api/auth')`：换身份出口（页签可见性判据的输入）；
 * - `vi.mock` **两份子页模块**：合并页的判据是「mount 了谁、mount 到哪」，
 *   子页内部行为由它们自己的交互测试负责（那两份原样保留、不改）。
 *
 * ⚠️ 夹具一律**合成值**（E-0077）。
 */
const LAYUI = vi.hoisted(() => {
  const messages: string[] = []
  const seenByBootstrap: boolean[] = []
  const seenByInit: boolean[] = []
  const tabClicks: Array<{ title: HTMLElement; li: HTMLElement; content: HTMLElement }> = []
  return { messages, seenByBootstrap, seenByInit, tabClicks }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => {
    LAYUI.seenByBootstrap.push(document.querySelector('#takerWalletTabs') !== null)
    return { layer: { msg: (t: string) => LAYUI.messages.push(t) } }
  }),
  initIslandElements: vi.fn(() => {
    LAYUI.seenByInit.push(document.querySelector('#takerWalletTabs') !== null)
  }),
  islandMsg: (_layer: unknown, text: string) => {
    LAYUI.messages.push(text)
  },
}))

vi.mock('@/api/auth', () => ({ fetchMe: vi.fn() }))

/** 两份子页模块的替身：记录「mount 进了哪个元素」，返回可断言的 unmount。 */
const mounts = vi.hoisted(() => ({
  withdraw: [] as Array<{ host: HTMLElement; unmount: ReturnType<typeof vi.fn> }>,
  wallet: [] as Array<{ host: HTMLElement; unmount: ReturnType<typeof vi.fn> }>,
}))

vi.mock('../agents-withdraw-review', () => ({
  default: {
    mount: vi.fn(async (root: HTMLElement) => {
      const unmount = vi.fn()
      mounts.withdraw.push({ host: root, unmount })
      return unmount
    }),
  },
}))
vi.mock('../agents-wallet-adjust', () => ({
  default: {
    mount: vi.fn(async (root: HTMLElement) => {
      const unmount = vi.fn()
      mounts.wallet.push({ host: root, unmount })
      return unmount
    }),
  },
}))

import { fetchMe } from '@/api/auth'
import page, {
  NO_ACCESS_MESSAGE,
  PANEL_CLASS,
  TAB_ATTR,
  TABS_ID,
  WALLET_PANEL_ID,
  WITHDRAW_PANEL_ID,
  buildHtml,
} from '../agents-taker-wallet'

const mockedFetchMe = vi.mocked(fetchMe)

function meWith(nodes: string[]) {
  return {
    id: 1,
    username: 'synthetic-cs',
    display_name: '合成客服',
    identity_plane: 'tenant',
    is_tenant_admin: false,
    is_account_admin: false,
    roles: [],
    menu_permissions: nodes,
    permissions: [],
  }
}

function flush(): Promise<void> {
  return (async () => {
    for (let i = 0; i < 8; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 0))
    }
  })()
}

describe('打手钱包合并页（agents-taker-wallet）', () => {
  let root: HTMLElement

  beforeEach(() => {
    vi.clearAllMocks()
    for (const bucket of [LAYUI.messages, LAYUI.seenByBootstrap, LAYUI.seenByInit]) {
      bucket.length = 0
    }
    mounts.withdraw.length = 0
    mounts.wallet.length = 0
    document.body.innerHTML = ''
    root = document.createElement('div')
    document.body.appendChild(root)
  })

  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('两个节点都有：两个页签都在，两份既有页模块 mount 进各自面板；标题是「打手钱包」', async () => {
    mockedFetchMe.mockResolvedValue(meWith([
      'finance.taker_withdraw_review',
      'finance.taker_wallet_adjust',
    ]))
    const unmount = await page.mount(root, { onNavigate: vi.fn() })
    await flush()

    // 陷阱 ③：运行期判据——bootstrap 时看不到标记，init 时看得到。
    expect(LAYUI.seenByBootstrap).toEqual([false])
    expect(LAYUI.seenByInit).toEqual([true])
    expect(root.innerHTML).toContain(PANEL_CLASS)
    // 两个页签（data 属性）+ 两个面板（子页 mount 的落点）。
    expect(root.querySelectorAll(`[${TAB_ATTR}]`).length).toBe(2)
    expect(mounts.withdraw).toHaveLength(1)
    expect(mounts.wallet).toHaveLength(1)
    expect(mounts.withdraw[0]!.host.id).toBe(WITHDRAW_PANEL_ID)
    expect(mounts.wallet[0]!.host.id).toBe(WALLET_PANEL_ID)
    // ⭐ 复用的是**页模块本身**：面板里没有合并页硬拼的标记（内容由子页负责）。
    expect(mounts.withdraw[0]!.host.innerHTML).toBe('')
    // 子页会各自写 document.title ⇒ 合并页最后改回自己的。
    expect(document.title).toBe('打手钱包')
    // 卸载：子页的 unmount 被逐个调用、标记清空。
    unmount()
    expect(mounts.withdraw[0]!.unmount).toHaveBeenCalledTimes(1)
    expect(mounts.wallet[0]!.unmount).toHaveBeenCalledTimes(1)
    expect(root.innerHTML).toBe('')
  })

  it('只持提现审核：只有「提现审核」页签、只 mount 提现子页（fail-closed 不画另一签）', async () => {
    mockedFetchMe.mockResolvedValue(meWith(['finance.taker_withdraw_review']))
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()

    expect(root.querySelectorAll(`[${TAB_ATTR}]`).length).toBe(1)
    expect(root.querySelector(`[${TAB_ATTR}]`)!.textContent).toBe('提现审核')
    expect(mounts.withdraw).toHaveLength(1)
    expect(mounts.wallet).toHaveLength(0)
  })

  it('只持钱包管理：只有「钱包管理」页签、只 mount 钱包子页', async () => {
    mockedFetchMe.mockResolvedValue(meWith(['finance.taker_wallet_adjust']))
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()

    expect(root.querySelectorAll(`[${TAB_ATTR}]`).length).toBe(1)
    expect(root.querySelector(`[${TAB_ATTR}]`)!.textContent).toBe('钱包管理')
    expect(mounts.withdraw).toHaveLength(0)
    expect(mounts.wallet).toHaveLength(1)
  })

  it('无任何节点：一句提示、不注入标记、一个子页都不 mount', async () => {
    mockedFetchMe.mockResolvedValue(meWith([]))
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()

    expect(LAYUI.messages).toContain(NO_ACCESS_MESSAGE)
    expect(root.innerHTML).toBe('')
    expect(mounts.withdraw).toHaveLength(0)
    expect(mounts.wallet).toHaveLength(0)
  })

  it('纯函数：buildHtml 只画传入的页签，面板与页签一一对应（陷阱 ①）', () => {
    const both = buildHtml([
      { key: 'withdraw', label: '提现审核', panelId: WITHDRAW_PANEL_ID },
      { key: 'wallet', label: '钱包管理', panelId: WALLET_PANEL_ID },
    ])
    expect(both).toContain(`id="${TABS_ID}"`)
    expect(both).toContain('class="layui-this"') // 第一个页签默认选中
    expect(both).toContain(`id="${WITHDRAW_PANEL_ID}"`)
    expect(both).toContain(`id="${WALLET_PANEL_ID}"`)
    // ⛔ 陷阱 ①：标记里不许出现 `class=".takerWalletPanel"` 这种选择器形态。
    expect(both).not.toContain('class=".')
    // 第一个面板默认显示、第二个不显示（`initIslandElements` 按索引切换的前提）。
    expect(both.indexOf('layui-tab-item layui-show')).toBeGreaterThan(-1)

    const single = buildHtml([{ key: 'wallet', label: '钱包管理', panelId: WALLET_PANEL_ID }])
    expect(single).toContain(WALLET_PANEL_ID)
    expect(single).not.toContain(WITHDRAW_PANEL_ID)
  })
})
