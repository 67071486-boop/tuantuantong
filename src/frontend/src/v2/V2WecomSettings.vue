<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「企微自建应用」v2（`/v2/wecom-settings`；D-0379；2026-11-11 视觉重做对齐 V2OrderManage 体系）。
 * 视觉 = 炭火（carbon token，色一律走 `carbon.css` 的 `.ttv2` token，⛔ 不写裸色值）；
 * 配置表单走 `cv2-form-grid` 家族（V2Profile 同款）、数据块走 cv2-ptabs + cv2-table-panel + cv2-pager、
 * 弹窗走 `cv2-dialog` 档（测试发消息 / 客户群详情）。
 * 端点契约 = `api/wecom.ts`（后端 `apps/wecom/views.py` 是实现即契约）。
 * 页内权限门 fail-closed：账号管理员 + 节点 `system.wecom_settings` 双判（同 V2QqbotSettings）。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import {
  WECOM_NOT_CONFIGURED_CODE,
  WECOM_SETTINGS_NODE,
  getWecomConfig,
  getWecomGroupChatInfo,
  listWecomCustomers,
  listWecomDepartments,
  listWecomFollowUsers,
  listWecomGroupChats,
  listWecomMessages,
  listWecomUsers,
  putWecomConfig,
  runWecomSelfCheck,
  sendWecomTestMessage,
  type WecomConfig,
  type WecomCustomerContact,
  type WecomDepartment,
  type WecomGroupChatRow,
  type WecomMessageRow,
  type WecomSelfCheckReport,
  type WecomUserRow,
} from '@/api/wecom'
import { BizError } from '@/api/errors'
import { useSessionStore } from '@/stores/session'
import V2WecomAftersales from './V2WecomAftersales.vue'
import V2WecomExternalPay from './V2WecomExternalPay.vue'
import V2WecomNotifications from './V2WecomNotifications.vue'

/* ── 逐字文案（本页自有，出处 = 本页实现）──────────────────────────────────── */

const NO_ACCESS_MESSAGE = '无权限管理企微自建应用'
const SECRET_PLACEHOLDER = '留空 = 不改（保存后任何接口都不回显）'
const CORP_ID_PLACEHOLDER = '企微后台 → 我的企业 → 企业 ID'
const CONTACTS_SECRET_LABEL = '客户联系 Secret（选填）'
const CONTACTS_SECRET_PLACEHOLDER =
  '留空 = 用应用 Secret；需先在企微后台「客户联系 → 可调用接口的应用」授权本应用'
const VERIFY_OK_TEXT = '回调验证已通过'
const VERIFY_NEVER_TEXT = '回调还没验证过（保存配置后到企微后台点一次「保存」触发）'
const GUIDE_TITLE = '企微后台要配的三件事'
const GUIDE_STEPS = [
  '① 接收消息：回调 URL / Token / EncodingAESKey 三样照下面信息卡复制进去，点保存（会触发一次验证）',
  '② 企业可信IP：把本服务器出口 IP 加进「应用 → 服务器配置 → 企业可信IP」（自检 60020 就是它没配）',
  '③ 客户联系：到企微后台「客户联系 → 可调用接口的应用」把本应用配进去授权（本页「客户联系 Secret」留空即可，直接用应用 Secret）',
] as const
const SELFCHECK_IDLE_TEXT = '自检会真拉一次应用 token 与客户联系 token（各计一次官方调用）。'
const SELFCHECK_RUNNING_TEXT = '自检进行中…'
const SELFCHECK_OK_PREFIX = '通过：'
const SELFCHECK_SKIP_PREFIX = '跳过：'
const SELFCHECK_BAD_PREFIX = '失败：'
const TEST_SEND_HINT = '发给自己先试：接收人填你在企微通讯录里的账号（userid）。'
const TEST_SEND_OK_TEXT = '已发出（企微回 errcode=0）；收没收到看企微'
const EMPTY_HINT_TEXT = '先在上方保存好 CorpID 与 Secret 并启用，下面的区块才能取数。'
const DEPTS_NOT_LOADED_TEXT = '点「拉取部门」加载组织架构。'
const DEPT_EMPTY_TEXT = '没有部门数据（应用可见范围里没有部门，或还没拉取）。'
const MEMBERS_NO_PICK_TEXT = '选一个部门看成员。'
const MEMBERS_EMPTY_TEXT = '这个部门没有成员（或应用的可见范围没覆盖）。'
const LOAD_FAIL_PREFIX = '拉取失败：'
const MSG_EMPTY_TEXT = '还没有回调消息。企微后台触发一次事件或发一条应用消息后，这里会出现台账行。'
const CUSTOMERS_EMPTY_TEXT = '这个成员还没有客户（或企微侧没返回）。'
const GROUPCHATS_EMPTY_TEXT = '还没有客户群。'
const COPY_OK_TEXT = '已复制'
const COPY_FAIL_TEXT = '复制失败（浏览器不允许自动复制），请手动选中复制'
const SAVE_OK_TEXT = '已保存'
const CONFIG_LOAD_FAIL_TEXT = '读配置失败：'

