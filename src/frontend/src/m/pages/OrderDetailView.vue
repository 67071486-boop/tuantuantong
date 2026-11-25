<script setup lang="ts">
import {
  f7Badge as F7Badge,
  f7Block as F7Block,
  f7BlockTitle as F7BlockTitle,
  f7Button as F7Button,
  f7Card as F7Card,
  f7Link as F7Link,
  f7List as F7List,
  f7ListItem as F7ListItem,
  f7Preloader as F7Preloader,
} from 'framework7-vue'
import MobilePageBar from '../components/MobilePageBar.vue'
import { SCREENSHOT_RETURN_ORDER, useWorkerBack } from '../router/useWorkerBack'
import { internalChatOrderReturn } from '../router/internalChatOrderReturn'
import { computed, defineAsyncComponent, inject, onBeforeUnmount, ref, useId, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Toast } from '../framework7Ui'
import MobilePhotoBrowser from '../components/MobilePhotoBrowser.vue'
import { useSessionStore } from '@/stores/session'
import type { Order } from '@/api/orders'
import { fetchMyOrderDetail, type MyOrderDetail } from '@/api/myOrders'
import { fetchScreenshots, type ScreenshotItem } from '@/api/screenshots'
import { BizError, HttpError } from '@/api/errors'
import { ErrorCode } from '@/config/errorCodes'
import { orderStatusLabel, type OrderStatus } from '@/config/orderStatus'
import SubmitWorkDialog from '../components/SubmitWorkDialog.vue'
import { formatDateTime } from '../utils/datetime'
import { isOrderNoShape } from '../utils/orderNoShape'
import { openInternalChatPreview } from '@/v2/internalChat/launcherPreview'
import { orderChatPreviewEnabled } from '@/v2/internalChat/orderChatPreview'
import { wxpusherOrderAppLink } from '../utils/wxpusherOrderLink'

/** 交单 / 报问题两键的权限节点（与 T3 同一颗：后端 `/submit/` `/report-issue/` 同一个门）。 */
const SUBMIT_NODE = 'order.submit_work'
/** 前端状态闸：只有代练中的单给两键（后端 1002 兜底）。 */
const IN_PROGRESS = 'IN_PROGRESS'
/** 复制成功提示（任务书 T4 自拟白名单，登记于报告）。 */
const COPY_SUCCESS_TEXT = '已复制'
/** 复制失败提示（任务书 T4 自拟白名单，登记于报告）。 */
const COPY_FAIL_TEXT = '复制失败'

const route = useRoute()
const router = useRouter()
const session = useSessionStore()
const OrderChatPane = defineAsyncComponent(() => import('@/v2/V2OrderChatPane.vue'))
const chatExpanded = ref(false)
const openInternalChat = inject(openInternalChatPreview, null)
const chatNoticeId = useId()
const chatEnabled = computed(() => session.isWorker && (session.user?.internal_chat_enabled === true || orderChatPreviewEnabled()))

/* ── 形状闸 ── */
const orderNo = computed(() => {
  const raw = route.params.orderNo
  return typeof raw === 'string' ? raw : ''
})
/** 不符 → 404 态页（模板直接按它分流），取数入口被它挡住（零请求）。 */
const shapeOk = computed(() => isOrderNoShape(orderNo.value))
const appLink = computed(() => wxpusherOrderAppLink(orderNo.value, route.query.entry))

/* ── 取数 ── */
const detail = ref<MyOrderDetail | null>(null)
const loadState = ref<'loading' | 'ready' | 'error'>('loading')
let detailRequest = 0
/** 错误文案 = 后端 msg 原样。 */
const loadError = ref('')
/** 403 = 已认证但这张单不是你接的（或无节点）：错误态给「返回」而不是「重试」。 */
const forbidden = ref(false)

