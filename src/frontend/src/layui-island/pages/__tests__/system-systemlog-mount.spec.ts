// @vitest-environment jsdom
/**
 * 系统跟踪日志页**挂载层**的接线测试（layui 换成可控替身，见 `order-orderlist.spec.ts` 的同一手法）。
 *
 * 为什么值得写：`mountSystemLogPage` 的接线（端点、参数名、下拉数据源、筛选→线上参数）都在闭包里，
 * 纯函数测不到；只断言源码文本是同义反复。这里挂载整页后断言**真实出口**：
 *  - `table.render` 的入参（`url` / `request` 名 / `page` 复刻值 / `where` 默认区间 / 8 列）；
 *  - `formSelects.config` 的入参（**相关人员的数据源必须是 `/api/audit/actors/`**，不是 `/api/accounts/`）；
 *  - 「游戏筛选」的 `<option>` 来自我方 `fetchGames`（对标的 13 个租户游戏名必须消失）；
 *  - 点搜索 → `table.reload` 的 `where` 就是**发出去的筛选参数**（`log_type=0` 被丢、`actor` 逗号编码）。
 */
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const EVIDENCE = readFileSync(
  path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    '../../../../public/replica/system-systemlog.html',
  ),
  'utf8',
)

/** layui 的可控替身。`vi.hoisted` 是因为 `vi.mock` 的工厂会被提升到 import 之前。 */
const LAYUI = vi.hoisted(() => {
  const formHandlers = new Map<string, (...args: unknown[]) => unknown>()
  const tableHandlers = new Map<string, (...args: unknown[]) => unknown>()
  /**
   * `form.val(filter)` 的替身**真的去读 DOM**（与 layui 的行为同形），这样「默认日期区间」
   * 这类页面自己写进输入框的值能被筛选读取路径看到，而不是让替身凭空返回一份配置。
   */
  const readForm = (filter?: string): Record<string, string> => {
    const form =
      document.querySelector<HTMLFormElement>(`form[lay-filter="${filter}"]`) ??
      document.querySelector<HTMLFormElement>('form.layui-form')
    const out: Record<string, string> = {}
    form?.querySelectorAll<HTMLInputElement | HTMLSelectElement>('[name]').forEach((el) => {
      const name = el.getAttribute('name')
      if (name) out[name] = el.value
    })
    return out
  }
  return {
    formHandlers,
    tableHandlers,
    readForm,
    form: {
      render: vi.fn(),
      on: (e: string, cb: (...args: unknown[]) => unknown) => formHandlers.set(e, cb),
      val: vi.fn((filter?: string) => readForm(filter)),
    },
    table: {
      render: vi.fn(),
      on: (e: string, cb: (...args: unknown[]) => unknown) => tableHandlers.set(e, cb),
      reload: vi.fn(),
    },
    layer: { msg: vi.fn() },
    laydate: { render: vi.fn() },
    formSelects: {
      render: vi.fn(),
      data: vi.fn(),
      value: vi.fn((): unknown => ''),
      config: vi.fn(),
    },
    element: { init: vi.fn() },
  }
})

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return {
    ...actual,
    bootstrapIsland: (async () => LAYUI) as unknown as typeof actual.bootstrapIsland,
    initIslandElements: vi.fn(),
  }
})

vi.mock('@/layui-island/config-replica', () => ({
  loadConfigArtifact: async () => EVIDENCE,
}))

vi.mock('@/layui/vendor', () => ({
  initFormSelects: vi.fn(),
  getFormSelects: () => LAYUI.formSelects,
}))

vi.mock('@/api/config', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/config')>()
  return {
    ...actual,
    fetchGames: vi.fn(async () => [
      { id: 1, name: '原神', sort_order: 1, is_active: true, created_at: '' },
    ]),
  }
})

import { mountSystemLogPage } from '../system-systemlog'

async function mountPage(): Promise<{ root: HTMLElement; unmount: () => void }> {
  const root = document.createElement('div')
  document.body.appendChild(root)
  const unmount = await mountSystemLogPage(root, { onNavigate: vi.fn() })
  return { root, unmount }
}

function renderOpts(): Record<string, unknown> {
  expect(LAYUI.table.render).toHaveBeenCalledTimes(1)
  return LAYUI.table.render.mock.calls[0]![0] as Record<string, unknown>
}

/** 最近一次 `table.reload(id, opts)` 的入参（`[id, opts]`）；没有就当场红。 */
function lastReload(): [string, Record<string, unknown>] {
  const calls = LAYUI.table.reload.mock.calls
  expect(calls.length).toBeGreaterThan(0)
  return calls[calls.length - 1] as [string, Record<string, unknown>]
}

