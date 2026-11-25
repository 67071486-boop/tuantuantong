<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { fetchSystemSwitches, updateSystemSwitch } from '@/api/systemSwitches'
import { useSessionStore } from '@/stores/session'

type SwitchValue = 'Y' | 'N'
type SwitchItem = { key: string; label: string; description: string; onLabel: string; offLabel: string }
const items: SwitchItem[] = [
  { key: 'forbid_duplicate_source_order_no', label: '禁止录入相同的来源单号', description: '影响管理端手工录单和编辑；自动同步不受影响。', onLabel: '禁止重复', offLabel: '允许重复' },
  { key: 'reject_unpaid_xgj_order', label: '拒绝识别闲管家待付款订单', description: '只影响闲管家的录单识别，其他平台不受影响。', onLabel: '拒绝识别', offLabel: '允许识别' },
  { key: 'hall_simulated_orders', label: '接单大厅生成模拟单', description: '模拟单用于展示，不参与结算或对账。', onLabel: '开启', offLabel: '关闭' },
  { key: 'hall_preferred_orders', label: '接单大厅优选订单', description: '关闭后，高价单不再使用优选标记、优选接单条件和优选强制押金。', onLabel: '开启', offLabel: '关闭' },
  { key: 'auto_refund_platform_order', label: '平台退款后自动退单', description: '收到闲管家的退款通知时生效。关闭后，系统不会自动退单或生成退款处理待办。', onLabel: '开启', offLabel: '关闭' },
]
const session = useSessionStore()
const canEdit = computed(() => session.canNode(['system.edit_settings']))
const values = reactive<Record<string, SwitchValue | null>>({})
const saved = reactive<Record<string, SwitchValue | null>>({})
const loading = ref(false)
const savingKey = ref<string | null>(null)
const toast = ref<{ text: string; kind: 'info' | 'error' } | null>(null)
let toastTimer: ReturnType<typeof setTimeout> | null = null

function showToast(text: string, kind: 'info' | 'error' = 'info'): void {
  if (toastTimer !== null) clearTimeout(toastTimer)
  toast.value = { text, kind }
  toastTimer = setTimeout(() => { toast.value = null; toastTimer = null }, kind === 'error' ? 6000 : 1500)
}
function errText(error: unknown): string {
  return error instanceof Error ? error.message : '操作失败'
}
async function reload(): Promise<void> {
  loading.value = true
  try {
    const { items: rows } = await fetchSystemSwitches()
    const loaded = items.map((item) => rows.find((row) => row.key === item.key))
    if (loaded.some((row) => !row || (row.value !== 'Y' && row.value !== 'N'))) {
      throw new Error('订单设置不完整，请刷新后重试')
    }
    for (const row of loaded) {
      if (!row) continue
      const hasPendingChoice = saved[row.key] != null && values[row.key] !== saved[row.key]
      if (!hasPendingChoice) values[row.key] = row.value
      saved[row.key] = row.value
    }
  } catch (error) {
    showToast(errText(error), 'error')
  } finally {
    loading.value = false
  }
}
async function saveSetting(item: SwitchItem): Promise<void> {
  const value = values[item.key]
  if (!canEdit.value || savingKey.value !== null || value == null) return
  if (value === saved[item.key]) { showToast('设置未改变'); return }
  savingKey.value = item.key
  try {
    await updateSystemSwitch(item.key, value)
    saved[item.key] = value
    showToast('保存成功')
  } catch (error) {
    try {
      const { items: rows } = await fetchSystemSwitches()
      const current = rows.find((row) => row.key === item.key)
      if (!current || (current.value !== 'Y' && current.value !== 'N')) throw new Error('设置状态未知')
      values[item.key] = current.value
      saved[item.key] = current.value
      showToast(`${errText(error)}，已重新读取当前设置`, 'error')
    } catch {
      values[item.key] = null
      saved[item.key] = null
      showToast('保存结果未知，请重新加载后确认', 'error')
    }
  } finally {
    savingKey.value = null
  }
}
onMounted(() => { void reload() })
onUnmounted(() => { if (toastTimer !== null) clearTimeout(toastTimer) })
</script>

<template>
  <div class="cv2-page" data-v2-page="settings">
    <section class="cv2-panel st-panel" aria-label="订单业务设置">
      <h3 class="cv2-panel-title">订单业务设置</h3>
      <p class="st-hint">选择需要的设置后，分别点击保存。未修改的设置不会改变。</p>
      <div v-for="(item, index) in items" :key="item.key" class="st-row" :data-v2-setting="item.key">
        <div class="st-info">
          <div class="st-title">{{ index + 1 }}. {{ item.label }}</div>
          <div class="st-hint">{{ item.description }}</div>
        </div>
        <div class="cv2-dlg-radios st-radios">
          <span v-if="!loading && saved[item.key] == null" role="status">状态未知，请重新加载确认</span>
          <label class="cv2-radio">
            <input v-model="values[item.key]" type="radio" :name="item.key" value="Y" :disabled="loading || savingKey !== null || !canEdit || saved[item.key] == null" />
            {{ item.onLabel }}
          </label>
          <label class="cv2-radio">
            <input v-model="values[item.key]" type="radio" :name="item.key" value="N" :disabled="loading || savingKey !== null || !canEdit || saved[item.key] == null" />
            {{ item.offLabel }}
          </label>
        </div>
        <button v-if="canEdit" type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" :disabled="loading || savingKey !== null || saved[item.key] == null || values[item.key] === saved[item.key]" @click="void saveSetting(item)">保存</button>
      </div>
      <button v-if="!loading && items.some((item) => saved[item.key] == null)" type="button" class="cv2-btn" :disabled="savingKey !== null" @click="void reload()">重新加载</button>
    </section>
    <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status">{{ toast.text }}</div>
  </div>
</template>

<style scoped>
.st-panel { padding: 16px 20px; }
.st-panel .cv2-panel-title { margin: 0 0 10px; font-size: 15px; font-weight: 600; }
.st-row { display: flex; align-items: stretch; gap: 0; margin-top: 8px; overflow: hidden; border: 1px solid var(--tt2-filter-line); border-radius: 4px; flex-wrap: wrap; }
.st-info { flex: 1 1 350px; padding: 8px 10px; background: var(--tt2-filter-label-bg); border-right: 1px solid var(--tt2-filter-line); font-size: 13px; color: var(--tt2-dim); }
.st-title { font-weight: 600; }
.st-hint { margin: 4px 0; color: var(--tt2-dim); }
.st-radios { flex: 1 1 220px; min-height: var(--el-component-size); align-items: center; flex-wrap: wrap; gap: 16px; padding: 8px 12px; background: var(--tt2-panel); }
.st-row > button { align-self: center; margin: 8px 12px; }
.cv2-page :deep(.cv2-toast) { top: 45%; left: 50%; transform: translate(-50%, -50%); z-index: 3000; max-width: 56vw; padding: 12px 22px; border: none; border-radius: 6px; background: var(--tt2-toast-bg); color: var(--tt2-panel); text-align: center; }
.cv2-page :deep(.cv2-toast--error) { color: var(--tt2-panel); }
</style>
