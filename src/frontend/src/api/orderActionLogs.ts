/** 「交易订单操作记录」页（`pages/order-tradingorderactionloglist.ts`）的端点封装。spec：`docs/specs/2026-10-04-trade-action-log-design.md` §4（后端契约）/ §5（前端）/ §7-9。| 方法 | 路径 | 用途 ||---|---|---|| GET | `/api/audit/order-action-logs/` | 订单动作日志列表 + 右侧「操作统计」+「操作人员」候选 |## 与 `api/auditLogs.ts` 的分工（为什么另开一个模块、而不是往那边塞）两条读端点打的是**同一张表**（`audit.OperationLog`），但**准入门不同**：`/api/audit/logs/`要 RBAC 节点 `system.logs`（系统跟踪日志页），本端点要 `order.platform_action_logs`（交易订单操作记录页）——spec §3-D2 把这条差异登记为「同一张表两个准入门」。
 * 行的形状也不同（本页要 `order_no` / `from_value` / `to_value`：`from_value` / `to_value`在 `/api/audit/logs/` 的行序列化器里**没有**；`order_no` D-0341 起**有**了——作为该页「来源订单号」列的展示兜底原值）⇒ 混在一个模块里会让「哪条端点归哪个门」变成注释里的一句话。## 消费方与分工（为什么这里**没有** `fetch…` 包装）`pages/order-tradingorderactionloglist.ts`（island 列表页）用 **`ORDER_ACTION_LOGS_URL`常量**把 URL 交给 `layui.table` 的 `url`（服务端分页必须走 `url` 模式），筛选参数由页面调`toOrderActionLogParams(...)` 组装后交给 `table.reload({where})`。
 * ⚠️ 本模块原先还有一个 `fetchOrderActionLogs`（`request()` 包装 + `OrderActionLogPayload`），但 2026-10-04 修复轮当场 `grep` 实测**当时全仓没有任何调用点**——页面走的是 layui 的 `url`模式，它只会成为一个没有说明的死导出（spec §10.7）⇒ 已删掉。将来真出现程序化调用方时，按**当时**的载荷形状再加回来；⛔ 不要凭「与 `api/auditLogs.ts` 同形」把死代码留在仓库里。响应 `data` 的形状（`{items,total,page,page_size,summary,actors,actor_options,actions}`）由下面几个行类型加页面里的 `ActionLogEnvelope` 描述。`actors`（登录账号数组）与 `actor_options`（`{actor,display}` 数组，线新增）是同一份数据的两种形状，后者给「操作人员」下拉提供「文本=账号名称、值=登录账号」的分离。 */

/** 列表端点（`apps/audit/urls.py`）。尾斜杠与 `apps/audit/urls.py` 的字面路径一致。 */
export const ORDER_ACTION_LOGS_URL = '/api/audit/order-action-logs/'

/** 一行的**恰好 11 个键**（后端行序列化器，spec §4.3）。
 * ⚠️ 这份清单是**前端侧的契约副本**：后端键名若与它不同，`toLogRow` 会把单元格渲染成空白
 * 而**不会报错**（`url` 模式把 `items` 原样交给表格）。后端那一份才是权威；两侧要同步改。 */
export type OrderActionLogRow = {
  /** 「系统单号」列。⚠️ 不是 `target_id`（spec §2.2：`target_id` 装的是主键，语义不同）。 */
  order_no: string
  /** 「交易单号」列（平台那边的单号；日志列有值用日志列、空则按 `order_no` 回填，spec §3-D7）。 */
  source_order_no: string
  /** 「游戏名称」列（回填规则同 `source_order_no`）。 */
  game_name: string
  /** 「变更内容」列的**改前**值（spec §3-D5；`order.*` 行上常见为空）。 */
  from_value: string
  /** 「变更内容」列的**改后**值。 */
  to_value: string
  /** 动作 code（如 `order.transition`）——页面不直接渲染，用于排障与 `action_label` 的出处对照。 */
  action: string
  /** 「操作说明」列的**短名**（= 后端 `ORDER_ACTION_LABELS[action]`，spec §3-D6）。 */
  action_label: string
  /** 服务端渲染好的整句「操作说明」（本页**不**渲染它，spec §3-D6；留给排障/其它页）。 */
  summary: string
  /** 操作人**账号名**（筛选参数 `actor` 按它匹配）。 */
  actor: string
  /** 「操作人员」列的**显示名**（昵称，取不到人时后端回退到 `actor`）。 */
  actor_display: string
  /** ISO-8601、当前时区——展示前必须过 `formatCreatedAt`（`@/api/auditLogs`）。 */
  created_at: string
}

