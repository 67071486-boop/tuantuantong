// `request` 在 `http.ts`（**业务信封**请求的统一传输 / 认证层出口）；`Page<T>` 在 `unwrap.ts`（信封与分页形状）。
// 两处都在既有的 `api/` 分层里，本文件不 import 任何 UI 库（骨架 spec §7.4 的边界）。
//
// ⚠️ 上面那句**不**意味着「`api/` 下每条请求都经 `request()`」——本文件的数据页路径经 `request()`，
// 但 `api/` 下另有网络出口（例如 `api/performances.ts` 的导出直接 `http.get<Blob>`，
// 因为它的成功响应是 `.xlsx` 字节、不是统一信封）；这里不数条目、也不下因果结论（E-0011），
// 现状与非穷举的说明以 `http.ts` 的模块注释为准。
import { fetchMe } from './auth'
import { request } from './http'
import type { Page } from './unwrap'

/** 四类配置资源的读取。 */
export type ConfigRow = {
  id: number
  name: string
  sort_order: number
  is_active: boolean
  created_at: string
}

/** 软删除 + 引用计数（后端 `ConfigReferenceCountField` 与 `deleted_at`，D-0085）。- `deleted_at`：`null` = 没删。删除（软删）后行留在库里，`?deleted=1` 才查得到；- `*_count`：**还活着**的下级各有多少个（删除前那句警告、以及「会连带下架什么」靠它）；游戏页的 `project_count` 是跨一层算的（`categories__projects`）。 */
export type SoftDeleteFields = {
  deleted_at: string | null
  order_count: number
}

/** 区服 / 分类带所属游戏（Task 1 的后端过滤参数按它走）。三个子类字段逐条来自 `apps/business/api/serializers.py` 的三个序列化器（`ProjectCategorySerializer` 的 `assigned_platforms`/`title_prefix`/`title_suffix`、`ProjectSerializer` 的 `category`/`base_price`）。 */
export type ServerRow = ConfigRow & { game: number }
export type CategoryRow = ConfigRow &
  SoftDeleteFields & {
    game: number
    /** 逗号拼接的平台 id 串（`"8,30,90"`），展示名走 `config/platforms.ts`。 */
    assigned_platforms: string | null
    title_prefix: string | null
    title_suffix: string | null
    /** 这个分类下面还有几个活着的常用项目（删分类会一起下架它们）。 */
    project_count: number
  }
export type ProjectRow = ConfigRow &
  SoftDeleteFields & { category: number; base_price: string | null }

/** `page_size` 取 **200** 条（后端上限现为 **300**：`EnvelopePagination.max_page_size`；本页未顶格）。
 * 这四类配置是**下拉选项**，不是列表页——分页会静默漏项（选不到第 201 个区服，而页面不会报错）。
 * 200 以内是现在能拿到的最小代价；超过 200 条配置的租户要等后续做「可搜索下拉 + 远程过滤」。 */
const CONFIG_PAGE_SIZE = 200

/** **全量**游戏字典（`GET /api/business/games/` 的一次 `page_size=200` 取数，不做任何收窄）。⚠️ 它只给**配置面**用：游戏维护页（订单类型分类 / 常用项目）的游戏下拉，以及账号页「游戏权限」多选（系统用户管理的「设置游戏权限」弹窗、新增/编辑账号页）。为什么必须留一个「不看 `game_ids`」的出口：那些控件要能勾上**自己当前没有**的游戏，否则「看不到没勾的游戏 ⇒ 也就勾不上」是死锁（计划 DoD #4）。⛔ **数据页不要用它**：数据页的筛选/录入下拉一律走 `fetchGames()`（按权限收窄）。 */
export async function fetchAllGames(): Promise<ConfigRow[]> {
  const page = await request<Page<ConfigRow>>({
    method: 'get',
    url: '/api/business/games/',
    params: { page_size: CONFIG_PAGE_SIZE },
  })
  return page.items
}

/** 当前用户**有权访问**的游戏字典——数据页的筛选 / 录单 / FK 名映射用它。全量列表照常取（后端 `/api/business/games/` 保持全量），再按 `/api/auth/me` 的`game_ids` 在前端收窄。⚠️ **空 `game_ids` ⇒ 空列表**（不是全量）：这是本线的「严格」口径，`?? []` 是 fail-closed——夹具/旧后端漏了这个字段时只会让下拉变空，不会放开别家游戏。⚠️ 每次调用都会重新读一次 `/api/auth/me`（不缓存）：会话在页面生命周期内可能变（登出/换号），缓存一份「当前用户的游戏」会把上一个会话的权限带过来。代价是每发一次游戏字典多一发小请求。管理员不受影响：后端给管理员的 `game_ids` 就是全部游戏。 */
export async function fetchGames(): Promise<ConfigRow[]> {
  const [games, me] = await Promise.all([fetchAllGames(), fetchMe()])
  const allowed = new Set(me.game_ids ?? [])
  return games.filter((g) => allowed.has(g.id))
}

