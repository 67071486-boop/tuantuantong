/**
 * `05` 职位对拍：**页面清单两路来源 + 两条判据**（role-promise-audit spec §3.1 的 N2）
 * ＋ **控件驱动：拦断写请求 + 403 记账 + N1 正对照 + N3 覆盖下界**（spec §3.2 / §3.3 / §3.4，T3）
 * ＋ **T7 的提速与盲区收窄**（真因、修法与代价见文末的 T7 段）。
 *
 * 本文件做两件事：
 *  - **T2（页面层）**：对每个职位算两列清单并**分别**断言——
 *    `reachable(R)`（逐条导航候选路由、**没被路由守卫弹回 `/`** 的留下 = 守卫的实际放行面）与
 *    `menuVisible(R)`（在 `/` 上读**渲染出来的**侧栏菜单条目，把标签静态映射成路径 = 菜单的实际可见面；
 *    可见性的**唯一出处是 DOM**，`AGENTS.md` §5C）。
 *  - **T3（控件层）**：对 `reachable` 里**每一条页面**上的**每一条页内控件**，驱动一次真实点击，
 *    把这次点击触发的 `/api/**` **GET 403** 记进 `findings`（spec §2 的判据本体：
 *    **职位 R + 页面 P + 控件 C ⇒ 一个 403**）；写请求一律**拦断**（spec §3.3，绝不让点击落库）。
 *
 * ## T2 的两条判据（T2 修复轮按控制方裁定，从「集合相等」改判据方向）
 *  1. **`onlyVisible(R)` 必须为空**：菜单里看得见、路由守卫却放行不了——**会伤到用户**的方向
 *     （点了菜单静默弹回首页）。
 *  2. **`onlyReachable(R)` ⊆ `HIDDEN_MENU_PATHS`**：守卫放行但菜单看不见的，**只允许**是
 *     `menu.ts` 里**有意隐藏**的那些（冻结 IA：入口不上菜单、但页面仍接线）。
 *     ⛔ 不许把 T2 报的那 21 条写死成白名单——那样「**新接了路由却忘了加菜单入口**」就检不出来。
 *
 * ## T3 的两条判据（spec §3.4）
 *  - **N1 正对照**：审计开跑前在浏览器里打一个**必然 403** 的假端点，断言它**出现在**观测里
 *    （否则整张网恒绿）。做法是 `page.route()` + `page.evaluate(fetch)`：请求**根本不出网**，
 *    所以「每个职位都必然 403」是**确定性**的（⛔ 拿真实端点当正对照不成立：`admin` 是超级对照，
 *    它的节点判据短路成全集、真实端点不会 403）。
 *  - **N3 覆盖下界**：**有候选控件的页面，至少要有一次「点过」**（成功或失败都算点过）；
 *    一条都没点 ⇒ **红**（那意味着控件枚举器静默失效了）。⛔ 「没有控件就不查」这种**静默**放行
 *    被显式堵住：没有候选控件的页面进 `pagesWithNoControls`，判据按**两个集合的差**算。
 *  - **N3′ 全局候选数下界**（T5 收口时补的洞）：N3 只在 `drivableTotal > 0` 的页面上成立，
 *    所以**选择器整体失效**（每页候选都变 0）时它反而更绿 ⇒ 另按**每个职位**钉住枚举到的候选
 *    总数（`CANDIDATE_FLOOR`，外部常量、非自指）。判据见用例第 ③ 段。
 *
 * ⛔ **findings 非空不得判红**（T4 才把发现冻成基线）：本文件只**记账**
 * （spec §4 的口径：已知缺陷摆在那里，若"有 403 就报红"，这条线从第一天就是红的、然后没人看）。
 *
 * ## T7（让这张网跑得动、把盲区收窄）——四件事，每条都配了实测依据
 * T3 的原始读数：整轮 **43.5 分钟**，其中 cs 那轮 `/orders` **一页 917s**；而 cs 有 **106 条**
 * 点不动，80 条集中在 `/orders`。T7 实测出的真因与修法：
 *  1. **「等候选渲染」原本是空转**：`waitForCandidates()` 的谓词数的是**审计自己打的标记**
 *     （枚举之前恒为 0）⇒ 每页无条件睡满 5s。改成数**候选选择器**并等"连续几拍稳定 + 最短观察窗"
 *     （实测 `/orders` 约 1.2s 稳定在 235 条）。
 *  2. **`/orders` 那 80 条点不动的真因是 layui 固定列克隆体遮挡**：Playwright 报错逐字为
 *     `<i class="layui-icon layui-icon-edit"></i> from <div class="layui-table-fixed layui-table-fixed-r">…</div>
 *     subtree intercepts pointer events`；主表那一份的矩形在 `left ≈ 1505–1595`（iframe 视口 1204 宽），
 *     人真正点到的是 `.layui-table-fixed-r` 里的克隆。⇒ 驱动前 `chooseTarget()` 按
 *     「枚举时可命中 > 横向在视口内」挑目标，**改点可命中那一份**。
 *  3. **同身份串去重到 `DEDUPE_N` 条**（13 行「指派打手」原本逐行点 13 次、走同一条代码路径）。
 *     ⛔ 代价：**同一控件在不同行上可能行为不同**，故可能漏掉"只在第 N 行才出现的 403"——
 *     所有没驱动的重复元素**逐条**记进 `deduped` / 计入 `dedupedDuplicates`，不是静默少点。
 *  4. **单条点击硬预算从 5s 降到 `CLICK_TIMEOUT_MS`（2s）**，且"不可见 / 点不到"的走**快速失败**
 *     （`isVisible()` 短路 + 有界 `elementFromPoint` 轮询），不再等 Playwright 的动作超时。
 *
 * ⚠️ **判据没有被放宽**：`findings` 的记账口径、N1 / N3 两条钉子都留着；N3 的下界改成
 * 「点过 + 去重 + 点不到的副本 ≥ 枚举出来的可驱动总数」——去重是**有意**少点，但**必须记账**。
 *
 * ## 两处纪律（都不是装饰）
 *  - 候选路由清单与 `04-smoke-all-pages.spec.ts` **同源**：都 import `fixtures/routes.ts`，
 *    ⛔ 不许在这里再抄一份抽取逻辑（`E-0021`：同一事实两套判据必然漂移，而漂移只表现为静默少测）。
 *  - 报告**每行一个文件 + `afterAll` 聚合**（照 `04` 的 `ROWS_DIR` 写法）：Playwright 在用例失败后
 *    会**重启 worker**、模块级数组清零 ⇒ 最该留痕的那一行反而丢。
 *    ⚠️ T3 实测补充：**失败后 Playwright 会重启 worker ⇒ `afterAll` 会跑两次**，中途那份报告可能是
 *    **残缺的**（还会打「缺 N 行」），**最终那份才是全的**。聚合逻辑不用改，但看到「缺行」警告时
 *    先看它是不是这个原因。
 *
 * ## 写模式（`docs/specs/2026-09-25-write-mode-audit-design.md` §3–§6；T2 本线新增）
 * 默认（读模式）**逐字不变**：写请求一律 `abort()`、连只读租户 `tt_e2e`、跑前不重置。
 * **只有**运行器注入 `TT_E2E_TARGET=write`（它由 `TT_E2E_WRITE=1` 推出，见 `playwright.config.ts`）
 * 时才：
 *  - **放行**驱动期的非 GET `/api/**` 请求（不 `abort()`），并把**探针决定放行**的条数记进
 *    `writesPerformed`；⚠️ 它**不代表服务端收到 / 写成功**（评审修复波 D）；
 *  - **每个职位开跑前重置一次**一次性写租户（`resetWriteTenant()`，起子进程跑 `reset_e2e_write`）；
 *  - 报告里多四个键 `mode` / `writesPerformed` / `nonGetApiResponses` / `resets`
 *    （⛔ 既有键一个不改、⛔ 不删不改名），另加一个
 *    **只记录、不判红**的 `writeIssues`（写模式下新出现的非 GET 403 / 5xx，逐条带归属）。
 * ⛔ **fail-closed**：`TT_E2E_TARGET` 不是**逐字**的 `write` ⇒ 一律保持拦断（W3 就是它的判据）。
 * ⭐ **W2 / W3 的判据是 `nonGetApiResponses`**（评审修复波 A）：它由 `page.on('response')` 观测
 *    「**真的收到响应的**非 GET `/api/**` 请求」（排除登录），**与 `WRITE_MODE` 分支无关**。
 *    原来 W3 用的 `writesPerformed` 只在写模式分支里 `push` ⇒ 读模式下结构性恒 0、断言恒真
 *    （把 `abort()` 换成 `fallback()` 也照样绿）。现在：读模式必须 0（W3）、写模式必须 > 0（W2）。
 * ⚠️ **写模式的覆盖下界是另一套**（`CANDIDATE_FLOOR_WRITE` / `LAYER_CONTROL_FLOOR_WRITE` /
 *    `INSTANTIATED_CANDIDATE_FLOOR_WRITE`）：一次性写租户是**每次重建出来的夹具态**
 *    （`seed_e2e` **不**造订单，但 `reset_e2e_write` 之后会调 `seed_e2e_write_orders` 补 4 笔），
 *    与读模式那份累积了订单的租户**不是同一份数据基线**
 *    ⇒ 拿读模式的下界去套写模式会**假红**（`CANDIDATE_FLOOR` 的注释早已登记过这个触发条件）。
 * ⚠️ 残差（如实写下来）：写租户自 T2b 起有 **4 笔确定性订单**（`seed_e2e_write_orders`）⇒
 *    `/orders` 的行内控件（「指派打手」弹层 / 实例化详情页）**进得了网**；但夹具**只翻 1 页**
 *    ⇒ 读模式那种"分页第 N 页才出现的控件"在写模式下仍覆盖不到（取舍，见 spec §7）。
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { expect, test, type Locator, type Page, type Response } from '@playwright/test'

import {
  assertWriteTenantConstantsMatchPython,
  FRONTEND_DIR,
  FRONTEND_URL,
  readPythonWriteTenantConstants,
  SHOTS_DIR,
  WRITE_TENANT_DB,
  WRITE_TENANT_DOMAIN,
  WRITE_TENANT_ENV_REL,
  WRITE_TENANT_SCHEMA,
} from '../config.mjs'
import { resetWriteTenant } from '../lib/tenant.mjs'
import { ACCOUNTS, switchAccount, type RoleName } from '../fixtures/auth'
import {
  diffBaseline,
  ROLE_AUDIT_BASELINE,
  validateBaseline,
  type RoleAuditBaselineEntry,
} from '../fixtures/role-audit-baseline'
import { CANDIDATES, HIDDEN_MENU_PATHS, PARAMETERIZED_ROUTES, ROUTE_PATH_BY_NAME, settledPath } from '../fixtures/routes'
import {
  AUDIT_API_PREFIX,
  AUDIT_FORBIDDEN,
  AUDIT_LOGIN_PATH,
  AUDIT_PROBE_URL,
  CONTROL_CANDIDATE_SELECTOR,
  CONTROL_SCOPE,
  CONTROL_SELECTORS,
  CONTROL_DRIVE_ATTR,
  CONTROL_DRIVE_SELECTOR,
  controlIdentity,
  ISLAND_IFRAME,
  LAYER_AUDIT_ATTR,
  LAYER_CLOSE,
  LAYER_CONTENT,
  LAYER_CONTENT_IFRAME,
  LAYER_IFRAME_MARK,
  LAYER_ROOT,
  LAYER_SURFACE_PREFIX,
  LAYER_SURFACE_UNTITLED,
  LAYER_TITLE,
  SIDEBAR_ITEM_LABEL_SPAN,
  SIDEBAR_MENU,
  SIDEBAR_SUB_MENU,
  SIDEBAR_SUB_MENU_ITEM,
  SIDEBAR_SUB_MENU_TITLE,
} from '../fixtures/selectors'
import { MENU_GROUPS, TOP_LEVEL_ITEMS } from '../../src/config/menu'

/* ——— 目标模式（spec §3 的第 2 道闸门；fail-closed） ——— */

/**
 * 本轮目标，由运行器注入（`run.mjs` 设 `TT_E2E_TARGET`，`playwright.config.ts` 还会按
 * `TT_E2E_WRITE` 复核一次并拒绝两者不一致的运行）。
 */
const AUDIT_TARGET: string | undefined = process.env.TT_E2E_TARGET

/**
 * 写模式开关：**只有逐字等于 `'write'` 才算写模式**。
 *
 * ⛔ **fail-closed**：缺省 / `'Write'` / `'WRITE'` / 拼错 / 任何别的值 ⇒ **false** ⇒ 保持拦断。
 * 这条判据的防的是「对**未知租户**放行写」——本线最贵的一种错（spec §3）。
 * 它的反面（读模式下不许有非 GET 请求**真的收到响应**）由**W3** 守（见主用例；
 * ⚠️ 判据是模式无关的 `nonGetApiResponses`，⛔ 不是只在写模式分支里记账的 `writesPerformed`）。
 */
const WRITE_MODE: boolean = AUDIT_TARGET === 'write'

/**
 * 职位矩阵：**系统管理员（对照）+ 四个业务职位 + 一个自定义最小权限角色**。
 * ⛔ `nobody`（`nobody001`）**不进矩阵**：它不进任何组、没有任何节点、菜单几乎为空，
 *    对本线（「看得见 vs 通不通」）没有判别力——加进来只会多花两分钟。
 *
 * ⚠️ `minrole` 是本线（minimal-role-audit，spec §2）新增的**自定义 RBAC 角色**（不是内置组预设），
 *    节点集显式枚举、只给 `order.list`。它补的正是审计此前缺的那个维度：内置预设的节点按**栏目批发**，
 *    而「**看得见但用不了**」这类缺陷恰好集中在**某些节点没有**的自定义角色上。
 *
 * ⚠️ `gamescope` 是本线（game-scope-audit，spec §2）新增的**单游戏范围角色**：节点集与内置「客服」
 *    **同源**（`seed_e2e._seed_gamescope` 调 `apps/rbac/seed.py::builtin_role_nodes("客服")`），
 *    但 `StaffProfile.games` **只挂第 1 款**游戏。它补的是审计此前**完全没测过**的一条权限轴
 *    （`apps/business/game_scope.py::games_of` 按账号的游戏集收窄）：在这条线之前 E2E 租户只有 1 款
 *    游戏，「只给一款」与「给全部」在库里是同一种状态。
 *    ⚠️ **实测（2026-09-26 整轮）它与 `cs` 的读数逐项一致**（reachable/menuVisible 13 = 13、
 *    候选 317 = 317、层内 6 = 6、实例化页 4 = 4、findings 都 0）——⛔ 别把"它看到的行数更少"当成
 *    这条轴今天被验证过的证据：`cs001` 本身也只挂第 1 款、而且租户里的订单**全在第 1 款上**。
 *    理由与代价见 `docs/specs/2026-09-26-game-scope-audit-design.md` §7。
 */
const ROLE_MATRIX: { role: RoleName; label: string }[] = [
  { role: 'admin', label: '系统管理员' },
  { role: 'cs', label: '客服' },
  { role: 'supervisor', label: '客服主管' },
  { role: 'taker', label: '代练员' },
  { role: 'dev', label: '系统开发员' },
  { role: 'minrole', label: '最小权限' },
  { role: 'gamescope', label: '客服（单游戏）' },
]

/**
 * 每个职位**枚举到的候选控件总数**的覆盖下界（spec §3.4 的 N3′）。
 *
 * ## 它补的是什么洞（T3 遗留的真洞）
 * N3 那条判据是「**有候选控件的页面至少点过一次**」。它挡不住**枚举器整个坏掉**：
 * 候选选择器一旦失效，每一页都变成"没有候选控件"⇒ N3 的过滤条件（`drivableTotal > 0`）
 * 一条都不命中，判据**反而更绿**。所以必须另有一条**按职位**钉住枚举规模的下界。
 *
 * ## 数字的出处（⛔ 是**实测**，不是从本文件自己算出来的）
 * T7 收口整轮 `node e2e/run.mjs --grep "职位"` 的报告（`.tools/e2e/role-audit.json`）：
 * `admin` 765 / `cs` 313 / `supervisor` 313 / `taker` 113 / `dev` 49，合计 **1553** 条。
 * 下界取实测值的 **50%**（向下取整）：`admin` 380 / `cs` 156 / `supervisor` 156 /
 * `taker` 56 / `dev` 24。
 *
 * ## 余量理由（⛔ 订正：50% **不是**为"数据波动"留的余量）
 *  - **它防的是什么**：控件枚举器**整体失效**那种形态（候选选择器改名 / island 作用域变了 ⇒
 *    每页候选塌到 0 或接近 0）。50% 是相对这个失效形态取的粗界——它比"实测值的 99%"经得住
 *    页面结构小改，又远高于"塌到 0"。⛔ 下界**不许**写成能自我派生的量（例如"上一次枚举到的
 *    条数"）——那是自指断言、恒真（`E-0024`）。
 *  - ⚠️ **它防不了"订单变少"**（原注释写「向下留余量是防另一侧的波动（租户被重建 / 订单变少）」，
 *    **不成立**，评审按实测报告逐页读数订正）：候选数**高度集中在数据页**上——
 *    `cs` / `supervisor` 的 `/orders` = **235 / 313（75%）**、`taker` 的 `/orders/mine` =
 *    **69 / 113（61%）**；而 `run.mjs` 只灌**夹具**（游戏 / 分类 / 项目 / 账号）、
 *    **不建订单**（建单的是 `02-golden-path`，它**不在** `--grep "职位"` 的执行面里）。
 *  - ⇒ **触发条件 = E2E 租户被重建 / 新环境首跑 / 有人清库**：那种租户上直接跑这条线，
 *    `cs` / `supervisor` 大约只剩 **95 条 < 156**、`taker` 约 **54 条 < 56**
 *    ⇒ **三个职位假红**（红的是"数据没了"，⛔ 不是枚举器坏了）。这是**已知假红触发条件**，
 *    已逐字登记进 spec **§11**；真要消除它，得让这条线**自己建订单**（或把下界按"非数据页"
 *    重算），⛔ 不是把下界调低（调低就挡不住枚举器整体失效了）。
 *  - 候选数**塌到 0**（选择器整体失效的形态）时，50% 的下界照样响亮变红 ⇒ 判别力在。
 *
 * 本键只数页内（`#app`）候选。层内候选在 `LAYER_CONTROL_FLOOR`。
 */
const CANDIDATE_FLOOR: Record<string, number> = {
  admin: 380,
  cs: 156,
  supervisor: 156,
  taker: 56,
  dev: 24,
  // `minrole` 只给 `order.list`，抛单那两颗不渲染。实测 `/` 14 + `/orders` 101 = 115，
  // 50% 向下取整 = 57。只降下界、不填 0：候选塌到 0 时 57 照样红。
  minrole: 57,
  // 本线（game-scope-audit）实测：`gamescope` 整轮候选 **317**（可驱动 305；13 条可达路由 +
  // 1 条实例化详情页）⇒ 50% 向下取整 = **158**。取数命令 `node e2e/run.mjs --grep "职位对拍 gamescope"`
  // （2026-09-26，只读租户当轮数据）。⚠️ 它与 `cs` 同形是**设计**（节点集照抄客服、游戏范围与
  // `cs001` 相同：都只挂第 1 款）⇒ 两个职位的读数一致；⛔ 本值仍只按**这个角色自己**的实测取，
  // 不抄 `cs` 的 156（租户数据会累积、读数会漂）。
  gamescope: 158,
}

/**
 * 每个职位**实例化详情页**上枚举到的候选控件总数的下界（spec §5 的 **D2** 的后半）。
 *
 * ## 它补的是什么洞（D2 的正题）
 * spec §5 的 D2 是「实例化页面也要计入候选下界」——`CANDIDATE_FLOOR` 那一枚**只看整轮总数**，
 * 于是「把详情页加进清单、但**驱动器根本没跑它**」会**静默通过**（总数只少那一页的量，
 * 而那一页在总数里占的比例小到被 50% 的余量吃掉）。本枚钉子把实例化页那一份**单独钉住**：
 * 它的候选数塌到 0（没渲染 / 没枚举 / 没进 `pageResults`）就当场红。
 *
 * ## 数字的出处（⛔ **实测**，不是从本文件自己算出来的）
 * 2026-09-25 本线 T1 的逐条实测（命令 `node e2e/run.mjs --grep "职位对拍 <职位>"`，
 * 读数是运行输出里 `🧩 实例化页读数` 那一行）：
 *  - `admin` **6**（`/orders/<20 位>` **4** + `/system-users/1/edit` **2**）；
 *  - `cs` **4**（`/orders/<20 位>`；同一条命令连跑两次都是 **4**，稳定）；
 *  - `supervisor` 与 `cs` **同形**（同一份菜单 / 同一个父页 `/orders`）⇒ 取 **4**，
 *    ⚠️ **未单独实测**（诚实登记：它是从 `cs` 的同形推来的，⛔ 别把它当实测读数引用）。
 * 下界取实测值的 **50%**（向下取整）：`admin` **3** / `cs` **2** / `supervisor` **2**。
 * 与 `CANDIDATE_FLOOR` 同一套口径（理由与余量见那里的注释：防的是**枚举器整体失效**，
 * ⛔ 不是"数据波动"；⛔ 也不许写成能从本次读数派生的量）。
 *
 * ## ⚠️ 为什么 `taker` / `dev` 登记成 `-1`
 * 它们的菜单里**到不了** `/orders` 与 `/system-users` ⇒ 这两条参数化路由对它们
 * **根本不进清单**（`detailOf` 不在它们的 `reachable` 里，spec §3）⇒ 本枚钉子对它们**无可判别**，
 * 登记成 `-1`（**显式空位**，与 `LAYER_CONTROL_FLOOR` 的哨兵同一套语义）。
 * ⚠️ **订正（评审修复波第 9 条）**：原来这里写"此时判据变成「这一遍实例化页候选数必须为 0」"
 * ——**措辞过强**：候选数为 0 单独**不**等于"没有实例化页"（一个解析出来了、但页面上 0 条候选控件的
 * 实例化页会让它照样绿）。现在哨兵是**两半**：① `row.parameterized` 必须为空（真的没解析出实例化页）；
 * ② 候选数必须为 0。两半都在用例里判。
 *
 * ⭐ **订正（2026-09-26，评审裁定 a——最小权限角色线）——上面那半①的判据已换**：
 * ① 从「`row.parameterized` 必须为空」改成「**`visited` 的实例化页必须为 0**」（= `parameterized`
 * 减去被守卫弹回 `/` 的 `instancePageUnreachable`）。理由与代价：一个**合法的最小权限配置**可以
 * 只持 `order.list` 而不持 `order.view_detail` ⇒ 父页可达、实例也解析得出，但详情页被守卫弹回 `/`
 * ——那是**该有的行为**，⛔ 不是缺陷；旧判据会把它**误红**（本线 `minrole` 实测踩到）。
 * ② 仍然是「候选数必须为 0」（⛔ 不退化成空判据）。
 * 上面原句按本仓惯例保留，**以本段为现状**。
 */
const INSTANTIATED_CANDIDATE_FLOOR: Record<string, number> = {
  admin: 3,
  cs: 2,
  supervisor: 2,
  taker: -1,
  dev: -1,
  /**
   * `minrole`（本线 minimal-role-audit）：**`-1` 空位**——它**没有可访问的**实例化详情页。
   *
   * 实测（`node e2e/run.mjs --grep "职位对拍 minrole"`，2026-09-26）：`parameterized`=1 条
   * （父页 `/orders` 可达 ⇒ 用该职位自己的会话解析出了一笔订单），但 `/orders/:order_no` 的路由
   * 守卫要 `order.view_detail`，而本角色按 spec §2 只给 `order.list` ⇒ 导航被弹回 `/`
   * ⇒ 进 `instancePageUnreachable`（**记录、不判红**）、`visited` 0 条、候选 0。
   *
   * ⭐ **这正是评审裁定 a（2026-09-26）要的形态**：`order.list` 不含 `order.view_detail` 是
   * **合法的最小权限配置**，弹回 `/` 是**该有的行为**；所以 `-1` 哨兵的语义从
   * 「`parameterized` 必须为空」订正为「**visited 的实例化页必须为 0**」（见主用例里那两半断言）。
   * ⛔ 若将来给这个角色补了 `order.view_detail`（那是**另一个决策**，等于扩权），
   * 本哨兵会以「visited 不为 0」红 ⇒ 逼人按实测重取。
   */
  minrole: -1,
  // 本线（game-scope-audit）实测：`gamescope` 的实例化页候选 = **4**（1 条 `/orders/<20 位>`）。
  // 它与 `minrole` 的 `-1` **不是同一形态**：本角色持 `order.view_detail` ⇒ 详情页**可达**、
  // 实例化页真的枚举到 4 条控件 ⇒ 50% 向下取整 = **2**。
  gamescope: 2,
}

/**
 * `INSTANTIATED_CANDIDATE_FLOOR` 里的**空位**取值（`-1`）：本枚钉子对该职位**无可判别**，
 * 此时改为断言实例化页候选数为 0（登记过期就红）。
 * ⚠️ 与 `LAYER_CONTROL_FLOOR_VACUOUS` 同一套语义（⛔ 不是"下界 0"、也⛔ 不是"没登记"）。
 *
 * ⭐ **订正（2026-09-26，评审裁定 a）**：这里钉的「有没有实例化页」从 `row.parameterized` 改成了
 * **`visited` 的实例化页条数**（`parameterized` − `instancePageUnreachable`）——判据本体在
 * 主用例那两半断言里；理由见 `INSTANTIATED_CANDIDATE_FLOOR` 的追加段与 `AuditRow.instancePageUnreachable`。
 */
const INSTANTIATED_CANDIDATE_FLOOR_VACUOUS = -1

/**
 * 每个职位**层内**枚举到的**可驱动控件总数**的下界（spec §4 的 **L1** 后半句）。
 *
 * ## 它防的失效形态
 * 层内枚举器**静默失效**：`LAYER_ROOT` 选择器改名 / 时机太早（层还没渲染出来）/ 新层判定坏了
 * ⇒ `layerDrivableTotal` 塌到 0 或接近 0，而只看页内读数完全看不出来。
 *
 * ## 数字的出处（⛔ **实测**，不是从本文件自己算出来的）
 * ⚠️ **订正（I.1）：这张网的计数会随 E2E 租户累积而漂移，⛔ 别把某一轮当定值。**
 * 逐轮实测的 `layerDrivableTotal`：
 *  - T1 **首轮**整轮（`node e2e/run.mjs --grep "职位"`，**14 分 48 秒**）：`admin` **24** / `cs` **7** /
 *    `supervisor` **7** / `taker` **0** / `dev` **0**；
 *  - T1 **后续**整轮（同一条命令，测得三次 `admin` = **24 / 23 / 23**）：`admin` **23** / `cs` **6** /
 *    `supervisor` **6**；
 *  - T3 收口最终整轮（**14.4 分钟**）：同为 `admin` **23** / `cs` **6** / `supervisor` **6**。
 * ⇒ 本键取的是**下界**（"约 50%"），对首轮与后续轮**都**留了一倍余量：
 * `admin` **12** / `cs` **3** / `supervisor` **3**。⛔ 不要按任何**单轮**的读数去改这个下界
 * （那正是"把漂移当定值"）。
 *
 * ## 余量与它防不了什么（诚实写下来）
 *  - 层内控件**集中在少数页**上（首轮实测：`cs` / `supervisor` 的 7 条里 **6 条**来自 `/orders` 的
 *    「指派打手」弹层、1 条来自「设定抛单价」弹层；`admin` 的 24 条分布在 12 个层里）。
 *    ⇒ **E2E 租户被重建 / 首跑无数据**时 `/orders` 行数为 0 ⇒ 那几个弹层打不开 ⇒ 本下界会**假红**
 *    （红的是"数据没了"，⛔ 不是枚举器坏了）。这条与 `CANDIDATE_FLOOR` 的已知假红触发条件是**同一条**。
 *  - ⛔ **它挡不住"层内枚举器只认出一半的层"**：那种形态下 `admin` 从 23 掉到 12 仍在界内。
 *    所以 L2（逐层 `drivable>0 && clicked===0`）与"层内记账恒等式"两条必须一起在场。
 *
 * ## `-1`（`LAYER_CONTROL_FLOOR_VACUOUS`）的含义
 * `taker` / `dev` 实测**一条层内可驱动控件都没有**（它们的菜单里只有 `my_*` / `home` 那几页，
 * 实测 `taker` 只开出 `layer:信息`（0 条控件）、`dev` 一个层都没开）⇒ 本枚钉子对这两个职位
 * **无可判别**。
 * ⚠️ **订正（F）：本条判据只钉住 `layerDrivableTotal === 0`，⛔ 不是"一个层都没开出来"。**
 * 原注释与 spec §8.3 把它说成后者——**与实现不符**（`taker` 实测 `layersOpened=1`、却照样通过）：
 *  - **它覆盖到的**：层里出现了带 `layui-btn` 可驱动控件的层 ⇒ `layerDrivableTotal > 0` ⇒ 红；
 *  - ⛔ **它覆盖不到的**（今天全部潜伏）：新页面开出**纯 `closeBtn:0` 层**、**只有非 `layui-btn` 控件
 *    的层**、或**纯 iframe 层** ⇒ `layerDrivableTotal` 仍为 0、哨兵照样绿，而 `layersOpened` /
 *    `layersSkippedIframe` 已经变了。⇒ 这三个数**必须一起读**（有层而无 drivable 时要人工复核登记），
 *    这一条已如实登记进 spec §9 残差；⛔ 不许把它读成"没有会开层的页面"。
 *    运行期另有一条**不判红**的提示（见本文件里 `layerFloor === LAYER_CONTROL_FLOOR_VACUOUS` 那一段）。
 * ⛔ 这与"下界 0"**不是**一回事：写 0 会让"枚举器整体失效"在这两个职位上照样绿。
 */
const LAYER_CONTROL_FLOOR: Record<string, number> = {
  admin: 12,
  cs: 3,
  supervisor: 3,
  taker: -1,
  dev: -1,
  // `minrole` 开不出可驱动控件的层（抛单按钮有渲染门），所以是空位哨兵 -1。
  // 填 0 会让层内枚举器整体失效也绿。
  minrole: -1,
  // 本线（game-scope-audit）实测：`gamescope` 的 `layerDrivableTotal` = **6**（`/orders` 行内
  // 「指派打手」等弹层，开 2 个层；与 `cs` 同形）⇒ 50% 向下取整 = **3**。
  gamescope: 3,
}

/**
 * 空位 `-1`：这个职位没有会开出可驱动控件的层，断言改成 `layerDrivableTotal === 0`（登记过期就红）。
 * 钉的是层内可驱动控件数，不是层数：开出了层但里面没有 `layui-btn` 也会通过。
 * 它不是下界 0：填 0 会让层内枚举器整体失效也绿。
 * 也**不是**"没登记"（缺键才会以 `toBeDefined()` 响亮失败）。
 */
const LAYER_CONTROL_FLOOR_VACUOUS = -1

/* ——— 写模式的覆盖下界（spec §4 的「判据不变」指的是 403 判据；覆盖下界按**各自的数据基线**） ———

 * ## ⛔ 为什么必须另立一套（不是"为了好看调低下界"）
 * 读模式的下界是在**累积了订单**的只读租户上实测的（`CANDIDATE_FLOOR` 的注释写明了
 * 「触发条件 = E2E 租户被重建 / 新环境首跑 ⇒ 三个职位假红」）。而写模式的租户**每个职位跑前
 * 都被 `reset_e2e_write` 推倒重建**，重建后灌的是**确定性夹具**：`seed_e2e` 的配置 / 账号
 * ＋（T2b 起）`seed_e2e_write_orders` 的 **4 笔订单**。
 * 把读模式的下界套上去，红的是"数据没了"、⛔ 不是"枚举器坏了"——**已知假红**。
 * ⇒ 写模式的下界在**它自己那份数据基线**上实测后登记（重建让这份数据是**确定的**）。
 *
 * ## ⚠️ 口径：本套只在 `TT_E2E_TARGET=write` 时被消费
 * 读模式**一个字都不改**（仍用上面那三张读模式表）。⛔ 不许把某一轮的读数当定值抄进来
 * （`LAYER_CONTROL_FLOOR` 的注释记过这条），也不许写成能从本次读数派生的量（自指、恒真）。
 *
 * ## 数字的出处（⛔ **实测**，不是从本文件自己算出来的）
 * T2b 的写模式**测量轮**（`TT_E2E_WRITE=1 node e2e/run.mjs --grep "职位"`，2026-09-25，
 * **13 passed / 2 failed / 15.6 分钟**；报告 = `.tools/e2e/role-audit.json`）。
 * ⚠️ 那一轮的两条红**正是本表要重取的原因**：cs / supervisor 的层内与实例化页下界当时
 * 还登记着 T2 的 `-1`（"写租户没有订单 ⇒ 一律 0"），而订单夹具让它们真的开出了
 * 「指派打手」弹层、也真的解析出了 `/orders/<单号>` ⇒ 哨兵按设计喊"登记过期"。
 * 逐职位读数（**写租户夹具态**的口径）：
 *
 * | 职位 | 候选总数（本表） | 层内可驱动（层表） | 实例化页候选（实例表） | writesPerformed（W2 表） |
 * |---|---|---|---|---|
 * | admin | 606 | 23 | 6（`/orders/7777…004` ＋ `/system-users/1/edit`） | 7 |
 * | cs | 149 | 6 | 4（`/orders/7777…004`） | 2 |
 * | supervisor | 149 | 6 | 4（同上） | 2 |
 * | taker | 62 | 0 | 0（`/orders/mine` 有数据，但参数化路由的父页 `/orders` 对它不可达） | 3 |
 * | dev | 49 | 0 | 0（同上） | 1 |
 *
 * ⚠️ **这是「写租户夹具态」的口径**（4 笔确定性订单 + `seed_e2e` 的配置 / 账号），
 * ⛔ **不是**只读租户那套累积数据（读模式的 765 / 313 / … 是另一份基线，两套互不换算）。
 * 候选数 / 层内数 / 实例化页数三张表的下界取实测值的 **50%**（向下取整），与读模式
 * 同一套余量口径（理由见 `CANDIDATE_FLOOR`：防的是**枚举器整体失效**，⛔ 不是数据波动）。
 * W2 那张表的下界取 **1**（spec §6 的原文就是「> 0」；实测五职位都 ≥ 1 ⇒ 没有空位）。
 * ⚠️ T2 那一轮（**无订单**）的读数 535 / 100 / 100 / 53 / 49 与 17 / 0 / 0 / 0 / 0
 * 已被本轮的 606 / 149 / 149 / 62 / 49 与 23 / 6 / 6 / 0 / 0 取代：订单夹具把 admin 的候选
 * 抬到 606、把 cs / supervisor 的**层内与实例化页从 0 抬到 6 / 4**——那 6 条层内控件
 * 全部来自 `/orders` 行内「指派打手」弹层（逐层读数见报告 `layers` 的
 * `layer:将订单指派给接手人/代练员`）。
 */

/** 写模式：每个职位**枚举到的候选控件总数**的下界（口径同 `CANDIDATE_FLOOR`）。 */
const CANDIDATE_FLOOR_WRITE: Record<string, number> = {
  admin: 303,
  cs: 74,
  supervisor: 74,
  taker: 31,
  dev: 24,
  // `minrole` 写模式实测整轮候选 51，50% 向下取整 = 25。
  // 下界接近 0 等于空判据：枚举器坏掉也绿。
  minrole: 25,
  // 本线（game-scope-audit）**写模式实测**：`gamescope` 整轮候选 **149**（可驱动 137；写租户
  // 夹具态的 4 笔确定性订单 + 14 页）⇒ 50% 向下取整 = **74**。取数命令
  // `TT_E2E_WRITE=1 node e2e/run.mjs --grep "职位对拍 gamescope"`（2026-09-26，重建后夹具态）。
  // ⚠️ 与 `cs` 的 74 同值是**设计**（同节点集、同游戏范围），但本值取自**这个角色自己**那一轮。
  gamescope: 74,
}

/**
 * 写模式：每个职位**层内可驱动控件总数**的下界（口径同 `LAYER_CONTROL_FLOOR`）。
 *
 * ⛔ `-1` = **显式空位**：实测 taker / dev 在写租户上**一条层内可驱动控件都没有**
 * （它们的菜单里到不了会开层的订单页；`/orders/mine` 有数据但行内那格不出「指派打手」弹层）
 * ⇒ 此时判据变成「必须恰好为 0」，登记过期就红。
 * ⚠️ **T2b 起 cs / supervisor 不再是空位**：订单夹具让 `/orders` 行内的「指派打手」弹层
 * 真的开出来了 ⇒ 它们各有 **6** 条层内可驱动控件（各 2 个弹层 × 3 条）。
 */
const LAYER_CONTROL_FLOOR_WRITE: Record<string, number> = {
  admin: 11,
  cs: 3,
  supervisor: 3,
  taker: -1,
  dev: -1,
  // `minrole` 写模式开不出可驱动控件的层，空位哨兵 -1。填 0 会让层内枚举器失效也绿。
  minrole: -1,
  // 本线（game-scope-audit）**写模式实测**：`gamescope` 的 `layerDrivableTotal` = **6**
  // （`/orders` 行内「指派打手」等弹层，开 2 个层）⇒ 50% 向下取整 = **3**。
  gamescope: 3,
}

/**
 * 写模式：每个职位**实例化页候选数**的下界（口径同 `INSTANTIATED_CANDIDATE_FLOOR`）。
 *
 * ⛔ `-1` = **显式空位**：taker / dev 的菜单里到不了参数化路由的父页 `/orders` 与
 * `/system-users`（taker 有 `/orders/mine`，但那不是 `/orders/{order_no}` 的父页）
 * ⇒ 它们**解析不出实例化页** ⇒ 此时判据变成「实例化页必须为空**且**候选数为 0」。
 * ⚠️ **T2b 起 cs / supervisor 不再是空位**：写租户有了订单 ⇒ 它们各自的会话都能解析出
 * `/orders/77770000000000000004`，实例化页候选 **4** 条。
 */
const INSTANTIATED_CANDIDATE_FLOOR_WRITE: Record<string, number> = {
  admin: 3,
  cs: 2,
  supervisor: 2,
  taker: -1,
  dev: -1,
  // `minrole`（本线 minimal-role-audit）：**写模式实测**与读模式同因 —— 它只给 `order.list`，
  // 详情页 `/orders/:order_no` 要 `order.view_detail` ⇒ 被守卫弹回 `/`（进 `instancePageUnreachable`，
  // 记录、不判红）⇒ **visited 的实例化页 = 0、实例化页候选 = 0** ⇒ `-1` 空位（⛔ 不填 0）。
  minrole: -1,
  // 本线（game-scope-audit）**写模式实测**：`gamescope` 的实例化页候选 = **4**
  // （1 条 `/orders/77770000000000000004`，`order.view_detail` 在节点集里 ⇒ 详情页可达）
  // ⇒ 50% 向下取整 = **2**。
  gamescope: 2,
}

