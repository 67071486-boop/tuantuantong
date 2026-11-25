/**
 * 【淘宝发货记录】页的 **layui 复刻页**（对标物 `/Order/tbsendrecordlist`，**证据件渲染**）。
 *
 * 形状：**单表列表页**（4 个筛选控件 + 工具栏 3 颗按钮 + 10 列表格 + 一个 `display:none` 的
 * 「上传淘宝订单」弹层 `#hidDiv`）。
 * 证据（全部离线）：
 * - 标记与页内 `<style>` → `public/replica/order-tbsendrecordlist.html`
 * （源 `.tools/recon/suqing1/html/Order__tbsendrecordlist.html` L24-216）
 * - 列定义 → `.tools/recon/suqing1/assets/JS2019/Order/tbsendrecordmang.js` **L48-88**
 * - 工具栏模板 `#toolbarDemo` → 同上 HTML **L168-175**
 * - 分页 → 同 JS **L89-91**：`page: true` / `limit: 50` / `limits: [50]`
 * - 工具栏事件 `refresh` / `send` / `import` → 同 JS **L97-110** 的 `switch`；
 * 「重试发货」的取行与文案 → 同 JS **L113-118**
 * - 页内「搜索 / 回车」两条重取路径 → 同 JS **L39-44**（回车）与 **L215-241**（`reloadOrderList`）
 *
 * ## ⚠️ JS 名 ≠ 页面名（本轮特别提醒第 3 条）
 *
 * HTML **L217** 加载的是 `/JS2019/Order/tbsendrecordmang.js?v=…`（`mang`，不是 `list`）。
 * 同名的 `tbsendrecordlist.js` 实测 404 —— 列定义/工具栏事件只能从 `tbsendrecordmang.js` 取。
 *
 * ## 列宽的运行期表达式（`device` 分支）
 *
 * `tbsendrecordmang.js` 的 `isPc = true`（L14，仅 android/ios 置 false）——**取桌面分支的百分比**：
 * 记录ID `'3%'` / 系统订单号 `'10%'` / 淘宝订单号 `'10%'` / 发货原参数 `'25%'` / 发货店铺 `'5%'` /
 * 发货结果 `'3%'` / 发货时间 `'7%'` / 发货人 `'3%'` / 备注说明 `'7%'`；勾选列是定值 `30`。
 * **百分比原样透传**（不换算像素）。
 *
 * ## `#total` 是**空的**（与「上家订单改价记录」不同）
 *
 * 工具栏模板里有 `<div style="color:#ff6a00" id="total"></div>`，对标 `done` 只记当前页，
 * **不写** `#total`。本页的 `done` 同样只把 layui 交回的 `curr` 记进 `currentPage`。
 * `SPEC.done` 仍是 `undefined`：`SPEC` 是声明式证据，活渲染在 `mountSendRecordPage`。
 *
 * ## ⭐ 接真后端（2026-10-03，spec §3.7/§3.8）
 *
 * ⛔ **本页不再走 `artifactPage`**：那个共享渲染器的契约是「表格恒空、不发任何写请求」，
 * 而且它在 `root` 的**捕获阶段**把所有 `[lay-event]` 点击收口成「尚未开放」并
 * `stopPropagation`（`artifactPage.ts:282-298`）⇒ 工具栏三颗按钮的事件**永远到不了 layui 的
 * 委派**（在它下面接 `table.on('toolbar(…)')` 是**死路**）。代价与先例同「已删除的订单」页
 * （`order-orderrecycle.ts` 文件头第一段，D-0025 / D-0144）：本页脱离那个统一护栏 ⇒
 * **本页自己的 DOM 断言成为唯一防线**（见 `__tests__/order-tbsendrecordlist.spec.ts`），
 * 且 `parity` 绿**不构成**本页接对了的证据（空表时表体不可见）。
 *
 * | # | 对标怎样 | 我们怎样 | 依据 |
 * |---|---|---|---|
 * | 1 | 走 `artifactPage`（表格恒空 + 点任何按钮「尚未开放」） | **整页自建** island 页（自己取证据件 / 塞标记 / `table.render` / 三个事件） | spec §3.8；理由见上一段（共享渲染器的捕获期收口） |
 * | 2 | 取数 URL 是 `/OrderAjax/GetTbSendRecordList?sDate=…&eDate=…&result=-1&key=` | 走 **layui 的 `url` 模式**打 `GET /api/business/tb-send-logs/`，筛选用 `where` 交（`page`/`page_size` 由 `request` 映射） | spec §3.7；先例 `order-orderrecycle.ts` |
 * | 3 | 两个日期框的 `value` 是**它对标服务端按取数当天**渲染的死值（证据件 `2026-09-18` / `2026-09-20`） | **运行期算**（今天 −2 天 ~ 今天，`defaultSendDateRange`） | 那两个值**真的发给后端**当筛选 ⇒ 照抄会让「快照日之后发的货默认看不见」（与绩效管理页 / 回收站页同一类真 bug，见 `FRONTEND-REPLICA-STANDARD.md` §7）。⚠️ 偏移量「−2 天」是**推断**（只有一张快照，见 `defaultSendDateRange` 的注释） |
 * | 4 | 「发货结果」列的 `templet` 按 `res.SendResult != 1` 渲染彩色「成功 / 失败」（JS L70-77） | **照抄**（`buildRenderCols` 给同一列挂同款 `templet`）；区别只在：对标那份写在它自己的 `cols` 字面量里，我们挂在**列定义之外**——`buildCols()` 一个字没动（它是探针与单测钉住的证据） | JS L70-77 |
 * | 5 | 「搜索」的 `key` 原样发（不 trim） | `trim()` 后为空则**不发**该参数 | 与 `order-orderrecycle.ts::toRecycleSearch` 同口径（纯空白当没填） |
 * | 6 | 「重试发货」把**逗号串的 `TbOrderNo` + `TbStoreName`** 发给 `/OrderAjax/SendAgin`（JS L118-130） | 把勾选行的**记录 id 列表**发给 `POST /api/business/tb-send-logs/retry/`（spec §3.7 的第一种形态） | spec §3.7 |
 * | 7 | 「重试发货」成功后 `layer.alert("成功X条，失败Y条")`（JS L141-143） | 同一句文案走 `layer.msg`（本页统一反馈出口），计数键取 `success`/`fail`；**拿不到计数就不编数字**、退化成一句中性反馈 | spec §3.8「`layer.msg` 反馈」；`retryFeedback` |
 * | 8 | 「导入文件发货」真开 `#hidDiv` 弹层 + 上传 txt（JS L155-211） | **本轮不做**（spec §6）：只提示「尚未开放」，**不置灰、不隐藏**（D-0025 §5） | spec §3.8 / §6 |
 * | 9 | 「刷新数据」走 `reloadOrderList()`，它 `table.reload({…, page:false})`（JS L230-240） | 按**当前筛选重新拉第一页**（`reload(1)`） | spec §3.8 明写「按当前筛选重新拉第一页」 |
 *
 * ## 接线形状（与 `order-orderrecycle.ts` 同形，⛔ 别改成别的）
 *
 * `table.render({elem,id,url,method:'get',request:{pageName:'page',limitName:'page_size'},
 * where,parseData,toolbar,defaultToolbar,page,cols,done})`；取数与翻页由 layui 自己发；
 * `parseData` 吃**统一信封** `{code,msg,data:{items,total,…}}` 并把 `data.total` 写进 `count`。
 * 写请求（重试发货）走 `api/tbSendLogs.ts::retrySendLogs` → 统一 `request()`。
 *
 * ## PII：清空了租户的淘宝店铺列表
 *
 * `#hidDiv` 里的 `<select id="source" name="source">`（HTML L186-203）原本有 **14 个该项租户自己的
 * 淘宝店铺名**（都是租户配置数据，不是静态枚举；**此处不复述任何一条**）——
 * **已全部清空**，只留首个 `<option value=""></option>`（草稿 `dropped` 里记的就是这一处）。
 * 该弹层 `display:none`，探针看不到，但红线是红线。其余内容：发货结果 3 项静态枚举、
 * 服务端渲染日期、按钮文案，无个人数据。
 * 提交前自查：`grep -nE '1[3-9][0-9]{9}|[0-9]{17}[0-9Xx]'` 0 命中。
 */
