// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import type { PlatformIntegrationRow, PlatformPushSettings } from '@/api/platformIntegrations'

/**
 * 【设置第三方交易平台接】island 页（`pages/system-tripartiteplatformsetting.ts`）的**交互测试**。
 *
 * 这条测试**驱动真交互**，不是只断言标记存在：
 *  - 点击证据件里的「保存设置」按钮（`.saveInterfaceBtn`）→ 断言 `POST / PATCH` 的**载荷**；
 *  - 点击全局设置那颗 `lay-submit`（`saveBtn1`）→ 断言 `PUT` 的 9 个值（含 int/bool 的类型）；
 *  - 点击行内「支付密码」格子 → 断言 `layer.prompt` 的标题 + `PATCH` 载荷 + 表格重取；
 *  - 五颗行内开关各触发一次 layui `switch(...)` 事件 → 断言 `PATCH` 的布尔字段；
 *  - 工具栏刷新 / 行内修改 / 行内删除走 layui 的 `table.on` 事件 → 断言请求与界面终态。
 *
 * ## 为什么用**真证据件 HTML**而不是手写一张小标记
 *
 * 页面走 `mountConfigLike`：真的去 `fetch('replica/system-tripartiteplatformsetting.html')`
 * 再把标记注入。测试把 `fetch` 指向**磁盘上的证据件**，于是断言的 DOM 就是页面真正渲染的
 * 那一份（列模板 `#switchTpl1-5`、9 组 radio 的 `name/value` 全来自证据件，抄错就红）。
 *
 * ## 三条老坑（`AGENTS.md` §5-G / E-0061 / E-0067）
 *
 * 1. 类名与选择器分成两个常量（页模块里 `*_CLASS` + `*_SELECTOR`）；
 * 2. 以 `(` 开头的语句一律前置分号（本文件里那些 layui 调用点都这么写）；
 * 3. `bootstrapIsland` 必须先跑（挂样式）、再注入标记——顺序由共享零件
 *    `config-replica.ts::mountConfigLike` 保证；本文件用一个**调用序列**把它钉住。
 * ④ 本页**没有定时器**（无 `setInterval` / 轮询）⇒ 不需要 E-0198 的假定时器收口；
 *    这里显式登记，免得下一个人以为漏了。
 *
 * 手法照 `thirdparty-xgjshop.spec.ts`：`vi.mock` 掉 `bootstrapIsland`
 * （jsdom 里加载不了真 layui）与 api 模块，其余（DOM、证据件、事件）都是真的。
 * ⚠️ `vi.mock` 会被提升，`import` 写在它之后。
 */

const HERE = path.dirname(fileURLToPath(import.meta.url))
/** `src/frontend`（本文件位于 `src/frontend/src/layui-island/pages/__tests__/`）。 */
const FRONTEND_ROOT = path.resolve(HERE, '../../../..')
/** 页面真正会 fetch 的那份证据件（原样读盘，别手写替身）。 */
const EVIDENCE = readFileSync(
  path.resolve(FRONTEND_ROOT, 'public/replica/system-tripartiteplatformsetting.html'),
  'utf8',
)

