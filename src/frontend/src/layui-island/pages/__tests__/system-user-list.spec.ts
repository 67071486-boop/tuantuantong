// @vitest-environment jsdom
import { existsSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setAccountStatus, updateAccount, type AccountRow } from '@/api/accounts'
// 「禁止结算」真开关那组用例要断言失败路径露出的后端 msg（`main` 侧同款）。
import { BizError } from '@/api/errors'
import * as systemUsersConfig from '@/config/systemUsers'
import { USER_TOOLBAR } from '@/config/systemUsers'
import {
  ADD_EDIT_USER_DIALOG_BASE,
  ALIPAY_CHECK_TEXT,
  BUSINESS_TYPE_OPTIONS,
  CARD_TITLES,
  CHECK_DROPDOWN_MENU,
  DELETE_CONFIRM_TEXTS,
  DIALOG_MESSAGES,
  ORDER_BY_PROMPT_BASE,
  REC_PRICE_DIALOG_BASE,
  SELF_COUNT_PROMPT_BASE,
  SET_GAMES_DIALOG_BASE,
  TAG_PROMPT_BASE,
  TOOLBAR_BUTTONS_FOR_TEST,
  TYPE_DIALOG_BASE,
  addEditUserDialogContent,
  addEditUserDialogTitle,
  alipayCheckCols,
  alipayCheckTableOptions,
  buildAlipayCheckRows,
  buildCols,
  buildUserListWhere,
  cardDialogArgs,
  defaultRecTotalRange,
  isPositiveInt,
  mountUserListPage,
  pageHtmlForTest,
  recPriceDialogArea,
  recPriceValidationMessage,
  recTotalCols,
  setGamesDialogContent,
  toolbarHtml,
  typeDialogArea,
  type OrderStatsRow,
} from '../system-user-list'
// 「新开 / 编辑系统子账户」子页自己声明的 key（与本文件弹层用例的 `content` 必须逐字一致）。
// 从**声明它的页面模块**取，与 `replicaRoutes.spec.ts` 取 `SET_GAMES_PAGE_KEY` 是同一手法。
import { USER_FORM_PAGE_KEY } from '../user-adduser'

/**
 * layui 的**可控替身**（真 layui 在 jsdom 里加载不了）。`vi.hoisted` 是因为 `vi.mock` 的工厂
 * 会被提升到 import 之前。手法照 `system-systemlog-mount.spec.ts` / `emp-emplist.spec.ts`。
 *
 * ⚠️ 本页的 `$` 是**用选择器字符串**调的（`openCard` 里 `content: $(containerSelector)`、
 * `$(containerSelector).hide()`），所以替身返回 `{ el, hide }`：`el` 让用例能认出弹的是哪个容器，
 * `hide` 让 `cancel`/`end` 不炸。
 */
const LAYUI = vi.hoisted(() => {
  const formHandlers = new Map<string, (...args: unknown[]) => unknown>()
  const tableHandlers = new Map<string, (...args: unknown[]) => unknown>()
  /** 每次 `layer.open` 的入参（用例要断「弹的是哪张卡片 / 标题 / area / content 是谁」）。 */
  const openCalls: Record<string, unknown>[] = []
  const layer = {
    msg: vi.fn(),
    open: vi.fn((config: Record<string, unknown>) => {
      openCalls.push(config)
      return openCalls.length
    }),
    close: vi.fn(),
    confirm: vi.fn(),
    prompt: vi.fn(),
    full: vi.fn(),
  }
  return {
    formHandlers,
    tableHandlers,
    openCalls,
    layer,
    form: {
      render: vi.fn(),
      val: vi.fn((): Record<string, string> => ({})),
      on: (e: string, cb: (...args: unknown[]) => unknown) => formHandlers.set(e, cb),
    },
    table: {
      render: vi.fn(),
      reload: vi.fn(),
      checkStatus: vi.fn(() => ({ data: [] as unknown[] })),
      on: (e: string, cb: (...args: unknown[]) => unknown) => tableHandlers.set(e, cb),
    },
    laydate: { render: vi.fn() },
    dropdown: { render: vi.fn() },
    formSelects: { render: vi.fn(), value: vi.fn((): unknown => ''), config: vi.fn() },
    element: { init: vi.fn() },
    // 桌面形态（`isPc = true`）⇒ 走「PC 端」那套弹层实参。
    device: () => ({ os: 'mac', android: false, ios: false, mobile: false }),
    $: (sel: string) => ({ el: document.querySelector(sel), hide: vi.fn() }),
  }
})

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return {
    ...actual,
    bootstrapIsland: (async () => LAYUI) as unknown as typeof actual.bootstrapIsland,
  }
})

vi.mock('@/layui/vendor', () => ({
  initFormSelects: vi.fn(),
  getFormSelects: () => LAYUI.formSelects,
  ensureBenchmarkVendor: vi.fn(async () => undefined),
}))

// 页面在 `mount` 里就取角色与游戏：不打桩会走真 `request()`（jsdom 里发真请求）。
vi.mock('@/api/accounts', () => ({
  fetchAccountRoles: vi.fn(async () => ['客服']),
  listAccounts: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 200 })),
  resetAccountPassword: vi.fn(async () => undefined),
  // ⚠️ 必须返回 **Promise**：开关处理器是 `setAccountStatus(...).then(...)`，返回 `undefined`
  // 会让「有效状态」那条用例炸在 `.then` 上（看起来像页面代码错，其实是桩不完整）。
  setAccountStatus: vi.fn(async () => ({ id: 0 })),
  setAccountsStatus: vi.fn(async () => undefined),
  updateAccount: vi.fn(async () => ({ id: 0 })),
}))
vi.mock('@/api/config', () => ({
  fetchGames: vi.fn(async () => [{ id: 1, name: '原神' }]),
  fetchAllGames: vi.fn(async () => [{ id: 1, name: '原神' }]),
}))
// 本页挂载时会调支付宝查重（卡片打开才取数），但打桩掉免得 jsdom 真发请求。
vi.mock('@/api/performances', () => ({
  fetchAlipayDuplicates: vi.fn(async () => ({ duplicates: [] })),
}))

// 挂载时还要判一次「押金额度」按钮的权限门（`fetchMe` + `canNode`）：不打桩会走真
// `request()`。默认给一个**没有任何节点**的用户 ⇒ 按钮不画（既有断言看到的仍是 14 颗）。
vi.mock('@/api/auth', () => ({ fetchMe: vi.fn(async () => ({ menu_permissions: [] })) }))

import { fetchAllGames, fetchGames } from '@/api/config'

/** 列的标题/隐藏标记（`Col` 是 `Record<string, unknown>`，这里统一收窄）。 */
function titled(cols: Record<string, unknown>[]): { title: string; hide: boolean }[] {
  return cols
    .filter((c) => typeof c.title === 'string')
    .map((c) => ({ title: c.title as string, hide: c.hide === true }))
}

