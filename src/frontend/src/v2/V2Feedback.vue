<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「问题反馈」v2（`/v2/feedback`；APP与小程序+问题反馈批 A 波，计划 §3）。旧页 =
 * island 自建页 `layui-island/pages/feedback.ts`（自有功能、无证据件）。一页两段：
 * 提交区（textarea + 截图两条输入路 = Ctrl+V 挂**整个提交区容器** + 按钮选文件；⛔ 拖拽不做；
 * 前置闸顺序承重 类型→体积→张数；上传走**串行队列**，后到批次排队不丢弃，失败必说出来）+
 * 列表区（列头随身份变；管理员判据 = `is_tenant_admin`，⛔ 不是 is_account_admin；
 * 处置单按钮按行状态换文案、备注预填、PATCH 两键都给；反馈是审计痕迹，⛔ 无删除）。
 *
 * 口径照旧页：状态色钉死 pending → `cv2-st--paused`（琥珀）/ handled → `cv2-st--settled`
 * （深绿实底；⛔ 别按名字选 cv2-st--pending——那是紫色）；截图点击预览 = cv2-dialog 自建弹窗
 * （照 V2SettleDialog 先例，⛔ 无 layer.photos），缩略 44px 高；非管理员列表是服务端 queryset
 * 收窄、PATCH 403 如实弹后端文案（前端判据只是体验层）。权限门 fail-closed（session.canNode，
 * ⛔ 不 import island perms）。上传队列 / 前置闸 / MIME 兜底逐段照抄旧页算法（⛔ 凭记忆重写）。
 */
import { computed, onMounted, ref } from 'vue'
import {
  createFeedback,
  handleFeedback,
  issueTicket,
  listFeedback,
  type FeedbackRow,
  type FeedbackStatus,
} from '@/api/feedback'
import { putFileToStorage } from '@/layui-island/directUpload'
import { useSessionStore } from '@/stores/session'

const session = useSessionStore()

/* ── 逐字文案与常量（出处 = island 页常量区与 MESSAGES；本地重写不引模块）────────── */

const NO_ACCESS_TEXT = '无权限查看问题反馈'
const NO_CONTENT_TEXT = '请描述你遇到的问题'
const SUBMITTED_TEXT = '反馈已提交，感谢反馈'
const FAILED_TEXT = '操作失败'
const EMPTY_TEXT = '还没有反馈记录'
const HANDLED_TEXT = '已标记为已处理'
const REOPENED_TEXT = '已改回未处理'
/** 类型这句必须与后端拒收文案逐字相同（词序 jpg / jpeg / png、无句号，照旧页）。 */
const IMAGE_BAD_TYPE_TEXT = '只支持 jpg / jpeg / png'
const MAX_IMAGE_BYTES = 10 * 1024 * 1024
const MAX_IMAGES = 3
const IMAGE_TOO_LARGE_TEXT = `单张截图不能超过 ${MAX_IMAGE_BYTES / (1024 * 1024)} MB`
const TOO_MANY_IMAGES_TEXT = `一条反馈最多 ${MAX_IMAGES} 张截图`
const ALLOWED_EXTS = ['png', 'jpg', 'jpeg'] as const
/** MIME 兜底表（只在「名字里没有扩展名」时用）：`image/jpeg` → `jpeg`，照旧页只留一种写法。 */
const MIME_EXT: Readonly<Record<string, string>> = {
  'image/png': 'png',
  'image/jpeg': 'jpeg',
}
/** 截图区灰字说明（照旧页 buildHtml 逐字；数字不在这里二次维护）。 */
const HINT_TEXT =
  `可以直接把截图粘贴到这个区域（截图工具截完按 Ctrl+V）；最多 ${MAX_IMAGES} 张，` +
  `单张不超过 ${MAX_IMAGE_BYTES / (1024 * 1024)} MB，只支持 png / jpg。`

/** 状态 → 炭火签档（钉死映射，键 = 状态原值 ⛔ 不是服务端中文 label）。 */
const STATUS_CLASS: Record<FeedbackStatus, string> = {
  pending: 'cv2-st--paused',
  handled: 'cv2-st--settled',
}

