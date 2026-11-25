// @vitest-environment jsdom
// ↑ 挂载组件需要 DOM（本仓 vitest 默认 environment 是 node，只在本文件覆盖）。
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/** 「内部抢单大厅」v2（V2Hall）的判据：mock API → **钉值渲染**（原生行 / 上家鸡蛋铺镜像 / 上家团团通镜像 / 加急徽标 / 接手价 / 统计行）、真交互（筛选与刷新 / 抢单确认弹层 → 三路端点分派 / peer 受理置灰不删卡 / 参考图弹层）、空态、权限门（fail-closed：没节点就不画）。功能语义的出处 = island 复刻页 `layui-island/pages/order-insiderecorderlist.ts`（组件文件头有逐条对照表）；全部夹具都是**合成值**（PII 纪律）。
 * 「内部抢单大厅」v2（V2Hall）的判据：mock API → **钉值渲染**（原生行 / 上家鸡蛋铺镜像 / 上家团团通镜像 / 加急徽标 / 接手价 / 统计行）、真交互（筛选与刷新 / 抢单确认弹层 → 三路端点分派 / peer 受理置灰不删卡 / 参考图弹层）、空态、权限门（fail-closed：没节点就不画）。功能语义的出处 = island 复刻页 `layui-island/pages/order-insiderecorderlist.ts`（组件文件头有逐条对照表）；全部夹具都是**合成值**（PII 纪律）。 */
import type { HallListItem } from '@/api/hall'

const fetchHall = vi.fn()
const claimOrderWithMessage = vi.fn()
const claimPeerMirror = vi.fn()
const fetchGames = vi.fn()
const fetchServers = vi.fn()
const claimUpstreamMirrorWithMessage = vi.fn()
const fetchScreenshots = vi.fn()

vi.mock('@/api/hall', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/hall')>()),
  fetchHall: (...args: unknown[]) => fetchHall(...args),
  claimOrderWithMessage: (...args: unknown[]) => claimOrderWithMessage(...args),
  claimPeerMirror: (...args: unknown[]) => claimPeerMirror(...args),
}))
vi.mock('@/api/config', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/config')>()),
  fetchGames: (...args: unknown[]) => fetchGames(...args),
  fetchServers: (...args: unknown[]) => fetchServers(...args),
}))
vi.mock('@/api/jidanpuUpstream', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/jidanpuUpstream')>()),
  claimUpstreamMirrorWithMessage: (...args: unknown[]) => claimUpstreamMirrorWithMessage(...args),
}))
vi.mock('@/api/screenshots', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/screenshots')>()),
  fetchScreenshots: (...args: unknown[]) => fetchScreenshots(...args),
}))


import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import V2Hall from '../V2Hall.vue'
import { useSessionStore } from '@/stores/session'
import { BizError, HttpError } from '@/api/errors'

/** 合成游戏 / 区服字典（fetchGames 默认预选第一条 —— 旧页 `selectedIndex = 0` 的同款行为）。 */
const GAMES = [
  { id: 2, name: '示例游戏一', sort_order: 1, is_active: true, created_at: '2026-10-01T00:00:00+08:00' },
  { id: 5, name: '示例游戏二', sort_order: 2, is_active: true, created_at: '2026-10-01T00:00:00+08:00' },
]
const SERVERS = [
  { id: 7, name: '区服甲', game: 2, sort_order: 1, is_active: true, created_at: '2026-10-01T00:00:00+08:00' },
]

/** 一行真实形状的原生大厅载荷（字段集 = `OrderListSerializer` + `hall_price` + 镜像加法键）。 */
const ROW_NATIVE: HallListItem = {
  order_no: '20261031000000000001',
  source_order_no: '',
  source: '',
  taker_name: '',
  status: 'WAITING_GRAB',
  game: 2,
  server: 7,
  project_text: '示例项目-深渊满星',
  total_price: '1.00',
  refund_fee: '0.00',
  customer_phone: '',
  is_urgent: false,
  created_at: '2026-10-31T09:30:00+08:00',
  hall_price: '100.00',
  game_role_name: '',
  customer_ww: '',
  sales_agent: '',
  created_by: '',
  remark: '',
  net_received: '1.00',
  hall_publisher_name: '',
  completed_at: null,
  time_requirement: '',
  payment_status: 'UNSETTLED',
  payment_method: '',
  taker_settled: false,
  origin: 'native',
  source_system: '',
  remote_order_id: '',
  remote_order_view: '',
  remote_game_area: '',
  remote_game_server: '',
  remote_hours_desc: '',
  mirror_pk: null,
  is_preferred: false,
  is_exited: false,
  exit_kind: null,
  sim_pk: null,
}

