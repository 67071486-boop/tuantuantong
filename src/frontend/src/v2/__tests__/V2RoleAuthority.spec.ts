// @vitest-environment jsdom
import { defineComponent } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  listRoleAuthorities: vi.fn(), getRoleAuthority: vi.fn(), fetchPermissionTree: vi.fn(),
  updateRoleAuthority: vi.fn(), createRoleAuthority: vi.fn(), copyRoleAuthority: vi.fn(),
  deleteRoleAuthority: vi.fn(), fetchRolePermissionRows: vi.fn(),
}))
vi.mock('@/api/roleAuthority', () => api)
import V2RoleAuthority from '../V2RoleAuthority.vue'
import { useSessionStore } from '@/stores/session'

const dialog = defineComponent({ props: ['modelValue'], template: '<div v-if="modelValue"><slot /></div>' })
const input = defineComponent({ props: ['modelValue'], emits: ['update:modelValue'], template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />' })
const tree = defineComponent({
  methods: { setCheckedKeys() {}, getCheckedKeys() { return ['order.list'] } },
  template: '<div />',
})

async function mountPage() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const session = useSessionStore()
  session.user = {
    id: 1, username: 'test', display_name: '测试', identity_plane: 'tenant',
    is_tenant_admin: false, is_account_admin: false, roles: [], role: '', permissions: [],
    menu_permissions: ['system.role_authority_edit'], tenant_name: '测试租户',
  }
  session.status = 'ready'
  const wrapper = mount(V2RoleAuthority, { global: { plugins: [pinia], stubs: { ElDialog: dialog, ElInput: input, ElTree: tree, ElSwitch: true } } })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.resetAllMocks()
  api.listRoleAuthorities.mockResolvedValue([{ id: 3, name: '客服', remark: '', can_be_sales_agent: false }])
  api.fetchPermissionTree.mockResolvedValue([{ code: 'order', name: '订单', children: [] }])
  api.getRoleAuthority.mockResolvedValue({ id: 3, name: '客服', remark: '', can_be_sales_agent: false, permission_codes: ['order.list'], dashboard_type: 'worker' })
  api.updateRoleAuthority.mockResolvedValue({})
  api.createRoleAuthority.mockResolvedValue({})
  api.copyRoleAuthority.mockResolvedValue({})
})

describe('角色首页看板设置', () => {
  it('编辑时回填并与权限一起保存，允许恢复默认首页', async () => {
    const wrapper = await mountPage()
    await wrapper.get('[data-v2-edit-btn="3"]').trigger('click')
    await flushPromises()
    const select = wrapper.get<HTMLSelectElement>('[data-v2="dashboard-type"]')
    expect(select.element.value).toBe('worker')
    expect(wrapper.text()).toContain('看板仅决定首页布局，数据权限仍以角色权限为准')
    await select.setValue('')
    await wrapper.get('[data-v2="role-save"]').trigger('click')
    await flushPromises()
    expect(api.updateRoleAuthority).toHaveBeenCalledWith(3, {
      name: '客服', remark: '', can_be_sales_agent: false, permission_codes: ['order.list'], dashboard_type: '',
    })
  })

  it('新角色默认不选，可选择工作看板并随权限保存', async () => {
    const wrapper = await mountPage()
    await wrapper.get('[data-v2="add"]').trigger('click')
    await flushPromises()
    const select = wrapper.get<HTMLSelectElement>('[data-v2="dashboard-type"]')
    expect(select.element.value).toBe('')
    await wrapper.get('[data-v2="name-input"]').setValue('运营')
    await select.setValue('work')
    await wrapper.get('[data-v2="role-save"]').trigger('click')
    await flushPromises()
    expect(api.createRoleAuthority).toHaveBeenCalledWith({
      name: '运营', remark: '', can_be_sales_agent: false, permission_codes: ['order.list'], dashboard_type: 'work',
    })
  })

  it('复制角色只提交名称和说明，由后端复制源角色的看板', async () => {
    const wrapper = await mountPage()
    await wrapper.get('[data-v2-copy-btn="3"]').trigger('click')
    await wrapper.get('[data-v2="copy-name-input"]').setValue('客服二')
    await wrapper.get('[data-v2="copy-submit"]').trigger('click')
    await flushPromises()
    expect(api.copyRoleAuthority).toHaveBeenCalledWith(3, { name: '客服二', remark: '' })
  })

  it('保存失败保留选择供重试', async () => {
    api.updateRoleAuthority.mockRejectedValue(new Error('保存失败'))
    const wrapper = await mountPage()
    await wrapper.get('[data-v2-edit-btn="3"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-v2="dashboard-type"]').setValue('work')
    await wrapper.get('[data-v2="role-save"]').trigger('click')
    await flushPromises()
    expect(wrapper.get<HTMLSelectElement>('[data-v2="dashboard-type"]').element.value).toBe('work')
    expect(wrapper.text()).toContain('保存失败')
  })
})
