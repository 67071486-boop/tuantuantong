/** 系统操作日志的三个端点（spec `docs/specs/2026-09-21-system-operation-log-design.md` §6）。| 方法 | 路径 | 用途 ||---|---|---|| GET  | `/api/audit/logs/` | 日志列表（6 筛选 + `ordering` 排序 + `page`/`page_size`） || GET  | `/api/audit/actors/` | 「相关人员」下拉的数据源（`?key=` 子串，上限 50，spec §6.3） || POST | `/api/audit/events/` | 前端上报「进入编辑订单页面」（`order.open_edit`，spec §6.2） |⚠️ **两条读端点的准入都是 RBAC 节点 `system.logs`**（不是 `IsAccountAdmin`）——这正是「相关人员」不能复用 `GET /api/accounts/` 的原因：持有日志权限但不是账号管理员的人会在那个端点拿到空名单（spec §6.3 / 评审 I5）。
 * ## 消费方与分工（与 `api/performances.ts` 同形）- `pages/system-systemlog.ts`（island 列表页）用 **`AUDIT_LOGS_URL` 常量**把 URL 交给`layui.table` 的 `url`（服务端分页必须是 `url` 模式），筛选参数由页面调`toAuditLogParams(...)` 组装后交给 `table.reload({where})`；- `fetchAuditLogs` / `fetchAuditActors` 是**同一批端点的类型化出口**（`request()` 统一信封 +错误分类），供测试与后续程序化调用方使用——这与 `api/performances.ts::fetchPerformances`完全同型：那边 `emp-emplist.ts` 也用 URL 常量走 layui 的 `url`，`fetchPerformances` 同样没有页面消费方。- 「相关人员」的**远端搜索**由 vendored 的 formselects-v4 自己发 `$.ajax`（插件行为，见
 * `pages/system-systemlog.ts`），所以它以 `AUDIT_ACTORS_URL` 为搜索地址，**参数名与响应字段与 `fetchAuditActors` 同一份契约**（`key` → `{id, username, display_name}`）。 */
import { request } from './http'
import type { Page } from './unwrap'

/** 列表端点（`apps/audit/urls.py`）。尾斜杠是 R18 的裁决（业务集合路径一律带）。 */
export const AUDIT_LOGS_URL = '/api/audit/logs/'

/** 人员枚举端点（`apps/audit/urls.py`）。 */
export const AUDIT_ACTORS_URL = '/api/audit/actors/'

/** 读操作上报端点（`apps/audit/urls.py`）。 */
export const AUDIT_EVENTS_URL = '/api/audit/events/'

/** 唯一允许前端上报的读动作（后端 `FRONTEND_REPORTABLE_READ_ACTIONS` 的那一个）。 */
export const ORDER_OPEN_EDIT = 'order.open_edit'

/** 日志分类 code → 中文。**逐字照抄对标** `systemlog.js` 的 `switch(res.logType)` 与页内`<option>`（8 个分类；spec A2/A11 的代码也照抄）。`0` 不在这里——它是「全部」，见`toAuditLogParams`。视图侧同表在 `apps/audit/models.py::LogType`；两侧是**跨语言的同一份词表**（前端展示、后端筛选），改一处必须同步另一处——`api/__tests__/auditLogs.spec.ts` 把这里的 8 条钉成字面值。 */
export const LOG_TYPE_LABELS: Readonly<Record<number, string>> = {
  5: '号库相关',
  10: '订单相关',
  20: '绩效相关',
  30: '用户相关',
  40: '系统相关',
  50: '会员相关',
  60: '排团相关',
  70: '其它',
}

