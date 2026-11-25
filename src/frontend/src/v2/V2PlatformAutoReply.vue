<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「平台消息自动回复打手设置」v2（`/v2/platform-auto-reply`；路由 name `v2-platform-auto-reply`）。
 *
 * 旧页 = island 复刻页 `layui-island/pages/system-platformautoreply.ts`（真 API 页）：
 * 8 列规则表 + 新增/修改弹层 + 批量修改弹层 + 图片上传。v2 等价重建，文案逐字照证据件
 * `public/replica/system-platformautoreply.html`（行号见常量表）。有意差异（设计稿 §4.2/§6）：
 * ① 回复时机 15/20 与 12 个非代练通平台在 v2 直接置灰（旧页可选、保存时才拒——同一结果的
 *   更直白表达；「未选时机提交 → 请选择回复时机」仍拦）；② 「启用平台」列 400→160（多平台
 *   预留宽度不搬）；③ 图片查看 = el-dialog 大图（image_url 现签短时效，不缓存不落库）；
 * ④ 新增不发 is_enabled（吃模型默认 True，照旧页差异 #9）；⑤ 上传本地先拦 2 MiB、
 * image_key 只在本次新上传时随载荷发（没换图不发，保持库里那张；无「清空图片」入口）；
 * ⑥ 批量修改逐行顺序 PATCH（⛔ 不并发）。
 * ⛔ 不 import island 页模块；权限门 `session.canNode('system.platform_auto_reply')`。
 */
import { computed, onMounted, ref } from 'vue'
import { fetchAllGames, type ConfigRow } from '@/api/config'
import {
  createPlatformAutoReplyRule,
  deletePlatformAutoReplyRule,
  fetchPlatformAutoReplyRules,
  updatePlatformAutoReplyRule,
  uploadPlatformAutoReplyImage,
  type PlatformAutoReplyRulePayload,
  type PlatformAutoReplyRuleRow,
} from '@/api/platformAutoReplies'
import { PLATFORMS } from '@/config/platforms'
import { useSessionStore } from '@/stores/session'

/* ── 逐字文案常量（出处 = 证据件 HTML / island 页，行号逐条标注）──────────────── */

/** 页头引文块（证据件 system-platformautoreply.html L40-43；粗体标签也在内）。 */
const PAGE_QUOTE_BOLD_1 = '功能介绍：'
const PAGE_QUOTE_LINE_1 =
  '您可以按需在此配置自动回复交易平台的打手留言消息，当交易平台有对应动作时，将会自动回复消息给打手；'
const PAGE_QUOTE_BOLD_2 = '前提条件：'
const PAGE_QUOTE_LINE_2 = '要求您已在本系统对接过第三方交易平台'
/** 工具条（证据件 L55-57 文案逐字）。 */
const TOOLBAR_REFRESH_TEXT = '刷新列表'
const TOOLBAR_ADD_TEXT = '新增自动回复'
const TOOLBAR_BATCH_TEXT = '批量修改自动回复内容'
const BATCH_NO_ROW_TEXT = '请勾选要操作的数据行'
/** 新增/修改弹层（证据件 L69-132；标题 = island L622）。 */
const FORM_QUOTE_TEXT = '根据实际需要，请在下面配置'
const FORM_TITLE_ADD = '新增自动回复'
const FORM_TITLE_EDIT = '修改自动回复'
const GAME_ALL_LABEL = '全部游戏'
// el-option 的 value prop 类型不含 null；运行时仍以 null 表示「全部游戏」（与 form.gameId 同值比较）。
const GAME_ALL_OPTION_VALUE = null as unknown as number
const CONTENT_PLACEHOLDER = '请在这里输入要自动回复的内容，不要有任何特殊代码'
const UPLOAD_BUTTON_TEXT = '上传'
const UPLOAD_BUTTON_BUSY_TEXT = '上传中…'
const UPLOAD_OK_TEXT = '图片已上传，保存后生效'
const UPLOAD_FAIL_PREFIX = '上传失败：'
const UPLOAD_TOO_LARGE_TEXT = '图片超过 2 MB，请压缩后再上传'
const NOT_OPEN_TITLE = '尚未开放'
/** 校验与保存提示（island validatePanelValues L335-352 / submitForm L664-683，逐字）。 */
const VALIDATE_TRIGGER_TEXT = '请选择回复时机'
const VALIDATE_CONTENT_EMPTY_TEXT = '回复内容不能为空'
const CONTENT_MAX_LENGTH = 200
const VALIDATE_CONTENT_LONG_TEXT = `回复内容不能超过 ${CONTENT_MAX_LENGTH} 个字符`
const SAVE_OK_TEXT = '已保存'
const SAVE_OK_REFRESH_FAIL_SUFFIX = '；但列表刷新失败：'
const SAVE_FAIL_PREFIX = '保存失败：'
/** 批量弹层（标题 = island L166；引文 = 证据件 L145 逐字）。 */
const BATCH_DIALOG_TITLE = '批量修改自动回复内容'
const BATCH_QUOTE_TEXT =
  '这个功能是作用于自动回复内容一致的游戏设置，当您在填写完回复内容保存后，所有已勾选的游戏消息设置，都将统一变更为刚保存的自动回复内容'
