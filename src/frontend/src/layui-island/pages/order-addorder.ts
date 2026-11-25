/**
 * 【录入订单】页的 **layui 复刻页**（对标物 `/Order/addorder`，**证据件渲染 + 接真后端**）。
 *
 * 与其余 70 个复刻页的分工：本页是「外观 1:1 复刻 + 保留真功能」的 6 页之一，
 * 录单必须真的提交到 `/api/business/orders/`。行为基线是既有 EP 实现
 * `views/orders/OrderNewView.vue` + `views/orders/orderFormFields.ts` + 它们的 spec。
 *
 * ## 运行期渲染模板（⛔ 不能省）
 *
 * 对标的整张表单躺在 `<template id="template">` 里，由原页面 JS 用 laytpl 克隆进 `#root`。
 * `<template>` 的元素内容**不参与渲染**，「把证据件塞进 DOM」只完成了一半 ⇒ 必须再跑一次
 * laytpl（`renderTemplate()`）。证据件 `public/replica/order-addorder.html` 与原文逐行一致，
 * 只有两处有意差异：`{{ const i18n = … }}` 无输出逻辑块删掉；`orderSoures2` 的 14 个店铺名
 * `<option>` 按 PII 口径清空（行数保持对齐）。
 *
 * ## 值级有意偏离（已登记，parity 会看到）
 *
 * `#project` 的 `maxlength` 放宽到 **900**（长单需要更多字数；后端 `Order.project_text`
 * 列宽同步 900）。⚠️ 因此这一处与证据件不再逐字一致是预期的。⚠️ `maxlength` 只管**用户打字**，
 * 管不住 JS 赋值（勾「常用项目」的拼接在 `toggleProjectText`）——真正的闸门是后端从模型派生的
 * `max_length`。决策与代价见 `docs/DECISIONS.md` D-0140 / D-0218。
 *
 * ## 运行期文案与显隐
 *
 * `showpw()` 改 4 个 label 文案（见 `labelOverrides()`）——parity 逐字比 `.layui-form-label`
 * 的文本，**必须**在挂载时改。`.gamegroupsdiv` 在游戏名不含「魔兽世界」时必须 `hide()`
 * ——不隐藏就会多出一个可见的 xm-select，label 的 x/宽度与控件序列都会偏。
 *
 * ## 功能映射（权威出处 = `FIELD_MAP` / `IGNORED_FIELDS` / `NOT_OPEN_CONTROLS`）
 *
 * 对标 68 个带 `name` 的控件逐条落在三张表里（spec 做穷尽性对拍）。要点：
 * `demand` 在对标页**没有控件**；`source` 不传（只传 `order_source` 字典 id，名字快照由**后端**写）；
 * `payment_method` 整行 `layui-hide`、不进载荷；`sales_agent` 那一格是运行期灌进去的 radio 组
 * （`renderSalesAgentOptions`，候选来自 `GET /orders/kefu-options/`；新建态提交 `sales_user`
 * 主键、编辑态走 `POST /orders/<单号>/sales-agent/`，名字一律由服务端从人身上算）。
 * 粘贴解析接我们自己的 `POST /api/business/orders/parse/`（EP 同一份 `applyParsedToForm`：
 * 只写解析出来的项、绝不置空；名称精确同名查找，找不到如实报出来）。
 *
 * ## 金额校验与录单态闸门
 *
 * `validateMoneyInput` 是权威；对标标记里 `#price` 的 `lay-verify="number"` 与 `#project` 的
 * `lay-verify="required"` **原样保留**（两者与我们的谓词不是同一个，差异对拍钉在
 * `order-addorder.spec.ts`）。金额「可为负」的提示走 `lay-tips`/焦点 tips（⛔ 不能写进
 * placeholder——parity 逐字比对视觉值）。⚠️ 录单态另有「订单总价必填」闸（有意偏离对标，
 * `totalPriceRequiredError`，**只接新建态**）；「默认 0」由证据件 `value="0"` 承担。
 *
 * ## 保存成功后（有意偏离对标）
 *
 * ⛔ **不弹层**：`createOrder` 成功后只出 `SAVE_SUCCESS_MESSAGE`（「录单成功」）小提示，
 * 随后 `resetAfterSave()` 复位成刚打开时的状态；单号不展示，返回值被显式丢弃。
 * 「刷新」不用 `location.reload()`（本页在 island 的 iframe 里，整页 reload 会丢会话感）。
 *
 * ## 编辑态（`?order_no=` 非空）
 *
 * = 管理订单页「编辑订单」弹窗的内嵌页（对标同构的 iframe 大弹层）。保存**真提交**：
 * `PATCH /api/business/orders/<no>/`，成功后 `postMessage` 通知父窗口关弹窗 + 出提示 + 刷新列表。
 * ⚠️ **编辑载荷只装非金额字段**（`formToUpdatePayload` 删掉 7 个金额）：后端把金额列只读
 * （改价要单独留痕），改价仍走订单列表的 `/price/`——**已知边界**，不是遗漏。
 */
import {
  changeSalesAgent,
  createOrder,
  fetchAssigneeOptions,
  // 「接单销售客服」候选：**复用既有封装**，⛔ 不在这里另写一个打同一端点的函数
  // （`order-orderlist.ts` 的筛选那一格用的是同一发）。
  fetchKefuOptions,
  fetchOrder,
  fetchSplitMembers,
  parsePastedText,
  readPlatformOrder,
  updateOrderWithMessage,
  type AssigneeOption,
  type CreateOrderPayload,
  type Order,
  type ParsedFields,
  type ParseResult,
  type UpdateOrderPayload,
} from '@/api/orders'
// 来源字典的**独立**封装（spec §6.3：不要塞进 `api/orders.ts`——并行线易冲突）。
import { fetchOrderSources, type OrderSourceRow } from '@/api/orderSources'
import type { Page } from '@/api/unwrap'
import {
  fetchCategories,
  fetchGames,
  fetchProjects,
  fetchServers,
  type CategoryRow,
  type ConfigRow,
  type ProjectRow,
  type ServerRow,
} from '@/api/config'
import { fetchMe, type User } from '@/api/auth'
import { BizError, HttpError, NetworkError } from '@/api/errors'
// `1022` 走**前端码表**那一处（镜像 `apps/core/error_codes.py`；⛔ 不在页面里再抄一个字面量，
// 免得同一个数字有两个出处）。
import { ErrorCode } from '@/config/errorCodes'
// 权限节点判据：本页有两个会打「要节点」接口的入口（保存 / 粘贴解析）。
// `canNode` 用岛页共享的那一份（`perms.ts`，与 `order-orderlist.ts` 同口径），
// ⛔ 不在这里另写一份（E-0021）。
import { canNode, type NodeViewerInput } from '@/layui-island/perms'
import {
  MONEY_INPUT_PLACEHOLDER,
  ORDER_MONEY_FIELDS,
  isBlankLikePython,
  validateMoneyInput,
} from '@/config/money'
// 「按内容认游戏」的纯函数：它只**回答**哪一款，切不切由页面按
// `userPickedGame` / `state.game` 自己决定（spec §3.1）。
import { detectGameFromText } from '@/views/orders/gameDetect'
// 常用项目的拼/删与客服工作台**共用同一份**实现（见该模块文件头的误删形态说明）；
// 700 字节同步闸的判据也住在那边（本页文件中部再导出，既有 import 路径不变）。
import { projectTextSyncLimitError, toggleProjectName } from '@/views/orders/projectText'
import {
  ACTOR_PLACEHOLDER,
  applyParsedToForm,
  emptyOrderForm,
  formToPayload,
  type OptionDicts,
  type OrderForm,
} from '@/views/orders/orderFormFields'
// 「默认指派此人」的四态判据外移在 `views/orders/assigneePreference.ts`
// （两个页面共用**同一份**判据，⛔ 不各写一套）；
// ⚠️ 这里**再导出**同一个绑定：既有测试与页内调用都还按 `order-addorder` 这个名字 import。
import { rememberAssigneeValue } from '@/views/orders/assigneePreference'
import {
  bootstrapIsland,
  initIslandElements,
  islandMsg,
  type IslandContext,
  type IslandPage,
} from '@/layui/island'
import { escapeHtml } from '@/layui-island/escape'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'
import { initFormSelects } from '@/layui/vendor'

/** 「默认指派此人」的载荷判据（四态）：共享实现再导出（`/kefu` 页也用同一条判据，不能 import 本文件），两个页面共用同一份实现；既有 import 路径不变。 */
export { rememberAssigneeValue }

/** 证据件（对标页 `<style>` 段 + body 标记；零转义，可直接与对标原文逐行对拍）。 */
export const ARTIFACT_URL = 'replica/order-addorder.html'

/** 证据件里的 `#template` / `#root`。 */
export const TEMPLATE_ID = 'template'
export const ROOT_ID = 'root'

/** 本页表单模型 = EP 的 `OrderForm` 加一个 `order_type`。⛔ 不直接改 `OrderForm`：那份是 EP 视图基线，加恒非空键会让 EP 载荷每单多一个键（对别的页面的行为改动）。 */
export type AddOrderForm = OrderForm & { order_type: string }

export function emptyAddOrderForm(): AddOrderForm {
  return { ...emptyOrderForm(), order_type: 'BOOST' }
}

/** 对标控件 `name` → `OrderForm` 键（readFormState / writeFormState 共用的唯一映射出处，spec 拿它与键集对拍）。唯一不在表里的是 `project`（自由文本，由 `toggleProjectText()` 处理）。 */
export const FIELD_MAP = {
  gameAcc: 'game_account',
  gameZAcc: 'game_sub_account',
  gameRole: 'game_role_name',
  gaemTf: 'current_info_1',
  gameSQ: 'current_info_2',
  dlcontent: 'content',
  souresOrderID: 'source_order_no',
  moblie: 'customer_phone',
  qq: 'customer_qq',
  ww: 'customer_ww',
  remark: 'remark',
  tag: 'tag',
  price: 'total_price',
  priceReceipts: 'received_price',
  throwPrice: 'platform_price',
  throwInterPrice: 'hall_price',
  // ⭐ D-0179：「给打手发单价」也写 `hall_price`——发单价 = 大厅价 = 指派价，⛔ 不新建金额列。
  // ⛔ 同一列两个 DOM 控件，FIELD_MAP 遍历顺序就是承重判据：写在后面的赢 ⇒ `#throwFee` 必须排在 `#throwInterPrice` 之后；顺序有专门用例钉住，⛔ 别把两行调换。
  throwFee: 'hall_price',
  // safeAmount / effAmount 不在这张表：后端已无 deposit_safe / deposit_efficiency。
  // 塞回来会让「FIELD_MAP 的值必须是 OrderForm 的键」变红。它们走「尚未开放」。
  desServiceFee: 'fee',
} as const satisfies Record<string, keyof OrderForm>

/** `dltype` 复选追加/删除自由文本项目的输入框（即证据件里的 `#project`）。 */
export const PROJECT_TEXT_NAME = 'project'

/*
 * 「代练项目 700 字节同步闸」的判据**下沉**在 `views/orders/projectText.ts`
 * （第二个消费方：客服精简工作台）。本页**再导出**，既有测试的 import 路径不变；
 * 定义与判据的全文注释随函数搬到了那边（⛔ 不在这里留第二份抄写）。
 */
export {
  PROJECT_TEXT_SYNC_MAX_BYTES,
  projectTextSyncLimitError,
} from '@/views/orders/projectText'

/** 对标 68 个带 `name` 的控件里不在 `FIELD_MAP` 的逐条去向（spec 的穷尽性对拍表：DOM 每个 `[name]` 必须落在两边之一）。 */
export const IGNORED_FIELDS: Readonly<Record<string, string>> = {
  // —— 有映射，但由专门的读取路径处理（不在 FIELD_MAP 的逐 name 文本同步里）——
  orderType: '→ order_type：radio，由 readFormState 的 checkedRadio 读',
  gameName: '→ game：下拉，选项来自 fetchGames()（值 = 我们的游戏 id）',
  gameArea: '→ server：xm-select，由 formSelects 同步回隐藏的 #gameArea',
  day: '→ time_requirement：与 hours 合成（dayHoursToTimeRequirement）',
  hours: '→ time_requirement：与 day 合成',
  jiaji: '→ is_urgent：radio，Y → true',
  projectType: '→ category：运行期由 fetchCategories 注入 .orderTypeDiv 的单选',
  dltype: '→ project：运行期由 fetchProjects 注入 .dllistdiv 的复选（恰好一项时进 FK）',
  project: '→ project_text：自由文本，由 toggleProjectText 处理（不进 FIELD_MAP）',
  // T6：它**不是**「尚未开放」了——选项来自 `fetchOrderSources()`（值 = 来源字典 id），
  // 由 `readOrderSource` 读（先读隐藏 `<select>`，为空回落 `formSelects`），进 `order_source`。
  // ⚠️ **不要**把它加进 FIELD_MAP：那张表会把值 `String()` 化，数字 id 会变成字符串（spec §12-S4）。
  orderSoures:
    '→ order_source：xm-select **单选**，选项来自 fetchOrderSources()（值 = 来源 id），由 readOrderSource 读',
  // ⭐ 「快速指派」三件套不再登记为「尚未开放」：`#throwFee` 进了 `FIELD_MAP`（→ `hall_price`），
  //    下面四个 `[name]` 由页面**快速指派那一段专门的读写路径**处理（`.kszpDiv` 的候选 radio /
  //    排序方式 / 搜索框 / 「默认指派此人」勾选框）——它们不是「逐 name 文本同步」的字段，
  //    所以留在本表（穷尽性判据要求每个 `[name]` 都落在 `FIELD_MAP` 或本表里）。
  recUser:
    '→ 快速指派：`.kszp` 里的接手人 radio（值 = auth.User 主键），由 form.on("radio(userlist)") 读',
  userOrderBy: '快速指派的「排序方式」：`data-notopen` 登记为不生效（见 NOT_OPEN_CONTROLS 的同名条目）',
  seachUserKey1: '→ 快速指派的搜索框：值作为 assignee-options 的 `key` 参数（不逐 name 同步）',
  defAss:
    '→ 快速指派的「默认指派此人」勾选框：进载荷的 `remember_assignee`（三态，见 rememberAssigneeValue）',
  // —— 对标有、我们后端没有：提示「尚未开放」（见 NOT_OPEN_CONTROLS）——
  gamePwd: '后端订单模型没有游戏登录密码字段',
  loginKey: '后端订单模型没有暗号/口令字段',
  gameServer: '对标把「区/平台」与「服」拆成两级，我们的区服只有一级',
  gameJob: '后端没有职业/门派配置',
  gameGroup: '后端没有阵营门派配置',
  safeKeyType: '后端订单模型没有密保类型字段',
  // P4-1 价格段修订删掉了后端的 `deposit_safe` / `deposit_efficiency` 两个字段
  // ⇒ 这两个对标控件按"对标有、我们没有"处理（提示「尚未开放」，见 NOT_OPEN_CONTROLS）。
  safeAmount: '后端已无「安全保证金」字段（P4-1 价格段修订删除）',
  effAmount: '后端已无「效率保证金」字段（P4-1 价格段修订删除）',
  customerName: '后端没有会员体系（会员号/余额）',
  vipDedCheck: '后端没有会员体系（余额扣款）',
  // ⭐ **拆单不是「尚未开放」**：勾上提交带 `split: true`
  // （`payload.split`），由后端按 `/` 切分项目落 N 笔同组订单——**有专门路径**
  // （保存链新建态那一支读它，见 `chaifenBox`）。
  chaifen:
    '→ split：录单页的「按代练项目拆分订单」勾选框（新建态保存时带 `split: true`，切分由后端做）',
  // —— 导入文件 tab：对标的 /OrderAjax/ImputAddOrderByFile 我们没有（整块提示尚未开放）——
  importGameName: '导入文件 tab：对标导入接口我们没有',
  fileSoure: '导入文件 tab：同上',
  orderSoures2: '导入文件 tab：同上（其 14 个店铺名 option 已按 PII 口径在证据件里清空）',
  throwAction: '导入文件 tab：同上',
  filterRepeat: '导入文件 tab：同上',
  // —— 发平台/延迟发平台：我们没有（.pushplatdiv / .delayDiv 默认 display:none）——
  pushplatformtemplte: '快速发单模板：后端没有',
  isDelay: '延迟发平台：后端没有',
  delaySec: '延迟发平台：后端没有',
  payMethed: '对标整行带 layui-hide（HTML L798，全文没有把它显示出来的代码）+ 我们录单没有这个控件',
  // —— 隐藏域：对标服务端渲染的编辑态/权限位，我们只做「新增」，一律不提交 ——
  orderID: '隐藏域：对标的编辑态订单号（本页只新增）',
  gameNumber: '隐藏域：对标的游戏数量缓存',
  readPriceIsAllowEdit: '隐藏域：对标的权限位',
  isChangeService: '隐藏域：对标的权限位',
  tGameArea: '隐藏域：对标的编辑态回填',
  tGameServer: '隐藏域：对标的编辑态回填',
  tGameJob: '隐藏域：对标的编辑态回填',
  tGameGroup: '隐藏域：对标的编辑态回填',
  editCustomerService: '隐藏域：对标的权限位',
  editOrderSoure: '隐藏域：对标的编辑态回填',
  tproject: '隐藏域：对标的编辑态回填',
  ypwd: '隐藏域：对标的密码回填位',
  isDis: '隐藏域：对标开关',
  tbPayTime: '隐藏域：对标字段',
  defOrderType: '隐藏域：对标的默认订单类型',
  memberDedSwitch: '隐藏域：会员扣款开关（我们没有会员体系）',
  customerId: '隐藏域：会员 id（我们没有会员体系）',
  assMode: '隐藏域：对标的指派模式',
}

/** 本页**有映射**的金额输入框（5 个，与 HTML 同序）。⚠️ 条数 ≠ `ORDER_MONEY_FIELDS` 的 6 项：`refund_fee` 在录单页没有输入框（退款是独立流程）。 */
export const MONEY_DOM_SELECTORS = [
  '#price',
  '#priceReceipts',
  '#throwPrice',
  '#throwInterPrice',
  '#desServiceFee',
] as const

/** 对标有、我们后端没有的字段/控件 → 提示「尚未开放」。`container` 必须只包着「不支持的」控件：捕获阶段会 `stopPropagation`，把功能控件包进去会让它点不动。 */
export const NOT_OPEN_CONTROLS: readonly {
  selector: string
  label: string
  container?: string
  /** 该控件是文本输入：**非空**时失焦提示（静默吞掉用户填的内容比置灰更糟）。 */
  onBlur?: boolean
}[] = [
  { selector: '#gamePwd', label: '登录密码', container: '.layui-input-inline', onBlur: true },
  { selector: '#loginKey', label: '暗号/口令', container: '.layui-input-inline', onBlur: true },
  { selector: '#customerName', label: '会员号/会员名', container: '.layui-input-inline', onBlur: true },
  // P4-1 删掉了后端这两个金额字段 ⇒ 对标控件照渲染，但填了会提示「尚未开放」
  // （**不置灰**，D-0025；`onBlur: true` 是因为它们是文本输入——静默吞掉用户填的内容比置灰更糟）。
  { selector: '#safeAmount', label: '安全保证金', container: '.layui-input-inline', onBlur: true },
  { selector: '#effAmount', label: '效率保证金', container: '.layui-input-inline', onBlur: true },
  // T6：`#orderSoures`（订单来源）已接真数据（选项来自 `fetchOrderSources()`，值进 `order_source`）
  // ⇒ 从这里**摘掉**。它原来提示「尚未开放」是因为「对标的来源列表接口我们没有」——那句话现在是假的。
  { selector: '#gameServer', label: '服务器（服）', container: '.layui-input-inline' },
  // `gameJob` / `gameGroup` 两个 `<select>` 在证据件里**没有 `id`**（只有 `name` 与
  // `xm-select`）——选择器按 name 写，写成 `#gameJob` 会永远匹配不到（实测 pairs）。
  { selector: 'select[name=gameJob]', label: '职业/门派', container: '.layui-input-inline' },
  { selector: 'select[name=gameGroup]', label: '阵营门派', container: '.layui-input-inline' },
  { selector: 'input[name=safeKeyType]', label: '密保类型', container: '.layui-inline' },
  { selector: 'input[name=vipDedCheck]', label: '从余额扣款', container: '.vipDedCheckBox' },
  // ⭐ 「快速指派」起，原来的四条（`#throwFee` / `defAss` / `.KSPD` / `.seachBtn` +
  //    `.resetSeachBtn`）已摘掉——它们现在是真的功能（选人 / 发单价 / 默认指派此人 / 搜索 / 重置）。
  //    ⛔ 不要再把它们登记回来。
  //    ⚠️ 指派面板的「排序方式」照证据件渲染但**不生效**：后端排序固定、没有逐人排序的落库能力
  //    ⇒ 照「渲染 + 点了提示尚未开放，⛔ 不置灰、⛔ 也不假装它生效」。
  { selector: 'input[name=userOrderBy]', label: '排序方式', container: '.layui-inline' },
  { selector: '#upmypic10', label: '选择订单文件' },
  { selector: 'a[lay-href="/System/autodiscernset"]', label: '订单识别关键词设置' },
  { selector: 'a[lay-href="/Game/projectsetting"]', label: '常用项目设置' },
  // 「导入文件订单」整个页签：原实现的导入接口我们后端没有 → 整块提示。
  { selector: '.layui-tab-item:last-child', label: '导入文件订单' },
]

/** 页内的 `lay-href` 链接（对标的 `layuiadmin` 扩展实现，vendored 的 layui 里没有它）。 */
const LAY_HREF_SELECTOR = 'a[lay-href]'

