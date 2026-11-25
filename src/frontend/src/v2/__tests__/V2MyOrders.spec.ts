// @vitest-environment jsdom
// ↑ 挂载组件需要 DOM（本仓 vitest 默认 environment 是 node，只在本文件覆盖）。
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/** 「指派给我的订单」v2（V2MyOrders）的判据：mock API → **钉值渲染**（列 / 状态签 / 金额 / D-0301 展示回退）、真交互（筛选 / 勾选 / 分页 / 提交完单 / 更改状态弹层 / 推送双档 / 导出 CSV / 详情层）、空态与失败态、权限门（fail-closed：没节点就不画）。功能语义的出处 = island 复刻页 `layui-island/pages/order-myorderlist.ts`（组件文件头有逐条对照表）；全部夹具都是**合成值**（PII 纪律）。
 * 「指派给我的订单」v2（V2MyOrders）的判据：mock API → **钉值渲染**（列 / 状态签 / 金额 / D-0301 展示回退）、真交互（筛选 / 勾选 / 分页 / 提交完单 / 更改状态弹层 / 推送双档 / 导出 CSV / 详情层）、空态与失败态、权限门（fail-closed：没节点就不画）。功能语义的出处 = island 复刻页 `layui-island/pages/order-myorderlist.ts`（组件文件头有逐条对照表）；全部夹具都是**合成值**（PII 纪律）。 */
import type { MyOrderDetail, MyOrderItem } from '@/api/myOrders'
import { HttpError } from '@/api/errors'
import OrderDetailDialog from '../OrderDetailDialog.vue'
import { openInternalChatPreview } from '../internalChat/launcherPreview'
import { webKLiveOrigin } from '../internalChat/liveChatPreview'

const fetchAllMyOrders = vi.fn()
const fetchMyOrderDetail = vi.fn()
const fetchOrder = vi.fn()
const fetchOrderAudit = vi.fn()
const submitMyOrder = vi.fn()
const reportIssueOrder = vi.fn()
const pushMemberJidanpuNow = vi.fn()
const syncMemberJidanpuNow = vi.fn()
const fetchOrderChat = vi.fn()
const joinOrderChat = vi.fn()

vi.mock('@/api/orderChat', async importOriginal => ({
  ...await importOriginal<typeof import('@/api/orderChat')>(),
  fetchOrderChat: (...args: unknown[]) => fetchOrderChat(...args),
  fetchOrderChatMessages: () => Promise.resolve({ messages: [], has_more: false, has_newer: false }),
  joinOrderChat: (...args: unknown[]) => joinOrderChat(...args),
}))

vi.mock('@/api/myOrders', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/myOrders')>()),
  fetchAllMyOrders: (...args: unknown[]) => fetchAllMyOrders(...args),
  fetchMyOrderDetail: (...args: unknown[]) => fetchMyOrderDetail(...args),
  submitMyOrder: (...args: unknown[]) => submitMyOrder(...args),
  reportIssueOrder: (...args: unknown[]) => reportIssueOrder(...args),
}))
vi.mock('@/api/orders', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/orders')>()),
  fetchOrder: (...args: unknown[]) => fetchOrder(...args),
  fetchOrderAudit: (...args: unknown[]) => fetchOrderAudit(...args),
}))
vi.mock('@/api/jidanpuMember', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/jidanpuMember')>()),
  pushMemberJidanpuNow: (...args: unknown[]) => pushMemberJidanpuNow(...args),
  syncMemberJidanpuNow: (...args: unknown[]) => syncMemberJidanpuNow(...args),
}))

import V2MyOrders from '../V2MyOrders.vue'
import { useSessionStore } from '@/stores/session'

/** 三行合成数据（形状照 `MyOrderItem`）：代练中+加急+来源号 / 待验收+空接手人 / 已完成。 */
const ROW_A: MyOrderItem = {
  id: 101,
  order_no: '11111111111111111111',
  source_order_no: 'SRC-001',
  status: 'IN_PROGRESS',
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
  // ⭐ D-0365：载荷自 2026-10-23 起带申请时刻（类型必填按后端形状补）。
  settle_requested_at: null,
}
const ROW_B: MyOrderItem = {
  ...ROW_A,
  id: 102,
  order_no: '22222222222222222222',
  source_order_no: '',
  status: 'PENDING_REVIEW',
  game_name: '示例游戏二',
  project_text: '示例项目-代练升级',
  is_urgent: false,
  created_at: '2026-10-02T09:00:00+08:00',
  taker_name: null,
  taker_price: null,
}
const ROW_C: MyOrderItem = {
  ...ROW_A,
  id: 103,
  order_no: '33333333333333333333',
  source_order_no: '',
  status: 'COMPLETED',
  game_name: '示例游戏一',
  project_text: '示例项目-日常任务',
  is_urgent: false,
  created_at: '2026-09-01T08:00:00+08:00',
}

const THREE_ROWS = [ROW_A, ROW_B, ROW_C]

/** 25 行合成数据（分页用：默认每页 20 ⇒ 第 2 页 5 行）。 */
function manyRows(count: number): MyOrderItem[] {
  return Array.from({ length: count }, (_, i) => ({
    ...ROW_A,
    id: i + 1,
    order_no: String(i + 1).padStart(20, '0'),
    source_order_no: '',
    is_urgent: false,
  }))
}

function makeRouter(): Router {
  const Blank = { template: '<div />' }
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/v2', name: 'v2', component: Blank, children: [{ path: '', name: 'v2-home', component: Blank }] },
      { path: '/orders/mine', name: 'orders-mine', component: Blank },
    ],
  })
}

type Perms = {
  menu_permissions: string[]
  is_account_admin?: boolean
}

async function mountPage(
  perms: Perms = { menu_permissions: ['my_orders', 'order.submit_work', 'order.view_screenshot', 'order.upload_screenshot'] },
  openChat?: (conversationId?: number) => void,
) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const session = useSessionStore()
  session.user = {
    id: 9,
    username: 'tester_a',
    display_name: '测试甲',
    identity_plane: 'tenant',
    is_tenant_admin: false,
    is_account_admin: perms.is_account_admin ?? false,
    roles: ['客服'],
    role: '客服',
    permissions: [],
    menu_permissions: perms.menu_permissions,
    tenant_name: '示例工作室甲',
  }
  session.status = 'ready'
  const router = makeRouter()
  // ⚠️ vue-router 5：`isReady()` 必须在初始导航**落在真实路由**之后才 resolve
  // （初始 '/' 不匹配任何记录时会挂起——与本仓其它 spec 先 push 再 isReady 同一口径）。
  await router.push('/v2')
  await router.isReady()
  const wrapper = mount(V2MyOrders, {
    attachTo: openChat ? document.body : undefined,
    global: {
      plugins: [router, pinia],
      ...(openChat && { provide: { [openInternalChatPreview as symbol]: openChat } }),
    },
  })
  await flushPromises()
  return { wrapper, router, session }
}

