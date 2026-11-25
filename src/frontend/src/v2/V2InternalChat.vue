<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import { layuiIconCodePoint } from '@/config/layuiIconNames'
import {
  conversationTitle, demoContacts, orderTitle, useDemoChat,
  type DemoConversation, type DemoMessage, type DemoPerson,
} from './internalChat/demoChat'

const chat = useDemoChat()
const searchQuery = ref('')
const contactQuery = ref('')
const showingContacts = ref(false)
const showingThread = ref(false)
const searchInput = ref<HTMLInputElement>()
const threadTitle = ref<HTMLElement>()
const composeButton = ref<HTMLButtonElement>()
const inbox = computed(() => {
  const query = searchQuery.value.trim().toLowerCase()
  return [...chat.conversations]
    .filter((conversation) => {
      const terms = [conversationTitle(conversation)]
      if (conversation.kind === 'order') {
        terms.push(orderTitle(conversation), ...conversation.members.map((member) => member.name))
      }
      return terms.some((term) => term.toLowerCase().includes(query))
    })
    .sort((a, b) => Number(b.pinned) - Number(a.pinned) || lastActivity(b) - lastActivity(a))
})
const contacts = computed(() => {
  const query = contactQuery.value.trim().toLowerCase()
  return demoContacts.filter((contact) => contact.name.toLowerCase().includes(query))
})
const settingsOpen = ref(false)
const remarkOpen = ref(false)
const remark = ref('')
const remarkInput = ref<HTMLInputElement>()
const settingsButton = ref<HTMLButtonElement>()
const editRemarkButton = ref<HTMLButtonElement>()
const threadHeader = ref<HTMLElement>()
const messageLog = ref<HTMLElement>()
const editor = ref<HTMLTextAreaElement>()
const mentionList = ref<HTMLElement>()
const highlighted = ref<string>()
const quotedDraft = computed(() => quotedMessage(chat.selected.draft.replyTo))

type MenuTarget = { conversation: DemoConversation } | { message: DemoMessage }
const context = shallowRef<{ target: MenuTarget; trigger: HTMLElement }>()
const menu = ref<HTMLElement>()
const menuPosition = ref({ left: '0px', top: '0px' })
const contextLabel = computed(() => {
  const target = context.value?.target
  if (!target) return ''
  if ('message' in target) return '引用'
  return target.conversation.pinned ? '取消置顶' : '置顶会话'
})

const mention = ref<{ start: number; end: number; query: string }>()
const mentionResults = computed(() => {
  if (!mention.value || chat.selected.kind !== 'order') return []
  const query = mention.value.query.toLowerCase()
  return chat.selected.members.filter((member) => member.name.toLowerCase().includes(query))
})

watch(() => [chat.selected.id, chat.selected.messages.length], scrollToLatest, { flush: 'post' })
onMounted(() => {
  scrollToLatest()
  document.addEventListener('pointerdown', dismissOutside)
})
onUnmounted(() => document.removeEventListener('pointerdown', dismissOutside))

function latestMessage(conversation: DemoConversation): DemoMessage | undefined {
  return conversation.messages[conversation.messages.length - 1]
}

function lastActivity(conversation: DemoConversation): number {
  return latestMessage(conversation)?.minute ?? conversation.startedAt
}

function showThread(): void {
  showingThread.value = true
  settingsOpen.value = false
  remarkOpen.value = false
  mention.value = undefined
  highlighted.value = undefined
  dismissContext()
  void nextTick(() => {
    threadTitle.value?.focus({ preventScroll: true })
    scrollToLatest()
  })
}

function selectConversation(conversation: DemoConversation): void {
  chat.select(conversation)
  showThread()
}

function showContacts(): void {
  showingContacts.value = true
  contactQuery.value = ''
  void nextTick(() => searchInput.value?.focus())
}

function returnToInbox(): void {
  showingContacts.value = false
  showingThread.value = false
  void nextTick(() => composeButton.value?.focus())
}

function openDirect(contact: DemoPerson): void {
  chat.openDirect(contact)
  showingContacts.value = false
  searchQuery.value = ''
  showThread()
}

