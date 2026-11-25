// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { buildOrderCols, TAKER_PERF_BRANCH } from '../order-orderlist'
import type { OrderListItem } from '@/api/orders'

/**
 * 管理订单页**第二批列**（spec `docs/specs/2026-09-23-order-list-columns-design.md` §7）的
 * templet 渲染判据：每个新/改的 templet 都测**有值**与**空值**两支。
 *
 * ⚠️ 取值与颜色一律指到对标源文件（`.tools/recon/order-pages/orderlist.js`，只读）：
 *  | 列 | 对标函数 | 行号 |
 *  |---|---|---|
 *  | 角色名 | `roleNameRow` | L1874-1879 |
 *  | 时间 | `orderHourDescRow` | L1975-1987 |
 *  | 旺旺/来源 | `wangwangRow` | L1989-2035 |
 *  | 发单价 | `throwPriceRow` | L2038-2060 |
 *  | 实收款 | `orderNetReceiptsRow` | L2087-2089 |
 *  | 销售客服 | `kefuRow` | L2101-2103 |
 *  | 发单人 | `assUserNameRow` | L2104-2107 |
 *  | 完单时间 | `completeTimeRow` | L2111-2123 |
 *  | 重要备注 | `remarkRow` | L2165-2167 |
 *
 * ⚠️ **按 `field` 取列，不按下标**：下标会随删列漂移（这正是本轮的改动），
 * 用 `field` 才让这些用例钉住「那一列的 templet」，而不是「第 N 个位置」。
 */
const BASE: OrderListItem = {
  order_no: '20260920213215233029',
  source_order_no: '',
  source: '微信',
  // ⭐ 列表载荷带派生键 `source_name`——「订单来源」列现在
  // 读它（挂字典 ⇒ 字典当前名，否则后端回落 `source` 快照）。本夹具没挂字典 ⇒ 两者同值。
  source_name: '微信',
  taker_name: '',
  status: 'UNASSIGNED',
  game: 2,
  server: null,
  project_text: '深渊满星',
  total_price: '100.00',
  refund_fee: '0.00',
  customer_phone: '',
  is_urgent: false,
  created_at: '2026-09-20T21:32:15+08:00',
  game_role_name: '',
  customer_ww: '',
  hall_price: null,
  sales_agent: '',
  created_by: '',
  remark: '',
  net_received: '100.00',
  hall_publisher_name: '',
  completed_at: null,
  time_requirement: '',
  payment_status: 'UNSETTLED',
  payment_method: '',
  // ⭐ 列表载荷带「打手绩效是否已结账」⇒「发单价」列的「结」标记。
  taker_settled: false,
}

/** 取某一列的**真实 templet**（不手抄标记，避免与实现分叉）并渲染一行。 */
function cell(field: string, row: Partial<OrderListItem> = {}): string {
  const col = buildOrderCols(
    () => 'G',
    () => '',
  ).find((c) => c.field === field)
  expect(col, `列定义里找不到 field=${field}`).toBeDefined()
  expect(col!.templet, `field=${field} 没有 templet`).toBeTypeOf('function')
  return (col!.templet as (d: OrderListItem) => string)({ ...BASE, ...row })
}

describe('第二批列：角色名（可点、空显「无」）', () => {
  it('有值：`a.changeRoleName` + `data-order`/`data-rolename` + 对标 tips + 原值', () => {
    const html = cell('_role', { game_role_name: '老王' })
    // 对标 L1876：`class="changeRoleName"`、`data-id`（我们统一用本页的 `data-order`）、
    // `data-rolename`、`lay-tips="角色名：X，点击可以快速编辑"`、内容为原值。
    expect(html).toContain('class="changeRoleName"')
    expect(html).toContain('data-order="20260920213215233029"')
    expect(html).toContain('data-rolename="老王"')
    expect(html).toContain('lay-tips="角色名：老王，点击可以快速编辑"')
    expect(html).toContain('>老王</a>')
    // 对标那条 `<a>` 带 `href`（少了它没有手型光标，见文件头「已知差异 13」同族理由）。
    expect(html).toContain('href="javascript:void(0)"')
  })

  it('空值：显示「无」，但 `data-rolename` 与 tips 里仍是**空串原值**（照对标 L1876）', () => {
    const html = cell('_role', { game_role_name: '' })
    expect(html).toContain('>无</a>')
    expect(html).toContain('data-rolename=""')
    // 对标 tips 拼的是 `res.roleName` **原值**，不是「无」那一档的显示值。
    expect(html).toContain('lay-tips="角色名：，点击可以快速编辑"')
    expect(html).not.toContain('lay-tips="角色名：无')
  })
})

