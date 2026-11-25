import { existsSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

/**
 * 这一组绑的是图标的**引入链**，不是「我们写了一条规则」。
 *
 * 已有的 `config/__tests__/menuIcons.spec.ts` 绑的是「映射名 ↔ `layui-icons.css` 里真有字形规则」，
 * 但**没有任何测试**保证这份 CSS **被程序引入**、`@font-face` 还在、字体文件还在磁盘上。
 * 实测（本文件加上之前）：删掉 `src/main.ts` 里那一行 import、或删掉 `@font-face` 块、
 * 或把 `url(...)` 指向不存在的文件，**44 条测试全绿**，而产物里一个字形规则、一个字体都没有
 * —— 断言打在「我们写了规则」这个中间层，而真实出口是「这条规则与这个字体能到浏览器」。
 *
 * 复审又实测出两个盲区，本文件一并堵上（都是「改一行、字形全没、测试不响」的形状）：
 *   1. **注释掉的 import 仍会被朴素正则匹配**——把 `main.ts` 那行改成 `// import …`，
 *      老断言照样通过；所以先剥注释再匹配（`stripComments`）。
 *   2. **只改 `@font-face` 的 `font-family` 名字**（不动 `.layui-icon{font-family:…}`）会让
 *      3 条测试里的前两条全绿，而字体不再被任何类引用、字形全部失效；所以断言两边**同名**。
 *
 * 同类教训见 `docs/ERRORS.md` E-0009（断言要打在真实出口/落地机制上）。
 *
 * 为什么读**源文件**而不是读构建入口：这条绑定的对象就是「源里存在这条引入」这一事实，
 * 读 `main.ts` 不必跑一次构建、也不依赖产物；产物级（`dist` 里真有 `@font-face` 与 hashed 字体）
 * 由 Task 4 的人工证据步负责，不在本文件的范围内。
 */
const here = dirname(fileURLToPath(import.meta.url)) // src/styles/__tests__/
const MAIN_TS = resolve(here, '../../main.ts') // src/main.ts
const ICONS_CSS = resolve(here, '../layui-icons.css') // src/styles/layui-icons.css

/** 剥掉 CSS 块注释与 `//` 行注释：不剥的话，**注释掉的那行 import 仍能被正则匹配到**。 */
function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1')
}

/** 取第一条 `@font-face` 的规则体；没有则 `null`。 */
function fontFaceBody(css: string): string | null {
  return /@font-face\s*\{([^}]*)\}/.exec(css)?.[1] ?? null
}

/** 取一条规则体里 `font-family` 的字体名（去引号、去空白、小写）。 */
function fontFamilyOf(ruleBody: string): string | null {
  return /font-family:\s*(['"]?)([^;'"]+)\1/.exec(ruleBody)?.[2]?.trim().toLowerCase() ?? null
}

describe('layui 图标的引入链（源级）', () => {
  it('main.ts 真的引入了 styles/layui-icons.css（注释掉的那行不算引入）', () => {
    const src = stripComments(readFileSync(MAIN_TS, 'utf8'))
    expect(src, `${MAIN_TS} 里没有 import './styles/layui-icons.css'（已排除注释）`).toMatch(
      /import\s+['"]\.\/styles\/layui-icons\.css['"]/,
    )
  })

  it('layui-icons.css 里有 @font-face，且 src 是一个 .woff2，字体文件真的在磁盘上', () => {
    const css = readFileSync(ICONS_CSS, 'utf8')

    const body = fontFaceBody(css)
    expect(body, 'layui-icons.css 里没有 @font-face 块').not.toBeNull()

    const url = /src:\s*url\((['"]?)([^'")]+)\1\)\s*format\(['"]woff2['"]\)/.exec(body ?? '')
    const fontUrl = url?.[2]
    expect(fontUrl, '@font-face 的 src 不是一个 url(...) format(\'woff2\')').toBeTruthy()
    expect(fontUrl ?? '', '字体 URL 必须以 .woff2 结尾（单文件、woff2-only）').toMatch(/\.woff2$/)

    // 不比对 hash：那会把「出处」（官方包的 sha256）复制成第二份，出处只在
    // docs/FRONTEND-THEME.md §5.3/§7 登记。这里只问「文件在不在」。
    const fontPath = resolve(dirname(ICONS_CSS), fontUrl ?? '')
    expect(existsSync(fontPath), `@font-face 指向的字体文件不存在：${fontPath}`).toBe(true)
  })

  it('@font-face 声明的字体名与 .layui-icon 用的是同一个（单侧改名会让字形全失效）', () => {
    const css = readFileSync(ICONS_CSS, 'utf8')

    const declared = fontFamilyOf(fontFaceBody(css) ?? '')
    const used = fontFamilyOf(/\.layui-icon\s*\{([^}]*)\}/.exec(css)?.[1] ?? '')

    expect(declared, '@font-face 里读不到 font-family').toBeTruthy()
    expect(used, '.layui-icon 里读不到 font-family').toBeTruthy()
    expect(used, `@font-face 声明 ${declared}，而 .layui-icon 用的是 ${used}——字形会全部失效`).toBe(
      declared,
    )
  })
})
