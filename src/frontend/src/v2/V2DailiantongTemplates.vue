<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「代练通订单要求模板」v2（`/v2/dailiantong-templates`；路由 name `v2-dailiantong-templates`）。
 *
 * 旧页 = island **自有功能页** `layui-island/pages/dailiantong-order-templates.ts`：管理
 * **代练通侧**的「订单要求模板」（上游 OrderRequireTemp，发单到代练通时用，每账号最多 10 个）
 * ——⚠️ 不是本系统的「代练要求模板」，页顶定位说明逐字照 island buildHtml（L170-173）原文。
 * v2 等价重建：顶部表单区 + 下方列表（旧页编辑就是回填顶部表单，⛔ 不改弹层编辑；
 * 编辑态 = 「保存修改 / 取消编辑」与「新增模板」显隐切换）；新增 POST、修改 PATCH（三项全发
 * 明确覆盖）无确认框；删除 / 设默认先过确认弹层（文案逐字）；提交中禁用全部按钮 + busy
 * 在途闸；成功文案只进表单旁动作说明条（不弹 toast，照旧页），失败 toast + 说明条两处都出；
 * 状态行三态（record_count=null 省略「服务端总数」段；`id=null` 的行操作列画「—」无写按钮）。
 * 有意差异（设计稿 §4.4）：确认框 layer.confirm → cv2-dialog--sm（按钮文案照 layer 默认
 * 「确定 / 取消」）；默认徽标 cv2-st--settled、非默认灰签；视觉炭火。
 * 权限门 fail-closed：`session.canNode(['order.list'])`。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import {
  createDailiantongOrderTemplate,
  deleteDailiantongOrderTemplate,
  listDailiantongOrderTemplates,
  setDefaultDailiantongOrderTemplate,
  updateDailiantongOrderTemplate,
  type OrderTemplateInput,
  type OrderTemplateRow,
} from '@/api/dailiantongExtra'
import { useSessionStore } from '@/stores/session'

/* ── 页内文案（逐字照 island 页；出处 = layui-island/pages/dailiantong-order-templates.ts）── */

const MESSAGES = {
  loading: '加载中…', // L90
  empty: '还没有模板', // L91
  failed: '加载失败', // L92
  actionFailed: '操作失败', // L93
  noAccess: '无权限查看代练通订单要求模板', // L94
  needTitle: '请填写模板标题', // L95
  needContent: '请填写模板内容', // L96
  created: '模板已创建', // L97
  updated: '模板已更新', // L98
  deleted: '模板已删除', // L99
  defaulted: '已设为默认模板', // L100
  deleteConfirm: '删除后不可恢复，请确认。', // L101
  defaultConfirm: '代练通发单会默认使用它。', // L102
  retry: '重试', // L272
} as const

/** 页顶定位说明（逐字照 island buildHtml L170-173 原文，业务方要求不许删短；
 * 两处 <b> 强调随原文保留，模板里按 lead + <b>代练通侧</b> + mid + <b>不是</b> + tail 拼装）。 */
const POSITIONING_LEAD = '这里的模板是'
const POSITIONING_MID =
  '的「订单要求模板」（上游 OrderRequireTemp，发单到代练通时使用），'
const POSITIONING_TAIL = '本系统的「代练要求模板」——两者互不相干，别混用。'

const LIST_HEADS: readonly string[] = ['模板标题', '模板内容', '是否默认', '操作'] // L106

/** 后端序列化器的上限（island L85-86）。 */
const TITLE_MAX_LENGTH = 64
const CONTENT_MAX_LENGTH = 1024

const TITLE_PLACEHOLDER = '最多 64 个字符' // island L179
const CONTENT_PLACEHOLDER = `发单到代练通时展示给打手的订单要求（最多 ${CONTENT_MAX_LENGTH} 个字符）` // island L185
const DEFAULT_AUX = '勾上 = 保存后成为代练通侧的默认模板' // island L192

const EMPTY_CELL = '—' // island L111

/** 权限门（fail-closed：无节点只画空态，不发请求）。 */
const session = useSessionStore()
const canView = computed(() => session.canNode(['order.list']))