/** 图片（island imageCell L225-229 / openImage L916-922，逐字）。 */
const VIEW_IMAGE_TEXT = '查看图片'
const VIEW_IMAGE_TIP = '查看这条规则的回复图片'
const NO_IMAGE_TEXT = '未上传图片'
/** 删除确认（island L789-791，逐字）。 */
const DELETE_CONFIRM_TEXT = '是否确认删除这条自动回复？'
const DELETE_CONFIRM_OK_TEXT = '确认删除'
const DELETE_CONFIRM_CANCEL_TEXT = '点错了'
const DELETE_OK_TEXT = '已删除'
const GAME_LOAD_FAIL_PREFIX = '游戏列表加载失败：'
/** 表格三态（同 v2 页通用口径）。 */
const LOADING_TEXT = '正在加载…'
const EMPTY_TEXT = '暂无数据'
const LOAD_FAIL_PREFIX = '加载失败：'
const RETRY_TEXT = '重试'
const REFRESH_OK_TEXT = '已刷新'

function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

/* ── 回复时机 4 颗（证据件 L98-101，value 逐字：0/10/15/20）；本版只收 0/10 ── */

const TRIGGER_OPTIONS: ReadonlyArray<{ value: number; title: string; supported: boolean }> = [
  { value: 0, title: '接单后', supported: true },
  { value: 10, title: '申请验收后', supported: true },
  { value: 15, title: '提交异常后', supported: false },
  { value: 20, title: '申请撤销后', supported: false },
]

/** 「用在哪些平台」里本版只认的平台（代练通 = 10，与模型默认值同源）。 */
const SUPPORTED_PLATFORM = 10

/** 单张规则图上限（2 MiB）：与后端 MAX_RULE_IMAGE_BYTES 同值；前端先拦是为了说清楚原因。 */
const MAX_REPLY_IMAGE_BYTES = 2 * 1024 * 1024
const UPLOAD_IMAGE_ACCEPT = 'image/jpeg,image/png'

/* ── 权限门（fail-closed：没节点不画、不发请求）────────────────────────────── */

const session = useSessionStore()
const canView = computed(() => session.canNode(['system.platform_auto_reply']))

/* ── toast（同 V2OrderRecycle：info 1500ms / error 6000ms）───────────────────── */

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

/* ── 列表（数组；无服务端分页 ⇒ 不渲染分页器）────────────────────────────────── */

const rows = ref<PlatformAutoReplyRuleRow[]>([])
const loading = ref(false)
const loadError = ref<string | null>(null)

