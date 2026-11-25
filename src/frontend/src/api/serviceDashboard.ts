import { request } from './http'

export type ServiceDashboardOrder = {
  order_no: string
  source_order_no: string
  status: string
  game_name: string
  project_text: string
  updated_at: string
}

export type ServiceDashboardData = {
  status_counts: Record<string, number>
  today_created: number
  today_completed: number
  today_followed: number
  attention_orders: ServiceDashboardOrder[]
}

export function fetchServiceDashboard(): Promise<ServiceDashboardData> {
  return request<ServiceDashboardData>({ method: 'get', url: '/api/business/dashboard/service/' })
}
