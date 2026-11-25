// 淘宝发货记录（对标 `/Order/tbsendrecordlist`）的**写出口**（spec §3.7 的第二个端点）。
//
// 分层与 `api/orderSources.ts` / `api/shopBindings.ts` 同形：传输、认证与信封解包全在
// `request()`（`./http.ts`）里——本文件**不另解一层信封**（`request<T>()` 回的就是 `data`）。
//
// ⚠️ 列表（`GET /api/business/tb-send-logs/`）**不在这里**：它由 layui 的 `table.render({url})`
// 自己发（`url` 模式），在 `parseData` 里解同一份统一信封（先例 `order-orderrecycle.ts`）。
// 在这里再包一个取数函数会造出**第二处取数出口**、与 layui 的那条链抢同一格。
import { request } from './http'

/** 列表 / 重试的公共前缀（spec §3.7；挂在 `/api/business/` 下与订单同族）。 */
export const TB_SEND_LOGS_PATH = '/api/business/tb-send-logs/'

/** 一条发货记录的**载荷形状**。⚠️ 键名 = **spec §3.1 那张表的模型字段名**（本仓接口一律 snake_case，先例 `ShopBindingRow`）。这份类型是**前端侧的契约副本**：后端序列化器的字段集若与它不同，页面会把单元格渲染成空白而**不会报错** ⇒ 页测试里有一条「夹具的键 = 这份清单」的用例把它钉住（同 `order-orderrecycle.spec.ts` 的 `RECYCLE_ROW_KEYS` 手法）。⛔ 别在这里加「后端也可能返回别的键名」的兜底别名：两个来源必然漂移，而漂移的表现是「测试全绿、页面空白」（E-0066 同族）。 */
export type TbSendLogRow = {
  /** = 对标的「记录ID」。 */
  id: number
  /** 系统订单号（spec §3.1 明确**不建 FK**）。 */
  order_no: string
  /** 淘宝/闲鱼订单号 = 发货时实际用的那个号（`Order.source_order_no` 的快照）。 */
  tb_order_no: string
  /** 「发货原参数」：本次发给上游的请求参数（不含 app_secret / sign）。 */
  send_params: string
  /** 发货店铺（`XgjShop.shop_name` 的快照；解析不到就是空串）。 */
  store_name: string
  /** `1` 成功 / `0` 失败（与对标 `result` 枚举同值）。 */
  result: number
  /** 发货时间（ISO 串；理论可为 `null`，页面按空串兜底）。 */
  sent_at: string | null
  /** 发货人：自动触发写 `系统`；页面「重试发货」写当前登录人显示名。 */
  operator: string
  /** 备注说明 = 失败原因。 */
  remark: string
  /** 落库时间。 */
  created_at: string
}

/** 「重试发货」的返回体（spec §3.7「逐条重发，返回逐条结果」）。⚠️ **spec 没有把这两个计数键写死**；这里取的是**对标自己那句文案的载荷形状**（`tbsendrecordmang.js` L141-143：`"成功" + data.success + "条，失败" + data.fail + "条"`）。两键都是可选：拿不到就不要编数字，页面退化成一句不点名的中性反馈（见 `order-tbsendrecordlist.ts`的 `retryFeedback`）。 */
export type TbSendRetryResult = {
  success?: number
  fail?: number
}

/** 重试发货端点（spec §3.7）。 */
export const TB_SEND_LOGS_RETRY_PATH = `${TB_SEND_LOGS_PATH}retry/`

/** 重试发货：入参 = 勾选行的**记录 id 列表**（spec §3.7 的第一种形态）。⚠️ 这正是页面上「重试发货」那颗按钮的**唯一**写出口（`order-tbsendrecordlist.ts` 的 `doRetry`）。参数名 `ids` 是前后端之间的一处契约：spec 只写了「id 列表」，没写 JSON 键 ⇒ 若后端改用别的键名（或改用 `tb_order_no` 列表），改**这一行**即可，页面其余部分不用动。 */
export async function retrySendLogs(ids: readonly number[]): Promise<TbSendRetryResult> {
  return request<TbSendRetryResult>({
    method: 'post',
    url: TB_SEND_LOGS_RETRY_PATH,
    data: { ids },
  })
}
