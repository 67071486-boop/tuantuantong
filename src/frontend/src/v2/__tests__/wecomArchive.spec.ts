// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { archiveFrame, archiveMessages, archiveRememberGroup, archiveSearch, type ArchiveMessage } from '../wecomArchive'
import { request } from '@/api/http'

vi.mock('@/api/http', () => ({ request: vi.fn() }))

type Sdk = Parameters<typeof archiveFrame>[0]
type FrameOptions = Parameters<ReturnType<Sdk['createOpenDataFrameFactory']>['createOpenDataFrame']>[0]
const signature = {
  corp_id: 'corp', agent_id: 1, timestamp: 1, nonce: 'nonce',
  corp_signature: 'corp-sig', agent_signature: 'agent-sig', verified: true, login_state: 'state',
}

async function setup() {
  const setData = vi.fn().mockResolvedValue(undefined)
  const instance = { el: document.createElement('iframe'), setData, dispose: vi.fn() }
  const createOpenDataFrame = vi.fn((_options: FrameOptions) => instance)
  const scrollIntoView = vi.fn()
  const sdk: Sdk = {
    register: vi.fn(), initOpenData: vi.fn().mockResolvedValue(undefined), createWWLoginPanel: vi.fn(),
    createOpenDataFrameFactory: () => ({ createOpenDataFrame }),
    createScrollViewContext: vi.fn().mockResolvedValue({ scrollIntoView }),
  }
  const loadEarlier = vi.fn()
  const frame = await archiveFrame(sdk, 'order', signature, document.createElement('div'),
    vi.fn(), vi.fn(), 'room', loadEarlier)
  const options = createOpenDataFrame.mock.calls[0]![0]
  options.handleUpdated()
  return { frame, options, setData, scrollIntoView, loadEarlier }
}