describe('人员管理 layui 页的声明式部分', () => {
  it('工具栏 14 颗按钮，顺序与文案对齐对标 `#toolbarDemo`；尺寸是 `-xs` 不是 `-sm`', () => {
    const doc = new DOMParser().parseFromString(toolbarHtml(), 'text/html')
    const buttons = Array.from(doc.querySelectorAll('button[data-tb]'))
    // 14 颗 = 对标 21 颗裁掉 7 颗（设置标记 / 禁止看订单密码 / 允许看订单密码 /
    // 禁止看订单电话 / 允许看订单电话 / 设置排序 / 组建群聊），顺序保持对标原文顺序。
    expect(buttons.length).toBe(14)
    // 「刷新列表」前面有一个 stock 图标字形（U+E669，私有区），比对文案时先剥掉。
    const texts = buttons.map((b) => (b.textContent ?? '').replace(/[\uE000-\uF8FF]/g, '').trim())
    expect(texts).toEqual(USER_TOOLBAR.map((t) => t.label))
    // 尺寸：对标 21 颗全是 `layui-btn-xs`（22px）。用 `-sm`（30px）会「大一点点」。
    for (const b of buttons) {
      expect(b.className).toContain('layui-btn-xs')
      expect(b.className).not.toContain('layui-btn-sm')
    }
    // 配色映射抽三颗：新增=红、刷新=默认绿、禁用启用=橙（`layui-btn-warm`）
    expect(buttons[0]!.className).toContain('layui-btn-danger')
    expect(buttons[1]!.className).not.toContain('layui-btn-normal')
    expect(buttons[2]!.className).toContain('layui-btn-warm')
  })

  it('`toolbarHtml(true)` = 14 颗对标 + 自有「押金额度」，且新按钮**紧跟「设置游戏权限」之后**', () => {
    // ⭐ 这颗是**自有**按钮（对标没有），所以它**不进**
    // `TOOLBAR_BUTTONS` / `USER_TOOLBAR`（那两份必须仍是 14 颗，见上一条与下一条）。
    // 默认 `false` ⇒ 无权限用户与既有测试看到的仍是 14 颗（fail-closed，D-0154 / E-0098）。
    const doc = new DOMParser().parseFromString(toolbarHtml(true), 'text/html')
    const buttons = Array.from(doc.querySelectorAll('button[data-tb]'))
    expect(buttons.length).toBe(15)
    const texts = buttons.map((b) => (b.textContent ?? '').replace(/[\uE000-\uF8FF]/g, '').trim())
    // 除新按钮外，其余 14 颗的文案/顺序**逐字不变**。
    expect(texts.filter((t) => t !== '押金额度')).toEqual(USER_TOOLBAR.map((t) => t.label))
    const gamesIndex = texts.indexOf('设置游戏权限')
    expect(gamesIndex).toBeGreaterThanOrEqual(0)
    expect(texts[gamesIndex + 1]).toBe('押金额度')
    const added = buttons[gamesIndex + 1]!
    expect(added.getAttribute('data-tb')).toBe('depositQuota')
    expect(added.getAttribute('data-label')).toBe('押金额度')
    // `layui-btn-xs` + **橙色**（`layui-btn-warm`）：业务方要求这颗自有按钮
    // 与对标的绿/蓝按钮区分、凸显出来（提交 `4bc488f5`）；⛔ 不是 `-sm`、也不是默认绿。
    expect(added.className).toContain('layui-btn-xs')
    expect(added.className).not.toContain('layui-btn-sm')
    expect(added.className).toContain('layui-btn-warm')
    expect(added.getAttribute('style')).toBe('margin-right:5px')
  })

  it('工具栏逐字标记与 `USER_TOOLBAR`（config 里的文案/顺序/配色）不漂移', () => {
    // `TOOLBAR_BUTTONS_FOR_TEST` 是「照抄 `#toolbarDemo` 原文」的那份（含图标与 style），
    // `USER_TOOLBAR` 是 config 里那份（含 action/needsSelection）。两份必须同序同文案同配色，
    // 否则页面会出现「按钮点了没反应」或「配色对不上对标」。
    expect(TOOLBAR_BUTTONS_FOR_TEST.length).toBe(USER_TOOLBAR.length)
    const COLOR_CLASS = { success: '', primary: 'layui-btn-normal', danger: 'layui-btn-danger', warning: 'layui-btn-warm' } as const
    const expectedCls = TOOLBAR_BUTTONS_FOR_TEST.map((b, i) => {
      const cfg = USER_TOOLBAR[i]!
      expect(b.label).toBe(cfg.label)
      expect(b.action).toBe(cfg.action)
      const colorClass = COLOR_CLASS[cfg.type]
      // 对标那颗按钮的 class 里必须带着 config 声明的颜色类（`success` = 无颜色类）。
      expect(b.cls.split(/\s+/)).toContain('layui-btn-xs')
      // 把「该带/不该带颜色类」折成两个可比较的布尔数组，避免在条件里 expect（eslint 禁）。
      return [colorClass === '' ? !/layui-btn-(normal|danger|warm)/.test(b.cls) : b.cls.includes(colorClass), b.line] as const
    })
    expect(expectedCls.every(([ok]) => ok)).toBe(true)
    // 行号是「这条是从对标哪一行抄的」的唯一凭据，必须是 14 个互不相同的实测行号
    //（被删掉的 7 颗占的是 283 / 288 / 289 / 290 / 291 / 292 / 299）。
    expect(expectedCls.map(([, line]) => line)).toEqual([
      279, 280, 281, 282, 284, 285, 286, 287, 293, 294, 295, 296, 297, 298,
    ])
  })

  it('主表 22 个有标题的列（对标 26 列裁掉 4 个开关列），其中对标 hide:true 的 6 列默认隐藏', () => {
    // 22 列 = 对标 26 列（`UNSUPPORTED_USER_SWITCHES` 展开的 6 列）裁掉 4 个开关列，
    // 只剩 `REAL_SWITCHES` 的 2 项 ⇒ 有标题的列 20+6=26 变 20+2=22，可见列 20 → 16。
    // ⚠️ 对标主表仍是 26 个有标题列（证据文档 §2.3 的 #1–#26），22 是**我方有意裁剪**后的数，
    // 本页从此不再 1:1——这是业务方的决定，不是缺陷。
    // ⭐ 持 `finance.taker_wallet_adjust` 的人多一列自有列
    //    「个人押金额度」（`buildCols(..., true)`）⇒ 23 个有标题列 / 17 个可见列。
    //    下面那条用例单独钉它；本行钉的是**无节点**时那 22 列**没漂**（fail-closed）。
    const all = titled(buildCols(new Map()))
    expect(all.length).toBe(22)
    expect(all.filter((c) => c.hide).map((c) => c.title).sort()).toEqual(
      ['姓名', '签约', '完单率', '接单价限制', '最后登录', '开户人'].sort(),
    )
    expect(all.filter((c) => !c.hide).length).toBe(16)
    expect(all.map((c) => c.title)).not.toContain('个人押金额度')
  })

  it('⭐ 自有列「个人押金额度」：只在持节点时插进「保证金」之后，且原 22 列逐字没漂', () => {
    // 加一列必须**同一次**改「恰好集合」断言（AGENTS §5-G）——但这里的判据不是把 22 改成 23
    // 就完事：那会让「谁被挤掉了 / 顺序变了」静默通过。所以拆成两半：
    //   ① `buildCols(..., true)` 的列集合 = **原来那 22 列逐字（顺序也逐字）** + 新列（插在
    //      「保证金」之后）：先断言「去掉新列后的序列 === 原来那 22 列」；
    //   ② 再断言新列只多这一个、位置正确。
    // ⚠️ 原 22 列的字面量列表是**手工枚举**的（不是从 `userlist.js` 现算）——它正是
    //    「这 22 列没漂」这条断言的判据本体，⛔ 别改成 `toContain`（那会放过删除与重排）。
    const ORIGINAL_22_TITLES = [
      '登录账号',
      '账号昵称',
      '姓名',
      '业类',
      '签约',
      '角色职位',
      '保证金',
      '接单上限',
      '完单率',
      '接单价限制',
      'QQ/微信',
      '电话',
      '备注说明',
      '有效状态',
      '禁止结算',
      '禁自助接单',
      '标记',
      '游戏权限',
      '最后登录',
      '排序值',
      '开户人',
      '操作',
    ] as const
    const original = titled(buildCols(new Map())) // 无节点 ⇒ 22 列
    expect(original.map((c) => c.title)).toEqual([...ORIGINAL_22_TITLES])

    const withOwned = titled(buildCols(new Map(), true))
    expect(withOwned.length).toBe(23)
    expect(withOwned.filter((c) => !c.hide).length).toBe(17)
    // ① 去掉自有列之后，原 22 列**逐字逐序**不变（这是「没漂」的判据）。
    expect(withOwned.map((c) => c.title).filter((t) => t !== '个人押金额度')).toEqual([
      ...ORIGINAL_22_TITLES,
    ])
    // ② 自有列只有一个、且紧跟「保证金」（押金口径的开关放在金额旁边）。
    const titles = withOwned.map((c) => c.title)
    expect(titles.filter((t) => t === '个人押金额度')).toHaveLength(1)
    expect(titles.indexOf('个人押金额度')).toBe(titles.indexOf('保证金') + 1)
    // ③ 它是**可见列**（不 hide）、居中、定宽（不是自动列——自动列会被均分撑开）。
    const owned = buildCols(new Map(), true).find(
      (c) => c.title === '个人押金额度',
    ) as { field: string; hide?: boolean; width?: number; align?: string; templet: unknown }
    expect(owned.field).toBe('deposit_quota_enabled')
    expect(owned.hide).toBeUndefined()
    expect(owned.align).toBe('center')
    expect(owned.width).toBe(110)
  })

  it('⭐「个人押金额度」列是 layui switch：`=== true` 才勾、带 `data-id` 与自己的 filter', () => {
    const col = buildCols(new Map(), true).find(
      (c) => c.title === '个人押金额度',
    ) as { templet: (d: Record<string, unknown>) => string }
    const on = col.templet({ id: 5, deposit_quota_enabled: true })
    expect(on).toContain('lay-skin="switch"')
    expect(on).toContain('lay-text="开|关"')
    expect(on).toContain('lay-filter="depositQuotaEnabled"')
    expect(on).toContain('data-id="5"')
    expect(on).toContain('checked')
    // 两条边界：显式 false 与键缺失（无档案行）都**不勾**（E-0021）。
    expect(col.templet({ id: 5, deposit_quota_enabled: false })).not.toContain('checked')
    expect(col.templet({ id: 5 })).not.toContain('checked')
  })

  it('列顺序与对标一致（抽样）', () => {
    const all = titled(buildCols(new Map())).map((c) => c.title)
    expect(all.slice(0, 5)).toEqual(['登录账号', '账号昵称', '姓名', '业类', '签约'])
    expect(all[all.length - 1]).toBe('操作')
    expect(all).toContain('保证金')
  })

  it('勾选列固定在最左；操作列**不固定**（对标是 `fixed: \'\'`）、宽 88', () => {
    const cols = buildCols(new Map())
    expect(cols[0]!.type).toBe('checkbox')
    expect(cols[0]!.fixed).toBe('left')
    expect(cols[0]!.width).toBe(30)
    const last = cols[cols.length - 1]!
    expect(last.title).toBe('操作')
    expect(last.width).toBe(88)
    // 对标 `userlist.js:801` 是 `fixed: ''` —— 我上一版错加了 fixed:right。
    expect(last.fixed).toBeUndefined()
  })

  it('列宽逐条对齐对标（抽几条上一版抄大的）', () => {
    const cols = buildCols(new Map())
    const byTitle = new Map(cols.filter((c) => typeof c.title === 'string').map((c) => [c.title as string, c]))
    expect(byTitle.get('业类')!.width).toBe(50)
    expect(byTitle.get('保证金')!.width).toBe(50)
    expect(byTitle.get('有效状态')!.width).toBe(75)
    expect(byTitle.get('登录账号')!.maxWidth).toBe(110)
    expect(byTitle.get('账号昵称')!.maxWidth).toBe(110)
  })

  it('对标写 `minwidth`（小写 w，layui 不认）的 5 列**保持自动列**：不许补 width/minWidth', () => {
    // 这是差异 #7（横向溢出）的根因：给这 5 列补上合法的 `minWidth`/`width` 会把自动列撑开，
    // 总宽 1649 → 1669（超过视口 1650）→ 出现横向滚动条（对标的 `minwidth` 是小写 w，layui 不认）。
    const cols = buildCols(new Map())
    const byTitle = new Map(cols.filter((c) => typeof c.title === 'string').map((c) => [c.title as string, c]))
    for (const title of ['登录账号', '账号昵称', '备注说明', '标记', '游戏权限']) {
      const col = byTitle.get(title)!
      expect(col.width, `${title} 不该有 width`).toBeUndefined()
      expect(col.minWidth, `${title} 不该有 minWidth`).toBeUndefined()
    }
  })

  it('`<form class="layui-form">` 包住整页（筛选栏 → 表格 → 隐藏弹层），不是只包筛选栏', () => {
    const html = pageHtmlForTest(['客服'], [])
    const doc = new DOMParser().parseFromString(html, 'text/html')
    const form = doc.querySelector('form.layui-form')
    expect(form).toBeTruthy()
    // 表格必须在 form 里面（上一版在外面）。
    expect(form!.querySelector('table#tableList_user')).toBeTruthy()
    // 4 个对标页内隐藏容器也必须在里面（探针按文档级选择器数 `.layui-form-label`，少一段条数就少）。
    const hiddenFound = ['.recPricebox', '.typebox', '.recTotalDiv', '.checkAliPayAccDiv'].map(
      (sel) => form!.querySelector(sel) !== null,
    )
    expect(hiddenFound).toEqual([true, true, true, true])
    // 隐藏容器里的下拉/输入框计数：筛选栏 10 个 label + 隐藏容器 5 个 = 15（对标实测 16 减去被裁的「组群状态」）。
    expect(form!.querySelectorAll('.layui-form-label').length).toBe(15)
    // 10 个筛选 + 1 个搜索按钮 + 隐藏容器 5 个 = 16（对标实测 17 减去被裁的「组群状态」）。
    expect(form!.querySelectorAll('.layui-input-inline').length).toBe(16)
  })

  it('统计文案 `#total` 在工具栏模板里、且在按钮容器**之后**（所以显示在按钮下方、表格上方）', () => {
    const html = toolbarHtml()
    const iBtn = html.indexOf('layui-btn-container')
    const iTotal = html.indexOf('id="total"')
    expect(iBtn).toBeGreaterThanOrEqual(0)
    expect(iTotal).toBeGreaterThan(iBtn)
    // 对标原文的色值就是 #ff6a00（不是 #FF5722）。
    expect(html).toContain('#ff6a00')
  })

  it('操作列：编辑 + 重置**都在**，且重置是红色（对标 `layui-btn-danger`）', () => {
    const cols = buildCols(new Map())
    const op = cols[cols.length - 1] as { templet: (d: unknown) => string }
    const html = op.templet({ id: 7 })
    expect(html).toContain('data-act="edit"')
    expect(html).toContain('data-act="reset"')
    expect(html).toContain('layui-btn-danger')
  })

  it('角色职位用 `.lableb` 徽标（#9ACD32）；账号昵称是**纯文字链接**（不带内/外徽标）', () => {
    const cols = buildCols(new Map())
    const byTitle = new Map(
      cols
        .filter((c) => typeof c.title === 'string')
        .map((c) => [c.title as string, c as { templet: (d: unknown) => string }]),
    )
    const role = byTitle.get('角色职位')!.templet({ role: '客服' })
    expect(role).toContain('lableb')
    expect(role).toContain('#9ACD32')
    // 对标 `userName` 的 templet 里那个内/外徽标是**死代码**（拼了没拼进 vo），我们也不渲染。
    const nick = byTitle.get('账号昵称')!.templet({ id: 1, is_active: true, display_name: '小航' })
    expect(nick).not.toContain('layui-badge')
    expect(nick).toContain('小航')
  })
})

describe('人员管理：业务类型 radio（D-0086）', () => {
  const typeboxRadios = (): Element[] => {
    const doc = new DOMParser().parseFromString(pageHtmlForTest(['客服'], []), 'text/html')
    return Array.from(doc.querySelectorAll('.typebox input[name=selbussType]'))
  }

  it('三项顺序是 专做代练 → 专做陪玩 → 代练+陪玩（与「新开系统账号」页一致）', () => {
    expect(BUSINESS_TYPE_OPTIONS.map((o) => o.value)).toEqual([1, 2, 0])
    expect(BUSINESS_TYPE_OPTIONS.map((o) => o.label)).toEqual(['专做代练', '专做陪玩', '代练+陪玩'])
    const radios = typeboxRadios()
    // ⚠️ 对标的原文顺序是 `0/1/2`，这里是有意偏离（D-0086）——见实现处的注释。
    expect(radios.map((r) => r.getAttribute('value'))).toEqual(['1', '2', '0'])
    expect(radios.map((r) => r.getAttribute('title'))).toEqual(['专做代练', '专做陪玩', '代练+陪玩'])
  })

  it('三项都不预选（多选且各行取值不同时，预选任意一项都会把存量行静默改值）', () => {
    // 「没选就保存」由保存时那句「请选择业务类型」拦住（`DIALOG_MESSAGES.businessTypeRequired`）。
    expect(typeboxRadios().some((r) => r.hasAttribute('checked'))).toBe(false)
    // 边界（E-0021）：`0`（代练+陪玩）是**合法值**，必须作为一个独立的、可选的 radio 存在。
    expect(typeboxRadios().some((r) => r.getAttribute('value') === '0')).toBe(true)
  })
})

