<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/** 分类按游戏加载；写操作分别受分类和常用项目权限控制。 */
import { computed, onMounted, reactive, ref } from 'vue'
import {
  createConfig,
  fetchAllGames,
  removeConfig,
  updateConfig,
  type CategoryRow,
  type ConfigRow,
  type ProjectRow,
} from '@/api/config'
import { PLATFORMS, parsePlatformIds, serializePlatformIds } from '@/config/platforms'
import { useSessionStore } from '@/stores/session'
import {
  formatDeletedAt,
  listAllConfig,
  restoreConfig,
  type DeletedConfigRow,
} from '@/v2/configList'

/* ── 逐字文案（出处 = island `game-projectsetting.ts`，⛔ 自造）───────────────── */

const CATEGORY_NODE = 'game.order_type_categories' // 旧页 L63
const PROJECT_NODE = 'game.common_projects' // 旧页 L64
const NO_GAME_MESSAGE = '还没有游戏，请先到「游戏管理」新增一款游戏' // 旧页 L460/L821
const CAT_EMPTY_TEXT = '还没有项目分类，点上方「新增分类」' // 旧页 L809
const PROJ_EMPTY_TEXT = '这个分类下还没有常用项目' // 旧页 L816
const NO_CATEGORY_MESSAGE = '该游戏还没有项目分类，请先新增一个分类' // 旧页 L587
const REFRESH_OK = '已刷新' // 旧页 L697
const CAT_NAME_EMPTY = '分类名称不能为空' // validateCategoryForm L97
const PROJ_NAME_EMPTY = '项目名称不能为空' // validateProjectForm L105
const ORDER_EMPTY = '排序值不能为空，哪怕写个数字0' // 同上两函数 L98/L106
const ORDER_NOT_NUMBER = '排序值只能是数字' // 同上 L99/L107
const CATEGORY_REQUIRED = '请选择项目分类' // 旧页 L606
const SORT_HINT = '数字越大越靠前' // 旧页 L235 aux
const SAVE_OK = '保存成功' // 旧页 L571/L625
const CREATE_OK = '新增成功' // 同上
const DELETE_OK = '删除成功' // 旧页 L655/L666
const RESTORED_OK = '已恢复' // config-replica.ts L374
const DELETED_EMPTY = '没有已删除的记录' // config-replica.ts L318
const DELETED_CAT_TITLE = '已删除的项目分类' // 旧页 L683
const DELETED_PROJ_TITLE = '已删除的常用项目' // 同上
const NO_PERM_CAT = '无权限管理项目分类'
const NO_PERM_PROJ = '无权限管理常用项目'

