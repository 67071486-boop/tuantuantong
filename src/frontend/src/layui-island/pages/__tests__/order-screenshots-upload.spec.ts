// @vitest-environment jsdom
/**
 * 【上传进度图】弹层（island key `order-screenshots-upload`）的**真交互**页测试。
 *
 * 任务书 `docs/plans/2026-09-25-media-storage.md` Task 8 / spec §8：
 *  拖拽 + Ctrl+V 粘贴 + 备注（≤50）+「图片类型」单选 + 已上传表格 + 进度条，
 *  直传 = 拿 ticket ⇒ `fetch(PUT)` ⇒ 认领（**不经过我们服务器**）。
 *
 * ⚠️ 判据一律写**字面值**：拿页面导出的常量去断言页面自己的行为是自指断言，
 * 常量被改坏时不会红（E-0024）。所以下面每条用例里的 URL / 文案 / 列名都是手写的。
 *
 * ⚠️ 三条 island 陷阱（`AGENTS.md` §5-G）由**实现**负责照做，但本文件也钉住最致命的那条
 * （先 `bootstrapIsland` 再塞标记）——见「挂载顺序」那条用例。
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const HERE = path.dirname(fileURLToPath(import.meta.url))
/** 证据件原文（`public/replica/order-screenshots-upload.html`；4 层上去 = `src/frontend`）。 */
const EVIDENCE = fs.readFileSync(
  path.resolve(HERE, '../../../../public/replica/order-screenshots-upload.html'),
  'utf8',
)

/** 合成单号（20 位数字，与后端 `Order.order_no` 的形状一致；⛔ 不用证据件里那个真单号）。 */
const ORDER_NO = '20260101000000000001'
/** 直传目标（服务端签发的 URL）——**不是**我们自己的 API 前缀。 */
const UPLOAD_URL = 'https://cos.example.invalid/bucket/demo/orders/x/y.jpg'

/**
 * ⚠️ **必须 `vi.hoisted`**：`vi.mock` 的工厂被提升到文件顶部、在 ESM import 期就执行，
 * 那时模块顶层的 `const` 还没初始化（TDZ）⇒ 工厂里直接引用 `MSGS` / `LAYUI` 会抛
 * `Cannot access '…' before initialization`。仓内先例：`user-myinfo.spec.ts:18`。
 */
const { MSGS, LAYUI, TABLE_HANDLERS } = vi.hoisted(() => {
  const msg = vi.fn()
  const tableRender = vi.fn()
  const tableReloadData = vi.fn()
  const tableReload = vi.fn()
  const layerOpen = vi.fn()
  const layerClose = vi.fn()
  const elementProgress = vi.fn()
  /** layui 表格的 `tool(tableList)` 处理器（行内两颗按钮走这条通道）。 */
  const tableHandlers = new Map<string, (...args: unknown[]) => unknown>()
  return {
    MSGS: [] as string[],
    TABLE_HANDLERS: tableHandlers,
    LAYUI: {
      msg,
      tableRender,
      tableReloadData,
      tableReload,
      layerOpen,
      layerClose,
      elementProgress,
      /** `bootstrap` = 挂 layui 样式/脚本，`artifact` = 取证据件标记（挂载顺序的**运行期**证据）。 */
      order: [] as string[],
      form: { render: vi.fn() },
      table: {
        render: tableRender,
        reloadData: tableReloadData,
        reload: tableReload,
        on: (e: string, cb: (...args: unknown[]) => unknown) => {
          tableHandlers.set(e, cb)
        },
      },
      layer: { msg, open: layerOpen, close: layerClose, confirm: vi.fn(), photos: vi.fn(), load: vi.fn() },
      element: { init: vi.fn(), progress: elementProgress },
      upload: { render: vi.fn() },
    },
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: (() => {
    LAYUI.order.push('bootstrap')
    return Promise.resolve(LAYUI)
  }) as unknown as () => Promise<unknown>,
  initIslandElements: vi.fn(),
  islandMsg: (_layer: unknown, text: string) => {
    MSGS.push(text)
  },
}))

vi.mock('@/api/screenshots', () => ({
  requestScreenshotTicket: vi.fn(),
  claimScreenshot: vi.fn(),
  fetchScreenshots: vi.fn(),
  deleteScreenshot: vi.fn(),
  screenshotZipUrl: vi.fn((orderNo: string) => `/api/business/orders/${orderNo}/screenshots/zip`),
}))

/**
 * 分档压缩单独有 `imageCompress.spec.ts`（纯函数 + 档位）。这里只把**压缩这一步**换成
 * 直通桩：jsdom 没有 canvas、也不会真正解码图片，真压缩会让用例永远等不到 `onload`。
 */
vi.mock('@/layui-island/imageCompress', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui-island/imageCompress')>()
  return { ...actual, compressImage: vi.fn(async (file: File) => file) }
})

import { claimScreenshot, fetchScreenshots, requestScreenshotTicket, type ScreenshotItem } from '@/api/screenshots'
import { compressImage } from '@/layui-island/imageCompress'
import page from '../order-screenshots-upload'

