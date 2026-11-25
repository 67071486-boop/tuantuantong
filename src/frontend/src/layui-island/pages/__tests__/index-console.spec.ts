// @vitest-environment jsdom
import fs from 'node:fs'
import path from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { User } from '@/api/auth'
import type { ConfigRow } from '@/api/config'
import type { Notification } from '@/api/notifications'
import type { LayuiModule } from '@/layui/loader'

/**
 * 【后台首页】island 页（`pages/index-console.ts`）的**接线 + 纯函数**测试。
 *
 * 覆盖两层，都打在**真实出口**上：
 *  1. 纯函数：`renderTemplate` / `pruneExcluded` / `markPlaceholders` / `infoCell` /
 *     `formatRoles` / `formatGameScope` / `noticeText` / `noticeLines`；
 *  2. 接线：装载即取三发真实接口、回填「我的信息」与通知列表、点通知开层、
 *     快捷操作导航、占位控件只提示、失败只回落、卸载无残留。
 *
 * 断言里的字面值不引常量（避免 E-0024 的自指断言）。
 *
 * Mock 层次**只桩 `@/api/http`**（照 `user-mymessage.spec.ts`）：`@/api/auth` / `@/api/config` /
 * `@/api/notifications` 用真的——它们的 `request(...)` 形状才是本页要钉的「真实调用形状」。
 */
const h = vi.hoisted(() => ({
  msgs: [] as string[],
  opened: [] as Array<Record<string, unknown>>,
  //: `laydate.render` 收到的配置（数据概览那个日期框是真区间选择器）。
  laydates: [] as Array<Record<string, unknown>>,
  //: 首页二维码横幅（D-0262）的删除确认层（`layer.confirm` 的实参与 yes 回调）。
  confirms: [] as Array<{ text: string; options: unknown; yes: (index: number) => void }>,
}))

vi.mock('@/api/http', () => ({ request: vi.fn() }))

vi.mock('@/layui/island', () => ({
  bootstrapIsland: async () => ({
    form: { render: () => undefined },
    // 页面要多加载一个 `laydate`（`bootstrapIsland(['form','layer','laydate'])`）：
    // 替身把 `render` 收到的配置记下来，用例据此断言 `range:'~'` / 默认值 / `done` 的行为。
    laydate: {
      render: (options: Record<string, unknown>) => {
        h.laydates.push(options)
      },
    },
    layer: {
      msg: (text: string) => {
        h.msgs.push(text)
      },
      open: (options: Record<string, unknown>) => {
        h.opened.push(options)
        // 真实 layui 的 `layer.open({type:1, content:'<div>…'})` 把内容插进
        // `document.body` 下的 `.layui-layer-content`（**root 之外**）——
        // 读码 `public/layui/layui.js` 的 layer 模块；本替身照这个位置放，别图省事塞进 root。
        const holder = document.createElement('div')
        holder.className = 'layui-layer-content'
        holder.innerHTML = String(options.content ?? '')
        document.body.appendChild(holder)
      },
      // 横幅的「删除」走 `layer.confirm`（真实签名 `(text, options, yes)`；页面拿到 index 后
      // 会调 `layer.close(index)` ⇒ 替身给一个 no-op close，别让它炸在收尾上）。
      confirm: (text: string, options: unknown, yes: (index: number) => void) => {
        h.confirms.push({ text, options, yes })
      },
      close: () => undefined,
    },
    laytpl: (template: string) => ({ render: () => template }),
  }),
  initIslandElements: () => undefined,
  islandMsg: (layer: { msg: (t: string) => void }, text: string) => {
    layer.msg(text)
  },
}))

import { request } from '@/api/http'
import { HOME_QR_CODES_URL, type HomeQrRow } from '@/api/homeQrCodes'
import { HOME_OVERVIEW_URL, type HomeOverview } from '@/api/home'
import indexConsole, {
  ARTIFACT_URL,
  MORE_LABEL,
  NOTICE_PAGE_SIZE,
  OVERVIEW_ACTIVE_CLASS,
  OVERVIEW_DATE_INPUT_ID,
  OVERVIEW_LOCKED_ATTR,
  OVERVIEW_LOCKED_REASON,
  QR_BANNER_SELECTOR,
  QR_BANNER_TEXT,
  QR_CARD_SELECTOR,
  QR_CAROUSEL_INTERVAL_MS,
  QR_DOT_ON_CLASS,
  QR_DOT_SELECTOR,
  QR_EMPTY_SELECTOR,
  QR_EMPTY_TEXT,
  QR_FIGURE_SELECTOR,
  QR_GAME_NAME_SELECTOR,
  QR_MANAGE_NODE,
  QR_ZOOM_SELECTOR,
  ROOT_ID,
  SHORTCUTS,
  SHORTCUT_ROUTES,
  TEMPLATE_ID,
  fillOverview,
  formatGameScope,
  formatRoles,
  infoCell,
  isoDate,
  markPlaceholders,
  noticeLines,
  noticeText,
  overviewWindow,
  parseRangeValue,
  pruneExcluded,
  qrBannerHtml,
  qrBuckets,
  rangeValue,
  renderShortcuts,
  renderTemplate,
  shortcutHtml,
  visibleShortcuts,
} from '../index-console'

const mockedRequest = vi.mocked(request)

const REPLICA = path.resolve(__dirname, '../../../../public/replica/index-console.html')
const artifact = fs.readFileSync(REPLICA, 'utf8')

/** 与 `order-addorder.ts` 同构的 `laytpl` 替身：把模板原文当渲染结果（证据件里已无 `{{ }}`）。 */
const fakeLaytpl = ((template: string) => ({ render: () => template })) as unknown as LayuiModule

/** 量到的真实调用（`@/api/http` 的 `request` 配置）。 */
type Call = { method?: string; url?: string; params?: unknown; data?: unknown }
const calls: Call[] = []
const navs: Array<{ name: string; params?: Record<string, string> }> = []

const ME: User = {
  id: 7,
  username: 'kefu01',
  display_name: '客服01',
  identity_plane: 'tenant',
  is_tenant_admin: false,
  roles: ['系统管理员'],
  permissions: [],
  // 通知卡片标题与「重要提示…」那一行的租户名从 `me.tenant_name` 取。
  // ⚠️ 夹具用**合成值**，且**刻意取一个从没在代码里出现过的名字**（`示例工作室乙`）：
  // 与任何（含已删除的）占位常量逐字节相同的值，分辨不出
  // 「读 `me.tenant_name`」与「把旧字符串硬编码回去」（E-0024 族：判据要能失败）。
  tenant_name: '示例工作室乙',
  // 夹具给 `menu_permissions` 字段（新的权限门正是读它）。
  // 这四个节点 ⇒ 可见集恰好 = 原来那 4 格（录入/管理订单/算账/用户管理）+ 无门的排行榜，
  // 于是既有用例（#17 等）的可见格数不变。「什么都没有 ⇒ 只剩排行榜」由 S3 专门钉住。
  menu_permissions: ['order.add_edit', 'order.list', 'finance.payroll', 'system_user.list'],
  game_ids: [1, 2, 3],
}

/** 「打手」型身份（S1）：只持四个 `my_*` 节点，非管理员 ⇒ 与对标打手那份逐格一致。 */
const TAKER_ME: User = {
  ...ME,
  menu_permissions: ['my_orders', 'my_performance', 'grab_hall', 'my_profile'],
}

/** 管理员型身份（S2/S8/S9/S10）：靠 `is_tenant_admin` 短路拿到全部 10 格（S3 明确选定的代价）。 */
const ADMIN_ME: User = { ...ME, is_tenant_admin: true, menu_permissions: [] }

/**
 * 「二维码横幅」的管理员身份（D-0262）：判据是 `canNode(me, 'home.qr_manage')`
 * （非管理员的**节点门**走 `menu_permissions`，与管理员短路是同一份 `canNode`）。
 */
const QR_MANAGER_ME: User = { ...ME, menu_permissions: [...(ME.menu_permissions ?? []), QR_MANAGE_NODE] }

/**
 * 横幅行的合成夹具（D-0262）。每行 id / URL 互不相同，断言拿错行立刻红。
 * ⚠️ `media_url` 是服务端**现签**的短时效链接，这里用**从没在代码里出现过的**合成域名
 * （真值守卫只扫 `src/**`，合成值照 E-0077 的口径）。
 */
function qrRow(id: number, kind: HomeQrRow['kind'], overrides: Partial<HomeQrRow> = {}): HomeQrRow {
  return {
    id,
    kind,
    game_id: kind === 'qq_group' ? 100 + id : null,
    game_name: kind === 'qq_group' ? `横幅游戏${id}` : null,
    media_key: `demo/users/7/home-qr.qr${id}.jpg`,
    media_url: `https://media.example.com/signed/qr-${id}.jpg`,
    sort_order: id,
    is_enabled: true,
    created_by: 'hr01',
    created_at: '2026-10-19T10:00:00+08:00',
    updated_at: '2026-10-19T10:00:00+08:00',
    ...overrides,
  }
}

/**
 * 「数据概览」的管理员身份：判据是 `/api/auth/me` 的 **`is_account_admin`**
 * （⛔ 不是 `is_tenant_admin`——页面照后端那条判据走，见 `index-console.ts` 的 `overviewVisible`）。
 */
const OVERVIEW_ADMIN_ME: User = { ...ME, is_account_admin: true }

/**
 * 「数据概览」的桩响应。**每格取互不相同的值**：任何一格填错位置都会红
 * （`pan5` 必须是 35.2、不是 28.6；`pan10` 是 `2(28.6%)` 一个字符串）。
 */
const OVERVIEW: HomeOverview = {
  start: '2026-09-20',
  end: '2026-09-20',
  add_count: 7,
  order_price: '1234.50',
  hall_price_total: '800.00',
  profit_amount: '434.50',
  profit_rate: '35.2',
  rec_order_count: 5,
  rec_order_rate: '71.4',
  cancel_count: 0,
  cancel_order_rate: '0.0',
  refund_count: 2,
  refund_order_rate: '28.6',
  finish_count: 3,
  finish_order_rate: '42.9',
}

const GAMES: ConfigRow[] = [
  { id: 1, name: '游戏A', sort_order: 1, is_active: true, created_at: '2026-09-01T00:00:00+08:00' },
  { id: 2, name: '游戏B', sort_order: 2, is_active: true, created_at: '2026-09-01T00:00:00+08:00' },
  { id: 3, name: '游戏C', sort_order: 3, is_active: true, created_at: '2026-09-01T00:00:00+08:00' },
]

function notification(id: number, kind: string, fromStatus: string | null = '进行中'): Notification {
  const orderNo = `1234567890123456${String(id).padStart(4, '0')}`
  return {
    id,
    kind,
    order_no: orderNo,
    source_order_no: null,
    payload: fromStatus === null ? { order_no: orderNo } : { order_no: orderNo, from_status: fromStatus },
    read_at: null,
    created_at: '2026-09-21T08:00:00+08:00',
  }
}

const NOTICES: Notification[] = [
  notification(1, '被指派'),
  notification(2, '被抢走'),
  notification(3, '待审核'),
]

/**
 * 三发请求各自的真实响应形状（`Page<T>` / `{user}`）。`me` 可换（权限门的用例要不同身份）。
 *
 * `overview` 只在本用例**预期**页面会取「数据概览」时才传（传了才会应答那条 URL）；
 * 不传时该 URL 与从前一样**响亮地抛**——「非管理员一发都不发」那条用例正靠这一点：
 * 桩备好了却不应答，说明它压根没被调用。
 */
