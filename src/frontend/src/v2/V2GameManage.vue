<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「游戏管理」v2（/v2/game-manage）。旧页 = island 复刻页 `layui-island/pages/game-gameserver.ts`
 * （对标 `/Game/gameserver`）。本页按 `docs/plans/2026-11-05-v2-game-and-platform-pages.md` §3.1
 * 等价重建：功能与文案逐字照旧页（出处见常量区行号），视觉换炭火。结构 = 工具条
 * （刷新/新增/已删除/计数）→ 5 列表格（游戏Id/名称/图标/行内排序/操作）→ 新增游戏弹层
 * （597 款平台游戏库 radio + 27 字母片 + 搜索下拉，同旧页高亮动作）→ 已删除弹层（软删 +
 * 恢复）→ 删除四段确认。换图标 = 三步直传（ticket → PUT 原始字节 → claim），PUT 不走共享 axios。
 * 有意差异（设计稿 §6 登记）：① 旧页隐藏的「游戏Id」列在 v2 显示（报修报号）；② 游戏名称
 * 纯文本（旧页蓝链是 0 调用死代码）；③ 排序值未变更不发请求（旧页不判变更）；④ 删除确认
 * 四段以独立段落呈现（旧页 `<br>` 拼串）；⑤ 新增成功 toast + 关层（旧页 layer.alert 链）；
 * ⑥ 已删除弹层恢复后保留并重取本层 + 主表（旧页关层）。
 * ⛔ 不 import layui 页模块；唯一例外 = `directUpload` 的 `putFileToStorage`（其 import 面只有
 * `api/csrf-cookie` + `api/http`，无 layui UI 依赖——设计稿 §3.1 核实后点名放行）。
 * 权限门 fail-closed：`session.canNode(['game.server'])`，无节点不画数据面、不发请求。
 */
import { onMounted, ref } from 'vue'
import { request } from '@/api/http'
import { createConfig, removeConfig, updateConfig, type ConfigRow } from '@/api/config'
import type { LibraryGame } from '@/config/game-library'
import { fetchGameLibrary } from '@/api/gameLibrary'
import { putFileToStorage } from '@/layui-island/directUpload'
import { useSessionStore } from '@/stores/session'
import {
  formatDeletedAt,
  listAllConfig,
  restoreConfig,
  type DeletedConfigRow,
} from '@/v2/configList'

/* ── 逐字文案（出处 = island 复刻页 / 证据件，⛔ 自造）───────────────────────── */

const REFRESH_OK = '已刷新' // game-gameserver.ts L508
const SORT_NOT_NUMBER = '排序值只能是数字' // 同上 L741
const SORT_SAVED = '排序已保存' // 同上 L745
const DELETE_HEAD = '确定将游戏删除吗？' // deleteWarning L179
const DELETE_TAIL = '删除后它会（连同下面的分类与项目）从录单和各页下拉里消失；' // L181
const DELETED_NOTE = '历史订单不受影响，以后可在工具栏的「已删除」里恢复。' // config-replica.ts L276-277
const DELETE_OK = '删除成功' // game-gameserver.ts L659
const PICKER_TITLE = '新增游戏' // 同上 L577
const PICK_CHARS_LABEL = '按字母索引' // 证据件 game-addeditgame.html
const PICK_SEARCH_LABEL = '搜索游戏名称' // 同上
const PICK_LIST_LABEL = '请选择游戏' // 同上
const PICK_SEARCH_PLACEHOLDER = '请选择或搜索' // 同上
const PICK_SEARCH_BTN = '搜索' // 同上
const PICK_SUBMIT = '确定添加到本系统' // 同上
const PICKER_QUOTE = '如果列表里没有找到需要添加的游戏，可以联系团团通客服或技术添加' // 证据件 blockquote（产品名已按本仓口径作「团团通」）
const FOUND_MESSAGE = '已找到，请注意下方红色字体' // game-gameserver.ts L307
const NOT_PICKED_MESSAGE = '请选择要添加的游戏' // 同上 L309
const CREATED_MESSAGE = '新增成功' // 同上 L311
const ADD_CHARS = [
  '全部', 'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M',
  'N', 'O', 'P', 'Q', 'R', 'S', 'T', 'U', 'V', 'W', 'X', 'Y', 'Z',
] as const // 同上 L298-301，顺序即对标顺序
const ALL_CHAR = '全部' // 同上 L304
const RELOGO_TEXT = '换图标' // logoCellHtml L218
const LOGO_UPDATED = '图标已更新' // game-gameserver.ts L539
const LOGO_ACCEPT = 'image/jpeg,image/png' // 同上 L131
const DELETED_TITLE = '已删除的游戏' // 同上 L675
const DELETED_EMPTY = '没有已删除的记录' // config-replica.ts L318
const RESTORED_OK = '已恢复' // config-replica.ts L374
const NO_PERMISSION = '无权限查看本页'
const LIBRARY_FAILED = '游戏库加载失败，请刷新重试'

