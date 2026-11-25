// @vitest-environment jsdom
/**
 * 【录入订单】layui 复刻页的**快速指派三件套** spec（业务方 2026-10-03 裁定；决策 D-0179）。
 *
 * 三件套 = **选人**（`.KSPD` → `.kszpDiv` 层）+ **给打手发单价**（`#throwFee`，业务方裁定
 * 就是 `Order.hall_price`）+ **默认指派此人**（`defAss` → 我自己档案上的 `default_assignee`）。
 *
 * ⚠️ 为什么单独一个文件（不塞进 `order-addorder.spec.ts`）：那一份已经 3000+ 行、覆盖的是
 * 「DOM ⇄ 模型 ⇄ 载荷」「权限门」「T6 来源」等**别的**目的；本线是一组**有对标出处的交互**
 * （`addorder.js` L1048-1153 / L1541-1588），独立成篇才看得清哪些是本线新增的判据。
 * 两份文件共用同一套「假 layui + 证据件原文 + 真页面模块」的挂载手法。
 */
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AxiosError } from 'axios'
import * as errorsModule from '@/api/errors'
import type { AssigneeOption } from '@/api/orders'
import {
  ASSIGNEE_OPTIONS_LIMIT,
  FIELD_MAP,
  HALL_ASSIGNEE_LABEL,
  HALL_PRICE_REQUIRED_MESSAGE,
  HALL_RADIO_VALUE,
  IGNORED_FIELDS,
  KSZP_LAYER_TITLE,
  NOT_OPEN_CONTROLS,
  ORDER_CREATED_ASSIGN_FAILED,
  ORDER_CREATED_THROW_FAILED,
  RESUBMIT_CONFIRM_MESSAGE,
  SUBMIT_OUTCOME_UNKNOWN_MESSAGE,
  THROW_FEE_TIP,
  THROW_FEE_TIP_HALL,
  isUnknownSubmitOutcome,
  assigneeThrowFee,
  mountOrderAddOrder,
  rememberAssigneeValue,
} from '../order-addorder'

/** 假 layui：只实现本页用到的那部分（与 `order-addorder.spec.ts` 同一手法）。 */
const LAYUI = vi.hoisted(() => ({
  msg: vi.fn(),
  // ⚠️ 形参写成 `unknown` 才能从 `mock.calls[0][0]` 读回 `layer.open` 的实参（无参替身会让
  //    `calls` 的类型是 `[]` ⇒ `vue-tsc` 报「Tuple type '[]' of length '0' has no element at index '0'」）。
  open: vi.fn((_options?: unknown) => 42),
  close: vi.fn(),
  tips: vi.fn(() => 7),
  /** `layer.confirm(content, options, yes, cancel)` 的替身：把 `yes` 记下来供测试驱动。 */
  confirm: vi.fn((_content?: unknown, _options?: unknown, yes?: () => void) => {
    LAYUI.confirmYes = yes
    return 9
  }),
  confirmYes: undefined as undefined | (() => void),
  render: vi.fn(),
  handlers: new Map<string, (...args: unknown[]) => unknown>(),
}))

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  const fake = {
    form: {
      render: LAYUI.render,
      on: (evt: string, cb: (...args: unknown[]) => unknown) => LAYUI.handlers.set(evt, cb),
    },
    layer: {
      msg: LAYUI.msg,
      open: LAYUI.open,
      close: LAYUI.close,
      tips: LAYUI.tips,
      confirm: LAYUI.confirm,
      prompt: vi.fn(),
    },
    laytpl: (tpl: string) => ({ render: () => tpl }),
    util: {},
    element: { init: vi.fn() },
    formSelects: {
      data: vi.fn(),
      value: vi.fn(),
      on: vi.fn(),
    },
    // 快速指派：`area` 的桌面/移动判据 + `layer.open` 的 `content` 必须是 jQuery 对象。
    device: () => ({ android: false, ios: false }),
    $: (selector: string) => ({ selector }),
  }
  return { ...actual, bootstrapIsland: (async () => fake) as unknown as typeof actual.bootstrapIsland }
})

const HERE = path.dirname(fileURLToPath(import.meta.url))
const EVIDENCE = readFileSync(
  path.resolve(HERE, '../../../../public/replica/order-addorder.html'),
  'utf8',
)

/** 一次请求的替身记录（只看我们关心的字段）。 */
type Sent = {
  url?: string
  method?: string
  params?: Record<string, unknown>
  data?: Record<string, unknown>
}

const GAME_ROW = { id: 2, name: '原神' }
const CANDIDATES = [
  { id: 7, name: '测试昵称甲', role: '代练员', tag: '标记甲', remark: '备注甲' },
  { id: 9, name: '测试昵称乙', role: '代练员', tag: '', remark: '' },
]

type Options = {
  items?: AssigneeOption[]
  defAssRecManUid?: number
  defAssRecMan?: string
  /** ⭐ 本线新增：服务端记着的**档位**是不是「内部抢单」（后端键 `defAssIsHall`）。 */
  defAssIsHall?: boolean
  truncated?: boolean
  /** 覆盖 `POST /api/business/orders/` 的响应（默认成功）。 */
  createResponse?: { code: number; msg: string; data: unknown }
  /**
   * 让**建单**那一发以「结果未知」的形状失败（评审 Important-1 的两种真实形状）：
   *  - `'non-envelope'`：响应不是统一信封（Django 未预期异常的 **500 HTML** 就是这一支）
   *    ⇒ 真 `request()` 抛 `NetworkError`；
   *  - `'http500'`：带响应的 **HTTP 500** ⇒ 真 `request()` 抛 `HttpError(500, …)`。
   */
  createThrows?: 'non-envelope' | 'http500'
  /** 让候选名单那一发**失败**（钉「失败要如实提示」）。 */
  optionsFail?: boolean
  orderNo?: string
}

/** 挂载一个**新建态**的录单页，并把词典 / 候选名单 / 建单三处 stubbed 出来。 */
async function mountCreate(options: Options = {}): Promise<{ sent: Sent[]; root: HTMLElement }> {
  window.history.replaceState(
    {},
    '',
    options.orderNo === undefined
      ? '/?page=order-addorder'
      : `/?page=order-addorder&order_no=${options.orderNo}`,
  )
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      if (String(url).includes('order-addorder.html')) {
        return { ok: true, status: 200, text: async () => EVIDENCE }
      }
      throw new Error(`未预期的 fetch：${String(url)}`)
    }),
  )
  const { http } = await import('@/api/http')
  const sent: Sent[] = []
  vi.spyOn(http, 'request').mockImplementation((async (config: Sent) => {
    sent.push(config)
    const url = config.url ?? ''
    const method = (config.method ?? 'get').toLowerCase()
    if (url.includes('/assignee-options/')) {
      if (options.optionsFail === true) throw new Error('网络异常，请检查连接')
      return {
        data: {
          code: 0,
          msg: 'ok',
          data: {
            items: options.items ?? [],
            truncated: options.truncated ?? false,
            roles: ['代练员'],
            defAssRecManUid: options.defAssRecManUid ?? 0,
            defAssRecMan: options.defAssRecMan ?? '',
            defAssIsHall: options.defAssIsHall ?? false,
          },
        },
      }
    }
    if (url.includes('/games/')) {
      return {
        data: {
          code: 0,
          msg: 'ok',
          data: { items: [GAME_ROW], total: 1, page: 1, page_size: 200 },
        },
      }
    }
    if (url.includes('/auth/me')) {
      return {
        data: {
          code: 0,
          msg: 'ok',
          data: {
            user: {
              username: 'u',
              display_name: 'u',
              game_ids: [2],
              menu_permissions: [
                'order.add_edit',
                'order.send_to_grab_hall',
                'order.submit_edit',
                'order.view_detail',
                'order.view_content',
              ],
            },
          },
        },
      }
    }
    if (url.endsWith('/api/business/orders/') && method === 'post') {
      if (options.createThrows === 'non-envelope') {
        // 非信封（Django 500 的 HTML）⇒ 真 `send()` 抛 `NetworkError`（"后端返回了非信封响应"）
        return { data: '<html><body>Server Error (500)</body></html>' }
      }
      if (options.createThrows === 'http500') {
        // 带响应的 500 ⇒ 真 `request()` 走 `mapHttpStatus(500, …)` ⇒ `HttpError(500)`
        throw new AxiosError('boom', 'ERR_BAD_RESPONSE', undefined, undefined, {
          status: 500,
          data: { code: 500, msg: '服务器错误', data: null },
        } as never)
      }
      return {
        data: options.createResponse ?? {
          code: 0,
          msg: 'ok',
          data: { order_no: '20260921000000000009' },
        },
      }
    }
    return {
      data: { code: 0, msg: 'ok', data: { items: [], total: 0, page: 1, page_size: 200 } },
    }
  }) as never)

  const root = document.createElement('div')
  document.body.appendChild(root)
  await mountOrderAddOrder(root, { onNavigate: vi.fn() })
  return { sent, root }
}