/* ── 提示（页面内自绘 toast，同 V2OrderRecycle；ElMessage 拿不到 .ttv2 皮）────────────── */

const toast = ref<{ text: string; kind: 'info' | 'error' } | null>(null)
let toastTimer: ReturnType<typeof setTimeout> | null = null

function showToast(text: string, kind: 'info' | 'error' = 'error'): void {
  if (toastTimer !== null) clearTimeout(toastTimer)
  toast.value = { text, kind }
  toastTimer = setTimeout(() => {
    toast.value = null
    toastTimer = null
  }, kind === 'error' ? 6000 : 1500)
}

/** 错误对象 → 用户可见文案（后端 `msg` 原文优先；island errorText 同式）。 */
function errText(e: unknown): string {
  return e instanceof Error && e.message !== '' ? e.message : MESSAGES.failed
}

/* ── 表单（新建态 / 编辑态共用；编辑 = 回填顶部表单，不是弹层）────────────────────────── */

const form = reactive<{ title: string; temp: string; isDefault: boolean }>({
  title: '',
  temp: '',
  isDefault: false,
})

/** 正在编辑的模板 id（`null` = 新建态）。 */
const editingId = ref<number | null>(null)

/** 表单旁常驻动作说明条（成功绿 / 失败红；照旧页 ACTION_MESSAGE_ID）。 */
const actionMessage = ref<{ text: string; kind: 'none' | 'ok' | 'error' }>({ text: '', kind: 'none' })

/** 在途闸（连点只发一次；提交中禁用全部按钮）。 */
const busy = ref(false)

function setEditing(id: number | null): void {
  editingId.value = id
}

/** 回到新建态（提交成功 / 取消编辑都走它；照旧页 resetForm）。 */
function resetForm(): void {
  form.title = ''
  form.temp = ''
  form.isDefault = false
  setEditing(null)
}

/** 读表单 → 契约载荷（三项全发 = 明确覆盖；照旧页 readForm 的 trim 口径）。 */
function readForm(): OrderTemplateInput {
  return {
    title: form.title.trim(),
    temp: form.temp.trim(),
    is_default: form.isDefault,
  }
}

/** 体验层预检（文案带字段名；服务端还有一层同义校验；照旧页 precheck）。返回空串 = 通过。 */
function precheck(payload: OrderTemplateInput): string {
  if (payload.title === '') return MESSAGES.needTitle
  if (payload.temp === '') return MESSAGES.needContent
  return ''
}

/** 预检失败：toast + 动作说明条两处都出（照旧页 msg + setActionMessage）。 */
function failPrecheck(problem: string): void {
  showToast(problem, 'error')
  actionMessage.value = { text: problem, kind: 'error' }
}

/* ── 列表（状态行三态；写成功后也走它）────────────────────────────────────────────── */

type StateKind = 'loading' | 'ready' | 'empty' | 'error'

const rows = ref<OrderTemplateRow[]>([])
const state = ref<StateKind>('loading')
const stateText = ref<string>(MESSAGES.loading)

async function reload(): Promise<void> {
  state.value = 'loading'
  stateText.value = MESSAGES.loading
  try {
    const payload = await listDailiantongOrderTemplates()
    rows.value = payload.items
    if (payload.items.length === 0) {
      state.value = 'empty'
      stateText.value = MESSAGES.empty
    } else {
      state.value = 'ready'
      // record_count=null（上游没给总数）⇒ 省略「（服务端总数 M）」段（island L348-349）。
      const count = payload.record_count === null ? '' : `（服务端总数 ${payload.record_count}）`
      stateText.value = `共 ${payload.items.length} 条模板${count}`
    }
  } catch (e) {
    state.value = 'error'
    stateText.value = errText(e)
  }
}

function refreshList(): void {
  if (busy.value) return
  void reload()
}

/* ── 写动作（busy 在途闸 + 提交中禁用全部按钮；成功只进说明条，失败 toast + 说明条）────── */

