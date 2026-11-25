// @vitest-environment jsdom
// ↑ 挂载组件需要 DOM（本仓 vitest 默认 environment 是 node，只在本文件覆盖）。
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 「客户管理」v2（V2Customers，2026-11-10 客户线；第二/三批增强同日；第五/六批 2026-11-11）
 * 的判据：mock API → **钉值渲染**（总量卡 = `total` 客户表全量、行 7 列〔第六批删来源单数〕、
 * 金额字符串原样、录单人列显示显示名）+
 * **真交互**（筛选触发请求参数 / 电话筛选 / 表头排序切 `order_by` / 重置清参 / 分页取
 * `filtered_total`）+
 * **抽屉**（第六批两段并一张表：系统单行可点开同款详情弹层、来源单行纯文本不可点、
 * 状态列 = 系统单走 v2 词表「待接单」/ 来源单走后端中文；第七批 colgroup 固定 8 列 +
 * 头部信息卡七项钉值）+ 空态。
 * 全部夹具都是**合成值**（PII 纪律）；后端契约见 `api/customers.ts` 头注
 * （`total` 恒全量、`filtered_total` 才是筛选后行数、抽屉 = 合并列表 + `customer_info`、
 * 状态词分面给）。
 */
const fetchCustomerMembers = vi.fn()
const fetchCustomerMemberFilters = vi.fn()
const fetchCustomerMemberOrders = vi.fn()

vi.mock('@/api/customers', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/customers')>()),
  fetchCustomerMembers: (...args: unknown[]) => fetchCustomerMembers(...args),
  fetchCustomerMemberFilters: (...args: unknown[]) => fetchCustomerMemberFilters(...args),
  fetchCustomerMemberOrders: (...args: unknown[]) => fetchCustomerMemberOrders(...args),
}))

// 详情 pane 用替身：本网判「弹层开合与接线（pane 落在 dlg-detail 里）」；
// pane 内部的双栏渲染与取数由它自己的消费页 spec / 真件覆盖，不在这里重复。
vi.mock('@/v2/V2OrderDetailPane.vue', () => ({
  default: { template: '<div data-v2="detail-pane-stub" />' },
}))

import V2Customers from '../V2Customers.vue'
import { useSessionStore } from '@/stores/session'

/** 合成会员行（形状 = `CustomerMemberRow`；值全部合成）。 */
const ROW_A = {
  eid: 'cm-eid-spec-a-0001',
  buyer_nick: '买家昵称甲',
  phone: '13800000001',
  order_count: 2,
  last_order_at: '2026-06-01T12:30:00+08:00',
  total_amount: '156.70',
  order_source: 3,
  source_name: '店铺甲',
  recorder: 'cs_jia',
  recorder_display: '客服甲',
}
const ROW_B = {
  eid: 'cm-eid-spec-b-0002',
  buyer_nick: '',
  phone: '',
  order_count: 1,
  last_order_at: '2026-05-30T09:00:00+08:00',
  total_amount: '-20.00',
  order_source: null,
  source_name: '',
  recorder: 'cs_yi',
  recorder_display: '',
}

const SOURCES = [
  { id: 3, name: '店铺甲' },
  { id: 7, name: '店铺乙' },
]
const RECORDERS = [
  { value: 'cs_jia', label: '客服甲' },
  { value: 'cs_yi', label: '客服乙' },
]

/** 合成抽屉系统单行（形状 = `CustomerOrderRow`；单号 = 20 位数字，与前端形状闸一致）。
 * `status_text` = 后端共享 label「待抢单」——v2 页必须按 V2 词表盖成「待接单」
 * （2026-11-03 拍板；修复轮 F2 回归钉：改回直用后端 label 会显「待抢单」当场红）。 */
const SYS_ROW = {
  no: '11111111111111111111',
  is_system: true,
  status: 'WAITING_GRAB',
  game: '抽屉游戏',
  project: '抽屉项目',
  amount: '88.00',
  time: '2026-06-01T12:30:00+08:00',
  status_text: '待抢单',
  shop: '店铺甲',
  recorder: '客服甲',
}

/** 合成抽屉头部信息卡（第七批 §1.1；形状 = `CustomerInfoCard`，值全部合成）。
 * eid 掩码出参（完整 eid 不出参）；统计 = 合并口径的原样字符串。 */
