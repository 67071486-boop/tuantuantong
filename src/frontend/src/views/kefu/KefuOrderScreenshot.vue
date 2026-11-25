<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { f7Button as F7Button } from 'framework7-vue'
import { fetchScreenshots, type ScreenshotItem } from '@/api/screenshots'
import { COMPRESSED_MAX_BYTES, MAX_UPLOAD_BYTES, compressImage } from '@/layui-island/imageCompress'

const MAX_ORDER_SCREENSHOTS = 20
const ORDER_SCREENSHOT = 0
const LIMIT_MESSAGE = '每单最多 20 张截图'

const files = defineModel<File[]>({ required: true })
const busy = defineModel<boolean>('busy', { default: false })
const props = defineProps<{ disabled?: boolean; orderNo?: string; canViewExisting?: boolean }>()

const pasteInput = ref<HTMLElement | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const saved = shallowRef<ScreenshotItem[]>([])
const loadingSaved = ref(false)
const savedLoadError = ref('')
const previews = shallowRef<{ file: File; url: string }[]>([])
const error = ref('')
const remaining = computed(() => MAX_ORDER_SCREENSHOTS - saved.value.length - files.value.length)
const inactive = computed(() => props.disabled || busy.value)
const pickerDisabled = computed(() => inactive.value || loadingSaved.value || remaining.value <= 0)
const prompt = computed(() => {
  if (busy.value) return '处理中…'
  if (remaining.value <= 0) return '已达截图上限'
  return 'Ctrl+V 粘贴'
})
let disposed = false

watch(files, (selected) => {
  const previous = new Map(previews.value.map((preview) => [preview.file, preview.url]))
  previews.value = selected.map((file) => {
    const url = previous.get(file) ?? URL.createObjectURL(file)
    previous.delete(file)
    return { file, url }
  })
  previous.forEach((url) => URL.revokeObjectURL(url))
}, { immediate: true })

watch(() => [props.orderNo, props.canViewExisting] as const, async ([orderNo, canView], _, onCleanup) => {
  saved.value = []
  savedLoadError.value = ''
  loadingSaved.value = Boolean(orderNo && canView)
  if (!orderNo || !canView) return
  let current = true
  onCleanup(() => { current = false })
  try {
    const result = await fetchScreenshots(orderNo)
    if (current) saved.value = result.items.filter((item) => item.img_type === ORDER_SCREENSHOT)
  } catch {
    if (current) savedLoadError.value = '已保存截图加载失败'
  } finally {
    if (current) loadingSaved.value = false
  }
}, { immediate: true })

onBeforeUnmount(() => {
  disposed = true
  previews.value.forEach(({ url }) => URL.revokeObjectURL(url))
  if (busy.value) busy.value = false
})

type ImageType = 'image/jpeg' | 'image/png'
const PNG_HEADER = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]

function imageType(file: File): Promise<ImageType | null> {
  return new Promise((resolve) => {
    const reader = new FileReader()
    reader.onerror = () => resolve(null)
    reader.onload = () => {
      const head = new Uint8Array(reader.result as ArrayBuffer)
      if (head[0] === 0xff && head[1] === 0xd8 && head[2] === 0xff) resolve('image/jpeg')
      else resolve(PNG_HEADER.every((byte, index) => head[index] === byte) ? 'image/png' : null)
    }
    reader.readAsArrayBuffer(file.slice(0, PNG_HEADER.length))
  })
}

function withImageType(file: File, type: ImageType): File {
  const extension = type === 'image/png' ? 'png' : 'jpg'
  const suffix = type === 'image/png' ? /\.png$/i : /\.jpe?g$/i
  if (file.type === type && suffix.test(file.name)) return file
  const name = file.name.replace(/\.[^.]+$/, '') || '订单截图'
  return new File([file], `${name}.${extension}`, { type, lastModified: file.lastModified })
}

async function validateImage(file: File): Promise<void> {
  const url = URL.createObjectURL(file)
  try {
    const image = new Image()
    image.src = url
    await image.decode()
  } finally {
    URL.revokeObjectURL(url)
  }
}

async function prepareScreenshot(original: File): Promise<File> {
  if (original.size > MAX_UPLOAD_BYTES) throw new Error('截图原图不能超过 18 MB')
  const type = await imageType(original)
  if (!type) throw new Error('截图仅支持 JPG 和 PNG')
  const prepared = await compressImage(withImageType(original, type)).catch(() => {
    throw new Error('无法读取截图，请重新选择')
  })
  if (prepared.size > COMPRESSED_MAX_BYTES) throw new Error('截图压缩后不能超过 5 MB')
  const selected = withImageType(prepared, prepared.type === 'image/jpeg' ? 'image/jpeg' : type)
  await validateImage(selected).catch(() => { throw new Error('无法读取截图，请重新选择') })
  return selected
}