import {
  retrySendLogs,
  TB_SEND_LOGS_PATH,
  type TbSendLogRow,
  type TbSendRetryResult,
} from '@/api/tbSendLogs'
import { fetchArtifactHtml, type ArtifactCol, type ArtifactSpec } from '@/layui/artifactPage'
import {
  bootstrapIsland,
  initIslandElements,
  islandMsg,
  type IslandContext,
  type IslandPage,
} from '@/layui/island'
import type { LayuiModule } from '@/layui/loader'
import { escapeHtml } from '@/layui-island/escape'
import { withRawCells } from '@/layui-island/rawCell'

/** 证据件（对标页 `<style>` 段 + body 标记，零转义，可直接与对标原文逐行对拍）。 */
const ARTIFACT_URL = 'replica/order-tbsendrecordlist.html'

/** 对标页服务端渲染的 `<title>`（`Order__tbsendrecordlist.html` L5）。 */
export const SEND_TITLE = '淘宝发货记录'

/** 对标 `table.render` 的 `elem`（`tbsendrecordmang.js` L43；HTML L177）。 */
export const TABLE_ELEM = '#tableList'

/**
 * `table.reload` / `table.on` / `table.checkStatus` / `table.getOptions` 要的是**实例 id**
 * （不带 `#`），传 `#id` 是静默 no-op（E-0061 ③）——所以选择器与 id 分成两个常量，
 * ⛔ 不要用 `TABLE_ELEM.slice(1)` 现算。
 */
