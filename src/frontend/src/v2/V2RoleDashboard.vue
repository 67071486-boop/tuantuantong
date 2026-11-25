<script setup lang="ts">
import { orderStatusClass } from '@/v2/orderStatusAppearance'
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useSessionStore } from '@/stores/session'
import { fetchGames, type ConfigRow } from '@/api/config'
import { fetchHomeQrCodes, type HomeQrRow } from '@/api/homeQrCodes'
import { fetchMyOrders, type MyOrderItem } from '@/api/myOrders'
import V2ServiceDashboard from '@/v2/V2ServiceDashboard.vue'
import { v2OrderStatusLabel } from '@/config/orderStatus'

const props = defineProps<{ layout: 'worker' | 'work' }>()
const session = useSessionStore()
const router = useRouter()
const games = ref<ConfigRow[]>([])
const gameError = ref(false)
const qrCodes = ref<HomeQrRow[]>([])
const qrError = ref(false)
const myOrders = ref<MyOrderItem[]>([])
const total = ref<number | null>(null)
const orderError = ref(false)
const loadingOrders = ref(false)
const canOrders = computed(() => session.canNode([props.layout === 'worker' ? 'my_orders.send_to_my_system' : 'order.list']))
const shortcuts = computed(() => [
  { label: '我的订单', node: 'my_orders', route: 'v2-my-orders' },
  { label: '我的收入', node: 'my_performance', route: 'v2-income' },
  { label: '内部接单', node: 'grab_hall', route: 'v2-hall' },
  { label: '我的资料', node: 'my_profile', route: 'v2-profile' },
  { label: '消息通知', node: 'my_profile', route: 'v2-messages' },
  { label: '录入订单', node: 'order.add_edit', route: 'v2-order-new' },
  { label: '管理订单', node: 'order.list', route: 'v2-order-manage' },
  { label: '结算情况', node: 'finance.payroll', route: 'v2-performance' },
  { label: '系统子账户', node: 'system_user.list', route: 'v2-user-list' },
].filter((item) => session.canNode([item.node])))
const rows = computed(() => myOrders.value)
const qrLabel = (row: HomeQrRow): string => row.kind === 'qq_group' ? `${row.game_name || '游戏'} QQ 群` : row.kind === 'qq_bot' ? 'QQ 机器人' : '微信公众号'

onMounted(async () => {
  void fetchGames().then((value) => { games.value = value }).catch(() => { gameError.value = true })
  if (props.layout === 'worker') {
    void fetchHomeQrCodes().then((value) => { qrCodes.value = value.items.filter((row) => row.is_enabled && row.media_url) }).catch(() => { qrError.value = true })
  }
  if (props.layout !== 'worker' || !canOrders.value) return
  loadingOrders.value = true
  try {
    const page = await fetchMyOrders({ page: 1, page_size: 5 })
    myOrders.value = page.items
    total.value = page.total
  } catch {
    orderError.value = true
  } finally {
    loadingOrders.value = false
  }
})
</script>

