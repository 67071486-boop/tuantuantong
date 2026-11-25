// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'

import { copyToClipboard } from '../clipboard'

/**
 * 剪贴板降级的单测（大厅订单号点击复制）。判据打在**真实出口**上：clipboard API 被调、或 `document.execCommand('copy')` 被调；
 * 不赌 jsdom 的实现（jsdom 本来就没有这两个能力，替身逐用例显式装/拆）。
 */

const writeText = vi.fn<(text: string) => Promise<void>>()

function stubClipboard(): void {
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: { writeText },
  })
}

function removeClipboard(): void {
  Reflect.deleteProperty(navigator, 'clipboard')
}

/** jsdom 没有 `document.execCommand` ⇒ 显式定义 + 用完删掉（不污染同文件其它用例）。 */
function stubExecCommand(impl: () => boolean): void {
  Object.defineProperty(document, 'execCommand', {
    configurable: true,
    value: vi.fn(impl),
  })
}

function removeExecCommand(): void {
  Reflect.deleteProperty(document, 'execCommand')
}

afterEach(() => {
  vi.clearAllMocks()
  removeClipboard()
  removeExecCommand()
  // 兜底：老接口那条路会往 body 塞离屏 textarea，异常路径也不能留垃圾。
  document.querySelectorAll('textarea').forEach((el) => el.remove())
})

describe('copyToClipboard（clipboard API → execCommand 降级）', () => {
  it('安全上下文：走 navigator.clipboard.writeText，成功返回 true', async () => {
    writeText.mockResolvedValue(undefined)
    stubClipboard()
    await expect(copyToClipboard('10000000000000000001')).resolves.toBe(true)
    expect(writeText).toHaveBeenCalledTimes(1)
    expect(writeText).toHaveBeenCalledWith('10000000000000000001')
  })

  it('★ http 内网域名：没有 clipboard API ⇒ 落到 execCommand（⛔ 不是直接失败）', async () => {
    stubExecCommand(() => true)
    await expect(copyToClipboard('JP10020030040050060')).resolves.toBe(true)
    expect(document.execCommand).toHaveBeenCalledWith('copy')
    // 离屏 textarea 用完必须摘掉（否则每点一次就往 body 塞一个）。
    expect(document.querySelectorAll('textarea')).toHaveLength(0)
  })

  it('clipboard 存在但被拒（无权限 / 非安全上下文）⇒ 仍能降级成功', async () => {
    writeText.mockRejectedValue(new Error('NotAllowedError'))
    stubClipboard()
    stubExecCommand(() => true)
    await expect(copyToClipboard('JP10020030040050060')).resolves.toBe(true)
    expect(writeText).toHaveBeenCalledTimes(1)
    expect(document.execCommand).toHaveBeenCalledWith('copy')
  })

  it('两路都失败 ⇒ 返回 false（调用方据此提示，⛔ 不假装成功）', async () => {
    stubExecCommand(() => false)
    await expect(copyToClipboard('JP10020030040050060')).resolves.toBe(false)
  })

  it('execCommand 直接抛异常 ⇒ 收敛成 false，不把异常抛给调用方', async () => {
    stubExecCommand(() => {
      throw new Error('execCommand boom')
    })
    await expect(copyToClipboard('JP10020030040050060')).resolves.toBe(false)
  })
})
