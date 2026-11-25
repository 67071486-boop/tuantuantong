// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'

/**
 * 「游戏维护三页」island 用的 **HTTP / 助手层**契约（`layui-island/config-replica.ts`）。
 *
 * 为什么这一层值得单独钉住：三页的写操作都不再走 `api/http.ts` 的 axios（iframe 与 SPA 同源，
 * 直接 `fetch` 更直白），于是「方法 / URL / CSRF 头 / 载荷 / 信封解包 / 错误映射」这一整层
 * **没有任何组件测试覆盖**——而它是三页唯一真正碰后端的代码。
 *
 * 本文件替代了 `src/composables/__tests__/useConfigResource.spec.ts` 与
 * `src/views/gameConfig/__tests__/replicaPages.spec.ts` 里与「请求形状」有关的断言
 * （逐条映射见三个页面 spec 的文件头）。页面自己的列定义与证据件在各自的 spec 里。
 */

const csrf = vi.hoisted(() => ({ token: 'tok-123' as string | null }))

vi.mock('@/api/csrf-cookie', () => ({
  readCsrfCookie: () => csrf.token,
}))
vi.mock('@/api/auth', () => ({
  fetchCsrf: vi.fn(async () => {
    csrf.token = 'tok-from-bootstrap'
    return null
  }),
}))

import { fetchCsrf } from '@/api/auth'
import {
  apiRequest,
  buildDeletedPanelHtml,
  createConfigRow,
  deleteConfigRow,
  fillGameOptions,
  findPanel,
  formatDeletedAt,
  listAllConfigPage,
  listConfigPage,
  openPanelLayer,
  patchConfigRow,
  restoreConfigRow,
  selectedGameId,
  selectedGameName,
  withAliases,
} from '../config-replica'

const fetchMock = vi.fn()
vi.stubGlobal('fetch', fetchMock)

type Call = { method: string; url: string; headers: Record<string, string>; body: unknown }
const calls: Call[] = []

/** 每条请求都回一个 HTTP 200 的信封（业务结果只读 code）。 */
function ok<T>(data: T, code = 0, msg = ''): void {
  fetchMock.mockImplementation((url: string, init: RequestInit = {}) => {
    calls.push({
      method: (init.method ?? 'GET').toUpperCase(),
      url,
      headers: (init.headers ?? {}) as Record<string, string>,
      body: init.body === undefined ? undefined : JSON.parse(String(init.body)),
    })
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ code, msg, data }),
    })
  })
}

afterEach(() => {
  vi.clearAllMocks()
  calls.length = 0
  csrf.token = 'tok-123'
})

describe('listConfigPage：GET 的形状', () => {
  it('链顶资源（游戏）只带分页参数', async () => {
    ok({ items: [], total: 0, page: 1, page_size: 200 })
    await listConfigPage('games', { page: 1, page_size: 200 })
    expect(calls[0]!.method).toBe('GET')
    expect(calls[0]!.url).toBe('/api/business/games/?page=1&page_size=200')
  })

  it('带父级的资源把父级参数拼进 query（分类 `?game=`、项目 `?game=`）', async () => {
    ok({ items: [], total: 0, page: 1, page_size: 200 })
    await listConfigPage('categories', { game: 7, page: 1, page_size: 200 })
    await listConfigPage('projects', { game: 7, page: 1, page_size: 200 })
    expect(calls.map((c) => c.url)).toEqual([
      '/api/business/categories/?game=7&page=1&page_size=200',
      '/api/business/projects/?game=7&page=1&page_size=200',
    ])
  })

  it('GET 不带 CSRF 头（Django 只对非安全方法跑 CsrfViewMiddleware）', async () => {
    ok({ items: [], total: 0, page: 1, page_size: 200 })
    await listConfigPage('games', { page: 1, page_size: 200 })
    expect(calls[0]!.headers['X-CSRFToken']).toBeUndefined()
  })

  it('**整页**返回（要 total 才能报「仅显示前 N 条」），不是只解出 items', async () => {
    ok({ items: [{ id: 1 }], total: 250, page: 1, page_size: 200 })
    const page = await listConfigPage<{ id: number }>('games', { page: 1, page_size: 200 })
    expect(page.total).toBe(250)
    expect(page.items).toHaveLength(1)
  })
})

