// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { OrderTemplateRow } from '@/api/dailiantongExtra'

/**
 * 【发平台设置 → 代练通订单要求模板】island 页的契约测试（dl-D，2026-10-27）。
 *
 * 这一页是**自有功能**（对标物里没有这一页），没有证据件可对拍；本文件钉的是
 * **有真实后果**的约定（`AGENTS.md` §5-G 的「自建 island 页必须有驱动真交互的页测试」）：
 *
 * 1. **挂载即请求** + 字段来自响应（标题 / 内容 / 默认徽标都断言具体值）；
 * 2. **三态可见**：加载中 / 空态 / 失败态（后端中文原样上屏 + 重试）；
 * 3. **增 / 改 / 删 / 设默认**：载荷恰好、连点只发一次（提交中按钮禁用）、成功刷新列表；
 * 4. **删除 / 设默认必须先过确认框**（用户取消 ⇒ 零请求）；
 * 5. **失败原样显示后端中文**（`layer.msg` + 表单旁常驻红字）；
 * 6. **四条 island 陷阱**（类名 / 选择器两个常量、挂载顺序按运行期 DOM 判）。
 *
 * ## 手法（照 `self-registration.spec.ts`）
 *
 * - `vi.mock('@/layui/island')`：jsdom 里加载不了真 layui ⇒ `layer.confirm` / `layer.msg`
 *   换成可控替身；`confirm` 默认**同步确认**，取消分支自己换掉实现（不调 cb）。
 * - `vi.mock('@/api/http')`：只换**网络出口**——`@/api/dailiantongExtra` 跑真实现，
 *   所以「打到哪条 URL、body 里是什么键」这两条判据才有意义。
 *
 * ⚠️ 夹具一律**合成值**（E-0077）：模板标题 / 内容都是编的，不是任何真租户的数据。
 */
const LAYUI = vi.hoisted(() => {
  const messages: string[] = []
  const confirms: Array<{ text: string; title: unknown }> = []
  // ⚠️ 挂载顺序的**运行期**判据（§5-G 陷阱 ③）。探针用新增按钮 id（在 buildHtml 里）。
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
      confirm: vi.fn((text: string, opts: { title?: unknown }, cb: (index: number) => void) => {
        confirms.push({ text, title: opts?.title })
        cb(101)
      }),
      close: vi.fn(),
    },
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => {
    LAYUI.seenByBootstrap.push(document.querySelector('#dltTplCreate') !== null)
    return { layer: LAYUI.layer }
  }),
  initIslandElements: vi.fn(() => {
    LAYUI.seenByInit.push(document.querySelector('#dltTplCreate') !== null)
  }),
  islandMsg: (_layer: unknown, text: string) => {
    LAYUI.messages.push(text)
  },
}))

/** 只换**网络出口**（见文件头）。`request()` 返回已解包的 `data`。 */
vi.mock('@/api/http', () => ({ request: vi.fn(), CSRF_HEADER_NAME: 'X-CSRFToken' }))
vi.mock('@/api/auth', () => ({ fetchMe: vi.fn() }))

import { fetchMe } from '@/api/auth'
import {
  DAILIANTONG_EXTRA_NODE,
  DAILIANTONG_ORDER_TEMPLATES_URL,
  orderTemplateDefaultUrl,
  orderTemplateDetailUrl,
  type OrderTemplateList,
} from '@/api/dailiantongExtra'
import { request } from '@/api/http'
import { ROUTE_NODE_CODES } from '@/config/routePermissions'
import page, {
  ACTION_ATTR,
  ACTION_ID_ATTR,
  ACTION_MESSAGE_ID,
  CANCEL_BUTTON_ID,
  CONTENT_INPUT_ID,
  CREATE_BUTTON_ID,
  DEFAULT_CHECKBOX_ID,
  MESSAGES,
  PAGE_KEY,
  PANEL_SELECTOR,
  RETRY_SELECTOR,
  SAVE_BUTTON_ID,
  STATUS_ID,
  TABLE_BODY_ID,
  TITLE_INPUT_ID,
  __testables,
} from '../dailiantong-order-templates'

const mockedRequest = vi.mocked(request)
const mockedFetchMe = vi.mocked(fetchMe)

/** 一次 HTTP 请求的入参（我们只关心这三个）。 */
type Cfg = { method?: string; url?: string; params?: unknown; data?: unknown }