async function load(): Promise<void> {
  const requestId = ++detailRequest
  detail.value = null
  qrViewerVisible.value = false
  dialogVisible.value = false
  if (!shapeOk.value) return
  const requestedNo = orderNo.value
  loadError.value = ''
  forbidden.value = false
  loadState.value = 'loading'
  try {
    const result = await fetchMyOrderDetail(requestedNo)
    if (requestId !== detailRequest || requestedNo !== orderNo.value) return
    if (result.order_no !== requestedNo) throw new Error('订单不存在或链接有误')
    detail.value = result
    loadState.value = 'ready'
  } catch (e) {
    if (requestId !== detailRequest || requestedNo !== orderNo.value) return
    const inaccessible = (e instanceof HttpError && (e.status === 403 || e.status === 404))
      || (e instanceof BizError && e.code === ErrorCode.BUSINESS)
    if (inaccessible && router.options.history.state[SCREENSHOT_RETURN_ORDER] === requestedNo
      && router.currentRoute.value.name === 'm-order-detail' && orderNo.value === requestedNo) {
      goBack()
      return
    }

    loadState.value = 'error'
    loadError.value = errText(e)
    forbidden.value = e instanceof HttpError && e.status === 403
    Toast.error(loadError.value)
  }
}

const goBack = useWorkerBack(() => ({ name: 'm-mine' }))
const returnToChat = inject(internalChatOrderReturn, undefined)

function onBack(): void {
  if (!returnToChat?.()) goBack()
}

/* ── 显示辅助 ── */

function hasValue(value: string | null | undefined): boolean {
  return (value ?? '').trim() !== ''
}

const hasRequirements = computed(() => hasValue(detail.value?.content) || hasValue(detail.value?.demand))
const hasGameAccount = computed(() =>
  hasValue(detail.value?.game_account)
  || hasValue(detail.value?.game_sub_account)
  || hasValue(detail.value?.game_role_name),
)
const claimedAt = computed(() => formatDateTime(detail.value?.claimed_at))

/* ── 复制（游戏账号三字段 + 单号） ── */

async function copyText(text: string): Promise<void> {
  // 剪贴板 API 不在（非安全上下文 / 老内核 WebView）⇒ 直接失败提示，不假装成功。
  if (typeof navigator.clipboard?.writeText !== 'function') {
    Toast.error(COPY_FAIL_TEXT)
    return
  }
  try {
    await navigator.clipboard.writeText(text)
    Toast.success(COPY_SUCCESS_TEXT)
  } catch {
    Toast.error(COPY_FAIL_TEXT)
  }
}

const qrViewerVisible = ref(false)
const canViewShots = computed(() => session.canNode(['order.view_screenshot']))
const canUploadShots = computed(() => session.canNode(['order.upload_screenshot']))
const verifiedOrderNo = computed(() =>
  loadState.value === 'ready' && detail.value?.order_no === orderNo.value ? orderNo.value : null,
)

function enterOrderChat(requestedNo: string, conversationId: number): void {
  if (requestedNo !== verifiedOrderNo.value || !chatEnabled.value || !chatExpanded.value || !openInternalChat) return
  openInternalChat(conversationId)
  chatExpanded.value = false
}

const shots = ref<ScreenshotItem[]>([])
const qrFromShots = ref('')
const groupQr = computed(() => (canViewShots.value && qrFromShots.value) || detail.value?.group_qr_url?.trim() || '')
const shotsState = ref<'loading' | 'ready' | 'forbidden' | 'error'>('loading')
const shotsError = ref('')
const shotsRetry = ref(0)
const shotsViewerVisible = ref(false)
const shotsViewerIndex = ref(0)
const shotsViewerImages = computed(() => shots.value.map((item) => item.url))

// 截图列表是共用接口，先由「我的订单」详情确认接单归属，再按查看权限读取。
watch([verifiedOrderNo, canViewShots, shotsRetry], async ([requestedNo, allowed], _, onCleanup) => {
  shots.value = []
  qrFromShots.value = ''
  shotsViewerVisible.value = false
  shotsState.value = 'loading'
  shotsError.value = ''
  if (!requestedNo || !allowed) return

  let stale = false
  onCleanup(() => { stale = true })
  const isCurrent = () => !stale && verifiedOrderNo.value === requestedNo && canViewShots.value
  try {
    const data = await fetchScreenshots(requestedNo)
    if (!isCurrent()) return
    shots.value = data.items.filter((item) => item.img_type === 0 && item.url.trim())
    let latestQr: ScreenshotItem | undefined
    for (const item of data.items) {
      if (item.img_type === 1 && item.url.trim() && (!latestQr || item.id > latestQr.id)) latestQr = item
    }
    qrFromShots.value = latestQr?.url ?? ''
    shotsState.value = 'ready'
  } catch (e) {
    if (!isCurrent()) return
    shotsState.value = e instanceof HttpError && e.status === 403 ? 'forbidden' : 'error'
    shotsError.value = errText(e)
  }
})

