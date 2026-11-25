// @vitest-environment jsdom
// ↑ 挂载组件需要 DOM（本仓 vitest 默认 environment 是 node，只在本文件覆盖）。
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/** 「录入订单」v2（V2OrderNew）的**保真网**（D-0355 全量重做轮）：业务方推翻上一版简化清单，口径 =「布局与功能与对标完全一致，炭火只换皮」。对标功能真源= 现役 island 复刻页 `layui-island/pages/order-addorder.ts`（对标的 1:1 复制）。本 spec五张网把「v2 实现 vs island 页」钉住：1. **字段恰好集合**——从 island 页/证据件提取的字段清单逐一对拍，漏一个红一个，且页面不得出现清单之外的字段锚点；2. **校验链逐条**——空提交被各闸拦 + 文案钉值（island onSave 同序）；3. **粘贴解析真交互**——粘贴样例 → /orders/parse/ mock → 各字段落位 + toast 钉值；4. **提交 mock 真交互**——载荷逐键钉值（含金额 '0' 默认/split/throw_to_hall/remember_assignee/sales_user）+ 成功 toast「录单成功」+ **原地复位不跳转**（island §七）；
 * 5. **销售客服网格**——持节点 radio 网格默认勾自己 / 不持节点只读文本（D-0208/B2）。⭐ 2026-11-01 信息架构重排轮（业务方行为模型驱动，D-0355 行内追记）追加两张网：6. **主流程/折叠两层归属**——「主流程区 ∪ 更多设置折叠区 = 重排前全集」的零丢失网（折叠 ≠ 省略：字段值照常进载荷）+ 折叠默认收起 / localStorage 记忆钉；⭐ 同日第二轮（业务方六项挪出折叠区：订单来源/拆分/加急/重要备注/内部派单/销售客服）只改两清单的成员，网本身不变；内部派单/销售客服挪进主流程后不再需要先点折叠头。7. **级联自动预选**——游戏选定（手动/识别回填）后区服自动预选第一项（业务方原话「把对应游戏的区服平台抄一份过来，默认自动选第一个」）；识别回填值 > 预选；手改不被覆盖。全部夹具都是**合成值**（PII 纪律）。 */
import { ElDialog, ElOption, ElSelect } from 'element-plus'

const fetchGames = vi.fn()
const fetchServers = vi.fn()
const fetchCategories = vi.fn()
const fetchProjects = vi.fn()
const fetchOrderSources = vi.fn()
const createOrder = vi.fn()
const fetchOrder = vi.fn()
const updateOrderWithMessage = vi.fn()
const changeSalesAgent = vi.fn()
const parsePastedText = vi.fn()
const readPlatformOrder = vi.fn()
const fetchAssigneeOptions = vi.fn()
const fetchKefuOptions = vi.fn()

vi.mock('@/api/config', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/config')>()),
  fetchGames: (...args: unknown[]) => fetchGames(...args),
  fetchServers: (...args: unknown[]) => fetchServers(...args),
  fetchCategories: (...args: unknown[]) => fetchCategories(...args),
  fetchProjects: (...args: unknown[]) => fetchProjects(...args),
}))
vi.mock('@/api/orderSources', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/orderSources')>()),
  fetchOrderSources: (...args: unknown[]) => fetchOrderSources(...args),
}))
vi.mock('@/api/orders', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/orders')>()),
  createOrder: (...args: unknown[]) => createOrder(...args),
  fetchOrder: (...args: unknown[]) => fetchOrder(...args),
  updateOrderWithMessage: (...args: unknown[]) => updateOrderWithMessage(...args),
  changeSalesAgent: (...args: unknown[]) => changeSalesAgent(...args),
  parsePastedText: (...args: unknown[]) => parsePastedText(...args),
  readPlatformOrder: (...args: unknown[]) => readPlatformOrder(...args),
  fetchAssigneeOptions: (...args: unknown[]) => fetchAssigneeOptions(...args),
  fetchKefuOptions: (...args: unknown[]) => fetchKefuOptions(...args),
}))

import V2OrderNew from '../V2OrderNew.vue'
import { NetworkError } from '@/api/errors'
import { useSessionStore } from '@/stores/session'
import { validateMoneyInput } from '@/config/money'

/* ── 合成夹具 ─────────────────────────────────────────────────────────────────── */

const GAMES = [
  { id: 2, name: '示例游戏一', sort_order: 1, is_active: true, created_at: '2026-10-01T00:00:00+08:00' },
  { id: 3, name: '示例游戏二', sort_order: 2, is_active: true, created_at: '2026-10-01T00:00:00+08:00' },
]
const SERVERS = [
  { id: 7, name: '区服甲', game: 2, sort_order: 1, is_active: true, created_at: '2026-10-01T00:00:00+08:00' },
]
const CATEGORIES = [
  { id: 4, name: '分类甲', game: 2, sort_order: 1, is_active: true, created_at: '2026-10-01T00:00:00+08:00', assigned_platforms: null, title_prefix: null, title_suffix: null, project_count: 0 },
]
const PROJECTS = [
  { id: 9, name: '项目甲', category: 4, sort_order: 1, is_active: true, created_at: '2026-10-01T00:00:00+08:00' },
  { id: 10, name: '项目乙', category: 4, sort_order: 2, is_active: true, created_at: '2026-10-01T00:00:00+08:00' },
]
const ORDER_SOURCES = {
  items: [
    { id: 20, name: '合成店铺A', remark: '', order_by: 1, shop_type: 0, group: null, group_name: '', is_active: true, created_at: '2026-10-01T00:00:00+08:00' },
  ],
  total: 1,
  page: 1,
  page_size: 200,
}
const ASSIGNEE_OPTIONS = {
  items: [
    { id: 5, name: '接手人乙', role: null, tag: '', remark: '' },
    { id: 6, name: '接手人丙', role: null, tag: '', remark: '' },
  ],
  truncated: false,
  roles: [],
  defAssRecManUid: 0,
  defAssRecMan: '',
  defAssIsHall: false,
}
const KEFU_OPTIONS = {
  items: [
    { id: 9, name: '测试甲' },
    { id: 11, name: '客服丙' },
  ],
  truncated: false,
}

/** 从 island 复刻页/证据件提取的**字段恰好清单**（anchor = v2 页的锚点，label = 对标文案）。 */
const ISLAND_FIELD_INVENTORY: readonly { anchor: string; label: string | null }[] = [
  // 左列：粘贴识别区
  { anchor: '[data-v2-field="content"]', label: '订单内容' },
  { anchor: '[data-v2-notopen="订单识别关键词设置"]', label: '识别不出来或有误？点击这里进行录单识别设置' },
  // ① 订单类型
  { anchor: '[data-v2-field="order-type"]', label: '订单类型' },
  // ② 游戏/区/平台/服（级联四连；后三格 = 对标有、我们没有 ⇒ 尚未开放壳照渲染）
  { anchor: '[data-v2-field="game"]', label: '游戏/区/平台/服' },
  { anchor: '[data-v2-field="server"]', label: null },
  { anchor: '[data-v2-notopen="服务器（服）"]', label: null },
  { anchor: '[data-v2-notopen="职业/门派"]', label: null },
  { anchor: '[data-v2-notopen="阵营门派"]', label: null },
  // ③ 游戏账号行（4 连）
  { anchor: '[data-v2-field="game_account"]', label: '游戏账号' },
  { anchor: '[data-v2-field="game_pwd"]', label: '登录密码' },
  { anchor: '[data-v2-field="game_sub_account"]', label: '子账号' },
  { anchor: '[data-v2-field="login_key"]', label: '暗号/口令' },
  // ④ 角色名行（3 连；mark1 label = 证据件服务端渲染值「开拓等级」）
  { anchor: '[data-v2-field="game_role_name"]', label: '角色名' },
  { anchor: '[data-v2-field="current_info_1"]', label: '开拓等级' },
  { anchor: '[data-v2-field="current_info_2"]', label: '当前信息2' },
  // ⑤ 项目分类 radio（分类加载后露面）
  { anchor: '[data-v2-field="category"]', label: '项目分类' },
  // ⑥ 常用项目卡（勾选列表 + 设置链接 + 权威文本框）
  { anchor: '[data-v2-field="projects"]', label: '常用代练/陪玩项目' },
  { anchor: '[data-v2-notopen="常用项目设置"]', label: '点击常用项目设置' },
  { anchor: '[data-v2-field="project_text"]', label: '代练项目/标题' },
  // ⑦ 拆分订单
  { anchor: '[data-v2-field="split"]', label: '是否拆分订单' },
  // ⑧ 时间要求行（双下拉 + 密保隐藏 + 来源 + 来源单号）
  { anchor: '[data-v2-field="day"]', label: '时间要求' },
  { anchor: '[data-v2-field="hours"]', label: null },
  { anchor: '[data-v2-hidden="safe-key-type"]', label: '密保类型' },
  { anchor: '[data-v2-field="order_source"]', label: '订单来源' },
  { anchor: '[data-v2-field="source_order_no"]', label: '来源单号' },
  // ⑨ 金额行（总价 + 接单实收款隐藏 + pushplat 五件隐藏）
  { anchor: '[data-v2-field="total_price"]', label: '订单总价' },
  { anchor: '[data-v2-field="received_price"]', label: '接单实收款' },
  { anchor: '[data-v2-field="platform_price"]', label: '发平台价格' },
  { anchor: '[data-v2-field="hall_price_inter"]', label: '发内部抢单大厅价' },
  { anchor: '[data-v2-field="safe_amount"]', label: '安全保证金' },
  { anchor: '[data-v2-field="eff_amount"]', label: '效率保证金' },
  { anchor: '[data-v2-field="push_template"]', label: '快速发单配置的要求' },
  // ⑩ 延迟发平台（隐藏行） ⑪ 支付方式行（隐藏行）
  { anchor: '[data-v2-field="delay_sec"]', label: '延迟发平台' },
  { anchor: '[data-v2-field="pay_method"]', label: '支付方式' },
  { anchor: '[data-v2-field="fee"]', label: '产生的手续费' },
  // ⑫ 联系三连
  { anchor: '[data-v2-field="customer_phone"]', label: '联系电话' },
  { anchor: '[data-v2-field="customer_qq"]', label: 'QQ/微信' },
  { anchor: '[data-v2-field="customer_ww"]', label: '旺旺/YY' },
  // ⑬ 会员号行（两格尚未开放）
  { anchor: '[data-v2-field="customer_name"]', label: '会员号/会员名' },
  { anchor: '[data-v2-field="vip_ded_check"]', label: '需要从余额扣款' },
  // ⑭ 备注 / 标记 / 加急
  { anchor: '[data-v2-field="remark"]', label: '重要备注' },
  { anchor: '[data-v2-field="tag"]', label: '自定义标记' },
  { anchor: '[data-v2-field="is_urgent"]', label: '是否加急' },
  // ⑮ 内部派单（按钮 + 发单价 + 默认指派此人）
  { anchor: '[data-v2="assign-button"]', label: '内部派单' },
  // ⑯ 接单销售客服 ⑰ 提交保存  页签
  { anchor: '[data-v2-field="sales_agent"]', label: '接单销售客服' },
  { anchor: '[data-v2="save-order"]', label: '提交保存' },
  { anchor: '[data-v2-tab="import"]', label: '导入文件订单' },
]

/** v2 页面上**允许存在**的 data-v2-field 锚点全集（恰好集合网的反向：不许有清单外锚点）。 */
const EXPECTED_FIELD_ANCHORS = new Set(
  ISLAND_FIELD_INVENTORY
    .map((entry) => /data-v2-field="([^"]+)"/.exec(entry.anchor)?.[1])
    .filter((a): a is string => a !== undefined),
)

function makeSession(permissions: string[]): void {
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
    menu_permissions: permissions,
    tenant_name: '示例工作室甲',
  }
  session.status = 'ready'
}

async function mountPage(
  permissions: string[] = ['order.add_edit', 'order.allow_change_sales_agent', 'order.send_to_grab_hall'],
  props?: { orderNo: string; allowSplitPrice?: boolean },
) {
  const pinia = createPinia()
  setActivePinia(pinia)
  makeSession(permissions)
  const wrapper = mount(V2OrderNew, { props, global: { plugins: [pinia] } })
  await flushPromises()
  return { wrapper }
}

