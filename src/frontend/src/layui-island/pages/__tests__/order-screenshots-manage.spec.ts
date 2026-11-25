// @vitest-environment jsdom
/**
 * 【管理订单截图】弹层（island key `order-screenshots-manage`）的**真交互**页测试。
 *
 * 任务书 Task 8 / spec §8：`200×160` 小图、全选、批量删除、打包下载；
 * 删除确认文案**逐字** `确定要删除吗？提示：一旦删除，将不可恢复！`；
 * ⚠️ A3：对标那句「距离上传时间超过 90 天的将会被自动删除…」**不写**（我们永久保留）。
 *
 * 判据一律写**字面值**（不拿页面导出的常量当期望值，E-0024 的自指断言）。
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const HERE = path.dirname(fileURLToPath(import.meta.url))
/** 证据件原文（`public/replica/order-screenshots-manage.html`）。 */
const EVIDENCE = fs.readFileSync(
  path.resolve(HERE, '../../../../public/replica/order-screenshots-manage.html'),
  'utf8',
)

/** 合成单号（20 位数字）。 */
const ORDER_NO = '20260101000000000001'
/** 后端 `GET …/screenshots` 的载荷形状（`screenshot_views.ScreenshotCollectionView.list_items`）。 */
const ITEMS = [
  {
    id: 21, img_type: 0, url: 'https://cos.example.invalid/demo/orders/x/a.jpg',
    original_name: 'a.jpg', size_bytes: 233, remark: '测试备注甲',
    uploaded_by: 1, created_at: '2026-01-01T10:20:30+08:00',
  },
  {
    id: 22, img_type: 1, url: 'https://cos.example.invalid/demo/orders/x/qr.png',
    original_name: 'qr.png', size_bytes: 512, remark: '',
    uploaded_by: 1, created_at: '2026-01-01T11:00:00+08:00',
  },
]

