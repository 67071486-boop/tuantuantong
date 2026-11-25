/**
 * 【绑定上家系统管理】页 —— **自挂载 island 页 + 接真后端**（对标 `/System/homesystemsetting`，
 * 对标 `<title>绑定上家系统管理</title>`）。
 *
 * ## 为什么不走 `artifactPage`（spec §11.1 / D-0149 同款）
 *
 * 共享渲染器 `src/layui/artifactPage.ts` 的契约**逐字**写着「表格恒空、不发任何写请求」
 * （`mountArtifact` 把 `data: []` 写死、并给整页挂了「点了提示尚未开放」的捕获阶段总闸），
 * 而本仓有**几十个**页文件在走它。本页要接真接口、发 POST/DELETE ⇒ 给它开参数等于改**共享行为**，
 * 代价落在那些页身上。所以本页照 D-0149（「我的资料」线）与
 * `pages/customer-customerlist.ts` 的先例**自己挂载**。
 *
 * ⚠️ 代价（spec §14-R11 已登记）：本页脱离共享护栏 ⇒ **本页自己的页测试成为唯一防线**；
 * `parity` 只看骨架，「绿」不构成本页接对了的证据。
 *
 * ## 挂载顺序（`AGENTS.md` §5-G 三条陷阱，一条都不能换）
 *
 * 1. `await bootstrapIsland(['form','table','layer'])` —— **先挂 layui.css**（运行期插 `<link>`，
 *    反了会 FOUC）。⚠️ 模式页写的是 `['table']`；本页要 `form.val`/`form.render`/`layer.open`
 *    ⇒ 显式带上 `form` 与 `layer`（读码 vendored `layui.js`：`table` 模块的依赖里有 `form`，
 *    但 `layer` 不在，靠 `['table']` 拿不到）。
 * 2. **再**塞证据件标记（`root.innerHTML = html`）。
 * 3. `initIslandElements(layui)` —— 必须**在塞标记之后**。
 * 4. ⭐ 运行期动作（密码框改 `type` / **删掉弹窗里那条「注意」** / **注入弹窗新项** / 注入行内「编辑」），
 *    见 mount 里那段「运行期」注释。⚠️ 都**要在快照之前**做完；「删注意」与「插系统类型」
 *    **谁先谁后无所谓**——`afterbegin` 本就把系统类型插到那条 `blockquote` **之前**，终态 DOM 一样。
 *    ⛔ 承重的是「那条注意必须被删掉」本身，⛔ 不是两步的相对顺序（也别为它补用例：只会是恒真断言）。
 * 5. `table.render({elem, url, parseData, …})`。
 *
 * ## 证据（⛔ 一个视觉值都没改）
 *
 * 页内 `<style>`、body 标记、`cols`、弹层参数与全部文案逐字取本页证据件
 * `public/replica/system-homesystemsetting.html`（对标原件在 `.tools/recon/`，不进 git）。
 * ⛔ `height:'full'` **照抄原文别「修正」**：官方 layui 只认 `full-<n>`，`'full'` 走到
 * `fullSize()` 里 `parseFloat('full')` = NaN、高度回落成内容自适应——这正是对标的行为。
 * ⚠️ `defaultToolbar` 在同一个 render 对象里写两次时**后写的生效** ⇒ `['filter']`。
 *
 * ## 与对标不同（每条给理由；⛔ 不是缺陷，也不是「尚未开放」）
 *
 * | # | 对标怎样 | 我们怎样 | 依据 |
 * |---|---|---|---|
 * | 1 | `homePwd` 的 input 是 `type="text"`（**明文显示密码**） | 运行期改成 `type="password"` | spec §11.3（对标自己的缺陷不复刻）。⛔ 证据件本身**不动**（它是 parity 的对拍物），只在注入后改这一个属性 |
 * | 2 | 说明里承诺「你这边传的截图，也将自动同步到上家系统」；弹窗里另有一条「注意：只支持同为鸡蛋铺的系统绑定，其它系统暂不支持」 | ⭐ **两条「注意」都删了**：页首那条「本阶段暂不支持截图同步」的补充说明**不再追加**；弹窗里那条在**运行期** `.remove()` | ⭐ 业务方裁定两条都删。⚠️ 页首证据件原文那句承诺**照旧保留**（⛔ 证据件一个字不动）；截图同步还没实现 ⇒ 那句话当前还不成立，按「正在进行中」登记 |
 * | 3 | 跨域 `GET /SystemAjax/GetMyBindHomeSystemList?` | layui 的 `url` 打 `/api/business/upstream-bindings/`（**统一列表**：团团通 + 鸡蛋铺混排），`parseData` 解统一信封 | spec §11.1；信封 / CSRF / 错误分类都在 `api/` 一处 |
 * | 4 | 列表恒空（旧实现走 `artifactPage`） | `parseData` 里对 `items` 逐个 `toBindingRow` | 后端给的是 snake_case（两类行**同一套 13 键**，含大厅两格），列 `field` 是对标名（见 `toBindingRow`） |
 * | 5 | 操作列挂 `toolbar:'#baroperating'` | **照挂**（旧 `artifactPage` 版本漏了它，因为空表下看不出来） | 漏了它行内「删除绑定」永远不渲染 ⇒ DELETE 这条链路点了没反应（静默） |
 * | 6 | `var re` 住在 `type:2` 的独立 iframe 文档里（重开 = 新文档 = 自动复位） | 同文档弹层 ⇒ **每次开弹窗显式复位** | spec §11.4 / E-0079：不复位会实体化成「新增成功后，再新增点『确定』一笔请求都不发、也没提示」 |
 * | 7 | 对标**没有**失败提示（它那边推送失败在页面上不可见） | 表格上方按需点亮一块**红字**：本租户有绑定最近推送失败时，显示失败个数、最后失败时刻、连续失败次数与原因 | ⭐ **裁决 R20**：spec §7.6「失败在页面上看得见」是**承诺**，而后端早就暴露了 `last_error` / `last_error_at` / `consecutive_failures` ⇒ 旧页面一处都没渲染，那句承诺就是假的。与差异表 #2 同类：为**真实性**而加，⛔ 不是复刻；视觉值自拟（对标没有这一块）。⛔ 同时**不做**"连续失败到阈值自动吊销绑定"（R20 排到 P2）。⚠️ 收口后判据改读**统一列表**的行：鸡蛋铺那一列 `consecutive_failures` 恒 `0`（模型没有这一列）⇒ 判据 `> 0` 不会把鸡蛋铺行算成失败 |
 * | 8 | 对标**没有**「最近一次收到同步」这条时间线 | 表格上方（失败提示之后）按取数结果显示一块**中性**信息：每个上家一行 `{名称}：{最近一次收到同步的时间}`，从没收到过则写「尚未收到过同步」（没有绑定则收起） | ⭐ **裁决 A19** / spec §16.7 第 2 条：下家页面**只**如实显示这条时间，⛔ 不心跳、⛔ 不按时间变红、⛔ 不通知"上家已停推"——低单量工作室不许看到假报警。与差异表 #7 同类：为**真实性**而加、⛔ 不是复刻；视觉值自拟（对标没有这一块）。⚠️ 与 #7 别混着读：#7 是"对面推来的东西我处理不了"（本页绑定的健康列），#8 只是"我上次收到是什么时候"。⚠️ 这块**也含鸡蛋铺行**；名称取统一行的 `system_name`（缺了回 `—`，与表格那一列同一口径） |
 * | 9 | 对标表格下方**没有任何绑定表单** | ⭐ 本页也**没有**：表格下方不追加任何表单，页面上只剩工具栏那颗「绑定一个新的上家系统」弹窗一个新增入口（重复入口已收口） | 那一块承载的「测试连接 / 立即同步 / 健康状态」一并不要；改配置 / 停用改由弹窗覆盖式 PUT 承担 |
 * | 10 | 弹窗只有「上家系统名称 / 上家系统网址 / 登录上家系统账号 / 登录上家系统密码」四格（且只支持同为鸡蛋铺的系统） | ⭐ 弹窗**新增**「系统类型」下拉（`鸡蛋铺系统` / `团团通系统`，默认 `鸡蛋铺系统`）+ `鸡蛋铺系统` 分支的两格：**发单价百分比**与**启用**开关；`团团通系统` 分支的**编辑**弹窗另有「同步上家抢单大厅」开关与「大厅价格百分比」两格（默认关 / 100，新增弹窗不画——POST 只收四格，画出来就是假话） | 有意偏离、⛔ 不是复刻；视觉值仍**逐字**取证据件那套（`width: 135px; padding: 8px 5px;` / `margin-left: 135px;` / `layui-input layui-input-shadow`），⛔ 证据件一个字没动。⚠️ **权限后果**（业务方明示接受、⛔ 不是缺陷）：本页要 `system.bind_upstream_system` 节点才打得开 ⇒ **没有这个节点的普通客服不再能自助绑上家鸡蛋铺账号** |
 *
 * ## 写入口径：鸡蛋铺绑定的读 / 写 / 删
 *
 * 鸡蛋铺那一份绑定是**租户多行**资源（⛔ 不是「一人一份的单行资源」）：
 *
 * - **读**：进本页那张**统一列表**（`GET /api/business/upstream-bindings/`，团团通 + 鸡蛋铺混排，
 *   每行 13 键）——不再单独 GET 本人那一行；
 * - **写**：走弹窗「鸡蛋铺系统」分支的**行级**端点——「新增」= `POST /api/business/upstream-bindings/jidanpu/`、
 *   行内「编辑」= `PUT …/jidanpu/<pk>/`（回填先 `GET …/<pk>/` 读一次）。按用户的 member PUT 为兼容
 *   保留、页面**弃用**——旧写法会把同账号的第二条绑定**静默覆盖**掉（E-0255 / D-0256）；
 * - **删**：行内「删除绑定」按 `system_type` 分流 ⇒ 鸡蛋铺走
 *   `DELETE /api/business/upstream-bindings/jidanpu/<pk>/`（**真删**，链路级联）。
 *
 * ⚠️ **与差异表 #2 那两条「注意」不是一回事**：那两条已被业务方裁定**删掉**（见 #2），
 * 运行期删那条证据件自带的「注意」**保留**。
 *
 * ## 列上的偏离（当前口径）
 *
 * 1. **`UserName`（绑定操作人）**：读后端的**显示名** `bound_by_name`（⛔ 不读主键——那会显示 `3`）。
 * 2. **`BindTimeStr`（绑定时间）**：走本仓复刻表的既有口径 `formatDateTime`
 *    （`pages/emp-emplist.ts`，那一页的行与它的 spec 都用它）。⚠️ 对标对这个值的形状**没有证据**
 *    （对标自己的列表是空的）⇒ 这是有意的偏离、不是对拍产物；缺值一律 `'—'`
 *    （与 `emp-emplist` 口径一致，⛔ 不是空串、更不是 `Invalid Date`）。
 * 3. **`HomeHost`（上家系统域名）**：逐字搬对标的 `templet`（域名的可点链接），并把取值处**转义**
 *    （⛔ 不复制对标那句原样拼接的注入面）。
 * 4. ⭐ **`SystemType`（系统类型）—— 我方新增的第一列**：对标 `cols` 里**没有**这一列
 *    （页测试拿证据件原文钉住这一点）。两类上家混在一张表里，不标类型就分不清哪行属于哪条产品线；
 *    `jidanpu` / `tuantuantong` 是机器值，⛔ **不上屏**（`systemTypeLabel` 换成 `鸡蛋铺系统` / `团团通系统`）。
 *
 * ## PII / 凭据（红线）
 *
 * - 统一列表端点**永不回显** `password` / `password_set` / `token_in_hash` / `token_out`；
 *   本页也不渲染它们（`UpstreamBindingRow` 里根本没有这些键）。
 * - 密码只在两条写请求的 body 里出现一次（团团通 `POST` 握手 / 鸡蛋铺弹窗 `PUT`）；
 *   页测试夹具全是**合成值**（保留域名 `*.test.invalid`）。
 * - ⚠️ **有意保留一处**：鸡蛋铺行的**上家子账号**（登录名）会出现在统一列表里（`account` 那一列）
 *   ——⛔ 不要顺手把它加进「第三方平台 → 鸡蛋铺系统同步」的只读总览。
 */
import {
  createPeerBinding,
  deletePeerBinding,
  getPeerBinding,
  updatePeerBindingHallSettings,
  type PeerBindingRow,
  type PeerHallSettings,
} from '@/api/peerBindings'
// ⭐（E-0255 / D-0256）鸡蛋铺那份绑定是**多行**资源：弹窗「新增」走
// `createUpstreamBindingRow`（行级 POST）、「编辑」走 `updateUpstreamBindingRow`（行级 PUT <pk>，
// 回填先 `getUpstreamBindingRow` 读一次）——⛔ 旧写法（`putUpstreamBinding` 打按用户的 member PUT）
// 会把同账号的第二条绑定**静默覆盖**掉（生产实锤 E-0255）；member 那两条端点为兼容保留、本页不再消费。
import {
  createUpstreamBindingRow,
  deleteUpstreamBinding,
  getUpstreamBindingRow,
  updateUpstreamBindingRow,
  type JidanpuUpstreamBindingPayload,
} from '@/api/jidanpuUpstream'
// ⭐ D-0261：弹窗「订单来源」下拉的数据源 = 既有订单来源字典 API（`fetchOrderSources`，
// 管理员先在「订单来源」页把「竹屿 / 叶枫」等项建好）。⛔ 不另开一条取数路径。
import { fetchOrderSources, type OrderSourceRow } from '@/api/orderSources'
// ⭐ 那张**统一列表**（团团通 + 鸡蛋铺混排，每行 13 键）的契约：URL 交给 layui 的 `url` 通道，
// 行类型给 `parseData` / `toBindingRow` 用（`api/upstreamBindings.ts`）。
import { UPSTREAM_BINDINGS_URL, type UpstreamBindingRow } from '@/api/upstreamBindings'
import { findPanel, loadConfigArtifact, openPanelLayer } from '@/layui-island/config-replica'
import {
  bootstrapIsland,
  initIslandElements,
  islandMsg,
  type IslandContext,
  type IslandPage,
} from '@/layui/island'
import type { LayuiModule } from '@/layui/loader'
// ⚠️ 「绑定时间」那一列用它，**不另写一份格式化**（同一事实只有一处实现）——它也正是
// `emp-emplist` 的行与它的 spec 用的那一份（见文件头第 2 条）。
import { formatDateTime } from './emp-emplist'

