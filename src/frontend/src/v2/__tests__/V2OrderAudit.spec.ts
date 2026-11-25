// @vitest-environment jsdom
// ↑ 挂载组件需要 DOM（本仓 vitest 默认 environment 是 node，只在本文件覆盖）。
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/** 「订单时间线」v2（V2OrderAudit，D-0360）的判据：**自建页**（没有对标 island 页）。- 权限门 **fail-closed**：无 `order.platform_action_logs` 节点 ⇒ 只画一句说明，数据面（搜索框 / 摘要卡 / 时间线）不画；- 查询真交互：输入双号 → mock API → **钉值渲染**（摘要卡含软删红签 / 节标题 /kind_label 人话 / 状态签色 / actor 随行 / truncated 提示 / 熔断红条 / 空节文案）；- 失败态：查无（1002「未找到匹配的订单」）⇒ toast 原样显示后端 msg、不画旧数据；- 空输入不发请求（本地闸，与后端「key 缺失同形同码」互为补充）。全部夹具都是**合成值**（PII 纪律）；载荷形状照后端 §1.6 契约（`apps/audit/api/timeline_views.py` 的测试是另一头的钉子）。
 * 「订单时间线」v2（V2OrderAudit，D-0360）的判据：**自建页**（没有对标 island 页）。- 权限门 **fail-closed**：无 `order.platform_action_logs` 节点 ⇒ 只画一句说明，数据面（搜索框 / 摘要卡 / 时间线）不画；- 查询真交互：输入双号 → mock API → **钉值渲染**（摘要卡含软删红签 / 节标题 /kind_label 人话 / 状态签色 / actor 随行 / truncated 提示 / 熔断红条 / 空节文案）；- 失败态：查无（1002「未找到匹配的订单」）⇒ toast 原样显示后端 msg、不画旧数据；- 空输入不发请求（本地闸，与后端「key 缺失同形同码」互为补充）。全部夹具都是**合成值**（PII 纪律）；载荷形状照后端 §1.6 契约（`apps/audit/api/timeline_views.py` 的测试是另一头的钉子）。 */
import { BizError } from '@/api/errors'
import type { OrderTimeline } from '@/api/auditTimeline'

const fetchOrderTimeline = vi.fn<(key: string) => Promise<OrderTimeline>>()

vi.mock('@/api/auditTimeline', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/auditTimeline')>()),
  fetchOrderTimeline: (...args: unknown[]) => fetchOrderTimeline(...(args as [string])),
}))

import V2OrderAudit from '../V2OrderAudit.vue'
import { useSessionStore } from '@/stores/session'

/** §1.6 形状的合成载荷：操作留痕 2 条（一条 failed 一条 done）+ 各空节 + 一个挂起绑定。 */
const TIMELINE: OrderTimeline = {
  order: {
    order_no: '11111111111111111111',
    source_order_no: 'SRC-EX-001',
    status: 'IN_PROGRESS',
    status_label: '代练中',
    game_name: '示例游戏一',
    project_text: '示例项目-排位上分',
    is_deleted: false,
    deleted_at: null,
    created_at: '2026-11-01T10:00:00+08:00',
    updated_at: '2026-11-01T12:30:00+08:00',
    taker_display: '测试打手甲',
  },
  related: {
    peer_links: [
      { binding_role: 'upstream', binding_id: 3, peer_order_no: 'PEER-1', origin_order_no: 'PEER-0' },
    ],
    split_group: null,
  },
  sections: [
    {
      key: 'operation_log',
      label: '操作留痕',
      truncated: false,
      items: [
        {
          at: '2026-11-01T12:30:00+08:00',
          kind: 'order.transition',
          kind_label: '订单状态流转',
          status: '',
          summary: '【测试操作人】状态从代练中改为待验收',
          actor: '测试操作人',
        },
      ],
    },
    {
      key: 'jidanpu_sync',
      label: '鸡蛋铺同步台账',
      truncated: true,
      items: [
        {
          at: '2026-11-01T11:00:00+08:00',
          kind: 'member.launch',
          kind_label: '向鸡蛋铺抛单',
          status: 'failed',
          summary: '上游回读失败（合成原因）',
          direction: 'out',
          attempts: 5,
        },
        {
          at: '2026-11-01T10:05:00+08:00',
          kind: 'member.launch',
          kind_label: '向鸡蛋铺抛单',
          status: 'done',
          summary: '已发出（合成）',
          direction: 'out',
          attempts: 1,
        },
      ],
    },
    { key: 'peer_sync', label: '上下游同步台账', truncated: false, items: [] },
    { key: 'dailiantong', label: '代练通链路', truncated: false, items: [] },
    { key: 'mirror', label: '镜像与链路行', truncated: false, items: [] },
    {
      key: 'qr_fetch',
      label: '查码拉取',
      truncated: false,
      items: [
        {
          at: null,
          kind: 'qr_fetch.note',
          kind_label: '查码拉取',
          status: '',
          summary: '逐次拉取暂无留痕（每分钟全量逐个问，尝试不落行）',
          suppress_until: null,
        },
      ],
    },
  ],
  bindings_health: [
    { binding_id: 7, store: '合成下家店铺甲', enabled: true, suspended: true, detail: '最近 20 条 launch 全部回读失败' },
    { binding_id: 8, store: '合成下家店铺乙', enabled: true, suspended: false, detail: '' },
  ],
}

