// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 【订单项目分类 + 常用代练项目】**合并页**的判据。
 *
 * 两类：
 * 1. 纯函数 / 标记：合并后的两表、两个弹窗字段、校验文案、删除警告；
 * 2. 挂载：一次装载把「游戏 + 我 + 分类 + 项目」四发都取到，右侧默认显示第一条分类的项目；
 *    切游戏会清空选中并重取；分类 / 项目按钮按 RBAC 节点分别显隐。
 *
 * ⚠️ 这一页**不是对标复刻**（业务方要求合并两页），所以不再钉对标 `cols` / 证据件——
 * 那两页的证据件与 `replicaRoutes.ts` 条目仍然保留（那张表是对标 89 条菜单的转录）。
 */

const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  const tableHandlers = new Map<string, (obj: unknown) => unknown>()
  const formHandlers = new Map<string, (obj: unknown) => unknown>()
  const opens: Array<Record<string, unknown>> = []
  const reloads: Array<{ id: string; data: unknown }> = []
  const renders: Array<Record<string, unknown>> = []
  const cache: Record<string, unknown[]> = {}
  let opened: { host: HTMLElement; opts: Record<string, unknown> } | null = null
  return {
    msg,
    tableHandlers,
    formHandlers,
    opens,
    reloads,
    renders,
    cache,
    form: {
      render: vi.fn(),
      on: vi.fn((event: string, cb: (obj: unknown) => unknown) => {
        formHandlers.set(event, cb)
      }),
    },
    table: {
      render: vi.fn((opts: Record<string, unknown>) => {
        renders.push(opts)
      }),
      reloadData: vi.fn((id: string, opts: { data: unknown }) => {
        reloads.push({ id, data: opts.data })
        cache[id] = (opts.data ?? []) as unknown[]
      }),
      on: vi.fn((event: string, cb: (obj: unknown) => unknown) => {
        tableHandlers.set(event, cb)
      }),
      cache,
    },
    layer: {
      open: vi.fn((opts: Record<string, unknown>) => {
        opens.push(opts)
        const host = document.createElement('div')
        host.className = 'layui-layer'
        const content = document.createElement('div')
        content.className = 'layui-layer-content'
        content.innerHTML = String(opts.content ?? '')
        host.appendChild(content)
        document.body.appendChild(host)
        opened = { host, opts }
        return opens.length
      }),
      close: vi.fn(() => {
        if (!opened) return
        ;(opened.opts.end as (() => void) | undefined)?.()
        opened.host.remove()
        opened = null
      }),
      msg,
      confirm: vi.fn((text: string, _opts: unknown, yes: (i: number) => void) => {
        LAYUI.confirms.push(text)
        yes(1)
      }),
      closeAll: vi.fn(),
    },
    confirms: [] as string[],
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => ({
    form: LAYUI.form,
    table: LAYUI.table,
    layer: LAYUI.layer,
  })),
  initIslandElements: vi.fn(),
}))

vi.mock('@/api/auth', () => ({ fetchMe: vi.fn() }))
vi.mock('@/api/config', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/config')>()),
  fetchAllGames: vi.fn(),
}))
vi.mock('@/layui-island/config-replica', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui-island/config-replica')>()
  return {
    ...actual,
    listAllConfigPage: vi.fn(),
    createConfigRow: vi.fn(),
    patchConfigRow: vi.fn(),
    deleteConfigRow: vi.fn(),
    openDeletedLayer: vi.fn(),
  }
})

import { fetchMe } from '@/api/auth'
import { fetchAllGames } from '@/api/config'
import { listAllConfigPage } from '@/layui-island/config-replica'
import page, {
  CATEGORY_FORM_ID,
  PROJECT_FORM_ID,
  buildCategoryCols,
  buildProjectCols,
  categoryDeleteWarning,
  categoryDialogHtml,
  pageHtml,
  projectDeleteWarning,
  projectDialogHtml,
  validateCategoryForm,
  validateProjectForm,
} from '../game-projectsetting'

const mockedGames = vi.mocked(fetchAllGames)
const mockedMe = vi.mocked(fetchMe)
const mockedList = vi.mocked(listAllConfigPage)

const GAMES = [
  { id: 1, name: '原神', sort_order: 0, is_active: true, created_at: '' },
  { id: 2, name: '鸣潮', sort_order: 0, is_active: true, created_at: '' },
]

const CATEGORIES = [
  {
    id: 11,
    game: 1,
    name: '深渊',
    sort_order: 5,
    is_active: true,
    assigned_platforms: '8,10',
    title_prefix: '前',
    title_suffix: '后',
    created_at: '',
    deleted_at: null,
    order_count: 2,
    project_count: 3,
  },
  {
    id: 12,
    game: 1,
    name: '日常',
    sort_order: 1,
    is_active: true,
    assigned_platforms: '',
    title_prefix: '',
    title_suffix: '',
    created_at: '',
    deleted_at: null,
    order_count: 0,
    project_count: 0,
  },
]

