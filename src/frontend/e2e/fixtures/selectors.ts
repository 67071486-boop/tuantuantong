/**
 * 全部选择器与文案常量。
 *
 * ⛔ **类名常量与选择器常量必须分开**（`AGENTS.md` §5G 的 **E-0061** 教训）：把 `class="${PANEL}"`
 * 误写成 `class="${PANEL_SELECTOR}"` 会造出 `class=".x"`、**永远匹配不到**，而这是**静默**的——
 * `typecheck` 看不见、`lint` 看不见，页面只是「点了没反应」。
 */
/**
 * `index.html` 自己的标题。
 *
 * ⚠️ **Task 7（2026-09-24）起它不再是登录页的判据**：登录页在能拿到工作室名时会把
 * `document.title` 改写成 `团团通 - <工作室名>`（`src/views/LoginView.vue` 的 `onMounted`
 * 里那次赋值），取不到名字时才保留本常量（spec §7.3 的静默回落）⇒ 断言登录页标题必须用下面的
 * **模式**判据。
 */
export const APP_TITLE = '团团通 · 租户后台'

/**
 * 标题的**模式判据**（两条既有断言的唯一出处：`login()` 与 `00-connectivity`）。
 *
 * 为什么是模式而不是字面量：
 *  1. 标题的**后半截是运行期取到的工作室名**（`GET /api/auth/site`），E2E tenant 名一变它就变；
 *  2. ⛔ **不许把工作室名写进 `src/**`**（PII 守卫，`AGENTS.md` §5C/E-0077）——夹具里也不行。
 *
 * ⚠️ **它证明什么、不证明什么**（评审 J3 订正）：
 *  - 它**只**证明「**顶层文档的标题以产品名开头**」——`团团通 · 租户后台`（名字取不到时）与
 *    `团团通 - <名>`（取到时）两种都命中；
 *  - ⛔ **它不证明「我在登录页」**：island 页写的 `document.title` 是写在**它自己那个 iframe 文档**里的。
 *    查全仓 `document.title` 的写入点（`grep -rn "document.title" src/`）：除 `LoginView.vue`
 *    那次赋值外
 *    **全部**落在 island 页面模块 / `layui/` 渲染器里（都跑在 iframe 文档中）⇒ 顶层文档的标题在
 *    **除登录页以外的所有壳路由**上始终是 `index.html` 的 `团团通 · 租户后台`，
 *    这条判据在任何一页上都会绿。
 *    「我在登录页」在 `00-connectivity` 里由它自己的 `toHaveURL(/\/login/)` 承担，
 *    在 `login()` 里由「两个输入框真的能填」承担。
 */
export const LOGIN_TITLE_PATTERN = /^团团通/

/** 登录页那两个输入框（`src/frontend/src/views/LoginView.vue` 的 `id="login-username"` /
 *  `id="login-password"`）——都有 id，用 id 最稳。
 *  ⛔ 这里**不写行号**（E-0088）：本线往 `LoginView.vue` 里插内容时它已经整体位移过一次。 */
export const LOGIN_USERNAME = '#login-username'
export const LOGIN_PASSWORD = '#login-password'
/** 提交按钮无 id、无 testid（`LoginView.vue` 里 `class="login-submit"` 那颗 `button`），用它的 class。 */
export const LOGIN_SUBMIT = '.login-submit'
/** 失败时的错误文案容器（`LoginView.vue` 里 `class="login-error"` + `role="alert"`，文本 = 服务端 msg 原样）。 */
export const LOGIN_ERROR = '.login-error'

/**
 * island 宿主（`LayuiIslandHost.vue:60-65`）渲染出来的 iframe；**没有 id**，用 data-testid。
 * ⚠️ 放在这里（而不是 T7）是因为 **T5 就要用它**——`ISLAND_MOUNT` / `ISLAND_UNREGISTERED`
 * 才是 T7 追加的（见 T7 Step 1）。
 */
export const ISLAND_IFRAME = 'iframe[data-testid="layui-island"]'

/* ——— T7 全站冒烟：iframe **文档内部**的两个锚点 ——— */

/** iframe 文档内的挂载点（`src/frontend/layui-island/page.html:17`）。 */
export const ISLAND_MOUNT = '#app'
/**
 * 页面 key 没登记时 `#app` 会变成这句（`src/layui-island/page.ts:50`）——那是**真缺陷**，判红。
 * ⚠️ 任务书原文写的是 `page.ts:33-34`；执行期实测该文案在**第 50 行**（`grep -n 未登记的 island 页面`），
 * 按 §5C「文档必须按实现写」订正为实际行号，**文案与常量值本身未改**。
 */