describe('写操作：方法 / URL / CSRF 头 / 载荷', () => {
  it('createConfigRow → POST 集合端点，带 `X-CSRFToken` 与 JSON 体', async () => {
    ok({ id: 9 })
    await createConfigRow('games', { name: '崩铁', is_mobile: false })
    expect(calls[0]!.method).toBe('POST')
    expect(calls[0]!.url).toBe('/api/business/games/')
    expect(calls[0]!.headers['X-CSRFToken']).toBe('tok-123')
    expect(calls[0]!.headers['Content-Type']).toBe('application/json')
    expect(calls[0]!.body).toEqual({ name: '崩铁', is_mobile: false })
  })

  it('patchConfigRow → PATCH 详情端点（**只发改动字段**，整对象回写会覆盖并发改的另一列）', async () => {
    ok({ id: 3 })
    await patchConfigRow('categories', 3, { sort_order: 9 })
    expect(calls[0]!.method).toBe('PATCH')
    expect(calls[0]!.url).toBe('/api/business/categories/3/')
    expect(calls[0]!.body).toEqual({ sort_order: 9 })
    expect(calls[0]!.headers['X-CSRFToken']).toBe('tok-123')
  })

  it('deleteConfigRow → DELETE 详情端点', async () => {
    ok(null)
    await deleteConfigRow('projects', 11)
    expect(calls[0]!.method).toBe('DELETE')
    expect(calls[0]!.url).toBe('/api/business/projects/11/')
    expect(calls[0]!.headers['X-CSRFToken']).toBe('tok-123')
  })

  it('cookie 里没有 token 时**先引导一次**（GET /api/auth/csrf），再带上新 token 发原请求', async () => {
    csrf.token = null
    ok({ id: 9 })
    await createConfigRow('games', { name: '崩铁' })
    expect(vi.mocked(fetchCsrf)).toHaveBeenCalledTimes(1)
    expect(calls[0]!.headers['X-CSRFToken']).toBe('tok-from-bootstrap')
  })
})

describe('错误映射（与原 `notifyError(e)` 分支一一对应）', () => {
  it('业务失败（code 1001）→ 抛 BizError，msg 就是后端那句', async () => {
    ok(null, 1001, '该记录已被其它数据引用，不能删除')
    await expect(deleteConfigRow('games', 1)).rejects.toMatchObject({
      name: 'BizError',
      code: 1001,
      message: '该记录已被其它数据引用，不能删除',
    })
  })

  it('401 → 「登录状态已失效，请重新登录」（原 `AuthExpiredError` 的提示）', async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 401, json: () => Promise.resolve(null) })
    await expect(apiRequest('/api/business/games/')).rejects.toThrow('登录状态已失效，请重新登录')
  })

  it('403 → 「没有权限执行该操作」（原 `HttpError` 的提示）', async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 403, json: () => Promise.resolve(null) })
    await expect(apiRequest('/api/business/games/')).rejects.toThrow('没有权限执行该操作')
  })

  it('响应不是信封（Django 未预期异常回 HTML）→ 按 HTTP 状态报错，不假装业务结果', async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 500,
      json: () => Promise.reject(new Error('not json')),
    })
    await expect(apiRequest('/api/business/games/')).rejects.toThrow('请求失败（HTTP 500）')
  })
})

describe('游戏下拉助手（对标是服务端内联 `<option>`，我们运行期灌）', () => {
  const form = { render: vi.fn() }

  function fixture(): HTMLElement {
    const root = document.createElement('div')
    root.innerHTML = '<select id="gameName" name="gameName" lay-filter="selectGame"></select>'
    return root
  }

  it('灌入真实游戏、选中第一个，并 `form.render("select")` 刷新 layui 的下拉壳', () => {
    const root = fixture()
    const select = fillGameOptions(root, form as never, [
      { id: 3, name: '原神' },
      { id: 4, name: '鸣潮' },
    ])
    expect(select!.options.length).toBe(2)
    expect(select!.value).toBe('3')
    expect(selectedGameId(select)).toBe(3)
    expect(selectedGameName(select)).toBe('原神')
    expect(form.render).toHaveBeenCalledWith('select')
  })

  it('没有游戏时选中项为 -1：`selectedGameId` 回 null（「未选就不发请求」的判据）', () => {
    const root = fixture()
    const select = fillGameOptions(root, form as never, [])
    expect(select!.selectedIndex).toBe(-1)
    expect(selectedGameId(select)).toBeNull()
    expect(selectedGameName(select)).toBe('')
  })

  it('`preserveId` 保住当前选中的游戏（刷新按钮不许把用户选的游戏换掉）', () => {
    const root = fixture()
    const select = fillGameOptions(
      root,
      form as never,
      [
        { id: 3, name: '原神' },
        { id: 4, name: '鸣潮' },
      ],
      4,
    )
    expect(selectedGameId(select)).toBe(4)
  })

  it('`preserveId` 指向的游戏已经没了 ⇒ 回落到第一项（不是留在空选上）', () => {
    const root = fixture()
    const select = fillGameOptions(root, form as never, [{ id: 3, name: '原神' }], 99)
    expect(selectedGameId(select)).toBe(3)
  })

  it('游戏名走转义（名字是用户输入，直接拼 innerHTML 就是存储型 XSS）', () => {
    const root = fixture()
    fillGameOptions(root, form as never, [{ id: 3, name: '<img src=x onerror=alert(1)>' }])
    const option = root.querySelector('option')!
    expect(option.getAttribute('value')).toBe('3')
    expect(option.textContent).toBe('<img src=x onerror=alert(1)>')
    expect(option.innerHTML).not.toContain('<img')
    expect(option.innerHTML).toContain('&lt;img')
  })
})

