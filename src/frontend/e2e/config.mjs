/**
 * E2E 的全部常量与路径解析（**纯 JS**：编排脚本与两个 `.ts` 配置共用同一份，见 Task 1 的说明）。
 *
 * ⛔ 这里**不放**任何本机绝对路径：仓库根由 `import.meta.url` 反推，工具（uv / vite / 浏览器）
 * 一律「先看环境变量、再看仓库内的约定位置、最后回落到 PATH 里的命令」。
 *
 * ## 两套租户（写模式，`docs/specs/2026-09-25-write-mode-audit-design.md` §2/§3）
 *  - **读租户**（默认）：`tt_e2e` / `e2e` / `e2e.test.local` / `.tools/e2e/tenant.env`。行为**逐字不变**。
 *  - **一次性写租户**（`TT_E2E_WRITE=1` 才走）：`tt_e2e_write` / `e2e_write` / `e2e-write.test.local` /
 *    `.tools/e2e/tenant-write.env`。它的常量在 Python 侧还有**另一份**（T1 的 `reset_e2e_write.py`）
 *    ⇒ 本文件在**加载期**做一次**跨来源对拍**（`assertWriteTenantConstantsMatchPython()`），
 *    对不上就**响亮失败**（⛔ 不是注释提醒、也不是静默降级成"不比对"）。
 */
import { existsSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

/** 本文件在 `<仓库根>/src/frontend/e2e/config.mjs` ⇒ 上溯三层就是仓库根。 */
export const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../../..')
export const SRC_DIR = resolve(REPO_ROOT, 'src')
export const FRONTEND_DIR = resolve(SRC_DIR, 'frontend')

export const BACKEND_PORT = 8010
export const FRONTEND_PORT = 5273
/** ⚠️ 用 `localhost` 而不是 `127.0.0.1`：Vite 默认只监听 IPv6 的 `[::1]`（前端 README 已实测）。 */
export const FRONTEND_URL = `http://localhost:${FRONTEND_PORT}`
export const BACKEND_URL = `http://127.0.0.1:${BACKEND_PORT}`

/* ——— 只读租户（默认，`tt_e2e`）：本线**不动**它的任何行为 ——— */

export const TENANT_SCHEMA = 'e2e'
export const TENANT_DOMAIN = 'e2e.test.local'
export const TENANT_DB = 'tt_e2e'

export const E2E_TOOLS_DIR = resolve(REPO_ROOT, '.tools/e2e')
export const TENANT_ENV_FILE = resolve(E2E_TOOLS_DIR, 'tenant.env')
export const SHOTS_DIR = resolve(E2E_TOOLS_DIR, 'shots')
export const REPORT_JSON = resolve(E2E_TOOLS_DIR, 'report.json')
/** ⛔ 必须显式指到 `.tools/` 下：默认会落 `src/frontend/test-results/`，把 `git status` 弄脏（DoD 第 5 条）。 */
export const TEST_RESULTS_DIR = resolve(E2E_TOOLS_DIR, 'test-results')

/* ——— 一次性写租户（写模式专用） ———
 *
 * ⛔ 这四个值与 `src/apps/platform/management/commands/reset_e2e_write.py` 的 `WRITE_*` 常量
 *    **必须逐字相同**，判据见本文件末尾的 `assertWriteTenantConstantsMatchPython()`。
 */

export const WRITE_TENANT_SCHEMA = 'e2e_write'
export const WRITE_TENANT_DB = 'tt_e2e_write'
export const WRITE_TENANT_DOMAIN = 'e2e-write.test.local'
export const WRITE_TENANT_ENV_FILE = resolve(E2E_TOOLS_DIR, 'tenant-write.env')

/** 凭据落点的**仓库相对路径**：跨来源对拍时与 Python 侧那个字面量比（两侧的仓库根可能不同）。 */
export const WRITE_TENANT_ENV_REL = '.tools/e2e/tenant-write.env'

/** Python 侧常量的**唯一出处**（`reset_e2e_write.py`，T1 交付）。 */
export const WRITE_TENANT_COMMAND_FILE = resolve(
  SRC_DIR,
  'apps/platform/management/commands/reset_e2e_write.py',
)

/**
 * 写模式开关（**唯一出处**：环境变量 `TT_E2E_WRITE=1`）。
 * ⛔ **fail-closed**：缺省 / 拼错 / 任何别的值一律**读模式**（用只读租户、拦断写）。
 */
export const WRITE_MODE = process.env.TT_E2E_WRITE === '1'

/**
 * 这一轮实际要连的租户（读模式 = 只读租户；写模式 = 一次性写租户）。
 *
 * ⚠️ 消费点都从这里取（`playwright.config.ts` 的凭据 / 域名、`vite.e2e.config.ts` 的 `Host`）
 * ——⛔ 不要在消费点各写一次三目，那样"某一处漏改"只会表现为**连错库**而没有任何声音。
 */
export const ACTIVE_TENANT = WRITE_MODE
  ? {
      schema: WRITE_TENANT_SCHEMA,
      domain: WRITE_TENANT_DOMAIN,
      db: WRITE_TENANT_DB,
      envFile: WRITE_TENANT_ENV_FILE,
      label: '一次性写租户（写模式）',
    }
  : {
      schema: TENANT_SCHEMA,
      domain: TENANT_DOMAIN,
      db: TENANT_DB,
      envFile: TENANT_ENV_FILE,
      label: '只读 E2E 租户（默认）',
    }

/** `uv` 不在 PATH 也不在仓库里时，回落成裸命令名（换机器时用环境变量 `UV_BIN` 覆盖）。 */
export const UV_BIN =
  process.env.UV_BIN ??
  (existsSync(resolve(REPO_ROOT, '.tools/bin/uv')) ? resolve(REPO_ROOT, '.tools/bin/uv') : 'uv')

/** 沙箱下 `uv` 必须能写缓存目录，否则连命令都起不来（见 AGENTS.local.md）。 */
export const UV_CACHE_DIR = process.env.UV_CACHE_DIR ?? resolve(REPO_ROOT, '.tools/cache')

/** 浏览器二进制目录：默认仓库内，绝不写死本机绝对路径。 */
export const BROWSERS_PATH =
  process.env.PLAYWRIGHT_BROWSERS_PATH ?? resolve(REPO_ROOT, '.tools/ms-playwright')

/** Vite 的可执行入口；用 `node <入口>` 起，避免依赖 PATH 里有 pnpm。 */
export const VITE_BIN = resolve(FRONTEND_DIR, 'node_modules/vite/bin/vite.js')

/**
 * 读租户凭据（`provision_tenant_db --emit-env` 写出的 0600 片段）。
 * 文件不存在时返回空对象——调用方自己决定这是不是错（供给阶段先于它，所以那时是正常的）。
 *
 * @param {string} [file] 凭据文件路径；缺省 = 只读租户那份（`TENANT_ENV_FILE`，**行为与今天逐字相同**）。
 *   写模式的消费点显式传 `WRITE_TENANT_ENV_FILE` / `ACTIVE_TENANT.envFile`。
 * @returns {Record<string, string>} 键值都是字符串；缺文件时是 `{}`。
 * ⚠️ 这条 `@returns` 是**承重**的（T2 执行期实测）：`tsconfig.node.json` 开了 `allowJs` 之后，
 * `playwright.config.ts` 会按 JS 推断拿类型，而 `const out = {}` 被推断成 `{}` ⇒ 不写它，
 * `tenantEnv.POSTGRES_DB` 直接报 TS2339（`{}` 上叫不出任何键）。
 */
export function readTenantEnv(file = TENANT_ENV_FILE) {
  if (!existsSync(file)) return {}
  const out = {}
  for (const raw of readFileSync(file, 'utf8').split('\n')) {
    const line = raw.trim()
    if (!line || line.startsWith('#')) continue
    const i = line.indexOf('=')
    if (i === -1) continue
    out[line.slice(0, i)] = line.slice(i + 1)
  }
  return out
}

/* ——— 跨来源对拍：Python 与 JS 各一份写租户常量，不许漂移 ———
 *
 * 判据形态照 `e2e/fixtures/routes.ts` 从 `src/router/index.ts` 抽字面量那条：**从另一侧的文件里抽**
 * （这里用正则读 `reset_e2e_write.py`），再与本文件的常量比。⛔ 抽不到就**响亮失败**——
 * 静默"抽不到 ⇒ 跳过比对"等于把这条判据变成恒真。
 */

/**
 * 从 `reset_e2e_write.py` 里抽一个模块级常量的**唯一字面量**。
 *
 * ⛔ 形状判据（三条都要，任一条不满足就抛——都属"另一侧的文件形状变了"）：
 *  1. 必须有 `^NAME = …` 这一行（行首、可有空白）；
 *  2. 右边必须**恰好含一个双引号字面量**（本仓 Python 侧的常量都是这个形状）；
 *  3. 抽出来的值不能是空串。
 *
 * @param {string} src 文件原文
 * @param {string} name 常量名（如 `WRITE_DATABASE`）
 * @returns {string} 抽出的字面量
 */
function readPyWriteConstant(src, name) {
  const line = src.match(new RegExp(`^\\s*${name}\\s*=\\s*(.+)$`, 'm'))
  if (!line) {
    throw new Error(
      `跨来源对拍失败：${WRITE_TENANT_COMMAND_FILE} 里找不到 \`${name} = …\` 这一行。\n` +
        '这是**响亮失败**（不是"跳过比对"）：该文件的形状变了，写租户常量就没人守了。',
    )
  }
  const literals = [...line[1].matchAll(/"([^"]*)"/g)].map((m) => m[1])
  if (literals.length !== 1 || literals[0] === '') {
    throw new Error(
      `跨来源对拍失败：${name} 右边应当恰好含一个非空的双引号字面量，实际抽到 ` +
        `${JSON.stringify(literals)}（原文：${line[1].trim()}）。\n` +
        '这是**响亮失败**：形状变了就得先改本函数，⛔ 不许静默跳过。',
    )
  }
  return literals[0]
}

/**
 * 读 Python 侧（`reset_e2e_write.py`）的四个写租户常量。
 *
 * ⚠️ `WRITE_ENV_FILE` 在 Python 侧写作 `Path(settings.REPO_ROOT) / ".tools/e2e/tenant-write.env"`
 * ⇒ 抽出来的是**仓库相对路径**（两侧的仓库根可以不同：worktree / CI）。JS 侧比的是同一个相对路径。
 */
export function readPythonWriteTenantConstants() {
  const src = readFileSync(WRITE_TENANT_COMMAND_FILE, 'utf8')
  return {
    schema: readPyWriteConstant(src, 'WRITE_SCHEMA'),
    database: readPyWriteConstant(src, 'WRITE_DATABASE'),
    domain: readPyWriteConstant(src, 'WRITE_DOMAIN'),
    envFile: readPyWriteConstant(src, 'WRITE_ENV_FILE'),
  }
}

/**
 * **跨来源判据**（spec §2 的表 / T1 留给 T2 的风险）：JS 侧与 Python 侧的写租户常量必须逐字相同。
 *
 * ⛔ 两侧是**独立来源**（一份 TS/JS 常量、一份 Python 常量，谁都不是从另一侧派生的）
 * ⇒ 这不是自指断言（`E-0024`）：任一侧被单独改坏，本条当场抛。
 * ⛔ 只写注释说"两边要一致"是**不算**的（任务书明令）——必须有一条会失败的判据。
 *
 * @returns {void} 一致时什么都不做；不一致时抛错（带逐字段 diff）。
 */
export function assertWriteTenantConstantsMatchPython() {
  const py = readPythonWriteTenantConstants()
  const js = {
    schema: WRITE_TENANT_SCHEMA,
    database: WRITE_TENANT_DB,
    domain: WRITE_TENANT_DOMAIN,
    envFile: WRITE_TENANT_ENV_REL,
  }
  const drift = Object.keys(js).filter((key) => py[key] !== js[key])
  if (drift.length > 0) {
    throw new Error(
      '⛔ 写租户常量**跨来源漂移**（JS 侧 `e2e/config.mjs` vs Python 侧 ' +
        `\`${WRITE_TENANT_COMMAND_FILE}\`）：\n` +
        drift.map((key) => `  - ${key}：JS=${JSON.stringify(js[key])} ≠ Python=${JSON.stringify(py[key])}`).join('\n') +
        '\n两侧是独立来源、必须逐字相同（spec §2 的表）：先裁定哪一侧是对的，再同时改。' +
        '⛔ 不许只改一侧、也不许把这条判据放宽成警告。',
    )
  }
}

// 加载期就跑一次：任何 E2E 入口（`run.mjs` / `playwright.config.ts` / `vite.e2e.config.ts` /
// `lib/tenant.mjs`）在 import 本文件时都会过这道闸。
assertWriteTenantConstantsMatchPython()
