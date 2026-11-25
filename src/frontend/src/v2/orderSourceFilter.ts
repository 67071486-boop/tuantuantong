/** 订单来源分组筛选的纯函数：V2 订单管理与绩效页共用同一判据。 */
export type GroupedSource = {
  group?: number | null
  groups?: readonly { id: number }[] | null
}

export function sourceInGroup(source: GroupedSource, groupId: number): boolean {
  return source.group === groupId || (source.groups ?? []).some((group) => group.id === groupId)
}

export function filterSourcesByGroup<T extends GroupedSource>(sources: readonly T[], groupId: number | null): T[] {
  if (groupId === null) return [...sources]
  return sources.filter((source) => sourceInGroup(source, groupId))
}
