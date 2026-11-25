<script setup lang="ts">
import { f7Block as F7Block, f7Button as F7Button, f7Icon as F7Icon, f7Preloader as F7Preloader } from 'framework7-vue'
import MobilePageBar from '../components/MobilePageBar.vue'
import { SCREENSHOT_RETURN_ORDER, useWorkerBack } from '../router/useWorkerBack'
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Toast, confirm } from '../framework7Ui'
import MobilePhotoBrowser from '../components/MobilePhotoBrowser.vue'
import { useSessionStore } from '@/stores/session'
import {
  claimScreenshot, deleteScreenshot, fetchScreenshots,
  requestScreenshotTicket, type ScreenshotItem,
} from '@/api/screenshots'
import { HttpError } from '@/api/errors'
import { claimBrowserScreenshot, requestBrowserScreenshotTicket, startScreenshotBrowser } from '@/api/screenshotBrowser'
import { isWxPusherAndroid } from '../utils/wxpusherOrderLink'
import { COMPRESSED_MAX_BYTES, MAX_UPLOAD_BYTES, compressImage } from '@/layui-island/imageCompress'
import { putFileToStorage } from '@/layui-island/directUpload'
import { formatDateTime } from '../utils/datetime'
import { isOrderNoShape } from '../utils/orderNoShape'
import {
  ALL_TOO_LARGE_TEXT, BAD_EXT_TEXT, DELETE_CONFIRM_TEXT, EMPTY_TEXT, FALLBACK_ERROR_TEXT,
  LIMIT_TEXT, overCompressedText, overOriginalText,
} from '../texts/screenshots'

/**
 * 订单截图（手机端 `/m/orders/:orderNo/screenshots`，spec §3.1）。取数 = 桌面同一套会话端点（`api/screenshots.ts`，⛔ 不复制封装）；列表只渲染**进度截图**（`img_type=0`）。路由门 = 桌面两颗入口按钮的同一对节点（`canNode` 任一即进），页内再按**各自动作的节点**收窄：上传区 `order.upload_screenshot`、删除 `order.screenshot_manage`——没权限的那块**不渲染**（⛔ 不置灰）。错误态口径对齐 M1 详情页：403 给「返回」，其余给「重试」。
 * ⚠️ 与桌面的两处登记差异：① 桌面管理弹层里的删除按钮**不按节点渲染**（无 `order.screenshot_manage` 的账号点它会吃后端 403），手机端三合一页没有那道弹层门可借 ⇒ 按 D-0154「点了必 403 的控件不渲染」补上页内门；② **上传区不随列表取数收窄**：列表 GET 的门是 `order.view_screenshot`，仅有上传节点的账号进页列表会吃 403 ⇒ 上传区只看形状闸 × `canUpload`、列表失败仍渲染，此时 20 张预检按已知快照（可能 0 张）算**偏乐观**——后端在签发与认领两处拦（权威），与桌面「取列表失败按 0 预检」同代价。
 * 上传照桌面线的压缩与上限口径（提示文案逐字取桌面，见 `m/texts/screenshots.ts`）：相册多选 + 拍照 + 粘贴三路进同一个 `handleFiles` → 扩展名早挡（只挡带扩展名的，无名可判的放过去由签发端点兜底）→ 原图 18MiB → `compressImage`（四档画质协议**直接复用不复制**）→ 压缩后 5MiB → **每单 20 张预检** → 逐张三跳 `ticket ⇒ PUT ⇒ claim`。⚠️ 桌面是「先排队、保存时认领」两步，手机端自有设计**即传即认领**（`remark` 空）：少一步保存、不留 `pending` 孤儿行；代价：传错只能真删（要 `order.screenshot_manage` 节点，无该节点的纯上传账号传错图只能找管理员删）。删除：确认文案逐字取桌面，确认后逐张 `DELETE` + 重取列表（成功失败都重取——列表就是真状态），失败 toast 后端 `msg` 原样（⛔ 不自编）。
 */

/* ── 形状闸（与详情页同一道：不符 → 404 态页，零请求） ── */
const { browserUpload = false } = defineProps<{ browserUpload?: boolean }>()
const emit = defineEmits<{ browserExpired: [message: string] }>()
const route = useRoute()
const router = useRouter()
const session = useSessionStore()

