// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * D-0296：【第三方平台 →「闲管家店铺对接」】页上那块
 * 「大厅优选模拟单」开关（key = `hall_simulated_orders`，默认 N）。
 *
 * 本文件证四件事（⛔ 与既有的 `thirdparty-xgjshop-unpaid-guard.spec.ts` 分工明确：
 * 那个文件证待付款那块——它已于同一天同步了「两块各自 mount」的新形状；本文件只证模拟单这块）：
 *
 * 1. **勾选态来自后端**（`GET /api/business/system-switches/` 按 key 找到的那一行），⛔ 不是写死的默认；
 * 2. 点保存 ⇒ **真发一次 PATCH**：断**真实出口**的三个要素——方法、URL、body
 *    （`@/api/http` 的 `request()` 被换掉，`api/systemSwitches.ts` / `api/auth.ts` **跑真实现**）；
 * 3. **fail-closed 渲染**：没有读（`system.settings`）或写（`system.edit_settings`）节点
 *    ⇒ 控件一个字节都不进 DOM，且**不发**那条注定 403 的 `GET`（D-0154 / E-0098）；
 * 4. 标记里有标题与说明（业务方给的文案：标题「大厅优选模拟单」、说明「开启后每款游戏自动挂
 *    2~3 张模拟优选单，仅在大厅展示」）。
 *
 * 手法照 `thirdparty-xgjshop-unpaid-guard.spec.ts`（`vi.mock` 掉 `bootstrapIsland` 与数据出口，
 * 只断言入参与 DOM）。⚠️ 桩打在 `@/api/http`：那是**被查找的那个模块**（E-0029）——页面与
 * 两个 api 模块都从它取 `request`；打在 `@/api/systemSwitches` 上就只能证"包装函数被调了"，
 * 证不到 URL 与 body。
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
import { fetchXgjBindings } from '@/api/xgjBindings'
import page, {
  HALL_SIM_BLOCK_CLASS,
  HALL_SIM_BLOCK_SELECTOR,
  HALL_SIM_HOST_SELECTOR,
  HALL_SIM_INPUT_NAME,
  HALL_SIM_SAVE_FILTER,
  HALL_SIM_SWITCH_KEY,
  SWITCHES_READ_NODE,
  SWITCHES_WRITE_NODE,
  canManageSwitchBlock,
} from '../thirdparty-xgjshop'

const mockedRequest = vi.mocked(request)
/** 开关列表端点（`api/systemSwitches.ts` 的集合路径；PATCH 跟在它后面加 key）。 */
const SWITCHES_URL = '/api/business/system-switches/'
const ME_URL = '/api/auth/me'

/** 身份替身：`is_tenant_admin` / `menu_permissions` 是 `canNode` 的两个入参。 */
function meOf(permissions: string[], tenantAdmin = false) {
  return {
    id: 1,
    username: 'hall-sim-admin',
    display_name: '大厅管理员',
    identity_plane: 'tenant',
    is_tenant_admin: tenantAdmin,
    roles: [],
    menu_permissions: permissions,
  }
}

type StubOptions = {
  /** 模拟单开关的后端现值（另一块（待付款）给死 'N'——本文件不证它）。 */
  value?: 'Y' | 'N'
  me?: ReturnType<typeof meOf> | null
  switchLoadThrows?: boolean
  saveThrows?: boolean
}

/** 当前这一段用例的桩配置（`stubRequest` 写它、`request` 替身读它）。 */
let stub: StubOptions = {}

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
      return {
        items: [
          {
            key: 'reject_unpaid_xgj_order',
            name: '录单识别闲管家订单时，待付款状态直接拒绝',
            value: 'N',
            default: 'Y',
            replica_sk: '',
          },
          {
            key: HALL_SIM_SWITCH_KEY,
            name: '接单大厅生成模拟单（演示卡）',
            value: stub.value ?? 'N',
            default: 'N',
            replica_sk: '',
          },
          {
            // ⚠️（D-0328）起本页有**第三块**开关（「接单大厅优选订单」），各块各自
            // mount 各自查值 ⇒ 桩必须把各行都给全（判据面见
            // `thirdparty-xgjshop-preferred-switch.spec.ts`）。
            key: 'hall_preferred_orders',
            name: '接单大厅优选订单（高价标记与接单闸）',
            value: 'Y',
            default: 'Y',
            replica_sk: '',
          },
          // ⛔ 曾有第四行 `hall_upstream_throw`（D-0329），2026-10-29 整块撤除（业务方拍板
          // 「同步订单是一整个功能」：随发版自动生效、不设租户总闸）⇒ 桩里也不再给这一行。
        ],
      }
    }
    if (config.url?.startsWith(SWITCHES_URL)) {
      if (stub.saveThrows) throw new Error('没有权限执行该操作')
      const value = (config.data as { value: 'Y' | 'N' }).value
      return { key: HALL_SIM_SWITCH_KEY, name: '接单大厅生成模拟单（演示卡）', value }
    }
    throw new Error(`本用例没桩这个 URL：${config.url}`)
  }) as never)
}

