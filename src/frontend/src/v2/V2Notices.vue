<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「系统内部公告管理」v2（`/v2/notices`；系统维护批次 A 波）。旧页 = island 页
 * `system-systemnotice.ts`：4 列 + 刷新/发布工具栏，无分页；无权限操作列整列空
 * （fail-closed）；标题服务端派生、本页只传 content（计划 §3 页 8）。
 */
import { computed, onMounted, ref } from 'vue'
import {
  SYSTEM_NOTICE_PAGE_SIZE,
  createSystemNotice,
  deleteSystemNotice,
  fetchSystemNotices,
  updateSystemNotice,
  type SystemNotice,
} from '@/api/systemNotices'
import { useSessionStore } from '@/stores/session'

const session = useSessionStore()

/* ── 逐字文案与常量（出处 = island 页同语义常量；本地重写不引模块）────────────── */

/** 列表里标题 / 内容的截断长度（island TITLE_MAX / CONTENT_MAX：标题 50、内容 200）。 */
const TITLE_MAX = 50
const CONTENT_MAX = 200
/** 弹窗 textarea 的 maxlength（island MAX_CONTENT = 1000）与 placeholder（dialogHtml 逐字）。 */
const MAX_CONTENT = 1000
const CONTENT_PLACEHOLDER = '请在此输入要发布给系统内所有人员看的公告'
/** 校验两句（island validateNotice 逐字）。 */
const CONTENT_REQUIRED_TEXT = '请输入公告内容'
const CONTENT_TOO_LONG_TEXT = `公告内容不能超过 ${MAX_CONTENT} 个字`
/** 无权限 / 空态 / 超量（island openDialog · text.none · reload 逐字）。 */
const NO_PERMISSION_TEXT = '没有发布公告的权限'
const EMPTY_TEXT = '还没有发布过公告'
/** 刷新成功（island onClick refresh 分支逐字）。 */
const REFRESHED_TEXT = '已刷新'

function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

/* ── 提示（页面内自绘 toast，同 V2UserList；info 1500ms / error 6000ms）───────── */

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

/* ── 权限门（操作列与发布钮：无 system.publish_notice 整列空，照 island canManage）── */

const canManage = computed(() => session.canNode(['system.publish_notice']))

/* ── 列表（无分页：一次取 SYSTEM_NOTICE_PAGE_SIZE=200，超量 toast 逐字照旧页）────── */

const rows = ref<SystemNotice[]>([])
const total = ref(0)
const loading = ref(false)

async function reload(): Promise<boolean> {
  loading.value = true
  try {
    const page = await fetchSystemNotices({ page: 1, page_size: SYSTEM_NOTICE_PAGE_SIZE })
    rows.value = page.items
    total.value = page.total
    if (page.total > page.items.length) {
      showToast(`公告太多，只显示了前 ${page.items.length} 条（共 ${page.total} 条）`)
    }
    return true
  } catch (e) {
    showToast(errText(e), 'error')
    return false
  } finally {
    loading.value = false
  }
}

function onRefresh(): void {
  void reload().then((ok) => {
    if (ok) showToast(REFRESHED_TEXT)
  })
}

/* ── 单元格展示（island noticeTitle / noticeExcerpt / formatNoticeTime 同口径重写）─ */

/** 内容摘要：压平空白、前 200 字加「...」；空内容显示「—」（island noticeExcerpt 同式）。 */
function noticeExcerpt(content: string): string {
  const text = (content ?? '').replace(/\s+/g, ' ').trim()
  if (!text) return '—'
  return text.length > CONTENT_MAX ? `${text.slice(0, CONTENT_MAX)}...` : text
}

/** 标题兜底：服务端已派生，理论上非空；只防「空标题显示空白」。 */
function noticeTitle(notice: SystemNotice): string {
  return (notice.title ?? '').trim() || noticeExcerpt(notice.content)
}

/** 发布时间显示成 `YYYY-MM-DD HH:mm`；解析不了就原样回（island formatNoticeTime 同式）。 */
function formatNoticeTime(raw: string): string {
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(raw ?? '')
  return match ? `${match[1]} ${match[2]}` : (raw ?? '')
}

