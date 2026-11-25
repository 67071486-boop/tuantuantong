// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 【绑定上家系统管理】页从 `artifactPage` 改成 **自挂载 + 接真后端** 之后的门禁（spec §11 / §12.5）。
 *
 * ⚠️ **驱动真交互，不钉常量**（`AGENTS.md` §5-G 硬要求）：`typecheck` 看不见 DOM 选择器对不对、
 * `lint` 看不见类名里多了个点，而页面「点了没反应」是**静默**的。所以本文件：
 *   - 证据件走**真实文件**（`public/replica/system-homesystemsetting.html`），不另造一份标记
 *     ——否则「密码框到底是什么 type / 四个输入叫什么 name」这类断言会与真实产物脱钩；
 *   - `layer` / `table` / `form` 是**可控替身**（真 layui 在 jsdom 里加载不了），
 *     但**主动作走的是页面自己登记的那两个真回调**（`table.on('toolbar(tableList)')` /
 *     `table.on('tool(tableList)')` / `form.on('submit(savebtn)')`），
 *     它们正是真浏览器里点击时会调的函数；
 *   - 写请求打的是**真 `api/*.ts`**（`peerBindings` / `jidanpuUpstream`）拼出来的请求配置
 *     （`@/api/http` 的 `request` 被替身化）⇒ 断言的是「真请求的形状」，不是页面自造的中间态。
 *
 * ⚠️ **收口**之后（业务方裁定：只留弹窗一个新增入口），本文件覆盖三件事：
 *   ① 那张**统一列表**（团团通 + 鸡蛋铺混排）+ 新增的「系统类型」列与行映射；
 *   ② 行内「删除绑定」按 `system_type` 分流的两个出口（含鸡蛋铺那句自拟确认语）；
 *   ③ 弹窗那条唯一入口（系统类型下拉 + 两个分支），以及**表格下方那块表单一个节点都不剩**。
 *
 * 手法照 `system-sourelist.spec.ts`（同一套弹层模式的最近先例）与 `order-orderrecycle.spec.ts`。
 * ⚠️ `vi.mock` 会被提升，`import` 必须写在它之后。
 */
const HERE = path.dirname(fileURLToPath(import.meta.url))
const EVIDENCE = readFileSync(
  path.resolve(HERE, '../../../../public/replica/system-homesystemsetting.html'),
  'utf8',
)

/**
 * **统一列表**一行**恰好**的键集合（= 后端 `upstream_binding_views.ROW_KEYS`，计划 §2.1）。
 *
 * ⚠️ 它是后端契约的**前端副本**：夹具一旦自带后端不返回的键，页测试就会在**说谎**
 * （页面读它会拿到 `undefined`，而测试因为是夹具自己造的键而全绿）。
 * ⛔ 两个清单要同步改；后端那份（`src/apps/business/api/upstream_binding_views.py`）才是权威。
 */
const LIST_ROW_KEYS: readonly string[] = [
  'id',
  'system_type',
  'system_name',
  'base_url',
  'account',
  'bound_by_name',
  'bound_at',
  'last_ok_at',
  'last_error',
  'last_error_at',
  'consecutive_failures',
  // ⭐ 大厅镜像：两格（团团通行真值 / 鸡蛋铺行开关为 null）。
  'hall_sync_enabled',
  'hall_price_percent',
]

/** 合成夹具（PII 纪律：保留域名 `*.test.invalid`；⛔ 无手机号 / 身份证 / 邮箱）。团团通行。 */
const ROW: UpstreamBindingRow = {
  id: 11,
  system_type: 'tuantuantong',
  system_name: '测试上家甲',
  base_url: 'https://peer-a.test.invalid',
  account: 'peer-account-a',
  bound_by_name: '测试操作人甲',
  bound_at: '2026-09-25T10:30:00+08:00',
  last_ok_at: null,
  last_error: '',
  last_error_at: null,
  consecutive_failures: 0,
  // ⚠️ 与下面 `PEER_DETAIL`（true / 80）**刻意不同**：编辑回填取错来源立刻红。
  hall_sync_enabled: false,
  hall_price_percent: 100,
}

/**
 * 一条**鸡蛋铺**行的夹具（收口后它与团团通行是**同一套 13 键**，只有 `system_type` 不同）。
 *
 * ⚠️ 每个字段的值都与 `ROW` **刻意不同**（id 23≠11、名称乙≠甲、地址 / 账号 / 操作人 / 时刻全不一样）
 * ⇒「删除分流取错了行」「列映射读错了字段」都会红；若两行同值，那些断言就是**恒真**的。
 */
const JIDANPU_ROW: UpstreamBindingRow = {
  id: 23,
  system_type: 'jidanpu',
  system_name: '测试上家乙',
  base_url: 'https://upstream-b.test.invalid',
  account: 'kefu-up-b',
  bound_by_name: '测试操作人乙',
  bound_at: '2026-09-28T09:00:00+08:00',
  last_ok_at: '2026-09-28T10:11:12+08:00',
  last_error: '',
  last_error_at: null,
  consecutive_failures: 0,
  // 鸡蛋铺那模型**没有**「同步上家抢单大厅」这一列 ⇒ 契约值是 `null`；百分比是它自己那列。
  hall_sync_enabled: null,
  hall_price_percent: 75,
}

/**
 * ⭐ 大厅镜像 P1：**团团通**绑定的行级详情（`GET /api/business/peer-bindings/<pk>/`）。
 *
 * 与列表行 `ROW` 的取值**刻意不同**（`true` / 80 ≠ `false` / 100）⇒ 「回填拿的是库里现值
 * 而不是列表显示值」这条判据才有判别力。
 */
const PEER_DETAIL = {
  id: 11,
  peer_name: '测试上家甲',
  peer_base_url: 'https://peer-a.test.invalid',
  peer_account: 'peer-account-a',
  password_set: true,
  bound_by: 3,
  bound_by_name: '测试操作人甲',
  bound_at: '2026-09-25T10:30:00+08:00',
  last_ok_at: null,
  last_error: '',
  last_error_at: null,
  consecutive_failures: 0,
  hall_sync_enabled: true,
  hall_price_percent: 80,
}

/**
 * 那条**单行资源**（`PUT /api/business/member/upstream-jidanpu-binding/`）的响应夹具。
 *
 * **合成值**（PII 守卫扫 `src/**`）。⚠️ `password_set: true` 而 `password` 键**根本不存在**
 * （后端 `write_only`）；每个字段的值都与「弹窗里的初值 / 默认值」不同（否则「存完没回填也绿」）：
 * `hall_price_percent: 80` ≠ 默认 100、两个间隔 11 / 900 ≠ 任何默认值。
 *
 * ⚠️ 它**不是**统一列表那一行的形状（那是 `ROW` / `JIDANPU_ROW`）：这条是单行资源自己的序列化器。
 */
const UPSTREAM_PUT_ROW = {
  id: 11,
  jidanpu_base_url: 'https://upstream-a.test.invalid',
  account: 'kefu-up-stream-a',
  account_name: '测试上家甲',
  // ⭐ 人填的上家系统名称（编辑回填取它；与空串初值刻意不同，回填取错来源立刻红）。
  name: '测试上家乙名',
  password_set: true,
  enabled: true,
  hall_price_percent: 80,
  hall_poll_seconds: 11,
  my_orders_poll_seconds: 900,
  last_run_at: '2026-09-28T10:11:12+08:00',
  last_ok_at: '2026-09-28T10:11:12+08:00',
  last_error: '上家账号打不开「我的订单」，请让上家给它开这个权限',
  last_error_at: '2026-09-28T10:11:12+08:00',
  checked_at: '2026-09-28T09:00:00+08:00',
  // ⭐ D-0261：行级 GET 的「订单来源」= `{id, 名字}`；id 7 与字典夹具里那项**同源**
  //（编辑回填走「字典里已有」的正常路；「不在字典里」的补口另有专条用例）。
  order_source: { id: 7, name: '竹屿' },
  created_at: '2026-09-28T09:00:00+08:00',
  updated_at: '2026-09-28T10:11:12+08:00',
}

/**
 * 弹窗「鸡蛋铺系统」分支那条**写**路径（`PUT` 单行资源）的**可控替身状态**
 * （在 `@/api/http` 那个按 URL 分流的桩里被读，见下面 `vi.mock('@/api/http')`）。
 *
 * ⚠️ 为什么不用 `vi.mocked(request).mockRejectedValueOnce(...)` 逐条控制：那条 `Once` 的**顺序**
 * 很容易被同一次用例里的其它请求先吃掉（那样用例会红得莫名其妙）。这里按**端点**分开控制。
 */
const UPSTREAM = vi.hoisted(() => ({
  /** PUT 失败时抛的那句话（`''` = 正常回上面那份单行资源夹具）。 */
  putError: '',
  /** ⭐ D-0256：**行级** POST / PUT（新增 / 编辑）失败时抛的那句话（`''` = 正常回包）。 */
  rowError: '',
  /** ⭐ D-0256：行级 **GET**（编辑回填）失败时抛的那句话——如那一行已被别人删掉。 */
  rowReadError: '',
  /** ⭐ D-0261：来源字典 GET 失败时抛的那句话（`''` = 正常回下面那份字典夹具）。 */
  sourcesError: '',
  /** ⭐ P1：团团通**行级详情** GET 失败时抛的那句话（`''` = 正常回 `PEER_DETAIL`）。 */
  peerReadError: '',
}))

/**
 * layui 的**可控替身**（照 `system-sourelist.spec.ts` 的同一套）。
 *
 * `form.val` 刻意实现成「按 `<form lay-filter>` 的作用域找输入」——那条硬要求（先预填、再 open）
 * 的判据就是它；`layer.open` 只记入参、**不搬节点**（真 layui 会把 `.change` 搬进
 * `.layui-layer-content`，搬走之后的路径由 `ensurePanel` 的快照兜底，另有专门用例）。
 */
const LAYUI = vi.hoisted(() => {
  const tableHandlers = new Map<string, (...args: unknown[]) => unknown>()
  const formHandlers = new Map<string, (...args: unknown[]) => unknown>()
  const formVals: Array<Record<string, unknown>> = []
  const msgs: string[] = []
  let openSeq = 0
  const msg = vi.fn((text: string) => {
    msgs.push(text)
  })
  const open = vi.fn<(config: Record<string, unknown>) => number>(() => {
    openSeq += 1
    return openSeq
  })
  const alert = vi.fn((_text: string, _cb: (i: number) => void) => undefined)
  return {
    tableHandlers,
    formHandlers,
    formVals,
    msgs,
    msg,
    open,
    alert,
    resetOpenSeq: () => {
      openSeq = 0
    },
    form: {
      render: vi.fn(),
      val: vi.fn((filter: string, data: Record<string, unknown>) => {
        formVals.push(data)
        const form = document.querySelector<HTMLFormElement>(`form[lay-filter="${filter}"]`)
        if (!form) return
        for (const [name, value] of Object.entries(data)) {
          const field = form.elements.namedItem(name) as HTMLInputElement | null
          if (!field) continue
          /* ⚠️ 语义照**真实现**写（vendored `public/layui/layui.js` 的 `form.val`，即
           * `t.prototype.val`）：
           *   `"checkbox"===a ? l[0].checked=n : "radio"===a ? l.each(function(){this.checked=this.value==n+""}) : l.val(n)`
           * ⇒ 复选框走 **`checked`**（⛔ 不是 `value`），单选框比 `value`，其余（input / select）走 `value`。
           * ⛔ 不许为了用例变绿改成别样：不照它写的话，「打开弹窗把开关复位」在测试里**永远看不到
           * 变化**（`checked` 恒为初值）⇒ 那条判据会变成恒真。 */
          if (field instanceof HTMLInputElement && field.type === 'checkbox') {
            field.checked = value as boolean
            continue
          }
          if (field instanceof HTMLInputElement && field.type === 'radio') {
            field.checked = field.value === String(value)
            continue
          }
          field.value = String(value)
        }
      }),
      on: (e: string, cb: (...args: unknown[]) => unknown) => {
        formHandlers.set(e, cb)
      },
    },
    table: {
      render: vi.fn((_options?: unknown) => undefined),
      reload: vi.fn(),
      on: (e: string, cb: (...args: unknown[]) => unknown) => {
        tableHandlers.set(e, cb)
      },
    },
    layer: {
      msg,
      open,
      alert,
      close: vi.fn(),
      confirm: vi.fn(),
    },
    /**
     * `layer.open({type:1, content})` 要的是 **jQuery 对象**（`config-replica.openPanelLayer`；
     * 传裸 `HTMLElement` 真 layui 会抛 `TypeError: r.parents is not a function`）——替身只包一层，
     * 断言用的 `content.el` 就是那个节点。
     */
    $: (el: Element) => ({ el }),
  }
})

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return {
    ...actual,
    // 真 layui 在 jsdom 里加载不了（它靠 `<script src>` + `document.currentScript`）。
    bootstrapIsland: (async () => LAYUI) as unknown as typeof actual.bootstrapIsland,
  }
})

/**
 * 传输层替身：断言的是**真 `api/*.ts`** 拼出来的请求配置，按 **URL 分流**。
 *
 * - `member/upstream-jidanpu-binding/` ⇒ 弹窗「鸡蛋铺系统」分支那条**写**（`PUT` 单行资源，
 *   回上面那份 `UPSTREAM_PUT_ROW`）；
 * - `upstream-bindings/jidanpu/<pk>/` ⇒ 统一列表行内「删除绑定」的**鸡蛋铺**那一支（DELETE）；
 * - `peer-bindings`（集合 / 详情）⇒ 团团通那一支：POST 一行、DELETE null、GET `{items,total}`。
 *
 * ⚠️ **不 mock `@/api/jidanpuUpstream` / `@/api/peerBindings` / `@/api/upstreamBindings`**：让真文件
 * 去拼请求，断言才落在「真请求的形状」上，而不是页面自造的中间态（见文件头那条口径）。
 * ⚠️ `request<T>()` 回的是**解包后的 `data`**（信封的 `{code,msg}` 由 `unwrap()` 吃掉）
 * ⇒ 这个替身回 `UpstreamBindingRow` / `null` / `{items,total}` 本身，⛔ 不是 `{code,msg,data}` 信封。
 */
vi.mock('@/api/http', () => ({
  request: vi.fn(async (config: { method?: string; url?: string }) => {
    const url = String(config.url ?? '')
    const method = String(config.method ?? '')
    // ⭐ D-0261：弹窗「订单来源」下拉的字典（`fetchOrderSources`，GET 集合 + page_size=200）。
    if (url.includes('/api/business/order-sources/')) {
      if (method !== 'get') throw new Error(`未预期的 request：${method} ${url}`)
      if (UPSTREAM.sourcesError !== '') throw new Error(UPSTREAM.sourcesError)
      return {
        items: [
          { id: 7, name: '竹屿', remark: '', order_by: 0, shop_type: 0, created_at: '2026-10-19T09:00:00+08:00', updated_at: '2026-10-19T09:00:00+08:00' },
          { id: 9, name: '叶枫', remark: '', order_by: 0, shop_type: 0, created_at: '2026-10-19T09:00:00+08:00', updated_at: '2026-10-19T09:00:00+08:00' },
        ],
        total: 2,
        page: 1,
        page_size: 200,
      }
    }
    if (url.includes('member/upstream-jidanpu-binding')) {
      if (method !== 'put') throw new Error(`未预期的 request：${method} ${url}`)
      if (UPSTREAM.putError !== '') throw new Error(UPSTREAM.putError)
      return UPSTREAM_PUT_ROW
    }
    // ⭐ D-0256：行级四条（POST 集合 = 新增；GET / PUT 明细 = 回填 / 编辑；DELETE = 既有真删）。
    //    ⛔ member 那条单行资源不再是页面的写路径：出现 PUT 打它就该报错（分流写死会立刻红）。
    if (url.includes('/upstream-bindings/jidanpu/')) {
      if (method === 'delete') return null
      if (method === 'get') {
        if (UPSTREAM.rowReadError !== '') throw new Error(UPSTREAM.rowReadError)
        return UPSTREAM_PUT_ROW
      }
      if (method === 'post' || method === 'put') {
        if (UPSTREAM.rowError !== '') throw new Error(UPSTREAM.rowError)
        return UPSTREAM_PUT_ROW
      }
      throw new Error(`未预期的 request：${method} ${url}`)
    }
    if (url.includes('peer-bindings')) {
      if (method === 'post') return ROW
      if (method === 'delete') return null
      if (method === 'get') {
        // ⭐ P1：**明细**（`<pk>/` 结尾）与集合是两条语义：明细给 `PEER_DETAIL`（编辑回填）。
        if (/peer-bindings\/\d+\/$/.test(url)) {
          if (UPSTREAM.peerReadError !== '') throw new Error(UPSTREAM.peerReadError)
          return PEER_DETAIL
        }
        return { items: [ROW], total: 1 }
      }
      if (method === 'put') {
        // ⭐ P1：编辑团团通只写两格（页面这一发只带两个键，见下面的载荷判据）。
        if (UPSTREAM.rowError !== '') throw new Error(UPSTREAM.rowError)
        return PEER_DETAIL
      }
    }
    throw new Error(`未预期的 request：${method} ${url}`)
  }),
}))

