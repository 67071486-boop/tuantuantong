/**
 * 【后台首页】的 **layui 复刻页**（对标物 `/Index/console`，1:1 复刻 + 身份/站内通知接真后端）。
 *
 * 装载顺序 ⛔ 别调换：title → `bootstrapIsland`（运行期插 `layui.css`，挪到塞标记之后会闪没样式的
 * 骨架，E-0067）→ **在惰性文档里裁剪再塞进活文档**（`display:none` 里的跨站 `<img>` 也会发请求；
 * `<template>` 的 content 不是宿主后代 ⇒ `pruneExcluded` 走 `findWithin` 下探，去掉它裁剪会静默全不生效）
 * → `renderTemplate`（`<template>` 内容不参与渲染，必须再跑一次 laytpl 克隆进 `#root`）→
 * `initIslandElements`（必须在标记进来之后）→ 清空快捷操作行 → 五发请求并行各自 catch →
 * 按权限渲染快捷操作 → 占位打标 → 回填「我的信息」→ 数据概览 → 通知公告 → 二维码横幅 → 点击委托。
 *
 * - 快捷操作：对标是服务端按账号渲染不同模板，等价物 = 按权限节点过滤同一张条目表
 *   （`visibleShortcuts`，没权限不渲染 ≠ 置灰）；谓词用 `@/layui-island/perms` 共享那份。
 * - 通知条目点击**不**标记已读（首页没有已读态，标已是用户看不见的副作用）。
 * - 首页顶部「二维码横幅」是**自造区**（决策 D-0262）：⛔ 不属于 1:1 复刻范围，
 *   复刻核对时别把它按「对标没有的东西」裁掉；实现见 `mountQrBanner` 一节。
 */
import { fetchMe, type User } from '@/api/auth'
import { fetchAllGames, type ConfigRow } from '@/api/config'
import {
  createHomeQrCode,
  deleteHomeQrCode,
  fetchHomeQrCodes,
  issueHomeQrTicket,
  updateHomeQrCode,
  type HomeQrKind,
  type HomeQrListPayload,
  type HomeQrRow,
} from '@/api/homeQrCodes'
import { fetchHomeOverview, type HomeOverview } from '@/api/home'
import { fetchNotifications, type Notification } from '@/api/notifications'
import { fetchSystemNotices, type SystemNotice } from '@/api/systemNotices'
import { fetchArtifactHtml } from '@/layui/artifactPage'
import { putFileToStorage } from '@/layui-island/directUpload'
import {
  compressImage,
  MAX_USER_MEDIA_BYTES,
  mediaQuality,
  userMediaTicketExt,
} from '@/layui-island/imageCompress'
import {
  bootstrapIsland,
  initIslandElements,
  islandMsg,
  type IslandContext,
  type IslandPage,
} from '@/layui/island'
import type { LayuiModule } from '@/layui/loader'
import { escapeHtml } from '@/layui-island/escape'
import { canNode, type NodeViewerInput } from '@/layui-island/perms'

/** 证据件（对标页 `/Index/console` 的页内样式段 + 处理过的 body 标记）。 */
export const ARTIFACT_URL = 'replica/index-console.html'

/** 证据件里两个 id：整页标记躺在 `#template` 里，克隆进 `#root`。 */
export const TEMPLATE_ID = 'template'
export const ROOT_ID = 'root'

/** iframe 里的文档标题（页签文案由壳的 `meta.title` 给，这里是文档自己的 title）。 */
const PAGE_TITLE = '后台首页'

/** 通知一页几条。 */
export const NOTICE_PAGE_SIZE = 3

/** 占位控件的统一提示词。 */
export const MORE_LABEL = '尚未开放'

/**
 * ── 数据概览：4 颗时间按钮 + 日期区间框 → 真取数 ────────────
 * 只做「数据概览」这一张卡：下面的「发单概览」整张卡保持占位不变。
 * ⚠️ 两类选择器必须分得开：数据概览的按钮类是**裸** `today` / `yesterday` / `last7days` /
 * `last30days`，发单概览那 4 颗是**带 `2` 后缀**的 ⇒ `class` 精确匹配天然不会互相命中。
 */
export type OverviewRangeKind = 'today' | 'yesterday' | 'last7days' | 'last30days'

/** 4 个档位（顺序 = 证据件里的 `<li>` 顺序：昨天 / 今天 / 最近7天 / 最近30天）。 */
export const OVERVIEW_RANGE_KINDS: readonly OverviewRangeKind[] = [
  'yesterday',
  'today',
  'last7days',
  'last30days',
]

/** 选中态的 class（对标原样：点亮 = 加/摘 `layui-border-blue`）。 */
export const OVERVIEW_ACTIVE_CLASS = 'layui-border-blue'

/**
 * 数据概览**那一个**日期框的 id。
 * ⚠️ 发单概览那个是 `…-date-2`——它是占位、不碰。
 */
export const OVERVIEW_DATE_INPUT_ID = 'ID-laydate-start-date-1'

