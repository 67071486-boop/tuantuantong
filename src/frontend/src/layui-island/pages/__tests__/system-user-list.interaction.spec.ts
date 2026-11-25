// @vitest-environment jsdom
/**
 * 人员管理页的挂载层交互测试：三颗按钮接线（设自助接单数 / 禁·解结算 / 检测功能取数）
 * + 两个开关列变真。
 *
 * 为什么单写一份而不用 `system-user-list.spec.ts`：那一份钉的是**声明式**导出（列/templet/文案），
 * 而这里要驱动**真点击 / 真 form 事件**——三条写入路径都在 `mountUserListPage` 的闭包里，
 * 只断言导出的常量等于同义反复。手法照 `system-user-list-cards-mount.spec.ts`（layui 换成可控替身）。
 *
 * 断言全部打在**真实出口**上：`updateAccount` 的 (id, 载荷) 与 `table.render` 的入参。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/** layui 的可控替身（`vi.hoisted`：`vi.mock` 工厂会被提升到 import 之前）。 */
const LAYUI = vi.hoisted(() => {
  const tableHandlers = new Map<string, (...a: unknown[]) => unknown>()
  const formHandlers = new Map<string, (...a: unknown[]) => unknown>()
  const spinner = {
    tableRender: vi.fn(),
    tableReload: vi.fn(),
    formRender: vi.fn(),
    dropdownRender: vi.fn(),
    // 接单统计卡片那两个日期框要的（缺了它挂载就炸在
    // `laydate.render` 上，看起来像页面代码错、其实是桩不完整）。
    laydateRender: vi.fn(),
    open: vi.fn((_o: unknown) => 7),
    full: vi.fn(),
    close: vi.fn(),
    msg: vi.fn(),
    prompt: vi.fn(),
    hide: vi.fn(),
  }
  const jq = vi.fn((selector: string) => ({ hide: spinner.hide, selector }))
  const layui = {
    device: () => ({ android: false, ios: false }),
    $: jq,
    form: {
      render: spinner.formRender,
      on: (e: string, cb: (...a: unknown[]) => unknown) => {
        formHandlers.set(e, cb)
        return formHandlers
      },
      val: vi.fn(() => ({})),
    },
    table: {
      render: spinner.tableRender,
      reload: spinner.tableReload,
      // 默认没有勾选行；用例按需换成 `{ data: rows }`。
      checkStatus: () => ({ data: [] as never[] }),
      on: (e: string, cb: (...a: unknown[]) => unknown) => {
        tableHandlers.set(e, cb)
        return tableHandlers
      },
    },
    layer: {
      msg: spinner.msg,
      open: spinner.open,
      full: spinner.full,
      close: spinner.close,
      confirm: vi.fn(),
      prompt: spinner.prompt,
    },
    dropdown: { render: spinner.dropdownRender },
    laydate: { render: spinner.laydateRender },
  }
  return { tableHandlers, formHandlers, jq, layui, ...spinner }
})

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return {
    ...actual,
    bootstrapIsland: (async () => LAYUI.layui) as unknown as typeof actual.bootstrapIsland,
    initIslandElements: vi.fn(),
  }
})

vi.mock('@/layui/vendor', () => ({ initFormSelects: vi.fn() }))

// ⭐ 挂载时会 `fetchMe()` + `canNode(...)` 判「押金额度」按钮
// 画不画。不打桩就会走真 `request()`（jsdom 里发真请求、拖慢整份用例）。
// 每个用例自己决定身份；`beforeEach` 默认给**没有任何节点**的用户 ⇒ 既有用例看到的仍是 14 颗。
vi.mock('@/api/auth', () => ({ fetchMe: vi.fn() }))

vi.mock('@/api/config', () => ({
  // 本页挂载取的是**全量** `fetchAllGames()`（配置面，防
  //    「看不到没勾的游戏 ⇒ 也就勾不上」的死锁，见 `system-user-list.spec.ts` 的防死锁 pin）；
  //    `fetchGames` 只是同模块的另一个导出，桩里留着以免同一页面出现两处来源。
  fetchGames: vi.fn(async () => [{ id: 1, name: '原神' }]),
  fetchAllGames: vi.fn(async () => [{ id: 1, name: '原神' }]),
}))

vi.mock('@/api/accounts', () => ({
  // ⚠️ `deleteAccount` / `restoreAccount` 必须有定义与真实类型 ⇒
  //    那两条路径在页测试里根本没被点过，「恢复链接点不动」才会静默通过。
  deleteAccount: vi.fn(async () => ({ id: 0 })),
  fetchAccountRoles: vi.fn(async () => ['客服']),
  listAccounts: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 200 })),
  // ⚠️ 工厂里这几个返回 Promise 的都要给**与真实签名一致**的解析值：`resetAccountPassword`
  // 的真实返回是 `Promise<{id:number}>`，这里写 `undefined` 与类型不符。
  resetAccountPassword: vi.fn(async () => ({ id: 0 })),
  restoreAccount: vi.fn(async () => ({ id: 0 })),
  setAccountStatus: vi.fn(),
  setAccountsStatus: vi.fn(),
  updateAccount: vi.fn(async () => ({})),
}))

vi.mock('@/api/performances', () => ({
  fetchAlipayDuplicates: vi.fn(async () => ({ duplicates: [] })),
}))

// ⭐ 行内「个人押金额度」开关的写入出口。只换网络出口——`TAKER_DEPOSIT_QUOTA_NODE`
// 等常量仍走真实现（页面把节点字面量住在那个模块里，替身漏了它挂载就判不出权限）。
vi.mock('@/api/takerDepositQuota', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/takerDepositQuota')>()
  return { ...actual, setDepositQuotaEnabled: vi.fn() }
})

import type { AccountRow, AccountWritePayload } from '@/api/accounts'
import { deleteAccount, resetAccountPassword, restoreAccount, updateAccount } from '@/api/accounts'
import { fetchMe } from '@/api/auth'
import { HttpError } from '@/api/errors'
import { fetchAlipayDuplicates } from '@/api/performances'
import { setDepositQuotaEnabled } from '@/api/takerDepositQuota'
import {
  buildCols,
  DEPOSIT_QUOTA_COLUMN,
  DEPOSIT_QUOTA_DIALOG_BASE,
  IDENTITY_TAB_ACTIVE_CLASS,
  IDENTITY_TAB_CLASS,
  IDENTITY_TABS,
  depositQuotaDialogContent,
  mountUserListPage,
} from '../system-user-list'

