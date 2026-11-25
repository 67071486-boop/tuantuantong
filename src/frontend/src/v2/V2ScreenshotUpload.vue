<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { claimScreenshot, fetchScreenshots, requestScreenshotTicket } from '@/api/screenshots'
import { putFileToStorage } from '@/layui-island/directUpload'
import { COMPRESSED_MAX_BYTES, MAX_UPLOAD_BYTES, compressImage } from '@/layui-island/imageCompress'

const props = defineProps<{ orderNo: string }>()
const emit = defineEmits<{ 'busy-change': [busy: boolean] }>()
type Pending = { id: number; name: string; size: number; type: number; url: string }
const rows = ref<Pending[]>([])
const imageType = ref(0)
const remark = ref('')
const busy = ref(false)
watch(busy, (value) => emit('busy-change', value), { flush: 'sync' })
const message = ref('')
const preview = ref('')
const pasteMenu = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
let uploadedCount = 0
let disposed = false
let loadCount: Promise<void> = Promise.resolve()

onMounted(() => {
  loadCount = fetchScreenshots(props.orderNo).then((result) => {
    uploadedCount = result.items.filter((item) => item.img_type === 0).length
  }).catch(() => {})
})
onUnmounted(() => {
  disposed = true
  rows.value.forEach((row) => release(row))
})
function release(row: Pending): void {
  if (row.url) URL.revokeObjectURL?.(row.url)
}
function errorText(error: unknown): string {
  return error instanceof Error && error.message ? error.message : '上传失败，请稍后重试'
}
function extOf(name: string): string {
  return name.includes('.') ? (name.split('.').pop() ?? '').toLowerCase() : ''
}
async function upload(files: File[]): Promise<void> {
  if (!files.length || busy.value) return
  busy.value = true
  message.value = ''
  const type = imageType.value
  try {
    await loadCount
    for (const original of files) {
      if (disposed) break
      const ext = extOf(original.name)
      if (ext && !['jpg', 'jpeg', 'png'].includes(ext)) {
        message.value = '只支持 jpg / jpeg / png'
        continue
      }
      if (original.size > MAX_UPLOAD_BYTES) {
        message.value = '已自动过滤18MB以上的图片'
        continue
      }
      if (type === 0 && uploadedCount + rows.value.filter((row) => row.type === 0).length >= 20) {
        message.value = '单个订单最多上传20张截图，请先删除后再上传'
        break
      }
      const file = await compressImage(original)
      if (disposed) break
      if (file.size > COMPRESSED_MAX_BYTES) {
        message.value = '已自动过滤5MB以上的图片'
        continue
      }
      try {
        const ticket = await requestScreenshotTicket(props.orderNo, extOf(file.name), type)
        if (disposed) break
        await putFileToStorage(ticket.upload_url, file)
        if (disposed) break
        let url = ''
        try { url = URL.createObjectURL(file) } catch { /* 预览不可用时仍可保存 */ }
        rows.value.push({ id: ticket.screenshot_id, name: file.name, size: file.size, type, url })
      } catch (error) {
        message.value = errorText(error)
      }
    }
  } finally {
    busy.value = false
    if (fileInput.value) fileInput.value.value = ''
  }
}
function chooseFiles(event: Event): void {
  void upload(Array.from((event.target as HTMLInputElement).files ?? []))
}
function dropFiles(event: DragEvent): void {
  void upload(Array.from(event.dataTransfer?.files ?? []))
}
function pasteFiles(event: ClipboardEvent): void {
  const files = Array.from(event.clipboardData?.items ?? [])
    .filter((item) => item.kind === 'file' && item.type.startsWith('image/'))
    .map((item) => item.getAsFile()).filter((file): file is File => file !== null)
  if (files.length) { event.preventDefault(); void upload(files) }
  else if (event.target instanceof Element && event.target.closest('.shot-paste')) message.value = '剪贴板中没有图片'
}
async function pasteFromMenu(): Promise<void> {
  pasteMenu.value = false
  if (!navigator.clipboard?.read) {
    message.value = '浏览器不支持读取剪贴板，请使用快捷键粘贴图片'
    return
  }
  try {
    const items = await navigator.clipboard.read()
    const files: File[] = []
    for (const item of items) {
      const type = item.types.find((value) => value === 'image/png' || value === 'image/jpeg')
      if (type) files.push(new File([await item.getType(type)], `粘贴图片.${type === 'image/png' ? 'png' : 'jpg'}`, { type }))
    }
    if (files.length) await upload(files)
    else message.value = '剪贴板中没有图片'
  } catch {
    message.value = '无法读取剪贴板，请允许浏览器访问或使用快捷键粘贴图片'
  }
}
function remove(row: Pending): void {
  release(row)
  rows.value = rows.value.filter((item) => item !== row)
  if (preview.value === row.url) preview.value = ''
}
async function save(): Promise<void> {
  if (busy.value) return
  if (!rows.value.length) { message.value = '请先上传图片再进行保存'; return }
  if (rows.value.filter((row) => row.type === 1).length > 1) {
    message.value = '群聊二维码仅限一张，再次上传会覆盖原二维码！'
    return
  }
  busy.value = true
  message.value = ''
  for (const row of [...rows.value]) {
    if (disposed) break
    try {
      await claimScreenshot(props.orderNo, { screenshot_id: row.id, original_name: row.name, remark: remark.value })
      if (disposed) break
      if (row.type === 0) uploadedCount += 1
      remove(row)
    } catch (error) { message.value = errorText(error) }
  }
  if (!message.value) message.value = '保存成功'
  busy.value = false
}
</script>

