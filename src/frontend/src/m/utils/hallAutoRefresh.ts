/** 大厅刷新档位和存储键与桌面端共用。页面切换和轮询生命周期由 `HallPage` 管理。 */

/** 「关闭」那一档的值（`0` = 关闭；`AUTO_REFRESH_STEPS_MS` 里的值都是**启用**的档位）。 */
export const AUTO_REFRESH_OFF = 0

/** 允许的自动刷新档位（毫秒）——桌面 :241 逐值（1 / 2 / 5 / 10 / 30 / 60 秒）。 */
export const AUTO_REFRESH_STEPS_MS = [1000, 2000, 5000, 10000, 30000, 60000] as const

/** 在途互斥的活性兜底（毫秒）——桌面 :235 逐值。 */
export const AUTO_REFRESH_ALIVE_MS = 30000

/** `localStorage` 的键——与桌面共用同一枚（:280）；读 / 写都 try/catch（:277-298）。 */
export const AUTO_REFRESH_STORAGE_KEY = 'tt.hall.autoRefreshMs'

/**
 * 缓存里那个值能算哪一档（**fail-safe**，桌面 :253-261 语义移植）：`Number(raw)` 化后**严格等于**某个档位才算数，
 * 否则一律 `AUTO_REFRESH_OFF`，⛔ 不抛、⛔ **不截断成邻近档**（把 500 当 1000 就是替用户改了设置）。`'1000'`（`localStorage` 恒是字符串）**算** 1000，前后空白照 `Number` 语义化掉（以代码行为为准，E-0003）；化不出档位值（`'1000abc'` / 负数 / 小数）一律不算。
 */
export function normalizeAutoRefreshMs(raw: unknown): number {
  if (typeof raw !== 'number' && typeof raw !== 'string') return AUTO_REFRESH_OFF
  // ⚠️ `Number('')` 是 0、`Number(null)` 是 0 ⇒ 先按「空」挡掉（E-0021 同族：0 与「空」不能混
  //    在一句真值判据里，虽然这里两者的结果恰好都是「关闭」）。
  if (typeof raw === 'string' && raw.trim() === '') return AUTO_REFRESH_OFF
  const n = Number(raw)
  if (!Number.isFinite(n)) return AUTO_REFRESH_OFF
  return (AUTO_REFRESH_STEPS_MS as readonly number[]).includes(n) ? n : AUTO_REFRESH_OFF
}

/** 安全读 `localStorage`（抛异常时当「没存过」⇒ 调用方按关闭处置；桌面 :282-289）。 */
export function readStoredAutoRefreshMs(): number {
  try {
    return normalizeAutoRefreshMs(window.localStorage.getItem(AUTO_REFRESH_STORAGE_KEY))
  } catch {
    return AUTO_REFRESH_OFF
  }
}

/** 安全写 `localStorage`（抛异常时静默放弃——记不住偏好远好过打断用户操作；桌面 :291-298）。 */
export function writeStoredAutoRefreshMs(ms: number): void {
  try {
    window.localStorage.setItem(AUTO_REFRESH_STORAGE_KEY, String(ms))
  } catch {
    /* 隐私模式 / 配额满：只影响「下次还记得」，不影响本次设置 */
  }
}