const CUSTOMER_INFO = {
  eid_masked: 'cm-e***01',
  buyer_nick: '买家昵称甲',
  phone: '13800000001',
  total_amount: '156.70',
  order_count: 3,
  last_order_at: '2026-06-02T10:00:00+08:00',
  created_at: '2026-01-01T08:00:00+08:00',
}

/** 合成抽屉来源单行（第六批合并进同一张表；纯文本不可点，`status` = 闲管家码数值、
 * 状态中文仍由后端 `status_text` 给）。 */
const SRC_ROW = {
  no: 'TB-SRC-合成-9',
  is_system: false,
  status: 21,
  game: '',
  project: '',
  amount: '45.00',
  time: '2026-05-31T10:00:00+08:00',
  status_text: '已发货',
  shop: '闲鱼店甲',
  recorder: '',
}

function membersPayload(overrides: Partial<{ total: number; filtered_total: number; items: unknown[] }> = {}) {
  return {
    items: [ROW_A, ROW_B],
    total: 12,
    filtered_total: 2,
    page: 1,
    page_size: 50,
    recorder_counts: [{ recorder: 'cs_a', name: '客服甲', count: 2 }, { recorder: 'cs_b', name: '客服乙', count: 1 }],
    ...overrides,
  }
}

function ordersPayload(
  overrides: Partial<{ total: number; items: unknown[] }> = {},
) {
  return {
    items: [SYS_ROW, SRC_ROW],
    total: 2,
    page: 1,
    page_size: 50,
    customer_info: CUSTOMER_INFO,
    ...overrides,
  }
}

function makeRouter(): Router {
  return createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/v2', name: 'v2', component: { template: '<div/>' } }],
  })
}

async function mountPage() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const session = useSessionStore()
  session.user = {
    id: 9,
    username: 'tester_a',
    display_name: '测试甲',
    identity_plane: 'tenant',
    is_tenant_admin: true,
    is_account_admin: false,
    roles: [],
    role: '',
    permissions: [],
    menu_permissions: ['customer.member_list'],
    tenant_name: '示例工作室甲',
  }
  session.status = 'ready'
  const router = makeRouter()
  await router.push('/v2')
  await router.isReady()
  const wrapper = mount(V2Customers, { global: { plugins: [router, pinia] } })
  await flushPromises()
  return { wrapper, session }
}

beforeEach(() => {
  fetchCustomerMembers.mockReset()
  fetchCustomerMemberFilters.mockReset()
  fetchCustomerMemberOrders.mockReset()
  fetchCustomerMembers.mockResolvedValue(membersPayload())
  fetchCustomerMemberFilters.mockResolvedValue({ sources: SOURCES, recorders: RECORDERS })
  fetchCustomerMemberOrders.mockResolvedValue(ordersPayload())
})

