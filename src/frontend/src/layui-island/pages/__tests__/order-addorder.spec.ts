// @vitest-environment jsdom
/**
 * 【录入订单】layui 复刻页的 spec：表单载荷 / 联动 / 粘贴解析 / 保存门 / 编辑态等
 * 行为基线的逐条防漂移测试（录单总价必填等口径见各 describe，判据指针 D-0082）。
 */
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { LayuiModule } from '@/layui/loader'
// 载荷构造用**页面在 `onSave` 里调的同一份**函数——不另造一份断言用的替身。
import { formToPayload } from '@/views/orders/orderFormFields'
import {
  FIELD_MAP,
  applyPasteToDom,
  IGNORED_FIELDS,
  MONEY_DOM_SELECTORS,
  NOT_OPEN_CONTROLS,
  NO_DETAIL_PERMISSION_MESSAGE,
  PASTE_HINT_LABEL_SUFFIX,
  PROJECT_TEXT_NAME,
  dayHoursToTimeRequirement,
  editOrderNoFromSearch,
  emptyAddOrderForm,
  firstMoneyError,
  formToUpdatePayload,
  labelOverrides,
  projectTextSyncLimitError,
  mountOrderAddOrder,
  orderSourceOptions,
  orderToAddOrderForm,
  readOrderSource,
  resetFormToInitial,
  SAVE_SUCCESS_MESSAGE,
  ORDER_SAVED_MESSAGE_TYPE,
  orderTypeValue,
  readFormState,
  readSalesAgent,
  readSalesAgentChoice,
  renderTemplate,
  SALES_AGENT_FAILED_MESSAGE,
  requiredError,
  SALES_AGENT_CONTAINER_SELECTOR,
  SALES_AGENT_INPUT_NAME,
  SPEAKING_READ_ORDER_CODES,
  totalPriceRequiredError,
  resolveNotOpenHosts,
  timeRequirementToDayHours,
  writeFormState,
} from '../order-addorder'
import { readPlatformOrder, type Order } from '@/api/orders'
import { ORDER_MONEY_FIELDS } from '@/config/money'

/**
 * layui 的**可控替身**（只用于编辑态保存分支那组**挂载**用例）。
 *
 * `onSave` 是 `mountOrderAddOrder` 的闭包、没有导出，纯函数测不到；`bootstrapIsland` 在 jsdom 里
 * 又加载不了真的 layui。所以把它换成替身，`form.on('submit(savebtn)')` 的回调与 `layer.msg` 的
 * 入参才拿得到（提示文案断言打在真实调用上，不是断言某个常量存在）。
 */
const LAYUI = vi.hoisted(() => {
  /**
   * formSelects 的**忠实替身**（只实现本页用到的那部分）。
   *
   * ⚠️ 它照抄 vendored 插件的**事件时序**——这是本组用例能判 C1 的前提
   * （真插件实测见 `order-addorder-formselects.spec.ts`，探针原始输出见计划 Part 4）：
   *  - `fsPick(id, v)`（真人点一个 `dd`）：先 `on`（**改前**值）→ radio 先清后 push → 再 `endOn`（**改后**值）
   *    （`public/vendor/formselects-v4.js:1177`（`on`）/ `:1182`（`vals.push`）/ `:1196`（`endOn`）；
   *    radio 的 `addLabel`（`:1199`）在 `:1209-1213` 先 `values.length=0`）。
   *  - `fsClear(id)`（radio 的「清空」快捷按钮，`:169`/`:113`）：`on`（改前）→ 清空 → `endOn`（**空集**）。
   *  - `value(id, arr, true)`（插件 API 赋初值）：**不触发**任何钩子（`handlerLabel(..., notOn=true)`），
   *    且**只收录 `arr` 里真能在 `dd[lay-value]` 命中的 id**——命不中就跳过（`:1562` 的 `if ((dd = …)[0])`；
   *    `:1566` 的 `valToName` 对非联动的本地字典取不到名字（`dl.find('.xm-select-linkage-group…')` 为空 ⇒ 空串）
   *    ⇒ `if (name)` 也不成立）。⚠️ 无条件写入的替身会把「真插件加不进去」盖成假成功
   *    （字典缺档时尤其如此）。
   *  - `value(id, [])`（`removeAll(id)`）：**不触发**任何钩子（`:1379` 的 `!isOn`）。
   *
   * 因此：把同步回调挂在 `on` 上 → `<select>` 收到**上一个** id（红）；挂在 `endOn` 上 → 收到改后的 id。
   */
  type FsValue = { value?: unknown }
  type FsCb = (id: string, vals: FsValue[]) => void
  const options = new Map<string, { name: string; value: unknown }[]>()
  const values = new Map<string, FsValue[]>()
  const onCbs = new Map<string, FsCb[]>()
  const endCbs = new Map<string, FsCb[]>()
  const valsOf = (id: string): FsValue[] => values.get(id) ?? []
  const copy = (v: FsValue[]): FsValue[] => v.map((x) => ({ ...x }))
  const fire = (m: Map<string, FsCb[]>, id: string, v: FsValue[]): void => {
    for (const cb of m.get(id) ?? []) cb(id, copy(v))
  }
  const formSelects = {
    data: (id: string, _type: string, opts: { arr?: { name: string; value: unknown }[] }): void => {
      options.set(id, opts.arr ?? [])
    },
    value: (id: string, type?: unknown, isAppend?: boolean): unknown => {
      if (type === undefined || typeof type === 'string') return copy(valsOf(id))
      const arr = type as unknown[]
      // ⚠️ **只在 `options` 里有这个 id 时才收录**（对齐真插件 `:1562` 的 `dd[lay-value]` 命中；
      // `:1566` 的 `valToName` 对本地字典返回空串、也进不去）。无条件写入会盖成假成功。
      const known = new Set((options.get(id) ?? []).map((o) => o.value))
      const present = arr.filter((v) => known.has(v))
      if (isAppend === true) {
        // radio：先清后 `push`（`addLabel` :1199，radio 清空在 `:1209-1213`，`vals.push` 在 `:1182`）
        values.set(
          id,
          present.map((v) => ({ value: v })),
        )
      } else if (isAppend === false) {
        values.set(
          id,
          valsOf(id).filter((x) => !present.includes(x.value)),
        )
      } else {
        values.set(id, [])
      }
      return undefined
    },
    on: (id: string, cb: FsCb, isEnd?: boolean): void => {
      const m = isEnd === true ? endCbs : onCbs
      m.set(id, [...(m.get(id) ?? []), cb])
    },
  }
  return {
    msg: vi.fn(),
    /** `layer.open` 的替身：返回一个递增的实例号（页面据此 `close`）。 */
    open: vi.fn(() => 3),
    handlers: new Map<string, (...args: unknown[]) => unknown>(),
    fs: formSelects,
    /** 测试驱动：模拟真人点选（改前 `on` → 改后 `endOn`）。 */
    fsPick(id: string, value: unknown): void {
      fire(onCbs, id, valsOf(id))
      values.set(id, [{ value }])
      fire(endCbs, id, valsOf(id))
    },
    /** 测试驱动：模拟 radio 的「清空」快捷按钮。 */
    fsClear(id: string): void {
      fire(onCbs, id, valsOf(id))
      values.set(id, [])
      fire(endCbs, id, [])
    },
    fsValues(id: string): unknown[] {
      return valsOf(id).map((v) => v.value)
    },
    /** 每个用例一次干净页面（真插件状态也活在实例里，跨用例会串）。 */
    fsReset(): void {
      options.clear()
      values.clear()
      onCbs.clear()
      endCbs.clear()
    },
  }
})

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  const fake = {
    form: {
      render: vi.fn(),
      on: (evt: string, cb: (...args: unknown[]) => unknown) => LAYUI.handlers.set(evt, cb),
    },
    layer: { msg: LAYUI.msg, open: LAYUI.open, close: vi.fn(), prompt: vi.fn(), tips: vi.fn() },
    // 快速指派（D-0179）：页面读 `layui.device()` 定弹层 `area`，并用 layui 自带的 jQuery
    // 把 `.kszpDiv` 交给 `layer.open` 当 `content`（传裸元素会让 layui 抛 TypeError）。
    device: () => ({ android: false, ios: false }),
    $: (selector: string) => ({ selector }),
    // 证据件里已无 laytpl 标签 ⇒ 「原样克隆器」与真实 laytpl 同义（理由见文件头那条注释）。
    laytpl: (tpl: string) => ({ render: () => tpl }),
    util: {},
    element: { init: vi.fn() },
    formSelects: LAYUI.fs,
  }
  return { ...actual, bootstrapIsland: (async () => fake) as unknown as typeof actual.bootstrapIsland }
})

const HERE = path.dirname(fileURLToPath(import.meta.url))

const EVIDENCE = readFileSync(
  path.resolve(HERE, '../../../../public/replica/order-addorder.html'),
  'utf8',
)

/**
 * 本页模块的源码（`pages/order-addorder.ts`）原文。
 *
 * 用途只有一个：把「保存成功弹窗**整块**被移除」变成一条**结构**断言。光断言
 * `SAVE_SUCCESS_MESSAGE === '录单成功'` 证不了「不再弹窗」——有人把弹窗加回来、同时保留
 * 这句文案，那条用例照样绿。所以这里直接钉「源码里不许再有那个弹层的痕迹」。
 */
const SOURCE = readFileSync(path.resolve(HERE, '../order-addorder.ts'), 'utf8')

/**
 * `/api/auth/me` 桩的**节点集**：保存门**按模式取**
 * （新建态单点 `order.add_edit`／编辑态 any(`order.add_edit`, `order.submit_edit`)）、
 * 粘贴解析门取 `order.add_edit`、编辑态回填门取 any(`order.view_detail`, `order.view_content`)。
 *
 * 四个挂载样板一律按「这些节点**都有**」的**正常用户**给全（两模式的保存门都放行）——这正是
 * 验收判据「新建态行为与外观逐字不变」那一档；门本身的分档（缺节点 / 身份取不到 / 管理员短路 /
 * 两模式的区别）在文件末尾「权限门」那一组用例里单独驱动。
 *
 * ⭐ `order.allow_change_sales_agent`（「接单销售客服」那一格的编辑门）也在这份里（而不是
 * 只在那一组新用例里单列）：正常用户的**外观不许变**（编辑态那一格照旧可改）。
 * ⚠️ 它是**放开型**权限点：**缺**它才是"没权限"那一档，用例 D/C 分开驱动。
 */
const ME_PERMISSIONS_ALL = [
  'order.send_to_grab_hall',
  'order.add_edit',
  'order.submit_edit',
  'order.view_detail',
  'order.view_content',
  'order.allow_change_sales_agent',
  'order.edit_payout_price',
]

/** 证据件解析成 DOM（**不是** mount：layui 在 jsdom 里加载不了，spec 只测纯函数与标记）。 */
function evidenceDom(): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML = EVIDENCE
  return host
}

/**
 * 「把 `#template` 的内容当作已渲染结果注入」——对标在运行期由 laytpl 完成这一步，
 * 而证据件里已经没有 laytpl 标签了（见 spec 的第一条用例），所以直接注入 `innerHTML`
 * 得到的就是**同一棵 DOM**。这是本文件唯一的注入手法，替代不可用的 layui。
 */
function renderedForm(): HTMLElement {
  const raw = evidenceDom()
  const tpl = raw.querySelector('#template')
  const host = document.createElement('div')
  host.innerHTML = tpl?.innerHTML ?? ''
  return host
}

/** 往表单里塞一组「人填进来的」值，走真实 DOM 路径（随后由 `readFormState` 读回）。 */
function fill(host: HTMLElement, values: Record<string, string>): void {
  for (const [selector, value] of Object.entries(values)) {
    const el = host.querySelector<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(selector)
    if (!el) throw new Error(`夹具选择器不存在：${selector}`)
    el.value = value
  }
}

function check(host: HTMLElement, selector: string): void {
  const el = host.querySelector<HTMLInputElement>(selector)
  if (!el) throw new Error(`夹具选择器不存在：${selector}`)
  el.checked = true
}

describe('录入订单 layui 页：证据件与「必须模板渲染」', () => {
  it('证据件是 laytpl 模板：有 #template、#root 与页内 <style>，且已无未解析的 {{ }}', () => {
    expect(EVIDENCE).toContain('<template id="template">')
    expect(EVIDENCE).toContain('<div id="root">')
    // 页内 <style> 的一个特征值（`.layui-form-pane .layui-form-label{width:110px`）——
    // 它只属于这一页，是「值从对标取」的锚点。
    expect(EVIDENCE).toContain('.layui-form-pane .layui-form-label')
    expect(EVIDENCE).toContain('width: 110px')
    // `{{` 只允许出现在 laytpl 已被解掉之后：证据件里一个都不该剩（否则页面会出现字面量）。
    expect(EVIDENCE.includes('{{')).toBe(false)
  })

  it('模板未被渲染时，表单在 DOM 里**查不到**（这就是「只照抄静态 HTML = 空白页」的机制）', () => {
    const raw = evidenceDom()
    // `<template>` 的内容进的是 DocumentFragment，`querySelector` 穿不进去：
    expect(raw.querySelector('#gameName')).toBeNull()
    expect(raw.querySelectorAll('[name]').length).toBe(0)
    // 渲染（= 把 template 的内容注入）之后才查得到
    expect(renderedForm().querySelector('#gameName')).not.toBeNull()
  })

  it('renderTemplate 真的把模板克隆进 #root（缺 #template 时返回 false，不静默留白）', () => {
    const calls: { tplLen: number; opts: unknown }[] = []
    // 证据件里没有 laytpl 标签，所以 laytpl 在这里等价于「把模板串原样返回的克隆器」；
    // 用桩是为了证明**代码确实调了 laytpl**（而不是把 `<template>` 直接塞进 DOM 了事）。
    const factory = ((tpl: string, opts: unknown) => {
      calls.push({ tplLen: tpl.length, opts })
      return { render: () => `<div id="rendered">${tpl.length}</div>` }
    }) as unknown as LayuiModule

    const host = evidenceDom()
    expect(renderTemplate(host, factory)).toBe(true)
    expect(host.querySelector('#rendered')).not.toBeNull()
    expect(calls).toHaveLength(1)
    expect(calls[0]?.opts).toEqual({ tagStyle: 'modern' })
    expect(calls[0]?.tplLen).toBeGreaterThan(10_000)
    // `#root` 的旧内容（那个 <template>）被替换掉
    expect(host.querySelector('#template')).toBeNull()

    const empty = document.createElement('div')
    expect(renderTemplate(empty, factory)).toBe(false)
    expect(calls).toHaveLength(1)
  })

  it('`#project` 的 `maxlength` 是 900（有意偏离证据件的 220；D-0140 到 300、D-0218 再到 900）', () => {
    // 判据打在**渲染出来的 DOM** 上（证据件经 `#template` 注入后就是页面真正拿到的控件）——
    // 只断言 EVIDENCE 字符串会漏掉「页面根本没渲染这个属性」。
    // ⚠️ 900 是**字面量**：⛔ 不要从源码/模型 import（自指断言在被断言的常量被改坏时不会红）。
    const input = renderedForm().querySelector<HTMLInputElement>('#project')
    expect(input).not.toBeNull()
    expect(input!.getAttribute('maxlength')).toBe('900')
  })

  it('PII：证据件里没有手机号 / 身份证号；租户的 14 个店铺来源 option 已清空', () => {
    expect(/1[3-9][0-9]{9}/.test(EVIDENCE)).toBe(false)
    expect(/[0-9]{17}[0-9Xx]/.test(EVIDENCE)).toBe(false)
    // 判据是**模式**不是具体店名（真名不入库）：证据件里不得有「店铺名形态」的 option——
    // 属性值与文本都算。`title="淘宝店"` 那类平台名标签、以及 `当你淘宝店…` 那类文案
    // 都不是 option，故不受影响（裸后缀判据会误报它们，见 config/__tests__/pii-guard.spec.ts）。
    expect(EVIDENCE).not.toMatch(/<option\b[^>]*(?:淘宝店|天猫店|旗舰店|专营店|专卖店)/)
    expect(EVIDENCE).not.toMatch(/<option\b[^>]*>[^<]*(?:淘宝店|天猫店|旗舰店|专营店|专卖店)/)
    const dom = renderedForm()
    expect(dom.querySelectorAll('#orderSoures2 option').length).toBe(0)
    // 静态配置枚举（游戏名）是可以留的
    expect(EVIDENCE).toContain('崩坏：星穹铁道')
  })

  it('证据件里 `#orderSoures` 是 xm-select **单选**（I2a 证据件断言 + M2 单选守卫）', () => {
    const dom = renderedForm()
    const el = dom.querySelector<HTMLSelectElement>('#orderSoures')
    expect(el).not.toBeNull()
    // I2(a)：插件只对带 `xm-select="<id>"` 的 select 渲染，单选语义靠 `xm-select-radio`
    // （`public/vendor/formselects-v4.js` 从属性读 `radio`）——没有它，插件会渲染成多选。
    expect(el!.getAttribute('xm-select')).toBe('orderSoures')
    // M2：这条守卫拦「有人把来源改成多选」。四处判据：radio 属性在、`multiple` 不在、
    // 没有 `xm-select-max`（多选的容量参数）、`name` 仍是后端的单值字段。
    expect(el!.hasAttribute('xm-select-radio')).toBe(true)
    expect(el!.hasAttribute('multiple')).toBe(false)
    expect(el!.hasAttribute('xm-select-max')).toBe(false)
    expect(el!.getAttribute('name')).toBe('orderSoures')
    // 同一份属性是**运行期**插件读的那一份（不是只写在证据件里的死标记）：`mount` 用
    // `select[xm-select]` 渲染（`vendor.ts::initFormSelects`），少一个属性就渲染不出来。
    expect(dom.querySelector('select[xm-select="orderSoures"]')).toBe(el)
  })

  it('showpw() 的两条运行期文案：证据件里是静态值，运行期必须改成对标 L814/L822 的值', () => {
    const dom = renderedForm()
    // 静态（服务端渲染）值
    expect(dom.querySelector('.projectstr')?.textContent).toBe('代练/陪玩项目')
    expect(dom.querySelector('.throwFeestr')?.textContent).toBe('发给打手价格')
    // 运行期值——parity 比的是这一个
    expect(labelOverrides('0')).toEqual({
      '.projectstr': '代练项目/标题',
      '.throwFeestr': '给打手发单价',
      '.timetag': '时间要求',
      '.bossNameLabel': '角色名',
    })
    // 陪玩单那一支
    expect(labelOverrides('1')).toEqual({
      '.projectstr': '陪玩内容/标题',
      '.throwFeestr': '发陪玩价格',
      '.timetag': '陪玩时长',
      '.bossNameLabel': '老板名称',
    })
  })
})

describe('录入订单 layui 页：控件映射的穷尽性', () => {
  it('DOM 里每个 [name] 都落在 FIELD_MAP 或 IGNORED_FIELDS 里（漏一个这条就红）', () => {
    const dom = renderedForm()
    const names = new Set(
      Array.from(dom.querySelectorAll<HTMLElement>('[name]')).map(
        (el) => el.getAttribute('name') ?? '',
      ),
    )
    expect(names.size).toBe(68) // 去重后 68（实测）
    const unmapped = [...names].filter(
      (n) => !(n in FIELD_MAP) && !(n in IGNORED_FIELDS),
    )
    expect(unmapped).toEqual([])
  })

  it('FIELD_MAP 的每个键在证据件里都存在，且每个值都是 OrderForm 的真键', () => {
    const dom = renderedForm()
    const formKeys = new Set(Object.keys(emptyAddOrderForm()))
    for (const [name, field] of Object.entries(FIELD_MAP)) {
      expect(dom.querySelector(`[name="${name}"]`)).not.toBeNull()
      expect(formKeys.has(field)).toBe(true)
    }
    // IGNORED_FIELDS 不许留「表里有、DOM 里没有」的陈旧键——**除了**两个运行期才注入的：
    // `projectType`（项目分类单选）与 `dltype`（常用项目复选）由 `fetchCategories` /
    // `fetchProjects` 的结果运行期拼出来。
    const RUNTIME_INJECTED = ['projectType', 'dltype']
    const staticKeys = Object.keys(IGNORED_FIELDS).filter((n) => !RUNTIME_INJECTED.includes(n))
    expect(staticKeys.filter((n) => dom.querySelector(`[name="${n}"]`) === null)).toEqual([])
    // 这两个**只能**是运行期注入的（证据件里没有它们，见上面 RUNTIME_INJECTED 的说明）
    expect(
      RUNTIME_INJECTED.filter((n) => dom.querySelector(`[name="${n}"]`) !== null),
    ).toEqual([])
  })

  it('5 个金额控件的选择器都在证据件里，#price 带着对标自带的 lay-verify="number"', () => {
    const dom = renderedForm()
    for (const sel of MONEY_DOM_SELECTORS) {
      expect(dom.querySelector(sel)).not.toBeNull()
    }
    expect(dom.querySelector('#price')?.getAttribute('lay-verify')).toBe('number')
  })

  it('「尚未开放」容器不吞掉任何功能控件（#gameName/#gameArea 与 #gameServer 是兄弟格）', () => {
    const dom = renderedForm()
    const hosts = resolveNotOpenHosts(dom)
    // 条数写死：少一条说明选择器过期或清单被悄悄删了。
    // 必须和 `NOT_OPEN_CONTROLS` 同一次改，否则静默漏接口。
    expect(NOT_OPEN_CONTROLS.length).toBe(15)
    expect(hosts.length).toBe(15)

    const FUNCTIONAL = [
      '#gameName',
      '#gameArea',
      '#project',
      '#dlcontent',
      '#price',
      '.dllistdiv',
      '.orderTypeDiv',
      'input[name=jiaji]',
      'input[name=orderType]',
      '[lay-filter=savebtn]',
      // `#orderSoures` 已接真数据（选项来自 `fetchOrderSources()`），它是功能控件——
      // 漏登记就会有人把它圈进「尚未开放」容器而静默并吞掉。
      '#orderSoures',
    ].join(', ')
    const swallowed = hosts.filter((h) => h.host.querySelector(FUNCTIONAL) !== null)
    expect(swallowed.map((h) => h.label)).toEqual([])

    // 登录密码 / 暗号 / 会员号 / 两个保证金 是文本输入：非空失焦要提示，避免静默吞掉用户输入。
    // ⚠️ **「给打手发单价」已从这份清单里摘掉**（D-0179）：它不再是「尚未开放」的控件，
    // 而是真能提交的 `hall_price`（见页面 `FIELD_MAP` 与既有的载荷用例）。
    expect(hosts.filter((h) => h.onBlur).map((h) => h.label)).toEqual([
      '登录密码',
      '暗号/口令',
      '会员号/会员名',
      // 两个保证金也是文本输入（P4-1 删字段后按「尚未开放」处理，见 NOT_OPEN_CONTROLS）
      '安全保证金',
      '效率保证金',
    ])
    // 每个 label 都是给人看的中文（不是选择器或空串）
    for (const h of hosts) expect(h.label.length).toBeGreaterThan(1)
  })
})

describe('录入订单 layui 页：DOM ⇄ 模型 ⇄ 载荷', () => {
  it('readFormState + formToPayload：只带非空字段，四项 FK 与负金额原样进载荷', () => {
    const dom = renderedForm()
    // 游戏 / 区服两个下拉的选项在运行期才由我们灌进去（`fetchGames` / `fetchServers`），
    // 这里先把选项补上，让 `.value = '2'` 这类赋值真的落得住。
    dom.querySelector('#gameName')!.innerHTML = '<option value="2">绝区零</option>'
    dom.querySelector('#gameArea')!.innerHTML = '<option value="7">华北一区</option>'
    fill(dom, {
      '[name="project"]': '深渊满星',
      '[name="gameAcc"]': 'acc-001',
      '[name="souresOrderID"]': 'SRC-1',
      '[name="price"]': '-199.50',
      '[name="qq"]': 'qq-test',
      '#gameName': '2',
      '#gameArea': '7',
      '#day': '2',
      '#hours': '1.5',
    })
    check(dom, 'input[name="jiaji"][value="Y"]')
    check(dom, 'input[name="orderType"][value="1"]')
    // 项目分类与常用项目由运行期注入，这里手写两条夹具（分类单选 + 恰好一项复选）
    dom.querySelector('.orderTypeDiv')!.innerHTML =
      '<input type="radio" name="projectType" value="9" checked>'
    dom.querySelector('.dllistdiv')!.innerHTML =
      `<input type="checkbox" name="dltype" value="11" data-name="深渊满星" checked>` +
      `<input type="checkbox" name="dltype" value="12" data-name="刷本">`

    const form = readFormState(dom)
    expect(form).toMatchObject({
      game: 2,
      server: 7,
      category: 9,
      project: 11,
      project_text: '深渊满星',
      game_account: 'acc-001',
      source_order_no: 'SRC-1',
      time_requirement: '2天1.5小时',
      is_urgent: true,
      order_type: 'COMPANION',
    })

    const payload = formToPayload(form, 'tester')
    expect(payload).toEqual({
      actor: 'tester',
      game: 2,
      server: 7,
      category: 9,
      project: 11,
      project_text: '深渊满星',
      game_account: 'acc-001',
      source_order_no: 'SRC-1',
      total_price: '-199.50',
      customer_qq: 'qq-test',
      time_requirement: '2天1.5小时',
      is_urgent: true,
      order_type: 'COMPANION',
      // ⚠️ 这 4 个金额框在证据件上就带 `value="0"`（服务端渲染的默认值），
      // 所以它们不是「空字段」而是用户看得见的 `0`。「空值不发」只适用于**没有默认值**的框
      // （`#dlcontent` 粘贴框等）。
      received_price: '0',
      platform_price: '0',
      hall_price: '0',
      fee: '0',
    })
    // 空字段一个都不发
    for (const key of ['customer_phone', 'tag', 'remark', 'content']) {
      expect(Object.keys(payload)).not.toContain(key)
    }
  })

  it('dltype 勾中**多项**时不猜 FK（project 留空，以自由文本为准）', () => {
    const dom = renderedForm()
    dom.querySelector('.dllistdiv')!.innerHTML =
      `<input type="checkbox" name="dltype" value="11" checked>` +
      `<input type="checkbox" name="dltype" value="12" checked>`
    expect(readFormState(dom).project).toBeNull()
  })

  it('writeFormState → readFormState 往返：映射字段、radio 与下拉都不漂移', () => {
    const dom = renderedForm()
    // 与生产同序：`#gameName` 的选项来自 `fetchGames()`（这里手工喂一个 id=5 的游戏）
    dom.querySelector('#gameName')!.innerHTML = '<option value="5">绝区零</option>'
    const form = {
      ...emptyAddOrderForm(),
      game: 5,
      game_account: 'acc-9',
      content: '客户原话',
      project_text: '等级 1-60',
      total_price: '-0.01',
      customer_ww: 'ww-1',
      time_requirement: '3天',
      is_urgent: true,
      order_type: 'COMPANION',
    }
    writeFormState(dom, form)
    const back = readFormState(dom)
    expect(back).toMatchObject({
      game: 5,
      game_account: 'acc-9',
      content: '客户原话',
      project_text: '等级 1-60',
      total_price: '-0.01',
      customer_ww: 'ww-1',
      is_urgent: true,
      order_type: 'COMPANION',
    })
    // 清空后（「继续录入」的路径）只剩游戏
    const cleared = { ...emptyAddOrderForm(), game: 5 }
    writeFormState(dom, cleared)
    const after = readFormState(dom)
    expect(after.game).toBe(5)
    expect(after.project_text).toBe('')
    expect(after.is_urgent).toBe(false)
  })

  /**
   * 「模型与界面必须一起改」：解析结果若只改模型、不推回 DOM，提交时 `readFormState`
   * 从 DOM 重读 ⇒ 整批解析结果**静默丢失**。这条用例把它钉住。
   */
  it('applyPasteToDom：解析命中的项必须同时落到 DOM（模型改了界面没改 = 提交时静默丢）', () => {
    const dom = renderedForm()
    dom.querySelector('#gameName')!.innerHTML = '<option value="2">绝区零</option>'
    const form = emptyAddOrderForm()
    const { filled, unresolved } = applyPasteToDom(
      dom,
      form,
      {
        game_account: 'acc-77',
        game_role_name: 'E2E角色',
        project_text: '等级 1-60',
        total_price: '-12.34',
        received_price: '-1.50',
        game_name: '绝区零',
        server_name: '华北一区',
      },
      { games: [{ id: 2, name: '绝区零' }], servers: [], categories: [], projects: [], orderSources: [] },
    )
    // DOM 真的改了
    expect((dom.querySelector('#gameAcc') as HTMLInputElement).value).toBe('acc-77')
    expect((dom.querySelector('#gameRole') as HTMLInputElement).value).toBe('E2E角色')
    expect((dom.querySelector('#project') as HTMLInputElement).value).toBe('等级 1-60')
    expect((dom.querySelector('#price') as HTMLInputElement).value).toBe('-12.34')
    expect((dom.querySelector('#priceReceipts') as HTMLInputElement).value).toBe('-1.50')
    expect((dom.querySelector('#gameName') as HTMLSelectElement).value).toBe('2')
    // 提交时从 DOM 重新读到的就是这批值（不再丢）
    expect(readFormState(dom)).toMatchObject({
      game: 2,
      game_account: 'acc-77',
      project_text: '等级 1-60',
      total_price: '-12.34',
      received_price: '-1.50',
    })
    expect(filled).toContain('game_account')
    // 区服名不在字典里 → 不硬填，且如实报出来
    expect(form.server).toBeNull()
    expect(unresolved).toEqual(['区服「华北一区」'])
  })

  it('applyPasteToDom：字典里有的区服名解析成 id（进 FK），对不上的只报不填', () => {
    const dom = renderedForm()
    const form = emptyAddOrderForm()
    const { filled, unresolved } = applyPasteToDom(
      dom,
      form,
      { server_name: '华北一区' },
      {
        games: [],
        servers: [{ id: 7, name: '华北一区' }],
        categories: [],
        projects: [],
        orderSources: [],
      },
    )
    expect(form.server).toBe(7)
    expect(filled).toContain('server')
    expect(unresolved).toEqual([])
  })

  it('粘贴框内容进 `content`（对标只有这一个框，它就是客户原始需求）', () => {
    const dom = renderedForm()
    fill(dom, { '#dlcontent': '客户发来的整段文字' })
    expect(readFormState(dom).content).toBe('客户发来的整段文字')
  })
})

