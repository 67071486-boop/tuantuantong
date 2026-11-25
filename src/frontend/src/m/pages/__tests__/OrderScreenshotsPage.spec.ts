// @vitest-environment jsdom
import { installFramework7ForTest } from '../../styles/__tests__/framework7Setup'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import type { AxiosRequestConfig } from 'axios'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// 传输层是唯一的桩边界（照既有页面 spec 同一手法）：api/screenshots 跑真代码，判据打在 mock 的 request 被调时的 url / method / data 上。
// ⚠️ `CSRF_HEADER_NAME` 必须给：`layui-island/directUpload.ts` 顶层 import 它（同源直传要带 CSRF 头），缺这个导出会在上传分支拿到 undefined 键。
vi.mock('../../router/useWorkerBack', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../router/useWorkerBack')>(),
  useWorkerBack: () => h.back,
}))
vi.mock('@/api/http', () => ({ request: vi.fn(), http: {}, CSRF_HEADER_NAME: 'X-CSRFToken' }))

vi.mock('../../framework7Ui', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../framework7Ui')>()
  return {
    ...actual,
    Toast: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn(), warning: vi.fn() }),
    confirm: vi.fn(actual.confirm),
  }
})
vi.mock('../../components/MobilePhotoBrowser.vue', () => ({
  default: {
  name: 'MobilePhotoBrowser',
  props: ['visible', 'images', 'index', 'showIndex'],
  emits: ['update:visible', 'index-change'],
  template:
    '<div class="photo-browser-stub" :data-visible="String(visible)"'
    + ' :data-images="JSON.stringify(images || [])"'
    + ' :data-index="String(index)"></div>',
},
}))

// 路由参数是可变夹具：形状闸用例要塞非 20 位单号。vi.hoisted 保证工厂引用不踩 TDZ。
const h = vi.hoisted(() => ({
  params: { orderNo: '10000000000000000002' },
  back: vi.fn(),
  push: vi.fn(),
}))
vi.mock('vue-router', () => ({
  useRoute: () => ({ name: 'm-order-screenshots', params: h.params }),
  useRouter: () => ({ back: h.back, push: h.push }),
}))

/**
 * jsdom 没有 canvas、`Image` 也不会真的解码 ⇒ 真 `compressImage` 会卡在 loadImage。
 * 与桌面同页 spec 同一手法：只把 `compressImage` 换成**恒等**（= 「压缩失败回落原图」
 * 那条既有路径），四档画质本身的取值钉子由 `imageCompress.spec.ts` 负责（边界值全列）。
 */
vi.mock('@/layui-island/imageCompress', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui-island/imageCompress')>()
  return { ...actual, compressImage: vi.fn(async (file: File) => file) }
})

import { Toast, confirm } from '../../framework7Ui'
import type { Mock } from 'vitest'
import { BizError, HttpError } from '@/api/errors'
import { request } from '@/api/http'
import type { ScreenshotItem } from '@/api/screenshots'
import type { User } from '@/api/auth'
import { useSessionStore } from '@/stores/session'
import OrderScreenshotsPage from '../OrderScreenshotsPage.vue'

const requestMock = request as unknown as Mock
/** 中性提示走 Toast 本体、失败走 Toast.error ⇒ 两者都要可清可断。 */
const toast = Toast as unknown as Mock & { success: Mock; error: Mock }
const dialogConfirm = confirm as unknown as Mock
const dialogButton = (index: number) =>
  document.querySelectorAll<HTMLElement>('.dialog.modal-in .dialog-button')[index]!

enableAutoUnmount(afterEach)

/* ── 合成夹具（E-0077：单号 / 图名全部合成值） ── */

const ORDER_NO = '10000000000000000002'

function shotFixture(overrides: Partial<ScreenshotItem> = {}): ScreenshotItem {
  return {
    id: 5,
    img_type: 0,
    url: 'https://blob-synth.test.invalid/signed-progress-a',
    original_name: '测试截图甲.png',
    size_bytes: 1234,
    remark: '',
    uploaded_by: 7,
    created_at: '2026-10-10T08:05:00+08:00',
    ...overrides,
  }
}

/** 建一个指定字节数的合成图片文件（字节内容无关紧要，判的是 size 与扩展名）。 */
function fileOf(name: string, bytes: number): File {
  return new File([new Uint8Array(bytes)], name, { type: 'image/png' })
}

