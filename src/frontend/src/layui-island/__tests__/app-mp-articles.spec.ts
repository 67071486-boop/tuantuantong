// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ArticleDetail, ArticleRow } from '@/api/articles'

/**
 * 【APP与小程序 → 发布文章作品】island 页的契约测试（2026-10-23 建）。
 *
 * 这一页是**自有功能**（对标物里没有），没有证据件可对拍 ⇒ 本文件钉的是**有真实后果**的约定：
 *
 * 1. **驱动真交互**（`AGENTS.md` §5-G：自建 island 页必须有能红的交互用例）：
 *    新建 → 填标题 / 正文（含粗体 + 一张图）→ 保存草稿 → **请求体**里 `status=draft`、
 *    `content_html` 含 `<strong>` 且图片**只有 `data-key`**（⛔ 没有 `src`）。
 * 2. **发布**：`POST …/<id>/publish/` 真被调用、列表**重新拉了一次**、刷新后状态变已发布并显示阅读数。
 * 3. **列表渲染阅读数**；频道预览里**不显示**阅读数（业务方 2026-10-23 口径第 5 条）。
 * 4. **预览随标题输入更新**（150ms 防抖）。
 * 5. **图片上传三步**：`media/ticket`（取票）→ `PUT upload_url`（直传）→ `media/claim`（认领）。
 * 6. **三态**：列表加载失败时显示后端文案 + 「重试」，点重试真的重新取数。
 * 7. **编辑已有文章必须先取详情**：列表行不带 `content_html` / `media`（后端 2026-10-23 契约），
 *    直接拿列表行进编辑器会把正文渲染成空白、一保存就覆盖成空。
 *
 * ## 手法（照 `pages/__tests__/open-apps.spec.ts`）
 * - `vi.mock('@/layui/island')`：jsdom 里加载不了真 layui，换成可控替身；
 * - `vi.mock('@/api/http')`：只换**网络出口**——`@/api/articles` 跑的是**真实现** ⇒
 *   「打到哪条 URL、body 里是什么键」这两条判据才有意义；
 * - 直传那一跳走**真的** `putFileToStorage`，只把全局 `fetch` 换成桩（它才是幂等字节的出口）。
 *
 * ⚠️ 夹具一律**合成值**（E-0077）：标题 / 简介 / 对象名 / 读链接都是编的。
 */
const LAYUI = vi.hoisted(() => {
  const messages: string[] = []
  const confirms: string[] = []
  // ⚠️ 这两个数组是**挂载顺序的运行期判据**（`AGENTS.md` §5-G 陷阱 ③）：
  //    ③a 先挂样式 ⇒ bootstrapIsland 看到的是 `false`；③b 标记先塞 ⇒ initIslandElements 看到 `true`。
  //    ⚠️ 选择器这里写**字面值**（不能引页面模块导出的常量）：本工厂在页面模块求值**之前**
  //    就会跑（它是那条 import 链的一环）⇒ 引常量会读到尚未初始化的绑定。
  const seenByBootstrap: boolean[] = []
  const seenByInit: boolean[] = []
  return {
    messages,
    confirms,
    seenByBootstrap,
    seenByInit,
    layer: {
      msg: vi.fn((text: string) => {
        messages.push(text)
      }),
      confirm: vi.fn((text: string, _options: unknown, ok: () => void) => {
        confirms.push(text)
        ok()
      }),
    },
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => {
    LAYUI.seenByBootstrap.push(document.querySelector('#ampNewBtn') !== null)
    return { layer: LAYUI.layer }
  }),
  initIslandElements: vi.fn(() => {
    LAYUI.seenByInit.push(document.querySelector('#ampNewBtn') !== null)
  }),
  islandMsg: (_layer: unknown, text: string) => {
    LAYUI.messages.push(text)
  },
}))

/** 只换**网络出口**（见文件头）。`request()` 返回的是**已解包**的 `data`。 */
vi.mock('@/api/http', () => ({ request: vi.fn() }))

