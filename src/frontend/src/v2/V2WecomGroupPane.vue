<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { request } from '@/api/http'
import V2WecomArchivePane from './V2WecomArchivePane.vue'

type GroupMember = { name?: string; userid?: string }
type Group = { chat_id: string; name: string; owner: string; members: GroupMember[] }
type QrState = 'loading' | 'ready' | 'pending' | 'unavailable' | 'uncertain'
type QrResult = { status: QrState; qr_url: string; message: string }
const props = defineProps<{ orderNo: string; sourceOrderNo?: string | undefined }>()
const loading = ref(true)
const archiveSearchAvailable = ref(false)
const error = ref('')
const groups = ref<Group[]>([])
const selectedChatId = ref<string | null>(null)
const tooMany = ref(false)
const pageChanged = ref(false)
const currentAfter = ref<number | null>(null)
const nextAfter = ref<number | null>(null)
const previousPages = ref<(number | null)[]>([])
const qr = ref<Record<string, QrResult>>({})
const timers = new Map<string, ReturnType<typeof setTimeout>>()
const POLL_LIMIT = 20
const MAX_MATCHED_GROUPS = 5
let disposed = false

async function loadQr(chatId: string, attempts = 0): Promise<void> {
  if (disposed) return
  if (attempts === 0) qr.value[chatId] = { status: 'loading', qr_url: '', message: '' }
  try {
    const result = await request<QrResult>({
      url: `/api/wecom/orders/${encodeURIComponent(props.orderNo)}/group-qr/`,
      method: 'POST',
      data: { chat_id: chatId },
    })
    if (disposed) return
    qr.value[chatId] = result
    if (result.status === 'pending' && attempts < POLL_LIMIT) {
      timers.set(chatId, setTimeout(() => { void loadQr(chatId, attempts + 1) }, 3000))
    } else if (result.status === 'pending') {
      qr.value[chatId] = { ...result, message: '群码仍在处理中，请稍后重试' }
    }
  } catch {
    if (!disposed) qr.value[chatId] = { status: 'unavailable', qr_url: '', message: '群码加载失败，请稍后重试' }
  }
}

async function loadGroupQrs(items: Group[]): Promise<void> {
  let next = 0
  async function worker(): Promise<void> {
    while (!disposed && next < items.length) {
      const group = items[next++]
      if (group) await loadQr(group.chat_id)
    }
  }
  await Promise.all(Array.from({ length: Math.min(items.length, 4) }, () => worker()))
}

async function loadGroups(after: number | null): Promise<boolean> {
  loading.value = true
  error.value = ''
  try {
    const result = await request<{
      groups: Group[]; too_many: boolean; next_after: number | null
      archive_search_available?: boolean
    }>({
      url: `/api/wecom/orders/${encodeURIComponent(props.orderNo)}/groups/`, method: 'GET',
      ...(after === null ? {} : { params: { after } }),
    })
    if (disposed) return false
    archiveSearchAvailable.value = result.archive_search_available === true
    pageChanged.value = after !== null && result.too_many !== true
    tooMany.value = result.too_many === true || after !== null || result.groups.length > MAX_MATCHED_GROUPS || result.next_after != null
    groups.value = pageChanged.value ? [] : result.groups
    currentAfter.value = after
    nextAfter.value = result.next_after ?? null
    loading.value = false
    if (!tooMany.value) void loadGroupQrs(result.groups)
    return true
  } catch {
    if (!disposed) error.value = '企微群资料加载失败，请稍后重试'
    return false
  } finally {
    if (!disposed) loading.value = false
  }
}

async function nextPage(): Promise<void> {
  if (nextAfter.value === null || loading.value) return
  const prior = currentAfter.value
  if (await loadGroups(nextAfter.value)) previousPages.value.push(prior)
}

async function previousPage(): Promise<void> {
  if (previousPages.value.length === 0 || loading.value) return
  const target = previousPages.value[previousPages.value.length - 1] ?? null
  if (await loadGroups(target)) previousPages.value.pop()
}

