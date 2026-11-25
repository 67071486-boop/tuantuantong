// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  ALIPAY_COLS,
  ALIPAY_DIALOG_TITLE,
  alipayRows,
  COLS,
  dateTimePart,
  deductFeeTemplet,
  DELETE_CONFIRM_TEXT,
  dlProjectTemplet,
  empPayIdTemplet,
  empTypeTemplet,
  filterWarnings,
  formatDateTime,
  gameOptionsHtml,
  gameRoleTemplet,
  hasUnparsableDate,
  logUserNameTemplet,
  NEED_SELECTION_TEXT,
  ORDER_STATUS_COLORS,
  ORDER_STATUS_DEFAULT_COLOR,
  orderIdTemplet,
  orderStatusOfFilterValue,
  orderStatusTagHtml,
  PAGE_LIMIT,
  PAGE_LIMITS,
  PLAYER_DROPDOWN_HEIGHT_STYLE_ID,
  PLAYER_SEARCH_DELAY_MS,
  PLAYER_SEARCH_PAGE_SIZE,
  PLAYER_SEARCH_URL,
  PLAYER_SELECT_ID,
  buildPlayerSearchConfig,
  performanceAmountTemplet,
  playerRowsFromAccountsEnvelope,
  playerSentinelRows,
  priceTemplet,
  refundFeeTemplet,
  REMOVED_TOOLBAR_EVENTS,
  islandPageUrl,
  SETTLE_DIALOG_TITLE,
  settlementDialogContent,
  settlementIdsParam,
  settleTagHtml,
  spanTemplet,
  TABLE3_ELEM,
  TABLE3_ID,
  TABLE_ELEM,
  TABLE_ID,
  TOOLBAR_ELEM,
  toPerformanceSearch,
  toRow,
  toolbarLabel,
  TOOLBAR_LABEL,
  TOTAL_TEXT,
  totalText,
  usedNotOpenFilters,
  userNameTemplet,
  wangwangTemplet,
  type PerformanceRow,
} from '../emp-emplist'
import {
  PLAYER_OPTION_EXCLUDE_NET_NEGATIVE,
  PLAYER_OPTION_EXCLUDE_SETTLE_FORBIDDEN,
  splitPlayerFilterSelection,
  type PerformanceListItem,
} from '@/api/performances'
// 子页自己声明的 island key（`page=` 的值必须与它逐字一致，见 `settlementDialogContent`）。
// 从**声明它的页面模块**取，与 `system-user-list.spec.ts` 取 `USER_FORM_PAGE_KEY` 是同一手法。
import { PAGE_KEY as SETTLEMENT_PAGE_KEY } from '../emp-settlementemp'

const HERE = path.dirname(fileURLToPath(import.meta.url))
/**
 * 证据件**原文**（`public/replica/emp-emplist.html`）。
 *
 * ⚠️ 读真文件（不是手写 fixture）：「4 颗删除按钮**在证据件里不存在**」这类判据，
 * 只有拿真证据件当输入才是**实测**的（手写 fixture 里当然不存在，那等于自证）。
 */
const EVIDENCE = readFileSync(path.resolve(HERE, '../../../../public/replica/emp-emplist.html'), 'utf8')

/**
 * ⭐⭐ 页面实现文件自己的源码文本。
 *
 * 用途只有一条：钉「那句被废除的提示**一处都不再存在**」（`AGENTS.md` §5-C「不留半新半旧」
 * 要求搜被废除的**主词本身**，而不是它派生出的短语）。
 * ⚠️ 本页是**自建 island 页**（`emp-settlementemp` 那一族的先例），页面源码就是它的实现；
 * 拿它当语料与 `AGENTS.md` §5-G 那条「断言某控件已删要对**标记**、先去掉注释」**不冲突**：
 * 这里要钉的正是「**连注释里的历史叙述都不再出现这句被废除的文案**」——
 * 那句文案是给用户看的提示，任何形态的残留（代码分支或注释）都是「半新半旧」。
 */
const PAGE_SOURCE = readFileSync(
  path.resolve(HERE, '../emp-emplist.ts'),
  'utf8',
)

/** 工具栏模板的原文（`#toolbarDemo`）——「删掉 4 颗」的判据打在它上面。 */
const TOOLBAR_HTML = /<script type="text\/html" id="toolbarDemo">([\s\S]*?)<\/script>/.exec(
  EVIDENCE,
)?.[1]

/** 一行真数据（字段名/类型照 `PerformanceListSerializer` 的实际响应）。 */
const ROW: PerformanceListItem = {
  id: 17,
  order_no: '20260920213215233029',
  /**
   * ⭐ **来源单号**（`PerformanceListSerializer.source_order_no`）。
   *
   * ⚠️ **合成值**，而且**故意不是 19 位数字**：真值形状会命中 `pii-guard.spec.ts` 的
   * 「APPID 形状（19 位数字串）」内建模式（扫描面含 `src/frontend/src/**`）
   * ⇒ 要贴近真实就仿**形状族**（自由文本来源单号，同 `order-orderlist.spec.ts` 的
   * `TB-2026-777` 与 `test_hall_api.py` 的 `TB-合成-大厅-本家`）。
   * ⚠️ 它与下面的 `order_no` **不同形**是**承重**的：同形的话「正文取的是哪个字段」
   * 那条断言**恒真**（自指判据）。
   */
  source_order_no: 'TB-合成-绩效-来源甲',
  user: 5,
  username: 'dailian_A',
  emp_type: '打单工资',
  temp_amount: '88.00',
  bonus_amount: '0.00',
  deduct_mode: '',
  deduct_value: '0.00',
  taker_price: null,
  review_status: '待审核',
  settle_status: '未结账',
  created_at: '2026-09-20T21:32:15+08:00',
  /** ⭐ C 线新增的出参：**枚举名**，不是中文（brief §8.1）。 */
  order_status: 'IN_PROGRESS',
  // ── ⭐ 后端新增的 12 个只读字段（形状逐条照序列化器）──
  order_price: '120.00',
  net_receipts: '100.00',
  refund_fee: '20.00',
  game_role: '旅行者',
  dl_project: '深渊12层',
  wangwang: 'ww_A',
  kefu: 'kefu_A',
  log_user_name: 'kefu_A',
  /** 扣点**额**（后端算好的；越界 ⇒ `null`，那种行不许显示 `0.00`）。 */
  deduct_fee: '4.40',
  settled_at: null,
  settled_by: '',
  settled_amount: null,
  negative_debt: false,
}

/**
 * layui 的**可控替身**。真 layui 在 jsdom 里加载不了，所以 `bootstrapIsland` 换成它之后，
 * `table.render` 的配置、`table.on` 的事件回调、`layer.prompt` 的入参都能被断言。
 * 手法照 `order-orderlist.spec.ts`（同一份替身约定）。
 */
const LAYUI = vi.hoisted(() => {
  const handlers = new Map<string, (obj: never) => void>()
  // `form` 模块的两类事件（`submit(seachBtn)` / `select(...)`）另存一份：handler 名与 table 的
  // `toolbar(tableList)` / `edit(tableList)` **同形**（都是 `事件名(过滤器)`），但键不会撞，
  // 不过分开存能让「搜的是表单事件还是表格事件」在用例里一眼看出来。
  const formHandlers = new Map<string, () => unknown>()
  // ⭐ 日期选择器 spy（复刻缺口补齐时加的）：入参 + **它被调那一刻**两个框的现值。
  const laydateRender = vi.fn((cfg: Record<string, unknown>) => ({
    cfg,
    valueAtCall:
      document.querySelector<HTMLInputElement>(String(cfg.elem))?.value ?? null,
  }))
  // ⚠️ spy **只建一次**（这里），再由下面的模块各自引用——不要在两处各建一个 `vi.fn()`：
  //    那种写法下断言打在没被调用的那个上，会以「没弹窗」的假象失败（本文件初版就踩了）。
  const layer = {
    open: vi.fn(),
    close: vi.fn(),
    // ⭐ `window.tools.reloadOrderData()` 的第一跳（对标 `_tools` 的 `layer.closeAll()`）。
    closeAll: vi.fn(),
    msg: vi.fn(),
    confirm: vi.fn(),
    prompt: vi.fn(),
  }
  return {
    handlers,
    formHandlers,
    open: layer.open,
    msg: layer.msg,
    confirm: layer.confirm,
    prompt: layer.prompt,
    closeAll: layer.closeAll,
    form: {
      render: vi.fn(),
      // ⚠️ 返回类型**显式**写成 `Record<string, unknown>`：用例里会 `mockReturnValue` 带上
      //    更多筛选键（游戏/订单状态/用户名…），推断出来的窄类型会让那些用例编译不过。
      // ⚠️ 这里返回的是**页面 `params()` 会拿去折参数的那份表单值**；它是替身，
      //    与 DOM 里那两个日期框的实际内容无关（真 layui 的 `form.val` 会读 DOM）。
      //    用例若关心日期，自己 `mockReturnValueOnce` 一份（或断言 DOM 里的值）。
      val: vi.fn(
        (): Record<string, unknown> => ({
          dateType: '25',
          sDate: '2026-06-20 00:00:00',
          eDate: '2026-09-20 23:59:59',
          settingSta: '',
        }),
      ),
      on: (e: string, cb: () => unknown) => formHandlers.set(e, cb),
    },
    table: {
      // 真 layui 在 ajax 成功后才会调 `done`；替身里由用例显式触发（见 `renderAndFinish`），
      // 因为「挂载即取一次数」正是写在 `done` 回调里的。
      render: vi.fn(),
      reload: vi.fn(),
      // ⚠️ `checkStatus().data` 里的行是 **layui 行**（`toRow()` 的产物，字段名是 `settingSta`
      //    而不是后端行的 `settle_status`）⇒ 替身的类型按 `PerformanceRow`，与页面的
      //    `TableModule.checkStatus` 同一份形状（brief「控制器补充」实测）。
      checkStatus: vi.fn(() => ({ data: [] as PerformanceRow[] })),
      on: (e: string, cb: (obj: never) => void) => handlers.set(e, cb),
    },
    layer,
    element: { init: vi.fn() },
    // ⭐ 日期选择器（复刻缺口补齐时加的）：记下 `laydate.render` 的**入参**，
    //    并**在它被调的那一刻**读一次两个框的现值 —— 后者是**顺序判据**：
    //    `laydate` 建实例时读 `elem` 的现值，若先 render 再填默认值，选择器里就是空的。
    laydateRender,
    // `bootstrapIsland` 收到的**模块清单**（判据：页面确实向 layui 要了 `laydate`；
    // 替身里 `laydate` 恒存在，不记这一笔就抓不到「忘了要模块」——真 layui 下那是 undefined ⇒ 崩）。
    bootstrapModules: [] as string[][],
    // ⭐ 查重弹层的 `content` 必须是 jQuery 对象（`layui.$(node)`，见 `openAlipayDialog` 的注释）。
    //    替身只要能证明「传进去的是页面里那个容器节点」即可——弹层行为由真 layui 负责。
    $: (el: unknown) => el,
    laydate: { render: laydateRender },
  }
})

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return {
    ...actual,
    bootstrapIsland: (async (mods: string[]) => {
      LAYUI.bootstrapModules.push(mods)
      return LAYUI
    }) as unknown as typeof actual.bootstrapIsland,
    // `islandMsg` 的实现要走真 layui 的 `layer.msg`；替身里直接转发到同一个 spy，
    // 于是「点了按钮提示了什么」可以在用例里断言。
    islandMsg: (_layer: unknown, text: string) => LAYUI.layer.msg(text),
  }
})

// 失败提示走 `notifyError`（Element Plus 的 ElMessage）。断言「后台 msg 有没有原样露出来」
// 就要换掉它——`layer.msg` 是**中性提示**的出口，不是错误出口（两者刻意分开，见 api/notify.ts）。
vi.mock('@/api/notify', () => ({ notifyError: vi.fn(), notifyTip: vi.fn() }))

vi.mock('@/api/performances', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/performances')>()
  return {
    ...actual,
    fetchPerformances: vi.fn(),
    adjustPerformance: vi.fn(async () => ROW),
    reviewPerformance: vi.fn(async () => ROW),
    deletePerformances: vi.fn(async () => ({ deleted: [11, 12] })),
    downloadPerformanceExport: vi.fn(async () => ({
      // 桩是 xlsx：zip 魔数开头，文件名也是 xlsx。
      blob: new Blob(['PK\u0003\u0004 fake-xlsx'], {
        type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      }),
      filename: 'performances-20260922.xlsx',
    })),
    fetchAlipayDuplicates: vi.fn(async () => ({
      duplicates: [
        {
          alipay_account: 'zhifubao_dup@example.com',
          count: 2,
          users: [
            { username: 'kefu_A', job: '客服', is_active: true },
            { username: 'kefu_B', job: '客服主管', is_active: false },
          ],
        },
      ],
    })),
  }
})

// 结账：**整个模块替身**——本页**一次都不该调**它（选状态 / 撤销原因全在
// 「清算绩效」子页里），这里是**防回归网**：谁把 `settle` / `revoke` 又插回本页，`not.toHaveBeenCalled`
// 那一组会立刻红；同时确保万一真被调到也不会发出网络请求。
// 「那些调用真的会变成哪个 URL / body」由 `api/__tests__/settlements.spec.ts` 钉住。
vi.mock('@/api/settlements', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/settlements')>()
  return {
    ...actual,
    settlePerformances: vi.fn(async () => ({
      // ⚠️ 后端真形状是 **id 的列表**（实测：`{"settled_performances": [4], "settled_adjustments": []}`，
      //    见 `api/settlements.ts` 的类型注释）⇒ 这里的假数据也用列表，页面必须取 `.length`。
      settled_performances: [11, 12],
      settled_adjustments: [7],
    })),
    revokeSettlements: vi.fn(async () => ({
      revoked_performances: [11, 12],
      revoked_adjustments: [7],
    })),
  }
})

// 三个筛选字典（游戏 / 订单来源 / 本租户用户名）。**必须打桩**：页面在 `mount` 里就取它们，
// 不打桩会走真 `request()` → jsdom 里发真请求。
vi.mock('@/api/config', () => ({
  fetchGames: vi.fn(async () => [
    { id: 101, name: '原神' },
    { id: 102, name: '崩坏：星穹铁道' },
  ]),
}))
vi.mock('@/api/orderSources', () => ({
  fetchOrderSources: vi.fn(async () => ({
    items: [
      { id: 7, name: '淘宝' },
      { id: 8, name: '闲鱼' },
    ],
    total: 2,
    page: 1,
    page_size: 200,
  })),
  // ⭐ 公司店铺分组：筛选栏「订单分组」的候选。
  fetchOrderSourceGroups: vi.fn(async () => ({
    items: [
      { id: 31, name: '甲组' },
      { id: 32, name: '乙组' },
    ],
    total: 2,
    page: 1,
    page_size: 200,
  })),
}))
vi.mock('@/api/accounts', () => ({
  listAccounts: vi.fn(async () => ({
    items: [{ username: 'dailian_A' }, { username: 'kefu_A' }],
    total: 2,
    page: 1,
    page_size: 200,
  })),
}))

import { adjustPerformance, deletePerformances, downloadPerformanceExport, fetchAlipayDuplicates } from '@/api/performances'
import { listAccounts } from '@/api/accounts'
import { revokeSettlements, settlePerformances } from '@/api/settlements'
import { notifyError } from '@/api/notify'
import { BizError, HttpError } from '@/api/errors'
import { PERFORMANCES_URL as API_URL_FROM_MODULE } from '@/api/performances'

/** `form.val('eps')` 的**首屏默认值**（每个用例开头都恢复成它）。 */
const DEFAULT_FILTERS: Record<string, unknown> = {
  dateType: '25',
  sDate: '2026-06-20 00:00:00',
  eDate: '2026-09-20 23:59:59',
  settingSta: '',
}

const mockedAdjust = vi.mocked(adjustPerformance)
const mockedDelete = vi.mocked(deletePerformances)
const mockedExport = vi.mocked(downloadPerformanceExport)
const mockedAlipay = vi.mocked(fetchAlipayDuplicates)
const mockedRevoke = vi.mocked(revokeSettlements)
const mockedSettle = vi.mocked(settlePerformances)
const mockedNotifyError = vi.mocked(notifyError)

beforeEach(() => {
  LAYUI.handlers.clear()
  LAYUI.formHandlers.clear()
  LAYUI.table.render.mockClear()
  LAYUI.table.reload.mockClear()
  LAYUI.form.val.mockClear()
  // ⚠️ **必须同时复位实现**：`mockReturnValue`（非 Once）会**跨用例**留下去，上个用例里
  //    改过的筛选值会污染下一个用例的 `where`（本轮实测踩到：导出用例把 `game`/`username`
  //    留在 mock 里，后面结账用例的 `where` 断言因此多出 5 个键）。
  LAYUI.form.val.mockReset()
  LAYUI.form.val.mockReturnValue({ ...DEFAULT_FILTERS })
  LAYUI.form.render.mockClear()
  // ⚠️ 日期选择器那支也要复位：`mock.results` 会**跨用例累积**（不清的话「render 时框里的值」
  //    那条顺序判据会读到上一个用例留下的记录，判据就废了）。
  LAYUI.laydateRender.mockClear()
  LAYUI.bootstrapModules.length = 0
  LAYUI.open.mockClear()
  LAYUI.msg.mockClear()
  LAYUI.layer.confirm.mockClear()
  LAYUI.layer.prompt.mockClear()
  LAYUI.layer.close.mockClear()
  LAYUI.layer.closeAll.mockClear()
  mockedAdjust.mockClear()
  mockedDelete.mockClear()
  mockedExport.mockClear()
  mockedAlipay.mockClear()
  mockedRevoke.mockClear()
  mockedSettle.mockClear()
  mockedNotifyError.mockClear()
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: true, status: 200, text: async () => EVIDENCE })),
  )
  // ⭐ jsdom **没有** `URL.createObjectURL` / `revokeObjectURL`（导出落盘那条路会炸）⇒ 打桩。
  vi.stubGlobal('URL', Object.assign(URL, {
    createObjectURL: vi.fn(() => 'blob:mock'),
    revokeObjectURL: vi.fn(),
  }))
})

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

/* ────── 纯函数（列表 → 后端参数；后端支持六个，本页最多发五个——没接 `user`） ────── */

describe('绩效管理（/Emp/emplist）列声明', () => {
  it('⭐ 20 列（28 − 删 6 − 再删 2）；勾选列 + 「系统用户」两列 fixed:left', () => {
    expect(TABLE_ELEM).toBe('#tableList')
    expect(TOOLBAR_ELEM).toBe('#toolbarDemo')
    expect(COLS.length).toBe(20)
    expect(COLS[0]?.type).toBe('checkbox')
    expect(COLS[0]?.fixed).toBe('left')
    expect(COLS[1]?.field).toBe('userName')
    expect(COLS[1]?.fixed).toBe('left')
  })

  it('⭐ 20 列**全部可见**：没有一列 `hide: true`（业务方「隐藏的也默认全部展现出来」）', () => {
    // ⚠️ 断言的是**全部列**都没有 hide（不是"某几列没有"）：本轮就是把 28 列里那 4 个 `hide`
    //    连同被删的列一起清掉的，写成子集断言会漏掉将来新加的 `hide`。
    expect(COLS.filter((c) => c.hide === true)).toEqual([])
    expect(COLS.filter((c) => c.title !== undefined)).toHaveLength(19)
  })

  it('⭐ 被删的 6 列 field **不在**列定义里（排团号/传图数/淘宝价/淘宝状态/申请清算/备注说明）', () => {
    // 判据打在 `buildCols()` 的**产物**上：源码里 grep 不到不够——列是运行期对象。
    const fields = COLS.map((c) => c.field)
    for (const gone of ['taskId', 'imgCount', 'taobaoPrice', 'taobaoSta', 'applicationSett', 'empRemark']) {
      expect(fields).not.toContain(gone)
    }
  })

  it('⭐ 2026-09-22 第二条指令再删 2 列：`completeTime` / `exceedHours` **连列对象都不在**', () => {
    // 业务方裁定「这两列不做也不要了 ⇒ 已删列」（不是「尚未开放」、也不是留空）。
    // 判据比「没有 templet」更强：**整列不在 `COLS` 里** ⇒ 表头也不会渲染出来。
    const fields = COLS.map((c) => c.field)
    expect(fields).not.toContain('completeTime')
    expect(fields).not.toContain('exceedHours')
  })

  it('edit:true 只在「预计绩效」「生效绩效」两列（对标是单元格直接编辑）', () => {
    expect(COLS.filter((c) => c.edit === true).map((c) => c.field)).toEqual([
      'tempAmount',
      'bonusAmount',
    ])
  })

  it('「订单项目」没有 width（自动列，不要补）', () => {
    const p = COLS.find((c) => c.field === 'dlProject')
    expect(p?.width).toBeUndefined()
    expect(p?.align).toBe('left')
  })

  it('⭐ 20 列表头逐字（含取消隐藏的 扣点 / 实际收款 / 结账方式；**不含**已删的 完成时间 / 是否超时）', () => {
    expect(COLS.map((c) => c.title)).toEqual([
      undefined,
      '系统用户',
      '预计绩效',
      '生效绩效',
      '扣点',
      '订单金额',
      '实际收款',
      '退款',
      '账单属性',
      '关联订单',
      '角色名',
      '订单项目',
      '销售客服',
      '订单状态',
      '旺旺/YY',
      '记录时间',
      '记录者',
      '结账状态',
      '结账时间',
      '结账方式',
    ])
    // 恰好多一个「勾选列」（`title` 为 `undefined`）⇒ 20 条标题里 19 条有文字。
    expect(COLS.map((c) => c.title).filter((t) => t !== undefined)).toHaveLength(19)
  })

  it('分页：limit 50、limits 8 档（最多到 300）', () => {
    // 出处：`emplist.js` L484-486 `page:true, limit:50, limits:[…,200,300]`。
    expect(PAGE_LIMIT).toBe(50)
    expect(PAGE_LIMITS).toEqual([10, 20, 30, 40, 50, 100, 200, 300])
  })

  it('#total 骨架：`showDeductFee` 为 false ⇒ 「扣点金额…元，」整段不出现；利润是 toFixed(2)', () => {
    // 出处：`emplist.js` L497-500 的 `done`。两处**对标自身的产物缺陷，照抄不修**：
    //  1. 「已生效差额利润」在源码里也用 `profit1` 拼（不是 `profit2`）——**骨架照抄**，
    //     但**值**取后端分开给的两项（见 `totalText` 的用例）；
    //  2. 「已生效差额利润」后面**没有 `元`** 就接了 `】`（源码就是这样拼的，实测对标渲染也如此）。
    expect(TOTAL_TEXT).toBe(
      '本次筛选条件下共计0条记录，总计【预计绩效0.00元，生效绩效0.00元，订单金额0.00元，记录退款金额0，' +
        '预计差额利润0.00元，已生效差额利润0.00】，默认显示最近3个月数据，若需扩大时间范围，可手动选择起始日期',
    )
    expect(TOTAL_TEXT).not.toContain('扣点金额')
  })
})