function respondWith(
  notices: Notification[] = NOTICES,
  me: User = ME,
  overview?: HomeOverview,
  systemNotices: Array<{
    id: number
    title: string
    content: string
    created_by: string
    created_at: string
    updated_at: string
  }> = [],
  qrCodes: HomeQrRow[] = [],
): void {
  mockedRequest.mockImplementation((async (config: Call) => {
    calls.push(config)
    if (config.url === '/api/auth/me') return { user: me }
    if (config.url === '/api/business/games/') {
      return { items: GAMES, total: GAMES.length, page: 1, page_size: 200 }
    }
    if (config.url === '/api/business/notifications/') {
      return { items: notices, total: notices.length, page: 1, page_size: NOTICE_PAGE_SIZE }
    }
    // 首页有「系统内部公告」那一发（与站内通知合并展示）。
    // 默认空集 ⇒ 既有那些「只数通知条数」的用例口径不变。
    if (config.url === '/api/business/system-notices/') {
      return {
        items: systemNotices,
        total: systemNotices.length,
        page: 1,
        page_size: NOTICE_PAGE_SIZE,
      }
    }
    //（D-0262）横幅那一发。默认空集 ⇒ 三格都是占位、既有用例口径不变。
    if (config.url === HOME_QR_CODES_URL) {
      return { items: qrCodes, total: qrCodes.length }
    }
    // 详情 URL（PATCH / DELETE 的 `<id>/`）：写动作回一行即可——下面的判据钉的是
    // 「发了什么请求」，不依赖写动作的返回体。
    if (config.url?.startsWith(HOME_QR_CODES_URL)) {
      return qrCodes[0] ?? null
    }
    if (config.url === HOME_OVERVIEW_URL && overview) return overview
    throw new Error(`本用例没预期这个请求：${String(config.url)}`)
  }) as never)
}

/** 把证据件塞进一个游离 div 并**跑一次 `renderTemplate`**（= 运行期真实形态）。 */
function rendered(): HTMLElement {
  const div = document.createElement('div')
  div.innerHTML = artifact
  expect(renderTemplate(div, fakeLaytpl)).toBe(true)
  return div
}

let mounted: (() => void) | null = null

async function mountPage(): Promise<{ root: HTMLElement; unmount: () => void }> {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: true, status: 200, text: async () => artifact })),
  )
  const root = document.createElement('div')
  document.body.appendChild(root)
  const unmount = await indexConsole.mount(root, {
    onNavigate: (name, params) => {
      navs.push({ name, params })
    },
  })
  mounted = unmount
  return { root, unmount }
}

afterEach(() => {
  mounted?.()
  mounted = null
})

beforeEach(() => {
  calls.length = 0
  navs.length = 0
  h.msgs.length = 0
  h.opened.length = 0
  h.laydates.length = 0
  h.confirms.length = 0
  document.body.innerHTML = ''
  document.title = ''
  mockedRequest.mockReset()
  respondWith()
})

describe('导出的常量与接口形状', () => {
  it('证据件 URL / 两个 DOM id / 分页大小 / 占位文案都是计划里的字面值', () => {
    expect(ARTIFACT_URL).toBe('replica/index-console.html')
    expect(TEMPLATE_ID).toBe('template')
    expect(ROOT_ID).toBe('root')
    expect(NOTICE_PAGE_SIZE).toBe(3)
    expect(MORE_LABEL).toBe('尚未开放')
    // 这个端点与它那个日期框（字面量出处 = `apps/business/urls.py`）。
    expect(HOME_OVERVIEW_URL).toBe('/api/business/home/overview/')
    expect(OVERVIEW_DATE_INPUT_ID).toBe('ID-laydate-start-date-1')
    expect(OVERVIEW_ACTIVE_CLASS).toBe('layui-border-blue')
  })

  it('快捷操作映射表由条目表派生，恰好 9 条（跳过无页面的排行榜那格），逐条字面量', () => {
    expect(SHORTCUT_ROUTES).toEqual([
      { href: '/Order/myorderlist', routeName: 'orders-mine' },
      { href: '/Emp/myemp', routeName: 'agents-my-income' },
      { href: '/Order/insiderecorderlist', routeName: 'orders-inside-hall' },
      { href: '/User/myinfo', routeName: 'accounts-profile' },
      { href: '/User/mymessage', routeName: 'accounts-messages' },
      { href: '/Order/addorder', routeName: 'order-new' },
      { href: '/Order/orderlist', routeName: 'orders' },
      { href: '/Emp/emplist', routeName: 'agents-performance' },
      { href: '/User/userlist', routeName: 'system-users' },
    ])
    // 第 10 格「接单排行榜」没有这一页 ⇒ 不进映射表（它在条目表里带 `placeholder`）。
    expect(SHORTCUTS).toHaveLength(10)
    expect(SHORTCUTS.filter((s) => s.placeholder)).toHaveLength(1)
  })
})

describe('renderTemplate：把 #template 的内容克隆进 #root', () => {
  it('#1 证据件里整页标记躺在 <template> 里，克隆后 .layadmin-shortcut 出现在 #root 内', () => {
    const div = document.createElement('div')
    div.innerHTML = artifact
    // 克隆之前：`<template>` 的内容是**另一个 DocumentFragment**，`querySelector` 够不到。
    expect(div.querySelector('.layadmin-shortcut')).toBeNull()

    expect(renderTemplate(div, fakeLaytpl)).toBe(true)
    expect(div.querySelector('#root')!.querySelector('.layadmin-shortcut')).not.toBeNull()
    // 克隆用的是 `innerHTML` 替换 ⇒ `<template>` 自己也被换掉了（不再有第二个模板）。
    expect(div.querySelector('#template')).toBeNull()
  })

  it('#2 缺 #template 时返回 false 且不抛（空 div）', () => {
    const empty = document.createElement('div')
    expect(() => renderTemplate(empty, fakeLaytpl)).not.toThrow()
    expect(renderTemplate(empty, fakeLaytpl)).toBe(false)
  })
})

describe('pruneExcluded：A2 / A4 的 5 处裁剪', () => {
  it('#3 已渲染的标记上删掉 5 个节点（算价器 li / #Operation 所在卡 / 3 个二维码盒）', () => {
    const root = rendered()
    const removed = pruneExcluded(root)

    expect(removed).toHaveLength(5)
    expect(root.querySelector('a[href*="calculator.html"]')).toBeNull()
    expect(root.querySelector('#Operation')).toBeNull()
    expect(root.querySelector('#qrcodeBox')).toBeNull()
    expect(root.querySelector('#jidanpuqrcodeBox')).toBeNull()
    expect(root.querySelector('#jidanpuappqrcodeBox')).toBeNull()
    // A4：那张卡的标题文案整条不再出现在页面文本里（card 整块删掉，不只是删图表 div）。
    expect(root.textContent).not.toContain('今日订单分时数据概览')
    // 数据概览 / 发单概览两张卡**必须留着**（A5 是占位，不是裁剪）。
    expect(root.querySelector('.pan1')).not.toBeNull()
    expect(root.querySelector('.pushorderdatadiv')).not.toBeNull()
  })

  it('#3b mount 的真实路径：**未渲染**的证据件 body 上也删得掉（否则 3 张跨站图会被真的取）', () => {
    // ⚠️ 这不是重复用例：`mount` 在惰性文档里裁的是**原始证据件**，那时整页标记还在
    // `<template>` 的 content 里，裸 `querySelector` 一个都找不到（本机 jsdom 探针实测）。
    const body = document.createElement('div')
    body.innerHTML = artifact
    expect(pruneExcluded(body)).toHaveLength(5)
    const tpl = body.querySelector('template') as HTMLTemplateElement
    expect(tpl.content.querySelector('#Operation')).toBeNull()
    expect(tpl.content.querySelector('#qrcodeBox')).toBeNull()
    expect(tpl.content.querySelector('#jidanpuqrcodeBox')).toBeNull()
    expect(tpl.content.querySelector('#jidanpuappqrcodeBox')).toBeNull()
    expect(tpl.content.querySelector('a[href*="calculator.html"]')).toBeNull()
    // 3 个 `oss4.jidanpu.com` 的 <img src> 必须连标记都不剩（R5 / R8）。
    expect(tpl.innerHTML).not.toContain('oss4.jidanpu.com/images')
  })

  it('#4 保留 layui-hide 的「魔兽排团」li（A3 是「不渲染」，不是「删掉」）', () => {
    const root = rendered()
    pruneExcluded(root)
    const link = root.querySelector('a[lay-href="/Group/grouplist2020"]')
    expect(link).not.toBeNull()
    expect(link!.closest('li')!.classList.contains('layui-hide')).toBe(true)
    // A2 只删算价器那一格，另外 4 格快捷操作一个不少。
    expect(root.querySelectorAll('.layadmin-shortcut li')).toHaveLength(6)
  })
})

describe('markPlaceholders：12 个占位控件', () => {
  it('#5 逐条打上 data-notopen（7 单点 + 4 时间按钮 + 1 日期输入），返回 12，且幂等', () => {
    const root = rendered()
    expect(markPlaceholders(root)).toBe(12)

    const label = (el: Element | null): string | null => el?.getAttribute('data-notopen') ?? null

    // ① 快捷操作「接单排行榜」（证据件 L138）
    expect(label(root.querySelector('a.rankList'))).toBe('接单排行榜')
    // ② 我的信息：「实名认证」那颗的**打标落在祖先 span.layui-btn 上**（计划「占位清单」第 2 条）
    expect(label(root.querySelector('a[lay-text="实名认证"]')!.closest('span.layui-btn'))).toBe('开始进行实名认证')
    // ② 「点击这里可以给系统续费」
    expect(label(root.querySelector('a[href*="/Pay/SystemRenewal"]'))).toBe('点击这里可以给系统续费')
    // ③ 声明 / 购买同款系统 / 微信小程序 / 系统帮助文档
    // 声明那一颗是**例外**：它的可见文字是一整句（≈50 字），托底 `placeholderLabel` 会把它整句
    // 塞进提示 ⇒ 该条目显式给 `label: '郑重声明'`（控制器裁定）。
    expect(label(root.querySelector('a[lay-tips="点击可以查看声明"]'))).toBe('郑重声明')
    expect(label(root.querySelector('a.contactDev'))).toBe('购买该同款系统点我')
    expect(label(root.querySelector('a.aboutjdpwxapp'))).toBe('点这里使用微信小程序')
    expect(label(root.querySelector('a[lay-href="/Helper/index"]'))).toBe('系统帮助文档')

    // ④ **发单概览**那 4 颗时间按钮（带 `2` 后缀）。⚠️ 数据概览那 4 颗（裸 `today` 等）
    //    已接真后端 ⇒ **不在**占位清单里（下面单独钉这一条）。
    const timeButtons = Array.from(
      root.querySelectorAll('.yesterday2, .today2, .last7days2, .last30days2'),
    )
    expect(timeButtons).toHaveLength(4)
    expect(timeButtons.map(label)).toEqual(['昨天', '今天', '最近7天', '最近30天'])
    // ⚠️ 数据概览那 4 颗**一颗都不许**带 `data-notopen`（它们是真控件；它们一旦被重新打进
    //    占位清单，S10 那两条总数计数会红——这里直接按选择器钉住是哪 4 颗）。
    for (const kind of ['yesterday', 'today', 'last7days', 'last30days']) {
      const button = root.querySelector(`.rep-title-rigth a.${kind}`)!
      expect(button.getAttribute('data-notopen')).toBeNull()
    }

    // ⑤ **发单概览**那一个日期输入：没有可见文字 ⇒ 回落到「该功能」
    const dates = Array.from(root.querySelectorAll('input[id^="ID-laydate-start-date"]'))
    expect(dates).toHaveLength(2)
    expect(label(root.querySelector('#ID-laydate-start-date-2'))).toBe('该功能')
    // ⚠️ 数据概览那个（`-1`）已改成真 laydate 区间选择器 ⇒ 不在占位清单里。
    expect(root.querySelector('#ID-laydate-start-date-1')!.getAttribute('data-notopen')).toBeNull()

    // 幂等：再跑一次不再计数（已有 data-notopen 的不重复打）
    expect(markPlaceholders(root)).toBe(0)

    // 「绑定鸡蛋铺账号」那颗是 `display:none`（对标就不显示）⇒ 计划明写**不处理**。
    expect(root.querySelector('a[lay-href="/User/myinfo#userInfo=1"]')!.closest('[data-notopen]')).toBeNull()
  })

  it('#6 只剩发单概览那个日期输入被打上 readonly，且证据件的 value 默认值原样保留（E-0074 / 约束 15）', () => {
    const root = rendered()
    markPlaceholders(root)
    // ⑤ 发单概览那个：照旧 `readonly` + 证据件 value 一个字符不动。
    const pushDate = root.querySelector<HTMLInputElement>('#ID-laydate-start-date-2')!
    expect(pushDate.hasAttribute('readonly')).toBe(true)
    expect(pushDate.value).toBe('2026-09-19 ~ 2026-09-19')
    // ⛔ 数据概览那个**不再** `readonly`（它是真控件，`readonly` 会让它看起来能用其实点不开——
    //    正是 R4 当初要避免的假象的反面：现在它真的能用了）。它的默认值由 `mount` 写（= 今天，
    //    见 `overviewWindow` 与装载用例），**不是** `markPlaceholders` 的职责。
    const homeDate = root.querySelector<HTMLInputElement>('#ID-laydate-start-date-1')!
    expect(homeDate.hasAttribute('readonly')).toBe(false)
  })
})

