// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ConfigRow } from '@/api/config'
import type { PlatformAutoReplyRuleRow } from '@/api/platformAutoReplies'

/**
 * 【交易平台自动回复】island 页（`pages/system-platformautoreply.ts`）的**交互测试**。
 *
 * 这条测试**驱动真交互**，不是只断言标记存在：
 *  - 点工具栏「新增自动回复」→ 填弹层 → 点「保存设置」→ 断言 `POST` 的**载荷**；
 *  - 点行内「修改」→ 断言预填 + `PATCH` 载荷；点「删除」→ 断言 `DELETE`；
 *  - 触发 layui 的 `switch(staChecked)` 事件 → 断言 `PATCH {is_enabled}`（含失败拨回）；
 *  - 点「查看图片」→ 有图走 `layer.photos`、没图只提示；
 *  - 勾选行 + 点「批量修改自动回复内容」→ 逐行 `PATCH {content}`；
 *  - 点弹层「上传」→ 选文件 → 真调 `POST upload-image/`（multipart）→ 缩略图 + 保存带
 *    `image_key`；失败 / 超限 / 上传中都各有用例。
 *
 * ## 为什么用**真证据件 HTML**而不是手写一张小标记
 *
 * 页面走 `mountConfigLike`：真的去 `fetch('replica/system-platformautoreply.html')`
 * 再把标记注入。测试把 `fetch` 指向**磁盘上的证据件**，于是断言的 DOM 就是页面真正渲染的
 * 那一份（开关模板 `#switchTpl1`、`replyOcc` 的 4 个 `value`、`platformIds` 的 13 个值
 * 全部来自证据件，抄错就红）。
 *
 * ## 业务方裁定的删除（有意偏离对标）
 *
 * 「专用人员类型 / 专用人员名称」整块**不做**（表格两列 + 弹层那组 radio + 那个下拉）。
 * 判据按 `AGENTS.md` §5-G：**打在去掉注释后的标记上**——证据件文件顶部有一段说明
 * **含这几个词**，直接搜文件文本会假绿（E-0161 的同族）。
 *
 * ## 四条老坑（`AGENTS.md` §5-G / E-0061 / E-0067）
 *
 * 1. 类名与选择器分成两个常量（页模块里 `VIEW_IMAGE_CLASS` + `VIEW_IMAGE_SELECTOR` 等）；
 * 2. 以 `(` 开头的语句一律前置分号；
 * 3. `bootstrapIsland` 必须先跑（挂样式）、再注入标记——顺序由共享零件
 *    `config-replica.ts::mountConfigLike` 保证；本文件用一个**调用序列**把它钉住；
 * ④ 本页**没有定时器**（无 `setInterval` / 轮询）⇒ 不需要 E-0198 的假定时器收口；
 *    显式登记，免得下一个人以为漏了。
 *
 * 手法照 `system-tripartiteplatformsetting.spec.ts`：`vi.mock` 掉 `bootstrapIsland`
 * （jsdom 里加载不了真 layui）与 api 模块，其余（DOM、证据件、事件）都是真的。
 * ⚠️ `vi.mock` 会被提升，`import` 写在它之后。
 */

const HERE = path.dirname(fileURLToPath(import.meta.url))
/** `src/frontend`（本文件位于 `src/frontend/src/layui-island/pages/__tests__/`）。 */
const FRONTEND_ROOT = path.resolve(HERE, '../../../..')
/** 页面真正会 fetch 的那份证据件（原样读盘，别手写替身）。 */
const EVIDENCE = readFileSync(
  path.resolve(FRONTEND_ROOT, 'public/replica/system-platformautoreply.html'),
  'utf8',
)
/** 去掉 HTML 注释后的**标记**（判「某个控件在不在这份标记里」一律用它，E-0161）。 */
const MARKUP = EVIDENCE.replace(/<!--[\s\S]*?-->/g, '')

/** layui 的**可控替身**：能像真 layui 一样搬节点、触发弹层、收事件。 */
const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  const tableHandlers = new Map<string, (obj: unknown) => unknown>()
  const formHandlers = new Map<string, (obj?: unknown) => unknown>()
  const tableRenders: Array<Record<string, unknown>> = []
  const reloads: Array<{ id: string; data: unknown }> = []
  const opens: Array<Record<string, unknown>> = []
  const confirms: string[] = []
  const photos: Array<Record<string, unknown>> = []
  const formRenders: Array<string | undefined> = []
  /** 生命周期顺序（`bootstrap` 必须先于 `fetch-artifact`；两者都先于 `init`）。 */
  const calls: string[] = []
  /** `table.checkStatus` 回什么（默认空 = 没勾行）。 */
  const state = { checked: [] as unknown[] }

  function layerContent(): HTMLElement {
    let host = document.querySelector<HTMLElement>('.layui-layer-content')
    if (!host) {
      host = document.createElement('div')
      host.className = 'layui-layer-content'
      document.body.appendChild(host)
    }
    return host
  }

  function removeLayerContent(): void {
    document.querySelector('.layui-layer-content')?.remove()
  }

  function closeLayer(index: number): void {
    const config = opens[index - 1]
    const end = config?.end as (() => void) | undefined
    end?.()
    const node = config?.content as HTMLElement | undefined
    if (node instanceof HTMLElement) node.style.display = 'none'
    removeLayerContent()
  }

  return {
    msg,
    tableHandlers,
    formHandlers,
    tableRenders,
    reloads,
    opens,
    confirms,
    photos,
    formRenders,
    calls,
    state,
    form: {
      render: vi.fn((type?: string) => {
        formRenders.push(type)
      }),
      on: vi.fn((event: string, cb: (obj?: unknown) => unknown) => {
        formHandlers.set(event, cb)
      }),
    },
    table: {
      render: vi.fn((opts: Record<string, unknown>) => {
        tableRenders.push(opts)
      }),
      reloadData: vi.fn((id: string, opts: { data: unknown }) => {
        reloads.push({ id, data: opts.data })
      }),
      on: vi.fn((event: string, cb: (obj: unknown) => unknown) => {
        tableHandlers.set(event, cb)
      }),
      checkStatus: vi.fn(() => ({ data: state.checked })),
    },
    layer: {
      /** 真 layui 会把 `content` 指向的节点 `show()` 后搬进 `.layui-layer-content`。 */
      open: vi.fn((opts: Record<string, unknown>) => {
        opens.push(opts)
        const node = opts.content as HTMLElement | undefined
        if (node instanceof HTMLElement) {
          node.style.display = ''
          layerContent().appendChild(node)
        }
        return opens.length
      }),
      close: vi.fn((index: number) => closeLayer(index)),
      closeAll: vi.fn(() => {
        if (opens.length > 0) closeLayer(opens.length)
      }),
      confirm: vi.fn((text: string, _options: unknown, onConfirm: (i: number) => void) => {
        confirms.push(text)
        onConfirm(confirms.length)
      }),
      photos: vi.fn((options: Record<string, unknown>) => {
        photos.push(options)
      }),
      alert: vi.fn(),
    },
    /** layui 的 jQuery 替身：本页只用它把节点交给 `layer.open`。 */
    $: (el: Element) => el,
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => {
    LAYUI.calls.push('bootstrap')
    return {
      form: LAYUI.form,
      table: LAYUI.table,
      layer: LAYUI.layer,
      $: LAYUI.$,
    }
  }),
  initIslandElements: vi.fn(() => {
    LAYUI.calls.push('init')
  }),
  // 真实现是 `islandMsg(layer, text)` → `layer.msg(text, {time:1500})`；替身只记文本。
  islandMsg: (_layer: unknown, text: string) => LAYUI.msg(text),
}))

