// 「我的资料 → 功能设置」里那块**代练员个人绑定**的四个出口（下游鸡蛋铺线 spec §9.2 / §9.3）。
//
// 分层与 `api/jidanpuSync.ts` 同形：传输、认证与信封解包全在 `request()`（`./http.ts`）里，
// 本文件不 import 任何 UI 库。
// ⚠️ 上面那个先例文件 `api/jidanpuSync.ts` 已于 2026-10-08 随本线删除，本条仅记先例；⛔ 别去找它。
//
// ⚠️ **本文件与 `jidanpuSync.ts` 读写的是两张零关系的表**（spec §4.1）：
//     - `jidanpuSync.ts`   → 租户级那一行（`JidanpuBinding`，「第三方平台 → 鸡蛋铺系统同步」页）；
//     - 本文件             → **代练员个人**那一行（`JidanpuMemberBinding`，一人一份）。
//   两张表的端点路径**刻意不同名**（本文件多一段 `member/`），⛔ 不要为了「省一个文件」合并它们：
//   合并会把「租户的凭据」与「某个代练员的凭据」在类型层就混成同一个东西。
// ⚠️ 上面那两行里的 `jidanpuSync.ts`（租户级那条线）已于 2026-10-08 随本线删除，本条仅记先例；
//    ⛔ 别去找它——本文件这条线（代练员个人绑定）**照旧生效**，「两条线零关系」这个结论不变。
//
// ⚠️ **`password` 只写不读**（spec §8.5 / §8-S1）：下面的 `JidanpuMemberBindingRow` **没有**
// 这个字段，只有一个 `password_set`（布尔）。这不是漏写——后端序列化器把 `password` 声明成
// `write_only`，响应里**连这个键都不出现** ⇒ 加回读模型只会得到一个恒 `undefined` 的字段，
// 而下一个读者会以为「后端回了但没渲染」（`api/jidanpuSync.ts` 里有同一段登记）。
// ⚠️ 被引用的那个文件已于 2026-10-08 随本线删除，本条仅记先例（⛔ 别去找它）；
//    本句的结论不变——`password` 仍是 `write_only`、仍然只写不读。
import { request } from './http'

