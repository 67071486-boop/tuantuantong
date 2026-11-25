// @vitest-environment jsdom
import { defineComponent } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { WecomAftersalesSettings } from '@/api/wecomAftersales'
import V2WecomAftersales from '../V2WecomAftersales.vue'

const { load, save, options } = vi.hoisted(() => ({ load: vi.fn(), save: vi.fn(), options: vi.fn() }))
vi.mock('@/api/wecomAftersales', () => ({
  getWecomAftersalesSettings: load,
  saveWecomAftersalesSettings: save,
  listWecomAftersalesOptions: options,
}))

const Select = defineComponent({
  props: { remoteMethod: Function },
  emits: ['update:modelValue', 'visible-change'],
  template: '<div><slot /><slot name="footer" /></div>',
})
const Option = defineComponent({
  props: { label: String, value: [String, Number] },
  template: '<li role="option"><slot>{{ label }}</slot></li>',
})
const groupScopeSelector = 'select[aria-label="适用群"]'
const initial: WecomAftersalesSettings = {
  available: true,
  legacy_pilot: false,
  enabled: false,
  status: 'disabled',
  keywords: ['退款'],
  default_keywords: ['退款', '换打手', '不想打了'],
  group_ids: [],
  group_options: [],
  order_recipient: 'sales',
  fixed_recipient_ids: [],
  fixed_recipient_options: [],
}

async function mountPanel() {
  const wrapper = mount(V2WecomAftersales, { global: { stubs: { ElSelect: Select, ElOption: Option } } })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  load.mockReset().mockResolvedValue(initial)
  save.mockReset().mockImplementation((draft) => Promise.resolve({ ...initial, ...draft, status: 'pending' }))
  options.mockReset().mockResolvedValue({ items: [], has_more: false })
})