/** `game` 为空时不带过滤——后端把空串当「没传」（Task 1 Step 1 的 `test_blank_parent_filter_is_treated_as_absent`）。 */
export async function fetchServers(gameId?: number | null): Promise<ServerRow[]> {
  const params = gameId ? { game: gameId, page_size: CONFIG_PAGE_SIZE } : { page_size: CONFIG_PAGE_SIZE }
  const page = await request<Page<ServerRow>>({
    method: 'get',
    url: '/api/business/servers/',
    params,
  })
  return page.items
}

export async function fetchCategories(gameId?: number | null): Promise<CategoryRow[]> {
  const params = gameId ? { game: gameId, page_size: CONFIG_PAGE_SIZE } : { page_size: CONFIG_PAGE_SIZE }
  const page = await request<Page<CategoryRow>>({
    method: 'get',
    url: '/api/business/categories/',
    params,
  })
  return page.items
}

export async function fetchProjects(categoryId?: number | null): Promise<ProjectRow[]> {
  const params = categoryId
    ? { category: categoryId, page_size: CONFIG_PAGE_SIZE }
    : { page_size: CONFIG_PAGE_SIZE }
  const page = await request<Page<ProjectRow>>({
    method: 'get',
    url: '/api/business/projects/',
    params,
  })
  return page.items
}

/* ——— 列表页（游戏维护三页）用的 CRUD 出口 ———
 *
 * 与上面四个 `fetch*` 的分工是**语义**差异，不是重复：
 *  - `fetch*` 是**下拉字典**语义——`page_size=200` 一次拉全、只回 `items`（录单页/详情页在用）；
 *  - 本组是**列表页**语义——回整页 `Page<T>`（要 `total` 渲染分页控件）、可按父级与页码取。
 * **不要**为了「统一」把两边合并：合并后必有一边牺牲语义（下拉丢掉 total，或列表丢掉 200 上限）。
 */

export type ConfigResourceName = 'games' | 'categories' | 'projects'

/** 列表查询参数。`page`/`page_size` 由调用方给；父级字段只在对应页面传。 */
export type ConfigListParams = {
  page?: number
  page_size?: number
  game?: number
  category?: number
}

/** 资源的 URL。**导出**是给 island 复刻页（`layui-island/pages/*.ts`）复用同一处端点拼装——
 * 端点在两处各拼一遍的话，改一处漏一处是迟早的事（E-0010 的形状）。 */
export function configUrl(resource: ConfigResourceName, id?: number): string {
  const base = `/api/business/${resource}/`
  return id === undefined ? base : `${base}${id}/`
}

/** 只丢 `undefined` / `null`（与 `orders.ts` 的 `toOrderParams` 同口径）：`0` 与 `''` 一律保留。 */
function toListParams(params: ConfigListParams): Record<string, number> {
  return Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null),
  ) as Record<string, number>
}

export async function listConfig<T extends ConfigRow>(
  resource: ConfigResourceName,
  params: ConfigListParams = {},
): Promise<Page<T>> {
  return request<Page<T>>({
    method: 'get',
    url: configUrl(resource),
    params: toListParams(params),
  })
}

export async function createConfig<T extends ConfigRow>(
  resource: ConfigResourceName,
  payload: Record<string, unknown>,
): Promise<T> {
  return request<T>({ method: 'post', url: configUrl(resource), data: payload })
}

/** 改一条配置。`patch` **只放改动字段**——整对象回写会把并发改的另一列覆盖掉。后端序列化器是 `ModelSerializer`，`PATCH` 是部分更新。成功回 HTTP **200**（不是 201/204）：`EnvelopeModelViewSet.finalize_response`把 DRF 默认的 201/204 统一改回 200（`src/apps/business/api/base.py:81-84`），因为「业务结果只读 code」。所以前端按普通信封解包即可，**不需要**给写请求单开一条分支。 */
export async function updateConfig<T extends ConfigRow>(
  resource: ConfigResourceName,
  id: number,
  patch: Record<string, unknown>,
): Promise<T> {
  return request<T>({ method: 'patch', url: configUrl(resource, id), data: patch })
}

/** 删除。被引用时后端回 200 + `code 1001`（`ProtectedError` 被显式接住），会走 `BizError`。 */
export async function removeConfig(
  resource: ConfigResourceName,
  id: number,
): Promise<null> {
  return request<null>({ method: 'delete', url: configUrl(resource, id) })
}
