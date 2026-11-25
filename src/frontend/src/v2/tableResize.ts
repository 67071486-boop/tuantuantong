/** v2 列表「列宽拖拽」共享逻辑（业务方 第二轮之②「字段之间可以手动拉长拉宽」的表格版；三张表 V2MyOrders / V2Income / V2Hall 共用）。## 形态- 表头每列右缘一个 6px 热区（`.cv2-th-resizer`，th 内 absolute 定位，模板各自渲染）；- `mousedown` 拖动横向改该列宽——`.cv2-table` 是 `table-layout: fixed`，宽度由`<colgroup><col>` 分配（列定义在页面里，宽度状态由本模块持有）；- `dblclick` 恢复默认（有默认档回默认值；没有固定宽的弹性列摘掉宽度回弹性行为）；- 列宽按「页面名 + 列键」存 localStorage（键 = `tt.v2.table.<page>`），下次进页还原。## 判据- 拖拽宽度钳制在 [COL_MIN_WIDTH, COL_MAX_WIDTH]（太窄挤烂表头、太宽一列吞整行）；- 读 localStorage 时丢弃损坏 JSON 与越界值（存储可能是别人写的、也可能被手改）；
 * - 写 localStorage 失败（隐私模式禁写）不抛错——本次会话拖拽仍然生效，只是不记忆；- 拖拽期间 `body.cv2-col-resizing`（col-resize 光标 + 禁选中文本），mouseup 收口。spec 钉见 `__tests__/tableResize.spec.ts`（手柄渲染 / 拖后写 localStorage / 双击复原 / 钳制）。 */
import { reactive } from 'vue'

/** 列宽表：列键 → px；`undefined` = 该列不设固定宽、按剩余空间弹性伸缩。 */
export type ColumnWidths = Record<string, number | undefined>

/** 列默认宽（列键 → px）：只有给默认值的列会被钉宽，其余列弹性分摊剩余空间。 */
export type ColumnDefaults = Record<string, number>

/** 拖拽宽度下限（px）：低于它表头与 44px 单行截断都会挤烂。 */
export const COL_MIN_WIDTH = 44
/** 拖拽宽度上限（px）：防一列吞掉整行（面板内容区常见 900–1200px）。 */
export const COL_MAX_WIDTH = 600

function storageKeyOf(pageKey: string): string {
  // ⭐ 存储版次 v2：固定列/弹性列模型换过档 ⇒ 旧会话拖拽存宽整批作废（一次性）。
  return `tt.v2.table.v2.${pageKey}`
}

/** 读 localStorage（键 = `tt.v2.table.<page>`）；损坏 JSON / 非数 / 越界值一律丢弃。 */
export function loadColumnWidths(pageKey: string): Record<string, number> {
  try {
    const raw = localStorage.getItem(storageKeyOf(pageKey))
    if (raw === null) return {}
    const parsed: unknown = JSON.parse(raw)
    if (parsed === null || typeof parsed !== 'object') return {}
    const out: Record<string, number> = {}
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (
        typeof value === 'number' &&
        Number.isFinite(value) &&
        value >= COL_MIN_WIDTH &&
        value <= COL_MAX_WIDTH
      ) {
        out[key] = Math.round(value)
      }
    }
    return out
  } catch {
    return {}
  }
}

/** 写 localStorage（禁写环境静默——拖拽本次会话仍生效，只是不记忆）。 */
export function saveColumnWidths(pageKey: string, widths: Record<string, number>): void {
  try {
    localStorage.setItem(storageKeyOf(pageKey), JSON.stringify(widths))
  } catch {
    // 存储被禁用：不做任何事（与折叠区 localStorage 的兜底同口径）。
  }
}

/** 一张表的列宽状态 + 交互（页面 `<script setup>` 直接调用）。
 * @param pageKey 页面名（进 localStorage 键，如 `'my-orders'`）
 * @param defaults 列默认宽（键 = 页面模板里的列键；不给默认值的列保持弹性宽） */
export function useColumnWidths(pageKey: string, defaults: ColumnDefaults = {}) {
  // ⭐ 持久化记录按「当前 defaults 里存在的键」过滤——某列从固定改弹性后，
  // 旧会话拖拽存下的数字不再把它钉死（否则改档不生效，业务方看到的还是旧宽）。
  const saved = loadColumnWidths(pageKey)
  const scoped: ColumnWidths = {}
  for (const [k, v] of Object.entries(saved)) {
    if (defaults[k] !== undefined) scoped[k] = v
  }
  const widths = reactive<ColumnWidths>({ ...defaults, ...scoped })
  /** 在途拖拽（null = 没在拖）。 */
  let drag: { key: string; startX: number; startW: number } | null = null

  /** 持久化当前所有固定宽（undefined 的弹性列不写）。 */
  function persist(): void {
    const snapshot: Record<string, number> = {}
    for (const [key, w] of Object.entries(widths)) {
      if (typeof w === 'number') snapshot[key] = w
    }
    saveColumnWidths(pageKey, snapshot)
  }

  /** `<col>` 的动态 style（无固定宽 = undefined ⇒ 该列按剩余空间分摊）。 */
  function colStyle(key: string): { width: string } | undefined {
    const w = widths[key]
    return typeof w === 'number' ? { width: `${w}px` } : undefined
  }

  /** 双击恢复默认：有默认档回默认值，否则摘掉固定宽（回弹性列）。 */
  function resetColumn(key: string): void {
    const def = defaults[key]
    if (def === undefined) {
      widths[key] = undefined
      // reactive 记录 delete 语义：置 undefined 即可（colStyle/persist 都只认 number）。
    } else {
      widths[key] = def
    }
    persist()
  }

  function onMove(event: MouseEvent): void {
    if (drag === null) return
    const next = drag.startW + (event.clientX - drag.startX)
    widths[drag.key] = Math.min(COL_MAX_WIDTH, Math.max(COL_MIN_WIDTH, Math.round(next)))
  }

  function onUp(): void {
    if (drag === null) return
    drag = null
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)
    document.body.classList.remove('cv2-col-resizing')
    persist()
  }

  /** 手柄 `mousedown`：记起点（列当前宽 = 已设固定宽优先，否则量 th 实宽——弹性列
   * 状态里没有值，只能从 DOM 拿），挂 document 级 move/up 监听并给 body 加拖拽态类。 */
  function startResize(event: MouseEvent, key: string): void {
    if (event.button !== 0) return
    event.preventDefault()
    const th = (event.currentTarget as HTMLElement).closest('th')
    const current = widths[key] ?? (th !== null ? th.offsetWidth : 0)
    drag = { key, startX: event.clientX, startW: current }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
    document.body.classList.add('cv2-col-resizing')
  }

  return { widths, colStyle, startResize, resetColumn }
}
