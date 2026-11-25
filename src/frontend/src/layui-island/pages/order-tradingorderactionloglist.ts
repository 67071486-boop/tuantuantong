/**
 * 【交易订单操作记录管理列表】页的 **layui 复刻页 + 接真后端**（对标物 `/Order/TradingOrderActionLogList`）。
 *
 * 形状：**页内左右两栏两张表**（左 `#tableList` 操作明细 = col-md9、右 `#tableList2` 操作统计 =
 * col-md3），共用一条筛选栏（4 个控件）+ 左表工具栏 1 颗按钮 `lay-event="refresh"`。
 *
 * 证据（全部离线，**视觉值唯一出处**）：
 *  - 标记与页内 `<style>` → `public/replica/order-tradingorderactionloglist.html`
 *    （源 `.tools/recon/suqing1/html/Order__TradingOrderActionLogList.html`）——**本轮一字未动**；
 *  - 左表列定义 → `.tools/recon/suqing1/assets/JS2019/Order/TradingOrderActionLogList.js` **L58-86**
 *  - 右表列定义 → 同 JS **L171-175**（由左表 `done` 里的 `loadTotalData(...)` 渲染，L168-181）
 *  - 工具栏模板 `#toolbarDemo` → 同 HTML **L174-178**（按钮文案逐字「刷新数据」）
 *  - 分页 → 同 JS **L87-89**：`page:true` / `limit:20` / `limits:[20,30,50]`
 *  - 日期框 → 同 JS **L31-39**：`laydate.render({elem:'#sDate', type:'date', min:-90})` 与
 *    `{elem:'#eDate', type:'date'}`（**日期**形状，不是 datetime）
 *
 * ## ⛔ 为什么不再用 `artifactPage`（本线最重要的一个改动）
 *
 * `artifactPage.mountArtifact` 在**捕获阶段**把所有 `button / a.layui-btn / [lay-event] / [lay-filter]`
 * 的点击收口成「尚未开放」并 `stopPropagation`（`artifactPage.ts:282-298`）。而本页工具栏那颗按钮
 * **就是** `lay-event="refresh"`（证据件 L176）⇒ 挂着 `artifactPage` 时**永远点不到**
 * `table.on('toolbar(tableList)')`。所以本页照 `system-systemlog.ts` 的形态改成**自挂载**：
 * 取证据件 → 注入 → `initIslandElements` → 自己 `table.render`。
 * 代价：artifactPage 的那些兜底提示也没有了 ⇒ 本页自己的两个行内链接行为由下面显式接管
 * （`AGENTS.md` §5-G：自建 island 页必须有「驱动真交互」的用例，见同目录 spec 文件）。
 *
 * ## 与对标的**有意差异**（每条都有本线的依据，不是抄错）
 *
 * | # | 差异 | 依据 |
 * |---|---|---|
 * | 1 | 端点换成本仓库的 `GET /api/audit/order-action-logs/`；对标的 `/OrderAjax/GetPushPlatfromOrderActionLogMangeList`（`Platfrom`/`Mange` 是它自己的拼写）不再出现在代码里 | spec §4.1 |
 * | 2 | 「**订单标题**」列换成「**变更内容**」= `from_value → to_value`（两列都空时该格**留空**） | 业务方 R4 / spec §3-D5 |
 * | 3 | 「**操作说明**」列 = **短名**（`action_label`）而不是整句 `summary` | spec §3-D6（对标那一列只有 100px） |
 * | 4 | 筛选栏「**操作动作**」的选项换成**后端随响应给的那些动作短名** + 「全部」，**控件保留**、参数名 `action`；那份候选**由后端随响应给**（`data.actions`），前端**不镜像** | spec §3-D3/D8（照抄对标的 11 个词表会「选了没结果」；镜像同一批中文短名必有一处先漂移，而漂移**没有网会报**） |
 * | 5 | 日期框默认值 = **今天 − 1 个自然月 ~ 今天**（运行期算） | spec §3-D10（证据件 `2026-08-20`/`2026-09-20` = 31 天 = 自然月，⛔ 不是 `system-systemlog.ts` 的「昨天~今天」） |
 * | 6 | 「系统单号」点开的是**我们自己的**订单详情岛页，且用 `order_no`（对标用的是它的**主键** `orderId`） | spec §3-D12 |
 * | 7 | 「交易单号」**不可点**：照对标渲染成蓝色链接，点了提示「尚未开放」 | spec §3-D13（对标那个交易详情弹层我们没有） |
 *
 * ## 两张表的渲染要点（静态参数见 `LEFT_TABLE_OPTIONS` / `RIGHT_TABLE_OPTIONS`）
 *
 * 1. **右表没有 `toolbar`**（只写 `elem`/`cols`/`data`/`page`）——layui 的模板是
 *    `{{# if(d.data.toolbar){ }}<div class="layui-table-tool">…`（实测 `public/layui/layui.js`
 *    偏移 277360），**toolbar 为假时整个工具区（连 `defaultToolbar` 的「筛选列」图标）都不渲染**。
 *    所以右表**不传 `toolbar`、也不传 `defaultToolbar`**。
 * 2. **右表 `page: false`**——不能吃 layui 的 `pageLimits` 兜底（那会凭空长出一条分页栏）。
 * 3. 左表 `defaultToolbar` 在同一个 `render` 对象里写了两次（L53 `[]` → **L57 `['filter']` 生效**）。
 * 4. `text: {none: '暂无数据'}` 是**沿用**原 `artifactPage` 渲染器的值（它给全批复刻页写了这一条），
 *    ⛔ 别改成 layui 的默认「无数据」——那是这一页在换渲染器时**不该变**的一个可见值。
 *
 * ## 右上「操作统计」的数据来源
 *
 * 对标把它塞在左表响应里（`done` 里 `loadTotalData(res.total)`）；我们的**同一次响应**在
 * `data.summary`（spec §3-D3/D4.4）。左表的 `parseData` 把原始信封里的 `summary`/`actors`
 * 拆到闭包变量，`done` 里再渲染右表、灌「操作人员」下拉——与对标「在 `done` 里渲染右表」同序。
 */