/* ── mock 的请求路由（四个端点各自可设实现） ── */

let listImpl: (config: AxiosRequestConfig) => Promise<unknown>
let ticketImpl: (config: AxiosRequestConfig) => Promise<unknown>
let claimImpl: (config: AxiosRequestConfig) => Promise<unknown>
let deleteImpl: (config: AxiosRequestConfig) => Promise<unknown>
/** 各端点的调用清单（判据打在 url / method / data 上）。 */
const callsOf = (pred: (config: AxiosRequestConfig) => boolean): AxiosRequestConfig[] =>
  requestMock.mock.calls.map((c) => c[0] as AxiosRequestConfig).filter(pred)

const isGet = (url: string) => (c: AxiosRequestConfig) =>
  c.url === url && String(c.method ?? 'get').toLowerCase() === 'get'
const isPost = (url: string) => (c: AxiosRequestConfig) =>
  c.url === url && String(c.method).toLowerCase() === 'post'

function wireRequestMock(): void {
  requestMock.mockImplementation((config: AxiosRequestConfig) => {
    const url = config.url ?? ''
    const method = String(config.method ?? 'get').toLowerCase()
    if (method === 'get' && url.endsWith('/screenshots')) return listImpl(config)
    if (method === 'post' && url.endsWith('/screenshots/ticket')) return ticketImpl(config)
    if (method === 'post' && url.endsWith('/screenshots')) return claimImpl(config)
    if (method === 'delete' && url.includes('/screenshots/')) return deleteImpl(config)
    throw new Error(`mock 未覆盖的请求：${method.toUpperCase()} ${url}`)
  })
}

const fetchMock = vi.fn()

/** 按 menu_permissions 造一个已就绪的会话（节点判据唯一来源）。 */
function loginAs(menuPermissions: string[]): void {
  const session = useSessionStore()
  session.status = 'ready'
  session.user = {
    id: 7,
    username: 'ceshidashoujia',
    display_name: '测试打手甲',
    identity_plane: 'tenant_a',
    is_tenant_admin: false,
    roles: [],
    permissions: [],
    menu_permissions: menuPermissions,
    tenant_name: '测试工作室甲',
  } as User
}

installFramework7ForTest()