/** 右侧「操作统计」一行（后端 `data.summary[j]`，spec §4.4）。 */
export type OrderActionLogSummaryRow = {
  action: string
  /** 短名（与行里的 `action_label` 同源）。 */
  action_label: string
  /** 操作人账号名（**筛选键**；展示面取下面的 `actor_display`）。 */
  actor: string
  /** 操作人的**账号名称**（昵称优先、空串回落账号）——线新增（spec §C1/C2）。
   * ⚠️ 类型上**必填**（新后端恒回这个键），但运行期仍按 `actor_display ?? actor` 取值：
   * 部署窗口里「前端新、后端旧」时它根本不存在，统计表不能因此变空白。 */
  actor_display: string
  /** **筛选后全集**上的行数（⛔ 不是当前这 20 行）。 */
  count: number
}

/** 「操作人员」下拉的**一个候选**（后端 `data.actor_options[j]`，线新增，spec §C1）。`actor` = 登录账号（**提交值**，后端 `actor__exact` 按它匹配）；`display` = 账号名称（**显示文本**）。⚠️ 两者必须分开：拿昵称去筛选会查空（昵称≠账号名的人全部落空），拿账号名显示就是本线要修的那个问题。旧后端只回 `data.actors: string[]` ⇒ 页面按「文本=值=账号」兜底（见页面的 `toActorOptions`）。 */
export type OrderActionLogActorOption = {
  actor: string
  display: string
}

/** 「操作动作」下拉的**一个候选**（后端 `data.actions[j]`，spec §3-D3）：我们的动作 code + 中文短名。⚠️ 这份候选**由后端给**（`apps/audit/actions.py::ORDER_ACTION_LABELS`），前端**不镜像**它——两处各写同一批中文短名必有一处先漂移，而漂移的症状（下拉里的短名与表格「操作说明」列的短名不一样）**没有任何网会报**。所以「短名的唯一出处」留在后端：行里的 `action_label` 与这里的 `label`出自同一张表 ⇒ 两处天然一致。 */
export type OrderActionLogActionOption = {
  code: string
  label: string
}

/** 端点接受的 6 个筛选 + `ordering` + 分页（spec §4.2）。 */
export type OrderActionLogSearchParams = {
  /** `yyyy-MM-dd`（本页的日期框是 laydate `type:'date'`，值就是日期形状；后端 `DateTimeField` 收）。 */
  stime?: string
  etime?: string
  /** 我们的动作 code（`ORDER_ACTION_LABELS` 的键之一）；空 = 不加筛选。 */
  action?: string
  /** 操作人**账号名**，整值相等。 */
  actor?: string
  /** 「系统单号 / 交易单号」关键词（后端 `order_no__icontains` 或 `source_order_no__icontains`）。 */
  key1?: string
  /** 服务端排序（spec §4.2）。**不传 = 后端默认序**（最新在前）。
   * ⚠️ 词表是**后端白名单**：只有「操作时间」一列的两个方向。传白名单外的值后端会拒成
   * 业务码 1000——这里用联合类型把两个合法值钉死在编译期，别扩成 `string`。 */
  ordering?: OrderActionLogOrdering
  page?: number
  page_size?: number
}

/** 服务端排序词表（与后端 `LOG_ORDERING_FIELDS` 的两个键**逐字对应**，
 * `apps/audit/api/serializers.py`；两个读端点**共用**同一个映射）。
 * `created_at` = 旧在前；`-created_at` = 新在前。 */
export type OrderActionLogOrdering = 'created_at' | '-created_at'

/** 「操作动作」下拉里「全部」那一项的 `value`（= **不加动作筛选**）。⚠️ 选项列表是**运行期**从响应的 `data.actions` 重建的（spec §3-D3：短名的唯一出处留在后端），所以「全部」这一项也由页面自己拼；它的 `value` 取空串——空值走 `toOrderActionLogParams` 的「空就不发」那一支，不会把一个非法的动作码发到后端（后端 `action` 按 `ORDER_ACTION_LABELS`的键做白名单，发未知码会当场 1000）。 */
export const ACTION_ALL_VALUE = ''

/** 组装查询参数（**唯一的线上形状出口**）：空值 / 空串 / `null` 不发送。本端点特有的规则：1. **空串一律不发**（「全部」那一项的值就是空串）——判据是「空」而不是真值（E-0021：真值判断会静默丢掉边界值）。2. `ordering` **没点过排序时不发**——多发一个空值会让「默认序」与后端缺省行为分叉。 */
export function toOrderActionLogParams(
  params: OrderActionLogSearchParams,
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
  put('action', params.action)
  put('actor', params.actor)
  put('key1', params.key1)
  // 排序：没点过排序时不要发这个参数（`put` 也会丢掉空串）。
  put('ordering', params.ordering)
  put('page', params.page)
  put('page_size', params.page_size)
  return out
}
