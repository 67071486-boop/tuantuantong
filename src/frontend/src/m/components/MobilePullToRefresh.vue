<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, useTemplateRef, watch } from 'vue'
import { f7ready } from 'framework7-vue'
import type { PullToRefresh } from 'framework7/types'

const props = defineProps<{ refreshing: boolean }>()
const emit = defineEmits<{ refresh: [] }>()
const content = useTemplateRef<HTMLElement>('content')
let ptr: PullToRefresh.PullToRefresh | undefined
let active = true

onMounted(() => f7ready((app) => {
  const scroll = content.value?.closest<HTMLElement>('.m-shell__scroll')
  if (!active || !scroll) return
  scroll.classList.add('ptr-content')
  ptr = app.ptr.create(scroll)
  ptr.on('refresh', () => {
    emit('refresh')
    void nextTick(() => {
      if (!props.refreshing && ptr?.el.classList.contains('ptr-refreshing')) ptr.done()
    })
  })
}))

watch(() => props.refreshing, (refreshing) => {
  if (!refreshing && ptr?.el.classList.contains('ptr-refreshing')) ptr.done()
})

onBeforeUnmount(() => {
  active = false
  if (!ptr) return
  const { el } = ptr
  ptr.destroy()
  ptr = undefined
  el.classList.remove('ptr-content', 'ptr-no-navbar', 'ptr-pull-up', 'ptr-pull-down', 'ptr-transitioning', 'ptr-refreshing', 'ptr-closing')
  for (const child of el.children) {
    if (child instanceof HTMLElement) child.style.removeProperty('transform')
  }
})
</script>

<template>
  <div ref="content"><slot /></div>
</template>
