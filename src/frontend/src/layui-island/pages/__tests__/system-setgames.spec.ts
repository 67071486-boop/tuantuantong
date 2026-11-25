// @vitest-environment jsdom
/**
 * 【设置游戏权限】iframe 子页（对标 `/User/SetGames?id=<uid>`）的测试。
 *
 * 本页不是弹层，而是父页（人员管理）`layer.open({type:2})` 里的**独立 iframe 页面**
 * （对标 `User_SetGames.html` + `JS2019/User/setgames.js`，证据在
 * `.tools/recon/sysuser/User_SetGames.html`）。
 *
 * 三层覆盖：
 *  1. **结构**：标记与对标原文逐块对齐（游戏 checkbox 的 `value` = **游戏名**、
 *     `lay-skin="tag"`、`lay-filter="selectGame"`、两个 `setMode` radio 的 `lay-tips`、
 *     保存按钮初始 `disabled` + `layui-disabled`）；
 *  2. **纯函数**：uid 解析 / 「共同游戏集合」口径 / 并集 / 关弹层的落点；
 *  3. **驱动真交互**（`AGENTS.md` §5 要求新建 island 页必须有）：真挂载 + 真点击 +
 *     真跑 `form.on` 回调，断言「点了没反应」这类 `typecheck`/`lint` 看不见的失败。
 *
 * ⚠️ 本文件用的是**假 layui**（不加载 3.9MB 的 `public/layui/layui.js`）：
 * `bootstrapIsland` 被 mock，`form.on` 把回调记进 Map、由测试按本仓既有写法手动触发
 * （同 `user-adduser.spec.ts` / `order-myorderlist.spec.ts`）。它能抓到「事件根本没注册」
 * （Map 里没有那个 key ⇒ 断言红），抓不到真 layui 内部的渲染细节——那些是真浏览器的事，
 * 见 `task-6-report.md` 的运行期验证一节。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AccountRow } from '@/api/accounts'
import type { ConfigRow } from '@/api/config'

/* ─────────────────────── 假 layui ─────────────────────── */

const LAYUI = vi.hoisted(() => {
  const formHandlers = new Map<string, (obj: unknown) => unknown>()
  const msg = vi.fn()
  const renders: string[] = []
  const cache: Record<string, unknown[]> = {}
  return {
    formHandlers,
    msg,
    renders,
    cache,
    form: {
      render: vi.fn((type?: string) => {
        renders.push(type ?? '')
      }),
      on: vi.fn((event: string, cb: (obj: unknown) => unknown) => {
        formHandlers.set(event, cb)
      }),
    },
    layer: { msg, open: vi.fn(), close: vi.fn() },
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => ({
    form: LAYUI.form,
    layer: LAYUI.layer,
  })),
  initIslandElements: vi.fn(),
  islandMsg: (_layer: unknown, text: string) => LAYUI.msg(text),
}))

vi.mock('@/api/config', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/config')>()
  return { ...actual, fetchGames: vi.fn(), fetchAllGames: vi.fn() }
})

vi.mock('@/api/accounts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/accounts')>()
  return { ...actual, getAccount: vi.fn(), updateAccount: vi.fn() }
})

import { fetchAllGames, fetchGames } from '@/api/config'
import { getAccount, updateAccount } from '@/api/accounts'
import page, {
  ALL_GAMES_VALUE,
  SET_GAMES_MESSAGES,
  closeIslandLayer,
  commonIds,
  setGamesFormHtml,
  uidsFromSearch,
  unionIds,
} from '../system-setgames'

const GAMES: ConfigRow[] = [
  { id: 10, name: '原神', sort_order: 0, is_active: true, created_at: '' },
  { id: 20, name: '鸣潮', sort_order: 1, is_active: true, created_at: '' },
  { id: 30, name: '绝区零', sort_order: 2, is_active: true, created_at: '' },
]

/** 一份最小可用的账号行（本页只读 `id` 与 `games`）。 */
function account(id: number, games: number[]): AccountRow {
  return {
    id,
    username: `u${id}`,
    display_name: `用户${id}`,
    is_active: true,
    is_tenant_admin: false,
    role: '客服',
    games,
    last_login: null,
    date_joined: '',
  }
}

