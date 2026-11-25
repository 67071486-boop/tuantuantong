<script setup lang="ts">
import { computed, inject } from 'vue'
import { RouterLink } from 'vue-router'
import { openInternalChatPreview } from './launcherPreview'
import { useChatPreviewUnread } from './liveChatPreview'

const props = defineProps<{ displayName: string; roleName: string; avatarSrc: string; production?: boolean }>()
const emit = defineEmits<{ navigate: [] }>()

const unreadTotal = useChatPreviewUnread(() => props.production === true)
const openPreview = inject(openInternalChatPreview, null)
const entryLabel = computed(() => (
  unreadTotal.value ? `内部沟通，${unreadTotal.value} 条未读消息` : '内部沟通'
))

function activate(): void {
  openPreview?.()
  emit('navigate')
}
</script>

<template>
  <div class="cv2-side-user">
    <img class="cv2-avatar" :src="avatarSrc" alt="" />
    <p class="cv2-user-line"><b>{{ displayName }}</b>[{{ roleName }}]</p>
  </div>
  <Teleport to="#cv2-topbar-chat" defer>
    <component
      :is="openPreview ? 'button' : RouterLink"
      :type="openPreview ? 'button' : undefined"
      :to="openPreview ? undefined : { name: 'v2-internal-chat' }"
      class="cv2-chat-entry cv2-chat-topbar-entry"
      :aria-label="entryLabel"
      :title="entryLabel"
      :aria-controls="openPreview ? 'cv2-chat-launcher-window' : undefined"
      @click="activate"
    >
      <span class="cv2-chat-entry-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" focusable="false">
          <path fill="currentColor" d="M12 3a9 9 0 1 0 0 18h9v-9a9 9 0 0 0-9-9Z" />
        </svg>
      </span>
      <span>内部沟通</span>
      <span v-if="unreadTotal" class="cv2-chat-count" aria-hidden="true">{{ unreadTotal > 99 ? '99+' : unreadTotal }}</span>
    </component>
  </Teleport>
</template>
<style scoped>
.cv2-chat-entry.cv2-chat-topbar-entry {
  flex: none;
  width: auto;
  min-height: 32px;
  gap: 6px;
  padding: 0 8px;
  white-space: nowrap;
}

.cv2-chat-topbar-entry .cv2-chat-count {
  position: static;
}
</style>
