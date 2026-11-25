<script setup lang="ts">
import { ElSwitch } from 'element-plus/es'
import 'element-plus/es/components/switch/style/css'
import { computed, onMounted, reactive, ref } from 'vue'
import { BizError } from '@/api/errors'
import {
  getWxPusherConfig, listWxPusherPreviews, listWxPusherRecipients,
  saveWxPusherConfig, sendWxPusherTest,
  type WxPusherConfig, type WxPusherConfigChange, type WxPusherOrderEvent,
  type WxPusherPreview, type WxPusherRecipient,
} from '@/api/wxpusherConfig'
import OrderTabDialog from './OrderTabDialog.vue'
import V2WxPusherDeliveryHistory from './V2WxPusherDeliveryHistory.vue'
import './integration-form.css'

const ORDER_EVENTS = [
  ['assigned', '新订单指派'],
  ['unassigned', '撤销指派'],
  ['review_rejected', '订单提交验收不通过'],
  ['completed', '订单已完成'],
  ['paused_resumed', '订单暂停或恢复'],
  ['issue', '订单标为问题单'],
  ['refunded', '订单已退单'],
] as const satisfies ReadonlyArray<readonly [WxPusherOrderEvent, string]>

const config = ref<WxPusherConfig | null>(null)
const loading = ref(false)
const saving = ref(false)
const configFeedback = ref<{ text: string; error?: boolean }>()
const clipboardFeedback = ref('')
const guideOpen = ref(false)
const showLoginKey = ref(false)
const form = reactive({
  app_id: '',
  subscribe_url: '',
  app_token: '',
  login_signing_key: '',
  push_enabled: false,
  login_enabled: false,
  binding_prompt_enabled: false,
  order_enabled: true,
  settlement_enabled: true,
  order_events: {} as Partial<WxPusherConfig['order_events']>,
})

const switchingApp = computed(() => !!config.value?.app_id && form.app_id.trim() !== config.value.app_id)
const changes = computed<WxPusherConfigChange>(() => {
  const previous = config.value
  if (!previous) return {}
  const data: WxPusherConfigChange = {}
  for (const key of ['app_id', 'subscribe_url'] as const) {
    const value = form[key].trim()
    if (value !== previous[key]) data[key] = value
  }
  for (const key of ['push_enabled', 'login_enabled', 'binding_prompt_enabled', 'order_enabled', 'settlement_enabled'] as const) {
    if (form[key] !== previous[key]) data[key] = form[key]
  }
  for (const key of ['app_token', 'login_signing_key'] as const) {
    const value = form[key]
    if (value) data[key] = value
  }
  if (switchingApp.value) data.login_enabled = form.login_enabled && Boolean(form.login_signing_key)
  for (const [key] of ORDER_EVENTS) {
    const value = form.order_events[key]
    if (value !== undefined && value !== previous.order_events[key]) (data.order_events ??= {})[key] = value
  }
  return data
})
const dirty = computed(() => Object.keys(changes.value).length > 0)
const recipients = ref<WxPusherRecipient[]>([])
const recipientId = ref<number | ''>('')
const loadingRecipients = ref(false)
const recipientsError = ref(false)
let recipientsRequest = 0
const sending = ref(false)
const formLocked = computed(() => saving.value || sending.value)
const sendFeedback = ref<{ text: string; error?: boolean }>()
const historyRefresh = ref(0)
const previewOpen = ref(false)
const loadingPreviews = ref(false)
const previewsError = ref(false)
const previews = ref<WxPusherPreview[]>([])
const previewKind = ref('')
const currentPreview = computed(() => previews.value.find(({ kind }) => kind === previewKind.value))
const previewDocument = computed(() => {
  const document = new DOMParser().parseFromString(currentPreview.value?.html ?? '', 'text/html')
  for (const anchor of document.querySelectorAll('a')) anchor.removeAttribute('href')
  return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">' +
    '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; form-action \'none\'">' +
    '<style>body{margin:12px;font:14px/1.75 system-ui,sans-serif;overflow-wrap:anywhere}a{color:inherit;text-decoration:underline}</style>' +
    '</head><body>' + document.body.innerHTML + '</body></html>'
})

function errorText(error: unknown, fallback: string): string {
  return error instanceof BizError ? error.message : fallback
}

