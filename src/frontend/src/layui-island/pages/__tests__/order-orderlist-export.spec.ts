// @vitest-environment jsdom
import { renderOrderRows } from './order-list-table-fixture'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 工具栏「导出」按钮的接线（spec §2-1 两档 / §7-1 / §7-2 / §7-3 / §8-2 的 F1–F6）。
 *
 * 判据打在**真 `api/orders.ts::downloadOrderExport` 拼出来的请求配置**上（E-0009 同族：
 * 不许断言页面自造的中间态）：只替 `@/api/http` 的 `request` 与 `http.get` 两个传输出口，
 * 下载函数本身走**真身** ⇒ `responseType` / `params` / 形状分流都是真实实现的产物。
 *
 * ⚠️ 夹具一律**合成值**（E-0077）：单号是 20 位数字的**形状**、来源单号写成明显合成的中文，
 * 不是取证记录里的真单号（后端 `order_nos` 的 RegexField 只认 20 位数字，形状必须像）。
 *
 * 挂载级替身的形状照 `order-orderlist-delete.spec.ts`（本页的真实挂载口是
 * `mountOrderListPage(root)`；它自己 `fetch` 证据件、再塞标记）。
 */
const HERE = path.dirname(fileURLToPath(import.meta.url))
/** 证据件原文（`public/replica/order-orderlist.html`；4 层上去 = `src/frontend`）。 */
const EVIDENCE = readFileSync(
  path.resolve(HERE, '../../../../public/replica/order-orderlist.html'),
  'utf8',
)

/** 对照标 `orderlist.js:3190` 的 i18n 字面值，**在测试里重写一遍**（E-0024：⛔ 不 import 被测常量）。 */
const ORDER_EXPORT_TOO_MANY_LITERAL =
  '勾选的情况下导出订单，每次最多只支持50笔。如果需要导出全部查询结果，可以不用勾选'

const XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

/** 合成单号：`20260927` + 12 位序号（20 位纯数字，与后端 RegexField 同形）。 */
const syntheticOrderNo = (seq: number): string => `20260927${String(seq).padStart(12, '0')}`

const LAYUI = vi.hoisted(() => {
  const msgs: string[] = []
  let rows: Array<{ order_no: string; source_order_no?: string }> = []
  let formValues: Record<string, string> = {}
  return {
    msgs,
    setRows: (next: Array<{ order_no: string; source_order_no?: string }>) => {
      rows = next
    },
    setFormValues: (next: Record<string, string>) => {
      formValues = next
    },
    table: {
      render: vi.fn(),
      reload: vi.fn(),
      reloadData: vi.fn(),
      on: vi.fn(),
      cache: {},
      getOptions: vi.fn(() => ({ page: { limit: 20 } })),
      checkStatus: vi.fn(() => ({ data: rows })),
    },
    form: {
      render: vi.fn(),
      on: vi.fn(),
      val: vi.fn(() => ({ ...formValues })),
    },
    layer: {
      msg: vi.fn((t: string) => {
        msgs.push(t)
      }),
      confirm: vi.fn(),
      alert: vi.fn(),
      close: vi.fn(),
      closeAll: vi.fn(),
      open: vi.fn(() => 1),
      load: vi.fn(() => 1),
      prompt: vi.fn(),
      tips: vi.fn(),
    },
    laydate: { render: vi.fn() },
    element: { init: vi.fn(), render: vi.fn() },
    $: (el: Element) => el,
  }
})

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return {
    ...actual,
    // ⛔ 只有 `bootstrapIsland` 换成替身：`islandMsg` / `initIslandElements` 保持真身
    //    （它们只碰替身里已有的 `layer.msg` / `element.init`）。
    bootstrapIsland: (async () => LAYUI) as unknown as typeof actual.bootstrapIsland,
  }
})

vi.mock('@/api/config', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/config')>()
  return { ...actual, fetchGames: vi.fn(async () => []), fetchServers: vi.fn(async () => []) }
})

