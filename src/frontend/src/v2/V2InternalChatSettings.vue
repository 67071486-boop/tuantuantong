<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { BizError } from '@/api/errors'
import SystemNotificationSettings from './internalChat/SystemNotificationSettings.vue'
import VisitorReceptionSettings from './internalChat/VisitorReceptionSettings.vue'
import {
  getInternalChatSettings, saveInternalChatSettings, type InternalChatSettings,
} from '@/api/internalChatSettings'

const saved = ref<InternalChatSettings>()
const primaryContact = ref<number | ''>('')
const backupContacts = ref<number[]>([])
const backupCandidate = ref<number | ''>('')
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const savedMessage = ref('')

const candidates = computed(() => saved.value?.candidates ?? [])
const candidateNames = computed(() => new Map(candidates.value.map(({ id, name }) => [id, name])))
const availableBackups = computed(() =>
  candidates.value.filter(({ id }) => id !== primaryContact.value && !backupContacts.value.includes(id)),
)
const locked = computed(() => loading.value || saving.value || !saved.value)
const hasChanges = computed(() => {
  const current = saved.value
  return !!current && (
    primaryContact.value !== (current.primary_contact_id ?? '') ||
    backupContacts.value.length !== current.backup_contact_ids.length ||
    backupContacts.value.some((id, index) => id !== current.backup_contact_ids[index])
  )
})

watch(primaryContact, (id) => {
  backupContacts.value = backupContacts.value.filter((backup) => backup !== id)
  if (backupCandidate.value === id) backupCandidate.value = ''
})

function applySettings(settings: InternalChatSettings) {
  saved.value = settings
  primaryContact.value = settings.primary_contact_id ?? ''
  backupContacts.value = [...settings.backup_contact_ids]
  backupCandidate.value = ''
}

async function loadSettings() {
  if (loading.value) return
  loading.value = true
  error.value = ''
  try {
    applySettings(await getInternalChatSettings())
  } catch (cause) {
    error.value = cause instanceof BizError ? cause.message : '读取配置失败，请重试。'
  } finally {
    loading.value = false
  }
}

function addBackup() {
  const id = backupCandidate.value
  if (id === '' || !availableBackups.value.some((item) => item.id === id)) return
  backupContacts.value.push(id)
  backupCandidate.value = ''
}

function moveBackup(index: number, step: -1 | 1) {
  const id = backupContacts.value[index]
  const next = index + step
  if (id === undefined || next < 0 || next >= backupContacts.value.length) return
  backupContacts.value.splice(index, 1)
  backupContacts.value.splice(next, 0, id)
}

async function saveSettings() {
  if (locked.value || !hasChanges.value) return
  saving.value = true
  error.value = ''
  savedMessage.value = ''
  try {
    applySettings(await saveInternalChatSettings({
      primary_contact_id: primaryContact.value === '' ? null : primaryContact.value,
      backup_contact_ids: [...backupContacts.value],
    }))
    savedMessage.value = '配置已保存'
  } catch (cause) {
    error.value = cause instanceof BizError ? cause.message : '保存配置失败，请重试。'
  } finally {
    saving.value = false
  }
}

onMounted(loadSettings)
</script>

<template>
  <section class="cv2-panel chat-directory" aria-labelledby="chat-directory-title">
    <div class="cv2-panel-head">
      <h2 id="chat-directory-title" class="cv2-panel-title">打手通讯录</h2>
      <button
        v-if="!saved && error"
        type="button"
        class="cv2-btn"
        aria-label="重新读取通讯录配置"
        @click="loadSettings"
      >重新读取</button>
    </div>
    <p class="chat-directory-hint">可从本工作室在职的非打手账号中选人。打手可向选中的联系人发起私聊，首选排在最前。</p>
    <p v-if="loading" role="status" class="chat-directory-hint">正在读取配置…</p>
    <p v-else-if="saved && !candidates.length" class="chat-directory-hint">当前没有可选联系人。</p>
    <form class="chat-directory-form" @submit.prevent="saveSettings">
      <div class="chat-directory-field">
        <label for="chat-primary-contact">首选客服</label>
        <select id="chat-primary-contact" v-model="primaryContact" class="cv2-input" :disabled="locked">
          <option value="">未设置</option>
          <option v-for="{ id, name } in candidates" :key="id" :value="id">{{ name }}</option>
        </select>
      </div>
      <div class="chat-directory-field">
        <label for="chat-backup-contact">备用客服</label>
        <div class="chat-directory-controls">
          <select
            id="chat-backup-contact"
            v-model="backupCandidate"
            class="cv2-input"
            :disabled="locked || !availableBackups.length"
          >
            <option value="">{{ availableBackups.length ? '选择备用客服' : '没有其他可选联系人' }}</option>
            <option v-for="{ id, name } in availableBackups" :key="id" :value="id">{{ name }}</option>
          </select>
          <button
            type="button"
            class="cv2-btn"
            aria-label="添加备用客服"
            :disabled="locked || backupCandidate === ''"
            @click="addBackup"
          >添加</button>
        </div>
      </div>
      <ol v-if="backupContacts.length" class="chat-directory-backups" aria-label="备用客服顺序">
        <li v-for="(id, index) in backupContacts" :key="id">
          <div class="chat-directory-row">
            <span>{{ candidateNames.get(id) }}</span>
            <div class="chat-directory-controls">
              <button
                type="button"
                class="cv2-btn cv2-btn--sm"
                :aria-label="`${candidateNames.get(id)}上移`"
                :disabled="locked || index === 0"
                @click="moveBackup(index, -1)"
              >上移</button>
              <button
                type="button"
                class="cv2-btn cv2-btn--sm"
                :aria-label="`${candidateNames.get(id)}下移`"
                :disabled="locked || index === backupContacts.length - 1"
                @click="moveBackup(index, 1)"
              >下移</button>
              <button
                type="button"
                class="cv2-btn cv2-btn--sm"
                :aria-label="`移除备用${candidateNames.get(id)}`"
                :disabled="locked"
                @click="backupContacts.splice(index, 1)"
              >移除</button>
            </div>
          </div>
        </li>
      </ol>
      <div class="chat-directory-actions">
        <button type="submit" class="cv2-btn cv2-btn--primary" :disabled="locked || !hasChanges">
          {{ saving ? '保存中…' : '保存配置' }}
        </button>
        <p v-if="error" class="chat-directory-error" role="alert">{{ error }}</p>
        <p v-else-if="savedMessage && !hasChanges" role="status">{{ savedMessage }}</p>
      </div>
    </form>
  </section>
  <VisitorReceptionSettings />
  <SystemNotificationSettings />
</template>

<style scoped>
.chat-directory {
  max-width: 780px;
}

.chat-directory-hint {
  color: var(--tt2-dim);
  margin: 8px 0 16px;
}

.chat-directory-form {
  display: grid;
  gap: 14px;
}

.chat-directory-field {
  display: grid;
  gap: 6px;
}

.chat-directory-controls,
.chat-directory-actions,
.chat-directory-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.chat-directory-field select {
  width: min(100%, 280px);
}

.chat-directory-backups {
  margin: 0;
  padding-inline-start: 28px;
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
}

.chat-directory-backups li {
  padding: 8px 8px 8px 0;
}

.chat-directory-backups li + li {
  border-top: 1px solid var(--tt2-line);
}

.chat-directory-row {
  justify-content: space-between;
}

.chat-directory-actions p {
  margin: 0;
}

.chat-directory-error {
  color: var(--tt2-st-error);
}
</style>