/** 数据行按 order_no 找行 tr。 */
function rowOf(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper'], no: string) {
  return wrapper.find(`[data-v2-row="${no}"]`)
}

beforeEach(() => {
  vi.clearAllMocks()
  fetchAllMyOrders.mockResolvedValue(THREE_ROWS)
  fetchMyOrderDetail.mockResolvedValue({
    order_no: ROW_A.order_no,
    source_order_no: ROW_A.source_order_no,
    project_text: ROW_A.project_text,
    game_name: '测试游戏',
    server_name: null,
    status: ROW_A.status,
    content: '合成订单全文',
    demand: '合成代练要求',
    time_requirement: '30 天',
    remark: '验收备注',
    game_account: '测试账号',
    game_sub_account: '',
    game_role_name: '测试角色',
    claimed_at: '2026-11-14T01:00:00+00:00',
    temp_amount: '8.80',
    is_urgent: true,
    group_qr_url: null,
  } satisfies MyOrderDetail)
  fetchOrder.mockResolvedValue({ content: '全量订单内容' })
  fetchOrderAudit.mockResolvedValue([])
  submitMyOrder.mockResolvedValue({})
  reportIssueOrder.mockResolvedValue({})
  pushMemberJidanpuNow.mockResolvedValue({ requested: 1, pushed: 1, skipped: 0, failed: 0, items: [{ order_no: ROW_A.order_no, result: 'done', reason: '' }] })
  syncMemberJidanpuNow.mockResolvedValue({ applied: 2 })
})

describe('V2MyOrders：钉值渲染（列 = 旧页真有值的那些）', () => {
  it('三行渲染：订单号列按 D-0301 回退（来源号非空取它）+ 急徽标只在加急行', async () => {
    const { wrapper } = await mountPage()
    expect(fetchAllMyOrders).toHaveBeenCalledTimes(1)
    expect(wrapper.findAll('[data-v2-row]')).toHaveLength(3)
    const cellA = rowOf(wrapper, ROW_A.order_no).find('.cv2-td-order')
    expect(cellA.text()).toContain('SRC-001') // 展示值 = source_order_no（非空）
    expect(cellA.find('.cv2-urgent').text()).toBe('急')
    const cellB = rowOf(wrapper, ROW_B.order_no).find('.cv2-td-order')
    expect(cellB.text()).toContain(ROW_B.order_no) // 来源号空 ⇒ 回退内置号
    expect(cellB.find('.cv2-urgent').exists()).toBe(false)
  })

  it('状态签带中文与 token 类；接手价右对齐等宽原样显示；空接手人/空接手价 = 空单元格', async () => {
    const { wrapper } = await mountPage()
    const stA = rowOf(wrapper, ROW_A.order_no).find('[data-v2-status]')
    expect(stA.text()).toBe('代练中')
    expect(stA.classes()).toContain('cv2-st--doing')
    const stB = rowOf(wrapper, ROW_B.order_no).find('[data-v2-status]')
    expect(stB.text()).toBe('待验收')
    expect(stB.classes()).toContain('cv2-st--accept')
    const numA = rowOf(wrapper, ROW_A.order_no).find('.cv2-td-num')
    expect(numA.text()).toBe('88.00') // 后端两位小数字符串原样（⛔ 不前端格式化）
    expect(numA.classes()).toContain('cv2-td-num')
    const cellsB = rowOf(wrapper, ROW_B.order_no).findAll('td')
    const textsB = cellsB.map((td) => td.text())
    expect(textsB.join('|')).toContain('||') // 接手人/接手价两列都是空串（⛔ 不写「—」占位）
  })

  it('统计行报真数据（筛出行数）+ 声明句；不出现旧页那排恒 0 的聚合骨架', async () => {
    const { wrapper } = await mountPage()
    const total = wrapper.find('[data-v2="total"]')
    expect(total.text()).toContain('共 3 笔订单')
    expect(total.text()).toContain('示例工作室甲')
    expect(total.text()).not.toContain('接手总金额')
  })

  it('工具栏有「检查发送失败订单」那类假控件的反面不存在：不渲染只弹「尚未开放」的按钮', async () => {
    const { wrapper } = await mountPage()
    const labels = wrapper.find('[data-v2="toolbar"]').text()
    for (const real of ['刷新', '发到我的管理系统', '同步订单状态', '更改状态', '导出订单']) {
      expect(labels).toContain(real)
    }
    expect(labels).not.toContain('检查发送失败订单')
  })
})

describe('V2MyOrders：筛选（本地筛全量行，字段同旧页 7 个）', () => {
  it('单号搜索两个号都认（D-0301）：按来源号 SRC-001 能筛出行 A', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-filter="key1"]').setValue('SRC-001') // el-input：锚点落在内层原生 input（attrs 实测）
    await wrapper.find('[data-v2="search"]').trigger('click')
    expect(wrapper.findAll('[data-v2-row]')).toHaveLength(1)
    expect(rowOf(wrapper, ROW_A.order_no).exists()).toBe(true)
  })

  it('顶部状态卡片已撤，三行都还在', async () => {
    const { wrapper } = await mountPage()
    expect(wrapper.find('[data-v2="status-cards"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2-scard]').exists()).toBe(false)
    expect(wrapper.findAll('[data-v2-row]')).toHaveLength(3)
  })

  it('只看加急 checkbox 即时生效；重置按钮恢复全部 + 默认日期', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-filter="urgent"] input').setValue(true) // el-checkbox 的原生 input
    expect(wrapper.findAll('[data-v2-row]')).toHaveLength(1)
    await wrapper.find('[data-v2="reset"]').trigger('click')
    expect(wrapper.findAll('[data-v2-row]')).toHaveLength(3)
    const sdate = wrapper.find('[data-v2-filter="sdate"] input').element as HTMLInputElement // 锚点在包裹层，input = el-date-picker 展示层
    const today = new Date()
    const p = (n: number): string => String(n).padStart(2, '0')
    const expectedFrom = new Date(today.getFullYear(), today.getMonth() - 6, today.getDate())
    expect(sdate.value).toBe(
      `${expectedFrom.getFullYear()}-${p(expectedFrom.getMonth() + 1)}-${p(expectedFrom.getDate())}`,
    )
  })

  it('筛出 0 行 ⇒ 空态一句话（暂无数据）', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-filter="key1"]').setValue('不存在的单号')
    expect(wrapper.find('[data-v2="empty"]').text()).toContain('暂无数据')
  })

  it('取数失败 ⇒ 表清空 + 错误 toast（后端 msg 原样显示，不拿空表冒充正常）', async () => {
    fetchAllMyOrders.mockRejectedValue(new Error('合成错误：没有权限'))
    const { wrapper } = await mountPage()
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('合成错误：没有权限')
    expect(wrapper.find('[data-v2="empty"]').exists()).toBe(true)
  })
})

