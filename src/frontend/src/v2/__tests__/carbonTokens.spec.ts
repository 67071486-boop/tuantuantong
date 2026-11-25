// vitest 默认 node 环境：本文件只读文件文本，不需要 DOM。
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

/** token 作用域网（实施计划 §5.4）：`carbon.css` 里1. **全部**颜色字面量（hex / rgb[a](）只存在于 `.ttv2 { … }` 块内、且都长在 `--tt2-*`声明上——v2 组件类只许 `var(--tt2-…)`，⛔ 不写裸色值（DRAFT §1「代码只许引用变量」）；2. `.ttv2` 之外**没有**任何 `--tt2-*` 定义（token 泄漏到旧前端 = 本线的红线）。为什么钉文本而不钉渲染：作用域泄漏是「CSS 文本形状」性质——jsdom 不算 cascade，编译产物又晚于源码；对着源码钉形状是最早、最不容易被绕开的判据。断言可失败性（AGENTS §5-G）：每条都有阳性对照 / 反向断言，块提取坏了会当场红。
 * token 作用域网（实施计划 §5.4）：`carbon.css` 里1. **全部**颜色字面量（hex / rgb[a](）只存在于 `.ttv2 { … }` 块内、且都长在 `--tt2-*`声明上——v2 组件类只许 `var(--tt2-…)`，⛔ 不写裸色值（DRAFT §1「代码只许引用变量」）；2. `.ttv2` 之外**没有**任何 `--tt2-*` 定义（token 泄漏到旧前端 = 本线的红线）。为什么钉文本而不钉渲染：作用域泄漏是「CSS 文本形状」性质——jsdom 不算 cascade，编译产物又晚于源码；对着源码钉形状是最早、最不容易被绕开的判据。断言可失败性（AGENTS §5-G）：每条都有阳性对照 / 反向断言，块提取坏了会当场红。 */

const CSS_PATH = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../carbon.css')
const CSS = readFileSync(CSS_PATH, 'utf8')

/** 从 `.ttv2` 选择器起做括号配对，取出整个声明块；取不到 = 响亮失败（不是静默跳过）。 */
function extractTtv2Block(source: string): { block: string; start: number; end: number } {
  const marker = source.search(/\.ttv2\s*\{/)
  if (marker < 0) throw new Error('carbon.css 里找不到 .ttv2 选择器——作用域网失去检查对象，当场红')
  const open = source.indexOf('{', marker)
  let depth = 0
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === '{') depth += 1
    if (source[i] === '}') {
      depth -= 1
      if (depth === 0) return { block: source.slice(open + 1, i), start: open + 1, end: i }
    }
  }
  throw new Error('.ttv2 块的括号不配对——文件本身坏了')
}

/** 剥掉 `/* … *\/` 注释再分析：注释里的色值只是出处注记（如「压深为 #6D6A63」），不是样式值。 */
function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, '')
}

const CSS_NO_COMMENTS = stripComments(CSS)
const { block, start: blockStart, end: blockEnd } = extractTtv2Block(CSS_NO_COMMENTS)
const outside = CSS_NO_COMMENTS.slice(0, blockStart) + CSS_NO_COMMENTS.slice(blockEnd)

/** 颜色字面量：3–8 位 hex、rgb()/rgba() 函数（碳火 token 的两种形态）。 */
const COLOR_RE = /#[0-9a-fA-F]{3,8}\b|rgba?\(/g

/** `--tt2-*` 的**定义**（冒号右侧有值）；`var(--tt2-…)` 是使用、不算定义。 */
const TOKEN_DEF_RE = /--tt2-[a-z0-9-]+\s*:/g