async function reloadList(): Promise<void> {
  loading.value = true
  try {
    rows.value = await fetchPlatformAutoReplyRules()
    loadError.value = null
  } catch (e) {
    rows.value = []
    loadError.value = errText(e)
  } finally {
    loading.value = false
  }
}

async function onRefresh(): Promise<void> {
  await reloadList()
  if (loadError.value !== null) showToast(loadError.value, 'error')
  else showToast(REFRESH_OK_TEXT)
}

/* ── 游戏字典（弹层「限定游戏」用；取失败 = 空数组 + 一次明确提示）────────────── */

const games = ref<ConfigRow[]>([])

async function loadGames(): Promise<void> {
  try {
    games.value = await fetchAllGames()
  } catch (e) {
    games.value = []
    showToast(`${GAME_LOAD_FAIL_PREFIX}${errText(e)}`, 'error')
  }
}

/* ── 勾选（批量修改的行集；无分页 ⇒ 表头勾 = 全部行）────────────────────────── */

const checkedIds = ref<number[]>([])

function isChecked(id: number): boolean {
  return checkedIds.value.includes(id)
}

function toggleRow(id: number): void {
  const i = checkedIds.value.indexOf(id)
  if (i === -1) checkedIds.value.push(id)
  else checkedIds.value.splice(i, 1)
}

const allChecked = computed(
  () => rows.value.length > 0 && rows.value.every((r) => isChecked(r.id)),
)

function toggleAll(): void {
  if (allChecked.value) checkedIds.value = []
  else checkedIds.value = rows.value.map((r) => r.id)
}

function checkedRows(): PlatformAutoReplyRuleRow[] {
  return rows.value.filter((r) => isChecked(r.id))
}

/* ── 行内启用开关：PATCH {is_enabled}；失败拨回 + 后端原文；成功整表重取 ─────── */

async function onRowSwitch(row: PlatformAutoReplyRuleRow, checked: boolean): Promise<void> {
  try {
    await updatePlatformAutoReplyRule(row.id, { is_enabled: checked })
  } catch (e) {
    showToast(errText(e), 'error')
  }
  await reloadList()
  if (loadError.value !== null) showToast(loadError.value, 'error')
}

/* ── 新增 / 修改弹层 ─────────────────────────────────────────────────────────── */

const editDlg = ref(false)
const editingRow = ref<PlatformAutoReplyRuleRow | null>(null)
const submitting = ref(false)

const form = ref({
  gameId: null as number | null,
  // undefined = 未选时机；不用 null 是为了过 el-radio-group v-model 的 prop 类型，运行时表现相同。
  trigger: undefined as number | undefined,
  content: '',
})

const formTitle = computed(() => (editingRow.value === null ? FORM_TITLE_ADD : FORM_TITLE_EDIT))

/** 本次弹层里刚上传成功的对象名 / 读链接；null / 空串 = 没换图（保存时不发 image_key）。 */
const pendingImageKey = ref<string | null>(null)
const pendingImageUrl = ref('')
const uploading = ref(false)
const fileInputEl = ref<HTMLInputElement | null>(null)

/** 缩略图：本次新图优先，否则库里那张；都没有不画。 */
const previewUrl = computed(() => {
  if (pendingImageUrl.value !== '') return pendingImageUrl.value
  const row = editingRow.value
  if (row !== null && row.has_image && row.image_url !== '') return row.image_url
  return ''
})

function openForm(row: PlatformAutoReplyRuleRow | null): void {
  editingRow.value = row
  const gameId = row?.game_id ?? null
  // 那款游戏已被删时回落「全部游戏」（照 island fillGameSelect L397 的口径）。
  const known = gameId !== null && games.value.some((g) => g.id === gameId)
  form.value = {
    gameId: known ? gameId : null,
    trigger: row?.trigger,
    content: row?.content ?? '',
  }
  pendingImageKey.value = null
  pendingImageUrl.value = ''
  editDlg.value = true
}