function flush(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

/** 把 `?uid=…` 写进 `window.location.search`（子页唯一的参数来源）。 */
function setSearch(uid: string): void {
  window.history.replaceState({}, '', `/layui-island/page.html?page=system-setgames&uid=${uid}`)
}

describe('设置游戏权限：标记与对标原文逐块对齐', () => {
  const doc = (): Document =>
    new DOMParser().parseFromString(
      `<div id="root">${setGamesFormHtml(GAMES, '622', new Set())}</div>`,
      'text/html',
    )

  it('表单结构与对标一致：`form.layui-form` 的内联 style、`.layui-field-box`、两个 `.layui-form-item`', () => {
    const d = doc()
    const form = d.querySelector('form.layui-form')!
    expect(form.getAttribute('action')).toBe('')
    expect(form.getAttribute('style')).toBe('margin-left:10px;margin-right:10px')
    const box = form.querySelector('.layui-field-box')!
    expect(box.getAttribute('style')).toBe('margin-left:0px;padding: 0px 0px;margin-top:10px')
    expect(
      Array.from(box.querySelectorAll('.layui-form-item > .layui-form-label')).map((l) =>
        l.textContent?.trim(),
      ),
    ).toEqual(['勾选游戏', '设置方式'])
  })

  it('游戏 checkbox：每个游戏一个，`value`/`title` 都是**游戏名**（不是 id），`lay-skin="tag"` + `lay-filter="selectGame"`', () => {
    const boxes = Array.from(doc().querySelectorAll<HTMLInputElement>('input[name="games"]'))
    // 首位是「全部游戏」伪选项 + 3 个真游戏。
    expect(boxes.length).toBe(4)
    expect(boxes.map((b) => b.value)).toEqual(['全部游戏', '原神', '鸣潮', '绝区零'])
    expect(boxes.map((b) => b.getAttribute('title'))).toEqual(['全部游戏', '原神', '鸣潮', '绝区零'])
    for (const b of boxes) {
      expect(b.getAttribute('type')).toBe('checkbox')
      expect(b.getAttribute('name')).toBe('games')
      expect(b.getAttribute('lay-skin')).toBe('tag')
      expect(b.getAttribute('lay-filter')).toBe('selectGame')
    }
    // 没有任何一个是 id 值（value 必须是游戏名，后端要按 name 回填）。
    expect(boxes.map((b) => b.value)).not.toContain('10')
  })

  it('两个 `setMode` radio：value 0/1、`title=""`、文案与 `lay-tips` 逐字（对标 `User_SetGames.html` L78-85）', () => {
    const radios = Array.from(doc().querySelectorAll<HTMLInputElement>('input[name="setMode"]'))
    expect(radios.map((r) => r.value)).toEqual(['0', '1'])
    expect(radios.map((r) => r.getAttribute('title'))).toEqual(['', ''])
    expect(radios.map((r) => r.getAttribute('type'))).toEqual(['radio', 'radio'])
    // 两个 radio 都**不预选**（对标原文就没有 checked）。
    expect(radios.some((r) => r.hasAttribute('checked'))).toBe(false)
    const tips = Array.from(doc().querySelectorAll('span[lay-tips]'))
    expect(tips.map((s) => s.getAttribute('lay-tips'))).toEqual([
      '将这些子账号的游戏权限全都设置成你选择的游戏',
      '在现在游戏权限的基础上，新增你所选择的游戏。如果原本就有这个游戏权限，将忽略',
    ])
    expect(tips.map((s) => s.textContent?.trim())).toEqual([
      '统一更换成所勾选的游戏',
      '在现有基础上新增勾选的游戏',
    ])
    // 两段文案各自裹在 `<div lay-radio>` 里（对标原文）。
    expect(Array.from(doc().querySelectorAll('div[lay-radio]')).length).toBe(2)
  })

  it('保存按钮：`id=saveBtn`、`layui-btn-normal w-lg`、初始 `disabled` + `layui-disabled`、`type=button`', () => {
    const btn = doc().querySelector<HTMLButtonElement>('#saveBtn')!
    expect(btn.textContent).toBe('保存设定')
    expect(btn.getAttribute('class')).toBe('layui-btn layui-btn-normal w-lg layui-disabled')
    expect(btn.hasAttribute('disabled')).toBe(true)
    expect(btn.getAttribute('type')).toBe('button')
    expect(btn.getAttribute('lay-filter')).toBe('saveBtn')
    expect(btn.getAttribute('lay-submit')).toBe('')
    // 按钮在 `.layui-input-inline` 里，外面那层是居中的（对标原文）。
    expect(btn.closest('.layui-input-inline')).toBeTruthy()
    expect(btn.closest('div')!.parentElement!.getAttribute('style')).toBe('text-align:center;align-items:center')
  })

  it('隐藏域 `#uid` 收下逗号串（多个人时对标传的就是逗号串）', () => {
    const d = new DOMParser().parseFromString(
      `<div id="root">${setGamesFormHtml(GAMES, '1,2,3', new Set())}</div>`,
      'text/html',
    )
    const uid = d.querySelector<HTMLInputElement>('#uid')!
    expect(uid.getAttribute('type')).toBe('hidden')
    expect(uid.getAttribute('name')).toBe('uid')
    expect(uid.value).toBe('1,2,3')
  })

  it('回填：传入的游戏名在清单里 ⇒ 那个 checkbox 带 `checked`；伪选项「全部游戏」**永不**被回填', () => {
    const d = new DOMParser().parseFromString(
      `<div id="root">${setGamesFormHtml(GAMES, '7', new Set(['原神', '绝区零', '全部游戏']))}</div>`,
      'text/html',
    )
    const checked = Array.from(d.querySelectorAll<HTMLInputElement>('input[name="games"]'))
      .filter((b) => b.checked)
      .map((b) => b.value)
    expect(checked).toEqual(['原神', '绝区零'])
  })

  it('清单为空（租户还没建游戏）⇒ 只剩「全部游戏」伪选项，页面不炸', () => {
    const d = new DOMParser().parseFromString(
      `<div id="root">${setGamesFormHtml([], '7', new Set())}</div>`,
      'text/html',
    )
    expect(
      Array.from(d.querySelectorAll<HTMLInputElement>('input[name="games"]')).map((b) => b.value),
    ).toEqual(['全部游戏'])
  })
})

describe('设置游戏权限：纯函数（口径可重跑）', () => {
  it('`uidsFromSearch`：逗号串 ⇒ 数字数组；空/非法段丢掉（不产生 NaN）', () => {
    expect(uidsFromSearch('?page=system-setgames&uid=1,2,3')).toEqual([1, 2, 3])
    expect(uidsFromSearch('?uid=622')).toEqual([622])
    expect(uidsFromSearch('?uid=')).toEqual([])
    expect(uidsFromSearch('')).toEqual([])
    expect(uidsFromSearch('?uid=1,,x,2')).toEqual([1, 2])
    expect(uidsFromSearch('?page=system-setgames')).toEqual([])
  })

  it('`commonIds`：单人回填真实值（含**空集**，不是 undefined）；无人/多人不一致 ⇒ undefined', () => {
    expect(commonIds([[10]])).toEqual([10])
    // 单人但一个游戏都没有 ⇒ 回填空集（`[]`），调用方据此「一个都不勾」。
    expect(commonIds([[]])).toEqual([])
    expect(commonIds([[10, 20], [20, 10]])).toEqual([10, 20])
    expect(commonIds([[10], [20]])).toBeUndefined()
    expect(commonIds([[10], []])).toBeUndefined()
    expect(commonIds([])).toBeUndefined()
  })

  it('`unionIds`：并集去重，**当前在前的顺序**稳定（保证载荷可断言）', () => {
    expect(unionIds([10], [20])).toEqual([10, 20])
    expect(unionIds([10, 20], [20, 30])).toEqual([10, 20, 30])
    expect(unionIds([], [30])).toEqual([30])
    expect(unionIds([10], [])).toEqual([10])
    expect(unionIds([10, 10], [10])).toEqual([10])
  })

  it('`closeIslandLayer`：调**父窗口**的 `layer.close(getFrameIndex(window.name))`', () => {
    const close = vi.fn()
    const getFrameIndex = vi.fn(() => '7')
    closeIslandLayer({
      name: 'layui-layer-iframe7',
      parent: { layer: { getFrameIndex, close } },
    } as never)
    expect(getFrameIndex).toHaveBeenCalledWith('layui-layer-iframe7')
    expect(close).toHaveBeenCalledWith('7')
  })

  it('`closeIslandLayer`：父窗口没有 layer（直接打开本页）⇒ 静默 no-op，不抛', () => {
    expect(() => closeIslandLayer({ name: '', parent: {} } as never)).not.toThrow()
    // 拿不到层索引（不是 layer 弹层里的 iframe）⇒ 也不调 close。
    const close = vi.fn()
    const getFrameIndex = vi.fn(() => undefined)
    closeIslandLayer({ name: '', parent: { layer: { getFrameIndex, close } } } as never)
    expect(close).not.toHaveBeenCalled()
  })

  /**
   * ⭐ Task 13：返回值语义（`true` = 确实关掉了自己）。
   *
   * 为什么需要它：`user-adduser` 的「保存后回列表」要靠它**区分双态**——弹层里关自己、
   * 整页路由才 `onNavigate`。若它照旧恒 `void`，子页就没法判「我在不在弹层里」。
   */
  it('`closeIslandLayer` 返回值：真的关掉了 ⇒ `true`', () => {
    const close = vi.fn()
    const getFrameIndex = vi.fn(() => '7')
    const win = { name: 'layui-layer-iframe7', parent: { layer: { getFrameIndex, close } } }
    expect(closeIslandLayer(win as never)).toBe(true)
    expect(getFrameIndex).toHaveBeenCalledWith('layui-layer-iframe7')
    expect(close).toHaveBeenCalledWith('7')
  })

  it('`closeIslandLayer` 返回值：关不到（无 layer / 索引为 undefined、null、空串）⇒ `false` 且不抛', () => {
    // 父窗口根本没有 `layer`（整页路由 / Vue 壳）⇒ false。
    expect(closeIslandLayer({ name: '', parent: {} } as never)).toBe(false)
    // `getFrameIndex` 回 `''`（真值判据会误伤的那种 index）也要算「关不到」。
    for (const index of [undefined, null, '']) {
      const close = vi.fn()
      const getFrameIndex = vi.fn(() => index)
      let result: boolean | undefined
      expect(() => {
        result = closeIslandLayer({ name: '', parent: { layer: { getFrameIndex, close } } } as never)
      }).not.toThrow()
      expect(result, `getFrameIndex 回 ${JSON.stringify(index)} 时应是 false`).toBe(false)
      expect(close).not.toHaveBeenCalled()
    }
  })
})

/* ─────────────────────── 驱动真交互：真挂载 + 真点击 ─────────────────────── */

describe('设置游戏权限：挂载后的真实流程', () => {
  let root: HTMLElement

  beforeEach(() => {
    vi.clearAllMocks()
    LAYUI.formHandlers.clear()
    LAYUI.renders.length = 0
    document.body.innerHTML = ''
    root = document.createElement('div')
    root.id = 'app'
    document.body.appendChild(root)
    vi.mocked(fetchAllGames).mockResolvedValue(GAMES as never)
    vi.mocked(fetchGames).mockResolvedValue(GAMES as never)
    vi.mocked(getAccount).mockImplementation(async (id: number) => account(id, []) as never)
    vi.mocked(updateAccount).mockResolvedValue({} as never)
    setSearch('1')
  })

  afterEach(() => {
    window.history.replaceState({}, '', '/')
    vi.unstubAllGlobals()
  })

  async function mountPage(uid = '1'): Promise<void> {
    setSearch(uid)
    await page.mount(root, {} as never)
    await flush()
    await flush()
  }

  /** 触发「保存设定」（layui 的 `submit(saveBtn)` 回调由测试手动派发，同仓既有写法）。 */
  const clickSave = (): unknown => LAYUI.formHandlers.get('submit(saveBtn)')!({ field: {} })

  /** 真点一下某个游戏 checkbox，再按 layui 的行为派发 `checkbox(selectGame)`。 */
  function checkGame(name: string): HTMLInputElement {
    const box = root.querySelector<HTMLInputElement>(`input[name="games"][value="${name}"]`)!
    box.click()
    LAYUI.formHandlers.get('checkbox(selectGame)')!({ elem: box, value: box.checked ? 'on' : 'off' })
    return box
  }

  /**
   * ⭐ **防死锁**（本线 DoD #4）：本页**就是配置面本身**——取数必须走 `fetchAllGames()`（全量），
   * ⛔ 不能走按当前用户 `game_ids` 收窄的 `fetchGames()`，否则「看不到没勾的游戏 ⇒ 也就勾不上」。
   * 形状照 `system-user-list.spec.ts` 的同名 pin（那里守的是同一条死锁）。
   * 变异证明：把本页改回 `fetchGames()` ⇒ 本条当场红（见收尾报告）。
   */
  it('挂载取的是 fetchAllGames，⛔ 不是按当前用户 game_ids 收窄的 fetchGames', async () => {
    vi.mocked(fetchAllGames).mockClear()
    vi.mocked(fetchGames).mockClear()
    await mountPage()
    expect(vi.mocked(fetchAllGames)).toHaveBeenCalled()
    expect(vi.mocked(fetchGames)).not.toHaveBeenCalled()
  })

  it('挂载：渲染出每个游戏一个 checkbox（首位「全部游戏」）、保存按钮初始带 `disabled`', async () => {
    await mountPage()
    const boxes = Array.from(root.querySelectorAll<HTMLInputElement>('input[name="games"]'))
    expect(boxes.map((b) => b.value)).toEqual(['全部游戏', '原神', '鸣潮', '绝区零'])
    const save = root.querySelector<HTMLButtonElement>('#saveBtn')!
    expect(save.hasAttribute('disabled')).toBe(true)
    expect(save.className).toContain('layui-disabled')
    // 渲染器被调过（layui 把 tag 皮肤画出来），且**有一次是无参调用**——传 `'checkbox'` 时
    // layui **不**渲染 radio，两个 `setMode` 会停在被 CSS 藏起来的原生 input 上 ⇒ 真浏览器里
    // 「设置方式」根本点不了（2026-09-22 实跑 Chromium 抓到；假 layui 看不见渲染结果，只钉调用形状）。
    expect(LAYUI.renders.length).toBeGreaterThan(0)
    expect(LAYUI.renders).toContain('')
    expect(root.querySelector<HTMLInputElement>('#uid')!.value).toBe('1')
  })

  it('单人：按**真实数据**回填该用户的游戏（不是像对标那样一律全勾）', async () => {
    vi.mocked(getAccount).mockImplementation(async (id: number) => account(id, [20]) as never)
    await mountPage('1')
    const checked = Array.from(root.querySelectorAll<HTMLInputElement>('input[name="games"]'))
      .filter((b) => b.checked)
      .map((b) => b.value)
    expect(checked).toEqual(['鸣潮'])
  })

  it('多人且各人游戏集合不同 ⇒ **一个都不预选**（沿用 `commonBusinessType` 的口径）', async () => {
    vi.mocked(getAccount).mockImplementation(async (id: number) =>
      account(id, id === 1 ? [10] : [20]) as never,
    )
    await mountPage('1,2')
    expect(vi.mocked(getAccount).mock.calls.length).toBe(2)
    const checked = Array.from(root.querySelectorAll<HTMLInputElement>('input[name="games"]')).filter(
      (b) => b.checked,
    )
    expect(checked.map((b) => b.value)).toEqual([])
  })

  it('勾任一游戏 ⇒ 保存按钮的 `disabled` 被移除（钉住 `form.on(\'checkbox(selectGame)\')` 真的接上了）', async () => {
    await mountPage()
    const save = root.querySelector<HTMLButtonElement>('#saveBtn')!
    // 事件没注册时 Map 里没有这个 key ⇒ 这条断言当场变红（变异证明见报告）。
    const onCheckbox = LAYUI.formHandlers.get('checkbox(selectGame)')
    expect(onCheckbox, '页面必须注册 `checkbox(selectGame)`').toBeTypeOf('function')
    const box = root.querySelector<HTMLInputElement>('input[name="games"][value="原神"]')!
    box.click()
    onCheckbox!({ elem: box, value: 'on' })
    expect(save.hasAttribute('disabled')).toBe(false)
    expect(save.className).not.toContain('layui-disabled')
  })

  it('「全部游戏」伪选项：勾它 ⇒ 全部游戏都勾上；取消它 ⇒ 全部取消', async () => {
    await mountPage()
    const all = root.querySelector<HTMLInputElement>(`input[name="games"][value="${ALL_GAMES_VALUE}"]`)!
    all.click()
    LAYUI.formHandlers.get('checkbox(selectGame)')!({ elem: all, value: 'on' })
    const afterCheck = Array.from(root.querySelectorAll<HTMLInputElement>('input[name="games"]'))
      .filter((b) => b.value !== ALL_GAMES_VALUE)
      .map((b) => b.checked)
    expect(afterCheck).toEqual([true, true, true])

    all.click()
    LAYUI.formHandlers.get('checkbox(selectGame)')!({ elem: all, value: 'off' })
    const afterUncheck = Array.from(root.querySelectorAll<HTMLInputElement>('input[name="games"]'))
      .filter((b) => b.value !== ALL_GAMES_VALUE)
      .map((b) => b.checked)
    expect(afterUncheck).toEqual([false, false, false])
  })

  it('不勾任何游戏直接保存 ⇒ 提示「至少选择一款游戏」，且**一个 PATCH 都不发**', async () => {
    await mountPage()
    expect(clickSave()).toBe(false)
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith('至少选择一款游戏')
    expect(vi.mocked(updateAccount).mock.calls.length).toBe(0)
  })

  it('勾了游戏但没选设置方式 ⇒ 提示「请选择设置方式」，且**没有** PATCH', async () => {
    await mountPage()
    checkGame('原神')
    clickSave()
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith(SET_GAMES_MESSAGES.noMode)
    expect(LAYUI.msg).toHaveBeenCalledWith('请选择设置方式')
    expect(vi.mocked(updateAccount).mock.calls.length).toBe(0)
  })

  it('`setMode=0`（统一更换）：对每个 uid 各发一次 PATCH，载荷 `games` 是**选中游戏的 id**（不是名字）', async () => {
    await mountPage('1,2')
    checkGame('鸣潮')
    checkGame('绝区零')
    root.querySelector<HTMLInputElement>('input[name="setMode"][value="0"]')!.checked = true
    clickSave()
    await flush()
    await flush()
    expect(vi.mocked(updateAccount).mock.calls.length).toBe(2)
    expect(vi.mocked(updateAccount).mock.calls.map((c) => c[0])).toEqual([1, 2])
    for (const call of vi.mocked(updateAccount).mock.calls) {
      expect(call[1]).toEqual({ games: [20, 30] })
    }
  })

  it('`setMode=1`（现有基础上新增）：载荷是「当前 ∪ 选中」的并集', async () => {
    // 当前只有「原神」(10)，勾「鸣潮」(20) ⇒ 期望 [10, 20]。
    vi.mocked(getAccount).mockImplementation(async (id: number) => account(id, [10]) as never)
    await mountPage('1')
    checkGame('鸣潮')
    root.querySelector<HTMLInputElement>('input[name="setMode"][value="1"]')!.checked = true
    clickSave()
    await flush()
    await flush()
    expect(vi.mocked(updateAccount).mock.calls.length).toBe(1)
    expect(vi.mocked(updateAccount).mock.calls[0]![1]).toEqual({ games: [10, 20] })
  })

  it('成功后先提示「已保存」，再由 `layer.msg` 的回调把 iframe 关掉（父页在 `end` 里刷新列表）', async () => {
    await mountPage('1')
    checkGame('原神')
    root.querySelector<HTMLInputElement>('input[name="setMode"][value="0"]')!.checked = true
    clickSave()
    await flush()
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith('已保存', { time: 1000 }, expect.any(Function))
    // 回调里调的是**父窗口**的 layer——jsdom 里 `window.parent === window` 且没有 `window.layer`
    // ⇒ 这里只钉「回调排进去了」，真正的关闭路径由 `closeIslandLayer` 的单测 + 真浏览器验证覆盖。
    const cb = vi.mocked(LAYUI.msg).mock.calls.find((c) => c[0] === '已保存')![2] as () => void
    expect(() => cb()).not.toThrow()
  })

  it('PATCH 失败 ⇒ 只提示后端文案，**不**关掉 iframe（让用户能重试）', async () => {
    vi.mocked(updateAccount).mockRejectedValue(new Error('没有权限执行该操作') as never)
    await mountPage('1')
    checkGame('原神')
    root.querySelector<HTMLInputElement>('input[name="setMode"][value="0"]')!.checked = true
    clickSave()
    await flush()
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith('没有权限执行该操作')
    expect(vi.mocked(LAYUI.msg).mock.calls.some((c) => c[0] === '已保存')).toBe(false)
  })

  it('一个 uid 都没有（直接打开子页）⇒ 保存不发请求，也不抛', async () => {
    await mountPage('')
    checkGame('原神')
    root.querySelector<HTMLInputElement>('input[name="setMode"][value="0"]')!.checked = true
    expect(() => clickSave()).not.toThrow()
    await flush()
    expect(vi.mocked(updateAccount).mock.calls.length).toBe(0)
  })
})
