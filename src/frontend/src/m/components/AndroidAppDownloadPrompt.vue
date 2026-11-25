<script setup lang="ts">
import { f7Button as F7Button } from 'framework7-vue'
import { ref, watch } from 'vue'
import MobileEntryPrompt from './MobileEntryPrompt.vue'

const props = defineProps<{ eligible: boolean }>()
const seenKey = 'tt:android-download-prompt-seen'
const mutedKey = 'tt:android-download-prompt-muted'
const visible = ref(false)
let seen = false

type StorageName = 'localStorage' | 'sessionStorage'

function readFlag(storage: StorageName, key: string): boolean {
  try {
    return window[storage].getItem(key) === '1'
  } catch {
    return false
  }
}

function writeFlag(storage: StorageName, key: string): void {
  try {
    window[storage].setItem(key, '1')
  } catch {
    // The mounted app still remembers that the user has seen the prompt.
  }
}

watch(() => props.eligible, (eligible) => {
  if (!eligible) {
    visible.value = false
    return
  }
  if (seen || readFlag('localStorage', mutedKey) || readFlag('sessionStorage', seenKey)) return
  seen = true
  writeFlag('sessionStorage', seenKey)
  visible.value = true
}, { immediate: true })

function mute(): void {
  writeFlag('localStorage', mutedKey)
  visible.value = false
}
</script>

<template>
  <MobileEntryPrompt
    v-if="visible"
    title="下载代代通 App"
    description="使用代代通 App 接单和聊天，及时接收新订单和聊天消息提醒。"
    @dismiss="visible = false"
  >
    <F7Button href="/download/" external large fill @click="visible = false">下载 App</F7Button>
    <F7Button type="button" large tonal @click="visible = false">稍后再说</F7Button>
    <F7Button type="button" class="m-download-prompt-mute" @click="mute">不再提醒</F7Button>
  </MobileEntryPrompt>
</template>

<style scoped>
.m-download-prompt-mute.button {
  --f7-button-height: 44px;
  --f7-button-text-color: var(--m-muted, #67636d);
  font-size: 14px;
  font-weight: 400;
}
</style>