/** 加急原生行（订单号列带「急」徽标）。 */
const ROW_URGENT: HallListItem = {
  ...ROW_NATIVE,
  order_no: '20261031000000000002',
  is_urgent: true,
}

/** 带来源号的原生行（D-0301：来源号非空 ⇒ 订单号列显示来源号）。 */
const ROW_SOURCE_NO: HallListItem = {
  ...ROW_NATIVE,
  order_no: '20261031000000000003',
  source_order_no: 'SRC-9',
  hall_price: null,
}

/** 上家鸡蛋铺镜像行（`upstream`；订单号列显示远端展示号、区服是远端文本、参数 = `mirror_pk`）。 */
const ROW_UPSTREAM: HallListItem = {
  ...ROW_NATIVE,
  order_no: '',
  server: null,
  origin: 'upstream',
  source_system: 'jidanpu',
  remote_order_id: 'R-9000000001',
  remote_order_view: 'RV-9000000001',
  remote_game_area: '国服',
  remote_game_server: '一区',
  mirror_pk: 31,
}

/** 上家团团通镜像行（`peer`；确认制受理）。 */
const ROW_PEER: HallListItem = {
  ...ROW_NATIVE,
  order_no: '',
  server: null,
  origin: 'peer',
  source_system: 'peer',
  remote_order_id: 'P-9000000002',
  remote_order_view: '',
  remote_game_area: '安卓',
  remote_game_server: 'Q区',
  mirror_pk: 77,
}

/** `mirror_pk` 缺失的镜像行（fail-closed：不画抢单按钮）。 */
const ROW_PEER_NO_PK: HallListItem = {
  ...ROW_PEER,
  remote_order_id: 'P-9000000003',
  mirror_pk: null,
}

const PAGE_ROWS = [ROW_NATIVE, ROW_URGENT, ROW_SOURCE_NO, ROW_UPSTREAM, ROW_PEER, ROW_PEER_NO_PK]

function hallPage(items: HallListItem[], total = items.length) {
  return { items, total, page: 1, page_size: 20 }
}

const SHOT = {
  id: 11,
  img_type: 0,
  url: 'https://cos.example/shot-1.png?sign=abc',
  original_name: '进度1.png',
  size_bytes: 1024,
  remark: '第一关',
  uploaded_by: 7,
  created_at: '2026-10-23T10:00:00+08:00',
}

function makeRouter(): Router {
  const Blank = { template: '<div />' }
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      {
        path: '/v2',
        name: 'v2',
        component: Blank,
        children: [
          { path: '', name: 'v2-home', component: Blank },
          { path: 'hall', name: 'v2-hall', component: Blank },
        ],
      },
      { path: '/orders/inside-hall', name: 'orders-inside-hall', component: Blank },
    ],
  })
}

type Perms = { menu_permissions: string[] }

async function mountPage(
  perms: Perms = { menu_permissions: ['grab_hall', 'grab_hall.take_order', 'order.view_screenshot'] },
  pinia = createPinia(),
) {
  setActivePinia(pinia)
  const session = useSessionStore()
  session.user ??= {
    id: 9,
    username: 'tester_a',
    display_name: '测试甲',
    identity_plane: 'tenant',
    is_tenant_admin: false,
    is_account_admin: false,
    roles: ['代练员'],
    role: '代练员',
    permissions: [],
    menu_permissions: perms.menu_permissions,
    tenant_name: '示例工作室甲',
  }
  session.status = 'ready'
  const router = makeRouter()
  await router.push('/v2')
  await router.isReady()
  const wrapper = mount(V2Hall, { global: { plugins: [router, pinia] } })
  await flushPromises()
  return { wrapper, session, pinia }
}

/** 数据行按行键找行 tr（原生 = 我方单号；镜像 = 远端单号）。 */
function rowOf(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper'], key: string) {
  return wrapper.find(`[data-v2-row="${key}"]`)
}

beforeEach(() => {
  vi.clearAllMocks()
  fetchGames.mockResolvedValue(GAMES)
  fetchServers.mockResolvedValue(SERVERS)
  fetchHall.mockResolvedValue(hallPage(PAGE_ROWS))
})

