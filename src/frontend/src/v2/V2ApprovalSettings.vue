<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { fetchApprovalSettings, saveApprovalSettings, type ApprovalSettings, type ApprovalSettingsInput } from '@/api/approvals'

const emit = defineEmits<{ saved: [] }>()
const settings = ref<ApprovalSettingsInput | null>(null)
const users = ref<ApprovalSettings['users']>([])
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const notice = ref('')
const ccCandidates = computed(() => users.value.filter(({ id }) => settings.value?.available_cc_ids.includes(id)))
watch(() => settings.value?.available_cc_ids, (ids) => {
  if (settings.value && ids) settings.value.default_cc_ids = settings.value.default_cc_ids.filter((id) => ids.includes(id))
})

function applyResponse({ users: candidates, ...values }: ApprovalSettings): void {
  settings.value = values
  users.value = candidates
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    applyResponse(await fetchApprovalSettings())
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '审批配置读取失败'
  } finally {
    loading.value = false
  }
}

async function save(): Promise<void> {
  if (!settings.value || saving.value) return
  saving.value = true
  error.value = ''
  notice.value = ''
  try {
    applyResponse(await saveApprovalSettings(settings.value))
    notice.value = '审批配置已保存'
    emit('saved')
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '审批配置保存失败'
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="cv2-panel approval-settings" aria-label="审批配置">
    <p v-if="loading" role="status">正在读取审批配置…</p>
    <form v-else-if="settings" @submit.prevent="save">
      <fieldset :disabled="saving">
        <legend>罚款审批</legend>
        <div class="approval-setting-row">
          <span>罚款审批人</span>
          <el-select v-model="settings.approver_ids" aria-label="罚款审批人" multiple filterable placeholder="选择罚款审批人">
            <el-option v-for="user in users" :key="user.id" :value="user.id" :label="user.name" />
          </el-select>
        </div>
        <div class="approval-setting-row">
          <span>可选抄送人</span>
          <el-select v-model="settings.available_cc_ids" aria-label="可选抄送人" multiple filterable placeholder="选择可抄送的账号">
            <el-option v-for="user in users" :key="user.id" :value="user.id" :label="user.name" />
          </el-select>
        </div>
        <div class="approval-setting-row">
          <span>默认抄送人</span>
          <el-select v-model="settings.default_cc_ids" aria-label="默认抄送人" multiple filterable placeholder="从可选抄送人里选择">
            <el-option v-for="user in ccCandidates" :key="user.id" :value="user.id" :label="user.name" />
          </el-select>
        </div>
      </fieldset>
      <fieldset :disabled="saving">
        <legend>退款处理</legend>
        <div class="approval-setting-row">
          <span>退款处理人</span>
          <el-select v-model="settings.refund_handler_id" :value-on-clear="null" aria-label="退款处理人" filterable clearable placeholder="选择退款处理人">
            <el-option v-for="user in users" :key="user.id" :value="user.id" :label="user.name" />
          </el-select>
        </div>
      </fieldset>
      <button type="submit" class="cv2-btn cv2-btn--primary" :disabled="saving">{{ saving ? '正在保存…' : '保存配置' }}</button>
    </form>
    <p v-if="error" class="approval-setting-error" role="alert">{{ error }}</p>
    <button v-if="error && !settings" type="button" class="cv2-btn" @click="load">重新读取</button>
    <p v-if="notice" role="status">{{ notice }}</p>
  </section>
</template>

<style scoped>
.approval-settings { max-width: 720px; padding: 16px; }
form { display: grid; justify-items: start; gap: 12px; }
fieldset { box-sizing: border-box; width: 100%; min-width: 0; margin: 0; display: grid; gap: 10px; border: 1px solid var(--tt2-line); border-radius: 4px; }
legend { padding: 0 8px; font-weight: 600; }
.approval-setting-row { display: grid; grid-template-columns: 102px minmax(0, 1fr); align-items: center; gap: 8px; }
.approval-setting-error { color: var(--tt2-st-error); }
</style>
