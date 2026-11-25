<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/** 订单来源管理与店铺归属。 */
import { computed, onMounted, ref } from 'vue'
import {
  createOrderSource,
  createOrderSourceGroup,
  fetchAllOrderSourceGroups,
  fetchAllOrderSources,
  fetchOrderSources,
  removeOrderSource,
  removeOrderSourceGroup,
  updateOrderSource,
  updateOrderSourceGroup,
  type OrderSourceGroupChoice,
  type OrderSourceGroupRow,
  type OrderSourceRow,
} from '@/api/orderSources'
import { fetchXgjShopsList, type XgjShopsListRow } from '@/api/xgjBindings'

/* ── 逐字文案与常量（出处 = island 页同语义常量 / 证据件；本地重写不引模块）────── */

/** 弹窗 9 个「哪种店铺」radio（value/title 逐字照证据件 L125-133）。 */
const SHOP_TYPE_RADIOS = [
  { value: 0, title: '淘宝/天猫' },
  { value: 10, title: '拼多多' },
  { value: 15, title: '京东' },
  { value: 20, title: '闲鱼' },
  { value: 30, title: '抖音' },
  { value: 40, title: '快手' },
  { value: 50, title: '唯品会' },
  { value: 60, title: '小红书' },
  { value: 70, title: '其它平台' },
] as const

/**
 * 列表「哪种店铺」列的文案（island shopTypeLabel 同式）。
 * ⚠️ `0` 落到 default：**非店铺/淘宝/天猫**，与弹窗的 `淘宝/天猫` **刻意不同**（照旧不统一）。
 */
function shopTypeLabel(value: number): string {
  switch (value) {
    case 10: return '拼多多'
    case 15: return '京东'
    case 20: return '闲鱼'
    case 30: return '抖音'
    case 40: return '快手'
    case 50: return '唯品会'
    case 60: return '小红书'
    case 70: return '其它平台'
    default: return '非店铺/淘宝/天猫'
  }
}

/** 排序前端容错（island parseOrderBy 同式）：只接受非负整数，否则静默按 0（后端非整数回 1000，刻意不同照旧）。 */
function parseOrderBy(raw: string): number {
  const text = raw.trim()
  return /^\+?\d+$/.test(text) ? Number(text) : 0
}

/** 编辑弹窗两条校验（island validateSoureForm 逐字）；未选店铺/排序非法的两条容错同式。 */
function validateSoureForm(input: { name: string; orderByRaw: string }): { error: string; orderBy: number } {
  if (input.name.trim() === '') return { error: '请输入来源名称', orderBy: 0 }
  if (input.orderByRaw.trim() === '') return { error: '请输入排序数值，可以写0', orderBy: 0 }
  return { error: '', orderBy: parseOrderBy(input.orderByRaw) }
}

/** 短信签名格的 title（证据件 lay-tips 原文）。 */
const SMS_SIGN_TIP =
  '如果系统有发短信，则该来源的订单短信发出去的签名是【】，单击可以修改(前提条件是本系统已开通自定义短信签名服务)'
/** 「订单是否可发送短信」列头帮助图标文案（证据件 title 逐字，含 HTML 的纯文本化）。 */
const SMS_HELP_TEXT =
  '正常(绿色)：代表该来源的订单会根据系统设置的短信业务来发送短信通知；禁用(灰色)：表示该来源的订单，将禁止发送任何短信通知'
