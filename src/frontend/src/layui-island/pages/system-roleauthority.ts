/**
 * 【角色职位权限】页的 **layui 实现**（对标物 `/System/RoleAuthority`，1:1 复刻 + **接后端**）。
 *
 * ## 四个面里的两个在这里（面 1 列表页 + 面 4 复制弹层）
 * - 面 2（新增/编辑）→ `./system-roleauthority-edit.ts`，`layer type:2` iframe
 * - 面 3（权限清单）→ `./system-roleauthority-view.ts`，`layer type:2` iframe
 * - 面 4（复制）→ **本文件**，`layer type:1` 内联 DOM（对标 `roleauthority.js:168-193`）
 *   裁决依据 **D-0031**：两个"编辑/查看"用 iframe，复制照对标用 type:1。
 *
 * ## 标记来自证据件，不用 `artifactPage`
 * `public/replica/system-roleauthority.html`（零转义，逐字来自对标页面）承载
 * 表壳 / `#toolbarDemo` / `#baroperating` / 隐藏的 `.copyRole`。**不用 `artifactPage`**：
 * 它把所有可点元素收口成「尚未开放」（`artifactPage.ts:259-269`），而本页要点真的按钮。
 * 所以这里只复用它的 `fetchArtifactHtml`。
 *
 * ## 取值出处（逐条）
 * - 表格 `cols` / `page:false` / `defaultToolbar:['filter']`
 *   → `.tools/recon/suqing1/assets/JS2019/System/roleauthority.js` **L20-43**
 *   （⚠️ 该对象里 `defaultToolbar` 写了**两次**，后写的 `['filter']` 生效）
 * - 工具栏两键与行内三键 → 证据件的 `#toolbarDemo` / `#baroperating`（= 对标 HTML 原文）
 * - 复制弹层三字段与校验文案 → `roleauthority.js` **L107-117**（校验）与证据件 `.copyRole`（标记）
 *
 * ## 不复刻的对标缺陷（登记在 `.agents/notes/implemented/architecture/2026-09-20-rbac-frontend-execution.md`）
 * - `roleauthority.js:30` 的 `data-name="…'+"` 多一个 `+` ⇒ 弹层标题会变成「<角色名>+的权限清单」。
 *   我们**不复刻**，标题就是「<角色名>的权限清单」（spec §8.1 / `FRONTEND-REPLICA-STANDARD.md` §5）。
 */
import { fetchMe, type User } from '@/api/auth'
// ⚠️ 不 import `listRoleAuthorities`：列表由 layui 的 `table.render({url})` 自己取（见 `mount`），
// 再引一份 axios 版封装会是没有消费方的死 import（`pnpm lint` 的 no-unused-vars 会红）。
import {
  copyRoleAuthority,
  deleteRoleAuthority,
  type RoleAuthorityRow,
} from '@/api/roleAuthority'
import { fetchArtifactHtml } from '@/layui/artifactPage'
import { canNode, type NodeViewerInput } from '@/layui-island/perms'
import { escapeHtml } from '@/layui-island/escape'
import {
  bootstrapIsland,
  islandMsg,
  type IslandContext,
  type IslandPage,
} from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

/** 证据件（对标页 `<style>` + body 标记，零转义）。 */
const ARTIFACT_URL = 'replica/system-roleauthority.html'

/** 接口前缀（表格 `table.render({url})` 用；开发期后端可能不在同源）。 */
const API_BASE = import.meta.env.VITE_API_BASE ?? ''
/**
 * 弹层页 URL（`/layui-island/page.html`）的基址。**与 `API_BASE` 分开是两个常量**：
 * 接口走 `VITE_API_BASE`，而**页面是构建产物里的静态资源**，走 `BASE_URL`。
 * 子路径部署（README 的 `TENANT_WEB_BASE=/console/` ⇒ `BASE_URL='/console/'`）下两者不同——
 * 拿 `API_BASE` 拼页 URL 会得到 `/layui-island/page.html`（**少了 `/console` 前缀**）⇒ 弹层整页 404。
 * 同仓约定见 `components/LayuiIslandHost.vue`（`import.meta.env.BASE_URL`）与
 * `pages/order-orderlist.ts`（其常量名叫 `API_BASE` 却赋 `BASE_URL`）。
 */
const PAGE_BASE = import.meta.env.BASE_URL.replace(/\/$/, '')

