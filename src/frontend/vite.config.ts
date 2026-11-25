import { fileURLToPath, URL } from 'node:url'

import { defineConfig, loadEnv, type ProxyOptions, type UserConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import vueDevTools from 'vite-plugin-vue-devtools'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

/** 开发期后端地址：Django `runserver` 的默认监听。 */
const BACKEND_ORIGIN = 'http://127.0.0.1:8000'

/**
 * 代理到后端，并**显式**把租户域名写进 `Host`。
 *
 * 为什么必须显式改写：本项目的租户由 `django-tenants` 的 `TenantMainMiddleware` 按请求的
 * `Host` 解析，而浏览器发来的是 `localhost:5173`，没有对应 `Domain` 行 → 404。
 *
 * 为什么用 `headers` 而不是 `configure` + `proxyReq` 事件：Vite 内置 http-proxy
 * （`node_modules/vite/dist/node/chunks/node.js`）里 `proxyReq` 事件只在出站请求**没有**
 * `Expect` 头时才 emit（L18743：`if (server && !proxyReq.getHeader("expect")) server.emit("proxyReq", …)`），
 * 所以走那条路的注入对带 `Expect: 100-continue` 的请求会**静默失效**
 * （判别手段是**显式**加 `-H 'Expect: 100-continue'`；本机 curl 8.7.1 对 `--data-binary` 的实测边界是
 * **1048577 字节（1MB + 1）**才自动带上，恰好 1MB 时**不带**——见 spec §16.1 的实测记录与自我更正），
 * 那类请求会被原样转发 `Host: localhost:5173`，仍然 404。`headers` 则是在 L18414-18417 **无条件**合并：
 * `if (options.headers) outgoing.headers = { ...outgoing.headers, ...options.headers }`，
 * 且 `Host` 不在任何黑名单里（`HEADER_BLACKLIST` 只有 `"trailer"`，L18386；
 * `HTTP2_HEADER_BLACKLIST` 只含 `:method/:path/:scheme/:authority/connection/keep-alive`
 * 且只在 HTTP/2 入站请求上生效，L18387-18394、L18422）。两种机制在同一环境实测对比过，
 * 见 task-7-report.md「修复轮 1」。
 *
 * `changeOrigin` 必须保持 `false`：L18439 是在 `options.headers` 合并**之后**执行的
 * `if (options.changeOrigin) outgoing.headers.host = …`，会把上面注入的 Host 覆写成目标地址
 * （`127.0.0.1:8000`），正好是这里不想要的结果。
 */
const proxyToBackend = (tenantHost: string, backendOrigin: string): ProxyOptions => ({
  target: backendOrigin,
  changeOrigin: false,
  headers: { Host: tenantHost },
})

// https://vite.dev/config/
export default defineConfig(({ mode }) => createViteConfig(mode))

/**
 * 配置工厂。`mode` 由调用方传入，因为下面的 `loadEnv` 需要真实的 `mode`
 * ——这就是这里不能用 `defineConfig({...})` 对象写法的原因。
 *
 * 单独导出（而不只是默认导出配置函数）是为了 `vitest.config.ts`：
 * vitest 的 `mergeConfig` 只接受配置**对象**，传配置函数会报 TS2345。
 */
export function createViteConfig(mode: string): UserConfig {
  // vite.config.ts 跑在 Node 里：`import.meta.env` 在这里不可用（那是浏览器侧代码用的），
  // 只能用 loadEnv。第三个参数传 '' 是为了把不带 VITE_ 前缀的键也读进来。
  const env = loadEnv(mode, process.cwd(), '')
  const tenantHost = env.VITE_TENANT_HOST || 'dev.test.local'
  const backendOrigin = env.TT_DEV_BACKEND_ORIGIN || BACKEND_ORIGIN
  // 前端挂载前缀。默认 '/'（与今天的行为完全一致）；将来挂到子路径时设 `TENANT_WEB_BASE=/console/`
  // （或用 Vite 自带的 CLI 参数 `vite build --base=/console/`，CLI 优先于这里：
  // `vite/dist/node/chunks/node.js:36908` 把 CLI 解析结果与配置 merge，CLI 在后）。
  // 刻意**不带 `VITE_` 前缀**：这个值只在 Node 侧的配置里读，浏览器侧读的是 Vite 自己注入的
  // `import.meta.env.BASE_URL`（见 src/router/index.ts）——用 VITE_ 前缀会让人误以为它是客户端配置。
  // ⚠️ 变量名**刻意取项目专有**（不叫 `APP_BASE` 这类通用名）：loadEnv 的第三个参数是空前缀，
  // 会把 shell / CI 里的同名环境变量一并读进来并**静默覆盖**配置文件与默认值；
  // 撞名的后果是产物挂在错的路径下，而现象看起来像「路由坏了」。
  const base = env.TENANT_WEB_BASE || '/'

  return {
    base,
    plugins: [
      vue(),
      vueDevTools(),
      // Element Plus 按需引入：自动引入 API（ElMessage 等）与组件（el-menu 等），
      // 并生成声明文件供 vue-tsc 使用（两个 .d.ts 已进版本库）。
      //
      // ⚠️ **测试模式下关掉两个 `.d.ts` 的写盘**（`dts: false`）。插件是在 `transform` 钩子里写它的，
      // 而写出的内容是「glob 扫到的自定义组件 + **那一次运行**加载过的 EP 组件」；**测试只加载一部分
      // 模块**，于是每跑一次 `vitest`，这两个被跟踪的文件就会被重写成**那一次的子集**（实测：跑
      // `pnpm test` 后 `components.d.ts` 变脏，跑 `pnpm build` 又把它恢复成完整集）——结果是
      // 「最后跑的是哪条命令，版本库里就提交哪一份」，甚至可能把子集提交进库。
      // 生成时机、以及为什么「陈旧/残缺」不是 `pnpm typecheck` 能查出来的，见
      // `docs/plans/2026-09-20-frontend-visual-theme.md` 的「执行后更正」(1) 与 E-0016。
      AutoImport({ resolvers: [ElementPlusResolver()], dts: mode === 'test' ? false : 'auto-imports.d.ts' }),
      Components({ resolvers: [ElementPlusResolver()], dts: mode === 'test' ? false : 'components.d.ts' }),
    ],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      proxy: {
        // 存活探针：Task 6 首页的连通性自检打的就是它。
        '/healthz': proxyToBackend(tenantHost, backendOrigin),
        // 为 P2 预留。此刻后端还没有 /api 路由，命中返回 404 —— 那是预期，不是代理没生效。
        '/api': proxyToBackend(tenantHost, backendOrigin),
        ...(env.VITE_INTERNAL_CHAT_LIVE === '1' ? {
          '/socket.io': { target: env.TT_DEV_CHAT_GATEWAY_ORIGIN || 'http://127.0.0.1:4191', ws: true },
        } : {}),
      },
    },
    build: {
      rollupOptions: {
        input: {
          // 主 SPA。
          index: fileURLToPath(new URL('./index.html', import.meta.url)),
          // 【系统人员】的 layui island（iframe 文档）。
          // ⚠️ **必须显式列进来**：Vite 的多页构建只认这里声明的 HTML，
          // 漏了就只在 dev 能访问、`vite build` 产物里没有它（页面空白且不报错）。
          'layui-island/page': fileURLToPath(
            new URL('./layui-island/page.html', import.meta.url),
          ),
          // 【打手】的手机端 H5（D-0222 / spec 2026-10-10-booster-h5-m1 §4.1）：
          // 同工程第三入口；部署侧由 nginx `location /m/` 直出 `dist/m/index.html`（§4.4）。
          // 与上面同一条纪律：不显式列进来，`vite build` 产物里就没有它。
          'm/index': fileURLToPath(new URL('./m/index.html', import.meta.url)),
        },
      },
    },
  }
}
