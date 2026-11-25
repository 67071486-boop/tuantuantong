import { request } from './http'
import type { AssigneeOption } from './orders'

const url = '/api/business/orders/assignee-favorites/'

export function fetchFavoriteAssignees(): Promise<AssigneeOption[]> {
  return request({ method: 'get', url })
}

export function setFavoriteAssignee(userId: number, favorite: boolean): Promise<AssigneeOption[]> {
  return request({ method: 'put', url, data: { user_id: userId, favorite } })
}