async function writeAndReload(
  action: () => Promise<unknown>,
  successText: string,
  after?: () => void,
): Promise<void> {
  if (busy.value) return
  busy.value = true
  actionMessage.value = { text: '', kind: 'none' }
  try {
    await action()
    actionMessage.value = { text: successText, kind: 'ok' }
    after?.()
    await reload()
  } catch (e) {
    const text = errText(e)
    showToast(text, 'error')
    actionMessage.value = { text: `${MESSAGES.actionFailed}：${text}`, kind: 'error' }
  } finally {
    busy.value = false
  }
}

/** 新增模板（确认框不是必需的：它不改已有数据；照旧页）。 */
function create(): void {
  if (busy.value) return
  const payload = readForm()
  const problem = precheck(payload)
  if (problem !== '') {
    failPrecheck(problem)
    return
  }
  void writeAndReload(() => createDailiantongOrderTemplate(payload), MESSAGES.created, resetForm)
}

/** 保存修改（必须处于编辑态；PATCH 三项全发）。 */
function save(): void {
  if (busy.value || editingId.value === null) return
  const id = editingId.value
  const payload = readForm()
  const problem = precheck(payload)
  if (problem !== '') {
    failPrecheck(problem)
    return
  }
  void writeAndReload(() => updateDailiantongOrderTemplate(id, payload), MESSAGES.updated, resetForm)
}

/** 开始编辑（行内「编辑」= 回填顶部表单；照旧页 startEdit）。 */
function startEdit(row: OrderTemplateRow): void {
  if (busy.value || row.id === null) return
  form.title = row.title
  form.temp = row.temp
  form.isDefault = row.is_default
  setEditing(row.id)
  actionMessage.value = { text: '', kind: 'none' }
}

/** 取消编辑（回到新建态并清掉说明条；照旧页 cancel）。 */
function cancelEdit(): void {
  if (busy.value) return
  resetForm()
  actionMessage.value = { text: '', kind: 'none' }
}

/* ── 确认弹层（删除 / 设默认必须先确认；layer.confirm → cv2-dialog--sm）──────────────── */

type Pending = { kind: 'delete' | 'default'; id: number; title: string; text: string }

const pending = ref<Pending | null>(null)

/** 确认句的模板名：标题为空回落 `模板 <id>`（island askDelete/askDefault 同式）。 */
function labelOf(row: OrderTemplateRow): string {
  return row.title === '' && row.id !== null ? `模板 ${row.id}` : row.title
}

function askDelete(row: OrderTemplateRow): void {
  if (busy.value || row.id === null) return
  pending.value = {
    kind: 'delete',
    id: row.id,
    title: '删除模板', // island L439
    text: `确定删除模板「${labelOf(row)}」吗？${MESSAGES.deleteConfirm}`,
  }
}

function askDefault(row: OrderTemplateRow): void {
  if (busy.value || row.id === null || row.is_default) return
  pending.value = {
    kind: 'default',
    id: row.id,
    title: '设为默认模板', // island L459
    text: `确定把模板「${labelOf(row)}」设为默认吗？${MESSAGES.defaultConfirm}`,
  }
}

function closeConfirm(): void {
  pending.value = null
}

/** 确认执行：先关弹层再发请求（照旧页 layer.close 后 run）；取消 = 什么都不发。 */
function confirmPending(): void {
  const target = pending.value
  if (target === null || busy.value) return
  pending.value = null
  if (target.kind === 'delete') {
    void writeAndReload(
      () => deleteDailiantongOrderTemplate(target.id),
      MESSAGES.deleted,
      () => {
        // 删的正是正在编辑的那条 ⇒ 表单回到新建态（island L444-446 的 after 钩子）。
        if (editingId.value === target.id) resetForm()
      },
    )
  } else {
    void writeAndReload(() => setDefaultDailiantongOrderTemplate(target.id), MESSAGES.defaulted)
  }
}

/* ── 其它出口 ─────────────────────────────────────────────────────────────────────── */

onMounted(() => {
  // fail-closed：无权限连首屏请求都不发（设计稿 §0-7），只画空态。
  if (canView.value) void reload()
})
</script>