vi.mock('@/api/platformAutoReplies', () => ({
  fetchPlatformAutoReplyRules: vi.fn(),
  createPlatformAutoReplyRule: vi.fn(),
  updatePlatformAutoReplyRule: vi.fn(),
  deletePlatformAutoReplyRule: vi.fn(),
  uploadPlatformAutoReplyImage: vi.fn(),
}))

vi.mock('@/api/config', () => ({
  fetchAllGames: vi.fn(),
}))

import { fetchAllGames } from '@/api/config'
import {
  createPlatformAutoReplyRule,
  deletePlatformAutoReplyRule,
  fetchPlatformAutoReplyRules,
  updatePlatformAutoReplyRule,
  uploadPlatformAutoReplyImage,
  type PlatformAutoReplyImageUpload,
} from '@/api/platformAutoReplies'
import page, {
  BATCH_CONTENT_FIELD,
  BATCH_PANEL_SELECTOR,
  CONTENT_FIELD,
  CONTENT_MAX_LENGTH,
  GAME_ALL_LABEL,
  GAME_SELECT_ID,
  PANEL_SELECTOR,
  PIC_SELECTOR,
  PLATFORM_CHECKBOX_NAME,
  SUPPORTED_PLATFORM,
  SWITCH_FILTER,
  TABLE_ELEM,
  TABLES_FOR_TEST,
  MAX_REPLY_IMAGE_BYTES,
  TRIGGER_OPTIONS,
  UPLOAD_BUTTON_ID,
  UPLOAD_BUTTON_TEXT,
  UPLOAD_INPUT_ID,
  VIEW_IMAGE_SELECTOR,
  panelPayload,
  readPanelValues,
  ruleToTableRow,
  validatePanelValues,
  type AutoReplyTableRow,
} from '../system-platformautoreply'

/* ────────────────────────────── 夹具（全部合成值）────────────────────────────── */

/** 两款游戏（合成名，⛔ 不是任何真实租户数据）。 */
const GAMES: ConfigRow[] = [
  {
    id: 11, name: '合成游戏甲', sort_order: 2, is_active: true,
    created_at: '2026-10-27T09:00:00+08:00',
  },
  {
    id: 12, name: '合成游戏乙', sort_order: 1, is_active: true,
    created_at: '2026-10-27T09:00:00+08:00',
  },
]

/** 一行「限定游戏甲 / 接单后 / 启用 / 无图」。 */
const ROW_ON: PlatformAutoReplyRuleRow = {
  id: 1,
  platform: 10,
  platform_label: '代练通',
  game_id: 11,
  game: 11,
  game_name: '合成游戏甲',
  trigger: 0,
  trigger_name: '接单后',
  content: '合成话术甲',
  has_image: false,
  image_url: '',
  is_enabled: true,
  created_at: '2026-10-27T09:10:00+08:00',
  updated_at: '2026-10-27T09:10:00+08:00',
}

/** 一行「全部游戏 / 申请验收后 / 禁用 / 有图」。 */
const ROW_ALL: PlatformAutoReplyRuleRow = {
  ...ROW_ON,
  id: 2,
  game_id: null,
  game: null,
  game_name: '',
  trigger: 10,
  trigger_name: '申请验收后',
  content: '合成话术乙',
  has_image: true,
  image_url: '/api/media/blob/tenant.test.com/reply/synthetic-b.png',
  is_enabled: false,
}

/**
 * 生产形态的图片链接：腾讯云 COS **预签名串**（多参数、`&` 分隔、带签名）。
 *
 * ⚠️ 这是「当前图片地址异常」的复现夹具：本机 / CI 的 `LocalStorage` 给的是
 * `/api/media/blob/<key>`（**没有查询串**），所以旧的「只 `toContain('…png')`」断言
 * 永远碰不到 `&`；生产是 `CosStorage.presign_get`，链接里全是 `&`。
 * ⛔ 不是真凭据，只是一段形状相同的合成签名。
 */
const COS_IMAGE_URL =
  'https://tt-g4-fixture.cos.ap-shanghai.myqcloud.com/kexing/platform-auto-replies/16c3005fd274.png' +
  '?q-sign-algorithm=sha1&q-ak=AKIDg4fixtureonly&q-sign-time=1793000000;1793000300' +
  '&q-key-time=1793000000;1793000300&q-header-list=host&q-url-param-list=&q-signature=0123456789abcdef'

/** 上传成功的回执（合成对象名；形态与后端 `upload-image/` 一致）。 */
const UPLOADED: PlatformAutoReplyImageUpload = {
  image_key: 'tenant.test.com/platform-auto-replies/synthetic-upload.png',
  url: '/api/media/blob/tenant.test.com/platform-auto-replies/synthetic-upload.png',
  expires_in: 300,
}

