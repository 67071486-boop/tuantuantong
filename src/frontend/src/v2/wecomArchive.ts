import { request } from '@/api/http'

export type ArchiveGroup = {
  chat_id: string; name: string; match: 'name' | 'message' | 'linked'; proof: string; anchor_msgid?: string
  remember?: boolean
}
export type ArchiveMessage = {
  msgid: string; secret_key: string; sent_at: string
  sender?: {
    id: string; type: 1 | 2 | 3; is_self: boolean
    external_kind?: 'wechat' | 'wecom'; corp_name?: string
  }
}

type MessageRow = {
  msgid: string; secret_key: string; time: string; self: boolean
  nameType: string; avatarType: string; identity: string; senderLabel: string; senderBadge: string
  senderBadgeKind: '' | 'wechat' | 'company' | 'external'; matched: boolean
  isViewportAnchor: boolean; anchorLostSeparator: boolean
}
type ChatData = { messages: MessageRow[]; anchorOffset: number; followLatest: boolean }
type ChatPosition = { kind: 'latest' } | { kind: 'preserve' | 'focus'; anchorId: string }
type ArchiveGroupsResult = { enabled: boolean; groups: ArchiveGroup[]; complete: boolean; requires_login?: boolean }

type Signature = {
  corp_id: string; agent_id: number; timestamp: number; nonce: string
  corp_signature: string; agent_signature: string; verified: boolean; login_state: string
}

type SignatureValue = { timestamp: number; nonceStr: string; signature: string }
type ChatFrame = {
  el: HTMLIFrameElement
  setData(data: ChatData): Promise<void>
  dispose(): void
}
type ScrollEvent = { detail: { scrollTop: number } }
type ArchiveSdk = {
  register(options: {
    corpId: string; agentId: number; jsApiList: string[]
    getConfigSignature: (url: string) => Promise<SignatureValue>
    getAgentConfigSignature: (url: string) => Promise<SignatureValue>
  }): void
  initOpenData(): Promise<unknown>
  createOpenDataFrameFactory(): {
    createOpenDataFrame(options: {
      handleMounted(): void
      handleUpdated(): void
      el: HTMLElement; data: ChatData; template: string; style: string
      methods: { error(): void; updateScroll(event: ScrollEvent): void; loadEarlier(): void }
    }): ChatFrame
  }
  createScrollViewContext(frame: ChatFrame, name: string): Promise<{
    scrollIntoView(selector: string, options: { block: 'start' | 'end' }): void
  } | undefined>
  createWWLoginPanel(options: {
    el: HTMLElement
    params: {
      login_type: 'CorpApp'; appid: string; agentid: string; redirect_uri: string
      state: string; redirect_type: 'callback'; panel_size: 'small'
    }
    onLoginSuccess(result: { code: string }): void
    onLoginFail(): void
  }): { unmount(): void }
}

declare global { interface Window { ww?: ArchiveSdk } }

const JS_SDK = 'https://wwcdn.weixin.qq.com/node/wework/wwopen/js/wecom-jssdk-2.4.0.js'
const OPEN_DATA_SDK = 'https://open.work.weixin.qq.com/wwopen/js/jwxwork-1.0.0.js'
let sdkReady: Promise<ArchiveSdk> | undefined
let openDataLoaded = false

function script(src: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const element = document.createElement('script')
    const fail = () => {
      window.clearTimeout(timer)
      element.remove()
      reject(new Error('企微组件加载失败，请重试'))
    }
    const timer = window.setTimeout(fail, 12000)
    element.src = src
    element.onload = () => {
      window.clearTimeout(timer)
      resolve()
    }
    element.onerror = fail
    document.head.appendChild(element)
  })
}

export function loadWecomArchiveSdk(): Promise<ArchiveSdk> {
  sdkReady ??= (async () => {
    if (!openDataLoaded) {
      await script(OPEN_DATA_SDK)
      openDataLoaded = true
    }
    if (!window.ww) await script(JS_SDK)
    if (!window.ww?.createOpenDataFrameFactory || !window.ww?.createWWLoginPanel) {
      throw new Error('企微组件暂不可用')
    }
    return window.ww
  })().catch((error: unknown) => {
    sdkReady = undefined
    throw error
  })
  return sdkReady
}

function archivePath(orderNo: string | undefined, page: string) {
  return orderNo === undefined
    ? `/api/wecom/archive/${page}/`
    : `/api/wecom/orders/${encodeURIComponent(orderNo)}/chat/${page}/`
}

export function archiveAccess() {
  return request<{ enabled: boolean }>({ url: archivePath(undefined, 'access') })
}