/** 身份夹具：持 `order.list` 的**非管理员**（管理员会短路放行，钉不住节点门）。 */
const KEFU = {
  id: 7,
  username: 'kefu-a',
  display_name: '客服甲',
  identity_plane: 'tenant_a',
  is_tenant_admin: false,
  roles: [] as string[],
  permissions: [] as string[],
  menu_permissions: [DAILIANTONG_EXTRA_NODE],
}

const ROW_A: OrderTemplateRow = {
  id: 7001,
  title: '模板甲',
  temp: '内容甲',
  is_default: false,
}
const ROW_B: OrderTemplateRow = {
  id: 7002,
  title: '模板乙',
  temp: '内容乙',
  is_default: true,
}

function listFixture(): OrderTemplateList {
  return { items: [ROW_A, ROW_B], record_count: 2 }
}

function flush(rounds = 6): Promise<void> {
  return (async () => {
    for (let i = 0; i < rounds; i += 1) await new Promise((resolve) => setTimeout(resolve, 0))
  })()
}

/** 按 `METHOD URL` 造一次响应路由；**没有备好的响应就抛**（⛔ 不是静默回 undefined）。 */
function respond(routes: Record<string, unknown>): void {
  mockedRequest.mockImplementation((config: Cfg) => {
    const key = `${(config.method ?? 'get').toUpperCase()} ${config.url ?? ''}`
    if (!(key in routes)) throw new Error(`用例没有备 ${key} 的响应`)
    const value = routes[key]
    if (typeof value === 'function') return (value as () => Promise<never>)()
    return Promise.resolve(value)
  })
}

function requests(): string[] {
  return mockedRequest.mock.calls.map((call) => {
    const cfg = call[0] as Cfg
    return `${(cfg.method ?? 'get').toUpperCase()} ${cfg.url}`
  })
}

function findConfig(method: string, url: string): Cfg | undefined {
  return mockedRequest.mock.calls
    .map((call) => call[0] as Cfg)
    .find((cfg) => (cfg.method ?? 'get').toUpperCase() === method && cfg.url === url)
}

let root: HTMLElement

function click(el: HTMLElement): void {
  el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
}

function statusText(): string {
  return root.querySelector<HTMLElement>(`#${STATUS_ID}`)?.textContent ?? ''
}

function actionText(): string {
  return root.querySelector<HTMLElement>(`#${ACTION_MESSAGE_ID}`)?.textContent ?? ''
}

function button(action: string, id: number): HTMLElement {
  const node = root.querySelector<HTMLElement>(
    `[${ACTION_ATTR}="${action}"][${ACTION_ID_ATTR}="${id}"]`,
  )
  if (!node) throw new Error(`页面上没有 ${action} 动作（id=${id}）`)
  return node
}

function setInput(id: string, value: string): void {
  const input = root.querySelector<HTMLInputElement | HTMLTextAreaElement>(`#${id}`)
  if (!input) throw new Error(`页面上没有 ${id}`)
  input.value = value
}

function setDefaultChecked(checked: boolean): void {
  const input = root.querySelector<HTMLInputElement>(`#${DEFAULT_CHECKBOX_ID}`)
  if (!input) throw new Error('页面上没有「是否默认」勾选框')
  input.checked = checked
}

beforeEach(() => {
  vi.clearAllMocks()
  // ⚠️ `clearAllMocks` 只清调用记录、不还原实现：取消分支换过的 mockImplementation 会残留。
  LAYUI.layer.confirm.mockImplementation(
    (text: string, opts: { title?: unknown }, cb: (index: number) => void) => {
      LAYUI.confirms.push({ text, title: opts?.title })
      cb(101)
    },
  )
  LAYUI.messages.length = 0
  LAYUI.confirms.length = 0
  LAYUI.seenByBootstrap.length = 0
  LAYUI.seenByInit.length = 0
  document.body.innerHTML = ''
  root = document.createElement('div')
  document.body.appendChild(root)
  mockedFetchMe.mockResolvedValue(KEFU)
  respond({ [`GET ${DAILIANTONG_ORDER_TEMPLATES_URL}`]: listFixture() })
})

afterEach(() => {
  document.body.innerHTML = ''
})

async function mount(): Promise<void> {
  await page.mount(root, { onNavigate: () => {} })
  await flush()
}