async function doSave(): Promise<void> {
  if (submitting.value) return
  if (form.value.trigger === undefined) {
    showToast(VALIDATE_TRIGGER_TEXT)
    return
  }
  // 编辑既有 15/20 时机（历史数据）时保存前明确拒绝，照旧页 validatePanelValues——置灰只挡新选，挡不住回填值。
  if (!TRIGGER_OPTIONS.some((o) => o.supported && o.value === form.value.trigger)) {
    const title = TRIGGER_OPTIONS.find((o) => o.value === form.value.trigger)?.title ?? ''
    showToast(`「${title}」尚未开放：本版只做「接单后」与「申请验收后」两种时机`, 'error')
    return
  }
  const content = form.value.content.trim()
  if (content === '') {
    showToast(VALIDATE_CONTENT_EMPTY_TEXT)
    return
  }
  if (content.length > CONTENT_MAX_LENGTH) {
    showToast(VALIDATE_CONTENT_LONG_TEXT)
    return
  }
  const payload: PlatformAutoReplyRulePayload = {
    platform: SUPPORTED_PLATFORM,
    game: form.value.gameId,
    trigger: form.value.trigger,
    content,
  }
  // image_key 只在本次新上传时发；没换图 = 不发（PATCH 语义 = 库里那张原样留着）。
  if (pendingImageKey.value !== null) payload.image_key = pendingImageKey.value
  submitting.value = true
  try {
    const target = editingRow.value
    if (target === null) await createPlatformAutoReplyRule(payload)
    else await updatePlatformAutoReplyRule(target.id, payload)
    editDlg.value = false
    await reloadList()
    if (loadError.value !== null) showToast(`${SAVE_OK_TEXT}${SAVE_OK_REFRESH_FAIL_SUFFIX}${loadError.value}`, 'error')
    else showToast(SAVE_OK_TEXT)
  } catch (e) {
    showToast(`${SAVE_FAIL_PREFIX}${errText(e)}`, 'error')
  } finally {
    submitting.value = false
  }
}

/* ── 图片上传（本地先拦 2 MiB；上传中禁用；失败不落 image_key）────────────────── */

async function onFileChange(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  // 清空 value：同一个文件被改过之后还能再选一次（否则 change 不再触发）。
  input.value = ''
  if (!file || uploading.value) return
  if (file.size > MAX_REPLY_IMAGE_BYTES) {
    showToast(UPLOAD_TOO_LARGE_TEXT)
    return
  }
  uploading.value = true
  try {
    const uploaded = await uploadPlatformAutoReplyImage(file)
    pendingImageKey.value = uploaded.image_key
    pendingImageUrl.value = uploaded.url
    showToast(UPLOAD_OK_TEXT)
  } catch (e) {
    showToast(`${UPLOAD_FAIL_PREFIX}${errText(e)}`, 'error')
  } finally {
    uploading.value = false
  }
}

function pickFile(): void {
  if (uploading.value) return
  fileInputEl.value?.click()
}

/* ── 看图（表内「查看图片」与弹层缩略图共用；现签 URL 即点即用，不缓存）────── */

const imageDlg = ref<{ url: string; alt: string } | null>(null)

function openImageView(row: PlatformAutoReplyRuleRow): void {
  if (!row.has_image || row.image_url === '') {
    showToast(NO_IMAGE_TEXT)
    return
  }
  imageDlg.value = { url: row.image_url, alt: `回复图片 ${row.id}` }
}

function openPreview(): void {
  const url = previewUrl.value
  if (url === '') {
    showToast(NO_IMAGE_TEXT)
    return
  }
  imageDlg.value = { url, alt: '回复图片' }
}

/* ── 批量修改：逐行顺序 PATCH {content}（⛔ 不并发；中断提示说清更新到第几条）── */

const batchDlg = ref(false)
const batchContent = ref('')
const batchSubmitting = ref(false)
let batchTargets: PlatformAutoReplyRuleRow[] = []