/** layui 的**可控替身**：能像真 layui 一样搬节点、触发弹层、收事件。 */
const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  const tableHandlers = new Map<string, (obj: unknown) => unknown>()
  const formHandlers = new Map<string, (obj?: unknown) => unknown>()
  const tableRenders: Array<Record<string, unknown>> = []
  const reloads: Array<{ id: string; data: unknown }> = []
  const opens: Array<Record<string, unknown>> = []
  const prompts: Array<Record<string, unknown>> = []
  const confirms: string[] = []
  const formRenders: Array<string | undefined> = []
  /** 生命周期顺序（`bootstrap` 必须先于 `fetch-artifact`；两者都先于 `init`）。 */
  const calls: string[] = []
  /** `layer.prompt` 由测试设定的「用户输入」。 */
  const state = { promptValue: '' }

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
    prompts,
    confirms,
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
      prompt: vi.fn((options: Record<string, unknown>, onOk: (value: string, i: number) => void) => {
        prompts.push(options)
        onOk(state.promptValue, prompts.length)
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

vi.mock('@/api/platformIntegrations', () => ({
  fetchPlatformIntegrations: vi.fn(),
  createPlatformIntegration: vi.fn(),
  updatePlatformIntegration: vi.fn(),
  deletePlatformIntegration: vi.fn(),
  fetchPlatformPushSettings: vi.fn(),
  savePlatformPushSettings: vi.fn(),
}))

import {
  createPlatformIntegration,
  deletePlatformIntegration,
  fetchPlatformIntegrations,
  fetchPlatformPushSettings,
  savePlatformPushSettings,
  updatePlatformIntegration,
} from '@/api/platformIntegrations'
import page, {
  APP_ID_DIV_CLASS,
  APP_SECRET_DIV_CLASS,
  LOGIN_PASSWORD_DIV_CLASS,
  LOGIN_PASSWORD_NOT_OPEN_TEXT,
  PANEL_SELECTOR,
  PAY_PASSWORD_TRIGGER_SELECTOR,
  PLATFORM_OPTIONS,
  SETTINGS_GROUPS,
  SETTINGS_EFFECT_ATTR,
  SETTINGS_EFFECT_LEGEND,
  SETTINGS_EFFECT_OFF_CLASS,
  SETTINGS_EFFECT_OFF_TEXT,
  SETTINGS_EFFECT_ON_CLASS,
  SETTINGS_EFFECT_ON_TEXT,
  SETTINGS_NOTICE_ID,
  SETTINGS_NOTICE_TEXT,
  annotateSettingsEffects,
  TABLE_ELEM,
  TABLES_FOR_TEST,
  integrationToTableRow,
  payPasswordCell,
  platformFieldConfig,
  platformNameCell,
  settingsRadioPayload,
  type IntegrationTableRow,
} from '../system-tripartiteplatformsetting'

/* ────────────────────────────── 夹具（全部合成值）────────────────────────────── */

/** 一行代练通（有客户端、两个凭据都已配置）。 */
const ROW_DLT: PlatformIntegrationRow = {
  id: 1,
  platform: 10,
  platform_label: '代练通',
  account: 'acct-a',
  app_id: 'app-id-a',
  has_secret: true,
  has_password: true,
  is_enabled: true,
  is_default: true,
  sync_accept: true,
  sms_notify: false,
  include_push: true,
  contact_qq: 'qq-a',
  contact_phone: 'line-a',
  remark: '备注甲',
  is_available: true,
  created_at: '2026-10-26T10:00:00+08:00',
  updated_at: '2026-10-26T10:00:00+08:00',
}

/** 一行代练妈妈（**没有客户端**、两个凭据都没配、开关都在反方向）。 */
const ROW_MAMA: PlatformIntegrationRow = {
  ...ROW_DLT,
  id: 2,
  platform: 30,
  platform_label: '代练妈妈',
  account: 'acct-b',
  app_id: 'app-id-b',
  has_secret: false,
  has_password: false,
  is_enabled: false,
  is_default: false,
  sync_accept: false,
  sms_notify: true,
  include_push: false,
  contact_qq: 'qq-b',
  contact_phone: 'line-b',
  remark: '备注乙',
  is_available: false,
}

/** 9 组设置的后端真值（刻意与证据件的默认值都不同，回填漏了哪组一眼可见）。 */
const SETTINGS: PlatformPushSettings = {
  quick_push_enabled: false, // SK132 默认 Y ⇒ 这里是 N
  legacy_simple_mode: 1, // SK156 默认 0
  check_game_region: 0, // SK157 默认 0
  forbid_push_when_double_gold_zero: true, // SK107 默认 N
  duplicate_push_check: 1, // SK159 默认 0（检测）
  select_jidanpu_center_by_default: true, // SK106 默认 N
  auto_zero_refund_on_conflict: false, // SK145 默认 Y
  block_push_completed_orders: 1, // SK155 默认 0
  auto_send_group_qr_after_accept: 1, // SK158 默认 0
  // 实施计划 §2.4 新增的三列：刻意与默认值不同，回填漏了能一眼看出来。
  default_power_level: 3,
  default_limit_accept: 5,
  require_template: '后端模板甲',
}

/** 证据件里 9 组单选的 `name → 期望的 checked value`（逐条手抄 html:159-237）。 */
const EXPECTED_RADIO: Record<string, string> = {
  SK132: 'N',
  SK156: '1',
  SK157: '0',
  SK107: 'Y',
  SK159: '1',
  SK106: 'Y',
  SK145: 'N',
  SK155: '1',
  SK158: '1',
}

function flush(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

/** layui 的 `form` 模块把 `[lay-submit]` 的点击转成 `submit(filter)` 事件——替身补这一跳。 */
function onSubmitBridge(event: Event): void {
  const target = event.target as HTMLElement | null
  const button = target?.closest?.('[lay-submit]')
  if (!(button instanceof HTMLElement)) return
  const filter = button.getAttribute('lay-filter') ?? ''
  LAYUI.formHandlers.get(`submit(${filter})`)?.()
}

describe('设置第三方交易平台接 岛页（T3/T4）', () => {
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
    document.addEventListener('click', onSubmitBridge)
  })

  afterAll(() => {
    document.removeEventListener('click', onSubmitBridge)
    vi.unstubAllGlobals()
  })

  beforeEach(() => {
    vi.clearAllMocks()
    LAYUI.tableHandlers.clear()
    LAYUI.formHandlers.clear()
    LAYUI.tableRenders.length = 0
    LAYUI.reloads.length = 0
    LAYUI.opens.length = 0
    LAYUI.prompts.length = 0
    LAYUI.confirms.length = 0
    LAYUI.formRenders.length = 0
    LAYUI.state.promptValue = ''
    document.body.innerHTML = ''
    root = document.createElement('div')
    document.body.appendChild(root)

    vi.mocked(fetchPlatformIntegrations).mockResolvedValue([ROW_DLT, ROW_MAMA])
    vi.mocked(fetchPlatformPushSettings).mockResolvedValue({ ...SETTINGS })
    vi.mocked(createPlatformIntegration).mockResolvedValue(ROW_DLT)
    vi.mocked(updatePlatformIntegration).mockResolvedValue(ROW_DLT)
    vi.mocked(deletePlatformIntegration).mockResolvedValue(null)
    vi.mocked(savePlatformPushSettings).mockImplementation(async (payload) => ({
      ...SETTINGS,
      ...payload,
    }))
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

  function lastReloadRows(): IntegrationTableRow[] {
    const last = LAYUI.reloads[LAYUI.reloads.length - 1]
    if (!last) throw new Error('没有调用过 table.reloadData')
    return last.data as IntegrationTableRow[]
  }

  function callToolbar(event: string): void {
    const handler = LAYUI.tableHandlers.get('toolbar(tableList)')
    if (!handler) throw new Error('没有注册 toolbar(tableList)')
    handler({ event })
  }

  function callTool(event: string, row: IntegrationTableRow): void {
    const handler = LAYUI.tableHandlers.get('tool(tableList)')
    if (!handler) throw new Error('没有注册 tool(tableList)')
    handler({ event, data: row })
  }

  /** 当前打开的弹层节点（`layer.open` 已经把 `.modifyParamDiv` 搬进 layer content）。 */
  function panel(): HTMLElement {
    const node = document.querySelector<HTMLElement>(PANEL_SELECTOR)
    if (!node) throw new Error('弹层节点不存在（layer.open 没把 .modifyParamDiv 搬出来？）')
    return node
  }

  /** 像用户一样点那颗「保存设置」（弹层里的 `<a class="… saveInterfaceBtn">`）。 */
  function clickPanelSave(): void {
    const button = panel().querySelector('.saveInterfaceBtn')
    if (!(button instanceof HTMLElement)) throw new Error('弹层里没有 .saveInterfaceBtn')
    button.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  }

  /** 勾一个平台 radio，并触发 layui 的 `radio(platforms)` 事件（用户点击的真实两跳）。 */
  function selectPlatform(value: number): void {
    for (const radio of panel().querySelectorAll<HTMLInputElement>('input[name="platforms"]')) {
      radio.checked = radio.value === String(value)
    }
    const handler = LAYUI.formHandlers.get('radio(platforms)')
    if (!handler) throw new Error('没有注册 radio(platforms)')
    handler({ value: String(value) })
  }

  function fillPanel(name: string, value: string): void {
    const input = panel().querySelector<HTMLInputElement>(`input[name="${name}"]`)
    if (!input) throw new Error(`弹层里没有 input[name=${name}]`)
    input.value = value
  }

  /** 点 9 组设置的「保存设置」（`lay-submit`，经上面的 bridge 变成 `submit(saveBtn1)`）。 */
  function clickSettingsSave(): void {
    const button = root.querySelector('button[lay-filter="saveBtn1"]')
    if (!(button instanceof HTMLElement)) throw new Error('证据件里没有 saveBtn1 按钮')
    button.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  }

  /** 从**真证据件**里取一个 `<script type="text/html" id="…">` 模板的原文。 */
  function switchTemplate(id: string): string {
    const match = new RegExp(`<script type="text/html" id="${id}">([\\s\\S]*?)</script>`).exec(
      EVIDENCE,
    )
    if (!match?.[1]) throw new Error(`证据件里找不到模板 ${id}`)
    return match[1]
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
    expect(root.querySelector('input[name="SK132"]')).toBeInstanceOf(HTMLInputElement)
  })

  it('表格接真：API 行映射成证据件模板认得的形状，再 reloadData 上屏', async () => {
    await mount()
    expect(fetchPlatformIntegrations).toHaveBeenCalledTimes(1)
    const rows = lastReloadRows()
    expect(rows).toHaveLength(2)
    // 五个开关的**编码方向**逐条核（模板用 `== 0` / `== 1` / `== 'Y'` 判断 checked）
    expect(rows[0]).toMatchObject({
      platformName: '代练通',
      platformAcc: 'acct-a',
      appId: 'app-id-a',
      concactQQ: 'qq-a',
      concactPhone: 'line-a',
      status: 0,
      isDefault: 'Y',
      isAutoAccep: 0,
      isNotifyPushMan: 0,
      isAutoSelect: 0,
    })
    expect(rows[1]).toMatchObject({
      platformName: '代练妈妈',
      status: 1,
      isDefault: 'N',
      isAutoAccep: 1,
      isNotifyPushMan: 1,
      isAutoSelect: 1,
    })
    // 读模型里根本没有凭据字面值（后端 write_only）⇒ 也绝不带进表格数据。
    expect(Object.keys(rows[0]!)).not.toContain('app_secret')
    expect(Object.keys(rows[0]!)).not.toContain('pay_password')
    expect(Object.keys(rows[0]!)).not.toContain('pay_password_md5')
  })

  it('「支付密码」列是函数模板：已设置绿对勾、未设置「点击设置」（has_password 驱动）', () => {
    // 列配置是模块级常量，不需要挂载就能取到（这里断言的是**列定义**本身）。
    const column = TABLES_FOR_TEST[0]!.cols[0]!.find((col) => col.title === '支付密码')
    expect(column).toBeDefined()
    expect(typeof column!.templet).toBe('function')
    const templet = column!.templet as (row: IntegrationTableRow) => string

    const filled = templet(integrationToTableRow(ROW_DLT))
    expect(filled).toContain('layui-icon-ok-circle')
    expect(filled).toContain('color:#16b777')
    expect(filled).toContain('data-id="1"')
    expect(filled).toContain('点击可以更改')

    const empty = templet(integrationToTableRow(ROW_MAMA))
    expect(empty).toContain('layui-badge')
    expect(empty).toContain('点击设置')
    expect(empty).toContain('data-id="2"')
    expect(empty).not.toContain('layui-icon-ok-circle')
  })

  it('「暂未开通」只写在平台名后面（is_available=false），⛔ 不拦保存', async () => {
    await mount()
    const column = tableCols().find((col) => col.title === '平台名称')
    const templet = column!.templet as (row: IntegrationTableRow) => string
    expect(templet(integrationToTableRow(ROW_DLT))).not.toContain('暂未开通')
    const mama = templet(integrationToTableRow(ROW_MAMA))
    expect(mama).toContain('代练妈妈')
    expect(mama).toContain('暂未开通')

    // 选一个「暂未开通」的平台保存：请求要真的发出去（不拦），成功后提示里说明它没客户端。
    vi.mocked(createPlatformIntegration).mockResolvedValue(ROW_MAMA)
    callToolbar('addBtn')
    selectPlatform(30)
    fillPanel('plaftacc', 'acct-m')
    fillPanel('appId', 'app-id-m')
    clickPanelSave()
    await flush()
    expect(createPlatformIntegration).toHaveBeenCalledWith(
      expect.objectContaining({ platform: 30, account: 'acct-m', app_id: 'app-id-m' }),
    )
    expect(LAYUI.msg).toHaveBeenCalledWith(expect.stringContaining('该平台暂未开通'))
    expect(lastReloadRows()).toHaveLength(2)
  })

  // ── 新增 / 修改弹层 ───────────────────────────────────────

  it('新增：打开弹层 → 7 个平台 radio → 选平台改 label/显隐 → 点保存发 POST（含凭据）', async () => {
    await mount()
    callToolbar('addBtn')
    expect(LAYUI.opens).toHaveLength(1)
    expect(LAYUI.opens[0]).toMatchObject({
      title: '新增一个代练平台对接',
      area: ['676px', '620px'],
      skin: 'layui-layer-lan',
    })
    // 平台 radio：7 个、顺序与证据件一致
    const radios = Array.from(
      panel().querySelectorAll<HTMLInputElement>('input[name="platforms"]'),
    ).map((radio) => ({ value: Number(radio.value), title: radio.getAttribute('title') }))
    expect(radios).toEqual(PLATFORM_OPTIONS.map((option) => ({ ...option })))

    // 新增时没选平台：平台账号 label 用 resetaddmodifyform 的默认值，AppSecret 行隐藏
    expect(panel().querySelector<HTMLElement>('.platacclabel')!.textContent).toBe('平台用户ID')
    expect(panel().querySelector<HTMLElement>(`.${APP_SECRET_DIV_CLASS}`)!.style.display).toBe(
      'none',
    )

    // 选代练通：label 变「代练通用户ID」、AppSecret 行显示、登录密码行保持隐藏
    selectPlatform(10)
    expect(panel().querySelector<HTMLElement>('.platacclabel')!.textContent).toBe('代练通用户ID')
    expect(
      panel().querySelector<HTMLElement>(`.${APP_SECRET_DIV_CLASS}`)!.style.display,
    ).not.toBe('none')
    expect(
      panel().querySelector<HTMLElement>(`.${LOGIN_PASSWORD_DIV_CLASS}`)!.style.display,
    ).toBe('none')

    fillPanel('plaftacc', 'acct-new')
    fillPanel('appId', 'app-id-new')
    fillPanel('appSecret', 'synthetic-secret-new')
    fillPanel('plaftpaypwd', 'synthetic-pay-new')
    fillPanel('remark', '备注新')
    fillPanel('concactQQ', 'qq-new')
    fillPanel('concactPhone', 'line-new')
    clickPanelSave()
    await flush()

    expect(createPlatformIntegration).toHaveBeenCalledWith({
      platform: 10,
      account: 'acct-new',
      app_id: 'app-id-new',
      remark: '备注新',
      contact_qq: 'qq-new',
      contact_phone: 'line-new',
      app_secret: 'synthetic-secret-new',
      pay_password: 'synthetic-pay-new',
    })
    // 弹层关了、列表重取了、提示说成功
    expect(LAYUI.opens).toHaveLength(1)
    expect(LAYUI.layer.close).toHaveBeenCalledWith(1)
    expect(lastReloadRows()).toHaveLength(2)
    expect(LAYUI.msg).toHaveBeenCalledWith('已保存')
  })

  it('保存连点 ⇒ 只发一次 POST（按钮禁用 + submitting 旗标双保险）', async () => {
    await mount()
    callToolbar('addBtn')
    selectPlatform(10)
    fillPanel('plaftacc', 'acct-once')
    fillPanel('appId', 'app-id-once')
    let resolveCreate: (row: PlatformIntegrationRow) => void = () => undefined
    vi.mocked(createPlatformIntegration).mockImplementationOnce(
      () =>
        new Promise<PlatformIntegrationRow>((resolve) => {
          resolveCreate = resolve
        }),
    )
    clickPanelSave()
    const button = panel().querySelector<HTMLElement>('.saveInterfaceBtn')!
    expect(button.classList.contains('layui-btn-disabled')).toBe(true)
    // 第二下在途点击：不许再发一发 POST（否则同一平台多出一条重复账号）。
    clickPanelSave()
    clickPanelSave()
    expect(createPlatformIntegration).toHaveBeenCalledTimes(1)

    resolveCreate(ROW_DLT)
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith('已保存')
    expect(button.classList.contains('layui-btn-disabled')).toBe(false)
  })

  it('弹层里的支付密码纯空格 ⇒ 不提交 `pay_password`（⛔ 不把空格 MD5 成真密码）', async () => {
    await mount()
    callToolbar('addBtn')
    selectPlatform(10)
    fillPanel('plaftacc', 'acct-blank')
    fillPanel('appId', 'app-id-blank')
    fillPanel('plaftpaypwd', '   ')
    clickPanelSave()
    await flush()
    expect(createPlatformIntegration).toHaveBeenCalledTimes(1)
    const payload = vi.mocked(createPlatformIntegration).mock.calls[0]![0]
    expect(Object.keys(payload)).not.toContain('pay_password')
  })

  it('修改：预填真值、两个密码框永远为空，PATCH 不携带空凭据', async () => {
    await mount()
    callTool('submitEdit', integrationToTableRow(ROW_DLT))
    expect(LAYUI.opens[0]).toMatchObject({ title: '编辑对接' })
    expect(panel().querySelector<HTMLInputElement>('input[name="plaftacc"]')!.value).toBe('acct-a')
    expect(panel().querySelector<HTMLInputElement>('input[name="appId"]')!.value).toBe('app-id-a')
    expect(panel().querySelector<HTMLInputElement>('input[name="remark"]')!.value).toBe('备注甲')
    expect(panel().querySelector<HTMLInputElement>('input[name="concactQQ"]')!.value).toBe('qq-a')
    expect(panel().querySelector<HTMLInputElement>('input[name="concactPhone"]')!.value).toBe(
      'line-a',
    )
    // 凭据永不回显：AppSecret / 支付密码框**必须**是空串
    expect(panel().querySelector<HTMLInputElement>('input[name="appSecret"]')!.value).toBe('')
    expect(panel().querySelector<HTMLInputElement>('input[name="plaftpaypwd"]')!.value).toBe('')
    // 平台 radio 预选代练通 + 对应的 label
    expect(
      panel().querySelector<HTMLInputElement>('input[name="platforms"]:checked')!.value,
    ).toBe('10')
    expect(panel().querySelector<HTMLElement>('.platacclabel')!.textContent).toBe('代练通用户ID')

    fillPanel('plaftacc', 'acct-a2')
    fillPanel('remark', '备注甲改')
    clickPanelSave()
    await flush()
    expect(updatePlatformIntegration).toHaveBeenCalledWith(1, {
      platform: 10,
      account: 'acct-a2',
      app_id: 'app-id-a',
      remark: '备注甲改',
      contact_qq: 'qq-a',
      contact_phone: 'line-a',
    })
    // 空凭据连键都不该出现（后端对空串支付密码**会拒**）
    const payload = vi.mocked(updatePlatformIntegration).mock.calls[0]![1]
    expect(Object.keys(payload)).not.toContain('app_secret')
    expect(Object.keys(payload)).not.toContain('pay_password')
    expect(LAYUI.msg).toHaveBeenCalledWith('已保存')
  })

  it('弹层校验：没选平台 / 该平台要求 AppId 时零请求', async () => {
    await mount()
    callToolbar('addBtn')
    clickPanelSave()
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith('请选择一个平台')
    expect(createPlatformIntegration).not.toHaveBeenCalled()

    selectPlatform(10)
    fillPanel('plaftacc', 'acct-x')
    // AppId 留空
    clickPanelSave()
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith('AppId不能为空')
    expect(createPlatformIntegration).not.toHaveBeenCalled()

    // 93 对标不要求 AppId ⇒ 同一份表单换个平台就能过前端闸（后端兜底另算）
    selectPlatform(93)
    fillPanel('plaftacc', 'acct-y')
    clickPanelSave()
    await flush()
    expect(createPlatformIntegration).toHaveBeenCalledWith(
      expect.objectContaining({ platform: 93, account: 'acct-y', app_id: '' }),
    )
  })

  it('平台 50：「平台登录密码」照渲染但写明尚未开放；保存时不提交、并明确说出来', async () => {
    await mount()
    callToolbar('addBtn')
    selectPlatform(50)
    const loginDiv = panel().querySelector<HTMLElement>(`.${LOGIN_PASSWORD_DIV_CLASS}`)!
    expect(loginDiv.style.display).not.toBe('none')
    expect(loginDiv.textContent).toContain('尚未开放')
    expect(loginDiv.textContent).toContain('平台登录密码')
    // 对标 50 分支还会隐藏 AppId 那一格
    expect(panel().querySelector<HTMLElement>(`.${APP_ID_DIV_CLASS}`)!.style.display).toBe('none')
    expect(LOGIN_PASSWORD_NOT_OPEN_TEXT).toContain('尚未开放')

    fillPanel('plaftacc', 'acct-gulu')
    fillPanel('plaftloginpwd', 'synthetic-login-password')
    clickPanelSave()
    await flush()
    const payload = vi.mocked(createPlatformIntegration).mock.calls[0]![0]
    expect(payload).not.toHaveProperty('platform_login_password')
    expect(payload).not.toHaveProperty('login_password')
    expect(LAYUI.msg).toHaveBeenCalledWith(expect.stringContaining('平台登录密码」尚未开放'))
  })

  it('选其它平台：label 与显隐按对标分支走（30/80/90 商户标识、50 登录密码、93 User_Code）', () => {
    // 纯函数把这 7 个平台的生效分支一次钉住（值全部来自 tripartiteplatformsetting.js:233-328）
    expect(platformFieldConfig(10)).toMatchObject({
      accountLabel: '代练通用户ID',
      appIdLabel: 'AppId',
      appSecretLabel: 'AppSecret',
      showLoginPassword: false,
      showAppId: true,
      showAppSecret: true,
    })
    for (const platform of [30, 80, 90]) {
      expect(platformFieldConfig(platform)).toMatchObject({
        accountLabel: '平台用户ID',
        appIdLabel: '商户标识',
        appSecretLabel: '商户密钥',
        showLoginPassword: false,
        showAppId: true,
        showAppSecret: true,
      })
    }
    expect(platformFieldConfig(50)).toMatchObject({
      accountLabel: '平台账号',
      appSecretLabel: '对接秘钥',
      showLoginPassword: true,
      showAppId: false,
      showAppSecret: true,
    })
    expect(platformFieldConfig(60)).toMatchObject({
      appIdLabel: 'AppId',
      appSecretLabel: 'AppSecret',
      showLoginPassword: false,
      showAppId: true,
      showAppSecret: true,
    })
    expect(platformFieldConfig(93)).toMatchObject({
      accountLabel: 'User_Code',
      appSecretLabel: '对接密钥',
      showAppId: true,
      showAppSecret: true,
    })
    // 还没选平台（新增弹层初始态）：AppSecret 行隐藏（证据件 HTML 的初始状态）
    expect(platformFieldConfig(0)).toMatchObject({
      accountLabel: '平台用户ID',
      showAppSecret: false,
    })
  })

  it('删除：确认语逐字照对标 → DELETE → 重取列表 → 提示', async () => {
    await mount()
    callTool('submitDel', integrationToTableRow(ROW_DLT))
    expect(LAYUI.confirms[0]).toBe(
      '是否确认删除这个对接账号？提示：删除此对接账号不会影响之前用这个账号发过单的交易操作',
    )
    expect(deletePlatformIntegration).toHaveBeenCalledWith(1)
    await flush()
    expect(lastReloadRows()).toHaveLength(2)
    expect(LAYUI.msg).toHaveBeenCalledWith('已删除')
  })

  it('工具栏刷新：真的重取列表并重绘', async () => {
    await mount()
    expect(fetchPlatformIntegrations).toHaveBeenCalledTimes(1)
    vi.mocked(fetchPlatformIntegrations).mockResolvedValue([ROW_DLT])
    callToolbar('refresh')
    await flush()
    expect(fetchPlatformIntegrations).toHaveBeenCalledTimes(2)
    expect(lastReloadRows()).toHaveLength(1)
  })

  // ── 五个行内开关 ─────────────────────────────────────────

  it.each([
    ['staChecked', 'is_enabled'],
    ['defaultChecked', 'is_default'],
    ['isAutoAccepChecked', 'sync_accept'],
    ['isNotifyPushManChecked', 'sms_notify'],
    ['isAutoSelectChecked', 'include_push'],
  ])('开关 %s：切换后 PATCH { %s } 并重取列表', async (filter, field) => {
    await mount()
    const handler = LAYUI.formHandlers.get(`switch(${filter})`)
    if (!handler) throw new Error(`没有注册 switch(${filter})`)
    const input = document.createElement('input')
    input.type = 'checkbox'
    input.value = '1'
    input.checked = true
    root.appendChild(input)

    handler({ elem: input })
    await flush()
    expect(updatePlatformIntegration).toHaveBeenCalledWith(1, { [field]: true })
    expect(fetchPlatformIntegrations).toHaveBeenCalledTimes(2) // 首次取数 + 切换后重取
    expect(lastReloadRows()).toHaveLength(2)
  })

  it('开关失败：勾选态拨回、把后端文案说出来、再以服务端为准重取', async () => {
    await mount()
    vi.mocked(updatePlatformIntegration).mockRejectedValueOnce(new Error('默认账号设置失败'))
    const handler = LAYUI.formHandlers.get('switch(defaultChecked)')!
    const input = document.createElement('input')
    input.type = 'checkbox'
    input.value = '2'
    input.checked = true
    root.appendChild(input)

    handler({ elem: input })
    await flush()
    expect(input.checked).toBe(false)
    expect(LAYUI.msg).toHaveBeenCalledWith('默认账号设置失败')
    expect(fetchPlatformIntegrations).toHaveBeenCalledTimes(2)
  })

  it('证据件的 5 个开关模板 name / lay-filter / data-id 与代码里的键**恰好一致**', async () => {
    await mount()
    // 逐条来自证据件（HTML L20-49）；写错一个字母（事件名、字段名）都要红。
    const expected: Array<[id: string, name: string, filter: string]> = [
      ['switchTpl1', 'status', 'staChecked'],
      ['switchTpl5', 'isDefault', 'defaultChecked'],
      ['switchTpl2', 'isAutoAccep', 'isAutoAccepChecked'],
      ['switchTpl3', 'isNotifyPushMan', 'isNotifyPushManChecked'],
      ['switchTpl4', 'isAutoSelect', 'isAutoSelectChecked'],
    ]
    for (const [id, name, filter] of expected) {
      const markup = switchTemplate(id)
      expect(markup, `${id} 的字段名与代码不一致`).toContain(`name="${name}"`)
      expect(markup, `${id} 的 lay-filter 与代码不一致`).toContain(`lay-filter="${filter}"`)
      // 开关值必须是这一行的主键（不是平台 id、也不是 index）
      expect(markup).toContain('value="{{d.id}}"')
      expect(LAYUI.formHandlers.has(`switch(${filter})`), `没有注册 switch(${filter})`).toBe(true)
    }
    // 恰好这 5 个：多注册一个（例如 tpl 名拼错）也算不一致
    expect([...LAYUI.formHandlers.keys()].filter((key) => key.startsWith('switch(')).sort()).toEqual(
      expected.map(([, , filter]) => `switch(${filter})`).sort(),
    )
    // 列上的 `templet: '#switchTpl…'` 引用也必须恰好是这 5 个
    expect(
      tableCols()
        .map((col) => col.templet)
        .filter((templet): templet is string => typeof templet === 'string')
        .sort(),
    ).toEqual(expected.map(([id]) => `#${id}`).sort())
  })

  // ── 支付密码那一格 ──────────────────────────────────────

  it('点「点击设置」→ layer.prompt → PATCH 支付密码 → 重取列表', async () => {
    await mount()
    root.insertAdjacentHTML('beforeend', payPasswordCell(ROW_MAMA))
    const trigger = root.querySelector<HTMLElement>(PAY_PASSWORD_TRIGGER_SELECTOR)!
    LAYUI.state.promptValue = 'synthetic-pay-b'
    trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()

    expect(LAYUI.prompts[0]).toMatchObject({ title: '请填充平台的支付密码' })
    expect(updatePlatformIntegration).toHaveBeenCalledWith(2, { pay_password: 'synthetic-pay-b' })
    expect(LAYUI.layer.close).toHaveBeenCalledWith(1)
    expect(fetchPlatformIntegrations).toHaveBeenCalledTimes(2)
    expect(LAYUI.msg).toHaveBeenCalledWith('支付密码已设置')
  })

  it('支付密码只填空格：也按空处理（零请求 + 不能为空）', async () => {
    await mount()
    root.insertAdjacentHTML('beforeend', payPasswordCell(ROW_DLT))
    const trigger = root.querySelector<HTMLElement>(PAY_PASSWORD_TRIGGER_SELECTOR)!
    LAYUI.state.promptValue = '   '
    trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()
    expect(updatePlatformIntegration).not.toHaveBeenCalled()
    expect(LAYUI.msg).toHaveBeenCalledWith('支付密码不能为空')
  })

  it('支付密码留空：零请求，并提示不能为空', async () => {
    await mount()
    root.insertAdjacentHTML('beforeend', payPasswordCell(ROW_DLT))
    const trigger = root.querySelector<HTMLElement>(PAY_PASSWORD_TRIGGER_SELECTOR)!
    LAYUI.state.promptValue = ''
    trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()
    expect(updatePlatformIntegration).not.toHaveBeenCalled()
    expect(LAYUI.msg).toHaveBeenCalledWith('支付密码不能为空')
  })

  // ── T4：9 组全局设置 ────────────────────────────────────

  it('9 组单选渲染后端真值（把证据件的默认 checked 逐组清掉）', async () => {
    await mount()
    for (const [name, wanted] of Object.entries(EXPECTED_RADIO)) {
      const inputs = Array.from(
        root.querySelectorAll<HTMLInputElement>(`input[name="${name}"]`),
      )
      expect(inputs.length, `${name} 这一组在证据件里不存在`).toBeGreaterThan(0)
      const checked = inputs.filter((input) => input.checked).map((input) => input.value)
      expect(checked, `${name} 应只勾 ${wanted}`).toEqual([wanted])
    }
  })

  it('读设置失败：清空 9 组默认勾选 + 说出来（不拿证据件默认值冒充真值）', async () => {
    vi.mocked(fetchPlatformPushSettings).mockRejectedValueOnce(new Error('读取设置失败'))
    await mount()
    expect(LAYUI.msg).toHaveBeenCalledWith('读取设置失败')
    for (const name of Object.keys(EXPECTED_RADIO)) {
      const checked = Array.from(
        root.querySelectorAll<HTMLInputElement>(`input[name="${name}"]`),
      ).filter((input) => input.checked)
      expect(checked, `${name} 读失败后仍留着证据件的默认勾选`).toEqual([])
    }
  })

  it('设置未返回前点保存：零 PUT + 明确提示；返回后可正常保存（防默认值覆盖真配置）', async () => {
    let resolveSettings: ((value: PlatformPushSettings) => void) | undefined
    vi.mocked(fetchPlatformPushSettings).mockReturnValue(
      new Promise<PlatformPushSettings>((resolve) => {
        resolveSettings = resolve
      }),
    )
    await mount()
    clickSettingsSave()
    await flush()
    // ⛔ 证据件里那 9 个 `checked` 是**对标默认值**，绝不能在这个窗口里被 PUT 回去
    expect(savePlatformPushSettings).not.toHaveBeenCalled()
    expect(LAYUI.msg).toHaveBeenCalledWith(expect.stringContaining('设置还没读取完成'))

    resolveSettings?.({ ...SETTINGS })
    await flush()
    clickSettingsSave()
    await flush()
    expect(savePlatformPushSettings).toHaveBeenCalledTimes(1)
    expect(LAYUI.msg).toHaveBeenCalledWith('已保存')
  })

  it('F6：整块声明改成「第 4 / 6 条已生效」，并带上「已生效 / 仅保存」图例', async () => {
    await mount()
    const notice = root.querySelector<HTMLElement>(`#${SETTINGS_NOTICE_ID}`)
    expect(notice).toBeInstanceOf(HTMLElement)
    expect(notice!.textContent).toContain(SETTINGS_NOTICE_TEXT)
    expect(notice!.textContent).toContain('平台接手后自动发群二维码')
    // ⭐ H3-1：撞单 0 撤那一组也真生效了，声明里必须点名。
    expect(notice!.textContent).toContain('撞单自动 0 撤')
    expect(notice!.textContent).toContain(SETTINGS_EFFECT_LEGEND)
    // ⛔ 上一版那句「本版仅保存，不影响发单」已不成立（已经有两条真生效）。
    expect(notice!.textContent).not.toContain('本版仅保存')
  })

  it('F6：9 组**逐组**标注生效范围——第 4 / 6 组「已生效」，其余 7 组「仅保存」', async () => {
    await mount()
    const badges = Array.from(
      root.querySelectorAll<HTMLElement>(`[${SETTINGS_EFFECT_ATTR}]`),
    )
    // 恰好 9 个（每组一个；⛔ 不能漏组、也不能多标）。
    expect(badges).toHaveLength(9)
    const map: Record<string, string> = {}
    for (const badge of badges) {
      map[badge.getAttribute(SETTINGS_EFFECT_ATTR)!] = badge.textContent ?? ''
    }
    expect(map).toEqual({
      SK132: SETTINGS_EFFECT_OFF_TEXT,
      SK156: SETTINGS_EFFECT_OFF_TEXT,
      SK157: SETTINGS_EFFECT_OFF_TEXT,
      SK107: SETTINGS_EFFECT_OFF_TEXT,
      SK159: SETTINGS_EFFECT_OFF_TEXT,
      SK106: SETTINGS_EFFECT_OFF_TEXT,
      // ⭐ H3-1：第 4 条（撞单 0 撤）2026-10-28 起真生效。
      SK145: SETTINGS_EFFECT_ON_TEXT,
      SK155: SETTINGS_EFFECT_OFF_TEXT,
      // 第 6 条（平台接手后自动发群二维码）2026-10-27 起真生效。
      SK158: SETTINGS_EFFECT_ON_TEXT,
    })
    const on = badges.filter((badge) => badge.textContent === SETTINGS_EFFECT_ON_TEXT)
    const off = badges.filter((badge) => badge.textContent === SETTINGS_EFFECT_OFF_TEXT)
    expect(on).toHaveLength(2)
    expect(off).toHaveLength(7)
    expect(on.every((badge) => badge.classList.contains(SETTINGS_EFFECT_ON_CLASS))).toBe(true)
    expect(off.every((badge) => badge.classList.contains(SETTINGS_EFFECT_OFF_CLASS))).toBe(true)
    // 标注插在每组说明文字**前面**（用户先看到「已生效/仅保存」再读说明）。
    for (const name of ['SK145', 'SK158']) {
      const item = root.querySelector<HTMLElement>(`input[name="${name}"]`)!.closest('.layui-form-item')!
      const description = item.querySelector<HTMLElement>('.layui-input-block')!
      expect(description.firstElementChild?.textContent).toBe(SETTINGS_EFFECT_ON_TEXT)
    }
  })

  it('F6：标注函数可重复调用（幂等，不会叠出第二个徽标）', async () => {
    await mount()
    annotateSettingsEffects(root)
    annotateSettingsEffects(root)
    expect(root.querySelectorAll(`[${SETTINGS_EFFECT_ATTR}]`)).toHaveLength(9)
  })

  it('9 组设置保存连点 ⇒ 只发一次 PUT（提交中闸 + 按钮禁用）', async () => {
    await mount()
    let resolveSave: (value: PlatformPushSettings) => void = () => undefined
    vi.mocked(savePlatformPushSettings).mockImplementationOnce(
      () =>
        new Promise<PlatformPushSettings>((resolve) => {
          resolveSave = resolve
        }),
    )
    clickSettingsSave()
    const button = root.querySelector<HTMLElement>('button[lay-filter="saveBtn1"]')!
    expect(button.classList.contains('layui-btn-disabled')).toBe(true)
    clickSettingsSave()
    clickSettingsSave()
    expect(savePlatformPushSettings).toHaveBeenCalledTimes(1)
    resolveSave({ ...SETTINGS })
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith('已保存')
    expect(button.classList.contains('layui-btn-disabled')).toBe(false)
  })

  it('保存 9 组：点真的 lay-submit → PUT 全量载荷（布尔 / 整数类型正确）→ 按后端回值回填', async () => {
    await mount()
    // 改第 1 组（N → Y）
    const yes = root.querySelector<HTMLInputElement>('input[name="SK132"][value="Y"]')!
    const no = root.querySelector<HTMLInputElement>('input[name="SK132"][value="N"]')!
    no.checked = false
    yes.checked = true

    vi.mocked(savePlatformPushSettings).mockResolvedValue({
      ...SETTINGS,
      quick_push_enabled: true,
    })
    clickSettingsSave()
    await flush()

    expect(savePlatformPushSettings).toHaveBeenCalledWith({
      quick_push_enabled: true,
      legacy_simple_mode: 1,
      check_game_region: 0,
      forbid_push_when_double_gold_zero: true,
      duplicate_push_check: 1,
      select_jidanpu_center_by_default: true,
      auto_zero_refund_on_conflict: false,
      block_push_completed_orders: 1,
      auto_send_group_qr_after_accept: 1,
    })
    // 类型不能糊成字符串（后端 ChoiceField 收整数）
    const payload = vi.mocked(savePlatformPushSettings).mock.calls[0]![0]
    expect(typeof payload.legacy_simple_mode).toBe('number')
    expect(typeof payload.quick_push_enabled).toBe('boolean')
    // 终态：以后端回的那份为准回填 + 提示
    expect(root.querySelector<HTMLInputElement>('input[name="SK132"][value="Y"]')!.checked).toBe(
      true,
    )
    expect(LAYUI.formRenders).toContain('radio')
    expect(LAYUI.msg).toHaveBeenCalledWith('已保存')
  })

  it('保存成功但列表刷新失败：两条文案分开说，⛔ 不误报成「保存失败」', async () => {
    await mount()
    vi.mocked(fetchPlatformIntegrations).mockRejectedValueOnce(new Error('列表挂了'))
    callToolbar('addBtn')
    selectPlatform(10)
    fillPanel('plaftacc', 'acct-z')
    fillPanel('appId', 'app-id-z')
    clickPanelSave()
    await flush()

    expect(createPlatformIntegration).toHaveBeenCalled()
    // 同一句里既说保存成功、又说刷新失败；不许出现「保存失败」这种会让人重复提交的误导
    expect(LAYUI.msg).toHaveBeenCalledWith(expect.stringContaining('已保存'))
    expect(LAYUI.msg).toHaveBeenCalledWith(expect.stringContaining('列表刷新失败'))
    expect(LAYUI.msg).not.toHaveBeenCalledWith(expect.stringContaining('保存失败'))
  })

  it('篡改值被拒：把第 1.1 组改成 9 → PUT 真的发 9 → 后端带字段名文案原样显示（不吞、不改写成成功）', async () => {
    await mount()
    // 用户侧/调试侧把一个非法值塞进 DOM（模拟"篡改"）：页面**不静默纠正**，照发，让后端拒。
    for (const input of root.querySelectorAll<HTMLInputElement>('input[name="SK156"]')) {
      input.checked = false
    }
    const tampered = document.createElement('input')
    tampered.type = 'radio'
    tampered.name = 'SK156'
    tampered.value = '9'
    tampered.checked = true
    root.appendChild(tampered)
    vi.mocked(savePlatformPushSettings).mockRejectedValueOnce(
      new Error('传统简易模式只能是 0（否）或 1（是）'),
    )
    clickSettingsSave()
    await flush()
    expect(savePlatformPushSettings).toHaveBeenCalledWith(
      expect.objectContaining({ legacy_simple_mode: 9 }),
    )
    expect(LAYUI.msg).toHaveBeenCalledWith('传统简易模式只能是 0（否）或 1（是）')
    // ⛔ 不允许"本地改一改就提示成功"：成功文案一条都没出现
    expect(LAYUI.msg).not.toHaveBeenCalledWith('已保存')
  })

  it('9 组单选缺一组时保存直接报错、零请求（不自造默认值糊过去）', () => {
    const holder = document.createElement('div')
    holder.innerHTML =
      '<input type="radio" name="SK132" value="Y" checked>' +
      '<input type="radio" name="SK156" value="0" checked>'
    expect(() => settingsRadioPayload(holder)).toThrow('SK157')
  })
})

/* ────────────────────────────── 与对标 cols 不漂移（原防漂移测试保留） ────────────────────────────── */

const titles = (cols: Record<string, unknown>[]): unknown[] => cols.map((col) => col.title)
const table = (index: number): { cols: [Record<string, unknown>[]] } => TABLES_FOR_TEST[index]!
const colsOf = (index: number): Record<string, unknown>[] => table(index).cols[0]!
const lastCol = (index: number): Record<string, unknown> => {
  const cols = colsOf(index)
  return cols[cols.length - 1]!
}

describe('设置第三方交易平台接 的表格配置（对标 tripartiteplatformsetting.js L66-111）', () => {
  it('1 张表：elem / toolbar 与对标一一对应', () => {
    expect(TABLES_FOR_TEST.map((entry) => entry.elem)).toEqual(['#tableList'])
    expect(TABLES_FOR_TEST.map((entry) => entry.toolbar)).toEqual(['#toolbarDemo'])
  })

  it('#tableList：13 列，标题与顺序逐字对齐对标', () => {
    expect(titles(colsOf(0))).toEqual([
      '平台名称',
      '平台账号/ID',
      'AppId',
      '支付密码',
      '是否启用',
      '默认账号',
      '同步验收',
      '接单短信通知',
      '发单包含',
      '发单联系QQ',
      '发单联系电话',
      '备注说明',
      '操作',
    ])
  })

  it('#tableList：列宽 / 对齐逐条对齐', () => {
    expect(colsOf(0).map((col) => col.width)).toEqual([
      105, 170, 150, 100, 100, 100, 100, 105, 100, 120, 120, 100, 170,
    ])
    expect(colsOf(0).map((col) => col.align)).toEqual([
      'center',
      'left',
      'left',
      'center',
      'center',
      'center',
      'center',
      'center',
      'center',
      'left',
      'left',
      'left',
      'center',
    ])
  })

  it('#tableList：5 个开关列引用证据件模板 + 操作列引用 #baroperating', () => {
    const byTitle = (title: string): Record<string, unknown> =>
      colsOf(0).find((col) => col.title === title)!
    expect(byTitle('是否启用').templet).toBe('#switchTpl1')
    expect(byTitle('默认账号').templet).toBe('#switchTpl5')
    expect(byTitle('同步验收').templet).toBe('#switchTpl2')
    expect(byTitle('接单短信通知').templet).toBe('#switchTpl3')
    expect(byTitle('发单包含').templet).toBe('#switchTpl4')
    expect(lastCol(0)).toMatchObject({ toolbar: '#baroperating', width: 170 })
  })

  it('表格级选项（page / defaultToolbar / height）逐条对齐对标', () => {
    expect(TABLES_FOR_TEST.map((entry) => entry.page)).toEqual([false])
    expect(TABLES_FOR_TEST.map((entry) => entry.defaultToolbar)).toEqual([['filter']])
    expect(TABLES_FOR_TEST.map((entry) => entry.height)).toEqual(['full'])
  })

  it('9 组设置的映射是**恰好 9 组**、覆盖证据件的 9 个 name', () => {
    expect(SETTINGS_GROUPS.map((group) => group.name)).toEqual([
      'SK132',
      'SK156',
      'SK157',
      'SK107',
      'SK159',
      'SK106',
      'SK145',
      'SK155',
      'SK158',
    ])
    // 平台名徽标：可用平台不加「暂未开通」，不可用平台要加
    expect(platformNameCell(ROW_DLT)).not.toContain('暂未开通')
    expect(platformNameCell(ROW_MAMA)).toContain('暂未开通')
  })
})
