/**
 * 【系统跟踪日志】页的 layui island（对标 `/System/systemlog`）——**已接后端**。
 *
 * ⚠️ 页标题取对标的 **`<title>系统跟踪日志</title>`**（`System__systemlog.html` L5），
 * **不是**菜单里的「系统操作日志管理」——两者在对标物上就不一致（见本轮任务说明）。
 *
 * ## 证据出处
 *
 * | 项 | 出处 |
 * |---|---|
 * | 页内 `<style>`（两段）、筛选栏、body 标记、工具栏模板 | `public/replica/system-systemlog.html` ← `.tools/recon/suqing1/html/System__systemlog.html` **L24-41 + L52-164**（逐字；只删了 `<script src>` 与 HTML 注释，PII 无） |
 * | 表头 `cols` | `.tools/recon/suqing1/assets/JS2019/System/systemlog.js` **L51-133** |
 * | `table.render` 参数 | 同上 **L42-137** |
 * | 搜索/刷新的行为 | 同上 **L140-176**（`toolbar(tableList)` 的 `refresh`、`submit(seachBtn)`、`reloadOrderList()`） |
 *
 * ## 渲染参数取值（逐条，全部取自 `systemlog.js`）
 *
 * | 参数 | 值 | 出处 |
 * |---|---|---|
 * | `height` | `'full-80'` | L45（layui 认这个写法：视口高 − 80，对标实测表格视图 920） |
 * | `cellMinWidth` | `150` | L46（把 70/100/120/130 四列抬到 150 → 实测渲染 151） |
 * | `defaultToolbar` | `['filter']` | L50（同对象里写了两次，后者生效） |
 * | `page` | `{ limit: 30, limits: [10,20,30,40,50,100] }` | L134-136（**六项**，不是默认五项） |
 *
 * ## 与对标「1:1」的四次**有意差异**（都是本线的接线要求，不是抄错）
 *
 * 1. **端点换成我们的**：对标打 `/SystemAjax/GetSysmteLogList?…`（`systemlog.js` L44，注意
 *    `Sysmte` 的拼写），我们打 `GET /api/audit/logs/`（spec §6.1）。对标的端点名不再出现在代码里。
 * 2. **「游戏筛选」的 `<option>` 换成我方游戏**：对标那 13 个游戏名是**它的租户数据**，
 *    我们挂载时用 `GET /api/business/games/` 重建（保留「全部游戏」这一项，值改成空串 =
 *    不加筛选）。「日志分类」的 9 个 `<option>` 是**照抄值**（8 分类 + 全部），一个都不动。
 * 3. **「记录日期」的默认值是运行时算的**：对标那两个 `value` 是**服务端渲染**进 HTML 的
 *    （`systemlog.js` 的 `laydate.render` 没有 `value`），照抄证据件里的 `2026-09-19 ~ 2026-09-20`
 *    会让页面永远只筛那个过期窗口。重建口径见 `defaultLogDateRange`；同时补上对标那两处
 *    `laydate.render({type:'datetime'})`——旧版因为「日期筛选尚未开放」刻意没接，现在用得上。
 * 4. **「操作时间」的箭头改走服务端排序**（2026-09-22，Task O；spec §16.2 第 1 条的裁决）：
 *    列上的 `sort: true` 是**复刻值、保留不动**，但 `table.render` 传 `autoSort: false`
 *    关掉 layui 的**本地**假排序（它只重排当前页 30 行），改由 `sort(...)` 事件把
 *    `field`/`type` 映射成后端 `ordering` 词表后重新取数（回第 1 页）。未点排序时**不发**
 *    该参数，请求形状与今天一致。
 *
 * ⚠️ **R29：「相关人员」保持对标原样的单选**。早先本页曾按 spec §6.1 的
 * （后来被证伪的）「多选」描述把 `#recUser` 的 `xm-select-radio` 摘掉——那会让下拉的快捷按钮条
 * 从对标的「清空」变成「全选/清空/反选」，是一处**没有对标依据的可见差异**。现已改回：
 * 该属性**留在证据件里不动**，控件是单选，页面只发一个 `username`。
 * 后端 `actor` 仍**同时接受**重复参数与逗号分隔的多值（那是 API 契约，见 `api/auditLogs.ts`），
 * **两侧都不要「顺手修正」对方**。
 *
 * ## 列的渲染（对标的 `templet` 逐条照抄，取我们的字段）
 *
 * 对标的 8 列里除「所属游戏」外都是 `<a href="javascript:void(0)">` 锚（`systemlog.js` L51-133），
 * 只有 `title` 属性、**没有任何点击行为**。我们照抄这个外观（含 `class=""` / `style=""` 的差异），
 * 所以「相关ID」是**纯文本不跳转**：值 = 行里的 `target_id`，不发明任何导航。
 *
 * 「操作说明」取 `summary`；「操作时间」取 `created_at` 并过 `formatCreatedAt`（后端回 ISO-8601，
 * 对标列是 `Y-m-d H:i:s`——Task 5 明确交接的事）。
 */