/** 两个 iframe 弹层的 island page key（文件名必须逐字一致）。 */
export const AR_EDIT_PAGE_KEY = 'system-roleauthority-edit'
export const AR_VIEW_PAGE_KEY = 'system-roleauthority-view'

/** 对标 `roleauthority.js:30` 的 `title` 后缀（逐字）。 */
export const NAME_TITLE_SUFFIX = '，点击可以查看权限清单'

/**
 * 复制弹层的名称上限 = **5**（对齐后端 `Role.name` 的 `max_length=5`）。
 *
 * ⚠️ **这里刻意偏离对标**：对标 `roleauthority.js` 与它的 `.copyRole` 标记写的是
 * `maxlength="6"`、提示文案写「2-6个字符」，而**它自己的模型上限是 5**
 * （`src/apps/rbac/serializers.py:11` 的 `MAX_NAME = 5`）。
 * 本线最初照抄对标（前端 6 + 后端回业务码），**2026-09-21 业务方裁决改为「对齐后端」= 5**，
 * 与 `main` 上那套并行实现的 `COPY_NAME_MAX_LENGTH = 5` 同口径。
 * 代价：这一条与对标的 `maxlength="6"` **不再 1:1**，已登记为已知偏离
 * （`.agents/notes/implemented/architecture/2026-09-20-rbac-frontend-execution.md`）。
 *
 * ⚠️ 证据件 `public/replica/system-roleauthority.html` 里的 `maxlength="6"` 是**对标留档**，
 * **不要改它**——本弹层的标记由 `copyDialogHtml()` 自己生成。
 */
export const COPY_NAME_MAXLENGTH = 5

/** 复制弹层的两条校验文案（对标 `roleauthority.js:107-117`；长度那条随上限一起改成 2-5）。 */
export const COPY_VALIDATION = {
  empty: '请填写新职位名称！',
  /** 对标原文是「职位名称为2-6个字符！」（提示 2-6、实际卡 1..6）；上限改 5 后同改为 2-5，**实际卡 1..5**。 */
  length: '职位名称为2-5个字符！',
} as const

/**
 * 节点判据与它的入参类型搬到了共享模块 `@/layui-island/perms`（首页快捷操作的权限门也要用
 * 同一判据，见该模块头部）。这里**保持对外形状不变**：本页 spec 与本文件的 `canDo` 继续从
 * 本模块拿 `NodeViewerInput` / `viewerCanNode`，实现只有 `perms.ts` 一份。
 */
export type { NodeViewerInput }

export { canNode as viewerCanNode }

/** 弹层保存回执只认同源消息（`island:reload` 是个可被任意窗口伪造的消息类型）。 */
export function isSameOriginMessage(origin: string, current: string): boolean {
  return origin === current
}

/**
 * 复制弹层「新职位名称」的校验：返回 `null` = 通过，否则返回要弹的文案。
 *
 * 抽成导出纯函数是为了**让测试直接测生产判据**：原先把这段判据在测试里抄了一份，
 * 于是把 `> 6 || < 2` 改成别的值时那几条断言**照样全绿**（E-0024 族：自指断言）。
 */
export function validateCopyName(jobName: string): string | null {
  if (jobName === '') return COPY_VALIDATION.empty
  // ⚠️ 对标原文是 `length > 6 || length < 1`（提示写 2-6、实际卡 1..6）；**本线不照抄那个 6**——
  // 上限已按 2026-09-21 业务方裁决改为 `COPY_NAME_MAXLENGTH = 5`（见该常量的注释）。
  if (jobName.length > COPY_NAME_MAXLENGTH || jobName.length < 1) return COPY_VALIDATION.length
  return null
}

/**
 * 本页 5 个动作各自的权限点（与后端 `apps/rbac/views.py::ACTION_NODES` + `DEFAULT_NODE` 同口径）。
 * 抽成一张表 + 一个判据，是为了让「哪个动作要哪个点」**可以被测试逐条钉住**——
 * 三个布尔散在回调里时，"删除误用了 edit 的判据"不会被任何判据抓住。
 */
export const AR_ACTION_NODES = {
  add: 'system.role_authority_edit',
  edit: 'system.role_authority_edit',
  copy: 'system.role_authority_edit',
  delete: 'system.role_authority_delete',
  view: 'system.role_authority_view',
} as const