describe('V2MyOrders：分页（客户端；limit 20 / limits 同旧页）', () => {
  it('25 行时翻到第 2 页，首行换成第 21 行；每页条数切换生效', async () => {
    fetchAllMyOrders.mockResolvedValue(manyRows(25))
    const { wrapper } = await mountPage()
    expect(wrapper.find('[data-v2="pager-total"]').text()).toBe('共 25 条')
    expect(rowOf(wrapper, String(1).padStart(20, '0')).exists()).toBe(true)
    await wrapper.find('[data-v2="pager"] .btn-next').trigger('click')
    await flushPromises()
    expect(rowOf(wrapper, String(21).padStart(20, '0')).exists()).toBe(true)
    expect(rowOf(wrapper, String(1).padStart(20, '0')).exists()).toBe(false)
    // 每页 10 条 ⇒ 页码拨回第 1 页（onPageSizeChange 的行为）：首行回到第 1 行；
    // 再点「下一页」⇒ 第 11 行回到首屏（翻页按钮在新的每页条数下照常工作）。
    await wrapper.find('[data-v2="page-size"]').setValue('10')
    await flushPromises()
    expect(wrapper.find('[data-v2="pager-total"]').text()).toBe('共 25 条')
    expect(rowOf(wrapper, String(1).padStart(20, '0')).exists()).toBe(true)
    await wrapper.find('[data-v2="pager"] .btn-next').trigger('click')
    await flushPromises()
    expect(rowOf(wrapper, String(11).padStart(20, '0')).exists()).toBe(true)
  })
})