function applyConfig(next: WxPusherConfig): void {
  const bindingPromptEnabled = next.binding_prompt_enabled === true
  config.value = { ...next, binding_prompt_enabled: bindingPromptEnabled }
  Object.assign(form, {
    app_id: next.app_id, subscribe_url: next.subscribe_url,
    app_token: '', login_signing_key: '', push_enabled: next.push_enabled,
    login_enabled: next.login_enabled, binding_prompt_enabled: bindingPromptEnabled, order_enabled: next.order_enabled,
    settlement_enabled: next.settlement_enabled, order_events: { ...next.order_events },
  })
  showLoginKey.value = false
  clipboardFeedback.value = ''
}

async function loadConfig(): Promise<void> {
  loading.value = true
  configFeedback.value = undefined
  try {
    applyConfig(await getWxPusherConfig())
    if (config.value?.configured) void loadRecipients()
  } catch {
    configFeedback.value = { text: '读取设置失败，请重试。', error: true }
  } finally {
    loading.value = false
  }
}

async function save(): Promise<void> {
  if (!config.value || !dirty.value || saving.value) return
  if (switchingApp.value && (!changes.value.app_id || !changes.value.app_token)) {
    configFeedback.value = { text: '更换应用时，请填写新应用的 App ID 和 appToken。', error: true }
    return
  }
  saving.value = true
  configFeedback.value = undefined
  try {
    const saved = await saveWxPusherConfig(changes.value, config.value.app_id)
    const appChanged = saved.app_id !== config.value.app_id
    applyConfig(saved)
    configFeedback.value = { text: '设置已保存' }
    if (appChanged) {
      recipients.value = []
      recipientId.value = ''
      sendFeedback.value = undefined
    }
    if (saved.configured) void loadRecipients()
  } catch (error) {
    configFeedback.value = { text: errorText(error, '未能确认保存，请重试。'), error: true }
  } finally {
    saving.value = false
  }
}

async function copy(text: string, label: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
    clipboardFeedback.value = `${label}已复制`
  } catch {
    clipboardFeedback.value = '无法自动复制，请手动选中后复制。'
  }
}

function generateLoginKey(): void {
  form.login_signing_key = Array.from(crypto.getRandomValues(new Uint8Array(16)),
    (byte) => byte.toString(16).padStart(2, '0')).join('')
  clipboardFeedback.value = '验证密钥已生成。请复制到 WxPusher 的系统对接设置。'
}

async function loadRecipients(): Promise<void> {
  const request = ++recipientsRequest
  loadingRecipients.value = true
  recipientsError.value = false
  try {
    const { items } = await listWxPusherRecipients()
    if (request !== recipientsRequest) return
    recipients.value = items
    if (!recipients.value.some(({ id }) => id === recipientId.value)) recipientId.value = ''
  } catch {
    if (request !== recipientsRequest) return
    recipientsError.value = true
    recipients.value = []
    recipientId.value = ''
  } finally {
    if (request === recipientsRequest) loadingRecipients.value = false
  }
}

async function sendTest(): Promise<void> {
  if (!config.value || typeof recipientId.value !== 'number' || dirty.value || saving.value || sending.value) return
  sending.value = true
  sendFeedback.value = undefined
  try {
    const { accepted } = await sendWxPusherTest(recipientId.value, config.value.app_id)
    sendFeedback.value = accepted
      ? { text: 'WxPusher 已受理，请让打手确认手机是否收到。' }
      : { text: 'WxPusher 未确认受理，请检查设置后重试。', error: true }
  } catch (error) {
    sendFeedback.value = { text: errorText(error, '未能确认发送结果，请查看发送记录。'), error: true }
  } finally {
    sending.value = false
    historyRefresh.value += 1
  }
}

async function openPreviews(): Promise<void> {
  previewOpen.value = true
  if (previews.value.length || loadingPreviews.value) return
  loadingPreviews.value = true
  previewsError.value = false
  try {
    previews.value = (await listWxPusherPreviews()).items
    previewKind.value = previews.value[0]?.kind ?? ''
  } catch {
    previewsError.value = true
  } finally {
    loadingPreviews.value = false
  }
}

onMounted(() => {
  void loadConfig()
})
</script>