/** 一行个人绑定（= 登录人那唯一一行；**单行资源**，`JidanpuMemberBinding`）。⭐ **多一个维度：对接方式**（官方 API 对接线 spec §11.1 / §11.2 / §2.1，同一条线在「绑定上家系统设置」页已经落地一遍，本文件是把同一批字段搬到**下家**这一条线上）：①式 = 账号 + 密码（**默认**）；②式 = `client_id` + `client_secret`（鸡蛋铺官方开放平台 API）。两个方式是**同一个端点的同一行记录**，只是换一套凭据 ⇒ 新增的是**第二个维度**，⛔ 不往既有的地址 / 账号那几格里塞（E-0068：标签读起来像、语义完全不同的东西）。⚠️ 与 `api/jidanpuUpstream.ts` 的同名类型是**两个文件各自的字面量**：两条线（下家 / 上家）的表与端点零关系，⛔ 别互相 import（那样会让一条线的改动意外改到另一条）。 */
export type JidanpuMemberBindingRow = {
  id: number
  /** ⭐ **「用哪套凭据」的唯一判据**：`password`（默认，登录式）/ `openapi`（官方开放平台 API）。
   * ⛔ **不许用 `client_id` 是不是非空去反推**（E-0021 家族）：切回①式之后 `client_id` 还在，
   * 那句真值判据会让页面与引擎**静默走②式**。⛔ 也不上屏（机器值）：页面经 `modeLabel` 换中文。 */
  mode: string
  /** 下家鸡蛋铺站点地址（尾斜杠已在后端归一化掉）。 */
  jidanpu_base_url: string
  /** 那边的登录名。②式下这一格**页面上不显示但值照旧**（两套凭据允许同时留着）。 */
  account: string
  /** 传令兵的姓名缓存（引擎还没跑过时为空串）。 */
  account_name: string
  /** **只回答「配没配」，不回答「配的是什么」**（后端 `password` 是 `write_only`）。
   * ⛔ **不要拿它当密码回填输入框**——那是把一个布尔当成密码串，页面上会显示 `true`/`false`。 */
  password_set: boolean
  /** ②式的 `client_id`（**身份、不是密钥**）⇒ 后端可读可写，页面上要**回显**（spec §11.2）。
   * 空串 = 不改（后端「空串 = 保留库里那一格」的既有口径）。 */
  client_id: string
  /** ②式的 `client_secret` **配没配**（⛔ 不回答「配的是什么」）。
   * ⛔ **不要拿它当密钥回填输入框**（页面上会显示 `true`/`false`）。它的两个正当用途：
   * ① 排障时看这一行配没配；② 决定那句提示怎么写（`fillMemberBinding` 里那句 placeholder）。 */
  client_secret_set: boolean
  /** ⭐ ②式的**回调地址**（spec §5.1 / §11.3）：**由后端算好**回给页面——⛔ 前端不拼域名
   * （域名拼接规则一变，前端那份必然漂移）。算不出来时是**空串**，原因在同行的
   * `callback_url_reason`（两者**恰好一个非空**）。 */
  callback_url: string
  /** ⭐ 算不出回调地址时的**人话原因**（spec §13.3-C10）：⛔ 静默回空串 = 本仓最恨的静默形态。 */
  callback_url_reason: string
  /** ⭐ 「**这台机器有没有人在消费**」的结论（spec §9.4 第 2 问）。`text` 是**后端算好的一句人话**（⛔ 前端不加工、也⛔ 不拿时刻自己再判一次 ——判据需要的那一列 `callback_watermark_at` 按 spec §4.2 **不上页面**，前端看不到它）。⚠️ **没有 `callback_last_at`**（本表那一列**不进序列化器**）：spec §9.4 第 1 问「对面还在推吗」在本页由这句话一起回答（同一句人话里已经含了它）⇒ ⛔ 不要为了「再显示一个时间点」往这里加一个后端根本不回的键（那会是一个恒 `undefined` 的字段，下一个读者会以为「后端回了但没渲染」——`api/jidanpuSync.ts` 里有同一段登记）。 */
  consumer_state: { text: string; bad: boolean }
  /** 录单时写进鸡蛋铺的来源名；模型默认「团团通系统」。 */
  source_name: string
  /** 总开关；**检测通过才允许为真**（后端 `enabled=True` 时会先同步跑一次检测）。 */
  enabled: boolean
  /** ⭐ 「同步大厅订单」开关（D-0299）：**只闸「抛大厅」**那两条路
   * （`member.launch`）；指派录单 / 状态回传 / 退单 / 改价 / 截图等**其他一切功能照旧**；
   * 后端默认 `true`（存量行为不变）。关掉后已挂对方大厅的存量单**不主动撤回**（D-0299 明文）。 */
  hall_sync_enabled: boolean
  /** 快路（日志）轮询间隔秒数。 */
  fast_poll_seconds: number
  /** 慢路（对账）间隔秒数。 */
  reconcile_seconds: number
  /** 大厅镜像那一档的抛单百分比（1–100）：抛单价 = 我方发单价 × 本值 / 100。 */
  throw_price_percent: number
  /** 上一轮同步**开跑**的时刻（跑没跑过都算）；还没跑过是 `null`。 */
  last_run_at: string | null
  /** 上一轮**成功**的时刻；从没成功过是 `null`。 */
  last_ok_at: string | null
  /** 最近的失败原因（**人话**，后端给的就是可显示文案）；没有失败时是空串。 */
  last_error: string
  last_error_at: string | null
  /** 上一次「启用」时那次检测通过的时刻（换过凭据会被清成 `null`）。 */
  checked_at: string | null
  created_at: string
  updated_at: string
}