/** 把 `NOT_OPEN_CONTROLS` 解析成要挂 `data-notopen` 的真实元素（导出纯函数供 spec 断言：每个 selector 真存在、每个 host 不含功能控件——容器选错会把整行吞掉）。 */
export function resolveNotOpenHosts(
  root: HTMLElement,
): { host: HTMLElement; label: string; onBlur: boolean }[] {
  const out: { host: HTMLElement; label: string; onBlur: boolean }[] = []
  for (const c of NOT_OPEN_CONTROLS) {
    const el = query<HTMLElement>(root, c.selector)
    if (!el) continue
    const host = c.container ? (el.closest<HTMLElement>(c.container) ?? el) : el
    out.push({ host, label: c.label, onBlur: c.onBlur === true })
  }
  return out
}

/** `orderType` radio 的取值 → 后端 `OrderType`（`apps/business/models.py:153-157`）。 */
export function orderTypeValue(raw: string): string {
  return raw === '1' ? 'COMPANION' : 'BOOST'
}

  /* ── 快速指派：纯函数与文案常量 ───────────────────────────────────── */

/** 快速指派弹层的标题（逐字照对标原文）。 */
export const KSZP_LAYER_TITLE = '快速指派接手人'

/**
 * ⛔ `ORDER_CREATED_ASSIGN_FAILED`（1022）这一档不是普通失败：订单**已经建好落库**、只有指派那一步没成。
 * 页面必须（1）原样显示后端那句、（2）**复位表单**——否则用户再点一次提交就是又建一张单。⛔ 别并进通用「保存失败」。
 * 值的唯一出处是前端码表 `@/config/errorCodes`，⛔ 不在这里另写字面量。
 */
export const ORDER_CREATED_ASSIGN_FAILED: number = ErrorCode.ORDER_CREATED_ASSIGN_FAILED

/**
 * ⛔ `ORDER_CREATED_THROW_FAILED`（1023）同一形状：内部抢单档是建单+抛单，抛单在建单事务之外 ⇒ 失败时订单已落库；同样必须原样显示 + 复位表单，⛔ 别并进通用「保存失败」。值唯一出处同上。
 */
export const ORDER_CREATED_THROW_FAILED: number = ErrorCode.ORDER_CREATED_THROW_FAILED

/**
 * 「提交结果未知」（5xx/网络异常 ⇒ 不知道后端建了没有）：⛔ 不能报成功、也不能像 1022 那样复位表单（真没建成功会把用户输入丢掉）——只能如实说结果未知，判断权交给用户。
 */
export const SUBMIT_OUTCOME_UNKNOWN_MESSAGE =
  '提交结果未知（服务器错误）：可能已经建单，请先到订单列表确认是否已建，再决定要不要重填或重新提交'

/** 「结果未知」之后再点保存时的二次确认文案（用户明确说「知道，再试一次」才继续）。 */
export const RESUBMIT_CONFIRM_MESSAGE =
  '上一次提交的结果未知（服务器错误），可能已经建单。请先到订单列表确认是否已建——确认要再提交一次吗？'

/** 这次失败算不算「结果未知」（只有它才需要防重复提交）：NetworkError / HttpError ≥500 ⇒ 请求可能已到达并落库。⛔ 别把业务失败/4xx 算进来（那是已知结果「没建」，加二次确认只是噪音）。 */
export function isUnknownSubmitOutcome(error: unknown): boolean {
  if (error instanceof NetworkError) return true
  return error instanceof HttpError && error.status >= 500
}

/**
 * ⛔ 「发单价」tip 第一句逐字，但有意丢掉对标的下半句（百分比写法）：我们这格走 `hall_price`（DecimalField），百分数在录单侧没有换算口——照抄就是让用户写必被 `1000` 拒掉的串（不留假话）。
 */
export const THROW_FEE_TIP =
  '可以在此输入给该打手或发内部抢单的发单价，不输入则默认为0。'

/**
 * ⛔ `THROW_FEE_TIP` 一个字节都不能动、这一档也不能复用它：内部抢单档「没填价 ⇒ 一个请求都不发」，照抄一句与本页行为相反的话比少照抄更糟。
 */
export const THROW_FEE_TIP_HALL =
  '发内部抢单的单价，必须大于 0。填 0 或留空点「录单」会被拦下，订单也不会建立。'

/**
 * `.KSPD` 按钮在内部抢单档的文案（逐字照对标原文）。
 * ⚠️ 字符串值照抄，但那一档的**载体**有意不同——对标那颗 radio 的 `value` 是 `999999`，
 * 我们的是 `hall`（理由见 `HALL_RADIO_VALUE`）。
 */
export const HALL_ASSIGNEE_LABEL = '内部抢单'

/**
 * 读平台订单失败时哪些业务码要把后端 `msg` 弹给客服（「开口面」唯一出处；散在 catch 里漏加过 `1028` ⇒ 客服看到一片空白像页面坏了）。
 * `1015` = 操作指引；`1028` = 说清为什么读不出来。⛔ `1016`/`1017`（真·上游故障）故意不在里；⛔ 别改成「所有 BizError 都开口」。
 */
export const SPEAKING_READ_ORDER_CODES: ReadonlySet<number> = new Set([1015, 1028])

/** 内部抢单没填发单价的提示：前端零请求闸；后端那道 `1000` 仍要有（拦不住直接打接口的人），两层各自独立。 */
export const HALL_PRICE_REQUIRED_MESSAGE = '内部抢单请先填发单价（必须大于 0）'

/** 「内部抢单」radio 的 `value`（纯前端档位标记，⛔ 不进载荷；有意用语义值不用对标的 999999——避免被误读为后端认这个数）。 */
export const HALL_RADIO_VALUE = 'hall'

/** 候选名单一次取多少人（后端 `AssigneeOptionsSerializer.limit` 的默认值，与弹层同档）。 */
export const ASSIGNEE_OPTIONS_LIMIT = 200

/** 「接单销售客服」候选一次取多少人（= 后端 `kefu_options` 默认）。本页显式传它：要用「这单现在记的是谁」匹配候选，截断时那个人可能落选 ⇒ 显式传同一个数字让「截断」可被如实说出（截断了就提示，⛔ 不静默）。 */
export const SALES_AGENT_OPTIONS_LIMIT = 200

/**
 * 选中接手人时的发单价自动填充（照对标算式逐档照抄：提成模式 20 ⇒ (提成/100)×接单价、10 ⇒ 定额、其余/无数据 ⇒ `0`）。
 * ⛔ 两处有意不同：算不出价写 `'0'` 不写空串（空串不发键 ⇒ 指派会以「没有接单价」失败）；空接单价算出 `NaN` 时也返回 `'0'`（⛔ 不把 NaN 递给用户）。
 * ⚠️ 今天后端不喂提成 ⇒ 线上恒落「没有提成数据」支（填 `'0'`）——算式是真的、接线是真的，只是数据源空着，不是假装算价。
 */
export function assigneeThrowFee(
  commission: { mode?: number | null; value?: number | string | null },
  totalPrice: string,
): string {
  const value = Number(commission.value ?? 0)
  // `value > 0` 逐字照对标（L1112 的 `if (allUserList[u].tichen > 0)`）：0 与负数都当「没配」。
  if (!Number.isFinite(value) || value <= 0) return '0'
  if (commission.mode === 20) {
    const price = Number(totalPrice)
    if (!Number.isFinite(price)) return '0'
    return ((value / 100) * price).toFixed(2)
  }
  if (commission.mode === 10) return String(value)
  return '0'
}

/** `day`+`hours` → 自由文本 `time_requirement`（`N天M小时`，0 的整段省略；两段都 0 → 空串 = 不填）。 */
export function dayHoursToTimeRequirement(day: string, hours: string): string {
  const parts: string[] = []
  if (Number(day) > 0) parts.push(`${Number(day)}天`)
  if (Number(hours) > 0) parts.push(`${Number(hours)}小时`)
  return parts.join('')
}

/**
 * `dayHoursToTimeRequirement` 的**回填用**逆函数：`'2天3.5小时'` → `{ day: '2', hours: '3.5' }`。
 * 只在编辑态用；认不出的形状（如 `'长期'`）回空串——调用方据此**保留后端原值**，不静默丢掉。
 */
export function timeRequirementToDayHours(text: string): { day: string; hours: string } {
  const day = /(\d+(?:\.\d+)?)\s*天/.exec(text)
  const hours = /(\d+(?:\.\d+)?)\s*小时/.exec(text)
  return { day: day ? day[1]! : '', hours: hours ? hours[1]! : '' }
}

/**
 * 编辑态：从 iframe URL 里读订单号（参数名用我们的 snake_case `order_no`，
 * 与 `api/orders.ts` 的路径参数一致）。
 */
export function editOrderNoFromSearch(search: string): string {
  return new URLSearchParams(search).get('order_no') ?? ''
}

/** 编辑回填：`GET /orders/<no>/` 响应 → 表单模型（逐键走 `FIELD_MAP` 唯一出处；金额 null 统一成 `''`）。⚠️ `order_source` 显式赋值、⛔ 不进 FIELD_MAP（那个循环会把数字 id `String()` 化）；不回填会把来源「改没」。 */
export function orderToAddOrderForm(order: Order): AddOrderForm {
  const form = emptyAddOrderForm()
  const target = form as unknown as Record<string, unknown>
  const source = order as unknown as Record<string, unknown>
  for (const field of Object.values(FIELD_MAP)) {
    const value = source[field]
    target[field] = value === null || value === undefined ? '' : String(value)
  }
  form.game = order.game
  form.server = order.server
  form.category = order.category
  form.project = order.project
  form.project_text = order.project_text
  form.content = order.content
  form.time_requirement = order.time_requirement
  form.is_urgent = order.is_urgent
  // ⭐ **接单销售客服**：它是 `Order.sales_agent` 那列**文本快照**
  // （不是主键）⇒ 编辑态回填要拿它的**文本**去匹配候选项的 `name`（见 `renderSalesAgentOptions`）。
  // 后端回 `null` 时统一成 `''`（口径同上面 `FIELD_MAP` 那个循环）。
  form.sales_agent = order.sales_agent ?? ''
  form.order_type = order.order_type === 'COMPANION' ? 'COMPANION' : 'BOOST'
  // T6：来源字典 id **显式赋值**（像上面的 FK 一样），**不要**加进 `FIELD_MAP`——
  // 那个循环会把值 `String()` 化（spec §12-S4），数字 id 会变成 `'20'`。
  // 不回填的话，编辑订单保存会把来源「改没」或「改不动」（两条都是静默失败）。
  form.order_source = order.order_source ?? null
  return form
}

/** 对标 `showpw()` 改写的 4 个 label 文案。**必须复刻**：parity 逐字比 `.layui-form-label` 的文本，不改就是 2 条 struct 差异。 */
export function labelOverrides(orderType: string): Record<string, string> {
  const companion = orderType === '1'
  return {
    '.projectstr': companion ? '陪玩内容/标题' : '代练项目/标题',
    '.throwFeestr': companion ? '发陪玩价格' : '给打手发单价',
    '.timetag': companion ? '陪玩时长' : '时间要求',
    '.bossNameLabel': companion ? '老板名称' : '角色名',
  }
}

/** 一个金额字段的输入校验（复用 EP 的同一份 `validateMoneyInput`）。 */
export function moneyError(form: OrderForm, field: string): string {
  const value = (form as unknown as Record<string, string>)[field] ?? ''
  const result = validateMoneyInput(value)
  return result.ok ? '' : result.msg
}

/** 第一个金额错误（`''` = 全通过）。顺序取 `ORDER_MONEY_FIELDS`，与 EP 的提交前检查同源。 */
export function firstMoneyError(form: OrderForm): string {
  for (const f of ORDER_MONEY_FIELDS) {
    const msg = moneyError(form, f)
    if (msg !== '') return msg
  }
  return ''
}

/** 提交前的必填校验（游戏与 `project_text`，后者按 Python `strip()` 判空）。⚠️ 不是录单态全部必填——总价那条单独在 `totalPriceRequiredError`（只新建态生效），本函数两条态共用。 */
export function requiredError(form: AddOrderForm): string {
  if (form.game === null) return '请选择游戏'
  if (isBlankLikePython(form.project_text)) return '代练/陪玩项目不可为空'
  return ''
}

/**
 * 录单（新建）态的第三条必填：订单总价。
 * ⛔ 判据用 `isBlankLikePython`，别改 JS 真值判——`'0'`/`'0.00'`/`'-5'` 都是合法值。
 * 「默认 0」来自证据件 `#price` 的 `value="0"`（⛔ 不在 TS 里再赋值），复位靠原生 `form.reset()`。
 * ⚠️ 有意偏离对标（对标对空值放行）；只在新建态接（编辑态载荷不含金额）。
 */
export function totalPriceRequiredError(form: AddOrderForm): string {
  if (isBlankLikePython(form.total_price)) return '订单总价不可为空'
  return ''
}

/** 保存成功提示（业务方口径）：⛔ 不弹窗、不展示单号（有意偏离对标），文案由业务方给定、不要改写。 */
export const SAVE_SUCCESS_MESSAGE = '录单成功'

/** 编辑态保存成功发给父窗口的消息类型：两端只有这一份定义（`order-orderlist.ts` import 本常量过滤；各抄一份字面量会在改名时静默断链）。 */
export const ORDER_SAVED_MESSAGE_TYPE = 'island:order-saved'

/**
 * 编辑保存成功的提示文案。**由父窗口显示**（子页面先关弹窗，它自己弹的提示会一起消失）。
 * 与录单页的 `SAVE_SUCCESS_MESSAGE` 同一口径：只出一个 `layer.msg` 小提示，不弹层。
 */
export const EDIT_SAVE_SUCCESS_MESSAGE = '保存成功'

/**
 * ⛔ 「订单已保存，但销售客服没改成」必须单独一句、把两件事都说清（编辑态换归属是第二发请求，它失败时订单已存好；说「保存失败」或不说都是假话）。
 * ⛔ 别把「已保存」那半句删掉（E-0010 同族：别把「一半成功」伪装成一个方向的结论）。
 */
export const SALES_AGENT_FAILED_MESSAGE = '订单已保存，但销售客服没改成'

/** 编辑态保存成功 → postMessage 通知父窗口（⛔ 别直接摸 `parent.layer`——本页会被单独打开，那时直接调会炸）。`msg` = 后端成功信封原文原样带过去（含「未能撤回」警告时父页用 `layer.alert`）。 */
export function notifyOrderSaved(orderNo: string, msg = ''): void {
  window.parent.postMessage(
    { type: ORDER_SAVED_MESSAGE_TYPE, order_no: orderNo, msg },
    window.location.origin,
  )
}

/** 编辑态载荷 = `formToPayload` 减去 7 个金额字段。⛔ 必须减：后端把金额列只读、送出去被静默忽略 ⇒ 用户改价拿到「保存成功」价格却没变；改价只能走 `/price/` 单独留痕（已知边界）。 */
export function formToUpdatePayload(form: OrderForm, actor: string): UpdateOrderPayload {
  const payload = formToPayload(form, actor) as Record<string, unknown>
  for (const field of ORDER_MONEY_FIELDS) delete payload[field]
  return payload as UpdateOrderPayload
}

/* ─────────────────────────── DOM ⇄ 模型 ─────────────────────────── */

function query<T extends HTMLElement>(root: HTMLElement, selector: string): T | null {
  return root.querySelector<T>(selector)
}

function inputValue(root: HTMLElement, name: string): string {
  const el = query<HTMLInputElement | HTMLTextAreaElement>(root, `[name="${name}"]`)
  return el?.value ?? ''
}

/** 把单个字段写进对应 DOM 控件（形参类型 = `FIELD_MAP` 键，控件名写错编译期挡住）。⚠️ 只写 DOM、不直接改模型——改了模型没写控件，提交重读与用户所见就分叉。 */
function writeField(root: HTMLElement, name: keyof typeof FIELD_MAP, value: string): void {
  const el = query<HTMLInputElement | HTMLTextAreaElement>(root, `[name="${name}"]`)
  if (el) el.value = value
}

function checkedRadio(root: HTMLElement, name: string): string {
  const el = query<HTMLInputElement>(root, `input[name="${name}"]:checked`)
  return el?.value ?? ''
}

function selectValue(root: HTMLElement, id: string): string {
  const el = query<HTMLSelectElement>(root, `#${id}`)
  return el?.value ?? ''
}

/** 数字型下拉（游戏 / 区服 / 分类 / 项目）的空值统一归成 `null`。 */
function toId(raw: string | number | null | undefined): number | null {
  if (raw === '' || raw === null || raw === undefined) return null
  const n = Number(raw)
  return Number.isFinite(n) ? n : null
}

/** 来源字典整页 → xm-select 选项数组。⚠️ 只消费 `items`（回的是整页对象，不是数组）。 */
export function orderSourceOptions(page: Page<OrderSourceRow>): { name: string; value: number }[] {
  return page.items.map((row) => ({ name: row.name, value: row.id }))
}

/** 读当前选中的来源 id：先读隐藏 `<select>`、为空回落 `formSelects.value`；`toId` 把空串归 `null`——空不是 0（`0` 不是合法来源 id，别用真值判混成一件事）。`fsValue` 是注入接缝（jsdom 直接给 select 赋值走同一路径）。 */
export function readOrderSource(
  root: HTMLElement,
  fsValue: (id: string) => unknown = () => undefined,
): number | null {
  const fromSelect = toId(selectValue(root, 'orderSoures'))
  if (fromSelect !== null) return fromSelect
  const raw = fsValue('orderSoures')
  const first = Array.isArray(raw) ? (raw[0] as unknown) : raw
  const value =
    typeof first === 'object' && first !== null
      ? (first as { value?: unknown }).value
      : (first as unknown)
  return toId(value === null || value === undefined ? '' : String(value))
}

/** DOM → 模型。`readServer` 是注入接缝（xm-select 选中值被同步回隐藏 `<select id="gameArea">`，默认读它——jsdom 直接赋值走同一路径）。 */
export function readFormState(
  root: HTMLElement,
  readServer: () => number | null = () => toId(selectValue(root, 'gameArea')),
): AddOrderForm {
  const form = emptyAddOrderForm()
  const target = form as unknown as Record<string, unknown>

  for (const [name, field] of Object.entries(FIELD_MAP)) {
    target[field] = inputValue(root, name)
  }

  form.game = toId(selectValue(root, 'gameName'))
  form.server = readServer()
  // 项目分类是「有项目就选」的单选（`projectType`，值 = 我们的分类 id）
  form.category = toId(checkedRadio(root, 'projectType'))
  // 勾中的常用项目（`dltype`）只在**恰好一项**时落进 FK；多项时以自由文本为准。
  const checkedProjects = Array.from(
    root.querySelectorAll<HTMLInputElement>('input[name="dltype"]:checked'),
  )
  form.project = checkedProjects.length === 1 ? toId(checkedProjects[0]?.value) : null
  form.project_text = inputValue(root, PROJECT_TEXT_NAME)
  form.time_requirement = dayHoursToTimeRequirement(
    selectValue(root, 'day'),
    selectValue(root, 'hours'),
  )
  form.is_urgent = checkedRadio(root, 'jiaji') === 'Y'
  form.order_type = orderTypeValue(checkedRadio(root, 'orderType'))

  return form
}

/**
 * 模型 → DOM（粘贴解析结果落地、以及「继续录入」清空表单时用）。
 * 只写**有对应控件**的字段：`demand` / `payment_method` 没有可用控件；`source` 只传
 * `order_source`（字典 id），名字快照由后端写；`sales_agent` 有专门的读写路径
 * （`renderSalesAgentOptions` / `readSalesAgentChoice`），不进 `FIELD_MAP`
 * （那一格是运行期灌进 `.cf` 的 radio 组，逐 name 文本同步对它没有意义）。
 */
export function writeFormState(root: HTMLElement, form: AddOrderForm): void {
  const source = form as unknown as Record<string, unknown>
  for (const [name, field] of Object.entries(FIELD_MAP)) {
    const el = query<HTMLInputElement | HTMLTextAreaElement>(root, `[name="${name}"]`)
    if (el) el.value = String(source[field] ?? '')
  }
  const projectText = query<HTMLInputElement>(root, `#${PROJECT_TEXT_NAME}`)
  if (projectText) projectText.value = form.project_text
  const gameSelect = query<HTMLSelectElement>(root, '#gameName')
  if (gameSelect && form.game !== null) gameSelect.value = String(form.game)
  setRadio(root, 'jiaji', form.is_urgent ? 'Y' : 'N')
  setRadio(root, 'orderType', form.order_type === 'COMPANION' ? '1' : '0')
}

/* ─────────────── 接单销售客服（`sales_agent`）───────────────
 * 这一格与页面上其余控件都不同：它是**运行期灌进去的 radio 组**（证据件里那个
 * `<div class="layui-input-block cf">` 本来就是空的——⛔ 别据「服务端 HTML 那格是空的」
 * 推出「没有这个控件」）；`name` 与对标同名（`inputOrderKefu`），值 = **用户主键**、
 * `title` = 显示名。后端 `Order.sales_agent` 存**名字文本快照**（不是主键）⇒ 编辑态回填
 * 要拿**文本**匹配，匹配不上时⛔ 不许静默丢值。⚠️ 名字只用于「回填 / 预勾 / 提示」，
 * ⛔ 不进载荷（提交的是主键）⇒ `title` / `data-sales-agent-name` 是展示面。
 */

/** 那一格的容器选择器（证据件里的 `<div class="layui-input-block cf">`）。 */
export const SALES_AGENT_CONTAINER_SELECTOR = 'div.layui-input-block.cf'

/**
 * radio 的 `name`——**逐字与对标同名**。
 * ⛔ 不要改成我们的 snake_case：这一格是复刻面，`name` 也是复刻产物的一部分
 * （`name` 只在前端用，不进载荷——载荷那一栏叫 `sales_agent`）。
 */
