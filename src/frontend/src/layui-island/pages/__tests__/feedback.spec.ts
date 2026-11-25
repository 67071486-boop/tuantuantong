// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { FeedbackListPayload, FeedbackRow } from '@/api/feedback'

/**
 * 【问题反馈】island 页的契约测试（2026-10-08，`docs/specs/2026-10-08-feedback-design.md` §6.5）。
 *
 * 这一页是**自有功能**（对标物侧栏最底部没有这一条），所以没有证据件可对拍；本文件钉的是
 * **有真实后果**的约定。§6.5 点名的 6 条一条不少：
 *
 * 1. 粘贴一张图 ⇒ **真的**发了 `ticket` 请求、**真的** `PUT` 了 `upload_url`、
 *    待提交区里出现这张图；
 * 2. 粘一段**文字** ⇒ **一个上传请求都不发**（负向判据，防「有 paste 就传」的假实现）；
 * 3. 提交 ⇒ 请求体里 `content` 与 `image_keys` 正确；
 * 4. 超过 3 张 ⇒ 第 4 张被拒**且就地提示**（判据打在**提示文案**上，不是"没崩"）；
 * 5. 非管理员 ⇒ **不**渲染「提交人」列头、**不**渲染处置按钮；
 * 6. 管理员 ⇒ 渲染它们，且点「标记已处理」会发 `PATCH`（状态 + 该行备注框里的值）。
 *
 * ## 手法（照 `open-apps.spec.ts` / `system-perfanalysis.spec.ts`）
 *
 * - `vi.mock('@/layui/island')`：jsdom 里加载不了真 layui，换成可控替身后 `layer.msg` 的
 *   **入参**才能被断言（第 4 条的判据就打在它上面）；
 * - `vi.mock('@/api/http')`：只换**网络出口**——`@/api/feedback`（URL、方法与载荷键的**唯一**
 *   拼装处）跑的是**真实现** ⇒ 「打到哪条 URL、body 里是什么键」这两条判据才有意义
 *   （把 api 模块也换成替身的话，那只是在断言自己传了一个对象）；
 * - ⛔ **不 mock `@/layui-island/directUpload`**：第 1 条要的正是**真的** `PUT`（只把 `fetch`
 *   换成替身），把那一步也换成替身就等于没测直传那一跳。
 *
 * ## 三条 island 陷阱（`AGENTS.md` §5-G）在本文件里的落点
 *
 * ① 类名与选择器分成两个常量：本文件**不写字面选择器**，一律引页面导出的
 *    `PANEL_SELECTOR` / `FORM_SELECTOR` / `ROW_CLASS` 等（与 `buildHtml` 用的是同一份常量）；
 * ② 以 `(` 开头的语句一律前置分号；
 * ③ 先 `await bootstrapIsland(...)` 挂样式、**再**塞标记，`initIslandElements(layui)` 留在
 *    塞标记**之后** —— 第 1 条与最后一条用例按**运行期 DOM** 判（不是读源码）。
 *
 * ⚠️ 夹具一律**合成值**（E-0077）：内容 / 提交人名都是编的，不是任何真租户的数据。
 */
const LAYUI = vi.hoisted(() => {
  const messages: string[] = []
  // ⚠️ 这两个数组是**挂载顺序的运行期判据**（`AGENTS.md` §5-G 陷阱 ③）：
  //    ③a 先挂样式 ⇒ bootstrapIsland 看到的是 `false`；③b 标记先塞 ⇒ initIslandElements 看到 `true`。
  //    ⚠️ 选择器这里写**字面值**（不能引页面模块导出的常量）：本工厂在页面模块求值**之前**
  //    就会跑（它是那条 import 链的一环）⇒ 引常量会读到尚未初始化的绑定。
  const seenByBootstrap: boolean[] = []
  const seenByInit: boolean[] = []
  return {
    messages,
    seenByBootstrap,
    seenByInit,
    form: { render: vi.fn() },
    layer: {
      msg: vi.fn((text: string) => messages.push(text)),
      // 截图点击预览走 `layer.photos`；桩上记调用，判据见「截图预览」那条。
      photos: vi.fn(),
    },
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => {
    LAYUI.seenByBootstrap.push(document.querySelector('#feedbackSubmitBtn') !== null)
    return { form: LAYUI.form, layer: LAYUI.layer }
  }),
  initIslandElements: vi.fn(() => {
    LAYUI.seenByInit.push(document.querySelector('#feedbackSubmitBtn') !== null)
  }),
  islandMsg: (_layer: unknown, text: string) => {
    LAYUI.messages.push(text)
  },
}))

/**
 * 只换**网络出口**（见文件头）。`request()` 返回的是**已解包**的 `data`。
 *
 * ⚠️ **必须连 `CSRF_HEADER_NAME` 一起给**：`@/layui-island/directUpload`（本文件**有意不 mock**
 * 它）会 import 这个常量。少给的话那一跳会抛
 * 「No "CSRF_HEADER_NAME" export is defined on the "@/api/http" mock」——
 * 而**症状**是"图没上去 + 待提交区空着"，看起来像页面坏了，其实只是替身少了一个导出。
 * 值照真实现取（`X-CSRFToken`，见 `api/http.ts`）。
 */
vi.mock('@/api/http', () => ({ request: vi.fn(), CSRF_HEADER_NAME: 'X-CSRFToken' }))
/** 身份：本页第一句就 `fetchMe()`（自检那道门）⇒ 必须可控。 */
vi.mock('@/api/auth', () => ({ fetchMe: vi.fn() }))
/**
 * 直传那一跳要读 `csrftoken` cookie 才带 `X-CSRFToken`（`directUpload.ts` 的同源分支）。
 * jsdom 的 cookie 罐是空的 ⇒ 不接桩的话那条分支**静默不出头**，而"同源必须带 CSRF"恰恰是
 * 那个模块存在的理由之一（它的文件头写着）。⇒ 这里固定给一个**合成**令牌。
 */
vi.mock('@/api/csrf-cookie', () => ({ readCsrfCookie: () => 'test-csrftoken' }))

import { fetchMe } from '@/api/auth'
import { request } from '@/api/http'
import { FEEDBACK_TICKET_URL, FEEDBACK_URL, feedbackDetailUrl } from '@/api/feedback'
import { ROUTE_NODE_CODES } from '@/config/routePermissions'
import page, {
  ACT_HANDLE,
  ACT_REOPEN,
  ACTIONS_ATTR,
  ACTIONS_CLASS,
  ALLOWED_EXTS,
  BTN_HANDLE_CLASS,
  BTN_REOPEN_CLASS,
  CONTENT_TEXTAREA_ID,
  FEEDBACK_NODE,
  FORM_SELECTOR,
  LIST_BODY_ID,
  LIST_HEAD_ID,
  MAX_IMAGES,
  MESSAGES,
  NOTE_INPUT_CLASS,
  NO_ACCESS_MESSAGE,
  PAGE_KEY,
  PANEL_SELECTOR,
  PENDING_BOX_ID,
  ROW_CLASS,
  SHOTS_SELECTOR,
  STATUS_STYLE,
  SUBMIT_BUTTON_ID,
  __testables,
} from '../feedback'

const { buildHtml, listHeadHtml } = __testables

const mockedRequest = vi.mocked(request)
const mockedFetchMe = vi.mocked(fetchMe)

/** 一次 HTTP 请求的入参（我们只关心这三个）。 */
type Cfg = { method?: string; url?: string; data?: unknown }