/** 按 data-v2-field 锚点找 el-select 组件（EP 化后原生 setValue 不再适用）。 */
function selectByAnchor(
  wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper'],
  anchor: string,
) {
  const found = wrapper
    .findAllComponents(ElSelect)
    .find((c) => c.attributes('data-v2-field') === anchor)
  expect(found, `应存在 data-v2-field="${anchor}" 的 el-select`).toBeDefined()
  return found!
}

/** 裸 $emit 不内置等 nextTick ⇒ 每次 emit 后 flush 再断言。 */
async function chooseSelect(
  wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper'],
  anchor: string,
  value: unknown,
) {
  selectByAnchor(wrapper, anchor).vm.$emit('update:modelValue', value)
  await flushPromises()
}

/** 读订单来源下拉当前的选中值（modelValue；'' = 没选）。 */
function formSourceOf(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper']): unknown {
  return selectByAnchor(wrapper, 'order_source').props('modelValue')
}

function toastOf(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper']): string {
  const el = wrapper.find('[data-v2="toast"]')
  return el.exists() ? el.text() : ''
}

/** ⭐ 2026-11-03 反馈升级：成功 = 屏中大浮层（save-flash），错误仍走小 toast。 */
function flashOf(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper']): string {
  const el = wrapper.find('[data-v2="save-flash"]')
  return el.exists() ? el.text() : ''
}

async function fillMinimal(
  wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper'],
): Promise<void> {
  await chooseSelect(wrapper, 'game', 2)
  await wrapper.find('[data-v2-field="project_text"]').setValue('示例项目')
  await wrapper.find('[data-v2-field="total_price"]').setValue('100')
}

/** （2026-11-01 第二轮后无展开助手）原先有个 `openMore` 助手：内部派单按钮等点击型交互住折叠区时先点折叠头、跟真人动线一致。同日第二轮业务方把内部派单/销售客服挪进主流程后，折叠区不再剩任何点击型交互（其余控件 jsdom 直 setValue），助手随之删除——折叠默认收起 / localStorage 记忆仍由网 6 的专用用例直接点折叠头钉住。
 * （2026-11-01 第二轮后无展开助手）原先有个 `openMore` 助手：内部派单按钮等点击型交互住折叠区时先点折叠头、跟真人动线一致。同日第二轮业务方把内部派单/销售客服挪进主流程后，折叠区不再剩任何点击型交互（其余控件 jsdom 直 setValue），助手随之删除——折叠默认收起 / localStorage 记忆仍由网 6 的专用用例直接点折叠头钉住。 */

beforeEach(() => {
  vi.clearAllMocks()
  // localStorage 跨用例共享同一 jsdom ⇒ 每例清掉，折叠态从「默认收起」起算。
  localStorage.clear()
  fetchGames.mockResolvedValue(GAMES)
  fetchServers.mockResolvedValue(SERVERS)
  fetchCategories.mockResolvedValue(CATEGORIES)
  fetchProjects.mockResolvedValue(PROJECTS)
  fetchOrderSources.mockResolvedValue(ORDER_SOURCES)
  fetchAssigneeOptions.mockResolvedValue(ASSIGNEE_OPTIONS)
  fetchKefuOptions.mockResolvedValue(KEFU_OPTIONS)
  createOrder.mockResolvedValue({ order_no: '11111111111111111111' })
})

