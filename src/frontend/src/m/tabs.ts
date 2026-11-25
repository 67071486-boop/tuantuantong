type CanNode = (codes: readonly string[]) => boolean

export const MINE_PERMISSIONS = ['my_orders', 'my_performance']
export const MINE_PERFORMANCE_HASH = '#performance'
export const HALL_SUBSCRIPTION_HASH = '#hall-subscription'

export function canReadWorkerOrders(canNode: CanNode): boolean {
  return canNode(['my_orders']) && canNode(['my_orders.send_to_my_system'])
}

export function canReadWorkerPerformance(canNode: CanNode): boolean {
  return canNode(['my_performance']) && canNode(['my_performance.export', 'order.list'])
}

export function canEnterMine(canNode: CanNode): boolean {
  return canReadWorkerOrders(canNode) || canReadWorkerPerformance(canNode)
}

export type MTabDef = {
  routeName: string
  label: string
  permissions: readonly string[]
}

export const M_TABS: readonly MTabDef[] = [
  { routeName: 'm-hall', label: '接单大厅', permissions: ['grab_hall.take_order'] },
  { routeName: 'm-mine', label: '我的订单', permissions: MINE_PERMISSIONS },
  { routeName: 'm-channel', label: '频道', permissions: [] },
  { routeName: 'm-me', label: '我的', permissions: ['my_profile'] },
]

export function permittedTabs(canNode: CanNode): readonly MTabDef[] {
  return M_TABS.filter(({ routeName, permissions }) => (
    routeName === 'm-mine' ? canEnterMine(canNode) : canNode(permissions)
  ))
}

// 已有业务页继续作为默认入口，频道只为没有业务入口的打手兜底。
export function firstPermittedTab(canNode: CanNode): MTabDef | null {
  const tabs = permittedTabs(canNode)
  return tabs.find(({ permissions }) => permissions.length > 0) ?? tabs[0] ?? null
}