describe('infoCell：按 span.tit 定位同一行的第二个 td', () => {
  it('#7 「游戏权限」落在第 4 行的第 2 个 td 上；找不到的 label 返回 null', () => {
    const root = rendered()
    const rows = root.querySelectorAll('.layui-table-info tbody tr')
    expect(rows).toHaveLength(8)

    const cell = infoCell(root, '游戏权限')
    expect(cell).toBe(rows[3]!.children[1])
    expect(cell!.tagName).toBe('TD')
    expect(infoCell(root, '不存在')).toBeNull()
    // 契约（计划 Step 3）：只 trim **标记侧**的 `textContent`，`label` 原样比较——
    // 所以带空格的 label 取不到（调用方一律传证据件原文）。
    expect(infoCell(root, '  游戏权限  ')).toBeNull()
  })
})

describe('纯函数：身份与权限文案', () => {
  it('#8 formatRoles：空 → 「—」、一条 → 原文、多条 → 「、」连接', () => {
    expect(formatRoles({ roles: [] })).toBe('—')
    expect(formatRoles({})).toBe('—')
    expect(formatRoles({ roles: ['系统管理员'] })).toBe('系统管理员')
    expect(formatRoles({ roles: ['A', 'B'] })).toBe('A、B')
    expect(formatRoles({ roles: [''] })).toBe('—')
  })

  it('#9 formatGameScope：全量加「全部游戏」前缀；顺序取全量接口的顺序；空 → 「—」', () => {
    expect(formatGameScope(GAMES, [1, 2, 3])).toBe('全部游戏,游戏A,游戏B,游戏C')
    expect(formatGameScope(GAMES, [2])).toBe('游戏B')
    expect(formatGameScope(GAMES, [])).toBe('—')
    // 顺序**不是** ids 的顺序，而是全量列表的顺序（[3,1] → 游戏A,游戏C）
    expect(formatGameScope(GAMES, [3, 1])).toBe('游戏A,游戏C')
    // 全量接口为空 ⇒ 「—」（不是「全部游戏」）
    expect(formatGameScope([], [1])).toBe('—')
  })
})

describe('纯函数：通知文案', () => {
  it('#10 noticeText：有单号带单号，order_no 为 null 只留【kind】', () => {
    expect(noticeText({ kind: '被指派', order_no: '123' })).toBe('【被指派】订单 123')
    expect(noticeText({ kind: '被指派', order_no: null })).toBe('【被指派】')
    // ⭐ D-0301：来源号非空 ⇒ 展示来源号；空 ⇒ 回退内置号。
    expect(
      noticeText({ kind: '被指派', order_no: '123', source_order_no: 'SRC-TEST-1' }),
    ).toBe('【被指派】订单 SRC-TEST-1')
    expect(
      noticeText({ kind: '被指派', order_no: '123', source_order_no: '' }),
    ).toBe('【被指派】订单 123')
  })

  it('#11 noticeLines：4 行、from_status 缺失为「—」、值里的尖括号被转义', () => {
    const lines = noticeLines({
      kind: '被指派',
      order_no: '<123>',
      payload: { from_status: '<进行中>' },
      created_at: '2026-09-21T08:00:00+08:00',
    })
    expect(lines.split('<br/>')).toHaveLength(4)
    expect(lines).toContain('类型：被指派')
    expect(lines).toContain('订单号：&lt;123&gt;')
    expect(lines).toContain('状态变化：&lt;进行中&gt;')
    expect(lines).toContain('通知时间：2026-09-21T08:00:00+08:00')
    expect(lines).not.toContain('<123>')

    const noFrom = noticeLines({
      kind: '被指派',
      order_no: null,
      payload: {},
      created_at: '2026-09-21T08:00:00+08:00',
    })
    expect(noFrom).toContain('订单号：—')
    expect(noFrom).toContain('状态变化：—')
  })
})

describe('快捷操作：条目表 + 权限门（S1–S7，纯函数）', () => {
  /** 把一格标记解析成 DOM（断言「渲染出来的东西」，不是「调用了某函数」——Global Constraint 8）。 */
  function cell(html: string): HTMLLIElement {
    const box = document.createElement('ul')
    box.innerHTML = html
    return box.querySelector('li')!
  }

  it('S1 打手型 me ⇒ 恰好 6 格，顺序逐条 = 我的订单/我的收入/内部接单/我的资料/我的信息/接单排行榜', () => {
    expect(visibleShortcuts(TAKER_ME).map((s) => s.cite)).toEqual([
      '我的订单',
      '我的收入',
      '内部接单',
      '我的资料',
      '我的信息',
      '接单排行榜',
    ])
  })

  it('S2 管理员（is_tenant_admin）⇒ 恰好 10 格，顺序 = 条目表顺序', () => {
    const visible = visibleShortcuts(ADMIN_ME)
    expect(visible).toHaveLength(10)
    expect(visible.map((s) => s.cite)).toEqual([
      '我的订单',
      '我的收入',
      '内部接单',
      '我的资料',
      '我的信息',
      '录入订单',
      '管理系统内所有订单',
      '给打手或客服算账',
      '管理系统子账户',
      '接单排行榜',
    ])
  })

  it('S3 什么都没有（{}）⇒ 恰好只剩没有门的排行榜', () => {
    expect(visibleShortcuts({}).map((s) => s.cite)).toEqual(['接单排行榜'])
  })

  it('S4 第 1 格「我的订单」的标记逐项照抄条目表（href/tips/lay-text/icon/color/cite/列宽类）', () => {
    const html = shortcutHtml(SHORTCUTS[0]!)
    expect(html).toContain('lay-href="/Order/myorderlist"')
    expect(html).toContain('lay-tips="在这里看指派给我的订单"')
    expect(html).toContain('lay-text="我的订单"')
    expect(html).toContain('class="layui-icon layui-icon-list"')
    expect(html).toContain('style="color:#FFB800"')
    expect(html).toContain('<cite>我的订单</cite>')
    expect(html).toContain('class="layui-col-xs6 layui-col-sm4 layui-col-md2 layui-col-lg2"')
  })

  it('S5 第 7 格：lay-text「管理订单」与 <cite>「管理系统内所有订单」是两个值，都要在', () => {
    const html = shortcutHtml(SHORTCUTS[6]!)
    expect(html).toContain('lay-text="管理订单"')
    expect(html).toContain('<cite>管理系统内所有订单</cite>')
  })

  it('S6 第 9 格：lay-text「用户管理」与 <cite>「管理系统子账户」是两个值，都要在', () => {
    const html = shortcutHtml(SHORTCUTS[8]!)
    expect(html).toContain('lay-text="用户管理"')
    expect(html).toContain('<cite>管理系统子账户</cite>')
  })

  it('S7 第 10 格「接单排行榜」：裸 href + 裸文本，没有 lay-href / <cite> / lay-tips', () => {
    const html = shortcutHtml(SHORTCUTS[9]!)
    expect(html).toContain('href="javascript:void(0)" class="rankList"')
    expect(html).toContain('layui-icon-align-left')
    expect(html).toContain('color:#FF5722')
    expect(html).toContain('接单排行榜')
    expect(html).not.toContain('lay-href')
    expect(html).not.toContain('<cite>')
    expect(html).not.toContain('lay-tips')
    // 「裸文本」的判据：解析后锚点里没有 <cite> 子元素，文字就是它自己的文本节点。
    const a = cell(html).querySelector('a')!
    expect(a.querySelector('cite')).toBeNull()
    expect(a.textContent?.trim()).toBe('接单排行榜')
  })

  it('S4b 十格的 lay-tips / 字形 / 颜色逐格照抄「关键取值表 → 快捷操作条目表」（表驱动，字面量）', () => {
    // 期望值**逐字**取自计划 `docs/plans/2026-09-24-home-shortcuts-permission.md`
    // 的「关键取值表 → 快捷操作条目表」那一行（列：`lay-tips` / `<i>` 字形 / 内联色）；
    // 第 10 格那一行的三格分别是「（对标没有 tips）」/ `align-left` / `#FF5722`。
    // 写字面量、**不引 `SHORTCUTS[i].tips/icon/color`**（E-0024：自指断言在条目表被改坏时不会红）。
    const table: Array<{ tips: string | null; icon: string; color: string }> = [
      { tips: '在这里看指派给我的订单', icon: 'list', color: '#FFB800' },
      { tips: '在这里可以看我的订单收入', icon: 'rmb', color: '#CB5DEF' },
      { tips: '在这里可以抢接内部的订单', icon: 'template', color: '#73D08D' },
      { tips: '我的资料', icon: 'friends', color: '#23d4d4' },
      { tips: '我的信息', icon: 'speaker', color: '#73D08D' },
      { tips: '将订单录入系统', icon: 'add-1', color: '#F86738' },
      { tips: '管理所有订单', icon: 'form', color: '#23d4d4' },
      { tips: '管理系统所有的代练打手的结算情况', icon: 'rmb', color: '#CB5DEF' },
      { tips: '管理系统内所有的系统子账户', icon: 'group', color: '#FFB800' },
      { tips: null, icon: 'align-left', color: '#FF5722' },
    ]
    expect(SHORTCUTS).toHaveLength(table.length)
    // 十格的字形与颜色（一格里两处插值，逐格断言）。
    table.forEach((row, i) => {
      const html = shortcutHtml(SHORTCUTS[i]!)
      expect(html).toContain(`class="layui-icon layui-icon-${row.icon}"`)
      expect(html).toContain(`style="color:${row.color}"`)
    })
    // 前九格各有 `lay-tips`；第 10 格是对标里唯一**没有** `lay-tips` 的一格（计划该行原文）。
    table.slice(0, 9).forEach((row, i) => {
      expect(shortcutHtml(SHORTCUTS[i]!)).toContain(`lay-tips="${row.tips!}"`)
    })
    expect(shortcutHtml(SHORTCUTS[9]!)).not.toContain('lay-tips')
  })

  it('renderShortcuts 写入 row.innerHTML，并返回渲染条数（null 行 ⇒ 0）', () => {
    const row = document.createElement('ul')
    expect(renderShortcuts(row, TAKER_ME)).toBe(6)
    expect(row.querySelectorAll('li')).toHaveLength(6)
    expect(renderShortcuts(null, TAKER_ME)).toBe(0)
  })
})