beforeEach(() => {
  setActivePinia(createPinia())
  requestMock.mockReset()
  toast.mockClear()
  toast.error.mockClear()
  dialogConfirm.mockClear()
  h.params.orderNo = ORDER_NO
  h.back.mockClear()
  h.push.mockClear()
  fetchMock.mockReset()
  fetchMock.mockResolvedValue({ ok: true, status: 200 })
  vi.stubGlobal('fetch', fetchMock)
  // 默认全绿：列表空、三跳全成、删除成功。各用例自己覆盖。
  listImpl = () => Promise.resolve({ items: [], total: 0 })
  ticketImpl = (config: AxiosRequestConfig) => {
    const ext = (config.data as { ext: string }).ext
    return Promise.resolve({
      screenshot_id: 100 + callsOf(isPost(`/api/business/orders/${ORDER_NO}/screenshots/ticket`)).length,
      key: `synth.${ext}`,
      upload_url: `/blob/synth.${ext}`,
      expires_in: 600,
    })
  }
  claimImpl = () => Promise.resolve({ id: 11 })
  deleteImpl = () => Promise.resolve(null)
  wireRequestMock()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function mountPage() {
  return mount(OrderScreenshotsPage)
}

describe('订单截图页（/m/orders/:orderNo/screenshots）', () => {
  it('在 WxPusher 中准备当前订单的浏览器上传链接', async () => {
    vi.stubGlobal('navigator', { userAgent: 'Mozilla/5.0/WxPusher-Android' })
    loginAs(['order.upload_screenshot'])
    const challenge = 'a'.repeat(43)
    requestMock.mockImplementation((config: AxiosRequestConfig) => {
      if (config.url === '/api/business/screenshot-browser/start') return Promise.resolve({ challenge })
      return Promise.resolve({ items: [], total: 0 })
    })
    const wrapper = mountPage()
    expect(wrapper.find('[data-testid="shot-album-btn"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="shot-camera-btn"]').exists()).toBe(false)
    await wrapper.get('[data-testid="shot-browser-start"]').trigger('click')
    await flushPromises()
    expect(callsOf(isPost('/api/business/screenshot-browser/start'))[0]?.data).toEqual({ order_no: ORDER_NO })
    expect(h.push).toHaveBeenCalledWith({
      name: 'm-order-screenshot-browser',
      params: { orderNo: ORDER_NO },
      hash: `#${challenge}`,
    })
  })

  it('单号形状不符 → 404 态页，零请求', async () => {
    loginAs([])
    h.params.orderNo = 'abc'
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('[data-testid="shot-404"]').exists()).toBe(true)
    expect(requestMock).not.toHaveBeenCalled()
  })

  it('列表只渲染进度截图（img_type=0）；群码行不出现；计数 n/20', async () => {
    loginAs([])
    listImpl = () =>
      Promise.resolve({
        items: [
          shotFixture(),
          shotFixture({ id: 6, original_name: '群码.png', img_type: 1 }),
          shotFixture({ id: 7, url: 'https://blob-synth.test.invalid/b', original_name: '测试截图乙.png' }),
        ],
        total: 3,
      })
    const wrapper = mountPage()
    await flushPromises()
    const thumbs = wrapper.findAll('[data-testid="shot-thumb"]')
    expect(thumbs).toHaveLength(2)
    expect(thumbs.map((w) => w.attributes('src'))).toEqual([
      'https://blob-synth.test.invalid/signed-progress-a',
      'https://blob-synth.test.invalid/b',
    ])
    expect(wrapper.find('[data-testid="shot-count"]').text()).toBe('2/20')
  })

  it('空态：「还没有截图」', async () => {
    loginAs([])
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('[data-testid="shot-empty"]').text()).toBe('还没有截图')
  })

  it('取数失败：错误态显示后端 msg 原样 + 重试后恢复', async () => {
    loginAs([])
    listImpl = () => Promise.reject(new BizError(1002, '记录不存在或已被删除'))
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('[data-testid="shot-error"]').text()).toContain('记录不存在或已被删除')
    expect(toast.error).toHaveBeenCalledWith('记录不存在或已被删除')
    listImpl = () => Promise.resolve({ items: [shotFixture()], total: 1 })
    await wrapper.find('[data-testid="shot-error-retry"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="shot-error"]').exists()).toBe(false)
    expect(wrapper.findAll('[data-testid="shot-thumb"]')).toHaveLength(1)
  })

  it('列表 403（无查看节点）：错误态给「返回」不给「重试」（M1 详情页口径）', async () => {
    loginAs(['order.upload_screenshot'])
    listImpl = () => Promise.reject(new HttpError(403, '你没有查看订单图片的权限'))
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('[data-testid="shot-error"]').text()).toContain(
      '你没有查看订单图片的权限',
    )
    expect(wrapper.find('[data-testid="shot-error-back"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="shot-error-retry"]').exists()).toBe(false)
    await wrapper.find('[data-testid="shot-error-back"]').trigger('click')
    expect(h.back).toHaveBeenCalledTimes(1)
  })

  it('Important：仅有 upload 无 view 节点（列表 403）→ 上传区仍渲染且上传链路照常可走', async () => {
    loginAs(['order.upload_screenshot'])
    listImpl = () => Promise.reject(new HttpError(403, '你没有查看订单图片的权限'))
    const wrapper = mountPage()
    await flushPromises()
    // 列表块是错误态（对齐 M1：403 → 返回）……
    expect(wrapper.find('[data-testid="shot-error"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="shot-error-back"]').exists()).toBe(true)
    // ……但上传区**不随列表失败收窄**（桌面同组合可以传，手机端也必须能传）。
    expect(wrapper.find('[data-testid="shot-upload"]').exists()).toBe(true)
    const input = wrapper.find('[data-testid="shot-album-input"]')
    Object.defineProperty(input.element, 'files', {
      value: [fileOf('传图.png', 512)],
      configurable: true,
    })
    await input.trigger('change')
    await flushPromises()
    // 预检按已知快照（0 张）偏乐观 ⇒ 照常发三跳；后端才是权威（桌面同代价）。
    const tickets = callsOf(isPost(`/api/business/orders/${ORDER_NO}/screenshots/ticket`))
    expect(tickets).toHaveLength(1)
    expect(tickets[0]!.data).toEqual({ ext: 'png', img_type: 0 })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const claims = callsOf(isPost(`/api/business/orders/${ORDER_NO}/screenshots`))
    expect(claims).toHaveLength(1)
  })

  it('预览：点缩略图 → ImageViewer visible + images 恰好是全部进度截图', async () => {
    loginAs([])
    listImpl = () =>
      Promise.resolve({ items: [shotFixture({ thumbnail_url: 'https://blob-synth.test.invalid/thumb' }), shotFixture({ id: 7 })], total: 2 })
    const wrapper = mountPage()
    await flushPromises()
    const thumbs = wrapper.findAll('[data-testid="shot-thumb"]')
    expect(thumbs[0]!.attributes('src')).toBe('https://blob-synth.test.invalid/thumb')
    expect(thumbs[0]!.attributes('loading')).toBe('lazy')
    const viewer = wrapper.find('.photo-browser-stub')
    expect(viewer.attributes('data-visible')).toBe('false')
    await wrapper.findAll('[data-testid="shot-thumb"]')[1]!.trigger('click')
    expect(viewer.attributes('data-visible')).toBe('true')
    expect(JSON.parse(viewer.attributes('data-images')!)).toEqual([
      'https://blob-synth.test.invalid/signed-progress-a',
      'https://blob-synth.test.invalid/signed-progress-a',
    ])
  })

  it('上传成功：ticket(ext, img_type=0) → PUT → claim(screenshot_id, original_name, remark 空) → 重取列表', async () => {
    loginAs(['order.upload_screenshot'])
    const wrapper = mountPage()
    await flushPromises()
    const input = wrapper.find('[data-testid="shot-album-input"]')
    Object.defineProperty(input.element, 'files', {
      value: [fileOf('测试截图甲.png', 1024), fileOf('测试截图乙.jpg', 2048)],
      configurable: true,
    })
    await input.trigger('change')
    await flushPromises()

    const tickets = callsOf(isPost(`/api/business/orders/${ORDER_NO}/screenshots/ticket`))
    expect(tickets).toHaveLength(2)
    expect(tickets.map((c) => c.data)).toEqual([
      { ext: 'png', img_type: 0 },
      { ext: 'jpg', img_type: 0 },
    ])
    // 两发 PUT（浏览器直传，不走 request()）。
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(fetchMock.mock.calls[0]![0]).toBe('/blob/synth.png')
    expect(fetchMock.mock.calls[0]![1]).toMatchObject({ method: 'PUT' })
    // 认领按签发时那条 pending 行的主键（⛔ 不是 key）。
    const claims = callsOf(isPost(`/api/business/orders/${ORDER_NO}/screenshots`))
    expect(claims.map((c) => c.data)).toEqual([
      { screenshot_id: 101, original_name: '测试截图甲.png', remark: '' },
      { screenshot_id: 102, original_name: '测试截图乙.jpg', remark: '' },
    ])
    // 传完重取列表（第 2 发 GET）。
    expect(callsOf(isGet(`/api/business/orders/${ORDER_NO}/screenshots`))).toHaveLength(2)
  })

  it('坏类型（.gif）：提示逐字「只支持 jpg / jpeg / png」，零请求', async () => {
    loginAs(['order.upload_screenshot'])
    const wrapper = mountPage()
    await flushPromises()
    const input = wrapper.find('[data-testid="shot-album-input"]')
    Object.defineProperty(input.element, 'files', {
      value: [fileOf('动图.gif', 1024)],
      configurable: true,
    })
    await input.trigger('change')
    await flushPromises()
    expect(toast).toHaveBeenCalledWith('只支持 jpg / jpeg / png')
    expect(toast).toHaveBeenCalledTimes(1)
    expect(callsOf(isPost(`/api/business/orders/${ORDER_NO}/screenshots/ticket`))).toHaveLength(0)
  })

  it('原图超 18MiB：提示「已自动过滤1张18MB以上的图片」，零请求', async () => {
    loginAs(['order.upload_screenshot'])
    const wrapper = mountPage()
    await flushPromises()
    const input = wrapper.find('[data-testid="shot-album-input"]')
    Object.defineProperty(input.element, 'files', {
      value: [fileOf('大图.png', 18 * 1024 * 1024 + 1)],
      configurable: true,
    })
    await input.trigger('change')
    await flushPromises()
    // 顺序照桌面：先「已自动过滤N张18MB以上的图片」，一张都没剩再补收尾句（桌面同两条）。
    expect(toast).toHaveBeenCalledWith('已自动过滤1张18MB以上的图片')
    expect(toast).toHaveBeenLastCalledWith('图片都太大了，请把图片大小控制在单张18MB以内')
    expect(callsOf(isPost(`/api/business/orders/${ORDER_NO}/screenshots/ticket`))).toHaveLength(0)
  })

  it('压缩后仍超 5MiB（jsdom 恒等回落原图）：提示 5MB 过滤句 + 收尾句，零请求', async () => {
    loginAs(['order.upload_screenshot'])
    const wrapper = mountPage()
    await flushPromises()
    const input = wrapper.find('[data-testid="shot-album-input"]')
    Object.defineProperty(input.element, 'files', {
      value: [fileOf('压不小.png', 5 * 1024 * 1024 + 1)],
      configurable: true,
    })
    await input.trigger('change')
    await flushPromises()
    // 顺序照桌面：先「已自动过滤N张5MB以上的图片」，一张都没剩再补收尾句。
    expect(toast).toHaveBeenCalledWith('已自动过滤1张5MB以上的图片')
    expect(toast).toHaveBeenLastCalledWith('图片都太大了，请把图片大小控制在单张18MB以内')
    expect(callsOf(isPost(`/api/business/orders/${ORDER_NO}/screenshots/ticket`))).toHaveLength(0)
  })

  it('已满 20 张：超限文案逐字，整批零请求', async () => {
    loginAs(['order.upload_screenshot'])
    listImpl = () =>
      Promise.resolve({
        items: Array.from({ length: 20 }, (_, i) => shotFixture({ id: i + 1 })),
        total: 20,
      })
    const wrapper = mountPage()
    await flushPromises()
    const input = wrapper.find('[data-testid="shot-album-input"]')
    Object.defineProperty(input.element, 'files', {
      value: [fileOf('再传一张.png', 1024)],
      configurable: true,
    })
    await input.trigger('change')
    await flushPromises()
    expect(toast).toHaveBeenCalledWith('单个订单最多上传20张截图，请先删除后再上传')
    expect(toast).toHaveBeenCalledTimes(1)
    expect(callsOf(isPost(`/api/business/orders/${ORDER_NO}/screenshots/ticket`))).toHaveLength(0)
  })

  it('19 张传 3 张：只放行前面 1 张（超出的零往返），提示超限句', async () => {
    loginAs(['order.upload_screenshot'])
    listImpl = () =>
      Promise.resolve({
        items: Array.from({ length: 19 }, (_, i) => shotFixture({ id: i + 1 })),
        total: 19,
      })
    const wrapper = mountPage()
    await flushPromises()
    const input = wrapper.find('[data-testid="shot-album-input"]')
    Object.defineProperty(input.element, 'files', {
      value: [fileOf('甲.png', 1024), fileOf('乙.png', 1024), fileOf('丙.png', 1024)],
      configurable: true,
    })
    await input.trigger('change')
    await flushPromises()
    expect(toast).toHaveBeenCalledWith('单个订单最多上传20张截图，请先删除后再上传')
    const tickets = callsOf(isPost(`/api/business/orders/${ORDER_NO}/screenshots/ticket`))
    expect(tickets).toHaveLength(1)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('上传失败：后端 msg 原样 toast（⛔ 不被前端文案覆盖）', async () => {
    loginAs(['order.upload_screenshot'])
    ticketImpl = () =>
      Promise.reject(new BizError(1002, '单个订单最多上传20张截图，请先删除后再上传'))
    const wrapper = mountPage()
    await flushPromises()
    const input = wrapper.find('[data-testid="shot-album-input"]')
    Object.defineProperty(input.element, 'files', {
      value: [fileOf('甲.png', 1024)],
      configurable: true,
    })
    await input.trigger('change')
    await flushPromises()
    expect(toast.error).toHaveBeenCalledWith('单个订单最多上传20张截图，请先删除后再上传')
  })

  it('删除：确认文案逐字（钉字面值）；确认 → DELETE /screenshots/<id> + 重取；取消 → 不发', async () => {
    loginAs(['order.screenshot_manage'])
    listImpl = () => Promise.resolve({ items: [shotFixture()], total: 1 })
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.find('[data-testid="shot-del-5"]').trigger('click')
    expect(dialogConfirm).toHaveBeenCalledTimes(1)
    // E-0024：钉字面值，不 import 常量自指。
    expect(dialogConfirm.mock.calls[0]![0]).toMatchObject({
      content: '确定要删除吗？提示：一旦删除，将不可恢复！',
      confirmBtn: '确定',
      cancelBtn: '取消',
    })
    const gets = callsOf(isGet(`/api/business/orders/${ORDER_NO}/screenshots`))
    expect(gets).toHaveLength(1)

    // 取消：不发 DELETE。
    dialogButton(0).click()
    await flushPromises()
    expect(callsOf((c) => String(c.method).toLowerCase() === 'delete')).toHaveLength(0)

    // 确认：逐张 DELETE（行主键）+ 重取列表。
    dialogConfirm.mockClear()
    await wrapper.find('[data-testid="shot-del-5"]').trigger('click')
    dialogButton(1).click()
    await flushPromises()
    const deletes = callsOf((c) => String(c.method).toLowerCase() === 'delete')
    expect(deletes).toHaveLength(1)
    expect(deletes[0]!.url).toBe('/api/business/orders/screenshots/5')
    expect(callsOf(isGet(`/api/business/orders/${ORDER_NO}/screenshots`))).toHaveLength(2)
  })

  it('删除失败：msg 原样 toast，列表仍重取（列表就是真状态）', async () => {
    loginAs(['order.screenshot_manage'])
    listImpl = () => Promise.resolve({ items: [shotFixture()], total: 1 })
    deleteImpl = () => Promise.reject(new BizError(1002, '该截图不存在或已被删除'))
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.find('[data-testid="shot-del-5"]').trigger('click')
    dialogButton(1).click()
    await flushPromises()
    expect(toast.error).toHaveBeenCalledWith('该截图不存在或已被删除')
    expect(callsOf(isGet(`/api/business/orders/${ORDER_NO}/screenshots`))).toHaveLength(2)
  })

  it('权限 fail-closed：无上传节点 → 上传区不渲染（列表照常）；无管理节点 → 删除角标不渲染', async () => {
    loginAs(['order.view_screenshot'])
    listImpl = () => Promise.resolve({ items: [shotFixture()], total: 1 })
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('[data-testid="shot-upload"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="shot-del-5"]').exists()).toBe(false)
    expect(wrapper.findAll('[data-testid="shot-thumb"]')).toHaveLength(1)

    // 有上传无管理：上传区渲染、删除角标仍不渲染。
    loginAs(['order.upload_screenshot'])
    const wrapper2 = mountPage()
    await flushPromises()
    expect(wrapper2.find('[data-testid="shot-upload"]').exists()).toBe(true)
    expect(wrapper2.find('[data-testid="shot-del-5"]').exists()).toBe(false)
  })

  it('粘贴：window paste 事件携带图片文件 → 走同一条 handleFiles（ticket + PUT + claim）', async () => {
    loginAs(['order.upload_screenshot'])
    mountPage()
    await flushPromises()
    const file = fileOf('贴图.png', 512)
    const paste = Object.assign(new Event('paste'), {
      clipboardData: {
        items: [{ kind: 'file', type: 'image/png', getAsFile: () => file }],
      },
    })
    window.dispatchEvent(paste)
    await flushPromises()
    const tickets = callsOf(isPost(`/api/business/orders/${ORDER_NO}/screenshots/ticket`))
    expect(tickets).toHaveLength(1)
    expect(tickets[0]!.data).toEqual({ ext: 'png', img_type: 0 })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('20 张拍满后计数与返回：顶部计数、‹ 返回触发 router.back', async () => {
    loginAs([])
    listImpl = () =>
      Promise.resolve({
        items: Array.from({ length: 20 }, (_, i) => shotFixture({ id: i + 1 })),
        total: 20,
      })
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('[data-testid="shot-count"]').text()).toBe('20/20')
    await wrapper.find('[data-testid="shot-back"]').trigger('click')
    expect(h.back).toHaveBeenCalledTimes(1)
  })
})