function makeRouter(): Router {
  const Blank = { template: '<div />' }
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/v2', name: 'v2', component: Blank, children: [{ path: '', name: 'v2-home', component: Blank }] },
      { path: '/v2/order-action-log', name: 'v2-order-action-log', component: Blank },
    ],
  })
}

type Perms = { menu_permissions: string[] }

async function mountPage(perms: Perms = { menu_permissions: ['order.platform_action_logs'] }) {
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
    menu_permissions: perms.menu_permissions,
    tenant_name: '示例工作室甲',
  }
  session.status = 'ready'
  const router = makeRouter()
  await router.push('/v2')
  await router.isReady()
  const wrapper = mount(V2OrderAudit, { global: { plugins: [router, pinia] } })
  await flushPromises()
  return { wrapper, router }
}

/** 在搜索框输入并点「查时间线」（真交互入口）。 */
async function searchFor(
  wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper'],
  key: string,
): Promise<void> {
  await wrapper.find('[data-v2="search-key"]').setValue(key)
  await wrapper.find('[data-v2="search-btn"]').trigger('click')
  await flushPromises()
}

beforeEach(() => {
  vi.clearAllMocks()
  fetchOrderTimeline.mockResolvedValue(TIMELINE)
})

describe('V2OrderAudit：权限门（fail-closed）', () => {
  it('无节点 ⇒ 只画说明句，数据面（搜索框）不画；有节点 ⇒ 搜索框在', async () => {
    const denied = await mountPage({ menu_permissions: ['my_orders'] })
    expect(denied.wrapper.find('[data-v2="no-permission"]').exists()).toBe(true)
    expect(denied.wrapper.find('[data-v2="search-bar"]').exists()).toBe(false)
    expect(denied.wrapper.find('[data-v2="to-order-action-log"]').exists()).toBe(false)

    const allowed = await mountPage()
    expect(allowed.wrapper.find('[data-v2="no-permission"]').exists()).toBe(false)
    expect(allowed.wrapper.find('[data-v2="search-bar"]').exists()).toBe(true)
  })
})