describe('人员管理：两个页内隐藏容器与对标原文（改动 1）', () => {
  const pageDoc = (): Document => new DOMParser().parseFromString(pageHtmlForTest(['客服'], []), 'text/html')

  it('`.recPricebox` 两个输入框带 `id=price_min` / `id=price_max`（对标的 JS 按 id 取值）', () => {
    const min = pageDoc().querySelector('.recPricebox input[name=price_min]')
    const max = pageDoc().querySelector('.recPricebox input[name=price_max]')
    expect(min?.getAttribute('id')).toBe('price_min')
    expect(max?.getAttribute('id')).toBe('price_max')
    // 默认值照对标：0 / 99999。
    expect(min?.getAttribute('value')).toBe('0')
    expect(max?.getAttribute('value')).toBe('99999')
  })

  it('`.recPricebox` 保存按钮 class 逐字为 `layui-btn layui-btn-normal setRecPriceSaveBtn`，且**没有** `data-notopen`', () => {
    const btn = pageDoc().querySelector('.recPricebox a')
    expect(btn?.getAttribute('class')).toBe('layui-btn layui-btn-normal setRecPriceSaveBtn')
    expect(btn?.hasAttribute('data-notopen')).toBe(false)
  })

  it('`.typebox` 保存按钮 class 逐字为 `layui-btn layui-btn-normal settypes`，且**没有** `data-notopen`', () => {
    const btn = pageDoc().querySelector('.typebox a')
    expect(btn?.getAttribute('class')).toBe('layui-btn layui-btn-normal settypes')
    expect(btn?.hasAttribute('data-notopen')).toBe(false)
  })

  it('整页已无 `data-notopen`（处理器与两条监听随之删除）', () => {
    expect(pageHtmlForTest(['客服'], [])).not.toContain('data-notopen')
  })
})

describe('人员管理：四个弹层的实参逐字对拍（改动 2）', () => {
  it('「设置业务类型」：title / shade / shadeClose / type / skin / anim + area 两分支', () => {
    expect(TYPE_DIALOG_BASE).toEqual({
      title: '修改业务类型',
      shade: [0.3, '#333'],
      shadeClose: true,
      type: 1,
      skin: 'layui-layer-lan',
      anim: 5,
    })
    expect(typeDialogArea(true)).toEqual(['700px', '300px'])
    expect(typeDialogArea(false)).toEqual(['80%', '300px'])
  })

  it('「设抢单价范围」：title / shade / shadeClose / type / skin / anim + area 两分支', () => {
    expect(REC_PRICE_DIALOG_BASE).toEqual({
      title: '批量设置内部抢单价格范围限制',
      shade: [0.3, '#333'],
      shadeClose: true,
      type: 1,
      skin: 'layui-layer-lan',
      anim: 5,
    })
    expect(recPriceDialogArea(true)).toEqual(['410px', '230px'])
    expect(recPriceDialogArea(false)).toEqual(['85%', '230px'])
  })

  it('两个 `layer.prompt` 的实参（title / formType / maxlength 逐字）', () => {
    expect(TAG_PROMPT_BASE).toEqual({ title: '设置用户标记，不需要请填写：无', formType: 0, maxlength: 100 })
    expect(ORDER_BY_PROMPT_BASE).toEqual({
      title: '设置指派接手人时的排序值，值越大越在前，默认0',
      formType: 0,
      maxlength: 4,
    })
  })

  it('抢单价 6 条校验：文案逐字、**顺序**照对标 `userlist.js:1661-1687`', () => {
    expect(recPriceValidationMessage('', '1')).toBe('请输入最小接单价，不能小于0')
    expect(recPriceValidationMessage('0', '')).toBe('请输入最大接单价，不能大于99999')
    expect(recPriceValidationMessage('abc', '1')).toBe('最小接单价格式不正确，必须是0到99999之间的整数')
    expect(recPriceValidationMessage('1', 'abc')).toBe('最大接单价格式不正确，必须是0到99999之间的整数')
    expect(recPriceValidationMessage('-1', '1')).toBe('最小接单价必须大于或等于0')
    expect(recPriceValidationMessage('1', '100000')).toBe('最大接单价必须小于或等于99999')
    // 全过 ⇒ null（**没有**第 7 条，也**没有**「上限 ≥ 下限」——那条由后端负责）。
    expect(recPriceValidationMessage('0', '99999')).toBeNull()
    expect(recPriceValidationMessage('100', '200')).toBeNull()
    // 顺序：`min` 的格式错优先于 `max` 的空值错（前两条空值校验在前）。
    expect(recPriceValidationMessage('abc', '')).toBe('请输入最大接单价，不能大于99999')
    // 对标 `VerifyMoeny` **允许小数**（尽管提示文案写的是「整数」）——照抄，不「修正」。
    expect(recPriceValidationMessage('0.5', '1.25')).toBeNull()
  })

  it('排序值校验：正整数（`/^\\d+$/`）与那句提示', () => {
    expect(isPositiveInt('0')).toBe(true)
    expect(isPositiveInt('12')).toBe(true)
    expect(isPositiveInt('9999')).toBe(true)
    expect(isPositiveInt('-1')).toBe(false)
    expect(isPositiveInt('1.5')).toBe(false)
    expect(isPositiveInt('abc')).toBe(false)
    // ⚠️ 这两条是**有意比对标的 `VerifyInt`（`/^\+?[0-9]*$/`）紧**的两处：
    // 空串会让 `Number('') === 0` 静默写入 0；纯 `+` 会让 `Number('+') === NaN`。理由见 `isPositiveInt`。
    expect(isPositiveInt('')).toBe(false)
    expect(isPositiveInt('+')).toBe(false)
    expect(DIALOG_MESSAGES.orderByInvalid).toBe('请输入正整数')
  })

  it('标记「值没变不提交」与业务类型「没选就保存」的文案逐字', () => {
    // 断言写字面值，不引任何常量（E-0024：钉「值」，不钉「常量」）。
    expect(DIALOG_MESSAGES.tagUnchanged).toBe('编辑的内容与原先一样，无需提交')
    expect(DIALOG_MESSAGES.businessTypeRequired).toBe('请选择业务类型')
  })

  it('「排序值」列的 templet 渲染 `order_by`，含 `0` 这个边界（渲染成 0，不是空）', () => {
    const col = buildCols(new Map()).find((c) => c.title === '排序值') as {
      templet: (d: { id: number; order_by?: number }) => string
    }
    const zero = col.templet({ id: 7, order_by: 0 })
    expect(zero).toContain('data-act="orderby"')
    expect(zero).toContain('data-id="7"')
    expect(zero).toContain('title="在指派界面按排序值排列时，数值越大越排前面，点击可以编辑"')
    expect(zero).toContain('>0</a>')
    expect(col.templet({ id: 8, order_by: 3 })).toContain('>3</a>')
    // `order_by` 缺省（老数据 / 接口没带）时兜底 `0`，**不要**渲染成空串或 `undefined`。
    expect(col.templet({ id: 9 })).toContain('>0</a>')
  })
})

describe('人员管理：「设置游戏权限」改成 iframe 子页（Task 6）', () => {
  it('`layer.open` 实参逐字（`title` 是「设定游戏权限」、`type:2`、`area` 80%×30%）', () => {
    // 写死字面值（E-0024），不引任何常量。
    expect(SET_GAMES_DIALOG_BASE).toEqual({
      title: '设定游戏权限',
      shade: [0.3, '#333'],
      shadeClose: true,
      type: 2,
      skin: 'layui-layer-lan',
      anim: 5,
      area: ['80%', '30%'],
    })
    // ⚠️ 与工具栏按钮上那句「设置游戏权限」**不是同一句**（对标原文如此），别「统一」掉。
    expect(SET_GAMES_DIALOG_BASE.title).not.toBe('设置游戏权限')
  })

  it('`content` 指向 island 子页、`page=` 的值是子页的文件名（钉住 key 没写错），uid 是逗号串', () => {
    // 字面值（E-0024）：这份 URL 必须与 `pages/system-setgames.ts` 的文件名逐字一致，
    // 否则 `layui-island/page.ts` 的注册表查不到这个 key、弹层里是「未登记的 island 页面」白板。
    expect(setGamesDialogContent([1, 2, 3])).toBe(
      '/layui-island/page.html?page=system-setgames&uid=1,2,3',
    )
    // ⚠️ 基址必须跟着 `BASE_URL` 走：本仓支持把前端挂到子路径（`TENANT_WEB_BASE=/console/`
    // ⇒ `BASE_URL='/console/'`），那时写死的 `/layui-island/page.html` 会**整页 404**。
    // 显式注入 `/console/` 后断**整串字面值**（不是用被测代码同款表达式再算一遍的那种自指断言）：
    // 谁把实现改回硬编码绝对路径，这一条就红（红态证明见 task-13b-report.md）。
    expect(setGamesDialogContent([1, 2, 3], '/console/')).toBe(
      '/console/layui-island/page.html?page=system-setgames&uid=1,2,3',
    )
    expect(setGamesDialogContent([622])).toContain('page=system-setgames')
    expect(setGamesDialogContent([622])).toContain('uid=622')
  })
})

describe('人员管理：「新增 / 编辑系统子账户」改成 iframe 弹层（Task 13）', () => {
  it('`ADD_EDIT_USER_DIALOG_BASE` 实参逐字（`title` 随 id 变，**不进**这个常量）', () => {
    // 字面值（E-0024），逐字取自对标 `userlist.js:2191-2202` 的 `addEditUser`。
    expect(ADD_EDIT_USER_DIALOG_BASE).toEqual({
      shade: [0.3, '#333'],
      shadeClose: true,
      type: 2,
      skin: 'layui-layer-lan',
      anim: 5,
      area: ['90%', '90%'],
    })
    expect(ADD_EDIT_USER_DIALOG_BASE).not.toHaveProperty('title')
  })

  it('标题逐字：`id === 0` ⇒「新开系统子账户」；`id > 0` ⇒「编辑系统子账户」', () => {
    // 期望值写字面值，**不要**写成 `(id > 0 ? '编辑' : '新开') + '系统子账户'` 的自指断言。
    expect(addEditUserDialogTitle(0)).toBe('新开系统子账户')
    expect(addEditUserDialogTitle(9)).toBe('编辑系统子账户')
  })

  it('`content`：子页 key 是 `user-adduser`，参数名是**我方的 `id`**（不是对标的 `uid`）；`id=0` 是新增', () => {
    // 整串字面量：多一个字符、键名写错都要红。
    expect(addEditUserDialogContent(9)).toBe('/layui-island/page.html?page=user-adduser&id=9')
    expect(addEditUserDialogContent(0)).toBe('/layui-island/page.html?page=user-adduser&id=0')
    // ⚠️ 基址必须跟着 `BASE_URL` 走（子路径部署 ⇒ `/console/layui-island/…`）：
    // 显式注入 `/console/` 断整串字面值——这才是能发现「有人把它写死成绝对路径」的判据
    // （`toContain('page=…')` 对基址错误完全不敏感）。红态证明见 task-13b-report.md。
    expect(addEditUserDialogContent(9, '/console/')).toBe(
      '/console/layui-island/page.html?page=user-adduser&id=9',
    )
    expect(addEditUserDialogContent(0, '/console/')).toBe(
      '/console/layui-island/page.html?page=user-adduser&id=0',
    )
    // 单独再钉一次 key（`page=` 的值就是子页的**文件名**，写错 ⇒ 弹层里是「未登记的 island 页面」）。
    expect(addEditUserDialogContent(9)).toContain('page=user-adduser')
    // 与子页模块自己声明的 key、以及**真实文件名**三方对齐，避免手抄一份会漂移的清单。
    expect(addEditUserDialogContent(9)).toContain(`page=${USER_FORM_PAGE_KEY}`)
    const pagesDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
    expect(existsSync(path.join(pagesDir, `${USER_FORM_PAGE_KEY}.ts`))).toBe(true)
  })
})