import {
  AUDIT_ACTORS_URL,
  AUDIT_LOGS_URL,
  formatCreatedAt,
  LOG_TYPE_LABELS,
  toAuditLogParams,
  type AuditLogOrdering,
  type AuditLogRow,
  type AuditLogSearchParams,
} from '@/api/auditLogs'
import { fetchGames, type ConfigRow } from '@/api/config'
import { loadConfigArtifact } from '@/layui-island/config-replica'
import {
  bootstrapIsland,
  initIslandElements,
  type IslandContext,
  type IslandPage,
} from '@/layui/island'
import type { LayuiModule } from '@/layui/loader'
import { yesterdayDateRange } from '@/layui-island/dateRange'
import { initFormSelects } from '@/layui/vendor'

/** 证据件名（= `public/replica/<key>.html`）。 */
export const ARTIFACT_KEY = 'system-systemlog'
/** 对标 `table.render` 的 `elem` / `lay-filter`（`systemlog.js` L43、HTML L133）。 */
export const TABLE_ID = 'tableList'
export const TABLE_FILTER = 'tableList'
/** 对标 `page.limit` / `page.limits`（L134-136）——**复刻值，不改**。 */
export const PAGE_LIMIT = 30
export const PAGE_LIMITS = [10, 20, 30, 40, 50, 100]

/**
 * 「操作时间」列的 `field`（`buildLogCols` 里**唯一**带 `sort: true` 的列）。
 * 它是**复刻值**（对齐 `systemlog.js` L51-133），由 `system-systemlog.spec.ts` 钉着——
 * 排序事件就是按这个 `field` 认列的，所以两处必须同源。
 */
export const SORT_FIELD = 'createTime'

/** 相关人员下拉（对标 HTML 的 `xm-select="recUser"`）。 */
const ACTOR_ID = 'recUser'
const ACTOR_SELECTOR = `#${ACTOR_ID}`

