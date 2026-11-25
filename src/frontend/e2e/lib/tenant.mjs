/**
 * 确保 E2E 租户「存在且能连上」。
 *
 * 判据刻意是「**能连上**」而不是「库存在」：`provision_tenant_db` 对已存在的角色会拒绝重跑
 * （见其模块 docstring 的 Ruling 2/R5——它生成的密码没写进库，写出去就是一份库里不认的凭据），
 * 所以**能连上就必须跳过**，不能拿「库存在」当判据去重跑。
 */
import { spawnSync } from 'node:child_process'
import { mkdirSync } from 'node:fs'
import {
  E2E_TOOLS_DIR, SRC_DIR, TENANT_DB, TENANT_DOMAIN, TENANT_ENV_FILE,
  TENANT_SCHEMA, UV_BIN, UV_CACHE_DIR, WRITE_MODE, WRITE_TENANT_DB,
  WRITE_TENANT_ENV_FILE, readTenantEnv,
} from '../config.mjs'

/** 用「租户自己的身份连自己那个库」跑一句最轻的读，成功即视为就绪。 */
function readiness() {
  const env = readTenantEnv()
  if (!env.POSTGRES_DB || !env.POSTGRES_USER) return { ready: false, why: '凭据文件不存在或缺少 DB/USER' }
  // ⛔ **库身份检查必须先于任何一次连库**（终审加固）：`run.mjs` 有两步会碰数据库
  //    （本函数的探针、`seed_e2e`），而 Playwright 配置里那道「库名必须是 tt_e2e」的闸
  //    **更晚**才加载 ⇒ 凭据文件被改错时，最早的两次连库已经打到别的库上了。
  //    这里把判据提到**最早那一处**：库名不是 E2E 专用库就**不连**（`fatal` 由调用方直接终止，
  //    ⛔ 不当作「未就绪 ⇒ 去供给」，那会在错的前提下继续往下走）。
  if (env.POSTGRES_DB !== TENANT_DB) {
    return {
      ready: false,
      fatal: true,
      why: `凭据指向的库是 ${env.POSTGRES_DB}，不是 E2E 专用库 ${TENANT_DB}`,
    }
  }
  const probe = [
    'from apps.platform.models import Tenant, Domain',
    `t = Tenant.objects.filter(schema_name=${JSON.stringify(TENANT_SCHEMA)}).first()`,
    `d = Domain.objects.filter(domain=${JSON.stringify(TENANT_DOMAIN)}).first()`,
    "print('READY' if (t and d) else 'NOT_READY')",
  ].join('\n')
  const r = spawnSync(UV_BIN, ['run', 'python', 'manage.py', 'shell', '-c', probe], {
    cwd: SRC_DIR,
    env: { ...process.env, ...env, UV_CACHE_DIR },
    encoding: 'utf8',
  })
  if (r.status !== 0) {
    return { ready: false, why: (r.stderr || r.stdout || '').trim().split('\n').slice(-3).join(' ') }
  }
  // ⚠️ 不能直接写 `r.stdout.includes('READY')`：探针的失败分支打印的是 `NOT_READY`，
  //    而 `'NOT_READY'.includes('READY') === true` ⇒ 那种写法会把「目录行缺失」误判成已就绪。
  //    实测（Task 1 执行期）：让一个「打印 NOT_READY 且退出 0」的假 uv 冒充 UV_BIN 时，旧写法
  //    打出「✅ E2E 租户已就绪」并退出 0 —— 正是本任务要防的「静默说已就绪」。
  if (r.stdout.includes('NOT_READY')) {
    return { ready: false, why: '库连上了，但目录行（Tenant/Domain）缺失' }
  }
  return r.stdout.includes('READY')
    ? { ready: true }
    : { ready: false, why: '就绪探针没有打印 READY' }
}