export function archiveSearch(q: string, startDate: string, endDate: string) {
  return request<ArchiveGroupsResult>({
    url: archivePath(undefined, 'search'),
    method: 'POST', data: { q, start_date: startDate, end_date: endDate },
  })
}

export function archiveGroups(orderNo: string) {
  return request<ArchiveGroupsResult>({
    url: archivePath(orderNo, 'groups'),
  })
}

export function archiveRememberGroup(orderNo: string, group: Pick<ArchiveGroup, 'chat_id' | 'proof'>) {
  return request<{ chat_id: string }>({
    url: archivePath(orderNo, 'groups'),
    method: 'POST',
    data: { chat_id: group.chat_id },
    headers: { 'X-Wecom-Chat-Proof': group.proof },
  })
}

export function archiveMessages(orderNo: string | undefined, group: ArchiveGroup, before: number | null,
  range?: { around?: string; after?: number }) {
  return request<{
    messages: ArchiveMessage[]; next_before: number | null; next_after?: number | null; requires_login: boolean
  }>({
    url: archivePath(orderNo, 'messages'),
    params: { chat_id: group.chat_id, ...(before === null ? {} : { before }), ...range },
    ...(group.proof ? { headers: { 'X-Wecom-Chat-Proof': group.proof } } : {}),
  })
}

export function archiveSignature(orderNo: string | undefined, url = location.href.split('#')[0], reauthenticate = false) {
  return request<Signature>({
    url: archivePath(orderNo, 'signature'), params: { url, ...(reauthenticate ? { reauthenticate: true } : {}) },
  })
}

export function archiveLogin(orderNo: string | undefined, code: string, state: string) {
  return request<{ verified: boolean }>({ url: archivePath(orderNo, 'login'), method: 'POST', data: { code, state } })
}

const CHAT_DATE = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: 'long', day: 'numeric',
})
const CHAT_CLOCK = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
})
const CHAT_GAP_MS = 5 * 60_000

function messageTime(timestamp: number, previous: ArchiveMessage | undefined) {
  if (!Number.isFinite(timestamp)) return ''
  const previousTime = Date.parse(previous?.sent_at ?? '')
  const day = CHAT_DATE.format(timestamp)
  if (!Number.isFinite(previousTime) || CHAT_DATE.format(previousTime) !== day) {
    return `${day} ${CHAT_CLOCK.format(timestamp)}`
  }
  return timestamp - previousTime >= CHAT_GAP_MS ? CHAT_CLOCK.format(timestamp) : ''
}

function messageRow(message: ArchiveMessage, chatId: string, previous: ArchiveMessage | undefined,
  position: ChatPosition, focusedId: string | undefined): MessageRow {
  const { msgid, secret_key } = message
  const sender = message.sender?.id ? message.sender : undefined
  const employee = sender?.type === 1
  const external = sender?.type === 2
  const timestamp = Date.parse(message.sent_at)
  const time = messageTime(timestamp, previous)
  const isViewportAnchor = position.kind !== 'latest' && msgid === position.anchorId
  let senderBadge = ''
  let senderBadgeKind: MessageRow['senderBadgeKind'] = ''
  if (external) {
    switch (sender.external_kind) {
      case 'wecom':
        senderBadge = `@${sender.corp_name?.trim() || '企业微信'}`
        senderBadgeKind = 'company'
        break
      case 'wechat':
        senderBadge = '@微信'
        senderBadgeKind = 'wechat'
        break
      default:
        senderBadge = '@外部联系人'
        senderBadgeKind = 'external'
    }
  }
  return {
    msgid, secret_key, self: employee && sender.is_self, time,
    nameType: employee ? 'userName' : 'externalUserName',
    avatarType: employee ? 'userAvatar' : 'externalUserAvatar',
    identity: employee ? sender.id : external ? `${chatId}/${sender.id}` : '',
    senderLabel: sender?.type === 3 ? '机器人' : '未知发送人',
    senderBadge, senderBadgeKind, matched: msgid === focusedId,
    isViewportAnchor,
    // The former first message had a separator even if the older page now groups with it.
    anchorLostSeparator: position.kind === 'preserve' && isViewportAnchor && Number.isFinite(timestamp) && !time,
  }
}

