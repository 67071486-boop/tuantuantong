<script setup lang="ts">
/* 订单时间线保留直达搜索，展示与交易订单操作记录复用。 */
import { computed, onUnmounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { fetchOrderTimeline, type OrderTimeline } from '@/api/auditTimeline'
import { useSessionStore } from '@/stores/session'
import V2OrderTimeline from '@/v2/V2OrderTimeline.vue'
import { V2_ORDER_ACTION_LOG } from '@/v2/v2Menu'

const session = useSessionStore()

/* ── 权限门（fail-closed：会话取不到 = 不画；与 V2MyOrders 同口径）────────────── */

const canView = computed(() => session.canNode(['order.platform_action_logs']))

/* ── 提示（炭火 toast，页面内自绘——同 V2MyOrders 的本地实现）────────────────── */

const toast = ref<{ text: string; kind: 'info' | 'error' } | null>(null)
let toastTimer: ReturnType<typeof setTimeout> | null = null

function showToast(text: string, kind: 'info' | 'error' = 'error'): void {
  if (toastTimer !== null) clearTimeout(toastTimer)
  toast.value = { text, kind }
  toastTimer = setTimeout(() => {
    toast.value = null
    toastTimer = null
  }, 6000)
}

/** 错误对象的可显示文本。**不自造文案**：`request()` 抛出的 message 就是后端 `msg` 原文。 */
function errText(e: unknown): string {
  return e instanceof Error && e.message ? e.message : '查询失败'
}

/* ── 查询 ─────────────────────────────────────────────────────────────── */

const key = ref('')
const loading = ref(false)
const timeline = ref<OrderTimeline | null>(null)

async function search(): Promise<void> {
  const k = key.value.trim()
  if (!k) {
    showToast('请输入系统单号或来源单号', 'info')
    return
  }
  if (loading.value) return
  loading.value = true
  try {
    timeline.value = await fetchOrderTimeline(k)
  } catch (e) {
    // 查无（1002「未找到匹配的订单」）/ 无权限（403）都把后端 msg 原样给出。
    timeline.value = null
    showToast(errText(e), 'error')
  } finally {
    loading.value = false
  }
}

onUnmounted(() => {
  if (toastTimer !== null) clearTimeout(toastTimer)
})

const actionLogTarget = computed(() => {
  const orderNo = timeline.value?.order.order_no
  return { name: V2_ORDER_ACTION_LOG, query: orderNo ? { order_no: orderNo } : {} }
})
</script>

<template>
  <div class="cv2-page" data-v2-page="order-audit">
    <header class="cv2-page-head">
      <h1 class="cv2-page-title">订单时间线</h1>
      <RouterLink v-if="canView" class="cv2-panel-more" :to="actionLogTarget" data-v2="to-order-action-log">查看交易订单操作记录</RouterLink>
    </header>

    <!-- 权限门：fail-closed，会话取不到节点就只画这一句（同 V2MyOrders 的纪律） -->
    <div v-if="!canView" class="cv2-empty" data-v2="no-permission">
      你的账号没有「交易订单操作记录」权限，看不了订单时间线；需要的话找管理员开通。
    </div>

    <template v-else>
      <div class="cv2-toolbar" data-v2="search-bar">
        <input
          v-model="key"
          class="cv2-input cv2-input--grow"
          type="text"
          placeholder="系统单号或来源单号（20 位系统单号 / 录入的来源号都认）"
          data-v2="search-key"
          @keydown.enter="search()"
        />
        <button
          type="button"
          class="cv2-btn cv2-btn--primary"
          :disabled="loading"
          data-v2="search-btn"
          @click="search()"
        >
          {{ loading ? '查询中…' : '查时间线' }}
        </button>
      </div>

      <div v-if="toast" class="cv2-toast" :class="`cv2-toast--${toast.kind}`" data-v2="toast">
        {{ toast.text }}
      </div>

      <V2OrderTimeline v-if="timeline" :timeline="timeline" />
    </template>
  </div>
</template>

<style scoped>
.cv2-input--grow {
  flex: 1;
  min-width: 260px;
}

/* 炭火 toast（页面内自绘，同 V2MyOrders 的形状；error 停 6000ms 已在脚本里）。 */
.cv2-toast {
  margin-top: 10px;
  padding: 8px 12px;
  border-radius: 6px;
  border: 1px solid var(--tt2-line);
}

.cv2-toast--error {
  border-color: var(--tt2-st-error-line);
  background: var(--tt2-st-error-bg);
  color: var(--tt2-st-error);
}

.cv2-toast--info {
  color: var(--tt2-dim);
}
</style>
