<script setup lang="ts">
import { orderStatusClass } from '@/v2/orderStatusAppearance'
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { fetchServiceDashboard, type ServiceDashboardData } from '@/api/serviceDashboard'
import { v2OrderStatusLabel } from '@/config/orderStatus'

const props = defineProps<{ canOrders: boolean; shortcuts: { label: string; route: string }[] }>()
const router = useRouter()
const data = ref<ServiceDashboardData | null>(null)
const loading = ref(true)
const error = ref(false)
const cards = [
  { status: 'WAITING_GRAB', label: '待接单', tone: 'pending' },
  { status: 'PENDING_REVIEW', label: '待验收', tone: 'accept' },
  { status: 'ISSUE', label: '问题单', tone: 'error' },
  { status: 'PAUSED', label: '已暂停', tone: 'paused' },
] as const

function openOrders(status?: string, orderNo?: string): void {
  void router.push({ name: 'v2-order-manage', query: { self_sales: '1', ...(status ? { status } : {}), ...(orderNo ? { keyword: orderNo, open_order: orderNo } : {}) } })
}

onMounted(async () => {
  if (!props.canOrders) { loading.value = false; return }
  try { data.value = await fetchServiceDashboard() } catch { error.value = true } finally { loading.value = false }
})
</script>

<template>
  <div class="service-home" data-role-dashboard="work">
    <template v-if="canOrders">
      <p v-if="loading" class="service-message">正在加载订单数据…</p>
      <p v-else-if="error" class="service-message service-error">订单数据加载失败或当前账号无权查看统计，请稍后重试。</p>
      <template v-else-if="data">
        <div class="service-cards" aria-label="我的订单状态">
          <button v-for="card in cards" :key="card.status" type="button" class="service-stat" :class="`service-stat--${card.tone}`" @click="openOrders(card.status)">
            <span class="service-stat-icon" aria-hidden="true"><i></i></span>
            <span class="service-stat-text"><strong>{{ card.label }}</strong><small>销售客服为我的订单</small></span>
            <b>{{ data.status_counts[card.status] ?? 0 }}</b>
          </button>
        </div>
        <div class="service-grid">
          <section class="service-panel service-attention">
            <header class="service-header"><div><h2>重点跟进</h2><p>我的问题单与暂停订单</p></div><button type="button" class="service-link" @click="openOrders('ISSUE,PAUSED')">查看全部 →</button></header>
            <div v-if="data.attention_orders.length" class="service-table-wrap"><table class="service-table cv2-data-grid"><thead><tr><th>订单号</th><th>游戏</th><th>订单内容</th><th>状态</th><th>更新时间</th></tr></thead><tbody>
              <tr v-for="order in data.attention_orders" :key="order.order_no"><td><button type="button" class="service-order-link" @click="openOrders(undefined, order.order_no)">{{ order.source_order_no || order.order_no }}</button></td><td>{{ order.game_name || '—' }}</td><td>{{ order.project_text || '—' }}</td><td><span class="cv2-st cv2-st--mark cv2-order-status" :class="orderStatusClass(order.status)"><i class="cv2-dot" aria-hidden="true"></i>{{ v2OrderStatusLabel(order.status) }}</span></td><td>{{ order.updated_at.replace('T', ' ').slice(0, 16) }}</td></tr>
            </tbody></table></div><p v-else class="service-empty">暂无问题单和暂停订单</p>
          </section>
          <section class="service-panel service-today"><header class="service-header"><div><h2>今日工作概览</h2><p>仅统计销售客服为我的订单，按实际订单逐笔计算</p></div></header><div class="service-today-grid"><div><span>今日录单</span><strong>{{ data.today_created }}</strong></div><div><span>今日完成</span><strong>{{ data.today_completed }}</strong></div><div title="今天本人有操作记录的订单（含录单），同一订单只计一次"><span>今日跟进</span><strong>{{ data.today_followed }}</strong></div></div></section>
          <aside class="service-side"><section v-if="shortcuts.length" class="service-panel"><header class="service-header"><h2>快捷操作</h2></header><div class="service-shortcuts"><button v-for="item in shortcuts" :key="item.route" type="button" @click="router.push({ name: item.route })">{{ item.label }}<span aria-hidden="true">→</span></button></div></section><slot name="notices" /></aside>
        </div>
      </template>
    </template>
    <div v-if="!canOrders || loading || error" class="service-fallback"><section v-if="shortcuts.length" class="service-panel"><header class="service-header"><h2>快捷操作</h2></header><div class="service-shortcuts"><button v-for="item in shortcuts" :key="item.route" type="button" @click="router.push({ name: item.route })">{{ item.label }}<span aria-hidden="true">→</span></button></div></section><slot name="notices" /></div>
  </div>
