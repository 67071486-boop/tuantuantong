// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { ElSelect } from 'element-plus'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  createOrderSourceGroup,
  fetchAllOrderSourceGroups,
  fetchAllOrderSources,
  fetchOrderSources,
  updateOrderSource,
} from '@/api/orderSources'
import { fetchXgjShopsList } from '@/api/xgjBindings'
import V2OrderSources from '../V2OrderSources.vue'

vi.mock('@/api/orderSources', () => ({
  createOrderSource: vi.fn(),
  createOrderSourceGroup: vi.fn(),
  fetchAllOrderSourceGroups: vi.fn(),
  fetchAllOrderSources: vi.fn(),
  fetchOrderSources: vi.fn(),
  removeOrderSource: vi.fn(),
  removeOrderSourceGroup: vi.fn(),
  updateOrderSource: vi.fn(),
  updateOrderSourceGroup: vi.fn(),
}))
vi.mock('@/api/xgjBindings', () => ({ fetchXgjShopsList: vi.fn() }))

const dates = { created_at: '', updated_at: '' }
const firstGroup = { id: 11, name: '原分组', order_by: 0, ...dates }
const secondGroup = { id: 12, name: '新分组', order_by: 0, ...dates }
const source = {
  id: 31,
  name: '原来源',
  remark: '',
  order_by: 0,
  shop_type: 0,
  group: firstGroup.id,
  group_name: firstGroup.name,
  groups: [{ id: firstGroup.id, name: firstGroup.name }],
  ...dates,
}

function page<T>(items: T[]) {
  return { items, total: items.length, page: 1, page_size: 200 }
}

let wrapper: ReturnType<typeof mount<typeof V2OrderSources>>

function selectFor(target: string) {
  const select = wrapper.findAllComponents(ElSelect)
    .find((item) => item.attributes('data-v2') === target)
  if (!select) throw new Error(`找不到选择框 ${target}`)
  return select
}

beforeEach(async () => {
  vi.clearAllMocks()
  vi.mocked(fetchOrderSources).mockResolvedValue(page([source]))
  vi.mocked(fetchAllOrderSourceGroups).mockResolvedValue([firstGroup, secondGroup])
  vi.mocked(fetchAllOrderSources).mockResolvedValue([source, { ...source, id: 32, name: '后页来源' }])
  vi.mocked(fetchXgjShopsList).mockResolvedValue(page([]))
  vi.mocked(updateOrderSource).mockResolvedValue(source)
  vi.mocked(createOrderSourceGroup).mockResolvedValue(secondGroup)
  wrapper = mount(V2OrderSources, { attachTo: document.body })
  await flushPromises()
})

afterEach(() => wrapper.unmount())

describe('v2 订单来源多分组', () => {
  it('打开编辑时保留原分组，选择多个分组后一并提交', async () => {
    await wrapper.get('[data-v2-edit="31"]').trigger('click')
    await flushPromises()

    const select = selectFor('soure-group')
    expect(select.props('modelValue')).toEqual([11])
    select.vm.$emit('update:modelValue', [11, 12])
    await wrapper.get('[data-v2="soure-save"]').trigger('click')

    expect(updateOrderSource).toHaveBeenCalledWith(31, expect.objectContaining({ group_ids: [11, 12] }))
  })

  it('只保存来源资料时不覆盖已有分组', async () => {
    await wrapper.get('[data-v2-edit="31"]').trigger('click')
    await flushPromises()
    await wrapper.get('input[data-v2="soure-remark"]').setValue('修改说明')
    await wrapper.get('[data-v2="soure-save"]').trigger('click')

    expect(updateOrderSource).toHaveBeenCalledWith(31, expect.objectContaining({
      remark: '修改说明', group_ids: undefined,
    }))
  })

  it('新增分组时可以勾选现有来源并一次提交', async () => {
    await wrapper.get('[data-v2="groups"]').trigger('click')
    await flushPromises()
    expect(fetchAllOrderSources).toHaveBeenCalledOnce()
    const select = selectFor('group-sources')
    select.vm.$emit('update:modelValue', [31, 32])
    await wrapper.get('input[data-v2="group-name"]').setValue('测试分组')
    await wrapper.get('[data-v2="group-add"]').trigger('click')

    expect(createOrderSourceGroup).toHaveBeenCalledWith({
      name: '测试分组', order_by: 0, source_ids: [31, 32],
    })
  })
})
