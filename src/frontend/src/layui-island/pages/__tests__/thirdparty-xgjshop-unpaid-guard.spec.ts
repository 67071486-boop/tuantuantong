// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 线 F（业务方 2026-10-15）：【第三方平台 →「闲管家店铺对接」】页上那块
 * 「录单识别设置 → 录单识别闲管家订单时，待付款状态直接拒绝」开关。
 *
 * 本文件证四件事（⛔ 与既有的 `thirdparty-xgjshop.spec.ts` 分工明确：那个文件证本页原有的
 * 四件事，**一个字不改也得绿**；本文件只证新加的这块）：
 *
 * 1. **勾选态来自后端**（`GET /api/business/system-switches/` 的那一行），⛔ 不是写死的默认；
 * 2. 点保存 ⇒ **真发一次 PATCH**：断的是**真实出口**的三个要素——方法、URL、body
 *    （`@/api/http` 的 `request()` 被换掉，`api/systemSwitches.ts` 与 `api/auth.ts` **跑的是真实现**）；
 * 3. **fail-closed 渲染**：没有读权限（`system.settings`）/ 没有写权限（`system.edit_settings`）
 *    ⇒ 控件**一个字节都不进 DOM**，且**不发**那条注定 403 的 `GET`（D-0154 / E-0098）；
 * 4. 新增的这块**不改变**本页原有的四件事（引导 / 版本声明 / 配置+测试连接 / 两张表）。
 *
 * 手法照 `system-systemsetting.spec.ts`（`vi.mock` 掉 `bootstrapIsland` 与数据出口，
 * 只断言入参与 DOM）。⚠️ `vi.mock` 会被提升，`import` 写在它之后。
 * ⚠️ 桩打在 `@/api/http`：那是**被查找的那个模块**（E-0029），页面与两个 api 模块都从它取
 * `request`；打在 `@/api/systemSwitches` 上就只能证"包装函数被调了"，证不到 URL 与 body。
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
  SWITCHES_READ_NODE,
  SWITCHES_WRITE_NODE,
  UNPAID_GUARD_BLOCK_CLASS,
  UNPAID_GUARD_BLOCK_SELECTOR,
  UNPAID_GUARD_HOST_SELECTOR,
  UNPAID_GUARD_INPUT_NAME,
  UNPAID_GUARD_SAVE_FILTER,
  UNPAID_GUARD_SWITCH_KEY,
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
    username: 'xgj-guard-admin',
    display_name: '对接管理员',
    identity_plane: 'tenant',
    is_tenant_admin: tenantAdmin,
    roles: [],
    menu_permissions: permissions,
  }
}

type StubOptions = {
  value?: 'Y' | 'N'
  me?: ReturnType<typeof meOf> | null
  switchLoadThrows?: boolean
  saveThrows?: boolean
  /** `/auth/me` **挂着不放**（要手动放行）：用来钉「本页自己的取数不等它」（评审 P2-2）。 */
  holdMe?: boolean
}

/** 当前这一段用例的桩配置（`stubRequest` 写它、`request` 替身读它）。 */
let stub: StubOptions = {}
/**
 * `holdMe` 时用来放行 `/auth/me` 的那组开关（`stubRequest` 的替身里被赋值）。
 * ⚠️ 2026-10-28（D-0329）起三块开关各自 mount、**各发一次** `/auth/me` ⇒ 挂起的不止一个，
 * `releaseMe()` 必须全放（只放一个会让先挂起的那块永远等不到、整页 mount 永不落）。
 */
let heldMeReleases: Array<() => void> = []
let releaseMe: (() => void) | null = null

