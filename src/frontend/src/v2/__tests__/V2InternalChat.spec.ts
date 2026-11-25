// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { expect, it } from 'vitest'
import V2InternalChat from '../V2InternalChat.vue'

function mountChat() {
  return mount(V2InternalChat, { global: { plugins: [createPinia()] } })
}

it('places a personal remark, the full order title and preview on three distinct rows', async () => {
  const wrapper = mountChat()
  const row = wrapper.get('button[aria-label*="今晚要交的单"]')
  expect(row.get('.cv2-chat-title').text()).toBe('今晚要交的单')
  expect(row.get('.cv2-chat-order').text()).toBe('订单-20261116000000000091')
  expect(row.get('.cv2-chat-preview').text()).toBe('还差一局，打完发截图。')
  expect(row.get('[aria-label="3 条未读消息"]').text()).toBe('3')
  expect(row.find('[aria-label="已置顶"]').exists()).toBe(true)

  await row.trigger('click')
  const header = wrapper.get('section[aria-label="会话窗口"] header')
  expect(header.get('h2').text()).toBe('今晚要交的单')
  expect(header.get('.cv2-chat-order').text()).toBe('订单-20261116000000000091')
  expect(header.text()).not.toContain('还差一局')
  expect(row.find('[aria-label="3 条未读消息"]').exists()).toBe(false)

  await wrapper.get('button[aria-label="会话设置"]').trigger('click')
  await wrapper.get('button[aria-label="编辑备注"]').trigger('click')
  await wrapper.get('[role="dialog"] input').setValue('')
  await wrapper.get('[role="dialog"] form').trigger('submit')
  expect(header.get('h2').text()).toBe('订单-20261116000000000091')
  expect(header.find('.cv2-chat-order').exists()).toBe(false)
  expect(row.get('.cv2-chat-title').text()).toBe('订单-20261116000000000091')
  expect(row.find('.cv2-chat-order').exists()).toBe(false)
})

it('searches by remark, order number and contact, and reuses open direct conversations', async () => {
  const wrapper = mountChat()
  const search = wrapper.get('input[aria-label="搜索会话"]')
  const list = wrapper.get('section[aria-label="会话列表"]')
  await search.setValue('今晚')
  expect(list.findAll('.cv2-chat-row')).toHaveLength(1)
  await search.setValue('20261116000000000074')
  expect(list.findAll('.cv2-chat-row')).toHaveLength(1)
  expect(list.text()).toContain('订单-20261116000000000074')
  await search.setValue('阿洛')
  expect(list.findAll('.cv2-chat-row')).toHaveLength(2)
  await search.setValue('不存在')
  expect(list.text()).toContain('没有匹配的会话')

  for (let i = 0; i < 2; i++) {
    await wrapper.get('button[aria-label="发起会话"]').trigger('click')
    const contactSearch = wrapper.get('input[aria-label="搜索联系人"]')
    await contactSearch.setValue('不存在')
    expect(list.text()).toContain('没有匹配的联系人')
    await contactSearch.setValue('陈')
    expect(list.findAll('.cv2-chat-contact')).toHaveLength(1)
    await wrapper.get('.cv2-chat-contact').trigger('click')
    expect(wrapper.get('section[aria-label="会话窗口"] h2').text()).toBe('陈晨')
    expect(list.findAll('.cv2-chat-row[aria-label="陈晨"]')).toHaveLength(1)
  }
})

it('pins and unpins from the context menu without opening or marking a conversation read', async () => {
  const wrapper = mountChat()
  const row = wrapper.get('.cv2-chat-row[aria-label^="订单-20261116000000000074"]')
  await row.trigger('contextmenu')
  await wrapper.get('button[role="menuitem"]').trigger('click')
  expect(wrapper.findAll('.cv2-chat-row').slice(0, 2).map((item) => item.attributes('aria-label')))
    .toEqual([expect.stringContaining('今晚要交的单'), '订单-20261116000000000074，已置顶，2 条未读消息'])
  expect(wrapper.get('section[aria-label="会话窗口"] h2').text()).toBe('订单-20261116000000000086')

  await row.trigger('keydown', { key: 'F10', shiftKey: true })
  expect(wrapper.get('button[role="menuitem"]').text()).toBe('取消置顶')
  await wrapper.get('button[role="menuitem"]').trigger('click')
  expect(wrapper.findAll('.cv2-chat-row')[2]?.attributes('aria-label')).toBe('订单-20261116000000000074，2 条未读消息')
})

it('quotes from the message context menu, cancels, inserts a member mention and jumps to the sent quote', async () => {
  const wrapper = mountChat()
  const bubble = wrapper.findAll('.cv2-chat-bubble').find((message) => message.text() === '收到，已经开打。')!
  await bubble.trigger('contextmenu')
  await wrapper.get('button[role="menuitem"]').trigger('click')
  expect(wrapper.get('.cv2-chat-draft-quote').text()).toContain('收到，已经开打。')
  await wrapper.get('button[aria-label="取消引用"]').trigger('click')
  expect(wrapper.find('.cv2-chat-draft-quote').exists()).toBe(false)
  await bubble.trigger('keydown', { key: 'ContextMenu' })
  await wrapper.get('button[role="menuitem"]').trigger('click')

  const editor = wrapper.get('textarea[aria-label="输入消息"]')
  await editor.setValue('@')
  await wrapper.get('button[aria-label="@阿洛"]').trigger('click')
  expect((editor.element as HTMLTextAreaElement).value).toBe('@阿洛 ')
  await editor.setValue('@阿洛 请发结算截图')
  await editor.trigger('keydown', { key: 'Enter' })
  const log = wrapper.get('[role="log"]')
  const reply = log.get('button[aria-label^="查看引用原文，阿洛：收到"]')
  expect(log.text()).toContain('@阿洛 请发结算截图')
  await reply.trigger('click')
  expect(log.get('.cv2-chat-message--highlighted').text()).toContain('收到，已经开打。')
  expect(wrapper.find('.cv2-chat-draft-quote').exists()).toBe(false)
})

it('updates member options when the message caret moves through existing text', async () => {
  const wrapper = mountChat()
  const editor = wrapper.get('textarea')
  await editor.setValue('请 @阿')
  expect(wrapper.find('button[aria-label="@阿洛"]').exists()).toBe(true)

  editor.element.setSelectionRange(0, 0)
  await editor.trigger('selectionchange')
  expect(wrapper.find('button[aria-label="@阿洛"]').exists()).toBe(false)

  editor.element.setSelectionRange(3, 3)
  await editor.trigger('selectionchange')
  expect(wrapper.find('button[aria-label="@小林"]').exists()).toBe(true)
})
