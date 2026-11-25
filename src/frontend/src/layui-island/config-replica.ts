/**
 * 「游戏维护三页」island 复刻的**共享零件**（无状态、无模块级可变变量）。
 *
 * ⚠️ 它**故意不放在 `layui-island/pages/` 下**：那个目录的 `*.ts` 会被
 * `layui-island/page.ts` 的 `import.meta.glob('./pages/*.ts')` 当成页面模块（按**文件名**作 key），
 * 而 `config/replicaRoutes.spec.ts` 有一条「pages 下每个文件都要有映射表项」的断言——
 * 放进去会让它红，而**放宽那条断言是错的**（它正是「一个页面一个文件」的守卫）。
 *
 * 为什么会有这个文件：三页（`game-gameserver` / `game-ordertypssetting` / `game-projectsetting`）
 * 的**外观骨架**是同一套（证据件取回 → 注入 → `table.render` → `form.render`），
 * 只有「端点 / 列 / 交互」不同。把它们做成一个"共用页面工厂"会踩到既有设计刻意避开的东西：
 * 三条路由各指向一个**独立组件**，就是为了让 `useConfigResource(resource, …)` 不拿着旧资源
 * 把请求打到错的端点（`router/index.ts` L99-102 的注释）。island 侧同理——三个 `pages/*.ts`
 * 必须是**各自独立**的模块。所以这里只放**零件**：
 *
 *  - 没有「当前页面」这类模块级状态（唯一的两处模块级变量是两份**加载一次**的缓存：
 *    layui 命名空间与证据件 HTML，两者都与「哪个页面」无关）；
 *  - 三页各自调用 `mountConfigLike()` 并**显式传入**自己的 `table` 配置与 `onReady` 回调，
 *    资源端点、列、事件都写在各页自己的文件里。
 *
 * 行为口径全部来自对标页 JS（读码）：
 *  - `/Game/gameserver` → `gameserver.js`：`page:false`、四个行内开关、行内 `orderNum` 编辑、
 *    删除确认语「确定将游戏删除吗？」；
 *  - `/Game/OrderTypsSetting` → `OrderTypsSetting.js`：顶部游戏下拉切游戏重载、
 *    勾选后「删除」直接删（无二次确认）、未勾选提示「请勾选要操作的数据行」；
 *  - `/Game/projectsetting` → `projectsetting.js`：同上，且编辑未改动时提示「没有任何更改」。
 */
import { fetchCsrf } from '@/api/auth'
import { configUrl, type ConfigResourceName } from '@/api/config'
import { BizError } from '@/api/errors'
import { readCsrfCookie } from '@/api/csrf-cookie'
import { bootstrapIsland, initIslandElements, type IslandContext } from '@/layui/island'
import { escapeHtml } from '@/layui-island/escape'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

/* ────────────────────────────── layui 命名空间 ────────────────────────────── */

// 三页可能被先后挂载（切页签时 iframe 不重载），layui 只需 use 一次。
let layuiPromise: Promise<LayuiNamespace> | null = null

/** 加载 layui（form / table / layer / util）＋ 注入对标覆盖样式。 */
export function loadConfigLayui(): Promise<LayuiNamespace> {
  layuiPromise ??= bootstrapIsland(['form', 'table', 'layer', 'util'])
  return layuiPromise
}

/* ────────────────────────────── 证据件 ────────────────────────────── */

const artifactCache = new Map<string, Promise<string>>()

/**
 * 取回 `public/replica/<key>.html`（页内 `<style>` 原文 + 处理过的 body 标记，**零转义**）。
 *
 * 用文件而不是内联字符串的理由与 `artifactPage` 相同（见其文件头）：对齐、可核对、
 * 且证据件里会出现反引号与 `${`。同一份 HTML 可能被重复请求（切页签回来），故按 key 缓存。
 *
 * ⚠️ **不缓存 layui 渲染结果**：每次挂载都重新注入标记，`table.render` 也是新的
 * （否则第二次挂载会拿到一个已被销毁的表格实例）。
 */