export const ISLAND_UNREGISTERED = '未登记的 island 页面'

/* ——— 职位对拍（`05-role-action-audit.spec.ts`）：SPA 外壳里的侧栏菜单 ——— */

/**
 * `AppSidebar.vue` 里那个 `el-menu` 渲染出来的**根**节点（挂在 `.shell-sidebar` 下）。
 *
 * ⚠️ **必须靠 `role="menubar"` 把「根菜单」与「分组内联菜单」分开**（T2 执行期实测）：
 * `el-sub-menu` 展开后自己也会渲染一个 `ul.el-menu.el-menu--inline[role="menu"]`
 * ⇒ 裸的 `.shell-sidebar .el-menu` 一次命中 **8 个**元素（1 根 + 7 个分组），
 * `toBeAttached()` 直接以 strict mode violation 失败。根菜单的 `role` 是 `menubar`
 * （`element-plus/es/components/menu/src/menu.mjs` 渲染时写死 `role: "menubar"`），
 * 分组内联菜单是 `menu` ⇒ 这条属性就是两者的稳定区分点。
 *
 * 菜单在 **SPA 外壳**里（不是 island iframe 里）⇒ 这些常量一律在顶层 `page` 上用，
 * ⛔ 不要拿 `page.frameLocator(ISLAND_IFRAME)` 去取它们。
 */
export const SIDEBAR_MENU = '.shell-sidebar .el-menu[role="menubar"]'
/**
 * 分组（`AppSidebar.vue` 的 `v-for="g in groups"` 那个 `el-sub-menu`）。
 *
 * ⚠️ 顶层条目**没有**单独的常量：调用方用 `${SIDEBAR_MENU} > li.el-menu-item:not(.el-sub-menu)`
 * 直接把根菜单的直接子 `li` 取出来（分组标题是 `div.el-sub-menu__title`、不是 `li`，
 * 所以 `:not(.el-sub-menu)` 只是把分组那个 `li` 排除掉）。T2 实测 `count=3`（顶层条目数）。
 */
export const SIDEBAR_SUB_MENU = '.shell-sidebar .el-menu li.el-sub-menu'
/** 分组标题（`#title` 槽里渲染的 `div.el-sub-menu__title`）——同组条目标签靠它唯一化。 */
export const SIDEBAR_SUB_MENU_TITLE = '.el-sub-menu__title'
/** 分组**内**的条目（`el-menu-item` 渲染出来的 `li.el-menu-item`）。 */
export const SIDEBAR_SUB_MENU_ITEM = 'li.el-menu-item'
/**
 * 条目的**文本标签**（`AppSidebar.vue` 里那个 `<span>{{ item.label }}</span>`）——条目的**直接子** `span`。
 *
 * ⛔ **必须取「条目的直接子 span」，不能取整条 `li` 的 `textContent`**（T2 执行期实测）：
 * 置灰条目会多渲染一个 `<el-tag>即将开放</el-tag>`，它**自己也含一个 `<span>`**，
 * 于是整条的 `textContent` 是 `"自动发消息到微信群设置即将开放"`（`innerText` 同样）——
 * 拿它去查 `menu.ts` 的标签**永远查不到**。直接子 `span` 只有两个，**标签那个在前**，
 * 所以调用方要对**每一条条目**取 **`.first()`**（⛔ 不要对整个条目集合用 `allTextContents()`：
 * 那会把置灰条目的两个 span 当成两条，读出一个 `"即将开放"` 的假标签；T2 的探针实测到过这一形态）。
 *
 * ⚠️ **别用 `:first-child`**（T2 执行期实测踩到并订正）：`AppSidebar.vue` 里标签前后有
 * **HTML 注释**（dev / test 下 Vue 保留注释节点），于是 `<span>` 在 DOM 树里根本不是
 * `:first-child` ⇒ `:scope > span:first-child` **匹配到 0 个**，`innerText()` 会一直等到超时
 * （症状：admin 的用例 2.3 分钟后以「Target page … has been closed」超时收场，
 * 看起来像页面没渲染，实际是选择器差一个注释节点）。
 */
export const SIDEBAR_ITEM_LABEL_SPAN = ':scope > span'