describe('toPerformanceSearch：只产生后端支持的参数', () => {
  it('默认（录单时间 + 未选结账）⇒ emp_type + 日期两端 + ⭐`date_type=created`', () => {
    // ⭐ 线 C：录单档也**显式**发 `date_type`（后端默认值同为 `created` ⇒ 行为等价，
    //    但契约自描述、不依赖「后端默认值没变」这个隐含前提）。
    expect(
      toPerformanceSearch(
        {
          dateType: '25',
          sDate: '2026-06-20 00:00:00',
          eDate: '2026-09-20 23:59:59',
          settingSta: '',
        },
        '打单工资',
      ),
    ).toEqual({
      emp_type: '打单工资',
      date_type: 'created',
      date_from: '2026-06-20 00:00:00',
      date_to: '2026-09-20 23:59:59',
    })
    // ⭐（2026-11-07 起）date-only 形态照旧发原值（后端整天含端口径不变）——两形态都钉。
    expect(
      toPerformanceSearch(
        { dateType: '25', sDate: '2026-06-20', eDate: '2026-09-20', settingSta: '' },
        '打单工资',
      ),
    ).toEqual({
      emp_type: '打单工资',
      date_type: 'created',
      date_from: '2026-06-20',
      date_to: '2026-09-20',
    })
  })

  it('结账状态 N/Y → 后端中文取值（未结账 / 已结账）', () => {
    expect(toPerformanceSearch({ settingSta: 'N' }, '打单工资').settle_status).toBe('未结账')
    expect(toPerformanceSearch({ settingSta: 'Y' }, '打单工资').settle_status).toBe('已结账')
  })

  it('⭐ `review_status` / `project` **不再由本页产生**（`key2` 已删；`selectSeach1` 现在是对标的「代练/下家」）', () => {
    // ⚠️ 这两条是**变异判据**：把源码里 `REVIEW_STATUS_MAP` / `project` 那两段加回去就会红。
    //    判据打在「产物里没有这个键」上，而不是「某个常量被删了」——常量删了但参数照发是可能的。
    // ⚠️ `selectSeach1` 已在第四轮 C 恢复成对标的「代练/下家 ⇒ 批量搜用户」，它的 `username`
    //    由 `params()` 在本函数的产物**之外**追加（第四轮 M-1：其余筛选照发），⛔ 不经过本函数
    //    ⇒ 这里仍不该因它产生任何键。
    const p = toPerformanceSearch(
      { selectSeach1: '10', key2: '原神', settingSta: '', dateType: '25' },
      '打单工资',
    )
    expect('review_status' in p).toBe(false)
    expect('project' in p).toBe(false)
    expect('username' in p).toBe(false)
    // 阳性对照：同一批输入里**其它**筛选照旧生效（免得上面两条因为「整个函数坏了」而假绿）
    expect(p.emp_type).toBe('打单工资')
    expect(toPerformanceSearch({ settingSta: 'N' }, '打单工资').settle_status).toBe('未结账')
  })

  it('日期取不出来就不发（`?date_from=` 空串会让后端当成没传，但不发更省一次误判）', () => {
    const p = toPerformanceSearch({ sDate: '', eDate: '不是日期' }, '销售提成')
    expect('date_from' in p).toBe(false)
    expect('date_to' in p).toBe(false)
    expect(p.emp_type).toBe('销售提成')
  })

  it('⭐ `user`（人员主键）**任何情况下都不发**——本页走的是 `username`（逗号串批量搜归属人）', () => {
    const p = toPerformanceSearch(
      {
        settingSta: 'N',
        sDate: '2026-06-20 00:00:00',
        eDate: '2026-09-20 23:59:59',
        select: '5',
        user: '5',
      },
      '打单工资',
    )
    expect(Object.keys(p).sort()).toEqual([
      'date_from',
      'date_to',
      'emp_type',
      'settle_status',
    ])
    expect('user' in p).toBe(false)
  })

  it('`hasUnparsableDate`：只有「填了东西但取不出日期/时刻」才算（空框不算、合法值不算）——判据与 `toPerformanceSearch` 丢参的判据同源', () => {
    // 有东西但读不出 ⇒ true（这个参数会被静默丢掉，所以要提示）
    expect(hasUnparsableDate({ sDate: '不是日期', eDate: '2026-09-20 23:59:59' })).toBe(true)
    expect(hasUnparsableDate({ sDate: '', eDate: '2026-9-2' })).toBe(true)
    // 空框 / 只有空白 ⇒ false（用户主动清空，本来就不发这个参数，不该提示）
    expect(hasUnparsableDate({ sDate: '', eDate: '' })).toBe(false)
    expect(hasUnparsableDate({ sDate: '   ', eDate: '' })).toBe(false)
    expect(hasUnparsableDate({})).toBe(false)
    expect(hasUnparsableDate({ sDate: undefined, eDate: null })).toBe(false)
    // 合法 ⇒ false
    expect(hasUnparsableDate({ sDate: '2026-06-20 00:00:00', eDate: '2026-09-20 23:59:59' })).toBe(
      false,
    )
    // ⚠️ 判据是「`dateTimePart` 取不出来」而不是「像不像日期」：`dateTimePart` 是丢参的唯一判据，两处必须一致
    expect(dateTimePart('2026-6-2') === '').toBe(hasUnparsableDate({ sDate: '2026-6-2' }))
    // ⭐（2026-11-07 起）两形态都钉：date-only 照旧原样发、带时间**不再截断**也原样发
    //（⛔ 只钉一边 = 半个契约；「砍掉时分秒」的旧行为已随交单时间筛选修复移除）
    expect(dateTimePart('2026-06-20')).toBe('2026-06-20')
    expect(dateTimePart('2026-06-20 13:45:07')).toBe('2026-06-20 13:45:07')
    expect(dateTimePart('2026-06-20 13:45')).toBe('')
  })

  it('⭐ P4FU-C 的 11 个新筛参逐条接上（值送**我们的**存储形态，不是对标的中文/哨兵值）', () => {
    const p = toPerformanceSearch(
      {
        gameName: '101',
        orderSta: '待验收',
        orderType: '1',
        orderSoures: '7',
        customerType: '10',
        inputOrderKefu: 'kefu_A,kefu_B',
        popgameName: 'dailian_A',
        key1: '20260920213215233029',
        sDate: '2026-06-20 00:00:00',
        eDate: '2026-09-20 23:59:59',
      },
      '打单工资',
    )
    expect(p).toEqual({
      emp_type: '打单工资',
      game: 101,
      // 中文 label → **枚举名**（后端 `ChoiceField(OrderStatus.choices)`）
      order_status: 'PENDING_REVIEW',
      // 对标的 `-1/0/1` → 我们的 `OrderType` 枚举名
      order_type: 'COMPANION',
      order_source: 7,
      customer_type: 10,
      customer: 'kefu_A,kefu_B',
      username: 'dailian_A',
      keyword: '20260920213215233029',
      date_from: '2026-06-20 00:00:00',
      date_to: '2026-09-20 23:59:59',
    })
  })

  it('⭐ 订单分组（2026-10-26）：有值送 int；没选 / `0` / 表外值都不发键；两账单都能带', () => {
    expect(toPerformanceSearch({ sourceGroup: '31' }, '打单工资')).toMatchObject({
      source_group: 31,
    })
    // 两账单共用同一个表单值 ⇒ 换成销售提成档也照样发（页签只切 `emp_type`）。
    expect(toPerformanceSearch({ sourceGroup: '31' }, '销售提成')).toMatchObject({
      emp_type: '销售提成',
      source_group: 31,
    })
    expect(
      'source_group' in toPerformanceSearch({ sourceGroup: '' }, '打单工资'),
    ).toBe(false)
    expect('source_group' in toPerformanceSearch({}, '打单工资')).toBe(false)
    expect(
      'source_group' in toPerformanceSearch({ sourceGroup: '0' }, '打单工资'),
    ).toBe(false)
    expect(
      'source_group' in toPerformanceSearch({ sourceGroup: 'abc' }, '打单工资'),
    ).toBe(false)
  })

  it('⭐ 打手多选哨兵（2026-11-07）：勾「排除禁止结算的」发 excluded、勾「排除总绩效为负的」发 1；缺省都不发', () => {
    const P = PLAYER_OPTION_EXCLUDE_SETTLE_FORBIDDEN
    const N = PLAYER_OPTION_EXCLUDE_NET_NEGATIVE
    expect(toPerformanceSearch({ popgameName: P }, '打单工资')).toMatchObject({
      settle_forbidden_scope: 'excluded',
    })
    // 两账单共用同一个表单值 ⇒ 换成销售提成档也照样发（页签只切 `emp_type`）。
    expect(toPerformanceSearch({ popgameName: N }, '销售提成')).toMatchObject({
      emp_type: '销售提成',
      exclude_net_negative: '1',
    })
    // 两颗并存各自发参。
    expect(toPerformanceSearch({ popgameName: `${P},${N}` }, '打单工资')).toMatchObject({
      settle_forbidden_scope: 'excluded',
      exclude_net_negative: '1',
    })
    // 缺省（没勾哨兵）两个键都不发——默认路径零行为变化。
    expect('settle_forbidden_scope' in toPerformanceSearch({}, '打单工资')).toBe(false)
    expect('exclude_net_negative' in toPerformanceSearch({}, '打单工资')).toBe(false)
  })

  it('⭐ `splitPlayerFilterSelection` 纯函数网：哨兵剥离 / 全哨兵 ⇒ 不发 username / 与真用户名并存', () => {
    const P = PLAYER_OPTION_EXCLUDE_SETTLE_FORBIDDEN
    const N = PLAYER_OPTION_EXCLUDE_NET_NEGATIVE
    // 哨兵剥离 + 空项丢弃 + 顺序保持 + 不去重。
    expect(splitPlayerFilterSelection(['b', P, 'a', '', N])).toEqual({
      usernames: ['b', 'a'],
      excludeSettleForbidden: true,
      excludeNetNegative: true,
    })
    // 全是哨兵 ⇒ usernames 空 ⇒ `toPerformanceSearch` 不发 `username`。
    expect(splitPlayerFilterSelection([P, N]).usernames).toEqual([])
    const only = toPerformanceSearch({ popgameName: `${P},${N}` }, '打单工资')
    expect('username' in only).toBe(false)
    // 与真用户名并存：剩余用户名拼串发 `username`，哨兵各发各的。
    const both = toPerformanceSearch({ popgameName: `dailian_A,${P}` }, '打单工资')
    expect(both.username).toBe('dailian_A')
    expect(both.settle_forbidden_scope).toBe('excluded')
  })

  it('⭐ `customer_type: 0`（销售人）**是合法值、必须发出去**（E-0021：不能用真值判断）', () => {
    const p = toPerformanceSearch({ customerType: '0', inputOrderKefu: 'kefu_A' }, '打单工资')
    expect(p.customer_type).toBe(0)
    expect(p.customer).toBe('kefu_A')
    // 名单为空 ⇒ `customer` 与 `customer_type` 都不发（后端：单独传 customer_type 不过滤）
    const empty = toPerformanceSearch({ customerType: '0', inputOrderKefu: '' }, '打单工资')
    expect('customer' in empty).toBe(false)
    expect('customer_type' in empty).toBe(false)
  })

  it('⭐ 多选拼串只清洗（去空白/去空项），**不排序不去重**', () => {
    const p = toPerformanceSearch({ popgameName: ' b, a ,,b ' }, '打单工资')
    expect(p.username).toBe('b,a,b')
  })

  it('⭐ 「全部」= 不送该参数：game/order_source 的空值与 `0` 都不发（min_value=1）；orderType 的 `-1` 也不发', () => {
    const p = toPerformanceSearch(
      { gameName: '', orderSoures: '0', orderType: '-1' },
      '打单工资',
    )
    expect('game' in p).toBe(false)
    expect('order_source' in p).toBe(false)
    expect('order_type' in p).toBe(false)
    // ⛔ 对标的「全部游戏」哨兵值 `21714` **不是**我们的游戏 id，也不该被送——这里只证明
    //    「非数字/0 不发」这条；真实的 option 列表由 `gameOptionsHtml` 生成（见下一条）。
    expect('game' in toPerformanceSearch({ gameName: 'abc' }, '打单工资')).toBe(false)
  })

  it('⭐⭐ 日期口径：**五档全部**折成 `date_type` 进请求（批次 5 · 线 E 起全通）', () => {
    // ⭐⭐ **五档全部**能对上列、一个都不落空：验收档打 `Order.completed_at`
    //    （⛔ 不是生产上零写入的 `Performance.reviewed_at`；发单/交单同理打订单的
    //    `published_at` / `submitted_at`）；能对上的档各自映射，表外档不发 `date_type`。
    // ① 五档逐条钉值（⛔ 不是只断言「有 date_type」——那样把 completed 写成 created 也绿）
    expect(toPerformanceSearch({ dateType: '10' }, '打单工资').date_type).toBe('published')
    expect(toPerformanceSearch({ dateType: '20' }, '打单工资').date_type).toBe('submitted')
    expect(toPerformanceSearch({ dateType: '25' }, '打单工资').date_type).toBe('created')
    expect(toPerformanceSearch({ dateType: '40' }, '打单工资').date_type).toBe('completed')
    expect(toPerformanceSearch({ dateType: '30' }, '打单工资').date_type).toBe('settled')
    // ② `reviewed` 这个**旧取值不再存在**（改名换列，⛔ 不留两套契约）：没有任何控件值
    //    会折出它（把映射改回 `'40': 'reviewed'` ⇒ 上面第 4 条与本条同时红）。
    const allMapped = ['10', '20', '25', '30', '40'].map(
      (v) => toPerformanceSearch({ dateType: v }, '打单工资').date_type,
    )
    expect(allMapped).not.toContain('reviewed')
    // ③ 发单（10）档现在**真的发**，且日期两端照旧一起发（同一次进请求）
    const published = toPerformanceSearch(
      { dateType: '10', sDate: '2026-06-20 00:00:00' },
      '打单工资',
    )
    expect(published.date_type).toBe('published')
    expect(published.date_from).toBe('2026-06-20 00:00:00')
    // ④ 阴性对照：`dateType` **缺席**（替身没给 / 空值）不发，⛔ 不硬编码成 created
    expect('date_type' in toPerformanceSearch({}, '打单工资')).toBe(false)
    // ⑤ 原型键（`constructor` / `toString`）与任意怪值仍算**表外**：`DATE_TYPE_MAP` 是对象字面量，
    //    裸查表 `MAP[v]` 会命中继承来的函数（还会把函数塞进 `date_type`）
    //    ⇒ 查找必须走 `hasOwnProperty`（把这一句改回裸查表，本用例立刻红）。
    expect('date_type' in toPerformanceSearch({ dateType: 'constructor' }, '打单工资')).toBe(false)
    expect('date_type' in toPerformanceSearch({ dateType: 'toString' }, '打单工资')).toBe(false)
    expect('date_type' in toPerformanceSearch({ dateType: '99' }, '打单工资')).toBe(false)
  })

  it('⭐ 「订单状态」多选：全映射送逗号串码；部分映射只送已映射的；全未映射不发键（2026-10-29 实装）', () => {
    // 全映射 ⇒ 逗号串枚举码（后端同批收多值，`order__status__in`）
    expect(toPerformanceSearch({ orderSta: '待验收,已完成' }, '打单工资').order_status).toBe(
      'PENDING_REVIEW,COMPLETED',
    )
    // 部分映射：已映射的照送，未映射的档位不进 `order_status`（提示归 `usedNotOpenFilters`）
    expect(toPerformanceSearch({ orderSta: '待验收,不存在的状态' }, '打单工资').order_status).toBe(
      'PENDING_REVIEW',
    )
    // 单个已映射档位 = 多选的特例（join 后仍是那个码本身）
    expect(toPerformanceSearch({ orderSta: '待验收' }, '打单工资').order_status).toBe(
      'PENDING_REVIEW',
    )
    // 全未映射 / 没选 ⇒ 不发键
    expect('order_status' in toPerformanceSearch({ orderSta: '不存在的状态' }, '打单工资')).toBe(
      false,
    )
    expect('order_status' in toPerformanceSearch({}, '打单工资')).toBe(false)
  })

  it('⭐ 「搜索1」切到「批量搜单号」⇒ 不发 `keyword`（后端 order_no 是单个精确值）', () => {
    expect(toPerformanceSearch({ selectSeach2: '10', key1: 'A,B' }, '打单工资').keyword).toBeUndefined()
    expect(toPerformanceSearch({ selectSeach2: '0', key1: 'A' }, '打单工资').keyword).toBe('A')
  })

  it('⭐ 「收款状态」两档真发参、其它档不发（D-0365，2026-11-02 实装）', () => {
    // 已映射的两档 = 证据件 option 的 **value**（文案是「未收款/已结款」，值是这两个词）
    expect(toPerformanceSearch({ furlSta: '未结算' }, '打单工资').net_receipts_state).toBe(
      'unreceived',
    )
    expect(toPerformanceSearch({ furlSta: '已结算' }, '打单工资').net_receipts_state).toBe(
      'received',
    )
    // 「结算中」没有我方对应值（色块同款两态）⇒ 不发参（提示归 `usedNotOpenFilters` 点名）
    expect(
      'net_receipts_state' in toPerformanceSearch({ furlSta: '结算中' }, '打单工资'),
    ).toBe(false)
    // 没选 / 怪值 / 原型键都不发（`hasOwnProperty` 判据：裸查表会把 `constructor` 当已映射）
    expect('net_receipts_state' in toPerformanceSearch({}, '打单工资')).toBe(false)
    expect(
      'net_receipts_state' in toPerformanceSearch({ furlSta: 'constructor' }, '打单工资'),
    ).toBe(false)
    expect('net_receipts_state' in toPerformanceSearch({ furlSta: 'toString' }, '打单工资')).toBe(
      false,
    )
  })

  it('★ 2 类**保留**的无数据控件不产生任何参数（发了是静默忽略）', () => {
    // ⛔（D-0365）`furlSta` 真发参（上面那条用例），不在本表里；本表剩的是
    //    申请结算的 / 排团团号两类。
    const p = toPerformanceSearch(
      {
        applicationSetSta: 'on',
        taskSelect: '5',
        sDate: '2026-06-20 00:00:00',
        eDate: '2026-09-20 23:59:59',
      },
      '打单工资',
    )
    expect(Object.keys(p).sort()).toEqual(['date_from', 'date_to', 'emp_type'])
  })

  it('⭐ 被删掉的 4 个控件名也**不产生任何参数**（`taobaoSta`/`isCert`/`key3`/`key2`）', () => {
    // ⚠️ 集合口径是 **4 个**：`selectSeach1` 是对标的「代练/下家」控件
    //    （见页面「已知会不同」第 30 条），不属于「被删掉的控件」。
    //    ⛔ 没有放宽成 `toContain`：是集合本身少了一项。
    // ⛔（D-0365）阳性对照用仍是「尚未开放」的 `taskSelect`（`furlSta` 已真发参）。
    const p = toPerformanceSearch(
      {
        taobaoSta: '待发货',
        isCert: 'Y',
        key3: 'z',
        key2: '原神',
        taskSelect: '5',
      },
      '打单工资',
    )
    expect(Object.keys(p)).toEqual(['emp_type'])
  })
})

describe('筛选提示（`filterWarnings`）：合并成一条、逐条可辨', () => {
  it('默认值 ⇒ 一条都不弹', () => {
    expect(filterWarnings({ dateType: '25', orderSta: '' })).toEqual([])
  })

  it('⭐ 保留的「尚未开放」控件：**用过的**（非空）才点名，合并成一条（D-0365 改准）', () => {
    // ⛔（D-0365）收款状态的**已映射两档不点名**（真发参）；
    //    「结算中」作为未映射档位照旧点名。
    expect(usedNotOpenFilters({ furlSta: '未结算' })).toEqual([])
    expect(usedNotOpenFilters({ furlSta: '已结算' })).toEqual([])
    expect(usedNotOpenFilters({ furlSta: '结算中' })).toEqual(['结算中'])
    expect(
      usedNotOpenFilters({ applicationSetSta: 'on', taskSelect: '' }),
    ).toEqual(['申请结算的'])
    expect(
      filterWarnings({ dateType: '25', applicationSetSta: 'on', taskSelect: '5' }),
    ).toEqual(['「申请结算的、排团团号」尚未开放，本次已忽略'])
    // 排团团号那一颗也要能单独点名（`taskSelect` 是 xm-select-radio，值是单个字符串）
    expect(usedNotOpenFilters({ taskSelect: '5' })).toEqual(['排团团号'])
  })

  it('⭐ 被删掉的 3 个控件**不再点名**（它们是裁剪，不是「尚未开放」）', () => {
    // 变异判据：把 `NOT_OPEN_FILTERS` 里那三行加回去就红。
    expect(usedNotOpenFilters({ taobaoSta: '待发货', isCert: 'Y', key3: 'z' })).toEqual([])
    // 阳性对照（D-0365）：`furlSta` 的已映射档如今真发参不点名 ⇒
    // 换成「结算中」（仍是「不发参 + 点名」的档位）免得上面因「整个函数坏了」假绿。
    expect(usedNotOpenFilters({ taobaoSta: '待发货', furlSta: '结算中' })).toEqual(['结算中'])
  })

  it('「订单状态」未映射档位**点名档位**（2026-10-29 实装多选后的口径）', () => {
    // 多选且全部已映射 ⇒ 真送参、一条都不报
    expect(usedNotOpenFilters({ orderSta: '待验收,已完成' })).toEqual([])
    // 混选：只报未映射的那几档、按选中顺序（不带「（多选）」字样——多选已实装）
    expect(usedNotOpenFilters({ orderSta: '待验收,不存在的状态' })).toEqual(['不存在的状态'])
    // 单选但认不出的档位（理论上不该出现，防将来 option 漂移）也点名档位本身
    expect(usedNotOpenFilters({ orderSta: '不存在的状态' })).toEqual(['不存在的状态'])
    // 认得出的单选 ⇒ 不点名（它是真接了的筛选）
    expect(usedNotOpenFilters({ orderSta: '待验收' })).toEqual([])
  })

  it('⭐ 既有那句「不是可识别的日期」文案**一字未改**（单条警告时「；」拼接的结果与原来完全相同）', () => {
    expect(filterWarnings({ dateType: '25', sDate: '不是日期' })).toEqual([
      '「录单时间」不是可识别的日期，本次已忽略该筛选',
    ])
  })

  it('⭐⭐ 日期口径五档全通 ⇒ **一句提示都不弹**；「尚未开放」那句在代码里一处都不再存在', () => {
    // ⭐⭐ 业务方口径「五档全通」：发单（10）/ 交单（20）也接上了，「表外档位」这个概念
    //    在真实控件上**没有实例**——留着那句提示就是给一个不存在的档位留死路径
    //    （设计 §2.5 逐字要求删掉它）。
    // ① 五档**都不弹**那一句（判别力：把哪一档从 `DATE_TYPE_MAP` 里删掉，这里立刻红）
    for (const dateType of ['10', '20', '25', '30', '40']) {
      expect(filterWarnings({ dateType })).toEqual([])
    }
    // ② 怪值（option 漂移 / 替身送任意字符串）同样**不再弹日期口径那句**
    //    ——⛔ 那句文案连同它的分支已经删除，本页面不再对日期口径说任何话。
    //    ⚠️ 这不改变「不发 `date_type`」那条口径（见上面 `toPerformanceSearch` 的用例）。
    for (const dateType of ['99', 'constructor', 'toString', '']) {
      expect(filterWarnings({ dateType })).toEqual([])
    }
    // ③ ⛔ **被废除的「主词」本身一处都不该在**（AGENTS §5-C「不留半新半旧」）：
    //    这一句是**独立于实现对拍**的判据——把分支加回来（哪怕文案改了字）这里先红。
    expect(PAGE_SOURCE).not.toContain('日期口径尚未开放')
    expect(PAGE_SOURCE).not.toContain('「录单时间」以外的日期口径')
  })

  it('⭐ 多条警告**合并成一条**（`layer.msg` 后来的会覆盖先前的）', () => {
    const all = filterWarnings({
      dateType: '10',
      sDate: '不是日期',
      furlSta: '结算中',
      applicationSetSta: 'on',
      key2: '原神',
      selectSeach2: '10',
      key1: 'A',
    })
    // ⚠️ 期望值里**没有**「日期口径尚未开放」（五档全通、那句文案已整条删除）。
    // ⛔（D-0365）`furlSta` 的已映射档不进「尚未开放」；
    //    「结算中」作为未映射档位仍进（与申请结算的合并成同一句）。
    expect(all).toEqual([
      '「申请结算的、结算中」尚未开放，本次已忽略',
      '「录单时间」不是可识别的日期，本次已忽略该筛选',
      '「批量搜单号」尚未开放，本次已忽略「搜索1」',
    ])
  })

  it('⭐ 填了已删的 `key2`（搜索2）**不再多弹那句**（那个控件已从证据件删除）', () => {
    expect(filterWarnings({ dateType: '25', key2: '原神' })).toEqual([])
  })
})