function errText(e: unknown): string {
  return e instanceof Error && e.message ? e.message : FAILED_TEXT
}

/** ISO 8601 → `2026-10-08 12:00:00`（只截到秒，照旧页 formatTime）。 */
function formatTime(value: string): string {
  return value.replace('T', ' ').slice(0, 19)
}

/* ── 提示（页面内自绘 toast，同 V2Notices；info 1500ms / error 6000ms）─────────── */

const toast = ref<{ text: string; kind: 'info' | 'error' } | null>(null)
let toastTimer: ReturnType<typeof setTimeout> | null = null

function showToast(text: string, kind: 'info' | 'error' = 'info'): void {
  if (toastTimer !== null) clearTimeout(toastTimer)
  toast.value = { text, kind }
  toastTimer = setTimeout(() => {
    toast.value = null
    toastTimer = null
  }, kind === 'error' ? 6000 : 1500)
}

/* ── 权限门与管理员判据（两条判据各管各的，不互相替代——照旧页口径）────────────── */

const canView = computed(() => session.canNode(['home.feedback']))
/** 管理员判据 = `/api/auth/me` 的 `is_tenant_admin`（⛔ 不是 is_account_admin）。 */
const isAdmin = computed(() => session.user?.is_tenant_admin === true)

/* ── 待提交截图（已 PUT 成功、拿到 key 的那些）与前置闸（照旧页纯函数逐段重写）──── */

type PendingImage = { key: string; preview_url: string; name: string }

const pending = ref<PendingImage[]>([])

/** 从文件名取扩展名（去点、小写；推不出回空串，照旧页 extOf）。 */
function extOf(name: string): string {
  const dot = name.lastIndexOf('.')
  if (dot <= 0 || dot === name.length - 1) return ''
  return name.slice(dot + 1).toLowerCase()
}

/** 发给签发端点的扩展名：名字 → MIME 兜底 → null（照旧页 resolveExt；粘贴图常无文件名）。 */
function resolveExt(file: File): string | null {
  const byName = extOf(file.name)
  if ((ALLOWED_EXTS as readonly string[]).includes(byName)) return byName
  return MIME_EXT[(file.type ?? '').toLowerCase()] ?? null
}

/** 一次「选到文件」的前置闸（纯函数）：①类型 → ②体积 → ③张数，顺序承重照旧页。 */
function rejectReason(file: File, pendingCount: number): string | null {
  if (resolveExt(file) === null) return IMAGE_BAD_TYPE_TEXT
  if (file.size > MAX_IMAGE_BYTES) return IMAGE_TOO_LARGE_TEXT
  if (pendingCount >= MAX_IMAGES) return TOO_MANY_IMAGES_TEXT
  return null
}

/** 本机预览 URL（拿不到回空串——那一格退化成只显示文件名）。 */
function previewUrlOf(file: File): string {
  try {
    return typeof URL.createObjectURL === 'function' ? URL.createObjectURL(file) : ''
  } catch {
    return ''
  }
}

/** 从 paste 事件取图片文件：非 file 项与非 image/ 项跳过、无图片回空数组（照旧页）。 */
function imageFilesOf(
  items: ArrayLike<{ kind?: string; type?: string; getAsFile?: () => File | null }>,
): File[] {
  const out: File[] = []
  for (let i = 0; i < items.length; i += 1) {
    const item = items[i]
    if (!item || item.kind !== 'file' || !(item.type ?? '').startsWith('image/')) continue
    const file = item.getAsFile?.()
    if (file) out.push(file)
  }
  return out
}

function removePending(index: number): void {
  pending.value = pending.value.filter((_, i) => i !== index)
}

/** 收一张已签发地址的图：ticket → PUT，**成功之后**才挂进待提交列表（⛔ 先挂后传）。 */
async function uploadOne(file: File): Promise<void> {
  const ext = resolveExt(file)
  if (ext === null) {
    showToast(IMAGE_BAD_TYPE_TEXT)
    return
  }
  const ticket = await issueTicket(ext)
  await putFileToStorage(ticket.upload_url, file)
  pending.value = [
    ...pending.value,
    { key: ticket.key, preview_url: previewUrlOf(file), name: file.name },
  ]
}

