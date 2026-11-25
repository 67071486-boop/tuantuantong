// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { ElSelect } from 'element-plus'
import { beforeEach, expect, it, vi } from 'vitest'

const listAccounts = vi.fn()
const fetchAccountRoles = vi.fn()
const fetchAccountOpeners = vi.fn()
vi.mock('@/api/accounts', async (original) => ({
  ...(await original<typeof import('@/api/accounts')>()),
  listAccounts: (...args: unknown[]) => listAccounts(...args),
  fetchAccountRoles: () => fetchAccountRoles(),
  fetchAccountOpeners: (...args: unknown[]) => fetchAccountOpeners(...args),
}))
vi.mock('@/api/config', async (original) => ({
  ...(await original<typeof import('@/api/config')>()),
  fetchAllGames: () => Promise.resolve([]),
}))
import V2UserList from '../V2UserList.vue'
import { useSessionStore } from '@/stores/session'

beforeEach(() => {
  vi.clearAllMocks()
  fetchAccountRoles.mockResolvedValue([])
  fetchAccountOpeners.mockResolvedValue({ items: [{ id: 17, name: '开户人甲', username: 'opener_a' }], total: 1, page: 1 })
  listAccounts.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 })
})

it('用户类型先显示全部，选择身份与开户人发送对应筛选', async () => {
  fetchAccountOpeners.mockResolvedValue({ items: [{ id: 17, name: '开户人甲', username: 'opener_a' }], total: 101, page: 1 })
  const pinia = createPinia()
  setActivePinia(pinia)
  const session = useSessionStore()
  session.user = { id: 1, username: 'admin', display_name: '管理员', identity_plane: 'tenant', is_tenant_admin: true, is_account_admin: true, roles: [], role: '管理员', permissions: [], menu_permissions: [], tenant_name: '测试租户' }
  session.status = 'ready'
  const wrapper = mount(V2UserList, { global: { plugins: [pinia] } })
  await flushPromises()
  const groups = wrapper.findAll('[data-v2-filter-group]')
  expect(groups[0]?.attributes('data-v2-filter-group')).toBe('identity')
  const identity = wrapper.findAllComponents(ElSelect).find((s) => s.attributes('data-v2-filter') === 'identity')!
  const opener = wrapper.findAllComponents(ElSelect).find((s) => s.attributes('data-v2-filter') === 'opener')!
  expect(identity.props('modelValue')).toBe('all')
  expect(wrapper.text()).toContain('加载更多开户人')
  expect(wrapper.text()).toContain('开户人甲（opener_a）')
  fetchAccountOpeners.mockResolvedValueOnce({ items: [{ id: 18, name: '开户人甲', username: 'opener_b' }], total: 2, page: 2 })
  await wrapper.get('button').trigger('focus')
  const more = wrapper.findAll('button').find((button) => button.text() === '加载更多开户人')!
  await more.trigger('click')
  await flushPromises()
  expect(fetchAccountOpeners).toHaveBeenLastCalledWith('', 2)
  expect(wrapper.text()).toContain('开户人甲（opener_b）')
  identity.vm.$emit('change', 'taker')
  await flushPromises()
  expect(listAccounts).toHaveBeenCalledWith(expect.objectContaining({ is_worker: true, is_studio: false, page: 1 }))
  opener.vm.$emit('update:modelValue', 17)
  await wrapper.get('[data-v2="search"]').trigger('click')
  await flushPromises()
  expect(listAccounts).toHaveBeenCalledWith(expect.objectContaining({ opened_by: 17, page: 1 }))

  type OpenerPage = { items: Array<{ id: number; name: string; username: string }>; total: number; page: number }
  let resolveOld!: (value: OpenerPage) => void
  let resolveNew!: (value: OpenerPage) => void
  fetchAccountOpeners.mockImplementationOnce(() => new Promise<OpenerPage>((resolve) => { resolveOld = resolve }))
  fetchAccountOpeners.mockImplementationOnce(() => new Promise<OpenerPage>((resolve) => { resolveNew = resolve }))
  const searchOpeners = opener.props('remoteMethod') as (query: string) => void
  searchOpeners('旧')
  searchOpeners('新')
  resolveNew({ items: [{ id: 31, name: '新开户人', username: 'new' }], total: 101, page: 1 })
  await flushPromises()
  resolveOld({ items: [{ id: 32, name: '旧开户人', username: 'old' }], total: 1, page: 1 })
  await flushPromises()
  expect(wrapper.text()).toContain('新开户人（new）')
  expect(wrapper.text()).not.toContain('旧开户人（old）')

  fetchAccountOpeners.mockRejectedValueOnce(new Error('网络中断'))
  await wrapper.findAll('button').find((button) => button.text() === '加载更多开户人')!.trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('新开户人（new）')
  expect(wrapper.text()).toContain('开户人加载失败，重试')
  fetchAccountOpeners.mockResolvedValueOnce({ items: [{ id: 33, name: '另一开户人', username: 'next' }], total: 2, page: 2 })
  await wrapper.findAll('button').find((button) => button.text() === '开户人加载失败，重试')!.trigger('click')
  await flushPromises()
  expect(fetchAccountOpeners).toHaveBeenLastCalledWith('新', 2)
  expect(wrapper.text()).toContain('另一开户人（next）')
  wrapper.unmount()
})