describe('V2Customers（客户管理 v2）', () => {
  it('在客户列表右侧展示各录单人去重后的客户数', async () => {
    const { wrapper } = await mountPage()
    const panel = wrapper.get('[aria-label="录单人客户数"]')
    expect(panel.text()).toContain('客服甲')
    expect(panel.text()).toContain('2')
    expect(panel.text()).toContain('客服乙')
    expect(panel.text()).toContain('同一客户可属于多位录单人')
  })
  it('总量卡 = total（客户表全量数），列表行钉值渲染 7 列（第六批删来源单数）；录单人列显示显示名', async () => {
    const { wrapper } = await mountPage()
    // 挂载即拉一次字典 + 一次列表（默认 page=1、page_size=50、默认排序 = 订单数、不带筛选键）。
    expect(fetchCustomerMemberFilters).toHaveBeenCalledTimes(1)
    expect(fetchCustomerMembers).toHaveBeenCalledWith({
      order_by: 'order_count',
      page: 1,
      page_size: 50,
    })
    expect(wrapper.find('[data-v2="stat-total"]').text()).toBe('12')
    // 措辞钉值（第二批「会员」→「客户」）。
    expect(wrapper.find('.members-stat-label').text()).toBe('客户总数')
    expect(wrapper.find('[data-v2-row="cm-eid-spec-a-0001"]').exists()).toBe(true)
    const cells = wrapper.find('[data-v2-row="cm-eid-spec-a-0001"]').findAll('td')
    expect(cells.length).toBe(7)
    expect(cells[0]!.text()).toBe('买家昵称甲')
    expect(cells[1]!.text()).toBe('13800000001')
    // 订单数 = 后端合并口径（系统 + 有效来源）原样，前端不自算。
    expect(cells[2]!.text()).toBe('2')
    expect(cells[3]!.text()).toBe('2026-06-01 12:30')
    // ⛔ 前端不算钱：金额是后端字符串**原样**（两位小数）；负数照实（D-0015）。
    expect(cells[4]!.text()).toBe('156.70')
    expect(cells[5]!.text()).toBe('店铺甲')
    // 录单人列 = 显示名（`recorder_display`），不是登录账号（业务方第二批拍板）。
    expect(cells[6]!.text()).toBe('客服甲')
    // B 行：无昵称显示占位、无电话/店铺显示「—」；显示名空串回落登录账号。
    const cellsB = wrapper.find('[data-v2-row="cm-eid-spec-b-0002"]').findAll('td')
    expect(cellsB.length).toBe(7)
    expect(cellsB[0]!.text()).toBe('（无昵称）')
    expect(cellsB[1]!.text()).toBe('—')
    expect(cellsB[4]!.text()).toBe('-20.00')
    expect(cellsB[5]!.text()).toBe('—')
    expect(cellsB[6]!.text()).toBe('cs_yi')
    // 「来源单数」列已删（第六批）：列表头不再出现这个词。
    expect(wrapper.find('thead').text()).not.toContain('来源单数')
  })

  it('总量卡不随筛选变：筛选后行数变、total 仍 = 客户表全量', async () => {
    const { wrapper } = await mountPage()
    fetchCustomerMembers.mockResolvedValue(
      membersPayload({ total: 12, filtered_total: 1, items: [ROW_A] }),
    )
    await wrapper.find('[data-v2="search"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="stat-total"]').text()).toBe('12')
    expect(wrapper.find('[data-v2="pager-total"]').text()).toContain('1 位客户')
  })

  it('筛选取数：选了店铺/录单人/电话 ⇒ 查询按选中值发键；重置清回空（不发键）', async () => {
    const { wrapper } = await mountPage()
    // 直接改下拉 model（EP select 的展开面板交互不在本网；判据是「发什么参数」）。
    // 录单人提交的是登录账号（value），不是显示名（label）。
    const vm = wrapper.vm as unknown as {
      filters: { source: string; recorder: string; phone: string }
    }
    vm.filters.source = '3'
    vm.filters.recorder = 'cs_jia'
    vm.filters.phone = '13800'
    await wrapper.find('[data-v2="search"]').trigger('click')
    await flushPromises()
    expect(fetchCustomerMembers).toHaveBeenLastCalledWith({
      source: 3,
      recorder: 'cs_jia',
      phone: '13800',
      order_by: 'order_count',
      page: 1,
      page_size: 50,
    })
    // 重置：三个筛选清回空 ⇒ 参数里没有 source / recorder / phone 键（空串 = 不发键）。
    await wrapper.find('[data-v2="reset"]').trigger('click')
    await flushPromises()
    expect(fetchCustomerMembers).toHaveBeenLastCalledWith({
      order_by: 'order_count',
      page: 1,
      page_size: 50,
    })
  })

  it('排序：点「累计消费」表头发 order_by=total_amount 并回到第 1 页；再点同一列不发新请求；点「订单数」切回', async () => {
    const { wrapper } = await mountPage()
    fetchCustomerMembers.mockClear()
    await wrapper.find('[data-v2-sort="total_amount"]').trigger('click')
    await flushPromises()
    expect(fetchCustomerMembers).toHaveBeenCalledTimes(1)
    expect(fetchCustomerMembers).toHaveBeenLastCalledWith({
      order_by: 'total_amount',
      page: 1,
      page_size: 50,
    })
    // 活动档高亮在两颗表头钮之间切换。
    expect(wrapper.find('[data-v2-sort="total_amount"]').classes()).toContain('cv2-sort-head--on')
    expect(wrapper.find('[data-v2-sort="order_count"]').classes()).not.toContain('cv2-sort-head--on')
    // 点已选中列：保持降序、不取消（不发新请求）。
    await wrapper.find('[data-v2-sort="total_amount"]').trigger('click')
    await flushPromises()
    expect(fetchCustomerMembers).toHaveBeenCalledTimes(1)
    // 切回订单数档。
    await wrapper.find('[data-v2-sort="order_count"]').trigger('click')
    await flushPromises()
    expect(fetchCustomerMembers).toHaveBeenLastCalledWith({
      order_by: 'order_count',
      page: 1,
      page_size: 50,
    })
  })

  it('分页：翻页带上页码、页数口径显示 filtered_total（不是全量 total）', async () => {
    const { wrapper } = await mountPage()
    fetchCustomerMembers.mockResolvedValue(membersPayload())
    const vm = wrapper.vm as unknown as { onPageChange: (p: number) => void }
    vm.onPageChange(3)
    await flushPromises()
    expect(fetchCustomerMembers).toHaveBeenLastCalledWith({
      order_by: 'order_count',
      page: 3,
      page_size: 50,
    })
    // 「共 N 位客户」绑 filtered_total（2），不是全量 12（el-pagination 的 total 同源）。
    expect(wrapper.find('[data-v2="pager-total"]').text()).toBe('共 2 位客户')
    expect(wrapper.find('[data-v2="page-size"]').text()).toBe('每页 50 条')
  })

  it('空态：列表为空时说明数据来源（客户来自闲管家买家标识）', async () => {
    fetchCustomerMembers.mockResolvedValue(
      membersPayload({ items: [], filtered_total: 0 }),
    )
    const { wrapper } = await mountPage()
    const empty = wrapper.find('[data-v2="empty"]')
    expect(empty.text()).toContain('闲管家买家标识')
  })

  it('取数失败：行清空 + toast 报后端中文（信封 msg 经 request 抛出）', async () => {
    const { BizError } = await import('@/api/errors')
    fetchCustomerMembers.mockRejectedValue(new BizError(403, '你没有查看客户（会员）列表的权限'))
    const { wrapper } = await mountPage()
    expect(wrapper.find('[data-v2="empty"]').text()).toContain('加载失败')
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('没有查看客户')
  })
})