/**
 * **W2**（spec §6）：写模式下每个职位**探针决定放行**的非 GET `/api/**` 请求数的下界。
 *
 * ⛔ **语义是"探针决定放行"**（`performedWrites` 在路由处理器的放行分支里记账），
 * ⛔ **不是"服务端收到 / 写成功"**（评审修复波 D）——"真的收到响应"由 `nonGetApiResponses` 单独钉。
 *
 *  - `1` = 实测该职位在写模式下确实有写请求被放行（spec §6 的 W2 原文就是「> 0」）；
 *  - `-1`（`WRITES_PERFORMED_FLOOR_VACUOUS`）= 实测该职位在本夹具下**一个写请求都不发**
 *    （显式空位：此时判据变成「必须恰好为 0」，登记过期就红；`nonGetApiResponses` 同理取反）。
 *
 * ⛔ 它防的失效形态：**开关没接上**（`TT_E2E_TARGET` 没注入 / 写策略没换成放行）⇒ 白跑一轮
 * 还以为覆盖了写流程。⛔ 下界不许写成能从本次读数派生的量（自指、恒真，`E-0024`）。
 *
 * ## 为什么五个职位都是 `1`（不是"每个职位恰好 7/2/2/3/1"）
 * spec §6 的 W2 原文就是「`writesPerformed > 0`」⇒ 下界取 **1** 最贴近判据、也留足余量：
 * T2b 测量轮（写租户夹具态，有 4 笔订单）实测是 `admin` **7** / `cs` **2** / `supervisor` **2** /
 * `taker` **3** / `dev` **1**，五个职位**都**发过写（没有需要登记成 `-1` 的）。
 * ⚠️ T2 那一轮（无订单）是 6 / 1 / 1 / 1 / 1——订单夹具把「发单价 / 改状态 / 指派打手」那些
 * 行内控件补进了驱动面，所以这几个数只会**变大**，`1` 这个下界不动。
 * ⚠️ 它**不钉**"恰好几笔"：写请求的条数会随页面上的按钮数与点击顺序小幅波动，
 * 而 W2 要防的是"一笔都没有"。
 */
const WRITES_PERFORMED_FLOOR_WRITE: Record<string, number> = {
  admin: 1,
  cs: 1,
  supervisor: 1,
  taker: 1,
  dev: 1,
  // `minrole`（本线 minimal-role-audit）：**写模式实测** `writesPerformed=0` **且**
  // `nonGetApiResponses=0`、`writeIssues=[]`（它的页内入口经 5 道渲染门收口后没有任何控件会发写请求）
  // ⇒ 按本表语义登记 `-1`（判据变成「必须恰好为 0」，⛔ 不是"下界 0"、登记过期就红）。
  minrole: -1,
  // 本线（game-scope-audit）**写模式实测**：`gamescope` 的 `writesPerformed` = **1**、
  // `nonGetApiResponses` = **1**（`/orders` 行内有一颗控件真的发出了写请求）⇒ 按本表语义取 **1**
  // （spec §6 的原文就是「> 0」，⛔ 不是「下界 0」）。
  gamescope: 1,
}

/** `WRITES_PERFORMED_FLOOR_WRITE` 里的**空位**取值（`-1`）：该职位写模式下不发写请求。 */
const WRITES_PERFORMED_FLOOR_VACUOUS = -1

/* ——— 报告落点（`.tools/` 不进 git） ——— */

/**
 * 每个职位**一个文件**的行落点。⛔ 不是模块级数组——Playwright 在某条用例失败后会重启 worker，
 * 模块级数组会被清零，而「第一个失败的那个职位」恰恰是最该留痕的一行（`04` 的注释记过这条）。
 */
const ROWS_DIR = resolve(SHOTS_DIR, '..', 'role-audit-rows')
const REPORT_PATH = resolve(SHOTS_DIR, '..', 'role-audit.json')

/**
 * 领航文件：这次运行里**有没有实例化过参数化详情页**（spec §5 的 **D1** 全局那一半的载体）。
 *
 * ## ⛔ 为什么不能只用模块级变量（`instantiatedPageDrivenInRound`）
 * Playwright 在某条用例失败后会**重启 worker**，模块级变量随之清零（本文件 `ROWS_DIR` 的注释
 * 记的是同一件事，T1 执行期实测踩到）：重启后的那次 `afterAll` 会拿着一个**清零的**变量
 * 去判"整轮没驱动过实例化页" ⇒ **假红**（真正的失败已经由那条用例自己报了，这条红只会写错原因）。
 * ⇒ 用**文件**当载体：条目一旦写下，整轮之内（含重启）都还在；`e2e/run.mjs` 在每次整体运行前
 * 清 `role-audit-rows/` 那个目录，本文件与它同目录 ⇒ **每次整体运行天然是干净的**。
 *
 * ⚠️ 它的内容**不参与判据**（⛔ 不是"读了就算驱动过"）：判据只问"这个文件**在不在**"，
 * 而写它的那一行**紧跟在实例化成功之后**（见主用例里 `writeFileSync` 的位置）——
 * 即"真的解析出过至少一个实例"，与 D1 的措辞一致。
 */
const INSTANTIATED_SENTINEL = resolve(ROWS_DIR, 'instantiated.sentinel')

/**
 * 「本轮有职位**父页可达**」的领航文件（D1 全局那一半的**适用范围**判据；评审修复波第 1 条）。
 *
 * ⛔ **为什么需要它**：原来全局那一半只看 `roleOrder.length > 0`，于是
 * `--grep "职位对拍 taker"`（taker 的父页都不可达、一条实例**当然**解析不到）会被判红
 * ——**假红**（评审已实测复现）。现在判据改成「**本次跑过的职位里存在父页可达的职位**」
 * ⇒ 单跑 taker / dev 这类职位不再假红；而单职位的"父页可达却解析契约坏"由**该职位自己**那条
 * 契约断言守（见主用例 D1 ①）。
 * ⚠️ 与 `INSTANTIATED_SENTINEL` 同一套载体理由（文件、不是模块级变量）：worker 重启后
 * 模块级变量会清零，而文件在同一轮里一直在。
 */
const PARENT_REACHABLE_SENTINEL = resolve(ROWS_DIR, 'parent-reachable.sentinel')

/**
 * 「本轮出现过**解析契约坏**」的领航文件（D1 全局那一半的**红/不红分叉**；评审修复波第 3/7 条）。
 *
 * ⛔ 两分类（spec §2）在整轮层面的口径：全部失败都是 `noData` ⇒ **覆盖率与数据相关、不判红**；
 * 只要有一条 `contractBroken` ⇒ 红。单跑一个职位时，契约坏由**该职位自己**那条断言先红；
 * 本文件是**整轮层面的兜底**（例如某职位的用例在跑到契约断言之前就崩了、重启后不在 `roleOrder` 里）。
 */
const CONTRACT_BROKEN_SENTINEL = resolve(ROWS_DIR, 'contract-broken.sentinel')

/** 职位 → 行文件名（角色名里的非单词字符换成 `_`，与 `04` 的截图 / 行命名同一套规则）。 */
const rowFileOf = (role: string): string => resolve(ROWS_DIR, `row-${role.replace(/[^\w]+/g, '_')}.json`)

/**
 * 一条**发现**（spec §2 的判据本体：职位 R + 页面 P + 控件 C ⇒ 一个 403）。
 *
 * ⛔ **不是「缺陷判定」**：本文件只保证「这条 403 是**这个职位**点**这条控件**时打出来的」，
 * 是不是缺陷、要不要修，由人（和 T4 的基线）裁——spec §4 明说本网不允许"有 403 就报红"。
 */
type AuditFinding = {
  /** 出事的页面路径（`reachable` 里的那一条）。 */
  page: string
  /**
   * 这条控件**属于哪一层壳**（spec §2 的归属条）：
   *  - **页内控件**（`#app` 里）= 该页面的路径（与 `page` 相同）；
   *  - **层内控件**（`.layui-layer` 里）= `layer:<层标题>`，标题取 `.layui-layer-title`，
   *    取不到退成 `layer:untitled`（`layer.msg` 这类没有标题栏的层是**常态**，见 `LAYER_TITLE`）。
   *
   * ⚠️ 它是 T1 扩张**新增**的键（spec §5）：既有键（`page` / `control` / `urls` / `abortedWrites` /
   * `occurrences`）一个都没改名——报告是给 T2/基线读的，改键名等于把已有读数弄丢。
   */
  surface: string
  /** 控件身份串（`controlIdentity()` 的形状，spec §3.2）。 */
  control: string
  /** 这条控件触发的 **403 GET** 的完整 URL（按观测顺序；同一条 URL 可能重复出现）。 */
  urls: string[]
  /** 这条控件**触发但被拦断**的写请求（`<方法> <URL>`）——⛔ 只是记录，不是缺陷（spec §3.3）。 */
  abortedWrites: string[]
  /**
   * **同一页、同一身份串**被驱动的元素个数（≥ 1）。
   * ⚠️ 为什么会有 > 1：身份串**有意不含** `data-id` / `data-order` 这类逐行实例属性
   * （spec §3.2：同一颗「指派」按钮在 20 行里出现 20 次，身份**就该是同一个串**），
   * 所以 20 行各自打出的同一个 403 会归成**一条**发现、`occurrences` 记它出现了几次
   * （⛔ 不是把 20 条原样铺开：那对读报告的人与 T4 的基线都是噪声）。
   */
  occurrences: number
}

/**
 * 体检表一行。字段按 spec §3.1 / T2 任务书定死的形状，**并为 T3/T4 留位**：
 *  - `findings` / `controlsClicked` / `notes` 由 **T3** 填（T2 一律空数组 / 0）；
 *  - ⛔ 别改名、别删——T4 按这些键读同一份报告。
 *
 * `matched` / `onlyReachable` / `onlyVisible` 是 T2 的**读数**（不是判据本体：判据本体是下面那两条
 * `expect(...).toEqual([])`）。落进报告是为了让「哪些路径只在一侧」不必再跑一遍浏览器就能看。
 */
type AuditRow = {
  role: string
  /** 该职位在矩阵里的中文名（给人看的）。 */
  label: string
  /** 路由守卫实际放行的路径（排序后）。 */
  reachable: string[]
  /** 渲染出来的菜单里能到达的路径（排序后）。 */
  menuVisible: string[]
  /** **两条判据是否都成立**（T2 修复轮起：`onlyVisible` 空 **且** `onlyReachable ⊆ HIDDEN_MENU_PATHS`）。 */
  matched: boolean
  /** 「守卫放行但菜单里看不见」——⛔ 只允许是 `menu.ts` 有意隐藏的那些（见 `HIDDEN_MENU_PATHS`）。 */
  onlyReachable: string[]
  /** 「菜单里看得见但路由守卫不放行」——⛔ 必须为空（会伤到用户的方向）。 */
  onlyVisible: string[]
  /** T3 填：该职位跑出来的 403 发现（每条 = 一个页面 + 一条控件）。 */
  findings: AuditFinding[]
  /** T3 填：该职位**实际驱动过**的控件数（含点不动的：那也算「点过」，见 `clickFailures`）。 */
  controlsClicked: number
  /**
   * T7 填：**同一页同一身份串的第 N+1 个及以后的重复元素**没被驱动的条数（去重上限
   * `DEDUPE_N`）。⛔ 它与 `deduped` 的明细**必须成对读**：这是"没驱动多少条重复"的账，
   * 不是"少测了多少个页面"。
   */
  dedupedDuplicates: number
  /** T7 填：**同身份串另有可命中的那一份**（layui 固定列克隆体）而没驱动的副本条数。 */
  dedupedUnhittable: number
  /** T7 填：上面两类"没驱动"的**逐条明细**（⛔ 不许静默少点：每条都带身份串 + 原因）。 */
  deduped: { page: string; control: string; reason: string }[]
  /**
   * T7 填：该职位**候选可驱动总数**（各页驱动计划的分母之和）。N3 收口用的恒等式是
   * `controlsClicked + dedupedDuplicates + dedupedUnhittable >= drivableTotal`。
   */
  drivableTotal: number
  /** 自由备注（T2 记「菜单标签映射到多条路由」这类观察；T3 记每页的控件条数这类读数）。 */
  notes: string[]
  /** T3 填：点不动的控件（⛔ **不是权限缺陷**，是探针自己的限制；spec §3.3 明说不要判红）。 */
  clickFailures: { page: string; control: string; reason: string }[]
  /**
   * T1 填：**层内控件**点不动的条数（spec §5 要求的独立计数）。
   *
   * ⚠️ 与 `clickFailures` 是**同一批事件的两个视图**（层内那部分）：`clickFailures` 里每条都带
   * `surface`（层内的那些以 `layer:` 开头），本键只是它的**计数**——放一个计数键是因为
   * 报告要能一眼看出「层内驱动是不是基本没点动」，不必去筛数组。
   */
  layerClickFailures: number
  /**
   * T1 填：**层内**控件被真正驱动过的条数（含点不动的，口径同 `controlsClicked`）。
   */
  layerControlsClicked: number
  /**
   * T1 填：层内**枚举到的可驱动候选总数**（`layerControlsClicked` 的分母）。
   * 恒等式：`layerControlsClicked + layerDedupedDuplicates + layerDedupedUnhittable >= 本键`。
   */
  layerDrivableTotal: number
  /** T1 填：层内因**同一身份串已驱动够 `DEDUPE_N` 个**而没驱动的重复元素条数。 */
  layerDedupedDuplicates: number
  /** T1 填：层内因**同身份串另有可命中的那一份**而没驱动的副本条数。 */
  layerDedupedUnhittable: number
  /**
   * T1 填：**打开过的层**，按标题去重计数（spec §5 的 `layersOpened`）。
   *
   * ⚠️ 「哪些层算"打开过"」的口径见 `visibleLayers()` 上方那段注释：**只算本次驱动新开出来、
   * 且会一直留在屏幕上的层**（`layer.msg` / `layer.load` 这类**没有关闭按钮、到点自己消失**的
   * 提示层不计）——它们没有可驱动的控件，把几十条「尚未开放」提示算成「打开的层」会把读数淹掉。
   */
  layersOpened: number
  /**
   * T1 填：**关层失败**的次数（`.layui-layer-close` 点不动 / 点了没关掉 ⇒ 退到 `Escape` /
   * `layer.close()`，仍不行就**重新导航该页**；收尾复核还剩层也计一次）。
   *
   * ⚠️ 它**不判红**——spec §3 第 4 条的硬约束是"⛔ 不许带着残留层去点下一条页内控件"，
   * 而那件事由「关不掉就重新导航」保证，不是由"关层必须一次成功"保证。本键是给读报告的人看
   * **代价**的：关层失败率高时整轮会明显变慢（每次都要重新导航一页），那是排障入口。
   */
  layerCloseFailures: number
  /** T1 填：**`type:2` iframe 层**（内容在另一个文档里、本线枚举不到）被跳过的**层**数（spec §3 第 6 条）。 */
  layersSkippedIframe: number
  /** T1 填：被跳过的 iframe 层的 `surface` 身份（G：⛔ 不只记计数）。 */
  skippedIframeLayers: { page: string; surface: string }[]
  /**
   * T1 填：**同一次点击开出来的其余兄弟层**被跳过的**层**数（D：原名
   * `layersNestedSkipped`）。⛔ 它**不是**"层里再开的层"；真嵌套既不计数也不驱动，见 spec §6 / §9。
   */
  layersCoOpenedSkipped: number
  /**
   * T1 填：**按层逐条**的读数（页面 / `surface` / 层内候选数 / 枚举到几条可驱动 / 未驱动几条 /
   * 点了几条）。
   *
   * ⛔ 它不是 spec §5 点名的键，而是那几条**判据**的取证：spec §4 的 L2（「某个层里枚举到了控件、
   * 却一条都没点过 ⇒ 红」）需要**逐层**的分子分母；只留一个职位级计数的话，L2 会退化成
   * 「职位级总数不为零」那种恒真形状（`E-0024`）。键是**新增**的，既有键一个没动。
   * ⚠️ `candidateTotal` / `notDriven` 是A 加的：它们让层内「候选 → 可驱动 /
   * 未驱动」的分账**可逐层核对**（`candidateTotal === drivable + notDriven`）。
   */
  layers: {
    page: string
    surface: string
    candidateTotal: number
    drivable: number
    notDriven: number
    clicked: number
  }[]
  /**
   * T3 填：该职位**被拦断的写请求总数**（spec §3.3 的安全网到底接上没有的读数）。
   * ⚠️ 它不是缺陷、也不是判据：正常页面上很多按钮是"打开弹层"、根本不发写请求，**0 也可能正常**；
   * 而只要有写请求被拦下，这个数就会 > 0（做"写测试"的核对就靠它）。
   */
  abortedWrites: number
  /**
   * T2（写模式）填：本轮模式（`'read'` 默认 / `'write'`）。**新增键**，既有键一个不改。
   * 读模式下它恒为 `'read'`——报告里能一眼看出这一轮跑的是哪一种（spec §4 的 `mode`）。
   */
  mode: 'read' | 'write'
  /**
   * T2（写模式）填：驱动期**探针决定放行**的非 GET `/api/**` 请求数（spec §4 的 `writesPerformed`）。
   *
   * ⛔ **语义只是「路由处理器决定不拦这一条」**，不代表服务端收到、更不代表写成功
   * （评审修复波 D：原措辞"真的发出去 / 真的落库"过强）。
   * ⛔ **它不能当 W3 的判据**：`push` 只在 `WRITE_MODE` 分支里 ⇒ 读模式下**结构性恒为 0**
   * （评审修复波 A）。W3 / W2 的判据是下面那条 `nonGetApiResponses`。
   * ⚠️ 与 `abortedWrites` 是**互斥**的两个数：同一条请求要么被拦断、要么被放行。
   */
  writesPerformed: number
  /**
   * T2（写模式）填：驱动期**真的收到响应**的非 GET `/api/**` 请求数（登录除外）。
   *
   * 观测口 = `page.on('response')`（`installForbiddenObserver`），**与 `WRITE_MODE` 分支无关**
   * ⇒ 被 `abort()` 的请求没有响应、不计入；一旦拦断被改成放行就会计入。
   *  - 读模式必须为 **0**（**W3**：拦断仍在）；
   *  - 写模式必须 **> 0**（**W2**：开关真的切到了放行）。
   * ⚠️ 新增键（报告键只许增）：⛔ 不要拿它替换 / 改名 `writesPerformed`——两者语义不同。
   */
  nonGetApiResponses: number
  /**
   * T2（写模式）填：该职位这一遍**成功执行的重置次数**（spec §4 的 `resets`）。
   * 写模式下每个职位开跑前重置一次 ⇒ 正常应当是 `1`；⛔ 重置失败**抛**（不会静默继续），
   * 所以这个数只会是 0（没跑成/没重置）或 1，不会出现"半次"。
   */
  resets: number
  /**
   * T2（写模式）填：**写模式下新出现的异常响应**——非 GET 的 **403** 与任何 **≥500**，
   * 逐条带「页面 / surface / 控件身份串 / 状态行」。
   *
   * ⛔ **不是判据、不判红**：写请求的 403（该职位对那个写端点没权限）与读的 403 是**两件事**，
   * 本线的判据本体只认「GET 403」（spec §4 的"判据不变"）；这一栏是给读报告的人**取证**用的
   * （任务书明确要求"写模式下有没有新的 403 或 5xx，逐条列出"）。
   * 读模式下它恒为空数组（观测器不在读模式记这两类）。
   */
  writeIssues: { page: string; surface: string; control: string; issue: string }[]
  /** T3 填：**结构上就没有候选控件**的页面（N3 的显式豁免清单，⛔ 不许静默放行）。 */
  pagesWithNoControls: string[]
  /**
   * 上面那批里，**观察窗口内连一个候选都没渲染出来**的那些页（`waitForCandidates()` 超时）。
   * ⛔ 与"渲染出来了、但确实没有控件"分开记：后者是设计，前者更像探针 / 数据问题。
   */
  pagesNeverRendered: string[]
  /**
   * T3 填：枚举到、但**没有驱动**的候选（路由链接 / 驱动选择器不含的）。
   * ⛔ 这一列是**留痕**用的：本网的可驱动覆盖下界只对 `drivable` 成立，
   * 所以每一条"没点的候选"都要能在这儿被逐条看见，⛔ 不许静默漏点。
   * ⚠️ **层内的那些也在这里**（A）：`reason` 前置 `（层内 <surface>）` 标明归属，
   * 口径与页内**同一个** `notDrivenOf()`。
   */
  notDriven: { page: string; control: string; reason: string }[]
  /**
   * **本次给这个职位解析出来的「参数化详情页」实例**（spec §2 的"实例化"）——报告要能一眼看见
   * 「扫的是哪几条具体路径」，⛔ 不是只给一个计数（只给计数的话，"实例化了但没驱动"看不出来）。
   */
  parameterized: {
    /** 登记表里的模板（如 `/orders/{order_no}`）——说明这条来自哪条参数化路由。 */
    pattern: string
    /** 登记表里的 `routeName`（与 `router/index.ts` 逐字一致，由加载期自检钉住）。 */
    routeName: string
    /** 实例化后的**具体路径**（进页面清单、被驱动的那一条）。 */
    path: string
    /** 取实例时形状校验过的原始值（订单号 / 账号 id）——"扫的是哪一笔"要能复核。 */
    shapeValue: string
  }[]
  /**
   * **没取到实例、且原因是「这个职位就是没数据」**的登记（spec §2 的 `noData` 那一类：
   * HTTP 200 / 业务码 0 / 列表是**空数组**）——记下来并**跳过该职位这一页**，⛔ **不判红**
   * （数据短缺不是权限缺陷，覆盖率与数据相关）。
   *
   * ⚠️ 为什么要把"为什么没取到"一起记下来：只记"没取到"分不清
   * 「这租户没数据」（正常）与「端点 403 / 500 / 形状不对」（真问题）——那三种处置完全不同。
   * ⛔ 「端点 / 形状」那几种**不在这里**：它们进 `contractBroken`（下一栏）并**当场判红**。
   */
  noInstance: {
    pattern: string
    routeName: string
    /** 父列表路由（`detailOf`）——这条详情页本该从它点进来。 */
    detailOf: string
    /** 人读的原因（含 API 状态码 / 业务码 / "列表为空"）。 */
    reason: string
  }[]
  /**
   * ⭐ **实例解析到了、但这一页对该职位不可达**（导航被路由守卫弹回 `/`）——评审裁定 a
   * （2026-09-26，最小权限角色线）新增的**记录、不判红**那一档。
   *
   * ## 为什么必须有这一档（本线实测的真形态）
   * 一个**合法的「最小权限」配置**（权限页允许勾任意子集）可以只持 `order.list` 而**不持**
   * `order.view_detail`：那时父页 `/orders` **可达**、用该职位自己的会话也**解析得出**一笔订单，
   * 但 `/orders/:order_no` 的路由守卫会把它**弹回 `/`** —— 这正是**该有的最小权限行为**，
   * ⛔ 不是缺陷、也不是覆盖缺失（`order.list` 蕴含 `order.view_detail` 是**另一个决策**：
   * 那等于悄悄扩权，本线不立）。
   *
   * ⛔ **只允许在「真的被弹回 `/`」时出现**：入账的判据是导航的**终态路径**（`settledPathAtEntry`）
   * 逐字等于 `/`，⛔ **不是** `visited=false`——否则把 `visited` 改坏会同时改到"分类"与"计数"
   * 两侧、判据自洽（`E-0083` 的「变异连期望一起改」形态，本条的"弄红"证明正是打这个点）。
   * 终态不是 `/`（去了别处）⇒ 主用例**当场红**，⛔ 不许当"跳过"后门。
   *
   * ⚠️ 它**既不算 findings、也不算覆盖缺失**：`INSTANTIATED_CANDIDATE_FLOOR` 的下界与 D2 的
   * "自证不是空转"都改按 **visited 的实例化页**取（见那两处的注释）。
   */
  instancePageUnreachable: {
    role: string
    /** 登记表里的模板（如 `/orders/{order_no}`）。 */
    pattern: string
    /** 实例化后的具体路径（= 被弹回的那条 URL）。 */
    url: string
    /** 人读的原因：**含实测终态**（必须是 `/`，否则主用例判红）。 */
    reason: string
  }[]
  /**
   * **没取到实例、且原因是「解析契约坏了」**的登记（spec §2 的另一类；评审修复波第 3 条）：
   * 网络层失败 / HTTP 非 2xx / 业务码非 0 / 响应缺字段（`data.items` 不是数组）/ 实例不过形状判据 /
   * **翻页元数据 `total > 0` 但列表为空**（spec §8.9 的加固：取数条件写错 ⇒ 端点照办却回空列表）。
   *
   * ⛔ **这一栏非空 ⇒ 当场判红**（见主用例里紧跟 D1 的断言）。防的正是评审推演出的那个掩盖窗口：
   * `resolve()` 读错字段名 ⇒ 只解析出另一条路由 ⇒ D1（"≥1 条"）与 D2（候选总数下界）都过、
   * 整轮绿，只留一行 `noInstance`。两分类之后，它会被判成**契约坏**而红。
   * ⚠️ `where` 是**坏在哪一步**的短标签，`reason` 里**贴出拿到的原值**（判红时要能直接看见）。
   */
  contractBroken: {
    pattern: string
    routeName: string
    detailOf: string
    /** 坏在哪一步（如「响应缺字段（data.items 不是数组）」/「业务码非 0」）。 */
    where: string
    /** 人读的原因（含 API 状态码 / 业务码 / **拿到的原值**）。 */
    reason: string
  }[]
  /**
   * 本次**实例化页面的候选控件数**（`candidateTotal` 之和）——spec §5 的 **D2** 的分子。
   *
   * ⛔ 它**必须**计入整轮 `CANDIDATE_FLOOR` 的分子（那个和是 `pageResults` 全部页的
   * `candidateTotal` 之和，实例化页也在 `pageResults` 里）——**没有**这条读数的话，
   * 「加进清单但不驱动」会**静默通过**（`CANDIDATE_FLOOR` 只看总数、不看新页贡献了多少）。
   */
  instantiatedCandidateTotal: number
}

/** 建一份空行（`role` / `label` 之外全空）——⛔ 与 `AuditRow` 的字段保持单一形状。 */
function emptyRow(role: string, label: string): AuditRow {
  return {
    role,
    label,
    reachable: [],
    menuVisible: [],
    matched: false,
    onlyReachable: [],
    onlyVisible: [],
    findings: [],
    controlsClicked: 0,
    dedupedDuplicates: 0,
    dedupedUnhittable: 0,
    deduped: [],
    drivableTotal: 0,
    notes: [],
    clickFailures: [],
    layerClickFailures: 0,
    layerControlsClicked: 0,
    layerDrivableTotal: 0,
    layerDedupedDuplicates: 0,
    layerDedupedUnhittable: 0,
    layersOpened: 0,
    layerCloseFailures: 0,
    layersSkippedIframe: 0,
    skippedIframeLayers: [],
    layersCoOpenedSkipped: 0,
    layers: [],
    abortedWrites: 0,
    mode: WRITE_MODE ? 'write' : 'read',
    writesPerformed: 0,
    nonGetApiResponses: 0,
    resets: 0,
    writeIssues: [],
    pagesWithNoControls: [],
    pagesNeverRendered: [],
    notDriven: [],
    parameterized: [],
    noInstance: [],
    instancePageUnreachable: [],
    contractBroken: [],
    instantiatedCandidateTotal: 0,
  }
}

/* ——— ① 静态映射：菜单标签 → routeName → path（**只负责翻译，不负责可见性**） ——— */

/** 菜单条目的 DOM 身份键：`分组标题\u0000条目标签`（顶层条目的分组段为空串）。 */
const labelKey = (group: string | null, label: string): string => `${group ?? ''}\u0000${label}`

/**
 * (分组标题, 条目标签) → routeName。顶层条目退化成 `(null, 标签)`。
 *
 * ⚠️ 只收**可能渲染出来**的条目：整组 `hidden`（`canSeeGroup` 直接 false）与单条 `hidden`
 * （`useMenu.keepItem` 直接 false）的条目**永远不在 DOM 里**，收进来只会造出永远匹配不上的键。
 *   判定与 `composables/useMenu.ts` 的 `canSeeGroup` / `keepItem` 一一对应（那里是唯一出处）。
 *
 * ⛔ **同名同组却指向不同 routeName ⇒ 当场响亮失败**：那正是任务书点名的那种歧义
 * （同一 (分组标题, 条目标签) 对应多条菜单条目）——⛔ 不许静默取第一条、也不许因此放宽判据。
 * ⚠️ 区分「歧义」与「同一个 routeName 被写了两遍」：只有 routeName 不同才算歧义；
 *   【发平台设置】/【系统维护】里那条同名条目属于后者（【系统维护】的那条没有 routeName、已跳过）。
 */
const MENU_LABEL_TO_ROUTE: Map<string, string> = (() => {
  const map = new Map<string, string>()
  const add = (group: string | null, label: string, routeName: string | undefined): void => {
    if (routeName === undefined) return
    const k = labelKey(group, label)
    const seen = map.get(k)
    if (seen !== undefined && seen !== routeName) {
      throw new Error(
        `菜单标签有歧义：「${group ? `${group} / ` : ''}${label}」同时指向 ` +
          `${seen} 与 ${routeName} 两条路由 ⇒ (分组标题, 条目标签) 无法唯一定位。\n` +
          '这是**响亮失败**（不是静默取第一条）：请先裁定这条歧义，再让审计往下跑。',
      )
    }
    map.set(k, routeName)
  }
  for (const item of TOP_LEVEL_ITEMS) {
    if (item.hidden) continue
    add(null, item.label, item.routeName)
  }
  for (const group of MENU_GROUPS) {
    if (group.hidden) continue
    for (const item of group.items) {
      if (item.hidden) continue
      add(group.name, item.label, item.routeName)
    }
  }
  return map
})()

/**
 * 标签 → routeName 的**反查**（给报错文案指认用：报错要能说清是哪个标签）。
 * ⚠️ 同一 routeName 可能对应多个标签（如【发平台设置】与【系统维护】里的同名重复项），
 * 所以值是数组，不取第一条。
 */
const MENU_ROUTE_TO_LABELS: Map<string, string[]> = (() => {
  const map = new Map<string, string[]>()
  for (const [k, routeName] of MENU_LABEL_TO_ROUTE) {
    const label = k.split('\u0000')[1]
    map.set(routeName, [...(map.get(routeName) ?? []), label])
  }
  return map
})()

/**
 * routeName → path 的映射**唯一出处**在 `fixtures/routes.ts`（与 `04` 同源）。
 * ⛔ 本文件不再自己抄一份显式清单：抄一份就是两套判据，而漂移只表现为静默少映射几个页面。
 */
const ROUTE_NAME_TO_PATH: Map<string, string> = ROUTE_PATH_BY_NAME

/* ——— ② 读 DOM：渲染出来的菜单条目（可见性的唯一出处） ——— */

/** 读到的菜单条目：身份键（`分组\u0000标签`）+ 是否置灰。 */
type RenderedMenuItem = { key: string; label: string; disabled: boolean }

/**
 * 从页面 DOM 读出渲染出来的菜单条目。**每条都带「分组标题」（顶层为空）**，
 * 这样 (分组标题, 条目标签) 才唯一定位一条条目。
 *
 * ⚠️ **为什么读标签、而不是读 `:index` / href**：`AppSidebar.vue` 的 `el-menu-item` 只给
 * `:index="item.label"`、**没有 href**，点击走 `router.push({ name: item.routeName })`
 * ⇒ DOM 里拿不到目标路径这个事实，路径只能靠上面的静态映射翻译，而「这一条在不在 DOM 里」
 * **只能**来自本函数。两条信息合成一条路径，缺一不可（`AGENTS.md` §5C）。
 *
 * ⚠️ 顶层与分组内的条目**都要收**（任务书写明）：用 `el-menu-item` 的父级把两者分开，
 * 避免把分组标题（`div.el-sub-menu__title`，不是 `li`）或嵌套条目混进来。
 *
 * ⚠️ 标签取的是**条目的第一个直接子 span**（`SIDEBAR_ITEM_LABEL_SPAN`），⛔ 不是整条 `li` 的
 * `textContent`：置灰条目会多渲染一个「即将开放」小标签，它自己也含 span，会把标签污染成
 * `"…设置即将开放"`（T2 执行期实测）。用 `textContent()` 而不是 `innerText()`：后者要等元素
 * **可见**，而折叠分组里的条目标签在未展开时不可见（本函数读的是「渲染出来的条目」，不是
 * 「此刻可见的条目」——可见性判据在别处，见模块头）。
 */
async function readMenuItems(page: Page): Promise<RenderedMenuItem[]> {
  const items: RenderedMenuItem[] = []
  const pushes = async (locator: Locator, group: string | null): Promise<void> => {
    for (let i = 0; i < (await locator.count()); i++) {
      const item = locator.nth(i)
      // 直接子 span 只有两个（标签 + 置灰时的「即将开放」小标签），标签那个在**前** ⇒ `.first()`。
      const label = normalizeLabel(
        (await item.locator(SIDEBAR_ITEM_LABEL_SPAN).first().textContent()) ?? '',
      )
      items.push({
        key: labelKey(group, label),
        label,
        disabled: (await item.getAttribute('class'))?.includes('is-disabled') ?? false,
      })
    }
  }
  // ① 顶层：`el-sub-menu__title` 是 `div`（不是 `li`）⇒ `:not(.el-sub-menu)` 把分组排除掉。
  await pushes(page.locator(`${SIDEBAR_MENU} > li.el-menu-item:not(.el-sub-menu)`), null)
  // ② 分组内的条目：按 `el-sub-menu` 逐个收，把所在分组的标题一起带上。
  const groups = page.locator(SIDEBAR_SUB_MENU)
  for (let i = 0; i < (await groups.count()); i++) {
    const group = groups.nth(i)
    const title = normalizeLabel(await group.locator(SIDEBAR_SUB_MENU_TITLE).innerText())
    await pushes(group.locator(SIDEBAR_SUB_MENU_ITEM), title)
  }
  return items
}

/**
 * 标签归一化：去掉空白（渲染出来的标签两侧可能带排版用空白）。⚠️ **只有这一处归一化**——
 * 另一侧是 `menu.ts` 里干净的中文字面量，两侧比的是**同一个归一化后的串**
 * （`E-0083`：只归一化一侧会让判据看起来更可靠）。
 */
function normalizeLabel(raw: string): string {
  return raw.replace(/\s+/g, '')
}

/**
 * 把 DOM 读到的条目翻译成路径。**翻译不出 / 歧义一律响亮失败**（⛔ 不许静默取第一条、
 * 也不许因此放宽判据）：那说明 DOM 里有 `menu.ts` 没声明的条目，或同一 (分组, 标签) 对应多条路由。
 *
 * ⚠️ **置灰条目另行处理**（不进返回值，但**必须留痕**到 `notes`）：它们的 `routeName` 是
 * `undefined`（`isClickable` 为假 ⇒ 渲染成 `disabled`，只提示「即将开放」），**根本没有可去的路径**，
 * 而另一侧的候选清单（`CANDIDATES`）也不含它们 ⇒ 两侧都不算才是同一把尺（`E-0083`）。
 * 若这里把它们当「翻译不出」抛错，等于要求 `menu.ts` 的每个置灰项也得有路由——那与
 * 「置灰 = 路由表里没有该 name」这条既定语义（`useMenu.isClickable` 与 `menu.ts` 的注释）直接冲突。
 */
function pathsOfItems(items: RenderedMenuItem[], role: string, notes: string[]): string[] {
  const paths: string[] = []
  for (const item of items) {
    const [group, label] = item.key.split('\u0000')
    if (item.disabled) {
      notes.push(
        `置灰条目（无 routeName ⇒ 不可点、也无对应路由，本线两侧都不计）：「` +
          `${group ? `${group} / ` : ''}${label}」`,
      )
      continue
    }
    const routeName = MENU_LABEL_TO_ROUTE.get(item.key)
    if (routeName === undefined) {
      throw new Error(
        `职位 ${role}：菜单里渲染出条目「${group ? `${group} / ` : ''}${label}」，` +
          '但 `config/menu.ts` 里没有对应的可见条目（标签对不上，或它已被 `hidden`）。\n' +
          '这**不是**「少一条菜单」那么简单：本用例的 `menuVisible` 靠标签映射成路径，' +
          '映射不上的条目会让两侧清单**没法比**。请先核 `AppSidebar.vue` / `config/menu.ts` / `useMenu.ts`。',
      )
    }
    const path = ROUTE_NAME_TO_PATH.get(routeName)
    if (path === undefined) {
      throw new Error(
        `职位 ${role}：菜单条目「${label}」映射到 routeName=${routeName}，但路由表里没有它的 path。\n` +
          '多半是 `menu.ts` 与 `router/index.ts` 漂移了（菜单指向一条不存在的路由 = 死链）。',
      )
    }
    if (paths.includes(path) && !notes.some((n) => n.includes(path))) {
      notes.push(`菜单里有多条条目指向同一个 path：${path}（标签：${label}）`)
    }
    paths.push(path)
  }
  return paths
}

/* ——— ③ reachable(R)：逐条导航、读终态 path ——— */

/**
 * 逐条 `goto` 候选路由，返回**终态 path 等于候选路径**的那些（忽略 query）。
 *
 * 判据 = 终态 URL 的 `pathname` 是否 === 候选路径：
 *  - 相等 ⇒ 这条在该职位的 `reachable` 里；
 *  - 被弹回 `/` ⇒ 不在。
 * ⛔ **不用「页面上有没有报错」当放行判据**——那是另一件事（`04` 管的是那个）。
 * ⚠️ 候选里本来就有 `/` 这条，它自己永远算「到得了」（`/` 的守卫是 `canNode([])` ⇒ 恒真）。
 */
async function reachablePaths(page: Page): Promise<string[]> {
  const out: string[] = []
  for (const path of CANDIDATES) {
    await page.goto(`${FRONTEND_URL}${path}`)
    if ((await settledPath(page)) === path) out.push(path)
  }
  return out.sort()
}

/**
 * 等这次导航的**终态**落定，再读 path。
 *
 * ⛔ **实现已外移到 `fixtures/routes.ts` 的 `settledPath()`**（T2 修复轮，任务书第 ④ 条）：
 * T3 驱动控件后读终态也要用它，⛔ 不要在两侧各写一份（`E-0021`）。本条 ⛔ 不能
 * `goto()` 之后立刻读 `page.url()`——理由、实测数字与取舍全在 `settledPath()` 的注释里。
 */

/* ——— ③b 控件驱动（T3）：从 DOM 枚举 + 拦断写 + 收 403（spec §3.2 / §3.3 / §3.4 的 N1、N3） ——— */

/**
 * `/api/**` 上**非 GET** 请求的拦断记录（模块级数组）。
 *
 * ⚠️ **为什么是模块级**：路由处理器是在 `beforeAll` 里**一次装好**的（整个 worker 生命周期都生效），
 * 而报告是**逐职位**的 ⇒ 处理器只能往模块级数组里攒，再由每个职位的用例**取走并清空**归到自己行里。
 * ⛔ 不在这里判红：被拦下的写是本网的**有意安全边界**（spec §3.3），不是缺陷。
 */
const abortedWrites: string[] = []

/**
 * 驱动期**探针决定放行**的非 GET `/api/**` 请求（模块级，口径与 `abortedWrites` **对称**）。
 *
 * ⛔ **它的语义只是「路由处理器决定不拦这一条」**——⛔ **不代表**服务端真的收到、更不代表写成功
 * （评审修复波 D：原来的措辞「真的发出去」/「真的落库」比它证明的东西强）。
 * ⚠️ **它不能当 W3 的判据**：`push` 只出现在 `installWriteGate` 的 `WRITE_MODE` 分支里，
 *    而 W3 跑在读模式 ⇒ 读模式下它**结构性恒空**（评审修复波 A 点的名：那是恒真钉子）。
 *    W3 / W2 现在都改看下面那条**与模式分支无关**的 `nonGetApiResponses`。
 * ⚠️ 与 `abortedWrites` **互斥**：同一条请求要么被 `abort()`、要么被这里记为"决定放行"。
 */
const performedWrites: string[] = []

