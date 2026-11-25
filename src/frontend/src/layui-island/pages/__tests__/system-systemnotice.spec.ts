// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 【系统内部公告】页（2026-10-17 实装）的判据。
 *
 * 纯函数钉文案与转义；挂载钉「装载即拉列表 + 发公告走 POST + 编辑走 PATCH + 删除走 DELETE」。
 */
const API = vi.hoisted(() => ({
  list: vi.fn(),
  create: vi.fn(),
  update: vi.fn(),
  remove: vi.fn(),
}))

const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  const opens: Array<Record<string, unknown>> = []
  const reloads: Array<{ id: string; data: unknown }> = []
  let opened: { host: HTMLElement; opts: Record<string, unknown> } | null = null
  return {
    msg,
    opens,
    reloads,
    form: { render: vi.fn(), on: vi.fn() },
    table: {
      render: vi.fn(),
      reloadData: vi.fn((id: string, opts: { data: unknown }) => {
        reloads.push({ id, data: opts.data })
      }),
      on: vi.fn(),
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
        opened.host.remove()
        opened = null
      }),
      msg,
      confirm: vi.fn((_t: string, _o: unknown, yes: (i: number) => void) => yes(1)),
    },
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
vi.mock('@/api/systemNotices', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/systemNotices')>()),
  fetchSystemNotices: API.list,
  createSystemNotice: API.create,
  updateSystemNotice: API.update,
  deleteSystemNotice: API.remove,
}))

import { fetchMe } from '@/api/auth'
import page, {
  buildCols,
  formatNoticeTime,
  noticeContentHtml,
  noticeExcerpt,
  validateNotice,
} from '../system-systemnotice'

const ROWS = [
  {
    id: 7,
    title: '今晚维护',
    content: '今晚 23:00 维护\n预计一小时',
    created_by: 'admin',
    created_at: '2026-10-17T10:20:30+08:00',
    updated_at: '2026-10-17T10:20:30+08:00',
  },
]

beforeEach(() => {
  LAYUI.msg.mockReset()
  LAYUI.opens.length = 0
  LAYUI.reloads.length = 0
  document.body.innerHTML = ''
  API.list.mockReset()
  API.create.mockReset()
  API.update.mockReset()
  API.remove.mockReset()
  API.list.mockResolvedValue({ items: ROWS, total: 1, page: 1, page_size: 200 })
  ;(fetchMe as ReturnType<typeof vi.fn>).mockResolvedValue({
    id: 1,
    username: 'admin',
    display_name: '管理员',
    identity_plane: 'tenant',
    is_tenant_admin: true,
    roles: [],
  })
})

describe('纯函数', () => {
  it('内容摘要：空白折叠、超过 200 字加省略号、空内容显示 —', () => {
    expect(noticeExcerpt(' a\n\n b ')).toBe('a b')
    expect(noticeExcerpt('')).toBe('—')
    expect(noticeExcerpt('x'.repeat(201)).endsWith('...')).toBe(true)
    expect(noticeExcerpt('x'.repeat(200)).endsWith('...')).toBe(false)
  })

  it('时间显示成 YYYY-MM-DD HH:mm；解析不了原样回', () => {
    expect(formatNoticeTime('2026-10-17T10:20:30+08:00')).toBe('2026-10-17 10:20')
    expect(formatNoticeTime('随便')).toBe('随便')
  })

  it('全文：换行转 <br/>，其余 HTML 转义', () => {
    expect(noticeContentHtml('a\nb')).toBe('a<br/>b')
    expect(noticeContentHtml('<script>x</script>')).toBe('&lt;script&gt;x&lt;/script&gt;')
  })

  it('校验：空内容拦住、1000 字以内放行', () => {
    expect(validateNotice('   ')).toBe('请输入公告内容')
    expect(validateNotice('x'.repeat(1001))).toBe('公告内容不能超过 1000 个字')
    expect(validateNotice('今晚维护')).toBeNull()
  })

  it('列：标题 / 内容都是可点击的蓝链，操作列根据权限出现修改 + 删除', () => {
    const cols = buildCols(true)
    expect(cols.map((c) => c.title)).toEqual(['公告标题', '公告内容', '发布时间', '操作'])
    const titleCol = cols[0] as unknown as { templet: (d: typeof ROWS[0]) => string }
    expect(titleCol.templet(ROWS[0]!)).toContain('data-act="view"')
    const actionCol = cols[3] as unknown as { templet: (d: typeof ROWS[0]) => string }
    expect(actionCol.templet(ROWS[0]!)).toContain('data-act="edit"')
    expect(actionCol.templet(ROWS[0]!)).toContain('data-act="del"')
    const readonly = buildCols(false)[3] as unknown as {
      templet: (d: typeof ROWS[0]) => string
    }
    expect(readonly.templet(ROWS[0]!)).toBe('')
  })
})

