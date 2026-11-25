// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import {
  AR_ACTION_NODES,
  COPY_NAME_MAXLENGTH,
  COPY_VALIDATION,
  NAME_TITLE_SUFFIX,
  buildCols,
  canDo,
  copyDialogHtml,
  isSameOriginMessage,
  validateCopyName,
  viewerCanNode,
} from '../system-roleauthority'

/** 列的标题/宽度（`Record<string, unknown>` 统一收窄）。 */
const shape = (cols: Record<string, unknown>[]) =>
  cols.map((c) => ({
    title: c.title,
    width: c.width ?? null,
    align: c.align ?? null,
    unresize: c.unresize === true,
    hasTemplet: typeof c.templet === 'function' || typeof c.templet === 'string',
    toolbar: c.toolbar ?? null,
  }))

/**
 * 去掉 `//` 行注释与块注释（逐字符扫描；字符串/模板字面量里的 `//`、`/*` 不算注释，
 * **正则字面量**里的引号同样不算）。
 *
 * 为什么要它：凡是「按源码判」的断言都必须剥注释——`toContain(字面量)` 把同一个字面量写进
 * **注释**里照样绿，等于那条兜底不存在。本文件两处按源码判的用例（`height` 那条与末尾的
 * 权限点消费方那条）都过它；末尾那条还带负向对照，把「剥注释」这件事本身也钉住。
 * 提到模块作用域是因为两个 describe 都要用（块内函数声明不会跨块可见）。
 *
 * ⚠️ **正则那一支不是洁癖，是实测出来的**：本页 `escapeAttr` 里有
 * `.replace(/"/g, '&quot;')`，上一版扫描器把正则里的那个 `"` 当成字符串开始 ⇒ 此后整份文件
 * 都被当成字符串内容，**后面的注释一条也没剥掉**。症状是「按代码判」静默退化成「按原文判」：
 * 本任务新加的 `height` 断言在**删掉代码行、只留注释里的 `height: 'full'`** 时仍然绿
 * （变异测试抓到）。所以这里补上「`/` 起正则就整段跳过」的分支，负向对照见末尾那条。
 */
function stripComments(src: string): string {
  /** `/` 能不能起一个正则字面量：看上一个**有意义**的字符（除号的前一个字符不在此集里）。 */
  const canStartRegex = (prev: string | undefined): boolean =>
    prev === undefined || '(,=:[!&|?{};+-*%~^<>'.includes(prev)

  let out = ''
  let quote: string | null = null
  /** 上一个已输出的非空白字符（`\n` 与注释都不算），只给 `canStartRegex` 用。 */
  let prev: string | undefined
  const emit = (s: string): void => {
    out += s
    const t = s.trim()
    if (t !== '') prev = t[t.length - 1]
  }

  for (let i = 0; i < src.length; i += 1) {
    const c = src[i]!
    const next = src[i + 1]
    if (quote !== null) {
      emit(c)
      if (c === '\\') {
        emit(next ?? '')
        i += 1
      } else if (c === quote) {
        quote = null
      }
      continue
    }
    if (c === '"' || c === "'" || c === '`') {
      quote = c
      emit(c)
      continue
    }
    if (c === '/' && next === '/') {
      while (i < src.length && src[i] !== '\n') i += 1
      emit('\n')
      continue
    }
    if (c === '/' && next === '*') {
      i += 2
      while (i < src.length && !(src[i] === '*' && src[i + 1] === '/')) i += 1
      i += 1
      continue
    }
    if (c === '/' && canStartRegex(prev)) {
      // 正则字面量：里面有引号也不该被当成字符串起始。找未转义的收尾 `/`（字符组里的不算），
      // 跨行（未闭合）就退回当普通 `/` 处理，避免把除号误吞成一大段。
      let j = i + 1
      let inClass = false
      for (; j < src.length; j += 1) {
        const d = src[j]!
        if (d === '\\') {
          j += 1
        } else if (d === '\n') {
          break
        } else if (d === '[') {
          inClass = true
        } else if (d === ']') {
          inClass = false
        } else if (d === '/' && !inClass) {
          break
        }
      }
      if (src[j] === '/') {
        emit(src.slice(i, j + 1))
        i = j
        continue
      }
    }
    emit(c)
  }
  return out
}

