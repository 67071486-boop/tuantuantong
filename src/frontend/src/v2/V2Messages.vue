<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElSwitch } from 'element-plus/es'
import 'element-plus/es/components/switch/style/css'
import { request } from '@/api/http'
import { useWxPusherBinding } from '@/composables/useWxPusherBinding'

type StaffEvent = 'approval_assigned' | 'refund_assigned'
type StaffPreferences = { enabled: boolean; events: Record<StaffEvent, boolean>; group_chat_enabled?: boolean }

const staffUrl = '/api/auth/wxpusher/staff'
const eventOptions: { key: StaffEvent; title: string; description: string }[] = [
  { key: 'approval_assigned', title: '审批待处理', description: '审批单指派给你时提醒。' },
  { key: 'refund_assigned', title: '退款待处理', description: '退款处理任务指派给你时提醒。' },
]

const {
  guide, loading, failed, challenge, expired, pendingAction, busy, bindingError,
  confirmingUnbind, testNoticeAccepted, loadGuide, startBinding, sendTestNotice, unbind,
} = useWxPusherBinding(staffUrl)

const preferences = ref<StaffPreferences | null>(null)
const saved = ref<StaffPreferences | null>(null)
const saving = ref(false)
const preferencesLoading = ref(true)
const prefsError = ref('')
const savedMessage = ref(false)

const changed = computed(() =>
  !!preferences.value && !!saved.value && (
    preferences.value.enabled !== saved.value.enabled
    || eventOptions.some(({ key }) => preferences.value?.events[key] !== saved.value?.events[key])
  ),
)

function acceptPrefs(result: StaffPreferences): void {
  saved.value = result
  preferences.value = { ...result, events: { ...result.events } }
}

async function loadPreferences(): Promise<void> {
  preferencesLoading.value = true
  prefsError.value = ''
  try {
    acceptPrefs(await request<StaffPreferences>({ url: `${staffUrl}/preferences`, method: 'GET' }))
  } catch {
    prefsError.value = '无法读取提醒设置，请重试。'
  } finally {
    preferencesLoading.value = false
  }
}

async function savePreferences(): Promise<void> {
  if (!preferences.value || !saved.value || saving.value || !changed.value) return
  const events = Object.fromEntries(eventOptions
    .filter(({ key }) => preferences.value?.events[key] !== saved.value?.events[key])
    .map(({ key }) => [key, preferences.value?.events[key]]))
  const data = {
    ...(preferences.value.enabled !== saved.value.enabled ? { enabled: preferences.value.enabled } : {}),
    ...(Object.keys(events).length ? { events } : {}),
  }
  saving.value = true
  savedMessage.value = false
  prefsError.value = ''
  try {
    acceptPrefs(await request<StaffPreferences>({ url: `${staffUrl}/preferences`, method: 'PATCH', data }))
    savedMessage.value = true
  } catch (error) {
    prefsError.value = error instanceof Error && error.message ? error.message : '未能确认保存，请重试。'
  } finally {
    saving.value = false
  }
}

onMounted(loadPreferences)
</script>

