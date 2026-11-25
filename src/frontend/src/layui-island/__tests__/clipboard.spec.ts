// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { copyTextToClipboard } from '../clipboard'

/**
 * ⭐ **B2**：剪贴板的**两条路**。
 *
 * 为什么值得单独测：业务方要求「右键复制单号」，而租户站点有 `http://` 的内网访问形态
 * （非安全上下文）—— 那里 `navigator.clipboard` **整个不存在**，只走它的话这个功能
 * 在那些环境里**永远失败**（这正是本仓早先的行为：不存在就只提示失败）。
 * 现在必须落到 `document.execCommand('copy')` 兜底上，并且**成没成**要如实返回。
 */
describe('copyTextToClipboard：clipboard API 优先 → execCommand 兜底', () => {
  afterEach(() => {
    Reflect.deleteProperty(navigator, 'clipboard')
    // `document.execCommand` 是 jsdom **没有**的 API ⇒ 每个用例自己装、自己拆。
    Reflect.deleteProperty(document, 'execCommand')
  })

  function stubClipboard(writeText: (text: string) => Promise<void>): ReturnType<typeof vi.fn> {
    const fn = vi.fn(writeText)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: fn } })
    return fn
  }

  /** 装一个假的 `document.execCommand`，记录它收到的命令。 */
  function stubExecCommand(result: boolean): ReturnType<typeof vi.fn> {
    const fn = vi.fn(() => result)
    Object.defineProperty(document, 'execCommand', { configurable: true, value: fn })
    return fn
  }

  it('① 有 clipboard API ⇒ 用它，**不**碰 execCommand', async () => {
    const writeText = stubClipboard(async () => {})
    const exec = stubExecCommand(true)
    await expect(copyTextToClipboard('TB-1')).resolves.toBe(true)
    expect(writeText).toHaveBeenCalledWith('TB-1')
    expect(exec).not.toHaveBeenCalled()
  })

  it('② clipboard API **不存在**（非安全上下文）⇒ 走 execCommand 兜底并成功', async () => {
    expect(navigator.clipboard).toBeUndefined()
    const exec = stubExecCommand(true)
    await expect(copyTextToClipboard('TB-2')).resolves.toBe(true)
    expect(exec).toHaveBeenCalledWith('copy')
    // 兜底时造过临时 textarea ⇒ 用完必须清掉（⛔ 在页面上留节点）
    expect(document.querySelectorAll('textarea')).toHaveLength(0)
  })

  it('③ clipboard API 被拒（NotAllowedError）⇒ 退到 execCommand 兜底', async () => {
    stubClipboard(async () => {
      throw new Error('NotAllowedError')
    })
    const exec = stubExecCommand(true)
    await expect(copyTextToClipboard('TB-3')).resolves.toBe(true)
    expect(exec).toHaveBeenCalledWith('copy')
  })

  it('④ 两条路都不成 ⇒ 返回 false（调用方据此说「复制失败了」，⛔ 不假装成功）', async () => {
    expect(navigator.clipboard).toBeUndefined()
    // jsdom 里连 `execCommand` 都没有 —— 这就是「非安全上下文 + 老浏览器」那一档。
    await expect(copyTextToClipboard('TB-4')).resolves.toBe(false)
  })

  it('⑤ execCommand 返回 false / 抛错 ⇒ 也算失败', async () => {
    stubExecCommand(false)
    await expect(copyTextToClipboard('TB-5')).resolves.toBe(false)

    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      value: () => {
        throw new Error('boom')
      },
    })
    await expect(copyTextToClipboard('TB-6')).resolves.toBe(false)
  })

  it('⑥ 空串不调任何 API（没东西可复制 ⇒ 直接 false）', async () => {
    const writeText = stubClipboard(async () => {})
    const exec = stubExecCommand(true)
    await expect(copyTextToClipboard('')).resolves.toBe(false)
    expect(writeText).not.toHaveBeenCalled()
    expect(exec).not.toHaveBeenCalled()
  })
})
