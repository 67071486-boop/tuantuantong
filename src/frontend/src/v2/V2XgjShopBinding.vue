<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「闲管家店铺对接」v2（`/v2/xgj-shop`；第三方平台批 B3，计划 §2）。
 * 旧页 = island 自有功能页 `layui-island/pages/thirdparty-xgjshop.ts`（六件事 + 工具栏三颗 + 行操作四颗），文案与提示逐字照旧页。
 * 四条红线：app_secret 只写不读；两个推送地址只读；操作只按行 id；订购标黄 ≠ 版本不对。⛔ 无「向下游同步上家大厅单」开关（D-0329 已撤，别加回）。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import {
  createXgjBinding,
  deleteXgjBinding,
  fetchXgjBindings,
  fetchXgjShops,
  queryXgjOrderDetail,
  syncXgjShops,
  testXgjConnection,
  updateXgjBinding,
  type XgjOrderDetail,
  type XgjShopBindingRow,
  type XgjShopRow,
  type XgjSyncResult,
} from '@/api/xgjBindings'

/* ── 逐字文案与常量（出处 = island 页同名常量；本地重写不引模块）──────────────── */

const ASSET_BASE = import.meta.env.BASE_URL || '/'

/** 引导图（旧页 GUIDE_IMAGES 逐字：业务方 2026-09-22 两张闲管家截图，AppSecret 已打码）。 */
const GUIDE_IMAGES = [
  {
    src: `${ASSET_BASE}guide/xgj-add-app.png`,
    alt: '闲管家工作台：左侧「开放平台 → 应用」，右上角「添加应用」',
    caption: '① 左侧「开放平台 → 应用」，右上角「添加应用」',
  },
  {
    src: `${ASSET_BASE}guide/xgj-app-key.png`,
    alt: '闲管家「应用概况」：应用类型=进销存、应用模式=自研系统，AppKey / AppSecret，右上角「确认上线」',
    caption: '② 「应用概况」里的 AppKey / AppSecret，右上角「确认上线」',
  },
] as const

/** 空态与加载（应用表 / 店铺表逐字照旧页 table text.none）。 */
const BINDING_EMPTY_TEXT = '还没有配置闲管家店铺对接'
const SHOP_EMPTY_TEXT = '还没有同步到店铺（先保存应用，或点「同步店铺」）'
const LOADING_TEXT = '正在加载…'
const SHOP_SECTION_TITLE = '店铺（一个应用最多 30 家店；每家店对应一个订单来源）'


/** 弹层与提示文案（旧页逐字）。 */
const EDIT_TITLE_ADD = '新增闲管家店铺对接'
const EDIT_TITLE_EDIT = '修改闲管家店铺对接'
const QUERY_DIALOG_TITLE = '查询闲管家订单详情'
const TEST_DIALOG_TITLE = '测试连接'
const APP_KEY_PLACEHOLDER = '请输入应用的 AppKey'
const SHOP_NAME_PLACEHOLDER = '例如：主应用'
const APP_SECRET_PLACEHOLDER = '留空表示不修改'
const APP_SECRET_HINT = '为了安全，保存后不再回显；要换就直接填新的。'
const ORDER_NO_PLACEHOLDER = '闲鱼订单号（纯数字）'
const SAVE_OK_TEXT = '已保存'
const DELETE_OK_TEXT = '已删除'
const REFRESH_OK_TEXT = '已刷新'
const ORDER_NO_NOT_NUMERIC_TEXT = '订单号只能是数字'
const BINDING_NOT_PICKED_TEXT = '请先选择应用'
const QUERYING_TEXT = '查询中…'
const TEST_EMPTY_SHOPS_TEXT = '连接成功，但这个应用还没有绑定任何闲鱼店铺'

function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

/* ── toast（同族页口径：info 1500ms / error 6000ms）────────────────────────── */

const toast = ref<{ text: string; kind: 'info' | 'error' } | null>(null)
let toastTimer: ReturnType<typeof setTimeout> | null = null

function showToast(text: string, kind: 'info' | 'error' = 'info'): void {
  if (toastTimer !== null) clearTimeout(toastTimer)
  toast.value = { text, kind }
  toastTimer = setTimeout(() => {
    toast.value = null
    toastTimer = null
  }, kind === 'error' ? 6000 : 1500)
}