function rowLabel(conversation: DemoConversation): string {
  return [
    conversationTitle(conversation),
    conversation.kind === 'order' && conversation.remark ? orderTitle(conversation) : '',
    conversation.pinned ? '已置顶' : '',
    conversation.unread ? `${conversation.unread} 条未读消息` : '',
  ].filter(Boolean).join('，')
}

function toggleSettings(): void {
  settingsOpen.value = !settingsOpen.value
  remarkOpen.value = false
  if (settingsOpen.value) void nextTick(() => editRemarkButton.value?.focus())
}

function editRemark(): void {
  if (chat.selected.kind !== 'order') return
  remark.value = chat.selected.remark
  settingsOpen.value = false
  remarkOpen.value = true
  void nextTick(() => remarkInput.value?.focus())
}

function closeRemark(): void {
  remarkOpen.value = false
  settingsButton.value?.focus()
}

function saveRemark(): void {
  if (chat.selected.kind !== 'order') return
  chat.selected.remark = remark.value.trim()
  closeRemark()
}

function scrollToLatest(): void {
  if (messageLog.value) messageLog.value.scrollTop = messageLog.value.scrollHeight
}

function clock(minute: number): string {
  const hours = String(Math.floor(minute / 60) % 24).padStart(2, '0')
  return `${hours}:${String(minute % 60).padStart(2, '0')}`
}

function showMessageTime(index: number, minute: number): boolean {
  const previous = chat.selected.messages[index - 1]
  return !previous || minute - previous.minute >= 5
}

function senderName(message: DemoMessage): string {
  return message.sender?.name ?? '我'
}

function quotedMessage(id: string | undefined): DemoMessage | undefined {
  return chat.selected.messages.find((message) => message.id === id)
}

const messages = computed(() => chat.selected.messages.map((message) => ({
  ...message,
  quote: quotedMessage(message.replyTo),
})))

function jumpToMessage(id: string): void {
  highlighted.value = id
  const original = messageLog.value?.querySelector<HTMLElement>(`#cv2-chat-message-${id} .cv2-chat-bubble`)
  original?.focus({ preventScroll: true })
  original?.scrollIntoView?.({ block: 'nearest' })
}

function isMention(text: string): boolean {
  return chat.selected.kind === 'order' && chat.selected.members.some((member) => text === `@${member.name}`)
}

function updateMention(event: Event): void {
  if (chat.selected.kind !== 'order') return
  const textarea = event.target as HTMLTextAreaElement
  const start = textarea.selectionStart
  const end = textarea.selectionEnd
  const match = start === end && textarea.value.slice(0, end).match(/@([^@\s]*)$/)
  mention.value = match ? { start: end - (match[1]?.length ?? 0) - 1, end, query: match[1] ?? '' } : undefined
}

function insertMention(person: DemoPerson): void {
  const range = mention.value
  if (!range) return
  const draft = chat.selected.draft
  const inserted = `@${person.name} `
  draft.text = draft.text.slice(0, range.start) + inserted + draft.text.slice(range.end)
  mention.value = undefined
  void nextTick(() => {
    editor.value?.focus()
    editor.value?.setSelectionRange(range.start + inserted.length, range.start + inserted.length)
  })
}

function submitMessage(): void {
  chat.send()
  mention.value = undefined
  editor.value?.focus()
}

function handleEditorKey(event: KeyboardEvent): void {
  if (event.isComposing) return
  if (mentionResults.value.length && (event.key === 'ArrowDown' || (event.key === 'Enter' && !event.shiftKey))) {
    event.preventDefault()
    mentionList.value?.querySelector('button')?.focus()
    return
  }
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    submitMessage()
  }
}

function moveMentionFocus(event: KeyboardEvent): void {
  if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
  const options = [...(mentionList.value?.querySelectorAll('button') ?? [])]
  const current = options.findIndex((button) => button === event.target)
  const step = event.key === 'ArrowDown' ? 1 : -1
  event.preventDefault()
  options[(current + step + options.length) % options.length]?.focus()
}

