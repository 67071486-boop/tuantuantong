type Action = 'toggle' | 'transfer' | 'end'
type MenuState = { enabled?: boolean, assigned: boolean, visitor?: boolean, busy?: boolean }

const icons: Record<Action, string> = {
  toggle: 'M9 18a6 6 0 0 1 12 0M15 12a3 3 0 1 0 0-6 3 3 0 0 0 0 6M3 12h4m-2-2v4',
  transfer: 'M4 8h14m-4-4 4 4-4 4M20 16H6m4-4-4 4 4 4',
  end: 'M6 6l12 12M18 6 6 18',
}

function menuItem(doc: Document, action: Action, label: string, onSelect: () => void, busy: boolean) {
  const row = doc.createElement('div')
  row.className = 'btn-menu-item rp-overflow' + (action === 'end' ? ' danger' : '')
  row.dataset.visitorMenuAction = action
  row.setAttribute('role', 'menuitem')
  row.setAttribute('aria-disabled', String(busy))
  row.tabIndex = busy ? -1 : 0
  const icon = doc.createElementNS('http://www.w3.org/2000/svg', 'svg')
  icon.classList.add('btn-menu-item-icon')
  icon.setAttribute('viewBox', '0 0 24 24')
  icon.setAttribute('width', '24')
  icon.setAttribute('height', '24')
  icon.setAttribute('aria-hidden', 'true')
  const path = doc.createElementNS(icon.namespaceURI, 'path')
  path.setAttribute('d', icons[action])
  path.setAttribute('fill', 'none')
  path.setAttribute('stroke', 'currentColor')
  path.setAttribute('stroke-width', '1.7')
  path.setAttribute('stroke-linecap', 'round')
  path.setAttribute('stroke-linejoin', 'round')
  icon.append(path)
  const text = doc.createElement('span')
  text.className = 'btn-menu-item-text'
  text.textContent = label
  row.append(icon, text)
  const activate = (event: Event) => {
    event.preventDefault()
    event.stopImmediatePropagation()
    if (!busy) onSelect()
  }
  row.addEventListener('click', activate)
  row.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') activate(event)
  })
  return row
}

export function installVisitorReceptionMenu(doc: Document, state: () => MenuState, actions: Record<Action, () => void>) {
  let currentMenu: HTMLElement | undefined
  let signature: string | undefined
  let controls: HTMLElement[] = []
  const hidden = new Map<HTMLElement, string>()
  const unsupported = new Set(['搜索', '选择消息', '添加', '添加联系人', '屏蔽用户', '取消屏蔽'])
  const selector = '.sidebar-tools-button.menu-open, .chat-utils .btn-menu-toggle.menu-open[aria-label="更多"]'

  function hideUnavailableActions(visitor: boolean, conversationMenu: HTMLElement | undefined) {
    const unavailable = new Set<HTMLElement>()
    if (visitor) {
      doc.querySelectorAll<HTMLElement>('.chat-utils [aria-label="搜索"]').forEach(item => unavailable.add(item))
      conversationMenu?.querySelectorAll<HTMLElement>(':scope > .btn-menu-item').forEach(item => {
        const label = item.querySelector('.btn-menu-item-text')?.textContent?.trim()
        if (label && unsupported.has(label)) unavailable.add(item)
      })
    }
    for (const [item, display] of hidden) {
      if (unavailable.has(item)) continue
      item.style.display = display
      hidden.delete(item)
    }
    for (const item of unavailable) {
      if (hidden.has(item)) continue
      hidden.set(item, item.style.display)
      item.style.display = 'none'
    }
  }

  function refresh() {
    const trigger = doc.querySelector<HTMLElement>(selector)
    const menu = trigger ? doc.querySelector<HTMLElement>('.btn-menu.active') ?? undefined : undefined
    const { enabled, assigned, visitor = false, busy = false } = state()
    const isMain = trigger?.matches('.sidebar-tools-button') === true
    hideUnavailableActions(visitor, isMain ? undefined : menu)
    const items: [Action, string][] = []
    if (isMain && enabled !== undefined) items.push(['toggle', enabled ? '停止接待' : '开始接待'])
    if (!isMain && assigned) items.push(['transfer', '转接会话'], ['end', '结束会话'])
    const nextSignature = JSON.stringify([items, busy])
    if (menu === currentMenu && signature === nextSignature) return
    controls.forEach(element => element.remove())
    controls = []
    currentMenu = menu
    signature = nextSignature
    if (!menu || !items.length) return

    if (menu.lastElementChild && menu.lastElementChild.tagName !== 'HR') {
      const line = doc.createElement('hr')
      line.setAttribute('role', 'separator')
      controls.push(line)
    }
    for (const [action, label] of items) {
      controls.push(menuItem(doc, action, label, () => {
        trigger?.click()
        actions[action]()
      }, busy))
    }
    menu.append(...controls)
  }

  // Web K mounts its overlays outside the trigger and may rebuild them for each opening.
  const observer = new MutationObserver((records) => {
    if (records.some(record => record.type === 'childList'
      ? [...record.addedNodes, ...record.removedNodes].some(node => node.nodeType === 1 && (node as Element).matches('.btn-menu'))
      : (record.target as Element).matches('.btn-menu, .btn-menu-toggle, .sidebar-tools-button'))) refresh()
  })
  observer.observe(doc.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ['class'] })
  refresh()
  return {
    refresh,
    dispose() {
      observer.disconnect()
      hideUnavailableActions(false, undefined)
      controls.forEach(element => element.remove())
    },
  }
}