/** 证据件名（= island key = `pages/<key>.ts` 的文件名）。 */
export const ARTIFACT_KEY = 'system-homesystemsetting'
/** 对标页服务端渲染的 `<title>`。 */
export const TITLE = '绑定上家系统管理'
/** 对标 `table.render` 的 `elem`。 */
export const TABLE_ELEM = '#tableList'
/**
 * `table.reload` / `table.on` 要的是 **id**（不带 `#`），传 `#id` 是静默 no-op（E-0061 ③）
 * ⇒ 与 `TABLE_ELEM` **分成两个常量**，⛔ 不要用 `TABLE_ELEM.slice(1)` 现算。
 */
export const TABLE_ID = 'tableList'
/** 对标表级 `toolbar`（模板在证据件 `#toolbarDemo`）。 */
export const TOOLBAR_ELEM = '#toolbarDemo'
/** 对标行内模板（模板在证据件 `#baroperating`）。 */
export const ROW_TOOLBAR_ELEM = '#baroperating'
/**
 * 弹层容器的 **class 名**（证据件 `<div class="change" …>`）。
 *
 * ⚠️ **类名与选择器分成两个常量**（`AGENTS.md` §5-G 陷阱① / E-0061）：拼 `class=".change"`
 * 会造出一个永远匹配不到的选择器，而症状是**静默**的。
 */
export const PANEL_CLASS = 'change'
/** 同一个容器的 CSS 选择器（带点）。查询一律用它、拼 class 一律用 `PANEL_CLASS`。 */
export const PANEL_SELECTOR = '.change'

/** 站点基址（子路径部署时不是 `/`）。⛔ 不写死 `/`（否则子路径部署取到 404）。 */
const API_BASE = import.meta.env.BASE_URL || '/'

/**
 * 交给 layui `table.render` 的**完整列表 URL**（`url` 模式下取数由 layui 自己发）。
 * ⭐ 指向**统一列表**（团团通 + 鸡蛋铺混排）；
 * ⛔ 不再是 `peer-bindings/`（那条只覆盖团团通那一半）。
 */
export const BINDINGS_TABLE_URL = `${API_BASE.replace(/\/$/, '')}${UPSTREAM_BINDINGS_URL}`

/**
 * HTML 文本 / 属性转义。
 *
 * ⛔ `HomeHost` 来自**用户输入**（绑定弹窗里的「上家访问网址」；落库前只过 URL 闸门、不过 HTML
 * 闸门）⇒ 直接拼进 `templet` 的 HTML 串里就是一个注入面（本仓另一个页面**已经出过**这类
 * XSS 形状的缺陷）。属性值与文本节点共用这一份转义；`&` 必须**先**转，否则后面几步产出的
 * 实体会被二次转义。
 */
export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

/** 对标 `HomeHost` 那个链接的 `title`（逐字照对标）。 */
export const HOME_HOST_LINK_TITLE = '点击可以访问这个系统'

/**
 * 对标的 `HomeHost` 列 `templet`：域名 → 可点的新窗口链接。
 *
 * ⚠️ **逐字搬**对标那句 `<a href="…" target="_blank" style="" title="…">…</a>`——
 * 连它那个空 `style=""` 也照抄（无视觉影响，⛔ 别顺手删）。
 * 唯一与对标不同的是**取值处转义**：对标写的是原样拼接，本仓**不复制那个注入面**（见 `escapeHtml`）。
 */
export function homeHostCellTemplate(d: { HomeHost?: unknown }): string {
  const host = d.HomeHost == null ? '' : String(d.HomeHost)
  const safe = escapeHtml(host)
  return `<a href="${safe}" target="_blank" style="" title="${HOME_HOST_LINK_TITLE}">${safe}</a>`
}

/**
 * 表头：**逐条照抄**对标。字段名的大小写也照抄：`HomeCompany`/`HomeHost`/`HomeAcc`/`UserName`/`BindTimeStr`。
 *
 * ⚠️ 末列**必须**挂 `toolbar: ROW_TOOLBAR_ELEM`（差异表 #5）：漏了它行内「删除绑定」按钮
 * 永远不会渲染 ⇒ DELETE 那条链路整条死掉。除这一个**绑定**、`HomeHost` 列的 `templet`
 * 与第一列 `SystemType`（我方新增，见下）外，各列的宽度 / 对齐 / `sort` / `unresize` **一个值都没动**。
 */
export const COLS: readonly Record<string, unknown>[] = [
  // ⭐ **我方新增的第一列**：对标 `cols` 里**没有**「系统类型」这一列（页测试拿证据件原文
  // 钉住这一点）。两类上家**混在一张表**里，不标类型就分不清哪一行属于哪条产品线。
  // 视觉值自拟（对标没有可取值）：居中、宽 120；字段名沿用本表其余列的大写驼峰口径，⛔ 不是 snake_case。
  { field: 'SystemType', title: '系统类型', width: 120, align: 'center' },
  { field: 'HomeCompany', title: '上家系统名称', width: 150, align: 'left' },
  // ⭐ 对标这一列的 `templet` 会把域名渲染成 `<a>`。缺了它就是「纯文本、没链接色、点不动」，不是像素级复刻。
  { field: 'HomeHost', title: '上家系统域名', align: 'left', width: 200, templet: homeHostCellTemplate },
  { field: 'HomeAcc', title: '上家系统子账号', width: 200, align: 'left' },
  { field: 'UserName', title: '绑定操作人', width: 150, align: 'left' },
  // 对标 `sort:true`：表头右侧多一组排序手柄（CSS 三角形，不加文字、不改列宽）。
  { field: 'BindTimeStr', title: '绑定时间', width: 200, align: 'center', sort: true },
  // 对标 `fixed:''`（空串＝不固定 → 不传 `fixed`）、`unresize:true`、挂行内模板。
  // ⭐ 宽 160 → 200（E-0296）：注入「编辑」按钮后两颗 xs 按钮合计 ≈130px == 160px 列的内容区宽
  //   （减左右各 15px 内边距）——**零余量**；`.layui-table-cell` 是 `overflow:hidden + ellipsis`
  //   ⇒ 字体栈稍宽「删除绑定」整颗被裁出视野、原地只剩灰「…」，用户侧表现为「删除功能没有了」
  //   （生产实锤）。160 是对标**单按钮时代**的值；⛔ 不许改回去。
  { title: '操作', width: 200, unresize: true, toolbar: ROW_TOOLBAR_ELEM },
]

/**
 * 一行的数据形状：统一列表的接口字段（spread 保留）+ 对标 `cols[].field` 的名字。
 *
 * ⚠️ 接口行是**同一套 13 键**（`UpstreamBindingRow`）⇒ 两类行的字段名已经统一
 * （不再有 `peer_*`），这里只做「接口名 → 对标列名」的映射。
 */
export type BindingCellRow = UpstreamBindingRow & {
  /** ⭐ **我方新增列** ← `system_type`（经 `systemTypeLabel` 上屏，⛔ 不上屏 `jidanpu` 这种机器值）。 */
  SystemType: string
  /** 对标列 `HomeCompany` ← `system_name`（鸡蛋铺行缺名称时回 `—`，见 `upstreamSystemName`）。 */
  HomeCompany: string
  /** 对标列 `HomeHost` ← `base_url`。 */
  HomeHost: string
  /** 对标列 `HomeAcc` ← `account`。 */
  HomeAcc: string
  /** 对标列 `UserName` ← `bound_by_name`（绑定操作人的**显示名**，见文件头第 1 条）。 */
  UserName: string
  /** 对标列 `BindTimeStr` ← `bound_at`，经 `formatDateTime` 的 `Y-m-d H:i`（见文件头第 2 条）。 */
  BindTimeStr: string
  /** 行内「删除绑定」按它对出 `id`（对标读的就是这个名字）。 */
  BindHomeSystemLogId: number
}

/**
 * 上家系统在**屏幕上**的名字：接口的 `system_name`。
 *
 * ⚠️ 鸡蛋铺那一份在同步引擎跑过之前是**空串** ⇒ 缺了就回 `—`，⛔ **不拿地址编一个名字**。
 * ⚠️ 表格那一列与 A19 那块信息**共用这一份**（同一事实只判一次）。
 */
export function upstreamSystemName(row: Pick<UpstreamBindingRow, 'system_name'>): string {
  return row.system_name || '—'
}

/**
 * 接口行 → 表格行（照 `pages/system-pddshipsetting.ts::toBindingRow` 的先例）：
 * 补上对标 `cols[].field` 的键名，让 layui 能按列取到值。
 */
export function toBindingRow(row: UpstreamBindingRow): BindingCellRow {
  return {
    ...row,
    // ⭐ 我方新增列：`jidanpu` / `tuantuantong` 是机器值 ⇒ 一律经 `systemTypeLabel` 换中文。
    SystemType: systemTypeLabel(row.system_type),
    HomeCompany: upstreamSystemName(row),
    HomeHost: row.base_url,
    HomeAcc: row.account,
    // 显示名（后端 `bound_by_name`）。⛔ 不要读主键——旧口径 `bound_by` 显示出来是 `3`。
    UserName: row.bound_by_name,
    // `bound_at` 经后端是**非空**，但取不到值时回 `'—'`（`emp-emplist` 对缺失时间戳的口径），
    // ⛔ 不回空串、也不把 raw ISO 原样漏到单元格里。
    BindTimeStr: row.bound_at ? formatDateTime(row.bound_at) : '—',
    BindHomeSystemLogId: row.id,
  }
}

/** 四条校验文案：**逐字**照对标（⛔ 不改标点、不补字段名）。 */
export const VALIDATION_MESSAGES = {
  name: '请输入这个上家名称（可自定义名称）',
  url: '请输入这个上家的访问网址，例如http://d.jidanpu.com',
  acc: '请输入上家给你开的子账号',
  pwd: '请输入上家给你开的子账号登录密码，不然没法验证',
} as const

/**
 * 提交前的四条校验（对标的**顺序**：名称 → 网址 → 账号 → 密码）。
 *
 * 返回 `{error}`（`''` = 通过）——与 `system-sourelist.ts::validateSoureForm` 同一手法：
 * **判据只有这一处**，调用方不再自己判断一遍。
 *
 * ⚠️ 判据保持对标的**宽松口径**：只判空串（不 trim、不校验网址形状）。「网址必须是合法 URL」
 * 这一条在**后端**（`normalize_base_url` 抛自有中文，spec §14-R7），⛔ 不在前端重复判一次
 * （同一事实不在两处各判一次）。
 */
export function validateBindingForm(input: {
  homeCompany: string
  homeUrl: string
  homeAcc: string
  homePwd: string
}): { error: string } {
  if (input.homeCompany === '') return { error: VALIDATION_MESSAGES.name }
  if (input.homeUrl === '') return { error: VALIDATION_MESSAGES.url }
  if (input.homeAcc === '') return { error: VALIDATION_MESSAGES.acc }
  if (input.homePwd === '') return { error: VALIDATION_MESSAGES.pwd }
  return { error: '' }
}

/**
 * 「同步失败」红字提示那条 DOM 的 id（⭐ **裁决 R20**：失败必须在页面上看得见）。
 *
 * ⚠️ 这是本页**有意偏离对标**（差异表 #7：为**真实性**而加、⛔ 不是复刻）。
 * ⚠️ **判据读统一列表的行**：鸡蛋铺那一列 `consecutive_failures` 恒 `0`
 * （模型没有这一列）⇒ `> 0` 这个判据不会把鸡蛋铺行算成失败。
 * ⛔ 本页**不做**"连续失败到阈值自动吊销绑定"（裁决 R20 排到 P2：重投还不存在时
 * 按计数自动吊销，会让一次网络抖动永久弄死一条好绑定、只能重新握手恢复）。
 */
export const FAILURE_NOTICE_ID = 'peerBindingFailureNotice'

/**
 * 「同步失败」提示的文案（纯函数，便于用真值钉住）。
 *
 * ⚠️ 只看**未解绑**的绑定（团团通那一半的列表本来就不含软删行，计划 §2.1）；
 * `consecutive_failures > 0` 即视为"最近失败过"。取 `last_error_at` **最新**的那一条展示原因
 * （多行失败时不好一屏列全）。
 */
export function failureNoticeText(failed: readonly UpstreamBindingRow[]): string {
  if (failed.length === 0) return ''
  const latest = failed.reduce((a, b) =>
    (b.last_error_at ?? '') > (a.last_error_at ?? '') ? b : a,
  )
  const when = latest.last_error_at ? `，最近一次 ${formatDateTime(latest.last_error_at)}` : ''
  const reason = latest.last_error || '（对端没有回原因）'
  return `注意：${failed.length} 个上家系统的订单同步最近失败了（已连续失败 ${latest.consecutive_failures} 次${when}）：${reason}`
}

/**
 * 「最近一次收到同步」那块信息的 DOM id（⭐ **裁决 A19** / spec §16.7 第 2 条）。
 *
 * ⚠️ 这是本页**又一处有意偏离**对标（差异表 #8：为**真实性**而加、不是复刻）。
 * 统一列表端点回的 `last_ok_at` 就是"这个上家最近一次推来的东西是什么时候"。
 * ⚠️ 这块信息**也含鸡蛋铺行**；名称取统一行的 `system_name`，缺了回 `—`
 * （与表格那一列**同一份**口径，见 `upstreamSystemName`）。
 */