/** 保存的载荷。⚠️ **`password` 省略或空串 = 不改**（spec §8-S4）：页面上的密码框**永远留空**（后端不回密码，留空只能表示「不动它」）。⛔ **不要**在提交前把它过滤掉又指望后端报错——后端把空串当「不改」（对），但**绝不能**让它变成「清空密码」。⚠️ **没有 `account_uid`**：那是**租户级**那条线才有的列（「同步账号」下拉）。本线的指派由后端按登录人自己那一行做，页面不选人（spec §3.4）。⚠️ 两个间隔字段**可以省**（后端 `required=False`，缺键就保持库里原值）。本页**没有**这两个输入框（业务方要的字段清单里没有它们），所以这里也不声明——让「页面上没有的东西」在类型上就发不出去。⚠️ `throw_price_percent`（1–100）在**本页**是必填（那一格永远有值：留空按 100 提交）；后端那一边是 `required=False`（缺键 = 保持库里原值，模型默认 100）——⛔ 两边的「必填」不是一回事。范围由序列化器与模型校验器两头卡（⛔ 传 0 会把免费单真送进对方大厅）。
 * ⭐ **追加三个可选键**（对接方式那条线；纯加法，上面六个键的语义**一个字不动**）：- `mode`：⚠️ **①式的载荷刻意不带这个键**（与上家那条线同一口径：普通①式保存的请求体**逐字不变**）⇒ 后端起 token 按**库里那一格**走；**唯一**的例外是「②→① 切回来」那一跳，那时补一个`mode: 'password'`（不补就是一处静默：页面说保存成功，而这套刚填的①式凭据**永远不会被用**）。- `client_id` / `client_secret`：②式那两格。`client_secret` **只写不读**、**空串 = 不改**（与 `password` 同一条口径，⛔ 不自己发明第三套）。 */
export type JidanpuMemberBindingPayload = {
  jidanpu_base_url: string
  account: string
  password?: string
  /** ⭐ 对接方式（唯一判据）。①式不带（见上）。 */
  mode?: string
  /** ⭐ ②式的 `client_id`。 */
  client_id?: string
  /** ⭐ ②式的 `client_secret`（**只写不读**；**空串 = 不改**）。 */
  client_secret?: string
  source_name: string
  throw_price_percent: number
  /** ⭐ 「同步大厅订单」（D-0299）：本页两支载荷**都必带**（那一格永远有勾选态；
   * 取不到控件时页面按 `true` 发＝后端模型默认）。后端那一侧 `required=False`：
   * **缺键 = 保持库里原值**；「缺省 true」只发生在**新建**那一档（模型默认）。 */
  hall_sync_enabled: boolean
  enabled: boolean
}

/** 来源字典里的一条 —— **下拉用的那一份**（出参键 `source_options`，D-0177）。- `label`：**要显示、也是要保存**的那个名字（上游 `name` 里第一个 `-` 之后那一段）；⚠️ 由**后端**算好（唯一出处 `jidanpu.source_label`）：⛔ 页面不许自己再切一份—— 切法漂移会让「下拉里选中的」与「库里保存的」**悄悄不一致**。- `name`：上游原始名（带前缀），只作排障。⚠️ 与 `api/jidanpuSync.ts` 里那个同名类型是**两个文件各自的字面量**：两条线（租户级 / 个人级）的表与端点零关系，⛔ 别互相 import（那样会让一条线的改动意外改到另一条）。⚠️ 租户级那一边（`api/jidanpuSync.ts`）已于 2026-10-08 随本线删除，本条仅记先例（⛔ 别去找它）——本文件这个类型照旧生效，⛔ 也别为「对齐」去 import 任何别处的同名类型。 */
export type JidanpuSourceOption = {
  name: string
  label: string
}

