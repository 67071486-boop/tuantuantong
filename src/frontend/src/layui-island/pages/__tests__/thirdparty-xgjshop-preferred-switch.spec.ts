// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * D-0328：【第三方平台 →「闲管家店铺对接」】页上那块
 * 「接单大厅优选订单」开关（key = `hall_preferred_orders`，默认 **Y** = 存量行为）。
 *
 * 本文件证五件事（与前两个开关 spec 分工明确：`…unpaid-guard.spec.ts` 证待付款块、
 * `…hall-sim-switch.spec.ts` 证模拟单块；本文件只证优选这块）：
 *
 * 1. **勾选态来自后端**（`GET /api/business/system-switches/` 按 key 找到的那一行），
 *    ⛔ 不是写死的默认；后端没这一行 ⇒ 响亮报错、不画控件（fail-closed）；
 * 2. 点保存 ⇒ **真发一次 PATCH**：断**真实出口**的方法、URL、body（`@/api/http` 的
 *    `request()` 被换掉，`api/systemSwitches.ts` / `api/auth.ts` 跑真实现）；
 * 3. **fail-closed 渲染**：没有读（`system.settings`）或写（`system.edit_settings`）节点
 *    ⇒ 控件一个字节都不进 DOM，且**不发**那条注定 403 的 `GET`（D-0154 / E-0098）；
 * 4. 标记里有标题与说明（两向语义：开 = 角标 + 完成数闸 + 强制押金；关 = 不标优选、
 *    高价单按普通规则接）；
 * 5. 保存失败 ⇒ 勾选态回滚到后端最后已知值。
 *
 * 手法照 `thirdparty-xgjshop-hall-sim-switch.spec.ts`（`vi.mock` 掉 `bootstrapIsland`
 * 与数据出口，只断言入参与 DOM）。⚠️ 桩打在 `@/api/http`：那是**被查找的那个模块**
 * （E-0029）——页面与两个 api 模块都从它取 `request`。
 */

/** layui 的可控替身（`form.on` 收处理器，`layer.msg` 是提示的出口）。 */
const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  const formHandlers = new Map<string, () => unknown>()
  const tableRenders: Array<Record<string, unknown>> = []
  return {
    msg,
    formHandlers,
    tableRenders,
    form: {
      render: vi.fn(),
      on: vi.fn((event: string, cb: () => unknown) => {
        formHandlers.set(event, cb)
      }),
    },
    table: {
      render: vi.fn((opts: Record<string, unknown>) => {
        tableRenders.push(opts)
      }),
      reloadData: vi.fn(),
      on: vi.fn(),
    },
    layer: {
      msg,
      open: vi.fn(),
      closeAll: vi.fn(),
      confirm: vi.fn(),
      alert: vi.fn(),
    },
    /** 本页只用 `$` 把弹层节点交给 `layer.open`。 */
    $: (el: Element) => el,
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => ({
    form: LAYUI.form,
    table: LAYUI.table,
    layer: LAYUI.layer,
    $: LAYUI.$,
  })),
  initIslandElements: vi.fn(),
  islandMsg: (_layer: unknown, text: string) => LAYUI.msg(text),
}))

vi.mock('@/api/xgjBindings', () => ({
  fetchXgjBindings: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 200 })),
  fetchXgjShops: vi.fn(async () => ({ items: [] })),
  createXgjBinding: vi.fn(),
  updateXgjBinding: vi.fn(),
  deleteXgjBinding: vi.fn(),
  testXgjConnection: vi.fn(),
  queryXgjOrderDetail: vi.fn(),
  syncXgjShops: vi.fn(),
}))

vi.mock('@/api/http', () => ({ request: vi.fn() }))

import { request } from '@/api/http'
import page, {
  HALL_PREFERRED_BLOCK_CLASS,
  HALL_PREFERRED_BLOCK_SELECTOR,
  HALL_PREFERRED_HOST_SELECTOR,
  HALL_PREFERRED_INPUT_NAME,
  HALL_PREFERRED_SAVE_FILTER,
  HALL_PREFERRED_SWITCH_KEY,
  SWITCHES_READ_NODE,
  SWITCHES_WRITE_NODE,
} from '../thirdparty-xgjshop'