/**
 * 驱动期**真的收到响应**的非 GET `/api/**` 请求（模块级；观测口 = `page.on('response')`）。
 *
 * ## 为什么另立一条观测（评审修复波 A）
 * 原来的 W3 用 `performedWrites`（见上），而它只在 `WRITE_MODE` 分支里 `push` ⇒ **读模式下恒空**、
 * `expect(...).toBe(0)` **永远绿**。后果：把读模式的 `route.abort()` 换成 `route.fallback()`
 * （或删掉处理器）让写请求真的漏到只读租户时，W3 **照样绿**——这条判据形同装饰。
 *
 * 所以这里改用一个**与模式分支无关**的观测：`page.on('response')`（装在 `installForbiddenObserver`
 * 里，⛔ 不经过 `installWriteGate` 的放行 / 拦断三目）。语义是
 * **「这条非 GET 请求拿到了响应」**——被 `abort()` 的请求只触发 `requestfailed`、**没有响应**，
 * 而放行（真实网络 / `route.fallback` / `route.fulfill`）都会触发 `response`。
 * ⇒ 同一条读数在两种模式下**都能被证伪**：
 *  - 读模式必须 **0**（W3：拦断仍在）；
 *  - 写模式必须 **> 0**（W2：开关真的切到了放行）。
 *
 * ⚠️ **排除登录**（`AUDIT_LOGIN_PATH`）：它在两种模式下都是**有意放行**的写请求，不排除就会被算成
 * "写请求出网了" ⇒ W3 假红。⚠️ 本观测的窗口从**驱动期**起算（登录在窗口之前）⇒ 这条排除今天是**兜底**
 * （挡"驱动期里再登录一次"那种形态），⛔ 不是"读模式因此才有 5 条"。
 * ⚠️ 与 `performedWrites` **是两个不同的键、两种不同的语义**（前者 = 探针决定放行，后者 = 真的收到响应）
 * ——⛔ 别把两者混成一个（评审修复波 A 的明文要求），也⛔ 不许改名 / 删掉既有键（报告键只许增）。
 */
const nonGetApiResponses: string[] = []

/**
 * 写模式下**新出现的异常响应**的原始流水：非 GET 的 **403** 与任何 **≥500**（"`<状态> <方法> <URL>`"）。
 *
 * ⛔ **只记录、不判红**（判据本体只认 GET 403；spec §4）。读模式下这个数组**永远是空的**
 * （观测器只在 `WRITE_MODE` 下记这两类）⇒ 读模式的行为逐字不变。
 */
const writeModeResponses: string[] = []

/**
 * `writeModeResponses` 的**带归属版本**：`{页面 / surface / 控件身份串 / 状态行}`。
 *
 * ⚠️ 归属的取法与 `findings` 同一套（在 `clickOneControl()` 前后切一刀、按当前驱动的
 * 控件身份串入账），实体在 `driveControlsOnPage()` / `driveControlsInLayer()` 两处 push。
 */
const writeModeIssues: { page: string; surface: string; control: string; issue: string }[] = []

/**
 * 写请求拦断的**开关**（模块级：路由处理器在 `beforeAll` 里装一次，整个 worker 都生效）。
 *
 * ⛔ **为什么必须有这个开关**（T3 执行期实测踩到）：一开始把拦断**无条件**装着，
 * 结果它把 `POST /api/auth/login` 也拦了 ⇒ **换账号登录失败**（登录页停在原地、报
 * 「网络异常，请检查连接」），用例以「没离开 /login」收场——**看起来像登录坏了，其实是本网自己的安全网
 * 装错了位置**（任务书要的是"驱动期拦断写请求"，不是"整个 worker 期间拦断一切写"）。
 * 所以：**登录 / 页面导航期一律放行，只有控件驱动那一段才拦**（`DRIVING_WRITES_ABORTED`）。
 * ⚠️ 这一条也解释了为什么用**开关**而不是"驱动前才装路由"：路由处理器一次装满、行为由开关切，
 * 比"装 / 卸 / 再装"少一类状态（后者在 worker 重启或职位之间漏装一次就会**静默不拦**）。
 */
let drivingWritesAborted = false

/**
 * 装**写请求策略**：驱动期的非 GET `/api/**` 请求——**读模式拦断、写模式放行**。
 *
 * ## 读模式（默认，spec 母规范 §3.3）
 * ⛔ 这是本线最硬的默认安全约束：审计**不是**写测试，一次点击可能发 POST（「保存」），
 * 绝不能让点击真的落库。⛔ 行为**逐字不变**（`abort()`，并把被拦下的记进 `abortedWrites`）。
 *
 * ## 写模式（`docs/specs/2026-09-25-write-mode-audit-design.md` §3/§4）
 * 只有 `WRITE_MODE`（`TT_E2E_TARGET === 'write'`，由运行器在**确认租户是一次性写租户**之后注入）
 * 才**放行**，并把它记进 `performedWrites`（**W2** 的分子；⚠️ 那一栏的语义是"**探针决定放行**"，
 * ⛔ 不是"服务端收到 / 写成功"——评审修复波 D；真正"收到响应"的那条读数在 `nonGetApiResponses`）。
 * ⛔ **fail-closed**：`WRITE_MODE` 为假时这条分支不可能走到放行那一侧
 * （其余一切取值 ⇒ 保持拦断），所以"对未知租户放行写"这条路径**不存在**。
 *
 * ⚠️ **只拦 / 放 `/api/**` 上的非 GET**：GET 一律放行（判据本体就是 GET 403 的观测）；
 * 非 `/api/` 的请求（静态资源 / `javascript:` 的伪 URL 导航）不拦。
 * ⚠️ 读模式用 `abort()`（不是 `fulfill()`）：abort 让请求**根本不出网**，比伪造一个响应更安全，
 * 也避免前端把伪造的成功响应当真、继续往下跑「先写后读」的流程。
 */
function installWriteGate(page: Page): void {
  void page.route('**/api/**', (route) => {
    const request = route.request()
    if (drivingWritesAborted && request.method() !== 'GET' && request.url().includes(AUDIT_API_PREFIX)) {
      if (WRITE_MODE) {
        performedWrites.push(`${request.method()} ${request.url()}`)
        void route.fallback()
        return
      }
      abortedWrites.push(`${request.method()} ${request.url()}`)
      void route.abort()
      return
    }
    void route.fallback()
  })
}

/** 装 N1 用的假端点处理器：**每个职位都必然 403**（请求被本地 `fulfill`，根本不出网）。 */
function installAuditProbe(page: Page): void {
  void page.route('**/api/__audit_probe__*', (route) => {
    void route.fulfill({
      status: AUDIT_FORBIDDEN,
      contentType: 'application/json',
      body: '{"code":403,"msg":"audit probe","data":null}',
    })
  })
}

/**
 * 本次审计观测到的 **403 GET `/api/**`**（响应原文 `403 GET <url>`）。**每次装观测器时清空**。
 *
 * ⛔ 口径必须与 spec §2 逐条对齐（放宽任何一条都会把无关噪声算成"发现"）：
 *  - 只算 `/api/**`：静态资源 / 页面本身的响应不算；
 *  - 只算 **GET**：写请求在驱动期被拦断、根本不出网（spec §3.3）；
 *  - 只算 **403**：401 是登录前的会话探针（实测每页都有）、400/404 可能是页面自己的分支
 *    ——「4xx 一律判红 = 造一个天天红的摆设」（spec §2）。
 *  - ⛔ **不算导航请求**（`isNavigationRequest()`）：整文档导航的 URL 也可能带 `/api/`（理论上），
 *    那属于「页面打不开」，不是「控件被权限拦下」。
 */
const auditForbidden: string[] = []

/**
 * 装 403 观测器（`page.on('response')`，覆盖 island iframe 里的 `fetch`）。
 *
 * ⛔ **一次装好、整个 worker 都留着**（⛔ 不要在驱动前改写 / 重装）：本网只关心
 * "**点击**触发的 403"，而点击前的页面加载（`/orders` 自己打的那些请求）必须**先被记进流水**、
 * 再在每条控件前**切一刀**排除掉——如果在驱动前才装观测器，页面加载期的那些请求根本不在流水里，
 * 它们会在**第一条控件**的窗口内到达并被记到那条控件头上（**错误归因**）。
 *
 * ## 写模式的**诊断**（T2 新增；⛔ 不是判据、不判红）
 * `WRITE_MODE` 下额外把两类**异常响应**记进 `writeModeResponses`：
 *  ① **非 GET 的 403**（该职位对这个**写**端点没权限）——判据本体只认 GET 403（spec §4），
 *    所以它**不进** `findings`，只进诊断栏；
 *  ② 任何 **≥500**（含 GET）。
 * ⛔ 读模式下这两类**一个都不记**（`WRITE_MODE` 为假时这段分支等于不存在）⇒ 读模式行为逐字不变。
 *
 * ## ⭐ 与模式分支**无关**的那条观测（评审修复波 A，W2/W3 的判据）
 * 本函数**无论读写**都把「**真的收到响应的**非 GET `/api/**` 请求」记进 `nonGetApiResponses`
 * （登录除外）。它**不经过** `installWriteGate` 的放行 / 拦断三目，所以读模式下照样可能变非零：
 * 一旦拦断被改成放行（`route.fallback()` / `route.fulfill()` / 删掉处理器），请求就会拿到响应、
 * 这条读数就会从 0 变正 ⇒ **W3 会红**。这正是把"恒真钉子"换成"能被弄红"的判据的那处改动。
 */
function installForbiddenObserver(page: Page): void {
  page.on('response', (res) => {
    const request = res.request()
    // ⭐ 模式无关的观测：先记「非 GET 且拿到响应」，⛔ 不要被下面的 403 分支 / `WRITE_MODE` 短路掉。
    if (
      request.method() !== 'GET' &&
      res.url().includes(AUDIT_API_PREFIX) &&
      !res.url().includes(AUDIT_LOGIN_PATH)
    ) {
      nonGetApiResponses.push(`${res.status()} ${request.method()} ${res.url()}`)
    }
    if (res.status() !== AUDIT_FORBIDDEN) {
      if (WRITE_MODE && res.status() >= 500 && res.url().includes(AUDIT_API_PREFIX)) {
        writeModeResponses.push(`${res.status()} ${request.method()} ${res.url()}`)
      }
      return
    }
    if (!res.url().includes(AUDIT_API_PREFIX)) return
    if (request.isNavigationRequest()) return
    if (request.method() !== 'GET') {
      if (WRITE_MODE) writeModeResponses.push(`${res.status()} ${request.method()} ${res.url()}`)
      return
    }
    auditForbidden.push(`${AUDIT_FORBIDDEN} ${request.method()} ${res.url()}`)
  })
}

/**
 * 审计这一段落的**前置**：装写拦断 + 装观测器 + **N1 正对照**。
 *
 * ⛔ N1（spec §3.4）**必须在审计开跑前**、且必须是**确定性**的：这里用 `page.route()` 把
 * `/api/__audit_probe__*` 就地 `fulfill` 成 403，再在浏览器里 `fetch` 它 —— 请求不出网，
 * 所以对**任何**职位都必然成立（`admin` 的节点判据短路成全集，拿真实端点它会 200，做不了正对照）。
 *
 * ⛔ 三条断言都**钉值**，⛔ 不是「`clientErrors` 是数组」这种恒真形状：
 *  ① 观测器**逐字**记下了这次响应（`403 GET <url>`）；
 *  ② 记下的确实**是这次探测的 URL**（不是别的什么 403）；
 *  ③ 这次探测**只产出一条**记录（观测器 / 路由处理器被重复安装时，这里会变成 2+ ⇒ 当场红）。
 */
async function runAuditProbe(page: Page): Promise<void> {
  const before = auditForbidden.length
  // 在浏览器里打这个**必然 403** 的端点；`route.fulfill` 会让这次 fetch 正常拿到响应（不是网络错）。
  await page.evaluate((url) => fetch(url).then((r) => r.status), AUDIT_PROBE_URL)
  // ⛔ 不等 `clientErrors` 变长（那要等 `observe()` 的**另一个**处理器）——本网自己的观测才是判据。
  await expect
    .poll(() => auditForbidden.length, {
      timeout: 5_000,
      message: 'N1 正对照：观测器没记下那次必然 403 的响应 ⇒ 观测器 / 路由处理器没接上，整张网会恒绿',
    })
    .toBeGreaterThan(before)
  const probe = auditForbidden[auditForbidden.length - 1]
  expect(probe, 'N1 正对照：观测器记下的不是那次探测响应').toContain(AUDIT_PROBE_URL)
  expect(probe, 'N1 正对照：观测器记下的状态码 / 方法不对').toMatch(/^403 GET /)
  // ⛔ 不是"数组是数组"那种恒真形状：这次探测**本身**必须只产出一条（多一条说明路由处理器重复装了）。
  expect(
    auditForbidden.slice(before),
    'N1 正对照：一次探测却记下了多条 403（路由处理器重复安装？）',
  ).toHaveLength(1)
}

/**
 * 读这次点击**新产生**的 403（与 `since` 之前那份快照做差）。
 *
 * ⚠️ 用**下标切片**而不是 `Set` 去重：同一秒里同一个 URL 打两次（例如弹层重建）也要各算一条
 * ——去重会把「打得更凶」这个信号抹平。
 */
function newForbiddenSince(since: number): string[] {
  return auditForbidden.slice(since)
}

/** 读**这次导航快照之后**攒下的写拦断（同样用下标切片；读走即归位到该控件的账上）。 */
function newAbortedWritesSince(since: number): string[] {
  return abortedWrites.slice(since)
}

/**
 * 等候选控件渲染出来的**观察窗口**（毫秒）。⛔ 这个数**不是**"业务上多快"的断言，只是"别把还没渲染
 * 当成没有"；给到 5s 是因为 E2E 租户的列表页要真取一次数（实测毫秒级枚举下 `/orders` 是 0 条）。
 *
 * ⚠️ T7 起这个窗口**不再是每页无条件烧掉的时间**：`waitForCandidates()` 一见"候选数稳定"就收工
 * （实测 `/orders` 约 1.2s 就稳定在 235 条 ⇒ 每页省约 3.8s；admin 那 60 页合计约 3.8 分钟）。
 */
const CANDIDATE_WAIT_MS = 5_000

/** 轮询间隔（毫秒）——`waitForCandidates()` 的采样节奏。 */
const CANDIDATE_POLL_MS = 200

/** 「候选数已稳定」需要**连续**多少个相同采样（口径：除第一次外还要连续这么多拍不动）。 */
const CANDIDATE_STABLE_SAMPLES = 2

/**
 * 最短观察窗（毫秒）：**即使候选数已经稳定**，也要至少看这么久才收工。
 *
 * ⛔ 为什么"稳定"必须配一个最短窗（T7 执行期实测）：`/orders` 的渲染是**两段式**——先出工具栏
 * 那约 20 条，隔一会儿才出行内那 215 条。若只判"连续几拍不变"，观察窗口撞上第一段就会
 * 提前收官、**静默少点 200 多条控件**（正是本网最该防的那种"看起来跑完了"）。
 */
const CANDIDATE_MIN_WAIT_MS = 800

/** `skipped` 里「路由链接」那一类的统一理由（点它会跳页 ⇒ 由页面层覆盖，不算页内控件）。 */
const ROUTE_LINK_REASON = '路由链接（href 是页面路径）：点了会跳页，由页面层（reachable）覆盖'

/* ——— ③b-1b T1 弹层扩张：标记前缀、属性与时机 ——— */

/**
 * 页内控件审计标记值的**前缀**（空串——既有读数与语义都不动）。
 */
const CONTROL_DRIVE_TAG_PREFIX_PAGE = ''

/**
 * 层内控件审计标记值的**前缀**。⛔ 必须与页内那个不同：两边的下标都从 0 数，而
 * `markedLocator()` 是按标记值在**整个 island 文档**里找元素的（层不在 `#app` 里，作用域分不开两者）
 * ⇒ 同值标记会让 `locator.click()` 命中两个元素、以 strict mode 失败，症状是"层内控件全都点不动"。
 */
const CONTROL_DRIVE_TAG_PREFIX_LAYER = 'L'

/**
 * 层内控件的标记**属性**（⛔ 与页内那个 `CONTROL_DRIVE_ATTR` **不是同一个**）。
 *
 * 层内和页内必须用不同的标记属性。同一文档里反复枚举时，上一轮的 `L0` 会和本轮撞车，
 * `locator.click()` 以 strict mode violation 失败，看起来像控件点不到。
 * layui 关普通层是删节点，不是藏起来。更可能是 `type:1` 把页内节点搬进层、关层时放回。
 * 清标记只能清层内那一套，否则会把页内快照一起废掉。
 * ⚠️ 与 `AGENTS.md` §5G 的 **E-0061** 同族（类名 / 选择器、属性 / 选择器要分成两个常量）。
 */
const CONTROL_DRIVE_ATTR_LAYER = `${CONTROL_DRIVE_ATTR}-layer`

/** 等「点击之后层冒出来」的窗口（毫秒）。`layer.open` 是同步插 DOM，见 `waitForFreshLayers()`。 */
const LAYER_OPEN_WAIT_MS = 350

/** 上面那个窗口的**轮询间隔**（毫秒）——一见有层就收工，不再睡满整个窗口。 */
const LAYER_OPEN_POLL_MS = 60

/**
 * 关层后**确认它真的没了**的等待（毫秒）。layui 关层有动画，量一次会把「正在消失」判成「没关掉」。
 * ⚠️ 与 `HIT_TEST_BUDGET_MS` 同一类取舍：有界轮询而不是一次量。
 */
const LAYER_CLOSE_WAIT_MS = 500

/** 关层确认的轮询间隔（毫秒）。 */
const LAYER_CLOSE_POLL_MS = 100

/**
 * 「哪些层算**打开过**」的判据说明（实现见 `visibleLayers()`；⛔ 这里**不是**一个选择器常量，
 * 判据落在函数里，因为它要读 DOM 属性而不是只做一个 `querySelector`）。
 *
 * ## 为什么不能把所有 `.layui-layer` 都算上（T1 实测）
 * 本仓大量页面用 `layer.msg('「上传截图」尚未开放')` 之类的**提示层**：实测探针在 `/orders`
 * 的 30 条页内控件上开出了 **12 个层**，其中 **10 个**就是这种提示层；`/`（首页）14 条控件
 * 开出 **12 个**、全是提示层。它们的特点是——**没有关闭按钮**（layui 的 `layer.msg` 把
 * `closeBtn` 关掉）、**到点自己消失**（`time: 1500`）、层里**一个控件都没有**。
 * 把几十条这样的提示算成"打开过的层"，`layersOpened` 会涨到几十、而 `layerControlsClicked`
 * 仍是那几条 ⇒ 读数被淹掉，读的人分不清"层内没控件"与"层内枚举器坏了"。
 *
 * ## 判据本身（⛔ 用**结构**，不用标题文本 / 类名关键词猜）
 * **该层里有可点的关闭按钮**（`LAYER_CLOSE`）⇒ 它是一个「会一直留在屏幕上、需要显式关掉」的层。
 * layui 的行为出处（读源码 `src/frontend/public/layui/layui.js` 的 `vessel()`）：只有
 * `closeBtn` 为真时才渲染 `<a class="layui-layer-close">`；而 `layer.msg` / `layer.load`
 * 在 `x.open` 里把 `closeBtn` 置 0 ⇒ 它们一定没有这个节点。
 * ⚠️ 实测佐证（探针 §3 的 DOM 原文）：那条提示层是
 * `<div class="layui-layer layui-layer-dialog layui-layer-border layui-layer-msg layui-layer-hui" …>
 *  <div class="layui-layer-content">「上传截图」尚未开放</div><div class="layui-layer-setwin"></div></div>`
 * ——`.layui-layer-setwin` **是空的**、`.layui-layer-close` 个数 **0**。
 *
 * ⚠️ **代价（诚实写下来）**：一个**没有关闭按钮、但层里有控件**的层会被漏掉（它不算"打开过"、
 * 因此不会被枚举）。今天的实测里没有这种层；若将来出现，本判据会**静默少扫一层**——
 * 这正是 spec §6 要记进残差的那一条，所以它必须写在实现旁边而不是只写在文档里。
 */

/* ——— ③b-1 驱动前的探活与「同文档」清场 ——— */

/**
 * island iframe / `#app` 现在还在不在（用**短超时**探活，不抛）。
 *
 * ⛔ **每条控件前都要探**（任务书）：点击可能触发 SPA 换页，iframe 被整个换掉，
 * 此时上一条的 `frameLocator` 已经指向一个**不存在的文档**。
 */
async function islandPresent(page: Page): Promise<boolean> {
  const frame = page.frameLocator(ISLAND_IFRAME)
  const iframeCount = await page.locator(ISLAND_IFRAME).count()
  if (iframeCount === 0) return false
  const appCount = await frame.locator(CONTROL_SCOPE).count()
  return appCount > 0
}

/**
 * 点击前把**残留弹层**关掉。
 *
 * ⛔ **注意 `layer.closeAll()` 的既有教训 `E-0039`**：它对**另一个宿主文档**是合法 no-op
 * （换宿主文档后 `layer` 变量指向的已经不是当前页面的 layui 实例）⇒ 拿它当"关掉了"的证据会撒谎。
 * **本函数的前提是：我们从头到尾在同一个 island iframe 文档里操作**（`page.goto` 之间不清），
 * 所以 `layer` 就是**这个文档的** layui；而任何一次**导航**之后我们都会重新 `goto` + 重新枚举，
 * 清场也随之重来。⛔ 若将来把驱动改成"跨文档点击"，这个前提就破了、这里会静默失效。
 */
async function closeLayers(page: Page): Promise<void> {
  try {
    await page
      .frameLocator(ISLAND_IFRAME)
      .locator(CONTROL_SCOPE)
      .evaluate(() => {
        const layui = (window as unknown as { layui?: { layer?: { closeAll?: (t?: string) => void } } }).layui
        layui?.layer?.closeAll?.()
      })
  } catch {
    /* 文档正在被换掉时这里会抛——不判红：真正的判据是下一次「iframe 还在不在」的探活。 */
  }
}

/* ——— ③b-2 从 DOM 枚举候选控件（**不许手工登记**，spec §3.2） ——— */

/** 一条可驱动的候选控件：身份串 + 它在页面上的**审计标记值**（驱动时按标记点它）。 */
type ControlEntry = {
  identity: string
  tag: string
  /**
   * 枚举那一刻，它**矩形中心的最上层节点**是不是它自己或它的后代（= 浏览器的命中测试）。
   * ⛔ 这不是"多余信息"：`/orders` 的操作列是 layui 的 `fixed: 'right'`，同一颗按钮会被
   * **克隆**一份渲染进 `.layui-table-fixed-r`，人真正点到的是克隆那一份；主表那一颗的矩形中心
   * 落在固定列的蒙层底下（T7 执行期实测，见 `CONTROL_HITTABLE_*` 的注释）。
   * 驱动时**优先挑可命中的那一份**——否则每条都要白等一次动作超时。
   */
  hittable: boolean
  /** 命中测试落在谁身上（给 `clickFailures` 当证据：克隆体 / 蒙层 / 视口之外都会点名）。 */
  top: string
  /**
   * 枚举那一刻，它矩形中心的**横坐标**在视口内（纵向可由 `scrollIntoViewIfNeeded` 补上）。
   *
   * ⛔ 为什么单独记这一条（T7 执行期实测，是"改点可命中的那一份"能落地的关键）：
   * `/orders` 的操作列 `fixed: 'right'` 会把同一颗按钮渲染两份——主表那份在
   * `left ≈ 1505–1595`（iframe 视口只有 **1204** 宽，够不着），克隆那份在 `.layui-table-fixed-r`
   * 里、贴着右边缘（横向一定在视口内）。**纵向在视口外的克隆**（实测 28 条）Playwright 一点就
   * 自动滚上来了，所以判"人点得到"时横向才算硬伤。挑选目标时按「枚举时可命中 > 横向在视口内 >
   * 其余」排序，就能把可点的克隆排在够不着的主表副本前面。
   */
  inViewportX: boolean
  /** 矩形是 0×0（`display:none` / `layui-hide`）——这类**不要**烧命中轮询预算，直接判不可点。 */
  zeroSize: boolean
}

/**
 * 一条候选控件在**驱动时的精确选择器**：按枚举那一刻打上的审计标记取值。
 *
 * ⛔ **不在枚举里就把选择器算好、而是集中在这一处派生**，理由与 `markedLocator()` 相同：
 * 「怎么按标记找元素」只能有一份实现（`E-0021`）。标记值本身带前缀
 * （`CONTROL_DRIVE_TAG_PREFIX_PAGE` / `..._LAYER`），而属性名也分两套
 * （`CONTROL_DRIVE_ATTR` / `CONTROL_DRIVE_ATTR_LAYER`）⇒ 这里按前缀选属性，
 * 调用方只给一条 `ControlEntry`、不必知道自己拿的是页内还是层内。
 */
function driveSelectorOf(entry: ControlEntry): string {
  return `[${driveAttrOf(entry)}="${entry.tag}"]`
}

/**
 * 一条控件该用**哪个标记属性**去找：按 `tag` 是否带层前缀判。
 * ⛔ 判据只看前缀（`CONTROL_DRIVE_TAG_PREFIX_LAYER`）——它同时是"这条来自层内枚举"的标记，
 * 两件事本来就是同一件（见 `EnumerateOptions.tagPrefix` / `driveAttr`）。
 */
function driveAttrOf(entry: ControlEntry): string {
  return entry.tag.startsWith(CONTROL_DRIVE_TAG_PREFIX_LAYER)
    ? CONTROL_DRIVE_ATTR_LAYER
    : CONTROL_DRIVE_ATTR
}

/** 一次枚举的结果。 */
type ControlSnapshot = {
  /**
   * **可驱动**的候选（`#app` 里带 `layui-btn` 类、且不是路由链接）——驱动循环只走这一列。
   * ⚠️ 每条都带一个**页面上的审计标记**（`CONTROL_DRIVE_ATTR`），驱动时按标记点那个元素
   * （⛔ 不要按 `.nth(i)`：`locator()` 与 `querySelectorAll` 的命中顺序在 layui 固定列克隆体上分叉）。
   */
  drivable: ControlEntry[]
  /** 被排除的路由链接的身份串（**留痕**用：排除规则若把候选全吃了，报告里要看得出来）。 */
  routeLinks: string[]
  /** 候选（可驱动 + 路由链接）**引用到的元素个数**——**N3 覆盖下界的分母**（spec §3.2 的五条选择器）。 */
  candidateTotal: number
  /** 候选里**不在驱动选择器命中面内**的那些（身份串 + 为什么）——⛔ 显式留痕，不许静默漏点。 */
  skipped: { identity: string; reason: string }[]
}

/**
 * 一次枚举快照里**候选、但没被驱动**的条目（路由链接 + 命中候选选择器却不带 `layui-btn` 的）——
 * 页内与层内**共用这一份**（A）。
 *
 * ⛔ 为什么必须共用（`E-0021`）：层内原来传 `recordNonBtnSkipped:false`，而且**没人读**
 * `snapshot.skipped` / `snapshot.routeLinks` ⇒ 层里"命中候选选择器但不带 `layui-btn`"的控件、
 * 以及层内路由链接，**既不驱动也不逐条记账**——那正是母规范 §3.2 明令禁止的「静默少点」。
 * 现在两条路径都走本函数，报告里进**同一个** `notDriven`（层内那些在 `reason` 前缀里带
 * `（层内 <surface>）`，归属不丢）。
 *
 * ⚠️ 判据的**分账恒等式**（页内与层内都成立）：
 * `candidateTotal === drivable.length + routeLinks.length + skipped.length`
 * （`enumerateCandidateControls()` 把按元素去重后的候选恰好分进这三类）⇒ 谁把 `skipped` 丢掉，
 * 层内那条 `layers[].candidateTotal` 的比对就会响。
 */
function notDrivenOf(snapshot: ControlSnapshot): { identity: string; reason: string }[] {
  return [
    ...snapshot.routeLinks.map((identity) => ({ identity, reason: ROUTE_LINK_REASON })),
    ...snapshot.skipped,
  ]
}

/**
 * `enumerateCandidateControls()` 的作用域参数（T1 扩张：同一套枚举器既扫页内、也扫层内）。
 *
 * ⛔ **为什么参数化而不是复制一份**：层内控件与页内控件的「候选选择器 / 身份串 / 路由链接排除 /
 * 可驱动判据」**必须是同一份实现**——`E-0021`：同一事实两套判据必然漂移，而漂移只表现为
 * **静默少点**（报告上看不出来）。这里变的只有「在哪个根下面找」与「标记值用哪个前缀」。
 */
type EnumerateOptions = {
  /** 枚举的**根**（Playwright 侧）：页内是 `#app`，层内是**那一层**。 */
  root: Locator
  /**
   * 在页面上下文里，从根再往下找选择器时用的**作用域选择器**。
   * 页内是 `#app`（即根本身），层内是 `.layui-layer`（根就是层元素本身）。
   */
  scope: string
  /**
   * 审计标记值的**前缀**。⛔ 页内与层内**必须用不同前缀**：两边的标记值都从 0 开始数，
   * 而 `markedLocator()` 是按标记值在**整个 island 文档**里找元素的（层不在 `#app` 里、
   * 没法用作用域把两者分开）⇒ 同值标记会**互相撞车**（strict mode 命中两个元素，
   * 每一次驱动都白等到超时，症状是"层内控件全都点不动"）。
   */
  tagPrefix: string
  /**
   * 打标记用的**属性名**。⛔ 页内与层内用**两个不同的属性**（理由见 `CONTROL_DRIVE_ATTR_LAYER`）：
   * 层内阶段每次枚举前要"清掉上一轮**层内**标记再重打"，若与页内共用一个属性，那一清会把
   * **页内快照**也一起废掉（页内那些标记是驱动循环赖以继续的锚点）。
   */
  driveAttr: string
  /** 是否记录「命中候选选择器、但不带 `layui-btn`」的条目（层内已另行逐条记账，见调用方）。 */
  recordNonBtnSkipped: boolean
}

/**
 * 枚举页面上的候选控件。**选择器与身份串的唯一出处都在 `fixtures/selectors.ts`**
 * （`CONTROL_SELECTORS` / `controlIdentity`），⛔ 这里不复制第二份"清单"。
 *
 * ## 身份串为什么在浏览器里又写了一遍（以及它凭什么不会漂移）
 * `page.evaluate()` 的函数体是在**页面上下文**里执行的，**拿不到 Node 侧的闭包**
 * （`controlIdentity` 不在页面里）⇒ 要读 DOM 属性就只能把那段形状**内联**进 evaluate。
 * ⛔ 内联的那份**不是**第二套判据：本函数**每次调用**都会把**同一个元素**分别喂给
 * 「内联实现」与「夹具层实现」，**逐元素断言两个串相等**，不等就抛明确错误
 * （`E-0021` 要消灭的是"两份会各自漂移的实现"，不是"一份实现被序列化两次"）。
 *
 * ⛔ **排除路由链接**（spec §2 的 C 条）：点它会**跳页**，那是 P 那一层（`reachable`）覆盖的，
 * 算进控件层会把「跳页」误记成「控件点了 403」。
 * 判据用**结构**、⛔ 不用 class / 文本关键词猜：`<a>` 且 `href` 是**真实的页面路径**
 * （以 `/` 开头、或以本站 `FRONTEND_URL` 开头）⇒ 路由链接。`href=""` / `href="#…"` /
 * `href="javascript:…"` 都是**页内控件**的写法（本仓 island 页工具栏普遍写 `javascript:void(0)`，
 * 它们点了不换页，正是本层要驱动的对象）。
 *
 * ⚠️ 返回的 `routeLinks` **只用于留痕**：报告里若某页排除了若干条，要能看出"排除了什么"
 * （一个把候选控件全吃掉的排除规则，症状与「这页本来就没有控件」一模一样——那是**静默**失效）。
 */
async function enumerateCandidateControls(
  page: Page,
  origin: string,
  opts: EnumerateOptions,
): Promise<ControlSnapshot> {
  const result = await opts.root.evaluate(
      (root, args) => {
        const [selectors, fixtureBody, originUrl, driveAttrName, scopeSel, tagPrefix, recordNonBtn] =
          args as [readonly string[], string, string, string, string, string, boolean]
        // ⛔ 内联的身份串（形状 = `fixtures/selectors.ts` 的 `controlIdentity()`）：
        //    每次调用都会与夹具层实现逐个元素对拍（见函数上方注释），所以它漂移不了。
        const inlineIdentity = (el: Element): string => {
          const cls = (el.getAttribute('class') ?? '').trim().split(/\s+/).filter(Boolean).slice(0, 2).join('.')
          const act = el.getAttribute('data-act') ?? el.getAttribute('lay-event') ?? ''
          const text = (el.textContent ?? '').trim().replace(/\s+/g, '').slice(0, 12)
          return `${el.tagName.toLowerCase()}.${cls}#${act}#${text}`
        }
        const isRouteLink = (el: Element): boolean => {
          if (el.tagName !== 'A') return false
          const href = el.getAttribute('href')
          if (!href) return false
          return (
            href.startsWith('/api/') ||
            href.startsWith(originUrl) ||
            (href.startsWith('/') && !href.startsWith('//'))
          )
        }
        // 枚举的根：页内就是 `#app` 本身；层内是那一层的根。两者都走 `scopeSel` 这一段，
        // ⛔ 不要在这里写两个分支（那份"页内一份、层内一份"就是 `E-0021` 要消灭的漂移）。
        const scopeRoot: Element =
          scopeSel === '' ? (root as unknown as Element) : (root.querySelector(scopeSel) ?? (root as unknown as Element))
        // 按**元素**去重（`CONTROL_SELECTORS` 里有重复覆盖），并保留 DOM 顺序。
        const all = new Set<Element>()
        for (const sel of selectors) {
          for (const el of scopeRoot.querySelectorAll(sel)) all.add(el)
        }
        const elements = [...all]
        const inline = elements.map((el) => inlineIdentity(el))
        // 夹具层的实现（`controlIdentity`）：把函数体原样喂进页面上下文执行，**与内联那份对拍**。
        // ⚠️ 函数体只用参数、不用闭包 ⇒ 序列化进页面上下文执行是安全的（⛔ 不要给它加外部引用）。
        const fixtureFn = (0, eval)(`(${fixtureBody})`) as (node: Element) => string
        const fixture = elements.map((el) => fixtureFn(el))
        const mismatch = inline
          .map((id, i) => ({ index: i, inline: id, fixture: fixture[i] }))
          .filter((it) => it.inline !== it.fixture)
        if (mismatch.length > 0) {
          throw new Error(
            `控件身份串的两份实现漂移了（${mismatch.length} 条）：` +
              JSON.stringify(mismatch.slice(0, 3)) +
              '\n这是**响亮失败**（不是静默少记）：请把本函数里的内联实现与 ' +
              '`fixtures/selectors.ts` 的 `controlIdentity()` 对齐。',
          )
        }
        // ⛔ **给每条可驱动候选打一个临时标记属性**，驱动时按标记点它（理由见 `CONTROL_DRIVE_ATTR`）。
        //    标记值 = `<前缀><下标>`：⛔ 前缀不是装饰——页内与层内都从 0 数，而 `markedLocator()`
        //    是按标记值在**整个 island 文档**里找元素的（层不在 `#app` 里、作用域分不开两者）
        //    ⇒ 同值标记会互相撞车（strict mode 命中两个元素，症状是"层内控件全都点不动"）。
        const driveAttr = driveAttrName
        // 命中测试落在谁身上（诊断用）：`<tag>#<id>.<class 前 2 个>`，视口之外明确写出来。
        const describeTop = (el: Element | null): string => {
          if (!el) return 'null（矩形中心在视口之外）'
          const cls = (el.getAttribute('class') ?? '').trim().split(/\s+/).filter(Boolean).slice(0, 2).join('.')
          return `${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}${cls ? '.' + cls : ''}`
        }
        const drivable: {
          identity: string
          tag: string
          hittable: boolean
          top: string
          inViewportX: boolean
          zeroSize: boolean
        }[] = []
        const routeLinks: string[] = []
        const skipped: { identity: string; reason: string }[] = []
        let tagSeq = 0
        for (let i = 0; i < elements.length; i++) {
          if (isRouteLink(elements[i])) {
            routeLinks.push(inline[i])
            continue
          }
          if (!(elements[i] as Element).classList.contains('layui-btn')) {
            if (recordNonBtn) {
              skipped.push({
                identity: inline[i],
                reason: '候选选择器命中、但不是 `layui-btn`（驱动只点带该类的页内控件）',
              })
            }
            continue
          }
          const tag = `${tagPrefix}${tagSeq++}`
          elements[i].setAttribute(driveAttr, tag)
          // 命中测试（枚举那一刻、无弹层时的读数）：矩形中心的最上层节点是不是它自己 / 它的后代。
          // ⚠️ 这里**不** `scrollIntoView`：要的就是"它现在在哪、谁能被点到"这个事实；驱动前另有
          //    有界命中轮询（`hitTestUntil`）。
          const box = elements[i].getBoundingClientRect()
          const topEl = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2)
          const hittable = !!topEl && (topEl === elements[i] || elements[i].contains(topEl))
          const cx = box.left + box.width / 2
          drivable.push({
            identity: inline[i],
            tag,
            hittable,
            top: describeTop(topEl),
            inViewportX: cx >= 0 && cx <= window.innerWidth,
            zeroSize: box.width === 0 || box.height === 0,
          })
        }
        return { drivable, routeLinks, candidateTotal: elements.length, skipped }
      },
      [
        CONTROL_SELECTORS,
        controlIdentity.toString(),
        origin,
        opts.driveAttr,
        opts.scope,
        opts.tagPrefix,
        opts.recordNonBtnSkipped,
      ] as [readonly string[], string, string, string, string, string, boolean],
    )
  return result
}

/* ——— ③b-3 逐条驱动（每条控件单独一次真实点击） ——— */

/**
 * 等这一页的候选控件**渲染出来**（最多 `timeout` 毫秒）。
 *
 * ⛔ **为什么必须等**（T3 执行期实测踩到，两次跑都因此漏掉 D-0145）：`#app` 是 island 宿主页
 * （`layui-island/page.html`）里的**静态**节点，iframe 一开就 `attached`；而 `page.ts` 是
 * **动态 import + `await` 取数**之后才把按钮塞进去的（`04-smoke` 的注释记过同款竞态：
 * "iframe 在、但 `#app` 一直空"）⇒ 立刻枚举会读到 **0 条候选**，而报告上它与
 * "这页结构上本来就没有控件"**长得一模一样**——那会把「探针没等」伪装成「这页没有控件」。
 * 实测：`/orders` / `/orders/inside-hall` / `/orders/recycle` 三页在毫秒级枚举下都是 0 条；
 * 等一下就有（行内 `.jsr` 指派按钮正是这么漏掉的）。
 *
 * ## T7 执行期实测订正（原来的等待是**空转**的）
 * 原实现 `expect.poll(() => candidateCount(page), …)` 里的 `candidateCount()` 数的是
 * **审计自己打的标记**（`CONTROL_DRIVE_SELECTOR` = `[data-tt-audit-drive]`），而标记是在
 * `enumerateCandidateControls()` 里才打上的 ⇒ **枚举之前那个数恒为 0**，这个 `expect.poll`
 * 必然等满 `timeout` 才失败返回 `false`。也就是说：
 *  - 它实际上等价于"**每页无条件睡满 5 秒**"（`rendered` 因而**恒为 false**，`pagesNeverRendered`
 *    只在 `candidateTotal === 0` 时才会被填，所以报告上看不出来）；
 *  - 而它**刚好**把那 5 秒当成了"等异步渲染"的窗口，所以 `D-0145` 那条仍被点到——**侥幸**。
 * T7 改成：谓词数**候选选择器**（`CONTROL_CANDIDATE_SELECTOR`），再要求**连续若干次采样不变**
 * 且**至少过了 `CANDIDATE_MIN_WAIT_MS`**。两条都要，理由不同：
 *  - **"数 > 0"不够**（实测）：`/orders` 先渲染出工具栏那约 20 条，行内按钮晚得多
 *    （同一页最终 235 条）⇒ 只看"有没有"会在 20 条时就开始枚举，**静默少点 200 多条**；
 *  - **"稳定"也不够**：工具栏那一档本身也可能连续几次采样不变（观察窗口撞上它就误判收官），
 *    所以再加一个最短观察窗，把"先冒出来的一撮"熬过去。
 *
 * ⚠️ 轮询**按候选控件数**，⛔ 不用 `waitForLoadState('networkidle')`（页面上有轮询时会白等到超时）。
 * ⚠️ **等不到不算失败**：返回"没渲染出来"，由调用方把"这页没有候选控件"记进
 * `pagesWithNoControls`（含这个观察窗口），而不是静默当成"这页没有控件"。
 */