/* ── 全文弹层（标题「内部公告：<前30字>...」逐字；正文换行保留）────────────────── */

const viewDlg = ref<SystemNotice | null>(null)

function viewTitle(notice: SystemNotice): string {
  return `内部公告：${noticeTitle(notice).slice(0, 30)}...`
}

/* ── 发布 / 编辑弹窗（唯一字段公告内容；标题服务端派生只读不传）────────────────── */

const editDlg = ref<{ notice: SystemNotice | null; content: string } | null>(null)
const saving = ref(false)

function openEditDlg(): void {
  if (!canManage.value) {
    showToast(NO_PERMISSION_TEXT)
    return
  }
  editDlg.value = { notice: null, content: '' }
}

function onRowEdit(notice: SystemNotice): void {
  if (!canManage.value) {
    showToast(NO_PERMISSION_TEXT)
    return
  }
  editDlg.value = { notice, content: notice.content ?? '' }
}

function editDlgTitle(): string {
  return editDlg.value?.notice ? '编辑公告' : '新增一个内部公告'
}

/** 本地校验两句逐字（island validateNotice；长度由 maxlength + 后端把住）。 */
async function saveNotice(): Promise<void> {
  const dlg = editDlg.value
  if (dlg === null || saving.value) return
  if (!(dlg.content ?? '').trim()) {
    showToast(CONTENT_REQUIRED_TEXT)
    return
  }
  if ((dlg.content ?? '').length > MAX_CONTENT) {
    showToast(CONTENT_TOO_LONG_TEXT)
    return
  }
  saving.value = true
  try {
    if (dlg.notice) await updateSystemNotice(dlg.notice.id, dlg.content)
    else await createSystemNotice(dlg.content)
    editDlg.value = null
    showToast('保存成功')
    await reload()
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    saving.value = false
  }
}

/* ── 删除（确认问句逐字；按钮「确定删除 / 取消」照旧页 layer.confirm）─────────── */

const delDlg = ref<SystemNotice | null>(null)

function onRowDelete(notice: SystemNotice): void {
  delDlg.value = notice
}