async function flush(): Promise<void> {
  for (let i = 0; i < 12; i += 1) await Promise.resolve()
}

/**
 * 最近一次挂载返回的卸载函数（I-3 的「卸载路也要释放」判据要用它）。
 * ⚠️ 不把 `mountPage` 的返回类型改成对象：24 条在册用例都在解构 `root`，
 * 改签名是**为了加一条判据去动 24 条用例**——不值。
 */
let unmount: (() => void) | undefined

async function mountPage(orderNo = ORDER_NO): Promise<HTMLElement> {
  window.history.replaceState(
    {}, '',
    `/layui-island/page.html?page=order-screenshots-upload&order_no=${orderNo}`,
  )
  const root = document.createElement('div')
  document.body.appendChild(root)
  unmount = await page.mount(root, { onNavigate: vi.fn() })
  await flush()
  return root
}

/** 造一个 `File`（内容无所谓，压缩那一跳已被桩掉）。 */
function makeFile(name = 'a.jpg', type = 'image/jpeg'): File {
  return new File([new Uint8Array([0xff, 0xd8, 0xff])], name, { type })
}

/** 覆盖 `size`（jsdom 里造 19 MB 的真实字节太贵）。 */
function withSize(file: File, size: number): File {
  Object.defineProperty(file, 'size', { value: size })
  return file
}

/**
 * 合成一批「已上传」行（`GET …/screenshots` 的载荷形状）。
 * ⛔ 不发真链接、不用真图：`url` 只仿服务端签发链接的形状（`AGENTS.md` §5-C 夹具纪律）。
 */
function shots(count: number, imgType = 0): ScreenshotItem[] {
  return Array.from({ length: count }, (_, index) => ({
    id: index + 1,
    img_type: imgType,
    url: `https://cos.example.invalid/bucket/shot-${index}.jpg`,
    original_name: `shot-${index}.jpg`,
    size_bytes: 1024,
    remark: '',
    uploaded_by: 1,
    created_at: '2026-01-01T00:00:00Z',
  }))
}

/** 驱动 `#fileInput` 的 `change`（jsdom 里 `input.click()` 不会真开文件框）。 */
async function chooseFiles(root: HTMLElement, files: File[]): Promise<void> {
  const input = root.querySelector<HTMLInputElement>('#fileInput')
  expect(input, '证据件/实现里应当有 #fileInput').not.toBeNull()
  Object.defineProperty(input!, 'files', { value: files, configurable: true })
  input!.dispatchEvent(new Event('change'))
  await flush()
}

/** 驱动拖拽落点（`drop` 带 `dataTransfer.files`）。 */
async function dropFiles(root: HTMLElement, files: File[]): Promise<void> {
  const area = root.querySelector<HTMLElement>('#drop_area')
  expect(area, '证据件里应当有 #drop_area').not.toBeNull()
  const event = new Event('drop', { bubbles: true, cancelable: true })
  Object.assign(event, { dataTransfer: { files } })
  area!.dispatchEvent(event)
  await flush()
}

/** 驱动 Ctrl+V 粘贴（`clipboardData.items` 里的文件项）。 */
async function pasteFiles(root: HTMLElement, files: File[]): Promise<void> {
  const box = root.querySelector<HTMLElement>('#uploadInput')
  expect(box, '证据件里应当有 #uploadInput').not.toBeNull()
  const event = new Event('paste', { bubbles: true, cancelable: true })
  Object.assign(event, {
    clipboardData: {
      items: files.map((file) => ({
        kind: 'file', type: file.type, getAsFile: () => file,
      })),
    },
  })
  box!.dispatchEvent(event)
  await flush()
}

/** 点「点我将上传的图片提交保存」。 */
async function clickSave(root: HTMLElement): Promise<void> {
  const button = root.querySelector<HTMLElement>('button[lay-filter="savebtn"]')
  expect(button, '证据件里应当有保存按钮').not.toBeNull()
  button!.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
  await flush()
}

/**
 * 取某个 mock 的**最后一次**调用。
 * ⚠️ 不用 `calls.at(-1)`：本工程 tsconfig 的 `lib` 目标没有 `Array.prototype.at`
 * （实测 TS2550；同仓先例见 `emp-billsummary.spec.ts:1066` 与 `router/__tests__/routes.spec.ts:28`）。
 */
function lastCall(fn: { mock: { calls: unknown[][] } }): unknown[] | undefined {
  const { calls } = fn.mock
  return calls.length > 0 ? calls[calls.length - 1] : undefined
}

/** 表格最近一次拿到的行数据（对标用 `table.reload({data})` 刷新，见 `uploadpicnew.js:417-425`）。 */
function lastRows(): Array<Record<string, unknown>> {
  const fromReload = lastCall(LAYUI.tableReload)?.[1] as
    | { data?: Array<Record<string, unknown>> }
    | undefined
  if (fromReload?.data) return fromReload.data
  const fromRender = lastCall(LAYUI.tableRender)?.[0] as
    | { data?: Array<Record<string, unknown>> }
    | undefined
  return fromRender?.data ?? []
}