describe('V2MyOrders：行操作（提交完单 / 更改状态——业务规则全在后端，前端转述 msg）', () => {
  it('提交完成：确认弹层（逐字对标文案）→ 确定 → submitMyOrder(order_no) → 先刷新再提示成功', async () => {
    const { wrapper } = await mountPage()
    await rowOf(wrapper, ROW_A.order_no).find('[data-v2-submit]').trigger('click')
    const dlg = wrapper.find('[data-v2="dlg-confirm"]')
    expect(dlg.exists()).toBe(true)
    expect(dlg.text()).toContain('确定打完了吗？提交前请再三确认哦')
    expect(submitMyOrder).not.toHaveBeenCalled()
    await wrapper.find('[data-v2="confirm-submit"]').trigger('click')
    await flushPromises()
    expect(submitMyOrder).toHaveBeenCalledTimes(1)
    expect(submitMyOrder).toHaveBeenCalledWith(ROW_A.order_no)
    expect(fetchAllMyOrders).toHaveBeenCalledTimes(2) // 成功后刷新（reload）
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('提交成功，已送交验收')
    // 「关闭」断言 = 状态复位可复用：再点同一行的「提交完成」能重新打开（confirmState 已被清空）。
    // ⛔ 不断言 destroy-on-close 的槽内容销毁：jsdom（vitest 默认无 rAF，pretendToBeVisual=false）
    // 里 Vue 的离开过渡永不收尾，destroy 永不发生——那是环境限制，不是组件行为。
    await rowOf(wrapper, ROW_A.order_no).find('[data-v2-submit]').trigger('click')
    expect(wrapper.find('[data-v2="dlg-confirm"] .cv2-dlg-text').exists()).toBe(true)
  })

  it('改状态先校验来源态：非代练中 ⇒ 固定文案提示、不开弹层、零请求', async () => {
    const { wrapper } = await mountPage()
    await rowOf(wrapper, ROW_B.order_no).find('[data-v2-status]').trigger('click')
    expect(wrapper.find('[data-v2="dlg-status"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('只有代练中的单可以更改状态，当前状态：待验收')
    expect(submitMyOrder).not.toHaveBeenCalled()
    expect(reportIssueOrder).not.toHaveBeenCalled()
  })

  it('代练中 ⇒ 弹层恰好两档（待验收/问题单）→ 选问题单 ⇒ reportIssueOrder（单号、逐单）', async () => {
    const { wrapper } = await mountPage()
    await rowOf(wrapper, ROW_A.order_no).find('[data-v2-status]').trigger('click')
    const dlg = wrapper.find('[data-v2="dlg-status"]')
    expect(dlg.exists()).toBe(true)
    const radios = dlg.findAll('input[type="radio"]')
    expect(radios).toHaveLength(2)
    expect(dlg.text()).toContain('待验收')
    expect(dlg.text()).toContain('问题单')
    await radios[1]!.setValue(true) // 问题单
    expect(dlg.find('[data-v2="status-reason"]').exists()).toBe(true)
    await wrapper.find('[data-v2="confirm-status"]').trigger('click')
    await flushPromises()
    expect(reportIssueOrder).not.toHaveBeenCalled()
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('请填写变更原因')
    await wrapper.find('[data-v2="status-reason-input"]').setValue('账号掉线')
    await wrapper.find('[data-v2="confirm-status"]').trigger('click')
    await flushPromises()
    expect(reportIssueOrder).toHaveBeenCalledTimes(1)
    expect(reportIssueOrder).toHaveBeenCalledWith(ROW_A.order_no, '账号掉线')
    expect(submitMyOrder).not.toHaveBeenCalled()
    expect(fetchAllMyOrders).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('已标记为问题单')
    // 「关闭」断言 = 状态复位可复用（overlay DOM 在 jsdom 里会留存——环境限制，同前一条用例注记）。
    await rowOf(wrapper, ROW_A.order_no).find('[data-v2-status]').trigger('click')
    expect(wrapper.find('[data-v2="dlg-status"] .cv2-dlg-radios').exists()).toBe(true)
  })
})

describe('V2MyOrders：⭐ 2026-11-01 第二轮 弹窗全局统一（三档宽度 + 头/体/脚规格）', () => {
  it.each([true, false])('关闭订单详情后直达相应群，是否已经入群：%s', async isMember => {
    vi.stubEnv('VITE_INTERNAL_CHAT_PREVIEW', '1')
    vi.stubEnv('VITE_INTERNAL_CHAT_LIVE', '1')
    const group = { id: 81, is_member: isMember, can_join: true, members: [] }
    fetchOrderChat.mockResolvedValue(group)
    joinOrderChat.mockResolvedValue({ ...group, is_member: true })
    const openChat = vi.fn()
    const modalVisibility: boolean[] = []
    const { wrapper } = await mountPage(undefined, openChat)
    const detailSelector = '[data-v2="dlg-detail"]'
    try {
      openChat.mockImplementation(() => {
        const detail = wrapper.find(detailSelector)
        modalVisibility.push(detail.exists() && detail.isVisible())
      })
      await rowOf(wrapper, ROW_A.order_no).get('[data-v2-detail]').trigger('click')
      await wrapper.get(`${detailSelector} [data-v2="tab-order-chat"]`).trigger('click')
      await flushPromises()
      const iframe = wrapper.get<HTMLIFrameElement>(`${detailSelector} iframe`).element
      const detailDialog = wrapper.getComponent(OrderDetailDialog).getComponent({ name: 'ElDialog' })
      for (const type of ['tt:order-history.ready', 'tt:order-history.activate']) {
        window.dispatchEvent(new MessageEvent('message', { data: { type }, origin: webKLiveOrigin, source: iframe.contentWindow }))
      }
      await flushPromises()
      expect(joinOrderChat).toHaveBeenCalledTimes(isMember ? 0 : 1)
      expect(openChat).not.toHaveBeenCalled()
      detailDialog.vm.$emit('closed')
      detailDialog.vm.$emit('closed')
      expect(openChat).toHaveBeenCalledExactlyOnceWith(81)
      expect(modalVisibility).toEqual([false])
    } finally {
      wrapper.unmount()
      vi.unstubAllEnvs()
    }
  })

  it.each([true, false])('无群旧订单的详情保持空历史且不自动加入，是否可首发：%s', async canSend => {
    vi.stubEnv('VITE_INTERNAL_CHAT_PREVIEW', '1')
    vi.stubEnv('VITE_INTERNAL_CHAT_LIVE', '1')
    fetchOrderChat.mockResolvedValue({
      id: null, has_conversation: false, title: '合成旧订单', order_no: ROW_A.order_no,
      local_order_no: ROW_A.order_no, members: [], self_member_id: null,
      is_member: false, can_join: false, can_send: canSend, archived: false, can_reopen: false,
    })
    const openChat = vi.fn()
    const { wrapper } = await mountPage(undefined, openChat)
    const detailSelector = '[data-v2="dlg-detail"]'
    try {
      await rowOf(wrapper, ROW_A.order_no).get('[data-v2-detail]').trigger('click')
      await wrapper.get(`${detailSelector} [data-v2="tab-order-chat"]`).trigger('click')
      await flushPromises()
      expect(wrapper.find(`${detailSelector} iframe`).exists()).toBe(false)
      const start = wrapper.get(`${detailSelector} [data-order-chat-start]`)
      expect(start.text()).toContain('暂无聊天消息')
      expect(start.find('textarea').exists()).toBe(canSend)
      expect(fetchOrderChat).toHaveBeenCalledExactlyOnceWith(ROW_A.order_no)
      expect(joinOrderChat).not.toHaveBeenCalled()
      expect(openChat).not.toHaveBeenCalled()
    } finally {
      wrapper.unmount()
      vi.unstubAllEnvs()
    }
  })

  it('确认类弹层 = sm 480：cv2-dialog 档位类（⭐ width prop 已摘除，宽度由档位类 + min(480,92vw) 接管）', async () => {
    const { wrapper } = await mountPage()
    await rowOf(wrapper, ROW_A.order_no).find('[data-v2-submit]').trigger('click')
    const confirm = wrapper.find('[data-v2="dlg-confirm"]')
    expect(confirm.classes()).toContain('cv2-dialog')
    expect(confirm.classes()).toContain('cv2-dialog--sm')
    await rowOf(wrapper, ROW_A.order_no).find('[data-v2-status]').trigger('click')
    const status = wrapper.find('[data-v2="dlg-status"]')
    expect(status.classes()).toContain('cv2-dialog--sm')
  })

  it('详情弹层走 detail 档（min(960px, 94vw)）；截图弹层 = lg 860；垂直居中', async () => {
    const { wrapper } = await mountPage()
    const row = rowOf(wrapper, ROW_A.order_no)
    await row.find('[data-v2-detail]').trigger('click')
    const detail = wrapper.find('[data-v2="dlg-detail"]')
    expect(detail.classes()).toContain('cv2-dialog--detail')
    // 宽度由 cv2-dialog--detail 接管。xl 1400 仍留给别的大窗。
    await row.find('[data-v2-shot-view]').trigger('click')
    const shot = wrapper.find('[data-v2="dlg-shot"]')
    expect(shot.classes()).toContain('cv2-dialog--lg')
  })

  it('CSS 形状钉（⭐ 2026-11-03 填充顶栏版）：头 11/18 主色实底・体 16/20・脚 12/20・标题 15px/600 白字・右上 ×；根 padding 0（顶栏贴死弹窗边）・详情体零内距；档位类 sm/md/lg 三档在 carbon.css', () => {
    expect(CARBON_CSS).toMatch(/\.ttv2 \.el-dialog__header\s*\{[^}]*padding: 11px 18px[^}]*background:\s*var\(--tt2-primary\)/)
    expect(CARBON_CSS).toMatch(/\.ttv2 \.el-dialog__title\s*\{[^}]*font-size: 15px[^}]*font-weight: 600[^}]*color:\s*var\(--tt2-on-primary\)/)
    expect(CARBON_CSS).toMatch(/\.ttv2 \.el-dialog\s*\{[^}]*padding: 0/)
    expect(CARBON_CSS).toMatch(/\.ttv2 \.el-dialog__body\s*\{[^}]*padding: 16px 20px/)
    expect(CARBON_CSS).toMatch(/\.ttv2 \.el-dialog__footer\s*\{[^}]*padding: 12px 20px/)
    expect(CARBON_CSS).toMatch(/\.ttv2 \.el-dialog__headerbtn\s*\{[^}]*top: 10px[^}]*right: 12px/)
    expect(CARBON_CSS).toMatch(/\.ttv2 \.el-dialog\.cv2-dialog--xl \.el-dialog__body\s*\{[^}]*padding: 0/)
    expect(CARBON_CSS).toMatch(/\.ttv2 \.el-dialog\s*\{[^}]*max-width: calc\(100vw - 32px\)/)
    // ⭐ 响应式窄档：档位宽 = min(档位, 92vw)——620px 窄面板下弹层不再横向溢出
    //（el-dialog 的 width prop 仍是定值、与档位类成对；prop 侧的断言在上面两条用例里照旧）。
    for (const [cls, w] of [['sm', 480], ['md', 640], ['lg', 860]] as const) {
      expect(CARBON_CSS).toMatch(
        new RegExp(`\\.cv2-dialog--${cls}\\s*\\{ width: min\\(${w}px, 92vw\\); \\}`),
      )
    }
  })
})

