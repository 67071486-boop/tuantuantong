<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  archiveFrame, archiveGroups, archiveLogin, archiveMessages, archiveRememberGroup, archiveSearch, archiveSignature, loadWecomArchiveSdk,
  type ArchiveGroup, type ArchiveMessage,
} from './wecomArchive'

const props = defineProps<{ orderNo?: string; initialQuery?: string }>()
const emit = defineEmits<{ groupSelected: [chatId: string | null] }>()
const dayMs = 24 * 60 * 60 * 1000
const today = Date.now()
const dateFormat = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' })
const latestDate = dateFormat.format(today)
const query = ref(props.initialQuery ?? '')
const startDate = ref(dateFormat.format(today - 30 * dayMs))
const endDate = ref(latestDate)
const searchError = ref('')
const available = ref(props.orderNo === undefined)
const opened = ref(true)
const loading = ref(false)
const loadingHistory = ref(false)
const historyError = ref('')
const error = ref('')
const componentError = ref('')
const complete = ref(true)
const searched = ref(false)
const groups = ref<ArchiveGroup[]>([])
const selectableGroups = computed(() => props.orderNo === undefined ? groups.value
  : groups.value.filter(group => group.match === 'linked' || group.proof))
const selected = ref<ArchiveGroup | null>(null)
watch(() => selected.value?.chat_id ?? null, (chatId) => emit('groupSelected', chatId))
const nextBefore = ref<number | null>(null)
const nextAfter = ref<number | null>(null)
const count = ref(0)
const loginMount = ref<HTMLElement | null>(null)
const loginPending = ref(false)
const frameMount = ref<HTMLElement | null>(null)
let messages: ArchiveMessage[] = []
let frame: Awaited<ReturnType<typeof archiveFrame>> | undefined
let loginPanel: ReturnType<Awaited<ReturnType<typeof loadWecomArchiveSdk>>['createWWLoginPanel']> | undefined
let frameTimer: number | undefined
let generation = 0

function clearFrameTimer() {
  window.clearTimeout(frameTimer)
  frameTimer = undefined
}

function resetDisplay() {
  generation += 1
  clearFrameTimer()
  loginPanel?.unmount()
  loginPanel = undefined
  loginPending.value = false
  frame?.dispose()
  frame?.el.remove()
  frame = undefined
  frameMount.value?.replaceChildren()
  messages = []
  count.value = 0
  nextBefore.value = null
  nextAfter.value = null
  loadingHistory.value = false
  historyError.value = ''
  componentError.value = ''
}

function failure(err: unknown, fallback: string) {
  return err instanceof Error && err.message ? err.message : fallback
}

async function findGroups() {
  if (props.orderNo === undefined) return
  beginSearch()
  const current = generation
  try {
    await loadOrderGroups(props.orderNo, current)
  } catch (err) {
    if (generation === current) {
      available.value = true
      error.value = failure(err, '查找企微群失败，请重试')
    }
  } finally {
    if (generation === current) loading.value = false
  }
}

async function loadOrderGroups(orderNo: string, current: number, afterLogin = false) {
  const result = await archiveGroups(orderNo)
  if (generation !== current) return
  available.value = result.enabled
  if (!result.enabled) return
  if (result.requires_login) {
    if (afterLogin) throw new Error('企业微信登录未完成，请重试')
    await withArchiveAuthorization(current, true, () => loadOrderGroups(orderNo, current, true))
    return
  }
  const onlyGroup = completeSearch(result)
  if (onlyGroup) await selectGroup(onlyGroup)
}

function beginSearch() {
  resetDisplay()
  opened.value = true
  loading.value = true
  searched.value = false
  error.value = ''
  selected.value = null
  groups.value = []
}

function completeSearch(result: Awaited<ReturnType<typeof archiveGroups>>) {
  if (!result.enabled) {
    error.value = '本账号暂未开通聊天记录'
    return
  }
  groups.value = result.groups
  complete.value = result.complete
  searched.value = true
  if (selectableGroups.value.length === 1 && (props.orderNo !== undefined || result.complete)) {
    return selectableGroups.value[0]
  }
}