describe('V2Customers 客户订单抽屉（第六批：两段并一张表）', () => {
  it('点行开右侧抽屉：按该客户 eid 拉合并列表、抽屉头显示昵称/电话、两行统一 8 列钉值', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-row="cm-eid-spec-a-0001"]').trigger('click')
    await flushPromises()
    expect(fetchCustomerMemberOrders).toHaveBeenCalledWith({
      eid: 'cm-eid-spec-a-0001',
      page: 1,
      page_size: 50,
    })
    expect(wrapper.find('[data-v2="drawer"]').exists()).toBe(true)
    expect(wrapper.find('[data-v2="drawer-title"]').text()).toContain('买家昵称甲')
    expect(wrapper.find('[data-v2="drawer-title"]').text()).toContain('13800000001')
    // 两段标题已删（第六批）：单一合并表，无「系统订单 / 闲管家来源订单」小节。
    expect(wrapper.find('[data-v2="drawer-system-title"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2="drawer-source-title"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2="drawer-source-table"]').exists()).toBe(false)
    // 系统单行：统一 8 列；状态列 = v2 词表映射——`status` 是 WAITING_GRAB、后端
    // `status_text` 给的是共享 label「待抢单」，v2 页必须显示「待接单」（2026-11-03
    // 拍板；否则同一抽屉里行与详情弹层状态词自相矛盾，修复轮 F2 的回归钉）。
    const sysCells = wrapper.find(`[data-v2-drawer-row="${SYS_ROW.no}"]`).findAll('td')
    expect(sysCells.length).toBe(8)
    expect(sysCells[0]!.text()).toBe(SYS_ROW.no)
    expect(sysCells[1]!.text()).toBe('抽屉游戏')
    expect(sysCells[2]!.text()).toBe('抽屉项目')
    expect(sysCells[3]!.text()).toBe('88.00')
    expect(sysCells[4]!.text()).toBe('2026-06-01 12:30')
    expect(sysCells[5]!.text()).toBe('待接单')
    expect(sysCells[5]!.text()).not.toBe('待抢单')
    expect(sysCells[6]!.text()).toBe('店铺甲')
    expect(sysCells[7]!.text()).toBe('客服甲')
    // 来源单行：同表同列；空游戏/项目/录单人显示「—」；状态中文直接来自 status_text。
    const srcCells = wrapper.find(`[data-v2-drawer-row="${SRC_ROW.no}"]`).findAll('td')
    expect(srcCells.length).toBe(8)
    expect(srcCells[0]!.text()).toBe(SRC_ROW.no)
    expect(srcCells[1]!.text()).toBe('—')
    expect(srcCells[2]!.text()).toBe('—')
    expect(srcCells[3]!.text()).toBe('45.00')
    expect(srcCells[4]!.text()).toBe('2026-05-31 10:00')
    expect(srcCells[5]!.text()).toBe('已发货')
    expect(srcCells[6]!.text()).toBe('闲鱼店甲')
    expect(srcCells[7]!.text()).toBe('—')
    expect(wrapper.find('[data-v2="drawer-total"]').text()).toBe('共 2 张订单')
  })

  it('抽屉头部信息卡（第七批）：七项钉值（eid 掩码 / 合并统计 / 建档时间），列宽 = colgroup 固定 8 列', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-row="cm-eid-spec-a-0001"]').trigger('click')
    await flushPromises()
    const info = wrapper.find('[data-v2="drawer-info"]')
    expect(info.exists()).toBe(true)
    // eid 出参是后端掩码原样（完整 eid 不出参，后端测试钉掩码形状）。
    expect(info.find('[data-v2="info-eid"]').text()).toBe('cm-e***01')
    expect(info.find('[data-v2="info-nick"]').text()).toBe('买家昵称甲')
    expect(info.find('[data-v2="info-phone"]').text()).toBe('13800000001')
    // ⛔ 前端不算钱：累计消费是后端字符串原样；订单数 / 两处时刻同钉。
    expect(info.find('[data-v2="info-amount"]').text()).toBe('156.70')
    expect(info.find('[data-v2="info-count"]').text()).toBe('3')
    expect(info.find('[data-v2="info-last"]').text()).toBe('2026-06-02 10:00')
    expect(info.find('[data-v2="info-created"]').text()).toBe('2026-01-01 08:00')
    // 固定列宽的机器判据：colgroup 恰好 8 个 col（fixed 布局 ⇒ 两客户抽屉列形状一致）。
    const cols = wrapper.find('[data-v2="drawer-body"] colgroup').findAll('col')
    expect(cols).toHaveLength(8)
  })

  it('只有 is_system 行的单号是链接；来源单行纯文本不可点', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-row="cm-eid-spec-a-0001"]').trigger('click')
    await flushPromises()
    // 系统单行：单号是链接（打开详情的入口）。
    expect(
      wrapper.find(`[data-v2-drawer-row="${SYS_ROW.no}"]`).find('a').exists(),
    ).toBe(true)
    // 来源单行：纯文本 td，没有任何链接（单不在系统、无详情可开）。
    expect(
      wrapper.find(`[data-v2-drawer-row="${SRC_ROW.no}"]`).find('a').exists(),
    ).toBe(false)
  })

  it('抽屉内点系统单单号 ⇒ 打开同款订单详情弹层（复用 pane，⛔ 不做第二份详情）', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-row="cm-eid-spec-a-0001"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="dlg-detail"]').exists()).toBe(false)
    await wrapper.find(`[data-v2-drawer-order="${SYS_ROW.no}"]`).trigger('click')
    await flushPromises()
    const dlg = wrapper.find('[data-v2="dlg-detail"]')
    expect(dlg.exists()).toBe(true)
    // 弹层里落的是 V2OrderDetailPane（本 spec 用替身顶替其内部渲染；接线本身钉死）。
    expect(dlg.find('[data-v2="detail-pane-stub"]').exists()).toBe(true)
  })

  it('抽屉空态：该客户没有订单时给一句说明（不白屏）', async () => {
    fetchCustomerMemberOrders.mockResolvedValue(ordersPayload({ items: [], total: 0 }))
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-row="cm-eid-spec-b-0002"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="drawer-empty"]').text()).toContain('还没有订单')
  })

  it('抽屉取数失败：清行 + toast（不打断抽屉本身）', async () => {
    const { BizError } = await import('@/api/errors')
    fetchCustomerMemberOrders.mockRejectedValue(new BizError(500, '加载失败（合成）'))
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-row="cm-eid-spec-a-0001"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="drawer-empty"]').text()).toContain('还没有订单')
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('加载失败')
  })
})