/* ——— 职位对拍（`05-role-action-audit.spec.ts`，T3）：岛页 iframe **文档内部**的页内控件 ——— */

/**
 * 页内控件的**候选选择器全表**（spec §3.2 的五条，顺序照抄）——**唯一出处**。
 *
 * ⛔ **不许手工登记控件清单**（spec §3.2）：登记表会漂移，而「漂移的登记表」正是本线要防的东西。
 * 控件从**渲染结果**枚举，覆盖下界由 spec §3.4 的 N3（`有候选控件的页面一条都没点到 ⇒ 红`）守。
 * ⛔ **也不许把这个清单再抄进 spec / 别处**：那就是两套判据，而漂移只表现为静默少点几条
 * （本仓 `E-0021`）。spec 里有一条断言把这个数组**逐字**钉到 spec §3.2 的原文上。
 *
 * ⚠️ `.layui-btn` 与 `a.layui-btn` / `button.layui-btn` **有意重复**：前两条收的是「layui 按钮的两种标签」，
 * 第三条收「其它标签但带 layui-btn 类」的（例如 `<div class="layui-btn">`）。枚举时按元素去重（用 `Set`），
 * ⛔ 不要按选择器计数（重复的那条会让条数虚高）。
 */
export const CONTROL_SELECTORS: readonly string[] = [
  'a.layui-btn',
  'button.layui-btn',
  '.layui-btn',
  '[lay-event]',
  '[data-act]',
]

/** 候选选择器在页面里的作用域：island iframe 的挂载点（`#app`）。 */
export const CONTROL_SCOPE = ISLAND_MOUNT

/**
 * 驱动用的**候选选择器**：审计在页面上给"可驱动的候选控件"打一个临时标记属性，
 * 再用 `CONTROL_DRIVE_SELECTOR` 精确点到**那一个元素**。
 *
 * ⛔ **为什么不用"按选择器 + `.nth(i)`"**（T3 执行期实测，两轮都栽在这上面）：
 * `locator('#app .layui-btn').nth(i)` 与 `root.querySelectorAll('.layui-btn')[i]` **不是同一个元素**——
 * 实测 `/orders` 上两者都数到 232 条，但**顺序不同**：`.layui-btn` 只算候选一列，而 layui 的表格会
 * 把同一批行按钮**渲染两份**（主表体 + 固定列克隆），`locator()` 的命中顺序与 `querySelectorAll`
 * 的文档顺序在克隆体上分叉 ⇒ `.nth(19)` 点到的是"发送短信"、而枚举里第 19 条的身份串是
 * "指派打手"。后果是**静默点错元素**（点了一堆 `submitEdit`，把它们的 403 / 失败记到别的账上），
 * 而 D-0145 那条「指派」的 403 因此**一次都没被触发**。
 * 打标记 + 按标记取元素把"身份串"与"要点哪个元素"重新钉在一起，**与任何遍历顺序无关**。
 *
 * ⚠️ 标记属性是临时的、每个文档重建一次（`page.goto` 会换文档）；它**不参与身份串**
 * （`controlIdentity()` 只读 `class` / `data-act` / `lay-event` / 文本）。
 */
export const CONTROL_DRIVE_ATTR = 'data-tt-audit-drive'

/** 按标记取元素的选择器（与 `CONTROL_DRIVE_ATTR` 同一出处）。 */
export const CONTROL_DRIVE_SELECTOR = `[${CONTROL_DRIVE_ATTR}]`

/**
 * `CONTROL_SELECTORS` 五条并成一条的选择器——**只给「这一页的控件渲染出来没有」这个等待用**
 * （`waitForCandidates()`）。
 *
 * ⛔ **必须从 `CONTROL_SELECTORS` 派生**（`join(',')`），⛔ 不许另写一份串：另写一份就是两套判据，
 * 而漂移只表现为**静默少等几条**（`E-0021`）。
 * ⚠️ 它与 `CONTROL_DRIVE_SELECTOR` **不是**一回事、也**不能**互换：后者数的是审计**自己刚打上标记**
 * 的元素，枚举之前恒为 0（T7 执行期实测：拿它当"等渲染"的谓词 ⇒ 永远等满超时、`rendered` 恒假）。
 */
export const CONTROL_CANDIDATE_SELECTOR = CONTROL_SELECTORS.join(',')

