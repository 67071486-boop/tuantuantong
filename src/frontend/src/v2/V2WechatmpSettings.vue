<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「微信公众号设置」v2（`/v2/wechatmp-settings`；2026-11-06 第三方平台批 B1，计划 §4）。
 * 事实源 = island 页 `layui-island/pages/system-wechatmp-settings.ts`（文案逐字照它）：认证横幅（常驻）/
 * 配置表单 / 服务器配置信息卡 / 菜单卡 四区块。照旧页实现：按钮从不禁用，未就绪点击由后端回 1000 如实显示；
 * AppSecret 只有掩码、三个凭据框恒留空（空串提交 = 后端保持旧值）；⛔ 无 AppSecret 原文 / 绑定列表 / 模板试发。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import {
  WECHATMP_SETTINGS_NODE,
  deleteWechatMpMenu,
  getWechatMpConfig,
  getWechatMpMenu,
  saveWechatMpConfig,
  syncWechatMpMenu,
  type WechatMpConfigRow,
  type WechatMpMode,
} from '@/api/wechatmp'
import { useSessionStore } from '@/stores/session'

/* ── 逐字文案（出处 = island 页常量，行号见常量名旁注释）──────────────────────── */

/** 认证横幅（island VERIFY_BANNER L106-107；常驻显眼，业务方点名）。 */
const VERIFY_BANNER =
  '公众号或服务号需通过微信认证才能使用菜单、模板消息与网页授权；未认证请先到微信公众平台完成认证'
/** 服务器 IP 未配置的占位（island SERVER_IP_PLACEHOLDER L110；来自 settings `MP_SERVER_PUBLIC_IP`）。 */
const SERVER_IP_PLACEHOLDER = '未配置（向部署方查询本机公网 IP）'

/** 三种加密模式（island MODE_OPTIONS L113-117；value = 后端 `EncryptionMode`）。 */
const MODE_OPTIONS: ReadonlyArray<{ value: WechatMpMode; label: string }> = [
  { value: 'plain', label: '明文模式' },
  { value: 'compatible', label: '兼容模式' },
  { value: 'safe', label: '安全模式' },
]

/** 页内提示（island MESSAGES L120-131 逐字）。 */
const MESSAGES = {
  failed: '操作失败',
  copyFailed: '复制失败（浏览器不允许自动复制），请手动选中上面的内容复制',
  saved: '已保存：回到微信公众平台核对服务器配置（URL / Token / AESKey / IP 白名单）后，再点「启用」。',
  menuSynced: '已同步预设菜单：打开公众号应能看到「绑定账号」与「我的订单」两项。',
  menuDeleted: '已删除菜单。',
} as const

/** 无权限自检提示（island NO_ACCESS_MESSAGE L134）。 */
const NO_ACCESS_MESSAGE = '无权限管理微信公众号设置'
/** 删除菜单二次确认（island layer.confirm 文案 L484 逐字）。 */
const MENU_DELETE_CONFIRM_TEXT =
  '确定删除公众号菜单吗？粉丝手机上的菜单会立即消失（预设菜单也要重新同步才会回来）。'
const COPY_OK_TEXT = '已复制'

/** 表单各框 placeholder 与旁注（island buildHtml L231-275 逐字）。 */
const APPSECRET_PLACEHOLDER = '留空 = 保持不变（已保存的值只显示掩码）'
const TOKEN_PLACEHOLDER = '留空 = 保持不变（服务器配置信息卡里有原文）'
const AES_KEY_PLACEHOLDER = '43 位；留空 = 保持不变（明文模式可以不填）'
const TEMPLATE_ID_PLACEHOLDER = '公众平台「模板消息」里申请的那条；留空 = 推送不发'
const ENABLED_AUX_TEXT = '关掉后回调 / 菜单 / 订单推送全部停用（微信侧的服务器配置验证会失败）'
const SAVE_HINT_TEXT = 'AppSecret / Token / EncodingAESKey 留空 = 保持不变'

/** 菜单卡三态文案（island menuBodyHtml L188 / L198 逐字）。 */
const MENU_EMPTY_TEXT = '公众号上还没有菜单：点上面的「同步预设菜单」创建。'
const MENU_RAW_SUMMARY_TEXT = '原始回包'