describe('V2OrderNew 编辑订单保存', () => {
  const orderNo = '11111111111111111111'
  const detail = {
    order_no: orderNo, order_type: 'BOOST', game: 2, server: 7, category: null,
    project_text: '合成项目', total_price: '100.00', time_requirement: '',
    sales_user: 11, sales_agent: '旧昵称', order_source: 20,
  }
  const permissions = ['order.submit_edit', 'order.allow_change_sales_agent']
  const changeButton = '[data-v2="sales-agent-change"]'
  const current = '[data-v2="sales-agent-current"]'
  const saveButton = '[data-v2="save-order"]'

  beforeEach(() => {
    fetchOrder.mockReset().mockResolvedValue(detail)
    updateOrderWithMessage.mockReset().mockResolvedValue({ order: detail, message: '' })
    changeSalesAgent.mockReset().mockResolvedValue({ ...detail, sales_user: 12 })
    fetchKefuOptions.mockResolvedValue({
      items: [{ id: 11, name: '客服同名' }, { id: 12, name: '客服同名' }],
      truncated: false,
    })
  })

  async function chooseAnother(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper']) {
    await wrapper.get(changeButton).trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-v2-sales-agent="11"]').classes()).toContain('cv2-btn--primary')
    expect(wrapper.get('[data-v2-sales-agent="12"]').text()).toContain('账号 ID 12')
    await wrapper.get('[data-v2-sales-agent="12"]').trigger('click')
  }

  it('只改备注时，保留同游戏内原有的订单分类和常用项目', async () => {
    fetchCategories.mockResolvedValueOnce([
      ...CATEGORIES,
      { ...CATEGORIES[0], id: 5, name: '分类乙' },
    ])
    const project = { ...PROJECTS[0], id: 12, name: '项目丙', category: 5 }
    const catalog = [...PROJECTS, project]
    fetchProjects.mockImplementationOnce(async (category: number) =>
      catalog.filter((row) => row.category === category),
    )
    fetchOrder.mockResolvedValueOnce({ ...detail, category: 4, project: project.id, remark: '原备注' })
    const { wrapper } = await mountPage(['order.submit_edit'], { orderNo })
    expect(wrapper.find('[data-v2-project="9"]').exists()).toBe(true)
    expect(wrapper.find('[data-v2-project="12"]').exists()).toBe(false)

    await wrapper.get('[data-v2-field="remark"]').setValue('修改后的备注')
    await wrapper.get(saveButton).trigger('click')
    await flushPromises()

    expect(updateOrderWithMessage).toHaveBeenCalledExactlyOnceWith(orderNo, expect.objectContaining({
      category: 4,
      project: 12,
      remark: '修改后的备注',
    }))
    wrapper.unmount()
  })

  it('按账号主键选中原客服，改同名账号也会在普通字段保存后换归属', async () => {
    updateOrderWithMessage.mockResolvedValue({ order: detail, message: '平台来源未更新' })
    const { wrapper } = await mountPage(permissions, { orderNo })
    expect(wrapper.get(current).text()).toBe('旧昵称')
    await chooseAnother(wrapper)
    await wrapper.get(saveButton).trigger('click')
    await flushPromises()
    expect(updateOrderWithMessage.mock.calls[0]?.[0]).toBe(orderNo)
    expect(updateOrderWithMessage.mock.calls[0]?.[1]).not.toHaveProperty('sales_agent')
    expect(updateOrderWithMessage.mock.calls[0]?.[1]).not.toHaveProperty('sales_user')
    expect(changeSalesAgent).toHaveBeenCalledExactlyOnceWith(orderNo, { user: 12 })
    expect(updateOrderWithMessage.mock.invocationCallOrder[0]).toBeLessThan(changeSalesAgent.mock.invocationCallOrder[0]!)
    expect(wrapper.emitted('saved')).toEqual([['平台来源未更新']])
    wrapper.unmount()
  })

  it('历史显示名没有归属主键时不自动指定同名账号，只读账号照样能保存订单', async () => {
    fetchOrder.mockResolvedValue({ ...detail, sales_user: null, sales_agent: '客服同名' })
    const { wrapper } = await mountPage(['order.submit_edit'], { orderNo })
    expect(wrapper.get('[data-v2="sales-agent-readonly"]').text()).toBe('客服同名')
    expect(wrapper.find(changeButton).exists()).toBe(false)
    expect(fetchKefuOptions).not.toHaveBeenCalled()
    await wrapper.get(saveButton).trigger('click')
    await flushPromises()
    expect(updateOrderWithMessage.mock.calls[0]?.[1]).not.toHaveProperty('sales_agent')
    expect(changeSalesAgent).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('普通字段失败时不换归属，换归属失败时保留弹窗并显示已确认的原客服', async () => {
    const { wrapper } = await mountPage(permissions, { orderNo })
    await chooseAnother(wrapper)
    updateOrderWithMessage.mockRejectedValueOnce(new Error('保存失败'))
    await wrapper.get(saveButton).trigger('click')
    await flushPromises()
    expect(changeSalesAgent).not.toHaveBeenCalled()
    changeSalesAgent.mockRejectedValueOnce(new Error('该单客服提成已结账'))
    await wrapper.get(saveButton).trigger('click')
    await flushPromises()
    expect(wrapper.get(current).text()).toBe('旧昵称')
    expect(toastOf(wrapper)).toContain('订单已保存，销售客服未更换')
    expect(wrapper.emitted('saved')).toBeUndefined()
    wrapper.unmount()
  })

  it('换归属请求超时后，确认写入时带上原警告关闭，无法回读时提示重新打开核对', async () => {
    updateOrderWithMessage.mockResolvedValue({ order: detail, message: '平台来源未更新' })
    const { wrapper } = await mountPage(permissions, { orderNo })
    await chooseAnother(wrapper)
    changeSalesAgent.mockRejectedValueOnce(new NetworkError('请求超时'))
    fetchOrder.mockRejectedValueOnce(new NetworkError('请求超时'))
    await wrapper.get(saveButton).trigger('click')
    await flushPromises()
    expect(toastOf(wrapper)).toContain('暂时无法确认销售客服是否已更换')
    expect(wrapper.emitted('saved')).toBeUndefined()

    await chooseAnother(wrapper)
    changeSalesAgent.mockRejectedValueOnce(new NetworkError('请求超时'))
    fetchOrder.mockResolvedValueOnce({ ...detail, sales_user: 12, sales_agent: '客服同名' })
    await wrapper.get(saveButton).trigger('click')
    await flushPromises()
    expect(fetchOrder).toHaveBeenCalledTimes(3)
    expect(wrapper.emitted('saved')).toEqual([['平台来源未更新']])
    wrapper.unmount()
  })
})

/* ── 网 1：字段恰好集合 ──────────────────────────────────────────────────────── */

describe('V2OrderNew 保真网①：island 页字段/功能全量清单恰好集合', () => {
  it('清单里每一条都在页面上（锚点存在 + 标签文案钉值），漏一个红一个', async () => {
    const { wrapper } = await mountPage()
    const missing: string[] = []
    for (const entry of ISLAND_FIELD_INVENTORY) {
      const el = wrapper.find(entry.anchor)
      if (!el.exists()) {
        missing.push(entry.anchor)
        continue
      }
      if (entry.label !== null && !wrapper.text().includes(entry.label)) {
        missing.push(`${entry.anchor}（缺标签「${entry.label}」）`)
      }
    }
    expect(missing, `island 页字段清单缺了 ${missing.length} 条`).toEqual([])
  })

  it('页面上没有清单之外的 data-v2-field 锚点（多一个也是漂移）', async () => {
    const { wrapper } = await mountPage()
    const html = wrapper.html()
    const actual = new Set(
      Array.from(html.matchAll(/data-v2-field="([^"]+)"/g)).map((m) => m[1] as string),
    )
    const extra = [...actual].filter((a) => !EXPECTED_FIELD_ANCHORS.has(a))
    expect(extra, '清单之外的字段锚点').toEqual([])
    // 反向：清单里的 field 锚点也必须都在（与上一条合起来 = 恰好集合）。
    const missing = [...EXPECTED_FIELD_ANCHORS].filter((a) => !actual.has(a))
    expect(missing, '清单里的字段锚点没渲染出来').toEqual([])
  })

  it('⭐ D-0371 提交保存 = cv2-btn--submit（红橙实底主提交钮；「查询」类页面 primary 不变，双主钮分工）', async () => {
    const { wrapper } = await mountPage()
    const save = wrapper.find('[data-v2="save-order"]')
    expect(save.classes()).toContain('cv2-btn--submit')
    expect(save.classes()).not.toContain('cv2-btn--primary')
  })

  it('粘贴区的两条提示逐字在页上（placeholder + 识别设置链接 + 权威文本框 placeholder）', async () => {
    const { wrapper } = await mountPage()
    const textarea = wrapper.find('[data-v2-field="content"]').element as HTMLTextAreaElement
    expect(textarea.placeholder).toBe(
      '可以把已经格式化好的订单文本内容直接粘贴到这里，然后用鼠标点一下框框外面任何地方，即可自动识别出订单各个内容',
    )
    expect(textarea.getAttribute('placeholder')).toContain('即可自动识别出订单各个内容')
    // 权威文本框（#project）的 placeholder 与 900 上限（D-0218）逐字。
    const project = wrapper.find('[data-v2-field="project_text"]').element as HTMLInputElement
    expect(project.placeholder).toBe(
      '可选择上面的项目，也可以直接在此输入项目，若有多个项目，请用/号分隔，最终代练/陪玩项目以这里填写的为准...',
    )
    expect(project.maxLength).toBe(900)
  })

  it('时间要求双下拉的选项逐值对齐证据件（天 0–365 默认 30；小时 0–23 步 0.5 默认 0）', async () => {
    const { wrapper } = await mountPage()
    // EP el-select 没有原生 <select> ⇒ 选项逐值对拍走组件树（ElOption 子树 + modelValue）。
    const daySelect = selectByAnchor(wrapper, 'day')
    const hourSelect = selectByAnchor(wrapper, 'hours')
    expect(daySelect.props('modelValue')).toBe('30')
    expect(hourSelect.props('modelValue')).toBe('0')
    const dayOptions = daySelect.findAllComponents(ElOption)
    const hourOptions = hourSelect.findAllComponents(ElOption)
    expect(dayOptions.length).toBe(366)
    expect(dayOptions[0]!.props('label')).toBe('0天')
    expect(dayOptions[dayOptions.length - 1]!.props('label')).toBe('365天')
    expect(hourOptions.length).toBe(47)
    expect(hourOptions[0]!.props('label')).toBe('0小时')
    expect(hourOptions[1]!.props('label')).toBe('0.5小时')
    expect(hourOptions[hourOptions.length - 1]!.props('label')).toBe('23小时')
  })

  it('金额六格的初值 = 证据件 value="0"（island 模型从 DOM 起家；载荷因此是 0 不是缺键）', async () => {
    const { wrapper } = await mountPage()
    for (const anchor of ['total_price', 'received_price', 'platform_price', 'hall_price_inter', 'fee']) {
      const el = wrapper.find(`[data-v2-field="${anchor}"]`).element as HTMLInputElement
      expect(el.value).toBe('0')
    }
  })
})

/* ── 网 2：校验链（island onSave 同序同文案）────────────────────────────────── */

describe('V2OrderNew 保真网②：校验链逐条（空提交被各闸拦 + 文案钉值）', () => {
  it('没有可用游戏时保存 ⇒ 「请选择游戏」，createOrder 零调用', async () => {
    fetchGames.mockResolvedValueOnce([])
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    expect(toastOf(wrapper)).toBe('请选择游戏')
    expect(createOrder).not.toHaveBeenCalled()
  })

  it('选了游戏但项目文本为空 ⇒ 「代练/陪玩项目不可为空」（Python strip 判空口径）', async () => {
    const { wrapper } = await mountPage()
    await chooseSelect(wrapper, 'game', 2)
    await wrapper.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    expect(toastOf(wrapper)).toBe('代练/陪玩项目不可为空')
    expect(createOrder).not.toHaveBeenCalled()
  })

  it('必填齐但总价被清空 ⇒ 「订单总价不可为空」（D-0082；isBlankLikePython 判空而非真值）', async () => {
    const { wrapper } = await mountPage()
    await fillMinimal(wrapper)
    await wrapper.find('[data-v2-field="total_price"]').setValue('')
    await wrapper.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    expect(toastOf(wrapper)).toBe('订单总价不可为空')
    expect(createOrder).not.toHaveBeenCalled()
  })

  it('金额非法 ⇒ 第一个金额错误（顺序 = ORDER_MONEY_FIELDS；文案 = validateMoneyInput 本身）', async () => {
    const { wrapper } = await mountPage()
    await fillMinimal(wrapper)
    await wrapper.find('[data-v2-field="total_price"]').setValue('abc')
    await wrapper.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    const moneyResult = validateMoneyInput('abc')
    if (moneyResult.ok) throw new Error('合成值 abc 应被判非法（探针自证）')
    expect(toastOf(wrapper)).toBe(moneyResult.msg)
    expect(createOrder).not.toHaveBeenCalled()
  })

  it('700 字节同步闸（E-0242）排在金额之后：合法金额 + 超限文本 ⇒ 同步闸文案、零请求', async () => {
    const { wrapper } = await mountPage()
    await fillMinimal(wrapper)
    // 240 个汉字 = 720 字节（UTF-8 一字 3 字节）> 700。
    await wrapper.find('[data-v2-field="project_text"]').setValue('项'.repeat(240))
    await wrapper.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    expect(toastOf(wrapper)).toContain('代练项目超出鸡蛋铺同步上限')
    expect(createOrder).not.toHaveBeenCalled()
  })

  it('无大厅发单权限时忽略已记住的内部抢单选项，可照常录单', async () => {
    fetchAssigneeOptions.mockResolvedValue({ ...ASSIGNEE_OPTIONS, defAssIsHall: true })
    const { wrapper } = await mountPage(['order.add_edit'])
    await fillMinimal(wrapper)

    await wrapper.get('[data-v2="assign-button"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-v2="assign-button"]').text()).toBe('暂不指派')
    expect(wrapper.find('[data-v2-row="throw-fee"]').exists()).toBe(false)

    await wrapper.get('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    expect(createOrder).toHaveBeenCalledTimes(1)
    expect(createOrder.mock.calls[0]?.[0]).not.toHaveProperty('throw_to_hall')
    expect(createOrder.mock.calls[0]?.[0]).not.toHaveProperty('remember_assignee')
  })

  it('内部抢单档没填发单价 ⇒ 「内部抢单请先填发单价（必须大于 0）」，一个请求都不发（R3 前端闸）', async () => {
    fetchAssigneeOptions.mockResolvedValue({ ...ASSIGNEE_OPTIONS, defAssIsHall: true })
    const { wrapper } = await mountPage()
    await fillMinimal(wrapper)
    expect(wrapper.find('[data-v2="assign-button"]').text()).toBe('内部抢单')
    await wrapper.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    expect(toastOf(wrapper)).toBe('内部抢单请先填发单价（必须大于 0）')
    expect(createOrder).not.toHaveBeenCalled()
  })
})

/* ── 网 3：粘贴解析真交互 ───────────────────────────────────────────────────── */

describe('V2OrderNew 保真网③：粘贴解析（失焦识别 + 自动认游戏 + 触发点③）', () => {
  it('粘贴样例 → /orders/parse/ → 各字段落位 + toast「已识别：…」（只写解析出来的项）', async () => {
    const { wrapper } = await mountPage()
    await chooseSelect(wrapper, 'game', 2)
    parsePastedText.mockResolvedValue({
      fields: {
        game_name: '示例游戏一',
        server_name: '区服甲',
        game_account: 'acc_001',
        game_role_name: 'role_01',
        customer_phone: '13800000000',
        total_price: '88.00',
        project_text: '示例粘贴项目',
        source_order_no: 'SYNTH-PLATFORM-0001',
      },
      matched: ['game', 'server'],
      unmatched: [],
    })
    await wrapper.find('[data-v2-field="content"]').setValue('示例粘贴内容')
    await wrapper.find('[data-v2-field="content"]').trigger('blur')
    await flushPromises()
    expect(parsePastedText).toHaveBeenCalledWith('示例粘贴内容')
    // 解析值进了各自的输入框（island `applyPasteToDom` 的落地面）。
    expect((wrapper.find('[data-v2-field="game_account"]').element as HTMLInputElement).value).toBe('acc_001')
    expect((wrapper.find('[data-v2-field="game_role_name"]').element as HTMLInputElement).value).toBe('role_01')
    expect((wrapper.find('[data-v2-field="customer_phone"]').element as HTMLInputElement).value).toBe('13800000000')
    expect((wrapper.find('[data-v2-field="total_price"]').element as HTMLInputElement).value).toBe('88.00')
    expect((wrapper.find('[data-v2-field="project_text"]').element as HTMLInputElement).value).toBe('示例粘贴项目')
    expect((wrapper.find('[data-v2-field="source_order_no"]').element as HTMLInputElement).value).toBe('SYNTH-PLATFORM-0001')
    // toast 钉值：已识别清单（后端字段名，与 island 同序同文案）。
    expect(toastOf(wrapper)).toContain('已识别：')
    expect(toastOf(wrapper)).toContain('game_account')
  })

  it('解析没带游戏名时按整段原文认游戏（命中即切游戏并重建级联）；多款 ⇒ 平手提示拼在最前', async () => {
    const { wrapper } = await mountPage()
    parsePastedText.mockResolvedValue({
      fields: { game_account: 'acc_002' },
      matched: [],
      unmatched: [],
    })
    await wrapper.find('[data-v2-field="content"]').setValue('来一单示例游戏二，急')
    await wrapper.find('[data-v2-field="content"]').trigger('blur')
    await flushPromises()
    // 自动切到示例游戏二（id 3）+ 级联重拉。
    expect(fetchServers).toHaveBeenLastCalledWith(3)
    expect(fetchCategories).toHaveBeenLastCalledWith(3)
    // island 粘贴路径不弹「已自动选择游戏」（那是读平台订单那条路的文案）——
    // 游戏命中以「已识别：game」呈现在同一句 toast 里。
    expect(toastOf(wrapper)).toContain('已识别：')
    expect(toastOf(wrapper)).toContain('game')

    // 多款平手：不猜，提示请手动选择。
    const { wrapper: w2 } = await mountPage()
    parsePastedText.mockResolvedValue({
      fields: { game_account: 'acc_003' },
      matched: [],
      unmatched: [],
    })
    await w2.find('[data-v2-field="content"]').setValue('示例游戏一 示例游戏二')
    await w2.find('[data-v2-field="content"]').trigger('blur')
    await flushPromises()
    expect(toastOf(w2)).toContain('出现了多款游戏')
    expect(toastOf(w2)).toContain('请手动选择游戏')
  })

  it('解析回填出来源单号 ⇒ 触发点③：读一次平台订单（按次计费，没出单号不发）', async () => {
    const { wrapper } = await mountPage()
    parsePastedText.mockResolvedValue({
      fields: { game_account: 'acc_004', source_order_no: 'SYNTH-PLATFORM-0002' },
      matched: [],
      unmatched: [],
    })
    await wrapper.find('[data-v2-field="content"]').setValue('示例粘贴内容')
    await wrapper.find('[data-v2-field="content"]').trigger('blur')
    await flushPromises()
    expect(readPlatformOrder).toHaveBeenCalledTimes(1)

    readPlatformOrder.mockClear()
    const { wrapper: w2 } = await mountPage()
    parsePastedText.mockResolvedValue({
      fields: { game_account: 'acc_005' },
      matched: [],
      unmatched: [],
    })
    await w2.find('[data-v2-field="content"]').setValue('示例粘贴内容')
    await w2.find('[data-v2-field="content"]').trigger('blur')
    await flushPromises()
    expect(readPlatformOrder).not.toHaveBeenCalled()
  })

  it('区服名在新游戏的列表里才落位；不在 ⇒ 「没能对应上」如实报出，不静默', async () => {
    const { wrapper } = await mountPage()
    // 示例游戏二的区服列表为空 ⇒ 粘贴文本里的「区服甲」对不上。
    fetchServers.mockImplementation(async (gameId: number) =>
      gameId === 3 ? [] : SERVERS,
    )
    parsePastedText.mockResolvedValue({
      fields: { game_name: '示例游戏二', server_name: '区服甲' },
      matched: [],
      unmatched: [],
    })
    await wrapper.find('[data-v2-field="content"]').setValue('示例粘贴内容')
    await wrapper.find('[data-v2-field="content"]').trigger('blur')
    await flushPromises()
    expect(toastOf(wrapper)).toContain('没能对应上：区服「区服甲」')
  })
})

/* ── 网 3b：读平台订单（触发点①②）─────────────────────────────────────────── */

describe('V2OrderNew 保真网③b：读平台订单（单号门槛 + 回填四条 + 来源判定）', () => {
  const PLATFORM_FIELDS = {
    payment: '66.00',
    buyer_nick: '买家昵称甲',
    customer_phone: '13900000000',
    remark: '平台商品标题甲',
    order_status: 1,
    pay_time: 0,
    order_source: null,
    order_source_name: '',
    platform: 'agiso',
  }

  it('来源单号 <15 字 ⇒ 一次请求都不发（对标 L1666）；≥15 ⇒ 按自动判定（没指定来源 ⇒ 不发来源）', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-field="source_order_no"]').setValue('短单号')
    await wrapper.find('[data-v2-field="source_order_no"]').trigger('change')
    await flushPromises()
    expect(readPlatformOrder).not.toHaveBeenCalled()

    readPlatformOrder.mockClear()
    readPlatformOrder.mockResolvedValue(PLATFORM_FIELDS)
    await wrapper.find('[data-v2-field="source_order_no"]').setValue('SYNTH-LONG-ORDER-NO-0001')
    await wrapper.find('[data-v2-field="source_order_no"]').trigger('change')
    await flushPromises()
    // 首屏默认第一条来源只是"本页默认"不是客服指定 ⇒ 请求里不带 order_source（自动判定）。
    expect(readPlatformOrder).toHaveBeenCalledWith(null, 'SYNTH-LONG-ORDER-NO-0001')
  })

  it('商品内容触发自动切游戏时，提示核对分类而不声称尚未选择', async () => {
    const { wrapper } = await mountPage()
    readPlatformOrder.mockResolvedValue({ ...PLATFORM_FIELDS, remark: '示例游戏二 商品' })
    await wrapper.find('[data-v2-field="source_order_no"]').setValue('SYNTH-LONG-ORDER-NO-0001')
    await wrapper.find('[data-v2-field="source_order_no"]').trigger('change')
    await flushPromises()
    expect(toastOf(wrapper)).toContain('请核对区服和项目分类')
    expect(toastOf(wrapper)).not.toContain('项目分类请重新选择')
    expect((wrapper.find('[data-v2-field="category"] input[value="4"]').element as HTMLInputElement).checked).toBe(true)
  })

  it('回填四条逐条钉值：价格非空才写、旺旺仅空时填、电话非空才写、需求空才填', async () => {
    const { wrapper } = await mountPage()
    readPlatformOrder.mockResolvedValue(PLATFORM_FIELDS)
    await wrapper.find('[data-v2-field="source_order_no"]').setValue('SYNTH-LONG-ORDER-NO-0002')
    await wrapper.find('[data-v2-field="source_order_no"]').trigger('change')
    await flushPromises()
    expect((wrapper.find('[data-v2-field="total_price"]').element as HTMLInputElement).value).toBe('66.00')
    expect((wrapper.find('[data-v2-field="customer_ww"]').element as HTMLInputElement).value).toBe('买家昵称甲')
    expect((wrapper.find('[data-v2-field="customer_phone"]').element as HTMLInputElement).value).toBe('13900000000')
    // 商品标题不再进需求框（2026-11-05 业务方拍板：随单保存、打手可见）⇒ 保持为空。
    expect((wrapper.find('[data-v2-field="content"]').element as HTMLTextAreaElement).value).toBe('')

    // 已填的旺旺/内容不被覆盖（仅空时填）。
    const { wrapper: w2 } = await mountPage()
    await w2.find('[data-v2-field="customer_ww"]').setValue('客服已填昵称')
    await w2.find('[data-v2-field="content"]').setValue('客服已敲的需求，足够长不会被覆盖')
    readPlatformOrder.mockResolvedValue(PLATFORM_FIELDS)
    await w2.find('[data-v2-field="source_order_no"]').setValue('SYNTH-LONG-ORDER-NO-0003')
    await w2.find('[data-v2-field="source_order_no"]').trigger('change')
    await flushPromises()
    expect((w2.find('[data-v2-field="customer_ww"]').element as HTMLInputElement).value).toBe('客服已填昵称')
    expect((w2.find('[data-v2-field="content"]').element as HTMLTextAreaElement).value).toBe('客服已敲的需求，足够长不会被覆盖')
  })

  it('客服自己选了来源 ⇒ 请求带上那个来源 id；清空来源 ⇒ 读单函数直接返回（不发请求）', async () => {
    const { wrapper } = await mountPage()
    readPlatformOrder.mockResolvedValue(PLATFORM_FIELDS)
    await wrapper.find('[data-v2-field="source_order_no"]').setValue('SYNTH-LONG-ORDER-NO-0010')
    // 客服点了下拉（用户操作 ⇒ orderSourceIsAuto=false）⇒ 触发点②带刚选的来源读一次。
    await chooseSelect(wrapper, 'order_source', 20)
    await flushPromises()
    expect(readPlatformOrder).toHaveBeenCalledWith(20, 'SYNTH-LONG-ORDER-NO-0010')

    // 清空（'' ⇒ null）⇒ `sourceOverride === null` 直接 return，一个请求都不发
    // （自动判定不许把刚清掉的来源又顶回来）。
    readPlatformOrder.mockClear()
    await chooseSelect(wrapper, 'order_source', '')
    await flushPromises()
    expect(readPlatformOrder).not.toHaveBeenCalled()
    expect(formSourceOf(wrapper)).toBe('')
  })

  it('读单失败：开口面（1015/1028）弹后端 msg；其它业务码静默；传输层错误只进 console', async () => {
    const { BizError } = await import('@/api/errors')
    const { wrapper } = await mountPage()
    readPlatformOrder.mockRejectedValue(new BizError(1015, '这家店还没配好平台对接，请手动选择订单来源'))
    await wrapper.find('[data-v2-field="source_order_no"]').setValue('SYNTH-LONG-ORDER-NO-0004')
    await wrapper.find('[data-v2-field="source_order_no"]').trigger('change')
    await flushPromises()
    expect(toastOf(wrapper)).toContain('请手动选择订单来源')

    const { wrapper: w2 } = await mountPage()
    readPlatformOrder.mockRejectedValue(new BizError(1016, '上游故障'))
    await w2.find('[data-v2-field="source_order_no"]').setValue('SYNTH-LONG-ORDER-NO-0005')
    await w2.find('[data-v2-field="source_order_no"]').trigger('change')
    await flushPromises()
    expect(toastOf(w2)).toBe('')
  })

  /**
   * ⭐ 2026-11-10 客户线（W1）：买家身份随「读平台订单」暂存、随**创建**载荷回传。
   * 断言打在真实出口（`createOrder` 收到的载荷）上；本页没有编辑态，两键只在新建这一条路发。
   */
  it('客户线：读单成功响应带 buyer_eid ⇒ 创建载荷带上 buyer_eid/buyer_nick 两键', async () => {
    const { wrapper } = await mountPage()
    readPlatformOrder.mockResolvedValue({ ...PLATFORM_FIELDS, buyer_eid: 'SYNTH-EID-0003' })
    await wrapper.find('[data-v2-field="source_order_no"]').setValue('SYNTH-LONG-ORDER-NO-0021')
    await wrapper.find('[data-v2-field="source_order_no"]').trigger('change')
    await flushPromises()
    // 判别力：买家昵称真的回填进旺旺格（读单真的发生了，载荷带键不是别的原因）。
    expect((wrapper.find('[data-v2-field="customer_ww"]').element as HTMLInputElement).value).toBe('买家昵称甲')

    await fillMinimal(wrapper)
    await wrapper.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    const payload = createOrder.mock.calls[0]![0] as Record<string, unknown>
    expect(payload.buyer_eid).toBe('SYNTH-EID-0003')
    expect(payload.buyer_nick).toBe('买家昵称甲')
  })

  /**
   * ⭐ 客户线另一半：暂存是「**每次读单先清**」——换单号再读一次、这次成功但**没有**
   * `buyer_eid`（阿奇索那条腿的形状；失败那一档归 island 那份 spec 钉）
   * ⇒ 上一单的买家身份不得跟着进这一单的创建载荷。
   * ⚠️ test-utils 的 `setValue` 自带一次 `change`（v-model.lazy 兼容）+ 本 spec 既有写法
   * 显式再 `trigger('change')` 一次 ⇒ 同一对操作真实地读了**两次**（实测探针钉过）。
   */
  it('客户线：再读一次响应无 buyer_eid ⇒ 创建载荷不带这两键（暂存被清）', async () => {
    const { wrapper } = await mountPage()
    // 第一读带 eid（闲管家腿），此后每一读都无 eid（阿奇索腿的形状）。
    readPlatformOrder.mockResolvedValue(PLATFORM_FIELDS)
    readPlatformOrder.mockResolvedValueOnce({ ...PLATFORM_FIELDS, buyer_eid: 'SYNTH-EID-0004' })
    await wrapper.find('[data-v2-field="source_order_no"]').setValue('SYNTH-LONG-ORDER-NO-0022')
    await wrapper.find('[data-v2-field="source_order_no"]').trigger('change')
    await flushPromises()

    // 换单号再读：仍然无 eid ⇒ 上一单的买家身份不得跟着进这一单的创建载荷。
    await wrapper.find('[data-v2-field="source_order_no"]').setValue('SYNTH-LONG-ORDER-NO-0023')
    await wrapper.find('[data-v2-field="source_order_no"]').trigger('change')
    await flushPromises()
    // 「再读一次真的发生了」打在新单号那一读上（⛔ 不钉次数：一对操作读两次是 test-utils 的形状）。
    expect(readPlatformOrder).toHaveBeenCalledWith(null, 'SYNTH-LONG-ORDER-NO-0023')

    await fillMinimal(wrapper)
    await wrapper.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    const payload = createOrder.mock.calls[0]![0] as Record<string, unknown>
    expect(payload).not.toHaveProperty('buyer_eid')
    expect(payload).not.toHaveProperty('buyer_nick')
  })
})