describe('V2MyOrders：权限门（fail-closed：没节点就不画）', () => {
  it('无 order.submit_work ⇒ 工具栏不画「更改状态」；状态签仍可点（判据在后端，同旧页）', async () => {
    const { wrapper } = await mountPage({
      menu_permissions: ['my_orders', 'order.view_screenshot', 'order.upload_screenshot'],
    })
    expect(wrapper.find('[data-v2="change-status"]').exists()).toBe(false)
    expect(rowOf(wrapper, ROW_A.order_no).find('[data-v2-status]').exists()).toBe(true)
  })

  it('无截图节点 ⇒ 截图列整列不画（th/td 都没有）；有节点 ⇒ 每行「传/看」两颗', async () => {
    const none = await mountPage({ menu_permissions: ['my_orders', 'order.submit_work'] })
    expect(none.wrapper.text()).not.toContain('订单截图')
    const both = await mountPage()
    const rowA = both.wrapper.find(`[data-v2-row="${ROW_A.order_no}"]`)
    expect(rowA.find('[data-v2-shot-upload]').exists()).toBe(true)
    expect(rowA.find('[data-v2-shot-view]').exists()).toBe(true)
    await rowA.find('[data-v2-shot-view]').trigger('click')
    const dlg = both.wrapper.find('[data-v2="dlg-shot"]')
    expect(dlg.exists()).toBe(true)
    const frame = dlg.find('iframe')
    expect(frame.attributes('src')).toContain('page=order-screenshots-manage')
    expect(frame.attributes('src')).toContain(`order_no=${ROW_A.order_no}`)
  })
})

describe('V2MyOrders：工具栏动作（推送双档 / 导出 CSV）', () => {
  async function checkRowA(): Promise<Awaited<ReturnType<typeof mountPage>>> {
    const page = await mountPage()
    await page.wrapper.find(`[data-v2-row-check="${ROW_A.order_no}"]`).setValue(true)
    return page
  }

  it('「同步订单状态」有勾选 ⇒ pushMemberJidanpuNow(nos, "status")（只推勾选的行）', async () => {
    const { wrapper } = await checkRowA()
    await wrapper.find('[data-v2="sync-status"]').trigger('click')
    await flushPromises()
    expect(pushMemberJidanpuNow).toHaveBeenCalledTimes(1)
    expect(pushMemberJidanpuNow).toHaveBeenCalledWith([ROW_A.order_no], 'status')
    expect(syncMemberJidanpuNow).not.toHaveBeenCalled()
  })

  it('「同步订单状态」无勾选 ⇒ 拉一轮（syncMemberJidanpuNow）；「发到我的管理系统」无勾选 ⇒ 只提示零请求', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="sync-status"]').trigger('click')
    await flushPromises()
    expect(syncMemberJidanpuNow).toHaveBeenCalledTimes(1)
    expect(pushMemberJidanpuNow).not.toHaveBeenCalled()

    await wrapper.find('[data-v2="push-record"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('请选中要操作的记录')
    expect(pushMemberJidanpuNow).not.toHaveBeenCalled()
  })

  it('导出 CSV：勾选优先；正文带 UTF-8 BOM、表头与勾选行的展示单号都在', async () => {
    const createObjectURL = vi.fn((_blob: Blob) => 'blob:mock')
    const revokeObjectURL = vi.fn()
    URL.createObjectURL = createObjectURL as unknown as typeof URL.createObjectURL
    URL.revokeObjectURL = revokeObjectURL as unknown as typeof URL.revokeObjectURL
    const { wrapper } = await checkRowA()
    await wrapper.find('[data-v2="export"]').trigger('click')
    expect(createObjectURL).toHaveBeenCalledTimes(1)
    const blob = createObjectURL.mock.calls[0]![0]
    // ⚠️ BOM 要打在**字节**上：jsdom/TextDecoder 的 `Blob.text()` 走 UTF-8 decode，会把行首
    // BOM 剥掉（浏览器同款行为）——`text()` 只用于看正文内容。
    const bytes = new Uint8Array(await blob.arrayBuffer())
    expect(Array.from(bytes.slice(0, 3)), '文件头必须是 UTF-8 BOM（EF BB BF）').toEqual([0xef, 0xbb, 0xbf])
    const text = await blob.text()
    expect(text).toContain('"订单号"')
    expect(text).toContain('SRC-001') // D-0301：导出的也是展示单号
    expect(text).toContain('是') // 行 A 加急 ⇒ 「加急」列 = 是
    expect(text).not.toContain(ROW_B.order_no) // 只导勾选的行
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock')
  })

  it('订单号蓝链 ⇒ 详情是暖米连体格（标题 = 展示单号，带上这一行已知的项目）', async () => {
    const { wrapper } = await mountPage()
    await rowOf(wrapper, ROW_A.order_no).find('[data-v2-detail]').trigger('click')
    const dlg = wrapper.find('[data-v2="dlg-detail"]')
    expect(dlg.exists()).toBe(true)
    expect(dlg.find('iframe').exists()).toBe(false)
    expect(dlg.find('[data-v2="order-detail-pane"]').exists()).toBe(true)
    expect(dlg.text()).toContain('SRC-001')
    await flushPromises()
    expect(dlg.text()).toContain('示例项目-陪玩上分')
    expect(dlg.text()).toContain('合成订单全文')
    expect(dlg.text()).toContain('合成代练要求')
    expect(dlg.text()).toContain('30 天')
    expect(dlg.text()).toContain('验收备注')
    expect(dlg.text()).toContain('8.80 元')
    expect(dlg.text()).toContain('2026-11-14 09:00')
    expect(dlg.text()).not.toContain('联系电话')
    expect(dlg.text()).not.toContain('操作日志')
    expect(dlg.find('[data-v2="tab-dlt"]').exists()).toBe(false)
    expect(fetchMyOrderDetail).toHaveBeenCalledExactlyOnceWith(ROW_A.order_no)
    expect(fetchOrder).not.toHaveBeenCalled()
    expect(fetchOrderAudit).not.toHaveBeenCalled()
  })

  it('非本人的详情显示拒绝，不回落到通用端点', async () => {
    fetchMyOrderDetail.mockRejectedValueOnce(new HttpError(403, '不能查看详情'))
    const { wrapper } = await mountPage()
    await rowOf(wrapper, ROW_A.order_no).find('[data-v2-detail]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toBe('这张单不是你接的，或无权查看')
    expect(fetchOrder).not.toHaveBeenCalled()
    expect(fetchOrderAudit).not.toHaveBeenCalled()
  })

  it('有详情权限的账号继续取全量详情和日志', async () => {
    const { wrapper } = await mountPage({ menu_permissions: ['my_orders', 'order.view_content'] })
    await rowOf(wrapper, ROW_A.order_no).find('[data-v2-detail]').trigger('click')
    await flushPromises()
    const dlg = wrapper.get('[data-v2="dlg-detail"]')
    expect(dlg.text()).toContain('全量订单内容')
    expect(dlg.text()).toContain('操作日志')
    expect(fetchOrder).toHaveBeenCalledExactlyOnceWith(ROW_A.order_no)
    expect(fetchOrderAudit).toHaveBeenCalledExactlyOnceWith(ROW_A.order_no)
    expect(fetchMyOrderDetail).not.toHaveBeenCalled()
  })
})