import { request } from '@/api/http'
import { listPeerBindings } from '@/api/peerBindings'
import type { UpstreamBindingRow } from '@/api/upstreamBindings'
import {
  BINDINGS_TABLE_URL,
  BIND_OK_TEXT,
  COLS,
  DIALOG_FIELD_NAMES,
  EDIT_DIALOG_TITLE,
  ENABLED_FIELD_NAME,
  ENABLED_ITEM_ID,
  FAILURE_NOTICE_ID,
  HALL_PERCENT_FIELD_NAME,
  HALL_PERCENT_ITEM_ID,
  HOME_HOST_LINK_TITLE,
  JIDANPU_UNBIND_CONFIRM_TEXT,
  LAST_SYNC_NOTICE_CLASS,
  LAST_SYNC_NOTICE_ID,
  LAST_SYNC_NOTICE_SELECTOR,
  ORDER_SOURCE_FIELD_NAME,
  ORDER_SOURCE_ITEM_ID,
  ORDER_SOURCE_NONE_LABEL,
  ORDER_SOURCE_NONE_VALUE,
  PANEL_CLASS,
  PANEL_SELECTOR,
  PEER_EDIT_DIALOG_TITLE,
  PEER_HALL_PERCENT_DEFAULT,
  PEER_HALL_PERCENT_FIELD_NAME,
  PEER_HALL_PERCENT_INVALID_TEXT,
  PEER_HALL_PERCENT_ITEM_ID,
  PEER_HALL_PERCENT_LABEL,
  PEER_HALL_SAVE_OK_TEXT,
  PEER_HALL_SYNC_FIELD_NAME,
  PEER_HALL_SYNC_ITEM_ID,
  PEER_HALL_SYNC_LABEL,
  ROW_EDIT_BUTTON_TEMPLATE,
  ROW_EDIT_EVENT,
  ROW_TOOLBAR_ELEM,
  SYSTEM_TYPE_DEFAULT,
  SYSTEM_TYPE_FIELD_NAME,
  SYSTEM_TYPE_ITEM_ID,
  SYSTEM_TYPE_JIDANPU,
  SYSTEM_TYPE_LABEL_JIDANPU,
  SYSTEM_TYPE_LABEL_TUANTUANTONG,
  SYSTEM_TYPE_OPTIONS,
  SYSTEM_TYPE_SELECT_ID,
  SYSTEM_TYPE_TUANTUANTONG,
  TABLE_ELEM,
  TABLE_ID,
  TITLE,
  TOOLBAR_ELEM,
  UNBIND_CONFIRM_TEXT,
  UNBIND_OK_TEXT,
  VALIDATION_MESSAGES,
  applySystemType,
  bindingDialogOptions,
  enabledItemHtml,
  ensureOrderSourceOption,
  escapeHtml,
  failureNoticeText,
  fillOrderSourceOptions,
  hallPercentItemHtml,
  homeHostCellTemplate,
  injectDialogExtras,
  injectRowEditButton,
  lastSyncNoticeText,
  mountHomesystemsettingPage,
  orderSourceItemHtml,
  peerHallPercentItemHtml,
  peerHallSettingsOf,
  peerHallSyncItemHtml,
  readDialogJidanpuPayload,
  readPeerHallSettings,
  systemTypeItemHtml,
  toBindingRow,
  upstreamSystemName,
  validateBindingForm,
  validateJidanpuDialogForm,
  type BindingCellRow,
} from '../system-homesystemsetting'

const CTX = { onNavigate: vi.fn() }

function textResponse(text: string): Response {
  return { ok: true, status: 200, text: async () => text } as unknown as Response
}

/** 挂载整页（证据件走全局 `fetch` 的真文件；取数走 layui 替身）。 */
async function mount(): Promise<{ root: HTMLElement; cleanup: () => void }> {
  const root = document.createElement('div')
  document.body.appendChild(root)
  const cleanup = await mountHomesystemsettingPage(root, CTX)
  return { root, cleanup }
}

/** `table.render` 收到的唯一那份配置（**真正交给 layui 的出口**）。 */
type RenderOpts = {
  elem?: string
  id?: string
  url?: string
  method?: string
  toolbar?: string
  defaultToolbar?: unknown
  cols?: unknown[]
  height?: unknown
  page?: unknown
  text?: unknown
  data?: unknown
  parseData?: (r: {
    code: number
    msg: string
    data?: { items?: UpstreamBindingRow[]; total?: number }
  }) => { code: number; msg: string; count: number; data: BindingCellRow[] }
}

function renderOpts(): RenderOpts {
  const call = LAYUI.table.render.mock.calls[0]
  expect(call, 'table.render 没有被调用').toBeTruthy()
  return call![0] as RenderOpts
}

/** 点一次工具栏事件（layui 的 `table.on('toolbar(tableList)')`）。 */
function fireToolbar(event: string): void {
  const cb = LAYUI.tableHandlers.get(`toolbar(${TABLE_ID})`)
  expect(cb, '本页没有登记工具栏处理器 ⇒ 工具栏按钮点了没反应').toBeTruthy()
  cb!({ event })
}

/** 点一次行内事件（layui 的 `table.on('tool(tableList)')`）。 */
function fireTool(event: string, row: BindingCellRow): void {
  const cb = LAYUI.tableHandlers.get(`tool(${TABLE_ID})`)
  expect(cb, '本页没有登记行内处理器 ⇒ 行内「删除绑定」点了没反应').toBeTruthy()
  cb!({ event, data: row })
}

/** 点一次「验证并保存绑定」（layui 的 `form.on('submit(savebtn)')`）。 */
function fireSubmit(): void {
  const cb = LAYUI.formHandlers.get('submit(savebtn)')
  expect(cb, '本页没有登记保存按钮的 submit 处理器 ⇒ 点「验证并保存绑定」没反应').toBeTruthy()
  cb!()
}

/**
 * 切一次弹窗里的「系统类型」。
 *
 * ⚠️ 顺序照**真交互**（读码 vendored `public/layui/layui.js` 的 select 分支：用户点中某个
 * `<dd>` 时先 `y.val(a)` 把原 `<select>` 的值改成新值，**再** `layui.event.call(this, $,
 * "select(" + lay-filter + ")", {elem, value, othis})`）⇒ 这里也必须先改 `<select>.value` 再驱动
 * 页面登记的那个**真回调**；否则 `submit()` 读到的还是旧类型，测的就不是用户看到的那条路径。
 * 事件名与 `value` 都钉**字面**，⛔ 不用生产常量拼（那样常量被改坏时这条也一起变、判据变成自指）。
 */
function fireSelectSystemType(panel: HTMLElement, value: string): void {
  const select = panel.querySelector<HTMLSelectElement>('[name="homeSystemType"]')
  expect(select, '弹层里没有「系统类型」下拉').not.toBeNull()
  select!.value = value
  const cb = LAYUI.formHandlers.get('select(homeSystemType)')
  expect(cb, '本页没有登记 select(homeSystemType) 处理器 ⇒ 切「系统类型」没反应').toBeTruthy()
  cb!({ value })
}

/** 弹层里某一格所属的 `.layui-form-item`（切换可见性的判据要打在**那一格**上）。 */
function dialogItem(panel: HTMLElement, name: string): HTMLElement {
  const field = panel.querySelector<HTMLElement>(`[name="${name}"]`)
  expect(field, `弹层里没有 name=${name} 的那一格`).not.toBeNull()
  const item = field!.closest<HTMLElement>('.layui-form-item')
  expect(item, `name=${name} 不在 .layui-form-item 里`).not.toBeNull()
  return item!
}

/** 四格的 `display`（`''` = 显示、`'none'` = 隐藏）。 */
function visibility(panel: HTMLElement): [string, string, string, string] {
  return [
    dialogItem(panel, 'homeCompany').style.display,
    panel.querySelector<HTMLElement>(`#${HALL_PERCENT_ITEM_ID}`)!.style.display,
    panel.querySelector<HTMLElement>(`#${ENABLED_ITEM_ID}`)!.style.display,
    panel.querySelector<HTMLElement>(`#${ORDER_SOURCE_ITEM_ID}`)!.style.display,
  ]
}

/** 当前弹层节点（`layer.open` 拿到的那个）。 */
function openedPanel(): HTMLElement {
  const calls = LAYUI.open.mock.calls
  expect(calls.length, 'layer.open 没被调用').toBeGreaterThan(0)
  return (calls[calls.length - 1]![0] as { content: { el: HTMLElement } }).content.el
}

/** 最近一次 `layer.open` 的整个配置。 */
function lastOpenConfig(): Record<string, unknown> {
  const calls = LAYUI.open.mock.calls
  expect(calls.length, 'layer.open 没被调用').toBeGreaterThan(0)
  return calls[calls.length - 1]![0] as Record<string, unknown>
}

/** 在弹层里按 `name` 填一个值（**真 DOM**，不是给回调塞假 field）。 */
function fill(panel: HTMLElement, name: string, value: string): void {
  const el = panel.querySelector<HTMLInputElement>(`[name="${name}"]`)
  expect(el, `弹层里没有 name=${name} 的输入`).not.toBeNull()
  el!.value = value
}

/** 把一张行夹具转成表格行（行内事件拿到的是 `parseData` 的产物）。 */
function cell(row: UpstreamBindingRow = ROW): BindingCellRow {
  return toBindingRow(row)
}

type ReqConfig = { method?: string; url?: string; data?: unknown }
const requestCalls = (): ReqConfig[] =>
  vi.mocked(request).mock.calls.map(([config]) => config as unknown as ReqConfig)
const callsByMethod = (method: string): ReqConfig[] =>
  requestCalls().filter((config) => config.method === method)
/**
 * ⭐ D-0261：页面 mount 会恒发一发「来源字典」GET（弹窗下拉的数据源）。
 * 钉「行级读发了没有」的判据要**滤掉它**——⛔ 不滤的话，字典那一发会把
 * 「这条路径零 GET / 恰一发 GET」的断言顶红（mount 时序还让它时红时绿）。
 */
const rowGets = (): ReqConfig[] =>
  callsByMethod('get').filter((config) => !String(config.url).includes('order-sources'))

/** 排空已 resolve 的 promise 链。 */
async function flush(): Promise<void> {
  for (let i = 0; i < 6; i += 1) await Promise.resolve()
}