export type ArAction = keyof typeof AR_ACTION_NODES

/** 该动作对应的节点判据。 */
export function canDo(me: NodeViewerInput, action: ArAction): boolean {
  return canNode(me, AR_ACTION_NODES[action])
}

/** HTML 属性/文本插值的最小转义（`Role.name` 只受 max_length 约束，可能含引号）。 */
export function escapeAttr(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/**
 * 列表表格的列（对标 `roleauthority.js:22-43`）。
 *
 * ⚠️ **名字列永远渲染对标那条蓝链**（`#1E9FFF` + `class="viewRole"`）——**不按权限换形态**：
 * 换成纯文本会改掉**可见的计算样式**（链接是 `#1E9FFF`、纯文本继承 `#333`），
 * 而 D-0025 要的是 1:1。权限判据落在**点击**上（`system.role_authority_view`，
 * 见 `mount` 里的 `.viewRole` 委托）：看得到、点不开 ≠ 置灰——前者不破坏复刻判据。
 */
export function buildCols(): Record<string, unknown>[] {
  return [
    {
      field: 'name',
      title: '角色职位名称',
      width: 150,
      align: 'left',
      unresize: false,
      templet: (row: RoleAuthorityRow): string =>
        `<a href="javascript:void(0);" class="viewRole" data-id="${row.id}" data-name="${escapeAttr(row.name)}" style="color:#1E9FFF" title="${escapeAttr(row.name)}${NAME_TITLE_SUFFIX}">${escapeAttr(row.name)}</a>`,
    },
    {
      field: 'remark',
      title: '角色说明',
      align: 'left',
      sort: false,
      unresize: true,
      templet: (row: RoleAuthorityRow): string =>
        `<a href="javascript:void(0);" style="" title="${escapeAttr(row.remark)}">${escapeAttr(row.remark)}</a>`,
    },
    { fixed: '', title: '操作', width: 300, toolbar: '#baroperating', unresize: true },
  ]
}

/**
 * 复制弹层的标记（`type:1` 内联 DOM）。
 *
 * 逐字取自证据件的 `.copyRole`（= 对标 HTML 原文），只把两个值**预先填好**：
 * `currentJobId` = 被复制的角色 id、`#currole` = 角色名。
 * 对标的做法是打开前 `$(".currentJobId").val(id)` / `$('#currole').val(name)` 再
 * `content: $(".copyRole")`（把 DOM **搬进**弹层）；我们用**等价但不动原节点**的写法：
 * 生成一份副本 ⇒ 关闭时不需要还原、也不必依赖 layer 的"关闭时放回原位"行为。
 * 输入框**每次都是空的**（对标在 `cancel` 里清空 `#jobName`/`#remark`，副本天然为空）。
 *
 * ⚠️ **外面必须包一层 `.layui-form`**（对标没有：它的 `.copyRole` 本来就在页面那个
 * `<form class="layui-form">` 里，被搬进弹层后祖先没了——我们不复制这个副作用）。
 * 依据是读源码，不是猜：layui 的提交处理器就是 `form` 模块的 `t.prototype.submit`
 * （`public/layui/layui.js` 偏移 271670 的 `on("click","*[lay-submit]",…` 绑定的那个 `i`，
 * 而 `i` 是偏移 253152 处那条逗号表达式 `i=(t.prototype.set=…,…,t.prototype.submit=…)` 的值），
 * 它取字段用的是 `a.parents(".layui-form").eq(0)`（偏移 271020）→ `getValue(null, n)`
 * （偏移 271091，`getValue` 本体在偏移 253765，内部是 `t.find("input,select,textarea")`）。
 * 按钮**没有** `.layui-form` 祖先时那个集合为空 ⇒ `data.field` 恒为 `{}` ⇒ 弹层永远弹
 * 「请填写新职位名称！」，**点了没反应**且不报错。包一层后祖先存在，字段才收集得到。
 * 用 `<div>` 而不是 `<form>`：`parents()` 按 class 匹配，且 `layui.css` 里 `.layui-form`
 * **本身**没有规则块（那几条命中都是**后代**选择器：`.layui-form input[type=checkbox]` /
 * `.layui-form input[type=radio]` / `.layui-form select{display:none}`、
 * `.layui-form [lay-ignore]{display:initial}`、`.layui-form [lay-checkbox]{display:none}`、
 * `.layui-form [lay-radio]{display:none}`）——**本弹层不含这些控件** ⇒ 不改变可见样式。
 * 同型处置见 `order-orderlist.ts:818-824`（那边的弹层同样包一层 `.layui-form`，理由是 radio 渲染）。
 */
export function copyDialogHtml(roleId: number, roleName: string): string {
  return `
    <div class="layui-form">
    <div class="copyRole">
        <div class="layui-row">
            <div class="layui-form-item">
                <label class="layui-form-label">当前角色职位名称：</label>
                <div class="layui-input-block">
                    <input type="text" name="currole" id="currole" disabled class="layui-input layui-input-shadow" value="${escapeAttr(roleName)}">
                </div>
            </div>
            <div class="layui-form-item">
                <label class="layui-form-label">新角色职位名称：</label>
                <div class="layui-input-block">
                    <input type="text" name="jobName" id="jobName" autocomplete="off" placeholder="新角色职位名称" class="layui-input layui-input-shadow" maxlength="${COPY_NAME_MAXLENGTH}">
                </div>
            </div>
            <div class="layui-form-item">
                <label class="layui-form-label">角色职位说明：</label>
                <div class="layui-input-block">
                    <textarea name="remark" id="remark" placeholder="请输入角色职位说明" class="layui-textarea" maxlength="50"></textarea>
                </div>
            </div>
            <div class="layui-form-item">
                <div class="layui-input-block">
                    <button class="layui-btn layui-btn-normal" lay-submit="" type="button" lay-filter="copysubbtn" style="width: 80px">提交</button>
                </div>
            </div>
            <input type="hidden" name="currentJobId" class="currentJobId" value="${roleId}" />
        </div>
    </div>
    </div>`
}

export default {
  mount: async (root: HTMLElement, _ctx: IslandContext) => {
    document.title = '角色职位权限'
    const layui: LayuiNamespace = await bootstrapIsland(['form', 'table', 'layer'])
    const form = layui.form as LayuiModule
    const table = layui.table as LayuiModule
    const layer = layui.layer as LayuiModule
    const $ = (
      layui as unknown as { $?: (sel: string) => { html(): string | undefined } }
    ).$

    root.innerHTML = await fetchArtifactHtml(ARTIFACT_URL)
    // 对标那个 `<form action="">` 一旦真提交会刷新 iframe：关掉默认提交（证据件不动，
    // 运行时补一个属性——与 `replicaPage.ts` 的 `onsubmit="return false"` 同一处置）。
    root.querySelector('form')?.setAttribute('onsubmit', 'return false')

    const msg = (text: string): void => islandMsg(layer as unknown as Record<string, unknown>, text)

    const me: User = await fetchMe()

    /** 判据门：不持有该点时提示并**中止**（返回 false）。 */
    const requireNode = (ok: boolean, label: string): boolean => {
      if (!ok) msg(`无权限执行「${label}」`)
      return ok
    }

    // 持有 `table.render` 返回的实例而不是走 `table.reload('tableList', …)`：后者的第一参要的是
    // **不带 `#` 的 id**，传 `#id` 是**静默 no-op**（见 `pages/system-systemnotice.ts:37` 的实测）。
    const tableRef = (table.render as (o: unknown) => { reload(o?: unknown): void })({
      elem: '#tableList',
      // ⚠️ 对标 `roleauthority.js` 的 `height: 'full'` —— 本页**唯一**未复刻的取值
      //（②线执行记录 §4 第 7 条登记的已知偏离）。2026-09-23 补齐。
      height: 'full',
      url: `${API_BASE}/api/role-authorities/`,
      toolbar: '#toolbarDemo',
      // ⚠️ 对标在同一个 render 对象里写了两次 `defaultToolbar`（`roleauthority.js:25` 的 `[]`
      //    与 `:27` 的 `['filter']`）——**后写生效** ⇒ 生效值是 `['filter']`。
      defaultToolbar: ['filter'],
      page: false,
      cols: [buildCols()],
      text: { none: '暂无数据' },
      // 我们的 API 不回 layui 的 `{code,msg,count,data}` 信封，而是 `{code,msg,data:[…]}`。
      // `parseData` 把它翻成 layui 要的形状（`count` 在 `page:false` 下不参与渲染）。
      parseData: (res: { code: number; msg?: string; data?: RoleAuthorityRow[] }) => ({
        code: res.code,
        // `code !== 0` 时 layui 走 `errorView` 把文本**渲染进表格主体**（不是弹提示）；
        // 空串是 falsy ⇒ 会回落到 i18n 的 `table.dataFormatError`（一段与真实原因无关的模板文案）
        // ⇒ 这里透传信封的 `msg`，让错误文案是后端给的那句。
        msg: res.msg ?? '',
        count: res.data?.length ?? 0,
        data: res.data ?? [],
      }),
      done: () => (form.render as () => void)(),
    })

    const reload = (): void => tableRef.reload({ page: false })

    /* —— 工具栏两键（对标 `table.on('toolbar(tableList)')`，`roleauthority.js:48-60`） —— */
    ;(table.on as (e: string, cb: (obj: { event: string }) => void) => void)(
      'toolbar(tableList)',
      (obj: { event: string }) => {
        if (obj.event === 'refresh') {
          reload()
          return
        }
        if (obj.event === 'addBtn') {
          if (!requireNode(canDo(me, 'add'), '新增一个角色职位')) return
          openEditDialog(0)
        }
      },
    )

    /* —— 行内三键（对标 `table.on('tool(tableList)')`，`roleauthority.js:61-75`） —— */
    ;(table.on as (e: string, cb: (obj: { event: string; data: RoleAuthorityRow }) => void) => void)(
      'tool(tableList)',
      (obj: { event: string; data: RoleAuthorityRow }) => {
        const row = obj.data
        if (obj.event === 'submitEdit') {
          if (!requireNode(canDo(me, 'edit'), '修改权限')) return
          openEditDialog(row.id)
          return
        }
        if (obj.event === 'submitcopy') {
          if (!requireNode(canDo(me, 'copy'), '复制职位')) return
          openCopyDialog(row.id, row.name)
          return
        }
        if (obj.event === 'submitDel') {
          if (!requireNode(canDo(me, 'delete'), '删除职位')) return
          confirmDelete(row.id)
        }
      },
    )

    /** 面 2：`type:2` iframe（D-0031 / 对标 `roleauthority.js:143-155`）。 */
    function openEditDialog(roleId: number): void {
      ;(layer.open as (o: unknown) => void)({
        title: roleId > 0 ? '编辑权限' : '新增一个角色职位',
        shade: [0.3, '#333'],
        shadeClose: true,
        type: 2,
        skin: 'layui-layer-lan',
        anim: 8,
        area: ['90%', '90%'],
        content: `${PAGE_BASE}/layui-island/page.html?page=${AR_EDIT_PAGE_KEY}&role_id=${roleId}`,
        // 兜底刷新：弹层里的页保存成功后会 `postMessage`，但即使用户是直接关掉的，
        // 关一次也重拉一次列表（纯 GET，无副作用）——见 spec §8.3「保存成功后由父页刷新列表」。
        end: () => reload(),
      })
    }

    /** 面 3：`type:2` iframe（对标 `roleauthority.js:132-142` 的 `.viewRole` 委托）。 */
    const onViewRoleClick = (event: Event): void => {
      const el = (event.target as HTMLElement).closest<HTMLElement>('.viewRole')
      if (!el) return
      event.preventDefault()
      if (!requireNode(canDo(me, 'view'), '查看权限清单')) return
      const id = el.dataset.id ?? '0'
      const name = el.dataset.name ?? ''
      ;(layer.open as (o: unknown) => void)({
        // ⚠️ 角色名是租户自建的自由文本，`dataset.name` 解码后仍可能含标签；
        //    `layer.open` 的 title 按 HTML 渲染 ⇒ 过 `escapeHtml`（L19）。
        title: `${escapeHtml(name)}的权限清单`,
        shade: [0.3, '#333'],
        shadeClose: true,
        type: 2,
        skin: 'layui-layer-lan',
        anim: 8,
        area: ['90%', '85%'],
        content: `${PAGE_BASE}/layui-island/page.html?page=${AR_VIEW_PAGE_KEY}&role_id=${encodeURIComponent(id)}`,
      })
    }
    root.addEventListener('click', onViewRoleClick)

    /** 面 4：`type:1` 内联 DOM（对标 `roleauthority.js:168-193`）。 */
    function openCopyDialog(roleId: number, roleName: string): void {
      ;(layer.open as (o: unknown) => void)({
        title: '复制成新职位',
        shade: [0.3, '#333'],
        shadeClose: true,
        type: 1,
        skin: 'layui-layer-lan',
        anim: 8,
        area: ['650px', '400px'],
        content: copyDialogHtml(roleId, roleName),
        // 弹层标记是运行期插进 DOM 的，`success` 里补一次 `form.render()`（与
        // `order-orderlist.ts:841-843` 同一处置，让 layui 重新扫一遍 `.layui-form` 容器）。
        // ⚠️ 点得响**不是**靠这次 render：submit 的点击是 layui 的**文档级委托**
        // （`public/layui/layui.js` 偏移 271670 的 `on("click","*[lay-submit]",…)`），
        // 动态插入的按钮无需 render 也会派发到 `form.on('submit(copysubbtn)')`。
        success: () => (form.render as () => void)(),
      })
    }

    // 复制弹层的提交（对标 `roleauthority.js:76-124` 的 `form.on('submit(copysubbtn)')`）。
    let copyInflight = false
    ;(form.on as (e: string, cb: (data: { field: Record<string, string> }) => boolean) => void)(
      'submit(copysubbtn)',
      (data: { field: Record<string, string> }) => {
        const jobName = data.field['jobName'] ?? ''
        const invalid = validateCopyName(jobName)
        if (invalid !== null) {
          msg(invalid)
          return false
        }
        if (copyInflight) return false
        copyInflight = true
        const id = Number(data.field['currentJobId'] ?? '0')
        void copyRoleAuthority(id, { name: jobName, remark: data.field['remark'] ?? '' })
          .then(() => {
            msg('复制成功')
            reload()
            ;(layer.closeAll as (type?: string) => void)('page')
          })
          .catch((e: unknown) => msg(e instanceof Error ? e.message : '复制失败'))
          .finally(() => {
            copyInflight = false
          })
        return false
      },
    )

    /** 删除确认（文案**逐字**照抄对标 `roleauthority.js:196-198`，一行、无换行）。 */
    function confirmDelete(roleId: number): void {
      const text =
        '真的删除吗？警告：一旦删除，所有设定为该角色的系统用户将会无法访问系统，正确做法是先将该职位的所有用户职位改成其它的职位'
      ;(layer.confirm as (t: string, o: unknown, cb: (i: number) => void) => void)(
        text,
        {},
        (index: number) => {
          ;(layer.close as (i: number) => void)(index)
          void deleteRoleAuthority(roleId)
            .then(() => {
              msg('删除成功')
              reload()
            })
            .catch((e: unknown) => msg(e instanceof Error ? e.message : '删除失败'))
        },
      )
    }

    /* —— iframe 弹层的保存回执：刷新列表 + 关掉弹层 —— */
    const onMessage = (event: MessageEvent): void => {
      // ⚠️ 先判同源：不判的话任何窗口都能触发本页 `reload()` + 关掉所有 iframe 弹层。
      if (!isSameOriginMessage(event.origin, window.location.origin)) return
      const data = event.data as { type?: string } | null
      if (data?.type !== 'island:reload') return
      reload()
      // 子页（`type:2` 的 iframe 弹层）保存成功后请父页把它关掉——`layer.closeAll` 只在**宿主文档**
      // 里能找到 `.layui-layer`，子页自己调是 no-op（评审实测的机制）。
      ;(layer.closeAll as (type?: string) => void)('iframe')
    }
    window.addEventListener('message', onMessage)

    // `$` 只是把证据件里 `type="text/html"` 的模板留在 DOM 里供 layui 取用；这里显式断言
    // 它确实在，否则 `toolbar`/`toolbar:'#baroperating'` 会**静默**变成空容器（空表下看不见）。
    // ⚠️ 判据取**真值**而不是 `=== ''`：jQuery 对**空集**返回 `undefined`（不是 `''`），
    // 所以 `=== ''` 在「模板整个缺失」时反而不提示——那正是这条守卫要抓的情形。
    if ($ !== undefined) {
      const tpl = $('#toolbarDemo').html()
      const bar = $('#baroperating').html()
      if (!tpl || !bar) msg('页面模板缺失：取证据件可能没加载完整')
    }

    return () => {
      window.removeEventListener('message', onMessage)
      root.removeEventListener('click', onViewRoleClick)
      root.innerHTML = ''
    }
  },
} satisfies IslandPage
