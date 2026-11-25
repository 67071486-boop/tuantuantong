/**
 * 两步式共用流程 `runBatchFlow`（spec §5.1 / §D12；Phase B · Task 3）。
 *
 * 判据一律打在**真出口**上（`AGENTS.md` §5-G，E-0009 同族）：`@/api/http` 的 `request()`
 * **发了几发、每发的 body 是什么**，弹层的**按钮文案**，以及 `onDone` 与 `msg` 的**顺序**。
 *
 * ⚠️ `@/api/orders` 的 `batchOrderAction` **保持真身**（只替 http 层）⇒ URL / 方法 / 单号形状闸
 * 都在断言范围内；夹具单号是**合成值**（E-0077），但**形状**必须过 20 位闸（spec §4.4）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

/** 合成单号（20 位纯数字 ⇒ 过 `assertOrderNo`；E-0077：夹具一律合成值）。 */
const A = '20000000000000000001'
const B = '20000000000000000002'
const C = '20000000000000000003'

/**
 * loading 层号的**定值**（⭐ T1）：替身 `layer.load` 恒返回它 ⇒ 断言可以写字面量
 * （E-0024：期望值是值、不是常量引用）。它不与任何真实 layui 层号相干，只在本文件的替身里成立。
 */
const LOAD_INDEX = 9001

/**
 * layui 的**可控替身**，形状照 `order-orderlist-delete.spec.ts` 的 `LAYUI`（本模块只用到 `layer`）。
 *
 * ⚠️ 真 layui 的 `confirm` 四参是 `(content, options, yes, btn2)`（vendored
 * `public/layui/layui.js` 的 `confirm:function(e,t,i,n){…yes:i,btn2:n…}`）——**第四参是第二颗
 * 按钮的回调、不是关层**；关层走 `options.cancel`。替身按同一形状分派，并把 `cancel` 记下来，
 * 供「关层」那条用例触发。
 *
 * ⚠️ 本模块取 `layer` 的路径是 `@/layui/loader` 的 `loadLayui(['layer'])`（**该模块没有
 * `layui` 导出**，计划里那条测试草稿的前提不成立）⇒ 这里替 `loadLayui` 返回同一个替身命名空间。
 */
const LAYUI = vi.hoisted(() => {
  const confirms: Array<{ text: string; buttons?: string[]; cancel?: () => void }> = []
  const msgs: string[] = []
  /** 每次 `layer.confirm` 开层那一刻**还开着的 loading 层号**（判据 d：不许叠层）。 */
  const loadsAtConfirm: number[][] = []
  /** 当前还开着的 loading 层号（判据 b/c/d：出口必须把它清空）。 */
  const openLoads: number[] = []
  /** 点**第二颗**按钮（`取消`）；`false` ⇒ 点第一颗。 */
  let clickSecond = false
  /** 模拟右上角 X 关层。 */
  let closeByX = false
  const layer = {
    confirm: vi.fn(
      (
        text: string,
        options: { btn?: string[]; cancel?: () => void },
        yes?: () => void,
        btn2?: () => void,
      ) => {
        confirms.push({ text, buttons: options?.btn, cancel: options?.cancel })
        loadsAtConfirm.push([...openLoads])
        if (closeByX) options?.cancel?.()
        else if (clickSecond) btn2?.()
        else yes?.()
        return confirms.length
      },
    ),
    msg: vi.fn((text: string) => {
      msgs.push(text)
    }),
    /**
     * ⭐ T1：loading。层号用**定值**（`LOAD_INDEX`）⇒ 断言是字面量、不靠 `.mock.results` 反推
     * （E-0024：期望值给字面量）。闸保证同一时刻只有一个流程在飞，所以定值不会撞。
     */
    load: vi.fn((_type?: number, _options?: unknown) => {
      openLoads.push(LOAD_INDEX)
      return LOAD_INDEX
    }),
    close: vi.fn((index: number) => {
      const at = openLoads.indexOf(index)
      if (at >= 0) openLoads.splice(at, 1)
    }),
  }
  return {
    confirms,
    msgs,
    loadsAtConfirm,
    openLoads,
    layer,
    setClickSecond: (value: boolean) => {
      clickSecond = value
    },
    setCloseByX: (value: boolean) => {
      closeByX = value
    },
    loadLayui: vi.fn(async () => ({ layer })),
  }
})