function previewShot(index: number): void {
  shotsViewerIndex.value = index
  shotsViewerVisible.value = true
}

function openScreenshots(): void {
  if (!verifiedOrderNo.value || (!canViewShots.value && !canUploadShots.value)) return
  router.options.history.replace(router.currentRoute.value.fullPath, {
    [SCREENSHOT_RETURN_ORDER]: verifiedOrderNo.value,
  })
  void router.push({ name: 'm-order-screenshots', params: { orderNo: verifiedOrderNo.value } })
}

/* ── 交单 / 报问题（复用 T3 弹层） ── */

const canSubmit = computed(() => session.canNode([SUBMIT_NODE]))
/** 两键的可见性 = 状态闸 × 权限，二者都满足才渲染（不满足就不画，⛔ 不置灰）。 */
const canAct = computed(
  () => verifiedOrderNo.value !== null && detail.value?.status === IN_PROGRESS && canSubmit.value,
)

const dialogVisible = ref(false)
const dialogTarget = ref<OrderStatus>('PENDING_REVIEW')

function openDialog(target: OrderStatus): void {
  dialogTarget.value = target
  dialogVisible.value = true
}

/**
 * 弹层成功回调：用**响应载荷**的 status 就地更新（载荷是流转后的完整订单）——
 * ⛔ 不重取详情（任务书 T4 明确）。按载荷里的 `order_no` 认单（不是闭包里那张）。
 */
function onDialogSuccess(order: Order): void {
  const current = detail.value
  if (current !== null && current.order_no === order.order_no) {
    current.status = order.status
  }
}

/** 失败一律原样显示错误文本（`request()` 抛出的 message 就是后端 `msg` 原文）。 */
function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

watch(orderNo, load, { immediate: true })
watch(orderNo, () => { chatExpanded.value = false })
onBeforeUnmount(() => { detailRequest += 1 })
</script>

