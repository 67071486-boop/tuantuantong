<script lang="ts">
// 只保留当前会话刚抢到的单，切页返回时也能挡住旧快照。
const recentClaimsByUser = new WeakMap<object, Map<string, number>>()
</script>

<script setup lang="ts">
import { orderStatusClass } from '@/v2/orderStatusAppearance'
import ElDialog from '@/v2/OrderTabDialog.vue'
/** 内部抢单大厅：游戏与订单由服务端筛选并分页。 */
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { fetchGames, fetchServers, type ConfigRow, type ServerRow } from '@/api/config'
import {
  claimOrderWithMessage,
  claimPeerMirror,
  fetchHall,
  PEER_CLAIM_ACCEPTED_TEXT,
  PEER_CLAIM_TAKEN_CODE,
  toHallParams,
  type HallListItem,
} from '@/api/hall'
import { claimUpstreamMirrorWithMessage } from '@/api/jidanpuUpstream'
import { fetchScreenshots, type ScreenshotItem } from '@/api/screenshots'
import { BizError } from '@/api/errors'
import { showBackendSuccessMessage } from '@/config/backendMessage'
import { ORDER_STATUS_VALUES, v2OrderStatusLabel } from '@/config/orderStatus'
import { useSessionStore } from '@/stores/session'
import { useColumnWidths } from '@/v2/tableResize'

const session = useSessionStore()

/* ── 文案（出处见 island 页同名常量；本地重写不引模块）──────────────────── */

/** 抢单确认语：逐字取对标 i18n `js.recThisOrder`（zh-CN，`insiderecorderlist.js:296`）。 */
const CLAIM_CONFIRM_TEXT =
  '确定接下此单吗？【提醒】本系统不属于交易平台，不提供担保，订单结算需要自行和本商户联系'
/** 抢单成功默认提示（后端成功 `msg` 为空串时的回退；island 页同句）。 */
const CLAIM_SUCCESS_TEXT = '抢单成功'
/** peer（上家团团通）受理成功提示（`api/hall.ts::PEER_CLAIM_ACCEPTED_TEXT` 同源；确认制不说「抢单成功」）。 */
const PEER_ACCEPTED_TEXT = PEER_CLAIM_ACCEPTED_TEXT
/** 空列表提示（layui `text.none` 同文案）。 */
const EMPTY_TEXT = '暂无数据'
/** 参考图空列表提示（业务方口径，逐字；island 页 `NO_SCREENSHOTS_TEXT`）。 */
const NO_SCREENSHOTS_TEXT = '这单还没有上传过截图'
/** 看图弹层标题（island 页 `IMG_TIT`）。 */
const SHOT_DIALOG_TITLE = '查看参考图'

/** ⭐ 项目全文小弹窗（点击内容格开层，宽随内容）。 */
const projectDlg = ref<{ text: string } | null>(null)

/** 悬停预览浮层（业务方 2026-11-05「预览不是弹窗 在鼠标旁边预览出一个内容」）：
 * 跟随光标、移开即消失、pointer-events:none 不挡鼠标；点击仍是原全文弹窗。 */
const hoverPreview = ref<{ text: string; x: number; y: number } | null>(null)

function movePreview(e: MouseEvent): void {
  if (hoverPreview.value === null) return
  // 光标右下 14/16 起浮；贴近右缘/下缘时向内收，不出视口。
  hoverPreview.value = {
    text: hoverPreview.value.text,
    x: Math.min(e.clientX + 14, window.innerWidth - 440),
    y: Math.min(e.clientY + 16, window.innerHeight - 120),
  }
}

/** 一键复制单号（剪贴板 + toast 回执；无 clipboard 权限的旧浏览器静默失败不谎报成功）。 */
async function copyOrderNo(no: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(no)
    showToast('已复制单号')
  } catch {
    /* 剪贴板不可用（非安全上下文/旧浏览器）：不弹假成功 */
  }
}
/** 接手价 hover 说明（island 页 `REC_FEE_TIT`；对标缺陷「操作」已按旧页口径去掉）。 */
const REC_FEE_TIT = '你接手此单的金额：'
/** 订单号 hover 说明（island 页 `ORDER_DETAILS_TIT` 前缀语义）。 */
const ORDER_NO_TIT_SUFFIX = '，只有接下此单才能看详情'
/** 加急徽标的 title（island 页 `BADGE_URGENT` 的 title）。 */
const URGENT_TIT = '加急订单，优先安排'