describe('列表行 → 表格列（只映射后端确有的字段）', () => {
  it('字段映射：系统用户 / 两个金额 / **扣点额** / 订单号 / 订单状态 / 结账状态 / 记录时间', () => {
    expect(toRow(ROW)).toMatchObject({
      userName: 'dailian_A',
      tempAmount: '88.00',
      bonusAmount: '0.00',
      // ⭐ 数据源换过了：不是 `deduct_value`（参数值），是后端算好的 `deduct_fee`（金额）
      deductFee: '4.40',
      deductMode: '',
      deductValue: '0.00',
      orderId: '20260920213215233029',
      empType: '打单工资',
      // ⭐ D-0071 ③：这一列显示的是**订单状态**（枚举名 → 中文），不是审核状态
      orderSta: '代练中',
      settingSta: '未结账',
      createTime: '2026-09-20 21:32',
      takerPrice: '',
      settled_at: null,
      id: 17,
    })
  })

  /**
   * ⭐ 「系统用户」列显示的是**昵称**（`display_name`），
   * ⛔ 不是登录账号（`username`）。口径 = 「昵称优先、空串回落登录账号」，
   * 唯一出处 `apps/accounts/display.py::user_display_name`（后端 `PerformanceListSerializer`
   * 的 `display_name` 就是它）。
   *
   * ⚠️ 既有夹具（`ROW`）**没有** `display_name` 键，所以它继续绿**不算**覆盖到了这条
   * ——这条自己带一个与 `username` 不同形的昵称（`测试昵称甲` ≠ `dailian_A`）。
   */
  it('⭐ 「系统用户」列取 `display_name`（昵称优先）：昵称 ≠ 登录账号时显示昵称', () => {
    const row = toRow({ ...ROW, display_name: '测试昵称甲' })

    expect(row.userName).toBe('测试昵称甲')
    // 承重：夹具里昵称与登录账号**不同形** ⇒ 实现若仍取 `item.username`，上一行立刻红。
    expect(row.userName).not.toBe('dailian_A')
    // 这一列有 templet（`userNameTemplet`，会转义）⇒ 昵称真的流到单元格里。
    expect(userNameTemplet(row)).toContain('测试昵称甲')
  })

  it('⭐ `display_name` **缺席**（前端已上、后端没上）⇒ 回落登录账号（不留空单元格）', () => {
    const { display_name: dropped, ...withoutName } = { ...ROW, display_name: '测试昵称甲' }

    // 非空哨兵：那一格**本来有**昵称（否则下面两条会以「反正没有」变成恒真）。
    expect(dropped).toBe('测试昵称甲')
    // 键整个不在（旧后端的载荷形状）
    expect(toRow(withoutName as PerformanceListItem).userName).toBe('dailian_A')
    // 键在但值是 `undefined`（同类窗口的另一种形状）
    expect(toRow({ ...ROW, display_name: undefined }).userName).toBe('dailian_A')
    // ⚠️ 空串**不是**回落条件（`??` 只认 `null`/`undefined`）：后端对 `user=null` 的行回的
    // 就是空串（`get_display_name` 的 `user is None` 分支），那时两处口径都是空单元格。
    expect(toRow({ ...ROW, display_name: '', user: null, username: null }).userName).toBe('')
  })

  it('⭐ 后端出参 12 个、本页消费 10 个：这里逐条钉其中 9 个的字面值（第 10 个 `settled_at` 见相邻用例）', () => {
    // ⚠️ **不是「12 个逐条」**：后端出参确实 12 个，
    //    但 `settled_by` / `settled_amount` **本页没有对应列、也没有任何消费者** ⇒ 刻意不钉它们。
    // 判据是**字面值**（不是断言某个常量）——照 `AGENTS.md` §5「断言要钉值，不要钉常量」。
    expect(toRow(ROW)).toMatchObject({
      price: '120.00',
      netReceipts: '100.00',
      refundFee: '20.00',
      gameRole: '旅行者',
      dlProject: '深渊12层',
      wangwang: 'ww_A',
      kefu: 'kefu_A',
      logUserName: 'kefu_A',
      settingTime: '—',
    })
    // 「本页没有消费者」不是嘴上说说：行载荷里**没有**这两个键（有消费者才会往 `toRow` 里放）。
    const r = toRow(ROW)
    expect('settled_by' in r).toBe(false)
    expect('settled_amount' in r).toBe(false)
  })

  it('⭐ 三个可空字段为 `null` ⇒ 各自的**口径**（金额 `—` / 结账时间 `—` / 文本空串）', () => {
    const row = toRow({
      ...ROW,
      net_receipts: null,
      deduct_fee: null,
      settled_at: null,
      settled_amount: null,
      settled_by: '',
    })
    expect(row.netReceipts).toBe('—')
    // ⚠️ 显示值 `—`，而**原值**另存一份（`net_receipts`）给 `priceTemplet` 的色块判据用：
    //    判据要 `is not None`，被 `—` 化之后就分不出「没录入」与「录了别的」（E-0021）。
    expect(row.net_receipts).toBeNull()
    // ⛔ **不是 `0.00`**：后端刻意用 `null` 表示「这一格没有数」（扣点越界），
    //    显示 `0.00` 会说假话（与「这个数是 0」混淆）。
    expect(row.deductFee).toBe('—')
    expect(row.deductFee).not.toBe('0.00')
    expect(row.settingTime).toBe('—')
    expect(row.settled_at).toBeNull()
    expect(row.userName).toBe('dailian_A')
  })

  it('⭐ 结账留痕有值 ⇒ 结账时间格式化；`deduct_fee` 有值 ⇒ 原样', () => {
    const row = toRow({
      ...ROW,
      settled_at: '2026-09-21T10:05:00+08:00',
      settled_by: 'kefu_A',
      settled_amount: '83.60',
      deduct_fee: '4.40',
    })
    expect(row.settingTime).toBe('2026-09-21 10:05')
    expect(row.settled_at).toBe('2026-09-21T10:05:00+08:00')
    expect(row.deductFee).toBe('4.40')
  })

  it('⭐ `settled_at` 有值但**格式取不出来** ⇒ 不是 `—`（那是「没结账」的意思）', () => {
    // `empPayIdTemplet` 按 `settled_at is not None` 判，所以这个字段**保留原值**；
    // 格式化失败只影响「结账时间」那一格显示成空（`formatDateTime` 的既有口径）。
    const row = toRow({ ...ROW, settled_at: '不是时间' })
    expect(row.settingTime).toBe('')
    expect(row.settled_at).toBe('不是时间')
  })

  it('⭐ 这一列**不再**受 `review_status` 影响（D-0071 ③/④）', () => {
    expect(toRow({ ...ROW, review_status: '已通过' }).orderSta).toBe('代练中')
    expect(toRow({ ...ROW, review_status: null, order_status: 'COMPLETED' }).orderSta).toBe('已完成')
  })

  it('未知枚举值**原样显示**（不崩、不空白——一眼能看出漂移）', () => {
    expect(toRow({ ...ROW, order_status: 'SOMETHING_NEW' }).orderSta).toBe('SOMETHING_NEW')
  })

  it('没有接单人 / 还没审核的行不崩（`username`/`review_status` 都是 null）', () => {
    const row = toRow({ ...ROW, user: null, username: null, review_status: null })
    expect(row.userName).toBe('')
    expect(row.orderSta).toBe('代练中')
    // `order_status` 缺失（老后端/夹具）⇒ 空串，不崩
    expect(toRow({ ...ROW, order_status: '' }).orderSta).toBe('')
  })

  it('formatDateTime：ISO 带时区 → 对标的 `Y-m-d H:i`；取不出来回空串', () => {
    expect(formatDateTime('2026-09-20T21:32:15+08:00')).toBe('2026-09-20 21:32')
    expect(formatDateTime('')).toBe('')
    expect(formatDateTime(null)).toBe('')
  })
})

describe('#total 文案（同一份骨架，数字换成本次筛选的真值）', () => {
  /** 六项合计（**每一项都取不同的值**：哪一槽接错了会被字面值对上号）。 */
  const SUMMARY = {
    temp_amount: '88.00',
    bonus_amount: '12.34',
    order_price: '120.00',
    refund_fee: '5.00',
    profit_expected: '27.00',
    profit_actual: '102.66',
  }

  it('⭐ 给定六项 ⇒ **逐槽**对拍（含两个差额利润取的是**不同**的真值）', () => {
    expect(totalText(3, SUMMARY)).toBe(
      '本次筛选条件下共计3条记录，总计【预计绩效88.00元，生效绩效12.34元，' +
        '订单金额120.00元，记录退款金额5.00，预计差额利润27.00元，' +
        '已生效差额利润102.66】，默认显示最近3个月数据，若需扩大时间范围，可手动选择起始日期',
    )
  })

  it('⭐ 两个差额利润槽**不是同一个值**（对标的笔误不照抄：`profit_expected` / `profit_actual` 分开取）', () => {
    // ⚠️ 这条是本轮的**判别用例**：若实现把两个槽都填 `profit_expected`（照抄对标的 `profit1` 笔误），
    //    下面这句就会红。两个值刻意不同（`27.00` vs `102.66`）。
    const text = totalText(1, SUMMARY)
    expect(text).toContain('预计差额利润27.00元')
    expect(text).toContain('已生效差额利润102.66】')
    expect(text).not.toContain('已生效差额利润27.00')
  })

  /**
   * `.tools/parity.mjs`（L356）的 `tmpl()` —— **逐字相同**的实现：把每一段数字
   * （`\d` 开头、后跟数字或逗号）换成 `#`，再把连续空白折成一个空格，最后 `trim()`。
   * 原文的那一行就是下面这个函数体（连正则字符集一起抄的，没有自己改）。
   *
   * ⚠️ **判别力就在这个正则不吞小数点**：`0.00` → `#.#`、裸 `0` → `#`（`.` 原样留下）。
   * 自写的 mask 若把 `0.00` 与 `0` **都**归一成 `#`，真 `tmpl` 判「不等」、那个 mask 判「相等」，
   * 自证是**假的**——所以这里用真实现，并按真规则给出**正确的期望**（差异只在退款那一槽）。
   */
  const tmpl = (s: string): string => s.replace(/\d[\d,]*/g, '#').replace(/\s+/g, ' ').trim()

  /** 把「记录退款金额」那一槽折成同一形状——**只允许**差异出现在这一槽。 */
  const collapseRefundSlot = (s: string): string =>
    s.replace(/记录退款金额#[.#]*，/g, '记录退款金额#，')

  it('⭐ 与 `.tools/parity.mjs` 的 `tmpl` 逐字同一实现：退款槽两位小数 ⇒ 与骨架**不同形**，差异**只有**那一槽', () => {
    const ours = tmpl(totalText(3, SUMMARY))
    const bone = tmpl(TOTAL_TEXT)
    // ① 差异**真的存在**（这正是不等价 mask 的判别力缺口：它把两串判成了相等）
    expect(ours).not.toBe(bone)
    // ② 差异**只在**「记录退款金额」那一槽：把该槽折平后，其余 5 槽 + 骨架逐字同形
    expect(collapseRefundSlot(ours)).toBe(collapseRefundSlot(bone))
  })

  it('⭐ 退款槽的差异是**数据形状**（对标原样透传、没有 `toFixed`）而不是算法差异', () => {
    // 对标 `emplist.js:499`：`'记录退款金额' + res.total.TotalReFundAmount + '，'` —— 原样透传。
    // ⇒ 同一个 `tmpl` 判定下：后端若给**整数形**（`'5'`），产物与骨架**逐字同形**；
    expect(tmpl(totalText(3, { ...SUMMARY, refund_fee: '5' }))).toBe(tmpl(TOTAL_TEXT))
    // 而给两位小数（`'5.00'`，我们后端现在的形状）时，只多出那个小数点（`#` → `#.#`）。
    expect(tmpl(totalText(3, { ...SUMMARY, refund_fee: '5.00' }))).toBe(
      tmpl(TOTAL_TEXT).replace('记录退款金额#，', '记录退款金额#.#，'),
    )
  })

  it('⭐ 空集（后端回六项 `"0.00"`）⇒ 与骨架里的占位值逐字相同', () => {
    const zeros = {
      temp_amount: '0.00',
      bonus_amount: '0.00',
      order_price: '0.00',
      refund_fee: '0.00',
      profit_expected: '0.00',
      profit_actual: '0.00',
    }
    expect(totalText(0, zeros)).toBe(
      TOTAL_TEXT.replace('共计0条记录', '共计0条记录').replace(
        '记录退款金额0，',
        '记录退款金额0.00，',
      ),
    )
    // 也就是说：除「记录退款金额」那一槽由裸 `0` 变成了后端给的 `0.00` 之外，其余一字不变。
    expect(totalText(0, zeros).replace('记录退款金额0.00，', '记录退款金额0，')).toBe(TOTAL_TEXT)
  })

  it('⭐ `summary` 缺席（旧后端 / 没有该键）⇒ 六个槽回落到骨架里的占位值', () => {
    // 前后端分开部署时会经过这个窗口；回落是「显示骨架」而不是「编一个金额」。
    expect(totalText(3)).toBe(TOTAL_TEXT.replace('共计0条记录', '共计3条记录'))
    expect(totalText(3, null)).toBe(TOTAL_TEXT.replace('共计0条记录', '共计3条记录'))
  })

  it('⭐ 千分位：后端给的是**裸**两位小数字符串 ⇒ 原样填（⛔ 不加分隔符，否则形状会变）', () => {
    // 判据：`parity` 的模板归一里 `#.#` 与 `#` 是**两种**形状 ⇒ 自己加千分位会把
    // 「预计绩效」那一槽从 `#.#` 压成 `#`。
    expect(totalText(1, { ...SUMMARY, temp_amount: '1234.56' })).toContain('预计绩效1234.56元')
    expect(totalText(1, { ...SUMMARY, temp_amount: '1234.56' })).not.toContain('1,234.56')
  })

  it('非数字兜底成 0（不让 NaN 进界面）', () => {
    expect(totalText(Number.NaN)).toContain('共计0条记录')
  })

  it('工具栏文案逐条对齐证据件（未知 event 回退成 event 本身）', () => {
    expect(toolbarLabel('refresh')).toBe('刷新列表')
    expect(toolbarLabel('delEmpBtn')).toBe('删除记录')
    expect(toolbarLabel('exportBtn')).toBe('导出记录')
    expect(toolbarLabel('payBtn')).toBe('结算并直接打款')
    expect(toolbarLabel(undefined)).toBe('该功能')
    expect(toolbarLabel('mystery')).toBe('mystery')
  })

  it('⭐ brief §4① 删掉的 4 颗按钮：**证据件里不存在**、`TOOLBAR_LABEL` 里也没有', () => {
    for (const event of REMOVED_TOOLBAR_EVENTS) {
      // 证据件（真文件）里没有这颗按钮……
      expect(TOOLBAR_HTML).not.toContain(`lay-event="${event}"`)
      // ……文案表里也没有它的条目（否则「点了提示尚未开放」的兜底会给一颗屏幕上没有的按钮编文案）
      expect(event in TOOLBAR_LABEL).toBe(false)
      // 兜底的 `toolbarLabel` 只会回退成 event 本身（= 有事件名也不用它）
      expect(toolbarLabel(event)).toBe(event)
    }
    // 阴性对照：删剩的 7 颗**都在**证据件里（证明上一条不是因为证据件取空了）
    const kept = TOOLBAR_HTML?.match(/lay-event="([^"]+)"/g) ?? []
    expect(kept).toHaveLength(7)
    expect(kept.join('|')).toContain('lay-event="refresh"')
    expect(kept.join('|')).toContain('lay-event="delEmpBtn"')
  })

  it('⭐ 证据件里那颗「仅修改结算状态」还带着对标的 `lay-tips` 原文', () => {
    expect(TOOLBAR_HTML).toContain('仅对勾选的记录进行结算状态修改')
  })
})

describe('「订单状态」列：彩色标签（templet **逐字**照抄 `emplist.js` L351-377）', () => {
  it('待验收：`#ff6a00` + title「当前订单状态：待验收」+ `data-id` 是**订单号**', () => {
    expect(orderStatusTagHtml(toRow({ ...ROW, order_status: 'PENDING_REVIEW' }))).toBe(
      '<a href="javascript:void(0)" class="lableb setdlsta" data-id="20260920213215233029"' +
        ' title="当前订单状态：待验收" style="background-color:#ff6a00;">待验收</a>',
    )
  })

  it('已完成：`#0094ff`（第二个状态，证明取色真的按状态分支）', () => {
    expect(orderStatusTagHtml(toRow({ ...ROW, order_status: 'COMPLETED' }))).toBe(
      '<a href="javascript:void(0)" class="lableb setdlsta" data-id="20260920213215233029"' +
        ' title="当前订单状态：已完成" style="background-color:#0094ff;">已完成</a>',
    )
  })

  it('⭐ 未知状态：原样显示 + 兜底色 `#b0aeae;`（**照抄对标源码里那个多余分号** ⇒ 渲染成 `;;`）', () => {
    expect(orderStatusTagHtml(toRow({ ...ROW, order_status: 'SOMETHING_NEW' }))).toBe(
      '<a href="javascript:void(0)" class="lableb setdlsta" data-id="20260920213215233029"' +
        ' title="当前订单状态：SOMETHING_NEW" style="background-color:#b0aeae;;">SOMETHING_NEW</a>',
    )
    expect(ORDER_STATUS_DEFAULT_COLOR).toBe('#b0aeae;')
  })

  it('7 个已知状态的色值逐条取自对标（`emplist.js` L352-375）', () => {
    expect(ORDER_STATUS_COLORS).toEqual({
      待上号: '#585858',
      代练中: '#219227',
      待验收: '#ff6a00',
      已完成: '#0094ff',
      已暂停: '#FF5722',
      问题单: '#ff0000',
      已退单: '#efaaaa',
    })
  })

  it('⭐ 状态值 / 订单号里的 HTML **必须被转义**（存储型 XSS 回归：`label` 与 `orderId` 三处出口）', () => {
    // 未知枚举值原样返回（`orderStatusLabel` 的既有口径），
    // `label` / `orderId` 直接插进 `title` / `data-id` / 正文就能长出可执行的
    // `<img src=x onerror=…>`。下面既断字面产物、也用真 DOM 复验一次。
    const EVIL_STATUS = '<img src=x onerror=alert(1)>'
    const EVIL_ORDER = '"><img src=x onerror=alert(2)>'
    const html = orderStatusTagHtml(
      toRow({ ...ROW, order_status: EVIL_STATUS, order_no: EVIL_ORDER }),
    )
    // ① 产物里**没有任何可执行元素**（没有真 `<img`、没有能逃出属性的 `onerror=`）
    expect(html).not.toContain('<img')
    expect(html).not.toContain('&quot;><img')
    // ② 断**转义后的实体**（`<`/`>`/`"` 都成了实体），逐字钉产物
    expect(html).toBe(
      '<a href="javascript:void(0)" class="lableb setdlsta"' +
        ' data-id="&quot;&gt;&lt;img src=x onerror=alert(2)&gt;"' +
        ' title="当前订单状态：&lt;img src=x onerror=alert(1)&gt;"' +
        ' style="background-color:#b0aeae;;">&lt;img src=x onerror=alert(1)&gt;</a>',
    )
    // ③ 真 DOM 复验：插进去之后**没有** `img` 元素；正文是**文本**而不是被解析出的标签
    const wrap = document.createElement('div')
    wrap.innerHTML = html
    expect(wrap.querySelector('img')).toBeNull()
    const tag = wrap.querySelector<HTMLElement>('a.setdlsta')!
    expect(tag.textContent).toBe(EVIL_STATUS)
    expect(tag.getAttribute('data-id')).toBe(EVIL_ORDER)
  })

  it('⭐ 正对照：正常状态值的产物**逐字不变**（转义没有多转、也没有改变骨架）', () => {
    // 与上面那条成对：XSS 用例若被「把产物整个掐掉」这种假修复骗过，这条会红。
    expect(orderStatusTagHtml(toRow({ ...ROW, order_status: 'COMPLETED' }))).toBe(
      '<a href="javascript:void(0)" class="lableb setdlsta" data-id="20260920213215233029"' +
        ' title="当前订单状态：已完成" style="background-color:#0094ff;">已完成</a>',
    )
    expect(orderStatusTagHtml(toRow({ ...ROW, order_status: 'PENDING_REVIEW' }))).toBe(
      '<a href="javascript:void(0)" class="lableb setdlsta" data-id="20260920213215233029"' +
        ' title="当前订单状态：待验收" style="background-color:#ff6a00;">待验收</a>',
    )
  })

  it('反查：中文 label → 枚举名（复用 `@/config/orderStatus`，不另建表）', () => {
    expect(orderStatusOfFilterValue('待抢单')).toBe('WAITING_GRAB')
    expect(orderStatusOfFilterValue('已完成')).toBe('COMPLETED')
    expect(orderStatusOfFilterValue('待上号')).toBeUndefined()
    expect(orderStatusOfFilterValue('已完成,问题单')).toBeUndefined()
  })

  it('⭐ 20 列里 **17 列**用函数 templet（T3 补齐 15 列 + 修复轮给 `orderId` 加蓝色 + 图片存储线给 `userName` 加收款码入口）；各列的产物在下面两个 describe 里逐条钉', () => {
    expect(
      COLS.filter((c) => typeof c.templet === 'function').map((c) => c.field),
    ).toEqual([
      // ⭐ 图片存储线：「系统用户」那一格改函数 templet ⇒ 可点开该行人员的收款码。
      'userName',
      'tempAmount',
      'bonusAmount',
      'deductFee',
      'price',
      'refundFee',
      'empType',
      // ⭐（只为一个 `<span style="color:#0094ff">`，**不是**为了可点）
      'orderId',
      'gameRole',
      'dlProject',
      'orderSta',
      'wangwang',
      'createTime',
      'logUserName',
      'settingSta',
      'settingTime',
      'empPayId',
    ])
    expect(COLS).toHaveLength(20)
  })
})

/* ──────────── T3：单元格 templet（逐条钉渲染产物，出处 `emplist.js` 的产物） ──────────── */

/** 一行拿来渲染 templet 的行载荷（先过 `toRow`，与页面里同源）。 */
function row(over: Partial<PerformanceListItem> = {}): PerformanceRow {
  return toRow({ ...ROW, ...over })
}

describe('T3 templet：`tempAmount` / `bonusAmount`（`emplist.js` L242-259）', () => {
  it('⚠️ 列定义本身就是函数 templet（两列都是）', () => {
    expect(
      COLS.filter((c) => typeof c.templet === 'function').map((c) => c.field),
    ).toContain('tempAmount')
    expect(
      COLS.filter((c) => typeof c.templet === 'function').map((c) => c.field),
    ).toContain('bonusAmount')
  })

  it('非 20（打手）：`javascript:void(0);`（**带分号**）+ `#FF5722` + 对标那句 tips', () => {
    expect(performanceAmountTemplet(row(), 'tempAmount')).toBe(
      '<a href="javascript:void(0);" style="color:#FF5722"' +
        ' lay-tips="发给dailian_A时的预计发单价，结算时请以生效绩效为准，点击可以修改">88.00</a>',
    )
  })

  it('`bonusAmount` 是另一句（「最终需要给…结算的金额，如果…没亲自交单，这里就是0」）', () => {
    expect(performanceAmountTemplet(row(), 'bonusAmount')).toBe(
      '<a href="javascript:void(0);" style="color:#FF5722"' +
        ' lay-tips="最终需要给dailian_A结算的金额，如果dailian_A没亲自交单，这里就是0，点击可以修改">0.00</a>',
    )
  })

  /**
   * ⚠️ **对标的原文有个反直觉之处，照抄了**：那段「按固定金额提成 / 按接单价百分比提成 /
   * 按利润百分比提成」的 `empRemark` 只在 **`empType == 20`（销售客服）** 那一支被拼进 tips；
   * 非 20 的整句里**根本没有它**（`emplist.js` L243-250：变量算了、但那一支的字符串没拼它）。
   * ⇒ 下面的判据就是钉这个：**默认支不含**、销售支按三态变。
   */
  it('⭐ tips 三态（`logUserName` 含「固定」/ 含「接单价」/ 都不是）：**只在 `empType==20` 那一支**出现', () => {
    const salesTips = (logUserName: string): string =>
      performanceAmountTemplet(
        row({ log_user_name: logUserName, emp_type: '销售提成' }),
        'tempAmount',
      )
    expect(salesTips('张三固定')).toContain('销售客服的提成账单，按固定金额提成')
    expect(salesTips('按接单价结算')).toContain('销售客服的提成账单，按接单价百分比提成')
    expect(salesTips('普通记录者')).toContain(
      '销售客服的提成账单，按利润百分比提成(退款部分已计算在内)',
    )
    // 默认（打手）支**不含**那三句里的任何一句——照对标原文（变量算了但没拼）
    const workerTips = performanceAmountTemplet(row({ log_user_name: '张三固定' }), 'tempAmount')
    expect(workerTips).not.toContain('按固定金额提成')
    expect(workerTips).not.toContain('提成')
  })

  it('⭐ `empType == 20`（销售客服）⇒ tips 换成「销售客服的提成账单，…」那一支', () => {
    // 两列都换；且这一支**没有**「点击可以修改」，也**没有**亏损追加（照对标原文）。
    expect(performanceAmountTemplet(row({ emp_type: '销售提成' }), 'tempAmount')).toBe(
      '<a href="javascript:void(0);" style="color:#FF5722"' +
        ' lay-tips="销售客服的提成账单，按利润百分比提成(退款部分已计算在内)">88.00</a>',
    )
    expect(performanceAmountTemplet(row({ emp_type: '销售提成' }), 'bonusAmount')).toBe(
      '<a href="javascript:void(0);" style="color:#FF5722"' +
        ' lay-tips="销售客服的提成账单，按利润百分比提成(退款部分已计算在内)">0.00</a>',
    )
  })

  it('⭐ 亏损判据 `(price − tempAmount − refundFee) <= 0` ⇒ 加粗 + 14px + 追加「，这单貌似是亏损的」', () => {
    // ROW: 120.00 − 88.00 − 20.00 = 12.00 > 0 ⇒ 不亏损
    expect(performanceAmountTemplet(row(), 'tempAmount')).not.toContain('font-weight:bold')
    expect(performanceAmountTemplet(row(), 'tempAmount')).not.toContain('这单貌似是亏损的')
    // 退款拉到 40 ⇒ 120 − 88 − 40 = −8 ⇒ 亏损
    const loss = performanceAmountTemplet(row({ refund_fee: '40.00' }), 'tempAmount')
    expect(loss).toContain('style="color:#FF5722;font-weight:bold;font-size:14px"')
    expect(loss).toContain('点击可以修改，这单貌似是亏损的')
  })

  it('⭐ 边界：差额**恰好等于 0** 也算亏损（对标的 `<= 0`）', () => {
    expect(performanceAmountTemplet(row({ refund_fee: '32.00' }), 'tempAmount')).toContain(
      ';font-weight:bold;font-size:14px',
    )
  })

  it('⭐ 亏损时 `empType==20` 那一支**不追加**那句（照对标原文的分支结构）', () => {
    const lossSales = performanceAmountTemplet(
      row({ emp_type: '销售提成', refund_fee: '40.00' }),
      'tempAmount',
    )
    expect(lossSales).toContain(';font-weight:bold;font-size:14px')
    expect(lossSales).not.toContain('这单貌似是亏损的')
  })
})

