// @vitest-environment jsdom
/**
 * 【人员管理 → 押金额度】iframe 子页（`pages/taker-deposit-quota.ts`）的测试。
 *
 * 这一页是**自有功能**（对标物没有「按人 × 按游戏配押金额度」），没有证据件可对拍
 * ⇒ 本文件钉的是**有真实后果**的约定（计划 `docs/plans/2026-10-23-taker-deposit-quota-plan.md` §7.2 / §8.2）：
 *
 * 1. ⭐ **uid 只认正整数**：空 / `0` / 负数 / 小数 / 字母 ⇒ 显示错误且**一个请求都不发**。
 * 2. ⭐ **渲染**：每款游戏一行（游戏名 / 全局额度只读 / 个人额度输入框）；`amount=null` ⇒
 *    空输入框 + placeholder `0`；外部值过 `escapeHtml`。
 * 3. ⭐ **一键填充**：统一值写进所有输入框（空 / 非法 ⇒ 提示）；「按全局额度填充」写各自的
 *    `global_amount`；覆盖**已有非空且不同**的值前先 `layer.confirm`，确认前**不动**输入框。
 * 4. ⭐ **保存载荷**：空输入框 ⇒ `"0"`；开关状态来自 switch；负数 / 三位小数在页面就被挡住
 *    （**不发请求**）；成功 → 关弹层；失败 → **原样**显示后端 msg、不关弹层。
 * 5. ⭐ **三条 island 陷阱**：类名 / 选择器两个常量（本文件全用导出常量，不手抄选择器）；
 *    `bootstrapIsland` 时标记还没塞、`initIslandElements` 时已塞（运行期判据）。
 *
 * ## 手法
 *
 * - `vi.mock('@/layui/island')`：jsdom 里加载不了真 layui，换成可控替身
 *   （`form.on` 把回调记进 Map、由测试手动派发——同 `system-setgames.spec.ts`）；
 * - `vi.mock('@/api/takerDepositQuota')`：只换网络出口，页面逻辑跑真实现；
 * - `vi.mock('@/layui-island/layerHost')`：`closeIslandLayer` 的真实现要父窗口的 `layer`
 *   （jsdom 里 `window.parent === window`）⇒ 换成 spy 才能断言「成功关弹层 / 失败不关」。
 *
 * ⚠️ 夹具一律**合成值**（E-0077）：人名 / 游戏名 / 金额都是编的。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const LAYUI = vi.hoisted(() => {
  const messages: string[] = []
  const confirms: Array<{ text: string; options: unknown; ok: (index: number) => void }> = []
  const closes: number[] = []
  const switchHandlers = new Map<string, (obj: { elem?: HTMLInputElement }) => void>()
  const seenByBootstrap: boolean[] = []
  const seenByInit: boolean[] = []
  const formRenders: string[] = []
  return {
    /** 与页面导出的 `SWITCH_ID` **同值**（`vi.mock` 工厂会被提升，拿不到 import 的常量）。 */
    SWITCH_ID: 'depositQuotaEnabled',
    messages,
    confirms,
    closes,
    switchHandlers,
    seenByBootstrap,
    seenByInit,
    formRenders,
    layer: {
      msg: vi.fn((text: string) => {
        messages.push(text)
      }),
      confirm: vi.fn((text: string, options: unknown, ok: (index: number) => void) => {
        confirms.push({ text, options, ok })
      }),
      close: vi.fn((index: number) => {
        closes.push(index)
      }),
    },
    form: {
      render: vi.fn((type?: string) => {
        formRenders.push(type ?? '')
      }),
      on: vi.fn((event: string, cb: (obj: { elem?: HTMLInputElement }) => void) => {
        switchHandlers.set(event, cb)
      }),
    },
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => {
    // 陷阱 ③ 的运行期判据：挂样式那一刻，页面标记**还没**进 DOM。
    LAYUI.seenByBootstrap.push(document.querySelector(`#${LAYUI.SWITCH_ID}`) !== null)
    return { form: LAYUI.form, layer: LAYUI.layer }
  }),
  initIslandElements: vi.fn(() => {
    // 陷阱 ③的后半：初始化那一刻，页面标记**已经**在 DOM 里。
    LAYUI.seenByInit.push(document.querySelector(`#${LAYUI.SWITCH_ID}`) !== null)
  }),
  islandMsg: (_layer: unknown, text: string) => {
    LAYUI.messages.push(text)
  },
}))