/** 本页源码路径（按本 spec 文件解析；`__dirname` 在 ESM 下不存在，用 `import.meta.url`）。 */
const PAGE_TS = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../system-roleauthority.ts',
)

/** 本页源码（剥掉注释后的**代码部分**），给「按源码判」的用例共用。 */
const pageCode = (): string => stripComments(readFileSync(PAGE_TS, 'utf8'))

describe('列表页的表格声明（对标 roleauthority.js:20-43）', () => {
  it('3 列、顺序与取值逐条对齐对标；操作列宽 300 且带行内模板', () => {
    expect(shape(buildCols())).toEqual([
      { title: '角色职位名称', width: 150, align: 'left', unresize: false, hasTemplet: true, toolbar: null },
      { title: '角色说明', width: null, align: 'left', unresize: true, hasTemplet: true, toolbar: null },
      { title: '操作', width: 300, align: null, unresize: true, hasTemplet: false, toolbar: '#baroperating' },
    ])
  })

  it('角色职位名称列渲染蓝链：色值 `#1E9FFF`、class `viewRole`、title 带对标后缀', () => {
    const cols = buildCols()
    const tmpl = cols[0]!.templet as (row: { id: number; name: string }) => string
    const html = tmpl({ id: 3, name: '客服' })
    expect(html).toContain('class="viewRole"')
    expect(html).toContain('style="color:#1E9FFF"')
    expect(html).toContain('data-id="3"')
    expect(html).toContain('data-name="客服"')
    expect(html).toContain(`title="客服${NAME_TITLE_SUFFIX}"`)
    // ⚠️ 对标的 data-name 有一个 `+` 的缺陷（roleauthority.js:30 的 `'+"`），**不复刻**。
    expect(html).not.toContain('客服+')
    // 形态**不随权限变化**：`buildCols` 不接受权限入参（判据落在点击上）。
    expect(buildCols.length).toBe(0)
  })

  it('角色说明列是对标那种无点击处理的 `<a>`（保留 title 属性）', () => {
    const cols = buildCols()
    const tmpl = cols[1]!.templet as (row: { remark: string }) => string
    const html = tmpl({ remark: '负责订单操作' })
    expect(html).toContain('href="javascript:void(0);"')
    expect(html).toContain('title="负责订单操作"')
    expect(html).toContain('>负责订单操作</a>')
  })

  it('table.render 带 height:"full"（对标 roleauthority.js 里唯一未复刻的取值）', () => {
    // ⚠️ 判据打在**页面源码的代码部分**上：`table.render` 的参数对象在 `mount` 里构造、
    // 没有导出，只能用读源码的方式钉住（与 `page-mount-order.spec.ts` 同一种网）。
    // 剥注释不可省：本页注释里也写着 `height: 'full'` 这个字面量，不剥的话**只留注释也能绿**
    //（这条由变异测试实测过：删掉代码行、保留注释，`pageCode()` 必须不再命中）。
    expect(pageCode()).toMatch(/height:\s*'full'/)
  })
})

/**
 * 弹层保存回执的同源校验（安全闸）。
 *
 * 背景（本页原有的缺陷）：`mount` 监听 `window` 的 `message` 来刷新表格并关掉 iframe 弹层，
 * 而判据只看 `data.type === 'island:reload'` ⇒ **任意窗口**（含跨源 iframe）都能
 * `postMessage({type:'island:reload'})` 触发本页 `reload()` 并把所有弹层关掉。
 *
 * 判据抽成导出的纯函数、测试直接驱动它（两个入参 → 布尔），不构造真实 `MessageEvent`：
 * 后者要造 `window` 环境且验不到判据本身。
 */
