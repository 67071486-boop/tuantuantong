// @vitest-environment jsdom
import { openOptions, selectOption } from '../../components/__tests__/selectOption'
import { installFramework7ForTest } from '../../styles/__tests__/framework7Setup'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import type { AxiosRequestConfig } from 'axios'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

// 传输层是唯一的桩边界（照 M1 既有页面 spec 同一手法）：api/performances 跑真代码，
// 判据打在 mock 的 request 被调时的 url / method / params 上。
// ⚠️ `http` 键必须给：`api/performances.ts` 顶层 `import { http, request }`。
vi.mock('@/api/http', () => ({ request: vi.fn(), http: {} }))

// List(asyncLoading/load-more/slot)、PullDownRefresh(modelValue/refresh/slot)、Toast。
vi.mock('../../framework7Ui', () => ({
  Toast: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn(), warning: vi.fn() }),
}))

import { Toast } from '../../framework7Ui'
import MobilePagedList from '../../components/MobilePagedList.vue'
import PullDownRefresh from '../../components/MobilePullToRefresh.vue'
import type { Mock } from 'vitest'
import { BizError } from '@/api/errors'
import { request } from '@/api/http'
import type { PerformanceListItem, PerformanceSummary } from '@/api/performances'
import type { User } from '@/api/auth'
import { useSessionStore } from '@/stores/session'
import PerformancePage from '../PerformancePage.vue'
import MinePage from '../MinePage.vue'

const requestMock = request as unknown as Mock
const toast = Toast as unknown as Mock & { success: Mock; error: Mock }

enableAutoUnmount(afterEach)

/* ── 会话夹具（合成值，E-0077；照 `MePage.spec.ts::loginAs` 的现成写法） ── */

function loginAs(menuPermissions: string[]): void {
  const session = useSessionStore()
  session.status = 'ready'
  session.user = {
    id: 7,
    username: 'ceshidashoujia',
    display_name: '测试打手甲',
    identity_plane: 'tenant_a',
    is_tenant_admin: false,
    roles: ['代练员'],
    permissions: [],
    menu_permissions: menuPermissions,
    tenant_name: '测试工作室甲',
    avatar_url: null,
  } as User
}

/* ── 合成夹具（E-0077：单号 10000000000000000002 起、项目/人名全部合成值） ── */

const SUMMARY: PerformanceSummary = {
  temp_amount: '70.00',
  bonus_amount: '65.50',
  order_price: '100.00',
  refund_fee: '0.00',
  profit_expected: '30.00',
  profit_actual: '25.50',
}

function rowFixture(overrides: Partial<PerformanceListItem> = {}): PerformanceListItem {
  return {
    id: 1,
    order_no: '10000000000000000002',
    source_order_no: '',
    user: 7,
    username: 'ceshidashoujia',
    emp_type: '打单工资',
    temp_amount: '70.00',
    bonus_amount: '65.50',
    deduct_mode: '',
    deduct_value: '0.00',
    taker_price: null,
    review_status: null,
    settle_status: '未结账',
    created_at: '2026-10-01T10:30:00+08:00',
    order_status: 'IN_PROGRESS',
    order_price: '100.00',
    net_receipts: null,
    refund_fee: '0.00',
    game_role: 'synth-role-a',
    dl_project: '测试项目甲',
    wangwang: '',
    kefu: '',
    log_user_name: '测试记录员甲',
    deduct_fee: null,
    settled_at: null,
    settled_by: '',
    settled_amount: null,
    negative_debt: false,
    ...overrides,
  }
}

/** 分页信封的 `data`（`core.envelope.paginated(summary=)` 形状，summary 与 items 同源）。 */
function pageData(rows: PerformanceListItem[], total: number, page = 1): unknown {
  return {
    items: rows,
    total,
    page,
    page_size: 20,
    summary: SUMMARY,
  }
}

function rowOf(id: number): PerformanceListItem {
  return rowFixture({
    id,
    order_no: `10000000000000000${String(id).padStart(2, '0')}`,
    temp_amount: `${id}.00`,
    bonus_amount: `${id}.50`,
  })
}