describe('挂载：三条 island 陷阱、门禁、挂载即请求', () => {
  it('先 bootstrapIsland、再塞标记、最后 initIslandElements（运行期 DOM 判，不是读源码）', async () => {
    await mount()
    expect(LAYUI.seenByBootstrap).toEqual([false])
    expect(LAYUI.seenByInit).toEqual([true])
    expect(root.querySelector(PANEL_SELECTOR)).not.toBeNull()
  })

  it('① 类名与选择器是两个常量（`class=".x"` 会永远匹配不到）', () => {
    const host = document.createElement('div')
    host.innerHTML = __testables.buildHtml()
    expect(host.querySelector(PANEL_SELECTOR)).not.toBeNull()
    expect(host.innerHTML).not.toContain('class=".')
  })

  it('节点码与 `config/routePermissions.ts` 的登记同值', () => {
    expect(DAILIANTONG_EXTRA_NODE).toBe('order.list')
    expect(ROUTE_NODE_CODES[PAGE_KEY]).toBe(DAILIANTONG_EXTRA_NODE)
  })

  it('没有节点时**不注入标记、不发业务请求**（深链自检）', async () => {
    mockedFetchMe.mockResolvedValue({ ...KEFU, menu_permissions: [] })
    await page.mount(root, { onNavigate: () => {} })
    await flush()
    expect(LAYUI.messages).toContain(MESSAGES.noAccess)
    expect(root.querySelector(PANEL_SELECTOR)).toBeNull()
    expect(requests()).toEqual([])
  })

  it('挂载即取列表，标题 / 内容 / 默认徽标都来自响应', async () => {
    await mount()
    expect(requests()).toEqual([`GET ${DAILIANTONG_ORDER_TEMPLATES_URL}`])
    const body = root.querySelector<HTMLElement>(`#${TABLE_BODY_ID}`)?.textContent ?? ''
    expect(body).toContain(ROW_A.title)
    expect(body).toContain(ROW_A.temp)
    expect(body).toContain('模板乙')
    // 默认的就画徽标、不再画「设为默认」；非默认的画按钮。
    expect(body).toContain('默认')
    expect(root.querySelector(`[${ACTION_ATTR}="default"][${ACTION_ID_ATTR}="7001"]`)).not.toBeNull()
    expect(root.querySelector(`[${ACTION_ATTR}="default"][${ACTION_ID_ATTR}="7002"]`)).toBeNull()
  })
})

describe('三态：加载中 / 空 / 失败（⛔ 不静默空白）', () => {
  it('请求还没回来时状态条写着「加载中…」', async () => {
    let resolveList!: (value: OrderTemplateList) => void
    mockedRequest.mockImplementation(
      () =>
        new Promise<OrderTemplateList>((resolve) => {
          resolveList = resolve
        }),
    )
    const mounting = page.mount(root, { onNavigate: () => {} })
    await flush(4)
    expect(statusText()).toBe(MESSAGES.loading)
    resolveList(listFixture())
    await mounting
    await flush(2)
    expect(root.querySelector<HTMLElement>(`#${TABLE_BODY_ID}`)?.textContent).toContain('模板甲')
  })

  it('空结果 ⇒ 状态条说「还没有模板」', async () => {
    respond({
      [`GET ${DAILIANTONG_ORDER_TEMPLATES_URL}`]: {
        items: [],
        record_count: 0,
      } satisfies OrderTemplateList,
    })
    await mount()
    expect(statusText()).toBe(MESSAGES.empty)
    expect(root.querySelector<HTMLElement>(`#${TABLE_BODY_ID}`)?.textContent).toContain(MESSAGES.empty)
  })

  it('失败 ⇒ 后端中文**原样**上屏 + 一颗「重试」；点重试成功后铺出数据', async () => {
    const failure = '读取代练通订单要求模板失败（上游提示：账号未配置）。'
    respond({
      [`GET ${DAILIANTONG_ORDER_TEMPLATES_URL}`]: () => Promise.reject(new Error(failure)),
    })
    await mount()
    expect(statusText()).toContain(`${MESSAGES.failed}：${failure}`)
    expect(root.querySelector(RETRY_SELECTOR)).not.toBeNull()

    respond({ [`GET ${DAILIANTONG_ORDER_TEMPLATES_URL}`]: listFixture() })
    click(root.querySelector<HTMLElement>(RETRY_SELECTOR)!)
    await flush()
    expect(requests()).toHaveLength(2)
    expect(statusText()).toContain('共 2 条模板')
  })
})