import { formatCreatedAt } from '@/api/auditLogs'
import {
  ACTION_ALL_VALUE,
  ORDER_ACTION_LOGS_URL,
  toOrderActionLogParams,
  type OrderActionLogActionOption,
  type OrderActionLogActorOption,
  type OrderActionLogOrdering,
  type OrderActionLogRow,
  type OrderActionLogSearchParams,
  type OrderActionLogSummaryRow,
} from '@/api/orderActionLogs'
import { dayRangeMonthsAgo, type DateRange } from '@/layui-island/dateRange'
import { escapeHtml } from '@/layui-island/escape'
import { loadConfigArtifact } from '@/layui-island/config-replica'
import {
  bootstrapIsland,
  initIslandElements,
  type IslandContext,
  type IslandPage,
} from '@/layui/island'
import type { LayuiModule } from '@/layui/loader'
import { getFormSelects, initFormSelects } from '@/layui/vendor'

/** 证据件名（= `public/replica/<key>.html`）。 */
export const ARTIFACT_KEY = 'order-tradingorderactionloglist'

/** 左表 `elem`（对标 JS L52；HTML L173）。⚠️ 与 `TABLE_ID` 分成两个常量（E-0061 ①）。 */
export const TABLE_ELEM = '#tableList'
/** 左表实例 id（= `lay-filter`，`table.reload` 的第一个实参；传 `#tableList` 是**静默 no-op**）。 */
export const TABLE_ID = 'tableList'
/** 左表 `lay-filter`（`table.on('toolbar(tableList)')` / `sort(tableList)` 的事件名就是它）。 */
export const TABLE_FILTER = 'tableList'
/** 右表 `elem`（同 JS L171；HTML L186）。**无 `toolbar`**（见文件头）。 */
export const TABLE2_ELEM = '#tableList2'
/** 右表实例 id。 */
export const TABLE2_ID = 'tableList2'
/** 左表 `toolbar`（同 JS L55；模板在 HTML L174-178，按钮文案逐字「刷新数据」）。 */
export const TOOLBAR_ELEM = '#toolbarDemo'
/** 生效的 `defaultToolbar`（同 JS L57；L53 那次被覆盖）。 */
export const DEFAULT_TOOLBAR = ['filter'] as const
/** 对标 L88-89：`limit: 20` / `limits: [20, 30, 50]`（左表；右表 `page:false` 用不到）。 */
export const PAGE_LIMIT = 20
export const PAGE_LIMITS = [20, 30, 50] as const
/**
 * 「操作时间」列的 `field`——`buildCols()` 里**唯一**带 `sort: true` 的列（对标 L84）。
 * 它是**复刻值**（列上的 `sort: true` 不动），排序事件按这个 `field` 认列 ⇒ 两处必须同源。
 */
export const SORT_FIELD = 'actionTime'
/** 沿用原 `artifactPage` 渲染器给空表写的文案（见文件头第 4 条）。 */
const NONE_TEXT = '暂无数据'
/** 「操作人员」控件的 `id`（证据件 L149 的 `xm-select="recUser"`）。 */
const ACTOR_ID = 'recUser'
/** 同一个控件的 CSS 选择器（⚠️ 与 `ACTOR_ID` 分成两个常量，E-0061 ①）。 */
const ACTOR_SELECTOR = `#${ACTOR_ID}`

/** 列的宽松类型（`buildCols` 的产物：`templet` 是函数，其余是 layui 的键值）。 */
export type ActionLogCol = Record<string, unknown>

/**
 * 左表 7 列：宽度/对齐/`sort` 逐条照 `TradingOrderActionLogList.js` **L58-86**；
 * 「订单标题」→「变更内容」那一列是本线的**有意差异**（文件头第 2 条）。
 *
 * 只有 3 列带 `templet`（系统单号 / 交易单号两个锚 + 变更内容）：其余列**没有** `templet`，
 * layui 会按 `escape: true`（2.13.9 的默认）当**纯文本转义**——正是我们要的（安全且与对标同形）。
 */
