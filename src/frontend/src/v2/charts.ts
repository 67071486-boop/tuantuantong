/**
 * v2 图表底座。环形图按 Metabase 复刻规格对齐（2026-11-06）：8 色 Metabase 色板 + 名字哈希
 * 恒同色 + 「其他」合并（占比 <2.5% 候选 ≥2 才并；>8 段收前 7 + 其他）+ HTML 中心覆盖层
 * （悬停切片/图例切该块、窄容器只显数值）+ 悬停淡 30% 联动 + 深色排名卡；扇形整环不加白描边
 * （业务方拍板优先于 Metabase）。图表用色只能以 JS 常量住本文件：token 守卫只扫 carbon.css，
 * canvas 与 tooltip 读不到 CSS 变量。ECharts 是 vendored 3.6.2（⛔ npm 加装）；加载器照
 * `layui-island/echarts.ts` 同款逻辑复刻（同 script id 幂等、失败粘性），⛔ 不 import
 * layui-island——改加载口径两处同改。createBarLineCombo（双轴柱线）用色与 carbon.css
 * `--tt2-dsh-*` 同值两侧并存（改值两处同改）。
 */
const BASE = import.meta.env.BASE_URL || '/'

/** 注入的 `<script>` 的 `id`（与 `layui-island/echarts.ts` 同值：两份加载器彼此幂等）。 */
export const ECHARTS_SCRIPT_ID = 'vendor-echarts'

/** 脚本地址（`BASE_URL` + `vendor/echarts.min.js`；写死 `/vendor/…` 会在子路径部署下 404）。 */
export const ECHARTS_SCRIPT_SRC = `${BASE}vendor/echarts.min.js`

/** 脚本加载失败时的唯一中文文案（页面 toast 它）。 */
export const ECHARTS_LOAD_ERROR = '图表库加载失败，请刷新页面重试'

/** `chart.on(eventName, handler)` 的事件参数（只声明本底座读的键；⛔ 不抄整份 d.ts）。 */
export type EChartsEventParams = {
  seriesIndex?: number
  dataIndex?: number
}

/** ECharts 实例上本底座用到的方法（只声明用到的；⛔ 不抄整份 d.ts）。 */
export type EChartsInstance = {
  /** 第二个参数缺省（合并模式）：换数据集时保留过渡动画。 */
  setOption: (option: unknown, notMerge?: boolean) => void
  /** 悬停联动（淡化其余 + 中心切该块）注册回调用；3.6.2 的 `chart.on`。 */
  on?: (eventName: string, handler: (params: EChartsEventParams) => void) => void
  dispatchAction?: (payload: unknown) => void
  resize?: () => void
  dispose?: () => void
}

/** `window.echarts` 上本底座用到的方法。 */
export type EChartsNamespace = {
  init: (element: HTMLElement) => EChartsInstance
}

/** 单例：同一页里只插一次脚本、只解析一次；失败粘性（不复位——节点还挂在 DOM 里）。 */
let promise: Promise<EChartsNamespace> | null = null

/** 加载 ECharts，resolve `window.echarts`。必须在页面标记已进 DOM 之后调用（init 要量尺寸）。 */
export function loadECharts(): Promise<EChartsNamespace> {
  if (promise) return promise
  promise = new Promise<EChartsNamespace>((resolve, reject) => {
    const existing = document.getElementById(ECHARTS_SCRIPT_ID) as HTMLScriptElement | null
    const script = existing ?? document.createElement('script')
    script.id = ECHARTS_SCRIPT_ID
    script.addEventListener('load', () => {
      const echarts = (window as { echarts?: EChartsNamespace }).echarts
      // 脚本 200 但没挂上 `window.echarts` 同样按失败处理，⛔ 不 resolve undefined 让页面在 init 处崩。
      if (echarts === undefined || typeof echarts.init !== 'function') {
        reject(new Error(ECHARTS_LOAD_ERROR))
        return
      }
      resolve(echarts)
    })
    script.addEventListener('error', () => reject(new Error(ECHARTS_LOAD_ERROR)))
    if (!existing) {
      script.src = ECHARTS_SCRIPT_SRC
      document.body.appendChild(script)
    }
  })
  return promise
}