vi.mock('@/api/takerDepositQuota', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/takerDepositQuota')>()
  return { ...actual, fetchDepositQuota: vi.fn(), saveDepositQuota: vi.fn() }
})

vi.mock('@/layui-island/layerHost', () => ({ closeIslandLayer: vi.fn(() => true) }))

import { fetchDepositQuota, saveDepositQuota, type DepositQuota } from '@/api/takerDepositQuota'
import { BizError } from '@/api/errors'
import { closeIslandLayer } from '@/layui-island/layerHost'
import page, {
  AMOUNT_SELECTOR,
  DEPOSIT_QUOTA_MESSAGES,
  DISABLED_NOTICE_ID,
  ERROR_SELECTOR,
  EXPLANATION_ID,
  FILL_BUTTON_ID,
  FILL_INPUT_ID,
  GATE_HINT_TEXT,
  GLOBAL_FILL_BUTTON_ID,
  PAGE_KEY,
  PANEL_SELECTOR,
  ROW_SELECTOR,
  SAVE_BUTTON_ID,
  SWITCH_FILTER,
  SWITCH_ID,
  TITLE,
  buildHtml,
  buildErrorHtml,
  explanationText,
  isValidAmount,
  uidFromSearch,
} from '../taker-deposit-quota'

const fetchMock = vi.mocked(fetchDepositQuota)
const saveMock = vi.mocked(saveDepositQuota)
const closeMock = vi.mocked(closeIslandLayer)

/** 一款游戏都没填的合成夹具（`amount: null` ⇒ 空输入框）。 */
function quotaFixture(overrides: Partial<DepositQuota> = {}): DepositQuota {
  return {
    user_id: 3,
    username: 'sq001',
    display_name: '张三',
    enabled: false,
    games: [
      { game_id: 1, game_name: '原神', global_amount: '50.00', amount: null },
      { game_id: 2, game_name: '三角洲', global_amount: '300.00', amount: null },
    ],
    ...overrides,
  }
}

let root: HTMLElement

