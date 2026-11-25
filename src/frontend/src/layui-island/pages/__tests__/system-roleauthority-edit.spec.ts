// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { PermissionNodeModule, RoleAuthorityDetail } from '@/api/roleAuthority'
import {
  CAN_BE_SALES_AGENT_INPUT_ID,
  EDIT_PAGE_TITLE,
  EDIT_VALIDATION,
  SHAKE_NO_NODE_MESSAGE,
  bindTree,
  buildSavePayload,
  editFormHtml,
  selectedCodes,
  treeHtml,
} from '../system-roleauthority-edit'

/* ─────────────────────── 假 layui（只给下面那组「驱动真交互」用） ───────────────────────
 *
 * 不加载 3.9MB 的 `public/layui/layui.js`：`bootstrapIsland` 被 mock，`form.on` 把回调记进
 * Map、由测试手动触发（本仓既有写法，见 `system-setgames.spec.ts` / `user-adduser.spec.ts`）。
 * 它能抓到「事件根本没注册」，抓不到真 layui 的渲染细节——那些是真浏览器的事。
 */
const LAYUI = vi.hoisted(() => {
  const formHandlers = new Map<string, (obj: unknown) => unknown>()
  const msg = vi.fn()
  const renders: string[] = []
  const slide = vi.fn(() => ({ slideUp: vi.fn(), slideDown: vi.fn() }))
  return {
    formHandlers,
    msg,
    renders,
    slide,
    form: {
      render: vi.fn(() => {
        renders.push('render')
      }),
      on: vi.fn((event: string, cb: (obj: unknown) => unknown) => {
        formHandlers.set(event, cb)
      }),
    },
    layer: { msg },
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => ({
    form: LAYUI.form,
    layer: LAYUI.layer,
    $: LAYUI.slide,
  })),
  initIslandElements: vi.fn(),
  islandMsg: (_layer: unknown, text: string) => LAYUI.msg(text),
}))

vi.mock('@/api/auth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/auth')>()
  return { ...actual, fetchMe: vi.fn() }
})

vi.mock('@/api/roleAuthority', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/roleAuthority')>()
  return {
    ...actual,
    fetchPermissionTree: vi.fn(),
    getRoleAuthority: vi.fn(),
    createRoleAuthority: vi.fn(),
    updateRoleAuthority: vi.fn(),
  }
})

import { fetchMe } from '@/api/auth'
import {
  createRoleAuthority,
  fetchPermissionTree,
  getRoleAuthority,
  updateRoleAuthority,
} from '@/api/roleAuthority'
import page from '../system-roleauthority-edit'

const leaf = (code: string, name: string, note: string, available = false) => ({
  code, name, note, level: 1, available,
})
const MODULES: PermissionNodeModule[] = [
  {
    code: 'order', name: '订单业务', note: '订单业务管理', level: 0, available: false,
    children: [
      leaf('order.list', '订单管理列表', '订单管理列表'),
      leaf('order.add_edit', '添加和编辑订单', '添加和编辑订单', true),
    ],
  },
  {
    code: 'system', name: '系统维护', note: '管理系统设置', level: 0, available: false,
    children: [leaf('system.role_authority', '角色职位权限', '允许进入角色职位权限的管理页面', true)],
  },
]

/** 测试用的 jQuery 替身：记录调用并**真的**切 display（让展开/收起可断言）。 */
function fakeSlide() {
  const calls: string[] = []
  const fn = (el: HTMLElement) => ({
    slideUp: () => { calls.push(`up:${el.className}`); el.style.display = 'none' },
    slideDown: () => { calls.push(`down:${el.className}`); el.style.display = '' },
  })
  return { calls, fn }
}

