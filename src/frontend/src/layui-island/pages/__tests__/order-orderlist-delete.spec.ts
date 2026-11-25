// @vitest-environment jsdom
import { renderOrderRows } from './order-list-table-fixture'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 工具栏「删除」按钮的接线（spec §7.1；§8-T9 里与删除相关的那几条）。
 *
 * 判据打在**真 `api/orders.ts` 拼出来的请求配置**上（E-0009 同族：不许断言页面自造的中间态），
 * 以及**两跳确认的逐字文案**与 `isDelEmp` 分流上。
 *
 * 挂载级替身的形状照 `order-orderlist-phase1.spec.ts`（本页的真实挂载口是
 * `mountOrderListPage(root)`；它自己 `fetch` 证据件、再塞标记）。
 */
const HERE = path.dirname(fileURLToPath(import.meta.url))
/** 证据件原文（`public/replica/order-orderlist.html`；4 层上去 = `src/frontend`）。 */
const EVIDENCE = readFileSync(
  path.resolve(HERE, '../../../../public/replica/order-orderlist.html'),
  'utf8',
)

/**
 * layui 的**可控替身**。`layer.confirm` 照真 layui 的两种调用形态分派：
 *  - `confirm(text, yes)`（第 1 跳，本页用的就是这一形态）；
 *  - `confirm(text, options, yes, btn2)`（第 2 跳，两颗按钮各走一个回调）。
 *
 * 真 layui（vendored `public/layui/layui.js`，偏移 167447）：
 * `confirm:function(e,t,i,n){var a="function"==typeof t; a&&(n=i,i=t), x.open(…) }`——
 * 所以第 2 个参数是不是函数决定 `yes` 落在第 3 个还是第 2 个参数上。
 *
 * ⚠️ 回调**必须收到自己那一层的 `index`**（真 layui 的按钮处理器传的是 `n.index`，
 * vendored `layui.js` 偏移 178582）：替身按「`confirm` 的返回值」把它送进回调，
 * 判据才能打在**真实的 `layer.close` 调用参数**上（E-0009 同族）。
 */
const LAYUI = vi.hoisted(() => {
  const confirms: Array<{ text: string; buttons?: string[] }> = []
  const msgs: string[] = []
  let rows: Array<{ order_no: string }> = []
  /** 第 2 跳点哪一颗：`false` = 左（需要删除接手人绩效 ⇒ `Y`）；`true` = 右（⇒ `N`）。 */
  let clickSecond = false
  /** ⭐ T1 的 loading 哨兵层号（既有断言里过滤它用）。 */
  const loadIndex = 9003
  return {
    confirms,
    msgs,
    loadIndex,
    setRows: (next: Array<{ order_no: string }>) => {
      rows = next
    },
    setClickSecond: (value: boolean) => {
      clickSecond = value
    },
    table: {
      render: vi.fn(),
      reload: vi.fn(),
      reloadData: vi.fn(),
      on: vi.fn(),
      cache: {},
      getOptions: vi.fn(() => ({ page: { limit: 20 } })),
      checkStatus: vi.fn(() => ({ data: rows })),
    },
    form: { render: vi.fn(), on: vi.fn(), val: vi.fn(() => ({})) },
    layer: {
      confirm: vi.fn((text: string, optionsOrYes: unknown, yes?: unknown, btn2?: unknown) => {
        const isTwoArg = typeof optionsOrYes === 'function'
        const options = isTwoArg ? undefined : (optionsOrYes as { btn?: string[]; title?: string } | undefined)
        const yesCb = (isTwoArg ? optionsOrYes : yes) as ((i: number) => void) | undefined
        const btn2Cb = (isTwoArg ? yes : btn2) as ((i: number) => void) | undefined
        confirms.push({ text, buttons: options?.btn })
        // 真 layui 把 `n.index` 当第一个实参交给回调（偏移 178582）⇒ 替身照做，
        // 且它等于本函数的返回值（真 layui 里 `n.index` 就是 `x.open()` 的返回值）。
        const index = confirms.length
        // 只有带两颗按钮的那一跳才能点「右」；第 1 跳无 `btn`，恒走 `yes`。
        if (clickSecond && options?.btn && options.title !== '批量操作') btn2Cb?.(index)
        else yesCb?.(index)
        return index
      }),
      msg: vi.fn((t: string) => {
        msgs.push(t)
      }),
      alert: vi.fn(),
      close: vi.fn(),
      closeAll: vi.fn(),
      open: vi.fn(() => 1),
      /**
       * ⭐ T1：`runBatchFlow` 现在会 `layer.load(1, { shade: false })` + `layer.close(index)`。
       * 层号改成**定值哨兵**（原来返回 1，会和两跳确认的层号 1 撞）⇒ 既有「两跳都被 close」判据
       * 只需过滤它（见 `dialogCloses()`）。
       */
      load: vi.fn(() => loadIndex),
      prompt: vi.fn(),
      tips: vi.fn(),
    },
    laydate: { render: vi.fn() },
    element: { init: vi.fn(), render: vi.fn() },
    $: (el: Element) => el,
  }
})

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return {
    ...actual,
    // ⛔ 只有 `bootstrapIsland` 换成替身：`islandMsg` / `initIslandElements` 保持真身
    //    （它们只碰替身里已有的 `layer.msg` / `element.init`）。
    bootstrapIsland: (async () => LAYUI) as unknown as typeof actual.bootstrapIsland,
  }
})

