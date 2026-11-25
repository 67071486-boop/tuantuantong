// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  BATCH_ORDER_INVALID_TEXT,
  BATCH_ORDER_PROMPT_TITLE,
  COMBO_PRICE_INVALID_MESSAGES,
  COMBO_PRICE_LAYER_TITLE,
  COMBO_PRICE_MAIN_RANGE_IGNORED_TEXT,
  HALL_PUBLISHER_SELECT_ID,
  KEFU_SELECT_ID,
  LOSS_PRICE_RANGE_IGNORED_TEXT,
  NOT_OPEN_FILTERS,
  POPGAME_SELECT_ID,
  PUBLISHER_GAME_SWITCHED_TEXT,
  notOpenMessage,
  toOrderSearch,
  usedNotOpenFilters,
} from '../order-orderlist'
import { toOrderParams } from '@/api/orders'
import { request } from '@/api/http'

/**
 * 筛选接线（spec §5.4 / §5.6 / §7.6 / §8-T9 第 43–46 条 / §8-T11 第 70–72 条）。
 *
 * 判据：**逐条**一发，钉**参数名与值**；`0` / `false` **不许**被当空值丢掉（E-0021）。
 * ⚠️ 计划 T9 Step 1 的骨架把 `orderSoures` / `inputOrderKefu` / `popgameName` 写成**数组**，
 * 而 `RawFilterValues` 是 `Record<string, string>`、页面侧真值来自 formSelects 的**逗号串**
 * ⇒ 本文件按**字符串**写（与 `toOrderSearch` 的签名和注释口径一致）。
 *
 * ⚠️ 夹具一律**合成值**（E-0077）；单号是 20 位数字的**形状**，不是取证记录里的真单号。
 */
const HERE = path.dirname(fileURLToPath(import.meta.url))
const EVIDENCE = readFileSync(
  path.resolve(HERE, '../../../../public/replica/order-orderlist.html'),
  'utf8',
)

const ORDER_NO_A = '20260924000000000801'
const ORDER_NO_B = '20260924000000000802'