export const LAST_SYNC_NOTICE_ID = 'peerBindingLastSyncNotice'

/**
 * 那块信息的 **class 名**。
 *
 * ⚠️ **类名与选择器分成两个常量**（`AGENTS.md` §5-G 陷阱① / E-0061）：拼 `class=".x"` 会造出
 * 一个永远匹配不到的选择器，而症状是**静默**的。拼 class 一律用本常量，查询一律用下面那个。
 */
export const LAST_SYNC_NOTICE_CLASS = 'peer-binding-last-sync'
/** 同一个容器的 CSS 选择器（带点）。 */
export const LAST_SYNC_NOTICE_SELECTOR = '.peer-binding-last-sync'

/**
 * 「最近一次收到同步」的文案（纯函数，便于用真值钉住；**每行一个绑定**）。
 *
 * 口径见 spec §16.7 第 2 条（**绝对时刻**）/ 裁决 A19：
 * - 收到过 ⇒ `{上家名称}：{formatDateTime(last_ok_at)}`
 * - 没收到过 ⇒ `{上家名称}：尚未收到过同步`
 *
 * ⛔ **这是信息、不是报警**（A19）：只写**时间点**，⛔ 不写"多久没收到"、⛔ 不按时间比较、
 * ⛔ 没有阈值、⛔ 没有心跳——低单量工作室"两小时没单"是正常现象，不是故障。
 *
 * ⚠️ 顺序照**列表顺序**（不是按时间排序）：这是"每个上家各自最近一次"，不是排行榜。
 * ⚠️ 名称一律过 `upstreamSystemName`（缺名称回 `—`）：鸡蛋铺行在同步引擎跑过之前 `system_name`
 * 是空串，直接拼会在行首留下一个**光秃秃的全角冒号**（`：尚未收到过同步`）。
 */
export function lastSyncNoticeText(rows: readonly UpstreamBindingRow[]): string {
  return rows
    .map((row) =>
      row.last_ok_at
        ? `${upstreamSystemName(row)}：${formatDateTime(row.last_ok_at)}`
        : `${upstreamSystemName(row)}：尚未收到过同步`,
    )
    .join('\n')
}

/**
 * 弹层参数：**逐项**照对标。
 * `type: 1` 由 `openPanelLayer` 自己补（它还要把 `content` 包成 jQuery 对象）。
 *
 * ⚠️ `title` 缺省 = 新增弹窗的对标原文；行内「编辑」复用同一份弹层、只换标题
 * （D-0256：编辑走同一条「鸡蛋铺」分支，其余参数一个值都不改）。
 */
export function bindingDialogOptions(content: unknown, title = '绑定一个新的上家系统'): Record<string, unknown> {
  return {
    title,
    shade: [0.3, '#333'],
    shadeClose: true,
    skin: 'layui-layer-lan',
    anim: 8,
    area: ['680px', '480px'],
    content,
  }
}

/** 解绑确认语（**团团通**那条链路）：**逐字**照对标。 */
export const UNBIND_CONFIRM_TEXT =
  '确定要解除与这个上家的绑定吗？注意：解绑后，上家指派过来的新订单无法同步，但在本系统解绑之前同步过来的订单不受影响'

/**
 * 删一条**鸡蛋铺**绑定的确认语（⭐ **我方自拟**：对标那句只讲团团通那条链路，
 * 而鸡蛋铺那一类是**真删** ⇒ 必须写明级联代价）。
 *
 * ⚠️ 它讲的代价与后端行为**一一对应**：它名下还在跟踪的同步链路行
 * （订单 ↔ 上家单的对应关系）**跟着删、不可恢复**；大厅镜像行的归属置空、同步台账保留。
 * ⛔ 别把它「统一」成上面团团通那一句——那一句说的是**软删 + 通知上家**，两件事不一样。
 */
export const JIDANPU_UNBIND_CONFIRM_TEXT =
  '确定删除这条上家鸡蛋铺绑定吗？删除后，它名下还在跟踪的同步链路会一并删除（订单与上家单的对应关系不再保留），同步台账保留。'

/** `layer.confirm` 的按钮参数：逐字照对标的 `{icon:3,title:'提示'}`。 */
export const UNBIND_CONFIRM_OPTIONS = { icon: 3, title: '提示' }

/** 解绑成功后的前端提示（后端成功信封的 `msg` 是空串 ⇒ 用前端文案，先例 `system-sourelist.ts`）。 */
export const UNBIND_OK_TEXT = '已解绑'

/** 绑定成功后的前端提示（同上：`request()` 成功只回 `data`，信封的 `msg` 被解包丢掉）。 */
export const BIND_OK_TEXT = '绑定成功'

/** 弹层节点取不到时的兜底提示（绝不静默）。 */
export const PANEL_MISSING_TEXT = '弹窗内容缺失，请联系管理员'

/* ══════════════ 弹窗「系统类型」分流 ══════════════
 *
 * ## 两个分支各是什么
 *
 * - `团团通系统` = 本页**原本那条**链路（四格 → `POST /api/business/peer-bindings/`，载荷与行为逐字不变；
 *   **编辑**一支另有「同步上家抢单大厅」开关与「大厅价格百分比」两格，
 *   写入口 = `PUT /api/business/peer-bindings/<pk>/`，只写这两键）；
 * - `鸡蛋铺系统` = 迁移过来的那一份（输入 → 行级 POST / PUT，
 *   另加「发单价百分比」「启用」与「订单来源」（D-0261）三格）；
 *   ⚠️ 它是**页面上唯一**写那一份绑定的入口（表格下方那块表单已删除，见差异表 #9）。
 * - ⭐ 鸡蛋铺分支**复用证据件自带的「上家系统名称」格**（`homeCompany`）写后端的 `name` 列
 *   ——两个分支都用它（团团通写 `peer_name`、鸡蛋铺写 `name`，见 `applySystemType` /
 *   `readDialogJidanpuPayload`）。选填（鸡蛋铺分支不判空）；占位提示 / `maxlength` 逐字取证据件，
 *   ⛔ 不另造视觉值。
 *
 * ## 新项怎么进去（运行期注入，⛔ 证据件一个字不动）
 *
 * 「系统类型」插成 `.layui-form-pane` 的**第一个子元素**（原第一位是证据件自带的那条「注意」，
 * 调用方先把它删掉，见 mount 里那段「运行期」注释）；「百分比 + 启用」插在**密码那一格之后**，
 * 「订单来源」再插在「启用」之后（三格都属「鸡蛋铺」分支）。
 * ⚠️ 新项都必须在 `<form lay-filter="form">` **内部**：`form.val('form', …)` 是按 form
 * 作用域找输入的（vendored `layui.js` 的 `form.val`：`this.getFormElem(e).find('[name="…"]')`），
 * 插到 form 外会**静默**取不到（先例 `system-sourelist.ts` 的同一段注释）。
 *
 * ## 视觉值从哪来（⛔ 不发明新值）
 *
 * 标签 `style="width: 135px; padding: 8px 5px;"`、输入块 `style="margin-left: 135px;"`
 * 与输入框 `class="layui-input layui-input-shadow"` **逐字**取本页证据件弹层那四格。
 * ⛔ 证据件本体不动，这些新项是**运行期**插的。
 */

/** 「系统类型」下拉的 `name`（也是它的 `id`）。 */
export const SYSTEM_TYPE_FIELD_NAME = 'homeSystemType'
/** `鸡蛋铺系统` = 迁移过来的那一份（一条 PUT 打到**本人那一行**单行资源上）。 */
export const SYSTEM_TYPE_JIDANPU = 'jidanpu'
/** `团团通系统` = 本页原本那条 `peer-bindings` 链路（逐字不变）。 */
export const SYSTEM_TYPE_TUANTUANTONG = 'tuantuantong'
/**
 * 两个系统类型在**屏幕上**的中文（唯一出处）。
 *
 * ⚠️ 两处用它：① 弹窗下拉的选项文案；② 统一列表第一列「系统类型」的单元格
 * （`systemTypeLabel`）——⛔ 不许在两处各抄一份中文。
 */
export const SYSTEM_TYPE_LABEL_JIDANPU = '鸡蛋铺系统'
export const SYSTEM_TYPE_LABEL_TUANTUANTONG = '团团通系统'
/**
 * 默认类型 = **鸡蛋铺系统**。
 * ⚠️ 要翻只改这一处：`openForm()` 的初值与 `applySystemType` 的分流都以它为准。
 */
export const SYSTEM_TYPE_DEFAULT = SYSTEM_TYPE_JIDANPU
/** 下拉的选项表（顺序 = 屏幕上的顺序；第一项就是浏览器默认选中项）。 */
export const SYSTEM_TYPE_OPTIONS: ReadonlyArray<{ value: string; label: string }> = [
  { value: SYSTEM_TYPE_JIDANPU, label: SYSTEM_TYPE_LABEL_JIDANPU },
  { value: SYSTEM_TYPE_TUANTUANTONG, label: SYSTEM_TYPE_LABEL_TUANTUANTONG },
]
/**
 * 列表行里的 `system_type` → 屏幕上的中文（⭐ **我方新增列**）。
 *
 * ⛔ **不许把 `jidanpu` / `tuantuantong` 直接上屏**：那是机器值，用户看不懂。
 * ⚠️ 取值不在契约里时回 `—`（⛔ 不拿机器值当兜底，也⛔ 不假装它是团团通/鸡蛋铺）。
 */
export function systemTypeLabel(type: string): string {
  if (type === SYSTEM_TYPE_JIDANPU) return SYSTEM_TYPE_LABEL_JIDANPU
  if (type === SYSTEM_TYPE_TUANTUANTONG) return SYSTEM_TYPE_LABEL_TUANTUANTONG
  return '—'
}
/**
 * 下拉的 `lay-filter`（layui 的切换事件名是 `select(<lay-filter>)`）。
 * ⚠️ 与 `SYSTEM_TYPE_FIELD_NAME` **分成两个常量**（既有纪律：`name` 给字段、`lay-filter` 给事件）——
 * 今天两者同值，明天只改一个不会静默把另一个也改掉。
 */
export const SYSTEM_TYPE_SELECT_ID = 'homeSystemType'
/** 弹窗里「发单价百分比」那一格的 `name`。 */
export const HALL_PERCENT_FIELD_NAME = 'homeHallPercent'
/** 弹窗里「启用」开关的 `name`。 */
export const ENABLED_FIELD_NAME = 'homeEnabled'
/** ⭐ D-0261：弹窗里「订单来源」下拉的 `name`（写入口是 `order_source_id`，页面侧的名字独立起）。 */
export const ORDER_SOURCE_FIELD_NAME = 'homeOrderSource'
/** ⭐ D-0261：「订单来源」下拉里「未指定」那一项的 `value`（空串；提交时翻成 `null`）。 */
export const ORDER_SOURCE_NONE_VALUE = ''
/** 「未指定」那一项的屏幕文案（我方自拟：字典项都删光了时下拉里也只有它，⛔ 不能是空下拉）。 */
export const ORDER_SOURCE_NONE_LABEL = '不指定'
/** 弹窗里三个**证据件自带**的输入的 `name`（逐字，⛔ 一个都不改名）。 */
export const DIALOG_FIELD_NAMES = {
  company: 'homeCompany',
  url: 'homeUrl',
  acc: 'homeAcc',
  pwd: 'homePwd',
} as const
/** 运行期注入的三个**表单项**（`<div class="layui-form-item">`，即「那一格」的容器）的 id。 */
export const SYSTEM_TYPE_ITEM_ID = 'homeSystemTypeItem'
export const HALL_PERCENT_ITEM_ID = 'homeHallPercentItem'
export const ENABLED_ITEM_ID = 'homeEnabledItem'
/** ⭐ D-0261：「订单来源」那一格的容器 id。 */
export const ORDER_SOURCE_ITEM_ID = 'homeOrderSourceItem'

/** 六个新项的标签 / 输入块视觉值（逐字取证据件那四格，⛔ 不发明新值）。 */
const DIALOG_LABEL_STYLE = 'width: 135px; padding: 8px 5px;'
const DIALOG_BLOCK_STYLE = 'margin-left: 135px;'
/** 证据件里输入框的 class（逐字：`class="layui-input layui-input-shadow"`）。 */
const DIALOG_INPUT_CLASS = 'layui-input layui-input-shadow'

/**
 * 「系统类型」那一格的标记。
 *
 * ⚠️ 选项文案**逐字**取 `SYSTEM_TYPE_OPTIONS`（⛔ 不在这里另抄一份中文）；
 * ⛔ 六个新项的容器都带 id——切换可见性靠它（`name` 只给 layui 取字段）。
 */
export function systemTypeItemHtml(): string {
  const options = SYSTEM_TYPE_OPTIONS
    .map((option) => `<option value="${option.value}">${option.label}</option>`)
    .join('')
  return (
    `<div class="layui-form-item" id="${SYSTEM_TYPE_ITEM_ID}">` +
    `<label class="layui-form-label" style="${DIALOG_LABEL_STYLE}">系统类型</label>` +
    `<div class="layui-input-block" style="${DIALOG_BLOCK_STYLE}">` +
    `<select name="${SYSTEM_TYPE_FIELD_NAME}" lay-filter="${SYSTEM_TYPE_SELECT_ID}"` +
    ` class="${DIALOG_INPUT_CLASS}">${options}</select>` +
    '</div></div>'
  )
}

/**
 * 「发单价百分比」那一格的标记。
 *
 * ⚠️ `type=number / min=1 / max=100 / step=1 / placeholder="100"` 取自「我的资料」证据件里那一格
 * （同一个字段；⛔ 别写成两套边界）。页面上**只有这一个**入口，边界值与后端模型
 * `hall_price_percent` 的 1–100 对齐。
 */