function truncatedText(n: number, m: number): string {
  return `数据太多，只显示前 ${n} 条（共 ${m} 条）` // game-gameserver.ts L494
}

/* ── 行类型与基础 ─────────────────────────────────────────────────────────── */

type GameRow = ConfigRow & {
  logo_url?: string | null
  deleted_at: string | null
  order_count: number
  category_count: number
  project_count: number
}

type GameLogoTicket = { key: string; upload_url: string; expires_in?: number }

const RESOURCE = 'games' as const

function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

const session = useSessionStore()
const canManage = session.canNode(['game.server'])

/* ── toast（页面自绘，同 V2OrderRecycle：info 1500 / error 6000）────────────── */

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

/* ── 主表取数（无服务端分页：自动翻页拉全量，条数进工具条）──────────────────── */

const rows = ref<GameRow[]>([])
const total = ref(0)
const loading = ref(false)

async function reload(): Promise<boolean> {
  loading.value = true
  try {
    const page = await listAllConfig<GameRow>(RESOURCE)
    rows.value = page.items
    total.value = page.total
    if (page.truncated) showToast(truncatedText(page.items.length, page.total))
    return true
  } catch (e) {
    rows.value = []
    total.value = 0
    showToast(errText(e), 'error')
    return false
  } finally {
    loading.value = false
  }
}

async function refreshAll(): Promise<void> {
  if (await reload()) showToast(REFRESH_OK)
}

/* ── 行内排序（blur/Enter 提交；空值按 0；未变更不发；非整数拦截 + 重取）──────── */

const sortSavingId = ref<number | null>(null)

async function commitSort(row: GameRow, event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const raw = input.value.trim()
  const value = raw === '' ? 0 : Number(raw)
  if (!Number.isInteger(value)) {
    showToast(SORT_NOT_NUMBER, 'error')
    await reload()
    return
  }
  // 值未变更不发请求（有意差异：旧页不判变更）；enter+blur 双触发由在途闸收口。
  if (value === row.sort_order || sortSavingId.value !== null) return
  sortSavingId.value = row.id
  try {
    await updateConfig(RESOURCE, row.id, { sort_order: value })
    showToast(SORT_SAVED)
    await reload()
  } catch (e) {
    showToast(errText(e), 'error')
    await reload()
  } finally {
    sortSavingId.value = null
  }
}

/* ── 删除（软删；确认 = 四段，条件计数段仅 >0 时出）─────────────────────────── */

const delDlg = ref<GameRow | null>(null)
const deleting = ref(false)

/** 四段确认文案（照 island `deleteWarning` L172-184；抽纯函数便于 spec 钉值）。 */
function deleteWarningSegments(row: GameRow): string[] {
  const parts: string[] = []
  if (row.category_count > 0) parts.push(`${row.category_count} 个项目分类`)
  if (row.project_count > 0) parts.push(`${row.project_count} 个常用项目`)
  if (row.order_count > 0) parts.push(`${row.order_count} 笔订单`)
  const segs: string[] = [DELETE_HEAD]
  if (parts.length > 0) segs.push(`该游戏下面有 ${parts.join('、')}。`)
  segs.push(DELETE_TAIL)
  segs.push(DELETED_NOTE)
  return segs
}