async function waitForCandidates(page: Page, timeout: number): Promise<boolean> {
  const started = Date.now()
  const deadline = started + timeout
  let last = 0
  let stable = 0
  while (Date.now() < deadline) {
    const n = await candidateCount(page)
    stable = n > 0 && n === last ? stable + 1 : 0
    last = n
    if (stable >= CANDIDATE_STABLE_SAMPLES && Date.now() - started >= CANDIDATE_MIN_WAIT_MS) {
      return true
    }
    await page.waitForTimeout(CANDIDATE_POLL_MS)
  }
  // 到点了：观察窗口里渲染出来过（`last > 0`）也算"渲染出来了"，只是没能稳定下来。
  return last > 0
}

/**
 * 这一页的驱动结果（`driveControlsOnPage()` 的返回值）。
 */
type PageDriveResult = {
  /** 这一页是否被成功访问（`goto` 落定、且 `#app` 在）。 */
  visited: boolean
  /**
   * `goto` 落定后的**终态路径**（被路由守卫弹回时**不是**请求的那条）。
   *
   * ⭐ 本键是 `instancePageUnreachable` 那一档的**事实来源**（评审裁定 a，2026-09-26）：
   * 「实例解析到了、但页面被弹回 `/`」只认这个**导航事实**，⛔ 不认 `visited`
   * ——否则把 `visited` 改坏时两侧会一起变、判据自洽（`E-0083` 的「变异连期望一起改」形态）。
   */
  settledPathAtEntry: string
  /** 候选控件在观察窗口内渲染出来了（`false` + 候选 0 ⇒ 见 `pagesWithNoControls` 的语义）。 */
  rendered: boolean
  /** 候选控件条数（spec §3.2 的五条选择器、按元素去重）——**N3 的分母**。 */
  candidateTotal: number
  /** 其中**可驱动**的条数（不含路由链接、且在驱动选择器命中面内）。 */
  drivable: number
  /**
   * 本次驱动计划覆盖的**可驱动候选总数**（= 驱动计划所依据的那次枚举的 `snapshot.drivable.length`）。
   * ⛔ 它是 N3 的**分母**：驱动数 + 去重数 + 点不动的副本数必须 ≥ 它，否则就是**静默少点**。
   */
  drivableTotal: number
  /** 因为**同一身份串已驱动够 N 个**而没驱动的重复元素条数（T7 的去重取舍，⛔ 必须留痕）。 */
  dedupedDuplicates: number
  /** 因为**同一身份串另有可命中的一份**而没驱动的副本条数（layui 固定列克隆体那一类）。 */
  dedupedUnhittable: number
  /** 上面两类"没驱动"的**逐条明细**（⛔ 不许只给计数、不给条目）。 */
  deduped: { identity: string; reason: string }[]
  /** 候选里**没被驱动**的那些（路由链接 + 驱动选择器不含的）——⛔ 显式留痕，不许静默漏点。 */
  skipped: { identity: string; reason: string }[]
  /** 驱动过的控件身份串（含点不动的：那也算「点过」，见 N3）。 */
  clickedIdentity: string[]
  findings: AuditFinding[]
  clickFailures: { page: string; control: string; reason: string }[]
  /** 该页被排除的路由链接（留痕用）。 */
  routeLinks: string[]
  /** 该页因为某种原因**没能驱动**（例如 iframe 一直没出来、或导航被弹回）——探针缺陷，要留痕。 */
  enumerationFailed: string
  /** T1 填：**层内**控件被驱动过的条数（含点不动的，口径同 `clickedIdentity`）。 */
  layerControlsClicked: number
  /** T1 填：层内**枚举到的可驱动候选总数**（`layerControlsClicked` 的分母）。 */
  layerDrivableTotal: number
  /** T1 填：层内因**同一身份串已驱动够 `DEDUPE_N` 个**而没驱动的重复元素条数。 */
  layerDedupedDuplicates: number
  /** T1 填：层内因**同身份串另有可命中的那一份**而没驱动的副本条数。 */
  layerDedupedUnhittable: number
  /** T1 填：**本次驱动开出来的层**（按标题去重前的逐层读数，供 L2 的分子/分母用）。 */
  layers: LayerRecord[]
  /** T1 填：层内点不动的控件（`page` / `surface` / `control` / `reason`）。 */
  layerClickFailures: { page: string; surface: string; control: string; reason: string }[]
  /** T1 填：`type:2` iframe 层被跳过的**层**数（spec §3 第 6 条）。 */
  layersSkippedIframe: number
  /** T1 填：被跳过的 iframe 层的 `surface` 身份（G：⛔ 不只记计数）。 */
  skippedIframeLayers: { page: string; surface: string }[]
  /**
   * T1 填：**同一次点击开出来的其余兄弟层**被跳过的**层**数（D：原名
   * `layersNestedSkipped`，名实不符已改）。⛔ 它**不是**"层里再开的层"——真嵌套既不计数也不驱动，
   * 见 `LayerStageResult.coOpenedSkipped` 与 spec §6 / §9。
   */
  layersCoOpenedSkipped: number
  /** T1 填：这一页上「该层的关闭按钮点不动 / 关不掉」的次数（进 `notes` 留痕，⛔ 不判红）。 */
  layerCloseFailures: number
}

/**
 * **一层**的读数（`PageDriveResult.layers` 的一条）。
 *
 * ⛔ 它是 spec §4 的 **L2** 判据（「某个层里枚举到了控件、却一条都没点过 ⇒ 红」）的取证：
 * 判据必须能**逐层**问「`drivable > 0 && clicked === 0`」，⛔ 不能只看职位级的总数
 * （那是"总数不为零"的恒真形状，`E-0024`）。
 */
type LayerRecord = {
  /** 该层是在哪一页被点开的。 */
  page: string
  /** `surface` 归属键（`layer:<层标题>` / `layer:untitled`）。 */
  surface: string
  /**
   * 层内**候选**总数（`ControlSnapshot.candidateTotal`）——A 新增。
   * 它是层内「候选 → 可驱动 / 未驱动（路由链接 + 非按钮候选）」分账的**分母**：
   * `candidateTotal === drivable + notDriven`（见 `notDrivenOf()` 的注释）。
   */
  candidateTotal: number
  /** 层内枚举到的**可驱动**控件条数。 */
  drivable: number
  /** 层内枚举到、但**没有驱动**的候选条数（`notDrivenOf(snapshot).length`，与页内 `notDriven` 同口径）。 */
  notDriven: number
  /** 层内**实际驱动过**的条数（含点不动的；口径同 `clickedIdentity`）。 */
  clicked: number
}

/**
 * 一次「层内阶段」的读数（`driveLayerStage()` 的返回值）。
 *
 * ⚠️ 它只描述**刚刚那一次点击**开出来的层；调用方把它累加进 `PageDriveResult`。
 */
type LayerStageResult = {
  /** 开出来的层（**只算"会留在屏幕上"的那些**，判据见 `visibleLayers()` 上方的注释）。 */
  layers: LayerRecord[]
  /**
   * 本次点击**新开出来的层数**（= `visibleLayers()` 里 `times > beforeTimes` 的条数，含后面被跳过
   * 的 iframe 层 / 同批开的兄弟层）——K 新增。
   *
   * ⛔ 它的唯一用途：调用方靠它判「这次点击**真的开过层**了吗」——只有开过层，才可能发生
   * 「页内节点被 `layer.open({type:1, content:$(node)})` 搬进层」这件事，才需要重新枚举页内快照。
   * 原来调用方**无条件**重新枚举（每个驱动过的页内控件一次，代价远超实际开层次数）。
   */
  opened: number
  /** 层内枚举到的可驱动总数。 */
  drivableTotal: number
  /** 层内驱动过的条数。 */
  clicked: number
  /** 层内"没驱动"的两类账（必须逐条留痕，⛔ 不许静默少点）。 */
  dedupedDuplicates: number
  dedupedUnhittable: number
  deduped: { identity: string; reason: string }[]
  /**
   * 层内**候选但没驱动**的条目（路由链接 + 非 `layui-btn` 候选）——A 新增。
   * 与页内 `notDriven` **同一口径**（同一份 `notDrivenOf()`），调用方把它并进页面的
   * `result.skipped`，最终落在报告同一个 `notDriven` 键里。
   */
  notDriven: { surface: string; identity: string; reason: string }[]
  /** 层内点不动的控件。 */
  clickFailures: { surface: string; control: string; reason: string }[]
  /** 层内控件触发的 403 发现（**已经带 `surface`**；由调用方合并进页面级 `findings`）。 */
  findings: AuditFinding[]
  /** `type:2` iframe 层被跳过的层数。 */
  skippedIframe: number
  /**
   * 被跳过的 iframe 层的 **`surface` 身份**（G）——⛔ 不只是计数：
   * 「跳过的是哪一层」必须能在报告里点出来（今天无实例，但跳过的判据是
   * 「类名含 `layui-layer-iframe` **或** 内容里有 `<iframe>`」的**并集**，
   * 一个同文档 `type:1` 层里嵌了 iframe 也会被整层跳过 ⇒ 只记计数就没人能发现误杀）。
   */
  skippedIframeLayers: string[]
  /**
   * 本次点击里除目标层之外**同批开出来的其余层**被跳过的层数（D：由
   * `nestedSkipped` 改名而来）。
   *
   * ⛔ **名实相符**：它数的是 `fresh.slice(1)`（**同一次点击开出来的兄弟层**，如弹层旁再叠一个
   * `layer.msg`），⛔ **不是**「层里再开的层」。**真嵌套**（层内控件点出来的层）既不计数也不驱动
   * ——`driveControlsInLayer()` 不递归，那些层只可能被收尾 `closeAll()` / 重新导航吞掉；
   * 这一条如实写进 spec 残差（§6 / §9），⛔ 不许拿本键当"嵌套层已被覆盖"的证据。
   */
  coOpenedSkipped: number
  /** 关层失败的次数（`.layui-layer-close` 点不动 / 点了没关掉 ⇒ 退到重新导航）。 */
  closeFailures: number
  /**
   * 本次层内阶段**是否重新导航过该页**（关层关不掉时的最后一道，spec §3 第 4 条）。
   *
   * ⛔ 调用方必须用它**作废页面快照**：重新导航 = 换文档 ⇒ 页内控件的**审计标记全部消失**，
   * 而驱动循环里那个「条数比对」判的是 `markedCount() !== snapshot.drivable.length`——
   * 重新导航后标记数是 0、而快照长度通常 > 0 ⇒ 它**恰好**也会重新枚举。但那条依赖是**偶然**的
   * （若这一页的可驱动快照本来就只有 0 条，条数比对就不会触发，后面每条都白等超时）。
   * ⇒ 显式把这件事报出去，让调用方**无条件**重新枚举。
   */
  renavigated: boolean
}

/**
 * 单条控件的**点击硬预算**（毫秒）。
 *
 * ⚠️ Playwright 未配 `actionTimeout` 时默认**不限时**（直接挂到用例超时）⇒ 必须显式给。
 * ⚠️ **T7 从 5s 下调到 2s 的依据**（T3 的原始数据）：`/orders` 一页有 80 条点不动的控件，
 * 每条烧满 5s ≈ 400s，是**整轮 43.5 分钟**里最大的一块。而**能点的**控件实测 ≤1s 就完成
 * （`a.jsr` 那一类打开弹层 + 取名单也在 1s 内）⇒ 2s 既不会误杀"慢但能点"的，又把真正点不动的
 * 每条预算砍掉 60%。⚠️ 这条只是**兜底**：绝大多数"点不动"在更早的 `isVisible()` /
 * 有界命中轮询（`HIT_TEST_BUDGET_MS`）就被快速记下了，根本走不到这 2s。
 */
const CLICK_TIMEOUT_MS = 2_000

/**
 * 驱动前的**有界命中轮询**预算（毫秒）：先 `scrollIntoViewIfNeeded()`，再反复问
 * `document.elementFromPoint(矩形中心)` 命中的是不是控件自己 / 它的后代。
 *
 * ⛔ 这就是本仓 `02-golden-path` 的 `clickRowAction()` 配方（**有界** `elementFromPoint` 轮询 +
 * 把"矩形中心的最上层节点是谁"打出来当诊断），本网照抄，理由有两条：
 *  ① **它比"等 Playwright 动作超时"快一个数量级**：实测一次 `elementFromPoint` ≈ 数毫秒，
 *     而等点击超时要按 `CLICK_TIMEOUT_MS` 整段烧；
 *  ② **它给得出真因**：命中失败时能点名"最上层是哪个节点"（克隆体 / 蒙层 / 视口之外），
 *     而 Playwright 的超时错误只说"元素拦了指针事件"。
 * ⚠️ 必须**先滚进视口再判**：视口之外时 `elementFromPoint` 返回 `null`（`02` 的注释记过），
 * 先滚再判才是"人点得到的那个点"。
 * ⚠️ 有界轮询而不是量一次：layui 关层有动画，上一层的 `div.layui-layer-shade` 会在弹层消失后
 * 再盖一小会儿（`02` 实测踩到）⇒ 量一次会把"正在消失的遮罩"判成"点不动"。
 */
const HIT_TEST_BUDGET_MS = 800

/** 有界命中轮询的采样间隔（毫秒）。 */
const HIT_TEST_POLL_MS = 150

/**
 * 挑选目标时**最多对几个同身份串候选**做命中探测（第一个用 `HIT_TEST_BUDGET_MS`、
 * 其余用下面这个更短的预算）。
 *
 * ⛔ 上限必须存在：`/orders` 上「操作列图标」那一组有 **160 条同身份串**（80 条够不着的主表副本 +
 * 80 条可点的克隆），逐个探会把一页拖回 T3 的时长。排序（可命中 > 横向在视口内）保证**第一个
 * 通常就是克隆**，探测一次就中；只有整组都不可命中时才多探两个、然后**退回点第一个**。
 */
const TARGET_PROBE_MAX = 3

/** 第 2 个及以后候选的命中探测预算（毫秒）——它们只是"备胎"，不该烧满第一个的预算。 */
const HIT_TEST_PROBE_MS = 300

/**
 * 同一页、**同一身份串最多驱动几个元素**（T7 的去重上限）。
 *
 * ## 为什么允许去重
 * T3 实测：`/orders` 上 13 行各有一颗「指派打手」，**13 条逐个点**，走的是同一条代码路径、
 * 打出同一条 403（报告里那条 `D-0145` 的 `occurrences=13` 就是这么来的）。逐行重复只买来
 * "次数"，不买"覆盖面"。
 *
 * ## 取 2（不是 1、也不是 3）的理由
 *  - **1** 会让"同一身份串里只有第 2 个能点动 / 只有第 2 个触发请求"这类情况**整条漏掉**；
 *  - **2** 保留"至少两个不同实例都被驱动过"这个最小交叉验证，代价是原来的 1/6；
 *  - **3** 在 13 行的页面上只多买 1 次重复，却把预算再抬 50%——本网的瓶颈正是这个预算。
 *
 * ## ⛔ 这条取舍的边界（必须与报告里的 `dedupedDuplicates` 一起读）
 * **同一控件在不同行上可能因数据状态不同而行为不同**（例如"该单已有接单人"才渲染的「清除」、
 * 或只有某一行才有某种状态）⇒ 去重**可能漏掉"只在第 N 行（N > 2）才出现的 403"**。
 * 这是**有意的判别力牺牲**：被牺牲的每一条都逐条记进 `deduped`（并计入 `dedupedDuplicates`），
 * 读报告的人能看见"这一页有 200 条同身份串只点了 2 条"——⛔ 不是静默少点。
 */
const DEDUPE_N = 2

/** 去重那条的统一说明（进 `deduped` 明细，⛔ 不许只给计数）。 */
const DEDUPE_REASON =
  `同一页同一身份串的第 ${DEDUPE_N + 1} 个及以后的重复元素（去重上限 N=${DEDUPE_N}）：` +
  '与已驱动的那几个走同一条代码路径。⚠️ 取舍边界：同身份串在不同行上可能行为不同，' +
  '故可能漏掉"只在第 N 行才出现的 403"（见 DEDUPE_N 的注释）'

/**
 * 「这一份没被驱动，是因为同身份串另有**可命中**的那一份」的原因串。
 *
 * 典型形态（T7 实测）：layui 表格 `fixed: 'right'` 的操作列会**克隆**一份进
 * `.layui-table-fixed-r`；主表那一颗的矩形中心落在固定列的蒙层底下（`elementFromPoint` 命中的是
 * 克隆体），**人真正点到的是克隆那一份**（`02-golden-path` 的 `fixedRightRowOf()` 为同一件事存在）
 * ⇒ 本网改点可命中的那一份，并把这一份记下来。
 */
const UNHITTABLE_COPY_REASON =
  '同一身份串另有可命中的那一份（layui 固定列克隆体）：这一份的矩形中心被别的节点盖住，' +
  '真实点击到不了它 ⇒ 改点了可命中的那一份'

/**
 * **一次**命中测试：矩形中心的最上层节点是不是控件自己 / 它的后代。
 *
 * ⚠️ 与枚举时的 `hittable` 同一个判据，但挑选目标时是在**驱动前**、`scrollIntoViewIfNeeded()`
 * 之后重新量的（枚举那一刻的读数会被"还没滚到"的视口污染）。
 */
async function hitTestOnce(loc: Locator): Promise<{ ok: boolean; top: string }> {
  try {
    return await loc.evaluate((el) => {
      const rect = el.getBoundingClientRect()
      const top = el.ownerDocument.elementFromPoint(
        rect.left + rect.width / 2,
        rect.top + rect.height / 2,
      )
      if (top && (top === el || el.contains(top))) return { ok: true, top: 'ok' }
      if (!top) return { ok: false, top: 'null（矩形中心在视口之外）' }
      const cls = (top.getAttribute('class') ?? '').trim().split(/\s+/).filter(Boolean).slice(0, 2).join('.')
      return {
        ok: false,
        top: `${top.tagName.toLowerCase()}${top.id ? '#' + top.id : ''}${cls ? '.' + cls : ''}`,
      }
    })
  } catch (err) {
    // 元素已经 detach / 标记失效时这里会抛——照样算"没命中"，由调用方处理。
    return { ok: false, top: `evaluate 失败：${String(err).split('\n')[0].slice(0, 120)}` }
  }
}

/**
 * 按**审计标记**精确取那一个元素（⛔ 不按选择器下标：命中顺序在固定列克隆体上分叉）。
 *
 * ⛔ **本函数是「驱动哪一条」的唯一入口**（页内与层内都走它，T1 扩张）：调用方传的
 * `driveSelector` 是在**枚举那一刻**由 `ControlEntry.tag` 拼好的选择器，所以
 * 「身份串 ↔ 要点的元素」的绑定与任何遍历顺序无关。
 * ⛔ **不要再按「页内 / 层内」写两个定位分支**：`markedLocator(page, entry)`
 * 对两边都是同一个形状——标记值本身带前缀（`CONTROL_DRIVE_TAG_PREFIX_*`），
 * 而 `[data-tt-audit-drive="L3"]` 这种值是**全文档唯一**的。
 */
function markedLocator(page: Page, driveSelector: string): Locator {
  return page.frameLocator(ISLAND_IFRAME).locator(driveSelector)
}

/**
 * **有界**命中轮询：先 `scrollIntoViewIfNeeded()`，再反复问"矩形中心命中的是不是它自己 / 后代"。
 *
 * ⛔ 这是本仓 `02-golden-path` 的 `clickRowAction()` 配方（有界 `elementFromPoint` 轮询 +
 * 把"矩形中心的最上层节点是谁"当诊断），本网照抄，理由两条：
 *  ① **比等 Playwright 动作超时快一个数量级**（一次 `elementFromPoint` ≈ 数毫秒）；
 *  ② **给得出真因**——命中失败时点名最上层是哪个节点。
 * ⚠️ 必须**先滚进视口再判**：视口之外时 `elementFromPoint` 返回 `null`。
 * ⚠️ 有界轮询而不是量一次：layui 关层有动画，遮罩会多盖一小会儿（`02` 实测踩到）。
 */
async function hitTestUntil(
  page: Page,
  driveSelector: string,
  budgetMs: number,
): Promise<{ ok: boolean; top: string }> {
  const loc = markedLocator(page, driveSelector)
  try {
    await loc.scrollIntoViewIfNeeded({ timeout: CLICK_TIMEOUT_MS })
  } catch {
    /* 滚不动 / 已 detach：下面的命中判定照样给出结论 */
  }
  const deadline = Date.now() + budgetMs
  let last = ''
  for (;;) {
    const hit = await hitTestOnce(loc)
    if (hit.ok) return hit
    last = hit.top
    if (Date.now() >= deadline) return { ok: false, top: last }
    await page.waitForTimeout(HIT_TEST_POLL_MS)
  }
}

/** 挑选目标的结果：`target` = 要驱动的那一份；`skipped` = 探测后判定"人点不到"的那些同身份串副本。 */
type TargetChoice = {
  /** 要驱动的那一份（⛔ 永远有值：挑不出可命中的就**退回排序第一的那一份**，见 `chooseTarget()`）。 */
  target: ControlEntry
  /** 判定"点不到"、因而**没有驱动**的同身份串副本（它们进 `dedupedUnhittable` 的账）。 */
  skipped: ControlEntry[]
  /** 判定失败时的诊断（最上层节点是谁）——进 `clickFailures` 的 reason 用。 */
  diagnosis: string
}

/**
 * 在**同一身份串**的候选里挑一份"人真正点得到"的目标。
 *
 * ## 它解决什么（T7 的核心修复）
 * T3 实测：`/orders` 的 80 条"点不动"全部是**主表操作列**那颗按钮，Playwright 的完整报错逐字是
 * `<i class="layui-icon layui-icon-edit"></i> from <div class="layui-table-fixed layui-table-fixed-r">…</div>
 * subtree intercepts pointer events` ⇒ **layui 的固定列克隆体挡住了主表原件**（人真正点到的是克隆
 * 那一份，`02-golden-path` 的 `fixedRightRowOf()` 为同一件事存在）。原实现按 DOM 顺序点主表那一份，
 * 于是每条都白等一次动作超时。
 *
 * ## 排序与退回（两条都不是装饰）
 *  - 排序 = 「枚举时可命中(2) > 横向在视口内(1) > 其余(0)」，同分保持原序（`Array.sort` 稳定）。
 *    实测：主表副本的矩形在 `left ≈ 1505–1595`（iframe 视口 1204 宽，**横向够不着**），克隆贴着右边缘
 *    横向一定在视口内 ⇒ 克隆排前面。
 *  - ⛔ **探测不通过 ≠ 跳过**：命中测试与 Playwright 的可操作性判定**不是同一份实现**（实测有
 *    26 条元素命中测试说"被克隆体盖住"、Playwright 却把它点成功了）⇒ 一个都没通过时**退回点排序第一
 *    的那一份**，让它自己去试、或留下真实的失败记录。宁可白等一次超时，也不静默漏掉一条可能的 403。
 */
async function chooseTarget(page: Page, remaining: ControlEntry[]): Promise<TargetChoice> {
  const score = (e: ControlEntry): number => (e.hittable ? 2 : e.inViewportX ? 1 : 0)
  const ordered = [...remaining].sort((a, b) => score(b) - score(a))
  const probedFail = new Set<number>()
  let targetIndex = -1
  let diagnosis = ''
  const limit = Math.min(ordered.length, TARGET_PROBE_MAX)
  for (let i = 0; i < limit; i++) {
    const cand = ordered[i]
    // 0×0（`display:none` / `layui-hide`）不烧命中预算：它连矩形都没有，命中测试没有意义。
    if (cand.zeroSize) {
      diagnosis = diagnosis || '控件不可见（矩形 0×0 ⇒ display:none / layui-hide）'
      probedFail.add(i)
      continue
    }
    const hit = await hitTestUntil(page, driveSelectorOf(cand), i === 0 ? HIT_TEST_BUDGET_MS : HIT_TEST_PROBE_MS)
    if (hit.ok) {
      targetIndex = i
      break
    }
    diagnosis = `真实点击到不了它：矩形中心的最上层节点是「${hit.top}」`
    probedFail.add(i)
  }
  // ⛔ 一个都没通过 ⇒ **退回点第一个**（理由见函数注释）。
  if (targetIndex < 0) targetIndex = 0
  const target = ordered[targetIndex]
  const skipped = ordered.filter((_, i) => probedFail.has(i) && i !== targetIndex)
  return { target, skipped, diagnosis }
}

/**
 * 驱动**一条控件**：清场 → 可见性短路 → 真实点击 → 收这一段里新出现的 403。
 *
 * ⛔ 点击用 `locator.click()`（**真实点击**，走命中测试），⛔ 不用 `dispatchEvent`
 * ——本仓踩过：`dispatchEvent` 绕过了命中测试，「点了没反应」（被弹层遮住 / 元素不可见）**抓不到**。
 *
 * ⚠️ **点不动不算失败**（任务书 / spec §3.3）：弹层遮挡、元素 detach、超时都记进 `clickFailures`
 * 并继续下一条——那不是权限缺陷。本条函数**不吞**异常，由调用方记。
 *
 * ## 快速失败（T7 加，为了不再等 Playwright 的动作超时）
 * `isVisible()` 为假（`display:none` / `visibility:hidden`，本仓 island 页里大量 `layui-hide` 的按钮
 * 属于这一支）⇒ 立刻记，不点。实测 T3 里 `/orders` 有 7 条这种，每条白烧 5s。
 * ⚠️ **只有这一道闸在这里**：真正难缠的"被克隆体盖住 / 视口之外"由**挑选目标**那一步处理
 * （`chooseTarget()` 会改点可命中的那一份），⛔ 不在这里直接判死——理由见 `chooseTarget()`。
 *
 * ## 等待策略：**事件驱动 + 有界兜底**（T3 执行期按实测调过，理由承重）
 * 任务书要的是"点之后等一小段"。本函数先发起一个"这个控件触发的 `/api/**` 响应"的**等待**，
 * 然后与一个**兜底窗口**赛跑：
 *  - 有响应 ⇒ **立刻**收工（不给每条控件白等固定时长；一次点击可能打好几个请求，`race` 会不断续期）；
 *  - 一直没有（例如点开的是纯前端弹层、或请求被拦断成一个"失败请求"事件）⇒ 兜底窗口到点收工。
 * ⚠️ 兜底窗口**不能太短**：`D-0145` 那条 403 是**打开弹层之后**才发的（构造弹层 → 取候选人名单），
 * 窗口太短会**静默漏掉**它——而"漏掉一条已知缺陷"正是本网最不该犯的错（`E-0004` 的空结果教训）。
 * 实测（本机）：每条控件的这次等待 ≈ 0.1–0.6s（多数控件一发请求就结束）。
 * ⛔ **不用 `waitForLoadState('networkidle')`**：它等的是"网络静默"，页面上有轮询/长连接时会白等到超时。
 *
 * ## ⛔ T1：层内控件**不能**走第 ① 步的「清场」
 * `closeLayers()` 是 `layer.closeAll()`——它会把**当前文档里所有层**都关掉，包括我们正要驱动的**那一层**
 * （T1 实测踩到：层内驱动循环里第一次调用它，层连同它的控件一起消失，之后每条都报"点不动"）。
 * ⇒ 层内驱动传 `closeResidualLayers: false`：**不关层**，只做②③④（有界命中 / 真实点击 / 收 403）。
 * 清层的责任**移交给**层内阶段的收尾（`closeOneLayer()` + 关不掉就重新导航），
 * 那条纪律（spec §3 第 4 条：⛔ 不许带着残留层去点下一条页内控件）落在 `driveLayerStage()` 里。
 */
async function clickOneControl(
  page: Page,
  entry: ControlEntry,
  opts: { closeResidualLayers: boolean },
): Promise<{ forbidden: string[]; writes: string[]; issues: string[] }> {
  const before = auditForbidden.length
  const writesBefore = abortedWrites.length
  // T2：写模式的**诊断**流水也在点击前切一刀（口径与 403 完全一样：只收这一段里新出现的）。
  const issuesBefore = writeModeResponses.length
  // ① 清掉残留弹层（`before` 的账已经量过，清场本身不该被算进这条控件）。
  //    ⛔ 层内控件**必须**传 false（见函数头注释：`closeAll()` 会把要驱动的那一层也关掉）。
  if (opts.closeResidualLayers) await closeLayers(page)
  const loc = markedLocator(page, driveSelectorOf(entry))
  // ② **显然不可点的先短路**。
  // ⚠️ `isVisible()` 也包在 try 里（T1）：标记失效 / 命中多个元素时它会抛，而那种情形
  //    按"这一条没点动"处理才对（调用方记进 `clickFailures`），⛔ 不该让整个驱动循环崩掉。
  let visible = false
  try {
    visible = await loc.isVisible()
  } catch (err) {
    throw new Error(
      `取这一条控件时失败（标记失效 / 命中多个元素）：${String(err).split('\n')[0].slice(0, 160)}`,
    )
  }
  if (!visible) {
    throw new Error('控件不可见（display:none / visibility:hidden）⇒ 跳过，未点击')
  }
  // ③ **先起等待、再点击**：点击是同步发请求的，先起等待才不会漏掉那一次响应。
  let settle: () => void = () => {}
  const settled = new Promise<void>((resolve) => {
    settle = resolve
  })
  const onResponse = (res: Response): void => {
    if (res.url().includes(AUDIT_API_PREFIX) && res.request().method() !== 'OPTIONS') settle()
  }
  page.on('response', onResponse)
  try {
    await loc.click({ timeout: CLICK_TIMEOUT_MS })
    // ④ 等到"有响应"或兜底窗口到点（见上方注释）。
    await Promise.race([settled.then(() => page.waitForTimeout(120)), page.waitForTimeout(600)])
  } finally {
    page.off('response', onResponse)
  }
  return {
    forbidden: newForbiddenSince(before),
    writes: newAbortedWritesSince(writesBefore),
    // ⛔ 读模式下 `writeModeResponses` 恒空 ⇒ 这里自然是 `[]`（行为逐字不变）。
    issues: writeModeResponses.slice(issuesBefore),
  }
}

/**
 * 这一页**候选**控件的条数（spec §3.2 的五条选择器并集）——「渲染出来没有 / 渲染齐没有」的读数。
 *
 * ⛔ 与 `markedCount()` **同一事实的两个口径，别混**（T7 执行期实测踩到）：
 *  - 本函数数的是**候选选择器**命中面（枚举之前就能 > 0）⇒ 只能用来**等渲染**；
 *  - `markedCount()` 数的是**审计刚打上的标记**（枚举之前恒为 0）⇒ 只能用来判"DOM 有没有被重渲染"。
 *  T3 的 `candidateCount()` 两个字面量混用（等待用标记数）⇒ 等待**空转**、`rendered` 恒假（见
 *  `waitForCandidates()` 的注释）。
 */
async function candidateCount(page: Page): Promise<number> {
  return page
    .frameLocator(ISLAND_IFRAME)
    .locator(CONTROL_SCOPE)
    .evaluate(
      (root, selector) => root.querySelectorAll(selector as string).length,
      CONTROL_CANDIDATE_SELECTOR,
    )
}

/**
 * 这一页**已打审计标记**的可驱动控件条数——与 `snapshot.drivable.length` **同一口径**
 * （都按 `CONTROL_DRIVE_SELECTOR` 在同一个根上数）。用来判「DOM 有没有被重渲染」（条数变了 ⇒
 * 标记已随重渲染作废，必须重新枚举）。
 *
 * ⛔ **只能数带页面前缀的标记**（T1 扩张）：层内控件也会被打上标记（前缀不同），
 * 而层**不在 `#app` 里** ⇒ 本函数天然只看到页内的；但为了让"与 `snapshot.drivable.length` 同口径"
 * 这件事**写在代码里**，还是按前缀过滤一次（否则哪天帝国的层被搬进 `#app`，这条判据会静默错位）。
 */
async function markedCount(page: Page): Promise<number> {
  return page
    .frameLocator(ISLAND_IFRAME)
    .locator(CONTROL_SCOPE)
    .evaluate(
      (root, args) => {
        const [selector, prefix] = args as [string, string]
        return [...root.querySelectorAll(selector)].filter((el) =>
          (el.getAttribute('data-tt-audit-drive') ?? '').startsWith(prefix),
        ).length
      },
      [CONTROL_DRIVE_SELECTOR, CONTROL_DRIVE_TAG_PREFIX_PAGE] as [string, string],
    )
}

/**
 * 等「这次点击开出来的层」出现（最多 `LAYER_OPEN_WAIT_MS`，**一见到就收工**）。
 *
 * ⛔ 为什么不是"无条件睡满 `LAYER_OPEN_WAIT_MS`"（T1 实测的耗时账）：本网的页内控件**绝大多数
 * 不开层**（实测 cs 的 26 条页内控件里只有 2 次开出层），无条件睡满会把每一条白等 350ms
 * （一页 26 条 ≈ 9 秒）。`layer.open` 是**同步插 DOM** 的（读源码：`x.open` 里 `i()` /
 * `vessel()` 都直接 `append`），所以"层有没有出现"在点击返回后通常一个微任务内就定了
 * ⇒ 有界轮询 + 一见即收，快得多，而且**判据没变**（仍然是"此刻有没有可见的新层"）。
 *
 * @returns 本次新开出来的层（按 `times` 升序；没有就是空数组）。
 */
async function waitForFreshLayers(page: Page, beforeTimes: number): Promise<VisibleLayer[]> {
  const deadline = Date.now() + LAYER_OPEN_WAIT_MS
  for (;;) {
    const fresh = (await visibleLayers(page)).filter((l) => l.times > beforeTimes)
    if (fresh.length > 0) return fresh
    if (Date.now() >= deadline) return []
    await page.waitForTimeout(LAYER_OPEN_POLL_MS)
  }
}

/**
 * 此刻**已存在的最大层序号**（`times` 的最大值；一个层都没有时是 0）。
 *
 * ⛔ 用途只有一个：层内阶段用它判「哪一层是**这次点击新开出来的**」（`times > 点击前的最大值`）。
 * 为什么不能用「点击前后对比标题集合」，见 `VisibleLayer.times` 的注释（同名层与空标题层会漏判）。
 *
 * ⚠️ **不抛**：文档正在被换掉（点击触发跳页）时读不到层——那时按 0 处理，下一次枚举照样是
 * 干净的一次加载，不会把旧层的序号误判成新层。
 */
async function maxLayerTimes(page: Page): Promise<number> {
  try {
    return await page
      .frameLocator(ISLAND_IFRAME)
      .locator('body')
      .evaluate((body, rootSel) => {
        const all = [...body.querySelectorAll(rootSel)].map((el) => Number(el.getAttribute('times') ?? 0))
        return all.length ? Math.max(...all) : 0
      }, LAYER_ROOT)
  } catch {
    return 0
  }
}

/**
 * 一条**可见的 layui 层**（`visibleLayers()` 的读数）。
 */
type VisibleLayer = {
  /**
   * layui 给这一层的**自增序号**（根节点的 `times` 属性）。
   *
   * ⛔ 它是本线认「哪一层是**这次点击新开出来的**」的唯一稳定依据（T1 实测订正）：
   * 一开始想用「枚举前记下**当前层标题的集合**、点击后找不在集合里的标题」来判新层——
   * 但那有两个漏洞：① 同一页面会有**同名层**（`/orders` 上第二次点「指派打手」又开一个
   * 「将订单指派给接手人/代练员」）⇒ 标题相同就判不出新层；② 空标题的层全都叫 `''`。
   * `times` 是 layui 每次 `layer.open` 递增的序号（读源码：`x.index` 自增后写进根节点的
   * `times="<n>"`），**单调递增且逐层唯一** ⇒「`times > 点击前的最大值`」就是"新开的层"。
   * 实测佐证（探针）：`/orders` 的 12 次开层的 `times` 恰好是 1,2,3,…,12。
   */
  times: number
  /** `surface` 归属键（`layer:<层标题>` / `layer:untitled`）。 */
  surface: string
  /** 层标题原文（`LAYER_TITLE` 的文本；取不到是空串）。 */
  title: string
  /** 该层是不是 `type:2` iframe 层（判据见 `LAYER_IFRAME_MARK` 的注释）。 */
  iframe: boolean
  /**
   * 本线给这一层打的**审计标记值**（根节点的 `LAYER_AUDIT_ATTR`）——**定位**用这个，⛔ 不用 `times`。
   *
   * 同一个 `times` 可能在文档里出现两次，按它定位会 strict mode violation，看起来像层内控件点不动。
   * 定位用每次枚举现打的标记。layui 关普通层是删节点。
   */
  auditTag: string
}

/**
 * 读**此刻可见的 layui 层**（按 `times` 升序 = DOM 顺序 = 叠加顺序，最后一个是**最上层**）。
 *
 * ## 「可见」的判据
 * `getClientRects().length > 0`（有布局盒）。⛔ 不用 `offsetParent !== null`：layui 的层是
 * `position: fixed`，而 `offsetParent` 对 fixed 定位元素**恒为 `null`** ⇒ 那个判据会把**所有**层
 * 判成不可见（实测踩过：探针第一版用 `offsetParent`，一个层都没扫到）。
 *
 * ## 「算不算打开过」的判据
 * 只有**带关闭按钮**（`LAYER_CLOSE`）的层才算「会留在屏幕上的层」——理由与实测数字见
 * `visibleLayers()` 上方那段注释（判据落在函数里：要读 DOM 属性，⛔ 不是一个 `querySelector` 常量）。
 *
 * ⛔ **为什么必须在 island iframe 文档里读**（本仓 `E-0039` 的同一族）：
 * layui 的 `layer` 模块把层插进**它自己所在文档**的 `document.body`。本仓的 island 页跑在
 * `iframe[data-testid="layui-island"]` 里，所以「页面上点开一个层」= **那个 iframe 文档里**
 * 多一个 `.layui-layer`，**顶层文档里没有**。⇒ 本函数（以及后面所有层相关操作）一律走
 * `page.frameLocator(ISLAND_IFRAME)`。**这一段是前提，不是实现细节**：`layer.closeAll()` 在
 * *当前这个 iframe 文档* 里是合法的（`E-0039` 说的是**跨文档**调用会退化成合法 no-op），
 * 我们从头到尾都在这一个文档里操作，所以它有效；一旦将来把驱动改成"跨文档点击"，这个前提就破了。
 */