/** 状态机英文值 → v2 中文标签（仅显示层替换；枚举出处 = orderStatus.ts，msg 其余原文不动）。 */
const STATUS_EN_RE = new RegExp(`\\b(${ORDER_STATUS_VALUES.join('|')})\\b`, 'g')

/** 错误对象的可显示文本。**不自造文案**：`request()` 抛出的 message 就是后端 `msg` 原文；
 * 唯一替换 = 状态机英文值转中文（「当前状态： COMPLETED」显示成「当前状态： 已完成」）。 */
function errText(e: unknown): string {
  return (e instanceof Error ? e.message : '操作失败').replace(STATUS_EN_RE, (en) => v2OrderStatusLabel(en))
}

/* ── 提示（炭火 toast，页面内自绘——V2MyOrders 同一实现）────────────────── */

const toast = ref<{ text: string; kind: 'info' | 'error' } | null>(null)
let toastTimer: ReturnType<typeof setTimeout> | null = null

/** info 停 1500ms、error 停 6000ms（错误要看清）。 */
function showToast(text: string, kind: 'info' | 'error' = 'info'): void {
  if (toastTimer !== null) clearTimeout(toastTimer)
  toast.value = { text, kind }
  toastTimer = setTimeout(() => {
    toast.value = null
    toastTimer = null
  }, kind === 'error' ? 6000 : 1500)
}

/** 成功提示出口（`showBackendSuccessMessage` 的 actions：普通 toast / 警告长驻）。 */
const backendMessageActions = {
  toast: (text: string): void => {
    showToast(text)
  },
  alert: (text: string): void => {
    // 「代练通订单未能撤回」警告：6 秒 error 档停留（要看清、又不用打断式弹层盖表）。
    showToast(text, 'error')
  },
}

/* ── 权限门（fail-closed：会话取不到 = 不画；与 island 页 `canNode(me, …)` 同口径）── */

const canClaim = computed(() => session.canNode(['grab_hall.take_order']))
const canViewShots = computed(() => session.canNode(['order.view_screenshot']))

/* ── 字典：游戏 / 区服 ──────────────────────────────────────────────────── */

const games = ref<ConfigRow[]>([])
const servers = ref<ServerRow[]>([])

/* ── 筛选（服务端筛选；请求时经 `toHallParams` 去空值）────────────────────── */

const filters = reactive({ game: null as number | null, server: null as number | null, keyword: '', projectText: '' })

function currentParams() {
  return toHallParams({
    game: filters.game,
    server: filters.server,
    keyword: filters.keyword.trim(),
    project_text: filters.projectText.trim(),
  })
}

/* ── 取数（服务端分页；判据 = 旧页 `table.render` 的 url/where/page 同一口径）── */

const rows = ref<HallListItem[]>([])
const total = ref(0)
const loading = ref(false)

const PAGE_LIMITS = [10, 20]
const page = ref(1)
const pageSize = ref(20)

/** 行数变少（刷新）后当前页可能越界 ⇒ 收回来（客户端分页那套 watch 的服务端版）。 */
watch(total, (t) => {
  const last = Math.max(1, Math.ceil(t / pageSize.value))
  if (page.value > last) page.value = last
})

// 大厅读缓存默认 1 秒，留 200ms 余量。窗口过后重新接受服务端结果，允许同一单重抛。
const HALL_RELOAD_DELAY_MS = 1200
const viewer = session.user
const viewerClaims = (viewer === null ? undefined : recentClaimsByUser.get(viewer)) ?? new Map<string, number>()
if (viewer !== null) recentClaimsByUser.set(viewer, viewerClaims)
let claimReloadTimer: ReturnType<typeof setTimeout> | undefined
let disposed = false
let loadSequence = 0
let nextPage = 1

function claimKey(origin: HallListItem['origin'], target: string): string {
  return `${origin}:${target}`
}

function matchesRecentClaim(row: HallListItem, requestedAt: number): boolean {
  const target = claimTargetOf(row)
  if (target === null) return false
  return (viewerClaims.get(claimKey(row.origin, target)) ?? 0) > requestedAt
}

function scheduleClaimReload(): void {
  clearTimeout(claimReloadTimer)
  const now = Date.now()
  for (const [key, until] of viewerClaims) {
    if (until <= now) viewerClaims.delete(key)
  }
  const until = Math.max(0, ...viewerClaims.values())
  if (until > now) {
    claimReloadTimer = setTimeout(() => { void load(nextPage) }, until - now)
  }
}