import { request } from '@/api/http'
import page, {
  ARTICLE_ACTION_ATTR,
  ARTICLE_ID_ATTR,
  BACK_BUTTON_ID,
  COVER_FILE_ID,
  COVER_PREVIEW_ID,
  EDITOR_ID,
  LIST_BODY_ID,
  LIST_RETRY_ATTR,
  LIST_STATE_ID,
  MESSAGES,
  cleanArticleHtml,
  parseArticleBlocks,
  NEW_BUTTON_ID,
  PREVIEW_CHANNEL_ID,
  PREVIEW_TITLE_ID,
  PUBLISH_BUTTON_ID,
  SAVE_DRAFT_BUTTON_ID,
  TITLE_INPUT_ID,
  PREVIEW_VIEW_ATTR,
  CHANNEL_CARDS_ID,
  CHANNEL_COUNT_ID,
} from '../pages/app-mp-articles'

const mockedRequest = vi.mocked(request)

type Cfg = { method?: string; url?: string; data?: unknown }
type Routes = Record<string, (data?: unknown) => unknown>

const ARTICLES_URL = '/api/business/articles/'
const PROFILE_URL = '/api/business/channel-profile/'
const TICKET_URL = '/api/business/articles/media/ticket'
const CLAIM_URL = '/api/business/articles/media/claim'

/** 响应路由：按 `METHOD URL` 认领。**没有备好响应就抛**（⛔ 不是静默回 undefined）。 */
let routes: Routes

function ready(key: string, fn: (data?: unknown) => unknown): void {
  routes[key] = fn
}

function flush(): Promise<void> {
  return (async () => {
    for (let i = 0; i < 10; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 0))
    }
  })()
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function callTo(method: string, url: string): Cfg {
  const found = mockedRequest.mock.calls
    .map((call) => call[0] as Cfg)
    .filter((cfg) => (cfg.method ?? 'get').toLowerCase() === method.toLowerCase() && cfg.url === url)
  if (found.length === 0) throw new Error(`没有发出 ${method.toUpperCase()} ${url}`)
  return found[found.length - 1]!
}

function callCount(method: string, url: string): number {
  return mockedRequest.mock.calls
    .map((call) => call[0] as Cfg)
    .filter((cfg) => (cfg.method ?? 'get').toLowerCase() === method.toLowerCase() && cfg.url === url).length
}

// ── 夹具（**全部合成**）────────────────────────────────────────────────────

/** 草稿：列表行（**没有** `content_html` / `media`，照后端契约）。 */
const DRAFT: ArticleRow = {
  id: 7,
  title: '合成草稿甲',
  intro: '合成摘要',
  status: 'draft',
  cover_url: '',
  read_count: 3,
  published_at: null,
  created_at: '2026-10-20T10:00:00+08:00',
  updated_at: '2026-10-21T09:30:00+08:00',
  created_by: '合成作者',
}

/** 已发布：列表行。`read_count` 取一个**别的格子里不会出现**的值，方便判据。 */
const PUBLISHED: ArticleRow = {
  id: 8,
  title: '合成已发布乙',
  intro: '',
  status: 'published',
  cover_url: 'https://media.example.invalid/synthetic/cover-b.png',
  read_count: 4207,
  published_at: '2026-10-22T08:00:00+08:00',
  created_at: '2026-10-19T10:00:00+08:00',
  updated_at: '2026-10-22T08:00:00+08:00',
  created_by: '合成作者',
}

/** 详情：列表行 + **必有**的 `content_html` / `media`。 */
const DRAFT_DETAIL: ArticleDetail = {
  ...DRAFT,
  content_html: '<p>旧正文 <strong>粗体</strong></p>',
  media: {},
}

/** 已发布文章的详情（正文 + media）。 */
const PUBLISHED_DETAIL: ArticleDetail = {
  ...PUBLISHED,
  content_html: '<p>已发布正文</p>',
  media: {},
}

/** 频道简介（工作室名 / 简介 / 已发布篇数）。 */
const PROFILE = { name: '合成工作室', intro: '合成简介', article_count: 1 }

const UPLOAD_URL = '/api/media/blob/demo/users/1/article-cover.png'
const TICKET_KEY = 'demo/users/1/article-cover.png'
const CLAIM_URL_OK = 'https://media.example.invalid/demo/users/1/article-cover.png'

