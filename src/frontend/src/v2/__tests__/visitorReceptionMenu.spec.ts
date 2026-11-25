// @vitest-environment jsdom
import { expect, it, vi } from 'vitest'
import { installVisitorReceptionMenu } from '../internalChat/visitorReceptionMenu'

it('按当前访客会话增添有分隔线的菜单操作，并保持原生选项', () => {
  const frame = document.createElement('iframe')
  document.body.append(frame)
  const doc = frame.contentDocument!
  doc.body.innerHTML = '<div class="chat-utils"><button class="btn-menu-toggle menu-open" aria-label="更多"></button></div>' +
    '<div class="btn-menu active"><div role="menuitem">搜索</div></div>'
  const transfer = vi.fn()
  const end = vi.fn()
  let assigned = true
  const binding = installVisitorReceptionMenu(doc, () => ({ enabled: true, assigned }), {
    toggle: vi.fn(), transfer, end,
  })
  try {
    const actions = () => Array.from(doc.querySelectorAll('[role="menuitem"]')).map(item => item.textContent)
    expect(actions()).toEqual(['搜索', '转接会话', '结束会话'])
    expect(doc.querySelector('hr')?.previousElementSibling?.textContent).toBe('搜索')
    doc.querySelector<HTMLElement>('[data-visitor-menu-action="end"]')!.click()
    expect(end).toHaveBeenCalledOnce()
    assigned = false
    binding.refresh()
    expect(actions()).toEqual(['搜索'])
    expect(doc.querySelector('hr')).toBeNull()
  } finally {
    binding.dispose()
    frame.remove()
  }
})

it('同一菜单里的开始和停止标签随接待状态切换', () => {
  const frame = document.createElement('iframe')
  document.body.append(frame)
  const doc = frame.contentDocument!
  doc.body.innerHTML = '<button class="sidebar-tools-button menu-open"></button>' +
    '<div class="btn-menu active"><div role="menuitem">在新窗口打开</div></div>'
  const toggle = vi.fn()
  let enabled = false
  const binding = installVisitorReceptionMenu(doc, () => ({ enabled, assigned: false }), {
    toggle, transfer: vi.fn(), end: vi.fn(),
  })
  try {
    const item = () => doc.querySelector<HTMLElement>('[data-visitor-menu-action="toggle"]')!
    expect(item().textContent).toBe('开始接待')
    item().click()
    expect(toggle).toHaveBeenCalledOnce()
    enabled = true
    binding.refresh()
    expect(item().textContent).toBe('停止接待')
  } finally {
    binding.dispose()
    frame.remove()
  }
})

it('访客会话沿用通讯录限制，切回内部会话后恢复添加联系人', () => {
  const frame = document.createElement('iframe')
  document.body.append(frame)
  const doc = frame.contentDocument!
  doc.body.innerHTML = '<div class="chat-utils"><button class="btn-menu-toggle menu-open" aria-label="更多"></button></div>' +
    '<div class="btn-menu active"><div class="btn-menu-item" role="menuitem"><span class="btn-menu-item-text">添加</span></div></div>'
  let visitor = true
  const binding = installVisitorReceptionMenu(doc, () => ({ assigned: false, visitor }), {
    toggle: vi.fn(), transfer: vi.fn(), end: vi.fn(),
  })
  try {
    const add = doc.querySelector<HTMLElement>('[role="menuitem"]')!
    expect(add.style.display).toBe('none')
    visitor = false
    binding.refresh()
    expect(add.style.display).toBe('')
  } finally {
    binding.dispose()
    frame.remove()
  }
})
