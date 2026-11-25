import { f7ready } from 'framework7-vue'

const htmlEscapes: Record<string, string> = {
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}

export function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (character) => htmlEscapes[character] ?? character)
}

type ToastMessage = string | { message: string; duration: number }

function showToast(input: ToastMessage): void {
  const { message, duration } = typeof input === 'string' ? { message: input, duration: 2000 } : input
  f7ready((app) => {
    const toast = app.toast.create({
      text: escapeHtml(message),
      position: 'center',
      closeTimeout: duration,
      destroyOnClose: true,
    })
    toast.el.setAttribute('role', 'status')
    toast.open()
  })
}

export const Toast = Object.assign(showToast, {
  error: showToast,
  success: showToast,
  warning: showToast,
})

interface Confirmation {
  title?: string
  content: string
  confirmBtn: string
  cancelBtn: string
  onConfirm: () => void
  onClosed?: () => void
}

export function confirm(options: Confirmation): { close: () => void } {
  let canceled = false
  let closeDialog: (() => void) | undefined
  f7ready((app) => {
    if (canceled) return
    const dialog = app.dialog.create({
      title: options.title ? escapeHtml(options.title) : undefined,
      text: escapeHtml(options.content),
      buttons: [
        { text: escapeHtml(options.cancelBtn) },
        { text: escapeHtml(options.confirmBtn), strong: true, onClick: options.onConfirm },
      ],
      destroyOnClose: true,
    })
    dialog.el.setAttribute('role', 'alertdialog')
    dialog.el.setAttribute('aria-modal', 'true')
    dialog.el.setAttribute('aria-label', options.title || options.content)
    for (const [index, button] of dialog.el.querySelectorAll<HTMLElement>('.dialog-button').entries()) {
      button.setAttribute('role', 'button')
      button.tabIndex = 0
      button.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter' && event.key !== ' ') return
        event.preventDefault()
        button.click()
      })
      if (index === 0) dialog.once('opened', () => button.focus())
    }
    const previousFocus = document.activeElement
    dialog.once('close', () => { closeDialog = undefined })
    dialog.once('closed', () => {
      if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus()
      options.onClosed?.()
    })
    closeDialog = () => {
      const wasOpened = dialog.opened
      dialog.close(false)
      // A dialog queued behind another one has no close event.
      if (!wasOpened) {
        closeDialog = undefined
        dialog.destroy()
        options.onClosed?.()
      }
    }
    dialog.open()
  })
  return {
    close() {
      canceled = true
      closeDialog?.()
    },
  }
}
