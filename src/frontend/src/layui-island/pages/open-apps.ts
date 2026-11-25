/**
 * 【开放平台】→「应用管理」页（island key = `open-apps`，路由 `/open-platform/apps`）。
 *
 * spec：`docs/specs/2026-10-08-open-platform-v2-parity-design.md` §7.1（回调配置与开关）/
 * §7.4（推送与重试）/ §8（前端一节）/ §13.18-3（台账读 / 重投端点的**冻结契约**）/
 * §4-V2 与 §14-11（「应用 = 全租户」的代价必须在页面上说破）。
 * 契约的另一半是**实现即契约**（`src/apps/openapi/api/app_views.py`、`serializers.py`、`urls.py`）。
 *
 * ## 这一页是**自有功能**，不是复刻
 *
 * 对标物（鸡蛋铺）里没有【开放平台】这一组 ⇒ 本页**没有证据件**、也不走 `mountConfigLike`
 * （那是「取 `public/replica/<key>.html` → 注入 → 灌数据」的复刻专用链路）。标记由本文件自己写，
 * 风格照同类自有功能页的 layui 惯例。
 * island key 登记在 `config/__tests__/replicaRoutes.spec.ts` 的 `OWN_FEATURE_PAGE_KEYS`。
 *
 * ## ⛔ 权限设置 / 游戏范围两块**已整块删除**
 *
 * 删掉的是：权限树弹层那一整块（含 `bindTree`·`selectedCodes`·`treeHtml` 那批 import）、
 * 游戏范围那一整块（含四种模式函数 / 两个保存回报 / 「全部游戏」勾选框与其两条通道）、
 * 列表里的「权限」与「游戏范围」两列、行操作里的 `lay-event="perm"` 与保存游戏范围按钮。
 *
 * **为什么删**（不是"清理旧代码"，是后端语义真的没了）：设计文档 §4-V2 的裁定是
 * **一个应用凭据 = 本租户全部业务数据**（§13.10），v1 那套「勾哪些权限点 / 看哪几款游戏」
 * 连同 `nodes` / `games` / `all_games` 三列与两个 `PUT` 端点一起被废除（§13.1）。
 * ⇒ 页面上留着它们就是留一个**点了会 404/405 的入口**（`AGENTS.md` §5-D 的反面）。
 * ⛔ 别把它们"顺手加回来"。⚠️ 代价（如实登记，也是页面上必须说破的那句）：
 * 密钥的知情面 = 该页可见面，而凭据的能力面 = 整个租户（§14-11）。
 *
 * ## 六块（数据来自 §7.1 / §7.3 / §13.18-3 那几条端点）
 *
 * 1. **列表**（`GET /api/open/apps/`）：⭐ **直接显示该应用的密钥明文**（业务方口径：
 *    可随时获取，没有「只看一次」限制），
 *    并在「操作」那一格给一个**复制 AppKey + AppSecret** 的动作；
 *    ⭐ 列表**上方**那一段 quote 里显式告知「**本应用可访问本租户全部业务数据**」（§4-V2 的代价）。
 * 2. **新建弹层**（`POST /api/open/apps/`）：响应里带明文密钥 ⇒ 建完**弹一层**把
 *    AppKey / AppSecret 一起摆出来（方便当场复制）。⚠️ 这一层**不是**"唯一能看到的地方"：
 *    列表里随时看得到、随时能复制（⛔ 所以这里**没有**「只显示这一次」那类措辞——
 *    它会是**与实现相反的一句话**）。
 * 3. **回调与推送弹层**（`PATCH /api/open/apps/<pk>/` + `GET .../deliveries/`）：
 *    ① 「回调地址」输入（**仅 https**：前端给**即时提示**，**真判据以后端为准**
 *    ——后端回的业务码 `1000` 与它那句中文必须**如实显示**）；
 *    ② 「回调开关」（`lay-skin="switch"`，发的是 `callback_state` 的 `enabled` / `disabled`）；
 *    ③ 一句人话说明（https only / 地址空或开关停用**不推送** / 对方要按我们的签名验真）；
 *    ④ ⭐ 「最近推送记录」**可折叠面板**（时间 / 事件 / 订单号 / 状态 / 重试次数 / 失败原因），
 *    `failed` 的行给「重投」按钮（二次确认 → `POST .../deliveries/<id>/retry/` → 刷新）。
 * 4. **停用 / 启用**（`PATCH`，`{state}`）：⛔ **没有删除按钮**——「只能停用不能删」，
 *    后端连 `DELETE` 路由都没有（405）。停用前必须说清「第三方**当场**调不通」。
 * 5. **轮换密钥**（`POST .../rotate-secret/`）：**单密钥、立即失效**（§5.3，没有双密钥宽限期）
 *    ⇒ 二次确认里必须写出「旧密钥立刻作废、第三方当场调不通」。
 *
 * ## ⚠️ 台账那几个契约字段里，本页**有意不用** `processed_at`
 *
 * 「时间」那一列取 `created_at`（**事件什么时候发生的**）；`processed_at` 是内部排障用的
 * 投递时刻，塞进同一格会让"事件时间"与"投递时间"长得一样（`AGENTS.md` §5-C 的 E-0068 同族：
 * 判一个字段的语义看它挂在哪儿）。⛔ 不是漏了它：契约（§13.18-3）要求它**在载荷里**，本页照收。
 *
 * ## ⚠️ 一条**部署面**的降级分支（不是死代码，有用例）
 *
 * 万一行载荷里没有 `callback_url` / `callback_state` 两个键（回滚 / 半新半旧），
 * ⛔ **不许**当成"没配回调"显示成空——那会让用户一保存就把**真实配置覆盖掉**
 * （`permission_codes` 上踩过同形的坑）⇒ 这时**明写一句并中止这一次打开**
 * （`MESSAGES.callbackFieldsMissing`）。
 *
 * ## 四条 island 陷阱（`AGENTS.md` §5-G，改这一页前先读）
 *
 * ① 类名与选择器分成两个常量（`class="${PANEL_CLASS}"` 拼标记、`PANEL_SELECTOR` 查节点）；
 * ② 以 `(` 开头的语句一律前置分号（`;(table.render as …)(…)`——ASI 不会在 `)` 换行 `(` 之间插分号）；
 * ③ 顺序：`await bootstrapIsland(...)`（挂样式）→ `root.innerHTML = …`（塞标记）→
 *    `initIslandElements(layui)`（按标记里的 `lay-*` 初始化）；
 * ④ 页里有定时器时：假定时器要**先** `vi.useFakeTimers()` **再**包装 `window.setInterval`，
 *    且每个用例自己收口。⚠️ 本页**没有**任何定时器（推送记录是点开时拉一次、重投后刷新一次），
 *    所以这一条落在测试的通用纪律上（`__tests__/open-apps.spec.ts`）。
 * ⚠️ 四条都有**运行期**判据（`__tests__/open-apps.spec.ts`），不是靠这段注释。
 *
 * ## ⭐ 「API 文档」入口 —— 只放在这一页，⛔ 不进侧栏菜单
 *
 * 工具栏第三颗按钮「API 文档」⇒ `layer.open` 一个全屏弹层：左边是从 `##` / `###` 生成的目录，
 * 右边是 `marked` 渲染后的正文（有界高度 + `overflow:auto`），顶部两颗动作按钮
 * （复制 Markdown 原文 / 下载 .md）。
 *
 * ### 内容出处：`docs/OPEN-API.md`，但**从 `src/docs/open-api.md` 这份副本 import**
 *
 * ⛔ **首选写法（直接 `?raw` import 仓库根那份）实测不成立**：
 * `?raw` 指向 Vite root（`src/frontend`）之外的文件时，**`vite build` 能解析、`vitest run` 分环境**——
 * node 环境的用例能过，而**本文件这条 import 链只要被一个 `// @vitest-environment jsdom` 的用例
 * 加载就报 `Error: Denied ID …/docs/OPEN-API.md?raw`**（`vite/dist/node/chunks/node.js` 的
 * `isServerAccessDeniedForTransform`：`rawRE` 命中时要求 `server.fs` 的 allow 列表放行；
 * 默认 allow = 工作区根 = `src/frontend`）。页测试必然是 jsdom ⇒ 首选写法对本页**不可用**。
 * ⚠️ 想让首选成立得改 `vite.config.ts` 的 `server.fs.allow`（那会把仓库路径加进 dev server 的
 * 可服务面）——**那是另一件事，本页没有做**。
 *
 * ⇒ 用**副本**：`src/docs/open-api.md`（在 Vite root 之内 ⇒ 两边都解析得到），
 * 由 `import … from '../../docs/open-api.md?raw'` 读进来。
 * ⭐ **副本与出处必须逐字节相同**，这条**有网**：
 * `__tests__/open-apps.spec.ts` 用 `fs` 同时读两份、断言 `toEqual`（弄红证明见该文件的注释）。
 * ⛔ 改了 `docs/OPEN-API.md` 之后**必须**重跑一次 `cp ../../docs/OPEN-API.md src/docs/open-api.md`
 * （在 `src/frontend` 下执行）——忘了的话门禁会红并把这行命令打出来。
 * ⛔ 不许把文档内容改成硬编码的字符串常量（那会与源漂移且没人守）。
 *
 * ### ⛔ 这一页里没有「净化」这一步，取而代之的是一条**能红的网**
 *
 * `marked` 默认**原样放行裸 HTML**，而这份文档是给租户看的页面。本页**不做**运行期净化
 * （不引第二个依赖、也不自己写白名单——那会把"文档里有什么"变成运行期才知道的事），
 * 改成把判据放在测试里：渲染结果里出现的**标签名必须 ⊆ 白名单**，且**不许出现事件处理属性 /
 * `javascript:` 协议**。文档里一旦有人写裸 `<script>` / `<img onerror>`，那条网**当场红**，
 * 逼作者去改文档（`AGENTS.md` §5-G「结构性检查网自己的断言也必须能失败」）。
 * ⚠️ 插入 DOM **只用 `innerHTML`**，⛔ 不走 `eval` / `new Function`。
 *
 * ### 锚点 id
 *
 * 自己生成：`openApiDocSection-<序号>-<标题 slug>`。**唯一性由序号保证**（⛔ 不靠 slug 去重），
 * slug 只为可读。⚠️ 序号是「本次渲染里的第几个标题」⇒ 只在本页内部用，对外没有长期链接。
 * ⚠️ 目录与锚点**都从同一次 `marked` 渲染的产物**里派生（`renderApiDoc`）⇒ 不会出现
 * 「目录里有一项、点过去没有对应标题」。
 */
import { Marked } from 'marked'