<template>
  <div class="cv2-page integration-form" data-v2-page="wxpusher-settings">
    <section class="cv2-panel">
      <header class="cv2-panel-head push-header">
        <h2 class="cv2-panel-title">消息推送订阅</h2>
        <span v-if="config" class="cv2-st" :class="config.configured ? 'cv2-st--settled' : 'cv2-st--unsettled'">
          <i class="cv2-dot" aria-hidden="true"></i>{{ config.configured ? '应用凭据已配置' : '应用凭据待配置' }}
        </span>
        <button type="button" class="cv2-btn" @click="guideOpen = true">配置教程</button>
        <button type="button" class="cv2-btn" data-v2="wxpusher-preview" @click="openPreviews">模板预览</button>
      </header>
      <p v-if="config?.source === 'environment'" class="cv2-field-hint">当前沿用现有配置，已绑定打手可继续使用。</p>
      <p v-if="loading" role="status">正在读取推送设置…</p>
      <template v-else-if="!config">
        <p v-if="configFeedback" class="push-error" role="alert">{{ configFeedback.text }}</p>
        <button type="button" class="cv2-btn" @click="loadConfig">重新读取</button>
      </template>
      <form v-else @submit.prevent="save">
        <fieldset class="push-fieldset" :disabled="formLocked">
          <legend>WxPusher 应用配置</legend>
          <div class="cv2-form-grid">
            <label class="cv2-field">
              <span class="cv2-field-label">App ID</span>
              <input v-model="form.app_id" class="cv2-input" inputmode="numeric" autocomplete="off" maxlength="19" placeholder="WxPusher 应用的编号" />
            </label>
            <label class="cv2-field">
              <span class="cv2-field-label">appToken</span>
              <input id="wxpusher-app-token" v-model="form.app_token" class="cv2-input" type="password" autocomplete="new-password" maxlength="256"
                :placeholder="config.has_app_token && !switchingApp ? '已配置，留空保留原值' : '填写应用的 appToken'" />
            </label>
            <div class="cv2-field cv2-field--wide">
              <label class="cv2-field-label" for="wxpusher-subscribe-url">公开订阅链接</label>
              <span class="push-inline">
                <input id="wxpusher-subscribe-url" v-model="form.subscribe_url" class="cv2-input" type="url" autocomplete="off" maxlength="200" placeholder="选填 · WxPusher 应用的关注链接" />
                <button v-if="config.subscribe_url && form.subscribe_url === config.subscribe_url" type="button" class="cv2-btn"
                  @click="copy(config.subscribe_url, '订阅链接')">复制订阅链接</button>
              </span>
            </div>
            <div class="cv2-field cv2-field--wide">
              <label class="cv2-field-label" for="wxpusher-system-url">系统入口地址</label>
              <span class="push-inline">
                <input id="wxpusher-system-url" class="cv2-input" aria-label="系统入口地址" :value="config.system_url" readonly />
                <button type="button" class="cv2-btn" :disabled="!config.system_url" @click="copy(config.system_url, '系统入口地址')">复制地址</button>
              </span>
            </div>
            <div class="cv2-field cv2-field--wide">
              <label class="cv2-field-label" for="wxpusher-login-key">系统对接验证密钥</label>
              <span class="push-inline">
                <input id="wxpusher-login-key" v-model="form.login_signing_key" class="cv2-input" :type="showLoginKey ? 'text' : 'password'"
                  autocomplete="new-password" minlength="32" maxlength="256" :placeholder="config.has_login_signing_key && !switchingApp ? '已配置，留空保留原值' : '填写与 WxPusher 一致的密钥（至少 32 字符）'" />
                <button type="button" class="cv2-btn" @click="generateLoginKey">生成密钥</button>
                <button v-if="form.login_signing_key" type="button" class="cv2-btn" @click="copy(form.login_signing_key, '验证密钥')">复制密钥</button>
                <button v-if="form.login_signing_key" type="button" class="cv2-btn" @click="showLoginKey = !showLoginKey">{{ showLoginKey ? '隐藏' : '显示' }}</button>
              </span>
            </div>
          </div>
          <p v-if="clipboardFeedback" class="cv2-field-hint" role="status">{{ clipboardFeedback }}</p>
          <p v-if="switchingApp" class="push-error" role="status">打手需绑定当前应用。请填写新应用的 appToken，按需更新订阅链接和验证密钥。</p>
        </fieldset>

        <fieldset class="push-fieldset" :disabled="formLocked" data-v2="push-controls">
          <legend>通知与登录开关</legend>
          <div class="push-options push-options--main">
            <label><ElSwitch v-model="form.push_enabled" name="push_enabled" aria-label="消息推送" :disabled="formLocked" />消息推送</label>
            <label v-if="switchingApp && !form.login_signing_key">
              <ElSwitch :model-value="false" disabled aria-label="WxPusher 一键登录" />WxPusher 一键登录（填写新密钥后可启用）
            </label>
            <label v-else><ElSwitch v-model="form.login_enabled" name="login_enabled" aria-label="WxPusher 一键登录" :disabled="formLocked" />WxPusher 一键登录</label>
            <label><ElSwitch v-model="form.binding_prompt_enabled" name="binding_prompt_enabled"
              aria-label="登录后提醒打手绑定" :disabled="formLocked" />登录后提醒打手绑定</label>
          </div>
          <p class="cv2-field-hint">关闭绑定提醒后，打手仍可在「我的 → 消息通知」主动绑定。订单和结算通知遵循工作室与打手双方的接收设置。</p>
          <div class="push-options">
            <label><ElSwitch v-model="form.order_enabled" name="order_enabled" aria-label="订单状态变更" :disabled="formLocked" />订单状态变更</label>
            <label><ElSwitch v-model="form.settlement_enabled" name="settlement_enabled" aria-label="结算结果" :disabled="formLocked" />结算结果</label>
          </div>
          <details class="push-order-events">
            <summary>订单通知明细</summary>
            <p v-if="!form.order_enabled || !form.push_enabled" class="cv2-field-hint">重新开启总开关后，以下设置生效。</p>
            <div class="push-options">
              <label v-for="[key, label] in ORDER_EVENTS" :key="key">
                <ElSwitch v-model="form.order_events[key]" :name="key" :aria-label="label" :disabled="formLocked" size="small" />{{ label }}
              </label>
            </div>
          </details>
        </fieldset>

        <div class="push-actions">
          <button type="submit" class="cv2-btn cv2-btn--primary" :disabled="!dirty || saving || sending">{{ saving ? '保存中…' : '保存设置' }}</button>
          <span v-if="configFeedback" :class="{ 'push-error': configFeedback.error }" :role="configFeedback.error ? 'alert' : 'status'">{{ configFeedback.text }}</span>
        </div>
      </form>
    </section>

    <section class="cv2-panel">
      <header class="cv2-panel-head push-header">
        <h3 class="cv2-panel-title">测试消息</h3>
        <button v-if="config?.configured" type="button" class="cv2-btn" :disabled="loadingRecipients || sending || saving" @click="loadRecipients">刷新打手</button>
      </header>
      <p v-if="!config?.configured" class="cv2-field-hint">保存应用凭据后可测试发送。</p>
      <p v-else-if="loadingRecipients" class="cv2-field-hint" role="status">正在读取已绑定打手…</p>
      <p v-else-if="recipientsError" class="push-error" role="alert">读取打手失败，请刷新打手列表。</p>
      <p v-else-if="!recipients.length" class="cv2-field-hint">请先让打手在 H5「消息通知」绑定 WxPusher。</p>
      <div v-else class="push-actions">
        <select v-model="recipientId" class="cv2-input push-recipient" aria-label="接收测试消息的打手" :disabled="saving || sending || dirty">
          <option value="">请选择已绑定的打手</option>
          <option v-for="recipient in recipients" :key="recipient.id" :value="recipient.id">{{ recipient.name }}</option>
        </select>
        <button type="button" class="cv2-btn cv2-btn--primary" data-v2="wxpusher-send-test"
          :disabled="recipientId === '' || saving || sending || dirty" @click="sendTest">{{ sending ? '发送中…' : '发送测试消息' }}</button>
        <span v-if="dirty" class="cv2-field-hint">请先保存设置再试发。</span>
      </div>
      <p v-if="sendFeedback" :class="{ 'push-error': sendFeedback.error }" :role="sendFeedback.error ? 'alert' : 'status'">{{ sendFeedback.text }}</p>
    </section>

    <V2WxPusherDeliveryHistory :refresh-key="historyRefresh" />

    <OrderTabDialog v-model="guideOpen" title="WxPusher 配置教程" class="cv2-dialog cv2-dialog--md">
      <ol class="push-guide">
        <li>在电脑上打开 <a href="https://wxpusher.zjiecode.com/admin/" target="_blank" rel="noopener noreferrer">WxPusher 管理后台</a>。用微信扫码登录，首次扫码会自动注册。</li>
        <li>点击「创建应用」，填入工作室应用的名称和用途说明后保存。在当前应用找到 App ID 和 appToken。</li>
        <li>把 App ID 和 appToken 填到本页。公开订阅链接可按需填写。</li>
        <li>
          在 WxPusher「应用管理 → 系统对接」填入本页的「系统入口地址」。首次配置时，在团团通点「生成密钥」，
          把同一个验证密钥填到两边并保存。密钥至少 32 个字符，请勿外传。两边已配置相同密钥时可继续使用原密钥。
          回到本页打开「WxPusher 一键登录」，点「保存设置」。
        </li>
        <li>在本页单独打开「消息推送」，选择需要发送的通知类型，再点「保存设置」。</li>
        <li>请打手从「我的 → 消息通知」扫码绑定，再从 WxPusher 打开团团通入口验证一键登录，并用本页发送测试消息。</li>
      </ol>
      <template #footer><button type="button" class="cv2-btn cv2-btn--primary" @click="guideOpen = false">知道了</button></template>
    </OrderTabDialog>

    <OrderTabDialog v-model="previewOpen" title="通知模板预览" class="cv2-dialog cv2-dialog--md">
      <p class="cv2-field-hint">使用模拟数据展示通知。实际通知里的顶部和正文链接可打开打手端。</p>
      <p v-if="loadingPreviews" role="status">正在读取模板…</p>
      <div v-else-if="previewsError" class="push-actions">
        <span class="push-error" role="alert">读取模板失败。</span>
        <button type="button" class="cv2-btn" @click="openPreviews">重试</button>
      </div>
      <template v-else-if="currentPreview">
        <label class="push-preview-picker">
          消息类型
          <select v-model="previewKind" class="cv2-input">
            <option v-for="preview in previews" :key="preview.kind" :value="preview.kind">{{ preview.title }}</option>
          </select>
        </label>
        <p class="push-preview-url">顶部链接 <code>{{ currentPreview.url }}</code></p>
        <iframe class="push-preview" title="通知正文预览" sandbox="" :srcdoc="previewDocument"></iframe>
      </template>
      <p v-else class="cv2-field-hint">暂无通知模板。</p>
    </OrderTabDialog>
  </div>