let fetchMock: ReturnType<typeof vi.fn>
/** jsdom **没有**实现 `URL.createObjectURL`（浏览器有）⇒ 由测试补上，否则「浏览图片」只剩降级分支。 */
const ORIGINAL_CREATE_OBJECT_URL = URL.createObjectURL
const ORIGINAL_REVOKE_OBJECT_URL = URL.revokeObjectURL
const createObjectURL = vi.fn(() => 'blob:http://localhost/pending.jpg')
const revokeObjectURL = vi.fn()

beforeEach(() => {
  document.body.innerHTML = ''
  MSGS.length = 0
  LAYUI.order.length = 0
  TABLE_HANDLERS.clear()
  unmount = undefined
  for (const fn of [
    LAYUI.msg, LAYUI.tableRender, LAYUI.tableReloadData, LAYUI.tableReload,
    LAYUI.layerOpen, LAYUI.layerClose, LAYUI.elementProgress,
    createObjectURL, revokeObjectURL,
  ]) fn.mockClear()
  Object.defineProperty(URL, 'createObjectURL', {
    configurable: true, writable: true, value: createObjectURL,
  })
  Object.defineProperty(URL, 'revokeObjectURL', {
    configurable: true, writable: true, value: revokeObjectURL,
  })
  vi.mocked(requestScreenshotTicket)
    .mockReset()
    .mockResolvedValue({
      screenshot_id: 11, key: 'demo/orders/x/y.jpg', upload_url: UPLOAD_URL, expires_in: 600,
    })
  vi.mocked(claimScreenshot).mockReset().mockResolvedValue({ id: 11 })
  // 已上传列表（`D-0153` ① 的本地预检靠它拿「已上传数量」）——默认空，用例按需改。
  // ⛔ 不给默认值的话 `fetchScreenshots` 回 `undefined`，`await` 后取 `.items` 会炸。
  vi.mocked(fetchScreenshots).mockReset().mockResolvedValue({ items: [], total: 0 })
  fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
    if (String(url).includes('order-screenshots-upload.html')) {
      return { ok: true, status: 200, text: async () => EVIDENCE } as unknown as Response
    }
    return { ok: true, status: 200 } as unknown as Response
  })
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  document.body.innerHTML = ''
  Object.defineProperty(URL, 'createObjectURL', {
    configurable: true, writable: true, value: ORIGINAL_CREATE_OBJECT_URL,
  })
  Object.defineProperty(URL, 'revokeObjectURL', {
    configurable: true, writable: true, value: ORIGINAL_REVOKE_OBJECT_URL,
  })
})