describe('第二批列：时间（用 time_requirement，刻意偏离对标 L1975 的 orderHoursDescStr）', () => {
  it('有值：`<span style="color:#5BB75B">` 包住那段文本（色值照对标 L1983）', () => {
    expect(cell('_hour', { time_requirement: '2天3小时' })).toBe(
      '<span style="color:#5BB75B">2天3小时</span>',
    )
  })

  it('空值：**空串**（对标 L1977-1979 的 `if (!timestr) return ""`，不渲染空壳 span）', () => {
    expect(cell('_hour', { time_requirement: '' })).toBe('')
  })
})

describe('第二批列：完单时间（仍默认隐藏，但内容实装）', () => {
  it('有值：按本页既有形状渲染（`replace("T", " ")` + 截到分钟，与 createRow 同一约定）', () => {
    expect(cell('_completeTime', { completed_at: '2026-09-23T10:30:00.000000+08:00' })).toBe(
      '<span>2026-09-23 10:30</span>',
    )
  })

  it('`completed_at === null` ⇒ 空串（对标 completeTimeRow L2111-2113 也是空串那档）', () => {
    expect(cell('_completeTime', { completed_at: null })).toBe('')
  })
})

describe('第二批列：旺旺/来源（只做文本，不做对标的 OpenWangwang 外链）', () => {
  it('有值：纯 `<span>` 包住 customer_ww', () => {
    expect(cell('_ww', { customer_ww: 'tb_ww001' })).toBe('<span>tb_ww001</span>')
  })

  it('空值：空 span（不造「无」，对标 L2029 那一档也是空内容）', () => {
    expect(cell('_ww', { customer_ww: '' })).toBe('<span></span>')
  })
})

describe('第二批列：发单价（金额走 money()；两支改价链见 T8 describe）', () => {
  it('有值：金额文本 + 对标 `throwPriceRow` 金额那份 `#ff6a00`（L2047/L2055）', () => {
    // ⚠️ 对标 `#5FB878`（L2039）按源码是**「结」标记的底色**，不是金额的颜色；我们不做那个标记，
    //    所以取的是金额自己的颜色（spec §7.3 把它记成「对标原色」）。
    const html = cell('_throw', { hall_price: '88.00' })
    expect(html).toContain('88.00')
    expect(html).toContain('color:#ff6a00')
  })

  it('`hall_price === null` ⇒ `0.00`（与「接单价」同一 money() 口径，controller ruling 1）', () => {
    expect(cell('_throw', { hall_price: null })).toContain('0.00')
  })

  /* ── ⭐ 2026-10-28（业务方真机）：有平台推送 ⇒ 显示**推送行的 price** ── */

  const PUSH = {
    platform: 10,
    serial_no: '19000000000000000201',
    state: 'sent' as const,
    remote_status: 11,
    remote_cancel_status: 10,
    is_active: true,
    price: '3.00',
  }

  it('有推送 + `price=3.00` + 本地 `hall_price=null` ⇒ **显示 3.00**（不再显示 0.00）', () => {
    const html = cell('_throw', { hall_price: null, platform_push: PUSH })
    expect(html).toContain('3.00')
    expect(html).not.toContain('0.00')
    // 改价写的仍是**本地**发单价 ⇒ `data-fee` 不能灌平台价。
    expect(html).toContain('data-fee=""')
  })

  it('有推送但 `price` 键缺席（旧后端）⇒ 降级回本地 `hall_price`', () => {
    const oldShape: Record<string, unknown> = { ...PUSH }
    delete oldShape.price
    const html = cell('_throw', {
      hall_price: '12.50',
      platform_push: oldShape as never,
    })
    expect(html).toContain('12.50')
  })

  it('有推送但 `price=null`（受限角色脱敏）⇒ 降级回本地 `hall_price`', () => {
    const html = cell('_throw', {
      hall_price: '12.50',
      platform_push: { ...PUSH, price: null },
    })
    expect(html).toContain('12.50')
  })

  it('平台订单全部结束（`is_active=false`）⇒ 回退本地 `hall_price`（普通内部订单）', () => {
    const html = cell('_throw', {
      hall_price: '12.50',
      platform_push: { ...PUSH, is_active: false },
    })
    expect(html).toContain('12.50')
    expect(html).not.toContain('3.00')
  })

  it('无推送 ⇒ 仍本地 `hall_price`（不回归）', () => {
    const html = cell('_throw', { hall_price: '12.50' })
    expect(html).toContain('12.50')
  })

  /**
   * 发单价可点：两支改价链 `changeThrowFeeEdit` / `changeThrowFee` 各出现一次。
   * 断言「没有链接」会红。
   */
  it('**可点**：T8 起是两支改价链之一（旧的「只做金额、没有 `<a>`」断言已作废）', () => {
    expect(cell('_throw', { hall_price: '88.00', hall_publisher_name: '' })).toContain(
      'class="changeThrowFeeEdit"',
    )
    expect(cell('_throw', { hall_price: '88.00', hall_publisher_name: 'publisher1' })).toContain(
      'class="changeThrowFee"',
    )
  })
})