/** 只给测试用：清掉单例与脚本节点（连同节点清，防「给已 settle 的节点再挂监听器⇒永久挂住」）。 */
export function __resetForTest(): void {
  promise = null
  document.getElementById(ECHARTS_SCRIPT_ID)?.remove()
}

/* ── 多色板（Metabase 复刻规格：8 色 + 名字哈希恒同色）────────────────────── */

/** 扇形/图例的 8 色 Metabase 色板（蓝/绿/紫/红/黄/橙/青/靛）。 */
export const DONUT_PALETTE: readonly string[] = [
  '#509EE3',
  '#88BF4D',
  '#A989C5',
  '#EF8C8C',
  '#F9D45C',
  '#F2A86F',
  '#98D9D9',
  '#7172AD',
]

/** 名字 → 色板下标：31 进制滚动 hash 取模（自写实现，不拷 Metabase 函数体）——同名维度跨图恒同色。 */
export function paletteIndexForName(name: string): number {
  let hash = 0
  for (let i = 0; i < name.length; i += 1) {
    hash = (hash * 31 + name.charCodeAt(i)) >>> 0
  }
  return hash % DONUT_PALETTE.length
}

/** 「其他」段的深灰（Metabase 同款半透明墨；白底上呈灰）。 */
export const DONUT_OTHER_COLOR = 'hsla(204, 66%, 8%, 0.44)'

/** 占比低于此值的切片是「其他」合并候选；候选 ≥2 个才并成一个「其他」（单个照常显示）。 */
const OTHER_MIN_SHARE = 0.025

/** 硬顶：合并后盘面仍超 8 段时收成「前 7 名 + 其他」（其他恒排最后）。 */
const MAX_DISPLAY_SLICES = 8

/** 低于此占比的片渲染值撑到 0.5%（防不可见；占比/明细仍按真值）。 */
const MIN_VISIBLE_SHARE = 0.005

/** 悬停排名明细 tooltip 的暗底（Metabase 交互定稿）。 */
export const DONUT_TIP_BG = '#303C46'

/** 订单状态环「具名五态」的固定取色（键 = `@/config/orderStatus` 的状态码；值 = Metabase 色板族，
 * 同名状态恒同色）。其余状态恒并「其他」（DONUT_OTHER_COLOR）——合并语义与 buildDonutData 的
 * 阈值合并不同，页面侧单独合并。 */
export const STATUS_RING_COLORS: Readonly<Record<string, string>> = {
  COMPLETED: '#88BF4D',
  IN_PROGRESS: '#509EE3',
  PENDING_REVIEW: '#F9D45C',
  WAITING_GRAB: '#98D9D9',
  UNASSIGNED: '#A989C5',
}

/** tooltip 正文字 / 次要字 / 行悬停底 / 分隔线（暗底上的衍生色，全部收口在本文件）。 */
const TIP_TEXT = '#f2f4f7'
const TIP_SUBTEXT = '#b9c0cb'
const TIP_ROW_HOVER = '#182630'
const TIP_DIVIDER = 'rgba(255, 255, 255, 0.15)'

/** 中心总计的墨色 / 弱字色（canvas 读不到 CSS 变量 ⇒ 按 carbon.css `--tt2-ink`/`--tt2-dim` 值复刻）。 */
export const DONUT_INK = '#2b2a28'
export const DONUT_DIM = '#6d6a63'

/* ── 数据整形（纯函数，页测可直接驱动）────────────────────────────────────── */

export type DonutInputRow = {
  name: string
  /** 接口原样的金额字符串（两位小数）；扇形只吃它，展示也原样用它。 */
  amount: string
}