<template>
  <div v-if="layout === 'worker'" class="worker-home" :class="{ 'worker-home--with-orders': canOrders && shortcuts.length > 0, 'worker-home--has-qr': canOrders && shortcuts.length > 0 && (qrCodes.length > 0 || qrError) }" :data-role-dashboard="layout">
    <section class="worker-welcome">
      <div class="worker-welcome-main">
        <span class="worker-avatar" aria-hidden="true">{{ (session.user?.display_name || session.user?.username || '我').slice(0, 1) }}</span>
        <div>
          <span class="worker-kicker">我的工作台</span>
          <h2>你好，{{ session.user?.display_name || session.user?.username }}</h2>
          <p>{{ session.user?.roles?.join('、') || '暂无职位' }}</p>
        </div>
      </div>
      <div class="worker-games">
        <span class="worker-games-label">游戏权限</span>
        <p v-if="gameError">加载失败，请刷新重试</p>
        <div v-else-if="games.length" class="worker-game-list"><span v-for="game in games" :key="game.id">{{ game.name }}</span></div>
        <p v-else>暂无</p>
      </div>
    </section>

    <section v-if="shortcuts.length" class="worker-panel worker-shortcut-panel">
      <div class="worker-heading"><div><span class="worker-section-label">常用入口</span><h2>快捷操作</h2></div></div>
      <div class="worker-shortcuts">
        <button v-for="(item, index) in shortcuts" :key="item.route" type="button" @click="router.push({ name: item.route })">
          <span class="worker-shortcut-index" aria-hidden="true">{{ String(index + 1).padStart(2, '0') }}</span>
          <span>{{ item.label }}</span><span class="worker-arrow" aria-hidden="true">→</span>
        </button>
      </div>
    </section>

    <section v-if="canOrders" class="worker-panel worker-orders-panel" data-role-orders>
      <div class="worker-heading"><div><span class="worker-section-label">订单动态</span><h2>我的订单 <span v-if="total !== null" class="worker-count">{{ total }} 单</span></h2><p>最近的订单</p></div><button v-if="session.canNode(['my_orders'])" type="button" class="worker-more" @click="router.push({ name: 'v2-my-orders' })">查看全部 <span aria-hidden="true">→</span></button></div>
      <p v-if="loadingOrders" class="worker-empty">正在加载订单…</p>
      <p v-else-if="orderError" class="worker-empty role-error">订单加载失败，请刷新重试</p>
      <p v-else-if="!rows.length" class="worker-empty">暂无订单</p>
      <div v-else class="worker-table-wrap"><table class="worker-table cv2-data-grid"><thead><tr><th>订单号</th><th>订单内容</th><th>订单状态</th></tr></thead><tbody>
        <tr v-for="row in rows" :key="row.order_no"><td>{{ row.source_order_no || row.order_no }}</td><td>{{ row.project_text || '—' }}</td><td><span class="cv2-st cv2-st--mark cv2-order-status" :class="orderStatusClass(row.status)"><i class="cv2-dot" aria-hidden="true"></i>{{ v2OrderStatusLabel(row.status) }}</span></td></tr>
      </tbody></table></div>
    </section>

    <section v-if="qrCodes.length || qrError" class="worker-panel worker-qr-panel">
      <div class="worker-heading"><div><h2>常用二维码</h2><p>扫码查看相关服务</p></div></div>
      <p v-if="qrError" class="worker-empty role-error">二维码加载失败，请刷新重试</p>
      <div v-else class="role-qrs"><figure v-for="qr in qrCodes" :key="qr.id"><img :src="qr.media_url || ''" :alt="qrLabel(qr)" /><figcaption>{{ qrLabel(qr) }}</figcaption></figure></div>
    </section>
  </div>
  <V2ServiceDashboard v-else :can-orders="canOrders" :shortcuts="shortcuts"><template #notices><slot name="notices" /></template></V2ServiceDashboard>
</template>

