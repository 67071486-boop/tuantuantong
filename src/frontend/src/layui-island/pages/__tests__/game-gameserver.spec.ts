// @vitest-environment jsdom
import fs from 'node:fs'
import path from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { GAME_LIBRARY } from '@/config/game-library'

/**
 * 游戏管理页（对标 `/Game/gameserver`）＋ 新增弹窗（对标 `/Game/AddEditGame`）的**防漂移测试**。
 *
 * 值全部来自对标产物（**读码 / 实测**）：
 *  - `.tools/recon/gameconfig/js/gameserver.js`（列定义 / 层参数 / 删除确认语）；
 *  - `.tools/recon/gameconfig/js/addeditgame.js`（字母片 / 下拉 / 提交 / 提示文案）；
 *  - `.tools/recon/gameconfig/AddEditGame.html`（597 个单选的属性、`blockquote` 原文、页脚按钮）；
 *  - `.tools/recon/gameconfig/++` 里的 `Game__gameserver.html`（工具栏 / 开关模板 / 页内 `<style>`）。
 *
 * ## 本文件覆盖的三件事（都是"删掉/改错都不报错"的那类）
 *
 * 1. **列表只剩 4 列**：四个开关列是业务方 2026-09-22 裁定删掉的**刻意偏离**
 *    （对标是 8 列）——钉住它，别被"按对标补回来"。
 * 2. **游戏库数据**：597 款、逐项属性、以及那 8 项脏 `data-firstKey`（照抄对标的缺陷）。
 * 3. **弹窗真能打字真能提交**：挂载页面 → 点「新增一款游戏」→ 弹层里 597 个单选 + 27 个字母片
 *    → 不选就提交要提示原文 → 选一款提交只发 `{name}`。
 *    （E-0061：`typecheck` 看不见 DOM 选择器、`lint` 看不见类名多一个点，页测试是唯一兜底。）
 */
const HERE = path.dirname(__filename)
const REPLICA_DIR = path.resolve(HERE, '../../../../public/replica')
const artifact = fs.readFileSync(path.join(REPLICA_DIR, 'game-gameserver.html'), 'utf8')
const pickerArtifact = fs.readFileSync(path.join(REPLICA_DIR, 'game-addeditgame.html'), 'utf8')
const doc = new DOMParser().parseFromString(artifact, 'text/html')
const pickerDoc = new DOMParser().parseFromString(pickerArtifact, 'text/html')

type Col = Record<string, unknown>
const titled = (cols: Col[]): Col[] => cols.filter((c) => typeof c.title === 'string')

/* ─────────────────────────── layui 替身（jsdom 里加载不了真 layui） ─────────────────────────── */

const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  const tableHandlers = new Map<string, (obj: unknown) => unknown>()
  const formHandlers = new Map<string, (obj: unknown) => unknown>()
  const tableRenders: Array<Record<string, unknown>> = []
  const reloads: Array<{ id: string; data: unknown }> = []
  const opens: Array<Record<string, unknown>> = []
  const alerts: string[] = []
  /** `layer.confirm` 收到的确认文案（删除警告的判据）。 */
  const confirms: string[] = []
  /** `table.cache`：页面按 id 找行（删除警告要读引用计数）。 */
  const cache: Record<string, unknown[]> = {}
  return {
    msg,
    tableHandlers,
    formHandlers,
    tableRenders,
    reloads,
    opens,
    alerts,
    confirms,
    cache,
    form: {
      render: vi.fn(),
      on: vi.fn((event: string, cb: (obj: unknown) => unknown) => {
        formHandlers.set(event, cb)
      }),
    },
    table: {
      render: vi.fn((opts: Record<string, unknown>) => {
        tableRenders.push(opts)
      }),
      reloadData: vi.fn((id: string, opts: { data: unknown }) => {
        reloads.push({ id, data: opts.data })
        // 真 layui 会把行数据留在 `table.cache[id]`（页面按 id 找那一行时要用）。
        cache[id] = (opts.data ?? []) as unknown[]
      }),
      cache,
      on: vi.fn((event: string, cb: (obj: unknown) => unknown) => {
        tableHandlers.set(event, cb)
      }),
    },
    layer: {
      /** 真 layui 的 `layer.open({type:1, content:'<div>…'})` 把这段 HTML 塞进 `.layui-layer-content`。 */
      open: vi.fn((opts: Record<string, unknown>) => {
        opens.push(opts)
        let host = document.querySelector('.layui-layer-content')
        if (!host) {
          host = document.createElement('div')
          host.className = 'layui-layer-content'
          document.body.appendChild(host)
        }
        host.innerHTML = String(opts.content ?? '')
        ;(opts.success as (() => void) | undefined)?.()
        return opens.length
      }),
      alert: vi.fn((text: string, cb?: () => void) => {
        alerts.push(text)
        cb?.()
      }),
      closeAll: vi.fn(),
      close: vi.fn(),
      // 真 layui 的 `layer.confirm(text, opts, yes)`：点「确定」走 `yes(index)`；
      // 这里把文案记下来（删除警告的判据就在文案里）。
      confirm: vi.fn((text: string, _o: unknown, ok: (i: number) => void) => {
        confirms.push(text)
        ok(1)
      }),
      msg,
    },
    $: (el: Element) => el,
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => ({
    form: LAYUI.form,
    table: LAYUI.table,
    layer: LAYUI.layer,
    $: LAYUI.$,
  })),
  initIslandElements: vi.fn(),
  islandMsg: (_layer: unknown, text: string) => LAYUI.msg(text),
}))

import {
  ADD_CHARS,
  ALL_CHAR,
  COLS,
  CREATED_MESSAGE,
  FOUND_MESSAGE,
  LOGO_INPUT_CLASS,
  LOGO_INPUT_SELECTOR,
  NOT_PICKED_MESSAGE,
  PICKER_CLASS,
  PICKER_LIST_ID,
  buildCharChipsHtml,
  buildGameOptionsHtml,
  buildGameRadiosHtml,
  fillPicker,
  deleteWarning,
  clearHighlights,
  highlightByLetter,
  highlightByValue,
  logoCellHtml,
  logoFileExt,
  pickedGameError,
} from '../game-gameserver'
import { DELETED_BUTTON_LABEL } from '@/layui-island/config-replica'
import { bootstrapIsland, initIslandElements } from '@/layui/island'
import page from '../game-gameserver'

/** 对标 `AddEditGame.html` 里那种结构：`input[name=games]` + layui 渲染出来的标题 div。 */
function mountRadioFixture(pairs: ReadonlyArray<readonly [string, string]>): HTMLElement {
  const box = document.createElement('div')
  box.className = PICKER_CLASS
  box.innerHTML = pairs
    .map(
      ([firstKey, name]) =>
        `<div class="layui-inline" style="width:200px">` +
        `<input type="radio" name="games" value="${name}" title="${name}" data-name="${name}" data-firstKey="${firstKey}" lay-filter="" />` +
        `<div class="layui-unselect layui-form-radio"><i class="layui-icon"></i><div>${name}</div></div>` +
        `</div>`,
    )
    .join('')
  document.body.appendChild(box)
  return box
}