function beginContext(event: MouseEvent | KeyboardEvent, target: MenuTarget): void {
  if ('key' in event && event.key !== 'ContextMenu' && !(event.key === 'F10' && event.shiftKey)) return
  event.preventDefault()
  const trigger = event.currentTarget as HTMLElement
  const bounds = trigger.getBoundingClientRect()
  const fromPointer = 'clientX' in event && (event.clientX !== 0 || event.clientY !== 0)
  const left = fromPointer ? event.clientX : bounds.left + 8
  const top = fromPointer ? event.clientY : bounds.bottom
  menuPosition.value = { left: `${left}px`, top: `${top}px` }
  context.value = { target, trigger }
  settingsOpen.value = false
  remarkOpen.value = false
  mention.value = undefined
  void nextTick(() => {
    if (!menu.value) return
    menuPosition.value = {
      left: `${Math.max(4, Math.min(left, window.innerWidth - menu.value.offsetWidth - 4))}px`,
      top: `${Math.max(4, Math.min(top, window.innerHeight - menu.value.offsetHeight - 4))}px`,
    }
    menu.value.querySelector('button')?.focus({ preventScroll: true })
  })
}

function dismissContext(restoreFocus = false): void {
  const trigger = context.value?.trigger
  context.value = undefined
  if (restoreFocus) void nextTick(() => trigger?.focus({ preventScroll: true }))
}

function useContextAction(): void {
  const target = context.value?.target
  if (!target) return
  if ('conversation' in target) {
    target.conversation.pinned = !target.conversation.pinned
    dismissContext(true)
  } else {
    chat.selected.draft.replyTo = target.message.id
    dismissContext()
    editor.value?.focus()
  }
}

function dismissOutside(event: PointerEvent): void {
  const target = event.target as Node
  if (!menu.value?.contains(target)) dismissContext()
  if (!threadHeader.value?.contains(target)) {
    settingsOpen.value = false
    remarkOpen.value = false
  }
  if (target !== editor.value && !mentionList.value?.contains(target)) mention.value = undefined
}

function handleEscape(event: KeyboardEvent): void {
  if (context.value) dismissContext(true)
  else if (mention.value) {
    mention.value = undefined
    editor.value?.focus()
  } else if (settingsOpen.value || remarkOpen.value) {
    settingsOpen.value = false
    closeRemark()
  } else if (showingContacts.value) returnToInbox()
  else return
  event.preventDefault()
}
</script>

