/**
 * 【订单项目分类 + 常用代练项目】**合并页**（island key 仍是 `game-projectsetting`，路由仍是
 * `/game-config/projects`；2026-10-17 业务方要求合并）。
 *
 * ## 为什么合并
 *
 * 业务方原话：「订单项目分类管理和常用代练项目管理……本质上他就是一个 一级分类里面加二级
 * 常用项目的功能，分开实在是不方便使用」。所以这一页是**左分类 / 右项目**的主从布局：
 * 点左边一条分类，右边就是它下面的常用项目；新增项目时分类默认取**当前选中的那条**。
 *
 * ## 与两份旧页的关系（不是复刻，是对标页的重组）
 *
 * - 数据与字段仍来自对标的两页：分类有「分类名称 / 排序 / 指定发平台 / 追加到标题前 / 追加到
 *   标题后」，项目有「项目名称 / 排序 / 所属分类」；
 * - 两份对标页的 `public/replica/*.html` **保留**（映射表 `replicaRoutes.ts` 的 89 条
 *   是手工维护的逐条转录，⛔ 不因这一页改动而删条目）；
 * - 旧的 `game-ordertypssetting.ts` 页文件已删除（入口与路由一并消失），它的独立页不再可达。
 *
 * ## 三处有意的偏离（都有理由，别"改回去"）
 *
 * 1. **合并本身**：对标是两页（`/Game/OrderTypsSetting` 与 `/Game/projectsetting`）；
 * 2. **删除改成行内单条 + 二次确认**：对标是勾选后直接批量删（连确认都没有）。合并页没有
 *    勾选列，每行「编辑 / 删除」两颗图标，删除前把「下面还有 N 个项目、N 笔订单」说清楚；
 * 3. **新增项目时分类必选且默认当前分类**：对标那页没有分类字段（见旧
 *    `game-projectsetting.ts` 文件头的来龙去脉），我们的 `Project.category` 是必填 FK。
 *
 * ## 四条 island 陷阱（与 `wecom-group-qr.ts` 同一纪律）
 *
 * ① 类名与选择器分成两个常量；② 以 `(` 开头的语句前置分号；③ 先 `bootstrapIsland` →
 * `root.innerHTML` → `initIslandElements`；④ 页里没有定时器。
 */