/**
 * 把 `request()` 换成按 URL 分流的替身。
 * ⚠️ 收的是**真实调用参数**（`api/systemSwitches.ts` / `api/auth.ts` 的原样入参）
 * ⇒ 下面断 URL / 方法 / body 就是断真实出口。
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
      if (stub.holdMe === true) {
        return await new Promise((resolve) => {
          heldMeReleases.push(() => resolve({ user }))
          releaseMe = () => {
            heldMeReleases.forEach((release) => release())
          }
        })
      }
      return { user }
    }
    if (config.url === SWITCHES_URL) {
      if (stub.switchLoadThrows) throw new Error('取开关失败')
      const value = stub.value ?? 'Y'
      return {
        items: [
          {
            key: UNPAID_GUARD_SWITCH_KEY,
            name: '录单识别闲管家订单时，待付款状态直接拒绝',
            value,
            default: 'Y',
            replica_sk: '',
          },
          {
            // ⚠️（D-0296）起本页有**第二块**开关（「大厅优选模拟单」），各块 mount
            // 各自查值 ⇒ 桩必须把各行都给全，否则那块的「找不到就响亮报错」会在每条用例里
            // 抛「后端开关列表里没有…」（本文件只证待付款那块，sim / preferred 那两块的判据
            // 分别在 `thirdparty-xgjshop-hall-sim-switch.spec.ts` /
            // `thirdparty-xgjshop-preferred-switch.spec.ts`）。
            key: 'hall_simulated_orders',
            name: '接单大厅生成模拟单（演示卡）',
            value: 'N',
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
      return { key: UNPAID_GUARD_SWITCH_KEY, name: '录单识别闲管家订单时，待付款状态直接拒绝', value }
    }
    throw new Error(`本用例没桩这个 URL：${config.url}`)
  }) as never)
}

function flush(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

function checkboxOf(root: HTMLElement): HTMLInputElement | null {
  return root.querySelector<HTMLInputElement>(
    `${UNPAID_GUARD_BLOCK_SELECTOR} input[name="${UNPAID_GUARD_INPUT_NAME}"]`,
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

describe('闲管家店铺对接页 · 录单识别「待付款」开关（线 F）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    LAYUI.formHandlers.clear()
    LAYUI.tableRenders.length = 0
    document.body.innerHTML = ''
    stub = {}
    heldMeReleases = []
    releaseMe = null
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  // ── 判据 ①：勾选态来自后端 ──────────────────────────────────────
  it('① 后端给 Y ⇒ 勾上，且那一发 GET 真打在开关集合端点上', async () => {
    const root = await mount({ value: 'Y' })
    const box = checkboxOf(root)
    expect(box, '控件应当画出来（这个身份两个节点都有）').not.toBeNull()
    expect(box!.checked).toBe(true)
    // 值是从后端读的（不是页面写死的）：GET 的 URL 必须逐字对。
    // ⚠️（D-0296）起两块、（D-0328）起三块；（D-0329）加的**第四块**
    // 已于同日撤除 ⇒ 集合端点被 GET **3 次**（待付款块 + 模拟单块 + 优选块）。
    const gets = switchCalls().filter((c) => c.method === 'get')
    expect(gets).toHaveLength(3)
    expect(gets[0]!.url).toBe(SWITCHES_URL)
    expect(gets[1]!.url).toBe(SWITCHES_URL)
    expect(gets[2]!.url).toBe(SWITCHES_URL)
  })

  it('① 后端给 N ⇒ 不勾（同一条链的另一个方向，防"恒勾上"）', async () => {
    const root = await mount({ value: 'N' })
    expect(checkboxOf(root)!.checked).toBe(false)
  })

  // ── 判据 ②：保存真发一次 PATCH（真实出口：方法 + URL + body）────
  it('② 点保存真发一次 PATCH：断 URL 与请求体，且两个方向都跟着勾选态走', async () => {
    const root = await mount({ value: 'Y' })
    const box = checkboxOf(root)!

    // 关掉 ⇒ PATCH `N`
    box.checked = false
    LAYUI.formHandlers.get(`submit(${UNPAID_GUARD_SAVE_FILTER})`)!()
    await flush()
    let patches = switchCalls().filter((c) => c.method === 'patch')
    expect(patches).toHaveLength(1)
    expect(patches[0]).toEqual({
      method: 'patch',
      url: `${SWITCHES_URL}${UNPAID_GUARD_SWITCH_KEY}/`,
      data: { value: 'N' },
    })
    expect(LAYUI.msg).toHaveBeenCalledWith('已保存')

    // 再打开 ⇒ PATCH `Y`（证明 body 随勾选态走，⛔ 不是恒发一个常量）
    box.checked = true
    LAYUI.formHandlers.get(`submit(${UNPAID_GUARD_SAVE_FILTER})`)!()
    await flush()
    patches = switchCalls().filter((c) => c.method === 'patch')
    expect(patches).toHaveLength(2)
    expect(patches[1]).toEqual({
      method: 'patch',
      url: `${SWITCHES_URL}${UNPAID_GUARD_SWITCH_KEY}/`,
      data: { value: 'Y' },
    })
  })

  it('② 保存失败 ⇒ 勾选态回滚到后端最后已知值，并把失败说出来', async () => {
    const root = await mount({ value: 'Y', saveThrows: true })
    const box = checkboxOf(root)!
    box.checked = false
    LAYUI.formHandlers.get(`submit(${UNPAID_GUARD_SAVE_FILTER})`)!()
    await flush()
    // 不回滚 = 把"没保存上"伪装成"已保存"（勾选态是这块唯一的反馈面）
    expect(box.checked).toBe(true)
    expect(LAYUI.msg).toHaveBeenCalledWith('没有权限执行该操作')
  })

  // ── 判据 ③：fail-closed 渲染（没权限 ⇒ 一个字节都不画）──────────
  it('③ 只有读权限 ⇒ 控件不渲染，且不发那条注定 403 的 GET', async () => {
    const root = await mount({ me: meOf([SWITCHES_READ_NODE]) })
    expect(root.querySelector(UNPAID_GUARD_BLOCK_SELECTOR)).toBeNull()
    expect(root.querySelector(UNPAID_GUARD_HOST_SELECTOR)!.innerHTML).toBe('')
    expect(switchCalls()).toHaveLength(0)
  })

  it('③ 只有写权限 ⇒ 控件同样不渲染（勾选态读不出来，画出来就是撒谎）', async () => {
    const root = await mount({ me: meOf([SWITCHES_WRITE_NODE]) })
    expect(root.querySelector(UNPAID_GUARD_BLOCK_SELECTOR)).toBeNull()
    expect(root.querySelector(UNPAID_GUARD_HOST_SELECTOR)!.innerHTML).toBe('')
    expect(switchCalls()).toHaveLength(0)
  })

  it('③ 身份取不到（401 / 网络）⇒ 按"没有节点"处理，不画控件', async () => {
    const root = await mount({ me: null })
    expect(root.querySelector(UNPAID_GUARD_BLOCK_SELECTOR)).toBeNull()
    expect(switchCalls()).toHaveLength(0)
  })

  it('③ 读值失败 ⇒ 一个字节都不画（勾选态不明的开关不许上屏），并把失败说出来', async () => {
    const root = await mount({ switchLoadThrows: true })
    expect(root.querySelector(UNPAID_GUARD_BLOCK_SELECTOR)).toBeNull()
    expect(root.querySelector(UNPAID_GUARD_HOST_SELECTOR)!.innerHTML).toBe('')
    expect(LAYUI.msg).toHaveBeenCalledWith('取开关失败')
  })

  it('③ 纯函数 `canManageSwitchBlock`：两个节点缺一不可，管理员两条都短路', () => {
    expect(canManageSwitchBlock(meOf([SWITCHES_READ_NODE, SWITCHES_WRITE_NODE]))).toBe(true)
    expect(canManageSwitchBlock(meOf([SWITCHES_READ_NODE]))).toBe(false)
    expect(canManageSwitchBlock(meOf([SWITCHES_WRITE_NODE]))).toBe(false)
    expect(canManageSwitchBlock(meOf([]))).toBe(false)
    expect(canManageSwitchBlock({})).toBe(false)
    // 租户管理员 / 账号管理员由 `canNode` 短路（与后端 `has_node` 对管理员的口径一致）
    expect(canManageSwitchBlock(meOf([], true))).toBe(true)
  })

  it('标记里同时有 class 名与 name（选择器常量与标记拼法不许各写一半）', async () => {
    const root = await mount({ value: 'Y' })
    const host = root.querySelector(UNPAID_GUARD_HOST_SELECTOR)!
    expect(host.innerHTML).toContain(`class="${UNPAID_GUARD_BLOCK_CLASS} layui-form"`)
    expect(host.innerHTML).toContain(`name="${UNPAID_GUARD_INPUT_NAME}"`)
    expect(host.innerHTML).toContain(`lay-filter="${UNPAID_GUARD_SAVE_FILTER}"`)
    // ⛔ 阿奇索不在本开关的管辖内，页面必须写出来（否则租户会以为连阿奇索的单也一起拦）
    expect(host.innerHTML).toContain('阿奇索')
  })

  // ── 判据 ④：本页原有的四件事不受影响 ─────────────────────────────
  it('④ 加了这一块之后，本页原有的引导 / 版本声明 / 两张表照旧', async () => {
    const root = await mount({ value: 'Y' })
    expect(root.innerHTML).toContain('四步接好闲管家')
    expect(root.innerHTML).toContain('开放平台')
    expect(root.innerHTML).toContain('才能识别订单')
    // 两张表各 render 过一次（应用表 + 店铺表）
    expect(LAYUI.tableRenders).toHaveLength(2)
  })

  it('④ 开关块**不阻塞**本页自己的取数：`/auth/me` 还挂着时两张表已经开始拉', async () => {
    // 评审 P2-2：早先写成 `await mountUnpaidGuardBlock(); await reload()` ⇒ 两张表要**串行**等
    // `/auth/me`（有权限时再等一次 `/system-switches/`）才开始拉，而那两次请求与表毫无关系。
    // 判据打在**副作用**上：把 `/auth/me` 挂着不放，本页的 `fetchXgjBindings` 仍必须已经被调用。
    // ⛔ 这不是"时序偏好"：串行版本下这条必红（挂起的 me 会把 reload 一起堵死）。
    stubRequest({ value: 'Y', holdMe: true })
    const root = document.createElement('div')
    document.body.appendChild(root)
    const mounted = page.mount(root)
    await flush()
    // 前置哨兵：`/auth/me` 此刻**确实还挂着**（放行函数就位、但还没调用）——否则下面恒真
    expect(releaseMe, '前置：这一条要的是"me 还挂着"那一刻').not.toBeNull()
    expect(vi.mocked(fetchXgjBindings)).toHaveBeenCalled()
    // 放行之后照常画完（这一块没有被这次时序改动弄坏）
    releaseMe!()
    await mounted
    expect(checkboxOf(root)!.checked).toBe(true)
  })
})
