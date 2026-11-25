// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import { BizError, HttpError } from '@/api/errors'
import { request } from '@/api/http'
import V2InternalChatSettings from '../V2InternalChatSettings.vue'

vi.mock('@/api/http', () => ({ request: vi.fn() }))

const send = vi.mocked(request)
const url = '/api/internal-chat/settings/'
const candidates = [
  { id: 71, name: '客服甲' },
  { id: 23, name: '客服乙' },
  { id: 40, name: '客服丙' },
]

it('启用客服席位时保存人事和订单席位、班次与接待上限', async () => {
  const legacy = { contact_ids: [71], greetings: [{ contact_id: 71, enabled: true, text: '原有欢迎语。' }], candidates }
  const seats = [
    { kind: 'hr', contact_ids: [23], capacity: 3, shifts: [{ contact_id: 23, weekday: 5, start: '09:00', end: '18:00' }] },
    { kind: 'orders', contact_ids: [40], capacity: 10, shifts: [] },
  ]
  send.mockImplementation(async config => config.url === url
    ? { primary_contact_id: 71, backup_contact_ids: [], candidates }
    : { ...legacy, seats: config.method === 'put' ? seats : [] })

  const page = mount(V2InternalChatSettings)
  await flushPromises()
  expect(page.get('.visitor-settings').text()).toContain('自动发欢迎语')
  const settings = page.get('.visitor-seat-settings')
  await settings.get('#visitor-seat-hr-contact').setValue('23')
  await settings.get('[aria-label="添加人事客服接待人员"]').trigger('click')
  await settings.get('#visitor-seat-hr-capacity').setValue('3')
  await settings.get('[aria-label="添加人事客服班次"]').trigger('click')
  await settings.get('[aria-label="星期"]').setValue('5')
  await settings.get('#visitor-seat-orders-contact').setValue('40')
  await settings.get('[aria-label="添加订单客服接待人员"]').trigger('click')
  await settings.get('form').trigger('submit')
  await flushPromises()
  expect(send).toHaveBeenLastCalledWith({ method: 'put', url: '/api/internal-chat/visitor/settings/', data: { seats } })
  expect(settings.text()).toContain('客服席位配置已保存')
  expect(page.find('.visitor-settings').exists()).toBe(false)
  page.unmount()
})

beforeEach(() => {
  send.mockReset()
  send.mockImplementation(async config => {
    if (config.url !== url) throw new HttpError(404, '未开放')
    return { primary_contact_id: 71, backup_contact_ids: [23], candidates }
  })
})

it('读取首选和备用客服，保存备用顺序并将移到首选的客服移出备用', async () => {
  const wrapper = mount(V2InternalChatSettings)
  await flushPromises()
  const primary = wrapper.get<HTMLSelectElement>('select#chat-primary-contact')
  const spare = wrapper.get<HTMLSelectElement>('select#chat-backup-contact')
  expect(send).toHaveBeenCalledWith({ url, method: 'GET' })
  expect(primary.element.value).toBe('71')
  expect(wrapper.get('ol').text()).toContain('客服乙')

  await spare.setValue('40')
  await wrapper.get('button[aria-label="添加备用客服"]').trigger('click')
  await wrapper.get('button[aria-label="客服丙上移"]').trigger('click')
  await primary.setValue('23')
  expect(wrapper.get('ol').text()).toContain('客服丙')
  expect(wrapper.get('ol').text()).not.toContain('客服乙')

  await spare.setValue('71')
  await wrapper.get('button[aria-label="添加备用客服"]').trigger('click')
  send.mockResolvedValueOnce({ primary_contact_id: 23, backup_contact_ids: [40, 71], candidates })
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(send).toHaveBeenLastCalledWith({
    url, method: 'PUT', data: { primary_contact_id: 23, backup_contact_ids: [40, 71] },
  })
  expect(wrapper.get('[role="status"]').text()).toBe('配置已保存')
})

it('读取失败时禁止保存，重读成功后可以清空名单，保存被拒绝会保留草稿', async () => {
  let first = true
  send.mockImplementation(async config => {
    if (config.url !== url) throw new HttpError(404, '未开放')
    if (first) {
      first = false
      throw new Error('断网')
    }
    return { primary_contact_id: 71, backup_contact_ids: [23], candidates }
  })
  const wrapper = mount(V2InternalChatSettings)
  await flushPromises()
  expect(wrapper.get('[role="alert"]').text()).toContain('读取配置失败')
  expect(wrapper.get<HTMLButtonElement>('button[type="submit"]').element.disabled).toBe(true)

  await wrapper.get('button[aria-label="重新读取通讯录配置"]').trigger('click')
  await flushPromises()
  await wrapper.get('select#chat-primary-contact').setValue('')
  await wrapper.get('button[aria-label="移除备用客服乙"]').trigger('click')
  send.mockRejectedValueOnce(new BizError(1000, '客服职位已变更'))
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(send).toHaveBeenLastCalledWith({
    url, method: 'PUT', data: { primary_contact_id: null, backup_contact_ids: [] },
  })
  expect(wrapper.get('[role="alert"]').text()).toBe('客服职位已变更')
  expect(wrapper.get<HTMLSelectElement>('select#chat-primary-contact').element.value).toBe('')
  expect(wrapper.find('ol').exists()).toBe(false)
})