/** 逐张判闸（超限当场说哪条，其余照常传）+ 串行上传（顺序 = 屏幕顺序，照旧页）。 */
async function acceptFilesInner(files: readonly File[]): Promise<void> {
  for (const file of files) {
    const reason = rejectReason(file, pending.value.length)
    if (reason !== null) {
      showToast(reason)
      continue
    }
    try {
      await uploadOne(file)
    } catch (e) {
      showToast(errText(e), 'error')
    }
  }
}

/**
 * 上传的**串行队列**（两条入口一条实现；照旧页 F1 修正后的形状）：
 * 后到批次串到前一批之后（来得晚 ≠ 被丢掉），`queue` 自吞 rejection（一次失败不毒化后续）。
 */
let queue: Promise<void> = Promise.resolve()
function acceptFiles(files: readonly File[]): Promise<void> {
  const run = (): Promise<void> => acceptFilesInner(files)
  const next = queue.then(run, run)
  queue = next.then(
    () => undefined,
    () => undefined,
  )
  return next
}

const fileInput = ref<HTMLInputElement | null>(null)

function onPick(): void {
  fileInput.value?.click()
}

function onFileChange(): void {
  const input = fileInput.value
  if (!input) return
  const files = Array.from(input.files ?? [])
  // 清空 value：同一文件选第二次也要触发 change（浏览器按值判「没变」，照旧页）。
  input.value = ''
  void acceptFiles(files)
}

/** Ctrl+V 挂在**整个提交区容器**（模板 @paste）：无图片项静默不管、不 preventDefault。 */
function onPaste(event: ClipboardEvent | Event): void {
  const clipboard = (event as ClipboardEvent).clipboardData
  const files = clipboard?.items ? imageFilesOf(clipboard.items) : []
  if (files.length === 0) return
  event.preventDefault()
  void acceptFiles(files)
}

/* ── 提交（成功先清空表单与截图、再刷新列表——照旧页顺序）─────────────────────── */

const content = ref('')
const submitting = ref(false)

async function submit(): Promise<void> {
  if (submitting.value) return
  const trimmed = content.value.trim()
  if (trimmed === '') {
    showToast(NO_CONTENT_TEXT)
    return
  }
  submitting.value = true
  try {
    await createFeedback({ content: trimmed, image_keys: pending.value.map((img) => img.key) })
    content.value = ''
    pending.value = []
    showToast(SUBMITTED_TEXT)
    try {
      await reload()
    } catch (e) {
      showToast(errText(e), 'error')
    }
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    submitting.value = false
  }
}

/* ── 列表（无分页无搜索无删除；表头与行用同一个 isAdmin，照旧页）────────────────── */

const rows = ref<FeedbackRow[]>([])
const loading = ref(false)
let reloadSeq = 0

async function reload(): Promise<void> {
  const seq = ++reloadSeq
  loading.value = true
  try {
    const payload = await listFeedback()
    if (seq !== reloadSeq) return
    rows.value = payload.items
  } finally {
    if (seq === reloadSeq) loading.value = false
  }
}

/** 表头与行同一份判据：管理员多「提交人」「处理」两列（照旧页 listHeadHtml）。 */
const listHeads = computed(() => {
  const heads = ['提交时间', '反馈内容', '状态', '管理员备注', '截图']
  if (isAdmin.value) {
    heads.splice(1, 0, '提交人')
    heads.push('处理')
  }
  return heads
})

/* ── 管理员处置（单按钮按行状态换文案；PATCH 两键都给——清空备注 = 清空，照旧页）── */

const actingId = ref<number | null>(null)

async function act(row: FeedbackRow): Promise<void> {
  if (actingId.value !== null) return
  const nextStatus: FeedbackStatus = row.status === 'handled' ? 'pending' : 'handled'
  actingId.value = row.id
  try {
    await handleFeedback(row.id, { status: nextStatus, admin_note: row.admin_note })
    showToast(nextStatus === 'handled' ? HANDLED_TEXT : REOPENED_TEXT)
    await reload()
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    actingId.value = null
  }
}

/* ── 截图点击预览（cv2-dialog 自建弹窗，照 V2SettleDialog 先例）────────────────── */

const previewShot = ref<{ url: string; key: string } | null>(null)

