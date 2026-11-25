import { chatRequest as request } from './chatTransport'

const settingsUrl = '/api/internal-chat/settings/'
const systemNotificationSettingsUrl = '/api/internal-chat/system-notification-settings/'

export interface ChatDirectoryChoice {
  primary_contact_id: number | null
  backup_contact_ids: number[]
}

export interface InternalChatSettings extends ChatDirectoryChoice {
  candidates: { id: number; name: string }[]
}

export function getInternalChatSettings() {
  return request<InternalChatSettings>({ url: settingsUrl, method: 'GET' })
}

export function saveInternalChatSettings(data: ChatDirectoryChoice) {
  return request<InternalChatSettings>({ url: settingsUrl, method: 'PUT', data })
}

export interface SystemNotificationRules {
  approval_submitted: boolean
  approval_approved: boolean
  wecom_aftersales: boolean
  order_grab_qr: boolean
}

export interface SystemNotificationSettings extends SystemNotificationRules {
  available?: boolean
}

export function getSystemNotificationSettings() {
  return request<SystemNotificationSettings>({ url: systemNotificationSettingsUrl, method: 'GET' })
}

export function saveSystemNotificationSettings(data: Partial<SystemNotificationRules>) {
  return request<SystemNotificationSettings>({ url: systemNotificationSettingsUrl, method: 'PUT', data })
}
