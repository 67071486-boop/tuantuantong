// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { VIEW_PAGE_TITLE, buildViewCols, levelLabel, viewTableHtml } from '../system-roleauthority-view'

const shape = (cols: Record<string, unknown>[]) =>
  cols.map((c) => ({
    field: c.field,
    title: c.title,
    width: c.width ?? null,
    align: c.align ?? null,
    fixed: c.fixed ?? null,
    unresize: c.unresize === true,
  }))

describe('权限清单弹层的表格声明（对标 js_RoleAuthorityView.js:19-52）', () => {
  it('5 列、顺序/宽度/对齐/左固定逐条对齐对标', () => {
    expect(shape(buildViewCols())).toEqual([
      { field: 'name', title: '拥有权限名称', width: 150, align: 'left', fixed: 'left', unresize: true },
      { field: 'level', title: '级别', width: 70, align: 'left', fixed: null, unresize: true },
      { field: 'parent_name', title: '所属栏目', width: 150, align: 'left', fixed: null, unresize: true },
      { field: 'note', title: '权限说明', width: null, align: 'left', fixed: null, unresize: true },
      { field: 'order_by', title: '排序', width: 80, align: 'center', fixed: null, unresize: true },
    ])
  })

  it('级别列把 0 映成「一级」、其余映成「二级」（对标三元表达式，不是数字）', () => {
    expect(levelLabel(0)).toBe('一级')
    expect(levelLabel(1)).toBe('二级')
    expect(levelLabel(7)).toBe('二级')
  })

  it('对标 `<title>` 逐字：角色职位权限清单', () => {
    expect(VIEW_PAGE_TITLE).toBe('角色职位权限清单')
  })

  it('列 templet 里来自 row 的值一律转义（第 3 列 parent_name 的 `"` 不许裸拼进属性）', () => {
    const cols = buildViewCols()
    const templet = cols[2]!.templet as (row: Record<string, unknown>) => string
    const html = templet({
      name: 'x',
      level: 1,
      parent_name: '" onmouseover="alert(1)',
      note: '',
      order_by: 1,
    })
    expect(html).not.toContain('" onmouseover=')
    expect(html).toContain('&quot;')
  })

  it('标记里有 `#tableList`、`#toolbarDemo`（**1 颗**刷新键）与 roleId 隐藏域', () => {
    const d = new DOMParser().parseFromString(viewTableHtml(3), 'text/html')
    expect(d.querySelector('table#tableList')).not.toBeNull()
    expect(d.querySelector('table#tableList')?.getAttribute('lay-filter')).toBe('tableList')
    // ⚠️ `#toolbarDemo` 是 `<script type="text/html">`：按 HTML 规范它的内容是**纯文本**，
    // 直接在本文档里 `querySelectorAll('#toolbarDemo button')` 恒得 0（实测 `expected +0 to be 1`）。
    // 所以把模板文本二次解析成文档再取按钮——仓库惯用写法，见
    // `game-gameserver.spec.ts:147-149` 与 `game-projectsetting.spec.ts:121-122`。
    const toolbar = new DOMParser().parseFromString(
      d.querySelector('#toolbarDemo')!.innerHTML,
      'text/html',
    )
    const buttons = Array.from(toolbar.querySelectorAll('button'))
    expect(buttons.length).toBe(1)
    expect(buttons[0]!.textContent).toContain('刷新列表')
    expect(buttons[0]!.className).toContain('layui-btn-xs')
    expect(buttons[0]!.getAttribute('title')).toBe('刷新当前数据')
    expect(d.querySelector<HTMLInputElement>('input#roleId')!.value).toBe('3')
  })
})