describe('发布文章作品 island 页（app-mp-articles）', () => {
  let root: HTMLElement
  let cleanup: (() => void) | null
  /** 服务端**此刻**那份列表（创建 / 发布会改它 ⇒ 刷新之后看得到变化）。 */
  let articlesOnServer: ArticleRow[]

  beforeEach(() => {
    vi.clearAllMocks()
    LAYUI.messages.length = 0
    LAYUI.confirms.length = 0
    LAYUI.seenByBootstrap.length = 0
    LAYUI.seenByInit.length = 0
    document.body.innerHTML = ''
    root = document.createElement('div')
    document.body.appendChild(root)
    cleanup = null

    articlesOnServer = [DRAFT, PUBLISHED]
    routes = {}
    ready(`GET ${ARTICLES_URL}`, () => ({
      items: articlesOnServer,
      total: articlesOnServer.length,
      page: 1,
      page_size: 200,
    }))
    ready(`GET ${PROFILE_URL}`, () => PROFILE)
    ready(`GET /api/business/articles/7/`, () => DRAFT_DETAIL)
    ready(`GET /api/business/articles/8/`, () => PUBLISHED_DETAIL)
    ready(`DELETE /api/business/articles/7/`, () => ({}))
    ready(`PATCH /api/business/articles/8/`, (data) => {
      const patch = (data ?? {}) as Partial<ArticleRow>
      articlesOnServer = articlesOnServer.map((row) =>
        row.id === PUBLISHED.id ? { ...row, ...patch } : row,
      )
      return articlesOnServer.find((row) => row.id === PUBLISHED.id)!
    })
    ready(`PATCH /api/business/articles/7/`, (data) => {
      const patch = (data ?? {}) as Partial<ArticleRow>
      articlesOnServer = articlesOnServer.map((row) =>
        row.id === DRAFT.id ? { ...row, ...patch } : row,
      )
      return articlesOnServer.find((row) => row.id === DRAFT.id)!
    })
    // 新建：**用入参**造一行（标题 / 状态都从页面真实发出来的载荷里取，⛔ 不是写死夹具）。
    ready(`POST ${ARTICLES_URL}`, (data) => {
      const payload = (data ?? {}) as Record<string, unknown>
      const created: ArticleRow = {
        ...DRAFT,
        id: 9,
        title: String(payload.title ?? ''),
        intro: String(payload.intro ?? ''),
        status: (payload.status as ArticleRow['status']) ?? 'draft',
        read_count: 0,
        created_at: '2026-10-23T10:00:00+08:00',
        updated_at: '2026-10-23T10:00:00+08:00',
      }
      articlesOnServer = [...articlesOnServer, created]
      return created
    })
    // 发布：把这一行改成已发布（`published_at` / 阅读数都变化，刷新后才看得出来）。
    ready(`POST /api/business/articles/9/publish/`, () => {
      articlesOnServer = articlesOnServer.map((row) =>
        row.id === 9
          ? {
              ...row,
              status: 'published' as const,
              published_at: '2026-10-23T10:01:00+08:00',
              read_count: 12,
            }
          : row,
      )
      return articlesOnServer.find((row) => row.id === 9)!
    })
    ready(`POST ${TICKET_URL}`, () => ({
      key: TICKET_KEY,
      upload_url: UPLOAD_URL,
      expires_in: 600,
    }))
    ready(`POST ${CLAIM_URL}`, () => ({ key: TICKET_KEY, url: CLAIM_URL_OK }))

    mockedRequest.mockImplementation((cfg: Cfg) =>
      Promise.resolve().then(() => {
        const key = `${(cfg.method ?? 'get').toUpperCase()} ${cfg.url ?? ''}`
        const handler = routes[key]
        if (handler === undefined) throw new Error(`测试没有为 ${key} 准备响应`)
        return handler(cfg.data)
      }),
    )
  })

  afterEach(() => {
    cleanup?.()
    // 每个用例自己收口（`AGENTS.md` §5-G 陷阱 ④：上一个用例挂载的页面会继续打**同一个**替身）。
    document.body.innerHTML = ''
    vi.unstubAllGlobals()
  })

  async function mount(): Promise<void> {
    cleanup = await page.mount(root, { onNavigate: vi.fn() })
    await flush()
  }

  function byId<T extends HTMLElement>(id: string): T {
    const node = root.querySelector<T>(`#${id}`)
    if (node === null) throw new Error(`页面里没有 #${id}`)
    return node
  }

  function clickNew(): Promise<void> {
    byId<HTMLButtonElement>(NEW_BUTTON_ID).click()
    return flush()
  }

  function typeTitle(value: string): void {
    const input = byId<HTMLInputElement>(TITLE_INPUT_ID)
    input.value = value
    input.dispatchEvent(new Event('input', { bubbles: true }))
  }

  function typeBody(html: string): void {
    const editor = byId<HTMLElement>(EDITOR_ID)
    editor.innerHTML = html
    editor.dispatchEvent(new Event('input', { bubbles: true }))
  }

  it('保存草稿：请求体 status=draft、content_html 含 <strong> 且图片只有 data-key（没有 src）', async () => {
    await mount()
    expect(LAYUI.seenByBootstrap).toEqual([false])
    expect(LAYUI.seenByInit).toEqual([true])

    await clickNew()
    typeTitle('合成新文章')
    typeBody(
      '<p>正文 <strong>加粗</strong>' +
        '<img data-key="demo/users/1/body-a.png" src="https://media.example.invalid/body-a.png" ' +
        'alt="脏属性" onerror="alert(1)"></p>',
    )
    byId<HTMLButtonElement>(SAVE_DRAFT_BUTTON_ID).click()
    await flush()

    const body = callTo('POST', ARTICLES_URL).data as Record<string, unknown>
    expect(body.status).toBe('draft')
    expect(body.title).toBe('合成新文章')
    expect(String(body.content_html)).toContain('<strong>加粗</strong>')
    expect(String(body.content_html)).toContain('data-key="demo/users/1/body-a.png"')
    // ⛔ 保存时**不许**把编辑器为了显示而补的 src / alt / onerror 写进库。
    expect(String(body.content_html)).not.toContain('src=')
    expect(String(body.content_html)).not.toContain('onerror')
    // 保存后回到列表并**真的重拉了一次**（看得到刚建的标题）。
    expect(callCount('GET', ARTICLES_URL)).toBeGreaterThan(1)
    expect(byId<HTMLElement>(LIST_BODY_ID).textContent).toContain('合成新文章')
    expect(LAYUI.messages).toContain(MESSAGES.saved)
  })

  it('发布：publish/ 真被调用、列表刷新后状态变已发布并显示阅读数', async () => {
    await mount()
    const before = callCount('GET', ARTICLES_URL)

    await clickNew()
    typeTitle('要发布的文章')
    byId<HTMLButtonElement>(PUBLISH_BUTTON_ID).click()
    await flush()

    expect(callTo('POST', '/api/business/articles/9/publish/')).toBeDefined()
    expect(callCount('GET', ARTICLES_URL)).toBeGreaterThan(before)
    const listText = byId<HTMLElement>(LIST_BODY_ID).textContent ?? ''
    expect(listText).toContain('要发布的文章')
    expect(listText).toContain('已发布')
    expect(listText).toContain('12')
    expect(LAYUI.messages).toContain(MESSAGES.published)
  })

  it('列表渲染阅读数；频道预览里**不显示**阅读数', async () => {
    await mount()
    const listText = byId<HTMLElement>(LIST_BODY_ID).textContent ?? ''
    expect(listText).toContain('4207')
    expect(listText).toContain('已发布')

    // 预览的切换按钮在编辑态的 DOM 里（两块都常驻，只是 `display` 切换）⇒ 直接点即可。
    const channelTab = root.querySelector<HTMLElement>(`[${PREVIEW_VIEW_ATTR}="channel"]`)
    if (channelTab === null) throw new Error('没有「频道列表」切换按钮')
    channelTab.click()
    await flush()

    const channelText = byId<HTMLElement>(PREVIEW_CHANNEL_ID).textContent ?? ''
    expect(channelText).toContain('合成工作室')
    expect(channelText).toContain('合成已发布乙')
    expect(channelText).not.toContain('4207')
  })

  it('预览随标题输入更新（150ms 防抖后）', async () => {
    await mount()
    await clickNew()
    typeTitle('实时预览标题')
    // 防抖是 150ms ⇒ 先确认**还没**更新（避免「其实没防抖」的假绿），再等过去。
    expect(byId<HTMLElement>(PREVIEW_TITLE_ID).textContent).toBe('无标题')
    await sleep(220)
    expect(byId<HTMLElement>(PREVIEW_TITLE_ID).textContent).toBe('实时预览标题')
  })

  it('封面图片走三步：ticket → PUT 原字节 → claim，认领后预览用读链接', async () => {
    const fetchStub = vi.fn(async () => ({ ok: true, status: 200 }))
    vi.stubGlobal('fetch', fetchStub)
    await mount()
    await clickNew()

    const file = new File([new Uint8Array([1, 2, 3])], 'cover.png', { type: 'image/png' })
    const input = byId<HTMLInputElement>(COVER_FILE_ID)
    Object.defineProperty(input, 'files', { value: [file], configurable: true })
    input.dispatchEvent(new Event('change', { bubbles: true }))
    await flush()

    // ① 取票：`{ext}`（去点、小写）
    expect((callTo('POST', TICKET_URL).data as Record<string, unknown>).ext).toBe('png')
    // ② 直传：PUT 到服务端给的那条地址，body 就是**同一个 File**（不是 multipart）。
    expect(fetchStub).toHaveBeenCalledTimes(1)
    const [url, init] = fetchStub.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe(UPLOAD_URL)
    expect(init.method).toBe('PUT')
    expect(init.body).toBe(file)
    // ③ 认领：`{key}`
    expect((callTo('POST', CLAIM_URL).data as Record<string, unknown>).key).toBe(TICKET_KEY)
    // 认领回来的读链接真的上屏了（封面预览）。
    const previewImg = byId<HTMLElement>(COVER_PREVIEW_ID).querySelector('img')
    expect(previewImg?.getAttribute('src')).toBe(CLAIM_URL_OK)
  })

  it('列表加载失败显示后端文案 + 重试；点重试真的重新取数', async () => {
    ready(`GET ${ARTICLES_URL}`, () => {
      throw new Error('合成后端错误')
    })
    await mount()

    const stateText = byId<HTMLElement>(LIST_STATE_ID).textContent ?? ''
    expect(stateText).toContain('合成后端错误')
    const retry = root.querySelector<HTMLElement>(`[${LIST_RETRY_ATTR}]`)
    if (retry === null) throw new Error('失败态里没有「重试」按钮')

    // 重试前把服务端修好（判据是「重试真的又打了一次」+ 这次能看到行）。
    ready(`GET ${ARTICLES_URL}`, () => ({
      items: articlesOnServer,
      total: articlesOnServer.length,
      page: 1,
      page_size: 200,
    }))
    const before = callCount('GET', ARTICLES_URL)
    retry.click()
    await flush()
    expect(callCount('GET', ARTICLES_URL)).toBeGreaterThan(before)
    expect(byId<HTMLElement>(LIST_BODY_ID).textContent).toContain('合成草稿甲')
  })

  it('预览解析兼容 <b>/<i>/<div>，并且丢掉 <script> 这类标签（只认 data-key）', () => {
    // 后端会把 `<b>` / `<i>` / `<div>` 归一化成 `<strong>` / `<em>` / 段落；编辑器
    // （`document.execCommand('bold')`）在部分浏览器上产出的就是 `<b>` ⇒ 预览要兼容。
    const blocks = parseArticleBlocks(
      '<div>段落<br><b>粗</b> <i>斜</i>' +
        '<img data-key="demo/users/1/a.png" src="https://evil.example.invalid/a.png" ' +
        'onerror="alert(1)"><script>alert(1)</script></div>',
      { 'demo/users/1/a.png': 'https://media.example.invalid/demo/users/1/a.png' },
    )
    const paragraph = blocks.find((block) => block.type === 'p')
    if (paragraph === undefined || paragraph.type !== 'p') throw new Error('没有解析出段落块')
    expect(paragraph.spans.some((span) => span.bold && span.text.includes('粗'))).toBe(true)
    expect(paragraph.spans.some((span) => span.italic && span.text.includes('斜'))).toBe(true)
    const image = blocks.find((block) => block.type === 'img')
    if (image === undefined || image.type !== 'img') throw new Error('没有解析出图片块')
    // ⛔ 读链接只从 `media` 取（HTML 里的 src 一律不认）。
    expect(image.url).toBe('https://media.example.invalid/demo/users/1/a.png')
    expect(JSON.stringify(blocks)).not.toContain('alert(1)')
    // 保存前也把 `<b>` 归一化成 `<strong>`（与后端同口径，少一次惊讶）。
    expect(cleanArticleHtml('<p><b>粗</b></p>')).toBe('<p><strong>粗</strong></p>')
  })

  it('编辑已有文章先取详情（列表行不带 content_html / media，⛔ 不能直接进编辑器）', async () => {
    await mount()
    const editButton = root.querySelector<HTMLElement>(
      `[${ARTICLE_ACTION_ATTR}="edit"][${ARTICLE_ID_ATTR}="7"]`,
    )
    if (editButton === null) throw new Error('列表里没有「编辑」按钮')
    editButton.click()
    await flush()

    expect(callTo('GET', '/api/business/articles/7/')).toBeDefined()
    expect(byId<HTMLElement>(EDITOR_ID).innerHTML).toContain('旧正文')
    expect(byId<HTMLInputElement>(TITLE_INPUT_ID).value).toBe('合成草稿甲')
    // 回到列表那颗按钮在编辑态里可见（这里只确认它在 DOM 里，状态切换由 `display` 控制）。
    expect(byId<HTMLButtonElement>(BACK_BUTTON_ID)).toBeDefined()
  })

  it('删除确认里的标题必须转义（⛔ 不能把用户标题当 HTML 拼进 layer.confirm）', async () => {
    articlesOnServer = [{ ...DRAFT, title: '<img src=x onerror=alert(1)>合成标题' }]
    await mount()
    const deleteButton = root.querySelector<HTMLElement>(
      `[${ARTICLE_ACTION_ATTR}="delete"][${ARTICLE_ID_ATTR}="7"]`,
    )
    if (deleteButton === null) throw new Error('列表里没有「删除」按钮')
    deleteButton.click()
    await flush()

    const text = LAYUI.confirms[0] ?? ''
    // 判据：**标签起点被转义**（`<img` 不再成立）⇒ 浏览器只会当文字，不会造出元素。
    // ⚠️ `onerror=` 仍以**文字**出现，那是正常的（转义后的整段标题）；⛔ 别断言它消失。
    expect(text).toContain('&lt;img')
    expect(text).not.toContain('<img')
    expect(text).toContain('合成标题')
  })

  it('编辑已发布文章后保存：仍以 status=published 落库（⛔ 不能悄悄下架）', async () => {
    await mount()
    const editButton = root.querySelector<HTMLElement>(
      `[${ARTICLE_ACTION_ATTR}="edit"][${ARTICLE_ID_ATTR}="8"]`,
    )
    if (editButton === null) throw new Error('列表里没有已发布文章的「编辑」按钮')
    editButton.click()
    await flush()

    expect(callTo('GET', '/api/business/articles/8/')).toBeDefined()
    typeTitle('合成已发布乙（改过）')
    byId<HTMLButtonElement>(SAVE_DRAFT_BUTTON_ID).click()
    await flush()

    const body = callTo('PATCH', '/api/business/articles/8/').data as Record<string, unknown>
    expect(body.status).toBe('published')
    // 已发布文章保存后不能误报「已保存草稿」（评审 2026-10-23）。
    expect(LAYUI.messages).toContain(MESSAGES.savedPublished)
    expect(LAYUI.messages).not.toContain(MESSAGES.saved)
    // 刷新后的列表仍是已发布（用状态列的字面值判，不看服务端夹具自证）。
    const rowText = byId<HTMLElement>(LIST_BODY_ID).textContent ?? ''
    expect(rowText).toContain('合成已发布乙（改过）')
    expect(rowText).toContain('已发布')
  })

  it('频道预览按发布时间倒序，篇数与公开端同源（不是管理端 updated_at 顺序）', async () => {
    articlesOnServer = [
      {
        ...PUBLISHED,
        id: 11,
        title: '旧发布·新改动',
        published_at: '2026-10-01T08:00:00+08:00',
        updated_at: '2026-10-23T09:00:00+08:00',
      },
      {
        ...PUBLISHED,
        id: 12,
        title: '新发布·旧改动',
        published_at: '2026-10-22T08:00:00+08:00',
        updated_at: '2026-10-22T08:00:00+08:00',
      },
    ]
    ready(`GET ${PROFILE_URL}`, () => ({ ...PROFILE, article_count: 3 }))
    await mount()

    const cards = Array.from(
      root.querySelectorAll<HTMLElement>(`#${CHANNEL_CARDS_ID} .ampCardTitle`),
    ).map((node) => node.textContent)
    expect(cards).toEqual(['新发布·旧改动', '旧发布·新改动'])
    // 刻意让 profile 的篇数（3）≠ 本页行数（2）：判据必须取公开端的数，⛔ 不能拿行数冒充。
    expect(byId<HTMLElement>(CHANNEL_COUNT_ID).textContent).toBe('3 篇文章')
  })
})