/** 填一个能过必填校验的表单并走保存那条链（`submit(savebtn)` → `onSave` → 真实出口）。 */
async function save(root: HTMLElement): Promise<void> {
  root.querySelector<HTMLInputElement>('#project')!.value = '深渊满星'
  const handler = LAYUI.handlers.get('submit(savebtn)')
  expect(handler).toBeTypeOf('function')
  handler!()
  await new Promise((resolve) => setTimeout(resolve, 0))
  await new Promise((resolve) => setTimeout(resolve, 0))
}

const postCalls = (sent: Sent[]): Sent[] =>
  sent.filter((c) => (c.method ?? '').toLowerCase() === 'post' && c.url === '/api/business/orders/')

const kspd = (root: HTMLElement): HTMLElement => root.querySelector<HTMLElement>('.KSPD')!
const throwLayer = (root: HTMLElement): HTMLElement =>
  root.querySelector<HTMLElement>('.throwFeelayer')!
const defAss = (root: HTMLElement): HTMLInputElement =>
  root.querySelector<HTMLInputElement>('input[name=defAss]')!

/** 走**插件的事件契约**选一个接手人（radio 是 layui 的 `form.on('radio(userlist)')`）。 */
function pick(root: HTMLElement, value: string): void {
  void root
  const handler = LAYUI.handlers.get('radio(userlist)')
  expect(handler).toBeTypeOf('function')
  handler!({ value })
}

/** `.kszp` 里当前带 `checked` 的那些 radio 的 `value`（判「哪一档被选中」用的读写口）。 */
function checkedRadioValues(root: HTMLElement): string[] {
  return Array.from(root.querySelectorAll<HTMLInputElement>('.kszp input[name=recUser]'))
    .filter((r) => r.hasAttribute('checked'))
    .map((r) => r.value)
}

/** 点一个按钮并等那一发重拉走完（`loadCandidates` 是 `await` 里的真请求）。 */
async function clickAndSettle(el: HTMLElement): Promise<void> {
  el.click()
  await new Promise((resolve) => setTimeout(resolve, 0))
  await new Promise((resolve) => setTimeout(resolve, 0))
}

