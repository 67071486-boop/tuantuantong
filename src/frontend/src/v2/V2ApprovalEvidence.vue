<script setup lang="ts">
import { onUnmounted, ref } from 'vue'
import { requestApprovalImageTicket } from '@/api/approvals'
import { putFileToStorage } from '@/layui-island/directUpload'
import { COMPRESSED_MAX_BYTES, MAX_UPLOAD_BYTES, compressImage, userMediaTicketExt } from '@/layui-island/imageCompress'
import V2ApprovalImage from '@/v2/V2ApprovalImage.vue'

type Image = { key: string; name: string; preview: string }

defineProps<{ disabled: boolean }>()
const emit = defineEmits<{ change: [keys: string[]]; 'busy-change': [busy: boolean] }>()
const images = ref<Image[]>([])
const busy = ref(false)
const error = ref('')
let disposed = false

onUnmounted(() => {
  disposed = true
  for (const image of images.value) if (image.preview) URL.revokeObjectURL(image.preview)
})

function remove(image: Image): void {
  if (image.preview) URL.revokeObjectURL(image.preview)
  images.value = images.value.filter(({ key }) => key !== image.key)
  emit('change', images.value.map(({ key }) => key))
}

async function upload(files: readonly File[]): Promise<void> {
  if (!files.length || busy.value) return
  busy.value = true
  emit('busy-change', true)
  error.value = ''
  try {
    for (const original of files) {
      if (disposed) break
      if (images.value.length >= 20) {
        error.value = '最多上传 20 张截图'
        break
      }
      if (!/\.(jpe?g|png)$/i.test(original.name) && !['image/jpeg', 'image/png'].includes(original.type)) {
        error.value = '截图仅支持 JPG 和 PNG'
        continue
      }
      if (original.size > MAX_UPLOAD_BYTES) {
        error.value = '截图原图不能超过 18 MB'
        continue
      }
      try {
        const file = await compressImage(original)
        if (file.size > COMPRESSED_MAX_BYTES) {
          error.value = '截图压缩后不能超过 5 MB'
          continue
        }
        if (disposed) break
        const { key, upload_url } = await requestApprovalImageTicket(userMediaTicketExt(original, file))
        if (disposed) break
        await putFileToStorage(upload_url, file)
        if (disposed) break
        const preview = URL.createObjectURL?.(file) ?? ''
        images.value.push({ key, name: file.name, preview })
        emit('change', images.value.map((image) => image.key))
      } catch (cause) {
        error.value = cause instanceof Error ? cause.message : '上传失败，请重试'
      }
    }
  } finally {
    busy.value = false
    if (!disposed) emit('busy-change', false)
  }
}

function pasteFiles(event: ClipboardEvent): void {
  const files = Array.from(event.clipboardData?.items ?? [])
    .filter((item) => item.kind === 'file' && item.type.startsWith('image/'))
    .map((item) => item.getAsFile()).filter((file): file is File => file !== null)
  if (files.length) void upload(files)
  else error.value = '剪贴板中没有图片'
}
</script>

<template>
  <div class="approval-evidence">
    <div
      class="evidence-paste"
      :contenteditable="!disabled && !busy"
      role="textbox"
      aria-label="粘贴截图"
      @beforeinput.prevent
      @paste.prevent="!disabled && pasteFiles($event)"
    ></div>
    <p v-if="busy" role="status">正在上传截图…</p>
    <p v-if="error" class="evidence-error" role="alert">{{ error }}</p>
    <ul v-if="images.length" class="evidence-images">
      <li v-for="image in images" :key="image.key">
        <V2ApprovalImage v-if="image.preview" :src="image.preview" :alt="image.name" />
        <span v-else>{{ image.name }}</span>
        <button type="button" class="cv2-btn cv2-btn--quiet" :disabled="disabled || busy" :aria-label="`移除 ${image.name}`" @click="remove(image)">移除</button>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.approval-evidence { display: grid; gap: 5px; }
.evidence-paste { min-height: 34px; padding: 5px 8px; border: 1px dashed var(--tt2-line); border-radius: 4px; background: var(--tt2-btn-soft-bg); }
.evidence-paste:empty::before { content: '点击这里后，按 Ctrl+V 或 ⌘+V 粘贴截图'; color: var(--tt2-dim); }
.evidence-paste:focus-visible { outline: 2px solid var(--tt2-primary); }
.evidence-images { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 12px; }
.evidence-images li { --approval-thumb-width: 96px; --approval-thumb-height: 70px; display: grid; justify-items: center; }
.approval-evidence > p { margin: 0; }
.evidence-error { color: var(--tt2-st-error); }
</style>
