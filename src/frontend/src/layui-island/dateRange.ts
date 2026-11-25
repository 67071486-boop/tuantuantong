/**
 * 「筛选栏两个日期框的默认值」的**唯一出处**（`yyyy-MM-dd HH:mm:ss`，本地时区）。
 *
 * ## 为什么抽成独立模块
 *
 * 这段算式原先在 **3 个页面**里各抄了一份（`pages/order-orderlist.ts`、
 * `pages/emp-billsummary.ts`、`pages/system-systemlog.ts` 的 `defaultLogDateRange`）：
 *
 * | 页面 | 区间 | 出处（对标服务端渲染进 HTML 的 `value`） |
 * |---|---|---|
 * | 订单列表 | 今天 − 3 个自然月 `00:00:00` ~ 今天 `23:59:59` | `Order__orderlist.html` L211/L215 |
 * | 结算记录（`/Emp/billsummary`） | 同上 | `Emp__billsummary.html` L97/L101 |
 * | 绩效管理（`/Emp/emplist`） | 同上（**本轮新接**） | `Emp__emplist.html` L249/L253 |
 * | 我的收入（`/Emp/myemp`） | **今天 − 6 个自然月** `00:00:00` ~ 今天 `23:59:59`（⭐ D-0365 评审 F-1 起接真筛选时按本表的 6 走） | `Emp__myemp.html` L119/L123（`2026-03-20` / `2026-09-20`，快照日 2026-09-20 ⇒ 6 个自然月） |
 * | 系统操作日志 | **昨天** `00:00:00` ~ 今天 `23:59:59` | `System__systemlog.html` L83/L87 |
 * | 交易订单操作记录 | 今天 − **1** 个自然月 ~ 今天（**日期形状**，无时分秒） | `order-tradingorderactionloglist.html` L120/L124（`2026-08-20` / `2026-09-20`，快照日 2026-09-20 ⇒ 08-20→09-20 是 **31 天**，8 月有 31 天 ⇒ 自然月口径） |
 *
 * 这几页的区间**逐字相同或只差起点与偏移**，**格式化那一半**（补零）也一模一样——只有
 * 时间形状的几页用时间部分，交易订单操作记录页是**日期形状**（见下面那条 ⚠️）。所以这里把
 * 「格式化 + 取区间」做成函数，各页只传自己的偏移，⛔ 不再留第 6 份复制（`AGENTS.md` §5「不重复同一事实」）。
 * ⚠️ 交易订单操作记录页那两个框的**形状**也与上表不同：它是 **laydate `type:'date'`**（值就是
 * `yyyy-MM-dd`，证据件的 `value` 也没有时分秒）⇒ 用本模块的 `dayRangeMonthsAgo` 那一支。
 *
 * ⚠️ **各页的偏移是每页一个值，别互相照抄**：同一批对标快照里 `Emp__myemp` 是 6 个月、
 * `Emp__dayoperation` 是 1 个月（`emp-billsummary.ts` 的注释记着这条）。本模块只提供**机制**，
 * 不替任何一页决定偏移。
 *
 * ⚠️ **为什么必须运行期算、不能照抄证据件里那个静态 `value`**：证据件里那两个值是**对标服务端
 * 按取数当天**渲染出来的（2026-09-20 的快照 ⇒ `2026-06-20`/`2026-09-20`）。它们是**真的发给
 * 后端的筛选**，留着静态值会让默认视图永远停在过去（2026-09-20 之后录的单默认看不见）。
 */

/** 两位补零。 */
function pad2(value: number): string {
  return String(value).padStart(2, '0')
}

/**
 * 把一个 `Date` 折成对标的 `yyyy-MM-dd`：
 * - `end === false` ⇒ `00:00:00`（区间**起点**）
 * - `end === true`  ⇒ `23:59:59`（区间**终点**）
 *
 * 值一律取**本地时区**的字段（`getFullYear`/`getMonth`/`getDate`），
 * ⛔ 不用 `toISOString()`——那是 UTC，东八区下会把日期整体推前一天。
 */
export function formatDateRange(date: Date, end: boolean): string {
  return (
    `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ` +
    (end ? '23:59:59' : '00:00:00')
  )
}

/**
 * 默认日期区间的形状：`sDate` = 起点（`00:00:00`）、`eDate` = 终点（`23:59:59`）。
 * 两个框的 `id` 就叫这两个名字（`#sDate` / `#eDate`），所以键名照它们取。
 */
export type DateRange = { sDate: string; eDate: string }