function flush(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

/** 把 `?uid=…` 写进 `window.location.search`（子页唯一的参数来源）。 */
function setSearch(query: string): void {
  const suffix = query === '' ? '' : `&${query}`
  window.history.replaceState({}, '', `/layui-island/page.html?page=${PAGE_KEY}${suffix}`)
}

async function mountPage(query = 'uid=3'): Promise<void> {
  setSearch(query)
  await page.mount(root)
  await flush()
}

function amountInputs(): HTMLInputElement[] {
  return Array.from(root.querySelectorAll<HTMLInputElement>(AMOUNT_SELECTOR))
}

function amountValues(): string[] {
  return amountInputs().map((input) => input.value)
}

function click(id: string): void {
  const btn = root.querySelector<HTMLButtonElement>(`#${id}`)
  expect(btn, `页面里没有 #${id}`).toBeTruthy()
  btn!.click()
}

beforeEach(() => {
  vi.clearAllMocks()
  LAYUI.messages.length = 0
  LAYUI.confirms.length = 0
  LAYUI.closes.length = 0
  LAYUI.switchHandlers.clear()
  LAYUI.seenByBootstrap.length = 0
  LAYUI.seenByInit.length = 0
  LAYUI.formRenders.length = 0
  document.body.innerHTML = ''
  root = document.createElement('div')
  root.id = 'app'
  document.body.appendChild(root)
  fetchMock.mockResolvedValue(quotaFixture())
  saveMock.mockResolvedValue(quotaFixture())
  closeMock.mockReturnValue(true)
})

afterEach(() => {
  window.history.replaceState({}, '', '/')
  document.body.innerHTML = ''
})

describe('uid 解析：只认正整数', () => {
  it('`uid=12` ⇒ 12；空 / 0 / 负数 / 小数 / 字母 / 尾随杂质 ⇒ null', () => {
    expect(uidFromSearch('?uid=12')).toBe(12)
    expect(uidFromSearch('uid=12&x=1')).toBe(12)
    expect(uidFromSearch('?uid=%2012%20')).toBe(12)
    expect(
      ['', '?uid=', '?uid=0', '?uid=-1', '?uid=1.5', '?uid=abc', '?uid=12x'].map((search) =>
        uidFromSearch(search),
      ),
    ).toEqual([null, null, null, null, null, null, null])
  })

  it('金额形状：非负、最多两位小数（0 合法，空串不合法——空输入框的归一在保存时做）', () => {
    expect(
      ['0', '0.00', '50', '50.5', '50.05', '9999999999.99'].map((v) => isValidAmount(v)),
    ).toEqual([true, true, true, true, true, true])
    expect(['', '-1', '1.234', '.5', '1.', '1e3', 'abc'].map((v) => isValidAmount(v))).toEqual([
      false,
      false,
      false,
      false,
      false,
      false,
      false,
    ])
  })
})

describe('没有 uid / uid 非法：显示错误、提示，且一个请求都不发', () => {
  it('空 uid ⇒ 错误块 + 提示，`fetchDepositQuota` 没被调用', async () => {
    await mountPage('')
    expect(fetchMock).not.toHaveBeenCalled()
    expect(root.querySelector(ERROR_SELECTOR)?.textContent).toContain('没有要设置押金额度的人')
    expect(LAYUI.messages).toContain('没有要设置押金额度的人')
  })

  it('uid=0 / uid=abc ⇒ 同上（不是白屏，也不是打一次 `/takers/0/…` 的无意义请求）', async () => {
    for (const query of ['uid=0', 'uid=abc', 'uid=-2', 'uid=1.5']) {
      fetchMock.mockClear()
      root.innerHTML = ''
      await mountPage(query)
      expect(fetchMock).not.toHaveBeenCalled()
      expect(root.textContent ?? '').toContain('没有要设置押金额度的人')
    }
  })
})

describe('加载失败：显示错误 + 后端原样 msg（不白屏）', () => {
  it('GET 拒绝 ⇒ 错误块含「加载失败」与后端 msg', async () => {
    fetchMock.mockRejectedValue(new BizError(1002, '账号不存在'))
    await mountPage()
    const text = root.textContent ?? ''
    expect(text).toContain('押金额度加载失败')
    expect(text).toContain('账号不存在')
    expect(root.querySelector(ERROR_SELECTOR)).not.toBeNull()
  })

  it('GET 拒绝但错误对象上没消息 ⇒ 仍显示「加载失败」，不抛', async () => {
    fetchMock.mockRejectedValue(new Error(''))
    await expect(mountPage()).resolves.toBeUndefined()
    expect(root.textContent ?? '').toContain('押金额度加载失败')
  })
})

describe('渲染：每款游戏一行、全局额度只读、空额度显示空', () => {
  it('页面 key / 标题常量与文件名一致；标题写进 `document.title`', async () => {
    expect(PAGE_KEY).toBe('taker-deposit-quota')
    expect(TITLE).toBe('押金额度')
    await mountPage()
    expect(document.title).toBe(TITLE)
    expect(LAYUI.SWITCH_ID).toBe(SWITCH_ID)
    expect(root.querySelector(PANEL_SELECTOR)).not.toBeNull()
  })

  it('行数 / 游戏名 / 全局额度只读 / 个人额度初值（null ⇒ 空 + placeholder 0）', async () => {
    fetchMock.mockResolvedValue(
      quotaFixture({
        games: [
          { game_id: 1, game_name: '原神', global_amount: '50.00', amount: null },
          { game_id: 2, game_name: '三角洲', global_amount: '300.00', amount: '300.00' },
        ],
      }),
    )
    await mountPage()
    const rows = Array.from(root.querySelectorAll(ROW_SELECTOR))
    expect(rows).toHaveLength(2)
    expect(rows[0]!.textContent).toContain('原神')
    expect(rows[0]!.textContent).toContain('50.00')
    expect(rows[1]!.textContent).toContain('三角洲')
    expect(rows[1]!.textContent).toContain('300.00')

    const inputs = amountInputs()
    expect(inputs).toHaveLength(2)
    expect(inputs[0]!.value).toBe('')
    expect(inputs[0]!.placeholder).toBe('0')
    expect(inputs[1]!.value).toBe('300.00')
    // 「全局额度」那格是**只读展示**：一行里只有一个 input（个人额度那个）。
    expect(rows[1]!.querySelectorAll('input')).toHaveLength(1)

    // 开关：layui switch + 初值来自 `enabled`
    const sw = root.querySelector<HTMLInputElement>(`#${SWITCH_ID}`)!
    expect(sw.getAttribute('lay-skin')).toBe('switch')
    expect(sw.checked).toBe(false)
    expect(root.querySelector<HTMLElement>(`#${EXPLANATION_ID}`)!.textContent).toBe(
      explanationText(false),
    )
    // layui 的 switch 是 `form.render()` 画出来的：必须真调过一次（无参全量渲染）。
    expect(LAYUI.formRenders).toContain('')
  })

  it('游戏名等外部值过 `escapeHtml`（不产生真 DOM 元素）', async () => {
    fetchMock.mockResolvedValue(
      quotaFixture({
        games: [
          {
            game_id: 1,
            game_name: '<img src=x onerror="alert(1)">',
            global_amount: '1.00',
            amount: '<b>9</b>',
          },
        ],
      }),
    )
    await mountPage()
    expect(root.querySelector('img')).toBeNull()
    expect(root.querySelector('b')).toBeNull()
    expect(root.textContent ?? '').toContain('<img')
  })

  it('三条 island 陷阱：bootstrap 时标记还没塞、initIslandElements 时已塞', async () => {
    await mountPage()
    expect(LAYUI.seenByBootstrap).toEqual([false])
    expect(LAYUI.seenByInit).toEqual([true])
  })

  it('开关切换（开 → 关）⇒ 说明文案跟着变（可交互的那一档 = 列表里总闸已开）', async () => {
    // ⚠️ 总闸关着时这颗开关是 `disabled` 的（只读），切换不会发生 ⇒ 这条用例必须挂「已启用」。
    fetchMock.mockResolvedValue(quotaFixture({ enabled: true }))
    await mountPage()
    const sw = root.querySelector<HTMLInputElement>(`#${SWITCH_ID}`)!
    const handler = LAYUI.switchHandlers.get(`switch(${SWITCH_FILTER})`)
    expect(handler, `页面必须注册 switch(${SWITCH_FILTER})`).toBeTypeOf('function')
    sw.checked = false
    handler!({ elem: sw })
    expect(root.querySelector<HTMLElement>(`#${EXPLANATION_ID}`)!.textContent).toBe(
      explanationText(false),
    )
    expect(explanationText(false)).toContain('全局押金额度')
  })
})

describe('一键填充 / 按全局额度填充（覆盖前确认）', () => {
  it('统一额度写进**所有**输入框；原来都是空的 ⇒ 不弹确认', async () => {
    fetchMock.mockResolvedValue(quotaFixture({ enabled: true }))
    await mountPage()
    root.querySelector<HTMLInputElement>(`#${FILL_INPUT_ID}`)!.value = '88.5'
    click(FILL_BUTTON_ID)
    expect(amountValues()).toEqual(['88.5', '88.5'])
    expect(LAYUI.confirms).toHaveLength(0)
  })

  it('统一额度为空 / 非法 ⇒ 提示且**一个输入框都不动**', async () => {
    fetchMock.mockResolvedValue(quotaFixture({ enabled: true }))
    await mountPage()
    const fill = root.querySelector<HTMLInputElement>(`#${FILL_INPUT_ID}`)!
    for (const [raw, expected] of [
      ['', '请输入统一额度'],
      ['-5', '额度必须是大于等于 0 的数字，最多两位小数'],
      ['1.234', '额度必须是大于等于 0 的数字，最多两位小数'],
    ] as const) {
      fill.value = raw
      LAYUI.messages.length = 0
      click(FILL_BUTTON_ID)
      expect(LAYUI.messages).toContain(expected)
      expect(amountValues()).toEqual(['', ''])
    }
  })

  it('覆盖已有非空且不同的值 ⇒ 先 confirm；确认前不动，确认后才写', async () => {
    fetchMock.mockResolvedValue(
      quotaFixture({
        enabled: true,
        games: [
          { game_id: 1, game_name: '原神', global_amount: '50.00', amount: '10' },
          { game_id: 2, game_name: '三角洲', global_amount: '300.00', amount: '20' },
        ],
      }),
    )
    await mountPage()
    root.querySelector<HTMLInputElement>(`#${FILL_INPUT_ID}`)!.value = '99'
    click(FILL_BUTTON_ID)
    expect(LAYUI.confirms).toHaveLength(1)
    expect(LAYUI.confirms[0]!.text).toContain('已填写的个人额度将被覆盖，确定继续吗？')
    // 确认之前：原值一点没动（这条是 E-0027 同型的防静默改值）。
    expect(amountValues()).toEqual(['10', '20'])
    LAYUI.confirms[0]!.ok(5)
    expect(amountValues()).toEqual(['99', '99'])
    // 确认框被关掉（layui 的 confirm 回调里必须自己 close）。
    expect(LAYUI.closes).toContain(5)
  })

  it('已有值与新值相同 ⇒ 不算覆盖，不弹 confirm', async () => {
    fetchMock.mockResolvedValue(
      quotaFixture({
        enabled: true,
        games: [{ game_id: 1, game_name: '原神', global_amount: '50.00', amount: '50.00' }],
      }),
    )
    await mountPage()
    click(GLOBAL_FILL_BUTTON_ID)
    expect(LAYUI.confirms).toHaveLength(0)
    expect(amountValues()).toEqual(['50.00'])
  })

  it('按全局额度填充：各自写自己的 `global_amount`；有冲突时也先确认', async () => {
    fetchMock.mockResolvedValue(
      quotaFixture({
        enabled: true,
        games: [
          { game_id: 1, game_name: '原神', global_amount: '50.00', amount: null },
          { game_id: 2, game_name: '三角洲', global_amount: '300.00', amount: '7' },
        ],
      }),
    )
    await mountPage()
    click(GLOBAL_FILL_BUTTON_ID)
    expect(LAYUI.confirms).toHaveLength(1)
    // 有冲突就**整批**等确认（不是「先把不冲突的填上、再问冲突的」）——确认前一个都不动。
    expect(amountValues()).toEqual(['', '7'])
    LAYUI.confirms[0]!.ok(1)
    expect(amountValues()).toEqual(['50.00', '300.00'])
  })
})

describe('保存：载荷 / 本地校验 / 成功关弹层 / 失败原样显示 msg', () => {
  // ⚠️ 本组用例一律挂「列表里总闸已打开」（`enabled: true`）：关着的时候页面上所有
  // 输入与保存都是 `disabled`（见本文件末尾那组「总闸关着 ⇒ 只读」），点了不发请求。
  it('空输入框 ⇒ `"0"`；开关状态来自 switch；成功后关弹层', async () => {
    fetchMock.mockResolvedValue(
      quotaFixture({
        enabled: true,
        games: [
          { game_id: 1, game_name: '原神', global_amount: '50.00', amount: null },
          { game_id: 2, game_name: '三角洲', global_amount: '300.00', amount: '300.00' },
        ],
      }),
    )
    await mountPage()
    root.querySelector<HTMLInputElement>(`#${SWITCH_ID}`)!.checked = true
    click(SAVE_BUTTON_ID)
    await flush()

    expect(saveMock).toHaveBeenCalledTimes(1)
    expect(saveMock).toHaveBeenCalledWith(3, {
      enabled: true,
      // 空输入框归一成 `"0"`（0 是合法值 = 该游戏免押），**原样**保留已填的字符串。
      amounts: { '1': '0', '2': '300.00' },
    })
    expect(closeMock).toHaveBeenCalledTimes(1)
    expect(LAYUI.messages).toContain('押金额度已保存')
  })

  it('开关关 ⇒ `enabled: false`（真值判断不能把 false 吃掉）', async () => {
    fetchMock.mockResolvedValue(quotaFixture({ enabled: true }))
    await mountPage()
    // 已启用状态下把弹层里那颗开关拨关（可交互的那一档）。
    root.querySelector<HTMLInputElement>(`#${SWITCH_ID}`)!.checked = false
    click(SAVE_BUTTON_ID)
    await flush()
    expect(saveMock).toHaveBeenCalledWith(3, {
      enabled: false,
      amounts: { '1': '0', '2': '0' },
    })
  })

  it('负数 / 三位小数 / 非数字 ⇒ 提示且**不发请求**、不关弹层', async () => {
    fetchMock.mockResolvedValue(quotaFixture({ enabled: true }))
    await mountPage()
    for (const bad of ['-1', '1.234', 'abc']) {
      amountInputs()[0]!.value = bad
      saveMock.mockClear()
      closeMock.mockClear()
      LAYUI.messages.length = 0
      click(SAVE_BUTTON_ID)
      await flush()
      expect(saveMock).not.toHaveBeenCalled()
      expect(LAYUI.messages).toContain('额度必须是大于等于 0 的数字，最多两位小数')
      expect(closeMock).not.toHaveBeenCalled()
    }
  })

  it('0 合法：填 0 原样提交（不是被当成「没填」丢掉）', async () => {
    fetchMock.mockResolvedValue(quotaFixture({ enabled: true }))
    await mountPage()
    amountInputs()[0]!.value = '0'
    click(SAVE_BUTTON_ID)
    await flush()
    expect(saveMock).toHaveBeenCalledWith(3, {
      enabled: true,
      amounts: { '1': '0', '2': '0' },
    })
  })

  it('PUT 失败 ⇒ **原样**显示后端 msg，且不关弹层（让用户能重试）', async () => {
    saveMock.mockRejectedValue(new BizError(1000, '个人额度不能超过 9999999999.99'))
    fetchMock.mockResolvedValue(quotaFixture({ enabled: true }))
    await mountPage()
    click(SAVE_BUTTON_ID)
    await flush()
    expect(LAYUI.messages).toContain('个人额度不能超过 9999999999.99')
    expect(LAYUI.messages).not.toContain('押金额度已保存')
    expect(closeMock).not.toHaveBeenCalled()
  })

  it('PUT 失败且没有可读消息 ⇒ 回落到「保存失败」', async () => {
    saveMock.mockRejectedValue(new Error(''))
    fetchMock.mockResolvedValue(quotaFixture({ enabled: true }))
    await mountPage()
    click(SAVE_BUTTON_ID)
    await flush()
    expect(LAYUI.messages).toContain('保存失败')
  })

  it('连点两次保存只发一次 PUT（`saving` 挡住重复提交）', async () => {
    let resolveSave: ((value: DepositQuota) => void) | undefined
    saveMock.mockImplementation(
      () =>
        new Promise<DepositQuota>((resolve) => {
          resolveSave = resolve
        }),
    )
    fetchMock.mockResolvedValue(quotaFixture({ enabled: true }))
    await mountPage()
    click(SAVE_BUTTON_ID)
    click(SAVE_BUTTON_ID)
    await flush()
    expect(saveMock).toHaveBeenCalledTimes(1)
    resolveSave!(quotaFixture())
    await flush()
    expect(LAYUI.messages).toContain('押金额度已保存')
  })
})

describe('⭐ 列表里总闸关着（`enabled: false`）⇒ 提示 + 整页只读、保存不了', () => {
  // 业务方 2026-10-23：关着 = 这个人不能用个人押金额度。点「押金额度」按钮**仍能进弹层**
  // （不静默），但**必须**看到「未启用个人押金额度，请先在列表里打开」，且输入与保存都禁用。
  // ⛔ 判据不能只是「提示出现了」：那样把 `disabled` 删掉照样绿（用户填完才被拒）。

  it('顶部逐字提示；开关 / 统一额度 / 两颗填充 / 每行输入 / 保存**全部** disabled', async () => {
    await mountPage() // 默认夹具就是 enabled: false
    const notice = root.querySelector<HTMLElement>(`#${DISABLED_NOTICE_ID}`)
    expect(notice, '总闸关着时必须显示提示块').not.toBeNull()
    expect(notice!.textContent).toBe('未启用个人押金额度，请先在列表里打开')
    // 常量与 DOM 同一句话（改一个字两条一起红）。
    expect(DEPOSIT_QUOTA_MESSAGES.disabledNotice).toBe(notice!.textContent)

    const mustBeDisabled = [
      root.querySelector<HTMLInputElement>(`#${SWITCH_ID}`)!,
      root.querySelector<HTMLInputElement>(`#${FILL_INPUT_ID}`)!,
      root.querySelector<HTMLButtonElement>(`#${FILL_BUTTON_ID}`)!,
      root.querySelector<HTMLButtonElement>(`#${GLOBAL_FILL_BUTTON_ID}`)!,
      root.querySelector<HTMLButtonElement>(`#${SAVE_BUTTON_ID}`)!,
      ...amountInputs(),
    ]
    // 5 个固定控件 + 每款游戏一个输入框（夹具 2 款）⇒ 7 个
    for (const el of mustBeDisabled) {
      expect(el.disabled, `${el.id || el.className} 应当禁用`).toBe(true)
    }
    // 每款游戏那一行的输入框也都在名单里（夹具 2 行 ⇒ 上面 `mustBeDisabled` 实际 7 个）。
    expect(amountInputs()).toHaveLength(2)
    expect(mustBeDisabled).toHaveLength(7)
  })

  it('点了保存 / 一键填充 **不发请求、不改输入框**（禁用不是「看着灰、点下去还写」）', async () => {
    await mountPage()
    const before = amountValues()
    click(SAVE_BUTTON_ID)
    click(FILL_BUTTON_ID)
    click(GLOBAL_FILL_BUTTON_ID)
    await flush()
    expect(saveMock).not.toHaveBeenCalled()
    expect(amountValues()).toEqual(before)
    expect(closeMock).not.toHaveBeenCalled()
  })

  it('`buildHtml` 两条分支互斥：关 ⇒ 有提示且全 disabled；开 ⇒ 无提示、无 disabled', () => {
    const off = new DOMParser().parseFromString(buildHtml(quotaFixture()), 'text/html')
    expect(off.querySelector(`#${DISABLED_NOTICE_ID}`)).not.toBeNull()
    expect(off.querySelector(`#${SAVE_BUTTON_ID}`)!.hasAttribute('disabled')).toBe(true)
    expect(off.querySelector(`#${SWITCH_ID}`)!.hasAttribute('disabled')).toBe(true)

    const on = new DOMParser().parseFromString(
      buildHtml(quotaFixture({ enabled: true })),
      'text/html',
    )
    expect(on.querySelector(`#${DISABLED_NOTICE_ID}`)).toBeNull()
    expect(on.querySelector(`#${SAVE_BUTTON_ID}`)!.hasAttribute('disabled')).toBe(false)
    expect(on.querySelector(`#${SWITCH_ID}`)!.hasAttribute('disabled')).toBe(false)
    for (const el of Array.from(on.querySelectorAll('input, button'))) {
      expect(el.hasAttribute('disabled'), '总闸开着时不该有禁用控件').toBe(false)
    }
  })
})

describe('纯标记函数（不挂载也能自证）', () => {
  it('`buildErrorHtml` 转义外部文案、带错误类名', () => {
    const doc = new DOMParser().parseFromString(
      buildErrorHtml('<script>alert(1)</script>'),
      'text/html',
    )
    expect(doc.querySelector(ERROR_SELECTOR)).not.toBeNull()
    expect(doc.querySelector('script')).toBeNull()
  })

  it('`buildHtml` 开关开时带 `checked`，说明文案随之变', () => {
    const doc = new DOMParser().parseFromString(buildHtml(quotaFixture({ enabled: true })), 'text/html')
    expect(doc.querySelector<HTMLInputElement>(`#${SWITCH_ID}`)!.hasAttribute('checked')).toBe(true)
    expect(doc.querySelector(`#${EXPLANATION_ID}`)!.textContent).toBe(explanationText(true))
  })

  it('页面上常驻一句「押金闸没开时配了也不冻」的提示（计划 §2.2 的如实告知）', () => {
    const doc = new DOMParser().parseFromString(buildHtml(quotaFixture()), 'text/html')
    expect(doc.querySelector(PANEL_SELECTOR)!.textContent).toContain(GATE_HINT_TEXT)
    expect(GATE_HINT_TEXT).toContain('押金闸已打开')
  })
})