describe('录入订单 layui 页：必填与金额校验', () => {
  it('requiredError：游戏必填；project_text 按 Python strip() 判空（全角空格也算空）', () => {
    expect(requiredError({ ...emptyAddOrderForm(), game: null })).toBe('请选择游戏')
    expect(requiredError({ ...emptyAddOrderForm(), game: 2 })).toBe('代练/陪玩项目不可为空')
    // brief 的空白集：`\u3000`（全角空格）与 `\u00a0` 都在 Python 的 `isspace()` 里
    expect(requiredError({ ...emptyAddOrderForm(), game: 2, project_text: '\u3000' })).toBe(
      '代练/陪玩项目不可为空',
    )
    // ⚠️ `emptyAddOrderForm()` 的 `total_price` 是 `''`，而这条仍然通过——这是**对的**：
    // 「订单总价必填」是**另一条只对新建态生效**的闸（`totalPriceRequiredError`，见下面那条用例），
    // 刻意不塞进 `requiredError`（后者是新建/编辑两态共用的，塞进去会误拦编辑态）。
    expect(requiredError({ ...emptyAddOrderForm(), game: 2, project_text: ' 1-60 ' })).toBe('')
  })

  /**
   * 订单总价必填的**独立**判据（业务方裁定，见 `docs/DECISIONS.md` D-0082）。
   *
   * 判别力：判据若写成 JS 真值（`if (!form.total_price)`）⇒ `'0'` / `'0.00'` 会被当成「没填」，
   * 下面那三条立刻红（E-0021 的 JS 侧同型：金额 0 与 '' 必须分开判）。空白集必须与
   * `project_text` 同源（`isBlankLikePython`，Python `strip()` 语义），**不是** JS `trim()`。
   */
  it('totalPriceRequiredError：空 / 纯空白（含全角空格）报错；`0`、`0.00`、负数都合法', () => {
    const base = { ...emptyAddOrderForm(), game: 2, project_text: '1-60' }
    expect(totalPriceRequiredError({ ...base, total_price: '' })).toBe('订单总价不可为空')
    expect(totalPriceRequiredError({ ...base, total_price: '\u3000' })).toBe('订单总价不可为空')
    expect(totalPriceRequiredError({ ...base, total_price: ' \u00a0 ' })).toBe('订单总价不可为空')
    // 金额 0 与负数都是合法值（D-0015）——「填了 0」不等于「没填」
    expect(totalPriceRequiredError({ ...base, total_price: '0' })).toBe('')
    expect(totalPriceRequiredError({ ...base, total_price: '0.00' })).toBe('')
    expect(totalPriceRequiredError({ ...base, total_price: '-5' })).toBe('')
    // 它与 `requiredError` 是**两个**判据（那条是两态共用的）：同一份空总价的表单在它这里通过
    expect(requiredError({ ...base, total_price: '' })).toBe('')
  })

  it('firstMoneyError：7 个金额字段逐个过 validateMoneyInput，负值合法', () => {
    const form = emptyAddOrderForm()
    expect(firstMoneyError(form)).toBe('')
    expect(firstMoneyError({ ...form, total_price: '-199.50' })).toBe('')
    expect(firstMoneyError({ ...form, total_price: '0' })).toBe('')
    // 后端拒的输入必须在前端先被拦下（消息非空即可，措辞由 config/money 负责）
    expect(firstMoneyError({ ...form, total_price: 'abc' })).not.toBe('')
    expect(firstMoneyError({ ...form, total_price: '1,000' })).not.toBe('')
    // 顺序取 ORDER_MONEY_FIELDS：第一个出错的就是 total_price
    expect(firstMoneyError({ ...form, total_price: 'x', received_price: 'y' })).toBe(
      firstMoneyError({ ...form, total_price: 'x' }),
    )
  })

  /**
   * `#price` 上除了我们的校验，还有**对标自带的** `lay-verify="number"`（layui 2.13.9 的规则是
   * `if(e && isNaN(e)) return …`，见 `public/layui/layui.js` 的 `this.config={verify:{…}}`）。
   * 两者不是同一个谓词，只可能在 `#price` 上叠成「先被 layui 拦」。真正会**缩掉能力**的方向是
   * 「我们接受、layui 先拒」——下面用 23 个形状对拍，把那一个方向的实测结果钉住。
   *
   * 证明力的边界（照 E-0011 的口径）：这只能说明**这 23 个形状里**危险方向为空，
   * 不能说明它永远为空；语料按分支分层覆盖才能谈完备性。
   */
  it('与 #price 的 lay-verify="number"（layui isNaN 规则）对拍：危险方向在这 23 个形状里为空', () => {
    const layuiRejects = (v: string): boolean => v !== '' && Number.isNaN(Number(v))
    const oursAccepts = (v: string): boolean => firstMoneyError({ ...emptyAddOrderForm(), total_price: v }) === ''
    const corpus = [
      '12', '-199.50', '0', '-0.01', '00', ' 12 ', '\u00a012',
      '   ', '', 'abc', '1,000', '1_000', '1.2.3', '-', '１２', '$12', 'NaN', '1.234',
      '99999999999.99', '.5', '1.', '+12', '1e3',
    ]
    expect(corpus.length).toBe(23)
    // 危险方向：我们接受而 layui 会先拒 → 会静默缩掉后端允许的输入
    expect(corpus.filter((v) => oursAccepts(v) && layuiRejects(v))).toEqual([])
    // 两边都拒的（同一批里最典型的三条）
    for (const v of ['abc', '1,000', '1_000']) {
      expect(oursAccepts(v)).toBe(false)
      expect(layuiRejects(v)).toBe(true)
    }
    // 只有我方拦下的（layui 的 isNaN 放行）：由提交处理器兜底
    for (const v of ['0x10', '1e3', 'Infinity']) {
      expect(oursAccepts(v)).toBe(false)
      expect(layuiRejects(v)).toBe(false)
    }
  })
})

describe('录入订单 layui 页：保存成功后的提示与复位（业务方 2026-09-20 口径）', () => {
  it('成功提示就是「录单成功」，且弹层整块被移除（不是「只是不触发它」）', () => {
    // 业务方给定文案，逐字钉住——**不要**改成别的措辞
    expect(SAVE_SUCCESS_MESSAGE).toBe('录单成功')
    // 弹层的内容构造函数与它独有的那颗按钮 `复制单号` 都不该再存在
    // （`复制单号` 只在这个弹层里出现过，是干净的判据；`去列表`/`继续录入` 被注释提到过，
    //  不能当判据——第一版就误用了它们，实测当场红）。
    expect(SOURCE).not.toContain('orderSavedHtml')
    expect(SOURCE).not.toContain('ttSavedOrderNo')
    expect(SOURCE).not.toContain('复制单号')
    // 页内**任何位置**都不该再调 `layer.open`：整页只剩 `layer.msg`（提示）与 `layer.tips`
    // （金额框的可见提示，见文件头第六节）。这条同时挡住「把弹窗挪到别的分支再弹」。
    expect(SOURCE).not.toContain('layer.open')
    // ⚠️（D-0179「快速指派」）快速指派**合法地**有**一处**层（候选名单，`content` 就是
    // 证据件里的 `.kszpDiv`）⇒ 判据是**枚举：恰好一处，且必须是它**。
    
    //    「有人用别的写法再开一个弹层」由它**上面**那句 `not.toContain('layer.open')` 覆盖
    //    （那是**直接形式**：`layer.open(...)` 一旦有人写，那句就红）。两道合起来的覆盖面是
    //    「直接形式一处都不许有 + 间接形式（`callLayer('open'`）恰好一处且必须是快速指派那个」；
    //    ⛔ 别为了让它们"看起来更全"而放宽任何一条（原样保留这两条断言）。
    expect(SOURCE.split("callLayer('open'").length - 1).toBe(1)
    expect(SOURCE).toContain('title: KSZP_LAYER_TITLE')
    // ⚠️ 光有上面这些还不够：直接往成功路径里塞别的弹窗调用、同时保留上述任何一条，它们照样绿。
    // 所以再钉一条**结构**断言：`onSave` 的 try 块里只允许发请求 + 置模型，不许有弹窗调用
    // （源码片段是这一层能拿到的最强判据，行为面由 E2E 兜底：断言没有 `.layui-layer` 弹层）。
    // ⚠️ **切片右端必须是「最后一个」`} catch`，不是第一个**：编辑态那一支里有一个**内层**
    //    `try { … } catch`（换归属失败只提示、不改别的东西）⇒ 从第一个 catch 切会把新建态
    //    那半漏出扫描面，这条判据会以「锚点找不到」的形式假红。
    const tryBody = SOURCE.slice(
      SOURCE.indexOf('    submitting = true\n    try {\n'),
      SOURCE.lastIndexOf('    } catch'),
    )
    expect(tryBody).toContain('await createOrder(payload)')
    expect(tryBody).not.toContain('layer')
    expect(tryBody).not.toContain('Dialog')
  })

  it('resetFormToInitial：复位到模板默认值（粘贴框/项目清空、日期回 1天0小时、单号与加急取消）', () => {
    const dom = renderedForm()
    // `#gameName` 的选项是运行期由 `fetchGames()` 灌进去的（证据件里那个 select 是空的），
    // 所以先补上选项，`form.reset()` 才有「默认项」可回。
    dom.querySelector('#gameName')!.innerHTML =
      '<option value="2">绝区零</option><option value="3">原神</option>'
    // 分类 / 常用项目也是运行期注入的（`fetchCategories` / `fetchProjects`）。
    // ⚠️ 夹具按**生产**那个形状来：运行期注入的新控件带 `checked` **属性**（HTML attribute），
    // 于是它的「出厂默认」就是选中——`form.reset()` 保留它是**对的**。第一版用例期望「取消」
    // 其实是错的（实测抓到），这里显式把 `defaultChecked` 打回 false，模拟「初始未勾选、
    // 用户点出来才勾上」：只有这个形状才谈得上「复位要把勾去掉」。
    dom.querySelector('.orderTypeDiv')!.innerHTML =
      '<input type="radio" name="projectType" value="9" checked>'
    dom.querySelector('.dllistdiv')!.innerHTML =
      '<input type="checkbox" name="dltype" value="11" data-name="深渊满星" checked>'
    for (const el of Array.from(dom.querySelectorAll<HTMLInputElement>('input[name="projectType"]'))) {
      el.removeAttribute('checked')
    }
    for (const el of Array.from(dom.querySelectorAll<HTMLInputElement>('input[name="dltype"]'))) {
      el.removeAttribute('checked')
    }
    expect(dom.querySelector<HTMLInputElement>('input[name="dltype"]')?.checked).toBe(false)
    fill(dom, {
      '[name="project"]': '深渊满星',
      '[name="gameAcc"]': 'acc-001',
      '[name="price"]': '128.50',
      '[name="dlcontent"]': '客户发来的整段文字',
      '#priceReceipts': '88',
      '#day': '3',
      '#hours': '2',
    })
    check(dom, 'input[name="jiaji"][value="Y"]')
    check(dom, 'input[name="orderType"][value="1"]')

    expect(resetFormToInitial(dom, 2)).toBe(true)

    // 逐项钉住复位（都是运行期真会被人填进去的字段）
    expect((dom.querySelector('#dlcontent') as HTMLTextAreaElement).value).toBe('')
    expect((dom.querySelector('#project') as HTMLInputElement).value).toBe('')
    expect((dom.querySelector('#gameAcc') as HTMLInputElement).value).toBe('')
    // 日期回模板默认值（`#day` 默认 1、`#hours` 默认 0）
    expect((dom.querySelector('#day') as HTMLSelectElement).value).toBe('1')
    expect((dom.querySelector('#hours') as HTMLSelectElement).value).toBe('0')
    // 金额回模板自带的 `value="0"`（不是长出来的空串）
    expect((dom.querySelector('#price') as HTMLInputElement).value).toBe('0')
    expect((dom.querySelector('#priceReceipts') as HTMLInputElement).value).toBe('0')
    // 默认游戏 = 传进来的首个游戏；区服（xm-select 的隐藏 select）清空
    expect((dom.querySelector('#gameName') as HTMLSelectElement).value).toBe('2')
    expect((dom.querySelector('#gameArea') as HTMLSelectElement).value).toBe('')
    // 运行期注入的分类勾选被取消（`form.reset()` 回到 `defaultChecked`＝false）；
    // 常用项目由 `onGameChange` / `onCategoryChange` 重建列表（fixture 里只钉住它的读取路径）
    expect(dom.querySelector('input[name="projectType"]:checked')).toBeNull()
    expect(dom.querySelector('input[name="dltype"]:checked')).toBeNull()

    // 复位后的模型真的等价于「刚打开」：必填回流、加急与订单类型回默认、时间要求回 1 天
    expect(readFormState(dom)).toMatchObject({
      game: 2,
      server: null,
      category: null,
      project: null,
      project_text: '',
      game_account: '',
      content: '',
      total_price: '0',
      time_requirement: '1天',
      is_urgent: false,
      order_type: 'BOOST',
    })
    expect(requiredError(readFormState(dom))).toBe('代练/陪玩项目不可为空')
  })

  it('resetFormToInitial：不传游戏 id 时，`#gameName` 交给浏览器回它的第一个 option（`games[0]`）', () => {
    const dom = renderedForm()
    dom.querySelector('#gameName')!.innerHTML =
      '<option value="2">绝区零</option><option value="3">原神</option>'
    dom.querySelector<HTMLSelectElement>('#gameName')!.value = '3'
    dom.querySelector<HTMLInputElement>('#gameAcc')!.value = 'acc-9'
    // 这条钉住 `resetAfterSave` 「游戏回首个」的推导：它靠的是 `reset()` 让 select 回
    // **第一个 option**，而不是我们手写 `games[0]`——因为首屏也是
    // `if (firstGame) await onGameChange(firstGame.id)`（同一个起点）。
    expect(resetFormToInitial(dom)).toBe(true)
    expect(dom.querySelector<HTMLSelectElement>('#gameName')!.value).toBe('2')
    expect(dom.querySelector<HTMLInputElement>('#gameAcc')!.value).toBe('')
  })

  it('resetFormToInitial：证据件里没有 form[lay-filter=form1] 时返回 false（不静默当成功）', () => {
    expect(resetFormToInitial(document.createElement('div'))).toBe(false)
  })

  it('day + hours 合成 time_requirement（为 0 的那段省略）', () => {
    expect(dayHoursToTimeRequirement('1', '0')).toBe('1天')
    expect(dayHoursToTimeRequirement('0', '0.5')).toBe('0.5小时')
    expect(dayHoursToTimeRequirement('2', '1.5')).toBe('2天1.5小时')
    expect(dayHoursToTimeRequirement('0', '0')).toBe('')
  })

  it('orderType radio → 后端 OrderType 枚举（0 代练单 / 1 陪玩单）', () => {
    expect(orderTypeValue('0')).toBe('BOOST')
    expect(orderTypeValue('1')).toBe('COMPANION')
    // 缺省（没有选中项时 checkedRadio 回空串）按代练单处理，与对标 radio 的 checked 默认一致
    expect(orderTypeValue('')).toBe('BOOST')
  })

  it('自由文本项目名不在映射表里，而由 toggleProjectText 的算法维护（此处只钉住常量）', () => {
    expect(PROJECT_TEXT_NAME).toBe('project')
  })
})

/**
 * 编辑态（`?order_no=` 非空）—— 管理订单页「编辑订单」弹窗内嵌本页时走的这一支。
 * 同形：iframe 里装本页 + `order_no`；本页负责回填与保存（真发 PATCH，见下面那一组）。
 */
describe('编辑态：URL 参数 / 详情回填 / time_requirement 逆解析', () => {
  /** 一份**只填本组用例关心的键**的详情响应；其余键按 `Order` 类型补齐为中性值。 */
  function order(overrides: Partial<Order>): Order {
    return {
      order_no: '20260921000000000001',
      status: 'UNASSIGNED',
      order_type: 'BOOST',
      game: 3,
      server: 7,
      category: 11,
      project: 22,
      project_text: '原神/深渊',
      // ⭐ `Order` 有这一列（`OrderSerializer` 是 `exclude = []`）。
      split_id: '',
      content: '客户原始需求',
      demand: '',
      time_requirement: '2天3.5小时',
      game_account: 'acc-1',
      game_sub_account: '',
      game_role_name: '角色A',
      current_info_1: '',
      current_info_2: '',
      customer_phone: '13800000000',
      customer_qq: '',
      customer_ww: '',
      total_price: '199.50',
      received_price: null,
      platform_price: null,
      hall_price: null,
      // ⚠️ **`refund_fee` 必填**（`api/orders.ts` 的 `Order.refund_fee: string`）。
      //    中性值取 `'0.00'`（= 未退款）。
      refund_fee: '0.00',
      fee: null,
      payment_method: '',
      // `Order.payment_status` 是必填键（后端三档码，default `UNSETTLED`）⇒ 夹具补齐。
      payment_status: 'UNSETTLED',
      source: '',
      order_source: null,
      source_order_no: 'TB-123',
      sales_agent: '',
      is_urgent: true,
      remark: '',
      tag: '',
      created_by: '',
      updated_by: '',
      created_at: '2026-09-21T00:00:00+08:00',
      updated_at: '2026-09-21T00:00:00+08:00',
      allowed_transitions: [],
      // 详情响应的只读布尔键（后端 `OrderSerializer` 恒回它）；夹具默认不可改「已完成」。
      completed_requires_review: false,
      ...overrides,
    }
  }

  it('editOrderNoFromSearch：认 `order_no`，没有就回空串（订单一律走这个入口判定编辑态）', () => {
    expect(editOrderNoFromSearch('?page=order-addorder&order_no=20260921000000000001')).toBe(
      '20260921000000000001',
    )
    expect(editOrderNoFromSearch('?page=order-addorder')).toBe('')
  })

  it('timeRequirementToDayHours：与 dayHoursToTimeRequirement 互逆；认不出的形状回空串', () => {
    expect(timeRequirementToDayHours('2天3.5小时')).toEqual({ day: '2', hours: '3.5' })
    expect(timeRequirementToDayHours(dayHoursToTimeRequirement('5', '1.5'))).toEqual({
      day: '5',
      hours: '1.5',
    })
    expect(timeRequirementToDayHours('长期')).toEqual({ day: '', hours: '' })
    // 空串（`formToPayload` 会丢掉它）也不能被解析成默认 1天
    expect(timeRequirementToDayHours('')).toEqual({ day: '', hours: '' })
  })

  it('orderToAddOrderForm：FK 三件套 + 自由文本 + 来源单号 + 金额，null 归空串', () => {
    const form = orderToAddOrderForm(order({}))
    expect(form.game).toBe(3)
    expect(form.server).toBe(7)
    expect(form.category).toBe(11)
    expect(form.project).toBe(22)
    expect(form.project_text).toBe('原神/深渊')
    expect(form.content).toBe('客户原始需求')
    expect(form.game_account).toBe('acc-1')
    expect(form.source_order_no).toBe('TB-123')
    expect(form.total_price).toBe('199.50')
    // 后端 6 个可空金额回 null → 空串（不是字符串 'null'、也不是 0）
    expect(form.received_price).toBe('')
    expect(form.fee).toBe('')
    expect(form.is_urgent).toBe(true)
    expect(form.time_requirement).toBe('2天3.5小时')
  })

  it('orderToAddOrderForm：order_type 只认 COMPANION，其余（含空）回 BOOST', () => {
    expect(orderToAddOrderForm(order({ order_type: 'COMPANION' })).order_type).toBe('COMPANION')
    expect(orderToAddOrderForm(order({ order_type: 'BOOST' })).order_type).toBe('BOOST')
    expect(orderToAddOrderForm(order({ order_type: '' })).order_type).toBe('BOOST')
  })

  // 来源 id 的回填。判别力——`orderToAddOrderForm` 不给 `form.order_source` 显式赋值就红
  // （`emptyAddOrderForm()` 里它是 `null`）；**不许**把它加进 `FIELD_MAP`：那张表会 `String()` 化
  // （`String(20)` = `'20'`），后端收到字符串 id 与「数字 id」的契约不符（spec §6.2 / S4）。
  it('orderToAddOrderForm：order_source 原样回填数字 id（不走 FIELD_MAP 的 String() 化）', () => {
    const form = orderToAddOrderForm(order({ order_source: 20 }))
    expect(form.order_source).toBe(20)
    expect(typeof form.order_source).toBe('number')
    expect(orderToAddOrderForm(order({ order_source: null })).order_source).toBeNull()
    // 没来源的订单（如来源字典项已删除、FK 被 SET_NULL）回填成 null，而不是 0 / 'null'
    expect(orderToAddOrderForm(order({ order_source: null })).order_source).not.toBe(0)
  })

  it('readOrderSource：优先读隐藏 select，为空回落到 formSelects，空串归 null', () => {
    const dom = renderedForm()
    const select = dom.querySelector<HTMLSelectElement>('#orderSoures')!
    select.innerHTML = '<option value="20">闲鱼A</option><option value="11">淘宝店</option>'
    // 隐藏 select 有值 ⇒ 以它为准（`mount` 把 xm-select 的选中值同步回它，与 `#gameArea` 同形）
    select.value = '20'
    expect(readOrderSource(dom, () => [{ value: 11 }])).toBe(20)
    // 隐藏 select 没值 ⇒ 回落到 formSelects（浏览器里 xm-select 的选中值只存在插件实例里）
    select.value = ''
    expect(readOrderSource(dom, () => [{ value: 11 }])).toBe(11)
    // 两处都没有 ⇒ null（**不是 0**：0 在 `order_source` 上不是合法 id）
    expect(readOrderSource(dom, () => [])).toBeNull()
    expect(readOrderSource(dom)).toBeNull()
  })

  // 回落的**形状**覆盖（`formSelects.value()` 的返回值随版本/调用方式变化，
  // 只测一种形状等于把「另一种形状会静默读到 null」留成漏洞）。
  it('readOrderSource 的回落覆盖三种形状：`[{value:11}]`、`[11]`、`11`', () => {
    const dom = renderedForm()
    const select = dom.querySelector<HTMLSelectElement>('#orderSoures')!
    select.innerHTML = '<option value="20">闲鱼A</option><option value="11">淘宝店</option>'
    // 有 option 但没选中时，浏览器/jsdom 默认选中第一条 ⇒ 显式清空才会走**回落**分支
    select.value = ''
    expect(select.value).toBe('')
    expect(readOrderSource(dom, () => [{ value: 11 }])).toBe(11)
    // 裸数字数组（真插件的 `value(id, 'val')` 形状）
    expect(readOrderSource(dom, () => [11])).toBe(11)
    // 完全裸的标量（自造替身/旧版本可能这么回）
    expect(readOrderSource(dom, () => 11)).toBe(11)
  })

  // E-0021：`0` 不是「空」——真值判会把 `0` 当成没选而静默丢掉这个值。
  it('readOrderSource 与载荷都**保留** `order_source: 0`（`0` 不是空，E-0021）', () => {
    const dom = renderedForm()
    const select = dom.querySelector<HTMLSelectElement>('#orderSoures')!
    select.innerHTML = '<option value="0">零号来源</option>'
    select.value = ''
    // 回落分支的三种形状都要把 0 留下来（`if (raw)` 那种真值判会在这一行红）
    expect(readOrderSource(dom, () => [{ value: 0 }])).toBe(0)
    expect(readOrderSource(dom, () => 0)).toBe(0)
    // 快速路径（同步过的隐藏 select）同理
    select.value = '0'
    expect(readOrderSource(dom, () => undefined)).toBe(0)
    // 载荷层：`formToPayload` 的空值过滤只认 `null` / `undefined` / 空白串，`0` 必须留下
    const payload = formToPayload({ ...emptyAddOrderForm(), order_source: 0 }, 'u')
    expect(payload).toHaveProperty('order_source', 0)
  })

  it('orderSourceOptions：只消费整页的 items（`total` 与 `items.length` 不同时也照 items 来）', () => {
    // 判别力：`fetchOrderSources()` 返回的是**整页**
    // （`{items,total,page,page_size}`），不是解包后的数组——写成 `page.map(...)`
    // 会因「page 不是数组」直接红。
    const page = {
      items: [
        {
          id: 20,
          name: '闲鱼A',
          remark: '',
          order_by: 0,
          shop_type: 20,
          group: null,
          group_name: '',
          created_at: '',
          updated_at: '',
        },
      ],
      total: 21,
      page: 1,
      page_size: 200,
    }
    expect(orderSourceOptions(page)).toEqual([{ name: '闲鱼A', value: 20 }])
    expect(orderSourceOptions({ ...page, items: [] })).toEqual([])
  })

  it('formToUpdatePayload：**删掉 7 个金额**，其余沿用 formToPayload 的口径（空值不发）', () => {
    const form = {
      ...emptyAddOrderForm(),
      game: 3,
      project_text: '原神/深渊',
      source_order_no: 'TB-1',
      total_price: '199.50',
      received_price: '1.00',
      customer_phone: '',
    }
    const payload = formToUpdatePayload(form, '客服小张')
    // 非金额字段照发（含操作人）
    expect(payload.source_order_no).toBe('TB-1')
    expect(payload.project_text).toBe('原神/深渊')
    expect(payload.game).toBe(3)
    expect(payload.actor).toBe('客服小张')
    // 7 个金额**一个都不在**载荷里（后端只读；改价走 `/price/`）
    for (const field of ORDER_MONEY_FIELDS) {
      expect(payload).not.toHaveProperty(field)
    }
    // 空串仍然不发——那是 `formToPayload` 的既有口径，本函数不重新发明
    expect(payload).not.toHaveProperty('customer_phone')
  })

  it('回填能真的落到 DOM：`writeFormState(orderToAddOrderForm(o))` 后 `readFormState` 读回同一批值', () => {
    const dom = renderedForm()
    // `#gameName` 的 option 由 `fetchGames()` 运行期灌进去，jsdom 里没有 → 先补一份，
    // 否则 `writeFormState` 里那句 `select.value = '3'` 会因没有对应 option 而静默落成 `''`。
    dom.querySelector<HTMLSelectElement>('#gameName')!.innerHTML =
      '<option value="3">原神</option>'
    const source = order({ time_requirement: '1天', project: null })
    writeFormState(dom, orderToAddOrderForm(source))
    const back = readFormState(dom, () => 7)
    expect(back.game).toBe(3)
    expect(back.server).toBe(7)
    expect(back.project_text).toBe('原神/深渊')
    expect(back.content).toBe('客户原始需求')
    expect(back.game_account).toBe('acc-1')
    expect(back.source_order_no).toBe('TB-123')
    expect(back.total_price).toBe('199.50')
    expect(back.is_urgent).toBe(true)
    // `time_requirement` 由 day/hours 合成：证据件默认 1天/0小时 ⇒ 与详情回填值一致
    expect(back.time_requirement).toBe('1天')
  })
})