export function loadConfigArtifact(key: string): Promise<string> {
  let pending = artifactCache.get(key)
  if (!pending) {
    const url = `${import.meta.env.BASE_URL}replica/${key}.html`
    pending = fetch(url).then((res) => {
      if (!res.ok) throw new Error(`证据件取不到（HTTP ${res.status}）：replica/${key}.html`)
      return res.text()
    })
    artifactCache.set(key, pending)
  }
  return pending
}

/* ────────────────────────────── 表格 ────────────────────────────── */

/** 与 `artifactPage` 的 `buildCol` 同口径：`templet` 是函数时由页面自己给（把真数据渲染出来）。 */
export type ConfigCol = Record<string, unknown>

/**
 * 标准挂载：注入证据件 → `initIslandElements` → `form.render` → `table.render`，
 * 然后把 layui 命名空间与表格实例交给页面自己的 `onReady`（那里放该页的交互）。
 *
 * 表格数据**由页面自己取**（`data: rows`），不走 layui 的 `url`：我们的统一信封
 * （`{code,msg,data:{items,total}}`）与 CSRF/错误映射都在 `api/` 里，绕开它等于再造一套。
 *
 * @param key       证据件名（= island key = `pages/<key>.ts` 的文件名）
 * @param title     对标页的 `<title>`（island 外壳全站共用一个静态 title，各页自己设）
 * @param tableSpec layui `table.render` 的配置（`elem`/`cols`/`toolbar`/… 由页面给）
 * @param onReady   标记与表格都在之后回调；返回卸载函数
 */
export async function mountConfigLike(
  root: HTMLElement,
  opts: {
    key: string
    title: string
    tableSpec: Record<string, unknown>
    onReady: (ctx: {
      root: HTMLElement
      layui: LayuiNamespace
      form: LayuiModule
      table: LayuiModule
      layer: LayuiModule
      ctx: IslandContext
    }) => (() => void) | void
  },
  ctx: IslandContext,
): Promise<() => void> {
  const layui = await loadConfigLayui()
  const form = layui.form
  const table = layui.table as LayuiModule
  const layer = layui.layer as LayuiModule

  const html = await loadConfigArtifact(opts.key)
  document.title = opts.title
  root.innerHTML = html
  initIslandElements(layui)

  // ⚠️ **必须先 `table.render` 一次**（哪怕 `data: []`）：layui 的表格实例是在 `render` 时
  // 注册进它的实例表的，`table.reloadData(id, …)` / `table.cache[id]` / `checkStatus(id)`
  // 都按实例 id 查。不先 render 就调 reloadData，layui 只会打一行
  // `[Layui warn]: The table instance with ID 'tableList' not found` 然后**什么都不做**——
  // 现象是「页面标记在、但工具栏按钮与表格整个没渲染」（实测：parity 报 `工具栏按钮 对标2 vs 我们0`）。
  // 行数据由页面自己在 `onReady` 里取（我们的统一信封与 CSRF 都在 `api/`/本文件里，不走 layui 的 `url`）。
  ;(table.render as (o: unknown) => void)({ data: [], ...opts.tableSpec })
  ;(form.render as (type?: string) => void)()

  const teardown = opts.onReady({ root, layui, form, table, layer, ctx }) ?? undefined

  return () => {
    teardown?.()
    // 只清我们自己注入的标记：`layer` 的弹层挂在 `document.body`，由页面自己 close。
    root.innerHTML = ''
  }
}

/* ────────────────────────────── HTTP ────────────────────────────── */

/** 统一信封（与 `api/unwrap.ts` 的 `ApiResult` 同形；这里不 import 是为了少一层类型体操）。 */
type Envelope<T> = { code: number; msg: string; data: T }
type Paged<T> = { items: T[]; total: number; page: number; page_size: number }

const UNSAFE = new Set(['post', 'put', 'patch', 'delete'])

async function csrfToken(): Promise<string> {
  const existing = readCsrfCookie()
  if (existing) return existing
  // 引导：先打一次 token 端点拿到 cookie（与 `api/http.ts` 的 §4.3-2 同一处置）。
  await fetchCsrf()
  return readCsrfCookie() ?? ''
}

