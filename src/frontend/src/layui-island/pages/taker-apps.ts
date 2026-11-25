/**
 * 【开放平台】→「打手对接」页（island key = `taker-apps`，路由 `/open-platform/taker-apps`）。
 *
 * spec：`docs/specs/2026-10-08-taker-open-api-design.md` §3.1（`TakerApp` 的列）/ §6（身份限权）
 * / §7.1（新节点）/ §7.3（菜单与页面）。契约的另一半是**实现即契约**
 * （`src/apps/takerapi/api/app_views.py`、`serializers.py`、`urls.py`）。
 *
 * ## 这一页是**自有功能**，不是复刻
 *
 * 对标物（鸡蛋铺）里没有【开放平台】这一组、更**没有打手侧开放接口**这一套
 * （spec §2 与 §10-7：权限目录从此不再 100% 等于对标全表）⇒ 本页**没有证据件**、
 * 也不走 `mountConfigLike`（那是「取 `public/replica/<key>.html` → 注入 → 灌数据」的复刻专用链路）。
 * 标记由本文件自己写，风格照 `open-apps.ts`（同组的先例）的 layui 惯例。
 * island key 登记在 `config/__tests__/replicaRoutes.spec.ts` 的 `OWN_FEATURE_PAGE_KEYS`，
 * 并在 `docs/FRONTEND-LAYOUT.md` §4 的【开放平台】一节里有一段自己的说明。
 * ⛔ 本仓「像素级 1:1 复刻」的纪律**不适用于本页**（对标物里没有它）⇒ 走的是
 * 「自建 island 页 + 一条**驱动真交互**的页测试」那一路（`AGENTS.md` §5-G）。
 *
 * ## 五块
 *
 * 1. **列表**（`GET /api/taker/apps/`）：⭐ **直接显示该应用的密钥明文**（与上家侧 `D-0201`
 *    同口径：可随时获取，⛔ 没有"只显示一次"那套），并在「操作」那一格给一个
 *    **复制 AppKey + AppSecret** 的动作；
 * 2. **新建**（`POST /api/taker/apps/`）：⭐ **按身份分两副面孔**（见下）；
 * 3. **停用 / 启用**（`PATCH`，`{state}`）：⛔ **没有删除按钮**——「只能停用不能删」，
 *    后端连 `DELETE` 路由都没有（未定义方法回 405）；
 * 4. **轮换密钥**（`POST .../rotate-secret/`）：**单密钥、立即失效**（没有双密钥宽限期）
 *    ⇒ 二次确认里必须写出「旧密钥立刻作废、第三方当场调不通」；
 * 5. **复制密钥**：把 `AppKey` + `AppSecret` 两行写进剪贴板（失败**要说出来**）。
 *
 * ## ⭐ 回调线多出来的三块
 *
 * 6. **回调配置面板**（行内 `lay-event="callback"` ⇒ 弹层）：回调地址输入（上限
 *    `CALLBACK_URL_MAX_LENGTH` = 与后端列宽同值的 200）+ 即时提示 + 回调开关（layui switch）
 *    + 保存按钮；保存走 **一次 `PATCH` 发两个键**（`callback_url` / `callback_state`）；
 * 7. **推送台账**：面板里一个**折叠区**「查看推送台账」，⭐ **懒加载**——面板打开时**不取**，
 *    点开折叠区才 `GET .../deliveries/?page=1&page_size=20`（见 `loadDeliveries` 的注释）；
 * 8. **手工重投**：⭐ **只有 `failed` 行**画「重投」按钮（判据打在**机器值** `status` 上），
 *    二次确认后 `POST .../deliveries/<delivery_pk>/retry/`，成功 ⇒ 重拉台账 + 提示；
 *    失败（例如那一条已经不是 `failed`）⇒ **把后端那句中文原样显示**（⛔ 前端不编第二句）。
 *
 * ⚠️ **与上家侧「应用管理」页（`open-apps.ts`）的取舍**：那三块的**形状**照它做，而
 * **台账渲染那一份是直接 import 它的实现**（`deliveriesHtml` / `DELIVERY_RETRY_ATTR`）——
 * 台账行的 13 键契约两族**逐字同集**，而「只有 `failed` 行有重投按钮」
 * 是**同一条**后端判据 ⇒ ⛔ 不抄第二份渲染（红线：不抄第二份判据）。代价如实登记：
 * 本页因此多一条模块依赖（`taker-apps` → `open-apps`，与「接入文档」那几件同一个方向）；
 * 换来的是那条判据只有**一处**实现。
 * ⚠️ **面板的 `id` / `class` 与文案是本页自己的**（⛔ 不与上家侧共用 `openApps*` 的名字：
 * 两页可能同时存在，同名 `id` 会让 `getElementById` 取到另一页的节点）。
 * ⚠️ **懒加载那一处与上家侧不同**（有意）：那边面板一开就取台账，这边**点开折叠区才取**
 * ——spec 判据 ③ 明写「打开页面时不拉台账；点开折叠后才拉」（防"关着还在刷"）。
 *
 * ## ⭐⭐ 身份限权（spec §6 / §9-10 的落点，本页最承重的一段）
 *
 * | 谁 | 列表 | 创建时的"绑定打手" |
 * |---|---|---|
 * | 持 `system.pinduoduo_settings`（系统管理员那一支） | **全部**（后端 `_visible_apps` 回全部） | **必须选**一个打手账号（**渲染选择器**） |
 * | 持 `system.allow_taker_open_api`（打手本人） | **只有自己的**（后端已收窄） | **锁死为他自己**（**⛔ 不渲染选择器**） |
 *
 * ⚠️ **判据必须与后端同源**：后端的分支谓词是 `has_node_now(user, SYSTEM_ADMIN_NODE)`
 * （`TakerAppAdminView._is_system_admin`），本页用 `canNode(me, SYSTEM_ADMIN_NODE)`
 * （`apps/accounts/permissions.py::is_account_admin` 与 `core.permissions.is_tenant_admin`
 * 在租户平面上同真同假，而 `canNode` 的两条管理员短路与后端 `has_node_now` 的
 * `is_tenant_admin` 短路**同一个谓词**）。⛔ 别改用职位名 / 单看 `is_tenant_admin`：
 * 那会把"被显式授了 `system.pinduoduo_settings` 的非管理员"错判成打手，
 * 于是页面的创建一定失败（后端要他给 `taker`，而页面上根本没有那个选择器）——
 * 正是"门与身份分叉"的形状（`E-0021` 同族）。
 *
 * ⚠️ **列表本身**不靠前端过滤：`_visible_apps` 是**服务端**的唯一口径（⛔ 前端再做一次过滤
 * 就是同一事实两套判据，而漂移只表现为"打手某天看见了别人的行"——行里有**明文密钥**）。
 *
 * ## 四条 island 陷阱（`AGENTS.md` §5-G，改这一页前先读）
 *
 * ① 类名与选择器分成两个常量（`class="${FORM_CLASS}"` 拼标记、`FORM_SELECTOR` 查节点）
 *    —— ⚠️ 回调面板**也要**（`CALLBACK_CLASS` / `CALLBACK_SELECTOR`）；
 * ② 以 `(` 开头的语句一律前置分号（`;(table.render as …)(…)`——ASI 不会在 `)` 换行 `(` 之间插分号）；
 * ③ 顺序：`await bootstrapIsland(...)`（挂样式）→ `root.innerHTML = …`（塞标记）→
 *    `initIslandElements(layui)`（按标记里的 `lay-*` 初始化）；
 * ④ ⚠️ 本页**没有任何定时器**（列表是拉一次 + 每次写动作后重拉一次；台账是**展开那一下**
 *    才拉一次，也不是定时器）⇒ 这一条落在测试的通用纪律上
 *    （`__tests__/taker-apps.spec.ts` 的 `afterEach` 逐个收口）。
 * ⚠️ 四条都有**运行期**判据（那个页测试），不是靠这段注释。
 *
 * ## ⚠️ 两个节点 code **刻意不住在本文件里**
 *
 * `TAKER_APP_NODE` / `SYSTEM_ADMIN_NODE` 从 `@/api/takerApps` import（与上家侧
 * `open-apps.ts` 的 `APP_MANAGE_NODE` **同一手法**）⇒ 本文件里**没有一个带点的小写字面量**，
 * 于是后端那张网（`src/tests/test_rbac_menu_alignment.py` 的源 3）的冻结条数**不因本页变化**。
 * ⛔ **别把它们搬进来**：搬进来就要同一次改那张网的冻结数（那是有意的绊线，但没有额外收益），
 * 而这两个 code 的**取值**另有 `routePermissions.spec.ts` 与 `route-declarations.spec.ts` (c) 钉住。
 *
 * ## ⛔ 三件明确不做（spec §6 / §10）
 *
 * - ⛔ **改不了绑定的打手**：`taker` 不在 `PATCH` 白名单里（它是"这个应用能看什么"的唯一依据，
 *   改它等于**不换凭据就换掉整个权限面**）⇒ 页面上没有这个入口；
 * - ⛔ **没有删除**（只能停用，见上）；
 * - ⛔ **不自造 API 路径**：内部端点就是 `/api/taker/apps/…`，
 *   候选账号列表复用既有的 `GET /api/accounts/`（见 `@/api/takerApps` 的说明）。
 */