beforeEach(() => {
  document.body.innerHTML = ''
  LAYUI.table.render.mockClear()
  LAYUI.table.reload.mockClear()
  LAYUI.open.mockClear()
  LAYUI.alert.mockClear()
  LAYUI.layer.close.mockClear()
  LAYUI.layer.confirm.mockClear()
  LAYUI.form.render.mockClear()
  LAYUI.form.val.mockClear()
  LAYUI.formVals.length = 0
  LAYUI.msgs.length = 0
  LAYUI.msg.mockClear()
  LAYUI.tableHandlers.clear()
  LAYUI.formHandlers.clear()
  LAYUI.resetOpenSeq()
  vi.mocked(request).mockClear()
  // ⭐ 弹窗「鸡蛋铺系统」分支那两条写路径的替身状态：默认正常回包。
  UPSTREAM.putError = ''
  UPSTREAM.rowError = ''
  UPSTREAM.rowReadError = ''
  UPSTREAM.sourcesError = ''
  UPSTREAM.peerReadError = ''
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      if (String(url).includes('system-homesystemsetting.html')) return textResponse(EVIDENCE)
      throw new Error(`未预期的 fetch：${String(url)}`)
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

/* ───────────────────────── 夹具的键契约（防止测试自己说谎） ───────────────────────── */

describe('夹具契约', () => {
  it('夹具的键 = 统一列表的键集合（**恰好 13 个**；两类行同一套键）', () => {
    expect([...Object.keys(ROW)].sort()).toEqual([...LIST_ROW_KEYS].sort())
    expect([...Object.keys(JIDANPU_ROW)].sort()).toEqual([...LIST_ROW_KEYS].sort())
    // 正向哨兵：恰好 13 个（既不少——少了页面读 `undefined`，也不多——多了夹具在说谎）。
    // ⚠️ 这份清单是后端契约的**前端副本**（后端 `upstream_binding_views.ROW_KEYS` 才是权威，
    // 由 `src/tests/test_upstream_binding_list.py` 的键集用例守着）。
    expect(LIST_ROW_KEYS).toHaveLength(13)
    // ⛔ 旧 `peer-bindings` 那一套键名（12 键口径）**一个都不许**混进统一列表的夹具：
    // 留着一个就是「夹具在替后端说一条它不回的字段」（页面会读到 `undefined` 而全绿）。
    for (const gone of ['peer_name', 'peer_base_url', 'peer_account', 'password_set', 'bound_by']) {
      expect(LIST_ROW_KEYS).not.toContain(gone)
    }
    // 载荷里**不许**出现密码 / 令牌键（计划 §1 凭据红线：这几个永不回显）。
    for (const forbidden of ['password', 'password_set', 'token_in_hash', 'token_out']) {
      expect(LIST_ROW_KEYS).not.toContain(forbidden)
    }
    // 两行的 `system_type` 恰好是契约里那两个值（⛔ 不写第三个值）。
    expect([ROW.system_type, JIDANPU_ROW.system_type].sort()).toEqual(['jidanpu', 'tuantuantong'])
  })
})

/* ───────────────────────── 挂载后的 DOM 与表格配置（视觉值不变） ───────────────────────── */

describe('挂载：DOM + `table.render` 配置（对标那些视觉值一个都没改）', () => {
  it('七列：第一列是**我方新增**的「系统类型」，其余六列逐字对齐 `homesystemsetting.js` L25-43', () => {
    // ⭐ 第一列：**我方新增**（收口后两类上家混在一张表里，计划 §2.3）。钉**字面**字段名与宽度。
    expect(COLS[0]).toMatchObject({
      field: 'SystemType',
      title: '系统类型',
      width: 120,
      align: 'center',
    })
    // ⛔ 对标证据件里**没有**「系统类型」这一列（否则上面那句「我方新增」就是假话）。
    expect(EVIDENCE).not.toContain('系统类型')
    // 后六列是照抄对标那一份（标题 / 宽度顺序逐字）。
    expect(COLS.slice(1).map((c) => c.title)).toEqual([
      '上家系统名称',
      '上家系统域名',
      '上家系统子账号',
      '绑定操作人',
      '绑定时间',
      '操作',
    ])
    expect(COLS.map((c) => c.width)).toEqual([120, 150, 200, 200, 150, 200, 200])
    expect(COLS[2]).toMatchObject({ field: 'HomeHost', align: 'left' })
    // 对标 L39 的 `sort:true` 只有「绑定时间」这一列。
    expect(COLS.filter((c) => c.sort === true)).toHaveLength(1)
    // ⭐ 操作列 160 → 200（E-0296）：160 是对标**单按钮**时代的值；D-0256 注入「编辑」后
    //   两颗按钮合计 ≈130px == 160px 列的内容区宽（`.layui-table-cell` 左右各 15px 内边距
    //   + `overflow:hidden/ellipsis`）——字体栈稍宽「删除绑定」整颗被裁成灰「…」（生产实锤）。
    //   200 ⇒ 内容区 170px，跨字体栈余量 ≈40px。低于这个宽度就是回到零余量，**不许**改回去。
    expect(COLS[6]).toMatchObject({ width: 200, unresize: true, toolbar: ROW_TOOLBAR_ELEM })
    // 对标 L42 的 `fixed:''`（空串＝不固定）→ 实现里**不应**出现 `fixed`。
    expect('fixed' in (COLS[6] as object)).toBe(false)
    // ⛔ 「操作」那一列仍**必须**挂行内模板（差异表 #5）：漏了它行内「删除绑定」永远不渲染。
    expect(COLS[6]).toMatchObject({ toolbar: ROW_TOOLBAR_ELEM })
  })

  it('mount 后：表格 / 工具栏 / 行内模板 / 弹层容器 / 四个输入都在 DOM 里', async () => {
    const { root, cleanup } = await mount()
    expect(root.querySelector(TABLE_ELEM)).not.toBeNull()
    expect(root.querySelector(TOOLBAR_ELEM)).not.toBeNull()
    expect(root.querySelector(ROW_TOOLBAR_ELEM)).not.toBeNull()
    // ⚠️ 这一条同时是**陷阱①**的判据：`PANEL_SELECTOR` 一旦写成 `change`（少点）或
    //    `PANEL_CLASS` 一旦写成 `.change`（多点），这里立刻红——而页面「点了没反应」是静默的。
    const panel = root.querySelector<HTMLElement>(PANEL_SELECTOR)
    expect(panel, `选择器 ${PANEL_SELECTOR} 匹配不到弹层`).not.toBeNull()
    expect(panel!.className).toBe(PANEL_CLASS)
    for (const name of ['homeCompany', 'homeUrl', 'homeAcc', 'homePwd']) {
      expect(root.querySelector(`[name="${name}"]`), `缺输入 ${name}`).not.toBeNull()
    }
    // ⭐ 钉**字面**、⛔ 不比 `TITLE`：比常量时把常量打错（例如少了"管理"）这条不会红。
    expect(document.title).toBe('绑定上家系统管理')
    // 常量本身也在「字面钉子」那组里钉住（两处一起改，⛔ 不留一条恒真的自指断言）。
    expect(TITLE).toBe('绑定上家系统管理')
    cleanup()
  })

  it('`table.render` 拿到的是 url 模式 + 对标参数（elem/id/toolbar/defaultToolbar/height/page）', async () => {
    const { cleanup } = await mount()
    const opts = renderOpts()
    expect(opts.elem).toBe(TABLE_ELEM)
    // ⭐ 钉**字面** `tableList`：`TABLE_ID` 是"要传 id 不是 `#id`"那条约定的一半，
    // 比常量时把常量打错（例如写成 `#tableList`）这条不会红。
    expect(opts.id).toBe('tableList')
    expect(opts.url).toBe(BINDINGS_TABLE_URL)
    // ⭐ 收口后钉的是**统一列表**那条端点（团团通 + 鸡蛋铺混排），⛔ 不再是 `peer-bindings/`。
    expect(String(opts.url)).toBe('/api/business/upstream-bindings/')
    expect(String(opts.url)).not.toContain('peer-bindings')
    expect(opts.method).toBe('get')
    expect(opts.toolbar).toBe(TOOLBAR_ELEM)
    // 对标同一个对象里写了两次 `defaultToolbar`（`[]` → `['filter']`），后写的生效。
    expect(opts.defaultToolbar).toEqual(['filter'])
    expect(opts.height).toBe('full')
    expect(opts.page).toBe(false)
    // 空表时的占位文案（旧 `artifactPage` 渲染器给的同一句）。
    expect(opts.text).toEqual({ none: '暂无数据' })
    // ⛔ 不再有本地 `data`：带 `data` 会让 layui 走本地分支、`url` 被忽略（表恒空）。
    expect('data' in opts).toBe(false)
    // ⛔ `cols` 的**嵌套层数**也要钉住：layui 的契约是「数组的数组」。
    expect(opts.cols).toEqual([COLS])
    cleanup()
  })

  it('刷新按钮 → 走 layui 的 `url` 通道重取（`table.reload`），零写请求', async () => {
    const { cleanup } = await mount()
    fireToolbar('refresh')
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
    // ⭐ 钉字面：`reload` 传错（带 `#`）是**静默 no-op**，比常量时看不出常量本身被打错。
    expect(LAYUI.table.reload.mock.calls[0]![0]).toBe('tableList')
    expect(LAYUI.open).not.toHaveBeenCalled()
    expect(callsByMethod('post')).toHaveLength(0)
    cleanup()
  })
})

/* ───────────────────────── `HomeHost` 列的链接模板 ───────────────────────── */

describe('`HomeHost` 列的 `templet`：逐字搬对标 L29-31，取值处转义', () => {
  /** `HomeHost` 列挂在 `COLS` 上的那个 `templet`（**真正交给 layui 的出口**）。 */
  const templet = (): ((d: { HomeHost: string }) => string) => {
    const fn = COLS[2]!.templet
    expect(
      typeof fn,
      '`HomeHost` 列没有挂 `templet` ⇒ 域名只渲染成纯文本：没链接色、下划线、点不动',
    ).toBe('function')
    return fn as (d: { HomeHost: string }) => string
  }

  it('渲染成 `<a>`：href / target / title / 链接文本逐项对齐对标那句模板', () => {
    // 先钉「这一列挂的就是那个函数」（⛔ 不是另写一个长得像的）。
    expect(COLS[2]!.templet).toBe(homeHostCellTemplate)
    const html = templet()({ HomeHost: 'https://peer-a.test.invalid' })
    const holder = document.createElement('div')
    holder.innerHTML = html
    const a = holder.querySelector('a')
    expect(a, '域名没有被渲染成链接').not.toBeNull()
    expect(a!.getAttribute('href')).toBe('https://peer-a.test.invalid')
    expect(a!.getAttribute('target')).toBe('_blank')
    // title 用**字面**钉（⛔ 不比常量——常量被改坏时这条不会红）。
    expect(a!.getAttribute('title')).toBe('点击可以访问这个系统')
    expect(HOME_HOST_LINK_TITLE).toBe('点击可以访问这个系统')
    expect(a!.textContent).toBe('https://peer-a.test.invalid')
    // 整串逐字 = 对标 `'<a href="' + res.HomeHost + '" target="_blank" style="" ' +
    //   'title="点击可以访问这个系统">' + res.HomeHost + '</a>'`（含它那个空 `style`）。
    expect(html).toBe(
      '<a href="https://peer-a.test.invalid" target="_blank" style="" title="点击可以访问这个系统">https://peer-a.test.invalid</a>',
    )
  })

  it('⛔ 域名里的 HTML 字符不能从属性 / 文本里逃出来（不拿用户输入拼 HTML）', () => {
    // 载荷同时尝试 ① 从 `href="…"` 里闭合出去、塞一个事件属性；② 塞一个带 `onerror` 的 `<img>`。
    const evil = 'https://peer-a.test.invalid"><img src=x onerror=alert(1)>'
    const holder = document.createElement('div')
    holder.innerHTML = templet()({ HomeHost: evil })
    // ⚠️ **每条断言都要能失败**（评审点名：旧版三条在漏洞实现下同样成立——注入的 `<img>`
    //    落在 `<a>` **里面**，`<a>` 的个数与它自己的 `onerror`/`onclick` 都不变）。
    // ① 整棵子树**只有一个**元素（漏洞版会多出一个 `<img>` ⇒ 这条立刻红）。
    expect(holder.querySelectorAll('*')).toHaveLength(1)
    expect(holder.firstElementChild!.tagName).toBe('A')
    // ② 子树里**任何**元素都不带 `on*` 事件属性（漏洞版的 `<img onerror=…>` 会命中）。
    const onAttributes = [...holder.querySelectorAll('*')].flatMap((el) =>
      [...el.attributes].map((attribute) => attribute.name),
    )
    expect(onAttributes.filter((name) => name.startsWith('on'))).toEqual([])
    // ③ 没有注入出来的元素。
    expect(holder.querySelector('img, script, iframe, svg'), '有元素从属性里逃出来了').toBeNull()
    const a = holder.querySelector('a')!
    // 值原样待在属性 / 文本里（是**字符**，不是可执行的标记）：漏洞版这里会是截断后的串。
    expect(a.getAttribute('href')).toBe(evil)
    expect(a.textContent).toBe(evil)
    // 对照哨兵（证明上面不是恒真）：同一个值**原样**拼进同一个模板，确实会造出 `<img>`。
    const raw = document.createElement('div')
    raw.innerHTML = `<a href="${evil}" target="_blank" style="" title="${HOME_HOST_LINK_TITLE}">${evil}</a>`
    expect(raw.querySelector('img'), '对照组没造出 img ⇒ 上半段断言证明不了什么').not.toBeNull()
    // 并自证「子树只有一个元素 / 没有 on* 属性」这两条在对照组上确实会红。
    expect(raw.querySelectorAll('*').length).toBeGreaterThan(1)
    expect(
      [...raw.querySelectorAll('*')].flatMap((el) =>
        [...el.attributes].map((attribute) => attribute.name),
      ).filter((name) => name.startsWith('on')),
    ).not.toEqual([])
  })

  it('`escapeHtml` 五个字符逐一转义；`&` 必须**先**转（否则产出的实体会被二次转义）', () => {
    expect(escapeHtml('&')).toBe('&amp;')
    // `&` 先转的判据：`<` 必须变成 `&lt;`；若 `&` 留到最后再转，这里会是 `&amp;lt;`。
    expect(escapeHtml('<')).toBe('&lt;')
    expect(escapeHtml('>')).toBe('&gt;')
    expect(escapeHtml('"')).toBe('&quot;')
    expect(escapeHtml("'")).toBe('&#39;')
    expect(escapeHtml('&<>"\'')).toBe('&amp;&lt;&gt;&quot;&#39;')
  })
})

/* ───────────────────────── 运行期修正（证据件本体不动） ───────────────────────── */

describe('运行期修正：密码框 type + 两条「注意」都删掉（业务方 2026-10-04 裁定）', () => {
  it('证据件里 `#homePwd` 是 `type="text"`，我们的 DOM 里是 `type="password"`（spec §11.3）', () => {
    // 先钉住**对标确实是明文的**（否则这条断言会在「对标本来就是 password」时也绿）。
    const evidencePwd = /<input[^>]*id="homePwd"[^>]*>/.exec(EVIDENCE)?.[0] ?? ''
    expect(evidencePwd).toContain('type="text"')
    // 再钉住我们把它改成了 password。
    return mount().then(({ root, cleanup }) => {
      expect(root.querySelector<HTMLInputElement>('#homePwd')!.type).toBe('password')
      cleanup()
    })
  })

  it('⭐ 两条「注意」都不在**运行期 DOM** 里（证据件里确实有那两句，⛔ 证据件一个字没动）', async () => {
    // ① 证据件里**确实有**这两句 ⇒ 下面那两条否定断言不是恒真（E-0161 的纪律：比**运行期 DOM**，
    //    ⛔ 不是比「文件文本」）。
    expect(EVIDENCE).toContain('注意：只支持同为鸡蛋铺的系统绑定，其它系统暂不支持')
    expect(EVIDENCE).toContain('你这边传的截图，也将自动同步到上家系统')
    const { root, cleanup } = await mount()
    // ② 弹窗里那条（证据件自带）：运行期 `.remove()`——判据打在**那一块节点上**，不是文本里搜不到。
    const panel = root.querySelector<HTMLElement>(PANEL_SELECTOR)
    expect(panel, `选择器 ${PANEL_SELECTOR} 匹配不到弹层`).not.toBeNull()
    expect(panel!.querySelector('blockquote.layui-elem-quote')).toBeNull()
    expect(root.textContent).not.toContain('只支持同为鸡蛋铺的系统绑定')
    // ③ 页首那条「本阶段暂不支持截图同步」的补充说明**不再追加**（那条前端自拟文案已连常量一起删，
    //    全仓 `grep` 零命中 ⇒ 页面上一个字都不该有；⛔ 这不是「把整页清空」的恒真断言：见下面 ④）。
    expect(root.textContent).not.toContain('本阶段暂不支持截图同步')
    expect(root.textContent).not.toContain('在本系统尚未实现')
    // ④ **正向对照**：证据件原文那句承诺**照旧在页面上**（它在页首那条说明块里，⛔ 不许顺手删）。
    expect(root.textContent).toContain('你这边传的截图，也将自动同步到上家系统')
    // 页首那条说明块自己还在（被删的只有弹窗里那一条）。
    expect(root.querySelectorAll('blockquote.layui-elem-quote').length).toBeGreaterThan(0)
    cleanup()
  })
})

/* ───────────────────────── ⭐ 裁决 R20：失败在页面上看得见 ───────────────────────── */

describe('同步失败红字提示（裁决 R20；有意 DOM 偏离 #7）', () => {
  const failedRow = (over: Partial<UpstreamBindingRow> = {}): UpstreamBindingRow => ({
    ...ROW,
    consecutive_failures: 2,
    last_error: 'connection refused',
    last_error_at: '2026-09-25T11:00:00+08:00',
    ...over,
  })

  const notice = (root: HTMLElement): HTMLElement => {
    const el = root.querySelector<HTMLElement>(`#${FAILURE_NOTICE_ID}`)
    expect(el, `页面里没有 #${FAILURE_NOTICE_ID} 这块提示（R20 要它看得见）`).not.toBeNull()
    return el!
  }

  it('接口回了失败行 ⇒ 红字提示**可见**并写出原因 / 次数 / 时刻', async () => {
    const { root, cleanup } = await mount()
    const noticeEl = notice(root)
    // 挂载时是收起的（⛔ 不是无条件显示，否则它成了噪音）。
    expect(noticeEl.style.display).toBe('none')

    renderOpts().parseData!({
      code: 0,
      msg: '',
      data: { items: [failedRow()], total: 1 },
    })
    expect(noticeEl.style.display).toBe('block')
    // 钉**字面**（⛔ 不比 `failureNoticeText(...)`：那样改坏纯函数这条不会红）。
    expect(noticeEl.textContent).toBe(
      '注意：1 个上家系统的订单同步最近失败了（已连续失败 2 次，最近一次 2026-09-25 11:00）：connection refused',
    )
    cleanup()
  })

  it('下一次取数全好 ⇒ 提示收起、文案清空（⛔ 不留一条过期的红字）', async () => {
    const { root, cleanup } = await mount()
    const noticeEl = notice(root)
    renderOpts().parseData!({ code: 0, msg: '', data: { items: [failedRow()], total: 1 } })
    expect(noticeEl.style.display).toBe('block')

    // ⭐ 收口后判据读**统一列表**的行：鸡蛋铺那一类 `consecutive_failures` 恒 `0`
    //（模型没有这一列）⇒ 它在也不许把红字点亮（R20 口径不变，计划 §2.1）。
    renderOpts().parseData!({
      code: 0,
      msg: '',
      data: { items: [{ ...ROW, consecutive_failures: 0 }, JIDANPU_ROW], total: 2 },
    })
    expect(noticeEl.style.display).toBe('none')
    expect(noticeEl.textContent).toBe('')
    cleanup()
  })

  it('⭐ 鸡蛋铺行**恒 `0`** 的连续失败数不会把红字点亮（哪怕它带着一句 `last_error`）', async () => {
    const { root, cleanup } = await mount()
    const noticeEl = notice(root)
    renderOpts().parseData!({
      code: 0,
      msg: '',
      data: {
        // 带上 `last_error` 而 `consecutive_failures` 仍是 0 —— 那张模型本来就没有计数列。
        items: [{ ...JIDANPU_ROW, last_error: '上家账号打不开「我的订单」', last_error_at: '2026-09-28T10:00:00+08:00' }],
        total: 1,
      },
    })
    expect(noticeEl.style.display).toBe('none')
    cleanup()
  })

  it('多行失败 ⇒ 显示失败**个数**、原因取 `last_error_at` 最新的那一条', () => {
    const older = failedRow({ system_name: '旧的上家', last_error: '旧的错', last_error_at: '2026-09-25T09:00:00+08:00' })
    const newer = failedRow({ system_name: '新的上家', last_error: '新的错', last_error_at: '2026-09-25T12:00:00+08:00' })
    expect(failureNoticeText([older, newer])).toBe(
      '注意：2 个上家系统的订单同步最近失败了（已连续失败 2 次，最近一次 2026-09-25 12:00）：新的错',
    )
    // 顺序无关（取最新靠 `last_error_at`，不是数组顺序）。
    expect(failureNoticeText([newer, older])).toBe(failureNoticeText([older, newer]))
    // 没有失败行 ⇒ 空串（调用方据此收起）。
    expect(failureNoticeText([])).toBe('')
    // ⛔ 缺失原因是"（对端没有回原因）"，不是空白（留一条看不懂的红字没有意义）。
    expect(failureNoticeText([failedRow({ last_error: '' })])).toContain('（对端没有回原因）')
  })
})

/* ───────────────────────── ⭐ 裁决 A19：最近一次收到同步（有意 DOM 偏离 #8） ───────────────────────── */

describe('「最近一次收到同步」信息块（A19；有意 DOM 偏离 #8）', () => {
  /** 收到过同步的一行（`last_ok_at` 用本页既有的 `formatDateTime` 口径渲染）。 */
  const receivedRow = (over: Partial<UpstreamBindingRow> = {}): UpstreamBindingRow => ({
    ...ROW,
    last_ok_at: '2026-09-26T10:00:00+08:00',
    ...over,
  })

  const notice = (root: HTMLElement): HTMLElement => {
    const el = root.querySelector<HTMLElement>(LAST_SYNC_NOTICE_SELECTOR)
    expect(
      el,
      `页面里没有 ${LAST_SYNC_NOTICE_SELECTOR} 这块信息（差异表 #8 要它出现）`,
    ).not.toBeNull()
    return el!
  }

  it('有 `last_ok_at` 的行走「已收到」文案，空 `last_ok_at` 的行走「尚未收到过同步」', async () => {
    const { root, cleanup } = await mount()
    const el = notice(root)
    // 还没取到数时是收起的（⛔ 不是无条件显示）。
    expect(el.style.display).toBe('none')

    renderOpts().parseData!({
      code: 0,
      msg: '',
      data: {
        items: [
          receivedRow(),
          receivedRow({ ...JIDANPU_ROW, last_ok_at: null }),
        ],
        total: 2,
      },
    })
    expect(el.style.display).toBe('block')
    // 钉**字面**（⛔ 不比 `lastSyncNoticeText(...)`：那样改坏纯函数这条不会红）。
    expect(el.textContent).toBe(
      '测试上家甲：2026-09-26 10:00\n测试上家乙：尚未收到过同步',
    )
    cleanup()
  })

  it('⭐ 收口后的行为变化：这块信息**也含鸡蛋铺行**（拿两类的统一行各钉一次名称与时刻）', async () => {
    const { root, cleanup } = await mount()
    const el = notice(root)
    renderOpts().parseData!({
      code: 0,
      msg: '',
      data: {
        // 两类都是**统一列表**的行（同一套 13 键）；鸡蛋铺那一行 `last_ok_at` 也给了值
        //（收口前的实现只认 `peer_name`，那时它会因读不到名称而拼不出这一行）。
        items: [ROW, JIDANPU_ROW],
        total: 2,
      },
    })
    expect(el.textContent).toBe(
      '测试上家甲：尚未收到过同步\n测试上家乙：2026-09-28 10:11',
    )
    // ⛔ 不许把机器值（`system_type`）当名称摆上去。
    expect(el.textContent).not.toContain('jidanpu')
    expect(el.textContent).not.toContain('tuantuantong')
    cleanup()
  })

  it('⭐ 鸡蛋铺行的 `system_name` 为空（同步引擎还没跑过）⇒ 名称回 `—`，⛔ 不留一个光秃秃的冒号', async () => {
    const { root, cleanup } = await mount()
    const el = notice(root)
    renderOpts().parseData!({
      code: 0,
      msg: '',
      data: { items: [{ ...JIDANPU_ROW, system_name: '', last_ok_at: null }], total: 1 },
    })
    expect(el.textContent).toBe('—：尚未收到过同步')
    expect(el.textContent).not.toBe('：尚未收到过同步')
    cleanup()
  })

  it('类名与选择器是**两个**常量：DOM 的 class 逐字等于 class 常量（岛页陷阱①）', async () => {
    const { root, cleanup } = await mount()
    const el = notice(root)
    // `class="${SELECTOR}"` 那个静默 bug 会让这里变成 `.peer-binding-last-sync` ⇒ 立刻红。
    expect(el.className).toBe(LAST_SYNC_NOTICE_CLASS)
    // 再钉**字面**（常量本身被打错时上面那条自指断言不会红）。
    expect(el.className).toBe('peer-binding-last-sync')
    expect(el.id).toBe(LAST_SYNC_NOTICE_ID)
    expect(el.id).toBe('peerBindingLastSyncNotice')
    cleanup()
  })

  it('⛔ 不是报警：中性色（无红），时间再旧也照样显示（A19）', async () => {
    const { root, cleanup } = await mount()
    const el = notice(root)
    renderOpts().parseData!({
      code: 0,
      msg: '',
      data: {
        items: [receivedRow({ last_ok_at: '2020-01-01T00:00:00+08:00' })],
        total: 1,
      },
    })
    // "多久没收到"不改变任何东西 ⇒ 没有按时间变红 / 自动收起的逻辑（低单量工作室不许见假报警）。
    expect(el.style.display).toBe('block')
    const style = el.getAttribute('style') ?? ''
    for (const red of ['#ff5722', '#fff2f0', '#c62828']) {
      expect(style, `信息块用了失败提示的红色 ${red}（A19：信息不是报警）`).not.toContain(red)
    }
    // 与失败提示**不是同一块**（两块语义不同：一块报警、一块只是如实报时间）。
    expect(el.id).not.toBe(FAILURE_NOTICE_ID)
    cleanup()
  })

  it('纯函数：每行一个绑定，顺序照列表；缺 `last_ok_at` 的行逐字是「尚未收到过同步」', () => {
    expect(lastSyncNoticeText([receivedRow()])).toBe('测试上家甲：2026-09-26 10:00')
    expect(lastSyncNoticeText([receivedRow({ last_ok_at: null })])).toBe(
      '测试上家甲：尚未收到过同步',
    )
    expect(
      lastSyncNoticeText([
        receivedRow({ system_name: '上家乙', last_ok_at: '2026-09-26T12:00:00+08:00' }),
        receivedRow({ system_name: '上家甲', last_ok_at: null }),
      ]),
    ).toBe('上家乙：2026-09-26 12:00\n上家甲：尚未收到过同步')
  })
})

/* ───────────────────────── 行映射 + parseData（空表 / 非空表） ───────────────────────── */

describe('toBindingRow / parseData：系统类型列 + 行映射 + 空表与非空表', () => {
  it('⭐ toBindingRow：**七个**列名逐个映射（新增的 `SystemType` 只上中文、⛔ 不上机器值）', () => {
    expect(toBindingRow(ROW)).toEqual({
      ...ROW,
      // ⭐ 我方新增列：`tuantuantong` ⇒ `团团通系统`（⛔ 原样上屏的话这里会红）。
      SystemType: '团团通系统',
      HomeCompany: '测试上家甲',
      HomeHost: 'https://peer-a.test.invalid',
      HomeAcc: 'peer-account-a',
      // ⭐ 修 1：显示名，**不是**旧口径的 FK 主键。
      UserName: '测试操作人甲',
      // ⭐ 修 2：`formatDateTime` 的 `Y-m-d H:i`，**不是** raw ISO（缺了格式化这里就红）。
      BindTimeStr: '2026-09-25 10:30',
      BindHomeSystemLogId: 11,
    })
    // ⭐ 另一类行：`jidanpu` ⇒ `鸡蛋铺系统`（**钉字面**；映射写反这里立刻红）。
    expect(toBindingRow(JIDANPU_ROW)).toEqual({
      ...JIDANPU_ROW,
      SystemType: '鸡蛋铺系统',
      HomeCompany: '测试上家乙',
      HomeHost: 'https://upstream-b.test.invalid',
      HomeAcc: 'kefu-up-b',
      UserName: '测试操作人乙',
      BindTimeStr: '2026-09-28 09:00',
      BindHomeSystemLogId: 23,
    })
    // ⛔ 两个机器值**一个都不许**漏到屏幕上（它们不是给人看的）。
    for (const row of [ROW, JIDANPU_ROW]) {
      const mapped = toBindingRow(row)
      expect(mapped.SystemType).not.toBe(row.system_type)
      expect(['团团通系统', '鸡蛋铺系统']).toContain(mapped.SystemType)
    }
    // 行内「删除绑定」按它取值 ⇒ 必须**恰好**等于 `id`（后端 `pk`）。
    expect(toBindingRow(ROW).BindHomeSystemLogId).toBe(ROW.id)
    expect(toBindingRow(JIDANPU_ROW).BindHomeSystemLogId).toBe(JIDANPU_ROW.id)
    // 反向哨兵：raw ISO 一个字都不许漏进单元格（断言**值**，不只断言"含 T"）。
    expect(toBindingRow(ROW).BindTimeStr).not.toBe(ROW.bound_at)
  })

  it('`systemTypeLabel`：两个契约值各有中文；⛔ 未知取值回 `—`（不把机器值当兜底）', () => {
    // 两个常量本身也钉字面（与上面那两条断言两处一起改，⛔ 不留恒真的自指断言）。
    expect(SYSTEM_TYPE_LABEL_TUANTUANTONG).toBe('团团通系统')
    expect(SYSTEM_TYPE_LABEL_JIDANPU).toBe('鸡蛋铺系统')
    // 未知 / 空取值走的是同一条出口（`toBindingRow` 就是页面真正用的那一处）。
    expect(toBindingRow({ ...ROW, system_type: '' }).SystemType).toBe('—')
    expect(toBindingRow({ ...ROW, system_type: 'xgj' }).SystemType).toBe('—')
    // ⛔ 未知取值**不许**落成两类中的任何一类（那会让用户以为它属于另一条产品线）。
    expect(toBindingRow({ ...ROW, system_type: 'xgj' }).SystemType).not.toBe('团团通系统')
    expect(toBindingRow({ ...ROW, system_type: 'xgj' }).SystemType).not.toBe('鸡蛋铺系统')
  })

  it('`HomeCompany`：鸡蛋铺行缺名称（`system_name` 空串）⇒ `—`，与 A19 同一份口径', () => {
    const named = toBindingRow(JIDANPU_ROW)
    expect(named.HomeCompany).toBe('测试上家乙')
    const nameless = toBindingRow({ ...JIDANPU_ROW, system_name: '' })
    expect(nameless.HomeCompany).toBe('—')
    // ⛔ 不许拿地址编一个名字（计划 §2.3）。
    expect(nameless.HomeCompany).not.toContain('upstream-b.test.invalid')
    // 与 A19 那块信息**共用同一份**口径（同一事实只判一次）。
    expect(upstreamSystemName({ system_name: '' })).toBe('—')
    expect(upstreamSystemName({ system_name: '测试上家乙' })).toBe('测试上家乙')
  })

  it('`BindTimeStr`：缺时间戳回 `—`（与 `emp-emplist` 对缺失时间戳的口径一致），⛔ 不是空串', () => {
    // 两类行的 `bound_at` 后端都是**非空**（团团通 `auto_now_add` / 鸡蛋铺 `created_at`）
    // ⇒ 空串是这里能构造的唯一"缺失"形态。
    expect(toBindingRow({ ...ROW, bound_at: '' }).BindTimeStr).toBe('—')
    expect(toBindingRow({ ...JIDANPU_ROW, bound_at: '' }).BindTimeStr).toBe('—')
  })

  it('`parseData` 空表：count 0、data []，不抛', async () => {
    const { cleanup } = await mount()
    const parsed = renderOpts().parseData!({ code: 0, msg: '', data: { items: [], total: 0 } })
    expect(parsed).toMatchObject({ code: 0, count: 0, data: [] })
    cleanup()
  })

  it('`parseData` 非空表：count = 服务端 total，两类行都在、列值逐个对得上', async () => {
    const { cleanup } = await mount()
    const parsed = renderOpts().parseData!({
      code: 0,
      msg: '',
      data: { items: [ROW, JIDANPU_ROW], total: 3 },
    })
    expect(parsed.count).toBe(3)
    expect(parsed.data).toHaveLength(2)
    expect(parsed.data[0]!.SystemType).toBe('团团通系统')
    expect(parsed.data[0]!.HomeCompany).toBe('测试上家甲')
    expect(parsed.data[0]!.HomeHost).toBe('https://peer-a.test.invalid')
    expect(parsed.data[0]!.HomeAcc).toBe('peer-account-a')
    expect(parsed.data[0]!.UserName).toBe('测试操作人甲')
    expect(parsed.data[0]!.BindTimeStr).toBe('2026-09-25 10:30')
    expect(parsed.data[0]!.BindHomeSystemLogId).toBe(11)
    // ⭐ 第二行是**鸡蛋铺**那一类（同一张表里混排，计划 §0 裁定 2/3）。
    expect(parsed.data[1]!.SystemType).toBe('鸡蛋铺系统')
    expect(parsed.data[1]!.HomeCompany).toBe('测试上家乙')
    expect(parsed.data[1]!.HomeAcc).toBe('kefu-up-b')
    expect(parsed.data[1]!.BindHomeSystemLogId).toBe(23)
    cleanup()
  })

  it('`parseData` 后端漏 items/total 时也不抛（回落成空页 + count 0）', async () => {
    const { cleanup } = await mount()
    expect(renderOpts().parseData!({ code: 0, msg: '', data: undefined })).toMatchObject({
      count: 0,
      data: [],
    })
    cleanup()
  })

  it('`listPeerBindings`（团团通那条链路的类型化出口）打的是 GET 集合端点', async () => {
    // 列表在页面上走 layui 的 `url` 通道；这个出口与 `api/auditLogs.ts::fetchAuditLogs` 同型，
    // 今天没有页面消费方 ⇒ 直接钉它拼出来的请求形状，别让它悄悄漂移。
    await listPeerBindings()
    const gets = callsByMethod('get')
    expect(gets).toHaveLength(1)
    expect(gets[0]!.url).toBe('/api/business/peer-bindings/')
  })
})

/* ───────────────────────── 弹窗：校验 + 提交（真交互） ───────────────────────── */

describe('弹窗：校验与提交（驱动真交互）', () => {
  it('四条校验文案逐字等于 `homesystemsetting.js` L150-165，顺序是名称→网址→账号→密码', () => {
    expect(VALIDATION_MESSAGES).toEqual({
      name: '请输入这个上家名称（可自定义名称）',
      url: '请输入这个上家的访问网址，例如http://d.jidanpu.com',
      acc: '请输入上家给你开的子账号',
      pwd: '请输入上家给你开的子账号登录密码，不然没法验证',
    })
    expect(validateBindingForm({ homeCompany: '', homeUrl: 'x', homeAcc: 'x', homePwd: 'x' })).toEqual(
      { error: VALIDATION_MESSAGES.name },
    )
    expect(validateBindingForm({ homeCompany: 'x', homeUrl: '', homeAcc: 'x', homePwd: 'x' })).toEqual(
      { error: VALIDATION_MESSAGES.url },
    )
    expect(validateBindingForm({ homeCompany: 'x', homeUrl: 'x', homeAcc: '', homePwd: 'x' })).toEqual(
      { error: VALIDATION_MESSAGES.acc },
    )
    expect(validateBindingForm({ homeCompany: 'x', homeUrl: 'x', homeAcc: 'x', homePwd: '' })).toEqual(
      { error: VALIDATION_MESSAGES.pwd },
    )
    expect(validateBindingForm({ homeCompany: 'x', homeUrl: 'x', homeAcc: 'x', homePwd: 'x' })).toEqual({
      error: '',
    })
  })

  it('弹层参数逐项照 `homesystemsetting.js` L87-96；`type:1` 由 `openPanelLayer` 补', () => {
    expect(bindingDialogOptions('CONTENT')).toEqual({
      title: '绑定一个新的上家系统',
      shade: [0.3, '#333'],
      shadeClose: true,
      skin: 'layui-layer-lan',
      anim: 8,
      area: ['680px', '480px'],
      content: 'CONTENT',
    })
  })

  it('工具栏「绑定一个新的上家系统」→ 开弹窗、**八个**字段按新口径预填、`re` 复位路径可走', async () => {
    const { cleanup } = await mount()
    await flush() // ⭐ D-0261：等来源字典回来（选项灌入发生在 openForm 里）。
    fireToolbar('addBtn')
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    const config = lastOpenConfig()
    expect(config.title).toBe('绑定一个新的上家系统')
    expect(config.type).toBe(1)
    expect(config.shade).toEqual([0.3, '#333'])
    expect(config.shadeClose).toBe(true)
    expect(config.skin).toBe('layui-layer-lan')
    expect(config.anim).toBe(8)
    expect(config.area).toEqual(['680px', '480px'])
    // 初值照对标 `addBind()`（L82-86）：网址预填 `http://`；密码**显式清空**（同文档复用节点）。
    // ⭐ 三格初值：系统类型回**默认**（鸡蛋铺）、百分比 `100`、开关**不勾**（布尔 `false`，
    //  ⛔ 不是字符串 `'false'`——真 layui 的复选框分支是 `l[0].checked = n`）。
    // ⭐ D-0261 起多第四格：来源回「不指定」（空串；提交时显式翻成 `null`）。
    // ⭐ P1起多两格大厅设置：**默认关 / 100**（新增弹窗不显示它们，但状态要复位）。
    expect(LAYUI.formVals[0]).toEqual({
      homeSystemType: 'jidanpu',
      homeHallPercent: '100',
      homeEnabled: false,
      homeOrderSource: '',
      homePeerHallSync: false,
      homePeerHallPercent: '100',
      homeCompany: '',
      homeUrl: 'http://',
      homeAcc: '',
      homePwd: '',
    })
    // 初值**真的落在 DOM 上**（替身的 `form.val` 是按 `<form lay-filter>` 作用域找输入的）。
    const panel = openedPanel()
    expect(panel.querySelector<HTMLInputElement>('[name="homeUrl"]')!.value).toBe('http://')
    expect(panel.querySelector<HTMLSelectElement>('[name="homeSystemType"]')!.value).toBe('jidanpu')
    expect(panel.querySelector<HTMLInputElement>('[name="homeHallPercent"]')!.value).toBe('100')
    expect(panel.querySelector<HTMLInputElement>('[name="homeEnabled"]')!.checked).toBe(false)
    // ⭐ D-0261：下拉被灌了字典（「不指定」+ 两项）、值停在「不指定」。
    const sourceSelect = panel.querySelector<HTMLSelectElement>('[name="homeOrderSource"]')!
    expect([...sourceSelect.options].map((o) => [o.value, o.textContent])).toEqual([
      ['', '不指定'],
      ['7', '竹屿'],
      ['9', '叶枫'],
    ])
    expect(sourceSelect.value).toBe('')
    cleanup()
  })

  it('⭐ 团团通分支：切到「团团通系统」→ 填四格 → 点「验证并保存绑定」⇒ POST 体逐字不变', async () => {
    const { cleanup } = await mount()
    fireToolbar('addBtn')
    const panel = openedPanel()
    // ⚠️ 默认是**鸡蛋铺**（`SYSTEM_TYPE_DEFAULT`）⇒ 这条用例必须先切到团团通，否则走的是 PUT。
    fireSelectSystemType(panel, 'tuantuantong')
    fill(panel, 'homeCompany', '测试上家甲')
    fill(panel, 'homeUrl', 'https://peer-a.test.invalid')
    fill(panel, 'homeAcc', 'peer-account-a')
    fill(panel, 'homePwd', 'peer-password-a')
    fireSubmit()
    await vi.waitFor(() => expect(callsByMethod('post')).toHaveLength(1))
    const post = callsByMethod('post')[0]!
    expect(post.url).toBe('/api/business/peer-bindings/')
    expect(post.data).toEqual({
      peer_name: '测试上家甲',
      peer_base_url: 'https://peer-a.test.invalid',
      peer_account: 'peer-account-a',
      password: 'peer-password-a',
    })
    // ⛔ 团团通分支**不碰**鸡蛋铺那条单行资源（两条链路各走各的端点）。
    expect(callsByMethod('put')).toHaveLength(0)
    cleanup()
  })

  it('成功：`layer.alert` → 确定回调里关弹窗 + 让 layui 重取列表', async () => {
    const { cleanup } = await mount()
    fireToolbar('addBtn')
    const panel = openedPanel()
    fireSelectSystemType(panel, 'tuantuantong')
    fill(panel, 'homeCompany', '测试上家甲')
    fill(panel, 'homeAcc', 'peer-account-a')
    fill(panel, 'homePwd', 'peer-password-a')
    fireSubmit()
    await vi.waitFor(() => expect(LAYUI.alert).toHaveBeenCalledTimes(1))
    // ⭐ 钉**字面**（不是 `BIND_OK_TEXT`）——否则改坏常量这条不会红。
    expect(LAYUI.alert.mock.calls[0]![0]).toBe('绑定成功')
    // 弹窗还没关：对标是「alert 的确定回调里才关」。
    expect(LAYUI.layer.close).not.toHaveBeenCalled()
    LAYUI.table.reload.mockClear()
    const cb = LAYUI.alert.mock.calls[0]![1] as (i: number) => void
    cb(99)
    expect(LAYUI.layer.close).toHaveBeenCalledWith(99)
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
    // ⭐ 钉字面（同刷新那条：`reload('#tableList')` 是静默 no-op）。
    expect(LAYUI.table.reload.mock.calls[0]![0]).toBe('tableList')
    cleanup()
  })

  it('⭐ 成功了但用户用 × / ESC 关掉 alert（确定回调没跑），再点保存仍然真发请求（复评 fix 1）', async () => {
    const { cleanup } = await mount()
    fireToolbar('addBtn')
    const panel = openedPanel()
    fireSelectSystemType(panel, 'tuantuantong')
    fill(panel, 'homeCompany', '测试上家甲')
    fill(panel, 'homeAcc', 'peer-account-a')
    fill(panel, 'homePwd', 'peer-password-a')
    fireSubmit()
    await vi.waitFor(() => expect(LAYUI.alert).toHaveBeenCalledTimes(1))
    expect(callsByMethod('post')).toHaveLength(1)
    // ⛔ 用户点 alert 右上角的 ×（或按 ESC）：`layer.alert` 的**确定回调根本不会跑**
    //（替身从不自动调回调 ⇒ 这正是那个形态）。表单弹窗**还开着**。
    expect(LAYUI.layer.close).not.toHaveBeenCalled()
    // 再点一次「验证并保存绑定」：必须**真的再发一发**。
    // 若把 `re = true` 只放在 alert 的确定回调里 ⇒ 这里会撞 `if (!re) return`：
    // **零请求、零提示**，只有关掉重开才恢复。
    // ⭐ 对标口径：成功回调的**第一句** `re = true`、再弹 alert。
    fireSubmit()
    await vi.waitFor(() => expect(callsByMethod('post')).toHaveLength(2))
    cleanup()
  })

  it('校验不过 ⇒ 只提示、**零写请求**（团团通分支的四格判据）', async () => {
    const { cleanup } = await mount()
    fireToolbar('addBtn')
    const panel = openedPanel()
    // ⚠️ 默认是鸡蛋铺 ⇒ 这条用例先切到团团通：它守的是**团团通那四格**的判据（名称最先判）。
    //    鸡蛋铺那三条缺项判据在下面「鸡蛋铺分支」那一节里各有一条。
    fireSelectSystemType(panel, 'tuantuantong')
    fireSubmit()
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith(VALIDATION_MESSAGES.name, { time: 1500 })
    // ⚠️ 挂载期**一个请求都不发**（那块表单连同它那条 GET 一起删了）
    //    ⇒ 判据回到最强的形态：整轮交互里一条请求都没有（逐个写方法点名，patch 也在内）。
    //    ⭐ D-0261 起唯一例外是 mount 那一发「来源字典」GET（弹窗下拉的数据源，与本轮交互无关）
    //    ⇒ 判据改钉「**除字典外**零请求」。
    expect(callsByMethod('get').filter((c) => !String(c.url).includes('order-sources'))).toEqual([])
    expect(callsByMethod('post')).toHaveLength(0)
    expect(callsByMethod('put')).toHaveLength(0)
    expect(callsByMethod('patch')).toHaveLength(0)
    expect(callsByMethod('delete')).toHaveLength(0)
    cleanup()
  })

  it('后端拒绝 ⇒ 原样显示它的 msg、**不关**弹窗、`re` 复位到可以重试', async () => {
    const { cleanup } = await mount()
    vi.mocked(request).mockRejectedValueOnce(new Error('上家拒绝了这次绑定，请核对账号密码') as never)
    fireToolbar('addBtn')
    const panel = openedPanel()
    fireSelectSystemType(panel, 'tuantuantong')
    fill(panel, 'homeCompany', '测试上家甲')
    fill(panel, 'homeAcc', 'peer-account-a')
    fill(panel, 'homePwd', 'peer-password-a')
    fireSubmit()
    await vi.waitFor(() =>
      expect(LAYUI.msg).toHaveBeenCalledWith('上家拒绝了这次绑定，请核对账号密码', { time: 1500 }),
    )
    // 失败不关弹窗（对标 L177/L181-187）。
    expect(LAYUI.layer.close).not.toHaveBeenCalled()
    expect(LAYUI.alert).not.toHaveBeenCalled()
    // ⭐ 失败分支**复位了闸**：不改任何字段再点一次，必须**真的再发一发**（否则用户被静默卡死）。
    fireSubmit()
    await vi.waitFor(() => expect(callsByMethod('post')).toHaveLength(2))
    cleanup()
  })

  it('⭐ 新增成功后**再开一次弹窗**，第二次提交仍然真的发请求（E-0079 的回归网）', async () => {
    const { cleanup } = await mount()
    // 第一次：开弹窗 → 填 → 提交成功 → 走 alert 的确定回调（关弹窗 + 重取）。
    fireToolbar('addBtn')
    let panel = openedPanel()
    fireSelectSystemType(panel, 'tuantuantong')
    fill(panel, 'homeCompany', '测试上家甲')
    fill(panel, 'homeAcc', 'peer-account-a')
    fill(panel, 'homePwd', 'peer-password-a')
    fireSubmit()
    await vi.waitFor(() => expect(LAYUI.alert).toHaveBeenCalledTimes(1))
    ;(LAYUI.alert.mock.calls[0]![1] as (i: number) => void)(99)
    expect(callsByMethod('post')).toHaveLength(1)

    // 第二次：**再开一次**弹窗。⚠️ 这条路径守的是「成功后还能再绑一个」这个**用户可见行为**；
    // 它**不再**能证明 `openForm` 的复位——成功分支（对标 L179-180）自己已把 `re` 复位回 true
    // ⇒ 删掉 `openForm` 里的 `re = true`，本用例照样绿。真正钉那次复位的判别用例是下面那条
    // 「请求还在途时…重开」（刻意**不走**成功路径，否则两条复位互相自洽、变成恒真，E-0035 族）。
    fireToolbar('addBtn')
    expect(LAYUI.open).toHaveBeenCalledTimes(2)
    panel = openedPanel()
    // ⚠️ 重开时类型**已复位**成鸡蛋铺 ⇒ 第二次也要显式切回团团通（否则走的是 PUT）。
    fireSelectSystemType(panel, 'tuantuantong')
    fill(panel, 'homeCompany', '测试上家乙')
    fill(panel, 'homeAcc', 'peer-account-b')
    fill(panel, 'homePwd', 'peer-password-b')
    fireSubmit()
    await vi.waitFor(() => expect(callsByMethod('post')).toHaveLength(2))
    expect(callsByMethod('post')[1]!.data).toMatchObject({ peer_name: '测试上家乙' })
    cleanup()
  })

  it('⭐ 请求还在途时用户自己关掉弹窗再重开，第二次提交仍然真的发请求（`openForm` 复位是承重的）', async () => {
    const { cleanup } = await mount()
    // 第一次：让 POST **永不落地**（= 用户没等结果就关掉了弹窗）⇒ `re` 停在 false。
    // ⚠️ 这条路**没有**经过成功的 alert 回调（那条路自己会 `re = true`）⇒ 只有 `openForm()`
    // 的复位能救第二次提交。⛔ 这就是「不靠成功路径」的判别用例（E-0035 族的自指断言防线）。
    vi.mocked(request).mockImplementationOnce(() => new Promise(() => {}) as never)
    fireToolbar('addBtn')
    let panel = openedPanel()
    fireSelectSystemType(panel, 'tuantuantong')
    fill(panel, 'homeCompany', '测试上家甲')
    fill(panel, 'homeAcc', 'peer-account-a')
    fill(panel, 'homePwd', 'peer-password-a')
    fireSubmit()
    await vi.waitFor(() => expect(callsByMethod('post')).toHaveLength(1))
    expect(LAYUI.alert, '第一次请求还没落地，不该弹成功 alert').not.toHaveBeenCalled()

    // 用户自己关掉弹窗（页面对「关闭」没有回调 ⇒ 重开是唯一可观察的「关掉再开」），再开一次。
    // 删掉 `openForm()` 的 `re = true`：下面第二次提交会在 `if (!re) return` 处**静默返回**
    // （零请求、零提示），请求数停在 1 ⇒ 本用例立刻红（spec §11.4 / E-0079）。
    fireToolbar('addBtn')
    expect(LAYUI.open).toHaveBeenCalledTimes(2)
    panel = openedPanel()
    fireSelectSystemType(panel, 'tuantuantong')
    fill(panel, 'homeCompany', '测试上家乙')
    fill(panel, 'homeAcc', 'peer-account-b')
    fill(panel, 'homePwd', 'peer-password-b')
    fireSubmit()
    await vi.waitFor(() => expect(callsByMethod('post')).toHaveLength(2))
    expect(callsByMethod('post')[1]!.data).toMatchObject({ peer_name: '测试上家乙' })
    cleanup()
  })

  it('弹层节点被 `table.reload` 摘掉后，第二次打开仍能从快照补回并预填', async () => {
    const { root, cleanup } = await mount()
    fireToolbar('addBtn')
    const stolen = openedPanel()
    stolen.remove() // 模拟 layui 重建工具栏那一带
    fireToolbar('addBtn')
    expect(LAYUI.open).toHaveBeenCalledTimes(2)
    const reopened = openedPanel()
    expect(reopened).not.toBe(stolen)
    expect(root.querySelector('form')!.contains(reopened)).toBe(true)
    expect(LAYUI.formVals[1]).toEqual({
      homeSystemType: 'jidanpu',
      homeHallPercent: '100',
      homeEnabled: false,
      homeOrderSource: '',
      homePeerHallSync: false,
      homePeerHallPercent: '100',
      homeCompany: '',
      homeUrl: 'http://',
      homeAcc: '',
      homePwd: '',
    })
    // ⭐ 从**快照**补出来的那一份也要带着三个新项（顺序是承重的：快照拍在注入**之后**）。
    expect(reopened.querySelector(`#${SYSTEM_TYPE_ITEM_ID}`)).not.toBeNull()
    expect(reopened.querySelector(`#${HALL_PERCENT_ITEM_ID}`)).not.toBeNull()
    expect(reopened.querySelector(`#${ENABLED_ITEM_ID}`)).not.toBeNull()
    expect(reopened.querySelector(`#${ORDER_SOURCE_ITEM_ID}`)).not.toBeNull()
    expect(reopened.querySelector('blockquote.layui-elem-quote')).toBeNull()
    cleanup()
  })

})

/* ══════════════ ⭐ 弹窗「系统类型」分流（业务方裁定） ══════════════ */

describe('⭐ 弹窗「系统类型」下拉 + 三个新项（业务方 2026-10-04 裁定）', () => {
  it('六个新项都在弹层里：`系统类型` 是 pane 的第一项、另五格依次紧跟密码那格；选项文案逐字', async () => {
    const { root, cleanup } = await mount()
    const panel = root.querySelector<HTMLElement>(PANEL_SELECTOR)
    expect(panel, `选择器 ${PANEL_SELECTOR} 匹配不到弹层`).not.toBeNull()
    const pane = panel!.querySelector<HTMLElement>('.layui-form-pane')
    expect(pane).not.toBeNull()
    // ⛔ 四个新项都必须在 `<form lay-filter="form">` **内部**：`form.val('form', …)` 按 form
    //    作用域找输入（vendored `layui.js` 的 `form.val` 是 `getFormElem(e).find('[name=…]')`），
    //    插到 form 外会**静默**取不到（先例 `system-sourelist.ts` 的同一段注释）。
    expect(panel!.closest('form[lay-filter="form"]')).not.toBeNull()

    // ① 「系统类型」是 `.layui-form-pane` 的**第一个子元素**（原来第一位是那条「注意」，已删）。
    const typeItem = panel!.querySelector<HTMLElement>(`#${SYSTEM_TYPE_ITEM_ID}`)
    expect(typeItem, '弹层里没有注入「系统类型」那一格').not.toBeNull()
    expect(pane!.firstElementChild).toBe(typeItem)
    expect(panel!.querySelector('blockquote.layui-elem-quote')).toBeNull()

    // ② 「百分比 + 启用 + 订单来源 + 两格大厅设置」依次紧跟在**密码那一格**之后
    //    （密码 → 百分比 → 启用 → 来源 → 同步上家抢单大厅 → 大厅价格百分比）。
    const percentItem = panel!.querySelector<HTMLElement>(`#${HALL_PERCENT_ITEM_ID}`)
    const enabledItem = panel!.querySelector<HTMLElement>(`#${ENABLED_ITEM_ID}`)
    const sourceItem = panel!.querySelector<HTMLElement>(`#${ORDER_SOURCE_ITEM_ID}`)
    expect(percentItem).not.toBeNull()
    expect(enabledItem).not.toBeNull()
    expect(sourceItem).not.toBeNull()
    const syncItem = panel!.querySelector<HTMLElement>(`#${PEER_HALL_SYNC_ITEM_ID}`)
    const peerPercentItem = panel!.querySelector<HTMLElement>(`#${PEER_HALL_PERCENT_ITEM_ID}`)
    expect(syncItem).not.toBeNull()
    expect(peerPercentItem).not.toBeNull()
    expect(dialogItem(panel!, 'homePwd').nextElementSibling).toBe(percentItem)
    expect(percentItem!.nextElementSibling).toBe(enabledItem)
    expect(enabledItem!.nextElementSibling).toBe(sourceItem)
    expect(sourceItem!.nextElementSibling).toBe(syncItem)
    expect(syncItem!.nextElementSibling).toBe(peerPercentItem)

    // ③ 下拉本身：`lay-filter` 就是事件名里那一段；两个选项的**值 + 文案**逐字。
    const select = panel!.querySelector<HTMLSelectElement>('[name="homeSystemType"]')
    expect(select).not.toBeNull()
    expect(select!.tagName).toBe('SELECT')
    expect(select!.getAttribute('lay-filter')).toBe('homeSystemType')
    expect([...select!.options].map((o) => [o.value, o.textContent])).toEqual([
      ['jidanpu', '鸡蛋铺系统'],
      ['tuantuantong', '团团通系统'],
    ])
    // 默认 = 鸡蛋铺系统（选项第一项即浏览器默认选中项；`openForm` 也显式写它）。
    expect(select!.value).toBe('jidanpu')

    // ④ 两格的控件形状（与后端模型那两列同口径：数字框 1–100、开关是 `lay-skin="switch"` 的复选框）。
    const percent = panel!.querySelector<HTMLInputElement>('[name="homeHallPercent"]')
    expect(percent).not.toBeNull()
    expect(percent!.type).toBe('number')
    expect(percent!.min).toBe('1')
    expect(percent!.max).toBe('100')
    expect(percent!.placeholder).toBe('100')
    const enabled = panel!.querySelector<HTMLInputElement>('[name="homeEnabled"]')
    expect(enabled).not.toBeNull()
    expect(enabled!.type).toBe('checkbox')
    expect(enabled!.getAttribute('lay-skin')).toBe('switch')
    expect(enabled!.getAttribute('lay-text')).toBe('启用|停用')

    // ⑤ 四格的标签文案（⭐ D-0261 加「订单来源」）。
    expect(typeItem!.querySelector('.layui-form-label')!.textContent).toBe('系统类型')
    expect(percentItem!.querySelector('.layui-form-label')!.textContent).toBe('发单价百分比')
    expect(enabledItem!.querySelector('.layui-form-label')!.textContent).toBe('启用')
    expect(sourceItem!.querySelector('.layui-form-label')!.textContent).toBe('订单来源')
    cleanup()
  })

  it('三个新项的视觉值 / 常量逐字取证据件那套（⛔ 不发明新值）', () => {
    // 先钉「证据件里确实是这三个值」（否则下面那几条断言可能在自说自话）。
    expect(EVIDENCE).toContain('style="width: 135px; padding: 8px 5px;"')
    expect(EVIDENCE).toContain('style="margin-left: 135px;"')
    expect(EVIDENCE).toContain('class="layui-input layui-input-shadow"')
    for (const html of [systemTypeItemHtml(), hallPercentItemHtml(), enabledItemHtml(), orderSourceItemHtml()]) {
      expect(html).toContain('class="layui-form-item"')
      expect(html).toContain('style="width: 135px; padding: 8px 5px;"')
      expect(html).toContain('style="margin-left: 135px;"')
    }
    // 三个 id（切换可见性靠它们）+ 三个 `name` 各就各位（钉**字面**，⛔ 不比被测常量）。
    expect(systemTypeItemHtml()).toContain('id="homeSystemTypeItem"')
    expect(systemTypeItemHtml()).toContain('name="homeSystemType"')
    expect(systemTypeItemHtml()).toContain('lay-filter="homeSystemType"')
    expect(hallPercentItemHtml()).toContain('id="homeHallPercentItem"')
    expect(hallPercentItemHtml()).toContain('name="homeHallPercent"')
    expect(hallPercentItemHtml()).toContain('class="layui-input layui-input-shadow"')
    expect(enabledItemHtml()).toContain('id="homeEnabledItem"')
    expect(enabledItemHtml()).toContain('name="homeEnabled"')
    // ⭐ D-0261：「订单来源」那一格（标记里只带「不指定」一项——字典项是打开弹窗时才灌的）。
    expect(orderSourceItemHtml()).toContain('id="homeOrderSourceItem"')
    expect(orderSourceItemHtml()).toContain('name="homeOrderSource"')
    expect(orderSourceItemHtml()).toContain('id="homeOrderSourceItem"')
    expect(orderSourceItemHtml()).toContain(
      `<option value="${ORDER_SOURCE_NONE_VALUE}">${ORDER_SOURCE_NONE_LABEL}</option>`,
    )
    expect(ORDER_SOURCE_FIELD_NAME).toBe('homeOrderSource')
    expect(ORDER_SOURCE_ITEM_ID).toBe('homeOrderSourceItem')
    expect(ORDER_SOURCE_NONE_VALUE).toBe('')
    expect(ORDER_SOURCE_NONE_LABEL).toBe('不指定')
    // 常量本身也钉住（两处一起改，⛔ 不留恒真的自指断言）。
    expect(SYSTEM_TYPE_OPTIONS).toEqual([
      { value: SYSTEM_TYPE_JIDANPU, label: '鸡蛋铺系统' },
      { value: SYSTEM_TYPE_TUANTUANTONG, label: '团团通系统' },
    ])
    expect(SYSTEM_TYPE_JIDANPU).toBe('jidanpu')
    expect(SYSTEM_TYPE_TUANTUANTONG).toBe('tuantuantong')
    expect(SYSTEM_TYPE_DEFAULT).toBe('jidanpu')
    expect(SYSTEM_TYPE_FIELD_NAME).toBe('homeSystemType')
    expect(SYSTEM_TYPE_SELECT_ID).toBe('homeSystemType')
    expect(HALL_PERCENT_FIELD_NAME).toBe('homeHallPercent')
    expect(ENABLED_FIELD_NAME).toBe('homeEnabled')
    expect(DIALOG_FIELD_NAMES).toEqual({
      company: 'homeCompany',
      url: 'homeUrl',
      acc: 'homeAcc',
      pwd: 'homePwd',
    })
    expect(SYSTEM_TYPE_ITEM_ID).toBe('homeSystemTypeItem')
    expect(HALL_PERCENT_ITEM_ID).toBe('homeHallPercentItem')
    expect(ENABLED_ITEM_ID).toBe('homeEnabledItem')
  })

  it('`injectDialogExtras` 幂等（再调两次不会多出第二份）；`null` ⇒ `false`（绝不静默成功）', async () => {
    const { root, cleanup } = await mount()
    const panel = root.querySelector<HTMLElement>(PANEL_SELECTOR)!
    expect(injectDialogExtras(panel)).toBe(true)
    expect(injectDialogExtras(panel)).toBe(true)
    for (const id of [SYSTEM_TYPE_ITEM_ID, HALL_PERCENT_ITEM_ID, ENABLED_ITEM_ID, ORDER_SOURCE_ITEM_ID]) {
      expect(panel.querySelectorAll(`#${id}`)).toHaveLength(1)
    }
    expect(injectDialogExtras(null)).toBe(false)
    cleanup()
  })

  it('`applySystemType` 是纯 DOM 函数：`null` 不抛；未知取值按**默认那一支**处理', async () => {
    // 直接调模块导出的那个函数（不经过弹窗）：分流只依赖传进来的节点与 `type`。
    expect(() => applySystemType(null, 'tuantuantong')).not.toThrow()
    const { root, cleanup } = await mount()
    const panel = root.querySelector<HTMLElement>(PANEL_SELECTOR)!
    applySystemType(panel, SYSTEM_TYPE_TUANTUANTONG)
    expect(visibility(panel)).toEqual(['', 'none', 'none', 'none'])
    // ⭐ 名称格两个分支都显示（鸡蛋铺写 `name`）⇒ 鸡蛋铺分支首格不再是 'none'。
    applySystemType(panel, SYSTEM_TYPE_JIDANPU)
    expect(visibility(panel)).toEqual(['', '', '', ''])
    // ⚠️ 取不到 / 传空串时按**默认那一支**（鸡蛋铺）处理，⛔ 不会留下「三格都露着」的中间态。
    applySystemType(panel, '')
    expect(visibility(panel)).toEqual(['', '', '', ''])
    cleanup()
  })

  it('⭐ 切换：驱动真回调 `select(homeSystemType)` ⇒ 三格 `display` 按类型分流（真交互）', async () => {
    const { cleanup } = await mount()
    fireToolbar('addBtn')
    const panel = openedPanel()
    // 打开时默认 = 鸡蛋铺系统 ⇒ 名称格照旧显示（⭐ 2026-10-29 起鸡蛋铺也用名称格）+ 百分比 / 启用。
    expect(visibility(panel)).toEqual(['', '', '', ''])
    fireSelectSystemType(panel, 'tuantuantong')
    expect(visibility(panel)).toEqual(['', 'none', 'none', 'none'])
    fireSelectSystemType(panel, 'jidanpu')
    expect(visibility(panel)).toEqual(['', '', '', ''])
    // ⛔ 判据是 `display` 那一层：两个分支都**不禁用**、也**不清值**（切过去再切回来东西还在，
    //    否则用户填一半切一下类型就白填了）。
    const percent = panel.querySelector<HTMLInputElement>('[name="homeHallPercent"]')!
    percent.value = '42'
    fireSelectSystemType(panel, 'tuantuantong')
    fireSelectSystemType(panel, 'jidanpu')
    expect(percent.value).toBe('42')
    expect(percent.disabled).toBe(false)
    cleanup()
  })

  it('⭐ 打开弹窗复位：上次切到「团团通」+ 改过两格，重开回到默认形态（E-0079 族）', async () => {
    const { cleanup } = await mount()
    fireToolbar('addBtn')
    let panel = openedPanel()
    fireSelectSystemType(panel, 'tuantuantong')
    fill(panel, 'homeHallPercent', '50')
    panel.querySelector<HTMLInputElement>('[name="homeEnabled"]')!.checked = true
    expect(visibility(panel)).toEqual(['', 'none', 'none', 'none'])

    fireToolbar('addBtn') // 关掉再打开
    panel = openedPanel()
    expect(panel.querySelector<HTMLSelectElement>('[name="homeSystemType"]')!.value).toBe('jidanpu')
    expect(panel.querySelector<HTMLInputElement>('[name="homeHallPercent"]')!.value).toBe('100')
    // ⚠️ 这一条正是替身的 `form.val` 必须照**真实现**走 `checked` 的原因：无脑 `field.value =
    //    String(value)` 时复选框的 `checked` 恒为 true ⇒ 这条会红，而页面其实是对的（E-0024 族）。
    expect(panel.querySelector<HTMLInputElement>('[name="homeEnabled"]')!.checked).toBe(false)
    // ⭐ 名称格两个分支都显示 ⇒ 重开（默认 = 鸡蛋铺）后首格不再是 'none'。
    expect(visibility(panel)).toEqual(['', '', '', ''])
    cleanup()
  })
})

/* ══════════════ ⭐ 弹窗「鸡蛋铺系统」分支：载荷 / 缺项 / 回填 ══════════════ */

describe('⭐ 弹窗「鸡蛋铺系统」分支（默认分支）', () => {
  /** 填「鸡蛋铺」分支那六格（名称 / 网址 / 账号 / 密码 / 百分比 / 开关）。 */
  function fillJidanpu(panel: HTMLElement): void {
    // ⭐ 名称格（选填）也填上——鸡蛋铺分支写后端的 `name` 列。
    fill(panel, 'homeCompany', '初音网游')
    fill(panel, 'homeUrl', 'https://upstream-b.test.invalid')
    fill(panel, 'homeAcc', 'kefu-up-b')
    fill(panel, 'homePwd', 'upstream-password-b')
    fill(panel, 'homeHallPercent', '50')
    panel.querySelector<HTMLInputElement>('[name="homeEnabled"]')!.checked = true
  }

  it('`validateJidanpuDialogForm`：顺序 url→acc→pwd，三句话逐字就是 `VALIDATION_MESSAGES` 那三句', () => {
    expect(validateJidanpuDialogForm({ homeUrl: '', homeAcc: 'x', homePwd: 'x' })).toEqual({
      error: '请输入这个上家的访问网址，例如http://d.jidanpu.com',
    })
    expect(validateJidanpuDialogForm({ homeUrl: 'x', homeAcc: '', homePwd: 'x' })).toEqual({
      error: '请输入上家给你开的子账号',
    })
    expect(validateJidanpuDialogForm({ homeUrl: 'x', homeAcc: 'x', homePwd: '' })).toEqual({
      error: '请输入上家给你开的子账号登录密码，不然没法验证',
    })
    expect(validateJidanpuDialogForm({ homeUrl: 'x', homeAcc: 'x', homePwd: 'x' })).toEqual({
      error: '',
    })
    // 逐条与那一份文案对齐（⛔ 没另造中文）；「名称」那一条**不在**这三格里。
    expect(validateJidanpuDialogForm({ homeUrl: '', homeAcc: '', homePwd: '' })).toEqual({
      error: VALIDATION_MESSAGES.url,
    })
  })

  it('⭐ 默认分支 → 填六格 → 提交 ⇒ **行级 POST** 载荷恰好七键（⛔ 不打 member 单行资源）', async () => {
    const { cleanup } = await mount()
    await flush() // ⭐ D-0261：等来源字典回来。
    fireToolbar('addBtn')
    const panel = openedPanel()
    fillJidanpu(panel)
    fireSubmit()
    await vi.waitFor(() => expect(callsByMethod('post')).toHaveLength(1))
    const post = callsByMethod('post')[0]!
    // ⭐ D-0256：新增 = `POST /api/business/upstream-bindings/jidanpu/`（行级创建）。
    //    旧写法打 member 那条单行资源 ⇒ 同账号第二条绑定被静默覆盖（E-0255）。
    expect(post.url).toBe('/api/business/upstream-bindings/jidanpu/')
    // 期望值是**字面量**（E-0024：⛔ 不比页面自造的中间态）。
    expect(post.data).toEqual({
      jidanpu_base_url: 'https://upstream-b.test.invalid',
      account: 'kefu-up-b',
      // ⭐ 名称格（选填）随载荷显式发（空串 = 清除；这里填了「初音网游」）。
      name: '初音网游',
      password: 'upstream-password-b',
      hall_price_percent: 50,
      enabled: true,
      // ⭐ D-0261：没动来源下拉 ⇒ 显式 `null`（= 不指定；不是省略键）。
      order_source_id: null,
    })
    expect(Object.keys(post.data as object).sort()).toEqual([
      'account', 'enabled', 'hall_price_percent', 'jidanpu_base_url',
      'name', 'order_source_id', 'password',
    ])
    // ⛔ 页面上没有那两个轮询间隔输入框 ⇒ 载荷里也不许出现（多带它们等于偷偷改库里的配置）。
    expect(post.data).not.toHaveProperty('hall_poll_seconds')
    expect(post.data).not.toHaveProperty('my_orders_poll_seconds')
    // ⛔ **任何** PUT 都不该有：member 单行资源不再是页面的写路径（那条路会把行覆盖掉）。
    expect(callsByMethod('put')).toHaveLength(0)
    cleanup()
  })

  it('⭐ 存完 ⇒ **在 `layer.alert` 回调之外**让统一列表重取（`table.reload`，收口后那块回填没了）', async () => {
    const { cleanup } = await mount()
    fireToolbar('addBtn')
    fillJidanpu(openedPanel())
    fireSubmit()
    await vi.waitFor(() => expect(LAYUI.alert).toHaveBeenCalledTimes(1))
    expect(LAYUI.alert.mock.calls[0]![0]).toBe('绑定成功')
    // ⛔ `layer.alert` 的确定回调**一次都没跑**（替身从不自动调它）⇒ 下面这次重取只可能来自
    //    回调**之外**那一句（放进回调里的话，用户用 × / ESC 关掉 alert 就永远看不到新存的那一行）。
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
    // ⭐ 钉字面（`reload('#tableList')` 是静默 no-op）——与团团通分支、刷新按钮同一个出口。
    expect(LAYUI.table.reload.mock.calls[0]![0]).toBe('tableList')
    expect(LAYUI.table.reload.mock.calls[0]![1]).toEqual({})

    // 确定回调只关弹窗（⛔ 不再补第二发重取）。
    ;(LAYUI.alert.mock.calls[0]![1] as (i: number) => void)(99)
    expect(LAYUI.layer.close).toHaveBeenCalledWith(99)
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
    cleanup()
  })

  it('⭐ 成功但用户用 × / ESC 关掉 alert ⇒ 再点保存仍然真发第二发（`re` 闸两个分支共用）', async () => {
    const { cleanup } = await mount()
    fireToolbar('addBtn')
    fillJidanpu(openedPanel())
    fireSubmit()
    await vi.waitFor(() => expect(LAYUI.alert).toHaveBeenCalledTimes(1))
    expect(callsByMethod('post')).toHaveLength(1)
    // ⛔ 确定回调没跑（替身从不自动调）⇒ 表单弹窗还开着；再点一次必须**真的再发一发**
    //（这条同时钉住「成功分支第一句就复位 `re`」这一条，两个分支共用同一个闸）。
    fireSubmit()
    await vi.waitFor(() => expect(callsByMethod('post')).toHaveLength(2))
    // ⛔ 第二次也没有变成 member PUT / peer POST（类型与模式都没有被上一次提交改掉）。
    expect(callsByMethod('put')).toHaveLength(0)
    cleanup()
  })

  it('失败 ⇒ 原样显示后端 msg（1500ms）、**不关**弹窗、`re` 复位到可以重试', async () => {
    UPSTREAM.rowError = '该账号打不开上家的抢单大厅，请让上家给它开这个权限'
    const { cleanup } = await mount()
    fireToolbar('addBtn')
    fillJidanpu(openedPanel())
    fireSubmit()
    await vi.waitFor(() =>
      expect(LAYUI.msg).toHaveBeenCalledWith(
        '该账号打不开上家的抢单大厅，请让上家给它开这个权限',
        { time: 1500 },
      ),
    )
    expect(LAYUI.layer.close).not.toHaveBeenCalled()
    expect(LAYUI.alert).not.toHaveBeenCalled()
    // 失败分支复位了闸 ⇒ 不改任何字段再点一次，必须**真的再发一发**。
    UPSTREAM.rowError = ''
    fireSubmit()
    await vi.waitFor(() => expect(callsByMethod('post')).toHaveLength(2))
    cleanup()
  })

  for (const [missing, message] of [
    ['homeUrl', '请输入这个上家的访问网址，例如http://d.jidanpu.com'],
    ['homeAcc', '请输入上家给你开的子账号'],
    ['homePwd', '请输入上家给你开的子账号登录密码，不然没法验证'],
  ] as const) {
    it(`缺 \`${missing}\` ⇒ **一个写请求都不发**、提示逐字对得上`, async () => {
      const { cleanup } = await mount()
      fireToolbar('addBtn')
      const panel = openedPanel()
      const values: Record<string, string> = {
        homeUrl: 'https://upstream-a.test.invalid',
        homeAcc: 'kefu-up-a',
        homePwd: 'upstream-password-a',
      }
      values[missing] = ''
      for (const [name, value] of Object.entries(values)) fill(panel, name, value)
      LAYUI.msg.mockClear()
      fireSubmit()
      await flush()
      // 钉**字面**（⛔ 不比 `VALIDATION_MESSAGES`：改坏常量这条不会红）。
      expect(LAYUI.msg).toHaveBeenCalledWith(message, { time: 1500 })
      expect(callsByMethod('put')).toHaveLength(0)
      expect(callsByMethod('post')).toHaveLength(0)
      expect(callsByMethod('patch')).toHaveLength(0)
      expect(callsByMethod('delete')).toHaveLength(0)
      cleanup()
    })
  }

  it('`readDialogJidanpuPayload`：网址 / 账号 / 名称 trim、密码**不 trim**、空格百分比按 100', async () => {
    const { cleanup } = await mount()
    fireToolbar('addBtn')
    const panel = openedPanel()
    fill(panel, 'homeCompany', '  初音网游  ')
    fill(panel, 'homeUrl', '  https://upstream-a.test.invalid  ')
    fill(panel, 'homeAcc', '  kefu-up-a  ')
    fill(panel, 'homePwd', ' pass word ')
    fill(panel, 'homeHallPercent', '')
    // ⭐ D-0261：来源下拉停在「不指定」（空串）⇒ 载荷显式给 `null`（后端口径：null = 清除）。
    expect(readDialogJidanpuPayload(panel)).toEqual({
      jidanpu_base_url: 'https://upstream-a.test.invalid',
      account: 'kefu-up-a',
      // ⭐ 名称 trim 后照发（空串 = 清除）。
      name: '初音网游',
      password: ' pass word ',
      hall_price_percent: 100,
      enabled: false,
      order_source_id: null,
    })
    // 弹层都取不到时给的是「全空 + 默认 100 + 不勾」这一份**确定**的值（⛔ 不抛、也不编别的）。
    expect(readDialogJidanpuPayload(null)).toEqual({
      jidanpu_base_url: '',
      account: '',
      name: '',
      password: '',
      hall_price_percent: 100,
      enabled: false,
      order_source_id: null,
    })
    cleanup()
  })
})

/* ══════════════ ⭐ 行内「编辑」+ 行级 PUT <pk>（E-0255 / D-0256） ══════════════ */

describe('⭐ 行内「编辑」入口 + 行级编辑（D-0256）', () => {
  /** 编辑弹窗回填的那份**库里现值**（= http 替身对行级 GET 的回包 `UPSTREAM_PUT_ROW`）。 */
  const EDIT_URL = '/api/business/upstream-bindings/jidanpu/'

  it('行内模板**运行期**注入「编辑」按钮：两类行都渲染（无条件）、证据件本体一字不动、幂等', async () => {
    // 先钉「对标证据件里确实没有这颗按钮」（否则「我方新增」就是假话）。
    expect(EVIDENCE).not.toContain(ROW_EDIT_EVENT)
    expect(EVIDENCE).toContain('submitDel')
    const { root, cleanup } = await mount()
    const tpl = root.querySelector<HTMLElement>(ROW_TOOLBAR_ELEM)
    expect(tpl, '行内模板不见了').not.toBeNull()
    const text = tpl!.textContent ?? ''
    expect(text).toContain(`lay-event="${ROW_EDIT_EVENT}"`)
    expect(ROW_EDIT_EVENT).toBe('submitEdit')
    // ⭐ 团团通行也能编辑 ⇒ 模板里**不再**带 `d.system_type` 的 laytpl 条件
    //（类型分流在点击期做）；⛔ 别把条件加回来——那会让团团通行又变成不可编辑。
    expect(text).not.toContain('d.system_type')
    expect(ROW_EDIT_BUTTON_TEMPLATE).not.toContain('d.system_type')
    // ⛔ 删除按钮原样保留（注入是**前置**，不是替换）。
    expect(text).toContain('submitDel')
    // 幂等：再调一次不多插一份。
    expect(injectRowEditButton(root)).toBe(true)
    expect((root.querySelector(ROW_TOOLBAR_ELEM)!.textContent ?? '').match(/submitEdit/g))
      .toHaveLength(1)
    cleanup()
  })

  it('点击「编辑」⇒ 先按主键读一次现值 → 打开标题为「编辑」的弹窗、按库值预填、类型格收起', async () => {
    const { cleanup } = await mount()
    await flush() // ⭐ D-0261：等来源字典回来（下拉选项与回填值都要它）。
    fireTool(ROW_EDIT_EVENT, cell(JIDANPU_ROW))
    // ⭐ 先读再开：统一列表 11 键没有 enabled / 百分比，不读库里现值就提交会把它们打回默认值。
    //    ⚠️ 等**弹窗打开**（GET 发出 ≠ 后续预填已经跑完，waitFor 打在终态上）。
    await vi.waitFor(() => expect(LAYUI.open).toHaveBeenCalledTimes(1))
    expect(rowGets()).toHaveLength(1)
    expect(rowGets()[0]!.url).toBe(`${EDIT_URL}23/`)
    // ⭐ 钉**字面**标题（新增弹窗的标题照对标原文，见 `bindingDialogOptions` 那组用例）。
    expect(lastOpenConfig().title).toBe('编辑上家鸡蛋铺绑定')
    expect(EDIT_DIALOG_TITLE).toBe('编辑上家鸡蛋铺绑定')
    // 预填 = **GET 回包的库值**（upstream-a / 80 / 启用），⛔ 不是列表行的显示值
    //（JIDANPU_ROW 是 upstream-b / 没有百分比——取错了来源这里立刻红）。
    // ⭐ D-0261：来源回填 GET 自带的 `{id, 名字}`（'7' = 竹屿）。
    expect(LAYUI.formVals[0]).toEqual({
      homeSystemType: 'jidanpu',
      homeHallPercent: '80',
      homeEnabled: true,
      homeOrderSource: '7',
      // 鸡蛋铺编辑不显示两格大厅设置 ⇒ 回默认值（⛔ 不残留上一次团团通编辑的值）。
      homePeerHallSync: false,
      homePeerHallPercent: '100',
      // ⭐ 名称格回填 = 行级 GET 自带的 `name`（没起名才是空串）。
      homeCompany: '测试上家乙名',
      homeUrl: 'https://upstream-a.test.invalid',
      homeAcc: 'kefu-up-stream-a',
      homePwd: '',
    })
    const panel = openedPanel()
    expect(panel.querySelector<HTMLInputElement>('[name="homeUrl"]')!.value)
      .toBe('https://upstream-a.test.invalid')
    // ⭐ D-0261：下拉的值**真的落在 DOM 上**（= 库里现值 7）。
    expect(panel.querySelector<HTMLSelectElement>('[name="homeOrderSource"]')!.value).toBe('7')
    // 编辑模式：系统类型那一格收起（编辑不改「这是哪类系统」）。
    expect(panel.querySelector<HTMLElement>(`#${SYSTEM_TYPE_ITEM_ID}`)!.style.display).toBe('none')
    cleanup()
  })

  it('编辑弹窗不动表单再提交 ⇒ **PUT 到那一行的主键**、密码空串照传（= 不改）', async () => {
    const { cleanup } = await mount()
    await flush() // ⭐ D-0261：等来源字典回来（不动下拉 ⇒ PUT 照发原值 7）。
    fireTool(ROW_EDIT_EVENT, cell(JIDANPU_ROW))
    await vi.waitFor(() => expect(LAYUI.open).toHaveBeenCalledTimes(1))
    fireSubmit()
    await vi.waitFor(() => expect(callsByMethod('put')).toHaveLength(1))
    const put = callsByMethod('put')[0]!
    // ⭐ 只动目标行：URL 带那一行的 id（`JIDANPU_ROW.id = 23`，⛔ 不是集合路径、更不是 member 资源）。
    expect(put.url).toBe(`${EDIT_URL}23/`)
    expect(put.data).toEqual({
      jidanpu_base_url: 'https://upstream-a.test.invalid',
      account: 'kefu-up-stream-a',
      // ⭐ 名称格整表单重发（= 回填的库里现值；清空它 = 清除名称）。
      name: '测试上家乙名',
      password: '',
      hall_price_percent: 80,
      enabled: true,
      // ⭐ D-0261：不动下拉 ⇒ 原值照发（库里配的 7 = 竹屿；不改就发原值，⛔ 不是省略键）。
      order_source_id: 7,
    })
    // ⛔ 编辑不是新增：一条 POST 都不该有。
    expect(callsByMethod('post')).toHaveLength(0)
    cleanup()
  })

  it('编辑时密码**可留空**（= 不改）：空密码不触发「请输入密码」那条校验、照发 PUT', async () => {
    const { cleanup } = await mount()
    fireTool(ROW_EDIT_EVENT, cell(JIDANPU_ROW))
    await vi.waitFor(() => expect(LAYUI.open).toHaveBeenCalledTimes(1))
    // 密码框本来就是空的（后端不回显）⇒ 直接提交必须放行。
    LAYUI.msg.mockClear()
    fireSubmit()
    await vi.waitFor(() => expect(callsByMethod('put')).toHaveLength(1))
    expect(LAYUI.msg).not.toHaveBeenCalled()
    cleanup()
  })

  it('编辑后关掉弹窗、再开「新增」⇒ 提交回到**行级 POST**、系统类型格放回（`editingRowId` 复位）', async () => {
    const { cleanup } = await mount()
    // 第一次：编辑 23 行（editingRowId = 23）。
    fireTool(ROW_EDIT_EVENT, cell(JIDANPU_ROW))
    await vi.waitFor(() => expect(LAYUI.open).toHaveBeenCalledTimes(1))
    // 第二次：开**新增**弹窗（`openForm` 必须清掉编辑目标，否则「新增」会把旧行覆盖掉）。
    fireToolbar('addBtn')
    expect(LAYUI.open).toHaveBeenCalledTimes(2)
    const panel = openedPanel()
    // ⭐ 系统类型那一格放回（编辑模式收起过它；不复位就是静默的旧形态，E-0079 同族）。
    expect(panel.querySelector<HTMLElement>(`#${SYSTEM_TYPE_ITEM_ID}`)!.style.display).toBe('')
    expect(panel.querySelector<HTMLInputElement>('[name="homeUrl"]')!.value).toBe('http://')
    fill(panel, 'homeUrl', 'https://upstream-c.test.invalid')
    fill(panel, 'homeAcc', 'kefu-up-c')
    fill(panel, 'homePwd', 'upstream-password-c')
    fireSubmit()
    await vi.waitFor(() => expect(callsByMethod('post')).toHaveLength(1))
    expect(callsByMethod('post')[0]!.url).toBe(EDIT_URL)
    cleanup()
  })

  it('那一行已经被别人删了 ⇒ 原样显示后端 msg、**不开**弹窗（绝不打开一个空弹窗）', async () => {
    const { cleanup } = await mount()
    UPSTREAM.rowReadError = '这个绑定不存在'
    fireTool(ROW_EDIT_EVENT, cell(JIDANPU_ROW))
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('这个绑定不存在', { time: 1500 }))
    expect(LAYUI.open).not.toHaveBeenCalled()
    cleanup()
  })

  it('⭐ 团团通行点「编辑」⇒ GET 一次详情 → 打开大厅设置弹窗、按库值回填两格、凭据格只读', async () => {
    const { cleanup } = await mount()
    await flush() // 来源字典
    fireTool(ROW_EDIT_EVENT, cell(ROW))
    await vi.waitFor(() => expect(LAYUI.open).toHaveBeenCalledTimes(1))
    // 读的是**团团通明细**端点（⛔ 不是鸡蛋铺的 upstream-bindings）。
    const gets = rowGets()
    expect(gets).toHaveLength(1)
    expect(gets[0]!.url).toBe('/api/business/peer-bindings/11/')
    expect(lastOpenConfig().title).toBe(PEER_EDIT_DIALOG_TITLE)
    expect(PEER_EDIT_DIALOG_TITLE).toBe('编辑上家团团通绑定')
    // 回填 = **GET 回包的库值**（true / 80），⛔ 不是列表行 ROW 的 false / 100。
    expect(LAYUI.formVals[0]).toMatchObject({
      homeSystemType: 'tuantuantong',
      homePeerHallSync: true,
      homePeerHallPercent: '80',
      homeCompany: '测试上家甲',
      homeUrl: 'https://peer-a.test.invalid',
      homeAcc: 'peer-account-a',
      homePwd: '',
    })
    const panel = openedPanel()
    expect(panel.querySelector<HTMLInputElement>(`[name="${PEER_HALL_SYNC_FIELD_NAME}"]`)!.checked)
      .toBe(true)
    expect(panel.querySelector<HTMLInputElement>(`[name="${PEER_HALL_PERCENT_FIELD_NAME}"]`)!.value)
      .toBe('80')
    // 两格**显示**（这一支编辑的就是它们）。
    expect(panel.querySelector<HTMLElement>(`#${PEER_HALL_SYNC_ITEM_ID}`)!.style.display).toBe('')
    expect(panel.querySelector<HTMLElement>(`#${PEER_HALL_PERCENT_ITEM_ID}`)!.style.display).toBe('')
    // 四个凭据格只读（PUT 只写两格 ⇒ 留成可编辑 = 让用户以为改了会生效）。
    for (const name of Object.values(DIALOG_FIELD_NAMES)) {
      expect(panel.querySelector<HTMLInputElement>(`[name="${name}"]`)!.readOnly).toBe(true)
    }
    cleanup()
  })

  it('⭐ 团团通编辑保存 ⇒ PUT 到那一行、载荷**只有**两格（⛔ 不带凭据）', async () => {
    const { cleanup } = await mount()
    await flush()
    fireTool(ROW_EDIT_EVENT, cell(ROW))
    await vi.waitFor(() => expect(LAYUI.open).toHaveBeenCalledTimes(1))
    const panel = openedPanel()
    // 用户改两格（照真交互：直接改 DOM，submit 从同一份 DOM 读）。
    panel.querySelector<HTMLInputElement>(`[name="${PEER_HALL_SYNC_FIELD_NAME}"]`)!.checked = true
    fill(panel, PEER_HALL_PERCENT_FIELD_NAME, '90')
    fireSubmit()
    await vi.waitFor(() => expect(callsByMethod('put')).toHaveLength(1))
    const put = callsByMethod('put')[0]!
    expect(put.url).toBe('/api/business/peer-bindings/11/')
    // 载荷**恰好两键**：⛔ 空密码 / 旧网址 / 名称 / 类型一个都不许带上。
    expect(put.data).toEqual({ hall_sync_enabled: true, hall_price_percent: 90 })
    // 成功后列表重取（与另一支同一出口）+ 那句前端成功提示（后端成功信封的 msg 是空串）。
    await vi.waitFor(() => expect(LAYUI.table.reload).toHaveBeenCalled())
    expect(LAYUI.alert).toHaveBeenCalledWith(PEER_HALL_SAVE_OK_TEXT, expect.any(Function))
    cleanup()
  })

  it('⛔ 大厅价格百分比越界 ⇒ 一次请求都不发、给字段级提示', async () => {
    const { cleanup } = await mount()
    await flush()
    fireTool(ROW_EDIT_EVENT, cell(ROW))
    await vi.waitFor(() => expect(LAYUI.open).toHaveBeenCalledTimes(1))
    const panel = openedPanel()
    fill(panel, PEER_HALL_PERCENT_FIELD_NAME, '0')
    fireSubmit()
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith(PEER_HALL_PERCENT_INVALID_TEXT, { time: 1500 })
    expect(callsByMethod('put')).toHaveLength(0)
    cleanup()
  })

  it('团团通明细读失败 ⇒ 原样显示后端 msg、**不开**弹窗', async () => {
    const { cleanup } = await mount()
    await flush()
    UPSTREAM.peerReadError = '这个绑定不存在'
    fireTool(ROW_EDIT_EVENT, cell(ROW))
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('这个绑定不存在', { time: 1500 }))
    expect(LAYUI.open).not.toHaveBeenCalled()
    cleanup()
  })

  it('⛔ 契约外的 system_type 点了「编辑」⇒ 不开弹窗、不发请求（fail-closed）', async () => {
    const { cleanup } = await mount()
    const unknown = { ...cell(ROW), system_type: '未知系统' }
    fireTool(ROW_EDIT_EVENT, unknown)
    await flush()
    expect(LAYUI.open).not.toHaveBeenCalled()
    expect(rowGets()).toHaveLength(0)
    cleanup()
  })
})