/**
 * 编辑态**保存分支**（`?order_no=` 非空时点「保存」）—— 挂载整页跑出来的。
 *
 * 口径：「编辑订单」的保存**必须真提交**——用后端**既有**的
 * `PATCH /api/business/orders/<no>/`（`api/orders.ts` 的 `updateOrder`，端点 = `OrderViewSet.partial_update`）；
 * 成功后 `postMessage` 通知父窗口（关弹窗 / 出提示 / 刷新列表都在父窗口做，见
 * `order-orderlist.spec.ts` 的同名一组用例）。
 *
 * 断言打在**真实出口**上：`http.request`（`api/http.ts` 自述的「业务信封请求的统一传输/认证层出口」；
 * 注意 `api/` 下还有别的网络出口，见该文件模块注释里那份**非穷举**的说明）
 * 实际发出去的配置（**方法 + URL + 载荷**），通知取 `window.postMessage` 的真实调用。
 */
describe('编辑态保存：真发 PATCH（方法与载荷）+ 通知父窗口；失败则原样提示且不通知', () => {
  /** 详情响应：**故意给成一份「能过必填校验」的单**（game + project_text 都有）。 */
  const ORDER_DETAIL = {
    order_no: '20260921000000000001',
    game: 2,
    project_text: '原神/深渊',
    content: '客户原始需求',
    time_requirement: '',
    customer_phone: '13800138000',
    total_price: '199.50',
    source_order_no: 'TB-OLD',
    // 区服也是 xm-select（`#gameArea`）：编辑回填走 `setServerSelection`，与来源同形
    server: 7,
    // 编辑态要回填的来源字典 id（选项来自 `GET /order-sources/`，见下面的桩）
    order_source: 20,
  }

  /** 来源字典（下拉选项源）：两条，id 故意与详情里的 20 不同一条（11）以便测「改选」。 */
  const SOURCE_ROWS = [
    {
      id: 20,
      name: '闲鱼A',
      remark: '',
      order_by: 0,
      shop_type: 20,
      created_at: '',
      updated_at: '',
    },
    {
      id: 11,
      name: '淘宝店',
      remark: '',
      order_by: 0,
      shop_type: 0,
      created_at: '',
      updated_at: '',
    },
  ]

  type Sent = { method?: string; url?: string; data?: Record<string, unknown>; params?: Record<string, unknown> }

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
    window.history.replaceState({}, '', '/')
    LAYUI.msg.mockClear()
    LAYUI.handlers.clear()
    LAYUI.fsReset()
  })

  /** 挂载编辑态 → 返回「已发出的请求」与 `layer.msg` / `postMessage` 的真实调用记录。 */
  async function mountEditing(options?: {
    failPatchWith?: string
    /** PATCH 成功信封里的 `msg`（缺省空串 = 后端「保存成功」那条默认口径）。 */
    patchMsg?: string
    /** 覆盖详情响应的若干键（如「这单本来就没有来源」⇒ `order_source: null`）。 */
    detail?: Record<string, unknown>
    catalog?: {
      categories: { id: number; name: string; game: number }[]
      projects: { id: number; name: string; category: number }[]
    }
  }): Promise<{
    sent: Sent[]
    post: ReturnType<typeof vi.fn>
    root: HTMLElement
  }> {
    const detail = { ...ORDER_DETAIL, ...(options?.detail ?? {}) }
    window.history.replaceState({}, '', '/?order_no=20260921000000000001')
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: unknown) => {
        if (String(url).includes('order-addorder.html')) {
          return { ok: true, status: 200, text: async () => EVIDENCE }
        }
        throw new Error(`未预期的 fetch：${String(url)}`)
      }),
    )
    const { http } = await import('@/api/http')
    const sent: Sent[] = []
    vi.spyOn(http, 'request').mockImplementation((async (config: Sent) => {
      sent.push(config)
      const url = config.url ?? ''
      const method = (config.method ?? 'get').toLowerCase()
      if (url.endsWith('/orders/') && method === 'get') {
        return { data: { code: 0, msg: 'ok', data: { items: [
          { order_no: '20260921000000000002', project_text: '原单项目', total_price: '100.00', split_is_balance_anchor: true },
          { order_no: detail.order_no, project_text: detail.project_text, total_price: detail.total_price, split_is_balance_anchor: false },
        ], total: 2, page: 1, page_size: 200 } } }
      }
      if (url.includes('/orders/') && method === 'patch') {
        if (options?.failPatchWith !== undefined) throw new Error(options.failPatchWith)
        // `msg` 由用例给（缺省空串 = 真实后端「保存成功」那条的成功信封）。
        return { data: { code: 0, msg: options?.patchMsg ?? '', data: { ...detail } } }
      }
      if (url.includes('/orders/')) {
        return { data: { code: 0, msg: 'ok', data: { ...detail } } }
      }
      if (url.includes('/games/')) {
        return {
          data: {
            code: 0,
            msg: 'ok',
            data: { items: [{ id: 2, name: '原神' }], total: 1, page: 1, page_size: 200 },
          },
        }
      }
      if (url.includes('/servers/')) {
        return {
          data: {
            code: 0,
            msg: 'ok',
            data: {
              items: [
                { id: 7, name: '华北一区', game: 2 },
                { id: 8, name: '华东二区', game: 2 },
              ],
              total: 2,
              page: 1,
              page_size: 200,
            },
          },
        }
      }
      if (url.includes('/order-sources/')) {
        return {
          data: {
            code: 0,
            msg: 'ok',
            data: { items: SOURCE_ROWS, total: 2, page: 1, page_size: 200 },
          },
        }
      }
      if (options?.catalog && (url.includes('/categories/') || url.includes('/projects/'))) {
        const items = url.includes('/categories/')
          ? options.catalog.categories
          : options.catalog.projects.filter((row) => row.category === config.params?.category)
        return { data: { code: 0, msg: 'ok', data: { items, total: items.length, page: 1, page_size: 200 } } }
      }
      if (url.includes('/auth/me')) {
        // `fetchGames()` 会读 `/api/auth/me` 的 `user.game_ids`；本组的游戏字典只有 id=2
        // （`/api/business/games/` 那一支的 `items: [{ id: 2, name: '原神' }]`）⇒ 权限钉字面 [2]。
        // ⚠️ 载荷形状是后端信封的 `data.user`（`fetchMe` 取的是 `.user`）。
        return {
          data: {
            code: 0,
            msg: 'ok',
            data: {
              user: {
                username: 'u',
                display_name: 'u',
                game_ids: [2],
                menu_permissions: ME_PERMISSIONS_ALL,
              },
            },
          },
        }
      }
      return {
        data: { code: 0, msg: 'ok', data: { items: [], total: 0, page: 1, page_size: 20 } },
      }
    }) as never)

    // 编辑态保存成功后由本页 `window.parent.postMessage` 通知父窗口（jsdom 里
    // `window.parent === window`，所以 spy `window.postMessage` 就接在同一个出口上）。
    const post = vi.fn()
    vi.spyOn(window, 'postMessage').mockImplementation(post as never)

    const root = document.createElement('div')
    document.body.appendChild(root)
    await mountOrderAddOrder(root, { onNavigate: vi.fn() })
    // 前置哨兵：编辑态回填真的发生了（否则下面的断言可能来自「压根没进编辑态」）
    expect(sent.some((c) => (c.url ?? '').includes('/orders/20260921000000000001/'))).toBe(true)
    return { sent, post, root }
  }

  /** 走真实 DOM 改几个字段（详情回填之后）。 */
  function editFields(root: HTMLElement): void {
    root.querySelector<HTMLInputElement>('#moblie')!.value = '13900139000'
    root.querySelector<HTMLInputElement>('#souresOrderID')!.value = 'TB-NEW-1'
    root.querySelector<HTMLInputElement>('#remark')!.value = '客户要求换区服'
    // 金额也改一个：它**不会**进 PATCH 载荷（后端只读；改价走 `/price/`）
    root.querySelector<HTMLInputElement>('#price')!.value = '88.00'
  }

  /** 点保存并等 onSave 的异步链走完（`updateOrder` → 真实出口）。 */
  async function clickSave(): Promise<void> {
    const save = LAYUI.handlers.get('submit(savebtn)')
    expect(save).toBeTypeOf('function')
    LAYUI.msg.mockClear()
    save!()
    // `onSave` 里有 `await updateOrder(...)` 一层（updateOrder 内部再 await request），
    // 一个 microtask 不够——给 macrotask 一轮。
    await new Promise((resolve) => setTimeout(resolve, 0))
  }

  it('点保存**真的发 PATCH** 到 `orders/<单号>/`，载荷装的是改后的非金额字段', async () => {
    const { sent, post, root } = await mountEditing()
    editFields(root)
    await clickSave()

    const patch = sent.find((c) => (c.method ?? '').toLowerCase() === 'patch')
    expect(patch).toBeTruthy()
    expect(patch!.url).toBe('/api/business/orders/20260921000000000001/')
    expect(patch!.data).toMatchObject({
      customer_phone: '13900139000',
      source_order_no: 'TB-NEW-1',
      remark: '客户要求换区服',
      game: 2,
      project_text: '原神/深渊',
    })
    // 金额**一律不在** PATCH 载荷里（后端 `OrderUpdateSerializer` 把它们列为只读；
    // 改价只能走 `/price/`，否则会「看起来保存成功、价格没变」）。
    for (const field of ORDER_MONEY_FIELDS) {
      expect(patch!.data).not.toHaveProperty(field)
    }
    // 编辑**绝不建新单**（不能走成 POST /orders/）
    expect(sent.filter((c) => (c.method ?? 'get').toLowerCase() === 'post')).toEqual([])
    // 提示与关弹窗由父窗口负责：本页只发通知（iframe 一关，这里弹的提示就没了）
    // `msg` = 后端成功信封的原文（空串 ⇒ 父页回退「保存成功」）。
    expect(post).toHaveBeenCalledWith(
      { type: ORDER_SAVED_MESSAGE_TYPE, order_no: '20260921000000000001', msg: '' },
      window.location.origin,
    )
    expect(LAYUI.msg).not.toHaveBeenCalled()
  })

  it('只改备注时保留不在当前分类候选里的既有常用项目', async () => {
    const { sent, root } = await mountEditing({
      detail: { category: 10, project: 22 },
      catalog: {
        categories: [
          { id: 10, name: '合成分类甲', game: 2 },
          { id: 11, name: '合成分类乙', game: 2 },
        ],
        projects: [
          { id: 21, name: '当前分类的项目', category: 10 },
          { id: 22, name: '既有项目', category: 11 },
        ],
      },
    })
    expect(root.querySelector('input[name="dltype"][value="21"]')).not.toBeNull()
    expect(root.querySelector('input[name="dltype"][value="22"]')).toBeNull()
    root.querySelector<HTMLInputElement>('#remark')!.value = '只改备注'
    await clickSave()

    const patch = sent.find((request) => request.method === 'patch')
    expect(patch?.data).toMatchObject({ category: 10, remark: '只改备注' })
    expect(patch?.data).not.toHaveProperty('project')
  })

  it('PEER 子单普通编辑把接单价与其他字段放进同一发 PATCH', async () => {
    const { sent, root } = await mountEditing({ detail: { split_id: 'peer-group-01', split_kind: 'PEER' } })
    const price = root.querySelector<HTMLInputElement>('#price')!
    expect(price.disabled).toBe(false)
    price.value = '88.00'
    await clickSave()
    const patch = sent.find((entry) => (entry.method ?? '').toLowerCase() === 'patch')
    expect(patch?.data).toHaveProperty('total_price', '88.00')
    expect(sent.filter((entry) => (entry.method ?? '').toLowerCase() === 'post')).toEqual([])
  })

  /**
   * 后端保存成功后可能顺手撤代练通订单；**撤不掉时仍是成功码**，警告只在 `msg` 里。
   * 判据：`msg` 必须**原样**进 postMessage（父页负责用 alert 长停留显示）。
   */
  it('后端成功信封带「未能撤回」警告 ⇒ 原样带给父窗口（不吞、不改写、本页不自己弹）', async () => {
    const WARNING =
      '代练通订单未能撤回（原话：状态不允许），请先到『第三方平台订单管理』撤回代练通平台订单，再指派打手'
    const { post, root } = await mountEditing({ patchMsg: WARNING })
    editFields(root)
    await clickSave()
    expect(post).toHaveBeenCalledWith(
      { type: ORDER_SAVED_MESSAGE_TYPE, order_no: '20260921000000000001', msg: WARNING },
      window.location.origin,
    )
    // 提示仍由父窗口出（iframe 一关就没了）
    expect(LAYUI.msg).not.toHaveBeenCalled()
  })

  it('PATCH 失败 ⇒ 原样展示后端消息，且**不**通知父窗口（弹窗不关、列表不刷）', async () => {
    const { sent, post, root } = await mountEditing({ failPatchWith: '订单状态不允许编辑' })
    editFields(root)
    await clickSave()

    expect(sent.some((c) => (c.method ?? '').toLowerCase() === 'patch')).toBe(true)
    expect(LAYUI.msg).toHaveBeenCalledWith('订单状态不允许编辑', { time: 1500 })
    expect(post).not.toHaveBeenCalled()
  })

  it('必填校验失败 ⇒ **不发** PATCH（请求来源的值不合法时连出口都到不了）', async () => {
    const { sent, post, root } = await mountEditing()
    // 把项目文本清空（后端必填之一）
    root.querySelector<HTMLInputElement>('#project')!.value = ''
    await clickSave()

    expect(sent.filter((c) => (c.method ?? 'get').toLowerCase() === 'patch')).toEqual([])
    expect(post).not.toHaveBeenCalled()
    expect(LAYUI.msg).toHaveBeenCalled()
  })

  it('「代练项目」超 700 字节同步上限 ⇒ **不发** PATCH，出逐字上限文案（2026-10-15 E-0242）', async () => {
    const { sent, post, root } = await mountEditing()
    // 250 个汉字 = 750 字节（UTF-8 中文一字符 3 字节）——编辑态回填值再改写，同样被闸兜住
    root.querySelector<HTMLInputElement>('#project')!.value = '深'.repeat(250)
    await clickSave()

    expect(sent.filter((c) => (c.method ?? 'get').toLowerCase() === 'patch')).toEqual([])
    expect(post).not.toHaveBeenCalled()
    expect(LAYUI.msg).toHaveBeenCalledWith(
      '代练项目超出鸡蛋铺同步上限（当前 750 字节，上限 700 字节），请精简后再保存；完整细节可放订单截图',
      { time: 1500 },
    )
  })

  /**
   * **编辑态不受「订单总价必填」影响**——这是**有意**的，不是漏做：
   * 编辑态载荷由 `formToUpdatePayload` 摘掉了全部金额键（改价走订单列表的 `/price/` 入口，
   * 那条路的表单本身已必填）⇒ 这里的 `#price` 空不空**根本不会提交**。
   * 业务方裁定（D-0082）也只针对**录单**。
   *
   * 判别力：把 `onSave` 里那道闸从「只在新建态」改成无条件接 ⇒ 这条用例变红
   * （收不到 PATCH、反而收到「订单总价不可为空」）。
   */
  it('编辑态 `#price` 清空仍能保存（有意：编辑态不发金额，总价必填只对录单态）', async () => {
    const { sent, post, root } = await mountEditing()
    root.querySelector<HTMLInputElement>('#price')!.value = ''
    await clickSave()

    const patch = sent.find((c) => (c.method ?? '').toLowerCase() === 'patch')
    expect(patch).toBeTruthy()
    // 金额一律不进 PATCH 载荷（后端只读；改价走 `/price/`）——所以空总价无从提交、也就不该被拦
    expect(patch!.data).not.toHaveProperty('total_price')
    expect(LAYUI.msg).not.toHaveBeenCalledWith('订单总价不可为空', { time: 1500 })
    expect(post).toHaveBeenCalled()
  })

  // **走真插件的事件时序**（替身，见文件头的 `fsPick`/`fsClear`）。
  // 判别力：把 `mount` 里的同步从 `endOn` 退回 `on`（或用错 id）⇒ 这条红。
  it('C1：编辑态四处交互（回填 20 → 点选 11 → 点选 20 → 清空）隐藏 select 与读取值都正确', async () => {
    const { sent, root } = await mountEditing()
    const select = root.querySelector<HTMLSelectElement>('#orderSoures')!
    const read = (): number | null => readOrderSource(root, () => LAYUI.fs.value('orderSoures'))

    // ① 回填 20（编辑详情带 order_source=20）
    expect(select.value).toBe('20')
    expect(LAYUI.fsValues('orderSoures')).toEqual([20])
    expect(read()).toBe(20)

    // ② 点选 11（改选）：**改后**事件才拿得到 11；挂 `on` 会写进上一个 id 20
    LAYUI.fsPick('orderSoures', 11)
    expect(select.value).toBe('11')
    expect(LAYUI.fsValues('orderSoures')).toEqual([11])
    expect(read()).toBe(11)

    // ③ 再点选 20
    LAYUI.fsPick('orderSoures', 20)
    expect(select.value).toBe('20')
    expect(read()).toBe(20)

    // ④ 点「清空」：插件给的是**空集**，必须写 `''`（跳过就把 20 留在 select 里）
    LAYUI.fsClear('orderSoures')
    expect(select.value).toBe('')
    expect(LAYUI.fsValues('orderSoures')).toEqual([])
    expect(read()).toBeNull()

    // 真实出口：清空后保存，PATCH 里是**显式 null**（I1），不是「旧 id 20」
    editFields(root)
    await clickSave()
    const patch = sent.find((c) => (c.method ?? '').toLowerCase() === 'patch')
    expect(patch!.data).toHaveProperty('order_source', null)
  })

  // T6：编辑态回填来源。判别力——`orderToAddOrderForm` 不带 `order_source`，
  // 或 `prefillEditOrder` 不调 `setOrderSourceSelection`，两条都会红。
  it('编辑态回填来源：hidden select 落到详情里的 id，且未改动时随 PATCH 原样带回', async () => {
    const { sent, post, root } = await mountEditing()
    // 回填真的落到了那个 xm-select 的宿主 `<select>` 上（选项来自 `fetchOrderSources()`）
    expect(root.querySelector<HTMLSelectElement>('#orderSoures')!.value).toBe('20')
    editFields(root)
    await clickSave()

    const patch = sent.find((c) => (c.method ?? '').toLowerCase() === 'patch')
    expect(patch).toBeTruthy()
    // 不回填/不带回 ⇒ 编辑保存会把来源「改没」或「改不动」——两者都是静默失败
    expect(patch!.data).toMatchObject({ order_source: 20 })
    expect(post).toHaveBeenCalled()
  })

  /* ── S3（D-0178）：编辑态**不动**来源 ────── */

  /**
   * **S3①**：编辑态订单自带的来源是**第二条**（id 11，而首屏「第一条」是 20）
   * ⇒ 必须还是 11，**不**被那条默认值顶掉（spec §3.3 第 3 条）。
   * 判别力：把首屏那句「选第一条」挪到 `prefillEditOrder` **之后**（或去掉编辑态那道闸）⇒ 这里变 20。
   */
  it('S3①：编辑态订单自带来源（11）不被首屏那条「第一条」（20）顶掉', async () => {
    const { sent, root } = await mountEditing({ detail: { order_source: 11 } })
    expect(root.querySelector<HTMLSelectElement>('#orderSoures')!.value).toBe('11')
    expect(LAYUI.fsValues('orderSoures')).toEqual([11])
    editFields(root)
    await clickSave()
    const patch = sent.find((c) => (c.method ?? '').toLowerCase() === 'patch')
    expect(patch!.data).toMatchObject({ order_source: 11 })
  })

  /**
   * **S3②**：编辑态订单**没有**来源（`order_source: null`）⇒ 未选中，**不**给它编一个第一条。
   * 判别力：去掉首屏那句的 `editOrderNo === ''` ⇒ 这里会是 `20`（把「本来没有」静默改成「第一条」）。
   */
  it('S3②：编辑态订单没有来源 ⇒ 未选中（不编一个第一条给它）', async () => {
    const { root } = await mountEditing({ detail: { order_source: null } })
    expect(root.querySelector<HTMLSelectElement>('#orderSoures')!.value).toBe('')
    expect(LAYUI.fsValues('orderSoures')).toEqual([])
  })

  // **判别测试**：改选**经插件的事件契约**驱动（「直接给隐藏 select 赋值」是假测试——
  // 它绕过了唯一会写这个 select 的生产代码）。
  it('编辑态改选另一个来源 ⇒ PATCH 载荷是**新的** id（不是回填的旧 id）', async () => {
    const { sent, root } = await mountEditing()
    expect(root.querySelector<HTMLSelectElement>('#orderSoures')!.value).toBe('20')
    LAYUI.fsPick('orderSoures', 11)
    editFields(root)
    await clickSave()

    const patch = sent.find((c) => (c.method ?? '').toLowerCase() === 'patch')
    expect(patch!.data).toMatchObject({ order_source: 11 })
  })

  // 编辑态「把来源清空」必须在 PATCH 上**可表达**。
  // `formToPayload` 会丢掉 `null`（空值不发），所以「初始非空 → 用户清空」这一支要显式补 `null`。
  it('编辑态清空来源 ⇒ PATCH 载荷**含** `order_source: null`（spec §5.1 的 FK 置空）', async () => {
    const { sent, root } = await mountEditing()
    expect(root.querySelector<HTMLSelectElement>('#orderSoures')!.value).toBe('20')
    LAYUI.fsClear('orderSoures')
    editFields(root)
    await clickSave()

    const patch = sent.find((c) => (c.method ?? '').toLowerCase() === 'patch')
    expect(patch).toBeTruthy()
    // 显式 null：不是「键缺失」（键缺失会让后端原样保留旧 FK，用户看到「清空成功」却没清）
    expect(patch!.data).toHaveProperty('order_source', null)
  })

  // I1 的另一半：**没动过**不许凭空补键。
  // 场景 = 这单本来就没有来源（`order_source: null`）且用户没碰下拉。
  it('编辑态「本来就没来源且没动过」⇒ PATCH 载荷**不含** `order_source` 键', async () => {
    const { sent, root } = await mountEditing({ detail: { order_source: null } })
    expect(root.querySelector<HTMLSelectElement>('#orderSoures')!.value).toBe('')
    editFields(root)
    await clickSave()

    const patch = sent.find((c) => (c.method ?? '').toLowerCase() === 'patch')
    expect(patch).toBeTruthy()
    expect(patch!.data).not.toHaveProperty('order_source')
  })

  // 详情带 `order_source=999`，而字典（`SOURCE_ROWS`）里**没有** 999 —— 现实成因是第 201 条被
  // `page_size=200` 截断，或 `fetchOrderSources()` 失败走了 catch（选项为空）。
  // 真插件对 API 写入加不进不存在的 id（`:1562`）⇒ 隐藏 select 仍为 `""` ⇒ `readOrderSource` 返回 `null`。
  // 用户**只改个电话号**保存：载荷**不得**含 `order_source`（既不是 `null`、也不是 999）——
  // 键缺失 = 后端原样保留旧 FK。判别力：判据退回 `source === null`（丢开「用户真的清过」）⇒ 这条红。
  it('NEW-I1：详情带字典里没有的来源 id（999）且用户没动下拉 ⇒ PATCH **不含** `order_source` 键', async () => {
    const { sent, root } = await mountEditing({ detail: { order_source: 999 } })
    // 前置哨兵：999 真的没进插件、也没落进隐藏 `<select>`（选项只有 20/11）
    expect(LAYUI.fsValues('orderSoures')).toEqual([])
    expect(root.querySelector<HTMLSelectElement>('#orderSoures')!.value).toBe('')
    editFields(root)
    await clickSave()

    const patch = sent.find((c) => (c.method ?? '').toLowerCase() === 'patch')
    expect(patch).toBeTruthy()
    // 含 `order_source: null` ⇒ 来源被静默清空（丢开「用户真的清过」的方向）；含 999 ⇒ 发一个不存在的 FK。
    expect(patch!.data).not.toHaveProperty('order_source')
  })

  // 同型缺陷：`#gameArea` 用的是同一个「改前钩子」形状，且**真的会提交旧值**——
  // 点 A 后 select 仍空（回落能救），再点 B 时 `on` 带来的是 A ⇒ `readServer` 读到 A。
  it('gameArea 同型缺陷：连点两个区服，隐藏 select 与 `readFormState().server` 都是**后点的**那个', async () => {
    const { root } = await mountEditing()
    // 编辑态的区服也由插件选中（`fetchServers` 给了什么就用什么；这里只要求「有选项」）
    const select = root.querySelector<HTMLSelectElement>('#gameArea')!
    const options = Array.from(select.options).map((o) => Number(o.value))
    expect(options.length).toBeGreaterThanOrEqual(2)
    const [first, second] = options as [number, number]

    // 先点**第二个**（回填值是 7）：挂 `on` 时这里写进的是改前的 7 ⇒ 直接红
    LAYUI.fsPick('gameArea', second)
    expect(select.value).toBe(String(second))
    expect(readFormState(root).server).toBe(second)

    LAYUI.fsPick('gameArea', first)
    expect(select.value).toBe(String(first))
    expect(readFormState(root).server).toBe(first)
  })
})

/**
 * T6：「订单来源」下拉接真数据（`GET /api/business/order-sources/`）。
 *
 * 三条判据（spec §6.2 / §8 #18 + 计划 Part 4 的 T4 契约）：
 *  1. 取数走 `fetchOrderSources()`，**带 `page_size=200`**（默认 20 会静默截断到 20 条）；
 *  2. 选项来自**整页的 `items`**（`fetchOrderSources()` 返回 `{items,total,page,page_size}`，
 *     不是解包后的数组——计划 T6 伪代码里的 `page.map(...)` 是改前写法）；
 *  3. 选中值随录单提交为 **数字 id** 的 `order_source`；**没选就不发这个键**（空值不发）。
 *
 * 断言打在真实出口上：`http.request` 实际收到的 **方法 + URL + params/载荷**，
 * 以及隐藏 `<select id="orderSoures">` 的真实 `option`（它是「选项真的来自 items」的可见证据）。
 */
