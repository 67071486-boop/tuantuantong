/**
 * 【平台消息自动回复打手设置】页（island key = `system-platformautoreply`）——
 * 证据件标记 + **真 API**（T13）。
 *
 * 形状（逐字来自 `public/replica/system-platformautoreply.html`）：引文块 + 单表 +
 * 隐藏的「自动回复」弹层（`.modifyParamDiv`）+ 隐藏的「批量修改回复内容」弹层
 * （`#batchRelyContent`）。
 *
 * ## 为什么不再用 `artifactPage`
 *
 * 旧实现走 `artifactPage`：表格 `data: []` 写死、捕获阶段的「尚未开放」总闸把工具栏 /
 * 行内 / 开关的点击全吞掉（`src/layui/artifactPage.ts` 的 catch-all）⇒ 接真数据后整张表
 * 仍然恒空、按钮永远只弹提示。本页改成**自挂载**：`mountConfigLike`（取证据件 → 注入 →
 * `table.render` 一次 → `form.render` → 进 `onReady`），在 `onReady` 里自己挂
 * `table.on` / `form.on`、自己开弹层、自己保存。
 *
 * ## 与对标不同（每条都给理由；值纪律见 `AGENTS.md` §5 / D-0025）
 *
 * | # | 差异 | 理由 |
 * |---|---|---|
 * | 1 | **「专用人员类型 / 专用人员名称」整块删除**：表格少 2 列、弹层里那组 radio（`replyUserType`）与「指定专用人员」下拉（`inputOrderKefu`）**不渲染**（证据件里也删了，见该文件顶部注释） | 业务方 2026-10-27 原话「这一块删掉就行 我们用不上」。我方人员体系与对标不同，也不打算做这层「只为某个发单客服 / 接单销售回复」的指定 ⇒ **对标有、我们不做**，⛔ 不是「暂无后端所以空着」 |
 * | 2 | 表格列挂**适配字段**（`GameName` / `ReplyOccasion` / `ReplyMsg` / `PlatformName` / `ReplyStatus` / `PlatformAutoReplySetId`） | 证据件的开关模板（`#switchTpl1`）读的是对标自己的字段名与**反向编码**（`ReplyStatus == 0` 才启用）；我们把 API 行映射成模板认得的形状，模板本身**一个字没改** |
 * | 3 | 「图片」列是**函数 templet**（`查看图片` 链接） | 对标那列的函数模板没入库（`.tools/recon` 不进 git）⇒ **推断**：给一个能点的入口。有 `has_image` 时用**现签的短时效链接**开 `layer.photos`（既有媒体预览方式，同 `emp-settlementemp` 的收款码）；没有时只提示「未上传图片」，⛔ 不开空层 |
 * | 4 | 弹层「限定游戏」下拉多一项 **`全部游戏`（`value=""`）**，选项按**我方游戏**重灌 | 证据件那 14 个 `<option>` 是**对标租户自己的游戏配置、不是产品逻辑**（同结算记录页的既有处置）；我方模型 `game` 可空 = 全部游戏，不补这一项就**永远配不出「所有游戏」的规则** |
 * | 5 | 「回复时机」4 颗 radio **照对标渲染**，但只收 `0 接单后` / `10 申请验收后`；选 `15 提交异常后` / `20 申请撤销后` 时保存**明确提示尚未开放、零请求** | 发送引擎已上线（T14），但只认 `remote_status` 12 / 13 两档；15 / 20 上游没有对应的回读码，引擎不触发（见 `dailiantong_auto_reply.TRIGGER_BY_REMOTE_STATUS`）。按标准 §5：**不置灰、不隐藏**，点了给一句人话 |
 * | 6 | 「用在哪些平台」13 个 checkbox **照对标渲染**，但保存时**只认代练通（10）**；没勾代练通或勾了别的 ⇒ 明确提示、零请求 | 我方模型是**单个** `platform`（默认 10），而那 13 个是多选。v1 只落代练通（业务概念上这一页就是「代练通自动回复」）；勾别的不是静默丢弃，而是**明确拒**（写操作不假装成功，标准 §5） |
 * | 7 | 「上传图片」按钮：照对标渲染（`#upbtn` + `.pic` 缩略图位），**真上传**（B1）：点了打开文件选择 → `POST platform-auto-replies/upload-image/`（multipart）→ 成功后画缩略图、把 `image_key` 随规则保存；`.pic` 里有图时点开预览、没图时提示「未上传图片」 | 对标的上传实现（`upload.render`）没入库 ⇒ 用**原生 file input + fetch**（本页其余请求都走 `@/api/platformAutoReply*`）。对象名与类型/大小（≤2 MiB）都由**服务端**定（⛔ 客户端不拼 key）。上传中按钮禁用并显示「上传中…」，失败给中文原因、**不落规则**（⛔ 不假装成功） |
 * | 8 | 保存成功后弹本仓文案「已保存」 | 对标弹的是**它服务端**的 `data.msg`；本仓统一请求通道成功只回 `data`（`api/unwrap.ts`），拿不到那句 ⇒ 不自造一句谎称"已生效" |
 * | 9 | 新增行**默认启用**（不发 `is_enabled`，吃模型默认 `True`） | 证据件与对标 JS 都没给「新增时的初值」；同 `PlatformIntegration.is_enabled` 的既有先例（本仓自定，报告里登记） |
 * | 10 | 弹层尺寸 / 标题、批量弹层的按钮文案是本仓取值（`新增自动回复` / `修改自动回复` / `保存` / `取消`） | 对标弹层由**它的 JS** 动态 `layer.open`，那份 JS 没入库 ⇒ **推断**（尺寸按证据件表单的行数估）。⛔ 不把推断写成"照抄对标" |
 * | 11 | 「批量修改自动回复内容」= 把内容 PATCH 到**勾选的行**上（逐行一发） | 证据件的 `#batchRelyContent` 块自己那段说明就是这个语义（「所有已勾选的游戏消息设置，都将统一变更为刚保存的自动回复内容」）；字段只有内容一格 ⇒ 照它做。⚠️ 标题 / 按钮文案同差异 #10 |
 *
 * ## 生效口径（别读成「保存完就立刻发」）
 *
 * 发送引擎**已上线**（T14，`apps/business/dailiantong_auto_reply.py`），但它的触发**依赖
 * 常驻同步进程**：生产形态是 systemd 单元
 * `deploy/systemd/tuantuantong-dailiantong-sync.service` 跑的 `manage.py dailiantong_sync --loop`
 * （随发版安装 / 重启，见 `AGENTS.md` §5-A）。同步进程停着 ⇒ 保存的规则不会触发。
 *
 * 每轮回读之后，引擎只看**回读到的** `remote_status`：`12` ⇒「接单后」、`13` ⇒「申请验收后」；
 * 撤销中的单（`remote_cancel_status` 非空且非 10）一条都不发；同一 `(push, rule)` 只发一次
 * （`DailiantongAutoReplySent` 唯一约束）。页面上不改一句话：对标的引文块本来写的就是
 * 「当交易平台有对应动作时，将会自动回复消息给打手」——那是**产品承诺**，不是本页状态。
 *
 * ## 凭据 / PII
 *
 * 无凭据。规则正文（`content`）是租户自己的话术：渲染时一律过 `escapeHtml`。
 * ⚠️ `layer.photos` 的 `alt` 照旧过 `escapeHtml`，但 `src` **不能**（它是 URL，`&` 被转义成
 * `&amp;` 会弄坏预签名串——2026-10-28 真浏览器定案，见 `photoSrc`）。
 */
