export function orderChatPreviewEnabled(): boolean {
  return import.meta.env.DEV
    && import.meta.env.VITE_INTERNAL_CHAT_PREVIEW === '1'
    && import.meta.env.VITE_INTERNAL_CHAT_LIVE === '1'
}