/** 本地日期 → `yyyy-MM-dd`（⛔ 不用 `toISOString()`：那是 UTC，会把两地日期差一天）。 */
export function isoDate(date: Date): string {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

/** 「今天」按天平移（本地时区；`Date` 自己会把跨月 / 跨年归一化）。 */
function shiftDays(base: Date, days: number): Date {
  const shifted = new Date(base.getFullYear(), base.getMonth(), base.getDate())
  shifted.setDate(shifted.getDate() + days)
  return shifted
}

/**
 * 档位 → `[start, end]`（**含首尾**）：今天 = `[今, 今]`、昨天 = `[昨, 昨]`、
 * 最近7天 = `[今-6, 今]`、最近30天 = `[今-29, 今]`。
 * ⚠️ 这两个跨度是**推断**（对标窗口由服务端算、口径取不到）；要改读法只改这一处，
 * 并由业务方确认（⛔ 别在别处再写一遍跨度）。
 * ⚠️ `now` 有默认值只为**测试**能钉住「今天」；生产调用一律不传。⛔ 不要把窗口写成 UTC。
 */
export function overviewWindow(
  kind: OverviewRangeKind,
  now: Date = new Date(),
): { start: string; end: string } {
  const today = shiftDays(now, 0)
  if (kind === 'today') {
    return { start: isoDate(today), end: isoDate(today) }
  }
  if (kind === 'yesterday') {
    const yesterday = shiftDays(now, -1)
    return { start: isoDate(yesterday), end: isoDate(yesterday) }
  }
  if (kind === 'last7days') {
    return { start: isoDate(shiftDays(now, -6)), end: isoDate(today) }
  }
  return { start: isoDate(shiftDays(now, -29)), end: isoDate(today) }
}

/** `[start, end]` → 区间框的显示值（形状 = 对标服务端渲染的 `A ~ B`）。 */
export function rangeValue(start: string, end: string): string {
  return `${start} ~ ${end}`
}

/**
 * 区间框的值 → `[start, end]`；**形状不对就 `null`**（只有一个日期 / 空 / 顺序反 / 不是
 * `yyyy-MM-dd`）。⛔ 不抛：laydate 在只点了起点时也会回调，那不是错误。
 */
export function parseRangeValue(value: string): { start: string; end: string } | null {
  const parts = String(value ?? '')
    .split('~')
    .map((part) => part.trim())
  if (parts.length !== 2) return null
  const [start, end] = parts as [string, string]
  const shape = /^\d{4}-\d{2}-\d{2}$/
  if (!shape.test(start) || !shape.test(end)) return null
  if (start > end) return null
  return { start, end }
}

/** 数据概览 4 颗按钮的选择器（**裸类名**，精确到这一张卡；见上「两类选择器必须分得开」）。 */
export const OVERVIEW_BUTTON_SELECTOR = '.yesterday, .today, .last7days, .last30days'

/**
 * **非管理员**时给数据概览那 4 颗按钮与日期框打的属性（值 = 点击时提示的那句话）。
 * ⚠️ **有意不复用 `data-notopen`**：语义是「你没有权限看」≠「尚未开放」，
 * 且 `[data-notopen]` 全页恰好 12 个是判据，借它打标会把那个数搅浑。
 */
export const OVERVIEW_LOCKED_ATTR = 'data-overview-locked'

/** 非管理员点数据概览控件时的提示（**说清是权限问题**，⛔ 不说「尚未开放」）。 */
export const OVERVIEW_LOCKED_REASON = '仅系统管理员可查看数据概览'

/**
 * 回填 12 格。逐格对应，**没有 `pan11`**（证据件本来就跳号，⛔ 别补）。
 * ⚠️ 用 `textContent` 而不是 `.html(...)`：整页动态回填一律走 `textContent` / `escapeHtml`，视觉结果相同。
 */
export function fillOverview(root: HTMLElement, data: HomeOverview): void {
  const set = (selector: string, text: string): void => {
    const el = root.querySelector<HTMLElement>(selector)
    if (el) el.textContent = text
  }
  set('.pan1', String(data.add_count))
  set('.pan2', data.order_price)
  set('.pan3', data.hall_price_total)
  set('.pan4', data.profit_amount)
  set('.pan5', `${data.profit_rate}%`)
  set('.pan6', String(data.rec_order_count))
  set('.pan7', `${data.rec_order_rate}%`)
  set('.pan8', String(data.cancel_count))
  set('.pan9', `${data.cancel_order_rate}%`)
  // ⚠️ 对标原样：退单那一格是 `N(x%)` **一个**字符串（`data.refundCount + '(' + … + '%)'`）。
  set('.pan10', `${data.refund_count}(${data.refund_order_rate}%)`)
  set('.pan12', String(data.finish_count))
  set('.pan13', `${data.finish_order_rate}%`)
}

/**
 * 快捷操作格的**条目表**（顺序 = 表序；值逐字照对标，两处抓取同源）。
 * ⚠️ 两点别「顺手整理」：
 * - `layText` 与 `cite` 只有第 7、9 格不同（`管理订单` / `用户管理`）——那是 layui
 *   打开页签时的文案，与可见文字是两件事。
 * - 第 10 格「接单排行榜」是对标里唯一的裸锚点形态（无 `lay-href` / `lay-tips` / `<cite>`）。
 */
export type Shortcut = {
  /** 对标的 `lay-href`（第 10 格是裸 `href`）。 */
  href: string
  /** 可见文字（`<cite>` 的内容；第 10 格是裸文本）。 */
  cite: string
  /** 对标的 `lay-tips`；第 10 格没有。 */
  tips?: string
  /** 对标的 `lay-text`（layui 打开页签的文案）；缺省 = `cite`。 */
  layText?: string
  /** `<i class="layui-icon layui-icon-{icon}">` 的字形名。 */
  icon: string
  /** `<i>` 的内联色（逐字，含 `#` 大小写）。 */
  color: string
  /** 我们的路由 name（`config/menu.ts` 的条目名）；第 10 格没有这一页，给空串。 */
  routeName: string
  /** 权限节点 code；`undefined` = 无门、总是显示（只有第 10 格）。 */
  node?: string
  /** 第 10 格的裸锚点形态（`class` 名）；有它就不渲染 `lay-href`/`lay-tips`/`lay-text`。 */
  plainAnchor?: { className: string }
  /** 文字用裸文本节点而不是 `<cite>`。 */
  bareText?: boolean
  /** 占位格（不进 `SHORTCUT_ROUTES`；点了由 `data-notopen` 提示「尚未开放」）。 */
  placeholder?: boolean
}

/** 每一格的列宽类（对标逐字，`public/replica/index-console.html:100`）。 */
const SHORTCUT_ITEM_CLASS = 'layui-col-xs6 layui-col-sm4 layui-col-md2 layui-col-lg2'

export const SHORTCUTS: readonly Shortcut[] = [
  {
    href: '/Order/myorderlist',
    cite: '我的订单',
    tips: '在这里看指派给我的订单',
    icon: 'list',
    color: '#FFB800',
    routeName: 'orders-mine',
    node: 'my_orders',
  },
  {
    href: '/Emp/myemp',
    cite: '我的收入',
    tips: '在这里可以看我的订单收入',
    icon: 'rmb',
    color: '#CB5DEF',
    routeName: 'agents-my-income',
    node: 'my_performance',
  },
  {
    href: '/Order/insiderecorderlist',
    cite: '内部接单',
    tips: '在这里可以抢接内部的订单',
    icon: 'template',
    color: '#73D08D',
    routeName: 'orders-inside-hall',
    node: 'grab_hall',
  },
  {
    href: '/User/myinfo',
    cite: '我的资料',
    tips: '我的资料',
    icon: 'friends',
    color: '#23d4d4',
    routeName: 'accounts-profile',
    node: 'my_profile',
  },
  {
    href: '/User/mymessage',
    cite: '我的信息',
    tips: '我的信息',
    icon: 'speaker',
    color: '#73D08D',
    routeName: 'accounts-messages',
    node: 'my_profile',
  },
  {
    href: '/Order/addorder',
    cite: '录入订单',
    tips: '将订单录入系统',
    icon: 'add-1',
    color: '#F86738',
    routeName: 'order-new',
    node: 'order.add_edit',
  },
  {
    href: '/Order/orderlist',
    cite: '管理系统内所有订单',
    tips: '管理所有订单',
    layText: '管理订单',
    icon: 'form',
    color: '#23d4d4',
    routeName: 'orders',
    node: 'order.list',
  },
  {
    href: '/Emp/emplist',
    cite: '给打手或客服算账',
    tips: '管理系统所有的代练打手的结算情况',
    icon: 'rmb',
    color: '#CB5DEF',
    routeName: 'agents-performance',
    node: 'finance.payroll',
  },
  {
    href: '/User/userlist',
    cite: '管理系统子账户',
    tips: '管理系统内所有的系统子账户',
    layText: '用户管理',
    icon: 'group',
    color: '#FFB800',
    routeName: 'system-users',
    node: 'system_user.list',
  },
  {
    href: 'javascript:void(0)',
    cite: '接单排行榜',
    icon: 'align-left',
    color: '#FF5722',
    routeName: '',
    plainAnchor: { className: 'rankList' },
    bareText: true,
    placeholder: true,
  },
]

/**
 * 快捷操作格的**导航映射表**（对标 `lay-href` → 我们的路由 name），由 `SHORTCUTS` 派生。
 * 点击委托与 spec 都消费它；不在表里的 `lay-href` 一律**不导航**。
 */
export const SHORTCUT_ROUTES: ReadonlyArray<{ href: string; routeName: string }> = SHORTCUTS.filter(
  (s) => !s.placeholder,
).map((s) => ({ href: s.href, routeName: s.routeName }))

/** 快捷操作**整排容器**的选择器——委托靠它把「快捷操作」这一支收窄到这一排内的锚点。 */
export const SHORTCUT_ROOT_SELECTOR = '.layadmin-shortcut'

/** 快捷操作行的容器选择器（`<ul>` 在 `.layadmin-shortcut` 内）。 */
export const SHORTCUT_LIST_SELECTOR = '.layadmin-shortcut ul'

/**
 * 一格快捷操作的标记。两种形态：第 10 格裸 `href` + 裸文本，其余 9 格
 * `lay-href` / `lay-tips` / `lay-text` + `<cite>`；所有插值过 `escapeHtml`。
 */
export function shortcutHtml(s: Shortcut): string {
  const icon =
    `<i class="layui-icon layui-icon-${escapeHtml(s.icon)}"` +
    ` style="color:${escapeHtml(s.color)}"></i>`
  const label = s.bareText ? escapeHtml(s.cite) : `<cite>${escapeHtml(s.cite)}</cite>`
  const anchor = s.plainAnchor
    ? `<a href="${escapeHtml(s.href)}" class="${escapeHtml(s.plainAnchor.className)}">`
    : `<a lay-href="${escapeHtml(s.href)}" lay-tips="${escapeHtml(s.tips ?? '')}"` +
      ` lay-text="${escapeHtml(s.layText ?? s.cite)}">`
  return `<li class="${SHORTCUT_ITEM_CLASS}">${anchor}${icon}${label}</a></li>`
}

/**
 * 权限门：`node === undefined` ⇒ 总是显示（只有第 10 格）；否则 `canNode(me, node)` 为真才显示
 * ——没权限的**不渲染**（不是置灰）。
 */
export function visibleShortcuts(me: NodeViewerInput): Shortcut[] {
  return SHORTCUTS.filter((s) => s.node === undefined || canNode(me, s.node))
}

/** 把可见的格渲染进 `row`（`innerHTML`），返回渲染条数（`row` 为空 ⇒ 0）。 */
export function renderShortcuts(row: HTMLElement | null, me: NodeViewerInput): number {
  if (!row) return 0
  const visible = visibleShortcuts(me)
  row.innerHTML = visible.map(shortcutHtml).join('')
  return visible.length
}

/** 通知条目的短标题上限。 */
const NOTICE_TITLE_MAX = 50

/** 弹层标题里标题原文的上限。 */
const LAYER_TITLE_MAX = 30

/** `a.viewNotice` 的类名与选择器**分成两个常量**（E-0061 ①：`class="${SEL}"` 会造出 `class=".viewNotice"`）。 */
const NOTICE_LINK_CLASS = 'viewNotice'
const NOTICE_LINK_SELECTOR = '.viewNotice'

/** `a.viewOrder`（弹层里那个「查看订单」）的类名与选择器，同上。 */
const ORDER_LINK_CLASS = 'viewOrder'
const ORDER_LINK_SELECTOR = '.viewOrder'

/** 占位控件的标记属性与它的选择器。 */
const NOTOPEN_ATTR = 'data-notopen'
const NOTOPEN_SELECTOR = '[data-notopen]'

/** 通知列表容器。 */
const NOTICE_LIST_SELECTOR = '.systemNoticeUl'

/** 通知卡里那条声明（「重要提示…」）的锚点。打标与回填租户名两处共用。 */
const DECLARATION_SELECTOR = 'a[lay-tips="点击可以查看声明"]'

/**
 * 证据件里租户名的 **PII 占位符**（3 处租户名一律置「—」，多租户数据不入库）。
 * 运行期把「重要提示…」那一处换成真实租户名——对标由**服务端**渲染，不是设计上的破折号。
 * `String.replace` 只换第一处；该行恰有这一个 `—`（占位符不存在时是 no-op）。
 */
const PII_PLACEHOLDER = '—'

/** 占位文案取不到可见文字时的回落（`cite` / 文本节点都没有 ⇒「该功能」）。 */
const DEFAULT_PLACEHOLDER_LABEL = '该功能'

/** 在 `scope` 与它内部每个 `<template>` 的 **content** 里找第一个匹配（template 内容不是宿主元素的后代）。 */
function findWithin(scope: ParentNode, selector: string): Element | null {
  const direct = scope.querySelector(selector)
  if (direct) return direct
  for (const tpl of Array.from(scope.querySelectorAll('template'))) {
    const hit = tpl.content?.querySelector(selector) ?? null
    if (hit) return hit
  }
  return null
}

/**
 * 把证据件 `#template` 的内容经 laytpl 克隆进 `#root`。
 * ⛔ 与 `order-addorder.ts` 的同名函数**两份实现、不跨页 import**（跨页 import 会把两个 island
 * 页模块打进同一模块图，任一侧的副作用会传染）。取不到 `#template` / `#root` 就返回 `false`。
 */
export function renderTemplate(root: HTMLElement, laytpl: LayuiModule): boolean {
  const tplEl = root.querySelector<HTMLTemplateElement>(`#${TEMPLATE_ID}`)
  const rootEl = root.querySelector<HTMLElement>(`#${ROOT_ID}`)
  if (!tplEl || !rootEl) return false
  const factory = laytpl as unknown as (
    tpl: string,
    opts?: unknown,
  ) => { render: (data?: unknown) => string }
  // `tagStyle: 'modern'` 逐字取自对标；证据件里已没有 laytpl 标签，保留它只为将来重扒时行为一致。
  rootEl.innerHTML = factory(tplEl.innerHTML, { tagStyle: 'modern' }).render()
  return true
}

/**
 * 取「我的信息」表里 `span.tit` 文案等于 `label`（trim 后比较）的那一行**第二个 td**。
 * 取不到返回 `null`。返回 `Element` 而不是 `HTMLElement`（跨文档/替身下 `instanceof` 不可靠）。
 */
export function infoCell(root: HTMLElement, label: string): HTMLElement | null {
  for (const tit of Array.from(root.querySelectorAll('.layui-table-info span.tit'))) {
    if ((tit.textContent ?? '').trim() !== label) continue
    const cell = tit.closest('td')?.nextElementSibling ?? null
    return cell instanceof HTMLElement ? cell : null
  }
  return null
}

/**
 * A2 / A4 的 5 处裁剪。每删一处把**人类可读的中文描述** push 进返回值（便于测试断言条数）。
 * ⛔ **不要**顺手删 `layui-hide` 的「魔兽排团」`li`（「不渲染」已被对标自带的 `layui-hide` 满足）。
 */
export function pruneExcluded(root: HTMLElement): string[] {
  const removed: string[] = []
  const drop = (el: Element | null, description: string): void => {
    if (!el) return
    el.remove()
    removed.push(description)
  }
  // ⚠️ 选择器**只写到 `calculator`，不写 `.html`**：后者会被后端结构网
  // `test_rbac_menu_alignment.py::_island_declared_codes` 当成第 4 个「岛页按钮门」⇒ 后端全套红。
  // 那条网故意不查权限树，所以从源头避开这个形状，⛔ 不去放宽那条网。
  drop(
    findWithin(root, 'a[href*="calculator"]')?.closest('li') ?? null,
    '快捷操作：万能游戏报价算价器',
  )
  drop(
    findWithin(root, '#Operation')?.closest('.layui-card') ?? null,
    '底部卡片：今日订单分时数据概览（ECharts #Operation）',
  )
  drop(findWithin(root, '#qrcodeBox'), '隐藏二维码盒 #qrcodeBox')
  drop(findWithin(root, '#jidanpuqrcodeBox'), '隐藏二维码盒 #jidanpuqrcodeBox')
  drop(findWithin(root, '#jidanpuappqrcodeBox'), '隐藏二维码盒 #jidanpuappqrcodeBox')
  return removed
}

/**
 * 占位控件清单：7 个单点 + **发单概览 4 颗时间按钮** + **1 个日期输入** = **12**。
 * ⛔ 别把后两条放宽回宽选择器（如 `.rep-title-rigth a` / `input[id^=…]`）——那会让已接真端点的
 * 数据概览那 4 颗重新变成「点了说尚未开放」；计数断言（恰好 12）在前端用例里钉住。
 *
 * `wrap` = 打标落在锚点的哪个祖先上；`label` = 覆盖 `placeholderLabel()` 的提示词
 * （只给可见文字是一整句的控件用，如声明那颗，省略时走 `placeholderLabel()`）。
 */
type PlaceholderTarget = { selector: string; wrap?: string; readonly?: boolean; label?: string }

const PLACEHOLDER_TARGETS: readonly PlaceholderTarget[] = [
  // ① 快捷操作「接单排行榜」
  { selector: '.rankList' },
  // ② 我的信息：「实名认证」（那颗 `span` 带 `layui-hide`，照标是条件显示）
  { selector: 'a[lay-text="实名认证"]', wrap: 'span.layui-btn' },
  // ② 我的信息：「点击这里可以给系统续费」
  { selector: 'a[href*="/Pay/SystemRenewal"]' },
  // ③ 通知和公告：声明、购买同款系统、微信小程序、帮助文档
  { selector: DECLARATION_SELECTOR, label: '郑重声明' },
  { selector: '.contactDev' },
  { selector: '.aboutjdpwxapp' },
  { selector: 'a[lay-href="/Helper/index"]' },
  // ④ **发单概览**的 4 颗时间按钮。⚠️ 带 `2` 后缀 = 精确到那一张卡（数据概览那 4 颗是真控件）。
  { selector: '.yesterday2, .today2, .last7days2, .last30days2' },
  // ⑤ **发单概览**那一个日期输入：额外 `readonly`，`value` 不动。
  { selector: 'input#ID-laydate-start-date-2', readonly: true },
]

/** 占位文案：优先 `cite`，其次自身文本，都没有 ⇒「该功能」（日期输入就是这一支）。 */
function placeholderLabel(el: Element): string {
  const text = (el.querySelector('cite')?.textContent ?? el.textContent ?? '').trim()
  return text || DEFAULT_PLACEHOLDER_LABEL
}

/** 给清单上的每个控件加 `data-notopen="<文案>"`，日期输入额外 `readonly`；幂等（已打标的跳过）。 */
export function markPlaceholders(root: HTMLElement): number {
  let marked = 0
  for (const target of PLACEHOLDER_TARGETS) {
    for (const anchor of Array.from(root.querySelectorAll(target.selector))) {
      const el = (target.wrap ? anchor.closest(target.wrap) : null) ?? anchor
      if (target.readonly) el.setAttribute('readonly', 'readonly')
      if (el.hasAttribute(NOTOPEN_ATTR)) continue
      el.setAttribute(NOTOPEN_ATTR, target.label ?? placeholderLabel(el))
      marked += 1
    }
  }
  return marked
}

/** 「我的职位」：数组用 `、` 连接，空（或全空白）⇒「—」。 */
export function formatRoles(me: { roles?: string[] }): string {
  return (me.roles ?? []).filter(Boolean).join('、') || '—'
}

/**
 * 「游戏权限」：按**全量游戏接口的顺序**列出我有的游戏；一个不差时前缀「全部游戏」。
 */
export function formatGameScope(all: { id: number; name: string }[], ids: readonly number[]): string {
  const set = new Set(ids)
  const mine = all.filter((g) => set.has(g.id))
  if (!mine.length) return '—'
  const names = mine.map((g) => g.name)
  return mine.length === all.length && all.length > 0 ? ['全部游戏', ...names].join(',') : names.join(',')
}

/**
 * 通知短标题：`【kind】订单 <单号>`，没有单号时只留 `【kind】`。
 * ⭐ D-0301：<单号> = **展示单号**（`source_order_no || order_no`，来源号非空取它）。
 */
export function noticeText(n: {
  kind: string
  order_no: string | null
  source_order_no?: string | null
}): string {
  const no = n.source_order_no || n.order_no
  return no ? `【${n.kind}】订单 ${no}` : `【${n.kind}】`
}

/** 通知详情的 4 行（`<br/>` 连接）。**每个值都过 `escapeHtml`**。 */
export function noticeLines(n: {
  kind: string
  order_no: string | null
  source_order_no?: string | null
  payload: { from_status?: string } & Record<string, unknown>
  created_at: string
}): string {
  // ⭐ D-0301：「订单号」行 = 展示单号（`source_order_no || order_no`）。
  const no = n.source_order_no || n.order_no
  return [
    `类型：${escapeHtml(n.kind)}`,
    `订单号：${escapeHtml(no ?? '—')}`,
    `状态变化：${escapeHtml(n.payload.from_status ?? '—')}`,
    `通知时间：${escapeHtml(n.created_at)}`,
  ].join('<br/>')
}

/**
 * 首页「通知和公告」卡片里的一条，两种来源：
 * - `system`：【系统内部公告】（整租户可见、没有已读态）；
 * - `notification`：站内通知（被指派 / 被抢走 / 待审核，按收件人）。
 * 对标那张卡的数据源只有系统公告；站内通知是既有功能，两种都要能渲染，公告排前。
 */
export type HomeNotice =
  | { source: 'system'; notice: SystemNotice }
  | { source: 'notification'; notice: Notification }

/** 一条首页公告的短标题：公告用 `【公告】标题`，站内通知沿用 `noticeText`。 */
export function homeNoticeText(item: HomeNotice): string {
  if (item.source === 'system') return `【公告】${item.notice.title || '（无标题公告）'}`
  return noticeText(item.notice)
}

/** 弹层里那段正文：公告读全文（换行转 `<br/>`），站内通知仍是那 4 行。 */
export function homeNoticeBody(item: HomeNotice): string {
  if (item.source === 'system') {
    const content = escapeHtml(item.notice.content ?? '')
      .replace(/\r\n/g, '<br/>')
      .replace(/\n/g, '<br/>')
    const meta = `发布时间：${escapeHtml(item.notice.created_at)}`
    return `${content}<br/><br/><span style="color:#999">${meta}</span>`
  }
  return noticeLines(item.notice)
}

/** 列表项上的 `data-id`（两种来源的 `id` 会撞号，加前缀区分）。 */
function homeNoticeKey(item: HomeNotice): string {
  return item.source === 'system' ? `s:${item.notice.id}` : `n:${item.notice.id}`
}

/** 一条首页公告 / 通知的列表项标记。 */
function homeNoticeItemHtml(item: HomeNotice): string {
  const short = homeNoticeText(item).slice(0, NOTICE_TITLE_MAX)
  return (
    `<li><a href="javascript:void(0)" class="${NOTICE_LINK_CLASS}"` +
    ` data-id="${escapeHtml(homeNoticeKey(item))}" title="${escapeHtml(short)}">${escapeHtml(short)}...</a></li>`
  )
}

/** 一条通知的列表项标记（保留给「只渲染通知」的调用方；⛔ 不要在两处各写一份列表项形状）。 */
export function noticeItemHtml(n: Notification): string {
  return homeNoticeItemHtml({ source: 'notification', notice: n })
}

/**
 * 回填「我的信息」的一格：**只换格子里的那一处文本**，保留包装元素。
 * ⛔ 不能写 `cell.textContent = …`：「本系统登录账号」格里还有两颗按钮、「我的职位」的值
 * 包在带行内样式的 `<span>` 里——写 `td.textContent` 会把它们连同样式一起抹掉。
 */
function fillCell(cell: Element | null, text: string, selector?: string): void {
  if (!cell) return
  const target = (selector ? cell.querySelector(selector) : null) ?? cell.querySelector(':scope > span')
  if (target) {
    target.textContent = text
    return
  }
  cell.textContent = text
}

/* ════════════════════════════════════════════════════════════════════════════
 * 首页二维码横幅（自造区，决策 D-0262）——⛔ 不属于 1:1 复刻范围，别当「对标没有的东西」裁掉。
 * 对标没有这个功能 ⇒ 标记、文案与视觉值都在本节自持；视觉值取 island 同款 layui 常量，⛔ 不自造花哨样式。
 * ════════════════════════════════════════════════════════════════════════════ */

/** 横幅的管理权限节点（**自造节点**，`benchmark_id = None`，见 `apps/rbac/catalog.py`）。 */
export const QR_MANAGE_NODE = 'home.qr_manage'

/** 横幅通栏文案（业务方逐字给定，D-0262 ②）。 */
export const QR_BANNER_TEXT = '关注工作室微信公众号或 QQ 机器人进入 QQ 群，可获得最新订单推送、快速抢单'

/** 某格没有（可用）数据时的占位文案（占位，不整块藏掉）。 */
export const QR_EMPTY_TEXT = '二维码待配置'

/** 群码轮播的自动切换间隔（业务方点名 5s）。 */
export const QR_CAROUSEL_INTERVAL_MS = 5000

/** 三格的固定标题（不做自由文案字段，标题固定）。 */
export const QR_KIND_LABELS: Record<HomeQrKind, string> = {
  qq_bot: 'QQ机器人',
  qq_group: 'QQ群',
  wechat_mp: '微信公众号',
}

/* ── 类名与选择器**分成两个常量**（E-0061 ①：`class=".x"` 会造出带点的 class）──── */

export const QR_BANNER_CLASS = 'tt-qr-banner'
export const QR_BANNER_SELECTOR = '.tt-qr-banner'
export const QR_TEXT_CLASS = 'tt-qr-banner-text'
export const QR_TEXT_SELECTOR = '.tt-qr-banner-text'
export const QR_GRID_CLASS = 'tt-qr-grid'
export const QR_GRID_SELECTOR = '.tt-qr-grid'
export const QR_CARD_CLASS = 'tt-qr-card'
export const QR_CARD_SELECTOR = '.tt-qr-card'
export const QR_CARD_TITLE_CLASS = 'tt-qr-card-title'
export const QR_CARD_TITLE_SELECTOR = '.tt-qr-card-title'
export const QR_GAME_NAME_CLASS = 'tt-qr-game-name'
export const QR_GAME_NAME_SELECTOR = '.tt-qr-game-name'
export const QR_FIGURE_CLASS = 'tt-qr-figure'
export const QR_FIGURE_SELECTOR = '.tt-qr-figure'
export const QR_IMG_CLASS = 'tt-qr-img'
export const QR_IMG_SELECTOR = '.tt-qr-img'
/** 停用行的图压暗（同类 `.tt-qr-img` 的修饰类）。 */
export const QR_IMG_OFF_CLASS = 'tt-qr-img-off'
export const QR_IMG_OFF_SELECTOR = '.tt-qr-img-off'
export const QR_EMPTY_CLASS = 'tt-qr-empty'
export const QR_EMPTY_SELECTOR = '.tt-qr-empty'
export const QR_DOTS_CLASS = 'tt-qr-dots'
export const QR_DOTS_SELECTOR = '.tt-qr-dots'
export const QR_DOT_CLASS = 'tt-qr-dot'
export const QR_DOT_SELECTOR = '.tt-qr-dot'
export const QR_DOT_ON_CLASS = 'tt-qr-dot-on'
/** 轮播切换箭头（常驻两侧、不挂 hover；`qrArrowsHtml` 只在群码 ≥2 张时拼进展示区）。 */
export const QR_PREV_CLASS = 'tt-qr-prev'
export const QR_PREV_SELECTOR = '.tt-qr-prev'
export const QR_NEXT_CLASS = 'tt-qr-next'
export const QR_NEXT_SELECTOR = '.tt-qr-next'
export const QR_ZOOM_CLASS = 'tt-qr-zoom'
export const QR_ZOOM_SELECTOR = '.tt-qr-zoom'
/** 「查看大图」链的**固定容器**（轮播换张时只重绘容器内部，容器自己不动 ⇒ 定位稳定）。 */
export const QR_ZOOM_HOLDER_CLASS = 'tt-qr-zoom-holder'
export const QR_ZOOM_HOLDER_SELECTOR = '.tt-qr-zoom-holder'
export const QR_ACTIONS_CLASS = 'tt-qr-actions'
export const QR_ACTIONS_SELECTOR = '.tt-qr-actions'
/** 「已停用」徽标。 */
export const QR_BADGE_CLASS = 'tt-qr-badge-disabled'
export const QR_BADGE_SELECTOR = '.tt-qr-badge-disabled'
/** 「添加」弹窗与其里的控件（弹窗插在 `.layui-layer-content` ⇒ 用 **document** 级查询）。 */
const QR_FORM_ID = 'tt-qr-form'
const QR_FORM_SELECTOR = '#tt-qr-form'
const QR_GAME_SELECT_CLASS = 'tt-qr-game-select'
const QR_GAME_SELECT_SELECTOR = '.tt-qr-game-select'
const QR_FORM_FILE_CLASS = 'tt-qr-form-file'
const QR_FORM_FILE_SELECTOR = '.tt-qr-form-file'
/** 「换图」的临时文件输入（myProfile 同款姿势：插进 body 才 click）。 */
const QR_FILE_INPUT_CLASS = 'tt-qr-file-input'

/**
 * 横幅的页内样式（**值出处**：`.layui-card` 的白底/圆角/投影、`.layui-btn` 主题色
 * `#16baaa`、layui 链接蓝 `#01aaed`、分割线灰 `#d2d2d2`/`#eee`——都从 `layui.css` 取）。
 */
const QR_STYLE_HTML = [
  '<style>',
  `.${QR_BANNER_CLASS}{margin-bottom:15px}`,
  `.${QR_TEXT_CLASS}{text-align:center;color:#666;font-size:14px;line-height:20px;padding:10px 0 2px}`,
  `.${QR_GRID_CLASS}{display:flex;gap:10px;margin-top:10px}`,
  `.${QR_CARD_CLASS}{flex:1;background-color:#fff;border-radius:2px;box-shadow:0 1px 2px 0 rgba(0,0,0,.05);padding:10px;text-align:center}`,
  `.${QR_CARD_TITLE_CLASS}{font-weight:700;color:#333;font-size:14px;margin-bottom:4px}`,
  `.${QR_GAME_NAME_CLASS}{color:#16baaa;font-size:13px;line-height:20px;min-height:20px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}`,
  // 展示区高度钉死 300px、宽度撑满，img `object-fit:contain` 等比缩放居中（大图缩小、小图放大）；
  // `position:relative` 是轮播箭头（绝对定位在两侧）的定位基准；占位文案行高跟着 300px 走（298+2）。
  `.${QR_FIGURE_CLASS}{cursor:pointer;height:300px;width:100%;display:flex;align-items:center;justify-content:center;overflow:hidden;position:relative}`,
  `.${QR_IMG_CLASS}{width:100%;height:100%;object-fit:contain}`,
  `.${QR_IMG_OFF_CLASS}{opacity:.4}`,
  `.${QR_EMPTY_CLASS}{color:#999;font-size:13px;width:100%;line-height:298px;border:1px dashed #d2d2d2;border-radius:2px}`,
  `.${QR_DOTS_CLASS}{margin-top:6px;height:12px}`,
  `.${QR_DOT_CLASS}{display:inline-block;width:8px;height:8px;border-radius:50%;background-color:#d2d2d2;margin:0 3px;cursor:pointer}`,
  // 轮播切换箭头（‹ ›）：**常驻**（没有 hover 门）且命中区 32×32（好点）；白底半透明 + 灰边 + 主题色字形。
  `.${QR_PREV_CLASS}{position:absolute;top:50%;transform:translateY(-50%);left:6px;width:32px;height:32px;line-height:30px;text-align:center;border:1px solid #d2d2d2;border-radius:50%;background-color:rgba(255,255,255,.9);color:#16baaa;font-size:20px;cursor:pointer;user-select:none;z-index:2}`,
  `.${QR_NEXT_CLASS}{position:absolute;top:50%;transform:translateY(-50%);right:6px;width:32px;height:32px;line-height:30px;text-align:center;border:1px solid #d2d2d2;border-radius:50%;background-color:rgba(255,255,255,.9);color:#16baaa;font-size:20px;cursor:pointer;user-select:none;z-index:2}`,
  `.${QR_DOT_ON_CLASS}{background-color:#16baaa}`,
  `.${QR_ZOOM_CLASS}{color:#01aaed;font-size:12px}`,
  `.${QR_BADGE_CLASS}{display:inline-block;font-size:12px;font-weight:400;color:#ff5722;border:1px solid #ff5722;border-radius:2px;padding:0 4px;margin-left:6px;vertical-align:1px}`,
  `.${QR_ACTIONS_CLASS}{margin-top:8px;display:flex;flex-wrap:wrap;gap:4px;justify-content:center}`,
  '</style>',
].join('')

/** 三格的数据分桶（`qq_group` 再稳定排一次：后端有序，这里防御「将来后端改了排序」轮播乱序）。 */
export function qrBuckets(rows: readonly HomeQrRow[]): {
  bot: HomeQrRow | null
  group: HomeQrRow[]
  mp: HomeQrRow | null
} {
  let bot: HomeQrRow | null = null
  let mp: HomeQrRow | null = null
  const group: HomeQrRow[] = []
  for (const row of rows) {
    if (row.kind === 'qq_bot') {
      if (bot === null) bot = row
    } else if (row.kind === 'wechat_mp') {
      if (mp === null) mp = row
    } else {
      group.push(row)
    }
  }
  group.sort((a, b) => a.sort_order - b.sort_order || a.id - b.id)
  return { bot, group, mp }
}

/** 一张图（或占位）。`media_url` 取不到（对象存储抽风）时也落占位——同 `HomeQrCodeSerializer` 的回落口径。 */
function qrFigureHtml(row: HomeQrRow | null): string {
  if (!row || !row.media_url) {
    return `<div class="${QR_EMPTY_CLASS}">${QR_EMPTY_TEXT}</div>`
  }
  const off = row.is_enabled ? '' : ` ${QR_IMG_OFF_CLASS}`
  return `<img class="${QR_IMG_CLASS}${off}" src="${escapeHtml(row.media_url)}" alt="二维码">`
}

/**
 * 轮播切换箭头（‹ ›）。**常驻**（不挂 hover）且命中区 32×32。
 * ⛔ 只该在群码 ≥2 张时拼进展示区（单张没有「切」可言；调用方负责这个条件）。
 */
function qrArrowsHtml(): string {
  return `<span class="${QR_PREV_CLASS}">‹</span><span class="${QR_NEXT_CLASS}">›</span>`
}

/** 管理按钮（`data-act` + `data-id`/`data-kind`，处置见 `mountQrBanner` 的 `handleAct`）。 */
function qrButton(act: string, label: string, attrs: Record<string, string | number>): string {
  const danger = act === 'delete' ? ' layui-btn-danger' : ' layui-btn-primary'
  const data = Object.entries(attrs)
    .map(([key, value]) => ` data-${key}="${escapeHtml(String(value))}"`)
    .join('')
  return `<button type="button" class="layui-btn layui-btn-xs${danger}" data-act="${act}"${data}>${label}</button>`
}

/** 固定格（机器人 / 公众号）的操作行：有行 = 换图/启停/删；没行 = 添加。 */
function qrFixedActionsHtml(row: HomeQrRow | null, kind: HomeQrKind): string {
  if (!row) return qrButton('add', '添加', { kind })
  return (
    qrButton('replace', '换图', { id: row.id }) +
    qrButton('toggle', row.is_enabled ? '停用' : '启用', { id: row.id }) +
    qrButton('delete', '删除', { id: row.id })
  )
}

/** 群码格的操作行：添加游戏群码 + **当前张**的 前移/后移/换图/启停/删（「每张的…」= 操作当前张）。 */
function qrGroupActionsHtml(buckets: ReturnType<typeof qrBuckets>, index: number): string {
  const buttons = [qrButton('add', '添加游戏群码', { kind: 'qq_group' })]
  const row = buckets.group[index]
  if (row) {
    if (index > 0) buttons.push(qrButton('move-up', '前移', { id: row.id }))
    if (index < buckets.group.length - 1) buttons.push(qrButton('move-down', '后移', { id: row.id }))
    buttons.push(qrButton('replace', '换图', { id: row.id }))
    buttons.push(qrButton('toggle', row.is_enabled ? '停用' : '启用', { id: row.id }))
    buttons.push(qrButton('delete', '删除', { id: row.id }))
  }
  return buttons.join('')
}

/** 一格的标记。`titleBadge` = 「已停用」徽标；`zoomable` = 给「查看大图」链。 */
function qrCardHtml(
  kind: HomeQrKind,
  parts: { titleBadge: string; gameName: string; figure: string; zoom: string; actions: string; dots: string },
): string {
  return (
    `<div class="${QR_CARD_CLASS}" data-kind="${kind}">` +
    `<div class="${QR_CARD_TITLE_CLASS}">${QR_KIND_LABELS[kind]}${parts.titleBadge}</div>` +
    (kind === 'qq_group'
      ? `<div class="${QR_GAME_NAME_CLASS}">${escapeHtml(parts.gameName)}</div>`
      : '') +
    `<div class="${QR_FIGURE_CLASS}">${parts.figure}</div>` +
    parts.dots +
    `<div class="${QR_ZOOM_HOLDER_CLASS}" style="margin-top:4px">${parts.zoom}</div>` +
    `<div class="${QR_ACTIONS_CLASS}">${parts.actions}</div>` +
    '</div>'
  )
}

/**
 * 横幅整块标记（纯函数，测试钉「通栏文案 / 三格顺序 / 占位 / 管理控件按节点渲染」都打在这里）。
 *
 * ⚠️ 停用行**画出来**（压暗 + 「已停用」徽标）：持点的人必须能看见并重新启用它们——
 * 「不持点只见启用行」的过滤在**服务端 queryset**，这里不做第二份（feedback 同口径）。
 */
export function qrBannerHtml(rows: readonly HomeQrRow[], canManage: boolean): string {
  const buckets = qrBuckets(rows)
  const cardOf = (kind: HomeQrKind, row: HomeQrRow | null): string => {
    const drawable = row !== null && row.media_url !== null
    return qrCardHtml(kind, {
      titleBadge: row !== null && !row.is_enabled ? `<span class="${QR_BADGE_CLASS}">已停用</span>` : '',
      gameName: kind === 'qq_group' ? (row?.game_name ?? '') : '',
      figure: qrFigureHtml(row),
      dots: '',
      zoom: drawable
        ? `<a href="javascript:void(0)" class="${QR_ZOOM_CLASS}" data-id="${row!.id}">查看大图</a>`
        : '',
      actions: canManage ? qrFixedActionsHtml(row, kind) : '',
    })
  }
  const groupCount = buckets.group.length
  const groupCard =
    `<div class="${QR_CARD_CLASS}" data-kind="qq_group">` +
    `<div class="${QR_CARD_TITLE_CLASS}">${QR_KIND_LABELS.qq_group}</div>` +
    `<div class="${QR_GAME_NAME_CLASS}">${escapeHtml(buckets.group[0]?.game_name ?? '')}</div>` +
    `<div class="${QR_FIGURE_CLASS}">${qrFigureHtml(buckets.group[0] ?? null)}${
      groupCount > 1 ? qrArrowsHtml() : ''
    }</div>` +
    (groupCount > 1
      ? `<div class="${QR_DOTS_CLASS}">${buckets.group
          .map(
            (_, i) =>
              // `data-index` = 点点直达第 N 张（T4 顺手补）；箭头与点点的处置见 `onClick`。
              `<i class="${QR_DOT_CLASS}${i === 0 ? ` ${QR_DOT_ON_CLASS}` : ''}" data-index="${i}"></i>`,
          )
          .join('')}</div>`
      : '') +
    `<div class="${QR_ZOOM_HOLDER_CLASS}" style="margin-top:4px">${
      buckets.group[0]?.media_url
        ? `<a href="javascript:void(0)" class="${QR_ZOOM_CLASS}" data-id="${buckets.group[0]!.id}">查看大图</a>`
        : ''
    }</div>` +
    `<div class="${QR_ACTIONS_CLASS}">${canManage ? qrGroupActionsHtml(buckets, 0) : ''}</div>` +
    '</div>'
  const cards = [cardOf('qq_bot', buckets.bot), groupCard, cardOf('wechat_mp', buckets.mp)]
  return (
    `${QR_STYLE_HTML}` +
    `<div class="${QR_TEXT_CLASS}">${QR_BANNER_TEXT}</div>` +
    `<div class="${QR_GRID_CLASS}">${cards.join('')}</div>`
  )
}

/** 上传白名单（与后端 `storage.ALLOWED_EXTS` 同口径；前端只是早挡，边界在服务端）。 */
const QR_UPLOAD_EXTS = ['jpg', 'jpeg', 'png']

/** 前端早挡的两句（与后端 `qr_code_views` 的 `IMAGE_*_LIMIT_MSG` 是同一事实的两处字面值，改要同一次）。 */
const QR_TYPE_LIMIT_TEXT = '只支持 jpg / jpeg / png'
const QR_SIZE_LIMIT_TEXT = '图片超过 10 MB'

/** 错误 → 可显示文案（后端业务错误经 `request()` 抛出的 `Error.message` 原样显示）。 */
function qrErrText(error: unknown): string {
  return error instanceof Error && error.message ? error.message : '操作失败，请重试'
}

/**
 * 压缩 + 签票据 + 直传，返回 `media_key`（feedback / myProfile 的现成姿势）。
 * ⚠️ 画质按 `wechat-qr` 档（下限 0.8）：二维码是**给人扫的**，压狠了扫不出来。
 */
async function uploadHomeQrFile(file: File): Promise<string> {
  const ext = (file.name.split('.').pop() ?? '').toLowerCase()
  if (!QR_UPLOAD_EXTS.includes(ext)) throw new Error(QR_TYPE_LIMIT_TEXT)
  if (file.size > MAX_USER_MEDIA_BYTES) throw new Error(QR_SIZE_LIMIT_TEXT)
  const prepared = await compressImage(file, mediaQuality('wechat-qr', file.size))
  const ticket = await issueHomeQrTicket(userMediaTicketExt(file, prepared))
  await putFileToStorage(ticket.upload_url, prepared)
  return ticket.key
}

/** 横幅运行期的依赖（页面传入真 layui `layer`；测试传替身——判据全打在真 DOM 与记录到的调用上）。 */
export type QrBannerDeps = {
  layer: Record<string, unknown>
  /** 「添加游戏群码」弹窗的游戏下拉（页面用已取好的全量游戏档案，不再多发一发）。 */
  games: readonly { id: number; name: string }[]
  onToast: (text: string) => void
}

/** 横幅句柄：`stop()` 清定时器 + 摘监听 + 摘节点（页面卸载函数调用）。 */
export type QrBannerHandle = { stop: () => void }

/**
 * 挂载二维码横幅：渲染、插到 `parent` 的**第一个孩子**、接事件、起轮播。
 * 变更操作成功后**重取列表重绘**（单一出口）。
 * ⚠️ 「点击切换」与「点击放大」各自有目标：群码格点图 = 切下一张、机器人/公众号点图 = 放大，
 * 「查看大图」链三格都有。群码格的箭头与点点在 `onClick` 里**先于** figure 分支处置。
 * ⚠️ 管理控件只在 `canNode(me, 'home.qr_manage')` 时渲染（fail-closed；点了会 403 的入口就不画）。
 */
export function mountQrBanner(
  parent: HTMLElement,
  rows: readonly HomeQrRow[],
  me: NodeViewerInput,
  deps: QrBannerDeps,
): QrBannerHandle {
  const canManage = canNode(me, QR_MANAGE_NODE)
  let buckets = qrBuckets(rows)
  let current = 0
  let timerId: number | null = null
  let hovering = false

  const banner = document.createElement('div')
  banner.className = QR_BANNER_CLASS
  parent.insertBefore(banner, parent.firstChild)

  const groupCard = (): HTMLElement | null =>
    banner.querySelector<HTMLElement>(`${QR_CARD_SELECTOR}[data-kind="qq_group"]`)

  /** 把第 `index` 张群码刷成当前张：图、游戏名、点点、操作行（**就地改**，不重排监听）。 */
  const show = (index: number): void => {
    const card = groupCard()
    const count = buckets.group.length
    if (!card || count === 0) return
    current = ((index % count) + count) % count
    const row = buckets.group[current]!
    const figure = card.querySelector<HTMLElement>(QR_FIGURE_SELECTOR)
    // ⚠️ 整块 innerHTML 重写 ⇒ 箭头要**一起**拼回去（T4；箭头没有自己的监听，
    // 点击走 banner 上的委托，重建不丢行为）。单张不渲染箭头（与初绘同一条条件）。
    if (figure) figure.innerHTML = qrFigureHtml(row) + (count > 1 ? qrArrowsHtml() : '')
    const nameEl = card.querySelector<HTMLElement>(QR_GAME_NAME_SELECTOR)
    if (nameEl) nameEl.textContent = row.game_name ?? ''
    const dots = card.querySelectorAll(QR_DOT_SELECTOR)
    dots.forEach((dot, i) => dot.classList.toggle(QR_DOT_ON_CLASS, i === current))
    const zoomHolder = card.querySelector<HTMLElement>(QR_ZOOM_HOLDER_SELECTOR)
    if (zoomHolder) {
      zoomHolder.innerHTML = row.media_url
        ? `<a href="javascript:void(0)" class="${QR_ZOOM_CLASS}" data-id="${row.id}">查看大图</a>`
        : ''
    }
    const actions = card.querySelector<HTMLElement>(QR_ACTIONS_SELECTOR)
    if (actions && canManage) actions.innerHTML = qrGroupActionsHtml(buckets, current)
  }

  const startTimer = (): void => {
    if (timerId !== null || hovering || buckets.group.length < 2) return
    timerId = window.setInterval(() => {
      show(current + 1)
    }, QR_CAROUSEL_INTERVAL_MS)
  }
  const stopTimer = (): void => {
    if (timerId !== null) {
      window.clearInterval(timerId)
      timerId = null
    }
  }

  const paint = (): void => {
    banner.innerHTML = qrBannerHtml(
      [...(buckets.bot ? [buckets.bot] : []), ...buckets.group, ...(buckets.mp ? [buckets.mp] : [])],
      canManage,
    )
  }

  /** 变更成功后的唯一收口：重取、重绘、重绑悬停、重起轮播。失败只说一句话（横幅保持原样）。 */
  const reload = async (showIndex = 0): Promise<void> => {
    try {
      buckets = qrBuckets((await fetchHomeQrCodes()).items)
    } catch (error) {
      deps.onToast(qrErrText(error))
      return
    }
    stopTimer()
    current = 0
    paint()
    bindHover()
    show(showIndex)
    startTimer()
  }

  const openZoom = (row: HomeQrRow | null | undefined): void => {
    if (!row) return
    if (!row.media_url) {
      deps.onToast('图片链接暂时取不到，请刷新重试')
      return
    }
    const title = row.game_name
      ? `${QR_KIND_LABELS[row.kind]}二维码：${row.game_name}`
      : `${QR_KIND_LABELS[row.kind]}二维码`
    ;(deps.layer.open as (o: unknown) => void)({
      type: 1,
      // ⚠️ layer 的标题会进 HTML 串 ⇒ 动态部分必须转义（与页内 openNotice 同一条纪律）。
      title: escapeHtml(title),
      skin: 'layui-layer-rim',
      shadeClose: true,
      content:
        '<div style="text-align:center;padding:10px">' +
        `<img src="${escapeHtml(row.media_url)}" style="max-width:100%;max-height:70vh" alt="二维码大图"></div>`,
    })
  }

  /** 「换图」：临时 `<input type="file">`（myProfile 同款：**插进文档再 click**，用完即摘）。 */
  const replaceImage = (id: number): void => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = 'image/jpeg,image/png'
    input.className = QR_FILE_INPUT_CLASS
    input.style.display = 'none'
    document.body.appendChild(input)
    input.addEventListener('change', () => {
      const file = input.files?.[0]
      input.remove()
      if (!file) return
      void (async () => {
        try {
          const mediaKey = await uploadHomeQrFile(file)
          await updateHomeQrCode(id, { media_key: mediaKey })
          deps.onToast('已更换')
          await reload(current)
        } catch (error) {
          deps.onToast(qrErrText(error))
        }
      })()
    })
    input.click()
  }

  /** 「添加」弹窗（弹窗形态照 `system-systemnotice.ts` 的 layer.open type 1 + btn）。 */
  const openAddDialog = (kind: HomeQrKind): void => {
    if (kind === 'qq_group' && deps.games.length === 0) {
      deps.onToast('还没有游戏档案，请先添加游戏')
      return
    }
    const needGame = kind === 'qq_group'
    const options = deps.games
      .map((game) => `<option value="${game.id}">${escapeHtml(game.name)}</option>`)
      .join('')
    const formHtml =
      `<div id="${QR_FORM_ID}" style="padding:12px 16px">` +
      (needGame
        ? '<div class="layui-form-item">' +
          '<label style="margin-right:8px">游戏</label>' +
          `<select class="${QR_GAME_SELECT_CLASS}" style="height:30px;min-width:200px;border:1px solid #d2d2d2;border-radius:2px">${options}</select>` +
          '</div>'
        : '') +
      '<div class="layui-form-item">' +
      '<label style="margin-right:8px">图片</label>' +
      `<input type="file" class="${QR_FORM_FILE_CLASS}" accept="image/jpeg,image/png">` +
      '</div></div>'
    ;(deps.layer.open as (o: unknown) => number)({
      type: 1,
      title: needGame ? '添加游戏群码' : `添加${QR_KIND_LABELS[kind]}二维码`,
      area: ['460px', needGame ? '240px' : '190px'],
      shadeClose: false,
      content: formHtml,
      btn: ['保存', '取消'],
      yes: (layerIndex: number) => {
        void submitAdd(layerIndex, kind)
      },
    })
  }

  const submitAdd = async (layerIndex: number, kind: HomeQrKind): Promise<void> => {
    // 弹窗内容被 layer 插进 `.layui-layer-content`（document.body 下）⇒ document 级查询。
    const select = document.querySelector<HTMLSelectElement>(
      `${QR_FORM_SELECTOR} ${QR_GAME_SELECT_SELECTOR}`,
    )
    const fileInput = document.querySelector<HTMLInputElement>(
      `${QR_FORM_SELECTOR} ${QR_FORM_FILE_SELECTOR}`,
    )
    let gameId: number | undefined
    if (kind === 'qq_group') {
      gameId = Number(select?.value ?? '')
      if (!select?.value || Number.isNaN(gameId)) {
        deps.onToast('请选择游戏')
        return
      }
    }
    const file = fileInput?.files?.[0]
    if (!file) {
      deps.onToast('请选择二维码图片')
      return
    }
    try {
      const mediaKey = await uploadHomeQrFile(file)
      await createHomeQrCode({
        kind,
        ...(kind === 'qq_group' ? { game_id: gameId } : {}),
        media_key: mediaKey,
      })
      ;(deps.layer.close as (i: number) => void)(layerIndex)
      deps.onToast('已添加')
      await reload()
    } catch (error) {
      deps.onToast(qrErrText(error))
    }
  }

  /** 排序：把 `id` 前移 / 后移一位，然后**全量重排号**（PATCH 有变化的行）+ 重取重绘。 */
  const reorder = async (id: number, delta: number): Promise<void> => {
    const from = buckets.group.findIndex((row) => row.id === id)
    const to = from + delta
    if (from < 0 || to < 0 || to >= buckets.group.length) return
    const order = [...buckets.group]
    const moved = order.splice(from, 1)[0]!
    order.splice(to, 0, moved)
    try {
      const jobs = order
        .map((row, index) => (row.sort_order === index ? null : updateHomeQrCode(row.id, { sort_order: index })))
        .filter((job): job is Promise<HomeQrRow> => job !== null)
      await Promise.all(jobs)
      await reload(to)
    } catch (error) {
      deps.onToast(qrErrText(error))
    }
  }

  /** 全部行（三格拼平；换张 / 放大 / 启停都按 id 在这里找行）。 */
  const findRow = (id: number): HomeQrRow | null =>
    [...(buckets.bot ? [buckets.bot] : []), ...buckets.group, ...(buckets.mp ? [buckets.mp] : [])].find(
      (row) => row.id === id,
    ) ?? null

  const handleAct = (button: Element): void => {
    const act = button.getAttribute('data-act')
    const rawId = button.getAttribute('data-id')
    const id = rawId === null ? Number.NaN : Number(rawId)
    if (!canManage) return
    if (act === 'add') {
      const kind = button.getAttribute('data-kind')
      if (kind === 'qq_bot' || kind === 'qq_group' || kind === 'wechat_mp') openAddDialog(kind)
      return
    }
    if (act === 'replace') {
      if (!Number.isNaN(id)) replaceImage(id)
      return
    }
    if (act === 'toggle') {
      const row = findRow(id)
      if (!row) return
      void (async () => {
        try {
          await updateHomeQrCode(id, { is_enabled: !row.is_enabled })
          deps.onToast(row.is_enabled ? '已停用' : '已启用')
          await reload()
        } catch (error) {
          deps.onToast(qrErrText(error))
        }
      })()
      return
    }
    if (act === 'delete') {
      ;(deps.layer.confirm as (text: string, options: unknown, yes: (index: number) => void) => void)(
        '删除后不可恢复，确定删除这张二维码吗？',
        { title: '删除二维码' },
        (index: number) => {
          void (async () => {
            try {
              await deleteHomeQrCode(id)
              ;(deps.layer.close as (i: number) => void)(index)
              deps.onToast('已删除')
              await reload()
            } catch (error) {
              deps.onToast(qrErrText(error))
            }
          })()
        },
      )
      return
    }
    if (act === 'move-up') void reorder(id, -1)
    if (act === 'move-down') void reorder(id, 1)
  }

  const onClick = (event: Event): void => {
    const target = event.target as Element | null
    if (!target || typeof target.closest !== 'function') return
    // 轮播箭头与点点都长在群码格 figure **之内** ⇒ 必须先于 figure 分支判：否则点箭头会被
    // 下面的「点图 = 切下一张」吃掉——效果上右箭头碰巧一样，**左箭头会错成下一张**。
    if (target.closest(QR_PREV_SELECTOR)) {
      show(current - 1)
      return
    }
    if (target.closest(QR_NEXT_SELECTOR)) {
      show(current + 1)
      return
    }
    const dot = target.closest(QR_DOT_SELECTOR)
    if (dot) {
      // 点点直达第 N 张（T4 顺手补）。`data-index` 是自己渲染的，缺席/非数一律不动。
      const raw = dot.getAttribute('data-index')
      if (raw !== null) {
        const index = Number(raw)
        if (!Number.isNaN(index)) show(index)
      }
      return
    }
    const figure = target.closest(QR_FIGURE_SELECTOR)
    if (figure) {
      // 群码格：点图 = 切下一张（业务方点名；只有一张时切了也是同一张）。
      if (figure.closest(`${QR_CARD_SELECTOR}[data-kind="qq_group"]`)) {
        show(current + 1)
        return
      }
      // 机器人 / 公众号：点图 = 放大（「查看大图」链上带着行 id ⇒ 从那里取行；没有链 = 没图可放大）。
      const idText = figure.closest(QR_CARD_SELECTOR)?.querySelector(QR_ZOOM_SELECTOR)?.getAttribute('data-id')
      if (idText !== null && idText !== undefined) openZoom(findRow(Number(idText)))
      return
    }
    const zoomLink = target.closest(QR_ZOOM_SELECTOR)
    if (zoomLink) {
      const idText = zoomLink.getAttribute('data-id')
      if (idText !== null) openZoom(findRow(Number(idText)))
      return
    }
    const button = target.closest('[data-act]')
    if (button) handleAct(button)
  }

  const onEnter = (): void => {
    hovering = true
    stopTimer()
  }
  const onLeave = (): void => {
    hovering = false
    startTimer()
  }

  /** 悬停暂停（群码格的 figure；`paint()` 会换掉节点 ⇒ 每次 paint 后重绑）。 */
  const bindHover = (): void => {
    const figure = groupCard()?.querySelector<HTMLElement>(QR_FIGURE_SELECTOR)
    if (figure) {
      figure.addEventListener('mouseenter', onEnter)
      figure.addEventListener('mouseleave', onLeave)
    }
  }

  banner.addEventListener('click', onClick)
  paint()
  bindHover()
  startTimer()

  return {
    stop: (): void => {
      stopTimer()
      banner.removeEventListener('click', onClick)
      banner.remove()
    },
  }
}

