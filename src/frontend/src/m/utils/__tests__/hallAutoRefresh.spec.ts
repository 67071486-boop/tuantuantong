// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'

import {
  AUTO_REFRESH_ALIVE_MS,
  AUTO_REFRESH_OFF,
  AUTO_REFRESH_STEPS_MS,
  AUTO_REFRESH_STORAGE_KEY,
  normalizeAutoRefreshMs,
  readStoredAutoRefreshMs,
  writeStoredAutoRefreshMs,
} from '../hallAutoRefresh'

/**
 * 自动刷新**参数与存储**的单测（桌面 `order-insiderecorderlist.ts` 的同参移植——
 * 档位 / 关闭档 / 活性兜底 / 键名逐值对齐，出处行号见 `../hallAutoRefresh.ts` 文件头）。
 */

describe('AUTO_REFRESH 常量（与桌面逐值对齐）', () => {
  it('档位 = 1/2/5/10/30/60 秒；关闭档 0；活性兜底 30 秒；键名同桌面', () => {
    expect([...AUTO_REFRESH_STEPS_MS]).toEqual([1000, 2000, 5000, 10000, 30000, 60000])
    expect(AUTO_REFRESH_OFF).toBe(0)
    expect(AUTO_REFRESH_ALIVE_MS).toBe(30000)
    expect(AUTO_REFRESH_STORAGE_KEY).toBe('tt.hall.autoRefreshMs')
  })
})

describe('normalizeAutoRefreshMs（fail-safe 归一化，桌面 :253-261 语义）', () => {
  it("档位内的数字与数字串通过（'1000' 是 localStorage 的正常读入形状）", () => {
    expect(normalizeAutoRefreshMs(1000)).toBe(1000)
    expect(normalizeAutoRefreshMs('60000')).toBe(60000)
  })

  it("不在允许集合里一律当「关闭」：500 / 'abc' / 负数 / 小数 / 带非数字尾巴的串", () => {
    expect(normalizeAutoRefreshMs(500)).toBe(AUTO_REFRESH_OFF)
    expect(normalizeAutoRefreshMs('abc')).toBe(AUTO_REFRESH_OFF)
    expect(normalizeAutoRefreshMs(-1000)).toBe(AUTO_REFRESH_OFF)
    expect(normalizeAutoRefreshMs(2.5)).toBe(AUTO_REFRESH_OFF)
    expect(normalizeAutoRefreshMs('1000abc')).toBe(AUTO_REFRESH_OFF)
  })

  it("Number 化后恰等于档位的数字串算档位（' 1000' 去空白后就是 1000——与桌面代码同语义）", () => {
    // ⚠️ 桌面 `order-insiderecorderlist.ts:258` 的 `Number(raw)` 会去前后空白：`Number(' 1000')`
    //    本来就等于 1000（桌面文件头注释写「' 1000' 一律不算」是过头话，代码不是这么跑的；
    //    本移植以**代码行为**为准，E-0003：断言行为前先读源码）。
    expect(normalizeAutoRefreshMs(' 1000')).toBe(1000)
    expect(normalizeAutoRefreshMs('1000.0')).toBe(1000)
  })

  it('null / undefined / 空串 / 空白串 → 关闭（不抛）', () => {
    expect(normalizeAutoRefreshMs(null)).toBe(AUTO_REFRESH_OFF)
    expect(normalizeAutoRefreshMs(undefined)).toBe(AUTO_REFRESH_OFF)
    expect(normalizeAutoRefreshMs('')).toBe(AUTO_REFRESH_OFF)
    expect(normalizeAutoRefreshMs('   ')).toBe(AUTO_REFRESH_OFF)
  })
})

describe('存储读写（fail-safe）', () => {
  afterEach(() => {
    window.localStorage.removeItem(AUTO_REFRESH_STORAGE_KEY)
  })

  it('写入后可读回；读不到（没存过）→ 关闭', () => {
    expect(readStoredAutoRefreshMs()).toBe(AUTO_REFRESH_OFF)
    writeStoredAutoRefreshMs(5000)
    expect(readStoredAutoRefreshMs()).toBe(5000)
    expect(window.localStorage.getItem(AUTO_REFRESH_STORAGE_KEY)).toBe('5000')
  })

  it('存储里的非法值 → 读出为关闭（不截断成邻近档）', () => {
    window.localStorage.setItem(AUTO_REFRESH_STORAGE_KEY, '500')
    expect(readStoredAutoRefreshMs()).toBe(AUTO_REFRESH_OFF)
  })
})
