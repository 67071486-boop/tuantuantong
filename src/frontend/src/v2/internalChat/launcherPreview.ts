import type { InjectionKey, Ref } from 'vue'

export const openInternalChatPreview: InjectionKey<(conversationId?: number) => void> = Symbol('openInternalChatPreview')

export const internalChatProduction: InjectionKey<Readonly<Ref<boolean>>> = Symbol('internalChatProduction')