/* ══════════════ ⭐ 弹窗「订单来源」下拉（D-0261） ══════════════ */

describe('⭐ 弹窗「订单来源」下拉（D-0261）', () => {
  /** 选中下拉里的某个值（照真交互：先改 `<select>.value`，submit 从同一份 DOM 读）。 */
  function pickSource(panel: HTMLElement, value: string): void {
    const select = panel.querySelector<HTMLSelectElement>('[name="homeOrderSource"]')
    expect(select, '弹层里没有「订单来源」下拉').not.toBeNull()
    select!.value = value
  }

  it('新增：选「叶枫」→ 提交 ⇒ POST 体 `order_source_id: 9`（选了的必须真的发出去）', async () => {
    const { cleanup } = await mount()
    await flush() // ⭐ 等来源字典回来（没有选项就选不了 9）。
    fireToolbar('addBtn')
    const panel = openedPanel()
    fill(panel, 'homeUrl', 'https://upstream-b.test.invalid')
    fill(panel, 'homeAcc', 'kefu-up-b')
    fill(panel, 'homePwd', 'upstream-password-b')
    pickSource(panel, '9')
    fireSubmit()
    await vi.waitFor(() => expect(callsByMethod('post')).toHaveLength(1))
    expect(callsByMethod('post')[0]!.data).toHaveProperty('order_source_id', 9)
    cleanup()
  })

  it('编辑：改选「不指定」→ 提交 ⇒ PUT 体 `order_source_id: null`（= 清除，不是省略键）', async () => {
    const { cleanup } = await mount()
    await flush()
    fireTool(ROW_EDIT_EVENT, cell(JIDANPU_ROW))
    await vi.waitFor(() => expect(LAYUI.open).toHaveBeenCalledTimes(1))
    pickSource(openedPanel(), ORDER_SOURCE_NONE_VALUE)
    fireSubmit()
    await vi.waitFor(() => expect(callsByMethod('put')).toHaveLength(1))
    expect(callsByMethod('put')[0]!.data).toHaveProperty('order_source_id', null)
    cleanup()
  })

  it('⭐ 补口端到端：字典加载失败 + 编辑带来源的行 ⇒ 用行级 GET 自带的 {id, 名字} 补口、value 停在该 id', async () => {
    // ⭐ 这条是 `openEditForm` 里 `ensureOrderSourceOption(...)` 那句接线的**承重网**：
    //    字典挂了（下拉里没有 id=7）⇒ 唯一能让「库里的 7」出现在选项里的就是补口那一句；
    //    删掉它 ⇒ 选项只剩「不指定」、value 落空（「保存却变不指定」= 静默换掉用户配置）。
    UPSTREAM.sourcesError = '来源字典接口挂了'
    const { cleanup } = await mount()
    await flush() // ⭐ 等字典那一发失败落进 catch（orderSourceOptions = []）。
    fireTool(ROW_EDIT_EVENT, cell(JIDANPU_ROW))
    await vi.waitFor(() => expect(LAYUI.open).toHaveBeenCalledTimes(1))
    const select = openedPanel().querySelector<HTMLSelectElement>('[name="homeOrderSource"]')!
    // GET 回包 `UPSTREAM_PUT_ROW.order_source = {id: 7, 名'竹屿'}`——字典挂了它还在 ⇒ 补口真的发生。
    expect([...select.options].map((o) => [o.value, o.textContent])).toEqual([
      ['', '不指定'],
      ['7', '竹屿'],
    ])
    expect(select.value).toBe('7')
    cleanup()
  })

  it('字典取失败 ⇒ 下拉只剩「不指定」、保存照常（fail-soft：少一个可选项 ≠ 保存失败）', async () => {
    UPSTREAM.sourcesError = '来源字典接口挂了'
    const { cleanup } = await mount()
    await flush() // ⭐ 等那一发失败落进 catch（orderSourceOptions 回空）。
    fireToolbar('addBtn')
    const panel = openedPanel()
    const select = panel.querySelector<HTMLSelectElement>('[name="homeOrderSource"]')!
    expect([...select.options].map((o) => [o.value, o.textContent])).toEqual([['', '不指定']])
    fill(panel, 'homeUrl', 'https://upstream-b.test.invalid')
    fill(panel, 'homeAcc', 'kefu-up-b')
    fill(panel, 'homePwd', 'upstream-password-b')
    fireSubmit()
    await vi.waitFor(() => expect(callsByMethod('post')).toHaveLength(1))
    expect(callsByMethod('post')[0]!.data).toHaveProperty('order_source_id', null)
    cleanup()
  })

  it('`fillOrderSourceOptions` / `ensureOrderSourceOption`：灌字典幂等替换、补口不重复插', () => {
    const panel = document.createElement('div')
    panel.innerHTML = orderSourceItemHtml()
    const select = panel.querySelector<HTMLSelectElement>('select')
    expect(select, 'orderSourceItemHtml() 里应有一个 <select>').not.toBeNull()
    const values = (): string[] => [...select!.options].map((o) => o.value)
    fillOrderSourceOptions(panel, [
      { id: 7, name: '竹屿', remark: '', order_by: 0, shop_type: 0, group: null, group_name: '', created_at: 'x', updated_at: 'x' },
    ])
    expect(values()).toEqual(['', '7'])
    // 再灌一次 = **替换**（不是追加两份「不指定」）。
    fillOrderSourceOptions(panel, [
      { id: 9, name: '叶枫', remark: '', order_by: 0, shop_type: 0, group: null, group_name: '', created_at: 'x', updated_at: 'x' },
    ])
    expect(values()).toEqual(['', '9'])
    // 补口：库里那项不在字典里 ⇒ 追加；再补一次 ⇒ **不**重复插。
    ensureOrderSourceOption(panel, { id: 5, name: '星辰' })
    ensureOrderSourceOption(panel, { id: 5, name: '星辰' })
    expect(values()).toEqual(['', '9', '5'])
    // `null` 面板不抛（⛔ 不静默成功，也绝不炸页面）。
    expect(() => fillOrderSourceOptions(null, [])).not.toThrow()
    expect(() => ensureOrderSourceOption(null, { id: 1, name: 'x' })).not.toThrow()
  })
})