/**
 * ⚠️ **Task 4b 起工具栏「删除」走 `runBatchFlow`**，而它取 `layer` 的出口是 `@/layui/loader` 的
 * `loadLayui(['layer'])`（⛔ **不是** `bootstrapIsland`）。jsdom 里真 `loadLayui` 会去插 `<script>`
 * 且永不 resolve（页面看起来「点了没反应」）⇒ 必须指向**同一份**替身
 * （处置与 `order-orderlist-phase1.spec.ts:262` 逐字同形）。
 */
vi.mock('@/layui/loader', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/loader')>()
  return { ...actual, loadLayui: async () => LAYUI }
})

vi.mock('@/api/config', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/config')>()
  return { ...actual, fetchGames: vi.fn(async () => []), fetchServers: vi.fn(async () => []) }
})

/**
 * ⚠️ **T9 起**本页 mount 时会取候选名单（`fetchFilterOptions`）与订单来源字典
 * （`fetchOrderSources`）各一发。两个都替身化：本文件断言的是「点删除发几发 `request()`」，
 * 那两发 GET 会污染 `toHaveBeenCalledTimes` 一类的计数（**判据本身不动**）。
 * 候选名单那一面在 `order-orderlist-filters.spec.ts` 里测。
 */
vi.mock('@/api/orders', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/orders')>()
  return {
    ...actual,
    fetchFilterOptions: vi.fn(async () => ({
      takers: [],
      publishers: [],
      takers_truncated: false,
      publishers_truncated: false,
    })),
  }
})

vi.mock('@/api/orderSources', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/orderSources')>()
  return {
    ...actual,
    fetchOrderSources: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 200 })),
    // ⭐ `mount` 会多取一次分组字典（筛选栏「订单分组」的候选）
    // ⇒ 不替身化会多一发真请求（本文件多处用 `mockResolvedValueOnce` 排队响应 ⇒
    //   队首被它吃掉，批量动作读到错形状，`[orderBatch] … length` 当场红）。
    fetchOrderSourceGroups: vi.fn(async () => ({
      items: [],
      total: 0,
      page: 1,
      page_size: 200,
    })),
  }
})

/**
 * 传输层替身：`request()` 被换掉，**`batchOrderAction` 保持真身**——URL / 方法 / 载荷因此断言在
 * 真 `api/orders.ts` 的产物上（见文件顶部说明）。
 * ⚠️ Task 4b 起工具栏删单走批量（`runBatchFlow` → `POST /api/business/orders/batch/delete/`）；
 * 单笔的 `deleteOrder` 已不再被本页调用（⛔ 它不是「行内入口」，spec §D11 只管工具栏）。
 */
vi.mock('@/api/http', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/http')>()
  return { ...actual, request: vi.fn() }
})

import { request } from '@/api/http'
import {
  DELETE_CONFIRM_TEXT,
  DELETE_PERFORMANCE_BUTTONS,
  DELETE_PERFORMANCE_CONFIRM_TEXT,
  mountOrderListPage,
} from '../order-orderlist'

/**
 * ⭐ T1：只看**弹层**的 `layer.close` 调用参数（过滤掉 `runBatchFlow` 关 loading
 * 用的哨兵层号）。本文件既有判据表达的都是「两跳确认层都被 close」这个弹层语义。
 */
function dialogCloses(): number[] {
  return LAYUI.layer.close.mock.calls
    .map((call) => call[0])
    .filter((index) => index !== LAYUI.loadIndex)
}

