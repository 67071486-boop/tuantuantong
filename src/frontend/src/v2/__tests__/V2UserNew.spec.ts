// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { ElDialog } from 'element-plus'
import { createMemoryHistory, createRouter } from 'vue-router'
import { expect, it, vi } from 'vitest'
import { listSelfRegInvites, revokeSelfRegInvite, type SelfRegInviteRow } from '@/api/selfRegistration'
import V2UserNew from '../V2UserNew.vue'

vi.mock('@/api/config', () => ({ fetchAllGames: vi.fn().mockResolvedValue([]) }))
vi.mock('@/api/accounts', () => ({ fetchAccountRoles: vi.fn().mockResolvedValue([]) }))
vi.mock('@/api/selfRegistration', () => ({
  fetchSelfRegConfig: vi.fn().mockResolvedValue({ game_ids: [], forbid_orders_default: false }),
  listSelfRegInvites: vi.fn(),
  revokeSelfRegInvite: vi.fn().mockResolvedValue(undefined),
}))

const invite: SelfRegInviteRow = {
  id: 1,
  created_at: '2026-11-14T10:00:00+08:00',
  created_by_name: '测试账号',
  status: 'unused',
  expires_at: '2026-11-21T10:00:00+08:00',
  consumed_at: null,
  consumed_by_name: null,
  chosen_game_names: [],
}

it('同一弹窗保持第二步，确认后作废并刷新台账，取消不发请求', async () => {
  vi.mocked(revokeSelfRegInvite).mockClear()
  vi.mocked(listSelfRegInvites).mockResolvedValue([invite])
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/v2/user-new', component: V2UserNew }],
  })
  await router.push('/v2/user-new')
  const wrapper = mount(V2UserNew, { attachTo: document.body, global: { plugins: [router] } })
  try {
    await flushPromises()
    await wrapper.get('[data-v2-tab="selfreg"]').trigger('click')
    const dialog = () => wrapper.findAllComponents(ElDialog).filter((dlg) => dlg.props('modelValue'))
    const open = () => wrapper.get('[data-v2-revoke="1"]').trigger('click')
    const firstConfirm = () => wrapper.get('[data-v2="revoke-first"]').trigger('click')

    await open()
    const first = dialog()[0]!.vm
    await firstConfirm()
    expect(dialog().map(({ vm }) => vm)).toEqual([first])
    expect(revokeSelfRegInvite).not.toHaveBeenCalled()
    await wrapper.get('[data-v2="revoke-second"]').element.closest('div')!.querySelector('button')!.click()
    await flushPromises()
    expect(dialog()).toHaveLength(0)
    expect(revokeSelfRegInvite).not.toHaveBeenCalled()

    await open()
    await firstConfirm()
    vi.mocked(listSelfRegInvites).mockResolvedValue([{ ...invite, status: 'expired' }])
    await wrapper.get('[data-v2="revoke-second"]').trigger('click')
    await flushPromises()
    expect(revokeSelfRegInvite).toHaveBeenCalledExactlyOnceWith(1)
    expect(wrapper.find('[data-v2-revoke="1"]').exists()).toBe(false)
    expect(wrapper.get('[data-v2-invite="1"]').text()).toContain('已过期')
  } finally {
    wrapper.unmount()
  }
})