import { fetchMe, type User } from '@/api/auth'
import {
  fetchAllGames,
  type CategoryRow,
  type ConfigRow,
  type ProjectRow,
} from '@/api/config'
import {
  PLATFORMS,
  parsePlatformIds,
  serializePlatformIds,
} from '@/config/platforms'
import { escapeHtml } from '@/layui-island/escape'
import {
  createConfigRow,
  deleteConfigRow,
  fillGameOptions,
  listAllConfigPage,
  openDeletedLayer,
  patchConfigRow,
  selectedGameId,
  type GameOption,
} from '@/layui-island/config-replica'
import { canNode } from '@/layui-island/perms'
import { bootstrapIsland, initIslandElements, type IslandContext, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

export const PAGE_KEY = 'game-projectsetting'
export const TITLE = '项目维护'

/** 两个 RBAC 节点（合并页的「任一即可进入」由路由声明；页内按钮按各自节点收）。 */
export const CATEGORY_NODE = 'game.order_type_categories'
export const PROJECT_NODE = 'game.common_projects'

/** 分类表 / 项目表的 layui id（`table.reloadData` / `table.on` 都用它）。 */
export const CATEGORY_TABLE = 'categoryTable'
export const PROJECT_TABLE = 'projectTable'

export const CATEGORY_FORM_ID = 'mergeCategoryDialog'
export const PROJECT_FORM_ID = 'mergeProjectDialog'

/** 删除分类时那句警告（含下级项目数）。 */
export function categoryDeleteWarning(row: CategoryRow): string {
  const count = row.project_count ?? 0
  const inUse = count > 0 ? `<br>这个分类下面还有 ${count} 个常用项目，会一起下架。` : ''
  return (
    `确定删除分类「${escapeHtml(row.name)}」吗？` +
    inUse +
    '<br>历史订单不受影响，以后可在「已删除分类」里恢复。'
  )
}

/** 删除项目时那句警告（含订单引用数）。 */
export function projectDeleteWarning(row: ProjectRow): string {
  const orders = row.order_count ?? 0
  const inUse = orders > 0 ? `<br>这个项目被 ${orders} 笔订单引用。` : ''
  return (
    `确定删除项目「${escapeHtml(row.name)}」吗？` +
    inUse +
    '<br>删除后会从录单下拉里消失，以后可在「已删除项目」里恢复。'
  )
}

/** 分类表单的本地校验（文案逐字沿用旧分类页 / 对标 `submit(savebtn)`）。 */
export function validateCategoryForm(input: { name: string; orderRaw: string }): string | null {
  if (input.name === '') return '分类名称不能为空'
  if (input.orderRaw === '') return '排序值不能为空，哪怕写个数字0'
  if (!/^\d+$/.test(input.orderRaw)) return '排序值只能是数字'
  return null
}

/** 项目表单的本地校验（文案逐字沿用旧项目页 / 对标 `submit(savebtn)`）。 */
export function validateProjectForm(input: { name: string; orderRaw: string }): string | null {
  if (input.name === '') return '项目名称不能为空'
  if (input.orderRaw === '') return '排序值不能为空，哪怕写个数字0'
  if (!/^\d+$/.test(input.orderRaw)) return '排序值只能是数字'
  return null
}

/** 分类下拉里那一行的文案（`名称（N 个项目）`，给项目弹窗选分类用）。 */
export function categoryOptionLabel(row: CategoryRow): string {
  return `${row.name}（${row.project_count ?? 0} 个项目）`
}

/** 分类表的列（`canManage` = 有没有 `game.order_type_categories`）。 */
export function buildCategoryCols(canManage: boolean): Array<Record<string, unknown>> {
  return [
    {
      field: 'selected',
      title: '',
      width: 32,
      align: 'center',
      templet: (d: CategoryRow & { _selected?: boolean }) =>
        d._selected === true ? '<span style="color:#1E9FFF">▶</span>' : '',
    },
    {
      field: 'name',
      title: '分类名称',
      minWidth: 140,
      templet: (d: CategoryRow) =>
        `<a href="javascript:void(0)"` +
        ` data-act="${canManage ? 'edit-category' : ''}"` +
        ` data-id="${d.id}" style="color:#1E9FFF" title="${escapeHtml(d.name)}">${escapeHtml(d.name)}</a>`,
    },
    {
      field: 'sort_order',
      title: '排序',
      width: 64,
      align: 'center',
      templet: (d: CategoryRow) => `<span title="数字越大越靠前">${d.sort_order}</span>`,
    },
    {
      field: 'project_count',
      title: '项目',
      width: 60,
      align: 'center',
      templet: (d: CategoryRow) => `<span>${d.project_count ?? 0}</span>`,
    },
    {
      title: '操作',
      width: 84,
      align: 'center',
      templet: (d: CategoryRow) => (canManage ? categoryActions(d.id) : ''),
    },
  ]
}

/** 项目表的列（`canManage` = 有没有 `game.common_projects`）。 */
export function buildProjectCols(canManage: boolean): Array<Record<string, unknown>> {
  return [
    {
      field: 'selected',
      title: '',
      width: 32,
      align: 'center',
      templet: (d: ProjectRow & { _selected?: boolean }) =>
        d._selected === true ? '<span style="color:#1E9FFF">▶</span>' : '',
    },
    {
      field: 'name',
      title: '项目名称',
      minWidth: 200,
      templet: (d: ProjectRow) =>
        `<a href="javascript:void(0)"` +
        ` data-act="${canManage ? 'edit-project' : ''}"` +
        ` data-id="${d.id}" style="color:#1E9FFF" title="${escapeHtml(d.name)}">${escapeHtml(d.name)}</a>`,
    },
    {
      field: 'sort_order',
      title: '排序',
      width: 64,
      align: 'center',
      templet: (d: ProjectRow) => `<span>${d.sort_order}</span>`,
    },
    {
      title: '操作',
      width: 84,
      align: 'center',
      templet: (d: ProjectRow) => (canManage ? projectActions(d.id) : ''),
    },
  ]
}

function categoryActions(id: number): string {
  return (
    `<a class="layui-btn layui-btn-xs layui-btn-warm" data-act="edit-category" data-id="${id}" title="编辑分类">` +
    '<i class="layui-icon layui-icon-edit"></i></a>' +
    ` <a class="layui-btn layui-btn-xs layui-btn-danger" data-act="del-category" data-id="${id}" title="删除分类">` +
    '<i class="layui-icon layui-icon-delete"></i></a>'
  )
}

function projectActions(id: number): string {
  return (
    `<a class="layui-btn layui-btn-xs layui-btn-warm" data-act="edit-project" data-id="${id}" title="编辑项目">` +
    '<i class="layui-icon layui-icon-edit"></i></a>' +
    ` <a class="layui-btn layui-btn-xs layui-btn-danger" data-act="del-project" data-id="${id}" title="删除项目">` +
    '<i class="layui-icon layui-icon-delete"></i></a>'
  )
}

function platformBoxes(checkedRaw: string | null | undefined): string {
  const checked = new Set(parsePlatformIds(checkedRaw))
  return PLATFORMS.map(
    (p) =>
      '<label class="merge-platform" style="display:inline-block;min-width:110px;margin:2px 8px 2px 0">' +
      `<input type="checkbox" name="platform" value="${p.id}"${checked.has(p.id) ? ' checked' : ''} /> ` +
      `${escapeHtml(p.label)}</label>`,
  ).join('')
}

/** 分类弹窗的 HTML（值全部转义；`row = null` 是新增）。 */
export function categoryDialogHtml(row: CategoryRow | null): string {
  const name = escapeHtml(row?.name ?? '')
  const order = row?.sort_order ?? 0
  const before = escapeHtml(row?.title_prefix ?? '')
  const after = escapeHtml(row?.title_suffix ?? '')
  return (
    `<div id="${CATEGORY_FORM_ID}"><form class="layui-form" lay-filter="${CATEGORY_FORM_ID}">` +
    '<div class="layui-form-item"><label class="layui-form-label">分类名称</label>' +
    `<div class="layui-input-block"><input class="layui-input" name="name" maxlength="64" value="${name}" /></div></div>` +
    '<div class="layui-form-item"><label class="layui-form-label">排序</label>' +
    `<div class="layui-input-block"><input class="layui-input" name="sort_order" maxlength="9" value="${order}" />` +
    '<div class="layui-form-mid layui-word-aux">数字越大越靠前</div></div></div>' +
    '<div class="layui-form-item"><label class="layui-form-label">指定发平台</label>' +
    `<div class="layui-input-block">${platformBoxes(row?.assigned_platforms)}</div></div>` +
    '<div class="layui-form-item"><label class="layui-form-label">追加到标题前</label>' +
    `<div class="layui-input-block"><input class="layui-input" name="title_prefix" maxlength="50" value="${before}" /></div></div>` +
    '<div class="layui-form-item"><label class="layui-form-label">追加到标题后</label>' +
    `<div class="layui-input-block"><input class="layui-input" name="title_suffix" maxlength="50" value="${after}" /></div></div>` +
    '</form></div>'
  )
}

/** 项目弹窗的 HTML（`categories` 必须非空；`row = null` 是新增）。 */
export function projectDialogHtml(
  row: ProjectRow | null,
  categories: readonly CategoryRow[],
  preferredCategoryId: number | null,
): string {
  const name = escapeHtml(row?.name ?? '')
  const order = row?.sort_order ?? 0
  const wanted = row?.category ?? preferredCategoryId ?? categories[0]?.id ?? 0
  const options = categories
    .map(
      (c) =>
        `<option value="${c.id}"${c.id === wanted ? ' selected' : ''}>${escapeHtml(categoryOptionLabel(c))}</option>`,
    )
    .join('')
  return (
    `<div id="${PROJECT_FORM_ID}"><form class="layui-form" lay-filter="${PROJECT_FORM_ID}">` +
    '<div class="layui-form-item"><label class="layui-form-label">项目名称</label>' +
    `<div class="layui-input-block"><input class="layui-input" name="name" maxlength="450" value="${name}" /></div></div>` +
    '<div class="layui-form-item"><label class="layui-form-label">所属分类</label>' +
    `<div class="layui-input-block"><select name="category">${options}</select></div></div>` +
    '<div class="layui-form-item"><label class="layui-form-label">排序</label>' +
    `<div class="layui-input-block"><input class="layui-input" name="sort_order" maxlength="9" value="${order}" />` +
    '<div class="layui-form-mid layui-word-aux">数字越大越靠前</div></div></div>' +
    '</form></div>'
  )
}

/* ────────────────────────────── 页面标记 ────────────────────────────── */

const PAGE_STYLE = `
<style>
.merge-page { padding: 0 12px 12px; }
.merge-toolbar { padding: 12px 0 0; }
.merge-toolbar .layui-form-item { margin-bottom: 10px; }
.merge-actions { display: inline-block; vertical-align: middle; }
.merge-actions .layui-btn { margin-right: 6px; }
.merge-total { display: inline-block; margin-left: 8px; color: #ff6a00; }
.merge-columns { display: flex; gap: 12px; align-items: flex-start; }
.merge-col { margin: 0; }
.merge-col-categories { flex: 0 0 380px; }
.merge-col-projects { flex: 1 1 auto; min-width: 0; }
.merge-count { color: #999; font-weight: 400; margin-left: 8px; }
.merge-scope { color: #1E9FFF; margin-left: 8px; }
.merge-dialog .layui-form-item { margin-bottom: 10px; }
@media (max-width: 980px) {
  .merge-columns { display: block; }
  .merge-col-categories { margin-bottom: 12px; }
}
</style>
`

export function pageHtml(): string {
  return (
    PAGE_STYLE +
    '<div class="merge-page">' +
    '<form class="layui-form merge-toolbar" lay-filter="mergeToolbar">' +
    '<div class="layui-form-item">' +
    '<label class="layui-form-label">选择游戏</label>' +
    '<div class="layui-input-inline" style="width:220px">' +
    '<select id="gameName" name="gameName" lay-filter="selectGame"></select>' +
    '</div>' +
    '<div class="merge-actions">' +
    '<button type="button" class="layui-btn layui-btn-xs" data-act="refresh" title="刷新当前数据">' +
    '<i class="layui-icon">&#xe669;</i>刷新数据</button>' +
    '<button type="button" class="layui-btn layui-btn-xs layui-btn-danger" data-act="add-category" title="新增一个项目分类">' +
    '<i class="layui-icon layui-icon-add-1"></i>新增分类</button>' +
    '<button type="button" class="layui-btn layui-btn-xs layui-btn-danger" data-act="add-project" title="新增一个常用代练项目">' +
    '<i class="layui-icon layui-icon-add-1"></i>新增常用项目</button>' +
    '<button type="button" class="layui-btn layui-btn-xs" data-act="deleted-categories" title="查看并恢复已删除的分类">已删除分类</button>' +
    '<button type="button" class="layui-btn layui-btn-xs" data-act="deleted-projects" title="查看并恢复已删除的项目">已删除项目</button>' +
    '</div>' +
    '<div class="merge-total" id="mergeTotal"></div>' +
    '</div></form>' +
    '<div class="merge-columns">' +
    '<div class="layui-card merge-col merge-col-categories">' +
    '<div class="layui-card-header">项目分类<span class="merge-count" id="categoryCount"></span></div>' +
    '<div class="layui-card-body"><table id="' + CATEGORY_TABLE + '" lay-filter="' + CATEGORY_TABLE + '"></table></div>' +
    '</div>' +
    '<div class="layui-card merge-col merge-col-projects">' +
    '<div class="layui-card-header">常用项目<span class="merge-scope" id="projectScope"></span>' +
    '<span class="merge-count" id="projectCount"></span></div>' +
    '<div class="layui-card-body"><table id="' + PROJECT_TABLE + '" lay-filter="' + PROJECT_TABLE + '"></table></div>' +
    '</div>' +
    '</div></div>'
  )
}

/* ────────────────────────────── 页面实现 ────────────────────────────── */

export async function mountProjectPage(
  root: HTMLElement,
  _ctx: IslandContext,
): Promise<() => void> {
  const layui = await bootstrapIsland(['form', 'table', 'layer', 'util'])
  const form = layui.form as LayuiModule
  const table = layui.table as LayuiModule
  const layer = layui.layer as LayuiModule

  document.title = TITLE
  root.innerHTML = pageHtml()
  initIslandElements(layui as LayuiNamespace)

  const msg = (text: string): void => {
    ;(layer.msg as (t: string, o?: unknown) => void)(text, { time: 1600 })
  }

  const el = <T extends HTMLElement>(selector: string): T | null =>
    root.querySelector<T>(selector)

  let games: GameOption[] = []
  let me: User | null = null
  let canManageCategories = false
  let canManageProjects = false
  let categories: CategoryRow[] = []
  let projects: ProjectRow[] = []
  let selectedCategoryId: number | null = null
  let selectedProjectId: number | null = null
  let gameSelect: HTMLSelectElement | null = null
  let dialogIndex: number | null = null

  const closeDialog = (): void => {
    if (dialogIndex !== null) {
      ;(layer.close as (i: number) => void)(dialogIndex)
      dialogIndex = null
    }
  }

  const selectedCategory = (): CategoryRow | null =>
    categories.find((c) => c.id === selectedCategoryId) ?? null

  function updateCounts(totalCategories?: number): void {
    const categoryCount = el<HTMLElement>('#categoryCount')
    if (categoryCount) {
      categoryCount.textContent = `（${totalCategories ?? categories.length} 个）`
    }
    const projectCount = el<HTMLElement>('#projectCount')
    if (projectCount) projectCount.textContent = `（${projects.length} 个）`
    const scope = el<HTMLElement>('#projectScope')
    if (scope) scope.textContent = selectedCategory()?.name ?? ''
    const total = el<HTMLElement>('#mergeTotal')
    if (total) total.textContent = `分类 ${categories.length} 个 · 项目 ${projects.length} 个`
  }

  function categoryRows(): Array<CategoryRow & { _selected?: boolean }> {
    return categories.map((row) => ({
      ...row,
      _selected: row.id === selectedCategoryId,
    }))
  }

  function projectRows(): Array<ProjectRow & { _selected?: boolean }> {
    return projects.map((row) => ({
      ...row,
      _selected: row.id === selectedProjectId,
    }))
  }

  function renderCategoryTable(): void {
    ;(table.reloadData as (id: string, o: unknown) => void)(CATEGORY_TABLE, {
      data: categoryRows(),
      page: false,
    })
  }

  function renderProjectTable(): void {
    ;(table.reloadData as (id: string, o: unknown) => void)(PROJECT_TABLE, {
      data: projectRows(),
      page: false,
    })
  }

  async function loadProjects(categoryId: number | null): Promise<void> {
    selectedProjectId = null
    if (categoryId === null) {
      projects = []
      renderProjectTable()
      updateCounts()
      return
    }
    try {
      const page = await listAllConfigPage<ProjectRow>('projects', { category: categoryId })
      projects = page.items
      renderProjectTable()
      updateCounts()
      if (page.truncated) {
        msg(`项目太多，只显示了前 ${page.items.length} 条（共 ${page.total} 条）`)
      }
    } catch (e) {
      msg(e instanceof Error ? e.message : '项目加载失败')
    }
  }

  function selectCategory(id: number): void {
    selectedCategoryId = id
    renderCategoryTable()
    updateCounts()
    void loadProjects(id)
  }

  /**
   * 重取分类（`preserveSelection` = 保住当前选中的分类）。
   *
   * 新建 / 删除分类后都走它；没有分类时右侧清空并提示去新增。
   */
  async function loadCategories(preserveSelection = true, totalHint?: number): Promise<void> {
    const game = selectedGameId(gameSelect)
    if (game === null) {
      categories = []
      projects = []
      selectedCategoryId = null
      renderCategoryTable()
      renderProjectTable()
      updateCounts(0)
      msg('还没有游戏，请先到「游戏管理」新增一款游戏')
      return
    }
    try {
      const page = await listAllConfigPage<CategoryRow>('categories', { game })
      categories = page.items
      const keep = preserveSelection && categories.some((c) => c.id === selectedCategoryId)
      if (!keep) {
        selectedCategoryId = categories[0]?.id ?? null
      }
      renderCategoryTable()
      updateCounts(totalHint ?? page.total)
      if (page.truncated) {
        msg(`分类太多，只显示了前 ${page.items.length} 条（共 ${page.total} 条）`)
      }
      await loadProjects(selectedCategoryId)
    } catch (e) {
      msg(e instanceof Error ? e.message : '分类加载失败')
    }
  }

  function applyPermissions(): void {
    canManageCategories = canNode(me ?? {}, CATEGORY_NODE)
    canManageProjects = canNode(me ?? {}, PROJECT_NODE)
    const addCategory = el<HTMLButtonElement>('[data-act="add-category"]')
    if (addCategory) addCategory.style.display = canManageCategories ? '' : 'none'
    const addProject = el<HTMLButtonElement>('[data-act="add-project"]')
    if (addProject) addProject.style.display = canManageProjects ? '' : 'none'
    // 「已删除」里只有「恢复」这一个动作（PATCH `deleted_at:null`）⇒ 没有对应写节点的人
    // 点进去只能看、点恢复必 403。按各自节点隐藏，避免画一个注定失败的入口。
    const deletedCategories = el<HTMLButtonElement>('[data-act="deleted-categories"]')
    if (deletedCategories) {
      deletedCategories.style.display = canManageCategories ? '' : 'none'
    }
    const deletedProjects = el<HTMLButtonElement>('[data-act="deleted-projects"]')
    if (deletedProjects) deletedProjects.style.display = canManageProjects ? '' : 'none'
  }

  function openLayer(
    title: string,
    content: string,
    onSave: () => Promise<boolean>,
  ): void {
    closeDialog()
    const index = (layer.open as (o: unknown) => number)({
      type: 1,
      title,
      area: ['640px', 'auto'],
      maxHeight: 620,
      shade: [0.3, '#333'],
      shadeClose: false,
      skin: 'layui-layer-lan',
      anim: 5,
      content: `<div class="merge-dialog" style="padding:14px 16px 2px">${content}</div>`,
      btn: ['保存', '取消'],
      yes: (layerIndex: number) => {
        void onSave().then((ok) => {
          if (ok) (layer.close as (i: number) => void)(layerIndex)
        })
      },
      // 用户点右上角 X 关闭时也要把"当前弹层"清掉；否则下一次 open 会先 close 一个旧索引。
      end: () => {
        dialogIndex = null
      },
    })
    dialogIndex = index
    ;(form.render as (type?: string) => void)()
  }

  const dialogRoot = (id: string): HTMLElement | null => document.getElementById(id)
  const inputValue = (host: HTMLElement, name: string): string =>
    (host.querySelector<HTMLInputElement>(`[name="${name}"]`)?.value ?? '').trim()

  function openCategoryDialog(row: CategoryRow | null): void {
    if (!canManageCategories) {
      msg('没有「订单项目分类」权限')
      return
    }
    openLayer(
      row ? `编辑分类：${escapeHtml(row.name)}` : '新增项目分类',
      categoryDialogHtml(row),
      async () => {
      const host = dialogRoot(CATEGORY_FORM_ID)
      if (!host) return false
      const name = inputValue(host, 'name')
      const orderRaw = inputValue(host, 'sort_order')
      const invalid = validateCategoryForm({ name, orderRaw })
      if (invalid !== null) {
        msg(invalid)
        return false
      }
      const platformIds = Array.from(
        host.querySelectorAll<HTMLInputElement>('input[name="platform"]:checked'),
      ).map((box) => Number(box.value))
      const body = {
        name,
        sort_order: Number(orderRaw),
        title_prefix: inputValue(host, 'title_prefix'),
        title_suffix: inputValue(host, 'title_suffix'),
        assigned_platforms: serializePlatformIds(platformIds),
      }
      try {
        if (row) {
          await patchConfigRow<CategoryRow>('categories', row.id, body)
        } else {
          const created = await createConfigRow<CategoryRow>('categories', {
            ...body,
            game: selectedGameId(gameSelect),
          })
          selectedCategoryId = created.id
        }
        msg(row ? '保存成功' : '新增成功')
        await loadCategories(true)
        return true
      } catch (e) {
        msg(e instanceof Error ? e.message : '保存失败')
        return false
      }
    })
  }

  function openProjectDialog(row: ProjectRow | null): void {
    if (!canManageProjects) {
      msg('没有「常用代练项目」权限')
      return
    }
    if (categories.length === 0) {
      msg('该游戏还没有项目分类，请先新增一个分类')
      return
    }
    openLayer(
      row ? `编辑项目：${escapeHtml(row.name)}` : '新增常用代练项目',
      projectDialogHtml(row, categories, selectedCategoryId),
      async () => {
        const host = dialogRoot(PROJECT_FORM_ID)
        if (!host) return false
        const name = inputValue(host, 'name')
        const orderRaw = inputValue(host, 'sort_order')
        const invalid = validateProjectForm({ name, orderRaw })
        if (invalid !== null) {
          msg(invalid)
          return false
        }
        const categoryRaw = host.querySelector<HTMLSelectElement>('[name="category"]')?.value ?? ''
        const category = Number(categoryRaw)
        if (!Number.isInteger(category) || category <= 0) {
          msg('请选择项目分类')
          return false
        }
        try {
          if (row) {
            await patchConfigRow<ProjectRow>('projects', row.id, {
              name,
              sort_order: Number(orderRaw),
              category,
            })
          } else {
            await createConfigRow<ProjectRow>('projects', {
              name,
              sort_order: Number(orderRaw),
              category,
            })
          }
          // 项目可能被挪到别的分类：跟着它走（用户刚选的那个分类才是他现在想看的）。
          selectedCategoryId = category
          msg(row ? '保存成功' : '新增成功')
          await loadCategories(true)
          return true
        } catch (e) {
          msg(e instanceof Error ? e.message : '保存失败')
          return false
        }
      },
    )
  }

  function confirmLayer(
    text: string,
    onYes: () => Promise<void> | void,
    yesText = '确定删除',
  ): void {
    ;(layer.confirm as (t: string, o: unknown, cb: (i: number) => void) => void)(
      text,
      { title: '提示', btn: [yesText, '取消'] },
      (index: number) => {
        ;(layer.close as (i: number) => void)(index)
        void onYes()
      },
    )
  }

  async function removeCategory(row: CategoryRow): Promise<void> {
    try {
      await deleteConfigRow('categories', row.id)
      msg('删除成功')
      if (selectedCategoryId === row.id) selectedCategoryId = null
      await loadCategories(false)
    } catch (e) {
      msg(e instanceof Error ? e.message : '删除失败')
    }
  }

  async function removeProject(row: ProjectRow): Promise<void> {
    try {
      await deleteConfigRow('projects', row.id)
      msg('删除成功')
      if (selectedProjectId === row.id) selectedProjectId = null
      await loadCategories(true)
    } catch (e) {
      msg(e instanceof Error ? e.message : '删除失败')
    }
  }

  function openDeleted(kind: 'categories' | 'projects'): void {
    const game = selectedGameId(gameSelect)
    if (game === null) {
      msg('还没有游戏，请先到「游戏管理」新增一款游戏')
      return
    }
    void openDeletedLayer({
      layer,
      resource: kind,
      params: { game },
      title: kind === 'categories' ? '已删除的项目分类' : '已删除的常用项目',
      onRestored: () => loadCategories(true),
    })
  }

  async function refreshAll(): Promise<void> {
    const keep = selectedGameId(gameSelect)
    try {
      games = (await fetchAllGames()) as GameOption[]
    } catch (e) {
      msg(e instanceof Error ? e.message : '加载失败')
      return
    }
    gameSelect = fillGameOptions(root, form, games, keep)
    msg('已刷新')
    await loadCategories(true)
  }

  // ── 事件 ──
  const onRootClick = (event: Event): void => {
    const target = event.target as HTMLElement
    const action = target.closest<HTMLElement>('[data-act]')
    if (!action) return
    const act = action.dataset.act
    const id = Number(action.dataset.id ?? 0)
    if (act === 'refresh') {
      event.preventDefault()
      void refreshAll()
      return
    }
    if (act === 'add-category') {
      event.preventDefault()
      openCategoryDialog(null)
      return
    }
    if (act === 'add-project') {
      event.preventDefault()
      openProjectDialog(null)
      return
    }
    if (act === 'deleted-categories') {
      event.preventDefault()
      openDeleted('categories')
      return
    }
    if (act === 'deleted-projects') {
      event.preventDefault()
      openDeleted('projects')
      return
    }
    if (act === 'edit-category') {
      event.preventDefault()
      event.stopPropagation()
      const row = categories.find((c) => c.id === id)
      if (row) openCategoryDialog(row)
      return
    }
    if (act === 'del-category') {
      event.preventDefault()
      event.stopPropagation()
      const row = categories.find((c) => c.id === id)
      if (row) confirmLayer(categoryDeleteWarning(row), () => removeCategory(row))
      return
    }
    if (act === 'edit-project') {
      event.preventDefault()
      event.stopPropagation()
      const row = projects.find((p) => p.id === id)
      if (row) openProjectDialog(row)
      return
    }
    if (act === 'del-project') {
      event.preventDefault()
      event.stopPropagation()
      const row = projects.find((p) => p.id === id)
      if (row) confirmLayer(projectDeleteWarning(row), () => removeProject(row))
    }
  }
  root.addEventListener('click', onRootClick)

  ;(table.on as (e: string, cb: (obj: { data: CategoryRow }) => void) => void)(
    `row(${CATEGORY_TABLE})`,
    (obj) => {
      if (obj.data && obj.data.id !== selectedCategoryId) selectCategory(obj.data.id)
    },
  )
  ;(table.on as (e: string, cb: (obj: { data: ProjectRow }) => void) => void)(
    `row(${PROJECT_TABLE})`,
    (obj) => {
      selectedProjectId = obj.data?.id ?? null
      renderProjectTable()
    },
  )
  ;(form.on as (e: string, cb: () => void) => void)('select(selectGame)', () => {
    selectedCategoryId = null
    selectedProjectId = null
    void loadCategories(false)
  })
  // 兜底：layui 的 `form.on` 在某些渲染顺序下收不到原生 change（实测过），再挂一条原生的。
  const onGameChange = (): void => {
    selectedCategoryId = null
    selectedProjectId = null
    void loadCategories(false)
  }
  gameSelect = fillGameOptions(root, form, [], null)
  gameSelect?.addEventListener('change', onGameChange)

  // ── 初次装载：游戏 + 我是谁（决定按钮显隐）并行取 ──
  const [loadedGames, loadedMe] = await Promise.all([
    fetchAllGames().catch((): ConfigRow[] => []),
    fetchMe().catch((): User | null => null),
  ])
  games = loadedGames as GameOption[]
  me = loadedMe
  gameSelect = fillGameOptions(root, form, games, null)
  if (gameSelect) {
    gameSelect.removeEventListener('change', onGameChange)
    gameSelect.addEventListener('change', onGameChange)
  }
  applyPermissions()

  ;(table.render as (o: unknown) => void)({
    elem: `#${CATEGORY_TABLE}`,
    data: [],
    page: false,
    cols: [buildCategoryCols(canManageCategories)],
    text: { none: '还没有项目分类，点上方「新增分类」' },
  })
  ;(table.render as (o: unknown) => void)({
    elem: `#${PROJECT_TABLE}`,
    data: [],
    page: false,
    cols: [buildProjectCols(canManageProjects)],
    text: { none: '这个分类下还没有常用项目' },
  })

  if (games.length === 0) {
    msg('还没有游戏，请先到「游戏管理」新增一款游戏')
  }
  await loadCategories(false)

  return () => {
    root.removeEventListener('click', onRootClick)
    gameSelect?.removeEventListener('change', onGameChange)
    closeDialog()
    root.innerHTML = ''
  }
}

export function mount(root: HTMLElement, ctx: IslandContext): Promise<() => void> {
  return mountProjectPage(root, ctx)
}

export default { mount } satisfies IslandPage