/** 未就绪提示（island notReadyMessage L142-146 逐字；前端只复述后端 `ready` 位，不自己重判）。 */
function notReadyMessage(ready: boolean): string {
  return ready
    ? ''
    : '当前配置未就绪（未启用，或 AppID / AppSecret / Token 没配齐）：回调验签会回 403，菜单与订单推送也全部停用。'
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : MESSAGES.failed
}

/* ── toast（info 1500ms / error 6000ms，同族先例）────────────────────────────── */

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

/* ── 权限门（照旧页 mount 自检：账号管理员 + 节点；fail-closed）───────────────── */

const session = useSessionStore()
const denied = ref(false)

/* ── 配置表单（服务端行是唯一真相；三个凭据框恒留空 = 掩码回写防呆）────────────── */

const form = reactive({
  appid: '',
  appsecret: '',
  token: '',
  aesKey: '',
  mode: 'plain' as WechatMpMode,
  templateId: '',
  enabled: false,
})

/** 服务端**此刻**的配置行（复制按钮的取数出口）。 */
const current = ref<WechatMpConfigRow | null>(null)
const configLoading = ref(false)
const saving = ref(false)

const notReadyText = computed(() => (current.value === null ? '' : notReadyMessage(current.value.ready)))

const serverIpText = computed(() => {
  const row = current.value
  if (row === null) return ''
  return row.server.server_ip === '' ? SERVER_IP_PLACEHOLDER : row.server.server_ip
})

/** 把服务端配置灌进表单（唯一装配出口；三个凭据刻意留空，placeholder 说明语义）。 */
function applyRow(row: WechatMpConfigRow): void {
  current.value = row
  form.appid = row.appid
  form.appsecret = ''
  form.token = ''
  form.aesKey = ''
  form.mode = row.mode
  form.templateId = row.template_id
  form.enabled = row.enabled
}

async function loadConfig(): Promise<void> {
  configLoading.value = true
  try {
    applyRow(await getWechatMpConfig())
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    configLoading.value = false
  }
}

/** 保存（PUT；三个凭据**总是发**、空串 = 后端保持旧值——S4 语义，⛔ 别"优化"成没改就不发）。 */
function onSave(): void {
  if (saving.value) return
  saving.value = true
  void saveWechatMpConfig({
    appid: form.appid.trim(),
    appsecret: form.appsecret.trim(),
    token: form.token.trim(),
    aes_key: form.aesKey.trim(),
    mode: form.mode,
    enabled: form.enabled,
    template_id: form.templateId.trim(),
  })
    .then(() => getWechatMpConfig())
    .then((row) => {
      applyRow(row)
      showToast(MESSAGES.saved)
    })
    // 失败如实显示后端文案（业务码 1000：首次保存缺凭据 / safe 缺 AESKey / 43 位不对）。
    .catch((e: unknown) => showToast(errText(e), 'error'))
    .finally(() => {
      saving.value = false
    })
}

/* ── 服务器配置信息卡：复制内容从行数据取（⛔ 不从 DOM 读）────────────────────── */

/** 缺 `navigator.clipboard` 不是静默失败（island copyTextToClipboard 逐段照抄）。 */
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

function copyRowValue(pick: (row: WechatMpConfigRow) => string): void {
  const row = current.value
  if (row === null) return
  copyTextToClipboard(pick(row), (ok) => showToast(ok ? COPY_OK_TEXT : MESSAGES.copyFailed))
}

function copyCallbackUrl(): void {
  copyRowValue((row) => row.server.callback_url)
}
function copyServerToken(): void {
  copyRowValue((row) => row.server.token)
}
function copyServerAesKey(): void {
  copyRowValue((row) => row.server.aes_key)
}
function copyServerIp(): void {
  copyRowValue((row) => row.server.server_ip)
}

/* ── 菜单卡：同步 / 删除 / 当前菜单（两种回包形状都认，失败如实显示）──────────── */

const menuState = ref<{ menu: unknown; error: string }>({ menu: null, error: '' })
const syncing = ref(false)
const deleting = ref(false)
const deleteDlg = ref(false)