/** 挂载期的两发辅助取数（候选名单 / 订单来源字典）：本文件判的是「点导出发几发」，两发会污染计数。 */
vi.mock('@/api/orders', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/orders')>()
  return {
    ...actual,
    fetchFilterOptions: vi.fn(async () => ({
      takers: [],
      publishers: [],
      takers_truncated: false,
      publishers_truncated: false,
    })),
  }
})

vi.mock('@/api/orderSources', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/orderSources')>()
  return {
    ...actual,
    fetchOrderSources: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 200 })),
    // ⭐ `mount` 会多取一次分组字典（筛选栏「订单分组」的候选）
    // ⇒ 不替身化会多一发真请求（本文件多处用 `mockResolvedValueOnce` 排队响应 ⇒
    //   队首被它吃掉，批量动作读到错形状，`[orderBatch] … length` 当场红）。
    fetchOrderSourceGroups: vi.fn(async () => ({
      items: [],
      total: 0,
      page: 1,
      page_size: 200,
    })),
  }
})

/**
 * 传输层替身：`http.get`（**本文件枚举到的导出出口**，⛔ 不写「唯一」：
 * 未枚举的其它出口存在与否本文件不作断言）换成替身，
 * 其余一律保持真身 ⇒ `downloadOrderExport` 的形状分流（JSON 信封 vs xlsx 字节）走真实现。
 */
vi.mock('@/api/http', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/http')>()
  return { ...actual, request: vi.fn(), http: { ...actual.http, get: vi.fn() } }
})

/**
 * 身份取数（`/api/auth/me`）单独替身。
 *
 * ⚠️ **为什么不用 `@/api/http` 的 `request` 桩来喂身份（实测，整体评审 I3 修复轮）**：本文件那个
 * `async (importOriginal) => …` 工厂**拦不住** `@/api/auth` 里的 `import { request } from './http'`
 * ——`fetchMe()` 实测走的是**真** `request`、抛 `NetworkError`（原文件里给它 `mockResolvedValue` 的
 * 那行是**死桩**：不报错，但也不生效）。在**没有渲染门**时这看不出来（各开关全 false 与全 true
 * 对「导出按钮在不在」没影响）；一旦按 `order.export` 摘按钮，它就会把 F2–F6 伪装成「按钮不见了」。
 * ⇒ 改成在**本页真正 import 的那一层**（`@/api/auth::fetchMe`）打桩，判据不变。
 */
vi.mock('@/api/auth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/auth')>()
  return { ...actual, fetchMe: vi.fn() }
})

import { fetchMe } from '@/api/auth'
import { http } from '@/api/http'
import { toOrderParams, type OrderSearchParams } from '@/api/orders'
import { mountOrderListPage } from '../order-orderlist'

const mockedFetchMe = vi.mocked(fetchMe)
const mockedGet = vi.mocked(http.get)

/** jsdom **没有** `URL.createObjectURL` / `revokeObjectURL`（导出落盘那条路会炸）⇒ 打桩。 */
const createObjectURL = vi.fn(() => 'blob:mock')
const revokeObjectURL = vi.fn()

const xlsxBlob = (): Blob => new Blob(['PK\u0003\u0004 fake-xlsx'], { type: XLSX_MIME })
const envelopeBlob = (code: number, msg: string): Blob =>
  new Blob([JSON.stringify({ code, msg, data: null })], { type: 'application/json' })