describe('挂载：装载即取真数 + 真动作', () => {
  it('#12 装载即并行发五发请求（me / games / notifications / system-notices / qr-codes），并设 document.title', async () => {
    await mountPage()
    expect(calls.map((c) => c.url)).toEqual([
      '/api/auth/me',
      '/api/business/games/',
      '/api/business/notifications/',
      '/api/business/system-notices/',
      HOME_QR_CODES_URL,
    ])
    expect(calls.map((c) => c.method?.toLowerCase())).toEqual(['get', 'get', 'get', 'get', 'get'])
    expect(calls[2]!.params).toEqual({ page: 1, page_size: 3 })
    expect(calls[3]!.params).toEqual({ page: 1, page_size: 3 })
    // 横幅列表零参数（按持点分档是服务端 queryset 的事，前端不发明筛选）。
    expect(calls[4]!.params).toBeUndefined()
    expect(document.title).toBe('后台首页')
  })

  it('#13 回填「我的信息」三格，且只换格子里的那一处文本（不抹掉包装元素）', async () => {
    const { root } = await mountPage()
    const account = infoCell(root, '本系统登录账号')!
    expect(account.querySelector('span.info.ft14')!.textContent).toBe('kefu01')
    // 那颗格子里还有两颗按钮：必须留着（写 td.textContent 会把它们一起抹掉）
    expect(account.querySelectorAll('span.layui-btn')).toHaveLength(2)

    const job = infoCell(root, '我的职位')!
    expect(job.textContent?.trim()).toBe('系统管理员')
    // 值包在 `<span style="margin-top:10px">` 里（证据件 L182）⇒ 连包装与行内样式一起留着
    expect(job.querySelector(':scope > span')!.getAttribute('style')).toBe('margin-top:10px')

    expect(infoCell(root, '游戏权限')!.textContent?.trim()).toBe('全部游戏,游戏A,游戏B,游戏C')
    // A7：「我接手的订单」是占位，证据件原值不动
    expect(infoCell(root, '我接手的订单')!.textContent).toBe('0笔 / 0.00元')
  })

  it('#14 通知和公告上半 = 3 条真实通知（对标标记同形），下半「系统更新公告」仍空', async () => {
    const { root } = await mountPage()
    const card = root.querySelector('.systemNoticeUl')!.closest('.layui-card')!
    const header = card.querySelector(':scope > .layui-card-header')!
    expect(header.textContent).toBe('【示例工作室乙】通知和公告')
    expect(header.textContent).not.toContain('【—】')

    const links = root.querySelectorAll('.systemNoticeUl a.viewNotice')
    expect(links).toHaveLength(3)
    expect(links[0]!.getAttribute('data-id')).toBe('n:1')
    expect(links[0]!.getAttribute('title')).toBe('【被指派】订单 12345678901234560001')
    expect(links[0]!.textContent).toBe('【被指派】订单 12345678901234560001...')
    expect(Array.from(links).map((a) => a.textContent)).toEqual([
      '【被指派】订单 12345678901234560001...',
      '【被抢走】订单 12345678901234560002...',
      '【待审核】订单 12345678901234560003...',
    ])
    // A6 下半：占位空 <ul>
    expect(root.querySelector('.jdpNews')!.children).toHaveLength(0)
  })

  it('#14s 系统内部公告排在站内通知前面，点开显示公告全文（不是那 4 行通知格式）', async () => {
    respondWith([notification(1, '被指派')], ME, undefined, [
      {
        id: 8,
        title: '今晚维护',
        content: '今晚 23:00 维护\n预计一小时',
        created_by: 'admin',
        created_at: '2026-10-17T10:20:30+08:00',
        updated_at: '2026-10-17T10:20:30+08:00',
      },
    ])
    const { root } = await mountPage()
    const links = root.querySelectorAll('.systemNoticeUl a.viewNotice')
    expect(links).toHaveLength(2)
    expect(links[0]!.textContent).toBe('【公告】今晚维护...')
    expect(links[0]!.getAttribute('data-id')).toBe('s:8')
    links[0]!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    const content = document.querySelector('.layui-layer-content')!
    expect(content.textContent).toContain('今晚 23:00 维护')
    expect(content.innerHTML).toContain('<br>')
    // 系统公告没有订单，弹层里不该出现「查看订单」
    expect(content.querySelector('a.viewOrder')).toBeNull()
  })

  it('#14b 短标题超过 50 字截断到 50，title 属性与正文同源', async () => {
    respondWith([notification(9, 'K'.repeat(80))])
    const { root } = await mountPage()
    const link = root.querySelector('.systemNoticeUl a.viewNotice')!
    const title = link.getAttribute('title')!
    expect(title).toHaveLength(50)
    expect(link.textContent).toBe(`${title}...`)
    expect(title.startsWith('【KKK')).toBe(true)
  })

  it('#14c 「重要提示…」那一行的租户名也被填上（F1：证据件里它是 PII 占位符 `—`）', async () => {
    const { root } = await mountPage()
    const declaration = root.querySelector('a[lay-tips="点击可以查看声明"]')!
    // 对标由服务端渲染**同一个**租户名（`Index__console.html:254` 的 `Tit1 + 公司名 + Tit2`）。
    expect(declaration.textContent).toBe(
      '重要提示：本系统为示例工作室乙内部管理系统，与软件开发商鸡蛋铺无关，鸡蛋铺不对该系统内任何业务负责',
    )
    expect(declaration.textContent).not.toContain('—')
    // 用 `textContent` 填（Global Constraint 4）⇒ 锚点仍是纯文本，不引入子元素。
    expect(declaration.children).toHaveLength(0)
  })

  it('#14d 通知条目的两个 sink 都转义（kind/order_no 含 `<b>&"`）', async () => {
    const nasty = '<b>&"'
    respondWith([
      {
        id: 42,
        kind: nasty,
        order_no: nasty,
        source_order_no: nasty,
        payload: { order_no: nasty, from_status: nasty },
        read_at: null,
        created_at: '2026-09-21T08:00:00+08:00',
      },
    ])
    const { root } = await mountPage()

    // sink ①：列表项（`noticeItemHtml` 用 `innerHTML` 拼）。
    // ⚠️ `getAttribute` / `textContent` 读的是 DOM **解码后**的值（`&lt;` → `<`），所以「转义了」
    // 的证据只能打在（a）**序列化**的 `innerHTML`，与（b）「payload 有没有被当成标记解析」上；
    // 不能拿 `&lt;b&gt;&amp;&quot;` 去比 `getAttribute`——re-serialize 时属性值里的 `"` 写作 `&quot;`、
    // 文本节点里的 `"` 不转义，两边本来就不逐字相同。
    const ul = root.querySelector('.systemNoticeUl')!
    const link = ul.querySelector('a.viewNotice')!
    expect(link.getAttribute('data-id')).toBe('n:42')
    expect(ul.innerHTML).toContain('&lt;b&gt;')
    // 属性值仍是**完整**的 payload（缺 `escapeHtml` 时会被 payload 里的 `"` 截断成 `【<b>&`）
    expect(link.getAttribute('title')).toBe('【<b>&"】订单 <b>&"')
    // 那串字符以**文本**呈现，没有变成元素（删掉 `escapeHtml` 就会红）
    expect(link.textContent).toBe('【<b>&"】订单 <b>&"...')
    expect(link.querySelector('*')).toBeNull()
    expect(ul.querySelector('b')).toBeNull()
    // ⚠️ 不能断言 `innerHTML` 不含 `<b>`：属性值在 re-serialize 时 `&lt;` 会被解回 `<`，
    // 于是 `title="【<b>…"` 里本来就有这个子串（它落在引号内、不是元素）。判据是上面的元素检查。

    // sink ②：`layer.open` 的 `title`（会进 layui 的 HTML 串，见 `index-console.ts` 的注释）
    link.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(h.opened).toHaveLength(1)
    expect(String(h.opened[0]!.title)).toBe(
      '内部公告：【&lt;b&gt;&amp;&quot;】订单 &lt;b&gt;&amp;&quot;...',
    )
    expect(String(h.opened[0]!.title)).not.toContain('<b>')
  })

  it('#15 点通知条目 ⇒ 开对标形状的层，且**不标记已读**（R1：装载后零写请求）', async () => {
    const { root } = await mountPage()
    calls.length = 0
    root.querySelector('.systemNoticeUl a.viewNotice')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))

    expect(h.opened).toHaveLength(1)
    expect(h.opened[0]).toMatchObject({ type: 1, skin: 'layui-layer-rim', area: ['50%', '60%'] })
    expect(String(h.opened[0]!.title)).toBe('内部公告：【被指派】订单 12345678901234560001...')
    const content = String(h.opened[0]!.content)
    expect(content).toContain('类型：被指派')
    expect(content).toContain('订单号：12345678901234560001')
    expect(content).toContain('状态变化：进行中')
    expect(content).toContain('查看订单')
    // R1：首页通知条目点击**不**标记已读 ⇒ 一个写请求都不发
    expect(calls.filter((c) => c.method?.toLowerCase() === 'post')).toHaveLength(0)
  })

  it('#16 点弹层里的「查看订单」⇒ ctx.onNavigate(\'order-detail\', { order_no })', async () => {
    const { root } = await mountPage()
    root.querySelector('.systemNoticeUl a.viewNotice')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    // 弹层在 document.body 下（root 之外）——这才是真实位置
    const orderLink = document.querySelector('.layui-layer-content a.viewOrder')
    expect(orderLink).not.toBeNull()
    orderLink!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(navs).toEqual([{ name: 'order-detail', params: { order_no: '12345678901234560001' } }])
  })

  it('#17 四格快捷操作各自导航到我们的 route name（且不提示「尚未开放」）', async () => {
    const { root } = await mountPage()
    const table: Array<[string, string]> = [
      ['/Order/addorder', 'order-new'],
      ['/Order/orderlist', 'orders'],
      ['/Emp/emplist', 'agents-performance'],
      ['/User/userlist', 'system-users'],
    ]
    for (const [href, routeName] of table) {
      const link = root.querySelector(`.layadmin-shortcut a[lay-href="${href}"]`)
      expect(link).not.toBeNull()
      link!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      expect(navs[navs.length - 1]).toEqual({ name: routeName, params: undefined })
    }
    expect(navs.map((n) => n.name)).toEqual(['order-new', 'orders', 'agents-performance', 'system-users'])
    expect(h.msgs).toEqual([])
  })

  it('#18 「接单排行榜」/ **发单概览的**按钮与日期输入点了只说「尚未开放」，且不发导航', async () => {
    const { root } = await mountPage()
    root.querySelector('a.rankList')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(h.msgs[h.msgs.length - 1]).toBe('「接单排行榜」尚未开放')

    // ⚠️ **数据概览**那 4 颗与它那个日期框是真控件（不进占位清单）⇒
    //    本用例改用**发单概览**那一组（那张卡不在本组范围内）。
    root.querySelector('.yesterday2')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(h.msgs[h.msgs.length - 1]).toBe('「昨天」尚未开放')

    root.querySelector('#ID-laydate-start-date-2')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(h.msgs[h.msgs.length - 1]).toBe('「该功能」尚未开放')

    // 声明那一颗走的是 `PlaceholderTarget.label` 覆盖（R19 / F1b），不是整句可见文字
    root
      .querySelector('a[lay-tips="点击可以查看声明"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(h.msgs[h.msgs.length - 1]).toBe('「郑重声明」尚未开放')

    expect(navs).toEqual([])
    expect(h.opened).toEqual([])
  })

  it('F2 点证据件里 `layui-hide` 的「实名认证」锚点（同样带 lay-href="/User/myinfo"）不导航', async () => {
    const { root } = await mountPage()
    // 证据件 L173：它在「我的信息」格一个 `layui-hide` 的 span 里，**不在**快捷操作那一排。
    const hidden = root.querySelector('a[lay-text="实名认证"]')!
    expect(hidden.closest('.layadmin-shortcut')).toBeNull()
    // 它带 `lay-href="/User/myinfo"` —— 与快捷操作第 4 格同 href；委托的快捷分支若不按容器收窄，
    // 第 4 格一进映射表就会把它一并导航走（改动前 `/User/myinfo` 不在表里 ⇒ 不导航）。
    expect(hidden.getAttribute('lay-href')).toBe('/User/myinfo')
    hidden.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(navs).toEqual([])
    // ⚠️ `data-notopen` 打在它的祖先 `span.layui-btn` 上（`PLACEHOLDER_TARGETS` 第 2 条的 `wrap`，
    // #5 已钉住这一点），所以收窄后它**落到占位分支**、提示「尚未开放」——与改动前逐字一致。
    expect(h.msgs).toEqual(['「开始进行实名认证」尚未开放'])
  })

  it('#19 三发请求全失败：mount 照样 resolve、页面骨架仍在、失败项回落成占位值', async () => {
    mockedRequest.mockImplementation((async (config: Call) => {
      calls.push(config)
      throw new Error('后端挂了')
    }) as never)
    // ⚠️ 「mount resolve」由这一行承担：它若 reject，本用例直接红。
    const { root, unmount } = await mountPage()
    expect(typeof unmount).toBe('function')

    // 登录账号那一格还挂着两颗按钮 ⇒ 判据落在 `span.info.ft14` 上（整格 textContent 会带上按钮文字）
    expect(infoCell(root, '本系统登录账号')!.querySelector('span.info.ft14')!.textContent).toBe('—')
    expect(infoCell(root, '我的职位')!.textContent?.trim()).toBe('—')
    expect(infoCell(root, '游戏权限')!.textContent?.trim()).toBe('—')
    expect(root.querySelector('.systemNoticeUl')!.children).toHaveLength(0)
    // 横幅那一发也失败 ⇒ 整块按「全空」回落：三格占位，页面骨架不受影响（D-0262）。
    expect(root.querySelectorAll(QR_EMPTY_SELECTOR)).toHaveLength(3)
    // 卡片与快捷操作都还在（不是整页空白）
    expect(root.querySelector('.layadmin-shortcut')).not.toBeNull()
    expect(root.querySelector('.layui-table-info')).not.toBeNull()
    expect(root.querySelectorAll('.reportPanTitle')).toHaveLength(14)
  })

  it('#20 unmount 之后 root 清空，旧节点与弹层里的旧链接都不再有副作用', async () => {
    const { root, unmount } = await mountPage()
    root.querySelector('.systemNoticeUl a.viewNotice')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    const popupLink = document.querySelector('.layui-layer-content a.viewOrder')!
    const oldNotice = root.querySelector('.systemNoticeUl a.viewNotice')!

    unmount()
    expect(root.innerHTML).toBe('')

    h.opened.length = 0
    // 旧节点塞回已卸载的 root：root 上的捕获委托必须已经摘掉
    root.appendChild(oldNotice)
    oldNotice.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    // 弹层里的旧链接仍在 document.body 里：document 级的 viewOrder 委托也必须已经摘掉
    popupLink.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(h.opened).toHaveLength(0)
    expect(navs).toEqual([])
  })

  it('S8 装载后（管理员）快捷操作行恰好 10 格：DOM 顺序 = 条目表顺序，首个「我的订单」、末个「接单排行榜」', async () => {
    respondWith(NOTICES, ADMIN_ME)
    const { root } = await mountPage()
    const items = root.querySelectorAll('.layadmin-shortcut li')
    expect(items).toHaveLength(10)
    expect(items[0]!.querySelector('cite')!.textContent).toBe('我的订单')
    // 第 10 格没有 <cite>（裸文本）⇒ 取锚点文本，不取 cite。
    const last = items[9]!.querySelector('a')!
    expect(last.classList.contains('rankList')).toBe(true)
    expect(last.textContent?.trim()).toBe('接单排行榜')
    // DOM 顺序逐格 = 条目表顺序（不是「调用了某函数」）。
    expect(
      Array.from(items).map((li) => {
        const a = li.querySelector('a')!
        return a.querySelector('cite')?.textContent ?? a.textContent?.trim()
      }),
    ).toEqual([
      '我的订单',
      '我的收入',
      '内部接单',
      '我的资料',
      '我的信息',
      '录入订单',
      '管理系统内所有订单',
      '给打手或客服算账',
      '管理系统子账户',
      '接单排行榜',
    ])
  })

  it('S8b 装载后（打手）DOM 顺序 = 打手可见的 6 格，逐格与对标打手那份一致', async () => {
    respondWith(NOTICES, TAKER_ME)
    const { root } = await mountPage()
    const items = Array.from(root.querySelectorAll('.layadmin-shortcut li'))
    expect(
      items.map((li) => {
        const a = li.querySelector('a')!
        return a.querySelector('cite')?.textContent ?? a.textContent?.trim()
      }),
    ).toEqual(['我的订单', '我的收入', '内部接单', '我的资料', '我的信息', '接单排行榜'])
  })

  it('S9 装载后 9 格各自导航到对应 route name，第 10 格只提示「尚未开放」', async () => {
    respondWith(NOTICES, ADMIN_ME)
    const { root } = await mountPage()
    const table: Array<[string, string]> = [
      ['/Order/myorderlist', 'orders-mine'],
      ['/Emp/myemp', 'agents-my-income'],
      ['/Order/insiderecorderlist', 'orders-inside-hall'],
      ['/User/myinfo', 'accounts-profile'],
      ['/User/mymessage', 'accounts-messages'],
      ['/Order/addorder', 'order-new'],
      ['/Order/orderlist', 'orders'],
      ['/Emp/emplist', 'agents-performance'],
      ['/User/userlist', 'system-users'],
    ]
    for (const [href, routeName] of table) {
      const link = root.querySelector(`.layadmin-shortcut a[lay-href="${href}"]`)
      expect(link).not.toBeNull()
      link!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      expect(navs[navs.length - 1]).toEqual({ name: routeName, params: undefined })
    }
    expect(navs.map((n) => n.name)).toEqual(table.map(([, routeName]) => routeName))

    root.querySelector('a.rankList')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(h.msgs).toEqual(['「接单排行榜」尚未开放'])
    // 第 10 格不导航（它没有 route name）。
    expect(navs).toHaveLength(9)
  })

  it('S10 装载后 [data-notopen] 恰好 12，且**新渲染的** .rankList 带 data-notopen="接单排行榜"', async () => {
    respondWith(NOTICES, ADMIN_ME)
    const { root } = await mountPage()
    expect(root.querySelectorAll('[data-notopen]')).toHaveLength(12)
    // 行被清空后重渲染 ⇒ 页面上只剩这一个 .rankList（若打标被重渲染冲掉，这里会是 null）。
    expect(root.querySelectorAll('.rankList')).toHaveLength(1)
    expect(root.querySelector('a.rankList')!.getAttribute('data-notopen')).toBe('接单排行榜')
  })

  it('S11 无权限的格完全不渲染（不是置灰），行里没有 display:none 的残留', async () => {
    respondWith(NOTICES, { ...ME, menu_permissions: ['my_orders'] })
    const { root } = await mountPage()
    // 只有「我的订单」+ 无门的排行榜。
    expect(root.querySelectorAll('.layadmin-shortcut li')).toHaveLength(2)
    expect(root.querySelector('a[lay-href="/Order/myorderlist"]')).not.toBeNull()
    expect(root.querySelector('a[lay-href="/Order/addorder"]')).toBeNull()
    expect(root.querySelector('a[lay-href="/Order/orderlist"]')).toBeNull()
    expect(root.querySelector('a[lay-href="/User/userlist"]')).toBeNull()
    // 不是「藏起来」：行里既没有 layui-hide（证据件里那格魔兽排团已被整行清掉），
    // 也没有内联的 display:none 残留。
    expect(root.querySelector('.layadmin-shortcut .layui-hide')).toBeNull()
    expect(root.querySelector('.layadmin-shortcut [style*="display:none"]')).toBeNull()
  })
})