<template>
  <section class="m-odetail" :class="{ 'm-odetail--with-actions': canAct }">
    <MobilePageBar title="订单详情" back-test-id="detail-back" @back="onBack" />

    <!-- 单号形状闸：不符 → 404 态页（零请求，见上） -->
    <section v-if="!shapeOk" class="m-odetail__missing" data-testid="detail-404">
      <p class="m-odetail__missing-code">404</p>
      <p class="m-odetail__missing-text">订单不存在或链接有误</p>
    </section>

    <template v-else>
      <div v-if="loadState === 'loading'" class="m-odetail__loading" data-testid="detail-loading" role="status">
        <F7Preloader :size="18" aria-hidden="true" /> 加载中…
      </div>

      <!-- 取数失败：msg 原样；403 → 返回，其它 → 重试 -->
      <section v-else-if="loadState === 'error'" class="m-odetail__error" data-testid="detail-error">
        <p class="m-odetail__error-text">{{ loadError }}</p>
        <F7Button type="button"
          v-if="!forbidden"
          class="m-odetail__error-retry" round outline small
          data-testid="detail-error-retry"
          @click="load"
        >
          重试
        </F7Button>
      </section>

      <template v-else-if="verifiedOrderNo && detail">
        <F7BlockTitle role="heading" aria-level="2" class="m-odetail__group-title">代练内容</F7BlockTitle>
        <F7Card outline class="m-odetail__card" data-testid="detail-card-base">
          <div class="m-odetail__summary-header">
            <div class="m-odetail__summary-main">
              <div class="m-odetail__summary-tags">
                <F7Badge class="m-odetail__status" data-testid="detail-status-badge">{{ orderStatusLabel(detail.status) }}</F7Badge>
                <F7Badge v-if="detail.is_urgent" color="red" data-testid="detail-row-urgent">加急</F7Badge>
              </div>
              <p v-if="hasValue(detail.game_name)" class="m-odetail__game"
                data-testid="detail-row-game">{{ detail.game_name }}</p>
              <p v-if="hasValue(detail.project_text)" class="m-odetail__project"
                data-testid="detail-row-project">{{ detail.project_text }}</p>
              <p v-if="hasValue(detail.server_name)" class="m-odetail__server"
                data-testid="detail-row-server">{{ detail.server_name }}</p>
            </div>
            <p class="m-odetail__pay" data-testid="detail-row-temp-amount">
              <span>接单金额</span><strong>{{ detail.temp_amount }} 元</strong>
            </p>
          </div>
          <F7List v-if="hasRequirements" class="m-odetail-list" data-testid="detail-card-require">
            <F7ListItem v-if="hasValue(detail.content)" class="m-odetail-row m-odetail-row--multiline"
              title="订单内容" data-testid="detail-row-content">
              <template #after>
                <span class="m-odetail-row__value">{{ detail.content }}</span>
              </template>
            </F7ListItem>
            <F7ListItem v-if="hasValue(detail.demand)" class="m-odetail-row m-odetail-row--multiline"
              title="代练要求" data-testid="detail-row-demand">
              <template #after>
                <span class="m-odetail-row__value">{{ detail.demand }}</span>
              </template>
            </F7ListItem>
          </F7List>

          <F7Link v-if="appLink" class="external m-odetail__app-link" :href="appLink" data-testid="detail-open-daidaitong">
            在代代通打开
          </F7Link>
        </F7Card>

        <F7BlockTitle role="heading" aria-level="2" class="m-odetail__group-title">本单群聊</F7BlockTitle>
        <F7Block strong inset outline class="m-odetail__panel" data-testid="detail-card-qr">
          <div v-if="groupQr" class="m-odetail__qr">
            <F7Button type="button" class="m-odetail__qr-preview" aria-label="放大群聊二维码" @click="qrViewerVisible = true">
              <img
                class="m-odetail__qr-img"
                :src="groupQr"
                alt="群聊二维码"
                data-testid="detail-qr-img"
              />
            </F7Button>
            <div class="m-odetail__qr-copy">
              <strong>扫码进本单群</strong>
              <p class="m-odetail__hint">点击二维码放大后扫码进群</p>
            </div>
            <MobilePhotoBrowser
              v-model:visible="qrViewerVisible"
              :images="[groupQr]"
              data-testid="detail-qr-viewer"
            />
          </div>
          <p v-else class="m-odetail__qr-empty" data-testid="detail-qr-empty">暂无群码</p>
        </F7Block>

        <template v-if="chatEnabled">
          <F7BlockTitle role="heading" aria-level="2" class="m-odetail__group-title">内部沟通</F7BlockTitle>
          <F7Block strong inset outline class="m-odetail__panel">
            <F7Button type="button" tonal round small data-testid="detail-open-internal-chat"
              :aria-describedby="chatNoticeId" :aria-expanded="chatExpanded" @click="chatExpanded = !chatExpanded">
              {{ chatExpanded ? '收起内部沟通' : '打开内部沟通' }}
            </F7Button>
            <p :id="chatNoticeId" class="m-odetail__hint m-odetail__chat-notice">
              此群聊仅供内部沟通。请用微信扫描「本单群聊」二维码进群，联系老板上号。
            </p>
            <OrderChatPane v-if="chatExpanded" :key="orderNo" :order-no="orderNo"
              :production="session.user?.internal_chat_enabled === true" :enter-on-load="openInternalChat !== null"
              class="m-order-chat-pane" @enter="enterOrderChat" />
          </F7Block>
        </template>

        <div class="m-odetail__shots-heading">
          <F7BlockTitle role="heading" aria-level="2" class="m-odetail__group-title">订单截图</F7BlockTitle>
          <F7Button
            v-if="canUploadShots || (canViewShots && shotsState !== 'forbidden')"
            type="button" class="m-odetail__shots-btn" round tonal
            data-testid="detail-open-screenshots"
            @click="openScreenshots"
          >
            {{ canUploadShots ? '上传截图' : '截图详情' }}
          </F7Button>
        </div>
        <F7Block strong inset outline class="m-odetail__panel" data-testid="detail-card-screenshots">
          <p v-if="!canViewShots || shotsState === 'forbidden'" class="m-odetail__hint">
            没有查看订单截图的权限，请联系管理员开通。
          </p>
          <div v-else-if="shotsState === 'loading'" class="m-odetail__shots-status" role="status">
            <F7Preloader :size="16" aria-hidden="true" /> 加载中…
          </div>
          <div v-else-if="shotsState === 'error'" class="m-odetail__shots-status">
            <p class="m-odetail__hint" role="status">{{ shotsError }}</p>
            <F7Button type="button" round outline small data-testid="detail-shots-retry" @click="shotsRetry += 1">
              重试
            </F7Button>
          </div>
          <template v-else-if="shots.length">
            <ul class="m-odetail__shots-strip" aria-label="订单截图">
              <li v-for="(item, index) in shots" :key="item.id">
                <F7Button type="button" class="m-odetail__shot" :aria-label="`放大第 ${index + 1} 张截图`" @click="previewShot(index)">
                  <img :src="item.thumbnail_url || item.url" :alt="item.original_name" loading="lazy" />
                </F7Button>
              </li>
            </ul>
            <p class="m-odetail__hint m-odetail__shots-caption">共 {{ shots.length }} 张，点开看大图。</p>
            <MobilePhotoBrowser
              v-model:visible="shotsViewerVisible"
              :images="shotsViewerImages"
              :index="shotsViewerIndex"
              show-index
            />
          </template>
          <p v-else class="m-odetail__hint">暂无订单截图</p>
        </F7Block>

        <F7BlockTitle role="heading" aria-level="2" class="m-odetail__group-title">订单信息</F7BlockTitle>
        <F7Card outline class="m-odetail__card">
          <F7List class="m-odetail-list">
            <F7ListItem v-if="hasValue(detail.time_requirement)" class="m-odetail-row"
              title="时间要求" data-testid="detail-row-time-requirement">
              <template #after>
                <span class="m-odetail-row__value">{{ detail.time_requirement }}</span>
              </template>
            </F7ListItem>
            <F7ListItem v-if="hasValue(detail.remark)" class="m-odetail-row m-odetail-row--multiline"
              title="备注" data-testid="detail-row-remark">
              <template #after>
                <span class="m-odetail-row__value">{{ detail.remark }}</span>
              </template>
            </F7ListItem>
            <F7ListItem class="m-odetail-row m-odetail-row--order-no" title="单号" data-testid="detail-row-order-no">
              <template #after>
                <span class="m-odetail-row__value">{{ detail.source_order_no || detail.order_no }}</span>
                <F7Button
                  class="m-odetail-row__copy" small tonal round
                  type="button"
                  data-testid="detail-copy-order-no"
                  @click="copyText(detail.source_order_no || detail.order_no)"
                >
                  复制
                </F7Button>
              </template>
            </F7ListItem>
            <F7ListItem v-if="claimedAt" class="m-odetail-row" title="接手时间" data-testid="detail-row-claimed-at">
              <template #after>
                <span class="m-odetail-row__value">{{ claimedAt }}</span>
              </template>
            </F7ListItem>
          </F7List>

          <F7List v-if="hasGameAccount" class="m-odetail-list" data-testid="detail-card-account">
            <F7ListItem v-if="hasValue(detail.game_account)" class="m-odetail-row"
              title="游戏账号" data-testid="detail-row-game-account">
              <template #after>
                <span class="m-odetail-row__value">{{ detail.game_account }}</span>
                <F7Button
                  class="m-odetail-row__copy" small tonal round
                  type="button"
                  data-testid="detail-copy-game-account"
                  @click="copyText(detail.game_account)"
                >
                  复制
                </F7Button>
              </template>
            </F7ListItem>
            <F7ListItem v-if="hasValue(detail.game_sub_account)" class="m-odetail-row"
              title="子账号" data-testid="detail-row-game-sub-account">
              <template #after>
                <span class="m-odetail-row__value">{{ detail.game_sub_account }}</span>
                <F7Button
                  class="m-odetail-row__copy" small tonal round
                  type="button"
                  data-testid="detail-copy-game-sub-account"
                  @click="copyText(detail.game_sub_account)"
                >
                  复制
                </F7Button>
              </template>
            </F7ListItem>
            <F7ListItem v-if="hasValue(detail.game_role_name)" class="m-odetail-row"
              title="角色信息" data-testid="detail-row-game-role-name">
              <template #after>
                <span class="m-odetail-row__value">{{ detail.game_role_name }}</span>
                <F7Button
                  class="m-odetail-row__copy" small tonal round
                  type="button"
                  data-testid="detail-copy-game-role-name"
                  @click="copyText(detail.game_role_name)"
                >
                  复制
                </F7Button>
              </template>
            </F7ListItem>
          </F7List>
        </F7Card>
      </template>

      <!-- 底部固定交单入口：状态 × 权限都满足才画（fail-closed，不置灰） -->
      <footer v-if="canAct" class="m-odetail__actions" data-testid="detail-actions">
        <F7Button type="button"
          class="m-odetail__actions-submit" round fill
          data-testid="detail-open-submit"
          @click="openDialog('PENDING_REVIEW')"
        >
          提交完成
        </F7Button>
        <F7Button type="button"
          class="m-odetail__actions-report" round outline color="red"
          data-testid="detail-open-report"
          @click="openDialog('ISSUE')"
        >
          报问题
        </F7Button>
      </footer>

      <SubmitWorkDialog
        v-if="verifiedOrderNo"
        :key="verifiedOrderNo"
        v-model:visible="dialogVisible"
        :order-no="verifiedOrderNo"
        :initial-target="dialogTarget"
        @success="onDialogSuccess"
      />
    </template>
  </section>