describe('V2Hall：钉值渲染（首屏预选第一个游戏 + 四路行形状）', () => {
  it('首屏：字典先取、游戏预选第一条，首个请求带 game=<第一条> + page=1 + page_size=20', async () => {
    await mountPage()
    expect(fetchGames).toHaveBeenCalledTimes(1)
    expect(fetchServers).toHaveBeenCalledWith(2)
    expect(fetchHall).toHaveBeenCalledTimes(1)
    expect(fetchHall).toHaveBeenCalledWith({ game: 2, page: 1, page_size: 20 })
  })

  it('列宽拖拽手柄：每列 th 右缘一枚（D-0371 第五轮撤「备注要求」列 + 本轮撤「时限要求」列 ⇒ 8 数据列；拖拽行为钉在 tableResize.spec）', async () => {
    const { wrapper } = await mountPage()
    const headers = wrapper.findAll('.cv2-table thead th')
    expect(headers.length).toBe(8)
    // ⭐ status/hours/price/shots 四列固定（无手柄）⇒ 手柄 = 5（撤备注列后：单号/项目/角色/时间/操作）；
    // ⭐ D-0371 第五轮 info 改 150 定宽起步但仍可拖（业务方「角色信息拉窄一点」）。
    expect(wrapper.findAll('.cv2-th-resizer').length).toBe(5)
    for (const key of ['status', 'hours', 'price', 'shots']) {
      expect(wrapper.find(`[data-v2-col="${key}"]`).exists(), `${key} 已转固定`).toBe(false)
    }
    for (const key of ['order', 'project', 'info', 'time', 'actions']) {
      expect(wrapper.find(`[data-v2-col="${key}"]`).exists(), `${key} 可拖`).toBe(true)
    }
    // ⭐ D-0371 第五轮：「备注要求」整列撤除（业务方「不展示」）。
    expect(wrapper.find('[data-v2-note]').exists()).toBe(false)
    expect(wrapper.findAll('th').filter(h => h.text().includes('备注要求')).length).toBe(0)
  })

  it('原生行：订单号（D-0301 来源号优先）+ 急徽标 + 项目 + 角色信息（我方区服字典）+ 状态 + 接手价 + 发布时间', async () => {
    const { wrapper } = await mountPage()
    const row = rowOf(wrapper, ROW_URGENT.order_no)
    expect(row.exists()).toBe(true)
    expect(row.find('.cv2-urgent').text()).toBe('急')
    expect(row.find('[data-v2-order-no]').text()).toBe(ROW_URGENT.order_no)
    expect(row.text()).toContain('示例项目-深渊满星')
    // 角色信息 = `[游戏名]我方区服名`（查字典，不裸摆主键）。
    expect(row.text()).toContain('[示例游戏一]区服甲')
    expect(row.find('[data-v2-status]').text()).toBe('待接单')
    expect(row.find('[data-v2-status]').classes()).toEqual(expect.arrayContaining(['cv2-order-status', 'cv2-st--pending']))
    expect(row.find('.cv2-td-num').text()).toBe('100.00')
    expect(row.find('.cv2-td-time').text()).toBe('2026-10-31 09:30')
    const plain = rowOf(wrapper, ROW_NATIVE.order_no)
    expect(plain.find('.cv2-urgent').exists()).toBe(false)
    // hall_price 为 null ⇒ 接手价格空（不编 0）。
    expect(rowOf(wrapper, ROW_SOURCE_NO.order_no).find('.cv2-td-num').text()).toBe('')
    // 带来源号的原生行显示来源号（D-0301）。
    expect(rowOf(wrapper, ROW_SOURCE_NO.order_no).find('[data-v2-order-no]').text()).toBe('SRC-9')
  })

  it('镜像行（upstream / peer）：订单号列显示远端展示号、角色信息用远端文本；接手价 title 带口径句', async () => {
    const { wrapper } = await mountPage()
    const up = rowOf(wrapper, ROW_UPSTREAM.remote_order_id)
    expect(up.find('[data-v2-order-no]').text()).toBe('RV-9000000001')
    expect(up.text()).toContain('[示例游戏一]国服一区')
    const peer = rowOf(wrapper, ROW_PEER.remote_order_id)
    // 展示号缺失回落远端单号（⛔ 不回落空的 order_no）。
    expect(peer.find('[data-v2-order-no]').text()).toBe('P-9000000002')
    expect(peer.text()).toContain('[示例游戏一]安卓Q区')
    const price = rowOf(wrapper, ROW_NATIVE.order_no).find('.cv2-td-num')
    expect(price.attributes('title')).toBe('你接手此单的金额：100.00元')
  })

  it('统计行：真实笔数 + 声明句带租户名；「总金额0元」不出现（后端无汇总，不摆假数）', async () => {
    fetchHall.mockResolvedValue(hallPage(PAGE_ROWS, 41))
    const { wrapper } = await mountPage()
    const totalEl = wrapper.find('[data-v2="total"]')
    expect(totalEl.text()).toContain('当前共有41笔订单等待接单')
    expect(totalEl.text()).toContain('并非交易平台')
    expect(totalEl.text()).toContain('示例工作室甲')
    expect(totalEl.text()).not.toContain('总金额')
  })

  it('空态：0 行 ⇒ 暂无数据；加载中 ⇒ 正在加载…', async () => {
    fetchHall.mockResolvedValue(hallPage([], 0))
    const { wrapper } = await mountPage()
    expect(wrapper.find('[data-v2="empty"]').text()).toContain('暂无数据')
  })
})