/** 「测试连接」的结论（后端 `JidanpuMemberTestConnectionView` 的出参）。⚠️ **`ok === false` 是一条正常的结论**（信封 `code` 恒为 0），不是请求失败：那时 `messages` 是后端逐条点名的人话（缺哪条权限 / 来源名有没有 / 登录对不对），页面要把它**显示出来**。⛔ 别把 `ok === false` 当成异常吞掉（那会把结论说成「测试失败」）。⭐ **2026-09-30（D-0177）追加三个键**（纯加法，上面两个键的语义一个字不动）：- `source_options`：对方来源字典**全量**（页面「订单来源名」下拉的唯一数据源）；- `source_options_ok`：**整份字典那一跳读到了没有**——⚠️ **登录就没成**时它是 `false`、字典是空的。页面**必须**据它决定要不要打「对方已无此来源」那个标记：⛔ 少了它，一次登录失败会让页面喊「对方已无此来源」（把「没读到」说成「没有」，`AGENTS.md` §5-E）；
 * - `source_ok`：下拉里选中那个名字在字典里**唯一命中**了没有。⭐ **追加一个可选键**（对接方式那条线；纯加法）：- `checks`：**②式**「测试连接」的**逐条权限表**（行形状见 `JidanpuEndpointCheck`，字段名就是契约：后端 `jidanpu_openapi.EndpointProbe.as_dict()`）。①式**没有**这个键⇒ 页面退回逐条 `messages`（⛔ 不许让用户点了一颗按钮而屏幕上什么都没有）。 */
export type JidanpuMemberTestResult = {
  ok: boolean
  /** 检测没过时逐条点名的人话；全过时是空数组。 */
  messages: string[]
  /** ⭐ 对方来源字典全量（下拉的唯一数据源）。⚠️ **①式才有**：②式那一支的键集合是 `{ok, messages, actions, checks}`（后端按**存的那一行**的 `mode` 分流；那条路压根不登录，拿不到字典）⇒ 页面**必须**按「这个键在不在」决定要不要动下拉，⛔ 不许把「没这个键」当成「对方没有来源」（`AGENTS.md` §5-E：把「没读到」说成「没有」是本仓最恨的形态之一）。 */
  source_options?: JidanpuSourceOption[]
  /** ⭐ 整份字典那一跳读到了没有（`false` ⇒ 不许打「对方已无此来源」那个标记）。同上：**①式才有**。 */
  source_options_ok?: boolean
  /** ⭐ 选中那个名字在字典里唯一命中了没有。同上：**①式才有**。 */
  source_ok?: boolean
  /** ⭐ ②式才有：逐条权限表（**恰好 5 行、顺序固定**：取 token → 游戏列表 → 常用项目 →
   * 查订单状态 → 进度图）。⚠️ 前端只负责**摆位置**与把「没探」和「不可用」分开，
   * `label` / `reason` / `message` / `action` **全部原样透传**（一个字都不改写，只做转义）。 */
  checks?: JidanpuEndpointCheck[]
  /** ⭐ ②式才有：去重后的「接下来做什么」（顺序 = 探测顺序）。 */
  actions?: string[]
}

/** ②式「测试连接」逐条表里的**一行**（= 后端 `EndpointProbe.as_dict()`，**字段名就是契约**）。⚠️ 与 `api/jidanpuUpstream.ts` 里的同名类型是**两个文件各自的字面量**（两条线零关系，⛔ 别互相 import）。行里的字段语义（`probed` 与 `ok` **不是一回事**）与那条线**逐字相同**：`probed=false` ⇒ **这一发根本没打**，`reason` 说清为什么没探，而 `ok` 同样是 `false`⇒ ⛔ **不许把未探测的行渲染成「不可用」**（那会把用户指去开一个根本不需要开的权限）。 */
export type JidanpuEndpointCheck = {
  /** 哪一跳（`token` / `GetGames` / `GetProject` / `GetOrderStatus` / `GetOrderProcessImgs`）。 */
  step: string
  /** 那一跳的**中文名**（页面上显示它，⛔ 前端不再写一份）。 */
  label: string
  /** 真的打了这一发没有。 */
  probed: boolean
  /** 可用吗（⚠️ 与 `probed` 不是一回事）。 */
  ok: boolean
  /** 不可用的档位；可用或未探测 ⇒ 空串。 */
  kind: string
  /** 人话原因（对方原话优先；未探测 ⇒ 为什么没探）。 */
  reason: string
  /** 页面上要显示的那一段（**只有「探到了且不可用」才有**）。 */
  message: string
  /** 这一档「接下来做什么」（未探测的行**继承**把它挡住的那一跳的指引）。 */
  action: string
}