const CHAT_TEMPLATE = `
  <scroll-view ref="chat" class="chat-scroll" scroll-y="{{true}}" bindscroll="updateScroll" bindscrolltoupper="loadEarlier">
    <view class="chat-feed{{data.followLatest ? ' chat-feed--latest' : ''}}">
      <view wx:for="{{data.messages}}" wx:key="msgid" class="chat-message{{item.matched ? ' chat-message--match' : ''}}">
        <view wx:if="{{item.isViewportAnchor}}" class="chat-viewport-anchor{{item.anchorLostSeparator ? ' chat-viewport-anchor--without-time' : ''}}"
          style="top: {{data.anchorOffset}}px;" aria-hidden="true" />
        <view wx:if="{{item.time}}" class="chat-time-separator">{{item.time}}</view>
        <view class="chat-row{{item.self ? ' chat-row--self' : ''}}">
          <view class="chat-avatar" aria-hidden="true">
            <view class="chat-default-avatar">
              <view class="chat-avatar-head" />
              <view class="chat-avatar-shoulders" />
            </view>
            <ww-open-data wx:if="{{item.identity}}" type="{{item.avatarType}}" openid="{{item.identity}}" class="chat-avatar-image" />
          </view>
          <view class="chat-content">
            <view class="chat-sender">
              <span class="chat-sender-name"><ww-open-data wx:if="{{item.identity}}"
                type="{{item.nameType}}" openid="{{item.identity}}" /></span>
              <span class="chat-sender-fallback">{{item.senderLabel}}</span>
              <span wx:if="{{item.senderBadge}}" class="chat-sender-badge chat-sender-badge--{{item.senderBadgeKind}}">{{item.senderBadge}}</span>
            </view>
            <view class="chat-bubble">
              <ww-suspense>
                <ww-open-message message-id="{{item.msgid}}" secret-key="{{item.secret_key}}"
                  display-type="normal" open-type="viewMessage" binderror="error" />
                <block wx:slot="fallback"><view class="chat-message-loading">正在加载消息…</view></block>
              </ww-suspense>
            </view>
          </view>
        </view>
      </view>
      <view class="chat-latest" aria-hidden="true" />
    </view>
  </scroll-view>
`

function chatStyle(el: HTMLElement) {
  // The iframe has its own document, so pass the resolved theme colors.
  const tokens = [
    'ink', 'panel', 'dim', 'line', 'tint-amber-bg', 'wecom-peer-bubble', 'wecom-own-bubble',
    'wecom-wechat-label', 'wecom-company-label',
  ]
  const theme = getComputedStyle(el)
  const colors: string[] = []
  for (const token of tokens) {
    const key = `--tt2-${token}`
    const value = theme.getPropertyValue(key).trim()
    if (value) colors.push(`${key}: ${value};`)
  }
  return `
    .chat-scroll {
      ${colors.join(' ')} --chat-separator-height: 30px; height: 100%; background: var(--tt2-panel, Canvas);
      color: var(--tt2-ink, CanvasText); font: 14px/1.5 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
    }
    .chat-message { position: relative; padding: 4px 14px; }
    .chat-message--match { background: var(--tt2-tint-amber-bg, Cornsilk); }
    .chat-feed--latest .chat-message { overflow-anchor: none; }
    .chat-viewport-anchor { position: absolute; left: 0; width: 1px; height: 1px; pointer-events: none; overflow-anchor: none; }
    .chat-viewport-anchor--without-time { margin-top: calc(-1 * var(--chat-separator-height)); }
    .chat-time-separator {
      height: var(--chat-separator-height); line-height: var(--chat-separator-height);
      text-align: center; color: var(--tt2-dim, GrayText); font-size: 11px; font-variant-numeric: tabular-nums;
    }
    .chat-latest { height: 6px; }
    .chat-row { display: flex; align-items: flex-start; gap: 8px; }
    .chat-row--self { flex-direction: row-reverse; }
    .chat-avatar { position: relative; flex: none; width: 28px; height: 28px; border-radius: 4px; overflow: hidden; }
    .chat-default-avatar {
      position: absolute; inset: 0; background: var(--tt2-line, ButtonFace);
      color: var(--tt2-dim, GrayText);
    }
    .chat-avatar-head, .chat-avatar-shoulders { position: absolute; background: currentColor; }
    .chat-avatar-head { top: 6px; left: 10px; width: 8px; height: 8px; border-radius: 50%; }
    .chat-avatar-shoulders { top: 15px; left: 6px; width: 16px; height: 8px; border-radius: 8px 8px 4px 4px; }
    .chat-avatar-image { position: relative; display: block; width: 100%; height: 100%; object-fit: cover; }
    .chat-content {
      display: flex; flex-direction: column; align-items: flex-start; min-width: 0; max-width: min(calc(100% - 36px), 580px);
    }
    .chat-row--self .chat-content { align-items: flex-end; }
    .chat-sender {
      display: flex; flex-wrap: wrap; align-items: baseline; gap: 0 4px; max-width: 100%;
      margin-bottom: 2px; color: var(--tt2-dim, GrayText); font-size: 11px; line-height: 14px; overflow-wrap: anywhere;
    }
    .chat-row--self .chat-sender { justify-content: flex-end; }
    .chat-sender-name:empty,
    .chat-sender-name:not(.chat-sender-name:empty) + .chat-sender-fallback { display: none; }
    .chat-sender-badge { white-space: nowrap; }
    .chat-sender-badge--wechat { color: var(--tt2-wecom-wechat-label, #15B628); }
    .chat-sender-badge--company { color: var(--tt2-wecom-company-label, #FF8214); }
    .chat-bubble {
      box-sizing: border-box; min-height: 28px; max-width: 100%; padding: 4px 8px;
      border-radius: 5px; background: var(--tt2-wecom-peer-bubble, ButtonFace); overflow-wrap: anywhere;
    }
    .chat-row--self .chat-bubble { background: var(--tt2-wecom-own-bubble, ButtonFace); }
    .chat-message-loading { min-width: 95px; color: var(--tt2-ink, CanvasText); }
  `
}