describe('第二批列：实收款（只读派生值，业务方裁定不可点）', () => {
  it('直接渲染载荷里的 net_received，不在前端重算', () => {
    expect(cell('_netreceipts', { net_received: '123.45' })).toBe('<span>123.45</span>')
  })

  it('**不可点**：没有 `<a>`（对标那支是 `editNetReceiptsPrice` 链接，L2088 — 我们刻意不做）', () => {
    expect(cell('_netreceipts', { net_received: '123.45' })).not.toContain('<a')
  })
})

describe('第二批列：销售客服 / 发单人 / 录单人（纯文本）', () => {
  it('销售客服：`<span lay-tips>` 包住 sales_agent（形状照对标 kefuRow L2102）', () => {
    expect(cell('_kefu', { sales_agent: '客服甲' })).toBe(
      '<span lay-tips="客服甲">客服甲</span>',
    )
  })

  it('销售客服空值：空 span（对标那一档内容也是空串，不造「无」）', () => {
    expect(cell('_kefu', { sales_agent: '' })).toBe('<span lay-tips=""></span>')
  })

  it('发单人：有值渲染 hall_publisher_name（对标 assUserNameRow L2105-2106）', () => {
    expect(cell('_assUserName', { hall_publisher_name: '张三' })).toBe(
      '<span lay-tips="张三">张三</span>',
    )
  })

  it('发单人空值 ⇒ 「无」（对标 L2105 那条空判据那一档）', () => {
    expect(cell('_assUserName', { hall_publisher_name: '' })).toBe(
      '<span lay-tips="无">无</span>',
    )
  })

  it('录单人：字段名仍是 addOrderUserName（照对标列定义），值取 created_by', () => {
    expect(cell('addOrderUserName', { created_by: 'admin' })).toBe('admin')
  })

  // ⭐ 业务方要求：这列要显示**账号名称**（如「小九」），不是登录账号（手机号）。
  // 判据两条腿：有显示名时**优先**用它（改回 `d.created_by` 立即红）；没有时回落账号快照。
  it('录单人：有显示名时优先显示它（不是登录账号）', () => {
    expect(cell('addOrderUserName', { created_by: '13800000000', created_by_name: '小九' })).toBe(
      '小九',
    )
  })

  it('录单人：显示名为空时回落账号快照', () => {
    expect(cell('addOrderUserName', { created_by: '13800000000', created_by_name: '' })).toBe(
      '13800000000',
    )
  })

  it('录单人空值：空内容（业务方只点名「发单人空显无」，这列不造「无」）', () => {
    expect(cell('addOrderUserName', { created_by: '' })).toBe('')
  })
})

describe('第二批列：重要备注（可点、照对标 L2166）', () => {
  it('有值：`a.orderRemark` + `data-remark` + 红字 `#E03636` + tips 是备注原文', () => {
    const html = cell('_remark', { remark: '客户要求尽快' })
    expect(html).toContain('class="orderRemark"')
    expect(html).toContain('data-order="20260920213215233029"')
    expect(html).toContain('data-remark="客户要求尽快"')
    expect(html).toContain('style="color:#E03636"')
    expect(html).toContain('lay-tips="客户要求尽快"')
    expect(html).toContain('>客户要求尽快</a>')
    expect(html).toContain('href="javascript:void(0)"')
  })

  it('空值：显示「无」+ tips 换成对标那句「点击可以编辑订单重要备注」+ `data-remark=""`', () => {
    const html = cell('_remark', { remark: '' })
    expect(html).toContain('>无</a>')
    expect(html).toContain('data-remark=""')
    expect(html).toContain('lay-tips="点击可以编辑订单重要备注"')
  })
})

