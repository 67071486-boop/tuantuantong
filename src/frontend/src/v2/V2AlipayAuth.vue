<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import {
  alipayReviewMessage, alipayStatusLabel, authorizeAlipay, clearConfirmedAlipayRequest, createAlipayAuthorization, listAlipayAuthorizations,
  newAlipayRequestId, pendingAlipayRequestId, rememberAlipayRequestId, refreshAlipayAuthorization, identityAlipayAuthorization, refreshAlipayIdentity, revokeAlipayAuthorization, needsAlipayIdentity, type AlipayAuthorization,
} from '@/api/alipayAuthorization'

const rows = ref<AlipayAuthorization[]>([])
const error = ref('')
const busy = ref(false)
const showAdd = ref(false)
const account = ref('')
const realName = ref('')
const requestId = ref('')
const hasPendingRequest = ref(pendingAlipayRequestId() !== null)
const active = ref<AlipayAuthorization | null>(null)
const retrying = ref(false)
const confirming = ref<AlipayAuthorization | null>(null)
let retryTimer: ReturnType<typeof setTimeout> | undefined
let retryGeneration = 0
let disposed = false
function stopRetry(): void {
  retryGeneration++
  if (retryTimer !== undefined) clearTimeout(retryTimer)
  retryTimer = undefined
  retrying.value = false
}
function closeAuthorization(): void { stopRetry(); active.value = null }
function scheduleRetry(row: AlipayAuthorization): void {
  stopRetry()
  if (disposed || active.value?.id !== row.id || !row.retryable_login || row.requires_platform_review || (row.status !== 'pending' && !needsAlipayIdentity(row)) || row.qr_png_base64) return
  retrying.value = true
  const generation = retryGeneration
  retryTimer = setTimeout(async () => {
    retryTimer = undefined
    if (generation !== retryGeneration || active.value?.id !== row.id) return
    if (busy.value) { error.value = '自动尝试已暂停，请刷新列表查看状态。'; stopRetry(); return }
    busy.value = true
    try {
      const updated = await (needsAlipayIdentity(row) ? identityAlipayAuthorization(row.id) : authorizeAlipay(row.id))
      if (generation !== retryGeneration || active.value?.id !== row.id) return
      active.value = updated
      await load()
      if (generation === retryGeneration && active.value?.id === row.id) scheduleRetry(updated)
    } catch (e) {
      if (generation === retryGeneration) { error.value = message(e); stopRetry() }
    } finally { busy.value = false }
  }, 3000)
}
const message = (e: unknown): string => e instanceof Error ? e.message : '请求失败，请稍后重试'

async function load(): Promise<void> {
  try {
    rows.value = await listAlipayAuthorizations()
    clearConfirmedAlipayRequest(rows.value)
    hasPendingRequest.value = pendingAlipayRequestId() !== null
    error.value = ''
  }
  catch (e) { error.value = message(e) }
}
function begin(): void {
  account.value = ''; realName.value = ''; requestId.value = pendingAlipayRequestId() ?? newAlipayRequestId(); error.value = ''; showAdd.value = true
}
async function create(): Promise<void> {
  if (busy.value) return
  if (!account.value.trim() || !realName.value.trim()) { error.value = '请填写支付宝账号和实名'; return }
  busy.value = true; error.value = ''
  try {
    rememberAlipayRequestId(requestId.value)
    hasPendingRequest.value = true
    const row = await createAlipayAuthorization({ account: account.value.trim(), real_name: realName.value.trim(), request_id: requestId.value })
    showAdd.value = false; active.value = row; await load(); scheduleRetry(row)
  } catch (e) { error.value = message(e) }
  finally { busy.value = false }
}
async function authorize(row: AlipayAuthorization): Promise<void> {
  if (busy.value || row.requires_platform_review) return
  stopRetry(); busy.value = true; error.value = ''
  try { const updated = await authorizeAlipay(row.id); active.value = updated; await load(); scheduleRetry(updated) }
  catch (e) { error.value = message(e) }
  finally { busy.value = false }
}
async function identity(row: AlipayAuthorization): Promise<void> {
  if (busy.value || row.requires_platform_review) return
  stopRetry(); busy.value = true; error.value = ''
  try { const updated = await identityAlipayAuthorization(row.id); if (!disposed) { active.value = updated; await load(); scheduleRetry(updated) } }
  catch (e) { error.value = message(e) }
  finally { busy.value = false }
}
async function revoke(): Promise<void> {
  const row = confirming.value
  if (!row || busy.value || row.requires_platform_review) return
  confirming.value = null; stopRetry(); busy.value = true; error.value = ''
  try { await revokeAlipayAuthorization(row.id); if (active.value?.id === row.id) closeAuthorization(); await load() }
  catch (e) { await load(); error.value = message(e) }
  finally { busy.value = false }
}
async function refresh(row: AlipayAuthorization): Promise<void> {
  if (busy.value) return
  stopRetry(); busy.value = true; error.value = ''
  const generation = retryGeneration
  try {
    const wasIdentity = needsAlipayIdentity(row)
    let updated = await (wasIdentity ? refreshAlipayIdentity(row.id) : refreshAlipayAuthorization(row.id))
    if (generation === retryGeneration && active.value?.id === row.id && !wasIdentity && needsAlipayIdentity(updated) && !updated.requires_platform_review && !updated.last_error) updated = await identityAlipayAuthorization(row.id)
    if (generation === retryGeneration && active.value?.id === row.id) {
      if (wasIdentity && needsAlipayIdentity(updated) && !updated.qr_png_base64 && !updated.requires_platform_review) updated = { ...updated, qr_png_base64: row.qr_png_base64 }
      active.value = updated
      scheduleRetry(updated)
    }
    await load()
  } catch (e) { error.value = message(e) }
  finally { busy.value = false }
}
onMounted(load)
onBeforeUnmount(() => { disposed = true; stopRetry() })
</script>