describe('withAliases：把 API 键补成对标 `cols[].field` 的别名', () => {
  it('不改原对象，返回带别名的副本', () => {
    const row = { id: 1, name: '代练', sort_order: 5 }
    const aliased = withAliases(row, { TypeName: '代练', OrderBy: 5 })
    expect(aliased.TypeName).toBe('代练')
    expect(aliased.OrderBy).toBe(5)
    expect(aliased.name).toBe('代练')
    expect(row).not.toHaveProperty('TypeName')
  })
})

/**
 * `findPanel` / `openPanelLayer` 的**泛化**（2026-09-21 订单来源线追加）。
 *
 * 这两条钉的是「只加一个可选参数、既有两页行为逐字不变」：默认值仍是 `.addeditDiv`，
 * 显式传 `'.change'` 时取到的是来源管理页那个容器。**第一条是回归守卫**——把默认值改成
 * `'.change'` 会让「游戏维护」两页的弹层取不到节点（`openPanelLayer` 静默回 `null`）。
 */
describe('弹层助手：`selector` 泛化后默认行为不变', () => {
  afterEach(() => {
    document.body.innerHTML = ''
  })

  function host(cls: string): { root: HTMLElement; panel: HTMLElement } {
    const root = document.createElement('div')
    root.innerHTML = `<form class="layui-form"><div class="${cls}"><input name="x" /></div></form>`
    // 挂进文档：`findPanel` 第一落点是**文档级**选择器（弹层把节点搬走后 `root` 里没有它）。
    document.body.appendChild(root)
    return { root, panel: root.querySelector<HTMLElement>(`.${cls}`)! }
  }

  it('默认（不传 selector）找的仍是 `.addeditDiv`，对 `.change` 视而不见', () => {
    const { root, panel } = host('change')
    expect(findPanel(root)).toBeNull()
    expect(findPanel(root, '.change')).toBe(panel)
  })

  it('节点被借进弹层时两个选择器都在 `.layui-layer-content` 里找得到', () => {
    const { root, panel } = host('change')
    const layerBody = document.createElement('div')
    layerBody.className = 'layui-layer-content'
    document.body.appendChild(layerBody)
    layerBody.appendChild(panel)
    expect(findPanel(root, '.change')).toBe(panel)
  })

  it('默认（不传 selector）对 `.addeditDiv` 生效：content 就是那个节点，关闭后放回 `<form>`', () => {
    const { root, panel } = host('addeditDiv')
    type OpenConfig = { content: { el: Element }; type: number; end: () => void }
    const open = vi.fn<(config: OpenConfig) => number>(() => 9)
    const layer = { open } as never
    const layui = { $: (el: Element) => ({ el }) } as never
    // 这一发**不传第五参**：默认值一旦被改成别的类名（如 `.change`），它会回 `null` ⇒ 红。
    expect(openPanelLayer(root, layui, layer, { title: 'x' })).toBe(9)
    const config = open.mock.calls[0]![0]
    expect(config.type).toBe(1)
    expect(config.content.el).toBe(panel)

    // 缺 `end` 回调会让第二次打不开（layui 关闭时把节点从文档里摘掉）——这里直接钉住它。
    const stolen = document.createElement('div')
    document.body.appendChild(stolen)
    stolen.appendChild(panel)
    config.end()
    expect(panel.parentElement).toBe(root.querySelector('form'))
  })

  it('显式传 `.change` 时取到的是来源管理页那个容器（`openPanelLayer` 的第五参）', () => {
    const { root, panel } = host('change')
    const open = vi.fn<(config: { content: { el: Element } }) => number>(() => 3)
    const layer = { open } as never
    const layui = { $: (el: Element) => ({ el }) } as never
    expect(openPanelLayer(root, layui, layer, {}, '.change')).toBe(3)
    expect(open.mock.calls[0]![0].content.el).toBe(panel)
  })

  it('显式选择器找不到节点时回 `null`（不抛），且不调 `layer.open`', () => {
    const { root } = host('change')
    const open = vi.fn<(config: Record<string, unknown>) => number>(() => 3)
    const layer = { open } as never
    const layui = { $: (el: Element) => ({ el }) } as never
    expect(openPanelLayer(root, layui, layer, {}, '.addeditDiv')).toBeNull()
    expect(open).not.toHaveBeenCalled()
  })
})

