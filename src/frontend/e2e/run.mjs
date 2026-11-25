#!/usr/bin/env node
/**
 * E2E 编排器：确保租户 → 灌数据 → 跑 Playwright。
 *
 * ⚠️ **为什么前两步不放在 Playwright 的 `globalSetup` 里**（本线读源码实测，见 spec §1.1-1）：
 * `webServer` 会被塞进 `config.plugins`，而任务顺序是「插件 setup → globalSetup」
 * （`playwright/lib/runner/index.js:6936` → `:6321-6327`）⇒ globalSetup 跑的时候，后端**已经**被等过
 * 端口就绪、并且正连着一个还没灌数据的库。所以排序必须由本文件控制。
 *
 * ## 两种模式（`docs/specs/2026-09-25-write-mode-audit-design.md` §3/§5）
 *  - **默认（读模式）**：只读租户 `tt_e2e`，**行为逐字与今天相同**（不重置、拦断写）。
 *  - **写模式**（`TT_E2E_WRITE=1`）：一次性写租户 `tt_e2e_write`，跑前 `reset_e2e_write` 推倒重建，
 *    并把 `TT_E2E_TARGET=write` 注入给 spec（spec 据此决定放行写请求）。
 */
import { spawnSync } from 'node:child_process'
import { mkdirSync, rmSync } from 'node:fs'
import { resolve } from 'node:path'

import {
  ACTIVE_TENANT, FRONTEND_DIR, SHOTS_DIR, SRC_DIR, UV_BIN, UV_CACHE_DIR, WRITE_MODE, readTenantEnv,
} from './config.mjs'
import { ensureTenant, resetWriteTenant } from './lib/tenant.mjs'

mkdirSync(SHOTS_DIR, { recursive: true })

// 冒烟体检表的「每行一个文件」目录：**在这里清一次**。
// ⛔ 不要在 spec 的 `beforeAll` 里清——Playwright 在用例失败后会**重启 worker**、`beforeAll` 会再跑，
//    那样重启前的行会被自己清掉（正是本目录要解决的问题）。
const SMOKE_ROWS_DIR = resolve(SHOTS_DIR, '..', 'smoke-rows')
rmSync(SMOKE_ROWS_DIR, { recursive: true, force: true })
mkdirSync(SMOKE_ROWS_DIR, { recursive: true })

// 职位对拍报告（`05-role-action-audit.spec.ts`）的「每行一个文件」目录：**同 `smoke-rows` 一样在这里清一次**。
// 同一套理由：`beforeAll` 清不掉——Playwright 在用例失败后会**重启 worker**、`beforeAll` 会再跑，
// 那样重启前写下的行（含第一个失败的那个职位）会被自己清掉。
const ROLE_AUDIT_ROWS_DIR = resolve(SHOTS_DIR, '..', 'role-audit-rows')
rmSync(ROLE_AUDIT_ROWS_DIR, { recursive: true, force: true })
mkdirSync(ROLE_AUDIT_ROWS_DIR, { recursive: true })

// 1) 数据地基。
//
//    ⛔ **两条路，读模式那条逐字保持今天的行为**（spec §5：`pnpm e2e` 不带开关时必须逐字不变）：
//      - **读模式（默认）**：确保只读租户在（幂等）→ 灌夹具。下面的代码与今天**一字不差**。
//      - **写模式（`TT_E2E_WRITE=1`）**：**跑前先把一次性写租户推倒重建**（spec §2/§4）。
//        `reset_e2e_write` 自己就含「DROP → 建库建角色 → 装租户 → 灌夹具」，所以这里
//        **不做** `ensureTenant()`（那是只读租户那套：它会拿 `tenant.env` 去探活，而写模式
//        连的是另一份凭据）。
//
//    ⚠️ `resetWriteTenant()` 的任何失败都**抛**（库名闸门 / 子进程非 0 / 复核不过），
//       这里接住后**拒绝启动**（⛔ 不带着一个没重置干净的租户往下跑）。
if (WRITE_MODE) {
  console.log(
    `🔧 写模式：租户=${ACTIVE_TENANT.label} schema=${ACTIVE_TENANT.schema} ` +
      `库=${ACTIVE_TENANT.db} 域名=${ACTIVE_TENANT.domain}（凭据 ${ACTIVE_TENANT.envFile}）`,
  )
  try {
    resetWriteTenant({ label: 'run' })
  } catch (err) {
    console.error(`⛔ 写模式前置失败，E2E 未开跑：${err.message}`)
    process.exit(1)
  }
} else {
  // ⚠️ `ensureTenant()` 失败时**自己** `process.exit(1)`（见 `lib/tenant.mjs`），
  // 所以这里不包 try/catch、也不假装它能抛——它抛不出来，只会把进程带走。
  ensureTenant()
}

// 1b) 业务夹具：读模式幂等灌入；写模式已由 `reset_e2e_write` 灌过（它内部起子进程跑 seed_e2e）。
//     ⛔ 必须带着**租户凭据**跑，否则会连到 `.env` 里那个开发库上去。
if (!WRITE_MODE) {
  const seeded = spawnSync(UV_BIN, ['run', 'python', 'manage.py', 'seed_e2e'], {
    cwd: SRC_DIR,
    env: { ...process.env, ...readTenantEnv(), UV_CACHE_DIR },
    stdio: 'inherit',
  })
  if (seeded.status !== 0) {
    console.error('⛔ seed_e2e 失败，E2E 未开跑。')
    process.exit(seeded.status ?? 1)
  }
}

// 1c) **把目标注入给 spec**（spec §3 的第 2 道闸门）：`05-role-action-audit.spec.ts` 只在
//     `TT_E2E_TARGET === 'write'` 时才允许写请求真的发出去；其余一切取值（含缺省 / 拼错）
//     一律**保持拦断**（fail-closed）。
//     ⚠️ 注入点在这里（而不是让 spec 自己看 `TT_E2E_WRITE`）：模式与**租户选择**是同一次决定，
//     由本文件统一给，spec 只消费结果。worker 继承 CLI 进程的 env ⇒ 它看得到这个值。
process.env.TT_E2E_TARGET = WRITE_MODE ? 'write' : 'read'

// 2) 跑规格。额外的参数原样透传（例如 `pnpm e2e --grep 主干`）。
//    ⛔ 不要用 `pnpm e2e -- --grep …`：裸 `--` 是 Playwright 的选项终止符，
//    它后面的 `--grep` 会被当成「位置参数」忽略 ⇒ 整套照跑（实测，见 ERRORS.md 的 E-0083）。
const cli = resolve(FRONTEND_DIR, 'node_modules/@playwright/test/cli.js')
const result = spawnSync(process.execPath, [cli, 'test', '--config', 'e2e/playwright.config.ts', ...process.argv.slice(2)], {
  cwd: FRONTEND_DIR,
  stdio: 'inherit',
})
process.exit(result.status ?? 1)
