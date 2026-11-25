<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import {
  getWecomNotificationSettings,
  listWecomNotificationRecords,
  saveWecomNotificationSettings,
  type WecomNotificationRecord,
} from '@/api/wecomNotifications'

const form = reactive({ group_sync_enabled: true, group_qr_enabled: true, external_pay_enabled: true, payment_mismatch_enabled: false, order_status_enabled: false })
const loaded = ref(false)
const saving = ref(false)
const settingsMessage = ref('')
const settingsError = ref(false)
const records = ref<WecomNotificationRecord[]>([])
const loadingRecords = ref(false)
const recordsError = ref('')
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const hasNext = computed(() => page.value * pageSize.value < total.value)

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '请稍后重试'
}

function recipientStatus(status: string): string {
  return ({ sent: '已发送', failed: '未发送', unknown: '结果未知', pending: '待发送', sending: '发送中' } as Record<string, string>)[status] ?? '状态未知'
}

function timeText(value: string | null): string {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return '—'
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).formatToParts(parsed)
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((item) => item.type === type)?.value ?? ''
  return `${part('year')}-${part('month')}-${part('day')} ${part('hour')}:${part('minute')}:${part('second')}`
}

async function loadSettings(): Promise<void> {
  try {
    Object.assign(form, await getWecomNotificationSettings())
    loaded.value = true
    settingsMessage.value = ''
    settingsError.value = false
  } catch (error) {
    loaded.value = false
    settingsError.value = true
    settingsMessage.value = `读取通知设置失败：${errorText(error)}`
  }
}

async function saveSettings(): Promise<void> {
  if (!loaded.value || saving.value) return
  saving.value = true
  try {
    Object.assign(form, await saveWecomNotificationSettings({ ...form }))
    settingsMessage.value = '功能设置已保存'
    settingsError.value = false
  } catch (error) {
    settingsMessage.value = `保存通知设置失败：${errorText(error)}`
    settingsError.value = true
  } finally {
    saving.value = false
  }
}

async function loadRecords(next: number): Promise<void> {
  loadingRecords.value = true
  try {
    const result = await listWecomNotificationRecords(next)
    records.value = result.items
    page.value = result.page
    pageSize.value = result.page_size
    total.value = result.total
    recordsError.value = ''
  } catch (error) {
    recordsError.value = `读取通知记录失败：${errorText(error)}`
  } finally {
    loadingRecords.value = false
  }
}

function refresh(): void {
  void loadSettings()
  void loadRecords(1)
}

onMounted(refresh)
</script>

<template>
  <section class="cv2-panel" data-v2="block-wecom-notifications">
    <div class="cv2-panel-head">
      <h3 class="cv2-panel-title">企微功能设置</h3>
      <button type="button" class="cv2-btn" data-v2="notification-refresh" @click="refresh">刷新</button>
    </div>
    <div class="wn-options">
      <label><input v-model="form.group_sync_enabled" type="checkbox" :disabled="!loaded || saving" data-v2="feature-group-sync" /> 客户群信息同步</label>
      <label><input v-model="form.group_qr_enabled" type="checkbox" :disabled="!loaded || saving" data-v2="feature-group-qr" /> 订单群码生成</label>
      <label><input v-model="form.external_pay_enabled" type="checkbox" :disabled="!loaded || saving" data-v2="feature-external-pay" /> 对外收款与退款同步</label>
    </div>
    <p class="cv2-field-hint">关闭客户群同步期间的回调会保留，重新开启后补齐；关闭收款同步后重新开启，会补拉关闭期间的账单，超过三天未匹配的收款仍需人工处理。关闭群码生成后，已生成群码仍按原规则到期清理。</p>
    <div class="wn-options">
      <label><input v-model="form.payment_mismatch_enabled" type="checkbox" :disabled="!loaded || saving" data-v2="notify-payment" /> 收款金额不一致通知收款人</label>
      <label><input v-model="form.order_status_enabled" type="checkbox" :disabled="!loaded || saving" data-v2="notify-order" /> 问题单及暂停通知销售客服</label>
    </div>
    <p class="cv2-field-hint">开启后仅通知新出现的情况；销售客服匹配到多人时会分别通知。</p>
    <button type="button" class="cv2-btn cv2-btn--primary" :disabled="!loaded || saving" data-v2="notification-save" @click="saveSettings">{{ saving ? '保存中…' : '保存功能设置' }}</button>
    <span v-if="settingsMessage" class="wn-message" :class="{ 'wn-error': settingsError }" data-v2="notification-settings-message">{{ settingsMessage }}</span>
    <h4 class="wn-subtitle">通知记录</h4>
    <p v-if="loadingRecords" class="cv2-field-hint">正在读取通知记录…</p>
    <p v-else-if="recordsError" class="wn-error" data-v2="notification-records-error">{{ recordsError }}</p>
    <p v-else-if="records.length === 0" class="cv2-field-hint" data-v2="notification-records-empty">暂无通知记录</p>
    <div v-else class="cv2-table-panel" data-v2="notification-records">
      <div class="wn-scroll">
        <table class="cv2-table">
          <thead><tr><th>时间</th><th>类型</th><th>订单号</th><th>内容</th><th>状态</th><th>接收人</th><th>未发送原因</th></tr></thead>
          <tbody><tr v-for="item in records" :key="item.id" :data-v2-notification-row="item.id">
            <td>{{ timeText(item.created_at) }}</td>
            <td>{{ item.kind === 'payment_mismatch' ? '收款金额不一致' : '问题单或暂停' }}</td>
            <td>{{ item.order_no || '—' }}</td><td>{{ item.detail || '—' }}</td>
            <td>{{ item.status === 'sent' ? '已发送' : item.status === 'failed' ? '未全部发送' : '待发送' }}</td>
            <td><template v-if="item.recipients?.length"><div v-for="person in item.recipients" :key="person.userid">{{ person.userid }}：{{ recipientStatus(person.status) }}</div></template><template v-else>—</template></td>
            <td>{{ item.reason || '—' }}</td>
          </tr></tbody>
        </table>
      </div>
      <div class="cv2-pager">
        <span class="cv2-pager-total">第 {{ page }} 页 · 共 {{ total }} 条</span>
        <button type="button" class="cv2-btn cv2-btn--sm" :disabled="page <= 1 || loadingRecords" data-v2="notification-prev" @click="loadRecords(page - 1)">上一页</button>
        <button type="button" class="cv2-btn cv2-btn--sm" :disabled="!hasNext || loadingRecords" data-v2="notification-next" @click="loadRecords(page + 1)">下一页</button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.wn-options { display: flex; flex-wrap: wrap; gap: 12px 28px; margin-bottom: 10px; }
.wn-options label { display: flex; align-items: center; gap: 8px; }
.wn-message { margin-left: 12px; }
.wn-error { color: var(--tt2-st-error); }
.wn-subtitle { margin: 24px 0 12px; }
.wn-scroll { overflow-x: auto; }
</style>
