<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import {
  decideApproval, fetchApprovalOptions, fetchApprovals,
  type Approval, type ApprovalDecision, type ApprovalMode, type ApprovalOptions,
} from '@/api/approvals'
import { isOrderNoShape } from '@/api/orders'
import AppIcon from '@/components/AppIcon.vue'
import { useSessionStore } from '@/stores/session'
import OrderTabDialog from '@/v2/OrderTabDialog.vue'
import V2ApprovalImage from '@/v2/V2ApprovalImage.vue'
import V2ApprovalSettings from '@/v2/V2ApprovalSettings.vue'
import OrderDetailDialog from '@/v2/OrderDetailDialog.vue'
import V2OrderDetailPane from '@/v2/V2OrderDetailPane.vue'
import V2PenaltyDialog from '@/v2/V2PenaltyDialog.vue'

const MODES = [
  { value: 'pending', label: '待我处理', empty: '暂无待处理审批' },
  { value: 'mine', label: '我发起的', empty: '暂无你发起的审批' },
  { value: 'copied', label: '抄送我的', empty: '暂无抄送给你的审批' },
  { value: 'handled', label: '我已处理', empty: '暂无你已处理的审批' },
] as const
const TEMPLATES = [
  { key: 'penalty', title: '罚款审批', form: V2PenaltyDialog },
] as const
type TemplateKey = (typeof TEMPLATES)[number]['key']
const PAGE_SIZE = 20
const session = useSessionStore()
const dateTime = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'short', timeZone: 'Asia/Shanghai' })
const options = ref<ApprovalOptions | null>(null)
const tab = ref<ApprovalMode | 'settings'>('pending')
const page = ref(1)
const total = ref(0)
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))
const items = ref<Approval[]>([])
const loading = ref(false)
const choosingTemplate = ref(false)
const templateKey = ref<TemplateKey | null>(null)
const activeTemplate = computed(() => TEMPLATES.find((template) => template.key === templateKey.value))
const activeMode = computed(() => MODES.find((mode) => mode.value === tab.value))
const detail = ref<Approval | null>(null)
const orderDetailNo = ref<string | null>(null)
const comment = ref('')
const deciding = ref(false)
const decisionError = ref('')
const settingsError = ref('')
const listError = ref('')
const notice = ref('')
let requestSequence = 0

function statusText(item: Approval): string {
  if (item.status === 'pending') {
    if (item.kind === 'penalty') return '待审批'
    if (item.requires_assignment) return item.approver_name ? '待重新分配' : '待分配'
    return '待处理'
  }
  if (item.status === 'approved') return item.kind === 'refund' ? '已处理' : '已通过'
  return '已驳回'
}

function shownDate(value: string): string {
  return dateTime.format(new Date(value))
}

function statusClass(item: Approval): string {
  if (item.status === 'rejected') return 'cv2-st--error'
  if (item.status === 'approved') return 'cv2-st--doing'
  return item.requires_assignment ? 'cv2-st--accept' : 'cv2-st--pending'
}

async function loadOptions(): Promise<void> {
  settingsError.value = ''
  try {
    options.value = await fetchApprovalOptions()
  } catch (cause) {
    settingsError.value = cause instanceof Error ? cause.message : '审批权限读取失败'
  }
}

async function load(): Promise<void> {
  const sequence = ++requestSequence
  if (tab.value === 'settings') { loading.value = false; return }
  loading.value = true
  listError.value = ''
  try {
    const result = await fetchApprovals(tab.value, page.value)
    if (sequence !== requestSequence) return
    items.value = result.items
    total.value = result.total
    if (page.value > pageCount.value) page.value = pageCount.value
  } catch (cause) {
    if (sequence === requestSequence) listError.value = cause instanceof Error ? cause.message : '审批记录读取失败'
  } finally {
    if (sequence === requestSequence) loading.value = false
  }
}

function changeTab(next: ApprovalMode | 'settings'): void {
  if (tab.value === next) return
  items.value = []
  total.value = 0
  page.value = 1
  tab.value = next
  notice.value = ''
}

function selectTemplate(key: TemplateKey): void {
  choosingTemplate.value = false
  templateKey.value = key
}

function onSubmitted(): void {
  const title = activeTemplate.value?.title
  templateKey.value = null
  if (tab.value === 'mine' && page.value === 1) void load()
  else {
    changeTab('mine')
    page.value = 1
  }
  notice.value = `${title ?? '审批'}已提交`
}

function showDetail(item: Approval): void {
  detail.value = item
  comment.value = ''
  decisionError.value = ''
}