/* ── 网 4：提交 mock 真交互（载荷逐键钉值 + 成功原地复位）──────────────────── */

describe('V2OrderNew 保真网④：提交载荷与成功行为（island onSave create 分支同构）', () => {
  it('最小成功路径：载荷 = 共享 formToPayload 契约 + 金额四格 0 + order_source 首条 + sales_agent 快照；day/hours 不泄漏', async () => {
    const { wrapper } = await mountPage()
    await fillMinimal(wrapper)
    await wrapper.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    expect(createOrder).toHaveBeenCalledTimes(1)
    const payload = createOrder.mock.calls[0]![0] as Record<string, unknown>
    expect(payload.actor).toBe('测试甲')
    expect(payload.game).toBe(2)
    expect(payload.project_text).toBe('示例项目')
    expect(payload.total_price).toBe('100')
    expect(payload.order_type).toBe('BOOST')
    // 金额四格与证据件 value="0" 同源（island 载荷同样带这四个 0）。
    expect(payload.received_price).toBe('0')
    expect(payload.platform_price).toBe('0')
    expect(payload.hall_price).toBe('0')
    expect(payload.fee).toBe('0')
    // 首屏默认第一条来源照发（island readOrderSource 同口径；auto 只影响读单）。
    expect(payload.order_source).toBe(20)
    // 选自己 ⇒ 不发 sales_user；名字快照照发（island 同一形状）。
    expect(payload.sales_user).toBeUndefined()
    expect(payload.sales_agent).toBe('测试甲')
    // 布尔开关没勾就不发键（缺键 = 不拆/不抛/不动偏好）。
    expect(payload.split).toBeUndefined()
    expect(payload.throw_to_hall).toBeUndefined()
    expect(payload.remember_assignee).toBeUndefined()
    expect(payload.assignee).toBeUndefined()
    // ⭐ 加急同步钉（2026-10-31 真缺陷回归：v2 搬运漏了 jiaji→form.is_urgent 同步 ⇒ 恒 false）。
    //    默认「否」⇒ 缺键（formToPayload 对 false 同样省略，与 island 一致）。
    expect(payload.is_urgent).toBeFalsy()
    // time_requirement：小时默认 0 ⇒ 只有天段。新建默认 30 天。
    expect(payload.time_requirement).toBe('30天')
    // 空值键不发（formToPayload 通用规则）。
    expect(payload.content).toBeUndefined()
    expect(payload.remark).toBeUndefined()
  })

  it('新老客三段控件：默认不选、选新/老才发 customer_kind_override，成功保存后复位为不选', async () => {
    const { wrapper } = await mountPage()
    const none = wrapper.get('[data-v2-customer-kind="none"]')
    expect(none.text()).toBe('不选')
    expect(none.attributes('aria-pressed')).toBe('true')
    expect(wrapper.get('[data-v2-customer-kind="new"]').text()).toBe('新')
    expect(wrapper.get('[data-v2-customer-kind="old"]').text()).toBe('老')

    // 不选 ⇒ 不发这个键（自动单走自动判定、无 ID 手工单不显示标签）。
    await fillMinimal(wrapper)
    await wrapper.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    let payload = createOrder.mock.calls[0]![0] as Record<string, unknown>
    expect(payload.customer_kind_override).toBeUndefined()
    // 成功后复位为「不选」。
    expect(wrapper.get('[data-v2-customer-kind="none"]').attributes('aria-pressed')).toBe('true')

    await fillMinimal(wrapper)
    await wrapper.get('[data-v2-customer-kind="old"]').trigger('click')
    await wrapper.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    payload = createOrder.mock.calls[1]![0] as Record<string, unknown>
    expect(payload.customer_kind_override).toBe('old')

    await fillMinimal(wrapper)
    await wrapper.get('[data-v2-customer-kind="new"]').trigger('click')
    await wrapper.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    payload = createOrder.mock.calls[2]![0] as Record<string, unknown>
    expect(payload.customer_kind_override).toBe('new')
  })

  it('加急勾「是」⇒ 载荷 is_urgent=true（真缺陷回归钉：同步行漏过 ⇒ 恒 false，2026-10-31）', async () => {
    const { wrapper } = await mountPage()
    await fillMinimal(wrapper)
    await wrapper.find('[data-v2-field="is_urgent"]').find('input[value="Y"]').setValue('Y')
    // radio change → watch(jiaji) 同步 form.is_urgent（v2 直落，island 是读表单时落）。
    await wrapper.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    const payload = createOrder.mock.calls[0]![0] as Record<string, unknown>
    expect(payload.is_urgent).toBe(true)
  })

  it('成功后 = toast「录单成功」+ 原地复位（金额回 0、文本清空、天/小时回默认），⛔ 不跳转（island §七）', async () => {
    const { wrapper } = await mountPage()
    await chooseSelect(wrapper, 'game', 2)
    await wrapper.find('[data-v2-field="project_text"]').setValue('示例项目')
    await wrapper.find('[data-v2-field="total_price"]').setValue('100')
    await wrapper.find('[data-v2-field="content"]').setValue('示例内容')
    await chooseSelect(wrapper, 'day', '2')
    await chooseSelect(wrapper, 'hours', '3')
    await wrapper.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    expect(flashOf(wrapper), '成功反馈 = 大浮层（2026-11-03 业务方「反馈太弱」升级）').toBe('录单成功')
    const total = wrapper.find('[data-v2-field="total_price"]').element as HTMLInputElement
    const project = wrapper.find('[data-v2-field="project_text"]').element as HTMLInputElement
    const content = wrapper.find('[data-v2-field="content"]').element as HTMLTextAreaElement
    expect(total.value).toBe('0')
    expect(project.value).toBe('')
    expect(content.value).toBe('')
    expect(selectByAnchor(wrapper, 'day').props('modelValue')).toBe('30')
    expect(selectByAnchor(wrapper, 'hours').props('modelValue')).toBe('0')
    // 载荷里的时间合成（这一单是 2天3小时）。
    expect((createOrder.mock.calls[0]![0] as Record<string, unknown>).time_requirement).toBe('2天3小时')
  })

  it('拆单勾选 ⇒ 载荷带 split:true；搜索后点接手人 ⇒ assignee，不带记住默认', async () => {
    const { wrapper } = await mountPage()
    await fillMinimal(wrapper)
    await wrapper.find('[data-v2-field="split"] input').setValue(true)
    await wrapper.find('[data-v2="assign-button"]').trigger('click')
    await wrapper.find('[data-v2-field="assign-search"]').setValue('乙')
    await wrapper.find('[data-v2="assign-search-btn"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-v2-assignee="5"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="assign-button"]').text()).toBe('接手人乙')
    expect(wrapper.find('[data-v2="assign-button"]').classes()).toContain('tt-assignee-picked')
    expect(wrapper.find('[data-v2-row="throw-fee"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2="dlg-assign"]').isVisible()).toBe(false)
    await wrapper.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    const payload = createOrder.mock.calls[0]![0] as Record<string, unknown>
    expect(payload.split).toBe(true)
    expect(payload).not.toHaveProperty('split_orders')
    expect(payload.assignee).toBe(5)
    expect(payload.remember_assignee).toBeUndefined()
  })

  it('新增子单在项目下方，按分预览剩余接单价并随录单一并提交', async () => {
    const { wrapper } = await mountPage()
    await fillMinimal(wrapper)
    await wrapper.get('[data-v2-field="total_price"]').setValue('0.30')
    const checkbox = wrapper.get('[data-v2-field="split"] input')
    const panel = '[data-v2="create-split-orders"]'
    const rows = '[data-v2="create-split-order"]'
    expect(wrapper.find(panel).exists()).toBe(false)
    await checkbox.setValue(true)
    expect(wrapper.get('.tt-panel-card').find(panel).exists()).toBe(true)
    expect(wrapper.findAll(rows)).toHaveLength(0)
    expect(wrapper.get('[data-v2="create-split-remaining"]').text()).toContain('0.30')
    await wrapper.get('[data-v2="create-split-add"]').trigger('click')
    await wrapper.get('[data-v2="create-split-add"]').trigger('click')
    const [first, second] = wrapper.findAll(rows)
    await first!.get('[data-v2="create-split-project"]').setValue('测试/保持完整项目')
    await first!.get('[data-v2="create-split-price"]').setValue('0.10')
    await second!.get('[data-v2="create-split-project"]').setValue('另一笔项目')
    await second!.get('[data-v2="create-split-price"]').setValue('0.20')
    expect(wrapper.get('[data-v2="create-split-remaining"]').text()).toContain('0.00')
    await wrapper.get('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    expect(createOrder).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      project_text: '示例项目',
      total_price: '0.30',
      split: true,
      split_orders: [
        { project_text: '测试/保持完整项目', total_price: '0.10' },
        { project_text: '另一笔项目', total_price: '0.20' },
      ],
    }))
    expect(wrapper.find(panel).exists()).toBe(false)
    await checkbox.setValue(true)
    expect(wrapper.findAll(rows)).toHaveLength(0)
    wrapper.unmount()
  })

  it('缺少已添加子单的内容时不提交，取消拆分后恢复普通录单', async () => {
    const { wrapper } = await mountPage()
    await fillMinimal(wrapper)
    const checkbox = wrapper.get('[data-v2-field="split"] input')
    await checkbox.setValue(true)
    await wrapper.get('[data-v2="create-split-add"]').trigger('click')
    await wrapper.get('[data-v2="save-order"]').trigger('click')
    expect(createOrder).not.toHaveBeenCalled()
    expect(toastOf(wrapper)).toContain('第 1 笔子单的项目内容不可为空')
    await checkbox.setValue(false)
    await wrapper.get('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    expect(createOrder).toHaveBeenCalledTimes(1)
    expect(createOrder.mock.calls[0]?.[0]).not.toHaveProperty('split')
    expect(createOrder.mock.calls[0]?.[0]).not.toHaveProperty('split_orders')
    wrapper.unmount()
  })

  it('点选中接手人旁的取消按钮后直接恢复暂不指派，并提交普通订单', async () => {
    fetchAssigneeOptions.mockResolvedValue({ ...ASSIGNEE_OPTIONS, defAssRecManUid: 5 })
    const { wrapper } = await mountPage()
    await fillMinimal(wrapper)
    expect(wrapper.get('[data-v2="assign-button"]').text()).toBe('接手人乙')
    const clear = wrapper.get('[data-v2="assign-clear"]')
    expect(clear.attributes('aria-label')).toBe('取消指派')

    await clear.trigger('click')
    expect(wrapper.get('[data-v2="assign-button"]').text()).toBe('暂不指派')
    expect(wrapper.find('[data-v2="assign-clear"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2="dlg-assign"]').exists()).toBe(false)
    expect(createOrder).not.toHaveBeenCalled()

    await wrapper.get('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    expect(createOrder).toHaveBeenCalledTimes(1)
    const payload = createOrder.mock.calls[0]![0] as Record<string, unknown>
    expect(payload).not.toHaveProperty('assignee')
    expect(payload).not.toHaveProperty('throw_to_hall')
    expect(payload.hall_price).toBe('0')
    expect(payload.remember_assignee).toBe('N')
  })

  it('服务端默认内部抢单：throw_to_hall + remember_assignee:"hall"，⛔ 不带 assignee；填了价才能过闸', async () => {
    fetchAssigneeOptions.mockResolvedValue({ ...ASSIGNEE_OPTIONS, defAssIsHall: true })
    const { wrapper } = await mountPage()
    await fillMinimal(wrapper)
    await wrapper.find('[data-v2-field="hall_price"]').setValue('12.5')
    await wrapper.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    const payload = createOrder.mock.calls[0]![0] as Record<string, unknown>
    expect(payload.throw_to_hall).toBe(true)
    expect(payload.remember_assignee).toBe('hall')
    expect(payload.assignee).toBeUndefined()
    expect(payload.hall_price).toBe('12.5')
  })

  it('选了别的销售客服 ⇒ 载荷带 sales_user 主键且 sales_agent 名字被摘掉（名字由服务端算）', async () => {
    const { wrapper } = await mountPage()
    await fillMinimal(wrapper)
    await wrapper.find('[data-v2="sales-agent-change"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-v2-sales-agent="11"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="sales-agent-current"]').text()).toBe('客服丙')
    expect(wrapper.findAllComponents(ElDialog)[0]!.props('modelValue')).toBe(false)
    await wrapper.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    const payload = createOrder.mock.calls[0]![0] as Record<string, unknown>
    expect(payload.sales_user).toBe(11)
    expect(payload.sales_agent).toBeUndefined()
  })

  it('失败路径：1022/1023（单已建好）⇒ 原样显示后端 msg + 复位表单；普通失败只提示不复位', async () => {
    const { BizError } = await import('@/api/errors')
    // 1022：指派失败但单已建 ⇒ 提示 + 复位（再点一次就是又建一张单）。
    createOrder.mockRejectedValue(new BizError(1022, '订单11111111111111111111已建，但指派失败：人已停用'))
    const { wrapper } = await mountPage()
    await fillMinimal(wrapper)
    await wrapper.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    expect(toastOf(wrapper)).toContain('已建')
    expect((wrapper.find('[data-v2-field="total_price"]').element as HTMLInputElement).value).toBe('0')

    // 1023：抛大厅失败，同处置。
    createOrder.mockRejectedValue(new BizError(1023, '订单11111111111111111112已建，但抛单失败'))
    const { wrapper: w2 } = await mountPage()
    await fillMinimal(w2)
    await w2.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    expect(toastOf(w2)).toContain('已建')

    // 普通业务失败：只提示，输入保留。
    createOrder.mockRejectedValue(new Error('你没有录入订单的权限'))
    const { wrapper: w3 } = await mountPage()
    await fillMinimal(w3)
    await w3.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    expect(toastOf(w3)).toBe('你没有录入订单的权限')
    expect((w3.find('[data-v2-field="total_price"]').element as HTMLInputElement).value).toBe('100')
  })

  it('5xx/网络异常 ⇒ 「结果未知」文案 + 保留输入；再点保存先弹二次确认、确认后才重发', async () => {
    const { NetworkError } = await import('@/api/errors')
    const { wrapper } = await mountPage()
    await fillMinimal(wrapper)
    createOrder.mockRejectedValueOnce(new NetworkError('网络中断'))
    await wrapper.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    expect(toastOf(wrapper)).toContain('提交结果未知（服务器错误）')
    // 输入保留（真的没建成功时复位会丢一整单）。
    expect((wrapper.find('[data-v2-field="total_price"]').element as HTMLInputElement).value).toBe('100')
    // 再点保存 ⇒ 不发请求，弹确认层（逐字文案）。
    await wrapper.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    expect(createOrder).toHaveBeenCalledTimes(1)
    const dlg = wrapper.find('[data-v2="dlg-resubmit"]')
    expect(dlg.exists()).toBe(true)
    // ⭐ 弹窗档 = sm 480（确认类，全局统一）。
    expect(dlg.classes()).toContain('cv2-dialog--sm')
    expect(dlg.classes()).toContain('cv2-dialog--sm') // width prop 已摘除（档位类接管）
    expect(dlg.text()).toContain('上一次提交的结果未知（服务器错误），可能已经建单')
    // 确认 ⇒ 清标志并重发。
    createOrder.mockResolvedValue({ order_no: '11111111111111111113' })
    await wrapper.find('[data-v2="resubmit-confirm"]').trigger('click')
    await flushPromises()
    expect(createOrder).toHaveBeenCalledTimes(2)
    expect(flashOf(wrapper)).toBe('录单成功')
  })
})