import { fetchAllGames, type ConfigRow } from '@/api/config'
import {
  createPlatformAutoReplyRule,
  deletePlatformAutoReplyRule,
  fetchPlatformAutoReplyRules,
  updatePlatformAutoReplyRule,
  uploadPlatformAutoReplyImage,
  type PlatformAutoReplyRulePayload,
  type PlatformAutoReplyRuleRow,
} from '@/api/platformAutoReplies'
import { findPanel, mountConfigLike, openPanelLayer } from '@/layui-island/config-replica'
import { escapeHtml } from '@/layui-island/escape'
import { islandMsg, type IslandPage } from '@/layui/island'
import type { LayuiModule } from '@/layui/loader'

/** 证据件名（= island key = `pages/<key>.ts` 的文件名，`mountConfigLike` 按它取 HTML）。 */
export const PAGE_KEY = 'system-platformautoreply'
/** 对标服务端渲染的 `<title>`（island 外壳全站共用一个静态 title）。 */
export const TITLE = '平台消息自动回复打手设置'

/** 表格 elem（对标 `#tableList`）与 `reloadData` / `table.on` / `checkStatus` 要的 **id**。 */
export const TABLE_ELEM = '#tableList'
export const TABLE_ID = 'tableList'

/** 「新增 / 修改」弹层容器：证据件里**只有 class、没有 id**（`.modifyParamDiv`）。 */
export const PANEL_CLASS = 'modifyParamDiv'
export const PANEL_SELECTOR = `.${PANEL_CLASS}`

/** 「批量修改回复内容」弹层容器（证据件里是 id）。⚠️ id 与选择器分成两个常量（E-0061 ①）。 */
export const BATCH_PANEL_ID = 'batchRelyContent'
export const BATCH_PANEL_SELECTOR = `#${BATCH_PANEL_ID}`

/** 「查看图片」链接的类名与选择器（拼进 templet 用类名、查点击目标用选择器）。 */
export const VIEW_IMAGE_CLASS = 'viewReplyImage'
export const VIEW_IMAGE_SELECTOR = `.${VIEW_IMAGE_CLASS}`

/** 弹层里图片预览位（证据件 `<span class="pic" style="cursor:pointer;"></span>`）。 */
export const PIC_CLASS = 'pic'
export const PIC_SELECTOR = `.${PIC_CLASS}`

/** 弹层里的控件名（逐字来自证据件；抄错一个字母就取不到值）。 */
export const GAME_SELECT_ID = 'gameName'
export const CONTENT_FIELD = 'replycontent'
export const BATCH_CONTENT_FIELD = 'batchContent'
export const PLATFORM_CHECKBOX_NAME = 'platformIds'
export const PTID_FIELD = 'ptid'
export const UPLOAD_BUTTON_ID = 'upbtn'
/** 弹层里那颗「保存设置」（证据件是 `<a class="… saveBtn">`，不是 `lay-submit`）。 */
export const SAVE_BUTTON_CLASS = 'saveBtn'

/** 「限定游戏」下拉的第一项：`null` = 全部游戏（我方补充项，见文件头差异 #4）。 */
export const GAME_ALL_LABEL = '全部游戏'

/** 回复正文上限：证据件 `#replycontent` / `#batchContent` 的 `maxlength="200"`。 */
export const CONTENT_MAX_LENGTH = 200

/** 页面自己插进根容器的隐藏文件选择框（证据件里没有这个节点，差异 #7）。 */
export const UPLOAD_INPUT_ID = 'replyImageInput'

/** 上传按钮的文案：证据件 `#upbtn` 原文是「上传」（⛔ 别改成「上传图片」）。 */
export const UPLOAD_BUTTON_TEXT = '上传'
export const UPLOAD_BUTTON_BUSY_TEXT = '上传中…'

/**
 * 单张规则图上限（**2 MiB**）：与后端 `platform_auto_reply_views.MAX_RULE_IMAGE_BYTES` 同值。
 *
 * ⚠️ 这是跨层契约：后端那道 2.5 MiB 的请求体墙（E-0095）会让更大的 multipart 请求拿到一个
 * **没有中文原因的 400**，所以前端先拦一次、把原因说清楚。
 * ⚠️ 必须与后端**同时**改（改一处就会让「前端放行、后端拒」或反之）。
 */
export const MAX_REPLY_IMAGE_BYTES = 2 * 1024 * 1024

/** file input 的 `accept`：与后端魔数白名单一致（jpg / png）。 */
export const UPLOAD_IMAGE_ACCEPT = 'image/jpeg,image/png'