describe('挂载：标记与要点取值逐条来自证据件', () => {
  it('先挂 bootstrapIsland、再塞标记（E-0061 陷阱③；jest 替身里按**运行期调用序**判）', async () => {
    const root = await mountPage()
    expect(LAYUI.order).toEqual(['bootstrap'])
    // 证据件真的被塞进去了（不是「什么都没渲染也算过」）
    expect(root.querySelector('#drop_area')).not.toBeNull()
    expect(root.querySelector('#uploadInput')).not.toBeNull()
    expect(root.querySelector('#remark')).not.toBeNull()
  })

  it('A3 + D-0153 ④：「90 天」那半句**仍然不在**，而删掉的那半句已按证据件**逐字补回**', async () => {
    const root = await mountPage()
    // 「90 天」那半边**一个字都不许回来**（写了就是说假话：我们永久保留）。
    expect(root.textContent ?? '').not.toContain('90天')
    expect(root.textContent ?? '').not.toContain('90 天')
    expect(root.textContent ?? '').not.toContain('自动失效')
    expect(root.textContent ?? '').not.toContain('截图有效时间')
    // 补回的后半句：**逐字**（含全角标点），且带证据件里那个内联样式 `color:#808080`。
    expect(root.textContent ?? '').toContain(
      '提示：如果出现上传不了，请多尝试几次，或每次上传数量少一点；群聊二维码仅限一张，再次上传会覆盖原二维码！',
    )
    const restored = Array.from(root.querySelectorAll<HTMLElement>('p'))
      .find((p) => (p.textContent ?? '').includes('如果出现上传不了'))
    expect(restored, '证据件 L106-108 那个 <p> 应当按原标签补回').not.toBeUndefined()
    expect(restored!.getAttribute('style')).toBe('color:#808080')
    // 非空哨兵：本页确实渲染了那一带的其它文案（否则上面三条是恒真判据）
    expect(root.textContent ?? '').toContain('请先点击上传图片，再点击保存')
  })

  it('图片类型：两个单选、文案与 value 取自证据件、默认勾 0（订单截图）', async () => {
    const root = await mountPage()
    const radios = Array.from(root.querySelectorAll<HTMLInputElement>('input[name="imgType"]'))
    expect(radios.map((el) => [el.value, el.getAttribute('title'), el.checked])).toEqual([
      ['0', '订单截图', true],
      ['1', '群聊二维码', false],
    ])
  })

  it('图片备注：`maxlength=50` + 占位文案（逐字取自证据件）', async () => {
    const root = await mountPage()
    const remark = root.querySelector<HTMLInputElement>('#remark')
    expect(remark!.getAttribute('maxlength')).toBe('50')
    expect(remark!.getAttribute('placeholder')).toBe('可以给图片做个备注说明')
  })

  it('拖拽区文案、粘贴提示、保存按钮、底部橙字提示逐字取自证据件', async () => {
    const root = await mountPage()
    expect(root.querySelector('#drop_area')!.textContent).toContain('将文件拖到此处')
    expect(root.querySelector('.uploadTips')!.textContent).toBe('点击此处可粘贴图片')
    const save = root.querySelector<HTMLElement>('button[lay-filter="savebtn"]')!
    expect(save.className).toBe('layui-btn layui-btn-danger w-md')
    expect(save.textContent).toBe('点我将上传的图片提交保存')
    expect(save.getAttribute('style')).toBe('width:100%;margin-top:10px')
    expect(root.textContent ?? '')
      .toContain('提示：全部上传完成前，请勿关闭本界面。上传完成后请再浏览一次订单截图以确保全部上传成功！')
  })

  it('进度条标记与证据件一致（`lay-filter="progressBar"`、初始 `display:none`）', async () => {
    const root = await mountPage()
    const bar = root.querySelector<HTMLElement>('#progressBar')
    expect(bar!.getAttribute('lay-filter')).toBe('progressBar')
    expect(bar!.getAttribute('lay-showPercent')).toBe('yes')
    expect(bar!.style.display).toBe('none')
    expect(bar!.querySelector('.layui-progress-bar')!.getAttribute('lay-percent')).toBe('0%')
  })

  it('已上传表格：4 列（文件名称 / 大小 / 上传状态 / 操作 width:150）+ `limit:100`、`page:false`', async () => {
    await mountPage()
    const opts = lastCall(LAYUI.tableRender)?.[0] as {
      elem: string
      data: unknown[]
      limit: number
      page: boolean
      defaultToolbar: unknown[]
      height: string
      cols: Array<Array<{ field?: string; title?: string; width?: number; toolbar?: string }>>
    }
    expect(opts.elem).toBe('#tableList')
    expect(opts.data).toEqual([])
    expect(opts.limit).toBe(100)
    expect(opts.page).toBe(false)
    expect(opts.defaultToolbar).toEqual([])
    // 我们只做桌面后台 ⇒ 取对标的 PC 分支（`isPc ? 'full' : 160`）
    expect(opts.height).toBe('full')
    expect(opts.cols[0]!.map((c) => [c.field, c.title, c.width, c.toolbar])).toEqual([
      ['name', '文件名称', undefined, undefined],
      ['size', '大小', undefined, undefined],
      ['remark', '上传状态', undefined, undefined],
      [undefined, '操作', 150, '#baroperating'],
    ])
  })

  it('「上传状态」列的 templet 逐字 = 证据件里那句绿字', async () => {
    await mountPage()
    const opts = lastCall(LAYUI.tableRender)?.[0] as {
      cols: Array<Array<{ title?: string; templet?: (row: unknown) => string }>>
    }
    const status = opts.cols[0]!.find((c) => c.title === '上传状态')!
    expect(status.templet!({})).toBe('<span style="color:#5FB878">上传成功(等待保存)</span>')
    const size = opts.cols[0]!.find((c) => c.title === '大小')!
    expect(size.templet!({ size: 2 * 1024 * 1024 })).toBe('<span>2.00MB</span>')
  })

  it('操作列的两颗按钮在证据件模板里（`lay-event` = viewPic / delitem）', () => {
    expect(EVIDENCE).toContain('lay-event="viewPic"')
    expect(EVIDENCE).toContain('lay-event="delitem"')
  })
})