</template>

<style scoped>
.push-header, .push-actions, .push-inline { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.push-header .cv2-panel-title { margin-right: auto; }
.push-inline .cv2-input { flex: 1; min-width: 160px; min-height: 38px; border-color: var(--tt2-filter-line); }
.push-inline .cv2-input:first-child { border-radius: 0 4px 4px 0; }
.push-inline .cv2-btn { white-space: nowrap; }
.push-fieldset { min-width: 0; margin: 12px 0; padding: 8px 0 0; border: 0; }
.push-fieldset legend { padding: 0; margin-bottom: 8px; font-weight: 600; }
.push-options { display: flex; flex-wrap: wrap; gap: 10px 24px; margin: 10px 0; }
.push-options label { display: inline-flex; align-items: center; gap: 7px; }
.push-options--main { font-weight: 600; }
.push-order-events { margin: 8px 0 12px; }
.push-order-events summary { width: fit-content; cursor: pointer; color: var(--tt2-primary-deep); }
.push-error { color: var(--tt2-st-error); }
.push-recipient { width: auto; min-width: 220px; }
.push-guide { display: grid; gap: 12px; padding-inline-start: 24px; }
.push-preview-picker { display: flex; align-items: center; gap: 8px; margin-top: 12px; }
.push-preview-url { overflow-wrap: anywhere; }
.push-preview { display: block; width: 100%; min-height: 290px; border: 1px solid var(--tt2-line); }
@media (max-width: 720px) {
  .push-inline .cv2-input:first-child { border-radius: 0 0 4px 4px; }
}
</style>