async function findByKeyword(reauthenticate = false) {
  const term = query.value.trim()
  const start = startDate.value
  const end = endDate.value
  const days = (Date.parse(end) - Date.parse(start)) / dayMs + 1
  searchError.value = ''
  if (term.length < 2) {
    searchError.value = '请输入至少 2 个字的查询内容'
    return
  }
  if (!Number.isFinite(days) || days < 1 || end > latestDate) {
    searchError.value = '请选择有效的开始和结束日期'
    return
  }
  if (days > 31) {
    searchError.value = '日期范围最多 31 天'
    return
  }
  beginSearch()
  const current = generation
  try {
    await withArchiveAuthorization(current, reauthenticate, async (sdk, signature) => {
      const result = await archiveSearch(term, start, end)
      if (generation !== current) return
      if (result.requires_login) {
        if (!reauthenticate) await findByKeyword(true)
        else throw new Error('企微身份验证已过期，请重新搜索')
        return
      }
      const onlyGroup = completeSearch(result)
      if (onlyGroup) {
        selected.value = onlyGroup
        await showMessages(onlyGroup, sdk, signature, current)
      }
    })
  } catch (err) {
    if (generation === current) error.value = failure(err, '查找企微聊天记录失败，请重试')
  } finally {
    if (generation === current) loading.value = false
  }
}

type MessagePage = { kind: 'initial' } | { kind: 'older' | 'newer'; cursor: number }

async function loadMessages(group: ArchiveGroup, position: MessagePage, current: number) {
  const initial = position.kind === 'initial'
  const around = initial && props.orderNo === undefined ? group.anchor_msgid : undefined
  const before = position.kind === 'older' ? position.cursor : null
  const range = position.kind === 'newer' ? { after: position.cursor } : around ? { around } : undefined
  const result = range ? await archiveMessages(props.orderNo, group, before, range)
    : await archiveMessages(props.orderNo, group, before)
  if (generation !== current) return
  if (result.requires_login) {
    await selectGroup(group, true)
    return
  }
  if (initial && props.orderNo !== undefined && group.remember) {
    // Saving the verified match helps after a rename, while reading still works if saving fails.
    void archiveRememberGroup(props.orderNo, group).catch(() => undefined)
  }
  const page = [...result.messages].reverse()
  const previousFirst = messages[0]
  const updated = initial ? page : position.kind === 'older' ? [...page, ...messages] : [...messages, ...page]
  if (frame && page.length) {
    // The iframe needs a visible viewport before asking it to scroll.
    if (initial) {
      count.value = updated.length
      await nextTick()
    }
    if (generation !== current) return
    await frame.render(updated, previousFirst && !initial
      ? { kind: 'preserve', anchorId: previousFirst.msgid }
      : around && page.some(message => message.msgid === around)
        ? { kind: 'focus', anchorId: around }
        : { kind: 'latest' })
    if (generation !== current) return
  }
  messages = updated
  count.value = updated.length
  if (position.kind !== 'newer') nextBefore.value = result.next_before
  if (position.kind !== 'older') nextAfter.value = result.next_after ?? null
  if (initial && updated.length) {
    await nextTick()
    if (generation !== current || !frameMount.value) return
    const viewport = frameMount.value.closest('.dtl-face-content')
    if (viewport) {
      const clipped = frameMount.value.getBoundingClientRect().bottom - viewport.getBoundingClientRect().bottom
      if (clipped > 0) viewport.scrollTop += clipped
    }
  }
}

async function showMessages(group: ArchiveGroup, sdk: Awaited<ReturnType<typeof loadWecomArchiveSdk>>,
  sig: Awaited<ReturnType<typeof archiveSignature>>, current: number) {
  await nextTick()
  if (generation !== current || !frameMount.value) return
  frameTimer = window.setTimeout(() => {
    if (generation === current) componentError.value = '企微组件加载超时，请重新打开聊天'
  }, 15000)
  const frameElement = document.createElement('div')
  frameMount.value.replaceChildren(frameElement)
  const instance = await archiveFrame(sdk, props.orderNo, sig, frameElement, () => {
    if (generation === current) {
      componentError.value = props.orderNo === undefined
        ? '有消息无法展示，请核对企微身份和会话存档权限'
        : '有消息无法展示，请重新登录企业微信后再试'
    }
  }, () => {
    if (generation === current) clearFrameTimer()
  }, group.chat_id, () => {
    if (generation === current) void pageMessages('older')
  })
  if (generation !== current) {
    instance.dispose()
    instance.el.remove()
    return
  }
  frame = instance
  instance.el.title = '企微群聊聊天记录'
  instance.el.style.width = '100%'
  instance.el.style.height = 'var(--archive-frame-height, clamp(300px, 60vh, 620px))'
  await loadMessages(group, { kind: 'initial' }, current)
}