describe('工具栏「删除」按钮', () => {
  const mockedRequest = vi.mocked(request)

  beforeEach(() => {
    LAYUI.confirms.length = 0
    LAYUI.msgs.length = 0
    LAYUI.layer.close.mockClear()
    // ⭐ T1：`layer.load` 是跨用例共享的替身，必须清（否则「只开过一次圈」会假绿）。
    LAYUI.layer.load.mockClear()
    LAYUI.setRows([])
    LAYUI.setClickSecond(false)
    mockedRequest.mockReset()
    // 默认：批量端点的 `dry_run` / `apply` 两发都照请求里的 `order_nos` 回「全合格」。
    // （Task 4b 起工具栏删单走 `runBatchFlow`；单笔端点那条链已不再被页面调用。）
    mockedRequest.mockImplementation((async (config: {
      url?: string
      data?: { dry_run?: boolean; order_nos?: string[] }
    }) => {
      if (String(config.url).includes('/batch/delete/')) {
        const orderNos = config.data?.order_nos ?? []
        return config.data?.dry_run === true
          ? { dry_run: true, eligible: orderNos, ineligible: [], applied: [], effects: [] }
          : { dry_run: false, eligible: orderNos, ineligible: [], applied: orderNos }
      }
      return {}
    }) as never)
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: unknown) => {
        if (String(url).includes('order-orderlist.html')) {
          return { ok: true, status: 200, text: async () => EVIDENCE }
        }
        throw new Error(`未预期的 fetch：${String(url)}`)
      }),
    )
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
  })

  async function mountPage(): Promise<{ root: HTMLElement; cleanup: () => void }> {
    const root = document.createElement('div')
    document.body.appendChild(root)
    const cleanup = await mountOrderListPage(root)
    // ⚠️ **抹掉 mount 期的那一发 `request()`**（本线 minimal-role-audit 起，页 mount 会取一次
    //    `/api/auth/me`）。本文件多处判「零请求 / 恰好一发删除」，不抹掉会把 me 那一发算进去。
    mockedRequest.mockClear()
    return { root, cleanup }
  }

  function clickDelete(root: HTMLElement): void {
    renderOrderRows(root, LAYUI.table.render.mock.lastCall?.[0], LAYUI.table.checkStatus().data)
      .forEach((box) => { box.checked = true })
    LAYUI.table.reload.mockClear()
    const btn = root.querySelector<HTMLElement>('button[data-tb="delOrderBtn"]')
    expect(btn, '证据件里找不到 [data-tb=delOrderBtn]').not.toBeNull()
    btn!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  }

  // ⚠️ 这里原有一个 `flush()`（`setTimeout(0)` 排空链）——Task 4b 起工具栏删单走**两发**
  //    （`runBatchFlow` 的 `dry_run` + `apply`，中间还 await 一次 `loadLayui`）⇒ 固定一次宏任务
  //    不够用，各用例改用 `vi.waitFor`（等到真实出口上的判据成立为止）⇒ 那个 helper 已无引用、删除。

  it('两跳确认的文案与两个按钮**逐字**取对标（orderlist.js:4392-4403）', () => {
    expect(DELETE_CONFIRM_TEXT).toBe('真的删除吗？警告：一旦删除，将无法找回，请谨慎处理')
    expect(DELETE_PERFORMANCE_CONFIRM_TEXT).toBe(
      '删除订单会自动删除接单客服的接单提成记录，是否需要同步删除代练接手人的绩效记录？',
    )
    expect([...DELETE_PERFORMANCE_BUTTONS]).toEqual([
      '需要删除接手人绩效',
      '不需要删除接手人绩效',
    ])
  })

  it('勾 0 笔 ⇒ 复用本页既有文案、零请求', async () => {
    const { root, cleanup } = await mountPage()
    LAYUI.setRows([])
    clickDelete(root)
    expect(LAYUI.msgs).toContain('请选中要操作的记录')
    expect(mockedRequest).not.toHaveBeenCalled()
    cleanup()
  })

  it('勾 2 笔 ⇒ 两跳之后走**批量**端点（勾 1 笔也是长度 1 的批量；spec §D11）', async () => {
    // ⚠️ 工具栏删除走**真批量**（勾 1 笔也是长度 1 的批量）；旧「一次一笔、零请求」口径
    //    不成立，判据打在 `request()` 的真出口上。
    const { root, cleanup } = await mountPage()
    const TWO = ['20260924000000000401', '20260924000000000402']
    LAYUI.setRows(TWO.map((order_no) => ({ order_no })))
    clickDelete(root)
    expect(LAYUI.confirms.map((c) => c.text)).toEqual([
      DELETE_CONFIRM_TEXT,
      DELETE_PERFORMANCE_CONFIRM_TEXT,
    ])
    await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
    expect(mockedRequest.mock.calls[0]![0]).toMatchObject({
      method: 'post',
      url: '/api/business/orders/batch/delete/',
      data: { isDelEmp: 'Y', order_nos: TWO, dry_run: true },
    })
    expect(mockedRequest.mock.calls[1]![0]).toMatchObject({
      method: 'post',
      url: '/api/business/orders/batch/delete/',
      data: { isDelEmp: 'Y', order_nos: TWO, dry_run: false },
    })
    cleanup()
  })

  it('勾 1 笔 ⇒ 两跳确认后走**批量**端点（长度 1 的批量），body 是 isDelEmp=Y', async () => {
    const { root, cleanup } = await mountPage()
    LAYUI.setRows([{ order_no: '20260924000000000401' }])
    clickDelete(root)
    expect(LAYUI.confirms.map((c) => c.text)).toEqual([
      DELETE_CONFIRM_TEXT,
      DELETE_PERFORMANCE_CONFIRM_TEXT,
    ])
    expect(LAYUI.confirms[1]!.buttons).toEqual(['需要删除接手人绩效', '不需要删除接手人绩效'])
    await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
    expect(mockedRequest.mock.calls[0]![0]).toMatchObject({
      method: 'post',
      url: '/api/business/orders/batch/delete/',
      data: { isDelEmp: 'Y', order_nos: ['20260924000000000401'], dry_run: true },
    })
    expect(mockedRequest.mock.calls[1]![0]).toMatchObject({
      method: 'post',
      url: '/api/business/orders/batch/delete/',
      data: { isDelEmp: 'Y', order_nos: ['20260924000000000401'], dry_run: false },
    })
    cleanup()
  })

  it('第 2 跳点右按钮 ⇒ body 是 isDelEmp=N（两档分流各走一边）', async () => {
    const { root, cleanup } = await mountPage()
    LAYUI.setRows([{ order_no: '20260924000000000401' }])
    LAYUI.setClickSecond(true)
    clickDelete(root)
    await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
    expect(mockedRequest.mock.calls[0]![0]).toMatchObject({
      method: 'post',
      url: '/api/business/orders/batch/delete/',
      data: { isDelEmp: 'N', order_nos: ['20260924000000000401'], dry_run: true },
    })
    expect(mockedRequest.mock.calls[1]![0]).toMatchObject({
      data: { isDelEmp: 'N', dry_run: false },
    })
    cleanup()
  })

  /**
   * 两跳确认的**关层**（文件头「已知差异 49」）：替身把真 layui 的 `n.index` 交给回调
   * ⇒ 第 1 跳 = 1、第 2 跳 = 2（与替身里 `return confirms.length` 同源）。
   * ⛔ 判据打在 `layer.close` 的**真实调用参数**上，不看页面自造的中间变量。
   */
  it('点左按钮「需要删除接手人绩效」⇒ 第 1 跳与第 2 跳的 index 都被 layer.close', async () => {
    const { root, cleanup } = await mountPage()
    LAYUI.setRows([{ order_no: '20260924000000000401' }])
    LAYUI.setClickSecond(false)
    clickDelete(root)
    await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
    expect(mockedRequest.mock.calls[0]![0]).toMatchObject({ data: { isDelEmp: 'Y' } })
    expect(dialogCloses().sort((a, b) => a - b)).toEqual([1, 2, 3])
    cleanup()
  })

  it('点右按钮「不需要删除接手人绩效」⇒ 第 1 跳与第 2 跳的 index 都被 layer.close', async () => {
    const { root, cleanup } = await mountPage()
    LAYUI.setRows([{ order_no: '20260924000000000401' }])
    LAYUI.setClickSecond(true)
    clickDelete(root)
    await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
    expect(mockedRequest.mock.calls[0]![0]).toMatchObject({ data: { isDelEmp: 'N' } })
    expect(dialogCloses().sort((a, b) => a - b)).toEqual([1, 2, 3])
    cleanup()
  })

  it('成功后提示 + 重取列表；失败提示走后端文案（errText）', async () => {
    const { root, cleanup } = await mountPage()
    LAYUI.setRows([{ order_no: '20260924000000000401' }])
    clickDelete(root)
    await vi.waitFor(() => expect(LAYUI.msgs).toContain('删除成功'))
    expect(LAYUI.table.reload).toHaveBeenCalled()

    // 失败那一支：`runBatchFlow` 的第一发（`dry_run`）抛 `Error`（真 http 层抛的是带后端 msg 的
    // BizError）⇒ 原样显示 message，**不** reload。
    LAYUI.msgs.length = 0
    LAYUI.table.reload.mockClear()
    mockedRequest.mockReset()
    mockedRequest.mockRejectedValue(new Error('删除失败：没有权限'))
    LAYUI.confirms.length = 0
    clickDelete(root)
    await vi.waitFor(() => expect(LAYUI.msgs).toContain('删除失败：没有权限'))
    expect(LAYUI.table.reload).not.toHaveBeenCalled()
    cleanup()
  })
})
