/**
 * 【系统内部公告】页（island key = `system-systemnotice`，路由 `/system/notices`；
 * 2026-10-17 从「证据件渲染」改成**真功能**）。
 *
 * 对标 `/System/systemnotice`（`systemnotice.js`）的可见行为逐条照抄：
 *
 * - 工具栏两颗按钮：`刷新列表` / `发布一个内部公告`；
 * - 表格四列：`公告标题`（蓝链，点开全文）/ `公告内容`（前 200 字，点开全文）/ `发布时间` /
 *   `操作`（修改、删除）；
 * - `page: false`（不分页）、右上角「筛选列」图标；
 * - 发布 / 编辑弹窗**只有「公告内容」一个 textarea**（`maxlength=1000`），标题由服务端派生；
 * - 删除是硬删，二次确认（对标也有 `layer.confirm`，文案「是否确认删除？」那类）。
 *
 * 三处**我方新增**（都不改变对标已有的可见结构）：
 * 1. 标题 / 内容为空时列里显示「—」，不显示空白；
 * 2. 删除确认里写明「删除后主页通知卡片也会消失」；
 * 3. 表格空数据时给一句「还没有发布过公告」。
 *
 * ⚠️ 主页「通知和公告」卡片的数据来源见 `pages/index-console.ts`：它把本页的公告与站内通知
 * **合并**展示（公告在前）。公告**没有已读态**（与对标一致）。
 */