function removeClaimedRows(now: number): void {
  const remaining = rows.value.filter((row) => !matchesRecentClaim(row, now))
  total.value = Math.max(0, total.value - (rows.value.length - remaining.length))
  rows.value = remaining

  scheduleClaimReload()
}

onBeforeUnmount(() => {
  disposed = true
  loadSequence++
  clearTimeout(toastTimer ?? undefined)
  clearTimeout(claimReloadTimer)
})
async function load(requestedPage: number): Promise<void> {
  const sequence = ++loadSequence
  nextPage = requestedPage
  const requestedAt = Date.now()
  loading.value = true
  try {
    const data = await fetchHall({
      ...currentParams(),
      page: requestedPage,
      page_size: pageSize.value,
    })
    if (sequence !== loadSequence) return
    rows.value = data.items.filter((row) => !matchesRecentClaim(row, requestedAt))
    total.value = Math.max(0, data.total - (data.items.length - rows.value.length))
    page.value = requestedPage
    // 参考图存在性探测（异步、不阻塞上屏；结果进 shotsExist 缓存）
    void probeShots(rows.value)
  } catch (e) {
    if (sequence !== loadSequence) return
    rows.value = []
    total.value = 0
    showToast(errText(e), 'error')
  } finally {
    if (sequence === loadSequence) loading.value = false
  }
}

/** 查询 / 刷新 / 切页码外的**重查入口**都回第 1 页（旧页 `reload(1)` 同语义）。 */
function refilter(): void {
  void load(1)
}

/** 翻页（保持当前筛选）。 */
function onPageChange(p: number): void {
  void load(p)
}

/** 每页条数切换（旧页 limits 同值 [10,20]；切完回第 1 页）。 */
function onPageSizeChange(event: Event): void {
  const size = Number((event.target as HTMLSelectElement).value)
  if (Number.isFinite(size) && size > 0) {
    pageSize.value = size
    void load(1)
  }
}

/** 首屏选中第一个游戏；区服字典仍用于展示订单行。 */
onMounted(async () => {
  try {
    games.value = await fetchGames()
  } catch (e) {
    showToast(errText(e), 'error')
  }
  if (disposed) return
  if (games.value.length > 0) selectGame(games.value[0]!.id)
  else void load(1)
  scheduleClaimReload()
})

let serverSequence = 0
function selectGame(id: number): void {
  filters.game = id
  filters.server = null
  servers.value = []
  const sequence = ++serverSequence
  void fetchServers(id).then((result) => {
    if (sequence === serverSequence) servers.value = result
  }).catch((error: unknown) => {
    if (sequence === serverSequence) showToast(errText(error), 'error')
  })
  refilter()
}

/* ── 单元格展示（与 island 页 templet 同口径的本地重写）────────────────────── */

/** 行的业务标识：原生 = 我方单号；镜像 = 远端单号（island 页 `rowKey` 同式）。 */
function rowKeyOf(d: HallListItem): string {
  return d.origin === 'native' ? d.order_no : d.remote_order_id
}

/** 展示单号：镜像行 = 远端展示单号（缺失回落远端单号）；原生行 = 来源号非空取它（D-0301）
 * ——island 页 `orderIdView` templet 的同式重写。 */
function viewNoOf(d: HallListItem): string {
  return d.origin === 'native' ? d.source_order_no || d.order_no : d.remote_order_view || d.remote_order_id
}

/** 订单号列的 title：原生 = 项目内容；镜像 = 远端单号（island 页同分支）。 */
function orderNoTit(d: HallListItem): string {
  return `${d.origin === 'native' ? d.project_text : d.remote_order_id}${ORDER_NO_TIT_SUFFIX}`
}

/** 镜像行的主键是否是**能用的正整数**（fail-closed 判据；island 页 `isClaimableMirrorPk` 同式：
 * 判「正整数」不是真值——`null`/`0`/`NaN`/负数都不是合法主键，不拿它们拼 URL）。 */
function isClaimableMirrorPk(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value > 0
}