describe('录单页订单来源下拉：选项来自 fetchOrderSources（带 page_size=200）且选中进载荷', () => {
  type Sent = {
    method?: string
    url?: string
    data?: Record<string, unknown>
    params?: Record<string, unknown>
  }

  const SOURCE_ROWS = [
    {
      id: 20,
      name: '闲鱼A',
      remark: '',
      order_by: 0,
      shop_type: 20,
      created_at: '',
      updated_at: '',
    },
    {
      id: 11,
      name: '淘宝店',
      remark: '',
      order_by: 0,
      shop_type: 0,
      created_at: '',
      updated_at: '',
    },
  ]

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
    window.history.replaceState({}, '', '/')
    LAYUI.msg.mockClear()
    LAYUI.handlers.clear()
    LAYUI.fsReset()
  })

  /** 挂载**新增**态（无 `order_no`）→ 返回「已发出的请求」与 root。 */
  async function mountCreate(options?: {
    /** 覆盖来源字典的返回行（I-4 的转义用例需要一段含 HTML 的 `name`）。 */
    sourceRows?: typeof SOURCE_ROWS
  }): Promise<{ sent: Sent[]; root: HTMLElement }> {
    window.history.replaceState({}, '', '/?page=order-addorder')
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: unknown) => {
        if (String(url).includes('order-addorder.html')) {
          return { ok: true, status: 200, text: async () => EVIDENCE }
        }
        throw new Error(`未预期的 fetch：${String(url)}`)
      }),
    )
    const { http } = await import('@/api/http')
    const sent: Sent[] = []
    vi.spyOn(http, 'request').mockImplementation((async (config: Sent) => {
      sent.push(config)
      const url = config.url ?? ''
      const method = (config.method ?? 'get').toLowerCase()
      if (url.includes('/order-sources/')) {
        const rows = options?.sourceRows ?? SOURCE_ROWS
        return {
          data: {
            code: 0,
            msg: 'ok',
            data: { items: rows, total: rows.length, page: 1, page_size: 200 },
          },
        }
      }
      if (url.includes('/games/')) {
        return {
          data: {
            code: 0,
            msg: 'ok',
            data: { items: [{ id: 2, name: '原神' }], total: 1, page: 1, page_size: 200 },
          },
        }
      }
      if (url.includes('/auth/me')) {
        // `fetchGames()` 会读 `/api/auth/me` 的 `user.game_ids`；本组的游戏字典只有 id=2
        // （`/api/business/games/` 那一支的 `items: [{ id: 2, name: '原神' }]`）⇒ 权限钉字面 [2]。
        // ⚠️ 载荷形状是后端信封的 `data.user`（`fetchMe` 取的是 `.user`）。
        return {
          data: {
            code: 0,
            msg: 'ok',
            data: {
              user: {
                username: 'u',
                display_name: 'u',
                game_ids: [2],
                menu_permissions: ME_PERMISSIONS_ALL,
              },
            },
          },
        }
      }
      if (url.includes('/orders/') && method === 'post') {
        return { data: { code: 0, msg: 'ok', data: { order_no: '20260921000000000009' } } }
      }
      return {
        data: { code: 0, msg: 'ok', data: { items: [], total: 0, page: 1, page_size: 200 } },
      }
    }) as never)

    const root = document.createElement('div')
    document.body.appendChild(root)
    await mountOrderAddOrder(root, { onNavigate: vi.fn() })
    return { sent, root }
  }

  /** 填一个能过必填校验的表单并点保存，等 `onSave` 的异步链走完（`createOrder` → 真实出口）。 */
  async function fillAndSave(root: HTMLElement): Promise<void> {
    root.querySelector<HTMLInputElement>('#project')!.value = '深渊满星'
    const save = LAYUI.handlers.get('submit(savebtn)')
    expect(save).toBeTypeOf('function')
    save!()
    await new Promise((resolve) => setTimeout(resolve, 0))
  }

  /* ── ⭐ 拆单：录单页那颗勾选框的接线 ─────────────
   * 判据打在**真实出口**上：`api/orders.ts::createOrder` 拼出来的请求体（`sent` 里那一发）。
   * 两条互为对照：不勾 ⇒ **没有**这个键（缺键 = 不拆）；勾上 ⇒ 真 `bool true`
   * （⛔ 不是对标那个 `chaifen=Y` 字符串）。 */

  it('拆单：不勾那颗框 ⇒ 载荷**没有** `split` 键（缺键 = 不拆）', async () => {
    const { sent, root } = await mountCreate()
    await fillAndSave(root)
    const post = sent.find((c) => (c.method ?? '').toLowerCase() === 'post')
    expect(post).toBeTruthy()
    expect(post!.data).not.toHaveProperty('split')
  })

  it('拆单：勾上那颗框 ⇒ 载荷带 `split: true`（真 bool），且保存成功后勾被复位', async () => {
    const { sent, root } = await mountCreate()
    const box = root.querySelector<HTMLInputElement>('input[name=chaifen]')
    expect(box, '证据件里没有 input[name=chaifen]').not.toBeNull()
    box!.checked = true
    await fillAndSave(root)
    const post = sent.find((c) => (c.method ?? '').toLowerCase() === 'post')
    expect(post!.url).toBe('/api/business/orders/')
    expect(post!.data!.split).toBe(true)
    expect(typeof post!.data!.split).toBe('boolean')
    // 保存成功后 `resetFormToInitial` 的 `form.reset()` 把勾去掉（证据件那颗框没有 `checked`
    // 属性 ⇒ 复位回「不勾」）：不复位的话**下一单会静默也拆**。
    expect(box!.checked).toBe(false)
  })

  it('首屏请求带 `page=1 & page_size=200`，且宿主 select 的选项来自整页的 items', async () => {
    const { sent, root } = await mountCreate()
    const call = sent.find((c) => (c.url ?? '').includes('/order-sources/'))
    expect(call).toBeTruthy()
    expect((call!.method ?? 'get').toLowerCase()).toBe('get')
    // `page_size=200` ≤ 后端 `max_page_size`（现为 300，api/base.py）；不带会被默认 20 截断
    expect(call!.params).toEqual({ page: 1, page_size: 200 })
    const options = Array.from(root.querySelectorAll<HTMLOptionElement>('#orderSoures option'))
    expect(options.map((o) => [o.value, o.textContent])).toEqual([
      ['20', '闲鱼A'],
      ['11', '淘宝店'],
    ])
  })

  /* ── **订单来源默认选中第一条**（S2 / S4 / S5；spec §3.3，决策 `D-0178`） ───────────────────────── */

  /** **S2（新增）**：保存成功后的复位**重新选中第一条**（不是空）。 */
  it('S2：保存成功后来源下拉仍是第一条（不是空）', async () => {
    const { root } = await mountCreate()
    await fillAndSave(root)
    expect(root.querySelector<HTMLSelectElement>('#orderSoures')!.value).toBe('20')
    expect(LAYUI.fsValues('orderSoures')).toEqual([20])
  })

  /**
   * **S4（新增）**：来源字典为空 ⇒ **不炸、不选中、一句提示都不出**（挂载与保存两条链都走完）。
   *
   * ⚠️ 判据为什么取「**一句提示都没有**」而不是「没抛异常」（这是变异实测逼出来的）：
   * 首屏那句选第一条的代码落在 `fetchOrderSources()` 那个 `try` 里 ⇒ 少了「字典非空」那半句判据，
   * 抛出的 `TypeError` 会被 catch 成 `msg('Cannot read properties of undefined …')`
   * ——**内部报错当用户提示**（本仓禁止的形状），而「值读到 `''`、没抛到调用方」两件事**照样成立**。
   * 实测：只断言前两行的版本对这个变异是 **SURVIVED**（假绿）。
   * 同理，`resetAfterSave` 里那句「非空」判据被删时，保存链会在复位处抛进 `onSave` 的 catch，
   * 用户看到的是内部文案 ⇒ 所以这里**连保存链一起**钉。
   */
  it('S4：来源字典为空时不炸、不选中、一句提示都不出（挂载与保存两条链都走完）', async () => {
    const { sent, root } = await mountCreate({ sourceRows: [] })
    expect(root.querySelector<HTMLSelectElement>('#orderSoures')!.value).toBe('')
    expect(LAYUI.fsValues('orderSoures')).toEqual([])
    expect(LAYUI.msg).not.toHaveBeenCalled()

    await fillAndSave(root)
    const post = sent.find(
      (c) => (c.method ?? '').toLowerCase() === 'post' && c.url === '/api/business/orders/',
    )
    expect(post).toBeTruthy()
    expect(post!.data).not.toHaveProperty('order_source')
    // 保存链真的走完了（`resetAfterSave` 那一步没把异常漏成提示）
    expect(LAYUI.msg).toHaveBeenCalledWith(SAVE_SUCCESS_MESSAGE, { time: 1500 })
    for (const call of LAYUI.msg.mock.calls) {
      expect(String(call[0])).not.toContain('Cannot read')
    }
  })

  /**
   * 新建态首屏就选中第一条（D-0178，spec §1 裁定 2 / §4 的 S1 口径），载荷**必须**带上它。
   * ⛔ 断言是**等于第一条那个 id**（具体值），不许放宽回「键不存在」；
   * ⛔ 也**没有**加「来源不能为空」的保存闸（同一裁定的后半句）。
   * ℹ️「没选来源」这一档今天只剩「字典为空」（`S4`）与「用户手动清空」两种形态。
   * ⚠️ **顺序也是判据的一部分**：`setOrderSourceSelectOptions` 会**清掉**选中
   * ⇒ 选中必须在它**之后**（「灌选项时顺手清空」那一版会让首屏那两条红）。
   */
  it('S5（改自「没选来源 ⇒ 载荷没有 order_source 键」，2026-10-03 业务方裁定）：新建态载荷带上第一条来源', async () => {
    const { sent, root } = await mountCreate()
    // 首屏刚加载完选项时就是**第一条**（不再是「未选中」）
    expect(root.querySelector<HTMLSelectElement>('#orderSoures')!.value).toBe('20')
    // 插件实例态（真插件里选中值只存在于实例中——隐藏 `<select>` 对不代表插件里也对）
    expect(LAYUI.fsValues('orderSoures')).toEqual([20])
    await fillAndSave(root)
    const post = sent.find((c) => (c.method ?? '').toLowerCase() === 'post')
    expect(post).toBeTruthy()
    expect(post!.data).toMatchObject({ order_source: 20 })
    // 载荷里是**数字** id（不是字符串）——`readOrderSource` 的出口契约
    expect(typeof post!.data!.order_source).toBe('number')
  })

  it('选中来源后保存 ⇒ 载荷含 `order_source: 20`（数字 id，spec §8 #18）', async () => {
    const { sent, root } = await mountCreate()
    // 走**插件的事件契约**选（不是直接给隐藏 select 赋值——那条路绕过了生产代码）
    LAYUI.fsPick('orderSoures', 20)
    expect(root.querySelector<HTMLSelectElement>('#orderSoures')!.value).toBe('20')
    await fillAndSave(root)
    const post = sent.find((c) => (c.method ?? '').toLowerCase() === 'post')
    expect(post!.url).toBe('/api/business/orders/')
    expect(post!.data).toMatchObject({ order_source: 20, game: 2, project_text: '深渊满星' })
    expect(typeof post!.data!.order_source).toBe('number')
  })

  /**
   * 复位之后**重新选中第一条**——「保存成功 → 再录一笔」与「刚打开页面」同一口径
   *（spec §3.3 第 2 条）。
   * ⚠️ 所以这条用例先把客服改选的**第二条**（11）选上再保存：复位必须是**回到第一条 20**，
   * 既不是空、也不是留在 11 —— 只断言「非空」或只断言隐藏 `<select>` 都不够（两条腿都要）。
   */
  it('S6（改自「保存成功后来源下拉复位」，2026-10-03 业务方裁定）：复位后仍是第一条来源', async () => {
    const { root } = await mountCreate()
    LAYUI.fsPick('orderSoures', 11)
    expect(root.querySelector<HTMLSelectElement>('#orderSoures')!.value).toBe('11')
    await fillAndSave(root)
    expect(root.querySelector<HTMLSelectElement>('#orderSoures')!.value).toBe('20')
    // 插件的实例状态也要一起回到第一条（否则只是隐藏 select 看着对、插件里还留着 11）
    expect(LAYUI.fsValues('orderSoures')).toEqual([20])
  })

  /**
   * `setOrderSourceSelectOptions` 用 `innerHTML` 拼
   * `<option value="${s.id}">${s.name}</option>`，`s.name` 来自 API（用户输入）。
   *
   * ⚠️ **判别力为什么钉在「转义后的文本」而不是「不含 `<img`」上（实测）**：
   * `<select>` 的 fragment 解析走 HTML 的 **in select** 插入模式，`<img …>` 这类 start tag
   * 会被**丢弃**——所以「不转义」那版解析出来的 innerHTML 里本来就**没有** `<img`，
   * 单看「不含标签」是恒真断言（假绿）。探针（jsdom）对**本用例的 payload**：
   *   RAW innerHTML = `<option value="20"></option>` / text = `''`（`<img …>` 被丢弃）
   *   ESC innerHTML = `<option value="20">&lt;img src=x onerror=alert(1)&gt;</option>` / text = 原 payload
   * ⇒ 判据取 **`option.textContent` 必须等于原 payload**（未转义时它是 `''`，会红）
   * 与 **innerHTML 里出现转义文本**（未转义时 `&lt;` 不出现，会红）。
   */
  it('I-4：来源名里的 HTML payload 被转义进 `<option>`（存储型 XSS sink）', async () => {
    const payload = '<img src=x onerror=alert(1)>'
    const { root } = await mountCreate({ sourceRows: [{ ...SOURCE_ROWS[0]!, name: payload }] })
    const select = root.querySelector<HTMLSelectElement>('#orderSoures')!
    const option = select.querySelector<HTMLOptionElement>('option')!
    // 值保持**原样显示**（转义只改 HTML 语法，不改变可见文本）
    expect(option.textContent).toBe(payload)
    // HTML 里是可执行标签的**转义文本**，不是标签本身
    expect(select.innerHTML).toContain('&lt;img src=x onerror=alert(1)&gt;')
    expect(select.innerHTML).not.toContain('<img')
  })
})

/**
 * 录单（**新建态**）的第三条必填：**订单总价**（业务方裁定，见 `docs/DECISIONS.md` D-0082）。
 *
 * 业务背景：总价留空时 `formToPayload` 会丢掉那个空串键、后端落 `default=0.00`
 * ⇒ 订单被**静默**记成 0 元。这道闸把「静默的 0」变成「显式看见的 0」：页面里**默认就是 `0`**
 * （来自证据件 `#price` 的 `value="0"`），**只有人主动清空**才会被拦。
 *
 * ⚠️ **有意偏离对标**（登记在 `docs/FRONTEND-REPLICA-STANDARD.md` §7）：对标 `#price` 只带
 * `lay-verify="number"`，而 layui 2.13.9 那条规则是 `if(e && isNaN(e))` ⇒ **空值放行**，
 * 即对标允许把总价留空提交。
 *
 * 断言打在**真实出口**上：`http.request` 实际发出的配置（方法 + URL + 载荷）+ `layer.msg` 的真实调用。
 */
describe('录单页（新建态）：订单总价必填 + 默认 0（业务方 2026-09-22 裁定 D-0082）', () => {
  type Sent = {
    method?: string
    url?: string
    data?: Record<string, unknown>
    params?: Record<string, unknown>
  }

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
    window.history.replaceState({}, '', '/')
    LAYUI.msg.mockClear()
    LAYUI.handlers.clear()
    LAYUI.fsReset()
  })

  /** 挂载**新建**态（无 `order_no`）→ 返回「已发出的请求」与 root。 */
  async function mountCreate(): Promise<{ sent: Sent[]; root: HTMLElement }> {
    window.history.replaceState({}, '', '/?page=order-addorder')
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: unknown) => {
        if (String(url).includes('order-addorder.html')) {
          return { ok: true, status: 200, text: async () => EVIDENCE }
        }
        throw new Error(`未预期的 fetch：${String(url)}`)
      }),
    )
    const { http } = await import('@/api/http')
    const sent: Sent[] = []
    vi.spyOn(http, 'request').mockImplementation((async (config: Sent) => {
      sent.push(config)
      const url = config.url ?? ''
      const method = (config.method ?? 'get').toLowerCase()
      if (url.includes('/games/')) {
        return {
          data: {
            code: 0,
            msg: 'ok',
            data: { items: [{ id: 2, name: '原神' }], total: 1, page: 1, page_size: 200 },
          },
        }
      }
      if (url.includes('/auth/me')) {
        // `fetchGames()` 会读 `/api/auth/me` 的 `user.game_ids`；本组的游戏字典只有 id=2
        // （`/api/business/games/` 那一支的 `items: [{ id: 2, name: '原神' }]`）⇒ 权限钉字面 [2]。
        // ⚠️ 载荷形状是后端信封的 `data.user`（`fetchMe` 取的是 `.user`）。
        return {
          data: {
            code: 0,
            msg: 'ok',
            data: {
              user: {
                username: 'u',
                display_name: 'u',
                game_ids: [2],
                menu_permissions: ME_PERMISSIONS_ALL,
              },
            },
          },
        }
      }
      if (url.includes('/orders/') && method === 'post') {
        return { data: { code: 0, msg: 'ok', data: { order_no: '20260921000000000009' } } }
      }
      return {
        data: { code: 0, msg: 'ok', data: { items: [], total: 0, page: 1, page_size: 200 } },
      }
    }) as never)

    const root = document.createElement('div')
    document.body.appendChild(root)
    await mountOrderAddOrder(root, { onNavigate: vi.fn() })
    return { sent, root }
  }

  /**
   * 点保存并等 `onSave` 的异步链走完（`createOrder` → 真实出口）。
   * 项目是既有两条必填之一，先填上 ⇒ 「总价」成为**唯一**可能失败的那条闸。
   */
  async function clickSave(root: HTMLElement): Promise<void> {
    root.querySelector<HTMLInputElement>('#project')!.value = '深渊满星'
    const save = LAYUI.handlers.get('submit(savebtn)')
    expect(save).toBeTypeOf('function')
    LAYUI.msg.mockClear()
    save!()
    await new Promise((resolve) => setTimeout(resolve, 0))
  }

  const countPosts = (sent: Sent[]): number =>
    sent.filter((c) => (c.method ?? 'get').toLowerCase() === 'post').length

  it('打开时 `#price` 就是证据件的默认值 `"0"`（不是空串；TS 里没有第二处赋值）', async () => {
    const { root } = await mountCreate()
    expect(root.querySelector<HTMLInputElement>('#price')!.value).toBe('0')
    // 钉住「默认 0 的唯一出处是证据件」：那一行确实带 value="0" 与对标自带的 lay-verify="number"
    const priceTag = EVIDENCE.split('\n').find((l) => l.includes('id="price"'))!
    expect(priceTag).toContain('value="0"')
    expect(priceTag).toContain('lay-verify="number"')
  })

  it('清空总价后点保存 ⇒ 出「订单总价不可为空」提示，且**一个建单请求都没发**', async () => {
    const { sent, root } = await mountCreate()
    root.querySelector<HTMLInputElement>('#price')!.value = ''
    await clickSave(root)

    expect(LAYUI.msg).toHaveBeenCalledWith('订单总价不可为空', { time: 1500 })
    // 首屏那几条 GET 不算；闸门挡在**出口之前** ⇒ POST 数为 0
    expect(countPosts(sent)).toBe(0)
  })

  it('总价是纯空白（全角空格）时同样被拦（判据取 `isBlankLikePython` 的 Python strip 语义）', async () => {
    const { sent, root } = await mountCreate()
    root.querySelector<HTMLInputElement>('#price')!.value = '\u3000'
    await clickSave(root)

    expect(LAYUI.msg).toHaveBeenCalledWith('订单总价不可为空', { time: 1500 })
    expect(countPosts(sent)).toBe(0)
  })

  it('反向对照：总价填真实值时**正常提交**，请求体里带 `total_price`', async () => {
    const { sent, root } = await mountCreate()
    root.querySelector<HTMLInputElement>('#price')!.value = '128.00'
    await clickSave(root)

    const post = sent.find((c) => (c.method ?? '').toLowerCase() === 'post')
    expect(post).toBeTruthy()
    expect(post!.url).toBe('/api/business/orders/')
    expect(post!.data).toMatchObject({ total_price: '128.00', project_text: '深渊满星' })
    expect(LAYUI.msg).toHaveBeenCalledWith(SAVE_SUCCESS_MESSAGE, { time: 1500 })
  })

  it('总价是 `0` 也照样提交（「填了 0」不是「没填」，E-0021 / D-0015）', async () => {
    const { sent, root } = await mountCreate()
    // 证据件的默认值就是 '0'：**不动它**直接保存必须能过，否则「默认 0」等于「默认提交不了」
    await clickSave(root)

    const post = sent.find((c) => (c.method ?? '').toLowerCase() === 'post')
    expect(post).toBeTruthy()
    expect(post!.data).toMatchObject({ total_price: '0' })
  })

  it('保存成功后复位：`#price` 回到证据件的默认值 `"0"`（不是空串）', async () => {
    const { root } = await mountCreate()
    root.querySelector<HTMLInputElement>('#price')!.value = '128.00'
    await clickSave(root)
    // `resetFormToInitial` 用原生 `form.reset()` → 回到 `value` 属性 = '0'
    expect(root.querySelector<HTMLInputElement>('#price')!.value).toBe('0')
  })
})

/**
 * 「代练项目」700 字节同步上限闸（业务方裁定；生产取证 `docs/ERRORS.md` **E-0242**：
 * 对方接受上限按**字节**算、约 732 B ⇒ 上限 700 留余量）。保存前在既有校验链**末尾**追加
 * `projectTextSyncLimitError`，超限**警告并阻止保存**（不做静默截断）。
 *
 * 本组直测两条钉的是**函数本体**、「恰 700 放行」钉的是**防误伤方向**——它们不经
 * `onSave` 校验链，对「摘掉 `|| projectSyncBad`」那类链级变异按设计**不敏感**（保持绿才是对的）。
 */
describe('录单页（新建/编辑态）：代练项目 700 字节同步上限闸（2026-10-15 E-0242）', () => {
  type Sent = {
    method?: string
    url?: string
    data?: Record<string, unknown>
    params?: Record<string, unknown>
  }

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
    window.history.replaceState({}, '', '/')
    LAYUI.msg.mockClear()
    LAYUI.handlers.clear()
    LAYUI.fsReset()
  })

  /** 挂载**新建**态（无 `order_no`）→ 返回「已发出的请求」与 root（形态照「订单总价必填」组）。 */
  async function mountCreate(): Promise<{ sent: Sent[]; root: HTMLElement }> {
    window.history.replaceState({}, '', '/?page=order-addorder')
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: unknown) => {
        if (String(url).includes('order-addorder.html')) {
          return { ok: true, status: 200, text: async () => EVIDENCE }
        }
        throw new Error(`未预期的 fetch：${String(url)}`)
      }),
    )
    const { http } = await import('@/api/http')
    const sent: Sent[] = []
    vi.spyOn(http, 'request').mockImplementation((async (config: Sent) => {
      sent.push(config)
      const url = config.url ?? ''
      const method = (config.method ?? 'get').toLowerCase()
      if (url.includes('/games/')) {
        return {
          data: {
            code: 0,
            msg: 'ok',
            data: { items: [{ id: 2, name: '原神' }], total: 1, page: 1, page_size: 200 },
          },
        }
      }
      if (url.includes('/auth/me')) {
        // `fetchGames()` 会读 `/api/auth/me` 的 `user.game_ids`；本组游戏字典只有 id=2 ⇒ 权限钉字面 [2]。
        return {
          data: {
            code: 0,
            msg: 'ok',
            data: {
              user: {
                username: 'u',
                display_name: 'u',
                game_ids: [2],
                menu_permissions: ME_PERMISSIONS_ALL,
              },
            },
          },
        }
      }
      if (url.includes('/orders/') && method === 'post') {
        return { data: { code: 0, msg: 'ok', data: { order_no: '20260921000000000009' } } }
      }
      return {
        data: { code: 0, msg: 'ok', data: { items: [], total: 0, page: 1, page_size: 200 } },
      }
    }) as never)

    const root = document.createElement('div')
    document.body.appendChild(root)
    await mountOrderAddOrder(root, { onNavigate: vi.fn() })
    return { sent, root }
  }

  /**
   * 点保存并等 `onSave` 的异步链走完。
   * ⚠️ 与「订单总价必填」那组的同名助手不同：这里**不**代填 `#project`——
   * 项目文本正是本组的被测对象，由每个用例在点保存**之前**自己填。
   */
  async function clickSave(): Promise<void> {
    const save = LAYUI.handlers.get('submit(savebtn)')
    expect(save).toBeTypeOf('function')
    LAYUI.msg.mockClear()
    save!()
    await new Promise((resolve) => setTimeout(resolve, 0))
  }

  const countPosts = (sent: Sent[]): number =>
    sent.filter((c) => (c.method ?? 'get').toLowerCase() === 'post').length

  it('判据直测：恰好 700 字节 ⇒ 空串（≤700 放行）', () => {
    // 233 个汉字 = 699 字节 + 1 个 ASCII 字母 = 700 字节（UTF-8，中文一字符 3 字节）
    expect(projectTextSyncLimitError('深'.repeat(233) + 'a')).toBe('')
  })

  it('判据直测：701 字节 ⇒ 逐字文案（文案里 n = 701）', () => {
    expect(projectTextSyncLimitError('深'.repeat(233) + 'ab')).toBe(
      '代练项目超出鸡蛋铺同步上限（当前 701 字节，上限 700 字节），请精简后再保存；完整细节可放订单截图',
    )
  })

  it('新建态：`#project` 填 750 字节（250 个汉字）点保存 ⇒ 出逐字文案，且一个建单请求都没发', async () => {
    const { sent, root } = await mountCreate()
    root.querySelector<HTMLInputElement>('#project')!.value = '深'.repeat(250)
    await clickSave()

    expect(LAYUI.msg).toHaveBeenCalledWith(
      '代练项目超出鸡蛋铺同步上限（当前 750 字节，上限 700 字节），请精简后再保存；完整细节可放订单截图',
      { time: 1500 },
    )
    // 首屏那几条 GET 不算；闸门挡在**出口之前** ⇒ POST 数为 0
    expect(countPosts(sent)).toBe(0)
  })

  it('边界左：恰好 700 字节**放行**（不出上限文案，POST 真的发出且载荷带原文）', async () => {
    const { sent, root } = await mountCreate()
    // 233 个汉字（699 B）+ 1 个 ASCII 字母 = 700 B——700 是「≤700 放行」的左边界
    root.querySelector<HTMLInputElement>('#project')!.value = '深'.repeat(233) + 'a'
    await clickSave()

    expect(LAYUI.msg).not.toHaveBeenCalledWith(
      '代练项目超出鸡蛋铺同步上限（当前 700 字节，上限 700 字节），请精简后再保存；完整细节可放订单截图',
      { time: 1500 },
    )
    const post = sent.find((c) => (c.method ?? '').toLowerCase() === 'post')
    expect(post).toBeTruthy()
    expect(post!.data).toMatchObject({ project_text: '深'.repeat(233) + 'a' })
  })
})

/**
 * 录单页读平台订单（spec §2.5 的表 / §7.2）。
 *
 * 三个触发点：① `#souresOrderID` 的 `change`；
 * ② 订单来源下拉的 `change`（**带新来源值**）；
 * ③ 粘贴解析回填出 `source_order_no` 之后。
 *
 * 后端端点 = `POST /api/business/read-platform-order/`，
 * 出参 `data` 六个字段，其中 `payment` 已是**元**的两位小数字符串（D6：后端算，前端不换算）。
 */
