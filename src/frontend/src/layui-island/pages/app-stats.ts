/**
 * 【APP 与小程序 → 数据概览】island 页（island key = `app-stats`，
 * 路由 `/business/app-stats`，菜单 label「数据概览」）。
 *
 * 计划：`docs/plans/2026-10-23-app-mp-stats.md` §7（业务方口径）/ §8（评审改准的工程做法）。
 * 端点与类型在 `@/api/appStats`。
 *
 * ## 这一页是**自有功能**，不是复刻
 *
 * 对标物（鸡蛋铺）没有「APP 与小程序」这一组 ⇒ 没有证据件、不走 `artifactPage` /
 * `mountConfigLike` 那条复刻链路。标记由本文件自己写，风格照同类的**自建** island 页
 * （`xgj-receipts.ts` / `feedback.ts`）。⛔ 不用 Element Plus。
 *
 * ## 四条硬口径（都有交互用例钉着）
 *
 * 1. **微信块失败 / 未开通 ⇒ 显示「暂不可用」+ 原因，⛔ 不渲染任何数字**（不拿 0 冒充）。
 * 2. **微信是 T+1**：它的 7 天止于昨天；页面在标题旁写明，别让人以为「今天没数据」。
 * 3. **注册 / 接单 / 押金是自家数据**：窗口含今天（后端给的 `date_from/date_to`）。
 * 4. **权限门 fail-closed**：拿不到节点 ⇒ 一句提示、**不注入标记、不发业务请求**
 *    （`fetchMe()` 那一次身份请求照发——它就是这道门）。
 *
 * ## 三条 island 陷阱（`AGENTS.md` §5-G）
 *
 * ① 类名与选择器分成两个常量；
 * ② 以 `(` 开头的语句一律前置分号；
 * ③ 顺序：`await bootstrapIsland(...)`（先挂样式）→ 塞标记 → `initIslandElements(layui)`。
 * ⚠️ 本页**没有定时器**、也不依赖 layui 的 `table` 模块：四块都是静态 `<table>`，
 * 数据一换就整块重绘（判据简单、jsdom 里也能驱动）。
 */
