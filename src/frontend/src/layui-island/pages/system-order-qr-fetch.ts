/**
 * 【第三方平台】→「马上来查码对接」页（island key = `system-order-qr-fetch`，
 * 路由 `/thirdparty/order-qr-fetch`）。
 *
 * 决策 **D-0348**（2026-10-31 自开发）；后端契约 = `src/apps/business/api/order_qr_config_views.py`
 * + `business/urls.py`；前端契约另一半 = `api/orderQrFetch.ts`。
 *
 * ## 这一页是**自有功能**，不是复刻
 *
 * 对标物（鸡蛋铺）里没有【第三方平台】这一组 ⇒ 没有证据件、不走 `mountConfigLike`。
 * 配方照 `pages/system-wechatmp-settings.ts`（同组最新的自有功能设置页）。island key 登记
 * 在 `config/__tests__/replicaRoutes.spec.ts` 的 `OWN_FEATURE_PAGE_KEYS`。
 *
 * ## 三个区块
 *
 * 1. **401 闩横幅**（只在 `auth_failed_at` 非空时出现）：密钥被对端拒绝过、自动拉取已停；
 *    改密钥（或保存任意配置）即恢复；
 * 2. **配置表单**：启用开关 / 对端基址 / api_key（**留空 = 不改**，只回掩码）/ 保存；
 * 3. **能力说明卡**：触发时机、三态与限速的人话版（⛔ 不写对端真域名——部署者从对端运维页
 *    拿基址与密钥）。
 *
 * ## ⛔ 这一页**没有**的东西（有意，别"顺手补上"）
 *
 * - ⛔ **api_key 原文**：后端只回掩码（本页唯一不回原文的凭据），没有"查看密钥"按钮；
 * - ⛔ **「立即拉一次」按钮**：拉取只由自然触发点驱动（打手/客服点开群码区块、接单成功），
 *   手动触发会破坏防抖语义（那是 D-0348 的口径，不是漏做）；
 * - ⛔ **拉取台账列表**：台账在「操作日志」页（action=`order.malailai_qr_fetch`），本页不重复。
 *
 * ## 四条 island 陷阱（`AGENTS.md` §5-G，改这一页前先读）
 *
 * ① 类名与选择器分成两个常量；② 以 `(` 开头的语句一律前置分号；
 * ③ `await bootstrapIsland(...)`（挂样式）→ `root.innerHTML`（塞标记）→
 *    `initIslandElements(layui)`（按 `lay-*` 初始化）；④ 本页**没有**定时器。
 * ⚠️ 四条都有**运行期**判据（`__tests__/system-order-qr-fetch.spec.ts`）。
 */