async function visibleLayers(page: Page): Promise<VisibleLayer[]> {
  return page
    .frameLocator(ISLAND_IFRAME)
    .locator('body')
    .evaluate(
      (body, args) => {
        const [rootSel, titleSel, closeSel, iframeMark, contentIframe, prefix, untitled, auditAttr] =
          args as [string, string, string, string, string, string, string, string]
        let seq = 0
        return [...body.querySelectorAll(rootSel)]
          .filter((el) => (el as HTMLElement).getClientRects().length > 0)
          .filter((el) => !!el.querySelector(closeSel))
          .map((el) => {
            const title = el.querySelector(titleSel)?.textContent?.trim() ?? ''
            // ⛔ 每次枚举**重打**标记（旧标记随上一轮的层一起作废），见 `VisibleLayer.auditTag`。
            const auditTag = String(seq++)
            el.setAttribute(auditAttr, auditTag)
            return {
              times: Number(el.getAttribute('times') ?? 0),
              surface: title === '' ? untitled : `${prefix}${title}`,
              title,
              iframe: el.classList.contains(iframeMark.replace('.', '')) || !!el.querySelector(contentIframe),
              auditTag,
            }
          })
      },
      [
        LAYER_ROOT,
        LAYER_TITLE,
        LAYER_CLOSE,
        LAYER_IFRAME_MARK,
        LAYER_CONTENT_IFRAME,
        LAYER_SURFACE_PREFIX,
        LAYER_SURFACE_UNTITLED,
        LAYER_AUDIT_ATTR,
      ] as [string, string, string, string, string, string, string, string],
    )
}

/**
 * 关掉**指定标记**的那一层：优先点该层的 `.layui-layer-close`，退 `Escape`，再退 `layer.close(times)`。
 *
 * ## 三条纪律（每条都有实测/读码出处）
 *  1. **点的是「那一层自己的」关闭按钮**（按 `LAYER_AUDIT_ATTR` 定位，⛔ 不是「页面上第一个
 *     `.layui-layer-close`」、⛔ **也不是** `[times="N"]`）：层可以叠（实测 `/orders` 一次点击能开出 2 个），
 *     而 `times` 在旧层与新层之间**可以重复**（实测的 strict mode violation，
 *     见 `LAYER_AUDIT_ATTR` 的注释；⚠️ 那里原来的"关层不删 DOM"根因已按 layui 源码订正）⇒ 按 `times`
 *     定位会命中两个元素。
 *  2. **`Escape` 的边界**（spec §3 第 4 条点名的退路）：layui 的 `Escape` 是 `closeLast()`，
 *     只关**最上层**那一层。所以只有「要关的正好是最上层」时它才有效；否则它**关错层**
 *     （把别的层关掉、目标层还在）——这正是本函数必须先确认「目标层没了没有」而不是
 *     「按过 Escape 就算关掉了」的原因。按完之后照样由 `waitForLayerGone()` 判真伪。
 *  3. **`layer.close(times)` 作为最后一道**：它按 layui 的内部序号关**指定的那一层**。
 *     与 `closeAll()` 不同，它不会殃及其它层。⚠️ `closeAll()` 在本网里只用于「点下一条
 *     页内控件前的清场」（`closeLayers()`），⛔ 不用在这里——见 `clickOneControl()` 的注释。
 *
 * @returns 该层是否**真的**关掉了（`waitForLayerGone()` 的有界确认）。
 */
async function closeOneLayer(page: Page, layer: VisibleLayer): Promise<boolean> {
  const frame = page.frameLocator(ISLAND_IFRAME)
  const selector = `${LAYER_ROOT}[${LAYER_AUDIT_ATTR}="${layer.auditTag}"]`
  const closeBtn = frame.locator(`${selector} ${LAYER_CLOSE}`)
  let used = 'none'
  try {
    await closeBtn.first().click({ timeout: CLICK_TIMEOUT_MS })
    used = 'close-btn'
  } catch {
    used = 'close-btn-failed'
  }
  if (used === 'close-btn' && (await waitForLayerGone(page, selector, LAYER_CLOSE_WAIT_MS))) return true
  // 退路 ①：Escape（⚠️ 只对最上层有效，见上）。
  try {
    await frame.locator('body').press('Escape', { timeout: CLICK_TIMEOUT_MS })
  } catch {
    /* 文档换掉了 / 键盘事件发不出去：下面照样按"层还在不在"下结论 */
  }
  if (await waitForLayerGone(page, selector, LAYER_CLOSE_WAIT_MS)) return true
  // 退路 ②：`layer.close(times)`（**指定层**，不是 `closeAll()`）。
  // ⛔ 前提与 `closeLayers()` 相同：我们在**同一个 island iframe 文档**里操作（见 `visibleLayers()`）。
  try {
    await frame.locator('body').evaluate((_body, n) => {
      const w = window as unknown as { layui?: { layer?: { close?: (i: number) => void } } }
      w.layui?.layer?.close?.(n as number)
    }, layer.times)
  } catch {
    /* 文档换掉了 */
  }
  const gone = await waitForLayerGone(page, selector, LAYER_CLOSE_WAIT_MS)
  if (!gone) {
    // 留痕（运行期读数，⛔ 与判据无关）：关不掉时把"这一层当时长什么样"打出来。
    // 用途是回答"为什么关不掉"（关闭按钮在不在 / 它是不是已经被关了 / 文档里还剩几个层 / 还剩几个标记）。
    // ⚠️ 实测踩到的那个反例正是靠它定位的：`/orders` 的"关层失败"其实**不是**目标层关不掉，
    //    而是它随后又冒出来一个提示层——那时 `mine` 会是「已关」态、`hasCloseBtn=false`。
    try {
      const diag = await frame.locator('body').evaluate(
        (body, args) => {
          const [sel, attr, rootSel, closeSel] = args as [string, string, string, string]
          const me = body.querySelector(sel)
          return {
            mine: me ? (me.getAttribute('class') ?? '').slice(0, 80) : '（已被移出 DOM）',
            visible: me ? (me as HTMLElement).getClientRects().length > 0 : false,
            hasCloseBtn: !!me?.querySelector(closeSel),
            allRoots: body.querySelectorAll(rootSel).length,
            allMarks: body.querySelectorAll(`[${attr}]`).length,
          }
        },
        [selector, LAYER_AUDIT_ATTR, LAYER_ROOT, LAYER_CLOSE] as [string, string, string, string],
      )
      process.stderr.write(
        `[T1-CLOSE-FAIL] ${layer.surface} times=${layer.times} used=${used} ${JSON.stringify(diag)}\n`,
      )
    } catch {
      /* 文档换掉了 */
    }
  }
  return gone
}

/**
 * **有界**等某一层（按它的**审计标记**定位）消失（`LAYER_CLOSE_WAIT_MS` 内轮询）。
 *
 * 关层有动画，量一次会把正在消失判成没关掉，然后白走一次重新导航。
 * 判据用 `count()`：普通层关闭是删节点。不要把「已关的层还留在 DOM 里」当成根因。
 */
async function waitForLayerGone(page: Page, selector: string, budgetMs: number): Promise<boolean> {
  const deadline = Date.now() + budgetMs
  for (;;) {
    let count = 0
    try {
      count = await page.frameLocator(ISLAND_IFRAME).locator(selector).count()
    } catch {
      // 文档被换掉了 ⇒ 层当然也不在了（那是"关掉了"的一种）。
      return true
    }
    if (count === 0) return true
    if (Date.now() >= deadline) return false
    await page.waitForTimeout(LAYER_CLOSE_POLL_MS)
  }
}

/**
 * **层内阶段**（spec §3 第 1–4 条）：刚刚那一次页内点击，如果开出了层，就在层内再驱动一轮。
 *
 * ## 时机（spec §3 第 1 条）
 * 由调用方在**每点完一条页内控件之后**调用（不是"每页一次"）：`beforeTimes` 是点击**之前**
 * 那一刻的最大层序号，所以本函数只看**这次点击新开出来的**层。
 *
 * ## 边界（spec §3 第 5、6 条，⛔ 两条都不许静默放过）
 *  - **只一层**：新层里若有 ≥2 个，其余全部计入 `coOpenedSkipped`（**同一次点击开出来的兄弟层**
 *    不驱动；D 订正：它⛔ **不是**"层里再开的层"——**真嵌套**既不计入本键、
 *    也不被驱动，`driveControlsInLayer()` 不递归，见 spec §6 / §9）；
 *  - **`type:2` iframe 层**：内容在**另一个文档**里 ⇒ 枚举看不到 ⇒ 计入 `skippedIframe`
 *    并**跳过**（⛔ 不是当成"层内没有控件"；被跳过的层身份另记 `skippedIframeLayers`，修复波 G）。
 *
 * ## 关层（spec §3 第 4 条）——这一步是**硬约束**，不是收尾礼节
 * 母规范实测过：带着残留层去点下一条页内控件会**旧层与新层撞车**（`element detached`）。
 * 所以本函数**必须**在返回前把层关干净：目标层关一次；关不掉就**重新导航该页**，再核一次
 * "此刻还剩几个层"——还剩就**响亮地**记进 `closeFailures`（由调用方写进报告）。
 */
async function driveLayerStage(
  page: Page,
  path: string,
  beforeTimes: number,
): Promise<LayerStageResult> {
  const out: LayerStageResult = {
    layers: [],
    opened: 0,
    drivableTotal: 0,
    clicked: 0,
    dedupedDuplicates: 0,
    dedupedUnhittable: 0,
    deduped: [],
    notDriven: [],
    clickFailures: [],
    findings: [],
    skippedIframe: 0,
    skippedIframeLayers: [],
    coOpenedSkipped: 0,
    closeFailures: 0,
    renavigated: false,
  }
  let fresh: VisibleLayer[]
  try {
    fresh = await waitForFreshLayers(page, beforeTimes)
  } catch {
    // 文档在换（点击触发了跳页）⇒ 这一轮没有层可扫；页面层会在下一条控件前重新导航。
    return out
  }
  if (fresh.length === 0) return out
  // ⛔ **显式按 `times` 升序排**（L）：下面拿 `fresh[0]` 当「最下面那层」，
  //    而 `visibleLayers()` 返回的是 **DOM 顺序**——"DOM 顺序恰好等于 `times` 升序"只是原来
  //    **没写下来的假设**。排一次把它变成代码里的判据（同 `times` 时保持相对顺序，排序稳定）。
  fresh = [...fresh].sort((a, b) => a.times - b.times)
  out.opened = fresh.length
  // ⛔ **只驱动一层**（spec §3 第 5 条）：取 `times` 最小者（= 最下面那层），其余层全部计入
  //    `coOpenedSkipped`（D：原名 `nestedSkipped`，它数的其实是**同一次点击
  //    开出来的兄弟层**，⛔ 不是"层里再开的层"）。为什么取**最下面**那层而不是最上面：实测
  //    `/orders` 点一次「指派打手」会先开指派弹层、随后再叠一个 `layer.msg`（提示层）
  //    ——**有控件的那个层在下面**。
  const target = fresh[0]
  for (const l of fresh.slice(1)) {
    if (l.auditTag !== target.auditTag) out.coOpenedSkipped += 1
  }
  if (target.iframe) {
    // `type:2`：内容在另一个文档里 ⇒ 枚举看不到 ⇒ 显式跳过并计数（spec §3 第 6 条）。
    // ⚠️ 同时记下**被跳过的是哪一层**（G）：本判据是「类名命中 **或** 内容里有
    //    `<iframe>`」的并集，一个同文档 `type:1` 层里嵌了 iframe 也会被整层跳过 ⇒ 只记计数
    //    就没人能发现误杀。
    out.skippedIframe += 1
    out.skippedIframeLayers.push(target.surface)
  } else {
    await driveControlsInLayer(page, path, target, out)
  }
  // —— 关层（spec §3 第 4 条）——
  // ⛔ **纪律**：返回前这一页**不许有残留层**（母规范实测过旧层与新层撞车 `element detached`）。
  // ## 两条不是礼节、都是实测换来的
  //  ① **先按"目标层"严格关**（点 X → Escape → `layer.close(times)`），关掉了就收工——
  //     实测绝大多数层一次点 X 就没了，这条路最省。
  //  ② **关不掉才上重手段**：`closeAll()` 兜底再来一次；**仍关不掉**才 `page.goto` 重新导航。
  //     ⛔ T1 实测踩到过一个反例：`/orders` 点「指派打手」之后，**目标层（指派）早就关掉了**，
  //     而随后自己冒出来的 `layer:信息`（提示层，`time:1500`）被收尾复核当成了"关层失败"⇒
  //     每次都白走一次 `page.goto`（一页 2 次、每次约 1.6 秒，`/orders` 因此从 20.5s 涨到 37.7s）。
  //     ⇒ 顺序很重要：**先尽力关、再复核，最后才付重新导航的钱**。
  const closed = await closeOneLayer(page, target)
  if (!closed) out.closeFailures += 1
  for (const l of fresh.slice(1)) {
    // 本次没选中的叠层（多半是提示层）：尽力关，⛔ 不为它们单独重新导航。
    if (!(await closeTransientLayer(page, l))) out.closeFailures += 1
  }
  // 收尾复核：此刻这一页**还有没有残留层**（`closeOneLayer` 说"没了"只保证目标那一层）。
  try {
    let left = (await visibleLayers(page)).filter((l) => l.times > beforeTimes)
    if (left.length > 0) {
      // 第 ② 档：`closeAll()` 兜底（在这里**是**合法且安全的——见 `visibleLayers()` 的前提说明）。
      await closeLayers(page)
      await page.waitForTimeout(LAYER_CLOSE_WAIT_MS)
      left = (await visibleLayers(page)).filter((l) => l.times > beforeTimes)
    }
    if (left.length > 0) {
      // 第 ③ 档：**重新导航该页**（这是最贵的一档，所以放在最后）。
      out.closeFailures += left.length
      // 留痕（与 `[T7-PAGE]` 同一类运行期读数）：**关不掉的是哪一层**必须看得见——
      // 「关层失败率」是本线最贵的一项代价（每次都要重新导航一整页），它是排障入口。
      process.stderr.write(
        `[T1-LEFTOVER] ${path} beforeTimes=${beforeTimes} 关不掉=${left
          .map((l) => `${l.surface}@t${l.times}`)
          .join(' , ')} ⇒ 重新导航 ${path}\n`,
      )
      await page.goto(`${FRONTEND_URL}${path}`)
      await page.waitForTimeout(CANDIDATE_MIN_WAIT_MS)
      out.renavigated = true
    }
  } catch {
    /* 文档换掉了（那也是一种"没有残留层"） */
  }
  return out
}

/**
 * **尽力**关掉一个**提示层**（没有可驱动控件的层）：点 X → Escape → `layer.close(times)`，
 * 但 **⛔ 不为它重新导航**，也不把"它还在"当成关层失败去重导航。
 *
 * ## 为什么可以这样（T1 实测的 37.7s → 结论）
 * 实测 `/orders` 上的"关层失败=2"**全部**是 `layer:信息`（layui 的 `layer.msg`，`time:1500`，
 * 到点自己消失，层里 0 条控件）——原先对它们走的是「严格关 + 关不掉就重新导航该页」那一档，
 * 每次白花约 1.6 秒（`page.goto` + 稳定窗），一页 2 次。
 * ⛔ **纪律并没有放宽**：spec §3 第 4 条要的是「⛔ 不许带着残留层去点下一条页内控件」，
 * 而这件事由 `clickOneControl()` 第 ① 步的 `closeLayers()`（**每一轮页内控件前都跑**）保证；
 * 本函数只是不再为一条注定自己消失的提示层付"重新导航一整页"的钱。
 */
async function closeTransientLayer(page: Page, layer: VisibleLayer): Promise<boolean> {
  const frame = page.frameLocator(ISLAND_IFRAME)
  const selector = `${LAYER_ROOT}[${LAYER_AUDIT_ATTR}="${layer.auditTag}"]`
  try {
    await frame
      .locator(`${selector} ${LAYER_CLOSE}`)
      .first()
      .click({ timeout: CLICK_TIMEOUT_MS })
  } catch {
    /* 提示层可能没有关闭按钮 / 已经自己消失：下面按"还在不在"下结论 */
  }
  if (await waitForLayerGone(page, selector, LAYER_CLOSE_WAIT_MS)) return true
  try {
    await frame.locator('body').evaluate((_body, n) => {
      const w = window as unknown as { layui?: { layer?: { close?: (i: number) => void } } }
      w.layui?.layer?.close?.(n as number)
    }, layer.times)
  } catch {
    /* 文档换掉了 */
  }
  return waitForLayerGone(page, selector, LAYER_CLOSE_WAIT_MS)
}

/**
 * 清掉 island 文档里**层内那套**审计标记（`CONTROL_DRIVE_ATTR_LAYER`，⛔ **不是**页内那套）。
 *
 * 层内阶段在同一个文档里反复枚举，上一轮的 `L0` 会和本轮撞车，strict mode violation
 * 看起来像控件点不到。layui 关普通层是删节点；更可能是 `type:1` 把页内节点搬进层再放回。
 * 只清层内那套标记。页内靠 `page.goto` 换文档，不用这步。
 */
async function clearAuditMarks(page: Page): Promise<void> {
  await page
    .frameLocator(ISLAND_IFRAME)
    .locator('body')
    .evaluate((body, attr) => {
      for (const el of body.querySelectorAll(`[${attr}]`)) el.removeAttribute(attr)
    }, CONTROL_DRIVE_ATTR_LAYER)
}

/**
 * 在**指定的那一层内**枚举并逐条驱动控件（spec §3 第 2 条）。
 *
 * 记账口径与页内**完全一致**（同一份 `enumerateCandidateControls()`、同一份去重与挑选逻辑），
 * 区别只有三条：
 *  - 枚举的**根**是那一层（`LAYER_ROOT` 作用域）；
 *  - 标记值带**层前缀**（`CONTROL_DRIVE_TAG_PREFIX_LAYER`，⛔ 见那个常量的注释：不带的会撞车）；
 *  - 驱动时**不清场**（`closeResidualLayers: false`，⛔ 见 `clickOneControl()` 的注释：
 *    `closeAll()` 会把要驱动的这一层也关掉）。
 *
 * ⚠️ **层内控件不产生"重新导航"那套自愈**：一次点击若跳了页，`islandPresent()` 会判假，
 * 本函数就**停下**（把剩余控件记进 `clickFailures`）——比页内那层弱，因为"层内导航"意味着
 * 我们驱动的那个层已经不在了（页面都换了），重新导航回来也找不回它。这是**有意的**取舍，
 * 报告里表现为 `layerClickFailures`（不判红，是探针限制，spec §3.3 同款）。
 */
async function driveControlsInLayer(
  page: Page,
  path: string,
  layer: VisibleLayer,
  out: LayerStageResult,
): Promise<void> {
  const frame = page.frameLocator(ISLAND_IFRAME)
  const layerRoot = frame.locator(`${LAYER_ROOT}[${LAYER_AUDIT_ATTR}="${layer.auditTag}"]`)
  let snapshot: ControlSnapshot
  try {
    // ⛔ **先清全部审计标记再枚举**（理由见 `clearAuditMarks()` 的注释：同一文档里反复枚举，
    //    上一轮的层内标记必须清掉，否则同值标记会撞车）。
    await clearAuditMarks(page)
    snapshot = await enumerateCandidateControls(page, FRONTEND_URL, {
      root: layerRoot,
      scope: LAYER_ROOT,
      tagPrefix: CONTROL_DRIVE_TAG_PREFIX_LAYER,
      driveAttr: CONTROL_DRIVE_ATTR_LAYER,
      // ⛔ **层内也要逐条记账**（A）：层里"命中候选选择器但不带 `layui-btn`"的
      //    控件必须进 `notDriven`——原先这里传 `false` 且**没人读** `snapshot.skipped`，
      //    等于层内静默少点（母规范 §3.2 明令禁止）。消费点见下面的 `notDrivenOf(snapshot)`。
      recordNonBtnSkipped: true,
    })
  } catch (err) {
    out.clickFailures.push({
      surface: layer.surface,
      control: '(整个层)',
      reason: `层内枚举失败（层可能已被关掉 / 文档换掉）：${String(err).split('\n')[0].slice(0, 200)}`,
    })
    return
  }
  // 层内「候选但没驱动」的条目：与页内**同一份** `notDrivenOf()`（口径一致），带 `surface` 归属。
  const notDriven = notDrivenOf(snapshot)
  out.drivableTotal += snapshot.drivable.length
  out.notDriven.push(...notDriven.map((it) => ({ surface: layer.surface, ...it })))
  out.layers.push({
    page: path,
    surface: layer.surface,
    candidateTotal: snapshot.candidateTotal,
    drivable: snapshot.drivable.length,
    notDriven: notDriven.length,
    clicked: 0,
  })
  const record = out.layers[out.layers.length - 1]
  // 按身份串分组 + 去重：与页内**同一套**取舍（`DEDUPE_N`），细节见 `driveControlsOnPage()`。
  const used = new Set<string>()
  const groups = new Map<string, ControlEntry[]>()
  for (const entry of snapshot.drivable) {
    const list = groups.get(entry.identity)
    if (list) list.push(entry)
    else groups.set(entry.identity, [entry])
  }
  const recordDedup = (entry: ControlEntry, reason: string, unhittable: boolean): void => {
    used.add(entry.tag)
    if (unhittable) out.dedupedUnhittable += 1
    else out.dedupedDuplicates += 1
    out.deduped.push({ identity: entry.identity, reason })
  }
  for (const [identity, group] of groups) {
    let drivenHere = 0
    while (drivenHere < DEDUPE_N) {
      // ⛔ **「没有剩余条目 ⇒ break」必须排在存活检查之前**（C）。
      //    原来的顺序（先查层还在不在、后查还有没有剩余）会给**只有 1 条控件的层**记一条**幻影失败**：
      //    第 1 次点完（层随之关闭）后 `drivenHere=1 < DEDUPE_N` ⇒ 第 2 圈先命中"层没了" ⇒ 记一条
      //    `layerClickFailure`，而**根本没有"剩余控件"要驱动**。真实形态：`/game-config/projects`
      //    的「保存」（点了层自己关掉）。⚠️ 这一条同时**推翻了 spec §8.3 的根因叙述**
      //    （"⇒ 下一条找不到这一层"）：没有"下一条"，是**同一身份串的第 2 个去重名额**走到的这里。
      const remaining = snapshot.drivable.filter((it) => it.identity === identity && !used.has(it.tag))
      if (remaining.length === 0) break
      // 层没了 / 页面换了 ⇒ 停下（见函数头注释：层内不做"重新导航"那套自愈）。此时
      // `remaining` 非空 ⇒ 这是**真**的"驱动到一半层没了"，记一条失败是对的。
      const stillThere = await layerRoot.count()
      if (stillThere === 0 || !(await islandPresent(page))) {
        out.clickFailures.push({
          surface: layer.surface,
          control: identity,
          reason: '驱动过程中这一层消失 / island 换掉了（层内不做重新导航自愈，剩余控件不再驱动）',
        })
        break
      }
      const choice = await chooseTarget(page, remaining)
      for (const skipped of choice.skipped) recordDedup(skipped, UNHITTABLE_COPY_REASON, true)
      used.add(choice.target.tag)
      drivenHere += 1
      out.clicked += 1
      record.clicked += 1
      try {
        const ruled = await test.step(`层内控件 ${path} ${layer.surface} ${identity}`, () =>
          clickOneControl(page, choice.target, { closeResidualLayers: false }),
        )
        // T2：写模式的诊断（非 GET 403 / ≥500）带归属入账——⛔ 不判红，只记录。
        for (const issue of ruled.issues) {
          writeModeIssues.push({ page: path, surface: layer.surface, control: identity, issue })
        }
        recordLayerFinding(out, path, layer.surface, identity, ruled)
      } catch (err) {
        out.clickFailures.push({
          surface: layer.surface,
          control: identity,
          reason:
            (choice.diagnosis ? `${choice.diagnosis}；` : '') +
            String(err).split('\n')[0].slice(0, 200),
        })
      }
    }
    for (const entry of group) {
      if (used.has(entry.tag)) continue
      if (!entry.hittable && !entry.inViewportX) recordDedup(entry, UNHITTABLE_COPY_REASON, true)
      else recordDedup(entry, DEDUPE_REASON, false)
    }
  }
}

/**
 * 把一次层内点击的 403 记进 `out.findings`（**按 `page` + `surface` + 身份串**归并，与页内同一口径）。
 *
 * ⛔ 归并的键**必须带 `surface`**（T1 新增）：页内那条「指派打手」与层内同名控件是**两件事**，
 * 只按 `control` 归并会把两个不同层面的发现揉成一条（读报告的人再也分不清是哪一层打出来的）。
 * ⚠️ J：键**也带 `page`**——同名层（`layer:信息` / `layer:untitled`）在不同页上
 * 是两件事；本函数内 `page` 恒定，带上它是让键与跨阶段归并（调用点）**同一把尺**（`E-0083`）。
 */
function recordLayerFinding(
  out: LayerStageResult,
  path: string,
  surface: string,
  identity: string,
  ruled: { forbidden: string[]; writes: string[] },
): void {
  const fresh403 = ruled.forbidden.filter((line) => line.startsWith('403 '))
  if (fresh403.length === 0) return
  const existing = out.findings.find(
    (f) => f.page === path && f.surface === surface && f.control === identity,
  )
  if (existing) {
    existing.occurrences += 1
    existing.urls = [...new Set([...existing.urls, ...fresh403])]
    existing.abortedWrites = [...new Set([...existing.abortedWrites, ...ruled.writes])]
    return
  }
  out.findings.push({
    page: path,
    surface,
    control: identity,
    urls: fresh403,
    abortedWrites: ruled.writes,
    occurrences: 1,
  })
}

/**
 * 对**一条页面**驱动它的全部候选控件。返回这一页的读数。
 *
 * 三条与任务书一一对应的纪律：
 *  1. **每条控件单独算账**：`clickOneControl()` 在点击前切一刀、只收这一段里新出现的 403
 *     ⇒ 导航期页面自己打的请求、以及**上一条控件**打出来的 403 都不会记到这一条头上。
 *  2. **每条控件前确认 iframe / `#app` 还在**；不在就**重新导航到该页**再继续
 *     （换页会重建 iframe，旧快照的**审计标记**随作废 —— 重新导航后按**身份串**在新快照里找回这一条）。
 *  3. **点不动不判红**：记进 `clickFailures`，继续下一条。
 */
async function driveControlsOnPage(page: Page, path: string): Promise<PageDriveResult> {
  const result: PageDriveResult = {
    visited: false,
    settledPathAtEntry: '',
    rendered: false,
    candidateTotal: 0,
    drivable: 0,
    drivableTotal: 0,
    dedupedDuplicates: 0,
    dedupedUnhittable: 0,
    deduped: [],
    skipped: [],
    clickedIdentity: [],
    findings: [],
    clickFailures: [],
    routeLinks: [],
    enumerationFailed: '',
    layerControlsClicked: 0,
    layerDrivableTotal: 0,
    layerDedupedDuplicates: 0,
    layerDedupedUnhittable: 0,
    layers: [],
    layerClickFailures: [],
    layersSkippedIframe: 0,
    skippedIframeLayers: [],
    layersCoOpenedSkipped: 0,
    layerCloseFailures: 0,
  }
  // ⛔ 每次驱动前**重新导航**（不是复用上一次的末尾状态）：上一页的点击可能留下了弹层 /
  //    换了 iframe / 改了表格状态，从干净的一次加载开始，这一页的读数才可比。
  await page.goto(`${FRONTEND_URL}${path}`)
  // ⭐ 落定后的终态路径：被路由守卫弹回时**不是** `path`。`instancePageUnreachable` 那一档
  //    只认这个事实（⛔ 不认 `visited`，见 `PageDriveResult.settledPathAtEntry` 的注释）。
  result.settledPathAtEntry = await settledPath(page)
  if (result.settledPathAtEntry !== path) {
    result.enumerationFailed = `导航后被路由守卫弹回（终态不是 ${path}），这一页不再驱动`
    return result
  }
  if (!(await islandPresent(page))) {
    result.visited = true
    result.enumerationFailed = 'island iframe / #app 没挂载出来（无候选控件）'
    return result
  }
  result.visited = true
  // ⛔ 等候选控件渲染出来再枚举（见 `waitForCandidates()` 的注释：不等就会把"异步还没渲染"
  //    误判成"这页没有控件"，而这正是 D-0145 那条 403 被漏掉的原因）。
  const tPage = Date.now()
  result.rendered = await waitForCandidates(page, CANDIDATE_WAIT_MS)
  // 枚举（**唯一出处**在夹具层；每次调用都会与内联实现逐元素对拍）。
  // ⚠️ `let`：重新枚举（页面重渲染后）必须**换掉**这个快照——否则后续控件还在用旧标记，
  //    而旧标记已经随着 DOM 重建消失 ⇒ `locator([data-tt-audit-drive="旧值"])` 匹配不到，
  //    每条都白等到 10s 超时（一个"点不动"的假象，真因是快照没换）。
  let snapshot = await enumerateCandidateControls(page, FRONTEND_URL, {
    root: page.frameLocator(ISLAND_IFRAME).locator(CONTROL_SCOPE),
    scope: '',
    tagPrefix: CONTROL_DRIVE_TAG_PREFIX_PAGE,
    driveAttr: CONTROL_DRIVE_ATTR,
    recordNonBtnSkipped: true,
  })
  result.candidateTotal = snapshot.candidateTotal
  // ⛔ N3 的**分母**：驱动计划所依据的那次枚举里的可驱动条数（不是"实际点了几条"，
  //    否则判据恒真；见 `PageDriveResult.drivableTotal`）。
  result.drivableTotal = snapshot.drivable.length
  result.routeLinks = snapshot.routeLinks
  // ⛔ 页内与层内**共用**同一份「候选但没驱动」的记账（A；实现见 `notDrivenOf()`）。
  result.skipped = notDrivenOf(snapshot)
  // ⛔ 驱动循环：**按身份串分组**（T7 起）。分组是"去重 + 改点可命中的那一份"两件事的共同前提：
  //    同一身份串的那些元素要放在一起排序、一起记账，才谈得上"前 N 个"与"哪一份点得到"。
  //    每条控件仍然按它**页面上的审计标记**点击（⛔ 不按循环下标 / 选择器下标 —— 命中顺序在
  //    layui 固定列克隆体上是分叉的）。
  //
  // ⚠️ **记账口径（T7）**：初始枚举里的每一条 `drivable` 恰好归入下面三类之一（只有这三类）：
  //    ① 被驱动（`clickedIdentity`，含点不动的——那也算"真的发起过这次点击尝试"）；
  //    ② 因**同一身份串已驱动够 `DEDUPE_N` 个**而没驱动（`dedupedDuplicates`）；
  //    ③ 因**同身份串另有可命中的那一份**而没驱动（`dedupedUnhittable`，如固定列克隆体）。
  //    ⇒ `clicked + dedupedDuplicates + dedupedUnhittable >= drivableTotal`。
  //    这条恒等式就是"**不许静默少点**"的判据本体（spec §3.4 的 N3 收口处）。
  const used = new Set<string>()
  const initialDrivable = snapshot.drivable
  /** 按身份串分组（`Map` 保持首次出现顺序 = DOM 顺序）。 */
  const groups = new Map<string, ControlEntry[]>()
  for (const entry of initialDrivable) {
    const list = groups.get(entry.identity)
    if (list) list.push(entry)
    else groups.set(entry.identity, [entry])
  }
  /** 把一条"没驱动"记进去重账：两类各自计数 + **逐条**明细（⛔ 不许只给计数）。 */
  const recordDedup = (entry: ControlEntry, reason: string, unhittable: boolean): void => {
    used.add(entry.tag)
    if (unhittable) result.dedupedUnhittable += 1
    else result.dedupedDuplicates += 1
    result.deduped.push({ identity: entry.identity, reason })
  }
  for (const [identity, group] of groups) {
    let drivenHere = 0
    while (drivenHere < DEDUPE_N) {
      // ② 每次驱动前确认 iframe / `#app` 还在；不在（点击换了页）就**重新导航到该页**再继续。
      const stillThere = await islandPresent(page)
      if (!stillThere) {
        await page.goto(`${FRONTEND_URL}${path}`)
        if ((await settledPath(page)) !== path) {
          result.clickFailures.push({ page: path, control: identity, reason: `重新导航后没落回 ${path}` })
          break
        }
      }
      // ⚠️ **快照可能已经作废**：点击可能让页面重渲染（列表刷新 / 行数变化），那时**审计标记**
      //    就指向别的元素。用一次**廉价的条数比对**判它（条数变了 / iframe 换过 ⇒ 重新枚举 + 重打标记）。
      //    ⛔ 这里比的必须是 `markedCount()`（**标记数**），⛔ 不是 `candidateCount()`（候选数）——
      //    后者与 `snapshot.drivable.length` 不是同一个口径（T7 拆函数时实测踩到：候选数 235、
      //    可驱动 233，拿它比会把**每一页**都判成"重渲染了"、白白重新枚举）。
      if (!stillThere || (await markedCount(page)) !== snapshot.drivable.length) {
        snapshot = await enumerateCandidateControls(page, FRONTEND_URL, {
          root: page.frameLocator(ISLAND_IFRAME).locator(CONTROL_SCOPE),
          scope: '',
          tagPrefix: CONTROL_DRIVE_TAG_PREFIX_PAGE,
          driveAttr: CONTROL_DRIVE_ATTR,
          recordNonBtnSkipped: true,
        })
      }
      // 在当前快照里找**同身份串、且还没处理过**的那几条（标记在重渲染后已重打，必须用新快照）。
      const remaining = snapshot.drivable.filter((it) => it.identity === identity && !used.has(it.tag))
      if (remaining.length === 0) {
        // ⚠️ 两种"没有剩余"必须分开（T7 执行期实测踩到）：`drivenHere > 0` 时这是**正常**的
        //    ——同一身份串只有一条（比 `DEDUPE_N` 少），不是"消失"。原实现一律记
        //    「重新导航后不再出现」⇒ `/accounts/profile` 13 条控件报出 **21** 条点不动
        //    （每条都被冤枉记了一次）。只有**一条都没驱动过**却找不到它，才是真的"重渲染后没了"。
        if (drivenHere === 0) {
          result.clickFailures.push({
            page: path,
            control: identity,
            reason: '重新导航后这一条控件不再出现（页面重渲染后消失了）',
          })
        }
        break
      }
      // ③ **挑一份人真正点得到的目标**（T7 的核心修复；排序与退回规则见 `chooseTarget()`）。
      const choice = await chooseTarget(page, remaining)
      // 被判"人点不到"、因而改点了别的那一份：记进 `dedupedUnhittable`（⛔ 逐条留痕）。
      for (const skipped of choice.skipped) recordDedup(skipped, UNHITTABLE_COPY_REASON, true)
      used.add(choice.target.tag)
      // 「点过」的口径 = **真的发起过这次点击尝试**（成功 / 失败 / 被拦断都算）——见 N3 的注释。
      result.clickedIdentity.push(identity)
      drivenHere += 1
      // ⛔ **点之前**记下"此刻已存在的最大层序号"——层内阶段靠它认出"这次点击新开出来的层"
      //    （为什么不能用标题集合判新层，见 `VisibleLayer.times` 的注释）。
      const beforeLayerTimes = await maxLayerTimes(page)
      // ⚠️ **每条控件包一层 `test.step`**：它把「哪一条控件花了多久」打进运行输出，
      //    是本网**排障**的关键读数（T3 执行期实测：没有它时一条卡住的控件只会表现为
      //    「用例超时、报告里只点了 2 条」，完全看不出卡在哪一条）。step 只影响报告，不改行为。
      try {
        const ruled = await test.step(`控件 ${path} #${choice.target.tag} ${identity}`, () =>
          clickOneControl(page, choice.target, { closeResidualLayers: true }),
        )
        // T2：写模式的诊断（非 GET 403 / ≥500）带归属入账——⛔ 不判红，只记录。
        for (const issue of ruled.issues) {
          writeModeIssues.push({ page: path, surface: path, control: identity, issue })
        }
        const fresh403 = ruled.forbidden.filter((line) => line.startsWith('403 '))
        if (fresh403.length > 0) {
          // 同一页 + 同一身份串 ⇒ 归成一条（`occurrences` 累加；`urls` / 被拦断的写按观测合并去重）。
          // ⛔ 归并键**含 `surface`**（T1）：页内那条与层内同名控件是两件事，不能揉成一条。
          const existing = result.findings.find(
            (f) => f.control === identity && f.surface === path,
          )
          if (existing) {
            existing.occurrences += 1
            existing.urls = [...new Set([...existing.urls, ...fresh403])]
            existing.abortedWrites = [...new Set([...existing.abortedWrites, ...ruled.writes])]
          } else {
            result.findings.push({
              page: path,
              // 页内控件的 `surface` = **页面路径**（spec §2 的归属条）。
              surface: path,
              control: identity,
              urls: fresh403,
              abortedWrites: ruled.writes,
              occurrences: 1,
            })
          }
        }
      } catch (err) {
        // ⛔ 点不动 ≠ 权限缺陷（任务书 / spec §3.3）：记下来继续下一条。
        // 把 `chooseTarget()` 的命中诊断（最上层节点是谁）一并带上——那才是**真因**。
        result.clickFailures.push({
          page: path,
          control: identity,
          reason:
            (choice.diagnosis ? `${choice.diagnosis}；` : '') +
            String(err).split('\n')[0].slice(0, 200),
        })
        // ⛔ **这一条没点动 ⇒ 不走层内阶段**：`clickOneControl()` 只在**点击没发出去**时才抛
        //    （`isVisible()` 为假 / `locator.click()` 自己失败），Playwright 保证那种情况下
        //    这个控件**没有触发任何东西** ⇒ 此刻若扫到层，那层只能来自别处，记到这一条头上是
        //    **错误归因**。⚠️ 代价：若哪天出现"点击发出去了、层也开了，之后才抛"的路径，
        //    这一层会被漏扫——所以 `layerCloseFailures` / `layers*` 这几个计数键要一起读。
        //    （残留层由**下一条页内控件**前的 `closeLayers()` 清掉，见 `clickOneControl()` 第 ① 步。）
        // ⚠️ 用 `break`（**不是 `continue`**）：`continue` 会跳过驱动循环末尾那段「同一身份串
        //    里没进计划的元素全部记进去重账」——那正是"不许静默少点"的记账，跳过它会让本页的
        //    `clicked + deduped ≥ drivableTotal` 恒等式当场失衡。先 `break` 出去，末尾照常记账。
        break
      }
      // ⛔ **T1 层内阶段**（spec §3 第 1 条：**每点完一条页内控件之后**判一次）：这次点击有没有
      //    开出新层？有就枚举层内控件并逐条驱动，**并在返回前把层关干净**（spec §3 第 4 条：
      //    ⛔ 不许带着残留层去点下一条页内控件——母规范实测过旧层与新层撞车 `element detached`）。
      const stage = await driveLayerStage(page, path, beforeLayerTimes)
      result.layers.push(...stage.layers)
      result.layerDrivableTotal += stage.drivableTotal
      result.layerControlsClicked += stage.clicked
      result.layerDedupedDuplicates += stage.dedupedDuplicates
      result.layerDedupedUnhittable += stage.dedupedUnhittable
      for (const it of stage.deduped) {
        result.deduped.push({ identity: it.identity, reason: `（层内）${it.reason}` })
      }
      for (const it of stage.clickFailures) {
        result.layerClickFailures.push({ page: path, surface: it.surface, control: it.control, reason: it.reason })
        // ⚠️ 层内点不动**也**进 `clickFailures`（带 `surface` 归属）：那是"这一条控件我们试过了、
        //    但没点动"的**同一件事**。`layerClickFailures` 只是它的计数视图（spec §5），
        //    ⛔ 不是另一份数据——两边各自累加会让人以为有两批事件。
        result.clickFailures.push({ page: path, control: it.control, reason: `（层内 ${it.surface}）${it.reason}` })
      }
      for (const it of stage.notDriven) {
        // A：层内"候选但没驱动"的条目**并进页面的 `skipped`**（最终落进报告同一个
        // `notDriven` 键）。`reason` 前置 `（层内 <surface>）`——与下面 `clickFailures` 同一套归属写法。
        result.skipped.push({
          identity: it.identity,
          reason: `（层内 ${it.surface}）${it.reason}`,
        })
      }
      for (const f of stage.findings) {
        // 跨"这一页的多次层内阶段"再归并一次：键是 **(page, surface, control)**——比
        // `recordLayerFinding()` 同一把尺（函数内 page 恒定）。⛔ 不能只按 (surface, control)：
        // 同名层（`layer:信息` / `layer:untitled`）在不同页上是两件事（J）；
        // 也⛔ 不能只按 control：页内那条同名控件是另一件事。
        const existing = result.findings.find(
          (x) => x.page === f.page && x.surface === f.surface && x.control === f.control,
        )
        if (existing) {
          existing.occurrences += f.occurrences
          existing.urls = [...new Set([...existing.urls, ...f.urls])]
          existing.abortedWrites = [...new Set([...existing.abortedWrites, ...f.abortedWrites])]
        } else {
          result.findings.push(f)
        }
      }
      result.layersSkippedIframe += stage.skippedIframe
      for (const surface of stage.skippedIframeLayers) {
        // G：⛔ 不只记计数——「跳过的是哪一层」要能在报告里点出来（含页码归属）。
        result.skippedIframeLayers.push({ page: path, surface })
      }
      result.layersCoOpenedSkipped += stage.coOpenedSkipped
      result.layerCloseFailures += stage.closeFailures
      // ⛔ **只有这次点击真的开过层，才重新枚举页内快照**（K）。
      //    ## 为什么要重新枚举
      //    layui 开层时会把**页内那个节点搬进层里**（`layer.open({type:1, content:$(node)})`，
      //    `config-replica.ts` / `order-orderlist.ts` 都记了这条；读源码 `x.close` 的
      //    `conType==="object"` 那一支会 `unwrap()` 放回）⇒ 那个节点若在页内快照里，
      //    关层后它的位置/身份可能变了；重新枚举一次最省心。
      //    ## ⚠️ 订正（此前写的理由**不成立**，不许再抄）
      //    原注释说「`clearAuditMarks()` 把那个节点的**页内标记也一起清掉了**」——**错**：
      //    `clearAuditMarks()` 只清 `CONTROL_DRIVE_ATTR_LAYER`（层内那套属性），页内标记
      //    `CONTROL_DRIVE_ATTR` 一个都不动（见函数体里的 `CONTROL_DRIVE_ATTR_LAYER`）。
      //    ## 代价（原来是无条件的）
      //    原实现**每个驱动过的页内控件**都重新枚举一次（不只是开层的那些）——`admin` 的
      //    `controlsClicked≈421`，代价远超真实开层次数（本轮实测开层阶段 admin 21 个层 / cs 2 个）。
      //    现在只按「这次点击新开出来的层数 `stage.opened`」门控。
      //    ⚠️ 兜底仍在：下面循环顶部那条 `markedCount() !== snapshot.drivable.length` 的廉价比对
      //    覆盖"没开层但 DOM 重渲染了"的形态（那时标记数会变），两条一起才完整。
      if (stage.opened > 0) {
        snapshot = await enumerateCandidateControls(page, FRONTEND_URL, {
          root: page.frameLocator(ISLAND_IFRAME).locator(CONTROL_SCOPE),
          scope: '',
          tagPrefix: CONTROL_DRIVE_TAG_PREFIX_PAGE,
          driveAttr: CONTROL_DRIVE_ATTR,
          recordNonBtnSkipped: true,
        })
      }
    }
    // 同一身份串里**没进驱动计划**的其余元素：全部记进去重账（⛔ 不许静默少点）。
    // ⚠️ 两类要分开记，读报告的人才知道"T3 那 80 条点不动去哪了"：
    //  - **横向够不着的副本**（`!hittable && !inViewportX`，实测就是主表那份 `left ≈ 1505–1595` 的按钮）
    //    ⇒ `dedupedUnhittable`（T7 改点了可命中的克隆那一份）；
    //  - **够得着、只是超出 `DEDUPE_N` 上限**（含纵向在视口外、滚一下就能点的克隆）⇒ `dedupedDuplicates`。
    for (const entry of group) {
      if (used.has(entry.tag)) continue
      if (!entry.hittable && !entry.inViewportX) recordDedup(entry, UNHITTABLE_COPY_REASON, true)
      else recordDedup(entry, DEDUPE_REASON, false)
    }
  }
  result.drivable = result.clickedIdentity.length
  // ⛔ 既有字段名一个都不改（T1 的任务书要求「保持现有日志与计数口径」），**层内读数追加在后面**：
  //    既有的解析（人与脚本）不受影响，新的三个数一眼可见。
  process.stderr.write(
    `[T7-PAGE] ${path} 候选=${result.candidateTotal} 可驱动=${result.drivableTotal}` +
      ` 点过=${result.clickedIdentity.length} 点不动=${result.clickFailures.length}` +
      ` 去重=${result.dedupedDuplicates} 点不动的副本=${result.dedupedUnhittable}` +
      ` 403发现=${result.findings.length} 耗时=${((Date.now() - tPage) / 1000).toFixed(1)}s` +
      ` 层内点过=${result.layerControlsClicked} 层内可驱动=${result.layerDrivableTotal}` +
      ` 层内未驱动=${result.skipped.filter((it) => it.reason.startsWith('（层内')).length}` +
      ` 开层=${result.layers.length} 跳过iframe层=${result.layersSkippedIframe}` +
      ` 跳过同批开的层=${result.layersCoOpenedSkipped} 关层失败=${result.layerCloseFailures}\n`,
  )
  return result
}