export function ensureTenant() {
  mkdirSync(E2E_TOOLS_DIR, { recursive: true })
  const before = readiness()
  if (before.fatal) {
    console.error(`⛔ ${before.why}。`)
    console.error('   本线只连 E2E 专用库 ⇒ 到这里就停：不读任何行、也不供给 / 不灌数据。')
    console.error(`   请检查 ${TENANT_ENV_FILE}（或重跑 \`node e2e/lib/tenant.mjs\` 重新供给）。`)
    process.exit(1)
  }
  if (before.ready) {
    console.log(`✅ E2E 租户已就绪（schema=${TENANT_SCHEMA}，库=${readTenantEnv().POSTGRES_DB}）`)
    return { ok: true, created: false }
  }
  console.log(`ℹ️  E2E 租户未就绪（${before.why}）⇒ 开始供给`)
  const r = spawnSync(
    UV_BIN,
    ['run', 'python', 'manage.py', 'provision_tenant_db',
      '--schema', TENANT_SCHEMA,
      '--name', 'E2E 测试租户',
      '--domain', TENANT_DOMAIN,
      '--emit-env', TENANT_ENV_FILE],
    { cwd: SRC_DIR, env: { ...process.env, UV_CACHE_DIR }, encoding: 'utf8', stdio: 'inherit' },
  )
  if (r.status !== 0) {
    console.error('⛔ 供给失败。若提示角色已存在，说明库里已有一份凭据而我们不知道它的密码：')
    console.error('   请显式重跑并接受旧凭据作废：')
    console.error(`   cd ${SRC_DIR} && ${UV_BIN} run python manage.py provision_tenant_db \\`)
    console.error(`     --schema ${TENANT_SCHEMA} --name 'E2E 测试租户' --domain ${TENANT_DOMAIN} \\`)
    console.error(`     --emit-env ${TENANT_ENV_FILE} --rotate-password`)
    process.exit(1)
  }
  const after = readiness()
  if (!after.ready) {
    console.error(`⛔ 供给命令退出码是 0，但就绪自检仍失败：${after.why}`)
    process.exit(1)
  }
  console.log(`✅ E2E 租户已创建（schema=${TENANT_SCHEMA}，域名=${TENANT_DOMAIN}，库=${readTenantEnv().POSTGRES_DB}）`)
  return { ok: true, created: true }
}

/**
 * 把**一次性写租户**推倒重建 + 灌夹具（写模式专用；spec §2/§3/§4）。
 *
 * 唯一的实现是 Python 侧的 `manage.py reset_e2e_write`（T1 交付）——本函数只负责
 * 「**闸门 + 起子进程 + 复核**」三件事，⛔ 不自己写 DROP / 建库 / 灌数据的 SQL
 * （那是第二份实现，必与 T1 漂移）。
 *
 * ## 为什么要起子进程、而不是 `call_command`
 * T1 执行期实测：租户目录（`Tenant` / `Domain`）住在**租户自己那个库**的 `public` 里，
 * 而进程内的 `settings.DATABASES["default"]` 指着**应用库**（本机 `tt_dev`）⇒ 进程内跑会报
 * `schema 'e2e_write' 不存在`。⛔ 不要"顺手优化"成进程内调用。
 *
 * ## 三道 fail-closed 闸门（都在**任何破坏性动作之前**）
 *  1. `WRITE_MODE` 为假 ⇒ 直接抛（本函数**只属于写模式**，读模式永远不该走到这里）；
 *  2. 凭据文件**已存在**时，里面的 `POSTGRES_DB` 必须**逐字**是 `tt_e2e_write`，不等 ⇒ 抛
 *     （手滑把 `tenant-write.env` 换成只读租户 / 开发库那份时，这里拒绝启动）；
 *  3. 子进程退出后**复核**凭据文件：必须存在、且 `POSTGRES_DB` 仍是 `tt_e2e_write`
 *     ⇒ 闸门打在**消费点**上（`playwright.config.ts` 还会再判一次库名）。
 *
 * ⚠️ **子进程带着那份 0600 凭据跑**（与 `run.mjs` 灌只读租户同一姿势）：凭据里的
 * `POSTGRES_DB/USER/PASSWORD` 覆盖进程内的应用库配置。T1 的实测结论是**跑得通**
 * （`reset_e2e_write` 的 in-process 部分只用运维身份，租户库那一段由它自己的子进程负责）。
 * 文件还不存在时（首次跑）不带凭据跑——那正是它会生成凭据的那一次。
 *
 * 🔎 **已知边界（评审修复波 E，2026-09-25）：上面第 2 道闸门只判 `POSTGRES_DB`，⛔ 不判
 * `POSTGRES_HOST` / `POSTGRES_PORT`**。若有人**手工**把凭据改成「**另一台服务器上恰好同名的库**」，
 * 这条闸门不会察觉 ⇒ `reset_e2e_write` 的 DROP 会打到那台。现实风险低（凭据由
 * `provision_tenant_db` 本机写出、文件 0600、从不手工编辑），故**刻意不为此改造闸门架构**
 * （会把"单一出处"拆成两处、引入新的漂移面）——登记为已知边界，见
 * `docs/specs/2026-09-25-write-mode-audit-design.md` §7。
 *
 * @param {{ label?: string }} [opts] `label` 只用于日志（谁触发的这次重置）。
 * @returns {{ seconds: number }} 成功的重置耗时（秒）。
 * @throws {Error} 任一闸门不满足 / 子进程非 0 退出 ⇒ **抛**（调用方**不许**静默继续）。
 */