import { fetchMe } from '@/api/auth'
import {
  ORDER_QR_FETCH_NODE,
  getOrderQrConfig,
  saveOrderQrConfig,
  type OrderQrConfigRow,
} from '@/api/orderQrFetch'
import { canNode } from '@/layui-island/perms'
import { bootstrapIsland, initIslandElements, islandMsg, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

/** island key（= 文件名 = 路由 `meta.islandPage`，三处必须逐字一致）。 */
export const PAGE_KEY = 'system-order-qr-fetch'
export const TITLE = '马上来查码对接'

// ── 类名与选择器**分成两个常量**（陷阱 ①：`class="${SELECTOR}"` 会造出 `class=".x"`）──────

/** 每一个区块的外层卡片。 */
export const CARD_CLASS = 'orderQrFetchCard'
export const CARD_SELECTOR = `.${CARD_CLASS}`
/** 401 闩横幅（只在闩住时渲染）。 */
export const LATCH_BANNER_CLASS = 'orderQrFetchLatchBanner'
export const LATCH_BANNER_SELECTOR = `.${LATCH_BANNER_CLASS}`

// ── 各节点的 `id` ────────────────────────────────────────────────────────────

export const ENABLED_SWITCH_ID = 'orderQrFetchEnabled'
export const BASE_URL_INPUT_ID = 'orderQrFetchBaseUrl'
export const API_KEY_INPUT_ID = 'orderQrFetchApiKey'
export const MASKED_TEXT_ID = 'orderQrFetchMasked'
export const SAVE_BUTTON_ID = 'orderQrFetchSaveBtn'
export const SAVE_HINT_ID = 'orderQrFetchSaveHint'
export const LATCH_BANNER_TEXT_ID = 'orderQrFetchLatchText'

/** 表单容器（开关要 `form.render('checkbox', filter)` 才变成 layui 开关）。 */
export const FORM_ID = 'orderQrFetchForm'
export const FORM_FILTER = 'orderQrFetchForm'
export const ENABLED_FILTER = 'orderQrFetchEnabledToggle'

/** 无权限 / 非管理员时的提示（与 wechatmp 设置页同款自检口径）。 */
export const NO_ACCESS_MESSAGE = '你没有查看本页的权限'

/** 页内用户可见文案（抽成常量只为让 spec 钉住字面值）。 */
export const MESSAGES = {
  saved: '已保存，配置立即生效',
  failed: '读取配置失败',
  /** 对端把我们的密钥拒了（401 闩，横幅文案；后端同一段语义的人话版）。 */
  latchBanner:
    '查码密钥被对端拒绝过（401），自动拉取已暂停；填入新密钥（或保存任意配置）后自动恢复',
  apiKeyPlaceholder: '留空 = 保持不变（已保存的值只显示掩码）',
  baseUrlPlaceholder: '形如 https://example.example（从对端运维页复制）',
} as const

/** 保存按钮旁那句常驻提示（留空语义，逐字）。 */
export const SAVE_HINT_TEXT = 'api_key 留空 = 保持不变；保存任意配置会同时清除「密钥被拒」的暂停状态'

/** 能力说明卡的三行（人话版契约；与后端 `order_qr_fetch.py` 模块 docstring 同一口径）。 */
export const CAPABILITY_LINES: ReadonlyArray<string> = [
  '作用：订单还没有群二维码时，打手 / 客服点开订单的群码区块、或打手接单成功的那一刻，自动去对端查一次群码并落到订单上（扫码进本单群不用再等人工上传）。',
  '不会狂刷：同一单失败 / 未就绪后 10 分钟内不再自动查；对端返回「群没建 / 建码中」时静默等下一个自然触发点，不做后台轮询。',
  '没配置或开关关闭时一切照旧（完全向后兼容）。拉取的结果（成功 / 失败）在「操作日志」里，动作名是「马上来群码自动拉取」。',
]

/** 401 闩横幅的整段标记（只在闩住时插入）。 */
export function latchBannerHtml(text: string): string {
  return `
<div class="${LATCH_BANNER_CLASS} layui-bg-red layui-text" style="padding:10px 15px;margin-bottom:10px;line-height:22px">
  <b>已暂停：</b><span id="${LATCH_BANNER_TEXT_ID}">${text}</span>
</div>`
}

/** 整页标记（两张卡）。401 闩横幅**不在**这里——它由 `applyRow` 按服务端状态动态增删
 *（保存成功后只重灌值、⛔ 不重建整页：重建会把刚挂上的按钮监听一起丢掉）。 */
export function buildHtml(): string {
  return `
<div class="layui-card ${CARD_CLASS}">
  <div class="layui-card-header">查码配置</div>
  <div class="layui-card-body">
    <form class="layui-form" id="${FORM_ID}" lay-filter="${FORM_FILTER}" onsubmit="return false">
      <div class="layui-form-item">
        <label class="layui-form-label">对端基址</label>
        <div class="layui-input-block">
          <input type="text" id="${BASE_URL_INPUT_ID}" class="layui-input" autocomplete="off"
                 placeholder="${MESSAGES.baseUrlPlaceholder}">
        </div>
      </div>
      <div class="layui-form-item">
        <label class="layui-form-label">api_key</label>
        <div class="layui-input-block">
          <input type="password" id="${API_KEY_INPUT_ID}" class="layui-input" autocomplete="new-password"
                 placeholder="${MESSAGES.apiKeyPlaceholder}">
          <span class="layui-form-mid layui-word-aux" id="${MASKED_TEXT_ID}"></span>
        </div>
      </div>
      <div class="layui-form-item">
        <label class="layui-form-label">启用</label>
        <div class="layui-input-block" style="padding-top:8px">
          <input type="checkbox" id="${ENABLED_SWITCH_ID}" name="${ENABLED_FILTER}"
                 lay-skin="switch" lay-filter="${ENABLED_FILTER}">
          <span class="layui-form-mid layui-word-aux">关闭 = 全链路零动作（完全照旧）</span>
        </div>
      </div>
      <div class="layui-form-item">
        <div class="layui-input-block">
          <button type="button" class="layui-btn" id="${SAVE_BUTTON_ID}">保存配置</button>
          <span class="layui-form-mid layui-word-aux" id="${SAVE_HINT_ID}">${SAVE_HINT_TEXT}</span>
        </div>
      </div>
    </form>
  </div>
</div>
<div class="layui-card ${CARD_CLASS}">
  <div class="layui-card-header">能力说明</div>
  <div class="layui-card-body layui-text" style="line-height:22px">
    ${CAPABILITY_LINES.map((line) => `<p style="margin:0 0 6px">${line}</p>`).join('')}
  </div>
</div>`
}

export default {
  mount: async (root: HTMLElement, _ctx) => {
    document.title = TITLE
    // ⚠️ 顺序是硬要求（`AGENTS.md` §5-G 陷阱 ③）：先挂样式 → 再塞标记 → 最后 initIslandElements。
    const layui: LayuiNamespace = await bootstrapIsland(['layer', 'form'])
    const layer = layui.layer as LayuiModule
    const msg = (text: string): void => islandMsg(layer as unknown as Record<string, unknown>, text)

    // ⭐ **本页自己的门禁**（照 `pages/system-wechatmp-settings.ts` 的同款自检；iframe 深链时
    //    路由的两道门不生效）：持节点 + 账号管理员才能看。⚠️ 注入标记必须排在门禁**之后**。
    const me = await fetchMe()
    if (me.is_account_admin !== true || !canNode(me, ORDER_QR_FETCH_NODE)) {
      msg(NO_ACCESS_MESSAGE)
      return () => {}
    }

    /** 在途闸（防连点写两次）。 */
    let saving = false

    root.innerHTML = buildHtml()
    initIslandElements(layui)

    /** 页面里某个节点（缺了就回 `null`，由调用方决定要不要早退）。 */
    function at<T extends HTMLElement>(id: string): T | null {
      return root.querySelector<T>(`#${id}`)
    }

    /** 重渲开关（`lay-skin="switch"` 的裸 input 必须 `form.render` 才是 layui 开关）。 */
    function renderForm(): void {
      const form = (layui as unknown as { form?: LayuiModule }).form
      const render = (form as { render?: (type?: string, filter?: string) => void } | undefined)
        ?.render
      if (typeof render === 'function') render.call(form, 'checkbox', FORM_FILTER)
    }

    /** 401 闩横幅的增删（幂等：闩住且没有横幅 ⇒ 插；没闩且有横幅 ⇒ 摘）。 */
    function setLatchBanner(latched: boolean): void {
      const existing = root.querySelector(LATCH_BANNER_SELECTOR)
      if (latched && existing == null) {
        const form = at(FORM_ID)
        if (form) form.insertAdjacentHTML('beforebegin', latchBannerHtml(MESSAGES.latchBanner))
      } else if (!latched && existing != null) {
        existing.remove()
      }
    }

    /** 把服务端配置灌进页面（**唯一的装配出口**）。 */
    function applyRow(row: OrderQrConfigRow): void {
      const baseUrl = at<HTMLInputElement>(BASE_URL_INPUT_ID)
      if (baseUrl) baseUrl.value = row.base_url
      // ⛔ api_key **只回掩码**：掩码回写防呆——把掩码填回输入框会让下一次保存把掩码当新值
      //    存进去；所以这里**刻意留空**，掩码单独显示在旁边、placeholder 说明语义。
      const apiKey = at<HTMLInputElement>(API_KEY_INPUT_ID)
      if (apiKey) apiKey.value = ''
      const masked = at(MASKED_TEXT_ID)
      if (masked) masked.textContent = row.api_key_masked ? `当前密钥：${row.api_key_masked}` : ''
      const enabled = at<HTMLInputElement>(ENABLED_SWITCH_ID)
      if (enabled) enabled.checked = row.enabled
      setLatchBanner(row.auth_failed_at != null)
      renderForm()
    }

    // 保存（api_key **总是发**：空串 = 后端保持旧值——api 模块 docstring 的请求形状）。
    ;(at<HTMLButtonElement>(SAVE_BUTTON_ID) as HTMLButtonElement | null)?.addEventListener(
      'click',
      () => {
        if (saving) return
        saving = true
        const baseUrl = at<HTMLInputElement>(BASE_URL_INPUT_ID)?.value.trim() ?? ''
        const apiKey = at<HTMLInputElement>(API_KEY_INPUT_ID)?.value ?? ''
        const enabled = at<HTMLInputElement>(ENABLED_SWITCH_ID)?.checked ?? false
        void saveOrderQrConfig({ enabled, base_url: baseUrl, api_key: apiKey })
          .then((row) => {
            msg(MESSAGES.saved)
            applyRow(row)
          })
          .catch((e: Error) => msg(e.message))
          .finally(() => {
            saving = false
          })
      },
    )

    try {
      applyRow(await getOrderQrConfig())
    } catch (e) {
      msg(e instanceof Error ? e.message : MESSAGES.failed)
    }

    return () => {
      root.innerHTML = ''
    }
  },
} satisfies IslandPage