const { MSGS, LAYUI } = vi.hoisted(() => {
  const msg = vi.fn()
  const confirm = vi.fn()
  const open = vi.fn()
  const photos = vi.fn()
  return {
    MSGS: [] as string[],
    LAYUI: {
      msg, confirm, open, photos,
      /** `bootstrap` = 挂样式，`artifact` = 取证据件（挂载顺序的运行期证据）。 */
      order: [] as string[],
      form: { render: vi.fn(), on: vi.fn() },
      table: { render: vi.fn(), reload: vi.fn(), reloadData: vi.fn(), on: vi.fn() },
      layer: { msg, confirm, open, close: vi.fn(), photos, load: vi.fn() },
      element: { init: vi.fn(), progress: vi.fn() },
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
  screenshotZipUrl: vi.fn(
    (orderNo: string) => `/api/business/orders/${orderNo}/screenshots/zip`,
  ),
}))

import { deleteScreenshot, fetchScreenshots } from '@/api/screenshots'
import page, { itemHtml } from '../order-screenshots-manage'

async function flush(): Promise<void> {
  for (let i = 0; i < 12; i += 1) await Promise.resolve()
}

async function mountPage(orderNo = ORDER_NO): Promise<HTMLElement> {
  window.history.replaceState(
    {}, '',
    `/layui-island/page.html?page=order-screenshots-manage&order_no=${orderNo}`,
  )
  const root = document.createElement('div')
  document.body.appendChild(root)
  await page.mount(root, { onNavigate: vi.fn() })
  await flush()
  return root
}

/** 小图 `<li>` 的列表（DOM 顺序）。 */
const items = (root: HTMLElement): HTMLElement[] =>
  Array.from(root.querySelectorAll<HTMLElement>('.imgList > li'))

/** 勾选第 n 张（`input[name=ck]`）。 */
function check(root: HTMLElement, index: number): void {
  const box = items(root)[index]!.querySelector<HTMLInputElement>('input[name="ck"]')!
  box.checked = true
}

let fetchMock: ReturnType<typeof vi.fn>
let openSpy: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  document.body.innerHTML = ''
  MSGS.length = 0
  LAYUI.order.length = 0
  for (const fn of [LAYUI.msg, LAYUI.confirm, LAYUI.open, LAYUI.photos]) fn.mockClear()
  vi.mocked(fetchScreenshots).mockReset().mockResolvedValue({ items: ITEMS, total: 2 })
  vi.mocked(deleteScreenshot).mockReset().mockResolvedValue(null)
  fetchMock = vi.fn(async (url: string) => {
    if (String(url).includes('order-screenshots-manage.html')) {
      return { ok: true, status: 200, text: async () => EVIDENCE } as unknown as Response
    }
    return { ok: true, status: 200 } as unknown as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  openSpy = vi.spyOn(window, 'open').mockImplementation(() => null)
})

afterEach(() => {
  document.body.innerHTML = ''
  openSpy.mockRestore()
})

it('缩略图用于列表，大图仍使用原链接，群码回退原图', () => {
  const root = document.createElement('div')
  root.innerHTML = itemHtml({ ...ITEMS[0]!, thumbnail_url: 'https://example.invalid/thumb?a=1&b=2' })
  const image = root.querySelector('img')!
  expect(image.getAttribute('src')).toBe('https://example.invalid/thumb?a=1&b=2')
  expect(image.getAttribute('layer-src')).toBe(ITEMS[0]!.url)
  expect(image.getAttribute('loading')).toBe('lazy')
  root.innerHTML = itemHtml(ITEMS[1]!)
  expect(root.querySelector('img')!.getAttribute('src')).toBe(ITEMS[1]!.url)
})

describe('挂载与列表渲染', () => {
  it('先挂 bootstrapIsland、再塞标记，随后按单号取列表', async () => {
    const root = await mountPage()
    expect(LAYUI.order).toEqual(['bootstrap'])
    expect(vi.mocked(fetchScreenshots).mock.calls[0]).toEqual([ORDER_NO])
    expect(root.querySelector('.imgList')).not.toBeNull()
  })

  it('小图尺寸 `200×160` 与其它页内样式逐字留在注入的标记里（证据件原值）', async () => {
    const root = await mountPage()
    const style = root.querySelector('style')?.textContent ?? ''
    expect(style).toMatch(/li img\s*\{[^}]*width:\s*200px/)
    expect(style).toMatch(/height:\s*160px/)
    expect(style).toMatch(/li\s*\{[^}]*height:\s*248px/)
  })

  it('A3：那句「距离上传时间超过 90 天的将会被自动删除…」**整句不写**', async () => {
    const root = await mountPage()
    expect(root.textContent ?? '').not.toContain('90天')
    expect(root.textContent ?? '').not.toContain('90 天')
    expect(root.textContent ?? '').not.toContain('自动删除')
    // 非空哨兵：本页确实渲染了（否则上面三条恒真）
    expect(root.textContent ?? '').toContain('全选')
  })

  it('每张图渲染成 `<li>`：`img` 用服务端签发的 url、备注进 `.picnote`、删除带 `data-id`、勾选框 value = id', async () => {
    const root = await mountPage()
    const rows = items(root)
    expect(rows).toHaveLength(2)
    expect(rows[0]!.querySelector('img')!.getAttribute('src')).toBe(ITEMS[0]!.url)
    expect(rows[0]!.querySelector('.picnote span')!.getAttribute('title')).toBe('测试备注甲')
    expect(rows[0]!.querySelector<HTMLElement>('.del')!.getAttribute('data-id')).toBe('21')
    expect(rows[0]!.querySelector<HTMLInputElement>('input[name="ck"]')!.value).toBe('21')
    // 时间形如 `2026/1/1 10:20:30`（对标 `picmange` 页的渲染形），不是原样 ISO
    expect(rows[0]!.textContent ?? '').toMatch(/\d{4}\/\d{1,2}\/\d{1,2} \d{2}:\d{2}:\d{2}/)
  })

  it("大图浏览走证据件里那条 `layer.photos({photos:'.imgList', anim:5})`", async () => {
    await mountPage()
    expect(LAYUI.photos.mock.calls[0]?.[0]).toEqual({ photos: '.imgList', anim: 5 })
  })
})

describe('全选 / 批量删除 / 单张删除', () => {
  it('点「全选」⇒ 每张的 `input[name=ck]` 都跟着勾上', async () => {
    const root = await mountPage()
    const all = root.querySelector<HTMLInputElement>('#checkall')!
    expect(items(root).map((li) => li.querySelector<HTMLInputElement>('input[name="ck"]')!.checked))
      .toEqual([false, false])
    all.click()
    expect(items(root).map((li) => li.querySelector<HTMLInputElement>('input[name="ck"]')!.checked))
      .toEqual([true, true])
  })

  it('一张都没勾就点批量删除 ⇒ 提示照证据件原文，且**一个删除请求都不发**', async () => {
    const root = await mountPage()
    root.querySelector<HTMLElement>('button[lay-filter="delbtns"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
    await flush()
    expect(MSGS).toContain('请勾选图片再批量删除')
    expect(LAYUI.confirm).not.toHaveBeenCalled()
    expect(vi.mocked(deleteScreenshot)).not.toHaveBeenCalled()
  })

  it('批量删除的确认文案**逐字**；确认后逐张调删除端点并重取列表', async () => {
    const root = await mountPage()
    check(root, 0)
    check(root, 1)
    root.querySelector<HTMLElement>('button[lay-filter="delbtns"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
    await flush()
    expect(LAYUI.confirm.mock.calls[0]?.[0]).toBe('确定要删除吗？提示：一旦删除，将不可恢复！')
    expect(LAYUI.confirm.mock.calls[0]?.[1]).toEqual({ btn: ['确定', '取消'] })
    // 确认回调（第 3 个实参）——真实的删除发生在它里面
    const cb = LAYUI.confirm.mock.calls[0]?.[2] as (() => void) | undefined
    cb?.()
    await flush()
    expect(vi.mocked(deleteScreenshot).mock.calls.map((c) => c[0])).toEqual([21, 22])
    expect(vi.mocked(fetchScreenshots).mock.calls.length).toBe(2)
  })

  it('单张「删除」：同一句确认文案 ⇒ 只删那一张', async () => {
    const root = await mountPage()
    items(root)[1]!.querySelector<HTMLElement>('.del')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
    await flush()
    expect(LAYUI.confirm.mock.calls[0]?.[0]).toBe('确定要删除吗？提示：一旦删除，将不可恢复！')
    const cb = LAYUI.confirm.mock.calls[0]?.[2] as (() => void) | undefined
    cb?.()
    await flush()
    expect(vi.mocked(deleteScreenshot).mock.calls.map((c) => c[0])).toEqual([22])
  })
})

describe('打包下载', () => {
  it('点「打包下载」⇒ `window.open` 到 zip 端点（本单号）', async () => {
    const root = await mountPage()
    root.querySelector<HTMLElement>('button[lay-filter="importBtn"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
    await flush()
    expect(openSpy).toHaveBeenCalledWith(
      `/api/business/orders/${ORDER_NO}/screenshots/zip`, '_blank',
    )
  })

  it('两颗按钮的标记与证据件一致（`lay-filter` = importBtn / delbtns、配色与尺寸类）', async () => {
    const root = await mountPage()
    const zip = root.querySelector<HTMLElement>('button[lay-filter="importBtn"]')!
    expect(zip.className).toBe('layui-btn layui-btn-sm layui-btn-normal')
    // 打包下载那颗在证据件里带一个 **stock** 图标（`&#xe601;`）⇒ 照抄保留；
    // 图标字形是私用区字符，所以正文用 `toContain` 而不是全等。
    expect(zip.querySelector('i.layui-icon')).not.toBeNull()
    expect(zip.textContent).toContain('打包下载')
    const batch = root.querySelector<HTMLElement>('button[lay-filter="delbtns"]')!
    expect(batch.className).toBe('layui-btn layui-btn-sm layui-btn-danger')
    expect(batch.textContent).toBe('批量删除')
  })
})

describe('重渲染后的勾选状态（换承载方式才实体化的那条）', () => {
  it('批量删除后重取列表 ⇒ `#checkall` 跟着复位（不留「全选还勾着、底下一张没勾」的假状态）', async () => {
    const root = await mountPage()
    const all = root.querySelector<HTMLInputElement>('#checkall')!
    all.click()
    expect(all.checked).toBe(true)
    // 第 2 次 `fetchScreenshots` 回一个空列表（都删光了）
    vi.mocked(fetchScreenshots).mockResolvedValue({ items: [], total: 0 })
    check(root, 0)
    root.querySelector<HTMLElement>('button[lay-filter="delbtns"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
    const cb = LAYUI.confirm.mock.calls[0]?.[2] as (() => void) | undefined
    cb?.()
    await flush()
    expect(all.checked).toBe(false)
  })
})