export function buildCols(): ActionLogCol[] {
  return [
    {
      field: 'viewOrderId',
      title: '系统单号',
      width: 150,
      align: 'left',
      // 对标：`<a href="javascript:void(0);" class="orderdetails" data-viewId="…" data-id="…"
      // style="color:#0094ff" title="点击查看订单详情">…</a>`（L60-64）。
      // ⚠️ 我们**不带 `data-id`**（那是它的订单**主键**，本页拿不到、也不该拿，spec §2.2/D12）；
      // 单号放 `data-order-no`，由点击处理器读出来过 `/^\d{20}$/` 形状闸。
      templet: (d: LogRow): string =>
        `<a href="javascript:void(0);" class="orderdetails" data-order-no="${escapeHtml(
          d.viewOrderId,
        )}" style="color:#0094ff" title="点击查看订单详情">${escapeHtml(d.viewOrderId)}</a>`,
    },
    {
      field: 'tid',
      title: '交易单号',
      width: 190,
      align: 'left',
      // 对标 L66-70：`class="viewPlatmTid" style="color:#1e9fff" title="平台交易单号：…"`。
      // ⚠️ 照对标渲染成**蓝色链接**（spec §3-D13：不置灰），但点击只提示「尚未开放」——
      // 它开的那个交易详情弹层我们没有。
      // ⚠️ `title` **只写事实**（spec §10.2）：对标的原文后半截是「点击就能打开交易详情界面」，
      // 而我们点了只提示「尚未开放」⇒ 留着那半句就是**做不到的承诺**（`AGENTS.md` §5-C）。
      templet: (d: LogRow): string =>
        `<a href="javascript:void(0)" class="viewPlatmTid" style="color:#1e9fff" title="平台交易单号：${escapeHtml(
          d.tid,
        )}" data-tid="${escapeHtml(d.tid)}">${escapeHtml(d.tid)}</a>`,
    },
    { field: 'gameName', title: '游戏名称', width: 100, align: 'center' },
    {
      field: 'title',
      title: '变更内容',
      width: 200,
      align: 'center',
      // 值在 `toLogRow` 里就拼好（`formatChange`），这里只做转义——
      // 拼 HTML 的 `→` 是 HTML 上下文，文本必须转义（layui 对 templet 的返回值**不再**转义）。
      templet: (d: LogRow): string => escapeHtml(d.title),
    },
    { field: 'actionName', title: '操作说明', width: 100, align: 'center' },
    { field: 'actionUserName', title: '操作人员', width: 100, align: 'center' },
    { field: 'actionTime', title: '操作时间', width: 130, align: 'center', sort: true },
  ]
}

/** 右表「操作统计」3 列，逐条照同 JS L171-175（`totalCount` 的 `sort: true` 是复刻值）。 */
export function buildCols2(): ActionLogCol[] {
  return [
    { field: 'actionName', title: '操作', width: 100, align: 'center' },
    { field: 'actionUserName', title: '操作人', width: 130, align: 'center' },
    { field: 'totalCount', title: '操作次数', width: 100, align: 'center', sort: true },
  ]
}

/** 导出给单测对拍表头序列（两张表，与页面真正交给 layui 的那份一致）。 */
export const COLS_FOR_TEST: ActionLogCol[] = buildCols()
export const COLS2_FOR_TEST: ActionLogCol[] = buildCols2()

/**
 * 左表 `table.render` 里**不随每次取数变化**的那部分。
 *
 * 单测钉它、`mount` 里 `{...LEFT_TABLE_OPTIONS, where, parseData, done}` 展开它
 * ⇒ 这几条**静默出错**的键（`toolbar`/`defaultToolbar`/`page`/`autoSort`/`request`）不是
 * 「只在测试里对」，就是线上那一份。
 */
export const LEFT_TABLE_OPTIONS: Record<string, unknown> = {
  elem: TABLE_ELEM,
  // ⚠️ 服务端分页必须走 layui 的 `url`：`data` 模式只会在本地分页当前这一页的数组，
  // 「共 N 条 / 总页数」全错。对标的 `url` 是它的 Aj* 端点，这里换我们自己的。
  url: ORDER_ACTION_LOGS_URL,
  method: 'get',
  // ⚠️ **关掉本地假排序**（vendored layui 的默认值是 `true`）：开着的话点「操作时间」箭头
  // 只会重排**当前页那 20 行**。关掉后由 `sort(...)` 处理器改成**服务端排序**（spec §5）。
  autoSort: false,
  // layui 默认发 `page`/`limit`；后端分页器认的是 `page`/`page_size`（`EnvelopePagination`）。
  request: { pageName: 'page', limitName: 'page_size' },
  toolbar: TOOLBAR_ELEM,
  defaultToolbar: [...DEFAULT_TOOLBAR],
  page: { limit: PAGE_LIMIT, limits: [...PAGE_LIMITS] },
  cols: [buildCols()],
  text: { none: NONE_TEXT },
}

/**
 * 右表 `table.render` 里**不随每次取数变化**的那部分：**只有 `elem` 与 `page:false`**。
 *
 * ⛔ **不许给它加 `toolbar` / `defaultToolbar`**：那样右表会凭空长出工具区（evidence 实测
 * `#tableList2` 的 `.layui-table-tool` 不存在）。`data` / `cols` 由 `mount` 每次渲染时给。
 */