</template>

<style scoped>
.m-order-chat-pane { margin-top: 12px; --tt2-ink: var(--f7-text-color); --tt2-dim: var(--f7-list-item-subtitle-text-color); --tt2-line: var(--f7-list-item-border-color); --tt2-panel: var(--f7-page-bg-color); --tt2-primary: var(--f7-theme-color); }
.m-order-chat-pane:has(.order-chat-history) { min-height: 280px; height: min(70dvh, 640px); }
.m-order-chat-pane :deep(.cv2-btn) { border: 1px solid var(--f7-theme-color); border-radius: var(--f7-button-border-radius); padding: 6px 12px; color: var(--f7-theme-color); background: var(--f7-page-bg-color); }
.m-order-chat-pane :deep(.cv2-btn:disabled) { opacity: .5; }

.m-odetail {
  min-height: 100vh;
  padding: 12px 12px 16px;
  background: var(--f7-page-bg-color);
}

.m-odetail--with-actions {
  /* 给底部固定操作条让位，最后一张卡片不被盖住。 */
  padding-bottom: calc(76px + env(safe-area-inset-bottom));
}

.m-odetail__app-link {
  display: inline-flex;
  align-items: center;
  min-height: 44px;
  padding: 0 16px;
  color: var(--f7-theme-color);
}