describe('证据件保真：装载后页面不再带跨站资源', () => {
  it('root 里没有 oss4.jidanpu.com 的 <img>（mount 在惰性文档里裁完才塞进活文档）', async () => {
    const { root } = await mountPage()
    expect(root.querySelectorAll('img')).toHaveLength(0)
    expect(root.innerHTML).not.toContain('oss4.jidanpu.com/images')
    // 12 个数据概览面板（pan1..pan10 / pan12 / pan13——**没有 pan11**）+ 2 个发单概览 = 14
    expect(root.querySelector('.pan11')).toBeNull()
    expect(root.querySelectorAll('.reportPanTitle')).toHaveLength(14)
  })
})

/**
 * ── 数据概览 ────────────────────────────────────────────
 *
 * 行为 = `gettoday2()` 那一族：4 颗按钮各算一段 `[start, end]`、日期框是真 `laydate`
 * 区间选择器、管理员才取数、非管理员**一发都不发**。
 */
describe('数据概览：窗口算术（纯函数）', () => {
  //: 固定「今天」= 2026-09-20（本地时区）。⛔ 不让用例随系统日期漂。
  const NOW = new Date(2026, 8, 20, 10, 30, 0)

  it('T2-P1 4 个档位各算一段含首尾的窗口（今天 / 昨天 / 近7天 / 近30天）', () => {
    expect(overviewWindow('today', NOW)).toEqual({ start: '2026-09-20', end: '2026-09-20' })
    expect(overviewWindow('yesterday', NOW)).toEqual({ start: '2026-09-19', end: '2026-09-19' })
    expect(overviewWindow('last7days', NOW)).toEqual({ start: '2026-09-14', end: '2026-09-20' })
    expect(overviewWindow('last30days', NOW)).toEqual({ start: '2026-08-22', end: '2026-09-20' })
  })

  it('T2-P2 跨月 / 跨年也算得对（含非闰年的 2 月）', () => {
    expect(overviewWindow('yesterday', new Date(2026, 2, 1, 23, 59, 0))).toEqual({
      start: '2026-02-28',
      end: '2026-02-28',
    })
    expect(overviewWindow('last30days', new Date(2026, 2, 1, 0, 0, 0))).toEqual({
      start: '2026-01-31',
      end: '2026-03-01',
    })
    expect(overviewWindow('yesterday', new Date(2027, 0, 1, 0, 0, 0))).toEqual({
      start: '2026-12-31',
      end: '2026-12-31',
    })
  })

  it('T2-P3 `isoDate` 走**本地**日期（⛔ 不是 `toISOString()` 那种 UTC 串）', () => {
    // 本地 2026-09-20 00:30 = UTC 2026-09-19 16:30 ⇒ `toISOString().slice(0,10)` 会给出 09-19。
    expect(isoDate(new Date(2026, 8, 20, 0, 30, 0))).toBe('2026-09-20')
    expect(rangeValue('2026-09-20', '2026-09-20')).toBe('2026-09-20 ~ 2026-09-20')
  })

  it('T2-P4 `parseRangeValue` 只认两段 `yyyy-MM-dd` 且顺序不乱', () => {
    expect(parseRangeValue('2026-09-01 ~ 2026-09-05')).toEqual({
      start: '2026-09-01',
      end: '2026-09-05',
    })

    // 只点了起点（laydate 的中间态）/ 空串 / 形状不对 / 顺序反 / 多余分段 ⇒ 一律 null（不抛）
    expect(parseRangeValue('2026-09-01')).toBeNull()
    expect(parseRangeValue('')).toBeNull()
    expect(parseRangeValue('2026/09/01 ~ 2026/09/05')).toBeNull()
    expect(parseRangeValue('2026-09-05 ~ 2026-09-01')).toBeNull()
    expect(parseRangeValue('2026-09-01 ~ 2026-09-02 ~ 2026-09-03')).toBeNull()
  })

  it('T2-P5 `fillOverview` 把 12 格逐格填进证据件的 `.panN`（pan10 是 `N(x%)`，且**没有** pan11）', () => {
    const root = rendered()
    fillOverview(root, OVERVIEW)
    const text = (selector: string): string | null =>
      root.querySelector(selector)?.textContent ?? null

    expect(text('.pan1')).toBe('7') // 录单笔数
    expect(text('.pan2')).toBe('1234.50') // 订单总额
    expect(text('.pan3')).toBe('800.00') // 发单总额
    expect(text('.pan4')).toBe('434.50') // 预计利润
    expect(text('.pan5')).toBe('35.2%') // 利润率
    expect(text('.pan6')).toBe('5') // 接手数量
    expect(text('.pan7')).toBe('71.4%') // 接手率
    expect(text('.pan8')).toBe('0') // 撤销数量（恒 0）
    expect(text('.pan9')).toBe('0.0%') // 撤销率（恒 0.0）
    expect(text('.pan10')).toBe('2(28.6%)') // 退单数量：**一个字符串**
    expect(text('.pan12')).toBe('3') // 完成数量
    expect(text('.pan13')).toBe('42.9%') // 完成率
    // ⚠️ 证据件本来就跳号：`pan11` 不存在（⛔ 别补一格）。
    expect(root.querySelector('.pan11')).toBeNull()
    // 发单概览那两格**不归本函数**。
    expect(text('.pushOrderAllSpan')).toBe('0')
  })
})

