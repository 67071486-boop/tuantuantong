/**
 * 【新开 / 编辑系统子账户】页的 **layui island 复刻 + 接真 API**
 * （对标物 `/User/adduser`，title「新开/编辑系统子账户」）。
 *
 * 与静态复刻页（`artifactPage`）的三条区别（`impl-brief-ep.md` §0）：
 *  1. 数据**真的取**（编辑态回填 + 职位/游戏选项来自我们后端）；
 *  2. `保存资料` **真的提交**（新增 `POST /api/accounts/`、编辑 `PATCH /api/accounts/:id/`）；
 *  3. 只有「对标有、我们没有」的功能才提示「尚未开放」（本页：`重新实名`、`推荐人`）。
 *
 * ## 证据（结构从对标取，离线）
 * - 标记与页内 `<style>` → `public/replica/user-adduser.html`
 *   （源 `.tools/recon/suqing1/html/User__adduser.html`；页内 `<style>` 是 L52-79 原文）。
 * - 提交/校验/交互 → `.tools/recon/suqing1/assets/JS2019/User/adduser.js`：
 *   `form.verify` L510-549、`form.on('submit(savebtn)')` L617-698、`layout` L506-509、
 *   职位 tooltip L559-585、`layer.tips('尚未开放')` 的 `resetRnameCertBtn` L757-787。
 * - 我方契约 → `@/api/accounts`（`AccountWritePayload`）、`@/api/config`（`fetchAllGames`）。
 *   字段映射逐条见下面的 `PAYLOAD_MAP` 注释。
 *
 * ## 「当前是新增还是编辑」怎么拿到的
 * **主判据是宿主注入的 `?id=`**：`LayuiIslandHost.vue` 除了 `?page=<key>`，还会把
 * `route.params` 逐个拼进 iframe 的 query（那段是 `8ccd0cf` 加的），而路由是
 * `/system-users/:id/edit` ⇒ 真实 iframe URL 是 `?page=user-adduser&id=9`，
 * `resolveFormMode` 的**第一条分支**就读它。
 *
 * ⚠️ **本段此前写的是反面**（「宿主不带路由参数、search 里只有 `page`、只能靠 referrer」）——
 * 那句在 `8ccd0cf` 之后就是假的，本文按「按实现写」的规矩改成实况（原文按本项目惯例不复原，
 * 差异见 spec `2026-09-22-system-user-form-followup-design.md`）。
 *
 * **`document.referrer` 只是兜底**（第二条分支）：它就是父窗口（Vue 壳）当前的 URL，
 * 同一文档里 `history.pushState` 之后仍然对：
 * - 直接打开 `?page=user-adduser` 自测（没有 `id`、`referrer` 为空）→ **新增**；
 * - 从 `/system-users/9/edit` 里嵌进来（假设哪天宿主不再拼 query）→ `referrer` 是
 *   `…/system-users/9/edit`（**实测**：探针页 pushState 到该路径后新建 iframe，
 *   iframe 内 `document.referrer` 读到同一串）→ **编辑**。
 *
 * 取舍（如实口径）：`referrer` 在**跨源** iframe 下会被裁剪成只剩 origin；本页 iframe 由
 * 宿主用**同源相对路径** `/layui-island/page.html` 加载，所以这条兜底现在成立。
 * ⚠️ 兜底那条腿**已经不承重**了（主判据是 `?id=`），所以「跨源会退化成永远新增」这个风险
 * 也随之消失——留下它只是为了让不经壳的自测与将来的宿主改动都不至于变哑。
 *
 * ## 与对标**有意**不同的地方（都是「没有后端」或有裁定，不是漏做）
 * 1. 入职日期用 **laydate**（对标 adduser.js L506-509 同款）；调用后 `#inputDate` 的属性与
 *    对标 DOM **逐字相同**（`lay-key="1" lay-laydate-id="inputDate"`，**实测两侧**），
 *    而**两侧都没有**日历面板——所以这一格是对标行为的等价复刻，不是「降级」。
 *    ⚠️ 证据件里那一格的 `value="2026-09-20"` 已删（D-0086）：它此前让「值空才补今天」永远不成立，
 *    于是日期**恒为 20 号**；现在新建态补当天、用户可随手改。
 * 2. 「勾选游戏权限」「他的角色职位」两个块由**我们的数据**渲染：
 *    游戏 = `GET /api/business/games/`（对标那 15 个游戏名是服务端按租户渲染的，属租户数据）；
 *    职位 = `GET /api/accounts/roles/`（**取值域 = 当前租户的全部角色名**：内置职位 + 租户自建；
 *    自 2026-09-23 起从「五个常量」改为读租户 `Role` 表，见 `apps/accounts/views.py::roles`）。
 *    ⚠️ **别在这里写死内置职位的个数**（原文写「内置 5 + 自建」）：内置清单的权威出处是
 *    `apps/rbac/seed.py::BUILTIN_ROLE_MODULES`，而 `apps/business/roles.py::DEFAULT_ROLES`
 *    是**供给时**的另一份清单——**两者不是同一个数**（2026-10-12 线 D 给
 *    `BUILTIN_ROLE_MODULES` 加了内置「人事」，`DEFAULT_ROLES` 仍是原来那 5 个）。
 *    ⚠️ **这里不声称两边的角色集合相同**：对标的内置角色数与集合与我们不同
 *    （`docs/specs/2026-09-20-rbac-permission-system-design.md` 记对标 9 个内置角色），
 *    本轮只核过**形状**（名字字符串数组）与**取值域来源**，**没有**对拍对标的内置角色清单。
 *    **内层标记结构逐字照抄对标**（`div.layui-inline` + 内联 style）。
 * 3. 「重新实名」（`.resetRnameCertBtn`）对标打 `/UserAjax/ResetRnameCert`，我们没有 → 点了提示
 *    「尚未开放」。`recommend`（推荐人）对标由 `/UserAjax/GetAllUserByGameName` 灌
 *    formSelects，我们**没有对应接口**：它对标的控件在新建态就是空的（`id="recommend"` 隐藏域），
 *    可见页面上没有这个字段，所以不渲染、也不发这个键。
 * 4. **`业务类型` 的顺序与默认项**按业务方裁定改了（`1 专做代练(默认)` → `2 专做陪玩` →
 *    `0 代练+陪玩`，对标是 `0` 打头且为默认）；**`录的单不发短信` 整格删除**（我们不做短信，
 *    后端字段与数据库列同一次删掉）。两条的代价登记在 `FRONTEND-REPLICA-STANDARD.md` §7，见 D-0086。
 * 5. 对标有、**我们后端也真有**的字段：`base_salary`（基本底薪，对标 `display:none` 隐藏）、
 *    `id_card_no`（sid）、`remark` 等在 `AccountWritePayload` 里都有对应键，逐条见 `PAYLOAD_MAP`。
 *    ⇒ 本页**没有**「对标有值、我方无处可存」的字段（`录的单不发短信` 是反例，已按裁定删除，见第 4 条）。
 *    ⚠️ 别在这里写死「对标 27 个表单项」这类计数：删掉一格之后分母就变了（E-0011 的同族）。
 *
 * ## 校验：本地判据只来自对标 adduser.js（不是我方发明的）
 * 对标是 `lay-verify` + 提交前那串 `layer.msg`（L510-549 + L617-657）。我们照抄它的**文案与
 * 判据**（正则原样），但**不用 layui 的 `form.verify` 机制**——那要 `lay-submit` 注册
 * `form.on('submit(savebtn)')`，而我们本来就要在同一个回调里做「本地校验 → 发请求 → 提示」，
 * 拆两处会让错误文案有两个出处。前缀 `登录账号`/`姓名/名称` 的文案逐字取自对标 usernames 校验。
 * 长度/唯一性/金额区间的**服务端**判据（1000/1001/1003/1004）一律原样显示后端 msg
 * （`cannot 移除最后一个管理员` 这类是判据层不变量，前端不复制）。
 */