describe('售后纠纷通知', () => {
  it('默认关闭，启用时可直接保存全部客户群和恢复的默认词', async () => {
    const wrapper = await mountPanel()
    expect(wrapper.get('h3').text()).toBe('售后纠纷通知')
    const enabled = wrapper.get<HTMLInputElement>('input[type="checkbox"]')
    expect(enabled.element.checked).toBe(false)
    expect(wrapper.get<HTMLSelectElement>(groupScopeSelector).element.selectedOptions[0]?.text).toBe('全部客户群')
    expect(wrapper.text()).toContain('企微授权范围内，新建客户群自动纳入')
    expect(wrapper.find('[aria-label="选择客户群"]').exists()).toBe(false)
    await wrapper.get('[data-v2="aftersales-default-keywords"]').trigger('click')
    expect(wrapper.get('textarea').element.value).toBe('退款、换打手、不想打了')
    await enabled.setValue(true)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(save).toHaveBeenCalledExactlyOnceWith({
      enabled: true,
      keywords: ['退款', '换打手', '不想打了'],
      group_ids: [],
      order_recipient: 'sales',
      fixed_recipient_ids: [],
    })
    expect(options).not.toHaveBeenCalled()
  })

  it('旧规则明确显示仅测试群生效，保存后显示新规则的同步状态', async () => {
    load.mockResolvedValueOnce({
      ...initial, enabled: true, status: 'synced', legacy_pilot: true, keywords: initial.default_keywords,
    })
    const wrapper = await mountPanel()
    expect(wrapper.get('[role="status"]').text()).toBe('当前仅测试群生效')
    expect(wrapper.get<HTMLSelectElement>(groupScopeSelector).element.value).toBe('all')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(save).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ enabled: true, group_ids: [] }))
    expect(wrapper.get('[role="status"]').text()).toBe('等待同步')
  })

  it('保存关键词和群名单，岗位单选与固定通知人一起提交', async () => {
    const wrapper = await mountPanel()
    await wrapper.get(groupScopeSelector).setValue('selected')
    const [groups, role, users] = wrapper.findAllComponents(Select)
    groups!.vm.$emit('update:modelValue', ['synthetic-group-1', 'synthetic-group-2'])
    role!.vm.$emit('update:modelValue', 'publisher')
    users!.vm.$emit('update:modelValue', [8101, 8102])
    await wrapper.get('textarea').setValue('退款、换人；退款\n打不了了')
    await wrapper.get('input[type="checkbox"]').setValue(true)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(save).toHaveBeenCalledExactlyOnceWith({
      enabled: true,
      keywords: ['退款', '换人', '打不了了'],
      group_ids: ['synthetic-group-1', 'synthetic-group-2'],
      order_recipient: 'publisher',
      fixed_recipient_ids: [8101, 8102],
    })
    expect(wrapper.text()).toContain('等待同步')
  })

  it('切回全部仍可找回未保存的群选择，正式保存时不会提交旧名单', async () => {
    load.mockResolvedValueOnce({
      ...initial, enabled: true, status: 'synced', group_ids: ['synthetic-group-1'],
      group_options: [{ id: 'synthetic-group-1', name: '已选客户群' }],
    })
    const wrapper = await mountPanel()
    const scope = wrapper.get<HTMLSelectElement>(groupScopeSelector)
    expect(scope.element.value).toBe('selected')
    expect(wrapper.text()).toContain('已选客户群')
    await scope.setValue('all')
    expect(wrapper.find('[aria-label="选择客户群"]').exists()).toBe(false)
    await scope.setValue('selected')
    expect(wrapper.text()).toContain('已选客户群')
    await scope.setValue('all')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(save).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ enabled: true, group_ids: [] }))
  })

  it('启用指定范围时必须选群', async () => {
    const wrapper = await mountPanel()
    await wrapper.get(groupScopeSelector).setValue('selected')
    await wrapper.get('input[type="checkbox"]').setValue(true)
    await wrapper.get('form').trigger('submit')
    expect(save).not.toHaveBeenCalled()
    expect(wrapper.get('[role="alert"]').text()).toContain('至少选择一个客户群')
  })

  it('不开放的工作室不展示设置区，也不加载选项', async () => {
    load.mockResolvedValue({ ...initial, available: false })
    const wrapper = await mountPanel()
    expect(wrapper.text()).toBe('')
    expect(options).not.toHaveBeenCalled()
  })

  it('读取失败时可重试，确认未开放后隐藏区块', async () => {
    load.mockRejectedValueOnce(new Error('连接超时')).mockResolvedValue({ ...initial, available: false })
    const wrapper = await mountPanel()
    expect(wrapper.get('[role="alert"]').text()).toContain('连接超时')
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toBe('')
    expect(load).toHaveBeenCalledTimes(2)
  })

  it('打开和搜索触发相同查询时只请求一次，仍能加载后续选项', async () => {
    options.mockResolvedValueOnce({ items: [{ id: 'synthetic-group-1', name: '测试群一' }], has_more: true })
    const wrapper = await mountPanel()
    await wrapper.get(groupScopeSelector).setValue('selected')
    const groups = wrapper.findAllComponents(Select)[0]!
    const search = groups.props('remoteMethod') as (keyword: string) => void
    groups.vm.$emit('visible-change', true)
    search('')
    await flushPromises()
    search('  ')
    expect(options).toHaveBeenCalledExactlyOnceWith('group', '', 1)
    await groups.get('button').trigger('click')
    await flushPromises()
    search('测试')
    await flushPromises()
    expect(options.mock.calls).toEqual([['group', '', 1], ['group', '', 2], ['group', '测试', 1]])
  })

  it('用群号和账号区分下拉中的同名项，选中标签仍保留短名', async () => {
    options.mockResolvedValueOnce({ items: [
      { id: 'synthetic-group-1', name: '售后群', detail: 'synthetic-group-1' },
      { id: 'synthetic-group-2', name: '售后群', detail: 'synthetic-group-2' },
    ], has_more: false }).mockResolvedValueOnce({ items: [
      { id: 8101, name: '售后客服', detail: 'synthetic-service-a' },
      { id: 8102, name: '售后客服', detail: 'synthetic-service-b' },
    ], has_more: false })
    const wrapper = await mountPanel()
    await wrapper.get(groupScopeSelector).setValue('selected')
    const [groups, , users] = wrapper.findAllComponents(Select)
    groups!.vm.$emit('visible-change', true)
    users!.vm.$emit('visible-change', true)
    await flushPromises()
    const groupOptions = groups!.findAllComponents(Option)
    const userOptions = users!.findAllComponents(Option)
    expect(groupOptions.map((item) => item.props('label'))).toEqual(['售后群', '售后群'])
    expect(userOptions.map((item) => item.props('label'))).toEqual(['售后客服', '售后客服'])
    expect(groupOptions.map((item) => item.text())).toEqual(['售后群synthetic-group-1', '售后群synthetic-group-2'])
    expect(userOptions.map((item) => item.text())).toEqual(['售后客服synthetic-service-a', '售后客服synthetic-service-b'])
  })
})
