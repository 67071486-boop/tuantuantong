<script setup lang="ts">
import { orderStatusClass } from '@/v2/orderStatusAppearance'
import { computed, ref, watch } from 'vue'
import { HttpError } from '@/api/errors'
import { fetchMyOrderDetail, type MyOrderDetail } from '@/api/myOrders'
import { v2OrderStatusLabel } from '@/config/orderStatus'
import { formatDateTime } from '@/m/utils/datetime'

const props = defineProps<{ orderNo: string }>()
const detail = ref<MyOrderDetail | null>(null)
const error = ref('')
const forbidden = ref(false)
const retry = ref(0)
const copyMessage = ref('')

watch([() => props.orderNo, retry], async ([orderNo], _, onCleanup) => {
  let current = true
  onCleanup(() => { current = false })
  detail.value = null
  error.value = ''
  forbidden.value = false
  copyMessage.value = ''
  try {
    const loaded = await fetchMyOrderDetail(orderNo)
    if (current) detail.value = loaded
  } catch (e) {
    if (!current) return
    forbidden.value = e instanceof HttpError && e.status === 403
    error.value = forbidden.value
      ? '这张单不是你接的，或无权查看'
      : (e instanceof Error && e.message) || '订单详情加载失败'
  }
}, { immediate: true })

type DetailRow = { label: string; value: string | null; copy?: boolean; status?: string }

const groups = computed<{ title: string; rows: DetailRow[] }[]>(() => {
  const d = detail.value
  if (d === null) return []
  const orderNo = d.source_order_no || d.order_no
  return [
    { title: '基本信息', rows: [
      { label: '订单号', value: orderNo, copy: true },
      { label: '游戏', value: d.game_name },
      { label: '区服', value: d.server_name },
      { label: '代练项目', value: d.project_text },
      { label: '订单状态', value: v2OrderStatusLabel(d.status), status: d.status },
      { label: '加急', value: d.is_urgent ? '加急' : '—' },
      { label: '接手时间', value: formatDateTime(d.claimed_at) },
    ] },
    { title: '要求', rows: [
      { label: '订单内容', value: d.content },
      { label: '代练要求', value: d.demand },
      { label: '时间要求', value: d.time_requirement },
      { label: '备注', value: d.remark },
    ] },
    ...([d.game_account, d.game_sub_account, d.game_role_name].some(Boolean)
      ? [{ title: '游戏账号', rows: [
        { label: '游戏账号', value: d.game_account, copy: true },
        { label: '子账号', value: d.game_sub_account, copy: true },
        { label: '角色信息', value: d.game_role_name, copy: true },
      ] }]
      : []),
    { title: '我的报酬', rows: [{ label: '接单金额', value: `${d.temp_amount} 元` }] },
  ]
})

async function copyValue(value: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(value)
    copyMessage.value = '已复制'
  } catch {
    copyMessage.value = '复制失败，请手动选中文字复制。'
  }
}
</script>

<template>
  <div class="own-order-detail" data-v2="order-detail-pane">
    <div v-if="error" role="alert">
      <p>{{ error }}</p>
      <button v-if="!forbidden" type="button" class="cv2-btn cv2-btn--tool" @click="retry++">重试</button>
    </div>
    <p v-else-if="detail === null" role="status">正在加载订单详情…</p>
    <template v-else>
      <section v-for="group in groups" :key="group.title">
        <h3>{{ group.title }}</h3>
        <dl>
          <template v-for="row in group.rows" :key="row.label">
            <dt>{{ row.label }}</dt>
            <dd>
              <span v-if="row.status !== undefined" class="cv2-st cv2-order-status" :class="orderStatusClass(row.status)"><i class="cv2-dot" aria-hidden="true"></i>{{ row.value }}</span>
              <span v-else>{{ row.value || '—' }}</span>
              <button
                v-if="row.copy && row.value"
                type="button"
                class="cv2-actlink"
                :aria-label="`复制${row.label}`"
                @click="copyValue(row.value)"
              >复制</button>
            </dd>
          </template>
        </dl>
      </section>
      <section>
        <h3>群聊二维码</h3>
        <img v-if="detail.group_qr_url" :src="detail.group_qr_url" alt="群聊二维码" />
        <p v-else>暂无群码</p>
      </section>
      <p v-if="copyMessage" role="status">{{ copyMessage }}</p>
    </template>
  </div>
</template>

<style scoped>
.own-order-detail { padding: 16px; overflow: auto; }
section + section { margin-top: 14px; }
h3 { margin: 0 0 6px; font-size: 14px; }
dl { display: grid; grid-template-columns: 90px minmax(0, 1fr); margin: 0; }
dt, dd { margin: 0; padding: 6px 10px; border-bottom: 1px solid var(--tt2-filter-line); }
dt { background: var(--tt2-filter-label-bg); }
dd { overflow-wrap: anywhere; white-space: pre-wrap; }
dd > button { margin-left: 12px; }
img { display: block; width: 180px; height: 180px; object-fit: contain; }
[role="alert"] { color: var(--tt2-st-error); }
</style>