/* ── 网 5：销售客服网格（D-0208 / 修复轮 B2）────────────────────────────────── */

describe('V2OrderNew 保真网⑤：接单销售客服格', () => {
  it('持节点：默认只显示自己，点击更换后显示候选（不在候选里就插入自己）', async () => {
    const { wrapper } = await mountPage()
    expect(wrapper.find('[data-v2="sales-agent-current"]').text()).toBe('测试甲')
    expect(wrapper.find('[data-v2-sales-agent="11"]').exists()).toBe(false)
    await wrapper.find('[data-v2="sales-agent-change"]').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('[data-v2-sales-agent]').length).toBe(2)

    // 当前登录人不在候选里 ⇒ 插在最前并勾中（对标之外的自家口径，D-0208）。
    fetchKefuOptions.mockResolvedValue({ items: [{ id: 11, name: '客服丙' }], truncated: false })
    const { wrapper: w2 } = await mountPage()
    await w2.find('[data-v2="sales-agent-change"]').trigger('click')
    await flushPromises()
    const options = w2.findAll('[data-v2-sales-agent]')
    expect(options.length).toBe(2)
    expect(options[0]!.attributes('data-v2-sales-agent')).toBe('9')
  })

  it('不持节点：只读文本「测试甲（保存后归你自己）」、一颗 radio 都不画；保存不发 sales_user/sales_agent', async () => {
    const { wrapper } = await mountPage(['order.add_edit'])
    expect(wrapper.find('[data-v2="sales-agent-readonly"]').text()).toBe('测试甲（保存后归你自己）')
    expect(wrapper.findAll('[data-v2-field="sales_agent"] input[type="radio"]').length).toBe(0)
    await fillMinimal(wrapper)
    await wrapper.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    const payload = createOrder.mock.calls[0]![0] as Record<string, unknown>
    expect(payload.sales_user).toBeUndefined()
    expect(payload.sales_agent).toBeUndefined()
  })
})