export const RIGHT_TABLE_OPTIONS: Record<string, unknown> = {
  elem: TABLE2_ELEM,
  page: false,
}

/**
 * 行数据（**用对标的字段名**，让列的 `field`/`sort`/宽度保持逐字复刻）。
 * `parseData` 把接口行映射到这里；`field` 名与接口键名的对应见 `toLogRow`。
 */
export type LogRow = {
  /** 「系统单号」列 = 接口 `order_no`（⛔ 不是 `target_id`）。 */
  viewOrderId: string
  /** 「交易单号」列 = 接口 `source_order_no`。 */
  tid: string
  /** 「游戏名称」列 = 接口 `game_name`。 */
  gameName: string
  /** 「变更内容」列 = `from_value → to_value`（已在 `toLogRow` 里拼好）。 */
  title: string
  /** 「操作说明」列 = 接口 `action_label`（短名）。 */
  actionName: string
  /** 「操作人员」列 = 接口 `actor_display`（昵称；取不到时回退 `actor`）。 */
  actionUserName: string
  /** 「操作时间」列 = `created_at` 过 `formatCreatedAt`（列的 `templet` 不再吃 ISO）。 */
  actionTime: string
}

/**
 * 「变更内容」单元格的文案：`from → to`。
 *
 * 规则（spec §3-D5 + 本页对**只有一侧有值**那一档的读法）：
 * - 两侧都空 ⇒ **空串**（⛔ 不写 `—`、⛔ 不复用 `summary`：没有变更值就说没有，不造占位符）；
 * - 只有一侧有值 ⇒ **只显示那一侧**（⛔ 不渲染「`→ 95.00`」这种半截箭头：`order.create` 这类
 *   动作只写 `to_value`，带上悬空箭头后**大多数行**都会以一个孤零零的箭头开头）；
 * - 两侧都有 ⇒ `from → to`（箭头两侧各一个空格，与 spec 正文的写法一致）。
 */
export function formatChange(from: unknown, to: unknown): string {
  const before = String(from ?? '').trim()
  const after = String(to ?? '').trim()
  if (before === '' && after === '') return ''
  if (before === '') return after
  if (after === '') return before
  return `${before} → ${after}`
}

/** 接口行 → 列数据（7 个列字段）。 */
export function toLogRow(item: OrderActionLogRow): LogRow {
  return {
    viewOrderId: item.order_no ?? '',
    tid: item.source_order_no ?? '',
    gameName: item.game_name ?? '',
    title: formatChange(item.from_value, item.to_value),
    // 「操作说明」= **短名**（spec §3-D6）：⛔ 不要在这里退回 `summary` 整句（100px 宽的列会被截半句）。
    actionName: item.action_label ?? '',
    // 「操作人员」显示昵称；`actor_display` 取不到时退回行里存的账号名，不能变成空白
    // （与 `pages/system-systemlog.ts::toLogRow` 同一处置）。
    actionUserName: item.actor_display ?? item.actor ?? '',
    actionTime: formatCreatedAt(item.created_at),
  }
}

/** 右表一行（**用对标的字段名**：操作 / 操作人 / 操作次数）。 */
export type SummaryRow = {
  actionName: string
  actionUserName: string
  totalCount: number
}

/** 接口的统计行 → 右表行。 */
export function toSummaryRow(item: OrderActionLogSummaryRow): SummaryRow {
  return {
    actionName: item.action_label ?? '',
    // 「操作人」显示**账号名称**（2026-10-17 线新增的 `actor_display`）；
    // 旧后端不回这个键时回落登录账号（部署窗口，⛔ 不能变成空白）。
    actionUserName: item.actor_display ?? item.actor ?? '',
    totalCount: item.count ?? 0,
  }
}

/**
 * 「操作人员」下拉的候选取舍（2026-10-17 线，spec §C1/C2）。
 *
 * **优先** `data.actor_options`（`{actor, display}`）：显示账号名称、提交登录账号。
 * 旧后端只回 `data.actors: string[]`（部署窗口）⇒ 兜底成「文本 = 值 = 账号」——
 * 与改造前的行为逐字相同，页面不会因为少一个键就变空。
 *
 * ⚠️ 两个键**同时**存在时只认 `actor_options`（它们由后端同一处 `_actors()` 派生，
 * 前端取哪份都一样；此处只保留**一个**出口，⛔ 不要拿两份数据做合并/去重）。
 */
export function toActorOptions(
  data: { actor_options?: OrderActionLogActorOption[]; actors?: string[] } | undefined,
): OrderActionLogActorOption[] {
  if (Array.isArray(data?.actor_options)) return data.actor_options
  return (data?.actors ?? []).map((actor) => ({ actor, display: actor }))
}