function openBatch(): void {
  const picked = checkedRows()
  if (picked.length === 0) {
    showToast(BATCH_NO_ROW_TEXT)
    return
  }
  // 开层时快照目标行集：批量中途重取列表也不改变这一轮要改的行。
  batchTargets = picked
  batchContent.value = ''
  batchDlg.value = true
}

async function doBatchSave(): Promise<void> {
  const content = batchContent.value.trim()
  if (content === '') {
    showToast(VALIDATE_CONTENT_EMPTY_TEXT)
    return
  }
  if (content.length > CONTENT_MAX_LENGTH) {
    showToast(VALIDATE_CONTENT_LONG_TEXT)
    return
  }
  if (batchSubmitting.value) return
  const targets = batchTargets
  batchSubmitting.value = true
  try {
    let done = 0
    for (const row of targets) {
      try {
        await updatePlatformAutoReplyRule(row.id, { content })
        done += 1
      } catch (e) {
        showToast(`已更新 ${done} / ${targets.length} 条，第 ${done + 1} 条失败：${errText(e)}`, 'error')
        await reloadList()
        return
      }
    }
    batchDlg.value = false
    await reloadList()
    if (loadError.value !== null) showToast(`${SAVE_OK_TEXT}${SAVE_OK_REFRESH_FAIL_SUFFIX}${loadError.value}`, 'error')
    else showToast(SAVE_OK_TEXT)
  } finally {
    batchSubmitting.value = false
  }
}

/* ── 删除确认 ───────────────────────────────────────────────────────────────── */

const confirmDel = ref<PlatformAutoReplyRuleRow | null>(null)
const deleting = ref(false)

async function doDelete(): Promise<void> {
  const row = confirmDel.value
  if (row === null || deleting.value) return
  deleting.value = true
  try {
    await deletePlatformAutoReplyRule(row.id)
    confirmDel.value = null
    checkedIds.value = checkedIds.value.filter((id) => id !== row.id)
    await reloadList()
    if (loadError.value !== null) showToast(loadError.value, 'error')
    else showToast(DELETE_OK_TEXT)
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    deleting.value = false
  }
}

/* ── 其它出口 ───────────────────────────────────────────────────────────────── */

onMounted(() => {
  if (!canView.value) return
  // 两路取数互不拖累：列表挂了也要能开弹层（反之亦然），各自把错误说出来。
  void reloadList()
  void loadGames()
})
</script>