/* ───────────────── 逐字文案的**字面**钉子 ───────────────── */

describe('对标逐字文案的字面钉子（⛔ 不是自指断言：改常量这里就红）', () => {
  it('解绑确认语 = `homesystemsetting.js` L100 原话（一个字都不许改）', () => {
    expect(UNBIND_CONFIRM_TEXT).toBe(
      '确定要解除与这个上家的绑定吗？注意：解绑后，上家指派过来的新订单无法同步，但在本系统解绑之前同步过来的订单不受影响',
    )
  })

  it('绑定成功提示 = `绑定成功`', () => {
    expect(BIND_OK_TEXT).toBe('绑定成功')
  })

  it('解绑成功提示 = `已解绑`（同一族的第三个文案，顺手一起钉）', () => {
    expect(UNBIND_OK_TEXT).toBe('已解绑')
  })

  it('⭐ 鸡蛋铺那一条删除确认语 = 计划 §2.3 逐字（我方自拟、必须写明级联代价）', () => {
    expect(JIDANPU_UNBIND_CONFIRM_TEXT).toBe(
      '确定删除这条上家鸡蛋铺绑定吗？删除后，它名下还在跟踪的同步链路会一并删除（订单与上家单的对应关系不再保留），同步台账保留。',
    )
    // ⛔ 它必须讲清两件事（对标那一句没讲）：链路会一并删、台账保留。
    expect(JIDANPU_UNBIND_CONFIRM_TEXT).toContain('同步链路会一并删除')
    expect(JIDANPU_UNBIND_CONFIRM_TEXT).toContain('同步台账保留')
  })
})