describe('筛选 → 查询参数（逐条，spec §5.4 / §7.6）', () => {
  it('订单来源 / 订单类型 / 客服 各自送自己的参数', () => {
    expect(toOrderSearch({ orderSoures: '3' }, [])).toMatchObject({ order_source: 3 })
    expect(toOrderSearch({ bussType: '1' }, [])).toMatchObject({ order_type: 'BOOST' })
    expect(toOrderSearch({ bussType: '2' }, [])).toMatchObject({ order_type: 'COMPANION' })
    // `0` 档 = 「全部」⇒ **不发** `order_type`（spec §5.4 实装 2）
    expect(toOrderSearch({ bussType: '0' }, []).order_type).toBeUndefined()
    expect(toOrderSearch({ inputOrderKefu: '7' }, [])).toMatchObject({ sales_user: 7 })
  })

  it('⭐ 订单分组（2026-10-26）：有值送数字 id；没选 / `0` / 表外值都**不发键**', () => {
    // 有值 ⇒ 送 int（后端 `IntegerField(min_value=1)`）。
    expect(toOrderSearch({ sourceGroup: '4' }, [])).toMatchObject({ source_group: 4 })
    // 没选（证据件的首项 `value=""`）⇒ 键**不出现**（⛔ 不发 `''`，后端会判 1000）。
    expect('source_group' in toOrderSearch({ sourceGroup: '' }, [])).toBe(false)
    expect('source_group' in toOrderSearch({}, [])).toBe(false)
    // `0` / 表外值不发（`positiveInt` 只认正整数）。
    expect('source_group' in toOrderSearch({ sourceGroup: '0' }, [])).toBe(false)
    expect('source_group' in toOrderSearch({ sourceGroup: 'abc' }, [])).toBe(false)
  })

  it('销售客服三档：`kefu_type` 原样送、`sales_user` 取客服那一格（多选取第一个）', () => {
    for (const tier of ['0', '5', '10'] as const) {
      expect(toOrderSearch({ kefuType: tier, inputOrderKefu: '7' }, [])).toMatchObject({
        kefu_type: tier,
        sales_user: 7,
      })
    }
    expect(toOrderSearch({ inputOrderKefu: '7,8' }, []).sales_user).toBe(7)
    // 只选档、不选人 ⇒ `kefu_type` 单独存在（后端按「单独给不产生筛选」处理，spec §5.4）
    expect(toOrderSearch({ kefuType: '5' }, [])).toMatchObject({ kefu_type: '5' })
    expect(toOrderSearch({ kefuType: '5' }, []).sales_user).toBeUndefined()
  })

  it('价格口径三档原样送；金额 0 与负数都要发出去（`0` 不是空值，E-0021）', () => {
    for (const tier of ['0', '1', '2'] as const) {
      expect(toOrderSearch({ showPriceType: tier, minPrice: '0' }, [])).toMatchObject({
        show_price_type: tier,
        price_min: '0',
      })
    }
    const params = toOrderSearch({ minPrice: '0', maxPirce: '-1' }, [])
    expect(params.price_min).toBe('0')
    expect(params.price_max).toBe('-1')
    // 「只给上界 0」也照发（对照：真值判据会把它当「没给」丢掉）
    expect(toOrderSearch({ maxPirce: '0' }, []).price_max).toBe('0')
    // ⭐ `0/1/2` 三档**不看**弹层那 4 格（口径只有档 `3` 才走它们）
    expect(toOrderSearch({ showPriceType: '1', seachMutPrice_p1: '5' }, [])).not.toHaveProperty(
      'price_min',
    )
    expect(toOrderSearch({ showPriceType: '2', seachMutPrice_p3: '5' }, [])).not.toHaveProperty(
      'hall_price_min',
    )
  })

  /**
   * ⭐ T5（2026-10-09 价格口径线）：`3 接发组合` 档的送参 = 档位 + **弹层那 4 格**
   * （接单价一对走 `price_min`/`price_max`、发单价一对走 `hall_price_min`/`hall_price_max`）。
   *
   * ⚠️ 断言用 `toEqual`（**恰好对象**，不是 `toMatchObject`）：主栏那两格同一次里被填了别的值，
   * 漏进结果就会被判红——这正是本档最容易犯的错（口径在弹层，主栏不参与）。
   */
  it('⭐ 价格口径 `3 接发组合`：送 `3` + 弹层 4 格，⛔ **不带**主栏那两格', () => {
    expect(
      toOrderSearch(
        {
          showPriceType: '3',
          minPrice: '999',
          maxPirce: '9999',
          seachMutPrice_p1: '-999999',
          seachMutPrice_p2: '99999999',
          seachMutPrice_p3: '50',
          seachMutPrice_p4: '150',
        },
        [],
      ),
    ).toEqual({
      show_price_type: '3',
      price_min: '-999999',
      price_max: '99999999',
      hall_price_min: '50',
      hall_price_max: '150',
    })
    // 弹层某格为空 ⇒ 只少那一个键（「各自单侧可缺」，计划 §2-1）
    expect(toOrderSearch({ showPriceType: '3', seachMutPrice_p3: '50' }, [])).toEqual({
      show_price_type: '3',
      hall_price_min: '50',
    })
  })

  /**
   * ⭐ T5：`4 搜亏本单` 档**只送档位本身**——⛔ 一个区间参数都不发
   * （业务方裁定这一档「不使用金额区间」；后端对「档 4 + 任一区间参数」也会响亮拒绝）。
   *
   * ⚠️ 夹具把**全部 6 个**金额键都填上（主栏 2 + 弹层 4）：只要有一个漏出去，`toEqual` 就红。
   */
  it('⭐ 价格口径 `4 搜亏本单`：**只有** `show_price_type`，⛔ 一个区间键都没有', () => {
    expect(
      toOrderSearch(
        {
          showPriceType: '4',
          minPrice: '0',
          maxPirce: '10',
          seachMutPrice_p1: '1',
          seachMutPrice_p2: '2',
          seachMutPrice_p3: '3',
          seachMutPrice_p4: '4',
        },
        [],
      ),
    ).toEqual({ show_price_type: '4' })
  })

  /**
   * ⭐ T6：两档实装后**不再**报「价格口径」；档 `3`/`4` + 主栏区间被改过 ⇒ 各报**自己那句完整的话**
   * （既不是「尚未开放」那类标签，两档的文案也**各不相同**）。
   *
   * ⚠️ **改准**（原用例断言「`3`/`4` 都报『价格口径』」）：档 `3` 现在真送参、档 `4` 也真送参
   * ⇒ 说「尚未开放」就是假话。⛔ 不是删掉那条判据：两档的「主栏区间被填了不生效」那一支
   * **仍然**必须说出来（⛔ 不许静默丢条件，E-0021 同族），拼串模板另由本文件后面那条
   * `notOpenMessage` 用例钉住。
   * ⭐ **修复轮补**：档 `3` 那一支是本轮补的（首轮只做了档 `4`，档 `3` 当时登记为残差）。
   */
  it('⭐ 价格口径 `3`/`4` 不再报「价格口径」；两档 + 主栏区间被改过 ⇒ 各报自己那句完整的话', () => {
    expect(usedNotOpenFilters({ showPriceType: '3' }, [], { showPriceType: '0' })).toEqual([])
    expect(usedNotOpenFilters({ showPriceType: '4' }, [], { showPriceType: '0' })).toEqual([])
    // 首屏默认值（`0`）不触发
    expect(usedNotOpenFilters({ showPriceType: '0' }, [], { showPriceType: '0' })).toEqual([])

    const INITIAL_RANGE = { showPriceType: '0', minPrice: '-999999', maxPirce: '99999999' }
    // 档 `4` + 下界被改过 ⇒ 那句完整的话
    expect(
      usedNotOpenFilters(
        { ...INITIAL_RANGE, showPriceType: '4', minPrice: '100' },
        [],
        INITIAL_RANGE,
      ),
    ).toEqual([LOSS_PRICE_RANGE_IGNORED_TEXT])
    // 上界那一半**单独**也要成立（只钉一侧会让另一侧漏掉）
    expect(
      usedNotOpenFilters(
        { ...INITIAL_RANGE, showPriceType: '4', maxPirce: '200' },
        [],
        INITIAL_RANGE,
      ),
    ).toEqual([LOSS_PRICE_RANGE_IGNORED_TEXT])
    // 档 `4` 且两格都**没动过** ⇒ 不报（否则每次 reload 都弹）
    expect(
      usedNotOpenFilters({ ...INITIAL_RANGE, showPriceType: '4' }, [], INITIAL_RANGE),
    ).toEqual([])

    // ⭐ 档 `3` 那一支：**同一个判据**、**另一句文案**。
    expect(
      usedNotOpenFilters(
        { ...INITIAL_RANGE, showPriceType: '3', minPrice: '100' },
        [],
        INITIAL_RANGE,
      ),
    ).toEqual([COMBO_PRICE_MAIN_RANGE_IGNORED_TEXT])
    expect(
      usedNotOpenFilters(
        { ...INITIAL_RANGE, showPriceType: '3', maxPirce: '200' },
        [],
        INITIAL_RANGE,
      ),
    ).toEqual([COMBO_PRICE_MAIN_RANGE_IGNORED_TEXT])
    // 档 `3` 且两格都没动过 ⇒ 不报（防「每次搜索都弹」）
    expect(
      usedNotOpenFilters({ ...INITIAL_RANGE, showPriceType: '3' }, [], INITIAL_RANGE),
    ).toEqual([])
    // ⛔ 两句话**不许串**：只钉「非空数组」会让「两档都推同一句」逃掉。
    expect(
      usedNotOpenFilters(
        { ...INITIAL_RANGE, showPriceType: '3', minPrice: '100' },
        [],
        INITIAL_RANGE,
      ),
    ).not.toContain(LOSS_PRICE_RANGE_IGNORED_TEXT)
    expect(
      usedNotOpenFilters(
        { ...INITIAL_RANGE, showPriceType: '4', minPrice: '100' },
        [],
        INITIAL_RANGE,
      ),
    ).not.toContain(COMBO_PRICE_MAIN_RANGE_IGNORED_TEXT)
    // 别的档（`0/1/2`）+ 主栏区间被改过 ⇒ 那两格**真送参**、不许报任何提示
    for (const tier of ['0', '1', '2'] as const) {
      expect(
        usedNotOpenFilters(
          { ...INITIAL_RANGE, showPriceType: tier, minPrice: '100' },
          [],
          INITIAL_RANGE,
        ),
      ).toEqual([])
    }
  })

  /**
   * ⭐ `notOpenMessage`：两类元素拼成**一条**提示（⛔ 绝不发第二条：后弹的会覆盖先弹的）。
   *
   * 判别力：两类用**不同模板**——「尚未开放」那类要包上模板，那两句**完整的话**要原样
   * （它们对应的档位**已经开放**，套「尚未开放」就是假话）。把两者混成一种拼法，本用例必红。
   * ⭐ **2026-10-29 多选线改准**：`usedNotOpenFilters` 现在推入的 label 是**未映射的档位名**
   * （`待完善` 这类；见 `order-orderlist.ts` 的 `splitOrderStatusSelection`），不再是
   * 「订单状态」/「订单状态（多选）」——本用例跟着用**真实会出现的** label 当语料。
   */
  it('⭐ `notOpenMessage`：尚未开放那类包模板、两句话原样、同时成立时用 `；` 连成一条', () => {
    expect(notOpenMessage(['待完善'])).toBe('「待完善」筛选尚未开放，本次已忽略')
    expect(notOpenMessage(['待完善', '异常中'])).toBe(
      '「待完善、异常中」筛选尚未开放，本次已忽略',
    )
    expect(notOpenMessage([LOSS_PRICE_RANGE_IGNORED_TEXT])).toBe(LOSS_PRICE_RANGE_IGNORED_TEXT)
    // ⭐ 第二句走**同一个**「原样」通道（⛔ 不许被包上「尚未开放」模板）
    expect(notOpenMessage([COMBO_PRICE_MAIN_RANGE_IGNORED_TEXT])).toBe(
      COMBO_PRICE_MAIN_RANGE_IGNORED_TEXT,
    )
    expect(notOpenMessage(['待完善', LOSS_PRICE_RANGE_IGNORED_TEXT])).toBe(
      `「待完善」筛选尚未开放，本次已忽略；${LOSS_PRICE_RANGE_IGNORED_TEXT}`,
    )
    expect(notOpenMessage(['待完善', COMBO_PRICE_MAIN_RANGE_IGNORED_TEXT])).toBe(
      `「待完善」筛选尚未开放，本次已忽略；${COMBO_PRICE_MAIN_RANGE_IGNORED_TEXT}`,
    )
    // ⛔ 空输入不产出任何字符（调用方用 `used.length > 0` 兜住，这里钉住拼串本身不撒谎）
    expect(notOpenMessage([])).toBe('')
  })

  /**
   * ⭐ **2026-10-29 实装多选**：「订单状态」多选拼串逐 token 反查——全映射 ⇒ 逗号串码真送参；
   * 部分映射 ⇒ 只送已映射的码；未映射档位**不进** `status`（点名提示归 `usedNotOpenFilters`，
   * 纯函数判据在 `order-orderlist.spec.ts` 的 `usedNotOpenFilters` 区块）。
   */
  it('⭐ 「订单状态」多选：全映射送逗号串码；部分映射只送已映射的；全未映射不发键', () => {
    expect(toOrderSearch({ orderSta: '未分配,待抢单' }, []).status).toBe('UNASSIGNED,WAITING_GRAB')
    expect(toOrderSearch({ orderSta: '未分配,待完善,待抢单' }, []).status).toBe(
      'UNASSIGNED,WAITING_GRAB',
    )
    // 单个已映射档位 = 多选的特例（join 后仍是那个码本身）
    expect(toOrderSearch({ orderSta: '代练中' }, []).status).toBe('IN_PROGRESS')
    // 未映射档位单选 / 混选里已映射一个都没有 ⇒ 不发键
    expect('status' in toOrderSearch({ orderSta: '异常中' }, [])).toBe(false)
    expect('status' in toOrderSearch({ orderSta: '待完善,异常中' }, [])).toBe(false)
    expect('status' in toOrderSearch({}, [])).toBe(false)
  })

  it('未安排：勾选才发 `true`；不勾 ⇒ 不发', () => {
    expect(toOrderSearch({}, ['viewLeakOrder'])).toMatchObject({ is_leak: true })
    expect(toOrderSearch({}, [])).not.toHaveProperty('is_leak')
  })

  it('批量搜单号：`batchOrderId` ⇒ `order_nos`（换行换成逗号）', () => {
    expect(
      toOrderSearch({ selectSeach1: '10', batchOrderId: ORDER_NO_A }, []),
    ).toMatchObject({ order_nos: ORDER_NO_A })
    expect(
      toOrderSearch({ selectSeach1: '10', batchOrderId: `${ORDER_NO_A}\n${ORDER_NO_B}` }, [])
        .order_nos,
    ).toBe(`${ORDER_NO_A},${ORDER_NO_B}`)
    // 没进批量输入 ⇒ 不发（`selectSeach1=0` 那档由既有 `key1` → `keyword` 覆盖）
    expect(toOrderSearch({ selectSeach1: '0' }, []).order_nos).toBeUndefined()
  })

  it('打手 / 发单人两档：只发**其中**一个（两个方向）', () => {
    const taker = toOrderSearch({ recType: '0', popgameName: '11' }, [])
    expect(taker).toMatchObject({ taker_user: 11 })
    expect(taker.hall_publisher_user).toBeUndefined()

    const publisher = toOrderSearch({ recType: '10', popgameName: '11' }, [])
    expect(publisher).toMatchObject({ hall_publisher_user: 11 })
    expect(publisher.taker_user).toBeUndefined()

    // 未选人 ⇒ 两个都不发（`recType` 自己**不产生参数**）
    const none = toOrderSearch({ recType: '0', popgameName: '' }, [])
    expect(none.taker_user).toBeUndefined()
    expect(none.hall_publisher_user).toBeUndefined()
  })

  it('⭐ 独立「发单人」下拉：送 int、空值不发、与旧控件同时有值时新下拉优先', () => {
    // 单用新下拉 ⇒ 只发 `hall_publisher_user`。
    const dedicated = toOrderSearch({ hallPublisherUser: '21' }, [])
    expect(dedicated).toMatchObject({ hall_publisher_user: 21 })
    expect(dedicated.taker_user).toBeUndefined()

    // 空值一律 `undefined`（不发键；`''` 不许变成 0 / 静默全量）。
    const empty = toOrderSearch({ hallPublisherUser: '' }, [])
    expect('hall_publisher_user' in empty).toBe(false)
    // 新下拉空着时，旧组合控件照旧工作（两向）。
    expect(toOrderSearch({ hallPublisherUser: '', recType: '0', popgameName: '11' }, []))
      .toMatchObject({ taker_user: 11 })
    expect(toOrderSearch({ hallPublisherUser: '', recType: '10', popgameName: '11' }, []))
      .toMatchObject({ hall_publisher_user: 11 })

    // 两个控件都有值（互相清空的兜底）⇒ 新下拉是权威入口，旧组合控件那份不再产生**另一个**参数。
    const both = toOrderSearch(
      { hallPublisherUser: '21', recType: '0', popgameName: '11' },
      [],
    )
    expect(both.hall_publisher_user).toBe(21)
    expect(both.taker_user).toBeUndefined()

    // 非法值（这里不是后端闸，前端也不许把它送成 0）。
    expect(toOrderSearch({ hallPublisherUser: 'abc' }, []).hall_publisher_user).toBeUndefined()
  })

  it('operator / keytid 两块已真删：证据件里没有它们，送这些值也不产生参数、不报「尚未开放」', () => {
    // 业务方裁定（原话「平台单号这个删掉不需要了 派单人也去掉吧」）⇒ 两块连同
    // 证据件里的控件**真删**（`NOT_OPEN_FILTERS` 随之为空）。
    expect(EVIDENCE).not.toContain('name="operator"')
    expect(EVIDENCE).not.toContain('name="keytid"')
    const params = toOrderSearch({ operator: '测试派单人甲', keytid: '测试平台单号甲' }, [])
    expect(params).not.toHaveProperty('hall_publisher_user')
    expect(params).not.toHaveProperty('order_nos')
    expect(usedNotOpenFilters({ operator: '测试派单人甲' }, [], { operator: '' })).toEqual([])
    expect(usedNotOpenFilters({ keytid: '测试平台单号甲' }, [], { keytid: '' })).toEqual([])
  })

  it('本批实装的控件**不再**触发「尚未开放」提示（未开放清单今天为空）', () => {
    expect(NOT_OPEN_FILTERS.map((f) => f.name)).toEqual([])
    const used = usedNotOpenFilters(
      {
        orderSoures: '3',
        recType: '0',
        popgameName: '11',
        bussType: '1',
        kefuType: '5',
        inputOrderKefu: '7',
        viewLeakOrder: 'on',
        showPriceType: '1',
        minPrice: '0',
        maxPirce: '10',
        selectSeach1: '10',
      },
      ['viewLeakOrder'],
      {},
    )
    expect(used).toEqual([])
  })
})