<template>
  <div class="cv2-page" data-v2-page="platform-auto-reply">
    <template v-if="canView">
      <!-- 页头引文块（证据件 L39-44 逐字，含粗体标签） -->
      <div class="cv2-note cv2-page-quote" data-v2="page-quote">
        <p><b>{{ PAGE_QUOTE_BOLD_1 }}</b>{{ PAGE_QUOTE_LINE_1 }}</p>
        <p><b>{{ PAGE_QUOTE_BOLD_2 }}</b>{{ PAGE_QUOTE_LINE_2 }}</p>
      </div>

      <!-- 工具条（证据件 L55-57 文案逐字） -->
      <div class="cv2-toolbar" data-v2="toolbar">
        <button type="button" class="cv2-btn cv2-btn--tool" data-v2="refresh" :disabled="loading" @click="onRefresh">{{ TOOLBAR_REFRESH_TEXT }}</button>
        <button type="button" class="cv2-btn" data-v2="add" @click="openForm(null)">{{ TOOLBAR_ADD_TEXT }}</button>
        <button type="button" class="cv2-btn" data-v2="batch" @click="openBatch">{{ TOOLBAR_BATCH_TEXT }}</button>
        <span class="cv2-toolbar-count" data-v2="count">共 {{ rows.length }} 条规则</span>
      </div>

      <!-- 8 列规则表（列宽照设计稿 §4.2：130/125/弹性/80/160；勾选与开关列照 v2 先例） -->
      <section class="cv2-panel cv2-table-panel" aria-label="自动回复规则列表">
        <table class="cv2-table">
          <colgroup>
            <col style="width: 36px" />
            <col style="width: 130px" />
            <col style="width: 125px" />
            <col />
            <col style="width: 80px" />
            <col style="width: 160px" />
            <col style="width: 105px" />
            <col style="width: 130px" />
          </colgroup>
          <thead>
            <tr>
              <th class="cv2-th-check">
                <input type="checkbox" data-v2="check-page-all" :checked="allChecked" aria-label="勾选全部行" @change="toggleAll" />
              </th>
              <th class="cv2-th-center">限定游戏</th>
              <th class="cv2-th-center">回复时机</th>
              <th>回复内容</th>
              <th>图片</th>
              <th>启用平台</th>
              <th class="cv2-th-center">启用状态</th>
              <th class="cv2-th-center">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in rows"
              :key="row.id"
              class="cv2-row"
              :class="{ 'cv2-row--checked': isChecked(row.id) }"
              :data-v2-row="row.id"
            >
              <td class="cv2-th-check">
                <input
                  type="checkbox"
                  :data-v2-row-check="row.id"
                  :checked="isChecked(row.id)"
                  :aria-label="`勾选 ${row.id}`"
                  @change="toggleRow(row.id)"
                />
              </td>
              <td class="cv2-td-center" :data-v2-cell="`game-${row.id}`" :title="row.game_id === null ? '全部游戏' : row.game_name">
                {{ row.game_id === null ? '全部游戏' : row.game_name }}
              </td>
              <td class="cv2-td-center" :data-v2-cell="`trigger-${row.id}`">{{ row.trigger_name }}</td>
              <td :data-v2-cell="`content-${row.id}`" :title="row.content">{{ row.content }}</td>
              <td>
                <button type="button" class="cv2-actlink" :title="VIEW_IMAGE_TIP" :data-v2-view-image="row.id" @click="openImageView(row)">
                  {{ VIEW_IMAGE_TEXT }}
                </button>
              </td>
              <td :data-v2-cell="`platform-${row.id}`" :title="row.platform_label">{{ row.platform_label }}</td>
              <td class="cv2-td-center">
                <el-switch
                  :model-value="row.is_enabled"
                  :title="'绿色代表启用时'"
                  :data-v2-switch="`is_enabled-${row.id}`"
                  @change="(v: string | number | boolean) => onRowSwitch(row, Boolean(v))"
                />
              </td>
              <td class="cv2-td-center">
                <button type="button" class="cv2-actlink" :data-v2-edit="row.id" @click="openForm(row)">编辑</button>
                <button type="button" class="cv2-actlink cv2-actlink--danger cv2-actlink-gap" :data-v2-delete="row.id" @click="confirmDel = row">删除</button>
              </td>
            </tr>
            <tr v-if="rows.length === 0">
              <td :colspan="8" class="cv2-empty" data-v2="empty">
                <template v-if="loading">{{ LOADING_TEXT }}</template>
                <template v-else-if="loadError !== null">
                  {{ LOAD_FAIL_PREFIX }}{{ loadError }}
                  <button type="button" class="cv2-actlink cv2-actlink-gap" data-v2="retry" @click="onRefresh">{{ RETRY_TEXT }}</button>
                </template>
                <template v-else>{{ EMPTY_TEXT }}</template>
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <!-- 新增 / 修改弹层（md；标题 = island L622；引文 = 证据件 L70） -->
      <el-dialog
        :model-value="editDlg"
        :title="formTitle"
        width="640px"
        class="cv2-dialog cv2-dialog--md"
        destroy-on-close
        data-v2="dlg-form"
        @update:model-value="editDlg = false"
      >
        <p class="cv2-note cv2-note--dlg">{{ FORM_QUOTE_TEXT }}</p>
        <div class="cv2-form">
          <div class="cv2-field cv2-form-field">
            <span class="cv2-field-label">限定游戏</span>
            <el-select v-model="form.gameId" filterable :teleported="false" data-v2-field="game">
              <el-option :value="GAME_ALL_OPTION_VALUE" :label="GAME_ALL_LABEL" />
              <el-option v-for="g in games" :key="g.id" :value="g.id" :label="g.name" />
            </el-select>
          </div>
          <div class="cv2-field cv2-form-field">
            <span class="cv2-field-label">回复时机</span>
            <el-radio-group v-model="form.trigger" data-v2-field="trigger">
              <el-radio
                v-for="opt in TRIGGER_OPTIONS"
                :key="opt.value"
                :value="opt.value"
                :disabled="!opt.supported"
                :title="opt.supported ? undefined : NOT_OPEN_TITLE"
                :data-v2-trigger-opt="opt.value"
              >{{ opt.title }}</el-radio>
            </el-radio-group>
          </div>
          <div class="cv2-field cv2-form-field">
            <span class="cv2-field-label">自动回复内容</span>
            <el-input
              v-model="form.content"
              type="textarea"
              :rows="3"
              :maxlength="CONTENT_MAX_LENGTH"
              :placeholder="CONTENT_PLACEHOLDER"
              data-v2-field="content"
            />
          </div>
          <div class="cv2-field cv2-form-field">
            <span class="cv2-field-label">上传图片</span>
            <div class="cv2-upload-row">
              <button type="button" class="cv2-btn" data-v2="upload-btn" :disabled="uploading" @click="pickFile">
                {{ uploading ? UPLOAD_BUTTON_BUSY_TEXT : UPLOAD_BUTTON_TEXT }}
              </button>
              <img
                v-if="previewUrl !== ''"
                :src="previewUrl"
                class="cv2-pic-thumb"
                alt="回复图片"
                data-v2="pic-preview"
                @click="openPreview"
              />
            </div>
          </div>
          <div class="cv2-field cv2-form-field">
            <span class="cv2-field-label">用在哪些平台</span>
            <div class="cv2-platform-boxes" data-v2-field="platforms">
              <label
                v-for="p in PLATFORMS"
                :key="p.id"
                class="cv2-check"
                :data-v2-platform-box="p.id"
                :title="p.id === SUPPORTED_PLATFORM ? undefined : NOT_OPEN_TITLE"
              >
                <input
                  type="checkbox"
                  :checked="p.id === SUPPORTED_PLATFORM"
                  :disabled="p.id !== SUPPORTED_PLATFORM"
                  :data-v2-platform-check="p.id"
                />
                {{ p.label }}
              </label>
            </div>
          </div>
        </div>
        <template #footer>
          <div class="cv2-dlg-actions">
            <button type="button" class="cv2-btn cv2-btn--quiet" data-v2="form-cancel" @click="editDlg = false">取消</button>
            <button type="button" class="cv2-btn cv2-btn--submit cv2-btn--commit" data-v2="form-save" :disabled="submitting" @click="void doSave()">保存设置</button>
          </div>
        </template>
      </el-dialog>

      <!-- 隐藏文件选择框：必须由用户手势（点「上传」）触发 .click() -->
      <input
        ref="fileInputEl"
        type="file"
        class="cv2-file-hidden"
        :accept="UPLOAD_IMAGE_ACCEPT"
        data-v2="reply-image-input"
        @change="onFileChange"
      />

      <!-- 批量修改弹层（sm；标题 = island L166；引文 = 证据件 L145 逐字） -->
      <el-dialog
        :model-value="batchDlg"
        :title="BATCH_DIALOG_TITLE"
        width="480px"
        class="cv2-dialog cv2-dialog--sm"
        destroy-on-close
        data-v2="dlg-batch"
        @update:model-value="batchDlg = false"
      >
        <p class="cv2-note cv2-note--dlg">{{ BATCH_QUOTE_TEXT }}</p>
        <div class="cv2-field cv2-form-field">
          <span class="cv2-field-label">自动回复内容</span>
          <el-input
            v-model="batchContent"
            type="textarea"
            :rows="3"
            :maxlength="CONTENT_MAX_LENGTH"
            :placeholder="CONTENT_PLACEHOLDER"
            data-v2-field="batch-content"
          />
        </div>
        <template #footer>
          <div class="cv2-dlg-actions">
            <button type="button" class="cv2-btn cv2-btn--quiet" data-v2="batch-cancel" @click="batchDlg = false">取消</button>
            <button type="button" class="cv2-btn cv2-btn--submit cv2-btn--commit" data-v2="batch-save" :disabled="batchSubmitting" @click="void doBatchSave()">保存</button>
          </div>
        </template>
      </el-dialog>

      <!-- 看图弹层（lg；image_url 现签短时效，即点即用不缓存） -->
      <el-dialog
        :model-value="imageDlg !== null"
        title="回复图片"
        width="860px"
        class="cv2-dialog cv2-dialog--lg"
        destroy-on-close
        data-v2="dlg-image"
        @update:model-value="imageDlg = null"
      >
        <img v-if="imageDlg !== null" :src="imageDlg.url" :alt="imageDlg.alt" class="cv2-image-full" data-v2="image-full" />
      </el-dialog>

      <!-- 删除确认（sm；确认语与按钮逐字照 island L789-791） -->
      <el-dialog
        :model-value="confirmDel !== null"
        title="删除自动回复"
        width="480px"
        class="cv2-dialog cv2-dialog--sm"
        destroy-on-close
        data-v2="dlg-confirm"
        @update:model-value="confirmDel = null"
      >
        <p class="cv2-dlg-text" data-v2="confirm-text">{{ DELETE_CONFIRM_TEXT }}</p>
        <template #footer>
          <div class="cv2-dlg-actions">
            <button type="button" class="cv2-btn" data-v2="confirm-cancel" @click="confirmDel = null">{{ DELETE_CONFIRM_CANCEL_TEXT }}</button>
            <button type="button" class="cv2-btn cv2-btn--danger" data-v2="confirm-delete" :disabled="deleting" @click="void doDelete()">{{ DELETE_CONFIRM_OK_TEXT }}</button>
          </div>
        </template>
      </el-dialog>

      <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
        {{ toast.text }}
      </div>
    </template>
    <section v-else class="cv2-panel">
      <p class="cv2-empty" data-v2="no-permission">无权限查看本页</p>
    </section>
  </div>