describe('人员管理：7 个筛选接后端（Task 3）', () => {
  /** 后端 `AccountFilterSerializer` 收的 7 个新参数名（字面值，E-0024）。 */
  const NEW_BACKEND_PARAMS = ['game', 'business_type', 'member_type', 'is_cert', 'settle_forbidden', 'key2', 'key3']

  it('忽略清单已删除：`NOT_OPEN_FILTERS` 与死的 `GROUP_FILTER_OPTIONS` 都不再导出', () => {
    // 用命名空间导入而不是「导入会失败」来断言：导入失败是**构建期**的事，
    // 而这里要的是「这个导出真的没了」。删掉本页那个忽略分支后，两份清单都失去意义。
    expect(Object.keys(systemUsersConfig)).not.toContain('NOT_OPEN_FILTERS')
    expect(Object.keys(systemUsersConfig)).not.toContain('GROUP_FILTER_OPTIONS')
  })

  it('筛选栏标记里 7 个控件的 `name` 都在，且 10 个控件一个都不被丢', () => {
    const doc = new DOMParser().parseFromString(pageHtmlForTest(['客服'], []), 'text/html')
    const names = Array.from(
      doc.querySelectorAll('form.layui-form select[name], form.layui-form input[name]'),
    ).map((el) => el.getAttribute('name'))
    for (const name of ['gameName', 'bussType', 'userType', 'isCert', 'isDisSett', 'key2', 'key3']) {
      expect(names, `筛选栏缺 ${name}`).toContain(name)
    }
    // ⚠️ 这行原来是一条 `expect(doc.body.textContent).not.toContain('本次已忽略')`——
    // **恒真**：那个字符串只存在于 JS 里，从没进过 `pageHtmlForTest` 的标记，所以无论
    // 忽略分支在不在，它都通过（E-0065）。换成一条**能失败**的判据：
    // 10 个控件全给非哨兵值 ⇒ 产出里必须**一个不少**地出现 10 个后端参数名。
    // 只要实现里还留着任何「某些筛选本次忽略」的分支，这里就缺键而变红。
    const all = buildUserListWhere({
      jobRole: '客服', status: 'true', key: 'zhang', gameName: '3', bussType: '1',
      userType: '1', isCert: 'Y', isDisSett: 'Y', key2: 'ali', key3: '备注',
    })
    expect(Object.keys(all).sort()).toEqual([
      'business_type', 'game', 'is_active', 'is_cert', 'key', 'key2', 'key3',
      'member_type', 'role', 'settle_forbidden',
    ])
  })

  it('`buildUserListWhere` 把 10 个控件逐条落到后端参数名上', () => {
    expect(
      buildUserListWhere({
        jobRole: '客服',
        status: 'true',
        key: 'zhang',
        gameName: '3',
        bussType: '1',
        userType: '0',
        isCert: 'Y',
        isDisSett: 'N',
        key2: 'ali-888',
        key3: '备注X',
      }),
    ).toEqual({
      role: '客服',
      is_active: 'true',
      key: 'zhang',
      game: '3',
      business_type: '1',
      member_type: '0',
      is_cert: 'Y',
      settle_forbidden: false,
      key2: 'ali-888',
      key3: '备注X',
    })
    // 7 个新参数的**名字**逐个钉住：`buildUserListWhere` 的产物里必须出现这 7 个键。
    const produced = buildUserListWhere({
      gameName: '3', bussType: '1', userType: '0', isCert: 'Y', isDisSett: 'N', key2: 'a', key3: 'b',
    })
    for (const param of NEW_BACKEND_PARAMS) {
      expect(Object.keys(produced), `没发出 ${param}`).toContain(param)
    }
  })

  it('`studioFlag`（D-0228 身份分档隐藏域）三态：true⇒is_studio=true、false⇒is_studio=false（显式值，E-0021）、-1/空⇒不发送', () => {
    expect(buildUserListWhere({ studioFlag: 'true' }).is_studio).toBe(true)
    expect(buildUserListWhere({ studioFlag: 'false' }).is_studio).toBe(false)
    expect(buildUserListWhere({ studioFlag: '-1' }).is_studio).toBeUndefined()
    expect(buildUserListWhere({}).is_studio).toBeUndefined()
  })

  it('`workerFlag`（D-0228 改准二：打手=代练员职位）三态：true/false 显式发送、-1/空不发送', () => {
    expect(buildUserListWhere({ workerFlag: 'true' }).is_worker).toBe(true)
    expect(buildUserListWhere({ workerFlag: 'false' }).is_worker).toBe(false)
    expect(buildUserListWhere({ workerFlag: '-1' }).is_worker).toBeUndefined()
    expect(buildUserListWhere({}).is_worker).toBeUndefined()
  })

  it('四个「全部」哨兵不发送参数；`settle_forbidden=false` 仍然发送（E-0021）', () => {
    // 全部 = 空串（gameName / isCert / isDisSett / key2 / key3）、'-1'（**bussType** / userType）。
    expect(
      buildUserListWhere({
        jobRole: '', status: '', key: '', gameName: '', bussType: '-1',
        userType: '-1', isCert: '', isDisSett: '', key2: '', key3: '',
      }),
    ).toEqual({})
    // 边界：`bussType` 的哨兵 `-1` 是**真值**，光靠真值判断会把它发出去 —— 这条钉住它被挡下。
    expect(buildUserListWhere({ bussType: '-1' })).toEqual({})
    // 边界：'N' ⇒ false，**必须**出现在 where 里——用真值判断会把它当成「没传」丢掉。
    expect(buildUserListWhere({ isDisSett: 'N' })).toEqual({ settle_forbidden: false })
    expect(buildUserListWhere({ isDisSett: 'Y' })).toEqual({ settle_forbidden: true })
  })

  it('业务类型筛选四项、顺序照 D-0086；「全部」是 `-1`（真浏览器实测：空串会被插件跳过）', () => {
    // 有意偏离对标原文取值（对标「全部」= `0`）：`0` 是 `business_type` 的真实枚举值
    // （代练+陪玩），被「全部」占掉就永远筛不了它。四项顺序与「新开系统账号」页一致。
    // ⚠️ 哨兵**不能是空串**：`formselects-v4` 会把「第一个且 value 为假」的 option 整个跳过
    // （`public/vendor/formselects-v4.js:733-735`）⇒ 下拉里没有「全部」可点，回不到全部。
    // 原始浏览器输出见 `task-4-report.md` 的「改动 0」。
    expect(
      systemUsersConfig.BUSINESS_TYPE_FILTER_OPTIONS.map((o) => [o.label, o.value]),
    ).toEqual([
      ['全部', -1],
      ['代练', 1],
      ['陪玩', 2],
      ['代练+陪玩', 0],
    ])
  })

  it('业务类型「代练+陪玩」（`0`）**必须发出去**（E-0021：真值判断会把它当成「没传」）', () => {
    // 修复前：「全部」占着 `0` ⇒ 这个真实枚举值永远发不出去，UI 上筛不了「代练+陪玩」。
    // 断言写字面值 `'0'`（E-0024），不引任何常量。
    expect(buildUserListWhere({ bussType: '0' })).toEqual({ business_type: '0' })
    expect(buildUserListWhere({ bussType: '1' })).toEqual({ business_type: '1' })
  })

  it('业务类型「全部」⇒ 结果里**没有** `business_type` 这个键（不是「值等于空串」）', () => {
    // 哨兵 `-1`（`formselects-v4` 下的真实选中值），与「插件一项都没选」时的空串**都**不发。
    const where = buildUserListWhere({ bussType: '-1' })
    expect(where).not.toHaveProperty('business_type')
    expect(Object.keys(where)).not.toContain('business_type')
    expect(buildUserListWhere({ bussType: '' })).not.toHaveProperty('business_type')
  })

  it('前后端对拍（前端半边）：`选择职位` 拼成的逗号串**原样**落到 `role`', () => {
    // 与后端 `tests/test_account_api.py::FrontendBackendRoleContractTests` 用**同一个
    // 字面串** `客服,代练员`：前端原样透传 ⇒ 后端按逗号切分并逐个校验。
    // 任一侧改了格式（split / 取第一个 / 换分隔符），另一侧的用例会说话。
    expect(buildUserListWhere({ jobRole: '客服,代练员' })).toEqual({ role: '客服,代练员' })
  })
})