.m-odetail__loading {
  margin: 0;
  padding: 32px 0;
  font-size: 13px;
  color: #9ca3af;
  text-align: center;
}

.m-odetail__error {
  padding: 24px 12px;
  text-align: center;
}

.m-odetail__error-text {
  margin: 0 0 10px;
  font-size: 13px;
  color: #d54941;
}

.m-odetail__missing {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding-top: 40vw;
}

.m-odetail__missing-code {
  margin: 0;
  font-size: 40px;
  font-weight: 600;
  color: #d54941;
}

.m-odetail__missing-text {
  margin: 0 0 8px;
  font-size: 14px;
  color: #6b7280;
}

.m-odetail__group-title {
  margin: 22px 4px 8px;
  color: var(--f7-text-color);
  font-size: 16px;
  font-weight: 650;
}

.m-odetail__card {
  overflow: hidden;
  --f7-card-outline-border-color: var(--m-border);
}

.m-odetail__summary-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 16px;
  background: var(--m-accent-surface);
}

.m-odetail__summary-main {
  min-width: 0;
  overflow-wrap: anywhere;
}

.m-odetail__summary-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.m-odetail__game {
  margin: 8px 0 2px;
  font-size: 22px;
  font-weight: 700;
  line-height: 1.25;
}

.m-odetail__project, .m-odetail__server {
  margin: 0;
  color: var(--m-muted);
  font-size: 13px;
  line-height: 1.5;
  white-space: pre-wrap;
}

