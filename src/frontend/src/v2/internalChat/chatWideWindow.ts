export function chatConversationId(value: unknown): number | undefined {
  const id = typeof value === 'string' && /^[1-9]\d*$/.test(value) ? Number(value) : value
  return typeof id === 'number' && Number.isSafeInteger(id) && id > 0 ? id : undefined
}

export function openChatWindow(href: string): boolean {
  const tab = window.open('about:blank', '_blank')
  if (!tab) return false
  tab.opener = null
  tab.location.replace(href)
  return true
}

type ChatWideWindow = Window & {
  ttOpenChatWide?: (source: Window, conversationId?: unknown) => boolean
}

export function bindChatWideWindow(getFrame: () => HTMLIFrameElement | undefined, open: (id?: number) => boolean) {
  const host = window as ChatWideWindow
  const handler = (source: Window, id?: unknown) => {
    if (source !== getFrame()?.contentWindow) return false
    return open(chatConversationId(id))
  }
  host.ttOpenChatWide = handler
  return () => { if (host.ttOpenChatWide === handler) delete host.ttOpenChatWide }
}