beforeEach(() => {
  vi.clearAllMocks()
  // 事件处理器表也要清：挂在 `Map` 上，`vi.clearAllMocks()` 清不掉。
  // 不清的话，**上一次挂载**注册的处理器会留在表里 ⇒ 「页面是否注册了某事件」这类断言
  // 会靠残留假通过（本轮排序事件是新加的，必须真的证明它被注册）。
  LAYUI.formHandlers.clear()
  LAYUI.tableHandlers.clear()
  LAYUI.formSelects.value.mockReturnValue('')
  document.body.innerHTML = ''
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('system-systemlog 挂载：表格与两个下拉的取数接线', () => {
  it('主表走 GET /api/audit/logs/，分页参数名与 limit 是复刻值，8 列照旧', async () => {
    await mountPage()
    const opts = renderOpts()
    expect(opts.url).toBe('/api/audit/logs/')
    expect(opts.method).toBe('get')
    expect(opts.request).toEqual({ pageName: 'page', limitName: 'page_size' })
    expect(opts.page).toEqual({ limit: 30, limits: [10, 20, 30, 40, 50, 100] })
    expect(opts.cellMinWidth).toBe(150)
    expect(opts.height).toBe('full-80')
    expect(opts.toolbar).toBe('#toolbarDemo')
    expect(opts.defaultToolbar).toEqual(['filter'])
    expect((opts.cols as unknown[][])[0]).toHaveLength(8)
  })

  it('首屏 where 就是筛选栏里显示的默认日期区间（不是全表最新 30 条）', async () => {
    const { root } = await mountPage()
    const sDate = root.querySelector<HTMLInputElement>('input#sDate')!.value
    const eDate = root.querySelector<HTMLInputElement>('input#eDate')!.value
    expect(sDate).toMatch(/^\d{4}-\d{2}-\d{2} 00:00:00$/)
    expect(eDate).toMatch(/^\d{4}-\d{2}-\d{2} 23:59:59$/)
    expect(renderOpts().where).toEqual({ stime: sDate, etime: eDate })
  })

  it('相关人员的数据源是 /api/audit/actors/（`key` → 昵称 `display_name`，提交值仍是 `username`）', async () => {
    await mountPage()
    expect(LAYUI.formSelects.config).toHaveBeenCalledWith('recUser', {
      searchUrl: '/api/audit/actors/',
      searchName: 'key',
      // 显示昵称、提交账号名（spec §11.2）：`keyName` 改了，`keyVal` 没改。
      keyName: 'display_name',
      keyVal: 'username',
    })
  })

  it('相关人员保持对标的单选：xm-select-radio 留在标记里（R29，不摘）', async () => {
    const { root } = await mountPage()
    expect(root.querySelector('select#recUser')!.hasAttribute('xm-select-radio')).toBe(true)
  })

  it('游戏筛选的 option 换成我方游戏；对标的 13 个租户游戏名一个都不留', async () => {
    const { root } = await mountPage()
    const options = Array.from(root.querySelectorAll<HTMLOptionElement>('select#gameName option'))
    expect(options.map((o) => [o.value, o.textContent])).toEqual([
      ['', '全部游戏'],
      ['原神', '原神'],
    ])
    expect(root.querySelector('select#gameName')!.textContent).not.toContain('崩坏：星穹铁道')
  })

  it('关掉 layui 的本地假排序（autoSort === false），而列上的复刻值 sort: true 保留', async () => {
    await mountPage()
    // 开着 autoSort（vendored layui 的默认 true）时，点箭头只会重排当前页那 30 行——
    // 这条断言就是 Task O 的落点：本地假排序必须关掉。
    expect(renderOpts().autoSort).toBe(false)
    const cols = (renderOpts().cols as Record<string, unknown>[][])[0]!
    const timeCol = cols.find((c) => c.title === '操作时间')!
    // 复刻值不动（`system-systemlog.spec.ts` 也单独钉着它）。
    expect(timeCol.sort).toBe(true)
  })
})

describe('system-systemlog 挂载：点搜索 → 发出去的筛选参数', () => {
  it('六个筛选（含 log_type=0 丢、actor 单值）打在 table.reload 的 where 上', async () => {
    const { root } = await mountPage()
    const set = (selector: string, value: string): void => {
      ;(root.querySelector(selector) as HTMLInputElement | HTMLSelectElement).value = value
    }
    set('input[name=sDate]', '2026-09-20 00:00:00')
    set('input[name=eDate]', '2026-09-21 23:59:59')
    set('select[name=logType]', '0')
    set('select[name=gameName]', '原神')
    set('input[name=key1]', '改价')
    set('input[name=key2]', '20260921000000000001')
    LAYUI.formSelects.value.mockReturnValue('客服甲')

    const submit = LAYUI.formHandlers.get('submit(seachBtn)')
    expect(submit).toBeTypeOf('function')
    submit!()

    expect(LAYUI.table.reload).toHaveBeenCalledWith('tableList', {
      where: {
        stime: '2026-09-20 00:00:00',
        etime: '2026-09-21 23:59:59',
        game_name: '原神',
        actor: '客服甲',
        order_no: '20260921000000000001',
        key: '改价',
      },
      page: { curr: 1 },
    })
  })

  it('日志分类选真分类时照传（`20` 不被「全部」那支吃掉）', async () => {
    const { root } = await mountPage()
    ;(root.querySelector('select[name=logType]') as HTMLSelectElement).value = '20'
    LAYUI.formHandlers.get('submit(seachBtn)')!()
    expect(
      (LAYUI.table.reload.mock.calls[0]![1] as { where: Record<string, unknown> }).where.log_type,
    ).toBe(20)
  })

  it('工具栏「刷新列表」用当前筛选重取（回第 1 页）', async () => {
    const { root } = await mountPage()
    const sDate = root.querySelector<HTMLInputElement>('input#sDate')!.value
    const toolbar = LAYUI.tableHandlers.get('toolbar(tableList)')
    expect(toolbar).toBeTypeOf('function')
    toolbar!({ event: 'refresh' })
    expect(LAYUI.table.reload).toHaveBeenCalledWith('tableList', {
      where: { stime: sDate, etime: root.querySelector<HTMLInputElement>('input#eDate')!.value },
      page: { curr: 1 },
    })
  })

  it('读筛选值用 valStr（账号名）而不是 nameStr（昵称）——否则筛选恒空', async () => {
    // `keyName=display_name` 之后 `nameStr` 回的是**昵称**，而 `actor__in` 要的是**账号名**：
    // 拿昵称去匹配会恒查空（页面不报错、只是没结果）。判据打在真实出口的**第二个实参**上。
    await mountPage()
    LAYUI.formSelects.value.mockReturnValue('')
    const submit = LAYUI.formHandlers.get('submit(seachBtn)')
    expect(submit).toBeTypeOf('function')
    submit!()
    expect(LAYUI.formSelects.value).toHaveBeenCalledWith('recUser', 'valStr')
  })
})

/**
 * Task O 的接线：layui 的 `sort(<filter>)` 事件 → 服务端 `ordering` → `table.reload`。
 * 判据打在**真实出口** `table.reload` 的 `where` 上（不是源码文本、不是内部状态）。
 */
describe('system-systemlog 挂载：点「操作时间」箭头 → 重新取数并带上服务端 ordering', () => {
  /** 挂载后点排序：返回 `table.reload` 那一次的 `where`。 */
  async function sortAndWhere(
    obj: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    await mountPage()
    const sort = LAYUI.tableHandlers.get('sort(tableList)')
    expect(sort).toBeTypeOf('function')
    sort!(obj)
    const opts = lastReload()[1] as {
      where: Record<string, unknown>
      page: { curr: number }
    }
    // 排序与搜索同语义：**回第 1 页**。
    expect(opts.page).toEqual({ curr: 1 })
    return opts.where
  }

  it('升序带 `ordering=created_at`（旧在前）', async () => {
    const where = await sortAndWhere({ field: 'createTime', type: 'asc' })
    expect(where.ordering).toBe('created_at')
  })

  it('降序带 `ordering=-created_at`（新在前）', async () => {
    const where = await sortAndWhere({ field: 'createTime', type: 'desc' })
    expect(where.ordering).toBe('-created_at')
  })

  it('两向都带上筛选栏里的默认日期区间（排序不丢筛选）', async () => {
    await mountPage()
    const sDate = document.querySelector<HTMLInputElement>('input#sDate')!.value
    const eDate = document.querySelector<HTMLInputElement>('input#eDate')!.value
    const sort = LAYUI.tableHandlers.get('sort(tableList)')!
    sort({ field: 'createTime', type: 'asc' })
    const where = (lastReload()[1] as { where: Record<string, unknown> })
      .where
    expect(where).toEqual({ stime: sDate, etime: eDate, ordering: 'created_at' })
  })

  it('第三次点击（layui 报 `type: null` = 取消排序）回到**不带** ordering 的默认序', async () => {
    await mountPage()
    const sort = LAYUI.tableHandlers.get('sort(tableList)')!
    sort({ field: 'createTime', type: 'desc' })
    sort({ field: 'createTime', type: null })
    const where = (lastReload()[1] as { where: Record<string, unknown> })
      .where
    // 字面钉住键集合：多一个 `ordering: undefined`/空串都不行（线上形状必须与今天一致）。
    expect(Object.keys(where).sort()).toEqual(['etime', 'stime'])
    expect(where).not.toHaveProperty('ordering')
  })

  it('不点排序时请求里没有 ordering（搜索路径的线上形状不变）', async () => {
    await mountPage()
    LAYUI.formHandlers.get('submit(seachBtn)')!()
    const where = (lastReload()[1] as { where: Record<string, unknown> })
      .where
    expect(Object.keys(where).sort()).toEqual(['etime', 'stime'])
    expect(where).not.toHaveProperty('ordering')
  })
})