describe('T3 templet：其余 9 列（逐条照 `emplist.js` 的产物）', () => {
  it('⭐ `price`（订单金额）：有接单实收款 ⇒ 绿底 `#98FB98` + 「收」，**色块在前、红字金额在后**', () => {
    // 颜色 / 单字 / `.lableb` / 顺序**照抄对标** `emplist.js:265-281`（`return vo + pvo`）。
    const html = priceTemplet(row())
    expect(html).toBe(
      '<span class="lableb" style="background-color:#98FB98"' +
        ' title="本状态按「接单实收款」判定：订单已录入接单实收款（有值且不为 0）⇒ 收；要改请到订单管理里改价">收</span>' +
        '<span style="color:#FF5722">120.00</span>',
    )
    // 顺序判据（不只看子串）：色块那一段在红字金额那一段**之前**，且金额那一段是**结尾**。
    expect(html.indexOf('class="lableb"')).toBeLessThan(html.indexOf('#FF5722'))
    expect(html.endsWith('<span style="color:#FF5722">120.00</span>')).toBe(true)
  })

  it('⭐ `net_receipts=null` ⇒ 浅紫 `#E6E6FA` + 「未」（订单没录入实收）', () => {
    const html = priceTemplet(row({ net_receipts: null }))
    expect(html).toBe(
      '<span class="lableb" style="background-color:#E6E6FA"' +
        ' title="本状态按「接单实收款」判定：订单未录入接单实收款（或为 0）⇒ 未；要改请到订单管理里改价">未</span>' +
        '<span style="color:#FF5722">120.00</span>',
    )
    expect(html).not.toContain('#98FB98')
    expect(html).not.toContain('>收<')
  })

  it('⭐ E-0021 边界：`net_receipts=\'0.00\'` ⇒ **「未」**（金额 0 不能走真值判）', () => {
    // ⛔ 这条专门杀 `if (net)` 真值判：`Boolean('0.00') === true`，真值判会把它误判成「收」。
    const html = priceTemplet(row({ net_receipts: '0.00' }))
    expect(html).toContain('background-color:#E6E6FA')
    expect(html).toContain('>未<')
    expect(html).not.toContain('#98FB98')
    expect(html).not.toContain('>收<')
  })

  it('⭐ 负数接单实收款（录了负数）⇒ **「收」**（有值且 ≠ 0，刻意如此；D-0015）', () => {
    const html = priceTemplet(row({ net_receipts: '-5.00' }))
    expect(html).toContain('background-color:#98FB98')
    expect(html).toContain('>收<')
  })

  it('⭐ `title` 说的是**我们的**判据（接单实收款），⛔ 不含对标那句「不是给打手的钱」', () => {
    for (const net of ['100.00', null, '0.00']) {
      const html = priceTemplet(row({ net_receipts: net }))
      // 判据说明必须出现（一句话说清怎么判 + 怎么改）
      expect(html).toContain('接单实收款')
      expect(html).toContain('要改请到订单管理里改价')
      // 反向断言：对标那个色块讲的是「与老板/上家结算」，一个字都不许抄过来。
      expect(html).not.toContain('不是给打手的钱')
      expect(html).not.toContain('老板')
      expect(html).not.toContain('上家')
      expect(html).not.toContain('结算中')
      // 「中」我们不可达 ⇒ 连那个颜色都不该出现（不造恒假分支）
      expect(html).not.toContain('#B0E0E6')
      expect(html).not.toContain('>中<')
    }
  })

  it('⭐ 真 DOM：色块是 `span.lableb`（带 `background-color`），紧邻的下一个元素才是红字金额', () => {
    const wrap = document.createElement('div')
    wrap.innerHTML = priceTemplet(row())
    const badge = wrap.querySelector<HTMLElement>('span.lableb')
    expect(badge).not.toBeNull()
    expect(badge!.textContent).toBe('收')
    expect(badge!.getAttribute('style')).toContain('background-color:#98FB98')
    expect(badge!.getAttribute('title')).toContain('接单实收款')
    // 「色块在前、金额在后」在真 DOM 里也成立：色块的 nextElementSibling 就是金额那个 span。
    const amount = badge!.nextElementSibling as HTMLElement | null
    expect(amount?.tagName).toBe('SPAN')
    expect(amount?.getAttribute('style')).toContain('color:#FF5722')
    expect(amount?.textContent).toBe('120.00')
  })

  it('`refundFee`：>0 ⇒ 红字 + `title`；=0 ⇒ `color:#999` + 「没有退款」', () => {
    expect(refundFeeTemplet(row())).toBe(
      '<a href="javascript:void(0)" class="refundFee" data-id="20260920213215233029"' +
        ' data-fee="20.00" style="color:red" lay-tips="退款金额：20.00元">20.00</a>',
    )
    expect(refundFeeTemplet(row({ refund_fee: '0.00' }))).toBe(
      '<a href="javascript:void(0)" class="refundFee" data-id="20260920213215233029"' +
        ' data-fee="0.00" style="color:#999" lay-tips="没有退款">0.00</a>',
    )
  })

  it('`empType`：`打单工资` ⇒ 打手的；`销售提成` ⇒ 客服的（都是 `#5FB878`）；其余原值 + `#2F4056`', () => {
    expect(empTypeTemplet(row())).toBe('<span style="color:#5FB878">打手的</span>')
    expect(empTypeTemplet(row({ emp_type: '销售提成' }))).toBe(
      '<span style="color:#5FB878">客服的</span>',
    )
    expect(empTypeTemplet(row({ emp_type: '别的属性' }))).toBe(
      '<span style="color:#2F4056">别的属性</span>',
    )
  })

  it('`gameRole` / `dlProject` / `createTime` / `settingTime`：`<span>` 包裹（角色名带空 title）', () => {
    expect(gameRoleTemplet(row())).toBe('<span title="">旅行者</span>')
    expect(dlProjectTemplet(row())).toBe('<span>深渊12层</span>')
    expect(spanTemplet(row().createTime)).toBe('<span>2026-09-20 21:32</span>')
    expect(spanTemplet(row().settingTime)).toBe('<span>—</span>')
  })

  it('`wangwang`：非空 ⇒ 阿里旺旺对话链接（URL 逐字照对标）；空 ⇒ 空串', () => {
    expect(wangwangTemplet(row())).toBe(
      '<a href="http://amos.alicdn.com/getcid.aw?v=2&uid=ww_A&site=cntaobao&s=2&groupid=0&charset=utf-8"' +
        ' target="_blank" title="点击可以与买家对话">ww_A</a>',
    )
    expect(wangwangTemplet(row({ wangwang: '' }))).toBe('')
    expect(wangwangTemplet(row({ wangwang: null as unknown as string }))).toBe('')
  })

  it('`logUserName`：`<span lay-tips="由<X>记录此条账单(也是发单人)">`（名字两处都出现）', () => {
    expect(logUserNameTemplet(row())).toBe(
      '<span lay-tips="由kefu_A记录此条账单(也是发单人)">kefu_A</span>',
    )
  })

  it('⭐ `deductFee`：红字金额 + `title` 写清口径（方式 + 参数值）；`null` ⇒ `—` **不是** `0.00`', () => {
    expect(deductFeeTemplet(row({ deduct_mode: '0', deduct_value: '5' }))).toBe(
      '<span style="color:#ff5722" title="扣点：按比例 5">4.40</span>',
    )
    expect(deductFeeTemplet(row({ deduct_mode: '1', deduct_value: '3.00' }))).toBe(
      '<span style="color:#ff5722" title="扣点：固定金额 3.00">4.40</span>',
    )
    expect(deductFeeTemplet(row({ deduct_mode: '', deduct_value: '0.00' }))).toBe(
      '<span style="color:#ff5722" title="扣点：未设置扣点">4.40</span>',
    )
    // 扣点越界 ⇒ 后端回 `null` ⇒ `—`（后端契约：不给假数、不藏行）
    // ⚠️ `title` 写的是**扣点口径**（方式 + 参数值），与那一格有没有数**无关**；
    //    「这一格没有数」这件事由 `—` 自己表达（值不是数字、一眼看得出来）。
    expect(deductFeeTemplet(row({ deduct_fee: null, deduct_mode: '0', deduct_value: '5' }))).toBe(
      '<span style="color:#ff5722" title="扣点：按比例 5">—</span>',
    )
  })

  it('⭐ `empPayId`（结账方式）：`settled_at` 有值 ⇒ 蓝底 `lableb`「人工结账」；没值 ⇒ **同一个蓝底空 span**', () => {
    // ⚠️ 未结账**不回空串**：照对标 `emplist.js:463`
    //    渲染那个**空蓝框**（对标两支都渲染 span，未结账那支只是内容为空）。
    expect(empPayIdTemplet(row())).toBe('<span class="lableb" style="background-color:#0094ff"></span>')
    expect(empPayIdTemplet(row({ settled_at: '2026-09-21T10:05:00+08:00' }))).toBe(
      '<span class="lableb" style="background-color:#0094ff">人工结账</span>',
    )
  })

  it('⭐ `empPayId` 的判据是 `settled_at`（**不是** `settle_status`）：两者不一致时按 `settled_at`', () => {
    // 判据只有一个出处（E-0021 那一族：同一事实不要两套判据）。
    expect(empPayIdTemplet(row({ settle_status: '已结账', settled_at: null }))).toBe(
      '<span class="lableb" style="background-color:#0094ff"></span>',
    )
    expect(
      empPayIdTemplet(row({ settle_status: '未结账', settled_at: '2026-09-21T10:05:00+08:00' })),
    ).toContain('人工结账')
  })

  it('⭐ 「关联订单」列：可点蓝链（**属性集合与取值**照对标；⚠️ 属性书写顺序与对标相反），单号仍转义', () => {
    const html = orderIdTemplet(row())
    // 这一列**可点**（弹层已实装，见「已知会不同」第 23/29 条的更正与第 34 条）⇒
    // 断言的是**现在**的形状：对标的 `<a class="orderdetails" … title="点击查看订单详情">`。
    // ⛔ 期望值是**字面值**，不引用生产常量（E-0024）。
    // 📌 **正文 = 来源单号 `TB-合成-绩效-来源甲`**、`data-id` = 系统单号
    // `20260920213215233029`（对标那一格的表达式就是**这两个字段**：正文取来源单号、
    // `data-id` 取系统单号）。
    // ⚠️ 正文与 `data-id` **故意不同** ⇒ 「实现把两个字段写成一个」这种错法会红。
    expect(html).toBe(
      '<a class="orderdetails" href="javascript:void(0);" data-id="20260920213215233029"' +
        ' data-orderIdView="TB-合成-绩效-来源甲"' +
        ' style="color:#0094ff" title="点击查看订单详情">TB-合成-绩效-来源甲</a>',
    )
    // 列定义确实挂了这个函数 templet（否则蓝链不会渲染）
    expect(COLS.find((c) => c.field === 'orderId')?.templet).toBeTypeOf('function')
    // ⭐ **行映射真的有 `sourceOrderNo` 这个键**（⛔ 不是断言 templet 内部实现）：
    expect(row().sourceOrderNo).toBe('TB-合成-绩效-来源甲')
    expect(row().orderId).toBe('20260920213215233029')
    // 两处都是请求数据 ⇒ **都要**转义（与 `orderStatusTagHtml` 的 `data-id` 同一出口）。
    // ⛔ 这一条**不变**（可点不等于可以不过转义）：正文与 `data-id` 两处都要是转义后的实体。
    // ⚠️ 同一次调用里两个字段**都**放恶意串 ⇒ 漏掉任何一处转义本条都会红。
    expect(
      orderIdTemplet(row({ order_no: '<b>x</b>', source_order_no: '<b>x</b>' })),
    ).toBe(
      '<a class="orderdetails" href="javascript:void(0);" data-id="&lt;b&gt;x&lt;/b&gt;"' +
        ' data-orderIdView="&lt;b&gt;x&lt;/b&gt;"' +
        ' style="color:#0094ff" title="点击查看订单详情">&lt;b&gt;x&lt;/b&gt;</a>',
    )
  })

  it('⭐ D-0301（2026-10-24 覆盖 2026-10-09 旧裁定）：**来源单号为空 ⇒ 正文回退内置单号**（不出空链接）', () => {
    // `Order.source_order_no` 是 `blank=True` 的**非 NULL** 文本列 ⇒ 真实的「没填」就是空串。
    // D-0301 裁定 2：绩效/结算页「关联订单」列来源号空时**回退显示
    // 内置单号**、不再显示空。
    const html = orderIdTemplet(row({ source_order_no: '' }))
    expect(html).toBe(
      '<a class="orderdetails" href="javascript:void(0);" data-id="20260920213215233029"' +
        ' data-orderIdView="20260920213215233029"' +
        ' style="color:#0094ff" title="点击查看订单详情">20260920213215233029</a>',
    )
    // ⛔ 也不许照抄对标 `res.jionOrder` 在字段缺失时拼出字面量 `null` / `undefined` 的产物。
    expect(html).not.toContain('null')
    expect(html).not.toContain('undefined')
    // `data-id` 与 `data-orderIdView` 都是系统单号 ⇒ `<a>` 照样点得开订单详情。
    const wrap = document.createElement('div')
    wrap.innerHTML = html
    expect(wrap.querySelector<HTMLElement>('a.orderdetails')?.dataset.id).toBe(
      '20260920213215233029',
    )
    expect(wrap.querySelector<HTMLElement>('a.orderdetails')?.dataset.orderidview).toBe(
      '20260920213215233029',
    )
  })

  it('⚠️ 「关联订单」列的 `data-id` 仍必须是**系统单号**（那条 `/^\\d{20}$/` 形状闸打的是它）', () => {
    // 开层前那道闸（`onOrderDetailClick`）与开层路由都只认 20 位系统单号
    // ⇒ `data-id` 一旦跟着正文改成来源单号，**每一次点击**都会掉进「格式不正确」提示。
    const html = orderIdTemplet(row())
    const wrap = document.createElement('div')
    wrap.innerHTML = html
    const dataId = wrap.querySelector<HTMLElement>('a.orderdetails')?.dataset.id ?? ''
    expect(dataId).toBe('20260920213215233029')
    expect(/^\d{20}$/.test(dataId)).toBe(true)
    // 反向对照：换来源单号**不该**动到 `data-id`。
    expect(
      orderIdTemplet(row({ source_order_no: 'TB-合成-绩效-来源乙' })),
    ).toContain('data-id="20260920213215233029"')
  })

  it('⭐ 2026-09-22 已删列（完成时间 / 是否超时）**整列不在 `COLS`**，且 `toRow` 也不给它们键', () => {
    // 原句「接不了 ⇒ 留空、没有 templet」已按业务方裁定作废：现在**连列对象都不在**
    // （判据更强：不在 `COLS` ⇒ 表头与单元格都不会渲染）。
    const fields = COLS.map((c) => c.field)
    for (const field of ['completeTime', 'exceedHours']) {
      expect(fields).not.toContain(field)
    }
    const r = toRow(ROW)
    expect('completeTime' in r).toBe(false)
    expect('exceedHours' in r).toBe(false)
  })

  it('⚠️ 文本类字段里的 HTML 会被转义（存储型 XSS：角色名来自用户输入）', () => {
    const evil = row({ game_role: '<img src=x onerror=alert(1)>' })
    expect(gameRoleTemplet(evil)).toBe('<span title="">&lt;img src=x onerror=alert(1)&gt;</span>')
  })
})

/* ────────── ⭐ 收款码入口：**无对标证据**、形态沿用本页既有行内入口 ────────── */

describe('⭐ 收款码入口：`userName` 那一格的可点链（**无对标证据**，形态照 `.orderdetails`）', () => {
  it('templet 产物：`class` / `data-id`（绩效行主键）/ `title` / 蓝字正文逐字', () => {
    // ⛔ 期望值是**字面值**，不引用生产常量（E-0024：钉值，不钉常量）。
    expect(userNameTemplet(toRow(ROW))).toBe(
      '<a class="viewWechatQr" href="javascript:void(0);" data-id="17"' +
        ' style="color:#0094ff" title="查看收款码">dailian_A</a>',
    )
  })

  it('⭐ 2026-11-07 R3：`negative_debt` 行 ⇒ 用户名旁追加红签「挂负账」（`.lableb` + #ff0000，字面钉值）', () => {
    const html = userNameTemplet(toRow({ ...ROW, negative_debt: true }))
    expect(html).toContain('class="lableb negDebtMark"')
    expect(html).toContain('style="background-color:#ff0000;margin-left:2px;"')
    expect(html).toContain('>挂负账</span>')
    // 默认（键缺席 / false / undefined）一律不渲染（fail-closed）。
    expect(userNameTemplet(toRow(ROW))).not.toContain('挂负账')
    expect(userNameTemplet(toRow({ ...ROW, negative_debt: false }))).not.toContain('挂负账')
  })

  it('列定义确实挂了函数 templet（否则点不到）；**列数仍是 20**（不新增列）', () => {
    expect(COLS.find((c) => c.field === 'userName')?.templet).toBeTypeOf('function')
    expect(COLS).toHaveLength(20)
  })

  it('用户名里的 HTML 被转义（与 `.orderdetails` 同一出口：请求数据进 HTML 上下文）', () => {
    const evil = toRow({ ...ROW, username: '<img src=x onerror=alert(1)>' })
    expect(userNameTemplet(evil)).toBe(
      '<a class="viewWechatQr" href="javascript:void(0);" data-id="17"' +
        ' style="color:#0094ff" title="查看收款码">&lt;img src=x onerror=alert(1)&gt;</a>',
    )
  })

  it('`toRow` 认后端只读键 `wechat_qr_url`：有值原样带上、没有 / `null` ⇒ `null`', () => {
    // ⚠️ `ROW` 本身没有这个键（形状照 `PerformanceListItem` 的老夹具）⇒ 缺键必须是 `null`，
    //    不是 `undefined`（判据一律 `is not None` 那一族，E-0021）。
    expect(toRow(ROW).wechatQrUrl).toBeNull()
    expect(toRow({ ...ROW, wechat_qr_url: null }).wechatQrUrl).toBeNull()
    expect(toRow({ ...ROW, wechat_qr_url: 'https://cos.example.com/qr.png?sign=a' }).wechatQrUrl).toBe(
      'https://cos.example.com/qr.png?sign=a',
    )
  })
})

/* ────────────────────────── 挂载整页：列表取数与两个动作 ────────────────────────── */

/** 页面里**可见的文本**（剥掉 `<style>`/`<script>`/注释——证据件的注释是刻意保留的登记处）。 */
function visibleText(root: HTMLElement): string {
  const clone = root.cloneNode(true) as HTMLElement
  for (const el of Array.from(clone.querySelectorAll('style, script'))) el.remove()
  const walker = document.createTreeWalker(clone, 128 /* NodeFilter.SHOW_COMMENT */)
  const comments: Comment[] = []
  while (walker.nextNode()) comments.push(walker.currentNode as Comment)
  for (const c of comments) c.remove()
  return clone.textContent ?? ''
}

async function mountPage(): Promise<HTMLElement> {
  const root = document.createElement('div')
  document.body.appendChild(root)
  const { default: page } = await import('../emp-emplist')
  await page.mount(root, { onNavigate: vi.fn() } as never)
  return root
}

type RenderOpts = {
  done?: (r: { count: number; data: unknown[] }, curr: number, count: number) => void
}

/**
 * 模拟「layui 的 ajax 回来了」：把 `table.render` 的 `done` 回调按真 layui 的实参形状调一次
 * （`(res, curr, count)`）。挂在 `done` 里的首屏取数只有这样才会发生。
 */
function renderAndFinish(count = 0): void {
  const opts = LAYUI.table.render.mock.calls[0]![0] as RenderOpts
  opts.done?.({ count, data: [] }, 1, count)
}

describe('主表取数（GET /api/business/performances/）', () => {
  it('table.render 的 url / 方法 / 分页参数名 / 工具栏 / 列数都接在真端点上', async () => {
    await mountPage()
    expect(LAYUI.table.render).toHaveBeenCalledTimes(1)
    const opts = LAYUI.table.render.mock.calls[0]![0] as Record<string, unknown>
    expect(opts.elem).toBe(TABLE_ELEM)
    expect(opts.url).toBe(API_URL_FROM_MODULE)
    expect(opts.method).toBe('get')
    expect(opts.request).toEqual({ pageName: 'page', limitName: 'page_size' })
    expect(opts.toolbar).toBe(TOOLBAR_ELEM)
    expect(COLS).toHaveLength(20)
    expect(opts.cols).toEqual([COLS])
  })

  it('parseData 吃统一信封：total → count、items → 行（列字段已映射）', async () => {
    await mountPage()
    const opts = LAYUI.table.render.mock.calls[0]![0] as {
      parseData: (r: unknown) => { code: number; count: number; data: Record<string, unknown>[] }
    }
    const parsed = opts.parseData({ code: 0, msg: '', data: { items: [ROW], total: 7 } })
    expect(parsed.code).toBe(0)
    expect(parsed.count).toBe(7)
    expect(parsed.data[0]!.orderId).toBe(ROW.order_no)
    expect(parsed.data[0]!.userName).toBe('dailian_A')
  })

  it('挂载即按默认筛选取一次数（`table.reload` 带 emp_type、`date_type` 与日期两端，不带 page/page_size）', async () => {
    await mountPage()
    renderAndFinish()
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
    const [id, cfg] = LAYUI.table.reload.mock.calls[0]! as [string, { where: Record<string, unknown> }]
    expect(id).toBe(TABLE_ID)
    expect(cfg.where).toEqual({
      emp_type: '打单工资',
      // ⭐ 线 C：默认档（录单）也显式发 `date_type`，与 `toPerformanceSearch` 的用例同源。
      date_type: 'created',
      date_from: '2026-06-20 00:00:00',
      date_to: '2026-09-20 23:59:59',
    })
    expect('page' in cfg.where).toBe(false)
    expect('page_size' in cfg.where).toBe(false)
  })

  it('⭐⭐ 选「验收时间」+ 区间 ⇒ **真实出口**的请求里带 `date_type=completed`（列表与导出同源）', async () => {
    // ⚠️ 判据打在**真实出口**上，⛔ 不是页面内部变量：
    //    ① 列表那条链的出口是 `table.reload` 的 `where`（真 layui 拿它拼 XHR 的查询串）；
    //    ② 导出那条链的出口是被 mock 的**被查找模块** `@/api/performances::downloadPerformanceExport`。
    //    两条都断言，证明「映射真的进了请求」而不是只进了某个中间对象。
    // ⭐⭐ 期望值是 `completed`：验收那一档打订单的完单时间（见 `DATE_TYPE_MAP`）。
    await mountPage()
    // ⚠️ 用**持续**的 `mockReturnValue`（不是 `Once`）：下面「列表那一发」与「导出那一发」
    //    各自会调一次 `params()`，`Once` 只够第一次（第二次会拿到 `beforeEach` 的默认档，
    //    于是导出断言看到 `date_type=created` —— 本文件 2280 行记过同形的坑）。
    LAYUI.form.val.mockReturnValue({
      dateType: '40',
      sDate: '2026-06-20 00:00:00',
      eDate: '2026-09-20 23:59:59',
      settingSta: '',
    })
    renderAndFinish() // 首屏那一发先走完
    LAYUI.table.reload.mockClear()

    LAYUI.formHandlers.get('submit(seachBtn)')!()

    const cfg = LAYUI.table.reload.mock.calls[0]![1] as { where: Record<string, unknown> }
    expect(cfg.where.date_type).toBe('completed')
    expect(cfg.where.date_from).toBe('2026-06-20 00:00:00')
    expect(cfg.where.date_to).toBe('2026-09-20 23:59:59')
    // 同一份 `params()` 出口 ⇒ 导出拿到的必须是**同一个** `where`（含 `date_type`）
    LAYUI.handlers.get(`toolbar(${TABLE_ID})`)!({ event: 'exportBtn' } as never)
    await vi.waitFor(() => expect(mockedExport).toHaveBeenCalledTimes(1))
    expect(mockedExport).toHaveBeenCalledWith(cfg.where)
    expect(mockedExport.mock.calls[0]![0]).toMatchObject({ date_type: 'completed' })
    // 阴性对照：选「发单时间」⇒ **发 `date_type=published`**、且**一句提示都不弹**
    //    （发单档是**真接线**，与验收档同形）。
    LAYUI.table.reload.mockClear()
    LAYUI.msg.mockClear()
    LAYUI.form.val.mockReturnValue({
      dateType: '10',
      sDate: '2026-06-20 00:00:00',
      eDate: '2026-09-20 23:59:59',
      settingSta: '',
    })
    LAYUI.formHandlers.get('submit(seachBtn)')!()
    expect(LAYUI.msg).not.toHaveBeenCalled()
    const fallback = LAYUI.table.reload.mock.calls[0]![1] as { where: Record<string, unknown> }
    expect(fallback.where.date_type).toBe('published')
    expect(fallback.where.date_from).toBe('2026-06-20 00:00:00')
  })
})

describe('HTTP 层失败（403/401/5xx）：后端 msg 必须露出来，不能被吞成英文状态码文案', () => {
  it('`error` 回调把后端 msg 原样写进表内空态并提示一次', async () => {
    const root = await mountPage()
    const opts = LAYUI.table.render.mock.calls[0]![0] as {
      error: (res: unknown, msg: unknown) => void
      done: (r: { count: number; data: unknown[] }, c: number, n: number) => void
    }
    // 真 layui 把表格视图建在 `#tableList` 后面（mount 的 root 之内）——替身不会建，
    // 所以这里手工补一个同形状的容器，让「写进 text.none」这件事有落点。
    root.innerHTML += '<div class="layui-table-view"><div class="layui-none"></div></div>'

    opts.error({ code: 403, msg: '只有客服或客服主管可以查看绩效列表', data: null }, 'error')
    opts.done({ count: 0, data: [] }, 1, 0)

    expect(root.querySelector('.layui-none')!.textContent).toBe(
      '只有客服或客服主管可以查看绩效列表',
    )
    expect(mockedNotifyError).toHaveBeenCalledWith(
      expect.objectContaining({ message: '只有客服或客服主管可以查看绩效列表' }),
    )
  })

  it('⚠️ 真实浏览器里第一参是 **jqXHR**（msg 在 `responseJSON.msg`）——两种形状都要认', async () => {
    // 实测（playwright 抓到的真形状）：只读 `res.msg` 会拿到 undefined ⇒ 界面退回英文 `error`。
    await mountPage()
    const opts = LAYUI.table.render.mock.calls[0]![0] as {
      error: (res: unknown, msg: unknown) => void
    }
    opts.error(
      {
        readyState: 4,
        responseText: '{"code":403,"msg":"只有客服或客服主管可以查看绩效列表","data":null}',
        responseJSON: { code: 403, msg: '只有客服或客服主管可以查看绩效列表', data: null },
        status: 403,
        statusText: 'Forbidden',
      },
      'error',
    )
    expect(mockedNotifyError).toHaveBeenCalledWith(
      expect.objectContaining({ message: '只有客服或客服主管可以查看绩效列表' }),
    )
  })

  it('响应里读不出 msg 时给中性中文兜底（**不出现英文 `error`**，不抛）', async () => {
    await mountPage()
    const opts = LAYUI.table.render.mock.calls[0]![0] as {
      error: (res: unknown, msg: unknown) => void
    }
    // `'error'` 是真实浏览器里 layui 传进来的 textStatus；共享 helper 不再把它当文案。
    opts.error('<html>', 'error')
    expect(mockedNotifyError).toHaveBeenCalledWith(
      expect.objectContaining({ message: '请求失败，请稍后重试' }),
    )
    expect(mockedNotifyError).not.toHaveBeenCalledWith(
      expect.objectContaining({ message: 'error' }),
    )
  })
})

