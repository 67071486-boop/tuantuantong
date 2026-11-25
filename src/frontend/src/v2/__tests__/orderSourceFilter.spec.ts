import { expect, it } from 'vitest'

import { filterSourcesByGroup, sourceInGroup } from '../orderSourceFilter'

const sources = [
  { id: 1, name: '淘宝', group: 7, groups: [] },
  { id: 2, name: '闲鱼', group: null, groups: [{ id: 7 }] },
  { id: 3, name: '其它', group: null, groups: [] },
  { id: 4, name: '双组', group: 8, groups: [{ id: 7 }] },
]

it('按分组筛选时同时认单选归属与多选归属', () => {
  expect(filterSourcesByGroup(sources, 7).map((source) => source.id)).toEqual([1, 2, 4])
  expect(filterSourcesByGroup(sources, 8).map((source) => source.id)).toEqual([4])
})

it('未选分组时返回全部来源，未分组来源不落入任何组', () => {
  expect(filterSourcesByGroup(sources, null)).toEqual(sources)
  expect(sourceInGroup(sources[2]!, 7)).toBe(false)
})