<template>
  <div class="cv2-page" data-v2-page="dailiantong-templates">
    <div v-if="!canView" class="cv2-empty" data-v2="no-permission">{{ MESSAGES.noAccess }}</div>
    <template v-else>
      <!-- 表单区：定位说明（逐字）+ 标题/内容/是否默认 + 按钮组（编辑态显隐切换）+ 动作说明条 -->
      <section class="cv2-panel" aria-label="模板表单" data-v2="form-panel">
        <p class="cv2-note tpl-positioning" data-v2="positioning">
          {{ POSITIONING_LEAD }}<b>代练通侧</b>{{ POSITIONING_MID }}<b>不是</b>{{ POSITIONING_TAIL }}
        </p>
        <div class="tpl-form">
          <label class="cv2-field">
            <span class="cv2-field-label">模板标题</span>
            <el-input
              v-model="form.title"
              :maxlength="TITLE_MAX_LENGTH"
              :placeholder="TITLE_PLACEHOLDER"
              data-v2-field="title"
            />
          </label>
          <label class="cv2-field">
            <span class="cv2-field-label">模板内容</span>
            <el-input
              v-model="form.temp"
              type="textarea"
              :rows="3"
              :maxlength="CONTENT_MAX_LENGTH"
              :placeholder="CONTENT_PLACEHOLDER"
              data-v2-field="content"
            />
          </label>
          <label class="cv2-field">
            <span class="cv2-field-label">是否默认</span>
            <span class="tpl-default-cell">
              <el-checkbox v-model="form.isDefault" data-v2-field="default" />
              <span class="tpl-default-aux">{{ DEFAULT_AUX }}</span>
            </span>
          </label>
          <div class="cv2-form-actions">
            <button
              v-if="editingId === null"
              type="button"
              class="cv2-btn cv2-btn--submit cv2-btn--commit"
              data-v2="create"
              :disabled="busy"
              @click="create"
            >新增模板</button>
            <button
              v-else
              type="button"
              class="cv2-btn cv2-btn--submit cv2-btn--commit"
              data-v2="save"
              :disabled="busy"
              @click="save"
            >保存修改</button>
            <button
              v-if="editingId !== null"
              type="button"
              class="cv2-btn cv2-btn--quiet"
              data-v2="cancel"
              :disabled="busy"
              @click="cancelEdit"
            >取消编辑</button>
            <span
              class="cv2-action-msg"
              :class="{ 'cv2-action-msg--ok': actionMessage.kind === 'ok', 'cv2-action-msg--error': actionMessage.kind === 'error' }"
              data-v2="action-message"
            >{{ actionMessage.text }}</span>
          </div>
        </div>
      </section>

      <!-- 列表区：模板列表 + 刷新 / 状态行三态 / 4 列表格（id=null 行操作列无写按钮） -->
      <section class="cv2-panel" aria-label="模板列表" data-v2="list-panel">
        <div class="cv2-list-head">
          <h2 class="cv2-panel-title" data-v2="list-title">模板列表</h2>
          <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--find" data-v2="refresh" :disabled="busy" @click="refreshList">刷新</button>
        </div>
        <p class="cv2-status" :class="{ 'cv2-status--error': state === 'error' }" data-v2="status">
          <template v-if="state === 'error'">
            <span>加载失败：{{ stateText }}</span>
            <button type="button" class="cv2-btn cv2-btn--mini" data-v2="retry" :disabled="busy" @click="refreshList">{{ MESSAGES.retry }}</button>
          </template>
          <template v-else>{{ stateText }}</template>
        </p>
        <table class="cv2-table" data-v2="list">
          <colgroup>
            <col style="width: 220px" />
            <col />
            <col style="width: 96px" />
            <col style="width: 190px" />
          </colgroup>
          <thead>
            <tr>
              <th v-for="head in LIST_HEADS" :key="head" :data-v2-col="head">{{ head }}</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="(row, index) in rows"
              :key="row.id ?? `row-${index}`"
              :data-v2-row="row.id ?? `row-${index}`"
            >
              <td :title="row.title">{{ row.title === '' ? EMPTY_CELL : row.title }}</td>
              <td :title="row.temp" :data-v2-cell="`temp-${index}`">{{ row.temp === '' ? EMPTY_CELL : row.temp }}</td>
              <td class="cv2-td-center">
                <span
                  v-if="row.is_default"
                  class="cv2-st cv2-st--settled"
                  :data-v2-default-badge="row.id ?? ''"
                ><i class="cv2-dot" aria-hidden="true"></i>默认</span>
                <span v-else class="cv2-st cv2-st--unassigned"><i class="cv2-dot" aria-hidden="true"></i>否</span>
              </td>
              <td class="cv2-td-center cv2-td-actions">
                <template v-if="row.id !== null">
                  <button type="button" class="cv2-actlink" :data-v2-edit="row.id" :disabled="busy" @click="startEdit(row)">编辑</button>
                  <button
                    v-if="!row.is_default"
                    type="button"
                    class="cv2-actlink"
                    :data-v2-default="row.id"
                    :disabled="busy"
                    @click="askDefault(row)"
                  >设为默认</button>
                  <button type="button" class="cv2-actlink cv2-actlink--danger" :data-v2-delete="row.id" :disabled="busy" @click="askDelete(row)">删除</button>
                </template>
                <span v-else>—</span>
              </td>
            </tr>
            <tr v-if="rows.length === 0">
              <td :colspan="LIST_HEADS.length" class="cv2-empty" data-v2="empty">{{ MESSAGES.empty }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <!-- 确认弹层（删除 / 设默认共用；按钮文案照 layer.confirm 默认「确定 / 取消」） -->
      <el-dialog
        :model-value="pending !== null"
        :title="pending?.title ?? ''"
        class="cv2-dialog cv2-dialog--sm"
        destroy-on-close
        data-v2="dlg-confirm"
        @update:model-value="closeConfirm"
      >
        <p class="cv2-dlg-text" data-v2="confirm-text">{{ pending?.text }}</p>
        <template #footer>
          <div class="cv2-dlg-actions">
            <button type="button" class="cv2-btn cv2-btn--quiet" data-v2="confirm-cancel" :disabled="busy" @click="closeConfirm">取消</button>
            <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--commit" data-v2="confirm-ok" :disabled="busy" @click="confirmPending">确定</button>
          </div>
        </template>
      </el-dialog>

      <!-- 炭火 toast -->
      <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
        {{ toast.text }}
      </div>
    </template>
  </div>
</template>

<style scoped>
/* 本页新增的形状收在这里；色一律 var(--tt2-*) token（⛔ 不写裸色值）。 */
/* 定位说明块（cv2-note 基类的 margin-top 归零，贴面板顶）。 */
.tpl-positioning {
  margin: 0 0 14px;
}

/* 表单区纵向一列（照旧页 form-item 堆叠）；标题格放宽（cv2-field 里 EP 默认 160px 不够用）。 */
.tpl-form {
  display: grid;
  gap: 12px;
  max-width: 760px;
}

.tpl-form .cv2-field .el-input {
  width: 100%;
}

.tpl-default-cell {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.tpl-default-aux {
  font-size: 12px;
  color: var(--tt2-dim);
}

/* 表单旁常驻动作说明条（成功绿 / 失败红；照旧页 setActionMessage 的三态）。 */
.cv2-action-msg {
  font-size: 13px;
  color: var(--tt2-dim);
}

.cv2-action-msg--ok {
  color: var(--tt2-st-settled);
}

.cv2-action-msg--error {
  color: var(--tt2-st-error);
}

/* 列表头（模板列表 + 刷新）。 */
.cv2-list-head {
  display: flex;
  align-items: center;
  gap: 10px;
}

/* 状态行（旧页 STATUS_ID 的炭火对应物）。 */
.cv2-status {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 8px 0 10px;
  font-size: 13px;
  color: var(--tt2-dim);
}

.cv2-status--error {
  color: var(--tt2-st-error);
}

/* 行内动作相邻间隙（文字链无边框，照 V2OrderRecycle 的 td-shots 同款规则）。 */
.cv2-td-actions .cv2-actlink + .cv2-actlink {
  margin-left: 10px;
}
</style>
