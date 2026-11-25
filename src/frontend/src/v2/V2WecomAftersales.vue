<script setup lang="ts">
import { computed, onMounted, reactive, ref, shallowRef } from 'vue'
import {
  getWecomAftersalesSettings,
  listWecomAftersalesOptions,
  saveWecomAftersalesSettings,
  type WecomAftersalesDraft,
  type WecomAftersalesOption,
  type WecomAftersalesSettings,
  type WecomAftersalesStatus,
} from '@/api/wecomAftersales'

const form = reactive<Omit<WecomAftersalesDraft, 'keywords'>>({
  enabled: false, group_ids: [], order_recipient: 'sales', fixed_recipient_ids: [],
})
const groupScope = ref<'all' | 'selected'>('all')
const keywordText = ref('')
const defaultKeywords = ref<string[]>([])
const available = ref(false)
const legacyPilot = ref(false)
const status = ref<WecomAftersalesStatus>('disabled')
const saving = ref(false)
const refreshing = ref(false)
const error = ref('')
const saved = ref(false)

const statusText: Record<WecomAftersalesStatus, string> = {
  disabled: '已关闭', pending: '等待同步', synced: '已同步至企微', error: '同步失败，可重新保存',
}

function useOptions<ID extends string | number>(
  selectedIds: () => ID[],
  list: (query: string, page: number) => Promise<{ items: WecomAftersalesOption<ID>[]; has_more: boolean }>,
) {
  const displayed = shallowRef<WecomAftersalesOption<ID>[]>([])
  const known = shallowRef(new Map<ID, WecomAftersalesOption<ID>>())
  const busy = ref(false)
  const more = ref(false)
  const failed = ref(false)
  let query: string | null = null
  let page = 0
  let retrieval = 0

  function remember(items: WecomAftersalesOption<ID>[]): void {
    const next = new Map(known.value)
    for (const item of items) next.set(item.id, item)
    known.value = next
  }

  const items = computed(() => {
    const options = new Map(displayed.value.map((item) => [item.id, item]))
    for (const id of selectedIds()) {
      if (!options.has(id)) options.set(id, known.value.get(id) ?? { id, name: String(id) })
    }
    return Array.from(options.values())
  })

  async function nextPage(): Promise<void> {
    const ticket = ++retrieval
    busy.value = true
    failed.value = false
    try {
      const result = await list(query ?? '', page + 1)
      if (ticket !== retrieval) return
      remember(result.items)
      displayed.value = [...displayed.value, ...result.items]
      page += 1
      more.value = result.has_more
    } catch {
      if (ticket === retrieval) failed.value = true
    } finally {
      if (ticket === retrieval) busy.value = false
    }
  }

  function search(value: string): void {
    const trimmed = value.trim()
    if (query === trimmed) return
    query = trimmed
    page = 0
    displayed.value = []
    more.value = false
    void nextPage()
  }

  return reactive({ items, busy, more, failed, remember, nextPage, search })
}

const groups = useOptions(() => form.group_ids, (q, page) => listWecomAftersalesOptions('group', q, page))
const users = useOptions(() => form.fixed_recipient_ids, (q, page) => listWecomAftersalesOptions('user', q, page))

function applyStatus(data: WecomAftersalesSettings): void {
  available.value = data.available
  legacyPilot.value = data.legacy_pilot
  status.value = data.status
}

function applySettings(data: WecomAftersalesSettings): void {
  applyStatus(data)
  defaultKeywords.value = data.default_keywords
  keywordText.value = data.keywords.join('、')
  form.enabled = data.enabled
  form.order_recipient = data.order_recipient
  groupScope.value = data.group_ids.length > 0 ? 'selected' : 'all'
  form.group_ids = [...data.group_ids]
  form.fixed_recipient_ids = [...data.fixed_recipient_ids]
  groups.remember(data.group_options)
  users.remember(data.fixed_recipient_options)
}

function errText(cause: unknown): string {
  return cause instanceof Error ? cause.message : '请稍后重试'
}

