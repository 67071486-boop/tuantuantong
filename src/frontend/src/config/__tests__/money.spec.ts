import { describe, expect, it } from 'vitest'
import {
  MONEY_FIELD_LABELS,
  ORDER_MONEY_FIELDS,
  formatMoney,
  isBlankLikePython,
  validateMoneyInput,
} from '../money'

describe('金额格式化', () => {
  it('空值与非数字显示 —', () => {
    expect(formatMoney(null)).toBe('—')
    expect(formatMoney(undefined)).toBe('—')
    expect(formatMoney('')).toBe('—')
    expect(formatMoney('abc')).toBe('—')
  })

  it('固定两位小数', () => {
    expect(formatMoney('12.5')).toBe('12.50')
    expect(formatMoney(12)).toBe('12.00')
    expect(formatMoney('12.345')).toBe('12.35')
  })

  it('允许负数（退款 / 冲抵，D-0015）', () => {
    expect(formatMoney('-199.5')).toBe('-199.50')
    expect(formatMoney(-0.05)).toBe('-0.05')
  })

  it('6 个金额字段与后端 models.MONEY_FIELDS 一致，且每个都有中文名', () => {
    expect(ORDER_MONEY_FIELDS).toEqual([
      'total_price',
      'received_price',
      'platform_price',
      'hall_price',
      'refund_fee',
      'fee',
    ])
    expect(MONEY_FIELD_LABELS).toEqual({
      total_price: '订单总价',
      received_price: '接单实收款',
      platform_price: '发平台价格',
      hall_price: '发内部抢单大厅价',
      refund_fee: '累计退款额',
      fee: '产生的手续费',
    })
    // 字段数写死成 6：`toEqual` 对数组长度本来就敏感，但**同时钉住数字**才能让
    // 「有人把删掉的两个保证金字段加回来」在标题与数据两处都看得见。
    expect(ORDER_MONEY_FIELDS).toHaveLength(6)
    expect(Object.keys(MONEY_FIELD_LABELS)).toHaveLength(6)
  })
})

