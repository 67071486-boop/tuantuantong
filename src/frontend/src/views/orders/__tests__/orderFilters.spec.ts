import { describe, expect, it } from 'vitest'
import {
  ADVANCED_FILTER_KEY,
  auditActionLabel,
  emptyFilterForm,
  emptyStateText,
  hasActiveFilters,
  readAdvancedExpanded,
  toSearchParams,
  writeAdvancedExpanded,
  type StorageLike,
} from '../orderFilters'

function fakeStorage(seed: Record<string, string> = {}): StorageLike & { data: Record<string, string> } {
  const data: Record<string, string> = { ...seed }
  return {
    data,
    getItem: (k: string) => data[k] ?? null,
    setItem: (k: string, v: string) => {
      data[k] = v
    },
  }
}

describe('检索参数组装', () => {
  it('空筛选只送分页', () => {
    expect(toSearchParams(emptyFilterForm(), 1, 20)).toEqual({ page: 1, page_size: 20 })
  })

  it('is_urgent:false 必须送出去（否则「不加急」筛不出来）', () => {
    const form = emptyFilterForm()
    form.is_urgent = false
    expect(toSearchParams(form, 1, 20)).toEqual({ page: 1, page_size: 20, is_urgent: false })
  })

  it('时间范围按本地日取整，送带时区偏移的 ISO 8601', () => {
    const form = emptyFilterForm()
    form.date_from = '2026-09-20'
    form.date_to = '2026-09-20'
    const params = toSearchParams(form, 2, 50)
    const offsetMin = -new Date(2026, 8, 20, 0, 0, 0).getTimezoneOffset()
    const sign = offsetMin < 0 ? '-' : '+'
    const abs = Math.abs(offsetMin)
    const pad = (n: number) => String(n).padStart(2, '0')
    const offset = `${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`

    expect(params.date_from).toBe(`2026-09-20T00:00:00${offset}`)
    expect(params.date_to).toBe(`2026-09-20T23:59:59${offset}`)
    expect(params.page).toBe(2)
    expect(params.page_size).toBe(50)
  })
})

describe('空态文案必须区分「没有匹配」与「还没有订单」', () => {
  it('没有筛选且没有数据 → 还没有订单', () => {
    expect(emptyStateText(0, emptyFilterForm())).toBe('还没有订单')
  })

  it('筛过且没有数据 → 没有匹配的订单', () => {
    const form = emptyFilterForm()
    form.status = 'PAUSED'
    expect(emptyStateText(0, form)).toBe('没有匹配的订单')
  })

  it('有数据时没有空态文案', () => {
    expect(emptyStateText(3, emptyFilterForm())).toBe('')
  })

  it('is_urgent:false 也算「筛过」', () => {
    const form = emptyFilterForm()
    form.is_urgent = false
    expect(hasActiveFilters(form)).toBe(true)
  })

  it('下游控件被清空后不算「筛过」，也不把清空值送出去', () => {
    // 三个控件的「清空」写回值各不相同，都是在 element-plus 源码里读到的（见 orderFilters.ts 头部）：
    // el-select → `undefined`、el-date-picker → `null`、el-input → `''`。只判 `!== ''` / `!== null` 会让清空后的控件继续被当成「在筛」，于是空态永远显示「没有匹配的订单」（spec §4.2 要求两种空态可区分）。
    const form = emptyFilterForm()
    form.status = undefined
    form.game = undefined
    form.server = undefined
    form.is_urgent = undefined
    form.date_from = null
    form.date_to = null
    expect(hasActiveFilters(form)).toBe(false)
    expect(emptyStateText(0, form)).toBe('还没有订单')
    // 清空值一个都不能出现在检索参数里（尤其日期：`null` 曾会拼出 `new Date('nullT00:00:00')`）。
    expect(toSearchParams(form, 1, 20)).toEqual({ page: 1, page_size: 20 })
  })
})

describe('高级筛选展开状态的 localStorage 记忆', () => {
  it('没记过 → 收起', () => {
    expect(readAdvancedExpanded(fakeStorage())).toBe(false)
  })

  it('记了 1 → 展开；写 0 之后回到收起', () => {
    const store = fakeStorage({ [ADVANCED_FILTER_KEY]: '1' })
    expect(readAdvancedExpanded(store)).toBe(true)
    writeAdvancedExpanded(store, false)
    expect(store.data[ADVANCED_FILTER_KEY]).toBe('0')
    expect(readAdvancedExpanded(store)).toBe(false)
  })

  it('键名带页面前缀，不会与别的页面撞名', () => {
    expect(ADVANCED_FILTER_KEY).toBe('tt.orders.filters.advancedExpanded')
  })
})

describe('留痕动作名', () => {
  // ⚠️ 期望值一律写**字面中文**，不 import 生产端的常量（E-0024：自指断言在常量被改坏时不红）。
  it('三个新 code 翻成中文', () => {
    expect(auditActionLabel('order.transition')).toBe('状态变更')
    expect(auditActionLabel('order.change_taker_price')).toBe('改价')
    expect(auditActionLabel('performance.review')).toBe('绩效审核')
  })

  it('未登记的动作回退成原始 code（不崩、不空白）', () => {
    expect(auditActionLabel('order.view_credentials')).toBe('order.view_credentials')
    expect(auditActionLabel('SOMETHING_NEW')).toBe('SOMETHING_NEW')
    // 同名于 `Object.prototype` 的字符串也必须回退：`'toString' in {}` 为真（走的是原型链），
    // 用它当判据会拿到一个函数、渲染出 `function toString() { [native code] }`。
    expect(auditActionLabel('toString')).toBe('toString')
  })
})
