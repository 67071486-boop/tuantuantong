// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import V2WecomGroupPane from '../V2WecomGroupPane.vue'
import V2WecomArchivePane from '../V2WecomArchivePane.vue'
import V2WecomArchiveSearch from '../V2WecomArchiveSearch.vue'

const { archiveAccess, archiveGroups, archiveRememberGroup, archiveSearch, archiveMessages, archiveSignature, archiveLogin, loadWecomArchiveSdk, archiveFrame, request } =
  vi.hoisted(() => ({
    archiveAccess: vi.fn(), archiveGroups: vi.fn(), archiveRememberGroup: vi.fn(), archiveSearch: vi.fn(), archiveMessages: vi.fn(), archiveSignature: vi.fn(), archiveLogin: vi.fn(),
    loadWecomArchiveSdk: vi.fn(), archiveFrame: vi.fn(), request: vi.fn(),
  }))
vi.mock('../wecomArchive', () => ({
  archiveAccess, archiveGroups, archiveRememberGroup, archiveSearch, archiveMessages, archiveSignature, archiveLogin, loadWecomArchiveSdk, archiveFrame,
}))
vi.mock('@/api/http', () => ({ request }))

const ORDER = '20261114000000000001'
const candidate = { chat_id: 'group', name: '合成原单群', match: 'message', proof: 'signed' }
const latest = { msgid: 'later', secret_key: 'latest-key', sent_at: '2026-11-14T00:15:00Z' }
const earliest = { msgid: 'earlier', secret_key: 'older-key', sent_at: '2026-11-14T00:10:00Z' }

