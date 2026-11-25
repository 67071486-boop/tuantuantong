/** 【财务相关 → 闲管家收款明细】页（`pages/xgj-receipts.ts`）的端点封装。后端契约的**权威出处** = `src/apps/business/api/xgj_receipt_views.py` 的模块 docstring与取数层 `src/apps/business/xgj_receipt_stats.py`；本模块按那一份逐键声明（两边漂移时先改后端 docstring 再改这里）。| 方法 | 路径 | 用途 ||---|---|---|| GET | `/api/business/xgj-receipts/summary/` | 按店铺分组：店名 / 笔数 / 实收总额 / 金额缺失笔数（`?date_from=&date_to=`，缺省=前一天） || GET | `/api/business/xgj-receipts/` | 同筛选的分页明细行（`?page_size=`，缺省 50） |## 两条不能写错的纪律1. ⛔ **金额一律字符串、原样上屏**（后端同口径：`Decimal` 不交给 JSONEncoder 变 `float`）。
 * 前端合计**直接读后端的 `totals`**（`summary` 响应自带合计块）⇒ 页面**不做任何金额加法**，⛔ 不引入浮点。2. ⭐ **本页的节点 code 刻意住在本模块里**（⛔ 不在岛页里），与 `api/takerWallets.ts` /`api/withdrawals.ts` 同一手法：`tests/test_rbac_menu_alignment.py` 会把`layui-island/pages/*.ts` 里的**带点**字符串字面量当按钮级权限门收走并冻结条数⇒ 门常量放这里，那条冻结数不因为「多了一页」而变化。 */
import type { Page } from './unwrap'
import { request } from './http'

/** 本页自己的节点门（与 `config/routePermissions.ts` 的 `ROUTE_NODE_CODES['xgj-receipts']`**必须同值**）。用途：岛页被**深链**（`page.html?page=xgj-receipts`）打开时自己判一次可见性（那一刻路由那道门不生效——先例见 `pages/agents-wallet-adjust.ts`）。fail-closed：判不过就**不注入标记、一个请求都不发**（`AGENTS.md` §5-D / D-0154 / E-0098）。 */
export const XGJ_RECEIPT_NODE = 'finance.xgj_receipt_detail'

/** 端点路径（与后端 `urls.py` 的两条显式 `path()` 同形，都带尾斜杠）。 */
export const XGJ_RECEIPT_SUMMARY_URL = '/api/business/xgj-receipts/summary/'
export const XGJ_RECEIPT_LIST_URL = '/api/business/xgj-receipts/'

/** 日期区间（`yyyy-MM-dd` 字符串；两个键都可选，缺省 = 后端按「前一天」算）
 * + ⭐ 公司店铺分组（计划 `docs/plans/2026-10-26-order-source-groups.md` §4 #6）。 */
export type ReceiptRange = {
  date_from?: string
  date_to?: string
  /** 分组 id；`undefined` = 不筛（⛔ 页面不送空串——后端会把空串判非法整数）。 */
  source_group?: number
}

/** 组装区间查询参数（唯一出口）：**没给的键不发**（判据 `!== undefined`，E-0021）。 */
export function toRangeParams(range: ReceiptRange = {}): Record<string, string> {
  const out: Record<string, string> = {}
  if (range.date_from !== undefined) out.date_from = range.date_from
  if (range.date_to !== undefined) out.date_to = range.date_to
  // ⭐ 判据是 `!== undefined`（E-0021）：分组 id 是数字，`0` 非法但由后端拒；这里只区分
  //    「选了」与「没选」——没选时页面传 `undefined`、键**不出现**（不是空串）。
  if (range.source_group !== undefined) out.source_group = String(range.source_group)
  return out
}

/** 汇总的一行（后端 `xgj_receipt_stats.summary_rows` 的 `items[]` 逐键）。 */
export type ReceiptSummaryRow = {
  /** 店铺分组键（回查详情 `seller_eid`，spec §12 F2-D；未识别桶 = 空串）。 */
  shop_identity: string
  /** 展示名（后端已按「现解析 → 快照 → 组键」兜好；空串 + 空键时前端兜「未识别店铺」）。 */
  shop_name: string
  /** 交易成功笔数（总笔数 = 计入金额笔数 + 缺失笔数）。 */
  count: number
  /** 实收总金额（**字符串**两位小数；仅非空行求和，全空组 = "0.00"）。 */
  amount: string
  /** 金额缺失笔数（待回查 / 回查失败）。 */
  missing_count: number
}

/** 汇总载荷：分组行 + 合计块（合计由**后端**算，前端不做金额加法）。 */
export type ReceiptSummary = {
  items: ReceiptSummaryRow[]
  totals: {
    shop_count: number
    count: number
    amount: string
    missing_count: number
  }
}

/** 明细的一行（后端 `xgj_receipt_stats.serialize_detail` 逐键）。 */
export type ReceiptDetailRow = {
  id: number
  /** 闲鱼订单号（推送原文）。 */
  order_no: string
  /** 商家级 ID（推送原文，**行级事实**保留；不再是分组键，spec §12 F-2）。 */
  seller_id: string
  /** 店铺分组键（回查详情 `seller_eid`；未识别行为空串）。 */
  shop_identity: string
  shop_name: string
  user_name: string
  /** 实收金额（**字符串**；空串 = 待回查 / 回查失败——用 `amount_missing` 区分，⛔ 别拿空串当 0）。 */
  amount: string
  amount_missing: boolean
  /** 交易成功时间（ISO 8601 本地时刻；`null` = 推送没给可解析的时间）。 */
  success_time: string | null
  matched_count: number
  /** 匹配结果的一句人话（后端拼好，前端原样上屏）。 */
  match_summary: string
}

/** 汇总（`GET …/summary/`）。 */
export function fetchReceiptSummary(range: ReceiptRange = {}): Promise<ReceiptSummary> {
  return request<ReceiptSummary>({
    method: 'get',
    url: XGJ_RECEIPT_SUMMARY_URL,
    params: toRangeParams(range),
  })
}

/** 明细列表参数（分页参数与区间并排；`page` 从 1 起；`shop_identity` = 点店铺看单店明细，spec §12 F2-D）。⚠️ `shop_identity` 的两档都要**发得出去**：非空 = 单店精确筛；**空串 = 「未识别店铺」桶**（后端按「identity 为空」筛）。所以判据是 `!== undefined`（⛔ 不写 `!== ''`——那会把点桶变成「不带参 = 整窗明细」，数字对不上）。 */
export type ReceiptListParams = ReceiptRange & {
  page?: number
  page_size?: number
  shop_identity?: string
}

/** 明细分页（`GET …/`）。 */
export function fetchReceiptList(params: ReceiptListParams = {}): Promise<Page<ReceiptDetailRow>> {
  const query: Record<string, string | number> = toRangeParams(params)
  if (params.page !== undefined) query.page = params.page
  if (params.page_size !== undefined) query.page_size = params.page_size
  if (params.shop_identity !== undefined) query.shop_identity = params.shop_identity
  return request<Page<ReceiptDetailRow>>({
    method: 'get',
    url: XGJ_RECEIPT_LIST_URL,
    params: query,
  })
}