onMounted(() => {
  if (!canView.value) {
    showToast(NO_ACCESS_TEXT)
    return
  }
  void reload().catch((e: unknown) => showToast(errText(e), 'error'))
})
</script>

<template>
  <div class="cv2-page" data-v2-page="feedback">
    <!-- 无权限：不画任何一块、不发业务请求（照旧页 fail-closed 口径） -->
    <section v-if="!canView" class="cv2-panel" data-v2="no-access">
      <p class="cv2-dlg-text">{{ NO_ACCESS_TEXT }}</p>
    </section>

    <template v-else>
      <!-- 提交区（所有人可见）：@paste 挂在本容器（⛔ 不是只挂 textarea） -->
      <section class="cv2-panel fb-form" data-v2="submit-panel" @paste="onPaste">
        <div class="cv2-field">
          <span class="cv2-field-label">问题描述</span>
          <el-input
            v-model="content"
            type="textarea"
            :rows="4"
            :maxlength="2000"
            placeholder="请描述你遇到的问题"
            data-v2="content-input"
          />
        </div>
        <div class="cv2-field">
          <span class="cv2-field-label">截图</span>
          <div class="fb-shotbox">
            <div class="fb-pending" data-v2="pending-box">
              <span v-for="(img, i) in pending" :key="`${img.key}-${i}`" class="fb-pending-item">
                <img v-if="img.preview_url" :src="img.preview_url" alt="待提交截图" class="fb-thumb" />
                <span class="fb-pending-name">{{ img.name }}</span>
                <a href="#" class="fb-remove" :data-v2-remove="i" @click.prevent="removePending(i)">删除</a>
              </span>
            </div>
            <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--sm cv2-btn--commit" data-v2="pick" @click="onPick">选择图片</button>
            <input ref="fileInput" type="file" accept="image/png,image/jpeg" multiple class="fb-file-input" data-v2="file-input" @change="onFileChange" />
            <p class="fb-hint" data-v2="hint">{{ HINT_TEXT }}</p>
          </div>
        </div>
        <div class="fb-actions">
          <button type="button" class="cv2-btn cv2-btn--submit cv2-btn--commit" data-v2="submit" :disabled="submitting" @click="void submit()">提交</button>
        </div>
      </section>

      <!-- 列表区：列头随身份变；已处理行正文淡化、状态签不变；⛔ 无删除 -->
      <section class="cv2-panel cv2-table-panel" aria-label="反馈记录">
        <h3 class="cv2-panel-title" data-v2="list-title">反馈记录</h3>
        <table class="cv2-table">
          <thead>
            <tr>
              <th v-for="head in listHeads" :key="head">{{ head }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="row.id" class="cv2-row" :data-v2-row="row.id">
              <td class="cv2-td-time">{{ formatTime(row.created_at) }}</td>
              <td v-if="isAdmin" :data-v2-by="row.id">{{ row.created_by_name }}</td>
              <td :class="{ 'fb-dim': row.status === 'handled' }" :data-v2-content="row.id">{{ row.content }}</td>
              <td>
                <span class="cv2-st" :class="STATUS_CLASS[row.status]" :data-v2-status="row.id" :data-status="row.status">
                  <i class="cv2-dot" aria-hidden="true"></i>{{ row.status_label }}
                </span>
              </td>
              <td :class="{ 'fb-dim': row.status === 'handled' }" :data-v2-note="row.id">
                <template v-if="row.admin_note !== ''">{{ row.admin_note }}</template>
                <template v-else><span class="fb-none">—</span></template>
              </td>
              <td class="fb-shots" :data-v2-shots="row.id">
                <template v-if="row.images.length > 0">
                  <img
                    v-for="img in row.images"
                    :key="img.key"
                    :src="img.url"
                    alt="反馈截图"
                    class="fb-thumb fb-thumb--click"
                    :data-v2-shot="img.key"
                    @click="previewShot = { url: img.url, key: img.key }"
                  />
                </template>
                <template v-else><span class="fb-none">—</span></template>
              </td>
              <td v-if="isAdmin" class="fb-handle-cell" :data-v2-actions="row.id">
                <input
                  v-model="row.admin_note"
                  type="text"
                  maxlength="200"
                  placeholder="处理备注（可不填）"
                  class="cv2-input fb-note-input"
                  :data-v2-note-input="row.id"
                />
                <button
                  type="button"
                  class="cv2-btn cv2-btn--sm"
                  :class="row.status === 'handled' ? 'cv2-btn--tool' : 'cv2-btn--primary'"
                  :data-v2-act="row.status === 'handled' ? 'reopen' : 'handle'"
                  :disabled="actingId === row.id"
                  @click="void act(row)"
                >
                  {{ row.status === 'handled' ? '改回未处理' : '标记已处理' }}
                </button>
              </td>
            </tr>
            <tr v-if="rows.length === 0">
              <td :colspan="isAdmin ? 7 : 5" class="cv2-empty" data-v2="empty">
                <template v-if="loading">正在加载…</template>
                <template v-else>{{ EMPTY_TEXT }}</template>
              </td>
            </tr>
          </tbody>
        </table>
      </section>
    </template>

    <!-- 截图预览（放大；照「可点击预览不是下载」口径） -->
    <el-dialog
      :model-value="previewShot !== null"
      title="反馈截图"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-preview"
      @update:model-value="previewShot = null"
    >
      <div v-if="previewShot !== null" class="fb-preview" data-v2="preview-box">
        <img :src="previewShot.url" alt="反馈截图" data-v2="preview-img" />
      </div>
    </el-dialog>

    <!-- 炭火 toast（居中深条，同 V2Notices） -->
    <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
      {{ toast.text }}
    </div>
  </div>
</template>

<style scoped>
/* 本页形状收在这里；色一律走 carbon.css 的 token（⛔ 不写裸色值）。 */

/* 缩略：44px 高上限（照旧页口径）；可点的那格给放大光标。 */
.fb-thumb {
  max-height: 44px;
  max-width: 80px;
  margin-right: 4px;
  vertical-align: middle;
}

.fb-thumb--click {
  cursor: zoom-in;
}

/* 提交区截图块：缩略行 + 选图钮 + 灰字说明纵排。 */
.fb-shotbox {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
}

.fb-pending {
  line-height: 24px;
}

.fb-pending-item {
  margin-right: 8px;
  white-space: nowrap;
}

.fb-pending-name {
  margin-left: 2px;
  color: var(--tt2-dim);
  font-size: 12px;
}

/* 「删除」小链接：红字 token（旧页 #FF5722 的炭火映射）。 */
.fb-remove {
  margin-left: 4px;
  color: var(--tt2-st-error);
  font-size: 12px;
  text-decoration: none;
}

.fb-remove:hover {
  text-decoration: underline;
}

.fb-file-input {
  display: none;
}

.fb-hint {
  margin: 0;
  color: var(--tt2-dim);
  font-size: 12px;
}

.fb-actions {
  margin-top: 4px;
}

/* 已处理行的正文淡化（内容 / 备注两格；⛔ 不动状态签与按钮）。 */
.fb-dim {
  color: var(--tt2-dim);
}

.fb-none {
  color: var(--tt2-dim);
}

.fb-shots {
  white-space: nowrap;
}

/* 处置格：备注输入框 + 状态按钮同排。 */
.fb-handle-cell {
  white-space: nowrap;
}

.fb-note-input {
  width: 180px;
  margin-right: 6px;
  display: inline-block;
  vertical-align: middle;
}

/* 预览：弹窗内放大到可读（尺寸非色值）。 */
.fb-preview {
  text-align: center;
}

.fb-preview img {
  max-width: 100%;
  max-height: 480px;
}

/* 全局提示 = 对标 layer.msg 居中深条（V2Notices 同款）。 */
.cv2-page :deep(.cv2-toast) {
  top: 45%;
  left: 50%;
  transform: translate(-50%, -50%);
  z-index: 3000;
  max-width: 56vw;
  padding: 12px 22px;
  border: none;
  border-radius: 6px;
  background: var(--tt2-toast-bg);
  color: var(--tt2-panel);
  font-size: 14px;
  text-align: center;
}

.cv2-page :deep(.cv2-toast--error) {
  color: var(--tt2-panel);
}
</style>