const MESSAGES = {
  failed: '操作失败',
  notConfigured: '企微应用还没配置：先填好凭据再试',
} as const

/* ── 纯函数 ─────────────────────────────────────────────────────────────────── */

function errText(e: unknown): string {
  if (e instanceof BizError) return e.message
  return e instanceof Error ? e.message : MESSAGES.failed
}

/** ISO 8601 时刻的显示形态（截到秒、不做时区换算；null = 还没发生过）。 */
function timeText(value: string | null): string {
  if (value === null || value === '') return '—'
  return value.replace('T', ' ').slice(0, 19)
}

/** 未配置（1039）与其它失败要分开口：未配置给引导，其它原样显示。 */
function isNotConfigured(e: unknown): boolean {
  return e instanceof BizError && e.code === WECOM_NOT_CONFIGURED_CODE
}

/* ── 权限门 / toast ──────────────────────────────────────────────────────────── */

const session = useSessionStore()
const canView = computed(
  () => session.isAccountAdmin === true && session.canNode([WECOM_SETTINGS_NODE]),
)

const toast = ref<{ text: string; kind: 'info' | 'error' } | null>(null)
let toastTimer: ReturnType<typeof setTimeout> | null = null

function showToast(text: string, kind: 'info' | 'error' = 'info'): void {
  if (toastTimer !== null) clearTimeout(toastTimer)
  toast.value = { text, kind }
  toastTimer = setTimeout(() => {
    toast.value = null
    toastTimer = null
  }, kind === 'error' ? 6000 : 1500)
}

/** 「复制」的剪贴板出口（缺 navigator.clipboard 不是静默失败）。 */
function copyTextToClipboard(text: string, onDone: (ok: boolean) => void): void {
  const clipboard: { writeText?: (value: string) => Promise<void> } | undefined =
    typeof navigator === 'undefined' ? undefined : navigator.clipboard
  if (!clipboard || typeof clipboard.writeText !== 'function') {
    onDone(false)
    return
  }
  void Promise.resolve(clipboard.writeText.call(clipboard, text)).then(
    () => onDone(true),
    () => onDone(false),
  )
}

function copyValue(text: string): void {
  copyTextToClipboard(text, (ok) => showToast(ok ? COPY_OK_TEXT : COPY_FAIL_TEXT))
}

/* ── 区块 1：应用配置表单 ───────────────────────────────────────────────────── */

const config = ref<WecomConfig | null>(null)
type WecomTab = 'contacts' | 'messages' | 'external'
const activeTab = ref<WecomTab>('contacts')
const form = reactive({
  corpId: '',
  agentId: '',
  secret: '',
  callbackToken: '',
  aesKey: '',
  contactsSecret: '',
  enabled: false,
})
const saveResult = ref<{ bad: boolean; text: string } | null>(null)

function applyConfig(row: WecomConfig): void {
  config.value = row
  form.corpId = row.corp_id
  form.agentId = row.agent_id === 0 ? '' : String(row.agent_id)
  form.callbackToken = row.callback_token
  form.aesKey = row.encoding_aes_key
  form.enabled = row.enabled
  // secret 族只写不读：输入框恒空（占位符讲清「留空 = 不改」）。
  form.secret = ''
  form.contactsSecret = ''
}

async function saveConfig(): Promise<void> {
  const payload = {
    corp_id: form.corpId.trim(),
    agent_id: Number(form.agentId),
    enabled: form.enabled,
  } as Parameters<typeof putWecomConfig>[0]
  // 留空 = 不改：凭据族为空就不发这个键（⛔ 不发空串当「不改」）。callback_token /
  // encoding_aes_key 同属凭据族：回调门槛就在这两格上（后端 `_usable_crypto`），
  // 漏发它们 = 回调永远 403。
  if (form.secret !== '') payload.secret = form.secret
  if (form.callbackToken !== '') payload.callback_token = form.callbackToken
  if (form.aesKey !== '') payload.encoding_aes_key = form.aesKey
  if (form.contactsSecret !== '') payload.contacts_secret = form.contactsSecret
  try {
    const saved = await putWecomConfig(payload)
    const fresh = await getWecomConfig()
    applyConfig(fresh)
    saveResult.value = {
      bad: false,
      text: `${SAVE_OK_TEXT}；凭据${saved.ready ? '已齐' : '还没齐（对照信息卡检查）'}`,
    }
  } catch (e) {
    saveResult.value = { bad: true, text: errText(e) }
  }
}

