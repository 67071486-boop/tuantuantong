<script setup lang="ts">
import { ref } from 'vue'
import OrderTabDialog from '@/v2/OrderTabDialog.vue'

defineProps<{ src: string; alt: string }>()
const zoom = ref(false)
</script>

<template>
  <button type="button" class="approval-image" :aria-label="`放大查看 ${alt}`" aria-haspopup="dialog" @click="zoom = true">
    <img :src="src" :alt="alt" />
  </button>
  <OrderTabDialog
    v-if="zoom"
    :model-value="true"
    class="cv2-dialog"
    title="图片预览"
    width="min(90vw, 1100px)"
    @update:model-value="zoom = false"
  >
    <img class="approval-image-full" :src="src" :alt="alt" />
  </OrderTabDialog>
</template>

<style scoped>
.approval-image {
  display: block;
  padding: 0;
  background: var(--tt2-panel);
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
  cursor: zoom-in;
}
.approval-image:focus-visible { outline: 2px solid var(--tt2-primary); }
.approval-image img {
  display: block;
  width: var(--approval-thumb-width, 110px);
  height: var(--approval-thumb-height, 80px);
  object-fit: contain;
}
.approval-image-full { display: block; max-width: 100%; max-height: 72vh; margin: 0 auto; object-fit: contain; }
</style>