const updateMock = vi.mocked(updateAccount)
const alipayMock = vi.mocked(fetchAlipayDuplicates)
const restoreMock = vi.mocked(restoreAccount)
const resetPasswordMock = vi.mocked(resetAccountPassword)
const deleteMock = vi.mocked(deleteAccount)
const fetchMeMock = vi.mocked(fetchMe)
const depositQuotaSwitchMock = vi.mocked(setDepositQuotaEnabled)

/** `onNavigate` 的替身（「编辑」那颗链接断言它被调到）。 */
const navigateMock = vi.fn()

/** 让 `fetch`/Promise 链跑完（替身都是已 resolve 的 promise，两跳足够）。 */
const flush = async (): Promise<void> => {
  await new Promise((resolve) => setTimeout(resolve, 0))
}

async function mountPage(): Promise<HTMLElement> {
  const root = document.createElement('div')
  document.body.appendChild(root)
  await mountUserListPage(root, { onNavigate: navigateMock })
  // 真 layui 会把 toolbar 模板搬进 `.layui-table-tool`；替身不搬 ⇒ 补一份真按钮驱动委托。
  // ⚠️ 必须取**页面自己渲染出来的那份模板**（`#userToolbarTpl`），不能写 `toolbarHtml()`：
  // 「押金额度」是**按节点渲染**的自有按钮，只有持 `finance.taker_wallet_adjust` 的人那一次
  // 挂载才会把它写进模板。写死 `toolbarHtml()` 会让权限门那几条用例永远看不到它（假绿）。
  root.insertAdjacentHTML('beforeend', document.getElementById('userToolbarTpl')?.textContent ?? '')
  return root
}

/**
 * 把「操作」列的 templet 输出塞进 DOM —— 真 layui 会把 templet 渲染进单元格，替身不渲染，
 * 所以这里补一份**真标记**，让点击走**真的** `root` 委托链（而不是直接调页面内部函数，
 * 后者证明不了「这颗链接点了有反应」，正是本轮 Important 的教训）。
 */
function mountOperationCell(root: HTMLElement, row: Partial<AccountRow>): HTMLElement {
  const col = buildCols(new Map()).find((c) => c.title === '操作')
  expect(col, '主表必须有「操作」列').toBeTruthy()
  const html = (col!.templet as unknown as (d: Record<string, unknown>) => string)(row)
  root.insertAdjacentHTML('beforeend', `<div id="op-cell">${html}</div>`)
  return root.querySelector<HTMLElement>('#op-cell')!
}

/**
 * 按列标题取 templet 输出塞进 DOM（`mountOperationCell` 的通用版）。
 * 要分别驱动「操作」列与「登录账号」列两条**同一个** `act === 'edit'` 分支的入口。
 */
function mountColumnCell(root: HTMLElement, title: string, row: Partial<AccountRow>): HTMLElement {
  const col = buildCols(new Map()).find((c) => c.title === title)
  expect(col, `主表必须有「${title}」列`).toBeTruthy()
  const html = (col!.templet as unknown as (d: Record<string, unknown>) => string)(row)
  const id = `cell-${title}`
  root.insertAdjacentHTML('beforeend', `<div id="${id}">${html}</div>`)
  return root.querySelector<HTMLElement>(`#${id}`)!
}

function clickToolbar(root: HTMLElement, action: string): void {
  const btn = root.querySelector<HTMLButtonElement>(`[data-tb="${action}"]`)
  expect(btn, `主工具栏没有 [data-tb=${action}]`).toBeTruthy()
  btn!.click()
}

/** 把「勾选行」喂给 `table.checkStatus`（替身默认回空）。 */
function stubCheckedRows(rows: Partial<AccountRow>[]): void {
  LAYUI.layui.table.checkStatus = () => ({ data: rows as never[] })
}

/** 取某次 `layer.prompt` 的回调，并驱动它（模拟用户输入后点确定）。 */
function answerPrompt(value: string): void {
  const call = LAYUI.prompt.mock.calls[0]
  expect(call, 'layer.prompt 没有被调用').toBeTruthy()
  const cb = call![1] as (v: string, i: number) => void
  cb(value, 3)
}

/** 取渲染某只 `elem` 的 `table.render` 入参（同名多次取**最后一次**）。 */
function lastRenderOptsFor(elem: string): Record<string, unknown> {
  const calls = LAYUI.tableRender.mock.calls.filter((c) => (c[0] as { elem?: string }).elem === elem)
  expect(calls.length, `table.render 没有渲染 ${elem}`).toBeGreaterThan(0)
  return calls[calls.length - 1]![0] as Record<string, unknown>
}

