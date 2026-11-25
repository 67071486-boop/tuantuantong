<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { BizError, HttpError } from '@/api/errors'
import { getVisitorSettings, saveVisitorSettings, type VisitorGreeting, type VisitorSettings } from '@/api/internalChatVisitors'
import VisitorSeatSettings from './VisitorSeatSettings.vue'

const reloadMessage = '欢迎语配置版本已变更，请刷新页面后再配置。'
const saved = ref<VisitorSettings>()
const greetings = ref<VisitorGreeting[]>([])
const removedContactIds = ref(new Set<number>())
const editing = ref<number>()
const candidate = ref<number | ''>('')
const loading = ref(false)
const saving = ref(false)
const unavailable = ref(false)
const needsReload = ref(false)
const error = ref('')
const savedMessage = ref(false)
const names = computed(() => new Map(saved.value?.candidates.map(({ id, name }) => [id, name])))
const available = computed(() => {
  const selected = new Set(greetings.value.map(({ contact_id }) => contact_id))
  return (saved.value?.candidates ?? []).filter(({ id }) => !selected.has(id))
})
const unavailableContacts = computed(() => (saved.value?.unavailable_contacts ?? [])
  .filter(({ id }) => !removedContactIds.value.has(id)))
const locked = computed(() => !saved.value || loading.value || saving.value || needsReload.value)
const hasChanges = computed(() => {
  const previous = saved.value?.greetings
  return !!previous && (
    removedContactIds.value.size > 0 || previous.length !== greetings.value.length ||
    greetings.value.some((greeting, index) => {
      const prior = previous[index]
      return prior?.contact_id !== greeting.contact_id ||
        prior.enabled !== greeting.enabled || prior.text !== greeting.text
    })
  )
})
const greetingIncomplete = computed(() => greetings.value.some(needsGreetingText))

function needsGreetingText(greeting: VisitorGreeting) {
  return greeting.enabled && !greeting.text.trim()
}

function contactName(id: number) {
  return names.value.get(id) ?? '账号 ' + id
}

function promptReload() {
  needsReload.value = true
  error.value = reloadMessage
}

function apply(settings: VisitorSettings) {
  if (!Array.isArray(settings.greetings)) {
    promptReload()
    return false
  }
  needsReload.value = false
  const byContact = new Map(settings.greetings.map(greeting => [greeting.contact_id, greeting]))
  const ordered = settings.contact_ids.map(contact_id => byContact.get(contact_id) ?? { contact_id, enabled: false, text: '' })
  saved.value = { ...settings, greetings: ordered }
  greetings.value = ordered.map(greeting => ({ ...greeting }))
  removedContactIds.value.clear()
  if (editing.value !== undefined && !settings.contact_ids.includes(editing.value)) editing.value = undefined
  candidate.value = ''
  return true
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    apply(await getVisitorSettings())
  } catch (cause) {
    unavailable.value = cause instanceof HttpError && cause.status === 404
    if (!unavailable.value) error.value = '读取访客接待配置失败，请重试。'
  } finally {
    loading.value = false
  }
}

function addContact() {
  const id = candidate.value
  if (id === '' || !available.value.some(item => item.id === id)) return
  const previous = saved.value?.greetings.find(greeting => greeting.contact_id === id)
  greetings.value.push(previous ? { ...previous } : { contact_id: id, enabled: false, text: '' })
  removedContactIds.value.delete(id)
  candidate.value = ''
}

function removeContact(id: number) {
  if (saved.value?.contact_ids.includes(id) || unavailableContacts.value.some(contact => contact.id === id)) {
    removedContactIds.value.add(id)
  }
  greetings.value = greetings.value.filter(({ contact_id }) => contact_id !== id)
  if (editing.value === id) editing.value = undefined
}

function openEmptyGreeting(greeting: VisitorGreeting) {
  if (needsGreetingText(greeting)) editing.value = greeting.contact_id
}

async function isLegacySettings() {
  try {
    return !Array.isArray((await getVisitorSettings()).greetings)
  } catch {
    return false
  }
}