/** 这一行**能不能接、接的时候参数是什么**（渲染期与点击期共用同一处判据；island 页 `claimTargetOf` 的同式重写）：- 原生 ⇒ 我方单号（非空串）；- `upstream` / `peer` ⇒ `mirror_pk` 十进制串（不是正整数 ⇒ `null`，不画按钮）；- `remote`（已拆除的 P2 旧路）与拿不到行键的其它来源 ⇒ `null`。
 * 这一行**能不能接、接的时候参数是什么**（渲染期与点击期共用同一处判据；island 页 `claimTargetOf` 的同式重写）：- 原生 ⇒ 我方单号（非空串）；- `upstream` / `peer` ⇒ `mirror_pk` 十进制串（不是正整数 ⇒ `null`，不画按钮）；- `remote`（已拆除的 P2 旧路）与拿不到行键的其它来源 ⇒ `null`。 */
function claimTargetOf(d: HallListItem): string | null {
  if (d.origin === 'upstream' || d.origin === 'peer') {
    return isClaimableMirrorPk(d.mirror_pk) ? String(d.mirror_pk) : null
  }
  if (d.origin === 'remote') return null
  const key = rowKeyOf(d)
  return key === '' ? null : key
}

/** 角色信息：原生 = `[游戏名]我方区服`；镜像 = `[游戏名]远端文本区服`（不查我方字典）。 */
function gameInfoOf(d: HallListItem): string {
  const gameName = d.game === null || d.game === undefined ? '' : (games.value.find((g) => g.id === d.game)?.name ?? String(d.game))
  if (d.origin !== 'native') {
    return `[${gameName}]${d.remote_game_area}${d.remote_game_server}`
  }
  const serverName = d.server === null ? '' : (servers.value.find((s) => s.id === d.server)?.name ?? String(d.server))
  return `[${gameName}]${serverName}`
}

/** 发布时间：`2026-09-20T21:32:15+08:00` → `2026-09-20 21:32`（island 页 `fmtTime` 同式）。 */
function fmtTime(iso: string): string {
  return iso.replace('T', ' ').slice(0, 16)
}

/** 状态文案（原样走静态镜像；未知值原样显示）。 */
function statusText(status: string): string {
  return v2OrderStatusLabel(status)
}

/* ── 抢单（确认弹层 → 按来源分派三个端点；判据 = island 页点击委托的同式重写）── */

const confirmState = ref<{ origin: HallListItem['origin']; target: string } | null>(null)

/** 接单在途锁按来源与主键区分，避免重复提交。 */
const claimsInFlight = reactive(new Set<string>())
/** peer 已受理（置灰不删卡；刷新前一直留在原地）。 */
const claimedPeerPks = ref<Set<number>>(new Set())

function isPeerClaimed(d: HallListItem): boolean {
  return d.origin === 'peer' && d.mirror_pk !== null && claimedPeerPks.value.has(d.mirror_pk)
}

/** 行内按钮可不可点（peer 已受理 ⇒ 置灰；确认框都不弹——双击的第二下在这里被吃掉）。 */
function claimDisabled(d: HallListItem): boolean {
  const target = claimTargetOf(d)
  return isPeerClaimed(d) || (target !== null && claimsInFlight.has(claimKey(d.origin, target)))
}

/** 点「接手此单」：目标已由渲染期 `claimTargetOf` 判过，这里只负责开弹层。 */
function askClaim(d: HallListItem): void {
  const target = claimTargetOf(d)
  if (target === null) return
  if (claimDisabled(d)) return
  confirmState.value = { origin: d.origin, target }
}

/** 团团通镜像须等上家确认，其余已确认接单的行立即离厅。 */
async function doClaim(): Promise<void> {
  const state = confirmState.value
  if (state === null) return
  confirmState.value = null
  const { origin, target } = state
  if (origin !== 'native' && origin !== 'upstream' && origin !== 'peer') return
  const key = claimKey(origin, target)
  if (claimsInFlight.has(key) || (origin === 'peer' && claimedPeerPks.value.has(Number(target)))) return

  claimsInFlight.add(key)
  try {
    if (origin === 'peer') {
      const pk = Number(target)
      await claimPeerMirror(pk)
      if (disposed) return
      claimedPeerPks.value = new Set(claimedPeerPks.value).add(pk)
      showToast(PEER_ACCEPTED_TEXT)
      return
    }
    const { message } = origin === 'upstream'
      ? await claimUpstreamMirrorWithMessage(Number(target))
      : await claimOrderWithMessage(target)
    const now = Date.now()
    viewerClaims.set(key, now + HALL_RELOAD_DELAY_MS)
    if (disposed) return
    removeClaimedRows(now)
    showBackendSuccessMessage(backendMessageActions, message, CLAIM_SUCCESS_TEXT)
    await load(nextPage)
  } catch (e) {
    if (disposed) return
    showToast(errText(e), 'error')
    if (origin === 'peer' && e instanceof BizError && e.code === PEER_CLAIM_TAKEN_CODE) {
      await load(page.value)
    }
  } finally {
    claimsInFlight.delete(key)
  }
}