vi.mock('@/layui/loader', () => ({ loadLayui: LAYUI.loadLayui }))

/**
 * 传输层替身：`request()` 被换掉，**`batchOrderAction` 保持真身**——URL / 方法 / body
 * 因此断言在真 `api/orders.ts` 的产物上。
 */
vi.mock('@/api/http', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/http')>()
  return { ...actual, request: vi.fn() }
})

import type { AxiosRequestConfig } from 'axios'
import { request } from '@/api/http'
import { BizError } from '@/api/errors'
import type { BatchOutcome } from '@/api/orders'
import { runBatchFlow, type BatchFlowOptions } from '../orderBatch'

/** 一次批量响应（后端 `data` 的形状；spec §D2）。 */
function outcome(over: Partial<BatchOutcome> = {}): BatchOutcome {
  return { dry_run: true, eligible: [], ineligible: [], applied: [], ...over }
}

describe('runBatchFlow', () => {
  const mockedRequest = vi.mocked(request)

  beforeEach(() => {
    LAYUI.confirms.length = 0
    LAYUI.msgs.length = 0
    LAYUI.loadsAtConfirm.length = 0
    LAYUI.openLoads.length = 0
    LAYUI.layer.confirm.mockClear()
    LAYUI.layer.msg.mockClear()
    LAYUI.layer.load.mockClear()
    LAYUI.layer.close.mockClear()
    LAYUI.loadLayui.mockClear()
    LAYUI.setClickSecond(false)
    LAYUI.setCloseByX(false)
    mockedRequest.mockReset()
  })

  /**
   * 第 `n` 发 `request()` 的配置。缺了就**响亮失败**（`noUncheckedIndexedAccess` 下
   * 直接下标会报「possibly undefined」；⛔ 不用 `!` 把类型问题盖掉——那正好也会盖住
   * 「请求根本没发」这种真失败）。
   */
  function sent(n: number): AxiosRequestConfig {
    const call = mockedRequest.mock.calls[n]
    if (!call) {
      throw new Error(`第 ${n} 发请求不存在（实际发了 ${mockedRequest.mock.calls.length} 发）`)
    }
    return call[0]
  }

  /** 第 `n` 个确认层（同上）。 */
  function confirmAt(n: number): (typeof LAYUI.confirms)[number] {
    const entry = LAYUI.confirms[n]
    if (!entry) throw new Error(`第 ${n} 个确认层不存在（实际开了 ${LAYUI.confirms.length} 个）`)
    return entry
  }

  /** 只覆写关心的键，其余给稳定的默认值 ⇒ 每条用例的判据不掺别的变量。 */
  function flow(over: Partial<BatchFlowOptions> = {}): Promise<boolean> {
    return runBatchFlow({
      orderNos: [A],
      action: 'types',
      payload: { order_type: 'BOOST' },
      successMessage: (n) => `已处理 ${n} 笔订单`,
      onDone: vi.fn(),
      msg: vi.fn(),
      ...over,
    })
  }

  it('全合格 ⇒ 不加确认层、直接执行；第 2 发只含 eligible、dry_run=false', async () => {
    mockedRequest
      .mockResolvedValueOnce(outcome({ eligible: [A, B] }))
      .mockResolvedValueOnce(outcome({ dry_run: false, eligible: [A, B], applied: [A, B] }))
    const onDone = vi.fn()
    const msg = vi.fn()

    const done = await flow({ orderNos: [A, B], onDone, msg })

    expect(done).toBe(true)
    expect(mockedRequest).toHaveBeenCalledTimes(2)
    expect(sent(0)).toMatchObject({
      method: 'post',
      url: '/api/business/orders/batch/types/',
      data: { order_type: 'BOOST', order_nos: [A, B], dry_run: true },
    })
    expect(sent(1)).toMatchObject({
      method: 'post',
      url: '/api/business/orders/batch/types/',
      data: { order_type: 'BOOST', order_nos: [A, B], dry_run: false },
    })
    expect(LAYUI.layer.confirm).not.toHaveBeenCalled()
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(msg).toHaveBeenCalledWith('已处理 2 笔订单')
  })

  it('部分不合格 + 点「取消」⇒ 只有 1 发请求、零 apply、onDone 没调', async () => {
    mockedRequest.mockResolvedValueOnce(
      outcome({
        eligible: [A],
        ineligible: [{ order_no: B, code: 1014, msg: '订单已结账' }],
      }),
    )
    LAYUI.setClickSecond(true)
    const onDone = vi.fn()

    const done = await flow({ orderNos: [A, B], onDone })

    expect(done).toBe(false)
    expect(mockedRequest).toHaveBeenCalledTimes(1)
    expect(onDone).not.toHaveBeenCalled()
    expect(LAYUI.confirms).toHaveLength(1)
    expect(confirmAt(0).buttons).toEqual(['继续处理其余 1 笔', '取消'])
  })

  it('部分不合格 + 点「继续处理其余 N 笔」⇒ 第 2 发 order_nos 恰为 eligible', async () => {
    mockedRequest
      .mockResolvedValueOnce(
        outcome({
          eligible: [A, C],
          ineligible: [{ order_no: B, code: 1014, msg: '订单已结账' }],
        }),
      )
      .mockResolvedValueOnce(outcome({ dry_run: false, eligible: [A, C], applied: [A, C] }))
    const msg = vi.fn()

    const done = await flow({ orderNos: [A, B, C], msg })

    expect(done).toBe(true)
    expect(mockedRequest).toHaveBeenCalledTimes(2)
    const second = sent(1)
    expect(second.data.order_nos).toEqual([A, C])
    // ⛔ 混入不合格号会被后端**整批拒**（spec §D4 / Ruling 11）⇒ 显式钉住「不含 B」。
    expect(second.data.order_nos).not.toContain(B)
    expect(confirmAt(0).buttons).toEqual(['继续处理其余 2 笔', '取消'])
    expect(msg).toHaveBeenCalledWith('已处理 2 笔订单')
  })

  it('全不合格 ⇒ 只弹按钮为 [知道了] 的清单、零 apply、返回 false', async () => {
    mockedRequest.mockResolvedValueOnce(
      outcome({
        eligible: [],
        ineligible: [
          { order_no: A, code: 1014, msg: '订单已结账' },
          { order_no: B, code: 1002, msg: '当前状态不允许该操作' },
        ],
      }),
    )
    const onDone = vi.fn()

    const done = await flow({ orderNos: [A, B], onDone })

    expect(done).toBe(false)
    expect(mockedRequest).toHaveBeenCalledTimes(1)
    expect(onDone).not.toHaveBeenCalled()
    expect(LAYUI.confirms).toHaveLength(1)
    expect(confirmAt(0).buttons).toEqual(['知道了'])
  })

  it('成功 ⇒ 先 onDone 再 msg（顺序有实测依据：reload 里的筛选警告会被后来的 msg 覆盖）', async () => {
    mockedRequest
      .mockResolvedValueOnce(outcome({ eligible: [A] }))
      .mockResolvedValueOnce(outcome({ dry_run: false, eligible: [A], applied: [A] }))
    const order: string[] = []

    const done = await flow({
      onDone: () => order.push('reload'),
      msg: (text) => order.push(`msg:${text}`),
      successMessage: () => '状态已更新',
    })

    expect(done).toBe(true)
    expect(order).toEqual(['reload', 'msg:状态已更新'])
  })

  it('成功文案收 `applied.length`（恰好 1 笔沿用该动作原有文案）', async () => {
    mockedRequest
      .mockResolvedValueOnce(outcome({ eligible: [A, B] }))
      .mockResolvedValueOnce(outcome({ dry_run: false, eligible: [A, B], applied: [A] }))
    const seen: number[] = []

    await flow({
      orderNos: [A, B],
      successMessage: (n) => {
        seen.push(n)
        return `已处理 ${n} 笔订单`
      },
    })

    expect(seen).toEqual([1])
  })

  it('执行失败 ⇒ 只弹后端 msg 原文（含整批回滚那句）、不 onDone、返回 false', async () => {
    mockedRequest.mockResolvedValueOnce(outcome({ eligible: [A, B] }))
    mockedRequest.mockRejectedValueOnce(
      new BizError(1002, '第 2 笔：当前状态不允许该操作；本批未做任何修改'),
    )
    const onDone = vi.fn()
    const msg = vi.fn()

    const done = await flow({ orderNos: [A, B], onDone, msg })

    expect(done).toBe(false)
    expect(msg).toHaveBeenCalledTimes(1)
    expect(msg).toHaveBeenCalledWith('第 2 笔：当前状态不允许该操作；本批未做任何修改')
    expect(onDone).not.toHaveBeenCalled()
  })

  it('校验（dry_run）那一发失败 ⇒ 同样只弹后端 msg 原文、不 onDone', async () => {
    // 校验那一发也会失败（1000 入参 / 403 节点门 / 网络）⇒ 走同一支：弹后端 msg、不 onDone。
    mockedRequest.mockRejectedValueOnce(new BizError(1000, '校验失败（后端原文）'))
    const onDone = vi.fn()
    const msg = vi.fn()

    const done = await flow({ onDone, msg })

    expect(done).toBe(false)
    expect(msg).toHaveBeenCalledWith('校验失败（后端原文）')
    expect(onDone).not.toHaveBeenCalled()
  })

  it('清单每一条的单号与后端 msg 都过 escapeHtml（一行一条）', async () => {
    mockedRequest.mockResolvedValueOnce(
      outcome({
        eligible: [],
        ineligible: [
          // ⚠️ 单号来自**响应**（不是请求）⇒ 这里可以造一个带标签的值来钉转义这一条。
          { order_no: '<script>bad(1)</script>', code: 1002, msg: '<img src=x onerror="alert(1)">' },
        ],
      }),
    )

    await flow({ orderNos: [A] })

    const html = confirmAt(0).text
    expect(html).not.toContain('<script>')
    expect(html).not.toContain('<img')
    expect(html).toContain('&lt;script&gt;bad(1)&lt;/script&gt;')
    expect(html).toContain('&lt;img src=x onerror=&quot;alert(1)&quot;&gt;')
    expect(html).toContain('单号 ')
  })

  it('关层（右上角 X）⇒ 与取消同：零 apply、返回 false', async () => {
    mockedRequest.mockResolvedValueOnce(
      outcome({
        eligible: [A],
        ineligible: [{ order_no: B, code: 1014, msg: '订单已结账' }],
      }),
    )
    LAYUI.setCloseByX(true)
    const onDone = vi.fn()

    const done = await flow({ orderNos: [A, B], onDone })

    expect(done).toBe(false)
    expect(mockedRequest).toHaveBeenCalledTimes(1)
    expect(onDone).not.toHaveBeenCalled()
  })

  it('退款：applyPayload 按合格子集过滤 request_ids 后再覆盖第 2 发载荷', async () => {
    mockedRequest
      .mockResolvedValueOnce(
        outcome({
          eligible: [A, C],
          ineligible: [{ order_no: B, code: 1008, msg: '退款额超过可退上限' }],
        }),
      )
      .mockResolvedValueOnce(outcome({ dry_run: false, eligible: [A, C], applied: [A, C] }))
    const payload = { amount: '10.00', reason: '测试退款原因', request_ids: ['k1', 'k2', 'k3'] }

    const done = await flow({
      orderNos: [A, B, C],
      action: 'refunds',
      payload,
      applyPayload: (eligible) => ({
        ...payload,
        request_ids: [A, B, C]
          .map((no, i) => ({ no, id: payload.request_ids[i] }))
          .filter((item) => eligible.includes(item.no))
          .map((item) => item.id),
      }),
      successMessage: (n) => `已处理 ${n} 笔订单`,
    })

    expect(done).toBe(true)
    const second = sent(1)
    expect(second.data.request_ids).toEqual(['k1', 'k3'])
    expect(second.data.order_nos).toEqual([A, C])
    expect(second.data.amount).toBe('10.00')
    expect(second.data.reason).toBe('测试退款原因')
  })

  it('不注入 msg ⇒ 缺省用与页面同款的 islandMsg（layer.msg + time:1500）', async () => {
    mockedRequest
      .mockResolvedValueOnce(outcome({ eligible: [A] }))
      .mockResolvedValueOnce(outcome({ dry_run: false, eligible: [A], applied: [A] }))
    const options: BatchFlowOptions = {
      orderNos: [A],
      action: 'types',
      payload: { order_type: 'BOOST' },
      successMessage: () => '状态已更新',
      onDone: vi.fn(),
    }

    const done = await runBatchFlow(options)

    expect(done).toBe(true)
    expect(LAYUI.layer.msg).toHaveBeenCalledWith('状态已更新', { time: 1500 })
  })

  it('layui 加载失败（执行之前的故障）⇒ 报一句 + 返回 false，⛔ 不以 rejection 结束（评审①）', async () => {
    // 这是本函数**唯一**「拿不到 layer」的路径。若它跑在 `try` 之外，`runBatchFlow` 就会以
    // rejection 结束——同一个函数里两套失败口径（评审①）。这里钉「与两发请求同一口径」。
    LAYUI.loadLayui.mockRejectedValueOnce(new Error('layui 脚本加载失败'))
    const onDone = vi.fn()
    const msg = vi.fn()

    const done = await flow({ onDone, msg })

    expect(done).toBe(false)
    expect(msg).toHaveBeenCalledWith('layui 脚本加载失败')
    expect(onDone).not.toHaveBeenCalled()
    expect(mockedRequest).not.toHaveBeenCalled()
  })

  it('layui 加载失败且未注入 msg ⇒ 退到 console.error（页面上没有 UI 可弹，但⛔不静默）', async () => {
    LAYUI.loadLayui.mockRejectedValueOnce(new Error('layui 脚本加载失败'))
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})

    try {
      const done = await runBatchFlow({
        orderNos: [A],
        action: 'types',
        payload: { order_type: 'BOOST' },
        successMessage: () => '状态已更新',
        onDone: vi.fn(),
      })

      expect(done).toBe(false)
      expect(spy).toHaveBeenCalledWith('[orderBatch] layui 脚本加载失败')
    } finally {
      spy.mockRestore()
    }
  })

  it('onDone 同步抛（那一批已执行成功）⇒ ⛔ 不以 rejection 结束：成功提示照弹、返回 true、logError 留痕', async () => {
    mockedRequest
      .mockResolvedValueOnce(outcome({ eligible: [A] }))
      .mockResolvedValueOnce(outcome({ dry_run: false, eligible: [A], applied: [A] }))
    const msg = vi.fn()
    const boom = new TypeError("Cannot read properties of null (reading 'reload')")
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})

    try {
      const done = await flow({
        onDone: () => {
          throw boom
        },
        msg,
      })

      // 口径（本轮裁定「执行成功就该报成功」）：那一批**在后端已经落库** ⇒
      // ⛔ 不以 rejection 结束（上一轮的口径）、⛔ 不 `return false` 说「没执行」、
      // ⛔ 不把前端 TypeError 当后端 `msg` 原文弹给用户；只留一笔 `logError` 痕迹。
      expect(done).toBe(true)
      expect(msg).toHaveBeenCalledWith('已处理 1 笔订单')
      expect(spy).toHaveBeenCalledWith(`[orderBatch] ${boom.message}`)
    } finally {
      spy.mockRestore()
    }
  })

  it('successMessage 自身抛 ⇒ ⛔ 不以 rejection 逃出 Promise<boolean>：返回 true、logError 留痕、不弹空文案', async () => {
    mockedRequest
      .mockResolvedValueOnce(outcome({ eligible: [A] }))
      .mockResolvedValueOnce(outcome({ dry_run: false, eligible: [A], applied: [A] }))
    const onDone = vi.fn()
    const msg = vi.fn()
    const boom = new TypeError('文案函数炸了')
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})

    try {
      const done = await flow({
        onDone,
        msg,
        successMessage: () => {
          throw boom
        },
      })

      // 同一口径的另一档：文案函数抛不该拖垮「已执行」这个结果；⛔ 也不弹一句空话/半个串。
      expect(done).toBe(true)
      expect(onDone).toHaveBeenCalledTimes(1)
      expect(msg).not.toHaveBeenCalled()
      expect(spy).toHaveBeenCalledWith(`[orderBatch] ${boom.message}`)
    } finally {
      spy.mockRestore()
    }
  })

  /* ───────────── T1/T2（2026-10-05，规格 §4 a–g）───────────── */

  /**
   * 判据 a：**立刻**转圈，且在任何请求之前。
   *
   * ⛔ 不是「最后有个圈」——断言的是**调用顺序**（`layer.load` 的时刻早于第一次
   * `batchOrderAction`）。把 `layer.load` 挪到请求之后（或整段删掉）本条必红。
   */
  it('a. 点「确定」立刻转圈：layer.load 早于第一发 batchOrderAction', async () => {
    mockedRequest
      .mockResolvedValueOnce(outcome({ eligible: [A] }))
      .mockResolvedValueOnce(outcome({ dry_run: false, eligible: [A], applied: [A] }))

    await flow()

    // 对标形状（`.tools/recon/p2/raw/setordersta.js:74`，⛔ 不进 git）。
    expect(LAYUI.layer.load).toHaveBeenCalledWith(1, { shade: false })
    const loadOrder = LAYUI.layer.load.mock.invocationCallOrder[0]!
    const firstRequest = mockedRequest.mock.invocationCallOrder[0]!
    expect(loadOrder).toBeLessThan(firstRequest)
  })

  /** 判据 b：成功出口——loading 关、成功文案、`onDone` 调，且顺序仍是 `onDone` 先、文案后。 */
  it('b. 成功出口：loading 被关 + onDone 先于成功文案', async () => {
    mockedRequest
      .mockResolvedValueOnce(outcome({ eligible: [A] }))
      .mockResolvedValueOnce(outcome({ dry_run: false, eligible: [A], applied: [A] }))
    const order: string[] = []

    const done = await flow({
      onDone: () => order.push('onDone'),
      successMessage: () => '状态已更新',
      msg: (text) => order.push(`msg:${text}`),
    })

    expect(done).toBe(true)
    expect(LAYUI.layer.close).toHaveBeenCalledWith(LOAD_INDEX)
    expect(LAYUI.openLoads).toEqual([])
    expect(order).toEqual(['onDone', 'msg:状态已更新'])
  })

  /** 判据 c（第一档）：`dry_run` 抛——loading 关、报后端 `msg` 原文、⛔ 不 `onDone`。 */
  it('c1. dry_run 抛 ⇒ loading 被关 + 报后端 msg 原文 + 不 onDone', async () => {
    mockedRequest.mockRejectedValueOnce(new BizError(1000, '校验失败（后端原文）'))
    const onDone = vi.fn()
    const msg = vi.fn()

    const done = await flow({ onDone, msg })

    expect(done).toBe(false)
    expect(LAYUI.layer.close).toHaveBeenCalledWith(LOAD_INDEX)
    expect(LAYUI.openLoads).toEqual([])
    expect(msg).toHaveBeenCalledWith('校验失败（后端原文）')
    expect(onDone).not.toHaveBeenCalled()
  })

  /** 判据 c（第二档）：`write` 抛——同上（含整批回滚原文）。 */
  it('c2. write 抛 ⇒ loading 被关 + 报后端 msg 原文 + 不 onDone', async () => {
    mockedRequest.mockResolvedValueOnce(outcome({ eligible: [A, B] }))
    mockedRequest.mockRejectedValueOnce(
      new BizError(1002, '第 2 笔：当前状态不允许该操作；本批未做任何修改'),
    )
    const onDone = vi.fn()
    const msg = vi.fn()

    const done = await flow({ orderNos: [A, B], onDone, msg })

    expect(done).toBe(false)
    expect(LAYUI.layer.close).toHaveBeenCalledWith(LOAD_INDEX)
    expect(LAYUI.openLoads).toEqual([])
    expect(msg).toHaveBeenCalledWith('第 2 笔：当前状态不允许该操作；本批未做任何修改')
    expect(onDone).not.toHaveBeenCalled()
  })

  /** 判据 d（第一档）：全不合格 ⇒ **弹确认层时没有未关的 loading**，最终也关着。 */
  it('d1. 全不合格 ⇒ 弹清单那一刻 loading 已关（⛔ 不叠层）', async () => {
    mockedRequest.mockResolvedValueOnce(
      outcome({ eligible: [], ineligible: [{ order_no: A, code: 1014, msg: '订单已结账' }] }),
    )

    const done = await flow()

    expect(done).toBe(false)
    expect(LAYUI.confirms).toHaveLength(1)
    expect(LAYUI.loadsAtConfirm).toEqual([[]])
    expect(LAYUI.openLoads).toEqual([])
    expect(LAYUI.layer.close).toHaveBeenCalledWith(LOAD_INDEX)
  })

  /** 判据 d（第二档）：部分不合格 + 取消 / 关层 ⇒ 同上（且确认之后再没开过）。 */
  it('d2. 部分不合格 + 取消 ⇒ 弹清单那一刻 loading 已关、取消后仍关着、零 apply', async () => {
    mockedRequest.mockResolvedValueOnce(
      outcome({ eligible: [A], ineligible: [{ order_no: B, code: 1014, msg: '订单已结账' }] }),
    )
    LAYUI.setClickSecond(true)

    const done = await flow({ orderNos: [A, B] })

    expect(done).toBe(false)
    expect(mockedRequest).toHaveBeenCalledTimes(1)
    expect(LAYUI.loadsAtConfirm).toEqual([[]])
    expect(LAYUI.openLoads).toEqual([])
  })

  /** 判据 d 的**正向**半边：用户点「继续」⇒ 写请求之前**再开** loading（先关再开）。 */
  it('d3. 部分不合格 + 点「继续」⇒ 确认之后、写请求之前重新转圈', async () => {
    mockedRequest
      .mockResolvedValueOnce(
        outcome({ eligible: [A], ineligible: [{ order_no: B, code: 1014, msg: '订单已结账' }] }),
      )
      .mockResolvedValueOnce(outcome({ dry_run: false, eligible: [A], applied: [A] }))

    const done = await flow({ orderNos: [A, B] })

    expect(done).toBe(true)
    expect(LAYUI.loadsAtConfirm).toEqual([[]])
    // 开了两次（进入流程一次 + 确认之后一次）⇒ 关也有两次。
    expect(LAYUI.layer.load).toHaveBeenCalledTimes(2)
    expect(LAYUI.layer.close).toHaveBeenCalledTimes(2)
    expect(LAYUI.openLoads).toEqual([])
    // ⭐ S4（评审建议）：只断次数太弱——**第二次开圈必须早于写请求**（先关再开的顺序）。
    // 顺序虽是结构保证（`openLoading()` 就写在第二发 `batchOrderAction` 之前），但没钉住就会
    // 在「把 `openLoading()` 挪到 confirm 之前」这类改法下静默通过。
    const secondLoad = LAYUI.layer.load.mock.invocationCallOrder[1]!
    const writeRequest = mockedRequest.mock.invocationCallOrder[1]!
    expect(secondLoad).toBeLessThan(writeRequest)
  })

  /**
   * 判据 e（本批核心）：**连续两次调用** ⇒ 只发一组请求（1 dry_run + 1 write）。
   * 第二次返回 `false`、**零请求、零消息**、零确认层，且⛔ 不动第一发的 loading。
   */
  it('e. 闸：连续两次调用只发一组请求，第二次返回 false、零请求零消息', async () => {
    mockedRequest
      .mockResolvedValueOnce(outcome({ eligible: [A] }))
      .mockResolvedValueOnce(outcome({ dry_run: false, eligible: [A], applied: [A] }))
    const secondMsg = vi.fn()
    const secondDone = vi.fn()

    // 第一发**不 await**（模拟用户点了两下）；闸在第一个 await 之前就已置位。
    const first = flow()
    const second = await flow({ msg: secondMsg, onDone: secondDone })

    expect(second).toBe(false)
    expect(secondMsg).not.toHaveBeenCalled()
    expect(secondDone).not.toHaveBeenCalled()
    expect(LAYUI.layer.confirm).not.toHaveBeenCalled()
    // ⛔ 第二次不许动第一发的 loading：只开过一次。
    expect(LAYUI.layer.load).toHaveBeenCalledTimes(1)

    await expect(first).resolves.toBe(true)
    expect(mockedRequest).toHaveBeenCalledTimes(2)
    expect(sent(0).data.dry_run).toBe(true)
    expect(sent(1).data.dry_run).toBe(false)
  })

  /** 判据 f：闸**必须复位**——第一次失败之后，再点一次仍能正常发请求。 */
  it('f. 闸复位：第一次失败之后，第二次仍能发请求并成功', async () => {
    mockedRequest.mockRejectedValueOnce(new BizError(1002, '第一次失败甲'))
    const first = await flow()
    expect(first).toBe(false)

    mockedRequest
      .mockResolvedValueOnce(outcome({ eligible: [A] }))
      .mockResolvedValueOnce(outcome({ dry_run: false, eligible: [A], applied: [A] }))
    const second = await flow()

    expect(second).toBe(true)
    expect(mockedRequest).toHaveBeenCalledTimes(3)
    expect(sent(2).data.dry_run).toBe(false)
  })

  /** 判据 g：闸不吃成功——被拦下的第二次不许影响第一发的成功文案与 `onDone`。 */
  it('g. 被闸拦下的第二次不影响第一发的成功文案与 onDone', async () => {
    mockedRequest
      .mockResolvedValueOnce(outcome({ eligible: [A] }))
      .mockResolvedValueOnce(outcome({ dry_run: false, eligible: [A], applied: [A] }))
    const seen: string[] = []

    const first = flow({
      onDone: () => seen.push('onDone'),
      successMessage: () => '成功甲',
      msg: (text) => seen.push(text),
    })
    const second = await flow({ msg: vi.fn() })

    expect(second).toBe(false)
    await expect(first).resolves.toBe(true)
    expect(seen).toEqual(['onDone', '成功甲'])
  })

  /** 闸拦下的那一次也**不得**关掉第一发还在转的 loading（规格 §3-T2 的 ⚠️）。 */
  it('e2. 闸拦下的第二次⛔ 不许把第一发还在转的 loading 关掉', async () => {
    mockedRequest
      .mockResolvedValueOnce(outcome({ eligible: [A] }))
      .mockResolvedValueOnce(outcome({ dry_run: false, eligible: [A], applied: [A] }))

    const first = flow()
    // 第一发的 loading 此刻开着（它已经过了 `loadLayui` 那个 await ⇒ 这里等一个微任务）。
    await Promise.resolve()
    await Promise.resolve()
    expect(LAYUI.openLoads).toEqual([LOAD_INDEX])

    const second = await flow()
    expect(second).toBe(false)
    // ⛔ 第一次的 loading 仍开着（没有被第二次的 `finally` 关掉）。
    expect(LAYUI.openLoads).toEqual([LOAD_INDEX])

    await expect(first).resolves.toBe(true)
    expect(LAYUI.openLoads).toEqual([])
  })
})
