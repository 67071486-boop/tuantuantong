import { ORDER_STATUS_VALUES, type OrderStatus } from '@/config/orderStatus'

const statusClasses: Record<OrderStatus, string> = {
  UNASSIGNED: 'cv2-st--unassigned',
  WAITING_GRAB: 'cv2-st--pending',
  IN_PROGRESS: 'cv2-st--doing',
  PENDING_REVIEW: 'cv2-st--accept',
  COMPLETED: 'cv2-st--done',
  ISSUE: 'cv2-st--error',
  PAUSED: 'cv2-st--paused',
  REFUNDED: 'cv2-st--refund',
}

export function orderStatusClass(status: string): string {
  return (ORDER_STATUS_VALUES as readonly string[]).includes(status) ? statusClasses[status as OrderStatus] : statusClasses.UNASSIGNED
}
