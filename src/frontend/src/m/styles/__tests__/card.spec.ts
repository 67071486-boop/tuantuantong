// @vitest-environment node
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'url'
import { describe, expect, it } from 'vitest'

const here = path.dirname(fileURLToPath(import.meta.url))
const css = readFileSync(path.resolve(here, '../card.css'), 'utf8')

describe('打手端共享卡片轮廓', () => {
  it('使用白底、细边框和 Framework7 卡片圆角，沿用卡片间距', () => {
    expect(css).toMatch(/\.m-card\.card\s*\{/)
    expect(css).toContain('background: #fff;')
    expect(css).toContain('border: 1px solid var(--m-border);')
    expect(css).toContain('border-radius: var(--f7-card-border-radius);')
    expect(css).toContain('margin: 0 0 10px;')
  })
})
