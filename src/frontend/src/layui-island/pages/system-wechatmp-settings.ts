/**
 * 【第三方平台】→「微信公众号设置」页（island key = `system-wechatmp-settings`，
 * 路由 `/thirdparty/wechatmp-settings`）。
 *
 * 决策 **D-0268**（2026-10-19 自开发）；后端契约 = `src/apps/wechatmp/urls.py` + `views.py`；
 * 前端契约另一半 = `api/wechatmp.ts`。
 *
 * ## 这一页是**自有功能**，不是复刻
 *
 * 对标物（鸡蛋铺）里没有【第三方平台】这一组 ⇒ 没有证据件、不走 `mountConfigLike`。
 * 配方照 `pages/wecom-group-qr.ts`（同组**最新的**自有功能页）。island key 登记在
 * `config/__tests__/replicaRoutes.spec.ts` 的 `OWN_FEATURE_PAGE_KEYS`。
 *
 * ## 四个区块
 *
 * 1. **⭐ 认证提示横幅**（常驻显眼；业务方点名，文案**逐字**）——公众号 / 服务号必须
 * 微信认证，菜单 / 模板消息 / 网页授权才可用；
 * 2. **配置表单**：appid / appsecret（掩码展示、**留空 = 保持不变**，S4）/ token /
 * aes_key / 模式 / 启用开关 / 模板 id；
 * 3. **服务器配置信息卡**：回调 URL / Token / AESKey / 服务器 IP（一键复制；IP 来自
 * settings `MP_SERVER_PUBLIC_IP`，空 = "向部署方查询"占位，S6）；
 * 4. **菜单卡**：同步预设菜单（含「绑定账号」view 子菜单）/ 当前菜单 / 删除菜单；
 * `enabled=false` 或未就绪时按钮禁用并提示（后端同样 fail-closed）。
 *
 * ## ⛔ 这一页**没有**的东西（有意，别"顺手补上"）
 *
 * - ⛔ **appsecret 原文**：后端只回掩码（本页唯一不回原文的凭据）——页面上没有"查看
 * AppSecret"的按钮，复制出去的是**服务器配置三样**（token / aes_key / 回调 URL）；
 * - ⛔ **绑定管理列表**：谁绑了 openid 是「公众号绑定」的事，v1 没有读面（计划 T1）；
 * - ⛔ **模板消息的试发按钮**：v1 没有测试端点（照 webhook 通道"发送测试消息"是
 * **那条通道自己的端点**，本渠道没有）。
 *
 * ## 四条 island 陷阱（`AGENTS.md` §5-G，改这一页前先读）
 *
 * ① 类名与选择器分成两个常量；② 以 `(` 开头的语句一律前置分号；
 * ③ `await bootstrapIsland(...)`（挂样式）→ `root.innerHTML`（塞标记）→
 * `initIslandElements(layui)`（按 `lay-*` 初始化）；④ 本页**没有**定时器。
 * ⚠️ 四条都有**运行期**判据（`__tests__/system-wechatmp-settings.spec.ts`）。
 */
