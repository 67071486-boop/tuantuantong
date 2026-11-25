/**
 * 【唯一】layui island 入口的脚本：按 `?page=<key>` 挑页面模块并挂载。
 *
 * ## 新增页面只需一个文件（并行开发不冲突）
 *
 * 在 `src/layui-island/pages/` 下新建 `<key>.ts`，**默认导出**一个 `IslandPage`：
 *
 * ```ts
 * import { replicaPage } from '@/layui/replicaPage'
 * export default replicaPage({ ... })
 * ```
 *
 * 注册表由 `import.meta.glob` 自动发现，**不需要改本文件**（历史上这里是一张手写表，
 * 多线并行必冲突；D-0025 的分工约定因此改为「一个页面一个文件」）。
 * `key` 就是**文件名去掉 `.ts`**，必须与路由 `meta.islandPage` 逐字一致。
 *
 * 约定：key 用 `kebab-case`，与被复刻的对标路由同义
 * （`system-user-list` ← `/User/userlist`；`system-roleauthority` ← `/System/roleauthority`）。
 */
import { fetchMe } from '@/api/auth'
import { orderFinanceDirectConfigured } from '@/api/orderFinanceDirect'
import type { IslandContext, IslandPage } from '@/layui/island'

type IslandModule = { default: IslandPage }
type IslandLoader = () => Promise<IslandModule>

/** 页面注册表：`./pages/*.ts` → key = 文件名（**只扫一层**，`pages/__tests__/` 不会被扫到）。 */
const REGISTRY: Record<string, IslandLoader> = Object.fromEntries(
  Object.entries(import.meta.glob<IslandModule>('./pages/*.ts')).map(([path, loader]) => [
    path.replace(/^\.\/pages\//, '').replace(/\.ts$/, ''),
    loader,
  ]),
)

const root = document.getElementById('app')
const pageKey = new URLSearchParams(window.location.search).get('page') ?? ''

/** 跳转交给父窗口（Vue 壳）执行：iframe 里没有 vue-router。 */
function onNavigate(name: string, params?: Record<string, string>): void {
  window.parent.postMessage({ type: 'island:navigate', name, params }, window.location.origin)
}

/** 供测试与自检使用：已登记的页面 key 列表（排序后）。 */
export function registeredPageKeys(): string[] {
  return Object.keys(REGISTRY).sort()
}

if (root) {
  const loader = REGISTRY[pageKey]
  if (!loader) {
    // 未登记的 key：明确报出来，别留白屏（多页构建下最容易出的错就是「页面没注册」）。
    root.textContent = `未登记的 island 页面：${pageKey}`
  } else {
    loader()
      .then(async (mod) => {
        if (orderFinanceDirectConfigured()) await fetchMe().catch(() => undefined)
        return mod.default.mount(root, { onNavigate } satisfies IslandContext)
      })
      .catch((e: unknown) => {
        root.textContent = e instanceof Error ? e.message : '页面加载失败'
      })
  }
}