/** 一级菜单名列表（island menuBodyHtml 同式：`{button:[…]}` 与 `{menu:{button:[…]}}` 两种形状都接）。 */
const menuNames = computed<string[]>(() => {
  if (menuState.value.error !== '') return []
  const menu = menuState.value.menu
  if (menu === null || typeof menu !== 'object') return []
  const obj = menu as { button?: unknown; menu?: { button?: unknown } }
  const buttonList = Array.isArray(obj.button) ? obj.button : obj.menu?.button
  const buttons = Array.isArray(buttonList) ? (buttonList as Array<{ name?: string }>) : []
  return buttons.map((b) => (typeof b?.name === 'string' ? b.name : '')).filter((n) => n !== '')
})

const menuJson = computed(() => JSON.stringify(menuState.value.menu, null, 2))

/** 拉不到（后端业务码 1000）⇒ error 非空如实显示，⛔ 不渲染成「还没有菜单」。 */
async function loadMenu(): Promise<void> {
  try {
    const { wechat } = await getWechatMpMenu()
    menuState.value = { menu: wechat, error: '' }
  } catch (e) {
    menuState.value = { menu: null, error: errText(e) }
  }
}

function onMenuSync(): void {
  if (syncing.value) return
  syncing.value = true
  void syncWechatMpMenu()
    .then(async () => {
      await loadMenu()
    })
    .then(() => showToast(MESSAGES.menuSynced))
    .catch((e: unknown) => showToast(errText(e), 'error'))
    .finally(() => {
      syncing.value = false
    })
}

function doMenuDelete(): void {
  if (deleting.value) return
  deleting.value = true
  void deleteWechatMpMenu()
    .then(async () => {
      deleteDlg.value = false
      await loadMenu()
      showToast(MESSAGES.menuDeleted)
    })
    .catch((e: unknown) => showToast(errText(e), 'error'))
    .finally(() => {
      deleting.value = false
    })
}

/* ── 其它出口 ───────────────────────────────────────────────────────────────── */

onMounted(() => {
  // 页内门（与路由两道门同源；iframe 深链时路由门不生效，照旧页 mount 自检）。
  if (session.isAccountAdmin !== true || !session.canNode([WECHATMP_SETTINGS_NODE])) {
    denied.value = true
    showToast(NO_ACCESS_MESSAGE)
    return
  }
  void loadConfig()
  void loadMenu()
})
</script>