/**
 * 走我们的统一信封发一条请求，**返回解包后的 `data`**。
 * 业务失败（`code !== 0`）抛 `BizError`，与 `api/unwrap.ts` 的 `unwrap()` 同口径。
 *
 * 为什么不用 `api/http.ts` 的 `request()`：见文件头——三页各自 import 它当然也行，
 * 但这里刻意只依赖 `api/errors` / `api/csrf-cookie` / `api/auth` 这三个**无状态**出口，
 * 让「哪一页用了哪个端点」在三个 `pages/*.ts` 里一眼可见。
 *
 * ⚠️ 401/403 不做会话跳转：island 与父窗口同源，会话失效时下一次进出 SPA 由 `main.ts`
 * 的 401 处理器接管；这里只把服务端 `msg` 交给 layui 的 `layer.msg` 显示。
 */
export async function apiRequest<T>(
  path: string,
  init: { method?: string; body?: unknown; signal?: AbortSignal } = {},
): Promise<T> {
  const method = (init.method ?? 'get').toUpperCase()
  const headers: Record<string, string> = {}
  if (init.body !== undefined) headers['Content-Type'] = 'application/json'
  if (UNSAFE.has(method.toLowerCase())) headers['X-CSRFToken'] = await csrfToken()

  const res = await fetch(path, {
    method,
    headers,
    credentials: 'same-origin',
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
    ...(init.signal ? { signal: init.signal } : {}),
  })

  let body: unknown = null
  try {
    body = await res.json()
  } catch {
    body = null
  }

  if (res.status === 401) throw new Error('登录状态已失效，请重新登录')
  if (res.status === 403) throw new Error('没有权限执行该操作')
  if (
    typeof body !== 'object' ||
    body === null ||
    !('code' in body) ||
    !('msg' in body) ||
    !('data' in body)
  ) {
    // 未预期异常时 Django 回 HTML（不是信封），此时不能假装是业务结果。
    throw new Error(`请求失败（HTTP ${res.status}）`)
  }
  const envelope = body as Envelope<T>
  if (envelope.code !== 0) throw new BizError(envelope.code, envelope.msg || '操作失败')
  return envelope.data
}

/** `GET /api/business/<resource>/`，返回整页（要 `total`）。 */
export function listConfigPage<T>(
  resource: ConfigResourceName,
  params: Record<string, string | number> = {},
  signal?: AbortSignal,
): Promise<Paged<T>> {
  const query = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) query.set(k, String(v))
  const qs = query.toString()
  return apiRequest<Paged<T>>(`${configUrl(resource)}${qs ? `?${qs}` : ''}`, { signal })
}

/** 一次拉满的页大小（后端 `EnvelopePagination.max_page_size` 现为 300，本页保持 200，与三页原来的取值一致）。 */
export const CONFIG_PAGE_SIZE_MAX = 200
/** 自动翻页的**兜底上限**（页数）。到这个规模就停下并如实报出来，不静默截断。 */
export const CONFIG_MAX_PAGES = 25

/**
 * 把某个配置资源**全部**取回来（自动翻页）。
 *
 * 为什么需要它：对标这三页是 `page:false`（不翻页），而我们的接口上限是 200 ⇒ 超过 200 条时
 * 旧行为只弹一句「仅显示前 200 条」，**后面的一律看不到**（对「下拉选项」这种用途是静默丢项）。
 * 业务方 2026-09-22 说「不会有这么多条，但加一个兜底」⇒ 这里循环取完；真到 `CONFIG_MAX_PAGES`
 * 这种荒唐规模才停下，并把 `truncated` 交回页面**如实提示**。
 */
export async function listAllConfigPage<T>(
  resource: ConfigResourceName,
  params: Record<string, string | number> = {},
  signal?: AbortSignal,
): Promise<{ items: T[]; total: number; truncated: boolean }> {
  const first = await listConfigPage<T>(
    resource,
    { ...params, page: 1, page_size: CONFIG_PAGE_SIZE_MAX },
    signal,
  )
  const items = [...first.items]
  let page = 1
  while (items.length < first.total && page < CONFIG_MAX_PAGES) {
    page += 1
    const next = await listConfigPage<T>(
      resource,
      { ...params, page, page_size: CONFIG_PAGE_SIZE_MAX },
      signal,
    )
    // 翻页途中后端被删掉一些行时，某一页可能是空的 —— 不停就会**死循环**。
    if (next.items.length === 0) break
    items.push(...next.items)
  }
  return { items, total: first.total, truncated: items.length < first.total }
}