export const TABLE_ID = 'tableList'

/** 对标 `toolbar`（同 JS L46；模板在 HTML L168-175）。 */
export const TOOLBAR_ELEM = '#toolbarDemo'

/** 表头：逐条照抄 `tbsendrecordmang.js` L48-88（`align:'center'` 全列都有，故逐条写）。 */
export function buildCols(): ArtifactCol[] {
  return [
    { type: 'checkbox', fixed: 'left', width: 30 },
    { field: 'TaobaoOrderSendLogID', title: '记录ID', width: '3%', align: 'center' },
    { field: 'Order_Id', title: '系统订单号', width: '10%', align: 'center' },
    { field: 'TbOrderNo', title: '淘宝订单号', width: '10%', align: 'center' },
    { field: 'SendResult_s', title: '发货原参数', width: '25%', align: 'center' },
    { field: 'TbStoreName', title: '发货店铺', width: '5%', align: 'center' },
    // `templet`（L70-77）按 `SendResult != 1` 渲染「成功 / 失败」彩色 `<span>`；
    // ⛔ 它**不在**这里：本函数是「列定义 = 证据」的那一份（探针 / 单测钉住），
    // 活渲染的那一份由 `buildRenderCols()` 在它之上挂 `templet`（差异 #4）。
    { field: 'SendResult', title: '发货结果', width: '3%', align: 'center' },
    { field: 'SendDateEx', title: '发货时间', width: '7%', align: 'center' },
    { field: 'UserID', title: '发货人', width: '3%', align: 'center' },
    { field: 'Remark', title: '备注说明', width: '7%', align: 'center' },
  ]
}

/** 导出给单测对拍表头序列（与 `tbsendrecordmang.js` 的 `cols` 一致）。 */
export const COLS_FOR_TEST: ArtifactCol[] = buildCols()

/** 对标 L90-91：`limit: 50` / `limits: [50]`（只有一项，「条/页」下拉只有 50）。 */
export const PAGE_LIMIT = 50
export const PAGE_LIMITS = [50] as const

/** 渲染规格（导出给单测锁定 `page`/`defaultToolbar`/`toolbar`/`height` 这些**静默出错**的键）。 */
export const SPEC: ArtifactSpec = {
  url: ARTIFACT_URL,
  tables: [
    {
      elem: TABLE_ELEM,
      toolbar: TOOLBAR_ELEM,
      // 同对象里写了两次，后写的 `['filter']` 生效（L45 → L47）。
      defaultToolbar: ['filter'],
      page: { limit: PAGE_LIMIT, limits: [...PAGE_LIMITS] },
      cols: buildCols(),
    },
  ],
}

/* ────────────────────────── 接后端：URL / 筛选 / 行映射 ────────────────────────── */