it('在访客接待中为每个人保存独立的欢迎语', async () => {
  send.mockImplementation(async config => config.url === url
    ? { primary_contact_id: 71, backup_contact_ids: [23], candidates }
    : {
      contact_ids: [71],
      greetings: [{ contact_id: 71, enabled: true, text: '客服甲欢迎您。' }],
      candidates,
    })
  const wrapper = mount(V2InternalChatSettings)
  await flushPromises()
  const visitor = wrapper.get('.visitor-settings')

  await visitor.get('#visitor-reception-contact').setValue('23')
  await visitor.get('button[aria-label="添加接待人员"]').trigger('click')
  const added = visitor.get<HTMLInputElement>('#visitor-greeting-enabled-23')
  const submit = visitor.get<HTMLButtonElement>('button[type="submit"]')
  expect(added.element.checked).toBe(false)
  await added.setValue(true)
  expect(submit.element.disabled).toBe(true)
  await visitor.get('#visitor-greeting-text-23').setValue('你好，我是客服乙。')
  const payload = {
    contact_ids: [71, 23],
    greetings: [
      { contact_id: 71, enabled: true, text: '客服甲欢迎您。' },
      { contact_id: 23, enabled: true, text: '你好，我是客服乙。' },
    ],
  }
  send.mockResolvedValueOnce({ ...payload, candidates })
  await visitor.get('form').trigger('submit')
  await flushPromises()
  expect(send).toHaveBeenLastCalledWith({
    method: 'put', url: '/api/internal-chat/visitor/settings/', data: { ...payload, removed_contact_ids: [] },
  })
  expect(visitor.get('[role="status"]').text()).toBe('访客配置已保存')
  expect(submit.element.disabled).toBe(true)
})

it('关闭欢迎语保留该接待人员的正文，保存回执会回填到页面', async () => {
  send.mockImplementation(async config => config.url === url
    ? { primary_contact_id: 71, backup_contact_ids: [23], candidates }
    : {
      contact_ids: [71, 23],
      greetings: [
        { contact_id: 71, enabled: true, text: '客服甲欢迎您。' },
        { contact_id: 23, enabled: true, text: '欢迎联系。' },
      ],
      candidates,
    })
  const wrapper = mount(V2InternalChatSettings)
  await flushPromises()
  const visitor = wrapper.get('.visitor-settings')

  await visitor.get('#visitor-greeting-enabled-23').setValue(false)
  const payload = {
    contact_ids: [71, 23],
    greetings: [
      { contact_id: 71, enabled: true, text: '客服甲欢迎您。' },
      { contact_id: 23, enabled: false, text: '欢迎联系。' },
    ],
  }
  send.mockResolvedValueOnce({ ...payload, candidates })
  await visitor.get('form').trigger('submit')
  await flushPromises()
  expect(send).toHaveBeenLastCalledWith({
    method: 'put', url: '/api/internal-chat/visitor/settings/', data: { ...payload, removed_contact_ids: [] },
  })
  expect(visitor.get<HTMLInputElement>('#visitor-greeting-enabled-71').element.checked).toBe(true)
  expect(visitor.get<HTMLInputElement>('#visitor-greeting-enabled-23').element.checked).toBe(false)
  await visitor.get('button[aria-label="编辑客服乙的欢迎语"]').trigger('click')
  expect(visitor.get<HTMLTextAreaElement>('#visitor-greeting-text-23').element.value).toBe('欢迎联系。')
})

it('后端切回单人欢迎语版本时提示刷新，并禁止用新版页面保存', async () => {
  send.mockImplementation(async config => config.url === url
    ? { primary_contact_id: 71, backup_contact_ids: [23], candidates }
    : { contact_ids: [71], greeting_sender_id: 71, greeting_text: '欢迎联系。', candidates })
  const wrapper = mount(V2InternalChatSettings)
  await flushPromises()
  const visitor = wrapper.get('.visitor-settings')
  expect(visitor.get('[role="alert"]').text()).toBe('欢迎语配置版本已变更，请刷新页面后再配置。')
  expect(visitor.find('form').exists()).toBe(false)
})