/* ── mock 的请求路由 ── */

let listImpl: (config: AxiosRequestConfig) => Promise<unknown>

function wireRequestMock(): void {
  requestMock.mockImplementation((config: AxiosRequestConfig) => listImpl(config))
}

installFramework7ForTest()

beforeEach(() => {
  setActivePinia(createPinia())
  loginAs(['my_performance', 'my_performance.export'])
  requestMock.mockReset()
  toast.mockClear()
  toast.error.mockClear()
  wireRequestMock()
})

function mountPage() {
  return mount(PerformancePage)
}

describe('我的绩效（订单与绩效页）', () => {
  it('首屏请求打在会话端点上：GET /api/business/performances/ + page=1 + page_size=20', async () => {
    listImpl = () => Promise.resolve(pageData([rowFixture()], 1))
    mountPage()
    await flushPromises()
    expect(requestMock).toHaveBeenCalledTimes(1)
    const config = requestMock.mock.calls[0]![0] as AxiosRequestConfig
    expect(config.url).toBe('/api/business/performances/')
    expect(config.method).toBe('get')
    expect(config.params).toEqual({ page: 1, page_size: 20 })
  })

  it('列表卡片渲染载荷真字段：单号 / 预计 / 实际 / 收入类型 / 项目 / 状态中文 / 结算状态 / 记录时间', async () => {
    listImpl = () =>
      Promise.resolve(
        pageData(
          [
            rowFixture({
              order_status: 'IN_PROGRESS',
              created_at: '2026-10-01T10:30:00+08:00',
            }),
            rowFixture({
              id: 2,
              order_no: '10000000000000000003',
              order_status: 'COMPLETED',
              settle_status: '已结账',
              emp_type: '销售提成',
              dl_project: '测试项目乙',
              temp_amount: '12.00',
              bonus_amount: '10.00',
            }),
          ],
          2,
        ),
      )
    const wrapper = mountPage()
    await flushPromises()

    const cards = wrapper.findAll('.m-perf-card')
    expect(cards).toHaveLength(2)

    const first = cards[0]!
    expect(first.find('[data-testid="perf-row-temp"]').text()).toBe('70.00')
    expect(first.find('[data-testid="perf-row-bonus"]').text()).toBe('65.50')
    expect(first.find('[data-testid="perf-row-status"]').text()).toBe('代练中')
    expect(first.find('[data-testid="perf-row-settle"]').text()).toBe('未结账')
    expect(first.text()).toContain('10000000000000000002')
    expect(first.text()).toContain('测试项目甲')
    expect(first.text()).toContain('打单工资')
    // 记录时间按串内偏移换算（+08:00 ⇒ 项目时区墙钟），不直接渲染 ISO 串。
    expect(first.text()).toContain('2026-10-01 10:30')

    const second = cards[1]!
    expect(second.find('[data-testid="perf-row-status"]').text()).toBe('已完成')
    expect(second.find('[data-testid="perf-row-settle"]').text()).toBe('已结账')
    expect(second.text()).toContain('销售提成')
  })

  it('⭐ D-0301（2026-10-24）：来源号非空 ⇒ 卡片单号逐字显示来源号（空 ⇒ 回退内置号）', async () => {
    listImpl = () =>
      Promise.resolve(
        pageData([rowFixture({ source_order_no: 'SRC-TEST-1' })], 1),
      )
    const wrapper = mountPage()
    await flushPromises()
    const card = wrapper.findAll('.m-perf-card')[0]!
    expect(card.find('.m-perf-card__no').text()).toBe('单号 SRC-TEST-1')
    // ⛔ 来源号非空时内置号不再出现在单号行。
    expect(card.find('.m-perf-card__no').text()).not.toContain('10000000000000000002')
  })

  it('持 order.list ⇒ 顶部统计六瓦片：中文字面逐字取桌面 emp-emplist 汇总行，值 = summary 六项', async () => {
    // 管理侧（客服 / 管理员形态）持 `order.list` ⇒ 六格全画（后端也照旧给值）。
    loginAs(['my_performance', 'order.list'])
    listImpl = () => Promise.resolve(pageData([rowFixture()], 1))
    const wrapper = mountPage()
    await flushPromises()
    const labels = wrapper.findAll('.m-perf-summary__label').map((w) => w.text())
    // ⛔ 逐字（E-0024）：与 `emp-emplist.ts::TOTAL_TEXT` 骨架那六个槽名一字不差。
    expect(labels).toEqual([
      '预计绩效',
      '生效绩效',
      '订单金额',
      '记录退款金额',
      '预计差额利润',
      '已生效差额利润',
    ])
    expect(wrapper.find('[data-testid="perf-summary-temp"]').text()).toBe('70.00')
    expect(wrapper.find('[data-testid="perf-summary-bonus"]').text()).toBe('65.50')
    expect(wrapper.find('[data-testid="perf-summary-order-price"]').text()).toBe('100.00')
    expect(wrapper.find('[data-testid="perf-summary-refund"]').text()).toBe('0.00')
    expect(wrapper.find('[data-testid="perf-summary-profit-expected"]').text()).toBe('30.00')
    expect(wrapper.find('[data-testid="perf-summary-profit-actual"]').text()).toBe('25.50')
  })

  it('只持 finance.payroll（财务）⇒ 六瓦片照常画（业务方 2026-10-22 拍板：财务是合法编制）', async () => {
    // 判据是**两节点任一**（与后端 `can_view_order_money` 同一把钥匙）：财务看不到订单金额
    // 就没法对账，把真金额藏成 —/0.00 比不藏更糟。
    loginAs(['my_performance', 'my_performance.export', 'finance.payroll'])
    listImpl = () => Promise.resolve(pageData([rowFixture()], 1))
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.findAll('.m-perf-summary__label')).toHaveLength(6)
    expect(wrapper.find('[data-testid="perf-summary-order-price"]').text()).toBe('100.00')
    expect(wrapper.find('[data-testid="perf-summary-profit-actual"]').text()).toBe('25.50')
  })

  it('不持 order.list（打手）⇒ 订单金额 / 退款 / 两个差额利润四格**不在 DOM 里**（⛔ 不是 —）', async () => {
    // 后端对两把钥匙都没有的人把 summary 这四项给 `null`（字段脱敏、不是 403）
    // ⇒ 前端按节点整格不渲染。夹具照后端真实形状给 `null`（不是编造 `0.00`）。
    listImpl = () =>
      Promise.resolve({
        items: [rowFixture()],
        total: 1,
        page: 1,
        page_size: 20,
        summary: {
          ...SUMMARY,
          order_price: null,
          refund_fee: null,
          profit_expected: null,
          profit_actual: null,
        },
      })
    const wrapper = mountPage()
    await flushPromises()

    // 他自己挣的钱照旧画（这是本页对打手的主体信息）。
    expect(wrapper.find('[data-testid="perf-summary-temp"]').text()).toBe('70.00')
    expect(wrapper.find('[data-testid="perf-summary-bonus"]').text()).toBe('65.50')
    const labels = wrapper.findAll('.m-perf-summary__label').map((w) => w.text())
    expect(labels).toEqual(['预计绩效', '生效绩效'])

    // ⛔ 既不渲染成 `—`（会被当成加载失败），也不置灰占位：四个 testid 直接不存在。
    for (const testid of [
      'perf-summary-order-price',
      'perf-summary-refund',
      'perf-summary-profit-expected',
      'perf-summary-profit-actual',
    ]) {
      expect(wrapper.find(`[data-testid="${testid}"]`).exists()).toBe(false)
    }
    expect(wrapper.find('[data-testid="perf-summary"]').text()).not.toContain('—')
  })

  it('空态：0 行 → 「暂无绩效记录」（合计仍显示 summary 的 0.00 口径）', async () => {
    listImpl = () =>
      Promise.resolve({
        items: [],
        total: 0,
        page: 1,
        page_size: 20,
        summary: { ...SUMMARY, temp_amount: '0.00', bonus_amount: '0.00' },
      })
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('[data-testid="perf-empty"]').text()).toBe('暂无绩效记录')
    expect(wrapper.find('[data-testid="perf-summary-temp"]').text()).toBe('0.00')
  })

  it('卡片带共享轮廓类 m-card（真机反馈第 1 条；样式唯一出处 src/m/styles/card.css）', async () => {
    listImpl = () => Promise.resolve(pageData([rowFixture()], 1))
    const wrapper = mountPage()
    await flushPromises()
    const cards = wrapper.findAll('.m-perf-card')
    expect(cards).toHaveLength(1)
    expect(cards[0]!.classes()).toContain('m-card')
  })

  it('取数失败：错误态显示后端 msg 原样 + toast；重试按钮重新发请求并恢复', async () => {
    listImpl = () => Promise.reject(new BizError(1014, '该绩效记录不存在或已被删除'))
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('[data-testid="perf-error"]').text()).toContain(
      '该绩效记录不存在或已被删除',
    )
    expect(toast.error).toHaveBeenCalledWith('该绩效记录不存在或已被删除')

    listImpl = () => Promise.resolve(pageData([rowFixture()], 1))
    await wrapper.find('[data-testid="perf-retry"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="perf-error"]').exists()).toBe(false)
    expect(wrapper.findAll('.m-perf-card')).toHaveLength(1)
    expect(requestMock).toHaveBeenCalledTimes(2)
  })

  it('上拉加载更多：追加下一页（page=2），翻页不改顶部合计；取完不再发', async () => {
    const firstPage = Array.from({ length: 20 }, (_, i) => rowOf(i + 1))
    const secondPage = Array.from({ length: 20 }, (_, i) => rowOf(i + 21))
    let calls = 0
    listImpl = (config: AxiosRequestConfig) => {
      calls += 1
      const params = config.params as { page: number }
      if (params.page === 1) return Promise.resolve(pageData(firstPage, 40, 1))
      expect(params.page).toBe(2)
      return Promise.resolve(pageData(secondPage, 40, 2))
    }
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.findAll('.m-perf-card')).toHaveLength(20)
    expect(wrapper.getComponent({ name: 'MobilePagedList' }).props('asyncLoading')).toBe('load-more')

    await wrapper.find('.m-paged-list__more').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.m-perf-card')).toHaveLength(40)
    // 合计仍是全集口径的两个数（翻页不改它）。
    expect(wrapper.find('[data-testid="perf-summary-temp"]').text()).toBe('70.00')
    // 取完 ⇒ 不再渲染「加载更多」档。
    expect(wrapper.getComponent({ name: 'MobilePagedList' }).props('asyncLoading')).toBeUndefined()
    expect(calls).toBe(2)

    // 没有更多时再点 ⇒ 不发第三发。
    expect(wrapper.find('.m-paged-list__more').exists()).toBe(false)
    wrapper.getComponent(MobilePagedList).vm.$emit('load-more')
    await flushPromises()
    expect(calls).toBe(2)
  })

  it('加载更多失败：toast msg 原样、列表与页码不前进（下一发仍是 page=2）', async () => {
    const firstPage = Array.from({ length: 20 }, (_, i) => rowOf(i + 1))
    listImpl = (config: AxiosRequestConfig) => {
      const params = config.params as { page: number }
      if (params.page === 1) return Promise.resolve(pageData(firstPage, 40, 1))
      return Promise.reject(new BizError(1000, '页码超出范围'))
    }
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.find('.m-paged-list__more').trigger('click')
    await flushPromises()
    expect(toast.error).toHaveBeenCalledWith('页码超出范围')
    expect(wrapper.findAll('.m-perf-card')).toHaveLength(20)

    // 修好后再点，仍是 page=2（页码没有被失败的尝试顶掉）。
    let secondPageAsked = false
    listImpl = (config: AxiosRequestConfig) => {
      const params = config.params as { page: number }
      if (params.page === 1) return Promise.resolve(pageData(firstPage, 40, 1))
      secondPageAsked = true
      return Promise.resolve(pageData([rowOf(21)], 21, 2))
    }
    await wrapper.find('.m-paged-list__more').trigger('click')
    await flushPromises()
    expect(secondPageAsked).toBe(true)
    expect(wrapper.findAll('.m-perf-card')).toHaveLength(21)
  })

  it('下拉刷新 = 重取第 1 页并整表替换', async () => {
    let calls = 0
    listImpl = (config: AxiosRequestConfig) => {
      calls += 1
      const params = config.params as { page: number }
      if (params.page === 1) {
        return Promise.resolve(
          pageData(calls === 1 ? [rowFixture()] : [rowFixture(), rowOf(2)], calls === 1 ? 1 : 2),
        )
      }
      return Promise.resolve(pageData([rowOf(99)], 99, params.page))
    }
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/mine', component: MinePage }] })
    await router.push('/mine')
    const wrapper = mount(MinePage, { global: { plugins: [router] } })
    await flushPromises()
    expect(wrapper.findAll('.m-perf-card')).toHaveLength(1)

    await wrapper.getComponent(PullDownRefresh).vm.$emit('refresh')
    await flushPromises()
    // 刷新走 page=1（不是往下翻页），整表替换成 2 行。
    expect(requestMock.mock.calls[1]![0] as AxiosRequestConfig).toMatchObject({
      url: '/api/business/performances/',
      params: { page: 1, page_size: 20 },
    })
    expect(wrapper.findAll('.m-perf-card')).toHaveLength(2)
  })
})