/** 一次 `fetch`（直传那一跳）的入参。 */
type FetchCall = { url: string; init: { method?: string; headers?: Record<string, string> } }

const UPLOAD_URL = '/api/media/blob/demo/users/7/feedback.test1.png'

/** 身份夹具：**非**管理员（持 `home.feedback` 节点）。 */
const KEFU = {
  id: 7,
  username: 'kefu-fb-a',
  display_name: '客服甲',
  identity_plane: 'tenant_a',
  is_tenant_admin: false,
  roles: [] as string[],
  permissions: [] as string[],
  menu_permissions: [FEEDBACK_NODE],
}

/** 身份夹具：**租户**管理员（`is_tenant_admin`，spec §6.3 指定的那一条判据）。 */
const ADMIN = { ...KEFU, id: 1, username: 'admin-fb-a', is_tenant_admin: true }

/** 一条反馈（键名照 spec §4.2；值全是**合成值**）。 */
function row(over: Partial<FeedbackRow> = {}): FeedbackRow {
  return {
    id: 11,
    content: '测试反馈甲',
    status: 'pending',
    status_label: '未处理',
    admin_note: '',
    created_by_name: '客服甲',
    created_at: '2026-10-08T12:00:00+08:00',
    handled_by_name: null,
    handled_at: null,
    images: [],
    ...over,
  }
}

/** 列表响应（`{items, total}`）。显式类型标注是承重的：契约改键名时它会缺键而 `vue-tsc` 报错。 */
const LIST_ONE: FeedbackListPayload = { items: [row()], total: 1 }
const LIST_TWO: FeedbackListPayload = {
  items: [row(), row({ id: 12, content: '测试反馈乙', created_by_name: '客服乙' })],
  total: 2,
}

function flush(): Promise<void> {
  // 取数 / 上传都是多跳的（点击 → request → 铺DOM；ticket → fetch → 铺DOM）：连放几轮宏任务。
  return (async () => {
    for (let i = 0; i < 6; i += 1) await new Promise((resolve) => setTimeout(resolve, 0))
  })()
}

/** 按 `METHOD URL` 造一次响应路由；**没有备好的响应就抛**（⛔ 不是静默回 undefined）。 */
function respond(routes: Record<string, unknown>): void {
  mockedRequest.mockImplementation((config: Cfg) => {
    const key = `${(config.method ?? 'get').toUpperCase()} ${config.url ?? ''}`
    if (!(key in routes)) throw new Error(`用例没有备 ${key} 的响应`)
    const value = routes[key]
    // ⚠️ 失败那一支给的是**函数**：`Promise.reject(...)` 写在字面量里会**在装配期就建出一个
    //    未处理的 rejection**（Node 会报 `Unhandled Rejection`，且它可能把**别的**用例带红）。
    if (typeof value === 'function') return (value as () => Promise<never>)()
    return Promise.resolve(value)
  })
}

/** 全部请求（`METHOD URL`）。 */
function requests(): string[] {
  return mockedRequest.mock.calls.map((c) => {
    const cfg = c[0] as Cfg
    return `${(cfg.method ?? 'get').toUpperCase()} ${cfg.url}`
  })
}

/** 直传那一跳的 `fetch` 调用。 */
function fetchCalls(): FetchCall[] {
  return fetchMock.mock.calls.map((c) => ({ url: String(c[0]), init: c[1] as FetchCall['init'] }))
}

let fetchMock: ReturnType<typeof vi.fn>
let root: HTMLElement

/**
 * jsdom **没有**实现 `URL.createObjectURL`（浏览器有）⇒ 由测试补上。
 *
 * ⚠️ 不补的话 `previewUrlOf` 走它自己的降级分支（回空串）⇒ `pendingHtml` **不渲染 `<img>`**，
 * 于是「待提交区里出现这张缩略图」这类判据会**恒假**（实测踩到：4 条用例一起红，
 * 而其中 3 条其实功能是好的——症状看起来像"图根本没进去"）。照先例
 * `order-screenshots-upload.spec.ts` 同一做法。
 */
const createObjectURL = vi.fn(() => 'blob:http://localhost/pending.png')
const ORIGINAL_CREATE_OBJECT_URL = URL.createObjectURL

/**
 * 造一个带**时间戳**的合成文件。
 *
 * ⚠️ jsdom 的 `File` 构造器**不支持 `lastModified` 之外的 date 语义**——扩展名靠 `name`、
 * 体积靠 `new File([blob])` 的字节数（第 4 条要造一张 >10 MB 的必须真给字节，
 * 否则 `file.size` 是 0，那条用例会恒真）。
 */
function fakeFile(name: string, type: string, bytes = 8): File {
  return new File([new Uint8Array(bytes)], name, { type })
}

/** 造一张恰好**超限 1 字节**的文件（判据是 `>`，所以 `limit + 1` 才是"超了"）。 */
function oversizedFile(): File {
  return fakeFile('too-big.png', 'image/png', 10 * 1024 * 1024 + 1)
}

/**
 * 在某容器上派发一次 `paste`。
 *
 * ⚠️ 夹具形状照先例 `order-screenshots-upload.spec.ts`：`clipboardData` 挂在事件对象上
 * （jsdom 的 `ClipboardEvent` 不能直接带 `items`）。
 * ⚠️ `bubbles: true` 是**承重**的：本页的监听挂在**提交区容器**上，事件要从内部元素冒上来
 * （第 2 条把事件打在内层 `textarea` 上，正是为了钉「不是只挂 textarea」这件事）。
 */
function paste(target: HTMLElement, items: Array<Record<string, unknown>>): void {
  const event = new Event('paste', { bubbles: true, cancelable: true })
  Object.assign(event, { clipboardData: { items } })
  target.dispatchEvent(event)
}

const IMAGE_ITEM = (file: File) => ({
  kind: 'file',
  type: file.type,
  getAsFile: () => file,
})
const TEXT_ITEM = { kind: 'string', type: 'text/plain', getAsFile: () => null }

/** 待提交区里那几格的名字（`pendingHtml` 把文件名渲染成文本）。 */
function pendingText(): string {
  return root.querySelector<HTMLElement>(`#${PENDING_BOX_ID}`)?.textContent ?? ''
}

/** 列表表头那几列的字面文本。 */
function headCells(): string[] {
  const head = root.querySelector<HTMLElement>(`#${LIST_HEAD_ID}`)
  if (!head) throw new Error('页面上没有列表表头容器')
  return Array.from(head.querySelectorAll('th')).map((th) => th.textContent ?? '')
}

/** 列表行（按 `ROW_CLASS`）。 */
function rows(): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(`.${ROW_CLASS}`))
}

function click(el: HTMLElement): void {
  el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
}