/** 尚未开放两列（island onSmsClick 逐字句式）。 */
function notOpen(label: string): void {
  showToast(`「${label}」尚未开放`)
}
/** 删除分组确认文案：N 来自数据 source_count，拿不到或 0 不追加（island groupDeleteConfirmText 同式）。 */
function groupDeleteConfirmText(name: string, sourceCount?: number): string {
  const base = `确定删除分组「${name}」吗？`
  if (typeof sourceCount !== 'number' || sourceCount === 0) return base
  return `${base}该分组下的 ${sourceCount} 个来源将移出这个分组。`
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

/* ── 提示（页面内自绘 toast；info 1500ms / error 6000ms，V2UserList 先例）────── */

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

/* ── 来源列表（fetchOrderSources 自带 page=1&page_size=200，无分页控件照旧）───── */

const rows = ref<OrderSourceRow[]>([])
const loading = ref(false)

async function reloadSources(): Promise<void> {
  loading.value = true
  try {
    const page = await fetchOrderSources()
    rows.value = page.items
  } catch (e) {
    rows.value = []
    showToast(errText(e), 'error')
  } finally {
    loading.value = false
  }
}

async function reload(): Promise<void> {
  await Promise.all([reloadSources(), reloadGroupsDict()])
}

/* ── 分组字典（弹层下拉 / 分组管理 / 店铺归属「订单分组」共用一份）────────────── */

const groups = ref<OrderSourceGroupRow[]>([])

async function reloadGroupsDict(): Promise<void> {
  try {
    groups.value = await fetchAllOrderSourceGroups()
  } catch {
    // 取不到不拦页：下拉只剩「未分组」，页面照常可用（island 同口径）。
  }
}

/* ── 新增 / 编辑来源弹窗（先预填再开；未选店铺按 0、排序非法按 0 照旧容错）────── */

type SoureForm = {
  id: number | null
  name: string
  remark: string
  orderByRaw: string
  shopType: number | null
  groupIds: number[]
  initialGroups: OrderSourceGroupChoice[]
}
const soureDlg = ref<SoureForm | null>(null)
const soureSaving = ref(false)

function openSoureForm(row: OrderSourceRow | null): void {
  const initialGroups = row?.groups ?? (
    row?.group != null ? [{ id: row.group, name: row.group_name }] : []
  )
  soureDlg.value = {
    id: row?.id ?? null,
    name: row?.name ?? '',
    remark: row?.remark ?? '',
    orderByRaw: row != null ? String(row.order_by) : '0',
    shopType: row != null ? row.shop_type : 0,
    groupIds: initialGroups.map((group) => group.id),
    initialGroups,
  }
}

async function submitSoure(): Promise<void> {
  const dlg = soureDlg.value
  if (dlg === null || soureSaving.value) return
  const validated = validateSoureForm({ name: dlg.name, orderByRaw: dlg.orderByRaw })
  if (validated.error !== '') {
    showToast(validated.error)
    return
  }
  soureSaving.value = true
  const initialIds = new Set(dlg.initialGroups.map((group) => group.id))
  const groupsChanged = dlg.groupIds.length !== initialIds.size || dlg.groupIds.some((id) => !initialIds.has(id))
  const payload = {
    name: dlg.name,
    remark: dlg.remark,
    order_by: validated.orderBy,
    shop_type: dlg.shopType ?? 0,
    group_ids: dlg.id === null || groupsChanged ? dlg.groupIds : undefined,
  }
  try {
    if (dlg.id !== null) await updateOrderSource(dlg.id, payload)
    else await createOrderSource(payload)
  } catch (e) {
    soureSaving.value = false
    showToast(errText(e), 'error')
    return
  }
  soureSaving.value = false
  soureDlg.value = null
  showToast('保存成功')
  await Promise.all([reload(), reloadShops()])
}

/* ── 删除来源 ─────────────────────────────────────────────────────────────── */

const delSoureDlg = ref<OrderSourceRow | null>(null)

async function confirmDelSoure(): Promise<void> {
  const row = delSoureDlg.value
  if (row === null) return
  delSoureDlg.value = null
  try {
    await removeOrderSource(row.id)
    showToast('删除成功')
    await Promise.all([reload(), reloadShops()])
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

/* ── 分组管理弹窗（新增行 / 改名 prompt / 删除确认）──────────────────────────── */

const GROUP_ORDER_BY_DEFAULT = '0'
const groupDlg = ref(false)
const groupNewName = ref('')
const groupNewOrderBy = ref(GROUP_ORDER_BY_DEFAULT)
const groupNewSourceIds = ref<number[]>([])
const groupSourceOptions = ref<OrderSourceRow[]>([])
const groupSourcesLoading = ref(false)
const groupSaving = ref(false)
const renameDlg = ref<{ id: number; name: string } | null>(null)
const delGroupDlg = ref<OrderSourceGroupRow | null>(null)

async function openGroupManager(): Promise<void> {
  groupNewName.value = ''
  groupNewOrderBy.value = GROUP_ORDER_BY_DEFAULT
  groupNewSourceIds.value = []
  groupDlg.value = true
  groupSourcesLoading.value = true
  try {
    groupSourceOptions.value = await fetchAllOrderSources()
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    groupSourcesLoading.value = false
  }
}

async function addGroup(): Promise<void> {
  if (groupSaving.value) return
  const name = groupNewName.value.trim()
  if (name === '') {
    showToast('请输入分组名称')
    return
  }
  groupSaving.value = true
  try {
    await createOrderSourceGroup({
      name,
      order_by: parseOrderBy(groupNewOrderBy.value),
      source_ids: groupNewSourceIds.value,
    })
    groupNewName.value = ''
    groupNewOrderBy.value = GROUP_ORDER_BY_DEFAULT
    groupNewSourceIds.value = []
    await Promise.all([reload(), reloadShops()])
    showToast('保存成功')
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    groupSaving.value = false
  }
}

function openRename(group: OrderSourceGroupRow): void {
  renameDlg.value = { id: group.id, name: group.name }
}

async function submitRename(): Promise<void> {
  const dlg = renameDlg.value
  if (dlg === null) return
  const name = dlg.name.trim()
  if (name === '') {
    showToast('请输入分组名称')
    return
  }
  try {
    await updateOrderSourceGroup(dlg.id, { name })
  } catch (e) {
    showToast(errText(e), 'error')
    return
  }
  renameDlg.value = null
  await Promise.all([reload(), reloadShops()])
  showToast('保存成功')
}

async function confirmDelGroup(): Promise<void> {
  const group = delGroupDlg.value
  if (group === null) return
  delGroupDlg.value = null
  try {
    await removeOrderSourceGroup(group.id)
    showToast('删除成功')
    // 删掉的正是「订单分组」当前选中项 ⇒ 先复位再查，不拿死 id 去筛空表。
    if (shopsGroup.value === group.id) shopsGroup.value = null
    await Promise.all([reload(), reloadShops()])
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

/* ── 店铺归属区块（同页独立 panel；没选 ⇒ 不发键；重填保留已选）──────────────── */

const shopsRows = ref<XgjShopsListRow[]>([])
const shopsSource = ref<number | null>(null)
const shopsGroup = ref<number | null>(null)

async function reloadShops(): Promise<void> {
  const params: { order_source?: number; source_group?: number } = {}
  // el-select 空档是 null；⛔ 不送空串（后端把空串判非法整数 ⇒ 1000）。
  if (shopsSource.value !== null) params.order_source = shopsSource.value
  if (shopsGroup.value !== null) params.source_group = shopsGroup.value
  try {
    const page = await fetchXgjShopsList(params)
    shopsRows.value = page.items
  } catch (e) {
    shopsRows.value = []
    showToast(errText(e), 'error')
  }
}

function onShopsSourceChange(): void {
  void reloadShops()
}

function onShopsGroupChange(): void {
  void reloadShops()
}

function groupNamesOf(row: OrderSourceRow | XgjShopsListRow): string {
  const names = row.groups?.map((group) => group.name).join('、') ?? row.group_name
  return names || '未分组'
}

const soureDialogGroupOptions = computed(() => {
  // 保留分页或读取失败时字典中缺失的已有选项。
  const options = new Map(soureDlg.value?.initialGroups.map((group) => [group.id, group]))
  for (const group of groups.value) options.set(group.id, group)
  return Array.from(options.values())
})

/** 空态 colspan = 8 列。 */
const COLSPAN_ALL = 8
const SHOPS_COLSPAN = 5

onMounted(() => {
  void reload()
  void reloadShops()
})
</script>

<template>
  <div class="cv2-page" data-v2-page="order-sources">
    <!-- 工具条 3 颗（照旧页 toolbarDemo） -->
    <div class="cv2-toolbar" data-v2="toolbar">
      <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--find" data-v2="refresh" :disabled="loading" @click="reload()">刷新列表</button>
      <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="add" @click="openSoureForm(null)">新增一个来源</button>
      <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="groups" @click="openGroupManager">分组管理</button>
    </div>

    <!-- 来源表 8 列（列头/宽度照旧页 COLS；来源名称/说明蓝链外观、无点击行为照旧） -->
    <section class="cv2-panel cv2-table-panel" aria-label="订单来源列表">
      <table class="cv2-table">
        <colgroup>
          <col style="width: 130px" />
          <col />
          <col />
          <col style="width: 115px" />
          <col style="width: 200px" />
          <col style="width: 180px" />
          <col style="width: 160px" />
          <col style="width: 160px" />
        </colgroup>
        <thead>
          <tr>
            <th>来源名称</th>
            <th>来源说明</th>
            <th>公司店铺分组</th>
            <th class="cv2-th-center">哪种店铺</th>
            <th>短信签名</th>
            <th class="cv2-th-center os-th-sms">
              订单是否可发送短信
              <span class="os-help" :title="SMS_HELP_TEXT" data-v2="sms-help">?</span>
            </th>
            <th class="cv2-th-center">排序(数值越大越排前)</th>
            <th class="cv2-th-center">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id" class="cv2-row" :data-v2-row="row.id">
            <td><span class="cv2-order-link" :title="row.name" :data-v2-name="row.id">{{ row.name }}</span></td>
            <td class="os-remark" :title="row.remark">{{ row.remark }}</td>
            <td :data-v2-group-name="row.id">{{ groupNamesOf(row) }}</td>
            <td class="cv2-td-center" :data-v2-shop-type="row.id">{{ shopTypeLabel(row.shop_type) }}</td>
            <td>
              <a
                href="#"
                class="cv2-order-link os-sms-sign"
                :title="SMS_SIGN_TIP"
                :data-v2-sms-sign="row.id"
                @click.prevent="notOpen('短信签名')"
              >&nbsp;</a>
            </td>
            <td class="cv2-td-center">
              <button
                type="button"
                class="os-sms-switch"
                title="禁用"
                :data-v2-sms-switch="row.id"
                @click="notOpen('订单是否可发送短信')"
              ><i class="os-sms-switch-dot" aria-hidden="true"></i></button>
            </td>
            <td class="cv2-td-center cv2-num">{{ row.order_by }}</td>
            <td class="cv2-td-center os-actions">
              <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--sm cv2-btn--commit" :data-v2-edit="row.id" @click="openSoureForm(row)">编辑</button>
              <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--sm" :data-v2-del="row.id" @click="delSoureDlg = row">删除</button>
            </td>
          </tr>
          <tr v-if="rows.length === 0">
            <td :colspan="COLSPAN_ALL" class="cv2-empty" data-v2="empty">
              <template v-if="loading">正在加载…</template>
              <template v-else>暂无数据</template>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 店铺归属：同页独立区块（对标无此区块，照 island §3.4 形态）；没选不发键 -->
    <section class="cv2-panel os-shops" aria-label="店铺归属" data-v2="shops">
      <p class="cv2-panel-title" data-v2="shops-title">店铺归属</p>
      <div class="os-shops-filters">
        <label class="os-shops-field">
          <span class="os-shops-label">订单来源</span>
          <select v-model="shopsSource" class="cv2-input os-shops-select" data-v2="shops-source" @change="onShopsSourceChange">
            <option :value="null">全部来源</option>
            <option v-for="s in rows" :key="s.id" :value="s.id">{{ s.name }}</option>
          </select>
        </label>
        <label class="os-shops-field">
          <span class="os-shops-label">订单分组</span>
          <select v-model="shopsGroup" class="cv2-input os-shops-select" data-v2="shops-group" @change="onShopsGroupChange">
            <option :value="null">全部分组</option>
            <option v-for="g in groups" :key="g.id" :value="g.id">{{ g.name }}</option>
          </select>
        </label>
      </div>
      <table class="cv2-table" data-v2="shops-table">
        <colgroup>
          <col style="width: 220px" />
          <col style="width: 140px" />
          <col style="width: 160px" />
          <col style="width: 160px" />
          <col style="width: 160px" />
        </colgroup>
        <thead>
          <tr>
            <th>店铺名称</th>
            <th class="cv2-th-center">会员名</th>
            <th class="cv2-th-center">订单来源</th>
            <th class="cv2-th-center">公司店铺分组</th>
            <th class="cv2-th-center">所属应用</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="shop in shopsRows" :key="shop.id" class="cv2-row" :data-v2-shop-row="shop.id">
            <td>{{ shop.shop_name }}</td>
            <td class="cv2-td-center">{{ shop.user_name }}</td>
            <td class="cv2-td-center">{{ shop.order_source_name }}</td>
            <td class="cv2-td-center" :data-v2-shop-group="shop.id">{{ groupNamesOf(shop) }}</td>
            <td class="cv2-td-center">{{ shop.binding_name }}</td>
          </tr>
          <tr v-if="shopsRows.length === 0">
            <td :colspan="SHOPS_COLSPAN" class="cv2-empty" data-v2="shops-empty">暂无店铺</td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 新增 / 编辑来源弹窗（标题照旧；未选店铺按 0、排序非法按 0 的容错在提交里） -->
    <el-dialog
      :model-value="soureDlg !== null"
      :title="soureDlg !== null && soureDlg.id !== null ? '编辑来源' : '新增一个来源'"
      class="cv2-dialog cv2-dialog--lg"
      destroy-on-close
      data-v2="dlg-soure"
      @update:model-value="soureDlg = null"
    >
      <template v-if="soureDlg !== null">
        <div class="cv2-field">
          <span class="cv2-field-label">来源名称</span>
          <el-input v-model="soureDlg.name" :maxlength="10" placeholder="请输入来源名称" data-v2="soure-name" />
        </div>
        <div class="cv2-field">
          <span class="cv2-field-label">来源描述</span>
          <el-input v-model="soureDlg.remark" :maxlength="50" placeholder="对该来源的描述" data-v2="soure-remark" />
        </div>
        <div class="cv2-field">
          <span class="cv2-field-label">排序数值</span>
          <el-input v-model="soureDlg.orderByRaw" :maxlength="3" placeholder="排序数值，整数，数值越大越排前面" data-v2="soure-orderby" />
        </div>
        <div class="cv2-field">
          <span class="cv2-field-label">公司店铺分组</span>
          <el-select
            v-model="soureDlg.groupIds"
            multiple
            filterable
            clearable
            placeholder="选择分组"
            aria-label="公司店铺分组"
            class="os-group-select"
            data-v2="soure-group"
          >
            <el-option v-for="group in soureDialogGroupOptions" :key="group.id" :value="group.id" :label="group.name" />
          </el-select>
        </div>
        <div class="cv2-field">
          <span class="cv2-field-label">哪种店铺</span>
          <div class="cv2-dlg-radios os-radios" data-v2="soure-shoptype">
            <label v-for="o in SHOP_TYPE_RADIOS" :key="o.value" class="cv2-radio">
              <input v-model="soureDlg.shopType" type="radio" name="v2-soure-shoptype" :value="o.value" />
              {{ o.title }}
            </label>
          </div>
        </div>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="soure-save" :disabled="soureSaving" @click="void submitSoure()">保存设定</button>
        </div>
      </template>
    </el-dialog>

    <!-- 分组管理弹窗（新增行 + 每行改名/删除；N 来自 source_count） -->
    <el-dialog
      v-model="groupDlg"
      title="分组管理"
      class="cv2-dialog cv2-dialog--md"
      destroy-on-close
      data-v2="dlg-groups"
    >
      <div class="os-group-add">
        <el-input v-model="groupNewName" :maxlength="20" placeholder="分组名称（≤20 字）" class="os-group-add-name" data-v2="group-name" />
        <el-input v-model="groupNewOrderBy" :maxlength="3" placeholder="排序" class="os-group-add-order" data-v2="group-orderby" />
        <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="group-add" :disabled="groupSaving" @click="void addGroup()">新增</button>
      </div>
      <div class="cv2-field os-group-sources">
        <span class="cv2-field-label">添加已有来源</span>
        <el-select
          v-model="groupNewSourceIds"
          :loading="groupSourcesLoading"
          multiple
          filterable
          clearable
          placeholder="选择订单来源（可选）"
          aria-label="添加已有来源"
          class="os-group-select"
          data-v2="group-sources"
        >
          <el-option v-for="row in groupSourceOptions" :key="row.id" :value="row.id" :label="row.name" />
        </el-select>
      </div>
      <div v-if="groups.length === 0" class="cv2-empty os-group-empty" data-v2="groups-empty">暂无分组</div>
      <div v-else class="os-group-list">
        <div v-for="g in groups" :key="g.id" class="os-group-item" :data-v2-group-item="g.id">
          <span class="os-group-name" :title="g.name">{{ g.name }}</span>
          <span v-if="typeof g.source_count === 'number'" class="os-group-count" :data-v2-group-count="g.id">{{ g.source_count }} 个来源</span>
          <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--commit" :data-v2-group-rename="g.id" @click="openRename(g)">改名</button>
          <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--sm" :data-v2-group-del="g.id" @click="delGroupDlg = g">删除</button>
        </div>
      </div>
    </el-dialog>

    <!-- 改名 prompt（照旧 layer.prompt：空名拦「请输入分组名称」） -->
    <el-dialog
      :model-value="renameDlg !== null"
      title="修改分组名称"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-group-rename"
      @update:model-value="renameDlg = null"
    >
      <el-input
        v-if="renameDlg !== null"
        v-model="renameDlg.name"
        :maxlength="20"
        data-v2="group-rename-input"
        @keydown.enter.prevent="void submitRename()"
      />
      <div class="cv2-dlg-actions">
        <button type="button" class="cv2-btn cv2-btn--quiet" @click="renameDlg = null">取消</button>
        <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="group-rename-ok" @click="void submitRename()">确定</button>
      </div>
    </el-dialog>

    <!-- 删除分组确认（N 来自数据；0/缺省不追加那句） -->
    <el-dialog
      :model-value="delGroupDlg !== null"
      title="提示"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-group-del"
      @update:model-value="delGroupDlg = null"
    >
      <p class="cv2-dlg-text">{{ delGroupDlg === null ? '' : groupDeleteConfirmText(delGroupDlg.name, delGroupDlg.source_count) }}</p>
      <div class="cv2-dlg-actions">
        <button type="button" class="cv2-btn cv2-btn--quiet" @click="delGroupDlg = null">取消</button>
        <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--commit" data-v2="group-del-ok" @click="void confirmDelGroup()">确定</button>
      </div>
    </el-dialog>

    <!-- 删除来源确认（逐字「确定删除该来源吗？」） -->
    <el-dialog
      :model-value="delSoureDlg !== null"
      title="提示"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-soure-del"
      @update:model-value="delSoureDlg = null"
    >
      <p class="cv2-dlg-text">确定删除该来源吗？</p>
      <div class="cv2-dlg-actions">
        <button type="button" class="cv2-btn cv2-btn--quiet" @click="delSoureDlg = null">取消</button>
        <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--commit" data-v2="soure-del-ok" @click="void confirmDelSoure()">确定</button>
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

/* 表格内锚点动作链：基态不带全局链接下划线、悬停补回（照列表页家族口径）。 */
a.cv2-order-link {
  text-decoration: none;
}

a.cv2-order-link:hover {
  text-decoration: underline;
}

/* 来源说明列长文本单行截断（44px 行高家族口径）。 */
.os-remark {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 列头帮助小图标（对标 layui-bangzhu 的炭火版；圆圈问号，title 即逐字帮助文案）。 */
.os-help {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 14px;
  height: 14px;
  margin-left: 4px;
  border: 1px solid var(--tt2-st-paused-dot);
  border-radius: 50%;
  font-size: 11px;
  line-height: 1;
  color: var(--tt2-st-paused-dot);
  cursor: help;
  vertical-align: middle;
}

/* 短信签名格：恒空文本但保留可点的热区与 title（照旧 data-orginsmssign 恒空）。 */
.os-sms-sign {
  display: inline-block;
  min-width: 24px;
}

/* 「订单是否可发送短信」恒关开关的炭火版（灰底 + 白点居左 = 禁用态；点了只提示）。 */
.os-sms-switch {
  position: relative;
  width: 40px;
  height: 20px;
  padding: 0;
  border: 1px solid var(--tt2-tool-line);
  border-radius: 10px;
  background: var(--tt2-btn-soft-line);
  cursor: pointer;
  vertical-align: middle;
}

.os-sms-switch-dot {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--tt2-panel);
}

.os-actions {
  white-space: nowrap;
}

.os-actions .cv2-btn--sm + .cv2-btn--sm {
  margin-left: -1px;
}

/* 面板限高内部滚动（照 V2OrderShipLog 同款；顶部区域较矮给更多余量）。 */
.cv2-table-panel {
  max-height: calc(100vh - 380px);
  min-height: 220px;
  overflow: auto;
}

/* ── 店铺归属区块（同页独立 panel；子表跟随内容，不与主表共滚动）。── */
.os-shops {
  margin-top: 12px;
}

.os-shops-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 16px;
  margin-bottom: 8px;
}

.os-shops-field {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.os-shops-label {
  font-size: 13px;
  color: var(--tt2-dim);
}

.os-shops-select {
  width: 180px;
  padding: 5px 10px;
}

.os-group-select {
  flex: 1;
  min-width: 0;
}

.os-group-sources {
  margin-bottom: 10px;
}

.os-radios {
  flex-wrap: wrap;
  row-gap: 10px;
}

.os-radios .cv2-radio {
  white-space: nowrap;
}

/* ── 分组管理弹窗：新增一行三件套 + 行列表。── */
.os-group-add {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.os-group-add-name {
  flex: 1;
}

.os-group-add-order {
  width: 90px;
  flex: none;
}

.os-group-empty {
  padding: 18px 12px;
}

.os-group-list {
  max-height: 320px;
  overflow: auto;
}

.os-group-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0;
  border-bottom: 1px solid var(--tt2-line);
}

.os-group-item:last-child {
  border-bottom: none;
}

.os-group-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.os-group-count {
  color: var(--tt2-dim);
  font-size: 12px;
  flex: none;
}

/* 弹层表单行距（尺寸非色值）。 */
.cv2-dialog .cv2-field {
  margin-bottom: 12px;
}
</style>