describe('新增：载荷恰好、连点只发一次、成功刷新', () => {
  it('新增载荷 = `{title, temp, is_default}`；提交中按钮禁用、连点只发一次；成功后重新取列表', async () => {
    let resolveCreate!: (value: OrderTemplateRow) => void
    mockedRequest.mockImplementation((config: Cfg) => {
      const key = `${(config.method ?? 'get').toUpperCase()} ${config.url ?? ''}`
      if (key === `GET ${DAILIANTONG_ORDER_TEMPLATES_URL}`) {
        return Promise.resolve(listFixture())
      }
      if (key === `POST ${DAILIANTONG_ORDER_TEMPLATES_URL}`) {
        return new Promise<OrderTemplateRow>((resolve) => {
          resolveCreate = resolve
        })
      }
      throw new Error(`用例没有备 ${key} 的响应`)
    })

    await mount()
    setInput(TITLE_INPUT_ID, '模板丙')
    setInput(CONTENT_INPUT_ID, '内容丙')
    setDefaultChecked(true)

    const createBtn = root.querySelector<HTMLButtonElement>(`#${CREATE_BUTTON_ID}`)!
    click(createBtn)
    await flush(2)

    const post = findConfig('POST', DAILIANTONG_ORDER_TEMPLATES_URL)
    expect(post, '应当发过一条 POST').toBeDefined()
    expect(post!.data).toEqual({ title: '模板丙', temp: '内容丙', is_default: true })
    expect(createBtn.disabled).toBe(true)
    expect(requests().filter((key) => key.startsWith('POST '))).toHaveLength(1)

    // 连点：提交中按钮虽被禁用，仍直接派发 click，闸门必须挡住（只发一次）。
    click(createBtn)
    await flush(2)
    expect(requests().filter((key) => key.startsWith('POST '))).toHaveLength(1)

    // 写后回读：GET 再发一次，列表里出现新模板。
    respond({
      [`GET ${DAILIANTONG_ORDER_TEMPLATES_URL}`]: {
        items: [ROW_A, ROW_B, { id: 7003, title: '模板丙', temp: '内容丙', is_default: true }],
        record_count: 3,
      } satisfies OrderTemplateList,
    })
    resolveCreate({ id: 7003, title: '模板丙', temp: '内容丙', is_default: true })
    await flush()
    expect(requests().filter((key) => key === `GET ${DAILIANTONG_ORDER_TEMPLATES_URL}`)).toHaveLength(2)
    expect(statusText()).toContain('共 3 条模板')
    expect(actionText()).toBe(MESSAGES.created)
    expect(createBtn.disabled).toBe(false)
    // 成功后表单复位（新建态）。
    expect(root.querySelector<HTMLInputElement>(`#${TITLE_INPUT_ID}`)?.value).toBe('')
    expect(root.querySelector<HTMLButtonElement>(`#${SAVE_BUTTON_ID}`)?.hidden).toBe(true)
  })

  it('标题 / 内容为空 ⇒ 就地提示带字段名，**零请求**', async () => {
    await mount()
    setInput(TITLE_INPUT_ID, '')
    setInput(CONTENT_INPUT_ID, '')
    click(root.querySelector<HTMLElement>(`#${CREATE_BUTTON_ID}`)!)
    await flush()
    expect(requests()).toHaveLength(1)
    expect(LAYUI.messages).toContain(MESSAGES.needTitle)
  })

  it('写失败 ⇒ 后端中文原样显示（msg + 常驻红字），按钮恢复可用', async () => {
    const failure = '添加失败，每人最多添加10个模板。'
    respond({
      [`GET ${DAILIANTONG_ORDER_TEMPLATES_URL}`]: listFixture(),
      [`POST ${DAILIANTONG_ORDER_TEMPLATES_URL}`]: () => Promise.reject(new Error(failure)),
    })
    await mount()
    setInput(TITLE_INPUT_ID, '模板丙')
    setInput(CONTENT_INPUT_ID, '内容丙')
    const createBtn = root.querySelector<HTMLButtonElement>(`#${CREATE_BUTTON_ID}`)!
    click(createBtn)
    await flush()
    expect(LAYUI.messages).toContain(failure)
    expect(actionText()).toBe(`${MESSAGES.actionFailed}：${failure}`)
    expect(createBtn.disabled).toBe(false)
  })
})