function truncatedText(n: number, m: number, unit: string): string {
  return unit === '分类'
    ? `分类太多，只显示了前 ${n} 条（共 ${m} 条）` // 旧页 L473
    : `项目太多，只显示了前 ${n} 条（共 ${m} 条）` // 旧页 L432
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

/* ── 会话与权限（页面载入不拦；按钮按节点分收）──────────────────────────────── */

const session = useSessionStore()
const canManageCategories = session.canNode([CATEGORY_NODE])
const canManageProjects = session.canNode([PROJECT_NODE])

/* ── toast ───────────────────────────────────────────────────────────────── */

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

/* ── 字典与主从取数 ───────────────────────────────────────────────────────── */

const games = ref<ConfigRow[]>([])
const selectedGameId = ref<number | null>(null)
const categories = ref<CategoryRow[]>([])
const projects = ref<ProjectRow[]>([])
const selectedCategoryId = ref<number | null>(null)
const loading = ref(false)

let projectRequest = 0
let categoryRequest = 0

function categoryOptionLabel(row: CategoryRow): string {
  return `${row.name}（${row.project_count ?? 0} 个项目）` // 旧页 categoryOptionLabel L113
}

async function loadProjects(categoryId: number | null): Promise<void> {
  const request = ++projectRequest
  if (categoryId === null) {
    projects.value = []
    return
  }
  try {
    const page = await listAllConfig<ProjectRow>('projects', { category: categoryId })
    if (request !== projectRequest) return
    projects.value = page.items
    if (page.truncated) showToast(truncatedText(page.items.length, page.total, '项目'))
  } catch (e) {
    if (request !== projectRequest) return
    projects.value = []
    showToast(errText(e), 'error')
  }
}

/** 重取分类（preserveSelection = 保住当前选中）；无游戏时清空并提示。 */
async function loadCategories(preserveSelection: boolean): Promise<void> {
  const request = ++categoryRequest
  loading.value = true
  try {
    const game = selectedGameId.value
    if (game === null) {
      categories.value = []
      projects.value = []
      selectedCategoryId.value = null
      showToast(NO_GAME_MESSAGE)
      return
    }
    const page = await listAllConfig<CategoryRow>('categories', { game })
    if (request !== categoryRequest) return
    categories.value = page.items
    const keep = preserveSelection && categories.value.some((c) => c.id === selectedCategoryId.value)
    if (!keep) selectedCategoryId.value = null
    if (page.truncated) showToast(truncatedText(page.items.length, page.total, '分类'))
    await loadProjects(selectedCategoryId.value)
  } catch (e) {
    if (request !== categoryRequest) return
    categories.value = []
    selectedCategoryId.value = null
    showToast(errText(e), 'error')
  } finally {
    if (request === categoryRequest) loading.value = false
  }
}

function selectCategory(id: number): void {
  selectedCategoryId.value = id === selectedCategoryId.value ? null : id
  projects.value = []
  void loadProjects(selectedCategoryId.value)
}

function onGameChange(): void {
  selectedCategoryId.value = null
  categories.value = []
  void loadProjects(null)
  void loadCategories(false)
}

async function refreshAll(): Promise<void> {
  try {
    const fresh = await fetchAllGames()
    games.value = fresh
    // 保住用户当前选的游戏（旧页 fillGameOptions 的 preserveId 口径）；被删了才回落第一款。
    if (!fresh.some((g) => g.id === selectedGameId.value)) {
      selectedGameId.value = fresh[0]?.id ?? null
    }
  } catch (e) {
    showToast(errText(e), 'error')
    return
  }
  showToast(REFRESH_OK)
  await loadCategories(true)
}

onMounted(() => {
  void (async () => {
    try {
      games.value = await fetchAllGames()
    } catch (e) {
      showToast(errText(e), 'error')
      return
    }
    selectedGameId.value = games.value[0]?.id ?? null
    await loadCategories(false)
  })()
})

/* ── 分类弹层（新增/编辑）─────────────────────────────────────────────────── */

const catDlgOpen = ref(false)
const catEditing = ref<CategoryRow | null>(null)
const catSaving = ref(false)
const catForm = reactive({
  name: '',
  orderRaw: '0',
  titlePrefix: '',
  titleSuffix: '',
  platformIds: [] as number[],
})

function openCategoryDialog(row: CategoryRow | null): void {
  catEditing.value = row
  catForm.name = row?.name ?? ''
  catForm.orderRaw = String(row?.sort_order ?? 0)
  catForm.titlePrefix = row?.title_prefix ?? ''
  catForm.titleSuffix = row?.title_suffix ?? ''
  catForm.platformIds = parsePlatformIds(row?.assigned_platforms)
  catDlgOpen.value = true
}

/** 本地校验（同序同文案照旧页 validateCategoryForm）。返回提示文案或 null（通过）。 */
function validateCategoryForm(): string | null {
  if (catForm.name.trim() === '') return CAT_NAME_EMPTY
  if (catForm.orderRaw.trim() === '') return ORDER_EMPTY
  if (!/^\d+$/.test(catForm.orderRaw.trim())) return ORDER_NOT_NUMBER
  return null
}

async function saveCategory(): Promise<void> {
  if (catSaving.value) return
  const invalid = validateCategoryForm()
  if (invalid !== null) {
    showToast(invalid)
    return
  }
  const body = {
    name: catForm.name.trim(),
    sort_order: Number(catForm.orderRaw.trim()),
    title_prefix: catForm.titlePrefix.trim(),
    title_suffix: catForm.titleSuffix.trim(),
    assigned_platforms: serializePlatformIds(catForm.platformIds),
  }
  catSaving.value = true
  try {
    if (catEditing.value !== null) {
      await updateConfig('categories', catEditing.value.id, body)
      showToast(SAVE_OK)
    } else {
      const created = await createConfig<CategoryRow>('categories', {
        ...body,
        game: selectedGameId.value,
      })
      selectedCategoryId.value = created.id
      showToast(CREATE_OK)
    }
    catDlgOpen.value = false
    await loadCategories(true)
  } catch (e) {
    // 失败不关层（可改后重试，旧页同口径）。
    showToast(errText(e), 'error')
  } finally {
    catSaving.value = false
  }
}

/* ── 项目弹层（新增/编辑；编辑中改选分类后右侧跟随，旧页 L624 行为）───────────── */

const projDlgOpen = ref(false)
const projEditing = ref<ProjectRow | null>(null)
const projSaving = ref(false)
const projForm = reactive({
  name: '',
  orderRaw: '0',
  category: null as number | null,
})

function openProjectDialog(row: ProjectRow | null): void {
  if (categories.value.length === 0) {
    showToast(NO_CATEGORY_MESSAGE)
    return
  }
  projEditing.value = row
  projForm.name = row?.name ?? ''
  projForm.orderRaw = String(row?.sort_order ?? 0)
  projForm.category = row?.category ?? selectedCategoryId.value ?? categories.value[0]?.id ?? null
  projDlgOpen.value = true
}

/** 本地校验（同序同文案照旧页 validateProjectForm + L603 的分类必选）。 */
function validateProjectForm(): string | null {
  if (projForm.name.trim() === '') return PROJ_NAME_EMPTY
  if (projForm.orderRaw.trim() === '') return ORDER_EMPTY
  if (!/^\d+$/.test(projForm.orderRaw.trim())) return ORDER_NOT_NUMBER
  if (projForm.category === null || !Number.isInteger(projForm.category) || projForm.category <= 0) {
    return CATEGORY_REQUIRED
  }
  return null
}

async function saveProject(): Promise<void> {
  if (projSaving.value) return
  const invalid = validateProjectForm()
  if (invalid !== null) {
    showToast(invalid)
    return
  }
  const category = projForm.category as number
  const body = {
    name: projForm.name.trim(),
    sort_order: Number(projForm.orderRaw.trim()),
    category,
  }
  projSaving.value = true
  try {
    if (projEditing.value !== null) {
      await updateConfig('projects', projEditing.value.id, body)
      showToast(SAVE_OK)
    } else {
      await createConfig<ProjectRow>('projects', body)
      showToast(CREATE_OK)
    }
    // 项目可能被挪到别的分类：跟着它走（用户刚选的那个分类才是他现在想看的，旧页 L624）。
    selectedCategoryId.value = category
    projDlgOpen.value = false
    await loadCategories(true)
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    projSaving.value = false
  }
}

/* ── 删除（软删；确认中段是条件句——计数为 0 不出该行）────────────────────────── */

const delDlg = ref<{ kind: 'categories' | 'projects'; row: CategoryRow | ProjectRow } | null>(null)
const deleting = ref(false)

function catDeleteSegments(row: CategoryRow): string[] {
  const count = row.project_count ?? 0
  const segs = [`确定删除分类「${row.name}」吗？`]
  if (count > 0) segs.push(`这个分类下面还有 ${count} 个常用项目，会一起下架。`)
  segs.push('历史订单不受影响，以后可在「已删除分类」里恢复。')
  return segs
}

function projDeleteSegments(row: ProjectRow): string[] {
  const orders = row.order_count ?? 0
  const segs = [`确定删除项目「${row.name}」吗？`]
  if (orders > 0) segs.push(`这个项目被 ${orders} 笔订单引用。`)
  segs.push('删除后会从录单下拉里消失，以后可在「已删除项目」里恢复。')
  return segs
}

function confirmRemove(kind: 'categories' | 'projects', row: CategoryRow | ProjectRow): void {
  delDlg.value = { kind, row }
}

const delSegments = computed(() => {
  const dlg = delDlg.value
  if (dlg === null) return []
  return dlg.kind === 'categories'
    ? catDeleteSegments(dlg.row as CategoryRow)
    : projDeleteSegments(dlg.row as ProjectRow)
})

async function doRemove(): Promise<void> {
  const dlg = delDlg.value
  if (dlg === null || deleting.value) return
  deleting.value = true
  try {
    await removeConfig(dlg.kind, dlg.row.id)
    if (dlg.kind === 'categories' && selectedCategoryId.value === dlg.row.id) {
      selectedCategoryId.value = null
    }
    delDlg.value = null
    showToast(DELETE_OK)
    await loadCategories(dlg.kind !== 'categories')
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    deleting.value = false
  }
}

/* ── 已删除弹层 ×2（按当前游戏过滤，旧页 openDeleted 的 params 口径）───────────── */

const deletedCatOpen = ref(false)
const deletedProjOpen = ref(false)
const deletedCatRows = ref<DeletedConfigRow[]>([])
const deletedProjRows = ref<DeletedConfigRow[]>([])

function requireGame(): number | null {
  if (selectedGameId.value === null) {
    showToast(NO_GAME_MESSAGE)
    return null
  }
  return selectedGameId.value
}

async function openDeleted(kind: 'categories' | 'projects'): Promise<void> {
  const game = requireGame()
  if (game === null) return
  if (kind === 'categories') {
    deletedCatOpen.value = true
    try {
      const page = await listAllConfig<DeletedConfigRow>('categories', { game, deleted: 1 })
      deletedCatRows.value = page.items
    } catch (e) {
      deletedCatRows.value = []
      showToast(errText(e), 'error')
    }
  } else {
    deletedProjOpen.value = true
    try {
      const page = await listAllConfig<DeletedConfigRow>('projects', { game, deleted: 1 })
      deletedProjRows.value = page.items
    } catch (e) {
      deletedProjRows.value = []
      showToast(errText(e), 'error')
    }
  }
}

async function doRestore(kind: 'categories' | 'projects', id: number): Promise<void> {
  try {
    await restoreConfig(kind, id)
    showToast(RESTORED_OK)
    await openDeleted(kind)
    await loadCategories(true)
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

/* ── 空态文案（权限优先，其次无游戏 / 无分类 / 空列表）───────────────────────── */

const catEmptyText = computed(() => {
  if (!canManageCategories) return NO_PERM_CAT
  if (games.value.length === 0) return NO_GAME_MESSAGE
  return CAT_EMPTY_TEXT
})

const projEmptyText = computed(() => {
  if (!canManageProjects) return NO_PERM_PROJ
  if (categories.value.length === 0) return NO_CATEGORY_MESSAGE
  return PROJ_EMPTY_TEXT
})
</script>

<template>
  <div class="cv2-page" data-v2-page="game-projects">
    <!-- 筛选行：游戏下拉（无「全部」、默认第一款，照旧页）+ 刷新 + 按节点分收的动作钮 + 汇总 -->
    <section class="cv2-panel cv2-filter" aria-label="筛选" data-v2="filters">
      <div class="cv2-filter-row">
        <div class="cv2-field" data-v2-filter-group="game">
          <span class="cv2-field-label">选择游戏</span>
          <el-select
            v-model="selectedGameId"
            filterable
            :teleported="false"
            placeholder="选择游戏"
            data-v2-filter="game"
            @change="onGameChange"
          >
            <el-option v-for="g in games" :key="g.id" :value="g.id" :label="g.name" />
          </el-select>
        </div>
        <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--find" data-v2="refresh" :disabled="loading" @click="refreshAll">刷新数据</button>
        <button v-if="canManageCategories" type="button" class="cv2-btn cv2-btn--commit" data-v2="add-category" @click="openCategoryDialog(null)">新增分类</button>
        <button v-if="canManageProjects" type="button" class="cv2-btn cv2-btn--commit" data-v2="add-project" @click="openProjectDialog(null)">新增常用项目</button>
        <button v-if="canManageCategories" type="button" class="cv2-btn cv2-btn--quiet" data-v2="deleted-categories" @click="openDeleted('categories')">已删除分类</button>
        <button v-if="canManageProjects" type="button" class="cv2-btn cv2-btn--quiet" data-v2="deleted-projects" @click="openDeleted('projects')">已删除项目</button>
        <span class="cv2-toolbar-count" data-v2="summary">分类 {{ categories.length }} 个</span>
      </div>
    </section>

    <section class="cv2-panel gp-list" data-v2="cat-panel">
      <h2 class="cv2-panel-title">分类名称<span class="gp-count" data-v2="cat-count">（{{ categories.length }} 个）</span></h2>
      <p v-if="categories.length === 0" class="cv2-empty" data-v2="cat-empty">{{ catEmptyText }}</p>
      <div v-for="row in categories" :key="row.id" class="gp-category" :data-v2-cat-row="row.id">
        <div class="gp-category-head">
          <button type="button" class="gp-toggle" :data-v2-cat-toggle="row.id" :aria-expanded="row.id === selectedCategoryId" @click="selectCategory(row.id)">
            <span aria-hidden="true">{{ row.id === selectedCategoryId ? '▾' : '▸' }}</span> {{ row.name }}
          </button>
          <div v-if="canManageCategories" class="gp-actions">
            <button type="button" class="cv2-actlink" :data-v2-cat-edit="row.id" @click="openCategoryDialog(row)">编辑</button>
            <button type="button" class="cv2-actlink cv2-actlink--danger" :data-v2-cat-del="row.id" @click="confirmRemove('categories', row)">删除</button>
          </div>
        </div>
        <section v-if="row.id === selectedCategoryId" class="gp-projects" data-v2="proj-panel">
          <div class="gp-project-head">
            <strong>常用项目</strong><span class="gp-count" data-v2="proj-count">（{{ projects.length }} 个）</span>
            <button v-if="canManageProjects" type="button" class="cv2-btn cv2-btn--commit cv2-btn--fill" :data-v2-add-project-for="row.id" @click="openProjectDialog(null)">新增常用项目</button>
          </div>
          <table class="cv2-table gp-table">
            <colgroup><col /><col style="width: 64px" /><col style="width: 110px" /></colgroup>
            <thead><tr><th>项目名称</th><th class="cv2-th-center">排序</th><th class="cv2-th-center">操作</th></tr></thead>
            <tbody>
              <tr v-for="project in projects" :key="project.id" class="cv2-row" :data-v2-proj-row="project.id">
                <td><span :title="project.name">{{ project.name }}</span></td>
                <td class="cv2-td-center">{{ project.sort_order }}</td>
                <td class="cv2-td-center gp-actions">
                  <template v-if="canManageProjects">
                    <button type="button" class="cv2-actlink" :data-v2-proj-edit="project.id" @click="openProjectDialog(project)">编辑</button>
                    <button type="button" class="cv2-actlink cv2-actlink--danger" :data-v2-proj-del="project.id" @click="confirmRemove('projects', project)">删除</button>
                  </template>
                  <span v-else>—</span>
                </td>
              </tr>
              <tr v-if="projects.length === 0"><td colspan="3" class="cv2-empty" data-v2="proj-empty">{{ projEmptyText }}</td></tr>
            </tbody>
          </table>
        </section>
      </div>
    </section>

    <!-- 编辑保留已有的发平台与标题设置。 -->
    <el-dialog
      :model-value="catDlgOpen"
      :title="catEditing === null ? '新增项目分类' : `编辑分类：${catEditing.name}`"
      class="cv2-dialog cv2-dialog--md"
      data-v2="dlg-category"
      @update:model-value="catDlgOpen = false"
    >
      <div class="cv2-field gp-form-field">
        <span class="cv2-field-label">分类名称</span>
        <el-input v-model="catForm.name" maxlength="64" data-v2-field="name" />
      </div>
      <div class="gp-sort-field">
        <div class="cv2-field gp-form-field">
          <span class="cv2-field-label">排序</span>
          <el-input v-model="catForm.orderRaw" maxlength="9" data-v2-field="sort_order" />
        </div>
        <p class="cv2-field-hint">{{ SORT_HINT }}</p>
      </div>
      <div v-if="catEditing !== null" class="cv2-field gp-form-field gp-form-field--top">
        <span class="cv2-field-label">指定发平台</span>
        <el-checkbox-group v-model="catForm.platformIds" class="gp-platforms" data-v2="platforms">
          <el-checkbox
            v-for="p in PLATFORMS"
            :key="p.id"
            :value="p.id"
            :data-v2-platform="p.id"
          >{{ p.label }}</el-checkbox>
        </el-checkbox-group>
      </div>
      <div v-if="catEditing !== null" class="cv2-field gp-form-field">
        <span class="cv2-field-label">追加到标题前</span>
        <el-input v-model="catForm.titlePrefix" maxlength="50" data-v2-field="title_prefix" />
      </div>
      <div v-if="catEditing !== null" class="cv2-field gp-form-field">
        <span class="cv2-field-label">追加到标题后</span>
        <el-input v-model="catForm.titleSuffix" maxlength="50" data-v2-field="title_suffix" />
      </div>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="catDlgOpen = false">取消</button>
          <button type="button" class="cv2-btn cv2-btn--submit cv2-btn--commit" data-v2="category-save" :disabled="catSaving" @click="saveCategory">保存</button>
        </div>
      </template>
    </el-dialog>

    <!-- 项目弹层（sm）：所属分类/名称/排序 -->
    <el-dialog
      :model-value="projDlgOpen"
      :title="projEditing === null ? '新增常用代练项目' : `编辑项目：${projEditing.name}`"
      class="cv2-dialog cv2-dialog--sm"
      data-v2="dlg-project"
      @update:model-value="projDlgOpen = false"
    >
      <div class="cv2-field gp-form-field">
        <span class="cv2-field-label">所属分类</span>
        <el-select v-model="projForm.category" :teleported="false" data-v2-field="category">
          <el-option
            v-for="c in categories"
            :key="c.id"
            :value="c.id"
            :label="categoryOptionLabel(c)"
          />
        </el-select>
      </div>
      <div class="cv2-field gp-form-field">
        <span class="cv2-field-label">项目名称</span>
        <el-input v-model="projForm.name" maxlength="450" data-v2-field="name" />
      </div>
      <div class="gp-sort-field">
        <div class="cv2-field gp-form-field">
          <span class="cv2-field-label">排序</span>
          <el-input v-model="projForm.orderRaw" maxlength="9" data-v2-field="sort_order" />
        </div>
        <p class="cv2-field-hint">{{ SORT_HINT }}</p>
      </div>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="projDlgOpen = false">取消</button>
          <button type="button" class="cv2-btn cv2-btn--submit cv2-btn--commit" data-v2="project-save" :disabled="projSaving" @click="saveProject">保存</button>
        </div>
      </template>
    </el-dialog>

    <!-- 删除确认（sm；中段是条件句——计数为 0 不出该行） -->
    <el-dialog
      :model-value="delDlg !== null"
      title="提示"
      class="cv2-dialog cv2-dialog--sm"
      data-v2="dlg-del-confirm"
      @update:model-value="delDlg = null"
    >
      <p v-for="(seg, i) in delSegments" :key="i" class="cv2-dlg-text gp-del-seg" data-v2="del-seg">{{ seg }}</p>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="delDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--danger" data-v2="confirm-del" :disabled="deleting" @click="doRemove">确定删除</button>
        </div>
      </template>
    </el-dialog>

    <!-- 已删除的项目分类 / 已删除的常用项目（按当前游戏过滤） -->
    <el-dialog
      :model-value="deletedCatOpen"
      :title="DELETED_CAT_TITLE"
      class="cv2-dialog cv2-dialog--md"
      data-v2="dlg-deleted-categories"
      @update:model-value="deletedCatOpen = false"
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
          <tr v-for="row in deletedCatRows" :key="row.id" :data-v2-deleted-cat-row="row.id">
            <td>{{ row.name }}</td>
            <td class="cv2-td-time">{{ formatDeletedAt(row.deleted_at) }}</td>
            <td class="cv2-td-center">
              <button type="button" class="cv2-actlink" :data-v2-restore-cat="row.id" @click="doRestore('categories', row.id)">恢复</button>
            </td>
          </tr>
          <tr v-if="deletedCatRows.length === 0">
            <td colspan="3" class="cv2-empty" data-v2="deleted-cat-empty">{{ DELETED_EMPTY }}</td>
          </tr>
        </tbody>
      </table>
    </el-dialog>

    <el-dialog
      :model-value="deletedProjOpen"
      :title="DELETED_PROJ_TITLE"
      class="cv2-dialog cv2-dialog--md"
      data-v2="dlg-deleted-projects"
      @update:model-value="deletedProjOpen = false"
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
          <tr v-for="row in deletedProjRows" :key="row.id" :data-v2-deleted-proj-row="row.id">
            <td>{{ row.name }}</td>
            <td class="cv2-td-time">{{ formatDeletedAt(row.deleted_at) }}</td>
            <td class="cv2-td-center">
              <button type="button" class="cv2-actlink" :data-v2-restore-proj="row.id" @click="doRestore('projects', row.id)">恢复</button>
            </td>
          </tr>
          <tr v-if="deletedProjRows.length === 0">
            <td colspan="3" class="cv2-empty" data-v2="deleted-proj-empty">{{ DELETED_EMPTY }}</td>
          </tr>
        </tbody>
      </table>
    </el-dialog>

    <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
      {{ toast.text }}
    </div>
  </div>
</template>

<style scoped>
/* 本页新增形状收在这里；色一律 var(--tt2-*) token（⛔ 裸色值）。 */

.gp-list { margin-top: 14px; }
.gp-category { border-top: 1px solid var(--tt2-line); }
.gp-category-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 52px; }
.gp-toggle { flex: 1; min-width: 0; text-align: left; background: transparent; border: 0; color: var(--tt2-ink); font: inherit; font-weight: 600; cursor: pointer; padding: 14px 8px; overflow-wrap: anywhere; }
.gp-toggle:hover { color: var(--tt2-primary); }
.gp-projects { padding: 12px 16px 16px; background: var(--tt2-btn-soft-bg); }
.gp-project-head { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.gp-project-head button { margin-left: auto; }

.gp-count {
  margin-left: 8px;
  font-size: 13px;
  font-weight: 400;
  color: var(--tt2-dim);
}

.gp-actions .cv2-actlink + .cv2-actlink {
  margin-left: 10px;
}

/* 左列表在 340px 面板里放宽单元格留白。 */
.gp-table td,
.gp-table th {
  padding-left: 8px;
  padding-right: 8px;
}

.gp-form-field {
  display: flex;
  align-items: stretch;
  gap: 0;
  margin-bottom: 14px;
}

.gp-form-field .cv2-field-label {
  display: inline-flex;
  align-items: center;
  flex: 0 0 100px;
  box-sizing: border-box;
  padding: 0 10px;
  border: 1px solid var(--tt2-filter-line);
  border-right: 0;
  border-radius: 4px 0 0 4px;
  background: var(--tt2-filter-label-bg);
  font-size: 13px;
  font-weight: 400;
  color: var(--tt2-dim);
}

.gp-form-field :deep(.el-input),
.gp-form-field :deep(.el-select) {
  flex: 1 1 0;
  min-width: 0;
}

.gp-form-field :deep(.el-input__wrapper),
.gp-form-field :deep(.el-select__wrapper) {
  min-height: 38px;
  border-radius: 0 4px 4px 0;
}

.gp-sort-field .gp-form-field {
  margin-bottom: 0;
}

.gp-sort-field {
  margin-bottom: 14px;
}

.gp-form-field--top {
  align-items: stretch;
}

.gp-form-field--top .cv2-field-label {
  align-items: start;
  padding-top: 8px;
}

.gp-platforms {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 18px;
  flex: 1 1 0;
  min-width: 0;
  padding: 4px 12px;
  border: 1px solid var(--tt2-filter-line);
  border-radius: 0 4px 4px 0;
}

.gp-del-seg + .gp-del-seg {
  margin-top: 8px;
}
</style>