describe('真动作 1：改金额 → POST /api/business/performances/{id}/adjust/', () => {
  it('行内编辑「预计绩效」⇒ 直接按新接单价发 adjust（对标 changeFee 就是改完即发）', async () => {
    await mountPage()
    renderAndFinish()
    const editHandler = LAYUI.handlers.get(`edit(${TABLE_ID})`)
    expect(editHandler).toBeTypeOf('function')
    editHandler!({ field: 'tempAmount', value: '88.00', data: ROW } as never)

    await vi.waitFor(() => expect(mockedAdjust).toHaveBeenCalledTimes(1))
    expect(mockedAdjust).toHaveBeenCalledWith(17, {
      taker_price: '88.00',
      reason: '绩效管理页行内改金额',
    })
    // 改完必须重新取数（后端按提成比例重算，不刷新会「改了没反应」）
    await vi.waitFor(() => expect(LAYUI.table.reload).toHaveBeenCalledTimes(2))
  })

  it('「生效绩效」列走同一条路径（对标：两列都是 changeFee）', async () => {
    await mountPage()
    renderAndFinish()
    LAYUI.handlers.get(`edit(${TABLE_ID})`)!({ field: 'bonusAmount', value: '12', data: ROW } as never)
    await vi.waitFor(() => expect(mockedAdjust).toHaveBeenCalledTimes(1))
    expect(mockedAdjust.mock.calls[0]![1]).toMatchObject({ taker_price: '12' })
  })

  it('负数与 0 是**合法**金额（D-0015）：不拦、照发', async () => {
    await mountPage()
    renderAndFinish()
    LAYUI.handlers.get(`edit(${TABLE_ID})`)!({ field: 'tempAmount', value: '-5.50', data: ROW } as never)
    await vi.waitFor(() => expect(mockedAdjust).toHaveBeenCalledTimes(1))
    expect(mockedAdjust.mock.calls[0]![1]).toMatchObject({ taker_price: '-5.50' })
  })

  it('非数字输入**不发请求**，只提示（对标 VerifyMoeny 失败也只提示）', async () => {
    await mountPage()
    renderAndFinish()
    LAYUI.handlers.get(`edit(${TABLE_ID})`)!({ field: 'tempAmount', value: 'abc', data: ROW } as never)
    expect(mockedAdjust).not.toHaveBeenCalled()
    expect(LAYUI.msg).toHaveBeenCalledWith('金额格式不正确，此次编辑无效')
  })

  it('后端拒绝时把后端 msg 原样提示（403 权限 / 1014 已结账 都走同一条出口）', async () => {
    mockedAdjust.mockRejectedValueOnce(new Error('你没有改接单价的权限'))
    await mountPage()
    renderAndFinish()
    LAYUI.handlers.get(`edit(${TABLE_ID})`)!({ field: 'tempAmount', value: '88', data: ROW } as never)
    await vi.waitFor(() =>
      expect(mockedNotifyError).toHaveBeenCalledWith(
        expect.objectContaining({ message: '你没有改接单价的权限' }),
      ),
    )
  })
})

describe('筛选栏候选：换成**我们自己的**数据（不是对标租户的）', () => {
  it('`gameOptionsHtml`：首项 `请选择`（空值）+ 我们的游戏', () => {
    expect(gameOptionsHtml([{ id: 101, name: '原神' }])).toBe(
      '<option value="">请选择</option>\n<option value="101">原神</option>',
    )
    // 空列表也保留首项（否则「全部」这个状态在界面上就没了）
    expect(gameOptionsHtml([])).toBe('<option value="">请选择</option>')
  })

  it('挂载后 `#gameName` 被**整个重写**成我们的游戏（对标租户那 14 个 option 不再存在）', async () => {
    const root = await mountPage()
    const options = Array.from(root.querySelectorAll<HTMLOptionElement>('#gameName option'))
    expect(options.map((o) => [o.value, o.textContent])).toEqual([
      ['', '请选择'],
      ['101', '原神'],
      ['102', '崩坏：星穹铁道'],
    ])
    // ⚠️ 负向对照：证据件里那 14 个对标租户的游戏名**一个都不该留在页面上**
    //    （留着就说明重写没生效，用户会选到一个我们后端不认识的 id）
    expect(root.textContent).not.toContain('三角洲行动')
    expect(root.textContent).not.toContain('遗忘之海')
    expect(root.querySelector('#gameName')!.innerHTML).not.toContain('21714')
  })

  it('`#orderSoures` / `#inputOrderKefu` 追加我们的候选；⭐ 2026-11-09 起 `#popgameName` **不再预载**（候选改关键词远程搜索）', async () => {
    const root = await mountPage()
    const orderSources = Array.from(
      root.querySelectorAll<HTMLOptionElement>('#orderSoures option'),
    )
    expect(orderSources.map((o) => [o.value, o.textContent])).toEqual([
      ['7', '淘宝'],
      ['8', '闲鱼'],
    ])
    // `#inputOrderKefu`（销售人/发单人那一格，非打手/下家）维持预载：占位 + 两条夹具候选。
    const kefuOptions = Array.from(
      root.querySelectorAll<HTMLOptionElement>('#inputOrderKefu > option'),
    )
    expect(kefuOptions[0]!.value).toBe('')
    expect(kefuOptions[0]!.textContent).toBe('选择或输入')
    expect(kefuOptions.slice(1).map((o) => [o.value, o.textContent])).toEqual([
      ['dailian_A', 'dailian_A'],
      ['kefu_A', 'kefu_A'],
    ])
    // ⭐ `#popgameName`（打手/下家多选）不再吃这份预载：直系 option 只剩占位一条
    //    （「其它分组」optgroup 里的哨兵另测）——回归成「预载候选」的实现会让本条红。
    const playerOptions = Array.from(
      root.querySelectorAll<HTMLOptionElement>('#popgameName > option'),
    )
    expect(playerOptions.map((o) => [o.value, o.textContent])).toEqual([['', '请选择']])
    expect(root.querySelector('#popgameName')!.textContent).not.toContain('dailian_A')
  })

  it('⭐ 2026-11-09：`#popgameName` 开**远程关键词搜索**——`xm-select-search` 属性值 = 账号端点 URL', async () => {
    // vendored formselects：属性**有值** = 远程搜索（值就是 searchUrl），空值 = 只在已加载
    // option 里本地过滤（改造前的形态）。挂载时把证据件里的空值属性改成端点 URL。
    const root = await mountPage()
    const select = root.querySelector<HTMLSelectElement>('#popgameName')
    expect(select, '#popgameName 应当在证据件里').not.toBeNull()
    expect(select!.getAttribute('xm-select-search')).toBe(PLAYER_SEARCH_URL)
    expect(PLAYER_SEARCH_URL).toBe('/api/accounts/')
  })

  it('⭐ 2026-11-09：挂载只发**一发**账号请求且不带 `key`（预载喂客服格；打手候选零预载请求）', async () => {
    vi.mocked(listAccounts).mockClear()
    const root = await mountPage()
    await vi.waitFor(() => expect(listAccounts).toHaveBeenCalledTimes(1))
    // 预载那发：`{page_size: 200}`（喂 `#inputOrderKefu`）；⛔ 不带 `key`——
    // 带 key 的那发是用户输入关键词时才有的（jsdom 里 formselects 未渲染，不会发生）。
    expect(listAccounts).toHaveBeenCalledWith({ page_size: 200 })
    expect(
      vi.mocked(listAccounts).mock.calls.some((call) => 'key' in (call[0] ?? {})),
    ).toBe(false)
    expect(root.querySelector('#inputOrderKefu')).not.toBeNull()
  })

  it('⭐ 2026-11-08：「其它分组」虚拟组**置顶**——`#popgameName` 里哨兵组紧跟 placeholder（业务方：移到列表最前）', async () => {
    const root = await mountPage()
    const group = root.querySelector<HTMLElement>('#popgameName optgroup')
    expect(group, '「其它分组」optgroup 没注入（prependVirtualGroupOptions）').not.toBeNull()
    expect(group!.getAttribute('label')).toBe('其它分组')
    // 结构与顺序：两颗哨兵 option 的 value/文案逐字（文案 = 业务方拍板的字面）。
    const sentinels = Array.from(group!.querySelectorAll<HTMLOptionElement>('option'))
    expect(sentinels.map((o) => [o.value, o.textContent])).toEqual([
      [PLAYER_OPTION_EXCLUDE_SETTLE_FORBIDDEN, '排除禁止结算的'],
      [PLAYER_OPTION_EXCLUDE_NET_NEGATIVE, '排除总绩效为负的'],
    ])
    // 位置钉（vendor 渲染顺序 = DOM 顺序，formselects-v4.js L732-741 ⇒ dt 排在所有打手 dd 之前）：
    // optgroup 紧贴首项 placeholder。候选改远程搜索后挂载期没有账号 option ⇒ 它后面是空
    //（远程回包的置顶哨兵由 playerSentinelRows / beforeSuccess 兜，见下面纯函数直调那条）。
    // ⛔ placeholder 空值 option 必须仍是 select 第一个子节点（vendor L733 只跳过遍历首项的
    // 空值 option，挪位会渲染出一个可点空白项）。
    const select = root.querySelector<HTMLSelectElement>('#popgameName')!
    const children = Array.from(select.children)
    expect(children[0]!.tagName).toBe('OPTION')
    expect((children[0] as HTMLOptionElement).value).toBe('')
    expect(children[1], '虚拟组应紧跟 placeholder、排在候选之前').toBe(group)
    // 「必须在 initFormSelects 之前」由 mount 里的调用顺序保证（render 时刻读 option）。
  })

  it('⭐ 2026-11-09（远程搜索纯函数直调）：`playerRowsFromAccountsEnvelope` 拆账号信封为候选行', () => {
    const envelope = (data: unknown, code = 0) => ({ code, msg: code === 0 ? '' : '测试失败', data })
    const rows = envelope({
      items: [
        { username: 'sq001', display_name: '测试打手甲' },
        { username: '13800000000', display_name: '' },
        { username: '', display_name: '空登录名' },
        '坏行',
      ],
      total: 4,
    })
    // 文本 = display_name || username；value = username（⛔ 不可换成 id）；空登录名的行跳过；坏行滤掉。
    expect(playerRowsFromAccountsEnvelope(rows)).toEqual([
      { name: '测试打手甲', value: 'sq001' },
      { name: '13800000000', value: '13800000000' },
    ])
    // `code≠0` / `data` 缺失 / `items` 非数组 ⇒ 空数组（控件自己显示空态，不另造提示）。
    expect(playerRowsFromAccountsEnvelope(envelope({ items: [] }, 1002))).toEqual([])
    expect(playerRowsFromAccountsEnvelope(envelope(null))).toEqual([])
    expect(playerRowsFromAccountsEnvelope(envelope({ items: '不是数组' }))).toEqual([])
    expect(playerRowsFromAccountsEnvelope('不是对象')).toEqual([])
  })

  it('⭐ 2026-11-09（远程搜索纯函数直调）：`playerSentinelRows` = 组头 + 两颗哨兵置顶行（远程回包整体替换 `<dl>`，静态哨兵靠它回来）', () => {
    expect(playerSentinelRows()).toEqual([
      { type: 'optgroup', name: '其它分组' },
      { name: '排除禁止结算的', value: PLAYER_OPTION_EXCLUDE_SETTLE_FORBIDDEN },
      { name: '排除总绩效为负的', value: PLAYER_OPTION_EXCLUDE_NET_NEGATIVE },
    ])
  })

  it('⭐ 2026-11-09（远程搜索纯函数直调）：`buildPlayerSearchConfig` 各键钉值 + beforeSearch 闸 + beforeSuccess 哨兵置顶', () => {
    const config = buildPlayerSearchConfig()
    expect(config.searchName).toBe('key')
    expect(config.keyVal).toBe('value')
    expect(config.delay).toBe(PLAYER_SEARCH_DELAY_MS)
    expect(PLAYER_SEARCH_DELAY_MS).toBe(400)
    expect(config.data).toEqual({ page_size: PLAYER_SEARCH_PAGE_SIZE })
    expect(PLAYER_SEARCH_PAGE_SIZE).toBe(200)
    expect(config.beforeSearch(PLAYER_SELECT_ID, PLAYER_SEARCH_URL, '')).toBe(false)
    expect(config.beforeSearch(PLAYER_SELECT_ID, PLAYER_SEARCH_URL, '   ')).toBe(false)
    expect(config.beforeSearch(PLAYER_SELECT_ID, PLAYER_SEARCH_URL, 'sq')).toBe(true)
    // beforeSuccess：哨兵置顶 + 关键词命中行（串**真** playerRowsFromAccountsEnvelope，不用桩）。
    const rows = config.beforeSuccess(PLAYER_SELECT_ID, PLAYER_SEARCH_URL, 'sq', {
      code: 0,
      msg: '',
      data: { items: [{ username: 'sq001', display_name: '测试打手甲' }] },
    })
    expect(rows).toEqual([
      { type: 'optgroup', name: '其它分组' },
      { name: '排除禁止结算的', value: PLAYER_OPTION_EXCLUDE_SETTLE_FORBIDDEN },
      { name: '排除总绩效为负的', value: PLAYER_OPTION_EXCLUDE_NET_NEGATIVE },
      { name: '测试打手甲', value: 'sq001' },
    ])
  })

  it('⭐ 2026-11-08：证据件 `#popgameName` 带搜索属性（`xm-select-search` 在，搜「排除」才能过滤到虚拟组）', () => {
    // vendor 侧判据（读码）：默认 filter 按 option 文本子串匹配（formselects-v4.js L144-146），
    // optgroup 的 option 渲染成平铺 `dd`（L732-741）、组头 `dt` 在子项全隐时自动藏（L382-386）
    // ⇒ 搜索天然覆盖「其它分组」里的两颗哨兵，无需页面额外接线。
    const tag = /<select name="popgameName"[^>]*>/.exec(EVIDENCE)?.[0] ?? ''
    expect(tag, '证据件里的 #popgameName select 标签没取到').not.toBe('')
    expect(tag).toContain('xm-select-search')
    expect(tag).toContain('xm-select-search-type="title"')
  })

  it('⭐ 2026-11-08：打手下拉面板加高（页面级 <style> 注入；窄选择器只钉 #popgameName 的相邻兄弟）', async () => {
    await mountPage()
    const style = document.getElementById(PLAYER_DROPDOWN_HEIGHT_STYLE_ID)
    expect(style, '面板加高的 <style> 没注入（mount）').not.toBeNull()
    const css = style!.textContent ?? ''
    expect(css).toContain('max-height: 420px')
    // 影响面钉住：每条提到 `dl` 的规则都必须从 `select#popgameName` 起
    // （vendor 把 .xm-select-parent 插在原 <select> 之后 ⇒ 相邻兄弟形状），
    // ⛔ 不许放宽成 `.xm-select-parent …` 面板族——同页其余 formselects 下拉不加高。
    const selectors = css
      .split('}')
      .map((rule) => rule.split('{')[0]?.trim() ?? '')
      .filter((selector) => selector.includes('dl'))
    expect(selectors.length, '加高样式里没有可辨认的规则').toBeGreaterThan(0)
    for (const selector of selectors) {
      expect(selector.startsWith('select#popgameName'), `选择器放宽了：${selector}`).toBe(true)
    }
  })

  it('⭐ 2026-10-26：筛选栏运行期注入「订单分组」（首项「全部分组」+ 分组字典候选）', async () => {
    const root = await mountPage()
    const select = root.querySelector<HTMLSelectElement>('#sourceGroup')
    expect(select, '「订单分组」控件没注入（injectSourceGroupFilter）').not.toBeNull()
    const options = Array.from(select!.querySelectorAll('option')).map((o) => [
      o.value,
      o.textContent,
    ])
    expect(options).toEqual([
      ['', '全部分组'],
      ['31', '甲组'],
      ['32', '乙组'],
    ])
    // 与同排「订单来源」同族：这几个属性缺一个，formselects 就会按普通 select 渲染。
    for (const attr of ['xm-select-radio', 'xm-select-search', 'xm-select-skin', 'xm-select-height']) {
      expect(select!.hasAttribute(attr), `缺属性 ${attr}`).toBe(true)
    }
    // 标签文案（对标没有这一颗，文案是页面自拟）。
    const label = select!.closest('.layui-inline')?.querySelector('.layui-form-label')
    expect(label?.textContent).toBe('订单分组')
  })

  it('⭐ 2026-10-04：`#inputOrderKefu` option 文本 = **昵称**（缺昵称回落登录账号）、值仍是 `username`；提交后逗号串不变（spec §3-4）', async () => {
    // 合成夹具：一个账号的昵称与登录账号**不同形**（判据才判得动），另一个昵称为空 ⇒ 钉回落。
    vi.mocked(listAccounts).mockResolvedValueOnce({
      items: [
        { username: 'kefu_A', display_name: '测试昵称甲' },
        { username: 'dailian_B', display_name: '' },
      ],
      total: 2,
      page: 1,
      page_size: 200,
    } as never)
    const root = await mountPage()
    // ⭐ 2026-11-09：预载候选只剩 `#inputOrderKefu`（销售人/发单人那一格）——打手多选改远程
    //    关键词搜索后不再吃这份预载（同名文本回落口径由 `playerRowsFromAccountsEnvelope`
    //    的纯函数直调用例钉）。
    const options = Array.from(
      root.querySelectorAll<HTMLOptionElement>('#inputOrderKefu > option'),
    )
    expect(options.map((o) => [o.value, o.textContent])).toEqual([
      ['', '选择或输入'], // 首项 placeholder 不动
      ['kefu_A', '测试昵称甲'], // 文本 = 昵称
      ['dailian_B', 'dailian_B'], // 空昵称 ⇒ 回落登录账号
    ])
    // ⭐ **提交后的那一半**（防「把值改成 id」）：值从**页面 DOM** 取，再过真映射函数。
    const kefuValues = Array.from(
      root.querySelectorAll<HTMLOptionElement>('#inputOrderKefu option'),
    )
      .map((o) => o.value)
      .filter((value) => value !== '')
    expect(kefuValues).toEqual(['kefu_A', 'dailian_B'])
    const params = toPerformanceSearch(
      { customerType: '0', inputOrderKefu: kefuValues.join(',') },
      '打单工资',
    )
    // 后端按 `user__username__in` 筛 ⇒ 这一串必须仍是**登录账号**的逗号串。
    expect(params.customer).toBe('kefu_A,dailian_B')
    expect(params.customer_type).toBe(0)
  })

  it('`#popgameName` 在证据件里是**真的 `select`**（不再是点不动的 xm-select 静态壳）', () => {
    expect(EVIDENCE).toContain('<select name="popgameName" id="popgameName" xm-select="popgameName"')
    expect(EVIDENCE).not.toContain('<xm-select')
    // 那条为死壳准备的 xm-select 规则块也删了
    expect(EVIDENCE).not.toContain('.xm-select-default')
  })

  it('`#orderSta` 的 option 是**我们自己的 8 个状态**（个数仍是 9，与对标一致）', () => {
    const block = /<select name="orderSta"[\s\S]*?<\/select>/.exec(EVIDENCE)?.[0] ?? ''
    const values = Array.from(block.matchAll(/<option value="([^"]*)"/g)).map((m) => m[1])
    expect(values).toEqual([
      '',
      '未分配',
      '待抢单',
      '代练中',
      '待验收',
      '已完成',
      '问题单',
      '已暂停',
      '已退单',
    ])
    // 对标的两个我方没有的状态已不在（有意替换，不是漏做）
    expect(block).not.toContain('待上号')
    expect(block).not.toContain('异常中')
  })
})

describe('工具栏分发：删剩的 7 颗各自去哪（brief §2）', () => {
  /** 点一颗工具栏按钮（`layui.table` 的 `toolbar(tableList)` 事件）。 */
  function clickToolbar(event: string): void {
    const toolbar = LAYUI.handlers.get(`toolbar(${TABLE_ID})`)
    expect(toolbar, `缺少 toolbar(${TABLE_ID}) 处理器`).toBeTypeOf('function')
    toolbar!({ event } as never)
  }

  it('「刷新列表」重新取数（不看勾选、不读鸡蛋铺余额）', async () => {
    await mountPage()
    renderAndFinish()
    clickToolbar('refresh')
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(2)
  })

  it('`payBtn` 无勾选时不开付款弹窗', async () => {
    await mountPage()
    renderAndFinish()
    const before = LAYUI.table.reload.mock.calls.length
    clickToolbar('payBtn')
    expect(LAYUI.msg).toHaveBeenCalledWith('请选中要操作的记录')
    expect(LAYUI.table.reload.mock.calls.length).toBe(before)
    expect(LAYUI.open).not.toHaveBeenCalled()
    expect(LAYUI.confirm).not.toHaveBeenCalled()
    expect(LAYUI.prompt).not.toHaveBeenCalled()
    expect(mockedAdjust).not.toHaveBeenCalled()
    expect(mockedSettle).not.toHaveBeenCalled()
    expect(mockedRevoke).not.toHaveBeenCalled()
    expect(mockedDelete).not.toHaveBeenCalled()
    expect(mockedExport).not.toHaveBeenCalled()
  })

  it('「鸡蛋铺余额充值」仍提示「尚未开放」（那一颗是真缺能力，与 `payBtn` 的文案刻意分开）', async () => {
    await mountPage()
    renderAndFinish()
    clickToolbar('payBalanceBtn')
    expect(LAYUI.msg).toHaveBeenCalledWith('「鸡蛋铺余额充值」尚未开放')
    expect(LAYUI.msg).not.toHaveBeenCalledWith('「鸡蛋铺余额充值」该功能暂时不做')
  })

  it('⭐ 首屏 `dateType=发单时间(10)` 档：**不弹任何提示**，且请求里带 `date_type=published`', async () => {
    // ⭐⭐ 五档全通：日期口径的「尚未开放」提示与「按录单时间取数」的退路**都不存在**
    await mountPage()
    LAYUI.form.val.mockReturnValueOnce({
      dateType: '10',
      sDate: '2026-06-20 00:00:00',
      eDate: '2026-09-20 23:59:59',
      settingSta: '',
    })
    renderAndFinish()
    expect(LAYUI.msg).not.toHaveBeenCalled()
    const cfg = LAYUI.table.reload.mock.calls[0]![1] as { where: Record<string, unknown> }
    expect(cfg.where).toMatchObject({
      date_type: 'published',
      date_from: '2026-06-20 00:00:00',
      date_to: '2026-09-20 23:59:59',
    })
  })

  it('页签切到「销售客服提成账单」⇒ emp_type 换成销售提成并重新取数', async () => {
    const root = await mountPage()
    renderAndFinish()
    LAYUI.table.reload.mockClear()
    const title = document.createElement('ul')
    title.className = 'layui-tab-title'
    const li = document.createElement('li')
    li.setAttribute('lay-id', '20')
    title.appendChild(li)
    root.appendChild(title)
    li.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
    const cfg = LAYUI.table.reload.mock.calls[0]![1] as { where: Record<string, unknown> }
    expect(cfg.where.emp_type).toBe('销售提成')
  })

  it('⭐ 切页签**不丢**「订单分组」（两账单共用一个表单值，2026-10-26 计划 §5）', async () => {
    const root = await mountPage()
    renderAndFinish()
    // 表单里已经选了分组（真 layui 的 `form.val` 读 DOM；本替身读返回值 ⇒ 这里直接给值）。
    LAYUI.form.val.mockReturnValueOnce({
      ...DEFAULT_FILTERS,
      empType: '打单工资',
      sourceGroup: '31',
    })
    const title = document.createElement('ul')
    title.className = 'layui-tab-title'
    const li = document.createElement('li')
    li.setAttribute('lay-id', '20')
    title.appendChild(li)
    root.appendChild(title)
    LAYUI.table.reload.mockClear()
    li.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
    const cfg = LAYUI.table.reload.mock.calls[0]![1] as { where: Record<string, unknown> }
    expect(cfg.where.emp_type).toBe('销售提成')
    // 关键：分组筛选跟着过去了（页面没有清它）。
    expect(cfg.where.source_group).toBe(31)
  })

  it('⭐⭐ 真 DOM：点第二个页签（`.layui-tab-content` 只有 1 个面板）⇒ 标题切 `layui-this`、发出 `emp_type=销售提成`、唯一面板不被藏', async () => {
    // ⚠️ 判别力全在**真证据件结构**上：`layui/island.ts` 上一版的捕获处理器在算 `idx`
    //    **之前**就 `stopPropagation()`，而本页 `.layui-tab-content` 只有 1 个面板
    //    （两个页签共用一套 DOM，对标就是这种写法）⇒ 点第 2 个页签 `idx=1 >= 1`：
    //    事件已被吞掉、页面的 `onTabClick` **永远收不到**（判据 ② 会红）；
    //    同时它直接 `return`，标题 `layui-this` 也不切（判据 ① 会红）。
    //    上面那条「页签切到…」用的是**手搭的** `.layui-tab-title`（没有 `.layui-tab-content`，
    //    处理器在那儿会提前 `return`）⇒ 它测不到这两个 bug；本用例才是在真结构上驱动真点击。
    const root = await mountPage()
    renderAndFinish()
    LAYUI.table.reload.mockClear()
    const tab = root.querySelector<HTMLElement>('.layui-tab')!
    const liMain = tab.querySelector<HTMLElement>('.layui-tab-title > li[lay-id="10"]')!
    const liSales = tab.querySelector<HTMLElement>('.layui-tab-title > li[lay-id="20"]')!
    const panel = tab.querySelector<HTMLElement>('.layui-tab-content > .layui-tab-item')!
    // 前置哨兵：真结构确实是「2 标题 / 1 面板」，且初始态是第一个页签 + 面板可见
    expect(tab.querySelectorAll('.layui-tab-title > li')).toHaveLength(2)
    expect(tab.querySelectorAll('.layui-tab-content > .layui-tab-item')).toHaveLength(1)
    expect(panel.classList.contains('layui-show')).toBe(true)
    expect(liMain.classList.contains('layui-this')).toBe(true)

    liSales.dispatchEvent(new MouseEvent('click', { bubbles: true }))

    // ① 标题高亮照 layui 的可见行为切过去（点谁谁 `layui-this`）
    expect(liSales.classList.contains('layui-this')).toBe(true)
    expect(liMain.classList.contains('layui-this')).toBe(false)
    // ② 页面真的收到那次点击 ⇒ 发出 `emp_type=销售提成` 的取数请求
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
    const cfg = LAYUI.table.reload.mock.calls[0]![1] as { where: Record<string, unknown> }
    expect(cfg.where.emp_type).toBe('销售提成')
    // ③ fail-safe：面板数（1）少于标题数（2）时**不许**把唯一的面板藏掉
    expect(panel.classList.contains('layui-show')).toBe(true)
  })
})