describe('订单企微聊天记录', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    request.mockResolvedValue({ groups: [], archive_available: true })
    archiveSignature.mockResolvedValue({ verified: true })
    loadWecomArchiveSdk.mockResolvedValue({})
  })

  it('会话存档未开通时没有聊天记录入口', async () => {
    request.mockResolvedValue({ groups: [] })
    archiveGroups.mockResolvedValue({ enabled: false, groups: [], complete: true })
    const wrapper = mount(V2WecomGroupPane, { props: { orderNo: ORDER } })
    await flushPromises()
    expect(wrapper.find('.wecom-chat').exists()).toBe(false)
    expect(loadWecomArchiveSdk).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('群资料仍在加载或读取失败时，订单详情也会打开已匹配的聊天', async () => {
    let rejectDetails!: (error: Error) => void
    request.mockReturnValueOnce(new Promise<never>((_, reject) => { rejectDetails = reject }))
    archiveGroups.mockResolvedValue({ enabled: true, groups: [candidate], complete: true })
    archiveMessages.mockResolvedValue({ messages: [latest], next_before: null, requires_login: false })
    const render = vi.fn()
    archiveFrame.mockImplementation((_sdk, _order, _sig, mountPoint: HTMLElement) => {
      const el = document.createElement('iframe')
      mountPoint.appendChild(el)
      return Promise.resolve({ el, render, dispose: vi.fn() })
    })
    const wrapper = mount(V2WecomGroupPane, { props: { orderNo: ORDER } })
    await flushPromises()

    expect(wrapper.get('.wecom-details').text()).toContain('正在加载')
    expect(wrapper.find('.wecom-chat iframe').exists()).toBe(true)
    expect(render).toHaveBeenCalledExactlyOnceWith([latest], { kind: 'latest' })

    rejectDetails(new Error('群资料请求超时'))
    await flushPromises()
    expect(wrapper.get('.wecom-details').text()).toContain('加载失败')
    expect(wrapper.find('.wecom-chat iframe').exists()).toBe(true)
    wrapper.unmount()
  })

  it('搜索结果不完整时也直接打开已有凭证的单个命中群，补入旧消息时保留阅读位置', async () => {
    const render = vi.fn().mockResolvedValue(undefined)
    const dispose = vi.fn()
    archiveFrame.mockImplementation((_sdk, _order, _sig, mountPoint: HTMLElement) => {
      const el = document.createElement('iframe')
      mountPoint.appendChild(el)
      return Promise.resolve({ el, render, dispose })
    })
    const matched = { ...candidate, anchor_msgid: earliest.msgid }
    request.mockResolvedValueOnce({
      groups: [{ chat_id: candidate.chat_id, name: candidate.name, owner: 'staff-raw-id', members: [
        { name: '客服昵称', userid: 'staff-raw-id' }, { userid: 'member-raw-id' },
      ] }],
      archive_available: true,
    }).mockResolvedValueOnce({ status: 'ready', qr_url: 'https://example.com/group.png' })
    archiveGroups.mockResolvedValue({ enabled: true, groups: [matched], complete: false })
    archiveMessages.mockResolvedValueOnce({ messages: [latest], next_before: 5, requires_login: false })
      .mockResolvedValueOnce({ messages: [earliest], next_before: null, requires_login: false })
    const wrapper = mount(V2WecomGroupPane, { props: { orderNo: ORDER } })
    await flushPromises()

    const details = wrapper.get('.wecom-details')
    const chat = wrapper.get('.wecom-chat')
    expect(details.get('[data-v2="group-qr"] img').attributes('src')).toBe('https://example.com/group.png')
    expect(details.text()).toContain('群主：客服昵称')
    expect(details.text()).toContain('未获取昵称')
    expect(details.text()).not.toMatch(/staff-raw-id|member-raw-id/u)
    expect(details.get('article').classes()).toContain('wecom-group--selected')
    expect(details.element.nextElementSibling).toBe(chat.element)
    expect(chat.find('iframe[title="企微群聊聊天记录"]').exists()).toBe(true)
    expect(archiveGroups).toHaveBeenCalledOnce()
    expect(archiveMessages).toHaveBeenCalledWith(ORDER, matched, null)
    expect(render).toHaveBeenLastCalledWith([latest], { kind: 'latest' })

    await wrapper.findAll('button').find((button) => button.text() === '加载更早的消息')?.trigger('click')
    await flushPromises()
    expect(archiveMessages).toHaveBeenLastCalledWith(ORDER, matched, 5)
    expect(render).toHaveBeenLastCalledWith([earliest, latest], { kind: 'preserve', anchorId: latest.msgid })
    expect(wrapper.text()).not.toContain(latest.secret_key)
    await wrapper.findAll('button').find((button) => button.text() === '收起聊天记录')?.trigger('click')
    expect(dispose).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it('订单命中群聊后直接读消息，后台记住群的失败不影响查看', async () => {
    const group = { ...candidate, remember: true }
    archiveGroups.mockResolvedValueOnce({ enabled: true, groups: [group], complete: true })
    archiveRememberGroup.mockRejectedValueOnce(new Error('连接暂时中断，请重试'))
    const render = vi.fn()
    archiveFrame.mockResolvedValue({ el: document.createElement('iframe'), render, dispose: vi.fn() })
    archiveMessages.mockResolvedValue({ messages: [latest], next_before: null, requires_login: false })
    const wrapper = mount(V2WecomArchivePane, { props: { orderNo: ORDER } })
    await flushPromises()

    expect(wrapper.text()).toContain(candidate.name)
    expect(wrapper.text()).toContain('首次查看需要登录企业微信')
    expect(wrapper.text()).not.toContain('确认关联并查看')
    expect(archiveMessages).toHaveBeenCalledExactlyOnceWith(ORDER, group, null)
    expect(archiveRememberGroup).toHaveBeenCalledExactlyOnceWith(ORDER, group)
    expect(render).toHaveBeenCalledExactlyOnceWith([latest], { kind: 'latest' })
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('未获得群聊凭证时可稍后重查', async () => {
    const pending = { ...candidate, proof: '' }
    archiveGroups.mockResolvedValue({ enabled: true, groups: [pending], complete: false })
    const wrapper = mount(V2WecomArchivePane, { props: { orderNo: ORDER } })
    await flushPromises()

    expect(wrapper.text()).toContain('搜索结果可能不完整，可以稍后再查')
    expect(archiveRememberGroup).not.toHaveBeenCalled()
    expect(archiveMessages).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('有多个命中时，只需选择想看的群即可打开聊天记录', async () => {
    const second = { ...candidate, chat_id: 'other', name: '另一群', proof: 'signed-other' }
    archiveGroups.mockResolvedValue({ enabled: true, groups: [candidate, second], complete: true })
    archiveFrame.mockResolvedValue({ el: document.createElement('iframe'), render: vi.fn(), dispose: vi.fn() })
    archiveMessages.mockResolvedValue({ messages: [latest], next_before: null, requires_login: false })
    const wrapper = mount(V2WecomArchivePane, { props: { orderNo: ORDER } })
    await flushPromises()
    expect(archiveMessages).not.toHaveBeenCalled()
    await wrapper.findAll('button').find((button) => button.text().includes(second.name))?.trigger('click')
    await flushPromises()
    expect(archiveMessages).toHaveBeenCalledExactlyOnceWith(ORDER, second, null)
    expect(wrapper.text()).not.toContain('确认关联并查看')
    expect(archiveRememberGroup).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('多群等待选择，只标记与所选聊天对应的群资料，聊天命中无群码时不误标', async () => {
    const anotherGroup = { ...candidate, name: '另一企微群', chat_id: 'another-group' }
    request.mockResolvedValueOnce({
      groups: [{ chat_id: candidate.chat_id, name: candidate.name, owner: '', members: [] }], archive_available: true,
    }).mockResolvedValueOnce({ status: 'ready', qr_url: 'https://example.com/group.png' })
    archiveGroups.mockResolvedValue({ enabled: true, groups: [candidate, anotherGroup], complete: true })
    archiveMessages.mockResolvedValue({ messages: [], next_before: null, requires_login: false })
    archiveFrame.mockResolvedValue({ el: document.createElement('iframe'), dispose: vi.fn() })
    const wrapper = mount(V2WecomGroupPane, { props: { orderNo: ORDER } })
    await flushPromises()
    expect(wrapper.text()).toContain('请选择要查看的群聊')
    expect(archiveMessages).not.toHaveBeenCalled()
    await wrapper.findAll('button').find((button) => button.text().includes(anotherGroup.name))?.trigger('click')
    await flushPromises()
    expect(archiveMessages).toHaveBeenCalledExactlyOnceWith(ORDER, anotherGroup, null)
    expect(wrapper.find('.wecom-group--selected').exists()).toBe(false)
    expect(wrapper.get('.wecom-group-context').text()).toBe('当前聊天暂无对应的群资料和二维码。')
    expect(wrapper.find('[data-v2="group-qr"] img').exists()).toBe(false)
    await wrapper.get('.wecom-chat [aria-pressed="false"]').trigger('click')
    await flushPromises()
    expect(archiveMessages).toHaveBeenLastCalledWith(ORDER, candidate, null)
    expect(wrapper.get('.wecom-group--selected').text()).toContain(candidate.name)
    expect(wrapper.find('.wecom-group-context').exists()).toBe(false)
    expect(wrapper.get('.wecom-group--selected [data-v2="group-qr"] img').attributes('src')).toBe('https://example.com/group.png')
    wrapper.unmount()
  })

  it('先用企微登录组件校验账号，再向后端取消息', async () => {
    const group = { ...candidate, match: 'name' }
    const unmount = vi.fn()
    const createWWLoginPanel = vi.fn().mockReturnValue({ unmount })
    loadWecomArchiveSdk.mockResolvedValue({ createWWLoginPanel })
    archiveSignature.mockResolvedValue({ verified: false, corp_id: 'corp', agent_id: 23, login_state: 'state' })
    archiveGroups.mockResolvedValue({ enabled: true, groups: [group], complete: true })
    archiveMessages.mockResolvedValue({ messages: [], next_before: null, requires_login: false })
    archiveLogin.mockResolvedValue({ verified: true })
    archiveFrame.mockResolvedValue({ el: document.createElement('iframe'), dispose: vi.fn() })
    const wrapper = mount(V2WecomGroupPane, { props: { orderNo: ORDER } })
    await flushPromises()
    expect(archiveMessages).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('暂无已同步的聊天记录')
    expect(wrapper.text()).toContain('首次查看需要登录企业微信')
    const options = createWWLoginPanel.mock.calls[0]?.[0]
    options.onLoginSuccess({ code: 'authorization-code' })
    await flushPromises()
    expect(unmount).toHaveBeenCalledOnce()
    expect(archiveLogin).toHaveBeenCalledWith(ORDER, 'authorization-code', 'state')
    expect(archiveMessages).toHaveBeenCalledWith(ORDER, group, null)
    wrapper.unmount()
  })

  it('需先登录时，直接打开登录后匹配到的群聊', async () => {
    const createWWLoginPanel = vi.fn().mockReturnValue({ unmount: vi.fn() })
    loadWecomArchiveSdk.mockResolvedValue({ createWWLoginPanel })
    archiveSignature.mockResolvedValueOnce({ verified: false, corp_id: 'corp', agent_id: 23, login_state: 'state' })
      .mockResolvedValueOnce({ verified: true })
    const pending = { ...candidate, requires_confirmation: true }
    archiveGroups.mockResolvedValueOnce({ enabled: true, groups: [], complete: false, requires_login: true })
      .mockResolvedValueOnce({ enabled: true, groups: [pending], complete: true })
    archiveLogin.mockResolvedValue({ verified: true })
    archiveFrame.mockResolvedValue({ el: document.createElement('iframe'), render: vi.fn(), dispose: vi.fn() })
    archiveMessages.mockResolvedValue({ messages: [latest], next_before: null, requires_login: false })
    const wrapper = mount(V2WecomArchivePane, { props: { orderNo: ORDER } })
    await flushPromises()
    expect(wrapper.text()).not.toContain(candidate.name)
    createWWLoginPanel.mock.calls[0]?.[0].onLoginSuccess({ code: 'authorization-code' })
    await flushPromises()
    expect(archiveLogin).toHaveBeenCalledWith(ORDER, 'authorization-code', 'state')
    expect(archiveGroups).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('首次查看需要登录企业微信')
    expect(wrapper.text()).not.toContain('确认关联并查看')
    expect(archiveMessages).toHaveBeenCalledExactlyOnceWith(ORDER, pending, null)
    wrapper.unmount()
  })
  it('企微展示会话过期后可重新登录验证', async () => {
    const group = { ...candidate, match: 'name' }
    const createWWLoginPanel = vi.fn().mockReturnValue({ unmount: vi.fn() })
    loadWecomArchiveSdk.mockResolvedValue({ createWWLoginPanel })
    archiveSignature.mockResolvedValueOnce({ verified: true })
      .mockResolvedValueOnce({ verified: false, corp_id: 'corp', agent_id: 23, login_state: 'new-state' })
    archiveGroups.mockResolvedValue({ enabled: true, groups: [group], complete: true })
    archiveMessages.mockResolvedValue({ messages: [], next_before: null, requires_login: false })
    archiveFrame.mockImplementation((_sdk, _order, _sig, _el, onError: () => void) => {
      onError()
      return Promise.resolve({ el: document.createElement('iframe'), dispose: vi.fn() })
    })
    const wrapper = mount(V2WecomGroupPane, { props: { orderNo: ORDER } })
    await flushPromises()
    const reauthenticate = wrapper.findAll('button').find(button => button.text() === '重新验证企微身份')
    expect(reauthenticate).toBeDefined()
    await reauthenticate?.trigger('click')
    await flushPromises()
    expect(archiveSignature).toHaveBeenLastCalledWith(ORDER, undefined, true)
    expect(createWWLoginPanel.mock.calls[0]?.[0].params.state).toBe('new-state')
    wrapper.unmount()
  })

})


describe('独立企微聊天搜索', () => {
  beforeEach(() => {
    vi.resetAllMocks()
  })

  it('无系统订单号时也能搜索，先验证企微身份，再打开命中的历史消息', async () => {
    const group = { ...candidate, anchor_msgid: earliest.msgid }
    const unmount = vi.fn()
    const createWWLoginPanel = vi.fn().mockReturnValue({ unmount })
    loadWecomArchiveSdk.mockResolvedValue({ createWWLoginPanel })
    archiveSignature.mockResolvedValue({ verified: false, corp_id: 'corp', agent_id: 23, login_state: 'state' })
    archiveSearch.mockResolvedValue({ enabled: true, groups: [group], complete: true })
    archiveLogin.mockResolvedValue({ verified: true })
    archiveMessages.mockResolvedValueOnce({
      messages: [earliest], next_before: null, next_after: 6, requires_login: false,
    }).mockResolvedValueOnce({ messages: [latest], next_after: null, requires_login: false })
    const render = vi.fn().mockResolvedValue(undefined)
    archiveFrame.mockImplementation((_sdk, _order, _sig, mountPoint: HTMLElement) => {
      const el = document.createElement('iframe')
      mountPoint.appendChild(el)
      return Promise.resolve({ el, render, dispose: vi.fn() })
    })

    const wrapper = mount(V2WecomArchivePane, { props: { initialQuery: '合成关键词' } })
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(archiveGroups).not.toHaveBeenCalled()
    expect(archiveSearch).not.toHaveBeenCalled()
    expect(archiveMessages).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('首次查看需要登录企业微信')
    createWWLoginPanel.mock.calls[0]![0].onLoginSuccess({ code: 'authorization-code' })
    await flushPromises()

    expect(archiveLogin).toHaveBeenCalledWith(undefined, 'authorization-code', 'state')
    expect(archiveSearch).toHaveBeenCalledWith('合成关键词', expect.any(String), expect.any(String))
    expect(archiveMessages).toHaveBeenCalledWith(undefined, group, null, { around: earliest.msgid })
    expect(render).toHaveBeenLastCalledWith([earliest], { kind: 'focus', anchorId: earliest.msgid })
    await wrapper.findAll('button').find(button => button.text() === '加载更新的消息')?.trigger('click')
    await flushPromises()
    expect(archiveMessages).toHaveBeenLastCalledWith(undefined, group, null, { after: 6 })
    expect(render).toHaveBeenLastCalledWith([earliest, latest], { kind: 'preserve', anchorId: earliest.msgid })
    wrapper.unmount()
  })

  it('明确说明当前没有可查看的结果，即使企微搜索范围可能不完整', async () => {
    loadWecomArchiveSdk.mockResolvedValue({})
    archiveSignature.mockResolvedValue({ verified: true })
    archiveSearch.mockResolvedValue({ enabled: true, groups: [], complete: false })
    const wrapper = mount(V2WecomArchivePane, { props: { initialQuery: '合成关键词' } })
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('暂无可查看的匹配结果')
    expect(wrapper.text()).toContain('可调整关键词或日期后再查')
    expect(archiveMessages).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('拒绝超过 31 天的日期范围', async () => {
    const wrapper = mount(V2WecomArchivePane, { props: { initialQuery: '合成关键词' } })
    const [start, end] = wrapper.findAll('input[type=date]')
    await start?.setValue('2026-08-01')
    await end?.setValue('2026-09-01')
    await wrapper.get('form').trigger('submit')
    expect(wrapper.text()).toContain('日期范围最多 31 天')
    expect(archiveSignature).not.toHaveBeenCalled()
    expect(archiveSearch).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})


describe('订单详情与独立入口接线', () => {
  beforeEach(() => {
    vi.resetAllMocks()
  })

  function setupRouter() {
    return createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: V2WecomGroupPane, props: { orderNo: ORDER, sourceOrderNo: '来源单号2026' } },
        { path: '/v2/wecom-archive', name: 'v2-wecom-archive', component: V2WecomArchiveSearch },
      ],
    })
  }

  it('无匹配群时用来源单号打开独立搜索，链接里没有订单数据', async () => {
    request.mockResolvedValue({ groups: [], archive_available: true, archive_search_available: true })
    archiveGroups.mockResolvedValue({ enabled: true, groups: [], complete: true })
    archiveAccess.mockResolvedValue({ enabled: true })
    const router = setupRouter()
    await router.push('/')
    const wrapper = mount({ template: '<RouterView />' }, { global: { plugins: [router] } })
    await flushPromises()

    const entry = wrapper.get('a')
    expect(entry.text()).toBe('查找聊天记录')
    expect(entry.attributes('href')).toBe('/v2/wecom-archive')
    await entry.trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.fullPath).toBe('/v2/wecom-archive')
    expect(wrapper.get<HTMLInputElement>('input[type=search]').element.value).toBe('来源单号2026')
    expect(archiveSearch).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('只有订单查看权限时不展示独立搜索链接', async () => {
    request.mockResolvedValue({ groups: [], archive_available: true, archive_search_available: false })
    archiveGroups.mockResolvedValue({ enabled: true, groups: [], complete: true })
    const router = setupRouter()
    await router.push('/')
    const wrapper = mount({ template: '<RouterView />' }, { global: { plugins: [router] } })
    await flushPromises()
    expect(archiveGroups).toHaveBeenCalledWith(ORDER)
    expect(wrapper.find('a').exists()).toBe(false)
    wrapper.unmount()
  })

  it('未获准查看的账号直达独立页面也不会显示搜索框', async () => {
    archiveAccess.mockResolvedValue({ enabled: false })
    const router = setupRouter()
    await router.push('/v2/wecom-archive')
    const wrapper = mount({ template: '<RouterView />' }, { global: { plugins: [router] } })
    await flushPromises()
    expect(wrapper.text()).toContain('本账号暂未开通聊天记录查看权限')
    expect(wrapper.find('form').exists()).toBe(false)
    expect(archiveSignature).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