async function refreshFirstPage(): Promise<void> {
  if (await loadGroups(null)) previousPages.value = []
}

onMounted(() => { void loadGroups(null) })

onBeforeUnmount(() => {
  disposed = true
  for (const timer of timers.values()) clearTimeout(timer)
  timers.clear()
})

function imageFailed(chatId: string): void {
  qr.value[chatId] = { status: 'unavailable', qr_url: '', message: '群码图片加载失败，请重试' }
}

function ownerName(group: Group): string {
  return group.members.find((member) => member.userid === group.owner)?.name?.trim()
    || (group.owner ? '未获取昵称' : '暂无')
}
</script>

<template>
  <section class="wecom-groups" data-v2="wecom-groups">
    <div class="wecom-layout">
      <aside class="wecom-details" aria-label="群资料和二维码">
        <p v-if="loading">正在加载企微群资料…</p>
        <div v-else-if="error"><p>{{ error }}</p><button type="button" class="cv2-btn cv2-btn--sm" @click="void loadGroups(currentAfter)">重试</button></div>
        <div v-else-if="pageChanged" role="alert"><p>群列表已变化，请返回首页刷新。</p><button type="button" class="cv2-btn cv2-btn--sm" @click="void refreshFirstPage()">返回首页刷新</button></div>
        <p v-else-if="groups.length === 0 && !tooMany">暂未找到群资料和二维码</p>
        <template v-else>
          <p v-if="tooMany" role="alert">匹配到的群超过 5 个，已暂停生成群码，请检查订单号。群资料可分页查看。</p>
          <p v-if="selectedChatId && groups.length && !groups.some(group => group.chat_id === selectedChatId)" class="wecom-group-context">
            当前聊天暂无对应的群资料和二维码。
          </p>
          <p v-if="groups.length > 1">匹配到多个群，请核对群名。</p>
          <article v-for="group in groups" :key="group.chat_id" class="wecom-group"
            :class="{ 'wecom-group--no-qr': tooMany, 'wecom-group--selected': group.chat_id === selectedChatId }">
            <div class="wecom-info">
              <h4>{{ group.name }}</h4>
              <p>群主：{{ ownerName(group) }}</p>
              <p>群成员：{{ group.members.length }} 人</p>
              <ul v-if="group.members.length">
                <li v-for="(member, index) in group.members" :key="index">{{ member.name?.trim() || '未获取昵称' }}</li>
              </ul>
            </div>
            <div v-if="!tooMany && (!selectedChatId || group.chat_id === selectedChatId)" class="wecom-qr" data-v2="group-qr">
              <strong>群聊二维码</strong>
              <img v-if="qr[group.chat_id]?.status === 'ready' && qr[group.chat_id]?.qr_url" :src="qr[group.chat_id]?.qr_url" :alt="`${group.name}群聊二维码`" @error="imageFailed(group.chat_id)">
              <p v-else>{{ qr[group.chat_id]?.message || '正在获取群码…' }}</p>
              <button v-if="qr[group.chat_id]?.status === 'unavailable' || (qr[group.chat_id]?.status === 'pending' && qr[group.chat_id]?.message === '群码仍在处理中，请稍后重试')" type="button" class="cv2-btn cv2-btn--sm" @click="void loadQr(group.chat_id)">重试</button>
            </div>
          </article>
          <nav v-if="tooMany && (previousPages.length > 0 || nextAfter !== null)" class="wecom-pages" aria-label="客户群分页">
            <button type="button" class="cv2-btn cv2-btn--sm" :disabled="previousPages.length === 0" @click="void previousPage()">上一页</button>
            <span>第 {{ previousPages.length + 1 }} 页</span>
            <button type="button" class="cv2-btn cv2-btn--sm" :disabled="nextAfter === null" @click="void nextPage()">下一页</button>
          </nav>
        </template>
      </aside>
      <V2WecomArchivePane :key="orderNo" class="wecom-chat" :order-no="orderNo"
        @group-selected="selectedChatId = $event">
        <template v-if="archiveSearchAvailable" #search>
          <RouterLink class="cv2-btn cv2-btn--sm" :to="{
            name: 'v2-wecom-archive', state: { wecomArchiveKeyword: sourceOrderNo?.trim() || orderNo },
          }">
            查找聊天记录
          </RouterLink>
        </template>
      </V2WecomArchivePane>
    </div>
  </section>