const orderNo = computed(() => {
  const raw = route.params.orderNo
  return typeof raw === 'string' ? raw : ''
})
const shapeOk = computed(() => isOrderNoShape(orderNo.value))

/* ── 权限（页内两块；取不到 me 按无权限，先例 D-0154） ── */
const canUpload = computed(() => browserUpload || session.canNode(['order.upload_screenshot']))
const canManage = computed(() => !browserUpload && session.canNode(['order.screenshot_manage']))

const openInBrowser = !browserUpload && isWxPusherAndroid()
const preparingBrowser = ref(false)

async function prepareBrowserUpload(): Promise<void> {
  if (preparingBrowser.value) return
  preparingBrowser.value = true
  try {
    const targetOrder = orderNo.value
    const { challenge } = await startScreenshotBrowser(targetOrder)
    if (route.name !== 'm-order-screenshots' || targetOrder !== orderNo.value) return
    await router.push({
      name: 'm-order-screenshot-browser',
      params: { orderNo: targetOrder },
      hash: `#${challenge}`,
    })
  } catch (error) {
    Toast.error(errText(error))
  } finally {
    preparingBrowser.value = false
  }
}

/* ── 列表 ── */
const shots = ref<ScreenshotItem[]>([])
const loadState = ref<'loading' | 'ready' | 'error'>('loading')
const loadError = ref('')
/** 403 = 已认证但无 `order.view_screenshot`（列表门）：错误态给「返回」而不是「重试」（M1 详情页口径）。 */
const forbidden = ref(false)

async function load(): Promise<void> {
  if (!shapeOk.value) return
  loadError.value = ''
  forbidden.value = false
  loadState.value = 'loading'
  try {
    const data = await fetchScreenshots(orderNo.value)
    shots.value = data.items.filter((item) => item.img_type === IMG_TYPE_PROGRESS)
    loadState.value = 'ready'
  } catch (e) {
    loadState.value = 'error'
    loadError.value = errText(e)
    forbidden.value = e instanceof HttpError && e.status === 403
    Toast.error(loadError.value)
  }
}

/** 进度截图的 `img_type`（`OrderScreenshot.IMG_PROGRESS`）。 */
const IMG_TYPE_PROGRESS = 0
/** 允许上传的扩展名（与桌面 `ALLOWED_EXTS` / 后端 `core.storage.ALLOWED_EXTS` 同口径）。 */
const ALLOWED_EXTS = ['jpg', 'jpeg', 'png'] as const
/** 每单进度截图上限（桌面 `SCREENSHOT_LIMIT`；按 `img_type=0` 数，群码不占名额）。 */
const SCREENSHOT_LIMIT = 20

/** 预览：宫格缩略图点击 → `ImageViewer`（受控 index：点哪张从哪张看起，滑动同步回来）。 */
const viewerVisible = ref(false)
const viewerIndex = ref(0)
const viewerImages = computed(() => shots.value.map((item) => item.url))

function openPreview(index: number): void {
  viewerIndex.value = index
  viewerVisible.value = true
}

/* ── 上传 ── */
/** 上传在途闸：整批串行（并发点两下按钮不发第二套三跳）。 */
const uploading = ref(false)
const browserUploadedCount = ref(0)

/** 取扩展名（小写、去点；桌面 `extOf` 同口径）。 */
function extOf(name: string): string {
  return (name.split('.').pop() ?? '').toLowerCase()
}

function errText(e: unknown): string {
  return e instanceof Error && e.message ? e.message : FALLBACK_ERROR_TEXT
}

/** 从剪贴板事件里取图片文件（只认 `image/*`；桌面 `imageFilesOf` 同口径）。 */
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