export async function archiveFrame(sdk: ArchiveSdk, orderNo: string | undefined, signature: Signature, el: HTMLElement,
  error: () => void, mounted: () => void, chatId: string, loadEarlier: () => void) {
  const signedUrl = location.href.split('#')[0]
  async function sign(url: string, kind: 'corp_signature' | 'agent_signature') {
    const next = url === signedUrl ? signature : await archiveSignature(orderNo, url)
    return { timestamp: next.timestamp, nonceStr: next.nonce, signature: next[kind] }
  }
  sdk.register({
    corpId: signature.corp_id, agentId: signature.agent_id,
    jsApiList: ['wwapp.invokeJsApiByCallInfo'],
    getConfigSignature: (url) => sign(url, 'corp_signature'),
    getAgentConfigSignature: (url) => sign(url, 'agent_signature'),
  })
  await new Promise<void>((resolve, reject) => {
    const timer = window.setTimeout(() => reject(new Error('企微组件初始化超时，请重新验证企微身份')), 12000)
    void sdk.initOpenData().then(() => resolve(), () => {
      reject(new Error('企微组件初始化失败，请重新验证企微身份'))
    }).finally(() => window.clearTimeout(timer))
  })
  let scrollTop = 0
  let focusedId: string | undefined
  let activeRender: AbortController | null = null
  let viewUpdated: (() => void) | undefined
  const viewReady = new Promise<void>((resolve) => { viewUpdated = resolve })
  const instance = sdk.createOpenDataFrameFactory().createOpenDataFrame({
    el, data: { messages: [], anchorOffset: 0, followLatest: false }, handleMounted: mounted,
    handleUpdated() {
      const committed = viewUpdated
      viewUpdated = undefined
      committed?.()
    },
    template: CHAT_TEMPLATE, style: chatStyle(el),
    methods: { error, loadEarlier, updateScroll: (event) => { scrollTop = Math.max(0, event.detail.scrollTop) } },
  })
  return {
    el: instance.el,
    dispose() {
      activeRender?.abort()
      instance.dispose()
    },
    async render(messages: ArchiveMessage[], position: ChatPosition) {
      const latest = position.kind === 'latest'
      if (position.kind === 'focus') focusedId = position.anchorId
      const controller = new AbortController()
      activeRender = controller
      const timeout = window.setTimeout(() => controller.abort(), 12000)
      const unavailable = () => new Error('企微组件未能定位聊天记录，请重试')
      async function updateFrame() {
        await viewReady
        if (controller.signal.aborted) return
        // Keep the pixel offset relative to the previous top message.
        const committed = new Promise<void>((resolve) => { viewUpdated = resolve })
        await Promise.all([committed, instance.setData({
          messages: messages.map((message, index) =>
            messageRow(message, chatId, messages[index - 1], position, focusedId)),
          anchorOffset: position.kind === 'preserve' ? scrollTop : 0,
          followLatest: latest,
        })])
        if (controller.signal.aborted) return
        const scroll = await sdk.createScrollViewContext?.(instance, 'chat')
        if (!scroll) throw unavailable()
        if (controller.signal.aborted) return
        scroll.scrollIntoView(latest ? '.chat-latest' : '.chat-viewport-anchor', { block: latest ? 'end' : 'start' })
      }
      const interrupted = new Promise<never>((_, reject) => {
        controller.signal.addEventListener('abort', () => reject(unavailable()), { once: true })
      })
      try {
        await Promise.race([updateFrame(), interrupted])
      } finally {
        window.clearTimeout(timeout)
        activeRender = null
      }
    },
  }
}