import { fetchMe } from '@/api/auth'
import {
  APP_STATS_NODE,
  fetchAppStatsOverview,
  type AppStatsOverview,
  type ClaimBlock,
  type DepositBlock,
  type RegistrationBlock,
  type WechatBlock,
} from '@/api/appStats'
import { escapeHtml } from '@/layui-island/escape'
import { canNode } from '@/layui-island/perms'
import { bootstrapIsland, initIslandElements, islandMsg, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

/** island key（= 文件名 = 路由 `meta.islandPage`，三处必须逐字一致）。 */
export const PAGE_KEY = 'app-stats'
export const TITLE = '数据概览'

/** 容器**类名**（拼进标记）与**选择器**（查节点）——两者不能混（陷阱 ①）。 */
export const PANEL_CLASS = 'appStatsPanel'
export const PANEL_SELECTOR = `.${PANEL_CLASS}`

/** 四块内容区的 id。 */
export const WECHAT_ID = 'appStatsWechat'
export const REGISTRATION_ID = 'appStatsRegistration'
export const CLAIM_ID = 'appStatsClaim'
export const DEPOSIT_ID = 'appStatsDeposit'
export const ERROR_ID = 'appStatsError'

/** 时间范围按钮（`data-days` 是唯一参数来源）。 */
export const RANGE_BUTTON_CLASS = 'appStatsRange'
export const RANGE_BUTTON_SELECTOR = `.${RANGE_BUTTON_CLASS}`

/** 无节点时的提示。 */
export const NO_ACCESS_MESSAGE = '无权限查看 APP 与小程序数据概览'

/** 页内文案（抽成常量只为让 spec 钉住字面值）。 */
export const MESSAGES = {
  noAccess: NO_ACCESS_MESSAGE,
  failed: '加载失败',
  loading: '加载中…',
  unavailable: '暂不可用',
  wechatT1: '小程序数据由微信提供，T+1（只到昨天）',
  historyNote: '历史未区分',
  emptyReason: '未记录渠道',
} as const

/** 时间范围两档（业务方给的默认 7、上限 30）。 */
export const RANGE_DAYS = [7, 30] as const

/** 金额/数字上屏的兜底：`null` 不当 0（后端契约里不该出现，但防御只做一处）。 */
function text(value: unknown): string {
  return value === null || value === undefined ? '—' : escapeHtml(String(value))
}

/** 微信块：失败 ⇒ **只有一句「暂不可用：原因」**，⛔ 不画表、不写 0。 */
export function wechatHtml(block: WechatBlock): string {
  if (!block.available) {
    return (
      `<div class="appStatsUnavailable" style="color:#999;padding:6px 0">` +
      `${MESSAGES.unavailable}：${escapeHtml(block.reason)}</div>`
    )
  }
  const totals = block.totals
  const summary =
    totals === null
      ? ''
      : `<div style="color:#666;margin:0 0 6px">` +
        `近 ${block.series.length} 天日值相加：打开 ${text(totals.session_cnt)} 次 / ` +
        `访问页面 ${text(totals.visit_pv)} 次 / 访问人数 ${text(totals.visit_uv)} 人 / ` +
        `新增 ${text(totals.visit_uv_new)} 人（人数是当日去重，跨天相加会重复计同一个人）</div>`
  const rows = block.series
    .map(
      (day) =>
        `<tr><td>${escapeHtml(day.date)}</td><td>${text(day.session_cnt)}</td>` +
        `<td>${text(day.visit_pv)}</td><td>${text(day.visit_uv)}</td>` +
        `<td>${text(day.visit_uv_new)}</td></tr>`,
    )
    .join('')
  return (
    `${summary}<table class="layui-table" style="margin:0"><thead><tr>` +
    `<th>日期</th><th>打开次数</th><th>访问页面次数</th><th>访问人数</th><th>新增用户</th>` +
    `</tr></thead><tbody>${rows}</tbody></table>`
  )
}

/** 注册块：三桶 + 合计（数字由后端按日聚合）。 */
export function registrationHtml(block: RegistrationBlock): string {
  const rows = block.series
    .map(
      (day) =>
        `<tr><td>${escapeHtml(day.date)}</td><td>${text(day.mp_weixin)}</td>` +
        `<td>${text(day.app_android)}</td><td>${text(day.unknown)}</td>` +
        `<td>${text(day.total)}</td></tr>`,
    )
    .join('')
  const total = block.totals
  return (
    `<div style="color:#666;margin:0 0 6px">` +
    `合计：小程序 ${text(total.mp_weixin)} / App ${text(total.app_android)} / ` +
    `未识别 ${text(total.unknown)}（共 ${text(total.total)} 人）</div>` +
    `<div style="color:#999;margin:0 0 6px">${MESSAGES.historyNote}：${escapeHtml(block.note)}</div>` +
    `<table class="layui-table" style="margin:0"><thead><tr>` +
    `<th>日期</th><th>小程序</th><th>App</th><th>未识别</th><th>合计</th>` +
    `</tr></thead><tbody>${rows}</tbody></table>`
  )
}

/** 接单块：移动端 / 合计 + 每日渠道明细。 */
export function claimHtml(block: ClaimBlock): string {
  const rows = block.series
    .map((day) => {
      const detail =
        Object.keys(day.by_reason).length === 0
          ? '—'
          : Object.entries(day.by_reason)
              .map(([reason, count]) => `${escapeHtml(reason)} ${text(count)}`)
              .join('、')
      return (
        `<tr><td>${escapeHtml(day.date)}</td><td>${text(day.mobile)}</td>` +
        `<td>${text(day.total)}</td><td>${detail}</td></tr>`
      )
    })
    .join('')
  const total = block.totals
  return (
    `<div style="color:#666;margin:0 0 6px">` +
    `合计：移动端 ${text(total.mobile)} 单 / 全部 ${text(total.total)} 单` +
    `（移动端 = ${block.mobile_channels.map((c) => escapeHtml(c)).join('、')}）</div>` +
    `<table class="layui-table" style="margin:0"><thead><tr>` +
    `<th>日期</th><th>移动端接单</th><th>接单合计</th><th>渠道明细</th>` +
    `</tr></thead><tbody>${rows}</tbody></table>`
  )
}

/** 押金块：当前冻结总额 + 人数 + 每日变动 + 如实口径。 */
export function depositHtml(block: DepositBlock): string {
  const rows = block.series
    .map(
      (day) =>
        `<tr><td>${escapeHtml(day.date)}</td><td>${text(day.delta)}</td></tr>`,
    )
    .join('')
  return (
    `<div style="color:#666;margin:0 0 6px">` +
    `当前冻结总额 ${text(block.total_frozen)} 元，涉及 ${text(block.taker_count)} 人。` +
    `<span style="color:#999">${escapeHtml(block.note)}</span></div>` +
    `<table class="layui-table" style="margin:0"><thead><tr>` +
    `<th>日期</th><th>冻结额变动（元）</th>` +
    `</tr></thead><tbody>${rows}</tbody></table>`
  )
}

/** 页面静态外壳（四张卡 + 范围按钮）；数据由 `render()` 逐块填。 */
export function buildShell(): string {
  const buttons = RANGE_DAYS.map(
    (days) =>
      `<button class="layui-btn layui-btn-sm ${RANGE_BUTTON_CLASS}" data-days="${days}">近 ${days} 天</button>`,
  ).join(' ')
  return `
<div class="${PANEL_CLASS}" style="padding:10px 15px">
  <div style="margin:8px 0">${buttons}
    <span style="color:#999;margin-left:10px">${MESSAGES.wechatT1}</span>
  </div>
  <div id="${ERROR_ID}" style="color:#c00;margin:4px 0"></div>
  <div class="layui-card"><div class="layui-card-header">小程序访问</div>
    <div class="layui-card-body" id="${WECHAT_ID}"></div></div>
  <div class="layui-card"><div class="layui-card-header">注册数（App / 小程序）</div>
    <div class="layui-card-body" id="${REGISTRATION_ID}"></div></div>
  <div class="layui-card"><div class="layui-card-header">每日接单（移动端 / 全部）</div>
    <div class="layui-card-body" id="${CLAIM_ID}"></div></div>
  <div class="layui-card"><div class="layui-card-header">押金冻结</div>
    <div class="layui-card-body" id="${DEPOSIT_ID}"></div></div>
</div>`
}

export default {
  mount: async (root: HTMLElement, _ctx): Promise<() => void> => {
    document.title = TITLE
    // ⚠️ 顺序是硬要求（陷阱 ③）：先挂样式，再塞标记，最后 `initIslandElements`。
    const layui: LayuiNamespace = await bootstrapIsland(['layer'])
    const layer = layui.layer as LayuiModule
    const msg = (value: string): void =>
      islandMsg(layer as unknown as Record<string, unknown>, value)

    // ⚠️ 门禁必须排在注入标记**之前**（理由见文件头「权限门」段）。
    const me = await fetchMe()
    if (!canNode(me, APP_STATS_NODE)) {
      msg(NO_ACCESS_MESSAGE)
      return () => {}
    }

    root.innerHTML = buildShell()
    initIslandElements(layui)

    let days = 7

    const errorNode = (): HTMLElement | null =>
      root.querySelector<HTMLElement>(`#${ERROR_ID}`)
    const part = (id: string): HTMLElement | null =>
      root.querySelector<HTMLElement>(`#${id}`)

    const setActive = (): void => {
      root.querySelectorAll<HTMLElement>(RANGE_BUTTON_SELECTOR).forEach((button) => {
        const active = Number(button.dataset.days) === days
        button.classList.toggle('layui-btn-normal', active)
        button.classList.toggle('layui-btn-primary', !active)
      })
    }

    const render = (data: AppStatsOverview): void => {
      const error = errorNode()
      if (error !== null) error.textContent = ''
      const wechat = part(WECHAT_ID)
      if (wechat !== null) wechat.innerHTML = wechatHtml(data.wechat)
      const registration = part(REGISTRATION_ID)
      if (registration !== null) registration.innerHTML = registrationHtml(data.registrations)
      const claim = part(CLAIM_ID)
      if (claim !== null) claim.innerHTML = claimHtml(data.claims)
      const deposit = part(DEPOSIT_ID)
      if (deposit !== null) deposit.innerHTML = depositHtml(data.deposit)
    }

    const fail = (error: unknown): void => {
      const node = errorNode()
      const message =
        error instanceof Error && error.message !== ''
          ? error.message
          : MESSAGES.failed
      if (node !== null) node.textContent = message
      else msg(message)
    }

    const load = async (): Promise<void> => {
      const node = errorNode()
      if (node !== null) node.textContent = MESSAGES.loading
      try {
        render(await fetchAppStatsOverview(days))
      } catch (error) {
        fail(error)
      }
    }

    root.addEventListener('click', (event) => {
      const target = event.target
      if (!(target instanceof HTMLElement)) return
      const button = target.closest<HTMLElement>(RANGE_BUTTON_SELECTOR)
      if (button === null) return
      const next = Number(button.dataset.days)
      if (!Number.isFinite(next) || next === days) return
      days = next
      setActive()
      void load()
    })

    setActive()
    await load()

    return () => {
      root.innerHTML = ''
    }
  },
} satisfies IslandPage