async function doRemove(): Promise<void> {
  const row = delDlg.value
  if (row === null || deleting.value) return
  deleting.value = true
  try {
    await removeConfig(RESOURCE, row.id)
    delDlg.value = null
    showToast(DELETE_OK)
    await reload()
  } catch (e) {
    showToast(errText(e), 'error')
    await reload()
  } finally {
    deleting.value = false
  }
}

/* ── 新增一款游戏（从平台游戏库挑，⛔ 不是自由填名）────────────────────────── */

const pickerOpen = ref(false)
/** 优先用租户库里的平台游戏库；接口失败/为空时回落内置清单，保证弹层可用。 */
const libraryRows = ref<LibraryGame[]>([])
const pickedName = ref('')
const activeChar = ref<string>(ALL_CHAR)
const searchName = ref('')
const creating = ref(false)
/** 高亮状态（同旧页两种：字母命中 sel / 搜索命中 sel2；清一个先清另一个）。 */
const hlChar = ref('')
const hlValue = ref('')

function openPicker(): void {
  pickedName.value = ''
  activeChar.value = ALL_CHAR
  searchName.value = ''
  hlChar.value = ''
  hlValue.value = ''
  pickerOpen.value = true
}

function highlightByLetter(letter: string): void {
  hlValue.value = ''
  activeChar.value = letter
  hlChar.value = letter === ALL_CHAR ? '' : letter
}

/** 下拉/搜索钮的同一动作：返回命中个数（「已找到」提示只由下拉选中那路弹，照旧页）。 */
function highlightByValue(value: string): number {
  hlChar.value = ''
  hlValue.value = value
  if (value === '') return 0
  return libraryRows.value.filter((g) => g[1] === value).length
}

function onSearchPicked(value: string): void {
  if (highlightByValue(value) > 0) showToast(FOUND_MESSAGE)
}

function onSearchBtn(): void {
  highlightByValue(searchName.value)
}

function radioClass(item: LibraryGame): Record<string, boolean> {
  return {
    'gm-hit-char': hlChar.value !== '' && item[0] === hlChar.value,
    'gm-hit-search': hlValue.value !== '' && item[1] === hlValue.value,
  }
}

async function submitCreate(): Promise<void> {
  if (creating.value) return
  if (pickedName.value === '') {
    showToast(NOT_PICKED_MESSAGE)
    return
  }
  creating.value = true
  try {
    await createConfig(RESOURCE, { name: pickedName.value })
    showToast(CREATED_MESSAGE)
    pickerOpen.value = false
    await reload()
  } catch (e) {
    // 失败不关层（旧页同口径：可改选后重试）。
    showToast(errText(e), 'error')
  } finally {
    creating.value = false
  }
}

/* ── 已删除弹层（软删列表 + 恢复）────────────────────────────────────────── */

const deletedOpen = ref(false)
const deletedRows = ref<DeletedConfigRow[]>([])
const deletedLoading = ref(false)
const restoringId = ref<number | null>(null)

async function reloadDeleted(): Promise<void> {
  deletedLoading.value = true
  try {
    const page = await listAllConfig<DeletedConfigRow>(RESOURCE, { deleted: 1 })
    deletedRows.value = page.items
  } catch (e) {
    deletedRows.value = []
    showToast(errText(e), 'error')
  } finally {
    deletedLoading.value = false
  }
}

function openDeleted(): void {
  deletedOpen.value = true
  void reloadDeleted()
}

async function doRestore(id: number): Promise<void> {
  if (restoringId.value !== null) return
  restoringId.value = id
  try {
    await restoreConfig(RESOURCE, id)
    showToast(RESTORED_OK)
    await reloadDeleted()
    await reload()
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    restoringId.value = null
  }
}

/* ── 换图标（三步直传：ticket → PUT 原始字节 → claim；⛔ 不走共享 axios 实例）── */

const logoInput = ref<HTMLInputElement | null>(null)
let logoGameId = 0

function logoFileExt(name: string): string {
  const dot = name.lastIndexOf('.')
  return dot >= 0 ? name.slice(dot + 1).toLowerCase() : ''
}