/**
 * 站点基址（子路径部署时不是 `/`；与 `order-orderrecycle.ts` 同一写法）。
 *
 * ⛔ 不写死 `/`：本页列表取数走 layui 的 `url` 模式，URL 由**这里**拼出来交给 layui，
 * 写死会让子路径部署的页面取到 404（`order-billsummary.ts` 的基址守卫是同一族）。
 */
const API_BASE = import.meta.env.BASE_URL || '/'

/** 交给 layui `table.render` 的**完整 URL**（`url` 模式下取数由 layui 自己发）。
 * 路径的**唯一出处**在 `api/tbSendLogs.ts`（⛔ 不在本文件再写一份字面量）。 */
export const TB_SEND_LOGS_URL = `${API_BASE.replace(/\/$/, '')}${TB_SEND_LOGS_PATH}`

/** 一笔都没勾时的提示（**逐字**取对标 `tbsendrecordmang.js` L115 的 `layer.msg`）。 */
export const SEND_ROW_SELECT_TEXT = '请勾选要操作的数据行'

/** 「导入文件发货」本轮不做，统一这句（D-0025 §5：照对标渲染 + 点了提示尚未开放、不置灰）。 */
export const SEND_IMPORT_NOT_OPEN_TEXT = '「导入文件发货」尚未开放'

/**
 * 重试成功、但响应里**拿不到** `success`/`fail` 计数时的中性反馈。
 *
 * ⛔ 不编「成功 N 条」这样的数字（`AGENTS.md` §5 的「编文案」；先例 `order-orderrecycle.ts`
 * 差异 #11 的同一处置）——能说的只有「这一发请求成功了」。
 */
export const SEND_RETRY_DONE_TEXT = '操作完成'

/** 筛选栏读出来的**原始**值（键名 = 证据件里的 `name`，值一律是字符串）。 */
export type RawSendFilterValues = {
  sDate?: string
  eDate?: string
  result?: string
  key?: string
}

/** 后端列筛选参数（spec §3.7：`sDate` / `eDate` / `result` / `key`）。 */
export type SendLogSearchParams = {
  sDate?: string
  eDate?: string
  result?: string
  key?: string
}

/**
 * 筛选表单 → 后端参数（spec §3.7 的 4 个）。
 *
 * ⚠️ **「空」判据是 `!== ''`，不是真值**（E-0021）：`result` 的 `'0'`（失败）与 `'-1'`（全部）
 * 都是**合法且有意义**的取值，写成 `if (result)` 不会误伤（字符串非空即真），但写成
 * `Number(...)` 真值判断就会把 `'0'` **静默丢掉**一个筛选条件 ⇒ 这里一律按字符串原样转发。
 *
 * ⚠️ `key` 做 `trim()`（差异 #5）：纯空白的搜索词不该变成一个「必然 0 行」的筛选条件。
 */
export function toSendLogSearch(values: RawSendFilterValues): SendLogSearchParams {
  const params: SendLogSearchParams = {}
  const sDate = values.sDate?.trim() ?? ''
  if (sDate !== '') params.sDate = sDate
  const eDate = values.eDate?.trim() ?? ''
  if (eDate !== '') params.eDate = eDate
  // `result` 的选项值就是后端要的枚举（`-1` / `1` / `0`），**原样透传**。
  const result = values.result?.trim() ?? ''
  if (result !== '') params.result = result
  const key = values.key?.trim() ?? ''
  if (key !== '') params.key = key
  return params
}

/** 发货时间默认区间（两个日期框的 `value`，格式 = 证据件里的 `YYYY-MM-DD`）。 */
export type SendDateRange = { sDate: string; eDate: string }

const pad2 = (value: number): string => String(value).padStart(2, '0')
const ymd = (date: Date): string =>
  `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`