export function createConfigRow<T>(
  resource: ConfigResourceName,
  payload: Record<string, unknown>,
): Promise<T> {
  return apiRequest<T>(configUrl(resource), { method: 'post', body: payload })
}

export function patchConfigRow<T>(
  resource: ConfigResourceName,
  id: number,
  payload: Record<string, unknown>,
): Promise<T> {
  return apiRequest<T>(configUrl(resource, id), { method: 'patch', body: payload })
}

export function deleteConfigRow(resource: ConfigResourceName, id: number): Promise<null> {
  return apiRequest<null>(configUrl(resource, id), { method: 'delete' })
}

/* ────────────────────────────── 软删除（D-0085） ────────────────────────────── */

/** 删除确认里那句**统一的后果说明**（三页共用一份，别各写一遍）。 */
export const DELETED_NOTE =
  '历史订单不受影响，以后可在工具栏的「已删除」里恢复。'

/** 「已删除」入口的按钮文案（三页共用）。 */
export const DELETED_BUTTON_LABEL = '已删除'

/**
 * **恢复**一条软删除的配置。
 *
 * 后端的恢复就是一次普通 `PATCH {deleted_at: null}`（`SoftDeleteConfigMixin` 刻意不为它
 * 新增端点：复用既有的 `*.update` 埋点与端点契约）——所以这里也只是 `PATCH`，
 * **不是**什么新接口。
 */
export function restoreConfigRow<T>(
  resource: ConfigResourceName,
  id: number,
): Promise<T> {
  return patchConfigRow<T>(resource, id, { deleted_at: null })
}

/** ISO 时间串 → `YYYY-MM-DD HH:mm`（「已删除」列表里显示删除时间用）。取不到就原样回。 */
export function formatDeletedAt(raw: unknown): string {
  const text = String(raw ?? '')
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(text)
  return match ? `${match[1]} ${match[2]}` : text
}

/**
 * 「已删除」弹层的容器 **id**（进 `id="…"`）。
 * ⚠️ 与 `DELETED_PANEL_SELECTOR` 分成两个常量：混用会造出 `id="#x"`，选择器永远匹配不到、
 * 事件静默不生效（E-0061 ①）。
 */
export const DELETED_PANEL_ID = 'configDeletedPanel'
/** 同一个容器的 CSS 选择器（带 `#`）。 */
export const DELETED_PANEL_SELECTOR = `#${DELETED_PANEL_ID}`

/** 「已删除」弹层里那一行要显示的字段（三个资源都有 `id` / `name` / `deleted_at`）。 */
export type DeletedRow = { id: number; name: string; deleted_at: string | null }

/** 「已删除」弹层的标记（纯函数，可重跑；列表为空时给一句人话而不是空白）。 */
export function buildDeletedPanelHtml(rows: readonly DeletedRow[]): string {
  if (rows.length === 0) {
    return '<div style="padding:16px;color:#999">没有已删除的记录</div>'
  }
  const body = rows
    .map(
      (row) =>
        `<tr><td>${escapeHtml(row.name)}</td>` +
        `<td style="width:130px;color:#999">${escapeHtml(formatDeletedAt(row.deleted_at))}</td>` +
        `<td style="width:70px"><a class="layui-btn layui-btn-xs" data-restore="${row.id}">恢复</a></td></tr>`,
    )
    .join('')
  return (
    `<table class="layui-table" lay-size="sm" style="margin:0">` +
    `<thead><tr><th>名称</th><th>删除时间</th><th>操作</th></tr></thead>` +
    `<tbody>${body}</tbody></table>`
  )
}

/**
 * 打开「已删除」弹层：列出该资源**已删除**的行，每行一个「恢复」。
 *
 * 为什么用一个弹层而不是给主表加一个「已删除」模式：主表的列与工具栏是**对标复刻**出来的，
 * 加模式要动列定义、行内按钮与三页各自的交互；而「查看/恢复已删除」是我们自己的功能
 * （对标没有软删除），用一个自带的小弹层承载，与复刻部分**互不干扰**。
 *
 * @param params 取数参数——分类 / 项目页要带上当前的 `game`（否则会列出别的游戏的）
 * @param onRestored 恢复成功后的回调（页面重新取自己的列表）。返回值**忽略**
 *   （页面那个 `reload()` 回的是「取没取到」的 boolean，这里不关心）
 */