/* ── 快速指派弹层与权限门 ───────────────────────────────────────────────────── */

describe('V2OrderNew：快速指派弹层（搜索/截断/默认档位复原）', () => {
  it('弹层只有搜索；搜完才出名单，点一行选中', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="assign-button"]').trigger('click')
    const dlg = wrapper.find('[data-v2="dlg-assign"]')
    expect(dlg.exists()).toBe(true)
    expect(dlg.classes()).toContain('cv2-dialog--lg')
    expect(dlg.text()).toContain('快速指派接手人')
    expect(dlg.text()).not.toContain('排序方式')
    expect(dlg.text()).not.toContain('暂不指派')
    expect(dlg.text()).not.toContain('接手人乙')

    await wrapper.find('[data-v2-field="assign-search"]').setValue('乙')
    await wrapper.find('[data-v2="assign-search-btn"]').trigger('click')
    await flushPromises()
    expect(fetchAssigneeOptions).toHaveBeenLastCalledWith({ limit: 200, key: '乙' })
    expect(wrapper.find('[data-v2="dlg-assign"]').text()).toContain('接手人乙')
    await wrapper.find('[data-v2="assign-reset-btn"]').trigger('click')
    await flushPromises()
    expect(fetchAssigneeOptions).toHaveBeenLastCalledWith({ limit: 200 })
    expect(wrapper.find('[data-v2-assignee="5"]').exists()).toBe(false)
  })

  it('候选截断必须说出来；服务端默认档位（hall）首屏把按钮设成内部抢单', async () => {
    fetchAssigneeOptions.mockResolvedValue({
      ...ASSIGNEE_OPTIONS,
      truncated: true,
      defAssIsHall: true,
    })
    const { wrapper } = await mountPage()
    expect(toastOf(wrapper)).toContain('可指派接手人超过 200 位，已截断')
    expect(wrapper.find('[data-v2="assign-button"]').text()).toBe('内部抢单')
  })

  it('服务端默认接手人 ⇒ 首屏带出那个人；人不在名单里不假装选中', async () => {
    fetchAssigneeOptions.mockResolvedValue({
      ...ASSIGNEE_OPTIONS,
      defAssRecManUid: 5,
      defAssRecMan: '接手人乙',
    })
    const { wrapper } = await mountPage()
    expect(wrapper.find('[data-v2="assign-button"]').text()).toBe('接手人乙')

    fetchAssigneeOptions.mockResolvedValue({
      ...ASSIGNEE_OPTIONS,
      items: [{ id: 6, name: '接手人丙', role: null, tag: '', remark: '' }],
      defAssRecManUid: 5,
      defAssRecMan: '接手人乙',
    })
    const { wrapper: w2 } = await mountPage()
    expect(w2.find('[data-v2="assign-button"]').text()).toBe('暂不指派')
  })
})

describe('V2OrderNew：权限门 fail-closed（按节点渲染，不摆点了会 403 的入口）', () => {
  it('不持 order.add_edit ⇒ 提交保存按钮不渲染；粘贴区的「会自动识别」承诺摘掉且失焦不发解析请求', async () => {
    const { wrapper } = await mountPage([])
    expect(wrapper.find('[data-v2="save-order"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('[会自动识别订单各项内容]')
    const textarea = wrapper.find('[data-v2-field="content"]').element as HTMLTextAreaElement
    expect(textarea.getAttribute('placeholder')).toBeNull()
    parsePastedText.mockResolvedValue({ fields: {}, matched: [], unmatched: [] })
    await wrapper.find('[data-v2-field="content"]').setValue('示例内容')
    await wrapper.find('[data-v2-field="content"]').trigger('blur')
    await flushPromises()
    expect(parsePastedText).not.toHaveBeenCalled()
  })

  it('持 order.add_edit 但不持 allow_change_sales_agent ⇒ 保存按钮在、销售客服格只读', async () => {
    const { wrapper } = await mountPage(['order.add_edit'])
    expect(wrapper.find('[data-v2="save-order"]').exists()).toBe(true)
    expect(wrapper.find('[data-v2="sales-agent-readonly"]').exists()).toBe(true)
  })
})

describe('V2OrderNew：订单类型切换 4 个 label（对标 showpw() 的那组，逐字）', () => {
  it('陪玩单 ⇒ 陪玩内容/标题 / 陪玩时长 / 老板名称 / 发陪玩价格；代练单 ⇒ 原组', async () => {
    const { wrapper } = await mountPage()
    expect(wrapper.text()).toContain('代练项目/标题')
    expect(wrapper.text()).toContain('时间要求')
    expect(wrapper.text()).toContain('角色名')
    await wrapper.find('[data-v2-field="order-type"] input[value="COMPANION"]').setValue(true)
    await flushPromises()
    expect(wrapper.text()).toContain('陪玩内容/标题')
    expect(wrapper.text()).toContain('陪玩时长')
    expect(wrapper.text()).toContain('老板名称')
    expect(wrapper.text()).not.toContain('代练项目/标题')
    fetchAssigneeOptions.mockResolvedValue({ ...ASSIGNEE_OPTIONS, defAssIsHall: true })
    const { wrapper: hall } = await mountPage()
    expect(hall.text()).toContain('给打手发单价')
    await hall.find('[data-v2-field="order-type"] input[value="COMPANION"]').setValue(true)
    await flushPromises()
    expect(hall.text()).toContain('发陪玩价格')
    await wrapper.find('[data-v2-field="order-type"] input[value="BOOST"]').setValue(true)
    await flushPromises()
    expect(wrapper.text()).toContain('代练项目/标题')
  })

  it('陪玩单提交 ⇒ 载荷 order_type=COMPANION', async () => {
    const { wrapper } = await mountPage()
    await chooseSelect(wrapper, 'game', 2)
    await wrapper.find('[data-v2-field="project_text"]').setValue('示例项目')
    await wrapper.find('[data-v2-field="order-type"] input[value="COMPANION"]').setValue(true)
    await flushPromises()
    await wrapper.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    expect((createOrder.mock.calls[0]![0] as Record<string, unknown>).order_type).toBe('COMPANION')
  })
})

describe('V2OrderNew：级联与常用项目（与 island onGameChange/onCategoryChange 同语义）', () => {
  it('切游戏 ⇒ 区服/分类按新游戏重拉且旧选择清空；切分类 ⇒ 常用项目重拉', async () => {
    const { wrapper } = await mountPage()
    await chooseSelect(wrapper, 'game', 2)
    await flushPromises()
    expect(fetchServers).toHaveBeenLastCalledWith(2)
    expect(fetchCategories).toHaveBeenLastCalledWith(2)
    await wrapper.find('[data-v2-field="category"] input[value="4"]').setValue(true)
    await flushPromises()
    expect(fetchProjects).toHaveBeenLastCalledWith(4)
    // 项目勾选列表出来两颗。
    expect(wrapper.findAll('[data-v2-project]').length).toBe(2)
  })

  it('常用项目勾选：文本按共享规则拼/删（toggleProjectName）；恰好一项 ⇒ 载荷带 project FK', async () => {
    const { wrapper } = await mountPage()
    await chooseSelect(wrapper, 'game', 2)
    await wrapper.find('[data-v2-field="category"] input[value="4"]').setValue(true)
    await flushPromises()
    await wrapper.find('[data-v2-project="9"] input').setValue(true)
    await flushPromises()
    expect((wrapper.find('[data-v2-field="project_text"]').element as HTMLInputElement).value).toBe('项目甲')
    // 两项勾着 ⇒ project FK 为 null（文本是权威值）。
    await wrapper.find('[data-v2-project="10"] input').setValue(true)
    await flushPromises()
    expect((wrapper.find('[data-v2-field="project_text"]').element as HTMLInputElement).value).toBe('项目甲/项目乙')
    await wrapper.find('[data-v2-field="total_price"]').setValue('100')
    await wrapper.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    expect((createOrder.mock.calls[0]![0] as Record<string, unknown>).project).toBeUndefined()

    // 恰好一项 ⇒ FK 记它（独立挂载：上一段成功后已原地复位、勾选清空）。
    const { wrapper: w2 } = await mountPage()
    await chooseSelect(w2, 'game', 2)
    await w2.find('[data-v2-field="category"] input[value="4"]').setValue(true)
    await flushPromises()
    await w2.find('[data-v2-project="9"] input').setValue(true)
    await w2.find('[data-v2-field="total_price"]').setValue('100')
    await w2.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    expect((createOrder.mock.calls[1]![0] as Record<string, unknown>).project).toBe(9)
  })

  it('级联清空（切游戏）⇒ 勾选清空但权威文本不动（island 同口径：文本是权威值）', async () => {
    const { wrapper } = await mountPage()
    await chooseSelect(wrapper, 'game', 2)
    await wrapper.find('[data-v2-field="category"] input[value="4"]').setValue(true)
    await flushPromises()
    await wrapper.find('[data-v2-project="9"] input').setValue(true)
    await flushPromises()
    expect((wrapper.find('[data-v2-field="project_text"]').element as HTMLInputElement).value).toBe('项目甲')
    await chooseSelect(wrapper, 'game', 3)
    await flushPromises()
    expect(wrapper.findAll('[data-v2-project]').length).toBe(2)
    expect((wrapper.find('[data-v2-project="9"] input').element as HTMLInputElement).checked).toBe(false)
    expect((wrapper.find('[data-v2-field="project_text"]').element as HTMLInputElement).value).toBe('项目甲')
  })

  it('「尚未开放」控件：点了弹对应文案（对标有、后端没有的照渲染不置灰）', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-notopen="服务器（服）"]').trigger('click')
    expect(toastOf(wrapper)).toBe('「服务器（服）」尚未开放')
    await wrapper.find('[data-v2-tab="import"]').trigger('click')
    expect(toastOf(wrapper)).toBe('「导入文件订单」尚未开放')
  })

  it('尚未开放的文本框：失焦时有内容 ⇒ 提示；空 ⇒ 不提示（静默吞掉已填内容比置灰更糟的那条底线）', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-field="game_pwd"]').setValue('somepwd')
    await wrapper.find('[data-v2-field="game_pwd"]').trigger('blur')
    expect(toastOf(wrapper)).toBe('「登录密码」尚未开放')
    // 空值失焦 ⇒ 无新提示（独立挂载，避开上一条 toast 的 6s 窗口）。
    const { wrapper: w2 } = await mountPage()
    await w2.find('[data-v2-field="game_pwd"]').setValue('')
    await w2.find('[data-v2-field="game_pwd"]').trigger('blur')
    expect(toastOf(w2)).toBe('')
  })
})

/* ── 网 6：信息架构重排——主流程/折叠两层（2026-11-01，字段零丢失）────────────── */

