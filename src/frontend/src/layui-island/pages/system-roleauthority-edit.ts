/**
 * 【新增或编辑角色权限】`layer type:2` 的 **iframe 独立页**（面 2，D-0031）。
 *
 * 由列表页 `layer.open({type:2, area:['90%','90%'],
 * content:'…?page=system-roleauthority-edit&role_id=N'})` 打开（`role_id=0` = 新增）。
 * `title` 由**父页**给（`id>0 ? '编辑权限' : '新增一个角色职位'`）。
 *
 * 取值出处（**已核**，`.tools/recon/roleauth1/`，行号按 `cat -n`）：
 * - 表单标记与页内 `<style>` → `AddEditRoleAuthority_roleId0.html` 的内联 `<style>` 与 `<form>` 段
 * - 两条校验文案 → `.tools/recon/roleauth1/js_AddEditRoleAuthority.js` **L19-30**
 * - 树的点击语义 → 同上 **L61-74**
 * - 树的渲染 → 同上 **L90-106**
 *
 * ⚠️ **树的点击语义是"父不联动子、子不影响父"**（spec §8.2 / 暂停 spec R10）：
 * 子节点只切自己；父节点「若已选中 ⇒ 清空它**所有**子节点的 selected + 收起，否则展开」，
 * 然后父节点自己 toggle。**改它 = 改对标行为**，测试会红（`__tests__/system-roleauthority-edit.spec.ts`）。
 *
 * ⚠️ `available=False` 的**叶子**点了要提示「尚未开放」（D14 / spec §8.5），但**仍照常选中**
 * （不置灰）。⚠️ **栏目的 `available` 恒为 `False` 且没有信息量**（spec §5.3 追加之二）
 * ⇒ **只看叶子**，不要拿它判断栏目。
 *
 * ## ⭐ 复刻**有意偏离**：「可作为销售客服」那颗开关（2026-10-08 本线新增）
 *
 * ⚠️ **对标这一页没有这一格**：对标的 `AddEditRoleAuthority` 表单只有「角色职位名称 /
 * 角色说明 / 设置其权限」三项 + 保存（本页 `editFormHtml` 就是照它手写的模板字面量，
 * 逐条见上面「取值出处」）。我们**在「角色说明」与「设置其权限」之间插了一整格**：
 * `<input type="checkbox" name="can_be_sales_agent" lay-skin="switch" lay-text="是|否">`。
 *
 * **为什么加**（业务方裁定）：录单页「接单销售客服」的候选名单改成**按角色的一个
 * 标记**取（`Role.can_be_sales_agent`，`apps/business/selectors.py::kefu_options`）——
 * 内置「客服」「客服主管」由种子 + 一条数据迁移置上，而**租户自建的角色**只能在这里勾。
 * 没有这一格，租户建了自建角色也**无处勾这个标记**（界面不呈现 = 功能不存在）。
 *
 * **落点与代价（如实登记，⛔ 不许照对标「修」回去）**：
 *  - 表单的 `.layui-form-label` 由 **3 个变 4 个**、顺序是
 *    `角色职位名称 / 角色说明 / 可作为销售客服 / 设置其权限`
 *    （判据：`__tests__/system-roleauthority-edit.spec.ts` 的**恰好 4 个**断言）；
 *  - 本页**不是 fetch 证据件的那类页**（对比：列表页走 `public/replica/system-roleauthority.html`）
 *    ——它是**手写模板字面量**（`editFormHtml`），所以「证据件一个字节没动」这条判据
 *    在本页**不适用**（没有证据件可动）；这里能给的等价判据是「模板里**多**了这一格、
 *    其余标记逐字未变」（同一 spec 文件的字段断言 + 上面 `labels` 那条）。
 *  - 保存载荷多一个键 `can_be_sales_agent`（**必带**：`PATCH` 缺席 = 保持库里的值，
 *    漏带会让"取消勾选"静默不生效）；编辑态由详情接口的 `can_be_sales_agent` 回填。
 *  - 已登记进 `docs/DECISIONS.md`（本线那条决策）与
 *    `docs/FRONTEND-REPLICA-STANDARD.md` §7「已知会不同」。
 */