import { fetchMe } from '@/api/auth'
import {
  APP_MANAGE_NODE,
  createApiApp,
  listApiApps,
  listDeliveries,
  retryDelivery,
  rotateApiAppSecret,
  updateApiApp,
  type ApiAppCallbackState,
  type ApiAppRow,
  type ApiDeliveryRow,
} from '@/api/openApps'
import { escapeHtml } from '@/layui-island/escape'
import { canNode } from '@/layui-island/perms'
import { bootstrapIsland, initIslandElements, islandMsg, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

/**
 * ⭐ **对外 API 文档的原文** = `docs/OPEN-API.md` 的**逐字节副本** `src/docs/open-api.md`。
 *
 * ⛔ **为什么不直接 import 仓库根那份**：Vite 对 root（`src/frontend`）之外的文件有 fs 限制，
 * `?raw` 在 **jsdom 用例**里会报 `Error: Denied ID …`（实测，见文件头那一节）⇒ 副本放在
 * Vite root 之内，`vite build` 与 `vitest run` 两边都解析得到。
 * ⚠️ **两份必须逐字节相同**，这条有网（`__tests__/open-apps.spec.ts` 用 `fs` 同时读两份对拍）；
 * 改了 `docs/OPEN-API.md` 就重跑一次 `cp ../../docs/OPEN-API.md src/docs/open-api.md`。
 */
import openApiDocSource from '../../docs/open-api.md?raw'

/** island key（= 文件名 = 路由 `meta.islandPage`，三处必须逐字一致）。 */
export const PAGE_KEY = 'open-apps'
export const TITLE = '应用管理'

/** 三个弹层的**容器 class 名**（拼进标记用）与**选择器**（查节点用）——两者不能混（陷阱 ①）。 */
export const FORM_CLASS = 'openAppsForm'
export const FORM_SELECTOR = `.${FORM_CLASS}`
export const SECRET_CLASS = 'openAppsSecret'
export const SECRET_SELECTOR = `.${SECRET_CLASS}`
export const CALLBACK_CLASS = 'openAppsCallback'
export const CALLBACK_SELECTOR = `.${CALLBACK_CLASS}`

/** 列表表的 id / elem（layui 的 `reloadData` / `on` 要的是 **id**，带 `#` 是静默 no-op）。 */
export const TABLE_ID = 'openAppsTable'
export const TABLE_ELEM = '#openAppsTable'
/** 新建弹层与密钥弹层用到的那些 `id`。 */
export const CREATE_NAME_INPUT_ID = 'openAppsName'
export const CREATE_SAVE_BUTTON_ID = 'openAppsCreateSaveBtn'
export const SECRET_TEXT_ID = 'openAppsSecretText'
export const SECRET_KEY_TEXT_ID = 'openAppsKeyText'
export const SECRET_APP_NAME_ID = 'openAppsSecretName'
export const SECRET_CLOSE_BUTTON_ID = 'openAppsSecretCloseBtn'

/** 「回调与推送」面板里的 `id`（地址 / 开关 / 即时提示 / 保存 / 应用名）。 */
export const CALLBACK_APP_NAME_ID = 'openAppsCallbackAppName'
export const CALLBACK_URL_INPUT_ID = 'openAppsCallbackUrl'
export const CALLBACK_HINT_ID = 'openAppsCallbackHint'
export const CALLBACK_STATE_SWITCH_ID = 'openAppsCallbackState'
export const CALLBACK_SAVE_BUTTON_ID = 'openAppsCallbackSaveBtn'
/**
 * 回调那一块的 `layui-form` 容器（`id` 与 `lay-filter` **同值**，这是 layui 的用法）。
 *
 * ⚠️ **`form.render` 的根选择器就是 `.layui-form`**（vendored `public/layui/layui.js` 里
 * `h=".layui-form"`，`render(type, filter)` 取 `.layui-form[lay-filter="…"]`）⇒
 * 没有这一个容器，`lay-skin="switch"` 的那个裸 input **不会被渲染成开关**，
 * 页面上就是一个原生复选框。⇒ 本页的 `form.render` 调用都带上 `CALLBACK_FORM_FILTER`
 * （⛔ 不用无参的全局渲染：那会把页面上别的 `.layui-form` 一起重渲）。
 */
export const CALLBACK_FORM_ID = 'openAppsCallbackForm'
export const CALLBACK_FORM_FILTER = 'openAppsCallbackForm'
/** 开关自己的 `lay-filter`（真浏览器里 `form.on('switch(…)')` 按它认领；本页只用 DOM 勾选态）。 */
export const CALLBACK_STATE_FILTER = 'openAppsCallbackStateToggle'
/** 开关的 `name`（与 `id` **同值**：layui 的开关按 `name` 归属，缺它渲染不出开关）。 */
export const CALLBACK_STATE_NAME = 'openAppsCallbackStateToggle'

/** 「最近推送记录」那块：折叠开关 + 列表容器 + 失败提示。 */
export const DELIVERIES_TOGGLE_ID = 'openAppsDeliveriesToggle'
export const DELIVERIES_BODY_ID = 'openAppsDeliveriesBody'
export const DELIVERIES_WARN_ID = 'openAppsDeliveriesWarn'
/**
 * 「重投」按钮上那个**属性名**（与选择器分成两个常量——陷阱 ①：属性名进标记、
 * 选择器 `[${DELIVERY_RETRY_ATTR}]` 用来找节点）。⚠️ 值放**台账行自己的 id**。
 */
export const DELIVERY_RETRY_ATTR = 'data-retry-delivery'
/** 台账第一页取多少条（`EnvelopePagination` 上限 200；页面只展示最近这些）。 */
export const DELIVERIES_PAGE_SIZE = 20

// ── 「API 文档」弹层 ────────────────────────────────────────────────────────

/** 文档弹层的**容器 class 名**（拼标记）与**选择器**（查节点）——两者不能混（陷阱 ①）。 */
export const DOC_CLASS = 'openAppsApiDoc'
export const DOC_SELECTOR = `.${DOC_CLASS}`
/** 目录 / 正文两个滚动容器，加两颗动作按钮的 `id`。 */
export const DOC_TOC_ID = 'openAppsDocToc'
export const DOC_BODY_ID = 'openAppsDocBody'
export const DOC_COPY_BUTTON_ID = 'openAppsDocCopyBtn'
export const DOC_DOWNLOAD_BUTTON_ID = 'openAppsDocDownloadBtn'
/**
 * 目录项上的锚点属性名（陷阱 ① 的同族：**属性名进标记**、选择器 `[${DOC_ANCHOR_ATTR}]` 找节点）。
 * ⚠️ 值放**标题那个锚点 id**（与标题上的 `id=` 逐字同值 ⇒ 点过去必然有对应小节）。
 */
export const DOC_ANCHOR_ATTR = 'data-doc-anchor'
/** 目录项的 class 名 / 选择器（`data-doc-depth` 区分 2 / 3 两级）。 */
export const DOC_TOC_ITEM_CLASS = 'openAppsApiDocTocItem'
export const DOC_TOC_ITEM_SELECTOR = `.${DOC_TOC_ITEM_CLASS}`
/** 目录那个 `<ul>` 的 class 名 / 选择器。 */
export const DOC_TOC_LIST_CLASS = 'openAppsApiDocTocList'
export const DOC_TOC_LIST_SELECTOR = `.${DOC_TOC_LIST_CLASS}`
/** 「当前所在小节」的高亮 class 名 / 选择器（点目录时打在目录项与那个标题上）。 */
export const DOC_ACTIVE_CLASS = 'openAppsApiDocActive'
export const DOC_ACTIVE_SELECTOR = `.${DOC_ACTIVE_CLASS}`
/** 标题锚点 id 的前缀（唯一性由紧随其后的序号保证，见 `renderApiDoc`）。 */
export const DOC_ANCHOR_PREFIX = 'openApiDocSection'
/** 工具栏那颗按钮的 `lay-event`。 */
export const DOC_TOOLBAR_EVENT = 'apidoc'
/** 「下载 .md」写出来的文件名（就是出处那份文件的名字）。 */
export const DOC_DOWNLOAD_FILENAME = 'OPEN-API.md'
/** 弹层标题（把出处写进标题里，省得有人以为这是另一份文档）。 */
export const DOC_PANEL_TITLE = '对外 API 文档（docs/OPEN-API.md）'
/** 两颗动作按钮的文案（抽成常量只为让 spec 钉住字面值）。 */
export const DOC_COPY_BUTTON_TEXT = '复制 Markdown 原文'
export const DOC_DOWNLOAD_BUTTON_TEXT = '下载 .md'
/** 工具栏那颗按钮的文案。 */
export const DOC_TOOLBAR_BUTTON_TEXT = 'API 文档'
/**
 * 弹层顶部那句说明。
 *
 * ⚠️ **必须说清「内容不是前端写的」**：文档改了要重新构建才带上（否则用户会以为页面坏了）。
 * ⛔ 这里不许出现 markdown 的 `**`（走 `innerHTML`，浏览器不认 markdown）——强调用 `<b>`。
 */
export const DOC_PANEL_NOTICE =
  '内容就是仓库里的 <b>docs/OPEN-API.md</b>（渲染成网页版看更方便）：' +
  '左边点目录跳小节，右边是正文。要原文就点上面两颗按钮（复制 / 下载）。' +
  '⚠️ 文档改动要重新构建前端才会带上。'

/** 「最近推送记录」折叠链接的两句文案（`textContent` 写进去 ⇒ ⛔ 只能是纯文本）。 */
export const DELIVERIES_TOGGLE_CLOSED = '最近推送记录（点这里展开）'
export const DELIVERIES_TOGGLE_OPEN = '最近推送记录（点这里收起）'

/**
 * ⭐ **§4-V2 的代价必须在这页上说破**（§8 的「改」那一行 + §14-11）：
 * 一个应用凭据 = 本租户**全部**业务数据。⛔ 不许让它成为一个隐性默认
 * （页面上没有「权限设置 / 游戏范围」两块之后，这里是唯一能说出这句话的地方）。
 */
export const FULL_TENANT_NOTICE =
  '本应用可访问本租户全部业务数据（一个应用 = 整个租户，没有「只给几张单」这种收窄）：' +
  '拿到密钥的人能查单、改单、改状态、改发单价，也能读到成本与提成。'

/**
 * 密钥弹层顶部那句提示（创建成功 / 轮换成功两处共用）。
 *
 * ⛔ 这里**不许**出现「只显示这一次 / 关了就没」这类措辞——列表里就显示着密钥、随时能复制，
 * 那会是**与实现相反的一句话**（`AGENTS.md` §5-C：对外文案与实现冲突时，不能只改文档）。
 * ⭐ 这句要说的新事实：密钥**随时可查**（就在列表那一列）、**不必**抢着复制；
 * ⚠️ 但**轮换**那一侧仍有不可逆的后果要提（旧密钥立刻作废），那句在二次确认里（见
 * `confirmRotate`），⛔ 不靠这条提示兜。
 */
export const SECRET_PANEL_NOTICE =
  '密钥随时可以在列表里查看与复制（关闭本窗口不会丢失）。⛔ 这是凭据：不要贴到聊天群 / 工单 / 截图里。'

/**
 * `costs` 的人话提示（§9-11）。
 *
 * ⚠️ 这是**商业风险**提示（拿到数据的人可能是打手本人或竞品），不是免责话术。
 * ⚠️ 主语是「第三方查单时」——⛔ 别改回「勾选后」（权限勾选已废除，那指向不存在的动作）。
 */
export const COST_NOTICE =
  '第三方查单时能看到这张单的成本与提成（打手 / 客服的预计、生效、发单价与提成率）。'

/** PII 提示（§9-17：与对外文档**都**要写明）。 */
export const PII_NOTICE =
  '同时，第三方也会看到客户联系方式（电话 / QQ微信 / 旺旺）、玩家账号 / 子账号 / 角色名与订单原文' +
  '（客户填写的内容不由我方清洗）。'

/**
 * 回调地址的**即时提示**（前端这一侧的唯一一句）。
 *
 * ⚠️ **它不是判据**（设计文档 §4-V6 的判据在后端：保存时 + 每次投递前各一次，
 * 且包含 SSRF 私网段检查——前端**看不到** DNS 解析结果，做不到那一条）。
 * ⇒ 这里只在格式明显不对时**当场说一句**；真判据以后端回的 `1000` 为准，
 * 页面把那句中文**如实显示**（见 `MESSAGES` 与 `saveCallbackSettings`）。
 */
export const CALLBACK_URL_HINT =
  '回调地址只接受 https 开头的地址（http 与其它协议一律不收；内网地址后端也会拒）。'

/**
 * 回调那一块的**人话说明**（这三件事必须写清：https only / 空或停用时**不推送** / 对方要验签名）。
 *
 * ⚠️ 三个头的名字与签名要素来自**实现**（`apps/openapi/outbound.py:103-105/211-224`，
 * 设计文档 §7.3）：`X-TT-Timestamp` / `X-TT-Delivery` / `X-TT-Signature`，
 * `sign = HMAC-SHA256(AppSecret, timestamp + "\n" + delivery_id + "\n" + sha256(不含 sign 的报文))`。
 * ⛔ 页面上不抄那段公式（抄了就会漂），只说"要用 AppSecret 按我们的算法验真"。
 * ⚠️ 强调一律用 HTML 的 `<b>`（这段是塞进 `innerHTML` 的）——⛔ 不要写 markdown 的 `**`：
 * 浏览器不认 markdown，用户会**连着星号一起看到**。
 */
export const CALLBACK_NOTICE =
  '推送怎么走：订单状态变化 / 订单删除 / 订单新增进度图这三类事件，我们会按产生顺序 POST 到上面的地址，' +
  '每条都带 <b>X-TT-Timestamp</b> / <b>X-TT-Delivery</b> / <b>X-TT-Signature</b> 三个头，' +
  '对方要用 AppSecret 按我们的算法<b>验真</b>再处理（⛔ 不要只看来源 IP 就算数）。' +
  '<br>地址为空、或者开关是「停用」⇒ <b>不会推送</b>（不排队、不重试、也不报错）；' +
  '推送失败会自动重试 6 次（10 秒 / 1 分 / 5 分 / 15 分 / 1 小时 / 6 小时），仍失败就落在这里，可以手工重投；' +
  '因为是「至少一次」，对方要按 <b>X-TT-Delivery</b> 去重。' +
  '<br>地址<b>只收 https</b>：保存时与每次投递前各校验一次（DNS 会变），内网 / 回环地址会被拒。'

/** 「没有权限管理开放平台应用」的自检提示（页面被深链打开时用）。 */
export const NO_ACCESS_MESSAGE = '无权限管理开放平台应用'

/** 页内其它用户可见文案（抽成常量只为让 spec 钉住字面值）。 */
export const MESSAGES = {
  /** 应用名空着就保存。 */
  noName: '请填写应用名',
  /** 保存 / 轮换 / 启停失败时的兜底（后端文案通常已经有了）。 */
  failed: '操作失败',
  /**
   * 行载荷里没有回调那两个键（**部署面**的降级分支，见文件头）。
   * ⚠️ 必须**说出来并中止**：当成"没配回调"显示成空，用户一保存就覆盖掉真实配置。
   */
  callbackFieldsMissing:
    '后端这一次没有回这个应用的回调配置（只回了别的字段），为避免把现有配置覆盖成空，' +
    '这一次不打开设置面板。请刷新页面重试；若一直如此，说明前端与后端版本不一致。',
  /**
   * 复制失败（缺 `navigator.clipboard` / 被拒）。
   * ⚠️ 必须**说出来**并告诉人怎么办：静默失败会让用户以为剪贴板里已经有了
   * （先例：`order-orderlist.ts` 的「复制订单号失败了，请手动复制」）。
   */
  copyFailed: '复制失败（浏览器不允许自动复制），请手动选中列表里的密钥复制',
  /** 复制成功（文档原文）。⚠️ 说清"复制的是什么"，别只回一句"复制成功"。 */
  copyDocDone: '已复制整份 API 文档的 Markdown 原文（可以直接粘进编辑器或喂给 AI）。',
  /**
   * 复制文档失败（缺 `navigator.clipboard` / 被拒）。
   * ⚠️ 与 `copyFailed` 一样**必须说出来**，而且要**给一条出路**（那颗「下载 .md」就是）。
   */
  copyDocFailed: '复制失败（浏览器不允许自动复制）——请改用上面的「下载 .md」把文件存下来。',
  /**
   * 下载失败（这个浏览器没有 `URL.createObjectURL`，例如 jsdom / 很旧的浏览器）。
   * ⚠️ **给一条出路**：告诉人去哪儿拿这份文件，⛔ 不是"下载失败"四个字了事。
   */
  docDownloadFailed: '这个浏览器不支持直接下载——原文在仓库的 docs/OPEN-API.md 里，直接打开那个文件即可。',
} as const

/**
 * 列表里的一行 = 服务端那一行 + **本页在组装时按该行 `state` 预算出来的按钮文案**。
 *
 * 为什么要多这一个字段（而不是让模板自己去分支）：那颗状态按钮是**一个** `lay-event="toggle"`，
 * 文案必须**逐行不同**（`停用` / `启用`）——⛔ 不许做成"两个按钮各挂一个事件"（同一行两个按钮更乱），
 * ⛔ 也不许上 laytpl（见 `buildHtml` 那段注释：本仓没有这个先例，而函数 `templet` 够用）。
 * ⇒ 文案在**组装行的这一刻**算好，模板只负责插值。
 */
export type OpenAppTableRow = ApiAppRow & { state_action_label: string }

/**
 * 那颗状态按钮的**文案**（纯函数，⛔ 只有这一处判据）。
 *
 * 「启用中」⇒ 这一下是**停用**；「已停用」⇒ 这一下是**启用**。
 * ⚠️ 判据用 `row.state`（枚举值，spec §5.1 的 `enabled` / `disabled`），
 * ⛔ **不用** `state_text`（那是给人看的中文名，服务端可以改文案，用它做判据会静默错）。
 */
export function stateActionLabel(row: ApiAppRow): string {
  return row.state === 'enabled' ? '停用' : '启用'
}

/**
 * 把服务端回的行**映射**成表格行（在 `table.render` / `reloadData` 之前统一走一遍）。
 *
 * ⚠️ 这是**唯一**的装配出口：⛔ 别在别处再手搓一个 `state_action_label`（那就成了第二处判据）。
 */
export function decorateRows(rows: readonly ApiAppRow[]): OpenAppTableRow[] {
  return rows.map((row) => ({ ...row, state_action_label: stateActionLabel(row) }))
}

/**
 * 「操作」那一格（函数 `templet`，**不经过 laytpl**）。
 *
 * 四个 `lay-event`（`callback` / `copysecret` / `toggle` / `rotate`）**一个不多一个不少**：
 * - ⛔ **没有 `del`** ——「只能停用不能删」，后端连 `DELETE` 路由都没有；
 * - ⛔ **没有 `perm`** —— 权限设置那一整块已删除（见文件头）；
 * - ⭐ `callback`：打开「回调与推送」面板（回调地址 / 开关 / 推送记录）；
 * - ⭐ `copysecret`：**复制 AppKey + AppSecret** 到剪贴板。
 *   ⚠️ **密钥不进标记**：这个格子的 HTML 里**一个字都不含**密钥
 *   ⇒ 复制时从 `row.app_secret`（内存里的行数据）取，⛔ 不写成 `data-clipboard-text="…"`：
 *   那样密钥会**固化进 DOM 属性**（截图 / 调试面板 / 页面快照都拿得到），而页面本来只在
 *   一个文本节点里显示它。判据见 `__tests__/open-apps.spec.ts` 里那条「标记里没有密钥」。
 */
export function actionCell(row: OpenAppTableRow): string {
  return (
    `<a class="layui-btn layui-btn-xs" lay-event="callback">回调与推送</a>` +
    `<a class="layui-btn layui-btn-xs layui-btn-normal" lay-event="copysecret">复制密钥</a>` +
    `<a class="layui-btn layui-btn-xs layui-btn-primary" lay-event="toggle">` +
    `${escapeHtml(row.state_action_label)}</a>` +
    `<a class="layui-btn layui-btn-xs layui-btn-danger" lay-event="rotate">轮换密钥</a>`
  )
}

/**
 * 「AppSecret」那一格的**标记**（纯函数）。
 *
 * ⭐ 明文显示（列表里就该看得到它），⚠️ **但要走 `escapeHtml`**：
 * 密钥是服务端生成的 URL-safe 串（理论上不含 `<`），而"密钥来自库、库里的值可能被人改"
 * ⇒ 这里**不假设**它的字符集（本页其它来自库的值一律转义，别为这一格破例）。
 * ⚠️ 单元格宽度有限 ⇒ 外面包一个 `word-break:break-all` 的容器，长串会折行而不是被裁掉
 * （被裁掉的话"看得见"就成了半句——`layui-table-cell` 默认是 `overflow:hidden`）。
 */
export function secretCell(row: ApiAppRow): string {
  return (
    `<span style="word-break:break-all;font-family:monospace">` +
    `${escapeHtml(row.app_secret)}</span>`
  )
}

/**
 * 「复制密钥」的剪贴板出口。
 *
 * ⚠️ **缺 `navigator.clipboard` 不是静默失败**（`order-orderlist.ts` 有同款提示的先例）：
 * jsdom 与非安全上下文（`http://` 的普通域名）里它整个不存在；`writeText` 也可能被判
 * `NotAllowedError`（用户没授权 / 不是用户手势）⇒ 失败必须**说出来**（⛔ 不能让用户以为复制好了）。
 * ⚠️ 复制的内容是 `app_key` + `app_secret` **两行**（第三方对接要的就是这一对，
 * 只给一半等于让人回去再抄一次）。
 */
export function copyCredentialText(row: ApiAppRow): string {
  return `AppKey: ${row.app_key}\nAppSecret: ${row.app_secret}`
}

/**
 * 往剪贴板写一段文本（**本页唯一的剪贴板出口**）。
 *
 * ⚠️ **缺 `navigator.clipboard` 不是静默失败**：
 * jsdom 与非安全上下文（`http://` 的普通域名）里它整个不存在；`writeText` 也可能被判
 * `NotAllowedError`（用户没授权 / 不是用户手势）⇒ 失败必须**说出来**（⛔ 不能让用户以为复制好了）。
 * ⚠️ 抽成公共出口：密钥与「API 文档原文」两处都用它，⛔ 别各写一份
 * （两份的失败分支只要有一处漏掉 `onDone(false)`，那处就变成静默失败）。
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

export function copyCredentialToClipboard(
  row: ApiAppRow,
  onDone: (ok: boolean) => void,
): void {
  copyTextToClipboard(copyCredentialText(row), onDone)
}

/**
 * 把一段文本存成文件（`Blob` + `URL.createObjectURL` + `a[download]`）。
 *
 * ⚠️ **返回是不是真的发出去了**（`false` = 这个环境没有 `URL.createObjectURL`）：
 * 调用方必须把 `false` 变成一句**人话提示**（见 `MESSAGES.docDownloadFailed`），
 * ⛔ 不许点了没反应（那是本仓反复堵的"点了没反应"）。
 * ⚠️ jsdom / 很旧的浏览器没有 `createObjectURL` ⇒ 这一条**有真实的降级分支**，不是防御性写法。
 * ⚠️ 临时链接用完就 `revoke`（不 revoke 的话这一份 90 KB 的文本会一直挂在内存里）。
 */
export function downloadTextFile(filename: string, text: string): boolean {
  const create = URL.createObjectURL
  if (typeof create !== 'function') return false
  const url = create.call(URL, new Blob([text], { type: 'text/markdown;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  // ⚠️ 必须**挂进文档**再点：不在文档里的 `<a>` 在部分浏览器上点不动（Firefox 的已知差异）。
  document.body.appendChild(link)
  link.click()
  link.remove()
  if (typeof URL.revokeObjectURL === 'function') URL.revokeObjectURL(url)
  return true
}

/**
 * HTML 里那几个实体的解码表（只列 `marked` 会产出的那些）。
 *
 * ⚠️ 名单**故意很短**：它不是"通用 HTML 解码器"，只是给 `htmlToText` 与判据用；
 * 遇到表里没有的实体**原样保留**（⛔ 不抛、也不猜）。
 */
const NAMED_ENTITIES: Record<string, string> = {
  amp: '&',
  lt: '<',
  gt: '>',
  quot: '"',
  apos: "'",
  nbsp: ' ',
}

/**
 * 把一小段 HTML 变成纯文本（**先去标签、再解实体**，一趟正则、⛔ 不做二次解码）。
 *
 * 用途：目录条目的文字（标题里可能有 `<strong>` / `<code>`）。
 * ⚠️ **一趟扫描**很关键：先解 `&amp;lt;` 再解 `&lt;` 那种两趟写法会把 `&amp;lt;` 解成 `<`
 * （错的）；一趟扫描按最长匹配逐个还原才是对的。
 */
export function htmlToText(html: string): string {
  return html
    .replace(/<[^>]*>/g, '')
    .replace(/&(#[xX][0-9a-fA-F]+|#[0-9]+|[a-zA-Z][a-zA-Z0-9]*);/g, (whole, body: string) => {
      if (body.startsWith('#')) {
        const hex = body[1] === 'x' || body[1] === 'X'
        const code = Number.parseInt(hex ? body.slice(2) : body.slice(1), hex ? 16 : 10)
        return Number.isFinite(code) ? String.fromCodePoint(code) : whole
      }
      const named = NAMED_ENTITIES[body]
      return named === undefined ? whole : named
    })
}

/**
 * 标题文字 → 锚点里那截**可读**的 slug。
 *
 * ⚠️ 它**不负责唯一性**（唯一性由 `renderApiDoc` 里的序号保证）⇒ 两个标题 slug 撞了也没关系。
 * ⚠️ 只留「字母 / 数字」（含中日韩字符，它们是 `\p{L}`）⇒ **标点、引号、反斜杠全被换成 `-`**，
 * 于是锚点里不可能出现引号或反斜杠 ⇒ 后面的 `[${DOC_ANCHOR_ATTR}="…"]` 属性选择器是安全的。
 */
export function slugifyHeading(text: string): string {
  const slug = text
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60)
    .replace(/-+$/g, '')
  return slug === '' ? 'section' : slug
}

/** 一个标题在这一次渲染里的落点：层级 / 纯文本 / 锚点 id。 */
export type DocHeading = { depth: number; text: string; id: string }
/** 一次渲染的产物：正文 HTML + 全部标题（按出现顺序）。 */
export type RenderedDoc = { html: string; headings: DocHeading[] }

/** 匹配 `<h1>`–`<h6>`（⚠️ 标题不会嵌套，而代码块里的 `</h2>` 已被转义 ⇒ 惰性匹配是安全的）。 */
const DOC_HEADING_RE = /<h([1-6])>([\s\S]*?)<\/h\1>/g

/**
 * 渲染整份 Markdown（**全份只跑一次 `marked`**），并给每个标题补上唯一锚点 id。
 *
 * ⚠️ **目录与锚点都从这一次渲染的产物里派生**（⛔ 不另跑一次 `lexer` 去数标题）：
 * 两边同源才不会出现「目录里有一项、页面上没有对应小节」。
 * ⚠️ `{ async: false }` 是**必须**的：不传时 `marked.parse` 的返回类型是 `string | Promise<string>`
 * （它同时支持异步钩子），传了才是纯 `string`。
 * ⚠️ id = 前缀 + **序号** + slug：序号从 1 起、按出现顺序排 ⇒ **唯一性由构造保证**。
 */
export function renderApiDoc(source: string): RenderedDoc {
  const marked = new Marked({ gfm: true })
  const headings: DocHeading[] = []
  const html = marked
    .parse(source, { async: false })
    .replace(DOC_HEADING_RE, (_whole, depthText: string, inner: string) => {
      const depth = Number(depthText)
      const text = htmlToText(inner).trim()
      const id = `${DOC_ANCHOR_PREFIX}-${headings.length + 1}-${slugifyHeading(text)}`
      headings.push({ depth, text, id })
      return `<h${depth} id="${escapeHtml(id)}">${inner}</h${depth}>`
    })
  return { html, headings }
}

/** 目录里的一个条目（锚点 + 文字；两者都转义——id 由我们生成、文字来自文档）。 */
function docTocLink(h: DocHeading): string {
  return `<a href="javascript:;" ${DOC_ANCHOR_ATTR}="${escapeHtml(h.id)}">${escapeHtml(h.text)}</a>`
}

/**
 * 目录的标记（纯函数）。
 *
 * ⚠️ **只用 `##` 与 `###` 两级**（`#` 是文档大标题、`####` 是正文里的小标）；
 * `###` 归到它前面那个 `##` 下面（本次这份文档里没有"开头就是 `###`"的情形，
 * 真出现也不静默丢：那种条目直接放在顶层）。
 * ⚠️ 条数不在这里下判断：spec 用**文档原文里 `##` 的行数**去对它（见 `__tests__/open-apps.spec.ts`）。
 * ⭐ `prefix` 是**给另一页复用**留的入参（`taker-apps.ts` 的「接入文档」弹层要自己那一套
 * class 名，否则两页的样式会互相命中）——默认值就是本页自己那一对，既有调用点行为不变。
 */
export function docTocHtml(
  headings: readonly DocHeading[],
  prefix: { item: string; list: string } = { item: DOC_TOC_ITEM_CLASS, list: DOC_TOC_LIST_CLASS },
): string {
  const parts: string[] = []
  /** 当前那个 `##` 条目下面攒着的 `###` 条目。 */
  let sub: string[] = []
  /** 是否有一个 `##` 条目开着还没收尾。 */
  let openH2 = false
  const closeH2 = (): void => {
    if (!openH2) return
    if (sub.length > 0) parts.push(`<ul>${sub.join('')}</ul>`)
    parts.push('</li>')
    openH2 = false
    sub = []
  }
  for (const h of headings) {
    if (h.depth === 2) {
      closeH2()
      parts.push(`<li class="${prefix.item}" data-doc-depth="2">${docTocLink(h)}`)
      openH2 = true
    } else if (h.depth === 3) {
      const item = `<li class="${prefix.item}" data-doc-depth="3">${docTocLink(h)}</li>`
      if (openH2) sub.push(item)
      else parts.push(item)
    }
  }
  closeH2()
  return `<ul class="${prefix.list}">${parts.join('')}</ul>`
}

/**
 * 文档原文（`?raw` 进来的是**构建期常量**）。
 *
 * ⚠️ **必须走这个函数拿原文**，⛔ 不要把那个 import 绑定直接当实参传出去：
 * 压缩器会把**整份 92 KB 的字符串字面量内联到调用点**，而模块级绑定仍然留着 ⇒ 产物里**存两份**
 * （实测两版 `vite build` 对比：走函数后同一处搜索只出现一次，正好省出一份文档的体积）。
 */
export function openApiDocText(): string {
  return openApiDocSource
}

/**
 * 页面用的那一份渲染结果（**惰性算一次就缓存**）。
 *
 * ⚠️ 缓存的意义是「渲染只走 `marked` 一次」：弹层反复开关**不会**重跑渲染。
 * ⚠️ 文档内容在构建期就固化进产物了 ⇒ 缓存不会读到旧内容（改了文档要重新构建，见 `DOC_PANEL_NOTICE`）。
 */
let apiDocCache: RenderedDoc | null = null
export function apiDoc(): RenderedDoc {
  if (apiDocCache === null) apiDocCache = renderApiDoc(openApiDocText())
  return apiDocCache
}

/**
 * 一个 ISO 8601 时刻的**显示形态**（纯函数）：`2026-10-08 17:20:00`。
 *
 * ⚠️ 只截到秒、**不做时区换算**（后端给的就是带偏移的本地时刻）；
 * `null` / 空串 ⇒ `—`（⛔ 不是空白格：空白会被读成"这一列没数据"，
 * `AGENTS.md` §5-E 的"空结果不等于没有数据"同族）。
 * ⚠️ 走 `escapeHtml`：它可能来自库（`AGENTS.md` 的红线：来自库的值一律转义）。
 */
export function timeText(value: string | null): string {
  if (value === null || value === '') return '—'
  return escapeHtml(value.replace('T', ' ').slice(0, 19))
}

/**
 * 「最近使用」那一格（**纯函数**）。
 *
 * ⚠️ `null` 的含义是「**一次都没验签成功过**」，⛔ 不是"显示不出来"——
 * 渲染成空白会被读成"这一列没数据"（同 `timeText`）。
 */
export function lastUsedText(value: string | null): string {
  if (value === null || value === '') return '—（还没被调用过）'
  return timeText(value)
}

/**
 * 「状态」那一格：用后端的中文名（`state_text`），⛔ 前端不镜像一份词表（E-0021）。
 * 停用态标红——它是**唯一**的关闭动作，而页面必须让人一眼看到"这个应用现在是关的"。
 */
export function stateCell(row: ApiAppRow): string {
  const text = escapeHtml(row.state_text)
  return row.state === 'enabled'
    ? `<span style="color:#009688">${text}</span>`
    : `<span style="color:#FF5722">${text}</span>`
}

/**
 * 回调地址的**即时提示**（纯函数，⛔ 只有这一处判据）。
 *
 * 三个分支各自有用途：
 * - **空串** ⇒ 空提示：`callback_url=""` 是**合法值**（= 没配回调 ⇒ 不推送，§7.1）
 *   ⇒ ⛔ 不许提示"要填"（那会把一个合法状态说成错的）；
 * - **https 开头且后面还有东西** ⇒ 空提示（格式这一层没问题）；
 * - **其余**（http / 别的协议 / 只有 `https://` 没有主机）⇒ `CALLBACK_URL_HINT`。
 *
 * ⚠️ 判据用 `startsWith` + 长度而不是正则：这一条是**提示**，不是校验器；
 * 真判据在后端（含 SSRF 私网段检查，前端拿不到 DNS 解析结果）。⛔ 别把它"升级"成
 * 一个前端校验门——那会把后端的 `1000` 挡在页面之外，而"判据以后端为准"正是本任务的要求。
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
 * ⚠️ 三个分支都对着**真实后果**说：地址空 / 开关停用 ⇒ 这个应用**不会推送任何东西**
 * （用户以为"配好了"而其实一条都不会发，是本仓反复堵的假成功形态）。
 */
export function callbackSaveReport(url: string, state: ApiAppCallbackState): string {
  if (url.trim() === '') {
    return '已保存：回调地址为空 ⇒ 不会推送任何事件（不排队、不重试、也不报错）。'
  }
  if (state === 'disabled') {
    return '已保存：回调开关是「停用」⇒ 不会推送任何事件（地址留着，随时可以再打开）。'
  }
  return '已保存：推送已打开；下面「最近推送记录」里失败的行可以手工重投。'
}

/**
 * 台账一行的**标记**（纯函数）。
 *
 * ⭐ **「重投」按钮只画在 `status === 'failed'` 的行上**（⛔ 不是"画出来再等后端拒"）：
 * 后端对 `pending` / `done` 回 `1000`——把 `done` 重投 = **对方收到两次推送**（§13.18-3）。
 * ⇒ 判据打在**机器值** `status` 上（⛔ 不打在 `status_text` 那句中文上）。
 * ⚠️ 两个来自后端的字符串（`event_text` / `order_no` / `source_order_no` / `status_text` /
 * `reason`）一律 `escapeHtml`（`reason` 是**对方响应里的原文**，最脏的就是它）。
 */
export function deliveryRowHtml(d: ApiDeliveryRow): string {
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
  const retry =
    d.status === 'failed'
      ? `<button type="button" class="layui-btn layui-btn-xs layui-btn-danger" ` +
        `${DELIVERY_RETRY_ATTR}="${d.id}">重投</button>`
      : ''
  return (
    `<tr>` +
    `<td style="white-space:nowrap">${timeText(d.created_at)}</td>` +
    `<td style="white-space:nowrap">${escapeHtml(d.event_text)}</td>` +
    `<td>${order}</td>` +
    `<td style="white-space:nowrap"><span style="color:${statusColor}">` +
    `${escapeHtml(d.status_text)}</span>${next}</td>` +
    `<td>${d.attempts}</td>` +
    `<td>${reason}</td>` +
    `<td>${retry}</td>` +
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
export function deliveriesHtml(items: readonly ApiDeliveryRow[], total: number): string {
  if (items.length === 0) {
    return '<div style="color:#666;line-height:24px">还没有推送记录（这个应用没有产生过事件）。</div>'
  }
  const more =
    total > items.length
      ? `<div style="color:#666;line-height:24px">共 ${total} 条，这里只显示最近 ${items.length} 条。</div>`
      : ''
  return (
    `<table class="layui-table" lay-size="sm" style="margin:0"><thead><tr>` +
    '<th style="width:150px">时间</th>' +
    '<th style="width:120px">事件</th>' +
    '<th>订单号</th>' +
    '<th style="width:120px">状态</th>' +
    '<th style="width:70px">重试次数</th>' +
    '<th>失败原因</th>' +
    '<th style="width:70px">操作</th>' +
    '</tr></thead><tbody>' +
    items.map(deliveryRowHtml).join('') +
    `</tbody></table>${more}`
  )
}

/**
 * 页面标记。
 *
 * ⚠️ **行操作那一格是「函数 `templet`」而不是 layui 的工具栏模板**（`toolbar: '#id'`）：
 * 那颗状态按钮的文案要**逐行不同**（`停用` / `启用`），而工具栏模板是 `<script type="text/html">`
 * + **laytpl** 渲染的 —— 按行分支就得写 `{{# if … }}`，本仓**没有**任何页面在表格模板里用过 laytpl
 * （⛔ 不开这个先例）。函数 `templet` 是普通 JS 函数，**不经过 laytpl**，而 layui 的 `tool` 事件
 * 本来就是**表体上的委托**：vendored `public/layui/layui.js` 的 table 模块里是
 * `s.layBody.on("click","*[lay-event]", …)` ⇒ 单元格是模板渲染的还是函数渲染的，它都认。
 * 所以「一个 `lay-event="toggle"` + 文案由行数据决定」这条路成立，且**不需要**两个按钮。
 *
 * ⚠️ 顶部那段 quote 是**「应用 = 全租户」这份代价的显式告知**（§4-V2 / §8 / §14-11）：
 * 业务方要求删掉「权限设置 / 游戏范围」两块之后，**这里是唯一能说出这句话的地方**
 * ⇒ ⛔ 不许把它挪进某个弹层、也不许删（删了它，这份代价就成了隐性默认）。
 * 成本 / PII 两句同理：它们说的是"第三方能看到什么"，与权限块是否存在无关。
 */
export function buildHtml(): string {
  return `
<script type="text/html" id="openAppsToolbar">
  <button class="layui-btn layui-btn-sm" lay-event="add">新增应用</button>
  <button class="layui-btn layui-btn-sm layui-btn-primary" lay-event="refresh">刷新</button>
  <button class="layui-btn layui-btn-sm layui-btn-normal" lay-event="${DOC_TOOLBAR_EVENT}">${DOC_TOOLBAR_BUTTON_TEXT}</button>
</script>

<blockquote class="layui-elem-quote layui-quote-nm">
  开放平台应用：给第三方一套 <b>AppKey / AppSecret</b>，让它调用本系统的对外接口
  （查单 / 改单 / 改状态 / 上传进度图）。
  <br>
  <span style="color:#FF5722"><b>${FULL_TENANT_NOTICE}</b></span>
  <br>
  <span style="color:#FF5722">${COST_NOTICE}</span>
  <br>
  <span style="color:#FF5722">${PII_NOTICE}</span>
  <br>
  <span style="color:#666">
    应用<b>只能停用、不能删除</b>（停用是唯一的关闭动作，秒级生效）；密钥<b>没有宽限期</b>，
    轮换后旧密钥立刻作废。
  </span>
  <br>
  <span style="color:#666">
    推送按应用各配一份：在「回调与推送」里填<b>只收 https</b> 的地址；
    地址为空或开关停用就<b>不会推送</b>（下面「最近推送记录」里能看到发出去没有）。
  </span>
</blockquote>

<table id="${TABLE_ID}" lay-filter="${TABLE_ID}"></table>

<div class="${FORM_CLASS}" style="display:none;padding:20px 25px 0 0">
  <div class="layui-form-item">
    <label class="layui-form-label">应用名</label>
    <div class="layui-input-block">
      <input type="text" id="${CREATE_NAME_INPUT_ID}" class="layui-input" maxlength="50"
             autocomplete="off" placeholder="给这个第三方起的名字，例如：对接方甲">
    </div>
  </div>
  <div class="layui-form-item">
    <div class="layui-input-block">
      <button type="button" class="layui-btn" id="${CREATE_SAVE_BUTTON_ID}">创建应用</button>
    </div>
  </div>
  <div class="layui-form-mid layui-word-aux" style="padding:0 0 12px 110px">
    创建成功后系统会生成 AppKey 与 AppSecret（<b>密钥在列表里随时可以查看与复制</b>）；
    回调地址与开关建完再到「回调与推送」里配。
  </div>
</div>

<div class="${SECRET_CLASS}" style="display:none;padding:16px 25px 0 0">
  <div style="color:#FF5722;font-weight:700;line-height:24px">${SECRET_PANEL_NOTICE}</div>
  <div class="layui-form-item" style="margin-top:12px">
    <label class="layui-form-label">应用</label>
    <div class="layui-input-block"><span id="${SECRET_APP_NAME_ID}"></span></div>
  </div>
  <div class="layui-form-item">
    <label class="layui-form-label">AppKey</label>
    <div class="layui-input-block">
      <input type="text" id="${SECRET_KEY_TEXT_ID}" class="layui-input" readonly>
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
    <div class="layui-input-block">
      <button type="button" class="layui-btn layui-btn-normal" id="${SECRET_CLOSE_BUTTON_ID}">
        关闭
      </button>
    </div>
  </div>
</div>

<div class="${CALLBACK_CLASS}" style="display:none;padding:16px 20px 0 0">
  <div class="layui-form-mid layui-word-aux" style="padding:0 0 8px 0">
    应用：<b id="${CALLBACK_APP_NAME_ID}"></b>
  </div>
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
  <div style="border-top:1px solid #eee;padding-top:8px">
    <a href="javascript:;" id="${DELIVERIES_TOGGLE_ID}"
       style="font-weight:700;color:#1E9FFF">${DELIVERIES_TOGGLE_CLOSED}</a>
    <div id="${DELIVERIES_BODY_ID}" style="display:none;margin-top:6px;max-height:360px;overflow:auto"></div>
    <div id="${DELIVERIES_WARN_ID}" style="color:#FF5722;line-height:24px"></div>
  </div>
</div>

<style>
  .${DOC_CLASS} table{border-collapse:collapse;width:100%;margin:8px 0}
  .${DOC_CLASS} th,.${DOC_CLASS} td{border:1px solid #e6e6e6;padding:4px 8px;font-size:12px;vertical-align:top}
  .${DOC_CLASS} th{background:#f7f7f7;text-align:left}
  .${DOC_CLASS} pre{background:#f7f7f7;border:1px solid #eee;padding:8px;overflow:auto;font-size:12px}
  .${DOC_CLASS} code{font-family:monospace}
  .${DOC_CLASS} h1{font-size:22px}
  .${DOC_CLASS} h2{font-size:17px;margin:20px 0 8px;border-bottom:1px solid #eee;padding-bottom:4px}
  .${DOC_CLASS} h3{font-size:14px;margin:14px 0 6px}
  .${DOC_CLASS} blockquote{border-left:4px solid #1E9FFF;background:#F5FAFF;margin:8px 0;padding:6px 10px}
  .${DOC_CLASS} ${DOC_TOC_LIST_SELECTOR}{margin:0;padding:0}
  .${DOC_CLASS} ${DOC_TOC_ITEM_SELECTOR}{list-style:none;line-height:22px}
  .${DOC_CLASS} ${DOC_TOC_ITEM_SELECTOR} ul{margin:0;padding-left:14px}
  .${DOC_CLASS} ${DOC_TOC_ITEM_SELECTOR} a{color:#333;display:block;padding:1px 4px;border-radius:2px}
  .${DOC_CLASS} ${DOC_TOC_ITEM_SELECTOR} a:hover{background:#f2f2f2;color:#1E9FFF}
  .${DOC_CLASS} ${DOC_ACTIVE_SELECTOR}{background:#1E9FFF;color:#fff}
  .${DOC_CLASS} h2.${DOC_ACTIVE_CLASS},.${DOC_CLASS} h3.${DOC_ACTIVE_CLASS}{background:#FFFBE6}
</style>

<div class="${DOC_CLASS}" style="display:none;padding:0">
  <div style="padding:8px 12px 6px;border-bottom:1px solid #eee">
    <button type="button" class="layui-btn layui-btn-sm layui-btn-normal"
            id="${DOC_COPY_BUTTON_ID}">${DOC_COPY_BUTTON_TEXT}</button>
    <button type="button" class="layui-btn layui-btn-sm layui-btn-primary"
            id="${DOC_DOWNLOAD_BUTTON_ID}">${DOC_DOWNLOAD_BUTTON_TEXT}</button>
    <div class="layui-form-mid layui-word-aux" style="padding:6px 0 0 0">${DOC_PANEL_NOTICE}</div>
  </div>
  <div style="display:flex;align-items:flex-start">
    <div id="${DOC_TOC_ID}"
         style="flex:0 0 250px;max-width:250px;max-height:72vh;overflow:auto;border-right:1px solid #eee;padding:8px 10px"></div>
    <div id="${DOC_BODY_ID}"
         style="flex:1 1 auto;max-height:72vh;overflow:auto;padding:8px 16px"></div>
  </div>
</div>`
}

/** 弹层借走节点后要能找回来（layui 会把节点搬进 `.layui-layer-content`）。 */
export function findPanel(selector: string): HTMLElement | null {
  return (
    document.querySelector<HTMLElement>(`.layui-layer-content > ${selector}`) ??
    document.querySelector<HTMLElement>(selector)
  )
}

/**
 * 弹层关闭后把节点收回页面（否则第二次打开时取不到节点、弹层静默不出现）。
 *
 * ⚠️ **必须导出**：同组的「打手对接」页（`taker-apps.ts`）的「接入文档」弹层复用它
 * ——那件事与"哪一份文档"无关，两页各写一份必然漂移。
 */
export function reopenGuard(node: HTMLElement, host: HTMLElement): void {
  if (node.parentElement !== host) host.appendChild(node)
}

/** 读弹层里某个输入框 / 文本域的值（节点不在文档里时回空串）。 */
function fieldOf(panel: HTMLElement, id: string): string {
  const el = panel.querySelector<HTMLInputElement | HTMLTextAreaElement>(`#${id}`)
  return el?.value ?? ''
}

export const __testables = {
  actionCell,
  apiDoc,
  buildHtml,
  callbackSaveReport,
  callbackUrlHint,
  copyCredentialText,
  copyTextToClipboard,
  decorateRows,
  deliveriesHtml,
  deliveryRowHtml,
  docTocHtml,
  downloadTextFile,
  htmlToText,
  lastUsedText,
  renderApiDoc,
  secretCell,
  slugifyHeading,
  stateActionLabel,
  stateCell,
  timeText,
}

export default {
  mount: async (root: HTMLElement, _ctx) => {
    document.title = TITLE
    // ⚠️ 顺序是硬要求（`AGENTS.md` §5-G 陷阱 ③）：先挂样式（`bootstrapIsland`），再塞页面标记，
    //    最后 `initIslandElements`（它要按标记里的 `lay-*` / `layui-form` 初始化）。
    const layui: LayuiNamespace = await bootstrapIsland(['table', 'layer', 'form'])
    const table = layui.table as LayuiModule
    const layer = layui.layer as LayuiModule
    // ⭐ `form` 是**回调开关**要的：`lay-skin="switch"` 的裸 input 必须经 `form.render`
    //    才会变成开关（见 `CALLBACK_FORM_FILTER` 的注释）。
    //    ⚠️ 取不到时**降级成不渲染**（不是崩）：测试替身与"layui 半加载"那种情形下，
    //    页面照样能用（原生复选框），只是样式不是 layui 那套。
    const form = (layui as unknown as { form?: LayuiModule }).form
    // `LayuiNamespace.$` 是 `unknown`（`layui/loader.ts`）⇒ 局部收窄成 `layer.open` 要的那一形。
    const $ = (layui as unknown as { $: (el: HTMLElement) => unknown }).$
    const msg = (text: string): void => islandMsg(layer as unknown as Record<string, unknown>, text)

    /** 重渲回调那一块（`lay-skin="switch"` 的开关）。 */
    function renderCallbackForm(): void {
      const render = (form as { render?: (type?: string, filter?: string) => void } | undefined)
        ?.render
      if (typeof render === 'function') render.call(form, 'checkbox', CALLBACK_FORM_FILTER)
    }

    // ⚠️ **本页自己的门禁**（与路由那两道门同源，⛔ 不是第二套判据）：
    //    iframe 可被深链（`page.html?page=open-apps`）打开，那一刻路由的
    //    `requiresAccountAdmin` + `permissions` 都不生效（先例与理由：
    //    `pages/system-roleauthority-edit.ts` 的同名自检）。
    //    ⚠️ 注入标记必须排在门禁**之后**：`layui-island/page.ts` 丢掉 `mount` 的返回值，
    //    拒绝分支返回的 cleanup 永远不会被调用 ⇒ 先注入就会让被拒的深链看到整个页面壳。
    const me = await fetchMe()
    if (me.is_account_admin !== true || !canNode(me, APP_MANAGE_NODE)) {
      msg(NO_ACCESS_MESSAGE)
      return () => {}
    }

    root.innerHTML = buildHtml()
    initIslandElements(layui)

    /** 列表当前数据（弹层要用它拿行）。 */
    let rows: OpenAppTableRow[] = []
    /** 回调面板此刻对着哪个应用（`null` = 没开）——也用来丢弃**迟到**的推送记录响应。 */
    let callbackApp: ApiAppRow | null = null
    /** 在途闸门（逐动作一个；防连点写两次）。 */
    let savingCallback = false
    let savingCreate = false
    let retrying = false
    /**
     * 「API 文档」弹层里的内容**已经上屏过一次**没有（⛔ 每次打开不重跑 `marked`）。
     * ⚠️ 只在本页这一次挂载里有效；`apiDoc()` 自己那份缓存是跨挂载的（文档是静态的）。
     */
    let docInjected = false

    const reloadData = (data: unknown): void =>
      (table.reloadData as (id: string, o: unknown) => void)(TABLE_ID, { data })

    ;(table.render as (o: unknown) => void)({
      elem: TABLE_ELEM,
      id: TABLE_ID,
      // ⚠️ 先 render 一次（哪怕空数据）：实例是在 render 时注册的；数据用 `reloadData` 灌。
      data: [] as ApiAppRow[],
      toolbar: '#openAppsToolbar',
      defaultToolbar: [],
      cols: [
        [
          { field: 'name', title: '应用名', minWidth: 150 },
          { field: 'app_key', title: 'AppKey', width: 180 },
          // ⭐ 密钥**明文进列表**（业务方口径：可随时获取）。⛔ 别把它改成打码 / 折叠。
          { title: 'AppSecret', minWidth: 260, templet: (d: ApiAppRow) => secretCell(d) },
          { title: '状态', width: 90, templet: (d: ApiAppRow) => stateCell(d) },
          {
            title: '最近使用',
            width: 180,
            templet: (d: ApiAppRow) => lastUsedText(d.last_used_at),
          },
          // ⚠️ 函数 `templet`（不是 `toolbar: '#id'`）：状态按钮的文案**逐行不同**，
          //    见 `actionCell` / `stateActionLabel` 与 `buildHtml` 的注释。
          {
            title: '操作',
            // ⚠️ 四颗按钮（回调与推送 / 复制密钥 / 停用启用 / 轮换）⇒ 宽度 300
            //    （挤不下会折行、看着像坏了）。
            width: 300,
            align: 'center',
            templet: (d: OpenAppTableRow) => actionCell(d),
          },
        ],
      ],
      page: false,
      text: { none: '还没有应用；点上面的「新增应用」建一个' },
    })

    async function reload(): Promise<void> {
      // ⚠️ **装配只在这一个出口**：服务端那份行数据先过 `decorateRows()`（按 `state` 算好
      //    状态按钮的文案）再灌进表里 ⇒ 模板里不会出现"要用哪句话"的第二个判据。
      rows = decorateRows(await listApiApps())
      reloadData(rows)
    }

    /** 面板里那三个节点（每次都现查：节点会被 `layer.open` 搬走）。 */
    function panelPart(panel: HTMLElement, id: string): HTMLElement {
      const el = panel.querySelector<HTMLElement>(`#${id}`)
      if (!el) throw new Error(`回调面板里没有 #${id}`)
      return el
    }

    /** 按输入框当前的值刷新那句即时提示（空值 / 合法 https ⇒ 清空）。 */
    function refreshCallbackHint(panel: HTMLElement): void {
      panelPart(panel, CALLBACK_HINT_ID).textContent = callbackUrlHint(
        fieldOf(panel, CALLBACK_URL_INPUT_ID),
      )
    }

    /** 收起 / 展开「最近推送记录」（折叠面板的唯一出口）。 */
    function setDeliveriesOpen(panel: HTMLElement, open: boolean): void {
      panelPart(panel, DELIVERIES_BODY_ID).style.display = open ? '' : 'none'
      panelPart(panel, DELIVERIES_TOGGLE_ID).textContent = open
        ? DELIVERIES_TOGGLE_OPEN
        : DELIVERIES_TOGGLE_CLOSED
    }

    /**
     * 取这个应用最近一批推送记录并上屏。
     *
     * ⚠️ **失败要如实显示**（写进 `DELIVERIES_WARN_ID`），⛔ 不许渲染成"还没有推送记录"：
     * 「一条都没有」与「没取到」长得一样，用户就会以为自己的推送一直没发出去
     * （或者反过来以为很干净）——两个方向都是本仓反复堵的假信号。
     * ⚠️ **迟到响应要丢掉**：面板可以关掉再开另一个应用 ⇒ 用 `callbackApp?.id` 对账。
     */
    async function loadDeliveries(pk: number, panel: HTMLElement): Promise<void> {
      const body = panelPart(panel, DELIVERIES_BODY_ID)
      const warn = panelPart(panel, DELIVERIES_WARN_ID)
      warn.textContent = ''
      try {
        const page = await listDeliveries(pk, { page: 1, page_size: DELIVERIES_PAGE_SIZE })
        if (callbackApp === null || callbackApp.id !== pk) return
        body.innerHTML = deliveriesHtml(page.items, page.total)
      } catch (e) {
        if (callbackApp === null || callbackApp.id !== pk) return
        body.innerHTML = ''
        warn.textContent = e instanceof Error ? e.message : MESSAGES.failed
      }
    }

    /** 重新拉一次推送记录（重投成功之后用）。 */
    async function refreshDeliveries(pk: number): Promise<void> {
      const panel = findPanel(CALLBACK_SELECTOR)
      if (!panel) return
      setDeliveriesOpen(panel, true)
      await loadDeliveries(pk, panel)
    }

    /** 打开「新增应用」弹层。**先清空输入、再 open**（`layer.open` 会把节点搬出原位）。 */
    function openCreate(): void {
      const panel = findPanel(FORM_SELECTOR)
      if (!panel) return
      panel.querySelector<HTMLInputElement>(`#${CREATE_NAME_INPUT_ID}`)!.value = ''
      ;(layer.open as (o: unknown) => number)({
        type: 1,
        title: '新增开放平台应用',
        area: ['560px', '300px'],
        content: $(panel),
        end: () => reopenGuard(panel, root),
      })
    }

    /**
     * 创建成功 / 轮换成功之后把 AppKey 与 AppSecret 一起摆出来（方便当场复制）。
     *
     * ⚠️ **这一层不是"唯一能看到密钥的地方"**：列表里就有它
     * （`secretCell`）、「操作」那一格还能一键复制 ⇒ ⛔ 这一层里**不许**再出现
     * 「只显示这一次 / 关了就没」这类措辞（那会是**与实现相反的一句话**）。
     * ⚠️ 它仍有用：**轮换之后**新密钥当场摆出来最省事（列表要刷新一次才更新）。
     */
    function openSecret(row: ApiAppRow, title: string): void {
      const panel = findPanel(SECRET_SELECTOR)
      if (!panel) return
      panel.querySelector<HTMLElement>(`#${SECRET_APP_NAME_ID}`)!.textContent = row.name
      panel.querySelector<HTMLInputElement>(`#${SECRET_KEY_TEXT_ID}`)!.value = row.app_key
      panel.querySelector<HTMLTextAreaElement>(`#${SECRET_TEXT_ID}`)!.value = row.app_secret
      ;(layer.open as (o: unknown) => number)({
        type: 1,
        title,
        area: ['640px', '440px'],
        content: $(panel),
        end: () => reopenGuard(panel, root),
      })
    }

    /**
     * 打开「回调与推送」面板（回调地址 / 开关 / 最近推送记录）。
     *
     * ⚠️ **行载荷里没有回调那两个键就不要打开**（见文件头的部署面降级分支）：
     * 显示成"空的"会让用户一保存就把真实配置覆盖掉 —— 那是**最坏的那条静默路径**。
     */
    async function openCallback(row: ApiAppRow): Promise<void> {
      const panel = findPanel(CALLBACK_SELECTOR)
      if (!panel) return
      if (!('callback_url' in row) || !('callback_state' in row)) {
        msg(MESSAGES.callbackFieldsMissing)
        return
      }
      panelPart(panel, CALLBACK_APP_NAME_ID).textContent = row.name
      panel.querySelector<HTMLInputElement>(`#${CALLBACK_URL_INPUT_ID}`)!.value = row.callback_url
      // ⚠️ 判据用机器值 `=== 'enabled'`（⛔ 不是真值、也不是中文名）。
      panel.querySelector<HTMLInputElement>(`#${CALLBACK_STATE_SWITCH_ID}`)!.checked =
        row.callback_state === 'enabled'
      refreshCallbackHint(panel)
      // 推送记录：清掉上一个应用的痕迹，并**回到收起态**。
      panelPart(panel, DELIVERIES_BODY_ID).innerHTML = ''
      panelPart(panel, DELIVERIES_WARN_ID).textContent = ''
      setDeliveriesOpen(panel, false)

      callbackApp = row
      ;(layer.open as (o: unknown) => number)({
        type: 1,
        // ⚠️ 应用名是租户自建的自由文本，`layer.open` 的 title 按 HTML 渲染 ⇒ 过 `escapeHtml`。
        title: `回调与推送：${escapeHtml(row.name)}`,
        // ⚠️ 尺寸 `90%` × `90%` 是既有面板口径（与「API 文档」弹层同一对值）；
        //    ⛔ 别改回写死的像素值（原来那对 `760px`/`620px` 是"拍出来的"）。
        //    面板里有一个会变长的推送记录列表（下面那个 `max-height` 容器）⇒ 大一点是对的。
        area: ['90%', '90%'],
        content: $(panel),
        success: () => renderCallbackForm(),
        end: () => {
          callbackApp = null
          reopenGuard(panel, root)
        },
      })
      await loadDeliveries(row.id, panel)
    }

    /**
     * 保存回调设置（`PATCH`，两个键一起发）。
     *
     * ⚠️ **没有前端校验门**：地址是否合法**由后端判**（仅 https + SSRF，§4-V6），
     * 而它拒的时候会回业务码 `1000` + 一句中文 ⇒ 这里 `msg(e.message)` 把它**如实显示**。
     * 前端那句提示（`callbackUrlHint`）是**即时反馈**，⛔ 不是判据——把它做成闸门会让
     * 后端的判据永远打不到页面上（而"判据以后端为准"是这一块的要求）。
     * ⚠️ 两个键**一起发**（地址与开关是同一份配置）：开关单独立刻生效会让"地址空 + 开关开"
     * 变成一个没人明确选过的中间态。
     */
    function saveCallbackSettings(): void {
      if (callbackApp === null || savingCallback) return
      const panel = findPanel(CALLBACK_SELECTOR)
      if (!panel) return
      const app = callbackApp
      // ⚠️ 判据是 DOM 的**此刻**勾选态（⛔ 不从 `callbackApp` 上那份"打开时的状态"推：
      //    用户改完开关再保存，那条路会保存成旧值 —— 静默不生效）。
      const state: ApiAppCallbackState = panel.querySelector<HTMLInputElement>(
        `#${CALLBACK_STATE_SWITCH_ID}`,
      )!.checked
        ? 'enabled'
        : 'disabled'
      const url = fieldOf(panel, CALLBACK_URL_INPUT_ID).trim()
      savingCallback = true
      void updateApiApp(app.id, { callback_url: url, callback_state: state })
        .then(() => reload())
        .then(() => {
          msg(callbackSaveReport(url, state))
        })
        .catch((e: Error) => msg(e.message))
        .finally(() => {
          savingCallback = false
        })
    }

    /**
     * 重投一条**失败**的推送（二次确认 → `POST .../deliveries/<id>/retry/` → 刷新列表）。
     *
     * ⚠️ **在途闸门只包住那次请求**（⛔ 不包住二次确认）：`layer.confirm` 只有在用户点
     * 「确定」时才走回调，把闸门压在它上面会让**点了取消之后**下一次点击静默无效
     * （= 本仓反复堵的"点了没反应"）。代价如实登记：连点两次会开两个确认框，
     * 第二个请求被后端按 `pending` 拒（回 `1000`）并**显示出来**——可见，不静默。
     */
    function confirmRetry(deliveryPk: number): void {
      if (callbackApp === null || retrying) return
      const app = callbackApp
      ;(layer.confirm as (text: string, o: unknown, ok: () => void) => void)(
        `确定重投这一条推送吗（台账第 ${deliveryPk} 条）？我们会把它排回待投递队列，` +
          `投递进程下一轮（约 10 秒内）重发。⚠️ 投递是「至少一次」：` +
          `如果对方上次其实收到了，它会「再收到一次」（对方要按 X-TT-Delivery 去重）。`,
        {},
        () => {
          retrying = true
          void retryDelivery(app.id, deliveryPk)
            .then(() => refreshDeliveries(app.id))
            .then(() => {
              msg('已重投：这一条回到了待投递队列，下一轮投递就会发出去。')
            })
            .catch((e: Error) => msg(e.message))
            .finally(() => {
              retrying = false
            })
        },
      )
    }

    /** 停用 / 启用（`PATCH {state}`）。停用是**唯一**的关闭动作 ⇒ 二次确认里要说清后果。
     *  ⚠️ `layer.confirm` 的 content 按 **HTML** 渲染（E-0303）：应用名是自由文本 ⇒ 逐插值过
     *  `escapeHtml`（F-01）。本页规则：**confirm / alert / msg 的插值一律视为 HTML 上下文**。 */
    function confirmToggle(row: ApiAppRow): void {
      const to: 'enabled' | 'disabled' = row.state === 'enabled' ? 'disabled' : 'enabled'
      const text =
        to === 'disabled'
          ? `确定停用「${escapeHtml(row.name)}」吗？停用后第三方当场调不通（所有调用回「该应用已停用」），回调推送也一起停。`
          : `确定启用「${escapeHtml(row.name)}」吗？启用后第三方就能调它的接口了（推送是否发得出去还要看回调地址与开关）。`
      ;(layer.confirm as (text: string, o: unknown, ok: () => void) => void)(text, {}, () => {
        void updateApiApp(row.id, { state: to })
          .then(() => reload())
          .then(() => msg(to === 'disabled' ? '已停用' : '已启用'))
          .catch((e: Error) => msg(e.message))
      })
    }

    /**
     * 轮换密钥（`POST .../rotate-secret/`）：旧密钥**立即失效**（没有宽限期，§5.3 / §9-12）。
     *
     * ⚠️ 确认文案必须**含**「旧密钥立刻作废 + 第三方当场调不通」（这一条代价不变，§9-12）、
     * **不含**「只显示一次」——密钥在列表里随时看得到，那半句是**与实现相反的一句话**
     * （`__tests__/open-apps.spec.ts` 钉住这一含一不含）。
     * ⭐ 补一句新事实：新密钥**看得到**（轮换后不用抢着复制）。
     */
    function confirmRotate(row: ApiAppRow): void {
      ;(layer.confirm as (text: string, o: unknown, ok: () => void) => void)(
        `确定轮换「${escapeHtml(row.name)}」的密钥吗？「旧密钥立刻作废」` +
          `——正在跑的第三方集成会当场调不通，要有人去改它的配置。` +
          `（新密钥会写进这个应用，列表里随时看得到、也能一键复制。）`,
        {},
        () => {
          void rotateApiAppSecret(row.id)
            .then((saved) => reload().then(() => openSecret(saved, '新密钥（已生效）')))
            .catch((e: Error) => msg(e.message))
        },
      )
    }

    /**
     * 打开「API 文档」全屏弹层（目录 + 正文 + 两颗动作按钮）。
     *
     * ⚠️ 内容是**构建期固化**的那份 `docs/OPEN-API.md`（`?raw`）⇒ 不取后端、不读磁盘。
     * ⚠️ 上屏**只做一次**（`docInjected`）：`marked` 渲染一次、`innerHTML` 写一次，
     * 之后反复开关弹层只是把它搬来搬去（`layer.open` 会移动节点）。
     * ⚠️ 尺寸沿用本页既有面板口径（`90%` × `90%`，与「回调与推送」同一对值）——
     * 这一页的弹层尺寸没有新的对标出处，⛔ 不自己拍一个像素值。
     */
    function openApiDocPanel(): void {
      const panel = findPanel(DOC_SELECTOR)
      if (!panel) return
      const toc = panel.querySelector<HTMLElement>(`#${DOC_TOC_ID}`)
      const body = panel.querySelector<HTMLElement>(`#${DOC_BODY_ID}`)
      // ⚠️ 取不到节点就**不要开空弹层**（先例：`openCreate` 的同款早退）。
      if (!toc || !body) return
      if (!docInjected) {
        const doc = apiDoc()
        toc.innerHTML = docTocHtml(doc.headings)
        // ⚠️ **只插 HTML**（内容是仓库里我们自己的 Markdown）：⛔ 不走 `eval` / `new Function`。
        body.innerHTML = doc.html
        docInjected = true
      }
      ;(layer.open as (o: unknown) => number)({
        type: 1,
        title: DOC_PANEL_TITLE,
        area: ['90%', '90%'],
        content: $(panel),
        end: () => reopenGuard(panel, root),
      })
    }

    /**
     * 点目录里的某一项 ⇒ 跳到对应小节。
     *
     * ⚠️ 判据是**标题上那个 `id`**（与目录项的 `data-doc-anchor` 值是同一个串，
     * 由 `renderApiDoc` 一次生成 ⇒ 不会出现"目录里有、页面上没有"）。
     * ⚠️ 真正滚动靠 `scrollIntoView`；高亮那一份是**视觉反馈**，也是"点到了"的可见证据。
     * ⛔ 别把它换成 `location.hash = …`：iframe 里改 hash 会触发外层的路由，那是另一回事。
     */
    function jumpToDocSection(anchor: string): void {
      const panel = findPanel(DOC_SELECTOR)
      const target = document.getElementById(anchor)
      if (!panel || !target) return
      for (const el of panel.querySelectorAll(DOC_ACTIVE_SELECTOR)) {
        el.classList.remove(DOC_ACTIVE_CLASS)
      }
      panel.querySelector(`[${DOC_ANCHOR_ATTR}="${anchor}"]`)?.classList.add(DOC_ACTIVE_CLASS)
      target.classList.add(DOC_ACTIVE_CLASS)
      // ⚠️ jsdom 没有 `scrollIntoView` ⇒ 这条守卫是**为了测试环境**（真浏览器里一定有）。
      if (typeof target.scrollIntoView === 'function') target.scrollIntoView({ block: 'start' })
    }

    // ── 事件接线（工具栏 / 行操作 / 各面板里的按钮）────────────────────────────
    ;(table.on as (event: string, cb: (obj: unknown) => void) => void)(
      `toolbar(${TABLE_ID})`,
      (obj) => {
        const ev = (obj as { event: string }).event
        if (ev === 'add') return openCreate()
        if (ev === DOC_TOOLBAR_EVENT) return openApiDocPanel()
        if (ev === 'refresh') {
          void reload()
            .then(() => msg('已刷新'))
            .catch((e: Error) => msg(e.message))
        }
      },
    )
    ;(table.on as (event: string, cb: (obj: unknown) => void) => void)(
      `tool(${TABLE_ID})`,
      (obj) => {
        const row = (obj as { data: ApiAppRow }).data
        const ev = (obj as { event: string }).event
        // ⚠️ 每个分支都要有返回值语义：`callback` 是异步的（要另取一次推送记录），失败它自己 msg。
        if (ev === 'callback') {
          void openCallback(row)
          return
        }
        if (ev === 'copysecret') {
          // ⚠️ 复制的内容**从行数据取**（⛔ 不从 DOM 读：那张格子按设计不含密钥）。
          copyCredentialToClipboard(row, (ok) =>
            msg(ok ? `已复制「${escapeHtml(row.name)}」的 AppKey 与 AppSecret` : MESSAGES.copyFailed),
          )
          return
        }
        if (ev === 'toggle') return confirmToggle(row)
        if (ev === 'rotate') return confirmRotate(row)
      },
    )

    const createPanel = findPanel(FORM_SELECTOR)
    createPanel
      ?.querySelector<HTMLButtonElement>(`#${CREATE_SAVE_BUTTON_ID}`)
      ?.addEventListener('click', () => {
        if (savingCreate) return
        const name = fieldOf(createPanel, CREATE_NAME_INPUT_ID).trim()
        if (name === '') return msg(MESSAGES.noName)
        savingCreate = true
        void createApiApp(name)
          .then((saved) => {
            ;(layer.closeAll as (t?: string) => void)('page')
            return reload().then(() => openSecret(saved, '应用已创建：这是它的密钥'))
          })
          .catch((e: Error) => msg(e.message))
          .finally(() => {
            savingCreate = false
          })
      })
    findPanel(SECRET_SELECTOR)
      ?.querySelector<HTMLButtonElement>(`#${SECRET_CLOSE_BUTTON_ID}`)
      ?.addEventListener('click', () => {
        ;(layer.closeAll as (t?: string) => void)('page')
      })

    // ⚠️ 面板里那几个监听**挂在节点自己身上**（与上面那颗创建按钮同一做法）：
    //    `layer.open` 会把**节点**搬进 `.layui-layer-content`，而节点自己的监听**随节点走**
    //    （搬动只断**祖先上的委托**监听）⇒ 直接挂节点是对的。
    //    ⛔ 别改成 `document.addEventListener(… 委托)`：那样每次 mount 都会往 document 上叠一个
    //    闭包（页面被反复挂载/卸载时旧闭包仍会触发 → 一次点击发两次写请求，且**静默**）。
    const callbackPanel = findPanel(CALLBACK_SELECTOR)
    const urlInput = callbackPanel?.querySelector<HTMLInputElement>(`#${CALLBACK_URL_INPUT_ID}`)
    // 即时提示（边打边说；⛔ 它不是判据，见 `callbackUrlHint`）。
    urlInput?.addEventListener('input', () => {
      if (callbackPanel) refreshCallbackHint(callbackPanel)
    })
    callbackPanel
      ?.querySelector<HTMLButtonElement>(`#${CALLBACK_SAVE_BUTTON_ID}`)
      ?.addEventListener('click', () => saveCallbackSettings())
    callbackPanel
      ?.querySelector<HTMLElement>(`#${DELIVERIES_TOGGLE_ID}`)
      ?.addEventListener('click', () => {
        if (!callbackPanel) return
        const body = panelPart(callbackPanel, DELIVERIES_BODY_ID)
        setDeliveriesOpen(callbackPanel, body.style.display === 'none')
      })
    // 「重投」按钮是**列表里的行**（每次刷新重建）⇒ 在容器上挂一个委托（容器节点自己随
    // `layer.open` 一起搬，所以搬动不会断它）。
    callbackPanel
      ?.querySelector<HTMLElement>(`#${DELIVERIES_BODY_ID}`)
      ?.addEventListener('click', (ev) => {
        const target = ev.target as HTMLElement | null
        const button = target?.closest<HTMLElement>(`[${DELIVERY_RETRY_ATTR}]`)
        if (!button) return
        const deliveryPk = Number(button.getAttribute(DELIVERY_RETRY_ATTR))
        if (!Number.isInteger(deliveryPk) || deliveryPk <= 0) return
        confirmRetry(deliveryPk)
      })

    // ── 「API 文档」弹层里的三处接线（目录委托 + 两颗动作按钮）─────────────────
    // ⚠️ 与上面几处同一个做法：监听**挂在节点自己身上**（`layer.open` 只搬节点、不断节点自身的
    //    监听）；⛔ 别改成 `document` 上的委托（每次 mount 会叠一个闭包，一次点击触发多次）。
    const docPanel = findPanel(DOC_SELECTOR)
    docPanel?.querySelector<HTMLElement>(`#${DOC_TOC_ID}`)?.addEventListener('click', (ev) => {
      const link = (ev.target as HTMLElement | null)?.closest<HTMLElement>(`[${DOC_ANCHOR_ATTR}]`)
      const anchor = link?.getAttribute(DOC_ANCHOR_ATTR)
      if (anchor === undefined || anchor === null || anchor === '') return
      jumpToDocSection(anchor)
    })
    docPanel
      ?.querySelector<HTMLButtonElement>(`#${DOC_COPY_BUTTON_ID}`)
      ?.addEventListener('click', () => {
        copyTextToClipboard(openApiDocText(), (ok) =>
          msg(ok ? MESSAGES.copyDocDone : MESSAGES.copyDocFailed),
        )
      })
    docPanel
      ?.querySelector<HTMLButtonElement>(`#${DOC_DOWNLOAD_BUTTON_ID}`)
      ?.addEventListener('click', () => {
        // ⚠️ `false` = 这个环境存不了文件 ⇒ **说人话并给出路**（⛔ 不是点了没反应）。
        if (!downloadTextFile(DOC_DOWNLOAD_FILENAME, openApiDocText())) {
          msg(MESSAGES.docDownloadFailed)
        }
      })

    try {
      await reload()
    } catch (e) {
      msg(e instanceof Error ? e.message : MESSAGES.failed)
    }

    return () => {
      ;(layer.closeAll as (t?: string) => void)('page')
      root.innerHTML = ''
    }
  },
} satisfies IslandPage