import { fetchMe } from '@/api/auth'
import {
  TAKER_APP_NODE,
  SYSTEM_ADMIN_NODE,
  CALLBACK_URL_MAX_LENGTH,
  TAKER_DELIVERIES_PAGE_SIZE,
  createTakerApp,
  listTakerApps,
  listTakerCandidates,
  listTakerDeliveries,
  retryTakerDelivery,
  rotateTakerAppSecret,
  updateTakerApp,
  type TakerAppCallbackState,
  type TakerAppRow,
} from '@/api/takerApps'
import { escapeHtml } from '@/layui-island/escape'
import { canNode } from '@/layui-island/perms'
import { bootstrapIsland, initIslandElements, islandMsg, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

/**
 * ⭐ **接入文档的原文** = `docs/TAKER-API.md` 的**逐字节副本** `src/docs/taker-api.md`。
 *
 * ⛔ **为什么不直接 import 仓库根那份**：Vite 对 root（`src/frontend`）之外的文件有 fs 限制，
 * `?raw` 在 **jsdom 用例**里会报 `Error: Denied ID …`（实测；机制与先例见
 * `src/docs/AGENTS.md` 与 `open-apps.ts` 文件头那一节）⇒ 副本放在 Vite root 之内，
 * `vite build` 与 `vitest run` 两边都解析得到。
 * ⚠️ **两份必须逐字节相同**，这条有网（`__tests__/taker-apps.spec.ts` 用 `fs` 同时读两份对拍）；
 * 改了 `docs/TAKER-API.md` 就重跑一次 `cp ../../docs/TAKER-API.md src/docs/taker-api.md`。
 */
import takerApiDocSource from '../../docs/taker-api.md?raw'
/**
 * ⭐ **渲染 / 复制 / 下载那几个纯函数复用 `open-apps.ts` 那一套**（⛔ 不自己发明第二套渲染方式）。
 *
 * 它们**本来就与"哪一份文档"无关**（入参是一段 markdown 原文）：`renderApiDoc` / `docTocHtml`
 * 是纯函数、`copyTextToClipboard` / `downloadTextFile` 是两个动作出口。
 * ⇒ 副本机制（`?raw` 从 `src/docs/` 读）与渲染链只有**一处**实现，两页共用一个出处。
 * ⚠️ 如实登记的代价：这两页因此有一条**模块依赖**（`taker-apps` → `open-apps`）。
 * 换来的是一条硬保证：**"两页的文档弹层长得一样"这件事不靠人记得同步**。
 */
import {
  copyTextToClipboard,
  DELIVERY_RETRY_ATTR,
  deliveriesHtml,
  docTocHtml,
  downloadTextFile,
  findPanel,
  renderApiDoc,
  reopenGuard,
  type RenderedDoc,
} from './open-apps'

/** island key（= 文件名 = 路由 `meta.islandPage`，三处必须逐字一致）。 */
export const PAGE_KEY = 'taker-apps'
export const TITLE = '打手对接'

/** 新建弹层的**容器 class 名**（拼进标记用）与**选择器**（查节点用）——两者不能混（陷阱 ①）。 */
export const FORM_CLASS = 'takerAppsForm'
export const FORM_SELECTOR = `.${FORM_CLASS}`

/** 列表表的 id / elem（layui 的 `reloadData` / `on` 要的是 **id**，带 `#` 是静默 no-op）。 */
export const TABLE_ID = 'takerAppsTable'
export const TABLE_ELEM = '#takerAppsTable'

/** 新建弹层里的那些 `id`。⚠️ 选择器那个**只在管理员那一副面孔里存在**。 */
export const NAME_INPUT_ID = 'takerAppsName'
export const TAKER_SELECT_ID = 'takerAppsTaker'
export const PICKER_HINT_ID = 'takerAppsPickerHint'
export const CREATE_SAVE_BUTTON_ID = 'takerAppsCreateSaveBtn'

/**
 * 「操作」那一格的四个动作标记（**恰好四个**：`callback` / `copysecret` / `toggle` / `rotate`）。
 *
 * ⚠️ **属性名进标记、选择器用来找节点**（陷阱 ① 的同族；本页今天没有"按属性找节点"的地方，
 * 但把它抽成常量是给下一个人的信号：本页不允许把 `lay-event` 值写死在两处）。
 * ⭐ 恰好集合由页测试钉住（`__tests__/taker-apps.spec.ts`「操作」那一格那条）：
 * 加一个 `lay-event` 就要同一次改它（⛔ 不许放宽成"包含即可"）。
 */
export const TOOLBAR_ID = 'takerAppsToolbar'

// ── 「回调与推送」面板的节点 ─────────────────────────────────────────────────────
//
// ⚠️ 命名一律带 `takerApps` 前缀（⛔ 不与上家侧 `open-apps.ts` 的 `openApps*` 共用）：
// 两页在同一个 iframe 壳里可能同时存在，同名 `id` 会让 `getElementById` 取到**另一页**的节点。

/** 回调面板的**容器 class 名**（拼标记）与**选择器**（查节点）——两者不能混（陷阱 ①）。 */
export const CALLBACK_CLASS = 'takerAppsCallback'
export const CALLBACK_SELECTOR = `.${CALLBACK_CLASS}`

/** 面板里的那些 `id`（应用名 / 地址 / 即时提示 / 开关 / 保存）。 */
export const CALLBACK_APP_NAME_ID = 'takerAppsCallbackAppName'
export const CALLBACK_URL_INPUT_ID = 'takerAppsCallbackUrl'
export const CALLBACK_HINT_ID = 'takerAppsCallbackHint'
export const CALLBACK_STATE_SWITCH_ID = 'takerAppsCallbackState'
export const CALLBACK_SAVE_BUTTON_ID = 'takerAppsCallbackSaveBtn'
/**
 * 回调那一块的 `layui-form` 容器（`id` 与 `lay-filter` **同值**，这是 layui 的用法）。
 *
 * ⚠️ **`form.render` 的根选择器就是 `.layui-form`**（vendored `public/layui/layui.js` 里
 * `h=".layui-form"`，`render(type, filter)` 取 `.layui-form[lay-filter="…"]`）⇒
 * 没有这一个容器，`lay-skin="switch"` 的那个裸 input **不会被渲染成开关**，
 * 页面上就是一个原生复选框。⇒ 本页的 `form.render` 调用都带上 `CALLBACK_FORM_FILTER`
 * （⛔ 不用无参的全局渲染：那会把页面上别的 `.layui-form` 一起重渲）。
 */
export const CALLBACK_FORM_ID = 'takerAppsCallbackForm'
export const CALLBACK_FORM_FILTER = 'takerAppsCallbackForm'
/** 开关自己的 `lay-filter`（真浏览器里 `form.on('switch(…)')` 按它认领；本页只用 DOM 勾选态）。 */
export const CALLBACK_STATE_FILTER = 'takerAppsCallbackStateToggle'
/** 开关的 `name`（与 `id` **同值**：layui 的开关按 `name` 归属，缺它渲染不出开关）。 */
export const CALLBACK_STATE_NAME = 'takerAppsCallbackStateToggle'

/** 「查看推送台账」那块：折叠开关 + 列表容器 + 失败提示。 */
export const DELIVERIES_TOGGLE_ID = 'takerAppsDeliveriesToggle'
export const DELIVERIES_BODY_ID = 'takerAppsDeliveriesBody'
export const DELIVERIES_WARN_ID = 'takerAppsDeliveriesWarn'

/** 「没有权限进这一页」的自检提示（页面被深链打开时用）。 */
export const NO_ACCESS_MESSAGE = '无权限管理打手对接凭据'

/**
 * ⭐ **这份凭据的知情面 = 该页可见面，而它的能力面 = 「绑定的那个打手能看什么」**
 * （spec §3.1 / §10-4 / §10-5）。⛔ 这段话必须在页面上说破，别让它成为隐性默认：
 * 拿密钥的人能**以那个打手的身份**去大厅抢单、查他的单、改他单的状态。
 */
export const AUTHORITY_NOTICE =
  '这套凭据的能力 = <b>绑定的那个打手能看什么</b>：拿到密钥的一方可以以他的身份拉大厅、抢单、' +
  '查他的单、改他单的状态、传进度图。'

/**
 * ⚠️ **能力会静默跟着职位 / 节点变**（spec §10-4）——业务方与管理员都必须知道这一句：
 * 今天把某个角色的节点一改，那套外部系统明天就多/少了一批能力，而**两边都不会报错**。
 */
export const ROLE_NOTICE =
  '⚠️ 绑定账号的职位 / 权限节点一改，这套外部凭据的能力就<b>静默跟着变</b>' +
  '（范围判据与抢单判据都取自那一行）——改完记得回来看一眼。'

/** 打手那一副面孔的说明（他只能管自己的）。 */
export const TAKER_SCOPE_NOTICE =
  '这里只会出现<b>你自己的</b>接入凭据：新建出来的应用自动绑在你名下（不需要、也不允许选别人）。'

/** 管理员那一副面孔的说明（他看全部、且必须选一个打手）。 */
export const ADMIN_SCOPE_NOTICE =
  '你是系统管理员：这里能看到<b>全部</b>打手侧凭据；新建时必须<b>指定它绑定哪个打手账号</b>' +
  '（范围判据与抢单判据都从那一个账号算，spec §6）。'

// ── 回调与推送那三块的文案 ─────────────────────────────────────────────────────

/**
 * 回调地址的**即时提示**（本页这一侧的唯一一句）。
 *
 * ⚠️ **它不是判据**（判据在后端：保存时 + 每次投递前各一次，
 * 且包含 SSRF 私网段检查——前端**看不到** DNS 解析结果，做不到那一条）。
 * ⇒ 这里只在格式明显不对时**当场说一句**；真判据以后端回的 `1000` 为准，
 * 页面把那句中文**如实显示**（见 `saveCallbackSettings`）。
 * ⚠️ 措辞要比上家侧那句多说**「公网」**：这里是把地址给**打手自己的服务器**用，
 * "私网 / 回环 / 链路本地一律拒"是打手最容易踩的那一格
 * （上家侧那句只说了 https；两族的**文案**各写各的，不是谁抄谁）。
 */
export const CALLBACK_URL_HINT =
  '回调地址必须是 https 开头的公网地址（http 与其它协议一律不收；内网 / 回环地址后端也会拒）。'

/**
 * 回调那一块的**人话说明**（三件事必须写清：https 公网 only / 空或停用时**不推送** / 对方要验签名）。
 *
 * ⚠️ 三个头的名字与签名要素来自**共享投递引擎的同一份实现**
 * （`apps/openapi/outbound.py` 的 `HEADER_SIGNATURE` 等常量；打手侧**没有**第二份 outbound，
 * 它经 `CredentialSpec` 参数化后走同一份）：
 * `X-TT-Timestamp` / `X-TT-Delivery` / `X-TT-Signature`。
 * ⛔ 页面上不抄那段公式（抄了就会漂），只说"要用 AppSecret 按我们的算法验真"。
 * ⚠️ 强调一律用 HTML 的 `<b>`（这段是塞进 `innerHTML` 的）——⛔ 不要写 markdown 的 `**`：
 * 浏览器不认 markdown，用户会**连着星号一起看到**。
 * ⚠️ **推的是"打手当前接手的单"**：大厅里来了新单**不推**（仍靠轮询）
 * ——这一句必须说，否则打手会以为"有回调就不用轮询了"。
 */
export const CALLBACK_NOTICE =
  '推送怎么走：<b>你当前接手的单</b>发生状态变化 / 进度图新增 / 被删除 / 被改派人时，' +
  '我们会按产生顺序 POST 到上面的地址，每条都带 <b>X-TT-Timestamp</b> / <b>X-TT-Delivery</b> / ' +
  '<b>X-TT-Signature</b> 三个头，对方要用 AppSecret 按我们的算法<b>验真</b>再处理' +
  '（⛔ 不要只看来源 IP 就算数）。' +
  '<br>⚠️ <b>大厅里来了新单不推</b>：想第一时间知道"有新单可抢"仍然要轮询大厅接口。' +
  '<br>地址为空、或者开关是「停用」⇒ <b>不会推送</b>（不排队、不重试、也不报错）；' +
  '推送失败会自动重试 6 次（10 秒 / 1 分 / 5 分 / 15 分 / 1 小时 / 6 小时），仍失败就落在这里，可以手工重投；' +
  '因为是「至少一次」，对方要按 <b>X-TT-Delivery</b> 去重。' +
  '<br>地址<b>只收 https 且必须是公网</b>：保存时与每次投递前各校验一次（DNS 会变），' +
  '内网 / 回环 / 带用户名密码的地址会被拒。'

/** 折叠区那两个文案（折叠态 / 展开态）。 */
export const DELIVERIES_TOGGLE_CLOSED = '查看推送台账'
export const DELIVERIES_TOGGLE_OPEN = '收起推送台账'

/**
 * 保存回调配置之后那句**回报**（⛔ 不许"保存成功"四个字了事）。
 *
 * ⚠️ 三个分支都对着**真实后果**说：地址空 / 开关停用 ⇒ 这个应用**不会推送任何东西**
 * （用户以为"配好了"而其实一条都不会发，是本仓反复堵的假成功形态）。
 * ⚠️ 与上家侧 `open-apps.ts::callbackSaveReport` 是**两份文案**（形状同款、字面值各一份）：
 * 它指向的折叠区标题在本页叫「查看推送台账」⇒ 抄它那句会指向一个本页没有的名字。
 */
export function callbackSaveReport(url: string, state: TakerAppCallbackState): string {
  if (url.trim() === '') {
    return '已保存：回调地址为空 ⇒ 不会推送任何事件（不排队、不重试、也不报错）。'
  }
  if (state === 'disabled') {
    return '已保存：回调开关是「停用」⇒ 不会推送任何事件（地址留着，随时可以再打开）。'
  }
  return '已保存：推送已打开；下面「查看推送台账」里失败的行可以手工重投。'
}

/**
 * 回调地址的**即时提示**（纯函数，⛔ 只有这一处判据）。
 *
 * 判据（与上家侧 `open-apps.ts::callbackUrlHint` **同一条规则**，文案各一份）：
 * - **空串** ⇒ 空提示：`callback_url=""` 是**合法值**（= 没配回调 ⇒ 不推送）
 *   ⇒ ⛔ 不许提示"要填"（那会把一个合法状态说成错的）；
 * - **https 开头且后面还有东西** ⇒ 空提示（格式这一层没问题）；
 * - **其余**（http / 别的协议 / 只有 `https://` 没有主机）⇒ `CALLBACK_URL_HINT`。
 *
 * ⚠️ 判据用 `startsWith` + 长度而不是正则：这一条是**提示**，不是校验器；
 * 真判据在后端（含 SSRF 私网段检查，前端拿不到 DNS 解析结果）。⛔ 别把它"升级"成
 * 一个前端校验门——那会把后端的 `1000` 挡在页面之外，而"判据以后端为准"正是这一块的要求。
 */
export function callbackUrlHint(value: string): string {
  const trimmed = value.trim()
  if (trimmed === '') return ''
  const prefix = 'https://'
  if (trimmed.toLowerCase().startsWith(prefix) && trimmed.length > prefix.length) return ''
  return CALLBACK_URL_HINT
}

/** 页内其它用户可见文案（抽成常量只为让 spec 钉住字面值）。 */
export const MESSAGES = {
  /** 应用名空着就保存。 */
  noName: '请填写应用名',
  /** 管理员没选绑定账号就保存。 */
  noTaker: '请选择要绑定的打手账号',
  /** 保存 / 轮换 / 启停失败时的兜底（后端文案通常已经有了）。 */
  failed: '操作失败',
  /**
   * ⚠️ 候选账号列表取不到（**降级分支，不是防御性写法**）：
   * `GET /api/accounts/` 的门是 `IsAccountAdmin`，而"能进这一页"还可能是"被显式授了
   * `system.pinduoduo_settings` 或本页节点的非管理员角色" ⇒ 那种账号会走到这里。
   * ⛔ 不许静默变成一个空下拉（那样用户只会以为"本租户一个人都没有"）。
   */
  pickerFailed: '候选账号列表取不到（后端没放行这条请求），无法新建：',
  /** 候选取到了、但一条都没有。 */
  pickerEmpty: '本租户没有可绑定的账号。',
  /** 候选只取了第一页。 */
  pickerTruncated: '账号较多，这里只显示前 {shown} 个（共 {total} 个）。',
  /**
   * 复制失败（缺 `navigator.clipboard` / 被拒）。
   * ⚠️ 必须**说出来**并告诉人怎么办：静默失败会让用户以为剪贴板里已经有了。
   */
  copyFailed: '复制失败（浏览器不允许自动复制），请手动选中列表里的密钥复制',
  /**
   * 行载荷里没有回调那两个键（**部署面**的降级分支，与上家侧同款）。
   * ⚠️ 必须**说出来并中止**：当成"没配回调"显示成空，用户一保存就把真实配置覆盖掉。
   */
  callbackFieldsMissing:
    '后端这一次没有回这个应用的回调配置（只回了别的字段），为避免把现有配置覆盖成空，' +
    '这一次不打开设置面板。请刷新页面重试；若一直如此，说明前端与后端版本不一致。',
  /**
   * 重投成功之后那句回报。
   * ⚠️ 必须写出**两个后果**：① 投递进程下一轮（约 10 秒内）才发；② 投递是「至少一次」
   * ——对方上次其实收到了的话会**再收到一次**（❌ 不是"重投成功"四个字了事）。
   * ⚠️ **失败那一支⛔ 不在这里**：那必须显示**后端那句中文**（`e.message`），
   * 前端⛔ 不许自己编一句（spec 判据 3 逐字要求）。
   * ⚠️ 上面那两个后果**必须落在这一句自己的字面值里**（⛔ 不能只写在二次确认里：
   * 用户点了「确定」之后看到的就是这一句，那句话才是"回报"）⇒ spec 判据 1f 逐字钉住
   * 「至少一次」/「再收到一次」/「X-TT-Delivery」三个片段，谁把这半句删掉谁红。
   */
  retryDone:
    '已重投：这一条回到了待投递队列，下一轮投递（约 10 秒内）就会发出去。' +
    '⚠️ 投递是「至少一次」：如果对方上次其实收到了，它会「再收到一次」' +
    '（对方要按 X-TT-Delivery 去重）。',
} as const

// ── 「接入文档」弹层────────────────────────────────────────────
//
// 形状照同组的 `open-apps.ts`（【应用管理】那一页的「API 文档」弹层）**逐条同款**：
// 目录 + 正文两个滚动容器 + 「复制 Markdown 原文」/「下载 .md」两颗按钮。
// ⛔ **别自己发明第二套渲染方式**：`renderApiDoc` / `docTocHtml` / 两个动作出口
// 都是从那个模块 import 的同一份实现（理由见文件头 import 处那段注释）。

/** 文档弹层的**容器 class 名**（拼标记）与**选择器**（查节点）——两者不能混（陷阱 ①）。 */
export const DOC_CLASS = 'takerAppsApiDoc'
export const DOC_SELECTOR = `.${DOC_CLASS}`
/** 目录 / 正文两个滚动容器，加两颗动作按钮的 `id`。 */
export const DOC_TOC_ID = 'takerAppsDocToc'
export const DOC_BODY_ID = 'takerAppsDocBody'
export const DOC_COPY_BUTTON_ID = 'takerAppsDocCopyBtn'
export const DOC_DOWNLOAD_BUTTON_ID = 'takerAppsDocDownloadBtn'
/**
 * 目录项上的锚点属性名（陷阱 ① 的同族：**属性名进标记**、选择器 `[${DOC_ANCHOR_ATTR}]` 找节点）。
 * ⚠️ 值放**标题那个锚点 id**（与标题上的 `id=` 逐字同值 ⇒ 点过去必然有对应小节）。
 */
export const DOC_ANCHOR_ATTR = 'data-doc-anchor'
/** 目录项的 class 名 / 选择器（`data-doc-depth` 区分 2 / 3 两级）。 */
export const DOC_TOC_ITEM_CLASS = 'takerAppsApiDocTocItem'
export const DOC_TOC_ITEM_SELECTOR = `.${DOC_TOC_ITEM_CLASS}`
/** 目录那个 `<ul>` 的 class 名 / 选择器。 */
export const DOC_TOC_LIST_CLASS = 'takerAppsApiDocTocList'
export const DOC_TOC_LIST_SELECTOR = `.${DOC_TOC_LIST_CLASS}`
/** 「当前所在小节」的高亮 class 名 / 选择器（点目录时打在目录项与那个标题上）。 */
export const DOC_ACTIVE_CLASS = 'takerAppsApiDocActive'
export const DOC_ACTIVE_SELECTOR = `.${DOC_ACTIVE_CLASS}`
/** 标题锚点 id 的前缀（唯一性由紧随其后的序号保证，见 `renderApiDoc`）。 */
export const DOC_ANCHOR_PREFIX = 'takerApiDocSection'
/** 工具栏那颗按钮的 `lay-event`。 */
export const DOC_TOOLBAR_EVENT = 'apidoc'
/** 「下载 .md」写出来的文件名（就是出处那份文件的名字）。 */
export const DOC_DOWNLOAD_FILENAME = 'TAKER-API.md'
/** 弹层标题（把出处写进标题里，省得有人以为这是另一份文档）。 */
export const DOC_PANEL_TITLE = '接入文档（docs/TAKER-API.md）'
/** 两颗动作按钮的文案（抽成常量只为让 spec 钉住字面值）。 */
export const DOC_COPY_BUTTON_TEXT = '复制 Markdown 原文'
export const DOC_DOWNLOAD_BUTTON_TEXT = '下载 .md'
/** 工具栏那颗按钮的文案。 */
export const DOC_TOOLBAR_BUTTON_TEXT = '接入文档'
/**
 * 弹层顶部那句说明。
 *
 * ⚠️ **必须说清「内容不是前端写的」**：文档改了要重新构建才带上（否则用户会以为页面坏了）。
 * ⛔ 这里不许出现 markdown 的 `**`（走 `innerHTML`，浏览器不认 markdown）——强调用 `<b>`。
 * ⚠️ 它同时要说清**这一份是给谁的**：这套凭据的能力面是"那个打手能看什么"，
 * 开发者拿这份文档去对接时必须知道自己代表谁。
 */
export const DOC_PANEL_NOTICE =
  '内容就是仓库里的 <b>docs/TAKER-API.md</b>（渲染成网页版看更方便）：' +
  '左边点目录跳小节，右边是正文。要原文就点上面两颗按钮（复制 / 下载）。' +
  '⚠️ 这份是给<b>用别家系统的外部工作室（打手身份）</b>的接入契约；' +
  '文档改动要重新构建前端才会带上。'

/** 「复制文档」两句话（失败时**必须**给一条出路，⛔ 不是"复制失败"四个字了事）。 */
export const DOC_COPY_DONE = '已复制接入文档的 Markdown 原文'
export const DOC_COPY_FAILED = '复制失败（浏览器不允许自动复制），请用旁边的「下载 .md」'
/** 「下载 .md」在这个环境里做不到时的那句话（给出路：直接打开仓库里那个文件）。 */
export const DOC_DOWNLOAD_FAILED =
  '这个浏览器不支持直接下载——原文在仓库的 docs/TAKER-API.md 里，直接打开那个文件即可。'

/**
 * 列表里的一行 = 服务端那一行 + **本页在组装时按该行 `state` 预算出来的按钮文案**。
 *
 * 为什么要多这一个字段：那颗状态按钮是**一个** `lay-event="toggle"`，文案必须**逐行不同**
 * （`停用` / `启用`）——⛔ 不许做成"两个按钮各挂一个事件"，⛔ 也不许上 laytpl
 * （与 `open-apps.ts` 同一条判断：本仓没有在表格模板里用过 laytpl 的先例）。
 */
export type TakerAppTableRow = TakerAppRow & { state_action_label: string }

/**
 * 那颗状态按钮的**文案**（纯函数，⛔ 只有这一处判据）。
 *
 * ⚠️ 判据用 `row.state`（枚举值），⛔ **不用** `state_text`（中文名服务端可以改，用它做判据会静默错）。
 */
export function stateActionLabel(row: TakerAppRow): string {
  return row.state === 'enabled' ? '停用' : '启用'
}

/** 把服务端回的行**映射**成表格行（`table.render` / `reloadData` 之前统一走一遍）。 */
export function decorateRows(rows: readonly TakerAppRow[]): TakerAppTableRow[] {
  return rows.map((row) => ({ ...row, state_action_label: stateActionLabel(row) }))
}

/**
 * 「操作」那一格（函数 `templet`，**不经过 laytpl**）。
 *
 * 四个 `lay-event`（`callback` / `copysecret` / `toggle` / `rotate`）**一个不多一个不少**：
 * - ⭐ `callback`：打开「回调与推送」面板（回调地址 / 开关 / 推送台账）；
 * - ⛔ **没有 `del`** —— 「只能停用不能删」（后端连 `DELETE` 路由都没有）；
 * - ⛔ **没有"改绑定"** —— `taker` 不在 `PATCH` 白名单里（spec §6，理由见文件头）。
 * ⚠️ **密钥不进这一格的标记**：HTML 里**一个字都不含**密钥 ⇒ 复制时从 `row.app_secret`
 * （内存里的行数据）取，⛔ 不写成 `data-clipboard-text="…"`：那样密钥会**固化进 DOM 属性**
 * （截图 / 调试面板 / 页面快照都拿得到）。
 */
export function actionCell(row: TakerAppTableRow): string {
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
 * ⭐ 明文上屏（与上家侧 `D-0201` 同口径），⚠️ **但要走 `escapeHtml`**：密钥是服务端生成的
 * URL-safe 串（理论上不含 `<`），而"密钥来自库、库里的值可能被人改" ⇒ 这里**不假设**它的字符集。
 * ⚠️ 单元格宽度有限 ⇒ 包一个 `word-break:break-all` 的容器（`layui-table-cell` 默认
 * `overflow:hidden`，长串会被裁掉，"看得见"就成了半句）。
 */
export function secretCell(row: TakerAppRow): string {
  return (
    `<span style="word-break:break-all;font-family:monospace">` +
    `${escapeHtml(row.app_secret)}</span>`
  )
}

/** 「状态」那一格：用后端的中文名（`state_text`），⛔ 前端不镜像一份词表（E-0021）。 */
export function stateCell(row: TakerAppRow): string {
  const text = escapeHtml(row.state_text)
  return row.state === 'enabled'
    ? `<span style="color:#009688">${text}</span>`
    : `<span style="color:#FF5722">${text}</span>`
}

/**
 * 一个 ISO 8601 时刻的**显示形态**（纯函数）：`2026-10-08 17:20:00`。
 *
 * ⚠️ 只截到秒、**不做时区换算**（后端给的就是带偏移的本地时刻）；
 * `null` / 空串 ⇒ `—`（⛔ 不是空白格：空白会被读成"这一列没数据"）。
 */
export function timeText(value: string | null): string {
  if (value === null || value === '') return '—'
  return escapeHtml(value.replace('T', ' ').slice(0, 19))
}

/**
 * 「最近使用」那一格。
 *
 * ⚠️ `null` 的含义是「**一次都没验签成功过**」，⛔ 不是"显示不出来"——渲染成空白会被误读。
 */
export function lastUsedText(value: string | null): string {
  if (value === null || value === '') return '—（还没被调用过）'
  return timeText(value)
}

/**
 * 「复制密钥」的剪贴板出口。
 *
 * ⚠️ **缺 `navigator.clipboard` 不是静默失败**（jsdom 与非安全上下文里它整个不存在；
 * `writeText` 也可能被判 `NotAllowedError`）⇒ 失败必须**说出来**。
 * ⚠️ 复制的内容是 `app_key` + `app_secret` **两行**（第三方对接要的就是这一对）。
 */
export function copyCredentialText(row: TakerAppRow): string {
  return `AppKey: ${row.app_key}\nAppSecret: ${row.app_secret}`
}

// ⚠️ `copyTextToClipboard` 也是**从 `open-apps.ts` import 的同一个实现**（见文件头）：
//    "把一段文本写进剪贴板、失败要说出来"与"凭据 / 文档"无关 ⇒ 本文件不再各写一份
//    （两份的失败分支只要有一处漏掉 `onDone(false)`，那处就变成**静默失败**）。

/** 候选账号的 `id` → 显示形态（⛔ 只用于上屏，不作判据）。 */
export function candidateLabel(row: { username: string; display_name: string }): string {
  const name = row.display_name === '' ? row.username : row.display_name
  return `${name}（${row.username}）`
}

/**
 * 候选账号的 `<option>` 列表（纯函数）。
 *
 * ⚠️ 值与文字都走 `escapeHtml`：账号名是**用户数据**（本仓红线：来自库的值一律转义）。
 * ⚠️ 第一项是**空值占位**：管理员必须**显式选**一个（spec §6「必须选一个打手账号」），
 * 默认选中第一条会让"顺手建成了别人的应用"变成默认行为。
 */
export function candidateOptionsHtml(
  items: readonly { id: number; username: string; display_name: string }[],
): string {
  const head = '<option value="">请选择要绑定的打手账号</option>'
  return (
    head +
    items
      .map(
        (row) =>
          `<option value="${row.id}">${escapeHtml(candidateLabel(row))}</option>`,
      )
      .join('')
  )
}

/**
 * 候选列表的**提示文**（纯函数，⛔ 只有这一处判据）。
 *
 * 三个分支各自有用途：没有行 ⇒ 说"一个都没有"（⛔ 不是留空）；
 * 拿到的不全 ⇒ 如实说差多少（⛔ 不假装这就是全部）；拿全了 ⇒ 空提示。
 */
export function pickerHintText(shown: number, total: number): string {
  if (shown === 0) return MESSAGES.pickerEmpty
  if (total > shown) {
    return MESSAGES.pickerTruncated.replace('{shown}', String(shown)).replace(
      '{total}',
      String(total),
    )
  }
  return ''
}

/**
 * 页面标记。
 *
 * ⭐ **`showTakerPicker` 是身份那一半的落点**：`false`（打手）⇒ 那一块**根本不渲染**
 * （⛔ 不是"渲染出来再禁用"：禁用还看得见，用户会反复试）。
 * ⚠️ 顶部那段 quote 是**这份凭据的能力边界**的显式告知（`AUTHORITY_NOTICE` + `ROLE_NOTICE`）：
 * 它是页面上唯一能说出「拿密钥的人 = 那个打手的身份」的地方 ⇒ ⛔ 不许删。
 */
/**
 * 文档原文（`?raw` 进来的是**构建期常量**）。
 *
 * ⚠️ **必须走这个函数拿原文**，⛔ 不要把那个 import 绑定直接当实参传出去：压缩器会把
 * **整份字符串字面量内联到调用点**，而模块级那个绑定仍然留着 ⇒ 产物里**存了两份**。
 * 实测读数与完整理由见 `open-apps.ts` 的 `openApiDocText()`（同一条纪律、同一份证据）。
 */
export function takerApiDocText(): string {
  return takerApiDocSource
}

/**
 * 页面用的那一份渲染结果（**惰性算一次就缓存**）。
 *
 * ⚠️ 渲染链是 `open-apps.ts` 那份**同一个实现**（⛔ 本文件不新写一套）。
 * ⚠️ 缓存的意义是「渲染只走 `marked` 一次」：弹层反复开关**不会**重跑渲染。
 * ⚠️ 文档内容在构建期就固化进产物了 ⇒ 缓存不会读到旧内容（改了文档要重新构建）。
 */
let takerApiDocCache: RenderedDoc | null = null
export function takerApiDoc(): RenderedDoc {
  if (takerApiDocCache === null) takerApiDocCache = renderApiDoc(takerApiDocText())
  return takerApiDocCache
}

/** 文档弹层那一段标记（目录 + 正文 + 两颗动作按钮）。 */
function docPanelHtml(): string {
  return `
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

export function buildHtml(showTakerPicker: boolean): string {
  const picker = showTakerPicker
    ? `
  <div class="layui-form-item">
    <label class="layui-form-label">绑定打手</label>
    <div class="layui-input-block">
      <select id="${TAKER_SELECT_ID}" class="layui-input"></select>
      <div class="layui-form-mid layui-word-aux" id="${PICKER_HINT_ID}"
           style="padding:0;color:#FF5722"></div>
    </div>
  </div>`
    : ''
  const scope = showTakerPicker ? ADMIN_SCOPE_NOTICE : TAKER_SCOPE_NOTICE
  return `
<script type="text/html" id="${TOOLBAR_ID}">
  <button class="layui-btn layui-btn-sm" lay-event="add">新增打手凭据</button>
  <button class="layui-btn layui-btn-sm layui-btn-primary" lay-event="refresh">刷新</button>
  <button class="layui-btn layui-btn-sm layui-btn-normal" lay-event="${DOC_TOOLBAR_EVENT}">${DOC_TOOLBAR_BUTTON_TEXT}</button>
</script>

<blockquote class="layui-elem-quote layui-quote-nm">
  <b>打手对接</b>：给<b>打手</b>（用别家系统的外部工作室）一套 <b>AppKey / AppSecret</b>，
  让他调用本系统的对外接口（拉抢单大厅 / 抢单 / 查他的单 / 改他单的状态 / 传进度图）。
  <br>
  <span style="color:#FF5722"><b>${AUTHORITY_NOTICE}</b></span>
  <br>
  <span style="color:#FF5722">${ROLE_NOTICE}</span>
  <br>
  <span style="color:#666">
    凭据<b>只能停用、不能删除</b>（停用是唯一的关闭动作，秒级生效）；密钥<b>没有宽限期</b>，
    轮换后旧密钥立刻作废。
  </span>
  <br>
  <span style="color:#666">${scope}</span>
</blockquote>

<table id="${TABLE_ID}" lay-filter="${TABLE_ID}"></table>

<div class="${FORM_CLASS}" style="display:none;padding:20px 25px 0 0">
  <div class="layui-form-item">
    <label class="layui-form-label">应用名</label>
    <div class="layui-input-block">
      <input type="text" id="${NAME_INPUT_ID}" class="layui-input" maxlength="50"
             autocomplete="off" placeholder="给这套凭据起的名字，例如：对接方甲">
    </div>
  </div>
${picker}
  <div class="layui-form-item">
    <div class="layui-input-block">
      <button type="button" class="layui-btn" id="${CREATE_SAVE_BUTTON_ID}">创建</button>
    </div>
  </div>
  <div class="layui-form-mid layui-word-aux" style="padding:0 0 12px 110px">
    创建成功后系统会生成 AppKey 与 AppSecret（<b>密钥在列表里随时可以查看与复制</b>）。
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
        <input type="text" id="${CALLBACK_URL_INPUT_ID}" class="layui-input"
               maxlength="${CALLBACK_URL_MAX_LENGTH}"
               autocomplete="off" placeholder="https://接收推送的公网地址（留空 = 不推送）">
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
</div>${docPanelHtml()}`
}

// ⚠️ `findPanel` / `reopenGuard` 是**从 `open-apps.ts` import 的同一个实现**（见文件头：
//    弹层借节点 / 收节点这件事与"哪一份文档"无关）⇒ 本文件**不再各写一份**
//    （同族纪律：同一个判据两处实现必然漂移）。

/** 读弹层里某个节点的值（节点不在文档里时回空串）。 */
function fieldOf(panel: HTMLElement, id: string): string {
  const el = panel.querySelector<HTMLInputElement | HTMLSelectElement>(`#${id}`)
  return el?.value ?? ''
}

export const __testables = {
  actionCell,
  buildHtml,
  takerApiDoc,
  takerApiDocText,
  callbackSaveReport,
  callbackUrlHint,
  candidateLabel,
  candidateOptionsHtml,
  copyCredentialText,
  copyTextToClipboard,
  // ⚠️ 台账渲染那一份是**上家侧同一个实现**（见文件头那段取舍）：这里把它再导出一遍，
  //    页测试才能对「只有 `failed` 行有重投按钮」这条判据做**纯函数**级断言
  //    （⛔ 不是本页另写一份渲染再自己测自己）。
  deliveriesHtml,
  decorateRows,
  lastUsedText,
  pickerHintText,
  secretCell,
  stateActionLabel,
  stateCell,
  timeText,
}

export default {
  mount: async (root: HTMLElement, _ctx) => {
    document.title = TITLE
    // ⚠️ 顺序是硬要求（`AGENTS.md` §5-G 陷阱 ③）：先挂样式（`bootstrapIsland`），再塞页面标记，
    //    最后 `initIslandElements`（它要按标记里的 `lay-*` 初始化）。
    const layui: LayuiNamespace = await bootstrapIsland(['table', 'layer', 'form'])
    const table = layui.table as LayuiModule
    const layer = layui.layer as LayuiModule
    // ⭐ `form` 是**回调开关**要的：`lay-skin="switch"` 的裸 input 必须经 `form.render`
    //    才会变成开关（见 `CALLBACK_FORM_FILTER` 的注释）。
    //    ⚠️ 取不到时**降级成不渲染**（不是崩）：测试替身与"layui 半加载"那种情形下，
    //    页面照样能用（原生复选框），只是样式不是 layui 那套。
    const form = (layui as unknown as { form?: LayuiModule }).form
    // `LayuiNamespace.$` 是 `unknown` ⇒ 局部收窄成 `layer.open` 要的那一形。
    const $ = (layui as unknown as { $: (el: HTMLElement) => unknown }).$
    const msg = (text: string): void => islandMsg(layer as unknown as Record<string, unknown>, text)

    /** 重渲回调那一块（`lay-skin="switch"` 的开关）。⚠️ 带 `lay-filter`，⛔ 不用无参全局渲染。 */
    function renderCallbackForm(): void {
      const render = (form as { render?: (type?: string, filter?: string) => void } | undefined)
        ?.render
      if (typeof render === 'function') render.call(form, 'checkbox', CALLBACK_FORM_FILTER)
    }

    // ⚠️ **本页自己的门禁**（与路由那道门同源，⛔ 不是第二套判据）：
    //    iframe 可被深链（`page.html?page=taker-apps`）打开，那一刻路由的
    //    `permissions` 不生效（先例与理由：`pages/system-roleauthority-edit.ts` 的同名自检）。
    //    ⚠️ 注入标记必须排在门禁**之后**：`layui-island/page.ts` 丢掉 `mount` 的返回值，
    //    拒绝分支返回的 cleanup 永远不会被调用 ⇒ 先注入就会让被拒的深链看到整个页面壳。
    const me = await fetchMe()
    if (!canNode(me, TAKER_APP_NODE)) {
      msg(NO_ACCESS_MESSAGE)
      return () => {}
    }
    // ⭐ 身份那一半：与后端 `_is_system_admin` **同一个谓词**（理由见文件头）。
    const isSystemAdmin = canNode(me, SYSTEM_ADMIN_NODE)

    root.innerHTML = buildHtml(isSystemAdmin)
    initIslandElements(layui)

    /** 列表当前数据（行事件要用它拿行）。 */
    let rows: TakerAppTableRow[] = []
    /**
     * 「接入文档」弹层里的内容**已经上屏过一次**没有（⛔ 每次打开不重跑 `marked`）。
     * ⚠️ 只在本页这一次挂载里有效；`takerApiDoc()` 自己那份缓存是跨挂载的（文档是静态的）。
     */
    let docInjected = false
    /**
     * 回调面板此刻对着哪个应用（`null` = 没开）——也用来丢弃**迟到**的台账响应
     * （面板可以关掉再开另一个应用）。
     */
    let callbackApp: TakerAppRow | null = null
    /** 在途闸门（逐动作一个；防连点写两次）。 */
    let savingCreate = false
    let toggling = false
    let rotating = false
    let savingCallback = false
    let retrying = false

    const reloadData = (data: unknown): void =>
      (table.reloadData as (id: string, o: unknown) => void)(TABLE_ID, { data })

    ;(table.render as (o: unknown) => void)({
      elem: TABLE_ELEM,
      id: TABLE_ID,
      // ⚠️ 先 render 一次（哪怕空数据）：实例是在 render 时注册的；数据用 `reloadData` 灌。
      data: [] as TakerAppRow[],
      toolbar: `#${TOOLBAR_ID}`,
      defaultToolbar: [],
      cols: [
        [
          { field: 'name', title: '应用名', minWidth: 150 },
          { field: 'app_key', title: 'AppKey', width: 180 },
          // ⭐ 明文进列表（与上家侧同口径）：⛔ 别把它改成打码 / 折叠。
          { title: 'AppSecret', minWidth: 240, templet: (d: TakerAppRow) => secretCell(d) },
          { field: 'taker_name', title: '绑定打手', width: 140 },
          { title: '状态', width: 90, templet: (d: TakerAppRow) => stateCell(d) },
          {
            title: '最近使用',
            width: 170,
            templet: (d: TakerAppRow) => lastUsedText(d.last_used_at),
          },
          // ⚠️ 函数 `templet`（不是 `toolbar: '#id'`）：状态按钮的文案**逐行不同**。
          {
            title: '操作',
            // ⚠️ 四颗按钮（回调与推送 / 复制密钥 / 停用启用 / 轮换）⇒ 宽度 300
            //    （挤不下会折行、看着像坏了；与上家侧那一格同款）。
            width: 300,
            align: 'center',
            templet: (d: TakerAppTableRow) => actionCell(d),
          },
        ],
      ],
      page: false,
      text: { none: '还没有凭据；点上面的「新增打手凭据」建一套' },
    })

    async function reload(): Promise<void> {
      // ⚠️ **装配只在这一个出口**：服务端那份行数据先过 `decorateRows()` 再灌进表里
      //    ⇒ 模板里不会出现"要用哪句话"的第二个判据。
      rows = decorateRows(await listTakerApps())
      reloadData(rows)
    }

    /** 面板里某个节点（每次都现查：节点会被 `layer.open` 搬走）。 */
    function panelPart(panel: HTMLElement, id: string): HTMLElement {
      const el = panel.querySelector<HTMLElement>(`#${id}`)
      if (!el) throw new Error(`新建面板里没有 #${id}`)
      return el
    }

    /**
     * 管理员那一副面孔：把候选账号灌进选择器。
     *
     * ⚠️ **失败要如实说**（写进 `PICKER_HINT_ID`），⛔ 不许渲染成"本租户没有账号"：
     * "一条都没有"与"没取到"长得一样，用户会去建账号而不是来找权限问题（本仓反复堵的假信号）。
     */
    async function loadCandidates(panel: HTMLElement): Promise<void> {
      const select = panelPart(panel, TAKER_SELECT_ID) as HTMLSelectElement
      const hint = panelPart(panel, PICKER_HINT_ID)
      hint.textContent = ''
      try {
        const page = await listTakerCandidates()
        select.innerHTML = candidateOptionsHtml(page.items)
        hint.textContent = pickerHintText(page.items.length, page.total)
      } catch (e) {
        select.innerHTML = candidateOptionsHtml([])
        hint.textContent =
          MESSAGES.pickerFailed + (e instanceof Error ? e.message : MESSAGES.failed)
      }
    }

    /**
     * 「回调与推送」面板里取一个节点（现查：`layer.open` 会把节点搬走）。
     *
     * ⚠️ 与上面那个 `panelPart` 分开，只为了让报错文案说清是**哪个**面板少了节点。
     */
    function callbackPart(panel: HTMLElement, id: string): HTMLElement {
      const el = panel.querySelector<HTMLElement>(`#${id}`)
      if (!el) throw new Error(`回调面板里没有 #${id}`)
      return el
    }

    /** 按输入框当前的值刷新那句即时提示（空值 / 合法 https ⇒ 清空）。 */
    function refreshCallbackHint(panel: HTMLElement): void {
      callbackPart(panel, CALLBACK_HINT_ID).textContent = callbackUrlHint(
        fieldOf(panel, CALLBACK_URL_INPUT_ID),
      )
    }

    /** 收起 / 展开「查看推送台账」（折叠面板的唯一出口）。 */
    function setDeliveriesOpen(panel: HTMLElement, open: boolean): void {
      callbackPart(panel, DELIVERIES_BODY_ID).style.display = open ? '' : 'none'
      callbackPart(panel, DELIVERIES_TOGGLE_ID).textContent = open
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
     * ⚠️ 渲染走**上家侧同一个实现**（`deliveriesHtml`，见文件头那段取舍）：
     * 台账 13 键契约两族逐字同集，而"只有 `failed` 行有重投按钮"是**同一条**后端判据。
     */
    async function loadDeliveries(pk: number, panel: HTMLElement): Promise<void> {
      const body = callbackPart(panel, DELIVERIES_BODY_ID)
      const warn = callbackPart(panel, DELIVERIES_WARN_ID)
      warn.textContent = ''
      try {
        const page = await listTakerDeliveries(pk, {
          page: 1,
          page_size: TAKER_DELIVERIES_PAGE_SIZE,
        })
        if (callbackApp === null || callbackApp.id !== pk) return
        body.innerHTML = deliveriesHtml(page.items, page.total)
      } catch (e) {
        if (callbackApp === null || callbackApp.id !== pk) return
        body.innerHTML = ''
        warn.textContent = e instanceof Error ? e.message : MESSAGES.failed
      }
    }

    /** 重新拉一次推送台账（重投成功之后用）。 */
    async function refreshDeliveries(pk: number): Promise<void> {
      const panel = findPanel(CALLBACK_SELECTOR)
      if (!panel) return
      setDeliveriesOpen(panel, true)
      await loadDeliveries(pk, panel)
    }

    /**
     * 打开「回调与推送」面板（回调地址 / 开关 / 推送台账）。
     *
     * ⚠️ **行载荷里没有回调那两个键就不要打开**：显示成"空的"会让用户一保存就把真实配置
     * 覆盖掉 —— 那是**最坏的那条静默路径**（与上家侧同款）。
     * ⭐ **这里不取台账**（与上家侧**有意不同**）：spec 判据 ③ 要求「打开页面时不拉台账；
     * 点开折叠区才拉」⇒ 取数在下面那个 `click` 里（`loadDeliveries` 的唯一入口之一）。
     * 理由：面板开着不等于用户要看台账，而"关着还在刷"会让每次点面板都白打一条接口
     * （还要处理迟到响应）——懒加载把这件事收成一个显式动作。
     */
    function openCallback(row: TakerAppRow): void {
      const panel = findPanel(CALLBACK_SELECTOR)
      if (!panel) return
      if (!('callback_url' in row) || !('callback_state' in row)) {
        msg(MESSAGES.callbackFieldsMissing)
        return
      }
      callbackPart(panel, CALLBACK_APP_NAME_ID).textContent = row.name
      panel.querySelector<HTMLInputElement>(`#${CALLBACK_URL_INPUT_ID}`)!.value = row.callback_url
      // ⚠️ 判据用机器值 `=== 'enabled'`（⛔ 不是真值、也不是中文名）。
      panel.querySelector<HTMLInputElement>(`#${CALLBACK_STATE_SWITCH_ID}`)!.checked =
        row.callback_state === 'enabled'
      refreshCallbackHint(panel)
      // 推送台账：清掉上一个应用的痕迹，并**回到收起态**（数据等点开折叠时再取）。
      callbackPart(panel, DELIVERIES_BODY_ID).innerHTML = ''
      callbackPart(panel, DELIVERIES_WARN_ID).textContent = ''
      setDeliveriesOpen(panel, false)

      callbackApp = row
      ;(layer.open as (o: unknown) => number)({
        type: 1,
        // ⚠️ 应用名是租户自建的自由文本，`layer.open` 的 title 按 HTML 渲染 ⇒ 过 `escapeHtml`。
        title: `回调与推送：${escapeHtml(row.name)}`,
        // ⚠️ 尺寸沿用本页既有面板口径（`90%` × `90%`，与「接入文档」那一份、以及上家侧
        //    「回调与推送」那一份同一对值）——⛔ 不自己拍一个像素值（`AGENTS.md` §5-D）。
        //    面板里有一个会变长的台账列表（下面那个 `max-height` 容器）⇒ 大一点是对的。
        area: ['90%', '90%'],
        content: $(panel),
        success: () => renderCallbackForm(),
        end: () => {
          callbackApp = null
          reopenGuard(panel, root)
        },
      })
    }

    /**
     * 保存回调设置（`PATCH`，两个键一起发）。
     *
     * ⚠️ **没有前端校验门**：地址是否合法**由后端判**（仅 https + 必须公网 + SSRF），
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
      const state: TakerAppCallbackState = panel.querySelector<HTMLInputElement>(
        `#${CALLBACK_STATE_SWITCH_ID}`,
      )!.checked
        ? 'enabled'
        : 'disabled'
      const url = fieldOf(panel, CALLBACK_URL_INPUT_ID).trim()
      savingCallback = true
      void updateTakerApp(app.id, { callback_url: url, callback_state: state })
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
     * 重投一条**失败**的推送（二次确认 → `POST .../deliveries/<id>/retry/` → 重拉台账）。
     *
     * ⚠️ **在途闸门只包住那次请求**（⛔ 不包住二次确认）：`layer.confirm` 只有在用户点
     * 「确定」时才走回调，把闸门压在它上面会让**点了取消之后**下一次点击静默无效
     * （= 本仓反复堵的"点了没反应"）。代价如实登记：连点两次会开两个确认框，
     * 第二个请求被后端按 `pending` 拒（回 `1000`）并**显示出来**——可见，不静默。
     * ⚠️ **失败必须显示后端那句中文**（例如「只有投递失败的推送记录可以重投（…）」）：
     * 那是后端在回答"为什么不行"，⛔ 前端不许自己编一句（spec 判据 3）。
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
          void retryTakerDelivery(app.id, deliveryPk)
            .then(() => refreshDeliveries(app.id))
            .then(() => {
              msg(MESSAGES.retryDone)
            })
            .catch((e: Error) => msg(e.message))
            .finally(() => {
              retrying = false
            })
        },
      )
    }

    /**
     * 打开「接入文档」全屏弹层（目录 + 正文 + 两颗动作按钮）。
     *
     * ⚠️ 内容是**构建期固化**的那份 `docs/TAKER-API.md`（`?raw`）⇒ 不取后端、不读磁盘。
     * ⚠️ 上屏**只做一次**（`docInjected`）：`marked` 渲染一次、`innerHTML` 写一次，
     * 之后反复开关弹层只是把它搬来搬去（`layer.open` 会移动节点）。
     * ⚠️ 尺寸沿用本页既有面板口径（`90%` × `90%`，与 `open-apps.ts` 那一份同一对值）——
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
        const doc = takerApiDoc()
        // ⚠️ 传本页自己那一套 class 名：`docTocHtml` 是**共用实现**，但两个页面的样式
        //    必须各归各的（共用一个 class 名会让 `open-api` 页的 CSS 命中本页的目录）。
        toc.innerHTML = docTocHtml(doc.headings, {
          item: DOC_TOC_ITEM_CLASS,
          list: DOC_TOC_LIST_CLASS,
        })
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

    /** 打开「新增凭据」弹层。**先清空输入、再 open**（`layer.open` 会把节点搬出原位）。 */
    function openCreate(): void {
      const panel = findPanel(FORM_SELECTOR)
      if (!panel) return
      panel.querySelector<HTMLInputElement>(`#${NAME_INPUT_ID}`)!.value = ''
      // ⚠️ 打手那一副面孔里**没有**选择器（标记就没渲染）⇒ 这里按 `isSystemAdmin` 分支，
      //    ⛔ 不去查一个不存在的节点（`panelPart` 会抛）。
      if (isSystemAdmin) {
        panelPart(panel, PICKER_HINT_ID).textContent = ''
        void loadCandidates(panel)
      }
      ;(layer.open as (o: unknown) => number)({
        type: 1,
        title: isSystemAdmin ? '新增打手凭据（需指定绑定的打手账号）' : '新增打手凭据',
        area: isSystemAdmin ? ['560px', '340px'] : ['560px', '260px'],
        content: $(panel),
        end: () => reopenGuard(panel, root),
      })
    }

    /** 保存（创建）：打手那一支**不发** `taker`（后端锁死为他自己），管理员那支**必须发**。 */
    function submitCreate(panel: HTMLElement): void {
      if (savingCreate) return
      const name = fieldOf(panel, NAME_INPUT_ID).trim()
      if (name === '') return msg(MESSAGES.noName)
      let taker: number | undefined
      if (isSystemAdmin) {
        const raw = fieldOf(panel, TAKER_SELECT_ID)
        // ⚠️ 判据用 `=== ''`（"没选"），⛔ 不用真值：`0` 在这里不是合法账号，但用真值判断
        //    会把"没选"与"选了一个空字符串"混在一起——文案要说清是"没选"。
        if (raw === '') return msg(MESSAGES.noTaker)
        taker = Number(raw)
      }
      savingCreate = true
      void createTakerApp(taker === undefined ? { name } : { name, taker })
        .then(() => {
          ;(layer.closeAll as (t?: string) => void)('page')
          return reload().then(() =>
            msg(
              isSystemAdmin
                ? '已创建：密钥就在列表里，随时可以查看与复制'
                : '已创建，并绑定在你名下：密钥就在列表里，随时可以查看与复制',
            ),
          )
        })
        .catch((e: Error) => msg(e.message))
        .finally(() => {
          savingCreate = false
        })
    }

    /** 停用 / 启用（二次确认里说清后果：停用 = 第三方**当场**调不通）。
     *  ⚠️ `layer.confirm` 的 content 按 **HTML** 渲染（E-0303）：应用名是打手自建的自由文本 ⇒
     *  逐插值过 `escapeHtml`（F-01）；`tail` 里的 `<b>` 是**有意的字面标记**，⛔ 整串转义会毁掉它
     *  ——只转义插值、保留常量文案里的字面 HTML。 */
    function confirmToggle(row: TakerAppTableRow): void {
      if (toggling) return
      const next = row.state === 'enabled' ? 'disabled' : 'enabled'
      const action = next === 'disabled' ? '停用' : '启用'
      const tail =
        next === 'disabled'
          ? '停用后第三方<b>当场</b>调不通（秒级生效），但这套凭据与它的密钥都还在。'
          : '启用后第三方可以立刻继续用它调用接口。'
      ;(layer.confirm as (t: string, o: unknown, ok: () => void) => void)(
        `确定要${action}「${escapeHtml(row.name)}」吗？${tail}`,
        { title: `${action}凭据` },
        () => {
          toggling = true
          void updateTakerApp(row.id, { state: next })
            .then(() => {
              ;(layer.closeAll as (t?: string) => void)('dialog')
              return reload().then(() => msg(`已${action}「${escapeHtml(row.name)}」`))
            })
            .catch((e: Error) => msg(e.message))
            .finally(() => {
              toggling = false
            })
        },
      )
    }

    /** 轮换密钥（二次确认里必须写出「旧密钥立刻作废、第三方当场调不通」）。 */
    function confirmRotate(row: TakerAppTableRow): void {
      if (rotating) return
      ;(layer.confirm as (t: string, o: unknown, ok: () => void) => void)(
        `确定要轮换「${escapeHtml(row.name)}」的密钥吗？旧密钥<b>立刻作废</b>（没有宽限期），` +
          '正在用它的第三方<b>当场调不通</b>，必须马上换成新密钥。',
        { title: '轮换密钥' },
        () => {
          rotating = true
          void rotateTakerAppSecret(row.id)
            .then(() => {
              ;(layer.closeAll as (t?: string) => void)('dialog')
              return reload().then(() => msg(`已轮换「${escapeHtml(row.name)}」的密钥（新密钥在列表里）`))
            })
            .catch((e: Error) => msg(e.message))
            .finally(() => {
              rotating = false
            })
        },
      )
    }

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
        const row = (obj as { data: TakerAppTableRow }).data
        const ev = (obj as { event: string }).event
        // ⭐ `callback` 是同步打开的（台账取数在折叠区那一下，见 `openCallback` 的注释）。
        if (ev === 'callback') return openCallback(row)
        if (ev === 'copysecret') {
          // ⚠️ 复制的内容**从行数据取**（⛔ 不从 DOM 读：那张格子按设计不含密钥）。
          copyTextToClipboard(copyCredentialText(row), (ok) =>
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
      ?.addEventListener('click', () => submitCreate(createPanel))

    // ── 「回调与推送」面板里的四处接线（即时提示 / 保存 / 折叠 / 重投委托）─────────
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
        const body = callbackPart(callbackPanel, DELIVERIES_BODY_ID)
        const open = body.style.display === 'none'
        setDeliveriesOpen(callbackPanel, open)
        // ⭐ **台账的唯一取数入口之一**：只在**展开**的那一下拉（spec 判据 ③ 的懒加载）。
        //    ⚠️ `callbackApp` 在这里现读（⛔ 不是打开面板那一刻的快照）：面板可以换应用。
        //    收起时不打任何请求（⛔ 不是"关着还在刷"）。
        if (open && callbackApp !== null) void loadDeliveries(callbackApp.id, callbackPanel)
      })
    // 「重投」按钮是**列表里的行**（每次刷新重建）⇒ 在容器上挂一个委托（容器节点自己随
    // `layer.open` 一起搬，所以搬动不会断它）。
    callbackPanel
      ?.querySelector<HTMLElement>(`#${DELIVERIES_BODY_ID}`)
      ?.addEventListener('click', (ev) => {
        const target = ev.target as HTMLElement | null
        const button = target?.closest<HTMLElement>(`[${DELIVERY_RETRY_ATTR}]`)
        if (!button) return
        // ⚠️ 那个值就是**页面载荷里那一行的 `id`**（渲染时由 `deliveriesHtml` 写进属性）
        //    ⇒ 这里⛔ 不从别处拼（更不从数据库对象拼）。
        const deliveryPk = Number(button.getAttribute(DELIVERY_RETRY_ATTR))
        if (!Number.isInteger(deliveryPk) || deliveryPk <= 0) return
        confirmRetry(deliveryPk)
      })

    // ── 「接入文档」弹层里的三处接线（目录委托 + 两颗动作按钮）─────────────────
    // ⚠️ 与上面那几处同一个做法：监听**挂在节点自己身上**（`layer.open` 只搬节点、不断节点
    //    自身的监听）；⛔ 别改成 `document` 上的委托（每次 mount 会叠一个闭包，一次点击触发多次）。
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
        copyTextToClipboard(takerApiDocText(), (ok) =>
          msg(ok ? DOC_COPY_DONE : DOC_COPY_FAILED),
        )
      })
    docPanel
      ?.querySelector<HTMLButtonElement>(`#${DOC_DOWNLOAD_BUTTON_ID}`)
      ?.addEventListener('click', () => {
        // ⚠️ `false` = 这个环境存不了文件 ⇒ **说人话并给出路**（⛔ 不是点了没反应）。
        if (!downloadTextFile(DOC_DOWNLOAD_FILENAME, takerApiDocText())) {
          msg(DOC_DOWNLOAD_FAILED)
        }
      })

    await reload()

    return () => {
      // 本页没有定时器 / 全局监听（陷阱 ④）⇒ 清理只把面板收回原位，方便重新挂载。
      if (createPanel) reopenGuard(createPanel, root)
      if (callbackPanel) reopenGuard(callbackPanel, root)
      if (docPanel) reopenGuard(docPanel, root)
    }
  },
} satisfies IslandPage