/** 列表行（`OperationLogRowSerializer` 的 8 个显示字段 + `action` + `actor_display`）。行里有 `order_no`（内置单号原值）——「来源订单号」列的前端兜底（`source_order_no || order_no`）取它；「来源订单号」列的取值口径见 `pages/system-systemlog.ts::toLogRow`。 */
export type AuditLogRow = {
  /** 对标「相关ID」列（纯文本展示，不跳转）。 */
  target_id: string
  /** 8 个分类 code 之一；展示时过 `LOG_TYPE_LABELS`。 */
  log_type: number
  /** 「来源订单号」列的**来源号原值**（日志快照 → 订单当前值的后端回填，D7）。
   * ⚠️ 展示不直用它：为空时由页面兜底到 `order_no`（D-0341）。 */
  source_order_no: string
  /** 内置单号**原值**（D-0341）：「来源订单号」列为空时的兜底展示值。
   * ⚠️ 只是展示兜底：搜索与寻址键不因它改变（D-0301 的「寻址键不换」）。 */
  order_no: string
  game_role_name: string
  game_name: string
  /** 「操作说明」列（后端由结构化列渲染，前端原样展示）。 */
  summary: string
  /** 勾选 include_delete_reason 时返回删除原因原文。 */
  delete_reason?: string
  actor: string
  /** ISO-8601、当前时区（如 `2026-09-21T10:00:00+08:00`）——展示前必须过 `formatCreatedAt`。 */
  created_at: string
  /** 动作 code（如 `order.create`）。页面今天不渲染它，留着给筛选/排障用。 */
  action: string
  /** 「操作用户」列的**显示名**（昵称，空则账号名）；`actor_id` 取不到人时后端回退到 `actor`。
   * ⚠️ **可选**：老接口/老桩数据没有这个键，页面必须回退 `actor`（spec §11.2 的「兜底保留」）。
   * 它是**只读显示派生字段**，不落库、不参与筛选——筛选提交的仍是 `actor`（账号名）。 */
  actor_display?: string
}

/** 人员枚举行（`OperationLogActorRowSerializer` 的全部字段：`id` + `username` + `display_name`）。 */
export type AuditActorRow = {
  id: number
  username: string
  /** 下拉显示用的昵称（空则等于 `username`）。⚠️ 筛选参数仍提交 `username`（后端按存储值匹配）。 */
  display_name: string
}

/** 后端 `OperationLogSearchSerializer` 支持的 6 个筛选 + `ordering` + 分页。 */
export type AuditLogSearchParams = {
  /** `yyyy-MM-dd HH:mm:ss`（本地时区）。 */
  stime?: string
  etime?: string
  /** 8 个 code 之一；**`0` = 全部**（后端把它当「没传」的同义值）。 */
  log_type?: number | null
  game_name?: string
  /** 「相关人员」筛选，**传 `username` 列表**（不是 id——历史行的 `actor_id` 可能为空，spec §6.3）。⚠️ **页面今天只放一个元素**：对标 `#recUser` 是**单选**（`xm-select-radio`，spec §6.1 的 R29 更正），所以 `toLogSearchParams` 给的是长度 0/1 的列表。这里保持 `readonly string[]` 是因为**后端 API接受多值**（重复参数或逗号分隔）——那是契约，不是页面当前的形状；别把它收窄成 `string`。 */
  actor?: readonly string[] | null
  order_no?: string
  key?: string
  include_delete_reason?: boolean
  /** 服务端排序（spec §6.1 / §16.2 第 1 条）。**不传 = 后端默认序**（最新在前）。⚠️ 词表是**后端白名单**（`apps/audit/api/serializers.py::LOG_ORDERING_FIELDS`）：只有「操作时间」一列的两个方向。传白名单外的值后端会拒成业务码 1000——这里用联合类型把两个合法值钉死在编译期，别扩成 `string`。 */
  ordering?: AuditLogOrdering
  page?: number
  page_size?: number
}

/** 服务端排序词表（与后端 `LOG_ORDERING_FIELDS` 的两个键**逐字对应**）。
 * `created_at` = 旧在前；`-created_at` = 新在前。 */
export type AuditLogOrdering = 'created_at' | '-created_at'

/** 组装查询参数（**唯一的线上形状出口**）：空值 / 空串 / `null` 不发送。⚠️ 两条与 `api/orders.ts::toOrderParams` **不同**的规则，都是本端点的契约：1. **`log_type === 0` 丢掉**（下拉的「全部」= 不加这个筛选）。判据用 `=== 0` 而不是真值——反例是 `log_type: 20` 这种真值要走另一支（E-0021：真值判断会静默丢条件）；2. **`actor` 列表编码成逗号分隔的单个参数**。后端两种形状都收（`query.getlist("actor")`先展开**重复参数**、再按 `,` 拆，`apps/audit/api/views.py::_search_params`），但 axios 对数组参数默认序列化成 `actor[]=a`，那不是后端认的形状——所以这里显式 join，不依赖 axios 的默认。⚠️ 页面今天只会传**一个** `username`（对标控件单选，R29）；
 * 保留列表→join 是为了不砍掉后端已测试的多值容忍（`auditLogs.spec.ts` 用两个字面用户名钉着它）。 */