function logoInitial(name: string): string {
  const trimmed = name.trim()
  return trimmed === '' ? '游' : trimmed.slice(0, 1)
}

function pickLogo(id: number): void {
  logoGameId = id
  logoInput.value?.click()
}

async function onLogoFile(): Promise<void> {
  const file = logoInput.value?.files?.[0]
  if (!file || logoGameId <= 0) return
  try {
    const ticket = await request<GameLogoTicket>({
      method: 'post',
      url: `/api/business/games/${logoGameId}/logo/ticket`,
      data: { ext: logoFileExt(file.name) },
    })
    // ② 原始字节直传（同源补 CSRF、跨域裸发）：COS 预签名带本站头/credentials 会 403。
    await putFileToStorage(ticket.upload_url, file)
    await request<unknown>({
      method: 'post',
      url: `/api/business/games/${logoGameId}/logo`,
      data: { key: ticket.key },
    })
    showToast(LOGO_UPDATED)
    await reload()
  } catch (e) {
    showToast(errText(e), 'error')
    await reload()
  } finally {
    if (logoInput.value) logoInput.value.value = ''
  }
}

onMounted(() => {
  if (!canManage) return
  void fetchGameLibrary()
    .then((rows) => {
      libraryRows.value = rows.map((row) => [row.first_key, row.name])
    })
    .catch(() => {
      libraryRows.value = []
      showToast(LIBRARY_FAILED, 'error')
    })
  void reload()
})
</script>