describe('数据概览：4 颗按钮 / 日期区间 / 非管理员（真 DOM + 桩请求）', () => {
  //: 固定「今天」= 2026-09-20。⛔ 用例不许随系统日期漂（否则「近 7 天」那两条断言会飘）。
  const NOW = new Date(2026, 8, 20, 10, 30, 0)

  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  /** 本用例里发往「数据概览」端点的请求（真值取调用时的 `params`）。 */
  const overviewCalls = (): Call[] => calls.filter((call) => call.url === HOME_OVERVIEW_URL)

  it('T2-1 装载即按「今天」取一发、区间框默认值写成今天、并把「今天」点亮（另三颗摘掉）', async () => {
    respondWith(NOTICES, OVERVIEW_ADMIN_ME, OVERVIEW)
    const { root } = await mountPage()

    expect(overviewCalls()).toHaveLength(1)
    expect(overviewCalls()[0]!.params).toEqual({ start: '2026-09-20', end: '2026-09-20' })

    const input = root.querySelector<HTMLInputElement>(`#${OVERVIEW_DATE_INPUT_ID}`)!
    expect(input.value).toBe('2026-09-20 ~ 2026-09-20')
    // ⛔ 不是证据件里那个抓取当天的日期串（计划 §4-T2 明写不许硬编码它）。
    expect(input.value).not.toBe('2026-09-19 ~ 2026-09-19')
    // 真控件 ⇒ 没有 `readonly`、也没有 `data-notopen`。
    expect(input.hasAttribute('readonly')).toBe(false)
    expect(input.getAttribute('data-notopen')).toBeNull()

    expect(root.querySelector('.today')!.classList.contains(OVERVIEW_ACTIVE_CLASS)).toBe(true)
    for (const kind of ['yesterday', 'last7days', 'last30days']) {
      expect(root.querySelector(`.${kind}`)!.classList.contains(OVERVIEW_ACTIVE_CLASS)).toBe(false)
    }
    // 12 格已按桩值回填（`pan2` 两位小数、`pan5` 一位小数 + `%`）。
    expect(root.querySelector('.pan1')!.textContent).toBe('7')
    expect(root.querySelector('.pan2')!.textContent).toBe('1234.50')
    expect(root.querySelector('.pan5')!.textContent).toBe('35.2%')
  })

  it('T2-2 4 颗按钮各点一下 ⇒ 请求参数是真值，且选中态跟着走（点中的有、另三颗摘掉）', async () => {
    respondWith(NOTICES, OVERVIEW_ADMIN_ME, OVERVIEW)
    const { root } = await mountPage()
    calls.length = 0

    const table: Array<[string, { start: string; end: string }]> = [
      ['yesterday', { start: '2026-09-19', end: '2026-09-19' }],
      ['today', { start: '2026-09-20', end: '2026-09-20' }],
      ['last7days', { start: '2026-09-14', end: '2026-09-20' }],
      ['last30days', { start: '2026-08-22', end: '2026-09-20' }],
    ]
    for (const [kind, expected] of table) {
      const before = overviewCalls().length
      root.querySelector(`.${kind}`)!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      const issued = overviewCalls()
      expect(issued).toHaveLength(before + 1)
      expect(issued[issued.length - 1]!.params).toEqual(expected)
      // 选中态：点中的那颗有 `layui-border-blue`，另三颗都没有（逐颗断言，不是「至少一颗」）。
      for (const other of ['yesterday', 'today', 'last7days', 'last30days']) {
        expect(
          root.querySelector(`.${other}`)!.classList.contains(OVERVIEW_ACTIVE_CLASS),
          `${kind} 点完后 ${other} 的选中态`,
        ).toBe(other === kind)
      }
      // 区间框的值同步成这一段（按钮与日期框是同一个窗口的两种入口）。
      expect(root.querySelector<HTMLInputElement>(`#${OVERVIEW_DATE_INPUT_ID}`)!.value).toBe(
        rangeValue(expected.start, expected.end),
      )
    }
  })

  it('T2-3 非管理员 ⇒ **一次概览请求都不发**，12 格保持证据件的 0 / 0%', async () => {
    // 桩**备好**了（传了 overview），所以「没发」不是因为端点没桩，而是页面按权限提前返回。
    respondWith(NOTICES, ME, OVERVIEW)
    const { root } = await mountPage()

    expect(overviewCalls()).toHaveLength(0)
    // ⭐ 非管理员那 4 颗按钮与日期框**不是「点了什么都不发生」的死控件**——它们带 `data-overview-locked="仅系统管理员可查看数据概览"`，点击弹这句**权限**
    //    原因（⛔ 不是「尚未开放」：那是另一件事）；日期框另加 `readonly`。
    for (const kind of ['yesterday', 'today', 'last7days', 'last30days']) {
      const button = root.querySelector(`.${kind}`)!
      expect(button.getAttribute(OVERVIEW_LOCKED_ATTR)).toBe(OVERVIEW_LOCKED_REASON)
      button.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    }
    const dateInput = root.querySelector(`#${OVERVIEW_DATE_INPUT_ID}`)!
    expect(dateInput.getAttribute(OVERVIEW_LOCKED_ATTR)).toBe(OVERVIEW_LOCKED_REASON)
    expect(dateInput.hasAttribute('readonly')).toBe(true)
    dateInput.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(overviewCalls()).toHaveLength(0)
    expect(h.msgs).toEqual(new Array(5).fill(OVERVIEW_LOCKED_REASON))
    // ⛔ 借 `data-notopen` 打标会把「全页恰好 12 个占位」那条判据搅浑（S10）⇒ 一个都不许有。
    expect(root.querySelectorAll('[data-notopen]')).toHaveLength(12)
    // laydate 也没渲染（非管理员不接线）。
    expect(h.laydates).toHaveLength(0)

    // ⭐ 窗口的**可见态**对非管理员也是**决定**、不是碰巧（必须有专门的用例钉它）：
    //    默认窗口仍是「今天」（框里填今天、今天那颗点亮）——它说明这张卡算的是今天的口径，
    //    与「你没有权限看数据」不矛盾。
    expect((dateInput as HTMLInputElement).value).toBe(`${isoDate(NOW)} ~ ${isoDate(NOW)}`)
    expect(root.querySelector('.today')!.classList.contains(OVERVIEW_ACTIVE_CLASS)).toBe(true)
    for (const kind of ['yesterday', 'last7days', 'last30days']) {
      expect(root.querySelector(`.${kind}`)!.classList.contains(OVERVIEW_ACTIVE_CLASS)).toBe(false)
    }

    // 12 格 = 证据件的字面值（⛔ 不是「差不多 0」：逐格打值）。
    const text = (selector: string): string | null =>
      root.querySelector(selector)?.textContent ?? null
    expect(text('.pan1')).toBe('0')
    expect(text('.pan2')).toBe('0')
    expect(text('.pan3')).toBe('0')
    expect(text('.pan4')).toBe('0')
    expect(text('.pan5')).toBe('0%')
    expect(text('.pan6')).toBe('0')
    expect(text('.pan7')).toBe('0%')
    expect(text('.pan8')).toBe('0')
    expect(text('.pan9')).toBe('0%')
    expect(text('.pan10')).toBe('0')
    expect(text('.pan12')).toBe('0')
    expect(text('.pan13')).toBe('0%')
  })

  it('T2-4 日期框被渲染成真区间选择器：`range:"~"` + 默认值 = 今天；`done` 回调按选中区间取数', async () => {
    respondWith(NOTICES, OVERVIEW_ADMIN_ME, OVERVIEW)
    const { root } = await mountPage()

    expect(h.laydates).toHaveLength(1)
    const options = h.laydates[0]!
    expect(options.elem).toBe(`#${OVERVIEW_DATE_INPUT_ID}`)
    expect(options.range).toBe('~')
    expect(options.value).toBe('2026-09-20 ~ 2026-09-20')
    // ⛔ 不照抄对标那对抓取当天算出来的 `min` / `max`（照抄 = 凭空限死用户能选的范围）。
    expect(options.min).toBeUndefined()
    expect(options.max).toBeUndefined()

    calls.length = 0
    // 自选区间 ⇒ 取数 + 区间框同步 + 4 颗按钮的选中态全摘（窗口不再是某个档位了）。
    ;(options.done as (value: string) => void)('2026-09-01 ~ 2026-09-05')
    expect(overviewCalls()).toHaveLength(1)
    expect(overviewCalls()[0]!.params).toEqual({ start: '2026-09-01', end: '2026-09-05' })
    expect(root.querySelector<HTMLInputElement>(`#${OVERVIEW_DATE_INPUT_ID}`)!.value).toBe(
      '2026-09-01 ~ 2026-09-05',
    )
    for (const kind of ['yesterday', 'today', 'last7days', 'last30days']) {
      expect(root.querySelector(`.${kind}`)!.classList.contains(OVERVIEW_ACTIVE_CLASS)).toBe(false)
    }

    // 只点了起点（laydate 的中间态）⇒ 不发请求（`parseRangeValue` 判 null）。
    calls.length = 0
    ;(options.done as (value: string) => void)('2026-09-02')
    expect(overviewCalls()).toHaveLength(0)
  })

  it('T2-5 真 DOM：点数据概览那 4 颗**不**弹「尚未开放」；点发单概览那 4 颗**照旧**弹', async () => {
    respondWith(NOTICES, OVERVIEW_ADMIN_ME, OVERVIEW)
    const { root } = await mountPage()
    h.msgs.length = 0

    for (const kind of ['yesterday', 'today', 'last7days', 'last30days']) {
      root.querySelector(`.${kind}`)!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    }
    expect(h.msgs).toEqual([])

    for (const kind of ['yesterday2', 'today2', 'last7days2', 'last30days2']) {
      root.querySelector(`.${kind}`)!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    }
    expect(h.msgs).toEqual([
      '「昨天」尚未开放',
      '「今天」尚未开放',
      '「最近7天」尚未开放',
      '「最近30天」尚未开放',
    ])
  })

  it('T2-6 端点报错时只回落（12 格仍是 0/0%），不打断整页装载', async () => {
    mockedRequest.mockImplementation((async (config: Call) => {
      calls.push(config)
      if (config.url === '/api/auth/me') return { user: OVERVIEW_ADMIN_ME }
      if (config.url === '/api/business/games/') {
        return { items: GAMES, total: GAMES.length, page: 1, page_size: 200 }
      }
      if (config.url === '/api/business/notifications/') {
        return { items: NOTICES, total: NOTICES.length, page: 1, page_size: NOTICE_PAGE_SIZE }
      }
      if (config.url === HOME_OVERVIEW_URL) throw new Error('概览炸了')
      throw new Error(`本用例没预期这个请求：${String(config.url)}`)
    }) as never)

    const { root, unmount } = await mountPage()
    expect(typeof unmount).toBe('function')
    expect(root.querySelector('.pan1')!.textContent).toBe('0')
    expect(root.querySelector('.pan5')!.textContent).toBe('0%')
    // 页面骨架照旧（不是整页空白）。
    expect(root.querySelector('.layadmin-shortcut')).not.toBeNull()
    expect(root.querySelectorAll('.reportPanTitle')).toHaveLength(14)
  })
})

