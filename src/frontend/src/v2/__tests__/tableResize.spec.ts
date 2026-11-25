// @vitest-environment jsdom
// ↑ 挂载组件需要 DOM（本仓 vitest 默认 environment 是 node，只在本文件覆盖）。
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { flushPromises, mount, type DOMWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/** v2 列表「列宽拖拽」的判据（业务方 第二轮之①「像对标一样加上网格」+②「字段之间可以手动拉长拉宽」的表格版；共享 `src/v2/tableResize.ts`）：1. **手柄渲染**——表头每列右缘一枚 6px 热区（`.cv2-th-resizer`），枚数 = 列数；2. **拖拽真交互**——mousedown → document mousemove/mouseup 后，该列 `<col>` 宽度变为「默认宽 + 位移」，并写入 localStorage `tt.v2.table.<page>`；3. **钳制**——拖过头/拖过窄都被钳在 [44, 600]；4. **双击复原**——拖过的列双击手柄回默认宽，localStorage 同步；5. **CSS 形状钉**——th/td 全网格线 + 手柄 6px 热区钉在 carbon.css 源文本（jsdom 不算 cascade，与 CarbonShell.spec 同一手法）。全部夹具都是**合成值**（PII 纪律）。挂载 V2MyOrders 作代表页（三表共用同一模块，
 * Income/Hall 的手柄渲染各自在页面 spec 里钉枚数）。 */
import type { MyOrderItem } from '@/api/myOrders'

const fetchAllMyOrders = vi.fn()

vi.mock('@/api/myOrders', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/myOrders')>()),
  fetchAllMyOrders: (...args: unknown[]) => fetchAllMyOrders(...args),
}))
vi.mock('@/api/jidanpuMember', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/jidanpuMember')>()),
}))

import V2MyOrders from '../V2MyOrders.vue'
import { COL_MAX_WIDTH, COL_MIN_WIDTH, loadColumnWidths } from '../tableResize'
import { useSessionStore } from '@/stores/session'

/** 一行合成数据（形状照 `MyOrderItem`）。 */
const ROW_A: MyOrderItem = {
  id: 101,
  order_no: '11111111111111111111',
  source_order_no: 'SRC-001',
  status: 'IN_PROGRESS',
  settle_requested_at: null,
  game_name: '示例游戏一',
  project_text: '示例项目-陪玩上分',
  is_urgent: true,
  temp_amount: '0.00',
  bonus_amount: '0.00',
  review_status: null,
  created_at: '2026-10-01T10:30:00+08:00',
  username: 'tester_a',
  taker_name: '测试甲',
  emp_type: '打单工资',
  settle_status: '未结账',
  taker_price: '88.00',
}

const CARBON_CSS = readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../carbon.css'),
  'utf8',
)

const STORAGE_KEY = 'tt.v2.table.v2.my-orders'

async function mountPage() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const session = useSessionStore()
  session.user = {
    id: 9,
    username: 'tester_a',
    display_name: '测试甲',
    identity_plane: 'tenant',
    is_tenant_admin: false,
    is_account_admin: false,
    roles: ['客服'],
    role: '客服',
    permissions: [],
    menu_permissions: ['my_orders', 'order.submit_work', 'order.view_screenshot', 'order.upload_screenshot'],
    tenant_name: '示例工作室甲',
  }
  session.status = 'ready'
  const Blank = { template: '<div />' }
  const router: Router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/v2', name: 'v2', component: Blank, children: [{ path: '', name: 'v2-home', component: Blank }] },
      { path: '/orders/mine', name: 'orders-mine', component: Blank },
    ],
  })
  await router.push('/v2')
  await router.isReady()
  const wrapper = mount(V2MyOrders, { global: { plugins: [router, pinia] } })
  await flushPromises()
  return { wrapper }
}

/** 在手柄上按住 → document 移动 dx → 松开（tableResize.ts 的监听都挂在 document 上）。 */
async function dragHandle(handle: DOMWrapper<Element>, dx: number): Promise<void> {
  await handle.trigger('mousedown', { button: 0, clientX: 0 })
  document.dispatchEvent(new MouseEvent('mousemove', { clientX: dx }))
  document.dispatchEvent(new MouseEvent('mouseup'))
  await flushPromises()
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  fetchAllMyOrders.mockResolvedValue([ROW_A])
})