<template>
  <div class="cv2-page" data-v2-page="game-manage">
    <template v-if="canManage">
      <div class="cv2-toolbar" data-v2="toolbar">
        <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--find" data-v2="refresh" :disabled="loading" @click="refreshAll">刷新游戏列表</button>
        <button type="button" class="cv2-btn cv2-btn--commit" data-v2="add" @click="openPicker">新增一款游戏</button>
        <button type="button" class="cv2-btn cv2-btn--quiet" data-v2="deleted" @click="openDeleted">已删除</button>
        <span class="cv2-toolbar-count" data-v2="count">共 {{ total }} 款游戏</span>
      </div>

      <section class="cv2-panel cv2-table-panel" aria-label="游戏列表">
        <table class="cv2-table">
          <colgroup>
            <col style="width: 90px" />
            <col />
            <col style="width: 150px" />
            <col style="width: 220px" />
            <col style="width: 110px" />
          </colgroup>
          <thead>
            <tr>
              <th class="cv2-th-num">游戏Id</th>
              <th>游戏名称</th>
              <th class="cv2-th-center">图标</th>
              <th class="cv2-th-center">游戏排序(数据越大排序越靠前)</th>
              <th class="cv2-th-center">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="row.id" class="cv2-row" :data-v2-row="row.id">
              <td class="cv2-td-num" :data-v2-game-id="row.id">{{ row.id }}</td>
              <td :data-v2-game-name="row.id">{{ row.name }}</td>
              <td class="cv2-td-center" :data-v2-logo="row.id">
                <img
                  v-if="(row.logo_url ?? '').trim() !== ''"
                  class="gm-logo-img"
                  :src="row.logo_url ?? ''"
                  alt=""
                />
                <span v-else class="gm-logo-initial">{{ logoInitial(row.name) }}</span>
                <button type="button" class="cv2-actlink gm-relogo" :data-v2-relogo="row.id" @click="pickLogo(row.id)">{{ RELOGO_TEXT }}</button>
              </td>
              <td class="cv2-td-center">
                <input
                  class="cv2-input gm-sort-input"
                  :value="row.sort_order"
                  :data-v2-sort-input="row.id"
                  @change="commitSort(row, $event)"
                  @keydown.enter.prevent="commitSort(row, $event)"
                />
              </td>
              <td class="cv2-td-center">
                <button type="button" class="cv2-actlink cv2-actlink--danger" :data-v2-del="row.id" @click="delDlg = row">删除游戏</button>
              </td>
            </tr>
            <tr v-if="rows.length === 0">
              <td colspan="5" class="cv2-empty" data-v2="empty">
                <template v-if="loading">正在加载…</template>
                <template v-else>暂无数据</template>
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <input
        ref="logoInput"
        type="file"
        class="gm-logo-file"
        :accept="LOGO_ACCEPT"
        data-v2="logo-file"
        style="display: none"
        @change="onLogoFile"
      />

      <!-- 新增游戏：597 款平台游戏库挑一款（radio 网格 + 字母片 + 搜索下拉，同旧页高亮动作） -->
      <el-dialog
        :model-value="pickerOpen"
        :title="PICKER_TITLE"
        class="cv2-dialog cv2-dialog--lg"
        data-v2="dlg-add"
        @update:model-value="pickerOpen = false"
      >
        <div class="gm-field">
          <span class="gm-field-label">{{ PICK_CHARS_LABEL }}</span>
          <div class="gm-chars" data-v2="pick-chars">
            <button
              v-for="c in ADD_CHARS"
              :key="c"
              type="button"
              class="gm-char"
              :class="{ 'gm-char--on': activeChar === c }"
              :data-v2-char="c"
              @click="highlightByLetter(c)"
            >{{ c }}</button>
          </div>
        </div>
        <div class="gm-field">
          <span class="gm-field-label">{{ PICK_SEARCH_LABEL }}</span>
          <div class="gm-search-row">
            <el-select
              v-model="searchName"
              filterable
              :teleported="false"
              :placeholder="PICK_SEARCH_PLACEHOLDER"
              class="gm-search-select"
              data-v2="pick-search"
              @change="onSearchPicked"
            >
              <el-option v-for="g in libraryRows" :key="g[1] + g[0]" :value="g[1]" :label="g[1]" />
            </el-select>
            <button type="button" class="cv2-btn" data-v2="pick-search-btn" @click="onSearchBtn">{{ PICK_SEARCH_BTN }}</button>
          </div>
        </div>
        <div class="gm-field">
          <span class="gm-field-label">{{ PICK_LIST_LABEL }}</span>
          <div class="gm-radios" data-v2="pick-list">
            <label
              v-for="g in libraryRows"
              :key="g[1] + g[0]"
              class="gm-radio"
              :class="radioClass(g)"
              :data-firstkey="g[0]"
              :data-v2-game-radio="g[1]"
            >
              <input v-model="pickedName" type="radio" name="gm-pick-game" :value="g[1]" />
              <span>{{ g[1] }}</span>
            </label>
          </div>
        </div>
        <p class="gm-quote" data-v2="pick-quote">{{ PICKER_QUOTE }}</p>
        <template #footer>
          <div class="cv2-dlg-actions">
            <button type="button" class="cv2-btn cv2-btn--submit" data-v2="pick-submit" :disabled="creating" @click="submitCreate">{{ PICK_SUBMIT }}</button>
          </div>
        </template>
      </el-dialog>

      <!-- 已删除的游戏：软删列表 + 恢复 -->
      <el-dialog
        :model-value="deletedOpen"
        :title="DELETED_TITLE"
        class="cv2-dialog cv2-dialog--md"
        data-v2="dlg-deleted"
        @update:model-value="deletedOpen = false"
      >
        <table class="cv2-table">
          <colgroup>
            <col />
            <col style="width: 150px" />
            <col style="width: 90px" />
          </colgroup>
          <thead>
            <tr>
              <th>名称</th>
              <th>删除时间</th>
              <th class="cv2-th-center">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in deletedRows" :key="row.id" :data-v2-deleted-row="row.id">
              <td>{{ row.name }}</td>
              <td class="cv2-td-time">{{ formatDeletedAt(row.deleted_at) }}</td>
              <td class="cv2-td-center">
                <button type="button" class="cv2-actlink" :data-v2-restore="row.id" :disabled="restoringId !== null" @click="doRestore(row.id)">恢复</button>
              </td>
            </tr>
            <tr v-if="deletedRows.length === 0">
              <td colspan="3" class="cv2-empty" data-v2="deleted-empty">
                <template v-if="deletedLoading">正在加载…</template>
                <template v-else>{{ DELETED_EMPTY }}</template>
              </td>
            </tr>
          </tbody>
        </table>
      </el-dialog>

      <!-- 删除确认（四段：问句 + 条件计数段 + 后果 + 恢复说明；逐字照 island deleteWarning） -->
      <el-dialog
        :model-value="delDlg !== null"
        title="提示"
        class="cv2-dialog cv2-dialog--sm"
        data-v2="dlg-del-confirm"
        @update:model-value="delDlg = null"
      >
        <p v-for="(seg, i) in (delDlg === null ? [] : deleteWarningSegments(delDlg))" :key="i" class="cv2-dlg-text gm-del-seg" data-v2="del-seg">{{ seg }}</p>
        <template #footer>
          <div class="cv2-dlg-actions">
            <button type="button" class="cv2-btn cv2-btn--quiet" @click="delDlg = null">取消</button>
            <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--commit" data-v2="confirm-del" :disabled="deleting" @click="doRemove">确定</button>
          </div>
        </template>
      </el-dialog>
    </template>

    <section v-else class="cv2-panel">
      <p class="cv2-empty" data-v2="no-permission">{{ NO_PERMISSION }}</p>
    </section>

    <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
      {{ toast.text }}
    </div>
  </div>