/**
 * F1（2026-09-23 收口）：本批动过的 templet sink 里的**自由文本**一律过 `escapeHtml`。
 *
 * 判据分两半，缺一不可：
 *  1. **注入不成立**：产物解析后那一格（及其父层）里**没有**载荷造出来的元素；
 *  2. **值不丢**：`textContent` / `getAttribute` 把实体**解码回**原始字符串——
 *     只断「没有裸标签」的话，把整段文本删掉也能绿。
 *
 * ⚠️ 属性是 HTML 上下文，所以属性值与正文都要转义；`dataset` 会把实体解码回来 ⇒
 * 点击处理读到的仍是原值（Critical 的往返判据在
 * `order-orderlist-phase1.spec.ts` 的「带引号的备注往返」用例里）。
 */
describe('F1：自由文本 sink 过 escapeHtml（注入不成立 + 值原样回读）', () => {
  /** 把某一列的真实 templet 产物解析成 DOM（jsdom 像浏览器一样解析实体）。 */
  function render(field: string, row: Partial<OrderListItem>): { html: string; host: HTMLElement } {
    const html = cell(field, row)
    const host = document.createElement('div')
    host.innerHTML = html
    return { html, host }
  }

  it('`customer_ww` 带 `<img>` 载荷 ⇒ 没有裸 `<img`，且 textContent 还原原文', () => {
    const payload = '<img src=x onerror=1>'
    const { html, host } = render('_ww', { customer_ww: payload })
    expect(html).not.toContain('<img')
    expect(host.querySelector('img')).toBeNull()
    expect(host.textContent).toBe(payload)
  })

  it('`remark = a"b` ⇒ `data-remark` 属性原样回读 `a"b`（转义不丢值，Critical 的往返前提）', () => {
    const { host } = render('_remark', { remark: 'a"b' })
    expect(host.firstElementChild!.getAttribute('data-remark')).toBe('a"b')
  })

  it('`remark` 属性注入载荷解析后仍是纯文本（`lay-tips` / 正文都不出元素）', () => {
    const payload = '"><img src=x onerror=1>'
    const { html, host } = render('_remark', { remark: payload })
    expect(html).not.toContain('<img')
    expect(host.querySelector('img')).toBeNull()
    expect(host.firstElementChild!.getAttribute('lay-tips')).toBe(payload)
    expect(host.textContent).toBe(payload)
  })

  it('`game_role_name` 的属性与正文都把 `a"b` 原样回读', () => {
    const { host } = render('_role', { game_role_name: 'a"b' })
    const el = host.firstElementChild!
    expect(el.getAttribute('data-rolename')).toBe('a"b')
    expect(el.getAttribute('lay-tips')).toBe('角色名：a"b，点击可以快速编辑')
    expect(host.textContent).toBe('a"b')
  })

  it('`sales_agent` / `hall_publisher_name` 的 `lay-tips` 属性注入不成立', () => {
    const payload = '"><img src=x onerror=1>'
    // 逐字段收进一个对象再**一次性**断言：字段名留在 diff 里（`expect` 不收第二参数）。
    const seen: Record<string, { raw: boolean; imgEl: boolean; text: string | null; tip: string | null }> = {}
    for (const [field, row] of [
      ['_kefu', { sales_agent: payload }],
      ['_assUserName', { hall_publisher_name: payload }],
    ] as const) {
      const { html, host } = render(field, row)
      seen[field] = {
        raw: html.includes('<img'),
        imgEl: host.querySelector('img') !== null,
        text: host.textContent,
        tip: host.firstElementChild?.getAttribute('lay-tips') ?? null,
      }
    }
    expect(seen).toEqual({
      _kefu: { raw: false, imgEl: false, text: payload, tip: payload },
      _assUserName: { raw: false, imgEl: false, text: payload, tip: payload },
    })
  })

  it('`time_requirement` / `created_by` / `project_text` 的正文注入不成立', () => {
    const payload = '<img src=x onerror=1>'
    const seen: Record<string, { raw: boolean; imgEl: boolean; text: string | null }> = {}
    for (const [field, row] of [
      ['_hour', { time_requirement: payload }],
      ['addOrderUserName', { created_by: payload }],
      ['_project', { project_text: payload }],
    ] as const) {
      const { html, host } = render(field, row)
      seen[field] = {
        raw: html.includes('<img'),
        imgEl: host.querySelector('img') !== null,
        text: host.textContent,
      }
    }
    expect(seen).toEqual({
      _hour: { raw: false, imgEl: false, text: payload },
      addOrderUserName: { raw: false, imgEl: false, text: payload },
      _project: { raw: false, imgEl: false, text: payload },
    })
  })
})

