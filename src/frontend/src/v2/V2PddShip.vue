<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「其它平台店铺对接」v2（`/v2/pdd-ship`；系统维护批次 B 波，计划 §3 页 7）。
 * 旧页 = island 复刻页 `layui-island/pages/system-pddshipsetting.ts`（对标 `/System/pddshipsetting`）。
 * 重建为炭火页：功能介绍 blockquote → 接口量 → 店铺对接表 8 列（工具条 刷新 / 新增一个店铺对接，
 * 行内 修改 / 删除）→ 对接弹窗（平台 radio / AppId 只读预填 platform-config / AppSecret 行不画 /
 * accessToken 留空不改 / 对应订单来源 / 店铺ID或昵称 / 备注）→ 5 段平台功能设置（照渲染、点击
 * 「尚未开放」，保存以上设置同）。「对应订单来源」判过才带（字典没取到或认不出原值时不发键，
 * 防「保存成功但来源被静默清掉」）；弹窗 :close-on-click-modal="false"（照旧 shadeClose:false，
 * 半填的授权码误关代价高）。平台 AppSecret 一次都不进浏览器；授权码后端只写不读（E-0253），
 * 列渲染「已配置/未配置」。⛔ 不 import island 页模块。
 */
import { computed, onMounted, ref } from 'vue'
import { fetchOrderSources } from '@/api/orderSources'
import {
  createShopBinding,
  deleteShopBinding,
  fetchPlatformConfig,
  fetchShopBindings,
  updateShopBinding,
  type ShopBindingPayload,
  type ShopBindingRow,
} from '@/api/shopBindings'

/* ── 逐字文案与常量（出处 = island 页同语义常量 / 证据件；本地重写不引模块）────── */

const EMPTY_TABLE_TEXT = '暂无数据'
/** 功能介绍 blockquote（证据件 L63-66 逐字）。 */
const INTRO_TITLE = '功能介绍：'
const INTRO_BODY =
  '录单时自动读取拼多多/闲鱼/京东/抖音/快手等店铺的订单价格和买家留言，减少价格输入，也避免出现订单价出错；'
/** 接口量链接（证据件 L70 逐字；剩余调用量我们拿不到，照旧留空）。 */
const PAY_LINK_TEXT = '点击这里购买接口调用量（当前系统接口剩余调用量：，接口读取订单会消耗调用量）'
/** 弹窗内导语与保存钮（证据件 L91 / L169 逐字）。 */
const DIALOG_QUOTE = '请在下面填上对接参数，如何对接请联系我们售后客服'
/** 对接弹窗平台 radio（证据件：只有 5 档；10拼多多/20闲鱼/30抖音/40快手/60小红书）。 */
const PLATFORM_RADIOS = [
  { value: 10, title: '拼多多' },
  { value: 20, title: '闲鱼' },
  { value: 30, title: '抖音' },
  { value: 40, title: '快手' },
  { value: 60, title: '小红书' },
] as const
/** 平台必选（island validateBindingForm 逐字；AppId 只读 ⇒ 那条「必填」刻意不做，照旧）。 */
const PLATFORM_REQUIRED_TEXT = '请选择一个店铺所在的平台'
/** 删除确认（island remove 逐字，按钮同）。 */
const DEL_CONFIRM_TEXT = '是否确认删除这个店铺对接？'