beforeEach(() => {
  vi.clearAllMocks()
  LAYUI.messages.length = 0
  LAYUI.seenByBootstrap.length = 0
  LAYUI.seenByInit.length = 0
  document.body.innerHTML = ''
  root = document.createElement('div')
  document.body.appendChild(root)

  mockedFetchMe.mockResolvedValue(KEFU)
  respond({ [`GET ${FEEDBACK_URL}`]: LIST_ONE })
  fetchMock = vi.fn(async () => ({ ok: true, status: 200 }) as unknown as Response)
  vi.stubGlobal('fetch', fetchMock)
  createObjectURL.mockClear()
  Object.defineProperty(URL, 'createObjectURL', {
    configurable: true,
    writable: true,
    value: createObjectURL,
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  Object.defineProperty(URL, 'createObjectURL', {
    configurable: true,
    writable: true,
    value: ORIGINAL_CREATE_OBJECT_URL,
  })
  document.body.innerHTML = ''
})

/** 挂页面（`await` 完首屏那次列表请求）。 */
async function mount(): Promise<void> {
  await page.mount(root, { onNavigate: () => {} })
  await flush()
}

/**
 * ⭐ **可控的直传那一跳**（F1 的排队用例要它）：把 `fetch` 换成一个**不会自己落地**的 promise，
 * 由用例决定**什么时候**放行——「上一批还在传」这个时刻只有这样才定得住
 * （否则 `beforeEach` 那个替身会立刻成功，队列永远是空的，判据恒真）。
 *
 * ⚠️ 必须**在 `mount()` 之前**调用（替身是挂载期就用的；不过本页首屏只取列表，
 * 上传那一路要等用户操作，所以挂载后再换也来得及——本函数仍按"先换再挂"的写法用）。
 */
function deferredFetch(): { resolveAll: () => void } {
  const waiters: Array<() => void> = []
  fetchMock.mockImplementation(
    () =>
      new Promise((resolve) => {
        waiters.push(() => resolve({ ok: true, status: 200 } as unknown as Response))
      }),
  )
  return {
    resolveAll: () => {
      for (const done of waiters.splice(0)) done()
    },
  }
}

describe('挂载：三条 island 陷阱与首屏', () => {
  it('先挂 bootstrapIsland、再塞标记、最后 initIslandElements（按运行期 DOM 判，不是读源码）', async () => {
    await mount()
    // ③a：`bootstrapIsland` 被调用时标记**还不在**（先挂样式，防 FOUC）。
    expect(LAYUI.seenByBootstrap).toEqual([false])
    // ③b：`initIslandElements` 被调用时标记**已经在**（它要按 `lay-*` 初始化）。
    expect(LAYUI.seenByInit).toEqual([true])
    // 标记真的塞进去了（不是「什么都没渲染也算过」）。
    expect(root.querySelector(PANEL_SELECTOR)).not.toBeNull()
    expect(root.querySelector(FORM_SELECTOR)).not.toBeNull()
    expect(root.querySelector(`#${CONTENT_TEXTAREA_ID}`)).not.toBeNull()
  });

  it('① 类名与选择器是两个常量：`class="…"` 里放的是**类名**、选择器另取（`class=".x"` 会永远匹配不到）', () => {
    // 判据打在**真标记**上：`PANEL_SELECTOR` 能选中 `PANEL_CLASS` 那个节点，
    // 且标记里**没有** `class=".feedbackPanel"` 这种带点的写法。
    const host = document.createElement('div')
    host.innerHTML = buildHtml()
    expect(host.querySelector(PANEL_SELECTOR)).not.toBeNull()
    expect(host.innerHTML).not.toContain('class=".')
  })

  it('本页声明的节点 code 与 `config/routePermissions.ts` 的登记**同值**（⛔ 不靠注释自证）', () => {
    // iframe 里读不到 Vue 路由的 `meta`，所以页面自检用的是本文件那个字面量；
    // 两处漂移的症状是「路由放你进来、页面自己把你挡出去」，且**没有任何网会报**。
    expect(FEEDBACK_NODE).toBe('home.feedback')
    expect(ROUTE_NODE_CODES[PAGE_KEY]).toBe(FEEDBACK_NODE)
  })

  it('没有节点时**不注入标记**（深链自检；与路由那道门同源，⛔ 不是第二套判据）', async () => {
    mockedFetchMe.mockResolvedValue({ ...KEFU, menu_permissions: [] })
    await page.mount(root, { onNavigate: () => {} })
    await flush()
    expect(LAYUI.messages).toContain(NO_ACCESS_MESSAGE)
    // ⛔ 注入必须排在门禁之后：先注入就会让被拒的深链看到整个页面壳。
    expect(root.querySelector(PANEL_SELECTOR)).toBeNull()
  })
})

describe('提交区：粘贴、选文件、限流、提交（§6.5 第 1–4 条）', () => {
  it('第 1 条：粘贴一张图 ⇒ 真发 ticket、真 PUT `upload_url`、待提交区出现这张图', async () => {
    await mount()
    respond({
      [`GET ${FEEDBACK_URL}`]: LIST_ONE,
      [`POST ${FEEDBACK_TICKET_URL}`]: {
        key: 'demo/users/7/feedback.test1.png',
        upload_url: UPLOAD_URL,
        expires_in: 600,
      },
    })
    const file = fakeFile('shot-1.png', 'image/png')
    // ⚠️ 打在**提交区容器**上（本页的监听挂在那里，不是只挂 textarea）。
    paste(root.querySelector(FORM_SELECTOR)!, [IMAGE_ITEM(file)])
    await flush()

    // ① 签发请求：URL 与体里的 `ext`（**去点小写**）都对。
    const ticket = mockedRequest.mock.calls
      .map((c) => c[0] as Cfg)
      .find((c) => c.url === FEEDBACK_TICKET_URL)
    expect(ticket, '应当发过一条 ticket 请求').toBeDefined()
    expect(ticket!.method).toBe('POST')
    expect(ticket!.data).toEqual({ ext: 'png' })

    // ② 直传那一跳：**真的** PUT 到服务端给的 `upload_url`，且带同源必需的 CSRF 头。
    const put = fetchCalls().find((c) => c.url === UPLOAD_URL)
    expect(put, '应当对 upload_url 发过一次 fetch').toBeDefined()
    expect(put!.init.method).toBe('PUT')
    expect(put!.init.headers?.['X-CSRFToken']).toBe('test-csrftoken')

    // ③ 待提交区里出现这张图（带文件名，便于用户核对自己选了哪张）。
    expect(pendingText()).toContain('shot-1.png')
    expect(root.querySelectorAll(`#${PENDING_BOX_ID} img`).length).toBe(1)
  })

  it('第 2 条（负向）：粘一段**文字** ⇒ 一个上传请求都不发', async () => {
    await mount()
    const before = mockedRequest.mock.calls.length
    const beforeFetch = fetchMock.mock.calls.length
    // ⚠️ 打在**内层 textarea** 上：事件要冒泡到提交区容器上才会被认领
    //    —— 这条同时钉住「监听不是只挂在 textarea 上、也不是没挂」。
    paste(root.querySelector<HTMLElement>(`#${CONTENT_TEXTAREA_ID}`)!, [TEXT_ITEM])
    await flush()

    expect(requests().slice(before)).toEqual([])
    expect(fetchMock.mock.calls.length).toBe(beforeFetch)
    // 也**不许**弹「粘贴失败」那类噪音（用户粘文字是很正常的操作）。
    expect(LAYUI.messages).toEqual([])
    expect(pendingText()).toBe('')
  })

  it('第 2 条（补强）：`kind` 是 string 但 `type` 是 image/* —— 仍不算图片项（两条判据都要）', async () => {
    await mount()
    const before = mockedRequest.mock.calls.length
    paste(root.querySelector(FORM_SELECTOR)!, [
      { kind: 'string', type: 'image/png', getAsFile: () => fakeFile('x.png', 'image/png') },
    ])
    await flush()
    expect(requests().slice(before)).toEqual([])
  })

  it('点按钮选文件走**同一条**实现（`change` 事件 ⇒ 同样的 ticket + PUT）', async () => {
    await mount()
    respond({
      [`GET ${FEEDBACK_URL}`]: LIST_ONE,
      [`POST ${FEEDBACK_TICKET_URL}`]: {
        key: 'demo/users/7/feedback.test2.jpg',
        upload_url: '/api/media/blob/demo/users/7/feedback.test2.jpg',
        expires_in: 600,
      },
    })
    const input = root.querySelector<HTMLInputElement>('#feedbackFileInput')!
    Object.defineProperty(input, 'files', {
      configurable: true,
      value: [fakeFile('picked.jpg', 'image/jpeg')],
    })
    input.dispatchEvent(new Event('change', { bubbles: true }))
    await flush()

    const ticket = mockedRequest.mock.calls
      .map((c) => c[0] as Cfg)
      .find((c) => c.url === FEEDBACK_TICKET_URL)
    expect(ticket!.data).toEqual({ ext: 'jpg' })
    expect(pendingText()).toContain('picked.jpg')
  })

  it('第 4 条：贴到第 4 张 ⇒ 第 4 张**被拒**且**就地提示**是哪一条（⛔ 不是静默丢）', async () => {
    await mount()
    let n = 0
    mockedRequest.mockImplementation((config: Cfg) => {
      if (config.url === FEEDBACK_TICKET_URL) {
        n += 1
        return Promise.resolve({
          key: `demo/users/7/feedback.test${n}.png`,
          upload_url: `/api/media/blob/demo/users/7/feedback.test${n}.png`,
          expires_in: 600,
        })
      }
      return Promise.resolve(LIST_ONE)
    })

    const box = root.querySelector<HTMLElement>(FORM_SELECTOR)!
    for (let i = 1; i <= MAX_IMAGES; i += 1) {
      paste(box, [IMAGE_ITEM(fakeFile(`shot-${i}.png`, 'image/png'))])
      await flush()
    }
    // 前 3 张都进去了（否则"第 4 张被拒"可能是"一张都没进去"而恒真）。
    expect(root.querySelectorAll(`#${PENDING_BOX_ID} img`).length).toBe(MAX_IMAGES)
    expect(LAYUI.messages).toEqual([])

    const patchesBefore = n
    paste(box, [IMAGE_ITEM(fakeFile('shot-4.png', 'image/png'))])
    await flush()

    // 判据打在**提示文案**上（spec §6.5 第 4 条的原话：「判据打在提示上，不是"没崩"」）。
    expect(LAYUI.messages).toContain(MESSAGES.tooManyImages)
    // 而且它**真的没上传**：签发请求数没涨、待提交区没变。
    expect(n).toBe(patchesBefore)
    expect(pendingText()).not.toContain('shot-4.png')
  })

  it('单张超 10 MB ⇒ 就地提示体积那一条（判据是超限文案，且**没有**发签发请求）', async () => {
    await mount()
    const before = mockedRequest.mock.calls.length
    paste(root.querySelector(FORM_SELECTOR)!, [IMAGE_ITEM(oversizedFile())])
    await flush()
    expect(LAYUI.messages).toContain(MESSAGES.imageTooLarge)
    expect(requests().slice(before)).toEqual([])
  })

  it('类型不在白名单 ⇒ 就地提示（白名单文案里带出到底收哪几种）', async () => {
    await mount()
    const before = mockedRequest.mock.calls.length
    paste(root.querySelector(FORM_SELECTOR)!, [IMAGE_ITEM(fakeFile('note.gif', 'image/gif'))])
    await flush()
    expect(LAYUI.messages).toContain(MESSAGES.imageBadType)
    expect(requests().slice(before)).toEqual([])
    // 白名单本身也要对：`jpg` 与 `jpeg` **都**收（写成只收 `jpeg` 会让 iPhone 的 `.jpg` 全被拒）。
    expect([...ALLOWED_EXTS]).toEqual(['png', 'jpg', 'jpeg'])
  })

  it('超出 3 张的那几张**拒掉**，合规的照常上传（⛔ 不是"一张不合规整批丢"）', async () => {
    await mount()
    respond({
      [`GET ${FEEDBACK_URL}`]: LIST_ONE,
      [`POST ${FEEDBACK_TICKET_URL}`]: {
        key: 'demo/users/7/feedback.test9.png',
        upload_url: '/api/media/blob/demo/users/7/feedback.test9.png',
        expires_in: 600,
      },
    })
    paste(root.querySelector(FORM_SELECTOR)!, [
      IMAGE_ITEM(fakeFile('a.png', 'image/png')),
      IMAGE_ITEM(fakeFile('b.gif', 'image/gif')),
      IMAGE_ITEM(oversizedFile()),
      IMAGE_ITEM(fakeFile('c.png', 'image/png')),
    ])
    await flush()
    expect(LAYUI.messages).toEqual([
      MESSAGES.imageBadType,
      MESSAGES.imageTooLarge,
    ])
    // 两张合规的（a / c）都上去了。
    expect(pendingText()).toContain('a.png')
    expect(pendingText()).toContain('c.png')
    expect(root.querySelectorAll(`#${PENDING_BOX_ID} img`).length).toBe(2)
  })

  it('第 3 条：提交 ⇒ 请求体里 `content` 与 `image_keys` 正确；成功后**清空**表单与截图并刷新列表', async () => {
    await mount()
    respond({
      [`GET ${FEEDBACK_URL}`]: LIST_ONE,
      [`POST ${FEEDBACK_TICKET_URL}`]: {
        key: 'demo/users/7/feedback.test1.png',
        upload_url: UPLOAD_URL,
        expires_in: 600,
      },
      [`POST ${FEEDBACK_URL}`]: { id: 7 },
    })
    const box = root.querySelector<HTMLElement>(FORM_SELECTOR)!
    paste(box, [IMAGE_ITEM(fakeFile('shot-1.png', 'image/png'))])
    await flush()
    const textarea = root.querySelector<HTMLTextAreaElement>(`#${CONTENT_TEXTAREA_ID}`)!
    textarea.value = '  测试反馈甲  '

    const listCallsBefore = requests().filter((r) => r === `GET ${FEEDBACK_URL}`).length
    click(root.querySelector<HTMLElement>(`#${SUBMIT_BUTTON_ID}`)!)
    await flush()

    const created = mockedRequest.mock.calls
      .map((c) => c[0] as Cfg)
      .find((c) => c.method === 'POST' && c.url === FEEDBACK_URL)
    expect(created, '应当发过一条提交请求').toBeDefined()
    // ⚠️ `content` **去空白后**提交（后端也会去，但前端别把两头的空格当内容送上去）。
    expect(created!.data).toEqual({
      content: '测试反馈甲',
      image_keys: ['demo/users/7/feedback.test1.png'],
    })
    // 成功后：表单与截图都清空、提示说了、列表**真的**又取了一次。
    expect(textarea.value).toBe('')
    expect(pendingText()).toBe('')
    expect(LAYUI.messages).toContain(MESSAGES.submitted)
    expect(requests().filter((r) => r === `GET ${FEEDBACK_URL}`).length).toBe(listCallsBefore + 1)
  })

  it('内容空着就点提交 ⇒ 提示 + **不发提交请求**（也不清空已选的图）', async () => {
    await mount()
    const before = requests().length
    click(root.querySelector<HTMLElement>(`#${SUBMIT_BUTTON_ID}`)!)
    await flush()
    expect(LAYUI.messages).toContain(MESSAGES.noContent)
    expect(requests().slice(before)).toEqual([])
  })

  it('提交失败 ⇒ 提示后端文案、**表单与截图都留着**（清掉是最气人的那种失败）', async () => {
    await mount()
    respond({
      [`GET ${FEEDBACK_URL}`]: LIST_ONE,
      [`POST ${FEEDBACK_TICKET_URL}`]: {
        key: 'demo/users/7/feedback.test1.png',
        upload_url: UPLOAD_URL,
        expires_in: 600,
      },
      [`POST ${FEEDBACK_URL}`]: () => Promise.reject(new Error('反馈内容不能为空。')),
    })
    const box = root.querySelector<HTMLElement>(FORM_SELECTOR)!
    paste(box, [IMAGE_ITEM(fakeFile('shot-1.png', 'image/png'))])
    await flush()
    const textarea = root.querySelector<HTMLTextAreaElement>(`#${CONTENT_TEXTAREA_ID}`)!
    textarea.value = '测试反馈乙'
    click(root.querySelector<HTMLElement>(`#${SUBMIT_BUTTON_ID}`)!)
    await flush()
    expect(LAYUI.messages).toContain('反馈内容不能为空。')
    expect(textarea.value).toBe('测试反馈乙')
    expect(pendingText()).toContain('shot-1.png')
  })
})

describe('⭐ F1（2026-10-08 红队）：上一批还在传时再粘贴 ⇒ 第二批排队，⛔ 不被静默丢弃', () => {
  /**
   * 这一组的**缺陷现场**：原实现是 `if (uploading) return` ⇒ 第一批的 PUT 还没落地时
   * 用户再按一次 Ctrl+V，第二批**一个提示都没有地丢掉**（用户以为粘上了、提交时才发现少一张）。
   * 它踩的正是业务方最在意的那条路径（原话「一般截图工具都是截图后 Ctrl v 的」），
   * 也直接违反 spec §6.3 的「⛔ 不要静默丢弃」。
   *
   * ⚠️ 判据**必须钉在"第二批那一次请求真的发出去了"**上（`issueTicket` 被调了 2 次、
   * 待提交列表最终 2 张），⛔ 不是「没报错」——静默丢弃恰恰就是"没报错"。
   */
  it('第一批的 PUT 还没落地时再粘一张 ⇒ 第二批**最终也传上去**（签发请求共 2 次、待提交 2 张）', async () => {
    const put = deferredFetch()
    await mount()
    let n = 0
    mockedRequest.mockImplementation((config: Cfg) => {
      if (config.url === FEEDBACK_TICKET_URL) {
        n += 1
        return Promise.resolve({
          key: `demo/users/7/feedback.q${n}.png`,
          upload_url: `/api/media/blob/demo/users/7/feedback.q${n}.png`,
          expires_in: 600,
        })
      }
      return Promise.resolve(LIST_ONE)
    })

    const box = root.querySelector<HTMLElement>(FORM_SELECTOR)!
    paste(box, [IMAGE_ITEM(fakeFile('batch-1.png', 'image/png'))])
    await flush()
    // 断言"确实卡在途中"（否则下面那条判据可能是"两批都早就完成了"而失去意义）。
    expect(n, '第一批应当已经走到签发').toBe(1)
    expect(fetchMock.mock.calls.length, '第一批的 PUT 应当还没落地').toBe(1)

    // ⭐ 就在这一刻再粘一张（缺陷现场）。
    paste(box, [IMAGE_ITEM(fakeFile('batch-2.png', 'image/png'))])
    await flush()
    // 排队的批次要等前一批落地才轮到它 ⇒ 此时**还没**发出第二次签发。
    expect(n, '排队生效：前一批没落地前不该发第二次签发').toBe(1)

    // ⚠️ 一张图的完整链是「签发 → PUT → 挂进列表」三跳，而两个批次各有一张 ⇒
    //    放行-冲刷要跑够轮数（少了会让判据打在"还没轮到"的中间态上）。
    for (let i = 0; i < 4; i += 1) {
      put.resolveAll()
      await flush()
    }
    // ⛔ 核心判据：第二批**真的发出去了**（不是被丢掉）。
    expect(n, '第二批的签发请求必须真的发出去').toBe(2)
    const issuedKeys = mockedRequest.mock.calls
      .map((c) => c[0] as Cfg)
      .filter((c) => c.url === FEEDBACK_TICKET_URL)
      .map((c) => (c.data as { ext: string }).ext)
    expect(issuedKeys).toEqual(['png', 'png'])
    expect(pendingText()).toContain('batch-1.png')
    expect(pendingText()).toContain('batch-2.png')
    expect(root.querySelectorAll(`#${PENDING_BOX_ID} img`).length).toBe(2)
    // 也**不许**弹任何限流/失败提示（这一批完全合规）。
    expect(LAYUI.messages).toEqual([])
  })

  it('排队之后张数判据在**处理那一刻**求值：前一批占满 3 张 ⇒ 后一批被拒并**就地提示**（不是静默丢）', async () => {
    // ⚠️ 这条钉的是「⛔ 别在入队时就把 `rejectReason` 判死」：入队那一刻 `pending` 还是 0
    //    （前一批还没上传完），到处理那一刻才变成 3 ⇒ 必须**那时**才拒。
    const put = deferredFetch()
    await mount()
    let n = 0
    mockedRequest.mockImplementation((config: Cfg) => {
      if (config.url === FEEDBACK_TICKET_URL) {
        n += 1
        return Promise.resolve({
          key: `demo/users/7/feedback.q${n}.png`,
          upload_url: `/api/media/blob/demo/users/7/feedback.q${n}.png`,
          expires_in: 600,
        })
      }
      return Promise.resolve(LIST_ONE)
    })
    const box = root.querySelector<HTMLElement>(FORM_SELECTOR)!
    // 第一批：一次给 3 张（都在途中）。
    paste(box, [
      IMAGE_ITEM(fakeFile('batch-1a.png', 'image/png')),
      IMAGE_ITEM(fakeFile('batch-1b.png', 'image/png')),
      IMAGE_ITEM(fakeFile('batch-1c.png', 'image/png')),
    ])
    await flush()
    expect(n, '第一批只发出了第一张的签发（串行）').toBe(1)

    // 第二批：此刻入队（pending 还是 0，所以入队时判"没满"是对的）。
    paste(box, [IMAGE_ITEM(fakeFile('batch-2.png', 'image/png'))])
    await flush()

    // 放行全部：第一批 3 张都要落地（每落地一张，才轮到下一张）。
    for (let i = 0; i < 4; i += 1) {
      put.resolveAll()
      await flush()
    }
    expect(n, '第一批的 3 张都发出去了').toBe(3)
    // 第二批在**处理那一刻**看到 pending 已经是 3 ⇒ 拒掉它，并**就地**说出是哪一条。
    expect(LAYUI.messages).toContain(MESSAGES.tooManyImages)
    expect(pendingText()).not.toContain('batch-2.png')
    expect(root.querySelectorAll(`#${PENDING_BOX_ID} img`).length).toBe(MAX_IMAGES)
  })

  it('一次上传失败**不会**把队列卡死（后续批次照样传得上去）', async () => {
    // ⚠️ 链条上留一个未收口的 rejection，会让**之后每一次**粘贴都直接跳到 catch、
    //    静默不传——那正是本组要消灭的缺陷形状。这里钉住"失败之后还能再传"。
    await mount()
    let call = 0
    mockedRequest.mockImplementation((config: Cfg) => {
      if (config.url === FEEDBACK_TICKET_URL) {
        call += 1
        if (call === 1) return Promise.reject(new Error('图片无效或超过大小限制，请重新选择后重试'))
        return Promise.resolve({
          key: 'demo/users/7/feedback.after.png',
          upload_url: '/api/media/blob/demo/users/7/feedback.after.png',
          expires_in: 600,
        })
      }
      return Promise.resolve(LIST_ONE)
    })
    const box = root.querySelector<HTMLElement>(FORM_SELECTOR)!
    paste(box, [IMAGE_ITEM(fakeFile('will-fail.png', 'image/png'))])
    await flush()
    expect(LAYUI.messages).toContain('图片无效或超过大小限制，请重新选择后重试')

    paste(box, [IMAGE_ITEM(fakeFile('will-succeed.png', 'image/png'))])
    await flush()
    expect(call, '失败之后的第二次签发必须真的发出去').toBe(2)
    expect(pendingText()).toContain('will-succeed.png')
  })
})

describe('⭐ F2（2026-10-08 红队）：粘贴的图**没有文件名**时按 `file.type` 兜底，⛔ 不发出后端必拒的 ext', () => {
  /**
   * 缺陷现场（红队实测读数）：浏览器剪贴板在部分环境（远程桌面 / Linux）给出的
   * `File.name` 是**空的**，也可能是 `image` / `blob` 这种没有点的名字；那时只看文件名 ⇒
   * 前端要么发出一个**空的 ext**、要么把整段名字当 ext，最后用户看到的是后端那句
   * **归因错误**的「图片格式不正确」/「只支持 png / jpg」——而他粘的**明明是一张 PNG**。
   *
   * ⚠️ 下面两条**必须**断言 `issueTicket` 收到的 ext 是 `'png'`
   * （⛔ 不是「把整个文件名当 ext」、⛔ 不是空串）。
   */
  it.each([
    ['name = ""（空名）', ''],
    ['name = "image"（没有点）', 'image'],
    ['name = "截图"（中文名、没有点）', '截图'],
    ['name = "blob"', 'blob'],
    ['name = "屏幕截图 2026-10-08 120000"（超 8 字符的无点名）', '屏幕截图 2026-10-08 120000'],
  ])('%s + type=image/png ⇒ 签发请求的 ext 是 `png`', async (_label, name) => {
    await mount()
    respond({
      [`GET ${FEEDBACK_URL}`]: LIST_ONE,
      [`POST ${FEEDBACK_TICKET_URL}`]: {
        key: 'demo/users/7/feedback.noname.png',
        upload_url: '/api/media/blob/demo/users/7/feedback.noname.png',
        expires_in: 600,
      },
    })
    paste(root.querySelector<HTMLElement>(FORM_SELECTOR)!, [
      IMAGE_ITEM(fakeFile(name, 'image/png')),
    ])
    await flush()

    const ticket = mockedRequest.mock.calls
      .map((c) => c[0] as Cfg)
      .find((c) => c.url === FEEDBACK_TICKET_URL)
    expect(ticket, '应当发出过一条签发请求').toBeDefined()
    expect(ticket!.data).toEqual({ ext: 'png' })
    // 也**没有**弹格式不对那类错归因的提示。
    expect(LAYUI.messages).toEqual([])
  })

  it('type=image/jpeg 且没有点名 ⇒ ext 是 `jpeg`（与 `ALLOWED_EXTS` 里的写法对齐，不是 `jpg`）', async () => {
    await mount()
    respond({
      [`GET ${FEEDBACK_URL}`]: LIST_ONE,
      [`POST ${FEEDBACK_TICKET_URL}`]: {
        key: 'demo/users/7/feedback.noname.jpeg',
        upload_url: '/api/media/blob/demo/users/7/feedback.noname.jpeg',
        expires_in: 600,
      },
    })
    paste(root.querySelector<HTMLElement>(FORM_SELECTOR)!, [IMAGE_ITEM(fakeFile('', 'image/jpeg'))])
    await flush()
    const ticket = mockedRequest.mock.calls
      .map((c) => c[0] as Cfg)
      .find((c) => c.url === FEEDBACK_TICKET_URL)
    expect(ticket!.data).toEqual({ ext: 'jpeg' })
  })

  it('名字里的 ext 合法时**以名字为准**（type 可疑也不改判）', async () => {
    // ⚠️ 反向判据：不许因为"type 更权威"就把名字里写明的 `png` 改成别的。
    await mount()
    respond({
      [`GET ${FEEDBACK_URL}`]: LIST_ONE,
      [`POST ${FEEDBACK_TICKET_URL}`]: {
        key: 'demo/users/7/feedback.named.png',
        upload_url: '/api/media/blob/demo/users/7/feedback.named.png',
        expires_in: 600,
      },
    })
    paste(root.querySelector<HTMLElement>(FORM_SELECTOR)!, [
      IMAGE_ITEM(fakeFile('shot.PNG', 'image/jpeg')),
    ])
    await flush()
    const ticket = mockedRequest.mock.calls
      .map((c) => c[0] as Cfg)
      .find((c) => c.url === FEEDBACK_TICKET_URL)
    expect(ticket!.data).toEqual({ ext: 'png' })
  })

  it('名字与 type **都**推不出合法 ext ⇒ 就地提示，且⛔ 不发任何签发请求', async () => {
    // ⛔ 这里就是那条纪律：「要么推出合法 ext，要么**就地**说清是哪种图不支持，
    //    别让后端去报一句错归因的话」。
    await mount()
    const before = requests().length
    paste(root.querySelector<HTMLElement>(FORM_SELECTOR)!, [
      IMAGE_ITEM(fakeFile('', 'image/webp')),
    ])
    await flush()
    expect(LAYUI.messages).toContain(MESSAGES.imageBadType)
    expect(requests().slice(before)).toEqual([])
  })
})

describe('列表区：自己的 vs 全部（§6.5 第 5–6 条）', () => {
  it('第 5 条：非管理员 ⇒ **不**渲染「提交人」列头、**不**渲染处置按钮与备注框', async () => {
    await mount()
    expect(headCells()).toEqual(['提交时间', '反馈内容', '状态', '管理员备注', '截图'])
    expect(headCells()).not.toContain('提交人')
    expect(headCells()).not.toContain('处理')
    // 行里的"提交人"那半：连**文字**都不出现（`created_by_name` 是服务端照样回的，
    // 所以"不显示"必须是页面这一侧的选择，而不是"服务端没给"）。
    expect(rows().length).toBe(1)
    expect(rows()[0]!.textContent).toContain('测试反馈甲')
    expect(rows()[0]!.textContent).not.toContain('客服甲')
    expect(root.querySelectorAll(`[${ACTIONS_ATTR}]`).length).toBe(0)
    expect(root.querySelectorAll(`.${NOTE_INPUT_CLASS}`).length).toBe(0)
    expect(root.querySelectorAll(`.${ACTIONS_CLASS}`).length).toBe(0)
  })

  it('列表外观（2026-10-08 业务方反馈）：状态是**色标**、行有**状态色条**、按钮**分主次**', async () => {
    mockedFetchMe.mockResolvedValue(ADMIN)
    // ⚠️ **自己造一对**「一条未处理 + 一条已处理」：既有夹具 `LIST_TWO` 两行都是 `pending`
    //    （`row()` 的默认值），拿它测「两种状态长得不一样」会测到同一个东西。
    respond({
      [`GET ${FEEDBACK_URL}`]: {
        items: [
          row(),
          row({
            id: 12,
            content: '测试反馈乙',
            created_by_name: '客服乙',
            status: 'handled',
            status_label: '已处理',
          }),
        ],
        total: 2,
      },
    })
    await mount()

    const [pendingRow, handledRow] = rows() as [HTMLElement, HTMLElement]
    // ① 状态列是**色标**（不是一行白底小字），两种状态用**不同的**类。
    const badgeOf = (tr: HTMLElement) => tr.querySelector<HTMLElement>('td span.layui-badge')!
    expect(badgeOf(pendingRow).className).toBe(STATUS_STYLE.pending.badge)
    expect(badgeOf(handledRow).className).toBe(STATUS_STYLE.handled.badge)
    expect(badgeOf(pendingRow).textContent).toBe('未处理')
    expect(badgeOf(handledRow).textContent).toBe('已处理')
    // ② 第一格带**状态色条**（与色标同色）⇒ 不用先看状态列也能扫出哪几条没处理。
    expect(pendingRow.querySelector('td')!.getAttribute('style')).toContain(
      `border-left:3px solid ${STATUS_STYLE.pending.color}`,
    )
    expect(handledRow.querySelector('td')!.getAttribute('style')).toContain(
      `border-left:3px solid ${STATUS_STYLE.handled.color}`,
    )
    // ③ 按钮**分主次**：主操作有图标、用正常尺寸（⛔ 不是最小的 `-xs`）；回退操作是素色。
    const handleBtn = pendingRow.querySelector<HTMLElement>('[data-feedback-act]')!
    const reopenBtn = handledRow.querySelector<HTMLElement>('[data-feedback-act]')!
    expect(handleBtn.className).toBe(BTN_HANDLE_CLASS)
    expect(reopenBtn.className).toBe(BTN_REOPEN_CLASS)
    expect(handleBtn.className).not.toContain('layui-btn-xs')
    expect(handleBtn.querySelector('.layui-icon'), '主操作按钮应有图标').not.toBeNull()
    expect(reopenBtn.querySelector('.layui-icon'), '回退按钮应有图标').not.toBeNull()
  })

  it('状态色标的色值与 vendored `layui.css` **对拍**（⛔ 不许凭记忆写色值）', () => {
    // 判据打在**依赖自己的 CSS** 上：`STATUS_STYLE` 里那个色条色值必须等于它所引用的
    // `layui-bg-*` 类在该文件里的真实 `background-color`。漂了就红——而漂了的症状
    // 只是「色条与色标不同色」，人眼未必看得出来。
    // ⚠️ 本文件是 **jsdom** 环境 ⇒ `import.meta.url` 不是 `file:`（`fileURLToPath` 会抛
    //    「The URL must be of scheme file」）。vitest 的 cwd = 前端工程根 ⇒ 用 `process.cwd()` 定位。
    const css = readFileSync(resolve(process.cwd(), 'public/layui/css/layui.css'), 'utf8')
    for (const [state, style] of Object.entries(STATUS_STYLE)) {
      const cls = style.badge.split(' ').find((c) => c.startsWith('layui-bg-'))!
      const rule = new RegExp(`\\.${cls}\\{background-color:(#[0-9a-f]{6})`).exec(css)
      expect(rule, `${cls} 在 layui.css 里找不到`).not.toBeNull()
      expect(rule![1], `${state} 的色条色值与 ${cls} 的真实色值不一致`).toBe(style.color)
    }
  })

  it('第 6 条：管理员 ⇒ 渲染「提交人」列 + 处置按钮，且点「标记已处理」发 PATCH（含该行备注框的值）', async () => {
    mockedFetchMe.mockResolvedValue(ADMIN)
    respond({
      [`GET ${FEEDBACK_URL}`]: LIST_TWO,
      [`PATCH ${feedbackDetailUrl(11)}`]: { id: 11, status: 'handled' },
    })
    await mount()

    expect(headCells()).toEqual([
      '提交时间', '提交人', '反馈内容', '状态', '管理员备注', '截图', '处理',
    ])
    expect(rows().length).toBe(2)
    // 「提交人」那一格真的上了屏（两行各自的提交人）。
    const first = rows()[0]!
    expect(first.textContent).toContain('客服甲')
    expect(rows()[1]!.textContent).toContain('客服乙')

    // 待处理那一行显示的是「标记已处理」（文案随状态走，⛔ 不是两颗按钮里藏一颗）。
    const button = first.querySelector<HTMLElement>(`[data-feedback-act]`)!
    expect(button.getAttribute('data-feedback-act')).toBe(ACT_HANDLE)
    expect(button.textContent).toBe('标记已处理')

    // 管理员在本行备注框里写了东西 ⇒ 一起提交（改了备注再点按钮**不能**把它丢掉）。
    first.querySelector<HTMLInputElement>(`.${NOTE_INPUT_CLASS}`)!.value = '已定位，下版修'
    click(button)
    await flush()

    const patch = mockedRequest.mock.calls
      .map((c) => c[0] as Cfg)
      .find((c) => c.method === 'PATCH')
    expect(patch, '应当发过一条 PATCH').toBeDefined()
    expect(patch!.url).toBe(feedbackDetailUrl(11))
    expect(patch!.data).toEqual({ status: 'handled', admin_note: '已定位，下版修' })
    expect(LAYUI.messages).toContain(MESSAGES.handled)
  })

  it('第 6 条（另一半）：已处理那一行显示「改回未处理」，点了发 `status: pending`', async () => {
    mockedFetchMe.mockResolvedValue(ADMIN)
    respond({
      [`GET ${FEEDBACK_URL}`]: {
        items: [row({ status: 'handled', status_label: '已处理', admin_note: '已修复' })],
        total: 1,
      },
    })
    await mount()
    const button = root.querySelector<HTMLElement>(`[data-feedback-act]`)!
    expect(button.getAttribute('data-feedback-act')).toBe(ACT_REOPEN)
    expect(button.textContent).toBe('改回未处理')
    // 备注框预填了当前的备注（⛔ 不预填的话管理员一点按钮就把备注清空了，而且是**静默**的）。
    expect(root.querySelector<HTMLInputElement>(`.${NOTE_INPUT_CLASS}`)!.value).toBe('已修复')

    respond({
      [`GET ${FEEDBACK_URL}`]: LIST_ONE,
      [`PATCH ${feedbackDetailUrl(11)}`]: { id: 11, status: 'pending' },
    })
    click(button)
    await flush()
    const patch = mockedRequest.mock.calls
      .map((c) => c[0] as Cfg)
      .find((c) => c.method === 'PATCH')
    expect(patch!.data).toEqual({ status: 'pending', admin_note: '已修复' })
    expect(LAYUI.messages).toContain(MESSAGES.reopened)
  })

  it('管理员那一侧的 403 如实显示后端文案（⛔ 不在前端新造第二套权限判据）', async () => {
    mockedFetchMe.mockResolvedValue(ADMIN)
    respond({
      [`GET ${FEEDBACK_URL}`]: LIST_ONE,
      [`PATCH ${feedbackDetailUrl(11)}`]: () => Promise.reject(new Error('没有权限执行该操作。')),
    })
    await mount()
    click(root.querySelector<HTMLElement>(`[data-feedback-act]`)!)
    await flush()
    expect(LAYUI.messages).toContain('没有权限执行该操作。')
  })

  it('列表为空 ⇒ 空态文案说出来（空态 ≠ 取数失败，不靠留白表达）', async () => {
    respond({ [`GET ${FEEDBACK_URL}`]: { items: [], total: 0 } })
    await mount()
    const body = root.querySelector<HTMLElement>(`#${LIST_BODY_ID}`)!
    expect(body.textContent).toContain(MESSAGES.empty)
    expect(rows().length).toBe(0)
    // 空列表也**有表头**（一片白会被读成"页面坏了"）。
    expect(headCells().length).toBeGreaterThan(0)
  })

  it('取列表失败 ⇒ 如实提示（⛔ 不是静默空列表）', async () => {
    mockedRequest.mockRejectedValue(new Error('网络异常，请检查连接'))
    await mount()
    expect(LAYUI.messages).toContain('网络异常，请检查连接')
  })
})

describe('纯函数：标记与判据（可被单测证伪的那些）', () => {
  it('列头按 `isAdmin` 变（表头与行**同一份判据**）', () => {
    expect(listHeadHtml(false)).not.toContain('提交人')
    expect(listHeadHtml(true)).toContain('提交人')
    expect(listHeadHtml(true)).toContain('处理')
  })

  it('来自服务端的文本**一律转义**（`content` / 备注 / 提交人名都进 `innerHTML`）', () => {
    const host = document.createElement('div')
    host.innerHTML = __testables.listHtml(
      [
        row({
          content: '<img src=x onerror=alert(1)>',
          admin_note: '<script>bad()</script>',
          created_by_name: '<b>甲</b>',
          images: [{ key: '<k>', url: '/api/media/blob/a.png' }],
        }),
      ],
      { isAdmin: true },
    )
    expect(host.querySelector('img[src="x"]')).toBeNull()
    expect(host.querySelector('script')).toBeNull()
    expect(host.querySelector('b')).toBeNull()
    // 转义之后的**原文**仍然看得见（不是被丢掉）。
    expect(host.textContent).toContain('<img src=x onerror=alert(1)>')
  })

  it('`extOf`：去点、小写（`A.PNG` ⇒ `png`）；没有扩展名回**空串**（由签发端点兜底）', () => {
    expect(__testables.extOf('shot.PNG')).toBe('png')
    expect(__testables.extOf('shot.jpeg')).toBe('jpeg')
    // ⚠️ 没有扩展名 ⇒ **空串**（2026-10-08 改准：原先回的是整段名字 `'noext'`，
    //    而那正是 F2 缺陷的根因之一——"整段名字当 ext"会让后端收到一个必拒的值）。
    expect(__testables.extOf('noext')).toBe('')
    expect(__testables.extOf('')).toBe('')
    expect(__testables.extOf('trailing.')).toBe('')
    expect(__testables.extOf('.hidden')).toBe('')
  })

  it('时间格式化：`T` 换成空格、截到秒（时区偏移不动）', () => {
    expect(__testables.formatTime('2026-10-08T12:00:00+08:00')).toBe('2026-10-08 12:00:00')
  })

  it('截图那一格：没有图时是「—」而不是空白（空白会被读成"这列没数据"）', () => {
    expect(__testables.imagesHtml([])).toContain('—')
    const html = __testables.imagesHtml([{ key: 'k', url: '/api/media/blob/a.png' }])
    expect(html).toContain('src="/api/media/blob/a.png"')
    // 点击预览：缩略图是裸 `<img>`（点击交给 `layer.photos` 弹层），
    // ⛔ 不再包 `<a target="_blank">`——那会把「展示还是下载」交给浏览器。
    expect(html).not.toContain('<a ')
  })

  it('截图点击预览：列表每次重渲后把分组选择器交给 `layer.photos`（先例 = 截图管理页）', async () => {
    await mount()
    expect(LAYUI.layer.photos).toHaveBeenCalledWith({ photos: SHOTS_SELECTOR, anim: 5 })
  })

  it('`rejectReason` 三条判据各自能被证伪（边界：恰好 10 MB 放行、10 MB + 1 字节被拒）', () => {
    const exact = fakeFile('exact.png', 'image/png', 10 * 1024 * 1024)
    expect(__testables.isTooLarge(exact)).toBe(false)
    expect(__testables.isTooLarge(oversizedFile())).toBe(true)
    expect(__testables.rejectReason(exact, 0)).toBeNull()
    expect(__testables.rejectReason(oversizedFile(), 0)).toBe(MESSAGES.imageTooLarge)
    expect(__testables.rejectReason(fakeFile('a.gif', 'image/gif'), 0)).toBe(MESSAGES.imageBadType)
    expect(__testables.rejectReason(fakeFile('a.png', 'image/png'), MAX_IMAGES)).toBe(
      MESSAGES.tooManyImages,
    )
    // 没有扩展名但 `type` 是准的（粘贴进来的常见形状）⇒ 走 MIME 兜底**放行**。
    expect(__testables.rejectReason(fakeFile('clipboard', 'image/png'), 0)).toBeNull()
    // ⭐ F2：名字给不出 ext 时由 `type` 兜底；两条路都推不出 ⇒ **不收**（⛔ 不发必拒的 ext）。
    expect(__testables.resolveExt(fakeFile('', 'image/png'))).toBe('png')
    expect(__testables.resolveExt(fakeFile('image', 'image/jpeg'))).toBe('jpeg')
    expect(__testables.resolveExt(fakeFile('shot.PNG', 'image/jpeg'))).toBe('png')
    expect(__testables.resolveExt(fakeFile('', 'image/webp'))).toBeNull()
    expect(__testables.resolveExt(fakeFile('clipboard', ''))).toBeNull()
    expect(__testables.rejectReason(fakeFile('clipboard', ''), 0)).toBe(MESSAGES.imageBadType)
    // 兜底能推出的那三种**取值**必须与后端 `storage.ALLOWED_EXTS` (`("jpg","jpeg","png")`)
    // 是同一个集合（⛔ 不新造第四种写法）。⚠️ 这里比的是**集合**，顺序不参与判据
    // （前端数组按字母序排的；`imageBadType` 那句的**词序**另由下面那条 F3 用例钉住）。
    expect([...ALLOWED_EXTS].sort()).toEqual(['jpeg', 'jpg', 'png'])
    expect(MAX_IMAGES).toBe(3)
    expect(__testables.buildHtml()).toContain('问题描述')
  })

  /**
   * ⭐ **F3（2026-10-08 红队）：前端预检那句必须与后端拒收那句逐字相同。**
   *
   * 后端 = `apps/business/api/feedback_views.py::IMAGE_TYPE_LIMIT_MSG`（与头像那条先例
   * `apps/core/profile_views.py` 逐字相同）。两处各留一套措辞 ⇒ 同一件「图不对」的事，
   * 用户会看到**两种说法**（前端预检一次、后端拒收一次）。
   *
   * ⚠️ 这里钉的是**字面值**（⛔ 不是 `expect(MESSAGES.imageBadType).toBe(MESSAGES.imageBadType)`
   * 那种自指断言——它在被断言的常量改坏时**不会红**，见 E-0024）。
   * ⚠️ 词序是 `jpg / jpeg / png`（照抄后端，别"排整齐"成 `png / jpg / jpeg`），末尾不带句号。
   */
  it('类型预检那句与后端 `IMAGE_TYPE_LIMIT_MSG` **逐字相同**（⛔ 不许两边各留一套）', () => {
    expect(MESSAGES.imageBadType).toBe('只支持 jpg / jpeg / png')
    // 反向对照：上一版那句（`png / jpg / jpeg` + 「格式的截图」）**必须不再是**今天这句——
    // 少了这条，"改回去"与"没改"在断言上分不开（两条都只比字面量字符串）。
    expect(MESSAGES.imageBadType).not.toBe('只支持 png / jpg / jpeg 格式的截图')
  })
})