beforeEach(() => {
  LAYUI.handlers.clear()
  for (const fn of [LAYUI.msg, LAYUI.open, LAYUI.close, LAYUI.tips, LAYUI.render, LAYUI.confirm])
    fn.mockClear()
  LAYUI.confirmYes = undefined
  LAYUI.open.mockReturnValue(42)
  LAYUI.tips.mockReturnValue(7)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('快速指派（D-0179）：登记表与纯函数', () => {
  it('三件套已从「尚未开放」清单里摘出（逐条枚举，⛔ 不是整表 grep）', () => {
    const selectors = NOT_OPEN_CONTROLS.map((c) => c.selector)
    for (const gone of ['#throwFee', 'input[name=defAss]', '.KSPD', '.seachBtn', '.resetSeachBtn']) {
      expect(selectors).not.toContain(gone)
    }
    // 「排序方式」**新登记**为不生效（照渲染 + 点了有提示，⛔ 不静默无效、也不假装生效）。
    expect(selectors).toContain('input[name=userOrderBy]')
    // `IGNORED_FIELDS` 里那三条「后端没有」的老理由整条**不在**了（它们改成了「有专门路径」）。
    expect(IGNORED_FIELDS.defAss).not.toContain('后端没有')
    expect(IGNORED_FIELDS.recUser).not.toContain('我们没有')
    expect(IGNORED_FIELDS.seachUserKey1).not.toContain('隐藏')
    // ⛔ `throwFee` **不再出现在这张表里**：它进了 `FIELD_MAP`（值 = `hall_price`）。
    expect(Object.keys(IGNORED_FIELDS)).not.toContain('throwFee')
    expect(FIELD_MAP.throwFee).toBe('hall_price')
  })

  it('`#throwFee` → `hall_price` 的写入口排在 `#throwInterPrice` **之后**（两格同列，顺序承重）', () => {
    const keys = Object.keys(FIELD_MAP)
    // 同一列的两个控件：`readFormState` 逐键覆盖 ⇒ 后写的赢。改这两行的顺序会让发单价静默取到
    // `#throwInterPrice`（`.pushplatdiv` 里那个恒为 `0` 的隐藏格）。
    const hallPriceKeys = keys.filter((k) => FIELD_MAP[k as keyof typeof FIELD_MAP] === 'hall_price')
    expect(hallPriceKeys).toEqual(['throwInterPrice', 'throwFee'])
  })

  it('算价逐档照对标（ORDER_PERCENT 20 / FIXED 10 / 没有数据）', () => {
    expect(assigneeThrowFee({ mode: 20, value: 90 }, '100')).toBe('90.00')
    // ⚠️ `169.575` 走 JS 的 `toFixed(2)` 得 `'169.57'`（IEEE-754 的二进制近似，见
    // `config/money.ts::formatMoney` 的同一条注释）——对标用的是同一个 `toFixed(2)`，
    // 所以这里**照抄结果**，⛔ 不「修」成四舍五入的 `169.58`。
    expect(assigneeThrowFee({ mode: 20, value: 85 }, '199.50')).toBe('169.57')
    expect(assigneeThrowFee({ mode: 10, value: 12.5 }, '100')).toBe('12.5')
    // ⚠️ 与对标的两处有意不同都在这里钉住：没有数据 / 提成为 0 ⇒ `'0'`（不是空串，
    // 也不是对标拿空接单价乘出来的 `"NaN"`）。
    expect(assigneeThrowFee({}, '100')).toBe('0')
    expect(assigneeThrowFee({ mode: 20, value: 0 }, '100')).toBe('0')
    // 接单价为空串 ⇒ JS 里 `'' * x` 是 `0`（对标同一条算式也回 `0.00`）
    expect(assigneeThrowFee({ mode: 20, value: 90 }, '')).toBe('0.00')
    // 但接单价**不是数**时对标会把 `NaN` 填进框里（`(x/100)*NaN` 的 `toFixed`），我们回 `'0'`
    expect(assigneeThrowFee({ mode: 20, value: 90 }, 'abc')).toBe('0')
    expect(assigneeThrowFee({ mode: 30, value: 90 }, '100')).toBe('0')
  })

  it('「默认指派此人」的四态：记住人 / 记住内部抢单 / 清除 / 不动', () => {
    // ── 原来的三态（本单不是 hall 档）**逐字不变** ─────────────────────────────
    expect(rememberAssigneeValue(7, true, 0)).toBe('Y')
    expect(rememberAssigneeValue(7, false, 7)).toBe('N')
    expect(rememberAssigneeValue(null, false, 7)).toBe('N')
    // 勾着但这次没选中人（例如那个默认接手人已不在名单里）⇒ **不动**（⛔ 不悄悄清掉偏好）
    expect(rememberAssigneeValue(null, true, 7)).toBeUndefined()
    // 本来就没有默认 ⇒ 什么都不发（每次录单白发一条清除是没有意义的写）
    expect(rememberAssigneeValue(null, false, 0)).toBeUndefined()

    // ── ⭐ 本线新增的第四档「内部抢单」（业务方 2026-10-08 裁定 R1/R2）────────────
    // 本单 hall 档 + 勾着 ⇒ `'hall'`（记住「默认抛内部抢单」这个**档位**；这一档没有人被选中）
    expect(rememberAssigneeValue(null, true, 0, true, false)).toBe('hall')
    // ⚠️ 服务端**已经**记着 hall 时也照发（不是「没变就不发」）：那次提交本身是用户的新意思表示，
    //    而后端的「同值不写库」自己会把它变成一次 no-op（⛔ 前端不替后端判这个）
    expect(rememberAssigneeValue(null, true, 0, true, true)).toBe('hall')
    // 本单 hall 档 + 没勾，而**服务端本来记着一个人** ⇒ `'N'`（取消勾选 = 清除，照对标 tip 语义）
    expect(rememberAssigneeValue(null, false, 7, true, false)).toBe('N')
    // 本单 hall 档 + 没勾，而**服务端本来记着 hall** ⇒ `'N'`（同上；两列互斥，只可能一种为真）
    expect(rememberAssigneeValue(null, false, 0, true, true)).toBe('N')
    // 本单 hall 档 + 没勾 + 服务端**本来什么都没有** ⇒ 不发（没有东西可清）
    expect(rememberAssigneeValue(null, false, 0, true, false)).toBeUndefined()
    // ⚠️ 与「勾着但没选中人」那条边界**互不影响**：hall 档带着 `selectedId` 也照 hall 判
    //    （这一档根本没有人可选中，写进来只是为了钉住判据顺序——先看档位，再看勾没勾）
    expect(rememberAssigneeValue(7, true, 0, true, false)).toBe('hall')
  })

})

describe('快速指派：点 `.KSPD` 开层（对标 addorder.js L1541-1588）', () => {
  it('层参数逐条照对标：type1 / 标题 / shade / shadeClose / area / 内容 = 页内 `.kszpDiv`', async () => {
    const { root } = await mountCreate({ items: CANDIDATES })
    kspd(root).click()
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    const arg = LAYUI.open.mock.calls[0]![0] as Record<string, unknown>
    expect(arg.type).toBe(1)
    expect(arg.title).toBe(KSZP_LAYER_TITLE)
    expect(arg.shade).toEqual([0.3, '#333'])
    expect(arg.shadeClose).toBe(true)
    expect(arg.area).toEqual(['85%', '60%'])
    // content 必须交给 layui 的 jQuery（传裸元素 layui 会抛 `r.parents is not a function`）
    expect(arg.content).toEqual({ selector: '.kszpDiv' })
    // ⛔ 对标这一支**没有** skin / anim / maxmin —— 有就是抄错了另一支
    expect(arg.skin).toBeUndefined()
    expect(arg.anim).toBeUndefined()
    expect(arg.maxmin).toBeUndefined()
    // cancel / end 都把容器藏回去（对标 L1578-1583）
    expect(typeof arg.cancel).toBe('function')
    expect(typeof arg.end).toBe('function')
    ;(arg.end as () => void)()
    expect(root.querySelector<HTMLElement>('.kszpDiv')!.style.display).toBe('none')
  })

  it('候选名单灌进 `.kszp`：每颗 `.layui-inline[style="width:195px"]` + `name=recUser` + 顺序 = 暂不指派 → 内部抢单 → 候选人', async () => {
    const { root } = await mountCreate({ items: CANDIDATES })
    const wrappers = Array.from(root.querySelectorAll<HTMLElement>('.kszp > .layui-inline'))
    expect(wrappers.length).toBe(4) // 暂不指派 + 内部抢单 + 2 位候选
    for (const wrapper of wrappers) {
      expect(wrapper.getAttribute('style')).toBe('width:195px')
    }
    const radios = Array.from(root.querySelectorAll<HTMLInputElement>('.kszp input[name=recUser]'))
    // ⭐ **渲染顺序 = 暂不指派 → 内部抢单 → 候选人**（对标 `addorder.js:1054` 就是这两颗相邻，
    //    内部抢单紧跟「暂不指派」之后、候选名单之前）。
    // ⚠️ 这条断言是**改掉**原来那句 `expect(...).not.toContain('999999')` 得来的：
    //    业务方 2026-10-08 推翻了「不画内部抢单」那一版口径（R1/R4），
    //    「不含 999999」现在**换了个理由**仍成立——我们这一颗的 `value` 是 `hall`
    //    （有意偏离对标的魔法值，理由见 `renderCandidates` 与 spec §5 第 4 条①）⇒ 两条都钉住。
    expect(radios.map((r) => r.value)).toEqual(['0', HALL_RADIO_VALUE, '7', '9'])
    expect(radios.map((r) => r.getAttribute('title'))).toEqual([
      '暂不指派',
      HALL_ASSIGNEE_LABEL,
      '测试昵称甲',
      '测试昵称乙',
    ])
    // ⛔⛔ 对标的魔法值 `999999` **一个都不许出现**（我们不外发 `recUser`，抄它只会误导下一个人）
    expect(radios.map((r) => r.value)).not.toContain('999999')
    // 三态互斥：没选内部抢单时，「暂不指派」是唯一带 checked 的那一颗
    const checked = radios.filter((r) => r.hasAttribute('checked')).map((r) => r.value)
    expect(checked).toEqual(['0'])
    // radio 是 layui 渲染的（`lay-skin=primary`），且挂在 `radio(userlist)` 这个 filter 上
    for (const r of radios) {
      expect(r.getAttribute('lay-filter')).toBe('userlist')
      expect(r.getAttribute('lay-skin')).toBe('primary')
    }
    // 候选人名来自 API ⇒ 走 `escapeHtml`（注入一段 HTML 看它有没有被转义）
    const injected = await mountCreate({ items: [{ ...CANDIDATES[0]!, name: '<img src=x>' }] })
    expect(injected.root.querySelector('.kszp img')).toBeNull()
    expect(
      injected.root.querySelector<HTMLInputElement>('.kszp input[value="7"]')!.getAttribute('title'),
    ).toBe('<img src=x>')
  })
})

describe('快速指派：选中一个人（对标 addorder.js L1096-1137）', () => {
  it('选中 ⇒ 立即关层 + 露出发单价 + 保存勾选框 + 按钮文案变人名 + 加 `.KSPDC` + 焦点进价框', async () => {
    const { root } = await mountCreate({ items: CANDIDATES })
    // 前置：没选人时那一格是隐藏的（对标加载时 `userListCheckValue(0, …)` 的 else 分支）
    expect(throwLayer(root).style.display).toBe('none')
    expect(root.querySelector<HTMLElement>('.saveassbtn')!.style.display).toBe('none')
    kspd(root).click()
    pick(root, '7')
    // ① 立即关层（⛔ 对标没有「确定」按钮，`layer.open` 返回的实例号直接关）
    expect(LAYUI.close).toHaveBeenCalledWith(42)
    // ② 露出「给打手发单价」与「默认指派此人」
    expect(throwLayer(root).style.display).toBe('')
    expect(root.querySelector<HTMLElement>('.saveassbtn')!.style.display).toBe('')
    // ③ 按钮文案变人名 + 选中态类
    expect(kspd(root).textContent).toBe('测试昵称甲')
    expect(kspd(root).classList.contains('KSPDC')).toBe(true)
    // ④ 填价（没有提成数据 ⇒ `0`，见 `assigneeThrowFee`）并把焦点放进价框
    expect(root.querySelector<HTMLInputElement>('#throwFee')!.value).toBe('0')
    expect(document.activeElement).toBe(root.querySelector('#throwFee'))
    // ⑤ 价格 tip（对标 L1595-1608 的实参逐字）
    expect(LAYUI.tips).toHaveBeenCalledWith(
      THROW_FEE_TIP,
      root.querySelector('#throwFee'),
      { tips: [3, '#2F4056'], time: 9000, tipsMore: false },
    )
    // ⑥ layui 外观要重渲染（`form.render()`，对标 L1152）
    expect(LAYUI.render).toHaveBeenCalled()
  })

  it('有提成数据时按该人提成自动算价（真 DOM 路径；今天后端不喂，见 api/orders.ts）', async () => {
    const { root } = await mountCreate({
      items: [
        { ...CANDIDATES[0]!, commission_mode: 20, commission_value: 90 },
        { ...CANDIDATES[1]!, commission_mode: 10, commission_value: 12.5 },
      ],
    })
    // 接单价 = 100（证据件 `#price` 的默认值是 0，这里按真实路径先填 100）
    root.querySelector<HTMLInputElement>('#price')!.value = '100'
    pick(root, '7')
    expect(root.querySelector<HTMLInputElement>('#throwFee')!.value).toBe('90.00')
    // 换一个人 ⇒ 按**新那个人**的档重算（固定金额档）
    pick(root, '9')
    expect(root.querySelector<HTMLInputElement>('#throwFee')!.value).toBe('12.5')
  })

  it('选「暂不指派」（value=0）⇒ 反向复原（隐藏那一格 / 价回 0 / 按钮回文案 / 取消勾选）', async () => {
    const { root } = await mountCreate({ items: CANDIDATES })
    pick(root, '7')
    expect(throwLayer(root).style.display).toBe('')
    defAss(root).checked = true
    pick(root, '0')
    expect(throwLayer(root).style.display).toBe('none')
    expect(root.querySelector<HTMLInputElement>('#throwFee')!.value).toBe('0')
    expect(root.querySelector<HTMLElement>('.saveassbtn')!.style.display).toBe('none')
    expect(kspd(root).textContent).toBe('暂不指派')
    expect(kspd(root).classList.contains('KSPDC')).toBe(false)
    // 对标 L1150：选「暂不指派」会把「默认指派此人」**取消勾选**
    expect(defAss(root).checked).toBe(false)
  })
})

/* ── 第三档「内部抢单」（业务方 2026-10-08 裁定 R1/R3/R4；spec §3.2）─────────────────
 *
 * 三件套之外的**新一档**：选中它 ⇒ 填发单价 ⇒ 点「录单」= **建单 + 按这个价抛进内部抢单大厅**
 * （一次请求、两件事，载荷契约 `throw_to_hall: true`，后端由同批的后端任务实现）。
 * 反面（本线推翻的那一版）：这里曾经**故意不画**这一颗，理由是「我们这条路上没有这个语义」。
 */
describe('快速指派：第三档「内部抢单」（spec §3.2 / 判据 9-14）', () => {
  it('选中 ⇒ 层被关 + 露价框 + 焦点进价框 + 新 tip + `.KSPD` 文案与类；⛔ 价框既不清零也不重算', async () => {
    const { root } = await mountCreate({
      items: [
        // 有提成数据：用来证明这一档**不会**像「选人」那条那样按提成重算
        // （`assigneeThrowFee({mode:20, value:90}, '100')` = `90.00`，见上面的算式用例）
        { ...CANDIDATES[0]!, commission_mode: 20, commission_value: 90 },
        CANDIDATES[1]!,
      ],
    })
    root.querySelector<HTMLInputElement>('#price')!.value = '100'
    root.querySelector<HTMLInputElement>('#throwFee')!.value = '88.5'
    kspd(root).click()
    pick(root, HALL_RADIO_VALUE)
    // ① 选中即关层（照既有顺序：`layer.open` 返回的实例号直接 close，⛔ 没有「确定」按钮）
    expect(LAYUI.close).toHaveBeenCalledWith(42)
    // ② 露出「发单价」那一格，焦点进价框
    expect(throwLayer(root).style.display).toBe('')
    expect(document.activeElement).toBe(root.querySelector('#throwFee'))
    // ③ ⭐ **价框保持用户填的值**：不重算（不是 `90.00`）、不清零（不是 `'0'`）
    expect(root.querySelector<HTMLInputElement>('#throwFee')!.value).toBe('88.5')
    // ④ tip 是**新**文案，逐字比对（⛔ 不是那一档「不输入则默认为0」的旧文案）
    expect(LAYUI.tips).toHaveBeenCalledWith(THROW_FEE_TIP_HALL, root.querySelector('#throwFee'), {
      tips: [3, '#2F4056'],
      time: 9000,
      tipsMore: false,
    })
    expect(THROW_FEE_TIP_HALL).not.toBe(THROW_FEE_TIP)
    // ⑤ `.KSPD` 文案 = 内部抢单 + 加 `.KSPDC`（对标 L1490）
    expect(kspd(root).textContent).toBe(HALL_ASSIGNEE_LABEL)
    expect(kspd(root).classList.contains('KSPDC')).toBe(true)
    // ⑥ layui 外观重渲染（`form.render()`）
    expect(LAYUI.render).toHaveBeenCalled()
  })

  it('选中 ⇒ 「默认指派此人」**保持显示**、勾选状态不被强行清掉（业务方 2026-10-08 裁定 R1）', async () => {
    // ⚠️ **这条用例是「改口径」**（原来是「选中内部抢单 ⇒ 取消勾选并隐藏」）：
    //    业务方 2026-10-08 明裁 R1「之前让你加到内部抢单 指派 默认指派没有加上去 这个也需要」
    //    ⇒ 对标 `addorder.js:1490` 那一支本来就只有 `$(".saveassbtn").show()`（不碰勾选状态），
    //    我们原来那版是**有意偏离**，本线推翻它。被推翻的原文归档在
    //    `docs/specs/2026-10-08-hall-throw-and-autorefresh-design.md` §5 第 4 条②的更正块里。
    const { root } = await mountCreate({
      items: CANDIDATES,
      defAssRecManUid: 7,
      defAssRecMan: '测试昵称甲',
    })
    // 前置：首屏把服务端记着的那个人带出来了，勾选框是**勾着的**、`.saveassbtn` 可见
    expect(defAss(root).checked).toBe(true)
    expect(root.querySelector<HTMLElement>('.saveassbtn')!.style.display).toBe('')
    kspd(root).click()
    pick(root, HALL_RADIO_VALUE)
    // ① **仍然可见**（推翻的那一条就是这里）
    expect(root.querySelector<HTMLElement>('.saveassbtn')!.style.display).toBe('')
    // ② **勾选状态保持原样**（服务端记着东西时首屏带出来的勾）——勾上它就是「默认抛内部抢单」
    expect(defAss(root).checked).toBe(true)

    // ③ 反面（判别力）：用户**本来没勾**时切到内部抢单档，也⛔ **不许**被强行勾上
    //    （照对标那一支只 `show()`——⛔ 不替用户做决定；「本来没勾 ⇒ 没东西可清」由纯函数那条边界钉住）
    const bare = await mountCreate({ items: CANDIDATES })
    expect(defAss(bare.root).checked).toBe(false)
    kspd(bare.root).click()
    pick(bare.root, HALL_RADIO_VALUE)
    expect(defAss(bare.root).checked).toBe(false)
    expect(bare.root.querySelector<HTMLElement>('.saveassbtn')!.style.display).toBe('')
  })

  it('⭐ 提交载荷：勾着 ⇒ `throw_to_hall === true` + `remember_assignee === \'hall\'`、⛔ 不带 `assignee`、`hall_price` = 价框值', async () => {
    const { sent, root } = await mountCreate({
      items: CANDIDATES,
      // ⚠️ 服务端**记着一个**默认接手人 + 首屏据此把勾选框勾上（`loadCandidates`）⇒ 这一档
      //    「勾着」是**真实可达**的状态（不是手搓出来的），于是提交时应当带 `'hall'`。
      defAssRecManUid: 7,
      defAssRecMan: '测试昵称甲',
    })
    kspd(root).click()
    pick(root, HALL_RADIO_VALUE)
    root.querySelector<HTMLInputElement>('#throwFee')!.value = '88.5'
    await save(root)
    const post = postCalls(sent)
    expect(post.length).toBe(1)
    expect(post[0]!.data!.throw_to_hall).toBe(true)
    // R1 是二选一：这一档**没有**指派对象
    expect(post[0]!.data!.assignee).toBeUndefined()
    expect(post[0]!.data).not.toHaveProperty('assignee')
    // ⭐ 勾着 ⇒ 记住「默认抛内部抢单」（`remember_assignee` 随载荷发）。
    expect(post[0]!.data!.remember_assignee).toBe('hall')
    // `hall_price` 仍由 `#throwFee` 那一格出（`FIELD_MAP` 机制不动）
    expect(post[0]!.data!.hall_price).toBe('88.5')
  })

  it('⭐ 内部抢单档**没勾**而服务端记着一个人 ⇒ `remember_assignee === \'N\'`（取消勾选=清除）', async () => {
    const { sent, root } = await mountCreate({
      items: CANDIDATES,
      defAssRecManUid: 7,
      defAssRecMan: '测试昵称甲',
    })
    kspd(root).click()
    pick(root, HALL_RADIO_VALUE)
    // 用户把勾**取消掉**（那颗框现在显示着，所以这是可达的交互）⇒ 提交要清除已有的偏好
    defAss(root).checked = false
    root.querySelector<HTMLInputElement>('#throwFee')!.value = '88.5'
    await save(root)
    const post = postCalls(sent)
    expect(post.length).toBe(1)
    expect(post[0]!.data!.throw_to_hall).toBe(true)
    expect(post[0]!.data!.remember_assignee).toBe('N')
    expect(post[0]!.data).not.toHaveProperty('assignee')
  })

  it('内部抢单档没勾、服务端**本来也没有**偏好 ⇒ 不发 `remember_assignee`（没有东西可清）', async () => {
    const { sent, root } = await mountCreate({ items: CANDIDATES })
    kspd(root).click()
    pick(root, HALL_RADIO_VALUE)
    root.querySelector<HTMLInputElement>('#throwFee')!.value = '88.5'
    await save(root)
    const post = postCalls(sent)
    expect(post.length).toBe(1)
    expect(post[0]!.data!.throw_to_hall).toBe(true)
    expect(post[0]!.data).not.toHaveProperty('remember_assignee')
  })

  it('⭐ 默认接手人**不在候选名单里** + 用户切到内部抢单 ⇒ 提交 `hall`（**有意**行为：勾不重算，登记在新 spec §5）', async () => {
    // ⚠️ 这一格是**有意**的，⛔ 不是缺陷（行为照对标，见下）：
    //    对标那一支切档时就是 `$(".saveassbtn").show()`、**不重算勾选**（`addorder.js:1489-1490`），
    //    而「勾着 = 把**当前选择**设成默认」正是对标这颗粒子的语义；勾选框在提交前**对用户可见**
    //    （不是静默）⇒ 行为照对标，代价（用户的旧偏好会被换成 hall）写进新 spec §5 第 9 条。
    //    本条用例的作用就是**把这一格钉住**：谁把勾改成「切档时重算」，它会红。
    const { sent, root } = await mountCreate({
      items: CANDIDATES,
      // 服务端记着的那个人**不在**候选名单里（停用 / 被 limit 截断 / 被搜索词过滤都是这一形状）
      defAssRecManUid: 404,
      defAssRecMan: '已删掉的人',
    })
    // 前置：首屏按服务端事实勾着，但没有人被选中（`loadCandidates` 那一支）
    expect(defAss(root).checked).toBe(true)
    expect(kspd(root).textContent).toBe('暂不指派')
    kspd(root).click()
    pick(root, HALL_RADIO_VALUE)
    // 切档只 `show()` ⇒ 那个勾**还在**，于是提交把它读成「记住默认抛内部抢单」
    expect(defAss(root).checked).toBe(true)
    expect(root.querySelector<HTMLElement>('.saveassbtn')!.style.display).toBe('')
    root.querySelector<HTMLInputElement>('#throwFee')!.value = '88.5'
    await save(root)
    const post = postCalls(sent)
    expect(post.length).toBe(1)
    expect(post[0]!.data!.throw_to_hall).toBe(true)
    expect(post[0]!.data!.remember_assignee).toBe('hall')
    expect(post[0]!.data).not.toHaveProperty('assignee')
  })

  it('⭐ 首屏 `defAssIsHall=true` ⇒ 自动进「内部抢单」档（文案 / 类 / 价框可见 / 勾选框勾着），且**不弹 tip、不抢焦点**', async () => {
    // 业务方 2026-10-08 裁定 R2：「勾上并提交后，下次录单**自动选中『内部抢单』档**」。
    const { sent, root } = await mountCreate({
      items: CANDIDATES,
      // 两列互斥（后端保证）⇒ hall 为真时那个人一栏必然是 0 / ''，夹具照实写
      defAssIsHall: true,
      defAssRecManUid: 0,
      defAssRecMan: '',
    })
    // ① 档位落上了：`.KSPD` 文案 = 内部抢单 + `.KSPDC`
    expect(kspd(root).textContent).toBe(HALL_ASSIGNEE_LABEL)
    expect(kspd(root).classList.contains('KSPDC')).toBe(true)
    // ② 「发单价」那一格露出来（价格**每次用户自己填** ⇒ ⛔ 不重算、⛔ 不清零）
    expect(throwLayer(root).style.display).toBe('')
    // ③ 「默认指派此人」按服务端事实勾着
    expect(defAss(root).checked).toBe(true)
    // ④ **不弹 tip、不抢焦点**（照对标 L1071 第三参 `false`，与首屏带出默认接手人同一口径）
    expect(LAYUI.tips).not.toHaveBeenCalled()
    expect(document.activeElement).not.toBe(root.querySelector('#throwFee'))
    // ⑤ 弹层名单里选中的是「内部抢单」那一颗（三档互斥，且⛔ 不是「暂不指派」那一颗）
    const checkedRadios = Array.from(
      root.querySelectorAll<HTMLInputElement>('.kszp input[name=recUser]'),
    )
      .filter((r) => r.hasAttribute('checked'))
      .map((r) => r.value)
    expect(checkedRadios).toEqual([HALL_RADIO_VALUE])
    // ⑥ 用户填价、直接录单 ⇒ 载荷就是「抛内部抢单 + 记住这个默认档位」，⛔ 不带 `assignee`
    root.querySelector<HTMLInputElement>('#throwFee')!.value = '42'
    await save(root)
    const post = postCalls(sent)
    expect(post.length).toBe(1)
    expect(post[0]!.data!.throw_to_hall).toBe(true)
    expect(post[0]!.data!.remember_assignee).toBe('hall')
    expect(post[0]!.data).not.toHaveProperty('assignee')
  })

  it('⭐ 保存成功（`\'hall\'`）后复位 ⇒ 下一单**仍在内部抢单档**，价要自己填，且⛔ 不粘 `assignee`', async () => {
    const { sent, root } = await mountCreate({
      items: CANDIDATES,
      // 服务端本来记着**一个人**：这一单在 hall 档勾着提交 ⇒ 偏好换成 hall（互斥的另一半）
      defAssRecManUid: 7,
      defAssRecMan: '测试昵称甲',
    })
    kspd(root).click()
    pick(root, HALL_RADIO_VALUE)
    root.querySelector<HTMLInputElement>('#throwFee')!.value = '88.5'
    await save(root)
    expect(postCalls(sent).length).toBe(1)
    // 复位 = 「与刷新一次页面同口径」：默认 hall ⇒ 界面就是内部抢单档
    expect(kspd(root).textContent).toBe(HALL_ASSIGNEE_LABEL)
    expect(kspd(root).classList.contains('KSPDC')).toBe(true)
    expect(throwLayer(root).style.display).toBe('')
    expect(defAss(root).checked).toBe(true)
    // ⚠️ **价格不记**（R2 明裁「价格每次用户自己填」）⇒ 复位把那一格清回证据件的默认值
    expect(root.querySelector<HTMLInputElement>('#throwFee')!.value).toBe('0')
    // ⛔ 也不再把上一位接手人粘过来（按钮文案是人名就是粘住了）
    expect(kspd(root).textContent).not.toBe('测试昵称甲')

    // 第二单：价格用户自己填 ⇒ 照发；载荷里**没有** `assignee`（标记不粘）
    root.querySelector<HTMLInputElement>('#throwFee')!.value = '66'
    await save(root)
    const post = postCalls(sent)
    expect(post.length).toBe(2)
    expect(post[1]!.data!.throw_to_hall).toBe(true)
    expect(post[1]!.data!.remember_assignee).toBe('hall')
    expect(post[1]!.data).not.toHaveProperty('assignee')
  })

  it.each(['0', '0.00', ''])('发单价 = %j ⇒ 点「录单」**一个请求都不发** + 定稿那句提示', async (fee) => {
    const { sent, root } = await mountCreate({ items: CANDIDATES })
    kspd(root).click()
    pick(root, HALL_RADIO_VALUE)
    root.querySelector<HTMLInputElement>('#throwFee')!.value = fee
    await save(root)
    // ⭐ 零请求（判据用**解析后的数值**，⛔ 不是真值判：`'0'` / `'0.00'` 都是真值，
    //    而 `''` 会解析成 `0`——E-0021 的 JS 侧同型）
    // ⚠️ 断言打在**建单那个端点**上（`postCalls` 已经按 url + method 过滤）：⛔ 不要写
    //    `expect(sent.length).toBe(0)`——挂载阶段本来就会走 `http.request`（游戏 / 名单 /
    //    登录态），那样写是一条恒假的判据。
    expect(postCalls(sent).length).toBe(0)
    expect(LAYUI.msg).toHaveBeenCalledWith(HALL_PRICE_REQUIRED_MESSAGE, { time: 1500 })
    // 拦在提交之前 ⇒ 连「结果未知」的二次确认都不该被置位（下一发仍然是干净的一次提交）
    expect(LAYUI.confirm).not.toHaveBeenCalled()
  })

  it('同一档价 > 0 ⇒ 那一发照发（对照：证明上面那条「零请求」不是恒真）', async () => {
    const { sent, root } = await mountCreate({ items: CANDIDATES })
    kspd(root).click()
    pick(root, HALL_RADIO_VALUE)
    root.querySelector<HTMLInputElement>('#throwFee')!.value = '0.01'
    await save(root)
    expect(postCalls(sent).length).toBe(1)
    expect(LAYUI.msg.mock.calls.map((c) => String(c[0]))).not.toContain(HALL_PRICE_REQUIRED_MESSAGE)
  })

  it('选中内部抢单后再点「暂不指派」/ 改选某人 ⇒ 载荷里**没有** `throw_to_hall`（标记不粘住）', async () => {
    const { sent, root } = await mountCreate({ items: CANDIDATES })
    kspd(root).click()
    pick(root, HALL_RADIO_VALUE)
    // ① 切回「暂不指派」⇒ 反向复原（价归零 / 隐藏 / 文案回「暂不指派」）
    pick(root, '0')
    expect(kspd(root).textContent).toBe('暂不指派')
    expect(kspd(root).classList.contains('KSPDC')).toBe(false)
    expect(throwLayer(root).style.display).toBe('none')
    expect(root.querySelector<HTMLInputElement>('#throwFee')!.value).toBe('0')
    await save(root)
    const afterNone = postCalls(sent)
    expect(afterNone.length).toBe(1)
    expect(afterNone[0]!.data).not.toHaveProperty('throw_to_hall')
    expect(afterNone[0]!.data).not.toHaveProperty('assignee')
  })

  it('选中内部抢单后再改选某人 ⇒ 走既有的「按提成重算」那条支，且 `throw_to_hall` 不粘住', async () => {
    const { sent, root } = await mountCreate({
      items: [{ ...CANDIDATES[0]!, commission_mode: 20, commission_value: 90 }, CANDIDATES[1]!],
    })
    root.querySelector<HTMLInputElement>('#price')!.value = '100'
    kspd(root).click()
    pick(root, HALL_RADIO_VALUE)
    // 前置：内部抢单这一档**不重算**（价框还是证据件的 `0`）
    expect(root.querySelector<HTMLInputElement>('#throwFee')!.value).toBe('0')
    pick(root, '7')
    // 改选人 ⇒ 走既有分支：按这个人的提成重算（`(90/100)*100 = 90.00`）+ 文案换人名
    expect(root.querySelector<HTMLInputElement>('#throwFee')!.value).toBe('90.00')
    expect(kspd(root).textContent).toBe('测试昵称甲')
    await save(root)
    const post = postCalls(sent)
    expect(post.length).toBe(1)
    expect(post[0]!.data).not.toHaveProperty('throw_to_hall')
    expect(post[0]!.data!.assignee).toBe(7)
  })

  it('内部抢单当前档位下的 radio 互斥：只有「内部抢单」那一颗 checked', async () => {
    const { root } = await mountCreate({ items: CANDIDATES, defAssRecManUid: 7, defAssRecMan: '测试昵称甲' })
    const radios = (): HTMLInputElement[] =>
      Array.from(root.querySelectorAll<HTMLInputElement>('.kszp input[name=recUser]'))
    // 前置：服务端带出的默认接手人那一颗是选中的（三态里的另一档）
    expect(radios().filter((r) => r.hasAttribute('checked')).map((r) => r.value)).toEqual(['7'])
    // 从「选人」档切到「内部抢单」档：标记一落，**名单重画**后只剩内部抢单那一颗 checked
    kspd(root).click()
    pick(root, HALL_RADIO_VALUE)
    root.querySelector<HTMLElement>('.resetSeachBtn')!.click()
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(radios().filter((r) => r.hasAttribute('checked')).map((r) => r.value)).toEqual([
      HALL_RADIO_VALUE,
    ])
  })

  it('⭐ 建单已成功、抛大厅没成功（后端 1023）⇒ 原样给出带单号那句 + **复位表单**（防重复建单）', async () => {
    const { sent, root } = await mountCreate({
      items: CANDIDATES,
      createResponse: {
        code: ORDER_CREATED_THROW_FAILED,
        msg: '单已建好（单号 20261008120000000001），抛入内部抢单大厅没成功：状态不允许该变更',
        data: {
          order_no: '20261008120000000001',
          reason: '状态不允许该变更',
        },
      },
    })
    kspd(root).click()
    pick(root, HALL_RADIO_VALUE)
    root.querySelector<HTMLInputElement>('#throwFee')!.value = '88.5'
    await save(root)
    const shown = LAYUI.msg.mock.calls.map((c) => String(c[0]))
    expect(shown.some((m) => m.includes('20261008120000000001'))).toBe(true)
    expect(shown.some((m) => m.includes('抛入内部抢单大厅没成功'))).toBe(true)
    // ⛔ 不许伪装成功、⛔ 不许并进通用的「保存失败」
    expect(shown).not.toContain('录单成功')
    // 单已经建好了 ⇒ **复位表单**（再点一次保存就是又建一张单）
    expect(root.querySelector<HTMLInputElement>('#project')!.value).toBe('')
    expect(postCalls(sent).length).toBe(1)
  })

  it('1022 与 1023 是两个码（登记表的自指守卫：别把两者写成一个）', () => {
    expect(ORDER_CREATED_ASSIGN_FAILED).toBe(1022)
    expect(ORDER_CREATED_THROW_FAILED).toBe(1023)
  })
})

describe('快速指派：提交那一次请求（对标 L1763-1767 + 后端 D-0179 契约）', () => {
  it('选中的人进载荷的 `assignee`，发单价进 `hall_price`（两格同列时 `#throwFee` 赢）', async () => {
    const { sent, root } = await mountCreate({ items: CANDIDATES })
    pick(root, '7')
    // 客服手填一个发单价（选中后那格是可编辑的，对标不置灰）
    root.querySelector<HTMLInputElement>('#throwFee')!.value = '88.5'
    await save(root)
    const post = postCalls(sent)
    expect(post.length).toBe(1)
    expect(post[0]!.data!.assignee).toBe(7)
    // ⭐ 业务方裁定：发单价 = 大厅价 = 指派价 ⇒ 同一个 `hall_price`
    // （`#throwInterPrice` 在证据件里恒 `0`，这条同时钉住 FIELD_MAP 的书写顺序）
    expect(post[0]!.data!.hall_price).toBe('88.5')
    // 没勾「默认指派此人」且本来就没设 ⇒ **不发** `remember_assignee`（三态里的「不动」）
    expect(post[0]!.data).not.toHaveProperty('remember_assignee')
  })

  it('默认接手人：首屏自动带出那个人，且再提交时带 `assignee` + `remember_assignee=Y`', async () => {
    const { sent, root } = await mountCreate({
      items: CANDIDATES,
      defAssRecManUid: 7,
      defAssRecMan: '测试昵称甲',
    })
    // 首屏就带出来了（业务方原话：「下次他录单自动带出那个人」）
    expect(kspd(root).textContent).toBe('测试昵称甲')
    expect(kspd(root).classList.contains('KSPDC')).toBe(true)
    expect(throwLayer(root).style.display).toBe('')
    expect(defAss(root).checked).toBe(true)
    // 默认接手人那一颗在名单里显示成选中
    expect(
      root.querySelector<HTMLInputElement>('.kszp input[name=recUser][value="7"]')!.checked,
    ).toBe(true)
    // ⚠️ 首屏带出**不抢焦点**（对标 L1071 第三参 `false`）——焦点不该在价框里
    expect(document.activeElement).not.toBe(root.querySelector('#throwFee'))
    await save(root)
    const post = postCalls(sent)
    expect(post[0]!.data!.assignee).toBe(7)
    expect(post[0]!.data!.remember_assignee).toBe('Y')
  })

  it('取消勾选并提交 ⇒ `remember_assignee=N`（照对标 tip 的「自动清除」语义）', async () => {
    const { sent, root } = await mountCreate({
      items: CANDIDATES,
      defAssRecManUid: 7,
      defAssRecMan: '测试昵称甲',
    })
    defAss(root).checked = false
    await save(root)
    const post = postCalls(sent)
    // 「取消勾选」只取消**记住这个人**：这一单照旧指派给他（人还是选中的）
    expect(post[0]!.data!.assignee).toBe(7)
    expect(post[0]!.data!.remember_assignee).toBe('N')
  })

  it('默认接手人不在候选名单里 ⇒ 不假装选中，也不悄悄清掉那个偏好', async () => {
    const { sent, root } = await mountCreate({
      items: CANDIDATES,
      defAssRecManUid: 404,
      defAssRecMan: '已删掉的人',
    })
    // 勾选框按服务端事实勾着（对标 `getAllRecUserData` 就是这么设的），但没有人被选中
    expect(defAss(root).checked).toBe(true)
    expect(kspd(root).textContent).toBe('暂不指派')
    expect(throwLayer(root).style.display).toBe('none')
    await save(root)
    const post = postCalls(sent)
    expect(post[0]!.data!).not.toHaveProperty('assignee')
    // 判据：**不发** `remember_assignee`（既不是 Y 也不是 N）⇒ 后端原样保留那个偏好
    expect(post[0]!.data).not.toHaveProperty('remember_assignee')
  })

  it('⭐ 指派失败（后端 1022）⇒ 页面原样给出带单号的提示 + **复位表单**（防重复建单）', async () => {
    const { sent, root } = await mountCreate({
      items: CANDIDATES,
      createResponse: {
        code: 1022,
        msg: '单已建好（单号 20261003120000000001），指派没成功：该单没有接单价，请联系客服',
        data: { order_no: '20261003120000000001', reason: '该单没有接单价，请联系客服', assign_code: 1000 },
      },
    })
    pick(root, '7')
    await save(root)
    // ① 提示里**同时**有单号与原因（中间态必须说清「单已经建好了」）
    const shown = LAYUI.msg.mock.calls.map((c) => String(c[0]))
    expect(shown.some((m) => m.includes('20261003120000000001'))).toBe(true)
    expect(shown.some((m) => m.includes('指派没成功'))).toBe(true)
    // ⛔ 不许伪装成功
    expect(shown).not.toContain('录单成功')
    // ② **表单已复位**：单已经建好了，用户再点一次保存就是又建一张单
    expect(root.querySelector<HTMLInputElement>('#project')!.value).toBe('')
    // ③ 只发了一发建单请求（复位没有触发第二发）
    expect(postCalls(sent).length).toBe(1)
  })

  /* ── 评审 Important-1：「单可能已经建了」（5xx / 网络异常）那一档 ───────────────────
   *
   * ⚠️ 为什么与 `1022` **分开**：`1022` 是**已知**结果（后端明说单已建好）⇒ 复位表单；
   * 5xx / 网络异常是**未知**结果（后端可能已经在库里建了单、也可能一行没写）⇒
   * ⛔ 不复位（真没建成功时不能丢掉用户的输入）、⛔ 不让用户盲目重试（下一发要先确认）。
   */

  /** 两种「结果未知」的真实形状（非信封 500 HTML / 带响应的 500）走同一条路。 */
  it.each([
    ['非信封（Django 500 HTML）⇒ NetworkError', 'non-envelope'],
    ['带响应的 HTTP 500 ⇒ HttpError(500)', 'http500'],
  ] as const)('结果未知：%s ⇒ 提示「可能已经建单」、**不复位**、再点保存先确认', async (_name, kind) => {
    const { sent, root } = await mountCreate({ items: CANDIDATES, createThrows: kind })
    await save(root)
    // ① 如实提示「结果未知」——⛔ 不说成功、⛔ 不冒充业务失败
    const shown = LAYUI.msg.mock.calls.map((c) => String(c[0]))
    expect(shown).toContain(SUBMIT_OUTCOME_UNKNOWN_MESSAGE)
    expect(shown).not.toContain('录单成功')
    // ② **不复位**：真没建成功时这一单的输入要留住（用户不必重填整页）
    expect(root.querySelector<HTMLInputElement>('#project')!.value).toBe('深渊满星')
    // ③ **再点保存不直接重发**：先弹二次确认（这就是「防重复建单」那道闸）
    expect(postCalls(sent).length).toBe(1)
    await save(root)
    expect(postCalls(sent).length).toBe(1)
    expect(LAYUI.confirm).toHaveBeenCalledTimes(1)
    expect(String(LAYUI.confirm.mock.calls[0]![0])).toBe(RESUBMIT_CONFIRM_MESSAGE)
    expect(String(LAYUI.confirm.mock.calls[0]![0])).toContain('可能已经建单')
    // ④ 用户明确「确定要再提交一次」⇒ 才真的发第二发
    expect(LAYUI.confirmYes).toBeTypeOf('function')
    LAYUI.confirmYes!()
    await new Promise((resolve) => setTimeout(resolve, 0))
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(postCalls(sent).length).toBe(2)
  })

  it('「结果未知」只认 5xx / 网络异常（业务失败与 4xx 都不算）', () => {
    // 判据本身也钉住：⛔ 别把 BizError（含 1022）或 4xx 算成「可能已经建单」
    const { BizError, HttpError, NetworkError, AuthExpiredError } = errorsModule
    expect(isUnknownSubmitOutcome(new NetworkError('网络异常'))).toBe(true)
    expect(isUnknownSubmitOutcome(new HttpError(500, '服务器错误'))).toBe(true)
    expect(isUnknownSubmitOutcome(new HttpError(403, '无权限'))).toBe(false)
    expect(isUnknownSubmitOutcome(new BizError(1022, '单已建好'))).toBe(false)
    expect(isUnknownSubmitOutcome(new AuthExpiredError('未认证'))).toBe(false)
    expect(isUnknownSubmitOutcome(new Error('别的'))).toBe(false)
  })

  it('别的业务失败（不是 1022）⇒ 只提示、**不复位**（那一单其实一张都没建出来）', async () => {
    const { root } = await mountCreate({
      items: CANDIDATES,
      createResponse: { code: 1000, msg: '你没有该游戏的权限', data: null },
    })
    await save(root)
    expect(LAYUI.msg).toHaveBeenCalledWith('你没有该游戏的权限', { time: 1500 })
    // 用户填的东西还在（否则他会以为「填了半天没了」）
    expect(root.querySelector<HTMLInputElement>('#project')!.value).toBe('深渊满星')
  })
})

describe('快速指派：搜索 / 重置 / 截断 / 失败（对标 L1521-1538 与 D-0146 的候选端点）', () => {
  it('搜索按 `key` 参数重拉；重置清空搜索词再拉一次', async () => {
    const { sent, root } = await mountCreate({ items: CANDIDATES })
    const searchInput = root.querySelector<HTMLInputElement>('#seachUserKey1')!
    searchInput.value = '昵称甲'
    root.querySelector<HTMLElement>('.seachBtn')!.click()
    await new Promise((resolve) => setTimeout(resolve, 0))
    const calls = sent.filter((c) => (c.url ?? '').includes('/assignee-options/'))
    expect(calls.length).toBe(2)
    expect(calls[0]!.params).toEqual({ limit: ASSIGNEE_OPTIONS_LIMIT })
    expect(calls[1]!.params).toEqual({ limit: ASSIGNEE_OPTIONS_LIMIT, key: '昵称甲' })
    root.querySelector<HTMLElement>('.resetSeachBtn')!.click()
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(searchInput.value).toBe('')
    const after = sent.filter((c) => (c.url ?? '').includes('/assignee-options/'))
    expect(after.length).toBe(3)
    expect(after[2]!.params).toEqual({ limit: ASSIGNEE_OPTIONS_LIMIT })
  })

  /* ── 修复轮 2026-10-08（总指挥裁决 0 / 1）：重拉名单**不许**改档位与勾选 ────────────
   *
   * 判别对象是两句话（它们互相排斥，所以必须有一条用例能把两者分开）：
   *  - 红队 H1：「重拉名单时 `toGrabHall` / `defAssBox.checked` 被重置」；
   *  - 总指挥逐行核对 `loadCandidates` 的结论：`toGrabHall` **只可能被置 `true`**（没有被置回
   *    `false` 的路径）⇒ 「档位被重置」不成立；`defAssBox.checked` **确实**会被服务端默认复原
   *    ⇒ 那一半成立、且是「用户说了不要、系统又改回去」⇒ 本线按裁决 1 把它改成**只复原一次**。
   */

  it('⭐ 重拉名单**不重置档位**（裁决 0 的反例）：选内部抢单 + 勾着 ⇒ 点「重置」⇒ 文案 / 勾选 / 载荷都不变', async () => {
    const { sent, root } = await mountCreate({
      items: CANDIDATES,
      defAssRecManUid: 7,
      defAssRecMan: '测试昵称甲',
    })
    kspd(root).click()
    pick(root, HALL_RADIO_VALUE)
    expect(kspd(root).textContent).toBe(HALL_ASSIGNEE_LABEL)
    expect(defAss(root).checked).toBe(true)

    await clickAndSettle(root.querySelector<HTMLElement>('.resetSeachBtn')!)

    // ① 档位仍是内部抢单（红队预言的「变成暂不指派」**不成立**）
    expect(kspd(root).textContent).toBe(HALL_ASSIGNEE_LABEL)
    expect(kspd(root).classList.contains('KSPDC')).toBe(true)
    // ② 勾选框仍是勾着的
    expect(defAss(root).checked).toBe(true)
    // ③ 重画后的名单里被选中的仍是「内部抢单」那一颗（三档互斥）
    expect(checkedRadioValues(root)).toEqual([HALL_RADIO_VALUE])
    // ④ 提交载荷仍是「抛内部抢单 + 记住 hall」（红队预言的「不发键」**不成立**）
    root.querySelector<HTMLInputElement>('#throwFee')!.value = '42'
    await save(root)
    const post = postCalls(sent)
    expect(post.length).toBe(1)
    expect(post[0]!.data!.throw_to_hall).toBe(true)
    expect(post[0]!.data!.remember_assignee).toBe('hall')
    expect(post[0]!.data).not.toHaveProperty('assignee')
  })

  it('⭐ 用户**主动取消勾选**后，重拉名单不许把它复原（裁决 1 的判据 1）', async () => {
    // ⚠️ 「复原」是**有条件**的（只在首屏那一次）：改回无条件（= 对标每次都复原）这条会红。
    const { sent, root } = await mountCreate({
      items: CANDIDATES,
      defAssRecManUid: 7,
      defAssRecMan: '测试昵称甲',
    })
    // 前置：首屏照服务端事实复原了（否则下面「仍是取消状态」可能只是从来就没勾上）
    expect(defAss(root).checked).toBe(true)
    // 用户明确表达了「不要」——他刚点了取消勾选
    defAss(root).checked = false

    await clickAndSettle(root.querySelector<HTMLElement>('.resetSeachBtn')!)

    // ① 勾**仍然取消着**（服务端事实不再把它勾回去）
    expect(defAss(root).checked).toBe(false)
    // ② 提交发 `'N'`（= 清除那个偏好），⛔ 不是被复原成 `'Y'`
    await save(root)
    const post = postCalls(sent)
    expect(post.length).toBe(1)
    expect(post[0]!.data!.remember_assignee).toBe('N')
    // 这一单照旧指派给他（「取消勾选」只取消**记住**，不改这一单指派给谁）
    expect(post[0]!.data!.assignee).toBe(7)
  })

  it('⭐ 首屏那一次的服务端复原**照旧**（裁决 1 的判据 2：⛔ 别把上一条读成「永不复原」）', async () => {
    // 默认 hall ⇒ 自动进内部抢单档 + 勾着
    const hall = await mountCreate({ items: CANDIDATES, defAssIsHall: true })
    expect(kspd(hall.root).textContent).toBe(HALL_ASSIGNEE_LABEL)
    expect(defAss(hall.root).checked).toBe(true)
    expect(checkedRadioValues(hall.root)).toEqual([HALL_RADIO_VALUE])
    // 默认某人 ⇒ 那个人被选中 + 勾着
    const person = await mountCreate({
      items: CANDIDATES,
      defAssRecManUid: 7,
      defAssRecMan: '测试昵称甲',
    })
    expect(kspd(person.root).textContent).toBe('测试昵称甲')
    expect(defAss(person.root).checked).toBe(true)
    expect(
      person.root.querySelector<HTMLInputElement>('.kszp input[name=recUser][value="7"]')!.checked,
    ).toBe(true)
  })

  it('⭐ 默认档位 = **hall** 时改选「暂不指派」，重拉名单**不许**把 hall 复原回去（裁决 1 真正修掉的那一格）', async () => {
    // ⚠️ 这条是裁决 1 的**钉子用例**：它钉的是「服务端默认 hall ⇒ 用户主动离开那一档 ⇒ 重拉名单
    //    不许把他拽回去」。判据的四条里随便哪一条都够红：
    //      · 去掉 `order-addorder.ts::loadCandidates` 里那句 `if (!defAssPreferenceSynced)` 的
    //        guard（= 改回「每次都按服务端事实复原」）⇒ 这一发重拉会把 `toGrabHall` 重新置真、
    //        把勾重新勾上 ⇒ ② 的 `.KSPD` 文案变成「内部抢单」、③ 的 `.saveassbtn` 又显示出来、
    //        ④ 的载荷里多出 `throw_to_hall: true` ⇒ **本用例红**；
    //      · 只把 guard 里的 `defAssBox.checked` 那一支删掉不够（这一格真正被打回的是**档位**）。
    //    ⛔ 与上一条（默认接手人 + 取消勾选）是**两格**：那条钉「勾」，这条钉**档位本身**。
    const { sent, root } = await mountCreate({
      items: CANDIDATES,
      // 服务端记着的档位就是「内部抢单」（两列互斥 ⇒ 那个人一栏是 0 / ''）
      defAssIsHall: true,
      defAssRecManUid: 0,
      defAssRecMan: '',
    })
    // 前置：首屏照服务端事实进了内部抢单档（否则下面「仍是暂不指派」可能只是从来没进过那一档）
    expect(kspd(root).textContent).toBe(HALL_ASSIGNEE_LABEL)
    expect(checkedRadioValues(root)).toEqual([HALL_RADIO_VALUE])

    // 用户打开弹层、**改选「暂不指派」**（对标 L1150 那一支：反向复原 + 取消勾选）
    kspd(root).click()
    pick(root, '0')
    expect(kspd(root).textContent).toBe('暂不指派')
    expect(root.querySelector<HTMLElement>('.saveassbtn')!.style.display).toBe('none')

    await clickAndSettle(root.querySelector<HTMLElement>('.resetSeachBtn')!)

    // ① 重画后的名单里选中的仍是「暂不指派」（⛔ 不是被打回「内部抢单」那一颗）
    expect(checkedRadioValues(root)).toEqual(['0'])
    // ② 档位仍是暂不指派：文案回去了、`.KSPDC` 摘掉、价框层收起来
    expect(kspd(root).textContent).toBe('暂不指派')
    expect(kspd(root).textContent).not.toBe(HALL_ASSIGNEE_LABEL)
    expect(kspd(root).classList.contains('KSPDC')).toBe(false)
    expect(throwLayer(root).style.display).toBe('none')
    // ③ 那一档的「默认指派此人」收起（用户没在记档位）
    expect(root.querySelector<HTMLElement>('.saveassbtn')!.style.display).toBe('none')
    // ④ 提交载荷里**没有** `throw_to_hall`（这一单不抛单）；服务端本来记着 hall 而用户取消了
    //    ⇒ 按四态发 `'N'`（清除那个偏好），⛔ 不是被复原成 `'hall'`
    await save(root)
    const post = postCalls(sent)
    expect(post.length).toBe(1)
    expect(post[0]!.data).not.toHaveProperty('throw_to_hall')
    expect(post[0]!.data).not.toHaveProperty('assignee')
    expect(post[0]!.data!.remember_assignee).toBe('N')
  })

  it('名单被截断 ⇒ 必须说出来（被截断的人在弹层里选不到）', async () => {
    await mountCreate({ items: CANDIDATES, truncated: true })
    expect(LAYUI.msg).toHaveBeenCalledWith(
      `可指派接手人超过 ${ASSIGNEE_OPTIONS_LIMIT} 位，已截断：可用搜索缩小范围`,
      { time: 1500 },
    )
  })

  it('名单拉不到 ⇒ 如实提示（⛔ 不静默降级成「一个能指派的人都没有」）', async () => {
    const { root } = await mountCreate({ optionsFail: true })
    expect(LAYUI.msg).toHaveBeenCalledWith('网络异常，请检查连接', { time: 1500 })
    // 页面照样能用（KSPD 仍在、也没被登记成「尚未开放」）
    expect(kspd(root).getAttribute('data-notopen')).toBeNull()
  })
})

describe('快速指派：编辑态不接这一支（⛔ 不留「选了人但保存后什么都没发生」的假功能）', () => {
  it('编辑态点 `.KSPD` ⇒ 提示「尚未开放」，且**不**开层', async () => {
    const { root } = await mountCreate({ orderNo: '20260921000000000001' })
    expect(kspd(root).getAttribute('data-notopen')).toBe('编辑订单时的快速指派')
    kspd(root).click()
    expect(LAYUI.open).not.toHaveBeenCalled()
    expect(LAYUI.msg).toHaveBeenCalledWith('「编辑订单时的快速指派」尚未开放', { time: 1500 })
  })
})