const titleDivOf = (box: HTMLElement, i: number): HTMLElement =>
  box.querySelectorAll<HTMLElement>('.layui-form-radio > div')[i]!

describe('列：对标 `gameserver.js` 的 cols **减去四个开关列** + 我方「图标」列', () => {
  it('共 5 列（对标剩 4 列 + 我方新增「图标」）、**没有**勾选列（对标该表不写 `type:checkbox`）', () => {
    expect(COLS.length).toBe(5)
    expect(COLS.some((c) => c.type !== undefined)).toBe(false)
  })

  it('列名序列逐字一致（游戏Id / 游戏名称 / 图标 / 游戏排序 / 操作）', () => {
    expect(titled(COLS).map((c) => String(c.title))).toEqual([
      '游戏Id',
      '游戏名称',
      '图标',
      '游戏排序(数据越大排序越靠前)',
      '操作',
    ])
  })

  it('四个开关列**一个都不在**（业务方 2026-09-22 裁定；对标是 8 列）', () => {
    const titles = titled(COLS).map((c) => String(c.title))
    for (const label of ['是否默认', '是否为手机游戏', '是否为陪玩游戏', '是否禁发短信']) {
      expect(titles.some((t) => t.includes(label)), `列表里仍有「${label}」列`).toBe(false)
    }
    // 那三列的红色问号图标也一并消失（它是跟着表头文案走的）。
    expect(titles.some((t) => t.includes('layui-bangzhu'))).toBe(false)
    // 反向哨兵：列本身还在（别把表删空当成"通过"）。
    expect(titles.length).toBe(5)
  })

  it('宽度逐条对齐（图标 150 / 排序 200 / 操作 100；游戏名称自动宽）', () => {
    const byTitle = new Map(titled(COLS).map((c) => [String(c.title), c]))
    expect(byTitle.get('游戏名称')!.width).toBeUndefined()
    expect(byTitle.get('图标')!.width).toBe(150)
    expect(byTitle.get('游戏排序(数据越大排序越靠前)')!.width).toBe(200)
    expect(byTitle.get('操作')!.width).toBe(100)
  })

  it('`gameId` 列 `hide:true` + `fixed:left`（对标原文；漏 hide 会多一列）', () => {
    expect(COLS[0]!.field).toBe('gameId')
    expect(COLS[0]!.hide).toBe(true)
    expect(COLS[0]!.fixed).toBe('left')
  })

  it('前两列固定在最左；操作列**不固定**（对标写的是空串）', () => {
    expect(COLS[1]!.fixed).toBe('left')
    expect(COLS[COLS.length - 1]!.fixed).toBeUndefined()
  })

  it('排序列是**行内可编辑**（`edit:true`，对标 `orderNum`）+ 渲染真值', () => {
    expect(COLS[3]!.edit).toBe(true)
    expect(COLS[3]!.field).toBe('orderNum')
    expect(COLS[3]!.align).toBe('center')
    const col = COLS[3] as unknown as { templet: (d: { sort_order: number }) => string }
    expect(col.templet({ sort_order: 7 })).toBe('7')
  })

  it('「图标」列：有 `logo_url` 渲染 `<img>`，没有 / 空串渲染首字占位（不留空 src）', () => {
    const col = COLS[2] as unknown as { templet: (d: unknown) => string }
    const withLogo = col.templet({ id: 1, name: '原神', logo_url: '/api/media/blob/games/1.png' })
    expect(withLogo).toContain('<img src="/api/media/blob/games/1.png"')
    expect(withLogo).toContain('data-act="logo"')
    expect(withLogo).toContain('换图标')
    const noLogo = col.templet({ id: 2, name: '绝区零', logo_url: '' })
    expect(noLogo).not.toContain('<img')
    expect(noLogo).toContain('>绝</span>')
    // 占位是首字 + 蓝底色块，不是空白格。
    expect(noLogo).toContain('background:#EAF1FE')
  })

  it('操作列是**实心红色**按钮（`layui-btn-xs layui-btn-danger`，不是无边框文字）', () => {
    const op = COLS[COLS.length - 1] as unknown as { templet: (d: { id: number }) => string }
    const html = op.templet({ id: 7 })
    expect(html).toContain('layui-btn-xs')
    expect(html).toContain('layui-btn-danger')
    expect(html).toContain('layui-icon-delete')
    expect(html).toContain('删除游戏')
    expect(html).toContain('data-id="7"')
    expect(html).toContain('style="margin-right:3px"')
  })

  it('取票的 `ext` 是小写、不带点的扩展名；没有点给空串（让后端回它那句文案）', () => {
    expect(logoFileExt('logo.PNG')).toBe('png')
    expect(logoFileExt('a.b.jpeg')).toBe('jpeg')
    expect(logoFileExt('logo')).toBe('')
    expect(logoFileExt('')).toBe('')
  })

  it('游戏名是蓝链但**没有点击处理**（对标 `editGameName()` 是 0 调用点死代码）', () => {
    const col = COLS[1] as unknown as { templet: (d: { id: number; name: string }) => string }
    const html = col.templet({ id: 3, name: '原神' })
    expect(html).toContain('color:#1E9FFF')
    expect(html).toContain('>原神</a>')
    // 对标的蓝链是 `<a href="javascript:void(0)">`，没有 `data-act`（我们不会给它接编辑入口）。
    expect(html).not.toContain('data-act')
  })

  it('游戏名走转义（它是用户输入，直接拼进 innerHTML 就是存储型 XSS）', () => {
    const col = COLS[1] as unknown as { templet: (d: { id: number; name: string }) => string }
    const html = col.templet({ id: 1, name: '<img src=x onerror=alert(1)>' })
    expect(html).not.toContain('<img')
    expect(html).toContain('&lt;img')
  })
})