export const SALES_AGENT_INPUT_NAME = 'inputOrderKefu'

/** 候选（值 = 用户主键、文本 = 显示名）——与 `fetchKefuOptions()` 的 `items` 同一形状。 */
export type SalesAgentOption = { id: number; name: string }

/** 一条候选渲染出来的 radio（`lay-skin="primary"` 与页内其余 radio 同形，见 `renderCandidates`）。 */
const RADIO_CELL_STYLE = 'margin-right:12px;white-space:nowrap'

/**
 * 把一个候选渲染成 layui 的 radio 标记。
 * ⚠️ `title` 走 `escapeHtml`：名字是人填的**自由文本**，与快速指派那份名单同一条纪律。
 */
function salesAgentRadioHtml(item: SalesAgentOption, checked: boolean): string {
  return (
    `<div class="layui-inline" style="${RADIO_CELL_STYLE}">` +
    `<input type="radio" name="${SALES_AGENT_INPUT_NAME}" lay-skin="primary"` +
    ` title="${escapeHtml(item.name)}" value="${item.id}" data-sales-agent-name="${escapeHtml(item.name)}"` +
    `${checked ? ' checked="checked"' : ''}>` +
    '</div>'
  )
}

/**
 * 把当前销售客服**渲染成纯文本**（没有编辑权限时的只读档，⛔ 不做成"隐藏"）。
 * 隐藏会让客服**不知道自己是谁**；置灰的控件仍能点（点了没反应也是静默误导）
 * ⇒ 渲染成**没有 radio** 的文本节点。⚠️ 值走 `textContent`（⛔ 不是 `innerHTML` 拼接）。
 */
function renderSalesAgentReadOnly(host: HTMLElement, name: string): void {
  host.textContent = ''
  const span = document.createElement('span')
  span.className = 'tt-sales-agent-readonly'
  span.textContent = name
  host.appendChild(span)
}

/**
 * 新建态 + **没有**该节点时那一格显示的**后半句**。
 * 它要说清「保存后这一单的提成归谁」——不持节点的人**选不了别人**（选了也不算数），
 * 而"一组点得动、但保存后不作数的 radio"正是本仓明令禁止的静默误导。
 * ⛔ 别把它写成"没有权限"：用户不关心权限，他只想知道"这单算谁的"。
 */
const INTAKE_SALES_AGENT_SELF_HINT = '保存后归你自己'

/**
 * 新建态 + 没有该节点时那一格的**完整文案**（拿不到登录人显示名时只有后半句）。
 * ⚠️ 名字与「服务端录单兜底写进 `Order.sales_agent` 的那个名字」**同源**
 * （`apps/accounts/display.py::user_display_name`）。取不到时**不编一个名字**：
 * 只显示那句说明（⛔ 不写"未知"）。
 */
function intakeSalesAgentHint(currentUserName: string): string {
  return currentUserName === ''
    ? INTAKE_SALES_AGENT_SELF_HINT
    : `${currentUserName}（${INTAKE_SALES_AGENT_SELF_HINT}）`
}

/**
 * 渲染「接单销售客服」那一格（新建 / 编辑两态共用）。
 *
 * 两档口径：① **持 `order.allow_change_sales_agent`** ⇒ 两态都渲染可选 radio
 * （新建态默认勾当前登录人；编辑态按文本匹配 `name` 预勾）；② **不持该节点** ⇒
 * 两态都只读（纯文本；编辑态显示本单现值、新建态显示「自己的名字（保存后归你自己）」）。
 *
 * ⚠️ 新建态那一档也只读：否则不持节点的人选了别人时载荷不带 `sales_user`，
 * 保存后提成仍归录单人而界面不给任何提示——「没有节点 ⇒ 画不成可点控件」，
 * 界面所见与提交内容一致（⛔ 不是放宽判据，是把静默误导改成如实呈现）。
 *
 * ⚠️ 匹配不上**不许静默丢值**：候选名单是今天的角色标记算出来的，现值可能因停用 /
 * 改角色 / 改昵称而**不在候选里**——只按「值不在候选」处理会让用户保存时把这一列**改没**。
 * ⇒ 把现值作为**一个额外候选项插在最前**并勾中（值用 `0`：不是真实用户主键，只是占位，
 * 不会被人提交——提交出去的是 `title` 栏的**文本**）。新建态而当前登录人不在候选里
 * （名单被截断 / 字典加载失败）⇒ 同一手法补一条并勾中。
 *
 * @returns 实际渲染的候选（含插进去的那一条）；只读两档回空数组（一颗 radio 都没画），
 *   给 spec 与调用方核对。
 */
export function renderSalesAgentOptions(
  root: HTMLElement,
  options: {
    items: readonly SalesAgentOption[]
    /** 当前登录人的**主键**（新建态的默认勾选；取不到 `null` = 不预勾、也不补）。 */
    currentUserId: number | null
    /** 当前登录人的**显示名**（`fetchMe()` 的 `display_name || username`；补候选时用）。 */
    currentUserName: string
    /** 本单**现有**的销售客服名字快照（编辑态才有；空串 = 这单没有）。 */
    currentName: string
    /** 是不是编辑态（`order_no` 非空）。 */
    editing: boolean
  },
): SalesAgentOption[] {
  const host = query<HTMLElement>(root, SALES_AGENT_CONTAINER_SELECTOR)
  if (!host) return []
  const { items, currentUserId, currentUserName, currentName, editing } = options
  host.innerHTML = ''

  // —— 没有该节点 ⇒ 只读，⛔ 一颗可点的 radio 都不画（**新建 / 编辑同一档**，见函数 docstring）——
  if (!canChangeSalesAgent) {
    renderSalesAgentReadOnly(
      host,
      editing ? currentName : intakeSalesAgentHint(currentUserName),
    )
    return []
  }

  // —— 选中项：新建态 = 当前登录人；编辑态 = 现值那个**名字**（文本匹配） ——
  const wantName = editing ? currentName : ''
  const rendered: SalesAgentOption[] = []

  if (editing) {
    // 编辑态带现值、而候选里没有同名项 ⇒ 插在最前（见上面那个 ⚠️ 的第一处）
    const matched = items.find((o) => o.name === wantName)
    if (wantName !== '' && matched === undefined) rendered.push({ id: 0, name: wantName })
    rendered.push(...items)
  } else {
    // 新建态：当前登录人不在候选里 ⇒ 也插一条（见上面那个「同族的第二处」）
    const self = currentUserId === null ? undefined : items.find((o) => o.id === currentUserId)
    if (currentUserId !== null && currentUserName !== '' && self === undefined) {
      rendered.push({ id: currentUserId, name: currentUserName })
    }
    rendered.push(...items)
  }

  // 勾中谁：**只在渲染出来的那一串里找**（插入项也在里面）——新建态找当前登录人的**主键**，
  // 编辑态找现值那个**名字**。找不到就 `null`（一颗都不勾），⛔ 绝不勾一个不是他的。
  const checkedId =
    editing
      ? (rendered.find((o) => wantName !== '' && o.name === wantName)?.id ?? null)
      : (rendered.find((o) => o.id === currentUserId)?.id ?? null)

  host.innerHTML = rendered
    .map((item) => salesAgentRadioHtml(item, checkedId !== null && item.id === checkedId))
    .join('')
  return rendered
}

/**
 * 读那一格当前选中的**名字**（**薄封装**：`readSalesAgentChoice(root, editing).name`）。
 * ⚠️ 它**不再是提交路径的入口**（两条提交路要的都是**主键**）；保留是因为既有用例与注释
 * 按这个名字引用"当前选中项的名字"这一事实（判据一字未改）。
 * 四档判据（**顺序承重**，唯一实现在 `readSalesAgentChoice`，本函数只取 `.name`）：
 * ① 持节点 ⇒ 两态都读；② 不持节点 ⇒ 两态都回空（两条提交路都不发）；
 * ③ 其余：读 `:checked` 那一颗的名字。
 * ⚠️ 名字读 `data-sales-agent-name`（渲染时写上去的原文），⛔ 不是 `title` 属性——
 * `title` 里的 HTML 实体已被浏览器解码过一次；读 data 属性时「解码发生在哪一层」的依赖就消失了。
 */
export function readSalesAgent(root: HTMLElement, editing: boolean): string {
  return readSalesAgentChoice(root, editing).name
}

/**
 * 读那一格当前选中的**主键 + 名字**（两条提交路要的是**不同**的值，但必须来自**同一颗** radio：
 * 新建态 `POST /orders/` 的 `sales_user`、编辑态专用端点的 `user`，都是主键；
 * ⛔ 名字**不进**任何 PATCH 载荷）。分两次读会给「DOM 被改动」留窗口，
 * 那种分叉的表现是「名字是甲、提成归乙」——必须成对读。
 *
 * 三档判据与 `readSalesAgent` 逐字相同（那个函数是本函数的薄封装，⛔ 判据不复制两份）。
 * ⚠️ **`id` 为 `null` 的两档都要当"没有主键"用**，调用方⛔ 不许拿 `0` 兜底：
 * 渲染时给「现值不在候选里」那条插入项用的是 `value="0"`（一个**不是真实用户**的占位），
 * 把它当主键提交出去会让后端去查一个不存在的用户。
 * ⇒ 「读不出正整数主键」一律归成 `null`（`Number.parseInt` 结果按 `isFinite` + `> 0` 判，
 * ⛔ 不用真值判：`'0'` 是**真值**却不是合法主键，E-0021 的 JS 侧同型）。
 * ⚠️ 名字读 `data-sales-agent-name`（渲染时写上去的原文），⛔ 不是 `title` 属性（理由见上）。
 */
export function readSalesAgentChoice(
  root: HTMLElement,
  // ⚠️ 这个形参**不参与判据**（只读那一档与新建 / 编辑无关），但**保留**它：
  // 两个调用点与既有用例都按「(root, editing)」传参，删掉是一次无关的签名改动。
  // 按本仓 eslint 约定（`argsIgnorePattern: '^_'`）加下划线。
  _editing: boolean,
): { id: number | null; name: string } {
  // ⚠️ 判据里**没有** `editing`：没有该节点的人，新建态那一格也是只读的 ⇒ 读不到 radio 是**预期**。
  if (!canChangeSalesAgent) return { id: null, name: '' }
  const el = query<HTMLInputElement>(
    root,
    `input[name="${SALES_AGENT_INPUT_NAME}"]:checked`,
  )
  if (!el) return { id: null, name: '' }
  const raw = Number.parseInt(el.getAttribute('value') ?? '', 10)
  return {
    id: Number.isFinite(raw) && raw > 0 ? raw : null,
    name: el.getAttribute('data-sales-agent-name') ?? '',
  }
}

/**
 * 「刷新当前录单页」＝ 把表单恢复成**刚打开时的状态**。
 * ⛔ 不用 `location.reload()`：本页跑在 island 的 iframe 里，整页 reload 会丢「会话感」。
 * 用原生 `form.reset()` 复位（文本/textarea 回 `value` 属性或空串、checkbox/radio 回
 * `defaultChecked`、`select` 回 `defaultSelected`——即「清空已填 + 日期回默认值」）。
 * 两个**运行期注入**的下拉不在覆盖范围内：`#gameName` 在这里显式复位到默认游戏，
 * `#gameArea` 由调用方重启级联重建；`#orderSoures` 同属这一类、这里**显式清空**
 * （别让上一单选的来源静默进下一单）。
 * 返回 `false` 表示证据件里没有 `<form lay-filter="form1">`（调用方据此决定是否告警）。
 */
export function resetFormToInitial(root: HTMLElement, gameId: number | null = null): boolean {
  const form = query<HTMLFormElement>(root, 'form[lay-filter="form1"]')
  if (!form) return false
  form.reset()
  const gameSelect = query<HTMLSelectElement>(root, '#gameName')
  if (gameSelect && gameId !== null) gameSelect.value = String(gameId)
  const serverSelect = query<HTMLSelectElement>(root, '#gameArea')
  if (serverSelect) serverSelect.value = ''
  // T6：来源下拉（选项由 `fetchOrderSources()` 运行期灌进去，`form.reset()` 覆盖不到）。
  // ⚠️ 必须显式清：`form.reset()` 对「有 option、没写 `selected`」的 select 回到**第一条 option**，
  // 于是上一单选的来源会静默进下一单（`readOrderSource` 读的就是这个 `<select>`）。
  const sourceSelect = query<HTMLSelectElement>(root, '#orderSoures')
  if (sourceSelect) sourceSelect.value = ''
  return true
}

function setRadio(root: HTMLElement, name: string, value: string): void {
  for (const el of Array.from(root.querySelectorAll<HTMLInputElement>(`input[name="${name}"]`))) {
    el.checked = el.value === value
  }
}

/**
 * 把粘贴解析结果写进**模型与 DOM**，返回命中 / 没对上的字段名。
 * ⚠️ 为什么要有这一层（而不是在页面里直接调 `applyParsedToForm`）：那个函数**只改模型**。
 * 漏了「推回 DOM」这一步时，解析命中的账号 / 角色名 / 项目 / 金额全都没进输入框，
 * 而提交时 `readFormState` 又从 DOM 重新读 ⇒ 整批解析结果静默丢失。
 * 抽成导出函数是为了让 spec 能在真 DOM 上钉住这件事。
 */
export function applyPasteToDom(
  host: HTMLElement,
  form: AddOrderForm,
  fields: ParsedFields,
  dicts: OptionDicts,
): { filled: string[]; unresolved: string[] } {
  const out = applyParsedToForm(form, fields, dicts)
  writeFormState(host, form)
  return out
}

/* ─────────────────────────── 挂载 ─────────────────────────── */

/**
 * 宿主注入的上下文。
 * ⚠️ `onNavigate` 现在**没有调用方**（保存成功弹窗已按「小提示 + 刷新」的口径整块去掉）。
 * 类型与参数**保留**是为了不扩大改动面，将来要加「去列表」这类跳转时不必再回改签名。
 */
export type OrderAddOrderOptions = {
  onNavigate: (name: string, params?: Record<string, string>) => void
}

/**
 * 取「操作人」+ 权限门所需的 `me`：走我们**自己的**登录态接口，**只打一发**。
 * 操作人、权限节点集、销售客服格要的 `viewer.id` 与显示名都从这一发拿
 * （多读的字段是它的既有载荷，⛔ 不是新请求；「同一事实两套取数」是本仓最忌讳的形状）。
 * 拿不到（401 / 网络失败）⇒ 操作人回退占位符、`me` 回 `undefined`
 * ⇒ 权限门一律按「没有节点」处理（fail-closed，见 `setOrderAddOrderPermissions`）。
 */
export async function fetchViewerAndActor(): Promise<{
  actor: string
  me: NodeViewerInput | undefined
  /** 当前登录人的**完整载荷**（`me` 是它的窄视图；两者同源，取不到时一起是 `undefined`）。 */
  viewer: User | undefined
}> {
  try {
    const viewer = await fetchMe()
    return {
      actor: viewer.display_name || viewer.username || ACTOR_PLACEHOLDER,
      me: viewer,
      viewer,
    }
  } catch {
    return { actor: ACTOR_PLACEHOLDER, me: undefined, viewer: undefined }
  }
}

/** 把证据件里的 `<template>` 用 laytpl 渲染进 `#root`（⛔ 不能省：`<template>` 的内容不参与渲染，省了就是空白页）。 */
export function renderTemplate(root: HTMLElement, laytpl: LayuiModule): boolean {
  const tplEl = query<HTMLTemplateElement>(root, `#${TEMPLATE_ID}`)
  const rootEl = query<HTMLElement>(root, `#${ROOT_ID}`)
  if (!tplEl || !rootEl) return false
  const factory = laytpl as unknown as (
    tpl: string,
    opts?: unknown,
  ) => { render: (data?: unknown) => string }
  // `tagStyle: 'modern'` 保留这个参数：将来若重扒带 `{{= }}` 的证据件，行为与原实现一致。
  rootEl.innerHTML = factory(tplEl.innerHTML, { tagStyle: 'modern' }).render()
  return true
}

type FormSelectsLike = {
  data?: (id: string, type: string, opts: unknown) => void
  value?: (id: string, type?: string, isAppend?: boolean) => unknown
  /**
   * 注册插件事件。**第三参是 `isEnd`**：`true` 走 `endOn`（值**改完**之后），省略走 `on`（改**前**）。
   * 判据出自 `public/vendor/formselects-v4.js` 的 `Select4.prototype.on`。
   */
  on?: (
    id: string,
    cb: (id: string, vals: { value?: unknown }[]) => void,
    isEnd?: boolean,
  ) => void
}

/**
 * 把 xm-select 的选中值同步回**隐藏的** `<select>`（快速读取路径读它）。
 * ⚠️ **必须注册在 `endOn`（第三参 `true`）上**：vendored formselects 先派 `on`（**改前**值）
 * 再改内部状态、最后派 `endOn`（**改后**值）；挂 `on` 会把**上一个** id 写进 `<select>`
 * ⇒ 编辑/录单把来源 A 改成 B 时发出去的是 A（静默写错 FK）。
 * ⚠️ **空集必须写成 `''`**：点「清空」快捷按钮时 `endOn` 拿到的是空数组，跳过不写就会把
 * 上一个 id 留在 `<select>` 里。判据是 `vals.length`，不是真值——`0` 是合法值（E-0021）。
 * 例外：`value(id, ...)` / `data(...)` 这类 **API 写入**不触发钩子 ⇒ 「回填」路径的 `<select>`
 * 由调用方自己写（见 `setOrderSourceSelection`）。
 */
export function bindSelectionSync(
  formSelects: FormSelectsLike | undefined,
  id: string,
  select: () => HTMLSelectElement | null,
  /**
   * 可选的「这是**用户操作**」回调，收到 `endOn` 的**改后**值。
   * 能当「用户操作」用：程序化写入走 `handlerLabel(..., notOn=true)`，钩子不触发
   * ⇒ 回填/复位不会误触发。判据是「空集 ⇒ 用户清空」，所以必须用 `vals.length`（E-0021）。
   */
  onUserChange?: (vals: { value?: unknown }[]) => void,
): void {
  formSelects?.on?.(
    id,
    (_id, vals) => {
      const el = select()
      if (el) el.value = vals.length > 0 ? String(vals[0]?.value ?? '') : ''
      // select 不在也要把「用户操作」传出去：标志判的是用户意图，不依赖 DOM 是否就绪。
      onUserChange?.(vals)
    },
    true,
  )
}

/* ── 权限门（D-0154 的同一原则）──────────────────────────
 * ⛔ **按节点渲染、fail-closed**：节点不给 ⇒ 不渲染那个入口（⛔ 不置灰、不摆着让它 403）；
 * 身份取不到（401 / 网络失败）⇒ 一律按「没有节点」处理。
 * ⚠️ **为什么门必须做在页内**：编辑态是一条**独立 island 页 URL**、不经 `/orders/new` 的路由守卫
 * ⇒ 保存那半的门不能只靠调用方。粘贴解析是**失焦触发、没有按钮** ⇒ 等价物是**不注册那个监听器**
 * 并摘掉「会自动识别」的承诺。
 * ⚠️ **门要按模式取**（`SaveGateMode`）：`create` 是单点 `order.add_edit`、`partial_update` 才是
 * any 元组——共用并集会让「只持 `submit_edit`」的角色在新建态也看到按钮、点下去 403
 * （门比后端**宽**）。⛔ 新建态对正常用户的行为与外观一字不改（验收判据）。
 * ⚠️ **为什么是模块级 `let` + 导出 setter**：`applyAddOrderPermissionGates` 这个**纯 DOM 函数**
 * 被页测试直接驱动，拿不到 mount 的闭包状态。⛔ 业务代码里除 `mountOrderAddOrder` 外不要调它。
 */
let canSaveOrder = false
let canParsePaste = false
let canLoadOrderDetail = false
/**
 * 「接单销售客服」那一格**能不能改**。
 * 与上面三个开关同一个住处（模块级 `let` + 导出 setter）：那一格的渲染与读取发生在被 spec
 * 直接驱动的偏纯函数里，拿不到 mount 的闭包状态。
 * ⛔ **它只对编辑态有意义**（权限点原文限定「编辑订单页面」；新建单没有「现有销售客服」可换）。
 * ⛔ 因此**别**把这个开关直接串到渲染分支上——「新建态无权限也能选」那条用例会红。
 */
let canChangeSalesAgent = false
let canEditSplitPrice = false
let canEditCurrentSplitPrice = false
let initialOrderPrice = ''

/** 编辑态保存的门 = 后端 `partial_update` / `update` 的 any(`order.add_edit`, `order.submit_edit`)。 */
export const SAVE_PERMISSION_CODES = ['order.add_edit', 'order.submit_edit'] as const
/** 新建态保存的门 = 后端 `create` 的**单点** `order.add_edit`（⛔ 不是上面那个并集，见 `SaveGateMode`）。 */
export const CREATE_PERMISSION_CODE = 'order.add_edit'
/** 编辑态**第一发** `GET /orders/<no>/` 的门（后端 `retrieve` = any 详情节点）。 */
export const DETAIL_PERMISSION_CODES = ['order.view_detail', 'order.view_content'] as const
/** 粘贴解析的门（后端 `parse` = 单点 `order.add_edit`，两种模式下同一个端点）。 */
export const PARSE_PERMISSION_CODE = 'order.add_edit'
/**
 * 「接单销售客服」那一格的编辑门。
 * 节点 code 逐字取后端 `apps/rbac/catalog.py`（语义是**放开型**——持它的人才能改这一格，
 * ⛔ 不是"限制型"的反转）。
 */
export const SALES_AGENT_PERMISSION_CODE = 'order.allow_change_sales_agent'