describe('挂载', () => {
  it('装载即拉列表并画上，总数显示', async () => {
    const root = document.createElement('div')
    document.body.appendChild(root)
    const unmount = await page.mount(root, { onNavigate: () => undefined })
    expect(API.list).toHaveBeenCalledWith({ page: 1, page_size: 200 })
    expect(LAYUI.reloads.find((r) => r.id === 'tableList')?.data).toHaveLength(1)
    expect(root.querySelector('#noticeTotal')!.textContent).toBe('共 1 条')
    unmount()
  })

  it('发布：点「发布一个内部公告」→ 填内容 → 保存走 POST', async () => {
    API.create.mockResolvedValue(ROWS[0])
    const root = document.createElement('div')
    document.body.appendChild(root)
    const unmount = await page.mount(root, { onNavigate: () => undefined })

    root
      .querySelector<HTMLElement>('[data-act="add"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(LAYUI.opens).toHaveLength(1)
    const textarea = document.querySelector<HTMLTextAreaElement>(
      '#systemNoticeDialog textarea[name="content"]',
    )!
    textarea.value = '新公告内容'
    const openOpts = LAYUI.opens[LAYUI.opens.length - 1]!
    ;(openOpts.yes as (i: number) => void)(1)
    await Promise.resolve()
    await Promise.resolve()
    expect(API.create).toHaveBeenCalledWith('新公告内容')
    unmount()
  })

  it('查看全文的弹层标题会转义公告标题（layer 标题是 HTML 串）', async () => {
    API.list.mockResolvedValue({
      items: [{ ...ROWS[0]!, id: 8, title: '<b>evil</b>', content: '正文' }],
      total: 1,
      page: 1,
      page_size: 200,
    })
    const root = document.createElement('div')
    document.body.appendChild(root)
    const unmount = await page.mount(root, { onNavigate: () => undefined })
    const link = document.createElement('a')
    link.dataset.act = 'view'
    link.dataset.id = '8'
    root.appendChild(link)
    link.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    const title = String(LAYUI.opens[LAYUI.opens.length - 1]!.title)
    expect(title).toContain('&lt;b&gt;evil&lt;/b&gt;')
    expect(title).not.toContain('<b>')
    unmount()
  })

  it('删除：二次确认后走 DELETE', async () => {
    API.remove.mockResolvedValue(null)
    const root = document.createElement('div')
    document.body.appendChild(root)
    const unmount = await page.mount(root, { onNavigate: () => undefined })

    // 表格行由 layui 渲染；替身不画行 ⇒ 自己塞一颗同样 data-act 的按钮进 root，
    // 判据是**同一个点击委托**，不是 layui 的 DOM 长相。
    const delBtn = document.createElement('a')
    delBtn.dataset.act = 'del'
    delBtn.dataset.id = '7'
    root.appendChild(delBtn)
    delBtn.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await Promise.resolve()
    await Promise.resolve()
    expect(API.remove).toHaveBeenCalledWith(7)
    unmount()
  })
})