<template>
  <div
    class="cv2-chat-workspace"
    :class="{ 'cv2-chat-workspace--thread': showingThread }"
    @keydown.esc="handleEscape"
  >
    <section class="cv2-chat-inbox" aria-label="会话列表">
      <header class="cv2-chat-inbox-header">
        <div class="cv2-chat-inbox-heading">
          <button
            v-if="showingContacts"
            type="button"
            class="cv2-chat-icon"
            aria-label="返回会话"
            @click="returnToInbox"
          ><i class="layui-icon" aria-hidden="true">{{ layuiIconCodePoint('left') }}</i></button>
          <h1>{{ showingContacts ? '通讯录' : '会话' }}</h1>
          <button
            v-if="!showingContacts"
            ref="composeButton"
            type="button"
            class="cv2-chat-icon cv2-chat-new"
            aria-label="发起会话"
            title="发起会话"
            @click="showContacts"
          ><i class="layui-icon layui-icon-edit" aria-hidden="true"></i></button>
        </div>
        <label class="cv2-chat-search">
          <i class="layui-icon" aria-hidden="true">{{ layuiIconCodePoint('search') }}</i>
          <input
            v-if="showingContacts"
            ref="searchInput"
            v-model="contactQuery"
            type="search"
            aria-label="搜索联系人"
            placeholder="搜索联系人"
          />
          <input
            v-else
            v-model="searchQuery"
            type="search"
            aria-label="搜索会话"
            placeholder="搜索会话"
          />
        </label>
      </header>
      <ul v-if="!showingContacts" class="cv2-chat-list">
        <li v-for="conversation in inbox" :key="conversation.id">
          <button
            type="button"
            class="cv2-chat-row"
            :aria-current="conversation.id === chat.selected.id ? 'true' : undefined"
            :aria-label="rowLabel(conversation)"
            aria-keyshortcuts="Shift+F10"
            @click="selectConversation(conversation)"
            @contextmenu="beginContext($event, { conversation })"
            @keydown="beginContext($event, { conversation })"
          >
            <span class="cv2-chat-avatar" aria-hidden="true">
              <i v-if="conversation.kind === 'order'" class="layui-icon layui-icon-group"></i>
              <template v-else>{{ conversation.contact.name.slice(0, 1) }}</template>
            </span>
            <span class="cv2-chat-summary">
              <strong
                class="cv2-chat-title"
                :class="{ 'cv2-chat-title--canonical': conversation.kind === 'order' && !conversation.remark }"
              >{{ conversationTitle(conversation) }}</strong>
              <small v-if="conversation.kind === 'order' && conversation.remark" class="cv2-chat-order">
                {{ orderTitle(conversation) }}
              </small>
              <span class="cv2-chat-preview">{{ latestMessage(conversation)?.text || '暂无消息' }}</span>
            </span>
            <span v-if="conversation.unread || conversation.pinned" class="cv2-chat-row-status">
              <i v-if="conversation.pinned" class="layui-icon" role="img" aria-label="已置顶">{{ layuiIconCodePoint('top') }}</i>
              <span v-if="conversation.unread" class="cv2-chat-count" :aria-label="`${conversation.unread} 条未读消息`">
                {{ conversation.unread }}
              </span>
            </span>
          </button>
        </li>
        <li v-if="!inbox.length" class="cv2-chat-empty">没有匹配的会话</li>
      </ul>
      <ul v-else class="cv2-chat-list" aria-label="通讯录">
        <li v-for="contact in contacts" :key="contact.id">
          <button type="button" class="cv2-chat-contact" @click="openDirect(contact)">
            <span class="cv2-chat-avatar" aria-hidden="true">{{ contact.name.slice(0, 1) }}</span>
            <span>{{ contact.name }}<small>{{ contact.role }}</small></span>
          </button>
        </li>
        <li v-if="!contacts.length" class="cv2-chat-empty">没有匹配的联系人</li>
      </ul>
    </section>
    <section class="cv2-chat-thread" aria-label="会话窗口">
      <header ref="threadHeader" class="cv2-chat-thread-header">
        <button type="button" class="cv2-chat-icon cv2-chat-back" aria-label="返回会话列表" @click="returnToInbox">
          <i class="layui-icon" aria-hidden="true">{{ layuiIconCodePoint('left') }}</i>
        </button>
        <div class="cv2-chat-thread-title">
          <h2
            ref="threadTitle"
            tabindex="-1"
            :class="{ 'cv2-chat-title--canonical': chat.selected.kind === 'order' && !chat.selected.remark }"
          >{{ conversationTitle(chat.selected) }}</h2>
          <small v-if="chat.selected.kind === 'order' && chat.selected.remark" class="cv2-chat-order">
            {{ orderTitle(chat.selected) }}
          </small>
          <small>{{ chat.selected.kind === 'order' ? `${chat.selected.members.length + 1} 位成员` : chat.selected.contact.role }}</small>
        </div>
        <template v-if="chat.selected.kind === 'order'">
          <button
            ref="settingsButton"
            type="button"
            class="cv2-chat-icon"
            aria-label="会话设置"
            :aria-expanded="settingsOpen || remarkOpen"
            @click="toggleSettings"
          ><i class="layui-icon" aria-hidden="true">{{ layuiIconCodePoint('more') }}</i></button>
          <div v-if="settingsOpen" class="cv2-chat-popup">
            <button ref="editRemarkButton" type="button" class="cv2-btn" aria-label="编辑备注" @click="editRemark">编辑备注</button>
          </div>
          <div v-if="remarkOpen" class="cv2-chat-popup cv2-chat-remark" role="dialog" aria-label="编辑会话备注">
            <form @submit.prevent="saveRemark">
              <label for="cv2-chat-remark">会话备注</label>
              <input id="cv2-chat-remark" ref="remarkInput" v-model="remark" maxlength="30" placeholder="输入仅自己可见的备注" />
              <div>
                <button type="button" class="cv2-btn" @click="closeRemark">取消</button>
                <button type="submit" class="cv2-btn cv2-btn--primary">保存</button>
              </div>
            </form>
          </div>
        </template>
      </header>
      <div ref="messageLog" role="log" aria-label="聊天记录" class="cv2-chat-log">
        <p v-if="!messages.length" class="cv2-chat-empty">暂无消息</p>
        <template v-for="(message, index) in messages" :key="message.id">
          <time v-if="showMessageTime(index, message.minute)" class="cv2-chat-time">
            {{ index === 0 ? '今天 ' : '' }}{{ clock(message.minute) }}
          </time>
          <article
            :id="`cv2-chat-message-${message.id}`"
            class="cv2-chat-message"
            :class="{
              'cv2-chat-message--own': !message.sender,
              'cv2-chat-message--highlighted': highlighted === message.id,
            }"
            :aria-label="message.sender ? undefined : '我发送的消息'"
          >
            <span class="cv2-chat-avatar" aria-hidden="true">{{ senderName(message).slice(0, 1) }}</span>
            <div class="cv2-chat-message-content">
              <span v-if="message.sender" class="cv2-chat-sender">{{ message.sender.name }} · {{ message.sender.role }}</span>
              <div
                class="cv2-chat-bubble"
                tabindex="0"
                @contextmenu="beginContext($event, { message })"
                @keydown="beginContext($event, { message })"
              >
                <button
                  v-if="message.quote"
                  type="button"
                  class="cv2-chat-quoted-message"
                  :aria-label="`查看引用原文，${senderName(message.quote)}：${message.quote.text}`"
                  @click="jumpToMessage(message.quote.id)"
                >{{ senderName(message.quote) }}：{{ message.quote.text }}</button>
                <p><template v-for="(part, partIndex) in message.text.split(/(@[^\s@]+)/g)" :key="partIndex">
                  <span v-if="isMention(part)" class="cv2-chat-mention-mark">{{ part }}</span>
                  <template v-else>{{ part }}</template>
                </template></p>
              </div>
            </div>
          </article>
        </template>
      </div>
      <form class="cv2-chat-composer" @submit.prevent="submitMessage">
        <div v-if="quotedDraft" class="cv2-chat-draft-quote">
          <div><strong>引用 {{ senderName(quotedDraft) }}</strong><span>{{ quotedDraft.text }}</span></div>
          <button
            type="button"
            class="cv2-chat-icon"
            aria-label="取消引用"
            @click="chat.selected.draft.replyTo = undefined; editor?.focus()"
          ><i class="layui-icon" aria-hidden="true">{{ layuiIconCodePoint('close') }}</i></button>
        </div>
        <div class="cv2-chat-editor">
          <textarea
            ref="editor"
            v-model="chat.selected.draft.text"
            rows="2"
            maxlength="500"
            aria-label="输入消息"
            placeholder="输入消息…"
            @input="updateMention"
            @selectionchange="updateMention"
            @keydown="handleEditorKey"
          ></textarea>
          <div class="cv2-chat-compose-actions">
            <span>Enter 发送 · Shift+Enter 换行</span>
            <button type="submit" class="cv2-btn cv2-btn--primary" :disabled="!chat.selected.draft.text.trim()">发送</button>
          </div>
        </div>
        <div
          v-if="mentionResults.length"
          ref="mentionList"
          class="cv2-chat-popup cv2-chat-mentions"
          role="group"
          aria-label="选择要 @ 的成员"
          @keydown="moveMentionFocus"
        >
          <button
            v-for="person in mentionResults"
            :key="person.id"
            type="button"
            :aria-label="`@${person.name}`"
            @click="insertMention(person)"
          >{{ person.name }} <small>{{ person.role }}</small></button>
        </div>
      </form>
    </section>
    <div
      v-if="context"
      ref="menu"
      class="cv2-chat-popup cv2-chat-context"
      role="menu"
      :aria-label="'conversation' in context.target ? '会话操作' : '消息操作'"
      :style="menuPosition"
      @focusout="!menu?.contains($event.relatedTarget as Node) && dismissContext()"
    >
      <button type="button" role="menuitem" @click="useContextAction">{{ contextLabel }}</button>
    </div>
  </div>
</template>