/**
 * 保存那道门**按模式取**——同一个动作在后端**两种模式要求的节点不同**：
 * `'create'` ⇒ 单点 `order.add_edit`；`'edit'` ⇒ any(`order.add_edit`, `order.submit_edit`)。
 * ⛔ **两模式不能共用一个并集**：并集会让「只持 `order.submit_edit`」的角色在新建态也看到按钮，
 * 点下去 403——那是**门比后端宽**（D-0154 的同一形状）。
 */
export type SaveGateMode = 'create' | 'edit'

/** 编辑态缺详情节点时的提示（**不许静默**：那一发请求根本没发出去，用户得知道为什么是空表单）。 */
export const NO_DETAIL_PERMISSION_MESSAGE = '没有查看订单详情的权限'

/** 证据件里「订单内容」label 上那句**自动识别承诺**（缺 `order.add_edit` 时要摘掉）。 */
export const PASTE_HINT_LABEL_SUFFIX = '[会自动识别订单各项内容]'

/** 元组门（any）：与后端 `has_node(*codes)` 同语义。 */
function anyNode(viewer: NodeViewerInput, codes: readonly string[]): boolean {
  return codes.some((code) => canNode(viewer, code))
}

/**
 * 按 `/api/auth/me` 的载荷设定上面**四个**开关（节点 code 见 `apps/rbac/catalog.py`）。
 * `saveMode` 决定保存那道门取哪一组（理由见 `SaveGateMode`，⛔ 不是共用并集）。
 * ⛔ **默认全 false（fail-closed）**：身份取不到时不渲染 / 不发请求。
 */
export function setOrderAddOrderPermissions(
  me: NodeViewerInput | undefined,
  saveMode: SaveGateMode,
): void {
  const viewer: NodeViewerInput = me ?? {}
  canSaveOrder =
    saveMode === 'create'
      ? canNode(viewer, CREATE_PERMISSION_CODE)
      : anyNode(viewer, SAVE_PERMISSION_CODES)
  canParsePaste = canNode(viewer, PARSE_PERMISSION_CODE)
  canLoadOrderDetail = anyNode(viewer, DETAIL_PERMISSION_CODES)
  // 「接单销售客服」那一格的编辑门：**只对编辑态生效**（权限点原文限定在「编辑订单页面」）。
  // 与上面三个门**同源**（同一发 `/api/auth/me`、同一份节点集、同样 fail-closed）。
  canChangeSalesAgent = canNode(viewer, SALES_AGENT_PERMISSION_CODE)
  canEditSplitPrice = canNode(viewer, 'order.submit_payout_price') || canNode(viewer, 'order.edit_payout_price')
}

/**
 * 按上面那三个开关**摘掉证据件里的静态控件**（⛔ 必须在 `root.innerHTML = 证据件` +
 * `renderTemplate` **之后**调：那些元素在内联之前根本不存在）。
 * 保存按钮缺 `canSaveOrder` 时**真摘掉**（`remove()`，不是置灰——D-0154：点了会 403 的入口
 * 就不该出现）；「自动识别」那句承诺缺 `canParsePaste` 时**摘掉**（不注册失焦监听器之后，
 * 那句提示就是**假话**）。⚠️ 只动这两处、**保留 `<textarea>` 本身**（它仍是 `content` 的输入框）；
 * 同区那颗「录单识别设置」链接**不动**（它点了有响亮的「尚未开放」提示，不是静默误导）。
 */
export function applyAddOrderPermissionGates(root: HTMLElement): void {
  if (!canSaveOrder) root.querySelector('button[lay-filter="savebtn"]')?.remove()
  if (!canParsePaste) {
    const box = root.querySelector<HTMLTextAreaElement>('#dlcontent')
    const label = box?.closest('.layui-form-item')?.querySelector('label.layui-form-label')
    if (label) label.textContent = (label.textContent ?? '').replace(PASTE_HINT_LABEL_SUFFIX, '')
    box?.removeAttribute('placeholder')
  }
}