describe('⭐⭐ 筛选栏控件（真 DOM，证据件原文）：删掉的 4 个**不在 DOM**、保留的仍给「尚未开放」提示', () => {
  it('⭐ 被删的 4 个控件（`taobaoSta`/`isCert`/`key2`/`key3`）**在页面 DOM 里不存在**', async () => {
    const root = await mountPage()
    // ⚠️ 判据打在**真证据件**渲染出来的 DOM 上（挂载时 `root.innerHTML = <证据件原文>`）——
    //    源码里 grep 不到不够：控件是 `initFormSelects` 之后才成形的。
    // ⚠️ 集合是 4 个——`selectSeach1`（对标的「代练/下家」）已恢复、它的「在 DOM 里」由
    //    下面「批量搜用户」那组用例正向钉住（见「已知会不同」第 30 条）。
    for (const selector of [
      'select[name="taobaoSta"]',
      'select#isCert',
      'input#key2',
      'input#key3',
    ]) {
      expect(root.querySelector(selector), `${selector} 应当已从证据件删除`).toBeNull()
    }
    // 标签文案也不再出现（整条 `layui-inline` 删了，不是只删控件）。
    // ⚠️ 判据只看**可见文本**：证据件的 `<style>` 与 HTML 注释里仍然有这些词
    //    （注释正是「有意裁剪」的登记处），把 `innerHTML` 算进来这条判据会永远红。
    for (const label of ['淘宝状态', '是否实名', '搜索2', '搜索3']) {
      expect(visibleText(root)).not.toContain(label)
    }
  })

  it('⭐ 保留的控件**仍在 DOM**；未映射档（结算中）弹「尚未开放」、已映射档（未结算）真发参（D-0365 改准）', async () => {
    const root = await mountPage()
    // ⛔（D-0365）`furlSta` 是真筛选，不进「尚未开放」表（点名的是「结算中」未映射档）；
    //    「仍在 DOM」的三条判据保留（控件照证据件渲染）。
    expect(root.querySelector('select#furlSta')).not.toBeNull()
    expect(root.querySelector('[name="applicationSetSta"]')).not.toBeNull()
    expect(root.querySelector('select#taskSelect')).not.toBeNull()
    // 「结算中」映射不到 ⇒ 点名提示（合并通道）；「未结算」已映射 ⇒ **不弹**、真发参
    //    （发参形状已由上面 `toPerformanceSearch` 的纯函数用例钉住，这里只钉「不弹」）。
    LAYUI.form.val.mockReturnValueOnce({ dateType: '25', furlSta: '结算中' })
    renderAndFinish()
    expect(LAYUI.msg).toHaveBeenCalledWith('「结算中」尚未开放，本次已忽略')
    LAYUI.msg.mockClear()
    LAYUI.form.val.mockReturnValueOnce({ dateType: '25', furlSta: '未结算' })
    renderAndFinish()
    for (const call of LAYUI.msg.mock.calls) {
      expect(String(call[0])).not.toContain('尚未开放，本次已忽略')
    }
  })

  it('⭐ 阴性对照：默认筛选（三个保留控件都没填）**不弹**「尚未开放」那句', async () => {
    await mountPage()
    renderAndFinish()
    for (const call of LAYUI.msg.mock.calls) {
      expect(String(call[0])).not.toContain('尚未开放，本次已忽略')
    }
  })
})

describe('⭐⭐⭐ 批量搜用户（`selectSeach1`，第四轮 C：恢复上一轮**误删**的对标控件）', () => {
  type SelectHandler = (data: { value: string }) => void
  /** `form.on('select(selectSeach1)')` 的处理器（替身按事件名把它存下来了）。 */
  const selectSeach1 = (): SelectHandler =>
    LAYUI.formHandlers.get('select(selectSeach1)') as unknown as SelectHandler

  it('① 控件在 DOM 里、两个 option 文案逐字，且**无 label**（对标原文如此）', async () => {
    const root = await mountPage()
    const select = root.querySelector<HTMLSelectElement>('select#selectSeach1')
    expect(select, '`selectSeach1` 应当在（上一轮误删、本轮恢复）').not.toBeNull()
    expect(select!.getAttribute('lay-filter')).toBe('selectSeach1')
    expect(Array.from(select!.options).map((o) => [o.value, o.textContent])).toEqual([
      ['0', '代练/下家'],
      ['10', '批量搜用户'],
    ])
    // 对标那一格是 `div.layui-input-inline style="width: 110px;"`、**没有** label。
    expect(select!.closest('.layui-input-inline')!.getAttribute('style')).toBe('width: 110px;')
    expect(select!.closest('.layui-inline')!.querySelector('label')).toBeNull()
  })

  it('② 选「批量搜用户」⇒ 弹文本域（标题逐字）；三行 ⇒ `username` **与其它筛选同时发**（照对标 JS 的行为）', async () => {
    await mountPage()
    renderAndFinish()
    LAYUI.table.reload.mockClear()

    selectSeach1()({ value: '10' })

    expect(LAYUI.layer.prompt).toHaveBeenCalledTimes(1)
    const opts = LAYUI.layer.prompt.mock.calls[0]![0] as Record<string, unknown>
    expect(opts).toMatchObject({
      formType: 2,
      btn: ['搜索', '关闭'],
      title: '按系统用户昵称批量查询，每行一条。注意：该搜索会排除其它搜索条件',
      area: ['520px', '300px'],
    })

    const ok = LAYUI.layer.prompt.mock.calls[0]![1] as (v: string, i: number) => void
    ok('a\nb\n\n  c  \n', 7)

    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
    const cfg = LAYUI.table.reload.mock.calls[0]![1] as { where: Record<string, unknown> }
    // ⚠️ **第四轮 M-1（口径修正）**：对标的 `layer.prompt` **标题**说「该搜索会排除其它搜索条件」，
    //    但它**自己的 JS** 并不这么做——`emplist.js` L526-556 只设 `recUserName`，
    //    随后调的 `reloadOrderList()`（L905-958）拼的 `seachParam` **含全部条件 + `recUserName`**
    //    ⇒ 我们照 JS 的行为：`username` **与** `emp_type`/`date_from`/`date_to` **同时在场**。
    //    期望值写字面值（E-0024：不拿导出常量自指）。
    expect(cfg.where).toEqual({
      username: 'a,b,c',
      emp_type: '打单工资',
      // ⭐ 线 C：默认档（`DEFAULT_FILTERS` 的 `dateType: '25'`）显式发 `date_type=created`。
      date_type: 'created',
      date_from: '2026-06-20 00:00:00',
      date_to: '2026-09-20 23:59:59',
    })
    expect(LAYUI.layer.close).toHaveBeenCalledWith(7)
  })

  it('③ 文本域为空（含只有空白行）⇒ 提示「搜索项不能为空」且**零请求**', async () => {
    await mountPage()
    renderAndFinish()
    LAYUI.table.reload.mockClear()
    LAYUI.msg.mockClear()

    selectSeach1()({ value: '10' })
    const ok = LAYUI.layer.prompt.mock.calls[0]![1] as (v: string, i: number) => void
    ok('   \n\n', 4)

    expect(LAYUI.msg).toHaveBeenCalledWith('搜索项不能为空')
    expect(LAYUI.table.reload).not.toHaveBeenCalled()
    expect(LAYUI.layer.close).toHaveBeenCalledWith(4)
  })

  it('④ 选回「代练/下家」(0) ⇒ **清掉 `username`**（其余筛选在批量档本来也照发）', async () => {
    await mountPage()
    renderAndFinish()
    // 先进批量档，证明那个状态真的被设上过（下面才能断言它被清掉）
    selectSeach1()({ value: '10' })
    const ok = LAYUI.layer.prompt.mock.calls[0]![1] as (v: string, i: number) => void
    ok('a\nb', 1)

    LAYUI.table.reload.mockClear()
    selectSeach1()({ value: '0' })

    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
    const cfg = LAYUI.table.reload.mock.calls[0]![1] as { where: Record<string, unknown> }
    expect(cfg.where).toEqual({
      emp_type: '打单工资',
      // ⭐ 线 C：默认档显式发 `date_type=created`。
      date_type: 'created',
      date_from: '2026-06-20 00:00:00',
      date_to: '2026-09-20 23:59:59',
    })
    expect('username' in cfg.where).toBe(false)
  })

  it('⑤ `#popgameName` 的空选项文案是「请选择」（对标 xm-select 的默认占位）', async () => {
    const root = await mountPage()
    expect(
      root.querySelector<HTMLOptionElement>('select#popgameName option[value=""]')!.textContent,
    ).toBe('请选择')
    // 对照：另一个控件的 placeholder 是对标 HTML 原文，**没有**被连带改掉
    expect(
      root.querySelector<HTMLOptionElement>(
        'select#inputOrderKefu option[value=""]',
      )!.textContent,
    ).toBe('选择或输入')
  })
})

describe('⭐⭐ 两个日期框的默认值：**运行期重算**（证据件里那两个写死的值必须被覆盖）', () => {
  beforeEach(() => {
    // 固定「今天」⇒ 断言可以是**字面值**（不受跑测那天影响）
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.setSystemTime(new Date(2026, 8, 22, 10, 30, 0))
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('⭐ 挂载后两个框 = 「今天 − 3 个自然月 00:00:00」~「今天 23:59:59」', async () => {
    const root = await mountPage()
    const s = root.querySelector<HTMLInputElement>('input#sDate')!
    const e = root.querySelector<HTMLInputElement>('input#eDate')!
    expect(s.value).toBe('2026-06-22 00:00:00')
    expect(e.value).toBe('2026-09-22 23:59:59')
  })

  it('⭐ **判别力**：证据件里那两个写死的值（`2026-06-20` / `2026-09-20`）被覆盖', async () => {
    // ⚠️ 直接吃证据件的静态 `value` 会让默认区间失效 ⇒ 这里先证明证据件里确实写着那两个值
    //    （否则下面「被覆盖」的判据没有判别力）。
    expect(EVIDENCE).toContain('value="2026-06-20 00:00:00"')
    expect(EVIDENCE).toContain('value="2026-09-20 23:59:59"')
    const root = await mountPage()
    const s = root.querySelector<HTMLInputElement>('input#sDate')!
    const e = root.querySelector<HTMLInputElement>('input#eDate')!
    // 挂载后的值**逐字**等于本轮算出来的区间，而**不等于**证据件里那两个
    expect(s.value).toBe('2026-06-22 00:00:00')
    expect(e.value).toBe('2026-09-22 23:59:59')
    expect(s.value).not.toBe('2026-06-20 00:00:00')
    expect(e.value).not.toBe('2026-09-20 23:59:59')
  })

  it('⭐ 两个日期框交给 `laydate`（`type:datetime`，与对标 `emplist.js` L66-73 逐字同形）', async () => {
    // ⚠️ 复刻缺口：本页建起来时只画了证据件里的两个 `input`、
    //    **没接 `laydate`** ⇒ 业务方看到的症状是「点日期框什么都不出来」。
    await mountPage()
    // 配置**逐字**等于对标那两处：只有 `elem` + `type`（⛔ 无 `value` / `min` / `max`）
    expect(LAYUI.laydateRender).toHaveBeenCalledWith({ elem: '#sDate', type: 'datetime' })
    expect(LAYUI.laydateRender).toHaveBeenCalledWith({ elem: '#eDate', type: 'datetime' })
    // 页面确实向 layui **要了** `laydate` 模块（替身里它恒存在 ⇒ 不记这一笔就抓不到「忘了要模块」）
    expect(LAYUI.bootstrapModules[0]).toContain('laydate')
    // ⚠️ **顺序判据**：`laydate.render` 被调那一刻，框里必须**已经是**运行期算出来的默认值
    //    （真 laydate 建实例时读 `elem` 的现值；先 render 再填值 ⇒ 选择器里是空的）
    expect(
      LAYUI.laydateRender.mock.results.map((r) => (r.value as { valueAtCall: string }).valueAtCall),
    ).toEqual(['2026-06-22 00:00:00', '2026-09-22 23:59:59'])
  })

  it('⭐ 首屏取数用的就是这两个新值（不是证据件里的旧值）', async () => {
    const root = await mountPage()
    // ⚠️ 替身 `form.val` 不读 DOM，所以这里**显式**把 DOM 里那两个框的值喂回去——
    //    测的是「挂载时确实把界面改成了运行期算的区间，而首屏取数用的是界面上显示的值」
    //    （而不是「替身返回了什么」这种自证）。
    LAYUI.form.val.mockReturnValue({
      dateType: '25',
      sDate: root.querySelector<HTMLInputElement>('input#sDate')!.value,
      eDate: root.querySelector<HTMLInputElement>('input#eDate')!.value,
      settingSta: '',
    })
    renderAndFinish()
    const cfg = LAYUI.table.reload.mock.calls[0]![1] as { where: Record<string, unknown> }
    // ⭐（2026-11-07 起）运行期默认值本身带时分秒 ⇒ **原样透传**（不再是 date-only 截断）。
    expect(cfg.where.date_from).toBe('2026-06-22 00:00:00')
    expect(cfg.where.date_to).toBe('2026-09-22 23:59:59')
  })
})

describe('⭐ 汇总行 `#total` 接真值（`data.summary` 六项）', () => {
  const SUMMARY = {
    temp_amount: '88.00',
    bonus_amount: '12.34',
    order_price: '120.00',
    refund_fee: '5.00',
    profit_expected: '27.00',
    profit_actual: '102.66',
  }

  it('⭐ `parseData` 必须把 `summary` **带走**（layui 交给 `done` 的是它的返回值）', async () => {
    await mountPage()
    const opts = LAYUI.table.render.mock.calls[0]![0] as {
      parseData: (r: unknown) => { summary?: unknown }
    }
    const parsed = opts.parseData({ code: 0, msg: '', data: { items: [ROW], total: 7, summary: SUMMARY } })
    expect(parsed.summary).toEqual(SUMMARY)
  })

  it('⭐ `done` 里把六项写进 `#total`（缺 `summary` 的旧形状才回落到占位值）', async () => {
    const root = await mountPage()
    root.innerHTML += '<span id="total"></span>'
    const opts = LAYUI.table.render.mock.calls[0]![0] as {
      parseData: (r: unknown) => unknown
      done?: (r: unknown, c: number, n: number) => void
    }
    const parsed = opts.parseData({
      code: 0,
      msg: '',
      data: { items: [ROW], total: 3, summary: SUMMARY },
    })
    opts.done?.(parsed, 1, 3)
    expect(root.querySelector('#total')!.textContent).toBe(
      '本次筛选条件下共计3条记录，总计【预计绩效88.00元，生效绩效12.34元，' +
        '订单金额120.00元，记录退款金额5.00，预计差额利润27.00元，' +
        '已生效差额利润102.66】，默认显示最近3个月数据，若需扩大时间范围，可手动选择起始日期',
    )
  })

  it('⭐ 响应里**没有** `summary`（前后端分开部署的窗口）⇒ 回落成骨架的占位值，不编金额', async () => {
    const root = await mountPage()
    root.innerHTML += '<span id="total"></span>'
    const opts = LAYUI.table.render.mock.calls[0]![0] as {
      parseData: (r: unknown) => unknown
      done?: (r: unknown, c: number, n: number) => void
    }
    const parsed = opts.parseData({ code: 0, msg: '', data: { items: [ROW], total: 3 } })
    opts.done?.(parsed, 1, 3)
    expect(root.querySelector('#total')!.textContent).toBe(
      '本次筛选条件下共计3条记录，总计【预计绩效0.00元，生效绩效0.00元，订单金额0.00元，' +
        '记录退款金额0，预计差额利润0.00元，已生效差额利润0.00】，' +
        '默认显示最近3个月数据，若需扩大时间范围，可手动选择起始日期',
    )
  })
})

describe('真动作 2：「仅修改结算状态」开「清算绩效」子页弹窗（对标 `settSta` 1:1，2026-10-03 Task 4）', () => {
  /**
   * 勾选行的夹具——**`checkStatus().data` 是 layui 行**（`toRow()` 的产物），结账状态字段名是
   * `settingSta`（不是后端行里的 `settle_status`）⇒ 用 `toRow` 从后端行造，⛔ 不手写字段名。
   */
  const UNSETTLED: PerformanceRow = toRow({ ...ROW, id: 11, settle_status: '未结账' })
  const SETTLED: PerformanceRow = toRow({ ...ROW, id: 13, settle_status: '已结账' })

  /** 点 `settBtn`（`rows` 就是 `checkStatus` 要回的那一批勾选行）。 */
  async function clickSettBtn(rows: PerformanceRow[]): Promise<void> {
    await mountPage()
    renderAndFinish()
    LAYUI.table.checkStatus.mockReturnValueOnce({ data: rows })
    LAYUI.handlers.get(`toolbar(${TABLE_ID})`)!({ event: 'settBtn' } as never)
  }

  /** 那一刻 `layer.open` 的实参（先钉「恰好一次」）。 */
  function openedLayer(): Record<string, unknown> {
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    return LAYUI.open.mock.calls[0]![0] as Record<string, unknown>
  }

  it('勾 0 行 ⇒ 只提示对标原话，**不开层、零请求**', async () => {
    await clickSettBtn([])
    expect(LAYUI.msg).toHaveBeenCalledWith(NEED_SELECTION_TEXT)
    expect(NEED_SELECTION_TEXT).toBe('请选中要操作的记录')
    expect(LAYUI.open).not.toHaveBeenCalled()
    expect(LAYUI.layer.prompt).not.toHaveBeenCalled()
    expect(mockedSettle).not.toHaveBeenCalled()
    expect(mockedRevoke).not.toHaveBeenCalled()
    // 只有挂载时那次首屏取数；点这一下**不动**表格。
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
  })

  it('⭐ 勾 N 行 ⇒ `layer.open` **恰好一次**，实参**逐字**照对标 `settSta()`（含 80%×50% / type:2 / skin / anim）', async () => {
    await clickSettBtn([UNSETTLED, { ...UNSETTLED, id: 18 }])
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    // 期望值写**字面值**（E-0024）：不引常量、也不写「用被测代码同款表达式再算一遍」的自指断言。
    expect(LAYUI.open.mock.calls[0]![0]).toEqual({
      title: '清算绩效',
      shade: [0.3, '#333'],
      shadeClose: true,
      type: 2,
      skin: 'layui-layer-lan',
      anim: 8,
      area: ['80%', '50%'],
      content: '/layui-island/page.html?page=emp-settlementemp&ids=11,18',
    })
    // 两句反向哨兵：把 `type` / `anim` / `skin` 之类改错时上面那条也要能红（防止将来被「统一」成别的层）。
    expect(SETTLE_DIALOG_TITLE).toBe('清算绩效')
  })

  it('⭐ `content` 的 URL 形状 + **注入 `/console/` 基址复跑**（证明基址没写死成绝对路径）', async () => {
    await clickSettBtn([UNSETTLED, { ...UNSETTLED, id: 18 }])
    const content = String(openedLayer().content)
    // `page=` 的值就是子页模块导出的 key（⛔ 不是手抄的字符串，写错 ⇒ 弹层里是「未登记的 island 页面」）。
    expect(content).toBe(
      `/layui-island/page.html?page=${SETTLEMENT_PAGE_KEY}&ids=11,18`,
    )
    expect(SETTLEMENT_PAGE_KEY).toBe('emp-settlementemp')
    // ⚠️ 基址必须跟着 `BASE_URL` 走（子路径部署 `TENANT_WEB_BASE=/console/` ⇒ `BASE_URL='/console/'`）：
    // 显式注入 `/console/` 断**整串字面值**——谁把实现改回硬编码的 `/layui-island/…`，这些就红。
    expect(settlementDialogContent([11, 18], '/console/')).toBe(
      '/console/layui-island/page.html?page=emp-settlementemp&ids=11,18',
    )
    // 注入的基址也按同一规则剥尾斜杠（`/console/` 与 `/console` 同结果）。
    expect(islandPageUrl('page=emp-settlementemp&ids=7', '/console')).toBe(
      '/console/layui-island/page.html?page=emp-settlementemp&ids=7',
    )
  })

  it('⭐ 点击**不再发** `settle` / `revoke`（旧行为的两个写端点在本页一次都不打）——最关键的防回归', async () => {
    // 混选（1 未结账 + 1 已结账）也一样：开层就是这一下动作的全部，两个写端点一个都不碰。
    await clickSettBtn([UNSETTLED, { ...SETTLED, id: 18 }])
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    expect(mockedSettle).not.toHaveBeenCalled()
    expect(mockedRevoke).not.toHaveBeenCalled()
    // 旧行为里的原因层与结账确认层也都不该再出现。
    expect(LAYUI.layer.prompt).not.toHaveBeenCalled()
    expect(LAYUI.layer.confirm).not.toHaveBeenCalled()
    // 列表重取由**子页**成功后的 `parent.tools.reloadOrderData()` 触发，本页这一下不动表格。
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
  })

  it('⭐ id 形状闸：勾到的行 id **不是正整数** ⇒ 不开层、零请求（`content` 是 iframe 的 HTML 属性上下文）', async () => {
    // 喂一个「形状不可能来自序列化器」的 id（字符串注入形态）⇒ 白名单丢掉它、**不拼进 URL**。
    // ⚠️ 生产今天不可达（`id` 由后端给正整数）；这条钉的是**出口处的形状闸本身**。
    await clickSettBtn([{ ...UNSETTLED, id: '11"><img src=x>' as unknown as number }])
    expect(LAYUI.open).not.toHaveBeenCalled()
    expect(LAYUI.msg).toHaveBeenCalledWith('没有可清算的绩效记录')
    expect(mockedSettle).not.toHaveBeenCalled()
    expect(mockedRevoke).not.toHaveBeenCalled()
  })

  it('`settlementIdsParam`：只留正整数（逗号串），0 / 负数 / 小数 / NaN / Infinity 一律丢掉', () => {
    expect(settlementIdsParam([11, 12])).toBe('11,12')
    expect(settlementIdsParam([0, -1, 1.5, Number.NaN, Number.POSITIVE_INFINITY])).toBe('')
    expect(settlementIdsParam([7, 7])).toBe('7,7')
  })
})

describe('真动作 3：删除记录 → POST /api/business/performances/delete（D-0072 ①）', () => {
  async function openDelete(rows: PerformanceListItem[]): Promise<{ cb: (index: number) => void }> {
    await mountPage()
    renderAndFinish()
    // `checkStatus().data` 是 **layui 行**（`toRow()` 的产物）⇒ 夹具过一遍 `toRow`。
    // 本组只读 `.id`，所以这里换形状不改变任何期望值。
    LAYUI.table.checkStatus.mockReturnValueOnce({ data: rows.map(toRow) })
    LAYUI.handlers.get(`toolbar(${TABLE_ID})`)!({ event: 'delEmpBtn' } as never)
    expect(LAYUI.layer.confirm).toHaveBeenCalledTimes(1)
    const call = LAYUI.layer.confirm.mock.calls[0] as unknown as [string, (index: number) => void]
    return { cb: call[1] }
  }

  it('⭐ 确认文案**逐字**照对标，且**只传 content + yes**（标题/按钮走 layui 默认）', async () => {
    await openDelete([ROW])
    const call = LAYUI.layer.confirm.mock.calls[0] as unknown as unknown[]
    expect(call[0]).toBe(DELETE_CONFIRM_TEXT)
    expect(DELETE_CONFIRM_TEXT).toBe(
      '真的删除吗？警告：一旦删除，将无法找回，请谨慎处理，请勿随意删除绩效',
    )
    // ⚠️ **只有两个实参**：第三个（options）一旦出现，说明有人自己造了标题/按钮文案
    expect(call).toHaveLength(2)
  })

  it('勾 0 行 ⇒ 只提示「请选中要操作的记录」，不开确认层、不发请求', async () => {
    await mountPage()
    renderAndFinish()
    LAYUI.table.checkStatus.mockReturnValueOnce({ data: [] })
    LAYUI.handlers.get(`toolbar(${TABLE_ID})`)!({ event: 'delEmpBtn' } as never)
    expect(LAYUI.msg).toHaveBeenCalledWith(NEED_SELECTION_TEXT)
    expect(LAYUI.layer.confirm).not.toHaveBeenCalled()
    expect(mockedDelete).not.toHaveBeenCalled()
  })

  it('⭐ 多选放行：确认后一次带 `[17, 18]`，成功提示条数并重新取数', async () => {
    const { cb } = await openDelete([ROW, { ...ROW, id: 18 }])
    cb(5)
    await vi.waitFor(() => expect(mockedDelete).toHaveBeenCalledTimes(1))
    expect(mockedDelete).toHaveBeenCalledWith([17, 18])
    expect(LAYUI.layer.close).toHaveBeenCalledWith(5)
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('已删除 2 条绩效记录'))
    await vi.waitFor(() => expect(LAYUI.table.reload).toHaveBeenCalledTimes(2))
  })

  it('⭐ 失败（已结账不许删 / MAIN 行 / 有调整记录）⇒ 原样弹后端 msg、不重取、不给假成功', async () => {
    const backendMsg = '绩效 11 已结账，不能删除'
    mockedDelete.mockRejectedValueOnce(new BizError(1001, backendMsg))
    const { cb } = await openDelete([ROW])
    const before = LAYUI.table.reload.mock.calls.length
    cb(1)
    await vi.waitFor(() =>
      expect(mockedNotifyError).toHaveBeenCalledWith(
        expect.objectContaining({ message: backendMsg }),
      ),
    )
    expect(LAYUI.table.reload.mock.calls.length).toBe(before)
    expect(LAYUI.msg).not.toHaveBeenCalledWith(expect.stringContaining('已删除'))
  })
})

describe('真动作 4：导出记录 → GET /api/business/performances/export（与列表同一份筛选）', () => {
  it('⭐ 导出带**当前筛选**（与 `reload` 的 `where` 逐字相同）、不看勾选', async () => {
    await mountPage()
    renderAndFinish()
    // ⚠️ 用**持续**的 `mockReturnValue`（不是 Once）：首屏那次 `done` → `reload()` → `params()`
    //    会先吃掉一个 Once，后面「刷新 / 导出」就拿到默认值了。`beforeEach` 会复位实现。
    LAYUI.form.val.mockReturnValue({
      dateType: '25',
      sDate: '2026-06-20 00:00:00',
      eDate: '2026-09-20 23:59:59',
      settingSta: 'N',
      gameName: '101',
      orderSta: '已完成',
      popgameName: 'dailian_A',
    })
    LAYUI.table.checkStatus.mockReturnValue({ data: [toRow(ROW)] }) // 勾了行也不该被读
    // ⚠️ 读**最近一次** `reload`（第 0 次是挂载期那发，用的是默认筛选值）：先点一次刷新，
    //    拿到的就是列表这一份 `where`（导出必须与它逐字相同——同一个 `params()` 出口）。
    const lastReloadWhere = (): unknown => {
      const calls = LAYUI.table.reload.mock.calls
      return (calls[calls.length - 1]![1] as { where: unknown }).where
    }
    LAYUI.handlers.get(`toolbar(${TABLE_ID})`)!({ event: 'refresh' } as never)
    const where = lastReloadWhere()
    expect(where).toMatchObject({ game: 101, order_status: 'COMPLETED' })

    LAYUI.handlers.get(`toolbar(${TABLE_ID})`)!({ event: 'exportBtn' } as never)
    await vi.waitFor(() => expect(mockedExport).toHaveBeenCalledTimes(1))
    expect(mockedExport).toHaveBeenCalledWith(where)
    // 逐条点名（防止「只带 emp_type」这种假通过）
    expect(mockedExport.mock.calls[0]![0]).toMatchObject({
      emp_type: '打单工资',
      settle_status: '未结账',
      game: 101,
      order_status: 'COMPLETED',
      username: 'dailian_A',
    })
  })

  it('⭐ 触发浏览器下载，但**不关当前页**（对标会 `layer.close` 自己那个页签——我们照抄会关掉 island）', async () => {
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
    await mountPage()
    renderAndFinish()
    LAYUI.handlers.get(`toolbar(${TABLE_ID})`)!({ event: 'exportBtn' } as never)
    await vi.waitFor(() => expect(mockedExport).toHaveBeenCalledTimes(1))
    await vi.waitFor(() => expect(click).toHaveBeenCalledTimes(1))
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1)
    expect(LAYUI.layer.close).not.toHaveBeenCalled()
    click.mockRestore()
  })

  it('失败（超上限 / 参数非法）⇒ 走统一失败出口原样弹后端 msg（**不是**新标签页里一坨 JSON）', async () => {
    mockedExport.mockRejectedValueOnce(new BizError(1000, '本次导出 12000 行，超过上限 10000 行，请先收窄筛选条件'))
    await mountPage()
    renderAndFinish()
    LAYUI.handlers.get(`toolbar(${TABLE_ID})`)!({ event: 'exportBtn' } as never)
    await vi.waitFor(() =>
      expect(mockedNotifyError).toHaveBeenCalledWith(
        expect.objectContaining({ message: expect.stringContaining('超过上限') }),
      ),
    )
  })
})