describe('软删除相关的助手（D-0085）', () => {
  afterEach(() => {
    calls.length = 0
    fetchMock.mockReset()
  })

  it('`listAllConfigPage`：超过一页时**自动翻页取全量**（业务方要的兜底）', async () => {
    // 第 1 页给满 200 条、total 说 250 ⇒ 必须再取第 2 页，且顺序是「前一页在前」。
    let page = 0
    fetchMock.mockImplementation((url: string) => {
      calls.push({ method: 'GET', url, headers: {}, body: undefined })
      page += 1
      const items =
        page === 1
          ? Array.from({ length: 200 }, (_, i) => ({ id: i + 1, name: `第${i + 1}条` }))
          : [{ id: 201, name: '第201条' }, { id: 202, name: '第202条' }]
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          code: 0,
          msg: '',
          data: { items, total: 202, page, page_size: 200 },
        }),
      })
    })

    const result = await listAllConfigPage<{ id: number; name: string }>('games')

    expect(result.items.length).toBe(202)
    expect(result.total).toBe(202)
    expect(result.truncated).toBe(false)
    expect(calls.length).toBe(2)
    expect(calls[1]!.url).toContain('page=2')
    // 顺序：第 1 页在前（拼错顺序会让「数字越大越靠前」的排序在跨页处断开）。
    expect(result.items[0]!.id).toBe(1)
    expect(result.items[201]!.id).toBe(202)
  })

  it('`listAllConfigPage`：只有一页时**只发一次请求**（不白翻）', async () => {
    ok({ items: [{ id: 1, name: '原神' }], total: 1, page: 1, page_size: 200 })
    const result = await listAllConfigPage<{ id: number }>('games')
    expect(result.items.length).toBe(1)
    expect(calls.length).toBe(1)
    expect(result.truncated).toBe(false)
  })

  it('`restoreConfigRow`：就是一次 `PATCH {deleted_at: null}`（后端没有为恢复新增端点）', async () => {
    ok({ id: 7 })
    await restoreConfigRow('games', 7)
    expect(calls[0]!.method).toBe('PATCH')
    expect(calls[0]!.url).toContain('/api/business/games/7/')
    expect(calls[0]!.body).toEqual({ deleted_at: null })
  })

  it('`formatDeletedAt`：ISO → `YYYY-MM-DD HH:mm`；取不到就原样回（不显示成 NaN）', () => {
    expect(formatDeletedAt('2026-09-22T18:30:12.345678+08:00')).toBe('2026-09-22 18:30')
    expect(formatDeletedAt(null)).toBe('')
    expect(formatDeletedAt('不是时间')).toBe('不是时间')
  })

  it('`buildDeletedPanelHtml`：空列表给一句人话（不是一张空白表）', () => {
    expect(buildDeletedPanelHtml([])).toContain('没有已删除的记录')
  })

  it('`buildDeletedPanelHtml`：每行一个「恢复」按钮，带 `data-restore=<id>`，名称走转义', () => {
    const html = buildDeletedPanelHtml([
      { id: 7, name: '<img src=x>', deleted_at: '2026-09-22T10:00:00+08:00' },
    ])
    expect(html).toContain('data-restore="7"')
    expect(html).toContain('2026-09-22 10:00')
    expect(html).toContain('恢复')
    expect(html).not.toContain('<img')
    expect(html).toContain('&lt;img')
  })
})