<template>
  <div class="cv2-page integration-form" data-v2-page="wechatmp-settings">
    <template v-if="!denied">
      <!-- ① 认证提示横幅（常驻显眼；island VERIFY_BANNER 逐字） -->
      <div class="wm-banner integration-note" data-v2="verify-banner">
        <b>⭐ 微信认证提示：</b>{{ VERIFY_BANNER }}
      </div>

      <!-- ② 配置表单（island「公众号配置」卡；按钮从不禁用，未就绪只提示不拦） -->
      <section class="cv2-panel" data-v2="config-panel">
        <div class="cv2-panel-head">
          <h3 class="cv2-panel-title">公众号配置</h3>
        </div>
        <p v-if="notReadyText !== ''" class="wm-not-ready" data-v2="not-ready">{{ notReadyText }}</p>
        <div class="cv2-form-grid integration-form">
          <label class="cv2-field">
            <span class="cv2-field-label">AppID</span>
            <input v-model="form.appid" class="cv2-input" type="text" autocomplete="off" data-v2-field="appid" />
          </label>
          <label class="cv2-field">
            <span class="cv2-field-label">AppSecret</span>
            <input
              v-model="form.appsecret"
              class="cv2-input"
              type="text"
              autocomplete="off"
              :placeholder="APPSECRET_PLACEHOLDER"
              data-v2-field="appsecret"
            />
          </label>
          <label class="cv2-field">
            <span class="cv2-field-label">Token</span>
            <input
              v-model="form.token"
              class="cv2-input"
              type="text"
              autocomplete="off"
              :placeholder="TOKEN_PLACEHOLDER"
              data-v2-field="token"
            />
          </label>
          <label class="cv2-field">
            <span class="cv2-field-label">EncodingAESKey</span>
            <input
              v-model="form.aesKey"
              class="cv2-input"
              type="text"
              autocomplete="off"
              :placeholder="AES_KEY_PLACEHOLDER"
              data-v2-field="aes-key"
            />
          </label>
          <div class="cv2-field">
            <span class="cv2-field-label">加密模式</span>
            <el-select v-model="form.mode" :teleported="false" data-v2-field="mode">
              <el-option v-for="m in MODE_OPTIONS" :key="m.value" :value="m.value" :label="m.label" />
            </el-select>
          </div>
          <label class="cv2-field">
            <span class="cv2-field-label">订单推送模板ID</span>
            <input
              v-model="form.templateId"
              class="cv2-input"
              type="text"
              autocomplete="off"
              :placeholder="TEMPLATE_ID_PLACEHOLDER"
              data-v2-field="template-id"
            />
          </label>
          <div class="cv2-field cv2-field--wide">
            <span class="cv2-field-label">启用</span>
            <div class="wm-enabled-row" data-v2-field="enabled">
              <el-switch v-model="form.enabled" data-v2="enabled-switch" />
              <span class="cv2-field-hint" data-v2="enabled-aux">{{ ENABLED_AUX_TEXT }}</span>
            </div>
          </div>
        </div>
        <div class="cv2-form-actions">
          <button type="button" class="cv2-btn cv2-btn--submit cv2-btn--commit" data-v2="save" @click="onSave">保存配置</button>
          <span class="cv2-field-hint" data-v2="save-hint">{{ SAVE_HINT_TEXT }}</span>
        </div>
      </section>

      <!-- ③ 服务器配置信息卡（token / aes_key 原文要复制进公众平台） -->
      <section class="cv2-panel" data-v2="server-panel">
        <div class="cv2-panel-head">
          <h3 class="cv2-panel-title">服务器配置信息（复制到微信公众平台「服务器配置」）</h3>
        </div>
        <div class="cv2-form-grid integration-form">
          <div class="cv2-field cv2-field--wide">
            <span class="cv2-field-label">服务器 URL</span>
            <div class="wm-server-row">
              <span class="wm-mono" data-v2="server-callback-url">{{ current?.server.callback_url ?? '' }}</span>
              <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--quiet" data-v2="copy-callback-url" @click="copyCallbackUrl">复制</button>
            </div>
          </div>
          <div class="cv2-field cv2-field--wide">
            <span class="cv2-field-label">Token</span>
            <div class="wm-server-row">
              <span class="wm-mono" data-v2="server-token">{{ current?.server.token ?? '' }}</span>
              <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--quiet" data-v2="copy-token" @click="copyServerToken">复制</button>
            </div>
          </div>
          <div class="cv2-field cv2-field--wide">
            <span class="cv2-field-label">EncodingAESKey</span>
            <div class="wm-server-row">
              <span class="wm-mono" data-v2="server-aes-key">{{ current?.server.aes_key ?? '' }}</span>
              <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--quiet" data-v2="copy-aes-key" @click="copyServerAesKey">复制</button>
            </div>
          </div>
          <div class="cv2-field cv2-field--wide">
            <span class="cv2-field-label">服务器 IP</span>
            <div class="wm-server-row">
              <span class="wm-mono" data-v2="server-ip">{{ serverIpText }}</span>
              <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--quiet" data-v2="copy-server-ip" @click="copyServerIp">复制</button>
            </div>
            <span class="cv2-field-hint" data-v2="server-ip-aux">填进公众平台「IP 白名单」；部署时由部署方配好</span>
          </div>
        </div>
      </section>

      <!-- ④ 菜单卡（island「公众号菜单」卡；导语与三态文案逐字） -->
      <section class="cv2-panel" data-v2="menu-panel">
        <div class="cv2-panel-head">
          <h3 class="cv2-panel-title">公众号菜单</h3>
        </div>
        <p class="cv2-note integration-note" data-v2="menu-intro">
          预设菜单两项：「绑定账号」（微信内登录并绑定 openid，订单通知靠它）与「我的订单」（打手手机页）。
          同步会<b>整份覆盖</b>公众号上现有菜单；删除菜单也会<b>立即</b>生效。
        </p>
        <div class="cv2-form-actions">
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="menu-sync" @click="onMenuSync">同步预设菜单</button>
          <button type="button" class="cv2-btn cv2-btn--danger" data-v2="menu-delete" @click="deleteDlg = true">删除菜单</button>
        </div>
        <div class="wm-menu-body" data-v2="menu-body">
          <p v-if="menuState.error !== ''" class="wm-menu-error" data-v2="menu-error">{{ menuState.error }}</p>
          <p v-else-if="menuNames.length === 0" class="wm-dim" data-v2="menu-empty">{{ MENU_EMPTY_TEXT }}</p>
          <template v-else>
            <div class="wm-menu-names" data-v2="menu-names">
              <span v-for="(name, i) in menuNames" :key="i" class="wm-menu-badge">{{ name }}</span>
            </div>
            <details class="wm-menu-raw" data-v2="menu-raw">
              <summary>{{ MENU_RAW_SUMMARY_TEXT }}</summary>
              <pre>{{ menuJson }}</pre>
            </details>
          </template>
        </div>
      </section>
    </template>

    <section v-else class="cv2-panel">
      <p class="cv2-empty" data-v2="denied">{{ NO_ACCESS_MESSAGE }}</p>
    </section>

    <!-- 删除菜单二次确认（island layer.confirm 文案逐字；删除立即生效） -->
    <el-dialog
      :model-value="deleteDlg"
      title="提示"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      :close-on-click-modal="false"
      data-v2="dlg-menu-delete"
      @update:model-value="deleteDlg = false"
    >
      <p class="cv2-dlg-text" data-v2="menu-delete-question">{{ MENU_DELETE_CONFIRM_TEXT }}</p>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" data-v2="menu-delete-cancel" @click="deleteDlg = false">取消</button>
          <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--commit" data-v2="menu-delete-confirm" :disabled="deleting" @click="doMenuDelete">确定</button>
        </div>
      </template>
    </el-dialog>

    <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
      {{ toast.text }}
    </div>
  </div>