/* ——— ③c 参数化详情页：用**该职位自己的会话**取实例（detail-pages-audit spec §2） ——— */

/**
 * 整轮里**至少解析出一个实例化页面**（spec §5 的 **D1** 的全局那一半）。
 *
 * ⛔ **真正的载体是文件**（`INSTANTIATED_SENTINEL`），本变量只是**同进程内的快捷值**：
 * 用例失败后 worker 会重启、模块级变量清零 ⇒ 单靠它会让重启后的 `afterAll` **假红**
 * （T1 执行期实测踩到，见 `INSTANTIATED_SENTINEL` 的注释）。判定处两者取或。
 * ⚠️ 本变量**不是**承重的那一个（文件才是）：同进程内它省一次磁盘读，且让"这一遍确实解析到了"
 * 这件事在用例体里**可读**。⛔ 别把它写成"没它就不行"。
 * ⚠️ **语义口径**：它只看"**解析出来过**"，⛔ 不看"驱动过"——"驱动"那一半由 **D2**
 * （实例化页候选数下界）单独守（两枚钉子各自可弄红，见 spec §8.6）。
 */
let instantiatedPageDrivenInRound = false

/** 一次「实例化」的汇总（`resolveParameterizedInstances()` 的返回值）。 */
type InstantiationOutcome = {
  /** 解析出来、**该进该职位页面清单**的具体路径（已按 `detailOf` 可达性过滤、已过形状校验）。 */
  paths: string[]
  /** 逐条登记（成功的进 `AuditRow.parameterized`）——顺序与 `PARAMETERIZED_ROUTES` 一致。 */
  resolved: AuditRow['parameterized']
  /** 逐条 `noData`（这个职位就是没数据 ⇒ 进 `AuditRow.noInstance`，⛔ 不判红）。 */
  noInstance: AuditRow['noInstance']
  /** 逐条**解析契约坏**（进 `AuditRow.contractBroken`，**必须红**——评审修复波第 3 条）。 */
  contractBroken: AuditRow['contractBroken']
  /** 本职位**父页可达**的登记条数（D1 的分母：这些是"本该取到实例"的）。 */
  parentReachable: number
}

/**
 * 对**这个职位**逐条参数化路由**取实例**（spec §2）——只对「父列表页在该职位可达」的那些做。
 *
 * ## 三条纪律（spec §2 / §3 各一条）
 *  1. **用该职位自己的会话**：`PARAMETERIZED_ROUTES[].resolve` 走 `page.request.*`（带 cookie 罐）。
 *     ⛔ 不许拿管理员的实例硬塞给别的职位——"谁看得见哪一笔"是**数据权限**维度。
 *  2. **父页不可达 ⇒ 不进清单、也不取实例**：这条详情页对某职位"看不见"，本来就该跳过。
 *     ⚠️ 这一步是**推导**（`detailOf ∈ reachable`），⛔ 不是白名单。
 *  3. **取不到 ⇒ 按两类分开**（spec §2 的两分类；评审修复波第 3 条）：
 *     - `noData`（HTTP 200 / 业务码 0 / 列表为空）⇒ 记 `noInstance` 并跳过，⛔ **不判红**
 *       （数据短缺不是权限缺陷，覆盖率与数据相关）；
 *     - `contractBroken`（网络层失败 / HTTP 非 2xx / 业务码非 0 / 响应缺字段 / 实例不过形状判据）
 *       ⇒ 记 `contractBroken`，由用例里那条断言**当场红**（文案点名路由与坏在哪一步）。
 *
 * ## 为什么返回值要分四份（`paths` / `resolved` / `noInstance` / `contractBroken`）
 * `paths` 给驱动循环用；后三份给**报告**用——`resolved` 让人看见"扫的是哪几条具体路径"，
 * `noInstance` 让人看见"哪几条**没数据**"（正常），`contractBroken` 让人看见"哪几条**坏了**"（要红）。
 * ⛔ 只给一个计数就等于把这些都糊掉了——而混成一种正是评审点名的**掩盖窗口**。
 */
async function resolveParameterizedInstances(
  page: Page,
  role: string,
  reachable: readonly string[],
): Promise<InstantiationOutcome> {
  const out: InstantiationOutcome = {
    paths: [],
    resolved: [],
    noInstance: [],
    contractBroken: [],
    parentReachable: 0,
  }
  for (const route of PARAMETERIZED_ROUTES) {
    // ② 父列表页在该职位**可达**吗？不可达 ⇒ 这条详情页对它是"看不见"的，跳过（⛔ 不取实例）。
    if (!reachable.includes(route.detailOf)) continue
    out.parentReachable += 1
    const result = await route.resolve(page)
    if (result.ok) {
      out.paths.push(result.path)
      out.resolved.push({
        pattern: route.pattern,
        routeName: route.routeName,
        path: result.path,
        shapeValue: result.shapeValue,
      })
      console.log(
        `  🧩 实例化（${role}）：${route.pattern} ⇒ ${result.path}` +
          `（detailOf=${route.detailOf}；取实例端点 ${result.api.endpoint} ⇒ HTTP ${result.api.httpStatus}` +
          `/ code ${String(result.api.code)}）`,
      )
      continue
    }
    // ③ 取不到 ⇒ **按两类分开**（spec §2）：`noData` 不判红；`contractBroken` 必须红。
    const api = result.api
    const apiText =
      api === null
        ? '（这次请求没有拿到响应）'
        : `${api.endpoint} ⇒ HTTP ${String(api.httpStatus)} / code ${String(api.code)}` +
          (api.msg ? ` / msg「${api.msg}」` : '')
    const entry = {
      pattern: route.pattern,
      routeName: route.routeName,
      detailOf: route.detailOf,
    }
    if (result.kind === 'noData') {
      out.noInstance.push({ ...entry, reason: `${result.reason}｜${apiText}` })
      console.log(
        `  ⚠️ noInstance（没数据，不判红）（${role}）：${route.pattern}（detailOf=${route.detailOf}）—— ` +
          `${result.reason}｜${apiText}`,
      )
    } else {
      out.contractBroken.push({ ...entry, where: result.where, reason: `${result.reason}｜${apiText}` })
      console.log(
        `  ⛔ 契约坏（**必须红**）（${role}）：${route.pattern}（detailOf=${route.detailOf}）—— ` +
          `坏在「${result.where}」：${result.reason}｜${apiText}`,
      )
    }
  }
  return out
}

/* ——— ④ 两侧差集：报错文案要能指认方向 ——— */

const diff = (a: readonly string[], b: readonly string[]): string[] =>
  a.filter((x) => !b.includes(x))

/* ——— ⑤ 用例本体 ——— */

/**
 * ⛔ **`mode: 'default'`（T2 修复轮，任务书第 ② 条），不是 `serial`**：
 * `serial` 下**第一个职位失败会让后面 4 个显示 `did not run`** ⇒ 报告里只有 1 行，
 * 而这是**报告型**的网——**缺 4 行比红更糟**（看不到全貌）。本线要的是「一个职位失败不影响
 * 其它职位继续跑并写出自己那行」。
 *
 * ⚠️ **不会因此把 5 个职位真并发起来**（并发会互相抢账号 / 抢同一条 `page`）：
 * `e2e/playwright.config.ts` 钉死 **`workers: 1` + `fullyParallel: false`** ⇒
 * 本文件里的用例在**同一个 worker 里逐条串行**执行，只是「前一条失败不再跳过后面」。
 */
test.describe.configure({ mode: 'default' })

/**
 * ⛔ **不要用全局的 60s**（`e2e/playwright.config.ts` 的 `timeout`）：本文件一个职位要逐条导航
 * **60 条候选路由 × 约 1.3 秒 ≈ 78 秒**，用 60s 会把用例**卡在中间某条路由上**、报成
 * 「这条路由打不开」——那是探针自己的超时错觉，不是被测系统坏了（`04` 的注释里记过同款坑）。
 *
 * T3 起还多了一层**控件驱动**：每个职位 = `reachable` 的每一页 × 每页的候选控件（一次真实点击 +
 * 一小段等待）。⚠️ 控件条数取决于 E2E 租户里**恰好有什么数据**（spec §11 的残差：数据是累积的）
 * ⇒ 这里**不写"实测 N 条"**（那数字一定会过期，`E-0011`）。
 *
 * ⚠️ **T7 起这个预算只是"松的上界"，不再是"预计要花的钱"**：T3 实测 cs 一个职位约 20 分钟
 * （其中 `/orders` 一页就 917s），而瓶颈是**点不动的控件每条等满 5s 动作超时**——T7 把
 * ①等待改成"候选数稳定就收工"（每页约 1.2s 而不是固定 5s）、②点不到的副本改点可命中的那一份、
 * ③同身份串去重到 `DEDUPE_N` 条、④点击预算降到 `CLICK_TIMEOUT_MS`。保留 45 分钟是为了
 * **不让预算本身成为假红**（E2E 租户的数据是累积的，某天控件条数可能真的变多）；
 * 真超了会红，那时该做的是查为什么这么慢，⛔ 不是把预算再调大。
 */
test.setTimeout(45 * 60_000)

let page: Page
const roleOrder: string[] = []

test.beforeAll(async ({ browser }) => {
  // ⛔ **装写策略之前**先做闸门自检（写模式 spec §3 的第 2 道闸门，fail-closed）：
  //    `TT_E2E_WRITE=1` 表示**这一轮连的是一次性写租户**（凭据 / 域名由运行器与
  //    `playwright.config.ts` 选好）；此时若 worker 没拿到 `TT_E2E_TARGET=write`
  //    （运行器没接上目标），就**不许**放行写——宁可少覆盖，也不能对未知租户放行写。
  //    ⚠️ 这里**抛**（而不是只打印警告）：静默退化成读模式会让 W2 红在一个看不出原因的地方。
  if (process.env.TT_E2E_WRITE === '1' && !WRITE_MODE) {
    throw new Error(
      "闸门拒绝：TT_E2E_WRITE=1（本轮连的是一次性写租户）但 TT_E2E_TARGET 不是 'write'" +
        `（实际是 ${JSON.stringify(AUDIT_TARGET)}）⇒ 拒绝启动。\n` +
        '⛔ 不要手搓环境变量：写模式请跑 `TT_E2E_WRITE=1 node e2e/run.mjs …`' +
        '（`playwright.config.ts` 也会再拒一次这种不一致）。',
    )
  }
  // 重启后这里会再跑一次；`recursive: true` 对已存在的目录是幂等的 ⇒ 不会清掉已写的行。
  mkdirSync(ROWS_DIR, { recursive: true })
  page = await browser.newPage({ baseURL: FRONTEND_URL })
  // ① 写请求策略（读模式拦断 / 写模式放行，spec §3.3 + 写模式 spec §3/§4）：**一次装好**，
  //    页面的整个生命周期 + 5 个职位都生效。
  installWriteGate(page)
  // ② N1 的假端点处理器（spec §3.4）：同样一次装好（正对照在每个职位开跑前各打一次）。
  installAuditProbe(page)
  // ③ 403 观测器（+ 写模式的异常响应诊断）。
  installForbiddenObserver(page)
})

test.afterAll(async () => {
  await page?.close()
  // 从**每个小文件**聚合（顺序按矩阵，与跑的顺序无关）⇒ worker 重启也丢不了行。
  // ⚠️ 目录由 `e2e/run.mjs` 在每次整体运行前清空一次（`beforeAll` **不能**清：重启会再跑它）。
  const rows: AuditRow[] = []
  const missing: string[] = []
  for (const { role } of ROLE_MATRIX) {
    try {
      rows.push(JSON.parse(readFileSync(rowFileOf(role), 'utf8')) as AuditRow)
    } catch {
      missing.push(role)
    }
  }
  writeFileSync(REPORT_PATH, JSON.stringify(rows, null, 2))
  // ⛔ 「缺行」必须**看得见**：报告少一行而没人说，就正落回「报告比它看起来少」这个坑。
  //    这里不抛（真正的失败已经由用例自己报了），但用醒目的前缀打到运行输出里。
  if (missing.length) {
    console.warn(
      `⚠️ 职位对拍报告缺 ${missing.length} 行（这些职位没写出自己的行）：${missing.join('、')}`,
    )
  }
  console.log(
    `📋 职位对拍报告：${rows.length} 行（期望 ${ROLE_MATRIX.length}）→ .tools/e2e/role-audit.json` +
      (roleOrder.length ? `（本次实际跑过的职位顺序：${roleOrder.join(' → ')}）` : ''),
  )
  // ⛔ **D1 的全局那一半**（spec §5）：「整轮至少要真驱动过一个实例化页面」。
  //    ⚠️ 为什么判在 `afterAll`：`instantiatedPageDrivenInRound` 是**跨用例**的模块级开关
  //       （单跑一个职位时它不能替别的职位下结论，所以本条的"整轮"量词要有整轮的载体）。
  //    ⛔ 载体的选择见 `INSTANTIATED_SENTINEL` 的注释：**用文件、不用模块级变量**——
  //       失败后 worker 会重启、模块级变量清零，拿它来判"整轮"会**假红**（T1 执行期实测踩到）。
  //
  //    ## ⛔ 适用范围**只看"本次真的跑了整轮矩阵"**（评审修复波第 1 条）
  //    原来的条件是 `roleOrder.length > 0`——**太宽**：`--grep "职位对拍 taker"` 这种**只跑一个职位**、
  //    且该职位**父页都不可达**（taker 到不了 `/orders` / `/system-users`）时，本条会因为
  //    "整轮一条实例化页都没有"而红，而那是**当然的**（它本来就不该有）⇒ **假红**（评审已实测复现）。
  //    现在两个条件都要满足才判：
  //      ① `rows.length === ROLE_MATRIX.length`（本次**真的写了整轮**每一个职位的行）；
  //      ② 本轮**存在父页可达的职位**（`PARENT_REACHABLE_SENTINEL`，单跑 taker / dev 时为假）。
  //    ⚠️ 那 ② 为什么也要：整轮里若除 admin 外父页都不可达、而 admin 那一遍恰好在解析前崩了，
  //       没有 ② 就会把"其它职位当然没有"报成本条红。⚠️ 单职位的"父页可达却解析契约坏"由
  //       **该职位自己**的 D1 ① 守（见主用例），⛔ 不靠本全局条。
  //    ⚠️ 失败分叉：`contractBroken` 出现 ⇒ 红；失败**全是** `noData` ⇒ 覆盖率与数据相关、**不判红**
  //       （spec §2/§6 的两分类，评审修复波第 7 条）。
  const instantiatedInRound = existsSync(INSTANTIATED_SENTINEL) || instantiatedPageDrivenInRound
  const anyParentReachableInRound = existsSync(PARENT_REACHABLE_SENTINEL)
  const anyContractBrokenInRound = existsSync(CONTRACT_BROKEN_SENTINEL)
  const ranWholeMatrix = rows.length === ROLE_MATRIX.length
  if (ranWholeMatrix && anyParentReachableInRound && !instantiatedInRound) {
    if (anyContractBrokenInRound) {
      throw new Error(
        'D1（整轮）：整轮**一条实例化详情页都没解析出来**，且本轮出现过**解析契约坏**' +
          '（`PARAMETERIZED_ROUTES` 的解析 / 入清单 / 驱动这三步里至少有一处静默失效了）。\n' +
          '⚠️ 逐条原因见各职位那行的 `contractBroken` / 运行输出里的 `⚠️ 契约坏` 行。',
      )
    }
    // ⛔ 两分类（spec §2）的整轮口径：**全部失败都是"没数据"⇒ 不判红**（覆盖率与数据相关）。
    //    这正是评审修复波第 7 条要统一的口径（D1 不许把"租户里没有订单"判红）。
    console.log(
      '⚠️ D1（整轮）：整轮一条实例化详情页都没解析出来，但**每一条失败都被判为「没数据」**' +
        '（HTTP 200 / 业务码 0 / 列表为空）⇒ **不判红**（覆盖率与数据相关，spec §2/§6）。\n' +
        '   ⛔ 读这一行时别把它当成"这两页没问题"——是**没看到**，不是看过没问题。',
    )
  }
})