beforeEach(() => {
  vi.clearAllMocks()
  LAYUI.open.mockReturnValue(7)
  updateMock.mockResolvedValue({} as AccountRow)
  alipayMock.mockResolvedValue({ duplicates: [] })
  restoreMock.mockResolvedValue({ id: 0 })
  resetPasswordMock.mockResolvedValue({ id: 0 })
  deleteMock.mockResolvedValue({ id: 0 })
  // 默认身份：**没有任何节点** ⇒ 「押金额度」按钮不画（既有用例看到的仍是 14 颗）。
  fetchMeMock.mockResolvedValue({ menu_permissions: [] } as never)
  depositQuotaSwitchMock.mockResolvedValue({} as never)
  LAYUI.layui.table.checkStatus = () => ({ data: [] as never[] })
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('Task 7：设自助接单数（一个 prompt，写两个上限）', () => {
  it('点按钮弹 prompt（title 逐字）；输入 5 ⇒ 每个勾选行都 PATCH **两个**上限键', async () => {
    stubCheckedRows([
      { id: 3, grab_limit_dl: 5, grab_limit_pw: 5 },
      { id: 9, grab_limit_dl: 5, grab_limit_pw: 5 },
    ])
    const root = await mountPage()
    LAYUI.tableReload.mockClear()
    clickToolbar(root, 'selfCount')

    // (1) 弹的是 prompt，不是「尚未开放」；实参逐字（title 见 brief / 对标 userlist.js:2371）。
    expect(LAYUI.prompt).toHaveBeenCalledTimes(1)
    const args = LAYUI.prompt.mock.calls[0]![0] as Record<string, unknown>
    expect(args.title).toBe('设置用户在内部接单页面接单上限（以用户当前未完成订单数为准），0代表不限制')
    expect(args.formType).toBe(0)
    expect(args.maxlength).toBe(4)
    // 两行取值相同 ⇒ 预填该值（对标写死 `value: 0`，我方有意偏离，见实现处注释）。
    expect(args.value).toBe(5)
    expect(LAYUI.msg).not.toHaveBeenCalledWith('「设自助接单数」尚未开放', { time: 1500 })

    // (2) 输入 5 ⇒ 两个勾选行各一次 PATCH，且**两个键都在**（只写一个是本轮要防的错）。
    answerPrompt('5')
    await flush()
    expect(updateMock).toHaveBeenCalledTimes(2)
    expect(updateMock).toHaveBeenNthCalledWith(1, 3, { grab_limit_dl: 5, grab_limit_pw: 5 })
    expect(updateMock).toHaveBeenNthCalledWith(2, 9, { grab_limit_dl: 5, grab_limit_pw: 5 })
    expect(LAYUI.msg).toHaveBeenCalledWith('接单上限已保存', { time: 1500 })
    expect(LAYUI.tableReload).toHaveBeenCalledWith('tableList_user', { page: { curr: 1 } })
  })

  it('勾选行取值**不统一**（含缺失）⇒ 预填 0；统一为 7 ⇒ 预填 7', async () => {
    stubCheckedRows([{ id: 1, grab_limit_dl: 7 }, { id: 2, grab_limit_dl: 9 }])
    const root = await mountPage()
    clickToolbar(root, 'selfCount')
    expect((LAYUI.prompt.mock.calls[0]![0] as { value: number }).value).toBe(0)

    // 档案行不存在（键整个缺失）⇒ 模型默认就是 0，两行都缺失时预填 0（不是 undefined）。
    LAYUI.prompt.mockClear()
    stubCheckedRows([{ id: 1 }, { id: 2 }])
    clickToolbar(root, 'selfCount')
    expect((LAYUI.prompt.mock.calls[0]![0] as { value: number }).value).toBe(0)

    LAYUI.prompt.mockClear()
    stubCheckedRows([{ id: 1, grab_limit_dl: 7 }, { id: 2, grab_limit_dl: 7 }])
    clickToolbar(root, 'selfCount')
    expect((LAYUI.prompt.mock.calls[0]![0] as { value: number }).value).toBe(7)
  })

  it('输入 0 ⇒ **允许**（0 = 不限制，是合法值）；输入 -1 / abc ⇒ 提示且**不发 PATCH**', async () => {
    stubCheckedRows([{ id: 3, grab_limit_dl: 8 }])
    const root = await mountPage()

    clickToolbar(root, 'selfCount')
    answerPrompt('0')
    await flush()
    // E-0021 的边界：`0` 必须真的发出去（真值判断会把它当成「没填」丢掉）。
    expect(updateMock).toHaveBeenCalledWith(3, { grab_limit_dl: 0, grab_limit_pw: 0 })

    for (const bad of ['-1', 'abc', '']) {
      updateMock.mockClear()
      LAYUI.msg.mockClear()
      LAYUI.prompt.mockClear()
      clickToolbar(root, 'selfCount')
      answerPrompt(bad)
      await flush()
      expect(updateMock, `输入 ${JSON.stringify(bad)} 不该发 PATCH`).not.toHaveBeenCalled()
      expect(LAYUI.msg).toHaveBeenCalledWith('请输入正整数', { time: 1500 })
    }
  })
})

describe('Task 7：禁/解结算（逐行各自翻转）', () => {
  it('混合勾选（一真一假）⇒ 两次 PATCH 的载荷分别是 false 与 true（不是统一置值）', async () => {
    stubCheckedRows([
      { id: 3, settle_forbidden: true },
      { id: 9, settle_forbidden: false },
    ])
    const root = await mountPage()
    LAYUI.tableReload.mockClear()
    clickToolbar(root, 'settle')
    await flush()

    expect(updateMock).toHaveBeenCalledTimes(2)
    expect(updateMock).toHaveBeenNthCalledWith(1, 3, { settle_forbidden: false })
    expect(updateMock).toHaveBeenNthCalledWith(2, 9, { settle_forbidden: true })
    expect(LAYUI.msg).toHaveBeenCalledWith('已切换禁止结算状态', { time: 1500 })
    expect(LAYUI.tableReload).toHaveBeenCalledWith('tableList_user', { page: { curr: 1 } })
  })

  it('键缺失（无档案行）按 `false` 翻转成 `true`——不是把 undefined 发出去', async () => {
    stubCheckedRows([{ id: 3 }])
    const root = await mountPage()
    clickToolbar(root, 'settle')
    await flush()
    expect(updateMock).toHaveBeenCalledWith(3, { settle_forbidden: true })
  })

  it('**没有弹窗**（对标 `setDisSettBtn` 直接切换）：不调 `layer.prompt` / `layer.open`', async () => {
    stubCheckedRows([{ id: 3, settle_forbidden: false }])
    const root = await mountPage()
    clickToolbar(root, 'settle')
    await flush()
    expect(LAYUI.prompt).not.toHaveBeenCalled()
    expect(LAYUI.open).not.toHaveBeenCalled()
    expect(updateMock).toHaveBeenCalledWith(3, { settle_forbidden: true })
  })
})

describe('Task 7：两个开关列变真（PATCH 真实布尔值）', () => {
  /**
   * 触发 layui 的 `form.on('switch(...)')` 处理器——**按 vendor 的真实语义构造事件对象**：
   * 真 layui 发给处理器的是 `{elem, value: elem.value, othis}`，而 `elem.value` 是输入框的
   * `value` 属性（我们的开关标记没写 `value` ⇒ 恒 `"on"`，开关前后都一样），
   * **新状态在 `elem.checked`**（对标 `userlist.js:979-980` 自己读的也是它）。
   * 所以这里恒传 `value: 'on'`、用 `checked` 表达开/关——照抄 `value === 'on'` 的实现会在这条用例上红。
   */
  function flipSwitch(filter: string, id: number, on: boolean): void {
    const handler = LAYUI.formHandlers.get(`switch(${filter})`)
    expect(handler, `没有注册 switch(${filter}) 处理器`).toBeTruthy()
    const elem = document.createElement('input')
    elem.dataset.id = String(id)
    elem.checked = on
    handler!({ elem, value: 'on' })
  }

  it('禁止结算列：**关**（false）这个边界必须发出 `{settle_forbidden:false}`，不能被当成「没传」丢掉', async () => {
    await mountPage()
    flipSwitch('settleForbiddenChecked', 7, false)
    await flush()
    expect(updateMock).toHaveBeenCalledWith(7, { settle_forbidden: false })
    expect(LAYUI.msg).toHaveBeenCalledWith('已保存', { time: 1500 })
  })

  it('禁自助接单列：开 ⇒ `{grab_forbid_self_service:true}`；`notOpenChecked` 处理器已删除', async () => {
    await mountPage()
    flipSwitch('grabForbidChecked', 9, true)
    await flush()
    expect(updateMock).toHaveBeenCalledWith(9, { grab_forbid_self_service: true })
    // 两个开关列都改了过滤器 ⇒ 老的「点了提示未开放」处理器是死代码，必须删掉。
    expect(LAYUI.formHandlers.has('switch(notOpenChecked)')).toBe(false)
  })
})

describe('Task 7：检测功能取数（三种结果可区分）', () => {
  /** 点「检测功能」→ 点唯一那个菜单项 → 卡片开、子表渲染。 */
  async function openAlipayCard(root: HTMLElement): Promise<void> {
    clickToolbar(root, 'aliPay')
    const dd = LAYUI.dropdownRender.mock.calls[0]![0] as { click: (m: { id: number }) => void }
    dd.click({ id: 10 })
    await flush()
  }

  it('有重复：一行一个用户（账号/数量同组重复），空文案不被用上', async () => {
    alipayMock.mockResolvedValue({
      duplicates: [
        {
          alipay_account: 'ali-888@x.com',
          count: 2,
          users: [
            { username: '小航', job: '客服', is_active: true },
            { username: '小舟', job: '代练员', is_active: false },
          ],
        },
      ],
    })
    const root = await mountPage()
    await openAlipayCard(root)
    const opts = lastRenderOptsFor('#tableList3')
    expect((opts.data as unknown[]).length).toBe(2)
    expect(opts.data).toEqual([
      { str1: '小航', str2: 'ali-888@x.com', str3: '客服', id2: 2, str4: 'true' },
      { str1: '小舟', str2: 'ali-888@x.com', str3: '代练员', id2: 2, str4: 'false' },
    ])
  })

  it('真的没有重复：0 行 + 文案「没有重复的收款支付宝账号」', async () => {
    alipayMock.mockResolvedValue({ duplicates: [] })
    const root = await mountPage()
    await openAlipayCard(root)
    const opts = lastRenderOptsFor('#tableList3')
    expect(opts.data).toEqual([])
    expect(opts.text).toEqual({ none: '没有重复的收款支付宝账号' })
  })

  it('失败/无权限（403）：**可读提示**，不是静默空表、也不是「没有重复」那句', async () => {
    alipayMock.mockRejectedValue(new HttpError(403, '缺少权限：finance.payroll'))
    const root = await mountPage()
    await openAlipayCard(root)
    const opts = lastRenderOptsFor('#tableList3')
    expect(opts.data).toEqual([])
    const text = opts.text as { none: string }
    expect(text.none).toBe('你没有查看重复收款支付宝的权限')
    // 三种结果的判据两两不同（E-0004：空结果不等于没有数据）。
    expect(text.none).not.toBe('没有重复的收款支付宝账号')
    expect(text.none).not.toBe('该功能尚未开放')
  })

  it('端点无参调用：`fetchAlipayDuplicates()` 只调一次、不带任何参数', async () => {
    const root = await mountPage()
    await openAlipayCard(root)
    expect(alipayMock).toHaveBeenCalledTimes(1)
    expect(alipayMock.mock.calls[0]).toEqual([])
  })
})

/* ── 开关切换后的**行缓存**（必须两步才抓得到） ───────── */

describe('Task 8：开关切换后的行缓存（两步交互）', () => {
  /** 照 vendor 的真实语义构造 switch 事件（同上面那个 helper，语义见其注释）。 */
  function flipSwitch(filter: string, id: number, on: boolean): void {
    const handler = LAYUI.formHandlers.get(`switch(${filter})`)
    expect(handler, `没有注册 switch(${filter}) 处理器`).toBeTruthy()
    const elem = document.createElement('input')
    elem.dataset.id = String(id)
    elem.checked = on
    handler!({ elem, value: 'on' })
  }

  it('拨开关 → 再点「禁/解结算」⇒ 第二次发的是**开关之后的新值取反**，不是旧值取反', async () => {
    // 「服务器」上这一行的当前值：一开始 `false`（= 允许结算）。
    let serverValue = false
    /**
     * layui 的**行缓存**（`F.cache` 的那份副本）。它与 `serverValue` 是**两个变量**——
     * 这正是本用例的承重点：缓存**只在 `table.render` / `table.reload` 之后**才从服务端重建，
     * 中间拨开关**不会**动它。`table.checkStatus` 读的是**缓存**，不是服务端。
     *
     * ⛔ 这里两个闭包读的是**两个变量**（`serverValue` / `cacheValue`，只在 `tableReload` 桩里
     * 同步一次）：若它们读的是**同一个**变量，那个所谓「缓存」其实是**实时**的 ⇒
     * **载荷断言根本没有判别力**（删掉成功后的 `reload()` 也不会让它红）。
     * 现在的形状下，载荷断言**自己**也有判别力——由下面那条「**对照**」用例单独证明
     * （它把缓存钉在旧值上，断言第二步发的是「旧值取反」）。
     */
    let cacheValue = false
    LAYUI.layui.table.checkStatus = () => ({
      data: [{ id: 7, settle_forbidden: cacheValue }] as never[],
    })
    LAYUI.tableReload.mockImplementation(() => {
      cacheValue = serverValue // 刷新缓存 = 从服务端重新取数
    })
    updateMock.mockImplementation(async (_id: number, payload: Partial<AccountWritePayload>) => {
      if (payload.settle_forbidden !== undefined) serverValue = payload.settle_forbidden
      return {} as AccountRow
    })

    const root = await mountPage()

    // ── 第一步：把「禁止结算」列的开关**拨到开** ──────────────────────────────
    flipSwitch('settleForbiddenChecked', 7, true)
    await flush()
    expect(updateMock).toHaveBeenLastCalledWith(7, { settle_forbidden: true })
    expect(serverValue).toBe(true)
    // 成功路径**必须**刷新行缓存（T7 Important 2）。
    expect(LAYUI.tableReload).toHaveBeenCalledWith('tableList_user', { page: { curr: 1 } })
    expect(cacheValue, '刷新之后缓存就是新值了').toBe(true)

    // ── 第二步：对**同一行**点「禁/解结算」（它是按**缓存值**取反的） ──────────
    updateMock.mockClear()
    clickToolbar(root, 'settle')
    await flush()
    expect(
      updateMock,
      '第二步发的必须是「开关之后的新值(true)取反 = false」；发了 true 就说明行缓存是旧的',
    ).toHaveBeenCalledWith(7, { settle_forbidden: false })
  })

  it('**对照**：缓存停在旧值（= 成功路径没刷新）时，第二步确实会发错', async () => {
    // 与上一条唯一的差别：**手动**把缓存钉在旧值上（模拟「页面没刷新」），其余一模一样。
    // 它证明上一条的载荷断言**真的**在测「缓存刷没刷」，而不是靠旁边 `tableReload` 那条伴随断言
    // （没有这条对照，那句「承重点」的自述就是过头的）。
    let serverValue = false
    LAYUI.layui.table.checkStatus = () => ({
      data: [{ id: 7, settle_forbidden: false }] as never[], // ← 永远停在旧值
    })
    updateMock.mockImplementation(async (_id: number, payload: Partial<AccountWritePayload>) => {
      if (payload.settle_forbidden !== undefined) serverValue = payload.settle_forbidden
      return {} as AccountRow
    })

    const root = await mountPage()
    flipSwitch('settleForbiddenChecked', 7, true)
    await flush()
    expect(serverValue).toBe(true) // 服务端已经是 true 了

    updateMock.mockClear()
    clickToolbar(root, 'settle')
    await flush()
    // 缓存没刷新 ⇒ 取反发的是**旧值取反**（true）⇒ 等于把当前值又发了一遍、什么都没变。
    expect(updateMock).toHaveBeenCalledWith(7, { settle_forbidden: true })
  })
})

/* ── 操作列三颗链接**真的能点到**（驱动点击，不是断 HTML 字符串） ── */

describe('操作列：编辑 / 重置 / 恢复 三颗链接驱动真点击', () => {
  /** 取第 `index` 次 `layer.confirm` 的文案。 */
  function confirmText(index = 0): string {
    const call = (LAYUI.layui.layer.confirm as ReturnType<typeof vi.fn>).mock.calls[index]
    expect(call, `layer.confirm 第 ${index + 1} 次没被调用`).toBeTruthy()
    return call![0] as string
  }

  /** 驱动第 `index` 次 `layer.confirm` 的**确定**回调（模拟用户点「确定」）。 */
  function answerConfirm(index = 0): void {
    const call = (LAYUI.layui.layer.confirm as ReturnType<typeof vi.fn>).mock.calls[index]
    expect(call, `layer.confirm 第 ${index + 1} 次没被调用`).toBeTruthy()
    ;(call![2] as (i: number) => void)(index + 1)
  }

  it('「编辑」：点得到的链接 ⇒ 开 `id=3` 的编辑弹层（不再 `onNavigate("system-user-edit")`）', async () => {
    const root = await mountPage()
    const cell = mountOperationCell(root, { id: 3, deleted_at: null })
    expect(cell.querySelector('[data-act="edit"]'), '操作列必须渲染「编辑」').not.toBeNull()

    cell.querySelector<HTMLElement>('[data-act="edit"]')!.click()

    // ⛔ 出口是 iframe 弹层（`layer.open` 实参），不是跳页——仍然驱动**真点击**，
    //    并显式钉住「不再跳页」。
    const args = LAYUI.open.mock.calls[0]?.[0] as Record<string, unknown>
    expect(args, 'layer.open 没被调用').toBeTruthy()
    expect(args.title).toBe('编辑系统子账户')
    expect(args.type).toBe(2)
    expect(args.content).toContain('page=user-adduser')
    // ⚠️ 判据必须能区分 `id=` 与 `uid=`：写 `toContain('id=3')` 时，把实现里的键改成 `uid`
    // 这条**照样绿**（`'uid=3'` 里含 `'id=3'` 子串）⇒ 钉不住这个键。用 `&id=3`（带分隔符的形态）
    // 就要求 `id` 是**完整的键名**：`&uid=3` 不含 `&id=3`，而内容里 `page=` 一定在 `id=` 之前，
    // 所以这个 `&` 必然存在。再加一条负向钉，防「两个键都拼上」那种把戏。
    expect(args.content).toContain('&id=3')
    expect(args.content).not.toContain('uid=')
    expect(navigateMock).not.toHaveBeenCalled()
  })

  it('「重置」：点得到、走确认框，确认后**真的调** `resetAccountPassword(id)`', async () => {
    const root = await mountPage()
    const cell = mountOperationCell(root, { id: 5, deleted_at: null })

    cell.querySelector<HTMLElement>('[data-act="reset"]')!.click()
    expect(confirmText()).toContain('重置')
    expect(resetPasswordMock).not.toHaveBeenCalled() // 没确认之前不许动手

    answerConfirm()
    await flush()
    expect(resetPasswordMock).toHaveBeenCalledWith(5)
    expect(LAYUI.msg).toHaveBeenCalledWith('密码已重置为 123456', { time: 1500 })
  })

  it('「恢复」：已删行只渲染这一颗 ⇒ 点它**真的调** `restoreAccount(id)` 并刷新列表', async () => {
    const root = await mountPage()
    const cell = mountOperationCell(root, {
      id: 7,
      deleted_at: '2026-01-02T03:04:05Z',
    })
    // 已删行**不渲染**编辑 / 重置（回收站里只给「恢复」）。
    expect(cell.querySelector('[data-act="edit"]')).toBeNull()
    expect(cell.querySelector('[data-act="reset"]')).toBeNull()
    const link = cell.querySelector<HTMLElement>('[data-act="restore"]')
    expect(link, '已删行必须渲染「恢复」').not.toBeNull()

    link!.click()
    await flush()

    // ⛔ 这条曾经红：`restore` 那条分支是**死代码**（链接点不动）⇒ 恢复在 UI 上完全不可达。
    expect(restoreMock).toHaveBeenCalledWith(7)
    expect(LAYUI.msg).toHaveBeenCalledWith('已恢复', { time: 1500 })
    expect(LAYUI.tableReload).toHaveBeenCalledWith('tableList_user', { page: { curr: 1 } })
  })

  /* ── 顺手补一条：两次确认真的守住了 ── */

  it('「删除用户」：**两次确认**都点掉才动手；只点第一次**不许**调 `deleteAccount`', async () => {
    stubCheckedRows([{ id: 7 }])
    const root = await mountPage()
    clickToolbar(root, 'delete')

    // 第一次确认：文案逐字（含两个空格），且此时**一行都没删**。
    expect(confirmText(0)).toContain('确认要删除吗？')
    expect(confirmText(0)).toContain('强烈建议不要删除')
    expect(deleteMock).not.toHaveBeenCalled()

    answerConfirm(0)
    // 第二次确认：调的是同一个 `layer.confirm`（第 2 次），此刻**仍然一行都没删**。
    expect(confirmText(1)).toContain('第二次确认:  ')
    expect(deleteMock).not.toHaveBeenCalled()

    answerConfirm(1)
    await flush()
    expect(deleteMock).toHaveBeenCalledWith(7)
    expect(LAYUI.msg).toHaveBeenCalledWith('已删除 1 个账号', { time: 1500 })
    expect(LAYUI.tableReload).toHaveBeenCalledWith('tableList_user', { page: { curr: 1 } })
  })
})

/* ── Task 13：「新增」入口 + 「游戏权限」列链接也走弹层；三个入口的 `end` 都真的刷新列表 ── */

describe('Task 13：新增 / 登录账号列 / 游戏权限列（iframe 弹层）', () => {
  /** 取第 `index` 次 `layer.open` 的实参。 */
  function openArgs(index = 0): Record<string, unknown> {
    const call = LAYUI.open.mock.calls[index]
    expect(call, `layer.open 第 ${index + 1} 次没被调用`).toBeTruthy()
    return call![0] as Record<string, unknown>
  }

  it('工具栏「新增」：点 `[data-tb="new"]` ⇒ 开 `id=0` 的弹层，标题「新开系统子账户」，不跳页', async () => {
    const root = await mountPage()
    clickToolbar(root, 'new')

    const args = openArgs()
    expect(args.title).toBe('新开系统子账户')
    expect(args.type).toBe(2)
    expect(args.content).toBe('/layui-island/page.html?page=user-adduser&id=0')
    expect(args.area).toEqual(['90%', '90%'])
    expect(navigateMock).not.toHaveBeenCalled()
  })

  it('登录账号列那条链接（**同一个** `act="edit"` 分支的第二个入口）也开弹层', async () => {
    const root = await mountPage()
    // 防「只改了操作列那一处」：登录账号列由 `accountLink()` 产出——与操作列同分支、不同产出点。
    const cell = mountColumnCell(root, '登录账号', { id: 3, username: 'kefu01', deleted_at: null })
    expect(cell.querySelector('[data-act="edit"]'), '登录账号列必须渲染账号链接').not.toBeNull()

    cell.querySelector<HTMLElement>('[data-act="edit"]')!.click()

    const args = openArgs()
    expect(args.title).toBe('编辑系统子账户')
    // 同上面「操作列」那条：`&id=3` 才能区分 `id=` 与 `uid=`（`toContain('id=3')` 对后者不敏感）。
    expect(args.content).toContain('&id=3')
    expect(args.content).not.toContain('uid=')
    expect(navigateMock).not.toHaveBeenCalled()
  })

  it('「游戏权限」列链接 ⇒ 开「设定游戏权限」弹层（复用 Task 6 的实参），不再跳编辑页', async () => {
    const root = await mountPage()
    const cell = mountColumnCell(root, '游戏权限', { id: 3, games: [] })
    expect(cell.querySelector('[data-act="games"]'), '游戏权限列必须渲染链接').not.toBeNull()

    cell.querySelector<HTMLElement>('[data-act="games"]')!.click()

    // 对标的 `.games` 点击 ⇒ `setUserGames(id)`，开的是「设定游戏权限」
    // 弹层，**不是**编辑页（跳编辑页是偏差）。
    const args = openArgs()
    expect(args.title).toBe('设定游戏权限')
    expect(args.type).toBe(2)
    expect(args.area).toEqual(['80%', '30%'])
    expect(args.content).toContain('page=system-setgames')
    expect(args.content).toContain('uid=3')
    expect(navigateMock).not.toHaveBeenCalled()

    // 第三个入口（游戏权限列）的 `end` 也要与另两次开层同判据：**是函数且调它真刷新**。
    // 只断「存在」（`toBeTruthy`）挡不住漏写/写成非函数（子页关掉后列表不刷新是**静默**的）。
    expect(typeof args.end, '游戏权限列开层的 `end` 不是函数（子页关掉后列表不会刷新）').toBe(
      'function',
    )
    // 保险：先清一次 `reload` 的调用记录。实测挂载期**不会**调 `reload`（探针实测次数 0），
    // 所以下面这条只能由 `end` 触发；`mockClear` 只是防止将来挂载期新增 `reload()` 把这条变成恒真。
    LAYUI.tableReload.mockClear()
    ;(args.end as () => void)()
    expect(LAYUI.tableReload).toHaveBeenCalledWith('tableList_user', { page: { curr: 1 } })
  })

  it('`end` 是函数且调它**真的刷新列表**（新增 / 编辑两次开层都带 `end`；漏写会静默通过 ⇒ 判据打在 `typeof` 上）', async () => {
    const root = await mountPage()
    clickToolbar(root, 'new')
    mountOperationCell(root, { id: 3, deleted_at: null })
      .querySelector<HTMLElement>('[data-act="edit"]')!
      .click()

    const calls = LAYUI.open.mock.calls
    expect(calls, '新增与编辑应当各开一次层').toHaveLength(2)
    calls.forEach((call, i) => {
      expect(
        typeof (call[0] as Record<string, unknown>).end,
        `第 ${i + 1} 次 layer.open 的 \`end\` 不是函数（子页关掉后列表不会刷新）`,
      ).toBe('function')
    })

    ;((calls[0]![0] as Record<string, unknown>).end as () => void)()
    expect(LAYUI.tableReload).toHaveBeenCalledWith('tableList_user', { page: { curr: 1 } })
  })
})

// ── 身份切换条 ────────────────────────────────
describe('身份切换条：点 tab = 设 userType → 同一份 buildUserListWhere 重取（与手改下拉同一出口）', () => {
  it('点「内部员工」⇒ is_worker=false（成员类型退出分类，userType 复位 -1）；点「全部」⇒ 三个身份参数都不发送', async () => {
    const root = await mountPage()
    const chips = Array.from(root.querySelectorAll<HTMLElement>(`.${IDENTITY_TAB_CLASS}`))
    expect(chips.length).toBe(IDENTITY_TABS.length)
    // form.val 桩改成真语义：带第二参 = 存值，不带 = 取值（替身默认恒空 ⇒ 存不进）。
    const store: Record<string, string> = {}
    ;(LAYUI.layui.form.val as unknown as ReturnType<typeof vi.fn>).mockImplementation(
      (filter: string, values?: Record<string, string>) => {
        if (values === undefined) return { ...store }
        Object.assign(store, values)
        return { ...store }
      },
    )
    ;(chips[1] as HTMLElement).click()
    await flush()
    expect(store.userType).toBe('-1')
    expect(store.workerFlag).toBe('false')
    const picked = LAYUI.tableReload.mock.calls[
      LAYUI.tableReload.mock.calls.length - 1
    ]?.[1] as {
      where: Record<string, unknown>
      page: { curr: number }
    }
    expect(picked.where.is_worker).toBe(false)
    expect(picked.where.member_type).toBeUndefined()
    expect(picked.page.curr).toBe(1)
    expect(chips[1]!.classList.contains(IDENTITY_TAB_ACTIVE_CLASS)).toBe(true)
    expect(chips[0]!.classList.contains(IDENTITY_TAB_ACTIVE_CLASS)).toBe(false)
    ;(chips[0] as HTMLElement).click()
    await flush()
    const all = LAYUI.tableReload.mock.calls[
      LAYUI.tableReload.mock.calls.length - 1
    ]?.[1] as { where: Record<string, unknown> }
    expect(all.where.member_type).toBeUndefined()
    expect(all.where.is_studio).toBeUndefined()
    expect(all.where.is_worker).toBeUndefined()
    expect(chips[0]!.classList.contains(IDENTITY_TAB_ACTIVE_CLASS)).toBe(true)
  })

  it('点「打手」⇒ is_worker=true + is_studio=false；点「工作室」⇒ is_studio=true；成员类型不再参与分类（改准二）', async () => {
    const root = await mountPage()
    const chips = Array.from(root.querySelectorAll<HTMLElement>(`.${IDENTITY_TAB_CLASS}`))
    const store: Record<string, string> = {}
    ;(LAYUI.layui.form.val as unknown as ReturnType<typeof vi.fn>).mockImplementation(
      (filter: string, values?: Record<string, string>) => {
        if (values === undefined) return { ...store }
        Object.assign(store, values)
        return { ...store }
      },
    )
    const whereOfLast = (): Record<string, unknown> =>
      (LAYUI.tableReload.mock.calls[LAYUI.tableReload.mock.calls.length - 1]?.[1] as {
        where: Record<string, unknown>
      }).where
    ;(chips[2] as HTMLElement).click() // 打手
    await flush()
    expect(store.workerFlag).toBe('true')
    expect(store.studioFlag).toBe('false')
    expect(whereOfLast().is_worker).toBe(true)
    expect(whereOfLast().is_studio).toBe(false)
    expect(whereOfLast().member_type).toBeUndefined() // 成员类型退出分类
    ;(chips[3] as HTMLElement).click() // 工作室
    await flush()
    expect(store.studioFlag).toBe('true')
    expect(whereOfLast().is_studio).toBe(true)
    expect(whereOfLast().is_worker).toBeUndefined()
    ;(chips[1] as HTMLElement).click() // 内部员工
    await flush()
    expect(whereOfLast().is_worker).toBe(false)
    expect(whereOfLast().is_studio).toBeUndefined()
  })

  it('tab 集合是声明的常量：全部 / 内部员工 / 打手 / 工作室（D-0228 业务方拍板的四档）', () => {
    expect(IDENTITY_TABS.map((t) => t.label)).toEqual(['全部', '内部员工', '打手', '工作室'])
  })
})

/**
 * ⭐ 「押金额度」是人员管理页的**自有**按钮（对标没有），
 * 所以它**不进** `USER_TOOLBAR` 的 14 颗；**按节点渲染、没权限就不画**（D-0154 / E-0098），
 * 一次只能配一个人（计划 §10）。
 */
describe('押金额度（自有按钮，按节点渲染，fail-closed）', () => {
  /** 持 `finance.taker_wallet_adjust`（与后端端点同一个节点）的合成身份。 */
  const NODE_USER = { is_tenant_admin: false, menu_permissions: ['finance.taker_wallet_adjust'] }

  it('持 `finance.taker_wallet_adjust` ⇒ 按钮出现，且紧跟「设置游戏权限」之后', async () => {
    fetchMeMock.mockResolvedValue(NODE_USER as never)
    const root = await mountPage()
    const btn = root.querySelector<HTMLButtonElement>('[data-tb="depositQuota"]')
    expect(btn).not.toBeNull()
    expect(btn!.textContent).toBe('押金额度')
    expect(btn!.className).toContain('layui-btn-xs')
    const buttons = Array.from(root.querySelectorAll<HTMLElement>('[data-tb]'))
    const gamesIndex = buttons.findIndex((b) => b.dataset.tb === 'games')
    expect(gamesIndex, '工具栏里必须先有「设置游戏权限」').toBeGreaterThanOrEqual(0)
    expect(buttons[gamesIndex + 1]!.dataset.tb).toBe('depositQuota')
  })

  it('没有该节点 ⇒ **不渲染**按钮（默认身份；用户不会看到一个点了 403 的入口）', async () => {
    const root = await mountPage()
    expect(root.querySelector('[data-tb="depositQuota"]')).toBeNull()
    // 其余 14 颗照旧。
    expect(root.querySelectorAll('[data-tb]')).toHaveLength(14)
  })

  it('`fetchMe()` 失败（401 / 网络）⇒ fail-closed：不渲染按钮，页面其余部分照常挂载', async () => {
    fetchMeMock.mockRejectedValue(new HttpError(401, '未认证'))
    const root = await mountPage()
    expect(root.querySelector('[data-tb="depositQuota"]')).toBeNull()
    expect(root.querySelector('table#tableList_user')).not.toBeNull()
  })

  it('勾 1 行 ⇒ `layer.open({type:2})` 打开 `page=taker-deposit-quota&uid=<id>`，`end` 刷新列表', async () => {
    fetchMeMock.mockResolvedValue(NODE_USER as never)
    stubCheckedRows([{ id: 3 }])
    const root = await mountPage()
    LAYUI.open.mockClear()
    LAYUI.tableReload.mockClear()
    clickToolbar(root, 'depositQuota')

    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    const args = LAYUI.open.mock.calls[0]![0] as Record<string, unknown>
    expect(args.title).toBe('押金额度')
    expect(args.type).toBe(2)
    expect(args.area).toEqual(['720px', '560px'])
    expect(args.shade).toEqual(DEPOSIT_QUOTA_DIALOG_BASE.shade)
    expect(args.skin).toBe(DEPOSIT_QUOTA_DIALOG_BASE.skin)
    // `page=` 的值就是子页文件名（写错 ⇒ 弹层里是「未登记的 island 页面」）。
    expect(String(args.content)).toBe(depositQuotaDialogContent(3))
    expect(String(args.content)).toContain('page=taker-deposit-quota&uid=3')
    // `end` 真的接上了：子页保存成功后自己关弹层，父页借此刷新列表。
    ;(args.end as () => void)()
    expect(LAYUI.tableReload).toHaveBeenCalledWith('tableList_user', { page: { curr: 1 } })
  })

  it('勾 0 行 ⇒ 既有那句「请勾选要操作的数据行」，不开弹层', async () => {
    fetchMeMock.mockResolvedValue(NODE_USER as never)
    const root = await mountPage()
    LAYUI.open.mockClear()
    clickToolbar(root, 'depositQuota')
    expect(LAYUI.msg).toHaveBeenCalledWith('请勾选要操作的数据行', { time: 1500 })
    expect(LAYUI.open).not.toHaveBeenCalled()
  })

  it('勾多行 ⇒ 提示「押金额度只能给一个人配置」，不开弹层', async () => {
    fetchMeMock.mockResolvedValue(NODE_USER as never)
    stubCheckedRows([{ id: 3 }, { id: 9 }])
    const root = await mountPage()
    LAYUI.open.mockClear()
    clickToolbar(root, 'depositQuota')
    expect(LAYUI.msg).toHaveBeenCalledWith('押金额度只能给一个人配置', { time: 1500 })
    expect(LAYUI.open).not.toHaveBeenCalled()
  })

  it('子页 URL 走 `BASE_URL`（子路径部署不能少前缀）且 `page=` 与文件名逐字一致', () => {
    expect(depositQuotaDialogContent(3)).toMatch(/\/layui-island\/page\.html\?page=taker-deposit-quota&uid=3$/)
    expect(depositQuotaDialogContent(3, '/console/')).toBe(
      '/console/layui-island/page.html?page=taker-deposit-quota&uid=3',
    )
  })

  // ⭐ 列表里的**行内总闸**。它只改开关、⛔ 不动任何额度行
  // （载荷形状由 `api/__tests__/takerDepositQuota.spec.ts` 的「没有 amounts 键」钉住）。
  /** 造一个「个人押金额度」列的开关输入框（真 marker 由列的 templet 产出）。 */
  function switchElement(id: number, on: boolean): HTMLInputElement {
    const col = buildCols(new Map(), true).find(
      (c) => c.title === '个人押金额度',
    ) as { templet: (d: Record<string, unknown>) => string }
    const wrap = document.createElement('div')
    wrap.innerHTML = col.templet({ id, deposit_quota_enabled: !on })
    const elem = wrap.querySelector<HTMLInputElement>('input[type="checkbox"]')!
    expect(elem, '列 templet 必须产出 switch 输入框').toBeTruthy()
    // layui 已经把 UI 翻到新状态了（乐观更新）：处理器读的是 `elem.checked`。
    elem.checked = on
    return elem
  }

  function dispatchSwitch(elem: HTMLInputElement): void {
    const handler = LAYUI.formHandlers.get(`switch(${DEPOSIT_QUOTA_COLUMN.filter})`)
    expect(handler, '持节点时必须注册行内开关的处理器').toBeTruthy()
    handler!({ elem, value: 'on' })
  }

  it('持节点 ⇒ 表列里有「个人押金额度」；无节点 ⇒ 列与处理器都不存在（fail-closed）', async () => {
    // `cols` 是 layui 的**分组**形状（`[buildCols(...)]`）⇒ 拍平一层再看列。
    const renderTitles = (): string[] =>
      (lastRenderOptsFor('#tableList_user').cols as Array<Array<{ title?: string }>>)
        .flat()
        .map((c) => c.title ?? '')

    fetchMeMock.mockResolvedValue(NODE_USER as never)
    await mountPage()
    expect(renderTitles()).toContain('个人押金额度')
    expect(LAYUI.formHandlers.has(`switch(${DEPOSIT_QUOTA_COLUMN.filter})`)).toBe(true)

    // 换成无节点身份重新挂载（同一份测试文件里 `fetchMe` 的实现被覆盖）。
    // ⚠️ 处理器表是**模块级替身**、卸载时不清 ⇒ 必须先清空，否则「上一次挂载注册的」会冒充这一次。
    document.body.innerHTML = ''
    LAYUI.formHandlers.clear()
    fetchMeMock.mockResolvedValue({ menu_permissions: [] } as never)
    await mountPage()
    expect(renderTitles()).not.toContain('个人押金额度')
    expect(LAYUI.formHandlers.has(`switch(${DEPOSIT_QUOTA_COLUMN.filter})`)).toBe(false)
  })

  it('拨开 ⇒ 调 `setDepositQuotaEnabled(id, true)`，提示已保存，且**不整表 reload**（乐观更新）', async () => {
    fetchMeMock.mockResolvedValue(NODE_USER as never)
    await mountPage()
    LAYUI.tableReload.mockClear()
    const elem = switchElement(7, true)
    dispatchSwitch(elem)
    await flush()

    expect(depositQuotaSwitchMock).toHaveBeenCalledTimes(1)
    expect(depositQuotaSwitchMock).toHaveBeenCalledWith(7, true)
    expect(LAYUI.msg).toHaveBeenCalledWith('已保存', { time: 1500 })
    // 乐观更新的判据：成功后不重取列表（重取会回第 1 页）。
    expect(LAYUI.tableReload).not.toHaveBeenCalled()
  })

  it('拨关 ⇒ `setDepositQuotaEnabled(id, false)`（false 不能被真值判断吃掉）', async () => {
    fetchMeMock.mockResolvedValue(NODE_USER as never)
    await mountPage()
    const elem = switchElement(9, false)
    dispatchSwitch(elem)
    await flush()
    expect(depositQuotaSwitchMock).toHaveBeenCalledWith(9, false)
  })

  it('失败 ⇒ **把开关拨回去** + 弹后端原样 msg + 重画 switch，且不整表 reload', async () => {
    fetchMeMock.mockResolvedValue(NODE_USER as never)
    depositQuotaSwitchMock.mockRejectedValue(new Error('该账号已删除，不能配置个人押金额度'))
    await mountPage()
    LAYUI.tableReload.mockClear()
    LAYUI.formRender.mockClear()
    const elem = switchElement(7, true) // 用户拨到了「开」
    dispatchSwitch(elem)
    await flush()

    // 回滚打**真实出口**：DOM 上那颗输入框的 checked 回到拨之前的值。
    expect(elem.checked).toBe(false)
    // layui 的 switch 是渲染出来的壳 ⇒ 必须重画一次，否则界面停在「开」。
    expect(LAYUI.formRender).toHaveBeenCalledWith('checkbox')
    // 后端 msg 原样显示（不翻译、不减字）。
    expect(LAYUI.msg).toHaveBeenCalledWith('该账号已删除，不能配置个人押金额度', { time: 1500 })
    expect(LAYUI.msg).not.toHaveBeenCalledWith('已保存', { time: 1500 })
    // 回滚是本格的，不是「整表重取」——第 3 页不会因为拨错一个开关被甩回第 1 页。
    expect(LAYUI.tableReload).not.toHaveBeenCalled()
  })
})