describe('V2OrderAudit：查询与钉值渲染', () => {
  it('空输入不发请求（本地闸），输入双号后点查询调 fetchOrderTimeline(key)', async () => {
    const { wrapper } = await mountPage()
    await searchFor(wrapper, '   ')
    expect(fetchOrderTimeline).not.toHaveBeenCalled()

    await searchFor(wrapper, '11111111111111111111')
    expect(fetchOrderTimeline).toHaveBeenCalledWith('11111111111111111111')
  })

  it('摘要卡钉值：双号 / 状态签 / 接手人；软删单显示红签', async () => {
    const { wrapper } = await mountPage()
    await searchFor(wrapper, '11111111111111111111')
    expect(wrapper.find('[data-v2="order-no"]').text()).toBe('11111111111111111111')
    expect(wrapper.find('[data-v2="source-order-no"]').text()).toBe('SRC-EX-001')
    expect(wrapper.find('[data-v2="status-badge"]').text()).toBe('代练中')
    expect(wrapper.find('[data-v2="deleted-badge"]').exists()).toBe(false)

    fetchOrderTimeline.mockResolvedValue({
      ...TIMELINE,
      order: { ...TIMELINE.order, is_deleted: true, deleted_at: '2026-11-01T13:00:00+08:00' },
    })
    await searchFor(wrapper, '11111111111111111111')
    expect(wrapper.find('[data-v2="deleted-badge"]').exists()).toBe(true)
    expect(wrapper.find('[data-v2="status-badge"]').exists()).toBe(false)
  })

  it('时间线钉值：kind_label 人话 / 状态签色 / actor 随行 / truncated 提示 / 空节文案', async () => {
    const { wrapper } = await mountPage()
    await searchFor(wrapper, 'SRC-EX-001')

    const op = wrapper.find('[data-v2-section="operation_log"]')
    expect(op.find('[data-v2-kind-label="订单状态流转"]').exists()).toBe(true)
    expect(op.text()).toContain('【测试操作人】状态从代练中改为待验收')
    expect(op.text().match(/测试操作人/g)).toHaveLength(1)

    const jdp = wrapper.find('[data-v2-section="jidanpu_sync"]')
    expect(jdp.find('[data-v2-kind-label="向鸡蛋铺抛单"]').exists()).toBe(true)
    expect(jdp.find('[data-v2="truncated-badge"]').exists()).toBe(true)
    expect(jdp.find('[data-v2-item="member.launch"]').classes()).toContain('cv2-tl-item')

    // 空节照出（§1.6），带一句说明
    expect(wrapper.find('[data-v2-section="peer_sync"]').text()).toContain('这一节暂时没有留痕')

    // 查码节的如实空白 note（at=null 的行照渲染）
    expect(wrapper.find('[data-v2-section="qr_fetch"]').text()).toContain('逐次拉取暂无留痕')
  })

  it('熔断红条：挂起绑定红签 + detail；健康绑定不进红条样式', async () => {
    const { wrapper } = await mountPage()
    await searchFor(wrapper, '11111111111111111111')
    const hot = wrapper.find('[data-v2-breaker="7"]')
    expect(hot.attributes('data-v2-suspended')).toBe('true')
    expect(hot.classes()).toContain('cv2-tl-breaker--hot')
    expect(hot.text()).toContain('最近 20 条 launch 全部回读失败')
    expect(wrapper.find('[data-v2-breaker="8"]').classes()).not.toContain('cv2-tl-breaker--hot')
  })

  it('相关链接（related.peer_links）展示对端单号', async () => {
    const { wrapper } = await mountPage()
    await searchFor(wrapper, '11111111111111111111')
    expect(wrapper.find('[data-v2="peer-links"]').text()).toContain('PEER-1')
  })
})

describe('V2OrderAudit：失败态', () => {
  it('查无（后端 1002）⇒ toast 原样显示后端 msg、旧时间线被清掉', async () => {
    const { wrapper } = await mountPage()
    await searchFor(wrapper, '11111111111111111111')
    expect(wrapper.find('[data-v2="order-card"]').exists()).toBe(true)

    fetchOrderTimeline.mockRejectedValue(new BizError(1002, '未找到匹配的订单'))
    await searchFor(wrapper, '99999999999999999999')
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('未找到匹配的订单')
    expect(wrapper.find('[data-v2="order-card"]').exists()).toBe(false)
  })
})


it('跳到新版操作记录并带上已查到的系统单号', async () => {
  const { wrapper, router } = await mountPage()
  try {
    const link = wrapper.get('[data-v2="to-order-action-log"]')
    expect(link.attributes('href')).toBe('/v2/order-action-log')
    await searchFor(wrapper, 'SRC-EX-001')
    await wrapper.get('[data-v2="search-key"]').setValue('另一个未提交的关键词')
    await link.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe(
      `/v2/order-action-log?order_no=${TIMELINE.order.order_no}`,
    )
  } finally {
    wrapper.unmount()
  }
})

it('时间线用中文兜底显示新动作和状态，优先展示账号名称', async () => {
  const { wrapper } = await mountPage()
  try {
    fetchOrderTimeline.mockResolvedValue({
      ...TIMELINE,
      sections: [{
        key: 'test',
        label: '操作留痕',
        truncated: false,
        items: [
          {
            at: null, kind: 'sample.new_action', kind_label: 'sample.new_action',
            status: 'new_state', summary: 'sample.new_action', actor: 'tester_a', actor_name: '测试甲',
          },
          {
            at: null, kind: 'order.claim', kind_label: '接单',
            status: 'done', summary: '测试乙接手了订单', actor: 'tester_b', actor_name: '测试乙',
          },
        ],
      }],
    })
    await searchFor(wrapper, 'SRC-EX-001')
    const items = wrapper.get('[data-v2-section="test"]').text()
    expect(items).toContain('订单记录')
    expect(items).toContain('状态待核对')
    expect(items).toContain('（测试甲）')
    expect(items).not.toMatch(/sample\.new_action|new_state|tester_a|tester_b/)
    expect(items.match(/测试乙/g)).toHaveLength(1)
  } finally { wrapper.unmount() }
})