import {
  createAccount,
  fetchAccountRoles,
  getAccount,
  updateAccount,
  type AccountRole,
  type AccountRow,
  type AccountWritePayload,
} from '@/api/accounts'
import { fetchAllGames, type ConfigRow } from '@/api/config'
import { escapeHtml } from '@/layui-island/escape'
import { bootstrapIsland, initIslandElements, type IslandContext, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'
import { closeIslandLayer, type LayerHostWindow } from '../layerHost'

/**
 * island 页 key ＝ **文件名**（`layui-island/page.ts` 用 `import.meta.glob('./pages/*.ts')`
 * 按文件名注册）。父页 `system-user-list.ts` 的 `addEditUserDialogContent` 拼的 URL 必须与它
 * 逐字一致——`system-user-list.spec.ts` 把「父页 URL / 本常量 / 真实文件名」三方绑在一起。
 */
export const USER_FORM_PAGE_KEY = 'user-adduser'

/** 证据件路径（`public/replica/<key>.html`）。由 key 派生，避免「改文件名忘改这里」。 */
const ARTIFACT_URL = `replica/${USER_FORM_PAGE_KEY}.html`

/** 对标 `<title>`（`User__adduser.html` L5，服务端渲染的；island 外壳只有全站的「团团通」）。 */
const PAGE_TITLE = '新开/编辑系统子账户'

/** 我方 API 路径里要用的 id（从 `document.referrer` / query 解析，见文件头）。 */
export type UserFormMode = { mode: 'new' } | { mode: 'edit'; id: number }

/**
 * 从两串 URL 里解析「新增 / 编辑」。**纯函数**，便于单测（spec 直接喂字符串）。
 *
 * 判据顺序：显式 `?id=`（自测/将来宿主可直接用）→ 父窗口 URL 的
 * `/system-users/<数字>/edit`。`referrerUrl === ''`（直接打开 iframe）→ 新增。
 */
export function resolveFormMode(search: string, referrerUrl: string): UserFormMode {
  const fromQuery = new URLSearchParams(search).get('id')
  const queryId = Number(fromQuery)
  if (fromQuery !== null && Number.isInteger(queryId) && queryId > 0) {
    return { mode: 'edit', id: queryId }
  }
  const matched = /\/system-users\/(\d+)\/edit(?:[/?#]|$)/.exec(referrerUrl)
  if (matched) {
    const id = Number(matched[1])
    if (Number.isInteger(id) && id > 0) return { mode: 'edit', id }
  }
  return { mode: 'new' }
}

/** 对标 adduser.js 的 `loginAcc` 校验正则（L518）——**逐字**，含 `\s` 与 `·`。 */
const LOGIN_ACC_RE = /^[a-zA-Z0-9_\u4e00-\u9fa5\s·]+$/
/** 对标 `userName` 校验正则（L526）。 */
const USER_NAME_RE = /^[a-zA-Z0-9_\u4e00-\u9fa5\s·]+$/
/** 对标 `VerifyMoblie` 的判据：11 位、`1` 开头、第二位 3-9（`adduser.js` L539-544 只查格式）。 */
const MOBILE_RE = /^1[3-9]\d{9}$/
/** 对标 `VerifyMoeny`（抢单价范围：0-99999 的纯数字）。 */
const MONEY_RE = /^\d{1,5}$/

/**
 * **题名 → 我方载荷键**的映射。左侧是对标 `User__adduser.html` 里 `name=` 的原文
 * （也是我们证据件里的 `name`，两者逐字一致——所以 DOM 结构与对标可比），
 * 右侧是 `AccountWritePayload` 的键。**数值/布尔类在 `buildPayload` 里转换**。
 */
export const PAYLOAD_MAP = {
  loginAcc: 'username',
  userName: 'display_name',
  sid: 'id_card_no',
  bussType: 'business_type',
  userType: 'member_type',
  basicSalary: 'base_salary',
  autoBrokerageType: 'commission_mode',
  brokerageAmount: 'commission_value',
  setAlipayAcc: 'alipay_account',
  setAlipayAccName: 'alipay_name',
  setBankName: 'bank_name',
  setBankCardNo: 'bank_card_no',
  recOrderMaxCount: 'grab_limit_dl',
  recOrderMaxCount2: 'grab_limit_pw',
  recPriceMin: 'grab_price_min',
  recPriceMax: 'grab_price_max',
  livestreaming: 'live_url',
  qq: 'qq',
  moblie: 'mobile',
  inputDate: 'hired_on',
  tips: 'tag',
  remark: 'remark',
  insureAmount: 'deposit_amount',
} as const

export type FormValues = Record<string, string>

/** 读表单值。对标用 `data.field`（layui 的 `form.on('submit')` 第二个参数）。 */
export function readTextValues(form: LayuiModule): FormValues {
  return (form.val as (f: string) => FormValues)('sform') ?? {}
}

/** 勾选的游戏（对标 `$('input[name="games"]:checked')`，L641-643）。 */
export function readCheckedGames(root: HTMLElement): number[] {
  return Array.from(root.querySelectorAll<HTMLInputElement>('input[name="games"]:checked'))
    .map((el) => Number(el.value))
    .filter((v) => Number.isInteger(v) && v > 0)
}

/**
 * 「工作室」勾选（D-0228，我方自有格：对标没有，证据件副本加的 checkbox `isStudio`）。
 * 直接查 DOM 的 property（layui 复选不改 property；与 `readCheckedGames` 同一出口风格）。
 */
export function readIsStudio(root: HTMLElement): boolean {
  const el = root.querySelector<HTMLInputElement>('input[name="isStudio"]')
  return el?.checked === true
}

/** 选中的职位（对标 `$("input[name='userRole']:checked").val()`，L649）。 */
export function readCheckedRole(root: HTMLElement): AccountRole | '' {
  const el = root.querySelector<HTMLInputElement>('input[name="userRole"]:checked')
  return (el?.value ?? '') as AccountRole | ''
}

/** 空串 → `undefined`（= **不发这个键**）。后端 PATCH 语义是「缺席即保持库里的值」。 */
function numOrUndefined(raw: string): number | undefined {
  const text = raw.trim()
  if (text === '') return undefined
  const value = Number(text)
  return Number.isFinite(value) ? value : undefined
}

/**
 * 组装载荷。**只发本表单有的键**；空的可空字段**整键不发**——这不是省事：后端 PATCH 的
 * 「缺席 = 保持原值」语义要求如此（`apps/accounts/views.py::_persist_account`），
 * 把它写成 `null` 会变成「清空该字段」，与用户「没动这一格」的意图不同。
 */
export function buildPayload(values: FormValues, games: number[], role: AccountRole, isStudio = false): AccountWritePayload {
  // ⚠️ 取值键是**对标的 `name`**（`values` 是从 DOM 读来的），落到载荷的键才是
  // `PAYLOAD_MAP[name]`。两者**不能反**：上一版写成 `values[PAYLOAD_MAP[name]]`，
  // 于是每个字段都读到 `undefined`（而 `?? fallback` 让它静默变成空串/默认值）。
  const text = (name: keyof typeof PAYLOAD_MAP): string => (values[name] ?? '').trim()

  const payload: AccountWritePayload = {
    username: text('loginAcc'),
    display_name: text('userName'),
    role,
    games,
    // ⚠️ 兜底值是 **`1`（专做代练）**，与后端 `BusinessType` 的默认值同源（D-0086）。
    //    radio 正常渲染时这里是它的选中值；兜底只在「radio 没渲染出来」这种异常态生效。
    business_type: numOrUndefined(values.bussType ?? '') ?? 1,
    member_type: numOrUndefined(values.userType ?? '') ?? 0,
    // 工作室勾选（D-0228）：布尔始终发送（表单永远有态；编辑时预填当前值）。
    is_studio: isStudio,
    commission_mode: numOrUndefined(values.autoBrokerageType ?? '') ?? 20,
    id_card_no: text('sid'),
    alipay_account: text('setAlipayAcc'),
    alipay_name: text('setAlipayAccName'),
    bank_name: text('setBankName'),
    bank_card_no: text('setBankCardNo'),
    live_url: text('livestreaming'),
    qq: text('qq'),
    mobile: text('moblie'),
    tag: text('tips'),
    remark: text('remark'),
  }
  const optionalNumbers: (keyof typeof PAYLOAD_MAP)[] = [
    'basicSalary',
    'brokerageAmount',
    'recOrderMaxCount',
    'recOrderMaxCount2',
    'recPriceMin',
    'recPriceMax',
    'insureAmount',
  ]
  for (const name of optionalNumbers) {
    const value = numOrUndefined(values[name] ?? '')
    // ⚠️ 别把循环变量写成 `key`：那会遮住 `values` 的键名空间，让「读哪个键」看不出来。
    if (value !== undefined) (payload as Record<string, unknown>)[PAYLOAD_MAP[name]] = value
  }
  if ((values.inputDate ?? '').trim() !== '') payload.hired_on = (values.inputDate ?? '').trim()
  return payload
}

/**
 * 本地校验。**文案与判据逐条取自对标** `adduser.js`：
 * - `loginAcc`：非空（L511-514）、长度 ≥2（L515-517）、字符集（L518-520）；
 * - `userName`：非空（L522-525）、字符集（L526-528）、首尾下划线（L529-531）、
 *   含空格（L532-534）、长度 ≥2（L535-537）；
 * - `moblie`：填了才校验格式（L539-544 / L626-631）；
 * - `bussType`：必须选（L632-635）——radio 默认选中，所以实际只会在值异常时命中；
 * - 抢单价范围：`VerifyMoeny`（L636-639）；
 * - 游戏 ≥1（L644-647）、职位必选（L649-657）。
 *
 * ⚠️ 与对标**有意**不同的一处：对标的 `lay-verify="required|number"` 会让
 * `basicSalary`/`brokerageAmount`/`insureAmount` 这类「必填数字」在非数字时**由 layui 提前拦下**；
 * 我们在同一个函数里等价实现（非空 + 数字），文案沿用对标 `lay-verify` 的字段题名。
 * 后端 `CREATE_REQUIRED_FIELDS` 的六个新增必填仍以后端为准（编辑 PATCH 缺席即保持）。
 */
export function validateForm(
  values: FormValues,
  games: number[],
  role: AccountRole | '',
  isEdit: boolean,
): string {
  const loginAcc = (values.loginAcc ?? '').trim()
  if (loginAcc === '') return '登录账号不能为空'
  if (loginAcc.length < 2) return '登录账号长度最小是2位，请补齐'
  if (!LOGIN_ACC_RE.test(loginAcc)) return '登录账号不能有特殊字符'

  const userName = (values.userName ?? '').trim()
  if (userName === '') return '姓名/名称不能为空'
  if (!USER_NAME_RE.test(userName)) return '姓名/名称不能有特殊字符'
  if (/(^_)|(__)|(_+$)/.test(userName)) return '姓名/名称首尾不能出现下划线'
  if (userName.indexOf(' ') > -1) return '姓名/名称不能有空格'
  if (userName.length < 2) return '姓名/名称长度最小是2位，请补齐'

  const mobile = (values.moblie ?? '').trim()
  if (mobile !== '' && !MOBILE_RE.test(mobile)) return '输入的联系电话格式不正确，要么请留空'

  if ((values.bussType ?? '') === '') return '请选择业务类型'
  if (!MONEY_RE.test((values.recPriceMin ?? '').trim()) || !MONEY_RE.test((values.recPriceMax ?? '').trim())) {
    return '内部抢单价格范围格式不正确，只能填数字0到99999'
  }
  if (games.length < 1) return '请为该用户至少选择一款游戏权限'
  if (role === '') return '请为该用户选择一个职位权限'

  // 数字类（对标 `lay-verify="required|number"`）：填了就必须是数字。
  for (const [key, label] of [
    ['basicSalary', '基本底薪'],
    ['brokerageAmount', '提成金额/百分比'],
    ['recOrderMaxCount', '内部抢代练单上限'],
    ['recOrderMaxCount2', '内部抢陪玩单上限'],
    ['insureAmount', '已交保证金'],
  ] as const) {
    const raw = (values[key] ?? '').trim()
    if (raw !== '' && !/^\d+(\.\d+)?$/.test(raw)) return `${label}只能填数字`
  }

  // 对标的 `inputDate` 是 `lay-verify="required"`（L239），新增时必填。
  if (!isEdit && (values.inputDate ?? '').trim() === '') return '请选择入职日期'
  // 后端 `CREATE_REQUIRED_FIELDS` 的新增必填（`commission_value` 也在其中）。
  if (!isEdit) {
    if ((values.brokerageAmount ?? '').trim() === '') return '请填写提成金额/百分比'
  }
  return ''
}

/** 一行「内联控件」的标记。**内层结构逐字照抄对标**（游戏 L272-274 / 职位 L323-325）。 */
function inlineMarkup(
  inner: string,
  style: string,
  indent: string,
): string {
  return `${indent}<div class="layui-inline" style="${style}">\n${indent}    ${inner}\n${indent}</div>`
}

/** 对标游戏那一格的内联 style（L272）；职位那一格是 `width:130px; margin-top:0px`（L323）。 */
const GAME_INLINE_STYLE = 'margin-right:5px; margin-top:0px'
const ROLE_INLINE_STYLE = 'width:130px; margin-top:0px'

/**
 * 提成模式三档（对标 `autoBrokerageType` 的 value）。
 * ⚠️ 用**字面值**而不是从别处 import 常量：这三个值是对标实测的协议值，
 * 单测也就能直接写字面值断言（E-0024：别拿同一个符号自指）。
 */
export const COMMISSION_MODE_FIXED = 10
export const COMMISSION_MODE_ORDER_PERCENT = 20
export const COMMISSION_MODE_PROFIT_PERCENT = 30

/**
 * 新建子账户成功后「已把凭据复制到剪贴板」的提示文案（对标 `adduser.js:292-293` 逐字）。
 */
export const COPY_TOAST_OK = '已成功复制到剪贴板'
export const COPY_TOAST_FAIL = '复制失败了'

/**
 * 复制成功到回列表之间的等待（对标 `adduser.js:740` 的 `1200`）。
 * 对标在**没拿到 `copyStr`** 时是 `0`（立刻 reload）：我们这里既然**总是**要复制，
 * 就总是等 1200ms，让用户看得见提示。
 */
export const COPY_BACK_DELAY_MS = 1200

/**
 * 凭据弹窗的**正文**——对标 `adduser.js:715` 弹的是服务端给的 `data.msg`。
 *
 * 2026-09-23 在对标站真建了一个子账号把原文抓回来（抓完当场把那个号删了），**逐字**是这条模板：
 * `新开子账户成功，该用户登录账号：<登录账号>，初始登录密码：<初始密码>`
 * （分隔符实测是**全角逗号** `，` 与**全角冒号** `：`，中间没有空格）⇒ 照抄。
 * ⚠️ 抓回来的原文里带着对标租户标识与那对临时凭据，按 PII 守卫（`pii-guard.spec.ts` 的真值黑名单）
 * **不入库**，这里只留模板。
 *
 * ⚠️ 返回的是**纯文本**；要插进 `layer.alert` 的 HTML 里，调用方整串过 `escapeHtml`
 * （账号是用户可控的，见 E-0074 同族的 XSS 提醒）。
 */
export function credentialDialogText(username: string, password: string): string {
  return `新开子账户成功，该用户登录账号：${username}，初始登录密码：${password}`
}

/**
 * 进剪贴板的文本。对标进剪贴板的**不是**弹窗正文，而是服务端另给的 `data.copyStr`；
 * 实测它比弹窗那句**多**开头一截「<租户公司名>管理系统网址：<对标站域名>」+ **一个半角空格**，
 * 后两段是：
 * `你的登录账号：<登录账号> 初始登录密码：<初始密码>`（同样半角空格分隔）。
 *
 * ⛔ **有意偏离**（业务方裁定）：开头那截**不复制**——前端手里没有租户公司名，
 * 我们也不愿把域名写死进复制内容。所以只发账号与密码两段（**照抄**对标的后两段）。
 */
export function credentialClipboardText(username: string, password: string): string {
  return `你的登录账号：${username} 初始登录密码：${password}`
}

/**
 * 把文本写进剪贴板。
 *
 * 对标用的是 ClipboardJS 插件（`adduser.js:735`）。这里**有意不引入它**：
 * 仓库规定前端不新增运行时依赖，而 `navigator.clipboard` + `execCommand` 兜底
 * 已覆盖同一片浏览器。⚠️ 两者都异步/可能被拒 ⇒ 返回 Promise，调用方据此选文案。
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  const legacy = (): boolean => {
    const el = document.createElement('textarea')
    el.value = text
    // 放到视口外但仍可选中：`display:none` 会让 execCommand 失败
    el.setAttribute('readonly', '')
    el.style.position = 'fixed'
    el.style.top = '-1000px'
    el.style.opacity = '0'
    document.body.appendChild(el)
    el.select()
    let ok = false
    try {
      ok = document.execCommand('copy')
    } catch {
      ok = false
    }
    document.body.removeChild(el)
    return ok
  }

  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // 非安全上下文/无权限 ⇒ 落到 execCommand
    }
  }
  return legacy()
}

/**
 * 「提成金额/百分比」那个标签该显示什么（对标 `adduser.js:587-596` 逐字）。
 *
 * 对标原文只有 `10 → 固定提成金额`、`20/30 → 百分比数值` 三支，**未知值它什么都不做**
 * （标签停在原地）。我们这里把未知值也归到「百分比数值」：它是**显示**文案、
 * 不是判据，而 20 是后端的默认模式 ⇒ 异常态下显示后端默认语义比显示一个陈旧文案更不容易误导。
 * ⚠️ 这条与对标**有意更正确**的一点是「挂载时就算一次」：对标初进页面时标签仍是
 * 原始的「提成金额/百分比」，用户得点一下模式才变（见 D-0086 与复刻标准 §7）。
 */
export function commissionLabel(mode: number): string {
  return mode === COMMISSION_MODE_FIXED ? '固定提成金额' : '百分比数值'
}

/**
 * 提成金额那一格获得焦点时的提示文案（对标 `adduser.js:597-608` 逐字，含标点与例子）。
 * 对标把 `20` 与 `30` 合并成同一句，所以这里也只有两句。
 */
export function commissionTip(mode: number): string {
  return mode === COMMISSION_MODE_FIXED
    ? '请输入固定的金额，例如：5、10，默认为0'
    : '请输入百分比纯数字（不要有百分号），例如：5，代表5%'
}

/**
 * 当前选中的提成模式（读 DOM 的 `:checked`）。拿不到时回落 `20`（我方后端默认模式）。
 */
export function readCommissionMode(root: HTMLElement): number {
  const el = root.querySelector<HTMLInputElement>('input[name="autoBrokerageType"]:checked')
  const value = Number(el?.value ?? '')
  return Number.isFinite(value) && value > 0 ? value : COMMISSION_MODE_ORDER_PERCENT
}

/** 把标签文案写成当前模式对应的那一句。挂载时调用一次，radio 变化时再调用。 */
export function applyCommissionLabel(root: HTMLElement): void {
  const label = root.querySelector<HTMLElement>('.tcLabel')
  if (label) label.innerHTML = commissionLabel(readCommissionMode(root))
}

/**
 * 渲染游戏勾选框。对标原文（服务端按租户渲染）：
 * `<input type="checkbox" name="games" lay-skin="tag" value="<游戏名>" title="<游戏名>" />`，
 * `value` 就是**游戏名**（不是 id）。我们的 `AccountWritePayload.games` 要的是 **id**，
 * 所以 `value` 放 id、`title` 放名字——`value` 不可见，`title` 才决定渲染出来的标签文案，
 * 于是可见结果与对标同形（「游戏名」标签），而选中值是我们能用的 id。
 */
export function gamesMarkup(games: readonly ConfigRow[], checked: readonly number[]): string {
  const chosen = new Set(checked)
  return games
    .map((g) =>
      inlineMarkup(
        `<input type="checkbox" name="games" lay-skin="tag" value="${g.id}" title="${g.name}"${chosen.has(g.id) ? ' checked' : ''} />`,
        GAME_INLINE_STYLE,
        ' '.repeat(44),
      ),
    )
    .join('\n')
}

/**
 * 渲染职位单选。对标是 `<input type="radio" name="userRole" value="<职位id>" data-name="<职位名>"
 * title="<职位名>" lay-filter="roleFilter" />`；我们只有职位**名字**（`string[]`）,
 * 所以 `value` 直接用名字——提交时后端按**请求期**校验这个名字是当前租户的 `Role`
 * （`apps/accounts/serializers.py::validate_role_exists`），名字就是它要的那个字符串。
 */
export function rolesMarkup(roles: readonly AccountRole[], checked: AccountRole | ''): string {
  return roles
    .map((r) =>
      inlineMarkup(
        `<input type="radio" name="userRole" value="${r}" data-name="${r}" title="${r}" lay-filter="roleFilter"${r === checked ? ' checked' : ''} />`,
        ROLE_INLINE_STYLE,
        ' '.repeat(44),
      ),
    )
    .join('\n')
}

/** 今天我（YYYY-MM-DD）：对标 `inputDate` 的服务端默认值就是「今天」。 */
function today(): string {
  const d = new Date()
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/** 编辑态回填：把详情里的值写回**对标 name** 的输入框。 */
export function fillForm(root: HTMLElement, account: AccountRow): void {
  const set = (selector: string, value: string): void => {
    const el = root.querySelector<HTMLInputElement>(selector)
    if (el) el.value = value
  }
  const money = (v: number | string | null | undefined): string =>
    v === null || v === undefined || v === '' ? '' : String(Number(v))
  /**
   * 回填一个「对标带 `lay-verify="required"`」的数字框。
   *
   * ⛔ **库里没值时不许写成空串**（2026-09-23 业务方报的缺陷：编辑子账户点「保存资料」一直被拦，
   * 文案笼统「必填项不能为空」，看不出是哪一格，全填了也存不了）：
   * 对标那几个框的**服务端总会渲染一个值**——证据件 `public/replica/user-adduser.html` 里
   * `brokerageAmount` / `recOrderMaxCount` / `recOrderMaxCount2` / `insureAmount` / `basicSalary`
   * 都是 `value="0"`、`recPriceMax` 是 `value="99999"` ⇒ 它们的 `lay-verify="required"` **从不生效**。
   * 而我们的详情接口：**没有档案行**的账号（如 `seed_tenant` 灌的 `admin`）这些键**整个缺席**，
   * 有档案行的账号也可能是 `null`（`base_salary` / `grab_price_min` / `grab_price_max` 都是 `null=True`）。
   * 写成空串就会触发 layui 提交前 `validate($('*[lay-verify]'))` 的 `required` 规则
   * （源码 `public/layui/layui.js`：`required:function(e){if(!/[\S]+/.test(e)||e===undefined||null===e)return '必填项不能为空'}`；
   * 其余规则都是 `if(e&&…)` ⇒ **空值只被 `required` 挡**）⇒ 页面注册的 `submit(savebtn)` **根本不会执行**。
   * ⚠️ 其中最狠的是 `basicSalary`：那一格是 `style="display:none"`（对标原文亦然）⇒ **用户填不了**。
   * ⇒ 库里没值时**保留标记里的对标默认值**（= 对标服务端总会给一个值的行为）。
   * ⛔ 别把这里改成硬编码 0：默认值的唯一出处是**证据件本身**，改证据件时要一起动。
   */
  const setRequiredNumber = (selector: string, value: string): void => {
    if (value === '') return
    set(selector, value)
  }

  set('input[name="loginAcc"]', account.username)
  set('input[name="userName"]', account.display_name)
  set('input[name="sid"]', account.id_card_no ?? '')
  setRequiredNumber('input[name="brokerageAmount"]', money(account.commission_value))
  set('input[name="setAlipayAcc"]', account.alipay_account ?? '')
  set('input[name="setAlipayAccName"]', account.alipay_name ?? '')
  set('input[name="setBankName"]', account.bank_name ?? '')
  set('input[name="setBankCardNo"]', account.bank_card_no ?? '')
  // ⚠️ `money()` 同时吃掉 `null` / `undefined`（旧写法只挡 `undefined` ⇒ `String(null)` 会写出
  //    字面量 `"null"`，那连 `lay-verify="number"` 都过不去）。
  setRequiredNumber('input[name="recOrderMaxCount"]', money(account.grab_limit_dl))
  setRequiredNumber('input[name="recOrderMaxCount2"]', money(account.grab_limit_pw))
  setRequiredNumber('input[name="recPriceMin"]', money(account.grab_price_min))
  setRequiredNumber('input[name="recPriceMax"]', money(account.grab_price_max))
  set('input[name="livestreaming"]', account.live_url ?? '')
  set('input[name="qq"]', account.qq ?? '')
  set('input[name="moblie"]', account.mobile ?? '')
  set('input[name="inputDate"]', account.hired_on ?? '')
  set('input[name="tips"]', account.tag ?? '')
  set('input[name="remark"]', account.remark ?? '')
  setRequiredNumber('input[name="insureAmount"]', money(account.deposit_amount))
  setRequiredNumber('input[name="basicSalary"]', money(account.base_salary))
  // 单选/复选：先把 DOM 的值对好，再交给 `form.render()` 画 UI（顺序不能反）。
  // ⚠️ 用 **`checked` 属性**而不是 `.checked` 属性值：layui 的 `form.render()` 读的是
  //    **attribute**，只写 property 会在它渲染时被读成「未选中」而**静默复位**（实测踩到：
  //    测试脚本里 `.checked = true` 后 `form.render()` 一跑，选中的又变回第一项）。
  const radio = (name: string, value: string): void => {
    root.querySelectorAll<HTMLInputElement>(`input[name="${name}"]`).forEach((el) => {
      const on = el.value === value
      el.checked = on
      if (on) el.setAttribute('checked', 'checked')
      else el.removeAttribute('checked')
    })
  }
  radio('bussType', String(account.business_type ?? 1))
  radio('userType', String(account.member_type ?? 0))
  radio('autoBrokerageType', String(account.commission_mode ?? 20))
  // 工作室勾选预填（D-0228）：与 radio 同一纪律——写 attribute，`form.render()` 读的是它。
  const studioEl = root.querySelector<HTMLInputElement>('input[name="isStudio"]')
  if (studioEl !== null) {
    const on = account.is_studio === true
    studioEl.checked = on
    if (on) studioEl.setAttribute('checked', 'checked')
    else studioEl.removeAttribute('checked')
  }
  radio('userRole', account.role ?? '')
  radio('games', '')
  root.querySelectorAll<HTMLInputElement>('input[name="games"]').forEach((el) => {
    const on = account.games.includes(Number(el.value))
    el.checked = on
    if (on) el.setAttribute('checked', 'checked')
    else el.removeAttribute('checked')
  })
}

/** 错误提示。`layui.layer.msg`（对标同一处也用 `layer.msg`）。 */
function makeMsg(layer: LayuiModule): (text: string) => void {
  return (text: string) => (layer.msg as (t: string, o?: unknown) => void)(text, { time: 2000 })
}

/** 挂载页面。返回卸载函数（切路由时调用）。 */
export async function mountUserFormPage(
  root: HTMLElement,
  options: { onNavigate: (name: string, params?: Record<string, string>) => void; mode: UserFormMode },
): Promise<() => void> {
  const layui: LayuiNamespace = await bootstrapIsland(['form', 'layer', 'laydate'])
  const form = layui.form
  const layer = layui.layer
  const msg = makeMsg(layer)

  document.title = PAGE_TITLE

  const artifact = await fetch(`${import.meta.env.BASE_URL}${ARTIFACT_URL}`)
  if (!artifact.ok) throw new Error(`证据件取不到（HTTP ${artifact.status}）：${ARTIFACT_URL}`)
  root.innerHTML = await artifact.text()
  // 表单页没有页签，但渲染器约定「自己写 mount 也要调」——它只做一次幂等的结构绑定。
  initIslandElements(layui)

  const isEdit = options.mode.mode === 'edit'
  const accountId = options.mode.mode === 'edit' ? options.mode.id : 0

  // 选项先并行取，避免「先渲染空块、再画第二遍」的闪烁。
  const [roles, games, account] = await Promise.all([
    fetchAccountRoles().catch((e: unknown) => {
      msg(e instanceof Error ? e.message : '职位列表加载失败')
      return [] as AccountRole[]
    }),
    // ⚠️ 游戏权限多选取**全量**（`fetchAllGames`）：要能勾上这个账号**当前没有**的游戏，
    //    收窄了就是「看不到没勾的游戏 ⇒ 也就勾不上」的死锁（计划 DoD #4）。
    fetchAllGames().catch((e: unknown) => {
      msg(e instanceof Error ? e.message : '游戏列表加载失败')
      return [] as ConfigRow[]
    }),
    isEdit
      ? getAccount(accountId).catch((e: unknown) => {
          msg(e instanceof Error ? e.message : '账号详情加载失败')
          return null
        })
      : Promise.resolve(null),
  ])

  const gamesBox = root.querySelector<HTMLElement>('#gamesBox')
  const roleBox = root.querySelector<HTMLElement>('#roleBox')
  if (gamesBox) gamesBox.innerHTML = gamesMarkup(games, account?.games ?? [])
  if (roleBox) roleBox.innerHTML = rolesMarkup(roles, account?.role ?? '')

  if (account) fillForm(root, account)
  // 入职日期：对标服务端给「今天」，编辑态用库里那天的值（`fillForm` 已写）。
  const hiredOn = root.querySelector<HTMLInputElement>('input[name="inputDate"]')
  if (hiredOn && hiredOn.value === '') hiredOn.value = today()

  // laydate：对标 adduser.js L506-509 同款调用。**实测两侧的落点完全相同**：调用后
  // `#inputDate` 都被打上 `lay-key="1" lay-laydate-id="inputDate"`（对标 DOM 逐字相同），
  // 而**两侧都没有** `.layui-laydate` 面板（对标那份也不出面板）——所以日期在这一版里
  // 就是「可输入的文本框 + 同一个属性」，不存在「我们比对标少一个控件」的差异。
  // 模块/方法缺失时（将来换构建）自然退化成纯文本框，不阻断提交，故这里判能力再调。
  const laydate = layui.laydate as LayuiModule | undefined
  if (laydate && hiredOn && typeof laydate.render === 'function') {
    ;(laydate.render as (o: unknown) => void)({ elem: '#inputDate', type: 'date' })
  }

  // 所有控件就位后一次性渲染（layui 把 radio/checkbox 换成自己的 DOM）。
  ;(form.render as (t?: string) => void)()

  // 职位 tooltip：对标 adduser.js L559-585 的 `form.on('radio(roleFilter)')`。
  // 逐条照抄文案（它是「这个职位的权限有多大」的操作提示，不是数据）。
  const ROLE_TIPS: readonly [string, string][] = [
    ['系统管理员', '是系统内最高权限，拥有所有操作权限，正常情况下建议设置此职位的子账户数量不超过3个'],
    ['超级管理员', '是系统内最高权限，拥有所有操作权限，正常情况下建议设置此职位的子账户数量不超过3个'],
    ['总经理', '的权限仅次于系统管理员，拥有系统内绝大部分操作权限，默认情况下没有该职位设定数量不要超过2个'],
    ['客服', '一般用于接单录单销售的人用，拥有录单、查单、管理订单等通用权限，默认情况下没有删除订单权限'],
    ['代练员', '为最低权限位，适合给代练员、打手、团员或下家使用。这个职位的人只能看到指派给TA的单子，看不到别人的，也看不到接单价'],
    ['开发', '开发人员专用权限，仅用于开发人员协助本系统解决问题时使用，若担心安全，可以删除'],
    ['客服主管', '默认情况下，除了拥有客服的权限外，还拥有删除订单、更改接单价、新开系统子账号、管理子账号、管理打手与客服提成账单等高级功能，一般建议只给部门主管级人员使用'],
    ['外部录单', '适合给合作的销售用，可以录单、编辑,只能看到自己录的单子，看不到别人录的，也没有派单、删除权限'],
  ]
  ;(form.on as (e: string, cb: (obj: { elem: HTMLInputElement }) => void) => void)(
    'radio(roleFilter)',
    (obj) => {
      const name = obj.elem.title
      const hit = ROLE_TIPS.find(([key]) => name.indexOf(key) > -1)
      if (hit) (layer.tips as (t: string, o: unknown, o2?: unknown) => void)(name + hit[1], obj.elem)
    },
  )

  // ── 提成模式：标签文案 + 金额格焦点提示（对标 adduser.js L587-608，D-0086）──
  // ⚠️ 顺序是承重点，两者都排在上面那次 `form.render()` **之后**：
  //    - `applyCommissionLabel` 读的是 `:checked`，layui 渲染前那批原生 radio 的
  //      `checked` 状态才是准的；渲染后它把原生 input 藏起来、状态同步到自己的 DOM 上，
  //      此时 `:checked` 仍然读得到（我们不移除原生 input）；
  //    - 初值必须**主动算一次**（不能等用户点）：编辑一个「固定金额」模式的账号时，
  //      证据件里的标签原文是「提成金额/百分比」，不主动算就会一直错到用户手点一下。
  applyCommissionLabel(root)
  ;(form.on as (e: string, cb: (obj: { value?: string }) => void) => void)(
    'radio(autoBrokerageTypeFilter)',
    () => applyCommissionLabel(root),
  )

  /**
   * 金额格的焦点提示。对标是 `$("input.layui-input[name='brokerageAmount']").focus(...)`；
   * 我们改用**捕获阶段的事件委托**（与下面 `.resetRnameCertBtn` 的点击同一形状）：
   * `focus` 不冒泡，捕获阶段才能在 `root` 上听到；`root` 每次挂载都是新的，重复绑定不成问题。
   */
  const onFocusIn = (event: Event): void => {
    const el = event.target as HTMLElement | null
    if (!el || el.getAttribute('name') !== 'brokerageAmount') return
    const mode = readCommissionMode(root)
    ;(layer.tips as (t: string, o: unknown, o2?: unknown) => void)(commissionTip(mode), el, {
      tips: [3, '#FF6E97'],
      // 对标：固定金额那句 3000ms、百分比那句 4000ms（`adduser.js:600/604`）。
      time: mode === COMMISSION_MODE_FIXED ? 3000 : 4000,
      tipsMore: false,
    })
  }
  root.addEventListener('focusin', onFocusIn, true)

  /**
   * 保存成功后的「回列表」——**双态出口**。
   *
   * - **在弹层里**（父页 `layer.open({type:2})`，父窗口是**人员管理岛页**）⇒ 关掉承载自己的
   *   那个层，由**父页的 `end` 回调**刷新列表（T6 同款；对标是子页调
   *   `parent.tools.reloadOrderData()`，我方父页没有那个工具 ⇒ 用 `end` 是**有意偏离**）。
   * - **整页路由**（`/system-users/new`、`/system-users/:id/edit`，由 Vue 壳承载）⇒
   *   父窗口是 Vue 壳，`onNavigate` 有效。
   *
   * ⚠️ 必须先判「**真的**关掉了自己」再决定要不要 `onNavigate`：在整页路由下
   * `closeIslandLayer` 回 `false`（父窗口没有 `layer`），不判就会变成「人还在编辑页上，
   * 但什么都没发生」；反之只写 `onNavigate` 的话，弹层里那条 `island:navigate` 的 postMessage
   * 发给了人员管理岛页、而它不监听这个协议 ⇒ 保存成功却什么也不发生（旧实现的静默 no-op）。
   */
  function backToList(): void {
    if (closeIslandLayer(window as unknown as LayerHostWindow)) return
    options.onNavigate('system-users')
  }

  /**
   * 提交。对标 `form.on('submit(savebtn)')`（L617-698）：
   * 本地校验 → 新增走 `POST /api/accounts/`、编辑走 `PATCH /api/accounts/<id>/`
   * → 成功提示；成功后**回列表**（对标成功后 `location.reload()`，编辑态的父页就是列表；
   * 我方两条路线（整页路由 / 弹层）的出口见 `backToList`）。
   */
  ;(form.on as (e: string, cb: (obj: { field?: FormValues }) => void) => void)(
    'submit(savebtn)',
    () => {
      const values = readTextValues(form)
      const checkedGames = readCheckedGames(root)
      const role = readCheckedRole(root)
      const problem = validateForm(values, checkedGames, role, isEdit)
      if (problem !== '') {
        msg(problem)
        return
      }
      const payload = buildPayload(values, checkedGames, role as AccountRole, readIsStudio(root))
      if (isEdit) {
        void updateAccount(accountId, payload).then(
          () => {
            msg('已保存')
            backToList()
          },
          (e: unknown) => msg(e instanceof Error ? e.message : '保存失败'),
        )
        return
      }
      void createAccount(payload).then(
        (res) => {
          msg('账号已创建')
          showInitialPassword(res.account.username, res.initial_password)
        },
        (e: unknown) => msg(e instanceof Error ? e.message : '创建失败'),
      )
    },
  )

  /**
   * 新增成功后的**一次性凭据**（2026-09-23 按对标复刻，业务方报「弹窗不一致」）。
   *
   * 对标 `adduser.js:713-742` 的原文（**逐字**）：
   * ```js
   * layer.alert(data.msg, { icon: 1 }, function () {
   *   if (data.copyStr) {
   *     new ClipboardJS('.copyurl', { text: () => data.copyStr })
   *       .on('success', () => layer.msg('已成功复制到剪贴板', { time: 4000 }))
   *       .on('error',   () => layer.msg('复制失败了',        { time: 1000 }))
   *     $('.copyurl').trigger('click')
   *   }
   *   setTimeout(() => { location.reload(); parent.tools.reloadOrderData() },
   *              data.copyStr ? 1200 : 0)
   * })
   * ```
   * ⇒ 三件事**照抄**：① 弹窗是**标准 `layer.alert`**（不是自造面板）；
   * ② **点「确定」才复制**（不是弹出来就复制），复制的是 `data.copyStr`——⚠️ 它**不等于**弹窗正文
   * （对标那串前面多一截网址，见 `credentialClipboardText`）；
   * ③ 两条提示文案与时长逐字（`已成功复制到剪贴板`/4000ms、`复制失败了`/1000ms），
   * 复制完再等 1200ms 才离开。
   *
   * 「标准 `layer.alert`」长什么样是**实测**的（真 layui 跑一遍读 DOM，不是猜）：
   * 标题「**信息**」、`icon:1` 渲染成绿色对勾 `i.layui-icon-success`、**一颗「确定」**、右上角**有关闭 X**。
   * ⛔ 所以**不能**再传 `closeBtn: 0`（对标有关闭按钮）——点 X 只关层、不复制、也不离开
   * （对标同一个行为：`yes` 只在点「确定」时触发）。
   * ⚠️ 实参也照抄 `{ icon: 1 }`**不传标题**：标题「信息」是 layui 的默认值（对标也没传）。
   *
   * **弹窗正文与剪贴板文本都已在对标站实测抄回**（2026-09-23，真建了一个临时账号、用完当场删）
   * ——正文见 `credentialDialogText`、进剪贴板的那串见 `credentialClipboardText`。
   * ⇒ 我方 2026-09-23 之前那版**自拟**的两行（粗体账号 + 灰字「该密码只显示这一次…」）**已删掉**：
   * 对标弹窗就是**一句话**，多出来的那行正是业务方说的「弹窗不一致」。
   *
   * **两处有意偏离**（都写在这里，别当漏做）：
   * ① **不引 `ClipboardJS`**（对标从 `/lib/clipboard` 载入）：本仓规矩是**不擅自引运行时依赖**，
   *    改用平台 API `navigator.clipboard.writeText` + `execCommand('copy')` 兜底；
   * ② 进剪贴板的文本**去掉了开头那截**「<租户公司名>管理系统网址：<域名>」（业务方裁定，
   *    理由见 `credentialClipboardText`）。
   */
  function showInitialPassword(username: string, password: string): void {
    const clipboardText = credentialClipboardText(username, password)
    ;(layer.alert as (c: string, o: unknown, cb: (i: number) => void) => void)(
      // ⚠️ 正文里的账号/密码是用户可控值 ⇒ 整串过 `escapeHtml`（与页面别处同一把）。
      escapeHtml(credentialDialogText(username, password)),
      { icon: 1 },
      () => {
        // ⚠️ `copyToClipboard` **不 reject**：它 resolve 一个布尔（两条路都失败时是 `false`）。
        //    所以这里必须**判返回值**——写成 `.then(成功文案, 失败文案)` 的话 `false` 也算 fulfilled，
        //    「复制失败了」永远不会出现（2026-09-23 写这页时真的写错过一次，被本页用例逮住）。
        void copyToClipboard(clipboardText).then((ok) => {
          ;(layer.msg as (t: string, o: unknown) => void)(
            ok ? COPY_TOAST_OK : COPY_TOAST_FAIL,
            { time: ok ? 4000 : 1000 },
          )
        })
        // 对标 `setTimeout(..., data.copyStr ? 1200 : 0)`：留 1.2s 让上面那条提示看得见再走。
        window.setTimeout(() => backToList(), COPY_BACK_DELAY_MS)
      },
    )
  }

  /** 「重新实名」：对标打 `/UserAjax/ResetRnameCert`，我们**没有**该能力 → 提示「尚未开放」。 */
  const onClick = (event: Event): void => {
    const el = (event.target as HTMLElement).closest<HTMLElement>('.resetRnameCertBtn')
    if (!el) return
    event.preventDefault()
    event.stopPropagation()
    msg('「重新实名」尚未开放')
  }
  root.addEventListener('click', onClick, true)

  return () => {
    root.removeEventListener('click', onClick, true)
    root.removeEventListener('focusin', onFocusIn, true)
    root.innerHTML = ''
  }
}

/**
 * 标准 island 页出口。**新增与编辑共用本模块**（两条路由的 `meta.islandPage` 都是
 * `user-adduser`）；「当前是新增还是编辑」的取法见文件头。
 */
export function mount(root: HTMLElement, ctx: IslandContext): Promise<() => void> {
  const mode = resolveFormMode(window.location.search, document.referrer)
  return mountUserFormPage(root, { onNavigate: ctx.onNavigate, mode })
}

export default { mount } satisfies IslandPage