describe('直传三跳：ticket ⇒ PUT ⇒ 认领', () => {
  it('选文件 ⇒ 真的发起 ticket 请求（单号 / 扩展名 / 图片类型），并 PUT 到服务端给的 URL', async () => {
    const root = await mountPage()
    await chooseFiles(root, [makeFile('a.jpg')])

    expect(vi.mocked(requestScreenshotTicket).mock.calls[0]).toEqual([ORDER_NO, 'jpg', 0])
    const putCall = fetchMock.mock.calls.find((c) => c[0] === UPLOAD_URL)
    expect(putCall, '没有 PUT 到 upload_url').toBeDefined()
    expect((putCall![1] as RequestInit).method).toBe('PUT')
    // ⛔ 认领发生在「保存」那一步，不在上传那一刻（对标：先上传、再点保存）
    expect(vi.mocked(claimScreenshot)).not.toHaveBeenCalled()
  })

  it('拖拽落点同样走三跳（drag & drop）', async () => {
    const root = await mountPage()
    await dropFiles(root, [makeFile('drop.png', 'image/png')])
    expect(vi.mocked(requestScreenshotTicket).mock.calls[0]).toEqual([ORDER_NO, 'png', 0])
    expect(fetchMock.mock.calls.some((c) => c[0] === UPLOAD_URL)).toBe(true)
  })

  it('Ctrl+V 粘贴同样走三跳（只认 image/* 的剪贴板项）', async () => {
    const root = await mountPage()
    await pasteFiles(root, [makeFile('paste.jpg')])
    expect(vi.mocked(requestScreenshotTicket).mock.calls[0]).toEqual([ORDER_NO, 'jpg', 0])
    expect(fetchMock.mock.calls.some((c) => c[0] === UPLOAD_URL)).toBe(true)
  })

  it('超过 18 MB 的文件被**挡在 18MB 闸**（不是落到 5MB 闸）：钉只属于它那句 + 没进压缩 + 不发 ticket', async () => {
    const root = await mountPage()
    vi.mocked(compressImage).mockClear()
    await chooseFiles(root, [withSize(makeFile('huge.jpg'), 19 * 1024 * 1024)])

    expect(vi.mocked(requestScreenshotTicket)).not.toHaveBeenCalled()
    // ⛔ **不能只判 `includes('18MB')`**（原来的写法就是这条，是**假保证**）：5MB 闸的兜底句
    //    「图片都太大了，请把图片大小控制在单张18MB以内」**也含 `18MB`** ⇒ 把 18MB 那道闸整条删掉、
    //    让 19MB 落到 5MB 闸（压缩桩是直通 ⇒ `prepared.size` 仍 19MB > 5MB）时，旧判据**照样真**。
    // 判据一：只属于 18MB 闸的那句（`已自动过滤N张18MB以上的图片`，实现 :339）。5MB 闸那句是
    // `已自动过滤N张5MB以上的图片`、兜底句是「…18MB以内」，两者都不含这个子串。
    expect(MSGS).toContain('已自动过滤1张18MB以上的图片')
    // 判据二（独立于文案）：18MB 闸在**压缩之前** ⇒ 被它拦下的文件**一次都不该进压缩**。
    // 落到 5MB 闸就一定会先进 `compressImage`（那才轮到「压完仍超 5MB」被拒）。
    expect(vi.mocked(compressImage)).not.toHaveBeenCalled()
  })

  it('扩展名不在 jpg / jpeg / png ⇒ 挡在前端、不发 ticket，提示与后端同一句', async () => {
    const root = await mountPage()
    await chooseFiles(root, [makeFile('a.gif', 'image/gif')])
    expect(vi.mocked(requestScreenshotTicket)).not.toHaveBeenCalled()
    expect(MSGS).toContain('只支持 jpg / jpeg / png')
  })

  it('上传成功后把行插进表格（文件名称 / 大小 / 上传状态 / 隐藏的 screenshot_id）', async () => {
    const root = await mountPage()
    await chooseFiles(root, [makeFile('a.jpg')])
    const rows = lastRows()
    expect(rows).toHaveLength(1)
    expect(rows[0]!['name']).toBe('a.jpg')
    expect(rows[0]!['size']).toBe(3)
    expect(rows[0]!['screenshot_id']).toBe(11)
  })

  it('进度条推进：`element.progress(' + "'progressBar'" + ', ' + "'100%'" + ')` 且进度条显示出来', async () => {
    const root = await mountPage()
    await chooseFiles(root, [makeFile('a.jpg')])
    expect(LAYUI.elementProgress).toHaveBeenCalledWith('progressBar', '100%')
    expect(root.querySelector<HTMLElement>('#progressBar')!.style.display).not.toBe('none')
  })

  it('ticket 失败 ⇒ 原样弹后端 msg、表格不留行', async () => {
    vi.mocked(requestScreenshotTicket).mockRejectedValue(new Error('你没有上传订单图片的权限'))
    const root = await mountPage()
    await chooseFiles(root, [makeFile('a.jpg')])
    expect(MSGS).toContain('你没有上传订单图片的权限')
    expect(lastRows()).toEqual([])
  })
})

/**
 * `D-0153` ①：**每单上限 20 张截图**（口径：`img_type=0` 的行数，`pending` 与 `ready` 都算；
 * 群聊二维码不受此限）。后端在**签发与认领**两处拦、是权威；前端只做**本地预检**：
 * 已上传数量（本页挂载时从既有列表接口取）+ 待保存队列 > 20 ⇒ 直接提示同一句话、**零请求**。
 *
 * ⚠️ 判据钉**边界两侧**（E-0021）：20 张时加第 21 张必拦、19 张时加第 20 张必放行。
 * 只测一侧的话，把判据写成 `>=` / `>` 都不会红。
 */