/* ───────────────────────── 解绑（DELETE） ───────────────────────── */

describe('解绑：确认语逐字 + DELETE + 重取列表', () => {
  it('行内「删除绑定」⇒ `layer.confirm` 用对标 L100 的原话；确认后 DELETE(id)', async () => {
    const { cleanup } = await mount()
    fireTool('submitDel', cell())
    expect(LAYUI.layer.confirm).toHaveBeenCalledTimes(1)
    // ⭐ fix 3：钉**字面**（不是 `UNBIND_CONFIRM_TEXT`）。
    expect(LAYUI.layer.confirm.mock.calls[0]![0]).toBe(
      '确定要解除与这个上家的绑定吗？注意：解绑后，上家指派过来的新订单无法同步，但在本系统解绑之前同步过来的订单不受影响',
    )
    expect(LAYUI.layer.confirm.mock.calls[0]![1]).toEqual({ icon: 3, title: '提示' })
    const cb = LAYUI.layer.confirm.mock.calls[0]![2] as (i: number) => void
    cb(5)
    await vi.waitFor(() => expect(callsByMethod('delete')).toHaveLength(1))
    expect(callsByMethod('delete')[0]!.url).toBe('/api/business/peer-bindings/11/')
    expect(LAYUI.layer.close).toHaveBeenCalledWith(5)
    await vi.waitFor(() => expect(LAYUI.table.reload).toHaveBeenCalledTimes(1))
    // ⭐ fix 3：同样钉字面。
    expect(LAYUI.msg).toHaveBeenCalledWith('已解绑', { time: 1500 })
    cleanup()
  })

  it('解绑失败 ⇒ 原样显示后端 msg、**不**重取列表；之后再解绑仍能发请求（`finally` 复位）', async () => {
    const { cleanup } = await mount()
    vi.mocked(request).mockRejectedValueOnce(new Error('这个绑定不存在') as never)
    fireTool('submitDel', cell())
    ;(LAYUI.layer.confirm.mock.calls[0]![2] as (i: number) => void)(5)
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('这个绑定不存在', { time: 1500 }))
    expect(LAYUI.table.reload).not.toHaveBeenCalled()
    // ⭐ 闸被 `finally` 复位 ⇒ 第二次解绑**真的再发一发**（漏了 finally 会静默卡死）。
    fireTool('submitDel', cell())
    ;(LAYUI.layer.confirm.mock.calls[1]![2] as (i: number) => void)(6)
    await vi.waitFor(() => expect(callsByMethod('delete')).toHaveLength(2))
    cleanup()
  })

  it('行内事件的 id 取自 `BindHomeSystemLogId`（不是 `id` 以外的任何东西）', async () => {
    const { cleanup } = await mount()
    // 合成一行：两个 id 值**故意不同**，这样「取错了字段」会红（取 `id` 还是 `BindHomeSystemLogId`
    // 在两者相等时无法分辨——那是恒真断言）。
    const weird = { ...cell(), id: 11, BindHomeSystemLogId: 42 }
    fireTool('submitDel', weird)
    ;(LAYUI.layer.confirm.mock.calls[0]![2] as (i: number) => void)(5)
    await vi.waitFor(() => expect(callsByMethod('delete')).toHaveLength(1))
    expect(callsByMethod('delete')[0]!.url).toBe('/api/business/peer-bindings/42/')
    cleanup()
  })

  it('⭐ 鸡蛋铺行 ⇒ 我方自拟的确认语（写明级联代价）+ DELETE 到统一列表那条删除端点', async () => {
    const { cleanup } = await mount()
    // 走的是**行内真事件**（`table.on('tool(tableList)')` 的替身就是页面登记的那个真回调）。
    fireTool('submitDel', cell(JIDANPU_ROW))
    expect(LAYUI.layer.confirm).toHaveBeenCalledTimes(1)
    // ⭐ 钉**字面**（⛔ 不比 `JIDANPU_UNBIND_CONFIRM_TEXT`：改坏常量时那条自指断言不会红）。
    expect(LAYUI.layer.confirm.mock.calls[0]![0]).toBe(
      '确定删除这条上家鸡蛋铺绑定吗？删除后，它名下还在跟踪的同步链路会一并删除（订单与上家单的对应关系不再保留），同步台账保留。',
    )
    // ⛔ 与团团通那一句**不是**同一句（它说的是软删 + 通知上家，两件事不一样）。
    expect(LAYUI.layer.confirm.mock.calls[0]![0]).not.toBe(UNBIND_CONFIRM_TEXT)
    expect(LAYUI.layer.confirm.mock.calls[0]![1]).toEqual({ icon: 3, title: '提示' })
    ;(LAYUI.layer.confirm.mock.calls[0]![2] as (i: number) => void)(7)
    await vi.waitFor(() => expect(callsByMethod('delete')).toHaveLength(1))
    // ⭐ **请求形状**：端点 + **那一行的 id**（`JIDANPU_ROW.id = 23`，⛔ 不是 `ROW.id`）。
    expect(callsByMethod('delete')[0]!.url).toBe('/api/business/upstream-bindings/jidanpu/23/')
    // ⛔ 鸡蛋铺那一支**一条道都不许**走团团通那条软删端点（分流写死走团团通这里立刻红）。
    expect(
      callsByMethod('delete').filter((c) => String(c.url).includes('peer-bindings')),
    ).toEqual([])
    expect(LAYUI.layer.close).toHaveBeenCalledWith(7)
    await vi.waitFor(() => expect(LAYUI.table.reload).toHaveBeenCalledTimes(1))
    expect(LAYUI.msg).toHaveBeenCalledWith('已解绑', { time: 1500 })
    cleanup()
  })

  it('⭐ 团团通行**不**走鸡蛋铺那条端点（分流的另一半：⛔ 不许两条都打）', async () => {
    const { cleanup } = await mount()
    fireTool('submitDel', cell(ROW))
    ;(LAYUI.layer.confirm.mock.calls[0]![2] as (i: number) => void)(5)
    await vi.waitFor(() => expect(callsByMethod('delete')).toHaveLength(1))
    expect(callsByMethod('delete')[0]!.url).toBe('/api/business/peer-bindings/11/')
    expect(
      callsByMethod('delete').filter((c) => String(c.url).includes('upstream-bindings')),
    ).toEqual([])
    cleanup()
  })

  it('⭐ 鸡蛋铺删除的失败分支也原样显示后端 msg、不重取，且共用那道 `deleting` 闸（finally 复位）', async () => {
    const { cleanup } = await mount()
    vi.mocked(request).mockRejectedValueOnce(new Error('这个绑定不存在') as never)
    fireTool('submitDel', cell(JIDANPU_ROW))
    ;(LAYUI.layer.confirm.mock.calls[0]![2] as (i: number) => void)(7)
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('这个绑定不存在', { time: 1500 }))
    expect(LAYUI.table.reload).not.toHaveBeenCalled()
    // 闸被 `finally` 复位 ⇒ 第二次（团团通行）仍然真的再发一发。
    fireTool('submitDel', cell(ROW))
    ;(LAYUI.layer.confirm.mock.calls[1]![2] as (i: number) => void)(8)
    await vi.waitFor(() => expect(callsByMethod('delete')).toHaveLength(2))
    expect(callsByMethod('delete')[1]!.url).toBe('/api/business/peer-bindings/11/')
    cleanup()
  })
})