async function handleFiles(files: File[]): Promise<void> {
  if (files.length === 0 || uploading.value || !canUpload.value || openInBrowser) return
  uploading.value = true
  try {
    const accepted: File[] = []
    let badExt = 0
    let overOriginal = 0
    let overCompressed = 0
    for (const file of files) {
      const ext = extOf(file.name)
      // ⚠️ 只在**名字带扩展名**时早挡（拍照 / 粘贴进来的图可能没有扩展名）——
      // 没有扩展名的前端无从判断，放它过去由签发端点兜底（桌面同口径）。
      if (ext !== '' && !(ALLOWED_EXTS as readonly string[]).includes(ext)) {
        badExt += 1
        continue
      }
      if (file.size > MAX_UPLOAD_BYTES) {
        overOriginal += 1
        continue
      }
      const prepared = await compressImage(file)
      if (prepared.size > COMPRESSED_MAX_BYTES) {
        overCompressed += 1
        continue
      }
      accepted.push(prepared)
    }
    // 提示顺序照桌面：坏类型 → 18MiB → 5MiB →（一张都没剩且有「太大」被滤）收尾句。
    if (badExt > 0) Toast(BAD_EXT_TEXT)
    if (overOriginal > 0) Toast(overOriginalText(overOriginal))
    if (overCompressed > 0) Toast(overCompressedText(overCompressed))
    if (accepted.length === 0) {
      if (overOriginal + overCompressed > 0) Toast(ALL_TOO_LARGE_TEXT)
      return
    }

    // 每单 20 张预检（桌面 D-0153 ①）：快照 = 列表里的进度截图数。一张都放不下就整批零请求；放得下一部分就只传前面几张（超出的那些一次往返都不发）。
    // ⚠️ 列表取不到（失败 / 无 view 节点吃 403）时按已知快照（可能 0 张）算 ⇒ 预检**偏乐观**，可能放行一张后端会拒的图——后端在签发与认领两处拦（权威），真拒了走 failures[0] 原样显示它的 msg。与桌面「取列表失败按 0 预检」同代价。
    let uploadable = accepted
    const room = SCREENSHOT_LIMIT - (browserUpload ? browserUploadedCount.value : shots.value.length)
    if (room <= 0) {
      Toast(LIMIT_TEXT)
      return
    }
    if (accepted.length > room) {
      uploadable = accepted.slice(0, room)
      Toast(LIMIT_TEXT)
    }

    const failures: string[] = []
    let uploaded = 0
    for (const file of uploadable) {
      try {
        const ext = extOf(file.name)
        const ticket = browserUpload
          ? await requestBrowserScreenshotTicket(orderNo.value, ext)
          : await requestScreenshotTicket(orderNo.value, ext, IMG_TYPE_PROGRESS)
        await putFileToStorage(ticket.upload_url, file)
        if (browserUpload) {
          await claimBrowserScreenshot(orderNo.value, ticket.screenshot_id, file.name)
        } else {
          await claimScreenshot(orderNo.value, {
            screenshot_id: ticket.screenshot_id,
            original_name: file.name,
            remark: '',
          })
        }
        uploaded += 1
      } catch (e) {
        if (browserUpload && e instanceof HttpError && e.status === 403 && !e.csrfRefreshed) {
          emit('browserExpired', errText(e))
          break
        }
        failures.push(errText(e))
      }
    }
    if (browserUpload) {
      browserUploadedCount.value += uploaded
    } else {
      await load()
    }
    if (failures.length > 0) Toast.error(failures[0]!)
  } finally {
    uploading.value = false
  }
}

/* ── 删除（确认口径逐字取桌面） ── */

function confirmDelete(item: ScreenshotItem): void {
  confirm({
    // 桌面 `layer.confirm` 无标题 ⇒ 不给 header；按钮文案同桌面 `{ btn: ['确定', '取消'] }`。
    content: DELETE_CONFIRM_TEXT,
    confirmBtn: '确定',
    cancelBtn: '取消',
    onConfirm: () => {
      void removeShot(item)
    },
  })
}

async function removeShot(item: ScreenshotItem): Promise<void> {
  try {
    await deleteScreenshot(item.id)
  } catch (e) {
    Toast.error(errText(e))
  }
  // 删完重取列表（成功失败都重取：列表就是真状态；桌面 manage 同语义）。
  await load()
}

/* ── 三路上传入口：相册 / 拍照 / 粘贴 ── */

const albumInput = ref<HTMLInputElement | null>(null)
const cameraInput = ref<HTMLInputElement | null>(null)

function onFilePick(event: Event): void {
  const input = event.target as HTMLInputElement
  void handleFiles(Array.from(input.files ?? [])).then(() => {
    // 清掉选择值：同一张图再选一次也要能触发 change。
    input.value = ''
  })
}