async function save() {
  if (locked.value || !hasChanges.value || greetingIncomplete.value) return
  saving.value = true
  error.value = ''
  savedMessage.value = false
  try {
    savedMessage.value = apply(await saveVisitorSettings({
      contact_ids: greetings.value.map(({ contact_id }) => contact_id),
      greetings: greetings.value,
      removed_contact_ids: [...removedContactIds.value],
    }))
  } catch (cause) {
    // 单人欢迎语版本会报缺少旧版必填参数。
    if (cause instanceof BizError && cause.code === 1000 && await isLegacySettings()) {
      promptReload()
    } else {
      error.value = cause instanceof BizError ? cause.message : '保存访客接待配置失败，请重试。'
    }
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <VisitorSeatSettings v-if="saved && Array.isArray(saved.seats)" :settings="saved" @saved="apply" />
  <section v-if="!unavailable && !saved?.seats?.length" class="cv2-panel visitor-settings" aria-labelledby="visitor-settings-title">
    <div class="cv2-panel-head">
      <h2 id="visitor-settings-title" class="cv2-panel-title">访客接待</h2>
      <button v-if="!saved && error" type="button" class="cv2-btn" @click="load">重新读取</button>
    </div>
    <p class="visitor-settings-hint">访客首次注册时，每位开启欢迎语的接待人员会各发一条。关闭欢迎语后，访客仍可主动联系该人员。</p>
    <p v-if="loading" role="status">正在读取配置…</p>
    <form v-if="saved" class="visitor-settings-form" @submit.prevent="save">
      <div class="visitor-settings-field">
        <label for="visitor-reception-contact">接待人员</label>
        <div class="visitor-settings-actions">
          <select id="visitor-reception-contact" v-model="candidate" class="cv2-input" :disabled="locked || !available.length">
            <option value="">{{ available.length ? '选择接待人员' : '没有其他可选人员' }}</option>
            <option v-for="{ id, name } in available" :key="id" :value="id">{{ name }}</option>
          </select>
          <button type="button" class="cv2-btn" aria-label="添加接待人员" :disabled="locked || candidate === ''" @click="addContact">添加</button>
        </div>
        <ul v-if="greetings.length || unavailableContacts.length" class="visitor-settings-contacts">
          <li v-for="greeting in greetings" :key="greeting.contact_id">
            <div class="visitor-settings-contact">
              <span>{{ contactName(greeting.contact_id) }}</span>
              <div class="visitor-settings-actions">
                <label class="visitor-settings-toggle">
                  <input :id="`visitor-greeting-enabled-${greeting.contact_id}`" v-model="greeting.enabled" type="checkbox"
                    :aria-label="`启用${contactName(greeting.contact_id)}的欢迎语`" :disabled="locked"
                    @change="openEmptyGreeting(greeting)" />
                  自动发欢迎语
                </label>
                <button type="button" class="cv2-btn cv2-btn--sm" :disabled="locked"
                  :aria-label="`${editing === greeting.contact_id ? '收起' : '编辑'}${contactName(greeting.contact_id)}的欢迎语`"
                  :aria-expanded="editing === greeting.contact_id"
                  :aria-controls="`visitor-greeting-editor-${greeting.contact_id}`"
                  @click="editing = editing === greeting.contact_id ? undefined : greeting.contact_id">
                  {{ editing === greeting.contact_id ? '收起' : '编辑欢迎语' }}
                </button>
                <button type="button" class="cv2-btn cv2-btn--sm" :disabled="locked"
                  :aria-label="`移除${contactName(greeting.contact_id)}`"
                  @click="removeContact(greeting.contact_id)">移除</button>
              </div>
            </div>
            <div v-show="editing === greeting.contact_id" :id="`visitor-greeting-editor-${greeting.contact_id}`"
              class="visitor-settings-field">
              <template v-if="editing === greeting.contact_id">
                <label :for="`visitor-greeting-text-${greeting.contact_id}`">{{ contactName(greeting.contact_id) }}的欢迎语</label>
                <textarea :id="`visitor-greeting-text-${greeting.contact_id}`" v-model="greeting.text" class="cv2-input" rows="3"
                  maxlength="4000" :disabled="locked" :aria-invalid="needsGreetingText(greeting)"
                  placeholder="例如：你好，我是人事客服，请问有什么能帮你？" />
              </template>
            </div>
          </li>
          <li v-for="contact in unavailableContacts" :key="contact.id">
            <div class="visitor-settings-contact">
              <div class="visitor-settings-actions">
                <span>{{ contact.name }}</span>
                <span class="visitor-settings-hint">暂不可接待</span>
              </div>
              <button type="button" class="cv2-btn cv2-btn--sm" :disabled="locked"
                :aria-label="`移除${contact.name}`" @click="removeContact(contact.id)">移除</button>
            </div>
          </li>
        </ul>
      </div>
      <div class="visitor-settings-actions">
        <button type="submit" class="cv2-btn cv2-btn--primary" :disabled="locked || !hasChanges || greetingIncomplete">
          {{ saving ? '保存中…' : '保存访客配置' }}
        </button>
        <p v-if="greetingIncomplete" class="visitor-settings-hint">请填写已开启人员的欢迎语。</p>
        <p v-else-if="savedMessage && !hasChanges" role="status">访客配置已保存</p>
      </div>
    </form>
    <p v-if="error" role="alert" class="visitor-settings-error">{{ error }}</p>
  </section>
</template>

<style scoped>
.visitor-settings { max-width: 780px; margin-top: 16px; }
.visitor-settings-hint { color: var(--tt2-dim); }
.visitor-settings-form, .visitor-settings-field { display: grid; gap: 12px; }
.visitor-settings-field { gap: 6px; }
.visitor-settings-field select { width: min(100%, 280px); }
.visitor-settings-field textarea { height: auto; padding-block: 8px; resize: vertical; }
.visitor-settings-actions, .visitor-settings-contact, .visitor-settings-toggle {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.visitor-settings-contacts { margin: 0; padding: 0; list-style: none; }
.visitor-settings-contacts li { display: grid; gap: 8px; padding: 8px 12px; border-bottom: 1px solid var(--tt2-line); }
.visitor-settings-contact { justify-content: space-between; }
.visitor-settings-toggle input { margin: 0; accent-color: var(--tt2-primary); }
.visitor-settings-actions p { margin: 0; }
.visitor-settings-error { color: var(--tt2-st-error); }
</style>