async function confirmDelete(): Promise<void> {
  const notice = delDlg.value
  if (notice === null) return
  try {
    await deleteSystemNotice(notice.id)
    delDlg.value = null
    showToast('删除成功')
    await reload()
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

onMounted(() => {
  void reload()
})
</script>

<template>
  <div class="cv2-page" data-v2-page="notices">
    <!-- 工具栏：刷新列表 + 发布一个内部公告（红钮，照旧页 layui-btn-danger；发布钮 fail-closed） -->
    <div class="cv2-toolbar" data-v2="toolbar">
      <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--find" data-v2="refresh" :disabled="loading" @click="onRefresh">刷新列表</button>
      <button v-if="canManage" type="button" class="cv2-btn cv2-btn--danger cv2-btn--commit" data-v2="add" @click="openEditDlg">发布一个内部公告</button>
      <span class="cv2-toolbar-count" data-v2="total">共 {{ total }} 条</span>
    </div>

    <!-- 主表：4 列照旧页；无 publish_notice 时操作列整列空 -->
    <section class="cv2-panel cv2-table-panel" aria-label="系统内部公告列表">
      <table class="cv2-table">
        <thead>
          <tr>
            <th>公告标题</th>
            <th>公告内容</th>
            <th class="cv2-th-center">发布时间</th>
            <th class="cv2-th-center">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id" class="cv2-row" :data-v2-row="row.id">
            <td>
              <a href="#" class="cv2-order-link nt-title" :title="noticeTitle(row)" :data-v2-view="row.id" @click.prevent="viewDlg = row">
                {{ noticeTitle(row).slice(0, TITLE_MAX) }}...
              </a>
            </td>
            <td>
              <a href="#" class="cv2-order-link nt-excerpt" :title="noticeExcerpt(row.content)" :data-v2-view-content="row.id" @click.prevent="viewDlg = row">{{ noticeExcerpt(row.content) }}</a>
            </td>
            <td class="cv2-td-center cv2-td-time">{{ formatNoticeTime(row.created_at) }}</td>
            <td class="cv2-td-center nt-actions">
              <template v-if="canManage">
                <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--sm cv2-btn--commit" :data-v2-edit-btn="row.id" title="编辑" @click="onRowEdit(row)">修改</button>
                <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--sm" :data-v2-del-btn="row.id" title="删除" @click="onRowDelete(row)">删除</button>
              </template>
            </td>
          </tr>
          <tr v-if="rows.length === 0">
            <td colspan="4" class="cv2-empty" data-v2="empty">
              <template v-if="loading">正在加载…</template>
              <template v-else>{{ EMPTY_TEXT }}</template>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 全文弹层（标题「内部公告：<前30字>...」；正文换行保留） -->
    <el-dialog
      :model-value="viewDlg !== null"
      :title="viewDlg === null ? '' : viewTitle(viewDlg)"
      class="cv2-dialog cv2-dialog--lg"
      destroy-on-close
      data-v2="dlg-view"
      @update:model-value="viewDlg = null"
    >
      <p v-if="viewDlg !== null" class="nt-fulltext" data-v2="view-content">{{ viewDlg.content }}</p>
    </el-dialog>

    <!-- 发布 / 编辑弹窗（唯一字段公告内容；标题服务端派生，本页不传） -->
    <el-dialog
      :model-value="editDlg !== null"
      :title="editDlg === null ? '' : editDlgTitle()"
      class="cv2-dialog cv2-dialog--lg"
      destroy-on-close
      data-v2="dlg-edit"
      @update:model-value="editDlg = null"
    >
      <div class="cv2-field">
        <span class="cv2-field-label">公告内容</span>
        <el-input
          v-if="editDlg !== null"
          v-model="editDlg.content"
          type="textarea"
          :rows="12"
          :maxlength="MAX_CONTENT"
          :placeholder="CONTENT_PLACEHOLDER"
          data-v2="content-input"
        />
      </div>
      <div class="cv2-dlg-actions">
        <button type="button" class="cv2-btn cv2-btn--quiet" @click="editDlg = null">取消</button>
        <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="notice-save" :disabled="saving" @click="void saveNotice()">保存</button>
      </div>
    </el-dialog>

    <!-- 删除确认（问句与按钮逐字照旧页 layer.confirm） -->
    <el-dialog
      :model-value="delDlg !== null"
      title="提示"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-delete"
      @update:model-value="delDlg = null"
    >
      <p class="cv2-dlg-text" data-v2="del-question">
        确定删除公告「{{ delDlg === null ? '' : noticeTitle(delDlg) }}」吗？
      </p>
      <p class="cv2-dlg-text">删除后主页的「通知和公告」里也会消失。</p>
      <div class="cv2-dlg-actions">
        <button type="button" class="cv2-btn cv2-btn--quiet" @click="delDlg = null">取消</button>
        <button type="button" class="cv2-btn cv2-btn--danger" data-v2="del-confirm" @click="void confirmDelete()">确定删除</button>
      </div>
    </el-dialog>

    <!-- 炭火 toast（居中深条） -->
    <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
      {{ toast.text }}
    </div>
  </div>
</template>

<style scoped>
/* 本页新增的形状收在这里；色一律走 carbon.css 的 token（⛔ 不写裸色值）。 */

/* 表格内锚点动作链：基态不带全局链接下划线、悬停补回（V2UserList 同款）。 */
a.cv2-order-link {
  text-decoration: none;
}

a.cv2-order-link:hover {
  text-decoration: underline;
}

/* 标题 / 摘要列：单行截断（44px 行高的家族口径），全名靠模板 title。 */
.nt-title,
.nt-excerpt {
  display: block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

td.nt-actions {
  white-space: nowrap;
}

td.nt-actions .cv2-btn--sm + .cv2-btn--sm {
  margin-left: -1px;
}

/* 全文弹层正文：旧页 14px/30px 行高的同款可读形态（尺寸非色值）。 */
.nt-fulltext {
  margin: 0;
  font-size: 14px;
  line-height: 30px;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 600px;
  overflow: auto;
}

/* 全局提示 = 对标 layer.msg 居中深条（V2UserList 同款；覆盖 carbon.css 顶部白卡片形态）。 */
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
