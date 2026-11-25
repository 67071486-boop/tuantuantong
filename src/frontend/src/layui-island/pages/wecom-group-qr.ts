/**
 * 【第三方平台】→「自动上传企微群码到订单截图」页（island key = `wecom-group-qr`，
 * 路由 `/thirdparty/wecom-group-qr`）。
 *
 * spec：`docs/specs/2026-10-09-wecom-group-qr-auto-upload-design.md` §8（前端一节）/
 * §7.3（后端端点契约）/ §3-A1~A7（业务方裁定）/ **D9**（⛔ 不新增 RBAC 节点）/ **D10**
 * （页内再加一道账号管理员自检）。
 * 契约的另一半是**实现即契约**：`src/apps/openapi/api/wecom_qr_app_views.py` + `urls.py`。
 *
 * ## 这一页是**自有功能**，不是复刻
 *
 * 对标物（鸡蛋铺）里没有【第三方平台】这一组 ⇒ 本页**没有证据件**、也不走 `mountConfigLike`
 * （那是「取 `public/replica/<key>.html` → 注入 → 灌数据」的复刻专用链路）。标记由本文件自己写，
 * 配方照 `pages/open-apps.ts`（同一类**自有功能页**，本仓最新的那一份）。
 * island key 登记在 `config/__tests__/replicaRoutes.spec.ts` 的 `OWN_FEATURE_PAGE_KEYS`。
 *
 * ## 六个区块（数据只来自 §7.3 那五条端点）
 *
 * 1. **接入信息**：本租户域名（`window.location.origin`）+ AppKey + **AppSecret 明文 + 复制** + 状态，
 *    外加「停用 / 启用」与「轮换密钥」两颗动作按钮；
 * 2. **未创建**：一颗「创建」按钮，并且**写破**「创建后每个租户只有这一把，不能建第二个」
 *    （spec A6 业务方原话「该应用每个租户唯一 **不能设置多个**」）；
 * 3. **回调通知地址**（spec A7「最好也有个回调通知地址」）：地址输入 + 开关 + 保存 + 人话说明；
 * 4. **IP 白名单**：对接方的出口 IP 由租户自己填（空 = 不限制）；
 * 5. **推送台账**：**可折叠面板**（照 `open-apps.ts:1124-1152`），只读（本页没有重投——§7.3 五条
 *    端点里没有重投那一条，⛔ 不画一颗点了必然 404 的按钮）；
 * 6. **能力说明**：一句话讲清「只能读订单 + 取票只接受群聊二维码（`img_type=1`）」，
 *    并给出 `img_type=1` 的取票示例。
 *
 * ## ⛔ 这一页**没有**的几样东西（都是有意的，别"顺手补上"）
 *
 * - ⛔ **没有「权限设置 / 游戏范围」**：那是既有「应用管理」页 2026-10-08 已整块删掉的东西
 *   （v2 的裁定是「一个应用 = 本租户全部业务数据」）；本页是**能力受限**的专用应用，
 *   端点面由后端白名单（`apps/openapi/permissions.py`）fail-closed 定死，页面上没有可选项；
 *   ⚠️ 图种那一半（只能传群聊二维码）**锁在取票那一跳**，⛔ 不是凭证级 / 行级不变量
 *   （见下面 `CAPABILITY_NOTICE` 与 spec §7.2 的改准块、§9-15）。
 * - ⛔ **没有删除按钮**：沿用「只能停用不能删」（与 `/api/open/apps/` 同一条口径，后端连
 *   `DELETE` 路由都没有）。
 * - ⛔ **没有名称输入框**：§8.2 只要求一颗「创建」按钮；名字用后端默认值
 *   （`wecom_qr_app_views.DEFAULT_APP_NAME`）。⚠️ `name` 也**不在**本端点 PATCH 的可改字段里
 *   （§7.3 的四项）⇒ 页面上放一个改了不生效的输入框就是**静默不生效**。
 * - ⛔ **没有「只显示一次」那类措辞**：密钥**随时可取**（列表 / 本页都显示明文，与
 *   `docs/OPEN-API.md` §7.4 的既有口径一致）——写「关了就没」会是**与实现相反的一句话**。
 *
 * ## 四条 island 陷阱（`AGENTS.md` §5-G，改这一页前先读）
 *
 * ① 类名与选择器分成两个常量（`class="layui-card ${CARD_CLASS}"` 拼标记、`CARD_SELECTOR` 查节点
 *    ——写成 `class="${CARD_SELECTOR}"` 会造出 `class=".wecomQrCard"`、永远匹配不到）；
 * ② 以 `(` 开头的语句一律前置分号（`;(layer.confirm as …)(…)`——ASI 不会在 `)` 换行 `(` 之间插分号）；
 * ③ 顺序：`await bootstrapIsland(...)`（挂样式）→ `root.innerHTML = …`（塞标记）→
 *    `initIslandElements(layui)`（按标记里的 `lay-*` 初始化）；
 * ④ 页里有定时器时：假定时器要**先** `vi.useFakeTimers()` **再**包装 `window.setInterval`，
 *    且每个用例自己收口。⚠️ 本页**没有**任何定时器（台账是点开时拉一次），所以这一条落在测试的
 *    通用纪律上（`__tests__/wecom-group-qr.spec.ts`）。
 * ⚠️ 四条都有**运行期**判据（那个 spec 文件），不是靠这段注释。
 *
 * ## ⚠️ 台账三个状态里，本页**不画**任何按状态分叉的动作
 *
 * 既有「应用管理」页在 `failed` 行上画一颗「重投」；本页**没有**重投端点（见上）
 * ⇒ 六列全是只读：时间 / 事件 / 订单号 / 状态 / 尝试次数 / 失败原因。
 * 失败的行由服务端按退避自动重试（口径见 `docs/OPEN-API.md` §4.8），页面上只如实显示。
 */
