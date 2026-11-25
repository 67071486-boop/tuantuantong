/**
 * 【角色职位权限清单】`layer type:2` 的 **iframe 独立页**（面 3，D-0031）。
 *
 * 由列表页 `layer.open({type:2, area:['90%','85%'], content:'…?page=system-roleauthority-view&role_id=N'})`
 * 打开；`title` 由**父页**给（`<角色名>的权限清单`），本页负责 body 与自己的 `<title>`。
 *
 * 取值出处（逐条）：
 * - 5 列 → `.tools/recon/roleauth1/js_RoleAuthorityView.js` **L26-57**（`cols` 数组）
 * - 标记与页内 `<style>` → `.tools/recon/roleauth1/RoleAuthorityView_roleId3.html`（L52-89）
 * - 数据 → `GET /api/role-authorities/<id>/permissions/`
 *   （`apps/rbac/selectors.py::role_permission_rows`）
 *
 * ⚠️ **顶级节点的「所属栏目」是字符串 `无`**（`selectors.py:22` 直接给的），
 * 本页**不做**任何空值兜底——照原样渲染。
 */
import { fetchMe } from '@/api/auth'
// ⚠️ 只 import 类型：数据由 layui 的 `table.render({url})` 自己取（对标 `js_RoleAuthorityView.js:22` 同理），
// 再引一份 axios 版 `fetchRolePermissionRows` 会是没有消费方的死 import——`pages/system-roleauthority.ts:28-29`
// 对 `listRoleAuthorities` 已有同一处置（`pnpm lint` 的 no-unused-vars 会红）。
import type { RolePermissionRow } from '@/api/roleAuthority'
import { bootstrapIsland, islandMsg, type IslandContext, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'
// ⚠️ 跨页复用**同一份**节点判据（列表页那个模块只有常量与纯函数，import 它无副作用）。
import { escapeAttr, viewerCanNode } from './system-roleauthority'

/** 对标 `RoleAuthorityView_roleId3.html` 的 `<title>`（逐字）。 */
export const VIEW_PAGE_TITLE = '角色职位权限清单'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

/** 对标三元表达式 `res.menuLevel == 0 ? "一级" : "二级"`（**不是数字**）。 */
export function levelLabel(level: number): string {
  return level === 0 ? '一级' : '二级'
}

/**
 * 5 列（对标 `js_RoleAuthorityView.js:26-57`）。
 *
 * ⚠️ 后四列的 templet 也照对标出 `<a style="">` 外壳，**不是**直接出纯文本：
 * 对标 `layui.css` 有全局独立规则 `a{color:#333;text-decoration:none}`（对 `a` 的**直接声明
 * 胜过继承**），而单元格纯文本继承的是 `.layui-table{…background-color:#fff;color:#5f5f5f}`。
 * 两者的字色（51,51,51 vs 95,95,95）不同，且外壳还带 `title` 的 hover 提示 ⇒ 照对标出外壳。
 * 第一列对标就是蓝链 `#1E9FFF`；五个 templet 都是无点击处理的 `<a href="javascript:void(0);">`。
 */
export function buildViewCols(): Record<string, unknown>[] {
  return [
    {
      field: 'name',
      title: '拥有权限名称',
      width: 150,
      align: 'left',
      fixed: 'left',
      unresize: true,
      templet: (row: RolePermissionRow): string =>
        `<a href="javascript:void(0);" class="" style="color:#1E9FFF" title="${escapeAttr(row.name)}">${escapeAttr(row.name)}</a>`,
    },
    {
      field: 'level',
      title: '级别',
      width: 70,
      align: 'left',
      unresize: true,
      templet: (row: RolePermissionRow): string => {
        const label = levelLabel(row.level)
        return `<a href="javascript:void(0);" style="" title="${label}">${label}</a>`
      },
    },
    {
      field: 'parent_name',
      title: '所属栏目',
      width: 150,
      align: 'left',
      unresize: true,
      templet: (row: RolePermissionRow): string =>
        `<a href="javascript:void(0);" style="" title="${escapeAttr(row.parent_name)}">${escapeAttr(row.parent_name)}</a>`,
    },
    {
      field: 'note',
      title: '权限说明',
      align: 'left',
      unresize: true,
      templet: (row: RolePermissionRow): string =>
        `<a href="javascript:void(0);" style="" title="${escapeAttr(row.note)}">${escapeAttr(row.note)}</a>`,
    },
    {
      field: 'order_by',
      title: '排序',
      width: 80,
      align: 'center',
      unresize: true,
      templet: (row: RolePermissionRow): string => {
        const orderBy = escapeAttr(String(row.order_by))
        return `<a href="javascript:void(0);" style="" title="${orderBy}">${orderBy}</a>`
      },
    },
  ]
}

/**
 * 页内标记（`<form>` 段逐字取自对标 `RoleAuthorityView_roleId3.html`；
 * 页内 `<style>` 的**值**按对标取——对标页内还有一条 `.lableb`，
 * 那条走 `ISLAND_OVERRIDES` 的全局覆盖（`layui/island.ts:25,51`），不在本页重搬；
 * 本函数只把隐藏域 `roleId` 的值参数化，并按下一段补上 `onsubmit="return false"`）。
 *
 * ⚠️ 为什么页内要自带 `.layui-table-cell{height:40px;line-height:40px;padding:0 5px}`：
 * 那是**页内局部**值（`RoleAuthorityView_roleId3.html` L52-63 的 `<style>`），
 * **不在** `ISLAND_OVERRIDES` 里（`layui/island.ts:22` 明确写了它已被搬回**人员管理页**）。
 * 对标这一页自己是带的 ⇒ 我们必须带：不带就会生效 vendored `layui.css` 的
 * `.layui-table-cell{height:38px;line-height:28px;padding:6px 15px}`，与本页对标的
 * `40px/40px/0 5px` 是两组不同的值。
 *
 * 对标的 `<form action="">` 一旦真提交会刷新 iframe：这里加 `onsubmit="return false"`
 * 关掉默认提交（与 `replicaPage.ts` / `system-roleauthority.ts:226` 同一处置）。
 */
export function viewTableHtml(roleId: number): string {
  return `
<style type="text/css">
    .layui-table-cell {
        height: 40px;
        line-height: 40px;
        padding: 0 5px;
        position: relative;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        box-sizing: border-box;
        font-size: 12px;
    }
</style>
<form class="layui-form" action="" onsubmit="return false">
    <div class="" style="">
        <table class="layui-hide" id="tableList" lay-filter="tableList"></table>
        <script type="text/html" id="toolbarDemo">
            <div class="layui-btn-container">
                <button class="layui-btn layui-btn-xs" lay-event="refresh" type="button" title="刷新当前数据"><i class="layui-icon">&#xe669;</i>刷新列表</button>
            </div>
        </script>
    </div>
    <input type="hidden" name="roleId" id="roleId" value="${roleId}" />
</form>`
}

/** 从 `window.location.search` 取 `role_id`（父页打开 iframe 时带上的参数）。 */
function roleIdFromSearch(search: string): number {
  const raw = new URLSearchParams(search).get('role_id') ?? '0'
  return /^\d+$/.test(raw) ? Number(raw) : 0
}

export default {
  mount: async (root: HTMLElement, _ctx: IslandContext) => {
    document.title = VIEW_PAGE_TITLE
    const layui: LayuiNamespace = await bootstrapIsland(['form', 'table', 'layer'])
    const form = layui.form as LayuiModule
    const table = layui.table as LayuiModule
    const layer = layui.layer as LayuiModule

    const roleId = roleIdFromSearch(window.location.search)

    // ⚠️ **本页自己的门禁**：`type:2` 的 iframe 是**可 deep-link** 的
    // （D10 把"各自可 deep-link"写成了这一面的价值），所以不能只靠列表页按钮上的那道门。
    // 判据复用列表页导出的 `viewerCanNode`（**只有一处实现**，避免第二套口径漂移）。
    const me = await fetchMe()
    if (!viewerCanNode(me, 'system.role_authority_view')) {
      islandMsg(layer as unknown as Record<string, unknown>, '无权限查看权限清单')
      // 门禁**在渲染之前** ⇒ 此刻 root 仍是空的，没有要清的东西。
      // （`layui-island/page.ts` 丢弃 `mount` 的返回值，所以就算写了 cleanup 也不会被调用——
      //  这正是「先渲染后门禁」会漏出表单壳/表壳的原因，同 `system-roleauthority-edit.ts`。）
      return () => {}
    }

    root.innerHTML = viewTableHtml(roleId)

    const tableRef = (table.render as (o: unknown) => { reload(o?: unknown): void })({
      elem: '#tableList',
      // ⚠️ 对标 `height:'full-20'`（`js_RoleAuthorityView.js:21`）—— 逐字照抄。
      height: 'full-20',
      url: `${API_BASE}/api/role-authorities/${roleId}/permissions/`,
      toolbar: '#toolbarDemo',
      // ⚠️ 对标在同一个 render 对象里写了两次 `defaultToolbar`（`:23` 的 `[]` 与 `:25` 的
      //    `['filter']`）——**后写生效** ⇒ 生效值是 `['filter']`。
      defaultToolbar: ['filter'],
      page: false,
      cols: [buildViewCols()],
      text: { none: '暂无数据' },
      // 我们的 API 不回 layui 的 `{code,msg,count,data}` 信封，而是 `{code,msg,data:[…]}`。
      // `parseData` 把它翻成 layui 要的形状（`count` 在 `page:false` 下不参与渲染）。
      parseData: (res: { code: number; msg?: string; data?: RolePermissionRow[] }) => ({
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

    // ⚠️ `table.on` 不在本工程 `LayuiModule` 类型里（`layui/loader.ts:27` 是
    // `Record<string, unknown> & ((...args) => unknown)`）⇒ 按仓库惯用写法局部收窄
    // （同 `pages/system-roleauthority.ts:265`）。
    ;(table.on as (e: string, cb: (obj: { event: string }) => void) => void)(
      'toolbar(tableList)',
      (obj: { event: string }) => {
        if (obj.event === 'refresh') tableRef.reload({ page: false })
      },
    )

    return () => {
      root.innerHTML = ''
    }
  },
} satisfies IslandPage