<template>
  <div class="cv2-page" data-v2-page="alipay-auth">
    <p class="cv2-note">绑定用于结算付款的支付宝账号，付款方本人需扫码确认授权。</p>
    <p v-if="hasPendingRequest" role="status">上次申请结果尚未确认。请刷新列表；如仍无记录，请填写原来的支付宝账号和实名重试。</p>
    <div class="cv2-toolbar">
      <button type="button" class="cv2-btn cv2-btn--find" @click="load">刷新列表</button>
      <button type="button" class="cv2-btn cv2-btn--primary" @click="begin">增加一个用于付款的支付宝账号</button>
    </div>
    <p v-if="error" role="alert">{{ error }}</p>
    <section class="cv2-panel cv2-table-panel" aria-label="支付宝授权列表">
      <table class="cv2-table">
        <thead><tr><th>付款支付宝账号</th><th>支付宝名称</th><th>授权状态</th><th>申请时间</th><th>授权时间</th><th>系统提示</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id">
            <td>{{ row.account_masked }}</td><td>{{ row.real_name }}</td><td>{{ alipayStatusLabel(row.status, row.last_error, row.requires_platform_review) }}</td>
            <td>{{ row.created_at || '—' }}</td><td>{{ row.authorized_at || '—' }}</td><td>{{ alipayReviewMessage(row) || '—' }}</td>
            <td class="alipay-actions"><button v-if="['pending', 'failed', 'revoked'].includes(row.status)" type="button" class="cv2-btn cv2-btn--sm cv2-btn--primary" :disabled="busy || row.requires_platform_review" @click="authorize(row)">{{ row.status === 'revoked' ? '重新授权' : '继续授权' }}</button>
              <button v-if="needsAlipayIdentity(row)" type="button" class="cv2-btn cv2-btn--sm cv2-btn--primary" :disabled="busy || row.requires_platform_review" @click="identity(row)">获取付款方身份</button>
              <button v-if="['signed', 'authorized'].includes(row.status) && !row.requires_platform_review" type="button" class="cv2-btn cv2-btn--sm" :disabled="busy" @click="confirming = row">取消授权</button>
              <button type="button" class="cv2-btn cv2-btn--sm" :disabled="busy" @click="refresh(row)">刷新授权结果</button></td>
          </tr>
          <tr v-if="!rows.length"><td colspan="7" class="cv2-empty">暂无数据</td></tr>
        </tbody>
      </table>
    </section>
    <div v-if="showAdd" class="alipay-overlay" role="presentation">
      <section class="alipay-dialog" role="dialog" aria-modal="true" aria-label="添加付款支付宝">
        <h2>添加付款支付宝</h2>
        <div class="alipay-dialog-body">
          <p v-if="hasPendingRequest" class="alipay-hint">请使用上次提交的支付宝账号和实名重试，避免重复创建。</p>
          <label class="cv2-field alipay-field"><span class="cv2-field-label">支付宝账号</span><input v-model="account" class="cv2-input" autocomplete="off" placeholder="请输入支付宝账号"></label>
          <label class="cv2-field alipay-field"><span class="cv2-field-label">支付宝实名</span><input v-model="realName" class="cv2-input" autocomplete="off" placeholder="请输入支付宝实名"></label>
          <p v-if="error" role="alert" class="alipay-error">{{ error }}</p>
          <div class="cv2-dlg-actions"><button type="button" class="cv2-btn" :disabled="busy" @click="showAdd = false">取消</button><button type="button" class="cv2-btn cv2-btn--primary" :disabled="busy" @click="create">确定</button></div>
        </div>
      </section>
    </div>
    <div v-if="active" class="alipay-overlay" role="presentation">
      <section class="alipay-dialog" role="dialog" aria-modal="true" aria-label="支付宝授权">
        <h2>支付宝授权</h2><div class="alipay-dialog-body"><p>{{ alipayStatusLabel(active.status, active.last_error, active.requires_platform_review) }}</p>
        <p v-if="needsAlipayIdentity(active)">请使用付款支付宝扫码，授权获取付款方身份。</p>
        <img v-if="active.qr_png_base64 && !active.requires_platform_review" :src="`data:image/png;base64,${active.qr_png_base64}`" alt="支付宝授权二维码">
        <p v-else-if="retrying" role="status">正在自动尝试获取授权二维码，请稍候。</p>
        <p v-else-if="(!['authorized', 'signed'].includes(active.status) || needsAlipayIdentity(active)) && !active.requires_platform_review">暂无授权二维码，请点击继续授权重试。</p>
        <p v-if="alipayReviewMessage(active)">{{ alipayReviewMessage(active) }}</p>
        <p v-if="error" role="alert">{{ error }}</p>
        <div class="cv2-dlg-actions"><button type="button" class="cv2-btn" @click="closeAuthorization">关闭</button>
          <button type="button" class="cv2-btn" :disabled="busy" @click="refresh(active)">刷新授权结果</button>
          <button v-if="needsAlipayIdentity(active)" type="button" class="cv2-btn cv2-btn--primary" :disabled="busy || retrying || active.requires_platform_review" @click="identity(active)">获取付款方身份</button>
          <button v-if="['pending', 'failed', 'revoked'].includes(active.status)" type="button" class="cv2-btn cv2-btn--primary" :disabled="busy || retrying || active.requires_platform_review" @click="authorize(active)">继续授权</button></div></div>
      </section>
    </div>
    <div v-if="confirming" class="alipay-overlay" role="presentation">
      <section class="alipay-dialog" role="dialog" aria-modal="true" aria-label="确认取消授权">
        <h2>确认取消授权</h2><div class="alipay-dialog-body">
          <p>确定取消 {{ confirming.account_masked }} 的付款授权吗？取消后该账户不能用于打款，可重新授权。</p>
          <div class="cv2-dlg-actions"><button type="button" class="cv2-btn" @click="confirming = null">返回</button><button type="button" class="cv2-btn cv2-btn--primary" :disabled="busy" @click="revoke">确认取消授权</button></div>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.alipay-actions { white-space: nowrap; }