/** HTML 转义：`summary` / `actor` / `target_id` 都是请求来源的文本，直接拼进 `title` 会破标记。 */
function esc(value: unknown): string {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

/**
 * 行数据（**用对标的字段名**，让列的 `field`/`sort`/宽度保持逐字复刻）。
 * `parseData` 把接口行映射到这里。
 */
export type LogRow = {
  joinOrderId: string
  logType: number
  soureOrderId: string
  gameRoleName: string
  gameName: string
  remark: string
  userName: string
  createTime: string
  /** 动作 code——今天不渲染，映射出来是为了 `layui.table` 的事件/排障能拿到（不在任何列里）。 */
  action: string
}

/** 接口行 → 列数据。`created_at` 在这里就格式化，列的 `templet` 不再吃 ISO。 */
export function toLogRow(item: AuditLogRow): LogRow {
  return {
    joinOrderId: item.target_id ?? '',
    logType: item.log_type,
    // ⭐（D-0341）「来源订单号」列 = **来源号优先、内置单号兜底**（业务方
    //    D-0301 同一口径，与 `order-soureorderchangepricelog.ts` 的
    //    `orderIdView` 同族取法）。后端给的 `source_order_no` 已是「日志快照 → 订单
    //    当前值」回填后的原值（D7，`apps/audit/api/views.py::_order_backfill`）；
    //    它也为空（老行 / 单已删）时回退内置单号 `order_no`，不再是空格。
    //    ⚠️ 列头「来源订单号」与 8 列宽度是复刻值（`buildLogCols`），本线一个都不动。
    soureOrderId: item.source_order_no || (item.order_no ?? ''),
    gameRoleName: item.game_role_name ?? '',
    gameName: item.game_name ?? '',
    remark: item.summary ?? '',
    // 「操作用户」列**显示昵称**（spec §11.2）。⚠️ 回退必须保留：老接口/老桩数据没有
    // `actor_display` 这个键，取不到时退回行里存的账号名，不能变成空白。
    userName: item.actor_display ?? item.actor ?? '',
    createTime: formatCreatedAt(item.created_at),
    action: item.action ?? '',
  }
}

/** 分类 code → 中文；认不出的 code 回退成对标的默认值「全部」（`systemlog.js` 的 `vo = '全部'`）。 */
export function logTypeLabel(code: unknown): string {
  const key = Number(code)
  return LOG_TYPE_LABELS[key] ?? '全部'
}

/**
 * 主表 8 列（宽度/对齐/`sort`/`unresize` **逐条照抄** `systemlog.js` L51-133；
 * `templet` 照抄它的锚外观，取值换成我们的字段）。
 */
export function buildLogCols(): Record<string, unknown>[] {
  /** 对标 `joinOrderId` 那条：`class=""`，**没有** `style`。 */
  const plainNoStyle = (value: unknown): string =>
    `<a href="javascript:void(0);" class="" title="${esc(value)}">${esc(value)}</a>`
  /** 对标其余文本列：`style=""`（空值，不是没有属性）。 */
  const plain = (value: unknown): string =>
    `<a href="javascript:void(0);" style="" title="${esc(value)}">${esc(value)}</a>`
  return [
    {
      field: 'joinOrderId',
      title: '相关ID',
      width: 150,
      align: 'left',
      // 纯文本不跳转：只显示 `target_id`，不发明任何路由（spec §4.3/§8 的口径）。
      templet: (d: LogRow) => plainNoStyle(d.joinOrderId),
    },
    {
      field: 'logType',
      title: '日志分类',
      width: 70,
      align: 'center',
      templet: (d: LogRow) => logTypeLabel(d.logType),
    },
    {
      field: 'soureOrderId',
      title: '来源订单号',
      width: 200,
      align: 'left',
      templet: (d: LogRow) => plain(d.soureOrderId),
    },
    {
      field: 'gameRoleName',
      title: '订单角色名',
      width: 150,
      align: 'left',
      templet: (d: LogRow) => plain(d.gameRoleName),
    },
    // 对标这一列没有 `templet`（纯文本），照抄。
    { field: 'gameName', title: '所属游戏', width: 100, align: 'center' },
    // 对标 L116：这一列**没有 `width`**（自动列，吃掉定宽列之外的剩余宽度——实测渲染 542）。
    {
      field: 'remark',
      title: '操作说明',
      align: 'left',
      templet: (d: LogRow) => plain(d.remark),
    },
    {
      field: 'userName',
      title: '操作用户',
      width: 120,
      align: 'center',
      unresize: true,
      templet: (d: LogRow) => plain(d.userName),
    },
    {
      field: 'createTime',
      title: '操作时间',
      width: 130,
      align: 'center',
      sort: true,
      unresize: true,
      templet: (d: LogRow) => plain(d.createTime),
    },
  ]
}

/** 筛选栏里那几个控件的值（**DOM 的 name**：`sDate`/`eDate`/`logType`/`gameName`/`recUser`/`key1`/`key2`）。 */
export type LogFilterValues = {
  sDate?: string
  eDate?: string
  logType?: string
  gameName?: string
  /**
   * xm-select 的 `valStr`（= `keyVal` = **账号名**，不是下拉上显示的昵称，spec §11.2）。
   * **对标 `#recUser` 是单选**（`xm-select-radio`，R29）⇒ 这里是**一个**用户名；
   * 空串 = 没选。类型保持 `string`（不是列表）：页面只发单值，多值容忍在 `toAuditLogParams` 一侧。
   */
  recUser?: string
  key1?: string
  key2?: string
}

/**
 * layui `sort(<filter>)` 事件的载荷（vendored `public/layui/layui.js` 的
 * `layui.event.call(l.field, W, "sort("+n+")", …initSort)`）：`field` 是列的 `field`，
 * `type` 是 `'asc' | 'desc' | null`（**第三次点击**是 `null` = 取消排序）。
 */
export type LogSortEvent = { field?: unknown; type?: unknown }

/**
 * layui 的 `field` / `type` → 后端 `ordering` 词表（`AuditLogOrdering`）。
 *
 * **显式映射**（不吃 layui 的隐式约定）：
 * - 只认「操作时间」列（`SORT_FIELD`）；别的列没有 `sort: true`，认不出 ⇒ `undefined`；
 * - `asc` → `created_at`（旧在前）、`desc` → `-created_at`（新在前）；
 * - `type` 是 `null`（第三次点击「取消排序」）或其它认不出 ⇒ `undefined`，
 *   回到**不传** `ordering` 的请求形状（后端默认序 = 最新在前）。
 */
export function toLogOrdering(field: unknown, type: unknown): AuditLogOrdering | undefined {
  if (field !== SORT_FIELD) return undefined
  if (type === 'asc') return 'created_at'
  if (type === 'desc') return '-created_at'
  return undefined
}

/**
 * DOM 名 → 接口参数名（`systemlog.js` L157-165 的 `seachParam` 是对标的对应物）。
 *
 * 这里**只改名字**（`sDate→stime`、`key2→order_no`、`key1→key`…）并把单选的人员包成
 * 长度为 0/1 的列表；空值/`log_type=0`/列表→逗号串都留给 `toAuditLogParams`——线上形状只有那一个出口。
 * ⚠️ **页面不 split 逗号**：对标控件是单选（R29），值里出现逗号只可能是数据本身；
 * 「重复参数 / 逗号分隔」的容忍是**后端 API 契约**，由 `toAuditLogParams` 的 `join(',')` 与后端
 * `_search_params` 覆盖，不要在这里再造一层拆解。
 *
 * ⚠️ `ordering` 是**第二个入口参数**（不是 DOM 字段）：排序状态不来自筛选栏，
 * 但同样只在这条链上汇进 `toAuditLogParams`——**不要**在页面里另拼一次查询参数
 * （`currentWhere()` 是文件里写明的「唯一形状出口」）。
 */
export function toLogSearchParams(
  values: LogFilterValues,
  ordering?: AuditLogOrdering,
): AuditLogSearchParams {
  const logTypeRaw = (values.logType ?? '').trim()
  const logType = logTypeRaw === '' ? undefined : Number(logTypeRaw)
  const actor = (values.recUser ?? '').trim()
  return {
    stime: (values.sDate ?? '').trim(),
    etime: (values.eDate ?? '').trim(),
    log_type: logType !== undefined && Number.isFinite(logType) ? logType : undefined,
    game_name: (values.gameName ?? '').trim(),
    actor: actor === '' ? [] : [actor],
    order_no: (values.key2 ?? '').trim(),
    key: (values.key1 ?? '').trim(),
    // `undefined` = 未点过排序（或点了「取消排序」）⇒ `toAuditLogParams` 不会发这个参数。
    ordering,
  }
}

/**
 * 「记录日期」的默认区间：**昨天 00:00:00 ~ 今天 23:59:59**（`yyyy-MM-dd HH:mm:ss`，本地时区）。
 *
 * 出处与推断链（值一律从这里取，别猜）：证据件 HTML L83/L87 的 `value` 是
 * `2026-09-19 00:00:00` / `2026-09-20 23:59:59`。本批对标快照取自 **2026-09-20**
 * （同日快照的 `Order__orderlist` 是 `2026-06-20 ~ 2026-09-20`，即「今天 − 3 个自然月 ~ 今天」，
 * 见 `order-orderlist.ts::defaultDateRange` 的注释）⇒ 本页默认值 = 「昨天 00:00 ~ 今天 23:59」。
 * ⚠️ 这是**单样本实测 + 同族规律推断**，不是逐字读到对标服务端代码；但它是**服务端渲染值**
 * （`systemlog.js` 里那两处 `laydate.render` 没有 `value`），我们不能照抄一个会过期的固定日期。
 *
 * ⚠️ **2026-09-22：算式搬到了 `@/layui-island/dateRange`**（`yesterdayDateRange`——同一段格式化
 * 与另三页只有一份实现，本页与它们只差起点那一天）。这里是**再导出**，对外签名
 * （含「可注入固定 `now`」）逐字不变，既有单测继续钉同一份实现。
 */
export const defaultLogDateRange = yesterdayDateRange

/** xm-select 里我们用到的方法（`vendor.ts` 的公开类型只声明了 `render`/`value`，这里按需收窄）。 */
type FormSelectsLike = {
  data?: (id: string, type: string, opts: unknown) => void
  value?: (id: string, type?: string) => unknown
  config?: (id: string, config: Record<string, unknown>) => unknown
}

/** 挂载整页；返回卸载函数。 */
export async function mountSystemLogPage(root: HTMLElement, _ctx: IslandContext): Promise<() => void> {
  const layui = await bootstrapIsland(['form', 'table', 'layer', 'laydate'])
  const form = layui.form as LayuiModule
  const table = layui.table as LayuiModule
  const layer = layui.layer as LayuiModule
  const laydate = layui.laydate as LayuiModule

  document.title = '系统跟踪日志'
  root.innerHTML = await loadConfigArtifact(ARTIFACT_KEY)
  initIslandElements(layui)

  const msg = (text: string): void => {
    ;(layer.msg as (t: string, o?: unknown) => void)(text, { time: 1500 })
  }

  // —— 记录日期：填默认区间 + 交给 laydate（对标 L30-38 的 `type:'datetime'`，现在筛选真的可用了）
  const { sDate, eDate } = defaultLogDateRange()
  const sInput = root.querySelector<HTMLInputElement>('input#sDate')
  const eInput = root.querySelector<HTMLInputElement>('input#eDate')
  if (sInput) sInput.value = sDate
  if (eInput) eInput.value = eDate
  ;(laydate.render as (o: unknown) => void)({ elem: '#sDate', type: 'datetime' })
  ;(laydate.render as (o: unknown) => void)({ elem: '#eDate', type: 'datetime' })

  // —— 相关人员：数据源 = `GET /api/audit/actors/`（**不是** `/api/accounts/`，后者受
  //    `IsAccountAdmin` 约束，持 `system.logs` 但非账号管理员的人会拉到空名单，spec §6.3）。
  //    ⚠️ **R29：`xm-select-radio` 保持在证据件里不动**（对标就是单选，见文件头的 R29 说明）——
  //    摘掉它会多出「全选/反选」按钮而没有对标依据。控件是单选 ⇒ 页面只发一个 `username`。
  initFormSelects(ACTOR_SELECTOR)
  const formSelects = (layui as unknown as { formSelects?: FormSelectsLike }).formSelects
  // `searchName/keyName/keyVal` 只能走 `config()`（`render()` 不透传这三个，见 vendored
  // `formselects-v4.js:5280-5320` 的 `Select4.prototype.render` 只拷贝固定几个键）；
  // 带上 `searchUrl` 会让插件**立刻**用空 key 拉一次首屏 50 条（同文件 `:1758` 的
  // `config.searchUrl && data[id] && common.triggerSearch(...)`），所以这里不需要另发一次首屏请求。
  formSelects?.config?.(ACTOR_ID, {
    searchUrl: AUDIT_ACTORS_URL,
    searchName: 'key',
    // 显示昵称、提交账号名（`keyVal`）——spec §11.2。`keyName` 改了，`keyVal` **没改**：
    // 后端 `actor__in` 按**存储的账号名**匹配，昵称只是显示（改 `keyVal` 会让筛选恒空）。
    keyName: 'display_name',
    keyVal: 'username',
  })

  // —— 游戏筛选：换成我方游戏（对标那 13 个名字是它的租户数据）。
  //    ⚠️ **不论取数成功与否都把对标的静态 `<option>` 换掉**：失败时留一个空列表，
  //    而不是把它的租户游戏名留在页面上（选到只会筛出空集，且那是别人的数据）。
  let games: ConfigRow[] = []
  try {
    games = await fetchGames()
  } catch (e) {
    msg(e instanceof Error ? e.message : '游戏列表加载失败')
  }
  const gameSelect = root.querySelector<HTMLSelectElement>('select#gameName')
  if (gameSelect) {
    gameSelect.innerHTML =
      '<option value="">全部游戏</option>\n' +
      games.map((g) => `<option value="${esc(g.name)}">${esc(g.name)}</option>`).join('\n')
    // 默认停在「全部游戏」：不给 `selected` 属性的话浏览器会选中第 0 项，行为一致。
    gameSelect.selectedIndex = 0
  }
  ;(form.render as (type?: string) => void)('select')

  /** 读筛选栏（DOM 名原样；`#recUser` 的值只能从 xm-select 读——插件把 select 的 `name` 摘掉了）。 */
  function readFilterValues(): LogFilterValues {
    const values = (form.val as (f: string) => Record<string, string>)('form') ?? {}
    // ⚠️ 取值必须用 `valStr`（= `keyVal` = 账号名），**不能**用 `nameStr`：`keyName` 改成了
    // `display_name` 之后 `nameStr` 回的是**昵称**，拿它去匹配 `actor__in` ⇒ **昵称≠账号名的人查空**
    // （⚠️ 不要写成「恒查空」：昵称为空的人 `display_name` 回退成账号名，那种人查得到）
    // （页面不报错、只是没结果）。依据：vendored `public/vendor/formselects-v4.js` 的
    // `Select4.prototype.value`（`:1523` `valStr` 取 `val.value` / `:1533` `nameStr` 取 `val.name`）。
    const actorValue = formSelects?.value?.(ACTOR_ID, 'valStr')
    return {
      sDate: values.sDate,
      eDate: values.eDate,
      logType: values.logType,
      gameName: values.gameName,
      recUser: typeof actorValue === 'string' ? actorValue : '',
      key1: values.key1,
      key2: values.key2,
    }
  }

  /**
   * 当前排序状态（`undefined` = 没点过排序 / 点了「取消排序」）。
   * 它是页面闭包状态、不是 DOM 字段：`readFilterValues` 读不到它，所以经
   * `toLogSearchParams` 的第二个参数汇进 `currentWhere()`（唯一的参数形状出口）。
   */
  let sortOrdering: AuditLogOrdering | undefined

  /** 当前筛选对应的**线上参数**（唯一形状出口）。 */
  const currentWhere = (): Record<string, string | number> =>
    // 排序状态不是 DOM 字段（见 `toLogSearchParams` 的注释），但**只经这一条链**上线：
    // `readFilterValues → toLogSearchParams → toAuditLogParams`。别在别处再拼一次参数。
    toAuditLogParams(toLogSearchParams(readFilterValues(), sortOrdering))

  /** 重新取数。默认回到第 1 页（对标 `reloadOrderList` 里没有跳页，搜索语义就是回第一页）。 */
  function reload(page = 1): void {
    ;(table.reload as (id: string, o: unknown) => void)(TABLE_ID, {
      where: currentWhere(),
      page: { curr: page },
    })
  }

  // 搜索：`form.on('submit(seachBtn)')`（对标 L147-151）。layui 的 form 模块对
  // `*[lay-submit]` 做了文档级委托，所以这里不需要额外的点击监听。
  ;(form.on as (e: string, cb: () => void) => void)('submit(seachBtn)', () => {
    reload(1)
    return false
  })

  // 工具栏唯一的「刷新列表」（对标 L140-146）。
  ;(table.on as (e: string, cb: (obj: { event?: string }) => void) => void)(
    `toolbar(${TABLE_FILTER})`,
    (obj) => {
      if (obj.event === 'refresh') reload(1)
    },
  )

  // 排序：`autoSort:false`（见下面 `table.render`）关掉了 layui 的**本地**假排序——
  // 它只重排当前页那 30 行、不动全量日志。这里把 layui 的 `field`/`type` 映射成后端
  // `ordering` 词表后**重新取数**（回第 1 页，与搜索同语义，spec §16.2 第 1 条的裁决）。
  // ⚠️ 复刻值 `sort: true` 留在列上不动（`buildLogCols`），本行只接管事件。
  ;(table.on as (e: string, cb: (obj: LogSortEvent) => void) => void)(
    `sort(${TABLE_FILTER})`,
    (obj) => {
      // 认不出的列 / `type === null`（第三次点击「取消排序」）⇒ `undefined` ⇒ 不发 `ordering`。
      sortOrdering = toLogOrdering(obj.field, obj.type)
      reload(1)
    },
  )

  ;(table.render as (o: unknown) => void)({
    elem: `#${TABLE_ID}`,
    // ⚠️ 服务端分页必须走 layui 的 `url`：`data` 模式只会在本地分页当前这一页的数组，
    // 「共 N 条 / 总页数」全错。对标的 `url` 是 `/SystemAjax/GetSysmteLogList?…`，这里换我们自己的。
    url: AUDIT_LOGS_URL,
    method: 'get',
    // ⚠️ **关掉本地假排序**（vendored layui 的默认值是 `true`，`public/layui/layui.js`
    // 的 `autoSort:!0`）：开着的话点「操作时间」箭头只会让 layui 重排**当前页那 30 行**，
    // 既不请求服务端、也不影响翻页后的行（spec §16.1 R30 实测）。关掉后由上面的
    // `sort(...)` 处理器改成**服务端排序**——全量日志真的按操作时间排。
    autoSort: false,
    // layui 默认发 `page`/`limit`；后端分页器认的是 `page`/`page_size`（`EnvelopePagination`）。
    request: { pageName: 'page', limitName: 'page_size' },
    // 首屏就带上默认日期区间（否则会拉全表最新 30 条，与筛选栏显示的值不一致）。
    where: currentWhere(),
    toolbar: '#toolbarDemo',
    defaultToolbar: ['filter'],
    cellMinWidth: 150,
    height: 'full-80',
    page: { limit: PAGE_LIMIT, limits: PAGE_LIMITS },
    cols: [buildLogCols()],
    parseData: (res: {
      code: number
      msg: string
      data?: { items?: AuditLogRow[]; total?: number }
    }) => ({
      code: res.code,
      msg: res.msg,
      count: res.data?.total ?? 0,
      data: (res.data?.items ?? []).map(toLogRow),
    }),
    text: { none: '暂无数据' },
    done: () => {
      ;(form.render as (type?: string) => void)()
    },
  })
  ;(form.render as (type?: string) => void)()

  return () => {
    // 只清我们注入的标记；`layer` 的弹层挂在 `document.body`，由 layui 自己收。
    root.innerHTML = ''
  }
}

export default {
  mount: mountSystemLogPage,
} satisfies IslandPage