/* ═══════════════════════════════════════════════════════════════════════════
 * 首页二维码横幅（自造区，D-0262）。
 * 纯函数钉标记形状；真 DOM 用例钉轮播（定时器照 E-0198：先 useFakeTimers 再包装
 * window.setInterval，每用例经 afterEach 统一收口）、放大、权限门与管理动作。
 * ═══════════════════════════════════════════════════════════════════════════ */

describe('二维码横幅：纯函数（分桶与标记）', () => {
  it('Q-P1 qrBuckets：三格分桶 + 群码按 (sort_order, id) 稳定排序', () => {
    const buckets = qrBuckets([
      qrRow(2, 'qq_group', { sort_order: 1 }),
      qrRow(1, 'qq_bot'),
      qrRow(3, 'qq_group', { sort_order: 0 }),
      qrRow(4, 'wechat_mp'),
    ])
    expect(buckets.bot!.id).toBe(1)
    expect(buckets.mp!.id).toBe(4)
    expect(buckets.group.map((row) => row.id)).toEqual([3, 2])
  })

  it('Q-P2 qrBannerHtml：通栏文案逐字；全空 ⇒ 三格各一处占位文案', () => {
    const html = qrBannerHtml([], false)
    expect(html).toContain(QR_BANNER_TEXT)
    expect(html.match(new RegExp(QR_EMPTY_TEXT, 'g'))).toHaveLength(3)
  })

  it('Q-P3 qrBannerHtml：无节点零管理按钮；有节点有全套管理按钮；停用行有徽标与压暗图', () => {
    const disabledBot = qrRow(1, 'qq_bot', { is_enabled: false })
    // ⚠️ 判据打在**解析后的 DOM**上，不打在原文 contains：`<style>` 块里恒有类名规则，
    // 字符串 contains 会把「没渲染」误判成「渲染了」（Q-P4 的点点同理）。
    const hostOf = (html: string): HTMLElement => {
      const host = document.createElement('div')
      host.innerHTML = html
      return host
    }
    const viewer = hostOf(qrBannerHtml([disabledBot], false))
    expect(viewer.querySelector('[data-act]')).toBeNull()
    expect(viewer.querySelector('.tt-qr-badge-disabled')!.textContent).toBe('已停用')
    expect(viewer.querySelector('img.tt-qr-img-off')).not.toBeNull()

    const manager = hostOf(qrBannerHtml([disabledBot], true))
    expect(manager.querySelector('[data-act="replace"]')).not.toBeNull()
    const toggle = manager.querySelector<HTMLElement>('[data-act="toggle"]')!
    expect(toggle.textContent).toBe('启用')
    expect(manager.querySelector('[data-act="delete"]')).not.toBeNull()
    expect(manager.querySelector('[data-act="add"]')).not.toBeNull()
  })

  it('Q-P4 qrBannerHtml：群码 ≥2 张才有点点指示且第一张点亮；单张无点点', () => {
    const hostOf = (html: string): HTMLElement => {
      const host = document.createElement('div')
      host.innerHTML = html
      return host
    }
    const one = hostOf(qrBannerHtml([qrRow(11, 'qq_group', { sort_order: 0 })], false))
    expect(one.querySelectorAll('.tt-qr-dots')).toHaveLength(0)
    const two = hostOf(
      qrBannerHtml(
        [qrRow(11, 'qq_group', { sort_order: 0 }), qrRow(12, 'qq_group', { sort_order: 1 })],
        false,
      ),
    )
    const dots = two.querySelectorAll('.tt-qr-dot')
    expect(dots).toHaveLength(2)
    expect(dots[0]!.classList.contains(QR_DOT_ON_CLASS)).toBe(true)
    expect(dots[1]!.classList.contains(QR_DOT_ON_CLASS)).toBe(false)
  })

  // ↓↓ 以下两条 = 二维码横幅视觉修复（docs/plans/2026-10-19-qr-banner-visual-fix.md）的纯函数钉。
  it('Q-P5 qrBannerHtml：群码 ≥2 张才渲染左右箭头（钉在群码格展示区里）；单张不渲染', () => {
    const hostOf = (html: string): HTMLElement => {
      const host = document.createElement('div')
      host.innerHTML = html
      return host
    }
    const one = hostOf(qrBannerHtml([qrRow(11, 'qq_group', { sort_order: 0 })], false))
    expect(one.querySelectorAll('.tt-qr-prev')).toHaveLength(0)
    expect(one.querySelectorAll('.tt-qr-next')).toHaveLength(0)

    const two = hostOf(
      qrBannerHtml(
        [qrRow(11, 'qq_group', { sort_order: 0 }), qrRow(12, 'qq_group', { sort_order: 1 })],
        false,
      ),
    )
    expect(two.querySelectorAll('.tt-qr-prev')).toHaveLength(1)
    expect(two.querySelectorAll('.tt-qr-next')).toHaveLength(1)
    // 箭头只长在群码格的展示区里；机器人 / 公众号两格没有（它们点图 = 放大，不是切换）
    expect(two.querySelector(`${QR_CARD_SELECTOR}[data-kind="qq_group"] .tt-qr-prev`)).not.toBeNull()
    expect(two.querySelector(`${QR_CARD_SELECTOR}[data-kind="qq_bot"] .tt-qr-prev`)).toBeNull()
    expect(two.querySelector(`${QR_CARD_SELECTOR}[data-kind="wechat_mp"] .tt-qr-next`)).toBeNull()
  })

  it('Q-P6 横幅样式钉值：展示区固定 300px 高、img 满容器等比 contain；箭头命中区 32px 且不挂 hover 门', () => {
    const host = document.createElement('div')
    host.innerHTML = qrBannerHtml([], false)
    const css = host.querySelector('style')!.textContent ?? ''
    const ruleOf = (className: string): string => {
      const hit = css.match(new RegExp(`\\.${className}\\{([^}]*)\\}`))
      expect(hit, `样式表里找不到 .${className} 规则`).not.toBeNull()
      return hit![1]!
    }
    // A（图片自适应，真机反馈：大图缩得很小、小图更小）：外层容器高度钉死 + 宽度撑满，
    // img 满容器 + `object-fit:contain` 等比缩放居中——任何图都尽量占满展示区。
    expect(ruleOf('tt-qr-figure')).toContain('height:300px')
    const imgRule = ruleOf('tt-qr-img')
    expect(imgRule).toContain('width:100%')
    expect(imgRule).toContain('height:100%')
    expect(imgRule).toContain('object-fit:contain')
    // B（箭头常驻可见，真机反馈：看不出「这里可以切」）：命中区 ≥32px；规则里没有 hover 门
    // 与「先藏后显」——常驻 = 通用规则就画出来，不靠悬停。
    for (const cls of ['tt-qr-prev', 'tt-qr-next']) {
      const rule = ruleOf(cls)
      expect(rule).toContain('width:32px')
      expect(rule).toContain('height:32px')
      expect(rule).not.toContain(':hover')
      expect(rule).not.toContain('display:none')
      expect(rule).not.toContain('opacity')
    }
  })
})

