// @vitest-environment jsdom
import { readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import systemSystemsetting from '@/layui-island/pages/system-systemsetting'
import type { IslandContext } from '@/layui/island'

/**
 * **I-1 的跨页结构网**：`artifactPage` 的 catch-all（「点了就提示尚未开放」）对**页签标题**的
 * 口径**分两类**，不许一刀切（2026-09-22 第五轮收紧）：
 *
 *  ① **有面板的页签**（`.layui-tab-content` 里有 `.layui-tab-item`）⇒ 页签切换是纯客户端
 *     **结构行为**、不是功能，拦了并 `stopPropagation` 会让第二个面板永远看不到 ⇒ 点每个页签
 *     **零 toast**。
 *     ⚠️ **2026-09-24 我的资料线更正**：`user-myinfo` 改成**自挂载**（不再调 `artifactPage(`，
 *     spec §6）⇒ 它已从下面的源码推导集合里**掉出**。它原来守的那条判据（「点每个页签标题 ⇒ 零 toast」）
 *     由 `layui-island/pages/__tests__/user-myinfo.spec.ts` 的第 ⑥ 条在新实现上接住
 *     ——⛔ 判据没有随页面一起消失。
 *     ⚠️ **2026-10-10 系统开关线（D-0223）再改准**：`system-systemsetting` 也改成**自挂载**
 *     （自取证据件 + 自建「尚未开放」总闸，只接 SK32 一条）⇒ 它同样从推导集合里掉出。
 *     它守的两条判据都由
 *     `layui-island/pages/__tests__/system-systemsetting.spec.ts` 在新实现上接住：
 *     「点页签标题 ⇒ 零 toast」（I-1 回归）与「点真控件 ⇒ 仍弹」（阳性对照）。
 *  ② **没有面板的页签**（只有 `<ul class="layui-tab-title">`，**没有** `.layui-tab-content`）⇒
 *     `island.ts` 的页签处理器在 `if (!title || !content) return` 就退出、**不接管这种页签**
 *     ⇒ `artifactPage` 的 catch-all 是该页**唯一**的反馈 ⇒ 点每个页签**必须弹**「尚未开放」。
 *     ⚠️ **2026-11-10 第二批（客户线）**：唯一的 ② 类页 `customer-hylevelsetting` 已随
 *     「会员等级设置」整页删除（业务方拍板，REPLICA_SKIP 留痕）⇒ 两类**今天都为空集**。
 *
 * ⚠️ 为什么这一轮要把它写进网里：上一轮的修法是**全局**排除
 * `target.closest('.layui-tab-title')`（只要有页签标题就不弹）——它连 ② 那类页面唯一的反馈
 * 也一起吞掉了（实测：当时唯一的 ② 页点页签零 toast，而文件头却写「点了提示尚未开放」）。
 * 现在的判据是 `island.ts::islandTabTarget`（**唯一出处**，`initIslandElements` 调的是同一个）。
 *
 * ⚠️ 覆盖集是**显式枚举 + 源扫描对拍**（E-0065：覆盖/豁免类的判据必须显式枚举）：
 * 下面按源码算出「所有用 `artifactPage(` 渲染、且证据件带活页签」的页，断言它恰好是**空集**；
 * 新增一页会让这条**变红**，逼人把新页纳入这份清单（① 类零 toast、② 类必须弹，两类二选一）。
 */
const HERE = path.dirname(fileURLToPath(import.meta.url))
const PAGES_DIR = path.resolve(HERE, '../../layui-island/pages')
const REPLICA_DIR = path.resolve(HERE, '../../../public/replica')

const readIf = (file: string): string => readFileSync(file, 'utf8')

const REPLICA_FILES = readdirSync(REPLICA_DIR).filter((f) => f.endsWith('.html'))

/**
 * 全站带**活页签**（`<ul … layui-tab-title>`）的 replica 证据件（按内容判定，不靠文件名猜）。
 * 删掉 `customer-hylevelsetting.html`（2026-11-10 第二批）后现为 **7 个文件、11 个块**。
 * 语料变了这两个字面量会红，要人来核。
 * 订单详情和第三方平台订单管理是手写渲染器，不进 `TAB_PAGES`
 * （那一类只认 `artifactPage(`）。它们的「尚未开放」不走 `artifactPage` 的兜底。
 */
const TAB_TITLE_HTML: string[] = REPLICA_FILES.filter((f) =>
  /<ul[^>]*layui-tab-title/.test(readIf(path.join(REPLICA_DIR, f))),
).sort()

const TAB_TITLE_BLOCKS = REPLICA_FILES.reduce(
  (n, f) => n + (readIf(path.join(REPLICA_DIR, f)).match(/<ul[^>]*layui-tab-title/g)?.length ?? 0),
  0,
)

/** `artifactPage(` 页 → 它渲染的证据件（从源码里的 `replica/xxx.html` 字面量取）。 */
const ARTIFACT_PAGE_HTML: string[] = readdirSync(PAGES_DIR)
  .filter((f) => f.endsWith('.ts') && !f.includes('.spec.'))
  .flatMap((f) => {
    const src = readIf(path.join(PAGES_DIR, f))
    if (!src.includes('artifactPage(')) return []
    return Array.from(src.matchAll(/replica\/([A-Za-z0-9_-]+\.html)/g), (m) => m[1]!)
  })

/**
 * 「用 artifactPage 渲染 **且** 证据件带活页签」的页 = 受 I-1 影响的全集。
 * ⚠️ `user-myinfo` / `system-systemsetting` 改自挂载后不再调 `artifactPage(`（见文件头），
 * `customer-hylevelsetting` 页已删 ⇒ **今天为空集**（新增页会在这里露头，必须显式登记）。
 */
const TAB_PAGES: string[] = Array.from(new Set(ARTIFACT_PAGE_HTML))
  .filter((html) => TAB_TITLE_HTML.includes(html))
  .sort()

/** ① 有可切面板（`.layui-tab-content` 且其中有 `.layui-tab-item`）⇒ catch-all **不弹**。 */
const PANEL_PAGES: string[] = TAB_PAGES.filter((html) => {
  const src = readIf(path.join(REPLICA_DIR, html))
  return src.includes('layui-tab-content') && src.includes('layui-tab-item')
})

/** ② 没有面板 ⇒ island 不接管 ⇒ catch-all **必须弹**（既定行为）。 */
const NO_PANEL_PAGES: string[] = TAB_PAGES.filter((html) => !PANEL_PAGES.includes(html))

/**
 * 可挂载页的真实模块。两类都空了，但**阳性对照仍要点一个真页**：
 * `system-systemsetting` 虽不在 `TAB_PAGES` 里（自挂载），它页内的真控件点下去
 * 仍会被 catch-all 拦成「尚未开放」——这条钉住 catch-all 在本 harness 里**真的会弹**。
 */
const MOUNTS: Record<string, { name: string; mount: (r: HTMLElement, c: IslandContext) => Promise<() => void> }> = {
  'system-systemsetting.html': { name: 'system-systemsetting', mount: systemSystemsetting.mount },
}

/**
 * layui 的**可控替身**（jsdom 里加载不了真 layui）。`layer.msg` 就是「尚未开放」的出口。
 */
const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  return {
    msg,
    // ⭐ 系统开关线（D-0223）：`system-systemsetting` 改自挂载后，挂载会调
    //    `form.on('submit(save2btn)')` 与 `form.val('sform', …)`（本文件只测「尚未开放」
    //    收口，这两个替身记不记调用都无所谓，但**缺了会 TypeError**）。
    form: { render: vi.fn(), on: vi.fn(), val: vi.fn() },
    table: { render: vi.fn(), reload: vi.fn() },
    layer: { msg },
    element: { init: vi.fn() },
  }
})