async function refreshStatus(initial = false): Promise<void> {
  refreshing.value = true
  try {
    const data = await getWecomAftersalesSettings()
    error.value = ''
    if (initial) applySettings(data)
    else applyStatus(data)
  } catch (cause) {
    error.value = `读取失败：${errText(cause)}`
  } finally {
    refreshing.value = false
  }
}

async function save(): Promise<void> {
  if (!available.value || saving.value || refreshing.value) return
  saved.value = false
  error.value = ''
  const keywords = [...new Set(keywordText.value.split(/[、,，;；\r\n]+/u).map((word) => word.trim()).filter(Boolean))]
  const groupIds = groupScope.value === 'selected' ? form.group_ids : []
  if (form.enabled && groupScope.value === 'selected' && groupIds.length === 0) {
    error.value = '至少选择一个客户群'
    return
  }
  if (form.enabled && keywords.length === 0) {
    error.value = '至少填写一个关键词'
    return
  }
  if (keywords.length > 20 || keywords.some((word) => Array.from(word).length > 32)) {
    error.value = '最多 20 个关键词，每个最多 32 字'
    return
  }
  saving.value = true
  try {
    const data = await saveWecomAftersalesSettings({ ...form, group_ids: groupIds, keywords })
    applySettings(data)
    saved.value = true
  } catch (cause) {
    error.value = `保存失败：${errText(cause)}`
  } finally {
    saving.value = false
  }
}

onMounted(() => void refreshStatus(true))
</script>