/**
 * T7：接单价列 = ①收款状态三档标记 ＋ ②金额链接（spec §7 第 1 条 / §8 第 24 / 25 / 32 条）。
 *
 * ⚠️ 三句 `lay-tips` / 三个底色 / 单字标记都是**逐字字面值**（不是从页面常量反向引用，
 * E-0024：期望值写字面值，别 `assertEqual(f(x), 同一个常量)`）。出处：
 * `.tools/recon/order-pages/recon-payment-status.md` §1.2 的 `orderlist.js:2058-2075` 原文。
 */
describe('T7 接单价列：收款状态标记 + `editOrderPrice` 金额链接（spec §8 第 24 / 25 / 32 条）', () => {
  const TIPS_UNSETTLED =
    '该订单在实际中还未收到款（注意：与代练员或下家结账请到财务运营-绩效管理中操作，此处是用来与发单方/上家进行结算）'
  const TIPS_SETTLING =
    '该订单在实际中处于正在结算中（注意：与代练员或下家结算请到财务运营-绩效管理中操作，此处是用来与发单方/上家进行结算）'
  const TIPS_SETTLED =
    '该订单实际中已收款（注意：与代练员或下家结算请到财务运营-绩效管理中操作，此处是用来与发单方/上家进行结算）'

  it('三档的标记文本 / 底色 / tips（`lay-tips` 与 `title` 两处）逐字', () => {
    const cases = [
      ['UNSETTLED', '未', '#E6E6FA', TIPS_UNSETTLED],
      ['SETTLING', '中', '#B0E0E6', TIPS_SETTLING],
      ['SETTLED', '收', '#98FB98', TIPS_SETTLED],
    ] as const
    for (const [code, text, color, tips] of cases) {
      const html = cell('_price', { payment_status: code })
      expect(html).toContain('class="lableb jsSta"')
      expect(html).toContain(`background-color:${color}`)
      expect(html).toContain(`lay-tips="${tips}"`)
      expect(html).toContain(`title="${tips}"`)
      expect(html).toContain(`>${text}</a>`)
    }
  })

  it('未知码（空串）落「未」档', () => {
    const html = cell('_price', { payment_status: '' })
    expect(html).toContain('background-color:#E6E6FA')
    expect(html).toContain('>未</a>')
  })

  /**
   * MF-1（T7 评审）：**非空**未知码也要落「未」档。
   *
   * 上面那条只钉了空串；而查表若写成 `PAYMENT_STATUS_MARKS[code] ?? DEFAULT`（普通对象字面量），
   * **原型链上的键**（`__proto__` / `constructor` / `toString` …）会被 `[]` 命中并返回一个真值
   * ⇒ `??` 不兜底 ⇒ 标记渲成 `background-color:undefined …>undefined</a>`。判据用
   * `not.toContain('undefined')` 把它钉死（`data-sta` 里出现的码本身不含该串）。
   */
  it('非空未知码（原型链键）也落「未」档，不渲成 undefined', () => {
    for (const code of ['__proto__', 'constructor', 'toString', 'BOGUS']) {
      const html = cell('_price', { payment_status: code })
      expect(html).toContain('background-color:#E6E6FA')
      expect(html).toContain('>未</a>')
      expect(html).not.toContain('undefined')
    }
  })

  it('标记带 `data-order` / `data-sta`、金额链接带 `data-fee`（点击要用它们，不是文本）', () => {
    const html = cell('_price', { total_price: '88.00', payment_status: 'SETTLING' })
    expect(html).toContain('data-order="20260920213215233029"')
    expect(html).toContain('data-sta="SETTLING"')
    expect(html).toContain('data-fee="88.00"')
  })

  it('金额链接换成对标形状：class `editOrderPrice`（旧的 `orderPrice` 不再出现）、色 `#E03636`、两位小数', () => {
    const html = cell('_price', { total_price: '88.00' })
    expect(html).toContain('class="editOrderPrice"')
    // 旧 class **整串**都不再出现（重命名判据，不只是 `class="orderPrice"` 这一种拼法）。
    expect(html).not.toContain('orderPrice')
    expect(html).toContain('color:#E03636;')
    expect(html).toContain('>88.00</a>')
    // 刻意偏离对标的 `toFixed(1)`（C5）：`88.00` 不是 `88.0`
    expect(html).not.toContain('>88.0</a>')
  })

  it('`payment_method` 非空才追加「，支付方式：X」；为空时那一段不存在', () => {
    expect(cell('_price', { payment_method: '' })).not.toContain('，支付方式：')
    expect(cell('_price', { payment_method: '花呗' })).toContain('，支付方式：花呗')
  })

  /**
   * 评审建议 S1：非空那档要钉**整句恰好**（spec §8 25 的「有且等于」），
   * 不能只 `toContain('，支付方式：')`——那样前后多一句、金额写错都照样绿。
   */
  it('`payment_method` 非空时 tip **整句恰好**（`lay-tips` / `title` 两处逐字）', () => {
    const html = cell('_price', { total_price: '100.00', payment_method: '花呗' })
    const tip = '该笔订单的接单金额：100.00元，支付方式：花呗'
    expect(html).toContain(`lay-tips="${tip}"`)
    expect(html).toContain(`title="${tip}"`)
  })

  it('`payment_method` 注入串在两个属性 sink（`lay-tips` / `title`）里都被转义', () => {
    const evil = '<img src=x onerror=alert(1)>'
    const html = cell('_price', { payment_method: evil })
    expect(html).not.toContain('<img')
    const hits = html.match(/&lt;img src=x onerror=alert\(1\)&gt;/g) ?? []
    expect(hits).toHaveLength(2)
  })
})