/** 审计只认 `/api/**` 上的响应；写请求的拦断也按这个前缀（spec §3.3）。 */
export const AUDIT_API_PREFIX = '/api/'

/**
 * 登录端点（`POST /api/auth/login`）——**两种模式下都有意放行**，所以它从
 * 「非 GET 请求真的拿到响应」那条观测里**排除**（写模式 spec §6 的 W2/W3）。
 *
 * ⛔ **为什么必须有这条排除**：一个职位至少要换账号登录一次，而登录是**有意放行**的写请求
 * ⇒ 不排除的话它会被算成"写请求出网了"，W3 会**假红**。本线的观测窗口从**驱动期**起算
 * （登录在窗口之前，见 `nonGetResponsesBefore`），所以今天这条排除是**兜底**：它挡住
 * "驱动期里再发一次登录"（例如某个控件触发重新登录）那种形态。
 * ⚠️ 与 `LOGIN_*` 那几个 DOM 选择器**不是**一回事：那几个选登录**表单**的元素，
 *    这个选登录**请求的路径**。
 */
export const AUDIT_LOGIN_PATH = '/api/auth/login'

/** 只算 4xx 里的 403（spec §2：401 是登录前的会话探针、400/404 是页面自己的分支）。 */
export const AUDIT_FORBIDDEN = 403

/**
 * **N1 正对照用的假端点**（spec §3.4 的 N1）。⛔ 它在后端**不存在**：路由处理器 `fulfill` 出
 * 一个 403 就结束了，请求**根本不出网**（所以每个职位都成立、都有确定性——⛔ 不要拿真实端点当正对照，
 * `admin` 是超级对照、真实端点不会 403）。
 */
export const AUDIT_PROBE_URL = '/api/__audit_probe__'

/**
 * 页内控件的**身份串**（spec §3.2）：`<tag>.<class 前 2 个>#<data-act|lay-event>#<文本前 12 字>`。
 *
 * ⛔ **为什么是这个形状**（而不是 `index` / `outerHTML` / 文本全文）：
 *  - 它要能在**页面重渲染之后**重新定位同一个控件（点击可能触发 `goto` 换掉 iframe，
 *    那时枚举快照作废，只能拿这个串在**新快照**里找回同一条）⇒ 串必须只由**稳定属性**组成；
 *  - 它要能被**人**读着指认（基线冻结、报告里逐条列 403 都要拿它当主语）；
 *  - 它**不含** `data-id` / `data-order` 这类**逐行实例**属性：同一颗「指派」按钮在 20 行里出现 20 次，
 *    它们的身份**就该是同一个串**（本线的判据是「这个职位点这个控件会不会 403」，
 *    ⛔ 不是「第 3 行的按钮」；把实例 id 塞进来会让基线每跑一次都变一次）。
 *  - 它**不含**全文文本（只取前 12 字）：文本可能带动态值（订单号 / 金额）。
 *
 * ⚠️ **顺序固定**：`data-act` 优先、`lay-event` 兜底（前者是本仓页内动作的通用锚点，
 * 后者是 layui 表格工具栏的锚点）；两个都没有时该段为空串。
 * ⚠️ 这个名字是**为了人读**，两个不同的控件**理论上有**撞串的可能（同 tag、同前两类、同动作、同前 12 字）
 * ⇒ 调用方**不能**拿它当 Set 去重（同一页里 20 行「指派」本来就该撞），要按**快照下标**去重。
 */
export function controlIdentity(el: Element): string {
  const cls = ((el.getAttribute('class') ?? '').trim().split(/\s+/).filter(Boolean)).slice(0, 2).join('.')
  const act = el.getAttribute('data-act') ?? el.getAttribute('lay-event') ?? ''
  const text = (el.textContent ?? '').trim().replace(/\s+/g, '').slice(0, 12)
  return `${el.tagName.toLowerCase()}.${cls}#${act}#${text}`
}

/* ——— 职位对拍（`05-role-action-audit.spec.ts`，T1 弹层扩张）：layui 弹层 ——— */

