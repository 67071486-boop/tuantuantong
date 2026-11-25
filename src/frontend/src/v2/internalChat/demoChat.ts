import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'
import { useSessionStore } from '@/stores/session'

export interface DemoPerson {
  id: string
  name: string
  role: string
}

export interface DemoMessage {
  id: string
  sender?: DemoPerson
  minute: number
  text: string
  replyTo?: string
}

interface ConversationState {
  id: string
  unread: number
  pinned: boolean
  messages: DemoMessage[]
  startedAt: number
  draft: { text: string; replyTo?: string }
}

type OrderConversation = ConversationState & {
  kind: 'order'
  orderNumber: string
  remark: string
  members: DemoPerson[]
}

type DirectConversation = ConversationState & { kind: 'direct'; contact: DemoPerson }

export type DemoConversation = OrderConversation | DirectConversation

const lin = { id: 'lin', name: '小林', role: '客服' }
const chen = { id: 'chen', name: '陈晨', role: '派单员' }
const luo = { id: 'luo', name: '阿洛', role: '代练员' }
const qiao = { id: 'qiao', name: '小乔', role: '代练员' }
const zhou = { id: 'zhou', name: '小周', role: '代练员' }
const bei = { id: 'bei', name: '小北', role: '代练员' }
const qing = { id: 'qing', name: '阿青', role: '派单员' }

export const demoContacts: readonly DemoPerson[] = [lin, chen, luo, qiao]

function order(
  orderNumber: string,
  members: DemoPerson[],
  messages: DemoMessage[],
  { unread = 0, pinned = false, remark = '' }: { unread?: number; pinned?: boolean; remark?: string } = {},
): OrderConversation {
  return {
    kind: 'order', id: orderNumber, orderNumber, members, messages, unread, remark,
    pinned, startedAt: messages[0]?.minute ?? 0, draft: { text: '' },
  }
}

function initialConversations(): DemoConversation[] {
  return [
    order('20261116000000000086', [lin, luo], [
      { id: 'a1', sender: lin, minute: 867, text: '已经指派给阿洛，今天可以开始。' },
      { id: 'a2', minute: 868, text: '@阿洛 开打前在群里说一声。' },
      { id: 'a3', sender: luo, minute: 869, text: '收到，已经开打。' },
      { id: 'a4', sender: luo, minute: 882, text: '第一局打完了，结束后我把结算截图发这里。' },
      { id: 'a5', minute: 883, text: '好，发这里就行。', replyTo: 'a4' },
    ]),
    order('20261116000000000091', [zhou, lin], [
      { id: 'b1', sender: zhou, minute: 844, text: '打完第一局了。' },
      { id: 'b2', sender: zhou, minute: 847, text: '现在白银一 82 分。' },
      { id: 'b3', sender: zhou, minute: 852, text: '还差一局，打完发截图。' },
    ], { unread: 3, pinned: true, remark: '今晚要交的单' }),
    order('20261116000000000074', [bei, qing], [
      { id: 'c1', sender: bei, minute: 828, text: '已打到王者。' },
      { id: 'c2', sender: qing, minute: 834, text: '结算截图已经上传，可以验收了。' },
    ], { unread: 2 }),
    order('20261116000000000068', [lin, luo], [
      { id: 'd1', sender: lin, minute: 680, text: '这笔单已验收并结算。' },
    ]),
  ]
}

export function orderTitle(conversation: OrderConversation): string {
  return `订单-${conversation.orderNumber}`
}

export function conversationTitle(conversation: DemoConversation): string {
  return conversation.kind === 'order'
    ? conversation.remark || orderTitle(conversation)
    : conversation.contact.name
}

export const useDemoChat = defineStore('v2-internal-chat-demo', () => {
  const session = useSessionStore()
  const conversations = ref(initialConversations())
  const selectedId = ref(conversations.value[0]!.id)
  let nextMinute = 884
  let nextId = 1

  // 跟随现有认证态，切换用户或退出时丢弃上一位体验者的输入。
  watch([() => session.status, () => session.identityPlane, () => session.user?.id], () => {
    conversations.value = initialConversations()
    selectedId.value = conversations.value[0]!.id
    nextMinute = 884
    nextId = 1
  }, { flush: 'sync' })

  const selected = computed(() => conversations.value.find((chat) => chat.id === selectedId.value)!)
  const unreadTotal = computed(() => conversations.value.reduce((total, chat) => total + chat.unread, 0))

  function select(chat: DemoConversation): void {
    chat.unread = 0
    selectedId.value = chat.id
  }

  function openDirect(contact: DemoPerson): void {
    const id = `direct-${contact.id}`
    const existing = conversations.value.find((chat) => chat.id === id)
    if (existing) {
      select(existing)
      return
    }
    conversations.value.push({
      id, kind: 'direct', contact, unread: 0, pinned: false, startedAt: nextMinute++,
      messages: [], draft: { text: '' },
    })
    selectedId.value = id
  }

  function send(): void {
    const chat = selected.value
    const text = chat.draft.text.trim()
    if (!text) return
    chat.messages.push({
      id: `demo-message-${nextId++}`, minute: nextMinute++, text, replyTo: chat.draft.replyTo,
    })
    chat.draft = { text: '' }
  }

  return { conversations, selected, select, openDirect, send, unreadTotal }
})