/* ── D-0351 内容区配色（业务方看线上反馈「更突出、更直观」）──────────────
 * 工具档按钮的落点钉在渲染结果（DOM），行 hover / 勾选 / 文字操作的「值」钉在 carbon.css
 * 源文本上（jsdom 不算 cascade，与 CarbonShell.spec 同一手法）——两类判据一起钉防单边漂移。 */
const CARBON_CSS = readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../carbon.css'),
  'utf8',
)

describe('V2MyOrders：顶部状态卡片已撤', () => {
  it('页面上不再渲染状态卡片', async () => {
    const { wrapper } = await mountPage()
    expect(wrapper.find('[data-v2="status-cards"]').exists()).toBe(false)
    expect(wrapper.find('.cv2-scard').exists()).toBe(false)
  })
})

describe('V2MyOrders：⭐ D-0371 状态签三件套（墨点 + 浅底）+ 筛选字段左置（CSS 形状钉）', () => {
  it('八个状态各挂自己的 token 类（未分配暖灰 / 待抢单紫 / 代练中绿 / 待验收橙红 / 已完成蓝 / 问题单深红 / 已暂停橙 / 已退单浅红）', async () => {
    const STATUSES = [
      'UNASSIGNED',
      'WAITING_GRAB',
      'IN_PROGRESS',
      'PENDING_REVIEW',
      'COMPLETED',
      'ISSUE',
      'PAUSED',
      'REFUNDED',
    ] as const
    const EXPECTED: Record<(typeof STATUSES)[number], string> = {
      UNASSIGNED: 'cv2-st--unassigned',
      WAITING_GRAB: 'cv2-st--pending',
      IN_PROGRESS: 'cv2-st--doing',
      PENDING_REVIEW: 'cv2-st--accept',
      COMPLETED: 'cv2-st--done',
      ISSUE: 'cv2-st--error',
      PAUSED: 'cv2-st--paused',
      REFUNDED: 'cv2-st--refund',
    }
    fetchAllMyOrders.mockResolvedValue(
      STATUSES.map((status, i) => ({
        ...ROW_A,
        id: i + 1,
        order_no: String(i + 1).padStart(20, '0'),
        source_order_no: '',
        status,
      })),
    )
    const { wrapper } = await mountPage()
    for (const status of STATUSES) {
      const no = String(STATUSES.indexOf(status) + 1).padStart(20, '0')
      expect(
        rowOf(wrapper, no).find('[data-v2-status]').classes(),
        `状态 ${status} 应挂 ${EXPECTED[status]}`,
      ).toContain(EXPECTED[status])
      expect(
        rowOf(wrapper, no).find('[data-v2-status]').classes(),
        `状态 ${status} 行内字挂 cv2-st--mark`,
      ).toContain('cv2-st--mark')
    }
    // 表格签文字前带墨点（aria-hidden 由模板保证；8 行 = 8 枚）。
    expect(wrapper.findAll('[data-v2-status] .cv2-dot')).toHaveLength(8)
    expect(wrapper.find('[data-v2-status] .cv2-dot').attributes('aria-hidden')).toBe('true')
    expect(wrapper.findAll('[data-v2-status].cv2-order-status')).toHaveLength(8)
    const style = CARBON_CSS.match(/\.cv2-st\.cv2-order-status\s*\{([^}]+)\}/)?.[1] ?? ''
    expect(style).toContain('height: 25px')
    expect(style).toContain('background: var(--order-status-bg')
    expect(CARBON_CSS).toMatch(/\.cv2-order-status \.cv2-dot\s*\{[^}]*background:\s*var\(--order-status-dot/)
  })

  it('筛选字段标签左置：.cv2-field = 两列 grid（标签左 13px dim、控件右）；筛选签两态 = 未选中穿自己浅底、悬停描边转墨点色、选中该状态深字色实底白字', () => {
    expect(CARBON_CSS).toMatch(
      /\.cv2-field\s*\{[^}]*display:\s*grid[^}]*grid-template-columns:\s*auto 1fr/,
    )
    expect(CARBON_CSS).toMatch(/\.cv2-field-label\s*\{[^}]*grid-column:\s*1/)
    expect(CARBON_CSS).toMatch(/\.cv2-field-label\s*\{[^}]*font-size:\s*13px/)
    expect(CARBON_CSS).toMatch(
      /\.cv2-filter \.cv2-field > \.cv2-field-label\s*\{[^}]*background:\s*var\(--tt2-filter-label-bg\)[^}]*border-right:\s*1px solid var\(--tt2-line\)/,
    )
    // 签选中实底（钉一枚代表 + 八类选择器齐全）。
    expect(CARBON_CSS).toMatch(
      /\.cv2-chip\.cv2-chip--on\.cv2-st--pending\s*\{[^}]*background:\s*var\(--tt2-st-pending\)[^}]*color:\s*var\(--tt2-panel\)/,
    )
    for (const st of ['unassigned', 'pending', 'doing', 'accept', 'done', 'refund', 'paused', 'error']) {
      expect(CARBON_CSS).toMatch(new RegExp(`\\.cv2-chip\\.cv2-chip--on\\.cv2-st--${st}\\s*\\{`))
      expect(CARBON_CSS).toMatch(new RegExp(`\\.cv2-chip\\.cv2-st--${st}:hover\\s*\\{`))
    }
    // 悬停 = 描边转墨点色（例举紫；底不再换）；选中规则源码顺序在悬停之后（同特异性靠顺序压住）。
    expect(CARBON_CSS).toMatch(
      /\.cv2-chip\.cv2-st--pending:hover\s*\{[^}]*border-color:\s*var\(--tt2-st-pending-dot\)/,
    )
    const hoverAt = CARBON_CSS.indexOf('.cv2-chip.cv2-st--pending:hover')
    const onAt = CARBON_CSS.indexOf('.cv2-chip.cv2-chip--on.cv2-st--pending')
    expect(onAt).toBeGreaterThan(hoverAt)
    // 「未选中压回白底」那组规则已删（未选中就穿 `.cv2-st--*` 自己的浅底）。
    expect(CARBON_CSS).not.toMatch(
      /\.cv2-chip\.cv2-st--[a-z]+\s*,[^)]*\{[^}]*background:\s*var\(--tt2-panel\)/,
    )
    // 选中签上的墨点转白 90%（实底上同色墨点会隐形）。
    expect(CARBON_CSS).toMatch(/\.cv2-chip--on \.cv2-dot\s*\{[^}]*background:\s*var\(--tt2-st-dot-on\)/)
  })

  it('⭐ D-0354 追记：筛选控件 EP 皮（34px/4px/#DDE3E0/hover 主色/focus 主色+3px 光圈/占位 dim）与 default 档浅青底（CSS 形状钉）', () => {
    // EP 皮：.ttv2 内 --el-* 重映射 + 专用皮块；面板留在作用域内（teleported=false 在页面模板）。
    expect(CARBON_CSS).toMatch(/--el-component-size:\s*34px/)
    expect(CARBON_CSS).toMatch(/--tt2-filter-line:\s*#dde3e0/)
    expect(CARBON_CSS).toMatch(/--tt2-focus-ring:\s*rgba\(11, 122, 112, 0.12\)/)
    expect(CARBON_CSS).toMatch(
      /\.ttv2 \.el-select__wrapper\s*\{[^}]*min-height:\s*34px[^}]*box-shadow: 0 0 0 1px var\(--tt2-filter-line\) inset/,
    )
    expect(CARBON_CSS).toMatch(
      /\.ttv2 \.el-select__wrapper\.is-focused\s*\{[^}]*0 0 0 3px var\(--tt2-focus-ring\)/,
    )
    expect(CARBON_CSS).toMatch(
      /\.ttv2 \.el-input__wrapper\.is-focus\s*\{[^}]*0 0 0 3px var\(--tt2-focus-ring\)/,
    )
    expect(CARBON_CSS).toMatch(
      /\.ttv2 \.el-input__inner::placeholder\s*\{[^}]*color:\s*var\(--tt2-dim\)/,
    )
    expect(CARBON_CSS).toMatch(
      /\.ttv2 \.el-select__placeholder\.is-transparent\s*\{[^}]*color:\s*var\(--tt2-dim\)/,
    )
    // default 档中性灰浅底（⭐ D-0371：暖白底/暖灰线，全系统不再有纯白按钮）
    // + hover 加深一档（soft-hover 档）仍墨字；基类 6px 圆角、500 字重。
    expect(CARBON_CSS).toMatch(/--tt2-btn-soft-bg:\s*#f7f6f2/)
    expect(CARBON_CSS).toMatch(/--tt2-btn-soft-line:\s*#d6d1c6/)
    expect(CARBON_CSS).toMatch(/--tt2-btn-soft-hover-bg:\s*#f0eee8/)
    expect(CARBON_CSS).toMatch(/--tt2-btn-soft-hover-line:\s*#afa89a/)
    expect(CARBON_CSS).toMatch(
      /\.cv2-btn\s*\{[^}]*border: 1px solid var\(--tt2-btn-soft-line\)[^}]*border-radius:\s*6px[^}]*font-weight:\s*500/,
    )
    expect(CARBON_CSS).toMatch(
      /\.cv2-btn:hover\s*\{[^}]*border-color:\s*var\(--tt2-btn-soft-hover-line\)[^}]*background:\s*var\(--tt2-btn-soft-hover-bg\)[^}]*color:\s*var\(--tt2-ink\)/,
    )
    // 工具档并入中性灰 default 皮（tool token = soft 同值；hover 不另写、走基类 soft-hover）。
    expect(CARBON_CSS).toMatch(/--tt2-tool-bg:\s*#f7f6f2/)
    expect(CARBON_CSS).toMatch(/--tt2-tool-line:\s*#d6d1c6/)
    expect(CARBON_CSS).toMatch(/\.cv2-btn--tool\s*\{[^}]*border-color:\s*var\(--tt2-tool-line\)[^}]*background:\s*var\(--tt2-tool-bg\)/)
    // 工具档自己不再写 hover（走基类）。重置钮在筛选条里单独悬停，跟管理订单一样。
    expect(CARBON_CSS).not.toMatch(/(^|\n)\.cv2-btn--tool:hover/)
    expect(CARBON_CSS).toMatch(/\.cv2-filter-actions \.cv2-btn--tool:hover/)
    // 主提交钮 = 红橙实底白字 600 + 轻投影（双主钮的另一半）。
    expect(CARBON_CSS).toMatch(
      /\.cv2-btn--submit\s*\{[^}]*background:\s*var\(--tt2-btn-submit\)[^}]*color:\s*var\(--tt2-on-primary\)[^}]*font-weight:\s*600[^}]*box-shadow:[^}]*var\(--tt2-btn-submit-shadow\)/,
    )
    expect(CARBON_CSS).toMatch(
      /\.cv2-btn--submit:hover\s*\{[^}]*background:\s*var\(--tt2-btn-submit-hover\)/,
    )
    // 功能 tint 两族（蓝=对外推送 / 琥珀=改状态；琥珀为独立 token，不再借 st-pending）。
    expect(CARBON_CSS).toMatch(/--tt2-tint-blue:\s*#16407e/)
    expect(CARBON_CSS).toMatch(/--tt2-tint-blue-bg:\s*#e3ebf8/)
    expect(CARBON_CSS).toMatch(/--tt2-tint-blue-line:\s*rgba\(30, 95, 196, 0.35\)/)
    expect(CARBON_CSS).toMatch(/--tt2-tint-amber:\s*#6b5400/)
    expect(CARBON_CSS).toMatch(/--tt2-tint-amber-bg:\s*#f5eac5/)
    expect(CARBON_CSS).toMatch(/--tt2-tint-amber-line:\s*rgba\(154, 123, 0, 0.42\)/)
    for (const tint of ['blue', 'amber']) {
      const re = new RegExp(
        `\\.cv2-btn--tint-${tint}\\s*\\{[^}]*background: var\\(--tt2-tint-${tint}-bg\\)[^}]*color: var\\(--tt2-tint-${tint}\\)`,
      )
      expect(CARBON_CSS).toMatch(re)
    }
    // tint-gray 三枚 token 已删；「导出」按钮的类保留但皮并入 default（无独立规则）。
    expect(CARBON_CSS).not.toMatch(/--tt2-tint-gray/)
    expect(CARBON_CSS).not.toMatch(/\.cv2-btn--tint-gray/)
  })

  it('横排动作是纯文字：悬停只出主题绿下划线，记退款和删除仍实心', () => {
    expect(CARBON_CSS).toMatch(/--tt2-btn-quiet:\s*#4a4742/)
    expect(CARBON_CSS).toMatch(
      /\.ttb-strip \.cv2-btn:not\(\.cv2-btn--solid-red\):not\(\.cv2-btn--solid-yellow\):hover\s*\{[^}]*background:\s*transparent[^}]*box-shadow:\s*inset 0 -2px 0 var\(--tt2-primary\)/,
    )
    expect(CARBON_CSS).toMatch(
      /\.ttb-strip \.cv2-btn\.cv2-btn--solid-red:hover\s*\{[^}]*background:\s*var\(--tt2-btn-solid-red\)/,
    )
    expect(CARBON_CSS).toMatch(
      /\.ttb-strip \.cv2-btn\.cv2-btn--solid-yellow:hover\s*\{[^}]*background:\s*var\(--tt2-btn-solid-yellow\)/,
    )
    expect(CARBON_CSS).not.toMatch(/\.cv2-toolbar > \.cv2-btn/)
  })
})