/** 图例与扇区的一格（`amountText` 永远是原字符串；`value` 只喂扇形几何）。 */
export type DonutSlice = {
  name: string
  value: number
  amountText: string
  color: string
  percentText: string
}

/** tooltip 排名明细的一行（全量正金额行、金额降序——截断前的完整榜）。 */
export type DonutRankRow = {
  name: string
  amountText: string
  color: string
  percentText: string
}

export type DonutData = {
  /** 摆上盘面的格（小片合并/硬顶截断后的显示集；「其他」恒排最后）。 */
  slices: DonutSlice[]
  /** 悬停排名明细（含被并进「其他」的行）。 */
  rank: DonutRankRow[]
  /** 格值的算术和（Number，仅扇形几何/占比用；上屏总计一律用调用方给的后端字符串）。 */
  totalValue: number
}

/** 金额字符串 → 扇形几何值。⭐ 只喂 canvas 几何与占比，**不当钱用**（上屏一律 `amountText` 原字符串）；空串 / 非有限数 / ≤0 返回 null（不进扇形）。 */
export function parseDonutAmount(amount: string): number | null {
  const value = Number(amount)
  if (!Number.isFinite(value) || value <= 0) return null
  return value
}

function percentTextOf(value: number, total: number): string {
  if (total <= 0) return '0.0%'
  return `${((value / total) * 100).toFixed(1)}%`
}

/** 整数分求和（§1「唯一比大小/运算出口 = 整数分」）：合并「其他」的金额串用它算，不引浮点。 */
function sumAmountText(amounts: readonly string[]): string {
  const cents = amounts.reduce((acc, a) => acc + Math.round(Number(a) * 100), 0)
  return (cents / 100).toFixed(2)
}

type DisplayRow = { name: string; value: number; amountText: string; isOther: boolean }

function otherOf(rows: readonly { name: string; value: number; amountText: string }[]): DisplayRow {
  return {
    name: '其他',
    value: rows.reduce((acc, row) => acc + row.value, 0),
    amountText: sumAmountText(rows.map((row) => row.amountText)),
    isOther: true,
  }
}

/** 行列表 → 扇形数据：值降序；占比 <2.5% 的片 ≥2 个才并「其他」（单个照常显示）；>8 段收
 * 前 7 + 其他；名字哈希取色（同名恒同色）、其他恒灰；全空返回 null（调用方走空态）。 */
export function buildDonutData(rows: readonly DonutInputRow[]): DonutData | null {
  const picked: { name: string; value: number; amountText: string }[] = []
  for (const row of rows) {
    const value = parseDonutAmount(row.amount)
    if (value === null) continue
    picked.push({ name: row.name, value, amountText: row.amount })
  }
  if (picked.length === 0) return null
  picked.sort((a, b) => b.value - a.value)
  const totalValue = picked.reduce((acc, item) => acc + item.value, 0)
  const shareOf = (value: number): number => (totalValue > 0 ? value / totalValue : 0)

  const big = picked.filter((item) => shareOf(item.value) >= OTHER_MIN_SHARE)
  const small = picked.filter((item) => shareOf(item.value) < OTHER_MIN_SHARE)
  let display: DisplayRow[] =
    small.length >= 2
      ? [...big.map((item) => ({ ...item, isOther: false })), otherOf(small)]
      : picked.map((item) => ({ ...item, isOther: false }))
  if (display.length > MAX_DISPLAY_SLICES) {
    display = [
      ...display.slice(0, MAX_DISPLAY_SLICES - 1),
      otherOf(display.slice(MAX_DISPLAY_SLICES - 1)),
    ]
  }

  const slices: DonutSlice[] = display.map((item) => ({
    name: item.name,
    value: item.value,
    amountText: item.amountText,
    color: item.isOther ? DONUT_OTHER_COLOR : DONUT_PALETTE[paletteIndexForName(item.name)]!,
    percentText: percentTextOf(item.value, totalValue),
  }))

  // 排名明细 = 截断前的完整榜；点的颜色随它落进的格（显示格各自色、被并的归「其他」灰）。
  const colorByName = new Map(
    slices.filter((slice) => slice.name !== '其他').map((slice) => [slice.name, slice.color]),
  )
  const rank: DonutRankRow[] = picked.map((item) => ({
    name: item.name,
    amountText: item.amountText,
    color: colorByName.get(item.name) ?? DONUT_OTHER_COLOR,
    percentText: percentTextOf(item.value, totalValue),
  }))

  return { slices, rank, totalValue }
}