export async function mountOrderAddOrder(
  root: HTMLElement,
  // 下划线前缀 = 本仓库「有意保留、暂未使用」的参数约定（见 `eslint.config.ts` 的同名规则）：
  // 弹窗去掉后 `onNavigate` 没有调用方了，但签名保留（理由见 `OrderAddOrderOptions` 的注释）。
  _options: OrderAddOrderOptions,
): Promise<() => void> {
  const layui: LayuiNamespace = await bootstrapIsland(['form', 'layer', 'laytpl', 'util'])
  const form = layui.form as LayuiModule
  const layer = layui.layer as LayuiModule
  const laytpl = layui.laytpl as LayuiModule
  /**
   * ⚠️ layui 的 `form.render` / `form.on` 是**原型方法**（内部第一步读 `this.config`）：
   * 把方法取出来单独调用会丢 `this`，症状是 `Cannot read properties of undefined (reading 'config')`。
   * 所以一律走**成员表达式调用**（`form[name](…)`），`this` 才绑在 form 上。
   */
  const callForm = (name: string, ...args: unknown[]): unknown =>
    (form[name] as (...a: unknown[]) => unknown)(...args)
  const callLayer = (name: string, ...args: unknown[]): unknown =>
    (layer[name] as (...a: unknown[]) => unknown)(...args)
  const formRender = (t?: string): void => void callForm('render', t)
  const formSelects = (layui as unknown as { formSelects?: FormSelectsLike }).formSelects

  // 编辑态 = 管理订单页的「编辑订单」弹窗把本页装进 iframe 时带的 `order_no`（对标是
  // `AddOrder?orderId=`）。有它 ⇒ 拉详情回填、保存真提交（见 `onSave`）。
  const editOrderNo = editOrderNoFromSearch(window.location.search)

  document.title = editOrderNo === '' ? '录入订单' : '编辑订单'

  // 权限门：**在取证据件之前**取一次 `/api/auth/me`，**一次取数两用**（操作人 + 节点集）。
  // ⛔ fail-closed：取不到 ⇒ `me` 为 `undefined` ⇒ 四个门全关。⛔ 不拦页面（取不到只是少几个入口）。
  // 放在证据件之前是刻意的：门要在标记进 DOM **之前**定下来，避免按钮闪一下才被摘掉。
  const viewerAndActor = await fetchViewerAndActor()
  const viewer = viewerAndActor.viewer
  const canPublishToHall = canNode(viewer ?? {}, 'order.send_to_grab_hall')
  /**
   * 当前登录人的**显示名**（口径与候选行 `name` 同源：昵称优先、空串回落登录账号）。
   * 取不到身份时是空串 ⇒ 与 `currentUserId: null` 一起让那一格**一颗都不勾**（fail-closed）。
   */
  const viewerName = viewer ? viewer.display_name || viewer.username || '' : ''
  // 保存那道门**按模式取**：`order_no` 非空 = 编辑态（`PATCH` = any 元组）、否则新建态
  // （`POST` = 单点 `order.add_edit`）。⛔ 两模式共用并集会让「只持 submit_edit」的角色在新建态
  // 看到按钮、点下去 403（门比后端宽）。见 `SaveGateMode`。
  setOrderAddOrderPermissions(viewerAndActor.me, editOrderNo === '' ? 'create' : 'edit')

  const res = await fetch(`${import.meta.env.BASE_URL}${ARTIFACT_URL}`)
  if (!res.ok) throw new Error(`证据件取不到（HTTP ${res.status}）：${ARTIFACT_URL}`)
  root.innerHTML = await res.text()

  // ① 模板渲染（**不能省**：`<template>` 的内容不参与渲染，省了就是空白页）。
  if (!renderTemplate(root, laytpl)) {
    throw new Error('证据件缺少 #template / #root，无法渲染录入订单表单')
  }
  // 权限门（**必须在模板渲染之后**：那些控件是证据件里的静态 DOM，在内联之前根本不存在）。
  applyAddOrderPermissionGates(root)

  const msg = (text: string): void => islandMsg(layer as unknown as Record<string, unknown>, text)
  const notOpen = (label: string): void => msg(`「${label}」尚未开放`)

  // ② 页签 / 折叠面板（必须在标记注入之后跑，见 `layui/island.ts` 的 `initIslandElements`）。
  initIslandElements(layui)

  // ③ 多选下拉交给对标那份 formselects-v4；然后再让 layui 渲染其余表单控件。
  initFormSelects('select[xm-select]')
  formRender()

  let state: AddOrderForm = emptyAddOrderForm()
  // 操作人来自上面**同一发** `/api/auth/me`（见 `fetchViewerAndActor`）；取不到时是占位符。
  const actor = viewerAndActor.actor
  let games: ConfigRow[] = []
  const serversById = new Map<number, ServerRow>()
  /**
   * 编辑态**进来时**的来源 id（`null` = 这单本来就没有来源）。
   * 只服务一件事：`formToPayload` 把 `null` 当「空值」丢掉 ⇒ 有初始值才知道
   * 「用户是把它清空了」而不是「本来就没有」（后者不该凭空补一个 `null` 键）。
   */
  let initialOrderSource: number | null = null
  /**
   * 「用户**真的**清过来源」。
   * 不能拿 `source === null` 当判据：订单带回来的 id 不在字典里时（被 `page_size` 截断，
   * 或 `fetchOrderSources()` 失败），真插件加不进去 ⇒ 隐藏 select 仍是 `""`、读出来也是 `null`。
   * 此时用户没动过，若按 `source === null` 补键，PATCH 会带上 `order_source: null`
   * ⇒ **来源被静默清空**。只由**用户操作**置位：程序化写入（回填 / 复位的 `value(id, arr, true)`）
   * 不触发钩子 ⇒ 不会误置；空集 ⇒ true，又选回非空 ⇒ false。
   * 与 `initialOrderSource` 一样是**每次 mount 的新闭包**，不跨表单泄漏。
   */
  let sourceClearedByUser = false

  /**
   * 「下拉里现在这个来源**不是客服指定的**」——只可能是两种来路：① 本页给的**默认第一条**；
   * ② 后端**自动判定**回来的。
   * ⛔ **它承重**：读单时**不许**把它当 `order_source` 发出去——发了就等于告诉后端"我知道是哪家店"，
   * 后端的自动判定（**只在没给来源时才走**）会被顶掉 ⇒ **换店铺的单读不出来**。
   * ⚠️ 这是真实踩过的线上回归：第一版把默认第一条当成了"客服选的"，
   * 客服反馈「输单号什么都不出来、自动识别游戏也没生效」（两件事同一个根因）。
   * 置位点一共**六处**（新增写入点时必须同批回查，`AGENTS.md` §5-E）：
   *   1. `setOrderSourceSelection(id, auto)` 里的赋值——**五处调用**都**显式声明**这次写入是
   *      "自动来的"还是"指定来的"（参数⛔ 没有默认值，新增调用点必须自己选）；
   *   2. `bindSelectionSync` 回调里**直接**赋值的那一句（只由真人点选触发）。
   * ⚠️ 第 2 处**不承重**（实测改成恒 `false`，全量一条不红——清空后 `readOrderSource()` 本来就回
   * `null`，读单照样走自动判定）；留着只是让「清空 = 没指定」在状态上也成立，
   * ⛔ 别把它当成承重逻辑（「防御性写法不要写成承重逻辑」）。
   */
  let orderSourceIsAuto = true

  /**
   * 「客服**自己点过**游戏下拉（且此后还没重新录过单）」⇒ 自动切游戏不插手。
   * ⛔ **口径（二次裁定后）**：**每次识别都自动选一次**——单号那条路的两个触发点
   * （① 改来源单号 / ③ 粘贴回填出单号）到来时**先把这个标志清掉**；手改之后**不动**（同一次录入里
   * 再发生读单也不切，直到重新录一单）。
   * ⇒ 语义是「**自最近一次识别以来**，客服手动调过游戏」，⛔ **不是**「整单接管」。
   * ⚠️ **唯一置位点** = `callForm('on','select(selectGame)', …)` 的回调（layui 的 `select` 事件
   * 只在渲染出来的 `dd` 的 click 里派发，程序化写 value + `form.render('select')` **不会**走到它）。
   * 另一个复位点是 `resetAfterSave()`（保存成功 = 新的一单；⚠️ 它**不是承重的**——下一个 ①③ 也会
   * 清掉它）。与 `initialOrderSource` 一样是**每次 mount 的新闭包**，不跨表单泄漏。
   */
  let userPickedGame = false

  /**
   * ⭐ 2026-11-10 客户线：最近一次「读平台订单」暂存的买家身份（`buyer_eid`，隐藏透传，页面无控件）。
   * **每次读单先清**（换单号不拿上一单的身份），读成功且响应带非空 `buyer_eid` 才落（阿奇索腿无此键）；
   * `resetAfterSave()` 再清一次。新建保存随载荷回传（后端建户并连单）；编辑态不发。判据 = 两份录单 spec。
   */
  let pendingBuyer: { eid: string; nick: string } | null = null

  /**
   * 当前来源字典（`{id, name}`）——**运行期可变**。
   * ⚠️ 为什么不是「首屏取一次就完事」：自动判定命中一家**还没建过来源的店**时会**当场建一个**
   * ⇒ 那个来源**不在**首屏那份字典里。回填下拉前必须把它补进来（`ensureOrderSourceOption`），
   * 否则真插件只收录 `dd[lay-value]` 命得中的 id、隐藏 `<select>` 在没有对应 `<option>` 时
   * `value = id` 静默落成 `''` ⇒ 订单被**静默存成"没有来源"**。
   */
  let sourceOptions: { id: number; name: string }[] = []

  const fsData = (id: string, arr: { name: string; value: number | string }[]): void =>
    formSelects?.data?.(id, 'local', { arr })
  const fsValue = (id: string): unknown => formSelects?.value?.(id)

  const applyLabels = (orderType: string): void => {
    for (const [sel, text] of Object.entries(labelOverrides(orderType))) {
      const el = query<HTMLElement>(root, sel)
      if (el) el.textContent = text
    }
  }
  applyLabels('0')
  // 游戏名不含「魔兽世界」时隐藏阵营门派那一格。
  const groupDiv = query<HTMLElement>(root, '.gamegroupsdiv')
  if (groupDiv) groupDiv.style.display = 'none'
  /* ── 快速指派 ────────────────────────────────────────────────
   * 三件套 = **选人**（`.KSPD` → `.kszpDiv` 层）+ **给打手发单价**（`#throwFee` → `hall_price`）
   * + **默认指派此人**（`defAss` → 我自己档案上的 `default_assignee`）。
   * ⚠️ 标记一律用证据件里**既有**的那些（`content: $('.kszpDiv')`，⛔ 不在 TS 里重画标记）。
   * ⚠️ **只在新建态接**：编辑态的保存是 `PATCH`（后端不收 `assignee` / `remember_assignee`）
   * ⇒ 编辑态接上会是「选了人、保存后什么都没发生」的假功能；编辑态把 `.KSPD` 登记成
   * `data-notopen`（点了照「尚未开放」提示，⛔ 不置灰）。
   */
  const throwFeeLayer = query<HTMLElement>(root, '.throwFeelayer')
  const throwFeeInput = query<HTMLInputElement>(root, '#throwFee')
  const saveAssBtn = query<HTMLElement>(root, '.saveassbtn')
  const defAssBox = query<HTMLInputElement>(root, 'input[name=defAss]')
  /**
   * **拆单**：录单页那颗勾选框「按代练项目拆分订单」；勾上 ⇒ 提交带
   * `split: true`，切分由**后端**做（口径只有一处）。
   * ⚠️ **只在新建态进载荷**：编辑态走 `PATCH`，拆已有单是详情页那条路——在编辑态收它会是
   * 「勾了、保存后什么都没发生」。⚠️ 它是**真 checkbox**：`checked` 就是它的全部状态；
   * ⚠️ 复位走**既有**的 `form.reset()`（复位回「不勾」，下一单不会静默继承拆单，有页测试钉住）。
   */
  const chaifenBox = query<HTMLInputElement>(root, 'input[name=chaifen]')
  const kspdButton = query<HTMLElement>(root, '.KSPD')
  const kszpDiv = query<HTMLElement>(root, '.kszpDiv')
  const kszpList = query<HTMLElement>(root, '.kszp')
  const kszpSearchInput = query<HTMLInputElement>(root, '#seachUserKey1')
  /** layui 自己带的 jQuery：`layer` 的 `open`，它的 `content` **必须**传 jQuery 对象（传裸元素会抛）。 */
  const $ = (layui as unknown as { $: (selector: string) => unknown }).$
  // `isPc` 只看 `layui.device()` 认出的 android / ios；它只决定弹层的 `area`。
  const device = (layui as unknown as { device: () => { android?: boolean; ios?: boolean } }).device()
  const isPc = !(device.android || device.ios)
  /**
   * 当前选中的接手人（`null` = 暂不指派）。单选档下对标的两个变量在这里合成一个值。
   * ⚠️ 存**整行**（不只是 id/name）：算价要读他的提成两列（见 `assigneeThrowFee`）。
   */
  let selectedAssignee: AssigneeOption | null = null
  /** 候选名单——算价与「那个人还在不在名单里」都读它。 */
  let candidates: AssigneeOption[] = []
  /** 服务端记着的默认接手人（`0` = 没设）。**决定取消勾选时该不该发清除**（见 `rememberAssigneeValue`）。 */
  let initialDefaultAssigneeId = 0
  /**
   * 服务端记着的档位是不是「内部抢单」（后端键 `defAssIsHall`）。真 ⇒ 首屏就进「内部抢单」档、
   * 这颗勾选框**勾着**，且「有没有东西可清」的判据也算它一份（见 `rememberAssigneeValue`）。
   * ⚠️ 与 `initialDefaultAssigneeId > 0` **互斥** ⇒ 读面按 **hall 优先**，见 `loadCandidates`。
   */
  let initialDefAssIsHall = false
  /**
   * 服务端默认档位**有没有已经复原过**。
   * 判据：**只有第一次成功拉到名单的那一拉**才把服务端事实写进界面；之后的重拉（搜索 / 重置 /
   * 重开层时清搜索词那一拉）**只重画名单**。
   * ⚠️ 为什么：那些重拉不是「用户重新打开这一页」。用户**主动取消勾选**之后再点「搜索」，
   * 把勾按服务端事实勾回去就是「用户明确说了不要、系统又改回去」（静默改写）。
   * ⚠️ **有意偏离对标**（对标的回调每次都复原）：代价 = 本页生命周期内用户自己造成的偏差会**保留**。
   * ⛔ 别把它读成「永不复原」：首屏那一次照旧；`resetAfterSave()` 是**另一条**路，不受本标志管。
   */
  let defAssPreferenceSynced = false
  /** 弹层实例号（选中一项后要关层）。 */
  let kszpLayerIndex: number | undefined
  /** 「发单价」那句 tip 的实例号。 */
  let throwFeeTipsLayer: number | undefined

  /**
   * 当前档位（**三态，唯一真源**）：`'person'` 指派给某个人 · `'hall'` 内部抢单 · `'none'` 暂不指派。
   * ⚠️ 不让「谁被选中」从 `.kszp` 的 DOM 里现读：名单会被**搜索 / 重置重画**，弹层一关 DOM 就不在
   * ⇒ 只记在 DOM 上，提交时读回来要经过脆链。⚠️ 与 `selectedAssignee` **互斥**、且是它的**上位**
   * （`'hall'` 时 `selectedAssignee` 必为 `null`，`'person'` 时必非空，见 `applyAssigneeSelection`）。
   */
  let toGrabHall = false

  /**
   * 选中 / 取消选中之后把可见状态刷成对标那一套。
   * 三态各一支（判据顺序 = 内部抢单 → 指派某人 → 暂不指派）：
   *  - **内部抢单** ⇒ 露价框 + 焦点 + hall tip + `.KSPD` 文案「内部抢单」；
   *    ⛔ **不重算、不清零**价框；
   *  - **某人** ⇒ 按他的提成重算价；
   *  - **暂不指派** ⇒ 反向复原、价写 `'0'`。
   * `focusIt` = 这一次要不要 `focus()` + 弹价格 tip。**只有用户点选时才为真**：
   * 对标靠顺序耦合区分（tip 调用排在赋值之前 ⇒ 首屏带出默认接手人时 tip 读到空数组直接 return），
   * 我们用**显式参数**表达同一结果，⛔ 不照抄那处顺序耦合。
   * `clearDefAss` = 这一次要不要把「默认指派此人」**取消勾选**。只有**用户选「暂不指派」**那一次
   * 为真；首屏带出、搜索重画、保存后复位这几条**都不许**碰它——否则「服务端记着的那个人不在
   * 候选名单里」这一档会在复位时被静默取消勾选，提交时就真的把用户的偏好清掉了。
   * 内部抢单那一支显示这颗勾选框，不取消勾选、不隐藏（那一档提交要发 `remember_assignee`）。
   */
  function applyAssigneeSelection(focusIt = false, clearDefAss = false): void {
    if (toGrabHall) {
      // 内部抢单。**只有「选中即关层」那一次带 `focusIt`**。
      if (throwFeeLayer) throwFeeLayer.style.display = ''
      if (kspdButton) {
        kspdButton.textContent = HALL_ASSIGNEE_LABEL
        kspdButton.classList.add('KSPDC')
      }
      // 内部抢单也显示这颗框。只 show()，不改勾选：rememberAssigneeValue 读的就是它。
      if (saveAssBtn) saveAssBtn.style.display = ''
      if (focusIt) {
        throwFeeInput?.focus()
        throwFeeTipsLayer = (callLayer('tips', THROW_FEE_TIP_HALL, throwFeeInput, {
          tips: [3, '#2F4056'],
          time: 9000,
          tipsMore: false,
        }) as unknown) as number
      }
    } else if (selectedAssignee !== null) {
      // 先清掉用户可能已填的价，再按这个人的提成算一次。
      if (throwFeeInput) {
        throwFeeInput.value = assigneeThrowFee(
          {
            mode: selectedAssignee.commission_mode,
            value: selectedAssignee.commission_value,
          },
          inputValue(root, 'price'),
        )
      }
      if (throwFeeLayer) throwFeeLayer.style.display = ''
      // 编辑态**不显示**「默认指派此人」（本页只在新建态走到这一支）。
      if (saveAssBtn) saveAssBtn.style.display = ''
      if (kspdButton) {
        kspdButton.textContent = selectedAssignee.name
        kspdButton.classList.add('KSPDC')
      }
      if (focusIt) {
        throwFeeInput?.focus()
        throwFeeTipsLayer = (callLayer('tips', THROW_FEE_TIP, throwFeeInput, {
          tips: [3, '#2F4056'],
          time: 9000,
          tipsMore: false,
        }) as unknown) as number
      }
    } else {
      // 「暂不指派」的反向复原。
      if (throwFeeLayer) throwFeeLayer.style.display = 'none'
      if (throwFeeInput) throwFeeInput.value = '0'
      if (saveAssBtn) saveAssBtn.style.display = 'none'
      if (kspdButton) {
        kspdButton.textContent = '暂不指派'
        kspdButton.classList.remove('KSPDC')
      }
      if (clearDefAss && defAssBox) defAssBox.checked = false
      if (throwFeeTipsLayer !== undefined) {
        callLayer('close', throwFeeTipsLayer)
        throwFeeTipsLayer = undefined
      }
    }
    // `.checked` 是直接改 DOM 的，不 `formRender()` 则 layui 的单选/复选外观不会跟着变。
    formRender()
  }

  /**
   * 把候选名单灌进 `.kszp`。
   * 载荷逐条照对标：每颗都是 `div.layui-inline[style="width:195px"]` 包一颗
   * `input[type=radio][name=recUser][lay-skin=primary][lay-filter=userlist]`。
   * 持大厅发单权限时，在「暂不指派」和候选名单之间显示「内部抢单」。
   * ⚠️ 它的 `value` 是语义值 **`hall`**，⛔ 不是对标的魔法值 `999999`——本页**不把 `recUser`
   * 外发给后端**，这个值是**纯前端**的档位标记，抄 `999999` 只会让人以为后端认这个数。
   * ⚠️ `value=0`（「暂不指派」）**必须在**列表里（每轮重建后的第一颗）。
   */
  function renderCandidates(rows: AssigneeOption[]): void {
    if (!kszpList) return
    const options = rows
      .map(
        (row) =>
          '<div class="layui-inline" style="width:195px">' +
          `<input type="radio" name="recUser" lay-skin="primary" title="${escapeHtml(row.name)}"` +
          ` value="${row.id}" lay-filter="userlist"` +
          `${!toGrabHall && selectedAssignee?.id === row.id ? ' checked="checked"' : ''}></div>`,
      )
      .join('')
    // ⚠️ 名字来自 API（用户输入）⇒ 走 `escapeHtml`（与来源下拉同一条纪律）。
    // ⚠️ `checked` 跟着**当前档位**走（三态互斥）：搜索/重置重画名单时当前选中不能被抹掉，
    //    首屏带出的默认接手人也要在名单里显示成选中状态。
    kszpList.innerHTML =
      '<div class="layui-inline" style="width:195px">' +
      '<input type="radio" name="recUser" lay-skin="primary" title="暂不指派" value="0"' +
      ` lay-filter="userlist"${!toGrabHall && selectedAssignee === null ? ' checked="checked"' : ''}></div>` +
      (canPublishToHall
        ? '<div class="layui-inline" style="width:195px">' +
          '<input type="radio" name="recUser" lay-skin="primary" title="内部抢单"' +
          ` value="${HALL_RADIO_VALUE}" lay-filter="userlist"${toGrabHall ? ' checked="checked"' : ''}></div>`
        : '') +
      options
  }

  /**
   * 拉一次候选名单。
   * `key` = 搜索框的值。失败**如实提示、不静默降级**：静默失败会让客服以为
   * 「可以指派的人一个都没有」。
   * ⚠️ 它同时是「首屏」与「搜索 / 重置重拉」两条路；服务端默认档位的**复原只在第一次成功的那一拉**
   * （`defAssPreferenceSynced`），之后的重拉只重画名单。
   */
  async function loadCandidates(key?: string): Promise<void> {
    try {
      const page = await fetchAssigneeOptions({
        limit: ASSIGNEE_OPTIONS_LIMIT,
        ...(key === undefined || key === '' ? {} : { key }),
      })
      candidates = page.items
      // 被截断 ⇒ 名单外的**选不到**，必须把这件事说出来（与筛选项两份名单同口径）。
      if (page.truncated) {
        msg(`可指派接手人超过 ${ASSIGNEE_OPTIONS_LIMIT} 位，已截断：可用搜索缩小范围`)
      }
      initialDefaultAssigneeId = Number(page.defAssRecManUid ?? 0)
      // 服务端记着的档位：真 ⇒ 默认抛内部抢单。
      // ⚠️ 判据写成 `=== true`（不是真值判）：这个键在旧后端上**可能整个缺失** ⇒ 落到 `false`，
      //    与「没有这一档」同义，⛔ 不会把 `undefined` 当 `true`。
      initialDefAssIsHall = canPublishToHall && page.defAssIsHall === true
      // 默认接手人有值 ⇒ 勾上「默认指派此人」，并**按他复原选中状态**。
      // ⚠️ 复原**不带焦点**——首屏不该把光标抢进价格框。
      // ⚠️ 顺序：先定 `selectedAssignee` 再 `renderCandidates`——`checked` 跟着它走。
      //
      // 「默认档位是内部抢单」那一支与上面那一支**同一形状**（勾选框勾着 + 复原档位，
      // 且**不带焦点**、**不弹 tip**）。
      // ⚠️ 两支**互斥**（后端那两列不许同时为真）⇒ 按 **hall 优先**：万一两列同时为真，
      //    hall 是更具体的档位，而 `selectedAssignee = null` 也保证不会同时带上 `assignee`。
      // ⛔ **复原只在首屏那一次**：标志只在成功拉到名单的那一支置真（失败那一拉不算「已经复原过」）；
      //    之后的重拉**只重画名单**：⛔ 不碰 `defAssBox.checked`、⛔ 不碰 `toGrabHall`、
      //    ⛔ 不碰 `selectedAssignee`（那三个此刻是**用户的选择**）；`resetAfterSave()` 是另一条路。
      if (!defAssPreferenceSynced) {
        defAssPreferenceSynced = true
        if (initialDefAssIsHall) {
          toGrabHall = true
          if (defAssBox) defAssBox.checked = true
          selectedAssignee = null
        } else if (initialDefaultAssigneeId > 0) {
          if (defAssBox) defAssBox.checked = true
          // 那个默认接手人**不在名单里**（已被删 / 停用 / 被 `limit` 截断）⇒ **不假装选中**：
          // 按钮仍是「暂不指派」，勾选框按服务端事实保持勾着；提交时按 `rememberAssigneeValue`
          // 的判据**不动**那个偏好（⛔ 不悄悄清掉）。
          selectedAssignee = candidates.find((row) => row.id === initialDefaultAssigneeId) ?? null
        }
      }
      renderCandidates(candidates)
      applyAssigneeSelection()
    } catch (e) {
      msg(e instanceof Error ? e.message : '可指派接手人加载失败')
    }
  }

  /** 点 `.KSPD` ⇒ 开层（`assMode==1` 的 iframe 那一支不做，本仓零取证件）。 */
  function openKszpLayer(): void {
    if (!kszpDiv) return
    kszpLayerIndex = (callLayer('open', {
      type: 1,
      title: KSZP_LAYER_TITLE,
      shade: [0.3, '#333'],
      shadeClose: true,
      // 桌面 `60%` / 移动 `95%`（`isPc` 判据见 `layui.device()`）。
      area: ['85%', isPc ? '60%' : '95%'],
      // 内容 = 证据件里**既有**的 `.kszpDiv`（⛔ 不在 TS 里重画标记）。
      content: $('.kszpDiv'),
      // cancel / end 两处都把容器藏回去（层关了 layui 会把它放回原位）。
      cancel: () => {
        if (kszpDiv) kszpDiv.style.display = 'none'
      },
      end: () => {
        if (kszpDiv) kszpDiv.style.display = 'none'
        // 层关了就复位实例号 ⇒ 再点 `.KSPD` 会开一个**新**层而不是复用一个已经销毁的 index。
        kszpLayerIndex = undefined
      },
    }) as unknown) as number
  }

  if (editOrderNo === '') {
    // 首屏：「发单价」那一格按加载语义置成隐藏——由 `applyAssigneeSelection()` 一处管
    //（选中/取消两条路都会调它）。
    applyAssigneeSelection()
    if (kspdButton) {
      kspdButton.addEventListener('click', () => {
        // 开着搜索词时，重新开层前清掉它并重拉一次名单。
        if (kszpSearchInput && kszpSearchInput.value !== '') {
          kszpSearchInput.value = ''
          void loadCandidates()
        }
        openKszpLayer()
      })
    }
    // 搜索 / 重置：值作为 `assignee-options` 的 `key` 参数。
    query<HTMLElement>(root, '.seachBtn')?.addEventListener('click', () => {
      void loadCandidates(kszpSearchInput?.value ?? '')
    })
    query<HTMLElement>(root, '.resetSeachBtn')?.addEventListener('click', () => {
      if (kszpSearchInput) kszpSearchInput.value = ''
      void loadCandidates()
    })
    // 选中一颗 radio ⇒ **立即关层**（⛔ 不加「确定」按钮）。
    callForm('on', 'radio(userlist)', (obj: { value?: string; elem?: HTMLInputElement }) => {
      if (kszpLayerIndex !== undefined) {
        callLayer('close', kszpLayerIndex)
        kszpLayerIndex = undefined
      }
      // 第三档「内部抢单」。⚠️ 它**不**塞进下面「选人」的分支里：那一支要读提成两列算价，
      //    而内部抢单**没有人**可读。两档共用同一个关层与同一个 `formRender()`。
      if (obj.value === HALL_RADIO_VALUE) {
        if (!canPublishToHall) return
        toGrabHall = true
        // 档位互斥：内部抢单时**没有人**被选中（提交时也就不会带 `assignee`）。
        selectedAssignee = null
        // ⛔ 不重算、不清零价框。
        applyAssigneeSelection(true)
        return
      }
      // 切回「暂不指派」或改选某人 ⇒ **清掉内部抢单标记**（⛔ 不清就会「粘住」：下一单
      // 明明选了人、载荷里却还带着 `throw_to_hall`——那一发会被后端拒绝）。
      toGrabHall = false
      const id = toId(obj.value ?? '')
      if (id === null || id === 0) {
        selectedAssignee = null
        // 用户选「暂不指派」⇒ 同时取消「默认指派此人」勾选。
        applyAssigneeSelection(false, true)
        return
      }
      // ⚠️ 名字以**名单里的那一行**为准（`hit`）；找不到时（理论上不可达——radio 就是名单渲染的）
      //    只放一个最小行，⛔ 不假装知道他的 `role` / `tag` / `remark`。
      selectedAssignee = candidates.find((row) => row.id === id) ?? { id, name: '', role: null, tag: '', remark: '' }
      // 用户点选 ⇒ `focusIt = true`。
      applyAssigneeSelection(true)
    })
  } else if (kspdButton) {
    // 编辑态：这一支不接（见本段开头）。用既有的「尚未开放」机制**响亮**说出来，⛔ 不静默无效。
    kspdButton.setAttribute('data-notopen', '编辑订单时的快速指派')
    applyAssigneeSelection()
  }

  /* ── 配置级联：游戏 → 区服 / 项目分类 → 常用项目 ── */

  /**
   * 把区服写进**隐藏的** `<select id="gameArea">`。
   * 为什么要自己写：formSelects 把原 `<select>` 藏起来、另渲染一套视图，而 `readFormState`
   * 的默认读取路径读的是那个 `<select>` 的 `value`；`select.value = id` 在**没有对应
   * `<option>`** 时会**静默**落成 `''`（实测），于是「选了区服却提交不上去」。
   */
  function setServerSelectOptions(servers: { id: number; name: string }[]): void {
    const select = query<HTMLSelectElement>(root, '#gameArea')
    if (!select) return
    // ⚠️ 区服名来自 API（用户输入）⇒ 进 `innerHTML` 前过 `escapeHtml`。
    select.innerHTML = servers
      .map((s) => `<option value="${s.id}">${escapeHtml(s.name)}</option>`)
      .join('')
  }

  /** 让 xm-select 与隐藏 `<select>` 同时选中某个区服（粘贴解析命中区服名时用）。 */
  function setServerSelection(id: number): void {
    ;(formSelects?.value as undefined | ((i: string, v: unknown[], t: boolean) => void))?.(
      'gameArea',
      [id],
      true,
    )
    const select = query<HTMLSelectElement>(root, '#gameArea')
    if (select) select.value = String(id)
  }

  /**
   * 把来源字典灌进**隐藏的** `<select id="orderSoures">`（与 `setServerSelectOptions` 同形的理由）：
   * `select.value = id` 在**没有对应 `<option>`** 时会**静默**落成 `''`。
   * 灌完**显式清一次选中**：浏览器对「有 option 但没写 `selected`」的 select 默认选中第一条，
   * 不清的话「没选来源」会被读成「选了第一条来源」——每单都静默带上它。
   */
  function setOrderSourceSelectOptions(sources: { id: number; name: string }[]): void {
    const select = query<HTMLSelectElement>(root, '#orderSoures')
    if (!select) return
    // 安全：`s.name` 来自 API（用户输入）⇒ 必须转义后再进 `innerHTML`。
    // ⚠️ 这里**不能**只靠「标签会被 `<select>` 的解析器丢掉」来兜底：`<option>` / `</option>`
    // 这类 token 在 in-select 模式下**会真的改变选项结构**（探针实测）。转义才是唯一正确的修法
    // （判别测试见 `order-addorder.spec.ts` 的 I-4 用例）。
    select.innerHTML = sources
      .map((s) => `<option value="${s.id}">${escapeHtml(s.name)}</option>`)
      .join('')
    select.value = ''
  }

  /**
   * 让 xm-select 与隐藏 `<select>` 同时选中某个来源。
   * `auto` = **这次写入的来源是不是"自动来的"**（默认第一条 / 后端自动判定 ⇒ `true`；
   * 客服点的 / 粘贴文本里解析出来的 / 编辑态订单自带的 ⇒ `false`）。它只喂 `orderSourceIsAuto`
   * 那一个判据，⛔ 不参与显示。参数**故意没有默认值**：让新增调用点必须显式表态。
   */
  function setOrderSourceSelection(id: number, auto: boolean): void {
    ;(formSelects?.value as undefined | ((i: string, v: unknown[], t: boolean) => void))?.(
      'orderSoures',
      [id],
      true,
    )
    const select = query<HTMLSelectElement>(root, '#orderSoures')
    if (select) select.value = String(id)
    orderSourceIsAuto = auto
  }

  /** 把当前的 `sourceOptions` 灌进两条读取路径（xm-select 的本地字典 + 隐藏 `<select>`）。 */
  function applyOrderSourceOptions(): void {
    fsData(
      'orderSoures',
      sourceOptions.map((s) => ({ name: s.name, value: s.id })),
    )
    // ⚠️ `setOrderSourceSelectOptions` 会把选中**清掉**（浏览器默认选中第一条）⇒
    //    调用方必须**在它之后**再 `setOrderSourceSelection(id)`。
    setOrderSourceSelectOptions(sourceOptions)
  }

  /**
   * 保证来源字典里有这一项（后端自动判定可能**刚建了一个来源**）。
   * 已经有的不动（`some` 判据用 id，不用名字：同名不共用是后端的约定）。
   */
  function ensureOrderSourceOption(id: number, name: string): void {
    if (sourceOptions.some((s) => s.id === id)) return
    sourceOptions = [...sourceOptions, { id, name: name || `#${id}` }]
    applyOrderSourceOptions()
  }

  async function onGameChange(gameId: number): Promise<void> {
    state.game = gameId
    state.server = null
    state.category = null
    state.project = null
    serversById.clear()
    fsData('gameArea', [])
    setServerSelectOptions([])
    const div = query<HTMLElement>(root, '.orderTypeDiv')
    if (div) div.innerHTML = ''
    const list = query<HTMLElement>(root, '.dllistdiv')
    if (list) list.innerHTML = ''
    const area = query<HTMLElement>(root, '.orderTypeArea')
    if (area) area.style.display = 'none'

    let servers: ServerRow[] = []
    let categories: CategoryRow[] = []
    try {
      ;[servers, categories] = await Promise.all([fetchServers(gameId), fetchCategories(gameId)])
    } catch (e) {
      msg(e instanceof Error ? e.message : '配置加载失败')
      return
    }
    // 连点两次游戏时，先发的慢响应不能覆盖后发的（与 EP `onGameChange` 同一道守卫）。
    if (state.game !== gameId) return
    for (const s of servers) serversById.set(s.id, s)
    fsData(
      'gameArea',
      servers.map((s) => ({ name: s.name, value: s.id })),
    )
    // 隐藏的 `<select id="gameArea">` 也要有同样的 `<option>`：
    // `select.value = id` 在**没有对应 option 时静默变成 `''`**，而 `readFormState` 默认就是读它。
    setServerSelectOptions(servers)
    if (div && categories.length > 0) {
      div.innerHTML = categories
        .map(
          (c) =>
            `<input type="radio" lay-filter="projectType" name="projectType" title="${escapeHtml(c.name)}" value="${c.id}">`,
        )
        .join('')
      formRender('radio')
      if (area) area.style.display = ''
    }
  }

  /**
   * 按内容里的游戏名**自动切游戏**（决策 D-0178）。
   * 两条接入点共用它：读平台订单回填后的**商品标题**（接入点 A）
   * 与粘贴整段原文（接入点 B，走「注入 `game_name`」而不是调本函数——见 `onPasteBlur`）。
   *
   * @param text  要比对的文本（读单 = 商品标题；粘贴 = 整段粘贴原文）
   * @param label 提示里的来源名：`商品内容` / `粘贴内容`
   */
  async function autoSelectGameByContent(text: string, label: string): Promise<void> {
    // 规则 1：客服接管了 ⇒ **直接 return**（连比对都不做，不是「比完再决定切不切」）。
    if (userPickedGame) return
    const r = detectGameFromText(text, games)
    // 规则 2a：内容里没提游戏是常态 ⇒ 静默。
    if (r.kind === 'none') return
    // 规则 2b：平手**不猜**——切错游戏比不切更糟（客服多半不会发现）。
    if (r.kind === 'ambiguous') {
      // ⚠️ 游戏名是库里的自由文本，`msg` → `layer.msg` 按 HTML 渲染 ⇒ 逐个过 `escapeHtml`（E-0303）。
      msg(`${label}里出现了多款游戏（${r.names.map((n) => escapeHtml(n)).join('、')}），请手动选择游戏`)
      return
    }
    // 规则 3：已经是它 ⇒ 不重建级联、不提示（否则每次读单都白拉一发区服 + 分类）。
    if (state.game === r.game.id) return
    /**
     * 规则 4：**先把 `#gameName` 的选中值写过去**，⛔ 这一步不能省——`onGameChange` 自己不写它，
     * 少了它会出现「界面显示旧游戏 / 级联是新游戏的区服 / 提交的是旧游戏」三件不一致。
     */
    const select = query<HTMLSelectElement>(root, '#gameName')
    if (select) select.value = String(r.game.id)
    formRender('select')
    /**
     * 规则 5：提示发在 `await` **之前**——这次切换确实清掉了客服填的区服 / 分类，**不静默**。
     * ⚠️ 顺序是承重的：`onGameChange` 失败时它自己的「配置加载失败」在**后**发、覆盖掉本句，
     * 那才是对的顺序（先报「已切」再报「切不动」会让人以为切成功了）。
     */
    const reset = state.server !== null || state.category !== null
    msg(
      `已按${label}自动选择游戏「${escapeHtml(r.game.name)}」` +
        (reset ? '（区服/分类已重置，请重新选择）' : ''),
    )
    await onGameChange(r.game.id)
  }

  async function onCategoryChange(categoryId: number): Promise<void> {
    state.category = categoryId
    state.project = null
    const list = query<HTMLElement>(root, '.dllistdiv')
    if (!list) return
    list.innerHTML = ''
    let projects: ProjectRow[] = []
    try {
      projects = await fetchProjects(categoryId)
    } catch (e) {
      msg(e instanceof Error ? e.message : '项目列表加载失败')
      return
    }
    // 与 `onGameChange` 逐字同一谓词：响应回来时选中的**还是**这个分类才落盘。
    if (state.category !== categoryId) return
    list.innerHTML = projects
      .map(
        (p) =>
          `<input type="checkbox" name="dltype" value="${p.id}" data-name="${escapeHtml(p.name)}" lay-skin="primary" title="${escapeHtml(p.name)}" lay-filter="dltype">`,
      )
      .join('')
    formRender('checkbox')
  }

  /**
   * 对标 `form.on('checkbox(dltype)')` 的算法：勾上就把项目名按 `/` 拼进 `#project`，
   * 取消就把名字（连同可能的前导 `/`）删掉。差别只有一处：我们的 `value` 要进 FK（项目 id），
   * 名字另放在 `data-name` 上——所以这里读 `data-name`，文本结果与对标一致。
   */
  function toggleProjectText(el: HTMLInputElement): void {
    const name = el.dataset.name ?? ''
    const input = query<HTMLInputElement>(root, `#${PROJECT_TEXT_NAME}`)
    if (!input) return
    // 拼/删的规则只有一处实现（`views/orders/projectText.ts`）：取消勾选按 `/` 分词做整词删，
    // ⛔ 不再是子串 replace（前缀同名的项目会被误删）。
    input.value = toggleProjectName(input.value, name, el.checked)
  }

  /* ── 粘贴解析（我们后端的既有能力） ── */

  async function onPasteBlur(): Promise<void> {
    const box = query<HTMLTextAreaElement>(root, '#dlcontent')
    if (!box) return
    const text = box.value
    state.content = text
    if (text.trim() === '') return
    let result: ParseResult
    try {
      result = await parsePastedText(text)
    } catch (e) {
      // 「解析不出来」不是错误，是常态；只有传输层 / 信封层失败才提示。
      msg(e instanceof Error ? e.message : '识别失败')
      return
    }
    const gameBefore = state.game
    /**
     * 接入点 B：解析结果里**没有**显式「游戏：X」时，按**整段粘贴原文**认一次游戏。
     * 命中 ⇒ 把 `result.fields.game_name` **注入**（值就是字典里的原名 ⇒ 精确同名查找必然命中）：
     * 切游戏、区服重解析与「已识别：…」提示**全部沿用既有路径**，⛔ 不新增第二条切换逻辑。
     * ⚠️ 显式「游戏：X」的既有规则**优先级更高、原样不动**；客服已接管（`userPickedGame`）
     * ⇒ 连比对都不做。多款 ⇒ 把那句**拼进既有的 `parts`**，⛔ 不能单独发提示：
     * 后面那次提示会把它冲掉（这就是它为什么先存在一个局部变量里）。
     */
    let ambiguousGameMessage = ''
    if (result.fields.game_name === undefined && !userPickedGame) {
      const r = detectGameFromText(text, games)
      if (r.kind === 'hit') {
        result.fields.game_name = r.game.name
      } else if (r.kind === 'ambiguous') {
        // ⚠️ 游戏名来自库，这句话最终走 HTML 渲染（E-0303）⇒ 逐个过 `escapeHtml`。
        ambiguousGameMessage = `粘贴内容里出现了多款游戏（${r.names.map((n) => escapeHtml(n)).join('、')}），请手动选择游戏`
      }
    }
    // 来源字典是**运行期**灌进下拉的 ⇒ 必须把当前这份传进去，否则 `source` 永远进 `unresolved`。
    const sourceBefore = state.order_source
    let { filled, unresolved } = applyPasteToDom(root, state, result.fields, {
      games,
      servers: Array.from(serversById.values()),
      categories: [],
      projects: [],
      orderSources: sourceOptions,
    })
    // `writeFormState` 覆盖不到运行期注入的下拉 ⇒ 命中来源后必须**显式同步一次**
    // xm-select + 隐藏 `<select>`，否则界面看不见、`readFormState` 也读不回（`order_source` 静默丢）。
    if (state.order_source !== sourceBefore && state.order_source !== null) {
      // `false`：这个来源是**粘贴文本里写明的** ⇒ 算"指定"。
      setOrderSourceSelection(state.order_source, false)
    }
    // 触发点 ③：**粘贴解析回填出来源单号**之后读一次。
    // ⚠️ 判据是 `filled` 里有 `source_order_no`（= 这次粘贴确实写了这个字段），不是「粘贴框里有内容」
    // ——后者每粘一次都会白跑一趟上游，而读单按次计费（spec §3.5 / D4）。
    // ⚠️ 这条触发点真的可达：`ParsedFields` 有 `source_order_no` 键、`pasteFillTargets` 把它路由进
    //   `scalars`、后端 `intake_rules.py` 也有「来源单号」识别项。用例见 `order-addorder.spec.ts`。
    if (filled.includes('source_order_no')) void readPlatformOrderIntoForm()
    const pastedServer = result.fields.server_name
    if (state.game !== gameBefore && state.game !== null) {
      /**
       * 粘贴**同时改了游戏**：上面那次区服解析用的是**上一个游戏**的列表，必须作废并重来
       * （不重来的话 `onGameChange` 会把 `state.server` 清掉，而「旧列表里查到了」又不留提示
       * ⇒ 区服静默丢失、订单不带区服就存了）。
       * EP 那边要一个延迟重试位；这里 `onGameChange` 是 `await` 的，回来时新区服列表已在
       * `serversById` 里，当场重解析即可，**不需要**那个 pending 位。
       */
      await onGameChange(state.game)
      filled = filled.filter((f) => f !== 'server')
      unresolved = unresolved.filter((u) => u !== `区服「${pastedServer}」`)
      if (pastedServer !== undefined) {
        const hit = Array.from(serversById.values()).find((s) => s.name === pastedServer)
        if (hit) {
          state.server = hit.id
          filled = [...filled, 'server']
          setServerSelection(hit.id)
        } else {
          // 名字确实不在新游戏的列表里 → 提示留着，让客服手动选（**不许**静默）
          unresolved = [...unresolved, `区服「${pastedServer}」`]
        }
      }
    } else if (state.server !== null) {
      // 区服是 xm-select：解析出的 id 要同时落到 formSelects 与隐藏 `<select>` 上，
      // 否则既看不见（UI 无标签）又提交不上去（`readFormState` 读的就是那个隐藏 select）。
      setServerSelection(state.server)
    }
    const parts: string[] = []
    // 多款游戏那句**排在最前**：它是要客服动手的一句（其余都是「已识别」的记录）。
    if (ambiguousGameMessage !== '') parts.push(ambiguousGameMessage)
    if (filled.length > 0) parts.push(`已识别：${filled.join('、')}`)
    if (result.unmatched.length > 0) parts.push(`没认出来：${result.unmatched.join('、')}`)
    // 名称类字段没对上（如下拉字典里没有这个游戏/区服）→ 如实报出来，不静默丢。
    // ⚠️ `unresolved` 的每项里**内嵌粘贴原文解析出的名称**（客户可控的自由文本），
    //    这句话走 HTML 渲染（E-0303）⇒ 逐项过 `escapeHtml`。
    //    ⛔ 在 `unresolved.push(…)` 的构造点转义不行：后面的 filter 按**原文**比对剔除，
    //    构造点转义会让剔除失配（E-0027 家族：加转义先查既有比对路径）。
    if (unresolved.length > 0)
      parts.push(`没能对应上：${unresolved.map((u) => escapeHtml(u)).join('、')}`)
    if (parts.length > 0) msg(parts.join('；'))
  }

  /* ── 读平台订单（spec §2.5 / §7.2）── */

  /**
   * 读平台订单并回填表单。要点：
   *  - 单号 `length < 15` ⇒ **一次请求都不发**；
   *  - **没选来源也发**（per-source spec §6）：不给 `order_source`，后端按单号**自动判定**；
   *  - 成功 ⇒ 填价格（**仅当平台侧非空**）、买家昵称（**仅当 `#ww` 为空**）、
   *    电话（**仅当平台侧非空**）、需求（**仅当为空或长度 <2**）；
   *  - 失败 ⇒ **静默**降级为手工填价（不弹错，只把价格恢复可编辑）。
   * ⚠️ 金额是后端用 `Decimal` 除过 100 的**元**字符串——前端**不做任何换算**。
   * `sourceOverride` 是**触发点 ② 的入参**（客服刚点完下拉，以刚选的为准）；
   * 不传（`undefined`）才回落去 DOM 读——触发点 ①③ 走这条。
   * ⚠️ `null` 与 `undefined` 不是一回事（E-0021）：`null` 是「用户刚把来源清空了」，
   * 那时**仍然不发请求**（不会自动判定把刚清掉的来源又填回来）——这一条是刻意的。
   */
  async function readPlatformOrderIntoForm(sourceOverride?: number | null): Promise<void> {
    const tid = inputValue(root, 'souresOrderID').trim()
    /**
     * 给后端带哪个来源：
     * 触发点 ② ⇒ 用客服点的那个值；触发点 ①③ ⇒ **只有当客服真的指定过来源时**才从 DOM 读
     * 一个发出去；**默认值 / 上一次是后端自动判定的** ⇒ 发 `null`（请求里不带 `order_source` 键）
     * ⇒ 后端**重新自动判定**。
     * ⚠️ 这一格是那次线上回归的正中心：默认第一条被当成"客服选的"发出去 ⇒
     * 后端不去自动判定 ⇒ 换店铺的单读不出来（见 `orderSourceIsAuto` 的注释）。
     */
    const sourceId =
      sourceOverride === undefined
        ? orderSourceIsAuto
          ? null
          : readOrderSource(root, fsValue)
        : sourceOverride
    /** 这一次**真的把来源指定给了后端**吗（回填时要靠它决定 `auto` 的取值）。 */
    const sentExplicitSource = sourceId !== null
    // ⚠️ 两条**独立**的返回条件，别合成一个 `||`：语义不同。
    //    - 单号太短：就是这么挡的；
    //    - 用户刚清空来源：`sourceOverride === null`（E-0021 的 `null` vs `undefined` 约定）。
    if (tid.length < 15) return
    if (sourceOverride === null) return
    // ⭐ 2026-11-10 客户线：买家身份随**这次读单**暂存（成功才落、失败/无身份即清——换单号
    //    不能拿上一单的身份；阿奇索腿无 `buyer_eid` 键）。⚠️ 残差：清掉后上一单回填的昵称/电话
    //    仍显示在表单（仅空才填）⇒ 界面像"还连着"，实际这一单不连任何客户（STATUS 残差有记）。
    pendingBuyer = null
    try {
      const fields = await readPlatformOrder(sourceId, tid)
      const buyerEid = (fields.buyer_eid ?? '').trim()
      if (buyerEid !== '') pendingBuyer = { eid: buyerEid, nick: fields.buyer_nick }
      // 价格**仅当平台侧非空**时写：上游成功但没有 `payment` ⇒ 后端刻意回空串 + 成功码（合法契约）。
      // 无条件写会把客服已经填好的价格**清空**，而**空价不会报错**：载荷把空串键整个丢掉、
      // 后端 `total_price` `required=False` ⇒ 请求里没这个键时订单被记成 0 元（算错钱的方向）。
      // ⚠️ 本页不能静默录出「空总价」单（新建态有「订单总价必填」闸）；但下面这条链在别处仍成立
      //    （外部脚本 / 平台拉单路径不受本页闸门约束）⇒ 「0 元单」依旧合法存在，
      //    只是从「静默的 0」变成「显式看见的 0」。因此「仅当平台侧非空才写价」仍然必须保留。
      // ⚠️ 「退不掉」是**附加条件**：退单闸门读 `received_price`（接单实收），为空才回落**订单总价**
      //    ⇒ 总价 0 元但接单实收有钱的单**不会被这道闸拒**；总价也是 0 时毛收款就是 `0.00`。
      // ⚠️ 判据取**字符串比较** `!== ''`；**不要**改成「先转数字再判真假」那一类
      //    （`Number('0.00') === 0` ⇒ 0 元单的价格写不进去；E-0021 在 JS 侧的同型）。
      if (fields.payment !== '') writeField(root, 'price', fields.payment)
      // 昵称**仅当 `#ww` 为空**时填。在 `await` **之后**才读 `#ww`：
      // 请求飞行期间客服手填的昵称不会被覆盖（刻意比对标更保守）。
      if (!inputValue(root, 'ww')) writeField(root, 'ww', fields.buyer_nick)
      // 电话仅当平台侧非空。⚠️ 阿奇索那条路的 `customer_phone` 是**硬编码的空串**
      //（上游订单查询没有电话字段），走阿奇索时这一支不触发；闲管家那边是 `receiver_mobile`。
      if (fields.customer_phone) writeField(root, 'moblie', fields.customer_phone)
      // 商品标题（`fields.remark`）不再回填 `#dlcontent`：随单保存、打手可见
      //（2026-11-05 业务方拍板；v2 录单页同批）。标题仅用于下方自动切游戏。
      // 回填来源下拉：后端自动判定成了哪家店，前端要**看得见**。
      // ⚠️ 判据是 `typeof === 'number'`：`order_source` 可能是 `null`（那时前端**不动**下拉）。
      if (typeof fields.order_source === 'number') {
        // 自动判定可能**刚建了一个来源** ⇒ 先把它补进字典，再选中（顺序不能反：
        // `applyOrderSourceOptions` 会清掉选中）。
        ensureOrderSourceOption(fields.order_source, fields.order_source_name)
        // `auto = !sentExplicitSource`：**我们没发来源**的那一次，这个值确实是后端判出来的 ⇒
        // 继续保持"自动"（下一次换单号还会重新判定）；发过来源（客服选的 / 粘贴指定的 /
        // 编辑态自带的）⇒ 不许被这次回填降级成"自动"，否则客服为**不在自动判定范围内**的店铺
        // 手选的来源，下一次读单就丢了。
        setOrderSourceSelection(fields.order_source, !sentExplicitSource)
      }
      // 接入点 A：**现有全部回填之后**按商品标题自动切游戏。
      // ⛔ 扫描的是 `fields.remark`（后端回的商品标题），**不是** `#dlcontent` 的值
      //（客服手敲的内容不该拿来认游戏）；商品标题**每一单都有**（空串 ⇒ 检测器直接 `none`，不误伤）。
      //
      // ⛔ **每次识别都自动选一次**（二次裁定）。判据 = `sourceOverride === undefined`
      // ——它恰好只覆盖「单号这条路」的两个触发点（① 改来源单号 / ③ 粘贴回填出单号），**不包括**
      // 触发点 ②（客服改来源）。所以：客服**之前**手改过游戏 ⇒ 这次识别照样切过去；
      // 客服在**这次识别之后**手改游戏 ⇒ 同一次录入里不再被顶回去，直到重新录一单。
      // 见 `userPickedGame` 的注释。
      if (sourceOverride === undefined) userPickedGame = false
      await autoSelectGameByContent(fields.remark, '商品内容')
    } catch (e) {
      // 静默降级为手工填价：**不弹错**——上游失败原因已在后端日志里，这里只把价格恢复可编辑。
      // ⚠️ 还有一条「价格只读」的服务端标志，读单端点**没有**（只回六个字段）⇒ 本页从不锁价格，
      // 这里的解锁是失败分支的 1:1（今天等价于幂等）。
      const price = query<HTMLInputElement>(root, `[name="price"]`)
      if (price) price.readOnly = false
      // 「静默」只覆盖**后端明确的业务失败**（`BizError`）——那是设计好的降级。
      // **其余错误不得消失**：形状变了导致的 `TypeError`、传输层 `HttpError`/`NetworkError`
      // 都是「我们坏了」，把它们一起吞掉会让监控失明（E-0010；后端 docstring 写的是同一条规矩）。
      //
      // ⚠️ **开口面 = `SPEAKING_READ_ORDER_CODES`（`1015` ∪ `1028`）**，⛔ 不是"所有业务失败"
      //    （逐码理由见集合处的注释）：`1015` 的 msg 是给客服看的**操作指引**（自动判定最常见的
      //    结局恰恰是它——静默吞掉等于"填了单号什么都没发生、也不知道该手动选来源"）；
      //    `1028` 同理（msg 里写着为什么读不出来、买家付款后再读一次）。
      //    `1016`/`1017` 仍然静默：那是真·上游故障（凭据/网络），提示了客服也无事可做
      //    （原因已在后端日志里）。
      if (e instanceof BizError) {
        if (SPEAKING_READ_ORDER_CODES.has(e.code)) msg(e.message)
      } else {
        console.error('[order-addorder] 读平台订单回填失败（非业务错误）', e)
      }
    }
  }

  /* ── 提交 ── */

  let submitting = false
  /**
   * 「上一次**建单**提交的结果未知」（5xx / 网络异常）。
   * 为什么要有它：后端指派抛未预期异常时**响亮 500**，而订单**已经提交在库里**
   * （指派失败不回滚订单）⇒ 用户看到「服务器错误」后**再点一次保存**就是又建一张单。这一档：
   * ⛔ **不复位表单**（真的没建成功时，复位会把用户填的一整单丢掉——两种可能无法区分）；
   * ⛔ **不伪装成功**；✅ 置标志，下一次点保存**不会直接发请求**，先弹二次确认。
   */
  let submitOutcomeUncertain = false

  /**
   * 「接单销售客服」那一格**进来时**选中的是谁。
   * **为什么必须有**：编辑态**不把 `sales_agent` 放进 PATCH 载荷**（换归属走专用端点）
   * ⇒ 「用户改没改这一格」只能靠**前后对比**。名字拿来做提示文案，主键拿来做提交参数——
   * 两者**同一次读**（`readSalesAgentChoice`），⛔ 不要分两处读（会给"名字是甲、主键是乙"
   * 留一个 DOM 被改动的窗口）。
   * 写入点**恰好两处**（都在 `mount` 里）：首屏那次渲染之后、详情回填之后重新渲染的那一次。
   * ⛔ `resetAfterSave` 那一路**不写它**——那一路必然回到新建态语境，新建态初值由首屏那次定下。
   */
  let initialSalesAgent: { id: number | null; name: string } = { id: null, name: '' }

  /**
   * **把那一格画回服务端真值**。
   * ## 为什么必须有
   * 编辑态保存链是「先 PATCH 存单 → 再打专用端点换归属」。**第二发失败**时订单**已经存好了**
   * ⇒ 只能提示一句然后 `return`。可那一格仍然画着**用户新选的人**，而 toast 几秒就消失——
   * 唯一**持久**的"已经改成"证据就是那个选中态（静默误导）⇒ 这一发失败时把那一格重画回
   * `initialSalesAgent`（**进来时读到的服务端真值**）。
   * ## 它**不写** `initialSalesAgent`
   * 基线是「服务端真值」，失败没有改变服务端 ⇒ 基线不动（⛔ 改成新选的人会让第二发**再也不发**）。
   * ## 为什么用**名字**重画
   * 编辑态预勾就是按**名字文本**匹配的 ⇒ 传 `initialSalesAgent.name` 得到的 DOM 与
   * 「刚打开这一单时」逐字相同。
   */
  function restoreSalesAgentRow(): void {
    renderSalesAgentOptions(root, {
      items: salesAgentOptions,
      currentUserId: viewer?.id ?? null,
      currentUserName: viewerName,
      currentName: initialSalesAgent.name,
      editing: true,
    })
    // ⚠️ `innerHTML` 换掉之后 layui 的 radio 皮肤一起没了 ⇒ 重渲一次。
    // layui 的 `radio` 渲染**先 remove 旧皮肤再建** ⇒ 重复调用是幂等，
    // 与页内另外两处程序化改选之后的做法同一条口径。
    formRender('radio')
  }

  /**
   * 二次确认：上一次结果未知时，用户明确说「知道，再试一次」才允许再提交。
   * ⚠️ 选**二次确认**而不是「把按钮置灰」：置灰之后用户没有任何出路，而这一档**两种可能都存在**
   * （真建了 / 没建）⇒ 把判断交回用户，并把「先去订单列表看一眼」写进文案。
   * ⚠️ `yes` 回调里**先清标志再提交**：清早了会在网络又失败时丢失「未知」状态，
   * 清晚了会让本次提交又被自己拦一次。
   */
  function askToResubmitAfterUncertainOutcome(): void {
    callLayer(
      'confirm',
      RESUBMIT_CONFIRM_MESSAGE,
      { title: '上一次提交的结果未知', icon: 3 },
      () => {
        submitOutcomeUncertain = false
        void onSave()
      },
      () => undefined,
    )
  }

  /** 读当前区服：优先读同步过的隐藏 `<select>`，没有就回落到 formSelects 的当前值。 */
  function readServer(): number | null {
    const fromSelect = toId(selectValue(root, 'gameArea'))
    if (fromSelect !== null) return fromSelect
    const raw = fsValue('gameArea')
    const first = Array.isArray(raw) ? (raw[0] as unknown) : raw
    const value =
      typeof first === 'object' && first !== null
        ? (first as { value?: unknown }).value
        : (first as unknown)
    return toId(value === null || value === undefined ? '' : String(value))
  }

  /**
   * 把**本地的默认档位**对齐到服务端事实。
   * 三档（`undefined` = 这一次没发那个键 ⇒ **不动**，服务端那一份也就没变）：
   * `'hall'` ⇒ 只记 hall（后端写 hall 时把人清掉，两列互斥）；`'Y'` ⇒ 记人（`takerId` 为 `null`
   * 时**不动 id**：那是「勾着但没选中人」这一支，本来就不该发 `'Y'`）；`'N'` ⇒ 两者清零。
   * ⚠️ 它只描述「服务端现在记着什么」，⛔ 与「这一单选了什么」（`toGrabHall` / `selectedAssignee`）
   * 是两件事：后者由 `resetAfterSave` 复位——分得开才不会有「标记粘到下一单」。
   * ⚠️ 调用它的两条路径都必须**知道后端的确切结果**才配调（保存成功那两支，以及
   * `1023` 那一支——那里只有 `'N'` 算数）。
   */
  function syncLocalDefaultPreference(
    remember: 'Y' | 'N' | 'hall' | undefined,
    takerId: number | null,
  ): void {
    if (remember === 'hall') {
      initialDefAssIsHall = true
      initialDefaultAssigneeId = 0
      return
    }
    if (remember === 'Y') {
      initialDefAssIsHall = false
      if (takerId !== null) initialDefaultAssigneeId = takerId
      return
    }
    if (remember === 'N') {
      initialDefAssIsHall = false
      initialDefaultAssigneeId = 0
    }
  }

  async function onSave(): Promise<void> {
    if (submitting) return
    // ⭐ **结果未知 ⇒ 不许盲目重试**：先让用户明确确认一次再走下面那条链。
    //    位置在**读表单之前**：这一档不构造载荷、不发请求，只弹确认。
    if (submitOutcomeUncertain) {
      askToResubmitAfterUncertainOutcome()
      return
    }
    const current = readFormState(root, readServer)
    // 订单来源的选中值不在 `readFormState` 的覆盖范围内（运行期注入的下拉），单独读回模型。
    // 放在构造载荷**之前**是刻意的：create 与 edit 两条分支共用同一个「空值不发」口径
    // ——载荷函数会把 `null` 丢掉 ⇒ 「没选」就是**请求体里没有这个键**（E-0021：不是真值判）。
    const source = readOrderSource(root, fsValue)
    current.order_source = source
    // ⭐ 「接单销售客服」：**主键 + 名字一起读一次**，但两条提交路**都不把名字放进载荷**：
    //   新建态 ⇒ 主键进 `sales_user`；编辑态 ⇒ 主键进专用端点，**PATCH 载荷里没有这一列**。
    //   ⚠️ 改口的原因：`sales_agent` 只是**文本快照**，只送名字会造出「名字换了、钱没换」的假成功
    //   ⇒ 名字一律由**服务端**从人身上算（两列同源 ⇒ 不可能分叉）。
    //   ⚠️ 权限门（编辑态 + 无节点）在 `readSalesAgentChoice` 里：回 `{id: null, name: ''}`
    //   ⇒ 两条路都**不发**。新建态**不受**这个点限制（理由见 `SALES_AGENT_PERMISSION_CODE`）。
    //   ⚠️ 与 `order_source` 不同，空的 `sales_agent` **没有**「显式清空」这一支：那一格永远勾着
    //   一个人，客服没有「把这个人去掉」的操作。
    const pickedSalesAgent = readSalesAgentChoice(root, editOrderNo !== '')
    current.sales_agent = pickedSalesAgent.name
    /**
     * 「这次建单要不要**指定提成归属**」——两条建单路（普通 / 内部抢单）**共用这一处判据**。
     * 三档（`undefined` = 不发那个键 ⇒ 后端维持「归属 = 认证态」的既有行为）：
     * 选的是**自己** ⇒ 不发（与改动前逐字等价）；没读出主键 ⇒ 不发；
     * **不持节点** ⇒ 不发（没节点就不发 ⇒ 界面所见与提交内容一致）。其余（选了**别人**且持节点）
     * ⇒ 发**主键**。
     * ⚠️ **抽成一处是承重的**：两条建单路各写一份判据必然漂移（后果是「同一格选的人，
     * 两条路的提成归属不同」）。
     */
    const intakeSalesUserId =
      canChangeSalesAgent &&
      pickedSalesAgent.id !== null &&
      pickedSalesAgent.id !== (viewer?.id ?? null)
        ? pickedSalesAgent.id
        : null
    // 粘贴框内容即「客户原始需求」。
    current.content = inputValue(root, 'dlcontent')
    // 事件回调里已经写过的选择以 DOM 为准再读一次；这里补一次兜底。
    // ⚠️ 「订单总价必填」**只对新建态**：编辑态的载荷被摘掉了全部金额键 ⇒ 那个框空不空
    //    **根本不会提交**，一并拦会变成「因为一个不提交的框为空而保存不了」的怪象
    //    （改价仍走订单列表那条路，表单本身已必填）。串在 `requiredError` **之后**：
    //    既有两条必填的提示优先级不变。
    const totalPriceBad = editOrderNo === '' ? totalPriceRequiredError(current) : ''
    // ⭐ 「代练项目 700 字节同步上限闸」（业务方裁定；取证 `docs/ERRORS.md` **E-0242**）：
    //    实际接受上限 ≈732/733 **字节**（按字节不按字符），超限**警告并阻止保存**（不截断）。
    //    串在本链**最后**：上面两条必填与金额校验的提示优先级不变；⚠️ 这一段在 `let remembered`
    //    声明（其上有切片锚点说明）**之前**，不在 `submitting = true` 与 `try {` 之间。
    //    与 `#project` 的 `maxlength="900"`（字符口径）并存：这道管「能不能同步出去」。
    const projectSyncBad = projectTextSyncLimitError(current.project_text)
    const bad = requiredError(current) || totalPriceBad || firstMoneyError(current) || projectSyncBad
    if (bad !== '') {
      msg(bad)
      return
    }
    // ⭐ **内部抢单必须有一个大于 0 的价**——拦在**提交之前**，一个请求都不发
    //    （`submitting` 也还没置位，用户改完价可以直接再点一次「录单」）。
    // ⚠️ 判据是**解析后的数值**，⛔ 不是真值判：`'0'` / `'0.00'` 都是**真值**却是非法价
    //    （E-0021 的 JS 侧同型）。
    // ⚠️ 与后端那道 `1000` **各自独立**：这一层只是别让用户白跑一趟（浏览器里的闸拦不住
    //    直接打接口的人，后端那道仍然要有）。
    if (toGrabHall) {
      const fee = Number(throwFeeInput?.value)
      if (!(fee > 0)) {
        msg(HALL_PRICE_REQUIRED_MESSAGE)
        return
      }
    }
    /**
     * 这一次保存发出去的 `remember_assignee`（`undefined` = 没发这个键）。
     * ⚠️ **声明在 `try` 之外是承重的**：`1023` 那一条 `catch` 也要读它——后端在那条路径上的
     * 确切结果是「单建好了、**抛单没成**」⇒ 只有 `'N'` 落了，本地状态要照**这一个**事实对齐。
     * ⚠️ **位置也承重**：必须在 `submitting = true` / `try {` 这**两行之前**——
     * `__tests__/order-addorder.spec.ts` 有一条按源码文本切片的判据，锚点恰好是
     * `    submitting = true\n    try {\n`（管的是「保存链里不许出现弹层命名空间的字样」）；
     * 把任何语句插在它们中间都会让那条判据以 `expected undefined to be defined` 红
     * （实测踩到过）⇒ 要加语句请加在这一行之前，⛔ 别改那个锚点。
     */
    let remembered: 'Y' | 'N' | 'hall' | undefined
    submitting = true
    try {
      if (editOrderNo !== '') {
        // 编辑态：**真提交**。载荷**不含金额**（见 `formToUpdatePayload`）。
        const payload = formToUpdatePayload(current, actor)
        if (canEditCurrentSplitPrice && current.total_price !== initialOrderPrice) {
          ;(payload as Record<string, unknown>).total_price = current.total_price
        }
        // **支持编辑态清空来源**，但只在「用户真的清过」时补键：载荷函数会丢掉 `null`，
        // 所以「进来时有来源、用户清空了」这一支要**显式**发 `order_source: null`（FK 置空）。
        // ⚠️ `sourceClearedByUser` 不能省：`source === null` 还有第二种成因——订单带回来的 id
        // **不在字典里**（截断 / 字典加载失败），真插件加不进去 ⇒ 读了也是 `null`。那时用户没动过，
        // 补 `null` 就是静默清空来源。「本来就没有、用户也没动」也不发键（避免凭空加一个写入）。
        if (initialOrderSource !== null && sourceClearedByUser && source === null)
          payload.order_source = null
        // ⭐ **「接单销售客服」那一列不进 PATCH 载荷**：`current.sales_agent` 在上面被赋值了
        //    ⇒ 不删掉就会跟着发出去。后果**不是**"多送一个无害的键"：后端那道闸「`sales_agent`
        //    的值真的变了 ⇒ 要 `order.allow_change_sales_agent`」正好会被**我们自己**触发，
        //    而那个节点与「保存订单」的门是两个门（只持 `order.add_edit` 的客服会莫名 403）。
        //    **真正的**换归属走下面那一发专用端点（名字由服务端从人身上算，两列同源）。
        delete (payload as Record<string, unknown>).sales_agent
        // ⭐ 用**带回信封 `msg`** 的出口。后端保存成功后可能顺手撤这张单还挂着的平台订单；
        // **撤不掉时接口仍是成功码**、警告只在 `msg` 里 ⇒ 只取 `data` 会让那句警告被吞掉
        // ⇒ 父页永远只显示固定的「保存成功」。
        const { message: saveMessage } = await updateOrderWithMessage(editOrderNo, payload)
        // ⭐ **改归属走专用端点**：只在「用户真的改了这一格」且「他有那个节点」时发。
        //    ⚠️ 判据是**前后主键不同**（一次读回主键 + 名字）；⛔ 不是"载荷里有没有 `sales_agent`"
        //    ——那个键现在恒不发。⚠️ **位置**：必须在 PATCH **成功之后**（反过来会在 PATCH 失败时
        //    留下「归属换了、别的字段没存」的现场，而用户看到的是"保存失败"）。
        //    ⛔ **这一支里不许出现任何弹层命名空间的字样**：`__tests__/order-addorder.spec.ts`
        //    有一条按源码切片的结构判据（try 块里只允许发请求 + 置模型），本段注释也在扫描面里，
        //    写那几个字母会让**说得对**的判据红（实测踩到过）。
        //    ⚠️ **失败必须明说**：这一发失败时**订单已经存好了** ⇒ 既不能说"保存失败"（假话，
        //    用户会重填一遍），也不能当没事发生（界面会显示成"已改"）⇒ 用一句**把两件事都说清**
        //    的文案，并且**⛔ 不通知父窗口**（会让列表刷新+弹窗关闭，看起来像全成了）。
        const salesAgentChanged =
          pickedSalesAgent.id !== null && pickedSalesAgent.id !== initialSalesAgent.id
        if (salesAgentChanged) {
          try {
            await changeSalesAgent(editOrderNo, { user: pickedSalesAgent.id as number })
          } catch (e) {
            const why = e instanceof Error ? e.message : ''
            // ⭐ **失败时把那一格画回服务端真值**（见 `restoreSalesAgentRow`）：
            //    toast 几秒即逝，"选中态"是唯一持久的证据。
            //    ⚠️ 顺序：先复位再提示——先做终态免得中途 return；复位**不动** `initialSalesAgent`。
            restoreSalesAgentRow()
            msg(why === '' ? SALES_AGENT_FAILED_MESSAGE : `${SALES_AGENT_FAILED_MESSAGE}：${why}`)
            return
          }
        }
        // 关弹窗 / 出提示 / 刷新列表都由**父窗口**做——本页先关掉自己（在这里弹提示会随
        // iframe 一起消失）。后端成功信封的 `msg` **原样带过去**（空串 ⇒ 父页回退「保存成功」）。
        notifyOrderSaved(editOrderNo, saveMessage)
        return
      }
      const payload: CreateOrderPayload = formToPayload(current, actor)
      // ⭐ 2026-11-10 客户线（W1）：暂存的买家身份随创建载荷回传（后端 upsert 客户档案并连单）。
      //    ⛔ 没有就**不发这个键**（缺键 = 不建不连，与 `throw_to_hall` 同一条纪律）。
      //    ⚠️ 只在**新建**这条路发；编辑态在上面那段 `updateOrder` 早退，走不到这里。
      if (pendingBuyer !== null) {
        payload.buyer_eid = pendingBuyer.eid
        payload.buyer_nick = pendingBuyer.nick
      }
      // ⭐ **拆单**：勾了那颗框 ⇒ 这一发带 `split: true`（切分由**后端**做）。
      //    ⛔ 没勾就**不发这个键**（缺键 = 不拆；与 `throw_to_hall` 同一条「布尔开关」纪律）。
      //    ⚠️ 与下面两条后续动作（内部抢单 / 快速指派）**不互斥**：后端只对**第 1 笔**做那些动作。
      if (chaifenBox?.checked === true) payload.split = true
      // ⭐ **内部抢单**：建单这**同一次**请求里把单抛进内部抢单大厅。
      //    ⛔ 这一档**不带** `assignee`——二选一（两个键同时给 ⇒ 后端在建单之前回 `1000`），
      //    而这一档没有人被选中。⚠️ `hall_price` **不在这里补**：仍由 `#throwFee` 那一格经
      //    `FIELD_MAP` 出（前面那道闸已保证它解析后 `> 0`）。
      //    ⚠️ 抛单在后端**建单事务之外**：它失败时订单**不回滚**（业务码 `1023`）⇒
      //    下面的 catch 必须把那一档如实说出来。
      //    ⭐ **`remember_assignee`**：勾上就是「记住默认抛内部抢单」⇒ 按四态取值发出去。
      //    见 `rememberAssigneeValue`。
      if (toGrabHall) {
        payload.throw_to_hall = true
        remembered = rememberAssigneeValue(
          // 这一档没有人被选中（`applyAssigneeSelection` 保证它恒为 `null`）⇒ 恒走 hall 那两支。
          null,
          defAssBox?.checked === true,
          initialDefaultAssigneeId,
          true,
          initialDefAssIsHall,
        )
        if (remembered !== undefined) payload.remember_assignee = remembered
        if (intakeSalesUserId !== null) payload.sales_user = intakeSalesUserId
        await createOrder(payload)
        submitOutcomeUncertain = false
        state = current
        msg(SAVE_SUCCESS_MESSAGE)
        // 保存成功 ⇒ 本地状态跟服务端对齐（`'hall'` ⇒ 记住「默认抛内部抢单」；
        // `'N'` ⇒ 两者清零）——复位时照它把默认档位再带出来（见 `resetAfterSave`）。
        syncLocalDefaultPreference(remembered, null)
        resetAfterSave()
        return
      }
      // ⭐ **快速指派**：选中了人就把它的 id 带上（后端建单后**同一次请求**里指派）；
      //    「默认指派此人」按三态进载荷（读的是**服务端**记着的那个人 ⇒「本来就没设」不会白发清除）。
      if (selectedAssignee !== null) payload.assignee = selectedAssignee.id
      const remember = rememberAssigneeValue(
        selectedAssignee?.id ?? null,
        defAssBox?.checked === true,
        initialDefaultAssigneeId,
        // 本单不是 hall 档 ⇒ `isHall = false`（四态里前三支的判据与原来的三态逐字相同）；
        // 而 `initialIsHall` 要照实传：服务端记着 hall 时，「取消勾选」是一次真的清除。
        false,
        initialDefAssIsHall,
      )
      remembered = remember
      if (remember !== undefined) payload.remember_assignee = remember
      // ⭐ **「接单销售客服」选的是别人 ⇒ 指定提成归属**（「换销售客服 = 提成也换人」）。
      //    判据与上面那条**同一个** `intakeSalesUserId`（选自己 / 读不出主键 / 不持节点 ⇒ 不发键）。
      //    ⛔ 与 `assignee` **不冲突**：那一档是"指派打手"，这一档是"提成归属"，后端分别校验。
      //    ⚠️ **显式指定归属时，名字那一列不进载荷**：后端收到 `sales_user` 时**以服务端算出的
      //    显示名为准**（请求体里的 `sales_agent` 不作数）⇒ 送它是**死载荷**，而"载荷里有一个
      //    会被忽略的键"是下一个人最容易读错的东西。⛔ 别为了"少一次判断"把它留下。
      if (intakeSalesUserId !== null) {
        payload.sales_user = intakeSalesUserId
        delete (payload as Record<string, unknown>).sales_agent
      }
      // 单号（`order.order_no`）仍然返回，但**有意不展示**：业务方口径是「只出小提示 + 刷新」。
      await createOrder(payload)
      // 建单真的成功了 ⇒ 「上次结果未知」这一档解除（下一单不必再确认）。
      submitOutcomeUncertain = false
      state = current
      msg(SAVE_SUCCESS_MESSAGE)
      // 记住成功 ⇒ 本地也认这个默认（复位后照「下次录单」把这个人再带出来，与刷新一次页面同口径）。
      syncLocalDefaultPreference(remember, selectedAssignee?.id ?? null)
      resetAfterSave()
    } catch (e) {
      // ⭐ **「单已建好、指派没成功」那一档（后端 `1022`）不是普通失败**：
      //    单**已经落库**了 ⇒ 必须（1）原样显示后端那句（含单号与原因）；（2）**复位表单**
      //    ——不复位的话用户再点一次保存就是**又建一张单**。
      //    ⛔ 不把这一档并进下面那句「保存失败」：那会让页面看起来什么都没发生。
      if (e instanceof BizError && e.code === ORDER_CREATED_ASSIGN_FAILED) {
        msg(e.message)
        state = current
        resetAfterSave()
        return
      }
      // ⭐ **「单已建好、抛大厅没成功」那一档（后端 `1023`）与上面 `1022` 同一处置**：
      //    单**已经落库**了 ⇒ 原样显示后端那句并**复位表单**，⛔ 不许并进「保存失败」、
      //    ⛔ 不许伪装成功。⚠️ 后端**不会**把其它异常翻译成这个码（未预期的仍然响亮 500）。
      if (e instanceof BizError && e.code === ORDER_CREATED_THROW_FAILED) {
        msg(e.message)
        state = current
        // ⭐ 后端在这条路径上的确切结果：**单建好了，但抛单没成** ⇒ `'hall'` **没有**落库
        //    （后端只在抛单成功之后才记），而 `'N'` **落了**（无条件清除，抛单失败也照清）。
        //    ⇒ 本地状态只按 **`'N'`** 对齐，⛔ 别把「这一次想记 hall」当成已经记上了。
        //    ⚠️ `'hall'` / `undefined` 两支在这里**什么都不做**：与刷新一次页面同口径。
        if (remembered === 'N') syncLocalDefaultPreference('N', null)
        resetAfterSave()
        return
      }
      // ⭐ **「结果未知」那一档**（只对**建单**，编辑态的 PATCH 不在此列）：
      //    5xx / 网络异常时**无法知道**这一单建没建 ⇒ 如实说「结果未知」、**保留用户填的内容**，
      //    并置标志让下一次保存先走二次确认（⛔ 不盲目重试、⛔ 不伪装成功、⛔ 不无条件复位）。
      if (editOrderNo === '' && isUnknownSubmitOutcome(e)) {
        submitOutcomeUncertain = true
        msg(SUBMIT_OUTCOME_UNKNOWN_MESSAGE)
        return
      }
      // 后端业务失败（`code != 0`）由 `request()` 抛 `BizError`，`message` 就是后端 `msg`——
      // 原样展示，不改写（与改状态/改价两处同口径）。
      msg(e instanceof Error ? e.message : '保存失败')
    } finally {
      submitting = false
    }
  }

  /**
   * 保存成功后的「刷新当前录单页」：先做纯 DOM 复位（`resetFormToInitial`），再重建级联。
   * 级联重建直接复用 `onGameChange`（正是「级联下拉复位」）；游戏取 `games[0]`，
   * 与首屏同一个起点。
   * ⚠️ formSelects 不会因 `form.reset()` 复位（选中态存在它自己的实例里）⇒ 显式 `value(id, [])`
   * 清空（数组分支走 `common.removeAll`，同时撤掉它渲染的标签）。
   */
  function resetAfterSave(): void {
    // 本次录单结束 ⇒ 「上次结果未知」这一档跟着结束（它与「这一次填的内容」同生命周期）。
    submitOutcomeUncertain = false
    resetFormToInitial(root, games[0]?.id ?? null)
    // 快速指派的**表单级状态**也要复位（`resetFormToInitial` 只认识 `FIELD_MAP` 里的控件）：
    // 不复位的话下一单会**静默**沿用上一位接手人。
    // ⚠️ **内部抢单标记同一条纪律**：它是「这一单选了什么」的一部分，不复位就会**粘到下一单**
    //    （下一单明明没选内部抢单，载荷里却带着 `throw_to_hall`）。上一发若以 `1023` 收尾也会走到这里。
    // 复位照当前默认档位再带出来，不一律回到「暂不指派」。
    // 源是本地 initial*（已与服务端对齐），不是把这一单的选择粘到下一单。
    toGrabHall = initialDefAssIsHall
    // 勾选框与「首屏带出」同一口径：默认 hall 或默认某人 ⇒ 勾着。
    if (defAssBox) defAssBox.checked = initialDefAssIsHall || initialDefaultAssigneeId > 0
    // ⚠️ 顺序：先清选中再复原默认——`initialDefaultAssigneeId` 若刚被本次保存写成 Y，
    //    这一步就把那个人**照「下次录单」再带出来**（与刷新一次页面同口径）。
    // ⚠️ 默认 hall 那一支 `selectedAssignee` 必须是 `null`（两支互斥，同 `loadCandidates` 的 hall 优先）。
    selectedAssignee =
      initialDefAssIsHall || initialDefaultAssigneeId === 0
        ? null
        : (candidates.find((row) => row.id === initialDefaultAssigneeId) ?? null)
    applyAssigneeSelection()
    ;(formSelects?.value as undefined | ((i: string, v: unknown[]) => void))?.('gameArea', [])
    // 来源同形——formSelects 的选中态存在插件实例里，`form.reset()` 清不掉它
    // （`resetFormToInitial` 只清了隐藏 `<select>`）。
    ;(formSelects?.value as undefined | ((i: string, v: unknown[]) => void))?.('orderSoures', [])
    // 复位之后**重新选中第一条来源**：「保存成功 → 再录一笔」与「刚打开页面」同一口径
    // （先清后选，避免插件与隐藏 `<select>` 两条腿不一致）。字典为空 ⇒ 什么都不做（不炸）。
    // `auto = true`：这是**本页给的默认值**，不是客服指定的 ⇒ 读单时不许把它发出去。
    if (sourceOptions.length > 0) setOrderSourceSelection(sourceOptions[0]!.id, true)
    // 「客服手动调过游戏」这个标志回到**新表单**的初值。⚠️ 它**不是承重的**：下一个 ①③ 识别
    // 也会清它，这里只是让「保存完就是新的一单」在状态上也成立。
    userPickedGame = false
    // ⭐ 2026-11-10 客户线：暂存的买家身份跟着「新的一单」一起清（§5-E 回查复位路径——
    //    不清会把上一单的买家身份粘到下一单，且它不在 `FIELD_MAP` 里、`form.reset()` 够不着）。
    pendingBuyer = null
    const firstGame = games[0]
    if (firstGame) void onGameChange(firstGame.id)
    /**
     * 「接单销售客服」那一格也要复位——**同一族里最容易漏的一处**
     * （`AGENTS.md` §5-E「给一个字段加新的写入点时，必须回查它的回退 / 清理 / 重置路径」）：
     * 那些 radio 是**运行期灌进 `.cf` 的静态 DOM**，`form.reset()` 对它们只是回到
     * `defaultChecked`（= 上一单的那个人）⇒ 不复位就会**静默沿用上一位销售客服**。
     * 照「保存成功后再录一笔 = 刚打开页面」的同一口径重画：默认勾回**当前登录人**。
     * ⚠️ 复位之后本页恒处**新建态语境**（编辑态保存成功 = 关弹窗、不再录第二单），所以
     * `editing: false` 与 `currentName: ''` 是对的；真到那天编辑态也走这条复位路径时，
     * 这里要跟着改成按 `editOrderNo` 取（⛔ 别照抄成常量）。
     */
    renderSalesAgentOptions(root, {
      items: salesAgentOptions,
      currentUserId: viewer?.id ?? null,
      currentUserName: viewerName,
      currentName: '',
      editing: false,
    })
    // ⚠️ **复位之后也要重记"进来时选的是谁"**：`initialSalesAgent` 是「用户改没改这一格」的
    //    基线，复位把界面改回了默认 ⇒ 基线必须跟着回到同一个值，否则下一单的第一次保存会被判成
    //    "改动过"、白发一次换归属。判据与别的复位项同源：**读刚渲染出来的 DOM**，
    //    ⛔ 不在这里手写一个"应该是什么"的推断。
    initialSalesAgent = readSalesAgentChoice(root, false)
  }

  /* ── 控件登记：「尚未开放」的容器 + 金额焦点提示 ── */

  for (const entry of resolveNotOpenHosts(root)) {
    entry.host.setAttribute('data-notopen', entry.label)
    if (entry.onBlur) entry.host.setAttribute('data-notopen-blur', entry.label)
  }
  for (const link of Array.from(root.querySelectorAll<HTMLElement>(LAY_HREF_SELECTOR))) {
    if (!link.hasAttribute('data-notopen')) link.setAttribute('data-notopen', '该功能')
  }
  for (const sel of MONEY_DOM_SELECTORS) {
    const el = query<HTMLElement>(root, sel)
    el?.addEventListener('focus', () =>
      callLayer('tips', MONEY_INPUT_PLACEHOLDER, el, { tips: [1, '#3595CC'], time: 4000 }),
    )
  }

  const onClick = (event: Event): void => {
    const el = (event.target as HTMLElement).closest<HTMLElement>('[data-notopen]')
    if (!el) return
    event.preventDefault()
    event.stopPropagation()
    notOpen(el.dataset.notopen ?? '该功能')
  }
  const onFocusOut = (event: Event): void => {
    const target = event.target as HTMLElement
    const el = target.closest<HTMLElement>('[data-notopen-blur]')
    if (!el) return
    const input = target as HTMLInputElement
    if (typeof input.value !== 'string' || input.value.trim() === '') return
    notOpen(el.dataset.notopenBlur ?? '该功能')
  }
  // **捕获阶段**：layui 的表单/表格委托常在冒泡阶段 `return false` 把事件吞掉（踩过的坑，见
  // `system-user-list.ts` 的 `toolbarHtml()` 注释），捕获阶段先拿到就不会被吞。
  root.addEventListener('click', onClick, true)
  root.addEventListener('focusout', onFocusOut)

  /* ── layui 事件 ── */

  callForm('on', 'select(selectGame)', (obj: { value?: string }) => {
    // ⛔ 「客服自己点过游戏下拉」的**唯一**置位点（`userPickedGame` 的注释记了为什么这个回调
    // 等价于「真人点过」）：自动切游戏从此不再插手，直到保存成功后的复位把它清掉。
    userPickedGame = true
    const id = Number(obj.value ?? '')
    if (Number.isFinite(id)) void onGameChange(id)
  })
  callForm('on', 'radio(selectorderType)', (obj: { value?: string }) => {
    applyLabels(obj.value ?? '0')
    state.order_type = orderTypeValue(obj.value ?? '0')
  })
  callForm('on', 'radio(projectType)', (obj: { value?: string }) => {
    const id = Number(obj.value ?? '')
    if (Number.isFinite(id)) void onCategoryChange(id)
  })
  callForm('on', 'checkbox(dltype)', (obj: { elem?: HTMLInputElement }) => {
    if (obj.elem) toggleProjectText(obj.elem)
  })
  // 保存那颗按钮与它的 `lay-submit` 绑定**只在有 `canSaveOrder` 时注册**：缺节点时按钮已被
  // `applyAddOrderPermissionGates` 摘掉，这里也**不留**一条能走到 `onSave` 的路径
  // （两层 fail-closed：DOM 上不画 + 事件上不接）。
  if (canSaveOrder) {
    callForm('on', 'submit(savebtn)', () => {
      void onSave()
      // 与对标的 `return false` 一致：这里只走我们的 API，绝不让表单原生提交。
      return false
    })
  }

  // 粘贴解析是**失焦触发、没有按钮**⇒ 门的形态是「缺节点时根本不注册这个监听器」（一发都不发）；
  // 「会自动识别」的承诺由 `applyAddOrderPermissionGates` 同步摘掉（⛔ 不留假话）。
  if (canParsePaste) {
    query<HTMLTextAreaElement>(root, '#dlcontent')?.addEventListener('blur', () => {
      void onPasteBlur()
    })
  }

  // 触发点 ①：来源单号改完（失焦 / 回车）读一次。挂在元素上而不是 `document` 上：
  // 本页在 iframe 里、只有一个表单，委托到 document 没有额外好处。
  query<HTMLInputElement>(root, '#souresOrderID')?.addEventListener('change', () => {
    void readPlatformOrderIntoForm()
  })

  // xm-select 选中区服后把值同步回隐藏 `<select id="gameArea">`：`readFormState` 的默认读取路径
  // 因此对「真人点选」与「测试直接赋值」是同一条。
  // ⚠️ 必须是 `endOn`（改**后**）——用 `on`（改前）时「点 A 再点 B」写进 select 的是 A，
  // 而 `readFormState` 的快速路径读的就是它 ⇒ 提交旧区服（与来源下拉同一个缺陷）。
  bindSelectionSync(formSelects, 'gameArea', () => query<HTMLSelectElement>(root, '#gameArea'))

  // 订单来源同形——xm-select（**单选**）选中后同步回隐藏 `<select id="orderSoures">`；
  // 空集（「清空」）写 `''`。第四参是「这是**用户操作**」回调（程序化回填/复位不触发）：
  // 空集 ⇒ `sourceClearedByUser = true`，又选回非空 ⇒ 复位为 false（`onSave` 的判据靠它）。
  bindSelectionSync(
    formSelects,
    'orderSoures',
    () => query<HTMLSelectElement>(root, '#orderSoures'),
    (vals) => {
      sourceClearedByUser = vals.length === 0
      // 客服**自己**动过这个下拉（这个回调只由真人操作触发，程序化回填不会走到）：
      // 选了某一条 ⇒ 这是他**指定**的来源；清空了 ⇒ 按"没指定"记。
      // ⚠️ 这一支**不承重**（实测：改成恒 false 全量一条不红——清空后 `readOrderSource()`
      // 本来就回 `null`，读单照样走自动判定；见 `orderSourceIsAuto` 的注释）。
      orderSourceIsAuto = vals.length === 0
      // 触发点 ②：来源一变就用**刚选的新来源值**读一次——语义是「以用户刚选的为准」，
      // 照原实现把值显式传下去，**不**依赖 DOM 的读取时机。
      // ⚠️ 理由**不是**「DOM 里那个下拉还没同步完」——`bindSelectionSync` 的回调是
      //     **先写隐藏 `<select>`、再调本回调**，所以今天「传 override」与「回读 DOM」等价。
      //     保留 override 是因为它贴着原实现（`svalue` 就是显式入参），顺带使这条路径不依赖
      //     「谁先谁后」这个顺序——顺序将来若变，这里也不会读错值。
      // 清空（空集）⇒ `null` ⇒ 读单函数直接返回、不发请求。
      const picked = vals.length > 0 ? toId(String(vals[0]?.value ?? '')) : null
      void readPlatformOrderIntoForm(picked)
    },
  )

  /* ── 首屏：游戏字典 + 默认游戏的级联 ── */

  try {
    games = await fetchGames()
  } catch (e) {
    msg(e instanceof Error ? e.message : '游戏列表加载失败')
  }
  /**
   * 订单来源下拉的选项来自**真字典**（`GET /api/business/order-sources/?page_size=200`）。
   * 两处刻意的选择：① 与 `fetchGames()` **并列**在首屏取一次（来源是低频字典）；
   * ② 失败**如实提示、不静默降级成文本输入**——静默降级会让客服以为选中的是字典项，
   * 而实际发出去的是自由文本或什么都没有（写操作假装成功是禁止的形状）。
   * ⚠️ 返回**整页**，选项只消费 `.items`。
   */
  try {
    const orderSourcePage = await fetchOrderSources()
    sourceOptions = orderSourcePage.items.map((r) => ({ id: r.id, name: r.name }))
    fsData('orderSoures', orderSourceOptions(orderSourcePage))
    setOrderSourceSelectOptions(sourceOptions)
    // **新建态首屏默认选中第一条来源**。
    // ⚠️ 顺序不能反：`fsData` → `setOrderSourceSelectOptions`（它会把选中**清掉**）→ 选中第一条。
    // 编辑态**不动**：订单自带来源由 `prefillEditOrder` 照原样回填，订单没有来源时**不**编一个。
    // ℹ️ 不需要同步改 `state.order_source`：`readFormState` 不读来源，保存走 `readOrderSource`。
    // `auto = true`：默认值 ≠ 客服指定（否则读单会永远锁死在第一条来源上，见 `orderSourceIsAuto`）。
    if (editOrderNo === '' && sourceOptions.length > 0) {
      setOrderSourceSelection(sourceOptions[0]!.id, true)
    }
  } catch (e) {
    msg(e instanceof Error ? e.message : '订单来源加载失败')
  }
  /**
   * 首屏拉候选名单：**必须有这一发**，否则「默认指派此人」那条「下次录单自动带出那个人」
   * 永远不会发生（勾选框靠响应里的 `defAssRecManUid` 复原）。
   * 失败由 `loadCandidates` 自己 `msg` 提示（不静默降级）。
   */
  await loadCandidates()

  /**
   * 首屏拉「接单销售客服」候选（`GET /orders/kefu-options/`，复用订单列表页用的**同一个**封装，
   * ⛔ 不另写一个打同一端点的函数）。
   * 失败处理**刻意与上面两个字典不同**：这里是**静默降级成空候选**、⛔ 不提示一句。
   * 理由：候选只服务**这一格**，失败时那一格仍然可用（编辑态走「现值插在最前并勾中」那一档）；
   * 而按那两发的口径弹告警，会在**一个本来没坏的表单**上多出一条与本页主数据无关的告警。
   * ⚠️ 代价如实登记：真失败时用户看到的是「候选里只有他自己 / 只有现值」——分辨率不到
   * 「是后端坏了还是就这些人」。
   * ⚠️ `truncated` **必须说出来**（截断的人在控件里选不到 = 静默少选项）。
   */
  let salesAgentOptions: SalesAgentOption[] = []
  try {
    const kefu = await fetchKefuOptions({ limit: SALES_AGENT_OPTIONS_LIMIT })
    salesAgentOptions = (kefu.items ?? []).map((r) => ({ id: r.id, name: r.name }))
    if (kefu.truncated === true) {
      msg(`可作为销售客服的人超过 ${SALES_AGENT_OPTIONS_LIMIT} 位，已截断`)
    }
  } catch {
    salesAgentOptions = []
  }

  const gameSelect = query<HTMLSelectElement>(root, '#gameName')
  if (gameSelect) {
    // ⚠️ 游戏名来自 API（用户输入）⇒ 进 `innerHTML` 前过 `escapeHtml`。
    gameSelect.innerHTML = games
      .map((g) => `<option value="${g.id}">${escapeHtml(g.name)}</option>`)
      .join('')
    formRender('select')
  }
  // 操作人来自挂载开头那次 `fetchViewerAndActor()`（一次取数两用，⛔ 不再多发一发）。
  /**
   * 编辑态回填（对标编辑弹窗装本页时的初始化）。
   * 分三层，缺一层就有字段静默回填不上：
   *  1. `writeFormState` 写**静态标记里存在**的控件；
   *  2. `onGameChange` → `onCategoryChange` 重建**运行期注入**的区服 xm-select 与分类单选 /
   *     常用项目复选（它们的 `<option>` 来自我们的字典接口）；
   *  3. `day` + `hours` 由 `time_requirement` 逆解析后，只按**下拉里确实存在**的 option 落值
   *     （`select.value = 不存在的值` 会静默变 `''`）。
   * 最后 `state` 以 DOM 为准重读一次，与「人工改过再保存」走同一条读取路径。
   */
  function setSelectIfPresent(id: string, value: string): void {
    if (value === '') return
    const el = query<HTMLSelectElement>(root, `#${id}`)
    if (!el) return
    if (Array.from(el.options).some((o) => o.value === value)) el.value = value
  }

  async function prefillEditOrder(orderNo: string): Promise<void> {
    let order: Order
    try {
      order = await fetchOrder(orderNo)
    } catch (e) {
      msg(e instanceof Error ? e.message : '订单详情加载失败')
      return
    }
    const form = orderToAddOrderForm(order)
    initialOrderPrice = form.total_price
    canEditCurrentSplitPrice = false
    writeFormState(root, form)
    const totalPriceInput = query<HTMLInputElement>(root, '#price')
    if (totalPriceInput) totalPriceInput.disabled = true
    if (typeof order.split_id === 'string' && order.split_id !== '' && canEditSplitPrice) {
      const members = (await fetchSplitMembers(order.split_id).catch(() => [])) ?? []
      const anchor = members.find((member) => member.split_is_balance_anchor)
      if (anchor !== undefined && anchor.order_no !== orderNo && totalPriceInput) {
        totalPriceInput.disabled = false
        canEditCurrentSplitPrice = true
      }
    }
    if (gameSelect) formRender('select')
    if (form.game !== null) await onGameChange(form.game)
    if (form.server !== null) {
      state.server = form.server
      setServerSelection(form.server)
    }
    // 来源也是「运行期注入的下拉」，`writeFormState` 覆盖不到 ⇒ 像 `setServerSelection` 一样
    // 显式选中；不回填的话编辑保存会把来源改没/改不动。
    // 同时记下**进来时**的值（I1）：`onSave` 靠它区分「用户清空了」与「本来就没有」。
    initialOrderSource = form.order_source
    // `false`：编辑态的来源是**这张订单自己带的** ⇒ 算"指定"（读单/保存都按它走）。
    if (form.order_source !== null) setOrderSourceSelection(form.order_source, false)
    if (form.category !== null) {
      const radio = query<HTMLInputElement>(
        root,
        `input[name="projectType"][value="${form.category}"]`,
      )
      if (radio) radio.checked = true
      await onCategoryChange(form.category)
      formRender('radio')
    }
    if (form.project !== null) {
      const box = query<HTMLInputElement>(root, `input[name="dltype"][value="${form.project}"]`)
      if (box) box.checked = true
    }
    const { day, hours } = timeRequirementToDayHours(order.time_requirement)
    setSelectIfPresent('day', day)
    setSelectIfPresent('hours', hours)
    formRender('select')
    formRender('checkbox')
    state = readFormState(root, readServer)
    // `time_requirement` 是合成值：`readFormState` 会按 day/hours 重算。后端那个自由文本若是
    // 认不出的形状（如「长期」）或天数超出下拉范围，重算会把它改掉 ⇒ 模型以**后端原值**为准回填
    //（用户改过 day/hours 再保存时会重新读 DOM，不受这里影响）。
    state.time_requirement = order.time_requirement
    // ⭐ 「接单销售客服」那一格：详情到手才谈得上"预勾现有值"。
    //   - 有权限 ⇒ 按**文本**（名字快照、不是主键）匹配候选项的 `name` 预勾；
    //     匹配不上时把现值插在最前并勾中（⛔ 不许静默丢值）。
    //   - 没权限 ⇒ 那一格只读（纯文本、没有可点 radio），照旧显示现值。
    // ⚠️ 必须在 `state = readFormState(...)` **之后**写在这里，是因为它读的就是刚回来的 `order`。
    renderSalesAgentOptions(root, {
      items: salesAgentOptions,
      currentUserId: viewer?.id ?? null,
      currentUserName: viewerName,
      currentName: order.sales_agent ?? '',
      editing: true,
    })
    // ⛔ **灌完必须再让 layui 建一次皮肤**（缺了它=编辑态那一格点不动）。
    //    机制：`renderSalesAgentOptions` 是**整段 `innerHTML` 换掉** ⇒ 上一次建的
    //    `div.layui-form-radio` 随旧标记一起被删，而 `.layui-form input[type=radio]` 是
    //    `display:none` ⇒ 只剩一颗**看不见也点不到**的裸 input。
    //    为什么「录单能选、编辑订单不能改」：新建态在这次渲染**之后**还会被
    //    `onGameChange` → `onCategoryChange` 里的 `formRender('radio')` 顺手渲一次；
    //    编辑态的最后一次 radio 渲染排在**上面这几行之前** ⇒ 回填后的这一格从来没被渲过。
    //    判据：`__tests__/order-addorder.spec.ts` 的「编辑态回填后必须重渲 radio」那条用例。
    //    与 `restoreSalesAgentRow` 里那一句同口径。
    formRender('radio')
    // ⚠️ **回填之后重记基线**：编辑态的真实初值只有到详情回来才知道（首屏那次按"现值未知"画）。
    //    基线错成首屏那次的话，用户**什么都没改**保存时也会被判成"改过"、白打一次换归属端点。
    initialSalesAgent = readSalesAgentChoice(root, true)
  }

  /**
   * **模型从 DOM 初始化**，而不是从 `emptyAddOrderForm()`：证据件给 6 个金额框写了
   * `value="0"`，原实现也是读 DOM。不从 DOM 起家就会有两套初始值——「没粘贴过」时载荷里是
   * 6 个 `'0'`，「粘贴过」时被 `writeFormState` 按空模型覆盖成不发送：同一个界面两种结果。
   */
  state = readFormState(root, () => null)
  /**
   * 「接单销售客服」那一格的**首屏渲染**（新建 / 编辑都走这一句）。
   * 位置承重：必须在 `root.innerHTML = 证据件` + `renderTemplate` **之后**（那一格是证据件里的
   * 静态容器 `.cf`，内联之前根本不存在），⛔ 也不能放在 `prefillEditOrder` **里面**——
   * 「编辑态没有详情权限」（那一支不拉详情、直接提示一句）就永远渲染不到那一格，
   * 而它那时仍然要**如实显示"现在记的是谁"**（⛔ 空白比只读更糟）。
   * 编辑态这里先按「本单现值未知（空串）」渲染，真值由 `prefillEditOrder` 拉到详情后**再渲染一次**
   * 覆盖；新建态这一次就是终态（默认勾中当前登录人）。
   */
  // ⚠️ `viewerName` 的定义在挂载开头：`prefillEditOrder`（函数声明）也要读它，
  //    写在这里会落进「函数先被调用、`const` 还没初始化」的暂时性死区。
  renderSalesAgentOptions(root, {
    items: salesAgentOptions,
    currentUserId: viewer?.id ?? null,
    currentUserName: viewerName,
    currentName: '',
    editing: editOrderNo !== '',
  })
  // ⛔ 同上：**灌完就得让 layui 建皮肤**（理由见 `prefillEditOrder` 里那一句）。
  //    这一句同时兜住新建态里「后面没有别的 radio 渲染」的那些档：游戏字典为空、
  //    或该分类下没有分类数据——那两种情况下新建态那一格也会退化成**看不见的裸 input**（真浏览器实测）。
  formRender('radio')
  // ⭐ **记下"进来时选的是谁"**：编辑态的换归属判据是"前后主键不同"（那一列**不进** PATCH 载荷）
  //    ⇒ 这个基线就是判据的一半。编辑态先按"现值未知"记（拉到详情后**再记一次**覆盖）；
  //    新建态这一次就是终态（默认勾中当前登录人 ⇒ 提交时不发 `sales_user`，与改动前逐字等价）。
  initialSalesAgent = readSalesAgentChoice(root, editOrderNo !== '')
  if (editOrderNo !== '') {
    // 编辑态**第一发**是 `GET /orders/<no>/`（后端 `retrieve` = any 详情节点）。⚠️ 本页是**独立
    // island 页 URL**、不经路由守卫 ⇒ 门必须做在这里：缺详情节点时**一发都不发**（fail-closed），
    // 并如实说明为什么表单是空的（⛔ 不静默）。
    // ⚠️ 与保存那半是两个**不同**的节点组，互不代替：只持 `order.add_edit` 的角色能建单，
    //    但看不到别人的单 ⇒ 它不该拿到详情。
    if (!canLoadOrderDetail) {
      msg(NO_DETAIL_PERMISSION_MESSAGE)
    } else {
      await prefillEditOrder(editOrderNo)
    }
  } else {
    const firstGame = games[0]
    if (firstGame) await onGameChange(firstGame.id)
  }

  return () => {
    root.removeEventListener('click', onClick, true)
    root.removeEventListener('focusout', onFocusOut)
    root.innerHTML = ''
  }
}

export function mount(root: HTMLElement, ctx: IslandContext): Promise<() => void> {
  return mountOrderAddOrder(root, { onNavigate: ctx.onNavigate })
}

export default { mount } satisfies IslandPage