async function withArchiveAuthorization(current: number, reauthenticate: boolean,
  work: (sdk: Awaited<ReturnType<typeof loadWecomArchiveSdk>>, sig: Awaited<ReturnType<typeof archiveSignature>>) => Promise<void>) {
  const signatureRequest = reauthenticate
    ? archiveSignature(props.orderNo, undefined, true)
    : archiveSignature(props.orderNo)
  const [sdk, signature] = await Promise.all([loadWecomArchiveSdk(), signatureRequest])
  if (generation !== current) return
  if (signature.verified) {
    await work(sdk, signature)
    return
  }
  await nextTick()
  if (generation !== current || !loginMount.value) return
  loginPending.value = true
  loginPanel = sdk.createWWLoginPanel({
    el: loginMount.value,
    params: {
      login_type: 'CorpApp', appid: signature.corp_id, agentid: String(signature.agent_id),
      redirect_uri: location.href.split('#')[0] ?? location.origin, state: signature.login_state,
      redirect_type: 'callback', panel_size: 'small',
    },
    onLoginSuccess: ({ code }) => {
      if (generation !== current || !loginPanel) return
      loginPanel.unmount()
      loginPanel = undefined
      loginPending.value = false
      error.value = ''
      loading.value = true
      void archiveLogin(props.orderNo, code, signature.login_state)
        .then(({ verified }) => {
          if (generation !== current) return
          if (!verified) throw new Error('企微身份验证未完成')
          return work(sdk, signature)
        })
        .catch((err: unknown) => {
          if (generation === current) error.value = failure(err, '企微身份验证失败，请重试')
        })
        .finally(() => { if (generation === current) loading.value = false })
    },
    onLoginFail: () => {
      if (generation === current) error.value = '企微登录失败，请重新打开聊天'
    },
  })
}

async function selectGroup(group: ArchiveGroup, reauthenticate = false) {
  resetDisplay()
  const current = generation
  selected.value = group
  error.value = ''
  loading.value = true
  try {
    await withArchiveAuthorization(current, reauthenticate, (sdk, signature) =>
      showMessages(group, sdk, signature, current))
  } catch (err) {
    if (generation === current) {
      loginPending.value = false
      error.value = failure(err, '聊天记录加载失败，请重试')
    }
  } finally {
    if (generation === current) loading.value = false
  }
}

async function pageMessages(kind: 'older' | 'newer') {
  const cursor = kind === 'older' ? nextBefore.value : nextAfter.value
  if (!selected.value || cursor === null || loading.value || loadingHistory.value) return
  const current = generation
  loadingHistory.value = true
  historyError.value = ''
  try {
    await loadMessages(selected.value, { kind, cursor }, current)
  } catch (err) {
    if (generation === current) historyError.value = failure(err, '加载更多消息失败，请重试')
  } finally {
    if (generation === current) loadingHistory.value = false
  }
}

function close() {
  resetDisplay()
  opened.value = false
}

onMounted(() => { void findGroups() })
onBeforeUnmount(resetDisplay)
</script>

<template>
  <section v-if="available" class="archive-pane" :class="{ 'archive-pane--closed': !opened }">
    <button v-if="!opened" type="button" class="cv2-btn cv2-btn--sm" @click="findGroups">
      查看聊天记录
    </button>
    <template v-else>
      <header class="archive-header">
        <h4 class="archive-title">{{ selected?.name || '聊天记录' }}</h4>
        <div v-if="orderNo !== undefined" class="archive-actions">
          <span v-if="selected || loginPending" class="archive-login-hint">首次查看需要登录企业微信</span>
          <button type="button" class="cv2-btn cv2-btn--sm" :disabled="loading" @click="findGroups">重新找群</button>
          <button type="button" class="cv2-btn cv2-btn--sm" @click="close">收起聊天记录</button>
        </div>
      </header>
      <form v-if="orderNo === undefined" class="archive-search" @submit.prevent="findByKeyword()">
        <label class="archive-search-query">
          <span>订单号、群名或聊天关键词</span>
          <input v-model="query" type="search" maxlength="128" autocomplete="off" />
        </label>
        <label>
          <span>开始日期</span>
          <input v-model="startDate" type="date" :max="endDate || latestDate" />
        </label>
        <label>
          <span>结束日期</span>
          <input v-model="endDate" type="date" :min="startDate || undefined" :max="latestDate" />
        </label>
        <button type="submit" class="cv2-btn cv2-btn--primary" :disabled="loading">搜索聊天记录</button>
        <p v-if="searchError" class="archive-search-error" role="alert">{{ searchError }}</p>
      </form>
      <div v-if="selectableGroups.length && (selectableGroups.length > 1 || !selected)" class="archive-picker">
        <p>请选择要查看的群聊</p>
        <div class="archive-actions">
          <button v-for="group in selectableGroups" :key="group.chat_id" type="button" class="cv2-btn cv2-btn--sm"
            :class="{ 'cv2-btn--primary': selected?.chat_id === group.chat_id }"
            :aria-pressed="selected?.chat_id === group.chat_id" :disabled="loading" @click="selectGroup(group)">
            {{ group.name }}{{ group.match === 'message' ? '（聊天记录命中）' : '' }}
          </button>
        </div>
      </div>
      <p v-if="loading && count === 0" class="archive-notice" role="status">正在加载企微聊天记录…</p>
      <p v-if="error" class="archive-notice" role="alert">{{ error }}</p>
      <p v-if="searched && !complete" class="archive-notice">
        {{ orderNo === undefined ? '当前结果可能不完整，可调整关键词或日期后再查。' : '搜索结果可能不完整，可以稍后再查。' }}
      </p>
      <div v-if="(searched || error) && !selectableGroups.length" class="archive-notice archive-actions">
        <span v-if="searched && orderNo === undefined">暂无可查看的匹配结果。</span>
        <span v-else-if="searched && complete">没有找到本订单对应的企微群聊。</span>
        <slot name="search" />
      </div>
      <div v-show="loginPending" ref="loginMount" class="archive-login" />
      <div v-if="selected" class="archive-body">
        <div v-if="componentError || (error && !loginPending)" class="archive-notice">
          <p v-if="componentError" role="alert">{{ componentError }}</p>
          <button type="button" class="cv2-btn cv2-btn--sm" :disabled="loading"
            @click="selectGroup(selected, true)">重新验证企微身份</button>
        </div>
        <div v-if="count > 0" class="archive-history">
          <button v-if="nextBefore !== null" type="button" class="cv2-btn cv2-btn--sm" :disabled="loading || loadingHistory"
            :aria-busy="loadingHistory" @click="pageMessages('older')">{{ loadingHistory ? '正在加载…' : '加载更早的消息' }}</button>
          <span v-else-if="!loading">已到最早消息</span>
          <span v-if="historyError" role="alert">{{ historyError }}</span>
        </div>
        <div v-show="count > 0" ref="frameMount" class="archive-frame" />
        <div v-if="count > 0 && nextAfter !== null" class="archive-history">
          <button type="button" class="cv2-btn cv2-btn--sm" :disabled="loading || loadingHistory"
            :aria-busy="loadingHistory" @click="pageMessages('newer')">加载更新的消息</button>
        </div>
        <p v-if="!loading && !error && !componentError && count === 0 && !loginPending"
          class="archive-notice">暂无已同步的聊天记录。</p>
      </div>
    </template>
  </section>