describe('工具栏「导出」按钮（spec §2-1 两档 / §8-2）', () => {
  beforeEach(() => {
    LAYUI.msgs.length = 0
    LAYUI.setRows([])
    LAYUI.setFormValues({})
    LAYUI.table.reload.mockClear()
    LAYUI.form.val.mockClear()
    mockedFetchMe.mockReset()
    // ⚠️ 本文件全部用例都点「导出」⇒ mount 期那一发 `/api/auth/me` 必须**给 `order.export` 节点**，
    //    否则 `applyOrderListPermissionGates` 会把按钮摘掉（那会让 F2–F6 伪装成「点了没反应」）。
    //    两向判据（只持 `order.list` ⇒ 按钮不在）由本文件末尾「I3 渲染门」那一条自己驱动。
    mockedFetchMe.mockResolvedValue({
      is_tenant_admin: false,
      is_account_admin: false,
      menu_permissions: ['order.export'],
    } as never)
    mockedGet.mockReset()
    createObjectURL.mockClear()
    revokeObjectURL.mockClear()
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL, revokeObjectURL }))
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: unknown) => {
        if (String(url).includes('order-orderlist.html')) {
          return { ok: true, status: 200, text: async () => EVIDENCE }
        }
        throw new Error(`未预期的 fetch：${String(url)}`)
      }),
    )
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
  })

  async function mountPage(): Promise<{ root: HTMLElement; cleanup: () => void }> {
    const root = document.createElement('div')
    document.body.appendChild(root)
    const cleanup = await mountOrderListPage(root)
    // ⚠️ mount 期除了查身份（`fetchMe`，已替身）之外不应有别的业务请求；本文件的判据是
    //    「点导出发几发」，那一发打在 `mockedGet` 上，与身份替身互不干扰。
    LAYUI.table.reload.mockClear()
    return { root, cleanup }
  }

  function clickToolbar(root: HTMLElement, action: string): void {
    renderOrderRows(root, LAYUI.table.render.mock.lastCall?.[0], LAYUI.table.checkStatus().data)
      .forEach((box) => { box.checked = true })
    LAYUI.table.reload.mockClear()
    const btn = root.querySelector<HTMLElement>(`button[data-tb="${action}"]`)
    expect(btn, `证据件里找不到 [data-tb=${action}]`).not.toBeNull()
    btn!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  }

  it('F2 勾选档：勾 3 笔点导出 ⇒ 恰好一次 GET …/orders/export，参数只有那 3 个 order_no', async () => {
    const { root, cleanup } = await mountPage()
    const THREE = [syntheticOrderNo(101), syntheticOrderNo(102), syntheticOrderNo(103)]
    // ⚠️ **筛选控件里放一个非空值**：勾选档的判据是「⛔ 不叠加其余筛选」，而若筛选为空，
    //    「叠加了空筛选」与「没叠加」的产物**逐键相同** ⇒ 那条判据会恒真、杀不掉变异。
    LAYUI.setFormValues({ key1: '测试关键字甲' })
    // ⚠️ **每行都带一个与 `order_no` 不同的来源单号**：判据因此能杀掉「拿第 1 列那个显示单号
    //    （`source_order_no`）去拼 `order_nos`」这个变异（否则两者取值相同、变异照样绿）。
    LAYUI.setRows(
      THREE.map((order_no, i) => ({ order_no, source_order_no: `测试来源号${i}` })),
    )
    mockedGet.mockResolvedValueOnce({ data: xlsxBlob(), headers: {} } as never)
    clickToolbar(root, 'exportBtn')
    await vi.waitFor(() => expect(mockedGet).toHaveBeenCalledTimes(1))
    // ⛔ 字面量（不拿被测常量自证，E-0024）：路径**不带**尾斜杠是本线与后端的接口事实。
    expect(mockedGet.mock.calls[0]![0]).toBe('/api/business/orders/export')
    const cfg = mockedGet.mock.calls[0]![1] as {
      params: Record<string, unknown>
      responseType: string
    }
    expect(cfg.responseType).toBe('blob')
    // 恰好这一个键：多带任何筛选键（含上面那个 `key1` 映射出的 `keyword`）、或带上
    // `page` / `page_size` 都会红。
    expect(cfg.params).toEqual({ order_nos: THREE.join(',') })
    cleanup()
  })

  it('F3 勾 51 笔 ⇒ 零请求 + msg 逐字等于对标那句', async () => {
    const { root, cleanup } = await mountPage()
    LAYUI.setRows(
      Array.from({ length: 51 }, (_, i) => ({ order_no: syntheticOrderNo(200 + i) })),
    )
    clickToolbar(root, 'exportBtn')
    expect(LAYUI.msgs).toContain(ORDER_EXPORT_TOO_MANY_LITERAL)
    // 弹完**一个请求都不发**（照对标 `return false`）。
    expect(mockedGet).not.toHaveBeenCalled()
    cleanup()
  })

  it('F3b 恰 50 笔 ⇒ 放行：发请求、零拦截、不弹超限那句', async () => {
    const { root, cleanup } = await mountPage()
    // 边界值：闸的语义是「**>**50 才拦」（对标 `orderlist.js:3189-3193`）⇒ 恰 50 必须原样发。
    const FIFTY = Array.from({ length: 50 }, (_, i) => syntheticOrderNo(300 + i))
    LAYUI.setRows(FIFTY.map((order_no) => ({ order_no })))
    mockedGet.mockResolvedValueOnce({ data: xlsxBlob(), headers: {} } as never)
    clickToolbar(root, 'exportBtn')
    await vi.waitFor(() => expect(mockedGet).toHaveBeenCalledTimes(1))
    // ⛔ 零拦截：超限那句一个字都不许弹（`>` 写成 `>=` 时请求也不会发 ⇒ 上一行先超时红）。
    expect(LAYUI.msgs).toEqual([])
    const cfg = mockedGet.mock.calls[0]![1] as { params: Record<string, unknown> }
    // 送的就是这 50 笔本身（少一笔 / 多一笔都红）；两端写死字面量（E-0024）。
    const orderNos = String(cfg.params.order_nos).split(',')
    expect(orderNos).toHaveLength(50)
    expect(orderNos[0]).toBe('20260927000000000300')
    expect(orderNos[49]).toBe('20260927000000000349')
    expect(cfg.params).toEqual({ order_nos: FIFTY.join(',') })
    cleanup()
  })

  it('F4 不勾选档：改一个筛选再点导出 ⇒ 参数与列表同一份（逐键相等）、不含 order_nos', async () => {
    const { root, cleanup } = await mountPage()
    LAYUI.setFormValues({ key1: '测试关键字甲' })
    // 列表腿：点「刷新」，拿 `collectSearch()` 的真产物（`reload` 的 `where`）。
    clickToolbar(root, 'refresh')
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
    const where = (LAYUI.table.reload.mock.calls[0]![1] as { where: OrderSearchParams }).where
    // 先证明「改的那个筛选真的进了 where」（否则下面那条相等会因两侧都空而恒真）。
    expect(where).toEqual({ keyword: '测试关键字甲' })

    mockedGet.mockResolvedValueOnce({ data: xlsxBlob(), headers: {} } as never)
    clickToolbar(root, 'exportBtn')
    await vi.waitFor(() => expect(mockedGet).toHaveBeenCalledTimes(1))
    const cfg = mockedGet.mock.calls[0]![1] as { params: Record<string, unknown> }
    // ⚠️ 这条是**两侧同源的对拍**：`cfg.params` 与 `toOrderParams(where)` 都过同一个
    //    `toOrderParams` ⇒ 它对「页面是否原样透传列表腿的 `where`」有判别力，
    //    对「`toOrderParams` 的映射本身对不对」**没有**（那由下一行的写死字面量兜住）。
    expect(cfg.params).toEqual(toOrderParams(where))
    // 且就是那个筛选值本身（写死字面量，⛔ 不拿被测产物自证）
    expect(cfg.params).toEqual({ keyword: '测试关键字甲' })
    expect(cfg.params).not.toHaveProperty('order_nos')
    cleanup()
  })

  it('F5 失败：HTTP 200 + 信封 ⇒ 页面原样弹**后端那句** msg（不是自造文案）', async () => {
    const { root, cleanup } = await mountPage()
    const backendMsg = '本次导出 12000 行，超过上限 10000 行，请先收窄筛选条件'
    mockedGet.mockResolvedValueOnce({
      data: envelopeBlob(1000, backendMsg),
      headers: {},
    } as never)
    clickToolbar(root, 'exportBtn')
    await vi.waitFor(() => expect(LAYUI.msgs).toContain(backendMsg))
    // 信封不能当文件落盘。
    expect(createObjectURL).not.toHaveBeenCalled()
    cleanup()
  })

  it('F6 成功：xlsx 非 JSON ⇒ 落盘一次、`link.download` 装后端那个文件名、不弹错误', async () => {
    const { root, cleanup } = await mountPage()
    const chineseName = '订单导出0927102030.xlsx'
    mockedGet.mockResolvedValueOnce({
      data: xlsxBlob(),
      headers: {
        'content-disposition':
          'attachment; filename="orders-20260927.xlsx"; ' +
          `filename*=UTF-8''${encodeURIComponent(chineseName)}`,
      },
    } as never)
    // 钉住页面落盘那一跳的**真出口**：在 `link.click()` 触发的那一刻读 `link.download`。
    // ⛔ 期望值写成字面量（E-0024：不拿被测常量/中间态自证）；⛔ 退化名（ASCII
    // `orders-20260927.xlsx`、兜底 `orders.xlsx`）与它不同 ⇒ 丢掉 `filename` 这一跳必红。
    const clickedDownloads: string[] = []
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(function (this: HTMLAnchorElement) {
        clickedDownloads.push(this.download)
      })
    try {
      clickToolbar(root, 'exportBtn')
      await vi.waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1))
      expect(clickedDownloads).toEqual(['订单导出0927102030.xlsx'])
      expect(LAYUI.msgs).toEqual([])
    } finally {
      clickSpy.mockRestore()
      cleanup()
    }
  })

  /**
   * 工具栏「导出」`exportBtn` 的**渲染门**（整体评审 I3；`D-0154` 同一条原则：
   * 「点了会 403 的入口就不该出现」）。
   *
   * 走**真 mount**（⛔ 不是直接调 `setOrderListPermissions` / `applyOrderListPermissionGates`）：
   * 判据是「mount 读了 `/api/auth/me` 之后工具栏里还剩什么」——后者证明不了「渲染时读了它」。
   *
   * ⚠️ **两向都要判**：只判「没有节点 ⇒ 摘掉」会被「恒真删掉按钮」（把功能弄没了）骗过；
   * 只判「有节点 ⇒ 在」会被「根本没加门」骗过。两条一起才钉住 `order.export` 这一个节点的语义。
   */
  it('I3 只持 `order.list` ⇒ 按钮不在 DOM；给 `order.export` ⇒ 按钮在（单点门，⛔ 不是 anyNode）', async () => {
    const withMe = (nodes: string[]): void => {
      mockedFetchMe.mockResolvedValue({
        is_tenant_admin: false,
        is_account_admin: false,
        menu_permissions: nodes,
      } as never)
    }
    withMe(['order.list'])
    {
      const { root, cleanup } = await mountPage()
      expect(
        root.querySelector('button[data-tb="exportBtn"]'),
        '只持 order.list、不持 order.export 时按钮仍渲染 ⇒ 点它会打 /orders/export（403）',
      ).toBeNull()
      cleanup()
    }
    // ⚠️ 只给别的 `order.*` 节点（不给 `order.export`）也必须**不在**——门是单点，
    //   写成 `anyNode` 时这一档会红（后端 `OrderExportView.get_permissions` 只认这一个节点）。
    withMe(['order.list', 'order.view_screenshot'])
    {
      const { root, cleanup } = await mountPage()
      expect(
        root.querySelector('button[data-tb="exportBtn"]'),
        '给别的 order.* 节点就让按钮出现 ⇒ 门比后端宽（后端只认 order.export）',
      ).toBeNull()
      cleanup()
    }
    withMe(['order.list', 'order.export'])
    {
      const { root, cleanup } = await mountPage()
      expect(
        root.querySelector('button[data-tb="exportBtn"]'),
        '持 order.export 时按钮被摘掉（过度隐藏 ⇒ 功能被弄没，整体评审 I3）',
      ).not.toBeNull()
      cleanup()
    }
  })
})