describe('人员管理：检测功能二级下拉 + 两张卡片外壳（Task 5）', () => {
  /** 取某个子表工具栏模板（`<script type="text/html">`）**内部标记**解析出的 DOM。 */
  function subToolbarDoc(id: 'toolbarDemo2' | 'toolbarDemo3'): Document {
    const page = new DOMParser().parseFromString(pageHtmlForTest(['客服'], []), 'text/html')
    const tpl = page.querySelector(`script#${id}`)
    expect(tpl, `页面里没有 #${id} 模板`).toBeTruthy()
    return new DOMParser().parseFromString(tpl!.textContent ?? '', 'text/html')
  }

  /** 子表工具栏最后那行橙字。 */
  function noteOf(id: 'toolbarDemo2' | 'toolbarDemo3'): string | null | undefined {
    return subToolbarDoc(id).querySelector('div[style="color:#ff6a00"]')?.textContent
  }

  it('两个子表工具栏模板都在页面里，各 3 颗按钮（文案 / class / style / type 逐字）', () => {
    const page = new DOMParser().parseFromString(pageHtmlForTest(['客服'], []), 'text/html')
    expect(page.querySelector('script#toolbarDemo2')).toBeTruthy()
    expect(page.querySelector('script#toolbarDemo3')).toBeTruthy()
    for (const id of ['toolbarDemo2', 'toolbarDemo3'] as const) {
      const buttons = Array.from(subToolbarDoc(id).querySelectorAll('button'))
      expect(buttons.map((b) => b.textContent), `${id} 的按钮文案`).toEqual(['刷新列表', '禁用子账号', '删除用户'])
      expect(buttons.map((b) => b.getAttribute('class'))).toEqual([
        'layui-btn layui-btn-xs',
        'layui-btn layui-btn-xs',
        'layui-btn layui-btn-xs layui-btn-danger',
      ])
      expect(buttons.map((b) => b.getAttribute('style'))).toEqual([
        'margin-right:5px',
        'margin-right:5px',
        'margin-right:5px',
      ])
      expect(buttons.map((b) => b.getAttribute('type'))).toEqual(['button', 'button', 'button'])
      // 只有第一颗带 `title`（对标原文），「删除用户」那颗是红色。
      expect(buttons[0]!.getAttribute('title')).toBe('刷新当前数据')
      expect(buttons[2]!.className).toContain('layui-btn-danger')
    }
  })

  it('`#toolbarDemo2` 与 `#toolbarDemo3` 的橙字文案**不同**，且逐字等于对标原文', () => {
    // 这两句是两处的**唯一差异**——写死字面值（E-0024）。
    expect(noteOf('toolbarDemo2')).toBe(
      '这里已排除了禁用的子账号，只统计有效状态为正常的子账号。注意，数据有2分钟缓存，禁用账号、删除子账号均会有缓存',
    )
    expect(noteOf('toolbarDemo3')).toBe('这里包含了系统所有状态下的子账号')
    expect(noteOf('toolbarDemo2')).not.toBe(noteOf('toolbarDemo3'))
  })

  it('两个子表工具栏**没有** `lay-event`（那会让 layui 吞掉点击），改用 `data-sub` 记原值', () => {
    for (const id of ['toolbarDemo2', 'toolbarDemo3'] as const) {
      const doc = subToolbarDoc(id)
      expect(doc.querySelectorAll('[lay-event]').length, `${id} 不该有 lay-event`).toBe(0)
      expect(Array.from(doc.querySelectorAll('button[data-sub]')).map((b) => b.getAttribute('data-sub'))).toEqual([
        'refresh2',
        'disBtn',
        'delUserBtn2',
      ])
    }
  })

  it('「检测功能」是工具栏 14 颗里**唯一**带 `id` 的按钮，id 逐字为 `checkDropdown`', () => {
    const doc = new DOMParser().parseFromString(toolbarHtml(), 'text/html')
    const buttons = Array.from(doc.querySelectorAll('button[data-tb]'))
    expect(buttons.length).toBe(14)
    const withId = buttons.filter((b) => b.hasAttribute('id'))
    expect(withId.length).toBe(1)
    expect(withId[0]!.getAttribute('id')).toBe('checkDropdown')
    expect(withId[0]!.getAttribute('data-tb')).toBe('aliPay')
    expect(withId[0]!.textContent).toContain('检测功能')
  })

  it('两张卡片共用同一组 `layer.open` 实参：PC 端 title / shade / skin / anim / closeBtn / area 逐字', () => {
    expect(CARD_TITLES).toEqual({ recTotal: '接单统计', aliPay: '重复收款支付宝子账号检测' })
    // 写死字面值（E-0024），不引任何常量。
    expect(cardDialogArgs('接单统计', true)).toEqual({
      title: '接单统计',
      shade: [0.3, '#333'],
      shadeClose: true,
      type: 1,
      skin: 'layui-layer-lan',
      anim: 8,
      closeBtn: 0,
      maxmin: false,
      area: ['38%', '90%'],
    })
    expect(cardDialogArgs('重复收款支付宝子账号检测', true)).toEqual({
      title: '重复收款支付宝子账号检测',
      shade: [0.3, '#333'],
      shadeClose: true,
      type: 1,
      skin: 'layui-layer-lan',
      anim: 8,
      closeBtn: 0,
      maxmin: false,
      area: ['38%', '90%'],
    })
    // 电脑端**故意没有关闭按钮**（对标原文 `closeBtn: isPc ? 0 : 1`）。
    expect(cardDialogArgs('接单统计', true).closeBtn).toBe(0)
    // 两张卡片的实参**除 `title` 外全等**。
    expect({ ...cardDialogArgs('接单统计', true), title: '' }).toEqual({
      ...cardDialogArgs('重复收款支付宝子账号检测', true),
      title: '',
    })
  })

  it('两张卡片的非 PC 分支：shadeClose=false / closeBtn=1 / maxmin=true / area 95%×95%', () => {
    expect(cardDialogArgs('接单统计', false)).toEqual({
      title: '接单统计',
      shade: [0.3, '#333'],
      shadeClose: false,
      type: 1,
      skin: 'layui-layer-lan',
      anim: 8,
      closeBtn: 1,
      maxmin: true,
      area: ['95%', '95%'],
    })
  })

  it('二级下拉只有一个菜单项，文案与 `id` 逐字（对标 `userlist.js:1283-1286`）', () => {
    expect(CHECK_DROPDOWN_MENU).toEqual([{ title: '检测重复收款支付宝账号者', id: 10 }])
  })

  it('`#tableList2`（接单统计）8 列：title / width / align 逐条抄自 `userlist.js` L2440-2474', () => {
    const cols = recTotalCols()
    expect(cols.length).toBe(8)
    expect(cols.map((c) => [c.title, c.width, c.align])).toEqual([
      [undefined, 30, undefined],
      ['排名', 55, 'center'],
      ['姓名/名称', undefined, undefined],
      ['接单量', 140, 'center'],
      ['还未结算', 120, 'center'],
      ['完单率', 70, 'center'],
      ['开通日期', 120, 'center'],
      ['最近接单', 120, 'center'],
    ])
    expect(cols[0]).toEqual({ type: 'checkbox', fixed: 'left', width: 30 })
    // 「最近接单」为空显示「无」（取数接真后这一列会拿到空值）。
    const last = cols.find((c) => c.title === '最近接单')!.templet as unknown as (
      d: OrderStatsRow,
    ) => string
    expect(last({ lastRecOrderDate: '' } as OrderStatsRow)).toBe('无')
    expect(last({ lastRecOrderDate: '2026-09-18' } as OrderStatsRow)).toBe('2026-09-18')
    // 完单率拼 `%`（后端给的数值不带 `%`）。
    const rate = cols.find((c) => c.title === '完单率')!.templet as unknown as (
      d: OrderStatsRow,
    ) => string
    expect(rate({ complateRate: 93.98 } as OrderStatsRow)).toContain('93.98%')
  })

  it('`#tableList3`（重复收款支付宝子账号检测）6 列：title / width / align 逐条抄自 L2571-2603', () => {
    const cols = alipayCheckCols()
    expect(cols.length).toBe(6)
    expect(cols.map((c) => [c.title, c.width, c.align])).toEqual([
      [undefined, 30, undefined],
      ['子账号名称', 110, undefined],
      ['支付宝账号', 170, 'left'],
      ['职位', 85, 'center'],
      ['重复数量', 85, 'center'],
      ['当前状态', 85, 'center'],
    ])
    expect(cols[0]).toEqual({ type: 'checkbox', fixed: 'left', width: 30 })
  })

  // ⚠️ `#tableList2` 已接真取数 `GET /api/business/order-stats` ⇒ 旧的「仍是外壳」用例删除；
  // 取数行为由文件末尾的「接单统计卡片：点开 ⇒ 真取数」那组**驱动真交互**的用例钉住
  // （只断配置会漏掉「点了没反应」）。

  it('`#tableList3`（支付宝查重）已接数据：`page:false` + `defaultToolbar:[\'filter\']`，对标**没有** `limits`', () => {
    // 出处：对标 `userlist.js:2572-2605` 的 `table.render`——`page: false`；
    // `defaultToolbar` 在同一个对象字面量里写了两遍（`[]` 后跟 `['filter']`，后写生效）；
    // **没有** `limit` / `limits` 这两个键（`page:false` 之下它们没有落点，别照抄主表的 6 项）。
    const ali = alipayCheckTableOptions()
    expect(ali.elem).toBe('#tableList3')
    expect(ali.toolbar).toBe('#toolbarDemo3')
    expect(ali.data).toEqual([])
    // 空文案是「没有重复的收款支付宝账号」——**不是**「该功能尚未开放」：这张表已经接上数据，
    // 空结果的含义变成了「查过了，没有重复」（E-0004：空结果不等于没有数据）。
    expect(ali.text).toEqual({ none: '没有重复的收款支付宝账号' })
    expect(ali.page).toBe(false)
    expect(ali.defaultToolbar).toEqual(['filter'])
    expect(ali).not.toHaveProperty('limits')
    // 取数走我们自己的 `fetchAlipayDuplicates()`（`data`），仍然**不写** layui 的 `url`。
    expect(ali).not.toHaveProperty('url')
  })
})

describe('人员管理：三颗按钮接线 + 两个开关列变真（Task 7）', () => {
  it('`REAL_SWITCHES` 两项的 `field`/`filter`/`width` 逐字（写死字面值，E-0024）', () => {
    expect(systemUsersConfig.REAL_SWITCHES).toEqual([
      { label: '禁止结算', field: 'settle_forbidden', width: 40, filter: 'settleForbiddenChecked' },
      { label: '禁自助接单', field: 'grab_forbid_self_service', width: 80, filter: 'grabForbidChecked' },
    ])
  })

  it('开关列**恰好 3 个**、顺序 `[is_active, settle_forbidden, grab_forbid_self_service]`（裁定：4 个 `ns_*` 列已删）', () => {
    // ⚠️ 上一条只钉「两个真开关的 `field`/`filter`/`width`」与它们彼此的先后，抓不到
    //    「多出第 4 个开关列」或「`is_active` 被挪走」⇒ 这里**从产出反推**：让每个列的 templet
    //    真的渲染一次，看它吐不吐 `lay-skin="switch"`（判据形式照 `main` 那条「恰好 7 个」，
    //  按业务方裁定改成 3 个：`is_active` → 禁止结算 → 禁自助接单）。
    // 行数据给最小可用集：`games` 列会 `d.games.map(...)`（缺了会 TypeError）。
    const DUMMY_ROW = { id: 1, games: [], is_active: false }
    const switchCols = buildCols(new Map()).filter((c) => {
      const tpl = c.templet as ((d: Record<string, unknown>) => string) | undefined
      return typeof tpl === 'function' && tpl(DUMMY_ROW).includes('lay-skin="switch"')
    })
    expect(switchCols.map((c) => c.field)).toEqual([
      'is_active',
      'settle_forbidden',
      'grab_forbid_self_service',
    ])
    // `main` 侧被删掉的那条还钉了列的对齐与开关文案——裁定只说「列头不带 `<span lay-tips>`、
    // `filter` 用我方事件名」，这两项**没变**，强度照旧保留（确切数组，不是 `toContain`）。
    expect(switchCols.map((c) => c.align)).toEqual(['center', 'center', 'center'])
    expect(
      switchCols.map((c) =>
        parseSwitch((c.templet as (d: Record<string, unknown>) => string)(DUMMY_ROW)).getAttribute(
          'lay-text',
        ),
      ),
    ).toEqual(['正常|禁用', '是|否', '是|否'])
  })

  it('两份工具栏的 `action` 同改：设自助接单数 → selfCount、禁/解结算 → settle，且都要勾选', () => {
    const cfg = new Map(USER_TOOLBAR.map((t) => [t.label, t]))
    expect(cfg.get('设自助接单数')!.action).toBe('selfCount')
    expect(cfg.get('设自助接单数')!.needsSelection).toBe(true)
    expect(cfg.get('禁/解结算')!.action).toBe('settle')
    expect(cfg.get('禁/解结算')!.needsSelection).toBe(true)
    // 标记那份（`TOOLBAR_BUTTONS`，逐字照抄对标）必须同值——漂移对拍另有一条用例守着。
    const markup = new Map(TOOLBAR_BUTTONS_FOR_TEST.map((b) => [b.label, b]))
    expect(markup.get('设自助接单数')!.action).toBe('selfCount')
    expect(markup.get('禁/解结算')!.action).toBe('settle')
  })

  it('两个开关列的 templet：`true` 勾上、`false` 与**键缺失**都不勾（E-0021 的边界）', () => {
    const cols = buildCols(new Map())
    const byTitle = new Map(
      cols
        .filter((c) => typeof c.title === 'string')
        .map((c) => [c.title as string, c as { field: string; templet: (d: Record<string, unknown>) => string }]),
    )

    const settle = byTitle.get('禁止结算')!
    expect(settle.field).toBe('settle_forbidden')
    const settleOn = settle.templet({ id: 5, settle_forbidden: true })
    expect(settleOn).toContain('checked')
    expect(settleOn).toContain('lay-filter="settleForbiddenChecked"')
    // `data-id` 是处理器 PATCH 哪一行的唯一凭据。
    expect(settleOn).toContain('data-id="5"')
    // 两条边界：`false`（显式关）与 `{}`（档案行不存在 ⇒ 接口整个不给这个键）**都不许勾**。
    expect(settle.templet({ id: 5, settle_forbidden: false })).not.toContain('checked')
    expect(settle.templet({ id: 5 })).not.toContain('checked')

    const grab = byTitle.get('禁自助接单')!
    expect(grab.field).toBe('grab_forbid_self_service')
    const grabOn = grab.templet({ id: 9, grab_forbid_self_service: true })
    expect(grabOn).toContain('checked')
    expect(grabOn).toContain('lay-filter="grabForbidChecked"')
    expect(grabOn).toContain('data-id="9"')
    expect(grab.templet({ id: 9, grab_forbid_self_service: false })).not.toContain('checked')
    expect(grab.templet({ id: 9 })).not.toContain('checked')
  })

  it('**每一个** `REAL_SWITCHES` 列的 templet 都带该行的 `data-id`（缺了处理器会 PATCH `/accounts/0/`）', () => {
    // 处理器的行号是 `Number(obj.elem?.dataset.id ?? 0)`，`data-id` 一旦缺失就退化成 0
    // ⇒ PATCH `/api/accounts/0/`。今天 `switchCell` 恒带 `data-id`（行数据来自接口，`id` 必有），
    // 所以**加一条断言**把这个前提钉住，而不是在处理器里加运行时兜底。
    // ⚠️ 逐条**从 `REAL_SWITCHES` 反推**、不写死两个标题：将来加第三个真开关而忘了 `data-id` ⇒ 这里红。
    const DUMMY_ROW = { id: 42, games: [], is_active: false }
    const cols = buildCols(new Map())
    const tplByField = new Map(
      cols
        .filter((c) => typeof c.field === 'string' && typeof c.templet === 'function')
        .map((c) => [
          c.field as string,
          c.templet as unknown as (d: Record<string, unknown>) => string,
        ]),
    )
    expect(
      systemUsersConfig.REAL_SWITCHES.length,
      '这条断言的覆盖面**跟着 `REAL_SWITCHES` 走**；长度变了就要确认下面真的逐条跑到了',
    ).toBeGreaterThan(0)
    for (const sw of systemUsersConfig.REAL_SWITCHES) {
      const tpl = tplByField.get(sw.field)
      expect(tpl, `列表里找不到 ${sw.field} 这一列（REAL_SWITCHES 与 cols 已漂移）`).toBeTypeOf(
        'function',
      )
      const html = tpl!({ ...DUMMY_ROW, [sw.field]: true })
      expect(
        html,
        `\`${sw.field}\` 的开关格没带 data-id ⇒ 处理器会去 PATCH \`/api/accounts/0/\``,
      ).toContain('data-id="42"')
    }
  })

  it('「设自助接单数」的 `layer.prompt` 实参：title / formType / maxlength 逐字（`value` 在调用处补）', () => {
    // 出处：对标 `userlist.js:2371`（`setSelfRecOrderCount` 的 `layer.prompt({...})`）。
    // ⚠️ 本条**只钉 title/formType/maxlength**：对标的 `value: 0` 是我方**有意偏离**的一处
    //    （见 `SELF_COUNT_PROMPT_BASE` 的注释），预填值由交互用例按规则钉。
    expect(SELF_COUNT_PROMPT_BASE).toEqual({
      title: '设置用户在内部接单页面接单上限（以用户当前未完成订单数为准），0代表不限制',
      formType: 0,
      maxlength: 4,
    })
  })

  it('两种空/失败文案逐字，且**互不相同**（空结果 ≠ 出错，E-0004）', () => {
    expect(ALIPAY_CHECK_TEXT.none).toBe('没有重复的收款支付宝账号')
    expect(ALIPAY_CHECK_TEXT.forbidden).toBe('你没有查看重复收款支付宝的权限')
    expect(ALIPAY_CHECK_TEXT.none).not.toBe(ALIPAY_CHECK_TEXT.forbidden)
  })

  it('`buildAlipayCheckRows` 把三级数据摊成「一行一个用户」（同组账号/数量重复）', () => {
    // 出参形状见 `@/api/performances` 的 `AlipayDuplicatesResult`：一组重复账户 → 多个用户。
    const rows = buildAlipayCheckRows({
      duplicates: [
        {
          alipay_account: 'ali-888@x.com',
          count: 2,
          users: [
            // ⭐ 昵称与账号**不同形** ⇒ 这一行判得动
            //    「这一格取的是昵称、不是账号」（旧夹具没有 `display_name` ⇒ 那条判据不判别）。
            { username: 'kefu_A', display_name: '测试昵称甲', job: '客服', is_active: true },
            { username: '小舟', job: '代练员', is_active: false },
          ],
        },
        {
          alipay_account: 'ali-999@x.com',
          count: 3,
          users: [{ username: '小满', job: '', is_active: false }],
        },
      ],
    })
    // 3 个用户 ⇒ 3 行（不是 2 行，也不是 1 行）。
    expect(rows).toEqual([
      { str1: '测试昵称甲', str2: 'ali-888@x.com', str3: '客服', id2: 2, str4: 'true' },
      { str1: '小舟', str2: 'ali-888@x.com', str3: '代练员', id2: 2, str4: 'false' },
      { str1: '小满', str2: 'ali-999@x.com', str3: '', id2: 3, str4: 'false' },
    ])
    // ⭐ 判据的另一半：**渲出来的那一格**是昵称、不是账号。
    //    `str1` 列没有 `templet`（`alipayCheckCols()`）⇒ 这一格直接显示 `row.str1`；
    //    两半一起判，才同时钉住「数据层取了昵称」与「渲染层显示的就是它」。
    const str1Col = alipayCheckCols().find((c) => c.field === 'str1')
    expect(str1Col?.title).toBe('子账号名称')
    expect(str1Col?.templet).toBeUndefined()
    expect(rows[0]!.str1).toBe('测试昵称甲')
    expect(rows[0]!.str1).not.toBe('kefu_A')
    // 「当前状态」列的 templet 判的是**字符串** `'true'`（对标 `str4 == 'true'`），所以布尔要转成串。
    expect(rows.map((r) => typeof r.str4)).toEqual(['string', 'string', 'string'])
  })
})

