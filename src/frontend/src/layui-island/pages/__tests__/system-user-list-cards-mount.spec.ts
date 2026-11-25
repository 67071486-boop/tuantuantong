// @vitest-environment jsdom
/**
 * 人员管理页**挂载层**的接线测试： 的两张卡片外壳 + 检测功能二级下拉 +
 * 子表工具栏「点了提示尚未开放」。
 *
 * 为什么值得单写一份（而不只断言导出的常量）：`openCard` / `dropdown.render` / 子表工具栏的
 * 点击委托都在 `mountUserListPage` 的闭包里，纯函数测不到；只断言源码文本是同义反复。
 * 这里把 layui 换成可控替身（手法照 `system-systemlog-mount.spec.ts` / `emp-emplist.spec.ts`），
 * 挂载整页后**驱动真点击**，断言真实出口：`bootstrapIsland` 的模块列表、`dropdown.render` 的实参、
 * 菜单项点击后的 `layer.open` 实参（含 `content` 是哪只容器）、子表 `table.render` 的入参、
 * 以及子表按钮那句「尚未开放」。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/** layui 的可控替身。`vi.hoisted` 是因为 `vi.mock` 的工厂会被提升到 import 之前。 */
const LAYUI = vi.hoisted(() => {
  const tableHandlers = new Map<string, (...a: unknown[]) => unknown>()
  const formHandlers = new Map<string, (...a: unknown[]) => unknown>()
  const spinner = {
    tableRender: vi.fn(),
    tableReload: vi.fn(),
    formRender: vi.fn(),
    dropdownRender: vi.fn(),
    // 接单统计卡片那两个日期框要的（合并 `main` 的 P3e 后新增；缺了它挂载就炸在
    // `laydate.render` 上，看起来像页面代码错、其实是桩不完整）。
    laydateRender: vi.fn(),
    // ⚠️ 形参不能省：`vi.fn(() => 7)` 推出来的 `mock.calls` 是**空元组**，
    // 用例里 `calls[0]![0]` 会 TS2493 编译不过。给一个 `unknown` 形参即可。
    open: vi.fn((_o: unknown) => 7),
    full: vi.fn(),
    close: vi.fn(),
    msg: vi.fn(),
    confirm: vi.fn(),
    prompt: vi.fn(),
    hide: vi.fn(),
  }
  /**
 * `layui.$` 的替身：返回一个带 `hide()` 的对象，并**记住选择器**——
 * `layer.open` 的 `content` 到底是哪只容器，只能靠这个记号来判（真 layui 会真搬 DOM）。
 */
  const jq = vi.fn((selector: string) => ({ hide: spinner.hide, selector }))
  /** `bootstrapIsland` 拿到的模块列表（ 要求把 `dropdown` 加进去）。 */
  const bootstrapModules = vi.fn()
  const layui = {
    device: () => ({ android: false, ios: false }),
    $: jq,
    form: {
      render: spinner.formRender,
      on: (e: string, cb: (...a: unknown[]) => unknown) => formHandlers.set(e, cb),
      val: () => ({}),
    },
    table: {
      render: spinner.tableRender,
      reload: spinner.tableReload,
      checkStatus: () => ({ data: [] }),
      on: (e: string, cb: (...a: unknown[]) => unknown) => tableHandlers.set(e, cb),
    },
    layer: {
      msg: spinner.msg,
      open: spinner.open,
      full: spinner.full,
      close: spinner.close,
      confirm: spinner.confirm,
      prompt: spinner.prompt,
    },
    dropdown: { render: spinner.dropdownRender },
    laydate: { render: spinner.laydateRender },
  }
  return { tableHandlers, formHandlers, jq, layui, bootstrapModules, ...spinner }
})

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return {
    ...actual,
    bootstrapIsland: (async (modules: string[]) => {
      LAYUI.bootstrapModules(modules)
      return LAYUI.layui
    }) as unknown as typeof actual.bootstrapIsland,
    initIslandElements: vi.fn(),
  }
})

vi.mock('@/layui/vendor', () => ({ initFormSelects: vi.fn() }))

vi.mock('@/api/config', () => ({
  // 本页挂载取的是**全量** `fetchAllGames()`（配置面，防
  // 「看不到没勾的游戏 ⇒ 也就勾不上」的死锁，见 `system-user-list.spec.ts` 的防死锁 pin）；
  // `fetchGames` 只是同模块的另一个导出，桩里留着以免同一页面出现两处来源。
  fetchGames: vi.fn(async () => [{ id: 1, name: '原神' }]),
  fetchAllGames: vi.fn(async () => [{ id: 1, name: '原神' }]),
}))

