// @vitest-environment jsdom
// ↑ 挂载组件需要 DOM（本仓 vitest 默认 environment 是 node，只在本文件覆盖）。
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/** 「打手与客服报酬」v2（V2Performance）打手多选「其它分组」虚拟选项的判据（计划
 * `docs/plans/2026-11-07-settle-filter-rework.md` §4.4/§6.2）：系统用户多选下拉里、
 * 候选 `v-for` **之前**挂虚拟组头 + 两颗哨兵 option（2026-11-08 业务方置顶；排除禁止结算的 /
 * 排除总绩效为负的，文案与旧页逐字相同）；选中后 `buildParams` 经与旧页同一份
 * `splitPlayerFilterSelection` 翻译发参（`settle_forbidden_scope=excluded` /
 * `exclude_net_negative=1`），剩余用户名空 ⇒ 不发 `username`；缺省两键都不发。
 * 发参出口 = `buildParams`（列表 `load()` 与导出共用）。全部夹具都是合成值（PII 纪律）。 */
const fetchPerformancesWithSummary = vi.fn()

vi.mock('@/api/performances', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/performances')>()),
  fetchPerformancesWithSummary: (...args: unknown[]) =>
    fetchPerformancesWithSummary(...args),
}))

// 候选下拉的四个装载器：替身回空集（页面自己有「取不到不拦页」的 catch 口径，替身只为确定性）。
vi.mock('@/api/config', () => ({ fetchGames: vi.fn().mockResolvedValue([]) }))
vi.mock('@/api/orderSources', () => ({
  fetchOrderSources: vi.fn().mockResolvedValue({ items: [] }),
  fetchOrderSourceGroups: vi.fn().mockResolvedValue({ items: [] }),
}))
vi.mock('@/api/accounts', () => ({
  listAccounts: vi.fn().mockResolvedValue({ items: [] }),
}))

import { ElSelect } from 'element-plus'

import {
  PLAYER_OPTION_EXCLUDE_NET_NEGATIVE,
  PLAYER_OPTION_EXCLUDE_SETTLE_FORBIDDEN,
} from '@/api/performances'
import { listAccounts } from '@/api/accounts'
import { useSessionStore } from '@/stores/session'
import V2Performance from '../V2Performance.vue'

function makeRouter(): Router {
  const Blank = { template: '<div />' }
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/v2', name: 'v2', component: Blank, children: [{ path: '', name: 'v2-home', component: Blank }] },
      { path: '/v2/online-pay-detail', name: 'v2-online-pay-detail', component: Blank },
    ],
  })
}

async function mountPage(): Promise<{ wrapper: VueWrapper }> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const router = makeRouter()
  await router.push('/v2')
  await router.isReady()
  const wrapper = mount(V2Performance, { global: { plugins: [router, pinia] } })
  await flushPromises()
  return { wrapper }
}

