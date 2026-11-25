import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { MENU_GROUPS, BOTTOM_ITEMS, TOP_LEVEL_ITEMS } from '../menu'
import { LAYUI_ICON_NAMES, layuiIconCodePoint } from '../layuiIconNames'
import { ACTION_ICONS, BOTTOM_ICONS, GROUP_ICONS, TOP_LEVEL_ICONS, UI_ICONS } from '../menuIcons'
import { V2_GROUP_LABEL_OVERRIDES } from '@/v2/v2Menu'

/**
 * 本文件的三道防线，顺序就是「谁先响」：
 *  1. **类型**：图标名必须属于 `LayuiIconName`——写 `order` 这种不存在的名字**编译就不过**（第一道）；
 *  2. 下面第 2 条测试：名字必须在 196 个注册名里（挡住类型被放宽后的漏网）；
 *  3. 下面第二组读 `layui-icons.css` 的三条测试：**映射名必须在我们自己的 CSS 里真有字形规则**，
 *     码点必须与注册表一致，CSS 里不许有多余规则。
 *  2 与 3 不是重复：**名字存在 ≠ 我们写了它的字形**（196 个注册名里有 187 个我们没有规则），
 *  而「屏幕上有没有图形」只由 3 决定。少了 3，换图标会静默变空白（同类教训见 docs/ERRORS.md E-0009）。
 */

const valid = new Set<string>(LAYUI_ICON_NAMES)

/** 三张映射合并后的「消费方 → 图标名」；下面按 `MAPPED` 遍历的那几条断言走它。
 *  含 `UI_ICONS`（页签条的刷新按钮）、`ACTION_ICONS`（游戏维护三页的按钮）与 `BOTTOM_ICONS`
 *  （侧栏最底部那一条，2026-10-08 新增）：它们与菜单映射**共用同一套字形与同一份 CSS**，
 *  所以「有名字没规则 / 码点不一致 / CSS 多写了规则」这三条同样成立。
 *  ⚠️ 新增任何在 CSS 里写字形的地方，都要把名字登记进这几张表之一——否则 CSS 多写了规则，
 *  本文件的集合相等断言会红（2026-09-20 加动作图标时就红过一次，那是它该提醒的时候）。 */
const MAPPED = {
  ...GROUP_ICONS,
  ...TOP_LEVEL_ICONS,
  ...BOTTOM_ICONS,
  ...UI_ICONS,
  ...ACTION_ICONS,
}

/** 我们自己写的字形规则所在文件（相对本测试文件：`src/config/__tests__/` → `src/styles/`）。 */
const ICONS_CSS_PATH = fileURLToPath(new URL('../../styles/layui-icons.css', import.meta.url))
/** 一条 `.layui-icon-<名字>::before { … }`；规则体单独取，因为它未必都是字体字形。 */
// ⚠️ 冒号**一个或两个都要认**：对标自己的 `layui.css` 用的是单冒号
//（`.layui-icon-delete:before{content:"\e640"}`）。只写 `::before` 的话，照抄那种写法
// 能**静默逃过**下面的集合相等断言（独立评审 M-1 用 node 实测过：单冒号规则被解析成 0 条）。
const GLYPH_RULE = /\.layui-icon-([a-z0-9-]+)::?before\s*\{([^}]*)\}/g
/** 规则体里的 `content: '\e68e'`。 */
const CONTENT_CODE_POINT = /content:\s*'\\([0-9a-fA-F]{4})'/

/** 解析 `layui-icons.css` → 名字到十六进制码点（没有 4 位码点的规则记为 undefined）。 */
function readGlyphRules(css: string): Map<string, string | undefined> {
  const rules = new Map<string, string | undefined>()
  for (const m of css.matchAll(GLYPH_RULE)) {
    const name = m[1]
    const body = m[2]
    if (name === undefined || body === undefined) throw new Error(`字形规则解析失败：${m[0]}`)
    rules.set(name, CONTENT_CODE_POINT.exec(body)?.[1]?.toLowerCase())
  }
  return rules
}

/** 注册表里存的是真字符（`'\uE68E'`），CSS 里是转义（`'\e68e'`），这里归一成同一个十六进制串。 */
function codePointHex(char: string): string {
  return char.codePointAt(0)?.toString(16) ?? ''
}

const glyphs = readGlyphRules(readFileSync(ICONS_CSS_PATH, 'utf8'))