// 标题只声明这一组用例**真正钉住**的东西，且**枚举**例外——不写「逐条对齐」这种绝对句：
//  1. 空白 = 不填（本函数一律 ok；「必填」逐路径由调用方裁决）；
//  2. `.5` / `1.` / `+12` / `1e3` / `5e-1` / `1_000` 是**刻意比后端更严**的形状（后端实测都合法）。
// 上一版标题还带着「其余判据与后端对齐」，被本块的 `1_000` 用例证伪（同类说法早已被 `1e3` 证伪过）。
// **所以标题只枚举，不声称穷尽。**
describe('金额输入校验（空白 = 不填；`.5`/`1.`/`+12`/`1e3`/`5e-1`/`1_000` 刻意更严）', () => {
  it('空串 = 不填，合法（「是不是必填」由调用方按路径判断，见 money.ts 文件头）', () => {
    expect(validateMoneyInput('')).toEqual({ ok: true })
  })

  it('负数合法——不得加「必须 ≥ 0」的校验', () => {
    expect(validateMoneyInput('-199.50')).toEqual({ ok: true })
  })

  it('整数与一 / 两位小数合法', () => {
    expect(validateMoneyInput('0')).toEqual({ ok: true })
    expect(validateMoneyInput('12.5')).toEqual({ ok: true })
    expect(validateMoneyInput('-12.50')).toEqual({ ok: true })
  })

  it('非数字被拒', () => {
    expect(validateMoneyInput('abc')).toEqual({
      ok: false,
      msg: '金额格式不正确：只允许数字与最多两位小数（可为负）',
    })
    expect(validateMoneyInput('1,000')).toEqual({
      ok: false,
      msg: '金额格式不正确：只允许数字与最多两位小数（可为负）',
    })
    expect(validateMoneyInput('1e3')).toEqual({
      ok: false,
      msg: '金额格式不正确：只允许数字与最多两位小数（可为负）',
    })
  })

  it('超过两位小数被拒', () => {
    expect(validateMoneyInput('12.345')).toEqual({
      ok: false,
      msg: '金额格式不正确：只允许数字与最多两位小数（可为负）',
    })
  })

  // 标题里枚举的 6 个「更严」形状**逐条**断言（此前只断言了 `1e3`，标题与用例对不上）。
  // 后端实测：它们**都合法**（DRF 直接 `decimal.Decimal(data)`，没有格式正则；`Decimal('1_000')`
  // 就是 `Decimal('1000')`）。前端内联拒是刻意的——提示更早也更清楚。
  it('刻意比后端更严的 6 个形状逐个被拒（后端实测都合法）', () => {
    for (const shape of ['.5', '1.', '+12', '1e3', '5e-1', '1_000']) {
      expect(validateMoneyInput(shape)).toEqual({
        ok: false,
        msg: '金额格式不正确：只允许数字与最多两位小数（可为负）',
      })
    }
  })

  // 以下两组量级断言对着**运行中的** PriceChangeSerializer 实测过：
  // '12345678901' → max_whole_digits（「请确保小数点前不超过 10 个数字。」）
  // '99999999999.99' → max_digits（「请确保总计不超过 12 个数字。」）
  it('整数部分超过 10 位被拒（对齐后端 max_whole_digits=10）', () => {
    expect(validateMoneyInput('12345678901')).toEqual({
      ok: false,
      msg: '金额格式不正确：小数点前不能超过 10 位数字',
    })
    expect(validateMoneyInput('-12345678901')).toEqual({
      ok: false,
      msg: '金额格式不正确：小数点前不能超过 10 位数字',
    })
  })

  it('总位数超过 12 位被拒（对齐后端 max_digits=12）', () => {
    expect(validateMoneyInput('99999999999.99')).toEqual({
      ok: false,
      msg: '金额格式不正确：总计不能超过 12 位数字',
    })
  })

  it('量级上限的边界合法：10 位整数 + 2 位小数，且前导零不计入位数', () => {
    expect(validateMoneyInput('1234567890.99')).toEqual({ ok: true })
    expect(validateMoneyInput('-1234567890.99')).toEqual({ ok: true })
    // 后端实测 Decimal('0001234567890.99') 的 digits = 12 → 合法；逐字符数会错算成 13/15。
    expect(validateMoneyInput('0001234567890.99')).toEqual({ ok: true })
  })

  it('首尾空白被忽略（后端 smart_str(data).strip() 接受 " 12 "）', () => {
    expect(validateMoneyInput(' 12 ')).toEqual({ ok: true })
    expect(validateMoneyInput('\t-199.50\n')).toEqual({ ok: true })
    expect(validateMoneyInput('  ')).toEqual({ ok: true })
  })

  // ---- 空白集的精确对齐（U+FEFF）----
  // JS 的 trim/\s 比 Python 的 str.strip() **多一个 U+FEFF**（BOM），而 Python 的空白集
  // 比 JS **多 U+001C-U+001F 与 U+0085**（穷举 0..U+10FFFF 得到，见 money.ts 的 PY_STRIP_CHARS）。
  // 多剥 = 前端比后端松，是危险方向；少剥 = 更严，是噪音。两边都要对齐。
  it('U+FEFF（BOM）不算空白：前端不得剥掉它，否则比后端松', () => {
    // 后端实测：'\ufeff12' → invalid（'\ufeff'.isspace() 在 Python 里是 False）。
    // （这里写成**转义序列**，不把 U+FEFF 的实体放进注释——它不可见，会让人误以为注释是坏的。）
    expect(validateMoneyInput('\ufeff12')).toEqual({
      ok: false,
      msg: '金额格式不正确：只允许数字与最多两位小数（可为负）',
    })
    // 后端实测：只给一个 BOM，改价与录单都拒（录单的 allow_null 要求 strip() 后为空串，BOM 不满足）。
    expect(validateMoneyInput('\ufeff')).toEqual({
      ok: false,
      msg: '金额格式不正确：只允许数字与最多两位小数（可为负）',
    })
  })

  it('U+FEFF 不能再绕过长度上限（否则长度判据形同虚设）', () => {
    // 原始 1001 字符。若把 U+FEFF 当空白，裁剪后正好 1000 → 会被放行；后端实测 max_string_length。
    // 因为 U+FEFF 不被裁剪，这里命中的也正是**长度**那条判据（与后端同一个原因，不只是同一个结论）。
    expect(validateMoneyInput('\ufeff' + '0'.repeat(999) + '1')).toEqual({
      ok: false,
      msg: '金额格式不正确：长度不能超过 1000 个字符',
    })
  })

  it('Python 空白集比 JS 多的码点（U+001C-U+001F、U+0085）也照剥，否则前端会更严', () => {
    // 后端实测这三个都合法（Python strip() 会把它们去掉）。
    expect(validateMoneyInput('\u001c12')).toEqual({ ok: true })
    expect(validateMoneyInput('\u008512')).toEqual({ ok: true })
    expect(validateMoneyInput('\u008512\u001f')).toEqual({ ok: true })
  })

  // 后端实测：长度 1000 → 合法；1001 / 1002 → max_string_length（「字符串值太长。」）。
  // 这组是本轮补的第二处「前端比后端松」——此前 1001 个字符的 '0…01' 会被放行。
  it('超过 1000 个字符被拒（对齐后端 MAX_STRING_LENGTH=1000）', () => {
    expect(validateMoneyInput('0'.repeat(1000) + '1')).toEqual({
      ok: false,
      msg: '金额格式不正确：长度不能超过 1000 个字符',
    })
    expect(validateMoneyInput('0'.repeat(1000) + '.99')).toEqual({
      ok: false,
      msg: '金额格式不正确：长度不能超过 1000 个字符',
    })
  })

  it('恰好 1000 个字符合法，且长度按 trim 后计算（与后端 strip 后判长一致）', () => {
    expect(validateMoneyInput('0'.repeat(999) + '1')).toEqual({ ok: true })
    // 原始 1001 字符、trim 后 1000 —— 后端 strip 后也是 1000，两边都放行。
    expect(validateMoneyInput(' ' + '0'.repeat(999) + '1')).toEqual({ ok: true })
    // 原始 1002 字符、trim 后 1001 —— 两边都拒。
    expect(validateMoneyInput(' ' + '0'.repeat(1000) + '1')).toEqual({
      ok: false,
      msg: '金额格式不正确：长度不能超过 1000 个字符',
    })
  })
})