describe('v2 列表列宽拖拽（tableResize，三表共享）', () => {
  it('手柄渲染：⭐ 2026-11-03 业务方「内容短且基本固定的列固定宽度并居中」——status/price/shots 三列转固定（无手柄），手柄枚数 = 可拖列数 7；固定列不再挂柄', async () => {
    const { wrapper } = await mountPage()
    const headers = wrapper.findAll('.cv2-table thead th')
    const handles = wrapper.findAll('.cv2-th-resizer')
    expect(headers.length).toBe(10)
    expect(handles.length).toBe(7)
    for (const key of ['status', 'price', 'shots']) {
      expect(wrapper.find(`[data-v2-col="${key}"]`).exists(), `${key} 已转固定列，不应再有手柄`).toBe(false)
    }
    // 每枚手柄都带列键锚点（排查/测试按列寻址）。
    expect(wrapper.find('[data-v2-col="order"]').exists()).toBe(true)
  })

  it('拖拽真交互（⭐ 2026-11-06 order 定宽 206 起）：拖 +60 ⇒ col 宽 266，写入 localStorage', async () => {
    const { wrapper } = await mountPage()
    const colOf = () => wrapper.findAll('.cv2-table colgroup col')[1]! // ORDER_COLUMNS[1] = 'order'
    // 定宽起步：默认 206。
    expect(colOf().attributes('style')).toContain('width: 206px')
    await dragHandle(wrapper.find('[data-v2-col="order"]'), 60)
    expect(colOf().attributes('style')).toContain('width: 266px')
    expect(loadColumnWidths('my-orders')['order']).toBe(266)
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!)).toMatchObject({ order: 266 })
  })

  it('钳制：拖 -1000px 只到 44（COL_MIN_WIDTH）、拖 +1000px 只到 600（COL_MAX_WIDTH）', async () => {
    const { wrapper } = await mountPage()
    await dragHandle(wrapper.find('[data-v2-col="order"]'), -1000)
    // （弹性列 jsdom 起点 0 ⇒ 负向同样钳到 44。）
    expect(wrapper.findAll('.cv2-table colgroup col')[1]!.attributes('style')).toContain(
      `width: ${COL_MIN_WIDTH}px`,
    )
    await dragHandle(wrapper.find('[data-v2-col="order"]'), 1000)
    expect(wrapper.findAll('.cv2-table colgroup col')[1]!.attributes('style')).toContain(
      `width: ${COL_MAX_WIDTH}px`,
    )
  })

  it('双击复原（⭐ 2026-11-06 order 定宽 206 起）：拖宽后再双击手柄 ⇒ 回默认档 206，localStorage 同步', async () => {
    const { wrapper } = await mountPage()
    await dragHandle(wrapper.find('[data-v2-col="order"]'), 60)
    expect(wrapper.findAll('.cv2-table colgroup col')[1]!.attributes('style')).toContain('width: 266px')
    await wrapper.find('[data-v2-col="order"]').trigger('dblclick')
    expect(wrapper.findAll('.cv2-table colgroup col')[1]!.attributes('style')).toContain('width: 206px')
    expect(loadColumnWidths('my-orders')['order']).toBe(206)
  })

  it('固定列使用保存宽度，弹性列忽略旧宽度并可拖拽', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ order: 333, game: 180, project: 999 }))
    const { wrapper } = await mountPage()
    // order 已入 defaults ⇒ 会话存的 333 生效。
    expect(wrapper.findAll('.cv2-table colgroup col')[1]!.attributes('style')).toContain('width: 333px')
    expect(wrapper.findAll('.cv2-table colgroup col')[2]!.attributes('style')).toContain('width: 180px')
    const projectCol = wrapper.findAll('.cv2-table colgroup col')[3]!
    expect(projectCol.attributes('style') ?? '').not.toContain('width')
    // 弹性列拖一次后获得固定宽（jsdom 起点 0 + 40；钳制下限 44）。
    await dragHandle(wrapper.find('[data-v2-col="project"]'), 40)
    expect(wrapper.findAll('.cv2-table colgroup col')[3]!.attributes('style')).toContain(
      `width: ${COL_MIN_WIDTH}px`,
    )
    // 固定短列照旧有宽：状态列 96。
    expect(wrapper.findAll('.cv2-table colgroup col')[4]!.attributes('style')).toContain('width: 96px')
  })

  it('其他列变宽时仍为订单内容保留空间', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ order: 500 }))
    const { wrapper } = await mountPage()
    const table = wrapper.find('.cv2-table')
    expect(table.attributes('style')).toContain('min-width: 1532px')
    await dragHandle(wrapper.find('[data-v2-col="order"]'), 40)
    expect(table.attributes('style')).toContain('min-width: 1572px')
  })

  it('CSS 形状钉：th/td 全网格线（四边 1px --tt2-line）+ 手柄 6px 热区 + col-resize 光标', () => {
    expect(CARBON_CSS).toMatch(
      /:is\(\.cv2-table, \.cv2-data-grid\) > :is\(thead, tbody, tfoot\) > tr > :is\(th, td\)\s*\{[^}]*border: 1px solid var\(--tt2-line\)/,
    )
    expect(CARBON_CSS).toMatch(
      /\.cv2-table-panel \.cv2-table > :is\(thead, tbody, tfoot\) > tr > :is\(th, td\)\s*\{[^}]*border-top: 0;[^}]*border-left: 0;/,
    )
    expect(CARBON_CSS).toMatch(/\.cv2-th-resizer\s*\{[^}]*width: 6px[^}]*cursor: col-resize/)
    expect(CARBON_CSS).toMatch(/\.cv2-th-resizer\s*\{[^}]*position: absolute/)
    expect(CARBON_CSS).toMatch(/body\.cv2-col-resizing\s*\{[^}]*cursor: col-resize[^}]*user-select: none/)
    // ⛔ 行内截断规则不变：44px 单行省略仍是 td 的形状。
    expect(CARBON_CSS).toMatch(/\.cv2-table td\s*\{[^}]*height: 44px[^}]*text-overflow: ellipsis/)
  })
})
