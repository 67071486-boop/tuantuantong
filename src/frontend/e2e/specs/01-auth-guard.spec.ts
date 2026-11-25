import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { expect, test } from '@playwright/test'

import { FRONTEND_DIR } from '../config.mjs'
import { ACCOUNTS, switchAccount } from '../fixtures/auth'
import { expectNoPageErrors, observe } from '../fixtures/observers'

test.describe('登录态与鉴权守卫', () => {
  test('未登录访问业务页必须被挡到登录页', async ({ page }) => {
    const obs = observe(page)
    await page.goto('/orders')
    await expect(page).toHaveURL(/\/login(\?|$)/)
    expectNoPageErrors(obs, '未登录访问 /orders')
  })

  test('未登录直接打业务接口必须 401（不是 200，也不是 500）', async ({ request }) => {
    const res = await request.get('/api/business/orders/?page=1&page_size=1')
    expect(res.status()).toBe(401)
    const body = await res.json()
    // 判据是**业务码**：api_response() 永远返回信封 dict，看 data 是否为 null 是恒真的（E-0063）。
    expect(body.code).toBe(401)
  })

  test('八个账号都能登录（顺带证明 seed_e2e 的口令与本夹具一致）', async ({ page }) => {
    // 观测器**提到循环外**（T4 修复轮第 4 条）：同一个 `page` 上每调一次 `observe()` 就多挂一组
    // 监听器、且没有摘除手段 ⇒ 循环里调 6 次会让第 N 轮的事件同时写进前 N-1 个**再没人读**的数组。
    // 一个 spec 一个观测器，八个角色的整段登录过程都落在同一份 `obs` 上，不会漏。
    const obs = observe(page)
    // ⛔ **换账号一律走共享的 `switchAccount()`**（终审 I4 第 3 条）：本用例原先自己写
    // `login()` + `clearCookies()`，与 `02` / `03` 用的是**两份实现** ⇒ 两边会各自漂移
    // （T5 的 `switchAccount` 就带着一个错误的根因注释活了很久）。它是 `clearCookies()` →
    // 整文档导航 → `login()`，与原先那两行等价、但只有一份。
    for (const role of Object.keys(ACCOUNTS) as (keyof typeof ACCOUNTS)[]) {
      await switchAccount(page, role)
    }
    expectNoPageErrors(obs, '八个账号依次登录')
  })

  test('口令对拍：本夹具与 seed_e2e.py 的 PASSWORDS 逐字一致（防两处静默分叉）', async () => {
    // 这两份文件之间有一处硬耦合（Task 3 的实现者也点名了这个风险）。
    // 「注释里写一句必须一致」不构成判据——注释不会红；这里读 Python 那份当权威，逐条比。
    // ⚠️ 覆盖面**两个方向都判**（T1 修复轮补的反向）：既查「TS 有的 PY 要有」，
    //    也查「PY 有的 TS 要跟上」——只查前者时，`seed_e2e.py` 新增一个账号而 `auth.ts`
    //    没跟上会让本用例继续绿，而那个账号在审计里根本不会被登录到（静默丢覆盖）。
    //
    // ⚠️ T4 执行期修正（brief 原写 `../../../apps/...`）：`FRONTEND_DIR` = `<仓库根>/src/frontend`，
    //    三级 `..` 会落到**仓库根的父目录**（实测解析成
    //    `<主检出>/.worktrees/apps/platform/...`，文件不存在）⇒ 本用例 ENOENT 响亮失败。
    //    回到 `<仓库根>` 只要两级，故用 `../../src/apps/...`。
    const pyPath = resolve(
      FRONTEND_DIR,
      '../../src/apps/platform/management/commands/seed_e2e.py',
    )
    const src = readFileSync(pyPath, 'utf8')
    const block = src.match(/PASSWORDS\s*=\s*\{([\s\S]*?)\}/)
    expect(block, '没能从 seed_e2e.py 解析出 PASSWORDS —— 该文件形状变了，请同步本用例').not.toBeNull()
    const pairs = [...block![1].matchAll(/"([a-z0-9_]+)"\s*:\s*"([^"]+)"/g)].map(
      (m) => [m[1], m[2]] as const,
    )
    const pyPasswords = Object.fromEntries(pairs)

    /** 角色 → Python 那边的用户名（本映射也是判据的一部分：改名要在两边同时改）。 */
    const PY_USERNAME: Record<keyof typeof ACCOUNTS, string> = {
      admin: 'admin',
      cs: 'cs001',
      supervisor: 'supervisor001',
      taker: 'taker001',
      dev: 'dev001',
      minrole: 'minrole001',
      gamescope: 'gamescope001',
      nobody: 'nobody001',
    }
    for (const role of Object.keys(ACCOUNTS) as (keyof typeof ACCOUNTS)[]) {
      const pyName = PY_USERNAME[role]
      expect(ACCOUNTS[role].username, `角色 ${role} 的用户名与 Python 那份不一致`).toBe(pyName)
      expect(pyPasswords[pyName], `seed_e2e.py 里没有 ${pyName} 的口令`).toBeTruthy()
      expect(ACCOUNTS[role].password, `角色 ${role}（${pyName}）的口令与 Python 那份不一致`).toBe(
        pyPasswords[pyName],
      )
    }

    // ---- 反向：Python 多出来的用户名也必须被抓到（上面那条循环只管方向一）----
    // 判据是**双向集合相等**（两侧互含），而不是「TS 的都在 PY 里」这种单向包含。
    const claimedPyNames = Object.values(PY_USERNAME)
    expect(new Set(claimedPyNames).size, '两个角色映射到了同一个 Python 用户名').toBe(
      claimedPyNames.length,
    )
    expect(
      Object.keys(pyPasswords).sort(),
      'seed_e2e.py 的 PASSWORDS 里有本夹具没跟上的用户名（反向覆盖少了人）',
    ).toEqual([...claimedPyNames].sort())

    // ⚠️ 标题数字也要有判据：`01-auth-guard.spec.ts` 那条标题写「八个账号都能登录」，
    // 而它的循环走 `Object.keys(ACCOUNTS)` ⇒ 两侧**一起**增删账号时标题会静默变假话。
    // 集合相等管不了这一条（它只保证两侧一致），所以把标题里的数字单独钉成字面量。
    expect(Object.keys(ACCOUNTS), '账号个数变了：请同步上面那条标题里的数字').toHaveLength(8)
  })
})