vi.mock('@/api/accounts', () => ({
  fetchAccountRoles: vi.fn(async () => ['客服']),
  listAccounts: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 200 })),
  resetAccountPassword: vi.fn(),
  setAccountStatus: vi.fn(),
  setAccountsStatus: vi.fn(),
  updateAccount: vi.fn(),
}))

// 起「检测功能」卡片会真的取数；这份用例只关心外壳，替身给「没有重复」。
vi.mock('@/api/performances', () => ({
  fetchAlipayDuplicates: vi.fn(async () => ({ duplicates: [] })),
}))

// ⭐ 挂载时会 `fetchMe()` 判「押金额度」按钮的权限门。
// 不打桩会走真 `request()`；给一个**没有任何节点**的合成身份 ⇒ 工具栏仍是那 14 颗。
vi.mock('@/api/auth', () => ({ fetchMe: vi.fn(async () => ({ menu_permissions: [] })) }))

import { mountUserListPage, toolbarHtml } from '../system-user-list'

type OpenArgs = {
  title: string
  shade: unknown
  shadeClose: boolean
  type: number
  skin: string
  anim: number
  closeBtn: number
  maxmin: boolean
  area: string[]
  content: { selector: string }
  cancel: () => void
  end: () => void
}

async function mountPage(): Promise<HTMLElement> {
  const root = document.createElement('div')
  document.body.appendChild(root)
  await mountUserListPage(root, { onNavigate: vi.fn() })
  // 真 layui 的 `table.render({toolbar:'#userToolbarTpl'})` 会把模板的 innerHTML 搬进
  // `.layui-table-tool`；替身不搬，所以这里用**页面自己的** `toolbarHtml()` 补一份真按钮，
  // 好让 root 上的点击委托被真正驱动（内容与真 layui 搬进去的那份逐字相同）。
  // 子表工具栏同理（见「子表工具栏按钮」那个用例）。
  root.insertAdjacentHTML('beforeend', toolbarHtml())
  return root
}

/** 点主工具栏那颗按钮（`[data-tb=…]`）。 */
function clickToolbar(root: HTMLElement, action: string): void {
  const btn = root.querySelector<HTMLButtonElement>(`[data-tb="${action}"]`)
  expect(btn, `主工具栏没有 [data-tb=${action}]`).toBeTruthy()
  btn!.click()
}

/** 取 `table.render` 里渲染某只 `elem` 的那次入参（挂载时主表已经渲染过一次）。 */
function renderOptsFor(elem: string): Record<string, unknown> {
  const call = LAYUI.tableRender.mock.calls.find((c) => (c[0] as { elem?: string }).elem === elem)
  expect(call, `table.render 没有渲染 ${elem}`).toBeTruthy()
  return call![0] as Record<string, unknown>
}

