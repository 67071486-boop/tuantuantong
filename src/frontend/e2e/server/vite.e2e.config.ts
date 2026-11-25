/**
 * E2E 专用前端配置。
 *
 * 复用主配置的工厂（`createViteConfig`）的**其余部分**：插件、`resolve`、`build`，
 * 以及 `server` 上除 `proxy` 以外的形状。
 * `server.proxy` 是**整体覆盖**（不是「只改端口」）：工厂里 `proxyToBackend` 的 `target`
 * 写死成开发后端 `BACKEND_ORIGIN`（`http://127.0.0.1:8000`，定义在 `vite.config.ts` 顶部），
 * 而 E2E 必须换成 `BACKEND_URL`（8010）。
 * ⚠️ 「显式把租户域名写进 `Host`、且 `changeOrigin` 必须为 false」这一段在这里**确实是第二份**
 * （下面的 `headers` 与两条 proxy 条目复刻了工厂里 `proxyToBackend` 的做法）⇒ **有漂移面**；
 * 本线不改生产配置，该漂移面由控制方记入残差。
 */
import { createViteConfig } from '../../vite.config.ts'
import { ACTIVE_TENANT, BACKEND_URL, E2E_TOOLS_DIR } from '../config.mjs'

export default (() => {
  const base = createViteConfig('development')
  // 租户域名决定后端按 `Host` 路由到哪个租户库 ⇒ 必须跟着模式走：
  // 读模式 = `e2e.test.local`（只读租户 `tt_e2e`）；写模式 = `e2e-write.test.local`（一次性写租户）。
  const headers = { Host: ACTIVE_TENANT.domain }
  return {
    ...base,
    // 现有 Playwright 用例专项审计旧版 iframe 界面。
    define: { ...base.define, __LEGACY_ROUTE_AUDIT__: 'true' },
    // 与开发服务（`node_modules/.vite`）分开，避免两份配置来回切时反复触发依赖预构建。
    cacheDir: `${E2E_TOOLS_DIR}/.vite`,
    server: {
      ...base.server,
      strictPort: true,
      proxy: {
        '/healthz': { target: BACKEND_URL, changeOrigin: false, headers },
        '/api': { target: BACKEND_URL, changeOrigin: false, headers },
      },
    },
  }
})()