describe('修改：回填、载荷、成功刷新', () => {
  it('点「编辑」回填表单；保存发 PATCH（三项都发）并刷新列表', async () => {
    respond({
      [`GET ${DAILIANTONG_ORDER_TEMPLATES_URL}`]: listFixture(),
      [`PATCH ${orderTemplateDetailUrl(7001)}`]: {
        id: 7001,
        title: '改后标题',
        temp: '改后内容',
        is_default: true,
      } satisfies OrderTemplateRow,
    })
    await mount()
    click(button('edit', 7001))
    expect(root.querySelector<HTMLInputElement>(`#${TITLE_INPUT_ID}`)?.value).toBe(ROW_A.title)
    expect(root.querySelector<HTMLTextAreaElement>(`#${CONTENT_INPUT_ID}`)?.value).toBe(ROW_A.temp)
    expect(root.querySelector<HTMLButtonElement>(`#${SAVE_BUTTON_ID}`)?.hidden).toBe(false)

    setInput(TITLE_INPUT_ID, '改后标题')
    setInput(CONTENT_INPUT_ID, '改后内容')
    setDefaultChecked(true)
    click(root.querySelector<HTMLElement>(`#${SAVE_BUTTON_ID}`)!)
    await flush()

    const patch = findConfig('PATCH', orderTemplateDetailUrl(7001))
    expect(patch, '应当发过一条 PATCH').toBeDefined()
    expect(patch!.data).toEqual({ title: '改后标题', temp: '改后内容', is_default: true })
    expect(actionText()).toBe(MESSAGES.updated)
    // 成功后回到新建态。
    expect(root.querySelector<HTMLButtonElement>(`#${SAVE_BUTTON_ID}`)?.hidden).toBe(true)
    expect(root.querySelector<HTMLButtonElement>(`#${CREATE_BUTTON_ID}`)?.hidden).toBe(false)
  })

  it('编辑态点「取消编辑」只复位表单、不发写请求', async () => {
    await mount()
    click(button('edit', 7001))
    click(root.querySelector<HTMLElement>(`#${CANCEL_BUTTON_ID}`)!)
    await flush()
    expect(requests()).toHaveLength(1)
    expect(root.querySelector<HTMLInputElement>(`#${TITLE_INPUT_ID}`)?.value).toBe('')
    expect(root.querySelector<HTMLButtonElement>(`#${SAVE_BUTTON_ID}`)?.hidden).toBe(true)
  })

  it('保存修改连点只发一次 PATCH（在途闸）', async () => {
    let resolvePatch!: (value: OrderTemplateRow) => void
    mockedRequest.mockImplementation((config: Cfg) => {
      const key = `${(config.method ?? 'get').toUpperCase()} ${config.url ?? ''}`
      if (key === `GET ${DAILIANTONG_ORDER_TEMPLATES_URL}`) return Promise.resolve(listFixture())
      if (key === `PATCH ${orderTemplateDetailUrl(7001)}`) {
        return new Promise<OrderTemplateRow>((resolve) => {
          resolvePatch = resolve
        })
      }
      throw new Error(`用例没有备 ${key} 的响应`)
    })
    await mount()
    click(button('edit', 7001))
    setInput(TITLE_INPUT_ID, '改后标题')
    const saveBtn = root.querySelector<HTMLButtonElement>(`#${SAVE_BUTTON_ID}`)!
    click(saveBtn)
    click(saveBtn)
    await flush(2)
    expect(requests().filter((key) => key.startsWith('PATCH '))).toHaveLength(1)
    expect(saveBtn.disabled).toBe(true)
    resolvePatch({ ...ROW_A, title: '改后标题' })
    await flush()
  })
})