describe('isBlankLikePython：必填判据用 Python 的空白集，不是 JS trim', () => {
  // 这两个码点是 JS `trim()` 与 Python `str.strip()` 的**分歧点**，最能钉住「用的不是 JS trim」。
  it('Python 会裁、JS 不裁的 U+0085 算空', () => {
    expect(isBlankLikePython('\u0085')).toBe(true)
    expect('\u0085'.trim()).toBe('\u0085')
  })

  it('JS 会裁、Python 不裁的 U+FEFF 不算空', () => {
    expect(isBlankLikePython('\ufeff')).toBe(false)
    expect('\ufeff'.trim()).toBe('')
  })

  it('空串算空、普通文本不算空', () => {
    expect(isBlankLikePython('')).toBe(true)
    expect(isBlankLikePython('  \t\n\u3000')).toBe(true)
    expect(isBlankLikePython('深渊满星')).toBe(false)
    expect(isBlankLikePython(' 深渊满星 ')).toBe(false)
  })
})

describe('空白集内容（穷举 0..U+10FFFF 与语料对拍）', () => {
  // ⚠️ 这份期望值是**手写出来的**（不是从实现里读回来的），所以实现多一个/少一个码点都会红。
  // 背景：此前这一组只有「分歧码点」的断言（U+0085 / U+FEFF），丢一个 U+00A0 或 U+2007 **不会**变红；
  // 评审 Round 2 要求的正是这条内容断言（本报告曾声称「早有语料对拍」——那是错的，从未存在过）。
  const EXPECTED = [
    0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0x85, 0xa0, 0x1680, 0x2000,
    0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a, 0x2028,
    0x2029, 0x202f, 0x205f, 0x3000,
  ]

  it('穷举全部码点：算空的恰好是这 29 个（多一个少一个都红）', () => {
    expect(EXPECTED).toHaveLength(29)
    const expected = new Set(EXPECTED)
    const mismatched: number[] = []
    for (let cp = 0; cp <= 0x10ffff; cp++) {
      if (isBlankLikePython(String.fromCodePoint(cp)) !== expected.has(cp)) mismatched.push(cp)
    }
    // 断言整个数组（不是逐条 expect），失败时能一次看到**全部**不一致的码点。
    expect(mismatched).toEqual([])
  })

  it('几个已知的「不是空白」样本（防止把集合放宽）', () => {
    for (const s of ['0', 'x', '　x', 'x　', '\u200b', '\u180e', '\ufeff', '\u2060']) {
      expect(isBlankLikePython(s)).toBe(false)
    }
  })
})