describe('`toOrderParams` 的边界口径（E-0021）', () => {
  it('`0` / `false` 一律保留、空值才丢', () => {
    expect(toOrderParams({ price_min: '0', price_max: '0' })).toEqual({
      price_min: '0',
      price_max: '0',
    })
    expect(toOrderParams({ is_urgent: false, is_leak: false })).toEqual({
      is_urgent: false,
      is_leak: false,
    })
    expect(toOrderParams({ price_min: '', order_type: undefined })).toEqual({})
  })
})

/**
 * 页面级：`#popgameName` 真控件 + `recType` 切档（§7.6 / §8-T11 第 70–72 条）。
 *
 * ⭐ **2026-11-09 起候选 = 远程关键词搜索、不再预载**（对标 = 全量 option + 本地搜索；
 * 我方 = 关键词远程搜索——U14 截断的延伸修正，业务方 2026-11-09 拍板，
 * 最大在役租户 729 人底集预载截断 200 搜不到人）：挂载 / 切游戏**零** `filter-options` 请求，
 * 候选全部来自输入关键词时的防抖请求。本文件钉的是页面级真出口：
 * 「不发请求」「重置为仅占位 option」「远程搜索配置注入（formselects.config）」；
 * 配置对象怎么造 / 信封怎么拆在 `order-orderlist.spec.ts` 里直调纯函数。
 */
const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  const formHandlers = new Map<string, (...args: unknown[]) => unknown>()
  return {
    msg,
    formHandlers,
    form: {
      render: vi.fn(),
      on: (e: string, cb: (...args: unknown[]) => unknown) => formHandlers.set(e, cb),
      val: vi.fn(() => ({}) as Record<string, string>),
    },
    table: {
      render: vi.fn(),
      on: vi.fn(),
      reload: vi.fn(),
      checkStatus: vi.fn(() => ({ data: [] as unknown[] })),
      getOptions: vi.fn(() => ({ page: { limit: 20 } })),
    },
    layer: { open: vi.fn(), msg, close: vi.fn(), prompt: vi.fn(), tips: vi.fn(), confirm: vi.fn() },
    laydate: { render: vi.fn() },
    $: (el: Element) => el,
    element: { init: vi.fn() },
  }
})

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return { ...actual, bootstrapIsland: (async () => LAYUI) as unknown as typeof actual.bootstrapIsland }
})

vi.mock('@/api/config', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/config')>()
  return { ...actual, fetchGames: vi.fn(async () => []), fetchServers: vi.fn(async () => []) }
})

/**
 * ⭐ MF-2（2026-10-04 修复轮）：`getFormSelects()` 的**可控桩**（下面那条「顺序」用例用）。
 *
 * 真身读 `window.layui.formSelects`，而本文件里 `bootstrapIsland` 是桩、页测试**从不**装
 * `window.layui` ⇒ 它恒 `undefined`、页面那一句 `render` 是 no-op ⇒
 * 「候选填在 `render` 之前还是之后」在 jsdom 里**看不出来**（评审实测：把 `fillKefuSelect(...)`
 * 剪到 `render(...)` 之后，8 个 spec / 440 条全绿）。这条用例把它装成桩、在 `render` 里读 DOM。
 */
const FORM_SELECTS = vi.hoisted(() => ({
  render: vi.fn(),
  on: vi.fn(),
  value: vi.fn(),
  // ⭐ 2026-11-09：页面用 `formSelects.config(id, cfg)` 注入远程搜索 ajax 配置
  //（searchName / keyVal / delay / data / beforeSuccess）。
  config: vi.fn(),
}))

// ⚠️ **传输层替身**（与 `order-orderlist.spec.ts` 同形）：`api/orders` / `api/orderSources`
// 的函数保持真身 ⇒ 「候选名单的 URL 与 `game` 参数」断言的是真实现拼出来的请求配置。
vi.mock('@/api/http', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/http')>()
  return { ...actual, request: vi.fn() }
})