describe('D-0153 ①：每单 20 张的本地预检（后端仍是权威）', () => {
  const LIMIT_TEXT = '单个订单最多上传20张截图，请先删除后再上传'

  it('已上传 20 张 ⇒ 第 21 张被拦下：零请求 + 与后端同一句话', async () => {
    vi.mocked(fetchScreenshots).mockResolvedValue({ items: shots(20), total: 20 })
    const root = await mountPage()
    const fetchBefore = fetchMock.mock.calls.length
    await chooseFiles(root, [makeFile('twenty-first.jpg')])

    expect(vi.mocked(requestScreenshotTicket)).not.toHaveBeenCalled()
    // 直传那一跳也没有（`fetch` 把证据件那一次算进去了 ⇒ 按增量判）。
    expect(fetchMock.mock.calls.length - fetchBefore).toBe(0)
    expect(MSGS).toContain(LIMIT_TEXT)
  })

  it('已上传 19 张 ⇒ 第 20 张**放行**（边界另一侧：认领不发，但 ticket + PUT 真的发生）', async () => {
    vi.mocked(fetchScreenshots).mockResolvedValue({ items: shots(19), total: 19 })
    const root = await mountPage()
    await chooseFiles(root, [makeFile('twentieth.jpg')])

    expect(vi.mocked(requestScreenshotTicket).mock.calls[0]).toEqual([ORDER_NO, 'jpg', 0])
    expect(fetchMock.mock.calls.some((call) => call[0] === UPLOAD_URL)).toBe(true)
    expect(MSGS).not.toContain(LIMIT_TEXT)
  })

  it('已上传 20 张**群聊二维码**不占那 20 个名额 ⇒ 订单截图仍放行（口径：只数 `img_type=0`）', async () => {
    vi.mocked(fetchScreenshots).mockResolvedValue({ items: shots(20, 1), total: 20 })
    const root = await mountPage()
    await chooseFiles(root, [makeFile('still-ok.jpg')])

    expect(vi.mocked(requestScreenshotTicket).mock.calls[0]).toEqual([ORDER_NO, 'jpg', 0])
    expect(MSGS).not.toContain(LIMIT_TEXT)
  })

  /**
   * ⭐ **I-2（2026-09-25 评审修复）：本会话里已传的群聊二维码不占那 20 个名额。**
   *
   * 背景（独立评审，jsdom 真交互）：`PendingRow` 原先不记 `img_type`，而预检拿
   * `uploadedCount + rows.length` 当已上传数 ⇒ 「1 张二维码 + 20 张订单截图」只发 19 张票据
   * （第 20 张被前端拦下、还弹了后端那句「最多 20 张」），而后端本应放行 20 张
   * （`D-0153` ①：群聊二维码不占这 20 个名额）⇒ **误判成更严**。
   * 这条钉「同一会话里先传 1 张二维码，之后的 20 张订单截图票据都发出、且不弹上限提示」。
   */
  it('本会话已传 1 张群聊二维码 ⇒ 订单截图仍能占满 20 个名额（20 张票据都发出）', async () => {
    const root = await mountPage()
    // ① 先以 imgType=1 传 1 张二维码（进本页的待保存队列 `rows`）。
    root.querySelector<HTMLInputElement>('input[name="imgType"][value="1"]')!.checked = true
    await chooseFiles(root, [makeFile('qr.png', 'image/png')])
    expect(vi.mocked(requestScreenshotTicket).mock.calls.length).toBe(1)
    // ② 再切回「订单截图」，一次挑 20 张。
    root.querySelector<HTMLInputElement>('input[name="imgType"][value="0"]')!.checked = true
    vi.mocked(requestScreenshotTicket).mockClear()
    await chooseFiles(root, Array.from({ length: 20 }, (_, i) => makeFile(`shot-${i}.jpg`)))
    // 20 张 ⇒ 逐个 `await compressImage` 的那一圈微任务不止 `flush()` 的 12 拍，多排几次。
    for (let i = 0; i < 5; i += 1) await flush()

    // 判据：20 张票据**全部**发出（变异：不过滤 `rows` ⇒ 只发 19 张 ⇒ 红）。
    expect({ tickets: vi.mocked(requestScreenshotTicket).mock.calls.length }).toEqual({ tickets: 20 })
    // 且**不**该弹「最多 20 张」（前端预检没触发）。
    expect(MSGS).not.toContain(LIMIT_TEXT)
  })

  it('后端拒了 ⇒ **原样显示它的 msg**（前端预检没触发：列表是空的 ⇒ 那句话只能来自后端）', async () => {
    vi.mocked(fetchScreenshots).mockResolvedValue({ items: [], total: 0 })
    vi.mocked(requestScreenshotTicket).mockRejectedValue(new Error(LIMIT_TEXT))
    const root = await mountPage()
    await chooseFiles(root, [makeFile('race.jpg')])

    // 前置哨兵：预检**没有**拦（票据请求真的发了）——这条区分了「后端拒」与「前端预检拦」两条路。
    expect(vi.mocked(requestScreenshotTicket)).toHaveBeenCalled()
    expect(MSGS).toContain(LIMIT_TEXT)
  })
})