function flush(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

describe('交易平台自动回复 岛页（T13）', () => {
  let root: HTMLElement

  beforeAll(() => {
    // 页面走 mountConfigLike：它真的去 fetch 证据件。这里把 fetch 指到磁盘上的那一份。
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        LAYUI.calls.push('fetch-artifact')
        return { ok: true, status: 200, text: async () => EVIDENCE }
      }) as unknown as typeof fetch,
    )
  })

  afterAll(() => {
    vi.unstubAllGlobals()
  })

  beforeEach(() => {
    vi.clearAllMocks()
    LAYUI.tableHandlers.clear()
    LAYUI.formHandlers.clear()
    LAYUI.tableRenders.length = 0
    LAYUI.reloads.length = 0
    LAYUI.opens.length = 0
    LAYUI.confirms.length = 0
    LAYUI.photos.length = 0
    LAYUI.formRenders.length = 0
    LAYUI.calls.length = 0
    LAYUI.state.checked = []
    document.body.innerHTML = ''
    root = document.createElement('div')
    document.body.appendChild(root)

    vi.mocked(fetchPlatformAutoReplyRules).mockResolvedValue([ROW_ON, ROW_ALL])
    vi.mocked(fetchAllGames).mockResolvedValue([...GAMES])
    vi.mocked(createPlatformAutoReplyRule).mockResolvedValue(ROW_ON)
    vi.mocked(updatePlatformAutoReplyRule).mockResolvedValue(ROW_ON)
    vi.mocked(deletePlatformAutoReplyRule).mockResolvedValue(null)
    vi.mocked(uploadPlatformAutoReplyImage).mockResolvedValue({ ...UPLOADED })
  })

  async function mount(): Promise<void> {
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()
  }

  function renderOpts(): Record<string, unknown> {
    const found = LAYUI.tableRenders.find((entry) => entry.elem === TABLE_ELEM)
    if (!found) throw new Error(`没有对 ${TABLE_ELEM} 调用过 table.render`)
    return found
  }

  function tableCols(): Array<Record<string, unknown>> {
    return (renderOpts().cols as Array<Array<Record<string, unknown>>>)[0]!
  }

  function lastReloadRows(): AutoReplyTableRow[] {
    const last = LAYUI.reloads[LAYUI.reloads.length - 1]
    if (!last) throw new Error('没有调用过 table.reloadData')
    return last.data as AutoReplyTableRow[]
  }

  function callToolbar(event: string): void {
    const handler = LAYUI.tableHandlers.get('toolbar(tableList)')
    if (!handler) throw new Error('没有注册 toolbar(tableList)')
    handler({ event })
  }

  function callTool(event: string, row: AutoReplyTableRow): void {
    const handler = LAYUI.tableHandlers.get('tool(tableList)')
    if (!handler) throw new Error('没有注册 tool(tableList)')
    handler({ event, data: row })
  }

  /** 当前打开的「新增/修改」弹层节点（`layer.open` 已经把 `.modifyParamDiv` 搬进 layer content）。 */
  function panel(): HTMLElement {
    const node = document.querySelector<HTMLElement>(PANEL_SELECTOR)
    if (!node) throw new Error('弹层节点不存在（layer.open 没把 .modifyParamDiv 搬出来？）')
    return node
  }

  function batchPanel(): HTMLElement {
    const node = document.querySelector<HTMLElement>(BATCH_PANEL_SELECTOR)
    if (!node) throw new Error('批量弹层节点不存在')
    return node
  }

  /** 像用户一样点弹层里的「保存设置」（`<a class="… saveBtn">`）。 */
  function clickPanelSave(): void {
    const button = panel().querySelector('.saveBtn')
    if (!(button instanceof HTMLElement)) throw new Error('弹层里没有 .saveBtn')
    button.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  }

  function clickUpload(): void {
    const button = panel().querySelector(`#${UPLOAD_BUTTON_ID}`)
    if (!(button instanceof HTMLElement)) throw new Error('弹层里没有上传按钮')
    button.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  }

  /** 页面自己插的隐藏文件选择框（证据件里没有它 ⇒ 只能从根容器找）。 */
  function uploadInput(): HTMLInputElement {
    const input = root.querySelector<HTMLInputElement>(`#${UPLOAD_INPUT_ID}`)
    if (!input) throw new Error('页面没有插入隐藏的文件选择框')
    return input
  }

  /** 像用户一样选一个文件：给 input 塞 `files` 再触发 `change`。 */
  function chooseFile(file: File): void {
    const input = uploadInput()
    Object.defineProperty(input, 'files', { value: [file], configurable: true })
    input.dispatchEvent(new Event('change'))
  }

  function makeImageFile(name = 'synthetic-upload.png'): File {
    // 首 4 字节是 PNG 魔数（前端不判内容，只是让夹具看起来像图）
    return new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], name, { type: 'image/png' })
  }

  function chooseTrigger(value: number): void {
    for (const radio of panel().querySelectorAll<HTMLInputElement>('input[name="replyOcc"]')) {
      radio.checked = radio.value === String(value)
    }
  }

  function fillContent(value: string): void {
    const textarea = panel().querySelector<HTMLTextAreaElement>(`#${CONTENT_FIELD}`)
    if (!textarea) throw new Error('弹层里没有回复内容输入框')
    textarea.value = value
  }

  function setPlatforms(ids: readonly number[]): void {
    for (const box of panel().querySelectorAll<HTMLInputElement>(
      `input[name="${PLATFORM_CHECKBOX_NAME}"]`,
    )) {
      box.checked = ids.includes(Number(box.value))
    }
  }

  function setGame(value: string): void {
    const select = panel().querySelector<HTMLSelectElement>(`select#${GAME_SELECT_ID}`)
    if (!select) throw new Error('弹层里没有限定游戏下拉')
    select.value = value
  }

  function switchHandler() {
    const handler = LAYUI.formHandlers.get(`switch(${SWITCH_FILTER})`)
    if (!handler) throw new Error(`没有注册 switch(${SWITCH_FILTER})`)
    return handler
  }

  // ── 挂载顺序 + 列表接真 ─────────────────────────────────────

  it('先 bootstrapIsland（挂样式）再取证据件、注入标记，最后 initIslandElements', async () => {
    await mount()
    expect(LAYUI.calls.indexOf('bootstrap')).toBe(0)
    expect(LAYUI.calls.indexOf('fetch-artifact')).toBeGreaterThan(
      LAYUI.calls.indexOf('bootstrap'),
    )
    expect(LAYUI.calls.indexOf('init')).toBeGreaterThan(LAYUI.calls.indexOf('fetch-artifact'))
    // 标记真的注入了（不是只调了函数）
    expect(root.querySelector('#tableList')).toBeInstanceOf(HTMLElement)
    expect(root.querySelector('#toolbarDemo')).toBeInstanceOf(HTMLElement)
    expect(root.querySelector('#switchTpl1')).toBeInstanceOf(HTMLElement)
  })

  it('表格接真：API 行映射成证据件模板认得的形状，再 reloadData 上屏 + 重渲开关', async () => {
    await mount()
    expect(fetchPlatformAutoReplyRules).toHaveBeenCalledTimes(1)
    const rows = lastReloadRows()
    expect(rows).toHaveLength(2)
    // 开关模板的反向编码（`ReplyStatus == 0` 才 checked）与 value（行主键）逐条核
    expect(rows[0]).toMatchObject({
      GameName: '合成游戏甲',
      ReplyOccasion: '接单后',
      ReplyMsg: '合成话术甲',
      PlatformName: '代练通',
      ReplyStatus: 0,
      PlatformAutoReplySetId: 1,
    })
    expect(rows[1]).toMatchObject({
      // `null`（全部游戏）→ 我们自己补的那句文案（不是后端编的）
      GameName: GAME_ALL_LABEL,
      ReplyOccasion: '申请验收后',
      ReplyStatus: 1,
      PlatformAutoReplySetId: 2,
      AppleImageUrl: '/api/media/blob/tenant.test.com/reply/synthetic-b.png',
    })
    // 表格里的开关是 layui 控件：重取后必须再 render 一次（否则点不动）
    expect(LAYUI.formRenders.length).toBeGreaterThan(0)
  })

  it('限定游戏下拉按**我方游戏**重灌，第一项是「全部游戏」', async () => {
    await mount()
    expect(fetchAllGames).toHaveBeenCalledTimes(1)
    callToolbar('addBtn')
    const select = panel().querySelector<HTMLSelectElement>(`select#${GAME_SELECT_ID}`)!
    const options = Array.from(select.options).map((o) => [o.value, o.textContent])
    expect(options).toEqual([
      ['', GAME_ALL_LABEL],
      ['11', '合成游戏甲'],
      ['12', '合成游戏乙'],
    ])
    // 新增时默认「全部游戏」
    expect(select.value).toBe('')
  })

  it('游戏名里的 `<` 被转义（拼进 innerHTML 前过 escapeHtml）', async () => {
    vi.mocked(fetchAllGames).mockResolvedValueOnce([
      { ...GAMES[0]!, id: 21, name: '合成<b>游戏</b>' },
    ])
    await mount()
    callToolbar('addBtn')
    const option = panel().querySelector<HTMLOptionElement>(
      `select#${GAME_SELECT_ID} option[value="21"]`,
    )
    expect(option).toBeInstanceOf(HTMLOptionElement)
    // 转义后浏览器解析出的**文本**是原文，而不是长出一个 <b> 元素
    expect(option!.textContent).toBe('合成<b>游戏</b>')
    expect(option!.querySelector('b')).toBeNull()
  })

  it('文件头「生效口径」与现状一致：发送引擎已上线、依赖 `dailiantong_sync` 同步进程', () => {
    // 文件头必须如实：发送引擎已上线、生效**依赖常驻同步进程**
    // （否则读者会以为保存完就立刻会发）。
    const source = readFileSync(
      path.resolve(HERE, '../system-platformautoreply.ts'),
      'utf8',
    )
    expect(source).toContain('发送引擎已上线')
    expect(source).toContain('dailiantong_sync')
    expect(source).not.toContain('本版**没有发送引擎**')
    expect(source).not.toContain('发送链路未实现')
  })

  // ── 新增 / 修改弹层 ───────────────────────────────────────

  it('新增：打开弹层 → 选游戏/时机/内容/平台 → 点保存发 POST', async () => {
    await mount()
    callToolbar('addBtn')
    expect(LAYUI.opens).toHaveLength(1)
    expect(LAYUI.opens[0]).toMatchObject({
      title: '新增自动回复',
      shadeClose: false,
      skin: 'layui-layer-lan',
    })
    // 证据件里的 4 颗时机 radio（顺序与文案逐字照证据件）
    const radios = Array.from(
      panel().querySelectorAll<HTMLInputElement>('input[name="replyOcc"]'),
    ).map((radio) => ({ value: Number(radio.value), title: radio.getAttribute('title') }))
    expect(radios).toEqual(TRIGGER_OPTIONS.map((o) => ({ ...o })))

    setGame('12')
    chooseTrigger(10)
    fillContent('  合成话术新  ')
    setPlatforms([SUPPORTED_PLATFORM])
    clickPanelSave()
    await flush()

    expect(createPlatformAutoReplyRule).toHaveBeenCalledWith({
      platform: SUPPORTED_PLATFORM,
      game: 12,
      trigger: 10,
      content: '合成话术新',
    })
    // 弹层关了、列表重取了、提示说成功
    expect(LAYUI.layer.close).toHaveBeenCalledWith(1)
    expect(fetchPlatformAutoReplyRules).toHaveBeenCalledTimes(2)
    expect(LAYUI.msg).toHaveBeenCalledWith('已保存')
  })

  it('保存连点 ⇒ 只发一次 POST（按钮禁用 + submitting 旗标双保险）', async () => {
    await mount()
    callToolbar('addBtn')
    setGame('12')
    chooseTrigger(10)
    fillContent('合成话术连点')
    setPlatforms([SUPPORTED_PLATFORM])
    let resolveCreate: (row: PlatformAutoReplyRuleRow) => void = () => undefined
    vi.mocked(createPlatformAutoReplyRule).mockImplementationOnce(
      () =>
        new Promise<PlatformAutoReplyRuleRow>((resolve) => {
          resolveCreate = resolve
        }),
    )
    clickPanelSave()
    const button = panel().querySelector<HTMLElement>('.saveBtn')!
    expect(button.classList.contains('layui-btn-disabled')).toBe(true)
    clickPanelSave()
    clickPanelSave()
    expect(createPlatformAutoReplyRule).toHaveBeenCalledTimes(1)

    resolveCreate(ROW_ON)
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith('已保存')
    expect(button.classList.contains('layui-btn-disabled')).toBe(false)
  })

  it('修改：预填真值（含图片预览位），PATCH 不带 is_enabled', async () => {
    await mount()
    const row = lastReloadRows()[1]! // 全部游戏 / 申请验收后 / 有图 / 禁用
    callTool('submitEdit', row)
    expect(LAYUI.opens[0]).toMatchObject({ title: '修改自动回复' })
    expect(panel().querySelector<HTMLSelectElement>(`select#${GAME_SELECT_ID}`)!.value).toBe('')
    expect(
      panel().querySelector<HTMLInputElement>('input[name="replyOcc"]:checked')!.value,
    ).toBe('10')
    expect(panel().querySelector<HTMLTextAreaElement>(`#${CONTENT_FIELD}`)!.value).toBe(
      '合成话术乙',
    )
    // 图片预览位：有图 ⇒ 画一张缩略图（点它开预览）
    expect(panel().querySelector<HTMLElement>(PIC_SELECTOR)!.querySelector('img')).toBeInstanceOf(
      HTMLImageElement,
    )

    fillContent('合成话术乙改')
    clickPanelSave()
    await flush()
    expect(updatePlatformAutoReplyRule).toHaveBeenCalledWith(2, {
      platform: SUPPORTED_PLATFORM,
      game: null,
      trigger: 10,
      content: '合成话术乙改',
    })
    const payload = vi.mocked(updatePlatformAutoReplyRule).mock.calls[0]![1]
    expect(Object.keys(payload)).not.toContain('is_enabled')
    expect(LAYUI.msg).toHaveBeenCalledWith('已保存')
  })

  it.each([
    ['没选时机', () => chooseTrigger(-1), '请选择回复时机'],
    ['选了本版不做的时机（15 提交异常后）', () => chooseTrigger(15), '「提交异常后」尚未开放'],
    ['选了本版不做的时机（20 申请撤销后）', () => chooseTrigger(20), '「申请撤销后」尚未开放'],
    ['内容为空', () => fillContent('   '), '回复内容不能为空'],
    [
      '内容超过 200',
      () => fillContent('话'.repeat(CONTENT_MAX_LENGTH + 1)),
      `回复内容不能超过 ${CONTENT_MAX_LENGTH} 个字符`,
    ],
    ['勾了别的平台', () => setPlatforms([SUPPORTED_PLATFORM, 30]), '本版只支持「代练通」'],
    ['没勾平台', () => setPlatforms([]), '请勾选「代练通」'],
  ])('弹层校验：%s ⇒ 一句提示 + 零请求', async (_name, mutate, expected) => {
    await mount()
    callToolbar('addBtn')
    // 先填一份合法值，再应用本条的破坏动作（保证「失败只因为这一条」）。
    setGame('11')
    chooseTrigger(0)
    fillContent('合成话术合法')
    setPlatforms([SUPPORTED_PLATFORM])
    mutate()
    clickPanelSave()
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith(expect.stringContaining(expected))
    expect(createPlatformAutoReplyRule).not.toHaveBeenCalled()
    expect(updatePlatformAutoReplyRule).not.toHaveBeenCalled()
  })

  it('删除：确认 → DELETE → 重取列表 → 提示', async () => {
    await mount()
    callTool('submitDel', lastReloadRows()[0]!)
    expect(LAYUI.confirms[0]).toBe('是否确认删除这条自动回复？')
    expect(deletePlatformAutoReplyRule).toHaveBeenCalledWith(1)
    await flush()
    expect(fetchPlatformAutoReplyRules).toHaveBeenCalledTimes(2)
    expect(LAYUI.msg).toHaveBeenCalledWith('已删除')
  })

  it('工具栏刷新：真的重取列表并重绘', async () => {
    await mount()
    expect(fetchPlatformAutoReplyRules).toHaveBeenCalledTimes(1)
    vi.mocked(fetchPlatformAutoReplyRules).mockResolvedValue([ROW_ON])
    callToolbar('refresh')
    await flush()
    expect(fetchPlatformAutoReplyRules).toHaveBeenCalledTimes(2)
    expect(lastReloadRows()).toHaveLength(1)
  })

  // ── 行内启用开关 ─────────────────────────────────────────

  it(`开关 switch(${SWITCH_FILTER})：切换后 PATCH { is_enabled } 并重取列表`, async () => {
    await mount()
    const input = document.createElement('input')
    input.type = 'checkbox'
    input.value = '2'
    input.checked = true
    root.appendChild(input)

    switchHandler()({ elem: input })
    await flush()
    expect(updatePlatformAutoReplyRule).toHaveBeenCalledWith(2, { is_enabled: true })
    expect(fetchPlatformAutoReplyRules).toHaveBeenCalledTimes(2)
    expect(lastReloadRows()).toHaveLength(2)
  })

  it('开关失败：勾选态拨回、把后端文案说出来、再以服务端为准重取', async () => {
    await mount()
    vi.mocked(updatePlatformAutoReplyRule).mockRejectedValueOnce(new Error('回复时机不合法'))
    const input = document.createElement('input')
    input.type = 'checkbox'
    input.value = '1'
    input.checked = false
    root.appendChild(input)

    switchHandler()({ elem: input })
    await flush()
    expect(input.checked).toBe(true) // 拨回（原状态是 false）
    expect(LAYUI.msg).toHaveBeenCalledWith('回复时机不合法')
    expect(fetchPlatformAutoReplyRules).toHaveBeenCalledTimes(2)
  })

  it('开关的值不是有效主键：零请求（不拿 0 / NaN 去打端点）', async () => {
    await mount()
    const input = document.createElement('input')
    input.type = 'checkbox'
    input.value = 'abc'
    input.checked = true
    switchHandler()({ elem: input })
    await flush()
    expect(updatePlatformAutoReplyRule).not.toHaveBeenCalled()
  })

  // ── 查看图片 ────────────────────────────────────────────

  it('预签名图片链接**原样**交给 layer.photos（转义 `&` ⇒ 预加载请求带 `&amp;` ⇒ 签名失配）', async () => {
    // 真浏览器复现记录：layui `layer.photos` 拿 `data[].src` 先做 `new Image().src = src`
    // （原始 JS 串、**不经 HTML 解码**），再把同一个串拼进 `<img src="…">`。
    // 我们把 src 过 `escapeHtml` 后，预加载请求真的带上了 `&amp;`（Playwright 抓包），
    // COS 认为签名参数被改 ⇒ 403 ⇒ layui 弹「当前图片地址异常」。
    // ⇒ `src` 必须是**原样 URL**；会破出属性的字符改用百分号编码（见下一条）。
    vi.mocked(fetchPlatformAutoReplyRules).mockResolvedValue([
      { ...ROW_ALL, image_url: COS_IMAGE_URL },
    ])
    await mount()
    const imageLink = document.createElement('a')
    imageLink.className = VIEW_IMAGE_SELECTOR.slice(1)
    imageLink.dataset.id = '2'
    root.appendChild(imageLink)
    imageLink.dispatchEvent(new MouseEvent('click', { bubbles: true }))

    const options = LAYUI.photos[0] as {
      photos: { data: Array<{ alt: string; src: string }> }
    }
    expect(options.photos.data[0]!.src).toBe(COS_IMAGE_URL)
    expect(options.photos.data[0]!.src).not.toContain('&amp;')
    expect(options.photos.data[0]!.src).toContain('&q-signature=')
  })

  it('图片链接里带引号 / 尖括号 ⇒ 百分号编码（仍然破不出 `<img src="…">` 属性）', async () => {
    const hostile = 'https://cos.example.com/a.png?x=" onerror="alert(1)&y=<b>'
    vi.mocked(fetchPlatformAutoReplyRules).mockResolvedValue([
      { ...ROW_ALL, image_url: hostile },
    ])
    await mount()
    const imageLink = document.createElement('a')
    imageLink.className = VIEW_IMAGE_SELECTOR.slice(1)
    imageLink.dataset.id = '2'
    root.appendChild(imageLink)
    imageLink.dispatchEvent(new MouseEvent('click', { bubbles: true }))

    const options = LAYUI.photos[0] as {
      photos: { data: Array<{ alt: string; src: string }> }
    }
    const src = options.photos.data[0]!.src
    expect(src).not.toContain('"')
    expect(src).not.toContain('<')
    expect(src).not.toContain('>')
    expect(src).toContain('%22')
    expect(src).toContain('%3C')
    // `&` 是本条的对照：它**必须**留着（否则又回到上面的签名破坏）
    expect(src).toContain('&y=')
  })

  it('点「查看图片」：有图 ⇒ layer.photos，没图 ⇒ 只提示「未上传图片」', async () => {
    await mount()
    // 有图那一行：把 imageCell 的标记塞进 DOM，再像用户一样点它。
    const imageLink = document.createElement('a')
    imageLink.className = VIEW_IMAGE_SELECTOR.slice(1)
    imageLink.dataset.id = '2'
    root.appendChild(imageLink)
    imageLink.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(LAYUI.photos).toHaveLength(1)
    const options = LAYUI.photos[0] as {
      photos: { title: string; data: Array<{ alt: string; src: string }> }
    }
    expect(options.photos.title).toBe('回复图片')
    expect(options.photos.data[0]!.src).toContain('synthetic-b.png')

    // 没图那一行：只提示「未上传图片」，⛔ 不开空层、⛔ 不发请求。
    const emptyLink = document.createElement('a')
    emptyLink.className = VIEW_IMAGE_SELECTOR.slice(1)
    emptyLink.dataset.id = '1'
    root.appendChild(emptyLink)
    emptyLink.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(LAYUI.photos).toHaveLength(1) // 还是只有上面那一次
    expect(LAYUI.msg).toHaveBeenCalledWith('未上传图片')
    expect(updatePlatformAutoReplyRule).not.toHaveBeenCalled()
  })

  it('弹层里的图片预览位：有图点开预览，没图提示', async () => {
    await mount()
    callTool('submitEdit', lastReloadRows()[1]!)
    // `#ptid` 在弹层节点**外面**（证据件把它放在 `<form>` 里）：与证据件保持同形，写的是行主键。
    expect((document.querySelector('#ptid') as HTMLInputElement).value).toBe('2')
    const pic = panel().querySelector<HTMLElement>(PIC_SELECTOR)!
    pic.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(LAYUI.photos).toHaveLength(1)

    callTool('submitEdit', lastReloadRows()[0]!)
    const emptyPic = panel().querySelector<HTMLElement>(PIC_SELECTOR)!
    emptyPic.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(LAYUI.photos).toHaveLength(1)
    expect(LAYUI.msg).toHaveBeenCalledWith('未上传图片')
  })

  // ── 上传图片：真上传 ─────────────────────

  it('点「上传」→ 选文件 → 调上传接口 → 缩略图 → 保存带 image_key', async () => {
    await mount()
    callToolbar('addBtn')
    clickUpload()
    // 点按钮 = 打开文件选择（jsdom 不会弹框，只能断言隐藏 input 在、按钮没被禁）。
    expect(uploadInput().type).toBe('file')
    expect(
      panel().querySelector<HTMLElement>(`#${UPLOAD_BUTTON_ID}`)!.classList.contains(
        'layui-btn-disabled',
      ),
    ).toBe(false)

    const file = makeImageFile()
    chooseFile(file)
    await flush()

    expect(uploadPlatformAutoReplyImage).toHaveBeenCalledWith(file)
    // 缩略图：`.pic` 里出现刚上传那张（点开预览也用它，而不是库里那张旧图）。
    const pic = panel().querySelector<HTMLElement>(PIC_SELECTOR)!
    expect(pic.getAttribute('data-has-image')).toBe('1')
    expect(pic.querySelector('img')!.getAttribute('src')).toBe(UPLOADED.url)
    pic.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    const options = LAYUI.photos[0] as {
      photos: { data: Array<{ alt: string; src: string }> }
    }
    expect(options.photos.data[0]!.src).toBe(UPLOADED.url)
    expect(LAYUI.msg).toHaveBeenCalledWith('图片已上传，保存后生效')

    // 保存：载荷带上服务端给的对象名（⛔ 不是前端自己拼的）。
    setGame('11')
    chooseTrigger(0)
    fillContent('合成话术带图')
    setPlatforms([SUPPORTED_PLATFORM])
    clickPanelSave()
    await flush()
    expect(createPlatformAutoReplyRule).toHaveBeenCalledWith({
      platform: SUPPORTED_PLATFORM,
      game: 11,
      trigger: 0,
      content: '合成话术带图',
      image_key: UPLOADED.image_key,
    })
  })

  it('修改已有规则：换了图 ⇒ PATCH 带新 image_key（没换图时不带，见上一条）', async () => {
    await mount()
    callTool('submitEdit', lastReloadRows()[1]!)
    chooseFile(makeImageFile('synthetic-new.png'))
    await flush()
    fillContent('合成话术乙换图')
    clickPanelSave()
    await flush()
    expect(updatePlatformAutoReplyRule).toHaveBeenCalledWith(2, {
      platform: SUPPORTED_PLATFORM,
      game: null,
      trigger: 10,
      content: '合成话术乙换图',
      image_key: UPLOADED.image_key,
    })
  })

  it('上传中：按钮禁用 + 文案「上传中…」，再选文件不会发第二发', async () => {
    await mount()
    callToolbar('addBtn')
    let resolveUpload: (value: PlatformAutoReplyImageUpload) => void = () => undefined
    vi.mocked(uploadPlatformAutoReplyImage).mockImplementationOnce(
      () =>
        new Promise<PlatformAutoReplyImageUpload>((resolve) => {
          resolveUpload = resolve
        }),
    )
    chooseFile(makeImageFile())
    await flush()
    const button = panel().querySelector<HTMLElement>(`#${UPLOAD_BUTTON_ID}`)!
    expect(button.textContent).toBe('上传中…')
    expect(button.classList.contains('layui-btn-disabled')).toBe(true)
    expect(button.getAttribute('aria-disabled')).toBe('true')

    chooseFile(makeImageFile('another.png'))
    await flush()
    expect(uploadPlatformAutoReplyImage).toHaveBeenCalledTimes(1)

    resolveUpload({ ...UPLOADED })
    await flush()
    expect(button.textContent).toBe(UPLOAD_BUTTON_TEXT)
    expect(button.classList.contains('layui-btn-disabled')).toBe(false)
    expect(button.hasAttribute('aria-disabled')).toBe(false)
  })

  it('上传失败：给中文原因、按钮恢复；保存**不带** image_key（⛔ 不拿坏图落库）', async () => {
    await mount()
    callToolbar('addBtn')
    vi.mocked(uploadPlatformAutoReplyImage).mockRejectedValueOnce(
      new Error('只支持 jpg / jpeg / png 格式的图片'),
    )
    chooseFile(makeImageFile())
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith(
      '上传失败：只支持 jpg / jpeg / png 格式的图片',
    )
    const button = panel().querySelector<HTMLElement>(`#${UPLOAD_BUTTON_ID}`)!
    expect(button.textContent).toBe(UPLOAD_BUTTON_TEXT)
    expect(button.classList.contains('layui-btn-disabled')).toBe(false)

    setGame('11')
    chooseTrigger(0)
    fillContent('合成话术失败后保存')
    setPlatforms([SUPPORTED_PLATFORM])
    clickPanelSave()
    await flush()
    const payload = vi.mocked(createPlatformAutoReplyRule).mock.calls[0]![0]
    expect(Object.keys(payload)).not.toContain('image_key')
  })

  it('文件超过 2 MB：本地就拦下、零请求（后端那道墙只会回无解释的 400）', async () => {
    await mount()
    callToolbar('addBtn')
    const tooBig = new File(
      [new Uint8Array(MAX_REPLY_IMAGE_BYTES + 1)],
      'too-big.png',
      { type: 'image/png' },
    )
    chooseFile(tooBig)
    await flush()
    expect(uploadPlatformAutoReplyImage).not.toHaveBeenCalled()
    expect(LAYUI.msg).toHaveBeenCalledWith('图片超过 2 MB，请压缩后再上传')
  })

  // ── 批量修改 ────────────────────────────────────────────

  it('批量修改：没勾行 ⇒ 提示 + 零请求', async () => {
    await mount()
    LAYUI.state.checked = []
    callToolbar('batchEdit')
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith('请勾选要操作的数据行')
    expect(LAYUI.opens).toHaveLength(0)
    expect(updatePlatformAutoReplyRule).not.toHaveBeenCalled()
  })

  it('批量修改连点保存 ⇒ 不会重复跑第二遍（提交中闸）', async () => {
    await mount()
    LAYUI.state.checked = lastReloadRows()
    callToolbar('batchEdit')
    const textarea = batchPanel().querySelector<HTMLTextAreaElement>(`#${BATCH_CONTENT_FIELD}`)!
    textarea.value = '批量话术'
    let resolveFirst: (row: PlatformAutoReplyRuleRow) => void = () => undefined
    vi.mocked(updatePlatformAutoReplyRule).mockImplementationOnce(
      () =>
        new Promise<PlatformAutoReplyRuleRow>((resolve) => {
          resolveFirst = resolve
        }),
    )
    const yes = LAYUI.opens[0]!.yes as (index: number) => void
    yes(1)
    // 第一行的 PATCH 还挂着时再点一次：不许再起一轮循环。
    yes(1)
    expect(updatePlatformAutoReplyRule).toHaveBeenCalledTimes(1)

    resolveFirst(ROW_ON)
    await flush()
    // 一轮只跑完两行；第二轮没有发生（否则会变成 3 / 4 次）。
    await vi.waitFor(() => expect(updatePlatformAutoReplyRule).toHaveBeenCalledTimes(2))
  })

  it('批量修改：勾 2 行 ⇒ 开层 → 填内容 → 保存 ⇒ 逐行 PATCH {content}', async () => {
    await mount()
    LAYUI.state.checked = lastReloadRows()
    callToolbar('batchEdit')
    expect(LAYUI.opens).toHaveLength(1)
    expect(LAYUI.opens[0]).toMatchObject({
      title: '批量修改自动回复内容',
      btn: ['保存', '取消'],
    })
    const textarea = batchPanel().querySelector<HTMLTextAreaElement>(
      `#${BATCH_CONTENT_FIELD}`,
    )!
    expect(textarea.value).toBe('')
    textarea.value = '批量话术'
    const yes = LAYUI.opens[0]!.yes as (index: number) => void
    yes(1)
    await flush()
    expect(updatePlatformAutoReplyRule).toHaveBeenCalledTimes(2)
    expect(updatePlatformAutoReplyRule).toHaveBeenNthCalledWith(1, 1, { content: '批量话术' })
    expect(updatePlatformAutoReplyRule).toHaveBeenNthCalledWith(2, 2, { content: '批量话术' })
    expect(LAYUI.msg).toHaveBeenCalledWith('已保存')
  })

  it('批量修改：内容为空 ⇒ 提示 + 零请求；取消 ⇒ 不发请求', async () => {
    await mount()
    LAYUI.state.checked = lastReloadRows()
    callToolbar('batchEdit')
    const yes = LAYUI.opens[0]!.yes as (index: number) => void
    yes(1)
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith('回复内容不能为空')
    expect(updatePlatformAutoReplyRule).not.toHaveBeenCalled()

    const btn2 = LAYUI.opens[0]!.btn2 as (index: number) => void
    btn2(1)
    expect(LAYUI.layer.close).toHaveBeenCalledWith(1)
    expect(updatePlatformAutoReplyRule).not.toHaveBeenCalled()
  })

  it('批量修改中途失败：说清「已更新 N 条」并重取列表（⛔ 不谎报全部成功）', async () => {
    await mount()
    LAYUI.state.checked = lastReloadRows()
    callToolbar('batchEdit')
    batchPanel().querySelector<HTMLTextAreaElement>(`#${BATCH_CONTENT_FIELD}`)!.value = '批量话术'
    vi.mocked(updatePlatformAutoReplyRule).mockRejectedValueOnce(new Error('后端炸了'))
    const yes = LAYUI.opens[0]!.yes as (index: number) => void
    yes(1)
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith(expect.stringContaining('已更新 0 / 2 条'))
    expect(LAYUI.msg).not.toHaveBeenCalledWith('已保存')
    expect(fetchPlatformAutoReplyRules).toHaveBeenCalledTimes(2)
  })

  // ── 业务方裁定的删除：专用人员整块不做 ──────────────

  it('弹层里**没有**「指定专用人员类型」radio 与「指定专用人员」下拉（按标记判，E-0161）', async () => {
    await mount()
    callToolbar('addBtn')
    expect(panel().querySelector('input[name="replyUserType"]')).toBeNull()
    expect(panel().querySelector('#inputOrderKefu')).toBeNull()
    expect(panel().querySelector('.inputOrderKefuDiv')).toBeNull()
    // 证据件（去掉注释后的标记）里也没有这两块残留
    expect(MARKUP).not.toContain('replyUserType')
    expect(MARKUP).not.toContain('inputOrderKefu')
    expect(MARKUP).not.toContain('指定专用人员')
    // 阳性对照：注释里**有**这几个词（说明上面那条不是真空绿）
    expect(EVIDENCE).toContain('指定专用人员类型')
  })

  it('表格里**没有**「专用人员类型 / 专用人员名称」两列（恰好 8 列）', async () => {
    await mount()
    const titles = tableCols().map((col) => col.title)
    expect(titles).not.toContain('专用人员类型')
    expect(titles).not.toContain('专用人员名称')
    expect(titles).toHaveLength(8)
  })

  // ── 证据件开关模板 / 纯函数 ──────────────────────────────

  it('证据件 #switchTpl1 的字段名 / lay-filter / value 与代码里的键**恰好一致**', async () => {
    await mount()
    const markup = /<script type="text\/html" id="switchTpl1">([\s\S]*?)<\/script>/.exec(
      EVIDENCE,
    )![1]!
    expect(markup).toContain('name="status"')
    expect(markup).toContain(`lay-filter="${SWITCH_FILTER}"`)
    // 开关值必须是这一行的主键（不是平台 id、也不是 index）
    expect(markup).toContain('value="{{d.PlatformAutoReplySetId}}"')
    // 反向编码：`ReplyStatus == 0` 才 checked
    expect(markup).toContain('d.ReplyStatus == 0')
    // 代码真的注册了这个 filter
    expect(LAYUI.formHandlers.has(`switch(${SWITCH_FILTER})`)).toBe(true)
    // 行映射真的提供了这两个键
    const row = ruleToTableRow(ROW_ON)
    expect(row.PlatformAutoReplySetId).toBe(1)
    expect(row.ReplyStatus).toBe(0)
  })

  it('validatePanelValues / panelPayload：合法值 ⇒ 载荷形状正确（正文 trim）', () => {
    const values = {
      game: null,
      trigger: 0,
      triggerTitle: '接单后',
      content: '  合成话术  ',
      platforms: [SUPPORTED_PLATFORM],
    }
    expect(validatePanelValues(values)).toBe('')
    expect(panelPayload(values)).toEqual({
      platform: SUPPORTED_PLATFORM,
      game: null,
      trigger: 0,
      content: '合成话术',
    })
    // 本次上传了新图才带 `image_key`；不传 = PATCH 不动库里那张（⛔ 不发空串）。
    expect(panelPayload(values, 'tenant.test.com/platform-auto-replies/x.png')).toEqual({
      platform: SUPPORTED_PLATFORM,
      game: null,
      trigger: 0,
      content: '合成话术',
      image_key: 'tenant.test.com/platform-auto-replies/x.png',
    })
  })

  it('readPanelValues：从 DOM 读回游戏 / 时机 / 内容 / 平台', async () => {
    await mount()
    callToolbar('addBtn')
    setGame('11')
    chooseTrigger(10)
    fillContent('合成话术')
    setPlatforms([SUPPORTED_PLATFORM])
    expect(readPanelValues(panel())).toMatchObject({
      game: 11,
      trigger: 10,
      content: '合成话术',
      platforms: [SUPPORTED_PLATFORM],
    })
  })
})