/**
 * 「操作日期」的默认区间：**今天 − 1 个自然月 ~ 今天**（`yyyy-MM-dd`，本地时区）。
 *
 * 出处：证据件 HTML L120/L124 的 `value` 是 `2026-08-20` / `2026-09-20`，快照日 2026-09-20
 * ⇒ 08-20→09-20 是 **31 天**（8 月有 31 天）⇒ **自然月**口径（spec §3-D10），
 * ⛔ 不是「30 天」、⛔ 也不是 `system-systemlog.ts` 的「昨天 00:00 ~ 今天 23:59」（那是**另一页**
 * 对标的默认值）。算式与机制在 `@/layui-island/dateRange::dayRangeMonthsAgo`，这里只传偏移。
 */
export function defaultActionLogDateRange(now: Date = new Date()): DateRange {
  return dayRangeMonthsAgo(1, now)
}

/** 筛选栏里那几个控件的值（**DOM 的 name/id**：`sDate`/`eDate`/`actionSelect`/`recUser`/`key1`）。 */
export type ActionLogFilterValues = {
  sDate?: string
  eDate?: string
  actionSelect?: string
  /**
   * formSelects 的 `valStr`（= 控件里选中的**值**）。2026-10-17 线起候选来自
   * `data.actor_options`：**显示账号名称、值是登录账号**（`fillActorsOnce` 把两者分开灌）
   * ⇒ 这里读到的一直是登录账号，送去后端 `actor` 筛选不会因为「昵称≠账号」而查空。
   */
  recUser?: string
  key1?: string
}

/**
 * DOM 名 → 接口参数名（对标 `reloadOrderList` 里那段 `seachParam` 是对应物，L108-113）。
 *
 * 这里**只改名字**（`sDate→stime`、`actionSelect→action`、`recUser→actor`）并把空值交给
 * `toOrderActionLogParams`——线上形状只有那一个出口。
 *
 * ⚠️ `ordering` 是**第二个入口参数**（不是 DOM 字段）：排序状态不来自筛选栏，但同样只在这条链上
 * 汇进 `toOrderActionLogParams`——⛔ 不要在页面里另拼一次查询参数。
 */
export function toOrderActionLogSearchParams(
  values: ActionLogFilterValues,
  ordering?: OrderActionLogOrdering,
): OrderActionLogSearchParams {
  return {
    stime: (values.sDate ?? '').trim(),
    etime: (values.eDate ?? '').trim(),
    action: (values.actionSelect ?? '').trim(),
    actor: (values.recUser ?? '').trim(),
    key1: (values.key1 ?? '').trim(),
    ordering,
  }
}

/**
 * layui `sort(<filter>)` 事件的载荷（vendored `public/layui/layui.js` 的
 * `layui.event.call(l.field, W, "sort("+n+")", …initSort)`）：`field` 是列的 `field`，
 * `type` 是 `'asc' | 'desc' | null`（**第三次点击**是 `null` = 取消排序）。
 */
export type LogSortEvent = { field?: unknown; type?: unknown }

/**
 * layui 的 `field` / `type` → 后端 `ordering` 词表（`OrderActionLogOrdering`）。
 *
 * **显式映射**（不吃 layui 的隐式约定）：
 * - 只认「操作时间」列（`SORT_FIELD`）；别的列没有 `sort: true`，认不出 ⇒ `undefined`；
 * - `asc` → `created_at`（旧在前）、`desc` → `-created_at`（新在前）；
 * - `type` 是 `null`（第三次点击「取消排序」）或其它认不出 ⇒ `undefined`，
 *   回到**不传** `ordering` 的请求形状（后端默认序 = 最新在前）。
 */
export function toOrderActionLogOrdering(
  field: unknown,
  type: unknown,
): OrderActionLogOrdering | undefined {
  if (field !== SORT_FIELD) return undefined
  if (type === 'asc') return 'created_at'
  if (type === 'desc') return '-created_at'
  return undefined
}

/** 后端响应的信封形状（`parseData` 吃的那一份）。 */
export type ActionLogEnvelope = {
  code: number
  msg: string
  data?: {
    items?: OrderActionLogRow[]
    total?: number
    summary?: OrderActionLogSummaryRow[]
    actors?: string[]
    /**
     * 「操作人员」下拉的候选（`{actor,display}`，2026-10-17 线新增）：**显示账号名称、值是登录账号**。
     * 旧后端不回它 ⇒ 用下面的 `actors` 兜底（`toActorOptions`）。
     */
    actor_options?: OrderActionLogActorOption[]
    /** 「操作动作」下拉的候选（`{code,label}` 逐条，spec §3-D3）——**唯一出处是后端**。 */
    actions?: OrderActionLogActionOption[]
  }
}

/** formSelects 里我们用到的方法（`vendor.ts` 的公开类型只声明了 `render`/`value`，这里按需收窄）。 */
type FormSelectsLike = {
  data?: (id: string, type: string, opts: unknown) => void
  value?: (id: string, type?: string) => unknown
}