for (const { role, label } of ROLE_MATRIX) {
  // ⚠️ 标题里**必须**带职位键（`职位对拍 cs（客服）` 这种形状）：本线的常用命令是
  //    `node e2e/run.mjs --grep "职位"`（整轮）与 `--grep "职位对拍 cs"`（只跑一个职位，约 1 分钟），
  //    后者靠的就是这个串。⛔ 别把职位键从标题里删掉——那会让「单跑一个职位」的命令静默匹配 0 条
  //    （`E-0085`：判据要能被**运行到**）。
  test(`职位对拍 ${role}（${label}）`, async () => {
    roleOrder.push(role)
    // 行**先建**：任何断言失败也保证这个职位在报告里有行，否则失败的职位反而从报告里消失、
    // 表长会缩——那正是最该留痕的那个（`04` 的先例）。
    const row: AuditRow = emptyRow(role, label)

    try {
      // —— 写模式：**每个职位之间重置一次**一次性写租户（spec §4）——
      // ⛔ 为什么必须：写模式会**真的删数据**（配置 / 账号那几页的「删除」按钮真的会落库），
      //    不重置就会让后一个职位面对被前一个职位改烂的租户（覆盖坍塌 + 假红）。
      // ⛔ 失败**抛**（`resetWriteTenant()` 里每一道闸门 / 子进程非 0 都抛）⇒ 响亮失败，
      //    ⛔ 绝不静默继续（"半重置"的租户会让这一轮的读数整个不可信）。
      // ⚠️ 位置：**登录之前**——重置会重建账号（口令回到夹具值）⇒ 必须在换账号登录前完成。
      // ⚠️ 运行器跑前已经重置过一次（`run.mjs`）；这里这一次是**逐职位**的，两者都要。
      if (WRITE_MODE) {
        resetWriteTenant({ label: `职位对拍 ${role}` })
        row.resets += 1
      }
      // 换账号走**夹具层**的实现（清 Cookie → 整文档导航 → 真登录表单），⛔ 不在 spec 里另写一遍。
      await switchAccount(page, role)

      // ① 可见性：`/` 上读**渲染出来的**菜单（唯一出处是 DOM）。
      await expect(page.locator(SIDEBAR_MENU), `${role}：侧栏菜单没渲染出来`).toBeAttached({
        timeout: 20_000,
      })
      // ⛔ 不能只断「菜单容器在」：容器在而一条条目都没有时，`menuVisible` 会静默变空集，
      //    然后把「守卫放行但菜单看不见」全报成产品缺陷——那是**探针自己的错觉**。条数下界由这里响亮拦下。
      const leafCount = await page.locator(`${SIDEBAR_MENU} li.el-menu-item`).count()
      expect(leafCount, `${role}：侧栏菜单渲染出 0 条条目（读 DOM 的这一路没接上）`).toBeGreaterThan(0)
      const menuItems = await readMenuItems(page)

      // ② 路由守卫的放行面：逐条导航 60 条候选路由。
      const reachable = await reachablePaths(page)

      // ③ 把 DOM 条目翻译成路径（翻译不出 / 歧义 ⇒ 响亮失败；置灰条目进 notes、两侧都不计）。
      const menuVisible = [...new Set(pathsOfItems(menuItems, role, row.notes))].sort()

      row.reachable = reachable
      row.menuVisible = menuVisible
      row.onlyReachable = diff(reachable, menuVisible)
      row.onlyVisible = diff(menuVisible, reachable)

      /* ——— ③b 实例化参数化详情页（detail-pages-audit spec §2）———
       * ⛔ 顺序是**承重**的：取实例必须在 `reachable` **算完之后**（它要拿 `detailOf` 判父页可达性），
       *    而驱动循环在下面 ⇒ 实例化页要在那之前拼进清单。 */
      const instantiation = await resolveParameterizedInstances(page, role, reachable)
      row.parameterized = instantiation.resolved
      row.noInstance = instantiation.noInstance
      row.contractBroken = instantiation.contractBroken
      const instantiatedPaths = instantiation.paths
      /** 驱动面 = 守卫放行的页面 ∪ 实例化出来的详情页（详情页不占菜单位，是"从列表行进入"的）。 */
      const drivePaths = [...reachable, ...instantiatedPaths]

      // ④ 判据本体（spec §3.1 的 N2，T2 修复轮按控制方裁定从「集合相等」改成**两条判据**）。
      //    ① `onlyVisible` 空；② `onlyReachable ⊆ HIDDEN_MENU_PATHS`。
      //    ⛔ `HIDDEN_MENU_PATHS` 由 `fixtures/routes.ts` **从 `menu.ts` 推导**，不是写死的白名单
      //       ——写死就检不出「新接了路由却忘了加菜单入口」。
      //
      //    ## ⛔ 判据②**不覆盖**参数化详情页（评审修复波第 2 条：删掉那个死并集）
      //    本线第一版把上界写成 `HIDDEN_MENU_PATHS ∪ instantiatedPaths`，**那是死代码**（`AGENTS.md`
      //    §5E「防御性写法别写成承重逻辑」的现场）：
      //      `onlyReachable = reachable \ menuVisible`，而 `reachable` 只从 `CANDIDATES`（61 条、
      //      **一条都不含 `:`**、不含任何具体详情路径）派生 ⇒ 它与 `instantiatedPaths`（具体详情路径）
      //      **永不相交** ⇒ 这个并集**改不了** `undeclared` 的任何一位。
      //    ⚠️ 评审推演过反例：把 `detailOf` 改成一条**不可达**的父页，最终仍会红——但红的是
      //      **D1 的 per-role 那条**（父页可达却零实例 / 解析契约坏），⛔ 不是判据②。
      //    ⇒ 现在只留 `HIDDEN_MENU_PATHS`，并把"参数化详情页由 D1/D2 守、判据②不覆盖它"写进文档
      //      （spec §3 / README 已同步订正）。
      const undeclared = row.onlyReachable.filter((p) => !HIDDEN_MENU_PATHS.has(p))
      row.matched = row.onlyVisible.length === 0 && undeclared.length === 0

      // 醒目的读数（跑一次就能看见每个职位的两列条数与两条判据的读数，不必翻 JSON）。
      console.log(
        `职位对拍 ${role}（${label}）：reachable=${reachable.length} menuVisible=${menuVisible.length} ` +
          `onlyVisible=${row.onlyVisible.length}（须 0） onlyReachable=${row.onlyReachable.length}` +
          `（须 ⊆ menu.ts 有意隐藏的 ${HIDDEN_MENU_PATHS.size} 条；参数化详情页由 D1/D2 守、不在本条内）` +
          ` 实例化=${row.parameterized.length}条 noInstance=${row.noInstance.length}条` +
          ` 契约坏=${row.contractBroken.length}条`,
      )
      // 实例化路径**逐条打出来**（spec §7 第 1 条要求"报告里出现实例化页面的路径与逐职位计数"）：
      // ⛔ 只给计数的话，读报告的人看不出"到底扫了哪几条"。
      if (row.parameterized.length) {
        console.log(
          `  🧩 实例化路径（${role}）：` +
            row.parameterized.map((p) => `${p.path}（${p.pattern}）`).join(' / '),
        )
      }
      if (row.onlyVisible.length) {
        console.log(
          `  ⚠️ 菜单里看得见但路由守卫不放行：${row.onlyVisible
            .map((p) => `${p}（菜单标签：${(MENU_ROUTE_TO_LABELS.get(pathToRouteName(p) ?? '') ?? []).join('、') || '?'}）`)
            .join(' / ')}`,
        )
      }
      if (row.onlyReachable.length) {
        console.log(
          `  ⚠️ 守卫放行但菜单里看不见：${row.onlyReachable.join(' / ')}` +
            (undeclared.length ? ` ← 其中**非有意隐藏**的：${undeclared.join(' / ')}` : '（全部是有意隐藏的）'),
        )
      }

      // 判据①：菜单里看得见 ⇒ 守卫必须放行。⛔ 不许放宽成「包含」。
      //    两侧来源不同（一个读 DOM、一个看导航终态）⇒ 不是自指断言（`E-0024`）。
      expect(
        row.onlyVisible,
        `${role}：菜单里看得见但路由守卫不放行（点了会被弹回首页）：${row.onlyVisible.join('、')}`,
      ).toEqual([])
      // 判据②：守卫放行但菜单看不见的，**只允许**是 menu.ts 里有意隐藏的那些。
      //    ⚠️ 自证不是空转：`HIDDEN_MENU_PATHS` 若推导失败成空集，admin 的 21 条当场违反本条 ⇒ 自然红。
      //    ⛔ **参数化详情页不在本条内**（评审修复波第 2 条）：`onlyReachable` 只从 `CANDIDATES` 派生，
      //       而 `CANDIDATES` 一条都不含 `:`，与具体详情路径永不相交 ⇒ 把这个并集加进上界是**死代码**。
      //       详情页由 **D1/D2** 守（见下面的 D1 ① 与 D2）；本线 spec §3 / README 已按此订正。
      expect(
        undeclared,
        `${role}：守卫放行但菜单里看不见、且**不是** menu.ts 里有意隐藏的那些` +
          `（新接了路由却忘了加菜单入口？）：${undeclared.join('、')}\n` +
          `（本次全部 onlyReachable=${row.onlyReachable.length} 条：` +
          `${row.onlyReachable.join('、') || '（空）'}；上界 = menu.ts 有意隐藏的 ` +
          `${HIDDEN_MENU_PATHS.size} 条；⛔ 参数化详情页不在本条内，见 D1/D2）`,
      ).toEqual([])

      /* ——— T3：控件驱动（spec §3.2 / §3.3 / §3.4 的 N1、N3） ——— */

      // N1 正对照：**必须在驱动之前**打（否则"整张网恒绿"这件事不会被发现）。每个职位各打一次。
      await runAuditProbe(page)
      // ⛔ 到这一步才把**写拦断**打开：先前的登录 / 导航期一律放行（见 `drivingWritesAborted` 的注释）。
      drivingWritesAborted = true

      const clickFailures: { page: string; control: string; reason: string }[] = []
      const pagesWithNoControls: string[] = []
      const pagesNeverRendered: string[] = []
      const clickedNotVisited: string[] = []
      const pageResults: PageDriveResult[] = []
      const driveStart = Date.now()
      const abortedBefore = abortedWrites.length
      // T2：写模式的两个流水也在驱动前切一刀（口径与 `abortedBefore` 完全一样）。
      const writesPerformedBefore = performedWrites.length
      const writeIssuesBefore = writeModeIssues.length
      // ⭐ 评审修复波 A：**模式无关**的观测也切同一刀（口径与上面三个完全一样）。
      const nonGetResponsesBefore = nonGetApiResponses.length
      // ⛔ 驱动面 = `reachable` + **实例化出来的详情页**（`drivePaths`）。
      //    ⚠️ **订正（评审修复波第 10⑤ 条）**：这里原来写"用独立的 local 是为了让
      //    `pagesWithNoControls` 等既有读数**仍能分清两者**"——**不成立**。下面这个循环遍历的是
      //    **`drivePaths`**（含详情页）⇒ `pagesWithNoControls` / `pagesNeverRendered` / `notDriven` /
      //    `drivableTotal` / `notes` 这些既有键**已经把详情页算进去了**；它们今天**分不出**哪一条是
      //    详情页。⚠️ 但**不必**分：那些键的口径是"**本次驱动面**"（`reachable ∪ 实例化详情页`），
      //    而不是"守卫放行面"——要单独看详情页请读 `row.parameterized` / `instantiatedCandidateTotal`
      //    与 `[T7-PAGE]`（它逐页打了路径）。
      const instantiatedPathsSet = new Set(instantiatedPaths)
      /** 实例化路径 → 登记表里的模板（`instancePageUnreachable` 要带上它）。 */
      const instantiatedPatternByPath = new Map(
        row.parameterized.map((p) => [p.path, p.pattern] as const),
      )
      /**
       * ⛔ `instancePageUnreachable` 那一档的**自证**（评审裁定 a 第 1 条）：它**只允许**出现在
       * 「导航终态**真的**是 `/`」时。终态不是 `/` 的实例化页进这里 ⇒ 主用例**当场红**
       * ——⛔ 这一档不是"万能跳过"后门。
       */
      const badInstantiatedBounce: string[] = []
      for (const path of drivePaths) {
        const driven = await driveControlsOnPage(page, path)
        pageResults.push(driven)
        // ⛔ D2：把**实例化页面**上的候选数单独累加（分母与 `totalCandidate` 是同一批 `driven`，
        //    所以两者天然同源；这一栏只是把"新页贡献了多少"从总数里拆出来，让人能核账）。
        if (instantiatedPathsSet.has(path)) row.instantiatedCandidateTotal += driven.candidateTotal
        /**
         * ⭐ 「实例解析到了、但这一页对该职位不可达」= 被守卫弹回 `/`（评审裁定 a）。
         * ⛔ 判据取**导航终态**（`settledPathAtEntry`），⛔ **不取 `visited`**：后者被改坏时
         * 「分类」与「计数」会一起变、判据自洽（本条的"弄红"证明正是打这个点）。
         * 正常驱动到这一页时终态 === `path`；终态是 `/` ⇒ 被路由守卫弹回（合法的最小权限行为）。
         */
        const bouncedAway = instantiatedPathsSet.has(path) && driven.settledPathAtEntry !== path
        const isUnreachableInstance = bouncedAway && driven.settledPathAtEntry === '/'
        if (bouncedAway) {
          if (isUnreachableInstance) {
            row.instancePageUnreachable.push({
              role,
              pattern: instantiatedPatternByPath.get(path) ?? '（未知模板）',
              url: path,
              reason:
                `父页可达、实例解析成功，但导航 ${path} 后终态是 ${driven.settledPathAtEntry}` +
                '（该职位不持这条详情页要求的前置节点）⇒ 记录、既不判红也不算覆盖缺失',
            })
          } else {
            badInstantiatedBounce.push(
              `${path}（终态=${driven.settledPathAtEntry || '（空）'}）`,
            )
          }
        }
        row.findings.push(...driven.findings)
        clickFailures.push(...driven.clickFailures)
        row.controlsClicked += driven.clickedIdentity.length
        // T7：把"没驱动的重复元素"的账**逐条**收上来（⛔ 不许静默少点，见 `DEDUPE_N` 的边界）。
        row.dedupedDuplicates += driven.dedupedDuplicates
        row.dedupedUnhittable += driven.dedupedUnhittable
        row.drivableTotal += driven.drivableTotal
        // ⛔ **T1：层内的账单独累加**（⛔ 不混进 `controlsClicked` / `drivableTotal`——那两个键是
        //    N3 的**页内**恒等式的分子分母，混进来会把既有判据的口径弄脏；层内另有自己的恒等式）。
        row.layerControlsClicked += driven.layerControlsClicked
        row.layerDrivableTotal += driven.layerDrivableTotal
        row.layerDedupedDuplicates += driven.layerDedupedDuplicates
        row.layerDedupedUnhittable += driven.layerDedupedUnhittable
        row.layers.push(...driven.layers)
        row.layersSkippedIframe += driven.layersSkippedIframe
        row.skippedIframeLayers.push(...driven.skippedIframeLayers)
        row.layersCoOpenedSkipped += driven.layersCoOpenedSkipped
        row.layerClickFailures += driven.layerClickFailures.length
        for (const it of driven.deduped) {
          row.deduped.push({ page: path, control: it.identity, reason: it.reason })
        }
        // ⭐ 被守卫弹回 `/` 的实例化页**不算**这两个读数（它进了 `instancePageUnreachable`）：
        //    它不是"这页没有控件"、也不是"窗口内没渲染出来"，而是"这一页对该职位本来就不开放"。
        if (driven.candidateTotal === 0 && !isUnreachableInstance) pagesWithNoControls.push(path)
        // ⛔ 「没有候选控件」有两种来源，报告里必须能分开：**渲染出来了但确实没有** vs
        //    **观察窗口内什么都没渲染出来**（后者更像探针 / 数据问题，不该被当成"这页干净"）。
        if (driven.candidateTotal === 0 && !driven.rendered && !isUnreachableInstance) {
          pagesNeverRendered.push(path)
        }
        // 候选里有、但没被驱动的那些（路由链接 / 驱动选择器不含的）——⛔ 显式留痕，不许静默漏点。
        for (const it of driven.skipped) {
          row.notDriven.push({ page: path, control: it.identity, reason: it.reason })
        }
        // ⛔ 「一个候选控件都没有」只在**成功访问过这一页**时才成立：没访问到就"没有控件"是
        //    把探针失败伪装成豁免（那正是 N3 要堵的**静默放行**）。
        //    ⭐ 例外：实例化页**被守卫弹回 `/`** 那一档已单独入账（`instancePageUnreachable`），
        //    它既不判红也不算覆盖缺失 ⇒ 不在这里重复报（评审裁定 a）。
        if (driven.candidateTotal === 0 && !driven.visited && !isUnreachableInstance) {
          clickedNotVisited.push(path)
        }
        // 每页的读数进 `notes`：不必翻 JSON 也能看出"哪一页一条控件都没枚举到"。
        row.notes.push(
          `控件：${path} 候选 ${driven.candidateTotal} 条 / 可驱动 ${driven.drivableTotal} 条` +
            ` / 点过 ${driven.clickedIdentity.length} 条` +
            (driven.dedupedDuplicates + driven.dedupedUnhittable
              ? ` / 去重未驱动 ${driven.dedupedDuplicates + driven.dedupedUnhittable} 条` +
                `（其中同身份串重复 ${driven.dedupedDuplicates}、被克隆体盖住的副本 ${driven.dedupedUnhittable}，见 deduped）`
              : '') +
            (driven.skipped.length ? ` / 未驱动 ${driven.skipped.length} 条（见 notDriven）` : '') +
            (driven.enumerationFailed ? ` / ⚠️ ${driven.enumerationFailed}` : ''),
        )
      }
      // ⛔ `instancePageUnreachable` 的**自证**（评审裁定 a 第 1 条）：这一档只允许出现在
      //    「导航终态**真的**是 `/`」时。终态去了别处 ⇒ 红（⛔ 不许拿它当"跳过"后门）。
      expect(
        badInstantiatedBounce,
        `${role}：下列实例化页**没被成功访问、终态也不是 / ** —— 它们既不是"没数据"、也不是` +
          `"被守卫弹回 / "（\`instancePageUnreachable\` 只收后者）⇒ 这是探针 / 路由的异常终态，必须红：\n` +
          badInstantiatedBounce.map((s) => `  - ${s}`).join('\n'),
      ).toEqual([])
      row.clickFailures = clickFailures
      row.abortedWrites = abortedWrites.length - abortedBefore
      row.writesPerformed = performedWrites.length - writesPerformedBefore
      // ⭐ 评审修复波 A：W2/W3 的判据取自这条**与模式分支无关**的观测。
      row.nonGetApiResponses = nonGetApiResponses.length - nonGetResponsesBefore
      row.writeIssues = writeModeIssues.slice(writeIssuesBefore)

      /* ——— T2：写模式读数 + **W2 / W3** 两条钉子（spec §4 / §6）———
       * ⛔ 位置在**重断言之前**：W2/W3 一红，后面那一大段读数就打不出来了，
       *    而这两个数恰恰是判红时要看的第一眼（T1 执行期在实例化页读数上踩过同款坑）。
       */
      console.log(
        `  🖊️ 写模式读数 ${role}：mode=${row.mode} writesPerformed=${row.writesPerformed}` +
          `（探针决定放行） 收到响应的非 GET=${row.nonGetApiResponses}（W2/W3 的判据）` +
          ` 拦断写=${row.abortedWrites} resets=${row.resets}` +
          ` 写模式异常响应=${row.writeIssues.length} 条（逐条见报告 writeIssues）`,
      )
      if (WRITE_MODE) {
        // **W2**（spec §6）：写模式下至少真的发生过一次写；下界按职位登记、⛔ 不是从本次读数派生的量。
        const writeFloor = WRITES_PERFORMED_FLOOR_WRITE[role]
        expect(
          writeFloor,
          `${role}：没有登记"写模式下必须放行的写请求数"（\`WRITES_PERFORMED_FLOOR_WRITE\` 里缺这个职位）` +
            '⇒ W2 会静默放行',
        ).toBeDefined()
        if (writeFloor === WRITES_PERFORMED_FLOOR_VACUOUS) {
          expect(
            row.writesPerformed,
            `${role}：登记为「写模式下不放行写请求」（-1），但这一遍**探针放行了 ` +
              `${row.writesPerformed} 笔**非 GET 请求 ⇒ 那个登记已经过期，请按实测重取 ` +
              '`WRITES_PERFORMED_FLOOR_WRITE`。',
          ).toBe(0)
        } else {
          expect(
            row.writesPerformed,
            `${role}：**写模式下探针一笔写请求都没放行**（writesPerformed=${row.writesPerformed}，` +
              `登记下界 ${writeFloor}）——写策略没被切到"放行"？\`TT_E2E_TARGET\` 没接上？` +
              `（拦断写=${row.abortedWrites}，写模式下它应当是 0）这是 W2（spec §6）红。`,
          ).toBeGreaterThanOrEqual(writeFloor)
        }
        // ⭐ **W2 的判据本身**（评审修复波 A）：写模式下**必须**有非 GET `/api/**` 请求**真的收到响应**。
        //    ⛔ 与上面那条**不是**重复：`writesPerformed` 只证明"探针决定放行"（路由分支跑到了），
        //    而这条证明"响应真的回来了"——响应观测器没接上 / 请求被别处拦下时，只有这条会红。
        //    ⚠️ 空位（`-1`）取反：登记为"不发写请求"的职位，收到的响应数也必须**恰好为 0**。
        if (writeFloor === WRITES_PERFORMED_FLOOR_VACUOUS) {
          expect(
            row.nonGetApiResponses,
            `${role}：登记为「写模式下不发写请求」（-1），但这一遍**有 ` +
              `${row.nonGetApiResponses} 条非 GET 请求真的收到了响应** ⇒ 登记过期，按实测重取 ` +
              '`WRITES_PERFORMED_FLOOR_WRITE`。',
          ).toBe(0)
        } else {
          expect(
            row.nonGetApiResponses,
            `${role}：**写模式下没有任何非 GET \`/api/**\` 请求真的收到响应**` +
              `（收到响应的非 GET=${row.nonGetApiResponses}）——写策略没被切到"放行"？` +
              '响应观测器（`page.on(\'response\')`）没接上？这是 W2（spec §6）红。',
          ).toBeGreaterThan(0)
        }
        // 自证不是空转：放行过写时，**拦断数必须为 0**——两个数来自同一条请求的两个**互斥**分支，
        // 同时非零说明"放行"与"拦断"两条记账都跑了（那时 `writesPerformed` 是假的）。
        if (row.writesPerformed > 0) {
          expect(
            row.abortedWrites,
            `${role}：写模式下既有放行的写（${row.writesPerformed} 笔）、又有被拦断的写` +
              `（${row.abortedWrites} 笔）——两个互斥分支同时记账了？`,
          ).toBe(0)
        }
      } else {
        // **W3**（spec §6）：读模式（默认）下**不许有任何写请求真的出网**——这条判据就是
        // 「默认行为没变」本身（写策略一旦泄漏进默认模式，只读租户就会被真的写过）。
        // ⭐ **评审修复波 A**：判据改看**与 `WRITE_MODE` 分支无关**的 `nonGetApiResponses`。
        //    ⛔ 原来的 `expect(row.writesPerformed).toBe(0)` 是**恒真钉子**——`performedWrites.push`
        //    只在 `installWriteGate` 的 `WRITE_MODE` 分支里，而读模式走 `else` ⇒ 它结构性恒 0。
        //    后果：把这里的 `route.abort()` 改成 `route.fallback()`（写请求真的漏到只读租户）时，
        //    那条断言**照样绿**。现在这条能被弄红（证明见 spec §8.3）。
        expect(
          row.nonGetApiResponses,
          `${role}：**读模式（默认）下有 ${row.nonGetApiResponses} 条非 GET \`/api/**\` 请求真的收到了响应**` +
            '⇒ 写策略泄漏进了默认模式（W3，spec §6）。⛔ 只读租户可能已被写过：先查路由处理器。',
        ).toBe(0)
        // 默认模式的另两条"逐字不变"：**不重置**（spec §5）与**不记写模式诊断**。
        expect(
          row.resets,
          `${role}：读模式下重置了 ${row.resets} 次——默认行为必须逐字不变（spec §5：不重置）。`,
        ).toBe(0)
        expect(
          row.writeIssues,
          `${role}：读模式下标出了 ${row.writeIssues.length} 条"写模式异常响应"——` +
            '观测器把写模式那段诊断泄漏进了默认模式（本键在读模式下必须恒为空）。',
        ).toEqual([])
      }
      row.pagesWithNoControls = pagesWithNoControls
      row.pagesNeverRendered = pagesNeverRendered
      // T1：**`layersOpened` 按标题去重计数**（spec §5：`layersOpened`（按标题去重））。
      // ⚠️ 去重口径**只按 `surface`（= `layer:<标题>`）**，⛔ **不按页**：同一个标题的层在不同页上
      //    是"同一个层形状"，本键的含义就是"这次审计见过几种层"。
      row.layersOpened = new Set(row.layers.map((l) => l.surface)).size
      // ⛔ N3 的分母是**枚举出来的可驱动候选总数**（`drivableTotal`），⛔ 不是"实际点过几条"
      //    （后者会让下面的断言恒真，是本仓 `E-0024` 那种自指形状）。
      const totalDrivable = pageResults.reduce((n, r) => n + r.drivableTotal, 0)
      const totalCandidate = pageResults.reduce((n, r) => n + r.candidateTotal, 0)

      // ⛔ **参数化详情页读数单独一行**（本线 spec §7 第 1 条：报告里要有实例化路径与逐职位计数）。
      //    ⚠️ 位置**在断言之前**（T1 执行期实测踩到）：放到最后的话，判据一红这一行就永远打不出来，
      //    而"这次到底实例化页枚举到几条候选"恰恰是判红时要看的第一眼读数。
      console.log(
        `  🧩 参数化详情页（${role}）：实例化=${row.parameterized.length} 条（` +
          (row.parameterized.map((p) => p.path).join(' / ') || '（无）') +
          `） 实例化页候选合计=${row.instantiatedCandidateTotal}` +
          `（下界 ${String(
            (WRITE_MODE ? INSTANTIATED_CANDIDATE_FLOOR_WRITE : INSTANTIATED_CANDIDATE_FLOOR)[role],
          )}，出处见 ${
            WRITE_MODE ? 'INSTANTIATED_CANDIDATE_FLOOR_WRITE' : 'INSTANTIATED_CANDIDATE_FLOOR'
          }）` +
          ` noInstance=${row.noInstance.length} 条（其中**没数据**）` +
          ` 契约坏=${row.contractBroken.length} 条（必须 0）` +
          // ⭐ 评审裁定 a（2026-09-26）：实例解析到了、但页面被守卫弹回 `/` 的条数
          //    （记录、不判红；D2 的访问与下界都按 visited 取）。
          ` 实例页对该职位不可达=${row.instancePageUnreachable.length} 条（记录、不判红）` +
          // ⛔ 整轮候选数（含实例化页）也打出来：`CANDIDATE_FLOOR` 的分子就是它，
          //    读报告的人不必去翻 JSON 就能核对"新页把总数抬到多少"。
          `｜整轮候选=${totalCandidate}（可驱动 ${totalDrivable}；页数 ${pageResults.length}）`,
      )
      // 逐条实例化页的候选数（D2 的**取证**：下界该取多少、这一页是不是白扫了，都要能一眼看见）。
      // ⛔ 取读数用 `pageResults[drivePaths.indexOf(路径)]`：驱动循环**逐条**问了 `drivePaths`
      //    并**逐条** push 进 `pageResults` ⇒ 两个数组按下标一一对应（没有别的 push 点）。
      for (const inst of row.parameterized) {
        const r = pageResults[drivePaths.indexOf(inst.path)]
        console.log(
          `  🧩 实例化页读数（${role}）：${inst.path} 候选=${r?.candidateTotal ?? -1}` +
            `（可驱动 ${r?.drivableTotal ?? -1} / 点过 ${r?.clickedIdentity.length ?? -1}` +
            ` / 访问=${r?.visited ?? false} / 终态=${r?.settledPathAtEntry ?? '（未知）'}）`,
        )
      }

      /* ——— D1：参数化详情页的**解析**必须真的发生过（spec §5；评审修复波第 1/3/7/8 条）——— */
      // ⛔ **落盘位置是承重的**（T1 执行期实测踩到）：必须排在**任何**可能失败的断言**之前**——
      //    否则 D2 一红，worker 重启、模块级开关清零，重启后那次 `afterAll` 会拿清零的值去判
      //    "整轮没驱动过实例化页" ⇒ 报一条**归因错误**的红（真因是 D2，报出来的是 D1）。
      // ⚠️ 三个领航文件都在这里写（同 `INSTANTIATED_SENTINEL` 的载体理由：文件、不是模块级变量，
      //    worker 重启后仍在；`e2e/run.mjs` 每次整体运行前把它们所在的目录整个清掉）。
      if (row.parameterized.length > 0) {
        instantiatedPageDrivenInRound = true
        writeFileSync(INSTANTIATED_SENTINEL, `${role} ${row.parameterized.length}\n`)
      }
      if (instantiation.parentReachable > 0) {
        writeFileSync(PARENT_REACHABLE_SENTINEL, `${role} ${instantiation.parentReachable}\n`)
      }
      if (instantiation.contractBroken.length > 0) {
        writeFileSync(CONTRACT_BROKEN_SENTINEL, `${role} ${instantiation.contractBroken.length}\n`)
      }

      // ## D1 ①（per-role，**本线的主判据**）：父页可达 ⇒ "取实例"这一步**不许坏在契约上**。
      // ⛔ 两分类（spec §2；评审修复波第 3 条）——两类的处置**不对称**，必须分开：
      //   - `contractBroken`（网络层失败 / HTTP 非 2xx / 业务码非 0 / 响应缺字段 / 实例不过形状判据）
      //     ⇒ **当场红**，文案点名**哪条路由**、**坏在哪一步**、并**贴出拿到的原值**；
      //   - `noData`（HTTP 200 / 业务码 0 / 列表**空数组**）⇒ ⛔ **不判红**（覆盖率与数据相关，
      //     数据短缺不是权限缺陷；与 spec §2/§6 的口径一致，评审修复波第 7 条）。
      // ⭐ 这一条同时是 **D1 的真"能红"证明**的落点（评审修复波第 8 条）：把 `resolve` 读的字段名
      //    写错 ⇒ 这条红。⚠️ 原来 §8.6 那次"把判据关掉 ⇒ 整轮假绿"的变异只证明**该判据不冗余**，
      //    ⛔ **不证明它能红**（`AGENTS.md` §5G）。
      if (instantiation.contractBroken.length > 0) {
        throw new Error(
          `${role}：下列参数化详情页的**解析契约坏了**（父页可达，但"取实例"这一步坏在契约上）` +
            `——⛔ 这不是"没数据"，必须红：\n` +
            instantiation.contractBroken
              .map((n) => `  - ${n.pattern}（detailOf=${n.detailOf}）坏在「${n.where}」：${n.reason}`)
              .join('\n'),
        )
      }
      if (instantiation.parentReachable > 0 && row.parameterized.length === 0) {
        // ⛔ 父页可达却零实例、且**每一条都是"没数据"** ⇒ **不判红**（评审修复波第 7 条）。
        //    这正是两分类要统一的那个矛盾：原来这里把"租户里没有任何订单"（数据没了）也判红，
        //    与 spec §2/§6 的"取不到 ⇒ 记 noInstance 不判红、覆盖率与数据相关"直接冲突。
        console.log(
          `  ⚠️ D1（${role}）：${instantiation.parentReachable} 条参数化路由的父列表页都可达，` +
            `但这一遍一条实例都没解析到——**全部是"没数据"**（HTTP 200 / 业务码 0 / 列表为空）` +
            `⇒ 记 noInstance、**不判红**（覆盖率与数据相关，spec §2/§6）。` +
            `逐条：${row.noInstance.map((n) => n.pattern).join('、') || '（无）'}`,
        )
      }
      // ⛔ ② **只**看"解析出来过没有"（不看"驱动过没有"）："驱动"那一半由 **D2**（实例化页候选数
      //    下界）单独守。两枚钉子各守一步、可各自弄红，是本线想要的形态。

      // ③ **全局候选数下界**（spec §3.4 的 N3′）——补 N3 的洞（见 `CANDIDATE_FLOOR` 的注释）：
      //    N3 只判"有候选的页至少点过一次"，候选选择器整体失效时每页都变成"没有候选"、
      //    N3 反而更绿。这条按**每个职位**钉住枚举规模：塌下来就红。
      //  ⛔ 下界是外部给定的常量（出处见 `CANDIDATE_FLOOR`；写模式另有一套实测的
      //    `CANDIDATE_FLOOR_WRITE`，理由见那张表的注释——数据基线不同），**不是**从本次读数派生的量。
      const floor = (WRITE_MODE ? CANDIDATE_FLOOR_WRITE : CANDIDATE_FLOOR)[role]
      expect(
        floor,
        `${role}：没有登记候选数下界（\`${WRITE_MODE ? 'CANDIDATE_FLOOR_WRITE' : 'CANDIDATE_FLOOR'}\`` +
          ' 里缺这个职位）⇒ 这条判据会静默放行',
      ).toBeGreaterThan(0)
      expect(
        totalCandidate,
        `${role}：整轮只枚举到 ${totalCandidate} 条候选控件，低于覆盖下界 ${floor} 条` +
          `（下界的出处与余量见 \`${WRITE_MODE ? 'CANDIDATE_FLOOR_WRITE' : 'CANDIDATE_FLOOR'}\`）` +
          '——控件枚举器（候选选择器 / island 作用域）静默失效了？',
      ).toBeGreaterThanOrEqual(floor)

      /* ——— D2：实例化页面也计入候选下界（detail-pages-audit spec §5）———
       * 两半：① 上面那条 `CANDIDATE_FLOOR`（**已按新实测重取**，实例化页的候选数在
       * `totalCandidate` 里、也就是在这个分子里）；② 本枚——把**实例化页那一份**单独钉住。
       * ⛔ 没有 ② 的话，「把详情页加进清单、但驱动器根本没跑它」只会让总数少那一页的量，
       *    而那一页的占比被 50% 的余量吃掉 ⇒ **静默通过**（D2 要防的正是这个形态）。 */
      const instantiatedFloor = (WRITE_MODE
        ? INSTANTIATED_CANDIDATE_FLOOR_WRITE
        : INSTANTIATED_CANDIDATE_FLOOR)[role]
      expect(
        instantiatedFloor,
        `${role}：没有登记"实例化页候选数下界"（\`${
          WRITE_MODE ? 'INSTANTIATED_CANDIDATE_FLOOR_WRITE' : 'INSTANTIATED_CANDIDATE_FLOOR'
        }\` 里缺这个职位）⇒ D2 的这半会静默放行`,
      ).toBeDefined()
      /**
       * **真的被访问过**的实例化页条数（评审裁定 a 给 D2 的新分母）。
       * = 解析出来的实例 − **被路由守卫弹回 `/`** 的那些（后者已单独入账 `instancePageUnreachable`）。
       */
      const visitedInstantiatedCount = row.parameterized.length - row.instancePageUnreachable.length
      if (instantiatedFloor === INSTANTIATED_CANDIDATE_FLOOR_VACUOUS) {
        // 空位哨兵（与 `LAYER_CONTROL_FLOOR` 的 `-1` 同一套语义）：本职位**没有可访问的**实例化页 ⇒ 必须为 0。
        // ⭐ **语义订正（2026-09-26，评审裁定 a）**：原来钉的是 `row.parameterized` 为空——
        //    那会把「解析到了、但该职位对该页不可达」这一**合法的最小权限配置**误红
        //    （本线 minrole 实测踩到）。现在钉的是 **visited 的实例化页**。
        // ⛔ **两半都要**（评审修复波第 9 条）：`visitedInstantiatedCount===0` 单独并**不**等于
        //    "没有实例化页候选"——一个"解析出来了、但页面上 0 条候选控件"的实例化页会让它照样绿
        //    （`LAYER_CONTROL_FLOOR` 那条被订正过的同类弱点）。所以再钉候选数为 0。
        expect(
          visitedInstantiatedCount,
          `${role}：下界登记为「本职位没有**可访问**的实例化详情页」（-1），但这一遍有 ` +
            `${visitedInstantiatedCount} 条实例化页**真的被访问过**` +
            `（解析出 ${row.parameterized.length} 条，其中被守卫弹回 \`/\` 的 ` +
            `${row.instancePageUnreachable.length} 条）` +
            ` ⇒ 那个登记已经过期，请按实测重取 \`${WRITE_MODE ? 'INSTANTIATED_CANDIDATE_FLOOR_WRITE' : 'INSTANTIATED_CANDIDATE_FLOOR'}\``,
        ).toBe(0)
        expect(
          row.instantiatedCandidateTotal,
          `${role}：下界登记为「本职位没有可访问的实例化详情页」（-1），但这一遍实例化页上**枚举到了 ` +
            `${row.instantiatedCandidateTotal} 条候选控件**（实例化：` +
            `${row.parameterized.map((p) => p.path).join('、') || '（无）'}）` +
            ` ⇒ 那个登记已经过期，请按实测重取 \`${WRITE_MODE ? 'INSTANTIATED_CANDIDATE_FLOOR_WRITE' : 'INSTANTIATED_CANDIDATE_FLOOR'}\``,
        ).toBe(0)
      } else {
        expect(
          row.instantiatedCandidateTotal,
          `${role}：实例化详情页上只枚举到 ${row.instantiatedCandidateTotal} 条候选控件，低于下界 ` +
            `${instantiatedFloor} 条（**可访问**的实例化页 ${visitedInstantiatedCount} 条；` +
            `下界的出处与余量见 \`${WRITE_MODE ? 'INSTANTIATED_CANDIDATE_FLOOR_WRITE' : 'INSTANTIATED_CANDIDATE_FLOOR'}\`）——` +
            `详情页被"加进清单但不驱动"、或 enumerated 时没渲染出来？`,
        ).toBeGreaterThanOrEqual(instantiatedFloor)
      }
      // ⛔ 自证不是空转（D2 的口径一致性；**评审裁定 a 订正了分母与判别力**）：
      //    `pageResults` 里**真的被成功访问**的实例化页条数，必须等于
      //    「解析出来的 − 按**导航终态**判定被弹回 `/` 的」。
      // ⭐ **两侧来源不同、判据才不是自指**（`E-0024`）：左边读 `pageResults` 的 `visited`，
      //    右边读**导航终态**决定的分类 ⇒ 把 `visited` 改坏（例如被弹回也置 true）时两边**分叉**、
      //    本条当场红（"弄红"证明见 spec §8.5 的第 2 条）。
      // ⛔ 若把右边也写成从 `visited` 派生，这条就是自指、恒真——⛔ 不许那样改。
      const instantiatedInResults = pageResults.filter(
        (r, i) => instantiatedPathsSet.has(drivePaths[i]) && r.visited,
      ).length
      expect(
        instantiatedInResults,
        `${role}：\`pageResults\` 里**被成功访问**的实例化页有 ${instantiatedInResults} 条，` +
          `而按导航终态分类应有 ${visitedInstantiatedCount} 条（解析 ${row.parameterized.length} 条 − ` +
          `被弹回 \`/\` 的 ${row.instancePageUnreachable.length} 条）⇒ 两条记账路径对不上：` +
          `要么 \`visited\` 被改坏、要么 \`instancePageUnreachable\` 收错了页。`,
      ).toBe(visitedInstantiatedCount)

      // ④ N3 覆盖下界（spec §3.4）：**有候选控件的页面，至少要有一次「点过」**。
      //    「点过」= 那次点击**真的发起过**（点击成功 / 点击前的有界命中判定 / 重新导航后发现它已消失，
      //    都算）；判定它的键是 `clickedIdentity`（在这些分支**之前**入账），⛔ 不是"新增了 403"
      //    ——一个能正常用的控件不会产生任何 403，若拿 403 当"点过"，正常页面反而会被判红。
      const pagesWithoutClick = pageResults
        .filter((r) => r.drivableTotal > 0 && r.clickedIdentity.length === 0)
        .map((r) => `${r.enumerationFailed ? `${r.enumerationFailed} ` : ''}（可驱动 ${r.drivableTotal} 条）`)
      expect(
        pagesWithoutClick,
        `${role}：有候选控件、却**一条都没点到**的页面（控件枚举器静默失效了？）：\n` +
          pagesWithoutClick.join('\n'),
      ).toEqual([])
      const pagesWithCandidates = pageResults.filter((r) => r.drivableTotal > 0).length
      // ⑤ 自证不是空转：**每一条枚举出来的可驱动候选都要有归属**——
      //    被驱动、或**显式**计入去重（`dedupedDuplicates` / `dedupedUnhittable`）。
      //    ⛔ T7 起这条**不能**再写成 `点过 >= 可驱动总数`：去重是**有意**少点（`DEDUPE_N` 的取舍），
      //    照旧写会当场判红；而"少点"若**不记账**则是静默漏点——所以判据换成下面这条恒等式。
      //    枚举器少枚举一半、或驱动循环提前退出，都会在这里露出来。
      const accounted = row.controlsClicked + row.dedupedDuplicates + row.dedupedUnhittable
      expect(
        accounted,
        `${role}：枚举出来的可驱动候选（${totalDrivable}）里，有 ${totalDrivable - accounted} 条` +
          `**既没被驱动、也没被记进去重账**（枚举器 / 驱动循环静默少点）：\n` +
          `  点过=${row.controlsClicked} 去重重复=${row.dedupedDuplicates}` +
          ` 点不动的副本=${row.dedupedUnhittable}（合计 ${accounted}）`,
      ).toBeGreaterThanOrEqual(totalDrivable)
      expect(
        row.controlsClicked,
        `${role}：一个控件都没驱动（reachable=${reachable.length}，其中有候选控件的页面 ${pagesWithCandidates} 个）`,
      ).toBeGreaterThanOrEqual(pagesWithCandidates)
      // ⑥ 「没有候选控件」的页面必须**被成功访问过**（否则那是探针失败，不是"这页本来就没控件"）。
      expect(
        clickedNotVisited,
        `${role}：这些页面**没被成功访问**、却被当成"没有候选控件"豁免了（探针失败被伪装成豁免）：` +
          clickedNotVisited.join('、'),
      ).toEqual([])

      /* ——— T1：层内驱动的三条判据（spec §4 的 L1 / L2，以及层内自己的"不许静默少点"） ——— */

      // ⑦ 层内**记账恒等式**（与页内 ⑤ 同一条纪律，只是换了作用域）：层内枚举出来的每一条可驱动
      //    候选都要有归属（被驱动 / 显式计入去重）。⛔ 这条不是 L1 / L2——它是"层内少点了没有"的
      //    账本，L2 那枚钉子只判"某个层一条都没点"，判不出"层内点了 3 条、另有 40 条没记账"。
      //    ⚠️ 自证不是空转：把 `LAYER_ROOT` 改成一个不存在的类 ⇒ `layerDrivableTotal` 塌到 0 ⇒
      //    下面 L1 那条按职位钉的下界当场红（见 T1 的"弄红"证明）。
      const layerAccounted =
        row.layerControlsClicked + row.layerDedupedDuplicates + row.layerDedupedUnhittable
      expect(
        layerAccounted,
        `${role}：层内枚举出来的可驱动候选（${row.layerDrivableTotal}）里，有 ` +
          `${row.layerDrivableTotal - layerAccounted} 条**既没被驱动、也没被记进去重账**` +
          `（层内枚举器 / 驱动循环静默少点）：\n` +
          `  层内点过=${row.layerControlsClicked} 层内去重重复=${row.layerDedupedDuplicates}` +
          ` 层内点不动的副本=${row.layerDedupedUnhittable}（合计 ${layerAccounted}）`,
      ).toBeGreaterThanOrEqual(row.layerDrivableTotal)

      // ⑦b 层内**候选的完整分账**（A）：逐层核对
      //     `candidateTotal === drivable + notDriven`（候选 = 可驱动 + 未驱动）。
      //     ⛔ 这条补的是 ⑦ 的**上游**：⑦ 只判"可驱动的那些有没有归属"，判不出
      //     「层里有候选**根本没进 `drivable`/`notDriven`**」——原来层内传 `recordNonBtnSkipped:false`
      //     且没人读 `snapshot.skipped`，那些"命中候选选择器但不带 `layui-btn`"的控件与层内路由链接
      //     就是从这里**静默消失**的。判据是**逐层**的（⛔ 不是职位级总数，那是恒真形状 `E-0024`）。
      const layerPartitionMismatch = row.layers
        .filter((l) => l.drivable + l.notDriven !== l.candidateTotal)
        .map(
          (l) =>
            `${l.page} ｜ ${l.surface}：层内候选 ${l.candidateTotal} 条 ≠ 可驱动 ${l.drivable} 条` +
            ` + 未驱动 ${l.notDriven} 条`,
        )
      expect(
        layerPartitionMismatch,
        `${role}：下列层里**候选没有被完整分账**（有候选既没进可驱动、也没进 notDriven）——` +
          `层内枚举器 / 记账静默少点：\n` +
          layerPartitionMismatch.map((s) => `  - ${s}`).join('\n'),
      ).toEqual([])
      expect(
        row.layers.reduce((n, l) => n + l.candidateTotal, 0),
        `${role}：层内候选总数（逐层 candidateTotal 之和）与层内可驱动数 layerDrivableTotal 对不上` +
          `（两处应来自同一次枚举）：层候选 ${row.layers.reduce((n, l) => n + l.candidateTotal, 0)} 条 /` +
          ` 层可驱动 ${row.layerDrivableTotal} 条`,
      ).toBeGreaterThanOrEqual(row.layerDrivableTotal)

      // ⑧ **L1**（spec §4 第一行）：层内驱动**确实发生过**。
      //    两半都要：① 整轮 `layerControlsClicked > 0`（这个职位的这一遍）；② 按职位钉住
      //    **层内枚举到的可驱动总数** ≥ 一个**外部常量**下界（出处与余量见 `LAYER_CONTROL_FLOOR`；
      //    写模式另有一套实测的 `LAYER_CONTROL_FLOOR_WRITE`，理由见那张表）。
      //    ⛔ 下界不许写成能从本次读数派生的量（那是自指、恒真，`E-0024`）。
      const layerFloorName = WRITE_MODE ? 'LAYER_CONTROL_FLOOR_WRITE' : 'LAYER_CONTROL_FLOOR'
      const layerFloor = (WRITE_MODE ? LAYER_CONTROL_FLOOR_WRITE : LAYER_CONTROL_FLOOR)[role]
      expect(
        layerFloor,
        `${role}：没有登记层内可驱动数下界（\`${layerFloorName}\` 里缺这个职位、或还没按实测回填）` +
          `⇒ L1 的下界这一半会静默放行`,
      ).toBeDefined()
      expect(
        typeof layerFloor,
        `${role}：\`${layerFloorName}\` 的值必须是数字`,
      ).toBe('number')
      // ⛔ `-1` 是**显式的空位**（这个职位的菜单里根本没有会开层的页面 ⇒ 本枚钉子对它**无可判别**，
      //    T1 实测值与理由都写在 `LAYER_CONTROL_FLOOR`（写模式 = `LAYER_CONTROL_FLOOR_WRITE`）的注释里）。⛔ 它不是"没登记"（缺键才是），
      //    也不许被当成"下界 0"——那会让"枚举器整体失效"在这个职位上照样绿。
      if (layerFloor === LAYER_CONTROL_FLOOR_VACUOUS) {
        expect(
          row.layerDrivableTotal,
          `${role}：下界登记为「本职位没有会开出可驱动控件的层」（-1），但这一遍**层内枚举到了 ` +
            `${row.layerDrivableTotal} 条可驱动控件**（开出的层：` +
            `${[...new Set(row.layers.map((l) => l.surface))].join('、') || '（无）'}）` +
            ` ⇒ 那个登记已经过期，请按实测重取 \`${layerFloorName}\``,
        ).toBe(0)
        // ⚠️ **F 的诚实边界**：上面那条只钉住**层内可驱动控件数**，
        //    ⛔ 不是"一个层都没开出来"。`taker` 今天就会开出 `layer:信息`（`layersOpened=1`）而照样通过。
        //    开出层、但层里没有 `layui-btn` 可驱动控件的形态（纯 `closeBtn:0` 层 / 只有非 `layui-btn`
        //    控件的层 / 纯 iframe 层）**本哨兵覆盖不到** ⇒ 这里**不判红**（本轮无法实测复核这两个职位，
        //    加一条未实测的红判据比 F 原来的风险更贵），只把三个数一起打出来提醒人工复核登记。
        //    这一条已如实登记进 spec §9 残差。
        if (row.layersOpened > 0 || row.layersSkippedIframe > 0) {
          console.log(
            `  ⚠️ ${role}：登记为「无可驱动层内控件」，但这一遍**开出了层**：` +
              `layersOpened=${row.layersOpened} / layersSkippedIframe=${row.layersSkippedIframe} / ` +
              `layerControlsClicked=${row.layerControlsClicked} / layersCoOpenedSkipped=${row.layersCoOpenedSkipped}` +
              ` ⇒ 若这些层里其实有控件（例如只有非 \`layui-btn\` 控件 / 纯 iframe 层），` +
              `请输入 spec §9 复核下界登记（⛔ 本哨兵只判可驱动数为 0，判不出这一形态）`,
          )
        }
      } else {
        expect(
          row.layerDrivableTotal,
          `${role}：整轮只枚举到 ${row.layerDrivableTotal} 条**层内**可驱动控件，低于覆盖下界 ${layerFloor} 条` +
            `（下界的出处与余量见 \`${layerFloorName}\`）——层内枚举器（\`LAYER_ROOT\` 选择器 /` +
            `开层时机 / 新层判定）静默失效了？`,
        ).toBeGreaterThanOrEqual(layerFloor)
      }
      // 只有**层里真的有控件**的职位才要求"真的点过层内控件"。
      // ⛔ 判据必须打在 `layerDrivableTotal`（**层内枚举到的控件数**）上，⛔ **不是** `layers.length`：
      //    提示层（`layer:信息` 这种）也算"打开过的层"（它有可点的关闭按钮），但它层里
      //    **一条控件都没有** ⇒ 拿 `layers.length` 判会把 `taker` 这种"只开出提示层"的职位
      //    判成"层内驱动静默退出"——**假红**（T1 收口轮实测踩到：`taker` 开出的层全是 `layer:信息`、
      //    层内 0 条控件，却被这条判据报了红。⚠️ I.2 订正：最终整轮实测
      //    `taker` 的 `layersOpened=1`（⛔ 不是原注释写的"4 个"，那是一个更早的中间读数）。
      if (row.layerDrivableTotal > 0) {
        expect(
          row.layerControlsClicked,
          `${role}：这一遍**层内枚举到 ${row.layerDrivableTotal} 条可驱动控件**，却一条都没驱动过` +
            `（层内驱动循环静默退出了？逐层读数见报告 \`layers\`）`,
        ).toBeGreaterThan(0)
      }

      // ⑨ **L2**（spec §4 第二行）：**某个层里枚举到了控件、却一条都没点过 ⇒ 红**。
      //    ⛔ 判据必须打在**逐层**的 (可驱动, 点过) 上（`row.layers` 就是为此存在的）——
      //    只判职位级总数（"总数不为零"）是恒真的形状（`E-0024`），层内半途静默停止时检不出来。
      const layersEnumeratedNotClicked = row.layers
        .filter((l) => l.drivable > 0 && l.clicked === 0)
        .map((l) => `${l.page} ｜ ${l.surface}（层内可驱动 ${l.drivable} 条、点过 0 条）`)
      expect(
        layersEnumeratedNotClicked,
        `${role}：下列层里**枚举到了控件、却一条都没点过**（层内驱动半途静默停止？）：\n` +
          layersEnumeratedNotClicked.map((s) => `  - ${s}`).join('\n'),
      ).toEqual([])
      if (row.layerControlsClicked > 0) {
        // 自证不是空转：真的点过层内控件时，`layers` 里必须至少有**一条** `clicked > 0` 的记录
        // （否则 L2 的分子恒为 0、那条判据永远绿）。
        expect(
          row.layers.filter((l) => l.clicked > 0).length,
          `${role}：\`layerControlsClicked=${row.layerControlsClicked}\` 但 \`layers\` 里没有任何` +
            `一条记录 \`clicked > 0\` ⇒ L2 的读数和它对不上（两处用的是同一批事件吗？）`,
        ).toBeGreaterThan(0)
      }
      // ⛔ 层内点不动 **不判红**（spec §3.3 对页内控件定的同一条口径：那是探针限制，不是权限缺陷）。
      //    这里只在有失败时打一行，读报告的人能在 L2 之前先看到"哪些层内控件没点动"。
      if (row.layerClickFailures > 0) {
        console.log(`  ⚠️ ${role}：层内点不动的控件 ${row.layerClickFailures} 条（⛔ 不判红，见 clickFailures）`)
      }

      /* ——— T4：基线比对（spec §4）——— */

      // 把这次跑出来的 403 发现与**按职位冻结的**基线做双向比对（实现与形状说明见夹具文件）。
      const baselineDiff = diffBaseline(
        role,
        row.findings.map((f) => ({ page: f.page, control: f.control, urls: f.urls, surface: f.surface })),
      )
      // 判据①（「多」）：跑出来的 403 **必须都在基线里**——出现基线之外的 ⇒ 红（新缺陷 / 回归）。
      //    ⛔ 文案要点名是哪一条（页面 / 控件 / 命中的 URL），否则读报告的人还得自己去翻。
      expect(
        baselineDiff.extra.map((x) => x.label),
        `${role}：出现**基线之外**的 403（新缺陷 / 回归）——要么修掉它，要么在 ` +
          `e2e/fixtures/role-audit-baseline.ts 里登记（**必须带 D-01xx 编号**）并写明理由：\n` +
          baselineDiff.extra.map((x) => `  - ${x.label}`).join('\n'),
      ).toEqual([])
      // 判据②（「少 / 陈旧」）：基线里每一条都**必须仍复现**——不再复现 ⇒ 也红（文案写「基线陈旧」）。
      //    ⛔ 空 `findings` + 非空基线**也走这一支** ⇒ 「跑出来是空的」不会被当成通过（spec §4）。
      //    这条判据的存在意义：**修好缺陷的人会被这张网逼着把基线删掉**，基线因此只会缩。
      expect(
        baselineDiff.stale.map((x) => `基线陈旧：${x.label}`),
        `${role}：基线陈旧：下列条目**不再复现**；若缺陷已修，请删掉 ` +
          `e2e/fixtures/role-audit-baseline.ts 里对应的条目（留着会让本网永远红）：\n` +
          baselineDiff.stale.map((x) => `  - 基线陈旧：${x.label}`).join('\n'),
      ).toEqual([])
      console.log(
        `  🧊 基线比对（${role}）：403 发现 ${row.findings.length} 条 / 基线之外 ${baselineDiff.extra.length} 条` +
          `（须 0） / 基线陈旧 ${baselineDiff.stale.length} 条（须 0）` +
          `（本职位基线条目 ${ROLE_AUDIT_BASELINE.filter((e) => e.role === role).length} 条）`,
      )

      // 读数（人跑一次就能看见，不必翻 JSON）：每页候选 / 点过 / 去重 / 403 条数。
      console.log(
        `  控件对拍 ${role}：页数=${pageResults.length}（成功访问 ${pageResults.filter((r) => r.visited).length}）` +
          ` 候选=${totalCandidate}（可驱动 ${totalDrivable}） 点过=${row.controlsClicked}` +
          ` 去重重复=${row.dedupedDuplicates} 点不动的副本=${row.dedupedUnhittable}` +
          ` 点不动=${clickFailures.length} 未驱动=${row.notDriven.length}` +
          ` 拦断写=${row.abortedWrites}` +
          ` 无控件页=${pagesWithNoControls.length}（其中窗口内没渲染=${pagesNeverRendered.length}）` +
          ` 403发现=${row.findings.length}` +
          ` 耗时=${((Date.now() - driveStart) / 1000).toFixed(1)}s`,
      )
      // `layersCoOpenedSkipped` 数的是同一次点击开出来的兄弟层，不是嵌套层。
      console.log(
        `  🪟 层内对拍 ${role}：layerControlsClicked=${row.layerControlsClicked}` +
          ` layersOpened=${row.layersOpened}（按标题去重） layersSkippedIframe=${row.layersSkippedIframe}` +
          `（身份见报告 skippedIframeLayers ${row.skippedIframeLayers.length} 条）` +
          ` layersCoOpenedSkipped=${row.layersCoOpenedSkipped} layerClickFailures=${row.layerClickFailures}` +
          ` layersDrivableTotal=${row.layerDrivableTotal}` +
          ` 层内未驱动=${row.layers.reduce((n, l) => n + l.notDriven, 0)}` +
          ` layerCloseFailures=${row.layerCloseFailures}` +
          `（逐层明细 ${row.layers.length} 条，见报告 layers）`,
      )
      if (row.deduped.length) {
        const byReason = new Map<string, number>()
        for (const it of row.deduped) byReason.set(it.reason, (byReason.get(it.reason) ?? 0) + 1)
        console.log(
          `  ℹ️ 去重未驱动的元素（已逐条记进 deduped，⛔ 不是静默少点）：` +
            [...byReason].map(([r, n]) => `${n} 条 × ${r}`).join(' ； '),
        )
      }
      if (row.notDriven.length) {
        const byReason = new Map<string, number>()
        for (const it of row.notDriven) byReason.set(it.reason, (byReason.get(it.reason) ?? 0) + 1)
        console.log(
          `  ℹ️ 未被驱动的候选（已逐条记进 notDriven，不是静默漏点）：` +
            [...byReason].map(([r, n]) => `${n} 条 × ${r}`).join(' ； '),
        )
      }
      // ⛔ **findings 非空不判红**（spec §4：有 403 就报红会让这条线从第一天就是红的、然后没人看；
      //    冻结与"多/少"的判定是 T4 的事）。这里只把发现逐条打到输出里。
      if (row.findings.length) {
        console.log(`  🚨 ${role} 的 403 发现（原样列出，不判定是否缺陷）：`)
        for (const f of row.findings) {
          console.log(
            `     - ${f.page} ｜ surface=${f.surface} ｜ ${f.control} ｜ ${f.urls.join(' , ')}` +
              (f.occurrences > 1 ? ` ｜（同一身份串的元素 ×${f.occurrences}）` : ''),
          )
          if (f.abortedWrites.length) {
            console.log(`       被拦断的写：${f.abortedWrites.join(' , ')}`)
          }
        }
      }
      if (clickFailures.length) {
        console.log(
          `  ⚠️ 点不动的控件（不是权限缺陷，已记入 clickFailures）：\n` +
            clickFailures.map((f) => `     - ${f.page} ｜ ${f.control} ｜ ${f.reason}`).join('\n'),
        )
      }
    } finally {
      // ⛔ 关掉写拦断：下一条用例（下一个职位）要先**登录**，登录 POST 绝不能被拦（见开关的注释）。
      drivingWritesAborted = false
      // ⛔ 在 `finally` 里**落盘**（不是 push 进模块级数组）：worker 因失败重启时，模块级数组会清零，
      //    而这个职位恰恰可能是「第一个失败」⇒ 它的行会从报告里消失。
      mkdirSync(ROWS_DIR, { recursive: true })
      writeFileSync(rowFileOf(role), JSON.stringify(row, null, 2))
    }
  })
}