/** 「立即同步」的结果：这一轮**真的改了几笔**我方订单（`applied`）。 */
export type JidanpuMemberSyncNowResult = {
  applied: number
}

/** 手动推送的**两个动作**（计划 §2/§4）。- `record` = 「发到我的管理系统」：把勾选的单**录到**我绑定的下家鸡蛋铺（只录单，⛔ 不抛大厅）；- `status` = 「同步订单状态」（**有勾选时**）：把我方**当前状态**推给下家那张单。⚠️ 与无勾选时的「拉一轮」（`syncMemberJidanpuNow`，从下家往回拉）**方向相反**，⛔ 别混。 */
export type JidanpuMemberPushAction = 'record' | 'status'

/** 逐单结果的三种取值 —— **后端契约的字面值**（`JidanpuMemberPushNowView` 的 `result`）。
 * ⚠️ ⛔ 不要按 `result` 反推 HTTP 成败：这一跳即使有 `failed` 也回 `code 0`（推送失败不挡接口）。 */
export type JidanpuMemberPushResult = 'done' | 'skipped' | 'failed'

/** 手动推送的逐单结果（`items` 的一项，顺序 = 入参里首次出现的顺序）。 */
export type JidanpuMemberPushItem = {
  order_no: string
  result: JidanpuMemberPushResult
  /** 人话原因：`done` 是空串；`skipped` / `failed` 是后端（消费者）写下的那句原文。 */
  reason: string
}

/** 手动推送的结果汇总（`POST /api/business/member/jidanpu-push-now/` 的 `data`）。
 * ⚠️ `requested` = **去重后**的笔数；`pushed + skipped + failed === requested` 是后端的口径。
 * ⚠️ `failed` 不等于请求失败：失败的那几单已经进了台账退避重试（HTTP 仍是 200 + code 0）。 */
export type JidanpuMemberPushNowResult = {
  requested: number
  pushed: number
  skipped: number
  failed: number
  items: JidanpuMemberPushItem[]
}

const BINDING_URL = '/api/business/member/jidanpu-binding/'
const TEST_CONNECTION_URL = '/api/business/member/jidanpu-binding/test-connection/'
const SYNC_NOW_URL = '/api/business/member/jidanpu-sync-now/'
const PUSH_NOW_URL = '/api/business/member/jidanpu-push-now/'

/** 「订单来源名」的默认值（业务方裁定；唯一出处是后端 `JidanpuBinding.SOURCE_NAME_DEFAULT`）。⚠️ 前端这一份是**默认值的显示副本**：后端不回「模型默认值」这个元数据，而页面在**还没保存**时就得让用户看到它（空着的话用户以为保存失败了）。改这一处之前先改模型那一处。⚠️ 与 `jidanpuSync.ts` 的同名常量是**两个文件各自的字面量**（两条线各自的默认值在模型层也是同一处常量，但两条前端线不许互相 import —— 那样会让「租户级」的改动意外改到「个人级」）。⚠️ 租户级那一边（`jidanpuSync.ts`）已于 2026-10-08 随本线删除，本条仅记先例（⛔ 别去找它）——下面的 `SOURCE_NAME_DEFAULT` 是**本文件自己那一份**，照旧生效（值的唯一出处仍是后端模型）。 */
export const SOURCE_NAME_DEFAULT = '团团通系统'

/** 读**本人**那一行；**还没绑定时后端回 `null`**（不编一个空行）。 */
export async function fetchMemberJidanpuBinding(): Promise<JidanpuMemberBindingRow | null> {
  return request<JidanpuMemberBindingRow | null>({ method: 'get', url: BINDING_URL })
}

/** 存（**PUT**）。单行资源没有主键、没有 PATCH：后端「没有行就建、有就改」。⚠️ `enabled: true` 时后端**先同步跑一次检测**：不过就**一行都不落**，并用业务失败码把缺项**逐条点名**回给你（`request()` 会把它抛成 `BizError`，`message` 就是那句人话）。⇒ 调用方必须把这句话**原样显示**，⛔ 不要自己改写成「保存失败」。 */
export async function saveMemberJidanpuBinding(
  payload: JidanpuMemberBindingPayload,
): Promise<JidanpuMemberBindingRow> {
  return request<JidanpuMemberBindingRow>({ method: 'put', url: BINDING_URL, data: payload })
}