/* ══════════════ 收口：表格下方那一整块**已删除**（业务方裁定） ══════════════ */

/**
 * ⭐ **收口**（业务方原话：「我看绑定上家页面有两个地方可以新增上家系统 是重复了吗？
 * 一个是点击按钮新增 一个是直接下面地方输入新增」）：
 *
 * 那一整块（含它的样式注入 / 取数锁 / 保存 / 测试连接 / 立即同步 / 健康状态）**整段移除**，
 * 页面上只留工具栏那颗「绑定一个新的上家系统」弹窗一个新增入口。
 *
 * ⚠️ 这里**不**去断「某个常量不存在 / 某函数没导出」（`typecheck` 与 `grep` 已经管住那件事），
 * 只断**用户在页面上看得见**的终态：DOM 里没有那一块、也没有它的三颗按钮与健康状态。
 * ⚠️ 判据打在**运行期 DOM** 上（`AGENTS.md` §5-E / E-0161：⛔ 不是「文件文本里搜不到」）。
 */
describe('收口：表格下方那块表单在运行期 DOM 里一个节点都不剩', () => {
  it('mount 后页面上没有那块表单（类名 / 四格 / 三颗按钮 / 健康状态全都不在）', async () => {
    const { root, cleanup } = await mount()
    expect(root.querySelector('.jidanpuUpstreamBinding')).toBeNull()
    for (const id of [
      'upstreamBindingSaveBtn',
      'upstreamBindingTestBtn',
      'upstreamBindingSyncBtn',
      'upstreamBindingHealth',
    ]) {
      expect(root.querySelector(`#${id}`), `收口后不该再有 #${id}`).toBeNull()
    }
    for (const name of [
      'upstream_base_url',
      'upstream_account',
      'upstream_password',
      'upstream_hall_percent',
      'upstream_enabled',
    ]) {
      expect(root.querySelector(`[name="${name}"]`), `收口后不该再有 name=${name}`).toBeNull()
    }
    // 那段作用域限于本块的样式也随块一起下线（它没有别的消费者）。
    expect(document.getElementById('upstreamBindingBlockStyle')).toBeNull()
    // 那些**用户看得见**的按钮文案一个都不该留（正向对照：工具栏那颗仍在，证明不是「整页空」）。
    expect(root.textContent).not.toContain('测试连接')
    expect(root.textContent).not.toContain('立即同步')
    expect(root.textContent).toContain('绑定一个新的上家系统')
    cleanup()
  })
})