<style scoped>
.worker-home { display: grid; grid-template-columns: minmax(0, 1fr); gap: 14px; margin-bottom: 14px; }
.worker-home--with-orders { grid-template-columns: minmax(0, 1fr) minmax(230px, 280px); grid-template-areas: "welcome welcome" "orders shortcuts"; align-items: start; }
.worker-home--has-qr { grid-template-areas: "welcome welcome" "orders shortcuts" "orders qr"; }
.worker-home--with-orders .worker-welcome { grid-area: welcome; }
.worker-home--with-orders .worker-orders-panel { grid-area: orders; }
.worker-home--with-orders .worker-shortcut-panel { grid-area: shortcuts; }
.worker-home--with-orders .worker-qr-panel { grid-area: qr; }
.worker-welcome { display: flex; align-items: center; justify-content: space-between; gap: 24px; padding: 20px 24px; background: var(--tt2-panel); border: 1px solid var(--tt2-line); border-radius: 6px; }
.worker-welcome-main { display: flex; align-items: center; gap: 14px; flex: 1; min-width: 0; }
.worker-avatar { display: grid; place-items: center; flex: none; width: 48px; height: 48px; border-radius: 50%; color: var(--tt2-primary); background: #e3f0ed; font-size: 18px; font-weight: 600; }
.worker-kicker, .worker-section-label { color: var(--tt2-primary); font-size: 11px; font-weight: 600; letter-spacing: .06em; }
.worker-welcome h2 { margin: 4px 0; color: var(--tt2-ink); font-size: 18px; }
.worker-welcome p { margin: 0; color: var(--tt2-dim); font-size: 13px; line-height: 1.6; }
.worker-games { flex: 0 1 48%; min-width: 0; padding-left: 24px; border-left: 1px solid var(--tt2-line); }
.worker-games-label { color: var(--tt2-dim); font-size: 12px; }
.worker-game-list { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 7px; }
.worker-game-list span { padding: 3px 8px; color: var(--tt2-ink); background: #f6f4f0; border: 1px solid var(--tt2-line); border-radius: 4px; font-size: 12px; }
.worker-panel { min-width: 0; padding: 20px; background: var(--tt2-panel); border: 1px solid var(--tt2-line); border-radius: 6px; }
.worker-heading { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 16px; }
.worker-heading h2 { margin: 5px 0 0; color: var(--tt2-ink); font-size: 15px; font-weight: 600; }
.worker-heading p { margin: 5px 0 0; color: var(--tt2-dim); font-size: 12px; }
.worker-shortcuts { display: grid; gap: 2px; }
.worker-shortcuts button { display: flex; align-items: center; gap: 11px; min-width: 0; padding: 10px 8px; color: var(--tt2-ink); font: inherit; font-size: 13px; text-align: left; background: transparent; border: 0; border-radius: 4px; cursor: pointer; }
.worker-shortcuts button:hover, .worker-shortcuts button:focus-visible { color: var(--tt2-primary); background: var(--tt2-accent-box-bg); }
.worker-shortcut-index { color: var(--tt2-dim); font-size: 11px; font-variant-numeric: tabular-nums; }
.worker-arrow { margin-left: auto; color: var(--tt2-dim); }
.worker-count { margin-left: 7px; color: var(--tt2-primary); font-size: 12px; font-weight: 500; }
.worker-more { padding: 5px 0; color: var(--tt2-primary); font: inherit; font-size: 13px; background: none; border: 0; cursor: pointer; }
.worker-more:hover { color: var(--tt2-primary-deep); text-decoration: underline; }
.worker-table-wrap { overflow-x: auto; }
.worker-table { width: 100%; border-collapse: collapse; text-align: left; font-size: 13px; }
.worker-table th { padding: 11px 14px; color: var(--tt2-dim); font-weight: 500; background: #f8f6f2; white-space: nowrap; }
.worker-table td { padding: 14px; color: var(--tt2-ink); }
.worker-table td:first-child { color: var(--tt2-primary-deep); font-variant-numeric: tabular-nums; white-space: nowrap; }
.worker-table td:last-child { white-space: nowrap; width: 120px; }
.worker-qr-panel .role-qrs { justify-content: center; }
.worker-qr-panel .role-qrs img { width: 112px; height: 112px; }
.worker-empty { margin: 0; padding: 28px 0; color: var(--tt2-dim); font-size: 13px; text-align: center; }
@media (max-width: 900px) { .worker-home--with-orders { grid-template-columns: minmax(0, 1fr); grid-template-areas: "welcome" "shortcuts" "orders"; } .worker-home--has-qr { grid-template-areas: "welcome" "shortcuts" "orders" "qr"; } .worker-shortcuts { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 680px) { .worker-welcome { align-items: stretch; flex-direction: column; gap: 16px; padding: 18px; } .worker-games { padding: 14px 0 0; border-top: 1px solid var(--tt2-line); border-left: 0; } .worker-table { min-width: 540px; } }
.role-dashboard { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin-bottom: 16px; }
.role-card { background: #fff; border: 1px solid #e8e3dc; border-radius: 12px; padding: 20px; color: #403a35; }
.role-card h2 { margin: 0 0 14px; font-size: 16px; }
.role-identity { background: #f6efe5; }
.role-identity h2 { font-size: 21px; }
.role-card p { margin: 8px 0; color: #776e65; font-size: 13px; }
.role-eyebrow { color: #a06d48 !important; }
.role-shortcuts { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px; }
.role-shortcuts button { border: 1px solid #ece5dc; border-radius: 8px; background: #faf8f5; padding: 11px; text-align: left; color: #514a43; cursor: pointer; }
.role-shortcuts button span { float: right; color: #b9784f; }
.role-card-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; }
.role-card-head span { color: #8c8177; font-size: 12px; }
.role-orders { padding: 0; margin: 0; list-style: none; }
.role-orders li { display: grid; grid-template-columns: 1fr 1fr auto; gap: 8px; padding: 10px 0; border-top: 1px solid #f0ece7; font-size: 13px; }
.role-orders li span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.role-orders strong { color: #a56d4a; font-weight: 500; }
.role-qrs { display: flex; flex-wrap: wrap; gap: 18px; }
.role-qrs figure { margin: 0; text-align: center; font-size: 12px; color: #776e65; }
.role-qrs img { width: 100px; height: 100px; object-fit: contain; }
.role-error { color: #b84a3c !important; }
@media (max-width: 760px) { .role-dashboard { grid-template-columns: 1fr; } }
</style>
