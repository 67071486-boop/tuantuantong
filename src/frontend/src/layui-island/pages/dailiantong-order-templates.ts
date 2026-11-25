/**
 * 【代练通订单要求模板】island 页（island key = `dailiantong-order-templates`，
 * 路由 `/system/dailiantong-order-templates`，菜单 label「代练通订单要求模板」）。
 *
 * 后端契约：`src/apps/business/api/dailiantong_extra_views.py::DailiantongOrderTemplate*View` +
 * `dailiantong_extra_actions.py`（增 / 改 / 删 / 设默认都**回读列表核验**）。
 * 前端封装与节点码在 `@/api/dailiantongExtra`。
 *
 * ## ⚠️ 这里的「订单要求模板」是**代练通侧**的，不是我们自己的「代练要求模板」
 *
 * 模板是上游 `OrderRequireTemp*`（在代练通后台建、发单到代练通时用，每账号最多 10 个）；
 * 本仓自己的「代练要求模板」（发单页那份全局模板 / `PlatformPushSettings`）**是两回事**。
 * 页面顶上有一句写死这句区别的说明（业务方要求「页面文案要写清」），⛔ 别把它删短。
 *
 * ## 增 / 改 / 删 / 设默认
 *
 * - **新增**：标题 + 内容 + 是否默认 → `POST …/order-templates/`；
 * - **修改**：点行内「编辑」回填表单 → `PATCH …/order-templates/<id>/`（三项都发，明确覆盖）；
 * - **删除 / 设默认**：**必须先过确认框**（`layer.confirm`），确认后才发请求；
 * - 每个写动作**提交中禁用全部按钮** + 在途闸（连点只发一次），成功后**重新取列表**；
 * - 失败时把后端中文**原样**显示（`layer.msg` + 表单旁那条常驻的红色说明），⛔ 不吞、不换句话。
 *
 * ## 三态必须看得见（⛔ 不静默空白）
 *
 * 状态条 `STATUS_ID` 恒在：**加载中…** / **共 N 条模板** / **还没有模板** /
 * **加载失败：<后端原文>**（失败时带一颗「重试」）。
 *
 * ## 四条 island 陷阱（`AGENTS.md` §5-G；本页**没有定时器**）
 *
 * ① 类名与选择器分成两个常量；
 * ② 以 `(` 开头的语句一律前置分号；
 * ③ 顺序：`await bootstrapIsland(...)`（先挂样式）→ 门禁 → `root.innerHTML = buildHtml()`
 *    （再塞标记）→ `initIslandElements(layui)`（最后按标记里的 `lay-*` 初始化）。
 * 三条都有**运行期**判据（`__tests__/dailiantong-order-templates.spec.ts`）。
 */