/* ── 参考图（仅原生行 + 节点门；空列表人话点名，逐字）────────────────────────── */

const shotDlg = ref<{ orderNo: string; items: ScreenshotItem[] } | null>(null)
const previewShot = ref<ScreenshotItem | null>(null)

/** 参考图存在性缓存（业务方「有订单截图就显示可点击参考图 没有就展示无」）：
 * 按单号记忆探测结果，翻页/刷新只探可见行的未缓存单号（并行、每单一条轻请求）。 */
const shotsExist = ref<Record<string, boolean>>({})

async function probeShots(rows: HallListItem[]): Promise<void> {
  const targets = rows.filter(
    (r) => r.origin === 'native' && r.order_no !== '' && shotsExist.value[r.order_no] === undefined,
  )
  if (targets.length === 0) return
  await Promise.all(
    targets.map(async (r) => {
      try {
        const data = await fetchScreenshots(r.order_no)
        shotsExist.value[r.order_no] = data.items.some((it) => it.img_type === 0)
      } catch {
        shotsExist.value[r.order_no] = false
      }
    }),
  )
}

function hasShots(d: HallListItem): boolean {
  return d.origin === 'native' && shotsExist.value[d.order_no] === true
}

/** 说明文字 = 文件名（备注）（island 页 `screenshotPhoto` 的 alt 同式；Vue 插值自带转义）。 */
function shotAlt(item: ScreenshotItem): string {
  const name = item.original_name.trim()
  const remark = item.remark.trim()
  return remark === '' ? name : name === '' ? remark : `${name}（${remark}）`
}