describe('录单页读平台订单：三个触发点 + 门槛 + 静默失败 + 「仅当为空」的回填', () => {
  type Sent = {
    method?: string
    url?: string
    data?: Record<string, unknown>
    params?: Record<string, unknown>
  }

  const SOURCE_ROWS = [
    {
      id: 20,
      name: '闲鱼A',
      remark: '',
      order_by: 0,
      shop_type: 20,
      created_at: '',
      updated_at: '',
    },
    {
      id: 11,
      name: '淘宝店',
      remark: '',
      order_by: 0,
      shop_type: 0,
      created_at: '',
      updated_at: '',
    },
  ]

  /**
   * 平台订单的六个字段（读单端点的出口）。
   *
   * `payment` 取 `'0.01'`（= 上游 1 分）是**刻意**的：它是「后端 `Decimal` 除过 100、前端不再换算」
   * 这条口径的**判别值**——前端若又乘/除一次 100，`#price` 就不是 `0.01` 了。
   */
  const PLATFORM_FIELDS = {
    payment: '0.01',
    buyer_nick: '闲鱼买家',
    customer_phone: '13800138000',
    remark: '客户要双排上分',
    order_status: 1,
    pay_time: 1758384000,
    // per-source spec §5：后端判定出的来源 + 平台。⚠️ 三个字段都在，回填那条路才被驱动到。
    order_source: 20,
    order_source_name: '闲鱼A',
    platform: 'xgj',
  }

  /** 粘贴解析的默认返回：**回填出来源单号**（触发点 ③ 的前提）。 */
  const PASTE_TID = '123456789012345'

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
    window.history.replaceState({}, '', '/')
    LAYUI.msg.mockClear()
    LAYUI.handlers.clear()
    LAYUI.fsReset()
  })

  /**
   * 本 describe 的挂载会经 `initFormSelects('select[xm-select]')` 打一条**既有**告警
   * （`src/layui/vendor.ts:117`：jsdom 里 vendored formSelects 没加载）。它与本组用例无关，
   * 但每条新用例都会往 stderr 里多印一行。这里**只**吃掉那一条（判据 = 该条消息的**前缀**
   * `'[layui/vendor] formSelects 尚未就绪'`，用 `startsWith` 而不是 `includes`：后者连
   * 「别的告警正文里恰好含这串」也会被吞掉），
   * 其它告警照原样打出去——不做「静默所有 console.warn」那种会掩盖新问题的做法。
   */
  beforeEach(() => {
    const realWarn = console.warn
    vi.spyOn(console, 'warn').mockImplementation((...args: unknown[]) => {
      if (String(args[0]).startsWith('[layui/vendor] formSelects 尚未就绪')) return
      realWarn(...args)
    })
  })

  /**
   * 挂载**新增**态（照上面两个 describe 的样板）→ 返回「已发出的请求」与 root。
   *
   * `platform: null` ⇒ 读单回**业务失败**（HTTP 200 + `code 1017`）——
   * `request()` 会把它抛成 `BizError`，正是页面 catch 分支要吃的那一种。
   * `platformBrokenShape` ⇒ 读单**成功但 `data` 是 `null`**：`request()` 不抛 `BizError`，
   * 页面读 `fields.payment` 时抛 `TypeError`——用来分辨「静默只覆盖业务失败」这条判据。
   * `parseFields` 覆盖粘贴解析回填的字段（触发点 ③ 用）。
   */
  async function mountCreate(options?: {
    platform?: Record<string, unknown> | null
    platformBrokenShape?: boolean
    /** 让读单端点回一个**指定业务码**（用来测 1015 与 1016/1017 的提示差异）。 */
    readError?: { code: number; msg: string }
    /** 粘贴解析回填的字段；传函数可按次变化（触发点 ③ 的第二次粘贴要换个返回）。 */
    parseFields?: Record<string, string> | (() => Record<string, string>)
    /**
     * 覆盖游戏字典（`/games/` 的 `items`）——自动切游戏用例要 **≥2 款**
     * 才有判别力（见下面 `AUTO_GAMES`）。不传时与既有用例逐字相同（只有 id=2 的「原神」）。
     * ⚠️ `/auth/me` 的 `game_ids` 必须与它**同源**：`fetchGames()` 按 `game_ids` 收窄，
     * 两处不一致时多出来的游戏会被静默过滤掉，用例就会以「认不出游戏」的假象红。
     */
    games?: { id: number; name: string }[]
    /**
     * 覆盖来源字典的返回行。空数组 =「来源字典为空」那一档（「没选来源也发请求」
     * 与 `S4` 用例要用它——首屏默认选中第一条之后，只有字典为空才构造得出
     * 「没有来源」）。
     */
    sourceRows?: typeof SOURCE_ROWS
  }): Promise<{ sent: Sent[]; root: HTMLElement }> {
    const gameRows = options?.games ?? [{ id: 2, name: '原神' }]
    window.history.replaceState({}, '', '/?page=order-addorder')
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: unknown) => {
        if (String(url).includes('order-addorder.html')) {
          return { ok: true, status: 200, text: async () => EVIDENCE }
        }
        throw new Error(`未预期的 fetch：${String(url)}`)
      }),
    )
    const { http } = await import('@/api/http')
    const sent: Sent[] = []
    vi.spyOn(http, 'request').mockImplementation((async (config: Sent) => {
      sent.push(config)
      const url = config.url ?? ''
      const method = (config.method ?? 'get').toLowerCase()
      if (url.includes('read-platform-order')) {
        if (options?.readError) {
          return {
            data: { code: options.readError.code, msg: options.readError.msg, data: null },
          }
        }
        if (options?.platform === null) {
          return { data: { code: 1017, msg: '平台订单读取失败', data: null } }
        }
        if (options?.platformBrokenShape === true) {
          // 信封合法（`code 0` ⇒ `unwrap` 不抛 `BizError`），但 `data` 不是那六个字段的形状
          return { data: { code: 0, msg: 'ok', data: null } }
        }
        return {
          data: { code: 0, msg: 'ok', data: { ...PLATFORM_FIELDS, ...(options?.platform ?? {}) } },
        }
      }
      if (url.includes('/order-sources/')) {
        const rows = options?.sourceRows ?? SOURCE_ROWS
        return {
          data: {
            code: 0,
            msg: 'ok',
            data: { items: rows, total: rows.length, page: 1, page_size: 200 },
          },
        }
      }
      if (url.includes('/games/')) {
        return {
          data: {
            code: 0,
            msg: 'ok',
            data: { items: gameRows, total: gameRows.length, page: 1, page_size: 200 },
          },
        }
      }
      if (url.includes('/auth/me')) {
        // `fetchGames()` 会读 `/api/auth/me` 的 `user.game_ids` 把字典**收窄**
        // ⇒ 这里必须与 `/games/` 那一支是**同一份**（`gameRows`），否则多出来的那几款会被静默过滤掉。
        return {
          data: {
            code: 0,
            msg: 'ok',
            data: {
              user: {
                username: 'u',
                display_name: 'u',
                game_ids: gameRows.map((g) => g.id),
                menu_permissions: ME_PERMISSIONS_ALL,
              },
            },
          },
        }
      }
      if (url.includes('/orders/parse/')) {
        return {
          data: {
            code: 0,
            msg: 'ok',
            data: {
              fields:
                typeof options?.parseFields === 'function'
                  ? options.parseFields()
                  : (options?.parseFields ?? { source_order_no: PASTE_TID }),
              matched: [],
              unmatched: [],
            },
          },
        }
      }
      if (url.includes('/orders/') && method === 'post') {
        return { data: { code: 0, msg: 'ok', data: { order_no: '20260921000000000009' } } }
      }
      return {
        data: { code: 0, msg: 'ok', data: { items: [], total: 0, page: 1, page_size: 200 } },
      }
    }) as never)

    const root = document.createElement('div')
    document.body.appendChild(root)
    await mountOrderAddOrder(root, { onNavigate: vi.fn() })
    return { sent, root }
  }

  const readCalls = (sent: Sent[]): Sent[] =>
    sent.filter((c) => (c.url ?? '').includes('read-platform-order'))

  /** 走**真实 DOM 事件**改来源单号（触发点 ① 挂的就是 `change`）。 */
  function typeSourceOrderNo(root: HTMLElement, tid: string): void {
    const el = root.querySelector<HTMLInputElement>('#souresOrderID')!
    el.value = tid
    el.dispatchEvent(new Event('change'))
  }

  /** 走**真实 DOM 事件**把一段文字粘进粘贴框并失焦（`onPasteBlur` 挂的是 `blur`）。 */
  function pasteAndBlur(root: HTMLElement, text: string): void {
    const box = root.querySelector<HTMLTextAreaElement>('#dlcontent')!
    box.value = text
    box.dispatchEvent(new Event('blur'))
  }

  /** 让 `readPlatformOrderIntoForm` 的 `await` 链走完（`parsePastedText` 那条要两轮）。 */
  async function settle(): Promise<void> {
    await new Promise((resolve) => setTimeout(resolve, 0))
    await new Promise((resolve) => setTimeout(resolve, 0))
  }

  it('触发点 ①：填了 ≥15 位的来源单号后（change），自动带出价格与买家昵称', async () => {
    const { sent, root } = await mountCreate()
    LAYUI.fsPick('orderSoures', 20)
    typeSourceOrderNo(root, '123456789012345')
    await settle()

    const calls = readCalls(sent)
    expect(calls).toHaveLength(1)
    expect((calls[0]!.method ?? '').toLowerCase()).toBe('post')
    expect(calls[0]!.url).toBe('/api/business/read-platform-order/')
    // 请求体：`order_source` 必须是**数字 id**（不是来源名字），单号原样
    expect(calls[0]!.data).toEqual({ order_source: 20, tid: '123456789012345' })
    // 金额是后端换算好的**元**字符串 —— 前端不再做任何换算（'0.01' 而不是 '1'）
    expect(root.querySelector<HTMLInputElement>('#price')!.value).toBe('0.01')
    expect(root.querySelector<HTMLInputElement>('#ww')!.value).toBe('闲鱼买家')
    // 电话仅当平台侧非空；商品标题不回填需求（2026-11-05 拍板：随单保存打手可见）⇒ 需求保持为空
    expect(root.querySelector<HTMLInputElement>('#moblie')!.value).toBe('13800138000')
    expect(root.querySelector<HTMLTextAreaElement>('#dlcontent')!.value).toBe('')
  })

  it('单号不足 15 位时一次请求都不发（对标 `orderid.length >= 15`）', async () => {
    const { sent, root } = await mountCreate()
    LAYUI.fsPick('orderSoures', 20)
    // 14 位：差一位就到门槛
    typeSourceOrderNo(root, '12345678901234')
    await settle()
    expect(readCalls(sent)).toHaveLength(0)
    // 价格保持模板默认值，说明连回填都没发生
    expect(root.querySelector<HTMLInputElement>('#price')!.value).toBe('0')
  })

  it('没选来源**也发请求**（后端按单号自动判定），请求体里**不带** `order_source` 键', async () => {
    // ⚠️ 判据是**键不存在**，不是 `order_source: null`：后端两种都当"没给"，
    //    但"没给"这个语义只在键缺失时是**唯一**的（少一条能漂移的路径）。
    //
    // ⚠️ **挂载参数（`D-0178`）**：新建态首屏**默认选中第一条来源**
    //（spec §3.3）⇒「没选来源」这一档**不再能靠"首屏什么都不选"构造**（那样会静默带上第一条）。
    // 唯一还能构造它的形态是**来源字典为空**（`S4` 那一档），所以这里改传 `sourceRows: []`。
    // ⛔ 断言一个字没放宽，仍然是 `toEqual({ tid })` —— 变的只是夹具。
    const { sent, root } = await mountCreate({ sourceRows: [] })
    typeSourceOrderNo(root, '123456789012345')
    await settle()

    const calls = readCalls(sent)
    expect(calls).toHaveLength(1)
    expect(calls[0]!.data).toEqual({ tid: '123456789012345' })
    expect(calls[0]!.data).not.toHaveProperty('order_source')
    // 判别力：这一单**真的读到了**（价格被平台值填上），否则"发了请求"可能只是别的原因
    expect(root.querySelector<HTMLInputElement>('#price')!.value).toBe('0.01')
  })

  /**
   * **F1–F4：读单不得把「首屏默认第一条」当成客服指定的来源**（D-0178 上线时的实测回归）。
   *
   * 事故形态：读单请求把那个**默认值**当成了「客服指定的来源」发出去 ⇒ 后端**只在没给来源时
   * 才走**的自动判定被顶掉 ⇒ 客服输来源单号**什么都不出来**，连带「按商品标题自动选游戏」
   * 也没得可认。
   *
   * 判据打在两处**真实出口**上：`http.request` 真发出去的载荷 + 下拉两条腿的 DOM。
   */
  it('F1：只有**默认第一条**来源时，读单**不带** `order_source`（带了就等于顶掉后端自动判定）', async () => {
    // 后端判出来的店与「第一条」**故意不同**（11 vs 默认的 20）：否则「回填有没有真的发生」是恒真的
    const { sent, root } = await mountCreate({
      platform: { order_source: 11, order_source_name: '淘宝店' },
    })
    // 前置自证：首屏确实把第一条选中了 —— 否则这条用例测的不是「默认值」那一档
    expect(root.querySelector<HTMLSelectElement>('#orderSoures')!.value).toBe('20')
    typeSourceOrderNo(root, '123456789012345')
    await settle()

    const calls = readCalls(sent)
    expect(calls).toHaveLength(1)
    // ⛔ 逐字 `{ tid }`：多一个 `order_source: 20` 就是这次事故的形状
    expect(calls[0]!.data).toEqual({ tid: '123456789012345' })
    // 判别力：这一单**真的读到了**（价格被平台值填上）＋ 后端判出的来源**真的回填了**两条腿
    expect(root.querySelector<HTMLInputElement>('#price')!.value).toBe('0.01')
    expect(root.querySelector<HTMLSelectElement>('#orderSoures')!.value).toBe('11')
    expect(LAYUI.fsValues('orderSoures')).toEqual([11])
  })

  it('F2：客服**自己选过**来源 ⇒ 读单**带上**他选的那条（不抢客服的决定）', async () => {
    const { sent, root } = await mountCreate()
    LAYUI.fsPick('orderSoures', 11)
    typeSourceOrderNo(root, '123456789012345')
    await settle()
    expect(readCalls(sent)[0]!.data).toEqual({ order_source: 11, tid: '123456789012345' })
  })

  it('F3：自动判定回填之后再换一个单号 ⇒ **仍然不带**来源（每次都重新判定，业务方 2026-10-03 裁定）', async () => {
    const { sent, root } = await mountCreate({
      platform: { order_source: 11, order_source_name: '淘宝店' },
    })
    typeSourceOrderNo(root, '123456789012345')
    await settle()
    expect(root.querySelector<HTMLSelectElement>('#orderSoures')!.value).toBe('11')

    typeSourceOrderNo(root, '987654321098765')
    await settle()

    const calls = readCalls(sent)
    expect(calls).toHaveLength(2)
    // 第二发**仍然不带**来源：第一发回填的那个 11 是「后端判的」，不是「客服选的」
    expect(calls[1]!.data).toEqual({ tid: '987654321098765' })
  })

  it('F4：客服选过的来源**不会被自动判定的回填降级成「自动」**（手选来源那档靠它）', async () => {
    // 后端**原样回**我们发过去的 11（真实后端在 `_read_by_source` 里就是这么回的）
    const { sent, root } = await mountCreate({
      platform: { order_source: 11, order_source_name: '淘宝店' },
    })
    LAYUI.fsPick('orderSoures', 11)
    typeSourceOrderNo(root, '123456789012345')
    await settle()
    typeSourceOrderNo(root, '987654321098765')
    await settle()

    const calls = readCalls(sent)
    expect(calls).toHaveLength(2)
    // 两发都按客服选的那条走。回填若把 `orderSourceIsAuto` 顶成 `true`，第二发就会变成「不带来源」
    // ⇒ 客服为「不在自动判定范围内」的店铺手选的来源，下一次读单就丢了。
    expect(calls[0]!.data).toEqual({ order_source: 11, tid: '123456789012345' })
    expect(calls[1]!.data).toEqual({ order_source: 11, tid: '987654321098765' })
  })

  it('自动判定回来的来源要**回填下拉**（真插件的选中值 + 隐藏 select 两条腿）', async () => {
    const { root } = await mountCreate()
    typeSourceOrderNo(root, '123456789012345')
    await settle()
    // 两条腿都要：`readOrderSource` 先读隐藏 `<select>`、为空才回落插件；
    // 只写一条的话另一条读出来是 `null` ⇒ 订单**静默存成"没有来源"**。
    expect(LAYUI.fsValues('orderSoures')).toEqual([20])
    expect(root.querySelector<HTMLSelectElement>('#orderSoures')!.value).toBe('20')
  })

  it('自动判定出的来源**不在首屏字典里**时先补进下拉（它是刚建的，R4）', async () => {
    // 现实成因：后端自动判定命中一家还没建过来源的店 ⇒ **当场建一个**（per-source spec R4）
    // ⇒ 那个 id 不在首屏 `fetchOrderSources()` 的结果里。不补进字典的话，两条回填腿都会
    // **静默丢掉**它（真插件只收录 `dd[lay-value]` 命得中的 id；隐藏 select 无对应 option 时
    // `value = id` 落成 `''`）。
    const { root } = await mountCreate({
      platform: { order_source: 999, order_source_name: '刚建的新店' },
    })
    typeSourceOrderNo(root, '123456789012345')
    await settle()

    expect(LAYUI.fsValues('orderSoures')).toEqual([999])
    const select = root.querySelector<HTMLSelectElement>('#orderSoures')!
    expect(select.value).toBe('999')
    expect(Array.from(select.options).map((o) => o.textContent)).toContain('刚建的新店')
  })

  it('`sourceOverride === null`（用户刚清空来源）**仍然不发**（E-0021 的既有约定）', async () => {
    // ⚠️ `null` 与 `undefined` 不是一回事：`undefined` = "没传，去看 DOM"（可以自动判定），
    // `null` = "用户刚把它清空了" —— 那时再发一次请求会把刚清掉的来源又填回来。
    // 把两者抹平成 `== null` 会让这条红。
    const { sent, root } = await mountCreate()
    LAYUI.fsPick('orderSoures', 20)
    typeSourceOrderNo(root, '123456789012345')
    await settle()
    sent.length = 0

    LAYUI.fsClear('orderSoures') // radio 的「清空」快捷按钮 ⇒ 回调带**空集**
    await settle()
    expect(readCalls(sent)).toHaveLength(0)
  })

  it('自动判定没命中（1015）⇒ **必须把后端的「请手动选择订单来源」说出来**', async () => {
    // ⚠️ `1015` 的后端 msg 是给客服的**操作指引**（自动判定的最常见结局）：
    //    静默吞掉 = 客服填了单号、什么都没发生、也不知道"该手动选来源"。
    const { root } = await mountCreate({
      readError: { code: 1015, msg: '没能识别这个订单号属于哪家店铺，请手动选择订单来源' },
    })
    LAYUI.msg.mockClear()
    typeSourceOrderNo(root, '123456789012345')
    await settle()
    // ⚠️ 真实 `islandMsg` 会再带一个 `{time: …}` 选项参数 ⇒ 用 `expect.anything()` 收第二个位；
    //    只写一个参数会**假红**（实测踩过：msg 明明被调用了，断言却说不匹配）。
    expect(LAYUI.msg).toHaveBeenCalledWith(
      expect.stringContaining('请手动选择订单来源'), expect.anything(),
    )
  })

  it('上游故障（1016/1017）仍然静默——那是**设计好的**降级，与 1015/1028 分开判', async () => {
    // 判别力：把开口面写成"所有 BizError 都提示" ⇒ 这条红（两个子例各自红一次）。
    // ⚠️ 两个码都测：判据是「**恰好** 1015 ∪ 1028 开口」，只测 1016 会让"把 1017 也放进去"漏网。
    for (const code of [1016, 1017]) {
      const { root } = await mountCreate({
        readError: { code, msg: code === 1016 ? 'AppKey / AppSecret 无效' : '平台订单读取失败' },
      })
      LAYUI.msg.mockClear()
      typeSourceOrderNo(root, '123456789012345')
      await settle()
      expect(LAYUI.msg, `code=${code} 不该弹提示`).not.toHaveBeenCalled()
    }
  })

  it('待付款被拒（1028）⇒ **必须把后端那句话说出来**（它就是给录单的人看的）', async () => {
    // ⚠️ 判据打在**真实出口**上：弹层出口的文本必须**就是后端那句 `msg`**（逐字，不是包含关系）
    //    ——少开口时客服看到的页面与「坏了」不可区分。
    const refused =
      '这张闲管家订单还是「待付款」，已拒绝识别；买家付款后再读一次。' +
      '若长期不需要这个限制，请联系管理员到「闲管家店铺对接」页关掉'
    const { root } = await mountCreate({ readError: { code: 1028, msg: refused } })
    LAYUI.msg.mockClear()
    typeSourceOrderNo(root, '123456789012345')
    await settle()
    expect(LAYUI.msg).toHaveBeenCalledWith(refused, expect.anything())
  })

  it('读单失败的「开口面」**恰好**是 {1015, 1028}（加码忘了开口 ⇒ 这条红）', () => {
    // ⚠️ 为什么要有这条结构断言：读单失败默认静默，「谁该开口」只能靠 `SPEAKING_READ_ORDER_CODES`
    //    一处判据；散了就会漏开口（后端加码、前端没人跟着开口）。
    //    ⛔ 不许放宽成 `toContain`：多一个码（把上游故障也弹给客服）与少一个码都是缺陷。
    //    取值当场写数字（钉**值**不钉常量）：`1015` = 没配对 / 认不出店；`1028` = 上游订单状态不允许录单。
    expect([...SPEAKING_READ_ORDER_CODES].sort((a, b) => a - b)).toEqual([1015, 1028])
  })

  it('读不到时静默——不弹错，且价格恢复可编辑（对标 `code <= 0` 分支）', async () => {
    const { sent, root } = await mountCreate({ platform: null })
    const price = root.querySelector<HTMLInputElement>('#price')!
    // 模拟「上一次成功读单把价格锁上了」
    price.readOnly = true
    LAYUI.msg.mockClear()
    // **对照**：业务失败（`BizError`）是**设计好的**降级 ⇒ 不该进 `console.error`
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})

    LAYUI.fsPick('orderSoures', 20)
    typeSourceOrderNo(root, '123456789012345')
    await settle()

    expect(readCalls(sent)).toHaveLength(1)
    expect(LAYUI.msg).not.toHaveBeenCalled()
    expect(price.readOnly).toBe(false)
    // 失败不改任何回填值（价格仍是模板默认的 0，不会被写成空串或别的）
    expect(price.value).toBe('0')
    expect(error).not.toHaveBeenCalled()
  })

  it('非业务错误（形状坏了）用户侧仍静默，但**必须上报**——不把编程错误伪装成「读不到」', async () => {
    const { sent, root } = await mountCreate({ platformBrokenShape: true })
    const price = root.querySelector<HTMLInputElement>('#price')!
    price.readOnly = true
    LAYUI.msg.mockClear()
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})

    LAYUI.fsPick('orderSoures', 20)
    typeSourceOrderNo(root, '123456789012345')
    await settle()

    // ① 用户可见行为与业务失败**完全一致**：静默、价格解锁
    expect(readCalls(sent)).toHaveLength(1)
    expect(LAYUI.msg).not.toHaveBeenCalled()
    expect(price.readOnly).toBe(false)
    // ② 但错误本身不许消失（`data: null` ⇒ 读 `fields.payment` 抛 `TypeError`）
    expect(error).toHaveBeenCalledTimes(1)
    expect(error.mock.calls[0]![1]).toBeInstanceOf(TypeError)
  })

  it('买家昵称只在 #ww 为空时覆盖（已填的值不被平台值冲掉）', async () => {
    const { root } = await mountCreate()
    const ww = root.querySelector<HTMLInputElement>('#ww')!
    ww.value = '客服手填的昵称'
    LAYUI.fsPick('orderSoures', 20)
    typeSourceOrderNo(root, '123456789012345')
    await settle()
    expect(ww.value).toBe('客服手填的昵称')
    // 判别力：这一单**确实读到了**（价格被填），否则「没被覆盖」也可能只是压根没发请求
    expect(root.querySelector<HTMLInputElement>('#price')!.value).toBe('0.01')
  })

  it('电话只在平台侧非空时填（空串不覆盖客服已填的号码）', async () => {
    const { root } = await mountCreate({ platform: { customer_phone: '' } })
    const mobile = root.querySelector<HTMLInputElement>('#moblie')!
    mobile.value = '13900139000'
    LAYUI.fsPick('orderSoures', 20)
    typeSourceOrderNo(root, '123456789012345')
    await settle()
    expect(root.querySelector<HTMLInputElement>('#price')!.value).toBe('0.01')
    expect(mobile.value).toBe('13900139000')
  })

  it('价格只在平台侧非空时写：`payment` 空串不覆盖客服已填的价格，而 `0.00` 照写', async () => {
    // ① `payment: ''` 是后端在**上游没有价格**时的缺省形状（合法契约：回空串 + `code 0`；
    //    `apps/business/api/platform_order_views.py:193-194`）。无条件写会把客服已填的价格**清空**。
    //    ⚠️ **本页不能静默录出「空总价」单**（D-0082）：新建态有「订单总价必填」闸
    //    （`totalPriceRequiredError`），空总价**提交不出去**。
    //    但这条链**在别处仍然成立**——`formToPayload` 仍会丢掉空串键
    //    （`views/orders/orderFormFields.ts:285`）、后端仍落 `0.00`（`total_price` 是 `required=False` +
    //    模型 `default=Decimal("0.00")`，`api/serializers.py:294-295`；外部脚本 / 平台拉单路径不受本页闸门约束），
    //    而且「不填的人会留下默认的 `0`」——所以「0 元单」依旧合法存在，只是从「静默的 0」变成「显式看见的 0」。
    //    ⚠️ 「退不掉」是**附加条件**、别写成必然：退单闸门 `_gross_received`（`refunds.py`）读
    //    `received_price`（接单实收），为空才回落**订单总价 `total_price`**（D-0081 起；原来回落的是接单价
    //    `hall_price`）⇒ 总价 0 元但接单实收有钱的单**不会被这道闸拒**；而**总价也是 0** 时毛收款就是 `0.00`
    //    （可退额为 0）。**「两处价都空」这个失败形态已不存在**（`total_price` 非空、默认 0）。
    const first = await mountCreate({ platform: { payment: '' } })
    const price = first.root.querySelector<HTMLInputElement>('#price')!
    price.value = '88.50'
    LAYUI.fsPick('orderSoures', 20)
    typeSourceOrderNo(first.root, '123456789012345')
    await settle()
    // 判别力：这一单**确实读到了**（昵称被平台值填上），否则「没被清空」也可能只是压根没发请求
    expect(first.root.querySelector<HTMLInputElement>('#ww')!.value).toBe('闲鱼买家')
    expect(price.value).toBe('88.50')

    // ② `'0.00'` 是**有效金额**（0 元单合法），必须照写。⚠️ 这一条防的是「先转数字再判真假」
    //    那一类（`if (Number(fields.payment))` ⇒ `Number('0.00') === 0` ⇒ 假 ⇒ 不写 ⇒ 本断言红）；
    //    `if (fields.payment)` 今天分不出这两支——`'0.00'` 在 JS 里是非空字符串、真值为真。
    //    E-0021 的 Python 版（`bool(Decimal("0.00")) is False`）只有**转成数字之后**才在 JS 同型。
    const second = await mountCreate({ platform: { payment: '0.00' } })
    const zero = second.root.querySelector<HTMLInputElement>('#price')!
    zero.value = '88.50'
    LAYUI.fsPick('orderSoures', 20)
    typeSourceOrderNo(second.root, '123456789012345')
    await settle()
    expect(zero.value).toBe('0.00')
  })

  it('需求只在为空或长度 <2 时填；平台侧没有需求时不会把已敲的字抹掉', async () => {
    // ① 已有 ≥2 个字 ⇒ 不覆盖
    const first = await mountCreate()
    const box = first.root.querySelector<HTMLTextAreaElement>('#dlcontent')!
    box.value = '客户原话'
    LAYUI.fsPick('orderSoures', 20)
    typeSourceOrderNo(first.root, '123456789012345')
    await settle()
    expect(box.value).toBe('客户原话')

    // ② 只有 1 个字、而平台侧 remark 为空 ⇒ 也不该被写成空串
    const second = await mountCreate({ platform: { remark: '' } })
    const short = second.root.querySelector<HTMLTextAreaElement>('#dlcontent')!
    short.value = '急'
    LAYUI.fsPick('orderSoures', 20)
    typeSourceOrderNo(second.root, '123456789012345')
    await settle()
    expect(short.value).toBe('急')
  })

  it('触发点 ②：订单来源改变也会触发读单，并把**新来源 id** 传进去', async () => {
    const { sent, root } = await mountCreate()
    // 先选来源（此时单号还是空 ⇒ 不发请求），再填单号 → 触发点 ①
    LAYUI.fsPick('orderSoures', 20)
    typeSourceOrderNo(root, '123456789012345')
    await settle()
    expect(readCalls(sent)).toHaveLength(1)
    sent.length = 0 // 清掉触发点 ① 那一次，下面只看来源改变这一下

    // 走**插件的事件契约**改选（与上面 describe 同一手法）：真人点选 → `endOn` 带**改后**值
    LAYUI.fsPick('orderSoures', 11)
    await settle()

    const calls = readCalls(sent)
    expect(calls).toHaveLength(1)
    expect(calls[0]!.data).toEqual({ order_source: 11, tid: '123456789012345' })
    expect(typeof calls[0]!.data!.order_source).toBe('number')
  })

  it('触发点 ③：粘贴解析回填出来源单号之后，再读一次', async () => {
    const { sent, root } = await mountCreate()
    // 先选来源：此刻单号为空 ⇒ 触发点 ② 不发请求
    LAYUI.fsPick('orderSoures', 20)
    await settle()
    expect(readCalls(sent)).toHaveLength(0)

    pasteAndBlur(root, '账号：acc001')
    await settle()

    const calls = readCalls(sent)
    expect(calls).toHaveLength(1)
    expect(calls[0]!.data).toEqual({ order_source: 20, tid: PASTE_TID })
    expect(root.querySelector<HTMLInputElement>('#price')!.value).toBe('0.01')
  })

  it('触发点 ③ 只在**这次粘贴回填出**来源单号时读；已存在的单号不会因再粘一次而重读', async () => {
    // 粘贴解析的返回按次可变：第一次回填出来源单号，第二次不回填。
    let nextFields: Record<string, string> = { source_order_no: PASTE_TID }
    const { sent, root } = await mountCreate({ parseFields: () => nextFields })
    LAYUI.fsPick('orderSoures', 20)
    await settle()
    expect(readCalls(sent)).toHaveLength(0)

    // 第一次粘贴：回填出来源单号 ⇒ 触发点 ③ 读一次
    pasteAndBlur(root, '账号：acc001')
    await settle()
    expect(readCalls(sent)).toHaveLength(1)
    sent.length = 0

    // 第二次粘贴：**没有**回填出来源单号（`#souresOrderID` 里那 15 位是上一轮留下的）
    // ⇒ 判据若不是「这次 filled 里有 source_order_no」，这里就会白跑一趟上游
    // （读单按次计费，spec §3.5 / D4）。
    nextFields = { game_account: 'acc001' }
    pasteAndBlur(root, '再来一段')
    await settle()
    expect(readCalls(sent)).toHaveLength(0)
  })

  /**
   * T8：`/parse/` 新回的 5 个键在**页面级**真的落地。
   *
   * 判别力：`onPasteBlur` 不把 `sourceOptions` 传进 `applyPasteToDom` ⇒ 来源永远进
   * `unresolved`、两条腿都是空；不调 `setOrderSourceSelection` ⇒ 真 formSelects 与隐藏
   * `<select>` 都读不到（`writeFormState` **覆盖不到**运行期注入的下拉）。
   */
  it('T8：粘贴回填 5 个新键——4 个 scalar 落 DOM，来源命中同步 xm-select 两条腿', async () => {
    const { sent, root } = await mountCreate({
      parseFields: {
        current_info_1: '合成当前信息一',
        current_info_2: '合成当前信息二',
        customer_ww: '合成旺旺昵称',
        source_order_no: PASTE_TID,
        source: '闲鱼A',
      },
    })
    pasteAndBlur(root, '一段合成文本')
    await settle()

    // ① 四个 scalar：模型改完必须推回 DOM（提交时 `readFormState` 从 DOM 重读）
    expect(root.querySelector<HTMLInputElement>('[name="gaemTf"]')!.value).toBe('合成当前信息一')
    expect(root.querySelector<HTMLInputElement>('[name="gameSQ"]')!.value).toBe('合成当前信息二')
    expect(root.querySelector<HTMLInputElement>('#ww')!.value).toBe('合成旺旺昵称')
    expect(root.querySelector<HTMLInputElement>('#souresOrderID')!.value).toBe(PASTE_TID)

    // ② 来源名 '闲鱼A' ⇒ 字典 id 20：两条读取腿（插件 + 隐藏 select）都要有值
    expect(LAYUI.fsValues('orderSoures')).toEqual([20])
    expect(root.querySelector<HTMLSelectElement>('#orderSoures')!.value).toBe('20')

    // ③ 同一次粘贴点亮触发点 ③：**真发了**一次读平台订单，且带刚回填出的来源与单号
    const calls = readCalls(sent)
    expect(calls).toHaveLength(1)
    expect(calls[0]!.data).toEqual({ order_source: 20, tid: PASTE_TID })
  })

  it('T8：粘贴里的来源名对不上字典 ⇒ 不静默（提示「订单来源「X」」）且不改动已选来源', async () => {
    const { root } = await mountCreate({ parseFields: { source: '字典里没有的合成来源' } })
    LAYUI.fsPick('orderSoures', 11)
    LAYUI.msg.mockClear()

    pasteAndBlur(root, '一段合成文本')
    await settle()

    expect(LAYUI.msg).toHaveBeenCalledWith(
      expect.stringContaining('订单来源「字典里没有的合成来源」'),
      expect.anything(),
    )
    // 未命中 ⇒ 不写 order_source：用户已选的 11 原样保留（提示已告知客服手动选）
    expect(LAYUI.fsValues('orderSoures')).toEqual([11])
    expect(root.querySelector<HTMLSelectElement>('#orderSoures')!.value).toBe('11')
  })

  it('T8：触发点 ③ 读单失败也不许炸——业务失败静默降级，坏形状只上报', async () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})

    // ① 业务失败（1017）：静默，价格保持模板默认，不抛
    const failed = await mountCreate({
      platform: null,
      parseFields: { source_order_no: PASTE_TID },
    })
    pasteAndBlur(failed.root, '一段合成文本')
    await settle()
    expect(readCalls(failed.sent)).toHaveLength(1)
    expect(failed.root.querySelector<HTMLInputElement>('#price')!.value).toBe('0')
    expect(error).not.toHaveBeenCalled()

    // ② 坏形状（`code 0` 但 `data: null`）：用户侧仍静默，但编程错误必须上报
    const broken = await mountCreate({
      platformBrokenShape: true,
      parseFields: { source_order_no: PASTE_TID },
    })
    pasteAndBlur(broken.root, '一段合成文本')
    await settle()
    expect(readCalls(broken.sent)).toHaveLength(1)
    expect(error).toHaveBeenCalledTimes(1)
    expect(error.mock.calls[0]![1]).toBeInstanceOf(TypeError)
  })

  it('readPlatformOrder：走既有 `request()`，POST 到读单端点，字段原样透传', async () => {
    const { http } = await import('@/api/http')
    const sent: Sent[] = []
    vi.spyOn(http, 'request').mockImplementation((async (config: Sent) => {
      sent.push(config)
      return { data: { code: 0, msg: 'ok', data: PLATFORM_FIELDS } }
    }) as never)

    const fields = await readPlatformOrder(20, '123456789012345')

    // ⚠️ `sent[0]` 可能是 CSRF 引导那条 GET（写请求在 cookie 缺失时先取一次 token，
    // 见 `http.ts` 的 `refreshCsrfOrThrow`）⇒ 按 URL 挑出读单那一条。
    const call = sent.find((c) => (c.url ?? '').includes('read-platform-order'))
    expect(call).toBeTruthy()
    expect((call!.method ?? '').toLowerCase()).toBe('post')
    expect(call!.url).toBe('/api/business/read-platform-order/')
    expect(call!.data).toEqual({ order_source: 20, tid: '123456789012345' })
    expect(fields).toEqual(PLATFORM_FIELDS)
  })

  it('readPlatformOrder：来源传 `null` ⇒ **不发** `order_source` 键（自动判定的判据）', async () => {
    const { http } = await import('@/api/http')
    const sent: Sent[] = []
    vi.spyOn(http, 'request').mockImplementation((async (config: Sent) => {
      sent.push(config)
      return { data: { code: 0, msg: 'ok', data: PLATFORM_FIELDS } }
    }) as never)

    await readPlatformOrder(null, '123456789012345')

    const call = sent.find((c) => (c.url ?? '').includes('read-platform-order'))
    expect(call!.data).toEqual({ tid: '123456789012345' })
    expect(call!.data).not.toHaveProperty('order_source')
  })

  it('readPlatformOrder：来源 `0` 是**给了的值**，照发（别拿真值判，E-0021）', async () => {
    const { http } = await import('@/api/http')
    const sent: Sent[] = []
    vi.spyOn(http, 'request').mockImplementation((async (config: Sent) => {
      sent.push(config)
      return { data: { code: 0, msg: 'ok', data: PLATFORM_FIELDS } }
    }) as never)

    await readPlatformOrder(0, '123456789012345')

    const call = sent.find((c) => (c.url ?? '').includes('read-platform-order'))
    expect(call!.data).toEqual({ order_source: 0, tid: '123456789012345' })
  })

  /* ══════════════════════════════════════════════════════════════════════════════════════
   * **按内容自动选游戏**（G1–G9）
   * （spec `docs/specs/2026-10-03-addorder-auto-game-and-default-source-design.md`
   *  §3.2；决策 `docs/DECISIONS.md` 的 D-0178）
   *
   * 断言一律打在**真实出口**上：真实 DOM（`#gameName` / 通过页面自己注册的 layui 回调驱动）、
   * `http.request` **真发出去**的配置、`LAYUI.msg` 的真实调用。
   * ⛔ 特别地：**不许只断言 `state` 或只断言区服列表**——「切游戏只调 `onGameChange` 而没写
   * `#gameName`」这个陷阱只有「保存载荷里的 `game`」那一条判据抓得住（`readFormState` 读的是 DOM）。
   * ══════════════════════════════════════════════════════════════════════════════════════ */

  /**
   * 自动切游戏用的游戏字典：**三款**（首屏默认 `games[0]` = `原神` id=2）。
   *
   * 为什么要三款、且名字长度不同：
   *  - `三角洲行动`（5 字）永远是最长命中 ⇒ 只要它出现在文本里就一定命中它；
   *  - 客服手动点的那一款（`和平精英` id=5）与它**不同** ⇒ G3/G9 的「接管后不切」才有判别力
   *    （如果点的是默认那款，`state.game` 相同也会早退，用例会**假绿**）；
   *  - `原神`（2 字）留着当「首屏默认」那一档。
   */
  const AUTO_GAMES = [
    { id: 2, name: '原神' },
    { id: 9, name: '三角洲行动' },
    { id: 5, name: '和平精英' },
  ]

  /**
   * 真点保存并等异步链走完，回**真发出去**的那条**建单**请求。
   * ⚠️ 必须按 URL 挑（`/api/business/orders/`）：本 describe 里 `POST /read-platform-order/`
   * 与 `POST /orders/parse/` 都在这条链**之前**发过，只按方法挑 post 会挑到它们。
   */
  async function saveAndSettle(root: HTMLElement, sent: Sent[]): Promise<Sent> {
    root.querySelector<HTMLInputElement>('#project')!.value = '深渊满星'
    const save = LAYUI.handlers.get('submit(savebtn)')
    expect(save).toBeTypeOf('function')
    save!()
    await settle()
    const post = sent.find(
      (c) => (c.method ?? '').toLowerCase() === 'post' && c.url === '/api/business/orders/',
    )
    expect(post).toBeTruthy()
    return post!
  }

  /** 把 `#gameName` 设成某个 id 并驱动页面**真实注册**的那个回调（= 真人点 `dd` 的等价动作）。 */
  function humanPickGame(root: HTMLElement, id: number): void {
    // 真人点 `dd` 时 layui 自己会写这个下拉，再派发 `select(selectGame)`
    //（`public/layui/layui.js` 偏移 263498）⇒ 测试要**同时**做这两件事，否则模拟出来的
    //「真人点过」在 DOM 上是不完整的。
    root.querySelector<HTMLSelectElement>('#gameName')!.value = String(id)
    const cb = LAYUI.handlers.get('select(selectGame)')
    expect(cb).toBeTypeOf('function')
    cb!({ value: String(id) })
  }

  it('G1：读单的商品标题里出现某款游戏 ⇒ 自动切到它（下拉 / 提交载荷 / 提示三处都对）', async () => {
    const { sent, root } = await mountCreate({
      games: AUTO_GAMES,
      platform: { remark: '合成商品标题：三角洲行动手游｜双人代练' },
    })
    // 前置哨兵：首屏确实是**第一款**（id=2），否则「切到 9」可能是恒真
    expect(root.querySelector<HTMLSelectElement>('#gameName')!.value).toBe('2')
    /**
     * 客服**已经敲过**粘贴框（长度 ≥2）⇒ 平台值不会再覆盖它（既有的「仅当为空或长度 <2」口径）。
     * 这一步是**判别用**的：接入点 A 扫的必须是 `fields.remark`（商品标题）而**不是** `#dlcontent`
     * 的值——扫后者的话，这一单的粘贴框里没有游戏名 ⇒ 不会切（这条用例就会红）。
     */
    root.querySelector<HTMLTextAreaElement>('#dlcontent')!.value = '客户原话，先记一笔'
    LAYUI.msg.mockClear()

    // **真触发点**：填 ≥15 位来源单号 → `change`（不是直接调内部函数）
    typeSourceOrderNo(root, '123456789012345')
    await settle()

    // ① 界面：`#gameName` 真的写成了命中的那一款
    expect(root.querySelector<HTMLSelectElement>('#gameName')!.value).toBe('9')
    // ② 粘贴框**没被**平台标题覆盖（它是客服自己的字），也说明上面那步前置是真的生效了
    expect(root.querySelector<HTMLTextAreaElement>('#dlcontent')!.value).toBe('客户原话，先记一笔')
    // ② 提示打在**真实** `layer.msg` 调用上
    expect(LAYUI.msg).toHaveBeenCalledWith(
      '已按商品内容自动选择游戏「三角洲行动」',
      expect.anything(),
    )
    // ③ 真实出口：保存载荷里的 `game` 是命中那一款。
    //    ⛔ 这条是「只调 `onGameChange` 而没写 `#gameName`」那个陷阱的唯一判据
    //    （`readFormState` 读 DOM ⇒ 少了写值这里会是 2）。
    const post = await saveAndSettle(root, sent)
    expect(post.data).toMatchObject({ game: 9, project_text: '深渊满星' })
  })

  it('G1b：商品标题里只写了**俗称**「崩铁」⇒ 也认得出是 `崩坏：星穹铁道`（内置关键词表，2026-10-03 追加）', async () => {
    // 这一条钉的是**整链**：页面把租户字典里的游戏名交给检测器 → 检测器查内置关键词表 → 页面切过去。
    // 检测器自身的规则见 `views/orders/__tests__/gameDetect.spec.ts` 的 D14–D20；这里只证接线。
    const { root } = await mountCreate({
      games: [
        { id: 2, name: '原神' },
        { id: 3, name: '崩坏：星穹铁道' },
      ],
      platform: { remark: '合成商品标题：崩铁 双人代练' },
    })
    // 前置哨兵：首屏是第一款（原神，id=2），否则「切到 3」可能是恒真
    expect(root.querySelector<HTMLSelectElement>('#gameName')!.value).toBe('2')
    typeSourceOrderNo(root, '123456789012345')
    await settle()
    expect(root.querySelector<HTMLSelectElement>('#gameName')!.value).toBe('3')
    // 提示里回的是**字典里的原样名字**（带全角冒号），不是俗称
    expect(LAYUI.msg).toHaveBeenCalledWith(
      '已按商品内容自动选择游戏「崩坏：星穹铁道」',
      expect.anything(),
    )
  })

  it('G2：商品标题不含任何游戏名 ⇒ 不切、也**不提示**（保持首屏默认那一款）', async () => {
    const { root } = await mountCreate({
      games: AUTO_GAMES,
      platform: { remark: '客户要双排上分，速来' },
    })
    expect(root.querySelector<HTMLSelectElement>('#gameName')!.value).toBe('2')
    LAYUI.msg.mockClear()
    typeSourceOrderNo(root, '123456789012345')
    await settle()

    expect(root.querySelector<HTMLSelectElement>('#gameName')!.value).toBe('2')
    // 判别力：「内容里没提游戏」是常态 ⇒ **静默**才是对的；无条件提示会红。
    expect(LAYUI.msg).not.toHaveBeenCalled()
  })

  it('G3（2026-10-03 二次裁定）：客服**先自己改过**游戏 ⇒ 再录入订单**照样自动切一次**', async () => {
    const { root } = await mountCreate({
      games: AUTO_GAMES,
      platform: { remark: '合成商品标题：三角洲行动手游' },
    })
    // ⛔ 必须驱动页面**真实注册**的那个回调（= 真人点 `dd`）：layui 的 `select(selectGame)`
    // 只在渲染出的 `dd` 的 click 里派发，程序化写值 + `form.render('select')` **不会**走到它。
    humanPickGame(root, 5)
    await settle()
    expect(root.querySelector<HTMLSelectElement>('#gameName')!.value).toBe('5')
    LAYUI.msg.mockClear()

    // 客服「先手改了游戏，再录单」的场景：**每次识别都自动选一次** ⇒ 手改过也照样切成标题里那款 9，并出提示。
    typeSourceOrderNo(root, '123456789012345')
    await settle()

    // ⚠️ 把 `readPlatformOrderIntoForm` 里那句 `if (sourceOverride === undefined) userPickedGame = false`
    //    去掉 ⇒ 这里会停在 5（**这就是本用例能红的方式**）。
    expect(root.querySelector<HTMLSelectElement>('#gameName')!.value).toBe('9')
    expect(LAYUI.msg).toHaveBeenCalledWith(
      '已按商品内容自动选择游戏「三角洲行动」',
      expect.anything(),
    )
  })

  it('G3b：识别**之后**客服手改游戏 ⇒ 同一次录入里再换来源（触发点 ②）也**不切**（手改就不动）', async () => {
    const { sent, root } = await mountCreate({
      games: AUTO_GAMES,
      platform: { remark: '合成商品标题：三角洲行动手游' },
    })
    // 先正常识别一次（①）⇒ 自动切成 9
    typeSourceOrderNo(root, '123456789012345')
    await settle()
    expect(root.querySelector<HTMLSelectElement>('#gameName')!.value).toBe('9')

    // 客服手改成 5（他才知道这一单实际是哪款）
    humanPickGame(root, 5)
    await settle()
    expect(root.querySelector<HTMLSelectElement>('#gameName')!.value).toBe('5')
    LAYUI.msg.mockClear()

    // 触发点 ②：客服换一次来源 ⇒ 会**再读一次单**（标题里还是那款 9），但不许把游戏顶回去
    LAYUI.fsPick('orderSoures', 11)
    await settle()
    expect(readCalls(sent).length).toBeGreaterThanOrEqual(2)
    expect(root.querySelector<HTMLSelectElement>('#gameName')!.value).toBe('5')
    expect(LAYUI.msg).not.toHaveBeenCalled()
  })

  it('G4：商品标题里两款同长度游戏名都在 ⇒ **平手不猜**，并提示手动选择', async () => {
    const games = [
      { id: 2, name: '原神' },
      { id: 6, name: '幻塔' },
      { id: 7, name: '鸣潮' },
    ]
    const { root } = await mountCreate({ games, platform: { remark: '幻塔鸣潮双人代练' } })
    expect(root.querySelector<HTMLSelectElement>('#gameName')!.value).toBe('2')
    LAYUI.msg.mockClear()

    typeSourceOrderNo(root, '123456789012345')
    await settle()

    // 判别力：把 `ambiguous` 当成「取命中的第一个」⇒ 会切到 6（`#gameName` 不再是 2）且下面那句 msg 没有
    expect(root.querySelector<HTMLSelectElement>('#gameName')!.value).toBe('2')
    expect(LAYUI.msg).toHaveBeenCalledWith(
      '商品内容里出现了多款游戏（幻塔、鸣潮），请手动选择游戏',
      expect.anything(),
    )
  })

  /**
   * **G4b**：粘贴路径的 `ambiguous` 必须
   * **拼进既有的 `parts`**，⛔ 不能单独 `msg` —— 单独发的话，紧随其后的
   * `msg(parts.join('；'))` 会把它**冲掉**（客服压根看不到「请手动选择游戏」）。
   * 判据取**最后一条** msg：只断言「曾经发过」是抓不到这个冲掉的（旧那条被覆盖了也还在 mock 记录里）。
   */
  it('G4b：粘贴里两款同长度游戏名 ⇒ 「多款」那句与既有「已识别：…」在**同一条**提示里', async () => {
    const games = [
      { id: 2, name: '原神' },
      { id: 6, name: '幻塔' },
      { id: 7, name: '鸣潮' },
    ]
    const { root } = await mountCreate({ games, parseFields: { game_account: 'acc001' } })
    LAYUI.msg.mockClear()

    pasteAndBlur(root, '幻塔鸣潮双人代练')
    await settle()

    expect(root.querySelector<HTMLSelectElement>('#gameName')!.value).toBe('2')
    const calls = LAYUI.msg.mock.calls
    const last = String(calls[calls.length - 1]?.[0] ?? '')
    expect(last).toContain('粘贴内容里出现了多款游戏（幻塔、鸣潮），请手动选择游戏')
    expect(last).toContain('已识别：')
  })

  // ── E-0303：msg 的插值是 HTML 上下文——游戏名（库）与粘贴解析出的名称（客户可控）必须转义 ──

  /** 合成载荷（E-0077）：两款**等长**（`gameDetect` 平手判据），末位差一个字符。 */
  const XSS_NAME_A = '<img src=x onerror=ttXss1>'
  const XSS_NAME_B = '<img src=x onerror=ttXss2>'
  const XSS_ESCAPED_A = '&lt;img src=x onerror=ttXss1&gt;'

  it('E-0303：商品内容命中两款等长游戏（名字含 HTML 载荷）⇒ 「多款」提示里名字转义', async () => {
    const games = [
      { id: 2, name: '原神' },
      { id: 6, name: XSS_NAME_A },
      { id: 7, name: XSS_NAME_B },
    ]
    const { root } = await mountCreate({
      games,
      platform: { remark: `合成商品：${XSS_NAME_A}${XSS_NAME_B}代练` },
    })
    LAYUI.msg.mockClear()
    typeSourceOrderNo(root, '123456789012345')
    await settle()
    expect(LAYUI.msg).toHaveBeenCalledWith(
      `商品内容里出现了多款游戏（${XSS_ESCAPED_A}、&lt;img src=x onerror=ttXss2&gt;），请手动选择游戏`,
      expect.anything(),
    )
  })

  it('E-0303：按商品内容自动选中一款（名字含 HTML 载荷）⇒ 「已按…自动选择游戏」里名字转义', async () => {
    const games = [
      { id: 2, name: '原神' },
      { id: 6, name: XSS_NAME_A },
    ]
    const { root } = await mountCreate({
      games,
      platform: { remark: `合成商品标题：${XSS_NAME_A}` },
    })
    LAYUI.msg.mockClear()
    typeSourceOrderNo(root, '123456789012345')
    await settle()
    expect(root.querySelector<HTMLSelectElement>('#gameName')!.value).toBe('6')
    expect(LAYUI.msg).toHaveBeenCalledWith(
      `已按商品内容自动选择游戏「${XSS_ESCAPED_A}」`,
      expect.anything(),
    )
  })

  it('E-0303：粘贴命中多款（同上载荷）⇒ 拼进 parts 的那句里名字转义（G4b 同形状）', async () => {
    const games = [
      { id: 2, name: '原神' },
      { id: 6, name: XSS_NAME_A },
      { id: 7, name: XSS_NAME_B },
    ]
    const { root } = await mountCreate({ games, parseFields: { game_account: 'acc001' } })
    LAYUI.msg.mockClear()
    pasteAndBlur(root, `合成粘贴：${XSS_NAME_A}${XSS_NAME_B}代练`)
    await settle()
    const calls = LAYUI.msg.mock.calls
    const last = String(calls[calls.length - 1]?.[0] ?? '')
    expect(last).toContain(
      `粘贴内容里出现了多款游戏（${XSS_ESCAPED_A}、&lt;img src=x onerror=ttXss2&gt;），请手动选择游戏`,
    )
    expect(last).not.toContain('<img')
  })

  it('E-0303：粘贴解析出的游戏名对不上字典 ⇒ 「没能对应上」里那个名字转义', async () => {
    const { root } = await mountCreate({
      games: [{ id: 2, name: '原神' }],
      parseFields: { game_name: XSS_NAME_A },
    })
    LAYUI.msg.mockClear()
    pasteAndBlur(root, '合成粘贴文本')
    await settle()
    const calls = LAYUI.msg.mock.calls
    const last = String(calls[calls.length - 1]?.[0] ?? '')
    expect(last).toContain(`没能对应上：游戏「${XSS_ESCAPED_A}」`)
    expect(last).not.toContain(XSS_NAME_A)
    expect(last).not.toContain('<img')
  })

  it('G5：已经是同一款时**再读一次** ⇒ 不重复提示、也不多发一发区服/分类请求', async () => {
    const { sent, root } = await mountCreate({
      games: AUTO_GAMES,
      platform: { remark: '合成商品标题：三角洲行动手游' },
    })
    const cascadeCalls = (): number =>
      sent.filter((c) => /\/servers\/|\/categories\//.test(c.url ?? '')).length
    const cascadeAfterMount = cascadeCalls() // 首屏那一发（games[0] = 原神）

    typeSourceOrderNo(root, '123456789012345')
    await settle()
    expect(root.querySelector<HTMLSelectElement>('#gameName')!.value).toBe('9')
    const cascadeAfterSwitch = cascadeCalls()
    // 判别力：这一次**真的**重建了级联（否则「第二次没多发」可能只是压根没切过）
    expect(cascadeAfterSwitch).toBeGreaterThan(cascadeAfterMount)

    LAYUI.msg.mockClear()
    typeSourceOrderNo(root, '123456789012345')
    await settle()

    expect(root.querySelector<HTMLSelectElement>('#gameName')!.value).toBe('9')
    // 按**真实请求计数**判：同款早退 ⇒ 一发区服/分类都不多发
    expect(cascadeCalls()).toBe(cascadeAfterSwitch)
    expect(LAYUI.msg).not.toHaveBeenCalled()
  })

  it('G6：切游戏前客服已经选过分类 ⇒ 提示里必须说清「区服/分类已重置」', async () => {
    const { root } = await mountCreate({
      games: AUTO_GAMES,
      platform: { remark: '合成商品标题：三角洲行动手游' },
    })
    // `state.category` 只有 `onCategoryChange` 会置非空，而它挂在页面真实注册的
    // `radio(projectType)` 回调上（`onGameChange` 反而会把它清成 null）⇒ 只能这样驱动。
    const categoryOn = LAYUI.handlers.get('radio(projectType)')
    expect(categoryOn).toBeTypeOf('function')
    categoryOn!({ value: '31' })
    await settle()
    LAYUI.msg.mockClear()

    typeSourceOrderNo(root, '123456789012345')
    await settle()

    expect(root.querySelector<HTMLSelectElement>('#gameName')!.value).toBe('9')
    // 这次切换确实清掉了客服填的分类 ⇒ 必须**不静默**（切换与「什么都没发生」要能分辨）
    expect(LAYUI.msg).toHaveBeenCalledWith(
      '已按商品内容自动选择游戏「三角洲行动」（区服/分类已重置，请重新选择）',
      expect.anything(),
    )
  })

  it('G7：粘贴整段里含游戏名、解析结果**没有** `game_name` ⇒ 自动切（走既有粘贴路径）', async () => {
    const { root } = await mountCreate({
      games: AUTO_GAMES,
      parseFields: { game_account: 'acc001' },
    })
    LAYUI.msg.mockClear()

    pasteAndBlur(root, '三角洲行动 代练 账号：acc001')
    await settle()

    // ① 真的切了（走的是「注入 `game_name` → 既有精确同名查找 → `writeFormState`」那条路径）
    expect(root.querySelector<HTMLSelectElement>('#gameName')!.value).toBe('9')
    // ② 既有那句「已识别：…」里带上了 `game`（= 注入的字段真的进了 `filled`）。
    //    ⚠️ `filled` 装的是**字段键名**（`game_account` / `game`），不是中文标签
    //    （见 spec §8 的「实现期修订」第 1 条）。
    expect(LAYUI.msg).toHaveBeenCalledWith(
      expect.stringContaining('已识别：game_account、game'),
      expect.anything(),
    )
  })

  it('G8：粘贴里带**显式**「游戏：X」⇒ 以它为准（字典猜测不参与）', async () => {
    const { sent, root } = await mountCreate({
      games: AUTO_GAMES,
      parseFields: { game_name: '和平精英' },
    })
    // 粘贴原文里有**另一款**（`三角洲行动`）：若检测器抢在显式值之前，就会切成 9
    pasteAndBlur(root, '三角洲行动 代练 账号：acc001')
    await settle()

    expect(root.querySelector<HTMLSelectElement>('#gameName')!.value).toBe('5')
    // 真实出口：载荷里的 `game` 也是显式那一款
    const post = await saveAndSettle(root, sent)
    expect(post.data).toMatchObject({ game: 5 })
  })

  /**
   * **G10**：「保存 → 复位 → 下一单」这条**流程**。
   *
   * ⚠️ 「客服接管」标志由**单号那条触发点（①③）**负责清掉 ⇒ 下面这一次识别自动切
   * **靠的是 G3 那条判据**，**不是** `resetAfterSave` 里那句 `userPickedGame = false`
   * ——把那句删掉，本用例**照样绿**（那句只是让新表单的状态符合直觉，⛔ 不承重）。
   * ⇒ 本用例的价值 = 钉住整条流程：**复位把游戏下拉带回第一款** + 下一单**真的会再切一次**且**有提示**。
   */
  it('G10：保存成功后复位 ⇒ 游戏下拉回到第一款，且**下一单**再录单仍会自动切', async () => {
    const { sent, root } = await mountCreate({
      games: AUTO_GAMES,
      platform: { remark: '合成商品标题：三角洲行动手游' },
    })
    humanPickGame(root, 5) // 这一单客服手改过（本单不再被顶回去）
    await settle()
    expect(root.querySelector<HTMLSelectElement>('#gameName')!.value).toBe('5')

    await saveAndSettle(root, sent) // 保存成功 → `resetAfterSave()`
    await settle()
    // 复位把游戏下拉也带回 `games[0]`
    expect(root.querySelector<HTMLSelectElement>('#gameName')!.value).toBe('2')

    LAYUI.msg.mockClear()
    typeSourceOrderNo(root, '123456789012345')
    await settle()

    // 下一单照常自动切 + 有提示（⛔ 这一格的"能红"靠 G3；见上面的 ⚠️）
    expect(root.querySelector<HTMLSelectElement>('#gameName')!.value).toBe('9')
    expect(LAYUI.msg).toHaveBeenCalledWith(
      '已按商品内容自动选择游戏「三角洲行动」',
      expect.anything(),
    )
  })

  it('G9：客服已接管 ⇒ 粘贴里出现另一款游戏名也**不切**', async () => {
    const { root } = await mountCreate({
      games: AUTO_GAMES,
      parseFields: { game_account: 'acc001' },
    })
    humanPickGame(root, 5)
    await settle()
    expect(root.querySelector<HTMLSelectElement>('#gameName')!.value).toBe('5')
    LAYUI.msg.mockClear()

    pasteAndBlur(root, '三角洲行动 代练 账号：acc001')
    await settle()

    // 判别力：去掉粘贴那一支的 `!userPickedGame` 守卫 ⇒ 会注入 `三角洲行动` 并切成 9
    expect(root.querySelector<HTMLSelectElement>('#gameName')!.value).toBe('5')
    expect(LAYUI.msg).not.toHaveBeenCalledWith(
      expect.stringContaining('自动选择游戏'),
      expect.anything(),
    )
  })

  /**
   * ⭐ 2026-11-10 客户线（W1）：买家身份随「读平台订单」暂存、随**创建**载荷回传。
   * 断言打在真实出口上（`http.request` 真发出去的建单请求），照 `saveAndSettle` 既有写法。
   */
  it('客户线：读单成功响应带 buyer_eid ⇒ 创建载荷带上 buyer_eid/buyer_nick 两键', async () => {
    const { sent, root } = await mountCreate({ platform: { buyer_eid: 'SYNTH-EID-0001' } })
    typeSourceOrderNo(root, '123456789012345')
    await settle()
    // 判别力：读单真的发生了且买家昵称真的回填了（否则"载荷带键"可能只是别的原因）
    expect(readCalls(sent)).toHaveLength(1)
    expect(root.querySelector<HTMLInputElement>('#ww')!.value).toBe('闲鱼买家')

    const post = await saveAndSettle(root, sent)
    expect(post.data).toMatchObject({ buyer_eid: 'SYNTH-EID-0001', buyer_nick: '闲鱼买家' })
  })

  /**
   * ⭐ 客户线另一半：暂存是「**每次读单先清**」——换单号再读一次而这次**失败**（成功但无 eid
   * 的阿奇索腿归 v2 那份 spec 钉）⇒ 上一单的买家身份不得跟着进这一单的创建载荷。
   */
  it('客户线：再读一次失败 ⇒ 创建载荷不带 buyer_eid/buyer_nick（暂存被清）', async () => {
    // ⚠️ 用**可变的 options** 让第二次读单失败（`mountCreate` 的 mock 每次调用都现读
    //    `options.platform`，`null` 正是它文档里的「读单回 1017 业务失败」那一档）。
    //    ⛔ 不要改成对 `http.request` 再 spy 一层 + mockImplementationOnce：CSRF 预检
    //    （`fetchCsrf`）也走 `http.request`，一次性覆盖会被**预检**吃掉，读单那发反而
    //    发不出去（实测探针钉过），症状与「没读第二次」一模一样。
    const mountOptions: { platform?: Record<string, unknown> | null } = {
      platform: { buyer_eid: 'SYNTH-EID-0002' },
    }
    const { sent, root } = await mountCreate(mountOptions)
    typeSourceOrderNo(root, '123456789012345')
    await settle()
    expect(readCalls(sent)).toHaveLength(1)

    // 第二次读单换一个单号、且这一次**失败**（1017）⇒ 上一单的暂存必须已经清掉。
    mountOptions.platform = null
    typeSourceOrderNo(root, '987654321098765')
    await settle()
    expect(readCalls(sent)).toHaveLength(2)

    const post = await saveAndSettle(root, sent)
    expect(post.data).not.toHaveProperty('buyer_eid')
    expect(post.data).not.toHaveProperty('buyer_nick')
  })
})

