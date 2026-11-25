import { defineConfig } from '@playwright/test'

import {
  ACTIVE_TENANT, BACKEND_PORT, BROWSERS_PATH, FRONTEND_DIR, FRONTEND_PORT, FRONTEND_URL,
  REPORT_JSON, SRC_DIR, TEST_RESULTS_DIR,
  UV_BIN, UV_CACHE_DIR, VITE_BIN, WRITE_MODE, readTenantEnv,
} from './config.mjs'

// 浏览器二进制目录（仓库内、不进 git），全程不写本机绝对路径。
if (!process.env.PLAYWRIGHT_BROWSERS_PATH) {
  process.env.PLAYWRIGHT_BROWSERS_PATH = BROWSERS_PATH
}

/**
 * 目标注入（spec §3 的第 2 道闸门）：`'read' | 'write'`，由本文件**按 `TT_E2E_WRITE` 推出来**。
 *
 * ## ⛔ 为什么这里必须自己推一遍、而不是只信 `run.mjs` 注入的那个值
 * spec 放行写请求的唯一条件是「`TT_E2E_TARGET === 'write'`」，而**选哪份凭据**由本文件决定。
 * 若两者能被独立设置，就存在这条**灾难路径**：
 * `TT_E2E_TARGET=write npx playwright test`（**不带** `TT_E2E_WRITE`）⇒ 后端连着**只读租户** `tt_e2e`，
 * 而 spec 以为自己在写租户上 ⇒ **对只读租户真写**。
 * 所以：本文件把「开关 → 目标」这条推导钉死，并**拒绝**任何不一致的既有注入（fail-closed）。
 * 反方向（`TT_E2E_WRITE=1` 而 worker 没拿到 `TT_E2E_TARGET`）是**安全方向**：
 * spec 会保持拦断、写模式的 W2 钉子会红，绝不会对未知租户放行写。
 *
 * ⚠️ 写进 `process.env` 是为了让 **worker**（spec 跑在那里）看得到：worker 在 config 加载**之后**
 * 才被 fork、继承主进程 env。即使这一步在某些运行方式下不生效，上面那条反方向也仍然安全。
 */
const auditTarget = WRITE_MODE ? 'write' : 'read'
if (process.env.TT_E2E_TARGET !== undefined && process.env.TT_E2E_TARGET !== auditTarget) {
  throw new Error(
    `TT_E2E_WRITE 与 TT_E2E_TARGET 不一致（前者=${WRITE_MODE ? '1' : '未设'} ⇒ 目标应为 ` +
      `${auditTarget}，后者=${JSON.stringify(process.env.TT_E2E_TARGET)}）⇒ **拒绝启动**。\n` +
      '⛔ 这条不是形式主义：`TT_E2E_TARGET=write` 单开会让 spec 放行写请求，而后端连的却是' +
      '**只读租户** tt_e2e。写模式请只给 `TT_E2E_WRITE=1`（或跑 `node e2e/run.mjs`）。',
  )
}
process.env.TT_E2E_TARGET = auditTarget

const tenantEnv = readTenantEnv(ACTIVE_TENANT.envFile)
if (!tenantEnv.POSTGRES_DB) {
  throw new Error(
    `${ACTIVE_TENANT.label}的凭据缺失（找不到 ${ACTIVE_TENANT.envFile}）：` +
      (WRITE_MODE
        ? '写模式请跑 `TT_E2E_WRITE=1 node e2e/run.mjs`（它跑前会 `reset_e2e_write` 生成凭据）。'
        : '先跑 `node e2e/lib/tenant.mjs`。'),
  )
}
// ⛔ 只判「有没有值」不够：凭据文件被重生成 / 拷错时，`POSTGRES_DB` 可能指向**开发库**或
//    **另一个 E2E 租户**，而「绝不碰开发库 / 绝不对只读租户写」是本线最硬的约束。
//    判据是**库名相等**，且必须打在**消费点**上（这里就是把它灌进后端进程 env 的那一处）
//    ——否则整套会对着被禁止的库全绿。
// 🔎 已知边界（评审修复波 E，2026-09-25）：这条闸门**只判 `POSTGRES_DB`**，⛔ 不判
//    `POSTGRES_HOST` / `POSTGRES_PORT`——凭据被手工改成"另一台服务器上恰好同名的库"时它不报。
//    现实风险低（凭据由 `provision_tenant_db` 本机写出、文件 0600、从不手工编辑）⇒ 刻意不为此
//    改造闸门架构，登记为已知边界（`docs/specs/2026-09-25-write-mode-audit-design.md` §7）。
if (tenantEnv.POSTGRES_DB !== ACTIVE_TENANT.db) {
  throw new Error(
    `E2E 凭据指向的库是 ${tenantEnv.POSTGRES_DB}，不是${ACTIVE_TENANT.label}的 ` +
      `${ACTIVE_TENANT.db}：` +
      (WRITE_MODE
        ? `写模式只连一次性写租户库。请检查 ${ACTIVE_TENANT.envFile}（或重跑 \`reset_e2e_write\`）。`
        : `本线只连 E2E 专用库。请检查 ${ACTIVE_TENANT.envFile}（或重跑 \`node e2e/lib/tenant.mjs\`）。`),
  )
}

export default defineConfig({
  testDir: './specs',
  // ⛔ 必须显式指到 .tools/ 下：默认落 src/frontend/test-results/，会把 git status 弄脏。
  outputDir: TEST_RESULTS_DIR,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  // 三个规格共用同一套租户数据 ⇒ 串行，避免互相踩。
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list'], ['json', { outputFile: REPORT_JSON }]],
  use: {
    baseURL: FRONTEND_URL,
    locale: 'zh-CN',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { viewport: { width: 1440, height: 900 } } }],
  webServer: [
    {
      command: `${UV_BIN} run python manage.py runserver 127.0.0.1:${BACKEND_PORT} --noreload`,
      cwd: SRC_DIR,
      env: {
        ...process.env,
        // 凭据（POSTGRES_DB/USER/PASSWORD）指向本轮那个租户：读模式 = tt_e2e；写模式 = tt_e2e_write。
        // 环境变量优先于 .env。
        ...tenantEnv,
        UV_CACHE_DIR,
        DJANGO_CSRF_TRUSTED_ORIGINS: `${FRONTEND_URL},http://127.0.0.1:${FRONTEND_PORT}`,
      },
      port: BACKEND_PORT,
      // 一律 false：8010 上若已有别人（或上一次残留）的服务，宁可响亮失败，也不要用错的那个。
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: 'ignore',
      stderr: 'pipe',
    },
    {
      command: `node ${VITE_BIN} --config e2e/server/vite.e2e.config.ts --port ${FRONTEND_PORT} --strictPort`,
      cwd: FRONTEND_DIR,
      // `VITE_TENANT_HOST` 决定前端把 `/api` 代理到哪个租户域名（后端按 Host 做租户路由）
      // ⇒ 必须与 `tenantEnv` 同一个租户（写模式 = `e2e-write.test.local`）。
      env: { ...process.env, VITE_TENANT_HOST: ACTIVE_TENANT.domain },
      port: FRONTEND_PORT,
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: 'ignore',
      stderr: 'pipe',
    },
  ],
})