/** 挂载整页；返回卸载函数。 */
export async function mountOrderActionLogPage(
  root: HTMLElement,
  _ctx: IslandContext,
): Promise<() => void> {
  // ⚠️ 挂载顺序（E-0067）：`bootstrapIsland` 挂样式 → 取证据件 → `root.innerHTML` →
  // `initIslandElements`（它绑的是标记里的页签），顺序反了会 FOUC / 绑定落空。
  const layui = await bootstrapIsland(['form', 'table', 'layer', 'laydate'])
  const form = layui.form as LayuiModule
  const table = layui.table as LayuiModule
  const layer = layui.layer as LayuiModule
  const laydate = layui.laydate as LayuiModule

  // 对标 `<title>` 是服务端渲染的（`Order__TradingOrderActionLogList.html` L5）。
  document.title = '交易订单操作记录管理列表'
  root.innerHTML = await loadConfigArtifact(ARTIFACT_KEY)
  initIslandElements(layui)

  const msg = (text: string): void => {
    ;(layer.msg as (t: string, o?: unknown) => void)(text, { time: 1500 })
  }

  // —— 操作日期：填默认区间（今天 − 1 个自然月 ~ 今天）+ 交给 laydate。
  //    参数照对标 L31-39 逐条：两个都是 `type:'date'`，**起点**那一侧另有 `min:-90`。
  const { sDate, eDate } = defaultActionLogDateRange()
  const sInput = root.querySelector<HTMLInputElement>('input#sDate')
  const eInput = root.querySelector<HTMLInputElement>('input#eDate')
  if (sInput) sInput.value = sDate
  if (eInput) eInput.value = eDate
  ;(laydate.render as (o: unknown) => void)({ elem: '#sDate', type: 'date', min: -90 })
  ;(laydate.render as (o: unknown) => void)({ elem: '#eDate', type: 'date' })

  // —— 操作动作：**控件保留**（位置/结构照证据件），但选项换成**后端随响应给的那些动作短名**。
  //
  //    候选**由后端随响应给**（`data.actions`，spec §3-D3）——短名的唯一出处留在后端
  //    （`apps/audit/actions.py::ORDER_ACTION_LABELS`），⛔ 前端**不镜像**那份常量：
  //    两处各写同一批中文短名必有一处先漂移，而漂移的症状（下拉与表格的短名不一样）**没有网会报**。
  const actionSelect = root.querySelector<HTMLSelectElement>('select#actionSelect')

  /** 重建「操作动作」的选项：**「全部」+ 后端给的那一份**（`value` 用 code、显示用 label）。 */
  function setActionOptions(actions: OrderActionLogActionOption[]): void {
    if (!actionSelect) return
    const options: OrderActionLogActionOption[] = [
      // 「全部」由本页拼（后端只给真实动作）；它的 `value` 是空串 ⇒ 参数组装层不会发 `action`。
      { code: ACTION_ALL_VALUE, label: '全部' },
      ...actions,
    ]
    actionSelect.innerHTML = options
      .map((o) => `<option value="${escapeHtml(o.code)}">${escapeHtml(o.label)}</option>`)
      .join('\n')
    // 默认停在「全部」（= 第一项）：不给 `selected` 属性时浏览器会选中第 0 项，行为一致。
    actionSelect.selectedIndex = 0
  }

  // ⚠️ 挂载时**先把对标的静态选项清掉**（证据件里那 11 个是**它自己的**动作词表，与本页参数
  //    不对应 ⇒ 留着的话，响应回来之前用户能选中一个「选了没结果」的选项）。后端候选到了再填。
  setActionOptions([])
  ;(form.render as (type?: string) => void)('select')

  // —— 操作人员：formSelects 壳（证据件 L149 的 `xm-select="recUser" xm-select-radio`）。
  //    候选**不是**另发一次请求，而是跟着列表响应 `data.actors` 一起来（spec §3-D9）——
  //    所以这里只渲染壳，灌数据在左表的 `done` 里（见 `fillActorsOnce`）。
  initFormSelects(ACTOR_SELECTOR)
  const formSelects = getFormSelects() as FormSelectsLike | undefined

  /** 读筛选栏（DOM 名原样；`#recUser` 的值只能从 formSelects 读——插件把 select 的 `name` 摘掉了）。 */
  function readFilterValues(): ActionLogFilterValues {
    // ⚠️ 取证件的 `<form>` **没有** `lay-filter`（系统跟踪日志页有），所以这里不用
    // `form.val('form')`（那要求 `form[lay-filter="form"]`，本页恒取不到）——直接读 DOM。
    const actorValue = formSelects?.value?.(ACTOR_ID, 'valStr')
    return {
      sDate: root.querySelector<HTMLInputElement>('input#sDate')?.value ?? '',
      eDate: root.querySelector<HTMLInputElement>('input#eDate')?.value ?? '',
      actionSelect: root.querySelector<HTMLSelectElement>('select#actionSelect')?.value ?? '',
      recUser: typeof actorValue === 'string' ? actorValue : '',
      key1: root.querySelector<HTMLInputElement>('input#key1')?.value ?? '',
    }
  }

  /**
   * 当前排序状态（`undefined` = 没点过排序 / 点了「取消排序」）。
   * 它是页面闭包状态、不是 DOM 字段：`readFilterValues` 读不到它，所以经
   * `toOrderActionLogSearchParams` 的第二个参数汇进 `currentWhere()`（唯一的参数形状出口）。
   */
  let sortOrdering: OrderActionLogOrdering | undefined

  /** 当前筛选对应的**线上参数**（唯一形状出口）。 */
  const currentWhere = (): Record<string, string | number> =>
    toOrderActionLogParams(toOrderActionLogSearchParams(readFilterValues(), sortOrdering))

  /** 重新取数。默认回到第 1 页（搜索/刷新/排序都是「重看一遍」，spec §5）。 */
  function reload(page = 1): void {
    ;(table.reload as (id: string, o: unknown) => void)(TABLE_ID, {
      where: currentWhere(),
      page: { curr: page },
    })
  }

  // —— 右表（操作统计）：拿左表响应里的 `data.summary` 渲染。
  //    对标也是在左表 `done` 里 `table.render` 右表（L168-181），顺序同。
  function renderSummary(rows: OrderActionLogSummaryRow[]): void {
    ;(table.render as (o: unknown) => void)({
      ...RIGHT_TABLE_OPTIONS,
      cols: [buildCols2()],
      data: rows.map(toSummaryRow),
    })
  }

  // —— 「操作人员」候选：来自响应的 `data.actor_options`（**优先**）或 `data.actors`（旧后端兜底）。
  //    显示账号名称（`display`）、提交登录账号（`actor`）——两者**必须分开**：拿昵称去筛选会查空，
  //    拿账号名显示就是本线要修的那个问题。
  //    ⚠️ **只灌一次**（对标也是页面加载时 `getAllRecUserData()` 一次）：每次响应都重灌会**重置
  //    用户当前的选择**（选完点搜索 ⇒ 选择被清掉、筛选静默失效）。真到「日志里出现了新人」时，
  //    刷新整页即可——代价比静默丢筛选小。
  let actorsFilled = false

  function fillActorsOnce(options: OrderActionLogActorOption[]): void {
    if (actorsFilled || options.length === 0) return
    formSelects?.data?.(ACTOR_ID, 'local', {
      arr: options.map((option) => ({ name: option.display, value: option.actor })),
    })
    actorsFilled = true
  }

  // —— 「操作动作」的候选（`data.actions`）：同样**只填一次**，理由与上面那条一模一样
  //    （重填会把用户选中的动作重置回「全部」）。后端那一份是静态词表，没有「过期」问题。
  let actionsFilled = false

  function fillActionsOnce(actions: OrderActionLogActionOption[]): void {
    if (actionsFilled || actions.length === 0) return
    setActionOptions(actions)
    // ⚠️ 换完 `<option>` 必须重渲下拉壳，否则页面上还是旧的空壳。
    ;(form.render as (type?: string) => void)('select')
    actionsFilled = true
  }

  // —— 搜索：`form.on('submit(seachBtn)')`（对标 L152-156）。layui 的 form 模块对
  //    `*[lay-submit]` 做了文档级委托，所以这里不需要额外的点击监听。
  ;(form.on as (e: string, cb: () => void) => void)('submit(seachBtn)', () => {
    reload(1)
    return false
  })

  // —— 「回车 = 搜索」（spec §10.1）。作用范围**写死**在筛选栏的自由文本输入框上
  //    （本页只有 `key1`）：Enter 走与「搜索」**同一条路**（`reload(1)`，`page` 回 1）。
  //    ⛔ **不绑** `sDate` / `eDate` 两个日期输入框：laydate 面板自己把 Enter 当「确定」
  //    （vendored `public/layui/layui.js`），再绑一次会与它抢同一个键；
  //    「操作人员」「操作动作」是下拉，Enter 不适用。
  //    ⚠️ vendored layui 的 form 模块**没有** Enter 提交（全页 `keydown` 零命中）⇒
  //    没有这一段，「按回车没反应」是**静默**的。
  const key1Input = root.querySelector<HTMLInputElement>('input#key1')
  const onKey1Enter = (event: KeyboardEvent): void => {
    if (event.key !== 'Enter') return
    // 与「搜索」同一条路：⛔ 不另拼一套参数、⛔ 不绕开 `currentWhere()`。
    event.preventDefault()
    reload(1)
  }
  key1Input?.addEventListener('keydown', onKey1Enter)

  // —— 工具栏唯一的「刷新数据」（对标 L140-146；文案逐字取自证据件 L176）。
  ;(table.on as (e: string, cb: (obj: { event?: string }) => void) => void)(
    `toolbar(${TABLE_FILTER})`,
    (obj) => {
      if (obj.event === 'refresh') reload(1)
    },
  )

  // —— 排序：`autoSort:false` 关掉了 layui 的**本地**假排序（它只重排当前页那 20 行）。
  //    这里把 layui 的 `field`/`type` 映射成后端 `ordering` 词表后**重新取数**（回第 1 页）。
  //    ⚠️ 复刻值 `sort: true` 留在列上不动（`buildCols`），本行只接管事件。
  ;(table.on as (e: string, cb: (obj: LogSortEvent) => void) => void)(
    `sort(${TABLE_FILTER})`,
    (obj) => {
      // 认不出的列 / `type === null`（第三次点击「取消排序」）⇒ `undefined` ⇒ 不发 `ordering`。
      sortOrdering = toOrderActionLogOrdering(obj.field, obj.type)
      reload(1)
    },
  )

  // —— 左表渲染：`parseData` 把统一信封翻成 layui 要的 `{code,msg,count,data}`，
  //    顺带把 `summary`/`actor_options`（或旧后端的 `actors`）拆到**这次 mount 自己的**闭包；
  //    `done` 里渲染右表 + 灌人员下拉 + 重渲表单壳。
  //
  // ⚠️ 这两份数据**只能**是闭包状态（⛔ 不能放模块级变量：同一文档里挂两次会互相踩）。
  //    它们不进 layui 的表格载荷——layui 只认 `code`/`msg`/`count`/`data` 四个键。
  let pendingSummary: OrderActionLogSummaryRow[] = []
  let pendingActors: OrderActionLogActorOption[] = []
  let pendingActions: OrderActionLogActionOption[] = []
  ;(table.render as (o: unknown) => void)({
    ...LEFT_TABLE_OPTIONS,
    // 首屏就带上默认日期区间（否则会拉全表最新 20 条，与筛选栏显示的值不一致）。
    where: currentWhere(),
    parseData: (res: ActionLogEnvelope) => {
      // 赋值发生在 layui 读返回值**之前**（`done` 总在 `parseData` 之后被调）。
      pendingSummary = res.data?.summary ?? []
      // 新后端给 `actor_options`（文本/值分离）；旧后端只有 `actors` ⇒ 文本=值=账号兜底。
      pendingActors = toActorOptions(res.data)
      pendingActions = res.data?.actions ?? []
      return {
        code: res.code,
        msg: res.msg,
        count: res.data?.total ?? 0,
        data: (res.data?.items ?? []).map(toLogRow),
      }
    },
    done: () => {
      ;(form.render as (type?: string) => void)()
      renderSummary(pendingSummary)
      fillActorsOnce(pendingActors)
      fillActionsOnce(pendingActions)
    },
  })
  ;(form.render as (type?: string) => void)()

  /** 「系统单号」点开订单详情（对标 `.orderdetails` → `layer.open`，L134-147）。 */
  function openOrderDetailLayer(orderNo: string): void {
    // ⛔ **开层前的「单号形状闸」`/^\d{20}$/`**——与 `/orders/:order_no(\d{20})` 路由自己的约束
    // 同源（后端 `Order.order_no` 的 `lookup_value_regex = r"\d{20}"`）。**为什么必须有**：
    // `layer.open` 的 `title` 是 **HTML 上下文**（vendored `layui.js` 把 `a.title` 直接拼进
    // `<div class="layui-layer-title">`），而 `dataset` 会把 attribute 实体**解码回** `<`/`>`
    // ⇒ templet 里那次 `escapeHtml` 在这条链上**不构成保护**。不匹配 ⇒ **不开层**，
    // 只给一句**固定文案**（⛔ 不把那份不可信输入拼进提示）。先例与理由：
    // `pages/order-orderlist.ts:5925-5944`。
    if (!/^\d{20}$/.test(orderNo)) {
      msg('订单号格式不正确，无法打开订单详情')
      return
    }
    ;(layer.open as (o: unknown) => void)({
      // 对标 `title: viewId + "订单详情"`（L136）；`orderNo` 已过形状闸（**纯数字**），
      // 仍过一遍 `escapeHtml` 是因为这里的上下文是 HTML，边界上转义才有效。
      title: `${escapeHtml(orderNo)}订单详情`,
      shade: [0.3, '#333'],
      shadeClose: true,
      type: 2,
      skin: 'layui-layer-lan',
      anim: 8,
      area: ['85%', '92%'],
      // ⛔ **不走 `ctx.onNavigate`**：4 个订单/绩效页**刻意**从它改开层
      //（`order-orderlist.ts` 的注释里写了理由）；基址用 `BASE_URL`，⛔ 不写死 `/`。
      content:
        `${import.meta.env.BASE_URL}layui-island/page.html?page=order-orderview` +
        `&order_no=${encodeURIComponent(orderNo)}`,
    })
  }

  // —— 两个行内链接的点击（artifactPage 的兜底收口已经没有了，本页自己接管）。
  const onClick = (event: Event): void => {
    const target = event.target as HTMLElement
    const detail = target.closest<HTMLElement>('a.orderdetails')
    if (detail && root.contains(detail)) {
      event.preventDefault()
      openOrderDetailLayer(detail.dataset.orderNo ?? '')
      return
    }
    const plat = target.closest<HTMLElement>('a.viewPlatmTid')
    if (plat && root.contains(plat)) {
      event.preventDefault()
      // 对标点它开的是**交易详情**弹层（`platfromorderview?orderId=&tid=`）——我们没有这一页
      // ⇒ 照 `AGENTS.md` §5-D「无后端的部分照对标渲染 + 点了提示尚未开放」，**不置灰**。
      msg('「交易详情」尚未开放')
    }
  }
  root.addEventListener('click', onClick)

  return () => {
    key1Input?.removeEventListener('keydown', onKey1Enter)
    root.removeEventListener('click', onClick)
    // 只清我们注入的标记；`layer` 的弹层挂在 `document.body`，由 layui 自己收。
    root.innerHTML = ''
  }
}

export default {
  mount: mountOrderActionLogPage,
} satisfies IslandPage