<template>
  <div class="shot-upload" @dragover.prevent @drop.prevent="dropFiles" @paste="pasteFiles">
    <p class="shot-note">请先上传图片，再点击提交保存。群聊二维码仅限一张，再次上传会覆盖原二维码。</p>
    <div class="shot-fields">
      <fieldset class="shot-types" :disabled="busy">
        <legend>图片类型</legend>
        <label><input v-model.number="imageType" type="radio" :value="0"> 订单截图</label>
        <label><input v-model.number="imageType" type="radio" :value="1"> 群聊二维码</label>
      </fieldset>
      <label class="shot-remark">图片备注 <input v-model="remark" type="text" maxlength="50" placeholder="可以给图片做个备注说明" :disabled="busy"></label>
    </div>
    <label class="shot-drop" :class="{ 'shot-disabled': busy }">
      <input ref="fileInput" type="file" accept="image/jpeg,image/png" multiple :disabled="busy" @change="chooseFiles">
      <span class="shot-plus">＋</span><span>将文件拖到此处，或<span class="shot-link">点击上传图片文件</span></span>
    </label>
    <div class="shot-paste-wrap" @contextmenu.prevent="pasteMenu = !busy">
      <div class="shot-paste" :contenteditable="!busy" role="textbox" aria-label="点击此处后，可以使用快捷键 Ctrl+V 直接粘贴图片上传" @beforeinput.prevent @paste.prevent @click="pasteMenu = false"></div>
      <button v-if="pasteMenu" type="button" class="shot-paste-menu" @click="pasteFromMenu">粘贴图片</button>
    </div>
    <button type="button" class="shot-save cv2-btn cv2-btn--submit" :disabled="busy" @click="save">{{ busy ? '处理中，请稍候…' : '提交保存' }}</button>
    <p v-if="message" class="shot-message" role="status">{{ message }}</p>
    <table v-if="rows.length" class="shot-table cv2-data-grid">
      <thead><tr><th>文件名称</th><th>大小</th><th>上传状态</th><th>操作</th></tr></thead>
      <tbody><tr v-for="row in rows" :key="row.id"><td>{{ row.name }}</td><td>{{ (row.size / 1024 / 1024).toFixed(2) }} MB</td><td>上传成功（等待保存）</td><td><button type="button" :disabled="!row.url || busy" @click="preview = row.url">浏览图片</button> <button type="button" :disabled="busy" @click="remove(row)">删除</button></td></tr></tbody>
    </table>
    <div v-if="preview" class="shot-preview" @click="preview = ''"><img :src="preview" alt="图片预览"><button type="button" aria-label="关闭预览">×</button></div>
    <p class="shot-note">全部上传完成前，请勿关闭本界面。上传完成后请再浏览一次订单截图以确保全部上传成功！</p>
  </div>
</template>

<style scoped>
.shot-upload { color: var(--tt2-ink); font-size: 14px; }
.shot-note { color: var(--tt2-dim); line-height: 1.6; margin: 0 0 16px; }
.shot-fields { display: flex; align-items: center; gap: 24px; margin-bottom: 16px; flex-wrap: wrap; }
.shot-types { border: 0; padding: 0; margin: 0; display: flex; gap: 16px; align-items: center; }
.shot-types legend { float: left; margin-right: 16px; font-weight: 600; }
.shot-types input { accent-color: var(--tt2-primary); }
.shot-remark { display: flex; align-items: center; gap: 12px; font-weight: 600; flex: 1; min-width: 250px; }
.shot-remark input { flex: 1; min-width: 0; border: 1px solid var(--tt2-filter-line); border-radius: 4px; padding: 9px 12px; font: inherit; font-weight: 400; }
.shot-drop { min-height: 150px; border: 2px dashed var(--tt2-line); border-radius: 4px; display: flex; align-items: center; justify-content: center; flex-direction: column; gap: 8px; cursor: pointer; background: var(--tt2-btn-soft-bg); }
.shot-drop input { position: absolute; width: 1px; height: 1px; opacity: 0; }
.shot-plus { color: var(--tt2-primary); font-size: 38px; line-height: 1; }
.shot-link { color: var(--tt2-primary-deep); }
.shot-disabled { opacity: .6; cursor: wait; }
.shot-paste-wrap { position: relative; margin-top: 12px; }
.shot-paste { min-height: 58px; display: flex; align-items: center; justify-content: center; border: 1px solid var(--tt2-filter-line); border-radius: 4px; color: var(--tt2-dim); cursor: text; }
.shot-paste-menu { position: absolute; top: 100%; left: 0; z-index: 1; padding: 8px 16px; border: 1px solid var(--tt2-line); background: var(--tt2-panel); color: var(--tt2-ink); cursor: pointer; }
.shot-paste:empty::before { content: attr(aria-label); pointer-events: none; }
.shot-paste:focus { outline: 2px solid var(--tt2-primary); outline-offset: 1px; }
.shot-save { width: 100%; min-height: 46px; margin: 12px 0; font-size: 16px; font-weight: 600; }
.shot-message { color: var(--tt2-primary-deep); margin: 4px 0 12px; }
.shot-table { border-collapse: collapse; width: 100%; margin: 8px 0 16px; table-layout: fixed; }
.shot-table th, .shot-table td { padding: 10px 8px; text-align: left; overflow-wrap: anywhere; }
.shot-table button { background: none; border: 0; color: var(--tt2-primary-deep); cursor: pointer; }
.shot-preview { position: fixed; inset: 0; z-index: 4000; background: color-mix(in srgb, var(--tt2-side) 80%, transparent); display: flex; justify-content: center; align-items: center; }
.shot-preview img { max-width: 85vw; max-height: 85vh; object-fit: contain; }
.shot-preview button { position: absolute; top: 20px; right: 25px; color: var(--tt2-panel); background: transparent; border: 0; font-size: 30px; cursor: pointer; }
</style>