describe('token 作用域网：carbon.css（.ttv2 作用域 + 无裸色值）', () => {
  it('全部颜色字面量都在 .ttv2 块内（块外一个都没有）', () => {
    const leaks = outside.match(COLOR_RE) ?? []
    expect(
      leaks,
      `carbon.css 在 .ttv2 块之外出现裸色值：${JSON.stringify(leaks)}——组件类只许 var(--tt2-…)`,
    ).toEqual([])
  })

  it('块内每一处颜色字面量都长在 --tt2-* 声明的值上（不允许出现在别的属性值里）', () => {
    // 逐条声明检查：有颜色字面量的声明，属性名必须是 --tt2- 开头。
    // ⚠️ 不用带 g 标志的 COLOR_RE 做 test()（lastIndex 跨调用残留会漏判）——用非全局副本。
    const colorProbe = new RegExp(COLOR_RE.source)
    const offenders: string[] = []
    for (const declaration of block.split(';')) {
      if (!colorProbe.test(declaration)) continue
      const name = declaration.split(':')[0]?.trim() ?? ''
      if (!name.startsWith('--tt2-')) offenders.push(declaration.trim())
    }
    expect(offenders, `非 token 属性上出现颜色字面量：${JSON.stringify(offenders)}`).toEqual([])
  })

  it('.ttv2 之外没有 --tt2-* 定义（防 token 泄漏进旧前端）', () => {
    const leaks = outside.match(TOKEN_DEF_RE) ?? []
    expect(leaks, `.ttv2 之外定义了 ${JSON.stringify(leaks)}——token 只许挂在 .ttv2 下`).toEqual([])
  })

  it('阳性对照：块提取是真的拿到了 token（空块会让上面三条恒真）', () => {
    expect(block).toContain('--tt2-side:')
    expect(block).toContain('--tt2-primary:')
    expect((block.match(TOKEN_DEF_RE) ?? []).length).toBeGreaterThan(20)
  })

  it('DRAFT §1 的基础 token 全部在块内定义（值逐字对表；⛔ 不许改值）', () => {
    // 值出处 = docs/FRONTEND-V2-DRAFT.md §1（唯一视觉出处）。
    // ⚠️ 有意偏离样稿的 --tt2-dim 用 #6D6A63（压深过 4.5:1，DRAFT §8），不按样稿 #77746E。
    // ⚠️ --tt2-st-pending 用 #5B35A8（待抢单=紫），不按样稿 #B4690E（有意偏离样稿）。
    // ⚠️ --tt2-page 定稿纯白 #FFFFFF（业务方「大面积那层还是灰」）；侧栏文字 #C6CAD0。
    // ⚠️ D-0371（2026-11-03 业务方四轮本地预览逐项拍板）起状态签 = 墨点 + 同族浅底填充三件套
    //     （每状态 dot/bg/fg；代练中=绿、已完成=蓝、待抢单=紫、待验收=橙红、已退单=浅红）。
    //     fg 对 bg 4.65–6.61、fg 对白 5.47–8.83（≥4.5 ⇒ 选中实底白字同值过线），探针实测。
    const REQUIRED: Record<string, string> = {
      '--tt2-side': '#23252a',
      '--tt2-side-text': '#c6cad0',
      '--tt2-side-active': '#f5f3ef',
      '--tt2-page': '#ffffff',
      '--tt2-panel': '#ffffff',
      '--tt2-wecom-wechat-label': '#15b628',
      '--tt2-wecom-company-label': '#ff8214',
      '--tt2-ink': '#2b2a28',
      '--tt2-dim': '#6d6a63',
      '--tt2-line': '#e7e3dc',
      '--tt2-primary': '#0b7a70',
      '--tt2-primary-deep': '#085a52',
      '--tt2-st-unassigned': '#57544c',
      '--tt2-st-unassigned-bg': '#efede7',
      '--tt2-st-pending': '#5b35a8',
      '--tt2-st-pending-bg': '#e9dff9',
      '--tt2-st-doing': '#1d6b40',
      '--tt2-st-doing-bg': '#def0e3',
      '--tt2-st-accept': '#a03312',
      '--tt2-st-accept-bg': '#fad9cb',
      '--tt2-st-done': '#0e4fa8',
      '--tt2-st-done-bg': '#d8e6fc',
      '--tt2-st-refund': '#a02434',
      '--tt2-st-refund-bg': '#fbedf1',
      '--tt2-st-refund-chip': '#efaaaa',
      '--tt2-st-paused': '#8f4304',
      '--tt2-st-paused-bg': '#fbdfc2',
      '--tt2-st-error': '#8a2620',
      '--tt2-st-error-bg': '#f5c3b7',
    }
    // ⚠️ 收尾锚用 `;` 不用 `\b`：var(...) 引用形态的值以 `)` 结尾（非词字符），`\b` 恒不成立。
    const missing = Object.entries(REQUIRED).filter(([name, value]) => {
      const re = new RegExp(`${name}\\s*:\\s*${value}\\s*;`)
      return !re.test(block)
    })
    expect(
      missing.map(([name]) => name),
      'DRAFT §1 的 token 缺失或值被改动（值只能来自 DRAFT，改值要先改 DRAFT）',
    ).toEqual([])
  })

  it('D-0351 内容区反馈档的 token 全部在块内定义（值逐字对表）', () => {
    // 出处 = docs/FRONTEND-V2-DRAFT.md §1（业务方 2026-10-31 看线上反馈建立、
    // D-0371 起 tool 档并入中性灰：底/线与 default 档同值）。
    const REQUIRED_D0351: Record<string, string> = {
      '--tt2-primary-deeper': '#06423c',
      '--tt2-hover-wash': 'rgba(11, 122, 112, 0.06)',
      '--tt2-row-checked': '#dff2f0',
      '--tt2-tool-bg': '#f7f6f2',
      '--tt2-tool-line': '#d6d1c6',
    }
    const missing = Object.entries(REQUIRED_D0351).filter(([name, value]) => {
      const re = new RegExp(`${name}\\s*:\\s*${value.replace(/[()]/g, '\\$&')}`)
      return !re.test(block)
    })
    expect(missing.map(([name]) => name), 'D-0351 新 token 缺失或值被改动').toEqual([])
  })

  it('⭐ D-0371 状态签三件套：九枚墨点 + 九条墨点 40% 描边 + 已结账实底签 + 白点全部在块内定义（值逐字对表）', () => {
    // 出处 = DRAFT §1（业务方四轮本地预览逐项拍板的「墨点 + 同族浅底填充」三件套）。
    // 读数（.tools/st-contrast.mjs）：dot 对 bg 3.10–5.62（图形件 ≥3）；已结账实底白字 5.84。
    const DOTS: Record<string, string> = {
      '--tt2-st-unassigned-dot': '#8a867c',
      '--tt2-st-pending-dot': '#7a45d0',
      '--tt2-st-doing-dot': '#2e8b57',
      '--tt2-st-accept-dot': '#d64a1e',
      '--tt2-st-done-dot': '#1159c1',
      '--tt2-st-error-dot': '#8a2620',
      '--tt2-st-refund-dot': '#c22b3a',
      '--tt2-st-paused-dot': '#b45d04',
      '--tt2-st-unsettled-dot': '#5c6b7a',
    }
    const LINES: Record<string, string> = {
      '--tt2-st-unassigned-line': 'rgba(138, 134, 124, 0.4)',
      '--tt2-st-pending-line': 'rgba(122, 69, 208, 0.4)',
      '--tt2-st-doing-line': 'rgba(46, 139, 87, 0.4)',
      '--tt2-st-accept-line': 'rgba(214, 74, 30, 0.4)',
      '--tt2-st-done-line': 'rgba(17, 89, 193, 0.4)',
      '--tt2-st-error-line': 'rgba(138, 38, 32, 0.4)',
      '--tt2-st-refund-line': 'rgba(194, 43, 58, 0.4)',
      '--tt2-st-paused-line': 'rgba(180, 93, 4, 0.4)',
      '--tt2-st-unsettled-line': 'rgba(92, 107, 122, 0.4)',
    }
    const REQUIRED_D0371: Record<string, string> = {
      ...DOTS,
      ...LINES,
      '--tt2-st-settled': '#1f7345',
      '--tt2-st-dot-on': 'rgba(255, 255, 255, 0.9)',
    }
    const missing = Object.entries(REQUIRED_D0371).filter(([name, value]) => {
      const re = new RegExp(`${name}\\s*:\\s*${value.replace(/[()]/g, '\\$&')}\\s*;`)
      return !re.test(block)
    })
    expect(missing.map(([name]) => name), 'D-0371 三件套 token 缺失或值被改动').toEqual([])
    // 「已结账」实底签类同次落位（底=框=settled、字=白；收入页结算列专用）。
    expect(CSS_NO_COMMENTS).toMatch(
      /\.cv2-st--settled\s*\{[^}]*color:\s*var\(--tt2-on-primary\)[^}]*background:\s*var\(--tt2-st-settled\)/,
    )
    // 2026-11-06 实底改版：状态签基类 = 底取各态 --st-dot 实色 + 白字（全站签一次统一）。
    expect(CSS_NO_COMMENTS).toMatch(
      /\.cv2-st\s*\{[^}]*background:\s*var\(--st-dot[^}]*color:\s*var\(--tt2-on-primary\)/,
    )
    // 实底上的墨点转白 90%（--tt2-st-dot-on）；筛选签/状态卡（浅底）的点仍取各态 --st-dot。
    expect(CSS_NO_COMMENTS).toMatch(
      /\.cv2-st \.cv2-dot\s*\{[^}]*background:\s*var\(--tt2-st-dot-on\)/,
    )
  })

  it('⭐ D-0371 按钮档：中性灰浅底（含 hover 档）+ 红橙提交钮 + 琥珀独立 tint 全部在块内定义（值逐字对表）', () => {
    // 出处 = DRAFT §1（业务方拍板：查询=青绿 primary、提交=红橙 submit 双主钮；
    // tool 并入中性灰；危险档 hover 深红；琥珀 tint 不再借状态签 token）。
    const REQUIRED_BTNS: Record<string, string> = {
      '--tt2-btn-soft-bg': '#f7f6f2',
      '--tt2-btn-soft-line': '#d6d1c6',
      '--tt2-btn-soft-hover-bg': '#f0eee8',
      '--tt2-btn-soft-hover-line': '#afa89a',
      '--tt2-btn-submit': '#c4431b',
      '--tt2-btn-submit-hover': '#a93a15',
      '--tt2-btn-submit-shadow': 'rgba(43, 42, 40, 0.14)',
      '--tt2-btn-danger': '#b03a32',
      '--tt2-danger-hover': '#8f2b24',
      '--tt2-tint-amber': '#6b5400',
      '--tt2-tint-amber-bg': '#f5eac5',
      '--tt2-tint-amber-line': 'rgba(154, 123, 0, 0.42)',
      '--tt2-filter-line': '#dde3e0',
      '--tt2-focus-ring': 'rgba(11, 122, 112, 0.12)',
      '--tt2-tab-close': 'rgba(255, 255, 255, 0.8)',
      '--tt2-tint-blue': '#16407e',
      '--tt2-tint-blue-bg': '#e3ebf8',
      '--tt2-tint-blue-line': 'rgba(30, 95, 196, 0.35)',
      // ⭐ 连体网格标签格底 = #FBFBFB（对标证据件字面色，业务方 2026-11-07 指定全站换档）。
      //     dim 字对新值 5.21:1（旧 #F5F6F7 上 4.99:1——变浅对比反而更高）。
      '--tt2-label-bg': '#fbfbfb',
    }
    const missing = Object.entries(REQUIRED_BTNS).filter(([name, value]) => {
      const re = new RegExp(`${name}\\s*:\\s*${value.replace(/[()]/g, '\\$&')}`)
      return !re.test(block)
    })
    expect(missing.map(([name]) => name), 'D-0371 按钮 token 缺失或值被改动').toEqual([])
    // tint-gray 三枚 token 已删（导出按钮的皮并入 default 档）——不许残留定义。
    expect(block).not.toMatch(/--tt2-tint-gray/)
  })

  it('筛选名字格暖米、筛选提示另用一档，旧的标签底和提示色不改', () => {
    expect(block).toMatch(/--tt2-btn-find-wash\s*:\s*#e5f3f1\s*;/)
    expect(block).toMatch(/--tt2-st-accept-hover\s*:\s*#80290e\s*;/)
    expect(block).toMatch(/--tt2-filter-label-bg\s*:\s*#f6f4ef\s*;/)
    expect(block).toMatch(/--tt2-filter-ph\s*:\s*#d0cbc3\s*;/)
    expect(block).toMatch(/--tt2-placeholder-dim\s*:\s*#b9c2c0\s*;/)
    expect(block).toMatch(/--tt2-label-bg\s*:\s*#fbfbfb\s*;/)
    expect(CSS_NO_COMMENTS).toMatch(
      /\.cv2-filter \.flt-group > \.flt-label\s*,[\s\S]*?\{[^}]*background:\s*var\(--tt2-filter-label-bg\)[^}]*border-right:\s*1px solid var\(--tt2-line\)/,
    )
    expect(CSS_NO_COMMENTS).toMatch(
      /\.cv2-filter \.uf-group \.uf-prefix \.el-select__wrapper\s*\{[^}]*background:\s*var\(--tt2-filter-label-bg\)/,
    )
    expect(CSS_NO_COMMENTS).toMatch(
      /\.cv2-filter \.el-input__inner::placeholder[\s\S]*?\{[^}]*color:\s*var\(--tt2-filter-ph\)/,
    )
    expect(CSS_NO_COMMENTS).toMatch(
      /\.cv2-filter \.flt-group \.el-input \.el-input__wrapper\s*,[\s\S]*?\{[^}]*box-sizing:\s*border-box/,
    )
  })

  it('行内订单状态另有两枚字色，不改状态卡和单号沿用的旧值', () => {
    expect(block).toMatch(/--tt2-st-accept-ink\s*:\s*#ff6a00\s*;/)
    expect(block).toMatch(/--tt2-st-paused-ink\s*:\s*#c4960a\s*;/)
    expect(block).toMatch(/--tt2-st-accept\s*:\s*#a03312\s*;/)
    expect(block).toMatch(/--tt2-st-paused\s*:\s*#8f4304\s*;/)
  })

  it('阳性对照：D-0351 的值匹配器真的能分辨对错值（括号转义写坏会当场红）', () => {
    // 可失败性：rgba 值要经括号转义进正则——转义写坏有两种死法：恒不匹配（上面那条
    // 全体报缺、当场红）或恒匹配（静默假绿）。这条钉住匹配器**能分辨**：真值命中、坏值不中。真值从 REQUIRED 取（换主题不再挂红——琥珀→青绿换色踩过一次）。
    const wash = 'rgba(11, 122, 112, 0.06)' // = REQUIRED['--tt2-hover-wash']（换主题时两处同次改）
    const matcher = (value: string): RegExp =>
      new RegExp(`--tt2-hover-wash\\s*:\\s*${value.replace(/[()]/g, '\\$&')}`)
    expect(matcher(wash).test(block)).toBe(true)
    expect(matcher(wash.replace('0.06', '0.99')).test(block)).toBe(false)
  })

  it('⭐ 结算状态「未结账」的独立灰蓝 token 对在块内定义（值逐字对表）', () => {
    // 出处 = DRAFT §1（业务方原话「未结账改成颜色改成灰色一点带其他颜色 就是浅色那种」）；
    // 实测 #5C6B7A 对 #EAEDF0 4.65:1、对白 5.47:1（≥4.5 硬线）；「已结账」走 cv2-st--settled 实底档。
    const REQUIRED_UNSETTLED: Record<string, string> = {
      '--tt2-st-unsettled': '#5c6b7a',
      '--tt2-st-unsettled-bg': '#eaedf0',
    }
    const missing = Object.entries(REQUIRED_UNSETTLED).filter(([name, value]) => {
      const re = new RegExp(`${name}\\s*:\\s*${value.replace(/[()]/g, '\\$&')}\\s*;`)
      return !re.test(block)
    })
    expect(missing.map(([name]) => name), '未结账灰蓝 token 缺失或值被改动').toEqual([])
    // 签类同次落位（结算列专用，不进状态卡片/筛选签）。2026-11-06 实底改版：
    // 未结账签 = 底取灰蓝墨点实色（--st-dot 驱动基类实底）+ 白字；浅色 token 仍留作他处取色。
    expect(CSS_NO_COMMENTS).toMatch(
      /\.cv2-st--unsettled\s*\{[^}]*--st-dot:\s*var\(--tt2-st-unsettled-dot\)/,
    )
    expect(CSS_NO_COMMENTS).not.toMatch(
      /\.cv2-st--unsettled\s*\{[^}]*background:\s*var\(--tt2-st-unsettled-bg\)/,
    )
  })
})