import { fetchMe } from '@/api/auth'
import {
  createRoleAuthority,
  fetchPermissionTree,
  getRoleAuthority,
  updateRoleAuthority,
  type PermissionNodeModule,
  type RoleWritePayload,
} from '@/api/roleAuthority'
import { bootstrapIsland, islandMsg, type IslandContext, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'
// ⚠️ 跨页复用**同一份**节点判据与转义（本页要自证持有 `system.role_authority_edit`，
// 且树里的 `Name` / `Note` 来自租户库、必须转义）。**不要**在第二个文件里再写一遍。
import { escapeAttr, viewerCanNode } from './system-roleauthority'

/** 对标 `AddEditRoleAuthority_roleId0.html` 的 `<title>`（逐字）。 */
export const EDIT_PAGE_TITLE = '新增或编辑角色权限'

/** 两条校验文案（`js_AddEditRoleAuthority.js:19-30`，**逐字**）。 */
export const EDIT_VALIDATION = {
  title: '标题不能为空',
  permissions: '请至少选择一个权限',
} as const

/** 预留点被点击时的提示（D14 / spec §8.5；**不加角标**）。 */
export const SHAKE_NO_NODE_MESSAGE = '尚未开放'

/** 页面自己加的 toast 文案。 */
const MSG_SAVED = '保存成功'
const MSG_FAILED = '保存失败'
/** 保存时的在途闸门文案（对标 `re` 全局锁，`js_AddEditRoleAuthority.js:32-35`）。 */
const MSG_INFLIGHT = '正在保存，请稍候'

/** jQuery 里我们唯一用到的那两个方法（`layui.$`；测试传替身）。 */
export type SlideDeps = (el: HTMLElement) => { slideUp(): void; slideDown(): void }

/**
 * 页内标记（逐字取自对标 `AddEditRoleAuthority_roleId0.html` 的 `<form>` 段 +
 * 它的页内 `<style>`，只把隐藏域 `roleId` 参数化；`menuId` 留着是因为对标也有，
 * 我们不用它——已勾选由 `GET /api/role-authorities/<id>/` 的 `permission_codes` 给）。
 *
 * ⚠️ sprite 的 URL：对标页内写的是相对路径 `../images/icon_select.png`（该页在
 * `/System/` 下 ⇒ 解析成 `/images/icon_select.png`）。本页在 `/layui-island/page.html`，
 * 同样的相对路径会解析错，所以用 `BASE_URL` 拼绝对路径（成品件已按对标复制进
 * `public/images/icon_select.png`，14×47 / 1324 B）。
 *
 * ⚠️ 比对标多一句 `onsubmit="return false"`：对标那个 `<form action="">` 一旦真提交会刷新
 * **iframe 本身**，而我们的保存走 `form.on('submit(savebtn)')`。与 `replicaPage.ts` /
 * `system-roleauthority-view.ts:112-113` 同一处置（不改任何可见样式）。
 */
export function editFormHtml(roleId: number): string {
  return `
<style>
    .chengeRole ul {
        margin: 0 0 0 30px
    }
    .chengeRole ul li p {
            margin: 0;
        }
    .chengeRole ul li {
            list-style-type: none;
            font-size: 14px;
            line-height: 32px;
            color: #333333;
        }
    .chengeRole ul li span {
                cursor: pointer;
                padding: 0 0 0 22px;
                background-image: url(${import.meta.env.BASE_URL}images/icon_select.png);
                background-repeat: no-repeat;
                margin: 0 30px 0 0;
                background-position: 3px 4px;
                line-height: 32px;
            }
    .chengeRole ul li span.selected {
                    background-position: 3px -31px;
                }
</style>
<form class="layui-form" action="" onsubmit="return false">
    <div class="layui-form-pane" style="margin-top:10px;margin-left:10px">
        <div class="layui-form-item">
            <label class="layui-form-label">角色职位名称</label>
            <div class="layui-input-block">
                <input type="text" name="title" id="title" lay-verify="required" autocomplete="off" placeholder="该角色职位的名称" maxlength="5" class="layui-input layui-input-shadow boxshadow-success">
            </div>
        </div>
        <div class="layui-form-item">
            <label class="layui-form-label">角色说明</label>
            <div class="layui-input-block">
                <input type="text" name="remark" id="remark" lay-verify="" autocomplete="off" placeholder="对该角色的作用描述" maxlength="50" class="layui-input layui-input-shadow boxshadow-success">
            </div>
        </div>
        <div class="layui-form-item">
            <label class="layui-form-label">可作为销售客服</label>
            <div class="layui-input-block">
                <input type="checkbox" name="can_be_sales_agent" id="can_be_sales_agent" lay-skin="switch" lay-text="是|否">
            </div>
        </div>
        <div class="layui-form-item">
            <label class="layui-form-label">设置其权限</label>
            <div class="layui-input-block">
                <div class="chengeRole">
                    <ul class="setNav"></ul>
                </div>
            </div>
        </div>
        <div style="text-align:center;align-items:center">
            <button type="button" class="layui-btn   layui-btn-normal layui-btn-lg" lay-submit="" lay-filter="savebtn">保存</button>
        </div>
    </div>
    <input type="hidden" name="roleId" id="roleId" value="${roleId}" />
    <input type="hidden" name="menuId" id="menuId" />
</form>`
}

/**
 * 两级树的标记（对标 `js_AddEditRoleAuthority.js:90-106`）。
 *
 * - `roleid` 用**节点 code**（对标用 `Menu_ID`）——`benchmark_id` 只用于溯源，不参与判据。
 * - ⚠️ **不渲染 `<i class='icon-paste'>` / `<i class='icon-file'>`**（对标 L93 / L99 有）：
 *   它们在对标加载的 4 个样式表里 **0 命中**（暂停 spec R9）⇒ 实际不产生任何视觉。
 *   照抄会引入对标页面上**不存在**的元素。
 * - ⚠️ **每个来自租户库的值都过 `escapeAttr`**（`module.name` / `leaf.name` / `leaf.note`）：
 *   它们不是常量，一个 `"` 就能拆掉属性、往树里注入标记。Task 7 的权限清单页就是
 *   靠评审补上这一处的（`__tests__/system-roleauthority-view.spec.ts` 有同型用例）。
 * - **不出 `<ul class='sonNav'>` 当栏目没有子节点**（对标 L94-102 那层
 *   `if (MenuChildrenList.length > 0)`）；`<li>` 总是闭合——对标在
 *   `MenuChildrenList == null` 时漏了 `</li>`（L103），那是畸形标记，不复刻。
 */
export function treeHtml(modules: PermissionNodeModule[], selected: ReadonlySet<string>): string {
  const cls = (code: string): string => (selected.has(code) ? 'roleName selected' : 'roleName')
  return modules
    .map((m) => {
      const children =
        m.children.length > 0
          ? `<ul class="sonNav">${m.children
              .map(
                (c) =>
                  `<li><span class="${cls(c.code)}" roleid="${escapeAttr(c.code)}" title="${escapeAttr(c.note)}">${escapeAttr(c.name)}<label style="color:#FF5722">(${escapeAttr(c.note)})</label></span></li>`,
              )
              .join('')}</ul>`
          : ''
      return `<li><p class="columnName"><span class="${cls(m.code)}" roleid="${escapeAttr(m.code)}">${escapeAttr(m.name)}</span></p>${children}</li>`
    })
    .join('')
}

/** 收集已勾选的节点 code（对标 `$('.chengeRole .roleName.selected').each(...)`，L24-26）。 */
export function selectedCodes(root: HTMLElement): string[] {
  return Array.from(root.querySelectorAll<HTMLElement>('.chengeRole .roleName.selected'))
    .map((el) => el.getAttribute('roleid') ?? '')
    .filter((c) => c !== '')
}

/**
 * 绑定点击语义（对标 `js_AddEditRoleAuthority.js:61-74`）。
 *
 * 用**事件委托**挂在 `.chengeRole` 上，与对标的 `$('.chengeRole').on({click:…}, '.roleName')` 同构。
 * 展开/收起交给注入的 `$`（生产是 `layui.$` 的 `slideUp/slideDown`，测试是替身），
 * 这样"到底收起了没有"可以被断言，而不是只能靠肉眼看动画。
 *
 * ⚠️ **`root` 自己就是 `.chengeRole` 时也要认**：`querySelector('.chengeRole')`
 * 只找**后代**、永不匹配元素自身，而调用方有两种形态——生产传的是页面根 `#app`
 * （`.chengeRole` 是它的后代），测试直接传 `.chengeRole` 那个容器。
 * 只写 `querySelector` 时后一种会让本函数**静默早退**（点击全无反应）。
 *
 * `onLeafSelected` 回调在**尚未开放的叶子**（`isUnavailable(code) === true` 且不是栏目）
 * 被切到"选中"时调用一次（用于「尚未开放」提示）。判据放在本函数里而不是调用方的回调里，
 * 是为了让「只对叶子且 `available === false` 提示」这条规则**能被单测直接驱动**。
 */
export function bindTree(
  root: HTMLElement,
  deps: {
    $: SlideDeps
    /**
     * 该节点是否**尚未开放**（`available === false`）。只在**叶子**上被问。
     * ⚠️ 缺省 = 不问 = 不提示：调用方漏传时不会把每个叶子都误报成「尚未开放」。
     */
    isUnavailable?: (code: string) => boolean
    /** 被切到"选中"的**未开放叶子**的 code 回调一次（本页据此发「尚未开放」提示）。 */
    onLeafSelected?: (code: string) => void
  },
): void {
  const host = root.classList.contains('chengeRole')
    ? root
    : root.querySelector<HTMLElement>('.chengeRole')
  if (!host) return
  host.addEventListener('click', (event: Event) => {
    const span = (event.target as HTMLElement).closest<HTMLElement>('.roleName')
    if (!span || !host.contains(span)) return
    // 叶子不是 `.columnName` 的后代（那是一个 `<p>`、与叶子的 `<ul>` 是兄弟），
    // 所以 `closest` 只有对栏目行才非空——与对标的 `$this.parents('.columnName').length > 0` 同判据。
    const column = span.closest<HTMLElement>('.columnName')
    if (column) {
      // 父节点：已选中 ⇒ 清空**它自己那一组**的所有子节点 + 收起；否则展开。然后自己 toggle。
      const nav = column.nextElementSibling as HTMLElement | null
      if (nav && nav.classList.contains('sonNav')) {
        if (span.classList.contains('selected')) {
          nav.querySelectorAll<HTMLElement>('.roleName').forEach((el) => el.classList.remove('selected'))
          deps.$(nav).slideUp()
        } else {
          deps.$(nav).slideDown()
        }
      }
    }
    span.classList.toggle('selected')
    // 叶子被**选中**且**尚未开放**时提示；栏目永不给
    // （栏目的 available 恒为 False 且没有信息量，spec §5.3 追加之二）。
    const code = span.getAttribute('roleid') ?? ''
    if (!column && span.classList.contains('selected') && deps.isUnavailable?.(code) === true) {
      deps.onLeafSelected?.(code)
    }
  })
}

/** 从 `window.location.search` 取 `role_id`（`0` = 新增）。 */
function roleIdFromSearch(search: string): number {
  const raw = new URLSearchParams(search).get('role_id') ?? '0'
  return /^\d+$/.test(raw) ? Number(raw) : 0
}

/** 那颗「可作为销售客服」开关的 `id`（模板与读取**共用一处**，⛔ 不各写一遍字面量）。 */
export const CAN_BE_SALES_AGENT_INPUT_ID = 'can_be_sales_agent'

/**
 * 从表单读出**保存载荷**（`createRoleAuthority` / `updateRoleAuthority` 同一份）。
 *
 * ⚠️ 抽成导出函数（本线 2026-10-08）：载荷里多了一个**对标没有的**字段
 * （`can_be_sales_agent`，见文件头那条偏离登记），而"勾了没勾到底有没有进载荷"这件事
 * 在 `mount` 里是**测不到**的（`mount` 需要整个 layui 环境）⇒ 判据落在这里，
 * 由 `__tests__/system-roleauthority-edit.spec.ts` 直接驱动（勾 / 不勾 / 回填三条）。
 *
 * ⚠️ `can_be_sales_agent` **每次都带**（`!== null` 即 `.checked`）：`PATCH` 的缺席语义是
 * "保持库里的值"，漏带会让**取消勾选静默不生效**（用户看到保存成功、下次打开还是勾着）。
 * ⚠️ 名字仍然 `trim()`（原实现如此，两条校验文案的判据也建立在这上面），`remark` 不 trim。
 */
export function buildSavePayload(root: HTMLElement): RoleWritePayload {
  return {
    name: root.querySelector<HTMLInputElement>('#title')!.value.trim(),
    remark: root.querySelector<HTMLInputElement>('#remark')!.value,
    permission_codes: selectedCodes(root),
    can_be_sales_agent:
      root.querySelector<HTMLInputElement>(`#${CAN_BE_SALES_AGENT_INPUT_ID}`)?.checked === true,
  }
}

export default {
  mount: async (root: HTMLElement, _ctx: IslandContext) => {
    document.title = EDIT_PAGE_TITLE
    const layui: LayuiNamespace = await bootstrapIsland(['form', 'table', 'layer'])
    const form = layui.form as LayuiModule
    const layer = layui.layer as LayuiModule
    // `LayuiNamespace.$` 是 `unknown`（`layui/loader.ts:23`）⇒ 局部收窄成我们用到的那两个方法。
    const $ = (layui as unknown as { $: SlideDeps }).$

    const roleId = roleIdFromSearch(window.location.search)
    const msg = (text: string): void => islandMsg(layer as unknown as Record<string, unknown>, text)

    // ⚠️ **本页自己的门禁**（与列表页按钮上那道门**两道**）：`type:2` 的 iframe 可 deep-link，
    // 直接打开 `?page=system-roleauthority-edit&role_id=3` 会绕过列表页。
    // 判据复用列表页导出的 `viewerCanNode`（只有一处实现）。
    // ⚠️ 注入表单标记必须排在门禁**之后**：`layui-island/page.ts` 的
    // `loader().then((mod) => mod.default.mount(root, …))` **丢掉 mount 的返回值**，
    // 拒绝分支返回的 cleanup 永远不会被调用 ⇒ 先注入就会让被拒的 deep-link 看到表单壳。
    const me = await fetchMe()
    if (!viewerCanNode(me, 'system.role_authority_edit')) {
      msg('无权限编辑角色职位')
      // `root` 此刻仍是空的（上面没注入过任何标记），这里没有可清的东西。
      return () => {}
    }
    root.innerHTML = editFormHtml(roleId)

    // 并发取：目录树（所有节点）+ 详情（已勾选 code，仅编辑态）。
    const [modules, detail] = await Promise.all([
      fetchPermissionTree(),
      roleId > 0 ? getRoleAuthority(roleId) : Promise.resolve(null),
    ])

    const availableByCode = new Map<string, boolean>()
    for (const m of modules) for (const c of m.children) availableByCode.set(c.code, c.available)

    const selected = new Set(detail?.permission_codes ?? [])
    const setNav = root.querySelector<HTMLElement>('.chengeRole .setNav')!
    setNav.innerHTML = treeHtml(modules, selected)
    if (detail) {
      root.querySelector<HTMLInputElement>('#title')!.value = detail.name
      root.querySelector<HTMLInputElement>('#remark')!.value = detail.remark
      // ⭐ 「可作为销售客服」的**回填**（本线 2026-10-08）：详情接口回的 `can_be_sales_agent`
      // 是唯一来源。⚠️ 必须排在下面那一次 `form.render()` **之前**：`lay-skin="switch"` 的
      // 皮肤是 layui 在 render 那一刻按 `checked` 画出来的（写晚了开关外观与真实值不一致，
      // 表现为"看着是关的、提交却是开的"）。
      root.querySelector<HTMLInputElement>(`#${CAN_BE_SALES_AGENT_INPUT_ID}`)!.checked =
        detail.can_be_sales_agent
    }
    ;(form.render as () => void)()

    bindTree(root, {
      $,
      // ⚠️ **只看叶子**：栏目的 available 恒为 False、没有信息量（spec §5.3 追加之二）。
      // 照常选中（不置灰）——只是给一个"勾了暂时不生效"的反馈（D14 / spec §8.5）。
      isUnavailable: (code) => availableByCode.get(code) === false,
      onLeafSelected: () => msg(SHAKE_NO_NODE_MESSAGE),
    })

    let inflight = false
    // `LayuiModule` 的索引签名是 `unknown`（`layui/loader.ts:27`）⇒ 直接 `form.on(...)` 是 TS18046，
    // 按仓库惯用写法局部收窄（同 `pages/system-roleauthority.ts:365-366`）。
    ;(form.on as (e: string, cb: () => boolean) => void)('submit(savebtn)', () => {
      const payload = buildSavePayload(root)
      if (payload.name === '') {
        msg(EDIT_VALIDATION.title)
        return false
      }
      if (payload.permission_codes.length < 1) {
        msg(EDIT_VALIDATION.permissions)
        return false
      }
      if (inflight) {
        msg(MSG_INFLIGHT)
        return false
      }
      inflight = true
      const task =
        roleId > 0
          ? updateRoleAuthority(roleId, payload)
          : createRoleAuthority(payload)
      void task
        .then(() => {
          msg(MSG_SAVED)
          // 对标 `parent.tools.reloadOrderData()`（`js_AddEditRoleAuthority.js:48`）的对应实现：
          // 本页是**列表页 iframe 里的 iframe**，`window.parent` 就是列表页所在窗口。
          // ⚠️ **关掉 iframe 弹层由父页在收到本条消息后执行**（列表页
          // `system-roleauthority.ts` 的 `onMessage`）：`layer` 的关闭接口走 `layui.$`、
          // 只在**当前文档**里找 `.layui-layer`，而弹层属于列表页那个文档 ⇒ 子页自己调是 no-op。
          window.parent.postMessage({ type: 'island:reload' }, window.location.origin)
        })
        .catch((e: unknown) => {
          // 后端把领域层的预期失败（重名 / 未知 code / 最后管理员保护）翻成 200 + 业务码 1000，
          // `unwrap()` 抛 `BizError(code,msg)` ⇒ 这里 `e.message` 就是后端文案。
          // 未预期的错误（网络 / 5xx）也走这里，只是文案是 axios 的。
          msg(e instanceof Error ? e.message : MSG_FAILED)
        })
        .finally(() => {
          inflight = false
        })
      return false
    })

    return () => {
      root.innerHTML = ''
    }
  },
} satisfies IslandPage