beforeEach(() => {
  vi.clearAllMocks()
  LAYUI.open.mockReturnValue(7)
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('人员管理挂载层：Task 5 的两张卡片外壳（驱动真点击）', () => {
  it('`bootstrapIsland` 的模块列表加了 `dropdown`（检测功能）与 `laydate`（接单统计日期框）', async () => {
    await mountPage()
    // `dropdown` 缺了 ⇒ 检测功能点了毫无反应；`laydate` 缺了 ⇒ 卡片两个日期框点不开。
    expect(LAYUI.bootstrapModules).toHaveBeenCalledWith([
      'form',
      'table',
      'layer',
      'dropdown',
      'laydate',
    ])
  })

  it('点「接单统计」：开的是 `.recTotalDiv` 卡片（title/skin/anim/area/closeBtn 逐字），不是自造弹层', async () => {
    const root = await mountPage()
    clickToolbar(root, 'recTotal')
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    const args = LAYUI.open.mock.calls[0]![0] as OpenArgs
    expect(args.title).toBe('接单统计')
    expect(args.content.selector).toBe('.recTotalDiv')
    expect(args.shade).toEqual([0.3, '#333'])
    expect(args.shadeClose).toBe(true)
    expect(args.type).toBe(1)
    expect(args.skin).toBe('layui-layer-lan')
    expect(args.anim).toBe(8)
    // 电脑端**故意没有关闭按钮**（对标原文）。
    expect(args.closeBtn).toBe(0)
    expect(args.maxmin).toBe(false)
    expect(args.area).toEqual(['38%', '90%'])
    expect(typeof args.cancel).toBe('function')
    expect(typeof args.end).toBe('function')
    // 非 PC 端才 `layer.full`；PC 端不调。
    expect(LAYUI.full).not.toHaveBeenCalled()
  })

  it('点「接单统计」后再 `table.render` `#tableList2`：**真取数**（url + 本分支的子模板 toolbar），空文案不再是「未开放」', async () => {
    const root = await mountPage()
    clickToolbar(root, 'recTotal')
    // 挂载时主表渲染 1 次 + 点开卡片后子表 1 次。
    expect(LAYUI.tableRender).toHaveBeenCalledTimes(2)
    const opts = renderOptsFor('#tableList2')
    expect(opts.toolbar).toBe('#toolbarDemo2')
    // 本分支原先这里是「空数据 + 该功能尚未开放 + **无 url**」的外壳；合并裁定取 `main` 的
    // 真取数（P3e ），所以 `url` 必须在了，空态换成「暂无数据」（空结果 ≠ 功能没做，E-0004）。
    expect(opts.url).toBe('/api/business/order-stats')
    expect(opts.text).toEqual({ none: '暂无数据' })
    expect(opts).not.toHaveProperty('data')
    expect(LAYUI.formRender).toHaveBeenCalled()
  })

  it('点「检测功能」只**弹二级下拉**、不开卡片；点了菜单项才开 `.checkAliPayAccDiv` 卡片并取数', async () => {
    const root = await mountPage()
    clickToolbar(root, 'aliPay')
    // (a) 下拉实参逐字（对标 `userlist.js:1280-1290`）。
    expect(LAYUI.dropdownRender).toHaveBeenCalledTimes(1)
    const dd = LAYUI.dropdownRender.mock.calls[0]![0] as {
      elem: string
      show: boolean
      data: unknown
      shade: unknown
      click: (m: { id: number }) => void
    }
    expect(dd.elem).toBe('#checkDropdown')
    expect(dd.show).toBe(true)
    expect(dd.data).toEqual([{ title: '检测重复收款支付宝账号者', id: 10 }])
    expect(dd.shade).toEqual([0.3, '#2f363c'])
    // (b) **先下拉、还没开卡片**。
    expect(LAYUI.open).not.toHaveBeenCalled()
    // (c) 点菜单项 ⇒ 开卡片；取数是**异步**的，先只断言卡片本身。
    dd.click({ id: 10 })
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    const args = LAYUI.open.mock.calls[0]![0] as OpenArgs
    expect(args.title).toBe('重复收款支付宝子账号检测')
    expect(args.content.selector).toBe('.checkAliPayAccDiv')
    expect(args.closeBtn).toBe(0)
    expect(args.area).toEqual(['38%', '90%'])
    // (d) 取数落地后才渲染子表（替身回「没有重复」⇒ 空表 + 那句真文案）。
    await new Promise((resolve) => setTimeout(resolve, 0))
    // 挂载时主表 1 次 + 卡片里子表 1 次。
    expect(LAYUI.tableRender).toHaveBeenCalledTimes(2)
    const opts = renderOptsFor('#tableList3')
    expect(opts.toolbar).toBe('#toolbarDemo3')
    expect(opts.data).toEqual([])
    expect(opts.text).toEqual({ none: '没有重复的收款支付宝账号' })
    expect(opts).not.toHaveProperty('url')
    // 这张卡片里没有内嵌筛选 ⇒ 不重复调 `form.render`（与「接单统计」那张不同）。
    expect(LAYUI.formRender).not.toHaveBeenCalled()
  })

  it('菜单项 id 不是 10 时**不开卡片**（对标 `if (menudata.id == 10)`）', async () => {
    const root = await mountPage()
    clickToolbar(root, 'aliPay')
    const dd = LAYUI.dropdownRender.mock.calls[0]![0] as { click: (m: { id: number }) => void }
    dd.click({ id: 11 })
    expect(LAYUI.open).not.toHaveBeenCalled()
  })

  it("两张卡片的 `cancel`/`end` 都把各自的容器藏回去（对标 `$('.xxx').hide()`）", async () => {
    const root = await mountPage()
    clickToolbar(root, 'recTotal')
    LAYUI.hide.mockClear()
    const args = LAYUI.open.mock.calls[0]![0] as OpenArgs
    args.cancel()
    args.end()
    // `$('.recTotalDiv')` 建了两次（cancel / end 各一次），每次建的替身共用同一个 `hide` spy。
    expect(LAYUI.jq).toHaveBeenCalledWith('.recTotalDiv')
    expect(LAYUI.hide).toHaveBeenCalledTimes(2)
  })

  it('非 PC 端开卡片后补一次 `layer.full(index)`（对标原文 `if (!isPc)`）', async () => {
    // 移动端判据来自 `layui.device()` 的 `android/ios`；替身换成 android。
    const original = LAYUI.layui.device
    LAYUI.layui.device = () => ({ android: true, ios: false })
    try {
      const root = await mountPage()
      clickToolbar(root, 'recTotal')
      expect(LAYUI.full).toHaveBeenCalledWith(7)
      // 移动端的 `area`/`closeBtn`/`maxmin` 也换成另一支。
      const args = LAYUI.open.mock.calls[0]![0] as OpenArgs
      expect(args.area).toEqual(['95%', '95%'])
      expect(args.closeBtn).toBe(1)
      expect(args.maxmin).toBe(true)
      expect(args.shadeClose).toBe(false)
    } finally {
      LAYUI.layui.device = original
    }
  })

  /* ── ：「设置游戏权限」= 对标那个独立 iframe 子页 ── */

  it('点「设置游戏权限」：开的是 iframe 子页（`type:2` + `uid` 逗号串），`end` 里刷新列表', async () => {
    // 勾选行由 `table.checkStatus` 决定；替身默认回空 ⇒ 这条用例临时换成有勾选。
    // （替身那个空数组推出来的是 `never[]`，所以这里显式标注。）
    const original = LAYUI.layui.table.checkStatus
    LAYUI.layui.table.checkStatus = () => ({ data: [{ id: 3 }, { id: 9 }] as never[] })
    try {
      const root = await mountPage()
      LAYUI.tableReload.mockClear()
      clickToolbar(root, 'games')
      expect(LAYUI.open).toHaveBeenCalledTimes(1)
      const args = LAYUI.open.mock.calls[0]![0] as unknown as {
        title: string
        type: number
        skin: string
        anim: number
        area: string[]
        content: string
        end: () => void
      }
      expect(args.title).toBe('设定游戏权限')
      expect(args.type).toBe(2)
      expect(args.skin).toBe('layui-layer-lan')
      expect(args.anim).toBe(5)
      expect(args.area).toEqual(['80%', '30%'])
      // `page=` 的值必须与子页文件名 `system-setgames.ts` 逐字一致（写错就是「未登记的 island 页面」）。
      expect(args.content).toBe('/layui-island/page.html?page=system-setgames&uid=3,9')
      // 父子通信的**父侧**：子页自己 `parent.layer.close(...)`，父页在 `end` 里刷新列表。
      // 这条钉住 `end` 真的接上了（子页那条关弹层的调用见 `system-setgames.spec.ts` 与真浏览器验证）。
      LAYUI.tableReload.mockClear()
      args.end()
      expect(LAYUI.tableReload).toHaveBeenCalledWith('tableList_user', { page: { curr: 1 } })
    } finally {
      LAYUI.layui.table.checkStatus = original
    }
  })

  it('没勾选任何账号时点「设置游戏权限」：只提示**对标那句**、**不开**弹层（`uid` 在开弹层时就要定下来）', async () => {
    const root = await mountPage()
    clickToolbar(root, 'games')
    // 期望值写字面值、出处是对标 `userlist.js` 的 i18n `tableList_user`（E-0024：别拿同一个符号自指）。
    expect(LAYUI.msg).toHaveBeenCalledWith('请勾选要操作的数据行', { time: 1500 })
    expect(LAYUI.open).not.toHaveBeenCalled()
  })

  it('子表工具栏按钮（`data-sub`）点了提示「尚未开放」；主工具栏不受影响', async () => {
    const root = await mountPage()
    // 真 layui 会把 `#toolbarDemo2` 模板的 innerHTML 搬进 `.layui-table-tool`；替身不搬，
    // 所以这里手动放一颗同形按钮进 DOM，验证 root 上的委托**认 `data-sub`**。
    root.insertAdjacentHTML('beforeend', '<button data-sub="refresh2" type="button">刷新列表</button>')
    root.querySelector<HTMLButtonElement>('[data-sub="refresh2"]')!.click()
    expect(LAYUI.msg).toHaveBeenCalledWith('「刷新列表」尚未开放', { time: 1500 })
    // 既有主工具栏那条「尚未开放」路径不能被新分支挡住：
    // `notOpen` 动作的第一颗是「发起/更改签约」（`data-label` 给出文案）。
    LAYUI.msg.mockClear()
    clickToolbar(root, 'notOpen')
    expect(LAYUI.msg).toHaveBeenCalledWith('「发起/更改签约」尚未开放', { time: 1500 })
  })
})