</template>

<style scoped>
/* 页头引文块：贴住页头、压紧行距。 */
.cv2-page-quote {
  margin-top: 12px;
}

.cv2-page-quote p {
  margin: 0;
  line-height: 1.7;
}

.cv2-page-quote p + p {
  margin-top: 4px;
}

/* 行内两颗动作链接的间隙。 */
.cv2-actlink-gap {
  margin-left: 10px;
}

/* 弹层里的引文 / 表单排版。 */
.cv2-note--dlg {
  margin: 0 0 12px;
}

.cv2-form {
  display: grid;
  gap: 12px;
}

.cv2-form-field :deep(.el-textarea) {
  width: 100%;
}

/* 上传行：按钮 + 缩略图（48px，可点开预览）。 */
.cv2-upload-row {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 34px;
}

.cv2-pic-thumb {
  height: 48px;
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
  cursor: pointer;
}

/* 平台 13 checkbox：可换行的勾选网格；非代练通由 disabled 呈现置灰。 */
.cv2-platform-boxes {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
}

.cv2-platform-boxes .cv2-check {
  white-space: nowrap;
}

.cv2-file-hidden {
  display: none;
}

/* 看图弹层的大图：限高随视口、宽度自适应。 */
.cv2-image-full {
  display: block;
  max-width: 100%;
  max-height: 68vh;
  margin: 0 auto;
}
</style>