/**
 * 权限门（`D-0154` 的同一原则「没权限就别画 / fail-closed」）。
 *
 * 为什么这一组是**唯一的守卫**：职位对拍网（`e2e/specs/05-role-action-audit.spec.ts`）**抓不到**
 * 这三处——写模式探针不填表单（`submit(savebtn)` 到不了 `onSave`）、读模式下写请求被拦断、
 * 而编辑态的 iframe 入口对 `minrole` 已被 `order-orderlist.ts` 的详情节点门藏掉。
 *
 * 每道门四档：缺节点不画/不发 · 有节点照旧（正常用户逐字不变）· 身份取不到按缺处理 · 管理员短路。
 * 断言一律打在**真实出口**上：DOM（按钮在不在 / 提示文案）+ `http.request` 实际发出的配置。
 */
describe('录单页权限门：保存 / 粘贴解析 / 编辑态回填（缺节点就不画、不发）', () => {
  type Sent = { method?: string; url?: string; data?: Record<string, unknown> }
  /** 编辑弹层传进来的单号（`layui-island/page.html?page=order-addorder&order_no=…`）。 */
  const DETAIL_NO = '20260921000000000001'
  /** `/api/auth/me` 的**基础**载荷（`fetchGames()` 要读 `game_ids`）。 */
  const ME_BASE = { username: 'u', display_name: 'u', game_ids: [2] }
  const meWith = (...codes: string[]): Record<string, unknown> => ({
    ...ME_BASE,
    menu_permissions: codes,
  })
  /** 管理员：两条短路都要（`perms.ts::canNode` 读 `is_tenant_admin`）。`menu_permissions` 故意空。 */
  const ME_ADMIN = { ...ME_BASE, is_tenant_admin: true, menu_permissions: [] }
  /** 编辑态详情响应：一份「能过必填校验」的合成单。 */
  const DETAIL = {
    order_no: DETAIL_NO,
    game: 2,
    project_text: '原神/深渊',
    content: '客户原始需求',
    time_requirement: '',
    source_order_no: 'TB-OLD',
    order_source: null,
  }

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
    window.history.replaceState({}, '', '/')
    LAYUI.msg.mockClear()
    LAYUI.handlers.clear()
    LAYUI.fsReset()
  })

  /** 挂载（可选编辑态）→ 记录真实出口。`me: 'fail'` = `/api/auth/me` 抛（身份取不到）。 */
  async function mountGated(options: {
    me: Record<string, unknown> | 'fail'
    rememberedHall?: boolean
    orderNo?: string
  }): Promise<{ sent: Sent[]; root: HTMLElement }> {
    window.history.replaceState(
      {},
      '',
      options.orderNo === undefined
        ? '/?page=order-addorder'
        : `/?page=order-addorder&order_no=${options.orderNo}`,
    )
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: unknown) => {
        if (String(url).includes('order-addorder.html')) {
          return { ok: true, status: 200, text: async () => EVIDENCE }
        }
        throw new Error(`未预期的 fetch：${String(url)}`)
      }),
    )
    const { http } = await import('@/api/http')
    const sent: Sent[] = []
    vi.spyOn(http, 'request').mockImplementation((async (config: Sent) => {
      sent.push(config)
      const url = config.url ?? ''
      const method = (config.method ?? 'get').toLowerCase()
      if (url.includes('/auth/me')) {
        if (options.me === 'fail') throw new Error('身份取不到')
        return { data: { code: 0, msg: 'ok', data: { user: options.me } } }
      }
      if (url.includes('/orders/parse/')) {
        return { data: { code: 0, msg: 'ok', data: { fields: {}, matched: [], unmatched: [] } } }
      }
      if (url.includes('/orders/') && method === 'patch') {
        return { data: { code: 0, msg: 'ok', data: { ...DETAIL } } }
      }
      // 快速指派（D-0179）：本页挂载时会拉一次候选名单（默认接手人也从这一发来）。
      // ⚠️ 不给这一支的话它会落进下面的 `{...DETAIL}` 兜底 ⇒ `page.items` 是 `undefined`、
      //    页面把它当加载失败 `msg` 一句（而本组的用例在断言「有没有多余的提示」）。
      if (url.includes('/assignee-options/')) {
        return {
          data: {
            code: 0,
            msg: 'ok',
            data: {
              items: [],
              truncated: false,
              roles: [],
              defAssRecManUid: 0,
              defAssRecMan: '',
              // ⭐ 第六个键照端点**真实形状**给全：页面对缺失的键按 `false` 处理（判据是 `=== true`）
              //    ⇒ 行为不变，但夹具少一个键会让以后的人以为响应里没有它。
              defAssIsHall: options.rememberedHall ?? false,
            },
          },
        }
      }
      // 编辑态第一发 `GET /orders/<no>/` 与其余字典 GEt 都走这里。
      return {
        data: {
          code: 0,
          msg: 'ok',
          data:
            url.includes('/games/') ||
            url.includes('/servers/') ||
            url.includes('/categories/') ||
            url.includes('/order-sources/') ||
            url.includes('/projects/')
              ? { items: [], total: 0, page: 1, page_size: 200 }
              : { ...DETAIL },
        },
      }
    }) as never)

    const root = document.createElement('div')
    document.body.appendChild(root)
    await mountOrderAddOrder(root, { onNavigate: vi.fn() })
    return { sent, root }
  }

  const saveButton = (root: HTMLElement): HTMLButtonElement | null =>
    root.querySelector<HTMLButtonElement>('button[lay-filter="savebtn"]')
  const saveHandler = (): ((...a: unknown[]) => unknown) | undefined =>
    LAYUI.handlers.get('submit(savebtn)')
  const pasteBox = (root: HTMLElement): HTMLTextAreaElement =>
    root.querySelector<HTMLTextAreaElement>('#dlcontent')!
  const pasteHintLabel = (root: HTMLElement): HTMLLabelElement =>
    pasteBox(root).closest('.layui-form-item')!.querySelector<HTMLLabelElement>('label.layui-form-label')!
  const parseCalls = (sent: Sent[]): Sent[] =>
    sent.filter((c) => (c.url ?? '').includes('/orders/parse/'))
  const detailCalls = (sent: Sent[]): Sent[] =>
    sent.filter((c) => (c.url ?? '').endsWith(`/orders/${DETAIL_NO}/`))
  /** 粘贴一段文字并失焦（`onPasteBlur` 挂的是 `blur`）。 */
  function pasteAndBlur(root: HTMLElement, text: string): void {
    const box = pasteBox(root)
    box.value = text
    box.dispatchEvent(new Event('blur'))
  }
  const settle = async (): Promise<void> => {
    await new Promise((resolve) => setTimeout(resolve, 0))
    await new Promise((resolve) => setTimeout(resolve, 0))
  }

  /* ── 保存门：**按模式取**（新建态 `create` 单点 vs 编辑态 `partial_update` any） ────── */

  it('无大厅发单权限时不画内部抢单候选，旧的默认选项不复原', async () => {
    const { root } = await mountGated({ me: meWith('order.add_edit'), rememberedHall: true })
    expect(root.querySelector('.KSPD')?.textContent?.trim()).toBe('暂不指派')
    expect(root.querySelector('input[name="recUser"][value="hall"]')).toBeNull()
  })

  it('保存门：两个节点都没有 ⇒ 按钮**不在 DOM**，也不注册 `submit(savebtn)`', async () => {
    const { root } = await mountGated({ me: meWith('order.list') })
    expect(saveButton(root)).toBeNull()
    expect(saveHandler()).toBeUndefined()
  })

  it('保存门（编辑态）：只给 `order.submit_edit`（`partial_update` any 的另一半）⇒ 按钮在', async () => {
    // ⚠️ **必须带 `order_no`**：只有编辑态那条腿是 any 元组，新建态是单点（下一条用例）。
    const { root } = await mountGated({ me: meWith('order.submit_edit'), orderNo: DETAIL_NO })
    expect(saveButton(root)).not.toBeNull()
    expect(saveHandler()).toBeTypeOf('function')
  })

  it('保存门（新建态）：只给 `order.submit_edit` ⇒ 按钮**不在**（`create` 是单点，⛔ 并集会撑宽）', async () => {
    // 后端 `create = ("order.add_edit",)`（order_views.py:181）——`submit_edit` 只属于
    // `partial_update` 的 any 元组（:182）。两模式共用并集会让这个角色看到按钮、
    // 点保存打 `POST /orders/` ⇒ 403（门比后端**宽**）。
    const { root } = await mountGated({ me: meWith('order.submit_edit') })
    expect(saveButton(root)).toBeNull()
    expect(saveHandler()).toBeUndefined()
  })

  it('保存门（编辑态）：只给 `order.add_edit` ⇒ 按钮在（any 的另一半）', async () => {
    const { root } = await mountGated({ me: meWith('order.add_edit'), orderNo: DETAIL_NO })
    expect(saveButton(root)).not.toBeNull()
    expect(saveHandler()).toBeTypeOf('function')
  })

  it('保存门：给 `order.add_edit` ⇒ 按钮在（**新建态正常用户逐字不变**）', async () => {
    const { root } = await mountGated({ me: meWith('order.add_edit') })
    expect(saveButton(root)).not.toBeNull()
    expect(saveHandler()).toBeTypeOf('function')
  })

  it('保存门：身份取不到 ⇒ 按钮不在（fail-closed）', async () => {
    const { root } = await mountGated({ me: 'fail' })
    expect(saveButton(root)).toBeNull()
    expect(saveHandler()).toBeUndefined()
  })

  it('保存门：管理员短路（`menu_permissions` 为空也画）', async () => {
    const { root } = await mountGated({ me: ME_ADMIN })
    expect(saveButton(root)).not.toBeNull()
    expect(saveHandler()).toBeTypeOf('function')
  })

  /* ── 粘贴解析门：order.add_edit（失焦触发、没有按钮） ───────────────────────────── */

  it('解析门：缺 `order.add_edit` ⇒ 失焦**一发 parse 都不发**，且摘掉「会自动识别」那句承诺', async () => {
    const { sent, root } = await mountGated({ me: meWith('order.list') })
    // 提示被摘掉：label 后缀与 placeholder（它俩就是「粘贴后会自动识别」的承诺）
    expect(pasteHintLabel(root).textContent).not.toContain(PASTE_HINT_LABEL_SUFFIX)
    expect(pasteBox(root).getAttribute('placeholder')).toBeNull()
    pasteAndBlur(root, '一段合成文本')
    await settle()
    expect(parseCalls(sent)).toHaveLength(0)
  })

  it('解析门：有 `order.add_edit` ⇒ 失焦真的发 parse，且提示原样（外观逐字不变）', async () => {
    const { sent, root } = await mountGated({ me: meWith('order.add_edit') })
    expect(pasteHintLabel(root).textContent).toContain(PASTE_HINT_LABEL_SUFFIX)
    expect(pasteBox(root).getAttribute('placeholder')).not.toBeNull()
    pasteAndBlur(root, '一段合成文本')
    await settle()
    expect(parseCalls(sent)).toHaveLength(1)
    expect(parseCalls(sent)[0]!.url).toBe('/api/business/orders/parse/')
  })

  it('解析门：身份取不到 ⇒ 不发（fail-closed）', async () => {
    const { sent, root } = await mountGated({ me: 'fail' })
    pasteAndBlur(root, '一段合成文本')
    await settle()
    expect(parseCalls(sent)).toHaveLength(0)
  })

  it('解析门：管理员短路 ⇒ 照发', async () => {
    const { sent, root } = await mountGated({ me: ME_ADMIN })
    pasteAndBlur(root, '一段合成文本')
    await settle()
    expect(parseCalls(sent)).toHaveLength(1)
  })

  /* ── 编辑态回填门：any(order.view_detail, order.view_content) ───────────────────── */

  it('回填门：无详情节点 ⇒ **不发** `GET /orders/<no>/`，并如实提示（不静默空表单）', async () => {
    const { sent } = await mountGated({ me: meWith('order.add_edit'), orderNo: DETAIL_NO })
    expect(detailCalls(sent)).toHaveLength(0)
    expect(LAYUI.msg).toHaveBeenCalledWith(NO_DETAIL_PERMISSION_MESSAGE, expect.anything())
  })

  it('回填门：只给 `order.view_content`（any 的另一半）⇒ 发', async () => {
    const { sent } = await mountGated({ me: meWith('order.view_content'), orderNo: DETAIL_NO })
    expect(detailCalls(sent)).toHaveLength(1)
    expect((detailCalls(sent)[0]!.method ?? 'get').toLowerCase()).toBe('get')
  })

  it('回填门：给 `order.view_detail` ⇒ 发（正常用户逐字不变）', async () => {
    const { sent } = await mountGated({ me: meWith('order.view_detail'), orderNo: DETAIL_NO })
    expect(detailCalls(sent)).toHaveLength(1)
  })

  it('回填门：身份取不到 ⇒ 不发（fail-closed，且提示）', async () => {
    const { sent } = await mountGated({ me: 'fail', orderNo: DETAIL_NO })
    expect(detailCalls(sent)).toHaveLength(0)
    expect(LAYUI.msg).toHaveBeenCalledWith(NO_DETAIL_PERMISSION_MESSAGE, expect.anything())
  })

  it('回填门：管理员短路 ⇒ 发', async () => {
    const { sent } = await mountGated({ me: ME_ADMIN, orderNo: DETAIL_NO })
    expect(detailCalls(sent)).toHaveLength(1)
  })
})