it('禁止未完成订单发起付款，关闭已制单弹窗后提示打开同一张付款单', async () => {
  fetchPerformancesWithSummary.mockResolvedValue({
    items: [{
      id: 17, order_no: '20261107000000090017', order_status: 'IN_PROGRESS',
      settle_status: '未结账', emp_type: '打单工资', created_at: '2026-11-07T10:00:00',
      temp_amount: '20.00', bonus_amount: '20.00', deduct_fee: '0.00', deduct_mode: '',
      deduct_value: '0.00', display_name: '打手甲', username: 'worker', user: 5,
      log_user_name: 'recorder', order_price: '30.00', net_receipts: '25.00', refund_fee: '0.00', settled_at: null,
    }], total: 1, summary: null,
  })
  const pinia = createPinia()
  setActivePinia(pinia)
  useSessionStore().user = {
    id: 1, username: 'finance_actor', display_name: '财务甲', identity_plane: 'tenant',
    is_tenant_admin: false, roles: ['财务'], permissions: [],
    menu_permissions: ['finance.online_settlement_details', 'finance.change_settlement_status'],
  }
  const router = makeRouter()
  await router.push('/v2')
  const wrapper = mount(V2Performance, {
    global: {
      plugins: [router, pinia],
      stubs: {
        V2OnlinePayrollDialog: {
          props: ['selection'],
          template: '<button data-test="close-payroll" @click="$emit(\'close\', { active: true, id: \'original-batch\' })">{{ selection.category }}</button>',
        },
      },
    },
  })
  await flushPromises()
  await wrapper.get('[data-v2-check="17"]').setValue(true)
  await wrapper.get('[data-v2="pay"]').trigger('click')
  expect(wrapper.text()).toContain('只能对已完成订单的未结账报酬')
  expect(wrapper.find('[data-test="close-payroll"]').exists()).toBe(false)

  const current = await fetchPerformancesWithSummary()
  fetchPerformancesWithSummary.mockResolvedValue({
    ...current, items: [{ ...current.items[0], order_status: 'COMPLETED', emp_type: '销售提成' }],
  })
  await wrapper.get('[data-v2-tab="sales"]').trigger('click')
  await flushPromises()
  await wrapper.get('[data-v2-check="17"]').setValue(true)
  await wrapper.get('[data-v2="pay"]').trigger('click')
  expect(wrapper.get('[data-test="close-payroll"]').text()).toBe('customer_service')
  await wrapper.get('[data-test="close-payroll"]').trigger('click')
  const hint = wrapper.get('[data-v2="payroll-resume"]')
  expect(hint.text()).toContain('可前往「在线结算明细」继续付款')
  expect(hint.get('a').attributes('href')).toBe('/v2/online-pay-detail?batch=original-batch')
  wrapper.unmount()
})

/** 按 data-v2-filter 锚点找 el-select 组件（照 V2Income.spec：EP 化后原生 setValue 不再适用——emit 模拟选中）。 */
function selectByAnchor(wrapper: VueWrapper, anchor: string) {
  const found = wrapper
    .findAllComponents(ElSelect)
    .find((c) => c.attributes('data-v2-filter') === anchor)
  expect(found, `应存在 data-v2-filter="${anchor}" 的 el-select`).toBeDefined()
  return found!
}

/** 最近一次取数的第一参（`{...buildParams(), page, page_size}`）。 */
function lastParams(): Record<string, unknown> {
  const calls = fetchPerformancesWithSummary.mock.calls
  expect(calls.length, '应已发生过取数').toBeGreaterThan(0)
  const last = calls[calls.length - 1]!
  return last[0] as Record<string, unknown>
}

const EMPTY_PAGE = { items: [], total: 0, summary: null }

beforeEach(() => {
  vi.clearAllMocks()
  fetchPerformancesWithSummary.mockResolvedValue(EMPTY_PAGE)
})

it('选择每页 300 条后回到第一页，实际显示 300 条并按新大小翻页', async () => {
  const items = Array.from({ length: 307 }, (_, index) => ({
    id: index + 1, emp_type: '打单工资', created_at: '2026-11-07T10:00:00',
  }))
  fetchPerformancesWithSummary.mockImplementation(async ({ page, page_size }) => {
    const start = (page - 1) * page_size
    return { items: items.slice(start, start + page_size), total: items.length, summary: null }
  })
  const { wrapper } = await mountPage()
  const nextPage = () => wrapper.get('[data-v2="pager"] .btn-next').trigger('click')

  expect(wrapper.findAll('[data-v2-row]')).toHaveLength(50)
  await nextPage()
  await flushPromises()
  expect(lastParams()).toMatchObject({ page: 2, page_size: 50 })
  await wrapper.get('[data-v2-check="51"]').setValue(true)

  await wrapper.get('[data-v2="page-size"]').setValue('300')
  await flushPromises()
  expect(lastParams()).toMatchObject({ page: 1, page_size: 300 })
  expect(wrapper.findAll('[data-v2-row]')).toHaveLength(300)
  expect(wrapper.get<HTMLInputElement>('[data-v2-check="51"]').element.checked).toBe(false)
  expect(wrapper.get('[data-v2="pager-total"]').text()).toBe('共 307 条')

  await nextPage()
  await flushPromises()
  expect(lastParams()).toMatchObject({ page: 2, page_size: 300 })
  expect(wrapper.findAll('[data-v2-row]')).toHaveLength(7)
  expect(wrapper.find('[data-v2-row="301"]').exists()).toBe(true)
  wrapper.unmount()
})

