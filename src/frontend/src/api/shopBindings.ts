// 店铺对接（阿奇索）的**五个**出口（spec §6.1 + 平台级 AppId 的独立出口）。
//
// ⚠️ **新建本文件**、不复用 `layui-island/config-replica.ts` 的通用配置 CRUD：那一层的
// `ConfigResourceName` 是**显式白名单**（`'games' | 'categories' | 'projects'`，`api/config.ts:91`），
// 扩它要动「配置页」这个概念的类型与测试；更近的先例 `api/orderSources.ts` 也选了专用模块
// （spec §7.1.1 把这条留给实施计划定，本文件按 `orderSources` 那条路落地）。
//
// 分层与 `api/orderSources.ts` 同形：传输、认证与信封解包全在 `request()`（`./http.ts`）里，
// 本文件不 import 任何 UI 库。
import { request } from './http'
import type { Page } from './unwrap'

/** 一行的字段**照 spec §5.1 的模型 + §6.1 的序列化器**（接口一律 snake_case）。 */
export type ShopBindingRow = {
  id: number
  /** 平台码（`ShopType`；本轮只接受 `20` 闲鱼，spec D1）。 */
  platform: number
  /** 平台文案（后端 `get_platform_display()`）——表格「平台」列的 `templet` 用它。 */
  platform_label: string
  /** **平台级** AppId（`settings.AGISO_APP_ID`，只读，未配置时为空串）：表格 `AppId` 列用它。⚠️ 它**不是密钥**（对标本来就把它预填给租户看）；平台 `AGISO_APP_SECRET` **一次都不下发**（spec §9-S3 / §5.2 的更正块）。⚠️ **逐行**下发：一行对接都没有时列表为空 ⇒ 读不到任何值。弹层 `#appId` 的预填因此改走 `fetchPlatformConfig()`（与行解耦）；这个逐行字段保留给表格列用。 */
  platform_app_id: string
  shop_name: string
  /** 阿奇索侧店铺 ID：本轮**不写入、不展示**（spec §5.1），保留字段只为下一轮自动授权。 */
  shop_id: string
  /** 对应订单来源字典项的 id（`null` = 不设置）。列表里**只有 id**，没有名称。 */
  order_source: number | null
  /** 授权码**是否已配置**（布尔）。⚠️ E-0253 起后端**永不下发**授权码字面值（只写不读）：
   * 页面只靠这个布尔显示「已配置 / 未配置」，要看/换值只能在弹层里重新填。 */
  access_token_set: boolean
  remark: string
}

/** 新增 / 编辑的载荷。⚠️ **不做「空值不发」的过滤**（与 `orderSources.ts` 的 `toOrderSourcePayload` 刻意不同）：这里 `order_source: null` 是一个**有意义的取值**——「把绑定改回不设置」只能靠显式发 `null`表达，过滤掉它就等于「保存成功但来源还在」（E-0021 的同族：别拿真值判 0/null）。`access_token` 是**只写**字段（E-0253 起接口永不回显它）：PATCH 里空串/缺省 = 不改（后端 `ShopBindingSerializer.update()`），要换授权码就填新值。 */
export type ShopBindingPayload = {
  platform: number
  shop_name?: string
  order_source?: number | null
  access_token?: string
  remark?: string
}

/** 平台级配置（`GET /api/business/shop-bindings/platform-config/`）。⚠️ **与「行」解耦**：一行店铺对接都没有时列表为空，逐行的 `platform_app_id` 读不到值⇒ 新租户会看不到自己要拿去阿奇索授权的 AppId。这个独立出口就是为那一天准备的（spec §5.2 / §6.1 的 📌 更正块）。 */
export type PlatformConfig = {
  /** 平台级 AppId（`settings.AGISO_APP_ID`；未配置时为空串）。 */
  platform_app_id: string
}

/** 列表的 `page_size` 取 **200** 条（后端上限现为 **300**：`EnvelopePagination.max_page_size`；本页未顶格）。⚠️ 不带就是默认 **20**（`api/base.py:25`）——列表**静默截断**：第 21 条店铺在表格里看不到，页面不报任何错。本页对标是 `page:false`（`pddshipsetting.js:103`，一次取全），所以只能靠这个参数逼近「取全」。⚠️ 同义残差：超过 200 条时仍会被本页截断（本页取 200、未顶格到 300；与 `orderSources.ts` 同一条残差）。 */
const PAGE_SIZE_ALL = 200

const SHOP_BINDINGS_URL = '/api/business/shop-bindings/'

/** 列表 = 本页表格的数据源。显式带 `page=1&page_size=200`，返回**整页**。 */
export async function fetchShopBindings(): Promise<Page<ShopBindingRow>> {
  return request<Page<ShopBindingRow>>({
    method: 'get',
    url: SHOP_BINDINGS_URL,
    params: { page: 1, page_size: PAGE_SIZE_ALL },
  })
}

/** 平台级 AppId——**与任何一行店铺对接无关**（spec §6.1 的 📌 更正块）。
 * ⚠️ 调用方**必须**把它当成「可失败的可选预填值」：它取不到时列表照样要渲染出来
 * （见 `system-pddshipsetting.ts` 的 `reload()`：新端点失败退回逐行的旧路径）。 */
export async function fetchPlatformConfig(): Promise<PlatformConfig> {
  return request<PlatformConfig>({
    method: 'get',
    url: `${SHOP_BINDINGS_URL}platform-config/`,
  })
}

/** 新增。后端成功回 HTTP 200（`EnvelopeModelViewSet.finalize_response` 把 201 归一成 200）。 */
export async function createShopBinding(payload: ShopBindingPayload): Promise<ShopBindingRow> {
  return request<ShopBindingRow>({ method: 'post', url: SHOP_BINDINGS_URL, data: payload })
}

/** 编辑（页面「修改」）。走 **PATCH**：`urls.py` 的端点表只有 GET/POST/PATCH/DELETE
 * （spec §6.1），PUT 是被 `http_method_names` 关掉的 router 默认能力。 */
export async function updateShopBinding(
  id: number,
  payload: ShopBindingPayload,
): Promise<ShopBindingRow> {
  return request<ShopBindingRow>({
    method: 'patch',
    url: `${SHOP_BINDINGS_URL}${id}/`,
    data: payload,
  })
}

/** 删除（硬删；无 `PROTECT` 反向引用，spec §6.1）。 */
export async function deleteShopBinding(id: number): Promise<null> {
  return request<null>({ method: 'delete', url: `${SHOP_BINDINGS_URL}${id}/` })
}
