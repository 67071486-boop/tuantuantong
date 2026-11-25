import type { InjectionKey } from 'vue'

export const internalChatOrderReturn: InjectionKey<() => boolean> = Symbol('internalChatOrderReturn')