</template>

<style scoped>
.service-home { display: grid; gap: 14px; margin-bottom: 14px; }
.service-cards { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.service-stat { display: flex; align-items: center; gap: 12px; min-width: 0; padding: 18px; border: 1px solid var(--tt2-line); border-radius: 6px; background: var(--tt2-panel); text-align: left; cursor: pointer; }
.service-stat:hover { border-color: var(--tt2-primary); }
.service-stat-icon { display: grid; flex: none; place-items: center; width: 38px; height: 38px; border-radius: 50%; background: color-mix(in srgb, var(--tone) 12%, transparent); }
.service-stat-icon i { width: 10px; height: 10px; background: var(--tone); border-radius: 50%; }
.service-stat--pending { --tone: var(--tt2-st-pending-dot); } .service-stat--accept { --tone: var(--tt2-st-accept-dot); } .service-stat--error { --tone: var(--tt2-st-error-dot); } .service-stat--paused { --tone: var(--tt2-st-paused-dot); }
.service-stat-text { display: grid; gap: 5px; min-width: 0; color: var(--tt2-ink); } .service-stat-text small { color: var(--tt2-dim); font-size: 11px; } .service-stat b { margin-left: auto; color: var(--tone); font-size: 23px; }
.service-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(260px, 340px); grid-template-areas: 'attention side' 'today side'; gap: 14px; align-items: start; }
.service-attention { grid-area: attention; } .service-today { grid-area: today; } .service-side { grid-area: side; display: grid; gap: 14px; }
.service-panel, .service-side :deep(.cv2-dsh-card), .service-fallback :deep(.cv2-dsh-card) { min-width: 0; padding: 20px; background: var(--tt2-panel); border: 1px solid var(--tt2-line); border-radius: 6px; }
.service-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 16px; } .service-header h2 { margin: 0; color: var(--tt2-ink); font-size: 15px; } .service-header p { margin: 5px 0 0; color: var(--tt2-dim); font-size: 12px; }
.service-link, .service-order-link { padding: 0; border: 0; color: var(--tt2-primary); background: transparent; font: inherit; cursor: pointer; } .service-link:hover, .service-order-link:hover { text-decoration: underline; }
.service-table-wrap { overflow-x: auto; } .service-table { width: 100%; border-collapse: collapse; text-align: left; font-size: 13px; } .service-table th { padding: 10px; color: var(--tt2-dim); background: #f8f6f2; font-weight: 500; white-space: nowrap; } .service-table td { padding: 13px 10px; } .service-table td:first-child, .service-table td:last-child { white-space: nowrap; }
.service-today-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); } .service-today-grid div { display: grid; gap: 8px; padding: 4px 16px; border-left: 1px solid var(--tt2-line); } .service-today-grid div:first-child { padding-left: 0; border-left: 0; } .service-today-grid span { color: var(--tt2-dim); font-size: 13px; } .service-today-grid strong { color: var(--tt2-primary); font-size: 22px; }
.service-shortcuts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; } .service-shortcuts button { display: flex; justify-content: space-between; gap: 8px; padding: 10px; border: 1px solid var(--tt2-line); border-radius: 4px; background: #faf9f6; color: var(--tt2-ink); text-align: left; cursor: pointer; } .service-shortcuts button:hover { color: var(--tt2-primary); border-color: var(--tt2-primary); }
.service-empty, .service-message { margin: 0; padding: 20px; color: var(--tt2-dim); text-align: center; } .service-error { color: var(--tt2-st-error); } .service-fallback { display: grid; gap: 14px; }
@media (max-width: 1100px) { .service-cards { grid-template-columns: repeat(2, minmax(0, 1fr)); } .service-grid { grid-template-columns: minmax(0, 1fr); grid-template-areas: 'attention' 'today' 'side'; } }
@media (max-width: 560px) { .service-stat { padding: 12px; } .service-stat-text small { display: none; } .service-table { min-width: 650px; } }
</style>