async function copyPushUrl(url: string): Promise<void> {
  try {
    if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable')
    await navigator.clipboard.writeText(url)
    showToast('已复制推送地址')
  } catch {
    showToast('复制失败，请手动复制', 'error')
  }
}

/* ── 应用表 + 店铺表（两个请求两张表；店铺按应用逐个取再拼）────────────────────── */

const rows = ref<XgjShopBindingRow[]>([])
const shopRows = ref<XgjShopRow[]>([])
const loading = ref(false)
const shopsLoading = ref(false)

/** 拉一次店铺表；失败如实说出来并把表清空（旧页 loadShops 同口径）。 */
async function loadShops(): Promise<void> {
  shopsLoading.value = true
  if (rows.value.length === 0) {
    shopRows.value = []
    shopsLoading.value = false
    return
  }
  try {
    const lists = await Promise.all(rows.value.map((r) => fetchXgjShops(r.id)))
    shopRows.value = lists.flatMap((l) => l.items)
  } catch (e) {
    shopRows.value = []
    showToast(errText(e), 'error')
  } finally {
    shopsLoading.value = false
  }
}

/** 应用列表成功 ⇒ 回填两表；失败抛给调用方（表保留旧数据，照旧页 reloadData 不执行的口径）。 */
async function reload(): Promise<void> {
  const page = await fetchXgjBindings()
  rows.value = page.items
  await loadShops()
}

async function onRefresh(): Promise<void> {
  loading.value = true
  try {
    await reload()
    showToast(REFRESH_OK_TEXT)
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    loading.value = false
  }
}

/* ── 新增 / 修改弹层（app_secret 永远留空；空串照传 = 后端「不改」）────────────── */

const editDlg = ref(false)
const editingId = ref<number | null>(null)
const editSaving = ref(false)
const editForm = reactive({ shopName: '', appKey: '', appSecret: '', remark: '' })

const editTitle = computed(() => (editingId.value === null ? EDIT_TITLE_ADD : EDIT_TITLE_EDIT))

function openEdit(row: XgjShopBindingRow | null): void {
  editingId.value = row?.id ?? null
  editForm.shopName = row?.shop_name ?? ''
  editForm.appKey = row?.app_key ?? ''
  // 后端不回 app_secret（write_only）⇒ 回显不可能也不允许；留空 = 不改。
  editForm.appSecret = ''
  editForm.remark = row?.remark ?? ''
  editDlg.value = true
}

/** 保存后那句提示（旧页 saveMessage 逐字：同步失败不影响保存成功，必须说出来）。 */
function saveMessage(sync?: XgjSyncResult): string {
  if (!sync) return SAVE_OK_TEXT
  if (sync.skipped) return `已保存；但${sync.skipped}，店铺还没同步`
  if (sync.error) {
    const code = sync.code === undefined ? '' : `（错误码 ${sync.code}）`
    return `已保存，但${sync.error}${code}——请检查 AppKey / AppSecret 是否复制完整`
  }
  return (
    `已保存，已同步 ${sync.created_shops ?? 0} 家新店铺、更新 ${sync.updated_shops ?? 0} 家、` +
    `新建 ${sync.created_sources ?? 0} 个订单来源`
  )
}

async function submitEdit(): Promise<void> {
  if (editSaving.value) return
  editSaving.value = true
  const payload = {
    shop_name: editForm.shopName.trim(),
    app_key: editForm.appKey.trim(),
    // 空串照传（后端把空串当「不改」）；绝不能过滤掉再换成「清空」的语义。
    app_secret: editForm.appSecret.trim(),
    remark: editForm.remark.trim(),
  }
  try {
    const saved =
      editingId.value === null
        ? await createXgjBinding(payload)
        : await updateXgjBinding(editingId.value, payload)
    editDlg.value = false
    loading.value = true
    try {
      // 保存会自动同步一次店铺（R5）⇒ 两张表都要重拉，提示里带上同步结果。
      await reload()
      showToast(saveMessage(saved.sync))
    } catch (e) {
      showToast(errText(e), 'error')
    } finally {
      loading.value = false
    }
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    editSaving.value = false
  }
}

/* ── 删除（二次确认；只按行 id）────────────────────────────────────────────── */

const delDlg = ref<XgjShopBindingRow | null>(null)
const deleting = ref(false)
const delTargetName = computed(() => delDlg.value?.shop_name || delDlg.value?.app_key || '')