</template>

<style scoped>
/* 本页新增形状收在这里；色一律 var(--tt2-*) token（⛔ 裸色值）。 */

/* 图标格：40px 圆角缩略图 / 首字色块（色走 done 族蓝签 token）。 */
.gm-logo-img {
  width: 40px;
  height: 40px;
  border-radius: 6px;
  object-fit: cover;
  vertical-align: middle;
}

.gm-logo-initial {
  display: inline-block;
  width: 40px;
  height: 40px;
  line-height: 40px;
  border-radius: 6px;
  background: var(--tt2-st-done-bg);
  color: var(--tt2-st-done);
  font-weight: 600;
  text-align: center;
  vertical-align: middle;
}

.gm-relogo {
  margin-left: 8px;
}

.gm-sort-input {
  width: 72px;
  text-align: center;
}

/* 字母片（旧证据件 36×25 方块的炭火版）。 */
.gm-chars {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}

.gm-char {
  min-width: 36px;
  height: 25px;
  padding: 0 6px;
  border: 1px solid var(--tt2-line);
  border-radius: 2px;
  background: var(--tt2-panel);
  color: var(--tt2-dim);
  font-family: inherit;
  font-size: 13px;
  cursor: pointer;
}

.gm-char:hover {
  border-color: var(--tt2-primary);
  color: var(--tt2-primary-deep);
}

.gm-char--on {
  border-color: var(--tt2-primary);
  color: var(--tt2-primary);
}

.gm-field {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 8px 12px;
  align-items: start;
  margin-bottom: 14px;
}

.gm-field-label {
  padding-top: 5px;
  font-size: 13px;
  color: var(--tt2-dim);
  white-space: nowrap;
}

.gm-search-row {
  display: flex;
  gap: 8px;
}

.gm-search-select {
  width: 260px;
}

/* 597 项 radio 网格（旧页每项 200px 宽的炭火版）。 */
.gm-radios {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
  max-height: 320px;
  overflow-y: auto;
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
  padding: 8px 10px;
}

.gm-radio {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  width: 200px;
  font-size: 13px;
  cursor: pointer;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

/* 字母命中（旧 sel 橙）与搜索命中（旧 sel2 红）——色走 token：琥珀暂停族 / 金额红。 */
.gm-hit-char {
  color: var(--tt2-st-paused);
  font-weight: 600;
}

.gm-hit-search {
  color: var(--tt2-money-red);
  font-weight: 600;
}

/* 弹层里的橙色引文块（旧 blockquote #ff6a00 的 token 同值档）。 */
.gm-quote {
  margin: 4px 0 0;
  padding: 10px 14px;
  border: 1px solid var(--tt2-line);
  border-left: 3px solid var(--tt2-summary-orange);
  border-radius: 4px;
  color: var(--tt2-summary-orange);
  font-size: 13px;
}

.gm-del-seg + .gm-del-seg {
  margin-top: 8px;
}
</style>