export async function openDeletedLayer(opts: {
  layer: LayuiModule
  resource: ConfigResourceName
  params?: Record<string, string | number>
  title?: string
  onRestored?: () => unknown
}): Promise<void> {
  const { layer, resource, params = {}, title = '已删除的记录' } = opts
  const msg = (text: string): void => {
    ;(layer.msg as (t: string, o?: unknown) => void)(text, { time: 1500 })
  }
  let rows: DeletedRow[]
  try {
    const page = await listAllConfigPage<DeletedRow>(resource, { ...params, deleted: 1 })
    rows = page.items
  } catch (e) {
    msg(e instanceof Error ? e.message : '加载失败')
    return
  }

  const onDocClick = (event: Event): void => {
    const link = (event.target as HTMLElement).closest<HTMLElement>('a[data-restore]')
    if (!link || !link.closest(DELETED_PANEL_SELECTOR)) return
    event.preventDefault()
    const id = Number(link.dataset.restore ?? 0)
    if (!Number.isInteger(id) || id <= 0) return
    void restoreConfigRow(resource, id).then(
      async () => {
        msg('已恢复')
        ;(layer.close as (i: number) => void)(index)
        await opts.onRestored?.()
      },
      (e: Error) => msg(e.message),
    )
  }
  document.addEventListener('click', onDocClick)
  const index = (layer.open as (o: unknown) => number)({
    type: 1,
    title,
    area: ['520px', '420px'],
    shade: [0.3, '#333'],
    shadeClose: true,
    skin: 'layui-layer-lan',
    anim: 5,
    content: `<div id="${DELETED_PANEL_ID}" style="padding:10px">${buildDeletedPanelHtml(rows)}</div>`,
    end: () => document.removeEventListener('click', onDocClick),
  })
}

/* ────────────────────────────── 下拉与表格数据 ────────────────────────────── */

/** 我们 API 的游戏行（`GameSerializer` 的字段）。 */
export type GameOption = {
  id: number
  name: string
}

/**
 * 把 `<select>` 的选项换成真实游戏。
 *
 * 对标页的服务端模板里直接内联了该租户的游戏 `<option>`（`Game__OrderTypsSetting.html` L105-118），
 * 我们只能运行期灌。**证据件里刻意留空**（那些游戏名是租户业务数据，按 §PII 红线清空），
 * 结构（`<select id="gameName" name="gameName" lay-filter="selectGame">`）与对标逐字一致。
 *
 * 需要 `form.render()` 才能把 layui 的下拉壳刷新出来（否则显示的还是空壳）。
 *
 * `preserveId` 给**刷新按钮**用：重新取回游戏列表后要保住用户当前选的那一个——
 * 重灌就跳回第一款的话，用户会以为「点刷新把游戏换了」。找不到（被删了）才回落到第一项。
 * 不传 = 老行为（选第一项）。
 */
export function fillGameOptions(
  root: HTMLElement,
  form: LayuiModule,
  games: readonly GameOption[],
  preserveId?: number | null,
): HTMLSelectElement | null {
  const select = root.querySelector<HTMLSelectElement>('select#gameName')
  if (!select) return null
  // 游戏名是用户输入 ⇒ 拼进 innerHTML 前必须转义（`escapeHtml` 是唯一出处）。
  select.innerHTML = games
    .map((g) => `<option value="${g.id}">${escapeHtml(g.name)}</option>`)
    .join('')
  const kept = preserveId === undefined || preserveId === null
    ? -1
    : games.findIndex((g) => g.id === preserveId)
  select.selectedIndex = kept >= 0 ? kept : games.length > 0 ? 0 : -1
  ;(form.render as (type?: string) => void)('select')
  return select
}