/**
 * 「今天往回推 `monthsAgo` 个自然月的那天 `00:00:00`」~「今天 `23:59:59`」。
 *
 * ⚠️ **是「减自然月」不是「减 90 天」**：跨月天数不同，`3 月 31 日 − 3 个月` 落到
 * `12 月 31 日`（`Date` 的月份溢出归一化），而「减 90 天」会落到 12 月 31 日以外的一天。
 * 这条判据由既有单测拿着字面值钉住（`order-orderlist.spec.ts` / `emp-billsummary.spec.ts`）。
 *
 * ⚠️ 一律用 `new Date(y, m, d)` 的**本地**构造器（三个数字参数不是字符串，不会被当 UTC）。
 *
 * ⭐ **偏移可参（D-0365 评审 F-1）**：`monthsAgo` 默认 3（订单列表 / 结算记录 /
 * 绩效管理三页的证据件都是 3 个月），**「我的收入」（`/Emp/myemp`）取 6**——证据件
 * `Emp__myemp.html` L119/L123 的渲染值是 `2026-03-20 00:00:00` / `2026-09-20 23:59:59`
 * （快照日 2026-09-20 ⇒ 恰 6 个自然月）。本模块 docstring 的表格早已警告过
 * 「各页偏移别互相照抄」，本批把 myemp 接真筛选时误抄了 emplist 的 3，评审当场抓到。
 * ⛔ 新接页面前先看证据件的 `value`，别照抄别页的偏移。
 */
export function defaultDateRange(now: Date = new Date(), monthsAgo = 3): DateRange {
  const from = new Date(now.getFullYear(), now.getMonth() - monthsAgo, now.getDate())
  return { sDate: formatDateRange(from, false), eDate: formatDateRange(now, true) }
}

/**
 * 「**昨天** `00:00:00`」~「今天 `23:59:59`」——`/system/systemlog`（系统操作日志）的默认区间。
 *
 * 与 `defaultDateRange` 的差别**只有起点那一天**：那页对标的服务端值是
 * `2026-09-19 00:00:00` / `2026-09-20 23:59:59`（快照日 2026-09-20 ⇒ 昨天 ~ 今天）。
 */
export function yesterdayDateRange(now: Date = new Date()): DateRange {
  const from = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1)
  return { sDate: formatDateRange(from, false), eDate: formatDateRange(now, true) }
}

/**
 * 把一个 `Date` 折成 `yyyy-MM-dd`（**本地时区**）——「日期框」（laydate `type:'date'`）用的形状。
 *
 * 与 `formatDateRange` 的唯一区别就是**不带时分秒**：那些页的框是 `type:'datetime'`、
 * 值形如 `2026-09-20 00:00:00`；本页对标的框是 `type:'date'`、`value="2026-08-20"`
 * （`order-tradingorderactionloglist.html` L31-39 的 `laydate.render({type:'date'})`）。
 * ⛔ 别把时间硬塞进日期框：那会让输入框里显示一串证据件上没有的字符。
 */
export function formatDateOnly(date: Date): string {
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`
}

/**
 * 「今天往回推 `monthsAgo` 个自然月的那天」~「今天」——**两个都是 `yyyy-MM-dd`**。
 *
 * ⚠️ 与 `defaultDateRange` 同样用 `new Date(y, m - monthsAgo, d)` 的**本地**构造器做「减自然月」
 * （不是「减 N×30 天」），所以跨月天数不同也不会漂：`2026-09-20 − 1 个月 = 2026-08-20`
 * （31 天，8 月有 31 天）。
 *
 * 谁用它、偏移取多少**由各页自己决定**（本模块只给机制）：交易订单操作记录页取 `1`
 * （spec `2026-10-04-trade-action-log-design.md` §3-D10）。
 */
export function dayRangeMonthsAgo(monthsAgo: number, now: Date = new Date()): DateRange {
  const from = new Date(now.getFullYear(), now.getMonth() - monthsAgo, now.getDate())
  return { sDate: formatDateOnly(from), eDate: formatDateOnly(now) }
}

/**
 * 「**今天往回推 `daysAgo` 天**」~「今天」——两个都是 `yyyy-MM-dd`。
 *
 * 与 `dayRangeMonthsAgo` 的差别只有「按天推还是按自然月推」：本函数的偏移**就是天数**
 * （`daysAgo: 29` = 含今天共 **30 天**），⛔ 不是「减 N 个自然月」。
 *
 * ⚠️ 用 `new Date(y, m, d - daysAgo)` 的**本地**构造器（三个数字参数，不会被当 UTC）：
 * `Date` 会把越界的「日」自动归一到上/下一个月，所以跨月 / 跨年都是自然日回退，
 * 不会出现「9 月 0 日」这种串（与 `yesterdayDateRange` 同一机制）。
 *
 * 谁用它、`daysAgo` 取多少**由各页自己决定**（本模块只给机制）：
 * 「人员实名记录与配置」页（`user-rnamecertlogandsetting.ts`）取 **29**，因为业务方
 * 2026-10-10 拍板的默认区间是「**近 30 天**（`today-29d` 到 `today`）」
 * （spec `2026-10-10-realname-verification-design.md` §14-P7 / §15）。
 */
export function dayRangeDaysAgo(daysAgo: number, now: Date = new Date()): DateRange {
  const from = new Date(now.getFullYear(), now.getMonth(), now.getDate() - daysAgo)
  return { sDate: formatDateOnly(from), eDate: formatDateOnly(now) }
}