/**
 * 证据件 `name="replyOcc"` 的 4 个时机（`value` + `title`，**逐字照抄**）。
 *
 * ⚠️ 码值**不是**「10 / 20」：证据件原文是 `0` 接单后 / `10` 申请验收后 /
 * `15` 提交异常后 / `20` 申请撤销后（`value` 就是对标表单提交的值）。
 * `SUPPORTED_TRIGGERS` 才是本版真做的两个。
 */
export const TRIGGER_OPTIONS: ReadonlyArray<{ value: number; title: string }> = [
  { value: 0, title: '接单后' },
  { value: 10, title: '申请验收后' },
  { value: 15, title: '提交异常后' },
  { value: 20, title: '申请撤销后' },
]

/** 本版真做的两个时机（其余两颗 radio 照渲染、保存时拒）。 */
export const SUPPORTED_TRIGGERS: readonly number[] = [0, 10]

/** 「用在哪些平台」里本版只认的平台（代练通 = 10，与模型默认值同源）。 */
export const SUPPORTED_PLATFORM = 10
export const SUPPORTED_PLATFORM_LABEL = '代练通'

/** 证据件开关模板的 `lay-filter`（`#switchTpl1`：`lay-filter="staChecked"`）。 */
export const SWITCH_FILTER = 'staChecked'

/** 表格行里的「启用状态」列标题（逐字照证据件，含那颗 `lay-tips`）。 */
const STATUS_TITLE = '<span lay-tips="绿色代表启用时">启用状态</span>'

/** 行内操作列 / 工具栏的 `lay-event`（逐字照证据件 `#baroperating` / `#toolbarDemo`）。 */
export const EVENT_REFRESH = 'refresh'
export const EVENT_ADD = 'addBtn'
export const EVENT_BATCH_EDIT = 'batchEdit'
export const EVENT_EDIT = 'submitEdit'
export const EVENT_DELETE = 'submitDel'

/** 未做时机的固定提示（⛔ 不自造"成功"，只说明没做）。 */
export function unsupportedTriggerText(title: string): string {
  return `「${title}」尚未开放：本版只做「接单后」与「申请验收后」两种时机`
}

/** 批量修改的弹层标题 / 按钮（本仓取值，见文件头差异 #10）。 */
export const BATCH_LAYER_TITLE = '批量修改自动回复内容'
export const BATCH_SAVE_TEXT = '保存'
export const BATCH_CANCEL_TEXT = '取消'

/* ────────────────────────────── 表格 ────────────────────────────── */

/** 表格行：接口字段（spread 保留）+ 证据件 `cols[].field` / 开关模板认得的名字。 */
export type AutoReplyTableRow = PlatformAutoReplyRuleRow & {
  /** 对标列 `GameName`：游戏名；`null`（全部游戏）显示我们自己补的那句文案。 */
  GameName: string
  /** 对标列 `ReplyOccasion`：时机中文（后端 `trigger_name`）。 */
  ReplyOccasion: string
  /** 对标列 `ReplyMsg`：正文（渲染时过 `escapeHtml`）。 */
  ReplyMsg: string
  /** 对标列 `AppleImageUrl`：有图时给现签链接、没有给空串（显示由函数 templet 决定）。 */
  AppleImageUrl: string
  /** 对标列 `PlatformName`：平台中文（后端 `platform_label`）。 */
  PlatformName: string
  /** `#switchTpl1`：对标 `ReplyStatus == 0` 才 checked ⇒ 0 = 启用。 */
  ReplyStatus: 0 | 1
  /** `#switchTpl1` 的 `value="{{d.PlatformAutoReplySetId}}"`（= 行主键）。 */
  PlatformAutoReplySetId: number
}

/** API 行 → 表格行（**只做名字与编码方向的对齐**，不改任何语义）。 */
export function ruleToTableRow(row: PlatformAutoReplyRuleRow): AutoReplyTableRow {
  return {
    ...row,
    GameName: row.game_id === null ? GAME_ALL_LABEL : row.game_name,
    ReplyOccasion: row.trigger_name,
    ReplyMsg: row.content,
    AppleImageUrl: row.has_image ? row.image_url : '',
    PlatformName: row.platform_label,
    ReplyStatus: row.is_enabled ? 0 : 1,
    PlatformAutoReplySetId: row.id,
  }
}

/**
 * `layer.photos` 的 `src`：**原样 URL + 只编码会破出属性的字符**。
 *
 * ⚠️ 为什么**不能**像 `alt` 那样过 `escapeHtml`（2026-10-28 真浏览器定案）：
 * vendored layui 的 `layer.photos` 把同一个 `src` 串**用两次**——
 * ① `new Image().src = src` 预加载（原始 JS 串，**不经 HTML 实体解码**）；
 * ② 拼进 `content: '<img src="…">'`（这里才需要 HTML 安全）。
 * 过 `escapeHtml` 会把 `&` 变成 `&amp;`：①拿到的就是坏 URL ⇒ 预签名串的签名参数被改 ⇒
 * COS 回 403 ⇒ layui 弹「当前图片地址异常」（Playwright 抓包：请求 URL 里真的是 `&amp;`）。
 * ⇒ 只把 `"` `'` `<` `>` 百分号编码（合法 URL 里本就不该裸着出现；预签名串已由 SDK 编码），
 * `&` = 必须原样保留。
 */