describe('弹层保存回执只认同源消息', () => {
  it('跨源 → false；同源 → true', () => {
    expect(isSameOriginMessage('http://evil.test', 'http://localhost:5173')).toBe(false)
    expect(isSameOriginMessage('http://localhost:5173', 'http://localhost:5173')).toBe(true)
    // ⚠️ 反向的**前缀攻击**也必须拒：判据是 `origin === current` 全等，不是「以本站开头」。
    // 这条防的是「以后有人把它优化成 `startsWith(host)`」那种回归。
    expect(isSameOriginMessage('http://localhost:5173.evil.test', 'http://localhost:5173')).toBe(false)
  })

  /**
   * ⚠️ **这条不在 brief 里，是自审时补的**：上面那条只驱动了纯函数，而**真正的修复**是
   * 「`onMessage` 的第一道闸用它」——只留纯函数的话，谁把 `if (!isSameOriginMessage(…)) return`
   * 删掉，上面那条**照样全绿**（E-0024 族：判据存在 ≠ 判据被用上）。这条把接线也钉住。
   */
  it('onMessage 的第一道闸就是它，且在读 `data.type` 之前（判据被用上，不只是存在）', () => {
    const code = pageCode()
    // ⚠️ **断言整条语句（含 `!` 与 `return`），不只钉子串的位置**：
    //    只钉「`isSameOriginMessage(...)` 这个串存在、且在 `data?.type` 之前」的话，
    //    把判据**取反**（`if (isSameOriginMessage(...)) return`）或掏空成
    //    `if (!isSameOriginMessage(...)) ;` **测试照样绿** —— 而那等于**放行跨源消息**
    //    到 `reload()` + 关弹层（上面纯函数那条只证明函数本身对，证明不了它**被正确地用**）。
    //    已用变异证明（取反 ⇒ 本用例红）。
    expect(code).toContain(
      'if (!isSameOriginMessage(event.origin, window.location.origin)) return',
    )
    const guard = code.indexOf('isSameOriginMessage(event.origin, window.location.origin)')
    const typeCheck = code.indexOf("data?.type !== 'island:reload'")
    expect(guard).toBeGreaterThan(-1)
    expect(typeCheck).toBeGreaterThan(-1)
    expect(guard).toBeLessThan(typeCheck)
  })
})

describe('复制弹层（面 4；对标 roleauthority.js:168-193 与证据件 `.copyRole`）', () => {
  const doc = (html: string) => new DOMParser().parseFromString(html, 'text/html')

  it('三个字段的名字 / maxlength / placeholder / 控件类型逐条对齐对标', () => {
    const d = doc(copyDialogHtml(3, '客服'))
    const cur = d.querySelector<HTMLInputElement>('#currole')!
    const job = d.querySelector<HTMLInputElement>('#jobName')!
    const remark = d.querySelector<HTMLTextAreaElement>('#remark')!
    expect(cur.value).toBe('客服')
    expect(cur.disabled).toBe(true)
    expect(job.getAttribute('placeholder')).toBe('新角色职位名称')
    expect(job.getAttribute('maxlength')).toBe('5')
    // ⚠️ 上限 = 5（对齐后端），**刻意偏离对标证据件的 `maxlength="6"`**——见 `COPY_NAME_MAXLENGTH` 的注释。
    expect(COPY_NAME_MAXLENGTH).toBe(5)
    expect(remark.tagName).toBe('TEXTAREA')
    expect(remark.getAttribute('placeholder')).toBe('请输入角色职位说明')
    expect(remark.getAttribute('maxlength')).toBe('50')
  })

  it('标签带**全角冒号**（逐字照抄），提交键文案是「提交」且宽 80px', () => {
    const d = doc(copyDialogHtml(3, '客服'))
    const labels = Array.from(d.querySelectorAll('.layui-form-label')).map((l) => l.textContent)
    expect(labels).toEqual(['当前角色职位名称：', '新角色职位名称：', '角色职位说明：'])
    const btn = d.querySelector<HTMLButtonElement>('button')!
    expect(btn.textContent?.trim()).toBe('提交')
    expect(btn.getAttribute('style')).toContain('width: 80px')
    expect(btn.getAttribute('lay-filter')).toBe('copysubbtn')
  })

  it('隐藏域 `currentJobId` 是**被复制的那条角色**的 id（对标 `$(".currentJobId").val(id)`）', () => {
    const d = doc(copyDialogHtml(42, '客服主管'))
    const hidden = d.querySelector<HTMLInputElement>('input.currentJobId')!
    expect(hidden.type).toBe('hidden')
    expect(hidden.name).toBe('currentJobId')
    expect(hidden.value).toBe('42')
  })

  /**
   * ⚠️ **这条不在 brief 里，是为自审时发现的一个真缺陷补的兜底**（改动理由见
   * `copyDialogHtml` 的注释）：layui 的提交处理器 `t.prototype.submit` 用
   * `a.parents(".layui-form").eq(0)` 找容器、再 `getValue(null, 容器)` 收字段
   * （`public/layui/layui.js` 偏移 271020 / 271091 / 253765）。
   * 提交键**没有** `.layui-form` 祖先 → 容器集合为空 → `data.field` 恒为 `{}`
   * ⇒ 弹层永远弹「请填写新职位名称！」，且不报错。
   * 这条把「提交键的 `.layui-form` 容器里确实能选到三个字段」变成会红的判据。
   */
  it('提交键带 `.layui-form` 祖先，且该容器里能收齐 `jobName`/`remark`/`currentJobId`', () => {
    const d = doc(copyDialogHtml(42, '客服主管'))
    const btn = d.querySelector<HTMLButtonElement>('button[lay-filter="copysubbtn"]')!
    const container = btn.closest('.layui-form')
    expect(container).not.toBeNull()
    for (const name of ['jobName', 'remark', 'currentJobId']) {
      expect(container!.querySelector(`[name="${name}"]`)).not.toBeNull()
    }
  })
})