/**
 * 弹层**根**（layui 的 `layer` 组件把层挂在 `document.body` 下）。
 *
 * ⛔ **这就是本线要补的那个洞**：弹层 DOM **不在 `#app`（`CONTROL_SCOPE`）里** ⇒ 页内控件的枚举
 * 一个层内控件都看不到。本常量给「此刻有没有层」「层在哪儿」用，是层相关选择器的**唯一出处**。
 *
 * 版本出处（**读源码**，不是照记忆）：`src/frontend/public/layui/layui.js` 的 `vessel()` 里
 * 拼出来的根节点是 `<div class="layui-layer layui-layer-<type> …" id="layui-layer<times>"
 * type="<type>" times="<times>" …>`（该文件里 `u` 数组的第 0 项就是 `"layui-layer"`，
 * 根的 `id` 前缀 `"layui-layer"` 也在同一处）⇒ `.layui-layer` 同时是**类名**与 **id 前缀**，
 * 这里用类名（不依赖自增序号）。
 */
export const LAYER_ROOT = '.layui-layer'

/**
 * 弹层**标题**（`vessel()` 里那个 `<div class="layui-layer-title">…</div>`；`layer.open({title:…})`
 * 的文本落在这里）。本线的 `surface` 归属取它的文本，取不到就退成 `layer:untitled`。
 *
 * ⚠️ **取不到是常态而不是异常**：`layer.msg` / `layer.load` 这类层没有标题栏
 * （`vessel()` 里 `a.title` 为空串时不渲染这个节点）⇒ 退化成 `layer:untitled` 必须**安静**，
 * 不能抛（否则一页里几十个成功提示就把审计弄红）。
 */
export const LAYER_TITLE = '.layui-layer-title'

/**
 * 弹层**关闭按钮**（`vessel()` 的 `setwin` 里那颗
 * `<span class="layui-icon layui-icon-close layui-layer-close …">`）。
 *
 * ⛔ 本线的**关层**（spec §3 第 4 条）优先点它：它只关**这一层**，⛔ 不是 `layer.closeAll()`
 * （后者会把同文档里所有层都关掉，包括我们还没看过的）。
 *
 * 关闭按钮是 `<span class="layui-layer-close">`，这个类只在按钮上，不加到层根。
 */
export const LAYER_CLOSE = '.layui-layer-close'

/**
 * 弹层内容区（`vessel()` 里那个 `<div class="layui-layer-content">`）——层内控件的枚举作用域。
 *
 * ⚠️ 枚举范围取「**层根**」还是「层内容区」是有取舍的：取层根会把关闭按钮本身也算成一条控件
 * （它也是个可点的按钮），点它只会关层、不发请求；取内容区则把关闭按钮排除在外。本线取**层根**
 * （spec §3 第 2 条的原话是「在该层内用同一套候选选择器枚举控件」），代价是多驱动一条
 * 「点了只关层」的控件；好处是「层里有什么就点什么」这句话不需要额外的排除规则。
 * 本常量保留给调用方按需收窄（今天只有探针用）。
 */
export const LAYER_CONTENT = '.layui-layer-content'

/**
 * **`type:2` iframe 层的判据**（spec §3 第 6 条：它的内容在**另一个文档**里，本线看不到 ⇒ 跳过并计数）。
 *
 * ## 为什么判「类名 + 内容里有没有 `<iframe>`」两条
 * 读源码（`src/frontend/public/layui/layui.js` 的 `vessel()`）：`type:2` 时根节点的类名拼的是
 * `"layui-layer-" + d.type[a.type]`，而 `d.type = ["dialog","page","iframe","loading","tips"]`
 * ⇒ `type:2` ⇒ 根节点一定带 **`layui-layer-iframe`**（与 `skin` 无关，拼在 `skin` 之前）。
 * 同一次 `layer.open` 里，层内容被塞成
 * `'<iframe … class="layui-layer-load" frameborder="0" src="…"></iframe>'` ⇒ `.layui-layer-content > iframe`。
 * 两条**同时**认（任何一条命中就算）的理由：类名是「layui 自己声明的类型」，`iframe` 子节点是
 * 「我们自己看到的实体」——两者若哪天分叉（layui 换版本 / 本仓自己控制 DOM），宁可**多跳过一层
 * 并计数**，也不要把一个跨文档的层当成"层内没有控件"静默放过（spec §3 第 6 条点名的正是这个形态）。
 */
export const LAYER_IFRAME_MARK = '.layui-layer-iframe'
/** 层内容里的 `<iframe>` 探测（与 `LAYER_IFRAME_MARK` 同一条判据的另一半，见上）。 */
export const LAYER_CONTENT_IFRAME = `${LAYER_CONTENT} iframe`