/**
 * T7：「发单价」列**三支**（spec §7.2 / §8 第 27 条；对标 `throwPriceRow` `orderlist.js:2038-2060`）。
 *
 * 判据（决策 C17 / 裁定 9；2026-09-25 F2 重排为**先看有没有接手人**，同日 R-BO-1 把
 * 「有没有接手人」改用**订单状态**表达，判据函数 `TAKER_PERF_BRANCH` **只在一处**）：
 * - 非池内（不是 `UNASSIGNED` / `WAITING_GRAB`）⇒ `changeThrowFee`，点击开绩效弹层（**不看抛没抛过**）；
 * - 池内两态且 `hall_publisher_name === ''`（**从未抛过大厅**）⇒ `changeThrowFeeEdit` +
 *   标题不含 `%` 的 prompt；
 * - 池内两态且曾抛过 ⇒ `changeThrowFee` + 标题含 `%` 的 prompt。
 * ⛔ 判据**不读** `taker_name`：该字段在无权查看时是常量哨兵 `无权查看`（R-BO-1）。
 * 期望值写**字面值**（E-0024），提示原文逐字取自
 * `.tools/recon/order-pages/recon-payment-status.md` §5.1。
 */
describe('T7 发单价列三支（spec §8 第 27 条）', () => {
  const TIP_EDIT = '该笔订单还未指派过接手人，可以直接修改抛单价作为记账用，点击修改'
  const TIP_THROWN = '预计该笔订单总抛单金额：66.00元，点击可以修改抛单价'

  it('①从未抛过（`hall_publisher_name === ""`）⇒ `changeThrowFeeEdit`、tips 逐字 ①、**没有** `title`', () => {
    const html = cell('_throw', {
      hall_price: '66.00',
      hall_publisher_name: '',
      status: 'UNASSIGNED',
      taker_name: '',
    })
    expect(html).toContain('class="changeThrowFeeEdit"')
    expect(html).toContain('lay-tips="' + TIP_EDIT + '"')
    // ⚠️ 刻意**不**给 title：那句原文断言「还未指派过接手人」，而我们的判据是「从未抛过大厅」
    //    ⇒ 对 `/assign` 过的单显示它会是一句假话（spec §9 的 V2）。
    expect(html).not.toContain('title=')
    expect(html).toContain('data-fee="66.00"')
    expect(html).toContain('data-branch="edit"')
    expect(html).toContain('>66.00</a>')
  })

  it('②抛过且**当前在大厅** ⇒ `changeThrowFee`、tips 与 title 逐字 ②', () => {
    const html = cell('_throw', {
      hall_price: '66.00',
      hall_publisher_name: 'publisher1',
      status: 'WAITING_GRAB',
      taker_name: '',
    })
    expect(html).toContain('class="changeThrowFee"')
    expect(html).toContain('lay-tips="' + TIP_THROWN + '"')
    expect(html).toContain('title="' + TIP_THROWN + '"')
    expect(html).toContain('data-fee="66.00"')
    expect(html).toContain('data-branch="prompt"')
  })

  it('③抛过 + 不在大厅 + 已有人接 ⇒ 仍是 `changeThrowFee`（形态同②，点击行为由 T7 的用例断）', () => {
    const html = cell('_throw', {
      hall_price: '66.00',
      hall_publisher_name: 'publisher1',
      status: 'IN_PROGRESS',
      taker_name: '打手一',
    })
    expect(html).toContain('class="changeThrowFee"')
    expect(html).toContain('lay-tips="' + TIP_THROWN + '"')
    expect(html).toContain('title="' + TIP_THROWN + '"')
    expect(html).toContain('data-branch="dialog"')
  })

  /**
   * ⭐ R-BO-1反向对照：判据改用**订单状态**表达「有没有接手人」，⛔ 不读 `taker_name`。
   *
   * 池内两态（`UNASSIGNED` / `WAITING_GRAB`）在**库里**恒无接单人（D-0032），只有这一档才是
   * 「没接手人」；非池内一律算有接手人。三条对照按新语义：
   * ①池内 + `hall==''` ⇒ `edit`；②池内 + `hall!=''` ⇒ `prompt`；
   * ③非池内 + `hall==''` ⇒ `dialog`（业务方那条 bug：直接指派、没抛过）。
   */
  it('R-BO-1 反向对照：池内两态由「抛没抛过」分 edit/prompt；非池内 ⇒ dialog', () => {
    // ① 池内（UNASSIGNED）+ 从未抛过
    expect(TAKER_PERF_BRANCH({ hall_publisher_name: '', status: 'UNASSIGNED' })).toBe('edit')
    // ② 池内（WAITING_GRAB）+ 抛过
    expect(TAKER_PERF_BRANCH({ hall_publisher_name: 'p', status: 'WAITING_GRAB' })).toBe('prompt')
    // ③ 非池内 + 从未抛过（业务方实测那两笔直接指派的单）
    expect(TAKER_PERF_BRANCH({ hall_publisher_name: '', status: 'IN_PROGRESS' })).toBe('dialog')
  })

  /**
   * ⭐ F2回归：判据**先看有没有接手人**，再看抛没抛过。
   *
   * 业务方实测两笔单（`IN_PROGRESS` / `COMPLETED`）**有人接手、但从未抛过大厅**
   * （`hall_publisher_name` 为空，直接指派）⇒ 旧顺序把「抛没抛过」排在前面，这两笔落进
   * ①支，点发单价弹出的是「改发单价」prompt。本用例就是那两笔的形状。
   */
  it('F2：有接手人（非池内）+ 从未抛过大厅 ⇒ `dialog`（旧顺序会落 `edit`）', () => {
    const d = { hall_publisher_name: '', status: 'IN_PROGRESS' }
    expect(TAKER_PERF_BRANCH(d)).toBe('dialog')
    // 分支归属落到渲染上：③支与②支共用 `changeThrowFee`（`data-branch` 才区分点击行为）。
    const html = cell('_throw', { hall_price: '80.00', ...d, taker_name: '测试打手乙' })
    expect(html).toContain('data-branch="dialog"')
    expect(html).toContain('class="changeThrowFee"')
    expect(html).not.toContain('class="changeThrowFeeEdit"')
  })

  /**
   * ⭐ R-BO-1：载荷里的 `taker_name` 对**无权查看**的角色是常量 `无权查看`
   * （`OrderListSerializer.get_taker_name` 先判权限再判值）⇒ 「`taker_name !== ''`」会被当成
   * 「有接手人」。判据改用订单状态后哨兵不再参与判断，但结果仍要对：
   * ④非池内 + 哨兵 ⇒ `dialog`（那一档本来就有接手人）；⑤池内 + 哨兵 ⇒ `prompt`（**本次要防的回归**）。
   *
   * ⑤ 把判据改回 `taker_name !== ''` 就红。
   */
  it('R-BO-1：哨兵「无权查看」不再是判据（④非池内 ⇒ dialog；⑤池内 ⇒ prompt）', () => {
    const denied = '无权查看'
    // ④非池内 + 哨兵：哨兵那一档对应「有接手人」⇒ 绩效层
    expect(
      cell('_throw', {
        hall_price: '66.00',
        hall_publisher_name: '',
        status: 'IN_PROGRESS',
        taker_name: denied,
      }),
    ).toContain('data-branch="dialog"')
    // ⑤池内 + 哨兵：库里没有接单人 ⇒ 改发单价 prompt（**不是**绩效层）
    expect(
      cell('_throw', {
        hall_price: '66.00',
        hall_publisher_name: 'p',
        status: 'WAITING_GRAB',
        taker_name: denied,
      }),
    ).toContain('data-branch="prompt"')
  })

  it('三支的金额都走 `money()`（`hall_price` 为 `null` ⇒ `0.00`）', () => {
    expect(cell('_throw', { hall_price: null, hall_publisher_name: '' })).toContain('>0.00</a>')
    expect(cell('_throw', { hall_price: null, hall_publisher_name: 'p' })).toContain('>0.00</a>')
    expect(
      cell('_throw', { hall_price: null, hall_publisher_name: 'p', status: 'IN_PROGRESS', taker_name: '打手一' }),
    ).toContain('>0.00</a>')
  })
})