.alipay-actions .cv2-btn + .cv2-btn { margin-left: 6px; }
.alipay-overlay { position: fixed; inset: 0; z-index: 3000; display: flex; align-items: center; justify-content: center; padding: 16px; background: var(--tt2-scrim); }
.alipay-dialog { width: min(480px, 100%); max-height: 90vh; overflow: auto; background: var(--tt2-panel); border-radius: 4px; color: var(--tt2-ink); }
.alipay-dialog h2 { margin: 0; padding: 14px 20px; color: var(--tt2-panel); background: var(--tt2-primary); font-size: 16px; }
.alipay-dialog-body { padding: 20px; }
.alipay-field { grid-template-columns: 104px minmax(0, 1fr); gap: 0; align-items: stretch; margin-bottom: 14px; overflow: hidden; border-radius: 4px; }
.alipay-field .cv2-field-label { display: flex; align-items: center; padding: 0 10px; border: 1px solid var(--tt2-filter-line); border-right: 0; background: var(--tt2-filter-label-bg); color: var(--tt2-dim); font-size: 13px; font-weight: 400; }
.alipay-field input { width: 100%; min-height: 34px; box-sizing: border-box; border-color: var(--tt2-filter-line); border-radius: 0; }
.alipay-field input::placeholder { color: var(--tt2-filter-ph); }
.alipay-hint { color: var(--tt2-dim); line-height: 1.6; }
.alipay-error { color: var(--tt2-st-error); }
.alipay-dialog img { display: block; max-width: min(260px, 100%); margin: 12px auto; }
.alipay-dialog .cv2-dlg-actions { margin-top: 16px; flex-wrap: wrap; }
</style>