/* ── 区块 3：连通性自检 ─────────────────────────────────────────────────────── */

const selfCheck = ref<WecomSelfCheckReport | null>(null)
const selfCheckRunning = ref(false)

function selfCheckText(item: { ok: boolean; skipped: boolean; message: string }): string {
  if (item.skipped) return `${SELFCHECK_SKIP_PREFIX}${item.message}`
  return `${item.ok ? SELFCHECK_OK_PREFIX : SELFCHECK_BAD_PREFIX}${item.message}`
}

async function runSelfCheck(): Promise<void> {
  selfCheckRunning.value = true
  try {
    selfCheck.value = await runWecomSelfCheck()
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    selfCheckRunning.value = false
  }
}

/* ── 区块 4：测试发消息（工具栏按钮 → 弹窗）────────────────────────────────── */

const testOpen = ref(false)
const testToUser = ref('')
const testContent = ref('')
const testResult = ref<{ bad: boolean; text: string } | null>(null)

function openTestDialog(): void {
  testResult.value = null
  testOpen.value = true
}

async function sendTest(): Promise<void> {
  try {
    await sendWecomTestMessage(testToUser.value.trim(), testContent.value)
    testResult.value = { bad: false, text: TEST_SEND_OK_TEXT }
  } catch (e) {
    testResult.value = { bad: true, text: errText(e) }
  }
}

/* ── 区块 5a：通讯录 Tab（左部门树右成员表）────────────────────────────────── */

const departments = ref<WecomDepartment[]>([])
const users = ref<WecomUserRow[]>([])
const selectedDepartmentId = ref('')
const contactsWarn = ref('')
const contactsLoaded = ref(false)

async function loadContacts(): Promise<void> {
  try {
    departments.value = await listWecomDepartments()
    contactsWarn.value = ''
    contactsLoaded.value = true
  } catch (e) {
    contactsWarn.value = `${LOAD_FAIL_PREFIX}${errText(e)}`
  }
}

/** 轻量部门树（左栏列表形态）：按 `parentid` 链算层级、层级只管缩进。
 * 数据缺 `parentid` / 父不在列表里 / 环 ⇒ 该条按顶层（不炸）。 */
const deptTree = computed(() => {
  const byId = new Map(departments.value.map((d) => [d.id, d]))
  const depthOf = (d: WecomDepartment, seen: Set<number>): number => {
    const pid = d.parentid
    if (pid === undefined || pid === d.id || seen.has(pid) || !byId.has(pid)) return 0
    return 1 + depthOf(byId.get(pid)!, new Set([...seen, d.id]))
  }
  return departments.value.map((d) => ({
    id: d.id,
    name: d.name,
    depth: depthOf(d, new Set([d.id])),
  }))
})

async function onPickDepartment(id: number): Promise<void> {
  selectedDepartmentId.value = String(id)
  try {
    users.value = await listWecomUsers(id)
    contactsWarn.value = ''
  } catch (e) {
    users.value = []
    contactsWarn.value = `${LOAD_FAIL_PREFIX}${errText(e)}`
  }
}

/* ── 区块 5b：消息台账 Tab ─────────────────────────────────────────────────── */

const msgPage = ref(1)
const msgTotal = ref(0)
const msgRows = ref<WecomMessageRow[]>([])
const msgError = ref('')
const msgLoaded = ref(false)

async function loadMessages(): Promise<void> {
  try {
    const page = await listWecomMessages(msgPage.value)
    msgRows.value = page.items
    msgTotal.value = page.total
    msgError.value = ''
    msgLoaded.value = true
  } catch (e) {
    msgRows.value = []
    msgError.value = errText(e)
  }
}

function onMsgReload(): void {
  msgPage.value = 1
  void loadMessages()
}

function onMsgPageChange(page: number): void {
  msgPage.value = page
  void loadMessages()
}

/* ── 区块 5c：客户联系 Tab ─────────────────────────────────────────────────── */

const followUsers = ref<string[]>([])
const selectedFollowUser = ref('')
const customers = ref<WecomCustomerContact[]>([])
const groupChats = ref<WecomGroupChatRow[]>([])
const groupChatInfo = ref<{ chatId: string; text: string } | null>(null)
const externalWarn = ref('')
const externalLoaded = ref(false)

async function loadExternal(): Promise<void> {
  try {
    followUsers.value = await listWecomFollowUsers()
    const chats = await listWecomGroupChats()
    groupChats.value = chats.group_chat_list
    externalWarn.value = ''
    externalLoaded.value = true
  } catch (e) {
    externalWarn.value = isNotConfigured(e)
      ? MESSAGES.notConfigured
      : `${LOAD_FAIL_PREFIX}${errText(e)}`
  }
}