/**
 * 默认发货时间区间：**今天 −2 个自然日 ~ 今天**（本地时区，`YYYY-MM-DD`）。
 *
 * ⚠️ **偏移量是「推断」级证据，不是事实**（`AGENTS.md` §5-C 的三级标注）：可得的证据件只有
 * **一张快照**——`Order__tbsendrecordlist.html` 里 `#sDate` = `2026-09-18`、`#eDate` = `2026-09-20`
 * （该快照取于 2026-09-20）。从「2026-09-20 → 2026-09-18」这**一对值**反推出「今天减 2 天」，
 * 这是个**推断**：证据件没有第二张不同日期的渲染结果，无法排除「上周同一天」「减 48 小时」
 * 等同值写法。⛔ 不要把它当既定事实引用。
 *
 * ⛔ **也不 import `@/layui-island/dateRange`**：那是**别的几页**的偏移（3 个月 / 昨天），
 * 各页偏移不许互相照抄（那个文件的注释自己写着这一条）。
 *
 * ⚠️ 与回收站页不同，这里是**按天**回退 ⇒ 不存在「目标月没有那一天」的溢出问题
 * （`new Date(y, m, d - 2)` 由 `Date` 自己归一化，`1 月 1 日 − 2 天` = 上一年 12 月 30 日）。
 */
export function defaultSendDateRange(now: Date = new Date()): SendDateRange {
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 2)
  return { sDate: ymd(start), eDate: ymd(now) }
}

/**
 * 表格行（键 = 列定义里的 `field`；值 = 单元格内容），外加**不发往列**的定位键。
 *
 * ⚠️ `send_log_id` **不是**列定义里的 `field`（与 `order-orderrecycle.ts` 的 `order_no` 同一手法）：
 * 「重试发货」要按**记录 id** 发请求，而列里那一格是给人看的 `TaobaoOrderSendLogID` 文本 ⇒
 * 定位键单独留一格，⛔ 不去解析单元格文本。
 */
export type SendCellRow = Record<string, string | number> & { send_log_id: number }

/**
 * 发货时间：载荷是 DRF 的 ISO 串（`2026-10-03T12:55:00+08:00`），
 * 按本仓既有约定格式化成 `YYYY-MM-DD HH:mm`（先例 `order-orderrecycle.ts` 差异 #9）。
 */
function fmtTime(value: string | null): string {
  return typeof value === 'string' && value !== '' ? value.replace('T', ' ').slice(0, 16) : ''
}

/**
 * 把一条接口行映射成表格行（列定义读的就是这些 `field`）。
 *
 * ⚠️ **本函数是转义出口**：所有来自请求数据的字面都过 `escapeHtml` ⇒ 单号 / 备注 / 发货原参数
 * 都是**存储型 XSS** 的入口；对标自己没转义，但那不是照抄缺陷的理由。
 *
 * ⚠️ **改准**：本函数原先写「layui 的默认单元格渲染**不转义**」——那句是**错的**
 * （读码 `public/layui/layui.js` 偏移 279727 的 `escape:!0` 与偏移 272879 的
 * `("escape"in a?a:t).escape&&…`：列上没有 `escape` 键时取表配置 ⇒ 默认**转义**）。
 * 单元格能不能原样显示由**列上的 `templet`** 决定（`buildRenderCols()` 挂原样 templet，
 * 读码见 `@/layui-island/rawCell` 的模块 docstring）——否则 `A&B` 会被**二次转义**成 `A&amp;B`。
 *
 * ⚠️ **先兜空再判空**：`order_no` / `remark` 这类字段在每个载荷里都该有值，但缺键时
 * `escapeHtml(undefined)` 会抛 `TypeError`（`escape.ts`），而这里在 `parseData` 里跑 ⇒
 * layui 的 success 回调整条抛、**表格一行都不渲染**（`order-orderrecycle.ts` 抓过的致命缺陷）。
 */
export function buildSendRow(row: TbSendLogRow): SendCellRow {
  return {
    send_log_id: row.id,
    TaobaoOrderSendLogID: row.id,
    Order_Id: escapeHtml(row.order_no ?? ''),
    TbOrderNo: escapeHtml(row.tb_order_no ?? ''),
    SendResult_s: escapeHtml(row.send_params ?? ''),
    TbStoreName: escapeHtml(row.store_name ?? ''),
    // 单元格值给**原始数值**：`buildRenderCols` 那条 `templet` 就是按 `!== 1` 判的（JS L70-77）。
    SendResult: row.result,
    SendDateEx: escapeHtml(fmtTime(row.sent_at)),
    UserID: escapeHtml(row.operator ?? ''),
    Remark: escapeHtml(row.remark ?? ''),
  }
}