</template>

<style scoped>
/* 本页形状收在这里；色一律走 carbon.css 的 token（⛔ 不写裸色值）。 */

/* ① 认证横幅：琥珀警示盒（常驻，不置灰不折叠）。 */
.wm-banner {
  margin-top: 12px;
  max-width: 880px;
  padding: 10px 14px;
  border: 1px solid var(--tt2-tint-amber-line);
  border-left: 3px solid var(--tt2-tint-amber);
  border-radius: 4px;
  background: var(--tt2-filter-label-bg);
  color: var(--tt2-ink);
  font-size: 13px;
  line-height: 1.8;
}

/* 未就绪提示（表单上方整句红字，照旧页位置）。 */
.wm-not-ready {
  margin: 0 0 12px;
  max-width: 880px;
  color: var(--tt2-st-error);
  font-weight: 600;
  line-height: 1.7;
}

/* 启用开关 + 旁注同行。 */
.wm-enabled-row {
  display: flex;
  align-items: center;
  gap: 12px;
}

/* 服务器配置卡：值 + 复制钮同行；URL/密钥长串折行。 */
.wm-server-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.wm-server-row .cv2-btn--sm {
  flex: 0 0 auto;
  white-space: nowrap;
}

.wm-mono {
  font-family: var(--tt2-mono);
  word-break: break-all;
  line-height: 1.6;
}

/* 菜单卡三态。 */
.wm-menu-body {
  margin-top: 4px;
}

.wm-menu-error {
  margin: 10px 0 0;
  color: var(--tt2-st-error);
  line-height: 1.8;
}

.wm-dim {
  margin: 10px 0 0;
  color: var(--tt2-dim);
  line-height: 1.8;
}

.wm-menu-names {
  margin-top: 10px;
  line-height: 24px;
}

.wm-menu-badge {
  display: inline-block;
  margin: 0 6px 4px 0;
  padding: 1px 8px;
  border: 1px solid var(--tt2-line);
  border-radius: 2px;
  background: var(--tt2-label-bg);
  color: var(--tt2-dim);
  font-size: 12px;
  line-height: 20px;
}

.wm-menu-raw {
  margin-top: 8px;
}

.wm-menu-raw summary {
  cursor: pointer;
  color: var(--tt2-dim);
}

.wm-menu-raw pre {
  margin: 6px 0 0;
  padding: 8px;
  border: 1px solid var(--tt2-line);
  background: var(--tt2-label-bg);
  overflow: auto;
  font-family: var(--tt2-mono);
  font-size: 12px;
}
</style>

<style scoped src="./integration-form.css"></style>