/** 尚未开放统一句式（island notOpenLabel 同式）。 */
function notOpen(label: string): void {
  showToast(`「${label}」尚未开放`)
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

/* ── 提示（页面内自绘 toast；info 1500ms / error 6000ms）────────────────────── */

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

/* ── 店铺对接表（page_size=200 由 api 出口自带；soureName 由来源字典解析）─────── */

const rows = ref<ShopBindingRow[]>([])
const loading = ref(false)
const sourceNames = ref<Map<number, string>>(new Map())
/** 来源字典是否成功取到过（只置真不复位——决定 order_source 发不发键）。 */
let sourcesLoaded = false
/** 平台级 AppId（弹窗只读预填；优先 platform-config，退回列表第一行照旧）。 */
const platformAppId = ref('')

function soureNameOf(row: ShopBindingRow): string {
  if (row.order_source === null) return ''
  return sourceNames.value.get(row.order_source) ?? ''
}

async function reload(): Promise<void> {
  loading.value = true
  try {
    const [page, sourcesPage, config] = await Promise.all([
      fetchShopBindings(),
      // 字典失败不拦页：下拉只剩「不设置」、来源匹配列空（island 同口径降级）。
      fetchOrderSources().catch(() => null),
      // 平台配置只是预填值：失败退回逐行旧路径（island 同口径）。
      fetchPlatformConfig().catch(() => null),
    ])
    if (sourcesPage !== null) {
      const map = new Map<number, string>()
      for (const item of sourcesPage.items) map.set(item.id, item.name)
      sourceNames.value = map
      sourcesLoaded = true
    }
    const configured = config?.platform_app_id ?? ''
    const first = page.items[0]
    if (configured !== '') platformAppId.value = configured
    else if (first) platformAppId.value = first.platform_app_id
    rows.value = page.items
  } catch (e) {
    rows.value = []
    showToast(errText(e), 'error')
  } finally {
    loading.value = false
  }
}

/* ── 新增 / 编辑对接弹窗（order_source 判过才带；空 access_token = 不改）──────── */

type BindingForm = {
  id: number | null
  platform: number | null
  appId: string
  accessToken: string
  shopName: string
  remark: string
  orderSource: string
  /** 打开弹窗时那一行的原 order_source（判「认不出 ⇒ 不发键」用）。 */
  existingSource: number | null
}
const bindingDlg = ref<BindingForm | null>(null)
const bindingSaving = ref(false)

function openBindingForm(row: ShopBindingRow | null): void {
  bindingDlg.value = {
    id: row?.id ?? null,
    platform: row != null ? row.platform : null,
    appId: platformAppId.value,
    // 授权码没有回显（接口只给布尔）⇒ 新增与编辑都从空框开始；留空保存 = 不改。
    accessToken: '',
    shopName: row?.shop_name ?? '',
    remark: row?.remark ?? '',
    orderSource: row?.order_source != null ? String(row.order_source) : '',
    existingSource: row?.order_source ?? null,
  }
}

/**
 * 保存时 `order_source` 该发什么（island resolveOrderSource 同式重写，顺序是语义的一部分）：
 * 字典没取到 ⇒ 不带键；用户选了东西 ⇒ 以选择为准；没动而原值认不出 ⇒ 不带键（保留原值）；
 * 其余（含主动选「不设置」）⇒ 显式发 null 清空。
 */
function resolveOrderSource(selected: string): number | null | undefined {
  if (!sourcesLoaded) return undefined
  if (selected !== '') {
    const picked = Number(selected)
    return Number.isInteger(picked) && picked > 0 ? picked : undefined
  }
  if (bindingDlg.value?.existingSource != null && !sourceNames.value.has(bindingDlg.value.existingSource)) {
    return undefined
  }
  return null
}

async function submitBinding(): Promise<void> {
  const dlg = bindingDlg.value
  if (dlg === null || bindingSaving.value) return
  if (dlg.platform === null) {
    showToast(PLATFORM_REQUIRED_TEXT)
    return
  }
  const payload: ShopBindingPayload = {
    platform: dlg.platform,
    shop_name: dlg.shopName,
    // 空串在后端 PATCH 是「不改」：只改备注不会清掉授权码。
    access_token: dlg.accessToken,
    remark: dlg.remark,
  }
  const orderSource = resolveOrderSource(dlg.orderSource)
  if (orderSource !== undefined) payload.order_source = orderSource
  bindingSaving.value = true
  try {
    if (dlg.id !== null) await updateShopBinding(dlg.id, payload)
    else await createShopBinding(payload)
  } catch (e) {
    bindingSaving.value = false
    showToast(errText(e), 'error')
    return
  }
  bindingSaving.value = false
  bindingDlg.value = null
  showToast('保存成功')
  void reload()
}

/* ── 删除对接（确认语与按钮逐字）──────────────────────────────────────────── */

const delDlg = ref<ShopBindingRow | null>(null)

async function confirmDel(): Promise<void> {
  const row = delDlg.value
  if (row === null) return
  delDlg.value = null
  try {
    await deleteShopBinding(row.id)
    showToast('删除成功')
    void reload()
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

/* ── 5 段平台功能设置（照渲染；radio 点击「尚未开放」，文本输入不拦照旧）────────
 *
 * ⚠️ 各段里 display:none 的「自动创建订单」子块不渲染：开关被拦截永不显示（旧页同样
 * 展示不出来——capture 拦截截走了 layui 的切换监听）。 */

type NotOpenItem =
  | { kind: 'radios'; text: string; options: readonly { value: string; label: string; checked: boolean }[] }
  | { kind: 'text'; text: string }

const SWITCH_ITEM: NotOpenItem = {
  kind: 'radios',
  text: '功能总开关(如果关闭，则该对接功能将全部失败)',
  options: [
    { value: 'N', label: '关闭', checked: true },
    { value: 'Y', label: '打开', checked: false },
  ],
}
const AUTO_CREATE_SWITCH_ITEM: NotOpenItem = {
  kind: 'radios',
  text: '买家付款后是否需要自动创建系统订单（创建的订单包含单号、价格、留言），注意：该功能需要联系我们技术人确认是否可用',
  options: [
    { value: 'N', label: '否', checked: true },
    { value: 'Y', label: '是', checked: false },
  ],
}
const PDD_AUTO_CREATE_ITEM: NotOpenItem = {
  kind: 'radios',
  text: '买家付款后是否需要自动创建系统订单（创建的订单包含拼多多单号、价格、留言），注意：该功能需要联系我们技术人确认是否可用',
  options: [
    { value: 'N', label: '否', checked: true },
    { value: 'Y', label: '是', checked: false },
  ],
}

const NOT_OPEN_SECTIONS: { legend: string; items: readonly NotOpenItem[] }[] = [
  {
    legend: '闲鱼店铺功能设置',
    items: [
      SWITCH_ITEM,
      {
        kind: 'radios',
        text: '闲鱼的订单改成什么状态下能自动发货(默认不发货)',
        options: [
          { value: 'N', label: '不发', checked: false },
          { value: 'D', label: '待验收', checked: false },
          { value: 'Y', label: '已完成', checked: true },
        ],
      },
      {
        kind: 'radios',
        text: '闲鱼的订单改成已退单时是否需要同步执行闲鱼同意退款--(默认不执行|开启前需要确认授权页面的权限是否打开)',
        options: [
          { value: 'N', label: '不执行', checked: true },
          { value: 'Y', label: '执行', checked: false },
        ],
      },
      { kind: 'text', text: '闲鱼订单发货成功后发送闲鱼消息给买家(下面留空代表不发，要发消息请在下面输入)' },
      { kind: 'text', text: '闲鱼订单状态改成已退单时发送闲鱼消息给买家(下面留空代表不发，要发消息请在下面输入)' },
    ],
  },
  {
    legend: '拼多多功能设置',
    items: [SWITCH_ITEM, PDD_AUTO_CREATE_ITEM,
      { kind: 'text', text: '拼多多订单状态改撤销中时发送消息给买家(下面留空代表不发，要发消息请在下面输入)，注意：需要开拼多多助手' },
      { kind: 'text', text: '拼多多订单状态改已完成时发送消息给买家(下面留空代表不发，要发消息请在下面输入)，注意：需要开拼多多助手，每笔订单30天内只能发送两条消息' },
    ],
  },
  {
    legend: '抖音店铺功能设置',
    items: [SWITCH_ITEM, AUTO_CREATE_SWITCH_ITEM],
  },
  {
    legend: '快手店铺功能设置',
    items: [
      SWITCH_ITEM,
      {
        kind: 'radios',
        text: '快手的订单是否改成已完成时就自动发货(默认不发货)',
        options: [
          { value: 'N', label: '不发', checked: true },
          { value: 'Y', label: '发货', checked: false },
        ],
      },
      AUTO_CREATE_SWITCH_ITEM,
    ],
  },
  {
    legend: '小红书店铺功能设置',
    items: [
      SWITCH_ITEM,
      {
        kind: 'radios',
        text: '订单改成已完成时是否自动发货(默认不发货)',
        options: [
          { value: 'N', label: '不发', checked: true },
          { value: 'Y', label: '发货', checked: false },
        ],
      },
      AUTO_CREATE_SWITCH_ITEM,
    ],
  },
]

/** 空态 colspan = 8 列。 */
const COLSPAN_ALL = 8

/** 来源字典候选（弹窗下拉；首项「不设置」照证据件）。 */
const sourceOptions = computed(() => Array.from(sourceNames.value, ([id, name]) => ({ id, name })))

onMounted(() => {
  void reload()
})
</script>

<template>
  <div class="cv2-page" data-v2-page="pdd-ship">
    <!-- 功能介绍 blockquote（逐字） -->
    <p class="cv2-note pd-intro" data-v2="intro">
      {{ INTRO_TITLE }}{{ INTRO_BODY }}
    </p>

    <!-- 接口量（链接点击「尚未开放」，照旧 fail-open） -->
    <section class="cv2-panel pd-fieldset" aria-label="接口量" data-v2="pay-panel">
      <p class="pd-legend" data-v2="pay-legend">接口量</p>
      <a href="#" class="pd-pay-link" data-v2="pay-link" @click.prevent="notOpen('购买接口调用量')">{{ PAY_LINK_TEXT }}</a>
    </section>

    <!-- 店铺对接设置：工具条 + 8 列表 -->
    <section class="cv2-panel pd-fieldset" aria-label="店铺对接设置" data-v2="binding-panel">
      <p class="pd-legend" data-v2="binding-legend">店铺对接设置</p>
      <div class="cv2-toolbar" data-v2="toolbar">
        <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--find" data-v2="refresh" :disabled="loading" @click="reload()">刷新</button>
        <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="add" @click="openBindingForm(null)">新增一个店铺对接</button>
      </div>
      <table class="cv2-table" data-v2="table">
        <colgroup>
          <col style="width: 100px" />
          <col style="width: 200px" />
          <col style="width: 200px" />
          <col style="width: 170px" />
          <col style="width: 250px" />
          <col style="width: 355px" />
          <col />
          <col style="width: 160px" />
        </colgroup>
        <thead>
          <tr>
            <th class="cv2-th-center">平台</th>
            <th class="cv2-th-center">店铺ID或昵称</th>
            <th class="cv2-th-center">订单来源匹配</th>
            <th>AppId</th>
            <th>AppSecret</th>
            <th>AccessToken</th>
            <th>备注说明</th>
            <th class="cv2-th-center">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id" class="cv2-row" :data-v2-row="row.id">
            <td class="cv2-td-center">{{ row.platform_label }}</td>
            <td class="cv2-td-center" :data-v2-shop-name="row.id">{{ row.shop_name }}</td>
            <td class="cv2-td-center" :data-v2-source="row.id">{{ soureNameOf(row) }}</td>
            <td class="cv2-num">{{ row.platform_app_id }}</td>
            <!-- 平台密钥一次都不进浏览器：AppSecret 列恒空（照旧差异 #2）。 -->
            <td />
            <!-- E-0253：后端不下发授权码字面值 ⇒ 渲染「已配置/未配置」。 -->
            <td :data-v2-token-set="row.access_token_set">{{ row.access_token_set ? '已配置' : '未配置' }}</td>
            <td class="pd-remark" :title="row.remark">{{ row.remark }}</td>
            <td class="cv2-td-center pd-actions">
              <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--sm cv2-btn--commit" :data-v2-edit="row.id" @click="openBindingForm(row)">修改</button>
              <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--sm" :data-v2-del="row.id" @click="delDlg = row">删除</button>
            </td>
          </tr>
          <tr v-if="rows.length === 0">
            <td :colspan="COLSPAN_ALL" class="cv2-empty" data-v2="empty">
              <template v-if="loading">正在加载…</template>
              <template v-else>{{ EMPTY_TABLE_TEXT }}</template>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 5 段平台功能设置：照渲染、radio 点击「尚未开放」；文本输入不拦照旧 -->
    <section
      v-for="section in NOT_OPEN_SECTIONS"
      :key="section.legend"
      class="cv2-panel pd-fieldset"
      :aria-label="section.legend"
      :data-v2="`settings-${section.legend}`"
    >
      <p class="pd-legend">{{ section.legend }}</p>
      <div v-for="(item, index) in section.items" :key="index" class="pd-item">
        <p class="pd-item-text">{{ item.text }}</p>
        <div v-if="item.kind === 'radios'" class="pd-item-radios">
          <label v-for="o in item.options" :key="o.value" class="cv2-radio">
            <input type="radio" :name="`pd-${section.legend}-${index}`" :checked="o.checked" @click.prevent="notOpen(item.text)" />
            {{ o.label }}
          </label>
        </div>
        <el-input v-else :maxlength="500" :data-v2-decorative-input="index" />
      </div>
    </section>
    <div class="pd-saveall">
      <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="save-all" @click="notOpen('保存以上设置')">保存以上设置</button>
    </div>

    <!-- 对接弹窗（半填误关代价高 ⇒ 不点遮罩关窗）；AppSecret 行不画（平台密钥不进浏览器） -->
    <el-dialog
      :model-value="bindingDlg !== null"
      :title="bindingDlg !== null && bindingDlg.id !== null ? '编辑对接' : '新增一个店铺对接'"
      class="cv2-dialog cv2-dialog--lg"
      destroy-on-close
      :close-on-click-modal="false"
      data-v2="dlg-binding"
      @update:model-value="bindingDlg = null"
    >
      <template v-if="bindingDlg !== null">
        <p class="cv2-note pd-dlg-quote" data-v2="dlg-quote">{{ DIALOG_QUOTE }}</p>
        <div class="cv2-field">
          <span class="cv2-field-label">哪个平台的店铺</span>
          <div class="cv2-dlg-radios pd-dlg-radios" data-v2="dlg-platform">
            <label v-for="o in PLATFORM_RADIOS" :key="o.value" class="cv2-radio">
              <input v-model="bindingDlg.platform" type="radio" name="v2-pd-platform" :value="o.value" />
              {{ o.title }}
            </label>
          </div>
        </div>
        <div class="cv2-field">
          <span class="cv2-field-label">AppId</span>
          <!-- 平台统一值，只读预填（零绑定时来自 platform-config 端点）。 -->
          <el-input v-model="bindingDlg.appId" readonly :maxlength="20" placeholder="请填写AppID" data-v2="dlg-appid" />
        </div>
        <div class="cv2-field">
          <span class="cv2-field-label">授权码</span>
          <div class="pd-token-row">
            <el-input v-model="bindingDlg.accessToken" :maxlength="100" placeholder="请将授权码复制到此处" data-v2="dlg-token" />
            <button type="button" class="cv2-btn cv2-btn--commit" data-v2="get-auth" @click="notOpen('获取授权')">获取授权</button>
          </div>
        </div>
        <div class="cv2-field">
          <span class="cv2-field-label">店铺或商家ID</span>
          <el-input
            v-model="bindingDlg.shopName"
            :maxlength="50"
            placeholder="如果您对接了多个店铺并启用了下单推送，请在这里填上该店铺的ID"
            data-v2="dlg-shop-name"
          />
        </div>
        <div class="cv2-field">
          <span class="cv2-field-label">备注说明</span>
          <el-input v-model="bindingDlg.remark" :maxlength="50" data-v2="dlg-remark" />
        </div>
        <div class="cv2-field">
          <span class="cv2-field-label">对应订单来源</span>
          <select v-model="bindingDlg.orderSource" class="cv2-input pd-source-select" data-v2="dlg-source">
            <option value="">不设置</option>
            <option v-for="s in sourceOptions" :key="s.id" :value="String(s.id)">{{ s.name }}</option>
          </select>
        </div>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="binding-save" :disabled="bindingSaving" @click="void submitBinding()">保存设置</button>
        </div>
      </template>
    </el-dialog>

    <!-- 删除确认（文案与按钮逐字） -->
    <el-dialog
      :model-value="delDlg !== null"
      title="提示"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-del"
      @update:model-value="delDlg = null"
    >
      <p class="cv2-dlg-text">{{ DEL_CONFIRM_TEXT }}</p>
      <div class="cv2-dlg-actions">
        <button type="button" class="cv2-btn cv2-btn--quiet" @click="delDlg = null">点错了</button>
        <button type="button" class="cv2-btn cv2-btn--danger" data-v2="del-ok" @click="void confirmDel()">确认删除</button>
      </div>
    </el-dialog>

    <!-- 炭火 toast -->
    <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
      {{ toast.text }}
    </div>
  </div>
</template>

<style scoped>
/* 本页新增的形状收在这里；色一律走 carbon.css 的 .ttv2 token（类或 var 引用，⛔ 不写裸色值）。 */

.cv2-num {
  font-family: var(--tt2-mono);
  font-variant-numeric: tabular-nums;
}

/* 功能介绍 blockquote（左粗边说明条 = cv2-note 档）。 */
.pd-intro {
  margin: 0 0 12px;
}

/* 段落 panel：legend 即面板标题（对标 fieldset/legend 的炭火版）。 */
.pd-fieldset {
  margin-bottom: 12px;
}

.pd-legend {
  margin: 0 0 8px;
  font-size: 14px;
  font-weight: 600;
}

/* 接口量链接 = 对标橙（--tt2-summary-orange 是该字面色的 token 同值档）。 */
.pd-pay-link {
  color: var(--tt2-summary-orange);
  font-size: 14px;
  text-decoration: none;
}

.pd-pay-link:hover {
  text-decoration: underline;
}

.pd-remark {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pd-actions {
  white-space: nowrap;
}

.pd-actions .cv2-btn--sm + .cv2-btn--sm {
  margin-left: -1px;
}

/* 表格滚动（段落较多，主表独立滚动）。 */
.pd-fieldset .cv2-table {
  min-width: 900px;
}

/* ── 5 段功能设置：条目 = 说明文字 + radio/文本。── */
.pd-item {
  padding: 8px 0;
  border-top: 1px solid var(--tt2-line);
}

.pd-item:first-of-type {
  border-top: none;
}

.pd-item-text {
  margin: 0 0 6px;
  font-size: 13px;
  color: var(--tt2-ink);
}

.pd-item-radios {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 20px;
  padding-left: 10px;
}

.pd-saveall {
  text-align: center;
  padding: 4px 0 12px;
}

/* ── 对接弹窗。── */
.pd-dlg-quote {
  margin: 0 0 12px;
}

.pd-dlg-radios {
  flex-wrap: wrap;
  row-gap: 10px;
}

.pd-dlg-radios .cv2-radio {
  white-space: nowrap;
}

.pd-token-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.pd-token-row .el-input {
  flex: 1;
}

.pd-source-select {
  width: 240px;
  padding: 6px 10px;
}

/* 弹层表单行距（尺寸非色值）。 */
.cv2-dialog .cv2-field {
  margin-bottom: 12px;
}
</style>