/**
 * 审计给**每一层**打的临时标记属性（值 = 本线自己数的自增序号）。
 *
 * ## ⛔ 为什么不能拿 layui 的 `times` 去**定位**一层（T1 实测踩到，这是必须的）
 * `times` 有两个毛病，都是实测出来的：
 *  1. 按 `[times="N"]` 定位会命中**两个**元素（实测报错原文：
 *     `locator('[data-tt-audit-drive][data-tt-audit-drive="L0"]') resolved to 2 elements`）——
 *     同一个 `times` 值在文档里出现了两次。
 *  2. ⇒ 拿 `[times="N"]` 去 `locator.click()` / `locator.evaluate()` 会以
 *     **strict mode violation** 失败。后果不是崩，而是**每一次层内驱动都报"真实点击到不了它"**
 *     ——一个假象。
 * 所以：`times` 只用来判「哪一层是**这一次点击新开出来的**」（它单调递增，够用），
 * 而**定位**一律用本属性（每次枚举重新打，文档里唯一）。
 *
 * `type:1` 且内容是页内节点时，关层会把那个节点 unwrap 回文档，同一个标记值就会出现两次。
 * 所以定位用独立属性，每次枚举前 `clearAuditMarks()`。
 */
export const LAYER_AUDIT_ATTR = 'data-tt-audit-layer'

/**
 * 层内控件的 `surface` 归属前缀：`layer:<层标题>`；标题取不到时退成 `layer:untitled`。
 *
 * ⚠️ **这是报告键 `findings[].surface` 的取值来源**：页内控件用它所在页面的路径，层内控件用这个。
 * 标题里可能带动态值（订单号 / 账号名）⇒ 它**只用于人读的报告**，⛔ 不要拿它当 Set 去重
 * （`layersOpened` 是「按标题去重计数」，本来就接受这种粗粒度）。
 */
export const LAYER_SURFACE_PREFIX = 'layer:'
/** 层没有标题时的 `surface`（见 `LAYER_TITLE` 的注释：这是**常态**，不是异常）。 */
export const LAYER_SURFACE_UNTITLED = `${LAYER_SURFACE_PREFIX}untitled`

/* ——— T5 订单主干：录单页（`layui-island/pages/order-addorder.ts`）——— */

/**
 * 游戏那个原生 `<select>`。⛔ **它被 layui 设成 `display:none`**（`layui.css`：
 * `.layui-form select{display:none}`）⇒ `selectOption` 会因不可见而失败，只能点替身节点。
 * 保留这个常量是因为断言「哪一款游戏被选中了」仍要用它（`inputValue()` 读得到隐藏控件的值）。
 */
export const ADD_GAME_SELECT = '#gameName'

/**
 * 游戏的**替身选项**。
 *
 * ⚠️ 实测（T5 Step 1，真 DOM）：展开后 `#gameName` 的兄弟节点 `.layui-form-select` 里是
 * `<dl><dd lay-value="1" class="layui-this">E2E 测试游戏</dd></dl>`。
 * ⛔ **不能写成裸的 `.layui-form-select dd`**：同一页还有「时间要求」等下拉（实测那份 DOM 里
 * 近百个 `dd`，如 `0天`…`230天`）⇒ 裸选择器会命中一堆无关选项（`hasText` 虽然能过滤，
 * 但 `.first()` 落在哪个 `dl` 上取决于遍历顺序，是**隐式**的）。用 `+` 把范围钉在 `#gameName` 上。
 * ⚠️ 点之前必须**先展开**：点 `#gameName + .layui-form-select .layui-select-title`。
 */
export const ADD_GAME_PROXY_PREFIX = '#gameName + .layui-form-select'
export const ADD_PROJECT_TEXT = '#project'
export const ADD_PRICE = '#price'
/** 接单实收（`received_price`）——退款那一步的前件，见 spec 里 `RECEIVED_PRICE` 的注释。 */
export const ADD_RECEIVED = '#priceReceipts'
/** 粘贴解析的入口：**失焦即触发**（`order-addorder.ts:1547-1549`，没有独立按钮）。 */
export const ADD_CONTENT = '#dlcontent'
export const ADD_SUBMIT = 'button[lay-filter="savebtn"]'

/* ——— T5：订单管理页（`layui-island/pages/order-orderlist.ts`）——— */