export function hallPercentItemHtml(): string {
  return (
    `<div class="layui-form-item" id="${HALL_PERCENT_ITEM_ID}">` +
    `<label class="layui-form-label" style="${DIALOG_LABEL_STYLE}">发单价百分比</label>` +
    `<div class="layui-input-block" style="${DIALOG_BLOCK_STYLE}">` +
    `<input type="number" name="${HALL_PERCENT_FIELD_NAME}" min="1" max="100" step="1"` +
    ` class="${DIALOG_INPUT_CLASS}" placeholder="100">` +
    '</div></div>'
  )
}

/** 「启用」那一格的标记（layui 开关；`name` 见 `ENABLED_FIELD_NAME`）。 */
export function enabledItemHtml(): string {
  return (
    `<div class="layui-form-item" id="${ENABLED_ITEM_ID}">` +
    `<label class="layui-form-label" style="${DIALOG_LABEL_STYLE}">启用</label>` +
    `<div class="layui-input-block" style="${DIALOG_BLOCK_STYLE}">` +
    `<input type="checkbox" name="${ENABLED_FIELD_NAME}" lay-skin="switch" lay-text="启用|停用">` +
    '</div></div>'
  )
}

/**
 * ⭐ D-0261：「订单来源」那一格的标记（下拉）。
 *
 * ⚠️ 标记里**只带「不指定」一项**：字典项是租户数据，页面**打开时**才注入
 * （`fillOrderSourceOptions`，数据源 = `fetchOrderSources()`）——写在标记里就成了
 * 编译期的死数据。第一项 = 默认选中 = 「不指定」（值 = 空串）。
 */
export function orderSourceItemHtml(): string {
  return (
    `<div class="layui-form-item" id="${ORDER_SOURCE_ITEM_ID}">` +
    `<label class="layui-form-label" style="${DIALOG_LABEL_STYLE}">订单来源</label>` +
    `<div class="layui-input-block" style="${DIALOG_BLOCK_STYLE}">` +
    `<select name="${ORDER_SOURCE_FIELD_NAME}" class="${DIALOG_INPUT_CLASS}">` +
    `<option value="${ORDER_SOURCE_NONE_VALUE}">${ORDER_SOURCE_NONE_LABEL}</option>` +
    '</select>' +
    '</div></div>'
  )
}

/* ══════════ ⭐ 弹窗「团团通系统」分支的两格：同步上家抢单大厅 / 大厅价格百分比 ══════════
 *
 * 绑定行上的两列 `hall_sync_enabled` / `hall_price_percent`（`PeerUpstreamBinding`；开关**默认关**）。
 *
 * ## 为什么这两格**只在编辑已有绑定时出现**
 *
 * 写入口是 `PUT /api/business/peer-bindings/<pk>/`（只写这两键、少给的键不改）⇒ 它要一个
 * **已存在的行主键**。新增弹窗那条 POST 只收四个握手字段（`PeerBindCreateSerializer`），
 * 多带两键会被 DRF **静默忽略** ⇒ 若在新增弹窗里画出来并让用户以为生效，那是一句假话。
 * 所以新增弹窗把它们藏起来；行内「编辑」打开时才显示（`openPeerEditForm`）。
 * 新增的绑定照后端默认值 = **关 / 100**。
 *
 * ## 视觉值从哪来
 *
 * 同一张弹层里已有的那两套：标签 `style="width: 135px; padding: 8px 5px;"`、输入块
 * `style="margin-left: 135px;"`、输入框 `class="layui-input layui-input-shadow"`（逐字取证据件
 * 那四格）；开关用 layui 自带的 `lay-skin="switch"`（与本页「启用」那一格同形），提示行用 layui
 * 自带的 `.layui-form-mid layui-word-aux`。⛔ 证据件一个字不动，这两格是**运行期注入**。
 */

/** 「同步上家抢单大厅」开关的 `name`（也是 `readPeerHallSettings` 的读取键）。 */
export const PEER_HALL_SYNC_FIELD_NAME = 'homePeerHallSync'
/** 「大厅价格百分比」数字框的 `name`。 */
export const PEER_HALL_PERCENT_FIELD_NAME = 'homePeerHallPercent'
/** 两格的容器 id（切换可见性 / 页测试定位都靠它）。 */
export const PEER_HALL_SYNC_ITEM_ID = 'homePeerHallSyncItem'
export const PEER_HALL_PERCENT_ITEM_ID = 'homePeerHallPercentItem'
/** 百分比的合法范围与默认值（1–100，默认 100；⛔ 0 = 白送、⛔ >100 = 倒挂）。 */
export const PEER_HALL_PERCENT_MIN = 1
export const PEER_HALL_PERCENT_MAX = 100
export const PEER_HALL_PERCENT_DEFAULT = 100
/** 两格的屏幕文案（开关的 `lay-text` 是 layui 开关自己的两态文案）。 */
export const PEER_HALL_SYNC_LABEL = '同步上家抢单大厅'
export const PEER_HALL_PERCENT_LABEL = '大厅价格百分比'
export const PEER_HALL_SYNC_SWITCH_TEXT = '开启|关闭'
/** 「大厅价格百分比」下面那句人话（打手须知价格怎么来的）。 */
export const PEER_HALL_PERCENT_HINT = '打手看到的价格 = 上家大厅价 × 这个百分比'
/** 校验不通过时的那句字段级提示（⛔ 不发请求）。 */
export const PEER_HALL_PERCENT_INVALID_TEXT = '大厅价格百分比需要是 1 到 100 之间的整数'
/** 保存成功的前端提示（后端成功信封的 `msg` 是空串，与 `BIND_OK_TEXT` 同一处置）。 */
export const PEER_HALL_SAVE_OK_TEXT = '已保存'
/** 编辑一条**团团通**绑定时的弹窗标题（新增弹窗的标题照对标原文，⛔ 不改它）。 */
export const PEER_EDIT_DIALOG_TITLE = '编辑上家团团通绑定'

/**
 * 「同步上家抢单大厅」那一格的标记。
 *
 * ⚠️ 默认**不勾**（模型默认 `False`）——这里不写 `checked`，勾选状态只由 `form.val`
 * （新增复位成 `false` / 编辑回填库里现值）决定，⛔ 不要在标记里写死。
 */
export function peerHallSyncItemHtml(): string {
  return (
    `<div class="layui-form-item" id="${PEER_HALL_SYNC_ITEM_ID}">` +
    `<label class="layui-form-label" style="${DIALOG_LABEL_STYLE}">${PEER_HALL_SYNC_LABEL}</label>` +
    `<div class="layui-input-block" style="${DIALOG_BLOCK_STYLE}">` +
    `<input type="checkbox" name="${PEER_HALL_SYNC_FIELD_NAME}"` +
    ` lay-skin="switch" lay-text="${PEER_HALL_SYNC_SWITCH_TEXT}">` +
    '</div></div>'
  )
}

/**
 * 「大厅价格百分比」那一格的标记（数字框 1–100，默认 100，带一句人话提示）。
 *
 * ⚠️ `value="100"` 是**默认值**（与模型默认一致）：这一格是「库里现值」的编辑位
 * （不是「空 = 不改」那种语义），让用户看到 `100` 比看到一个空框更接近事实。
 */
export function peerHallPercentItemHtml(): string {
  return (
    `<div class="layui-form-item" id="${PEER_HALL_PERCENT_ITEM_ID}">` +
    `<label class="layui-form-label" style="${DIALOG_LABEL_STYLE}">${PEER_HALL_PERCENT_LABEL}</label>` +
    `<div class="layui-input-block" style="${DIALOG_BLOCK_STYLE}">` +
    `<input type="number" name="${PEER_HALL_PERCENT_FIELD_NAME}"` +
    ` min="${PEER_HALL_PERCENT_MIN}" max="${PEER_HALL_PERCENT_MAX}" step="1"` +
    ` class="${DIALOG_INPUT_CLASS}" placeholder="${PEER_HALL_PERCENT_DEFAULT}"` +
    ` value="${PEER_HALL_PERCENT_DEFAULT}">` +
    `<div class="layui-form-mid layui-word-aux">${PEER_HALL_PERCENT_HINT}</div>` +
    '</div></div>'
  )
}

/**
 * 后端详情（`GET peer-bindings/<pk>/` 或 PUT 的回包）→ 两格的值（**老数据按默认值兜底**）。
 *
 * 判据是「**是不是合法的布尔 / 1–100 的整数**」而不是真值（E-0021 同族）：
 *  - 开关只认**严格的 `true`**（`undefined` / `null` / `'true'` 都算「关」——⛔ 不让一个漂移的
 *    形状把「接收」打开）；
 *  - 百分比只认 `1–100` 的整数，其余（缺列 / `null` / `0` / `101` / 小数）一律回 **100**（默认）。
 * ⚠️ 0 是**非法配置**（白送），不是「用户要的 0%」⇒ 兜底到 100 而不是夹到 1。
 */
export function peerHallSettingsOf(
  detail: { hall_sync_enabled?: boolean; hall_price_percent?: number | null } | null | undefined,
): PeerHallSettings {
  const raw = detail?.hall_price_percent
  const percent =
    typeof raw === 'number' &&
    Number.isInteger(raw) &&
    raw >= PEER_HALL_PERCENT_MIN &&
    raw <= PEER_HALL_PERCENT_MAX
      ? raw
      : PEER_HALL_PERCENT_DEFAULT
  return {
    hall_sync_enabled: detail?.hall_sync_enabled === true,
    hall_price_percent: percent,
  }
}

/**
 * 从弹层 DOM 读两格并**就地校验**（返回 `error` 非空 ⇒ 调用方不许发请求）。
 *
 * ⚠️ 它是这一支**唯一**的读取口（读一次、校验与请求体都从这一份派生）。
 * ⚠️ 空串按**默认 100** 处理（不是 0）：与「发单价百分比」那格的既有口径一致。
 */
export function readPeerHallSettings(
  panel: HTMLElement | null,
): PeerHallSettings & { error: string } {
  const hall_sync_enabled = dialogInput(panel, PEER_HALL_SYNC_FIELD_NAME)?.checked === true
  const raw = (dialogInput(panel, PEER_HALL_PERCENT_FIELD_NAME)?.value ?? '').trim()
  const percent = raw === '' ? PEER_HALL_PERCENT_DEFAULT : Number(raw)
  if (
    !Number.isInteger(percent) ||
    percent < PEER_HALL_PERCENT_MIN ||
    percent > PEER_HALL_PERCENT_MAX
  ) {
    return {
      hall_sync_enabled,
      hall_price_percent: PEER_HALL_PERCENT_DEFAULT,
      error: PEER_HALL_PERCENT_INVALID_TEXT,
    }
  }
  return { hall_sync_enabled, hall_price_percent: percent, error: '' }
}

/**
 * ⭐ D-0261：把字典项灌进弹层里的「订单来源」下拉（**每次打开弹窗**都调——
 * 弹层可能刚从快照补出来，选项是注入时的旧形状）。恒保留第一项「不指定」。
 */
export function fillOrderSourceOptions(
  panel: HTMLElement | null,
  sources: readonly OrderSourceRow[],
): void {
  // ⚠️ 判「假值」而不是 `=== null`：`panel` 为 `null` 时可选链短路出的是 **`undefined`**
  // （`=== null` 守不住它——本次页测试当场红的实测）。
  const select = panel?.querySelector<HTMLSelectElement>(`[name="${ORDER_SOURCE_FIELD_NAME}"]`)
  if (!select) return
  select.innerHTML =
    `<option value="${ORDER_SOURCE_NONE_VALUE}">${ORDER_SOURCE_NONE_LABEL}</option>` +
    sources
      .map((source) => `<option value="${source.id}">${escapeHtml(source.name)}</option>`)
      .join('')
}

/**
 * ⭐ D-0261：编辑回填时，库里那项**不在字典返回里**（取数竞态 / 超 200 条被截断）⇒
 * 用行级 GET 自带的 `{id, 名字}` 补一个选项——⛔ 不补的话 `<select>` 塞不进那个值
 * （浏览器会把不存在的 value 静默丢成第一项，「保存却变不指定」）。
 */
export function ensureOrderSourceOption(
  panel: HTMLElement | null,
  source: { id: number; name: string },
): void {
  // ⚠️ 同 `fillOrderSourceOptions`：可选链短路出 `undefined`，⛔ 不判 `=== null`。
  const select = panel?.querySelector<HTMLSelectElement>(`[name="${ORDER_SOURCE_FIELD_NAME}"]`)
  if (!select) return
  if (select.querySelector(`option[value="${source.id}"]`) !== null) return
  select.insertAdjacentHTML(
    'beforeend',
    `<option value="${source.id}">${escapeHtml(source.name)}</option>`,
  )
}

/** 弹层里某一格输入（`null` = 这一格不在标记里；调用方自己决定怎么办，⛔ 不静默兜底成假值）。 */
function dialogInput(panel: HTMLElement | null, name: string): HTMLInputElement | null {
  return panel?.querySelector<HTMLInputElement>(`[name="${name}"]`) ?? null
}

/** 弹层里那一格的 `<select>`（系统类型那一格不是 `input`，另开一个读取口）。 */
function dialogSelect(panel: HTMLElement | null, name: string): HTMLSelectElement | null {
  return panel?.querySelector<HTMLSelectElement>(`select[name="${name}"]`) ?? null
}

/** 弹层里某一格输入所属的 `.layui-form-item`（切可见性要打在**那一格的容器**上）。 */
function dialogFormItem(panel: HTMLElement | null, name: string): HTMLElement | null {
  const item = dialogInput(panel, name)?.closest('.layui-form-item') ?? null
  return item instanceof HTMLElement ? item : null
}