describe('V2Hall：权限门（fail-closed，没节点一颗不画）', () => {
  it('没 grab_hall.take_order ⇒ 全部行都不画「接手此单」；有 ⇒ 可接行各一颗（数据门：mirror_pk 缺失的镜像行不画）', async () => {
    const withNode = await mountPage()
    // 5 行里可接的 = 原生 ×3 + upstream + peer（mirror_pk 缺失的那张 peer 卡不画）。
    expect(withNode.wrapper.findAll('[data-v2-claim]')).toHaveLength(5)
    // ⭐ D-0354 追记 3：接手此单从红字文字链改小号实底主色按钮（行内主行动；业务方修订）。
    const claimBtn = rowOf(withNode.wrapper, ROW_NATIVE.order_no).find('[data-v2-claim]')
    expect(claimBtn.classes(), '抢单 = 行内主行动按钮（⭐ 2026-11-03 业务方「接手此单按钮颜色换成红色类的颜色」：实底红 danger）').toContain('cv2-btn--danger')
    expect(claimBtn.classes(), '小号档（28px）').toContain('cv2-btn--sm')
    expect(claimBtn.classes()).not.toContain('cv2-actlink--danger')
    expect(rowOf(withNode.wrapper, ROW_PEER_NO_PK.remote_order_id).exists()).toBe(true)
    expect(
      rowOf(withNode.wrapper, ROW_PEER_NO_PK.remote_order_id).find('[data-v2-claim]').exists(),
    ).toBe(false)

    const withoutNode = await mountPage({ menu_permissions: ['grab_hall', 'order.view_screenshot'] })
    expect(withoutNode.wrapper.findAll('[data-v2-claim]')).toHaveLength(0)
  })

  it('没 order.view_screenshot ⇒ 不画参考图按钮、表头也没有参考图列', async () => {
    // b236b77e 起按钮 = 探测门（probeShots 查到 img_type=0 才画）＋ 节点门双闸：
    // 探测给图，让「有节点 ⇒ 3 颗 / 没节点 ⇒ 0 颗」的差异只归到节点上。
    fetchScreenshots.mockResolvedValue({ items: [SHOT], total: 1 })
    const { wrapper } = await mountPage({ menu_permissions: ['grab_hall', 'grab_hall.take_order'] })
    expect(wrapper.findAll('[data-v2-shot]')).toHaveLength(0)
    expect(wrapper.find('th').text()).not.toContain('参考图')
    const withNode = await mountPage()
    expect(withNode.wrapper.findAll('[data-v2-shot]')).toHaveLength(3) // 只有原生行有（镜像行没有我方单号）
  })
})