describe('游戏库数据（对标平台库的副本）', () => {
  it('597 款，且顺序与对标一致（首尾哨兵）', () => {
    expect(GAME_LIBRARY.length).toBe(597)
    expect(GAME_LIBRARY[0]).toEqual(['S', '三角洲行动'])
    expect(GAME_LIBRARY[596]).toEqual(['A', 'ARC突袭者'])
  })

  it('`卡厄思梦境` 出现两次（对标原样；后端唯一约束会拒第 2 次）', () => {
    expect(GAME_LIBRARY.filter(([, name]) => name === '卡厄思梦境').length).toBe(2)
  })

  it('那 8 项取不到字母片的脏数据照抄（7 个 `*` + 1 个小写 `b`）', () => {
    const odd = GAME_LIBRARY.filter(([firstKey]) => !/^[A-Z]$/.test(firstKey))
    expect(odd.length).toBe(8)
    expect(odd.filter(([k]) => k === '*').length).toBe(7)
    expect(odd.map(([k]) => k)).toContain('b')
  })

  it('清单里没有需要转义的字符（所以属性拼接不会破；破例时这条会红）', () => {
    for (const [, name] of GAME_LIBRARY) expect(name).not.toMatch(/[&<>"']/)
  })
})

describe('新增弹窗的标记（纯函数，可重跑）', () => {
  it('字母片 = `全部` + A–Z 共 27 个（对标 `charList`）', () => {
    expect(ADD_CHARS.length).toBe(27)
    expect(ADD_CHARS[0]).toBe(ALL_CHAR)
    expect(ADD_CHARS[1]).toBe('A')
    expect(ADD_CHARS[26]).toBe('Z')
    const html = buildCharChipsHtml()
    expect(html.match(/<span class='item'>/g)!.length).toBe(27)
    expect(html).toContain("<span class='item'>全部</span>")
  })

  it('597 个单选，属性逐项照抄（`name/value/title/data-name/data-firstKey/lay-filter=""`）', () => {
    const html = buildGameRadiosHtml()
    expect(html.match(/<input type="radio" name="games"/g)!.length).toBe(597)
    expect(html.match(/style="width:200px"/g)!.length).toBe(597)
    expect(html).toContain('value="三角洲行动"')
    expect(html).toContain('title="三角洲行动"')
    expect(html).toContain('data-name="三角洲行动"')
    expect(html).toContain('data-firstKey="S"')
    expect(html).toContain('lay-filter=""')
  })

  it('**刻意不搬** `data-isPw` / `data-isMoblie`（喂那两个已删开关的死数据）', () => {
    const html = buildGameRadiosHtml()
    expect(html).not.toContain('data-isPw')
    expect(html).not.toContain('data-isMoblie')
  })

  it('下拉项 597 个，且**不含**空占位（占位在证据件里，静态）', () => {
    const html = buildGameOptionsHtml()
    expect(html.match(/<option value=/g)!.length).toBe(597)
    expect(html).toContain('<option value="原神">原神</option>')
    expect(html).not.toContain('请选择或搜索')
  })
})

describe('新增弹窗的证据件（`public/replica/game-addeditgame.html`）', () => {
  it('四个标签块 + 页脚按钮的文案逐字（读码 `AddEditGame.html`）', () => {
    const text = pickerDoc.body.textContent ?? ''
    for (const label of ['按字母索引', '搜索游戏名称', '请选择游戏', '确定添加到本系统']) {
      expect(text, `弹窗里缺「${label}」`).toContain(label)
    }
    expect(text).toContain('如果列表里没有找到需要添加的游戏')
  })

  it('`blockquote` 是橙色 `#ff6a00`，且品牌名改成**团团通**（不把用户指去鸡蛋铺）', () => {
    const quote = pickerDoc.querySelector('blockquote')!
    expect(quote.getAttribute('style')).toBe('color:#ff6a00')
    expect(quote.textContent).toContain('团团通')
    expect(quote.textContent).not.toContain('鸡蛋铺')
  })

  it('页内 `<style>` 逐字带上那 4 条（字母片 + 两种高亮色），值是标的值', () => {
    const style = pickerDoc.querySelector('style')!.textContent ?? ''
    expect(style).toContain('.charViewboxs .item')
    expect(style).toContain('width: 36px')
    expect(style).toContain('border: 1px solid #A3AFB7')
    expect(style).toContain('.charViewboxs .item.active')
    expect(style).toContain('#FDA601')
    expect(style).toContain('.layui-inline .layui-form-radio div.sel2')
    expect(style).toContain('#ff0000')
    expect(style).toContain('font-size:24px')
  })

  it('下拉与搜索按钮照抄（`lay-search` / `lay-filter=seachGame` / 120px 的搜索按钮）', () => {
    const select = pickerDoc.querySelector('select')!
    expect(select.hasAttribute('lay-search')).toBe(true)
    expect(select.getAttribute('lay-filter')).toBe('seachGame')
    // 证据件里**只有**空占位那一项（597 项由页面运行期灌）。
    expect(select.querySelectorAll('option').length).toBe(1)
    expect(select.querySelector('option')!.getAttribute('value')).toBe('')
    const search = pickerDoc.querySelector('button[lay-filter="seachBtn"]')!
    expect(search.getAttribute('style')).toBe('width: 120px')
    expect(search.className).toBe('layui-btn layui-btn-normal')
    expect(search.textContent).toBe('搜索')
  })

  it('597 个单选的落点容器**是空的**（数据在 `config/game-library.ts`，不重复两份）', () => {
    const box = pickerDoc.querySelector(`#${PICKER_LIST_ID}`)!
    expect(box.innerHTML.trim()).toBe('')
    expect(box.querySelectorAll('input').length).toBe(0)
  })

  it('表单加了 `onsubmit="return false"`（对标没有；我们的弹层住在岛页文档里，隐式提交会导航走整页）', () => {
    expect(pickerDoc.querySelector('form')!.getAttribute('onsubmit')).toBe('return false')
  })
})

describe('新增弹层的游戏库填充：重复灌不会翻倍', () => {
  it('下拉替换而不是追加，并保留已选中的游戏', () => {
    const box = document.createElement('div')
    box.innerHTML = `<div class="charViewboxs"></div><select lay-filter="seachGame"><option value="">请选择或搜索</option></select><div id="${PICKER_LIST_ID}"></div>`
    const library: ReadonlyArray<readonly [string, string]> = [['L', '蓝色星原：旅谣'], ['Y', '原神']]
    fillPicker(box, library)
    const radios = box.querySelectorAll<HTMLInputElement>('input[name="games"]')
    radios[1]!.checked = true
    fillPicker(box, [...library, ['X', '新游戏']])
    expect(box.querySelectorAll('select[lay-filter="seachGame"] option')).toHaveLength(4)
    const picked = box.querySelector<HTMLInputElement>('input[name="games"]:checked')
    expect(picked?.value).toBe('原神')
  })
})

describe('列表证据件（`public/replica/game-gameserver.html`）：照抄对标标记', () => {
  it('`<form class="layui-form">` 包住表格（对标 `Game__gameserver.html` L79 开 / L105 闭）', () => {
    const form = doc.querySelector('form.layui-form')
    expect(form).toBeTruthy()
    expect(form!.querySelector('table#tableList[lay-filter="tableList"]')).toBeTruthy()
  })

  it('页内 `<style>` 逐字带上 `.layui-table-cell` 与 `.lableb`（这是该页的局部覆盖）', () => {
    const style = doc.querySelector('style')!.textContent ?? ''
    expect(style).toContain('.layui-table-cell')
    expect(style).toContain('height: 40px')
    expect(style).toContain('padding: 0 5px')
    expect(style).toContain('.lableb')
  })

  it('工具栏三颗按钮（对标两颗 ＋ **我方新增的「已删除」**）：文案 + 配色 + `lay-event` 逐字', () => {
    const toolbarHtml = doc.querySelector('#toolbarDemo')!.innerHTML
    const toolbar = new DOMParser().parseFromString(toolbarHtml, 'text/html')
    const buttons = Array.from(toolbar.querySelectorAll('button'))
    // ⚠️ 对标工具栏只有 2 颗（刷新 + 新增）。第 3 颗是**我方新增**的「已删除」入口——
    // 删除改成软删除（D-0085）之后必须有地方能看/恢复已删除的游戏（对标没有这个功能）。
    expect(buttons.length).toBe(3)
    const [refresh, add, deleted] = buttons
    expect(refresh!.getAttribute('lay-event')).toBe('refresh')
    expect(refresh!.getAttribute('title')).toBe('刷新当前数据')
    expect(refresh!.className).toBe('layui-btn layui-btn-xs')
    // `&#xe669;` 是一个私有区字形（layui 图标字体），`textContent` 里会带上它，比文案前先剥掉。
    const text = (el: Element): string => (el.textContent ?? '').replace(/[\uE000-\uF8FF]/g, '').trim()
    expect(text(refresh!)).toBe('刷新游戏列表')
    // 刷新那颗**没有**内联 style（吃 `.layui-btn` 的 margin-right:8px）。
    expect(refresh!.hasAttribute('style')).toBe(false)
    // 刷新那颗的字形是 layui 的 `&#xe669;`（解析后 = U+E669）。
    expect(refresh!.textContent).toContain('\uE669')

    expect(add!.getAttribute('lay-event')).toBe('addGameBtn')
    expect(add!.getAttribute('title')).toBe('新增一款游戏')
    expect(add!.className).toBe('layui-btn layui-btn-xs layui-btn-danger')
    expect(add!.getAttribute('style')).toBe('margin-right:5px')
    expect(text(add!)).toBe('新增一款游戏')
    expect(add!.innerHTML).toContain('layui-jia')

    expect(deleted!.getAttribute('lay-event')).toBe('deletedBtn')
    expect(deleted!.getAttribute('title')).toBe('查看并恢复已删除的游戏')
    expect(text(deleted!)).toBe(DELETED_BUTTON_LABEL)
    // 官方字形（对标那几个私有字形取不到码点）。
    expect(deleted!.innerHTML).toContain('layui-icon-return')
  })

  it('行内操作模板 `#baroperating` 用的是对标原文（图标类 `layui-icon-delete`）', () => {
    const bar = doc.querySelector('#baroperating')!.textContent ?? ''
    expect(bar).toContain('layui-btn-danger')
    expect(bar).toContain('layui-icon-delete')
    expect(bar).toContain('删除游戏')
    expect(bar).toContain('style="margin-right:3px"')
  })

  it('没有分页痕迹（对标 `page:false`，一次拉满 200 条）', () => {
    expect(artifact).not.toContain('layui-laypage')
  })
})

describe('高亮：两条路都不是"筛选"，只换字色（对标 `initselgames()` / `select(seachGame)`）', () => {
  const PAIRS = [
    ['S', '三角洲行动'],
    ['M', '魔兽世界'],
    ['S', '失控进化'],
    ['*', '喋血复仇'],
  ] as const

  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('点字母只给 `data-firstKey` 相同的项加 `sel`，其它项不加', () => {
    const box = mountRadioFixture(PAIRS)
    highlightByLetter(box, 'S')
    expect(titleDivOf(box, 0).classList.contains('sel')).toBe(true)
    expect(titleDivOf(box, 1).classList.contains('sel')).toBe(false)
    expect(titleDivOf(box, 2).classList.contains('sel')).toBe(true)
    // 597 项不会因为点字母而消失（它不是筛选）。
    expect(box.querySelectorAll('input[name="games"]').length).toBe(4)
  })

  it('点「全部」= 清空高亮（对标那边是谁也匹配不到 ⇒ 高亮全清）', () => {
    const box = mountRadioFixture(PAIRS)
    highlightByLetter(box, 'S')
    highlightByLetter(box, ALL_CHAR)
    expect(box.querySelectorAll('.sel').length).toBe(0)
  })

  it('脏 `data-firstKey`（`*`）点**任何一个字母片**都高亮不到（照抄对标的缺陷）', () => {
    const box = mountRadioFixture(PAIRS)
    // 字母片只有 `全部` + A–Z，`*` 根本不在其中 ⇒ 那一项永远不会被点亮。
    expect((ADD_CHARS as readonly string[]).includes('*')).toBe(false)
    for (const letter of ADD_CHARS) {
      highlightByLetter(box, letter)
      expect(titleDivOf(box, 3).classList.contains('sel'), `点「${letter}」时脏项被点亮了`).toBe(false)
    }
  })

  it('下拉选中按 `data-name` 加 `sel2` 并返回命中数（`卡厄思梦境` 那种重名会返回 2）', () => {
    const box = mountRadioFixture([
      ['K', '卡厄思梦境'],
      ['M', '魔兽世界'],
      ['K', '卡厄思梦境'],
    ] as const)
    expect(highlightByValue(box, '魔兽世界')).toBe(1)
    expect(titleDivOf(box, 1).classList.contains('sel2')).toBe(true)
    expect(highlightByValue(box, '卡厄思梦境')).toBe(2)
    expect(titleDivOf(box, 0).classList.contains('sel2')).toBe(true)
    expect(titleDivOf(box, 2).classList.contains('sel2')).toBe(true)
    // 换一次选中要清掉上一次的高亮（对标每次都先 removeClass）。
    expect(highlightByValue(box, '魔兽世界')).toBe(1)
    expect(box.querySelectorAll('.sel2').length).toBe(1)
  })

  it('空值（占位「请选择或搜索」）命中 0、且清掉高亮', () => {
    const box = mountRadioFixture(PAIRS)
    expect(highlightByValue(box, 'S')).toBe(0)
    expect(highlightByValue(box, '')).toBe(0)
    expect(box.querySelectorAll('.sel, .sel2').length).toBe(0)
  })

  it('`clearHighlights` 两种类一起清', () => {
    const box = mountRadioFixture(PAIRS)
    highlightByLetter(box, 'S')
    clearHighlights(box)
    expect(box.querySelectorAll('.sel, .sel2').length).toBe(0)
  })
})

describe('「没选游戏」的判据（纯函数）', () => {
  it('空 / null / undefined ⇒ 对标原文提示；有值 ⇒ 通过', () => {
    expect(pickedGameError('')).toBe(NOT_PICKED_MESSAGE)
    expect(pickedGameError(null)).toBe(NOT_PICKED_MESSAGE)
    expect(pickedGameError(undefined)).toBe(NOT_PICKED_MESSAGE)
    expect(pickedGameError('原神')).toBeNull()
  })

  it('提示文案逐字取对标 `addeditgame.js` L74', () => {
    expect(NOT_PICKED_MESSAGE).toBe('请选择要添加的游戏')
  })
})

/* ───────────────────────────── 挂载：驱动真交互 ───────────────────────────── */

type FetchCall = { url: string; method: string; body: unknown }

function flush(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

describe('挂载后的真实流程：点「新增一款游戏」→ 挑一款 → 提交', () => {
  let root: HTMLElement
  const calls: FetchCall[] = []
  let listItems: unknown[] = []

  beforeEach(() => {
    vi.clearAllMocks()
    LAYUI.tableHandlers.clear()
    LAYUI.formHandlers.clear()
    LAYUI.tableRenders.length = 0
    LAYUI.reloads.length = 0
    LAYUI.opens.length = 0
    LAYUI.alerts.length = 0
    LAYUI.confirms.length = 0
    calls.length = 0
    listItems = []
    document.body.innerHTML = ''
    document.cookie = 'csrftoken=test-token'
    root = document.createElement('div')
    document.body.appendChild(root)

    vi.stubGlobal('fetch', async (input: unknown, init?: { method?: string; body?: string }) => {
      const url = String(input)
      const method = (init?.method ?? 'get').toUpperCase()
      calls.push({ url, method, body: init?.body ? JSON.parse(init.body) : undefined })
      if (url.includes('/replica/')) {
        const key = url.split('/').pop()!.replace('.html', '')
        const file = path.join(REPLICA_DIR, `${key}.html`)
        return {
          ok: true,
          status: 200,
          text: async () => fs.readFileSync(file, 'utf8'),
          json: async () => ({}),
        }
      }
      if (url.includes('/api/business/games/')) {
        if (method === 'POST') {
          return {
            ok: true,
            status: 200,
            json: async () => ({ code: 0, msg: '', data: { id: 9, name: 'x' } }),
          }
        }
        return {
          ok: true,
          status: 200,
          json: async () => ({
            code: 0,
            msg: '',
            data: { items: listItems, total: listItems.length, page: 1, page_size: 200 },
          }),
        }
      }
      throw new Error(`测试里没接这个 URL：${url}`)
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.clearAllMocks()
  })

  async function mountPage(): Promise<void> {
    await page.mount(root, {} as never)
    await flush()
  }

  /** 点工具栏那颗「新增一款游戏」（走 layui 的 toolbar 事件）。 */
  async function clickAdd(): Promise<void> {
    const handler = LAYUI.tableHandlers.get('toolbar(tableList)')!
    handler({ event: 'addGameBtn' })
    await flush()
  }

  it('挂载即一页拉全（`page=1&page_size=200`），行数据进表', async () => {
    listItems = [{ id: 1, name: '原神', sort_order: 0 }]
    await mountPage()
    const list = calls.find((c) => c.url.includes('/api/business/games/'))!
    expect(list.url).toContain('/api/business/games/?page=1&page_size=200')
    expect(LAYUI.reloads[LAYUI.reloads.length - 1]).toEqual({ id: 'tableList', data: listItems })
  })

  it('点「刷新游戏列表」：重取一次列表并提示「已刷新」（三页刷新重做，2026-09-22）', async () => {
    await mountPage()
    LAYUI.msg.mockClear()
    const before = calls.filter((c) => c.url.includes('/api/business/games/')).length
    LAYUI.tableHandlers.get('toolbar(tableList)')!({ event: 'refresh' })
    await flush()
    await flush()
    expect(calls.filter((c) => c.url.includes('/api/business/games/')).length).toBe(before + 1)
    expect(LAYUI.msg).toHaveBeenCalledWith('已刷新', { time: 1500 })
  })

  it('点「新增一款游戏」：弹层参数照抄对标，弹层里真灌进 27 个字母片 + 598 个 option + 597 个单选', async () => {
    await mountPage()
    await clickAdd()

    const opts = LAYUI.opens[LAYUI.opens.length - 1]!
    expect(opts.type).toBe(1)
    expect(opts.title).toBe('新增游戏')
    expect(opts.area).toEqual(['85%', '63%'])
    expect(opts.skin).toBe('layui-layer-lan')
    expect(opts.anim).toBe(5)
    expect(opts.shade).toEqual([0.3, '#333'])
    expect(opts.shadeClose).toBe(true)

    const box = document.querySelector<HTMLElement>(`.layui-layer-content > .${PICKER_CLASS}`)!
    expect(box, '弹层里没有包裹容器（类名/选择器写岔了？）').toBeTruthy()
    expect(box.querySelectorAll('.charViewboxs .item').length).toBe(27)
    expect(box.querySelectorAll('.charViewboxs .item.active').length).toBe(1)
    // 597 项 + 证据件里那 1 个空占位。
    expect(box.querySelectorAll('select[lay-filter="seachGame"] option').length).toBe(598)
    expect(box.querySelectorAll('input[name="games"]').length).toBe(597)
    // 弹层内容是刚插进去的 ⇒ 必须再 `form.render()` 一次（否则是浏览器原生圆点）。
    expect(LAYUI.form.render).toHaveBeenCalled()
  })

  it('弹层里的字母片是真能点的（document 级委托；点「S」给对应项上 `active`）', async () => {
    await mountPage()
    await clickAdd()
    const box = document.querySelector<HTMLElement>(`.layui-layer-content > .${PICKER_CLASS}`)!
    const chips = box.querySelectorAll<HTMLElement>('.charViewboxs .item')
    const s = Array.from(chips).find((c) => c.textContent === 'S')!
    s.click()
    expect(s.classList.contains('active')).toBe(true)
    expect(chips[0]!.classList.contains('active')).toBe(false)
  })

  it('没选就提交 ⇒ 弹对标原文提示、**不发请求**、不关层', async () => {
    await mountPage()
    await clickAdd()
    const before = calls.length
    LAYUI.formHandlers.get('submit(savebtn)')!({})
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith(NOT_PICKED_MESSAGE, { time: 1500 })
    expect(calls.length).toBe(before)
    expect(LAYUI.alerts).toEqual([])
  })

  it('选一款提交 ⇒ `POST {name}` 只带游戏名，然后提示成功、关层、刷新列表', async () => {
    listItems = [{ id: 1, name: '原神', sort_order: 0 }]
    await mountPage()
    await clickAdd()
    const box = document.querySelector<HTMLElement>(`.layui-layer-content > .${PICKER_CLASS}`)!
    box.querySelectorAll<HTMLInputElement>('input[name="games"]')[1]!.checked = true
    LAYUI.formHandlers.get('submit(savebtn)')!({})
    await flush()

    const post = calls.find((c) => c.method === 'POST')!
    expect(post.url).toContain('/api/business/games/')
    // ⚠️ 只发名字：四个开关字段本次已从页面删掉（数据库字段保留，见实现文件头 §一）。
    expect(post.body).toEqual({ name: GAME_LIBRARY[1]![1] })
    expect(LAYUI.alerts).toEqual([CREATED_MESSAGE])
    expect(LAYUI.layer.closeAll).toHaveBeenCalled()
    // 关层之后列表被重新取了一次：挂载 1 次 + 关层 1 次 = 2 条 GET（POST 不算）。
    const gets = calls.filter((c) => c.method === 'GET' && c.url.includes('/api/business/games/'))
    expect(gets.length).toBe(2)
  })

  /**
   * 业务方 2026-09-24 报的 bug：「新增一款游戏成功后，继续新增一款游戏点击就没反应了，
   * 只有刷新浏览器后才可以继续新增」。
   *
   * 根因是**重入闸没复位**：`saving`（对标 `addeditgame.js` 里那个 `re` 布尔）在成功分支**不复位**，
   * 而它住在 `onReady` 的闭包里、活得比弹层久 ⇒ 一层被关掉后再开，闸门仍然是「正在提交」。
   * 对标那边之所以看不出来：它的新增页是 `type:2` 的**独立 iframe 文档**，重新打开 = **新文档** =
   * `re` 重新初始化为 `true`；我们按 spec 偏差表改成了 `type:1` 的**同文档弹层** ⇒ 缺陷就露出来了。
   *
   * ⚠️ 判据打在**真实出口**（第二笔 POST 发没发出去）上，不打在 `saving` 这个内部标志上——
   * 「点了没反应」的准确形状就是「一笔请求都没发、也没有任何提示」。
   */
  it('新增成功后**不刷新页面**再新增一款：第二次点「确定添加到本系统」照样发请求', async () => {
    listItems = []
    await mountPage()

    // 第一次：点「新增一款游戏」→ 选一款 → 提交成功。
    await clickAdd()
    let box = document.querySelector<HTMLElement>(`.layui-layer-content > .${PICKER_CLASS}`)!
    box.querySelectorAll<HTMLInputElement>('input[name="games"]')[0]!.checked = true
    LAYUI.formHandlers.get('submit(savebtn)')!({})
    await flush()
    expect(calls.filter((c) => c.method === 'POST').length).toBe(1)
    expect(LAYUI.alerts).toEqual([CREATED_MESSAGE])

    // 第二次：再点一次工具栏那颗按钮（弹层被重新灌满 597 项），再选一款、再提交。
    await clickAdd()
    box = document.querySelector<HTMLElement>(`.layui-layer-content > .${PICKER_CLASS}`)!
    expect(box.querySelectorAll('input[name="games"]').length).toBe(597)
    box.querySelectorAll<HTMLInputElement>('input[name="games"]')[1]!.checked = true
    LAYUI.formHandlers.get('submit(savebtn)')!({})
    await flush()

    const posts = calls.filter((c) => c.method === 'POST')
    expect(
      posts.length,
      '第二次点「确定添加到本系统」一笔请求都没发（重入闸没复位 ⇒ 静默 return，用户看到的就是「点了没反应」）',
    ).toBe(2)
    expect(posts[1]!.body).toEqual({ name: GAME_LIBRARY[1]![1] })
    expect(LAYUI.alerts).toEqual([CREATED_MESSAGE, CREATED_MESSAGE])
  })

  it('下拉选中同名项 ⇒ 弹「已找到」原文（对标 `select(seachGame)`）', async () => {
    await mountPage()
    await clickAdd()
    LAYUI.formHandlers.get('select(seachGame)')!({ value: '魔兽世界' })
    expect(LAYUI.msg).toHaveBeenCalledWith(FOUND_MESSAGE, { time: 1500 })
  })

  it('「搜索」按钮接成同一个动作（对标那颗是死按钮，见实现文件头偏差表）', async () => {
    await mountPage()
    await clickAdd()
    const box = document.querySelector<HTMLElement>(`.layui-layer-content > .${PICKER_CLASS}`)!
    const select = box.querySelector<HTMLSelectElement>('select[lay-filter="seachGame"]')!
    select.value = '魔兽世界'
    expect(LAYUI.formHandlers.get('submit(seachBtn)')!({})).toBe(false)
    // 蓝本里四项都只是换字色，不筛选：597 项一个不少。
    expect(box.querySelectorAll('input[name="games"]').length).toBe(597)
  })

  it('行内排序改动只发 `sort_order`（对标 `UpdateGameOrderNum`）', async () => {
    listItems = [{ id: 3, name: '原神', sort_order: 0 }]
    await mountPage()
    LAYUI.tableHandlers.get('edit(tableList)')!({ field: 'orderNum', value: '12', data: { id: 3 } })
    await flush()
    const patch = calls.find((c) => c.method === 'PATCH')!
    // 我们走的是自家 REST（`/api/business/games/3/`），不是对标那个端点。
    expect(patch.url).toContain('/api/business/games/3/')
    expect(patch.body).toEqual({ sort_order: 12 })
  })
})

describe('软删除：删除警告 + 已删除/恢复（业务方 2026-09-22 裁定，D-0085）', () => {
  let root: HTMLElement
  const calls: FetchCall[] = []
  let listItems: unknown[] = []
  let deletedItems: unknown[] = []

  const ROW = {
    id: 2,
    name: '绝区零',
    sort_order: 1,
    is_active: true,
    is_default: false,
    is_mobile: false,
    is_companion: false,
    disable_sms: false,
    created_at: '',
    deleted_at: null,
    order_count: 3,
    category_count: 2,
    project_count: 5,
  }

  function row(over: Record<string, unknown> = {}): Record<string, unknown> {
    return { ...ROW, ...over }
  }

  beforeEach(() => {
    vi.clearAllMocks()
    LAYUI.tableHandlers.clear()
    LAYUI.formHandlers.clear()
    LAYUI.tableRenders.length = 0
    LAYUI.reloads.length = 0
    LAYUI.opens.length = 0
    LAYUI.alerts.length = 0
    LAYUI.confirms.length = 0
    for (const k of Object.keys(LAYUI.cache)) delete LAYUI.cache[k]
    calls.length = 0
    listItems = [row()]
    deletedItems = []
    document.body.innerHTML = ''
    document.cookie = 'csrftoken=test-token'
    root = document.createElement('div')
    root.id = 'app'
    document.body.appendChild(root)
    vi.stubGlobal('fetch', async (input: unknown, init?: { method?: string; body?: string }) => {
      const url = String(input)
      const method = (init?.method ?? 'get').toUpperCase()
      calls.push({ url, method, body: init?.body ? JSON.parse(init.body) : undefined })
      if (url.includes('/replica/')) {
        const key = url.split('/').pop()!.replace('.html', '')
        return {
          ok: true,
          status: 200,
          text: async () => fs.readFileSync(path.join(REPLICA_DIR, `${key}.html`), 'utf8'),
          json: async () => ({}),
        }
      }
      if (url.includes('/api/business/games/')) {
        const items = url.includes('deleted=1') ? deletedItems : listItems
        const data =
          method === 'GET' ? { items, total: items.length, page: 1, page_size: 200 } : { id: 9 }
        return { ok: true, status: 200, json: async () => ({ code: 0, msg: '', data }) }
      }
      throw new Error('测试里没接这个 URL：' + url)
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.clearAllMocks()
  })

  async function mountPage(): Promise<void> {
    await page.mount(root, {} as never)
    await flush()
    await flush()
  }

  it('`deleteWarning`：把「下面有什么 / 删了会怎样 / 去哪恢复」三件事说全', () => {
    const text = deleteWarning(ROW as never)
    expect(text).toContain('确定将游戏删除吗？')
    expect(text).toContain('2 个项目分类')
    expect(text).toContain('5 个常用项目')
    expect(text).toContain('3 笔订单')
    expect(text).toContain('历史订单不受影响')
    expect(text).toContain('「已删除」里恢复')
  })

  it('`deleteWarning`：什么都没有的游戏不带那句引用清点（不是恒真文案）', () => {
    const text = deleteWarning(row({ order_count: 0, category_count: 0, project_count: 0 }) as never)
    expect(text).toContain('确定将游戏删除吗？')
    expect(text).not.toContain('该游戏下面有')
  })

  it('点「删除游戏」：先弹警告（带引用计数），确认后发 DELETE', async () => {
    await mountPage()
    calls.length = 0
    LAYUI.tableHandlers.get('tool(tableList)')!({ event: 'submitDel', data: row() })
    await flush()
    await flush()
    expect(LAYUI.confirms[LAYUI.confirms.length - 1]).toContain('2 个项目分类')
    const del = calls.find((c) => c.method === 'DELETE')!
    expect(del.url).toContain('/api/business/games/2/')
  })

  it('「已删除」按钮：列出已删除的游戏，点「恢复」发 `PATCH {deleted_at: null}` 并刷新', async () => {
    deletedItems = [row({ id: 77, name: '删掉的游戏', deleted_at: '2026-09-22T09:00:00+08:00' })]
    listItems = []
    await mountPage()
    LAYUI.tableHandlers.get('toolbar(tableList)')!({ event: 'deletedBtn' })
    await flush()
    await flush()
    const opened = LAYUI.opens[LAYUI.opens.length - 1]!
    expect(String(opened.content)).toContain('删掉的游戏')
    expect(String(opened.content)).toContain('data-restore="77"')
    expect(String(opened.content)).toContain('2026-09-22 09:00')
    document.querySelector<HTMLAnchorElement>('a[data-restore]')!.click()
    await flush()
    await flush()
    const patch = calls.find((c) => c.method === 'PATCH')!
    expect(patch.url).toContain('/api/business/games/77/')
    expect(patch.body).toEqual({ deleted_at: null })
    expect(LAYUI.msg).toHaveBeenCalledWith('已恢复', { time: 1500 })
  })
})

/* ─────────────── 游戏图标：真交互（§5-G 三条陷阱照做） ─────────────── */

type LogoFetchCall = {
  url: string
  method: string
  body: unknown
  headers: Record<string, string>
  credentials?: string
}

describe('「换图标」：点上传 → 真发三个请求 → 行内图标变了', () => {
  const UPLOAD_URL = '/api/media/blob/games/1/logo.png'
  const TICKET_KEY = 'games/1/logo.png'
  const FILE = { name: 'logo.png', type: 'image/png', size: 12 } as unknown as File

  let root: HTMLElement
  let listItems: Array<Record<string, unknown>> = []
  let claimFails = false
  const calls: LogoFetchCall[] = []

  function json(body: unknown): {
    ok: boolean
    status: number
    json: () => Promise<unknown>
    text: () => Promise<string>
  } {
    return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) }
  }

  beforeEach(() => {
    vi.clearAllMocks()
    LAYUI.tableHandlers.clear()
    LAYUI.formHandlers.clear()
    LAYUI.tableRenders.length = 0
    LAYUI.reloads.length = 0
    LAYUI.opens.length = 0
    LAYUI.alerts.length = 0
    LAYUI.confirms.length = 0
    for (const k of Object.keys(LAYUI.cache)) delete LAYUI.cache[k]
    calls.length = 0
    listItems = [{ id: 1, name: '原神', sort_order: 0, logo_url: '' }]
    claimFails = false
    document.body.innerHTML = ''
    // 同源直传要带 CSRF（`directUpload` 的判据）；这里把 cookie 挂上。
    document.cookie = 'csrftoken=test-token'
    root = document.createElement('div')
    root.id = 'app'
    document.body.appendChild(root)

    vi.stubGlobal(
      'fetch',
      async (input: unknown, init: LogoFetchCall = { url: '', method: 'get', body: undefined, headers: {} }) => {
        const url = String(input)
        const method = (init.method ?? 'get').toUpperCase()
        calls.push({
          url,
          method,
          // `apiRequest` 的 JSON 信封在 fetch 里是字符串；直传那一跳的 body 是 File 本身，
          // 必须**原样**留着才能断言「不是 multipart」。
          body:
            typeof init.body === 'string' && method !== 'PUT'
              ? JSON.parse(init.body as string)
              : init.body,
          headers: init.headers ?? {},
          credentials: init.credentials,
        })
        if (url.includes('/replica/')) {
          const key = url.split('/').pop()!.replace('.html', '')
          return {
            ...json({}),
            text: async () =>
              fs.readFileSync(path.join(REPLICA_DIR, `${key}.html`), 'utf8'),
          }
        }
        // ① 取票（先判：它最具体）。
        if (url.endsWith('/logo/ticket')) {
          return json({
            code: 0,
            msg: '',
            data: { key: TICKET_KEY, upload_url: UPLOAD_URL, expires_in: 300 },
          })
        }
        // ② 直传：body 是**文件本身**（不是 JSON、不是 FormData）。
        if (method === 'PUT') return json({})
        // ③ 认领（`/games/<id>/logo`，不带尾斜杠）。
        if (url.endsWith('/logo')) {
          if (claimFails) return json({ code: 4001, msg: '图标格式不支持', data: null })
          listItems = listItems.map((row) =>
            row.id === 1 ? { ...row, logo_url: UPLOAD_URL } : row,
          )
          return json({ code: 0, msg: '', data: { logo_url: UPLOAD_URL } })
        }
        if (url.includes('/api/business/games/')) {
          return json({
            code: 0,
            msg: '',
            data: { items: listItems, total: listItems.length, page: 1, page_size: 200 },
          })
        }
        throw new Error('测试里没接这个 URL：' + url)
      },
    )
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.clearAllMocks()
  })

  /**
   * 真交互的「点上传」那一下：
   *  - 陷阱①：类名与选择器是两个常量，选择器必须能用（`querySelector` 找得到 input）；
   *  - 陷阱③：`bootstrapIsland` 先跑（挂样式），`initIslandElements` 在塞完标记之后才跑。
   */
  async function mountAndOpenPicker(): Promise<{
    input: HTMLInputElement
    cell: HTMLElement
  }> {
    // 陷阱③：`initIslandElements` 必须在**塞完标记之后**跑。这里在它被调用的那一刻
    // 抓一份 `root.innerHTML`，据此判断「先有标记、再初始化」。
    const init = vi.mocked(initIslandElements)
    let markupAtInit = ''
    init.mockImplementationOnce(() => {
      markupAtInit = root.innerHTML
    })

    await page.mount(root, {} as never)
    await flush()

    expect(init.mock.calls.length, 'initIslandElements 没被调用').toBe(1)
    expect(markupAtInit, '先跑了 initIslandElements 才塞标记（§5-G 陷阱③）').toContain(
      'tableList',
    )
    // `bootstrapIsland` 的结果在模块里缓存（同一页面第二次挂载不再加载 layui），
    // ⇒ 只有本次真的加载过时才比顺序；顺序本身是 `mountConfigLike` 的既有契约。
    const bootstrap = vi.mocked(bootstrapIsland)
    if (bootstrap.mock.calls.length > 0) {
      expect(
        bootstrap.mock.invocationCallOrder[0]!,
        'initIslandElements 抢在 bootstrapIsland 之前跑了（§5-G 陷阱③）',
      ).toBeLessThan(init.mock.invocationCallOrder[0]!)
    }
    expect(root.querySelector('#tableList')).not.toBeNull()

    // layui 真渲染时这一格就是 `logoCellHtml` 的输出；测试里把同一份标记塞进 DOM 再点它。
    const cell = document.createElement('div')
    cell.innerHTML = logoCellHtml(listItems[0] as never)
    root.appendChild(cell)
    expect(cell.querySelector('img'), '初始状态不该有图（该行 logo_url 是空串）').toBeNull()
    expect(cell.textContent).toContain('原')

    cell.querySelector<HTMLAnchorElement>('a[data-act="logo"]')!.click()
    const input = document.querySelector<HTMLInputElement>(LOGO_INPUT_SELECTOR)
    expect(input, '点了「换图标」但没有文件框（input 没插进文档）').not.toBeNull()
    expect(LOGO_INPUT_SELECTOR).toBe(`.${LOGO_INPUT_CLASS}`)
    expect(input!.accept).toBe('image/jpeg,image/png')
    return { input: input!, cell }
  }

  function pickFile(input: HTMLInputElement): void {
    Object.defineProperty(input, 'files', { value: [FILE], configurable: true })
    input.dispatchEvent(new Event('change'))
  }

  it('三个请求的形状：票 → 原始字节 PUT → 认领，然后该行从首字变成 <img>', async () => {
    const { input, cell } = await mountAndOpenPicker()
    pickFile(input)

    await vi.waitFor(() => {
      expect(calls.filter((c) => c.method === 'POST').length).toBe(2)
      expect(calls.filter((c) => c.method === 'PUT').length).toBe(1)
    })
    await flush()

    // ① 取票：POST + **只带 `{ext}`**（后端 `GameLogoTicketSerializer` 的字段名；
    //    ⛔ 不带文件字节、⛔ 不是 `content_type`/`size` 那套）。
    const ticket = calls.find((c) => c.url.endsWith('/logo/ticket'))!
    expect(ticket.method).toBe('POST')
    expect(ticket.url).toBe('/api/business/games/1/logo/ticket')
    expect(ticket.body).toEqual({ ext: 'png' })

    // ② 直传：PUT 到签发的 upload_url，body 就是**同一个 File 对象**（⛔ 不是 multipart）。
    const put = calls.find((c) => c.method === 'PUT')!
    expect(put.url).toBe(UPLOAD_URL)
    expect(put.body).toBe(FILE)
    expect(put.body instanceof FormData).toBe(false)
    expect(put.headers['Content-Type']).toBe('image/png')
    // 同源要带 CSRF、credentials=include（`directUpload` 的承重分支）。
    expect(put.headers['X-CSRFToken']).toBe('test-token')
    expect(put.credentials).toBe('include')

    // ③ 认领：POST `{key}`。
    const claim = calls.filter((c) => c.method === 'POST')[1]!
    expect(claim.url).toBe('/api/business/games/1/logo')
    expect(claim.body).toEqual({ key: TICKET_KEY })
    expect(claim.headers['Content-Type']).toBe('application/json')

    // 传完重取列表（第二次 GET），该行带着服务端的 `logo_url`。
    await vi.waitFor(() => {
      const gets = calls.filter(
        (c) => c.method === 'GET' && c.url.includes('/api/business/games/'),
      )
      expect(gets.length).toBe(2)
    })
    const rows = LAYUI.reloads[LAYUI.reloads.length - 1]!.data as Array<Record<string, unknown>>
    expect(rows[0]!.logo_url).toBe(UPLOAD_URL)
    expect(LAYUI.msg).toHaveBeenCalledWith('图标已更新', { time: 1500 })

    // 行内图标真的变了：同一格重新渲染成 `<img>`，首字占位消失。
    cell.innerHTML = logoCellHtml(rows[0] as never)
    const img = cell.querySelector<HTMLImageElement>('img')
    expect(img, '传完这一格还是首字占位（没有渲染 <img>）').not.toBeNull()
    expect(img!.getAttribute('src')).toBe(UPLOAD_URL)
    expect(cell.textContent).toContain('换图标')
  })

  it('认领失败 ⇒ 原样显示后端 msg，并重取列表（不留「换掉了」的假象）', async () => {
    claimFails = true
    const { input } = await mountAndOpenPicker()
    pickFile(input)

    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('图标格式不支持', { time: 1500 })
    })
    await vi.waitFor(() => {
      const gets = calls.filter(
        (c) => c.method === 'GET' && c.url.includes('/api/business/games/'),
      )
      expect(gets.length).toBe(2)
    })
    // 失败后列表仍是空 logo（服务端没变），行内回落首字。
    const rows = LAYUI.reloads[LAYUI.reloads.length - 1]!.data as Array<Record<string, unknown>>
    expect(rows[0]!.logo_url).toBe('')
    expect(logoCellHtml(rows[0] as never)).not.toContain('<img')
  })
})