/* ── 接单统计卡片：默认日期窗（纯函数，含月末边界） ───────────────────────────────── */

// 每个用例先清页面和处理器表，否则真交互会红或假绿：
//    ① `document.body.innerHTML = ''` 必须清——`mountPage()` 每调用一次就往 `body` 追加一个根，
//       而开关单元格的 `<div id="switchCell">` 是**同一个 id**；jsdom 对「带 id 的
//       元素作用域选择器」在文档里存在同名 id 时会解析失败 ⇒ 第 2 条用例起拿到 `null`；
//    ② 处理器表挂在 `Map` 上，`vi.clearAllMocks()` 清不掉 ⇒ 上次挂载注册的处理器会残留，
//       「页面注册了某事件」这类断言会靠残留**假通过**（E-0061 的同族）。
beforeEach(() => {
  vi.clearAllMocks()
  LAYUI.formHandlers.clear()
  LAYUI.tableHandlers.clear()
  LAYUI.openCalls.length = 0
  document.body.innerHTML = ''
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('接单统计卡片：默认日期窗（纯函数，含月末边界）', () => {
  it('默认窗 = 今天往前 3 个自然月 ~ 今天（字面值，照对标服务端渲染的 2026-06-20 / 2026-09-20）', () => {
    expect(defaultRecTotalRange(new Date(2026, 8, 20))).toEqual({
      stime: '2026-06-20',
      etime: '2026-09-20',
    })
  })

  it('目标月份没有那一天 ⇒ 取**该月最后一天**（2026-05-31 → 2026-02-28），不是溢出成 3 月', () => {
    expect(defaultRecTotalRange(new Date(2026, 4, 31))).toEqual({
      stime: '2026-02-28',
      etime: '2026-05-31',
    })
  })

  it('没跨界的日号**原样保留**（不因为「安全」一律压到 28）', () => {
    expect(defaultRecTotalRange(new Date(2026, 8, 15)).stime).toBe('2026-06-15')
  })
})

/* ── 接单统计卡片：挂载 + 真交互 ───────────────────────── */

async function mountPage(): Promise<HTMLElement> {
  const root = document.createElement('div')
  document.body.appendChild(root)
  await mountUserListPage(root, { onNavigate: vi.fn() })
  // layui 平时把 `#userToolbarTpl`（`<script type="text/html">`）渲染进表格工具栏；替身不渲染表格
  // ⇒ 这里替它把模板内容放进 DOM，于是「点这颗按钮」走的是**真的** root 委托链，
  //    而不是直接调页面内部函数（后者证明不了「按钮点了有反应」）。
  root.insertAdjacentHTML('beforeend', document.getElementById('userToolbarTpl')?.textContent ?? '')
  return root
}

describe('游戏权限来源：配置面仍列**全量**（Task 5 防死锁）', () => {
  it('挂载取的是 fetchAllGames，⛔ 不是按当前用户 game_ids 收窄的 fetchGames', async () => {
    // 本页的「设置游戏权限」弹窗与主筛选栏**同一个来源**（挂载时那一次取数）⇒ 来源一旦被
    // 收窄，「看不到没勾的游戏 ⇒ 也就勾不上」这条死锁就成立。
    vi.mocked(fetchAllGames).mockClear()
    vi.mocked(fetchGames).mockClear()
    const root = await mountPage()
    expect(root.querySelectorAll('option').length).toBeGreaterThan(0)
    expect(vi.mocked(fetchAllGames)).toHaveBeenCalled()
    expect(vi.mocked(fetchGames)).not.toHaveBeenCalled()
  })
})

/** 真交互的起点：点工具栏那颗「接单统计」。 */
function recTotalButton(root: HTMLElement): HTMLElement {
  const btn = root.querySelector<HTMLElement>('[data-tb="recTotal"]')
  expect(btn, '工具栏里必须有「接单统计」按钮（否则本用例根本没驱动到真交互）').not.toBeNull()
  return btn!
}

/** 卡片那次 `table.render` 的入参（主表也 render 了，按 `elem` 认领）。 */
function cardRenderConfig(): Record<string, unknown> {
  const call = LAYUI.table.render.mock.calls.find(
    (c) => (c[0] as { elem?: string }).elem === '#tableList2',
  )
  expect(call, '卡片表格必须被 table.render 渲染（elem=#tableList2）').toBeTruthy()
  return call![0] as Record<string, unknown>
}

/** 最近一次 `table.reload` 的入参。 */
function lastReloadOptions(): Record<string, unknown> {
  const calls = LAYUI.table.reload.mock.calls
  expect(calls.length, '本用例必须先触发一次重取').toBeGreaterThan(0)
  return calls[calls.length - 1]![1] as Record<string, unknown>
}

/** 某个 `laydate.render` 的配置（按 `elem` 认领）。 */
function laydateConfig(elem: string): Record<string, unknown> {
  const call = LAYUI.laydate.render.mock.calls.find((c) => (c[0] as { elem?: string }).elem === elem)
  expect(call, `laydate 必须绑在 ${elem} 上`).toBeTruthy()
  return call![0] as Record<string, unknown>
}

describe('接单统计卡片：点开 ⇒ 真取数（默认窗 / 分页 / 8 列）', () => {
  beforeEach(() => {
    // 日期默认值必须在**确定的一天**上断言字面值（否则这条用例会随运行日期漂移）。
    vi.useFakeTimers()
    vi.setSystemTime(new Date(2026, 8, 20, 13, 0, 0))
    LAYUI.table.render.mockClear()
    LAYUI.table.reload.mockClear()
    LAYUI.layer.open.mockClear()
    LAYUI.layer.msg.mockClear()
    LAYUI.openCalls.length = 0
  })
  afterEach(() => {
    vi.useRealTimers()
    document.body.innerHTML = ''
  })

  it('点「接单统计」⇒ 弹 `.recTotalDiv`（标题/area 照对标），并**打开即取数**且参数正确', async () => {
    const root = await mountPage()
    // 打开之前**一张内表都不渲染**（对标初始态也只渲染 1 张表 ⇒ 探针的 ths 计数靠这条）。
    expect(LAYUI.table.render).toHaveBeenCalledTimes(1)
    expect(LAYUI.table.render.mock.calls[0]![0]).toMatchObject({ elem: '#tableList_user' })

    recTotalButton(root).click()

    // 1) 弹的是**卡片**，不是「该功能尚未开放」的空卡片。
    expect(LAYUI.layer.open).toHaveBeenCalledTimes(1)
    const openConfig = LAYUI.openCalls[0]!
    expect(openConfig.title).toBe('接单统计')
    expect(openConfig.area).toEqual(['38%', '90%'])
    expect(openConfig.type).toBe(1)
    // 本页的 `$` 返回 `{el, hide}`（见替身注释）：`el` 必须就是页面里那个卡片容器。
    expect((openConfig.content as { el: Element | null }).el).toBe(root.querySelector('.recTotalDiv'))

    // 2) 取数：URL / 分页参数名（本端点是 `limit` 不是 `page_size`）/ 页大小档位 / 默认窗两个日期
    const cfg = cardRenderConfig()
    expect(cfg.url).toBe('/api/business/order-stats')
    expect(cfg.method).toBe('get')
    expect(cfg.request).toEqual({ pageName: 'page', limitName: 'limit' })
    expect(cfg.page).toEqual({ limit: 100, limits: [20, 30, 50, 100, 500, 1000, 2000] })
    expect(cfg.where).toEqual({ stime: '2026-06-20', etime: '2026-09-20' })
    // 3) 工具栏用**本分支**的子表模板 id（不是 main 的 `#recTotalToolbarTpl`），列仍是 8 列。
    expect(cfg.toolbar).toBe('#toolbarDemo2')
    expect((cfg.cols as unknown[][])[0]).toHaveLength(8)
    // 首次打开是 `render`（不是 `reload`）。
    expect(LAYUI.table.reload).not.toHaveBeenCalled()
  })

  it('响应信封 → layui 行：`data.items`→`data`、`data.total`→`count`', async () => {
    const root = await mountPage()
    recTotalButton(root).click()
    const cfg = cardRenderConfig()
    const parsed = (
      cfg.parseData as (r: unknown) => { code: number; count: number; data: OrderStatsRow[] }
    )({
      code: 0,
      msg: '',
      data: {
        total: 3,
        page: 1,
        page_size: 100,
        items: [
          {
            idx: 1,
            uid: 5,
            userName: '打手甲',
            orderCount: 12,
            noSettAmount: 88.5,
            complateRate: 93.98,
            createUserDate: '2026-01-02',
            lastRecOrderDate: '2026-09-18',
          },
          {
            // 行集合**含没接单的人**（后端保证）：`orderCount=0` + `lastRecOrderDate='无'`。
            idx: 2,
            uid: 6,
            userName: '打手乙',
            orderCount: 0,
            noSettAmount: 0,
            complateRate: 0,
            createUserDate: '2026-02-03',
            lastRecOrderDate: '无',
          },
        ],
      },
    })
    expect(parsed.code).toBe(0)
    expect(parsed.count).toBe(3)
    expect(parsed.data).toHaveLength(2)
    // 空数据文案是「暂无数据」——不是「该功能尚未开放」：这张表已经接上数据（E-0004）。
    expect(cfg.text).toEqual({ none: '暂无数据' })
  })

  it('业务失败（HTTP 200 + code 1000）走 parseData 的原样 `msg`，**不静默成空表**', async () => {
    const root = await mountPage()
    recTotalButton(root).click()
    const cfg = cardRenderConfig()
    const parsed = (cfg.parseData as (r: unknown) => Record<string, unknown>)({
      code: 1000,
      msg: 'stime 格式必须是 yyyy-MM-dd',
      data: null,
    })
    expect(parsed).toEqual({ code: 1000, msg: 'stime 格式必须是 yyyy-MM-dd', count: 0, data: [] })
  })

  it('HTTP 层失败把**后端 msg** 露出来（403 节点拒权那句），不是 layui 的英文状态码文案', async () => {
    const root = await mountPage()
    recTotalButton(root).click()
    const card = root.querySelector('.recTotalDiv')!
    // 真 layui 的 `errorView` 会先往表内放一个 `.layui-none`；替身不渲染，这里补一个同形状的落点。
    card.insertAdjacentHTML(
      'beforeend',
      '<div class="layui-table-view"><div class="layui-none"></div></div>',
    )
    const cfg = cardRenderConfig()
    ;(cfg.error as (res: unknown) => void)({
      readyState: 4,
      status: 403,
      statusText: 'Forbidden',
      responseText: '{"code":403,"msg":"你没有查看接单统计的权限","data":null}',
      responseJSON: { code: 403, msg: '你没有查看接单统计的权限', data: null },
    })
    expect(LAYUI.layer.msg).toHaveBeenCalledWith('你没有查看接单统计的权限', { time: 1500 })
    expect(card.querySelector('.layui-none')!.textContent).toBe('你没有查看接单统计的权限')
    // ⛔ **不能**有 `done` 回调去「补写」这句错误话：HTTP 失败不触发 `done`，那种补写只会留到
    //    下一次成功时，把过期的错误话贴到「暂无数据」上（假消息）。写入点只有 `error` 一处。
    expect(cfg.done).toBeUndefined()
  })

  it('改日期 ⇒ 重取 + **回第 1 页**（对标 `laydate.done` 里是重新 render）', async () => {
    const root = await mountPage()
    recTotalButton(root).click()
    LAYUI.table.reload.mockClear()

    ;(root.querySelector('#rsDate') as HTMLInputElement).value = '2026-08-01'
    const done = laydateConfig('#rsDate').done as (() => void) | undefined
    expect(done).toBeTypeOf('function')
    done!()

    expect(LAYUI.table.reload).toHaveBeenCalledWith('tableList2', {
      where: { stime: '2026-08-01', etime: '2026-09-20' },
      page: { curr: 1 },
    })
  })

  it('改游戏 ⇒ 重取但**不回**第 1 页（对标 `reloadOrderList2()` 带当前页 `curr`）', async () => {
    const root = await mountPage()
    recTotalButton(root).click()
    LAYUI.table.reload.mockClear()

    ;(root.querySelector('#recTotalGameName') as HTMLSelectElement).value = '原神'
    LAYUI.formHandlers.get('select(selectGameTotal)')!({})

    expect(LAYUI.table.reload).toHaveBeenCalledWith('tableList2', {
      where: { stime: '2026-06-20', etime: '2026-09-20', gameName: '原神' },
    })
    // 刻意**没有** `page` 键。
    expect(lastReloadOptions()).not.toHaveProperty('page')
  })

  it('游戏选回「全部」⇒ **不送** `gameName`（空串是「没选」不是非法值）', async () => {
    const root = await mountPage()
    recTotalButton(root).click()
    LAYUI.table.reload.mockClear()

    ;(root.querySelector('#recTotalGameName') as HTMLSelectElement).value = ''
    LAYUI.formHandlers.get('select(selectGameTotal)')!({})

    expect(lastReloadOptions().where).toEqual({ stime: '2026-06-20', etime: '2026-09-20' })
  })

  it('日期输入框挂载时就被填上默认窗（不是靠服务端 `value`）', async () => {
    const root = await mountPage()
    expect((root.querySelector('#rsDate') as HTMLInputElement).value).toBe('2026-06-20')
    expect((root.querySelector('#reDate') as HTMLInputElement).value).toBe('2026-09-20')
    // 两个日期框都绑上了 laydate（少一个就是「改了日期不生效」）。
    expect(laydateConfig('#rsDate').type).toBe('date')
    expect(laydateConfig('#reDate').type).toBe('date')
  })

  it('卡片工具栏「刷新列表」重取（**留在当前页**）；「禁用子账号 / 删除用户」仍提示尚未开放', async () => {
    const root = await mountPage()
    recTotalButton(root).click()
    LAYUI.table.reload.mockClear()

    const card = root.querySelector('.recTotalDiv')!
    card.insertAdjacentHTML(
      'beforeend',
      '<button data-sub="refresh2">刷新列表</button><button data-sub="disBtn">禁用子账号</button><button data-sub="delUserBtn2">删除用户</button>',
    )
    card.querySelector<HTMLElement>('[data-sub="refresh2"]')!.click()
    expect(LAYUI.table.reload).toHaveBeenCalledWith('tableList2', {
      where: { stime: '2026-06-20', etime: '2026-09-20' },
    })
    expect(lastReloadOptions()).not.toHaveProperty('page')

    LAYUI.table.reload.mockClear()
    LAYUI.layer.msg.mockClear()
    card.querySelector<HTMLElement>('[data-sub="disBtn"]')!.click()
    expect(LAYUI.table.reload).not.toHaveBeenCalled()
    expect(LAYUI.layer.msg).toHaveBeenCalledWith('「禁用子账号」尚未开放', { time: 1500 })
  })
})

/* ── 「有效状态」开关：两个方向都要发对（合并 main 的 561b1e0 / E-0071） ───────────── */

/** 「禁自助接单」开关的 `lay-filter`：**我方事件名**（裁定：不用对标的 `isProhibitPublicRecOrder`）。 */
const GRAB_SWITCH_FILTER = 'grabForbidChecked'

/** 等 `updateAccount(...).then(...)` 的回调跑完（真实计时器，一拍即可）。 */
const flush = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0))