/* ══════ 撤除：上家这一侧的②式（官方开放平台 API）在**运行期 DOM** 里一个节点都不剩 ══════ */

/**
 * ⭐ **撤除**（业务方原话：「**绑定上家这边我不需要啊 谁绑定上家是去用别人上家的
 * api 密钥的？**」）：要绑上家，用的是**别人家**的鸡蛋铺账号 —— 你手里**不可能有别人的 API
 * 密钥 ⇒ 这种对接方式**只对「自己那套」有意义**（下家线，另一条分支在做），上家这一侧不存在
 * 这种用法。
 *
 * ⚠️ 判据打在**运行期 DOM** 上（`AGENTS.md` §5-E / E-0161：⛔ 不是「文件文本里搜不到」），
 * 而且**打开弹窗之后**再断——那几格全是**运行期注入**的，不打开弹窗断不到它们。
 * ⚠️ 每条负向判据后面都跟一条**正向对照**（①式那四格仍在），免得「整张弹窗是空的」也判绿。
 * ⚠️ 标识符用的是 T10 当年**真的**用过的那几个（`homeJidanpuMode` / `homeClientId` /
 * `homeClientSecret` / `homeCallback*` / `homeOpenApiTest*`）⇒ 谁把那一套加回来，这里就红。
 */
describe('撤除②式上家：弹窗里「对接方式」那一整套在运行期 DOM 里一个节点都不剩', () => {
  it('打开弹窗 ⇒ 没有「对接方式」下拉 / client_id / client_secret / 回调地址 / 逐条表 / 测试连接', async () => {
    const { cleanup } = await mount()
    fireToolbar('addBtn')
    const panel = openedPanel()
    // ① 「对接方式」那个下拉（`name` 与容器 id 都断）。
    expect(panel.querySelector('[name="homeJidanpuMode"]'), '撤除后不该再有「对接方式」下拉').toBeNull()
    expect(panel.querySelector('#homeJidanpuModeItem')).toBeNull()
    expect(panel.textContent).not.toContain('对接方式')
    expect(panel.textContent).not.toContain('官方开放平台 API')
    // ② `client_id` / `client_secret` 两格。
    expect(panel.querySelector('[name="homeClientId"]')).toBeNull()
    expect(panel.querySelector('[name="homeClientSecret"]')).toBeNull()
    expect(panel.querySelector('#homeClientIdItem')).toBeNull()
    // ③ 回调地址那一格（容器 / 地址输入 / 地址行 / 原因行 / 复制按钮）。
    expect(panel.querySelector('#homeCallbackItem')).toBeNull()
    expect(panel.querySelector('#homeCallbackUrl')).toBeNull()
    expect(panel.querySelector('#homeCallbackRow')).toBeNull()
    expect(panel.querySelector('#homeCallbackReason')).toBeNull()
    expect(panel.querySelector('#homeCallbackCopy')).toBeNull()
    expect(panel.textContent).not.toContain('回调地址')
    // ④ 那颗「测试连接」按钮 + 它的结果容器（**逐条权限表唯一的出口**就在那个容器里
    //    ⇒ 容器不在 = 逐条表没有任何出口，与「撤掉」等价）。
    expect(panel.querySelector('#homeOpenApiTestItem')).toBeNull()
    expect(panel.querySelector('#homeOpenApiTestBtn')).toBeNull()
    expect(panel.querySelector('#homeOpenApiTestResult')).toBeNull()
    expect(panel.textContent, '撤除后⛔ 不许再有「测试连接」这颗按钮（2026-10-04 裁定「不要」）')
      .not.toContain('测试连接')
    expect(panel.textContent).not.toContain('该做什么')
    // ⑤ **正向对照**：①式那四格与「系统类型」下拉**仍在**（证明不是「整张弹窗空」那种假绿）。
    for (const name of ['homeCompany', 'homeUrl', 'homeAcc', 'homePwd']) {
      expect(panel.querySelector(`[name="${name}"]`), `①式的 name=${name} 必须还在`).not.toBeNull()
    }
    expect(panel.querySelector('[name="homeSystemType"]')).not.toBeNull()
    cleanup()
  })
})


/* ══════════════ ⭐ 大厅镜像 P1 的两格（同步上家抢单大厅 / 大厅价格百分比） ══════════════ */

describe('⭐ 弹窗两格大厅设置（P1，2026-10-25）', () => {
  it('两格的标记：开关带 name / 中文标签 / 两态文案且**不预勾**；百分比 1–100 默认 100 带人话提示', () => {
    const sync = peerHallSyncItemHtml()
    expect(sync).toContain(`name="${PEER_HALL_SYNC_FIELD_NAME}"`)
    expect(sync).toContain(PEER_HALL_SYNC_LABEL)
    expect(sync).toContain('lay-skin="switch"')
    // ⛔ 默认关：标记里不许写 `checked`（勾选状态只由 `form.val` 决定）。
    expect(sync).not.toContain('checked')

    const percent = peerHallPercentItemHtml()
    expect(percent).toContain(`name="${PEER_HALL_PERCENT_FIELD_NAME}"`)
    expect(percent).toContain(PEER_HALL_PERCENT_LABEL)
    expect(percent).toContain('min="1"')
    expect(percent).toContain('max="100"')
    expect(percent).toContain(`value="${PEER_HALL_PERCENT_DEFAULT}"`)
    // 业务方要求的那句人话。
    expect(percent).toContain('打手看到的价格 = 上家大厅价 × 这个百分比')
  })

  it('`peerHallSettingsOf`：老数据 / 坏值一律回**关 / 100**（⛔ 不是 0、不是 undefined）', () => {
    expect(peerHallSettingsOf(undefined)).toEqual({ hall_sync_enabled: false, hall_price_percent: 100 })
    expect(peerHallSettingsOf(null)).toEqual({ hall_sync_enabled: false, hall_price_percent: 100 })
    // 开关只认严格 true；百分比只认 1–100 的整数。
    expect(peerHallSettingsOf({ hall_sync_enabled: true, hall_price_percent: 45 }))
      .toEqual({ hall_sync_enabled: true, hall_price_percent: 45 })
    for (const bad of [0, 101, -1, 90.5, null, undefined]) {
      expect(
        peerHallSettingsOf({ hall_sync_enabled: false, hall_price_percent: bad }).hall_price_percent,
      ).toBe(100)
    }
  })

  it('`readPeerHallSettings`：空串按 100、越界给字段级错误（⛔ 不发请求的判据在页用例里）', async () => {
    const { cleanup } = await mount()
    fireToolbar('addBtn')
    const panel = openedPanel()
    fill(panel, PEER_HALL_PERCENT_FIELD_NAME, '')
    expect(readPeerHallSettings(panel)).toEqual({
      hall_sync_enabled: false,
      hall_price_percent: 100,
      error: '',
    })
    fill(panel, PEER_HALL_PERCENT_FIELD_NAME, '101')
    expect(readPeerHallSettings(panel)).toMatchObject({
      hall_price_percent: 100,
      error: PEER_HALL_PERCENT_INVALID_TEXT,
    })
    cleanup()
  })
})
