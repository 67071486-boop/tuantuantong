import { describe, expect, it } from 'vitest'
import { AVATAR_PALETTE, avatarColor, avatarDataUri, avatarInitial } from '../avatar'

describe('avatarInitial', () => {
  it('中文取首个字符', () => {
    expect(avatarInitial('小客服')).toBe('小')
  })
  it('拉丁转大写', () => {
    expect(avatarInitial('kefu')).toBe('K')
  })
  it('空 / 全空白回落 ?', () => {
    expect(avatarInitial('')).toBe('?')
    expect(avatarInitial('   ')).toBe('?')
  })
  it('按码点取，不把代理对切一半', () => {
    // '𠮷' 是单个码点、两个 UTF-16 码元；用 name[0] 会得到半个字符。
    expect(avatarInitial('𠮷野家')).toBe('𠮷')
  })
})

describe('avatarColor', () => {
  it('同名稳定（同一账号每次刷新颜色一样）', () => {
    expect(avatarColor('小客服')).toBe(avatarColor('小客服'))
  })
  it('色板里的每个名字都能取到，且至少有两个不同的名字给出不同颜色', () => {
    // ⚠️ 只断言「返回值 ∈ 色板」是**恒真判据**（函数按定义只会返回色板里的值）——
    // 它证明不了任何东西。所以判据是「不同名字能散开」。
    const colors = new Set(['甲', '乙', '丙', '丁', '戊', '己', '庚', '辛'].map(avatarColor))
    expect(colors.size).toBeGreaterThan(1)
    for (const c of colors) expect(AVATAR_PALETTE).toContain(c)
  })
})

describe('avatarDataUri', () => {
  it('是内联 SVG，并带上了尺寸与首字', () => {
    const uri = avatarDataUri('小客服', 72)
    expect(uri.startsWith('data:image/svg+xml')).toBe(true)
    const svg = decodeURIComponent(uri.split(',')[1]!)
    expect(svg).toContain('width="72"')
    expect(svg).toContain('>小<')
    expect(svg).toContain(avatarColor('小客服'))
  })
  it('对 XML 特殊字符转义：整串输出逐字相等（& / > / " 三条替换各删一条都会红）', () => {
    // ⚠️ 被转义的是名字的**首个码点**（`escapeXml(avatarInitial(name))`）⇒ 夹具必须让**首字**
    // 就是那个字符。原先只 `expect(svg).toContain('&amp;')` 且夹具首字是 `&` ⇒ 删掉 `>` / `"`
    // 那两条 `.replace` 仍然全绿（E-0024 族：子串断言钉不住未被触发的分支）。
    // 判据改成**整串等号**：期望串是 SVG 模板的**字面量副本**（不从 `avatar.ts` 派生颜色/首字，
    // 只把算好的色值写死）——这样模板或转义任何一处变了都会红。
    const expected = (letter: string, color: string): string =>
      'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(
        '<svg xmlns="http://www.w3.org/2000/svg" width="72" height="72" viewBox="0 0 72 72">'
        + `<rect width="72" height="72" fill="${color}"/>`
        + '<text x="50%" y="50%" text-anchor="middle" dominant-baseline="middle" '
        + 'font-size="32" fill="#ffffff" '
        + 'font-family="Helvetica Neue,Helvetica,PingFang SC,Tahoma,Arial,sans-serif">'
        + `${letter}</text></svg>`,
      )
    // 三个夹具的首字分别是 `&` / `>` / `"`，色值都是 `#f76b6b`（djb2 取模实测，写死不派生）。
    expect(avatarDataUri('&<>', 72)).toBe(expected('&amp;', '#f76b6b'))
    expect(avatarDataUri('>甲', 72)).toBe(expected('&gt;', '#f76b6b'))
    expect(avatarDataUri('"甲', 72)).toBe(expected('&quot;', '#f76b6b'))
  })
})