function parseSwitch(html: string): HTMLInputElement {
  const doc = new DOMParser().parseFromString(html, 'text/html')
  const input = doc.querySelector('input[type=checkbox]')
  expect(input, 'templet 必须渲染出一个 checkbox').not.toBeNull()
  return input as unknown as HTMLInputElement
}

/** 挂载整页 + 把**任意一列**在某一行的开关单元格塞进 DOM（layui 的表格渲染在测试里是替身）。 */
async function mountSwitchCell(
  field: string,
  row: Record<string, unknown>,
): Promise<HTMLInputElement> {
  const root = await mountPage()
  const col = buildCols(new Map()).find((c) => c.field === field)
  expect(col, `主表必须有 field=${field} 的列（否则开关根本没接上）`).toBeTruthy()
  const html = (col!.templet as unknown as (d: Record<string, unknown>) => string)(row)
  root.insertAdjacentHTML('beforeend', `<div id="switchCell">${html}</div>`)
  const input = root.querySelector<HTMLInputElement>('#switchCell input[type=checkbox]')
  expect(input, '单元格必须真的进了 DOM（否则本用例没驱动到真交互）').not.toBeNull()
  return input!
}

/**
 * 拨一下开关：复刻 layui 的真实顺序——**先把 input 切到新状态、再派发事件**。
 * `value` 照 layui 源码传 `elem.value`（`switchCell` 不写 `value` 属性 ⇒ 恒为 `"on"`，
 * **不代表开关状态**）——于是「用 `obj.value` 判断开/关」这种写法会在「关」方向露馅。
 */
function flipRegisteredSwitch(
  input: HTMLInputElement,
  expectFilter: string = GRAB_SWITCH_FILTER,
): void {
  const filter = input.getAttribute('lay-filter')
  expect(filter, '开关标记必须带 lay-filter').toBe(expectFilter)
  const handler = LAYUI.formHandlers.get(`switch(${filter})`)
  expect(handler, `页面必须注册 switch(${filter})（否则开关点了没反应）`).toBeTypeOf('function')
  input.checked = !input.checked
  handler!({ elem: input, value: input.value, othis: input })
}

describe('人员管理：「有效状态」开关（两个方向都要发对）', () => {
  beforeEach(() => {
    vi.mocked(setAccountStatus).mockClear()
    LAYUI.layer.msg.mockClear()
  })
  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('「正常→禁用」发 `setAccountStatus(id, false)`、「禁用→正常」发 `setAccountStatus(id, true)`', async () => {
    await mountPage()
    const handler = LAYUI.formHandlers.get('switch(staChecked)')
    expect(handler, '`switch(staChecked)` 处理器必须已注册').toBeTruthy()

    // 方向 1：拨到「禁用」（`checked === false`）。
    // ⚠️ 这一条是本用例存在的理由：旧判据 `obj.value === 'on' || obj.value === 'true'` 在这里
    // **恒发 `true`**（layui 派发的 `value` 是 input 的 value 属性，我们没写 value ⇒ 恒为 "on"），
    // 所以「禁用」方向会红、而「启用」方向照样绿——单向用例抓不到这个 bug（E-0071）。
    const off = document.createElement('input')
    off.dataset.id = '7'
    off.checked = false
    handler!({ elem: off, value: 'on' })
    expect(setAccountStatus).toHaveBeenCalledWith(7, false)

    vi.mocked(setAccountStatus).mockClear()
    // 方向 2：拨回「正常」（`checked === true`）。
    const on = document.createElement('input')
    on.dataset.id = '8'
    on.checked = true
    handler!({ elem: on, value: 'on' })
    expect(setAccountStatus).toHaveBeenCalledWith(8, true)
  })
})

/* ── Task 8：「删除用户」= 软删（可恢复）的 UI ─────────────────────────────────── */