function closeDetail(): void {
  if (!deciding.value) detail.value = null
}

function canOpenOrder(orderNo: string): boolean {
  return isOrderNoShape(orderNo) && session.canNode(['order.view_detail', 'order.view_content'])
}

async function decide(decision: ApprovalDecision): Promise<void> {
  const item = detail.value
  if (!item?.can_decide || deciding.value) return
  decisionError.value = ''
  if (decision === 'rejected' && !comment.value.trim()) {
    decisionError.value = '请填写驳回原因'
    return
  }
  deciding.value = true
  try {
    detail.value = await decideApproval(item.id, decision, comment.value.trim())
    notice.value = '处理结果已保存'
    await load()
  } catch (cause) {
    decisionError.value = cause instanceof Error ? cause.message : '审批处理失败'
  } finally {
    deciding.value = false
  }
}

watch([tab, page], load)
onMounted(() => { void loadOptions(); void load() })
</script>

<template>
  <div class="cv2-page approvals" data-v2-page="approvals">
    <header class="approvals-header">
      <h1>审批中心</h1>
      <button v-if="options?.can_apply" type="button" class="cv2-btn cv2-btn--primary" data-v2="start-approval" @click="choosingTemplate = true">
        <AppIcon name="add-1" />
        发起审批
      </button>
    </header>
    <p v-if="settingsError" class="approvals-error" role="alert">
      {{ settingsError }} <button class="cv2-btn cv2-btn--quiet" type="button" @click="loadOptions">重试</button>
    </p>
    <section class="cv2-panel approval-results" aria-label="审批记录" :aria-busy="loading">
      <div class="approval-results-toolbar">
        <nav class="cv2-ptabs approval-tabs" aria-label="审批记录视图">
          <button
            v-for="mode in MODES"
            :key="mode.value"
            class="cv2-ptab"
            :class="{ 'cv2-ptab--on': mode.value === tab }"
            :aria-current="mode.value === tab ? 'page' : undefined"
            type="button"
            @click="changeTab(mode.value)"
          >{{ mode.label }}</button>
          <button
            v-if="options?.can_manage"
            class="cv2-ptab"
            :class="{ 'cv2-ptab--on': tab === 'settings' }"
            :aria-current="tab === 'settings' ? 'page' : undefined"
            type="button"
            @click="changeTab('settings')"
          >审批配置</button>
        </nav>
        <button v-if="tab !== 'settings'" type="button" class="cv2-btn cv2-btn--tool" :disabled="loading" @click="load">刷新</button>
      </div>
      <div v-if="tab === 'settings'" class="approval-settings-body">
        <V2ApprovalSettings @saved="loadOptions" />
      </div>
      <template v-else>
        <p v-if="notice" class="approvals-notice" role="status">{{ notice }}</p>
        <p v-if="listError" class="approvals-error" role="alert">
          {{ listError }} <button class="cv2-btn cv2-btn--quiet" type="button" @click="load">重试</button>
        </p>
        <p v-else-if="loading" class="approval-empty" role="status">正在读取审批记录…</p>
        <p v-else-if="items.length === 0" class="approval-empty">{{ activeMode?.empty }}</p>
        <template v-else>
          <div class="approval-table-scroll">
            <table class="cv2-table approval-table">
              <thead>
                <tr>
                  <th scope="col">审批类型</th>
                  <th scope="col">状态</th>
                  <th scope="col">原订单</th>
                  <th scope="col">发起人</th>
                  <th scope="col">审批或处理人</th>
                  <th scope="col">发起时间</th>
                  <th scope="col">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="item in items" :key="item.id">
                  <td class="approval-type">{{ item.kind === 'penalty' ? '罚款审批' : '退款处理' }}</td>
                  <td>
                    <span class="cv2-st cv2-st--mark" :class="statusClass(item)">
                      <span class="cv2-dot" aria-hidden="true" />
                      {{ statusText(item) }}
                    </span>
                  </td>
                  <td class="cv2-num">{{ item.original_order_no || '—' }}</td>
                  <td>{{ item.initiator_name || '系统' }}</td>
                  <td>{{ item.approver_name || '未分配' }}</td>
                  <td class="cv2-num">{{ shownDate(item.created_at) }}</td>
                  <td>
                    <button type="button" class="cv2-actlink" @click="showDetail(item)">{{ item.can_decide ? '处理' : '查看' }}</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="cv2-pager">
            <span class="cv2-pager-total">共 {{ total }} 条</span>
            <button type="button" class="cv2-btn" :disabled="page === 1" @click="page -= 1">上一页</button>
            <span>{{ page }} / {{ pageCount }}</span>
            <button type="button" class="cv2-btn" :disabled="page >= pageCount" @click="page += 1">下一页</button>
          </div>
        </template>
      </template>
    </section>

    <OrderTabDialog
      v-if="choosingTemplate"
      :model-value="true"
      title="发起审批"
      width="min(460px, calc(100% - 32px))"
      @update:model-value="choosingTemplate = false"
    >
      <section class="approval-templates" aria-label="审批模板">
        <h2>选择审批模板</h2>
        <button
          v-for="template in TEMPLATES"
          :key="template.key"
          type="button"
          class="approval-template"
          @click="selectTemplate(template.key)"
        >
          <span class="approval-template-icon"><AppIcon name="form" /></span>
          <span>{{ template.title }}</span>
        </button>
      </section>
    </OrderTabDialog>
    <component :is="activeTemplate.form" v-if="activeTemplate" @close="templateKey = null" @submitted="onSubmitted" />
    <OrderTabDialog
      v-if="detail"
      :model-value="true"
      :title="detail.kind === 'penalty' ? '罚款审批' : '退款处理'"
      width="min(690px, calc(100% - 32px))"
      :close-on-click-modal="false"
      :close-on-press-escape="!deciding"
      :show-close="!deciding"
      @update:model-value="closeDetail"
    >
      <dl class="approval-detail">
        <dt>状态</dt><dd>{{ statusText(detail) }}</dd>
        <dt>发起人</dt><dd>{{ detail.initiator_name || '系统' }}</dd>
        <dt>审批或处理人</dt><dd>{{ detail.approver_name || '未分配' }}</dd>
        <dt>发起时间</dt><dd>{{ shownDate(detail.created_at) }}</dd>
        <template v-if="detail.original_order_no">
          <dt>原订单</dt>
          <dd class="cv2-num">
            <button v-if="canOpenOrder(detail.original_order_no)" type="button" class="cv2-order-link approval-order-link" aria-haspopup="dialog" :disabled="deciding" @click="orderDetailNo = detail.original_order_no">{{ detail.original_order_no }}</button>
            <template v-else>{{ detail.original_order_no }}</template>
          </dd>
        </template>
        <template v-if="detail.refund_source_no">
          <dt>平台来源单号</dt><dd class="cv2-num">{{ detail.refund_source_no }}</dd>
        </template>
        <template v-if="detail.kind === 'penalty'">
          <dt>罚款对象</dt><dd>{{ detail.target_name }}（{{ detail.target_type === 'studio' ? '下家工作室' : '打手' }}）</dd>
          <dt class="approval-penalty-accent">罚款金额</dt><dd class="cv2-num approval-penalty-accent">{{ detail.amount }} 元</dd>
          <dt>项目内容</dt><dd>{{ detail.order.project_text || '—' }}</dd>
          <template v-if="detail.order.order_type">
            <dt>订单类型</dt><dd>{{ detail.order.order_type === 'COMPANION' ? '陪玩' : '代练' }}</dd>
          </template>
          <dt>接单价</dt><dd class="cv2-num">{{ detail.order.total_price ?? '0.00' }} 元</dd>
          <dt>罚款对象是否异议</dt><dd>{{ detail.objection || '—' }}</dd>
          <dt>备注</dt><dd>{{ detail.remark || '—' }}</dd>
          <dt>抄送人</dt><dd>{{ detail.cc_names.join('、') || '—' }}</dd>
        </template>
        <dt :class="{ 'approval-penalty-accent': detail.kind === 'penalty' }">{{ detail.kind === 'penalty' ? '罚款原因' : '退款待办原因' }}</dt>
        <dd :class="{ 'approval-penalty-accent': detail.kind === 'penalty' }">{{ detail.reason || '—' }}</dd>
        <template v-if="detail.generated_order_no">
          <dt>罚款订单</dt>
          <dd class="cv2-num">
            <button v-if="canOpenOrder(detail.generated_order_no)" type="button" class="cv2-order-link approval-order-link" aria-haspopup="dialog" :disabled="deciding" @click="orderDetailNo = detail.generated_order_no">{{ detail.generated_order_no }}</button>
            <template v-else>{{ detail.generated_order_no }}</template>
          </dd>
        </template>
        <template v-if="detail.decided_at">
          <dt>处理时间</dt><dd>{{ shownDate(detail.decided_at) }}</dd>
          <dt>处理意见</dt><dd>{{ detail.comment || '—' }}</dd>
        </template>
      </dl>
      <div v-if="detail.images.length" class="approval-images">
        <V2ApprovalImage v-for="(image, index) in detail.images" :key="image.url" :src="image.url" :alt="`证据截图 ${index + 1}`" />
      </div>
      <button
        v-if="detail.requires_assignment && options?.can_manage"
        type="button" class="cv2-btn" @click="closeDetail(); changeTab('settings')"
      >配置退款处理人</button>
      <template v-if="detail.can_decide">
        <label class="approval-comment">处理意见 <textarea v-model="comment" rows="3" maxlength="3000" :disabled="deciding" /></label>
        <p v-if="decisionError" class="approvals-error" role="alert">{{ decisionError }}</p>
        <div class="approval-decision">
          <button type="button" class="cv2-btn" :disabled="deciding" @click="decide('rejected')">驳回</button>
          <button type="button" class="cv2-btn cv2-btn--primary" :disabled="deciding" @click="decide('approved')">{{ detail.kind === 'refund' ? '标记已处理' : '通过并生成罚款单' }}</button>
        </div>
      </template>
    </OrderTabDialog>
    <OrderDetailDialog
      :model-value="orderDetailNo !== null"
      destroy-on-close
      :title="orderDetailNo === null ? '' : `${orderDetailNo}订单详情`"
      class="cv2-dialog cv2-dialog--detail"
      data-v2="approval-order-detail"
      @update:model-value="orderDetailNo = null"
      @chat-handoff="closeDetail()"
    >
      <V2OrderDetailPane v-if="orderDetailNo" :order-no="orderDetailNo" />
    </OrderDetailDialog>
  </div>