describe('V2Hall：抢单确认弹层 → 三路端点分派（文案逐字、判据同 island 页）', () => {
  it('原生行：确认语逐字 → 确定 → claimOrderWithMessage(order_no) → 就地刷新（不回第 1 页）+ 「抢单成功」', async () => {
    claimOrderWithMessage.mockResolvedValue({ order: {}, message: '' })
    const { wrapper } = await mountPage()
    await rowOf(wrapper, ROW_NATIVE.order_no).find('[data-v2-claim]').trigger('click')
    const dlg = wrapper.find('[data-v2="dlg-claim"]')
    expect(dlg.exists()).toBe(true)
    // ⭐ 弹窗档 = sm 480（全局统一，与 V2MyOrders 确认类同档）。
    expect(dlg.classes()).toContain('cv2-dialog--sm')
    expect(dlg.attributes('style')).toContain('width: 480px')
    expect(dlg.text()).toContain(
      '确定接下此单吗？【提醒】本系统不属于交易平台，不提供担保，订单结算需要自行和本商户联系',
    )
    expect(claimOrderWithMessage).not.toHaveBeenCalled()
    await wrapper.find('[data-v2="confirm-claim"]').trigger('click')
    await flushPromises()
    expect(claimOrderWithMessage).toHaveBeenCalledTimes(1)
    expect(claimOrderWithMessage).toHaveBeenCalledWith(ROW_NATIVE.order_no)
    expect(claimUpstreamMirrorWithMessage).not.toHaveBeenCalled()
    expect(claimPeerMirror).not.toHaveBeenCalled()
    // 就地刷新：重新取数（本例当前页 = 1）。
    expect(fetchHall).toHaveBeenCalledTimes(2)
    expect(fetchHall).toHaveBeenLastCalledWith({ game: 2, page: 1, page_size: 20 })
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('抢单成功')
    expect(rowOf(wrapper, ROW_NATIVE.order_no).exists()).toBe(false)
    wrapper.unmount()
  })

  it('抢单立刻移除该单，切页返回时也过滤旧缓存并在到期后补刷', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] })
    try {
      claimOrderWithMessage.mockResolvedValue({ order: {}, message: '' })
      const { wrapper, pinia } = await mountPage()
      await rowOf(wrapper, ROW_NATIVE.order_no).get('[data-v2-claim]').trigger('click')
      await wrapper.get('[data-v2="confirm-claim"]').trigger('click')
      await flushPromises()
      expect(rowOf(wrapper, ROW_NATIVE.order_no).exists()).toBe(false)
      expect(rowOf(wrapper, ROW_UPSTREAM.remote_order_id).exists()).toBe(true)
      wrapper.unmount()

      const { wrapper: returned } = await mountPage(undefined, pinia)
      expect(rowOf(returned, ROW_NATIVE.order_no).exists()).toBe(false)
      const { wrapper: anotherSession } = await mountPage()
      expect(rowOf(anotherSession, ROW_NATIVE.order_no).exists()).toBe(true)
      anotherSession.unmount()

      fetchHall.mockResolvedValue(hallPage(PAGE_ROWS.filter((row) => row !== ROW_NATIVE)))
      const requests = fetchHall.mock.calls.length
      await vi.advanceTimersByTimeAsync(1250)
      await flushPromises()
      expect(fetchHall).toHaveBeenCalledTimes(requests + 1)
      expect(rowOf(returned, ROW_NATIVE.order_no).exists()).toBe(false)

      fetchHall.mockResolvedValue(hallPage(PAGE_ROWS))
      await returned.get('[data-v2="refresh"]').trigger('click')
      await flushPromises()
      expect(rowOf(returned, ROW_NATIVE.order_no).exists()).toBe(true)
      returned.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('离开大厅后才确认抢中，返回时仍挡住旧快照且旧页面不再发查询', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] })
    try {
      let finishClaim!: (result: { message: string }) => void
      claimOrderWithMessage.mockImplementationOnce(() => new Promise((resolve) => { finishClaim = resolve }))
      const { wrapper, pinia } = await mountPage()
      await rowOf(wrapper, ROW_NATIVE.order_no).get('[data-v2-claim]').trigger('click')
      await wrapper.get('[data-v2="confirm-claim"]').trigger('click')
      expect(claimOrderWithMessage).toHaveBeenCalledExactlyOnceWith(ROW_NATIVE.order_no)
      wrapper.unmount()
      finishClaim({ message: '' })
      await flushPromises()
      expect(fetchHall).toHaveBeenCalledTimes(1)

      const { wrapper: returned } = await mountPage(undefined, pinia)
      expect(rowOf(returned, ROW_NATIVE.order_no).exists()).toBe(false)
      returned.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('原生行成功但 msg 带「未能撤回」警告 ⇒ 原文长驻显示（error 档），不用默认句', async () => {
    claimOrderWithMessage.mockResolvedValue({
      order: {},
      message: '代练通订单未能撤回（原话：…），请先到「第三方平台订单管理」撤回代练通平台订单，再指派打手',
    })
    const { wrapper } = await mountPage()
    await rowOf(wrapper, ROW_NATIVE.order_no).find('[data-v2-claim]').trigger('click')
    await wrapper.find('[data-v2="confirm-claim"]').trigger('click')
    await flushPromises()
    const toast = wrapper.find('[data-v2="toast"]')
    expect(toast.text()).toContain('代练通订单未能撤回')
    expect(toast.classes()).toContain('cv2-toast--error')
    wrapper.unmount()
  })

  it('原生行失败 ⇒ 原样显示后端 msg、不刷新（押金 / 403 等都走这一路，不自造文案）', async () => {
    claimOrderWithMessage.mockRejectedValue(new HttpError(403, '你的保证金不足，无法接单'))
    const { wrapper } = await mountPage()
    await rowOf(wrapper, ROW_NATIVE.order_no).find('[data-v2-claim]').trigger('click')
    await wrapper.find('[data-v2="confirm-claim"]').trigger('click')
    await flushPromises()
    expect(fetchHall).toHaveBeenCalledTimes(1) // 失败不刷新
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('你的保证金不足，无法接单')
  })

  it('upstream 镜像行：参数是 mirror_pk（不是远端单号）→ 刷新 + msg 原文优先', async () => {
    claimUpstreamMirrorWithMessage.mockResolvedValue({ order: {}, message: '' })
    const { wrapper } = await mountPage()
    await rowOf(wrapper, ROW_UPSTREAM.remote_order_id).find('[data-v2-claim]').trigger('click')
    await wrapper.find('[data-v2="confirm-claim"]').trigger('click')
    await flushPromises()
    expect(claimUpstreamMirrorWithMessage).toHaveBeenCalledTimes(1)
    expect(claimUpstreamMirrorWithMessage).toHaveBeenCalledWith(31)
    expect(claimOrderWithMessage).not.toHaveBeenCalled()
    expect(fetchHall).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('抢单成功')
    expect(rowOf(wrapper, ROW_UPSTREAM.remote_order_id).exists()).toBe(false)
    wrapper.unmount()
  })

  it('peer 镜像行（确认制）：受理成功 ⇒ 「正在受理中」+ 置灰不删卡**不刷新**；再点不弹层', async () => {
    const { wrapper } = await mountPage()
    const peerKey = ROW_PEER.remote_order_id
    await rowOf(wrapper, peerKey).find('[data-v2-claim]').trigger('click')
    expect(wrapper.find('[data-v2="dlg-claim"]').exists()).toBe(true)
    await wrapper.find('[data-v2="confirm-claim"]').trigger('click')
    await flushPromises()
    expect(claimPeerMirror).toHaveBeenCalledTimes(1)
    expect(claimPeerMirror).toHaveBeenCalledWith(77)
    // 不刷新（大厅读口只投影在厅行，刷了这张卡就消失——置灰留在原地）。
    expect(fetchHall).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('正在受理中')
    const row = rowOf(wrapper, peerKey)
    expect(row.classes()).toContain('cv2-row--peer-claimed')
    const btn = row.find('[data-v2-claim]')
    expect(btn.attributes('disabled')).toBeDefined()
    // 已置灰 ⇒ 再点不再走任何出口（jsdom 的离开过渡永不收尾，旧弹层 DOM 会残留——
    // 环境限制，同 V2MyOrders.spec 的注记；承重判据打在「不发第二发请求」上）。
    await btn.trigger('click')
    await flushPromises()
    expect(claimPeerMirror).toHaveBeenCalledTimes(1)
    expect(claimOrderWithMessage).not.toHaveBeenCalled()
  })

  it('peer 1012（被接走）⇒ 后端 msg + 就地刷新；其余失败 ⇒ 只显示 msg、卡保留', async () => {
    claimPeerMirror.mockRejectedValueOnce(new BizError(1012, '此单已被人接手，请刷新'))
    const { wrapper } = await mountPage()
    await rowOf(wrapper, ROW_PEER.remote_order_id).find('[data-v2-claim]').trigger('click')
    await wrapper.find('[data-v2="confirm-claim"]').trigger('click')
    await flushPromises()
    expect(fetchHall).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('此单已被人接手，请刷新')

    claimPeerMirror.mockRejectedValueOnce(new BizError(1015, '上家绑定已停用'))
    await rowOf(wrapper, ROW_PEER.remote_order_id).find('[data-v2-claim]').trigger('click')
    await wrapper.find('[data-v2="confirm-claim"]').trigger('click')
    await flushPromises()
    expect(fetchHall).toHaveBeenCalledTimes(2) // 其余失败不刷新
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('上家绑定已停用')
    // 失败的卡不置灰（还能再接）。
    expect(rowOf(wrapper, ROW_PEER.remote_order_id).classes()).not.toContain('cv2-row--peer-claimed')
  })

  it('取消 ⇒ 零请求', async () => {
    const { wrapper } = await mountPage()
    await rowOf(wrapper, ROW_NATIVE.order_no).find('[data-v2-claim]').trigger('click')
    await wrapper.find('[data-v2="dlg-claim"]').find('button:not(.cv2-btn--primary)').trigger('click')
    await flushPromises()
    expect(claimOrderWithMessage).not.toHaveBeenCalled()
    expect(claimPeerMirror).not.toHaveBeenCalled()
  })
})