export const ORDER_ROWS = '.layui-table-body tr'
/**
 * 列表筛选：关键字输入框 → `params.keyword`（`order-orderlist.ts:874`），
 * 后端按**订单号**命中（`search_orders` 的 `keyword`）。
 * ⛔ **一律用订单号筛出那一行**，不靠「新单在第一页」——`Order` **有** `Meta.ordering =
 * ["-created_at", "-id"]`（`apps/business/models.py:540`，即最新在前；⚠️ 旧注说"没有 ordering"是
 * **错的**，评审修复波第 4 条就地订正），但同一租户会被反复跑、订单越积越多 ⇒
 * 拿「第一行」当"本轮那一单"仍会取到**别轮的单**。
 */
export const ORDER_SEARCH_INPUT = '#key1'
export const ORDER_SEARCH_BUTTON = '[data-tb="seachBtnOk"]'
/** 订单号单元格（`div.orderLine`，id 形如 `order_<20位单号>`）。 */
export const ORDER_NO_CELL = 'div.orderLine'
export const TB_REFRESH = '[data-tb="refresh"]'
export const TB_CHANGE_STATUS = '[data-tb="changeStaBtn"]'
export const TB_SETTLE_STATUS = '[data-tb="setOrderJSBtn"]'
export const ROW_THROW = 'a.throwEdit'
/** 行内「接手人」那一格：**没人接时也在**（渲染成「指派打手」按钮，`takerRow` 两支都带 `jsr`）。 */
export const ROW_ASSIGN = 'a.jsr'
export const ROW_PRICE = 'a.editOrderPrice'
export const ROW_REFUND = 'a.refundFee'
/** 行内状态徽标 → 改状态弹层。 */
export const ROW_STATUS_BADGE = '.setdlsta'

/* ——— T5：弹层 ——— */

export const STATUS_DIALOG = '#tt-orderlist-transition'
export const DIALOG_STATUS_RADIOS = '#tt-orderlist-transition input[name="status"]'
/** layui 给 radio 渲染的替身（文本取原生 input 的 `title`，实测 `.layui-form-radio > div` 里）。 */
export const DIALOG_STATUS_RADIO_PROXY = '#tt-orderlist-transition .layui-form-radio'
export const DIALOG_STATUS_SAVE = '#tt-orderlist-transition [data-act="status-save"]'
/**
 * 抛单弹层：证据件里那个 `.setprice` 容器（`public/replica/order-orderlist.html:371`）。
 *
 * ⚠️ 实测：layui 开层时把它**搬进 `.layui-layer-content`**（`... > .setprice.layui-layer-wrap`），
 * 所以「层里有没有它」不能当判据；它就是弹层内容本身。
 */
export const DIALOG_THROW_PANEL = '.setprice'
export const DIALOG_THROW_PRICE = '.setprice #setprice'
export const DIALOG_THROW_SAVE = '[lay-filter="saveSettpricebtn"]'
export const ASSIGN_DIALOG = '#tt-order-assign'
/**
 * 指派弹层的**候选人复选框**。
 * 每项是 `input[type=checkbox][name="userList"]`，`title` = 显示名、`value` = 账号 id。
 * 原生控件被 layui 隐藏，可见替身是旁边的 `.layui-form-checkbox`。
 */
export const DIALOG_ASSIGN_USER_LIST = 'input[name="userList"]'
export const DIALOG_ASSIGN_SAVE = '[data-act="assign-save"]'
/** 「清除接单人」：只在该单已有接单人时才渲染。 */
export const DIALOG_ASSIGN_CLEAR = '[data-act="assign-clear"]'
export const DIALOG_REFUND_AMOUNT = '.setrefundOrderLog #refundFee'
export const DIALOG_REFUND_REASON = '.setrefundOrderLog #refundRemark'
export const DIALOG_REFUND_SAVE = '.saveReFundOrderBtn'
export const DIALOG_SETTLE_RADIOS = '.setDiv input[name="jsst"]'
export const DIALOG_SETTLE_SAVE = '[lay-filter="saveSettbtn"]'
/** `layer.prompt` 的输入框与第一个按钮（实测：`.layui-layer-prompt .layui-layer-input` 就是它）。 */
export const PROMPT_INPUT = '.layui-layer-prompt .layui-layer-input'
export const PROMPT_OK = '.layui-layer-prompt .layui-layer-btn0'
/** `layer.msg` / `layer.confirm` 的通用锚点。 */
export const LAYER_MSG = '.layui-layer-msg'
/** `layer.confirm` 的「确定」是第一个按钮；限定在 dialog 层内，避免与 prompt 层的按钮混。 */
export const LAYER_CONFIRM_OK = '.layui-layer-dialog .layui-layer-btn0'
/** 表的行按**订单号单元格**指认——`div.orderLine` 的 id 就是 `order_<20位单号>`。 */
export const ROW_BY_ORDER_NO = (orderNo: string) => `tr:has(#order_${orderNo})`