describe('菜单图标映射', () => {
  it('三张映射表都不是空的（空表会让下面的遍历断言空转通过）', () => {
    expect(Object.keys(GROUP_ICONS).length).toBeGreaterThan(0)
    expect(Object.keys(TOP_LEVEL_ICONS).length).toBeGreaterThan(0)
    // ⚠️ 这条对 `UI_ICONS` **必须单独写**：`{ ...undefined }` 在 JS 里是合法的空对象展开，于是
    // 「整张表**连同它在 layui-icons.css 里的规则**一起消失」时没有一条会响——`MAPPED` 静默少一项，
    // 按它遍历的那两条（有规则 / 码点一致）根本走不到那个名字，集合相等则两边同时少一项、互相抵消。
    // （只删表、留着 CSS 规则是另一种情况：那种由集合相等抓住，所以这里不写「遍历断言全部空转」。）
    expect(Object.keys(UI_ICONS).length).toBeGreaterThan(0)
    // ⚠️ 与 `UI_ICONS` 同款、**必须单独写**：`BOTTOM_ICONS` 整个消失时上面那两条遍历断言走不到
    // 它、集合相等也会两边同时少一项而互相抵消（2026-10-08 新增这张表时补的）。
    expect(Object.keys(BOTTOM_ICONS).length).toBeGreaterThan(0)
    expect(Object.keys(MAPPED).length).toBeGreaterThan(0)
  })

  it('只使用 layui 真实存在的图标名（防 E-0005 那类「名字是编的」）', () => {
    for (const [k, v] of Object.entries(MAPPED)) {
      expect(valid.has(v), `${k} 用了未注册的图标名 ${v}`).toBe(true)
    }
  })

  it('每个可见菜单单元都有图标', () => {
    for (const g of MENU_GROUPS.filter((g) => !g.hidden)) {
      expect(GROUP_ICONS[g.name], `分组「${g.name}」缺图标`).toBeTruthy()
    }
    for (const i of TOP_LEVEL_ITEMS.filter((i) => !i.hidden)) {
      expect(TOP_LEVEL_ICONS[i.label], `顶部条目「${i.label}」缺图标`).toBeTruthy()
    }
  })

  it('没有给不存在的菜单单元配图标（改标签后会红；v2 渲染层改名的组名按覆盖表放行）', () => {
    // ⭐【功能维护】这类键不是 `menu.ts` 的组名，而是 v2 侧栏渲染层改名（`V2_GROUP_LABEL_OVERRIDES`）
    // 的目标名——GROUP_ICONS 给它配的别名行照旧要过本网，所以合法键集合 = 菜单组名 ∪ 改名目标名
    //（期望侧 import 同一张覆盖表，⛔ 手抄第二份）。打错字 / 配了无关组名照样红。
    const groupNames = new Set([
      ...MENU_GROUPS.map((g) => g.name),
      ...Object.values(V2_GROUP_LABEL_OVERRIDES),
    ])
    const topLabels = new Set(TOP_LEVEL_ITEMS.map((i) => i.label))
    for (const k of Object.keys(GROUP_ICONS)) expect(groupNames.has(k), `未知分组 ${k}`).toBe(true)
    for (const k of Object.keys(TOP_LEVEL_ICONS)) expect(topLabels.has(k), `未知顶部条目 ${k}`).toBe(true)
  })

  it('侧栏最底部那一条（`BOTTOM_ITEMS`）有图标，且没有给不存在的底部条目配图标', () => {
    // ⚠️ 这条与上面两条**不是重复**：`BOTTOM_ICONS` 的键若与 `GROUP_ICONS` / `TOP_LEVEL_ICONS`
    // 撞名，`MAPPED`（普通对象展开）会**静默合并** ⇒ 按 `MAPPED` 遍历的那几条照样全绿，
    // 而「底部条目漏了图标」这件事没有任何一条会报。判据打**分开的那张表**上就不会被合并吃掉。
    // 本表今天只有一条（`问题反馈` → 复用 `form`）。
    const bottomLabels = new Set(BOTTOM_ITEMS.map((i) => i.label))
    for (const i of BOTTOM_ITEMS) {
      expect(BOTTOM_ICONS[i.label], `底部条目「${i.label}」缺图标`).toBeTruthy()
    }
    for (const k of Object.keys(BOTTOM_ICONS)) {
      expect(bottomLabels.has(k), `未知底部条目 ${k}`).toBe(true)
    }
  })
})

describe('映射 ↔ layui-icons.css 的字形规则', () => {
  it('layui-icons.css 里解析出了字形规则（否则下面三条会空转通过）', () => {
    expect(glyphs.size).toBeGreaterThan(0)
  })

  it('映射里的每个名字在 layui-icons.css 里都有 ::before 字形规则', () => {
    for (const [k, name] of Object.entries(MAPPED)) {
      expect(
        glyphs.has(name),
        `${k} → ${name} 在 layui-icons.css 里没有字形规则（换图标时只改了映射、忘了加规则？）`,
      ).toBe(true)
    }
  })

  it('每条字形规则的码点与注册表一致（名字对、码点错会渲染成另一个真实字形）', () => {
    for (const [k, name] of Object.entries(MAPPED)) {
      const expected = layuiIconCodePoint(name)
      expect(expected, `${name} 在注册表里不是字体字形（码点为 null）`).not.toBeNull()
      expect(glyphs.get(name), `${k} → ${name} 的码点`).toBe(
        expected === null ? '' : codePointHex(expected),
      )
    }
  })

  it('CSS 里的字形规则集合与映射用到的名字集合完全相等（多写的规则也算失败）', () => {
    const used = new Set<string>(Object.values(MAPPED))
    expect([...glyphs.keys()].sort()).toEqual([...used].sort())
  })
})