describe('复制弹层的校验（判据来自生产代码，不是测试自己的副本）', () => {
  it('空串 → 请填写新职位名称！', () => {
    expect(validateCopyName('')).toBe('请填写新职位名称！')
  })

  it('1..5 通过、6 及以上 → 职位名称为2-5个字符！（上限对齐后端 5，2026-09-21 业务方裁决）', () => {
    expect(validateCopyName('甲')).toBeNull()
    expect(validateCopyName('甲'.repeat(5))).toBeNull()
    expect(validateCopyName('甲'.repeat(6))).toBe('职位名称为2-5个字符！')
  })

  it('两条文案的字面值被钉住', () => {
    expect(COPY_VALIDATION.empty).toBe('请填写新职位名称！')
    expect(COPY_VALIDATION.length).toBe('职位名称为2-5个字符！')
    expect(COPY_NAME_MAXLENGTH).toBe(5)
  })

  it('名字列的 title 后缀字面值被钉住', () => {
    expect(NAME_TITLE_SUFFIX).toBe('，点击可以查看权限清单')
  })
})

describe('5 个动作 → 权限点（与后端 ACTION_NODES 同口径）', () => {
  it('每个动作的 code 是**字面量**（改错一个字母就红）', () => {
    expect(AR_ACTION_NODES).toEqual({
      add: 'system.role_authority_edit',
      edit: 'system.role_authority_edit',
      copy: 'system.role_authority_edit',
      delete: 'system.role_authority_delete',
      view: 'system.role_authority_view',
    })
  })

  it('canDo 按动作取对应判据：持有 delete 点只放行 delete', () => {
    const onlyDelete = { menu_permissions: ['system.role_authority_delete'] }
    expect(canDo(onlyDelete, 'delete')).toBe(true)
    // ⚠️ add / edit / copy 三个动作共用 `system.role_authority_edit`（与后端 ACTION_NODES 一致，是有意的），
    //    所以只持有 `_delete` 时它们都应为 false。
    expect(canDo(onlyDelete, 'edit')).toBe(false)
    expect(canDo(onlyDelete, 'add')).toBe(false)
    expect(canDo(onlyDelete, 'copy')).toBe(false)
    expect(canDo(onlyDelete, 'view')).toBe(false)
  })

  it('持有 edit 点 ⇒ add / edit / copy 三个动作都放行（三动作共用一个点是有意的）', () => {
    const onlyEdit = { menu_permissions: ['system.role_authority_edit'] }
    expect(canDo(onlyEdit, 'add')).toBe(true)
    expect(canDo(onlyEdit, 'edit')).toBe(true)
    expect(canDo(onlyEdit, 'copy')).toBe(true)
    expect(canDo(onlyEdit, 'delete')).toBe(false)
    expect(canDo(onlyEdit, 'view')).toBe(false)
  })

  it('管理员短路（两个布尔任一为真）', () => {
    expect(canDo({ is_tenant_admin: true, menu_permissions: [] }, 'delete')).toBe(true)
    expect(canDo({ is_account_admin: true, menu_permissions: [] }, 'view')).toBe(true)
  })
})