</template>

<style scoped>
:global(.ttv2 .dtl-face-content:has(.wecom-layout > .wecom-chat)) { overflow: clip; }
/* 已载入的聊天保留阅读高度，超出时先滚到聊天区，消息仍在内框滚动。 */
:global(.ttv2 .el-dialog .dtl-face-content:has(.wecom-chat .archive-frame iframe)) { overflow: auto; }
:global(.ttv2 .el-dialog .wecom-chat:has(.archive-frame iframe)) { min-height: 260px; }
:global(.ttv2 .el-dialog .wecom-chat .archive-body:has(.archive-frame iframe)) { min-height: min-content; }
:global(.ttv2 .el-dialog .wecom-chat .archive-frame:has(iframe)) { min-height: 120px; }
.wecom-groups { padding: 18px; container-type: inline-size; }
.wecom-groups:has(.wecom-chat) { box-sizing: border-box; height: 100%; }
.wecom-layout { display: grid; gap: 16px; align-items: start; }
.wecom-layout:has(> .wecom-chat) {
  height: 100%;
  grid-template-columns: minmax(210px, 28%) minmax(0, 1fr);
  grid-template-rows: minmax(0, 1fr);
}
.wecom-details, .wecom-chat { min-width: 0; }
:global(.ttv2 .el-dialog .wecom-layout:has(> .wecom-chat) > .wecom-details) {
  align-self: stretch;
  min-height: 0;
  overflow: auto;
  scrollbar-width: none;
}
:global(.ttv2 .el-dialog .wecom-layout:has(> .wecom-chat) > .wecom-details::-webkit-scrollbar) { display: none; }
.wecom-group-context { color: var(--tt2-dim); }
.wecom-groups .wecom-chat { align-self: stretch; min-height: 0; margin: 0; }
.wecom-group { display: grid; grid-template-columns: minmax(0, 1fr) 200px; gap: 20px; padding: 12px 0; border-bottom: 1px solid var(--tt2-line); }
.wecom-layout:has(> .wecom-chat) .wecom-group, .wecom-group--no-qr { grid-template-columns: minmax(0, 1fr); }
.wecom-group--selected { border-left: 2px solid var(--tt2-primary); padding-left: 10px; }
.wecom-info { min-width: 0; overflow-wrap: anywhere; }
.wecom-group h4 { margin: 0 0 8px; }
.wecom-group p { margin: 6px 0; }
.wecom-group ul { display: flex; flex-wrap: wrap; gap: 8px 20px; padding-left: 20px; }
.wecom-qr { display: flex; flex-direction: column; align-items: center; gap: 8px; text-align: center; overflow-wrap: anywhere; }
.wecom-qr img { display: block; width: 180px; max-width: 100%; height: auto; }
.wecom-pages { display: flex; align-items: center; justify-content: center; gap: 12px; padding: 16px 0; }
@container (max-width: 680px) {
  .wecom-layout:has(> .wecom-chat), .wecom-group { grid-template-columns: minmax(0, 1fr); }
  .wecom-layout:has(> .wecom-chat) { gap: 8px; grid-template-rows: auto minmax(0, 1fr); }
  :global(.ttv2 .el-dialog .wecom-layout:has(> .wecom-chat)) { grid-template-rows: minmax(0, min(42%, 180px)) minmax(0, 1fr); }
  .wecom-layout:has(> .wecom-chat) .wecom-group:not(.wecom-group--no-qr) {
    grid-template-columns: minmax(0, 1fr) minmax(72px, 112px);
    gap: 8px;
  }
  .wecom-layout:has(> .wecom-chat) .wecom-group { padding-block: 6px; }
  .wecom-layout:has(> .wecom-chat) .wecom-group ul { margin-block: 4px; gap: 4px 12px; }
}
</style>