describe('真动作 5：检测重复收款支付宝（只读弹层，逐字照抄 `emplist.js` L681-701/L1883-1921）', () => {
  /** 打开弹层并返回 `layer.open` 的入参。 */
  async function openAlipay(): Promise<Record<string, unknown>> {
    await mountPage()
    renderAndFinish()
    LAYUI.handlers.get(`toolbar(${TABLE_ID})`)!({ event: 'checkIdenticalAlipayAccBtn' } as never)
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    return LAYUI.open.mock.calls[0]![0] as Record<string, unknown>
  }

  /** 子表的 `table.render` 配置（主表那一发也在 `render` 的调用记录里）。 */
  function alipayRenderOpts(): Record<string, unknown> {
    const call = LAYUI.table.render.mock.calls.find(
      (c) => (c[0] as { elem?: string }).elem === TABLE3_ELEM,
    )
    expect(call, '没有渲染 #tableList3 子表').toBeTruthy()
    return call![0] as Record<string, unknown>
  }

  it('⭐ 弹层选项逐项照对标（title/shade/type/skin/anim/shadeClose/closeBtn/maxmin/area 桌面分支）', async () => {
    const opts = await openAlipay()
    expect(opts.title).toBe(ALIPAY_DIALOG_TITLE)
    expect(ALIPAY_DIALOG_TITLE).toBe('重复收款支付宝子账号检测')
    expect(opts.shade).toEqual([0.3, '#333'])
    expect(opts.type).toBe(1)
    expect(opts.skin).toBe('layui-layer-lan')
    expect(opts.anim).toBe(8)
    // `isPc` 取桌面分支（与 emp-billsummary / emp-mybillsummary 等页同一口径）
    expect(opts.shadeClose).toBe(true)
    expect(opts.closeBtn).toBe(0)
    expect(opts.maxmin).toBe(false)
    expect(opts.area).toEqual(['38%', '90%'])
  })

  it('⭐ `content` 是页面里**那个**容器节点（jQuery 对象；不是另抄一份 HTML——否则 `#tableList3` 会撞 id）', async () => {
    const opts = await openAlipay()
    const node = document.querySelector('.checkAliPayAccDiv')
    expect(node).not.toBeNull()
    expect(opts.content).toBe(node)
  })

  it('关层后把节点放回 `<form>` 并重新隐藏（否则第二次点这颗按钮弹层静默不出现）', async () => {
    const opts = await openAlipay()
    const node = document.querySelector<HTMLElement>('.checkAliPayAccDiv')!
    ;(opts.end as () => void)()
    expect(document.querySelector('.checkAliPayAccDiv')).toBe(node)
    expect(node.style.display).toBe('none')
  })

  it('⭐ 子表列定义逐条照对标（5 个数据列 + 勾选列；`重复数量`/`当前状态` 的 templet 逐字）', () => {
    expect(ALIPAY_COLS.map((c) => c.title)).toEqual([
      undefined,
      '子账号名称',
      '支付宝账号',
      '职位',
      '重复数量',
      '当前状态',
    ])
    expect(ALIPAY_COLS.map((c) => c.width)).toEqual([30, 110, 170, 85, 85, 85])
    expect(ALIPAY_COLS[1]?.field).toBe('str1')
    expect(ALIPAY_COLS[2]?.align).toBe('left')
    expect(ALIPAY_COLS[3]?.align).toBe('center')
    const count = ALIPAY_COLS[4]!.templet as (row: { id2: number }) => string
    expect(count({ id2: 3 })).toBe(
      '<span lay-tips="此收款支付宝账号存在3个，该子账号是其中一个">3</span>',
    )
    const state = ALIPAY_COLS[5]!.templet as (row: { str4: string }) => string
    expect(state({ str4: 'true' })).toBe('<span class="layui-badge layui-bg-blue">正常</span>')
    expect(state({ str4: 'false' })).toBe('<span class="layui-badge layui-bg-gray">禁用</span>')
  })

  it('子表：`elem`/`id`/`toolbar`/`page:false`/`defaultToolbar:[filter]` + 摊平后的行', async () => {
    await openAlipay()
    await vi.waitFor(() => expect(mockedAlipay).toHaveBeenCalledTimes(1))
    const opts = alipayRenderOpts()
    expect(opts.id).toBe(TABLE3_ID)
    expect(opts.toolbar).toBe('#toolbarDemo3')
    expect(opts.page).toBe(false)
    expect(opts.defaultToolbar).toEqual(['filter'])
    expect(opts.data).toEqual([
      {
        str1: 'kefu_A',
        str2: 'zhifubao_dup@example.com',
        str3: '客服',
        id2: 2,
        str4: 'true',
      },
      {
        str1: 'kefu_B',
        str2: 'zhifubao_dup@example.com',
        str3: '客服主管',
        id2: 2,
        str4: 'false',
      },
    ])
  })

  it('子表工具栏 `refresh2` ⇒ 重新拉一次（第二次是 `reload`，不再 `render`——否则会叠出第二张表）', async () => {
    await openAlipay()
    await vi.waitFor(() => expect(mockedAlipay).toHaveBeenCalledTimes(1))
    const rendersBefore = LAYUI.table.render.mock.calls.length
    LAYUI.handlers.get(`toolbar(${TABLE3_ID})`)!({ event: 'refresh2' } as never)
    await vi.waitFor(() => expect(mockedAlipay).toHaveBeenCalledTimes(2))
    await vi.waitFor(() => expect(LAYUI.table.reload).toHaveBeenCalledWith(TABLE3_ID, expect.anything()))
    expect(LAYUI.table.render.mock.calls.length).toBe(rendersBefore)
  })

  it('拉数据失败 ⇒ 原样弹后端 msg（页面不崩、不发第二次请求）', async () => {
    mockedAlipay.mockRejectedValueOnce(new HttpError(403, '你没有查看重复收款支付宝的权限'))
    await openAlipay()
    await vi.waitFor(() =>
      expect(mockedNotifyError).toHaveBeenCalledWith(
        expect.objectContaining({ message: '你没有查看重复收款支付宝的权限' }),
      ),
    )
  })

  it('`alipayRows`：空结果 ⇒ 空数组（空表照渲染，不造行）', () => {
    expect(alipayRows({ duplicates: [] })).toEqual([])
    expect(alipayRows(null)).toEqual([])
  })

  it('⭐ 2026-10-04：子表 `str1`（子账号名称）取**显示名** `display_name`、缺它回落 `username`（spec §2.4-D）', () => {
    // 「显示人统一成昵称」线：老键 `username` **仍在**（它是「账号」这个事实）；
    // 这一格显示的取 `display_name || username`——昵称与账号**不同形**才判得动。
    expect(
      alipayRows({
        duplicates: [
          {
            alipay_account: 'zhifubao_dup@example.com',
            count: 2,
            users: [
              { username: 'kefu_A', display_name: '测试昵称甲', job: '客服', is_active: true },
              // 空昵称 / 后端还没上（键缺席）⇒ 回落登录账号，⛔ 不回落成空串。
              { username: 'kefu_B', display_name: '', job: '客服主管', is_active: false },
              { username: 'kefu_C', job: '客服', is_active: true },
            ],
          },
        ],
      }).map((row) => row.str1),
    ).toEqual(['测试昵称甲', 'kefu_B', 'kefu_C'])
  })

  it('⭐ 2026-10-04：查重子表**渲出来的行**里 `str1` 是昵称（整链：从后端载荷到 `table.render` 的 data）', async () => {
    mockedAlipay.mockResolvedValueOnce({
      duplicates: [
        {
          alipay_account: 'zhifubao_dup@example.com',
          count: 2,
          users: [
            { username: 'kefu_A', display_name: '测试昵称甲', job: '客服', is_active: true },
            { username: 'kefu_B', display_name: '测试昵称乙', job: '客服主管', is_active: false },
          ],
        },
      ],
    } as never)
    await openAlipay()
    await vi.waitFor(() => expect(mockedAlipay).toHaveBeenCalledTimes(1))
    const data = alipayRenderOpts().data as { str1: string }[]
    expect(data.map((row) => row.str1)).toEqual(['测试昵称甲', '测试昵称乙'])
    // 判据的另一半：**账号不出现**在「子账号名称」这一列里。
    expect(data.map((row) => row.str1)).not.toContain('kefu_A')
  })
})

describe('「订单状态」列那颗标签：照对标渲染，点了提示「尚未开放」', () => {
  it('点 `.setdlsta` ⇒ 一句提示 + **零请求、零弹层**', async () => {
    const root = await mountPage()
    renderAndFinish()
    LAYUI.table.reload.mockClear()
    const wrap = document.createElement('div')
    wrap.innerHTML = orderStatusTagHtml(toRow(ROW))
    root.appendChild(wrap)
    wrap.querySelector<HTMLElement>('a.setdlsta')!.click()
    expect(LAYUI.msg).toHaveBeenCalledWith('「修改订单状态」尚未开放')
    expect(LAYUI.table.reload).not.toHaveBeenCalled()
    expect(LAYUI.open).not.toHaveBeenCalled()
  })
})

describe('T4：「关联订单」列的可点蓝链 ⇒ 原地开层装订单详情复刻页（对标 `emplist.js` L968-983）', () => {
  /** 把 templet 的**真产物**插进页面，再点它（替身 layui 不会替我们渲染表格单元格）。 */
  async function clickOrderDetail(
    orderNo = '20260920213215233029',
  ): Promise<Record<string, unknown>> {
    const root = await mountPage()
    renderAndFinish()
    const wrap = document.createElement('div')
    wrap.innerHTML = orderIdTemplet(row({ order_no: orderNo }))
    root.appendChild(wrap)
    const link = wrap.querySelector<HTMLElement>('a.orderdetails')
    expect(link, 'templet 没有渲染出 a.orderdetails').not.toBeNull()
    // ⚠️ 首屏 `done` → `reload()` 是**挂载时**发生的（`renderAndFinish` 触发），与「开层」无关
    //    ⇒ 点之前清一次，下面的「零请求」判据才是在钉**点击这一下**。
    LAYUI.table.reload.mockClear()
    link!.click()
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    return LAYUI.open.mock.calls[0]![0] as Record<string, unknown>
  }

  it('⭐ 层参数逐字（title / shade / shadeClose / type / skin / anim / area = **本页自己的** `90%×92%`）', async () => {
    const opts = await clickOrderDetail()
    // 对标原话：`title: orderid + "订单详情"`；⭐ D-0301：前缀 = **展示单号**
    // （夹具行的 `sourceOrderNo = 'TB-合成-绩效-来源甲'` 非空 ⇒ 标题带来源号）。
    expect(opts.title).toBe('TB-合成-绩效-来源甲订单详情')
    expect(opts.shade).toEqual([0.3, '#333'])
    expect(opts.shadeClose).toBe(true)
    expect(opts.type).toBe(2)
    expect(opts.skin).toBe('layui-layer-lan')
    expect(opts.anim).toBe(8)
    // ⛔ `area` 是**本页自己的值**（`emplist.js:981`）：同一个产品里订单列表 / 我的订单是
    //    `85%×92%`、我的绩效是 `60%×92%`、绩效账单是 `1125px|80%×90%` ⇒ **不许抽共用模板**。
    //    期望值写字面值——改成 `['85%','92%']` 本条必须红。
    expect(opts.area).toEqual(['90%', '92%'])
  })

  it('⭐ `content` = 我们自己的复刻页（`page=order-orderview` + `order_no` = 那一行的单号）', async () => {
    const opts = await clickOrderDetail()
    // 尖括号里的 `<BASE_URL>` 在本工程是 `/`（vitest 的 base 与 dev 相同）。
    expect(opts.content).toBe(
      '/layui-island/page.html?page=order-orderview&order_no=20260920213215233029',
    )
    // 单号要进 URL ⇒ 编码；换个单号再点一次，确认它跟着**行**走（不是写死的第一个值）。
    LAYUI.open.mockClear()
    const opts2 = await clickOrderDetail('20260101010101010101')
    expect(opts2.content).toBe(
      '/layui-island/page.html?page=order-orderview&order_no=20260101010101010101',
    )
  })

  it('⭐ 开层**不发任何 API 请求**（数据由层里的 island 页自己取；本页只开层）', async () => {
    await clickOrderDetail()
    // 主表没有重取；六个业务出口一个都没被碰；也没有走错误提示。
    expect(LAYUI.table.reload).not.toHaveBeenCalled()
    expect(mockedAdjust).not.toHaveBeenCalled()
    expect(mockedDelete).not.toHaveBeenCalled()
    expect(mockedExport).not.toHaveBeenCalled()
    expect(mockedAlipay).not.toHaveBeenCalled()
    expect(mockedRevoke).not.toHaveBeenCalled()
    expect(mockedSettle).not.toHaveBeenCalled()
    expect(mockedNotifyError).not.toHaveBeenCalled()
  })

  it('`data-id` 为空的链不弹层（不拿空单号去开一个必然白屏的层）', async () => {
    const root = await mountPage()
    renderAndFinish()
    const wrap = document.createElement('div')
    wrap.innerHTML =
      '<a class="orderdetails" href="javascript:void(0);" data-id="" style="color:#0094ff"' +
      ' title="点击查看订单详情"></a>'
    root.appendChild(wrap)
    wrap.querySelector<HTMLElement>('a.orderdetails')!.click()
    expect(LAYUI.open).not.toHaveBeenCalled()
  })

  it('⛔ 开层前的**单号形状闸**：非 20 位数字 / 带标记的 `data-id` ⇒ **不开层** + 固定文案提示（T4 评审 Minor-2）', async () => {
    const root = await mountPage()
    renderAndFinish()
    // 四种「形状不对」：短一位 / 多一位 / 非数字 / 带可执行形态的标记串。
    // ⚠️ 最后一种正是 `escapeHtml` 在这条链上挡不住的那一类——`link.dataset.id` 会把 attribute
    //    实体**解码回** `<` / `>`，而 `layer.open` 的 `title` 是 HTML 上下文。
    const badShapes = [
      '1234567890123456789',
      '123456789012345678901',
      'abcdefghijklmnopqrst',
      '<img src=x onerror=alert(1)>',
    ]
    for (const value of badShapes) {
      const wrap = document.createElement('div')
      wrap.innerHTML =
        '<a class="orderdetails" href="javascript:void(0);" style="color:#0094ff"' +
        ' title="点击查看订单详情"></a>'
      wrap.querySelector('a.orderdetails')!.setAttribute('data-id', value)
      root.appendChild(wrap)
      wrap.querySelector<HTMLElement>('a.orderdetails')!.click()
    }
    expect(LAYUI.open).not.toHaveBeenCalled()
    // 提示是**固定文案**（⛔ 不把那份不可信的输入拼进来）；四次点击 = 四条提示。
    expect(LAYUI.msg).toHaveBeenCalledWith('订单号格式不正确，无法打开订单详情')
    expect(LAYUI.msg).toHaveBeenCalledTimes(badShapes.length)
  })
})

describe('⭐ 收款码入口的点击（图片存储线 Task 10；**无对标证据** ⇒ 没传过时给提示、不开空白层）', () => {
  /** 后端签发的短时读链接（形状照 `PerformanceListSerializer.wechat_qr_url`：全 URL、可带 `&`）。 */
  type QrItem = PerformanceListItem & { wechat_qr_url?: string | null }
  const QR_ROW: QrItem = {
    ...ROW,
    wechat_qr_url: 'https://cos.example.com/users/5/wechat-qr.png?sign=a&x=1',
  }
  /** 同页另一行（用来证明取的是**被点那一行**的链接，不是第一行 / 写死的那个）。 */
  const OTHER_ROW: QrItem = {
    ...ROW,
    id: 18,
    username: 'dailian_B',
    wechat_qr_url: 'https://cos.example.com/users/6/wechat-qr.png?sign=b',
  }

  /** 挂页 → 灌行（`parseData` 在这里留 `pageRows`）→ 按 templet 渲染那一格 → 点它。 */
  async function clickQr(item: QrItem, rows: QrItem[] = [item]): Promise<void> {
    const root = await mountPage()
    loadPage(rows)
    // 首屏 `done` → `reload()` 是**挂载时**发生的 ⇒ 点之前清一次，「零请求」判据才钉得住。
    LAYUI.table.reload.mockClear()
    LAYUI.msg.mockClear()
    const wrap = document.createElement('div')
    wrap.innerHTML = userNameTemplet(toRow(item))
    root.appendChild(wrap)
    const link = wrap.querySelector<HTMLElement>('a.viewWechatQr')
    expect(link, 'templet 没有渲染出 a.viewWechatQr').not.toBeNull()
    link!.click()
  }

  it('⭐ 点开 ⇒ `layer.open` 一个图片层：URL 取**被点那一行**的 `wechat_qr_url`、标题带该行的人', async () => {
    await clickQr(OTHER_ROW, [QR_ROW, OTHER_ROW])
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    const opts = LAYUI.open.mock.calls[0]![0] as Record<string, unknown>
    expect(opts.type).toBe(1)
    expect(opts.title).toBe('dailian_B的微信收款码')
    // URL 是请求数据 ⇒ 出口转义（`&` → `&amp;`）；层里必须是**这一行**的链接。
    expect(String(opts.content)).toContain(
      'src="https://cos.example.com/users/6/wechat-qr.png?sign=b"',
    )
    expect(String(opts.content)).not.toContain('sign=a')
  })

  it('层参数逐字（`type` / `title` / `content`；⚠️ 形态是**我方自拟**，无对标证据）', async () => {
    await clickQr(QR_ROW)
    const opts = LAYUI.open.mock.calls[0]![0] as Record<string, unknown>
    expect(opts).toEqual({
      type: 1,
      title: 'dailian_A的微信收款码',
      content:
        '<div style="padding:10px;text-align:center">' +
        '<img src="https://cos.example.com/users/5/wechat-qr.png?sign=a&amp;x=1"' +
        ' alt="微信收款码" style="max-width:260px;max-height:260px;display:block"></div>',
    })
  })

  it('⭐ 没有收款码（键缺席 ⇒ `wechatQrUrl` 为 `null`）⇒ **只提示、不开层**（任务书原话）', async () => {
    await clickQr({ ...ROW })
    expect(LAYUI.open).not.toHaveBeenCalled()
    expect(LAYUI.msg).toHaveBeenCalledWith('该人员还没有上传微信收款码')
  })

  it('⭐ 显式 `null` 也算「没有收款码」（E-0021：不能用真值判断把它与「没传过」混掉）', async () => {
    await clickQr({ ...ROW, wechat_qr_url: null })
    expect(LAYUI.open).not.toHaveBeenCalled()
    expect(LAYUI.msg).toHaveBeenCalledWith('该人员还没有上传微信收款码')
  })

  it('⭐ 空串同样算「没有收款码」（空串不是有效 URL）', async () => {
    await clickQr({ ...ROW, wechat_qr_url: '' })
    expect(LAYUI.open).not.toHaveBeenCalled()
    expect(LAYUI.msg).toHaveBeenCalledWith('该人员还没有上传微信收款码')
  })

  it('`data-id` 回查不到行（表格已换过一批数据）⇒ 提示刷新、零弹层', async () => {
    const root = await mountPage()
    loadPage([QR_ROW])
    LAYUI.msg.mockClear()
    const wrap = document.createElement('div')
    wrap.innerHTML = userNameTemplet(toRow({ ...QR_ROW, id: 99 }))
    root.appendChild(wrap)
    wrap.querySelector<HTMLElement>('a.viewWechatQr')!.click()
    expect(LAYUI.msg).toHaveBeenCalledWith('该行数据已失效，请刷新列表后重试')
    expect(LAYUI.open).not.toHaveBeenCalled()
  })

  it('⭐ 纯展示、**零 API 请求**（链接由后端在列表载荷里签好，本页只开层）', async () => {
    await clickQr(QR_ROW)
    expect(LAYUI.table.reload).not.toHaveBeenCalled()
    expect(mockedAdjust).not.toHaveBeenCalled()
    expect(mockedDelete).not.toHaveBeenCalled()
    expect(mockedExport).not.toHaveBeenCalled()
    expect(mockedAlipay).not.toHaveBeenCalled()
    expect(mockedRevoke).not.toHaveBeenCalled()
    expect(mockedSettle).not.toHaveBeenCalled()
    expect(mockedNotifyError).not.toHaveBeenCalled()
  })

  it('收款码 URL 里的 HTML 被转义（`src` 是 HTML 上下文；与 `.orderdetails` 同类出口）', async () => {
    await clickQr({ ...ROW, wechat_qr_url: '"><img src=x onerror=alert(1)>' })
    const opts = LAYUI.open.mock.calls[0]![0] as Record<string, unknown>
    expect(String(opts.content)).not.toContain('<img src=x')
    expect(String(opts.content)).toContain('&quot;&gt;&lt;img src=x onerror=alert(1)&gt;')
  })

  it('点它**不抢**其它行内入口的点击（`closest` 只认 `.viewWechatQr`）', async () => {
    const root = await mountPage()
    loadPage([QR_ROW])
    LAYUI.msg.mockClear()
    LAYUI.open.mockClear()
    const wrap = document.createElement('div')
    wrap.innerHTML = orderIdTemplet(toRow(QR_ROW))
    root.appendChild(wrap)
    wrap.querySelector<HTMLElement>('a.orderdetails')!.click()
    expect(LAYUI.msg).not.toHaveBeenCalledWith('该人员还没有上传微信收款码')
    // `.orderdetails` 自己那一层照旧开（证明两条委托互不干扰）。
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
  })
})