/** 只切 `display`（⛔ 不是 `disabled`、⛔ 不是把值清掉：两条分支共用同一份 DOM 与同一个提交出口）。 */
function setItemVisible(item: HTMLElement | null, visible: boolean): void {
  if (item === null) return
  item.style.display = visible ? '' : 'none'
}

/**
 * 按系统类型切三格的 `display`（⭐ 业务方裁定的分流）。
 *
 * - `团团通系统` ⇒ 隐藏百分比 / 启用 / 订单来源（那一支只收四个凭据格 + 大厅设置）；
 * - `鸡蛋铺系统`（= 默认）⇒ 显示百分比 / 启用 / 订单来源。
 * - ⭐ 「上家系统名称」（`homeCompany`）那一格**两个分支都显示**——团团通
 *   写 `peer_name`、鸡蛋铺写后端的 `name` 列（再把它藏掉就是白白关掉一个「填名称」入口）。
 *
 * ⚠️ 判据是 `display:none` 那一层（⛔ 不禁用、⛔ 不清值）：两个分支的点位共用 `submit`，
 * 禁用或清值会让「切过去再切回来」丢掉用户刚填的东西。
 * ⚠️ `panel === null` 时**什么都不做**（调用方在弹层必须存在的那条路径上调用它）。
 */
export function applySystemType(
  panel: HTMLElement | null,
  type: string,
  opts: { peerHallEditable?: boolean } = {},
): void {
  if (panel === null) return
  const isTuantuantong = type === SYSTEM_TYPE_TUANTUANTONG
  setItemVisible(panel.querySelector<HTMLElement>(`#${HALL_PERCENT_ITEM_ID}`), !isTuantuantong)
  setItemVisible(panel.querySelector<HTMLElement>(`#${ENABLED_ITEM_ID}`), !isTuantuantong)
  // ⭐ D-0261：「订单来源」也只属「鸡蛋铺」分支（团团通绑定没有来源字典项）。
  setItemVisible(panel.querySelector<HTMLElement>(`#${ORDER_SOURCE_ITEM_ID}`), !isTuantuantong)
  // ⭐ 两格大厅设置只属团团通分支，且**只在编辑已有绑定时**可编辑
  // （新增没有行可 PUT，见那两格声明处的说明）。`=== true` 是 fail-closed。
  const peerHallVisible = isTuantuantong && opts.peerHallEditable === true
  setItemVisible(panel.querySelector<HTMLElement>(`#${PEER_HALL_SYNC_ITEM_ID}`), peerHallVisible)
  setItemVisible(panel.querySelector<HTMLElement>(`#${PEER_HALL_PERCENT_ITEM_ID}`), peerHallVisible)
}

/**
 * 把六个新项插进弹层（**幂等**：已存在的项不重复插）。
 *
 * ⚠️ **调用方必须先删掉弹窗里那条「注意」**（证据件自带）：系统类型要成为 `.layui-form-pane`
 * 的**第一个子元素**，那条 `blockquote` 不走就是它占着第一位。
 * ⚠️ 「百分比 + 启用」插在**密码那一格之后**（用 `name="homePwd"` 那一格定位，⛔ 不按
 * 「最后一个 `.layui-form-item`」现算——第二次调用时最后一项已经是我们自己插的启用了）；
 * ⭐ D-0261 的「订单来源」插在「启用」之后（同一族锚定法）。
 *
 * @returns 各项是否都在位（`false` = `panel` 取不到 / 弹层结构变了，页测试当场红）。
 */
export function injectDialogExtras(panel: HTMLElement | null): boolean {
  if (panel === null) return false
  const pane = panel.querySelector<HTMLElement>('.layui-form-pane')
  if (pane === null) return false
  // ① 系统类型：`.layui-form-pane` 的第一个子元素（`afterbegin` 正好是「插到最前面」）。
  if (panel.querySelector(`#${SYSTEM_TYPE_ITEM_ID}`) === null) {
    pane.insertAdjacentHTML('afterbegin', systemTypeItemHtml())
  }
  // ② 百分比 + 启用：插在密码那一格之后。两格各自按 id 查重（只缺一格时也能补上）。
  if (panel.querySelector(`#${HALL_PERCENT_ITEM_ID}`) === null) {
    const anchor = dialogFormItem(panel, DIALOG_FIELD_NAMES.pwd)
    if (anchor === null) return false
    anchor.insertAdjacentHTML('afterend', hallPercentItemHtml())
  }
  if (panel.querySelector(`#${ENABLED_ITEM_ID}`) === null) {
    const anchor =
      panel.querySelector<HTMLElement>(`#${HALL_PERCENT_ITEM_ID}`) ??
      dialogFormItem(panel, DIALOG_FIELD_NAMES.pwd)
    if (anchor === null) return false
    anchor.insertAdjacentHTML('afterend', enabledItemHtml())
  }
  // ③ ⭐ D-0261：订单来源——插在「启用」之后（同族锚定法）。
  if (panel.querySelector(`#${ORDER_SOURCE_ITEM_ID}`) === null) {
    const anchor =
      panel.querySelector<HTMLElement>(`#${ENABLED_ITEM_ID}`) ??
      panel.querySelector<HTMLElement>(`#${HALL_PERCENT_ITEM_ID}`)
    if (anchor === null) return false
    anchor.insertAdjacentHTML('afterend', orderSourceItemHtml())
  }
  // ④ ⭐ 同步上家抢单大厅 + 大厅价格百分比（插在「订单来源」之后；见上）。
  if (panel.querySelector(`#${PEER_HALL_SYNC_ITEM_ID}`) === null) {
    const anchor =
      panel.querySelector<HTMLElement>(`#${ORDER_SOURCE_ITEM_ID}`) ??
      dialogFormItem(panel, DIALOG_FIELD_NAMES.pwd)
    if (anchor === null) return false
    anchor.insertAdjacentHTML('afterend', peerHallSyncItemHtml())
  }
  if (panel.querySelector(`#${PEER_HALL_PERCENT_ITEM_ID}`) === null) {
    const anchor =
      panel.querySelector<HTMLElement>(`#${PEER_HALL_SYNC_ITEM_ID}`) ??
      panel.querySelector<HTMLElement>(`#${ORDER_SOURCE_ITEM_ID}`)
    if (anchor === null) return false
    anchor.insertAdjacentHTML('afterend', peerHallPercentItemHtml())
  }
  return true
}

/**
 * 读「鸡蛋铺」分支的载荷（恰好七键：⛔ 不带两个轮询间隔——页面上没有那两个输入框）。
 *
 * ⚠️ 它是这一支**唯一**的 DOM 读取口：`submit()` 用它读一次，校验与请求体都从**这一份**派生
 * （⛔ 不是「先读一遍校验、再读一遍发出去」）。
 * ⚠️ `password` **不 trim**（密码中的空格是密码的一部分）且**照传**（后端 `write_only`；
 * 弹窗这一支由校验保证非空——它是**新增**，与「编辑可留空 = 不改」不是一回事）。
 * ⚠️ 网址 / 账号 / **名称** trim（后端也会再归一化一次）。
 * ⭐ `name`（上家系统名称）复用证据件自带的 `homeCompany` 格——**选填**，
 * 空串照发（后端口径：这一格可回显 ⇒ 空串 = 清除，与密码的「空串 = 不改」不同一套语义）。
 * ⭐ D-0261：`order_source_id` —— 下拉的空串（「不指定」）**显式翻成 `null`**（后端口径：
 * `null` = 清除）；数字格的值翻成 `Number`。空串直接 `Number('')` 会得 `0`（E-0021：
 * `0` 不是合法主键）⇒ 分流先判空串。
 *
 * ⚠️ 返回类型把 `password` 收紧成**必填 string**（`JidanpuUpstreamBindingPayload` 里它是可选的
 * 「不可改」用法）：弹窗这一支是**新增**，密码必须真给。类型上收紧，是为了让「拿它拼载荷时
 * 密码可能是 `undefined`」不可能发生。
 */
export function readDialogJidanpuPayload(
  panel: HTMLElement | null,
): JidanpuUpstreamBindingPayload & { password: string } {
  const value = (name: string): string => dialogInput(panel, name)?.value ?? ''
  const rawSource = dialogSelect(panel, ORDER_SOURCE_FIELD_NAME)?.value ?? ORDER_SOURCE_NONE_VALUE
  return {
    jidanpu_base_url: value(DIALOG_FIELD_NAMES.url).trim(),
    account: value(DIALOG_FIELD_NAMES.acc).trim(),
    // ⭐ 人填的上家系统名称（选填；空串 = 清除，见上面的 docstring）。
    name: value(DIALOG_FIELD_NAMES.company).trim(),
    password: value(DIALOG_FIELD_NAMES.pwd),
    // 空串 ⇒ 100（后端那一列的默认值）；范围校验以后端为准（前端只做早挡）。
    hall_price_percent: Number(value(HALL_PERCENT_FIELD_NAME) || '100'),
    enabled: dialogInput(panel, ENABLED_FIELD_NAME)?.checked ?? false,
    order_source_id: rawSource === ORDER_SOURCE_NONE_VALUE ? null : Number(rawSource),
  }
}

/**
 * 弹窗「鸡蛋铺」分支的三条校验（顺序：网址 → 账号 → 密码）。
 *
 * ⚠️ 三句文案**逐字**复用 `VALIDATION_MESSAGES`（⛔ 不新造文案）；「名称」那一条**不在这三格里**
 * ——鸡蛋铺的「上家系统名称」**选填**（模型有 `name` 列，但业务口径仍是选填 ⇒ 不判空；
 * 团团通那一支照旧必填）。
 * ⚠️ 密码：**新增必填**（页面上唯一的新增入口在弹窗）；**编辑可留空**（= 不改，D-0256）
 * ——调用方对编辑传占位串跳过这条判据（见 `submit` 的注释），本函数自己不区分两种模式。
 * ⚠️ 判据保持对标的**宽松口径**：只判空串（不校验网址形状——那是后端 `normalize_base_url` 的事）。
 */
export function validateJidanpuDialogForm(input: {
  homeUrl: string
  homeAcc: string
  homePwd: string
}): { error: string } {
  if (input.homeUrl === '') return { error: VALIDATION_MESSAGES.url }
  if (input.homeAcc === '') return { error: VALIDATION_MESSAGES.acc }
  if (input.homePwd === '') return { error: VALIDATION_MESSAGES.pwd }
  return { error: '' }
}

/* ══════════════ 行内「编辑」入口（E-0255 / D-0256） ══════════════
 *
 * 绑定改**多行**资源后，改一条上家绑定不再能靠「再新增一次」（那会多出一行而不是改旧行）
 * ⇒ 行内要有「编辑」。对标 `#baroperating` 里**没有**这颗按钮（它只有「删除绑定」）⇒
 * **运行期注入**（⛔ 证据件本体一个字不动，与弹窗新项同一手法）：
 * - **两类行都渲染**：鸡蛋铺行打开「编辑上家鸡蛋铺绑定」（D-0256 的既有行为）；
 *   团团通行也能编辑——打开的是**大厅设置**那一支（`openPeerEditForm`：
 *   四个凭据格只读展示，PUT 只写两格大厅设置）。
 * - 视觉值取证据件那颗「删除绑定」按钮**同族**（`layui-btn layui-btn-xs` + 图标 + 文案），
 *   ⛔ 不带它的 `layui-btn-danger`（编辑不是危险动作）。
 */

/** 行内「编辑」的 `lay-event` 名（与对标的 `submitDel` 同族命名）。 */
export const ROW_EDIT_EVENT = 'submitEdit'
/** 编辑弹窗的标题（新增弹窗的标题照对标原文不变，见 `bindingDialogOptions`）。 */
export const EDIT_DIALOG_TITLE = '编辑上家鸡蛋铺绑定'

/**
 * 注入 `#baroperating` 模板的这一段（按钮标记；**两类行共用**）。
 *
 * ⭐ 团团通行也有编辑入口（`PUT /api/business/peer-bindings/<pk>/` 只写大厅两格）
 * ⇒ 模板里不带 `d.system_type` 的 laytpl 条件，类型分流放在**点击期**的 `openEditForm` 里
 * （⛔ 两处判据必然漂移）。未知类型仍由那里的 fail-closed 分支挡住、不开弹窗。
 */
export const ROW_EDIT_BUTTON_TEMPLATE =
  `<a class="layui-btn layui-btn-xs" style="" lay-event="${ROW_EDIT_EVENT}"` +
  ` title="编辑这条上家绑定"><i class="layui-icon layui-icon-edit"></i>编辑</a>`

/**
 * 把「编辑」按钮**运行期注入**行内模板（幂等：已在模板里就不重复插）。
 *
 * ⚠️ 模板是 `<script type="text/html">` 的**文本内容**（layui 用 laytpl 解析它），
 * 注入就是拼文本前置——`textContent` 赋值不会把里面的标记当 DOM 解析。
 *
 * @returns 模板是否就位（`false` = 证据件的行内模板不见了，页测试当场红）。
 */
export function injectRowEditButton(root: HTMLElement): boolean {
  const tpl = root.querySelector<HTMLElement>(ROW_TOOLBAR_ELEM)
  if (tpl === null) return false
  if ((tpl.textContent ?? '').includes(ROW_EDIT_EVENT)) return true
  tpl.textContent = ROW_EDIT_BUTTON_TEMPLATE + (tpl.textContent ?? '')
  return true
}

/* ────────────────────────────── 挂载 ────────────────────────────── */