describe('删除 / 设默认：必须先过确认框', () => {
  it('删除：确认框文案带模板名；确认后 DELETE 正确 URL 并刷新列表', async () => {
    respond({
      [`GET ${DAILIANTONG_ORDER_TEMPLATES_URL}`]: listFixture(),
      [`DELETE ${orderTemplateDetailUrl(7001)}`]: { id: 7001, deleted: true },
    })
    await mount()
    click(button('delete', 7001))
    await flush()

    expect(LAYUI.layer.confirm).toHaveBeenCalledTimes(1)
    expect(LAYUI.layer.confirm).toHaveBeenCalledWith(
      expect.stringContaining(ROW_A.title),
      { title: '删除模板' },
      expect.any(Function),
    )
    expect(LAYUI.layer.confirm).toHaveBeenCalledWith(
      expect.stringContaining(MESSAGES.deleteConfirm),
      expect.anything(),
      expect.any(Function),
    )
    expect(findConfig('DELETE', orderTemplateDetailUrl(7001))).toBeDefined()
    expect(actionText()).toBe(MESSAGES.deleted)
    expect(requests().filter((key) => key === `GET ${DAILIANTONG_ORDER_TEMPLATES_URL}`)).toHaveLength(2)
  })

  it('删除：用户在确认框点取消 ⇒ **零写请求**、列表不刷新', async () => {
    await mount()
    LAYUI.layer.confirm.mockImplementation(() => {
      // 不调 cb = 用户点了「取消」。
    })
    click(button('delete', 7001))
    await flush()
    expect(LAYUI.layer.confirm).toHaveBeenCalledTimes(1)
    expect(requests()).toHaveLength(1)
    expect(requests().some((key) => key.startsWith('DELETE '))).toBe(false)
  })

  it('删除连点只弹一次确认、只发一次 DELETE（在途闸）', async () => {
    let resolveDelete!: (value: { id: number; deleted: boolean }) => void
    mockedRequest.mockImplementation((config: Cfg) => {
      const key = `${(config.method ?? 'get').toUpperCase()} ${config.url ?? ''}`
      if (key === `GET ${DAILIANTONG_ORDER_TEMPLATES_URL}`) return Promise.resolve(listFixture())
      if (key === `DELETE ${orderTemplateDetailUrl(7001)}`) {
        return new Promise<{ id: number; deleted: boolean }>((resolve) => {
          resolveDelete = resolve
        })
      }
      throw new Error(`用例没有备 ${key} 的响应`)
    })
    await mount()
    const deleteBtn = button('delete', 7001)
    click(deleteBtn)
    click(deleteBtn)
    await flush(2)
    expect(LAYUI.layer.confirm).toHaveBeenCalledTimes(1)
    expect(requests().filter((key) => key.startsWith('DELETE '))).toHaveLength(1)
    resolveDelete({ id: 7001, deleted: true })
    await flush()
  })

  it('设默认：确认后 POST 默认端点并刷新列表', async () => {
    respond({
      [`GET ${DAILIANTONG_ORDER_TEMPLATES_URL}`]: listFixture(),
      [`POST ${orderTemplateDefaultUrl(7001)}`]: { ...ROW_A, is_default: true },
    })
    await mount()
    click(button('default', 7001))
    await flush()

    expect(LAYUI.layer.confirm).toHaveBeenCalledTimes(1)
    expect(LAYUI.layer.confirm).toHaveBeenCalledWith(
      expect.stringContaining(ROW_A.title),
      { title: '设为默认模板' },
      expect.any(Function),
    )
    expect(findConfig('POST', orderTemplateDefaultUrl(7001))).toBeDefined()
    expect(actionText()).toBe(MESSAGES.defaulted)
    expect(requests().filter((key) => key === `GET ${DAILIANTONG_ORDER_TEMPLATES_URL}`)).toHaveLength(2)
  })

  it('设默认：取消 ⇒ 零请求', async () => {
    await mount()
    LAYUI.layer.confirm.mockImplementation(() => {
      // 取消
    })
    click(button('default', 7001))
    await flush()
    expect(requests()).toHaveLength(1)
    expect(requests().some((key) => key.startsWith('POST '))).toBe(false)
  })

  it('已经是默认的那条不画「设为默认」按钮（再点一次没有语义）', async () => {
    await mount()
    expect(root.querySelector(`[${ACTION_ATTR}="default"][${ACTION_ID_ATTR}="7002"]`)).toBeNull()
  })
})

describe('纯函数自证（能失败）', () => {
  it('listHtml 空列表给空态那一行，不是空白', () => {
    expect(__testables.listHtml([])).toContain(MESSAGES.empty)
  })

  it('rowHtml 转义标题与内容（走 innerHTML 的字段一律过 escapeHtml）', () => {
    const html = __testables.rowHtml({
      id: 9,
      title: '<img src=x onerror=alert(1)>',
      temp: '<script>alert(2)</script>',
      is_default: false,
    })
    expect(html).not.toContain('<img src=x')
    expect(html).not.toContain('<script>')
    expect(html).toContain('&lt;img')
  })
})