/**
 * 挂载后台首页。返回卸载函数（摘掉两个委托 + 清空 root）。
 * 各发请求**并行且各自独立 catch**——一次装载不该因为一个卡片报错就整页失败
 * （失败项回落成 `—` / 空列表，页面骨架照常渲染）。
 */
export async function mount(root: HTMLElement, ctx: IslandContext): Promise<() => void> {
  // 1) 文档标题（页签文案由壳的 `meta.title` 给，这里是文档自己的 title）。
  document.title = PAGE_TITLE

  // 2) 先挂样式（`layui.css` 是 `bootstrapIsland` 运行期插的）——顺序反了会闪没样式的骨架；
  //    数据概览的日期框还要 `laydate`。顺序纪律：先 bootstrapIsland 挂样式、再塞标记、最后 initIslandElements。
  const layui = await bootstrapIsland(['form', 'layer', 'laydate'])
  const layer: Record<string, unknown> = layui.layer

  // 3) 在**惰性文档**里解析 + 裁剪：那 3 个二维码盒的跨站 <img> 从不进入活文档。
  const inert = document.implementation.createHTMLDocument('')
  inert.body.innerHTML = await fetchArtifactHtml(ARTIFACT_URL)
  pruneExcluded(inert.body)
  root.innerHTML = inert.body.innerHTML

  // 4) 克隆 `#template`（`<template>` 的内容不参与渲染）。
  if (!renderTemplate(root, layui.laytpl as LayuiModule)) {
    throw new Error('证据件缺少 #template / #root，无法渲染后台首页')
  }

  // 5) 绑定页内结构组件（页签/折叠面板）——**必须**在标记进来之后。
  initIslandElements(layui)

  // 6) 先**清空**快捷操作行：证据件里那一排是写死的管理员模板，权限不同的人会先看到不属于自己的格
  //    （清空只持续几毫秒，比「先闪错的格」好）。
  const shortcutRow = root.querySelector<HTMLElement>(SHORTCUT_LIST_SELECTOR)
  if (shortcutRow) shortcutRow.innerHTML = ''

  // 7) 五发请求并行，各自独立回落（横幅一发失败回落 null ⇒ 整块按「全空」渲染占位）。
  const [me, games, notices, systemNotices, qrPayload] = await Promise.all([
    fetchMe().catch((): User | null => null),
    fetchAllGames().catch((): ConfigRow[] => []),
    fetchNotifications({ page: 1, page_size: NOTICE_PAGE_SIZE }).catch(() => null),
    fetchSystemNotices({ page: 1, page_size: NOTICE_PAGE_SIZE }).catch(() => null),
    fetchHomeQrCodes().catch((): HomeQrListPayload | null => null),
  ])

  // 8) 按权限渲染快捷操作行。⛔ 必须在 `markPlaceholders` 之前：新渲染的「接单排行榜」才吃得到
  //    `data-notopen`，全页 12 这个数才成立。
  renderShortcuts(shortcutRow, me ?? {})

  // 9) 给占位控件打标（此时快捷操作行已是按权限渲染的产物）。
  markPlaceholders(root)

  // 10) 回填「我的信息」三格（「我接手的订单」与两个登录信息格是占位，不动）。
  fillCell(infoCell(root, '本系统登录账号'), me?.username ?? '—', 'span.info.ft14')
  fillCell(infoCell(root, '我的职位'), formatRoles(me ?? {}))
  fillCell(infoCell(root, '游戏权限'), formatGameScope(games, me?.game_ids ?? []))

  /**
   * 11) **数据概览**（逐条是判据，别调换）：
   * ① **日期框默认值 = 今天**（⛔ 不是证据件里那个抓取当天的字符串）；
   * ② **「今天」点亮、另三颗摘掉**——证据件静态点亮「昨天」是抓取当天的服务端状态，
   *    与「默认值 = 今天」自相矛盾 ⇒ 以默认窗口为准；
   * ③ **非管理员：一发请求都不发**（12 格保持证据件静态 `0`/`0%`），但 4 颗按钮与日期框带
   *    `data-overview-locked`、日期框补 `readonly`——⛔ 不留「亮着、能打字、点了没反应」的假控件；
   *    窗口可见态（今天点亮 + 框里填今天）保留，由 spec 钉住；
   * ④ 管理员：接 4 颗按钮 + `laydate` 区间选择器，并按**今天**先取一发。
   */
  const dateInput = root.querySelector<HTMLInputElement>(`#${OVERVIEW_DATE_INPUT_ID}`)
  const overviewButton = (kind: OverviewRangeKind): HTMLElement | null =>
    root.querySelector<HTMLElement>(`.${kind}`)
  /** 管理员判据**只有这一份**（`/api/auth/me` 的 `is_account_admin`，fail-closed：缺席 = 非管理员）。 */
  const overviewVisible = me?.is_account_admin === true
  const todayWindow = overviewWindow('today')
  if (dateInput) dateInput.value = rangeValue(todayWindow.start, todayWindow.end)

  /** 4 颗按钮的选中态：点亮 `kind`、其余三颗摘掉（`kind = null` ⇒ 全摘）。 */
  const markActiveRange = (kind: OverviewRangeKind | null): void => {
    for (const candidate of OVERVIEW_RANGE_KINDS) {
      overviewButton(candidate)?.classList.toggle(OVERVIEW_ACTIVE_CLASS, candidate === kind)
    }
  }
  markActiveRange('today')

  /** 取一发并回填 12 格。失败**只回落**（保留静态 0/0%），不弹层、不打断整页。 */
  const loadOverview = async (range: { start: string; end: string }): Promise<void> => {
    if (!overviewVisible) return
    try {
      fillOverview(root, await fetchHomeOverview(range))
    } catch {
      // 独立 catch：本卡片算不出来不影响页面其余部分。
    }
  }

  if (overviewVisible) {
    const laydate = layui.laydate as LayuiModule | undefined
    const renderDate = laydate?.render as ((options: unknown) => void) | undefined
    if (!renderDate) {
      // ⚠️ 拿不到 `laydate` 时日期框会退化成「点了没反应」的静默假控件 ⇒ 宁可响亮提示。
      //    该模块在 vendored layui 的 `builtin` 表里 ⇒ 这一支是兜底、不是常态。
      islandMsg(layer, '日期选择器未能加载，请刷新页面重试')
    }
    renderDate?.({
      elem: `#${OVERVIEW_DATE_INPUT_ID}`,
      // ⚠️ **不加** `min` / `max`：对标那对值是抓取当天算出来的三个月窗口，照抄等于凭空限死可选范围。
      range: '~',
      // 默认值 = 今天（与上面 ① 同一个值）。
      value: rangeValue(todayWindow.start, todayWindow.end),
      done: (value: unknown): void => {
        const picked = parseRangeValue(String(value ?? ''))
        if (!picked) return
        // 自选区间 ⇒ 4 颗按钮的选中态全部摘掉（窗口不再是某个档位，留着「今天」点亮会说假话）。
        markActiveRange(null)
        if (dateInput) dateInput.value = rangeValue(picked.start, picked.end)
        void loadOverview(picked)
      },
    })
    // 装载即取一发（窗口 = 今天）。
    await loadOverview(todayWindow)
  } else {
    // ── 非管理员：控件不接线，但也不留「看着能用」的假控件——打 `data-overview-locked`
    //    （点击提示**权限**原因）+ 日期框补 `readonly`（⛔ 不让往框里打字却什么都不会发生）。
    //    ⚠️ 12 格保持静态 `0` / `0%`；窗口可见态（今天点亮 + 框里填今天）**保留**：
    //    它说明这张卡算的是今天的口径，与「你没有权限看数据」不矛盾（spec 钉住）。
    for (const candidate of OVERVIEW_RANGE_KINDS) {
      overviewButton(candidate)?.setAttribute(OVERVIEW_LOCKED_ATTR, OVERVIEW_LOCKED_REASON)
    }
    if (dateInput) {
      dateInput.setAttribute(OVERVIEW_LOCKED_ATTR, OVERVIEW_LOCKED_REASON)
      dateInput.setAttribute('readonly', 'readonly')
    }
  }

  // 12) 通知和公告：卡片标题用我们的工作室名（证据件里是 PII 占位符 `—`）；
  //     公告排前、站内通知接后，共 `NOTICE_PAGE_SIZE`(3) 条。
  const noticeList: HomeNotice[] = [
    ...(systemNotices?.items ?? []).map(
      (notice): HomeNotice => ({ source: 'system', notice }),
    ),
    ...(notices?.items ?? []).map(
      (notice): HomeNotice => ({ source: 'notification', notice }),
    ),
  ].slice(0, NOTICE_PAGE_SIZE)
  const noticeCard = root.querySelector(NOTICE_LIST_SELECTOR)?.closest('.layui-card') ?? null
  const noticeHeader = noticeCard?.querySelector(':scope > .layui-card-header') ?? null
  if (noticeHeader) noticeHeader.textContent = `【${me?.tenant_name ?? ''}】通知和公告`
  // 「重要提示…」那一行的租户名同样是 PII 占位符——用 `textContent` 填（不是 HTML 插值），只换那一处。
  const declaration = root.querySelector<HTMLElement>(DECLARATION_SELECTOR)
  if (declaration) {
    declaration.textContent = (declaration.textContent ?? '').replace(
      PII_PLACEHOLDER,
      me?.tenant_name ?? '',
    )
  }
  const noticeUl = root.querySelector<HTMLElement>(NOTICE_LIST_SELECTOR)
  if (noticeUl) noticeUl.innerHTML = noticeList.map(homeNoticeItemHtml).join('')

  // 12½) 首页二维码横幅（自造区，D-0262）：渲染后插在页面容器**顶部** ⇒ 现有卡片整体下移；
  //      管理控件按 `canNode(me, 'home.qr_manage')` 决定画不画（fail-closed）。
  const qrBanner = mountQrBanner(root, qrPayload?.items ?? [], me ?? {}, {
    layer,
    games,
    onToast: (text) => {
      islandMsg(layer, text)
    },
  })

  /** 当前弹层对应的订单号（弹层内容由第三方渲染，链接上没有可读的 data-* ⇒ 开层时记下来）。 */
  let popupOrderNo: string | null = null

  /**
   * 开通知/公告弹层。⚠️ 站内通知是 4 行 + 「查看订单」链接；系统公告显示**全文**（没有订单链接）。
   */
  const openNotice = (id: string | null): void => {
    const item = noticeList.find((n) => homeNoticeKey(n) === id)
    if (!item) return
    const notification = item.source === 'notification' ? item.notice : null
    popupOrderNo = notification?.order_no ?? null
    const orderLink =
      notification?.order_no
        ? `<br/><a href="javascript:void(0)" class="${ORDER_LINK_CLASS}" style="color:#1E9FFF">查看订单</a>`
        : ''
    ;(layer.open as (o: unknown) => void)({
      // ⚠️ 标题会进 HTML 串 ⇒ 动态部分必须转义。
      title: `内部公告：${escapeHtml(homeNoticeText(item).slice(0, LAYER_TITLE_MAX))}...`,
      type: 1,
      skin: 'layui-layer-rim',
      area: ['50%', '60%'],
      content:
        '<div style="margin:10px 10px 10px 10px;font-size: 16px;padding: 10px 10px;' +
        `color: #666;line-height: 30px;">${homeNoticeBody(item)}${orderLink}</div>`,
    })
  }

  /**
   * 13) 捕获阶段的点击委托，**判据只有一份**（同时挂 root 与 document：弹层节点在 `document.body`
   * 下、root 的捕获监听不在其事件路径上）。
   * 匹配顺序 = 通知条目 → 查看订单 → 快捷操作 → 数据概览按钮 → 占位控件
   * （⚠️ 占位必须放最后：`data-notopen` 会把「接单排行榜」这类同时带快捷语义的控件一并吃掉）。
   */
  const onClick = (event: Event): void => {
    const target = event.target as Element | null
    if (!target || typeof target.closest !== 'function') return

    const noticeLink = target.closest(NOTICE_LINK_SELECTOR)
    if (noticeLink) {
      openNotice(noticeLink.getAttribute('data-id'))
      event.preventDefault()
      event.stopPropagation()
      return
    }

    const orderLink = target.closest(ORDER_LINK_SELECTOR)
    if (orderLink) {
      if (popupOrderNo) ctx.onNavigate('order-detail', { order_no: popupOrderNo })
      event.preventDefault()
      event.stopPropagation()
      return
    }

    const shortcut = target.closest('[lay-href]')
    // ⚠️ 只认**快捷操作那一排里**的锚点：「我的信息」格里 `layui-hide` 的「实名认证」锚点
    //    也带同款 `lay-href`，少了 `closest(SHORTCUT_ROOT_SELECTOR)` 会被误当快捷格导航。
    if (shortcut && shortcut.closest(SHORTCUT_ROOT_SELECTOR)) {
      const href = shortcut.getAttribute('lay-href')
      // `SHORTCUT_ROUTES` 由条目表 `SHORTCUTS` 派生 ⇒ 增删条目只改条目表一处。
      const hit = SHORTCUT_ROUTES.find((r) => r.href === href)
      if (hit) {
        ctx.onNavigate(hit.routeName)
        // 必须停：同一个 `onClick` 还挂在 document 上，不停就会导航两次。
        event.preventDefault()
        event.stopPropagation()
        return
      }
    }

    // 数据概览那 4 颗时间按钮。⚠️ 放在占位分支**之前**：它们是真控件，优先按真控件处置
    // （万一将来被打了 `data-notopen` 也仍然走这一支）。
    // ⚠️ 非管理员**不接**：`overviewVisible` 为假 ⇒ 落到下面的「锁住」分支（提示权限原因，⛔ 不发请求）。
    const rangeButton = target.closest(OVERVIEW_BUTTON_SELECTOR)
    if (rangeButton && overviewVisible) {
      const kind = OVERVIEW_RANGE_KINDS.find((candidate) =>
        rangeButton.classList.contains(candidate),
      )
      if (kind) {
        markActiveRange(kind)
        const picked = overviewWindow(kind)
        // 点档位按钮 ⇒ 把区间框的值同步成这一段（日期框与 4 颗按钮是同一个窗口的两种入口）。
        if (dateInput) dateInput.value = rangeValue(picked.start, picked.end)
        void loadOverview(picked)
        event.preventDefault()
        event.stopPropagation()
        return
      }
    }

    // 非管理员的「数据概览」控件：说清是**权限**问题（⛔ 不说「尚未开放」，那是另一件事）。
    const locked = target.closest(`[${OVERVIEW_LOCKED_ATTR}]`)
    if (locked) {
      const reason = locked.getAttribute(OVERVIEW_LOCKED_ATTR) || OVERVIEW_LOCKED_REASON
      islandMsg(layer, reason)
      event.preventDefault()
      event.stopPropagation()
      return
    }

    const placeholder = target.closest(NOTOPEN_SELECTOR)
    if (placeholder) {
      const label = placeholder.getAttribute(NOTOPEN_ATTR) || DEFAULT_PLACEHOLDER_LABEL
      islandMsg(layer, `「${label}」${MORE_LABEL}`)
      event.preventDefault()
      event.stopPropagation()
    }
  }

  root.addEventListener('click', onClick, true)
  // 弹层在 `document.body` 下（root 之外），root 的捕获监听收不到里面的「查看订单」。
  document.addEventListener('click', onClick, true)

  // 14) 返回卸载函数（先收横幅的定时器与监听，再摘两个委托 + 清空 root）。
  return () => {
    qrBanner.stop()
    root.removeEventListener('click', onClick, true)
    document.removeEventListener('click', onClick, true)
    root.innerHTML = ''
  }
}

export default { mount } satisfies IslandPage