</template>

<style scoped>
.archive-pane {
  margin-top: 16px;
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
  background: var(--tt2-panel);
  overflow: hidden;
}
.archive-pane--closed { border: 0; background: none; }
.archive-header, .archive-actions, .archive-history {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.archive-header { justify-content: space-between; padding: 8px 12px; border-bottom: 1px solid var(--tt2-line); }
.archive-search {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: 10px;
  padding: 10px 14px 12px;
  border-bottom: 1px solid var(--tt2-line);
}
.archive-search label { min-width: 0; display: grid; gap: 4px; color: var(--tt2-dim); }
.archive-search-query { flex: 1 1 230px; }
.archive-search input {
  min-width: 0;
  box-sizing: border-box;
  height: 32px;
  padding: 4px 8px;
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
  background: var(--tt2-panel);
  color: var(--tt2-ink);
  font: inherit;
}
.archive-search input:focus-visible { outline: 2px solid var(--tt2-primary); outline-offset: 1px; }
.archive-search-error { flex-basis: 100%; margin: 0; color: var(--tt2-st-error); }
.archive-title { min-width: 0; margin: 0; font-size: 14px; overflow-wrap: anywhere; }
.archive-login-hint { color: var(--tt2-dim); font-size: 12px; }
.archive-picker { padding: 10px 14px; border-bottom: 1px solid var(--tt2-line); }
.archive-picker p { margin: 0 0 6px; }
.archive-notice { margin: 0; padding: 12px 14px; }
.archive-notice p { margin: 0 0 6px; }
.archive-history { justify-content: center; min-height: 36px; padding: 4px 10px; color: var(--tt2-dim); font-size: 12px; }
.archive-history button { border-color: transparent; background: transparent; color: var(--tt2-tint-blue); }
.archive-history button:hover:not(:disabled) { text-decoration: underline; }
.archive-history [role='alert'] { color: var(--tt2-st-error); }
.archive-frame { background: var(--tt2-panel); }
.wecom-chat.archive-pane:not(.archive-pane--closed), .wecom-chat .archive-body { display: flex; flex-direction: column; }
.wecom-chat.archive-pane > :not(.archive-body), .wecom-chat .archive-body > :not(.archive-frame) { flex-shrink: 0; }
.wecom-chat .archive-body, .wecom-chat .archive-frame { flex: 1; min-height: 0; }
.wecom-chat .archive-frame { --archive-frame-height: 100%; }
.wecom-chat .archive-frame > :deep(div) { height: 100%; }
.wecom-chat .archive-frame :deep(iframe) { display: block; box-sizing: border-box; }
.archive-login { padding: 12px 14px; }
</style>