describe('保存（认领）：闸门与逐行落库', () => {
  it('一张都没传就点保存 ⇒ 提示照证据件原文，且零请求', async () => {
    const root = await mountPage()
    await clickSave(root)
    expect(MSGS).toContain('请先上传图片再进行保存')
    expect(vi.mocked(claimScreenshot)).not.toHaveBeenCalled()
  })

  it('图片类型 = 群聊二维码、却有 2 张 ⇒ 拒绝，文案逐字取自证据件，零认领', async () => {
    const root = await mountPage()
    // ⚠️ 这 2 张必须在单选**已经是**「群聊二维码」时上传：`img_type` 在上传那一刻定下
    //    （有意不同第 4 条）⇒ 只有真的传了 2 张二维码，这张闸才该拦
    //    （混着数会误伤订单截图，见下面 I-2 的那条判据）。
    root.querySelector<HTMLInputElement>('input[name="imgType"][value="1"]')!.checked = true
    await chooseFiles(root, [makeFile('a.jpg'), makeFile('b.jpg')])
    await clickSave(root)
    expect(MSGS).toContain('群聊二维码仅限一张，再次上传会覆盖原二维码！')
    expect(vi.mocked(claimScreenshot)).not.toHaveBeenCalled()
  })

  /**
   * ⭐ **I-2 的第二半（2026-09-25 评审修复）：保存时那张「仅限一张」的闸也按 `img_type` 数。**
   *
   * 背景（读码）：`imgType === GROUP_QR_TYPE && rows.length > 1` 把**两类图**混着数 ⇒
   * 「1 张二维码 + N 张订单截图」时，以 `imgType=1` 保存会被**误导性地**挡住
   * ——提示说「群聊二维码仅限一张」，而实际只有一张二维码。
   * 终态由后端行锁 + 淘汰收口，但提示是错的。
   */
  it('1 张二维码 + 2 张订单截图 ⇒ 以群聊二维码保存**不**报「仅限一张」（认领照常发生）', async () => {
    const root = await mountPage()
    // ① 先传 2 张订单截图（imgType 默认 0）。
    await chooseFiles(root, [makeFile('a.jpg'), makeFile('b.jpg')])
    // ② 再以 imgType=1 传 1 张二维码。
    root.querySelector<HTMLInputElement>('input[name="imgType"][value="1"]')!.checked = true
    await chooseFiles(root, [makeFile('qr.png', 'image/png')])
    // 非空哨兵：三类图都还在待保存列表里（否则下面的「不报错」可能只是空列表上的恒真）。
    expect(lastRows()).toHaveLength(3)
    MSGS.length = 0
    // 保存那一刻单选停在「群聊二维码」（imgType=1）。
    await clickSave(root)

    // 变异：不按 `img_type` 过滤（数 `rows.length`）⇒ 3 > 1 ⇒ 弹这句 + 零认领 ⇒ 红。
    expect(MSGS).not.toContain('群聊二维码仅限一张，再次上传会覆盖原二维码！')
    expect({ claims: vi.mocked(claimScreenshot).mock.calls.length }).toEqual({ claims: 3 })
  })

  it('保存 ⇒ 逐行认领（screenshot_id / original_name / 备注取备注框的值）', async () => {
    const root = await mountPage()
    await chooseFiles(root, [makeFile('a.jpg')])
    root.querySelector<HTMLInputElement>('#remark')!.value = '测试备注甲'
    await clickSave(root)
    expect(vi.mocked(claimScreenshot).mock.calls[0]).toEqual([
      ORDER_NO, { screenshot_id: 11, original_name: 'a.jpg', remark: '测试备注甲' },
    ])
  })

  it('保存成功后清空待保存列表（避免同一行被认领两次）', async () => {
    const root = await mountPage()
    await chooseFiles(root, [makeFile('a.jpg')])
    await clickSave(root)
    expect(lastRows()).toEqual([])
  })

  it('群聊二维码单选 ⇒ ticket 的 `img_type` 传 1', async () => {
    const root = await mountPage()
    root.querySelector<HTMLInputElement>('input[name="imgType"][value="1"]')!.checked = true
    await chooseFiles(root, [makeFile('qr.png', 'image/png')])
    expect(vi.mocked(requestScreenshotTicket).mock.calls[0]).toEqual([ORDER_NO, 'png', 1])
  })

  /**
   * ⭐ **M-1（2026-09-25 评审修复）：`rows.length > 1` 的「恰好 1 张」正边界。**
   *
   * 在册用例只有「2 张 ⇒ 拒」与「单选 ⇒ ticket 的 `img_type=1`」两条 —— 后者**没点保存**，
   * 所以「1 张群聊二维码点保存会怎样」此前零覆盖：把闸改成 `rows.length >= 1`（或把 `> 1`
   * 写成 `> 0`）不会有任何测试变红，而后果是**用户永远存不下群聊二维码**。
   */
  it('⛔ 恰好 1 张 + 群聊二维码 ⇒ 闸放行、**认领真的发生**（`rows.length > 1` 的正边界）', async () => {
    const root = await mountPage()
    root.querySelector<HTMLInputElement>('input[name="imgType"][value="1"]')!.checked = true
    await chooseFiles(root, [makeFile('qr.png', 'image/png')])
    await clickSave(root)
    // 认领打的是那一行（`screenshot_id` / `original_name` / 空备注）。
    expect(vi.mocked(claimScreenshot).mock.calls[0]).toEqual([
      ORDER_NO, { screenshot_id: 11, original_name: 'qr.png', remark: '' },
    ])
    // 反例：那张「仅限一张」的提示**一个字都不该出现**（出现即说明闸把合法的 1 张也拒了）。
    expect(MSGS).not.toContain('群聊二维码仅限一张，再次上传会覆盖原二维码！')
  })
})