/**
 * ⭐ **2026-10-21（钱包发薪线）：「发单价」列的绿色「结」标记**。
 *
 * 对标 `throwPriceRow`（`orderlist.js:2038-2044`）：单号有「已与打手结算」痕迹时，在金额**前**
 * 画 `<a class="lableb" style="background-color:#5FB878" lay-tips="…">结</a>`
 * （`let color = '#5FB878'` L2039、`let span = '结'` L2041、tips 原文 L2040）。
 *
 * 判据 = 载荷的 `taker_settled`（后端 `OrderListSerializer.taker_settled` = 该单 MAIN 绩效行
 * 的 `Performance.is_settled` 三字段并集，相关子查询注解）。⛔ 决策 C6 / spec §9-3 的
 * 「结」标记按 `taker_settled` 画。抛单次数与 `recFee > price` 转红仍未做。
 *
 * 期望值写**字面值**（E-0024）：颜色 / 单字 / tips 原文都逐字来自对标，不从页面常量自指。
 */
describe('⭐ 「发单价」列的「结」标记（2026-10-21，钱包发薪线）', () => {
  const TIP = '该订单已与打手结算了（详细的结算明细请到财务运营/绩效账单里查询）'

  it('已结账（`taker_settled === true`）⇒ 金额**前**画「结」：`.lableb` + 底 `#5FB878` + 白字', () => {
    const html = cell('_throw', {
      hall_price: '66.00',
      hall_publisher_name: 'publisher1',
      status: 'COMPLETED',
      taker_name: '打手一',
      taker_settled: true,
    })
    // 形状 / 尺寸 / 间距复用页内 `.lableb`（白字、padding 2px 4px、圆角 3px、右间距 2px）。
    expect(html).toContain('class="lableb"')
    expect(html).toContain('style="background-color:#5FB878"')
    expect(html).toContain('lay-tips="' + TIP + '"')
    // 本页 vendored layui 里 `lay-tips` 不渲染 ⇒ 同时拼 `title`（与收款状态标记的 V1 同一处置）。
    expect(html).toContain('title="' + TIP + '"')
    expect(html).toContain('>结</a>')
    // 标记在**金额前**；金额颜色仍是 `#ff6a00`（`#5FB878` 只归标记，⛔ 不挪到金额上）。
    expect(html.indexOf('>结</a>')).toBeLessThan(html.indexOf('>66.00</a>'))
    expect(html).toContain('style="color:#ff6a00"')
  })

  it('未结账（`taker_settled === false`）⇒ **不画**标记，金额照旧', () => {
    const html = cell('_throw', {
      hall_price: '66.00',
      hall_publisher_name: 'publisher1',
      status: 'COMPLETED',
      taker_name: '打手一',
      taker_settled: false,
    })
    expect(html).not.toContain('>结</a>')
    expect(html).not.toContain('#5FB878')
    expect(html).toContain('>66.00</a>')
  })

  it('①支（从未抛过）同一判据：已结账照样在金额前画标记', () => {
    const html = cell('_throw', {
      hall_price: '66.00',
      hall_publisher_name: '',
      status: 'UNASSIGNED',
      taker_settled: true,
    })
    expect(html).toContain('class="lableb"')
    expect(html.indexOf('>结</a>')).toBeLessThan(html.indexOf('>66.00</a>'))
  })
})

/**
 * U9（D-0147 批 E / spec §2.3 · §5-A7）：对标管理订单页 JS 在 2026-09-24 把
 * **发单价与接单价两列**的列宽从 `78` 改成 `70`（PC 也受影响）。
 *
 * ⚠️ 期望值写**字面值 `70`**（E-0024：不引用页面常量、不自指），改回 `78` 时这条必红。
 * ⚠️ 「实收款」那一列仍是 `78`——对标只动了发单价 / 接单价两列，本用例不把它一起改。
 */
describe('U9 列宽：发单价 / 接单价都是 70（对标 09-24 实测）', () => {
  it('两列的 `width` 都是 70（改回 78 必红）', () => {
    const cols = buildOrderCols(
      () => 'G',
      () => '',
    )
    const widths: Record<string, unknown> = {}
    for (const field of ['_throw', '_price'] as const) {
      const col = cols.find((c) => c.field === field)
      expect(col, `列定义里找不到 field=${field}`).toBeDefined()
      widths[field] = col!.width
    }
    expect(widths).toEqual({ _throw: 70, _price: 70 })
  })
})