/* ────────────────────────────── 与对标 cols 不漂移 ────────────────────────────── */

const titles = (cols: Record<string, unknown>[]): unknown[] => cols.map((col) => col.title)
const colsOf = (index: number): Record<string, unknown>[] => TABLES_FOR_TEST[index]!.cols[0]!
const lastCol = (index: number): Record<string, unknown> => {
  const cols = colsOf(index)
  return cols[cols.length - 1]!
}

describe('交易平台自动回复 的表格配置（对标 PlatformAutoReply.js 的 cols，减去专用人员两列）', () => {
  it('1 张表：elem / toolbar 与对标一一对应', () => {
    expect(TABLES_FOR_TEST.map((entry) => entry.elem)).toEqual(['#tableList'])
    expect(TABLES_FOR_TEST.map((entry) => entry.toolbar)).toEqual(['#toolbarDemo'])
  })

  it('#tableList：8 列，标题与顺序逐字对齐对标（专用人员两列按 2026-10-27 裁定删除）', () => {
    expect(titles(colsOf(0))).toEqual([
      undefined, // 首列 = 对标唯一那个 type:'checkbox'
      '限定游戏',
      '回复时机',
      // ⛔ 这里**没有**「专用人员类型」「专用人员名称」——业务方裁定不做
      '回复内容',
      '图片',
      '启用平台',
      '<span lay-tips="绿色代表启用时">启用状态</span>',
      '操作',
    ])
  })

  it('#tableList：列宽 / 对齐逐条对齐（未写 width 的是自动列 ⇒ undefined）', () => {
    expect(colsOf(0).map((col) => col.width)).toEqual([
      50,
      130,
      125,
      undefined,
      80,
      400,
      105,
      160,
    ])
    expect(colsOf(0).map((col) => col.align)).toEqual([
      undefined,
      'center',
      'center',
      'left',
      'left',
      'left',
      'center',
      'center',
    ])
  })

  it('#tableList：首列是对标的 checkbox（fixed left / width 50），操作列引用 #baroperating', () => {
    expect(colsOf(0)[0]).toMatchObject({ type: 'checkbox', width: 50, fixed: 'left' })
    expect(lastCol(0)).toMatchObject({ toolbar: '#baroperating', width: 160 })
  })

  it('#tableList：启用状态列引用证据件模板 #switchTpl1', () => {
    const status = colsOf(0).find(
      (col) => col.title === '<span lay-tips="绿色代表启用时">启用状态</span>',
    )
    expect(status).toBeDefined()
    expect(status!.templet).toBe('#switchTpl1')
  })

  it('表格级选项（page / defaultToolbar / height）逐条对齐对标', () => {
    expect(TABLES_FOR_TEST.map((entry) => entry.page)).toEqual([false])
    expect(TABLES_FOR_TEST.map((entry) => entry.defaultToolbar)).toEqual([['filter']])
    expect(TABLES_FOR_TEST.map((entry) => entry.height)).toEqual(['full'])
  })
})