/* ——— T5：内部抢单大厅（`order-insiderecorderlist.ts`，路由 `/orders/inside-hall`）——— */

/** 抢单按钮：`<a class="… recThisOrder" data-id="<单号>">接手此单</a>`。 */
export const HALL_CLAIM_BY_ORDER_NO = (orderNo: string) => `a.recThisOrder[data-id="${orderNo}"]`

/* ——— T5：绩效管理页（`emp-emplist.ts`，路由 `/agents/performance`）——— */

/**
 * 绩效页自带的**筛选栏**：「搜索1」输入框 + 「搜索」按钮。
 *
 * `#key1` 进 `params.keyword`，「搜索」是 `form.on('submit(seachBtn)')`，
 * 点了就回第 1 页并按当前控件重新取数。
 * 后端 `keyword` 是订单号、角色名、来源单号三个面的并集，所以按订单号筛得动，
 * 本用例不依赖「新行恰好在第一页」。三个面各自命中的判据在
 * `test_p4fu_performance_reads.FilterTests`。
 * ⚠️ 同一张表单里的日期区间（`#sDate` / `#eDate`，证据件有默认值）会一起生效；本用例录单与
 * 结账都在同一天，落在默认区间内。
 */
export const PERF_SEARCH_INPUT = '#key1'
export const PERF_SEARCH_BUTTON = 'button[lay-filter="seachBtn"]'
/** 「结账状态」列那颗标签：未结账 ⇒ 点击结账；已结账 ⇒ 只弹提示。 */
export const PERF_SETTLE_TAG = '.setSett'
/**
 * 按**行内文本**（订单号）指认那一行。行里「关联订单」列渲染的就是系统单号（`orderIdTemplet`），
 * 所以这条文本匹配命中的是**那一格**。
 *
 * ⚠️ **用之前先用 `PERF_SEARCH_INPUT` + `PERF_SEARCH_BUTTON` 把列表筛到这一笔**
 * （本文件的 T5 用例就是这么用的）：绩效列表是分页的，不筛就等于假设「新行在第一页」，
 * 而本线每跑一次就多一笔单 ⇒ 迟早掉出第一页（假红）。
 * ⚠️ 一笔订单有**两条**绩效行（「销售提成」＋「打单工资」），两行的订单号文本相同
 * ⇒ 调用方必须再按要走的那一行收窄（T5 用 `filter({ hasText: TAKER_NAME })` 取代练员那行）。
 */
export const PERF_ROW_BY_ORDER_NO = (orderNo: string) => `tr:has-text("${orderNo}")`

/* ——— 我的资料（`/accounts/profile`，island key `user-myinfo`）——— */

/**
 * 顶栏账号区的两个下拉项。
 *
 * ⚠️ **anchor 是 `data-test`、不是文字**：菜单项的文字以后可能改文案，而 `data-test` 是
 * `layouts/AppTopbar.vue:65-67` 与 `e2e` 之间的**显式契约**（改文案不该打挂 E2E）。
 * ⚠️ 这一项在 EP 的 `el-dropdown` 里、**菜单被 teleport 到 `document.body`** ⇒
 * 选择器必须**相对顶层 `page`** 取（⛔ 不是 `frameLocator`，island iframe 里没有它）。
 */
export const PROFILE_LINK = '[data-test="my-profile"]'
export const PASSWORD_LINK = '[data-test="change-password"]'
/** island 文档内的锚点（一律 `page.frameLocator(ISLAND_IFRAME)` 之后再取）。 */
export const PROFILE_MOBILE_INPUT = 'input[name="moblie"]'
export const PROFILE_SAVE_BUTTON = 'button[lay-filter="savebtn"]'
export const PROFILE_PWD_OLD = 'input[name="ypwd"]'
export const PROFILE_PWD_NEW = 'input[name="newPwd"]'
export const PROFILE_PWD_CONFIRM = 'input[name="renewPwd"]'
export const PROFILE_PWD_SUBMIT = 'button[lay-filter="savePwdBtn"]'