const mockedRequest = vi.mocked(request)
/** 开关列表端点（`api/systemSwitches.ts` 的集合路径；PATCH 跟在它后面加 key）。 */
const SWITCHES_URL = '/api/business/system-switches/'
const ME_URL = '/api/auth/me'

/** 身份替身：`is_tenant_admin` / `menu_permissions` 是 `canNode` 的两个入参。 */
function meOf(permissions: string[], tenantAdmin = false) {
  return {
    id: 1,
    username: 'preferred-admin',
    display_name: '优选管理员',
    identity_plane: 'tenant',
    is_tenant_admin: tenantAdmin,
    roles: [],
    menu_permissions: permissions,
  }
}

type StubOptions = {
  /** 优选开关的后端现值（另两块给死值——本文件不证它们）。 */
  value?: 'Y' | 'N'
  me?: ReturnType<typeof meOf> | null
  switchLoadThrows?: boolean
  saveThrows?: boolean
  /** 后端开关列表里**不给**优选那一条（前后端对不上的形状）。 */
  rowMissing?: boolean
}

/** 当前这一段用例的桩配置（`stubRequest` 写它、`request` 替身读它）。 */
let stub: StubOptions = {}

/** 三行的开关列表替身（另两块给死值；优选块的值由 `stub.value` 控制，默认 Y）。 */
function switchList() {
  const items: Array<Record<string, unknown>> = [
    {
      key: 'reject_unpaid_xgj_order',
      name: '录单识别闲管家订单时，待付款状态直接拒绝',
      value: 'N',
      default: 'Y',
      replica_sk: '',
    },
    {
      key: 'hall_simulated_orders',
      name: '接单大厅生成模拟单（演示卡）',
      value: 'N',
      default: 'N',
      replica_sk: '',
    },
    // ⛔ 曾有第四行 `hall_upstream_throw`（D-0329），2026-10-29 整块撤除（业务方拍板
    // 「同步订单是一整个功能」：随发版自动生效、不设租户总闸）⇒ 桩里也不再给这一行。
  ]
  if (!stub.rowMissing) {
    items.push({
      key: HALL_PREFERRED_SWITCH_KEY,
      name: '接单大厅优选订单（高价标记与接单闸）',
      value: stub.value ?? 'Y',
      default: 'Y',
      replica_sk: '',
    })
  }
  return { items }
}

/**
 * 把 `request()` 换成按 URL 分流的替身。
 * ⚠️ 收的是**真实调用参数** ⇒ 下面断 URL / 方法 / body 就是断真实出口。
 */
function stubRequest(options: StubOptions = {}): void {
  stub = options
  mockedRequest.mockImplementation((async (config: {
    method?: string
    url?: string
    data?: unknown
  }) => {
    if (config.url === ME_URL) {
      if (stub.me === null) throw new Error('未登录')
      const user = stub.me ?? meOf([SWITCHES_READ_NODE, SWITCHES_WRITE_NODE])
      return { user }
    }
    if (config.url === SWITCHES_URL) {
      if (stub.switchLoadThrows) throw new Error('取开关失败')
      return switchList()
    }
    if (config.url?.startsWith(SWITCHES_URL)) {
      if (stub.saveThrows) throw new Error('没有权限执行该操作')
      const value = (config.data as { value: 'Y' | 'N' }).value
      return { key: HALL_PREFERRED_SWITCH_KEY, name: '接单大厅优选订单（高价标记与接单闸）', value }
    }
    throw new Error(`本用例没桩这个 URL：${config.url}`)
  }) as never)
}