/** 主流程区（`data-v2="main-flow"`）应含的**字段**锚点 = 业务方 90% 动线四步 + 模式开关+ ⭐ 2026-11-01 第二轮业务方指令挪出折叠区的六块（订单来源/拆分/加急/重要备注/内部派单/销售客服；内部派单一块带 hall_price + default_assignee 两个锚点）。⭐ 业务方追加修正（同日第一轮）：金额行主流程**只留订单总价**——发平台价格 /发内部抢单大厅价移入折叠区（进载荷的真字段，隐藏 ≠ 省略）。
 * 主流程区（`data-v2="main-flow"`）应含的**字段**锚点 = 业务方 90% 动线四步 + 模式开关+ ⭐ 2026-11-01 第二轮业务方指令挪出折叠区的六块（订单来源/拆分/加急/重要备注/内部派单/销售客服；内部派单一块带 hall_price + default_assignee 两个锚点）。⭐ 业务方追加修正（同日第一轮）：金额行主流程**只留订单总价**——发平台价格 /发内部抢单大厅价移入折叠区（进载荷的真字段，隐藏 ≠ 省略）。 */
const MAIN_FLOW_FIELDS = [
  'order-type', // ① 订单类型（模式开关）
  'source_order_no', // ② 来源单号（第一个主字段）
  'order_source', // ②b 订单来源（第二轮挪出折叠区，与来源单号同族）
  'game', // ③ 游戏级联（平台/服/职业/阵营壳同住主流程行内）
  'server',
  'category',
  'projects', // ④ 常用项目勾选横排
  'project_text', // ④ 代练项目/标题
  'split', // ④b 是否拆分订单（第二轮挪出折叠区）
  'total_price', // ⑤ 订单总价（主流程金额唯一一格）
  'day', // ⑩ 时间要求·天（⭐ 2026-11-03 业务方「时间要求拿出来 和加急放一排」）
  'hours', // ⑩ 时间要求·小时（同上）
  'is_urgent', // ⑤b 是否加急（同排右位）
  'customer_phone', // ⑭ 联系三连（⭐ 2026-11-03 业务方「放到重要备注上一排」）
  'customer_qq', // ⑭ QQ/微信（同上）
  'customer_ww', // ⑭ 旺旺/YY（同上）
  'remark', // ⑤c 重要备注（第二轮挪出折叠区，独占整行）
  'sales_agent', // ⑤e 接单销售客服网格（第二轮挪出折叠区）
]

/** 折叠区字段 = 全集 − 主流程 − 左列「订单内容」（左列重排轮保留不动，算第三层）。 */
const MORE_FIELDS = [...EXPECTED_FIELD_ANCHORS].filter(
  (a) => !MAIN_FLOW_FIELDS.includes(a) && a !== 'content',
)

/** 从一段 HTML 里收集全部 data-v2-field 锚点名。 */
function fieldAnchorsOf(html: string): Set<string> {
  return new Set(Array.from(html.matchAll(/data-v2-field="([^"]+)"/g)).map((m) => m[1] as string))
}

describe('V2OrderNew 信息架构重排：主流程/折叠两层（字段零丢失 + 折叠记忆）', () => {
  it('字段零丢失网：左列识别区 ∪ 主流程 ∪ 折叠区 = 重排前全集、无交集、且各归其位', async () => {
    const { wrapper } = await mountPage()
    // 左列（订单内容粘贴识别区）重排轮保留不动，是第三层。
    const inPaste = fieldAnchorsOf(wrapper.find('.tt-paste-panel').html())
    const inMain = fieldAnchorsOf(wrapper.find('[data-v2="main-flow"]').html())
    const inMore = fieldAnchorsOf(wrapper.find('[data-v2="more-body"]').html())
    // 三层并集 = 重排前全集（缺一个红一个——折叠 ≠ 省略）。
    const union = new Set([...inPaste, ...inMain, ...inMore])
    expect([...union].sort()).toEqual([...EXPECTED_FIELD_ANCHORS].sort())
    // 左列只有「订单内容」一格，且它不住进右列两层。
    expect([...inPaste].sort()).toEqual(['content'])
    // 一个字段只能住一层。
    const overlap = [...inMain].filter((a) => inMore.has(a))
    expect(overlap, '同时出现在主流程与折叠区的字段').toEqual([])
    // 各归其位：主流程恰好是动线字段集合，折叠区恰好是其余全部。
    expect([...inMain].sort()).toEqual([...MAIN_FLOW_FIELDS].sort())
    expect([...inMore].sort()).toEqual([...MORE_FIELDS].sort())
  })

  it('动线钉位：订单类型 → 游戏级联 → 拆分 → 项目面板 → 来源行（订单来源左·来源单号右）→ 总价+时间+加急一排 → 联系三连 → 备注 → 内部派单 → 销售客服 → 提交保存（主流程从上到下，⭐ 第三轮：拆分上移到级联下面）；级联壳同区', async () => {
    const { wrapper } = await mountPage()
    const mainHtml = wrapper.find('[data-v2="main-flow"]').html()
    const idx = (needle: string) => {
      const at = mainHtml.indexOf(needle)
      expect(at, `主流程区应含 ${needle}`).toBeGreaterThan(-1)
      return at
    }
    // ⭐ 来源行合并（订单来源左·来源单号右）且整排挪到项目面板之后
    //（业务方「订单来源那一排 放到代练项目标题下面那一排」）。
    expect(idx('data-v2-field="game"')).toBeLessThan(idx('data-v2-field="split"'))
    expect(idx('data-v2-field="split"')).toBeLessThan(idx('data-v2-field="project_text"'))
    expect(idx('data-v2-field="project_text"')).toBeLessThan(idx('data-v2-field="order_source"'))
    expect(idx('data-v2-field="order_source"')).toBeLessThan(idx('data-v2-field="source_order_no"'))
    expect(idx('data-v2-field="source_order_no"')).toBeLessThan(idx('data-v2-field="total_price"'))
    expect(idx('data-v2-field="total_price"')).toBeLessThan(idx('data-v2-field="day"'))
    expect(idx('data-v2-field="day"')).toBeLessThan(idx('data-v2-field="hours"'))
    expect(idx('data-v2-field="hours"')).toBeLessThan(idx('data-v2-field="is_urgent"'))
    expect(idx('data-v2-field="is_urgent"')).toBeLessThan(idx('data-v2-field="customer_phone"'))
    expect(idx('data-v2-field="customer_phone"')).toBeLessThan(idx('data-v2-field="customer_qq"'))
    expect(idx('data-v2-field="customer_qq"')).toBeLessThan(idx('data-v2-field="customer_ww"'))
    expect(idx('data-v2-field="customer_ww"')).toBeLessThan(idx('data-v2-field="remark"'))
    expect(idx('data-v2="assign-button"')).toBeLessThan(idx('data-v2-field="remark"'))
    expect(idx('data-v2-field="remark"')).toBeLessThan(idx('data-v2-field="sales_agent"'))
    const dispatchRow = wrapper.find('.tt-dispatch-row')
    expect(dispatchRow.find('[data-v2="assign-button"]').exists()).toBe(true)
    expect(dispatchRow.find('[data-v2-field="remark"]').exists()).toBe(true)
    expect(idx('data-v2-field="sales_agent"')).toBeLessThan(idx('data-v2="save-order"'))
    // 级联的「尚未开放」壳（平台/服/职业/阵营）仍渲染在主流程级联行里（对标有、后端没有 ⇒ 照渲染）。
    expect(mainHtml).toContain('data-v2-notopen="服务器（服）"')
    expect(mainHtml).toContain('data-v2-notopen="职业/门派"')
  })

  it('金额行主流程只留订单总价；发平台价格/发内部抢单大厅价住折叠区且值照常在 DOM（业务方追加修正）', async () => {
    const { wrapper } = await mountPage()
    const mainHtml = wrapper.find('[data-v2="main-flow"]').html()
    const moreHtml = wrapper.find('[data-v2="more-body"]').html()
    expect(mainHtml).toContain('data-v2-field="total_price"')
    expect(mainHtml).not.toContain('data-v2-field="platform_price"')
    expect(mainHtml).not.toContain('data-v2-field="hall_price_inter"')
    expect(moreHtml).toContain('data-v2-field="platform_price"')
    expect(moreHtml).toContain('data-v2-field="hall_price_inter"')
    // hidden ≠ omitted：两格仍渲染、初值照证据件 value="0"（载荷契约零变化）。
    expect((wrapper.find('[data-v2-field="platform_price"]').element as HTMLInputElement).value).toBe('0')
    expect((wrapper.find('[data-v2-field="hall_price_inter"]').element as HTMLInputElement).value).toBe('0')
  })

  it('折叠头措辞钉值 + 默认收起；点击展开写 localStorage=1、再点收起写回 0；重挂载恢复展开态', async () => {
    const { wrapper } = await mountPage()
    const toggle = wrapper.find('[data-v2="more-toggle"]')
    expect(toggle.text()).toContain('更多设置（偶尔用）')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('[data-v2="more-settings"]').classes()).not.toContain('tt-more--open')
    await toggle.trigger('click')
    expect(toggle.attributes('aria-expanded')).toBe('true')
    expect(wrapper.find('[data-v2="more-settings"]').classes()).toContain('tt-more--open')
    expect(localStorage.getItem('tt.orders.new.moreOpen')).toBe('1')
    await toggle.trigger('click')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(localStorage.getItem('tt.orders.new.moreOpen')).toBe('0')
    // 重挂载：展开态从 localStorage 恢复（默认收起只对没记忆过的浏览器生效）。
    localStorage.setItem('tt.orders.new.moreOpen', '1')
    const { wrapper: w2 } = await mountPage()
    expect(w2.find('[data-v2="more-toggle"]').attributes('aria-expanded')).toBe('true')
    expect(w2.find('[data-v2="more-settings"]').classes()).toContain('tt-more--open')
  })
})

/* ── 网 7：级联自动预选（2026-11-01 新交互）──────────────────────────────────── */