async function confirmDel(): Promise<void> {
  const row = delDlg.value
  if (row === null || deleting.value) return
  deleting.value = true
  try {
    await deleteXgjBinding(row.id)
    delDlg.value = null
    loading.value = true
    try {
      await reload()
      showToast(DELETE_OK_TEXT)
    } catch (e) {
      showToast(errText(e), 'error')
    } finally {
      loading.value = false
    }
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    deleting.value = false
  }
}

/* ── 行内「同步店铺」：只刷店铺表（同步不改应用行）────────────────────────────── */

async function onSync(row: XgjShopBindingRow): Promise<void> {
  try {
    const result = await syncXgjShops(row.id)
    await loadShops()
    showToast(
      `同步完成：新增店铺 ${result.created_shops ?? 0} 家、` +
        `更新 ${result.updated_shops ?? 0} 家、新建来源 ${result.created_sources ?? 0} 个`,
    )
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

/* ── 行内「测试连接」：空列表是合法结果，与调用失败不是一回事 ──────────────────── */

const testDlg = ref<{ count: number; names: string[] } | null>(null)

async function onTest(row: XgjShopBindingRow): Promise<void> {
  try {
    const { shops } = await testXgjConnection(row.id)
    if (shops.length === 0) {
      showToast(TEST_EMPTY_SHOPS_TEXT)
      return
    }
    const names = shops.map((s) =>
      String(s.shop_name ?? s.user_nick ?? s.user_name ?? '（无名）'),
    )
    testDlg.value = { count: shops.length, names }
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

/* ── 查询订单详情弹层（纯数字订单号前端先挡一次；未登记字段照样显示）────────────── */

const queryDlg = ref(false)
const queryForm = reactive({ binding: '', orderNo: '' })
const detailRows = ref<Array<[string, string]> | null>(null)
const detailLoading = ref(false)

function openQuery(): void {
  // 旧页每次打开都重填下拉（选中回落到第一行），结果区清空；订单号保留上次输入。
  queryForm.binding = String(rows.value[0]?.id ?? '')
  detailRows.value = null
  detailLoading.value = false
  queryDlg.value = true
}

/** 上游订单字段 → 中文标签（官方 order_detail schema；只给显示用）。 */
const DETAIL_LABELS: Record<string, string> = {
  order_no: '闲鱼订单号',
  order_status: '订单状态原码',
  order_time: '买家下单时间',
  total_amount: '订单下单金额（分）',
  pay_amount: '订单实付金额（分）',
  pay_time: '支付时间',
  pay_no: '支付宝交易号',
  refund_status: '退款状态',
  refund_amount: '已退款金额（分）',
  refund_time: '退款时间',
  receiver_name: '收货人',
  receiver_mobile: '收货人电话',
  prov_name: '收货省份',
  city_name: '收货城市',
  area_name: '收货地区',
  town_name: '收货街道',
  address: '收货详细地址',
  waybill_no: '快递单号',
  express_code: '快递公司代码',
  express_name: '快递公司',
  express_fee: '运费（分）',
  consign_time: '发货时间',
  consign_type: '发货类型',
  confirm_time: '成交时间',
  cancel_reason: '取消原因',
  cancel_time: '取消时间',
  create_time: '订单创建时间',
  update_time: '订单更新时间',
  buyer_nick: '买家昵称',
  buyer_eid: '买家标识',
  seller_name: '卖家会员名',
  seller_eid: '卖家标识',
  seller_remark: '卖家备注',
  order_type: '订单类型原码',
  idle_biz_type: '子业务类型',
  pin_group_status: '拼团状态',
  is_tax_included: '是否含税',
  is_privacy: '是否隐私号订单',
}

/** 嵌套 goods 展平成与顶层同级显示。 */
const GOODS_LABELS: Record<string, string> = {
  title: '商品标题',
  price: '商品单价（分）',
  quantity: '购买数量',
  outer_id: '商家编码',
  sku_id: '管家SKU规格ID',
  sku_outer_id: '商家SKU编码',
  sku_text: 'SKU规格',
  product_id: '管家商品ID',
  item_id: '闲鱼商品ID',
  service_support: '商品服务项',
  images: '商品主图',
}

/** 值 → 显示串：null/undefined 显示空（不是 "null"），对象/数组转 JSON。 */
function formatDetailValue(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

/** 订单详情 → 「标签 / 值」两列；未登记的字段照样显示（标签回落英文原名）。 */
function renderDetailRows(detail: XgjOrderDetail): Array<[string, string]> {
  const result: Array<[string, string]> = []
  for (const [key, value] of Object.entries(detail)) {
    if (key === 'goods' && typeof value === 'object' && value !== null) {
      for (const [gKey, gValue] of Object.entries(value as Record<string, unknown>)) {
        result.push([GOODS_LABELS[gKey] ?? `商品.${gKey}`, formatDetailValue(gValue)])
      }
      continue
    }
    result.push([DETAIL_LABELS[key] ?? key, formatDetailValue(value)])
  }
  return result
}

async function submitQuery(): Promise<void> {
  if (detailLoading.value) return
  const orderNo = queryForm.orderNo.trim()
  // 校验顺序照旧页：先挡非数字，再挡没选应用（免得白跑一趟拿 1000）。
  if (!/^[0-9]+$/.test(orderNo)) {
    showToast(ORDER_NO_NOT_NUMERIC_TEXT)
    return
  }
  const binding = Number(queryForm.binding)
  if (!binding) {
    showToast(BINDING_NOT_PICKED_TEXT)
    return
  }
  detailLoading.value = true
  detailRows.value = null
  try {
    const { detail } = await queryXgjOrderDetail(binding, orderNo)
    detailRows.value = renderDetailRows(detail)
  } catch (e) {
    detailRows.value = null
    showToast(errText(e), 'error')
  } finally {
    detailLoading.value = false
  }
}

/* ── 店铺表格子渲染（三态订购状态；标黄只说订购、不说版本）────────────────────── */

function subscriptionCellOf(row: XgjShopRow): { cls: string; text: string } {
  if (row.subscription_warning) return { cls: 'xgj-sub-warn', text: '订购异常：可能识别不到订单' }
  if (row.is_valid === null && row.valid_end_time === null) {
    return { cls: 'xgj-sub-unknown', text: '—（还没同步到订购状态）' }
  }
  return { cls: 'xgj-sub-ok', text: '正常' }
}

const shopView = computed(() => shopRows.value.map((s) => ({ ...s, sub: subscriptionCellOf(s) })))

onMounted(() => {
  void reload().catch((e: unknown) => showToast(errText(e), 'error'))
})
</script>

<template>
  <div class="cv2-page" data-v2-page="xgj-shop">
    <!-- ① 四步图文引导（步骤与图注逐字） -->
    <section class="cv2-panel" aria-label="四步接好闲管家" data-v2="guide-panel">
      <div class="cv2-panel-head">
        <h3 class="cv2-panel-title">四步接好闲管家（照着下面两张图点）</h3>
      </div>
      <ol class="xgj-guide-list" data-v2="guide-steps">
        <li>打开<b>闲管家工作台</b>，左侧菜单点「<b>开放平台</b>」→「<b>应用</b>」。</li>
        <li>
          点右上角「<b>添加应用</b>」：应用模式选「<b>我有自研系统</b>」，应用类型选「<b>进销存</b>」，应用名称可填你工作室的名称。
          <span class="xgj-guide-hint">（「添加应用」表单里的那一项叫「我有自研系统」；建好之后在「应用概况」里会显示成「自研系统」——两张截图只拍到了后者。）</span>
        </li>
        <li>添加成功后，在「应用概况」里复制 <b>AppKey</b> 与 <b>AppSecret</b>，回到本页点「<b>新增对接</b>」把它们填进来。</li>
        <li>回到闲管家点「<b>确认上线</b>」；再回本页点这一行的「<b>同步店铺</b>」，把店铺同步成本系统的订单来源。</li>
      </ol>
      <div class="xgj-guide-figs" data-v2="guide-figs">
        <figure v-for="img in GUIDE_IMAGES" :key="img.caption" class="xgj-guide-fig">
          <img :src="img.src" :alt="img.alt" />
          <figcaption>{{ img.caption }}</figcaption>
        </figure>
      </div>
    </section>

    <!-- ② 版本声明（只声明 + 提醒，不做程序判定；标黄 ≠ 不是铂金版） -->
    <div class="cv2-note xgj-version" data-v2="version-notice">
      <p class="xgj-version-strong">店铺版本要求：店铺必须是「铂金版」或「ERP 专业版」，才能识别订单。</p>
      <p>低于这两个版本的店铺，闲管家开放平台<b>读不到</b>它的订单——这不是团团通能绕过的限制，请先到闲管家把店铺版本升到铂金版或 ERP 专业版。</p>
      <p class="xgj-version-dim">下面店铺表的「订购状态」标黄，只表示「订购看起来无效或已过期」，<b>标黄 ≠ 不是铂金版</b>；反过来，不标黄的店也<b>不保证</b>一定能读到订单（最终以闲管家接口返回为准）。</p>
    </div>

    <!-- ③ 配置说明 -->
    <div class="cv2-note xgj-intro" data-v2="intro-note">
      <p>在这里登记<b>你自己</b>在闲管家开放平台创建的「自研应用」：填 <b>AppKey / AppSecret</b>。保存后会自动按<b>店铺名</b>生成订单来源（<b>一家店 = 一个订单来源</b>），录单时填了单号就自动判定是哪家店。</p>
      <p class="xgj-warn-text">一个订单来源只绑一边：同一个来源如果在「其它平台店铺对接」里也绑了阿奇索，<b>录单会走阿奇索</b>，这里配的这一行<b>不会生效</b>。</p>
    </div>

    <!-- ④ 应用表：工具栏三颗（新增对接 / 查询订单详情 / 刷新）+ 行操作四颗 -->
    <section class="cv2-panel cv2-table-panel" aria-label="闲管家店铺对接列表" data-v2="binding-panel">
      <div class="cv2-toolbar" data-v2="toolbar">
        <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="add" @click="openEdit(null)">新增对接</button>
        <button type="button" class="cv2-btn cv2-btn--find cv2-btn--fill" data-v2="query" @click="openQuery()">查询订单详情</button>
        <button type="button" class="cv2-btn cv2-btn--find" data-v2="refresh" :disabled="loading" @click="void onRefresh()">刷新</button>
      </div>
      <div class="cv2-table-scroll">
        <table class="cv2-table xgj-binding-table" data-v2="binding-table">
          <colgroup>
            <col style="width: 110px" />
            <col style="width: 140px" />
            <col style="width: 75px" />
            <col style="width: 280px" />
            <col style="width: 280px" />
            <col style="width: 120px" />
            <col style="width: 260px" />
          </colgroup>
          <thead>
            <tr>
              <th>应用备注名</th>
              <th>AppKey</th>
              <th class="cv2-th-center">AppSecret</th>
              <th>订单消息推送URL</th>
              <th>商品消息推送URL</th>
              <th>备注说明</th>
              <th class="cv2-th-center">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="row.id" class="cv2-row" :data-v2-row="row.id">
              <td :data-v2-shop-name="row.id">{{ row.shop_name }}</td>
              <td class="xgj-mono">{{ row.app_key }}</td>
              <td class="cv2-td-center">
                <!-- 只回答「配没配」：后端 write_only，字面值一次都不进浏览器。 -->
                <span :class="row.app_secret_set ? 'xgj-set' : 'xgj-unset'" :data-v2-secret="row.id">{{ row.app_secret_set ? '已配置' : '未配置' }}</span>
              </td>
              <td class="xgj-push-cell">
                <template v-if="row.order_push_url">
                  <div class="xgj-push-line"><span class="xgj-push-url">{{ row.order_push_url }}</span><button type="button" class="cv2-copybtn" :data-v2-copy-order="row.id" aria-label="复制订单推送地址" title="复制订单推送地址" @click="void copyPushUrl(row.order_push_url)"><svg viewBox="0 0 16 16" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="9" rx="1.5" /><path d="M10.5 3.5h-6A1.5 1.5 0 0 0 3 5v7" /></svg></button></div>
                </template>
              </td>
              <td class="xgj-push-cell">
                <template v-if="row.product_push_url">
                  <div class="xgj-push-line"><span class="xgj-push-url">{{ row.product_push_url }}</span><button type="button" class="cv2-copybtn" :data-v2-copy-product="row.id" aria-label="复制商品推送地址" title="复制商品推送地址" @click="void copyPushUrl(row.product_push_url)"><svg viewBox="0 0 16 16" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="9" rx="1.5" /><path d="M10.5 3.5h-6A1.5 1.5 0 0 0 3 5v7" /></svg></button></div>
                </template>
              </td>
              <td class="xgj-remark" :title="row.remark">{{ row.remark }}</td>
              <td class="cv2-td-center xgj-actions">
                <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--sm cv2-btn--commit" :data-v2-edit="row.id" @click="openEdit(row)">修改</button>
                <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--commit" :data-v2-sync="row.id" @click="void onSync(row)">同步店铺</button>
                <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--find" :data-v2-test="row.id" @click="void onTest(row)">测试连接</button>
                <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--sm" :data-v2-del="row.id" @click="delDlg = row">删除</button>
              </td>
            </tr>
            <tr v-if="rows.length === 0">
              <td :colspan="7" class="cv2-empty" data-v2="empty">
                <template v-if="loading">{{ LOADING_TEXT }}</template>
                <template v-else>{{ BINDING_EMPTY_TEXT }}</template>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- ⑤ 店铺表（店 → 订单来源；订购异常标黄单元格，只说订购、不说版本） -->
    <section class="cv2-panel cv2-table-panel" aria-label="店铺" data-v2="shop-panel">
      <div class="cv2-panel-head">
        <h3 class="cv2-panel-title">{{ SHOP_SECTION_TITLE }}</h3>
      </div>
      <div class="cv2-table-scroll">
        <table class="cv2-table" data-v2="shop-table">
          <colgroup>
            <col style="width: 180px" />
            <col style="width: 150px" />
            <col style="width: 190px" />
            <col style="width: 160px" />
            <col style="width: 230px" />
          </colgroup>
          <thead>
            <tr>
              <th>店铺名称</th>
              <th>会员名</th>
              <th>对应来源</th>
              <th>所属应用</th>
              <th class="cv2-th-center">订购状态</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="shop in shopView" :key="shop.id" class="cv2-row" :data-v2-shop-row="shop.id">
              <td :data-v2-shop-cell="shop.id">{{ shop.shop_name }}</td>
              <td>{{ shop.user_name }}</td>
              <td :data-v2-shop-source="shop.id">
                <template v-if="shop.order_source_name">{{ shop.order_source_name }}</template>
                <span v-else class="xgj-source-missing">还没建（点「同步店铺」）</span>
              </td>
              <td>{{ shop.binding_name }}</td>
              <td class="cv2-td-center">
                <span :class="shop.sub.cls" :data-v2-sub="shop.id">{{ shop.sub.text }}</span>
              </td>
            </tr>
            <tr v-if="shopRows.length === 0">
              <td :colspan="5" class="cv2-empty" data-v2="shop-empty">
                <template v-if="shopsLoading">{{ LOADING_TEXT }}</template>
                <template v-else>{{ SHOP_EMPTY_TEXT }}</template>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- 新增 / 修改弹层（AppSecret 密码框永远留空；半填误关代价高 ⇒ 不点遮罩关窗） -->
    <el-dialog
      :model-value="editDlg"
      :title="editTitle"
      class="cv2-dialog cv2-dialog--md"
      destroy-on-close
      :close-on-click-modal="false"
      data-v2="dlg-edit"
      @update:model-value="editDlg = false"
    >
      <div class="cv2-field cv2-form-field">
        <span class="cv2-field-label">应用备注名</span>
        <el-input v-model="editForm.shopName" :maxlength="64" :placeholder="SHOP_NAME_PLACEHOLDER" data-v2="edit-shop-name" />
      </div>
      <div class="cv2-field cv2-form-field">
        <span class="cv2-field-label">AppKey</span>
        <el-input v-model="editForm.appKey" :maxlength="64" :placeholder="APP_KEY_PLACEHOLDER" data-v2="edit-app-key" />
      </div>
      <div class="cv2-field cv2-form-field">
        <span class="cv2-field-label">AppSecret</span>
        <el-input
          v-model="editForm.appSecret"
          type="password"
          autocomplete="new-password"
          :maxlength="128"
          :placeholder="APP_SECRET_PLACEHOLDER"
          data-v2="edit-app-secret"
        />
        <p class="cv2-field-hint xgj-field-hint" data-v2="edit-app-secret-hint">{{ APP_SECRET_HINT }}</p>
      </div>
      <div class="cv2-field cv2-form-field">
        <span class="cv2-field-label">备注说明</span>
        <el-input v-model="editForm.remark" :maxlength="200" data-v2="edit-remark" />
      </div>
      <div class="cv2-dlg-actions">
        <button type="button" class="cv2-btn cv2-btn--submit cv2-btn--commit" data-v2="edit-save" :disabled="editSaving" @click="void submitEdit()">保存</button>
      </div>
    </el-dialog>

    <!-- 查询订单详情弹层（应用下拉 + 纯数字校验 + 上游字段逐项两列表、未登记字段照样显示） -->
    <el-dialog
      :model-value="queryDlg"
      :title="QUERY_DIALOG_TITLE"
      class="cv2-dialog cv2-dialog--md"
      destroy-on-close
      :close-on-click-modal="false"
      data-v2="dlg-query"
      @update:model-value="queryDlg = false"
    >
      <div class="cv2-field cv2-form-field">
        <span class="cv2-field-label">应用</span>
        <select v-model="queryForm.binding" class="cv2-input" data-v2="query-binding">
          <option v-for="r in rows" :key="r.id" :value="String(r.id)">{{ r.shop_name || `#${r.id}` }}</option>
        </select>
      </div>
      <div class="cv2-field cv2-form-field">
        <span class="cv2-field-label">订单号</span>
        <el-input v-model="queryForm.orderNo" :maxlength="32" :placeholder="ORDER_NO_PLACEHOLDER" data-v2="query-order-no" />
      </div>
      <div class="cv2-dlg-actions xgj-query-actions">
        <button type="button" class="cv2-btn cv2-btn--submit cv2-btn--find cv2-btn--fill" data-v2="query-submit" :disabled="detailLoading" @click="void submitQuery()">查询</button>
      </div>
      <div class="xgj-detail-result" data-v2="detail-result">
        <p v-if="detailLoading" class="xgj-detail-loading" data-v2="detail-loading">{{ QUERYING_TEXT }}</p>
        <table v-else-if="detailRows !== null" class="cv2-table xgj-detail-table" data-v2="detail-table">
          <colgroup>
            <col style="width: 150px" />
            <col />
          </colgroup>
          <tbody>
            <tr v-for="(pair, idx) in detailRows" :key="idx" class="cv2-row">
              <td class="xgj-detail-label">{{ pair[0] }}</td>
              <td class="xgj-detail-value">{{ pair[1] }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </el-dialog>

    <!-- 删除二次确认（确认语逐字；layer.confirm 默认按钮口径 = 取消 / 确定） -->
    <el-dialog
      :model-value="delDlg !== null"
      title="提示"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-del"
      @update:model-value="delDlg = null"
    >
      <p class="cv2-dlg-text" data-v2="del-confirm-text">确定删除「{{ delTargetName }}」这条对接吗？</p>
      <div class="cv2-dlg-actions">
        <button type="button" class="cv2-btn cv2-btn--quiet" data-v2="del-cancel" @click="delDlg = null">取消</button>
        <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--commit" data-v2="del-ok" :disabled="deleting" @click="void confirmDel()">确定</button>
      </div>
    </el-dialog>

    <!-- 测试连接结果（能操作哪些店；空列表走 toast、不开这层） -->
    <el-dialog
      :model-value="testDlg !== null"
      :title="TEST_DIALOG_TITLE"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-test"
      @update:model-value="testDlg = null"
    >
      <template v-if="testDlg !== null">
        <p class="cv2-dlg-text" data-v2="test-count">这个应用能操作 {{ testDlg.count }} 家店：</p>
        <p class="xgj-test-names" data-v2="test-names">{{ testDlg.names.join('、') }}</p>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--commit" data-v2="test-ok" @click="testDlg = null">确定</button>
        </div>
      </template>
    </el-dialog>

    <!-- 炭火 toast -->
    <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
      {{ toast.text }}
    </div>
  </div>
</template>

<style scoped>
/* 本页新增的形状收在这里；色一律走 carbon.css 的 --tt2 token（⛔ 不写裸色值）。 */

.xgj-mono {
  font-family: var(--tt2-mono);
  font-variant-numeric: tabular-nums;
}

/* ── 四步引导：步骤列 + 两张截图并排。── */
.xgj-guide-list {
  margin: 10px 0 12px 18px;
  line-height: 1.9;
  font-size: 13px;
  color: var(--tt2-ink);
}

.xgj-guide-list li {
  margin-bottom: 6px;
}

.xgj-guide-hint {
  color: var(--tt2-dim);
}

.xgj-guide-figs {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}

.xgj-guide-fig {
  margin: 0;
  flex: 1 1 300px;
  min-width: 260px;
}

.xgj-guide-fig img {
  width: 100%;
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
}

.xgj-guide-fig figcaption {
  margin-top: 4px;
  font-size: 12px;
  color: var(--tt2-dim);
}

/* ── 版本声明：琥珀提示条（声明 + 提醒，两档字色）。── */
.xgj-version {
  border-left: 4px solid var(--tt2-tint-amber-line);
  background: var(--tt2-tint-amber-bg);
  line-height: 1.7;
}

.xgj-version p {
  margin: 0;
}

.xgj-version p + p {
  margin-top: 4px;
}

.xgj-version-strong {
  color: var(--tt2-summary-orange);
  font-weight: 600;
}

.xgj-version-dim {
  color: var(--tt2-dim);
}

/* ── 配置说明：正文暗色 + 两条红字说明。── */
.xgj-intro p {
  margin: 0;
  line-height: 1.7;
}

.xgj-intro p + p {
  margin-top: 6px;
}

.xgj-warn-text {
  color: var(--tt2-st-error);
}

/* ── 应用表：长 URL 与备注列。── */
.xgj-binding-table {
  min-width: 1080px;
}

.cv2-table-panel .cv2-table td.xgj-push-cell {
  height: auto;
  padding-top: 8px;
  padding-bottom: 8px;
  white-space: normal;
}

.xgj-push-url {
  word-break: break-all;
  min-width: 0;
  flex: 1;
}

.xgj-push-line { display: flex; align-items: flex-start; gap: 8px; }
.xgj-push-line button { flex: none; opacity: 1; }
.xgj-field-hint { margin: 0; }

.xgj-remark {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.xgj-actions {
  white-space: nowrap;
}

.xgj-actions .cv2-btn--sm + .cv2-btn--sm {
  margin-left: -1px;
}

.xgj-set {
  color: var(--tt2-st-settled);
}

.xgj-unset {
  color: var(--tt2-st-error);
}

/* ── 店铺表：来源未建与三态订购状态（标黄 = 订购异常，琥珀单元格）。── */
.xgj-source-missing {
  color: var(--tt2-st-error);
}

.xgj-sub-warn {
  background: var(--tt2-tint-amber-bg);
  color: var(--tt2-tint-amber);
  border: 1px solid var(--tt2-tint-amber-line);
  padding: 2px 6px;
}

.xgj-sub-unknown {
  color: var(--tt2-dim);
}

.xgj-sub-ok {
  color: var(--tt2-st-settled);
}

/* ── 查询详情弹层：结果两列表（标签列窄、值任意长）。── */
.xgj-query-actions {
  justify-content: flex-start;
  margin-top: 0;
}

.xgj-detail-result {
  margin-top: 12px;
}

.xgj-detail-loading {
  margin: 0;
  font-size: 13px;
  color: var(--tt2-dim);
}

.xgj-detail-table td {
  height: auto;
  padding: 6px 12px;
}

.xgj-detail-label {
  color: var(--tt2-dim);
  word-break: break-all;
}

.xgj-detail-value {
  word-break: break-all;
  white-space: pre-wrap;
}

/* ── 测试连接弹层：店名列表可断行。── */
.xgj-test-names {
  margin: 8px 0 0;
  font-size: 13px;
  line-height: 1.7;
  color: var(--tt2-ink);
  word-break: break-all;
}

/* 弹层表单行距（尺寸非色值）。 */
.cv2-dialog .cv2-form-field {
  grid-template-columns: 104px minmax(0, 1fr);
  gap: 0;
  align-items: stretch;
  margin-bottom: 12px;
  overflow: hidden;
  border-radius: 4px;
}
.cv2-dialog .cv2-form-field .cv2-field-label { display: flex; align-items: center; padding: 0 10px; border: 1px solid var(--tt2-filter-line); border-right: 0; background: var(--tt2-filter-label-bg); color: var(--tt2-dim); font-size: 13px; font-weight: 400; }
.cv2-dialog .cv2-form-field .cv2-input { width: 100%; min-height: 34px; box-sizing: border-box; border-color: var(--tt2-filter-line); border-radius: 0; }
.cv2-dialog .cv2-form-field :deep(.el-input__wrapper) { border-radius: 0; }
.cv2-dialog .cv2-form-field :deep(input::placeholder) { color: var(--tt2-filter-ph); }
.cv2-dialog .cv2-form-field .xgj-field-hint { margin-top: 4px; }
</style>