/**
 * 「发货结果」列的 `templet`——**逐字**照 `tbsendrecordmang.js` L70-77：
 * `res.SendResult != 1` ⇒ 红字「失败」，否则绿字「成功」。
 *
 * ⚠️ 这里把对标的 `!=` 写成 `!==` **并不过度收紧**：后端 `result` 是
 * `SmallIntegerField` ⇒ DRF 回的是**数字** `1`/`0`（`api/tbSendLogs.ts::TbSendLogRow` 的契约）。
 * 页测试同时钉住 `1 → 成功`、`0 → 失败` 两档。
 */
const resultTemplet = (d: Record<string, unknown>): string => {
  let div = '<span style="color:#5BB75B">成功</span>'
  if (d.SendResult !== 1) {
    div = '<span style="color:#ff0000">失败</span>'
  }
  return div
}

/**
 * 活渲染用的列定义 = `buildCols()` **原样** + 每个数据列挂 `templet`（差异 #4）。
 *
 * ⛔ 不改 `buildCols()`：它的单测 / 探针对拍钉的是「列定义 = 证据」（`order-orderrecycle.ts`
 * 的 `buildRecycleCols` 同款处置——那里也是「函数返回扁平证据、调用点再包一层」）。
 *
 * ⚠️ **两件事**（2026-10-03 本线改准）：
 * 1. 其余数据列走 `withRawCells()` 的**原样** templet —— 本页单元格值已由 `buildSendRow()`
 * 转义拼好，而 layui 2.13.9 的表原型配置是 `escape:!0`、**列上没有 `templet` 就被当纯文本
 * 转义** ⇒ 不挂它 `A&B` 会变成 `A&amp;B`（**二次转义**，本页原先的现象；读码见 `rawCell.ts`）。
 * 2. 「发货结果」列**保留自己的** `resultTemplet`（`withRawCells` 对已有 `templet` 的列不覆盖），
 * 这里再显式写一次是**判据的落点**：这一列渲染的是成功/失败彩色 `<span>`，不是原样值。
 */
export function buildRenderCols(): Array<Record<string, unknown>> {
  return withRawCells(buildCols()).map((col): Record<string, unknown> => {
    if (col.field === 'SendResult') return { ...col, templet: resultTemplet }
    return col
  })
}

/**
 * 重试发货的反馈文案。
 *
 * 计数键取对标那句 `layer.alert` 的载荷形状（`data.success` / `data.fail`，JS L141-143）：
 * **两个都有**才拼那句话；**缺一个就不编数字**、退化成 `SEND_RETRY_DONE_TEXT`（差异 #7）。
 * ⛔ 不要给缺失的计数补 `0`：那会把「后端没回计数」显示成「成功 0 条，失败 0 条」——一句假话。
 */
export function retryFeedback(result: TbSendRetryResult | null | undefined): string {
  if (result && typeof result === 'object') {
    const { success, fail } = result
    if (typeof success === 'number' && typeof fail === 'number') {
      return `成功${success}条，失败${fail}条`
    }
  }
  return SEND_RETRY_DONE_TEXT
}

/* ────────────────────────── 挂载 ────────────────────────── */

/** 写回两个日期框的默认值（`#sDate` / `#eDate`）。 */
function applyDefaultDateRange(root: HTMLElement, range: SendDateRange): void {
  for (const [id, value] of [
    ['sDate', range.sDate],
    ['eDate', range.eDate],
  ] as const) {
    const input = root.querySelector<HTMLInputElement>(`#${id}`)
    if (input) input.value = value
  }
}