async function onFollowUserChange(): Promise<void> {
  if (selectedFollowUser.value === '') {
    customers.value = []
    return
  }
  try {
    const payload = await listWecomCustomers(selectedFollowUser.value)
    customers.value = payload.contacts
    externalWarn.value = ''
  } catch (e) {
    customers.value = []
    externalWarn.value = `${LOAD_FAIL_PREFIX}${errText(e)}`
  }
}

async function openGroupChat(chatId: string): Promise<void> {
  try {
    const info = await getWecomGroupChatInfo(chatId)
    groupChatInfo.value = {
      chatId,
      text: info === null ? '（企微没有返回详情）' : JSON.stringify(info, null, 2),
    }
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

/* ── 初装 ───────────────────────────────────────────────────────────────────── */

onMounted(() => {
  if (!canView.value) return
  void (async () => {
    try {
      applyConfig(await getWecomConfig())
    } catch (e) {
      showToast(`${CONFIG_LOAD_FAIL_TEXT}${errText(e)}`, 'error')
    }
  })()
})
</script>

<template>
  <div class="cv2-page integration-form" data-v2-page="wecom-settings">
    <template v-if="canView">
      <!-- 区块 1：应用配置表单（cv2-form-grid 家族，V2Profile 同款） -->
      <section class="cv2-panel" data-v2="block-config">
        <div class="cv2-panel-head">
          <h3 class="cv2-panel-title">应用配置</h3>
          <span class="wc-head-chips">
            <span
              class="cv2-st"
              :class="config?.ready ? 'cv2-st--settled' : 'cv2-st--unsettled'"
              data-v2="ready-chip"
            ><i class="cv2-dot" aria-hidden="true"></i>{{ config?.ready ? '凭据已齐' : '凭据未齐' }}</span>
            <span
              class="cv2-st"
              :class="config?.enabled ? 'cv2-st--settled' : 'cv2-st--unsettled'"
              data-v2="enabled-chip"
            ><i class="cv2-dot" aria-hidden="true"></i>{{ config?.enabled ? '已启用' : '未启用' }}</span>
          </span>
        </div>
        <div class="cv2-form-grid integration-form">
          <label class="cv2-field">
            <span class="cv2-field-label">CorpID</span>
            <input
              v-model="form.corpId"
              class="cv2-input"
              type="text"
              maxlength="64"
              autocomplete="off"
              :placeholder="CORP_ID_PLACEHOLDER"
              data-v2="corp-id"
            />
          </label>
          <label class="cv2-field">
            <span class="cv2-field-label">AgentId</span>
            <input
              v-model="form.agentId"
              class="cv2-input"
              type="text"
              autocomplete="off"
              placeholder="应用的 AgentId（数字）"
              data-v2="agent-id"
            />
          </label>
          <label class="cv2-field cv2-field--wide">
            <span class="cv2-field-label">Secret</span>
            <input
              v-model="form.secret"
              class="cv2-input"
              type="password"
              autocomplete="new-password"
              maxlength="256"
              :placeholder="SECRET_PLACEHOLDER"
              data-v2="secret"
            />
          </label>
          <label class="cv2-field">
            <span class="cv2-field-label">回调 Token</span>
            <span class="wc-inline">
              <input
                v-model="form.callbackToken"
                class="cv2-input wc-grow"
                type="text"
                maxlength="64"
                autocomplete="off"
                data-v2="callback-token"
              />
              <button
                v-if="form.callbackToken !== ''"
                type="button"
                class="cv2-btn cv2-btn--sm cv2-btn--quiet"
                data-v2="copy-token"
                @click="copyValue(form.callbackToken)"
              >复制</button>
            </span>
          </label>
          <label class="cv2-field">
            <span class="cv2-field-label">EncodingAESKey</span>
            <span class="wc-inline">
              <input
                v-model="form.aesKey"
                class="cv2-input wc-grow"
                type="text"
                maxlength="43"
                autocomplete="off"
                data-v2="aes-key"
              />
              <button
                v-if="form.aesKey !== ''"
                type="button"
                class="cv2-btn cv2-btn--sm cv2-btn--quiet"
                data-v2="copy-aeskey"
                @click="copyValue(form.aesKey)"
              >复制</button>
            </span>
          </label>
          <label class="cv2-field cv2-field--wide">
            <span class="cv2-field-label">{{ CONTACTS_SECRET_LABEL }}</span>
            <input
              v-model="form.contactsSecret"
              class="cv2-input"
              type="password"
              autocomplete="new-password"
              maxlength="256"
              :placeholder="CONTACTS_SECRET_PLACEHOLDER"
              data-v2="contacts-secret"
            />
          </label>
          <div class="cv2-field">
            <span class="cv2-field-label">启用</span>
            <span class="wc-switchline">
              <el-switch
                v-model="form.enabled"
                active-text="启用"
                inactive-text="停用"
                data-v2="enabled-switch"
              />
            </span>
          </div>
        </div>
        <div class="cv2-form-actions">
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="config-save" @click="void saveConfig()">保存配置</button>
          <span
            v-if="saveResult !== null"
            class="wc-line"
            :class="saveResult.bad ? 'wc-bad' : 'wc-ok'"
            data-v2="config-result"
          >{{ saveResult.text }}</span>
        </div>
      </section>

      <!-- 区块 2 + 3：回调信息 / 连通性自检（窄屏自动换行成上下两块） -->
      <div class="cv2-columns">
        <!-- 区块 2：回调信息卡 + 企微后台三步指引 -->
        <section class="cv2-panel" data-v2="block-callback">
          <div class="cv2-panel-head">
            <h3 class="cv2-panel-title">回调信息</h3>
          </div>
          <div class="cv2-info-list">
            <div class="cv2-info-row" data-v2="row-callback-url">
              <span class="cv2-info-label">回调 URL</span>
              <span class="cv2-info-value wc-inline">
                <span class="cv2-num wc-url" data-v2="callback-url">{{ config?.callback_url ?? '—' }}</span>
                <button
                  type="button"
                  class="cv2-btn cv2-btn--sm cv2-btn--quiet"
                  data-v2="copy-url"
                  @click="copyValue(config?.callback_url ?? '')"
                >复制</button>
              </span>
            </div>
            <div class="cv2-info-row" data-v2="row-verified">
              <span class="cv2-info-label">回调验证</span>
              <span class="cv2-info-value" data-v2="verified-at">
                {{ config?.verified_at ? `${VERIFY_OK_TEXT}（${timeText(config.verified_at)}）` : VERIFY_NEVER_TEXT }}
              </span>
            </div>
          </div>
          <details class="cv2-guide integration-note" data-v2="guide">
            <summary data-v2="guide-title">{{ GUIDE_TITLE }}</summary>
            <ol data-v2="guide-steps">
              <li v-for="step in GUIDE_STEPS" :key="step">{{ step }}</li>
            </ol>
          </details>
        </section>

        <!-- 区块 3：连通性自检 -->
        <section class="cv2-panel" data-v2="block-selfcheck">
          <div class="cv2-panel-head">
            <h3 class="cv2-panel-title">连通性自检</h3>
            <button
              type="button"
              class="cv2-btn cv2-btn--primary cv2-btn--commit"
              data-v2="selfcheck-run"
              :disabled="selfCheckRunning"
              @click="void runSelfCheck()"
            >{{ selfCheckRunning ? SELFCHECK_RUNNING_TEXT : '开始自检' }}</button>
          </div>
          <p class="cv2-field-hint wc-line" data-v2="selfcheck-hint">{{ SELFCHECK_IDLE_TEXT }}</p>
          <div v-if="selfCheck !== null" class="cv2-info-list" data-v2="selfcheck-report">
            <div class="cv2-info-row">
              <span class="cv2-info-label">应用 token</span>
              <span class="cv2-info-value wc-line" :class="selfCheck.main.ok ? 'wc-ok' : 'wc-bad'" data-v2="selfcheck-main">
                {{ selfCheckText(selfCheck.main) }}
              </span>
            </div>
            <div class="cv2-info-row">
              <span class="cv2-info-label">客户联系 token</span>
              <span class="cv2-info-value wc-line" :class="selfCheck.contacts.ok ? 'wc-ok' : 'wc-bad'" data-v2="selfcheck-contacts">
                {{ selfCheckText(selfCheck.contacts) }}
              </span>
            </div>
          </div>
        </section>
      </div>

      <V2WecomNotifications />
      <V2WecomAftersales />
      <V2WecomExternalPay />

      <!-- 区块 4 + 5：数据块（通讯录 / 消息台账 / 客户联系三页签）；测试发消息 = 工具栏按钮弹窗 -->
      <section class="cv2-panel" data-v2="block-tabs">
        <div class="cv2-panel-head">
          <h3 class="cv2-panel-title">通讯录 / 消息台账 / 客户联系</h3>
          <button type="button" class="cv2-btn" data-v2="test-send-open" @click="openTestDialog()">测试发消息</button>
        </div>
        <p v-if="!config?.ready" class="cv2-field-hint wc-line" data-v2="empty-hint-tabs">{{ EMPTY_HINT_TEXT }}</p>
        <template v-else>
          <div class="cv2-ptabs" data-v2="tabs">
            <button
              type="button"
              class="cv2-ptab"
              :class="{ 'cv2-ptab--on': activeTab === 'contacts' }"
              data-v2-tab="contacts"
              @click="activeTab = 'contacts'"
            >通讯录</button>
            <button
              type="button"
              class="cv2-ptab"
              :class="{ 'cv2-ptab--on': activeTab === 'messages' }"
              data-v2-tab="messages"
              @click="activeTab = 'messages'"
            >消息台账</button>
            <button
              type="button"
              class="cv2-ptab"
              :class="{ 'cv2-ptab--on': activeTab === 'external' }"
              data-v2-tab="external"
              @click="activeTab = 'external'"
            >客户联系</button>
          </div>

          <!-- 工具栏：左 = 当前页签的动作钮（管理订单同款纯文字横排），右 = 测试发消息 -->
          <div class="cv2-toolbar" data-v2="data-toolbar">
            <div class="ttb-strip">
              <button
                v-show="activeTab === 'contacts'"
                type="button"
                class="cv2-btn"
                data-v2="contacts-load"
                @click="void loadContacts()"
              >拉取部门</button>
              <button
                v-show="activeTab === 'messages'"
                type="button"
                class="cv2-btn"
                data-v2="messages-load"
                @click="onMsgReload"
              >刷新台账</button>
              <button
                v-show="activeTab === 'external'"
                type="button"
                class="cv2-btn"
                data-v2="external-load"
                @click="void loadExternal()"
              >拉取客户联系数据</button>
            </div>
          </div>

          <!-- 页签 1：通讯录 = 左部门树右成员表 -->
          <div v-show="activeTab === 'contacts'" data-v2="tab-contacts">
            <p v-if="contactsWarn !== ''" class="wc-bad wc-line" data-v2="contacts-warn">{{ contactsWarn }}</p>
            <div v-else class="wc-split">
              <div class="wc-depts" data-v2="dept-tree">
                <p class="wc-depts-head">部门</p>
                <template v-if="departments.length > 0">
                  <button
                    v-for="opt in deptTree"
                    :key="opt.id"
                    type="button"
                    class="wc-dept"
                    :class="{ 'wc-dept--on': selectedDepartmentId === String(opt.id) }"
                    :style="{ paddingLeft: `${12 + opt.depth * 14}px` }"
                    :data-v2-dept="opt.id"
                    @click="void onPickDepartment(opt.id)"
                  >{{ opt.name }}</button>
                </template>
                <p v-else-if="contactsLoaded" class="wc-depts-empty" data-v2="departments-empty">{{ DEPT_EMPTY_TEXT }}</p>
                <p v-else class="wc-depts-empty" data-v2="departments-empty">{{ DEPTS_NOT_LOADED_TEXT }}</p>
              </div>
              <div class="wc-members">
                <p v-if="users.length === 0" class="cv2-field-hint wc-line" data-v2="members-empty">
                  {{ selectedDepartmentId === '' ? MEMBERS_NO_PICK_TEXT : MEMBERS_EMPTY_TEXT }}
                </p>
                <div v-else class="wc-tablebox cv2-table-panel" data-v2="users-table">
                  <div class="wc-table-scroll">
                    <table class="cv2-table">
                      <colgroup><col style="width: 260px" /><col /></colgroup>
                      <thead>
                        <tr><th>账号</th><th>姓名</th></tr>
                      </thead>
                      <tbody>
                        <tr v-for="u in users" :key="u.userid" :data-v2-user-row="u.userid">
                          <td class="cv2-num">{{ u.userid }}</td>
                          <td>{{ u.name ?? '—' }}</td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <!-- 页签 2：消息台账 = 表格 + 分页条（管理订单同款） -->
          <div v-show="activeTab === 'messages'" data-v2="tab-messages">
            <p v-if="msgError !== ''" class="wc-bad wc-line" data-v2="messages-error">{{ msgError }}</p>
            <div v-if="msgRows.length > 0" class="wc-tablebox cv2-table-panel" data-v2="messages-table">
              <div class="wc-table-scroll">
                <table class="cv2-table">
                  <colgroup><col style="width: 170px" /><col style="width: 130px" /><col style="width: 150px" /><col /></colgroup>
                  <thead>
                    <tr><th>时间</th><th>类型</th><th>发送方</th><th>内容</th></tr>
                  </thead>
                  <tbody>
                    <tr v-for="m in msgRows" :key="m.id" :data-v2-msg-row="m.id">
                      <td class="cv2-num">{{ timeText(m.received_at) }}</td>
                      <td>{{ m.msg_type }}{{ m.event ? `/${m.event}` : '' }}</td>
                      <td>{{ m.from_user_name === '' ? '—' : m.from_user_name }}</td>
                      <td :title="m.content">{{ m.content === '' ? '—' : m.content }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <div class="cv2-pager" data-v2="msg-pager">
                <span class="cv2-pager-total" data-v2="msg-page-info">第 {{ msgPage }} 页 · 共 {{ msgTotal }} 条</span>
                <el-pagination
                  v-model:current-page="msgPage"
                  :page-size="50"
                  :total="msgTotal"
                  layout="prev, pager, next"
                  background
                  size="small"
                  @current-change="onMsgPageChange"
                />
              </div>
            </div>
            <p v-else-if="msgLoaded" class="cv2-field-hint wc-line" data-v2="messages-empty">{{ MSG_EMPTY_TEXT }}</p>
          </div>

          <!-- 页签 3：客户联系 = 成员选择器 + 客户表格 + 客户群表格 -->
          <div v-show="activeTab === 'external'" data-v2="tab-external">
            <p v-if="externalWarn !== ''" class="wc-bad wc-line" data-v2="external-warn">{{ externalWarn }}</p>
            <template v-else>
              <div class="wc-ext-controls">
                <span class="cv2-field">
                  <span class="cv2-field-label">跟进人</span>
                  <el-select
                    v-model="selectedFollowUser"
                    class="wc-ext-select"
                    filterable
                    clearable
                    placeholder="— 选一个成员看他的客户 —"
                    :teleported="false"
                    data-v2="follow-user-select"
                    @change="void onFollowUserChange()"
                  >
                    <el-option v-for="u in followUsers" :key="u" :value="u" :label="u" />
                  </el-select>
                </span>
              </div>
              <p v-if="externalLoaded && followUsers.length === 0 && groupChats.length === 0" class="cv2-field-hint wc-line" data-v2="external-empty">
                没有配置客户联系的成员，也没有客户群。
              </p>
              <div v-if="customers.length > 0" class="wc-tablebox cv2-table-panel" data-v2="customers-table">
                <div class="wc-table-scroll">
                  <table class="cv2-table">
                    <colgroup><col /><col style="width: 220px" /></colgroup>
                    <thead>
                      <tr><th>客户名</th><th>跟进人</th></tr>
                    </thead>
                    <tbody>
                      <tr v-for="(c, i) in customers" :key="i" :data-v2-customer-row="i">
                        <td>{{ c.external_contact?.name ?? '—' }}</td>
                        <td class="cv2-num">{{ c.follow_info?.userid ?? '—' }}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </div>
              <p v-else-if="selectedFollowUser !== '' && customers.length === 0" class="cv2-field-hint wc-line" data-v2="customers-empty">{{ CUSTOMERS_EMPTY_TEXT }}</p>
              <div v-if="groupChats.length > 0" class="wc-tablebox cv2-table-panel" data-v2="groupchats-table">
                <div class="wc-table-scroll">
                  <table class="cv2-table">
                    <colgroup><col /><col style="width: 120px" /></colgroup>
                    <thead>
                      <tr><th>客户群</th><th>详情</th></tr>
                    </thead>
                    <tbody>
                      <tr v-for="g in groupChats" :key="g.chat_id" :data-v2-groupchat-row="g.chat_id">
                        <td class="cv2-num">{{ g.chat_id }}</td>
                        <td>
                          <a
                            href="#"
                            class="cv2-actlink"
                            :data-v2-groupchat-open="g.chat_id"
                            @click.prevent="void openGroupChat(g.chat_id)"
                          >看详情</a>
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </div>
              <p v-else-if="externalLoaded && groupChats.length === 0" class="cv2-field-hint wc-line" data-v2="groupchats-empty">{{ GROUPCHATS_EMPTY_TEXT }}</p>
            </template>
          </div>
        </template>
      </section>

      <!-- 测试发消息弹窗 -->
      <el-dialog
        :model-value="testOpen"
        title="测试发消息"
        class="cv2-dialog cv2-dialog--md"
        destroy-on-close
        data-v2="dlg-test"
        @update:model-value="testOpen = false"
      >
        <p class="cv2-dlg-text" data-v2="test-send-hint">{{ TEST_SEND_HINT }}</p>
        <div class="wc-dlg-fields">
          <div class="cv2-field">
            <span class="cv2-field-label">接收人</span>
            <el-input
              v-model="testToUser"
              maxlength="64"
              autocomplete="off"
              placeholder="接收人 userid（企微通讯录账号）"
              data-v2="test-to-user"
            />
          </div>
          <div class="cv2-field">
            <span class="cv2-field-label">消息内容</span>
            <el-input
              v-model="testContent"
              maxlength="2048"
              placeholder="消息内容"
              data-v2="test-content"
            />
          </div>
        </div>
        <p
          v-if="testResult !== null"
          class="wc-line"
          :class="testResult.bad ? 'wc-bad' : 'wc-ok'"
          data-v2="test-result"
        >{{ testResult.text }}</p>
        <template #footer>
          <div class="cv2-dlg-actions">
            <button type="button" class="cv2-btn cv2-btn--quiet" data-v2="test-cancel" @click="testOpen = false">取消</button>
            <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="test-send" @click="void sendTest()">发送</button>
          </div>
        </template>
      </el-dialog>

      <!-- 客户群详情弹窗 -->
      <el-dialog
        :model-value="groupChatInfo !== null"
        :title="groupChatInfo === null ? '' : `客户群详情 ${groupChatInfo.chatId}`"
        class="cv2-dialog cv2-dialog--md"
        destroy-on-close
        data-v2="dlg-groupchat"
        @update:model-value="groupChatInfo = null"
      >
        <pre v-if="groupChatInfo !== null" class="wc-pre" data-v2="groupchat-info">{{ groupChatInfo.text }}</pre>
      </el-dialog>

      <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
        {{ toast.text }}
      </div>
    </template>
    <section v-else class="cv2-panel">
      <p class="cv2-empty" data-v2="no-permission">{{ NO_ACCESS_MESSAGE }}</p>
    </section>
  </div>
</template>

<style scoped>
/* 本页新增的形状收在这里；色一律走 carbon.css 的 .ttv2 token（⛔ 不写裸色值）。
 * 表格规格（表头 38 / 行高 44 / 底线 / hover 晕）与分页条由 .cv2-table-panel /
 * .cv2-table / .cv2-pager（carbon.css）承担，本文件只补「盒」与布局。 */

.wc-head-chips {
  display: inline-flex;
  gap: 8px;
}

.wc-head-chips .cv2-st {
  cursor: default;
}

.wc-inline {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  flex: 1 1 auto;
}

@media (min-width: 721px) {
  .integration-form .cv2-form-grid .cv2-field { grid-template-columns: 180px minmax(0, 1fr); }
}

.integration-form .cv2-field > .wc-inline:has(.cv2-input) { align-items: stretch; }

.wc-grow {
  flex: 1 1 auto;
  min-width: 0;
}

.wc-switchline {
  display: inline-flex;
  align-items: center;
  gap: 10px;
}

.wc-url {
  word-break: break-all;
}

.wc-line {
  line-height: 22px;
}

.wc-ok {
  color: var(--tt2-st-settled);
  font-weight: 600;
}

.wc-bad {
  color: var(--tt2-st-error);
  font-weight: 600;
}

/* 通讯录：左部门树（220px 定宽）右成员表。 */
.wc-split {
  display: flex;
  gap: 14px;
  align-items: flex-start;
  margin-top: 14px;
}

.wc-depts {
  flex: 0 0 220px;
  max-height: 360px;
  overflow: auto;
  padding: 6px;
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
}

.wc-depts-head {
  margin: 0;
  padding: 4px 8px;
  font-size: 12px;
  color: var(--tt2-dim);
}

.wc-dept {
  display: block;
  width: 100%;
  padding: 6px 8px;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--tt2-dim);
  font-family: inherit;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.wc-dept:hover {
  color: var(--tt2-ink);
  background: var(--tt2-hover-wash);
}

.wc-dept--on {
  background: var(--tt2-tool-bg);
  color: var(--tt2-primary-deep);
  font-weight: 600;
}

.wc-depts-empty {
  margin: 0;
  padding: 8px;
  font-size: 12px;
  color: var(--tt2-dim);
}

.wc-members {
  flex: 1 1 auto;
  min-width: 0;
}

/* 表格盒：面板内镶边框（表体规格走 .cv2-table-panel 那套 carbon 规则）。 */
.wc-tablebox {
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
  overflow: hidden;
  background: var(--tt2-panel);
}

.wc-tablebox.cv2-table-panel {
  margin-top: 12px;
}

.wc-table-scroll {
  overflow-x: auto;
}

/* 客户联系页签的成员选择器（压过 carbon 的 `.cv2-field .el-select` 172px）。 */
.wc-ext-controls .cv2-field .wc-ext-select {
  width: 280px;
}

.wc-pre {
  margin: 0;
  padding: 10px 12px;
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
  background: var(--tt2-tool-bg);
  overflow: auto;
  max-height: 60vh;
  font-size: 12px;
  font-family: var(--tt2-mono);
}

.wc-dlg-fields {
  display: grid;
  gap: 12px;
  margin-top: 12px;
}

/* 炭火 toast：居中深条（管理订单 / QQ 机器人页同款口径）。 */
.cv2-page :deep(.cv2-toast) {
  top: 45%;
  left: 50%;
  transform: translate(-50%, -50%);
  z-index: 3000;
  max-width: 56vw;
  padding: 12px 22px;
  border: none;
  border-radius: 6px;
  background: var(--tt2-toast-bg);
  color: var(--tt2-panel);
  font-size: 14px;
  text-align: center;
}

.cv2-page :deep(.cv2-toast--error) {
  color: var(--tt2-panel);
}
</style>

<style scoped src="./integration-form.css"></style>
