<script setup lang="ts">
import { f7Badge as F7Badge, f7Button as F7Button, f7List as F7List } from 'framework7-vue'
import { computed, ref, useTemplateRef, watch } from 'vue'
import { fetchOrder, type Order } from '@/api/orders'
import { fetchGames, fetchServers } from '@/api/config'
import { formatMoney } from '@/config/money'
import { orderStatusLabel, orderStatusTagType } from '@/config/orderStatus'
import { useSessionStore } from '@/stores/session'
import { displayOrderNo, shortTime } from './format'

/**
 * 订单详情（页内第二视图，窄窗纵向排版）。⛔ 不是新窗口 / 新标签（业务方要求页内跳转）；⛔ 不套后台壳；指派 / 抛大厅 / 改状态**仍不在本页**。数据与宽版详情同源（`GET /orders/<单号>/`），只是取**窄窗下最有用**的那些字段纵向排。
 * ⭐ 底部「编辑」动作条 emit 给父页切到编辑视图（业务方需求「详情 → 二次编辑」）。渲染门 **fail-closed**：不持任一保存节点（`order.add_edit` ∪ `order.submit_edit`，与 `KefuOrderForm` 编辑态的 `canSaveOrder` 同一组）就**不画**——⛔ 不是画了点了报错（默认值调度侧代定、业务方过目）。
 */
const props = defineProps<{ orderNo: string }>()
defineEmits<{ back: []; edit: [] }>()
const footer = useTemplateRef<HTMLElement>('footer')
defineExpose({ footer })

const session = useSessionStore()

/** 「编辑」入口的渲染门：any(两个保存节点)；无节点不画按钮（fail-closed）。 */
const canEditOrder = computed(() =>
  session.canNode(['order.add_edit', 'order.submit_edit']),
)

const order = ref<Order | null>(null)
const loading = ref(false)
const error = ref('')
const gameName = ref('')
const serverName = ref('')
/** latest-wins 的令牌：换单号后旧响应回来不能覆盖新详情。 */
let token = 0

async function load(): Promise<void> {
  const my = ++token
  loading.value = true
  error.value = ''
  order.value = null
  gameName.value = ''
  serverName.value = ''
  try {
    const row = await fetchOrder(props.orderNo)
    if (my !== token) return
    order.value = row
    // 字典取不到不该把整页详情拖垮（游戏 / 区服名回落到 `#id`，见 `sections`）。
    const [games, servers] = await Promise.all([
      fetchGames().catch(() => []),
      fetchServers(row.game).catch(() => []),
    ])
    if (my !== token) return
    gameName.value = games.find((g) => g.id === row.game)?.name ?? ''
    serverName.value = servers.find((s) => s.id === row.server)?.name ?? ''
  } catch (e) {
    if (my !== token) return
    error.value = e instanceof Error && e.message !== '' ? e.message : '读取订单详情失败'
  } finally {
    if (my === token) loading.value = false
  }
}

watch(() => props.orderNo, () => { void load() }, { immediate: true })

/**
 * 头部要显示的「展示单号」。
 * ⚠️ 模板里**必须**真的渲染它（`<span class="kefu-detail-title">{{ title }}</span>`）：
 * `script setup` 里未被模板引用的顶层绑定会被 `@typescript-eslint/no-unused-vars` 判成未使用。
 */
const title = computed(() => (order.value ? displayOrderNo(order.value) : props.orderNo))

/**
 * 收款状态中文**逐字**取自后端 `PaymentStatus`（`apps/business/models.py:229-231`）：
 * 未收款 / 结算中 / 已结款。前端没有现成完整映射（island 只有单字标记），本组件是唯一消费者。
 * 未知码原样显示（与 `orderStatusLabel` 的 fail-soft 同口径）。
 */
const PAYMENT_STATUS_LABELS: Record<string, string> = {
  UNSETTLED: '未收款',
  SETTLING: '结算中',
  SETTLED: '已结款',
}

function paymentStatusLabel(code: string): string {
  return PAYMENT_STATUS_LABELS[code] ?? code
}

const orderRows = computed<[string, string][]>(() => {
  const o = order.value
  if (!o) return []
  return [
    ['代练内容', o.project_text],
    ['订单内容', o.content],
    ['需求描述', o.demand],
    ['游戏', gameName.value || `#${o.game}`],
    ['区服', o.server === null ? '' : (serverName.value || `#${o.server}`)],
    ['时间要求', o.time_requirement],
    ['订单总价', formatMoney(o.total_price)],
    ['接单实收款（录单填写）', formatMoney(o.received_price)],
    ['发平台价', formatMoney(o.platform_price)],
    ['发单价', formatMoney(o.hall_price)],
    ['手续费', formatMoney(o.fee)],
    ['累计退款', formatMoney(o.refund_fee)],
    ['收款状态', paymentStatusLabel(o.payment_status)],
    ['收款方式', o.payment_method],
    ['订单类型', o.order_type === 'COMPANION' ? '陪玩' : '代练'],
    ['加急', o.is_urgent ? '是' : '否'],
    ['旺旺/YY', o.customer_ww],
    ['QQ', o.customer_qq],
    ['联系电话', o.customer_phone],
    ['角色名', o.game_role_name],
    ['游戏账号', o.game_account],
    ['子账号', o.game_sub_account],
    ['当前信息1', o.current_info_1],
    ['当前信息2', o.current_info_2],
    ['接单销售客服', o.sales_agent],
    ['录单人（账号）', o.created_by],
    ['更新时间', shortTime(o.updated_at)],
    ['备注', o.remark],
  ]
})
</script>