describe('V2OrderNew 级联自动预选：游戏选定 ⇒ 区服预选第一项；识别回填值优先；手改不被覆盖', () => {
  it('选定游戏 ⇒ 区服自动预选字典第一项；切游戏后重新预选；预选值与手选同路进载荷', async () => {
    const { wrapper } = await mountPage()
    expect(selectByAnchor(wrapper, 'game').props('modelValue')).toBe(2)
    expect(selectByAnchor(wrapper, 'server').props('modelValue')).toBe(7)
    expect((wrapper.find('[data-v2-field="category"] input[value="4"]').element as HTMLInputElement).checked).toBe(true)
    expect(fetchProjects).toHaveBeenCalledWith(4)
    expect(wrapper.findAll('[data-v2-project]')).toHaveLength(2)
    await chooseSelect(wrapper, 'game', 2)
    await flushPromises()
    expect(selectByAnchor(wrapper, 'server').props('modelValue')).toBe(7)
    await chooseSelect(wrapper, 'game', 3)
    await flushPromises()
    expect(selectByAnchor(wrapper, 'server').props('modelValue')).toBe(7)
    // 预选不是摆设：与手选同一个模型位，载荷照发（formToPayload 契约零变化）。
    await wrapper.find('[data-v2-field="project_text"]').setValue('示例项目')
    await wrapper.find('[data-v2-field="total_price"]').setValue('100')
    await wrapper.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    expect((createOrder.mock.calls[0]![0] as Record<string, unknown>).server).toBe(7)
  })

  it('识别回填值 > 预选：粘贴带区服名且在字典里 ⇒ 落回填值；不在 ⇒ 预选第一项保留 + 「没能对应上」照报', async () => {
    fetchServers.mockImplementation(async (gameId: number) =>
      gameId === 3
        ? [
            { id: 7, name: '区服甲', game: 3, sort_order: 1, is_active: true, created_at: '2026-10-01T00:00:00+08:00' },
            { id: 8, name: '区服乙', game: 3, sort_order: 2, is_active: true, created_at: '2026-10-01T00:00:00+08:00' },
          ]
        : SERVERS,
    )
    const { wrapper } = await mountPage()
    parsePastedText.mockResolvedValue({
      fields: { game_name: '示例游戏二', server_name: '区服乙' },
      matched: [],
      unmatched: [],
    })
    await wrapper.find('[data-v2-field="content"]').setValue('示例粘贴内容')
    await wrapper.find('[data-v2-field="content"]').trigger('blur')
    await flushPromises()
    // 切到示例游戏二后字典第一项是区服甲(7)；回填的区服乙(8)必须赢过预选。
    expect(selectByAnchor(wrapper, 'server').props('modelValue')).toBe(8)

    // 名字不在字典里 ⇒ 不静默：预选第一项保留，提示照发（E-0004 那条底线的同族）。
    const { wrapper: w2 } = await mountPage()
    parsePastedText.mockResolvedValue({
      fields: { game_name: '示例游戏二', server_name: '区服不存在' },
      matched: [],
      unmatched: [],
    })
    await w2.find('[data-v2-field="content"]').setValue('示例粘贴内容')
    await w2.find('[data-v2-field="content"]').trigger('blur')
    await flushPromises()
    expect(selectByAnchor(w2, 'server').props('modelValue')).toBe(7)
    expect(toastOf(w2)).toContain('没能对应上：区服「区服不存在」')
  })

  it('识别等待常用项目期间手动选择区服，迟到的识别结果不能覆盖保存值', async () => {
    fetchServers.mockResolvedValue([
      { id: 7, name: '区服甲', game: 3, sort_order: 1, is_active: true, created_at: '2026-10-01T00:00:00+08:00' },
      { id: 8, name: '区服乙', game: 3, sort_order: 2, is_active: true, created_at: '2026-10-01T00:00:00+08:00' },
    ])
    const { wrapper } = await mountPage()
    let finishProjects!: (rows: typeof PROJECTS) => void
    fetchProjects.mockImplementationOnce(() => new Promise((resolve) => { finishProjects = resolve }))
    parsePastedText.mockResolvedValue({
      fields: { game_name: '示例游戏二', server_name: '区服甲' }, matched: [], unmatched: [],
    })
    await wrapper.find('[data-v2-field="content"]').setValue('示例粘贴内容')
    await wrapper.find('[data-v2-field="content"]').trigger('blur')
    await flushPromises()
    expect(selectByAnchor(wrapper, 'game').props('modelValue')).toBe(3)
    expect(selectByAnchor(wrapper, 'server').props('modelValue')).toBe(7)
    await chooseSelect(wrapper, 'server', 8)
    finishProjects(PROJECTS)
    await flushPromises()
    expect(selectByAnchor(wrapper, 'server').props('modelValue')).toBe(8)
    await wrapper.find('[data-v2-field="project_text"]').setValue('示例项目')
    await wrapper.find('[data-v2="save-order"]').trigger('click')
    await flushPromises()
    expect((createOrder.mock.calls[0]![0] as Record<string, unknown>).server).toBe(8)
  })

  it('手改区服不被预选覆盖（预选只挂在游戏切换这一件事上）', async () => {
    fetchServers.mockResolvedValue([
      { id: 7, name: '区服甲', game: 2, sort_order: 1, is_active: true, created_at: '2026-10-01T00:00:00+08:00' },
      { id: 8, name: '区服乙', game: 2, sort_order: 2, is_active: true, created_at: '2026-10-01T00:00:00+08:00' },
    ])
    const { wrapper } = await mountPage()
    await chooseSelect(wrapper, 'game', 2)
    await flushPromises()
    // 预选字典第一项（区服甲）；客服手改区服乙 ⇒ 不被预选覆盖。
    expect(selectByAnchor(wrapper, 'server').props('modelValue')).toBe(7)
    await chooseSelect(wrapper, 'server', 8)
    await flushPromises()
    expect(selectByAnchor(wrapper, 'server').props('modelValue')).toBe(8)
  })
})

/* ── 网 8：⭐ 2026-11-01 第三轮 连体字段网格的版式钉（CSS 形状钉在组件源文本上——
 *    scoped 样式 jsdom 不算 cascade，与本仓 carbon.css 形状钉同一手法）────────────── */

describe('V2OrderNew：⭐ 2026-11-01 第三轮 连体字段网格（标签格连体 + 自适应；可拖宽已于 2026-11-03 按业务方指令撤除）', () => {
  const VUE_SOURCE = readFileSync(
    path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../V2OrderNew.vue'),
    'utf8',
  )

  it('⭐ 2026-11-03 业务方「右下角斜杆删掉不需要」：可拖宽撤除——.tt-fitem = resize none + overflow hidden（连体格其余语法不动）', () => {
    expect(VUE_SOURCE).toMatch(/\.tt-fitem\s*\{[^}]*resize:\s*none/)
    expect(VUE_SOURCE).not.toMatch(/\.tt-fitem\s*\{[^}]*resize:\s*horizontal/)
    expect(VUE_SOURCE).toMatch(/\.tt-fitem\s*\{[^}]*overflow:\s*hidden/)
    expect(VUE_SOURCE).toMatch(/\.tt-fitem\s*\{[^}]*border-radius:\s*4px/)
  })

  it('标签 = 带边框暖米底的格子、与输入框零间距连体（右边框去掉）；.tt-fvalue 值格同族连体', () => {
    expect(VUE_SOURCE).toMatch(
      /\.tt-flabel\s*\{[^}]*border:\s*1px solid var\(--tt2-filter-line\)[^}]*border-right:\s*none/,
    )
    expect(VUE_SOURCE).toMatch(/\.tt-flabel\s*\{[^}]*background:\s*var\(--tt2-filter-label-bg\)/)
    expect(VUE_SOURCE).toMatch(
      /\.tt-fvalue\s*\{[^}]*border:\s*1px solid var\(--tt2-filter-line\)[^}]*background:\s*var\(--tt2-panel\)/,
    )
    // 连体 = 零间距：.tt-fitem 的 gap 必须是 0（⛔ 回退成散盒子版式会当场红）。
    expect(VUE_SOURCE).toMatch(/\.tt-fitem\s*\{[^}]*gap:\s*0;/)
    // 内缝直角：EP 控件圆角在连体格里压成 0（外角由包裹层 clip 给出）。
    expect(VUE_SOURCE).toMatch(
      /\.tt-fitem :deep\(\.el-input__wrapper\),\s*\n\s*\.tt-fitem :deep\(\.el-select__wrapper\)\s*\{[^}]*border-radius:\s*0/,
    )
  })

  it('上一轮的「纯文字标签散排 / 固定标签列」CSS 清干净：.tt-flabel 恰好两个定义块（基础块 + ≤720 竖排覆盖块）、无 110/120px 标签列', () => {
    // ⭐ 响应式窄档：≤720 档把连体格竖排（标签置上）⇒ 第二个定义块出现在
    // 该媒体查询里；两个块都必须真实存在（钉「基础块没被顺手删 / 覆盖块没落错档」）。
    expect((VUE_SOURCE.match(/\.tt-flabel\s*\{/g) ?? []).length).toBe(2)
    const mobile = mediaBlock(VUE_SOURCE, 'max-width: 720px')
    expect(mobile).toMatch(/\.tt-flabel\s*\{[^}]*width:\s*100%/)
    expect(VUE_SOURCE).not.toMatch(/width:\s*1[12]0px[^}]*\/\*.*标签/)
    for (const legacy of ['110px 固定', '120px（长标签自动加宽不截断）']) {
      expect(VUE_SOURCE).not.toContain(legacy)
    }
  })

  it('右列整体限宽 880；长内容字段独占整行（重要备注 / 来源单号 = grow 项）仍在', () => {
    expect(VUE_SOURCE).toMatch(/\.tt-ord-form\s*\{[^}]*max-width:\s*880px/)
    expect(VUE_SOURCE).toMatch(/\.tt-grow-item\s*\{[^}]*flex:\s*1 1 320px/)
    expect(VUE_SOURCE).toMatch(/\.tt-fitem > \.tt-grow\s*\{[^}]*flex:\s*1 1 260px/)
  })

  it('模板落点：radio/checkbox/按钮类控件套 .tt-fvalue 值格（订单类型/加急/拆分/内部派单/销售客服/项目分类），字段锚点零丢失', () => {
    // 值格覆盖面（与模板一一对应；漏一个 = 那个字段没有连体格）。
    for (const anchor of ['order-type', 'is_urgent', 'split', 'category', 'vip_ded_check']) {
      const at = VUE_SOURCE.indexOf(`data-v2-field="${anchor}"`)
      expect(at, `字段 ${anchor} 应存在`).toBeGreaterThan(-1)
      // 锚点往前找最近的 .tt-fvalue 开标签（同一个 fitem 内）。
      const before = VUE_SOURCE.slice(Math.max(0, at - 400), at)
      expect(before, `字段 ${anchor} 应包在 .tt-fvalue 值格里`).toContain('class="tt-fvalue"')
    }
  })
})

/* ── 网 9：⭐ 2026-11-01 响应式专包（业务方一直开着 ~620px 宽面板看本页）────────────────
 * 断点口径 = carbon.css 文件头注释（≤900 两列堆叠 / ≤720 标签置上）；CSS 形状钉读组件
 * 源文本（scoped 样式 jsdom 不算 cascade，与网 8 同一手法）。 */

/** 取 `@media (query)` 的整块（括号配对；取不到 = 响亮失败）。函数声明提升到文件级，
 *  上面的连体网格网（.tt-flabel 恰好两块）也用它取 ≤720 块。 */
function mediaBlock(source: string, query: string): string {
  const marker = source.indexOf(`@media (${query})`)
  if (marker < 0) throw new Error(`V2OrderNew.vue 里找不到 @media (${query})——响应式块丢了`)
  const open = source.indexOf('{', marker)
  let depth = 0
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === '{') depth += 1
    if (source[i] === '}') {
      depth -= 1
      if (depth === 0) return source.slice(open + 1, i)
    }
  }
  throw new Error('@media 块括号不配对')
}

describe('V2OrderNew：响应式（响应式专包 2026-11-01）', () => {
  // 与网 8 的 VUE_SOURCE 同一份文件（那个 const 住在自己的 describe 里，这里取本 describe 的副本）。
  const VUE_SOURCE = readFileSync(
    path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../V2OrderNew.vue'),
    'utf8',
  )

  it('断点恰为 900/720 两档（口径统一，别散落魔数）', () => {
    expect(VUE_SOURCE.match(/@media \(max-width: \d+px\)/g)).toEqual([
      '@media (max-width: 900px)',
      '@media (max-width: 720px)',
    ])
  })

  it('≤900：左粘贴区 + 右表单两列栅格改上下堆叠（粘贴区在上、表单在下、各自全宽、解除 880 限宽）', () => {
    const block = mediaBlock(VUE_SOURCE, 'max-width: 900px')
    expect(block).toMatch(/\.tt-ord-grid\s*\{[^}]*grid-template-columns:\s*1fr/)
    expect(block).toMatch(/\.tt-ord-form\s*\{[^}]*max-width:\s*none/)
    expect(block).toMatch(/\.tt-paste-panel :deep\(\.el-textarea__inner\)\s*\{[^}]*min-height:\s*180px/)
  })

  it('≤720：字段改「标签在上、控件在下、控件全宽」（定宽档 tt-w-* 被压掉、拖宽收回）；提交按钮铺满', () => {
    const block = mediaBlock(VUE_SOURCE, 'max-width: 720px')
    expect(block).toMatch(/\.tt-fitem\s*\{[^}]*flex-direction:\s*column/)
    expect(block).toMatch(/\.tt-fitem\s*\{[^}]*resize:\s*none/)
    expect(block).toMatch(/\.tt-flabel\s*\{[^}]*width:\s*100%[^}]*border-right:\s*1px solid var\(--tt2-filter-line\)/)
    // 控件全宽：五类直接子控件（同一组选择器）一律 width 100% + 纵向 -1px 合缝（横向连体缝压掉）。
    expect(block).toMatch(
      /\.tt-fitem > :deep\(\.el-input\),\s*\n\s*\.tt-fitem > :deep\(\.el-select\),\s*\n\s*\.tt-fitem > \.tt-notopen-shell,\s*\n\s*\.tt-fitem > \.tt-fvalue,\s*\n\s*\.tt-fitem > \.cv2-input\s*\{[^}]*width:\s*100%[^}]*margin-left:\s*0[^}]*margin-top:\s*-1px/,
    )
    expect(block).toMatch(/\.tt-save-btn\s*\{[^}]*width:\s*100%/)
  })

  it('堆叠 ≠ 隐藏：两个媒体查询块里不许出现 display:none / visibility:hidden（字段锚点一个不藏，恰好集合网照旧钉 DOM）', () => {
    for (const query of ['max-width: 900px', 'max-width: 720px']) {
      expect(mediaBlock(VUE_SOURCE, query)).not.toMatch(/display:\s*none|visibility:\s*hidden/)
    }
  })
})