/** 反查：path → routeName（只给报错文案指认标签用；查不到返回 `null`，不抛）。 */
function pathToRouteName(path: string): string | null {
  for (const [name, p] of ROUTE_NAME_TO_PATH) {
    if (p === path) return name
  }
  return null
}

/**
 * 结构兜底：本文件的标签映射**自己也要能变红**——如果 `menu.ts` 加了**带 routeName 的可见条目**
 * 而没进映射，上面每个职位的 `pathsOfItems()` 会响亮失败（找不到 routeName），
 * 这条则在**收集期**（不跑浏览器）点名是哪一条，省一轮排查。
 *
 * ⚠️ 判据是「映射是 `menu.ts` 可见条目的**超集**」，不是「恰好相等」：置灰条目
 * （`routeName === undefined`）本来就不该进映射（它们两侧都不计，见 `pathsOfItems()`）。
 */
test('职位对拍结构兜底：菜单标签映射覆盖 `menu.ts` 的全部可见条目（这条自己也要能变红）', () => {
  const declared = new Set<string>()
  for (const item of TOP_LEVEL_ITEMS) {
    if (item.hidden || item.routeName === undefined) continue
    declared.add(labelKey(null, item.label))
  }
  for (const group of MENU_GROUPS) {
    if (group.hidden) continue
    for (const item of group.items) {
      if (item.hidden || item.routeName === undefined) continue
      declared.add(labelKey(group.name, item.label))
    }
  }
  const missing = [...declared].filter((k) => !MENU_LABEL_TO_ROUTE.has(k))
  expect(
    missing,
    `menu.ts 的可见条目没进标签映射（${missing.length} 条）：` +
      missing.map((k) => k.replace('\u0000', ' / ').replace(/^\s*\/\s*/, '')).join('、'),
  ).toEqual([])
  // 顶层条目与分组内条目**都**必须收到（任务书要求），别只收其中一类。
  expect([...declared].some((k) => k.startsWith('\u0000')), '一条顶层条目都没收进来').toBe(true)
  expect([...declared].some((k) => !k.startsWith('\u0000')), '一条分组内条目都没收进来').toBe(true)
  // 反向：映射里不许有 `menu.ts` 没有的键（有的话，`pathsOfItems()` 会拿一个永远不该出现的标签去放行）。
  expect(
    [...MENU_LABEL_TO_ROUTE.keys()].filter((k) => !declared.has(k)),
    '标签映射里有 menu.ts 里不存在的可见条目',
  ).toEqual([])
})

/**
 * 结构兜底（T2）：**写租户常量跨来源对拍**——JS 侧（`e2e/config.mjs`）与 Python 侧
 * （`apps/platform/management/commands/reset_e2e_write.py`）各有一份，**必须逐字相同**。
 *
 * ⛔ 为什么要有这一条（T1 明确留给 T2 的风险）：两份常量是**独立来源**，而漂移的后果是
 * **连错库**（探针按 JS 侧的域名 / 凭据跑，重置命令按 Python 侧的常量 DROP 与建库）——
 * 那是一种不会报错、只会静默打到别处的错。判据必须是**会失败的**（⛔ 只写注释说"两边要一致"不算）。
 * ⚠️ `config.mjs` 在**加载期**也跑同一份实现（`assertWriteTenantConstantsMatchPython()`）
 * ⇒ 真漂移时本文件在**收集期**就加载不起来、整个 spec 红；这里再点名一遍，并把**外部契约值**
 * （spec §2 的表）钉死——否则"两侧相等"若两侧被一起改坏就检不出来（`E-0024` 同族）。
 */
test('职位对拍结构兜底：写租户常量与 `reset_e2e_write.py` 逐字一致（跨来源对拍；这条自己也要能变红）', () => {
  const py = readPythonWriteTenantConstants()
  // ① **跨来源**：Python 侧 == JS 侧（任一侧单独改坏 ⇒ 红）。
  expect(py.schema, 'Python 侧 schema 与 JS 侧不一致').toBe(WRITE_TENANT_SCHEMA)
  expect(py.database, 'Python 侧库名与 JS 侧不一致').toBe(WRITE_TENANT_DB)
  expect(py.domain, 'Python 侧域名与 JS 侧不一致').toBe(WRITE_TENANT_DOMAIN)
  expect(py.envFile, 'Python 侧凭据落点与 JS 侧不一致').toBe(WRITE_TENANT_ENV_REL)
  // ② **外部契约值**（spec §2 的表逐字；⛔ 不是从上面任一侧派生的量）。
  expect(py.schema).toBe('e2e_write')
  expect(py.database).toBe('tt_e2e_write')
  expect(py.domain).toBe('e2e-write.test.local')
  expect(py.envFile).toBe('.tools/e2e/tenant-write.env')
  // ③ 判据本体（与加载期同一份实现）：一致时**不许抛**。⚠️ 它抛的时候，本文件根本加载不到这里
  //    ——那正是"响亮失败"；这条 `not.toThrow()` 是给"有人把它改成只警告"那种改动守门的。
  expect(() => assertWriteTenantConstantsMatchPython()).not.toThrow()
})

/**
 * 结构兜底（T3）：**候选控件选择器全表**必须与 spec §3.2 的原文**逐字一致**。
 *
 * ⛔ 为什么要有这一条：`CONTROL_SELECTORS` 是控件枚举的**唯一出处**，而"唯一出处"这件事
 * 只有靠一条**外部判据**才能守住——否则谁把 `[lay-event]` 删掉，枚举器只会**静默少点**
 * 若干控件（一个页面少点几条，报告上看不出来）。
 * ⚠️ 这份期望值是 spec §3.2 那句原文的**逐字拷贝**（五条），⛔ 不是"从 `CONTROL_SELECTORS` 自己算出来"的
 * （那样就是自指断言、恒真，`E-0024`）。
 */
test('职位对拍结构兜底：控件候选选择器全表与 spec §3.2 逐字一致（这条自己也要能变红）', () => {
  const SPEC_3_2 = ['a.layui-btn', 'button.layui-btn', '.layui-btn', '[lay-event]', '[data-act]']
  expect([...CONTROL_SELECTORS], 'spec §3.2 的候选选择器全表变了（多一条 / 少一条 / 改了写法）').toEqual(
    SPEC_3_2,
  )
  // ⛔ 「作用域」也是判据的一部分：控件都长在 island iframe 的 `#app` 里（spec §3.2 / §2 的 C 条）。
  expect(CONTROL_SCOPE, '控件枚举的作用域不是 island 的挂载点').toBe('#app')
})

/**
 * 结构兜底（T1）：**层相关选择器的取值**必须与 layui 渲染出来的类名**逐字一致**。
 *
 * ⛔ 为什么要有这一条：这五条常量是「层内控件枚举」这件事的**唯一出处**（`selectors.ts`），
 * 而它们写错时的症状与"这一页没有层"**一模一样**——层内一块控件都扫不到、报告上只是
 * `layerDrivableTotal=0`（一个**静默**失效）。收集期这条把值钉死，省一轮真浏览器排查。
 * ⚠️ 期望值是**从 layui 源码读出来的类名**（`src/frontend/public/layui/layui.js` 的 `vessel()`：
 * `u = ["layui-layer", ".layui-layer-title", …, "layui-layer-iframe", "layui-layer-content",
 * …, "layui-layer-close"]` —— 注意 `u[1]` / `u[5]` / `u[7]` 在源码里是**带点的选择器**，
 * 而类名那几项不带点），⛔ 不是"从这些常量自己算出来"的（那是自指、恒真，`E-0024`）。
 */
test('职位对拍结构兜底：层相关选择器的取值与 layui 源码逐字一致（这条自己也要能变红）', () => {
  expect(LAYER_ROOT, '层根的选择器不是 layui 的 `.layui-layer`').toBe('.layui-layer')
  expect(LAYER_TITLE, '层标题的选择器不是 layui 的 `.layui-layer-title`').toBe('.layui-layer-title')
  expect(LAYER_CLOSE, '层关闭按钮的选择器不是 layui 的 `.layui-layer-close`').toBe('.layui-layer-close')
  expect(LAYER_IFRAME_MARK, '`type:2` 判据的类名不是 layui 的 `.layui-layer-iframe`').toBe(
    '.layui-layer-iframe',
  )
  expect(LAYER_CONTENT, '层内容区的选择器不是 layui 的 `.layui-layer-content`').toBe(
    '.layui-layer-content',
  )
  // iframe 探测必须真的**收在内容区里**（⛔ 不是 `iframe` 裸选择器：页面自己的 iframe 会是噪声）。
  expect(LAYER_CONTENT_IFRAME, '层内容里的 iframe 探测没有收在 `.layui-layer-content` 里').toBe(
    '.layui-layer-content iframe',
  )
  // `surface` 的形状（spec §2）：`layer:<标题>`；取不到标题时退成 `layer:untitled`。
  expect(LAYER_SURFACE_PREFIX, '`surface` 的前缀不是 `layer:`').toBe('layer:')
  expect(LAYER_SURFACE_UNTITLED, '没有标题时的 `surface` 不是 `layer:untitled`').toBe('layer:untitled')
})

/**
 * 结构兜底（T3）：**控件身份串的形状**必须真的是 spec §3.2 那一串，⛔ 不是一个"看起来像"的串。
 *
 * ⛔ 手写的假元素（不依赖浏览器）——**这是有意的**：这条判据要能在**收集期**（不跑 E2E）
 * 就把形状钉住。真正在 DOM 上跑的那份由 `enumerateCandidateControls()` 的逐元素对拍守（它拿的是真元素）。
 * ⚠️ 三条要点各有一个例子：`data-act` 优先于 `lay-event`、class 只取**前 2 个**、
 * 文本取**前 12 字且去掉空白**。
 */
test('职位对拍结构兜底：控件身份串的形状（spec §3.2；这条自己也要能变红）', () => {
  const make = (attrs: Record<string, string>, tag = 'A', text = ''): Element => {
    const el = {
      tagName: tag,
      getAttribute: (name: string) => attrs[name] ?? null,
      textContent: text,
    }
    return el as unknown as Element
  }
  expect(
    controlIdentity(
      make({ class: 'layui-btn layui-btn-xs layui-btn-normal jsr', 'data-act': 'go', 'lay-event': 'x' }, 'A', ' 指派 打手 '),
    ),
    '身份串没按 `<tag>.<class 前 2 个>#<data-act|lay-event>#<文本前 12 字>` 成形',
  ).toBe('a.layui-btn.layui-btn-xs#go#指派打手')
  // `data-act` 缺位时退到 `lay-event`；两个都没有就是空段。
  // ⚠️ 期望值是 `<tag>.<cls>#<act>#<text>`——**恰好**两个 `#`（一个是 `<cls>` 与 `<act>` 的分隔、
  //    一个是 `<act>` 与 `<text>` 的分隔）。原来这里多打了一个 `#`（`a.x##edit#`）⇒ 这条用例
  //    **一直红**，而它当时的标题不含「职位」⇒ 跑本线的 `--grep "职位"` **从不执行它**：
  //    「常红又从不跑」的钉子 = 一个**看起来存在、实际从未生效**的保证（`E-0085`）。
  expect(controlIdentity(make({ class: 'x', 'lay-event': 'edit' }))).toBe('a.x#edit#')
  expect(controlIdentity(make({ class: 'x' }))).toBe('a.x##')
  // class 只有一个 / 没有 class 时不能崩，也不能多出点号。
  expect(controlIdentity(make({ class: 'solo' }))).toBe('a.solo##')
  expect(controlIdentity(make({}))).toBe('a.##')
  // 文本超过 12 字要截断（否则每跑一次都可能变、基线冻不住）。
  expect(controlIdentity(make({ class: 'x' }, 'BUTTON', '一二三四五六七八九十十一十二十三'))).toBe(
    'button.x##一二三四五六七八九十十一',
  )
})

/* ——— T4：基线自己的三条判据（都在**收集期**跑，不依赖浏览器）——— */

/**
 * 结构兜底（A）：**层内候选的「没驱动」记账**必须真的覆盖两类候选
 * （路由链接 + 命中候选选择器却不带 `layui-btn` 的）。
 *
 * ⛔ 为什么要有这一条：层内原来传 `recordNonBtnSkipped:false`，而且**没人读**
 * `snapshot.skipped` / `snapshot.routeLinks` ⇒ 层里那些候选**既不驱动也不逐条记账**
 * ——母规范 §3.2 明令禁止的「静默少点」。现在页内与层内共用 `notDrivenOf()`，
 * 这条判据拿**合成快照**（不跑浏览器）钉住：谁把 `skipped` 那一类丢掉，当场红。
 * ⚠️ 快照是**合成**的（⛔ 不是从本次真实读数派生——那样会自指、恒真，`E-0024`）。
 */
test('职位对拍结构兜底：候选「没驱动」记账覆盖路由链接与非按钮候选（这条自己也要能变红）', () => {
  const snapshot: ControlSnapshot = {
    drivable: [],
    routeLinks: ['a.layui-btn.layui-btn-xs##去订单页'],
    // 候选总数 = 本快照里真正给出的候选条数（1 条路由链接 + 1 条非按钮候选 + 0 条可驱动）。
    // ⚠️ 它是**外部给定的期望值**，⛔ 不许写成 `routeLinks.length + skipped.length`（那就自指、恒真，`E-0024`）。
    candidateTotal: 2,
    skipped: [{ identity: 'div.layui-form-checkbox##', reason: '候选选择器命中、但不是 `layui-btn`' }],
  }
  const accounted = notDrivenOf(snapshot)
  expect(
    accounted.map((it) => it.identity),
    '`notDrivenOf()` 漏掉了一类候选（层内会因此静默少点：既不驱动、也不逐条记账）',
  ).toEqual(['a.layui-btn.layui-btn-xs##去订单页', 'div.layui-form-checkbox##'])
  // 路由链接那类必须仍带统一理由（与页内同一个 `ROUTE_LINK_REASON`，两处不许各写一句）。
  expect(accounted[0].reason).toBe(ROUTE_LINK_REASON)
  // 自证不是空转：候选总数 = 可驱动 + 记账条数（本快照可驱动 0 ⇒ 记账条数必须等于候选总数）。
  expect(
    snapshot.drivable.length + accounted.length,
    '候选没有被完整记账（drivable + routeLinks + skipped ≠ candidateTotal）',
  ).toBe(snapshot.candidateTotal)
})

/**
 * 判据①（spec §4）：**没有 `D-01xx` 编号的条目进不了基线**。
 *
 * ⛔ 为什么必须有一条"用例自己也能红"的证明：只断言"真基线合规"的自检，在**校验器坏成恒真**
 * （永远返回空错误表）时照样绿——那是 `E-0024` 的自指形状。所以下面拿**合成坏条目**逐个证明
 * 校验器真的会拒；坏条目的每一种都是"忘写编号 / 写歪编号"的真实形态。
 */
test('职位对拍结构兜底：基线每条都必须带 D-01xx 编号（这条自己也要能变红）', () => {
  const errors = validateBaseline(ROLE_AUDIT_BASELINE)
  expect(
    errors,
    `基线里有 ${errors.length} 处不合规（每条都要有 role/page/control/urlIncludes/note 与 D-01xx 编号）：\n` +
      errors.map((e) => `  #${e.index} ${String(e.field)}：${e.message}`).join('\n'),
  ).toEqual([])

  // ⚠️ 对照组用**合成条目**，⛔ 不再取 `ROLE_AUDIT_BASELINE[0]`：真基线已**清空**
  //    （2026-09-24，见 `role-audit-baseline.ts` 开头的清空段）⇒ `[0]` 是 `undefined`，
  //    拿它去喂 `validateBaseline()` 会抛 `TypeError` ⇒ 这条用例变成 **ERROR**（不是干净的 FAIL），
  //    而"校验器会拒坏条目"这个判据本身反而没被执行到。合成条目同时承担
  //    「校验器不能把好条目也拒掉」那半条反向钉子（见下方最后一行）。
  const good: RoleAuditBaselineEntry = {
    role: 'cs',
    page: '/x',
    control: 'a.b##go#保存',
    urlIncludes: '/api/x/',
    decision: 'D-9999',
    note: '合成条目（只在收集期用，不进真基线）',
  }
  const badCases: { what: string; entry: RoleAuditBaselineEntry }[] = [
    { what: 'decision 是空串（忘了写编号）', entry: { ...good, decision: '' } },
    { what: 'decision 少一位（D-145）', entry: { ...good, decision: 'D-145' } },
    { what: 'decision 带后缀（D-0145（待定））', entry: { ...good, decision: 'D-0145（待定）' } },
    { what: 'decision 整个缺字段', entry: { ...good, decision: undefined as unknown as string } },
    { what: 'control 是空串', entry: { ...good, control: '' } },
    { what: 'note 是空串（没有一行说明）', entry: { ...good, note: '' } },
    { what: 'page 不是路径（orders）', entry: { ...good, page: 'orders' } },
    { what: 'urlIncludes 是空串', entry: { ...good, urlIncludes: '' } },
    { what: 'role 是空串', entry: { ...good, role: '' as RoleName } },
  ]
  for (const c of badCases) {
    expect(
      validateBaseline([c.entry]),
      `校验器放过了坏条目「${c.what}」⇒ 它是恒真的，等于**没有**这条判据`,
    ).not.toEqual([])
  }
  // 反向也要钉住：校验器不能坏到把好条目也拒掉（否则上面那条"真基线合规"就永远红、被人调松）。
  expect(validateBaseline([good]), '校验器把一条合规的基线也拒了').toEqual([])
})

/**
 * 判据②（spec §4）：**双向比对的两个方向都能红**——「多」⇒ `extra`，「少 / 陈旧」⇒ `stale`。
 *
 * ⛔ 用**合成基线**做这条自检（不碰真基线）：真基线今天**为空**（2026-09-24 清空，见
 * `role-audit-baseline.ts`），拿它做正反例连一条"会命中"的例子都拿不出来、又会把"真基线的形状"
 * 耦进这条结构判据里。
 * ⚠️ 特别钉住 `findings` 为空的情形——那是 spec §4 点名不许被当成通过的那一支。
 * ⚠️ 也钉住**收口轮收紧的那一支**：**同页同控件、多出一条没被覆盖的 URL** 必须判 `extra`
 * （旧实现 `urls.some(...)` 会把它静默吸收；`D-0146` 那种"换端点"的真实形态正是这样）。
 */
test('职位对拍结构兜底：基线双向比对的两个方向都能红（这条自己也要能变红）', () => {
  const synthetic: RoleAuditBaselineEntry[] = [
    {
      role: 'cs',
      page: '/x',
      control: 'a.b##go#保存',
      urlIncludes: '/api/x/',
      decision: 'D-9999',
      note: '合成条目（只在收集期用，不进真基线）',
    },
  ]
  const hit = { page: '/x', control: 'a.b##go#保存', urls: ['403 GET http://h/api/x/?a=1'] }
  // 等 ⇒ 两个方向都空（正向：合规比对不会假红）。
  expect(diffBaseline('cs', [hit], synthetic)).toEqual({ extra: [], stale: [] })
  // 「多」：多出一条基线之外的 403 ⇒ `extra` 有它。
  expect(
    diffBaseline('cs', [hit, { page: '/y', control: 'c##go#别的', urls: ['403 GET http://h/api/y/'] }], synthetic)
      .extra,
  ).toHaveLength(1)
  // 「少 / 陈旧」：⚠️ 连"一条 403 都没有"也必须走这一支（⛔ 空结果不许被当成通过）。
  expect(diffBaseline('cs', [], synthetic).stale, 'findings 为空时没有报「基线陈旧」').toHaveLength(1)
  // URL 片段对不上 ⇒ 也算不复现（别拿"页面 + 控件对了"就当命中）。
  expect(diffBaseline('cs', [{ ...hit, urls: ['403 GET http://h/api/other/'] }], synthetic).extra).toHaveLength(1)
  // ⛔ **收口轮收紧的那一支**：**同页同控件、多出一条没被任何基线条目覆盖的 URL** ⇒ 必须判 `extra`。
  //    旧实现（`urls.some(...)`）会因为发现里**另有一条** URL 命中 `/api/x/` 就把整条发现吞掉
  //    ⇒ `extra` 与 `stale` **都为空、整轮全绿**（这正是评审点名的静默形态）。真实形态：
  //    `D-0146` 把名单端点在**同一个控件**上从 `/api/accounts/` 换成 `assignee-options/`——
  //    控件没变、403 换了端点。这条合成用例是它**唯一**的判据（⛔ 不需要跑真浏览器）。
  const extraOnSameControl = diffBaseline(
    'cs',
    [{ ...hit, urls: [...hit.urls, '403 GET http://h/api/z/'] }],
    synthetic,
  )
  expect(
    extraOnSameControl.extra,
    '同一个控件上多出一条**没被任何基线条目覆盖**的 403 URL 被静默吸收了（收口轮要堵的就是这个洞）',
  ).toHaveLength(1)
  // 文案要点名**是哪条 URL** 越界（⛔ 不是把整条发现的所有 URL 铺出来、让读的人自己找）。
  expect(extraOnSameControl.extra[0].uncoveredUrls).toEqual(['403 GET http://h/api/z/'])
  expect(extraOnSameControl.extra[0].label).toContain('/api/z/')
  // 另一根轴（`stale`）**不跟着一起红**：被覆盖的那条 URL 仍复现 ⇒ 基线条目不算陈旧。
  // 这两根轴刻意分开判——一条发现里**既有**已知 403、**又有**新 403 时，只该红「新 403」那一侧。
  expect(extraOnSameControl.stale, '多出一条越界 URL 时，原本仍复现的基线条目不该被判「陈旧」').toHaveLength(0)
  // 基线写**稳定前缀**时要能命中实际的长串。
  expect(
    diffBaseline('cs', [{ ...hit, control: 'a.b##go#保存并关闭' }], [{ ...synthetic[0], control: 'a.b##go' }]).extra,
  ).toHaveLength(0)
  // 比对是**按职位**的：别的职位的 findings 不该把 cs 的基线条目"喂绿"。
  const otherRole = diffBaseline('supervisor', [hit], synthetic)
  expect(otherRole.extra).toHaveLength(1)
  expect(otherRole.stale).toHaveLength(0)

  /* ——— B：`surface` 也是匹配键的一根轴（层内发现不许被页内条目吸收） ——— */
  // ⛔ 这条是**合成用例**（⛔ 不跑浏览器）：同页、同控件身份串，只有 `surface` 不同。
  //    修之前 `entriesFor()` / `stale` 只按 (page, control, urlIncludes) 匹配 ⇒ 这条
  //    `surface='layer:x'` 的发现会被**页内**基线条目（`synthetic`，没写 `surface`）认领
  //    ⇒ `extra` 为空、整轮**静默全绿**。而本线的目的恰恰是新增层内发现。
  const layerHit = { ...hit, surface: 'layer:x' }
  const absorbed = diffBaseline('cs', [layerHit], synthetic)
  expect(
    absorbed.extra.map((x) => x.label),
    '层内发现被**页内**基线条目吸收了（`surface` 没进匹配键）——这正是修复波 B 要堵的静默形态',
  ).toHaveLength(1)
  // 同一根轴的另一侧：页内条目此刻确实**不再复现**（layer 那条不是它认领的）⇒ 应当判「陈旧」。
  expect(
    absorbed.stale,
    '层内发现不匹配页内条目时，页内条目应当被判「陈旧」（两处判据必须同一把尺）',
  ).toHaveLength(1)
  // 反向：基线条目**显式写同一个 `surface`** 时能命中（层内条目只认层内发现）。
  const layerBaseline = [{ ...synthetic[0], surface: 'layer:x' }]
  expect(diffBaseline('cs', [layerHit], layerBaseline), '显式写了同一个 surface 的基线应当命中').toEqual({
    extra: [],
    stale: [],
  })
  // ⛔ 反方向也钉住：**页内**发现不许被**层内**条目认领（否则两根轴只对一个方向生效）。
  const pageVsLayerEntry = diffBaseline('cs', [hit], layerBaseline)
  expect(pageVsLayerEntry.extra, '页内发现被层内基线条目吸收了（surface 只对了一个方向）').toHaveLength(1)
  expect(pageVsLayerEntry.stale, '页内发现不匹配层内条目时，层内条目应当被判「陈旧」').toHaveLength(1)
  // 缺省语义：基线条目**不写 `surface`** = 页内 ⇒ 页内发现（也不写 surface）照旧命中。
  expect(diffBaseline('cs', [hit], synthetic)).toEqual({ extra: [], stale: [] })
})

/** 判据③（spec §4）：基线里的职位必须都在被测矩阵里——否则那条基线永远不会被比对、成了死条目。 */
test('结构兜底：基线里的职位都在被测矩阵里（这条自己也要能变红）', () => {
  const roles = new Set<string>(ROLE_MATRIX.map((r) => r.role))
  expect(
    ROLE_AUDIT_BASELINE.filter((e) => !roles.has(e.role)).map((e) => `${e.role} ｜ ${e.page} ｜ ${e.decision}`),
    '基线里有**不在被测矩阵**里的职位 ⇒ 那些条目永远不会被比对（死条目）',
  ).toEqual([])
  // 自证不是空转：矩阵本身不许是空的（空矩阵会让上面那条恒真）。
  expect(roles.size).toBeGreaterThan(1)
})

/**
 * 结构兜底（**评审修复波 F4**）：**种子里该被审计的角色/账号** 与 **`ROLE_MATRIX`** 对拍
 * （**跨来源**，⛔ 不是拿矩阵派生矩阵）。
 *
 * ## 它补的是什么洞
 * M1 那条钉子（报告行数 = 矩阵行数）是**自指**的：把某个角色整行从 `ROLE_MATRIX` 删掉，
 * 运行器就**不再跑它**、报告行数跟着少一行 ⇒ M1 **照样绿**，而覆盖率**静默缩水**
 * （评审原话：「M1 那条钉子在'把 `minrole` 从 `ROLE_MATRIX` 删掉'时照样绿」）。
 *
 * ## 两侧为什么不同源
 *  - **左（权威）**：`seed_e2e.py` 里 `upsert("<账号>", <组常量>, …)` 那一组**带职位组**的账号
 *    ——它们才是「种子里该被审计的角色/账号」。解析的是 **Python 源文件的字面文本**，
 *    ⛔ 与 `ROLE_MATRIX` 没有派生关系。
 *    ⚠️ **不用 `PASSWORDS`**：它是口令表，`nobody001` 也在里面（按设计**不进**矩阵）⇒
 *    拿它当「该被审计的账号」会多出一个人、把 `nobody` 误当成漏测。
 *  - **右**：`ROLE_MATRIX` 的 `role` 经 `ACCOUNTS` 映射成登录账号。
 *  ⇒ **删掉矩阵里那一行，右侧集合少一个，本用例当场红**（弄红证明见 spec §8.11）。
 *
 * ⚠️ 判据取**双向集合相等**（与 `01-auth-guard` 的口令对拍同一把尺）：
 * ①种子里带组的账号必须都在矩阵里（防静默缩水）；②矩阵里的角色必须都是种子里带组的账号
 * （防矩阵里挂一个种子里不存在的账号 ⇒ 那一行登录必失败）。两侧都**不含** `nobody001`。
 */
test('职位对拍结构兜底：种子里带职位组的账号与 ROLE_MATRIX 双向对拍（这条自己也要能变红）', () => {
  // ⚠️ 路径写法照 `01-auth-guard.spec.ts` 的口令对拍（`FRONTEND_DIR` = `<仓库根>/src/frontend`，
  //    两级 `..` 回到 `<仓库根>`）。
  const pyPath = resolve(
    FRONTEND_DIR,
    '../../src/apps/platform/management/commands/seed_e2e.py',
  )
  const src = readFileSync(pyPath, 'utf8')
  // 来源①：带第二个位置参数（职位组常量）的 `upsert` 调用。`upsert("nobody001")` 只有一个参数
  // ⇒ 天然被排除（那正是「按设计不进矩阵」的那一个）。
  const seededBusinessAccounts = [
    ...src.matchAll(/^\s*upsert\("([a-z0-9_]+)"\s*,\s*[A-Z_]+/gm),
  ].map((m) => m[1]!)
  // 自证不是空转：解析失败（`upsert` 形状变了 / 缩进变了）时，下面那条集合相等会退化成
  // 「空 = 空」而恒真 ⇒ 先把「解析出了人」单钉一条。
  expect(
    seededBusinessAccounts.length,
    '没能从 seed_e2e.py 解析出「带职位组的账号」（该文件形状变了，请同步本用例）',
  ).toBeGreaterThan(1)
  // 来源②：矩阵 × 夹具账号表。
  const matrixAccounts = ROLE_MATRIX.map((r) => ACCOUNTS[r.role].username)
  const sorted = (xs: readonly string[]): string[] => [...xs].sort()
  expect(
    sorted(matrixAccounts),
    'ROLE_MATRIX 与 seed_e2e.py 里「带职位组的账号」对不上：\n' +
      '  · 矩阵里少一个 ⇒ 那个角色**不会被跑**（覆盖率静默缩水，M1 抓不到）；\n' +
      '  · 矩阵里多一个 ⇒ 那个账号在种子里不存在（登录必失败）。\n' +
      `  种子里带组的账号：${sorted(seededBusinessAccounts).join(' / ')}\n` +
      `  矩阵映射出的账号：${sorted(matrixAccounts).join(' / ')}`,
  ).toEqual(sorted(seededBusinessAccounts))
})

/* ——— 本线（detail-pages-audit）的收集期结构钉子 ——— */

/**
 * 结构兜底（本线 T1）：**参数化详情页登记表的形状与下界登记**必须自洽（收集期跑，不跑浏览器）。
 *
 * ## ⚠️ 它**进** `--grep "职位"` 的执行面（写下来，免得下一个人照抄一个没核过的推断）
 * 本文件在 `testDir` 下**没有** `describe` 包裹 ⇒ Playwright 的 `--grep` 匹配的**完整标题**
 * 就是用例标题本身。本条标题以「职位对拍结构兜底：」开头 ⇒ **命中**，与其余 8 条结构钉子一样。
 * ✅ 实测佐证（T1 整轮，逐条枚举命中标题）：`--grep "职位"` 命中 **5 个职位 + 9 条结构钉子 = 14 条**，
 *  其中第 9 条就是本条；基线 `fa56a96e` 上是 **8** 条 ⇒ 那一轮是 **13 条**。
 * ⛔ 所以"给一条钉子起个不含「职位」的标题就把它挪出这条命令"这个想法**不成立**
 *  （`E-0085` 的同族：判据要能被**运行到**，而这件事必须**核过**再说）。
 *  ⚠️ 本条仍是纯收集期判据（不跑浏览器、毫秒级）⇒ 加进这条命令**几乎不花时间**。
 *
 * ⛔ 为什么必须有这一条：本线新增的三个判据（实例化 / **D1** / D2）全都读
 * `PARAMETERIZED_ROUTES` 与 `INSTANTIATED_CANDIDATE_FLOOR`——两者写歪一个，症状是
 * **运行时静默少扫几条**（而不是红）。这里把下面四条在**收集期**钉死：
 *  1. 登记表**显式枚举**、且每条都有完整的五件套（`pattern` / `routeName` / `detailOf` / `resolve` / `shape`）；
 *  2. 每条 `detailOf` 必须**是候选页面清单里的一条**（`CANDIDATES`）——否则"父页可达"这件事
 *     在导航层无从成立，D1 那半（父页可达才取实例）**永远不会被触发**；
 *     ⚠️ 这也是"**跨源**判据"的承重那一条（加载期那条只查 `detailOf` 以 `/` 开头，是**自指**的
 *     防御性写法，见 `assertParameterizedPatternsMatchRouter()` 的注释）；
 *  3. 每条 `pattern` 都必须能**从 `router/index.ts` 的参数化 path 实例化出来**（加载期自检已对拍
 *     集合相等，这里再钉一条**能拼出具体路径**的判据：形状判据至少接受一个合成见证值、
 *     占位符数 = 路由参数数、且替换后是一个真路径）；
 *  4. 每个被测职位都**必须有** `INSTANTIATED_CANDIDATE_FLOOR` 的登记（缺键 ⇒ D2 那半静默放行）。
 * ⚠️ 期望值（`ROLE_MATRIX` / `CANDIDATES`）都是**外部给定的**，⛔ 不是从被断言的那个数组自己算出来的
 * （那样是自指、恒真，`E-0024`）。
 */
test('职位对拍结构兜底：参数化详情页登记表与下界登记自洽（这条自己也要能变红）', () => {
  // ① 显式枚举 + 五件套齐全。
  expect(PARAMETERIZED_ROUTES.length, '参数化详情页登记表不该是空的（否则本线等于没做）').toBeGreaterThan(0)
  const incomplete = PARAMETERIZED_ROUTES.filter(
    (r) =>
      typeof r.pattern !== 'string' ||
      r.pattern === '' ||
      typeof r.routeName !== 'string' ||
      r.routeName === '' ||
      typeof r.detailOf !== 'string' ||
      r.detailOf === '' ||
      typeof r.resolve !== 'function' ||
      typeof r.shape?.test !== 'function',
  ).map((r) => r.pattern)
  expect(
    incomplete,
    '下列登记缺了 spec §4 要求的五件套之一（pattern / routeName / detailOf / resolve / shape）：' +
      incomplete.join('、'),
  ).toEqual([])

  // ② 每条 `detailOf` 都必须是候选页面清单里的一条（否则"父页可达"无从成立）。
  const detailOfNotInCandidates = PARAMETERIZED_ROUTES.filter((r) => !CANDIDATES.includes(r.detailOf)).map(
    (r) => `${r.pattern} 的 detailOf=${r.detailOf}`,
  )
  expect(
    detailOfNotInCandidates,
    '登记里的 detailOf 不在候选页面清单（CANDIDATES）里 ⇒ 它永远不会出现在任何职位的 reachable 里：' +
      detailOfNotInCandidates.join('、'),
  ).toEqual([])

  // ③ 每条 `pattern` 都要能从 `router/index.ts` 的参数化 path **实例化出具体路径**。
  // ⚠️ **订正（评审修复波第 10③ 条）**：原来这里一律用合成值 `'1'` 替换占位符，而对
  // `/orders/{order_no}` 而言 `'1'` **根本不过** `^\\d{20}$` 那条形状约束 ⇒ 原来的注释
  // "形状不必对"是**对的**，但它没把话说全：这条判据只钉「**占位符拼得出来 + 结果以 `/` 开头**」，
  // ⛔ **不钉形状**（形状由各条 `resolve()` 的运行期校验守）。
  // 现在改成**两件事都钉**：① 找得到一个**过 `r.shape` 的合成见证值**（形状判据至少不能是"谁都不接受"）
  // ⇒ 用 `'1'.repeat(20)` 这类**满足形状**的见证值去替换；② 替换后占位符清空、结果以 `/` 开头。
  // ⚠️ 见证值仍是**合成**的（⛔ 不是真实例）：本判据跑在收集期、没有浏览器，拿不到真实例。
  const SYNTHETIC_SHAPE_WITNESSES = ['1'.repeat(20), '1', '42']
  const notInstantiable = PARAMETERIZED_ROUTES.filter((r) => {
    const names = [...r.pattern.matchAll(/\{([^}]+)\}/g)].map((m) => m[1])
    if (names.length === 0) return true
    // ① 形状判据必须至少接受一个合成见证值（否则"实例化"这一步永远不可能成功）。
    const witness = SYNTHETIC_SHAPE_WITNESSES.find((v) => r.shape.test(v))
    if (witness === undefined) return true
    // ② 用见证值替换全部占位符后，必须是一条以 `/` 开头、不含剩余占位符的路径。
    let concrete = r.pattern
    for (const n of names) {
      concrete = concrete.replace(`{${n}}`, witness)
    }
    return concrete.includes('{') || concrete.includes('}') || !concrete.startsWith('/')
  }).map((r) => r.pattern)
  expect(
    notInstantiable,
    '下列 pattern 拼不出具体路径（形状判据不接受任何合成见证值 / 占位符没被解析 / 结果不是以 / 开头）：' +
      notInstantiable.join('、'),
  ).toEqual([])

  // ④ 每个被测职位都必须有 D2 的下界登记（缺键 ⇒ `expect(...).toBeDefined()` 才红，这里提前点名）。
  const missingFloor = ROLE_MATRIX.filter((r) => INSTANTIATED_CANDIDATE_FLOOR[r.role] === undefined).map(
    (r) => r.role,
  )
  expect(
    missingFloor,
    '下列职位没登记 `INSTANTIATED_CANDIDATE_FLOOR`（D2 那半会静默放行）：' + missingFloor.join('、'),
  ).toEqual([])
  // ④b **写模式的四张下界表**也必须每个职位都有登记（T2）：缺键时那几条判据在**写模式下**
  //     会走"未登记 ⇒ 静默放行"的分支，而读模式完全看不出来（本结构用例两种模式都跑）。
  //     ⛔ 与 ④ 同一把尺：外部给定的 `ROLE_MATRIX` × 各表，⛔ 不是从表自己算出来的量。
  const writeFloorTables: [string, Record<string, number>][] = [
    ['CANDIDATE_FLOOR_WRITE', CANDIDATE_FLOOR_WRITE],
    ['LAYER_CONTROL_FLOOR_WRITE', LAYER_CONTROL_FLOOR_WRITE],
    ['INSTANTIATED_CANDIDATE_FLOOR_WRITE', INSTANTIATED_CANDIDATE_FLOOR_WRITE],
    ['WRITES_PERFORMED_FLOOR_WRITE', WRITES_PERFORMED_FLOOR_WRITE],
  ]
  const missingWriteFloors = writeFloorTables.flatMap(([name, table]) =>
    ROLE_MATRIX.filter((r) => table[r.role] === undefined).map((r) => `${name} 缺 ${r.role}`),
  )
  expect(
    missingWriteFloors,
    '写模式的覆盖下界表缺职位登记（写模式跑起来那几条判据会静默放行）：' +
      missingWriteFloors.join('、'),
  ).toEqual([])
  // 自证不是空转：矩阵与候选清单都不许是空的（空的话上面三条全恒真）。
  expect(ROLE_MATRIX.length).toBeGreaterThan(1)
  expect(CANDIDATES.length).toBeGreaterThan(1)
})