/* ── 服务端筛选（真机反馈第一批第 3 条）：参数打在请求 params 上，筛选变更回第 1 页 ── */

describe('我的绩效 — 服务端筛选', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('新筛选失败时不把原筛选的金额或明细显示为新结果', async () => {
    listImpl = ({ params }) => params.settle_status === '已结账'
      ? Promise.reject(new BizError(1014, '查询失败'))
      : Promise.resolve(pageData([rowFixture()], 1))
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.get('[data-testid="perf-summary-temp"]').text()).toBe('70.00')
    expect(wrapper.findAll('.m-perf-card')).toHaveLength(1)

    await selectOption(wrapper, 'perf-settle-filter', '已结账')
    await flushPromises()
    expect(wrapper.get('[data-testid="perf-summary-temp"]').text()).toBe('—')
    expect(wrapper.findAll('.m-perf-card')).toHaveLength(0)
    expect(wrapper.get('[data-testid="perf-error"]').text()).toContain('查询失败')
  })

  it('结算状态筛选：请求带 settle_status（后端 SettleStatus 中文存储值），页码重置回 1', async () => {
    const firstPage = Array.from({ length: 20 }, (_, i) => rowOf(i + 1))
    listImpl = (config: AxiosRequestConfig) => {
      const params = config.params as { page: number }
      return Promise.resolve(pageData(params.page === 1 ? firstPage : [rowOf(21)], 21, params.page))
    }
    const wrapper = mountPage()
    await flushPromises()
    // 先翻到第 2 页，再筛 ⇒ 页码必须重置回 1（筛选变更 = 重取，不是接着翻）。
    await wrapper.find('.m-paged-list__more').trigger('click')
    await flushPromises()

    await selectOption(wrapper, 'perf-settle-filter', '未结账')
    await flushPromises()
    const last = requestMock.mock.calls[requestMock.mock.calls.length - 1]![0] as AxiosRequestConfig
    expect(last.url).toBe('/api/business/performances/')
    expect(last.params).toEqual({ page: 1, page_size: 20, settle_status: '未结账' })
  })

  it('订单状态筛选：选项来自全站共享枚举（8 档中文），请求送**枚举名**', async () => {
    listImpl = () => Promise.resolve(pageData([rowFixture()], 1))
    const wrapper = mountPage()
    await flushPromises()
    const dialog = await openOptions(wrapper, 'perf-status-filter')
    const options = dialog.findAll('button[data-option-value]')
    await dialog.get('.m-select__close').trigger('click')
    // 「全部状态」+ 8 个状态档（`@/config/orderStatus`，后端 choices 的镜像）。
    expect(options.map((option) => option.get('span').text())).toEqual([
      '全部状态',
      '未分配', '待抢单', '代练中', '待验收', '已完成', '问题单', '已暂停', '已退单',
    ])

    await selectOption(wrapper, 'perf-status-filter', 'COMPLETED')
    await flushPromises()
    const last = requestMock.mock.calls[requestMock.mock.calls.length - 1]![0] as AxiosRequestConfig
    expect(last.params).toEqual({ page: 1, page_size: 20, order_status: 'COMPLETED' })
  })

  it('关键字筛选：300ms 防抖后请求带 keyword（去空白），防抖窗口内不发', async () => {
    listImpl = () => Promise.resolve(pageData([rowFixture()], 1))
    const wrapper = mountPage()
    await flushPromises()
    vi.useFakeTimers()

    const input = wrapper.find('[data-testid="perf-keyword"] input')
    ;(input.element as HTMLInputElement).value = '  10000000000000000002  '
    await input.trigger('input')
    // 防抖窗口内：不发新请求。
    expect(requestMock.mock.calls).toHaveLength(1)
    vi.advanceTimersByTime(300)
    await flushPromises()
    expect(requestMock.mock.calls).toHaveLength(2)
    const last = requestMock.mock.calls[requestMock.mock.calls.length - 1]![0] as AxiosRequestConfig
    expect(last.params).toEqual({ page: 1, page_size: 20, keyword: '10000000000000000002' })

    // 清空关键字：到点重取，keyword 不送。
    ;(input.element as HTMLInputElement).value = ''
    await input.trigger('input')
    vi.advanceTimersByTime(300)
    await flushPromises()
    expect(requestMock.mock.calls).toHaveLength(3)
    expect((requestMock.mock.calls[requestMock.mock.calls.length - 1]![0] as AxiosRequestConfig).params).toEqual({
      page: 1,
      page_size: 20,
    })
  })

  it('筛选后加载更多：翻页请求带着同一份筛选参数（筛选对后续页持续生效）', async () => {
    const firstPage = Array.from({ length: 20 }, (_, i) => rowOf(i + 1))
    listImpl = (config: AxiosRequestConfig) => {
      const params = config.params as { page: number; settle_status?: string }
      if (params.page === 1) return Promise.resolve(pageData(firstPage, 40, 1))
      expect(params.settle_status).toBe('已结账')
      return Promise.resolve(pageData([rowOf(21)], 21, 2))
    }
    const wrapper = mountPage()
    await flushPromises()
    await selectOption(wrapper, 'perf-settle-filter', '已结账')
    await flushPromises()
    await wrapper.find('.m-paged-list__more').trigger('click')
    await flushPromises()
    const last = requestMock.mock.calls[requestMock.mock.calls.length - 1]![0] as AxiosRequestConfig
    expect(last.params).toEqual({ page: 2, page_size: 20, settle_status: '已结账' })
    expect(wrapper.findAll('.m-perf-card')).toHaveLength(21)
  })

  it('切换结算状态时，旧筛选分页返回得再晚也不会混进明细', async () => {
    let finishOldPage!: (result: unknown) => void
    listImpl = ({ params }) => {
      if (params.page === 2) return new Promise((resolve) => { finishOldPage = resolve })
      return Promise.resolve(params.settle_status === '已结账'
        ? pageData([rowFixture({ id: 3, order_no: '10000000000000000003' })], 1)
        : pageData([rowOf(1)], 2))
    }
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.get('.m-paged-list__more').trigger('click')
    await selectOption(wrapper, 'perf-settle-filter', '已结账')
    await flushPromises()

    finishOldPage(pageData([rowOf(2)], 2, 2))
    await flushPromises()
    expect(wrapper.findAll('.m-perf-card').map((card) => card.attributes('data-order-no'))).toEqual([
      '10000000000000000003',
    ])
  })
})