export function toAuditLogParams(
  params: AuditLogSearchParams,
): Record<string, string | number> {
  const out: Record<string, string | number> = {}
  const put = (key: string, value: string | number | undefined | null): void => {
    // 判据是「空」而不是真值：`0` 这类边界值要留下（E-0021）。
    if (value === undefined || value === null) return
    if (typeof value === 'string') {
      const trimmed = value.trim()
      if (trimmed === '') return
      out[key] = trimmed
      return
    }
    out[key] = value
  }
  put('stime', params.stime)
  put('etime', params.etime)
  // `0` = 全部：显式排掉（`put` 会保留 0，所以这条不能走 `put`）。
  if (params.log_type !== undefined && params.log_type !== null && params.log_type !== 0) {
    out.log_type = params.log_type
  }
  put('game_name', params.game_name)
  const actors = (params.actor ?? []).map((a) => a.trim()).filter((a) => a !== '')
  if (actors.length > 0) out.actor = actors.join(',')
  put('order_no', params.order_no)
  put('key', params.key)
  if (params.include_delete_reason === true) put('include_delete_reason', 'true')
  // 排序（spec §6.1）：**没点过排序时不要发这个参数**——今天的请求形状里没有它，
  // 多发一个空值会让「默认序」与后端缺省行为分叉（`put` 也会丢掉空串）。
  put('ordering', params.ordering)
  put('page', params.page)
  put('page_size', params.page_size)
  return out
}

/** 日志列表（`GET /api/audit/logs/`；响应形状是项目统一的 `{items,total,page,page_size}`）。 */
export async function fetchAuditLogs(
  params: AuditLogSearchParams = {},
): Promise<Page<AuditLogRow>> {
  return request<Page<AuditLogRow>>({
    method: 'get',
    url: AUDIT_LOGS_URL,
    params: toAuditLogParams(params),
  })
}

/** 人员枚举（`GET /api/audit/actors/?key=`；纯数组，**不分页**——下拉数据源不是分页表格）。
 * 空 `key` 不发参数（后端把空串当「没传」）。 */
export async function fetchAuditActors(key = ''): Promise<AuditActorRow[]> {
  const trimmed = key.trim()
  return request<AuditActorRow[]>({
    method: 'get',
    url: AUDIT_ACTORS_URL,
    params: trimmed === '' ? {} : { key: trimmed },
  })
}

/** 上报「进入编辑订单页面」（`POST /api/audit/events/`，spec §6.2）。⚠️ **只在真的进入订单编辑态时调**（本仓库的落点是管理订单列表的行内「编辑」按钮`openEditDialog()`——它打开的就是录单页的编辑态 iframe）。**不要在订单详情页加载时调**：详情页是「看」不是「改」，那样写出的日志会声称用户「进入了编辑页」——一条假的审计陈述。⚠️ 载荷里**只有** `action` + `order_no`：操作人与租户取认证态，请求体里带了也被后端丢弃（`OperationLogEventSerializer` 没有那些键）。 */
export async function reportOrderOpenEdit(orderNo: string): Promise<null> {
  return request<null>({
    method: 'post',
    url: AUDIT_EVENTS_URL,
    data: { action: ORDER_OPEN_EDIT, order_no: orderNo },
  })
}

/** 上报的**静默**包装（页面用这个）。spec §6.2 的失败语义：这条腿没有业务事务，写失败就是**丢一条查看记录**——不阻塞、不回滚、也不该弹一个用户看不懂的错误（用户点「编辑」时不该因为日志表出问题而看到报错，那会让「查看记录」反过来影响业务动作）。所以这里把拒绝吞掉。 */
export function reportOrderOpenEditQuietly(orderNo: string): void {
  if (orderNo === '') return
  void reportOrderOpenEdit(orderNo).catch(() => {
    /* 有意静默：丢一条查看记录，不打扰用户（spec §6.2）。 */
  })
}

/** ISO-8601（**当前时区**，如 `2026-09-21T10:00:00+08:00`）→ 对标「操作时间」列的 `Y-m-d H:i:s`。⚠️ **取字符串里的日期段，不做时区换算**：接口回的已经是 `Asia/Shanghai` 的墙上时间（spec §6.1 的实现期补记），对标那一列显示的也是它。若走 `new Date(...)` 再按浏览器本地时区渲染，东八区以外的浏览器会把时间整体平移——那是**改值**，不是格式化。形状认不出（空值 / 非字符串 / 非 ISO）时给空串，**绝不把原始 ISO 串填进列里**（这是 Task 5 明确交接给本任务的事）。 */
export function formatCreatedAt(value: unknown): string {
  if (typeof value !== 'string') return ''
  const m = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})/.exec(value)
  return m ? `${m[1]} ${m[2]}` : ''
}