describe('人员管理：软删（可恢复）的 UI（Task 8）', () => {
  /** 取「操作」列那条 `templet`（`Col` 是 `Record<string, unknown>`，这里收窄）。 */
  function operationTemplet(): (d: Record<string, unknown>) => string {
    const col = buildCols(new Map()).find((c) => c.title === '操作')
    expect(col, '主表必须有「操作」列').toBeTruthy()
    return col!.templet as unknown as (d: Record<string, unknown>) => string
  }

  it('操作列：`deleted_at` 有值 ⇒ 只给「恢复」，**不渲染**编辑 / 重置', () => {
    const html = operationTemplet()({ id: 7, deleted_at: '2026-01-02T03:04:05Z' })
    expect(html).toContain('data-act="restore"')
    expect(html).toContain('恢复')
    expect(html).not.toContain('data-act="edit"')
    expect(html).not.toContain('data-act="reset"')
  })

  it('操作列：没删的（`deleted_at` 为 `null`）⇒ 仍是编辑 + 重置，**没有**恢复', () => {
    const html = operationTemplet()({ id: 7, deleted_at: null })
    expect(html).toContain('data-act="edit"')
    expect(html).toContain('data-act="reset"')
    expect(html).not.toContain('data-act="restore"')
  })

  it('两次确认的文案**逐字**（含 `<br/>` 与 `第二次确认:` 后面那两个空格）', () => {
    // ⚠️ 期望值**写字面值**，不要引 `DELETE_CONFIRM_TEXTS` 自己（那会变成自指断言 E-0024）。
    // ⚠️ 这两句照抄对标，而**实际行为是软删、可恢复**——文案与实现有意不一致，
    //    登记在 `DELETE_CONFIRM_TEXTS` 的注释与 task-8-report.md 里。
    expect(DELETE_CONFIRM_TEXTS.first).toBe(
      '确认要删除吗？ <br/> <br/> 警告：一旦删除，将彻底无法恢复，强烈建议不要删除，只需要将此用户的有效状态改为禁用即可，请三思再操作！ <br/>',
    )
    expect(DELETE_CONFIRM_TEXTS.second).toBe(
      '第二次确认:  <br/> <br/> 是不是真的要删除？删除就永远无法恢复了 <br/>',
    )
  })

  it('4 个「未开放」开关列已按业务方裁定删除（`ns_*` 一个都不剩，也不再有 `notOpenChecked` 处理器）', () => {
    const notOpenCols = buildCols(new Map())
      .filter((c) => typeof c.field === 'string' && c.field.startsWith('ns_'))
      .map((c) => c.title)
    // 裁定（D-0096）：4 个「后端没有 ⇒ 点了提示尚未开放」的开关列**整列删除** ⇒ 一份确切空数组。
    // ⛔ 用 `toEqual([])`（不是 `toContain` / `toBeFalsy`）：多回一列也必须红。
    expect(notOpenCols).toEqual([])
    // 「禁自助接单」「禁止结算」都**不是**「未开放」列（它们现在是真开关）。
    expect(notOpenCols).not.toContain('禁自助接单')
    expect(notOpenCols).not.toContain('禁止结算')
    // 未开放的开关处理器也一并删除：`ns_*` 列既然没了，`switch(notOpenChecked)` 就是死代码。
    // ⚠️ 同一条判据另有一条同级规格守着（`system-user-list.interaction.spec.ts`）。
    expect(LAYUI.formHandlers.has('switch(notOpenChecked)')).toBe(false)
  })

  it('「有效状态」筛到「已删除」⇒ 发出的是 `deleted=true`（**不是** `is_active`）', () => {
    expect(buildUserListWhere({ status: 'deleted' })).toEqual({ deleted: true })
    // 旧的语义**没被抢走**：true / false 仍然落到 `is_active`。
    expect(buildUserListWhere({ status: 'true' })).toEqual({ is_active: 'true' })
    expect(buildUserListWhere({ status: 'false' })).toEqual({ is_active: 'false' })
    // 「全部」两个都不发。
    expect(buildUserListWhere({ status: '' })).toEqual({})
  })

  it('「有效状态」下拉的第 4 项是「已删除」（`value` 是字符串 `deleted`，不是布尔）', () => {
    expect(
      systemUsersConfig.USER_STATUS_FILTER_OPTIONS.map((o) => [o.label, o.value]),
    ).toEqual([
      ['全部', ''],
      ['有效', 'true'],
      ['禁用', 'false'],
      ['已删除', 'deleted'],
    ])
  })

  it('工具栏「删除用户」的 `action` 是 `delete`（不再是 `notOpen`），且配置与页标记**同改**', () => {
    const fromConfig = USER_TOOLBAR.find((b) => b.label === '删除用户')
    expect(fromConfig!.action).toBe('delete')
    const fromMarkup = TOOLBAR_BUTTONS_FOR_TEST.find((b) => b.label === '删除用户')
    expect(fromMarkup!.action).toBe('delete')
  })
})

/* ══════════════════════════════════════════════════════════════════════════════════════
 * 人员管理：「禁止结算」真开关（后端 `StaffProfile.settle_forbidden`）
 *
 * 我方 `field` 用后端列名 `settle_forbidden`（照「禁自助接单」的先例：对标的 field 名照抄会取不到值）。
 *
 * ⛔ 两处**有意偏离对标**（业务方裁定）：
 *  ① 列头 `title` 是**纯标签** `'禁止结算'`——**不带**对标的 `<span lay-tips="…">` 外层结构
 *     （该属性在我们 vendored 的 layui 里是死属性，`grep -c lay-tips` = 0，视觉完全一样）；
 *  ② `lay-filter` 用**我方事件名** `settleForbiddenChecked`（不是对标的 `disSettStatus`）
 *     ——它只是列标记与处理器之间的内部约定。
 * 列宽 40 与对标一致。
 * ══════════════════════════════════════════════════════════════════════════════════════ */

/** 「禁止结算」开关的 `lay-filter`：**我方事件名**（与 `config/systemUsers.ts` 的 `REAL_SWITCHES` 同值）。 */
const SETTLE_SWITCH_FILTER = 'settleForbiddenChecked'

/** 主表里那一列（找不到就是「开关根本没接上」⇒ 本组用例必须红）。 */
function settleSwitchColumn(): Record<string, unknown> {
  const col = buildCols(new Map()).find((c) => c.field === 'settle_forbidden')
  expect(col, '主表必须有 field=settle_forbidden 的列').toBeTruthy()
  return col!
}

/** 用**生产 templet** 渲染这一行的开关单元格。 */
function settleSwitchHtml(row: { id: number; settle_forbidden?: boolean }): string {
  const tpl = settleSwitchColumn().templet as unknown as (d: Record<string, unknown>) => string
  return tpl(row)
}

async function mountSettleSwitch(row: { id: number; settle_forbidden?: boolean }): Promise<HTMLInputElement> {
  return mountSwitchCell('settle_forbidden', row)
}

describe('人员管理：「禁止结算」列（照对标 `userlist.js:763` 的 `#switchTpl8`）', () => {
  it('列定义：field/width/align；title 是**纯标签**（裁定：不带对标 `<span lay-tips>` 外层结构）', () => {
    const col = settleSwitchColumn()
    expect(col.field).toBe('settle_forbidden')
    // 对标 `userlist.js:763` 的 `width: 40`（全表最窄的开关列）。
    expect(col.width).toBe(40)
    expect(col.align).toBe('center')
    // ⛔ 用 `toBe`（不是 `toContain`）：文案被改坏、或被塞回 `<span lay-tips="…">` 外层结构时都要红。
    // 裁定：列头是**纯标签**（带 span 的那版违反裁定，见本组文件头注释）。
    expect(col.title).toBe('禁止结算')

    const input = parseSwitch(settleSwitchHtml({ id: 42, settle_forbidden: false }))
    expect(input.getAttribute('lay-skin')).toBe('switch')
    expect(input.getAttribute('lay-text')).toBe('是|否')
    expect(input.getAttribute('lay-filter')).toBe(SETTLE_SWITCH_FILTER)
    expect(input.dataset.id).toBe('42')
  })

  it('开关初值取**行数据**（true ⇒ checked；false / 缺档案 ⇒ 不 checked）', () => {
    const on = parseSwitch(settleSwitchHtml({ id: 7, settle_forbidden: true }))
    expect(on.hasAttribute('checked')).toBe(true)
    const off = parseSwitch(settleSwitchHtml({ id: 7, settle_forbidden: false }))
    expect(off.hasAttribute('checked')).toBe(false)
    // `auth.User` 没有档案行时该键**整个不出现**（`payloads.py::_profile_fields`）⇒ 按「否」渲染。
    const missing = parseSwitch(settleSwitchHtml({ id: 7 }))
    expect(missing.hasAttribute('checked')).toBe(false)
  })

  it('① 拨动开关 ⇒ 发 `updateAccount(<id>, {settle_forbidden})`，两个方向布尔值都对', async () => {
    vi.mocked(updateAccount).mockResolvedValue({} as AccountRow)
    const input = await mountSettleSwitch({ id: 42, settle_forbidden: false })

    // 关 → 开
    flipRegisteredSwitch(input, SETTLE_SWITCH_FILTER)
    expect(updateAccount).toHaveBeenLastCalledWith(42, { settle_forbidden: true })

    // 开 → 关（判据若误用 `obj.value`，这里会错成 `true`）
    flipRegisteredSwitch(input, SETTLE_SWITCH_FILTER)
    expect(updateAccount).toHaveBeenLastCalledWith(42, { settle_forbidden: false })
    await flush()
  })

  it('② 成功后开关保持用户拨到的新值（不回滚）+ 提示「已保存」', async () => {
    vi.mocked(updateAccount).mockResolvedValue({} as AccountRow)
    const input = await mountSettleSwitch({ id: 7, settle_forbidden: false })

    flipRegisteredSwitch(input, SETTLE_SWITCH_FILTER)
    expect(input.checked).toBe(true)
    await flush()
    // 成功路径不许把它拨回去。
    expect(input.checked).toBe(true)
    expect(LAYUI.layer.msg).toHaveBeenCalledWith('已保存', { time: 1500 })
  })

  it('③ 失败 ⇒ 露出后端 msg + **重取该行**（本线口径：靠 `reload()` 从服务端取回原值，不是本地写回 `checked`）', async () => {
    vi.mocked(updateAccount).mockRejectedValue(new BizError(1000, '该字段不能通过本接口修改'))
    const input = await mountSettleSwitch({ id: 7, settle_forbidden: false })
    LAYUI.table.reload.mockClear()

    flipRegisteredSwitch(input, SETTLE_SWITCH_FILTER)
    expect(input.checked).toBe(true) // 用户确实拨过去了
    expect(updateAccount).toHaveBeenLastCalledWith(7, { settle_forbidden: true })
    await flush()
    // 失败 ⇒ ① 露出后端 msg；② 重取该行（本页所有写路径的统一收尾，行的原值由服务端回来）。
    // ⛔ 判据打在**真实出口** `table.reload` 上，不是「看 `input.checked` 有没有被本地改回去」：
    //    生产代码的 `switch(...)` 处理器**不做本地回滚**，失败与成功都走
    //    `msg(…) + reload()`；行回到原值是 `reload()` 重新取数的结果。
    expect(LAYUI.layer.msg).toHaveBeenCalledWith('该字段不能通过本接口修改', { time: 1500 })
    expect(LAYUI.table.reload).toHaveBeenCalledWith('tableList_user', { page: { curr: 1 } })
  })

  it('④ 4 个「未开放」开关列已按业务方裁定删除（`ns_*` 一个都不剩），「禁止结算」是真开关', () => {
    const notOpenCols = buildCols(new Map())
      .filter((c) => typeof c.field === 'string' && c.field.startsWith('ns_'))
      .map((c) => c.title)
    // 裁定（D-0096）：那 4 列（登录免验证 / 隐藏订单电话 / 禁看密码 / 禁看内容区）已**整列删除**
    // ⇒ 一点残余都不许有（⛔ `toEqual([])`，不是 `toContain` / `toBeFalsy`）。
    expect(notOpenCols).toEqual([])
    // 「禁止结算」必须**不再**是那个「点了提示未开放」的列。
    expect(notOpenCols).not.toContain('禁止结算')
    // 它必须是一列真开关（`field` 取后端列名），且挂在页面上。
    expect(settleSwitchColumn().field).toBe('settle_forbidden')
  })
})