function flush(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

function checkboxOf(root: HTMLElement): HTMLInputElement | null {
  return root.querySelector<HTMLInputElement>(
    `${HALL_SIM_BLOCK_SELECTOR} input[name="${HALL_SIM_INPUT_NAME}"]`,
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

describe('闲管家店铺对接页 ·「大厅优选模拟单」开关（D-0296）', () => {
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
  it('① 后端给 Y ⇒ 开关勾上，且 GET 真打在开关集合端点上', async () => {
    const root = await mount({ value: 'Y' })
    const box = checkboxOf(root)
    expect(box, '控件应当画出来（这个身份两个节点都有）').not.toBeNull()
    expect(box!.checked).toBe(true)
    const gets = switchCalls().filter((c) => c.method === 'get')
    // 各块开关各自 mount（D-0328 起加了优选块；D-0329 加的 throw 块已于同日撤除）
    // ⇒ 集合端点被 GET 3 次；URL 都逐字对。
    expect(gets).toHaveLength(3)
    expect(gets[0]!.url).toBe(SWITCHES_URL)
    expect(gets[1]!.url).toBe(SWITCHES_URL)
    expect(gets[2]!.url).toBe(SWITCHES_URL)
  })

  it('① 后端给 N（注册表默认）⇒ 不勾（防"恒勾上"——这个开关默认必须是关）', async () => {
    const root = await mount({ value: 'N' })
    expect(checkboxOf(root)!.checked).toBe(false)
  })

  // ── 判据 ②：保存真发一次 PATCH（真实出口：方法 + URL + body）────
  it('② 点保存真发一次 PATCH：断 URL 与请求体，且两个方向都跟着勾选态走', async () => {
    const root = await mount({ value: 'N' })
    const box = checkboxOf(root)!

    // 打开 ⇒ PATCH `Y`
    box.checked = true
    LAYUI.formHandlers.get(`submit(${HALL_SIM_SAVE_FILTER})`)!()
    await flush()
    let patches = switchCalls().filter((c) => c.method === 'patch')
    expect(patches).toHaveLength(1)
    expect(patches[0]).toEqual({
      method: 'patch',
      url: `${SWITCHES_URL}${HALL_SIM_SWITCH_KEY}/`,
      data: { value: 'Y' },
    })
    expect(LAYUI.msg).toHaveBeenCalledWith('已保存')

    // 再关掉 ⇒ PATCH `N`（body 随勾选态走，⛔ 不是恒发一个常量）。
    box.checked = false
    LAYUI.formHandlers.get(`submit(${HALL_SIM_SAVE_FILTER})`)!()
    await flush()
    patches = switchCalls().filter((c) => c.method === 'patch')
    expect(patches).toHaveLength(2)
    expect(patches[1]).toEqual({
      method: 'patch',
      url: `${SWITCHES_URL}${HALL_SIM_SWITCH_KEY}/`,
      data: { value: 'N' },
    })
  })

  it('② 保存成功后勾选态以**后端回的那一行**为准', async () => {
    stubRequest({ value: 'N' })
    // 让 PATCH 返回一个与发出值不同的值：勾选态必须跟着响应走。
    mockedRequest.mockImplementation((async (config: {
      method?: string
      url?: string
      data?: unknown
    }) => {
      if (config.url === ME_URL) return { user: meOf([SWITCHES_READ_NODE, SWITCHES_WRITE_NODE]) }
      if (config.url === SWITCHES_URL) {
        return {
          items: [
            { key: 'reject_unpaid_xgj_order', name: 'x', value: 'N', default: 'Y', replica_sk: '' },
            { key: HALL_SIM_SWITCH_KEY, name: 'x', value: 'N', default: 'N', replica_sk: '' },
          ],
        }
      }
      if (config.url?.startsWith(SWITCHES_URL)) {
        return { key: HALL_SIM_SWITCH_KEY, name: 'x', value: 'Y' }
      }
      throw new Error(`本用例没桩这个 URL：${config.url}`)
    }) as never)
    const root = document.createElement('div')
    document.body.appendChild(root)
    await page.mount(root)
    await flush()
    const box = checkboxOf(root)!
    expect(box.checked).toBe(false)
    box.checked = true
    LAYUI.formHandlers.get(`submit(${HALL_SIM_SAVE_FILTER})`)!()
    await flush()
    // 发出的是 Y，后端回 Y ⇒ 勾上（以响应为准；这里两端一致，防的是"回 N 却保持勾上"）。
    expect(box.checked).toBe(true)
  })

  it('② 保存失败 ⇒ 勾选态回滚到后端最后已知值，并把失败说出来', async () => {
    const root = await mount({ value: 'N', saveThrows: true })
    const box = checkboxOf(root)!
    box.checked = true
    LAYUI.formHandlers.get(`submit(${HALL_SIM_SAVE_FILTER})`)!()
    await flush()
    // 不回滚 = 把"没保存上"伪装成"已保存"（勾选态是这块唯一的反馈面）
    expect(box.checked).toBe(false)
    expect(LAYUI.msg).toHaveBeenCalledWith('没有权限执行该操作')
  })

  // ── 判据 ③：fail-closed 渲染（没权限 ⇒ 一个字节都不画）──────────
  it('③ 只有读权限 ⇒ 控件不渲染，且不发那条注定 403 的 GET', async () => {
    const root = await mount({ me: meOf([SWITCHES_READ_NODE]) })
    expect(root.querySelector(HALL_SIM_BLOCK_SELECTOR)).toBeNull()
    expect(root.querySelector(HALL_SIM_HOST_SELECTOR)!.innerHTML).toBe('')
    expect(switchCalls()).toHaveLength(0)
  })

  it('③ 只有写权限 ⇒ 控件同样不渲染（勾选态读不出来，画出来就是撒谎）', async () => {
    const root = await mount({ me: meOf([SWITCHES_WRITE_NODE]) })
    expect(root.querySelector(HALL_SIM_BLOCK_SELECTOR)).toBeNull()
    expect(root.querySelector(HALL_SIM_HOST_SELECTOR)!.innerHTML).toBe('')
    expect(switchCalls()).toHaveLength(0)
  })

  it('③ 身份取不到（401 / 网络）⇒ 按"没有节点"处理，不画控件', async () => {
    const root = await mount({ me: null })
    expect(root.querySelector(HALL_SIM_BLOCK_SELECTOR)).toBeNull()
    expect(switchCalls()).toHaveLength(0)
  })

  it('③ 读值失败 ⇒ 一个字节都不画（勾选态不明的开关不许上屏），并把失败说出来', async () => {
    const root = await mount({ switchLoadThrows: true })
    expect(root.querySelector(HALL_SIM_BLOCK_SELECTOR)).toBeNull()
    expect(root.querySelector(HALL_SIM_HOST_SELECTOR)!.innerHTML).toBe('')
    expect(LAYUI.msg).toHaveBeenCalledWith('取开关失败')
  })

  it('③ 纯函数 `canManageSwitchBlock`：两个节点缺一不可，管理员两条都短路', () => {
    expect(canManageSwitchBlock(meOf([SWITCHES_READ_NODE, SWITCHES_WRITE_NODE]))).toBe(true)
    expect(canManageSwitchBlock(meOf([SWITCHES_READ_NODE]))).toBe(false)
    expect(canManageSwitchBlock(meOf([SWITCHES_WRITE_NODE]))).toBe(false)
    expect(canManageSwitchBlock(meOf([]))).toBe(false)
    expect(canManageSwitchBlock({})).toBe(false)
    expect(canManageSwitchBlock(meOf([], true))).toBe(true)
  })

  // ── 判据 ④：标记里有标题与说明（业务方给的文案）──────────────────
  it('④ 标记里同时有 class 名与 name，且带标题「大厅优选模拟单」与业务方给的说明', async () => {
    const root = await mount({ value: 'N' })
    const host = root.querySelector(HALL_SIM_HOST_SELECTOR)!
    expect(host.innerHTML).toContain(`class="${HALL_SIM_BLOCK_CLASS} layui-form"`)
    expect(host.innerHTML).toContain(`name="${HALL_SIM_INPUT_NAME}"`)
    expect(host.innerHTML).toContain(`lay-filter="${HALL_SIM_SAVE_FILTER}"`)
    expect(host.innerHTML).toContain('大厅优选模拟单')
    expect(host.innerHTML).toContain('开启后每款游戏自动挂 2~3 张模拟优选单，仅在大厅展示')
  })

  // ── 判据 ⑤：本页原有的四件事不受影响 ─────────────────────────────
  it('⑤ 加了这一块之后，本页原有的引导 / 两张表照旧（两张表各 render 过一次）', async () => {
    const root = await mount({ value: 'N' })
    expect(root.innerHTML).toContain('四步接好闲管家')
    expect(LAYUI.tableRenders).toHaveLength(2)
    // 本页自己的取数没有被三块开关挡住。
    expect(vi.mocked(fetchXgjBindings)).toHaveBeenCalled()
  })
})