import { fetchMe, type User } from '@/api/auth'
import {
  SYSTEM_NOTICE_PAGE_SIZE,
  createSystemNotice,
  deleteSystemNotice,
  fetchSystemNotices,
  updateSystemNotice,
  type SystemNotice,
} from '@/api/systemNotices'
import { escapeHtml } from '@/layui-island/escape'
import { canNode } from '@/layui-island/perms'
import { bootstrapIsland, initIslandElements, type IslandContext, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

export const PAGE_KEY = 'system-systemnotice'
export const TITLE = '系统内部公告'
export const NOTICE_NODE = 'system.publish_notice'

export const TABLE_ID = 'tableList'
export const FORM_ID = 'systemNoticeDialog'
/** 对标 textarea 的 `maxlength="1000"`。 */
export const MAX_CONTENT = 1000
/** 列表里标题 / 内容的截断长度（对标：标题 50、内容 200）。 */
export const TITLE_MAX = 50
export const CONTENT_MAX = 200

/** 列表里内容摘要（对标 `res.content.substring(0, 200)`）。 */
export function noticeExcerpt(content: string): string {
  const text = (content ?? '').replace(/\s+/g, ' ').trim()
  if (!text) return '—'
  return text.length > CONTENT_MAX ? `${text.slice(0, CONTENT_MAX)}...` : text
}

/** 标题兜底：服务端已派生，理论上非空；这里只防「空标题显示空白」。 */
export function noticeTitle(notice: SystemNotice): string {
  return (notice.title ?? '').trim() || noticeExcerpt(notice.content)
}

/** 发布时间显示成 `YYYY-MM-DD HH:mm`；解析不了就原样回。 */
export function formatNoticeTime(raw: string): string {
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(raw ?? '')
  return match ? `${match[1]} ${match[2]}` : (raw ?? '')
}

/** 全文弹层的正文（换行转 `<br/>`；其余一律转义）。 */
export function noticeContentHtml(content: string): string {
  return escapeHtml(content ?? '')
    .replace(/\r\n/g, '<br/>')
    .replace(/\n/g, '<br/>')
}

/** 本地校验：只拦空内容（长度由 `maxlength` + 后端把住）。 */
export function validateNotice(content: string): string | null {
  if (!(content ?? '').trim()) return '请输入公告内容'
  if ((content ?? '').length > MAX_CONTENT) return `公告内容不能超过 ${MAX_CONTENT} 个字`
  return null
}

export function buildCols(canManage: boolean): Array<Record<string, unknown>> {
  return [
    {
      field: 'title',
      title: '公告标题',
      width: 200,
      align: 'left',
      templet: (d: SystemNotice) =>
        `<a href="javascript:void(0)" class="viewNotice" data-act="view" data-id="${d.id}"` +
        ` title="${escapeHtml(noticeTitle(d))}">${escapeHtml(noticeTitle(d).slice(0, TITLE_MAX))}...</a>`,
    },
    {
      field: 'content',
      title: '公告内容',
      align: 'left',
      templet: (d: SystemNotice) =>
        `<a href="javascript:void(0)" class="viewNotice" data-act="view" data-id="${d.id}"` +
        ` title="${escapeHtml(noticeExcerpt(d.content))}">${escapeHtml(noticeExcerpt(d.content))}</a>`,
    },
    {
      field: 'created_at',
      title: '发布时间',
      width: 160,
      align: 'center',
      templet: (d: SystemNotice) => `<span>${escapeHtml(formatNoticeTime(d.created_at))}</span>`,
    },
    {
      title: '操作',
      width: 160,
      align: 'left',
      templet: (d: SystemNotice) =>
        canManage
          ? `<a class="layui-btn layui-btn-xs layui-btn-warm" data-act="edit" data-id="${d.id}" title="编辑">` +
            '<i class="layui-icon layui-icon-edit"></i>修改</a>' +
            ` <a class="layui-btn layui-btn-xs layui-btn-danger" data-act="del" data-id="${d.id}" title="删除">` +
            '<i class="layui-icon layui-icon-delete"></i>删除</a>'
          : '',
    },
  ]
}

export function pageHtml(): string {
  return (
    '<style>.notice-page{padding:0 12px 12px}.notice-toolbar{padding:12px 0 0}' +
    '.notice-toolbar .layui-form-item{margin-bottom:10px}' +
    '.notice-dialog .layui-form-item{margin-bottom:10px}</style>' +
    '<div class="notice-page">' +
    '<form class="layui-form notice-toolbar" lay-filter="noticeToolbar">' +
    '<div class="layui-btn-container">' +
    '<button type="button" class="layui-btn layui-btn-xs" data-act="refresh" title="刷新当前数据">' +
    '<i class="layui-icon">&#xe669;</i>刷新列表</button>' +
    '<button type="button" class="layui-btn layui-btn-xs layui-btn-danger" data-act="add" title="发布一个内部公告">' +
    '<i class="layui-icon layui-icon-add-1"></i>发布一个内部公告</button>' +
    '<span id="noticeTotal" style="margin-left:8px;color:#ff6a00"></span>' +
    '</div></form>' +
    `<table id="${TABLE_ID}" lay-filter="${TABLE_ID}"></table>` +
    '</div>'
  )
}

export function dialogHtml(content = ''): string {
  return (
    `<div id="${FORM_ID}"><form class="layui-form layui-form-pane notice-dialog" lay-filter="${FORM_ID}">` +
    '<div class="layui-form-item layui-form-text">' +
    '<label class="layui-form-label">公告内容</label>' +
    '<div class="layui-input-block">' +
    `<textarea name="content" class="layui-textarea" maxlength="${MAX_CONTENT}"` +
    ' placeholder="请在此输入要发布给系统内所有人员看的公告"' +
    ' style="height:320px;font-size:14px">' +
    escapeHtml(content) +
    '</textarea></div></div></form></div>'
  )
}

export async function mountNoticePage(
  root: HTMLElement,
  _ctx: IslandContext,
): Promise<() => void> {
  const layui = await bootstrapIsland(['form', 'table', 'layer', 'util'])
  const form = layui.form as LayuiModule
  const table = layui.table as LayuiModule
  const layer = layui.layer as LayuiModule

  document.title = TITLE
  root.innerHTML = pageHtml()
  initIslandElements(layui as LayuiNamespace)

  const msg = (text: string): void => {
    ;(layer.msg as (t: string, o?: unknown) => void)(text, { time: 1600 })
  }
  const el = <T extends HTMLElement>(selector: string): T | null =>
    root.querySelector<T>(selector)

  let notices: SystemNotice[] = []
  let canManage = false
  let dialogIndex: number | null = null

  const closeDialog = (): void => {
    if (dialogIndex !== null) {
      ;(layer.close as (i: number) => void)(dialogIndex)
      dialogIndex = null
    }
  }

  async function reload(): Promise<boolean> {
    try {
      const page = await fetchSystemNotices({ page: 1, page_size: SYSTEM_NOTICE_PAGE_SIZE })
      notices = page.items
      ;(table.reloadData as (id: string, o: unknown) => void)(TABLE_ID, {
        data: page.items,
        page: false,
      })
      const total = el<HTMLElement>('#noticeTotal')
      if (total) total.textContent = `共 ${page.total} 条`
      if (page.total > page.items.length) {
        msg(`公告太多，只显示了前 ${page.items.length} 条（共 ${page.total} 条）`)
      }
      return true
    } catch (e) {
      msg(e instanceof Error ? e.message : '加载失败')
      return false
    }
  }

  function openDialog(notice: SystemNotice | null): void {
    if (!canManage) {
      msg('没有发布公告的权限')
      return
    }
    closeDialog()
    const index = (layer.open as (o: unknown) => number)({
      type: 1,
      title: notice ? '编辑公告' : '新增一个内部公告',
      area: ['60%', '75%'],
      shade: [0.3, '#333'],
      shadeClose: false,
      skin: 'layui-layer-lan',
      anim: 8,
      content: `<div style="padding:10px">${dialogHtml(notice?.content ?? '')}</div>`,
      btn: ['保存', '取消'],
      yes: (layerIndex: number) => {
        void submit(layerIndex, notice)
      },
      end: () => {
        dialogIndex = null
      },
    })
    dialogIndex = index
    ;(form.render as (type?: string) => void)()
  }

  async function submit(layerIndex: number, notice: SystemNotice | null): Promise<void> {
    const content =
      document.querySelector<HTMLTextAreaElement>(`#${FORM_ID} textarea[name="content"]`)?.value ?? ''
    const invalid = validateNotice(content)
    if (invalid !== null) {
      msg(invalid)
      return
    }
    try {
      if (notice) {
        await updateSystemNotice(notice.id, content)
      } else {
        await createSystemNotice(content)
      }
      msg('保存成功')
      ;(layer.close as (i: number) => void)(layerIndex)
      dialogIndex = null
      await reload()
    } catch (e) {
      msg(e instanceof Error ? e.message : '保存失败')
    }
  }

  function view(notice: SystemNotice): void {
    ;(layer.open as (o: unknown) => number)({
      type: 1,
      // ⚠️ layer 的标题会进 HTML 串 ⇒ 动态部分必须转义（与首页同一教训）。
      title: `内部公告：${escapeHtml(noticeTitle(notice).slice(0, 30))}...`,
      area: ['980px', '680px'],
      shadeClose: true,
      skin: 'layui-layer-rim',
      content:
        '<div style="padding:16px;font-size:14px;color:#000;line-height:30px;max-height:600px;overflow:auto;">' +
        `${noticeContentHtml(notice.content)}</div>`,
    })
  }

  function remove(notice: SystemNotice): void {
    ;(layer.confirm as (t: string, o: unknown, cb: (i: number) => void) => void)(
      `确定删除公告「${escapeHtml(noticeTitle(notice))}」吗？<br>删除后主页的「通知和公告」里也会消失。`,
      { title: '提示', btn: ['确定删除', '取消'] },
      (index: number) => {
        ;(layer.close as (i: number) => void)(index)
        void (async () => {
          try {
            await deleteSystemNotice(notice.id)
            msg('删除成功')
            await reload()
          } catch (e) {
            msg(e instanceof Error ? e.message : '删除失败')
          }
        })()
      },
    )
  }

  const onClick = (event: Event): void => {
    const target = event.target as HTMLElement
    const action = target.closest<HTMLElement>('[data-act]')
    if (!action) return
    const act = action.dataset.act
    const id = Number(action.dataset.id ?? 0)
    if (act === 'refresh') {
      event.preventDefault()
      void reload().then((ok) => {
        if (ok) msg('已刷新')
      })
      return
    }
    if (act === 'add') {
      event.preventDefault()
      openDialog(null)
      return
    }
    const notice = notices.find((n) => n.id === id)
    if (!notice) return
    if (act === 'view') {
      event.preventDefault()
      view(notice)
      return
    }
    if (act === 'edit') {
      event.preventDefault()
      openDialog(notice)
      return
    }
    if (act === 'del') {
      event.preventDefault()
      remove(notice)
    }
  }
  root.addEventListener('click', onClick)

  // 权限拿到之后再 render（操作列按它显示/隐藏）；数据随后由 `reload()` 灌进来。
  const me: User | null = await fetchMe().catch((): User | null => null)
  canManage = canNode(me ?? {}, NOTICE_NODE)

  ;(table.render as (o: unknown) => void)({
    elem: `#${TABLE_ID}`,
    data: [],
    page: false,
    defaultToolbar: ['filter'],
    toolbar: false,
    cols: [buildCols(canManage)],
    text: { none: '还没有发布过公告' },
  })
  await reload()

  return () => {
    root.removeEventListener('click', onClick)
    closeDialog()
    root.innerHTML = ''
  }
}

export function mount(root: HTMLElement, ctx: IslandContext): Promise<() => void> {
  return mountNoticePage(root, ctx)
}

export default { mount } satisfies IslandPage