/** 当前下拉选中的游戏（`null` = 选项为空）。 */
export function selectedGameId(select: HTMLSelectElement | null): number | null {
  if (!select) return null
  const value = Number(select.value)
  return Number.isInteger(value) && value > 0 ? value : null
}

/** 当前下拉选中项的**文案**（对标 `gameName` 列渲染的就是它）。 */
export function selectedGameName(select: HTMLSelectElement | null): string {
  if (!select) return ''
  return select.options[select.selectedIndex]?.textContent ?? ''
}

/**
 * 把我们的行数据映射成「该页 `cols` 里 `field` 指向的键」。
 *
 * 为什么需要这一层：对标的列 `field` 用的是它自己的字段名（`OrderBy` / `assPlatName` /
 * `gameName` …），而我们的 API 回的是 `sort_order` / `assigned_platforms` / `game`
 * （见 `apps/business/api/serializers.py`）。`field` 只影响 layui 内部的数据索引与
 * `hide:true` 列的取值——**不改数据形状**就得不到真实值，所以这里补上别名键。
 */
export function withAliases<T extends object>(
  row: T,
  aliases: Record<string, string | number | boolean | null>,
): T & Record<string, unknown> {
  return Object.assign({}, row, aliases) as T & Record<string, unknown>
}

/* ────────────────────────────── 弹层（layer） ────────────────────────────── */

/**
 * 取出页内那个**隐藏表单节点**（对标把它放在 `<form>` 里、`display:none`，弹层时借走）。
 *
 * ⚠️ 它**可能不在 `root` 里**：layui 的 `layer.open({type:1, content: $('.addeditDiv')})`
 * 会把这个节点**搬进弹层 body**（`.layui-layer-content`），只有弹层被销毁（`layer.close`）
 * 后我们才把它放回 `<form>`（见 `openPanelLayer` 的 `end` 回调）。所以查找一律用**文档级**
 * 选择器并限定在 `.layui-layer-content` / `<form>` 两个落点内，不要写 `root.querySelector`
 * ——那样第一次关闭之后第二次就取不到节点，弹层再也打不开（实测）。
 *
 * `selector` 是**可选**的（2026-09-21 订单来源线追加）：默认值 `.addeditDiv` 让三页的既有行为
 * **逐字不变**；传别的类名（如来源管理页的 `.change`）即可复用同一条「取节点 → 借给弹层 →
 * 关闭后放回」的链路。两个落点的**层级关系**保持不变（`.layui-layer-content > <selector>`）。
 */
export function findPanel(root: HTMLElement, selector = '.addeditDiv'): HTMLElement | null {
  return (
    document.querySelector<HTMLElement>(`.layui-layer-content > ${selector}`) ??
    root.querySelector<HTMLElement>(selector)
  )
}

/**
 * 打开「新增/编辑」弹层，并在弹层关闭后把节点**放回** `<form>`。
 *
 * 两条都是实测踩出来的：
 *  1. `content` 必须传 **jQuery 对象**（`layui.$(node)`），传裸 `HTMLElement` 会让 layui 抛
 *     `TypeError: r.parents is not a function`、弹层**根本不出来**（对标原文写的是
 *     `content: $('.addeditDiv')`，就是 jQuery 对象）；
 *  2. 弹层关闭后 layui 会把那个 DOM 节点**从文档里摘掉**，不搬回去的话第二次
 *     「新增/编辑」就找不到节点（`panel()` 回 null）→ 弹层静默不出现。
 *
 * @returns 弹层索引（`layer.close` 用），节点取不到时返回 `null`
 */
export function openPanelLayer(
  root: HTMLElement,
  layui: LayuiNamespace,
  layer: LayuiModule,
  config: Record<string, unknown>,
  selector = '.addeditDiv',
): number | null {
  const node = findPanel(root, selector)
  if (!node) return null
  const host = root.querySelector<HTMLElement>('form.layui-form') ?? root
  const jq = (layui as unknown as { $: (el: Element) => unknown }).$(node)
  return (layer.open as (o: unknown) => number)({
    ...config,
    type: 1,
    content: jq,
    end: () => {
      // 弹层销毁后把节点收回 `<form>`（下一个弹层还要用它）。
      if (node.parentElement !== host) host.appendChild(node)
    },
  })
}