/* ── tooltip 的行悬停高亮（ECharts tooltip 是真实 DOM ⇒ 注入一次样式，同 id 幂等）────── */

const TIP_STYLE_ID = 'tt2-donut-tip-style'

/** tooltip 内容类的样式（色值全部来自本文件常量；重复调用只注入一次）。 */
export function ensureDonutTipStyle(): void {
  if (document.getElementById(TIP_STYLE_ID) !== null) return
  const style = document.createElement('style')
  style.id = TIP_STYLE_ID
  style.textContent =
    '.tt2-donut-tip-title{color:rgba(255,255,255,0.69);font-size:13px;font-weight:600;margin-bottom:6px}' +
    '.tt2-tip-row{display:flex;align-items:center;gap:6px;padding:2px 4px;border-radius:4px;white-space:nowrap}' +
    '.tt2-tip-row:hover{background:' + TIP_ROW_HOVER + '}' +
    '.tt2-tip-dot{width:12px;height:12px;border-radius:50%;flex:none}' +
    '.tt2-tip-name{flex:1 1 auto;min-width:56px}' +
    '.tt2-tip-amount{font-weight:600;font-variant-numeric:tabular-nums}' +
    '.tt2-tip-pct{color:' + TIP_SUBTEXT + ';font-weight:600;min-width:44px;text-align:right;font-variant-numeric:tabular-nums}' +
    '.tt2-tip-foot{display:flex;justify-content:space-between;gap:16px;margin-top:6px;padding-top:6px;' +
    'border-top:1px solid ' + TIP_DIVIDER + ';color:' + TIP_SUBTEXT + '}'
  document.head.appendChild(style)
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

/* ── 明细卡定位（跟鼠标 10px、优先光标上方、右放不下翻左）────────────────────── */

type TipPoint = [number, number]
type TipSize = { contentSize: [number, number]; viewSize: [number, number] }

/** ECharts tooltip.position 回调：右偏 10px；上方放不下落下方；右侧放不下翻左。 */
function tipPosition(point: TipPoint, size: TipSize): [number, number] {
  const [mouseX, mouseY] = point
  const [cardW, cardH] = size.contentSize
  let x = mouseX + 10
  if (x + cardW > size.viewSize[0]) x = mouseX - cardW - 10
  if (x < 0) x = 0
  let y = mouseY - cardH - 10
  if (y < 0) y = mouseY + 10
  return [x, y]
}

/* ── 控制器（init / setOption / 中心覆盖层 / 淡 30% 联动）────────────────── */

export type DonutController = {
  /** 换数据集（合并 setOption，自带过渡动画）；`totalText` 用后端合计原字符串（⛔ 前端相加）。 */
  update(data: DonutData, totalText: string, dimensionLabel: string): void
  /** 图例悬停 ⇄ 高亮该扇区并淡化其余、中心切该块（按名字寻址：显隐切换会改变下标）。 */
  highlight(sliceName: string): void
  /** 复位淡化与中心。 */
  downplay(): void
  resize(): void
  dispose(): void
}

/** 单片的样式体（flat 或包进 `{ normal }` 由 applySliceColors 决定）。 */
type SliceStyle = { color: string; opacity: number }

/** 单片 option 数据（itemStyle 按开关走 flat 或 3.6.2 嵌套形态）。 */
type SliceDatum = {
  name: string
  value: number
  itemStyle: SliceStyle | { normal: SliceStyle }
}

/** 中心总计的 mono 字族（与 `.cv2-num` 同一款 ui-monospace 栈）。 */
const CENTER_MONO = "ui-monospace, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace"

/** 容器宽低于此值时中心只显示数值（不显示标签行）。 */
const CENTER_LABEL_MIN_WIDTH = 160

/** createDonut 的选项。全部可选。 */
export type CreateDonutOptions = {
  /** 中心第二行的小字（默认 '总计'）。 */
  centerLabel?: string
  /** 环外 label/引导线改用 3.6.2 真正生效的 label.normal 嵌套形态关掉（flat 的 show:false 它不认）。 */
  hideSliceLabels?: boolean
  /** 容器 mouseleave 兜底 dispatch hideTip（3.6.2 enterable 浮层移出容器后不隐的兜底）。 */
  hideTipOnLeave?: boolean
  /** 切片色/淡化走 3.6.2 真正生效的 itemStyle.normal 嵌套形态：flat 形态该版不认，切片会落回
   * 内置位置色板 ⇒ 与图例点异色。默认关 = 财务三页现状零变化；后台首页看板显式开。 */
  applySliceColors?: boolean
}

/** 在容器上建一张 Metabase 形态环形图（整环、12 点钟起顺时针、值降序、中心 HTML 覆盖层）。
 * 容器必须已在 DOM 里且有尺寸。 */
export function createDonut(
  container: HTMLElement,
  echarts: EChartsNamespace,
  options: CreateDonutOptions = {},
): DonutController {
  ensureDonutTipStyle()
  const chart = echarts.init(container)
  const centerLabel = options.centerLabel ?? '总计'
  const hideSliceLabels = options.hideSliceLabels === true
  const hideTipOnLeave = options.hideTipOnLeave === true
  const applySliceColors = options.applySliceColors === true

  // 中心覆盖层：HTML 绝对定位（⛔ 不用 echarts graphic/title），pointer-events:none 不挡扇形悬停。
  const hadPosition = container.style.position !== ''
  if (!hadPosition) container.style.position = 'relative'
  const centerValueEl = document.createElement('div')
  centerValueEl.className = 'tt2-donut-center-value'
  centerValueEl.style.cssText =
    'font-size:22px;font-weight:700;line-height:1.15;font-family:' + CENTER_MONO + ';color:' + DONUT_INK
  const centerLabelEl = document.createElement('div')
  centerLabelEl.className = 'tt2-donut-center-label'
  centerLabelEl.style.cssText =
    'font-size:14px;font-weight:700;line-height:1.15;color:' + DONUT_DIM
  const centerOverlay = document.createElement('div')
  centerOverlay.className = 'tt2-donut-center'
  centerOverlay.style.cssText =
    'position:absolute;left:0;top:0;right:0;bottom:0;display:flex;flex-direction:column;' +
    'align-items:center;justify-content:center;gap:4px;pointer-events:none'
  centerOverlay.appendChild(centerValueEl)
  centerOverlay.appendChild(centerLabelEl)
  container.appendChild(centerOverlay)

  // 3.6.2 扇形状态选项只认 label.normal/emphasis 嵌套形态；默认分支保留 flat 形态。
  const sliceLabelState = hideSliceLabels
    ? {
        label: { normal: { show: false }, emphasis: { show: false } },
        labelLine: { normal: { show: false }, emphasis: { show: false } },
      }
    : {
        label: { show: false },
        labelLine: { show: false },
      }
  const onContainerLeave = (): void => {
    chart.dispatchAction?.({ type: 'hideTip' })
  }
  if (hideTipOnLeave) container.addEventListener('mouseleave', onContainerLeave)

  let slices: DonutSlice[] = []
  let totalText = ''
  let totalValue = 0
  /** 淡 30% 联动的「保持全亮」片下标；null = 无悬停。 */
  let dimIndex: number | null = null

  function setCenter(valueText: string, nameText: string): void {
    centerValueEl.textContent = valueText
    centerLabelEl.textContent = nameText
  }

  /** 容器太窄只显示数值。 */
  function applyCenterWidth(): void {
    centerLabelEl.style.display = container.clientWidth < CENTER_LABEL_MIN_WIDTH ? 'none' : ''
  }

  /** 盘面数据（含淡化；<0.5% 的片渲染值撑到 0.5% 防不可见）。 */
  function seriesData(): SliceDatum[] {
    return slices.map((slice, index) => {
      const style: SliceStyle = {
        color: slice.color,
        opacity: dimIndex === null || dimIndex === index ? 1 : 0.3,
      }
      return {
        name: slice.name,
        value:
          totalValue > 0 && slice.value / totalValue < MIN_VISIBLE_SHARE
            ? totalValue * MIN_VISIBLE_SHARE
            : slice.value,
        itemStyle: applySliceColors ? { normal: style } : style,
      }
    })
  }

  function applySlices(): void {
    chart.setOption({ series: [{ data: seriesData() }] })
  }

  function update(data: DonutData, nextTotalText: string, dimensionLabel: string): void {
    slices = data.slices
    totalText = nextTotalText
    totalValue = data.totalValue
    dimIndex = null
    applyCenterWidth()
    setCenter(totalText, centerLabel)
    const tipHtml = (): string => {
      const rows = data.rank
        .map(
          (row) =>
            '<div class="tt2-tip-row">' +
            '<span class="tt2-tip-dot" style="background:' +
            row.color +
            '"></span>' +
            '<span class="tt2-tip-name">' +
            escapeHtml(row.name) +
            '</span>' +
            '<span class="tt2-tip-amount">' +
            escapeHtml(row.amountText) +
            '</span>' +
            '<span class="tt2-tip-pct">' +
            row.percentText +
            '</span></div>',
        )
        .join('')
      return (
        '<div class="tt2-donut-tip-title">' +
        escapeHtml(dimensionLabel) +
        '</div>' +
        rows +
        '<div class="tt2-tip-foot"><span>总计</span><span>100%</span></div>'
      )
    }
    chart.setOption({
      tooltip: {
        trigger: 'item',
        confine: true,
        enterable: true,
        backgroundColor: DONUT_TIP_BG,
        borderWidth: 0,
        padding: [8, 12],
        textStyle: { color: TIP_TEXT, fontSize: 12 },
        extraCssText: 'border-radius:8px;box-shadow:0 4px 16px rgba(0, 0, 0, 0.25);',
        position: tipPosition,
        // 闭包全量数据渲染排名列表（行 = 全部正金额行、金额降序；悬停行高亮走注入的样式）。
        formatter: tipHtml,
      },
      series: [
        {
          type: 'pie',
          radius: ['60%', '100%'],
          center: ['50%', '50%'],
          // 12 点钟起、顺时针、按值降序（数据侧已排序）；整环不设 borderWidth（业务方拍板无缝）。
          startAngle: 90,
          clockwise: true,
          itemStyle: {},
          ...sliceLabelState,
          data: seriesData(),
        },
      ],
    })
  }

  // 悬停联动（常开）：悬停扇形 → 其余片淡 30% + 中心切该块；移出复位。
  chart.on?.('mouseover', (params) => {
    if (params.seriesIndex !== 0 || params.dataIndex === undefined) return
    const slice = slices[params.dataIndex]
    if (slice === undefined) return
    dimIndex = params.dataIndex
    applySlices()
    setCenter(slice.amountText, slice.name)
  })
  chart.on?.('mouseout', () => {
    if (dimIndex === null) return
    dimIndex = null
    applySlices()
    setCenter(totalText, centerLabel)
  })

  return {
    update,
    highlight(sliceName: string): void {
      const index = slices.findIndex((slice) => slice.name === sliceName)
      if (index < 0) return
      dimIndex = index
      applySlices()
      setCenter(slices[index]!.amountText, slices[index]!.name)
    },
    downplay(): void {
      if (dimIndex === null) return
      dimIndex = null
      applySlices()
      setCenter(totalText, centerLabel)
    },
    resize(): void {
      applyCenterWidth()
      chart.resize?.()
    },
    dispose(): void {
      if (hideTipOnLeave) container.removeEventListener('mouseleave', onContainerLeave)
      centerOverlay.remove()
      if (!hadPosition && container.style.position === 'relative') container.style.position = ''
      chart.dispose?.()
    },
  }
}

/* ── 双轴柱线（2026-11-06 后台首页看板：趋势 / 客服统计）──────────────────── */

/** 柱（左轴）的用色（样稿 r4；与 carbon.css `--tt2-dsh-bar` 同值——canvas 读不到 CSS 变量，改值两处同改）。 */
export const COMBO_BAR_COLOR = '#4d9bd6'

/** 线（右轴）的用色（同上，与 `--tt2-dsh-line` 同值）。 */
export const COMBO_LINE_COLOR = '#2e8b6e'

/** 轴刻度字的墨色（x/y 轴 axisLabel 共用）：取看板中心数字那档近黑墨的 84%，
 * 白卡上的人名 / 刻度小字要够黑才读得清（旧 #6d6a63 太浅，业务方目验退回）。 */
export const AXIS_LABEL_COLOR = 'rgba(43, 42, 40, 0.84)'

/** 网格线 / 悬停参考线的衍生色（全部收口在本文件，同扇形 tooltip 的做法）。 */
const COMBO_GRID = '#eeece6'
const COMBO_GUIDE = '#b9c0cb'
const COMBO_CARD_BG = '#ffffff'
const COMBO_CARD_BORDER = '#e8e6e1'

/** 双轴柱线的一个横档：柱与线共用同一根 `label`。 */
export type BarLinePoint = {
  /** x 轴刻度（趋势 = MM-DD；客服统计 = 人名）。 */
  label: string
  /** 柱的几何值（左轴）。 */
  barValue: number
  /** 线的几何值（右轴）。 */
  lineValue: number
  /** 线的展示串（金额原样字符串；悬停卡显示它，⛔ 不拿几何值拼金额）。 */
  lineText: string
}

export type BarLineComboData = {
  points: readonly BarLinePoint[]
  /** 柱 / 线的系列名（悬停卡行名）。 */
  barName: string
  lineName: string
}

export type BarLineComboController = {
  /** 换数据集（合并模式）。 */
  update(data: BarLineComboData): void
  resize(): void
  dispose(): void
}

const COMBO_TIP_STYLE_ID = 'tt2-combo-tip-style'

/** 悬停白卡内容类的样式（色值全部来自本文件常量；重复调用只注入一次）。 */
export function ensureComboTipStyle(): void {
  if (document.getElementById(COMBO_TIP_STYLE_ID) !== null) return
  const style = document.createElement('style')
  style.id = COMBO_TIP_STYLE_ID
  style.textContent =
    '.tt2-combo-tip-title{color:' + DONUT_DIM + ';font-size:11.5px;margin-bottom:5px}' +
    '.tt2-combo-tip-row{display:flex;align-items:center;gap:7px;padding:2px 0;font-variant-numeric:tabular-nums}' +
    '.tt2-combo-tip-dot{width:9px;height:9px;border-radius:2px;flex:none}' +
    '.tt2-combo-tip-name{color:' + DONUT_INK + '}' +
    '.tt2-combo-tip-value{margin-left:auto;padding-left:14px;font-weight:500;color:' + DONUT_INK + '}'
  document.head.appendChild(style)
}

/** 右轴（金额）刻度：≥1000 缩写成 k（样稿 r4 的 fmtTick 同式）；其余取整。 */
function comboMoneyTick(value: number): string {
  if (value >= 1000) return (value / 1000).toFixed(value >= 10000 ? 0 : 1) + 'k'
  return String(Math.round(value))
}

type ComboTipParam = { dataIndex?: number }

function comboTipHtml(data: BarLineComboData, params: readonly ComboTipParam[]): string {
  const point = params[0]?.dataIndex === undefined ? undefined : data.points[params[0].dataIndex]
  if (!point) return ''
  const row = (color: string, name: string, valueText: string): string =>
    '<div class="tt2-combo-tip-row">' +
    '<span class="tt2-combo-tip-dot" style="background:' +
    color +
    '"></span>' +
    '<span class="tt2-combo-tip-name">' +
    escapeHtml(name) +
    '</span>' +
    '<span class="tt2-combo-tip-value">' +
    escapeHtml(valueText) +
    '</span></div>'
  return (
    '<div class="tt2-combo-tip-title">' +
    escapeHtml(point.label) +
    '</div>' +
    row(COMBO_BAR_COLOR, data.barName, String(point.barValue)) +
    row(COMBO_LINE_COLOR, data.lineName, point.lineText)
  )
}

/** 在容器上建一张双轴柱线图（柱=左轴、线=右轴；悬停出垂直参考线 + 白色明细卡——
 * 走 ECharts 的 axis 触发，参考线/白卡由它托管）。容器必须已在 DOM 里且有尺寸。 */
export function createBarLineCombo(
  container: HTMLElement,
  echarts: EChartsNamespace,
): BarLineComboController {
  ensureComboTipStyle()
  const chart = echarts.init(container)

  function update(data: BarLineComboData): void {
    chart.setOption({
      grid: { left: 48, right: 56, top: 16, bottom: 28 },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'line', lineStyle: { color: COMBO_GUIDE, width: 1, type: 'dashed' } },
        confine: true,
        backgroundColor: COMBO_CARD_BG,
        borderColor: COMBO_CARD_BORDER,
        borderWidth: 1,
        padding: [9, 12],
        textStyle: { color: DONUT_INK, fontSize: 12.5 },
        extraCssText: 'border-radius:8px;box-shadow:0 4px 14px rgba(0, 0, 0, 0.12);min-width:130px;',
        formatter: (params: readonly ComboTipParam[]) => comboTipHtml(data, params),
      },
      xAxis: {
        type: 'category',
        data: data.points.map((point) => point.label),
        axisLine: { lineStyle: { color: COMBO_GRID } },
        axisTick: { show: false },
        axisLabel: { color: AXIS_LABEL_COLOR, fontSize: 11 },
      },
      yAxis: [
        {
          type: 'value',
          axisLabel: { color: AXIS_LABEL_COLOR, fontSize: 10.5 },
          splitLine: { lineStyle: { color: COMBO_GRID } },
        },
        {
          type: 'value',
          axisLabel: { color: AXIS_LABEL_COLOR, fontSize: 10.5, formatter: comboMoneyTick },
          splitLine: { show: false },
        },
      ],
      series: [
        {
          name: data.barName,
          type: 'bar',
          yAxisIndex: 0,
          barMaxWidth: 18,
          itemStyle: { color: COMBO_BAR_COLOR },
          data: data.points.map((point) => point.barValue),
        },
        {
          name: data.lineName,
          type: 'line',
          yAxisIndex: 1,
          symbol: 'circle',
          symbolSize: 5,
          itemStyle: { color: COMBO_LINE_COLOR },
          lineStyle: { width: 2, color: COMBO_LINE_COLOR },
          data: data.points.map((point) => point.lineValue),
        },
      ],
    })
  }

  return {
    update,
    resize(): void {
      chart.resize?.()
    },
    dispose(): void {
      chart.dispose?.()
    },
  }
}