export async function mountHomesystemsettingPage(
  root: HTMLElement,
  _ctx: IslandContext,
): Promise<() => void> {
  document.title = TITLE

  // ① 先挂 layui（插 `layui.css`）——顺序反了会 FOUC。
  const layui = await bootstrapIsland(['form', 'table', 'layer'])
  const form = layui.form as LayuiModule
  const table = layui.table as LayuiModule
  const layer = layui.layer as LayuiModule

  const msg = (text: string): void => islandMsg(layer, text)
  const errText = (e: unknown): string => (e instanceof Error ? e.message : '操作失败')

  // ② 再塞证据件标记（`<style>` 段 + body 标记；零转义）。
  root.innerHTML = await loadConfigArtifact(ARTIFACT_KEY)
  // ③ 结构型组件必须在标记进来之后绑定。
  initIslandElements(layui)

  /* ── **运行期**注入（证据件本体一个字不动） ──
   *
   * ⚠️ 都必须在**快照之前**做完（下面 `initialPanel` / `panelTemplate`）：
   *    否则 `ensurePanel()` 从快照补出来的节点会**丢掉**它们（第二次打开弹窗就退回旧形态）。
   *  ① 密码框 `type="text"` → `type="password"`（spec §11.3，对标缺陷不复刻）；
   *  ② ⭐ **删掉弹窗里证据件自带的那条「注意」**（「只支持同为鸡蛋铺的系统绑定，其它系统暂不支持」）；
   *  ③ ⭐ **注入弹窗的新项**（系统类型下拉 / 发单价百分比 / 启用 / D-0261 订单来源
   *     / 两格大厅设置：同步上家抢单大厅 / 大厅价格百分比）。
   *  ④ ⭐ 行内「编辑」注入。
   *
   * ⚠️ ② 与 ③ 的**相对顺序无所谓**（⛔ 不是承重顺序）：③ 用 `afterbegin` 把系统类型插到
   *    `.layui-form-pane` 最前面，本来就在那条 `blockquote` **之前** ⇒ 先插后删与先删后插的
   *    终态 DOM 完全一样。**承重的是 ② 那句本身**（不删，那条「注意」就留在弹层里）。
   *    ⛔ 不要为这个顺序补用例。
   */
  const pwdInput = root.querySelector<HTMLInputElement>(`#${DIALOG_FIELD_NAMES.pwd}`)
  if (pwdInput) pwdInput.setAttribute('type', 'password')
  // ② 只删**弹层里**那一条（⛔ 不碰页首那条说明）。
  const dialogPanel = findPanel(root, PANEL_SELECTOR)
  dialogPanel?.querySelector('blockquote.layui-elem-quote')?.remove()
  // ③ 返回值只给排障与页测试用：`false` = 证据件的弹层结构变了（真产物上不可达）。
  injectDialogExtras(dialogPanel)
  // ④ ⭐ 行内「编辑」注入（E-0255 / D-0256）：证据件的 `#baroperating` 只有「删除绑定」，
  //    多行资源改一条绑定需要行内编辑入口（⛔ 证据件本体不动，注入在快照之前做完）。
  injectRowEditButton(root)

  /* ── ⭐ D-0261：弹窗「订单来源」下拉的字典（mount 时取一次） ──
   *
   * ⚠️ **fail-soft**：取不到（网络错 / 字典页还没建过项）⇒ 下拉里只剩「不指定」，
   * 保存照常可用——⛔ 不挡弹窗、⛔ 不弹全局报错（这不是「保存失败了」，是「少一个可选项」；
   * 残差：字典接口挂时管理员配不了来源，重开弹窗即重试）。
   * ⚠️ 选项在**每次打开弹窗**时灌（`openForm` / `openEditForm` 里调 `fillOrderSourceOptions`）
   * ——弹层可能刚从快照补出来，这里缓存的只是**数据**，不是 DOM。
   */
  let orderSourceOptions: readonly OrderSourceRow[] = []
  void fetchOrderSources()
    .then((page) => {
      orderSourceOptions = page.items
    })
    .catch(() => {
      orderSourceOptions = []
    })

  /* ── 运行期追加：**同步失败红字提示**（⭐ 裁决 R20，差异表 #7） ──
   *
   * ⚠️ 判据不是"页面里有没有这么个 div"，而是"接口回了失败 ⇒ 它真的可见"——
   * 挂载时是 `display:none`，只有 `parseData` 看到失败行才点亮（见 `renderFailureNotice`）。
   * 插在表格**上方**（对标表格紧跟在说明块后面；新块插在两者之间，不遮任何被比项）。
   */
  const failureNotice = document.createElement('div')
  failureNotice.id = FAILURE_NOTICE_ID
  // 视觉值自拟（对标没有这一块）：红框 + 浅红底 + 深红字，与 layui 的 `layui-bg-red` 同族。
  failureNotice.setAttribute(
    'style',
    'display:none;margin:10px 0;padding:8px 12px;border:1px solid #ff5722;' +
      'background:#fff2f0;color:#c62828;font-size:13px;line-height:20px;border-radius:3px',
  )
  root.querySelector(TABLE_ELEM)?.insertAdjacentElement('beforebegin', failureNotice)

  /** 按这一次取数的结果点亮 / 收起失败提示（`table.render` 的 `parseData` 每取一次调一次）。 */
  function renderFailureNotice(rows: readonly UpstreamBindingRow[]): void {
    const failed = rows.filter((row) => row.consecutive_failures > 0)
    if (failed.length === 0) {
      failureNotice.style.display = 'none'
      failureNotice.textContent = ''
      return
    }
    failureNotice.textContent = failureNoticeText(failed)
    failureNotice.style.display = 'block'
  }

  /* ── 运行期追加：**「最近一次收到同步」信息块**（⭐ A19 / spec §16.7，差异表 #8） ──
   *
   * ⛔ **信息、不是报警**：中性灰、无阈值、无心跳、也没有"多久没收到就自动收起 / 变红"
   *（业务裁定 A19：低单量工作室不许看到假报警）。有绑定就显示，空表就收起——判据只有这两条。
   * 插在失败提示与表格之间（两块都不遮任何被比项）。
   */
  const lastSyncNotice = document.createElement('div')
  lastSyncNotice.id = LAST_SYNC_NOTICE_ID
  // ⚠️ 拼 class 用 `LAST_SYNC_NOTICE_CLASS`（⛔ 不是带点的选择器常量）。
  lastSyncNotice.className = LAST_SYNC_NOTICE_CLASS
  // 视觉值自拟（对标没有这一块）：中性灰框，⛔ 不用失败提示那套红。
  // `white-space:pre-line` 让文案里的 `\n` 真的换行（`textContent` 不解析 HTML，⛔ 不拼标记）。
  lastSyncNotice.setAttribute(
    'style',
    'display:none;margin:10px 0;padding:8px 12px;border:1px solid #e6e6e6;' +
      'background:#fafafa;color:#666;font-size:13px;line-height:20px;border-radius:3px;' +
      'white-space:pre-line',
  )
  root.querySelector(TABLE_ELEM)?.insertAdjacentElement('beforebegin', lastSyncNotice)

  /** 每次取数刷新这块信息（每行一个绑定；空列表 ⇒ 收起）。 */
  function renderLastSyncNotice(rows: readonly UpstreamBindingRow[]): void {
    if (rows.length === 0) {
      lastSyncNotice.style.display = 'none'
      lastSyncNotice.textContent = ''
      return
    }
    lastSyncNotice.textContent = lastSyncNoticeText(rows)
    lastSyncNotice.style.display = 'block'
  }

  /* ── 弹层节点 ──
   *
   * 证据件里 `.change` 是**真实存在**的（`display:none`），但 `table.reload` 那一次
   * 渲染可能把工具栏那一带重建 / 把节点摘掉 ⇒ 留一份**模板快照**兜底（先例
   * `system-sourelist.ts`：没有它，「重载后再点新增」会静默打不开）。
   */
  const initialPanel = root.querySelector<HTMLElement>(PANEL_SELECTOR)
  const panelTemplate = initialPanel ? (initialPanel.cloneNode(true) as HTMLElement) : null
  const panelHost = (): HTMLElement | null =>
    root.querySelector<HTMLElement>('form.layui-form') ?? root.querySelector('form')

  /** 取回可用的弹层节点；被 layui 摘走后从快照补一个**干净副本**。 */
  function ensurePanel(): HTMLElement | null {
    const existing = findPanel(root, PANEL_SELECTOR)
    if (existing) return existing
    const host = panelHost()
    if (!host || !panelTemplate) return null
    const fresh = panelTemplate.cloneNode(true) as HTMLElement
    host.appendChild(fresh)
    return fresh
  }

  /* 弹层里的输入读取口 = 模块级的 `dialogInput` / `dialogSelect` / `dialogFormItem`
   *（按 `name` 在**弹层节点内**找，⛔ 不写文档级选择器）。六个新项的读取与载荷都走它们，
   * 所以这里不再另抄一份。 */

  /**
   * 新增弹窗的**重入闸**（对标那个 `var re`）。
   *
   * ⚠️ 它住在 `mount` 的闭包里，**活得比弹层久**（对标那个 `re` 住在 `type:2` 的独立 iframe
   * 文档里，重开 = 新文档 = 自动初始化；我们把它搬进同文档弹层后不再有那个免费复位）。
   * ⇒ **每次开弹窗必须复位**（`openForm` 的第一句）。不复位的症状是本项目**已经出过**的
   * 「新增成功后，再新增点『确定』一笔请求都不发、也没提示」（spec §11.4 / E-0079）。
   *
   * ⚠️ 复位点**有三处**、各有分工，别把它们合并成一处：
   *  - `openForm()`：开弹窗时复位——**唯一能救「上一次请求还在途 / 成功但没走 alert 回调时
   *    用户把弹窗关了」的那一处**；
   *  - `submit()` 的失败分支：后端拒绝 / 网络错时复位，让用户**不关弹窗**就能改了重试；
   *  - `submit()` **成功分支的第一句**（照对标：`re = true; layer.alert(...)`）：⛔ 不是可省的一句
   *    ——`layer.alert` 默认带关闭按钮（× / ESC），用户那样关掉时**确定回调根本不会跑**。
   *    复位若只放在回调里，表单弹窗还开着、`re` 仍是 false ⇒ 再点「验证并保存绑定」撞
   *    `if (!re) return`：**零请求、零提示**，只有关掉重开才恢复。
   *
   * ⚠️ 「开弹窗时复位」的判别用例**刻意不靠成功路径**（否则会与成功分支的复位自洽、变成恒真）：
   * 它构造「请求还在途 ⇒ `re` 停在 false」再由用户重开弹窗——见 spec 里「请求还在途时用户把
   * 弹窗关了再重开」那条（E-0035 族的自指断言就是这么防的）。
   */
  let re = true

  /**
   * **正在编辑哪一行**（E-0255 / D-0256）：`null` = 新增（POST）、数字 = 编辑那一行（PUT <pk>）。
   *
   * ⚠️ 与 `re` 同一套复位点：`openForm()`（开新增弹窗）把它清回 `null`；
   * `openEditForm()` 把它设成目标行。成功 / 失败**都不清**——用户用 × 关掉 alert 后再点保存，
   * 仍然是对**同一行**的编辑（与 `re` 的「成功分支第一句复位」不是一回事：
   * 这个状态描述「弹窗在编辑谁」，不描述「请求在不在途」）。
   */
  let editingRowId: number | null = null

  /** 解绑的去重闸（对标解绑也借用了同一个 `re`；这里**独立**一份）。
   *
   * ⚠️ **为什么不能与新增共用一个闸**：新增那个闸在**请求在途**时是 `false`
   * （`submit()` 里 `re = false` → 结果回来才复位；成功分支的第一句就复位，所以"成功后
   * 一直不复位"是**错的**）。共用一个闸的话，用户在等握手的那几秒里点「删除绑定」会被
   * `if (!re) return` **静默**挡掉（零请求、零提示）。对标那边两个动作分属不同文档、
   * 天然不共享状态；搬到同文档弹层后必须自己分开。
   */
  let deleting = false
  let layerIndex: number | null = null

  /**
   * 把弹层里**四个凭据格**设成只读 / 可写（`readOnly`，⛔ 不是 `disabled`：值仍随表单提交、
   * 也能被选中复制）。
   *
   * ⚠️ 只有「编辑团团通绑定」那一支需要它：那一支的 PUT **只写两格大厅设置**
   * （`updatePeerBindingHallSettings`），四个凭据格是**展示**。留成可编辑 = 让用户以为
   * 改了会生效（同一件事两套判据）。新增 / 鸡蛋铺编辑那一支照旧可编辑。
   */
  function setCredentialsReadOnly(panel: HTMLElement | null, readOnly: boolean): void {
    for (const name of Object.values(DIALOG_FIELD_NAMES)) {
      const input = dialogInput(panel, name)
      if (input) input.readOnly = readOnly
    }
  }

  /**
   * ⭐ 行内「编辑」打开一条**团团通**绑定。
   *
   * 与鸡蛋铺那一支**同形**（先 GET 一次库里现值 → 预填 → 开弹窗），差别只有三处：
   *  ① 读的是 `peer-bindings/<pk>/`（`getPeerBinding`），回的是 `peer_name` / `peer_base_url` /
   *    `peer_account` + 两格大厅设置；
   *  ② 四个凭据格**只读**（这一支的 PUT 只写两格，见 `setCredentialsReadOnly`）；
   *  ③ 两格大厅设置**显示**出来（`applySystemType(..., { peerHallEditable: true })`），
   *    老数据缺这两列时按默认值兜底（`peerHallSettingsOf`：关 / 100，⛔ 不是 `undefined`）。
   *
   * ⚠️ 密码格永远留空（后端不回显）；本支**不提交**它，所以空着不影响任何东西。
   */
  async function openPeerEditForm(id: number): Promise<void> {
    let detail: PeerBindingRow
    try {
      detail = await getPeerBinding(id)
    } catch (e) {
      msg(errText(e))
      return
    }
    const panel = ensurePanel()
    if (!panel) {
      msg(PANEL_MISSING_TEXT)
      return
    }
    editingRowId = id
    const hall = peerHallSettingsOf(detail)
    // 先灌「订单来源」字典（鸡蛋铺那三格在本支隐藏；`form.val` 仍是同一个表单作用域，
    // 所以照旧先灌它，⛔ 不留一个「切到鸡蛋铺分支时下拉是空的」中间态）。
    fillOrderSourceOptions(panel, orderSourceOptions)
    ;(form.val as (filter: string, data: Record<string, unknown>) => void)('form', {
      [SYSTEM_TYPE_FIELD_NAME]: SYSTEM_TYPE_TUANTUANTONG,
      // 鸡蛋铺那三格：本支隐藏，回默认值（⛔ 不残留上一次鸡蛋铺编辑里的值）。
      [HALL_PERCENT_FIELD_NAME]: '100',
      [ENABLED_FIELD_NAME]: false,
      [ORDER_SOURCE_FIELD_NAME]: ORDER_SOURCE_NONE_VALUE,
      // ⭐ 两格大厅设置 = 库里现值（老数据 ⇒ 关 / 100）。
      // ⚠️ 开关那一格给**布尔**（真 layui 的 `form.val` 走 `checked` 分支）。
      [PEER_HALL_SYNC_FIELD_NAME]: hall.hall_sync_enabled,
      [PEER_HALL_PERCENT_FIELD_NAME]: String(hall.hall_price_percent),
      // 四个凭据格 = 库里现值；密码永远留空（后端不回显）。
      [DIALOG_FIELD_NAMES.company]: detail.peer_name,
      [DIALOG_FIELD_NAMES.url]: detail.peer_base_url,
      [DIALOG_FIELD_NAMES.acc]: detail.peer_account,
      [DIALOG_FIELD_NAMES.pwd]: '',
    })
    applySystemType(panel, SYSTEM_TYPE_TUANTUANTONG, { peerHallEditable: true })
    setCredentialsReadOnly(panel, true)
    // 编辑模式收起「系统类型」（编辑不改「这是哪类系统」；放回的动作在 `openForm` 里）。
    setItemVisible(panel.querySelector<HTMLElement>(`#${SYSTEM_TYPE_ITEM_ID}`), false)
    ;(form.render as () => void)()
    layerIndex = openPanelLayer(
      root,
      layui,
      layer,
      bindingDialogOptions(panel, PEER_EDIT_DIALOG_TITLE),
      PANEL_SELECTOR,
    )
  }

  /**
   * 打开「绑定一个新的上家系统」弹窗（对标 `addBind()`）。
   *
   * ⚠️ **顺序是硬要求**：「先预填、再 open」——`layer.open({type:1, content: jq})` 会把
   * `.change` 节点**搬出 `<form>`**，而 `form.val('form', …)` 按 `<form lay-filter>` 的作用域
   * 找输入 ⇒ 先 open 再预填，`form.val` 找不到这些输入（先例 `system-sourelist.ts` 的同一段注释）。
   */
  function openForm(): void {
    // ⭐ E-0079 的复位：见 `re` 的声明处。这一句是承重的，删掉 ⇒ 第二次开弹窗点「确定」静默无请求。
    re = true
    // ⭐ D-0256：开的是**新增**弹窗 ⇒ 清掉上一次的编辑目标（否则「新增」会把旧行覆盖掉）。
    editingRowId = null
    if (layerIndex !== null) {
      ;(layer.close as (i: number) => void)(layerIndex)
      layerIndex = null
    }
    const panel = ensurePanel()
    if (!panel) {
      // 节点与快照都没有：只提示，不假装打开了（绝不静默）。
      msg(PANEL_MISSING_TEXT)
      return
    }
    // ⭐ D-0261：先灌「订单来源」字典、再 form.val（先于 form.val 的同一条硬要求）。
    fillOrderSourceOptions(panel, orderSourceOptions)
    ;(form.val as (filter: string, data: Record<string, unknown>) => void)('form', {
      // ⭐ 三格初值：系统类型回**默认**、百分比回 `100`、开关回**不勾**。
      //    ⚠️ 复选框那一格走的是真 layui 的 `l[0].checked = n` 分支（vendored `layui.js` 的
      //    `form.val`）⇒ 这里给**布尔**、⛔ 不是字符串 `'false'`（那会落成「已勾选」）。
      [SYSTEM_TYPE_FIELD_NAME]: SYSTEM_TYPE_DEFAULT,
      [HALL_PERCENT_FIELD_NAME]: '100',
      [ENABLED_FIELD_NAME]: false,
      // ⭐ D-0261：新增弹窗的来源回「不指定」。
      [ORDER_SOURCE_FIELD_NAME]: ORDER_SOURCE_NONE_VALUE,
      // ⭐ 两格大厅设置的**默认值** = 关 / 100（模型默认）。新增弹窗里它们不显示
      //（没有行可 PUT，见那两格声明处），但状态仍要复位——否则上一次编辑改成的「开 / 30%」
      // 会留在 DOM 里，下一次打开编辑弹窗的复位就被旧值污染（E-0079 同族）。
      [PEER_HALL_SYNC_FIELD_NAME]: false,
      [PEER_HALL_PERCENT_FIELD_NAME]: String(PEER_HALL_PERCENT_DEFAULT),
      // 三个字段的初值照对标 `addBind()`；`homePwd` 对标**没有**重置（它那边弹层是
      // 一次性文档）。我们在同文档复用节点 ⇒ 显式清空，否则上一次的密码会留在框里被下一次提交带上。
      [DIALOG_FIELD_NAMES.company]: '',
      [DIALOG_FIELD_NAMES.url]: 'http://',
      [DIALOG_FIELD_NAMES.acc]: '',
      [DIALOG_FIELD_NAMES.pwd]: '',
    })
    // ⭐ 编辑团团通那一支会把四个凭据格设成只读 ⇒ 新增时必须放回来，否则「编辑过一条
    //   团团通绑定再新增」会得到一个填不进去的表单（E-0079 同族）。
    setCredentialsReadOnly(panel, false)
    // ⭐ 可见性也要复位（E-0079 同族）：上一次切到「团团通系统」再关掉，这里不调的话，
    // 重开时那三格会**停在上一次的形态**（下拉显示「鸡蛋铺」但「上家系统名称」还露着）。
    // ⚠️ 新增模式**不显示**两格大厅设置：POST 只收四格（见上）。
    applySystemType(panel, SYSTEM_TYPE_DEFAULT, { peerHallEditable: false })
    // ⭐ D-0256：上一次若是**编辑**，「系统类型」那一格被收起过 ⇒ 这里必须放回来
    //（新增时要能选类型；先例同上：同文档复用节点，不复位就是静默的旧形态）。
    setItemVisible(panel.querySelector<HTMLElement>(`#${SYSTEM_TYPE_ITEM_ID}`), true)
    ;(form.render as () => void)()
    layerIndex = openPanelLayer(
      root,
      layui,
      layer,
      bindingDialogOptions(panel),
      PANEL_SELECTOR,
    )
  }

  /**
   * 行内「编辑」（E-0255 / D-0256）：按主键读一次库里现值 → 预填 → 打开**编辑**弹窗。
   *
   * ⚠️ **必须先读再开**：统一列表的行里没有 `enabled`（鸡蛋铺那两格必须按库里现值预填），
   * 不读库里现值就提交会把它们**静默打回默认值**（数据丢失）。读失败（行已被别人删了）
   * ⇒ 原样显示后端那句「这个绑定不存在」，⛔ 不开一个空弹窗。
   * ⚠️ 编辑只对**鸡蛋铺**行可达（按钮按 `system_type` 条件渲染）⇒ 类型选择收起、
   * 分支锁死「鸡蛋铺」（团团通行没有编辑端点）。
   */
  async function openEditForm(row: BindingCellRow): Promise<void> {
    re = true
    if (layerIndex !== null) {
      ;(layer.close as (i: number) => void)(layerIndex)
      layerIndex = null
    }
    // ⭐ 团团通行走**大厅设置**那一支（先 GET 一次详情 → 预填 → 只 PUT 两格）。
    if (row.system_type === SYSTEM_TYPE_TUANTUANTONG) {
      await openPeerEditForm(row.BindHomeSystemLogId)
      return
    }
    let detail
    try {
      detail = await getUpstreamBindingRow(row.BindHomeSystemLogId)
    } catch (e) {
      msg(errText(e))
      return
    }
    const panel = ensurePanel()
    if (!panel) {
      msg(PANEL_MISSING_TEXT)
      return
    }
    editingRowId = row.BindHomeSystemLogId
    // ⭐ D-0261：先灌字典；库里那项不在字典里（取数竞态 / 超 200 条截断）⇒ 用 GET 自带的
    // `{id, 名字}` 补一个选项，否则 `<select>` 塞不进那个值（静默变「不指定」= 换掉用户配置）。
    fillOrderSourceOptions(panel, orderSourceOptions)
    if (detail.order_source !== null) ensureOrderSourceOption(panel, detail.order_source)
    ;(form.val as (filter: string, data: Record<string, unknown>) => void)('form', {
      // 回填 = 库里现值：网址 / 账号 / 百分比 / 开关 / 订单来源；密码**永远留空**（后端不回显，
      // 留空 = 不改，与既有 member PUT 的同一口径）。
      [SYSTEM_TYPE_FIELD_NAME]: SYSTEM_TYPE_JIDANPU,
      [HALL_PERCENT_FIELD_NAME]: String(detail.hall_price_percent ?? '100'),
      [ENABLED_FIELD_NAME]: detail.enabled === true,
      [ORDER_SOURCE_FIELD_NAME]:
        detail.order_source === null ? ORDER_SOURCE_NONE_VALUE : String(detail.order_source.id),
      // ⭐ P1：两格大厅设置属团团通分支（本支隐藏）⇒ 回默认值，⛔ 不残留上一次的值。
      [PEER_HALL_SYNC_FIELD_NAME]: false,
      [PEER_HALL_PERCENT_FIELD_NAME]: String(PEER_HALL_PERCENT_DEFAULT),
      // ⭐ 人填的「上家系统名称」= 库里现值（没起名是空串；后端恒回这一键）。
      [DIALOG_FIELD_NAMES.company]: detail.name,
      [DIALOG_FIELD_NAMES.url]: detail.jidanpu_base_url,
      [DIALOG_FIELD_NAMES.acc]: detail.account,
      [DIALOG_FIELD_NAMES.pwd]: '',
    })
    applySystemType(panel, SYSTEM_TYPE_JIDANPU)
    // 上一次若是团团通编辑（四格只读）⇒ 这里必须放回来，否则鸡蛋铺这一支改不了凭据。
    setCredentialsReadOnly(panel, false)
    // 编辑模式收起「系统类型」（编辑不改「这是哪类系统」；放回的动作在 `openForm` 里）。
    setItemVisible(panel.querySelector<HTMLElement>(`#${SYSTEM_TYPE_ITEM_ID}`), false)
    ;(form.render as () => void)()
    layerIndex = openPanelLayer(
      root,
      layui,
      layer,
      bindingDialogOptions(panel, EDIT_DIALOG_TITLE),
      PANEL_SELECTOR,
    )
  }

  /**
   * 提交（对标 `form.on('submit(savebtn)')`）。
   *
   * ⭐ 按「系统类型」**分流成两条链路**，两条**共用同一颗按钮、同一个成功提示
   * （`BIND_OK_TEXT`）与同一道重入闸 `re`**（`re` 守的是「弹窗里这一次提交」，不是某一个端点）：
   *  - `团团通系统` = 本页原本那条（四格校验 + `POST /peer-bindings/`，⛔ 载荷与行为逐字不变）；
   *  - `鸡蛋铺系统` = 迁移过来的那一份（三格校验 + **行级**端点，D-0256：
   *    新增 = `POST /upstream-bindings/jidanpu/`、编辑 = `PUT …/jidanpu/<pk>/`；
   *    ⛔ 不打按用户的 member PUT——那会静默覆盖同账号的另一条绑定，E-0255）。
   *
   * ⚠️ 两个分支的校验与载荷都来自**同一份 DOM 快照**（鸡蛋铺那一支由
   * `readDialogJidanpuPayload` 读一次，校验与请求体都从它派生；团团通那一支在原处读四个值）。
   */
  async function submit(): Promise<void> {
    const panel = findPanel(root, PANEL_SELECTOR)
    // ⭐ 分流判据只读一次（下拉就在弹层里，与下面各格是同一份 DOM）。
    const systemType = dialogSelect(panel, SYSTEM_TYPE_FIELD_NAME)?.value ?? ''

    if (systemType !== SYSTEM_TYPE_TUANTUANTONG) {
      /* ── 分支二：鸡蛋铺系统（默认分支，见 `SYSTEM_TYPE_DEFAULT`）──
       * ⭐ D-0256：`editingRowId === null` = 新增 ⇒ **行级 POST**；
       *            数字 = 编辑那一行 ⇒ **行级 PUT <pk>**（⛔ 不再打按用户的 member PUT——
       *            那会把同账号的另一条绑定静默覆盖掉，E-0255）。 */
      // ⭐ `readDialogJidanpuPayload` 是这一支**唯一**的 DOM 读取口：校验与请求体来自同一份快照
      //（⛔ 不是「先读一遍校验、再读一遍发出去」——那样「判过的」与「发出去的」可能不是同一份）。
      const payload = readDialogJidanpuPayload(panel)
      const { error } = validateJidanpuDialogForm({
        homeUrl: payload.jidanpu_base_url,
        homeAcc: payload.account,
        // 密码：**新增必填**（页面上唯一的新增入口在这里）；**编辑可留空**（= 不改，
        // 后端不回显密码，留空只能是「不动它」）。占位串让共用的那条校验跳过 pwd 判据。
        homePwd:
          editingRowId !== null && payload.password === ''
            ? 'editing-blank-means-keep'
            : payload.password,
      })
      if (error !== '') {
        msg(error)
        return
      }
      // 与团团通分支同一口径：校验之后才看重入闸。
      if (!re) return
      re = false
      try {
        if (editingRowId === null) {
          await createUpstreamBindingRow(payload)
        } else {
          await updateUpstreamBindingRow(editingRowId, payload)
        }
        // ⭐ 成功分支的**第一句**就是复位（三个复位点之一，见 `re` 的声明处）。
        //    ⚠️ 它排在 `try` 里；⛔ 它不是「出错才复位」——`catch` 那一支自己也复位，两条路都复位。
        re = true
        /* ⭐ **鸡蛋铺那一行现在就在这张统一列表里**（那块回填没了，
         * 「列表里那一行」才是它的落点）= 与团团通分支同一个出口：`table.reload` 让 layui 重新取数。
         * ⛔ **必须在 `layer.alert` 的确定回调之外**：`layer.alert` 默认带关闭按钮（× / ESC），
         * 用户那样关掉时确定回调**根本不会跑** ⇒ 放进回调里的话，刚存好的那一行不会出现在列表里
         * （E-0079 同族）。 */
        ;(table.reload as (id: string, o: unknown) => void)(TABLE_ID, {})
      } catch (e) {
        // 失败**原样**显示后端 `msg`（业务码由 `api/http.ts` 统一映射）、**不关**弹窗，
        // 用户改完可重试（与团团通分支同一处置）。
        re = true
        msg(errText(e))
        return
      }
      ;(layer.alert as (t: string, cb: (i: number) => void) => void)(BIND_OK_TEXT, (index: number) => {
        ;(layer.close as (i: number) => void)(index)
        if (layerIndex !== null) {
          ;(layer.close as (i: number) => void)(layerIndex)
          layerIndex = null
        }
        // ⛔ 回调里**只关弹窗**：列表重取已在上面那次 `table.reload` 做掉（⛔ 别再补第二发）。
      })
      return
    }

    /* ── 分支一：团团通系统 ──
     *
     * ⭐ 这一支再按「新增 / 编辑」分两条出口——
     *  - **新增**（`editingRowId === null`）= 本页原本那条四格 POST（⛔ 载荷与行为逐字不变）；
     *  - **编辑**（数字）= 只存两格大厅设置：`PUT /api/business/peer-bindings/<pk>/`
     *    （载荷**只有** `hall_sync_enabled` / `hall_price_percent`；四个凭据格是只读展示，
     *    ⛔ 别把空密码 / 旧网址一起发出去）。
     */
    if (editingRowId !== null) {
      // 这一支**唯一**的读取口：读一次、校验与请求体都从这一份派生。
      const settings = readPeerHallSettings(panel)
      if (settings.error !== '') {
        // 字段级提示：0 / 101 / 非整数一律**一次请求都不发**（1–100）。
        msg(settings.error)
        return
      }
      // 与另两支同一口径：校验之后才看重入闸。
      if (!re) return
      re = false
      try {
        await updatePeerBindingHallSettings(editingRowId, {
          hall_sync_enabled: settings.hall_sync_enabled,
          hall_price_percent: settings.hall_price_percent,
        })
      } catch (e) {
        // 失败**原样**显示后端 msg（1000 越界 / 1002 不存在 / 网络错），**不关**弹窗。
        re = true
        msg(errText(e))
        return
      }
      // 成功分支的第一句复位（三个复位点之一，见 `re` 的声明处）。
      re = true
      // 列表重取**必须在 alert 回调之外**（× / ESC 关掉 alert 时回调不跑，E-0079 同族）。
      ;(table.reload as (id: string, o: unknown) => void)(TABLE_ID, {})
      ;(layer.alert as (t: string, cb: (i: number) => void) => void)(
        PEER_HALL_SAVE_OK_TEXT,
        (index: number) => {
          ;(layer.close as (i: number) => void)(index)
          if (layerIndex !== null) {
            ;(layer.close as (i: number) => void)(layerIndex)
            layerIndex = null
          }
          // ⛔ 回调里只关弹窗：列表重取已在上面那一发做掉。
        },
      )
      return
    }

    // 四个值**只读一次**：校验与载荷必须来自同一份快照（否则「判过的」与「发出去的」可能不是同一份）。
    const homeCompany = dialogInput(panel, DIALOG_FIELD_NAMES.company)?.value ?? ''
    const homeUrl = dialogInput(panel, DIALOG_FIELD_NAMES.url)?.value ?? ''
    const homeAcc = dialogInput(panel, DIALOG_FIELD_NAMES.acc)?.value ?? ''
    const homePwd = dialogInput(panel, DIALOG_FIELD_NAMES.pwd)?.value ?? ''
    const validated = validateBindingForm({ homeCompany, homeUrl, homeAcc, homePwd })
    if (validated.error !== '') {
      msg(validated.error)
      return
    }
    // 校验之后才看重入闸（照对标同一口径：校验不过时提示照给）。
    if (!re) return
    re = false
    try {
      await createPeerBinding({
        peer_name: homeCompany,
        peer_base_url: homeUrl,
        peer_account: homeAcc,
        password: homePwd,
      })
    } catch (e) {
      // 失败**原样**显示后端 `msg`（业务码由 `api/http.ts` 统一映射）、**不关**弹窗
      //（照对标：`layer.msg` + `re = true`，用户改完可重试）。
      re = true
      msg(errText(e))
      return
    }
    // 成功：对标走 `layer.alert(msg)` 的确定回调 → 关弹窗 + 刷新列表（spec §11.2）。
    // ⚠️ 我们**拿不到**服务端那句 `msg`（`request()` 成功只回解包后的 `data`）⇒ 用前端文案，
    //    ⛔ 不假装是服务端的话（先例 `order-orderrecycle.ts` 差异表 #11）。
    //
    // ⭐ 照对标：成功回调的**第一句是 `re = true`、再弹 alert**。
    //    照抄它不是装饰：`layer.alert` 默认带关闭按钮（× / ESC），用户那样关掉时**确定回调
    //    根本不会跑** ⇒ 复位若只放在回调里，表单弹窗还开着、`re` 仍是 false，再点
    //    「验证并保存绑定」就撞 `if (!re) return`——**零请求、零提示**。
    re = true
    ;(layer.alert as (t: string, cb: (i: number) => void) => void)(BIND_OK_TEXT, (index: number) => {
      ;(layer.close as (i: number) => void)(index)
      if (layerIndex !== null) {
        ;(layer.close as (i: number) => void)(layerIndex)
        layerIndex = null
      }
      // 列表走 layui 的 `url` 通道 ⇒ 重新取数的出口是 `table.reload`（不是页内自己再发一发）。
      ;(table.reload as (id: string, o: unknown) => void)(TABLE_ID, {})
    })
  }

  /**
   * 行内「删除绑定」（对标 `delBind()`）。
   *
   * ⭐ 按 `system_type` **分流成两个出口**：
   *  - `团团通系统` ⇒ 既有的 `DELETE /api/business/peer-bindings/<pk>/`（**软删** + 尽力通知上家），
   *    确认语**逐字**照对标那句；
   *  - `鸡蛋铺系统` ⇒ `DELETE /api/business/upstream-bindings/jidanpu/<pk>/`（**真删**，它名下的同步
   *    链路随之级联），确认语是我方自拟、写明级联代价的那一句（对标没有这一句）。
   *
   * ⚠️ 两个出口共用**同一道** `deleting` 闸（它守的是「这一次删除」，不是某一个端点）与同一个成功提示。
   */
  function remove(row: BindingCellRow): void {
    // ⭐ 分流判据只读一次（行就摆在这次事件里，两个出口后面都只用它）。
    const isJidanpu = row.system_type === SYSTEM_TYPE_JIDANPU
    ;(layer.confirm as (t: string, o: unknown, cb: (i: number) => void) => void)(
      isJidanpu ? JIDANPU_UNBIND_CONFIRM_TEXT : UNBIND_CONFIRM_TEXT,
      UNBIND_CONFIRM_OPTIONS,
      (index: number) => {
        ;(layer.close as (i: number) => void)(index)
        if (deleting) return
        deleting = true
        const removal = isJidanpu
          ? deleteUpstreamBinding(row.BindHomeSystemLogId)
          : deletePeerBinding(row.BindHomeSystemLogId)
        void removal
          .then(
            () => {
              ;(table.reload as (id: string, o: unknown) => void)(TABLE_ID, {})
              msg(UNBIND_OK_TEXT)
            },
            // 失败原样显示后端 msg（如 `1002` 「这个绑定不存在」），不重取列表。
            (e: unknown) => msg(errText(e)),
          )
          // 复位挂在 `finally`：闸活在弹层之外，漏了它「解绑一次后这一页再也解绑不了」（E-0079 同族）。
          .finally(() => {
            deleting = false
          })
      },
    )
  }

  /* ── 表格事件（layui 的 `table.on`）与表单提交 ── */
  ;(table.on as (e: string, cb: (obj: unknown) => void) => void)(
    `toolbar(${TABLE_ID})`,
    (obj: unknown) => {
      const event = (obj as { event?: string }).event
      if (event === 'refresh') {
        ;(table.reload as (id: string, o: unknown) => void)(TABLE_ID, {})
      } else if (event === 'addBtn') {
        openForm()
      }
    },
  )
  ;(table.on as (e: string, cb: (obj: unknown) => void) => void)(
    `tool(${TABLE_ID})`,
    (obj: unknown) => {
      const typed = obj as { event?: string; data?: BindingCellRow }
      if (typed.event === 'submitDel' && typed.data) remove(typed.data)
      // ⭐ D-0256：行内「编辑」**两类行都开**（团团通行 ⇒ 大厅设置那一支）。
      //    ⚠️ 这里仍按 `system_type` 分流（fail-closed）：契约外的类型**不开**弹窗，
      //    也不许静默落进鸡蛋铺那一支（那会拿错端点读错行）。
      else if (typed.event === ROW_EDIT_EVENT && typed.data) {
        const type = typed.data.system_type
        if (type === SYSTEM_TYPE_JIDANPU || type === SYSTEM_TYPE_TUANTUANTONG) {
          void openEditForm(typed.data)
        }
      }
    },
  )
  // 保存按钮（对标 `form.on('submit(savebtn)')`）。⚠️ 注册**在 `table.render` 之前**
  // 没关系：layui 的 `form.on` 只是登记回调，点击时按 `lay-filter` 找。
  ;(form.on as (e: string, cb: () => void) => void)('submit(savebtn)', () => {
    void submit()
  })
  /* ⭐ 弹窗里「系统类型」下拉：layui 的**文档路径**是 `form.on('select(<lay-filter>)')`
   *（读码 vendored `layui.js` 的 form 模块：`$ = "form"`，select 分支在用户选中时
   * `layui.event.call(this, $, "select(" + n + ")", {elem, value, othis})`，`n` 就是 `lay-filter`
   * ⇒ 事件名就是 `select(homeSystemType)`）。
   * ⚠️ 回调里取 `obj.value`（= layui 给的事件载荷，**刚选中的值**）——它是这条事件契约的一半；
   *    ⛔ 不要在这里回头读 `<select>.value`（那要依赖「layui 已经先写好原 select」这个
   *    它没写进文档的顺序；`submit()` 那边读 DOM 是另一回事，它跑在事件之后）。
   * ⚠️ 弹层被 layui 搬进 `.layui-layer-content` 之后**不在 `root` 里** ⇒ 一律用 `findPanel`
   *    （与 `submit()` 同一个查找口径）；写成 `root.querySelector` 在真浏览器里会**静默**不生效。
   */
  ;(form.on as (e: string, cb: (obj: unknown) => void) => void)(
    `select(${SYSTEM_TYPE_SELECT_ID})`,
    (obj: unknown) => {
      const value = (obj as { value?: unknown }).value
      // ⭐ 两格大厅设置只在**编辑已有绑定**时可见（`editingRowId !== null`）；
      // 新增弹窗里没有行可 PUT，画出来就是一句假话（见那两格声明处的说明）。
      applySystemType(findPanel(root, PANEL_SELECTOR), typeof value === 'string' ? value : '', {
        peerHallEditable: editingRowId !== null,
      })
    },
  )

  // ⑤ `table.render`：`url` 直连**统一列表**那条端点（团团通 + 鸡蛋铺混排），`parseData` 吃统一信封
  //    （`{code,msg,data:{items,total}}`）。
  //    ⚠️ 它必须排在上面那次运行期注入（弹窗六个新项）**之后**（见文件头「挂载顺序」）。
  ;(table.render as (o: unknown) => void)({
    elem: TABLE_ELEM,
    id: TABLE_ID,
    url: BINDINGS_TABLE_URL,
    method: 'get',
    toolbar: TOOLBAR_ELEM,
    defaultToolbar: ['filter'],
    cols: [COLS],
    height: 'full',
    page: false,
    text: { none: '暂无数据' },
    parseData: (r: {
      code: number
      msg: string
      data?: { items?: UpstreamBindingRow[]; total?: number }
    }) => {
      const items = r.data?.items ?? []
      // ⭐ R20：每次取数都刷新失败提示（失败行 ⇒ 点亮，全好 ⇒ 收起）。
      renderFailureNotice(items)
      // ⭐ A19：同一份数据刷新「最近一次收到同步」（有绑定就显示，空表收起）。
      renderLastSyncNotice(items)
      return {
        code: r.code,
        msg: r.msg,
        // 后端 `total` 是**服务端**计数；缺字段时回落 0（不抛，先例 `order-orderrecycle.ts`）。
        count: r.data?.total ?? 0,
        data: items.map(toBindingRow),
      }
    },
  })


  return () => {
    if (layerIndex !== null) {
      ;(layer.close as (i: number) => void)(layerIndex)
      layerIndex = null
    }
    // 只清我们自己注入的标记（layer 的弹层挂在 `document.body`，上面已 close）。
    root.innerHTML = ''
  }
}

export function mount(root: HTMLElement, ctx: IslandContext): Promise<() => void> {
  return mountHomesystemsettingPage(root, ctx)
}

export default { mount } satisfies IslandPage