/**
 * ⭐ 「接单销售客服」那一格（口径见
 * 计划 `docs/plans/2026-10-08-sales-agent-change.md`）。
 *
 * ## 这一格为什么值得单独一组
 * 这组用例判的是**首次接线**：候选从哪来、默认勾谁、编辑态怎么按**文本快照**预勾、没权限时什么样、
 * 以及最要紧的一条——**保存载荷里到底带不带 `sales_agent`**（`readFormState` 从空模型起家，
 * 忘了把这一格读回模型就是「每次保存都静默清空这一列」，而那不会报任何错）。
 *
 * ## 口径两档（⛔ 别照「权限点=限制」的直觉改）
 *  - **持 `order.allow_change_sales_agent`**：新建态默认勾**当前登录人**，
 *    编辑态按 `sales_agent` 那一列的**文本**匹配候选项的 `name` 预勾。
 *  - **不持该节点**：**两态都只读**（纯文本，⛔ 一颗可点的 radio 都不画）——编辑态显示本单现值，
 *    新建态显示「**自己的名字（保存后归你自己）**」。
 *
 * ⚠️ 新建态也走「不持节点 ⇒ 只读」这一档：只读分支若只覆盖编辑态，不持节点的人选了
 * 「测试客服甲」时载荷**不带** `sales_user` ⇒ 保存后提成仍归录单人，**界面不给任何提示**
 * （静默误导）。用例 E 按这个行为断言；⛔ 这不是放宽判据，方向是**把静默误导改成如实呈现**。
 *
 * 断言打在**真实出口**上：DOM（渲染出来的 radio / 勾中哪一颗）+ `http.request` 实际发出的配置。
 * ⚠️ 判据里的勾中状态一律用 `input[value=…]:checked` **从 DOM 现读**，⛔ 不读页面内部状态
 * （否则「渲染没勾上」这个缺陷会被断言自己掩盖过去）。
 */