.m-odetail__pay {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 4px;
  flex: none;
  margin: 0;
  color: var(--m-muted);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.m-odetail__pay strong {
  color: var(--f7-theme-color);
  font-size: 17px;
  font-weight: 700;
}

.m-odetail__panel {
  --f7-block-margin-vertical: 0px;
  --f7-block-padding-horizontal: 16px;
  --f7-block-padding-vertical: 14px;
  --f7-block-inset-border-radius: 16px;
  margin-inline: 0;
}

.m-odetail-list {
  --f7-list-margin-vertical: 0px;
  --f7-list-item-padding-horizontal: 16px;
  --f7-list-item-title-font-size: 14px;
  --f7-list-item-after-font-size: 14px;
}

.m-odetail-list + .m-odetail-list, .m-odetail__app-link {
  border-top: 1px solid var(--m-border);
}

.m-odetail-list :deep(.item-after) {
  align-items: center;
  justify-content: flex-end;
  flex-shrink: 1;
  min-width: 0;
  gap: 6px;
  text-align: end;
  white-space: normal;
}

.m-odetail-row--multiline :deep(.item-inner) {
  flex-direction: column;
  align-items: stretch;
  gap: 3px;
}

.m-odetail-row--multiline :deep(.item-after) {
  justify-content: flex-start;
  margin-inline-start: 0;
  padding-inline-start: 0;
  text-align: start;
}

.m-odetail-row__value {
  min-width: 0;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.m-odetail-row--order-no .m-odetail-row__value {
  font-variant-numeric: tabular-nums;
}

.m-odetail-row__copy.button {
  --f7-button-padding-horizontal: 10px;
  --f7-button-small-font-size: 12px;
  flex: none;
  inline-size: auto;
}

.m-odetail-row__copy:focus-visible {
  outline: 2px solid var(--f7-theme-color);
  outline-offset: 2px;
}

.m-odetail__status {
  --f7-badge-bg-color: var(--f7-page-bg-color);
  --f7-badge-text-color: var(--f7-theme-color);
}

.m-odetail__qr {
  display: flex;
  align-items: center;
  gap: 14px;
}

.m-odetail__qr-preview.button {
  flex: none;
  width: auto;
  height: auto;
  padding: 0;
}

.m-odetail__qr-img {
  display: block;
  width: clamp(68px, 20vw, 88px);
  aspect-ratio: 1;
  object-fit: contain;
  border: 1px solid var(--m-border);
  border-radius: 12px;
}

.m-odetail__qr-copy strong {
  display: block;
  margin: 0 0 4px;
  font-size: 14px;
  font-weight: 600;
}

.m-odetail__hint, .m-odetail__shots-status, .m-odetail__qr-empty {
  margin: 0;
  font-size: 13px;
  color: var(--m-muted);
  overflow-wrap: anywhere;
}

.m-odetail__chat-notice { margin-top: 8px; }

.m-odetail__shots-status {
  display: flex;
  align-items: center;
  gap: 8px;
}

.m-odetail__shots-strip {
  display: flex;
  gap: 8px;
  margin: 0;
  padding: 3px;
  overflow-x: auto;
  list-style: none;
}

.m-odetail__shots-strip li {
  flex: none;
}

.m-odetail__shot.button {
  width: 86px;
  height: 86px;
  padding: 0;
  overflow: hidden;
  border-radius: 10px;
  background: var(--m-accent-surface);
}

.m-odetail__shot img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.m-odetail__shots-caption {
  margin-top: 8px;
}

.m-odetail__actions {
  position: fixed;
  z-index: 2;
  right: 0;
  bottom: 0;
  left: 0;
  max-width: 560px;
  margin-inline: auto;
  box-sizing: border-box;
  display: flex;
  gap: 12px;
  padding: 10px 12px calc(10px + env(safe-area-inset-bottom));
  background: var(--f7-page-bg-color);
  border-top: 1px solid var(--m-border);
}

.m-odetail__shots-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin: 12px 4px 8px;
}

.m-odetail__shots-heading .m-odetail__group-title {
  margin: 0;
}

.m-odetail__shots-btn.button {
  width: fit-content;
  min-height: 44px;
}

.m-odetail__actions-submit,
.m-odetail__actions-report {
  flex: 1;
}

@media (max-width: 360px) {
  .m-odetail__group-title {
    margin-block-start: 14px;
  }

  .m-odetail__panel {
    --f7-block-padding-vertical: 10px;
  }

  .m-odetail__qr-img {
    width: 64px;
  }
}
</style>