// ⭐ 系统开关线（D-0223）：新页挂载即取开关列表（真模块会打后端 ⇒ 这里必须换成替身，
//    否则它会拿到「证据件 HTML 当 JSON」的解析错误）。
vi.mock('@/api/systemSwitches', () => ({
  fetchSystemSwitches: vi.fn(async () => ({
    items: [
      {
        key: 'forbid_duplicate_source_order_no',
        name: '录单时禁止录入相同的来源单号',
        value: 'N' as const,
        default: 'N' as const,
        replica_sk: 'SK32',
      },
    ],
  })),
  updateSystemSwitch: vi.fn(),
}))

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return {
    ...actual,
    bootstrapIsland: (async () => LAYUI) as unknown as typeof actual.bootstrapIsland,
    // 本文件只测「尚未开放」收口；页签类名切换由 `emp-emplist.spec.ts` 在真件上驱动。
    // ⚠️ 这里把 `initIslandElements` 换成 no-op ⇒ 被测的判据**只有** `artifactPage` 的 catch-all
    //    （它用的是真 `islandTabTarget`，因为 `importOriginal` 保留了其余导出）。
    initIslandElements: vi.fn(),
  }
})

/** 挂一页真模块：证据件走全局 fetch（真文件），layui 走替身。 */
async function mountPage(html: string): Promise<{ root: HTMLElement; cleanup: () => void }> {
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async () =>
        ({ ok: true, status: 200, text: async () => readIf(path.join(REPLICA_DIR, html)) }) as unknown as Response,
    ),
  )
  const root = document.createElement('div')
  document.body.appendChild(root)
  const cleanup = await MOUNTS[html]!.mount(root, {} as unknown as IslandContext)
  return { root, cleanup }
}