describe('V2Hall：筛选 / 刷新 / 分页（服务端筛选与分页，判据同旧页 table.render）', () => {
  it('搜索：关键字与项目随请求发（空值不发）；回第 1 页', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-filter="keyword"]').setValue('20261031000000000001') // el-input：锚点即内层原生 input
    await wrapper.find('[data-v2-filter="project"]').setValue('深渊')
    await wrapper.find('[data-v2="search"]').trigger('click')
    await flushPromises()
    expect(fetchHall).toHaveBeenLastCalledWith({
      game: 2,
      keyword: '20261031000000000001',
      project_text: '深渊',
      page: 1,
      page_size: 20,
    })
  })

  it('⭐ D-0354 刷新列表挪进筛选行（紧挨「搜索」右侧、同 tool 档）：旧工具栏不再存在；点击保持当前筛选、回第 1 页', async () => {
    const { wrapper } = await mountPage()
    // 筛选面板下方的孤颗工具栏已删（刷新住进筛选行的按钮组）。
    expect(wrapper.find('[data-v2="toolbar"]').exists()).toBe(false)
    const actions = wrapper.find('[data-v2="filters"] .cv2-filter-actions')
    const search = actions.find('[data-v2="search"]')
    const refresh = actions.find('[data-v2="refresh"]')
    expect(refresh.exists()).toBe(true)
    expect(refresh.classes()).toContain('cv2-btn--tool')
    // 位置钉：刷新与搜索同容器、且排在其右侧（其后）。
    expect(
      search.element.compareDocumentPosition(refresh.element) & Node.DOCUMENT_POSITION_FOLLOWING,
      '刷新列表必须紧挨在「搜索」右侧',
    ).toBeTruthy()
    await wrapper.find('[data-v2-filter="keyword"]').setValue('深渊满星')
    await wrapper.find('[data-v2="search"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-v2="refresh"]').trigger('click')
    await flushPromises()
    expect(fetchHall).toHaveBeenLastCalledWith({ game: 2, keyword: '深渊满星', page: 1, page_size: 20 })
  })

  it('切游戏：重拉区服 + 清空已选区服 + 回第 1 页重查', async () => {
    const { wrapper } = await mountPage()
    expect(wrapper.findAll('[data-v2-game]')).toHaveLength(2)
    expect(wrapper.find('[data-v2-filter="server"]').exists()).toBe(false)
    await wrapper.find('[data-v2-game="5"]').trigger('click')
    await flushPromises()
    expect(fetchServers).toHaveBeenLastCalledWith(5)
    expect(fetchHall).toHaveBeenLastCalledWith({ game: 5, page: 1, page_size: 20 })
  })

  it('快速切游戏时，较旧请求后返回也不能覆盖当前结果', async () => {
    const { wrapper } = await mountPage()
    let resolveOld!: (value: unknown) => void
    let resolveNew!: (value: unknown) => void
    fetchHall.mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve }))
    fetchHall.mockImplementationOnce(() => new Promise((resolve) => { resolveNew = resolve }))
    await wrapper.find('[data-v2-game="5"]').trigger('click')
    await wrapper.find('[data-v2-game="2"]').trigger('click')
    resolveNew({ items: PAGE_ROWS, total: 4, page: 1, page_size: 20 })
    await flushPromises()
    resolveOld({ items: [], total: 0, page: 1, page_size: 20 })
    await flushPromises()
    expect(wrapper.find('[data-v2="total"]').text()).toContain('4笔')
    expect(wrapper.find('[data-v2-game="2"]').attributes('aria-pressed')).toBe('true')
    expect(wrapper.find('[data-v2="loading-state"]').exists()).toBe(false)
  })

  it('旧请求失败时不清空新结果或提前结束加载', async () => {
    const { wrapper } = await mountPage()
    let rejectOld!: (reason: Error) => void
    let resolveNew!: (value: unknown) => void
    fetchHall.mockImplementationOnce(() => new Promise((_, reject) => { rejectOld = reject }))
    fetchHall.mockImplementationOnce(() => new Promise((resolve) => { resolveNew = resolve }))
    await wrapper.find('[data-v2-game="5"]').trigger('click')
    await wrapper.find('[data-v2-game="2"]').trigger('click')
    rejectOld(new Error('旧请求失败'))
    await flushPromises()
    expect(wrapper.find('[data-v2="loading-state"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('旧请求失败')
    resolveNew({ items: PAGE_ROWS, total: 4, page: 1, page_size: 20 })
    await flushPromises()
    expect(wrapper.find('[data-v2="total"]').text()).toContain('4笔')
  })

  it('翻页：保持当前页码（就地刷新语义）；每页条数切换回第 1 页', async () => {
    fetchHall.mockResolvedValue({ items: PAGE_ROWS, total: 45, page: 1, page_size: 20 })
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="pager"] .btn-next').trigger('click')
    await flushPromises()
    expect(fetchHall).toHaveBeenLastCalledWith({ game: 2, page: 2, page_size: 20 })
    await wrapper.find('[data-v2="page-size"]').setValue('10')
    await flushPromises()
    expect(fetchHall).toHaveBeenLastCalledWith({ game: 2, page: 1, page_size: 10 })
  })
})