describe('已上传表格的两颗行内按钮（浏览图片 / 删除）', () => {
  /**
   * 行内两颗按钮在对标里是 `#baroperating` 模板、走 layui 表格的 `tool(tableList)` 事件
   * （`uploadpicnew.js:362-416`）——所以这里驱动的也是**同一个通道**（`obj.data` 才有那一行）。
   */
  const toolHandler = (): ((obj: { event: string; data: unknown }) => void) | undefined =>
    TABLE_HANDLERS.get('tool(tableList)') as
    | ((obj: { event: string; data: unknown }) => void)
    | undefined

  it("第 1 行：点「浏览图片」⇒ 开层，参数逐字取自证据件（`type:1` / `area:['85%']` / `<img 580×420>`）", async () => {
    const root = await mountPage()
    await chooseFiles(root, [makeFile('a.jpg')])
    const row = lastRows()[0]
    LAYUI.layerOpen.mockClear()
    toolHandler()?.({ event: 'viewPic', data: row })
    const opts = LAYUI.layerOpen.mock.calls[0]?.[0] as Record<string, unknown>
    expect(opts['type']).toBe(1)
    expect(opts['title']).toBe(false)
    expect(opts['closeBtn']).toBe(0)
    expect(opts['area']).toEqual(['85%'])
    expect(opts['skin']).toBe('layui-layer-nobg')
    expect(opts['shadeClose']).toBe(true)
    expect(String(opts['content'])).toContain('width="580px" height="420px"')
    expect(String(opts['content'])).toContain('target="_blank"')
  })

  it('第 2 行：点「删除」⇒ 只把那行从待保存列表移除（**零请求**：pending 对象交给 24h 清理），提示照证据件', async () => {
    const root = await mountPage()
    await chooseFiles(root, [makeFile('a.jpg')])
    const before = fetchMock.mock.calls.length
    MSGS.length = 0
    toolHandler()?.({ event: 'delitem', data: lastRows()[0] })
    expect(lastRows()).toEqual([])
    expect(MSGS).toContain('删除成功！')
    expect(fetchMock.mock.calls.length).toBe(before)
    expect(vi.mocked(claimScreenshot)).not.toHaveBeenCalled()
  })
})

/**
 * ⭐ **I-3（2026-09-25 评审修复）：`releasePreview`「三条路都释放」的判据。**
 *
 * 背景（独立评审）：`URL.revokeObjectURL` 在本文件里被调用 6 次，但**全在 `beforeEach` /
 * `afterEach` 的替身装卸里**——没有一处 `expect`。把 `releasePreview` 从三条路里任意删掉，
 * 全仓 vitest 照样全绿。后果是**静默内存泄漏**：`createObjectURL` 出来的 blob 会被钉住
 * 直到页面卸载，而弹层可能来回开关。
 *
 * 三条路（实现行号）：认领成功（:426）/ 行内删除（:459）/ 卸载（:496，`rows.forEach`）。
 * 每条各有一条独立断言 —— **不是**一条 `toHaveBeenCalled()` 打天下：那样删掉任意两条也不会红。
 */
describe('I-3：本地对象 URL 的三条释放路径（每条各一条独立判据）', () => {
  /** 行内「删除」走 layui 的 `tool(tableList)` 通道（同「两颗行内按钮」那组的取法）。 */
  const toolHandler = (): ((obj: { event: string; data: unknown }) => void) | undefined =>
    TABLE_HANDLERS.get('tool(tableList)') as
    | ((obj: { event: string; data: unknown }) => void)
    | undefined

  it('路 1 · 认领成功 ⇒ 释放该行的 blob URL', async () => {
    const root = await mountPage()
    await chooseFiles(root, [makeFile('a.jpg')])
    revokeObjectURL.mockClear()
    await clickSave(root)

    expect(vi.mocked(claimScreenshot)).toHaveBeenCalled()
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:http://localhost/pending.jpg')
  })

  it('路 2 · 行内删除 ⇒ 释放那一行的 blob URL', async () => {
    const root = await mountPage()
    await chooseFiles(root, [makeFile('a.jpg')])
    revokeObjectURL.mockClear()
    toolHandler()?.({ event: 'delitem', data: lastRows()[0] })

    expect(lastRows()).toEqual([])
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:http://localhost/pending.jpg')
  })

  it('路 3 · 卸载弹层 ⇒ 释放**还留在**待保存列表里的行', async () => {
    const root = await mountPage()
    await chooseFiles(root, [makeFile('a.jpg')])
    revokeObjectURL.mockClear()
    // 非空哨兵：先把「确实还有一张待保存的图」钉住，否则「卸载时释放」可能只是空列表上的恒真。
    expect(lastRows()).toHaveLength(1)

    unmount?.()

    expect(revokeObjectURL).toHaveBeenCalledWith('blob:http://localhost/pending.jpg')
  })
})