import {
  createDailiantongOrderTemplate,
  DAILIANTONG_EXTRA_NODE,
  deleteDailiantongOrderTemplate,
  listDailiantongOrderTemplates,
  setDefaultDailiantongOrderTemplate,
  updateDailiantongOrderTemplate,
  type OrderTemplateInput,
  type OrderTemplateList,
  type OrderTemplateRow,
} from '@/api/dailiantongExtra'
import { fetchMe } from '@/api/auth'
import { escapeHtml } from '@/layui-island/escape'
import { canNode } from '@/layui-island/perms'
import { bootstrapIsland, initIslandElements, islandMsg, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

/** island key（= 文件名 = 路由 `meta.islandPage`，三处必须逐字一致）。 */
export const PAGE_KEY = 'dailiantong-order-templates'
export const TITLE = '代练通订单要求模板'

/** 页面容器的**类名**（拼进标记用）与**选择器**（查节点用）——两者不能混（陷阱 ①）。 */
export const PANEL_CLASS = 'dltTplPanel'
export const PANEL_SELECTOR = `.${PANEL_CLASS}`

/** 表单节点。 */
export const TITLE_INPUT_ID = 'dltTplTitle'
export const CONTENT_INPUT_ID = 'dltTplContent'
export const DEFAULT_CHECKBOX_ID = 'dltTplDefault'
export const CREATE_BUTTON_ID = 'dltTplCreate'
export const SAVE_BUTTON_ID = 'dltTplSave'
export const CANCEL_BUTTON_ID = 'dltTplCancel'
export const REFRESH_BUTTON_ID = 'dltTplRefresh'
export const ACTION_MESSAGE_ID = 'dltTplActionMessage'

/** 列表 / 状态条节点。 */
export const STATUS_ID = 'dltTplStatus'
export const TABLE_HEAD_ID = 'dltTplTableHead'
export const TABLE_BODY_ID = 'dltTplTableBody'

/** 行与行内动作的属性（陷阱 ① 的同族：属性名进标记、选择器由常量拼）。 */
export const ROW_ATTR = 'data-dlt-template-row'
export const ACTION_ATTR = 'data-dlt-template-action'
export const ACTION_ID_ATTR = 'data-dlt-template-id'
export const ACTION_SELECTOR = `[${ACTION_ATTR}]`
export const RETRY_ATTR = 'data-dlt-template-retry'
export const RETRY_SELECTOR = `[${RETRY_ATTR}]`

/** 后端序列化器的上限（`dailiantong_extra_actions.py` 的两个常量）。 */
export const TITLE_MAX_LENGTH = 64
export const CONTENT_MAX_LENGTH = 1024

/** 页内文案（抽成常量只为让 spec 钉住字面值）。 */
export const MESSAGES = {
  loading: '加载中…',
  empty: '还没有模板',
  failed: '加载失败',
  actionFailed: '操作失败',
  noAccess: '无权限查看代练通订单要求模板',
  needTitle: '请填写模板标题',
  needContent: '请填写模板内容',
  created: '模板已创建',
  updated: '模板已更新',
  deleted: '模板已删除',
  defaulted: '已设为默认模板',
  deleteConfirm: '删除后不可恢复，请确认。',
  defaultConfirm: '代练通发单会默认使用它。',
} as const

/** 列表的列（顺序与 `rowHtml` 一一对应）。 */
export const LIST_HEADS: readonly string[] = ['模板标题', '模板内容', '是否默认', '操作']

/** 默认徽标（自带样式值取 vendored `layui.css` 的 `layui-bg-green`）。 */
const DEFAULT_BADGE = '<span class="layui-badge layui-bg-green">默认</span>'
const NOT_DEFAULT_TEXT = '<span style="color:#999">否</span>'
const EMPTY_CELL = '<span style="color:#999">—</span>'

/** 错误对象 → 用户可见文案（后端 `msg` 优先；兜底那句只在意外分支用）。 */
function errorText(error: unknown): string {
  return error instanceof Error && error.message !== '' ? error.message : MESSAGES.failed
}

/** 表头标记（纯函数）。 */
export function headHtml(): string {
  return LIST_HEADS.map((head) => `<th>${escapeHtml(head)}</th>`).join('')
}

/** 一条模板的「操作」格（纯函数；`id` 解析不出时没有可点的动作）。 */
export function actionCell(row: OrderTemplateRow): string {
  if (row.id === null) return EMPTY_CELL
  const edit =
    `<button type="button" class="layui-btn layui-btn-xs" ${ACTION_ATTR}="edit" ` +
    `${ACTION_ID_ATTR}="${row.id}">编辑</button>`
  // 已经是默认的那条不再画「设为默认」（再点一次没有语义，只会多一发请求）。
  const makeDefault = row.is_default
    ? ''
    : ` <button type="button" class="layui-btn layui-btn-xs layui-btn-normal" ` +
      `${ACTION_ATTR}="default" ${ACTION_ID_ATTR}="${row.id}">设为默认</button>`
  const remove =
    ` <button type="button" class="layui-btn layui-btn-xs layui-btn-danger" ` +
    `${ACTION_ATTR}="delete" ${ACTION_ID_ATTR}="${row.id}">删除</button>`
  return edit + makeDefault + remove
}

/**
 * 列表的一行（纯函数）。⚠️ 标题 / 内容是上游自由文本且走 `innerHTML` ⇒ 一律过 `escapeHtml`。
 */
export function rowHtml(row: OrderTemplateRow): string {
  const id = row.id === null ? '' : ` ${ROW_ATTR}="${row.id}"`
  return (
    `<tr${id}>` +
    `<td>${row.title === '' ? EMPTY_CELL : escapeHtml(row.title)}</td>` +
    `<td style="white-space:pre-wrap">${row.temp === '' ? EMPTY_CELL : escapeHtml(row.temp)}</td>` +
    `<td>${row.is_default ? DEFAULT_BADGE : NOT_DEFAULT_TEXT}</td>` +
    `<td>${actionCell(row)}</td>` +
    `</tr>`
  )
}

/** 整个列表的标记（纯函数；空态要说出来，⛔ 不是留一块空白）。 */
export function listHtml(items: readonly OrderTemplateRow[]): string {
  if (items.length === 0) {
    return (
      `<tr><td colspan="${LIST_HEADS.length}" style="text-align:center;color:#999">` +
      `${MESSAGES.empty}</td></tr>`
    )
  }
  return items.map((row) => rowHtml(row)).join('')
}

/** 页面标记（说明 + 表单 + 列表）。 */
export function buildHtml(): string {
  return `
<div class="${PANEL_CLASS}" style="padding:10px 15px">
  <div style="margin:4px 0 10px;padding:8px 10px;background:#f8f8f8;border:1px solid #eee;color:#666">
    这里的模板是<b>代练通侧</b>的「订单要求模板」（上游 OrderRequireTemp，发单到代练通时使用），
    <b>不是</b>本系统的「代练要求模板」——两者互不相干，别混用。
  </div>

  <div style="margin:8px 0">
    <div class="layui-form-item">
      <label class="layui-form-label" style="width:88px">模板标题</label>
      <div class="layui-input-block">
        <input type="text" id="${TITLE_INPUT_ID}" maxlength="${TITLE_MAX_LENGTH}" autocomplete="off" class="layui-input" placeholder="最多 ${TITLE_MAX_LENGTH} 个字符">
      </div>
    </div>
    <div class="layui-form-item">
      <label class="layui-form-label" style="width:88px">模板内容</label>
      <div class="layui-input-block">
        <textarea id="${CONTENT_INPUT_ID}" maxlength="${CONTENT_MAX_LENGTH}" rows="3" class="layui-textarea" placeholder="发单到代练通时展示给打手的订单要求（最多 ${CONTENT_MAX_LENGTH} 个字符）"></textarea>
      </div>
    </div>
    <div class="layui-form-item">
      <label class="layui-form-label" style="width:88px">是否默认</label>
      <div class="layui-input-block" style="line-height:36px">
        <input type="checkbox" id="${DEFAULT_CHECKBOX_ID}">
        <span style="color:#999;margin-left:6px">勾上 = 保存后成为代练通侧的默认模板</span>
      </div>
    </div>
    <div class="layui-form-item">
      <button type="button" class="layui-btn" id="${CREATE_BUTTON_ID}">新增模板</button>
      <button type="button" class="layui-btn layui-btn-normal" id="${SAVE_BUTTON_ID}" hidden>保存修改</button>
      <button type="button" class="layui-btn layui-btn-primary" id="${CANCEL_BUTTON_ID}" hidden>取消编辑</button>
      <span id="${ACTION_MESSAGE_ID}" style="margin-left:8px"></span>
    </div>
  </div>

  <div style="margin:14px 0 8px">
    <span style="font-size:14px;font-weight:700">模板列表</span>
    <button type="button" class="layui-btn layui-btn-sm layui-btn-primary" id="${REFRESH_BUTTON_ID}" style="margin-left:8px">刷新</button>
  </div>
  <div id="${STATUS_ID}" style="margin:4px 0;min-height:20px">${MESSAGES.loading}</div>
  <table class="layui-table" style="margin:0">
    <thead><tr id="${TABLE_HEAD_ID}"></tr></thead>
    <tbody id="${TABLE_BODY_ID}"></tbody>
  </table>
</div>`
}

export const __testables = {
  actionCell,
  buildHtml,
  headHtml,
  listHtml,
  rowHtml,
}

export default {
  mount: async (root: HTMLElement, _ctx): Promise<() => void> => {
    document.title = TITLE
    // ⚠️ 顺序是硬要求（`AGENTS.md` §5-G 陷阱 ③）：先挂样式，再塞标记，最后 `initIslandElements`。
    const layui: LayuiNamespace = await bootstrapIsland(['layer'])
    const layer = layui.layer as LayuiModule
    const msg = (text: string): void => islandMsg(layer as unknown as Record<string, unknown>, text)

    // ⚠️ 门禁必须排在注入标记**之前**（iframe 深链时路由 `meta.permissions` 不生效）。
    const me = await fetchMe()
    if (!canNode(me, DAILIANTONG_EXTRA_NODE)) {
      msg(MESSAGES.noAccess)
      return () => {}
    }

    root.innerHTML = buildHtml()
    initIslandElements(layui)

    const titleEl = root.querySelector<HTMLInputElement>(`#${TITLE_INPUT_ID}`)
    const contentEl = root.querySelector<HTMLTextAreaElement>(`#${CONTENT_INPUT_ID}`)
    const defaultEl = root.querySelector<HTMLInputElement>(`#${DEFAULT_CHECKBOX_ID}`)
    const createBtn = root.querySelector<HTMLButtonElement>(`#${CREATE_BUTTON_ID}`)
    const saveBtn = root.querySelector<HTMLButtonElement>(`#${SAVE_BUTTON_ID}`)
    const cancelBtn = root.querySelector<HTMLButtonElement>(`#${CANCEL_BUTTON_ID}`)
    const refreshBtn = root.querySelector<HTMLButtonElement>(`#${REFRESH_BUTTON_ID}`)
    const actionEl = root.querySelector<HTMLElement>(`#${ACTION_MESSAGE_ID}`)
    const statusEl = root.querySelector<HTMLElement>(`#${STATUS_ID}`)
    const headEl = root.querySelector<HTMLElement>(`#${TABLE_HEAD_ID}`)
    const bodyEl = root.querySelector<HTMLElement>(`#${TABLE_BODY_ID}`)

    /** 最近一次取回的模板（行内动作按 id 找它，⛔ 不把整行数据塞进 DOM）。 */
    let rows: OrderTemplateRow[] = []
    /** 正在编辑的模板 id（`null` = 新建态）。 */
    let editingId: number | null = null
    /** 在途闸（防连点写两次）。 */
    let busy = false

    type StateKind = 'loading' | 'ready' | 'empty' | 'error'

    /** 列表状态条的**唯一**出口。失败态用 DOM 节点拼（后端原文可能含 `<`），⛔ 不拼 HTML。 */
    function setState(kind: StateKind, text: string): void {
      if (!statusEl) return
      statusEl.textContent = ''
      statusEl.style.color = kind === 'error' ? '#ff0000' : '#666'
      if (kind === 'error') {
        const span = document.createElement('span')
        span.textContent = `${MESSAGES.failed}：${text}`
        const retry = document.createElement('button')
        retry.type = 'button'
        retry.className = 'layui-btn layui-btn-xs layui-btn-normal'
        retry.setAttribute(RETRY_ATTR, '1')
        retry.style.marginLeft = '8px'
        retry.textContent = '重试'
        statusEl.appendChild(span)
        statusEl.appendChild(retry)
        return
      }
      statusEl.textContent = text
    }

    /** 表单旁那条常驻说明（成功绿 / 失败红）。⛔ 后端原文按 `textContent` 放，不拼 HTML。 */
    function setActionMessage(text: string, kind: 'none' | 'ok' | 'error'): void {
      if (!actionEl) return
      actionEl.textContent = text
      actionEl.style.color = kind === 'error' ? '#ff0000' : kind === 'ok' ? '#5FB878' : '#666'
    }

    /** 提交中禁用全部按钮（视觉 + 语义双保险；真正的连点闸是 `busy`）。 */
    function setBusy(value: boolean): void {
      for (const button of root.querySelectorAll('button')) {
        button.disabled = value
      }
    }

    /** 新建态 / 编辑态切换（只改按钮可见性，⛔ 不清空用户已经打的字）。 */
    function setEditing(id: number | null): void {
      editingId = id
      if (createBtn) createBtn.hidden = id !== null
      if (saveBtn) saveBtn.hidden = id === null
      if (cancelBtn) cancelBtn.hidden = id === null
    }

    /** 把一条模板回填进表单（编辑入口）。 */
    function fillForm(row: OrderTemplateRow): void {
      if (titleEl) titleEl.value = row.title
      if (contentEl) contentEl.value = row.temp
      if (defaultEl) defaultEl.checked = row.is_default
      setEditing(row.id)
    }

    /** 回到新建态（提交成功 / 取消编辑都走它）。 */
    function resetForm(): void {
      if (titleEl) titleEl.value = ''
      if (contentEl) contentEl.value = ''
      if (defaultEl) defaultEl.checked = false
      setEditing(null)
    }

    /** 读表单 → 契约载荷（三处都是页面自己的控件）。 */
    function readForm(): OrderTemplateInput {
      return {
        title: titleEl?.value.trim() ?? '',
        temp: contentEl?.value.trim() ?? '',
        is_default: defaultEl?.checked ?? false,
      }
    }

    /** 体验层预检（文案带字段名；服务端还有一层同义校验）。返回空串 = 通过。 */
    function precheck(payload: OrderTemplateInput): string {
      if (payload.title === '') return MESSAGES.needTitle
      if (payload.temp === '') return MESSAGES.needContent
      return ''
    }

    /** 取列表并铺开（唯一的读出口；写成功后也走它）。 */
    async function reload(): Promise<void> {
      setState('loading', MESSAGES.loading)
      try {
        const payload: OrderTemplateList = await listDailiantongOrderTemplates()
        rows = payload.items
        if (headEl) headEl.innerHTML = headHtml()
        if (bodyEl) bodyEl.innerHTML = listHtml(rows)
        if (rows.length === 0) {
          setState('empty', MESSAGES.empty)
        } else {
          const count = payload.record_count === null ? '' : `（服务端总数 ${payload.record_count}）`
          setState('ready', `共 ${rows.length} 条模板${count}`)
        }
      } catch (error) {
        setState('error', errorText(error))
      }
    }

    /**
     * 确认框 → 执行（删除 / 设默认共用）。
     * ⚠️ `busy` 闸在**打开确认框之前**也查一次：连点同一颗按钮不会叠出第二个确认框。
     * ⚠️ 用户取消时 layui 不回调 ⇒ 什么都不发（没有「悄悄执行」的分支）。
     */
    function confirmThen(text: string, title: string, run: () => void): void {
      ;(layer.confirm as (t: string, o: unknown, cb: (i: number) => void) => void)(
        text,
        { title },
        (index: number) => {
          ;(layer.close as (i: number) => void)(index)
          run()
        },
      )
    }

    /** 写动作的公共收尾：失败原样显示后端中文，成功刷新列表。 */
    async function writeAndReload(
      action: () => Promise<unknown>,
      successText: string,
      after?: () => void,
    ): Promise<void> {
      if (busy) return
      busy = true
      setBusy(true)
      setActionMessage('', 'none')
      try {
        await action()
        setActionMessage(successText, 'ok')
        after?.()
        await reload()
      } catch (error) {
        const text = errorText(error)
        msg(text)
        setActionMessage(`${MESSAGES.actionFailed}：${text}`, 'error')
      } finally {
        busy = false
        setBusy(false)
      }
    }

    /** 新增模板（确认框不是必需的：它不改已有数据）。 */
    function create(): void {
      if (busy) return
      const payload = readForm()
      const problem = precheck(payload)
      if (problem !== '') {
        msg(problem)
        setActionMessage(problem, 'error')
        return
      }
      void writeAndReload(
        () => createDailiantongOrderTemplate(payload),
        MESSAGES.created,
        resetForm,
      )
    }

    /** 保存修改（必须处于编辑态）。 */
    function save(): void {
      if (busy || editingId === null) return
      const payload = readForm()
      const problem = precheck(payload)
      if (problem !== '') {
        msg(problem)
        setActionMessage(problem, 'error')
        return
      }
      const id = editingId
      void writeAndReload(
        () => updateDailiantongOrderTemplate(id, payload),
        MESSAGES.updated,
        resetForm,
      )
    }

    /** 删除一条模板（**先确认**）。 */
    function askDelete(row: OrderTemplateRow): void {
      if (busy || row.id === null) return
      const id = row.id
      const label = row.title === '' ? `模板 ${id}` : row.title
      confirmThen(
        `确定删除模板「${escapeHtml(label)}」吗？${MESSAGES.deleteConfirm}`,
        '删除模板',
        () => {
          void writeAndReload(
            () => deleteDailiantongOrderTemplate(id),
            MESSAGES.deleted,
            () => {
              if (editingId === id) resetForm()
            },
          )
        },
      )
    }

    /** 设默认模板（**先确认**）。 */
    function askDefault(row: OrderTemplateRow): void {
      if (busy || row.id === null || row.is_default) return
      const id = row.id
      const label = row.title === '' ? `模板 ${id}` : row.title
      confirmThen(
        `确定把模板「${escapeHtml(label)}」设为默认吗？${MESSAGES.defaultConfirm}`,
        '设为默认模板',
        () => {
          void writeAndReload(
            () => setDefaultDailiantongOrderTemplate(id),
            MESSAGES.defaulted,
          )
        },
      )
    }

    /** 开始编辑（行内「编辑」）。 */
    function startEdit(row: OrderTemplateRow): void {
      if (busy || row.id === null) return
      fillForm(row)
      setActionMessage('', 'none')
      titleEl?.focus()
    }

    // ── 事件接线 ────────────────────────────────────────────────
    createBtn?.addEventListener('click', create)
    saveBtn?.addEventListener('click', save)
    cancelBtn?.addEventListener('click', () => {
      if (busy) return
      resetForm()
      setActionMessage('', 'none')
    })
    refreshBtn?.addEventListener('click', () => {
      if (busy) return
      void reload()
    })
    // 行内动作走**事件委托**（行由 `reload()` 重铺 innerHTML，逐行挂监听会随重铺丢失）。
    bodyEl?.addEventListener('click', (event: MouseEvent) => {
      const button = (event.target as HTMLElement | null)?.closest(ACTION_SELECTOR)
      if (!button) return
      const action = button.getAttribute(ACTION_ATTR)
      const id = Number(button.getAttribute(ACTION_ID_ATTR))
      const row = rows.find((item) => item.id === id)
      if (!row) return
      if (action === 'edit') startEdit(row)
      else if (action === 'delete') askDelete(row)
      else if (action === 'default') askDefault(row)
    })
    // 列表失败态的「重试」同样走委托。
    statusEl?.addEventListener('click', (event: MouseEvent) => {
      const target = event.target as HTMLElement | null
      if (!target?.closest(RETRY_SELECTOR)) return
      if (busy) return
      void reload()
    })

    if (headEl) headEl.innerHTML = headHtml()
    await reload()

    return () => {
      root.innerHTML = ''
    }
  },
} satisfies IslandPage