describe('二维码横幅：真 DOM（结构 / 权限门 / 管理 / 放大）', () => {
  const q = (scope: Element, selector: string): HTMLElement => {
    const el = scope.querySelector<HTMLElement>(selector)
    expect(el, `页内找不到 ${selector}`).not.toBeNull()
    return el!
  }

  /** 把页面的异步链（写请求 → 重取重绘）冲刷干净：本 describe 不开假定时器，靠微任务排空。 */
  const flushAsync = async (): Promise<void> => {
    for (let i = 0; i < 10; i += 1) await Promise.resolve()
  }

  it('Q1 挂载后横幅插在页面容器顶部（现有卡片整体下移）：通栏文案 + 三格固定顺序与固定标题', async () => {
    const { root } = await mountPage()
    const banner = root.firstElementChild as HTMLElement
    expect(banner.matches(QR_BANNER_SELECTOR)).toBe(true)
    expect(q(banner, '.tt-qr-banner-text').textContent).toBe(QR_BANNER_TEXT)
    expect(
      Array.from(banner.querySelectorAll('.tt-qr-card')).map((card) => card.getAttribute('data-kind')),
    ).toEqual(['qq_bot', 'qq_group', 'wechat_mp'])
    expect(
      Array.from(banner.querySelectorAll('.tt-qr-card-title')).map((title) => title.textContent),
    ).toEqual(['QQ机器人', 'QQ群', '微信公众号'])
  })

  it('Q2 全空 + 无节点：三格都是「二维码待配置」占位；零管理按钮、零查看大图、零提示', async () => {
    const { root } = await mountPage()
    expect(root.querySelectorAll(QR_EMPTY_SELECTOR)).toHaveLength(3)
    expect(root.querySelectorAll('[data-act]')).toHaveLength(0)
    expect(root.querySelectorAll(QR_ZOOM_SELECTOR)).toHaveLength(0)
    // 占位不是死控件连提示都没有的「假象」？——占位是**说明文案**不是按钮，本就无点击语义。
    expect(h.msgs).toEqual([])
  })

  it('Q3 有节点 + 全空：三格都有「添加」；点群码格的添加 ⇒ 弹窗带游戏下拉（现有游戏档案接口的数据）', async () => {
    respondWith(NOTICES, QR_MANAGER_ME)
    const { root } = await mountPage()
    expect(root.querySelectorAll('[data-act="add"]')).toHaveLength(3)
    q(root, `${QR_CARD_SELECTOR}[data-kind="qq_group"] [data-act="add"]`).dispatchEvent(
      new MouseEvent('click', { bubbles: true }),
    )
    expect(h.opened).toHaveLength(1)
    expect(h.opened[0]!.title).toBe('添加游戏群码')
    const content = String(h.opened[0]!.content)
    for (const game of GAMES) expect(content).toContain(`>${game.name}</option>`)
    // 弹窗内容插在 root 之外（.layui-layer-content）⇒ 页内查询必须用 document 级
    expect(document.querySelector('.tt-qr-game-select')).not.toBeNull()
    expect(document.querySelector('.tt-qr-form-file')).not.toBeNull()
  })

  it('Q11 有数据 + 无节点：图照常渲染 + 查看大图在；管理控件一个都不渲染（fail-closed，不是藏）', async () => {
    respondWith(NOTICES, ME, undefined, [], [qrRow(21, 'qq_bot'), qrRow(31, 'wechat_mp')])
    const { root } = await mountPage()
    expect(root.querySelectorAll(QR_ZOOM_SELECTOR)).toHaveLength(2)
    expect(root.querySelectorAll('[data-act]')).toHaveLength(0)
    expect(
      q(root, `${QR_CARD_SELECTOR}[data-kind="qq_bot"] img`).getAttribute('src'),
    ).toBe('https://media.example.com/signed/qr-21.jpg')
    // 不是 display:none 的残留：管理行里根本没有按钮节点
    expect(q(root, `${QR_CARD_SELECTOR}[data-kind="qq_bot"] .tt-qr-actions`).querySelectorAll('button')).toHaveLength(0)
  })

  it('Q12 管理员短路（is_tenant_admin，menu_permissions 为空）同样看到管理控件（与后端 has_node_now 同向）', async () => {
    respondWith(NOTICES, ADMIN_ME, undefined, [], [])
    const { root } = await mountPage()
    expect(root.querySelectorAll('[data-act="add"]')).toHaveLength(3)
  })

  it('Q6 点机器人格的图 ⇒ 开大图层（type 1，内容带现签 URL）', async () => {
    respondWith(NOTICES, ME, undefined, [], [qrRow(21, 'qq_bot'), qrRow(31, 'wechat_mp')])
    const { root } = await mountPage()
    q(root, `${QR_CARD_SELECTOR}[data-kind="qq_bot"] ${QR_FIGURE_SELECTOR}`).dispatchEvent(
      new MouseEvent('click', { bubbles: true }),
    )
    expect(h.opened).toHaveLength(1)
    expect(h.opened[0]!.type).toBe(1)
    expect(String(h.opened[0]!.title)).toBe('QQ机器人二维码')
    expect(String(h.opened[0]!.content)).toContain('https://media.example.com/signed/qr-21.jpg')
  })

  it('Q7 群码点「查看大图」⇒ 大图层标题带当前游戏名', async () => {
    respondWith(NOTICES, ME, undefined, [], [
      qrRow(11, 'qq_group', { sort_order: 0, game_name: '群码游戏甲' }),
      qrRow(12, 'qq_group', { sort_order: 1, game_name: '群码游戏乙' }),
    ])
    const { root } = await mountPage()
    q(root, `${QR_CARD_SELECTOR}[data-kind="qq_group"] ${QR_ZOOM_SELECTOR}`).dispatchEvent(
      new MouseEvent('click', { bubbles: true }),
    )
    expect(h.opened).toHaveLength(1)
    expect(String(h.opened[0]!.title)).toBe('QQ群二维码：群码游戏甲')
  })

  it('Q8 点删除先弹确认层；确认后才发 DELETE（真删）并重取列表', async () => {
    respondWith(
      NOTICES,
      QR_MANAGER_ME,
      undefined,
      [],
      [qrRow(21, 'qq_bot'), qrRow(31, 'wechat_mp')],
    )
    const { root } = await mountPage()
    calls.length = 0
    q(root, `${QR_CARD_SELECTOR}[data-kind="qq_bot"] [data-act="delete"]`).dispatchEvent(
      new MouseEvent('click', { bubbles: true }),
    )
    expect(h.confirms).toHaveLength(1)
    expect(h.confirms[0]!.text).toContain('确定删除')
    expect(calls).toHaveLength(0)
    h.confirms[0]!.yes(0)
    await flushAsync()
    const deleteCall = calls.find((c) => c.method?.toLowerCase() === 'delete')
    expect(deleteCall!.url).toBe('/api/business/home/qr-codes/21/')
    // 删完重取列表（重绘的唯一出口）
    expect(
      calls.filter((c) => c.url === HOME_QR_CODES_URL && c.method?.toLowerCase() === 'get'),
    ).toHaveLength(1)
  })

  it('Q9 点停用发 PATCH `{is_enabled: false}`；已停用格的按钮 = 「启用」+ 徽标 + 压暗图', async () => {
    respondWith(
      NOTICES,
      QR_MANAGER_ME,
      undefined,
      [],
      [qrRow(21, 'qq_bot'), qrRow(31, 'wechat_mp', { is_enabled: false })],
    )
    const { root } = await mountPage()
    calls.length = 0
    q(root, `${QR_CARD_SELECTOR}[data-kind="qq_bot"] [data-act="toggle"]`).dispatchEvent(
      new MouseEvent('click', { bubbles: true }),
    )
    await flushAsync()
    const patch = calls.find((c) => c.method?.toLowerCase() === 'patch')
    expect(patch!.url).toBe('/api/business/home/qr-codes/21/')
    expect(patch!.data).toEqual({ is_enabled: false })
    const mp = q(root, `${QR_CARD_SELECTOR}[data-kind="wechat_mp"]`)
    expect(q(mp, '[data-act="toggle"]').textContent).toBe('启用')
    expect(mp.querySelector('.tt-qr-badge-disabled')).not.toBeNull()
    expect(mp.querySelector('.tt-qr-img-off')).not.toBeNull()
  })

  it('Q10 群码排序：首张没有「前移」；点「后移」⇒ 恰好两行 PATCH 新序号，重取后停在移动后的那一张', async () => {
    // 桩的行数组是**同一个引用**：重取发生在 flush 期间 ⇒ 先把「落库后的形状」写进桩
    // （乙→0、甲→1），重取回来的顺序才会反映移动。
    const rows = [
      qrRow(11, 'qq_group', { sort_order: 0, game_name: '群码游戏甲' }),
      qrRow(12, 'qq_group', { sort_order: 1, game_name: '群码游戏乙' }),
      qrRow(13, 'qq_group', { sort_order: 2, game_name: '群码游戏丙' }),
    ]
    respondWith(NOTICES, QR_MANAGER_ME, undefined, [], rows)
    const { root } = await mountPage()
    const gameName = (): string | null =>
      root.querySelector(`${QR_CARD_SELECTOR}[data-kind="qq_group"] ${QR_GAME_NAME_SELECTOR}`)!
        .textContent
    expect(gameName()).toBe('群码游戏甲')
    expect(root.querySelector(`${QR_CARD_SELECTOR}[data-kind="qq_group"] [data-act="move-up"]`)).toBeNull()
    calls.length = 0
    q(root, `${QR_CARD_SELECTOR}[data-kind="qq_group"] [data-act="move-down"]`).dispatchEvent(
      new MouseEvent('click', { bubbles: true }),
    )
    rows[0]!.sort_order = 1
    rows[1]!.sort_order = 0
    await flushAsync()
    const patches = calls.filter((c) => c.method?.toLowerCase() === 'patch')
    // 全量重排号但只发有变化的行：丙(2→2)不发
    expect(patches.map((p) => `${String(p.url)} ${JSON.stringify(p.data)}`)).toEqual([
      '/api/business/home/qr-codes/12/ {"sort_order":0}',
      '/api/business/home/qr-codes/11/ {"sort_order":1}',
    ])
    // 重取后当前张停在**移动后的那一张**（甲，现在 index 1）——这正是「前移/后移」给操作者的反馈。
    expect(gameName()).toBe('群码游戏甲')
    expect(root.querySelector(`${QR_CARD_SELECTOR}[data-kind="qq_group"] [data-act="move-up"]`)).not.toBeNull()
  })
})

describe('二维码横幅：轮播（E-0198 定时器口径）', () => {
  const GROUPS = [
    qrRow(11, 'qq_group', { sort_order: 0, game_name: '群码游戏甲' }),
    qrRow(12, 'qq_group', { sort_order: 1, game_name: '群码游戏乙' }),
  ]

  /** 本文件里 `setInterval` / `clearInterval` 的记账把手（句柄逐个记下，卸载判据要逐个对）。 */
  const TIMERS = { created: [] as unknown[], cleared: [] as unknown[] }

  const q = (scope: Element, selector: string): HTMLElement => {
    const el = scope.querySelector<HTMLElement>(selector)
    expect(el, `页内找不到 ${selector}`).not.toBeNull()
    return el!
  }

  beforeEach(() => {
    // ⚠️ E-0198：包装必须排在 `useFakeTimers()` **之后**（那时 window.setInterval 已是假实现）；
    // 反了页面会起一个真 Node Timeout，advanceTimersByTime 推不动 ⇒「选了档一发都不发」的假红。
    vi.useFakeTimers()
    TIMERS.created.length = 0
    TIMERS.cleared.length = 0
    const fakeSet = window.setInterval.bind(window)
    const fakeClear = window.clearInterval.bind(window)
    vi.spyOn(window, 'setInterval').mockImplementation(((handler: TimerHandler, timeout?: number) => {
      const id = fakeSet(handler, timeout)
      TIMERS.created.push(id)
      return id
    }) as typeof window.setInterval)
    vi.spyOn(window, 'clearInterval').mockImplementation(((id?: number) => {
      TIMERS.cleared.push(id)
      fakeClear(id)
    }) as typeof window.clearInterval)
  })

  afterEach(() => {
    // 统一收口：先把本用例挂载的页面卸掉（清它的定时器），再丢假定时器环境（E-0198 ②）。
    mounted?.()
    mounted = null
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('Q4 自动 5s 切下一张并循环；悬停暂停、移出恢复；点图立即切下一张；点点跟着走', async () => {
    respondWith(NOTICES, ME, undefined, [], [...GROUPS, qrRow(21, 'qq_bot'), qrRow(31, 'wechat_mp')])
    const { root } = await mountPage()
    const groupCard = (): HTMLElement => q(root, `${QR_CARD_SELECTOR}[data-kind="qq_group"]`)
    const gameName = (): string | null => groupCard().querySelector(QR_GAME_NAME_SELECTOR)!.textContent
    expect(gameName()).toBe('群码游戏甲')
    // 起了恰好一个 5s 轮播 interval
    expect(TIMERS.created).toHaveLength(1)

    vi.advanceTimersByTime(QR_CAROUSEL_INTERVAL_MS)
    expect(gameName()).toBe('群码游戏乙')
    expect(groupCard().querySelectorAll(QR_DOT_SELECTOR)[1]!.classList.contains(QR_DOT_ON_CLASS)).toBe(true)
    expect(groupCard().querySelectorAll(QR_DOT_SELECTOR)[0]!.classList.contains(QR_DOT_ON_CLASS)).toBe(false)

    // 循环回第一张
    vi.advanceTimersByTime(QR_CAROUSEL_INTERVAL_MS)
    expect(gameName()).toBe('群码游戏甲')

    // 悬停暂停：推过两拍也不动
    q(groupCard(), QR_FIGURE_SELECTOR).dispatchEvent(new MouseEvent('mouseenter'))
    vi.advanceTimersByTime(QR_CAROUSEL_INTERVAL_MS * 2)
    expect(gameName()).toBe('群码游戏甲')

    // 移出恢复
    q(groupCard(), QR_FIGURE_SELECTOR).dispatchEvent(new MouseEvent('mouseleave'))
    vi.advanceTimersByTime(QR_CAROUSEL_INTERVAL_MS)
    expect(gameName()).toBe('群码游戏乙')

    // 点击图片 = 立即切下一张（业务方点名）
    q(groupCard(), QR_FIGURE_SELECTOR).dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(gameName()).toBe('群码游戏甲')
  })

  it('Q5 卸载：轮播 interval 逐个清掉（created 与 cleared 一一对应），root 清空', async () => {
    respondWith(NOTICES, ME, undefined, [], GROUPS)
    const { root, unmount } = await mountPage()
    expect(TIMERS.created).toHaveLength(1)
    unmount()
    expect(TIMERS.cleared).toEqual(TIMERS.created)
    expect(root.innerHTML).toBe('')
  })

  it('Q6b 群码 <2 张不起轮播定时器（一发都不该有）', async () => {
    respondWith(NOTICES, ME, undefined, [], [qrRow(21, 'qq_bot'), qrRow(31, 'wechat_mp')])
    await mountPage()
    expect(TIMERS.created).toHaveLength(0)
  })

  it('Q4b 左右箭头常驻可用：右=下一张、左=上一张（都循环）；点点可直达第 N 张；切换不增定时器', async () => {
    respondWith(NOTICES, ME, undefined, [], GROUPS)
    const { root } = await mountPage()
    const groupCard = (): HTMLElement => q(root, `${QR_CARD_SELECTOR}[data-kind="qq_group"]`)
    const gameName = (): string | null => groupCard().querySelector(QR_GAME_NAME_SELECTOR)!.textContent
    // 常驻：装载完（没 hover、没推时间）箭头就已经在 DOM 上——不是悬停才插进来的
    expect(groupCard().querySelectorAll('.tt-qr-prev')).toHaveLength(1)
    expect(groupCard().querySelectorAll('.tt-qr-next')).toHaveLength(1)

    // 右 = 下一张；在最后一张再右 = 循环回第一张
    q(groupCard(), '.tt-qr-next').dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(gameName()).toBe('群码游戏乙')
    q(groupCard(), '.tt-qr-next').dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(gameName()).toBe('群码游戏甲')

    // 左 = 上一张；在第一张往左 = 循环到最后一张
    q(groupCard(), '.tt-qr-prev').dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(gameName()).toBe('群码游戏乙')

    // 点点直达第 N 张（顺手补的直达能力）
    q(groupCard(), '.tt-qr-dot[data-index="0"]').dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(gameName()).toBe('群码游戏甲')
    q(groupCard(), '.tt-qr-dot[data-index="1"]').dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(gameName()).toBe('群码游戏乙')

    // 箭头 / 点点都是页内切换：自动轮播还是原来那一个 interval
    expect(TIMERS.created).toHaveLength(1)
  })
})