const FILTER_OPTIONS_URL = '/api/business/orders/filter-options/'
const ORDER_SOURCES_URL = '/api/business/order-sources/'
/** 2026-10-04 新线：「销售客服」候选（订单管理页 `#inputOrderKefu`）的端点。 */
const KEFU_OPTIONS_URL = '/api/business/orders/kefu-options/'

const mockedRequest = vi.mocked(request)

describe('页面级：候选改远程搜索（零预载 / 切档切游戏重置 / `#inputOrderKefu` 客服候选）（§7.6 / §8-T11 70–72 / 2026-11-09）', () => {
  /** ⭐ 2026-10-04：「销售客服」候选那一发的入参记录（证明**候选源真的被调过**）。 */
  let kefuSeen: Record<string, unknown>[] = []
  let kefuResult: { items: { id: number; name: string }[]; truncated: boolean }
  let kefuFails: boolean

  /** 合成夹具：**值 = 主键、文本 = 昵称**（昵称与主键不同形 ⇒ 判据判得动）。 */
  const KEFU_ITEMS = [
    { id: 31, name: '测试昵称甲' },
    { id: 32, name: '测试昵称乙' },
  ]

  beforeEach(() => {
    kefuSeen = []
    kefuResult = { items: KEFU_ITEMS, truncated: false }
    kefuFails = false
    LAYUI.msg.mockClear()
    LAYUI.layer.prompt.mockClear()
    LAYUI.table.reload.mockClear()
    LAYUI.formHandlers.clear()
    LAYUI.form.val.mockReturnValue({})
    FORM_SELECTS.render.mockReset()
    FORM_SELECTS.on.mockReset()
    FORM_SELECTS.value.mockReset()
    FORM_SELECTS.config.mockReset()
    mockedRequest.mockReset()
    // ⚠️ `filter-options` **不再**有分支：页面一旦（回归）发出这一发，会撞进「未预期的
    //    request」当场红——「挂载零候选请求」由传输层替身自己兜底。
    mockedRequest.mockImplementation((async (config: {
      url?: string
      params?: Record<string, unknown>
    }) => {
      if (config.url === KEFU_OPTIONS_URL) {
        kefuSeen.push(config.params ?? {})
        if (kefuFails) throw new Error('候选取不到（用例构造）')
        return kefuResult
      }
      if (config.url === ORDER_SOURCES_URL) {
        return { items: [], total: 0, page: 1, page_size: 200 }
      }
      throw new Error(`未预期的 request：${String(config.url)}`)
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
    const { mountOrderListPage } = await import('../order-orderlist')
    const cleanup = await mountOrderListPage(root)
    return { root, cleanup }
  }

  function options(root: HTMLElement): string[][] {
    return Array.from(root.querySelectorAll<HTMLOptionElement>('#popgameName option')).map((o) => [
      o.value,
      o.textContent ?? '',
    ])
  }

  /** 「销售客服」那一格的 option（`[value, text]`），含证据件里那条占位。 */
  function kefuOptions(root: HTMLElement): string[][] {
    return Array.from(
      root.querySelectorAll<HTMLOptionElement>('#inputOrderKefu option'),
    ).map((o) => [o.value, o.textContent ?? ''])
  }

  /** 独立「发单人」下拉的 option（`[value, text]`），含占位。 */
  function hallPublisherOptions(root: HTMLElement): string[][] {
    return Array.from(
      root.querySelectorAll<HTMLOptionElement>(`#${HALL_PUBLISHER_SELECT_ID} option`),
    ).map((o) => [o.value, o.textContent ?? ''])
  }

  function lastWhere(): Record<string, unknown> {
    const calls = LAYUI.table.reload.mock.calls
    const call = calls[calls.length - 1]
    expect(call?.[0]).toBe('datatable')
    return (call?.[1] as { where: Record<string, unknown> } | undefined)?.where ?? {}
  }

  function clickSearch(root: HTMLElement): void {
    const btn = root.querySelector<HTMLElement>('[data-tb="seachBtnOk"]')
    expect(btn, '搜索按钮应当在证据件里').not.toBeNull()
    btn!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  }

  it('mount 后 `#popgameName` 是**真 `<select>`**（不是占位 div、没有 `data-notopen`），且挂载**零**候选请求（2026-11-09 去预选）', async () => {
    const { root, cleanup } = await mountPage()
    const node = root.querySelector('#popgameName')
    expect(node).not.toBeNull()
    expect(node!.tagName).toBe('SELECT')
    expect(node!.getAttribute('name')).toBe('popgameName')
    expect(node!.getAttribute('data-notopen')).toBeNull()
    expect(node!.querySelector('xm-select')).toBeNull()
    // ⭐ 远程搜索的结构性钉：`xm-select-search` 的属性**值** = 端点 URL
    //（空值 = 只在已加载 option 里本地过滤，那是改造前的形态；属性值就是 formselects 的 searchUrl）。
    expect(node!.getAttribute('xm-select-search')).toBe(FILTER_OPTIONS_URL)
    // 挂载**不再**预载候选：下拉只有占位 option，且传输层一次 `filter-options` 都没发过
    //（若页面回归成预载，那一发会撞进替身的「未预期的 request」当场红）。
    expect(options(root)).toEqual([['', '请选择']])
    expect(
      mockedRequest.mock.calls.filter((c) => (c[0] as { url?: string }).url === FILTER_OPTIONS_URL),
    ).toHaveLength(0)
    cleanup()
  })

  /* ── ⭐ 2026-10-04 新线：「销售客服」那一格（`#inputOrderKefu`）的候选 ──────────────
   * 业务方原话「管理订单页面的销售客服下拉都是空的」。判据（spec §3-3）：
   * ① 挂载后 option 数 **> 1**（首项是占位）；
   * ② 某项 `value` = **id**、`textContent` = **昵称**；
   * ③ 选它 + 点搜索 ⇒ 请求里 `sales_user=<那个 id>`（**整链**：从页面 DOM 到请求载荷）。
   * ⚠️ 判据必须**先证明候选源被调过**（否则「页面本来就没填」与「填了空的」同形）。 */

  it('⭐ 挂载后 `#inputOrderKefu` 有候选：值 = id、文本 = 昵称，且**候选源真的被调过**', async () => {
    const { root, cleanup } = await mountPage()
    await vi.waitFor(() =>
      expect(kefuOptions(root)).toEqual([
        // 首项是证据件里那条占位（formselects 折叠态显示它的文案）。
        ['', '选择或输入'],
        ['31', '测试昵称甲'],
        ['32', '测试昵称乙'],
      ]),
    )
    // ⚠️ **承重判据**：「没填」与「填了空」同形 ⇒ 必须证明这一发真的发过、且入参是「不带 limit」。
    await vi.waitFor(() => expect(kefuSeen).toEqual([{}]))
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: KEFU_OPTIONS_URL,
      params: {},
    })
    cleanup()
  })

  it('⭐ 候选必须在 `initFormSelects`/`render` **之前**就填好（把「顺序」变成 DOM 上可失败的判据）', async () => {
    // 评审实测的漏洞：jsdom 里 `getFormSelects()` 恒 `undefined`、页面那一句 `render` 是 no-op
    // ⇒ 把 `fillKefuSelect(...)` 剪到 `render(...)` 之后（真浏览器里下拉**仍然是空的** =
    // 业务方报的 bug 原地复发）也全绿。这里给 `window.layui.formSelects` 装**可控桩**，
    // 在 `render` 的实现里读一次那一格的 option 数：formselects 是在 `render()` 那一刻读
    // `<select>` 的 option 建 `<dl>` 的（`public/vendor/formselects-v4.js:706-745` 的
    // `renderSelect` ⇒ `$(select).find('*').each(… option …)`）⇒
    // 「候选已经在」必须对**每一次** render 都成立，否则真浏览器里下拉就是空的。
    const countsAtRender: number[] = []
    FORM_SELECTS.render.mockImplementation((id?: unknown) => {
      if (id === KEFU_SELECT_ID) {
        countsAtRender.push(document.querySelectorAll('#inputOrderKefu option').length)
      }
      return undefined
    })
    // 真身 `getFormSelects()` 读的就是 `window.layui.formSelects`（`@/layui/vendor`）。
    vi.stubGlobal('layui', { formSelects: FORM_SELECTS })
    const { cleanup } = await mountPage()
    // 至少一次（`initFormSelects('select[xm-select]')` 就会渲染这一格）——否则本判据无从成立。
    expect(
      countsAtRender.length,
      '那一格一次都没被 render 过：候选填充与它断了链',
    ).toBeGreaterThan(0)
    // 1 条占位 + 2 条夹具候选 = 3；任何一次 render 只看到 1 条
    // ⇒ `fillKefuSelect(...)` 被挪到了 `render(...)` **之后**（下拉恒空的原来那条路）。
    expect(countsAtRender.filter((n) => n !== 3)).toEqual([])
    cleanup()
  })

  it('⭐ 选一个客服 + 点搜索 ⇒ `where` 里带上那个 **id**（整链，从 DOM 到请求载荷）', async () => {
    const { root, cleanup } = await mountPage()
    await vi.waitFor(() => expect(kefuOptions(root)).toHaveLength(3))
    // 值取**页面 DOM 里真实那个 option 的值**（⛔ 不手写 '31'，否则判据与实现无关）。
    const chosen = kefuOptions(root)[1]!
    const chosenValue = chosen[0]!
    LAYUI.form.val.mockReturnValue({ inputOrderKefu: chosenValue })
    clickSearch(root)
    expect(lastWhere()).toMatchObject({ sales_user: 31 })
    // `sales_user` 送的就该是这个 option 的值（它是用户主键）。
    expect(Number(chosenValue)).toBe(31)
    cleanup()
  })

  it('⭐ 空关键词闸必须在两个下拉 **render 之前**预埋（2026-11-09 存量缺陷修正：vendor 判闸在 render 那一刻、之后的 setTimeout 不再回头判）', async () => {
    // 真缺陷（线上「空关键词请求漏过闸」的根因，读 vendored 源码定案）：
    // formselects 对设了 searchUrl 的控件在 **render 那一刻**就同步判一次 `beforeSearch`
    //（formselects-v4.js:313-322 triggerSearch → search() :358 判闸；空关键词请求只是被排进
    // `:363-368` 的 setTimeout，**回调里不再回头判闸**）⇒ 完整配置若等到挂载收尾才 `config()` 并入
    // `ajaxs[id]`，首发空关键词请求抢在闸之前发出去。修法 = 挂载早期先预埋 `{beforeSearch}`。
    // 本条把「先后」钉在桩的**调用序**上（jsdom 练不到 vendor 本体，行号就是那半边的证据）：
    // 预埋那发 `config(id, {beforeSearch})` 必须早于该 id 的第一次 `render(id)`。
    const events: string[] = []
    FORM_SELECTS.config.mockImplementation((id: unknown, cfg: Record<string, unknown>) => {
      if (
        (id === POPGAME_SELECT_ID || id === HALL_PUBLISHER_SELECT_ID) &&
        typeof cfg.beforeSearch === 'function' &&
        Object.keys(cfg).length === 1
      ) {
        events.push(`gate:${String(id)}`)
      }
      return undefined
    })
    FORM_SELECTS.render.mockImplementation((id?: unknown) => {
      if (id === POPGAME_SELECT_ID || id === HALL_PUBLISHER_SELECT_ID) {
        events.push(`render:${String(id)}`)
      }
      return undefined
    })
    vi.stubGlobal('layui', { formSelects: FORM_SELECTS })
    const { cleanup } = await mountPage()
    // 两个闸都预埋过、两个控件都被 render 过（缺任何一半本条无从成立）。
    expect(events.filter((e) => e.startsWith('gate:'))).toEqual([
      `gate:${POPGAME_SELECT_ID}`,
      `gate:${HALL_PUBLISHER_SELECT_ID}`,
    ])
    expect(events.filter((e) => e.startsWith('render:'))).toEqual(
      expect.arrayContaining([`render:${POPGAME_SELECT_ID}`, `render:${HALL_PUBLISHER_SELECT_ID}`]),
    )
    // 每个控件：预埋必须严格早于它的第一次 render（退化成「收尾才配闸」的实现此处即红）。
    expect(events.indexOf(`gate:${POPGAME_SELECT_ID}`)).toBeLessThan(
      events.indexOf(`render:${POPGAME_SELECT_ID}`),
    )
    expect(events.indexOf(`gate:${HALL_PUBLISHER_SELECT_ID}`)).toBeLessThan(
      events.indexOf(`render:${HALL_PUBLISHER_SELECT_ID}`),
    )
    // 预埋进去的就是那道真闸：空 / 空白 ⇒ false（不发），非空 ⇒ true。
    const primed = FORM_SELECTS.config.mock.calls.find(
      ([id, cfg]) =>
        id === POPGAME_SELECT_ID &&
        typeof (cfg as Record<string, unknown>).beforeSearch === 'function' &&
        Object.keys(cfg as Record<string, unknown>).length === 1,
    )
    expect(primed, '预埋那发 config 调用应当存在').toBeDefined()
    const gate = (primed![1] as { beforeSearch: (id: unknown, url: unknown, kw: unknown) => boolean })
      .beforeSearch
    expect(gate(POPGAME_SELECT_ID, FILTER_OPTIONS_URL, '')).toBe(false)
    expect(gate(POPGAME_SELECT_ID, FILTER_OPTIONS_URL, '  ')).toBe(false)
    expect(gate(POPGAME_SELECT_ID, FILTER_OPTIONS_URL, 'sq')).toBe(true)
    cleanup()
  })

  it('客服候选被截断（`truncated`）⇒ **不弹提示**（业务方 2026-10-29 拍板「不做提示，有搜索功能就行了」）；页面照常可用', async () => {
    kefuResult = { items: [KEFU_ITEMS[0]!], truncated: true }
    const { root, cleanup } = await mountPage()
    // 页面照常可用：那一格有占位 + 那一条候选（`mountPage` 会 `await` 这一发 ⇒ 已 settle）。
    await vi.waitFor(() =>
      expect(kefuOptions(root)).toEqual([
        ['', '选择或输入'],
        ['31', '测试昵称甲'],
      ]),
    )
    expect(kefuSeen).toEqual([{}])
    // 反向钉打在**真出口**上（`layer.msg`）：候选源真的发过、option 也渲染出来了，但
    // `layer.msg` 一次都没被调用（2026-10-29 前这里会弹 `KEFU_OPTIONS_TRUNCATED_TEXT`）。
    expect(LAYUI.msg).not.toHaveBeenCalled()
    cleanup()
  })

  it('候选取不到 ⇒ 保持空、页面照常、**不弹警告**（但请求确实发过——不是「压根没调」）', async () => {
    kefuFails = true
    const { root, cleanup } = await mountPage()
    // 先证明这一发发过（否定性判据的前提，E-0004 同族）：`mountPage` 会 `await` 这一发，
    // 所以此刻它一定已经 settle（失败被 `.catch` 收住）。
    await vi.waitFor(() => expect(kefuSeen).toEqual([{}]))
    // 只剩那条占位；`layer.msg` 一次都没被调用（没有「尚未开放」/错误提示）。
    expect(kefuOptions(root)).toEqual([['', '选择或输入']])
    expect(LAYUI.msg).not.toHaveBeenCalled()
    cleanup()
  })

  it('切 `recType` ⇒ `#popgameName` 重置为**仅占位 option**（候选不再预载，选人走关键词远程搜索）', async () => {
    const { root, cleanup } = await mountPage()
    expect(options(root)).toEqual([['', '请选择']])
    // 判别力来源：先塞一条「上一次远程搜索留下的残留候选」，切档后必须被清掉
    //（退化成「什么都不做」的实现会让本条红——起始态与结束态会差一条残留 option）。
    const select = root.querySelector<HTMLSelectElement>(`#${POPGAME_SELECT_ID}`)
    expect(select).not.toBeNull()
    const stale = document.createElement('option')
    stale.value = '999'
    stale.textContent = '测试残留候选'
    select!.appendChild(stale)
    LAYUI.form.val.mockReturnValue({ recType: '10' })
    ;(LAYUI.formHandlers.get('select(recType)') as (d: { value: string }) => void)({
      value: '10',
    })
    expect(options(root)).toEqual([['', '请选择']])
    cleanup()
  })

  it('⭐ 独立「发单人」下拉：运行期注入在旧组合控件之后，`xm-select-search` 值 = 同一端点、挂载零候选请求', async () => {
    const { root, cleanup } = await mountPage()
    // 去预选：下拉只有占位 option（候选 = 输入关键词时远程搜）。
    expect(hallPublisherOptions(root)).toEqual([['', '请选择']])
    const select = root.querySelector<HTMLSelectElement>(`#${HALL_PUBLISHER_SELECT_ID}`)
    expect(select, '新下拉应当被运行期注入').not.toBeNull()
    expect(select!.tagName).toBe('SELECT')
    // 样式口径照同排 `xm-select`（单选 + 远程关键词搜索）。
    expect(select!.getAttribute('name')).toBe(HALL_PUBLISHER_SELECT_ID)
    expect(select!.getAttribute('xm-select')).toBe(HALL_PUBLISHER_SELECT_ID)
    expect(select!.getAttribute('xm-select-radio')).not.toBeNull()
    expect(select!.getAttribute('xm-select-search')).toBe(FILTER_OPTIONS_URL)
    // 位置：紧跟在旧「打手 / 发单人」组合控件那个 `.layui-inline` 之后。
    const oldInline = root.querySelector('#popgameName')!.closest('.layui-inline')
    const newInline = select!.closest('.layui-inline')
    expect(oldInline).not.toBeNull()
    expect(oldInline!.nextElementSibling).toBe(newInline)
    // 承重判据（2026-11-09 去预选）：挂载**零** `filter-options` 请求（回归成预载会让本条红）。
    expect(
      mockedRequest.mock.calls.filter((c) => (c[0] as { url?: string }).url === FILTER_OPTIONS_URL),
    ).toHaveLength(0)
    cleanup()
  })

  it('⭐ 选独立「发单人」下拉 + 点搜索 ⇒ `where` 里带那个 id；⛔ 不夹带 `taker_user`', async () => {
    const { root, cleanup } = await mountPage()
    // ⚠️ 值链与候选无关（formselects 把选中 id 写进隐藏 input，`collectSearch` 只读表单值）：
    //    远程搜索模式下 jsdom 里没有候选 option，本条钉的是「有值就送对参数」这半条链。
    LAYUI.form.val.mockReturnValue({ hallPublisherUser: '21' })
    clickSearch(root)
    expect(lastWhere()).toMatchObject({ hall_publisher_user: 21 })
    expect('taker_user' in lastWhere()).toBe(false)
    cleanup()
  })

  it('⭐ 新旧两个控件**互相清空**（谁改谁生效）：新下拉清旧组合控件，旧控件清新下拉', async () => {
    const handlers = new Map<string, (...args: unknown[]) => unknown>()
    FORM_SELECTS.on.mockImplementation((id: string, cb: (...args: unknown[]) => unknown) => {
      handlers.set(id, cb)
      return undefined
    })
    FORM_SELECTS.value.mockReturnValue(undefined)
    vi.stubGlobal('layui', { formSelects: FORM_SELECTS })
    const { root, cleanup } = await mountPage()
    // 挂载时必须把两个控件都挂上 formselects 事件（旧控件 = `#popgameName`）。
    expect([...handlers.keys()].sort()).toEqual(
      [HALL_PUBLISHER_SELECT_ID, 'popgameName'].sort(),
    )
    const hall = root.querySelector<HTMLSelectElement>(`#${HALL_PUBLISHER_SELECT_ID}`)!
    const legacy = root.querySelector<HTMLSelectElement>('#popgameName')!
    // 去预选后挂载无异步候选工作；下拉只有占位 option（选人走远程搜索，不在本条范围）。
    expect(hallPublisherOptions(root)).toEqual([['', '请选择']])
    // ⭐ jsdom 里远程搜索不会往 `<select>` 塞候选，「已选值」只能手工塞一条 option 再赋值
    //    （`select.value = '21'` 在没有对应 option 时会被静默清成 `''`，判据就没了）；
    //    真浏览器里清空的另一半 = `FORM_SELECTS.value(id, [])`，每个方向都有那一半的断言。
    const forceSelected = (select: HTMLSelectElement, value: string, label: string): void => {
      const option = document.createElement('option')
      option.value = value
      option.textContent = label
      select.appendChild(option)
      select.value = value
    }
    forceSelected(legacy, '11', '测试打手甲')
    forceSelected(hall, '21', '测试发单人甲')

    // ① 用户改了新下拉 ⇒ 旧组合控件的已选值被清空（DOM + formselects 两处一起清）。
    handlers.get(HALL_PUBLISHER_SELECT_ID)!()
    expect(legacy.value).toBe('')
    expect(FORM_SELECTS.value).toHaveBeenCalledWith('popgameName', [])

    // ⭐ 「互相清空后只发一个」：把清空后的**页面真状态**交给 `form.val` 的替身再点搜索
    // （旧控件那半已空 ⇒ 只允许发新下拉那一个参数；两个都发的话本条红）。
    LAYUI.form.val.mockReturnValue({
      hallPublisherUser: hall.value,
      popgameName: legacy.value,
      recType: '0',
    })
    clickSearch(root)
    expect(lastWhere()).toMatchObject({ hall_publisher_user: 21 })
    expect('taker_user' in lastWhere()).toBe(false)

    // ② 用户改了旧组合控件 ⇒ 新下拉的已选值被清空（上一半清掉后重赋一次值再验另一方向）。
    hall.value = '21'
    handlers.get('popgameName')!()
    expect(hall.value).toBe('')
    expect(FORM_SELECTS.value).toHaveBeenCalledWith(HALL_PUBLISHER_SELECT_ID, [])

    // ③ 切 `recType`（旧控件那半的档位）也算「旧控件被改」⇒ 新下拉让位。
    hall.value = '21'
    ;(LAYUI.formHandlers.get('select(recType)') as (d: { value: string }) => void)({
      value: '10',
    })
    expect(hall.value).toBe('')
    cleanup()
  })

  it('⭐ 切游戏 ⇒ 零候选请求：两份远程搜索配置换 `data.game` + 下拉重置为仅占位、清空已选并提示重选', async () => {
    // ⚠️ 真浏览器里这一格由 formselects 接管、**原 `<select>.value` 不再是真值** ⇒ 本条把
    // formselects 装成桩、让 `value(id, 'valStr')` 返回已选 id，证明「清空判据」读的是真出口；
    // 只读 DOM value 的退化实现会在 mount 后看到 `''` ⇒ 不提示 ⇒ 本条红。
    let selectedFromFormSelects: unknown = []
    FORM_SELECTS.value.mockImplementation(
      (id: string, type?: string) =>
        id === HALL_PUBLISHER_SELECT_ID && type === 'valStr'
          ? selectedFromFormSelects
          : [],
    )
    vi.stubGlobal('layui', { formSelects: FORM_SELECTS })
    const { root, cleanup } = await mountPage()
    expect(hallPublisherOptions(root)).toEqual([['', '请选择']])
    // 挂载时两份远程搜索配置已注入（`data` = 空对象：首屏没选游戏）。
    expect(FORM_SELECTS.config).toHaveBeenCalledWith(
      POPGAME_SELECT_ID,
      expect.objectContaining({ searchName: 'search', keyVal: 'id', delay: 400, data: {} }),
    )
    expect(FORM_SELECTS.config).toHaveBeenCalledWith(
      HALL_PUBLISHER_SELECT_ID,
      expect.objectContaining({ searchName: 'search', keyVal: 'id', delay: 400, data: {} }),
    )
    const hall = root.querySelector<HTMLSelectElement>(`#${HALL_PUBLISHER_SELECT_ID}`)!
    // DOM 上故意留空：真值只在 formselects 那一侧。
    hall.value = ''
    selectedFromFormSelects = '21'
    // 判别力来源：先塞一条「上一次远程搜索留下的残留候选」，切游戏后必须被清掉。
    const stale = document.createElement('option')
    stale.value = '999'
    stale.textContent = '测试残留候选'
    hall.appendChild(stale)
    LAYUI.form.val.mockReturnValue({ gameName: '7' })
    ;(LAYUI.formHandlers.get('select(selectGame)') as (d: { value: string }) => void)({
      value: '7',
    })
    // 下拉重置为仅占位（残留候选被清掉）+ 清空 + 提示（保留旧 id 只会筛出空集，页面上看不出原因）。
    expect(hallPublisherOptions(root)).toEqual([['', '请选择']])
    expect(hall.value).toBe('')
    expect(LAYUI.msg).toHaveBeenCalledWith(PUBLISHER_GAME_SWITCHED_TEXT, { time: 1500 })
    // ⭐ 切游戏**零**候选请求（候选改远程搜索；回归成「按游戏重取名单」会让本条红）。
    expect(
      mockedRequest.mock.calls.filter((c) => (c[0] as { url?: string }).url === FILTER_OPTIONS_URL),
    ).toHaveLength(0)
    // `data.game` 深合并进两份配置 ⇒ 下一次关键词请求就带新游戏（其余键原样保留）。
    expect(FORM_SELECTS.config).toHaveBeenCalledWith(POPGAME_SELECT_ID, { data: { game: 7 } })
    expect(FORM_SELECTS.config).toHaveBeenCalledWith(HALL_PUBLISHER_SELECT_ID, {
      data: { game: 7 },
    })
    cleanup()
  })

  it('选中的打手 / 发单人**只发其中一个**参数（两个方向，钉在 `collectSearch` 的真出口上）', async () => {
    const { root, cleanup } = await mountPage()
    LAYUI.form.val.mockReturnValue({ recType: '0', popgameName: '11' })
    clickSearch(root)
    expect(lastWhere()).toMatchObject({ taker_user: 11 })
    expect('hall_publisher_user' in lastWhere()).toBe(false)

    LAYUI.form.val.mockReturnValue({ recType: '10', popgameName: '21' })
    clickSearch(root)
    expect(lastWhere()).toMatchObject({ hall_publisher_user: 21 })
    expect('taker_user' in lastWhere()).toBe(false)
    cleanup()
  })

  it('挂载零候选请求 ⇒ 不弹任何提示、控件仍渲染、零写请求（2026-11-09 去预选后的形态）', async () => {
    // 历史注记：预载时代这里钉「截断（*_truncated）不弹提示」（业务方 2026-10-29 拍板）；
    // 去预选后 `*_truncated` 不再有页面出口（截断标记仍由后端返回，搜索语义见第 68 条），
    // 本条保留的是它的后半：挂载安静、控件照常渲染、不发任何写请求。
    const first = await mountPage()
    expect(options(first.root)).toEqual([['', '请选择']])
    expect(first.root.querySelector('#popgameName')!.tagName).toBe('SELECT')
    expect(LAYUI.msg).not.toHaveBeenCalled()
    first.cleanup()

    // 再挂一次（独立 root）同样安静；「零写请求」：整个挂载只发过 GET（来源字典 / 客服候选等）。
    LAYUI.msg.mockClear()
    const second = await mountPage()
    expect(
      mockedRequest.mock.calls.every((c) => (c[0] as { method?: string }).method === 'get'),
    ).toBe(true)
    expect(LAYUI.msg).not.toHaveBeenCalled()
    second.cleanup()
  })

  it('批量搜单号：`selectSeach1=10` ⇒ 弹对标原文的输入层，提交后发 `order_nos`（换行换逗号）', async () => {
    const { root, cleanup } = await mountPage()
    let submit: ((value: string, index: number) => void) | null = null
    LAYUI.layer.prompt.mockImplementation(((_o: unknown, cb: unknown) => {
      submit = cb as (value: string, index: number) => void
    }) as never)
    ;(LAYUI.formHandlers.get('select(selectSeach1)') as (d: { value: string }) => void)({
      value: '10',
    })
    expect(LAYUI.layer.prompt).toHaveBeenCalledWith(
      expect.objectContaining({ formType: 2, title: BATCH_ORDER_PROMPT_TITLE }),
      expect.any(Function),
    )
    expect(submit).not.toBeNull()

    // 形状不合要求（对标：`value.length < 5`）⇒ 提示 + **不记**这一条
    submit!('1234', 3)
    expect(LAYUI.msg).toHaveBeenCalledWith(BATCH_ORDER_INVALID_TEXT, { time: 1500 })
    LAYUI.table.reload.mockClear()
    clickSearch(root)
    expect(lastWhere().order_nos).toBeUndefined()

    // 合法输入：换行换成逗号后送出
    submit!(`${ORDER_NO_A}\n${ORDER_NO_B}`, 4)
    LAYUI.table.reload.mockClear()
    clickSearch(root)
    expect(lastWhere().order_nos).toBe(`${ORDER_NO_A},${ORDER_NO_B}`)
    cleanup()
  })
})

/**
 * ⭐ T7（2026-10-09 价格口径线）：`3 接发组合` 弹层的**真交互**。
 *
 * 判据全部打在**真出口**上（不测中间态）：
 *  - ① 选到 `3` ⇒ `layer.open` 的实参（`title` / `shade` / `shadeClose` / `type` / `skin` /
 *    `anim` / `area` **逐字**照对标）；
 *  - ② 弹层那颗「搜索」上**没有** `data-notopen`（有它就会被 `onNotOpenClick` 拦成「尚未开放」）；
 *  - ③ 4 格填**合法**值 + 点它 ⇒ **真的**发出一发 `table.reload`，`where` 里带 `show_price_type=3`
 *    与那 4 个区间参数（值取自**用户填进 DOM 的**值，⛔ 不是手写常量）；
 *  - ④ 某格**非法** ⇒ 弹**逐字**那条文案、且**一个请求都不发**。
 *
 * ⚠️ ③/④ 是「自建 island 页必须驱动真交互」那条规则的落地：`typecheck` 看不见选择器对不对、
 * `lint` 看不见类名里多了个点，而「点了没反应」是**静默**的（AGENTS.md §5-G）。
 */
describe('页面级：`3 接发组合` 弹层（T7，2026-10-09 价格口径线）', () => {
  beforeEach(() => {
    LAYUI.msg.mockClear()
    LAYUI.layer.open.mockClear()
    LAYUI.layer.close.mockClear()
    LAYUI.layer.prompt.mockClear()
    LAYUI.table.reload.mockClear()
    LAYUI.formHandlers.clear()
    LAYUI.form.val.mockReturnValue({})
    mockedRequest.mockReset()
    mockedRequest.mockImplementation((async () => {
      // 这一组只关心**筛选**：候选名单 / 字典一律回空（口径同 `fetchGames().catch(() => [])`）。
      return { items: [], total: 0, page: 1, page_size: 0 }
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
    const { mountOrderListPage } = await import('../order-orderlist')
    const cleanup = await mountOrderListPage(root)
    return { root, cleanup }
  }

  /** 走页面真实的 `form.on('select(showPriceType)')` 回调（模拟用户在真控件上切档）。 */
  function selectTier(value: string): void {
    const handler = LAYUI.formHandlers.get('select(showPriceType)') as
      | ((d: { value: string }) => void)
      | undefined
    expect(handler, '页面应当注册 `select(showPriceType)` 的切档监听').toBeTypeOf('function')
    handler!({ value })
  }

  /** 把值填进**真 DOM** 里那 4 个框（⛔ 不走 `form.val` 桩：那会绕开被测的那条路径）。 */
  function fillComboInputs(root: HTMLElement, values: readonly string[]): void {
    const ids = ['seachMutPrice_p1', 'seachMutPrice_p2', 'seachMutPrice_p3', 'seachMutPrice_p4']
    ids.forEach((id, index) => {
      const input = root.querySelector<HTMLInputElement>(`#${id}`)
      expect(input, `证据件里应当有 #${id}`).not.toBeNull()
      input!.value = values[index] ?? ''
    })
  }

  /** 点弹层那颗「搜索」（真 DOM 上的 click，走文档级捕获）。 */
  function clickComboSearch(root: HTMLElement): void {
    const btn = root.querySelector<HTMLElement>('.seachMutPriceBtn')
    expect(btn, '证据件里应当有 `.seachMutPriceBtn`').not.toBeNull()
    btn!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  }

  /** 点主栏那颗「搜索」（`collectSearch` 的真调用点之一）。 */
  function clickMainSearch(root: HTMLElement): void {
    const btn = root.querySelector<HTMLElement>('[data-tb="seachBtnOk"]')
    expect(btn, '主栏搜索按钮应当在证据件里').not.toBeNull()
    btn!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  }

  function lastWhere(): Record<string, unknown> {
    const calls = LAYUI.table.reload.mock.calls
    const call = calls[calls.length - 1]
    expect(call?.[0]).toBe('datatable')
    return (call?.[1] as { where: Record<string, unknown> } | undefined)?.where ?? {}
  }

  it('① 选到 `3` ⇒ `layer.open` 装 `.seachMutPriceDiv`，7 个参数逐字照对标', async () => {
    const { root, cleanup } = await mountPage()
    selectTier('3')
    expect(LAYUI.layer.open).toHaveBeenCalledTimes(1)
    const options = LAYUI.layer.open.mock.calls[0]?.[0] as Record<string, unknown>
    expect(options.title).toBe(COMBO_PRICE_LAYER_TITLE)
    expect(options.shade).toEqual([0.3, '#333'])
    expect(options.shadeClose).toBe(true)
    expect(options.type).toBe(1)
    expect(options.skin).toBe('layui-layer-lan')
    expect(options.anim).toBe(5)
    expect(options.area).toEqual(['560px', '250px'])
    // `content` 就是那块 div 本身（对标 `content: $('.seachMutPriceDiv')`）
    const panel = root.querySelector<HTMLElement>('.seachMutPriceDiv')
    expect(panel).not.toBeNull()
    expect(options.content).toBe(panel)
    // 打开时把它从 `display:none` 里放出来（对标同形）
    expect(panel!.style.display).toBe('')

    // 切到别的档 ⇒ 收掉弹层、把它隐藏回去，且**不再**弹层
    LAYUI.layer.open.mockClear()
    selectTier('0')
    expect(LAYUI.layer.open).not.toHaveBeenCalled()
    expect(panel!.style.display).toBe('none')
    cleanup()
  })

  it('② `.seachMutPriceBtn` 上**没有** `data-notopen`（有它就会被 root 级兜底拦成「尚未开放」）', async () => {
    const { root, cleanup } = await mountPage()
    expect(root.querySelector('.seachMutPriceBtn')!.getAttribute('data-notopen')).toBeNull()
    // 点它时**不该**出现「尚未开放」那句（兜底监听真的够不到它了）
    LAYUI.msg.mockClear()
    clickComboSearch(root)
    expect(
      LAYUI.msg.mock.calls.filter((c) => String(c[0]).includes('尚未开放')),
    ).toEqual([])
    cleanup()
  })

  it('③ 4 格填合法值 + 点「搜索」⇒ 真发一发 `table.reload`，`where` 带 `3` 与 4 个区间', async () => {
    const { root, cleanup } = await mountPage()
    // 用户在真控件上选到档 `3`（`form.val` 是桩 ⇒ 这里同时把它设成真实页面会读到的值）
    selectTier('3')
    LAYUI.form.val.mockReturnValue({ showPriceType: '3' })
    LAYUI.table.reload.mockClear()
    fillComboInputs(root, ['-999999', '99999999', '50', '150'])
    clickComboSearch(root)
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
    expect(lastWhere()).toMatchObject({
      show_price_type: '3',
      price_min: '-999999',
      price_max: '99999999',
      hall_price_min: '50',
      hall_price_max: '150',
    })
    // ⛔ 主栏那两格的值**不许**混进来（本档的口径在弹层那 4 格）
    expect(lastWhere().price_min).toBe('-999999')
    cleanup()
  })

  it('④ 某格填非法（`1.234` / `abc`）⇒ 弹**逐字**那条文案，且**一个请求都不发**', async () => {
    const { root, cleanup } = await mountPage()
    selectTier('3')
    LAYUI.form.val.mockReturnValue({ showPriceType: '3' })
    for (const [index, bad] of [
      [0, '1.234'],
      [1, 'abc'],
    ] as const) {
      const values = ['10', '20', '30', '40']
      values[index] = bad
      fillComboInputs(root, values)
      LAYUI.msg.mockClear()
      LAYUI.table.reload.mockClear()
      clickComboSearch(root)
      expect(LAYUI.msg).toHaveBeenCalledWith(COMBO_PRICE_INVALID_MESSAGES[index], { time: 1500 })
      expect(LAYUI.table.reload, `「${bad}」这一格非法时不该发请求`).not.toHaveBeenCalled()
    }
    cleanup()
  })

  /**
   * ⭐ ⑤ **修复轮补**：档 `3` 时主栏那两格「填了不生效」要**真的弹到用户眼前**
   * （整链：真 DOM 上点搜索 → `collectSearch` → `notOpenMessage` → `layer.msg`）。
   *
   * ⚠️ 单测（`usedNotOpenFilters` 那条）只证明函数**返回**了那句话；这条证明它**走到了出口**。
   * ⚠️ 对照：主栏那两格**没动过**（与首屏默认值相同）时**不许**弹——否则每次搜索都闪一句。
   */
  it('⑤ 档 `3` + 主栏区间被改过 ⇒ 点搜索弹那句「主栏的金额区间已忽略」；没动过则不弹', async () => {
    const { root, cleanup } = await mountPage()
    // 首屏快照 = 挂载那一刻的 `form.val`（本用例的桩回 `{}`）⇒ 下面这组值相对它是「改过」。
    LAYUI.form.val.mockReturnValue({ showPriceType: '3', minPrice: '100' })
    LAYUI.msg.mockClear()
    clickMainSearch(root)
    expect(LAYUI.msg).toHaveBeenCalledWith(COMBO_PRICE_MAIN_RANGE_IGNORED_TEXT, { time: 1500 })

    // 对照：回到档 `0`（那两格真送参）⇒ 同一组值**不报**（不许误伤）
    LAYUI.msg.mockClear()
    LAYUI.form.val.mockReturnValue({ showPriceType: '0', minPrice: '100' })
    clickMainSearch(root)
    expect(LAYUI.msg).not.toHaveBeenCalledWith(COMBO_PRICE_MAIN_RANGE_IGNORED_TEXT, {
      time: 1500,
    })
    cleanup()
  })

  /**
   * ⭐ ⑥ **承重路径**（懒绑定的代价必须被钉住）：**关层 → 再开 → 点「搜索」仍然有效**，
   * 且**关层之后那颗按钮不再有任何行为**。
   *
   * 本页那颗「搜索」的监听是**开层时绑在按钮元素上、关层时摘掉**的（⛔ 不是挂载期的文档级委托，
   * 理由见 `onComboPriceSearchClick` 的注释）。两个**相反**的失效方向各自有一半判据：
   *  - **少绑一次**（`openComboPriceLayer` 里那句绑定被删、或被挪到只跑一次的路径）⇒ 首次点击就
   *    不发请求 ⇒ ① 红（变异 MG1 实测 KILLED）；
   *  - **关层没摘**（`closeComboPriceLayer` 里那句 `unbind…` 被删）⇒ 那颗**已经隐藏**的按钮
   *    还留着监听 ⇒ ③ 那一发程序化点击会**多打一发** ⇒ 红（变异 MG2 实测 KILLED）。
   *    ⚠️ 这一半**不能**靠开层时那句「先 remove 再 add」的幂等盖住：幂等管的是「重复 add 不叠加」，
   *    管不了「层关了监听还在」——⛔ 别把 ③ 删掉，它是 `unbind` 的唯一判据。
   * ⚠️ 两次点击填的值**故意不同**（`50/150` ⇒ `60/160`）：这样「第二次用的还是第一次那 4 个值」
   *    （即 `collectSearch` 读了缓存而不是 DOM）也会被 ② 那条 `toMatchObject` 抓到。
   */
  it('⑥ 关层 → 再开 → 点「搜索」仍然有效；关层后那颗按钮不再有任何行为', async () => {
    const { root, cleanup } = await mountPage()
    selectTier('3')
    LAYUI.form.val.mockReturnValue({ showPriceType: '3' })
    fillComboInputs(root, ['-999999', '99999999', '50', '150'])
    LAYUI.table.reload.mockClear()
    clickComboSearch(root)
    // ① 首次点击：**恰好一发**（0 ⇒ 没绑上；2 ⇒ 绑重了）
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
    expect(lastWhere()).toMatchObject({ hall_price_min: '50', hall_price_max: '150' })

    // ② 关层（成功那条路径自己会关）⇒ 再开一次（用户切走再切回来是常规操作）⇒ **仍然有效**
    selectTier('0')
    selectTier('3')
    fillComboInputs(root, ['-999999', '99999999', '60', '160'])
    clickComboSearch(root)
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(2)
    expect(lastWhere()).toMatchObject({ hall_price_min: '60', hall_price_max: '160' })

    // ③ 再关层 ⇒ 那颗按钮**不该再有任何行为**（监听真的被摘了，而不是只把层藏了）
    selectTier('0')
    const before = LAYUI.table.reload.mock.calls.length
    clickComboSearch(root)
    expect(LAYUI.table.reload.mock.calls.length, '关层后那颗按钮还留着监听').toBe(before)
    cleanup()
  })
})