describe('页面按钮的门禁（brief §3.3：把 4 个 available=True 的点接上前端消费方）', () => {
  const 客服 = { is_tenant_admin: false, is_account_admin: false, menu_permissions: ['order.list'] }
  const 管理员 = { is_tenant_admin: true, is_account_admin: false, menu_permissions: [] }
  const 账号管理员 = { is_tenant_admin: false, is_account_admin: true, menu_permissions: [] }

  it('持有该点 → 放行；不持有 → 拒绝', () => {
    expect(viewerCanNode({ ...客服, menu_permissions: ['system.role_authority_edit'] }, 'system.role_authority_edit')).toBe(true)
    expect(viewerCanNode(客服, 'system.role_authority_edit')).toBe(false)
  })

  it('is_tenant_admin / is_account_admin 短路为真（menu_permissions 为空也放行）', () => {
    expect(viewerCanNode(管理员, 'system.role_authority_delete')).toBe(true)
    expect(viewerCanNode(账号管理员, 'system.role_authority_view')).toBe(true)
  })

  it('menu_permissions 缺席按空集（fail-closed）', () => {
    expect(viewerCanNode({ is_tenant_admin: false, is_account_admin: false }, 'system.role_authority')).toBe(false)
  })
})

/**
 * brief §3.3 / spec §11 第 12 行的收口判据。
 *
 * ①线的 `src/tests/test_rbac_structure.py::test_b2_…` **只扫 `src/apps/` 下的 `*.py`**，
 * 所以它今天靠**后端 API 门**就已经是绿的——而 `system.role_authority` /
 * `_edit` / `_view` 三个点的 `kind` 是 **`page`**，spec §5.2 要求它的消费方是
 * 「前端菜单门 + 路由门」。这条测试把「前端确有消费方」变成会红的判据：
 * 谁把某条接线删掉，这里立刻红。
 */
describe('4 个 available=True 的权限点在前端的消费方（承重结构兜底）', () => {
  // 相对本 spec 文件（`src/layui-island/pages/__tests__/`）解析：
  // `../../../` = `src/`（`__tests__` → `pages` → `layui-island` → `src`）。
  const SOURCES: Array<[file: string, needles: string[]]> = [
    // [路由/菜单侧] system.role_authority
    ['../../../config/routePermissions.ts', ["'system-role-authority': 'system.role_authority'"]],
    ['../../../config/menu.ts', ["'system.role_authority'"]],
    // [页面侧] _edit / _view / _delete
    ['../system-roleauthority.ts', [
      "'system.role_authority_edit'",
      "'system.role_authority_view'",
      "'system.role_authority_delete'",
    ]],
    ['../system-roleauthority-edit.ts', ["'system.role_authority_edit'"]],
    ['../system-roleauthority-view.ts', ["'system.role_authority_view'"]],
  ]

  // ⚠️ **收尾时必须是会跑的 `it`，不许再退回被跳过的写法**（Task 6 建它时两个 iframe 页
  // 还不存在，只能先跳过；Task 7 / 8 建完那两个文件后，Task 8 的 Step 7 已把它恢复成
  // 会跑的用例）。跳过 = 这条兜底从不存在。
  it('每个点都在前端源码里被**代码**引用（不是注释）', () => {
    // 负向对照：写进注释里的同一个字面量必须被剥掉——否则用例名是假的。
    expect(stripComments(`// 'system.role_authority'`)).not.toContain(`'system.role_authority'`)
    // 负向对照 2：**正则字面量里的引号不能让扫描器失步**。本页 `escapeAttr` 里就有
    // `.replace(/"/g, '&quot;')`——上一版扫描器被它带偏后，后面所有注释都不再被剥
    //（`stripComments` 的注释里有实测经过；这一条把那个缺陷钉住，防它回来）。
    expect(
      stripComments(`const q = s.replace(/"'/g, '')\n// 'system.role_authority'\n`),
    ).not.toContain(`'system.role_authority'`)
    for (const [rel, needles] of SOURCES) {
      const code = stripComments(readFileSync(new URL(rel, import.meta.url), 'utf8'))
      for (const n of needles) expect(code, `${rel} 的**代码**里缺 ${n}`).toContain(n)
    }
  })
})