describe('企微会话组件的聊天时间线', () => {
  it('等消息视图更新后再滚动，翻页保持原先的像素位置', async () => {
    const { frame, options, setData, scrollIntoView, loadEarlier } = await setup()
    const latest: ArchiveMessage = {
      msgid: 'latest', sent_at: '2026-11-14T00:15:00Z', secret_key: 'latest-key',
      sender: { id: 'staff', type: 1, is_self: true },
    }
    const customer: ArchiveMessage = {
      msgid: 'customer', sent_at: '2026-11-14T00:12:00Z', secret_key: 'customer-key',
      sender: { id: 'external', type: 2, is_self: false },
    }
    const unknown: ArchiveMessage = { msgid: 'older', sent_at: '2026-11-14T00:11:00Z', secret_key: 'older-key' }
    const afterGap: ArchiveMessage = { msgid: 'after-gap', sent_at: '2026-11-14T00:20:00Z', secret_key: 'gap-key' }
    const tomorrow: ArchiveMessage = { msgid: 'tomorrow', sent_at: '2026-11-15T00:00:00Z', secret_key: 'next-day-key' }

    expect(options.template).toContain('<ww-open-message')
    expect(options.template).toContain('open-type="viewMessage"')
    expect(options.template).toContain('class="chat-time-separator"')
    expect(options.template).toContain('openid="{{item.identity}}"')
    expect(options.style).toMatch(/\.chat-message-loading\s*\{[^}]*color: var\(--tt2-ink,/u)

    const firstPage = frame.render([latest], { kind: 'latest' })
    await flushPromises()
    expect(scrollIntoView).not.toHaveBeenCalled()
    expect(setData).toHaveBeenLastCalledWith(expect.objectContaining({
      messages: [expect.objectContaining({
        msgid: 'latest', secret_key: 'latest-key', self: true, identity: 'staff',
        nameType: 'userName', avatarType: 'userAvatar', time: expect.stringMatching(/08:15$/),
      })],
    }))
    options.handleUpdated()
    await firstPage
    expect(scrollIntoView).toHaveBeenLastCalledWith('.chat-latest', { block: 'end' })
    options.methods.updateScroll({ detail: { scrollTop: 87 } })
    options.methods.loadEarlier()
    expect(loadEarlier).toHaveBeenCalledOnce()

    const olderPage = frame.render(
      [unknown, customer, latest, afterGap, tomorrow],
      { kind: 'preserve', anchorId: latest.msgid },
    )
    await flushPromises()
    expect(scrollIntoView).toHaveBeenCalledOnce()
    expect(setData).toHaveBeenLastCalledWith(expect.objectContaining({
      anchorOffset: 87,
      messages: [
        expect.objectContaining({ msgid: 'older', senderLabel: '未知发送人', identity: '', self: false, time: '2026年11月14日 08:11', isViewportAnchor: false }),
        expect.objectContaining({ msgid: 'customer', identity: 'room/external', nameType: 'externalUserName', self: false, time: '', isViewportAnchor: false }),
        expect.objectContaining({ msgid: 'latest', time: '', isViewportAnchor: true, anchorLostSeparator: true }),
        expect.objectContaining({ msgid: 'after-gap', time: '08:20' }),
        expect.objectContaining({ msgid: 'tomorrow', time: '2026年11月15日 08:00' }),
      ],
    }))
    options.handleUpdated()
    await olderPage
    expect(scrollIntoView).toHaveBeenLastCalledWith('.chat-viewport-anchor', { block: 'start' })
    frame.dispose()
  })


  it('区分员工和各类外部联系人，并为身份缺失的旧消息显示默认头像和未知发送人', async () => {
    const { frame, options, setData } = await setup()
    const base = { sent_at: '2026-11-14T00:15:00Z', secret_key: 'message-key' }
    const displayed = frame.render([
      { ...base, msgid: 'mine', sender: { id: 'self', type: 1, is_self: true } },
      { ...base, msgid: 'coworker', sender: { id: 'employee', type: 1, is_self: false } },
      { ...base, msgid: 'customer', sender: { id: 'client', type: 2, is_self: false, external_kind: 'wechat' } },
      { ...base, msgid: 'partner', sender: {
        id: 'partner', type: 2, is_self: false, external_kind: 'wecom', corp_name: '合作企业',
      } },
      { ...base, msgid: 'partner-missing-company', sender: {
        id: 'partner-other', type: 2, is_self: false, external_kind: 'wecom',
      } },
      { ...base, msgid: 'client-unresolved', sender: { id: 'other', type: 2, is_self: false } },
      { ...base, msgid: 'old' },
      { ...base, msgid: 'robot', sender: { id: 'bot', type: 3, is_self: false } },
    ], { kind: 'latest' })
    await flushPromises()
    options.handleUpdated()
    await displayed

    expect(setData).toHaveBeenLastCalledWith(expect.objectContaining({
      messages: [
        expect.objectContaining({ msgid: 'mine', self: true, identity: 'self', nameType: 'userName', senderLabel: '未知发送人', senderBadge: '' }),
        expect.objectContaining({ msgid: 'coworker', self: false, identity: 'employee', nameType: 'userName', senderLabel: '未知发送人', senderBadge: '' }),
        expect.objectContaining({ msgid: 'customer', self: false, identity: 'room/client', nameType: 'externalUserName', avatarType: 'externalUserAvatar', senderBadge: '@微信', senderBadgeKind: 'wechat' }),
        expect.objectContaining({ msgid: 'partner', identity: 'room/partner', senderBadge: '@合作企业', senderBadgeKind: 'company' }),
        expect.objectContaining({ msgid: 'partner-missing-company', senderBadge: '@企业微信', senderBadgeKind: 'company' }),
        expect.objectContaining({ msgid: 'client-unresolved', identity: 'room/other', senderBadge: '@外部联系人', senderBadgeKind: 'external' }),
        expect.objectContaining({ msgid: 'old', self: false, identity: '', senderLabel: '未知发送人', senderBadge: '', senderBadgeKind: '' }),
        expect.objectContaining({ msgid: 'robot', self: false, identity: '', senderLabel: '机器人', senderBadge: '' }),
      ],
    }))
    expect(options.template).toContain('class="chat-sender"')
    expect(options.template).toContain('<span class="chat-sender-name"><ww-open-data')
    expect(options.template).not.toContain('item.senderLabel && !item.self')
    expect(options.template).toContain('<span wx:if="{{item.senderBadge}}"')
    expect(options.template).toContain('class="chat-sender-badge chat-sender-badge--{{item.senderBadgeKind}}">')
    expect(options.template).toContain('{{item.senderBadge}}</span>')
    expect(options.style).toMatch(/\.chat-sender-badge--company\s*\{\s*color: var\(--tt2-wecom-company-label, #FF8214\);\s*\}/u)
    expect(options.template).toContain('<span class="chat-sender-fallback">{{item.senderLabel}}</span>')
    expect(options.style).toMatch(/\.chat-sender-name:not\(\.chat-sender-name:empty\) \+ \.chat-sender-fallback/u)
    expect(options.template).toContain('class="chat-default-avatar"')
    expect(options.template).not.toContain('chat-avatar--unknown')
    expect(options.style).not.toMatch(/--tt2-(?:line|dim|wecom-wechat-label|wecom-company-label):\s*;/u)
    expect(options.style).toMatch(/\.chat-sender-badge--wechat\s*\{\s*color: var\(--tt2-wecom-wechat-label, #15B628\);\s*\}/u)
    expect(options.style).toMatch(/\.chat-sender-badge\s*\{\s*white-space: nowrap;\s*\}/u)
    frame.dispose()
  })

  it('组件一直未更新时结束等待，让页面可以重试', async () => {
    vi.useFakeTimers()
    try {
      const { frame, scrollIntoView } = await setup()
      const failure = frame.render([], { kind: 'latest' }).catch((error: unknown) => error)
      await vi.advanceTimersByTimeAsync(12000)
      expect(await failure).toEqual(new Error('企微组件未能定位聊天记录，请重试'))
      expect(scrollIntoView).not.toHaveBeenCalled()
      frame.dispose()
    } finally {
      vi.useRealTimers()
    }
  })
})

it('独立搜索关键词通过 POST 提交，浏览历史需要服务端签发的群凭证', async () => {
  await archiveSearch('合成客户关键词', '2026-11-01', '2026-11-15')
  expect(request).toHaveBeenLastCalledWith({
    url: '/api/wecom/archive/search/',
    method: 'POST',
    data: { q: '合成客户关键词', start_date: '2026-11-01', end_date: '2026-11-15' },
  })
  await archiveMessages(undefined, { chat_id: 'group', name: '已授权群聊', match: 'message', proof: 'signed-group' },
    null, { around: 'matched-message' })
  expect(request).toHaveBeenLastCalledWith({
    url: '/api/wecom/archive/messages/',
    params: { chat_id: 'group', around: 'matched-message' },
    headers: { 'X-Wecom-Chat-Proof': 'signed-group' },
  })
})

it('记住订单命中的群聊时附上订单群凭证', async () => {
  const group = { chat_id: 'group', proof: 'signed-group' }
  await archiveRememberGroup('20261114000000000001', group)
  expect(request).toHaveBeenLastCalledWith({
    url: '/api/wecom/orders/20261114000000000001/chat/groups/',
    method: 'POST',
    data: { chat_id: 'group' },
    headers: { 'X-Wecom-Chat-Proof': 'signed-group' },
  })
})