</template>

<style scoped>
.approvals { display: grid; align-content: start; gap: 14px; }
.approvals-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.approvals-header h1 { margin: 0; font-size: 22px; font-weight: 650; color: var(--tt2-ink); }
.approval-results { min-width: 0; margin: 0; padding: 0; }
.approval-results-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 6px 12px;
  padding: 0 14px;
  min-height: 48px;
  border-bottom: 1px solid var(--tt2-line);
}
.approval-tabs { flex-wrap: wrap; margin: 0; border: 0; align-self: stretch; }
.approval-settings-body { padding: 0 16px 16px; }
.approval-table-scroll { overflow: auto; }
.approval-table { table-layout: auto; }
.approval-table :is(th, td) { padding-inline: 14px; white-space: nowrap; }
.approval-table .approval-type { font-weight: 600; }
.approval-table .cv2-num { font-family: var(--tt2-mono); }
.approval-table .cv2-st--accept { --st-ink: var(--tt2-st-accept); }
.approval-empty { padding: 42px 14px; margin: 0; text-align: center; color: var(--tt2-dim); }
.approval-templates h2 { margin: 0 0 12px; font-size: 14px; font-weight: 500; color: var(--tt2-dim); }
.approval-template {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  padding: 10px 12px;
  border: 1px solid var(--tt2-line);
  border-radius: 6px;
  background: var(--tt2-btn-soft-bg);
  color: var(--tt2-ink);
  cursor: pointer;
  text-align: left;
  font: inherit;
  font-weight: 600;
}
.approval-template:hover, .approval-template:focus-visible { border-color: var(--tt2-primary); background: var(--tt2-hover-wash); }
.approval-template-icon {
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  border-radius: 4px;
  background: var(--tt2-st-pending-bg);
  color: var(--tt2-st-pending);
  font-size: 18px;
}
.approvals-notice, .approvals-error { margin: 0; padding: 10px 12px; }
.approvals-error { color: var(--tt2-st-error); }
.approvals-notice { color: var(--tt2-dim); }
.approval-detail { margin: 0 0 12px; display: grid; grid-template-columns: 122px minmax(0, 1fr); gap: 8px; }
.approval-detail dt { color: var(--tt2-dim); }
.approval-detail dd { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.approval-detail .approval-penalty-accent { color: var(--tt2-st-error); font-weight: 600; }
.approval-order-link { padding: 0; border: 0; background: none; cursor: pointer; font-size: inherit; }
.approval-images { display: flex; flex-wrap: wrap; gap: 10px; margin: 12px 0; }
.approval-comment { display: grid; gap: 6px; margin-top: 16px; }
.approval-comment textarea { resize: none; border: 1px solid var(--tt2-filter-line); border-radius: 4px; color: var(--tt2-ink); padding: 8px; font: inherit; }
.approval-decision { display: flex; justify-content: flex-end; gap: 8px; margin-top: 10px; }
</style>