describe('V2Performance：打手多选「其它分组」虚拟选项（2026-11-07）', () => {
  it('隐藏指定列并用显示名显示记录者', async () => {
    fetchPerformancesWithSummary.mockResolvedValue({
      items: [{
        id: 1, order_no: '20261107000000090001', order_status: 'IN_PROGRESS',
        settle_status: '未结账', emp_type: '打单工资', created_at: '2026-11-07T10:00:00',
        temp_amount: '20.00', bonus_amount: '20.00', deduct_fee: '0.00', deduct_mode: '',
        deduct_value: '0.00', display_name: '打手昵称', username: 'worker_login', user: 5,
        log_user_name: 'recorder_login', log_user_display: '记录人甲', game_role: '角色甲', dl_project: '项目甲', wangwang: '联系甲',
        order_price: '30.00', net_receipts: '25.00', refund_fee: '0.00', settled_at: null,
      }], total: 1, summary: null,
    })
    const { wrapper } = await mountPage()
    const headers = wrapper.findAll('thead th').map((th) => th.text())
    expect(headers).not.toContain('角色名')
    expect(headers).not.toContain('订单项目')
    expect(headers).not.toContain('旺旺/YY')
    expect(wrapper.text()).toContain('记录人甲')
    expect(wrapper.text()).not.toContain('recorder_login')
    expect(wrapper.text()).not.toContain('联系甲')
    expect(wrapper.find('.perf-th-order').text()).toBe('关联订单')
  })

  it('记录者显示名不改变提成说明的原有判据', async () => {
    fetchPerformancesWithSummary.mockResolvedValue({
      items: [{
        id: 2, order_no: '20261107000000090002', order_status: 'IN_PROGRESS',
        settle_status: '未结账', emp_type: '销售提成', created_at: '2026-11-07T10:00:00',
        temp_amount: '20.00', bonus_amount: '20.00', deduct_fee: '0.00', deduct_mode: '',
        deduct_value: '0.00', display_name: '客服', username: 'sales_login', user: 5,
        log_user_name: '固定账号', log_user_display: '记录人乙',
        order_price: '30.00', net_receipts: '25.00', refund_fee: '0.00', settled_at: null,
      }], total: 1, summary: null,
    })
    const { wrapper } = await mountPage()
    expect(wrapper.text()).toContain('记录人乙')
    expect(wrapper.get('[data-v2-cell="tempAmount"]').attributes('title')).toContain('按固定金额提成')
  })

  it('系统用户下拉里有「其它分组」组头与两颗哨兵 option，文案与旧页逐字相同', async () => {
    const { wrapper } = await mountPage()
    const group = wrapper.find('[data-v2-filter="player-virtual-group"]')
    expect(group.exists()).toBe(true)
    // el-option-group 把 label 渲染成组头标题元素（EP 惯例，不在组件属性上）。
    const text = group.text()
    expect(text).toContain('其它分组')
    expect(text).toContain('排除禁止结算的')
    expect(text).toContain('排除总绩效为负的')
  })

  it('缺省（没勾哨兵）不发参：首次取数与再点一次搜索都缺两个哨兵键', async () => {
    const { wrapper } = await mountPage()
    expect(fetchPerformancesWithSummary).toHaveBeenCalledTimes(1)
    expect('settle_forbidden_scope' in lastParams()).toBe(false)
    expect('exclude_net_negative' in lastParams()).toBe(false)

    await wrapper.find('[data-v2="search"]').trigger('click')
    await flushPromises()
    expect('settle_forbidden_scope' in lastParams()).toBe(false)
    expect('exclude_net_negative' in lastParams()).toBe(false)
  })

  it('勾「排除禁止结算的」⇒ excluded 真发参；取消勾选 ⇒ 键消失', async () => {
    const { wrapper } = await mountPage()
    selectByAnchor(wrapper, 'system-user').vm.$emit('update:modelValue', [
      PLAYER_OPTION_EXCLUDE_SETTLE_FORBIDDEN,
    ])
    await wrapper.find('[data-v2="search"]').trigger('click')
    await flushPromises()
    // 全是哨兵 ⇒ 剩余用户名空 ⇒ `username` 也不发。
    expect(lastParams()).toMatchObject({ settle_forbidden_scope: 'excluded' })
    expect('username' in lastParams()).toBe(false)

    selectByAnchor(wrapper, 'system-user').vm.$emit('update:modelValue', [])
    await wrapper.find('[data-v2="search"]').trigger('click')
    await flushPromises()
    expect('settle_forbidden_scope' in lastParams()).toBe(false)
  })

  it('⭐ 2026-11-07 R3：`negative_debt` 行 ⇒ 系统用户格渲染红签「挂负账」（data-v2 锚点钉值）', async () => {
    fetchPerformancesWithSummary.mockResolvedValue({
      items: [
        {
          id: 901, order_no: '20261107000000090001', order_status: 'IN_PROGRESS',
          settle_status: '未结账', emp_type: '打单工资', created_at: '2026-11-07T10:00:00',
          temp_amount: '20.00', bonus_amount: '20.00', deduct_fee: '0.00',
          deduct_mode: '', deduct_value: '0.00', display_name: '红签打手甲',
          username: 'neg-debt-player', user: 5, negative_debt: true,
        },
        {
          id: 902, order_no: '20261107000000090002', order_status: 'IN_PROGRESS',
          settle_status: '未结账', emp_type: '打单工资', created_at: '2026-11-07T10:00:00',
          temp_amount: '20.00', bonus_amount: '20.00', deduct_fee: '0.00',
          deduct_mode: '', deduct_value: '0.00', display_name: '普通打手乙',
          username: 'plain-player', user: 6, negative_debt: false,
        },
      ],
      total: 2,
      summary: null,
    })
    const { wrapper } = await mountPage()
    const tags = wrapper.findAll('[data-v2="neg-debt-tag"]')
    expect(tags).toHaveLength(1)
    expect(tags[0]!.text()).toBe('挂负账')
  })

  it('勾「排除总绩效为负的」⇒ exclude_net_negative=1；两颗并存各发各的；与真用户名并存时 username 只剩用户名', async () => {
    const { wrapper } = await mountPage()
    selectByAnchor(wrapper, 'system-user').vm.$emit('update:modelValue', [
      PLAYER_OPTION_EXCLUDE_NET_NEGATIVE,
    ])
    await wrapper.find('[data-v2="search"]').trigger('click')
    await flushPromises()
    expect(lastParams()).toMatchObject({ exclude_net_negative: '1' })

    selectByAnchor(wrapper, 'system-user').vm.$emit('update:modelValue', [
      'dailian_A',
      PLAYER_OPTION_EXCLUDE_SETTLE_FORBIDDEN,
      PLAYER_OPTION_EXCLUDE_NET_NEGATIVE,
    ])
    await wrapper.find('[data-v2="search"]').trigger('click')
    await flushPromises()
    expect(lastParams()).toMatchObject({
      username: 'dailian_A',
      settle_forbidden_scope: 'excluded',
      exclude_net_negative: '1',
    })
  })

  it('⭐ 2026-11-08：打手下拉搜索开着 + 面板加高（钉 SFC 源文本形状；vitest 不注入 SFC 样式，渲染级判不了）', () => {
    const sfc = readFileSync(
      path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../V2Performance.vue'),
      'utf8',
    )
    // 搜索：el-select 必须带 `filterable`（EP 默认不开；缺了虚拟组里的哨兵也搜不到）。
    const tag = /<el-select[^>]*data-v2-filter="system-user"[^>]*>/.exec(sfc)?.[0] ?? ''
    expect(tag, '打手 el-select 标签没取到').not.toBe('')
    expect(tag).toContain('filterable')
    // ⭐ 2026-11-09：候选改关键词远程搜索——remote + remote-method + loading 接线在标签上。
    expect(tag).toContain('remote')
    expect(tag).toContain(':remote-method="searchPlayerOptions"')
    expect(tag).toContain(':loading="playerSearching"')
    // 面板加高：scoped 覆盖锚在打手那颗的 data-v2-filter 上（窄选择器，kefu 等其他下拉不动），
    // 值钉 420px（EP 默认 wrap 限高 274px，el-select.css）。
    const rule =
      /\[data-v2-filter='system-user'\]\s*:deep\(\.el-select-dropdown__wrap\)\s*\{[^}]*\}/.exec(
        sfc,
      )?.[0] ?? ''
    expect(rule, '面板加高的 scoped 覆盖不在（锚 data-v2-filter=system-user）').not.toBe('')
    expect(rule).toContain('max-height: 420px')
  })

  it('⭐ 2026-11-08：「其它分组」虚拟组**置顶**——模板里渲染在打手候选 `v-for` 之前（业务方：移到列表最前）', () => {
    // EP 的 el-select 按子节点顺序渲染下拉项；候选替身回空集 ⇒ 挂载级 DOM 断言没东西可比，
    // 顺序钉打在 SFC 源文本上（同上一条先例）。
    const sfc = readFileSync(
      path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../V2Performance.vue'),
      'utf8',
    )
    // ⭐ 2026-11-09 起打手候选 = `v-for="a in playerOptions"`（远程搜索结果）；销售客服那颗仍是
    // `accountOptions`（预载）⇒ 从 system-user 那颗 select 标签起找，钉的是**这一颗内部**的
    // 顺序：select 标签 → 虚拟组 → 远程候选。
    const selectAt = sfc.indexOf('data-v2-filter="system-user"')
    const groupAt = sfc.indexOf('data-v2-filter="player-virtual-group"')
    const candidatesAt = sfc.indexOf('v-for="a in playerOptions"', selectAt)
    expect(selectAt, '打手 el-select 标签不在模板里').toBeGreaterThan(-1)
    expect(groupAt, '虚拟组 el-option-group 不在模板里').toBeGreaterThan(-1)
    expect(candidatesAt, '打手候选 v-for 不在模板里').toBeGreaterThan(-1)
    expect(groupAt, '虚拟组应排在打手候选之前').toBeLessThan(candidatesAt)
  })
})

