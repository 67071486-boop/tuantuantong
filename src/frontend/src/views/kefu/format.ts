import type { OrderListItem } from '@/api/orders'

/** MM-DD HH:mm；解析失败原样返回。 */
export function shortTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 买家：旺旺优先、没有才用手机号（同旧列表口径）。 */
export function buyerText(
  row: Pick<OrderListItem, 'customer_ww' | 'customer_phone'>,
): string {
  return row.customer_ww || row.customer_phone || ''
}

/** 展示单号：来源号非空取它，否则内置号（D-0301）。 */
export function displayOrderNo(
  row: Pick<OrderListItem, 'source_order_no' | 'order_no'>,
): string {
  return row.source_order_no || row.order_no
}