function onPaste(event: ClipboardEvent): void {
  const files = event.clipboardData?.items ? imageFilesOf(event.clipboardData.items) : []
  if (files.length === 0 || openInBrowser) return
  event.preventDefault()
  void handleFiles(files)
}

onMounted(() => {
  window.addEventListener('paste', onPaste)
  if (!browserUpload) void load()
})

onUnmounted(() => {
  window.removeEventListener('paste', onPaste)
})

const goBack = useWorkerBack(() => shapeOk.value
  ? {
    name: 'm-order-detail',
    params: { orderNo: orderNo.value },
    state: { [SCREENSHOT_RETURN_ORDER]: orderNo.value },
  }
  : { name: 'm-mine' })
</script>

<template>
  <section class="m-shot" :class="{ 'm-shot--browser': browserUpload }">
    <!-- 单号形状闸：不符 → 404 态页（零请求，与详情页同一道） -->
    <section v-if="!shapeOk" class="m-shot__missing" data-testid="shot-404">
      <p class="m-shot__missing-code">404</p>
      <p class="m-shot__missing-text">订单不存在或链接有误</p>
      <F7Button type="button" round outline small @click="goBack">返回</F7Button>
    </section>

    <template v-else>
      <MobilePageBar v-if="!browserUpload" title="订单截图" back-test-id="shot-back" @back="goBack">
        <template #right><span class="m-shot__count" data-testid="shot-count">{{ shots.length }}/20</span></template>
      </MobilePageBar>

      <!-- 首屏加载中 -->
      <div v-if="!browserUpload && loadState === 'loading'" class="m-shot__loading" data-testid="shot-loading" role="status">
        <F7Preloader :size="18" aria-hidden="true" /> 加载中…
      </div>

      <!-- 取数失败：msg 原样；403（无查看节点）→ 返回，其它 → 重试（M1 详情页口径） -->
      <section v-else-if="!browserUpload && loadState === 'error'" class="m-shot__error" data-testid="shot-error">
        <p class="m-shot__error-text">{{ loadError }}</p>
        <F7Button type="button" round outline small
          v-if="forbidden"
          data-testid="shot-error-back"
          @click="goBack"
        >
          返回
        </F7Button>
        <F7Button type="button" round outline small v-else data-testid="shot-error-retry" @click="load">
          重试
        </F7Button>
      </section>

      <template v-else-if="!browserUpload">
        <!-- 宫格：缩略图点击预览；删除角标只在有 `order.screenshot_manage` 时渲染（fail-closed） -->
        <ul v-if="shots.length > 0" class="m-shot-grid" data-testid="shot-grid">
          <li v-for="(item, index) in shots" :key="item.id" class="m-shot-grid__cell">
            <F7Button type="button" class="m-shot-grid__preview" @click="openPreview(index)">
              <img
                class="m-shot-grid__thumb"
                :src="item.thumbnail_url || item.url"
                loading="lazy"
                :alt="item.original_name"
                data-testid="shot-thumb"
              />
            </F7Button>
            <F7Button
              v-if="canManage"
              class="m-shot-grid__del"
              type="button"
              round
              :aria-label="`删除截图 ${item.original_name}`"
              :data-testid="`shot-del-${item.id}`"
              @click.stop="confirmDelete(item)"
            >
              <F7Icon f7="xmark" :size="16" aria-hidden="true" />
            </F7Button>
            <p class="m-shot-grid__time">{{ formatDateTime(item.created_at) }}</p>
          </li>
        </ul>
        <p v-else class="m-shot__empty" data-testid="shot-empty">{{ EMPTY_TEXT }}</p>
      </template>

      <!--
        上传区：只看形状闸 × `order.upload_screenshot`，**不随列表取数收窄**——
        仅有上传节点的账号列表会 403，上传区若藏在「取数成功」分支里它就传不了图
        （2026-10-11 评审 Important 修复；代价与桌面同：预检偏乐观，后端权威）。
      -->
      <F7Block v-if="canUpload && openInBrowser" strong inset outline class="m-shot-upload">
        <p class="m-shot-upload__hint">请用外部浏览器选图或拍照，打开后无需再次登录。</p>
        <F7Button type="button" round fill class="m-shot-upload__btn"
          :disabled.prop="preparingBrowser" data-testid="shot-browser-start" @click="prepareBrowserUpload">
          {{ preparingBrowser ? '正在准备…' : '在浏览器中上传' }}
        </F7Button>
      </F7Block>
      <F7Block v-else-if="canUpload" strong inset outline class="m-shot-upload" data-testid="shot-upload">
        <F7Button type="button"
          class="m-shot-upload__btn" round fill
          :disabled.prop="uploading"
          data-testid="shot-album-btn"
          @click="albumInput?.click()"
        >
          从相册选图
        </F7Button>
        <F7Button type="button"
          class="m-shot-upload__btn" round outline
          :disabled.prop="uploading"
          data-testid="shot-camera-btn"
          @click="cameraInput?.click()"
        >
          拍照
        </F7Button>
        <input
          ref="albumInput"
          class="m-shot-upload__input"
          type="file"
          multiple
          accept="image/jpeg,image/png"
          data-testid="shot-album-input"
          @change="onFilePick"
        />
        <input
          ref="cameraInput"
          class="m-shot-upload__input"
          type="file"
          accept="image/jpeg,image/png"
          capture="environment"
          data-testid="shot-camera-input"
          @change="onFilePick"
        />
        <p class="m-shot-upload__hint">支持相册多选、拍照与粘贴，单张不超过 18MB</p>
        <p v-if="browserUpload && browserUploadedCount > 0" class="m-shot-upload__hint"
          data-testid="shot-browser-success" role="status">已上传 {{ browserUploadedCount }} 张截图</p>
      </F7Block>

      <!-- 大图预览（受控 index + 滑动同步） -->
      <MobilePhotoBrowser
        v-if="!browserUpload"
        v-model:visible="viewerVisible"
        :images="viewerImages"
        :index="viewerIndex"
        :show-index="true"
        data-testid="shot-viewer"
        @index-change="(i: number) => (viewerIndex = i)"
      />
    </template>
  </section>