/** 「测试连接」：拿**本人那一行的凭据**登录鸡蛋铺跑一遍绑定检测（spec §9.1）。⚠️ **不传地址与账号**（凭据只从后端那一行取，spec §8.5）：那两样决定「我们去打谁的服务器」⇒ 请求方能传它们，这条端点就成了一条拿我们当跳板的通道。⭐ **2026-09-30（D-0177）起传两个可选参数**（原句写「不传任何参数 / 连请求体都不读」，按 `AGENTS.md` §4 保留）：- `password`：密码框里此刻填的那个 ⇒ **未保存也能查**。⚠️ 后端只在**内存副本**上用它，⛔ 不落库、⛔ 不回显；留空 = 用库里那一份。- `sourceName`：下拉里此刻选中的那个名字 ⇒ 「检查的就是你要保存的那个」（⛔ 不发它就会去查**库里那个旧名**，用户换了还没保存时会得到**假结论**）。空串 = 不传该键（后端回落库里那一行）。失败（还没绑定 / 缺项 / 上游异常）回业务失败码 ⇒ `request()` 抛 `BizError`，`message` 是人话。 */
export async function testMemberJidanpuConnection(
  password = '',
  sourceName = '',
): Promise<JidanpuMemberTestResult> {
  const config = { method: 'post' as const, url: TEST_CONNECTION_URL }
  const data: { password?: string; source_name?: string } = {}
  if (password !== '') data.password = password
  if (sourceName !== '') data.source_name = sourceName
  return request<JidanpuMemberTestResult>(
    Object.keys(data).length === 0 ? config : { ...config, data },
  )
}

/** 「立即同步」：对**本人那一行**跑一轮状态回拉（spec §9.3）。⚠️ **空体**：行主只来自登录态（后端一个字节的请求体都不读）。⚠️ 有频控（同会话最小间隔 / 在途互斥 / 失败退避，spec §9.3）——被挡时后端回**人话文案（含剩余秒数）**，`request()` 抛 `BizError` ⇒ 页面**原样显示**它，⛔ 页面**不许**自己再写一套节流判据（那两套判据一旦漂移，用户会看到「还能点」但请求发不出去）。⚠️ 这一跳**会真的改我方订单状态**（后端由 `apply_transition` + 审计留痕）。 */
export async function syncMemberJidanpuNow(): Promise<JidanpuMemberSyncNowResult> {
  return request<JidanpuMemberSyncNowResult>({ method: 'post', url: SYNC_NOW_URL })
}

/** 「手动推送到我的下家」（计划 §2/§4）：`POST /api/business/member/jidanpu-push-now/`，把**勾选的行**推给**登录人自己**绑定的下家鸡蛋铺。⚠️ **只推自己那一条绑定**（行主只来自登录态；请求体里塞 `user` / `binding` 一律被后端忽略）。⚠️ 入参规则由后端判（1–50 条、去重保序、`action` 枚举）⇒ 非法时 `request()` 抛`BizError(1000)`，`message` 就是后端那句带字段名的中文。⛔ 前端**不复刻**这些判据。⚠️ **`failed > 0` 也回 code 0**（推送失败不挡接口：那几单进了台账退避重试）⇒ 调用方要按`result.pushed / result.skipped / result.failed` 说话，⛔ 不能把「请求成功」说成「全推成功了」。⚠️ **不写节流判据**：与「立即同步」不同，这条端点没有频控闸（它的量 = 勾选的行数，且上限 50）。 */
export async function pushMemberJidanpuNow(
  orderNos: string[],
  action: JidanpuMemberPushAction,
): Promise<JidanpuMemberPushNowResult> {
  return request<JidanpuMemberPushNowResult>({
    method: 'post',
    url: PUSH_NOW_URL,
    // ⚠️ 键名是后端序列化器的字段名（`order_nos` / `action`），⛔ 不改写成驼峰。
    data: { order_nos: orderNos, action },
  })
}