export function resetWriteTenant({ label = 'run' } = {}) {
  if (!WRITE_MODE) {
    throw new Error(
      'resetWriteTenant 只属于写模式（`TT_E2E_WRITE=1`），当前不是写模式 ⇒ 拒绝执行。' +
        '⛔ 这条闸门防的是"在读模式里手滑重置了一次性写租户"。',
    )
  }
  const before = readTenantEnv(WRITE_TENANT_ENV_FILE)
  if (before.POSTGRES_DB && before.POSTGRES_DB !== WRITE_TENANT_DB) {
    throw new Error(
      `闸门拒绝：${WRITE_TENANT_ENV_FILE} 指向的库是 ${before.POSTGRES_DB}，` +
        `不是一次性写租户库 ${WRITE_TENANT_DB} ⇒ 拒绝重置（更拒绝 DROP），不启动子进程。`,
    )
  }
  console.log(`♻️  [${label}] 重置一次性写租户（库=${WRITE_TENANT_DB}，子进程 reset_e2e_write）…`)
  const started = Date.now()
  const child = spawnSync(UV_BIN, ['run', 'python', 'manage.py', 'reset_e2e_write'], {
    cwd: SRC_DIR,
    // 带着那份 0600 凭据（首次跑时文件还不存在，`before` 是 `{}` ⇒ 什么都不覆盖）。
    env: { ...process.env, ...before, UV_CACHE_DIR },
    stdio: 'inherit',
  })
  if (child.status !== 0) {
    throw new Error(
      `reset_e2e_write 失败（退出码 ${child.status ?? 'null'}）⇒ 写模式**必须**停在这里：` +
        '不带着一个"没重置干净"的租户往下跑（spec §4：写模式会真的删数据，结果不可复现）。',
    )
  }
  const seconds = (Date.now() - started) / 1000
  const after = readTenantEnv(WRITE_TENANT_ENV_FILE)
  if (!after.POSTGRES_DB) {
    throw new Error(`重置后仍读不到凭据文件 ${WRITE_TENANT_ENV_FILE}（供给本该写出它）。`)
  }
  if (after.POSTGRES_DB !== WRITE_TENANT_DB) {
    throw new Error(
      `重置后凭据指向的库是 ${after.POSTGRES_DB}，不是 ${WRITE_TENANT_DB} ⇒ 拒绝继续。`,
    )
  }
  console.log(`✅ [${label}] 写租户已重建并灌好夹具（${seconds.toFixed(1)}s）`)
  return { seconds }
}

// 允许单独跑：`node e2e/lib/tenant.mjs`
if (process.argv[1] && process.argv[1].endsWith('tenant.mjs')) {
  ensureTenant()
}