async function selectFiles(originals: File[]): Promise<void> {
  if (pickerDisabled.value || originals.length === 0) return
  error.value = ''
  busy.value = true
  const selected: File[] = []
  try {
    for (const original of originals) {
      if (disposed) return
      if (selected.length >= remaining.value) {
        error.value = LIMIT_MESSAGE
        break
      }
      try {
        const file = await prepareScreenshot(original)
        if (!files.value.includes(file) && !selected.includes(file)) selected.push(file)
      } catch (err) {
        if (!disposed) error.value = err instanceof Error ? err.message : '无法读取截图，请重新选择'
      }
    }
    if (!disposed && selected.length > 0) files.value = [...files.value, ...selected]
  } finally {
    if (!disposed) busy.value = false
  }
}

function chooseFiles(event: Event): void {
  const input = event.target as HTMLInputElement
  const selected = Array.from(input.files ?? [])
  input.value = ''
  void selectFiles(selected)
}

function pasteFiles(event: ClipboardEvent): void {
  if (pickerDisabled.value) return
  const data = event.clipboardData
  const images = Array.from(data?.items ?? [])
    .filter((entry) => entry.kind === 'file' && entry.type.startsWith('image/'))
    .flatMap((entry) => entry.getAsFile() ?? [])
  const selected = images.length > 0 ? images : Array.from(data?.files ?? [])
  if (selected.length > 0) void selectFiles(selected)
  else error.value = '剪贴板中没有图片'
}

async function removeFile(file: File): Promise<void> {
  if (inactive.value) return
  files.value = files.value.filter((entry) => entry !== file)
  error.value = ''
  await nextTick()
  pasteInput.value?.focus()
}
</script>

<template>
  <div class="kefu-field kefu-screenshot-field">
    <span class="kefu-label">订单截图</span>
    <div class="kefu-shot-field-content" :aria-busy="busy || loadingSaved || undefined">
      <div class="kefu-shot-row">
        <div class="kefu-shot-control">
          <div
            ref="pasteInput"
            class="kefu-shot-paste"
            :contenteditable="!pickerDisabled"
            :tabindex="pickerDisabled ? -1 : 0"
            :aria-disabled="pickerDisabled || undefined"
            role="textbox"
            aria-label="粘贴订单截图"
            :data-prompt="prompt"
            spellcheck="false"
            @beforeinput.prevent
            @dragover.prevent
            @drop.prevent
            @paste.prevent="pasteFiles"
          ></div>
          <F7Button
            type="button"
            class="kefu-shot-select"
            aria-label="选择订单截图"
            small
            :disabled.prop="pickerDisabled"
            @click="fileInput?.click()"
          >选图</F7Button>
        </div>
        <input ref="fileInput" type="file" accept="image/png,image/jpeg,.png,.jpg,.jpeg" multiple :disabled="pickerDisabled" hidden @change="chooseFiles" />
      </div>
      <ul v-if="saved.length || previews.length" class="kefu-shot-previews" aria-label="订单截图预览">
        <li v-for="image in saved" :key="image.id" class="kefu-shot-image">
          <img :src="image.thumbnail_url || image.url" :title="image.original_name" alt="已保存订单截图" class="kefu-shot-thumb" />
        </li>
        <li v-for="preview in previews" :key="preview.url" class="kefu-shot-image">
          <img :src="preview.url" :title="preview.file.name" alt="订单截图预览" class="kefu-shot-thumb" />
          <F7Button
            type="button"
            class="kefu-shot-remove"
            aria-label="移除订单截图"
            :title="`移除${preview.file.name}`"
            small
            round
            tonal
            :disabled.prop="inactive"
            @click="removeFile(preview.file)"
          >×</F7Button>
        </li>
      </ul>
      <p v-if="error || savedLoadError" class="kefu-shot-error" role="status">{{ error || savedLoadError }}</p>
    </div>
  </div>
</template>

<style scoped>
.kefu-screenshot-field > .kefu-label { align-self: start; line-height: 30px; }
.kefu-shot-field-content { min-width: 0; }
.kefu-shot-row { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }
.kefu-shot-control {
  display: flex;
  align-items: stretch;
  width: 142px;
  max-width: 100%;
  min-height: 30px;
  border-radius: var(--f7-button-border-radius);
  background: color-mix(in srgb, var(--f7-theme-color) 12%, var(--f7-list-strong-bg-color));
}
.kefu-shot-paste {
  display: flex;
  align-items: center;
  flex: 1;
  min-width: 0;
  padding: 4px 6px;
  border-start-start-radius: inherit;
  border-end-start-radius: inherit;
  color: var(--f7-theme-color);
  cursor: text;
  font-size: 12px;
}
.kefu-shot-paste::before {
  content: attr(data-prompt);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.kefu-shot-paste[aria-disabled="true"] { cursor: default; }
.kefu-shot-select {
  flex: none;
  width: auto;
  min-width: 0;
  align-self: center;
}
.kefu-shot-previews {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin: 5px 0;
  padding: 0;
  list-style: none;
}
.kefu-shot-image { display: flex; align-items: center; gap: 2px; }
.kefu-shot-thumb {
  display: block;
  width: 40px;
  height: 40px;
  object-fit: cover;
  border-radius: 5px;
}
.kefu-shot-remove { width: 24px; min-width: 24px; padding-inline: 0; }
.kefu-shot-error { margin: 3px 0 0; color: var(--f7-input-error-text-color); font-size: 12px; }
</style>