const PROJECTS = [
  {
    id: 21,
    category: 11,
    name: '满星',
    sort_order: 2,
    is_active: true,
    created_at: '',
    deleted_at: null,
    order_count: 4,
    base_price: null,
  },
]

function flush(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

beforeEach(() => {
  LAYUI.msg.mockReset()
  LAYUI.opens.length = 0
  LAYUI.reloads.length = 0
  LAYUI.renders.length = 0
  LAYUI.confirms.length = 0
  LAYUI.tableHandlers.clear()
  LAYUI.formHandlers.clear()
  document.body.innerHTML = ''
  mockedGames.mockResolvedValue(GAMES as never)
  mockedMe.mockResolvedValue({
    id: 1,
    username: 'admin',
    display_name: '管理员',
    identity_plane: 'tenant',
    is_tenant_admin: true,
    roles: [],
  } as never)
  mockedList.mockImplementation((async (resource: string) => {
    if (resource === 'categories') {
      return { items: CATEGORIES, total: CATEGORIES.length, truncated: false }
    }
    return { items: PROJECTS, total: PROJECTS.length, truncated: false }
  }) as never)
})

describe('合并页的标记与列', () => {
  it('页面上有游戏下拉、分类表、项目表，没有旧的「单一表格」入口', () => {
    const html = pageHtml()
    expect(html).toContain('select id="gameName"')
    expect(html).toContain('id="categoryTable"')
    expect(html).toContain('id="projectTable"')
    expect(html).toContain('data-act="add-category"')
    expect(html).toContain('data-act="add-project"')
    expect(html).toContain('data-act="deleted-categories"')
    expect(html).toContain('data-act="deleted-projects"')
  })

  it('分类 5 列 / 项目 4 列；有权限时才有「操作」两颗按钮，无权限时编辑链接不带 data-act', () => {
    const cat = buildCategoryCols(true)
    expect(cat.map((c) => c.title)).toEqual(['', '分类名称', '排序', '项目', '操作'])
    const catReadonly = buildCategoryCols(false)
    const nameCol = catReadonly[1] as unknown as { templet: (d: typeof CATEGORIES[0]) => string }
    expect(nameCol.templet(CATEGORIES[0]!)).not.toContain('data-act="edit-category"')
    const catName = cat[1] as unknown as { templet: (d: typeof CATEGORIES[0]) => string }
    expect(catName.templet(CATEGORIES[0]!)).toContain('data-act="edit-category"')
    const catAction = cat[4] as unknown as { templet: (d: { id: number }) => string }
    expect(catAction.templet({ id: 11 })).toContain('data-act="del-category"')

    const proj = buildProjectCols(true)
    expect(proj.map((c) => c.title)).toEqual(['', '项目名称', '排序', '操作'])
    const projAction = proj[3] as unknown as { templet: (d: { id: number }) => string }
    expect(projAction.templet({ id: 21 })).toContain('data-act="del-project"')
  })

  it('分类 / 项目校验文案逐字沿用旧页', () => {
    expect(validateCategoryForm({ name: '', orderRaw: '0' })).toBe('分类名称不能为空')
    expect(validateCategoryForm({ name: 'x', orderRaw: '' })).toBe('排序值不能为空，哪怕写个数字0')
    expect(validateCategoryForm({ name: 'x', orderRaw: '1a' })).toBe('排序值只能是数字')
    expect(validateCategoryForm({ name: 'x', orderRaw: '0' })).toBeNull()
    expect(validateProjectForm({ name: '', orderRaw: '0' })).toBe('项目名称不能为空')
    expect(validateProjectForm({ name: 'x', orderRaw: '0' })).toBeNull()
  })

  it('删除警告说清下级项目数 / 订单引用数，并对名称转义', () => {
    expect(categoryDeleteWarning(CATEGORIES[0]!)).toContain('还有 3 个常用项目')
    expect(projectDeleteWarning(PROJECTS[0]!)).toContain('被 4 笔订单引用')
    expect(categoryDeleteWarning({ ...CATEGORIES[0]!, name: '<b>' })).toContain('&lt;b&gt;')
  })

  it('分类弹窗有 5 个字段 + 13 个平台复选框；编辑时值回填', () => {
    const html = categoryDialogHtml(CATEGORIES[0]!)
    expect(html).toContain(`id="${CATEGORY_FORM_ID}"`)
    for (const name of ['name', 'sort_order', 'title_prefix', 'title_suffix']) {
      expect(html).toContain(`name="${name}"`)
    }
    expect(html).toContain('value="深渊"')
    expect(html).toContain('value="前"')
    expect(html).toContain('value="后"')
    // 13 个平台复选框，8/10 被勾上
    expect((html.match(/name="platform"/g) ?? []).length).toBe(13)
    expect(html).toContain('value="8" checked')
    expect(html).toContain('value="10" checked')
    expect(html).toContain('value="30"')
  })

  it('项目弹窗默认选中「当前分类」，编辑时选中项目原分类', () => {
    const add = projectDialogHtml(null, CATEGORIES, 12)
    expect(add).toContain(`id="${PROJECT_FORM_ID}"`)
    expect(add).toContain('<option value="12" selected>日常（0 个项目）</option>')
    const edit = projectDialogHtml(PROJECTS[0]!, CATEGORIES, 12)
    expect(edit).toContain('<option value="11" selected>深渊（3 个项目）</option>')
  })
})

describe('挂载：一次装载取全，切分类即换右侧项目', () => {
  it('分类与默认分类的项目都画上，右侧标题显示当前分类', async () => {
    const root = document.createElement('div')
    document.body.appendChild(root)
    const unmount = await page.mount(root, { onNavigate: () => undefined })
    await flush()

    expect(mockedGames).toHaveBeenCalled()
    expect(mockedMe).toHaveBeenCalled()
    // 先取分类（按游戏），再取该分类的项目
    expect(mockedList.mock.calls.map((c) => c[0])).toEqual(
      expect.arrayContaining(['categories', 'projects']),
    )
    const catReload = LAYUI.reloads.find((r) => r.id === 'categoryTable')
    expect(catReload?.data).toHaveLength(2)
    // 默认选中第一条（id 11）⇒ 右侧取的是 projects(category=11)
    const projectCall = mockedList.mock.calls.find((c) => c[0] === 'projects')
    expect(projectCall?.[1]).toEqual({ category: 11 })
    expect(root.querySelector('#projectScope')!.textContent).toBe('深渊')
    expect(LAYUI.reloads.find((r) => r.id === 'projectTable')?.data).toHaveLength(1)
    unmount()
  })

  it('点分类行会重取那一类的项目（不是只在浏览器里过滤）', async () => {
    const root = document.createElement('div')
    document.body.appendChild(root)
    const unmount = await page.mount(root, { onNavigate: () => undefined })
    await flush()
    const rowHandler = LAYUI.tableHandlers.get('row(categoryTable)')
    expect(rowHandler).toBeDefined()
    mockedList.mockClear()
    rowHandler!({ data: CATEGORIES[1] })
    await flush()
    expect(mockedList.mock.calls[0]?.[0]).toBe('projects')
    expect(mockedList.mock.calls[0]?.[1]).toEqual({ category: 12 })
    unmount()
  })

  it('编辑分类的弹层标题会转义分类名（layer 标题是 HTML 串，不能直接插用户输入）', async () => {
    mockedList.mockImplementation((async (resource: string) => {
      if (resource === 'categories') {
        const evil = { ...CATEGORIES[0]!, id: 99, name: '<b>evil</b>' }
        return { items: [evil], total: 1, truncated: false }
      }
      return { items: [], total: 0, truncated: false }
    }) as never)
    const root = document.createElement('div')
    document.body.appendChild(root)
    const unmount = await page.mount(root, { onNavigate: () => undefined })
    await flush()
    const btn = document.createElement('a')
    btn.dataset.act = 'edit-category'
    btn.dataset.id = '99'
    root.appendChild(btn)
    btn.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()
    const title = String(LAYUI.opens[LAYUI.opens.length - 1]!.title)
    expect(title).toContain('&lt;b&gt;evil&lt;/b&gt;')
    expect(title).not.toContain('<b>')
    unmount()
  })

  it('没有分类权限时「新增分类」按钮隐藏，项目按钮还在', async () => {
    mockedMe.mockResolvedValue({
      id: 2,
      username: 'ops',
      display_name: '运营',
      identity_plane: 'tenant',
      is_tenant_admin: false,
      menu_permissions: ['game.common_projects'],
      roles: [],
    } as never)
    const root = document.createElement('div')
    document.body.appendChild(root)
    const unmount = await page.mount(root, { onNavigate: () => undefined })
    await flush()
    const addCategory = root.querySelector<HTMLElement>('[data-act="add-category"]')!
    const addProject = root.querySelector<HTMLElement>('[data-act="add-project"]')!
    expect(addCategory.style.display).toBe('none')
    expect(addProject.style.display).not.toBe('none')
    // 「已删除分类」的恢复动作也要写节点 ⇒ 与新增按钮一起隐藏；项目侧保留。
    expect(
      root.querySelector<HTMLElement>('[data-act="deleted-categories"]')!.style.display,
    ).toBe('none')
    expect(
      root.querySelector<HTMLElement>('[data-act="deleted-projects"]')!.style.display,
    ).not.toBe('none')
    unmount()
  })
})