import { fetchMe } from '@/api/auth'
import {
  WECOM_QR_APP_NODE,
  createWecomQrApp,
  getWecomQrApp,
  listWecomQrDeliveries,
  rotateWecomQrAppSecret,
  updateWecomQrApp,
  type WecomQrAppCallbackState,
  type WecomQrAppRow,
  type WecomQrDeliveryRow,
} from '@/api/wecomGroupQr'
import { escapeHtml } from '@/layui-island/escape'
import { canNode } from '@/layui-island/perms'
import { bootstrapIsland, initIslandElements, islandMsg, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

/** island key（= 文件名 = 路由 `meta.islandPage`，三处必须逐字一致）。 */
export const PAGE_KEY = 'wecom-group-qr'
export const TITLE = '自动上传企微群码到订单截图'

// ── 类名与选择器**分成两个常量**（陷阱 ①：`class="${SELECTOR}"` 会造出 `class=".x"`）──────

/** 每一个区块的外层卡片（拼标记用 `CARD_CLASS`、查节点用 `CARD_SELECTOR`）。 */
export const CARD_CLASS = 'wecomQrCard'
export const CARD_SELECTOR = `.${CARD_CLASS}`
/** 「还没创建」那一块（未创建时显示）。 */
export const EMPTY_CLASS = 'wecomQrEmpty'
export const EMPTY_SELECTOR = `.${EMPTY_CLASS}`
/** 「已创建」那五块（创建之后才显示）。 */
export const APP_CLASS = 'wecomQrApp'
export const APP_SELECTOR = `.${APP_CLASS}`

// ── 各节点的 `id` ────────────────────────────────────────────────────────────

/** 接入信息那一块。 */
export const DOMAIN_TEXT_ID = 'wecomQrDomain'
export const APP_KEY_TEXT_ID = 'wecomQrAppKey'
export const SECRET_TEXT_ID = 'wecomQrSecret'
export const STATUS_TEXT_ID = 'wecomQrStatus'
export const COPY_SECRET_BUTTON_ID = 'wecomQrCopySecretBtn'
export const TOGGLE_BUTTON_ID = 'wecomQrToggleBtn'
export const ROTATE_BUTTON_ID = 'wecomQrRotateBtn'
/** 未创建那一块。 */
export const CREATE_BUTTON_ID = 'wecomQrCreateBtn'
/** 回调通知地址那一块。 */
export const CALLBACK_URL_INPUT_ID = 'wecomQrCallbackUrl'
export const CALLBACK_HINT_ID = 'wecomQrCallbackHint'
export const CALLBACK_STATE_SWITCH_ID = 'wecomQrCallbackState'
export const CALLBACK_SAVE_BUTTON_ID = 'wecomQrCallbackSaveBtn'
/** IP 白名单那一块。 */
export const IP_INPUT_ID = 'wecomQrIpAllowlist'
export const IP_SAVE_BUTTON_ID = 'wecomQrIpSaveBtn'
/** 推送台账那一块（折叠开关 + 列表容器 + 失败提示）。 */
export const DELIVERIES_TOGGLE_ID = 'wecomQrDeliveriesToggle'
export const DELIVERIES_BODY_ID = 'wecomQrDeliveriesBody'
export const DELIVERIES_WARN_ID = 'wecomQrDeliveriesWarn'
/** 能力说明那一块。 */
export const CAPABILITY_ID = 'wecomQrCapability'

/**
 * 回调那一块的 `layui-form` 容器（`id` 与 `lay-filter` **同值**，这是 layui 的用法）。
 *
 * ⚠️ **`form.render` 的根选择器就是 `.layui-form`**（vendored `public/layui/layui.js` 里
 * `h=".layui-form"`，`render(type, filter)` 取 `.layui-form[lay-filter="…"]`）⇒ 没有这一个容器，
 * `lay-skin="switch"` 的那个裸 input **不会被渲染成开关**，页面上就是一个原生复选框。
 * ⇒ 本页的 `form.render` 调用都带上 `CALLBACK_FORM_FILTER`（⛔ 不用无参的全局渲染：
 * 那会把页面上别的 `.layui-form` 一起重渲）。
 */
export const CALLBACK_FORM_ID = 'wecomQrCallbackForm'
export const CALLBACK_FORM_FILTER = 'wecomQrCallbackForm'
/** 开关自己的 `lay-filter`（真浏览器里 `form.on('switch(…)')` 按它认领；本页只用 DOM 勾选态）。 */
export const CALLBACK_STATE_FILTER = 'wecomQrCallbackStateToggle'
/** 开关的 `name`（与 `id` **同值**：layui 的开关按 `name` 归属，缺它渲染不出开关）。 */
export const CALLBACK_STATE_NAME = 'wecomQrCallbackStateToggle'

/** 台账第一页取多少条（`EnvelopePagination` 上限 200；页面只展示最近这些）。 */
export const DELIVERIES_PAGE_SIZE = 20

/** 「推送台账」折叠链接的两句文案（`textContent` 写进去 ⇒ ⛔ 只能是纯文本）。 */
export const DELIVERIES_TOGGLE_CLOSED = '最近推送记录（点这里展开）'
export const DELIVERIES_TOGGLE_OPEN = '最近推送记录（点这里收起）'

/**
 * ⭐ **「每租户只有一把」必须在这页上说破**（spec A6 的原话「不能设置多个」）。
 *
 * 它同时解释了两件用户会遇到的事：① 创建按钮建完就消失；② 第二次创建会被后端回
 * `1000`「本租户已创建过该应用，不能重复创建」——⛔ 不要让它变成一个隐性默认。
 */
export const ONLY_ONE_NOTICE =
  '创建后每个租户只有这一把，不能建第二个（同一套能力要给两家对接方的话，本功能不支持）。'

/** 「没有权限管理这一页」的自检提示（页面被深链打开时用）。 */
export const NO_ACCESS_MESSAGE = '无权限管理企微群码上传应用'

/**
 * 回调地址的**即时提示**（前端这一侧的唯一一句）。
 *
 * ⚠️ **它不是判据**（真判据在后端：保存时校验，仅 https + SSRF 私网段）——前端**看不到**
 * DNS 解析结果，做不到那一条 ⇒ 这里只在格式明显不对时**当场说一句**；后端回的业务码 `1000`
 * 与它那句中文由页面**如实显示**（见 `saveCallbackSettings`）。
 */
export const CALLBACK_URL_HINT =
  '回调地址只接受 https 开头的地址（http 与其它协议一律不收；内网地址后端也会拒）。'

/**
 * 回调那一块的**人话说明**（三件事必须写清：https only / 空或停用时**不推送** / 对方要验签名）。
 *
 * ⚠️ 推送事件名与签名头名字来自**实现**（`apps/openapi/outbound.py`；口径见
 * `docs/OPEN-API.md`）：`X-TT-Timestamp` / `X-TT-Delivery` / `X-TT-Signature`。
 * ⛔ 页面上不抄签名公式（抄了就会漂），只说"要用 AppSecret 按我们的算法验真"。
 * ⚠️ 强调一律用 HTML 的 `<b>`（这段是塞进 `innerHTML` 的）——⛔ 不要写 markdown 的 `**`：
 * 浏览器不认 markdown，用户会**连着星号一起看到**。
 */
export const CALLBACK_NOTICE =
  '推送怎么走：订单状态变化 / 订单删除 / 订单新增图片这几类事件，我们会按产生顺序 POST 到上面的地址，' +
  '每条都带 <b>X-TT-Timestamp</b> / <b>X-TT-Delivery</b> / <b>X-TT-Signature</b> 三个头，' +
  '对方要用 AppSecret 按我们的算法<b>验真</b>再处理（⛔ 不要只看来源 IP 就算数）。' +
  '<br>地址为空、或者开关是「停用」⇒ <b>不会推送</b>（不排队、不重试、也不报错）；' +
  '推送失败服务端会自动重试几次，仍失败的那条会留在下面的台账里（状态是「投递失败」）。'

/**
 * ⭐ **能力说明**（spec §8.2 最后一条：一句话列出「只能读订单 + 取票只接受群聊二维码」）。
 *
 * ⚠️ 这句话是**对外的能力承诺**，它的落地处在后端（`apps/openapi/permissions.py` 的
 * `WECOM_GROUP_QR_ALLOWED_ENDPOINTS` 恰好集合 + **取票端点**的 `img_type` 分叉）——
 * ⛔ 别在这里加后端没有的能力（`AGENTS.md` §5-C：文档 / 文案与实现冲突时要么改文案、要么改实现）。
 * ⚠️⭐ **「群聊二维码」那一半的口径是「取票那一跳」**（业务方裁定「不补代码、改文案」）：
 * 票的图种由**取票那一跳**决定，而**认领端点不重判图种** ⇒ ⛔ 别在这里写「凭证级不变量」
 * 这类更强的承诺（边界与三条前提见 spec §7.2 的改准块与 §9-15）。
 */
export const CAPABILITY_NOTICE =
  '这把密钥能做的事只有两件：<b>读订单</b>（含按时间增量拉单）与<b>取票只接受群聊二维码</b>' +
  '（取票必须显式传 <b>img_type=1</b>，传 <b>0</b> 或不传一律被拒）；改单 / 改状态 / 改价 / 录单 / ' +
  '传订单进度图一律被拒（后端按「方法 + 端点」的白名单放行，新端点默认拒绝）。'

/** `img_type=1` 的取票示例（页面上的 `<pre>` 原文）。⚠️ 逐字对着实现写（§7.2 + `docs/OPEN-API.md` §2）。 */
export const TICKET_EXAMPLE =
  '# 1) 先用 AppKey / AppSecret 换令牌（POST {域名}/api/open/oauth/token）\n' +
  '# 2) 再取「群聊二维码」的票（取票只放行这一类图）\n' +
  'POST {域名}/api/open/v1/orders/<订单号>/images/ticket/\n' +
  'Authorization: Bearer <令牌>\n' +
  'Idempotency-Key: <8-128 位随机串>\n' +
  'Content-Type: application/json\n' +
  '\n' +
  '{"img_type": 1}'

/** 页内其它用户可见文案（抽成常量只为让 spec 钉住字面值）。 */
export const MESSAGES = {
  /** 保存 / 创建 / 轮换 / 启停失败时的兜底（后端文案通常已经有了）。 */
  failed: '操作失败',
  /**
   * 复制失败（缺 `navigator.clipboard` / 被拒）。
   * ⚠️ 必须**说出来**并告诉人怎么办：静默失败会让用户以为剪贴板里已经有了
   * （先例：`pages/open-apps.ts` 的同名文案）。
   */
  copyFailed: '复制失败（浏览器不允许自动复制），请手动选中上面的密钥复制',
  /** 创建成功（+ 密钥已上屏）。⚠️ 说清"下一步该干什么"，⛔ 不是"操作成功"四个字。 */
  created: '已创建：这把密钥就是本租户唯一的一把，把它交给对接方（下面可以复制）。',
  /** 轮换成功。⚠️ 必须写出后果：旧密钥**当场失效**。 */
  rotated: '已轮换：新密钥已生效，旧密钥当场作废（对接方要马上换配置）。',
} as const

/**
 * 那颗状态按钮的**文案**（纯函数，⛔ 只有这一处判据）。
 *
 * 「启用中」⇒ 这一下是**停用**；「已停用」⇒ 这一下是**启用**。
 * ⚠️ 判据用 `row.state`（枚举值 `enabled` / `disabled`），⛔ **不用** `state_text`
 * （那是给人看的中文名，服务端可以改文案，用它做判据会静默错）。
 */
export function stateActionLabel(row: WecomQrAppRow): string {
  return row.state === 'enabled' ? '停用' : '启用'
}

/**
 * 一个 ISO 8601 时刻的**显示形态**（纯函数）：`2026-10-09 17:20:00`。
 *
 * ⚠️ 只截到秒、**不做时区换算**（后端给的就是带偏移的本地时刻）；
 * `null` / 空串 ⇒ `—`（⛔ 不是空白格：空白会被读成"这一列没数据"）。
 */
export function timeText(value: string | null): string {
  if (value === null || value === '') return '—'
  return escapeHtml(value.replace('T', ' ').slice(0, 19))
}

/**
 * 「复制密钥」的剪贴板出口（**本页唯一的剪贴板出口**）。
 *
 * ⚠️ **缺 `navigator.clipboard` 不是静默失败**（先例与理由：`pages/open-apps.ts` 的同名函数、
 * 更早的 `order-orderlist.ts`）：jsdom 与非安全上下文（`http://` 的普通域名）里它整个不存在；
 * `writeText` 也可能被判 `NotAllowedError` ⇒ 失败必须**说出来**。
 * ⚠️ 复制的内容是 `app_key` + `app_secret` **两行**（对接方要的就是这一对，只给一半等于让人
 * 回去再抄一次）。
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

/** 复制出去的文本（`AppKey` / `AppSecret` 两行）。 */
export function credentialText(row: WecomQrAppRow): string {
  return `AppKey: ${row.app_key}\nAppSecret: ${row.app_secret}`
}

/**
 * 回调地址的**即时提示**（纯函数，⛔ 只有这一处判据）。
 *
 * 三个分支各自有用途：
 * - **空串** ⇒ 空提示：`callback_url=""` 是**合法值**（= 没配回调 ⇒ 不推送）
 *   ⇒ ⛔ 不许提示"要填"（那会把一个合法状态说成错的）；
 * - **https 开头且后面还有东西** ⇒ 空提示（格式这一层没问题）；
 * - **其余**（http / 别的协议 / 只有 `https://` 没有主机）⇒ `CALLBACK_URL_HINT`。
 *
 * ⚠️ 判据用 `startsWith` + 长度而不是正则：这一条是**提示**，不是校验器
 * ⇒ ⛔ 别把它"升级"成一个前端校验门（那会把后端的 `1000` 挡在页面之外）。
 */
export function callbackUrlHint(value: string): string {
  const trimmed = value.trim()
  if (trimmed === '') return ''
  const prefix = 'https://'
  if (trimmed.toLowerCase().startsWith(prefix) && trimmed.length > prefix.length) return ''
  return CALLBACK_URL_HINT
}

/**
 * 保存回调配置之后那句**回报**（⛔ 不许"保存成功"四个字了事）。
 *
 * ⚠️ 两个分支都对着**真实后果**说：地址空 / 开关停用 ⇒ 这个应用**不会推送任何东西**
 * （用户以为"配好了"而其实一条都不会发，是本仓反复堵的假成功形态）。
 */
export function callbackSaveReport(url: string, state: WecomQrAppCallbackState): string {
  if (url.trim() === '') {
    return '已保存：回调地址为空 ⇒ 不会推送任何事件（不排队、不重试、也不报错）。'
  }
  if (state === 'disabled') {
    return '已保存：回调开关是「停用」⇒ 不会推送任何事件（地址留着，随时可以再打开）。'
  }
  return '已保存：推送已打开；发失败的记录会留在「最近推送记录」里（服务端会先自动重试几次）。'
}

/**
 * IP 白名单保存之后那句**回报**（空 = 不限制，是个**合法值**，⛔ 不许说成"没填"）。
 */
export function ipSaveReport(value: string): string {
  return value.trim() === ''
    ? '已保存：IP 白名单为空 ⇒ 不限制来源 IP（任何 IP 拿着这把密钥都能调）。'
    : '已保存：只有白名单里的 IP 能调（其余来源一律 401）。'
}

/**
 * 台账一行的**标记**（纯函数）。
 *
 * ⚠️ 六个来自后端的字符串（`event_text` / `order_no` / `source_order_no` / `status_text` /
 * `reason` / 时间）一律 `escapeHtml`（`reason` 是**对方响应里的原文**，最脏的就是它）。
 * ⚠️ 判据打在**机器值** `status` 上（⛔ 不打在 `status_text` 那句中文上）。
 * ⛔ 本页**没有**「重投」那一列（§7.3 五条端点里没有重投，见文件头）。
 */
export function deliveryRowHtml(d: WecomQrDeliveryRow): string {
  const statusColor =
    d.status === 'done' ? '#009688' : d.status === 'failed' ? '#FF5722' : '#FF9800'
  const next =
    d.status === 'pending' && d.next_attempt_at !== null
      ? `<div style="color:#666">下次 ${timeText(d.next_attempt_at)}</div>`
      : ''
  const reason =
    d.reason === ''
      ? '—'
      : escapeHtml(d.reason) + (d.response_status === null ? '' : `（HTTP ${d.response_status}）`)
  // ⭐ D-0301：主行 = **展示单号**（来源号非空取它、空回退内置号）；
  // 副行只在**两号不同**时标注「内置 <内置号>」（两号相同就不用重复说一遍）。
  const order =
    escapeHtml(d.source_order_no || d.order_no) +
    (d.source_order_no !== '' && d.source_order_no !== d.order_no
      ? `<div style="color:#666">内置 ${escapeHtml(d.order_no)}</div>`
      : '')
  return (
    `<tr>` +
    `<td style="white-space:nowrap">${timeText(d.created_at)}</td>` +
    `<td style="white-space:nowrap">${escapeHtml(d.event_text)}</td>` +
    `<td>${order}</td>` +
    `<td style="white-space:nowrap"><span style="color:${statusColor}">` +
    `${escapeHtml(d.status_text)}</span>${next}</td>` +
    `<td>${d.attempts}</td>` +
    `<td>${reason}</td>` +
    `</tr>`
  )
}

/**
 * 「最近推送记录」列表的标记（纯函数）。
 *
 * ⚠️ **空列表与"拉取失败"是两件事**：这里只在**真的没有行**时说「还没有推送记录」；
 * 拉取失败由调用方写进 `DELIVERIES_WARN_ID` 那个提示容器（⛔ 不许把失败渲染成空列表——
 * 那正是本仓反复堵的"假成功"形态）。
 * ⚠️ `total > items.length` 时**如实说**还差多少（页面今天只取第一页，⛔ 不假装这就是全部）。
 */
export function deliveriesHtml(items: readonly WecomQrDeliveryRow[], total: number): string {
  if (items.length === 0) {
    return '<div style="color:#666;line-height:24px">还没有推送记录（这把应用没有产生过事件）。</div>'
  }
  const more =
    total > items.length
      ? `<div style="color:#666;line-height:24px">共 ${total} 条，这里只显示最近 ${items.length} 条。</div>`
      : ''
  return (
    `<table class="layui-table" lay-size="sm" style="margin:0"><thead><tr>` +
    '<th style="width:150px">时间</th>' +
    '<th style="width:130px">事件</th>' +
    '<th>订单号</th>' +
    '<th style="width:130px">状态</th>' +
    '<th style="width:70px">尝试次数</th>' +
    '<th>失败原因</th>' +
    '</tr></thead><tbody>' +
    items.map(deliveryRowHtml).join('') +
    `</tbody></table>${more}`
  )
}

/**
 * 页面标记。
 *
 * ⚠️ 两个块（`EMPTY_SELECTOR` / `APP_SELECTOR`）**都**渲染出来、由 `applyRow()` 切 `display`：
 * 「还没创建」与「已创建」是**同一页的两种态**，⛔ 不用两套 `innerHTML`（那样每次刷新都会
 * 重建节点 ⇒ 挂在节点上的监听会**静默失效**，= 本仓反复堵的"点了没反应"）。
 */
export function buildHtml(): string {
  return `
<blockquote class="layui-elem-quote layui-quote-nm">
  这一页给「第三方程序自动把我们客户的<b>企微群二维码</b>传成订单截图」用：
  在下面建一把<b>只有本租户有</b>的开放平台密钥，并配好回调通知地址。
  <br>
  <span style="color:#FF5722"><b>${CAPABILITY_NOTICE}</b></span>
  <br>
  <span style="color:#666">
    密钥<b>只能停用不能删除</b>；轮换密钥<b>没有宽限期</b>——旧密钥当场作废。
    密钥随时可以在下面查看与复制。
  </span>
</blockquote>

<div class="${EMPTY_CLASS}" style="display:none">
  <div class="layui-card ${CARD_CLASS}">
    <div class="layui-card-header">还没创建</div>
    <div class="layui-card-body">
      <div style="color:#666;line-height:24px;padding-bottom:10px">
        ${ONLY_ONE_NOTICE}
      </div>
      <button type="button" class="layui-btn" id="${CREATE_BUTTON_ID}">创建应用</button>
      <div class="layui-form-mid layui-word-aux" style="padding:8px 0 0 0">
        创建成功后系统会生成 AppKey 与 AppSecret（默认应用名由系统给），
        再把<b>回调通知地址</b>与<b>IP 白名单</b>配好。
      </div>
    </div>
  </div>
</div>

<div class="${APP_CLASS}" style="display:none">

  <div class="layui-card ${CARD_CLASS}">
    <div class="layui-card-header">接入信息</div>
    <div class="layui-card-body">
      <div class="layui-form-item">
        <label class="layui-form-label">本租户域名</label>
        <div class="layui-input-block">
          <span class="layui-form-mid" id="${DOMAIN_TEXT_ID}"
                style="font-family:monospace;word-break:break-all"></span>
        </div>
      </div>
      <div class="layui-form-item">
        <label class="layui-form-label">AppKey</label>
        <div class="layui-input-block">
          <input type="text" id="${APP_KEY_TEXT_ID}" class="layui-input" readonly>
        </div>
      </div>
      <div class="layui-form-item">
        <label class="layui-form-label">AppSecret</label>
        <div class="layui-input-block">
          <textarea id="${SECRET_TEXT_ID}" class="layui-textarea" readonly rows="3"
                    style="word-break:break-all"></textarea>
        </div>
      </div>
      <div class="layui-form-item">
        <label class="layui-form-label">状态</label>
        <div class="layui-input-block">
          <span class="layui-form-mid" id="${STATUS_TEXT_ID}"></span>
          <button type="button" class="layui-btn layui-btn-sm" id="${COPY_SECRET_BUTTON_ID}">
            复制 AppKey 与 AppSecret
          </button>
          <button type="button" class="layui-btn layui-btn-sm layui-btn-primary" id="${TOGGLE_BUTTON_ID}"></button>
          <button type="button" class="layui-btn layui-btn-sm layui-btn-danger" id="${ROTATE_BUTTON_ID}">
            轮换密钥
          </button>
        </div>
      </div>
      <div class="layui-form-mid layui-word-aux" style="padding:0 0 6px 110px">
        ⛔ 这是凭据：不要贴到聊天群 / 工单 / 截图里。轮换之后<b>旧密钥立刻作废</b>，对接方要马上改配置。
      </div>
    </div>
  </div>

  <div class="layui-card ${CARD_CLASS}">
    <div class="layui-card-header">回调通知地址</div>
    <div class="layui-card-body">
      <blockquote class="layui-elem-quote" style="border-left:5px solid #1E9FFF;background:#F5FAFF">
        <span style="color:#333">${CALLBACK_NOTICE}</span>
      </blockquote>
      <div class="layui-form" id="${CALLBACK_FORM_ID}" lay-filter="${CALLBACK_FORM_FILTER}">
        <div class="layui-form-item">
          <label class="layui-form-label">回调地址</label>
          <div class="layui-input-block">
            <input type="text" id="${CALLBACK_URL_INPUT_ID}" class="layui-input" maxlength="200"
                   autocomplete="off" placeholder="https://接收推送的地址（留空 = 不推送）">
          </div>
        </div>
        <div class="layui-form-mid layui-word-aux" id="${CALLBACK_HINT_ID}"
             style="padding:0 0 8px 110px;color:#FF5722"></div>
        <div class="layui-form-item">
          <label class="layui-form-label">回调开关</label>
          <div class="layui-input-block">
            <input type="checkbox" id="${CALLBACK_STATE_SWITCH_ID}" name="${CALLBACK_STATE_NAME}"
                   lay-skin="switch" lay-text="启用|停用" lay-filter="${CALLBACK_STATE_FILTER}">
          </div>
        </div>
      </div>
      <div style="text-align:center;padding:4px 0 14px">
        <button type="button" class="layui-btn layui-btn-normal" id="${CALLBACK_SAVE_BUTTON_ID}">
          保存回调设置
        </button>
      </div>
    </div>
  </div>

  <div class="layui-card ${CARD_CLASS}">
    <div class="layui-card-header">IP 白名单</div>
    <div class="layui-card-body">
      <div class="layui-form-item">
        <label class="layui-form-label">允许的 IP</label>
        <div class="layui-input-block">
          <textarea id="${IP_INPUT_ID}" class="layui-textarea" rows="2" maxlength="500"
                    placeholder="留空 = 不限制。多个 IP / 网段怎么写，按对接方给的出口 IP 填，一行一个"></textarea>
        </div>
      </div>
      <div style="text-align:center;padding:4px 0 14px">
        <button type="button" class="layui-btn layui-btn-normal" id="${IP_SAVE_BUTTON_ID}">
          保存 IP 白名单
        </button>
      </div>
    </div>
  </div>

  <div class="layui-card ${CARD_CLASS}">
    <div class="layui-card-header">推送台账</div>
    <div class="layui-card-body">
      <a href="javascript:;" id="${DELIVERIES_TOGGLE_ID}"
         style="font-weight:700;color:#1E9FFF">${DELIVERIES_TOGGLE_CLOSED}</a>
      <div id="${DELIVERIES_BODY_ID}"
           style="display:none;margin-top:6px;max-height:360px;overflow:auto"></div>
      <div id="${DELIVERIES_WARN_ID}" style="color:#FF5722;line-height:24px"></div>
    </div>
  </div>

</div>

<div class="layui-card ${CARD_CLASS}">
  <div class="layui-card-header">能力与调用示例</div>
  <div class="layui-card-body">
    <div id="${CAPABILITY_ID}" style="color:#666;line-height:24px">
      ${CAPABILITY_NOTICE}
      回调能收到的事件只有三类：订单状态变化 / 订单删除 / 订单新增图片；
      ⛔ 没有「新订单」推送（要发现新单请按时间轮询上面那个拉单端点）。
    </div>
    <div style="color:#666;line-height:24px;padding:8px 0 4px">
      客户群里那张二维码怎么传上来（<b>img_type=1</b>，本应用只放行这一类）：
    </div>
    <pre style="background:#f7f7f7;border:1px solid #eee;padding:8px;overflow:auto">${escapeHtml(TICKET_EXAMPLE)}</pre>
  </div>
</div>`
}

export const __testables = {
  buildHtml,
  callbackSaveReport,
  callbackUrlHint,
  copyTextToClipboard,
  credentialText,
  deliveriesHtml,
  deliveryRowHtml,
  ipSaveReport,
  stateActionLabel,
  timeText,
}

export default {
  mount: async (root: HTMLElement, _ctx) => {
    document.title = TITLE
    // ⚠️ 顺序是硬要求（`AGENTS.md` §5-G 陷阱 ③）：先挂样式（`bootstrapIsland`），再塞页面标记，
    //    最后 `initIslandElements`（它要按标记里的 `lay-*` / `layui-form` 初始化）。
    const layui: LayuiNamespace = await bootstrapIsland(['layer', 'form'])
    const layer = layui.layer as LayuiModule
    // ⭐ `form` 是**回调开关**要的：`lay-skin="switch"` 的裸 input 必须经 `form.render`
    //    才会变成开关（见 `CALLBACK_FORM_FILTER` 的注释）。
    //    ⚠️ 取不到时**降级成不渲染**（不是崩）：测试替身与"layui 半加载"那种情形下，
    //    页面照样能用（原生复选框），只是样式不是 layui 那套。
    const form = (layui as unknown as { form?: LayuiModule }).form
    const msg = (text: string): void => islandMsg(layer as unknown as Record<string, unknown>, text)

    /** 重渲开关那一块（`lay-skin="switch"`）。 */
    function renderCallbackForm(): void {
      const render = (form as { render?: (type?: string, filter?: string) => void } | undefined)
        ?.render
      if (typeof render === 'function') render.call(form, 'checkbox', CALLBACK_FORM_FILTER)
    }

    // ⚠️ **本页自己的门禁**（与路由那两道门同源，⛔ 不是第二套判据；spec **D10** 直接指定
    //    照 `pages/open-apps.ts:1040-1044` 做）：【第三方平台】组**不能**加组级
    //    `accountAdminOnly`（会连累同组的「闲管家店铺对接」），而 AppSecret 是明文可取的凭据
    //    ⇒ 页面内容收窄到账号管理员。iframe 可被深链（`page.html?page=wecom-group-qr`）打开，
    //    那一刻路由的 `requiresAccountAdmin` + `permissions` 都不生效。
    //    ⚠️ 注入标记必须排在门禁**之后**：`layui-island/page.ts` 丢掉 `mount` 的返回值，
    //    拒绝分支返回的 cleanup 永远不会被调用 ⇒ 先注入就会让被拒的深链看到整个页面壳
    //    （包括那颗「创建」按钮）。
    const me = await fetchMe()
    if (me.is_account_admin !== true || !canNode(me, WECOM_QR_APP_NODE)) {
      msg(NO_ACCESS_MESSAGE)
      return () => {}
    }

    root.innerHTML = buildHtml()
    initIslandElements(layui)

    /** 服务端**此刻**那把应用（`null` = 还没建）。 */
    let current: WecomQrAppRow | null = null
    /** 在途闸门（逐动作一个；防连点写两次）。 */
    let creating = false
    let saving = false
    let rotating = false

    /** 页面里某个节点（缺了就回 `null`，由调用方决定要不要早退）。 */
    function at<T extends HTMLElement>(id: string): T | null {
      return root.querySelector<T>(`#${id}`)
    }

    /** 收起 / 展开「最近推送记录」（折叠面板的唯一出口）。 */
    function setDeliveriesOpen(open: boolean): void {
      const body = at(DELIVERIES_BODY_ID)
      const toggle = at(DELIVERIES_TOGGLE_ID)
      if (body) body.style.display = open ? '' : 'none'
      if (toggle) toggle.textContent = open ? DELIVERIES_TOGGLE_OPEN : DELIVERIES_TOGGLE_CLOSED
    }

    /**
     * 取最近一批推送记录并上屏。
     *
     * ⚠️ **失败要如实显示**（写进 `DELIVERIES_WARN_ID`），⛔ 不许渲染成"还没有推送记录"：
     * 「一条都没有」与「没取到」长得一样，用户会以为自己的推送一直没发出去
     * （或者反过来以为很干净）——两个方向都是本仓反复堵的假信号。
     */
    async function loadDeliveries(): Promise<void> {
      const body = at(DELIVERIES_BODY_ID)
      const warn = at(DELIVERIES_WARN_ID)
      if (warn) warn.textContent = ''
      try {
        const page = await listWecomQrDeliveries({ page: 1, page_size: DELIVERIES_PAGE_SIZE })
        if (body) body.innerHTML = deliveriesHtml(page.items, page.total)
      } catch (e) {
        if (body) body.innerHTML = ''
        if (warn) warn.textContent = e instanceof Error ? e.message : MESSAGES.failed
      }
    }

    /**
     * 把服务端那一行灌进页面（**唯一的装配出口**）。
     *
     * ⚠️ `null` = 还没建 ⇒ 显示「创建」那块、藏起「已创建」那几块；
     * ⛔ 两个块都留着（切 `display`），见 `buildHtml` 的注释。
     * ⚠️ 每次装配都把台账**清空并收回收起态**：换了一把应用之后还留着上一把的推送记录
     * 就是一条**指向别处的**证据（先例：`open-apps.ts::openCallback` 的同款处理）。
     */
    function applyRow(row: WecomQrAppRow | null): void {
      current = row
      const empty = root.querySelector<HTMLElement>(EMPTY_SELECTOR)
      const app = root.querySelector<HTMLElement>(APP_SELECTOR)
      if (empty) empty.style.display = row === null ? '' : 'none'
      if (app) app.style.display = row === null ? 'none' : ''
      const body = at(DELIVERIES_BODY_ID)
      const warn = at(DELIVERIES_WARN_ID)
      if (body) body.innerHTML = ''
      if (warn) warn.textContent = ''
      setDeliveriesOpen(false)
      if (row === null) return

      // 接入信息：域名取**当前浏览器地址**（第三方要填的就是它，⛔ 不写死后端域名）。
      const domain = at(DOMAIN_TEXT_ID)
      if (domain) domain.textContent = window.location.origin
      const key = at<HTMLInputElement>(APP_KEY_TEXT_ID)
      if (key) key.value = row.app_key
      const secret = at<HTMLTextAreaElement>(SECRET_TEXT_ID)
      if (secret) secret.value = row.app_secret
      const status = at(STATUS_TEXT_ID)
      if (status) {
        // 状态名用后端的中文（`state_text`），⛔ 前端不镜像一份词表（E-0021）。
        status.textContent = row.state_text
        status.style.color = row.state === 'enabled' ? '#009688' : '#FF5722'
      }
      const toggle = at<HTMLButtonElement>(TOGGLE_BUTTON_ID)
      if (toggle) toggle.textContent = stateActionLabel(row)

      // 回调：回填 = **这一行自己的**配置（⛔ 不是空、也不是别的值）。
      const url = at<HTMLInputElement>(CALLBACK_URL_INPUT_ID)
      if (url) url.value = row.callback_url
      const hint = at(CALLBACK_HINT_ID)
      if (hint) hint.textContent = callbackUrlHint(row.callback_url)
      const state = at<HTMLInputElement>(CALLBACK_STATE_SWITCH_ID)
      if (state) {
        // ⚠️ 判据用机器值 `=== 'enabled'`（⛔ 不是真值、也不是中文名）。
        state.checked = row.callback_state === 'enabled'
      }
      renderCallbackForm()

      const ip = at<HTMLTextAreaElement>(IP_INPUT_ID)
      if (ip) ip.value = row.ip_allowlist
    }

    /** 重新取一次那把应用并上屏（⛔ 唯一的取数出口）。 */
    async function reload(): Promise<void> {
      applyRow(await getWecomQrApp())
    }

    // ── 事件接线（⛔ 全部挂在**页面自己的节点**上：本页没有弹层，节点不会被搬走）─────────

    at<HTMLButtonElement>(CREATE_BUTTON_ID)?.addEventListener('click', () => {
      if (creating) return
      creating = true
      // ⚠️ **不传名字**：§8.2 只要求一颗「创建」按钮，默认名由后端给
      //    （`wecom_qr_app_views.DEFAULT_APP_NAME`）。
      void createWecomQrApp()
        .then((saved) => reload().then(() => msg(MESSAGES.created + `（应用名：${saved.name}）`)))
        // ⚠️ 失败**如实显示**后端文案：已有一把时后端回 `1000`「本租户已创建过该应用，
        //    不能重复创建」——那句话正是用户需要的解释，⛔ 不许换成一句笼统的"操作失败"。
        .catch((e: Error) => msg(e.message))
        .finally(() => {
          creating = false
        })
    })

    at<HTMLButtonElement>(COPY_SECRET_BUTTON_ID)?.addEventListener('click', () => {
      if (current === null) return
      const row = current
      // ⚠️ 复制的内容**从行数据取**（⛔ 不从 DOM 读）。
      copyTextToClipboard(credentialText(row), (ok) =>
        msg(ok ? '已复制 AppKey 与 AppSecret（两行）' : MESSAGES.copyFailed),
      )
    })

    /**
     * 停用 / 启用（`PATCH {state}`）。停用是**唯一**的关闭动作 ⇒ 二次确认里要说清后果。
     */
    at<HTMLButtonElement>(TOGGLE_BUTTON_ID)?.addEventListener('click', () => {
      if (current === null) return
      const row = current
      const to: 'enabled' | 'disabled' = row.state === 'enabled' ? 'disabled' : 'enabled'
      const text =
        to === 'disabled'
          ? `确定停用这把应用吗？停用后第三方当场调不通（所有调用回「该应用已停用」），回调推送也一起停。`
          : `确定启用这把应用吗？启用后第三方就能调它的接口了（推送是否发得出去还要看回调地址与开关）。`
      ;(layer.confirm as (text: string, o: unknown, ok: () => void) => void)(text, {}, () => {
        if (saving) return
        saving = true
        void updateWecomQrApp({ state: to })
          .then(() => reload())
          .then(() => msg(to === 'disabled' ? '已停用' : '已启用'))
          .catch((e: Error) => msg(e.message))
          .finally(() => {
            saving = false
          })
      })
    })

    /**
     * 轮换密钥（旧密钥**立即失效**，没有宽限期）。
     *
     * ⚠️ 确认文案里**必须**写出「旧密钥立刻作废 + 第三方当场调不通」——这一条没有放松
     * （先例：`pages/open-apps.ts::confirmRotate`）。
     * ⚠️ 在途闸门只包住那次请求（⛔ 不包住二次确认）：把闸门压在 `layer.confirm` 上会让
     * **点了取消之后**下一次点击静默无效（= 本仓反复堵的"点了没反应"）。
     */
    at<HTMLButtonElement>(ROTATE_BUTTON_ID)?.addEventListener('click', () => {
      if (current === null) return
      ;(layer.confirm as (text: string, o: unknown, ok: () => void) => void)(
        `确定轮换密钥吗？「旧密钥立刻作废」——正在跑的第三方集成会当场调不通，要有人去改它的配置。` +
          `（新密钥会写进来，本页随时看得到、也能一键复制。）`,
        {},
        () => {
          if (rotating) return
          rotating = true
          void rotateWecomQrAppSecret()
            .then(() => reload())
            .then(() => msg(MESSAGES.rotated))
            .catch((e: Error) => msg(e.message))
            .finally(() => {
              rotating = false
            })
        },
      )
    })

    // 回调地址的即时提示（边打边说；⛔ 它不是判据，见 `callbackUrlHint`）。
    at<HTMLInputElement>(CALLBACK_URL_INPUT_ID)?.addEventListener('input', () => {
      const hint = at(CALLBACK_HINT_ID)
      const input = at<HTMLInputElement>(CALLBACK_URL_INPUT_ID)
      if (hint && input) hint.textContent = callbackUrlHint(input.value)
    })

    /**
     * 保存回调设置（`PATCH`，两个键一起发）。
     *
     * ⚠️ **没有前端校验门**：地址是否合法**由后端判**（仅 https + SSRF），它拒的时候回业务码
     * `1000` + 一句中文 ⇒ 这里 `msg(e.message)` 把它**如实显示**。
     * ⚠️ 两个键**一起发**（地址与开关是同一份配置）：开关单独立刻生效会让"地址空 + 开关开"
     * 变成一个没人明确选过的中间态。
     */
    at<HTMLButtonElement>(CALLBACK_SAVE_BUTTON_ID)?.addEventListener('click', () => {
      if (current === null || saving) return
      // ⚠️ 判据是 DOM 的**此刻**勾选态（⛔ 不从"打开时的状态"推：用户改完开关再保存，
      //    那条路会保存成旧值 —— 静默不生效）。
      const state: WecomQrAppCallbackState = at<HTMLInputElement>(CALLBACK_STATE_SWITCH_ID)
        ?.checked
        ? 'enabled'
        : 'disabled'
      const url = at<HTMLInputElement>(CALLBACK_URL_INPUT_ID)?.value.trim() ?? ''
      saving = true
      void updateWecomQrApp({ callback_url: url, callback_state: state })
        .then(() => reload())
        .then(() => msg(callbackSaveReport(url, state)))
        .catch((e: Error) => msg(e.message))
        .finally(() => {
          saving = false
        })
    })

    /**
     * 保存 IP 白名单（`PATCH {ip_allowlist}`，**单独一个键**）。
     *
     * ⚠️ 与回调分开两次保存是有意的：两件事的后果不同（一个决定"推不推得出去"、
     *    一个决定"谁调得动"），合并成一颗按钮会让用户分不清自己刚改的是哪一件。
     * ⚠️ **空串是合法值**（= 不限制）⇒ 判据是"用户按了保存"，⛔ 不是"输入框非空"（E-0021）。
     */
    at<HTMLButtonElement>(IP_SAVE_BUTTON_ID)?.addEventListener('click', () => {
      if (current === null || saving) return
      const value = at<HTMLTextAreaElement>(IP_INPUT_ID)?.value.trim() ?? ''
      saving = true
      void updateWecomQrApp({ ip_allowlist: value })
        .then(() => reload())
        .then(() => msg(ipSaveReport(value)))
        .catch((e: Error) => msg(e.message))
        .finally(() => {
          saving = false
        })
    })

    at(DELIVERIES_TOGGLE_ID)?.addEventListener('click', () => {
      const body = at(DELIVERIES_BODY_ID)
      const open = body?.style.display === 'none'
      setDeliveriesOpen(open)
      // 点开就拉一次（照 `open-apps.ts:1124-1152` 的口径：不做定时轮询）。
      if (open) void loadDeliveries()
    })

    try {
      await reload()
    } catch (e) {
      msg(e instanceof Error ? e.message : MESSAGES.failed)
    }

    return () => {
      root.innerHTML = ''
    }
  },
} satisfies IslandPage
