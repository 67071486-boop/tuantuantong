<script setup lang="ts">
import { onBeforeUnmount, onMounted, watch } from 'vue'
import { f7ready } from 'framework7-vue'
import type { PhotoBrowser, Popup } from 'framework7/types'
import { escapeHtml } from '../framework7Ui'

const props = withDefaults(defineProps<{
  visible: boolean
  images: string[]
  index?: number
  showIndex?: boolean
}>(), { index: 0 })
const emit = defineEmits<{ 'update:visible': [visible: boolean]; indexChange: [index: number] }>()

let browser: PhotoBrowser.PhotoBrowser | undefined
let popup: Popup.Popup | undefined
let active = false
let previousFocus: HTMLElement | null = null

function close(): void {
  popup?.close(false)
}

function sync(): void {
  if (!props.visible || !props.images.length) {
    close()
    return
  }
  f7ready((app) => {
    if (!active || !props.visible || !props.images.length || browser) return
    previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const current = app.photoBrowser.create({
      type: 'popup',
      theme: 'dark',
      toolbar: false,
      navbarShowCount: props.showIndex,
      navbarOfText: '/',
      popupCloseLinkText: '关闭',
      photos: props.images.map(escapeHtml),
    })
    current.on('slideChange', () => emit('indexChange', current.activeIndex))
    current.on('closed', () => {
      const closedPopup = popup
      browser = undefined
      popup = undefined
      // Let Framework7 finish dispatching its close handlers before destroying the instances.
      queueMicrotask(() => {
        closedPopup?.destroy()
        current.destroy()
      })
      if (!active) return
      emit('update:visible', false)
      if (previousFocus?.isConnected) previousFocus.focus()
    })
    current.on('open', () => {
      popup = app.popup.get(current.el)
      // Route teardown can release the popup and its backdrop in the same tick.
      popup.params.animate = false
      current.el.setAttribute('role', 'dialog')
      current.el.setAttribute('aria-label', '图片预览')
      current.el.setAttribute('aria-modal', 'true')
      const closeLink = current.el.querySelector<HTMLElement>('.popup-close')
      closeLink?.setAttribute('role', 'button')
      closeLink?.setAttribute('aria-label', '关闭图片预览')
      if (closeLink) closeLink.tabIndex = 0
      closeLink?.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter' && event.key !== ' ') return
        event.preventDefault()
        closeLink.click()
      })
      current.once('opened', () => closeLink?.focus())
    })
    browser = current
    current.open(props.index)
  })
}

onMounted(() => {
  active = true
  sync()
})
watch(() => props.visible, sync)
onBeforeUnmount(() => {
  active = false
  close()
})
</script>

<template>
  <span hidden aria-hidden="true" />
</template>
