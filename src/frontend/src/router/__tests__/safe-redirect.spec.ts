import { describe, expect, it } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'
import { sanitizeRedirect } from '../safe-redirect'

const alwaysResolves = () => true

describe('sanitizeRedirect', () => {
  it.each([
    ['https://evil.example'],
    ['//evil.example'],
    ['/\\evil.example'],
    ['/%5cevil.example'],
    ['/%09/evil.example'],
    ['javascript:alert(1)'],
    ['/login'],
    ['/login?redirect=/x'],
    [''],
  ])('拒绝 %s', (raw) => {
    expect(sanitizeRedirect(raw, alwaysResolves)).toBeNull()
  })

  it.each([['/orders/new?game=3'], ['/'], ['/x']])('接受站内相对路径 %s', (raw) => {
    expect(sanitizeRedirect(raw, alwaysResolves)).toBe(raw)
  })

  it('非字符串（重复参数会解成数组）一律拒绝', () => {
    expect(sanitizeRedirect(['/', 'https://evil.example'], alwaysResolves)).toBeNull()
    expect(sanitizeRedirect(undefined, alwaysResolves)).toBeNull()
  })

  it('resolve 不命中真实路由时拒绝', () => {
    expect(sanitizeRedirect('/nope', () => false)).toBeNull()
  })

  /**
   * **双重编码**（`%252F…`）本条**不拒绝**——只解一次，解完还是 `%2F`，没有 `\` 与 C0 字符。与其只在注释里写"这没问题"，不如把**行为**与**落点**都钉住（本用例就是那句注释的证据）：落点必须仍是**站内绝对路径**（恰好一个前导 `/`）；一旦有人把 `resolve` 换成 `location.assign`、或把兜底路由改成跨源，这条会红。
   * ⚠️ spec §5.2 的 payload 表**没有**列这一形态（它列的是 `/%5c…`、`/%09…`）——这是"表外形态"的**行为固化**，不是"表内已验证"，两句话不能混。
   */
  it('双重编码放行时，落点仍钉在站内路径（不是协议相对 URL）', () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/:pathMatch(.*)*', name: 'not-found', component: { template: '<div />' } },
      ],
    })
    const raw = '/%252F%252Fevil.example'
    const out = sanitizeRedirect(raw, (p) => router.resolve(p).matched.length > 0)
    expect(out).toBe(raw)
    const resolved = router.resolve(out!)
    expect(resolved.href.startsWith('/')).toBe(true)
    expect(resolved.href.startsWith('//')).toBe(false)
  })
})