<template>
  <section v-if="available || error" class="cv2-panel" data-v2="block-aftersales">
    <form @submit.prevent="save">
      <div class="cv2-panel-head wa-head">
        <h3 class="cv2-panel-title">售后纠纷通知</h3>
        <div v-if="available" class="wa-toolbar">
          <label class="wa-switch">
            <input v-model="form.enabled" type="checkbox" :disabled="saving" aria-label="开启售后纠纷通知" /> 启用
          </label>
          <span class="wa-status" :class="{ 'wa-error': status === 'error' }" role="status">{{ legacyPilot ? '当前仅测试群生效' : statusText[status] }}</span>
          <button v-if="status === 'pending' || status === 'error'" class="cv2-btn cv2-btn--sm" type="button" :disabled="saving || refreshing" @click="refreshStatus()">刷新状态</button>
          <button class="cv2-btn cv2-btn--sm cv2-btn--primary" type="submit" :disabled="saving || refreshing">{{ saving ? '保存中…' : '保存' }}</button>
        </div>
        <button v-else type="button" class="cv2-btn cv2-btn--sm" :disabled="refreshing" @click="refreshStatus(true)">重试</button>
      </div>
      <div v-if="available" class="wa-grid">
        <div class="cv2-field wa-group-scope">
          <label for="wecom-aftersales-group-scope" class="cv2-field-label">适用群</label>
          <select id="wecom-aftersales-group-scope" v-model="groupScope" class="cv2-input" aria-label="适用群"
            :aria-describedby="groupScope === 'all' ? 'wecom-aftersales-all-groups-help' : undefined" :disabled="saving">
            <option value="all">全部客户群</option>
            <option value="selected">指定客户群</option>
          </select>
          <span v-if="groupScope === 'all'" id="wecom-aftersales-all-groups-help" class="cv2-field-hint">企微授权范围内，新建客户群自动纳入</span>
          <el-select v-else v-model="form.group_ids" aria-label="选择客户群" multiple filterable remote remote-show-suffix collapse-tags collapse-tags-tooltip fit-input-width placeholder="搜索并选择客户群"
            :remote-method="groups.search" :loading="groups.busy" :disabled="saving" :teleported="false" @visible-change="(open) => { if (open) groups.search('') }">
            <el-option v-for="choice in groups.items" :key="choice.id" :label="choice.name" :value="choice.id" class="wa-option-item">
              <span>{{ choice.name }}</span>
              <span v-if="choice.detail && choice.detail !== choice.name" class="wa-option-detail">{{ choice.detail }}</span>
            </el-option>
            <template v-if="groups.more || groups.failed" #footer>
              <button type="button" class="cv2-btn cv2-btn--sm" :disabled="groups.busy" @click="groups.nextPage">{{ groups.failed ? '重试加载' : '加载更多' }}</button>
            </template>
          </el-select>
        </div>
        <div class="cv2-field">
          <span class="cv2-field-label">订单通知对象</span>
          <el-select v-model="form.order_recipient" aria-label="订单通知对象" :disabled="saving" :teleported="false">
            <el-option label="录单人" value="creator" />
            <el-option label="发单人" value="publisher" />
            <el-option label="销售客服" value="sales" />
          </el-select>
        </div>
        <div class="cv2-field">
          <span class="cv2-field-label">固定通知人</span>
          <el-select v-model="form.fixed_recipient_ids" aria-label="固定通知人" multiple filterable remote remote-show-suffix collapse-tags collapse-tags-tooltip fit-input-width placeholder="搜索并选择账号（可选）"
            :remote-method="users.search" :loading="users.busy" :disabled="saving" :teleported="false" @visible-change="(open) => { if (open) users.search('') }">
            <el-option v-for="choice in users.items" :key="choice.id" :label="choice.name" :value="choice.id" class="wa-option-item">
              <span>{{ choice.name }}</span>
              <span v-if="choice.detail && choice.detail !== choice.name" class="wa-option-detail">{{ choice.detail }}</span>
            </el-option>
            <template v-if="users.more || users.failed" #footer>
              <button type="button" class="cv2-btn cv2-btn--sm" :disabled="users.busy" @click="users.nextPage">{{ users.failed ? '重试加载' : '加载更多' }}</button>
            </template>
          </el-select>
        </div>
        <div class="cv2-field wa-keywords">
          <div class="wa-keyword-label">
            <label for="wecom-aftersales-keywords" class="cv2-field-label">关键词</label>
            <button class="cv2-btn cv2-btn--sm cv2-btn--quiet" type="button" data-v2="aftersales-default-keywords" :disabled="saving" @click="keywordText = defaultKeywords.join('、')">恢复默认关键词</button>
          </div>
          <textarea id="wecom-aftersales-keywords" v-model="keywordText" class="cv2-input" rows="2" :disabled="saving" aria-describedby="wecom-aftersales-keyword-help" />
          <span id="wecom-aftersales-keyword-help" class="cv2-field-hint">用顿号、逗号或换行分隔，最多 20 个。摘要完成后，向有该订单查看权限的收件人发送一条系统通知。</span>
        </div>
      </div>
      <p v-if="error" class="wa-feedback wa-error" role="alert">{{ error }}</p>
      <p v-else-if="saved" class="wa-feedback" role="status">配置已保存</p>
    </form>
  </section>
</template>

<style scoped>
.wa-head,
.wa-toolbar,
.wa-switch,
.wa-keyword-label { display: flex; align-items: center; gap: 8px; }
.wa-head,
.wa-toolbar { flex-wrap: wrap; }
.wa-keyword-label { justify-content: space-between; }
.wa-status,
.wa-feedback { color: var(--tt2-dim); }
.wa-error { color: var(--tt2-st-error); }
.wa-feedback { margin: 10px 0 0; }
.wa-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px 12px; }
.wa-group-scope select,
.wa-grid :deep(.el-select) { width: 100%; }
.wa-option-item { height: auto; min-height: 34px; padding-block: 6px; line-height: 1.4; white-space: normal; overflow-wrap: anywhere; }
.wa-option-detail { display: block; color: var(--tt2-dim); font-size: 12px; }
.wa-keywords { display: flex; flex-direction: column; align-items: stretch; grid-column: 1 / -1; }
.wa-keywords textarea { box-sizing: border-box; width: 100%; min-width: 0; resize: vertical; }
@media (max-width: 640px) { .wa-grid { grid-template-columns: minmax(0, 1fr); } }
</style>