<template>
  <main class="cv2-page staff-notifications" data-v2-page="staff-notifications">
    <header class="staff-notifications__heading">
      <h1>消息通知</h1>
      <p>绑定 WxPusher 后，手机可以收到指派给你的待办提醒。</p>
    </header>

    <div class="staff-notifications__grid">
      <section class="cv2-panel staff-notifications__panel" aria-labelledby="staff-binding-title" :aria-busy="loading || busy">
        <header class="cv2-panel-head staff-notifications__panel-head">
          <h2 id="staff-binding-title" class="cv2-panel-title">手机推送</h2>
          <span v-if="guide && !loading && !failed" class="cv2-st" :class="guide.bound ? 'cv2-st--doing' : 'cv2-st--unassigned'">
            {{ guide.bound ? '已绑定' : '未绑定' }}
          </span>
        </header>

        <div class="staff-notifications__download">
          <h3>下载 WxPusher</h3>
          <p>在手机上安装并登录 WxPusher，开启系统通知。</p>
          <div class="staff-notifications__buttons">
            <a class="cv2-btn" href="https://wxpusher.zjiecode.com/download/" target="_blank" rel="noopener noreferrer">安卓官网下载</a>
            <a class="cv2-btn" href="https://apps.apple.com/cn/app/id6444387603" target="_blank" rel="noopener noreferrer">iPhone App Store</a>
          </div>
        </div>

        <div class="staff-notifications__binding">
          <h3>绑定当前账号</h3>
          <p v-if="loading" role="status">正在读取绑定状态…</p>
          <template v-else-if="failed">
            <p role="status">无法读取绑定状态，请重试。</p>
            <button class="cv2-btn" type="button" @click="loadGuide">重试</button>
          </template>
          <template v-else-if="guide?.bound">
            <p>当前团团通账号已关联 WxPusher。</p>
            <p v-if="confirmingUnbind">解绑后此账号将暂停接收 WxPusher 手机通知，可以再次扫码绑定。</p>
            <div v-if="confirmingUnbind" class="staff-notifications__buttons">
              <button class="cv2-btn" type="button" :disabled="busy" @click="confirmingUnbind = false; bindingError = ''">取消</button>
              <button class="cv2-btn cv2-btn--danger" type="button" :disabled="busy" @click="unbind">
                {{ pendingAction === 'unbind' ? '正在解绑…' : '确认解绑' }}
              </button>
            </div>
            <div v-else class="staff-notifications__buttons">
              <button v-if="guide.binding_enabled" class="cv2-btn cv2-btn--primary" type="button" :disabled="busy"
                @click="sendTestNotice">{{ pendingAction === 'test' ? '正在发送…' : '发送测试消息' }}</button>
              <button class="cv2-btn" type="button" :disabled="busy" @click="confirmingUnbind = true">解绑</button>
            </div>
            <p v-if="testNoticeAccepted" role="status">WxPusher 已受理，请在手机上确认是否收到。</p>
          </template>
          <p v-else-if="!guide?.binding_enabled">请联系工作室管理员开通 WxPusher 手机推送，再回来绑定。</p>
          <template v-else-if="challenge && !expired">
            <img :src="challenge.qr_url" class="staff-notifications__qr" width="160" height="160"
              alt="WxPusher 个人绑定二维码" referrerpolicy="no-referrer">
            <p role="status">在 WxPusher 扫码后回到本页，系统会自动确认绑定。</p>
            <button class="cv2-btn" type="button" :disabled="busy" @click="startBinding">
              {{ pendingAction === 'start' ? '正在换码…' : '换一个绑定码' }}
            </button>
          </template>
          <template v-else>
            <p v-if="expired" role="status">二维码已过期，请重新获取。</p>
            <p v-else>获取二维码后，用手机上的 WxPusher 扫码。</p>
            <button class="cv2-btn cv2-btn--primary" type="button" :disabled="busy" @click="startBinding">
              {{ pendingAction === 'start' ? '正在获取…' : '获取个人绑定码' }}
            </button>
          </template>
          <p v-if="bindingError" class="staff-notifications__error" role="alert">{{ bindingError }}</p>
        </div>
      </section>

      <section class="cv2-panel staff-notifications__panel" aria-labelledby="staff-preferences-title" :aria-busy="preferencesLoading || saving">
        <header class="cv2-panel-head staff-notifications__panel-head">
          <h2 id="staff-preferences-title" class="cv2-panel-title">员工提醒</h2>
        </header>
        <p v-if="guide?.binding_enabled && guide.workspace_enabled === false">工作室目前暂停了手机推送，请联系管理员。</p>
        <p v-if="preferencesLoading" role="status">正在读取提醒设置…</p>
        <button v-else-if="!preferences" class="cv2-btn" type="button" @click="loadPreferences">重新读取</button>
        <form v-else class="staff-notifications__preferences" @submit.prevent="savePreferences">
          <div class="staff-notifications__option">
            <label for="staff-notice-enabled">接收员工待办提醒</label>
            <ElSwitch id="staff-notice-enabled" v-model="preferences.enabled" :disabled="saving" />
          </div>
          <div v-for="event in eventOptions" :key="event.key" class="staff-notifications__option">
            <div>
              <label :for="`staff-notice-${event.key}`">{{ event.title }}</label>
              <p>{{ event.description }}</p>
            </div>
            <ElSwitch :id="`staff-notice-${event.key}`" v-model="preferences.events[event.key]" :disabled="saving" />
          </div>
          <div v-if="preferences.group_chat_enabled" class="staff-notifications__option">
            <div>
              <span class="staff-notifications__option-title">聊天提醒</span>
              <p>绑定 WxPusher 后，单聊和你参与的订单群有未读消息时会收到提醒。</p>
            </div>
            <span class="cv2-st cv2-st--doing">已开启</span>
          </div>
          <div class="staff-notifications__buttons">
            <button class="cv2-btn cv2-btn--primary" type="submit" :disabled="!changed || saving">
              {{ saving ? '正在保存…' : '保存设置' }}
            </button>
            <span v-if="savedMessage && !changed" role="status">已保存</span>
          </div>
        </form>
        <p v-if="prefsError" class="staff-notifications__error" role="alert">{{ prefsError }}</p>
      </section>
    </div>
  </main>
</template>

<style scoped>
.staff-notifications { color: var(--tt2-ink); }
.staff-notifications__heading { margin-bottom: 16px; }
.staff-notifications__heading h1 { margin: 0 0 8px; font-size: 22px; font-weight: 650; }
.staff-notifications__heading p,
.staff-notifications__panel p { margin: 0; color: var(--tt2-dim); font-size: 13px; line-height: 1.65; }
.staff-notifications__grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); align-items: start; gap: 16px; }
.staff-notifications__panel { display: grid; gap: 16px; padding: 16px; }
.staff-notifications__panel-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin: 0; }
.staff-notifications__panel-head h2 { margin: 0; }
.staff-notifications__download,
.staff-notifications__binding,
.staff-notifications__preferences { display: grid; justify-items: start; gap: 10px; }
.staff-notifications__binding { border-top: 1px solid var(--tt2-line); padding-top: 16px; }
.staff-notifications__download h3,
.staff-notifications__binding h3 { margin: 0; font-size: 14px; }
.staff-notifications__buttons { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.staff-notifications__buttons a { text-decoration: none; }
.staff-notifications__qr { border: 1px solid var(--tt2-line); border-radius: 6px; background: #fff; padding: 6px; }
.staff-notifications__option { display: flex; justify-content: space-between; align-items: center; gap: 12px; width: 100%; padding: 8px 0; border-bottom: 1px solid var(--tt2-line); }
.staff-notifications__option label,
.staff-notifications__option-title { font-size: 14px; font-weight: 600; }
.staff-notifications__panel p.staff-notifications__error { color: var(--tt2-st-error); }
@media (max-width: 780px) { .staff-notifications__grid { grid-template-columns: 1fr; } }
</style>