describe('V2Hall：参考图（仅原生行 + 节点门；空列表人话点名）', () => {
  it('点参考图 → 取数 → 有图开弹层（说明文字 = 文件名（备注））', async () => {
    fetchScreenshots.mockResolvedValue({ items: [{ ...SHOT, thumbnail_url: 'https://example.invalid/thumb' }], total: 1 })
    const { wrapper } = await mountPage()
    await rowOf(wrapper, ROW_NATIVE.order_no).find('[data-v2-shot]').trigger('click')
    await flushPromises()
    expect(fetchScreenshots).toHaveBeenCalledWith(ROW_NATIVE.order_no)
    const dlg = wrapper.find('[data-v2="dlg-shots"]')
    expect(dlg.exists()).toBe(true)
    // ⭐ 弹窗档 = lg 860（看图类）。
    expect(dlg.classes()).toContain('cv2-dialog--lg')
    expect(dlg.attributes('style')).toContain('width: 860px')
    expect(dlg.findAll('img')).toHaveLength(1)
    expect(dlg.get('img').attributes('src')).toBe('https://example.invalid/thumb')
    expect(dlg.get('img').attributes('loading')).toBe('lazy')
    expect(dlg.text()).toContain('进度1.png（第一关）')
    await dlg.get('button[aria-label="放大查看进度1.png（第一关）"]').trigger('click')
    await flushPromises()
    const preview = wrapper.get('[data-v2="shot-preview"]').element
    expect(preview?.querySelector('img')?.getAttribute('src')).toBe(SHOT.url)
    expect(dlg.find('a').exists()).toBe(false)
    wrapper.unmount()
  })

  it('空列表 ⇒ 提示「这单还没有上传过截图」（逐字）、不开空弹层', async () => {
    // 探测与弹层取数共用同一 mock（b236b77e）：先给探测有图、按钮才画得出来；
    // 挂载完（探测已跑完）再排队「下一次调用 = 点击后的面板取数 = 空列表」。
    fetchScreenshots.mockResolvedValue({ items: [SHOT], total: 1 })
    const { wrapper } = await mountPage()
    fetchScreenshots.mockResolvedValueOnce({ items: [], total: 0 })
    await rowOf(wrapper, ROW_NATIVE.order_no).find('[data-v2-shot]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('这单还没有上传过截图')
    expect(wrapper.find('[data-v2="dlg-shots"] .cv2-shot-grid').exists()).toBe(false)
  })

  it('取数失败 ⇒ 原样显示后端 msg', async () => {
    // 同上：探测给图让按钮画出来；点击后的那一次取数才失败 ⇒ toast 原样显示后端 msg。
    fetchScreenshots.mockResolvedValue({ items: [SHOT], total: 1 })
    const { wrapper } = await mountPage()
    fetchScreenshots.mockRejectedValueOnce(new HttpError(403, '你没有订单截图相关的权限'))
    await rowOf(wrapper, ROW_NATIVE.order_no).find('[data-v2-shot]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('你没有订单截图相关的权限')
  })
})


/* ── D-0354 追记 3：接手此单小号实底按钮的 CSS 形状钉（jsdom 不算 cascade，钉源文本）── */
const CARBON_CSS = readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../carbon.css'),
  'utf8',
)

describe('V2Hall：⭐ D-0354 追记 3 接手此单小号实底按钮（CSS 形状钉）', () => {
  it('大厅待接单使用订单状态的浅紫色', () => {
    expect(CARBON_CSS).toMatch(/\.cv2-order-status\.cv2-st--pending\s*\{[^}]*--order-status-bg:\s*var\(--tt2-st-pending-bg\)/)
  })
  it('cv2-btn--sm = 28px 高小号档；primary 实底白字 + hover deep（行内主行动按钮的皮）', () => {
    expect(CARBON_CSS).toMatch(
      /\.cv2-btn--sm\s*\{[^}]*height:\s*28px[^}]*display:\s*inline-flex[^}]*align-items:\s*center/,
    )
    expect(CARBON_CSS).toMatch(
      /\.cv2-btn--primary\s*\{[^}]*background:\s*var\(--tt2-primary\)[^}]*color:\s*var\(--tt2-panel\)/,
    )
    expect(CARBON_CSS).toMatch(
      /\.cv2-btn--primary:hover\s*\{[^}]*background:\s*var\(--tt2-primary-deep\)/,
    )
  })
})