describe('V2Performance：系统用户（打手/下家）候选改关键词远程搜索（2026-11-09）', () => {
  const PAGE = { items: [], total: 0, page: 1, page_size: 200 }

  beforeEach(() => {
    // 每个 remote-method 用例自己给账号替身换实现（module 级的 `{items:[]}` 是给挂载预载用的）。
    vi.mocked(listAccounts).mockReset()
    vi.mocked(listAccounts).mockResolvedValue(PAGE as never)
  })

  /** 打手那颗下拉里渲染出来的候选文案（teleported=false ⇒ 下拉就在组件子树里，v-show 渲染）。 */
  function playerDropdownItems(wrapper: VueWrapper): string[] {
    const select = selectByAnchor(wrapper, 'system-user')
    return select.findAll('.el-select-dropdown__item span').map((t) => t.text())
  }

  it('挂载：预载只喂销售客服（listAccounts 一发、不带 key）；system-user 下拉 remote 接线在', async () => {
    const { wrapper } = await mountPage()
    await flushPromises()
    expect(listAccounts).toHaveBeenCalledTimes(1)
    expect(listAccounts).toHaveBeenCalledWith({ page_size: 200 })
    const select = selectByAnchor(wrapper, 'system-user')
    expect(select.props('remote')).toBe(true)
    expect(typeof select.props('remoteMethod')).toBe('function')
    // 候选改远程搜索后，挂载期打手候选是空的：下拉里只剩「其它分组」两颗哨兵。
    expect(playerDropdownItems(wrapper)).toEqual(['排除禁止结算的', '排除总绩效为负的'])
  })

  it('remote-method（非空词）⇒ 400ms 防抖后 listAccounts({key, page_size:200})，命中行进候选', async () => {
    const { wrapper } = await mountPage()
    await flushPromises()
    vi.mocked(listAccounts).mockResolvedValue({
      items: [
        { username: 'sq_001', display_name: '测试打手甲' },
        { username: '', display_name: '空登录名' },
      ],
      total: 2,
      page: 1,
      page_size: 200,
    } as never)
    const remote = selectByAnchor(wrapper, 'system-user').props('remoteMethod') as (q: string) => void
    remote('测试打手')
    // 防抖 400ms（真实定时器）：vi.waitFor 的轮询窗口覆盖它。
    await vi.waitFor(() =>
      expect(listAccounts).toHaveBeenCalledWith({ key: '测试打手', page_size: 200 }),
    )
    await flushPromises()
    // 命中行进候选（空登录名的行跳过）；哨兵仍在列。
    const items = playerDropdownItems(wrapper)
    expect(items).toContain('测试打手甲')
    expect(items).not.toContain('空登录名')
    expect(items).toContain('排除禁止结算的')
  })

  it('remote-method（空 / 空白词）⇒ 不发请求、候选清空（只剩哨兵）', async () => {
    const { wrapper } = await mountPage()
    await flushPromises()
    const remote = selectByAnchor(wrapper, 'system-user').props('remoteMethod') as (q: string) => void
    remote('')
    remote('   ')
    await new Promise((r) => setTimeout(r, 450)) // 越过防抖窗口：即便有实现退化成发请求，这里也等到它发生
    expect(listAccounts).toHaveBeenCalledTimes(1) // 只有挂载那一发预载
    // 先造一次候选，再空词清空（判别力：退化成「不清空」的实现会让本条红）。
    vi.mocked(listAccounts).mockResolvedValue({
      items: [{ username: 'sq_001', display_name: '测试打手甲' }],
      total: 1,
      page: 1,
      page_size: 200,
    } as never)
    remote('测试打手')
    await vi.waitFor(() => expect(listAccounts).toHaveBeenCalledTimes(2))
    await flushPromises()
    expect(playerDropdownItems(wrapper)).toContain('测试打手甲')
    remote('')
    await new Promise((r) => setTimeout(r, 450))
    await flushPromises()
    expect(playerDropdownItems(wrapper)).toEqual(['排除禁止结算的', '排除总绩效为负的'])
  })

  it('过期回包丢弃：先发慢、后发快 ⇒ 候选是后发那次的（先到的旧回包不覆盖）', async () => {
    const { wrapper } = await mountPage()
    await flushPromises()
    let resolveFirst!: (v: Awaited<ReturnType<typeof listAccounts>>) => void
    vi.mocked(listAccounts)
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveFirst = resolve
          }),
      )
      .mockImplementationOnce(
        async () =>
          ({
            items: [{ username: 'sq_late', display_name: '后发先至' }],
            total: 1,
            page: 1,
            page_size: 200,
          }) as never,
      )
    const remote = selectByAnchor(wrapper, 'system-user').props('remoteMethod') as (q: string) => void
    remote('先发的词')
    await vi.waitFor(() => expect(listAccounts).toHaveBeenCalledTimes(2)) // 挂载预载 + 第一发
    remote('后发的词')
    await vi.waitFor(() => expect(listAccounts).toHaveBeenCalledTimes(3))
    await flushPromises()
    expect(playerDropdownItems(wrapper)).toContain('后发先至')
    // 第一发此刻才回 ⇒ 过期（seq 不匹配）⇔ 不得覆盖第二发的候选。
    resolveFirst({
      items: [{ username: 'sq_first', display_name: '先发后至' }],
      total: 1,
      page: 1,
      page_size: 200,
    } as never)
    await flushPromises()
    const items = playerDropdownItems(wrapper)
    expect(items).not.toContain('先发后至')
    expect(items).toContain('后发先至')
  })
})