/** 把一次 click 派发到 `el`，返回 `layer.msg` 收到的所有 message。 */
function clickAndCollect(el: Element): string[] {
  LAYUI.msg.mockClear()
  el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  return LAYUI.msg.mock.calls.map((c) => String(c[0]))
}

beforeEach(() => {
  vi.clearAllMocks()
  document.body.innerHTML = ''
})

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

describe('I-1 跨页结构网：artifactPage 页的页签按「有没有可切面板」分两类', () => {
  it('语料自证：全站带活页签的证据件恰好 7 个文件 / 11 个块（既有实测；变了就要人来核）', () => {
    expect(TAB_TITLE_HTML).toEqual([
      'emp-emplist.html',
      'order-addorder.html',
      'order-coreordercenter.html',
      'order-orderview.html',
      'system-systemsetting.html',
      'user-myinfo.html',
      'user-mymessage.html',
    ])
    expect(TAB_TITLE_BLOCKS).toBe(11)
  })

  it('受影响页今天为空集（两类都空；新增页必须显式登记）', () => {
    // ⚠️ 改自挂载的页（`user-myinfo` / `system-systemsetting`）不再出现在
    // 「用 `artifactPage(` 渲染」这个推导集合里；唯一的无面板页已删（见文件头）。
    // ⛔ 空集是**显式断言**、不是漏写：任何新增 artifactPage 带活页签的页都会把这条顶红。
    expect(TAB_PAGES).toEqual([])
    expect(PANEL_PAGES).toEqual([])
    expect(NO_PANEL_PAGES).toEqual([])
    expect(PANEL_PAGES.length + NO_PANEL_PAGES.length).toBe(TAB_PAGES.length)
  })
})

describe('阳性对照（本文件自身的哨兵）：同一个 harness 里点一个真控件仍会弹 ⇒ 上面的空集不是 harness 坏了', () => {
  // ⚠️ 标题原先写「不在任何页签里」——对这套夹具不成立（`#savequestionbtn` 在
  // `.layui-tab-item` 面板内；独立评审 2026-09-23 指出）。
  // 本条真正要证的是「**不是页签标题 `li`** 的真控件，点了仍会被 catch-all 拦成『尚未开放』」，
  // 与 ① 类「页签标题 `li` 不弹」恰好构成一对。措辞按事实改，**断言一字未动**。
  it('#savequestionbtn（system-systemsetting，不是页签标题 li）⇒ 仍弹', async () => {
    const { root, cleanup } = await mountPage('system-systemsetting.html')
    const btn = root.querySelector<HTMLElement>('#savequestionbtn')
    expect(btn, '证据件里应当有 `#savequestionbtn`').not.toBeNull()
    expect(clickAndCollect(btn!)).toEqual(['「保存」尚未开放'])
    cleanup()
  })
})