export async function mountSendRecordPage(
  root: HTMLElement,
  _ctx: IslandContext,
): Promise<() => void> {
  // ⚠️ 三条陷阱之③（前半）：**先**挂样式（`bootstrapIsland` 会运行期插 `layui.css` 与覆盖样式），
  // **再**塞页面标记——顺序反了会 FOUC。
  const layui = await bootstrapIsland(['form', 'table', 'layer', 'laydate'])
  const form = layui.form as LayuiModule
  const table = layui.table as LayuiModule
  const layer = layui.layer as LayuiModule
  const laydate = layui.laydate as LayuiModule | undefined

  // 对标 `<title>` 是服务端渲染的（`Order__tbsendrecordlist.html` L5）。
  document.title = SEND_TITLE

  root.innerHTML = await fetchArtifactHtml(ARTIFACT_URL)
  // ⚠️ 三条陷阱之③（后半）：`initIslandElements(layui)` **必须**留在塞标记**之后**
  //（页签 / 折叠面板要等标记进来之后再绑定）。
  initIslandElements(layui)

  const range = defaultSendDateRange()
  applyDefaultDateRange(root, range)
  // 日期控件（对标 `tbsendrecordmang.js` L21-29：两次 `laydate.render`，`type: 'date'`——
  // ⚠️ 本页是**日期**不是「回收站页」那种 `datetime`，⛔ 不要照抄那一页的 `type`）。
  const renderDate = (elem: string): void => {
    const render = laydate?.render as ((options: unknown) => void) | undefined
    if (render) render({ elem, type: 'date' })
  }
  renderDate('#sDate')
  renderDate('#eDate')

  const msg = (text: string): void => islandMsg(layer, text)
  const errText = (error: unknown): string =>
    error instanceof Error ? error.message : '操作失败'

  /**
 * 当前页码。**写入点**是下面 `table.render` 的 `done(_res, curr)`（= 对标那句 `page = curr`，
 * JS L92-94）；**读取点**是重试成功后的 `reload(currentPage)`（就地刷新，不跳回第 1 页）。
 */
  let currentPage = 1

  /** 用户当前选的「条/页」（laypage 就地写在 `table.render` 那份配置的 `page` 子对象上）。 */
  const currentLimit = (): number => {
    const options = (
      table.getOptions as (id: string) => { page?: { limit?: number } } | undefined
    )(TABLE_ID)
    return options?.page?.limit ?? PAGE_LIMIT
  }

  /** 读筛选栏（键名 = 证据件里的 `name`；正好就是 spec §3.7 的四个参数名）。 */
  const readFilters = (): RawSendFilterValues => {
    const value = (name: string): string =>
      root.querySelector<HTMLInputElement | HTMLSelectElement>(`[name="${name}"]`)?.value ?? ''
    return {
      sDate: value('sDate'),
      eDate: value('eDate'),
      result: value('result'),
      key: value('key'),
    }
  }

  /** 勾选中的行（工具栏「重试发货」取它）。 */
  const checkedRows = (): SendCellRow[] =>
    ((table.checkStatus as (id: string) => { data?: SendCellRow[] })(TABLE_ID).data ??
      []) as SendCellRow[]

  /**
 * 把当前筛选交给 layui 重新取数（`url` 模式：取数与分页都由 layui 自己的通道发）。
 *
 * ⚠️ **`where` 里绝不能放 `page`/`page_size`**：它会被 `table.reload` 记进实例配置，而
 * layui 自己维护的 `page.curr` 会被它盖掉 ⇒ 翻页永远发第 1 页（同 `order-orderrecycle.ts`）。
 * 页码 / 页长的唯一出口是 `table.render` 的 `request:{pageName,limitName}` + 这里的
 * `page:{curr, limit}`。
 *
 * ⚠️ **每页条数取 layui 维护的实时值**（`currentLimit()`），不要退回常量 50：用户改过
 * 「条/页」后任何一次重新取数都会把选择顶掉（E-0037 家族）。
 */
  const reload = (page = 1): void => {
    currentPage = page
    ;(table.reload as (id: string, options: unknown) => void)(TABLE_ID, {
      where: toSendLogSearch(readFilters()) as Record<string, unknown>,
      page: { curr: page, limit: currentLimit() },
    })
  }

  /**
 * 「重试发货」：取勾选行的**记录 id** → `POST …/retry/` → 反馈 + 就地刷新。
 *
 * 一笔都没勾的文案**逐字**取对标（JS L113-118），且**零请求**（连 `request` 都不碰）。
 * 失败一律原样显示错误文本（`api/http.ts` 已把 HTTP / 业务码映射成 `msg`）。
 */
  async function doRetry(): Promise<void> {
    const rows = checkedRows()
    if (rows.length === 0) {
      msg(SEND_ROW_SELECT_TEXT)
      return
    }
    let result: TbSendRetryResult | null = null
    try {
      result = await retrySendLogs(rows.map((row) => row.send_log_id))
    } catch (error) {
      msg(errText(error))
      return
    }
    msg(retryFeedback(result))
    reload(currentPage)
  }

  // 主表：**先 `table.render` 一次**（`reload` 按实例 id 查表，不先 render 会静默 no-op）。
  ;(table.render as (options: unknown) => void)({
    elem: TABLE_ELEM,
    id: TABLE_ID,
    // `url` 模式：取数请求由 layui 自己发（分页条因此按**服务端总数**画）。
    // `request` 把 layui 的 `page`/`limit` 映射成我们后端的 `page`/`page_size`。
    url: TB_SEND_LOGS_URL,
    method: 'get',
    request: { pageName: 'page', limitName: 'page_size' },
    // 首屏就带默认筛选（两个日期框在 mount 时已写进 `#sDate`/`#eDate`）——⛔ 不像订单列表那样
    // 在 `done` 里补一次 `reload(1)`：那会先用**无筛选**的结果画一帧，而对标首屏的请求就带参数。
    where: toSendLogSearch(readFilters()) as Record<string, unknown>,
    // 统一信封 `{code,msg,data:{items,total,…}}` → layui 认的 `{code,msg,count,data}`。
    parseData: (r: {
      code: number
      msg: string
      data?: { items?: TbSendLogRow[]; total?: number }
    }) => ({
      code: r.code,
      msg: r.msg,
      count: r.data?.total ?? 0,
      // 接口行 → 表格行（列定义读的是对标的 `field` 名；不映射的话所有单元格恒空——
      // `url` 模式把 items 原样交给表格，本页的列没有 `templet` 去自己取后端字段）。
      data: (r.data?.items ?? []).map((row) => buildSendRow(row)),
    }),
    toolbar: TOOLBAR_ELEM,
    // 对标在同一个 `render` 对象里写了两次 `defaultToolbar`（L45 `[]` 在前、**L47 `['filter']` 生效**）。
    defaultToolbar: ['filter'],
    page: { limit: PAGE_LIMIT, limits: [...PAGE_LIMITS] },
    cols: [buildRenderCols()],
    text: { none: '暂无数据' },
    // `done` 是 layui **每次取数后**的出口（首帧 / 翻页 / 改「条/页」都算）。⚠️ 它**只**做对标
    // `done` 做的那一件事（`page = curr`，JS L92-94）——⛔ **不写 `#total`**（对标那个 div 恒空，
    // 见文件头「`#total` 是空的」）。
    done: (_res: unknown, curr?: number) => {
      if (typeof curr === 'number') currentPage = curr
      ;(form.render as () => void)()
    },
  })
  ;(form.render as () => void)()

  /* ── 筛选栏：搜索按钮（对标 `form.on('submit(seachBtn)')`，JS L213-217） ── */
  ;(form.on as (event: string, callback: (data: unknown) => unknown) => void)(
    'submit(seachBtn)',
    () => {
      reload(1)
      return false
    },
  )

  /* ── 工具栏三颗按钮（对标 `table.on('toolbar(tableList)')` 的 switch，JS L97-110） ── */
  ;(table.on as (event: string, callback: (obj: unknown) => void) => void)(
    `toolbar(${TABLE_ID})`,
    (obj: unknown) => {
      const event = (obj as { event?: string }).event
      if (event === 'refresh') {
        // spec §3.8：按**当前筛选**重新拉**第一页**。
        reload(1)
        return
      }
      if (event === 'send') {
        void doRetry()
        return
      }
      if (event === 'import') {
        // spec §3.8 / §6：本轮不做；**不置灰、不隐藏**（D-0025 §5）⇒ 照渲染、点了提示、零请求。
        msg(SEND_IMPORT_NOT_OPEN_TEXT)
      }
    },
  )

  // 回车 = 搜索（对标 `$(document).keydown` → `page = 1; reloadOrderList()`，JS L39-44）。
  const onKeydown = (event: KeyboardEvent): void => {
    if (event.key !== 'Enter') return
    if ((event.target as HTMLElement | null)?.tagName !== 'INPUT') return
    event.preventDefault()
    reload(1)
  }
  root.addEventListener('keydown', onKeydown)

  return () => {
    root.removeEventListener('keydown', onKeydown)
    root.innerHTML = ''
  }
}

export default {
  mount: mountSendRecordPage,
} satisfies IslandPage