export function photoSrc(url: string): string {
  return url.replace(/["'<>]/g, (ch) => `%${ch.charCodeAt(0).toString(16).toUpperCase()}`)
}

/** 「图片」格的**函数模板**（见文件头差异 #3）。
 *
 * 一律给一个可点的「查看图片」：没有图时点它只提示「未上传图片」
 * （比"这一格什么都没有"更容易让人确认"确实没传"，也避免同一列两种视觉）。
 */
export function imageCell(row: AutoReplyTableRow): string {
  return (
    `<a class="${VIEW_IMAGE_CLASS}" style="cursor:pointer"` +
    ` data-id="${row.id}" lay-tips="查看这条规则的回复图片">查看图片</a>`
  )
}

/**
 * 表格列（顺序 / 标题 / 宽度 / 对齐 / 开关模板引用**逐条照**对标 `PlatformAutoReply.js`
 * 的 `cols`，**减去**「专用人员类型 / 专用人员名称」两列——业务方裁定删除，
 * 见文件头差异 #1）。
 */
export function buildRuleCols(): Record<string, unknown>[] {
  return [
    { type: 'checkbox', width: 50, fixed: 'left' },
    { field: 'GameName', title: '限定游戏', width: 130, align: 'center' },
    { field: 'ReplyOccasion', title: '回复时机', width: 125, align: 'center' },
    { field: 'ReplyMsg', title: '回复内容', align: 'left' },
    {
      field: 'AppleImageUrl',
      title: '图片',
      width: 80,
      align: 'left',
      templet: (row: AutoReplyTableRow) => imageCell(row),
    },
    { field: 'PlatformName', title: '启用平台', width: 400, align: 'left' },
    {
      field: 'ReplyStatus',
      title: STATUS_TITLE,
      width: 105,
      align: 'center',
      templet: '#switchTpl1',
    },
    { title: '操作', width: 160, align: 'center', fixed: '', toolbar: '#baroperating' },
  ]
}

/** 一个 `table.render` 的选项子集（逐条照抄对标页面 JS）。 */
type TableOptions = {
  elem: string
  toolbar?: string
  height?: number | string
  page?: false | { limit: number; limits: number[] }
  defaultToolbar?: string[]
  totalRow?: boolean
  cols: [Record<string, unknown>[]]
}

/** 对标的表格配置（`page: false` 照对标；列由 `buildRuleCols` 给）。 */
const TABLES: TableOptions[] = [
  {
    elem: TABLE_ELEM,
    toolbar: '#toolbarDemo',
    height: 'full',
    page: false,
    // 对标同一 `render` 对象里写两次、**后写的生效** ⇒ 本页是 `['filter']`。
    defaultToolbar: ['filter'],
    cols: [buildRuleCols()],
  },
]

/** 导出给 spec 做「与对标 `cols` 不漂移」的对拍（`__tests__/system-platformautoreply.spec.ts`）。 */
export const TABLES_FOR_TEST: TableOptions[] = TABLES

/* ────────────────────────────── 纯函数（弹层取值 / 校验） ────────────────────────────── */

/** 弹层里读回来的原始值（未校验）。 */
export type PanelValues = {
  /** `null` = 全部游戏。 */
  game: number | null
  /** 没勾任何时机时是 `null`。 */
  trigger: number | null
  /** 时机 radio 的 `title`（报错文案要用它，从 DOM 取而不是抄第二份码表）。 */
  triggerTitle: string
  content: string
  /** 「用在哪些平台」勾了哪些 id。 */
  platforms: number[]
}

function gameIdFromValue(value: string): number | null {
  if (value === '') return null
  const id = Number(value)
  return Number.isInteger(id) && id > 0 ? id : null
}

/** 从弹层节点读出用户填的值（纯 DOM 读取，不做校验）。 */
export function readPanelValues(panel: HTMLElement): PanelValues {
  const select = panel.querySelector<HTMLSelectElement>(`select#${GAME_SELECT_ID}`)
  const checked = panel.querySelector<HTMLInputElement>('input[name="replyOcc"]:checked')
  const textarea = panel.querySelector<HTMLTextAreaElement>(`#${CONTENT_FIELD}`)
  const platforms = Array.from(
    panel.querySelectorAll<HTMLInputElement>(`input[name="${PLATFORM_CHECKBOX_NAME}"]`),
  )
    .filter((input) => input.checked)
    .map((input) => Number(input.value))
    .filter((id) => Number.isInteger(id))
  return {
    game: gameIdFromValue(select?.value ?? ''),
    trigger: checked ? Number(checked.value) : null,
    triggerTitle: checked?.getAttribute('title') ?? '',
    content: textarea?.value ?? '',
    platforms,
  }
}

/**
 * 校验弹层值 ⇒ 返回**一句人话**（`''` = 通过）。纯函数，测试直接驱动它。
 *
 * 顺序照「先拦最不可能自己恢复的」：时机（选错的概率最高）→ 内容 → 平台。
 */
export function validatePanelValues(values: PanelValues): string {
  if (values.trigger === null) return '请选择回复时机'
  if (!SUPPORTED_TRIGGERS.includes(values.trigger)) {
    return unsupportedTriggerText(values.triggerTitle || String(values.trigger))
  }
  const content = values.content.trim()
  if (content === '') return '回复内容不能为空'
  if (content.length > CONTENT_MAX_LENGTH) {
    return `回复内容不能超过 ${CONTENT_MAX_LENGTH} 个字符`
  }
  if (!values.platforms.includes(SUPPORTED_PLATFORM)) {
    return `请勾选「${SUPPORTED_PLATFORM_LABEL}」（本版只支持这一个平台）`
  }
  if (values.platforms.some((id) => id !== SUPPORTED_PLATFORM)) {
    return `本版只支持「${SUPPORTED_PLATFORM_LABEL}」，其它平台尚未开放`
  }
  return ''
}

/**
 * 校验通过后要发的载荷。
 *
 * `imageKey` 只传**本次弹层里刚上传成功**的对象名（见文件头差异 #7）：
 * - `null` / 不传 ⇒ **不带 `image_key`**（PATCH 语义 = 不改，库里那张原样留着）；
 * - 字符串 ⇒ 带上它（`POST` 新增或 `PATCH` 换图）。
 * ⚠️ 本版没有「清空图片」入口（证据件也没有），所以不会发空串。
 */
export function panelPayload(
  values: PanelValues,
  imageKey: string | null = null,
): PlatformAutoReplyRulePayload {
  const payload: PlatformAutoReplyRulePayload = {
    platform: SUPPORTED_PLATFORM,
    game: values.game,
    trigger: values.trigger as number,
    content: values.content.trim(),
  }
  if (imageKey !== null) payload.image_key = imageKey
  return payload
}

/**
 * 把「限定游戏」下拉换成**我方游戏**（第一项 = 全部游戏）。
 *
 * 证据件的 14 个 `<option>` 是**对标租户自己的游戏数据**（同结算记录页的既有处置），
 * 结构 `select#gameName[name=gameName][lay-filter=selectGame]` 与对标逐字一致。
 * `selected` 传 `null` ⇒ 选「全部游戏」。
 */
export function fillGameSelect(
  panel: HTMLElement,
  form: LayuiModule,
  games: readonly ConfigRow[],
  selected: number | null,
): void {
  const select = panel.querySelector<HTMLSelectElement>(`select#${GAME_SELECT_ID}`)
  if (!select) return
  // 游戏名是用户输入 ⇒ 拼进 innerHTML 前必须转义（`escapeHtml` 是唯一出处）。
  select.innerHTML =
    `<option value="">${GAME_ALL_LABEL}</option>` +
    games.map((g) => `<option value="${g.id}">${escapeHtml(g.name)}</option>`).join('')
  select.value = selected === null ? '' : String(selected)
  // 找不到那款（被删了）时回落到「全部游戏」，⛔ 不静默停在第一项上。
  if (select.selectedIndex < 0) select.value = ''
  ;(form.render as (type?: string) => void)('select')
}

/* ────────────────────────────── 页面 ────────────────────────────── */

function errText(error: unknown): string {
  return error instanceof Error ? error.message : '操作失败'
}

async function mountPage(
  root: HTMLElement,
  ctx: Parameters<IslandPage['mount']>[1],
): Promise<() => void> {
  return mountConfigLike(
    root,
    {
      key: PAGE_KEY,
      title: TITLE,
      tableSpec: {
        elem: TABLE_ELEM,
        toolbar: '#toolbarDemo',
        height: 'full',
        page: false,
        defaultToolbar: ['filter'],
        cols: TABLES[0]!.cols,
      },
      onReady: ({ root: pageRoot, layui, form, table, layer }) => {
        const msg = (text: string): void =>
          islandMsg(layer as unknown as Record<string, unknown>, text)
        const renderForm = (type?: string): void =>
          (form.render as (t?: string) => void)(type)
        const reloadData = (rows: AutoReplyTableRow[]): void =>
          (table.reloadData as (id: string, options: unknown) => void)(TABLE_ID, { data: rows })

        /** 正在编辑的那一行；`null` = 新增。 */
        let editing: PlatformAutoReplyRuleRow | null = null
        /** 当前打开的「新增/修改」弹层索引（关闭 / 卸载时用）。 */
        let editLayerIndex: number | null = null
        /** 当前打开的批量弹层索引。 */
        let batchLayerIndex: number | null = null
        /**
         * 保存是否在途（**承重闸**）：连点两次会发两发 POST，而这张表不限「同平台 / 同游戏唯一」
         * ⇒ 多出一条重复规则。双保险 = 旗标 + 保存按钮禁用（同发单页）。
         */
        let submitting = false
        /** 批量修改的保存闸：`layer` 的「保存」按钮连点不许跑第二遍逐行 PATCH。 */
        let batchSubmitting = false
        /** 本页游戏字典（弹层「限定游戏」用）；取失败就是空数组 + 一次明确提示。 */
        let games: ConfigRow[] = []
        /**
         * 行主键 → 接口行（`查看图片` 要用 `image_url`，而 DOM 里只有 `data-id`）。
         * ⚠️ 每次重取列表都整份重建，⛔ 不要只增不删（删掉的行会留在里面）。
         */
        let rowsById = new Map<number, PlatformAutoReplyRuleRow>()
        /**
         * 本次弹层里**刚上传成功**的对象名；`null` = 没换图（保存时不发 `image_key`）。
         * ⚠️ 每次 `openForm` 都复位：上一条规则上传的图不能跟着到另一条规则上。
         */
        let pendingImageKey: string | null = null
        /** 本次上传成功后的现签读链接（缩略图 / 点开预览用）。 */
        let pendingImageUrl = ''
        /** 上传是否在途（**承重闸**：按钮禁用 + 不让第二个文件同时上传）。 */
        let uploadingImage = false

        async function reloadList(): Promise<void> {
          const rows = await fetchPlatformAutoReplyRules()
          rowsById = new Map(rows.map((row) => [row.id, row]))
          reloadData(rows.map(ruleToTableRow))
          // 表格里的开关是 layui 的渲染控件：重绘后必须再 render 一次，否则还停在被 CSS
          // 藏起来的原生 checkbox 上（点不动）。与 `mountConfigLike` 首次 render 同一个出口。
          renderForm()
        }

        async function loadGames(): Promise<void> {
          games = await fetchAllGames()
        }

        function panelOf(): HTMLElement | null {
          return findPanel(pageRoot, PANEL_SELECTOR)
        }

        /** 把「保存/取消」按钮那一格里的内容清除（图片预览位）。 */
        function setPic(panel: HTMLElement, row: PlatformAutoReplyRuleRow | null): void {
          const pic = panel.querySelector<HTMLElement>(PIC_SELECTOR)
          if (!pic) return
          if (row?.has_image && row.image_url) {
            pic.innerHTML =
              `<img src="${escapeHtml(row.image_url)}" alt="回复图片"` +
              ' style="height:48px;vertical-align:middle">'
            pic.setAttribute('data-has-image', '1')
          } else {
            pic.innerHTML = ''
            pic.removeAttribute('data-has-image')
          }
        }

        /** 把上传按钮恢复成「上传」并解除禁用（每次开弹层 / 上传结束都走这里）。 */
        function resetUploadButton(panel: HTMLElement): void {
          const button = panel.querySelector<HTMLElement>(`#${UPLOAD_BUTTON_ID}`)
          if (!button) return
          button.textContent = UPLOAD_BUTTON_TEXT
          button.classList.remove('layui-btn-disabled')
          button.removeAttribute('aria-disabled')
        }

        /**
         * 真上传一张图（B1）：`POST platform-auto-replies/upload-image/`。
         *
         * 三条都不许省：
         * 1. **本地先判大小**（`MAX_REPLY_IMAGE_BYTES`）：后端那道 2.5 MiB 请求体墙会给超限请求
         *    一个没有中文原因的 400（E-0095）⇒ 先拦下来说清楚，别让用户猜；
         * 2. 上传中**禁用按钮 + 显示「上传中…」**，并用 `uploadingImage` 挡住第二个文件；
         * 3. 成功才把 `image_key` 记进本弹层；失败**不写** `pendingImageKey`（保存不会带坏图）。
         */
        async function uploadImage(file: File): Promise<void> {
          if (uploadingImage) return
          if (file.size > MAX_REPLY_IMAGE_BYTES) {
            msg('图片超过 2 MB，请压缩后再上传')
            return
          }
          const panel = panelOf()
          const button = panel?.querySelector<HTMLElement>(`#${UPLOAD_BUTTON_ID}`)
          uploadingImage = true
          if (button) {
            button.classList.add('layui-btn-disabled')
            button.setAttribute('aria-disabled', 'true')
            button.textContent = UPLOAD_BUTTON_BUSY_TEXT
          }
          try {
            const uploaded = await uploadPlatformAutoReplyImage(file)
            // ⚠️ 上传期间用户可能关了弹层 / 换了另一条规则 ⇒ 只有**同一个弹层还开着**才落结果，
            //    否则会把 A 规则刚传的图带到 B 规则上（`openForm` 会复位，这里不抢它的活）。
            if (panelOf() === panel) {
              pendingImageKey = uploaded.image_key
              pendingImageUrl = uploaded.url
              // 缩略图与「查看图片」同一套渲染口径：`src` / `alt` 都过 `escapeHtml`。
              const pic = panel?.querySelector<HTMLElement>(PIC_SELECTOR)
              if (pic) {
                pic.innerHTML =
                  `<img src="${escapeHtml(pendingImageUrl)}" alt="回复图片"` +
                  ' style="height:48px;vertical-align:middle">'
                pic.setAttribute('data-has-image', '1')
              }
              msg('图片已上传，保存后生效')
            }
          } catch (err: unknown) {
            msg(`上传失败：${errText(err)}`)
          } finally {
            uploadingImage = false
            if (panel) resetUploadButton(panel)
          }
        }

        /*
         * 隐藏的文件选择框：证据件里没有它（差异 #7）⇒ 页面自己插一个到根容器。
         * ⚠️ 必须由**用户手势**（点 `#upbtn`）触发 `.click()`，否则浏览器会拦下弹框。
         */
        const fileInput = document.createElement('input')
        fileInput.type = 'file'
        fileInput.id = UPLOAD_INPUT_ID
        fileInput.accept = UPLOAD_IMAGE_ACCEPT
        fileInput.style.display = 'none'
        pageRoot.appendChild(fileInput)
        const onFileChange = (): void => {
          const file = fileInput.files?.[0]
          // 清空 value：同一个文件被改过之后还能再选一次（否则 `change` 不再触发）。
          fileInput.value = ''
          if (file) void uploadImage(file)
        }
        fileInput.addEventListener('change', onFileChange)

        /**
         * 打开「新增 / 修改」弹层。
         *
         * ⚠️ **顺序是硬要求：「先预填、再 open」**——`layer.open({type:1, content: …})` 会把
         * `.modifyParamDiv` 搬进 `.layui-layer-content`（挂在 `document.body` 下），
         * 先 open 再填就会在 `root` 里找不到节点（先例 `system-tripartiteplatformsetting.ts`）。
         * radio / select 的勾选态也要在 open 之前设好并 `form.render(...)`——搬走之后
         * layui 的 `form.render` 按 `.layui-form` 作用域找节点，找不到就画不出来。
         */
        function openForm(row: PlatformAutoReplyRuleRow | null): void {
          editing = row
          if (editLayerIndex !== null) {
            ;(layer.close as (index: number) => void)(editLayerIndex)
            editLayerIndex = null
          }
          const panel = panelOf()
          if (!panel) {
            msg('弹窗内容缺失，请联系管理员')
            return
          }
          // 换了一条规则 ⇒ 上一次的「待保存新图」必须作废（⛔ 不能跟着到另一条规则上）。
          pendingImageKey = null
          pendingImageUrl = ''
          uploadingImage = false
          resetUploadButton(panel)
          const gameId = row?.game_id ?? null
          fillGameSelect(panel, form, games, gameId)
          // 时机：先全部清掉再勾（否则证据件里没有默认勾选、上一次的值会留着）。
          for (const radio of panel.querySelectorAll<HTMLInputElement>('input[name="replyOcc"]')) {
            radio.checked = row !== null && Number(radio.value) === row.trigger
          }
          const content = panel.querySelector<HTMLTextAreaElement>(`#${CONTENT_FIELD}`)
          if (content) content.value = row?.content ?? ''
          // 「用在哪些平台」：本版只认代练通 ⇒ 打开时按该行平台勾好，其余一律不勾。
          const platform = row?.platform ?? SUPPORTED_PLATFORM
          for (const box of panel.querySelectorAll<HTMLInputElement>(
            `input[name="${PLATFORM_CHECKBOX_NAME}"]`,
          )) {
            box.checked = Number(box.value) === platform
          }
          // `#ptid` 是**弹层节点外面**的那两个隐藏域之一（证据件把它放在 `<form>` 里、
          // `.modifyParamDiv` 的**兄弟**位置）⇒ 必须从页根找，⛔ 不能 `panel.querySelector`。
          // 它只用于「和证据件保持同形」；页面逻辑用的是闭包里的 `editing`。
          const ptid = pageRoot.querySelector<HTMLInputElement>(`#${PTID_FIELD}`)
          if (ptid) ptid.value = row ? String(row.id) : '0'
          setPic(panel, row)
          renderForm()
          editLayerIndex = openPanelLayer(
            pageRoot,
            layui,
            layer,
            {
              // 标题是本仓取值（差异 #10）：对标弹层由它的 JS 动态 open，那份 JS 没入库。
              title: row ? '修改自动回复' : '新增自动回复',
              shade: [0.3, '#333'],
              shadeClose: false,
              // 桌面后台（本项目只做 PC）⇒ 证据件表单的固定尺寸估算。
              area: ['676px', '520px'],
              skin: 'layui-layer-lan',
              anim: 8,
            },
            PANEL_SELECTOR,
          )
        }

        /** 「保存设置」：校验 → POST / PATCH → 关层 → 重取列表 → 提示。 */
        function submitForm(): void {
          if (submitting) return
          const panel = panelOf()
          if (!panel) return
          const values = readPanelValues(panel)
          const error = validatePanelValues(values)
          if (error !== '') {
            msg(error)
            return
          }
          const payload = panelPayload(values, pendingImageKey)
          const target = editing
          // 双保险：旗标挡住第二次点击，按钮禁用让人**看得见**它已经点过了。
          submitting = true
          const saveButton = panel.querySelector<HTMLElement>(`.${SAVE_BUTTON_CLASS}`)
          if (saveButton) {
            saveButton.classList.add('layui-btn-disabled')
            saveButton.setAttribute('aria-disabled', 'true')
          }
          const finish = (): void => {
            submitting = false
            if (saveButton) {
              saveButton.classList.remove('layui-btn-disabled')
              saveButton.removeAttribute('aria-disabled')
            }
          }
          const request = target
            ? updatePlatformAutoReplyRule(target.id, payload)
            : createPlatformAutoReplyRule(payload)
          // ⚠️ 两条失败**分开说**：保存已经成功、只是列表刷新失败时，如果也报「保存失败」，
          // 用户会以为没保存而**再提交一次**（弹层这时已经关了）。
          void request.then(
            () => {
              finish()
              if (editLayerIndex !== null) {
                ;(layer.close as (index: number) => void)(editLayerIndex)
                editLayerIndex = null
              }
              editing = null
              void reloadList().then(
                () => msg('已保存'),
                (err: unknown) => msg(`已保存；但列表刷新失败：${errText(err)}`),
              )
            },
            (err: unknown) => {
              finish()
              msg(`保存失败：${errText(err)}`)
            },
          )
        }

        /** 批量修改：把内容 PATCH 到**勾选的行**上（证据件 `#batchRelyContent` 的说明语义）。 */
        function openBatch(rows: AutoReplyTableRow[]): void {
          if (rows.length === 0) {
            msg('请勾选要操作的数据行')
            return
          }
          if (batchLayerIndex !== null) {
            ;(layer.close as (index: number) => void)(batchLayerIndex)
            batchLayerIndex = null
          }
          const panel = findPanel(pageRoot, BATCH_PANEL_SELECTOR)
          if (!panel) {
            msg('弹窗内容缺失，请联系管理员')
            return
          }
          const textarea = panel.querySelector<HTMLTextAreaElement>(`#${BATCH_CONTENT_FIELD}`)
          if (textarea) textarea.value = ''

          const submit = (index: number): void => {
            const content = textarea?.value.trim() ?? ''
            if (content === '') {
              msg('回复内容不能为空')
              return
            }
            if (content.length > CONTENT_MAX_LENGTH) {
              msg(`回复内容不能超过 ${CONTENT_MAX_LENGTH} 个字符`)
              return
            }
            if (batchSubmitting) return
            batchSubmitting = true
            const batchButton = document.querySelector<HTMLElement>('.layui-layer-btn0')
            if (batchButton) {
              batchButton.classList.add('layui-btn-disabled')
              batchButton.setAttribute('aria-disabled', 'true')
            }
            // 逐行 PATCH（每行一条留痕；不用 Promise.all：失败时能说清是"保存了一部分"）。
            void (async () => {
              try {
                let done = 0
                for (const row of rows) {
                  try {
                    await updatePlatformAutoReplyRule(row.id, { content })
                    done += 1
                  } catch (err: unknown) {
                    msg(
                      `已更新 ${done} / ${rows.length} 条，第 ${done + 1} 条失败：${errText(err)}`,
                    )
                    await reloadList().catch(() => undefined)
                    return
                  }
                }
                ;(layer.close as (i: number) => void)(index)
                batchLayerIndex = null
                await reloadList().then(
                  () => msg('已保存'),
                  (err: unknown) => msg(`已保存；但列表刷新失败：${errText(err)}`),
                )
              } finally {
                batchSubmitting = false
                if (batchButton) {
                  batchButton.classList.remove('layui-btn-disabled')
                  batchButton.removeAttribute('aria-disabled')
                }
              }
            })()
          }

          const node = findPanel(pageRoot, BATCH_PANEL_SELECTOR)
          if (!node) {
            msg('弹窗内容缺失，请联系管理员')
            return
          }
          const jq = (layui as unknown as { $: (el: Element) => unknown }).$(node)
          batchLayerIndex = (layer.open as (options: unknown) => number)({
            type: 1,
            title: BATCH_LAYER_TITLE,
            area: ['600px', '360px'],
            shade: [0.3, '#333'],
            shadeClose: false,
            skin: 'layui-layer-lan',
            anim: 5,
            content: jq,
            btn: [BATCH_SAVE_TEXT, BATCH_CANCEL_TEXT],
            yes: (index: number) => submit(index),
            btn2: (index: number) => {
              ;(layer.close as (i: number) => void)(index)
              batchLayerIndex = null
            },
            end: () => {
              // 弹层销毁后把节点收回 `<form>`（下一个弹层还要用它）。照 `openPanelLayer` 的口径。
              const host = pageRoot.querySelector<HTMLElement>('form.layui-form') ?? pageRoot
              if (node.parentElement !== host) host.appendChild(node)
              batchLayerIndex = null
            },
          })
        }

        /** 删除（确认语是本仓措辞：对标 JS 没入库，⛔ 不冒充照抄）。 */
        function removeRow(row: PlatformAutoReplyRuleRow): void {
          ;(layer.confirm as (
            text: string,
            options: unknown,
            onConfirm: (index: number) => void,
          ) => void)(
            '是否确认删除这条自动回复？',
            { btn: ['确认删除', '点错了'] },
            (index: number) => {
              ;(layer.close as (i: number) => void)(index)
              void deletePlatformAutoReplyRule(row.id).then(
                () => {
                  void reloadList().then(
                    () => msg('已删除'),
                    (err: unknown) => msg(errText(err)),
                  )
                },
                (err: unknown) => msg(errText(err)),
              )
            },
          )
        }

        /* ── 表格：工具栏 / 行内按钮 ── */
        ;(table.on as (event: string, cb: (obj: unknown) => void) => void)(
          `toolbar(${TABLE_ID})`,
          (obj: unknown) => {
            const event = (obj as { event?: string }).event
            if (event === EVENT_REFRESH) {
              void reloadList().catch((err: unknown) => msg(errText(err)))
            } else if (event === EVENT_ADD) {
              openForm(null)
            } else if (event === EVENT_BATCH_EDIT) {
              const rows =
                ((table.checkStatus as (id: string) => { data?: AutoReplyTableRow[] })(
                  TABLE_ID,
                ).data ?? []) as AutoReplyTableRow[]
              openBatch(rows)
            }
          },
        )
        ;(table.on as (event: string, cb: (obj: unknown) => void) => void)(
          `tool(${TABLE_ID})`,
          (obj: unknown) => {
            const typed = obj as { event?: string; data?: PlatformAutoReplyRuleRow }
            const row = typed.data
            if (!row) return
            if (typed.event === EVENT_EDIT) openForm(row)
            else if (typed.event === EVENT_DELETE) removeRow(row)
          },
        )

        /* ── 行内启用开关：勾选态 = 后端布尔（模板的反向编码只在 `ruleToTableRow` 里）── */
        ;(form.on as (event: string, cb: (obj: unknown) => void) => void)(
          `switch(${SWITCH_FILTER})`,
          (obj: unknown) => {
            const elem = (obj as { elem?: HTMLInputElement }).elem
            if (!elem) return
            const rowId = Number(elem.value)
            if (!Number.isInteger(rowId) || rowId <= 0) return
            const checked = elem.checked
            void updatePlatformAutoReplyRule(rowId, { is_enabled: checked }).then(
              () => {
                void reloadList().catch((err: unknown) => msg(errText(err)))
              },
              (err: unknown) => {
                // 失败时把勾选态拨回去（不留一个"看起来保存了"的假象），再以服务端为准重取。
                elem.checked = !checked
                msg(errText(err))
                void reloadList().catch(() => undefined)
              },
            )
          },
        )

        /* ── 表格里的「查看图片」（委托挂在根容器上；行重绘后依然有效）── */
        const onPageClick = (event: Event): void => {
          const imageLink = (event.target as HTMLElement | null)?.closest?.(
            VIEW_IMAGE_SELECTOR,
          )
          if (!(imageLink instanceof HTMLElement)) return
          event.preventDefault()
          openImage(rowsById.get(Number(imageLink.dataset.id)) ?? null)
        }
        pageRoot.addEventListener('click', onPageClick)

        /* ── 弹层里的 保存 / 上传 / 图片预览 ──
         * ⚠️ **委托必须挂在这个节点自己身上**：`layer.open({type:1, content: …})` 会把
         * `.modifyParamDiv` **搬进 `.layui-layer-content`（document.body 下）**，它已经不在
         * `pageRoot` 里了 ⇒ 挂在根容器上的监听收不到弹层里的点击（实测：保存 / 上传 / 图片
         * 预览三个入口全部静默失效）。先例见 `system-tripartiteplatformsetting.ts` 的
         * `onPanelClick`。
         */
        const onPanelClick = (event: Event): void => {
          const target = event.target as HTMLElement | null
          if (target?.closest?.(`#${UPLOAD_BUTTON_ID}`)) {
            event.preventDefault()
            // 上传在途时再点不弹第二次选择框（按钮也已经禁用，这里再兜一道）。
            if (uploadingImage) return
            fileInput.click()
            return
          }
          const pic = target?.closest?.(PIC_SELECTOR)
          if (pic instanceof HTMLElement) {
            event.preventDefault()
            // 本次刚上传的图优先于库里那张（否则用户点缩略图看到的还是旧图）；
            // 没换图时回落到当前编辑的那一行（`editing` 是弹层的唯一事实来源）。
            if (pendingImageUrl) openImageUrl(pendingImageUrl, '回复图片')
            else openImage(editing)
            return
          }
          if (target?.closest?.(`.${SAVE_BUTTON_CLASS}`)) {
            event.preventDefault()
            submitForm()
          }
        }
        panelOf()?.addEventListener('click', onPanelClick)

        /**
         * 「查看图片」：有图 ⇒ `layer.photos`（既有媒体预览方式）；没图 ⇒ 一句提示。
         * ⚠️ `alt` 过 `escapeHtml`（只拼进 HTML）；`src` 走 `photoSrc`——**不能**过
         * `escapeHtml`，理由见 `photoSrc` 的注释（`&` → `&amp;` 会把预签名串弄坏）。
         */
        function openImageUrl(url: string, alt: string): void {
          ;(layer.photos as (options: unknown) => void)({
            photos: {
              title: '回复图片',
              data: [{ alt: escapeHtml(alt), src: photoSrc(url) }],
            },
          })
        }

        function openImage(row: PlatformAutoReplyRuleRow | null): void {
          if (!row?.has_image || !row.image_url) {
            msg('未上传图片')
            return
          }
          openImageUrl(row.image_url, `回复图片 ${row.id}`)
        }

        // 两路取数互不拖累：列表挂了也要能开弹层（反之亦然），各自把错误说出来。
        void loadGames().catch((err: unknown) => {
          // 游戏字典取不到 ⇒ 弹层只剩「全部游戏」；说出来，⛔ 不假装有选项。
          msg(`游戏列表加载失败：${errText(err)}`)
        })
        void reloadList().catch((err: unknown) => msg(errText(err)))

        return () => {
          pageRoot.removeEventListener('click', onPageClick)
          fileInput.removeEventListener('change', onFileChange)
          fileInput.remove()
          panelOf()?.removeEventListener('click', onPanelClick)
          if (editLayerIndex !== null) {
            ;(layer.close as (index: number) => void)(editLayerIndex)
            editLayerIndex = null
          }
          if (batchLayerIndex !== null) {
            ;(layer.close as (index: number) => void)(batchLayerIndex)
            batchLayerIndex = null
          }
        }
      },
    },
    ctx,
  )
}

export default {
  mount: mountPage,
} satisfies IslandPage
