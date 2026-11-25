<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { BizError, HttpError } from '@/api/errors'
import {
  getSystemNotificationSettings, saveSystemNotificationSettings, type SystemNotificationRules, type SystemNotificationSettings,
} from '@/api/internalChatSettings'

const rules = [
  { key: 'approval_submitted', title: '提交审批', description: '通知指定审批人。' },
  { key: 'approval_approved', title: '审批通过', description: '通知审批发起人。' },
  { key: 'wecom_aftersales', title: '企微售后纠纷', description: '命中已配置的售后规则时通知对应人员。' },
  { key: 'order_grab_qr', title: '接单成功后发送群二维码', description: '通知接单打手。有订单群二维码或详情页的企微群码就随通知发送，两处都没有时提示联系通讯录客服。' },
] as const satisfies readonly { key: keyof SystemNotificationRules; title: string; description: string }[]

const saved = ref<SystemNotificationSettings>()
const draft = ref<SystemNotificationSettings>()
const loading = ref(false)
const saving = ref(false)
const unavailable = ref(false)
const supported = computed(() => saved.value?.available !== false)
const error = ref('')
const confirmed = ref(false)

const changes = computed(() => {
  const next: Partial<SystemNotificationRules> = {}
  const current = draft.value
  const previous = saved.value
  if (current && previous) {
    for (const { key } of rules) {
      if (current[key] !== previous[key]) next[key] = current[key]
    }
  }
  return next
})
const hasChanges = computed(() => Object.keys(changes.value).length > 0)

function apply(settings: SystemNotificationSettings) {
  if (!settings || rules.some(({ key }) => typeof settings[key] !== 'boolean')) {
    throw new Error('系统通知配置内容异常')
  }
  saved.value = settings
  draft.value = { ...settings }
}

async function load() {
  if (loading.value) return
  loading.value = true
  error.value = ''
  try {
    apply(await getSystemNotificationSettings())
  } catch (cause) {
    unavailable.value = cause instanceof HttpError && cause.status === 404
    if (!unavailable.value) error.value = '读取系统通知规则失败，请重试。'
  } finally {
    loading.value = false
  }
}

async function save() {
  if (!draft.value || saving.value || !supported.value || !hasChanges.value) return
  saving.value = true
  confirmed.value = false
  error.value = ''
  try {
    apply(await saveSystemNotificationSettings(changes.value))
    confirmed.value = true
  } catch (cause) {
    error.value = cause instanceof BizError ? cause.message : '保存系统通知规则失败，请重试。'
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <section v-if="!unavailable" class="cv2-panel notice-settings" aria-labelledby="chat-system-notices-title">
    <div class="cv2-panel-head">
      <h2 id="chat-system-notices-title" class="cv2-panel-title">系统通知规则</h2>
      <button v-if="!draft && error" type="button" class="cv2-btn" @click="load">重新读取</button>
    </div>
    <p v-if="loading" class="notice-settings-hint" role="status">正在读取配置…</p>
    <p v-if="!supported" class="notice-settings-hint" role="status">当前聊天服务暂不支持发送系统通知。</p>
    <form v-if="draft" @submit.prevent="save">
      <ul class="notice-settings-rules">
        <li v-for="rule in rules" :key="rule.key" class="notice-settings-rule">
          <div>
            <label :for="`chat-system-notice-${rule.key}`">{{ rule.title }}</label>
            <p :id="`chat-system-notice-hint-${rule.key}`" class="notice-settings-hint">{{ rule.description }}</p>
          </div>
          <input :id="`chat-system-notice-${rule.key}`" v-model="draft[rule.key]" type="checkbox" role="switch"
            :aria-describedby="`chat-system-notice-hint-${rule.key}`" :disabled="saving || !supported" @change="confirmed = false" />
        </li>
      </ul>
      <div class="notice-settings-actions">
        <button type="submit" class="cv2-btn cv2-btn--primary" :disabled="saving || !supported || !hasChanges">
          {{ saving ? '保存中…' : '保存通知规则' }}
        </button>
        <span v-if="confirmed" role="status">系统通知规则已保存</span>
      </div>
    </form>
    <p v-if="error" class="notice-settings-error" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
.notice-settings { max-width: 780px; margin-top: 16px; }
.notice-settings-rules { margin: 0 0 14px; padding: 0; list-style: none; }
.notice-settings-rule {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding-block: 10px;
  border-bottom: 1px solid var(--tt2-line);
}
.notice-settings-hint { color: var(--tt2-dim); margin: 4px 0 0; }
.notice-settings-rule input { flex: none; margin: 0; accent-color: var(--tt2-primary); }
.notice-settings-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.notice-settings-error { color: var(--tt2-st-error); }
</style>