<template>
  <div class="kefu-detail">
    <div class="block kefu-detail-scroll">
      <p v-if="loading" class="kefu-detail-loading">订单加载中…</p>
      <div v-else-if="error" class="kefu-detail-error" role="alert">
        <p class="kefu-detail-error-text">{{ error }}</p>
        <F7Button type="button" class="kefu-back" @click="$emit('back')">← 返回</F7Button>
      </div>
      <template v-else-if="order">
        <header class="kefu-detail-head">
          <F7Badge class="kefu-status" :color="orderStatusTagType(order.status) === 'danger' ? 'red' : undefined">
            {{ orderStatusLabel(order.status) }}
          </F7Badge>
          <!-- ⚠️ `title` 必须在这里被引用（见脚本注释）。 -->
          <span class="kefu-detail-title">{{ title }}</span>
        </header>
        <p class="kefu-detail-meta">
          <span class="kefu-detail-no">内置单号：{{ order.order_no }}</span>
          <span class="kefu-detail-time">{{ shortTime(order.created_at) }}</span>
        </p>
        <F7List strong inset class="kefu-detail-list">
          <template #list>
            <li v-for="[label, value] in orderRows" :key="label" class="kefu-detail-row">
              <span class="kefu-detail-label">{{ label }}</span>
              <span class="kefu-detail-value">{{ value || '—' }}</span>
            </li>
          </template>
        </F7List>
      </template>
    </div>
    <!-- ⭐ 编辑入口：只在「详情已加载 + 有保存节点」时渲染（fail-closed）。
         本组件只 emit，切视图 / 审计留痕由父页 KefuConsoleView 做。 -->
    <footer ref="footer" class="kefu-detail-footer">
      <F7Button
        v-if="order !== null && canEditOrder"
        class="kefu-detail-edit"
        type="button"
        fill round
        @click="$emit('edit')"
      >
        编辑
      </F7Button>
    </footer>
  </div>
</template>

<style scoped>
/*
 * 详情页与录单表单同构的 flex 列——滚动收进 `.kefu-detail-scroll`，底部「编辑」动作条（`.kefu-detail-footer`）常驻不滚
 * （与 `KefuOrderForm` 的 `.kefu-form` / `.kefu-form-scroll` / `.kefu-footer` 同一套骨架，⛔ 不用 sticky / padding-bottom 补丁，那正是录单页踩过的坑）。
 */
.kefu-detail {
  box-sizing: border-box;
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  min-height: 0;
}
.kefu-detail-scroll {
  box-sizing: border-box;
  flex: 1 1 auto;
  min-height: 0;
  margin: 0;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 8px 8px 12px;
  -webkit-overflow-scrolling: touch;
}
.kefu-detail-loading,
.kefu-detail-error {
  margin: 0;
  padding: 8px 0;
  color: var(--kefu-muted);
  font-size: 12px;
  line-height: 1.5;
}
.kefu-detail-error-text {
  margin: 0 0 8px;
  color: var(--kefu-error);
  overflow-wrap: anywhere;
}
.kefu-detail-error .kefu-back {
  padding: 0;
  border: none;
  background: none;
  color: var(--f7-theme-color);
  font-size: 13px;
  cursor: pointer;
}
.kefu-detail-head {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  padding: 4px 10px;
}
.kefu-detail-title {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  font-size: 14px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.kefu-detail-meta {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 4px 8px;
  margin: 0 10px 8px;
  color: var(--kefu-muted);
  font-size: 12px;
}
.kefu-detail-list.list { margin: 0; }
.kefu-detail-row {
  display: flex;
  gap: 8px;
  margin: 0;
  padding: 6px 9px;
  font-size: 14px;
  line-height: 1.5;
}
.kefu-detail-row:not(:last-child) { border-bottom: 1px solid var(--kefu-divider); }
.kefu-detail-label {
  flex: 0 1 90px;
  overflow-wrap: anywhere;
  color: var(--kefu-muted);
}
.kefu-detail-value {
  flex: 1 1 auto;
  min-width: 0;
  text-align: start;
  /* ⭐ I-b：长单号 / 长账号在 331 下不许把行撑破（元素级溢出判据）。 */
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}
/* ── 底部「编辑」动作条（2026-10-29）：常驻不滚，按钮撑满宽度（窄页好点）。 ── */
.kefu-detail-footer {
  box-sizing: border-box;
  flex: 0 0 auto;
  display: flex;
}
.kefu-detail-footer:not(:empty) {
  padding: 8px 10px 10px;
  background: var(--f7-bars-bg-color);
  border-top: 1px solid var(--kefu-divider);
}
.kefu-detail-footer .kefu-detail-edit {
  flex: 1 1 auto;
  min-height: 36px;
}
</style>
