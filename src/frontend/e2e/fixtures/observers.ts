import { expect, type Page } from '@playwright/test'

/**
 * 采集页面上的信号。**判据口径照 spec §7，不许放宽**：
 * - **红**：`pageerror`（未捕获 JS 异常）、HTTP ≥ 500（`serverErrors`）；
 * - **只记录不红**：HTTP 4xx（`clientErrors`）、控制台 `error` 日志（`consoleErrors`）。
 *
 * 为什么 4xx 不判红（实测依据）：未登录时 island 页**照常渲染**，但会打出 6 个 401 控制台错误
 * （spec §1.1-3）——判红的话冒烟网从第一天起就是红的，然后就没人看了。
 *
 * ⚠️ `clientErrors` 是**给下游读的**（T5 / T7 的体检表要看「某页在打自己没权限的接口」）：
 * 声明了却不采集，下游读到的是 `undefined`（T4 修复轮第 1 条）。
 */
export type Observations = {
  pageErrors: string[]
  consoleErrors: string[]
  /** HTTP 4xx（`400 <= status < 500`）：**只记录，不判红**。 */
  clientErrors: string[]
  /** HTTP ≥ 500：判红（见 `expectNoPageErrors`）。 */
  serverErrors: string[]
}

export function observe(page: Page): Observations {
  const obs: Observations = {
    pageErrors: [],
    consoleErrors: [],
    clientErrors: [],
    serverErrors: [],
  }
  page.on('pageerror', (err) => obs.pageErrors.push(`${err.name}: ${err.message}`))
  page.on('console', (msg) => {
    if (msg.type() === 'error') obs.consoleErrors.push(msg.text().slice(0, 300))
  })
  page.on('response', (res) => {
    const status = res.status()
    if (status >= 500) {
      obs.serverErrors.push(`${status} ${res.request().method()} ${res.url()}`)
    } else if (status >= 400 && status < 500) {
      obs.clientErrors.push(`${status} ${res.request().method()} ${res.url()}`)
    }
  })
  return obs
}

/**
 * 「红」的两类断言收在一处——每个 spec 各写一遍必然漂移。
 * ⚠️ 只断 `pageErrors` 与 `serverErrors`：`clientErrors` / `consoleErrors` **有意不判红**。
 */
export function expectNoPageErrors(obs: Observations, where: string): void {
  expect(obs.pageErrors, `${where}：出现了未捕获 JS 异常`).toEqual([])
  expect(obs.serverErrors, `${where}：出现了 HTTP ≥ 500`).toEqual([])
}