</template>

<style scoped>
.m-shot {
  min-height: 100vh;
  padding: 0 12px 16px;
  background: var(--f7-page-bg-color);
}

.m-shot--browser {
  min-height: 0;
  padding: 0;
}

.m-shot__missing {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding-top: 40vw;
}

.m-shot__missing-code {
  margin: 0;
  font-size: 40px;
  font-weight: 600;
  color: #d54941;
}

.m-shot__missing-text {
  margin: 0 0 8px;
  font-size: 14px;
  color: #6b7280;
}

.m-shot__count {
  font-size: 12px;
  color: #9ca3af;
}

.m-shot__loading,
.m-shot__empty {
  margin: 0;
  padding: 32px 0;
  font-size: 13px;
  color: #9ca3af;
  text-align: center;
}

.m-shot__error {
  padding: 24px 12px;
  text-align: center;
}

.m-shot__error-text {
  margin: 0 0 10px;
  font-size: 13px;
  color: #d54941;
}

.m-shot-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.m-shot-grid__cell {
  position: relative;
}

.m-shot-grid__preview.button {
  height: auto;
  padding: 0;
}

.m-shot-grid__thumb {
  display: block;
  width: 100%;
  aspect-ratio: 1;
  object-fit: cover;
  background: #f3f3f3;
  border-radius: 8px;
}

.m-shot-grid__del.button {
  --f7-button-height: 44px;
  --f7-button-bg-color: rgb(0 0 0 / 65%);
  --f7-button-text-color: #fff;
  position: absolute;
  top: 4px;
  right: 4px;
  width: 44px;
  padding: 0;
}

.m-shot-grid__time {
  margin: 4px 0 0;
  font-size: 11px;
  color: #9ca3af;
  text-align: center;
}

.m-shot-upload {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin: 16px 0 0;
}

.m-shot-upload__btn.button {
  flex: 1;
  width: auto;
}

.m-shot-upload__input {
  display: none;
}

.m-shot-upload__hint {
  flex-basis: 100%;
  margin: 0;
  font-size: 12px;
  color: #9ca3af;
}
</style>