/** 让已排队的微任务跑完（真挂载那一组用；同 `system-setgames.spec.ts` 的写法）。 */
function flush(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

const renderTree = (selected: string[]) => {
  const host = document.createElement('div')
  host.className = 'chengeRole'
  host.innerHTML = `<ul class="setNav">${treeHtml(MODULES, new Set(selected))}</ul>`
  document.body.replaceChildren(host)
  return host
}

describe('编辑弹层的表单（对标 AddEditRoleAuthority_roleId0.html）', () => {
  it('字段顺序 / name / maxlength / placeholder / lay-verify 逐条对齐', () => {
    const d = new DOMParser().parseFromString(editFormHtml(0), 'text/html')
    const labels = Array.from(d.querySelectorAll('.layui-form-label')).map((l) => l.textContent)
    // ⚠️ **这是本线的有意偏离**：对标这一页**只有 3 格**
    // （`角色职位名称 / 角色说明 / 设置其权限`，证据件 `AddEditRoleAuthority_roleId0.html` 的
    // `<form>` 段），我们在「角色说明」与「设置其权限」**之间**插了第 4 格
    // 「可作为销售客服」（`Role.can_be_sales_agent` 的界面入口）⇒ 断言由**恰好 3 个**改成
    // **恰好 4 个**（⛔ 不是放宽成 `toContain`：顺序与个数都是判据）。
    // 依据：业务方裁定（候选名单按角色标记取 ⇒ 自建角色必须有个地方勾）；
    // 登记：`docs/DECISIONS.md` 本线那条 + `docs/FRONTEND-REPLICA-STANDARD.md` §7 + 页头。
    expect(labels).toEqual(['角色职位名称', '角色说明', '可作为销售客服', '设置其权限'])

    const title = d.querySelector<HTMLInputElement>('input#title')!
    expect(title.name).toBe('title')
    expect(title.maxLength).toBe(5)
    expect(title.getAttribute('placeholder')).toBe('该角色职位的名称')
    expect(title.getAttribute('lay-verify')).toBe('required')

    const remark = d.querySelector<HTMLInputElement>('input#remark')!
    expect(remark.name).toBe('remark')
    expect(remark.maxLength).toBe(50)
    expect(remark.getAttribute('placeholder')).toBe('对该角色的作用描述')

    expect(d.querySelector('.chengeRole ul.setNav')).not.toBeNull()
  })

  it('保存键：`layui-btn-normal` + `layui-btn-lg`、居中、`lay-filter="savebtn"`', () => {
    const d = new DOMParser().parseFromString(editFormHtml(0), 'text/html')
    const btn = d.querySelector<HTMLButtonElement>('button[lay-filter="savebtn"]')!
    expect(btn.textContent?.trim()).toBe('保存')
    expect(btn.className).toContain('layui-btn-normal')
    expect(btn.className).toContain('layui-btn-lg')
    expect(btn.closest('div')?.getAttribute('style')).toContain('text-align:center')
  })

  it('隐藏域 roleId 就是入参（0 = 新增；对标 `roleId=0` 的快照里 menuId 为空 ⇒ 全不勾）', () => {
    const d0 = new DOMParser().parseFromString(editFormHtml(0), 'text/html')
    expect(d0.querySelector<HTMLInputElement>('#roleId')!.value).toBe('0')
    const d3 = new DOMParser().parseFromString(editFormHtml(3), 'text/html')
    expect(d3.querySelector<HTMLInputElement>('#roleId')!.value).toBe('3')
  })

  it('对标 `<title>` 逐字：新增或编辑角色权限', () => {
    expect(EDIT_PAGE_TITLE).toBe('新增或编辑角色权限')
  })
})

describe('两级权限树的渲染（对标 js_AddEditRoleAuthority.js:74-105）', () => {
  it('栏目行是 `<p class="columnName">` + 无子级 ul 时不留空 ul', () => {
    // ⚠️ `treeHtml` 只出 `<li>` 序列——容器是 `editFormHtml` 里那个 `<ul class="setNav">`
    // （`mount` 用 `innerHTML` 填它），所以这里必须像 `renderTree` 那样补一层，
    // 否则 `.setNav > li` 选不到任何东西。断言本身原样。
    const d = new DOMParser().parseFromString(
      `<ul class="setNav">${treeHtml(MODULES, new Set())}</ul>`,
      'text/html',
    )
    const first = d.querySelector('.setNav > li')!
    expect(first.querySelector('p.columnName')).not.toBeNull()
    expect(first.querySelector('.roleName')!.textContent).toBe('订单业务')
    expect(first.querySelector('ul.sonNav')).not.toBeNull()
  })

  /**
   * ⚠️ **这条不在 brief 的用例清单里**：上面那条的标题写了「无子级 ul 时不留空 ul」，
   * 但 `MODULES` 里两个栏目**都有**子节点 ⇒ 那个分支（`treeHtml` 的 `children.length > 0`
   * 三元）实际一条判据都没有。把它单独钉住。
   */
  it('栏目没有子节点时不出空 `<ul class="sonNav">`', () => {
    const empty: PermissionNodeModule[] = [
      { code: 'x', name: '空栏目', note: 'n', level: 0, available: false, children: [] },
    ]
    const d = new DOMParser().parseFromString(
      `<ul class="setNav">${treeHtml(empty, new Set())}</ul>`,
      'text/html',
    )
    const li = d.querySelector('.setNav > li')!
    expect(li.querySelector('.roleName')!.textContent).toBe('空栏目')
    expect(li.querySelector('ul')).toBeNull()
  })

  it('⚠️ 不渲染 `icon-paste` / `icon-file`（对标声明了但 4 个样式表里 0 命中 ⇒ 实际不显示）', () => {
    const html = treeHtml(MODULES, new Set())
    expect(html).not.toContain('icon-paste')
    expect(html).not.toContain('icon-file')
  })

  it('叶子行：`roleid` 是**节点 code**（不是对标 Menu_ID）、title 是 Note、可见文字是 `Name(Note)` 且 Note 是橙色 label', () => {
    const d = new DOMParser().parseFromString(treeHtml(MODULES, new Set()), 'text/html')
    const span = d.querySelector('ul.sonNav .roleName')!
    expect(span.getAttribute('roleid')).toBe('order.list')
    expect(span.getAttribute('title')).toBe('订单管理列表')
    expect(span.textContent).toBe('订单管理列表(订单管理列表)')
    const label = span.querySelector('label')!
    expect(label.getAttribute('style')).toBe('color:#FF5722')
  })

  it('`selected` 初始态由已勾选节点决定', () => {
    const d = new DOMParser().parseFromString(
      treeHtml(MODULES, new Set(['order.add_edit', 'order'])),
      'text/html',
    )
    const sel = Array.from(d.querySelectorAll('.roleName.selected')).map((s) => s.getAttribute('roleid'))
    expect(sel.sort()).toEqual(['order', 'order.add_edit'])
  })

  /**
   * ⚠️ **这条不在 brief 的用例清单里**，与 Task 7 评审后补的那条同型
   * （`__tests__/system-roleauthority-view.spec.ts` 的「列 templet 里来自 row 的值一律转义」）：
   * `Name` / `Note` 来自租户库、**不是常量**，不转义时一个 `"` 就能把 `title` 属性拆掉、
   * 往树里注入标记。上面 4 条渲染用例用的都是无特殊字符的语料，**抓不到**这件事。
   */
  it('树里来自租户库的值（Name / Note）一律转义，`"` 不许裸拼进属性/文本', () => {
    const evil: PermissionNodeModule[] = [
      {
        code: 'order', name: '" onmouseover="alert(1)', note: 'x', level: 0, available: false,
        children: [leaf('order.list', '<img src=x onerror=alert(1)>', '" onmouseover="alert(2)')],
      },
    ]
    const html = treeHtml(evil, new Set())
    expect(html).not.toContain('" onmouseover=')
    expect(html).not.toContain('<img src=x')
    expect(html).toContain('&quot;')
    expect(html).toContain('&lt;img')
    // 转义后仍是**合法 DOM**：`title` 属性拿回来的是原文，不是被截断的前缀。
    const d = new DOMParser().parseFromString(html, 'text/html')
    const span = d.querySelector('ul.sonNav .roleName')!
    expect(span.getAttribute('title')).toBe('" onmouseover="alert(2)')
  })
})

describe('树的点击语义（**照抄对标，不许改成联动**；spec §8.2 / 暂停 spec R10）', () => {
  it('点子节点：只切自己——父节点与其他子节点都不动', () => {
    const host = renderTree(['order.list'])
    const { fn } = fakeSlide()
    bindTree(host, { $: fn })
    const first = host.querySelector<HTMLElement>('ul.sonNav .roleName[roleid="order.add_edit"]')!
    first.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(first.classList.contains('selected')).toBe(true)
    // 兄弟没被影响
    expect(host.querySelector('.roleName[roleid="order.list"]')!.classList.contains('selected')).toBe(true)
    // 父节点没被影响
    expect(host.querySelector('p.columnName .roleName')!.classList.contains('selected')).toBe(false)
  })

  it('点父节点（未选中）：只展开 + 自己变选中；**子节点一个都不动**', () => {
    const host = renderTree(['order.list'])
    const { calls, fn } = fakeSlide()
    bindTree(host, { $: fn })
    const parent = host.querySelector<HTMLElement>('p.columnName .roleName')!
    parent.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(parent.classList.contains('selected')).toBe(true)
    expect(calls.some((c) => c.startsWith('down:'))).toBe(true)
    expect(host.querySelector('.roleName[roleid="order.list"]')!.classList.contains('selected')).toBe(true)
    expect(host.querySelector('.roleName[roleid="order.add_edit"]')!.classList.contains('selected')).toBe(false)
  })

  it('点父节点（已选中）：**清空它所有子节点的 selected** + 收起，然后自己取消选中', () => {
    const host = renderTree(['order', 'order.list', 'order.add_edit'])
    const { calls, fn } = fakeSlide()
    bindTree(host, { $: fn })
    const parent = host.querySelector<HTMLElement>('p.columnName .roleName')!
    parent.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(parent.classList.contains('selected')).toBe(false)
    expect(host.querySelector('.roleName[roleid="order.list"]')!.classList.contains('selected')).toBe(false)
    expect(host.querySelector('.roleName[roleid="order.add_edit"]')!.classList.contains('selected')).toBe(false)
    expect(calls.some((c) => c.startsWith('up:'))).toBe(true)
  })

  it('清子节点**只清自己的那一组**（第二组的子节点不受影响）', () => {
    const host = renderTree(['order', 'order.list', 'system.role_authority'])
    const { fn } = fakeSlide()
    bindTree(host, { $: fn })
    const parent = host.querySelector<HTMLElement>('p.columnName .roleName')!
    parent.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(host.querySelector('.roleName[roleid="system.role_authority"]')!.classList.contains('selected')).toBe(true)
  })

  it('叶子点两次 = 勾上再取消（toggle 语义）', () => {
    const host = renderTree([])
    const { fn } = fakeSlide()
    bindTree(host, { $: fn })
    const s = host.querySelector<HTMLElement>('.roleName[roleid="order.list"]')!
    s.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(s.classList.contains('selected')).toBe(true)
    s.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(s.classList.contains('selected')).toBe(false)
  })
})

describe('「尚未开放」只对**叶子且 available === false** 提示（D14 / spec §8.5、§5.3 追加之二）', () => {
  /**
   * 判据取自节点自身的 `available`（**栏目也答**，模板里它们是 `false`）——
   * 这样「栏目永不被提示」这条规则才真的由 `bindTree` 的 `!column` 承担，
   * 去掉 `!column` 时下面第 1 条会红。生产里 `mount` 传的是**只装叶子**的 `availableByCode`
   * （栏目取不到 = 不算「未开放」），`!column` 是第二道防线。
   */
  const NODES = [...MODULES, ...MODULES.flatMap((m) => m.children)]
  const isUnavailable = (code: string): boolean =>
    NODES.find((n) => n.code === code)?.available === false

  /** 每次点之前节点都是**未选中**态（`onLeafSelected` 只在切到选中时触发）。 */
  const bindSpy = () => {
    const host = renderTree([])
    const seen: string[] = []
    bindTree(host, {
      $: fakeSlide().fn,
      isUnavailable,
      onLeafSelected: (code) => { seen.push(code) },
    })
    return { host, seen }
  }

  it('点 `p.columnName .roleName`（栏目）⇒ `onLeafSelected` 一次都不被调', () => {
    const { host, seen } = bindSpy()
    const parent = host.querySelector<HTMLElement>('p.columnName .roleName')!
    parent.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    // 照常选中（不置灰），只是不提示。
    expect(parent.classList.contains('selected')).toBe(true)
    expect(seen).toEqual([])
  })

  it('点 `available === false` 的叶子 ⇒ 被调一次，且参数是该叶子的 `code`', () => {
    const { host, seen } = bindSpy()
    const s = host.querySelector<HTMLElement>('.roleName[roleid="order.list"]')!
    s.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(seen).toEqual(['order.list'])
  })

  it('点 `available === true` 的叶子 ⇒ 不被调', () => {
    const { host, seen } = bindSpy()
    const s = host.querySelector<HTMLElement>('.roleName[roleid="order.add_edit"]')!
    s.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(s.classList.contains('selected')).toBe(true)
    expect(seen).toEqual([])
  })
})

describe('提示与校验文案（逐字）', () => {
  it('两条校验文案照抄对标 JS', () => {
    expect(EDIT_VALIDATION.title).toBe('标题不能为空')
    expect(EDIT_VALIDATION.permissions).toBe('请至少选择一个权限')
  })

  it('`available=false` 的叶子点了提示「尚未开放」（D14 / spec §8.5）', () => {
    expect(SHAKE_NO_NODE_MESSAGE).toBe('尚未开放')
  })

  it('收集已勾选：只收 `.roleName.selected` 的 roleid（栏目与叶子都收，与对标一致）', () => {
    const host = renderTree(['order', 'order.list'])
    expect(selectedCodes(host).sort()).toEqual(['order', 'order.list'])
  })
})

/* ═══════════════════════════════════════════════════════════════════════════════════════
 * ⭐ 「可作为销售客服」那颗开关（2026-10-08 本线新增；**对标没有这一格**）
 *
 * 三条判据，各自能红在不同地方：
 *  ① **渲染**：那一格真的在模板里（`layui-form-label` 由 3 个变 4 个，见上面那条断言）；
 *  ② **载荷**：勾 / 不勾都**必须**进载荷（且键**必须在**——`PATCH` 缺席 = 保持库里的值，
 *     漏带会让"取消勾选"静默不生效）；
 *  ③ **回填**：编辑态按详情接口的 `can_be_sales_agent` 回填（少了这一条，保存会把已勾上的
 *     标记按"新建态默认"改掉）。③ 走**真挂载**（`page.mount`）——纯函数测不到"mount 忘了调它"。
 * ═══════════════════════════════════════════════════════════════════════════════════════ */

/** 带整套表单标记的根节点（`editFormHtml` + 手填的名字 / 备注 / 已勾选节点）。 */
function formRoot(selected: string[] = ['order.list']): HTMLElement {
  const root = document.createElement('div')
  root.innerHTML = editFormHtml(0)
  root.querySelector<HTMLElement>('.chengeRole .setNav')!.innerHTML = treeHtml(
    MODULES,
    new Set(selected),
  )
  root.querySelector<HTMLInputElement>('#title')!.value = '客服'
  root.querySelector<HTMLInputElement>('#remark')!.value = '说明'
  return root
}

const salesAgentBox = (root: HTMLElement): HTMLInputElement =>
  root.querySelector<HTMLInputElement>(`#${CAN_BE_SALES_AGENT_INPUT_ID}`)!

describe('⭐「可作为销售客服」那颗开关：渲染（本线有意偏离，对标没有这一格）', () => {
  it('是一颗 layui 开关（`name` / `lay-skin="switch"` / `lay-text="是|否"`），新建态默认不勾', () => {
    const d = new DOMParser().parseFromString(editFormHtml(0), 'text/html')
    const box = d.querySelector<HTMLInputElement>(`#${CAN_BE_SALES_AGENT_INPUT_ID}`)!
    expect(box).not.toBeNull()
    expect(box.getAttribute('type')).toBe('checkbox')
    expect(box.getAttribute('name')).toBe('can_be_sales_agent')
    expect(box.getAttribute('lay-skin')).toBe('switch')
    expect(box.getAttribute('lay-text')).toBe('是|否')
    expect(box.checked).toBe(false)
  })

  it('位置：夹在「角色说明」与「设置其权限」之间（第 3 个 form-item）', () => {
    const d = new DOMParser().parseFromString(editFormHtml(0), 'text/html')
    const items = Array.from(d.querySelectorAll('.layui-form-item'))
    expect(items.map((i) => i.querySelector('.layui-form-label')?.textContent)).toEqual([
      '角色职位名称',
      '角色说明',
      '可作为销售客服',
      '设置其权限',
    ])
  })
})

describe('⭐「可作为销售客服」的保存载荷（勾 / 不勾都要进载荷；键**必须在**）', () => {
  it('不勾 ⇒ `can_be_sales_agent: false`（键在、值是 false，不是"缺这个键"）', () => {
    const root = formRoot()
    expect(salesAgentBox(root).checked).toBe(false)
    expect(buildSavePayload(root)).toEqual({
      name: '客服',
      remark: '说明',
      permission_codes: ['order.list'],
      can_be_sales_agent: false,
    })
    expect(Object.keys(buildSavePayload(root))).toContain('can_be_sales_agent')
  })

  it('勾上 ⇒ `can_be_sales_agent: true`（同一个读取路径，⛔ 不是两条分支各写一遍）', () => {
    const root = formRoot()
    salesAgentBox(root).checked = true
    expect(buildSavePayload(root)).toEqual({
      name: '客服',
      remark: '说明',
      permission_codes: ['order.list'],
      can_be_sales_agent: true,
    })
  })

  it('名字仍 trim、`remark` 不 trim（本线加字段时没有顺手改掉既有口径）', () => {
    const root = formRoot()
    root.querySelector<HTMLInputElement>('#title')!.value = '  客服  '
    root.querySelector<HTMLInputElement>('#remark')!.value = '  说明  '
    const payload = buildSavePayload(root)
    expect(payload.name).toBe('客服')
    expect(payload.remark).toBe('  说明  ')
  })
})

/** 编辑态详情夹具；`can_be_sales_agent` 由每个用例覆盖。 */
const editDetail = (canBeSalesAgent: boolean): RoleAuthorityDetail => ({
  id: 3,
  name: '客服甲',
  remark: '原备注',
  permission_codes: ['order.list'],
  can_be_sales_agent: canBeSalesAgent,
})

describe('⭐「可作为销售客服」的编辑态回填 + 保存整链（真挂载 + 真点保存）', () => {
  let root: HTMLElement

  function setSearch(roleId: string): void {
    window.history.replaceState(
      {},
      '',
      `/layui-island/page.html?page=system-roleauthority-edit&role_id=${roleId}`,
    )
  }

  async function mountEditPage(roleId = '3'): Promise<void> {
    setSearch(roleId)
    await page.mount(root, {} as never)
    await flush()
    await flush()
  }

  /** 触发「保存」（layui 的 `submit(savebtn)` 回调由测试手动派发，同仓既有写法）。 */
  const clickSave = (): unknown => LAYUI.formHandlers.get('submit(savebtn)')!({})

  beforeEach(() => {
    vi.clearAllMocks()
    LAYUI.formHandlers.clear()
    LAYUI.renders.length = 0
    document.body.innerHTML = ''
    root = document.createElement('div')
    root.id = 'app'
    document.body.appendChild(root)
    // 门禁：持 `system.role_authority_edit`（走节点判据，⛔ 不用管理员短路把它绕过）。
    vi.mocked(fetchMe).mockResolvedValue({
      menu_permissions: ['system.role_authority', 'system.role_authority_edit'],
    } as never)
    vi.mocked(fetchPermissionTree).mockResolvedValue(MODULES as never)
    vi.mocked(updateRoleAuthority).mockResolvedValue({} as never)
    vi.mocked(createRoleAuthority).mockResolvedValue({} as never)
  })

  afterEach(() => {
    window.history.replaceState({}, '', '/')
  })

  it('详情 `can_be_sales_agent: true` ⇒ 那颗开关是勾上的（回填）', async () => {
    vi.mocked(getRoleAuthority).mockResolvedValue(editDetail(true) as never)
    await mountEditPage()
    expect(salesAgentBox(root)).not.toBeNull()
    expect(salesAgentBox(root).checked).toBe(true)
    // 阳性对照：别的东西也真的回填了（否则"回填"可能只是这一格碰巧成立）。
    expect(root.querySelector<HTMLInputElement>('#title')!.value).toBe('客服甲')
  })

  it('详情 `can_be_sales_agent: false` ⇒ 保持不勾（反向，防"恒勾上"的实现）', async () => {
    vi.mocked(getRoleAuthority).mockResolvedValue(editDetail(false) as never)
    await mountEditPage()
    expect(salesAgentBox(root).checked).toBe(false)
  })

  it('新建态（`role_id=0`）：不取详情、开关默认不勾（回填不会凭空发生）', async () => {
    await mountEditPage('0')
    expect(vi.mocked(getRoleAuthority)).not.toHaveBeenCalled()
    expect(salesAgentBox(root).checked).toBe(false)
  })

  it('取消勾选后保存 ⇒ PATCH 的载荷里 `can_be_sales_agent: false`（**这一格真的接上了**）', async () => {
    vi.mocked(getRoleAuthority).mockResolvedValue(editDetail(true) as never)
    await mountEditPage()
    salesAgentBox(root).click()

    expect(clickSave()).toBe(false)
    await flush()
    await flush()

    expect(vi.mocked(updateRoleAuthority)).toHaveBeenCalledWith(3, {
      name: '客服甲',
      remark: '原备注',
      permission_codes: ['order.list'],
      can_be_sales_agent: false,
    })
  })

  it('勾上后保存 ⇒ 载荷里 `can_be_sales_agent: true`（新建态走 POST）', async () => {
    await mountEditPage('0')
    root.querySelector<HTMLInputElement>('#title')!.value = '新角色'
    // 新建态树里**一个都没预勾**（详情没取），先真点一个叶子——页内那道「请至少选择一个权限」
    // 闸门在它之前，不点就根本走不到请求（那会让本用例对"载荷对不对"零判别力）。
    root
      .querySelector<HTMLElement>('.chengeRole .roleName[roleid="order.list"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    salesAgentBox(root).click()

    expect(clickSave()).toBe(false)
    await flush()
    await flush()

    expect(vi.mocked(createRoleAuthority)).toHaveBeenCalledWith({
      name: '新角色',
      remark: '',
      permission_codes: ['order.list'],
      can_be_sales_agent: true,
    })
  })
})