describe('录单页「接单销售客服」那一格（本线 feat/sales-agent）', () => {
  type Sent = {
    method?: string
    url?: string
    data?: Record<string, unknown>
    params?: Record<string, unknown>
  }
  /** 编辑弹层传进来的单号（与上面那组同一模式）。 */
  const DETAIL_NO = '20260921000000000001'

  /**
   * ⛔ **类名与选择器分成两个常量**（`AGENTS.md` §5-G 坑①）：把选择器常量直接写进
   * `class="…"` 会造出 `class=".x"`、永远匹配不到——那是**静默**的。
   * 这里的选择器是**证据件里那个容器自己的类**（`order-addorder.html:861`），
   * 页面与 spec 都从这一个常量取，改一处两处同时变。
   */
  const CONTAINER_CLASS = 'layui-input-block cf'
  /**
   * ⚠️ 选择器**从页面导出的常量取**（`SALES_AGENT_CONTAINER_SELECTOR`）——**不是**为了省字，
   * 而是为了让「页面往哪儿灌」与「用例查哪儿」**只有一个出处**。⛔ 也**不要**在这里把
   * `CONTAINER_CLASS` 拼成选择器：那会让下面那条「类名 token 真的在证据件里」的判据变成
   * **自指**（无论页面写错成什么，用它拼出来的选择器都能查到自己拼的东西）。
   */
  const CONTAINER_SELECTOR = SALES_AGENT_CONTAINER_SELECTOR
  const KEFU_LABEL_TEXT = '接单销售客服'

  /**
   * 合成候选（⛔ 不是真实姓名/账号——`AGENTS.md` §5-C：夹具一律用合成值，`张三` 那类会被
   * PII 真值守卫当成待查对象；用「测试昵称甲」这种形状）。
   * ⚠️ 值 = **用户主键**，文本 = **显示名**（端点 `items` 的形状，见 `api/orders.ts::KefuOptions`）。
   */
  const KEFU_ITEMS = [
    { id: 7, name: '测试客服甲' },
    { id: 8, name: '测试客服乙' },
  ]
  /** 当前登录人（**故意不在这份候选里**的两条用例要用它：`meId` / `meName` 可覆盖）。 */
  const ME_ID = 8
  const ME_NAME = '测试客服乙'
  const mePayload = (permissions: string[], id = ME_ID, name = ME_NAME): Record<string, unknown> => ({
    id,
    username: 'u',
    display_name: name,
    game_ids: [2],
    menu_permissions: permissions,
  })
  /** 带编辑权限的节点集（本组只关心 `order.allow_change_sales_agent` 在不在里面）。 */
  const WITH_CHANGE = ['order.add_edit', 'order.view_detail', 'order.allow_change_sales_agent']
  const WITHOUT_CHANGE = ['order.add_edit', 'order.view_detail']

  /** 详情响应：一份「能过必填校验」的合成单（`sales_agent` 逐用例覆盖）。 */
  const detail = (salesAgent: string): Record<string, unknown> => ({
    order_no: DETAIL_NO,
    game: 2,
    project_text: '原神/深渊',
    content: '客户原始需求',
    time_requirement: '',
    order_source: null,
    sales_agent: salesAgent,
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
    window.history.replaceState({}, '', '/')
    LAYUI.msg.mockClear()
    LAYUI.handlers.clear()
    LAYUI.fsReset()
  })

  /**
   * 挂载（新建 / 编辑两态）→ 返回「已发出的请求」与 root。
   *
   * ⚠️ **必须先 `await mountOrderAddOrder(...)` 再查 DOM**（`AGENTS.md` §5-G 坑③ 的同一条：
   * 页面标记是**运行期**注入的——证据件先 `root.innerHTML`、再 `renderTemplate` 渲染
   * `<template>`，最后才是我们灌进去的那组 radio）；⛔ 提前查只会看到那个空 `.cf`。
   * 本页的 `bootstrapIsland` 由文件顶部那个替身接管（它同时负责「挂样式」那一步）。
   */
  async function mountSalesAgent(options: {
    /** `/api/auth/me` 的节点集（`null` = 身份取不到那一档）。 */
    permissions: string[] | null
    /** 编辑态的单号；不给 = 新建态。 */
    orderNo?: string
    /** 详情里的 `sales_agent`（那一列的**文本快照**）。 */
    salesAgent?: string
    /** 候选（端点返回的 `items`）；不给 = `KEFU_ITEMS`。 */
    items?: { id: number; name: string }[]
    /** 覆盖当前登录人的主键 / 显示名（「我不在候选里」那两条要用）。 */
    meId?: number
    meName?: string
    /**
     * ⭐ 换归属那个**专用端点**这一发失败——业务码 + 文案由这里给。
     * 不给 ⇒ 成功（回一份订单详情）。
     */
    salesAgentError?: { code: number; msg: string }
    /**
     * ⭐ 换归属那一发**抛一个不是 `Error` 的东西**（G2 用例）：页面那一支的
     * `why` 会是空串 ⇒ 走「只说那句提示、不带原因」那半支。
     * ⚠️ 与 `salesAgentError` **互斥**（两个都给时本函数先判这一个）。
     */
    salesAgentThrow?: unknown
  }): Promise<{ sent: Sent[]; root: HTMLElement }> {
    const items = options.items ?? KEFU_ITEMS
    const order = detail(options.salesAgent ?? '')
    window.history.replaceState(
      {},
      '',
      options.orderNo === undefined
        ? '/?page=order-addorder'
        : `/?page=order-addorder&order_no=${options.orderNo}`,
    )
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: unknown) => {
        if (String(url).includes('order-addorder.html')) {
          return { ok: true, status: 200, text: async () => EVIDENCE }
        }
        throw new Error(`未预期的 fetch：${String(url)}`)
      }),
    )
    const { http } = await import('@/api/http')
    const sent: Sent[] = []
    vi.spyOn(http, 'request').mockImplementation((async (config: Sent) => {
      sent.push(config)
      const url = config.url ?? ''
      const method = (config.method ?? 'get').toLowerCase()
      // ⭐ 候选源那一发。先判它，免得落进下面的兜底形状。
      if (url.includes('/kefu-options/')) {
        return { data: { code: 0, msg: 'ok', data: { items, truncated: false } } }
      }
      // ⭐ **换归属那个专用端点**：必须排在下面 `/orders/` 的两条
      //    兜底**之前**——它的路径里也含 `/orders/`（`/orders/<单号>/sales-agent/`），
      //    顺序反了会被 PATCH/POST 那两条吃掉，用例会以"没发那一发"的形式假红。
      if (url.includes('/sales-agent/')) {
        // ⭐ 抛「不是 `Error` 的东西」那一档：`e instanceof Error` 为假
        //    ⇒ 页面只弹那句不带原因的提示。先判它（两个开关互斥）。
        if (options.salesAgentThrow !== undefined) {
          throw options.salesAgentThrow
        }
        if (options.salesAgentError) {
          // ⚠️ 这里抛一个**带 `code` 的 `Error`**，而不是 import `BizError` 来抛：
          //    页面那一支 catch 的判据是 `e instanceof Error`（它只把 `message` 显示出来）
          //    ⇒ 两者行为等价，而这条替身不必依赖 `@/api/errors` 的模块加载顺序。
          //    ⛔ 别改成"返回一份 `code != 0` 的信封"：`request()` 的替身是**直接用**的，
          //    不走 `unwrap`，那样写会让失败路径静默变成成功（用例会以"没弹提示"的形式假红）。
          const err = new Error(options.salesAgentError.msg)
          ;(err as Error & { code: number }).code = options.salesAgentError.code
          throw err
        }
        return { data: { code: 0, msg: 'ok', data: { ...order } } }
      }
      if (url.includes('/assignee-options/')) {
        return {
          data: {
            code: 0,
            msg: 'ok',
            data: { items: [], truncated: false, roles: [], defAssRecManUid: 0, defAssRecMan: '', defAssIsHall: false },
          },
        }
      }
      if (url.includes('/auth/me')) {
        if (options.permissions === null) throw new Error('身份取不到')
        return {
          data: {
            code: 0,
            msg: 'ok',
            data: { user: mePayload(options.permissions, options.meId ?? ME_ID, options.meName ?? ME_NAME) },
          },
        }
      }
      if (url.includes('/games/')) {
        return {
          data: { code: 0, msg: 'ok', data: { items: [{ id: 2, name: '原神' }], total: 1, page: 1, page_size: 200 } },
        }
      }
      if (url.includes('/orders/') && method === 'patch') {
        return { data: { code: 0, msg: 'ok', data: { ...order } } }
      }
      if (url.includes('/orders/') && method === 'post') {
        return { data: { code: 0, msg: 'ok', data: { order_no: '20260921000000000009' } } }
      }
      // 其余字典（区服 / 分类 / 常用项目 / 来源）都回**空整页**——`onGameChange` 要遍历
      // `items`，回一个详情对象会在那里抛 `servers is not iterable`（形状必须对）。
      if (
        url.includes('/servers/') ||
        url.includes('/categories/') ||
        url.includes('/projects/') ||
        url.includes('/order-sources/')
      ) {
        return { data: { code: 0, msg: 'ok', data: { items: [], total: 0, page: 1, page_size: 200 } } }
      }
      // 编辑态第一发 `GET /orders/<no>/` 与其余 GET 走这里
      return { data: { code: 0, msg: 'ok', data: { ...order } } }
    }) as never)

    const root = document.createElement('div')
    document.body.appendChild(root)
    await mountOrderAddOrder(root, { onNavigate: vi.fn() })
    return { sent, root }
  }

  /** 那一格的容器（⛔ 选择器常量）＋它渲染出来的 radio。 */
  const cell = (root: HTMLElement): HTMLElement => {
    const el = root.querySelector<HTMLElement>(CONTAINER_SELECTOR)
    expect(el).not.toBeNull()
    return el!
  }
  const radios = (root: HTMLElement): HTMLInputElement[] =>
    Array.from(cell(root).querySelectorAll<HTMLInputElement>(`input[type=radio]`))
  const checkedRadio = (root: HTMLElement): HTMLInputElement | null =>
    cell(root).querySelector<HTMLInputElement>('input[type=radio]:checked')

  /** 点选一颗（走真实 DOM 赋值，⛔ 不调页面内部函数）。 */
  function pick(root: HTMLElement, value: string): void {
    const box = cell(root).querySelector<HTMLInputElement>(`input[type=radio][value="${value}"]`)
    expect(box).not.toBeNull()
    box!.checked = true
  }

  /**
   * ⭐ **「那一格被 layui 渲过没有」的记录器**（用例 A2 / B2 的判据）。
   *
   * 为什么必须专门钉这一条：本文件顶部那份替身里 `form.render` 只是个 spy、jsdom 也**不跑**
   * `layui.css` 的 `.layui-form input[type=radio]{display:none}` ⇒「那一格有几颗 input」
   * 两种实现**都是 2**（页面确实把 radio 灌进了 DOM），只有「layui 有没有拿着**终态**标记
   * 渲过一次」能把两版分开——缺渲染时终端里的真实表现是**一颗看不见、也点不到的 radio**
   * （裸 input 被 CSS 隐藏），即「编辑订单时不能改」。
   *
   * 判据的形状 = 「**最终 DOM 的 `innerHTML` 出现在某一次 `form.render('radio')` 的现场里**」
   * （只认 `type === 'radio'` 那几次；其余 `form.render('select' / 'checkbox' / 无参)` 不算数）。
   * ⛔ 不要把它写成「`form.render` 被调过」——那种判据在修复前也成立（页面本来就会渲别的控件）。
   *
   * @returns `seen` = 各次 radio 渲染**当时**那一格的标记；`restore` = 还回替身原状（用完必调，
   *   否则这个 spy 的实现在本文件后续用例里一直挂着）。
   */
  async function recordRadioRenders(): Promise<{ seen: string[]; restore: () => void }> {
    const { bootstrapIsland } = await import('@/layui/island')
    const fake = (await bootstrapIsland(['form'])) as unknown as {
      form: { render: ReturnType<typeof vi.fn> }
    }
    const seen: string[] = []
    fake.form.render.mockImplementation((type?: unknown) => {
      if (type !== 'radio') return
      const host = document.querySelector(CONTAINER_SELECTOR)
      // 渲在"那一格还不存在"的时刻也是**没渲到它**的一种（下面第一条断言会当场说出来）
      seen.push(host ? host.innerHTML : '（渲染时那一格还不在 DOM 里）')
    })
    return { seen, restore: () => fake.form.render.mockReset() }
  }

  /** 点保存并等 `onSave` 的异步链走完（`createOrder` / `updateOrder` → 真实出口）。 */
  async function saveAndSettle(root: HTMLElement): Promise<void> {
    root.querySelector<HTMLInputElement>('#project')!.value = '深渊满星'
    const save = LAYUI.handlers.get('submit(savebtn)')
    expect(save).toBeTypeOf('function')
    save!()
    await new Promise((resolve) => setTimeout(resolve, 0))
    await new Promise((resolve) => setTimeout(resolve, 0))
  }

  const patchCall = (sent: Sent[]): Sent | undefined =>
    sent.find((c) => (c.url ?? '').includes(`/orders/${DETAIL_NO}/`) && (c.method ?? '').toLowerCase() === 'patch')
  const postCall = (sent: Sent[]): Sent | undefined =>
    sent.find((c) => (c.url ?? '').endsWith('/orders/') && (c.method ?? '').toLowerCase() === 'post')
  /**
   * 换归属那个**专用端点**那一发。
   * ⚠️ 判据必须带上 `orderNo`（端点路径里含单号）——只按 `/sales-agent/` 找会在
   * 将来多一个同形状端点时**假绿**；`method` 一起判，免得把 OPTIONS 之类的探活算进来。
   */
  const salesAgentCall = (sent: Sent[]): Sent | undefined =>
    sent.find(
      (c) =>
        (c.url ?? '').includes(`/orders/${DETAIL_NO}/sales-agent/`) &&
        (c.method ?? '').toLowerCase() === 'post',
    )
  /**
   * 那一格**当前**选中的主键 + 名字——判据直接调页面导出的 `readSalesAgentChoice`（真实出口）。
   *
   * ⚠️ **"进来时选的是谁"没有独立出口可读**（它是页面内部状态）⇒ 本组用例一律用
   * **可观测的等价形态**去钉它：「没改就保存 ⇒ 那一发专用端点不发」与
   * 「改了人 ⇒ 那一发带新主键」（用例 D / B）。⛔ 不要为了"能直接断基线"而把它导出来：
   * 那样这条用例就变成在断页面自己的内部变量（自指），基线算错时**照样绿**。
   */
  const currentChoice = (root: HTMLElement): { id: number | null; name: string } =>
    readSalesAgentChoice(root, true)

  /* ── 那张「那一格在哪」的前置判据：证据件里它是一个**空**容器 ───────────────────── */

  it('前置：证据件那一格是空容器，radio 是**运行期**灌进去的（⛔ 别看静态 HTML 下结论）', () => {
    // 容器**类**里确实有那两个 token，而且它旁边那颗 label 的文本就是「接单销售客服」
    // ⇒ 下面所有用例查的确实是**那一格**（⛔ 不是页面上别的 `.layui-input-block.cf`）
    expect(CONTAINER_CLASS.split(' ').every((t) => EVIDENCE.includes(t))).toBe(true)
    const dom = renderedForm()
    const el = dom.querySelector<HTMLElement>(CONTAINER_SELECTOR)
    expect(el).not.toBeNull()
    expect(
      el!.closest('.layui-inline')!.querySelector('label.layui-form-label')?.textContent,
    ).toBe(KEFU_LABEL_TEXT)
    expect(el!.querySelectorAll('input').length).toBe(0)
    // `name` 是 `inputOrderKefu`，值是主键、文本是显示名——三条一起钉。
    expect(SALES_AGENT_INPUT_NAME).toBe('inputOrderKefu')
  })

  /* ── 用例 A：新建态 ⇒ 候选渲染出来、默认勾中当前登录人 ──────────────────────────── */

  it('A（新建态）：那一格渲染出候选 radio，且**默认勾中当前登录人**', async () => {
    const { root } = await mountSalesAgent({ permissions: WITH_CHANGE })
    // 候选**逐条**渲染（少一条 = 有人选不到；多一条 = 有人凭空出现）
    const boxes = radios(root)
    expect(boxes.map((b) => b.getAttribute('value'))).toEqual(['7', '8'])
    expect(boxes.map((b) => b.getAttribute('title'))).toEqual(['测试客服甲', '测试客服乙'])
    expect(boxes.every((b) => b.getAttribute('name') === SALES_AGENT_INPUT_NAME)).toBe(true)
    // 默认勾中当前登录人（`me.id = 8`）
    expect(checkedRadio(root)?.getAttribute('value')).toBe(String(ME_ID))
    // 那个名字就是这一格提交出去的值
    expect(readSalesAgent(root, false)).toBe(ME_NAME)
  })

  /* ── 用例 A2：新建态也必须**让 layui 渲过**那一格（缺了它就是看不见的裸 input） ────── */

  it('A2（新建态）：那一格必须被 `form.render(\'radio\')` 渲过（⛔ 裸 input 在 CSS 里是 display:none）', async () => {
    // 本组挂载夹具把 `servers` / `categories` 都回**空整页** ⇒ `onGameChange` / `onCategoryChange`
    // 里那两处 `formRender('radio')` 都走不到（`categories.length > 0` 不成立）。这正是新建态
    // 最容易退化成"看不见的裸 input"的那一档（真浏览器实测同形：游戏字典为空 ⇒ 皮肤数 0）。
    const { seen, restore } = await recordRadioRenders()
    try {
      const { root } = await mountSalesAgent({ permissions: WITH_CHANGE })
      // ① 终态确实灌了候选（免得上一条在"页面根本没渲染"时也绿）
      expect(radios(root).length).toBe(2)
      // ② 而且这一格的**终态标记**被交到过 layui 手里（修复前 `seen` 是空数组 ⇒ 红）
      expect(seen).toContain(cell(root).innerHTML)
    } finally {
      restore()
    }
  })

  /* ── 用例 B：编辑态 + 有权限 ⇒ 预勾现有值；改了人**走专用端点**（⛔ PATCH 不带那一列） ── */

  it('B（编辑态/有权限）：按**文本**预勾现有销售客服；改人 ⇒ PATCH **不带** `sales_agent`，改由专用端点换归属', async () => {
    const { sent, root } = await mountSalesAgent({
      permissions: WITH_CHANGE,
      orderNo: DETAIL_NO,
      salesAgent: '测试客服甲',
    })
    // 预勾的是**订单**那个人（7），不是当前登录人（8）——这是本用例的判别点
    expect(checkedRadio(root)?.getAttribute('value')).toBe('7')
    expect(readSalesAgent(root, true)).toBe('测试客服甲')
    // 读取出口的形状本身也钉一下：**主键与名字**一起回（两条提交路各要一个）
    expect(currentChoice(root)).toEqual({ id: 7, name: '测试客服甲' })

    // 改成另一个人（当前登录人 8）⇒ 走专用端点
    pick(root, String(ME_ID))
    await saveAndSettle(root)

    const patch = patchCall(sent)
    expect(patch).toBeTruthy()
    // ⛔ **PATCH 载荷里没有这一列**（名字由服务端从人身上算）
    expect(Object.keys(patch!.data!)).not.toContain('sales_agent')
    // 顺带钉住"确实保存了"（否则上一条断言在「压根没发请求」时也会绿）
    expect(patch!.data!.project_text).toBe('深渊满星')

    // ✅ 换归属走的是**专用端点**，入参是**主键**（⛔ 不是名字）
    const agent = salesAgentCall(sent)
    expect(agent).toBeTruthy()
    expect(agent!.method?.toLowerCase()).toBe('post')
    expect(agent!.url).toBe(`/api/business/orders/${DETAIL_NO}/sales-agent/`)
    expect(agent!.data).toEqual({ user: ME_ID })
    // 两发都成了 ⇒ 照旧通知父窗口（弹窗关、列表刷）
    expect(document.title).toBe('编辑订单')
  })

  /* ── 用例 B2：编辑态回填后**必须重渲** radio ────────────── */

  it('B2（编辑态/有权限）：回填后必须**再让 layui 渲一次**那一格（缺了它编辑订单点不动）', async () => {
    // ⛔ 新建态能选（它后面还有别的 radio 渲染顺手渲到），
    //    编辑态那一格若不专门重渲 ⇒ 裸 input 被 `layui.css` 的 `display:none` 藏掉，
    //    用户看到的是一格空白、点也点不动（保存链路本身是好的）。
    const { seen, restore } = await recordRadioRenders()
    try {
      const { root } = await mountSalesAgent({
        permissions: WITH_CHANGE,
        orderNo: DETAIL_NO,
        salesAgent: '测试客服甲',
      })
      // ① 回填对了（预勾的是订单那个人；⛔ 别让"渲过"这条断言在回填错时也绿）
      expect(checkedRadio(root)?.getAttribute('value')).toBe('7')
      // ② 交到 layui 手里的必须是**回填之后**的那个终态（修复前最后一次 radio 渲染发生在
      //    详情回来**之前**，当时的标记里没有勾中项 ⇒ 这条红）
      expect(seen).toContain(cell(root).innerHTML)
    } finally {
      restore()
    }
  })

  /* ── 用例 C：编辑态 + 没权限 ⇒ 只读（没有可点 radio）、载荷不含 `sales_agent` ────── */

  it('C（编辑态/没权限）：那一格只读（没有可点 radio、显示现值），载荷**不含** `sales_agent`', async () => {
    const { sent, root } = await mountSalesAgent({
      permissions: WITHOUT_CHANGE,
      orderNo: DETAIL_NO,
      salesAgent: '测试客服甲',
    })
    // 只读：一颗 radio 都没有（⛔ 不是"渲染了但点不动"），而现值**看得见**
    expect(radios(root)).toEqual([])
    expect(cell(root).textContent).toContain('测试客服甲')
    // 就算有人绕过 DOM 硬塞一颗进去，读取路径也不认（判据在 `readSalesAgent`，不是渲染）
    expect(readSalesAgent(root, true)).toBe('')

    await saveAndSettle(root)
    const call = patchCall(sent)
    expect(call).toBeTruthy()
    // ⛔ 载荷里**没有**这个键（`readSalesAgentChoice` 回空 ⇒ 那条路不发）
    expect(Object.keys(call!.data!)).not.toContain('sales_agent')
    // ⛔ **专用端点也一发都不发**（没节点 ⇒ 两条路都不发）
    expect(salesAgentCall(sent)).toBeUndefined()
    // 顺带钉住"确实保存了"（否则上一条断言在「压根没发请求」时也会绿）
    expect(call!.data!.remark !== undefined || call!.data!.project_text !== undefined).toBe(true)
  })

  /* ── 用例 D：编辑态 + 有权限 + 现值**不在候选里** ⇒ 插在最前并勾中 ──────────────── */

  it('D（编辑态/有权限/现值不在候选里）：现值作为**额外候选项插在最前**并勾中（⛔ 不静默丢值）', async () => {
    const { sent, root } = await mountSalesAgent({
      permissions: WITH_CHANGE,
      orderNo: DETAIL_NO,
      // 这个人已经不在候选里了（停用 / 角色去掉标记 / 改了昵称都会这样）
      salesAgent: '已经离职的测试客服丙',
    })
    const boxes = radios(root)
    // 1 + 2：插在最**前**的那一条是现值，后面才是真候选
    expect(boxes.map((b) => b.getAttribute('title'))).toEqual([
      '已经离职的测试客服丙',
      '测试客服甲',
      '测试客服乙',
    ])
    // 2 条候选**一个都没丢**
    expect(boxes.filter((b) => b.getAttribute('value') === '7')).toHaveLength(1)
    expect(boxes.filter((b) => b.getAttribute('value') === '8')).toHaveLength(1)
    // 勾中的是插进去那一条（不是"一颗都不勾"——那样用户既看不见现值、一保存还会把它改没）
    expect(checkedRadio(root)?.getAttribute('title')).toBe('已经离职的测试客服丙')
    expect(readSalesAgent(root, true)).toBe('已经离职的测试客服丙')

    // 「没改就保存」⇒ PATCH 不带那一列（这一列**不被清空**：服务端一个字都不动），
    // 而且**专用端点也不发**（前后主键相同 ⇒ 那不是一次改动）。
    await saveAndSettle(root)
    expect(Object.keys(patchCall(sent)!.data!)).not.toContain('sales_agent')
    expect(salesAgentCall(sent)).toBeUndefined()
  })

  /* ── 用例 E：新建态 + 没权限 ⇒ **只读** ───────── */

  it('E（新建态/没权限）：那一格**只读**（没有可点 radio、显示「自己的名字（保存后归你自己）」、不发 `sales_user`）', async () => {
    /**
     * ⚠️ **本条是一次行为决策，⛔ 不是放宽判据。**
     *
     * 新口径：**没有这个节点 ⇒ 新建态那一格不渲染可点 radio**，改成如实说清
     * 「这单保存后归谁」（= 你自己，后端按认证态写归属）。「界面显示选了甲、
     * 保存后提成归录单人」那种形状是**静默误导**。
     * 判据方向是**变严**的：断的是"**一颗 radio 都不能有**、且文案说清归属"——
     * 实现改回"仍可选但不发 `sales_user`"的老样子会立刻红。
     */
    const { sent, root } = await mountSalesAgent({ permissions: WITHOUT_CHANGE })
    // ① 一颗可点 radio 都没有（⛔ 不是"渲染了但点不动"：那种仍会让用户以为能改）
    expect(radios(root)).toEqual([])
    // ② 文案说清「保存后归谁」——这是本档**唯一**能让人看懂这格是干嘛的东西
    expect(cell(root).textContent).toContain(ME_NAME)
    expect(cell(root).textContent).toContain('保存后归你自己')
    // ③ 就算有人绕过 DOM 硬塞一颗进去，读取路径也不认（判据在 `readSalesAgent`，不是渲染）
    expect(readSalesAgent(root, false)).toBe('')

    await saveAndSettle(root)
    const call = postCall(sent)
    expect(call).toBeTruthy()
    // ④ ⛔ 没有这个节点 ⇒ **不发** `sales_user`（后端那道闸要它；发了就是一次注定 403 的请求）。
    //    界面（"保存后归你自己"）与提交内容（归属 = 认证态）因此**一致**。
    expect(Object.keys(call!.data!)).not.toContain('sales_user')
    // 顺带钉住"确实建单了"（否则上一条断言在「压根没发请求」时也会绿）
    expect(call!.data!.project_text).toBe('深渊满星')
  })

  /* ── 用例 F：新建态选**别人** ⇒ 带上 `sales_user` 主键 ──── */

  it('F（新建态/有权限）：选的人不是自己 ⇒ 建单载荷带上 `sales_user` **主键**（不是名字）', async () => {
    const { sent, root } = await mountSalesAgent({ permissions: WITH_CHANGE })
    expect(checkedRadio(root)?.getAttribute('value')).toBe(String(ME_ID))   // 默认是自己

    pick(root, '7')
    await saveAndSettle(root)
    const call = postCall(sent)
    expect(call).toBeTruthy()
    // 主键（7），⛔ 不是名字（"测试客服甲"）——名字由服务端从这个人算出来
    expect(call!.data!.sales_user).toBe(7)
    // ⛔ 那一列仍然**不进**载荷（名字一律服务端算；送名字会与归属分叉）
    expect(Object.keys(call!.data!)).not.toContain('sales_agent')
  })

  it('F2（新建态/有权限）：选的是**自己** ⇒ 与改动前等价（请求体里没有 `sales_user`）', async () => {
    const { sent, root } = await mountSalesAgent({ permissions: WITH_CHANGE })
    // 默认就勾着当前登录人 ⇒ 什么都不点直接保存
    await saveAndSettle(root)
    const call = postCall(sent)
    expect(call).toBeTruthy()
    // 「选自己」与"没选"是同一件事（后端缺键 ⇒ 归属 = 认证态，也就是自己）
    expect(Object.keys(call!.data!)).not.toContain('sales_user')
  })

  /* ── 用例 G：PATCH 成功 + 专用端点失败 ⇒ **明确提示**、不显示成"已改" ── */

  it('G（编辑态）：PATCH 成功而换归属**失败** ⇒ 弹「订单已保存，但销售客服没改成」，且不通知父窗口', async () => {
    const { sent, root } = await mountSalesAgent({
      permissions: WITH_CHANGE,
      orderNo: DETAIL_NO,
      salesAgent: '测试客服甲',
      // 后端业务失败（例：那个人刚好被他单占了 / 已结账那道闸）——`msg` 原样透出
      salesAgentError: { code: 1000, msg: '该用户不在销售客服候选范围内' },
    })
    const notified = vi.fn()
    const spy = vi.spyOn(window.parent, 'postMessage').mockImplementation(notified as never)

    pick(root, String(ME_ID))
    await saveAndSettle(root)

    // ① 订单那半**真的成了**（判据不是"页面说了什么"，而是那一发请求发过）
    expect(patchCall(sent)).toBeTruthy()
    // ② 提示**必须同时说清两件事**（⛔ 不许只说"保存失败"、⛔ 不许静默）
    const texts = LAYUI.msg.mock.calls.map((c) => String(c[0]))
    expect(texts.some((t) => t.includes(SALES_AGENT_FAILED_MESSAGE))).toBe(true)
    expect(texts.some((t) => t.includes('订单已保存'))).toBe(true)
    expect(texts.some((t) => t.includes('该用户不在销售客服候选范围内'))).toBe(true)
    // ③ **不通知父窗口**：通知 = 关弹窗 + 列表刷新 + 「保存成功」提示，那是"全成了"的形状
    //    ⇒ 这一档**不许**走那条（本用例的核心判据）。
    expect(spy).not.toHaveBeenCalled()
    // ④ **两发都真的发了**（否则上面那句"PATCH 成了"可能只是"什么都没发"的假绿）
    expect(postCall(sent)).toBeUndefined()                 // 这一档不是建单
    expect(salesAgentCall(sent)!.data).toEqual({ user: ME_ID })
    // ⑤ ⭐ **那一格复位回服务端真值**：订单那半确实存好了，但**归属没换成**
    //    ⇒ 那一格必须画回订单里那个人（7 / 测试客服甲），⛔ 不许留着用户新选的人——
    //    toast 几秒即逝，而"选中态"是唯一**持久**的"已经改成"证据。
    //    （把「还勾在用户选的人上」当成正确是静默误导）。
    expect(checkedRadio(root)?.getAttribute('value')).toBe('7')
    expect(readSalesAgent(root, true)).toBe('测试客服甲')
    //    不会被判成"改过"、⛔ 不会对着那个已经失败的端点再打一发。
    //    判据：那一发专用端点的总数**仍然是 1**（只发过第一次那一下）。
    //    ⚠️ 这条同时分辨「复位时顺手把基线改成用户选的人」那种写错法——那样第二次保存
    //    会被判成"没改过"而**永远不再发**（另一次也错，但错在相反方向；见 `restoreSalesAgentRow`
    //    的注释）。所以基线对不对必须由**两次**保存一起看。
    await saveAndSettle(root)
    expect(sent.filter((c) => (c.url ?? '').includes('/sales-agent/'))).toHaveLength(1)
    spy.mockRestore()
  })

  it('G2（编辑态）：换代那一发抛的**不是 `Error`** ⇒ 只弹那句不带原因的提示，那一格照样复位', async () => {
    /**
     * ⭐ `why` 取不到时（抛出的不是 `Error`，例如某个库里 `throw 'x'`）
     * 页面走的是 `msg(SALES_AGENT_FAILED_MESSAGE)`（**不带**原因那半支）。
     * 判据：① 提示里**只有**那句常量文案（⛔ 不该多一个冒号尾巴）；
     * ② 那一格**复位回服务端真值**——与上一条同一条口径，两条合起来覆盖复位这件事
     * 在两条 catch 分支上都成立（⛔ 不只断言 toast 文本——toast 会消失，那一格不会）。
     */
    const { root } = await mountSalesAgent({
      permissions: WITH_CHANGE,
      orderNo: DETAIL_NO,
      salesAgent: '测试客服甲',
      salesAgentThrow: '不是 Error 的东西',
    })

    pick(root, String(ME_ID))
    await saveAndSettle(root)

    const texts = LAYUI.msg.mock.calls.map((c) => String(c[0]))
    expect(texts).toContain(SALES_AGENT_FAILED_MESSAGE)
    // ⛔ 不是 `${…}：${why}` 那一支（那一支会多一个冒号）
    expect(texts.some((t) => t.startsWith(`${SALES_AGENT_FAILED_MESSAGE}：`))).toBe(false)
    // 那一格回到**服务端真值**（7 = 订单里那个人），⛔ 不是用户刚点的 8
    expect(checkedRadio(root)?.getAttribute('value')).toBe('7')
  })

  /* ── 两条边角（都是「静默丢值 / 静默写错值」的形状） ─────────────────────────────── */

  it('边角①：新建态而当前登录人**不在候选里** ⇒ 补一条并勾中（⛔ 不是一颗都不勾）', async () => {
    // 截断 / 字典加载失败 / 角色没打标记都会造成"我自己不在名单里"
    // ⚠️ 判据打在**持节点**这一档（"能选人"的唯一一档）；没节点那一档**只读**（用例 E），
    //    "补一条并勾中"在那档**不存在**（没有 radio 可勾）。
    const { sent, root } = await mountSalesAgent({
      permissions: WITH_CHANGE,
      items: [{ id: 7, name: '测试客服甲' }],
      meId: 99,
      meName: '我不是客服',
    })
    expect(radios(root).map((b) => b.getAttribute('title'))).toEqual(['我不是客服', '测试客服甲'])
    expect(checkedRadio(root)?.getAttribute('value')).toBe('99')
    await saveAndSettle(root)
    expect(postCall(sent)!.data!.sales_agent).toBe('我不是客服')
  })

  it('边角②：保存成功后那一格**复位**（回到默认勾中当前登录人，⛔ 不粘上一个客户服）', async () => {
    const { root } = await mountSalesAgent({ permissions: WITH_CHANGE })
    pick(root, '7')
    expect(checkedRadio(root)?.getAttribute('value')).toBe('7')
    await saveAndSettle(root)
    // 保存成功 ⇒ `resetAfterSave` 重画那一格（`form.reset()` 只会回到"渲染那一刻"的勾选，
    // 也就是上一单选的那个人 ⇒ 不复位就会静默把上一位销售客服带进下一单）
    expect(checkedRadio(root)?.getAttribute('value')).toBe(String(ME_ID))
  })

  it('边角③：候选加载失败 ⇒ 静默降级（那一格不炸、仍能提交当前登录人）', async () => {
    // ⚠️ 同上：空候选这一档要在**持节点**下才有可点的 radio；
    //    没节点那一档现在是只读文本（用例 E 覆盖）。
    const { sent, root } = await mountSalesAgent({
      permissions: WITH_CHANGE,
      items: [],
    })
    // 空候选 + 当前登录人 ⇒ 只剩他自己那一条（见「同族的第二处」）
    expect(radios(root).map((b) => b.getAttribute('value'))).toEqual([String(ME_ID)])
    await saveAndSettle(root)
    expect(postCall(sent)!.data!.sales_agent).toBe(ME_NAME)
  })
})