describe('筛选栏「搜索」（`seachBtn`）——2026-09-22 P4-FE-FIX I-2 补绑', () => {
  /** 模拟真人翻到第 n 页：layui 的 `done` 回调把真实 `curr` 交回来（同 `renderAndFinish` 的手法）。 */
  function goToPage(n: number): void {
    const opts = LAYUI.table.render.mock.calls[0]![0] as RenderOpts
    opts.done?.({ count: 50, data: [] }, n, 50)
  }

  it('提交 `seachBtn` ⇒ 调 `table.reload`（走同一个 `reload` 出口）、`where` 是当前控件的参数、`page.curr === 1`', async () => {
    await mountPage()
    renderAndFinish()
    // 先翻到第 3 页（`done` 是 currentPage 的唯一写入点）——再点搜索必须**回第 1 页**。
    goToPage(3)
    LAYUI.table.reload.mockClear()
    LAYUI.msg.mockClear()

    const handler = LAYUI.formHandlers.get('submit(seachBtn)')
    expect(handler, '本页必须绑 form.on("submit(seachBtn)")——否则点搜索零请求').toBeTypeOf(
      'function',
    )
    LAYUI.form.val.mockReturnValueOnce({
      dateType: '25',
      sDate: '2026-06-20 00:00:00',
      eDate: '2026-09-20 23:59:59',
      settingSta: 'N',
    })
    handler!()

    // ① 真的重新取数了（点搜索不再「零请求、表格不刷新」）
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
    const [id, cfg] = LAYUI.table.reload.mock.calls[0]! as [
      string,
      { where: Record<string, unknown>; page: { curr: number } },
    ]
    expect(id).toBe(TABLE_ID)
    // ② `where` = 当前控件的参数（`params()` 折出来的，不是另写一套拼参；
    //    后端支持更多，本页没接 `user`、也不再发 `review_status`/`project`）
    expect(cfg.where).toEqual({
      emp_type: '打单工资',
      settle_status: '未结账',
      // ⭐ 线 C：默认的录单档显式发 `date_type=created`（与首屏那一发同源）。
      date_type: 'created',
      date_from: '2026-06-20 00:00:00',
      date_to: '2026-09-20 23:59:59',
    })
    expect('page' in cfg.where).toBe(false)
    expect('page_size' in cfg.where).toBe(false)
    // ③ `page.curr === 1`（先翻到第 3 页再点搜索，必须回 1）
    expect(cfg.page).toEqual({ curr: 1 })
  })

  it('与工具栏「刷新列表」共用同一个出口：刷新**保持当前页**（=3），搜索才回第 1 页', async () => {
    await mountPage()
    renderAndFinish()
    goToPage(3)
    LAYUI.table.reload.mockClear()

    // 先单独点工具栏「刷新列表」：它**不动** currentPage（仍是 3）⇒ 这是与「搜索」的可见差别
    LAYUI.handlers.get(`toolbar(${TABLE_ID})`)!({ event: 'refresh' } as never)
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
    const refresh = LAYUI.table.reload.mock.calls[0]![1] as { page: { curr: number } }
    expect(refresh.page).toEqual({ curr: 3 })

    // 再点「搜索」：同一个 `reload` 出口（`where` 与刷新那一发同源），页码回 1
    LAYUI.table.reload.mockClear()
    LAYUI.formHandlers.get('submit(seachBtn)')!()
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
    const search = LAYUI.table.reload.mock.calls[0]![1] as { page: { curr: number } }
    expect(search.page).toEqual({ curr: 1 })
    expect((search as { where?: unknown }).where).toEqual(
      (refresh as { where?: unknown }).where,
    )
  })

  it('⭐⭐ 五档**每一档**都有一条**真实出口**用例：`#dateType` 的原始值 ⇒ `date_type` 进请求', async () => {
    // ⭐⭐ 计划 E5 ①：五档**各有**一条真实出口用例——逐档走**真页面**的「搜索」出口，
    //    断言 `table.reload` 的 `where`（真 layui 拿它拼 XHR 查询串）里带的是**那一档**的
    //    ⚠️ 期望值**逐档逐字写死**（⛔ 不从 `DATE_TYPE_MAP` 派生——那样映射写错也绿）。
    const expected: Array<[string, string]> = [
      ['10', 'published'],
      ['20', 'submitted'],
      ['25', 'created'],
      ['30', 'settled'],
      ['40', 'completed'],
    ]
    for (const [raw, want] of expected) {
      await mountPage()
      LAYUI.form.val.mockReturnValue({
        dateType: raw,
        sDate: '2026-06-20 00:00:00',
        eDate: '2026-09-20 23:59:59',
        settingSta: '',
      })
      renderAndFinish()
      LAYUI.table.reload.mockClear()
      LAYUI.msg.mockClear()

      LAYUI.formHandlers.get('submit(seachBtn)')!()

      const cfg = LAYUI.table.reload.mock.calls[0]![1] as { where: Record<string, unknown> }
      expect({ raw, got: cfg.where.date_type }).toEqual({ raw, got: want })
      // 日期两端与 `date_type` **同一次**进请求（五档都不例外）。
      expect(cfg.where.date_from).toBe('2026-06-20 00:00:00')
      expect(cfg.where.date_to).toBe('2026-09-20 23:59:59')
      // 逐档收口：全局 `afterEach` 会清 `document.body`，但本用例自己在循环里重复挂载
      // ⇒ 显式收一次，免得上一档的表格实例被下一档的 `params()` 读到（同文件既有的
      // 「每个用例自己收口」纪律，见 AGENTS §5-G 第 ④ 条的同族）。
      document.body.innerHTML = ''
    }
  })

  it('⭐ 发单时间档：点搜索 ⇒ **发 `date_type=published`** 且**一句提示都不弹**（与刷新同一口径）', async () => {
    // ⭐⭐ 发单档**真接线**（打 `Order.published_at`）⇒ 「弹尚未开放」「不带 date_type」
    //    两条旧期望都不该成立（留着正是设计 §2.5 点名要删的死路径）。
    await mountPage()
    renderAndFinish()
    LAYUI.table.reload.mockClear()
    LAYUI.msg.mockClear()
    LAYUI.form.val.mockReturnValueOnce({
      dateType: '10',
      sDate: '2026-06-20 00:00:00',
      eDate: '2026-09-20 23:59:59',
      settingSta: '',
    })

    LAYUI.formHandlers.get('submit(seachBtn)')!()

    expect(LAYUI.msg).not.toHaveBeenCalled()
    const cfg = LAYUI.table.reload.mock.calls[0]![1] as { where: Record<string, unknown> }
    expect(cfg.where).toMatchObject({
      date_type: 'published',
      date_from: '2026-06-20 00:00:00',
      date_to: '2026-09-20 23:59:59',
    })
  })

  it('⭐ N-1：日期填了不可识别的值 ⇒ 弹「本次已忽略该筛选」，且那个参数确实**没发**（不再静默放宽结果集）', async () => {
    await mountPage()
    renderAndFinish()
    LAYUI.table.reload.mockClear()
    LAYUI.msg.mockClear()
    LAYUI.form.val.mockReturnValueOnce({
      dateType: '25',
      sDate: '不是日期',
      eDate: '2026-09-20 23:59:59',
      settingSta: '',
    })

    LAYUI.formHandlers.get('submit(seachBtn)')!()

    expect(LAYUI.msg).toHaveBeenCalledWith('「录单时间」不是可识别的日期，本次已忽略该筛选')
    // 请求照发（**不新增拦截**，与对标同形）——只是参数少一个、并且这次**告诉了用户**
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
    const cfg = LAYUI.table.reload.mock.calls[0]![1] as { where: Record<string, unknown> }
    expect('date_from' in cfg.where).toBe(false)
    expect(cfg.where.date_to).toBe('2026-09-20 23:59:59')
  })

  it('⭐ N-1 阴性对照：首屏默认（合法日期）**不弹**那句提示（否则每次进页面都莫名其妙多一句）', async () => {
    await mountPage()
    renderAndFinish()
    expect(LAYUI.msg).not.toHaveBeenCalledWith('「录单时间」不是可识别的日期，本次已忽略该筛选')
  })
})

/* ──────────────── 「结账状态」列 `.setSett`：开「清算绩效」子页弹窗 ──────────────── */

/**
 * 一页真载荷（结账流程的最小集 + 第五种形态）：
 * - 甲（`user=5`）：11、12 **未结账**、13 **已结账**（都 `review_status=已通过`）
 * - 乙（`user=6`）：21 **未结账 + 已通过**、22 **未结账 + `review_status=待审核`**
 * - 无归属人（`user=null`）：31 **未结账**（`username` 也是 `null` ⇒ 行载荷的 `userName` 是 `''`；
 *   `review_status` 也只能是 `null`——DB 约束 `performance_review_needs_user` 禁止无主行有过审状态）
 *
 * ⚠️ `review_status` **逐行显式写**：基行 `ROW` 是 `待审核`，不写就会让「已通过」的行也带上 `待审核`，
 * 于是「前端不筛 `review_status`」这条判据**根本区分不出来**（判据与干扰项同值）。
 *
 * ⭐ 结账**按行** ⇒ 点 11 只把 `11` 交给子页：不含同人的 12（同批会一起发的错法）、
 * 不含已结账的 13（⚠️ **已结账的行也照开层**，只是 `ids` 仍是它自己）、
 * 不含乙的 21/22、不含无主的 31。
 * ⭐ 无主的 31 **前端直接挡掉、不开层**（体验层；后端结账的判据里有 `user__isnull=False`，
 * 所以它**一定**结不了——D-0074。业务方裁定「这类行不把请求发出去」）；
 * 有主但未过审的 22 **照开层**（`review_status` **不是**结账判据，D-0071 ③）。
 */
const SETTLE_ROWS: PerformanceListItem[] = [
  { ...ROW, id: 11, user: 5, username: 'dailian_A', review_status: '已通过', settle_status: '未结账' },
  { ...ROW, id: 12, user: 5, username: 'dailian_A', review_status: '已通过', settle_status: '未结账' },
  { ...ROW, id: 13, user: 5, username: 'dailian_A', review_status: '已通过', settle_status: '已结账' },
  { ...ROW, id: 21, user: 6, username: 'dailian_B', review_status: '已通过', settle_status: '未结账' },
  {
    ...ROW,
    id: 22,
    user: 6,
    username: 'dailian_B',
    review_status: '待审核',
    settle_status: '未结账',
  },
  { ...ROW, id: 31, user: null, username: null, review_status: null, settle_status: '未结账' },
]

/** 无归属人那一行（`user` / `username` 都是 `null`）。 */
const ORPHAN = SETTLE_ROWS[5]!

/** 有归属人、但 `review_status=待审核` 的那一行（前端**不**挡它）。 */
const UNAPPROVED = SETTLE_ROWS[4]!

/**
 * 把一页行按**真 layui 的顺序**灌进页面：先 `parseData`（页面在这里留 `pageRows`）、再 `done`
 * （首屏那次补取数挂在 `done` 里）。`renderAndFinish` 只能喂 `count`，结账的用例需要真行。
 */
function loadPage(items: PerformanceListItem[]): void {
  const opts = LAYUI.table.render.mock.calls[0]![0] as {
    parseData: (r: unknown) => unknown
    done?: (r: { count: number; data: unknown[] }, curr: number, count: number) => void
  }
  const parsed = opts.parseData({ code: 0, msg: '', data: { items, total: items.length } })
  expect((parsed as { data: unknown[] }).data).toHaveLength(items.length)
  opts.done?.({ count: items.length, data: [] }, 1, items.length)
}

/** 按**函数 templet** 渲染某行的「结账状态」标签并挂进页面（返回那颗 `a.setSett`）。 */
function renderSettleTag(root: HTMLElement, item: PerformanceListItem): HTMLElement {
  const wrap = document.createElement('div')
  wrap.innerHTML = settleTagHtml(toRow(item))
  root.appendChild(wrap)
  const link = wrap.querySelector<HTMLElement>('a.setSett')
  expect(link).not.toBeNull()
  return link!
}

/** 点某行的 `.setSett` 标签（先真灌一页行——标签的 `data-id` 靠 `pageRows` 回查）。 */
async function clickSettleTag(
  item: PerformanceListItem,
  rows: PerformanceListItem[] = SETTLE_ROWS,
): Promise<HTMLElement> {
  const root = await mountPage()
  loadPage(rows)
  const link = renderSettleTag(root, item)
  link.click()
  return link
}

/** 那一刻 `layer.open` 的实参（先钉「恰好一次」）。 */
function openedSettleLayer(): Record<string, unknown> {
  expect(LAYUI.open).toHaveBeenCalledTimes(1)
  return LAYUI.open.mock.calls[0]![0] as Record<string, unknown>
}

describe('结账状态列：`.lableb setSett` 标签（templet **逐字**照抄 `emplist.js` L451）', () => {
  it('未结账：`#CDC8B1` + title「该笔订单还未进行结账」+ `data-id` 是绩效行主键', () => {
    expect(settleTagHtml(toRow(SETTLE_ROWS[0]!))).toBe(
      '<a href="javascript:void(0)" class="lableb setSett" data-id="11"' +
        ' title="该笔订单还未进行结账" style="background-color:#CDC8B1;">未结账</a>',
    )
  })

  it('已结账：`#9ACD32` + title「该笔订单已结账过」，文案仍是后端真值', () => {
    expect(settleTagHtml(toRow(SETTLE_ROWS[2]!))).toBe(
      '<a href="javascript:void(0)" class="lableb setSett" data-id="13"' +
        ' title="该笔订单已结账过" style="background-color:#9ACD32;">已结账</a>',
    )
  })

  it('「订单状态」/「结账状态」两列都是**函数 templet**（T3 之后 15 列、修复轮加 `orderId` ⇒ 16 列）', () => {
    // ⚠️ 完整的名单在「列声明」那一组里（那条只钉名单，这里只钉这两列在名单里）。
    for (const field of ['orderSta', 'settingSta']) {
      expect(
        COLS.filter((c) => typeof c.templet === 'function').map((c) => c.field),
      ).toContain(field)
    }
    expect(COLS).toHaveLength(20)
  })

  /**
   * ⭐ 2026-10-23（提前结算申请）：这一行申请过 ⇒ 在本格内**多一颗**「已申请」标记。
   * ⚠️ 只在现有列上加标记：列数 / 列宽一个字不动（上一条的 20 列断言照旧钉着）。
   * ⚠️ 下面的 `toBe` 是**整串**断言：既钉住「多出的那一颗」，也钉住
   * 「原来那颗 `.setSett` 一个字没被改」。
   */
  it('申请过提前结算 ⇒ 同一格多一颗「已申请」标记（原标签逐字不变）', () => {
    const html = settleTagHtml(
      toRow({ ...SETTLE_ROWS[0]!, settle_requested_at: '2026-10-23T10:05:00+08:00' }),
    )

    expect(html).toBe(
      '<a href="javascript:void(0)" class="lableb setSett" data-id="11"' +
        ' title="该笔订单还未进行结账" style="background-color:#CDC8B1;">未结账</a>' +
        '<span class="lableb settleRequestMark"' +
        ' title="该笔绩效已申请提前结算（申请时间：2026-10-23 10:05）"' +
        ' style="background-color:#6A5ACD;margin-left:2px;">已申请</span>',
    )
    // 承重：标记真的挂在这一格（不是别的地方）——`data-id` 仍在原标签上、可点结账。
    expect(html).toContain('class="lableb setSett" data-id="11"')
    expect(html).toContain('已申请')
  })

  it('没申请（`null` / 键缺席 / 空串）⇒ 产出与加标记之前**逐字节相同**', () => {
    const plain =
      '<a href="javascript:void(0)" class="lableb setSett" data-id="11"' +
      ' title="该笔订单还未进行结账" style="background-color:#CDC8B1;">未结账</a>'

    expect(settleTagHtml(toRow({ ...SETTLE_ROWS[0]!, settle_requested_at: null }))).toBe(plain)
    // 键整个不在（旧后端 / 部署窗口）
    expect(settleTagHtml(toRow(SETTLE_ROWS[0]!))).toBe(plain)
    // 空串也按「没有」处理（不冒出空标记）
    expect(settleTagHtml(toRow({ ...SETTLE_ROWS[0]!, settle_requested_at: '' }))).toBe(plain)
  })

  it('申请时刻原样进 `toRow`（`settleRequestedAt`），没申请是 `null`', () => {
    expect(
      toRow({ ...ROW, settle_requested_at: '2026-10-23T10:05:00+08:00' }).settleRequestedAt,
    ).toBe('2026-10-23T10:05:00+08:00')
    expect(toRow(ROW).settleRequestedAt).toBeNull()
  })
})

describe('点 `.setSett` ⇒ 开**同一个**「清算绩效」子页弹窗（对标 `settSta`，2026-10-03 Task 4）', () => {
  it('未结账行 ⇒ 开层，实参**逐字**（与工具栏同一个出口）、`ids` 只有**那一个**绩效主键', async () => {
    await clickSettleTag(SETTLE_ROWS[0]!) // id=11，未结账，同人还有 12 / 已结账的 13
    expect(LAYUI.open.mock.calls[0]![0]).toEqual({
      title: '清算绩效',
      shade: [0.3, '#333'],
      shadeClose: true,
      type: 2,
      skin: 'layui-layer-lan',
      anim: 8,
      area: ['80%', '50%'],
      content: '/layui-island/page.html?page=emp-settlementemp&ids=11',
    })
    // ⛔ 不多不少：不含同人的 12（上一版会把同一人的一批一起结）、不含已结账的 13、不含无主的 31。
    expect(String(openedSettleLayer().content).endsWith('ids=11')).toBe(true)
    expect(mockedSettle).not.toHaveBeenCalled()
    expect(mockedRevoke).not.toHaveBeenCalled()
  })

  it('⭐ **已结账**的行 ⇒ **照样开同一个弹窗**（对标无条件开层；旧版「只提示、不开层」已删）', async () => {
    await clickSettleTag(SETTLE_ROWS[2]!) // id=13，已结账
    // 旧行为那句提示**不该**再出现。
    expect(LAYUI.msg).not.toHaveBeenCalledWith('该笔订单已结账过')
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    const opts = openedSettleLayer()
    expect(opts.title).toBe('清算绩效')
    expect(opts.type).toBe(2)
    expect(opts.area).toEqual(['80%', '50%'])
    expect(String(opts.content)).toBe(
      '/layui-island/page.html?page=emp-settlementemp&ids=13',
    )
    // 改成哪个状态由用户在子页里选 ⇒ 本页零写请求。
    expect(mockedSettle).not.toHaveBeenCalled()
    expect(mockedRevoke).not.toHaveBeenCalled()
  })

  it('点 12 ⇒ 只把 `12` 交给子页，11 不受影响（粒度 = 被点的那一行）', async () => {
    await clickSettleTag(SETTLE_ROWS[1]!) // id=12
    expect(String(openedSettleLayer().content)).toBe(
      '/layui-island/page.html?page=emp-settlementemp&ids=12',
    )
  })

  it('⭐ 无归属人的未结账行 ⇒ 只提示、**不开层、零请求**（闸门保留，对标不存在这种行）', async () => {
    // 先坐实「这一行确实渲染成**未结账**的标签」——否则这条用例会退化成别的情形。
    const link = await clickSettleTag(ORPHAN)
    expect(link.textContent).toBe('未结账')
    // 文案如实说明原因（后端结账的判据是「订单已完成 + 有归属人」，与审核状态无关）。
    expect(LAYUI.msg).toHaveBeenCalledWith('该笔绩效还没有接单人，不能结账')
    expect(LAYUI.open).not.toHaveBeenCalled()
    expect(mockedSettle).not.toHaveBeenCalled()
    expect(mockedRevoke).not.toHaveBeenCalled()
    // 只有挂载时那次首屏取数。
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
  })

  it('⭐ 有归属人但 `review_status=待审核` 的行 ⇒ **照开层**（前端不复制后端的 `review_status` 判据）', async () => {
    // 承重用例：前端只挡「没有归属人」那一种；未过审照开——后端结账的判据是
    // 「订单已完成 + 有归属人 + 未结账」，**与审核状态无关**（D-0071 ③）。
    await clickSettleTag(UNAPPROVED) // id=22
    expect(String(openedSettleLayer().content)).toBe(
      '/layui-island/page.html?page=emp-settlementemp&ids=22',
    )
    expect(LAYUI.msg).not.toHaveBeenCalledWith('该笔绩效还没有接单人，不能结账')
  })

  it('⭐ 点这一列**不再发** `settle` / `revoke`、也不弹原因层 / 确认层（旧行为的两条写端点一次都不打）', async () => {
    await clickSettleTag(SETTLE_ROWS[0]!)
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    expect(mockedSettle).not.toHaveBeenCalled()
    expect(mockedRevoke).not.toHaveBeenCalled()
    expect(LAYUI.layer.prompt).not.toHaveBeenCalled()
    expect(LAYUI.layer.confirm).not.toHaveBeenCalled()
    // 也不重取列表（刷新由子页成功后的 `parent.tools.reloadOrderData()` 触发）。
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
  })

  it('`data-id` 回查不到行（表格已换过一批数据）⇒ 提示刷新、不开层、零请求', async () => {
    const root = await mountPage()
    loadPage(SETTLE_ROWS)
    // 手工造一颗「上一批数据」的标签（id=99 不在 pageRows 里）
    const stale = renderSettleTag(root, { ...SETTLE_ROWS[0]!, id: 99 })
    stale.click()
    expect(LAYUI.msg).toHaveBeenCalledWith('该行数据已失效，请刷新列表后重试')
    expect(LAYUI.open).not.toHaveBeenCalled()
    expect(mockedSettle).not.toHaveBeenCalled()
  })

  it('结账标签**不抢**工具栏/页签的点击（`closest` 只认 `.setSett`）', async () => {
    const root = await mountPage()
    loadPage(SETTLE_ROWS)
    const other = document.createElement('a')
    other.className = 'lableb setsta'
    other.textContent = '未结账'
    root.appendChild(other)
    other.click()
    expect(LAYUI.open).not.toHaveBeenCalled()
    expect(LAYUI.msg).not.toHaveBeenCalled()
  })
})

describe('⭐ `window.tools`：子页 iframe 用它刷新父页列表（对标 `_tools`，`emplist.js` L1964-1974）', () => {
  type ToolsHost = { tools?: { reloadOrderData: () => void } }

  it('`reloadOrderData()` ⇒ `layer.closeAll()` 被调 + 本页就地重取（**真调一次**，不是只断 `typeof`）', async () => {
    await mountPage()
    renderAndFinish()
    LAYUI.table.reload.mockClear()
    const host = window as unknown as ToolsHost
    expect(host.tools).toBeTypeOf('object')
    // 子页正是这么调的：`parent.tools.reloadOrderData()` ⇒ 方法里的 `this` 就是这个对象。
    // ⚠️ 函数体不读 `this`（对标那两条也不读，用的是闭包里的 `layer` / `reloadOrderList`），
    //    所以这里钉的是**调用形态**：方法挂在对象上、不靠 `bind` 也能这么调。
    host.tools!.reloadOrderData()
    expect(LAYUI.layer.closeAll).toHaveBeenCalledTimes(1)
    // 重取走的是本页那个 `reload()` 出口（`where` = 当前筛选、`page.curr` = 当前页）。
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
    const [id, cfg] = LAYUI.table.reload.mock.calls[0]! as [
      string,
      { where: Record<string, unknown>; page: { curr: number } },
    ]
    expect(id).toBe(TABLE_ID)
    expect(cfg.where).toEqual({
      emp_type: '打单工资',
      // ⭐ 线 C：默认档显式发 `date_type=created`。
      date_type: 'created',
      date_from: '2026-06-20 00:00:00',
      date_to: '2026-09-20 23:59:59',
    })
    expect(cfg.page).toEqual({ curr: 1 })
  })

  it('卸载时让出 `window.tools`（别把闭包留在全局）', async () => {
    const root = document.createElement('div')
    document.body.appendChild(root)
    const { default: page } = await import('../emp-emplist')
    const unmount = await page.mount(root, { onNavigate: vi.fn() } as never)
    const host = window as unknown as ToolsHost
    expect(host.tools).toBeTypeOf('object')
    unmount()
    expect(host.tools).toBeUndefined()
  })
})

describe('⭐ 子路径部署（`BASE_URL=/console/`）：**页面自己**拼出来的 `content` 必须带 `/console` 前缀', () => {
  /**
   * 这条是上面 `settlementDialogContent([…], '/console/')` 的**承重补充**：那一条测的是**函数**，
   * 页面若绕过它、自己写死 `/layui-island/…`，在 `BASE_URL='/'` 下产物**一模一样**、测不出来
   * （实测：只把 `openSettlementDialog` 里的 `islandPageUrl(...)` 换成写死路径时，上面所有用例仍全绿）。
   * ⇒ 这里在**新模块注册表**里以 `/console/` 重新加载整页、真挂载、真点工具栏，断整串字面值。
   *
   * ⚠️ 手法：`vi.stubEnv('BASE_URL', …)` + `vi.resetModules()` 后再动态 import —— 实测可行
   * （`PAGE_BASE` 是模块顶层算的，只有重新加载模块才会重算）。`finally` 里两条都还原，
   * 否则 `/console/` 会漏给后面的用例。
   */
  it('点 `settBtn` ⇒ `content` 是 `/console/layui-island/page.html?page=…&ids=…`', async () => {
    vi.stubEnv('BASE_URL', '/console/')
    vi.resetModules()
    try {
      const fresh = await import('../emp-emplist')
      const root = document.createElement('div')
      document.body.appendChild(root)
      const unmount = await fresh.default.mount(root, { onNavigate: vi.fn() } as never)
      LAYUI.table.checkStatus.mockReturnValueOnce({
        data: [fresh.toRow({ ...ROW, id: 11, settle_status: '未结账' })],
      })
      LAYUI.handlers.get(`toolbar(${fresh.TABLE_ID})`)!({ event: 'settBtn' } as never)
      expect(LAYUI.open).toHaveBeenCalledTimes(1)
      // 整串字面值（E-0024）：写死 `/layui-island/…` 的回归在这里必红。
      expect(LAYUI.open.mock.calls[0]![0]).toMatchObject({
        content: '/console/layui-island/page.html?page=emp-settlementemp&ids=11',
      })
      unmount()
    } finally {
      vi.unstubAllEnvs()
      vi.resetModules()
    }
  })
})