async function openScreenshots(d: HallListItem): Promise<void> {
  // 防御：只有原生行会画这颗按钮；别拿空单号打一条注定不中的请求。
  if (d.origin !== 'native' || d.order_no === '') return
  try {
    const data = await fetchScreenshots(d.order_no)
    // ⭐ 业务方「参考图只展示订单截图 不能展示群聊二维码」：
    //    img_type 1 = 群聊二维码（D-0348 查码链路落的行），大厅一律不展示；
    //    空列表判定也按过滤后的集合（只有群码 = 视同没传过截图）。
    const items = data.items.filter((it) => it.img_type === 0)
    if (items.length === 0) {
      showToast(NO_SCREENSHOTS_TEXT)
      return
    }
    shotDlg.value = { orderNo: d.order_no, items }
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

/* ── 列宽拖拽（⭐ 对标式网格 + 手动拉宽；共享 src/v2/tableResize.ts）────
 * 列键与模板 th 一一对应；「代练/陪玩项目」不给默认宽（唯一弹性列，吃剩余宽度）；
 * 存 localStorage `tt.v2.table.v2.hall.v2`。 */
const HALL_COLUMNS = ['order', 'project', 'info', 'status', 'price', 'shots', 'time', 'actions'] as const

/* ⭐ key 升 v2（照 V2UserList hidden-cols 先例）：本批列宽整批改档，老用户拖拽存值已固化，
 * 不升版则新默认对其静默失效；代价 = 既有列宽偏好重置一次。 */
const { colStyle, startResize, resetColumn } = useColumnWidths('hall.v2', {
  /* ⭐ 2026-11-06：order 定宽 200（19 位等宽单号 + 复制钮，超出省略）、info 收 140、
   * status 放宽 140（墨点签完整显示）、「时限要求」列整列撤除；project 仍弹性。
   * price/shots/time/actions = 内容短且基本固定 ⇒ 窄固定宽（status/price/shots 居中无手柄）。 */
  order: 200,
  status: 140,
  info: 140,
  price: 96,
  shots: 104,
  time: 150,
  actions: 130,
})

/* ── 统计行（只报真数据：真实笔数 + 声明句；「总金额0元」不搬）────────────────── */

const tenantName = computed(() => session.user?.tenant_name || '')
</script>

<template>
  <div class="cv2-page" data-v2-page="hall">
    <nav class="hall-games" aria-label="游戏筛选">
      <button v-for="game in games" :key="game.id" type="button" class="hall-game"
        :class="{ 'hall-game--active': filters.game === game.id }" :aria-pressed="filters.game === game.id"
        :data-v2-game="game.id" @click="selectGame(game.id)">{{ game.name }}</button>
    </nav>
    <section class="cv2-panel cv2-filter" aria-label="筛选" data-v2="filters">
      <div class="cv2-filter-row">
        <label class="cv2-field">
          <span class="cv2-field-label">搜索</span>
          <el-input
            v-model="filters.keyword"
            placeholder="订单号/角色名"
            data-v2-filter="keyword"
            @keydown.enter="refilter"
          />
        </label>
        <label class="cv2-field">
          <span class="cv2-field-label">项目</span>
          <el-input
            v-model="filters.projectText"
            placeholder="代练/陪玩项目"
            data-v2-filter="project"
            @keydown.enter="refilter"
          />
        </label>
        <div class="cv2-filter-actions">
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--find cv2-btn--fill" data-v2="search" @click="refilter">搜索</button>
          <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--find" data-v2="refresh" :disabled="loading" @click="refilter">
            刷新列表
          </button>
          <span v-if="loading" class="cv2-filter-loading" data-v2="loading-state">正在加载…</span>
        </div>
      </div>
    </section>

    <!-- 统计行：真实笔数 + 声明句（租户名取会话）；「总金额0元」恒 0 不搬（DRAFT §8） -->
    <p class="cv2-total" data-v2="total">
      本系统为{{ tenantName }}内部订单管理系统，并非交易平台，不提供任何担保服务，所有订单请自行与{{ tenantName }}处理结算问题。当前共有{{ total }}笔订单等待接单
      <span class="cv2-total-notice">
        【声明】该系统并非交易平台，无担保服务，系统内所有订单业务及结算均由{{ tenantName }}负责运营，
        本系统一切业务和结算与软件开发商团团通无关
      </span>
    </p>

    <!-- 表格：语义 table（DRAFT §7）；列 = 旧页真有值的那些。
         ⭐ 2026-11-01 第二轮：<colgroup> 承接列宽拖拽 + 每 th 右缘 6px 手柄（双击复原）。 -->
    <section class="cv2-panel cv2-table-panel" aria-label="抢单列表">
      <table class="cv2-table">
        <colgroup>
          <col v-for="key in HALL_COLUMNS" :key="key" :style="colStyle(key)" />
        </colgroup>
        <thead>
          <tr>
            <th>
              订单号
              <span class="cv2-th-resizer" data-v2-col="order" @mousedown="startResize($event, 'order')" @dblclick="resetColumn('order')" />
            </th>
            <th>
              代练/陪玩项目
              <span class="cv2-th-resizer" data-v2-col="project" @mousedown="startResize($event, 'project')" @dblclick="resetColumn('project')" />
            </th>
            <th>
              角色信息
              <span class="cv2-th-resizer" data-v2-col="info" @mousedown="startResize($event, 'info')" @dblclick="resetColumn('info')" />
            </th>
            <th class="cv2-th-center">
              订单状态
            </th>
            <th class="cv2-th-num cv2-th-center">
              接手价
            </th>
            <th v-if="canViewShots" class="cv2-th-center">
              参考图
            </th>
            <th>
              发布时间
              <span class="cv2-th-resizer" data-v2-col="time" @mousedown="startResize($event, 'time')" @dblclick="resetColumn('time')" />
            </th>
            <th>
              操作
              <span class="cv2-th-resizer" data-v2-col="actions" @mousedown="startResize($event, 'actions')" @dblclick="resetColumn('actions')" />
            </th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in rows"
            :key="rowKeyOf(row)"
            class="cv2-row"
            :class="{ 'cv2-row--peer-claimed': isPeerClaimed(row) }"
            :data-v2-row="rowKeyOf(row)"
          >
            <td class="cv2-td-order">
              <span v-if="row.is_urgent === true" class="cv2-urgent" :title="URGENT_TIT">急</span>
              <span class="cv2-order-no" :data-v2-order-no="rowKeyOf(row)" :title="orderNoTit(row)">
                {{ viewNoOf(row) }}
              </span>
              <button
                type="button"
                class="cv2-copybtn"
                :data-v2-copy="rowKeyOf(row)"
                :aria-label="`复制单号 ${rowKeyOf(row)}`"
                title="复制单号"
                @click.stop="copyOrderNo(viewNoOf(row))"
              >
                <svg viewBox="0 0 16 16" aria-hidden="true">
                  <rect x="5.5" y="5.5" width="8" height="9" rx="1.5" />
                  <path d="M10.5 3.5h-6A1.5 1.5 0 0 0 3 5v7" />
                </svg>
              </button>
            </td>
            <td class="cv2-td-project">
              <!-- ⭐ 业务方「内容区域可点击 点击后弹窗展示完整的代练内容 弹窗不要太大」 -->
              <button
                type="button"
                class="cv2-project-more"
                :data-v2-project="rowKeyOf(row)"
                @mouseenter="hoverPreview = { text: row.project_text, x: 0, y: 0 }"
                @mousemove="movePreview"
                @mouseleave="hoverPreview = null"
                @click="projectDlg = { text: row.project_text }"
              >{{ row.project_text }}</button>
            </td>
            <td>{{ gameInfoOf(row) }}</td>
            <td class="cv2-td-center">
              <span class="cv2-st cv2-st--mark cv2-order-status" :class="orderStatusClass(row.status)" :data-v2-status="rowKeyOf(row)"><i class="cv2-dot" aria-hidden="true"></i>{{ statusText(row.status) }}</span>
            </td>
            <td class="cv2-td-num cv2-td-center" :title="`${REC_FEE_TIT}${row.hall_price ?? ''}元`">
              <span class="cv2-money">{{ row.hall_price ?? '' }}</span>
            </td>
            <td v-if="canViewShots" class="cv2-td-center">
              <!-- ⭐ 业务方「有订单截图就显示可点击参考图 没有就单纯展示无」（探测见 probeShots）；
                   非原生行不探测不显示。 -->
              <button
                v-if="row.origin === 'native' && hasShots(row)"
                type="button"
                class="cv2-iconact-text cv2-iconact-text--blue"
                :data-v2-shot="row.order_no"
                @click="openScreenshots(row)"
              >
                <i class="layui-icon layui-icon-picture" aria-hidden="true"></i>
                <span>参考图</span>
              </button>
              <span v-else-if="row.origin === 'native'" class="cv2-dim" data-v2="no-shot">无</span>
            </td>
            <td class="cv2-td-time">{{ fmtTime(row.created_at) }}</td>
            <td>
              <!-- ⭐ D-0354  3（业务方修订 §5「行内一律文字链」）：行内主行动可用小号实底——
                   抢单是大厅的主行动，28px 实底主色白字、hover deep；确认弹层与下单价零改动。 -->
              <button
                v-if="canClaim && claimTargetOf(row) !== null"
                type="button"
                class="cv2-btn cv2-btn--danger cv2-btn--sm"
                :data-v2-claim="rowKeyOf(row)"
                :data-v2-claim-origin="row.origin"
                :disabled="claimDisabled(row)"
                @click="askClaim(row)"
              >
                接手此单
              </button>
            </td>
          </tr>
          <tr v-if="rows.length === 0">
            <td :colspan="canViewShots ? 8 : 7" class="cv2-empty" data-v2="empty">
              <template v-if="loading">正在加载…</template>
              <template v-else>{{ EMPTY_TEXT }}</template>
            </td>
          </tr>
        </tbody>
      </table>
      <div class="cv2-pager" data-v2="pager">
        <span class="cv2-pager-total" data-v2="pager-total">共 {{ total }} 条</span>
        <label class="cv2-pager-size">
          每页
          <select class="cv2-input cv2-input--inline" data-v2="page-size" :value="pageSize" @change="onPageSizeChange">
            <option v-for="n in PAGE_LIMITS" :key="n" :value="n">{{ n }}</option>
          </select>
          条
        </label>
        <el-pagination
          :current-page="page"
          :page-size="pageSize"
          :total="total"
          layout="prev, pager, next"
          background
          @current-change="onPageChange"
        />
      </div>
    </section>

    <!-- 炭火 toast（页面内，吃 .ttv2 的皮） -->
    <div
      v-if="toast"
      class="cv2-toast"
      :class="{ 'cv2-toast--error': toast.kind === 'error' }"
      role="status"
      data-v2="toast"
    >
      {{ toast.text }}
    </div>

    <!-- 抢单确认（旧页 layer.confirm 的炭火版；确认语逐字同对标 zh-CN）；⭐ 弹窗档 = sm 480 -->
    <el-dialog
      :model-value="confirmState !== null"
      title="接手此单"
      width="480px"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-claim"
      @update:model-value="confirmState = null"
    >
      <p class="cv2-dlg-text">{{ CLAIM_CONFIRM_TEXT }}</p>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="confirmState = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="confirm-claim" @click="void doClaim()">
            确定
          </button>
        </div>
      </template>
    </el-dialog>

    <!-- 参考图（后端签发的短时效读链接；说明文字 = 文件名（备注））；⭐ 弹窗档 = lg 860（看图类） -->
    <el-dialog
      :model-value="shotDlg !== null"
      :title="SHOT_DIALOG_TITLE"
      width="860px"
      class="cv2-dialog cv2-dialog--lg"
      destroy-on-close
      data-v2="dlg-shots"
      @update:model-value="shotDlg = null; previewShot = null"
    >
      <div v-if="shotDlg !== null" class="cv2-shot-grid" data-v2="shot-list">
        <figure v-for="item in shotDlg.items" :key="item.id" class="cv2-shot-item">
          <button type="button" class="cv2-shot-preview" :aria-label="`放大查看${shotAlt(item)}`" @click="previewShot = item"><img :src="item.thumbnail_url || item.url" loading="lazy" :alt="shotAlt(item)" /></button>
          <figcaption>{{ shotAlt(item) }}</figcaption>
        </figure>
      </div>
    </el-dialog>
    <el-dialog
      :model-value="previewShot !== null"
      title="图片预览"
      width="min(90vw, 1100px)"
      destroy-on-close
      data-v2="shot-preview"
      @update:model-value="previewShot = null"
    >
      <img v-if="previewShot" class="cv2-shot-full" :src="previewShot.url" :alt="shotAlt(previewShot)" />
    </el-dialog>

    <!-- ⭐ 项目全文小弹窗：宽随内容（cv2-dialog--fit），正文 pre-wrap 撑开
         （⭐ 终审 P2：在根 div 内——双根会让根属性继承面变得不可预期） -->
    <el-dialog
      :model-value="projectDlg !== null"
      title="代练/陪玩项目"
      class="cv2-dialog cv2-dialog--fit"
      data-v2="dlg-project"
      @update:model-value="projectDlg = null"
    >
      <p v-if="projectDlg !== null" class="cv2-project-full" data-v2="project-full">{{ projectDlg.text }}</p>
    </el-dialog>
  </div>
  <!-- 悬停预览浮层：跟随光标的小卡（非弹窗）；pointer-events:none 不挡鼠标与滚动 -->
  <div
    v-if="hoverPreview"
    class="cv2-hovercard"
    :style="{ left: hoverPreview.x + 'px', top: hoverPreview.y + 'px' }"
    data-v2="hover-preview"
  >{{ hoverPreview.text }}</div>
</template>

<style scoped>
.cv2-shot-preview { display: block; padding: 0; border: 0; background: transparent; cursor: zoom-in; }
.cv2-shot-full { display: block; max-width: 100%; max-height: 75vh; margin: auto; object-fit: contain; }
/* 全局提示 = 对标 layer.msg 居中深条（业务方「那种全局提示 弹到中间展示」）：
 * 覆盖 carbon.css 的顶部白卡片形态；深底 = --tt2-toast-bg（ink 85% 透明派生）。 */
.cv2-page :deep(.cv2-toast) {
  top: 45%;
  left: 50%;
  transform: translate(-50%, -50%);
  z-index: 3000;
  max-width: 56vw;
  padding: 12px 22px;
  border: none;
  border-radius: 6px;
  background: var(--tt2-toast-bg);
  color: var(--tt2-panel);
  font-size: 14px;
  text-align: center;
}

.cv2-page :deep(.cv2-toast--error) {
  color: var(--tt2-panel);
}

/* 订单号列定宽后：td 转弹性行布局，超长单号自身省略号截断、复制钮不被挤出
 * （carbon.css 的 td 级 ellipsis 会把「…」打在行尾吞掉复制钮，这里只截单号）。 */
.cv2-td-order {
  display: flex;
  align-items: center;
  overflow: hidden;
}

.cv2-td-order .cv2-order-no {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

.cv2-td-order .cv2-urgent,
.cv2-td-order .cv2-copybtn {
  flex: none;
}
</style>

<style scoped>
.hall-games { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
.hall-game { border: 1px solid var(--tt2-line); background: var(--tt2-panel); color: var(--tt2-ink); border-radius: 4px; padding: 8px 16px; cursor: pointer; }
.hall-game--active { background: var(--tt2-primary); border-color: var(--tt2-primary); color: var(--tt2-panel); }
.cv2-total, .cv2-total-notice { color: var(--tt2-summary-orange); }
</style>