describe('V2MyOrders：D-0351 内容区配色（工具档 / 行 hover・勾选 / 行内文字操作）', () => {
  it('工具条跟管理订单同一排纯文字：不再按功能涂色；查询是青绿字，重置是灰字', async () => {
    const { wrapper } = await mountPage()
    for (const key of ['refresh', 'push-record', 'sync-status', 'change-status', 'export']) {
      const painted = wrapper.find(`[data-v2="${key}"]`).classes()
        .some((name) => /^cv2-btn--(tool|tint-|primary|danger|submit|solid-)/.test(name))
      expect(painted, `「${key}」走纯文字，不涂功能色`).toBe(false)
    }
    expect(wrapper.find('[data-v2="reset"]').classes()).toContain('cv2-btn--quiet')
    expect(wrapper.find('[data-v2="search"]').classes()).toContain('cv2-btn--find')
    expect(wrapper.find('[data-v2="search"]').classes()).toContain('cv2-btn--fill')
  })

  it('行内操作：传 / 看 = 图标幽灵钮 cv2-iconact（⭐ 2026-11-03 业务方「做成图标 类似对标那样」）；提交完成 = 小号红橙 submit 实底（D-0371）', async () => {
    const { wrapper } = await mountPage()
    const row = rowOf(wrapper, ROW_A.order_no)
    expect(row.find('[data-v2-shot-upload]').classes()).toContain('cv2-iconact')
    expect(row.find('[data-v2-shot-view]').classes()).toContain('cv2-iconact')
    expect(row.find('[data-v2-shot-upload]').text()).not.toContain('传')
    expect(row.find('[data-v2-shot-view]').text()).not.toContain('看')
    expect(
      row.find('[data-v2-shot-upload]').attributes('aria-label'),
      '图标钮无文字，语义必须走 aria-label',
    ).toBe('上传订单截图')
    expect(row.find('[data-v2-shot-view]').attributes('aria-label')).toBe('查看订单截图')
    expect(row.find('[data-v2-submit]').classes(), '提交完成应升级为小号按钮').toContain('cv2-btn--sm')
    expect(row.find('[data-v2-submit]').classes()).toContain('cv2-btn--submit')
    expect(CARBON_CSS).toMatch(/\.cv2-iconact\s*\{[^}]*color:\s*var\(--tt2-primary-deep\)/)
    expect(CARBON_CSS).toMatch(/\.cv2-actlink\s*\{[^}]*color:\s*var\(--tt2-primary-deep\)/)
  })

  it('行 hover = 琥珀晕、勾选行 = 浅琥珀底（CSS 值钉 + DOM 勾选类翻转都验）', async () => {
    expect(CARBON_CSS).toMatch(
      /\.cv2-table tbody tr:hover\s*\{[^}]*background:\s*var\(--tt2-hover-wash\)/,
    )
    expect(CARBON_CSS).toMatch(
      /\.cv2-table tbody tr\.cv2-row--checked\s*\{[^}]*background:\s*var\(--tt2-row-checked\)/,
    )
    const { wrapper } = await mountPage()
    expect(rowOf(wrapper, ROW_A.order_no).classes()).not.toContain('cv2-row--checked')
    await wrapper.find(`[data-v2-row-check="${ROW_A.order_no}"]`).setValue(true)
    expect(rowOf(wrapper, ROW_A.order_no).classes()).toContain('cv2-row--checked')
    await wrapper.find(`[data-v2-row-check="${ROW_A.order_no}"]`).setValue(false)
    expect(rowOf(wrapper, ROW_A.order_no).classes()).not.toContain('cv2-row--checked')
  })

  it('危险档 cv2-btn--danger = 红实底白字、hover 深红；全文件恰好一处定义（⭐ D-0371 修同名双定义：浅底版块已删）', () => {
    expect(CARBON_CSS).toMatch(/\.cv2-btn--danger\s*\{[^}]*background:\s*var\(--tt2-btn-danger\)/)
    expect(CARBON_CSS).toMatch(/\.cv2-btn--danger\s*\{[^}]*color:\s*var\(--tt2-on-primary\)/)
    expect(CARBON_CSS).toMatch(
      /\.cv2-btn--danger:hover\s*\{[^}]*background:\s*var\(--tt2-danger-hover\)/,
    )
    // 恰好一处：基定义只出现一次（bug 现场 = 实底版在前、浅底版在后按源码顺序覆盖）。
    expect(CARBON_CSS.match(/\.cv2-btn--danger\s*\{/g)).toHaveLength(1)
  })

  it('分页选中页码 = 青绿 deep 实底（EP 内核选中色在 .ttv2 内被覆盖到 primary-deep；白字对它过硬线）', () => {
    expect(CARBON_CSS).toMatch(
      /\.ttv2 \.el-pagination\.is-background \.el-pager li\.is-active\s*\{[^}]*background-color:\s*var\(--tt2-primary-deep\)/,
    )
  })
})