import { fetchMe } from '@/api/auth'
import {
  WECHATMP_SETTINGS_NODE,
  deleteWechatMpMenu,
  getWechatMpConfig,
  getWechatMpMenu,
  saveWechatMpConfig,
  syncWechatMpMenu,
  type WechatMpConfigRow,
  type WechatMpMode,
} from '@/api/wechatmp'
import { canNode } from '@/layui-island/perms'
import { escapeHtml } from '@/layui-island/escape'
import { bootstrapIsland, initIslandElements, islandMsg, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

/** island key（= 文件名 = 路由 `meta.islandPage`，三处必须逐字一致）。 */
export const PAGE_KEY = 'system-wechatmp-settings'
export const TITLE = '微信公众号设置'

// ── 类名与选择器**分成两个常量**（陷阱 ①：`class="${SELECTOR}"` 会造出 `class=".x"`）──────

/** 每一个区块的外层卡片（拼标记用 `CARD_CLASS`、查节点用 `CARD_SELECTOR`）。 */
export const CARD_CLASS = 'wechatMpCard'
export const CARD_SELECTOR = `.${CARD_CLASS}`
/** ⭐ 认证提示横幅（常驻显眼，业务方点名）。 */
export const BANNER_CLASS = 'wechatMpBanner'
export const BANNER_SELECTOR = `.${BANNER_CLASS}`

// ── 各节点的 `id` ────────────────────────────────────────────────────────────

/** 配置表单。 */
export const APPID_INPUT_ID = 'wechatMpAppid'
export const APPSECRET_INPUT_ID = 'wechatMpAppsecret'
export const TOKEN_INPUT_ID = 'wechatMpToken'
export const AES_KEY_INPUT_ID = 'wechatMpAesKey'
export const MODE_SELECT_ID = 'wechatMpMode'
export const ENABLED_SWITCH_ID = 'wechatMpEnabled'
export const TEMPLATE_ID_INPUT_ID = 'wechatMpTemplateId'
export const SAVE_BUTTON_ID = 'wechatMpSaveBtn'
/** 保存按钮旁边那句「留空 = 保持不变」的提示。 */
export const SAVE_HINT_ID = 'wechatMpSaveHint'
/** 未就绪（没启用 / 凭据不齐）时整块表单上方的那句提示。 */
export const NOT_READY_HINT_ID = 'wechatMpNotReadyHint'

/** 服务器配置信息卡（一键复制）。 */
export const CALLBACK_URL_TEXT_ID = 'wechatMpCallbackUrl'
export const SERVER_TOKEN_TEXT_ID = 'wechatMpServerToken'
export const SERVER_AES_KEY_TEXT_ID = 'wechatMpServerAesKey'
export const SERVER_IP_TEXT_ID = 'wechatMpServerIp'
export const COPY_CALLBACK_BUTTON_ID = 'wechatMpCopyCallbackBtn'
export const COPY_TOKEN_BUTTON_ID = 'wechatMpCopyTokenBtn'
export const COPY_AES_KEY_BUTTON_ID = 'wechatMpCopyAesBtn'
export const COPY_IP_BUTTON_ID = 'wechatMpCopyIpBtn'

/** 菜单卡。 */
export const MENU_SYNC_BUTTON_ID = 'wechatMpMenuSyncBtn'
export const MENU_DELETE_BUTTON_ID = 'wechatMpMenuDeleteBtn'
export const MENU_BODY_ID = 'wechatMpMenuBody'

/** 表单里 `layui-form` 容器（开关要 `form.render('checkbox', filter)` 才变成 layui 开关）。 */
export const FORM_ID = 'wechatMpForm'
export const FORM_FILTER = 'wechatMpForm'
export const ENABLED_FILTER = 'wechatMpEnabledToggle'

/** ⭐ 认证提示横幅（业务方点名，文案**逐字**；只增不改——它常驻显眼，不许置灰折叠）。 */
export const VERIFY_BANNER =
  '公众号或服务号需通过微信认证才能使用菜单、模板消息与网页授权；未认证请先到微信公众平台完成认证'

/** 服务器 IP 未配置时的占位（S6：来自 settings `MP_SERVER_PUBLIC_IP`，空 = 向部署方查询）。 */
export const SERVER_IP_PLACEHOLDER = '未配置（向部署方查询本机公网 IP）'

/** 三种加密模式的选项（value 逐字 = 后端 `EncryptionMode`；label 一句话人话）。 */
export const MODE_OPTIONS: ReadonlyArray<{ value: WechatMpMode; label: string }> = [
  { value: 'plain', label: '明文模式' },
  { value: 'compatible', label: '兼容模式' },
  { value: 'safe', label: '安全模式' },
]

/** 页内其它用户可见文案（抽成常量只为让 spec 钉住字面值）。 */
export const MESSAGES = {
  /** 保存 / 同步 / 删除失败时的兜底（后端文案通常已经有了）。 */
  failed: '操作失败',
  /** 复制失败（缺 `navigator.clipboard` / 被拒）——必须说出来（先例：open-apps.ts）。 */
  copyFailed: '复制失败（浏览器不允许自动复制），请手动选中上面的内容复制',
  /** 保存成功。⚠️ 说清"下一步该干什么"，⛔ 不是"操作成功"四个字。 */
  saved: '已保存：回到微信公众平台核对服务器配置（URL / Token / AESKey / IP 白名单）后，再点「启用」。',
  /** 菜单同步成功。 */
  menuSynced: '已同步预设菜单：打开公众号应能看到「绑定账号」与「我的订单」两项。',
  /** 菜单删除成功。 */
  menuDeleted: '已删除菜单。',
} as const

/** 「没有权限管理这一页」的自检提示（页面被深链打开时用）。 */
export const NO_ACCESS_MESSAGE = '无权限管理微信公众号设置'

/**
 * 未就绪时那句提示（纯函数，⛔ 只有这一处判据）。
 *
 * ⚠️ `enabled=true` 但凭据不齐**也是**未就绪（后端 `is_ready()` 的口径；
 * 前端只复述 `ready` 位，不自己重判——两套判据必然漂）。
 */
export function notReadyMessage(ready: boolean): string {
  return ready
    ? ''
    : '当前配置未就绪（未启用，或 AppID / AppSecret / Token 没配齐）：回调验签会回 403，菜单与订单推送也全部停用。'
}

/**
 * 复制一串文本到剪贴板（本页**唯一**的剪贴板出口；照 `wecom-group-qr.ts` 同名函数）。
 * ⚠️ 缺 `navigator.clipboard` **不是静默失败**：jsdom 与 `http://` 非安全上下文里它整个不存在。
 */
export function copyTextToClipboard(text: string, onDone: (ok: boolean) => void): void {
  const clipboard: { writeText?: (value: string) => Promise<void> } | undefined =
    typeof navigator === 'undefined' ? undefined : navigator.clipboard
  if (!clipboard || typeof clipboard.writeText !== 'function') {
    onDone(false)
    return
  }
  void Promise.resolve(clipboard.writeText.call(clipboard, text)).then(
    () => onDone(true),
    () => onDone(false),
  )
}

/**
 * 菜单卡正文的标记（纯函数）。
 *
 * ⚠️ `menu` 是 cgi-bin/menu/get 的**原文**：两种形状都要接 ——
 * 同步后页面自己拼的那份是 `{button:[…]}`，`menu/get` 的回包是 `{menu:{button:[…]}}`
 * （嵌在 `menu` 键里）。⛔ 只认一种会让"同步成功但列表还是空"这种假信号活下来。
 * 原文 JSON 折叠在 `<details>` 里（排障看得见、页面不花）；
 * 拉不到（后端回 `1000` 的业务错）时 `error` 非空 ⇒ **如实显示**（⛔ 不渲染成"还没有菜单"）。
 */
export function menuBodyHtml(
  menu: unknown,
  error: string,
): string {
  if (error !== '') {
    return `<div style="color:#FF5722;line-height:24px">${error}</div>`
  }
  let buttonList: unknown = null
  if (menu !== null && typeof menu === 'object') {
    const obj = menu as { button?: unknown; menu?: { button?: unknown } }
    buttonList = Array.isArray(obj.button) ? obj.button : obj.menu?.button
  }
  const buttons = Array.isArray(buttonList) ? (buttonList as Array<{ name?: string }>) : []
  if (buttons.length === 0) {
    return '<div style="color:#666;line-height:24px">公众号上还没有菜单：点上面的「同步预设菜单」创建。</div>'
  }
  const names = buttons
    .map((b) => (typeof b?.name === 'string' ? b.name : ''))
    .filter((n) => n !== '')
    // ⚠️ 微信 API 回包原文（菜单名可含任意字符）⇒ 进 `innerHTML` 前一律过 `escapeHtml`（L7）。
    .map((n) => `<span class="layui-badge layui-bg-gray" style="margin-right:6px">${escapeHtml(n)}</span>`)
    .join('')
  return (
    `<div style="line-height:28px">${names}</div>` +
    `<details style="margin-top:8px"><summary style="cursor:pointer;color:#666">原始回包</summary>` +
    `<pre style="background:#f7f7f7;border:1px solid #eee;padding:8px;overflow:auto">${escapeHtml(JSON.stringify(menu, null, 2))}</pre></details>`
  )
}

/**
 * 页面标记。所有区块**一次**渲染、由装配函数填值（⛔ 不用两套 innerHTML：挂在节点上的
 * 监听会随重建静默失效，= 本仓反复堵的"点了没反应"）。
 */
export function buildHtml(): string {
  const modeOptions = MODE_OPTIONS.map(
    (m) => `<option value="${m.value}">${m.label}</option>`,
  ).join('')
  return `
<div class="${BANNER_CLASS} layui-bg-orange layui-text" style="padding:10px 15px;margin-bottom:10px;line-height:22px">
  <b>⭐ 微信认证提示：</b>${VERIFY_BANNER}
</div>

<div class="layui-card ${CARD_CLASS}">
  <div class="layui-card-header">公众号配置</div>
  <div class="layui-card-body">
    <div id="${NOT_READY_HINT_ID}" style="color:#FF5722;line-height:22px;padding-bottom:8px"></div>
    <form class="layui-form" id="${FORM_ID}" lay-filter="${FORM_FILTER}" onsubmit="return false">
      <div class="layui-form-item">
        <label class="layui-form-label">AppID</label>
        <div class="layui-input-block">
          <input type="text" id="${APPID_INPUT_ID}" class="layui-input" autocomplete="off">
        </div>
      </div>
      <div class="layui-form-item">
        <label class="layui-form-label">AppSecret</label>
        <div class="layui-input-block">
          <input type="text" id="${APPSECRET_INPUT_ID}" class="layui-input" autocomplete="off"
                 placeholder="留空 = 保持不变（已保存的值只显示掩码）">
        </div>
      </div>
      <div class="layui-form-item">
        <label class="layui-form-label">Token</label>
        <div class="layui-input-block">
          <input type="text" id="${TOKEN_INPUT_ID}" class="layui-input" autocomplete="off"
                 placeholder="留空 = 保持不变（服务器配置信息卡里有原文）">
        </div>
      </div>
      <div class="layui-form-item">
        <label class="layui-form-label">EncodingAESKey</label>
        <div class="layui-input-block">
          <input type="text" id="${AES_KEY_INPUT_ID}" class="layui-input" autocomplete="off"
                 placeholder="43 位；留空 = 保持不变（明文模式可以不填）">
        </div>
      </div>
      <div class="layui-form-item">
        <label class="layui-form-label">加密模式</label>
        <div class="layui-input-block">
          <select id="${MODE_SELECT_ID}" class="layui-input">${modeOptions}</select>
        </div>
      </div>
      <div class="layui-form-item">
        <label class="layui-form-label">订单推送模板ID</label>
        <div class="layui-input-block">
          <input type="text" id="${TEMPLATE_ID_INPUT_ID}" class="layui-input" autocomplete="off"
                 placeholder="公众平台「模板消息」里申请的那条；留空 = 推送不发">
        </div>
      </div>
      <div class="layui-form-item">
        <label class="layui-form-label">启用</label>
        <div class="layui-input-block" style="padding-top:8px">
          <input type="checkbox" id="${ENABLED_SWITCH_ID}" name="${ENABLED_FILTER}"
                 lay-skin="switch" lay-filter="${ENABLED_FILTER}">
          <span class="layui-form-mid layui-word-aux">
            关掉后回调 / 菜单 / 订单推送全部停用（微信侧的服务器配置验证会失败）
          </span>
        </div>
      </div>
      <div class="layui-form-item">
        <div class="layui-input-block">
          <button type="button" class="layui-btn" id="${SAVE_BUTTON_ID}">保存配置</button>
          <span class="layui-form-mid layui-word-aux" id="${SAVE_HINT_ID}">
            AppSecret / Token / EncodingAESKey 留空 = 保持不变
          </span>
        </div>
      </div>
    </form>
  </div>
</div>

<div class="layui-card ${CARD_CLASS}">
  <div class="layui-card-header">服务器配置信息（复制到微信公众平台「服务器配置」）</div>
  <div class="layui-card-body">
    <div class="layui-form-item">
      <label class="layui-form-label">服务器 URL</label>
      <div class="layui-input-block" style="padding-top:8px">
        <span class="layui-form-mid" id="${CALLBACK_URL_TEXT_ID}"
              style="font-family:monospace;word-break:break-all"></span>
        <button type="button" class="layui-btn layui-btn-sm" id="${COPY_CALLBACK_BUTTON_ID}">复制</button>
      </div>
    </div>
    <div class="layui-form-item">
      <label class="layui-form-label">Token</label>
      <div class="layui-input-block" style="padding-top:8px">
        <span class="layui-form-mid" id="${SERVER_TOKEN_TEXT_ID}"
              style="font-family:monospace;word-break:break-all"></span>
        <button type="button" class="layui-btn layui-btn-sm" id="${COPY_TOKEN_BUTTON_ID}">复制</button>
      </div>
    </div>
    <div class="layui-form-item">
      <label class="layui-form-label">EncodingAESKey</label>
      <div class="layui-input-block" style="padding-top:8px">
        <span class="layui-form-mid" id="${SERVER_AES_KEY_TEXT_ID}"
              style="font-family:monospace;word-break:break-all"></span>
        <button type="button" class="layui-btn layui-btn-sm" id="${COPY_AES_KEY_BUTTON_ID}">复制</button>
      </div>
    </div>
    <div class="layui-form-item">
      <label class="layui-form-label">服务器 IP</label>
      <div class="layui-input-block" style="padding-top:8px">
        <span class="layui-form-mid" id="${SERVER_IP_TEXT_ID}" style="font-family:monospace"></span>
        <button type="button" class="layui-btn layui-btn-sm" id="${COPY_IP_BUTTON_ID}">复制</button>
        <div class="layui-form-mid layui-word-aux">填进公众平台「IP 白名单」；部署时由部署方配好</div>
      </div>
    </div>
  </div>
</div>

<div class="layui-card ${CARD_CLASS}">
  <div class="layui-card-header">公众号菜单</div>
  <div class="layui-card-body">
    <div style="padding-bottom:8px;line-height:22px;color:#666">
      预设菜单两项：「绑定账号」（微信内登录并绑定 openid，订单通知靠它）与「我的订单」（打手手机页）。
      同步会<b>整份覆盖</b>公众号上现有菜单；删除菜单也会<b>立即</b>生效。
    </div>
    <button type="button" class="layui-btn" id="${MENU_SYNC_BUTTON_ID}">同步预设菜单</button>
    <button type="button" class="layui-btn layui-btn-danger" id="${MENU_DELETE_BUTTON_ID}">删除菜单</button>
    <div id="${MENU_BODY_ID}" style="padding-top:8px"></div>
  </div>
</div>`
}

export const __testables = {
  buildHtml,
  copyTextToClipboard,
  menuBodyHtml,
  notReadyMessage,
}

export default {
  mount: async (root: HTMLElement, _ctx) => {
    document.title = TITLE
    // ⚠️ 顺序是硬要求（`AGENTS.md` §5-G 陷阱 ③）：先挂样式 → 再塞标记 → 最后 initIslandElements。
    const layui: LayuiNamespace = await bootstrapIsland(['layer', 'form'])
    const layer = layui.layer as LayuiModule
    const msg = (text: string): void => islandMsg(layer as unknown as Record<string, unknown>, text)

    // ⭐ **本页自己的门禁**（照 `pages/wecom-group-qr.ts` 的同款自检；iframe 深链时路由
    // 的两道门不生效）：持节点 + 账号管理员才能看。⚠️ 注入标记必须排在门禁**之后**
    // （`layui-island/page.ts` 丢掉 mount 的返回值，拒绝分支的 cleanup 不会被调用）。
    const me = await fetchMe()
    if (me.is_account_admin !== true || !canNode(me, WECHATMP_SETTINGS_NODE)) {
      msg(NO_ACCESS_MESSAGE)
      return () => {}
    }

    root.innerHTML = buildHtml()
    initIslandElements(layui)

    /** 服务端**此刻**的配置行（装配函数灌值用）。 */
    let current: WechatMpConfigRow | null = null
    /** 在途闸门（逐动作一个；防连点写两次）。 */
    let saving = false
    let syncing = false
    let deleting = false

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

    /** 菜单卡：取当前菜单并上屏（⛔ 失败如实显示，不渲染成"还没有菜单"）。 */
    async function loadMenu(): Promise<void> {
      const body = at(MENU_BODY_ID)
      try {
        const { wechat } = await getWechatMpMenu()
        if (body) body.innerHTML = menuBodyHtml(wechat, '')
      } catch (e) {
        if (body) body.innerHTML = menuBodyHtml(null, e instanceof Error ? e.message : MESSAGES.failed)
      }
    }

    /** 把服务端配置灌进页面（**唯一的装配出口**）。 */
    function applyRow(row: WechatMpConfigRow): void {
      current = row
      const appid = at<HTMLInputElement>(APPID_INPUT_ID)
      if (appid) appid.value = row.appid
      // ⛔ appsecret **只回掩码**（S4）：掩码回写防呆——把掩码填回输入框会让下一次保存
      // 把掩码当成新值存进去；所以这里**刻意留空**，placeholder 说明语义。
      const secret = at<HTMLInputElement>(APPSECRET_INPUT_ID)
      if (secret) secret.value = ''
      const token = at<HTMLInputElement>(TOKEN_INPUT_ID)
      if (token) token.value = ''
      const aesKey = at<HTMLInputElement>(AES_KEY_INPUT_ID)
      if (aesKey) aesKey.value = ''
      const mode = at<HTMLSelectElement>(MODE_SELECT_ID)
      if (mode) mode.value = row.mode
      const template = at<HTMLInputElement>(TEMPLATE_ID_INPUT_ID)
      if (template) template.value = row.template_id
      const enabled = at<HTMLInputElement>(ENABLED_SWITCH_ID)
      if (enabled) enabled.checked = row.enabled
      renderForm()

      const readyHint = at(NOT_READY_HINT_ID)
      if (readyHint) readyHint.textContent = notReadyMessage(row.ready)

      // 服务器配置信息卡（token / aes_key 原文 —— 要复制进公众平台）。
      const callback = at(CALLBACK_URL_TEXT_ID)
      if (callback) callback.textContent = row.server.callback_url
      const serverToken = at(SERVER_TOKEN_TEXT_ID)
      if (serverToken) serverToken.textContent = row.server.token
      const serverAes = at(SERVER_AES_KEY_TEXT_ID)
      if (serverAes) serverAes.textContent = row.server.aes_key
      const serverIp = at(SERVER_IP_TEXT_ID)
      if (serverIp) serverIp.textContent = row.server.server_ip === '' ? SERVER_IP_PLACEHOLDER : row.server.server_ip
    }

    // ── 保存（PUT；三个凭据留空 = 保持不变，S4）────────────────────────────
    at<HTMLButtonElement>(SAVE_BUTTON_ID)?.addEventListener('click', () => {
      if (saving) return
      saving = true
      void saveWechatMpConfig({
        appid: at<HTMLInputElement>(APPID_INPUT_ID)?.value.trim() ?? '',
        appsecret: at<HTMLInputElement>(APPSECRET_INPUT_ID)?.value.trim() ?? '',
        token: at<HTMLInputElement>(TOKEN_INPUT_ID)?.value.trim() ?? '',
        aes_key: at<HTMLInputElement>(AES_KEY_INPUT_ID)?.value.trim() ?? '',
        mode: (at<HTMLSelectElement>(MODE_SELECT_ID)?.value ?? 'plain') as WechatMpMode,
        enabled: at<HTMLInputElement>(ENABLED_SWITCH_ID)?.checked ?? false,
        template_id: at<HTMLInputElement>(TEMPLATE_ID_INPUT_ID)?.value.trim() ?? '',
      })
        .then(() => getWechatMpConfig())
        .then((row) => {
          applyRow(row)
          msg(MESSAGES.saved)
        })
        // ⚠️ 失败**如实显示**后端文案（`1000`：首次保存缺凭据 / safe 缺 AESKey / 43 位不对）。
        .catch((e: Error) => msg(e.message))
        .finally(() => {
          saving = false
        })
    })

    // ── 服务器配置信息卡的四颗复制按钮（复制内容**从行数据取**，⛔ 不从 DOM 读）──────
    const copyButton = (buttonId: string, pick: (row: WechatMpConfigRow) => string): void => {
      at<HTMLButtonElement>(buttonId)?.addEventListener('click', () => {
        if (current === null) return
        copyTextToClipboard(pick(current), (ok) =>
          msg(ok ? '已复制' : MESSAGES.copyFailed),
        )
      })
    }
    copyButton(COPY_CALLBACK_BUTTON_ID, (row) => row.server.callback_url)
    copyButton(COPY_TOKEN_BUTTON_ID, (row) => row.server.token)
    copyButton(COPY_AES_KEY_BUTTON_ID, (row) => row.server.aes_key)
    copyButton(COPY_IP_BUTTON_ID, (row) => row.server.server_ip)

    // ── 菜单：同步 / 删除（后端对未就绪一律回 `1000`，这里如实显示）──────────────
    at<HTMLButtonElement>(MENU_SYNC_BUTTON_ID)?.addEventListener('click', () => {
      if (syncing) return
      syncing = true
      void syncWechatMpMenu()
        .then(() => loadMenu())
        .then(() => msg(MESSAGES.menuSynced))
        .catch((e: Error) => msg(e.message))
        .finally(() => {
          syncing = false
        })
    })
    ;(at<HTMLButtonElement>(MENU_DELETE_BUTTON_ID) as HTMLButtonElement | null)?.addEventListener(
      'click',
      () => {
        // ⚠️ 二次确认：删除菜单**立即**生效（粉丝手机上的菜单当场消失）。
        ;(layer.confirm as (text: string, o: unknown, ok: () => void) => void)(
          '确定删除公众号菜单吗？粉丝手机上的菜单会立即消失（预设菜单也要重新同步才会回来）。',
          {},
          () => {
            if (deleting) return
            deleting = true
            void deleteWechatMpMenu()
              .then(() => loadMenu())
              .then(() => msg(MESSAGES.menuDeleted))
              .catch((e: Error) => msg(e.message))
              .finally(() => {
                deleting = false
              })
          },
        )
      },
    )

    try {
      applyRow(await getWechatMpConfig())
      await loadMenu()
    } catch (e) {
      msg(e instanceof Error ? e.message : MESSAGES.failed)
    }

    return () => {
      root.innerHTML = ''
    }
  },
} satisfies IslandPage