it('可明确移除暂不可接待的人员，并且不提供其编辑入口', async () => {
  const visitorUrl = '/api/internal-chat/visitor/settings/'
  const payload = { contact_ids: [71], greetings: [{ contact_id: 71, enabled: true, text: '欢迎联系。' }] }
  send.mockImplementation(async config => config.url === url
    ? { primary_contact_id: 71, backup_contact_ids: [23], candidates }
    : { ...payload, candidates, unavailable_contacts: [{ id: 93, name: '暂离客服' }] })
  const wrapper = mount(V2InternalChatSettings)
  await flushPromises()
  const visitor = wrapper.get('.visitor-settings')
  const dormant = visitor.get('button[aria-label="移除暂离客服"]')

  expect(visitor.text()).toContain('暂不可接待')
  expect(visitor.find('#visitor-greeting-enabled-93').exists()).toBe(false)
  expect(visitor.find('button[aria-label="编辑暂离客服的欢迎语"]').exists()).toBe(false)
  await dormant.trigger('click')
  send.mockResolvedValueOnce({ ...payload, candidates, unavailable_contacts: [] })
  await visitor.get('form').trigger('submit')
  await flushPromises()

  expect(send).toHaveBeenLastCalledWith({
    method: 'put', url: visitorUrl, data: { ...payload, removed_contact_ids: [93] },
  })
  expect(visitor.text()).not.toContain('暂离客服')
})

it('从有效名单移除人员后明确传给服务器，保存前重新添加则撤销移除', async () => {
  const payload = {
    contact_ids: [71, 23],
    greetings: [
      { contact_id: 71, enabled: false, text: '' },
      { contact_id: 23, enabled: true, text: '客服乙欢迎您。' },
    ],
  }
  send.mockImplementation(async config => config.url === url
    ? { primary_contact_id: 71, backup_contact_ids: [23], candidates }
    : { ...payload, candidates, unavailable_contacts: [] })
  const wrapper = mount(V2InternalChatSettings)
  await flushPromises()
  const visitor = wrapper.get('.visitor-settings')
  const removeButton = 'button[aria-label="移除客服乙"]'
  await visitor.get(removeButton).trigger('click')
  await visitor.get('#visitor-reception-contact').setValue('23')
  await visitor.get('button[aria-label="添加接待人员"]').trigger('click')
  expect(visitor.get<HTMLInputElement>('#visitor-greeting-enabled-23').element.checked).toBe(true)
  expect(visitor.get<HTMLButtonElement>('button[type="submit"]').element.disabled).toBe(true)

  await visitor.get(removeButton).trigger('click')
  const remaining = { contact_ids: [71], greetings: [payload.greetings[0]] }
  send.mockResolvedValueOnce({ ...remaining, candidates, unavailable_contacts: [] })
  await visitor.get('form').trigger('submit')
  await flushPromises()

  expect(send).toHaveBeenLastCalledWith({
    method: 'put', url: '/api/internal-chat/visitor/settings/', data: { ...remaining, removed_contact_ids: [23] },
  })
})

it('保存遭遇后端回退时只复查一次，保留草稿并禁止再次提交', async () => {
  const visitorUrl = '/api/internal-chat/visitor/settings/'
  send.mockImplementation(async config => config.url === url
    ? { primary_contact_id: 71, backup_contact_ids: [23], candidates }
    : { contact_ids: [71], greetings: [{ contact_id: 71, enabled: true, text: '原正文' }], candidates })
  const wrapper = mount(V2InternalChatSettings)
  await flushPromises()
  const visitor = wrapper.get('.visitor-settings')
  await visitor.get('button[aria-label="编辑客服甲的欢迎语"]').trigger('click')
  const editor = visitor.get<HTMLTextAreaElement>('#visitor-greeting-text-71')
  await editor.setValue('还没保存的欢迎语')
  send.mockRejectedValueOnce(new BizError(1000, '该字段是必填项。'))
  send.mockResolvedValueOnce({ contact_ids: [71], greeting_sender_id: 71, greeting_text: '原正文', candidates })
  await visitor.get('form').trigger('submit')
  await flushPromises()

  expect(visitor.get('[role="alert"]').text()).toContain('请刷新页面')
  expect(editor.element.value).toBe('还没保存的欢迎语')
  expect(visitor.get<HTMLButtonElement>('button[type="submit"]').element.disabled).toBe(true)
  await visitor.get('form').trigger('submit')
  await flushPromises()
  expect(send.mock.calls.map(([config]) => config).filter(config => config.url === visitorUrl)
    .map(config => config.method ?? 'get')).toEqual(['get', 'put', 'get'])
})