function flush(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

function checkboxOf(root: HTMLElement): HTMLInputElement | null {
  return root.querySelector<HTMLInputElement>(
    `${HALL_PREFERRED_BLOCK_SELECTOR} input[name="${HALL_PREFERRED_INPUT_NAME}"]`,
  )
}

/** 打到开关集合端点的那几次调用（GET 读值 / PATCH 保存都在这里）。 */
function switchCalls(): Array<{ method?: string; url?: string; data?: unknown }> {
  return mockedRequest.mock.calls
    .map((call) => call[0] as { method?: string; url?: string; data?: unknown })
    .filter((config) => (config.url ?? '').startsWith(SWITCHES_URL))
}

async function mount(options: StubOptions = {}): Promise<HTMLElement> {
  stubRequest(options)
  const root = document.createElement('div')
  document.body.appendChild(root)
  await page.mount(root)
  await flush()
  return root
}

describe('闲管家店铺对接页 ·「接单大厅优选订单」开关（D-0328）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    LAYUI.formHandlers.clear()
    LAYUI.tableRenders.length = 0
    document.body.innerHTML = ''
    stub = {}
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  // ── 判据 ①：勾选态来自后端 ──────────────────────────────────────
  it('① 后端给 Y（注册表默认）⇒ 开关勾上，各块开关各 GET 一次集合端点', async () => {
    const root = await mount({ value: 'Y' })
    const box = checkboxOf(root)
    expect(box, '控件应当画出来（这个身份两个节点都有）').not.toBeNull()
    expect(box!.checked).toBe(true)
    const gets = switchCalls().filter((c) => c.method === 'get')
    // 三块开关各自 mount（D-0329 撤了 throw 块后回到三块）⇒ 集合端点被 GET 3 次；URL 都逐字对。
    expect(gets).toHaveLength(3)
    for (const call of gets) expect(call.url).toBe(SWITCHES_URL)
  })

  it('① 后端给 N ⇒ 不勾（业务方「把优选关掉」之后的样子）', async () => {
    const root = await mount({ value: 'N' })
    expect(checkboxOf(root)!.checked).toBe(false)
  })

  it('① 后端列表里没有这一条 ⇒ 不画控件，并把对不上的事说出来（⛔ 不静默画成关）', async () => {
    const root = await mount({ rowMissing: true })
    expect(root.querySelector(HALL_PREFERRED_BLOCK_SELECTOR)).toBeNull()
    expect(LAYUI.msg).toHaveBeenCalledWith('后端开关列表里没有「接单大厅优选订单」那一条')
  })

  // ── 判据 ②：保存真发一次 PATCH（真实出口：方法 + URL + body）────
  it('② 点保存真发一次 PATCH：断 URL 与请求体，且两个方向都跟着勾选态走', async () => {
    const root = await mount({ value: 'Y' })
    const box = checkboxOf(root)!

    // 关掉 ⇒ PATCH `N`（业务方指令的方向）
    box.checked = false
    LAYUI.formHandlers.get(`submit(${HALL_PREFERRED_SAVE_FILTER})`)!()
    await flush()
    let patches = switchCalls().filter((c) => c.method === 'patch')
    expect(patches).toHaveLength(1)
    expect(patches[0]).toEqual({
      method: 'patch',
      url: `${SWITCHES_URL}${HALL_PREFERRED_SWITCH_KEY}/`,
      data: { value: 'N' },
    })
    expect(LAYUI.msg).toHaveBeenCalledWith('已保存')

    // 再打开 ⇒ PATCH `Y`（body 随勾选态走，⛔ 不是恒发一个常量）。
    box.checked = true
    LAYUI.formHandlers.get(`submit(${HALL_PREFERRED_SAVE_FILTER})`)!()
    await flush()
    patches = switchCalls().filter((c) => c.method === 'patch')
    expect(patches).toHaveLength(2)
    expect(patches[1]).toEqual({
      method: 'patch',
      url: `${SWITCHES_URL}${HALL_PREFERRED_SWITCH_KEY}/`,
      data: { value: 'Y' },
    })
  })

  it('② 保存失败 ⇒ 勾选态回滚到后端最后已知值，并把失败说出来', async () => {
    const root = await mount({ value: 'Y', saveThrows: true })
    const box = checkboxOf(root)!
    box.checked = false
    LAYUI.formHandlers.get(`submit(${HALL_PREFERRED_SAVE_FILTER})`)!()
    await flush()
    // 不回滚 = 把"没保存上"伪装成"已保存"（勾选态是这块唯一的反馈面）
    expect(box.checked).toBe(true)
    expect(LAYUI.msg).toHaveBeenCalledWith('没有权限执行该操作')
  })

  // ── 判据 ③：fail-closed 渲染（没权限 ⇒ 一个字节都不画）──────────
  it('③ 只有读权限 ⇒ 控件不渲染，且不发那条注定 403 的 GET', async () => {
    const root = await mount({ me: meOf([SWITCHES_READ_NODE]) })
    expect(root.querySelector(HALL_PREFERRED_BLOCK_SELECTOR)).toBeNull()
    expect(root.querySelector(HALL_PREFERRED_HOST_SELECTOR)!.innerHTML).toBe('')
    expect(switchCalls()).toHaveLength(0)
  })

  it('③ 只有写权限 ⇒ 控件同样不渲染（勾选态读不出来，画出来就是撒谎）', async () => {
    const root = await mount({ me: meOf([SWITCHES_WRITE_NODE]) })
    expect(root.querySelector(HALL_PREFERRED_BLOCK_SELECTOR)).toBeNull()
    expect(switchCalls()).toHaveLength(0)
  })

  it('③ 身份取不到（401 / 网络）⇒ 按"没有节点"处理，不画控件', async () => {
    const root = await mount({ me: null })
    expect(root.querySelector(HALL_PREFERRED_BLOCK_SELECTOR)).toBeNull()
    expect(switchCalls()).toHaveLength(0)
  })

  it('③ 读值失败 ⇒ 一个字节都不画（勾选态不明的开关不许上屏），并把失败说出来', async () => {
    const root = await mount({ switchLoadThrows: true })
    expect(root.querySelector(HALL_PREFERRED_BLOCK_SELECTOR)).toBeNull()
    expect(root.querySelector(HALL_PREFERRED_HOST_SELECTOR)!.innerHTML).toBe('')
    expect(LAYUI.msg).toHaveBeenCalledWith('取开关失败')
  })

  // ── 判据 ④：标记里有标题与说明（两向语义）──────────────────────
  it('④ 标记里同时有 class 名与 name，且带标题与两向语义的说明', async () => {
    const root = await mount({ value: 'Y' })
    const host = root.querySelector(HALL_PREFERRED_HOST_SELECTOR)!
    expect(host.innerHTML).toContain(`class="${HALL_PREFERRED_BLOCK_CLASS} layui-form"`)
    expect(host.innerHTML).toContain(`name="${HALL_PREFERRED_INPUT_NAME}"`)
    expect(host.innerHTML).toContain(`lay-filter="${HALL_PREFERRED_SAVE_FILTER}"`)
    expect(host.innerHTML).toContain('接单大厅优选订单')
    // 开向：角标 + 完成数闸 + 强制押金。
    expect(host.innerHTML).toContain('优选</b>角标')
    expect(host.innerHTML).toContain('<b>完成过订单</b>')
    expect(host.innerHTML).toContain('<b>强制冻结押金</b>')
    // 关向：不标优选、高价单按普通规则接（押金总闸照常）。
    expect(host.innerHTML).toContain('大厅不再标优选')
    expect(host.innerHTML).toContain('高价单按普通规则接')
  })

  // ── 判据 ⑤：本页原有的内容不受影响 ─────────────────────────────
  it('⑤ 加了这一块之后，本页原有的引导 / 两张表照旧（两张表各 render 过一次）', async () => {
    const root = await mount({ value: 'Y' })
    expect(root.innerHTML).toContain('四步接好闲管家')
    expect(LAYUI.tableRenders).toHaveLength(2)
  })
})
