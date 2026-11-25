<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, useTemplateRef, watch, watchPostEffect } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  f7Badge as F7Badge,
  f7Block as F7Block,
  f7BlockTitle as F7BlockTitle,
  f7Button as F7Button,
  f7Card as F7Card,
  f7Chip as F7Chip,
  f7Icon as F7Icon,
  f7List as F7List,
  f7ListInput as F7ListInput,
  f7ListItem as F7ListItem,
  f7Popover as F7Popover,
  f7Popup as F7Popup,
  f7Preloader as F7Preloader,
  f7Sheet as F7Sheet,
  f7Toggle as F7Toggle,
  f7,
} from 'framework7-vue'
import type { Dialog, Popover, Popup, Sheet } from 'framework7/types'
import MobileSearch from '../components/MobileSearch.vue'
import MobileSelect from '../components/MobileSelect.vue'
import {
  EMPTY_HALL_FILTER,
  hallFilterKey,
  readHallFilter,
  splitHallKeywords,
  validateHallFilter,
  writeHallFilter,
  type HallFilter,
} from '../utils/hallFilter'
import { Toast, confirm } from '../framework7Ui'
import HallOrderHeading from '../components/HallOrderHeading.vue'
import MobilePhotoBrowser from '../components/MobilePhotoBrowser.vue'
import MobilePagedList from '../components/MobilePagedList.vue'
import MobilePullToRefresh from '../components/MobilePullToRefresh.vue'
import { useSessionStore } from '@/stores/session'
import { BizError } from '@/api/errors'
import { fetchGames, fetchServers, type ConfigRow, type ServerRow } from '@/api/config'
import {
  claimOrderWithMessage,
  claimPeerMirror,
  claimSimulatedHallOrder,
  fetchHall,
  fetchHallClaimTarget,
  PEER_CLAIM_ACCEPTED_TEXT,
  PEER_CLAIM_TAKEN_CODE,
  type HallListItem,
} from '@/api/hall'
import { claimUpstreamMirrorWithMessage } from '@/api/jidanpuUpstream'
import { fetchScreenshots, type ScreenshotItem } from '@/api/screenshots'
import { isPlatformWithdrawWarning } from '@/config/backendMessage'
import {
  AUTO_REFRESH_ALIVE_MS,
  AUTO_REFRESH_OFF,
  AUTO_REFRESH_STEPS_MS,
  normalizeAutoRefreshMs,
  readStoredAutoRefreshMs,
  writeStoredAutoRefreshMs,
} from '../utils/hallAutoRefresh'
import {
  claimTargetOf,
  hallOrderNoDisplay,
  hallRowKey,
  hallServerDisplay,
  isClaimableMirrorPk,
  isHallClaimLink,
  type ClaimSourceRow,
  type ClaimTarget,
} from '../utils/hallDisplay'
// 手机端 ISO 时间显示（我的单「接手时间」/ 详情页共用）——团团通镜像卡的「发布」时刻也用它。
import { formatDateTime } from '../utils/datetime'
// 剪贴板**带降级**（clipboard API → execCommand）：http 内网域名下 clipboard 不可用。
import { copyToClipboard } from '../utils/clipboard'

/** 分页大小（spec §5.3：`page_size=20`；与桌面 `PAGE_LIMIT` :337 同值）。 */
const PAGE_SIZE = 20
/**
 * 抢单按钮的权限节点——与后端大厅列表 / 两路 claim **同一个**门
 * （spec §2.2：`order_views.py` 的 `claim` / `upstream_claim`）；**不是**栏目码 `grab_hall`。
 */
const CLAIM_NODE = 'grab_hall.take_order'
/** 关键字防抖毫秒（任务书 T2「300ms 防抖或回车二选一」，本页钉防抖；测试用假定时器推这一拍）。 */
const KEYWORD_DEBOUNCE_MS = 300
const CLAIM_PRELOADER_DELAY_MS = 450

const session = useSessionStore()
const router = useRouter()
const route = useRoute()
const hallPage = useTemplateRef<HTMLElement>('hallPage')
const moreMenu = useTemplateRef<InstanceType<typeof F7Popover>>('moreMenu')
const moreTrigger = useTemplateRef<InstanceType<typeof F7Button>>('moreTrigger')
const firstMoreAction = useTemplateRef<InstanceType<typeof F7Button>>('firstMoreAction')
const moreMenuId = useId()
const refreshPanel = useTemplateRef<HTMLElement>('refreshPanel')

/* ── 游戏字典 ── */
const games = ref<ConfigRow[]>([])
/** 字典加载态：`loading`（首屏）→ `ready` / `error`。⛔ 空态文案只在 `ready` 且空时出现——
 * 加载中闪「未分配游戏权限」是撒谎，加载失败则显示错误本身（文案必须按实现写）。 */
const gamesState = ref<'loading' | 'ready' | 'error'>('loading')
const gamesError = ref('')
const selectedGame = ref<number | null>(null)

const gamesMap = computed(() => new Map(games.value.map((g) => [g.id, g.name] as const)))
const gameChoices = computed(() => games.value.map(({ id, name }) => ({ value: id, label: name })))
const AUTO_REFRESH_CHOICES = AUTO_REFRESH_STEPS_MS.map((value) => ({ value, label: `${value / 1000} 秒` }))

const filterKey = computed(() => hallFilterKey(session.user, selectedGame.value))
const sessionFilters = new Map<string, HallFilter>()
const activeFilter = ref<HallFilter>({ ...EMPTY_HALL_FILTER })
const filterDraft = ref<HallFilter>({ ...EMPTY_HALL_FILTER })
const showHallFilter = ref(false)
const showHallMore = ref(false)
const showRefreshChoices = ref(false)
const filterError = ref('')
let filterSheet: Sheet.Sheet | undefined
let morePopover: Popover.Popover | undefined
let refreshSheet: Sheet.Sheet | undefined
let nextPanel: 'filter' | 'refresh' | null = null
let unmounted = false

function onFilterSheetOpen(sheet: Sheet.Sheet): void {
  filterSheet = sheet
}

watch(filterKey, (key) => {
  activeFilter.value = key === null ? { ...EMPTY_HALL_FILTER } : sessionFilters.get(key) ?? readHallFilter(key)
  showHallFilter.value = false
}, { immediate: true, flush: 'sync' })

const includeWords = computed(() => splitHallKeywords(activeFilter.value.include))
const excludeWords = computed(() => splitHallKeywords(activeFilter.value.exclude))
const filterChips = computed(() => {
  const { min, max } = activeFilter.value
  const price = min && max ? `¥${min}–${max}` : min ? `¥${min} 起` : max ? `最高 ¥${max}` : ''
  return [
    price,
    includeWords.value.length ? '包含 ' + includeWords.value.join('、') : '',
    excludeWords.value.length ? '排除 ' + excludeWords.value.join('、') : '',
  ].filter(Boolean)
})

function openHallFilter(): void {
  filterDraft.value = { ...activeFilter.value }
  filterError.value = ''
  showHallFilter.value = true
}

function openMore(): void {
  if (showHallMore.value || showHallFilter.value || showRefreshChoices.value) return
  const element = moreMenu.value?.$el
  const trigger = moreTrigger.value?.$el
  if (element && trigger) f7.popover.open(element, trigger, false)
}

function onMoreOpen(popover: Popover.Popover): void {
  morePopover = popover
  // The Vue wrapper omits the animation option when creating the popover.
  popover.params.animate = false
  showHallMore.value = true
}

function openFromMore(panel: 'filter' | 'refresh'): void {
  nextPanel = panel
  closeMore()
}

function closeMore(): void {
  const popover = morePopover
  if (!popover?.opened) return
  // iOS temporarily removes this class while positioning the menu on the next frame.
  const element = popover.el
  if (!element.classList.contains('modal-in')) {
    element.classList.add('modal-in')
    requestAnimationFrame(() => {
      if (morePopover !== popover) element.classList.remove('modal-in')
    })
  }
  popover.close(false)
}

function focusMoreTrigger(): void {
  if (!unmounted) moreTrigger.value?.$el.focus()
}

function focusMoreAction(): void {
  firstMoreAction.value?.$el.focus()
}

function focusRefreshChoice(): void {
  const active = refreshPanel.value?.querySelector<HTMLButtonElement>('[aria-pressed="true"]')
  const firstChoice = refreshPanel.value?.querySelector<HTMLButtonElement>('[data-refresh-interval]')
  const choice = active ?? firstChoice
  choice?.focus()
}

function trapOverlayFocus(event: KeyboardEvent): void {
  if (!(event.currentTarget instanceof HTMLElement)) return
  const controls = event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), [tabindex="0"]')
  const first = controls[0]
  const last = controls[controls.length - 1]
  if (!first || !last || document.activeElement !== (event.shiftKey ? first : last)) return
  event.preventDefault()
  const target = event.shiftKey ? last : first
  target.focus()
}

function onMoreClosed(): void {
  morePopover = undefined
  showHallMore.value = false
  const panel = nextPanel
  nextPanel = null
  if (unmounted) return
  if (panel === 'filter') openHallFilter()
  else if (panel === 'refresh') showRefreshChoices.value = true
  else focusMoreTrigger()
}

function onRefreshClosed(): void {
  refreshSheet = undefined
  focusMoreTrigger()
}

function updateHallFilter(filter: HallFilter): void {
  const keepRows = filter.min === activeFilter.value.min && filter.max === activeFilter.value.max
  const key = filterKey.value
  if (key !== null) {
    sessionFilters.set(key, filter)
    writeHallFilter(key, filter)
  }
  activeFilter.value = filter
  showHallFilter.value = false
  void loadFirst(keepRows)
}

function saveHallFilter(): void {
  const draft = filterDraft.value
  const filter = {
    min: draft.min.trim(),
    max: draft.max.trim(),
    include: splitHallKeywords(draft.include).join('，'),
    exclude: splitHallKeywords(draft.exclude).join('，'),
  }
  filterError.value = validateHallFilter(filter)
  if (!filterError.value) updateHallFilter(filter)
}

function clearHallFilter(): void {
  updateHallFilter({ ...EMPTY_HALL_FILTER })
}

/* ── 区服字典（随游戏重建；只服务原生行的 `server` 主键映射） ── */
const serverNames = ref(new Map<number, string>())
const resolveServerName = (id: number | null): string =>
  id === null ? '' : (serverNames.value.get(id) ?? '')

/* ── 列表状态 ── */
const items = ref<HallListItem[]>([])
const visibleRows = computed(() => {
  const includes = includeWords.value.map((word) => word.toLowerCase())
  const excludes = excludeWords.value.map((word) => word.toLowerCase())
  if (!includes.length && !excludes.length) return items.value
  return items.value.filter((row) => {
    if (row.is_exited) return true
    const project = row.project_text.toLowerCase()
    return !excludes.some((word) => project.includes(word))
      && (!includes.length || includes.some((word) => project.includes(word)))
  })
})
const total = ref(0)
/** 分页按服务端已返回的行数推进，本地移除刚抢到的卡片不改变分页位置。 */
const loadedCount = ref(0)
const lastLoadedPage = ref(0)
/** 全量重取（首屏 / 下拉 / 自动 / 1012 / 换游戏）的在途闸——同一时刻只允许一条。 */
const refreshBusy = ref(false)
const loadingFirstPage = ref(false)
/** 「加载更多」的在途闸（与上面的全量重取分开：它们描述的不是同一件事）。 */
const listLoading = ref(false)
let queuedLoadMore = false
/** 加载更多 / 首屏失败的文案（不改已有内容；出现重试按钮）。 */
const listError = ref('')
const refreshing = ref(false)
const autoRefreshState = ref<'idle' | 'refreshing' | 'failed'>('idle')
const lastListRefresh = ref<Date | null>(null)
const lastRefreshTime = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
})
const keyword = ref('')
/** 已提交的关键字（防抖后写入；请求带的是它，不是正在输入的 `keyword`）。 */
const keywordQuery = ref('')
/** 抢单成功后的提示（到「我的订单」查看）；下一次列表重取时清掉。 */
const claimHint = ref('')
const quickClaimHint = ref('')
let quickClaimDialog: ReturnType<typeof confirm> | undefined
/** 抢单在途闸（任务书：claim 请求在途时禁点）。 */
const claiming = ref(false)
let claimPreloaderTimer: number | undefined
let claimPreloader: Dialog.Dialog | undefined
let claimPreloaderFocus: HTMLElement | null = null

function scheduleClaimPreloader(): void {
  claimPreloaderTimer = window.setTimeout(() => {
    claimPreloaderTimer = undefined
    if (!claiming.value || unmounted || document.querySelector('.dialog.modal-in, .dialog.modal-out')) return
    const dialog = f7.dialog.preloader('正在抢单…')
    if (!dialog.opened) {
      dialog.close(false)
      dialog.destroy()
      return
    }
    const element = dialog.el
    element.setAttribute('role', 'dialog')
    element.setAttribute('aria-modal', 'true')
    element.setAttribute('aria-label', '正在抢单')
    element.tabIndex = -1
    element.addEventListener('keydown', (event) => {
      if (event.key === 'Tab') event.preventDefault()
    })
    claimPreloader = dialog
    claimPreloaderFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    element.focus()
  }, CLAIM_PRELOADER_DELAY_MS)
}

function closeClaimPreloader(): void {
  window.clearTimeout(claimPreloaderTimer)
  claimPreloaderTimer = undefined
  claimPreloader?.close(false)
  claimPreloader = undefined
  if (!unmounted && claimPreloaderFocus?.isConnected) claimPreloaderFocus.focus()
  claimPreloaderFocus = null
}

/* ── ⭐ 团团通上家镜像卡（`origin === 'peer'`，D-0303）的两个集合，键 = **镜像行主键 `mirror_pk`**（⛔ 不是上家单号：两张镜像表的主键空间互不相干，同一条上家单号可能来自两个不同上家）──
 * `peerClaimsInFlight`：**在途互斥**。⛔ 不复用布尔 `claiming`（那个是「全页只许一发」，peer 卡的在途只锁自己那张——一张卡在等上家判决时，别家上家的卡不该被连坐禁点）；`Set` + `finally` 复位（漏了复位，那张卡以后**再也接不了**，E-0079 同族）。
 * `peerClaimed`：**已受理、等上家判决**。成功之后**不删卡、不刷新**（确认制下后端那张镜像卡还在 `claiming`，删了用户会以为单没了）⇒ 置灰 + 「已接下」标记、再点一次请求都不发；列表重取（1012 / 自动刷新 / 下拉）**不清空它**（同一张卡的 `mirror_pk` 不变）。
 */
const peerClaimsInFlight = ref(new Set<number>())
const peerClaimed = ref(new Set<number>())

/** 这一行是不是 peer 卡（只有 peer 卡才读那两个集合；⛔ 别的来源 `mirror_pk` 是 null）。
 *  主键判据**复用** `isClaimableMirrorPk`（⛔ 不在这里再写一份「正整数」判据：两份必然漂移）。 */
function peerPkOf(row: ClaimSourceRow): number | null {
  if (row.origin !== 'peer') return null
  return isClaimableMirrorPk(row.mirror_pk) ? row.mirror_pk : null
}

/** 这一行是不是**已经受理、还在等上家判决**（其它来源恒 false）。 */
function isPeerClaimed(row: ClaimSourceRow): boolean {
  const pk = peerPkOf(row)
  return pk !== null && peerClaimed.value.has(pk)
}

/** 这一行的 peer 接单**是否在途**（模板按钮禁点用；其它来源恒 false）。 */
function isPeerClaimPending(row: ClaimSourceRow): boolean {
  const pk = peerPkOf(row)
  return pk !== null && peerClaimsInFlight.value.has(pk)
}

/**
 * 卡片上的「发布」时刻（**只给 peer 卡用**）：peer 行的 `created_at` 是后端填的 `first_seen_at`（= 这张卡**进我方大厅的时刻**）。
 * ⚠️ 不自己 `new Date(str)`：走本端既有的 `formatDateTime`（按串内偏移换算到项目时区，手机跨时区设备才不会整点平移）；认不出的串 → 空串，模板据此不画那一段（⛔ 不留空壳）。
 */
const peerTimeOf = (row: Pick<HallListItem, 'created_at'>): string => formatDateTime(row.created_at)

const finished = computed(() => loadedCount.value >= total.value)
const hasMore = computed(() => !finished.value && lastLoadedPage.value > 0)
/** 分页列表的 `asyncLoading`：'loading' 转圈 / 'load-more' 可点加载 / 其余不渲染加载条。 */
const asyncLoading = computed<'loading' | 'load-more' | undefined>(() => {
  if (loadingFirstPage.value) return undefined
  if (listLoading.value) return 'loading'
  if (hasMore.value) return 'load-more'
  return undefined
})

/**
 * 抢单按钮的**权限门**（fail-closed，桌面 :582-583 同口径）：`canNode` 取不到 me 时按空集处理
 * ⇒ 不渲染按钮（⛔ 不置灰；先例 D-0154 / E-0098）。
 */
const canClaim = computed(() => session.canNode([CLAIM_NODE]))

const gameNameOf = (row: HallListItem): string => {
  if (row.game === null || row.game === undefined) return '—'
  return gamesMap.value.get(row.game) ?? String(row.game)
}

/**
 * 卡片「订单号」那一行：文本 / 标签的判据在 `utils/hallDisplay.ts`
 * 的 `hallOrderNoDisplay`（原生 = 我方单号；镜像 = 上家单号，标注「上家单号」）。
 * 模板里调用两次（标签 + 文本），纯函数、无副作用。
 */
const orderNoOf = (row: HallListItem) => hallOrderNoDisplay(row)

function hallCardKey(row: HallListItem): string {
  // 不同鸡蛋铺上家可能使用相同单号，本地镜像主键能区分它们。
  if (row.origin === 'upstream' && isClaimableMirrorPk(row.mirror_pk)) return `upstream-mirror:${row.mirror_pk}`
  return hallRowKey(row)
}

/**
 * 点订单号 → 复制完整单号 + toast「订单号已复制」。
 * 走**带降级**的 `copyToClipboard`（http 内网域名下 clipboard 必失败，桌面那条线踩过）；
 * 失败原样提示，⛔ 不假装成功。
 */
async function onCopyOrderNo(row: HallListItem): Promise<void> {
  const { text } = orderNoOf(row)
  if (text === '') return // 两源都没有（数据异常）⇒ 这一行本就没画
  const ok = await copyToClipboard(text)
  if (ok) Toast.success('订单号已复制')
  else Toast.error('复制失败，请手动选中复制')
}

/**
 * 卡片全量详情浮层（业务方裁定）——**页内浮层，不跳详情页**。为什么不跳订单详情页（路由 `/m/orders/:orderNo`）：那个页只认 `/my-orders/<单号>/detail/`，后端硬判「我是这单的接单人」，而大厅只有**没人接**的单（`WAITING_GRAB`）⇒ 未接单点进去必然 403（`tests/test_my_order_detail_api.py::test_unclaimed_order_is_403_for_everyone`）。
 * 所以详情在**本页**用大厅列表**已经返回**的字段渲染（⛔ 不跳详情页）；镜像行（上家单）同样能打开：单号那行按 `orderNoOf().label` 显示「上家单号」。
 * ⭐ 唯一例外（D-0296）：原生在厅单的「参考图」段会现取 screenshots 接口（短时效签名，见 `loadDetailShots`）；其余字段仍然零请求。
 */
const detailRow = ref<HallListItem | null>(null)
let detailPopup: Popup.Popup | undefined
let detailTrigger: HTMLButtonElement | null = null

/** 打开浮层：存**当行快照**（列表之后的刷新不影响已打开的浮层）。
 *  ⭐ 参考图（D-0296）：每次打开都**现取**（见 `loadDetailShots`），关门时作废在途请求。 */
function openDetail(row: HallListItem, event: MouseEvent): void {
  const card = (event.currentTarget as HTMLElement).closest('.m-hall-card')
  detailTrigger = card?.querySelector<HTMLButtonElement>('.m-hall-card__project') ?? null
  detailRow.value = row
  void loadDetailShots()
}

function focusDetailClose(popup: Popup.Popup): void {
  void nextTick(() => {
    if (detailPopup !== popup || detailRow.value === null) return
    popup.el.querySelector<HTMLButtonElement>('.m-hall-detail__close')?.focus({ preventScroll: true })
  })
}

function closeDetail(): void {
  if (detailRow.value === null) return
  viewerVisible.value = false
  detailRow.value = null
  // 关浮层顺手作废在途的参考图请求（回来也不许写进下一次打开的浮层）。
  detailShotsSeq.value += 1
  detailShotsLoading.value = false
  detailShots.value = []
}

function onDetailPopupClosed(): void {
  detailPopup = undefined
  closeDetail()
  if (!unmounted && detailTrigger?.isConnected) detailTrigger.focus()
  detailTrigger = null
}

/* 参考图只为有权限的原生在厅单现取，避免缓存会过期的签名 URL。
 * 加载结束后只展示订单截图。无图、失败或无权限时隐藏整段。
 */
const canViewShots = computed(() => session.canNode(['order.view_screenshot']))
const detailShots = ref<ScreenshotItem[]>([])
const detailShotsLoading = ref(false)
/** 取数代次：浮层开关之间可能有请求在飞，只让最后一次打开的结果落地。 */
const detailShotsSeq = ref(0)

async function loadDetailShots(): Promise<void> {
  const row = detailRow.value
  if (row === null) return
  const mySeq = ++detailShotsSeq.value
  detailShots.value = []
  const canLoad = row.origin === 'native' && !row.is_exited && row.order_no !== '' && canViewShots.value
  detailShotsLoading.value = canLoad
  if (!canLoad) return
  try {
    const data = await fetchScreenshots(row.order_no)
    if (mySeq === detailShotsSeq.value) detailShots.value = data.items.filter((item) => item.img_type === 0)
  } catch {
    // 参考图取数失败时仍可查看订单详情。
  } finally {
    if (mySeq === detailShotsSeq.value) detailShotsLoading.value = false
  }
}

/**
 * 浮层里的「抢单」：复用列表上那条抢单逻辑（确认弹层 / 权限门 / 分流全在 `onClaimClick`）。
 * 先收起浮层，确认弹层才不会被压在浮层下面；抢单结果仍按原逻辑提示 / 移除卡片 / 1012 重取。
 */
function onDetailClaim(): void {
  const row = detailRow.value
  if (row === null) return
  detailTrigger = null
  closeDetail()
  onClaimClick(row)
}

/**
 * 浮层里的「区服」：原生行走区服字典；镜像行是**上家文本**（`remote_game_area` +
 * `remote_game_server`），两段都空才给「—」。
 */
function detailServerText(row: HallListItem): string {
  if (row.origin !== 'native') {
    const parts = [row.remote_game_area, row.remote_game_server]
      .map((part) => (part ?? '').trim())
      .filter((part) => part !== '')
    return parts.length > 0 ? parts.join(' ') : '—'
  }
  return hallServerDisplay(row, resolveServerName)
}

function cardMetaOf(row: HallListItem): string {
  const server = detailServerText(row)
  const peerTime = row.origin === 'peer' ? peerTimeOf(row) : ''
  return [
    peerTime && `进厅 ${peerTime.slice(-5)}`,
    server === '—' ? '' : server,
    row.time_requirement || row.remote_hours_desc,
  ].filter(Boolean).join(' · ')
}

function hasDistinctRemark(row: HallListItem): boolean {
  const remark = row.remark.trim()
  return remark !== '' && remark !== row.project_text.trim()
}

/** 参考图预览（照 OrderScreenshotsPage：受控 index，点哪张从哪张看起）。 */
const viewerVisible = ref(false)
const viewerIndex = ref(0)
const viewerImages = computed(() => detailShots.value.map((item) => item.url))

function openShotPreview(index: number): void {
  if (index < 0 || index >= viewerImages.value.length) return
  viewerIndex.value = index
  viewerVisible.value = true
}

/* ── 取数 ── */

/**
 * 重取**代数**：每次新的取数意图 `++reloadSeq`；响应落地前校验自己仍是最新代，**陈旧响应整体丢弃**（⛔ 不写 items / total / listError，也不动 `refreshBusy`）。
 * ⛔ 取数入口**不许**用 `refreshBusy` 早退丢弃意图——自动重取在途时用户切游戏 / 防抖到点 / 手动下拉，新的重取被静默吞掉会让列表停在旧筛选的数据上；在途就并发发、落地时「最后意图赢」。
 */
let reloadSeq = 0

// 每次列表重取仅屏蔽在它发起后才抢到的旧卡，之后发起的重取仍能显示重新进厅的订单。
const activeListReads = new Set<Set<string>>()

function beginListRead(): Set<string> {
  const claimed = new Set<string>()
  activeListReads.add(claimed)
  return claimed
}

function omitStaleClaims(rows: HallListItem[], claimed: Set<string>): HallListItem[] {
  return claimed.size ? rows.filter((row) => row.is_exited || !claimed.has(hallCardKey(row))) : rows
}

async function fetchPage(page: number): Promise<{ items: HallListItem[]; total: number }> {
  const data = await fetchHall({
    game: selectedGame.value,
    keyword: keywordQuery.value,
    page,
    page_size: PAGE_SIZE,
    include_exited: 1,
    price_min: activeFilter.value.min,
    price_max: activeFilter.value.max,
  })
  return { items: data.items, total: data.total }
}

/** 只有服务端条件不变时，才保留已加载行供本地关键词即时筛选。 */
async function loadFirst(keepRows = false): Promise<void> {
  if (selectedGame.value === null) return
  const mySeq = ++reloadSeq
  queuedLoadMore = false
  listLoading.value = false
  stopRefreshMotion()
  refreshBusy.value = true
  loadingFirstPage.value = true
  autoRefreshState.value = 'idle'
  listError.value = ''
  claimHint.value = ''
  if (!keepRows) {
    items.value = []
    total.value = 0
    loadedCount.value = 0
    lastListRefresh.value = null
  }
  lastLoadedPage.value = 0
  const claimed = beginListRead()
  try {
    const page = await fetchPage(1)
    if (mySeq !== reloadSeq) return // 已有更新的重取意图：陈旧响应丢弃
    items.value = omitStaleClaims(page.items, claimed)
    total.value = page.total
    loadedCount.value = page.items.length
    lastLoadedPage.value = 1
    lastListRefresh.value = new Date()
  } catch (e) {
    if (mySeq === reloadSeq) listError.value = errText(e) // 陈旧响应的失败同样不写
  } finally {
    activeListReads.delete(claimed)
    // 只有自己的代还是最新代时才收「忙」标志：过期代落地时不碰更新意图的标志。
    if (mySeq === reloadSeq) {
      refreshBusy.value = false
      loadingFirstPage.value = false
    }
  }
}

/**
 * 上拉加载更多：**追加**下一页；失败**保留**已有内容、给可重试的错误条（任务书 T2）。
 * 追加落地同样过代数校验（发起时不递增——它不使在途的重取意图过期；但任何其后发起的新意图
 * 都会让这次追加的落地失效，⛔ 不许把旧列表的行追加到新列表上）。
 */
async function onLoadMore(): Promise<void> {
  if (listLoading.value || !hasMore.value || selectedGame.value === null) return
  if (refreshBusy.value) {
    // 等后台刷新结束，用最新的分页信息续取一次。
    if (autoRefreshState.value === 'refreshing') queuedLoadMore = true
    return
  }
  const mySeq = reloadSeq
  listLoading.value = true
  listError.value = ''
  const claimed = beginListRead()
  try {
    const page = lastLoadedPage.value + 1
    const data = await fetchPage(page)
    if (mySeq !== reloadSeq) return // 期间发起了重取：追加落地丢弃
    items.value = [...items.value, ...omitStaleClaims(data.items, claimed)]
    total.value = data.total
    loadedCount.value += data.items.length
    lastLoadedPage.value = page
  } catch (e) {
    if (mySeq === reloadSeq) listError.value = errText(e)
  } finally {
    activeListReads.delete(claimed)
    if (mySeq === reloadSeq) listLoading.value = false
  }
}

/**
 * 重取**当前已加载**的那些页（桌面 `reload(currentPage)`「保持当前页与当前筛选」的列表流等价物：手机端是累积列表，按已加载页数重取后整体替换）。
 * 失败保留当前列表。下拉失败弹出提示，后台轮询失败更新行内状态。
 */
async function refreshLoaded(source: 'manual' | 'pull' | 'auto' | 'conflict'): Promise<void> {
  if (selectedGame.value === null) return
  const mySeq = ++reloadSeq
  queuedLoadMore = false
  listLoading.value = false
  stopRefreshMotion()
  refreshBusy.value = true
  loadingFirstPage.value = false
  autoRefreshState.value = source === 'auto' ? 'refreshing' : 'idle'
  claimHint.value = ''
  const claimed = beginListRead()
  try {
    const pages = Math.max(1, lastLoadedPage.value)
    const acc: HallListItem[] = []
    let newTotal = 0
    for (let page = 1; page <= pages; page++) {
      if (mySeq !== reloadSeq) return // 有更新的意图：剩余页不必再取（落地反正会被丢）
      const data = await fetchPage(page)
      acc.push(...data.items)
      newTotal = data.total
    }
    if (mySeq !== reloadSeq) return
    items.value = omitStaleClaims(acc, claimed)
    total.value = newTotal
    loadedCount.value = acc.length
    if (lastLoadedPage.value === 0) listError.value = ''
    lastLoadedPage.value = pages
    lastListRefresh.value = new Date()
    autoRefreshState.value = 'idle'
    if (source === 'auto') void nextTick(() => {
      if (mySeq === reloadSeq) playRefreshMotion()
    })
  } catch (e) {
    if (mySeq !== reloadSeq) return
    if (source === 'auto') autoRefreshState.value = 'failed'
    else if (source === 'manual' || source === 'pull') Toast.error(errText(e))
  } finally {
    activeListReads.delete(claimed)
    if (mySeq === reloadSeq) {
      refreshBusy.value = false
      if (queuedLoadMore) {
        queuedLoadMore = false
        void onLoadMore()
      }
    }
  }
}

/** 下拉刷新：回到第 1 页重取（桌面 `.refresh` → `reload(1)` :797-800 同语义），结束收起动画。 */
async function onPullRefresh(): Promise<void> {
  refreshing.value = true
  await refreshLoaded('pull')
  refreshing.value = false
}

/** 重试按钮：首屏失败回第 1 页，加载更多失败续下一页。 */
function onRetryClick(): void {
  if (lastLoadedPage.value === 0) void loadFirst(items.value.length > 0)
  else void onLoadMore()
}

/* ── 筛选 ── */

/** 切游戏：换区服字典 + 回第 1 页重取并清空列表。字典失败不拦列表（桌面 :646-657 同处置）。 */
async function onGameSelect(gameId: number): Promise<void> {
  if (!gamesMap.value.has(gameId) || gameId === selectedGame.value) return
  await loadGame(gameId)
}

async function loadGame(gameId: number): Promise<void> {
  selectedGame.value = gameId
  applyServers([])
  const list = loadFirst()
  try {
    const servers = await fetchServers(gameId)
    if (selectedGame.value === gameId) applyServers(servers)
  } catch (e) {
    if (selectedGame.value === gameId) Toast.error(errText(e))
  }
  await list
}

function applyServers(rows: ServerRow[]): void {
  serverNames.value = new Map(rows.map((s) => [s.id, s.name] as const))
}

/** 关键字 300ms 防抖（任务书钉的触发方式）；到点提交并回第 1 页。 */
watch(keyword, (next) => {
  if (keywordTimer !== null) window.clearTimeout(keywordTimer)
  keywordTimer = window.setTimeout(() => {
    keywordTimer = null
    if (next === keywordQuery.value) return
    keywordQuery.value = next
    void loadFirst()
  }, KEYWORD_DEBOUNCE_MS)
})
let keywordTimer: number | null = null

/* ── 抢单（两路分流 + 确认弹层 + 并发闸） ── */

/**
 * 点「抢单」：`claimTargetOf` 是渲染期与点击期**共用**的分流判据（`utils/hallDisplay.ts`）——按钮只在 `canClaim && target ≠ null` 时渲染（两道门照桌面：权限门 `canNode` 取不到 me 不画 + 数据门镜像主键不可用不画）。
 * 点击期对 `target` 再收窄一次是**形状先行拒绝**（桌面同一处置：按钮是渲染期画的，但 DOM 的 data-* 可被人为改 ⇒ 形状不对一次请求都不发）；`canClaim` 再判一次是给「按钮刚被 v-if 摘掉前的一瞬」兜底（fail-closed 不亏）。
 */
function onClaimClick(row: HallListItem, gameName = gameNameOf(row), onClosed?: () => void) {
  if (claiming.value) return
  // ⛔ 灰卡（已被抢 / 已撤下）不可接（D-0296）：按钮是渲染期不画的（`hasClaimTarget`），
  // 点击期再挡一道是同一处置——DOM 的 data-* 可被人为改，fail-closed 不亏。
  if (row.is_exited) return
  // ⭐ peer 卡：在途 / 已受理时**连确认框都不弹**（双击那第二下在这里就被吃掉）。
  const peerPk = peerPkOf(row)
  if (peerPk !== null && (peerClaimsInFlight.value.has(peerPk) || peerClaimed.value.has(peerPk))) {
    return
  }
  const target = claimTargetOf(row)
  if (target === null || !canClaim.value) return
  return confirm({
    title: '抢单确认',
    // 任务书 T2：确认文案要**含单价与游戏名**。`hall_price` 可为 null（抛单没带价，抢单会被
    // 后端 1013 拒）——显示「未定价」不编数字。
    content: `确定接下【${gameName}】这张单吗？接单价 ${row.hall_price ?? '未定价'} 元。`,
    confirmBtn: '接单',
    cancelBtn: '再想想',
    onConfirm: () => {
      void confirmClaim(row, target)
    },
    onClosed,
  })
}

const claimLinkQuery = [
  () => route.query.claimSource,
  () => route.query.claimRef,
  () => route.query.claimUnavailable === '1',
] as const

watch(claimLinkQuery, async ([source, ref, unavailable], _previous, onCleanup) => {
  if (source === undefined && ref === undefined && !unavailable) return
  quickClaimDialog?.close()
  quickClaimDialog = undefined
  quickClaimHint.value = ''
  let current = true
  onCleanup(() => { current = false })
  try {
    if (unavailable) {
      quickClaimHint.value = '抢单链接已失效，请在大厅查看新单'
      return
    }
    if (!canClaim.value || typeof ref !== 'string' || !isHallClaimLink(source, ref)) {
      quickClaimHint.value = '该抢单链接不可用，请在大厅查看可接订单。'
      return
    }
    quickClaimHint.value = '正在核对该笔订单…'
    const order = await fetchHallClaimTarget(source, ref)
    if (!current) return
    const matches = source === 'local'
      ? order.origin === 'native' && order.order_no === ref
      : order.origin === source && String(order.mirror_pk) === ref
    if (matches) {
      const dialog = onClaimClick(order, order.game_name || gameNameOf(order), () => {
        if (quickClaimDialog === dialog) quickClaimDialog = undefined
      })
      quickClaimDialog = dialog
    }
    quickClaimHint.value = quickClaimDialog ? '' : '该订单暂时无法接单，请在大厅查看。'
  } catch (error) {
    if (current) quickClaimHint.value = errText(error)
  } finally {
    if (current) {
      const query = { ...route.query }
      delete query.claimSource
      delete query.claimRef
      if (unavailable) delete query.claimUnavailable
      void router.replace({ query, hash: route.hash })
    }
  }
}, { immediate: true })

/**
 * 真正发抢单请求：按分流结果走**各自**的端点（原生 = `claimOrder(order_no)`；上家镜像 = `claimUpstreamMirror(mirror_pk)`，⛔ 不是 `remote_order_id`；模拟单 = `claimSimulatedHallOrder(sim_pk)`；⭐ 团团通镜像 = `claimPeerMirror(mirror_pk)`，D-0303）。
 * 成功：toast「抢单成功」（spec 的自拟白名单句）+ 移除该卡片 + 提示到「我的订单」查看。失败：toast **后端 msg 原样**（⛔ 不静默失败、不自造文案），且 **1012 时自动重取当前已加载列表**。
 * ⚠️ 模拟单的接单端点**永远**回 1012（D-0296：模拟卡接不走是产品语义）⇒ 只会走失败支：toast + 重取，列表回来时那张卡已变灰卡——⛔ 不存在「模拟单抢单成功」。⭐ peer 支单独走 `confirmPeerClaim`（确认制下「成功」只代表**已受理**，文案 / 置灰 / 刷新三条都不同）。
 */
async function confirmClaim(row: HallListItem, target: ClaimTarget): Promise<void> {
  if (target.kind === 'peer') {
    await confirmPeerClaim(target.mirrorPk)
    return
  }
  if (claiming.value) return
  claiming.value = true
  scheduleClaimPreloader()
  try {
    // ⭐ 抢单成功后后端可能自动撤回该单挂着的代练通订单；撤不掉时**仍是成功码**，
    // 只在 `msg` 里带警告 ⇒ 必须把 `msg` 原文显示出来（⛔ 不能只弹固定的「抢单成功」）。
    let backendMessage = ''
    if (target.kind === 'upstream') {
      backendMessage = (await claimUpstreamMirrorWithMessage(target.mirrorPk)).message
    } else if (target.kind === 'simulated') {
      await claimSimulatedHallOrder(target.simPk)
    } else {
      backendMessage = (await claimOrderWithMessage(target.orderNo)).message
    }
    const key = hallCardKey(row)
    for (const claimed of activeListReads) claimed.add(key)
    items.value = items.value.filter((it) => hallCardKey(it) !== key)
    if (backendMessage.trim() === '') {
      Toast.success('抢单成功')
    } else if (isPlatformWithdrawWarning(backendMessage)) {
      // 警告要读得完 + 看得见（TDesign 的 warning 主题，停留更久）。
      Toast.warning({ message: backendMessage, duration: 8000 })
    } else {
      Toast.success({ message: backendMessage, duration: 6000 })
    }
    claimHint.value = '抢单成功，可到「我的订单」查看'
  } catch (e) {
    Toast.error(errText(e))
    if (e instanceof BizError && e.code === 1012) void refreshLoaded('conflict')
  } finally {
    claiming.value = false
    closeClaimPreloader()
  }
}

/**
 * ⭐ 团团通镜像卡接单（D-0303）——与桌面 `order-insiderecorderlist.ts::claimPeerRow` **逐条同口径**：成功（`code 0`）⇒ 「正在受理中」+ 卡片**置灰 / 标已接下**（⛔ 不删卡、⛔ 不刷新：确认制下后端那张镜像卡还在 `claiming`，刷新会让它从 `listed` 投影里直接消失）；`1012` ⇒ toast 后端 `msg` + **重取已加载列表**；其余（未开同步 / 抢单闸 / 押金 / 实名 / 403 / 网络）⇒ toast `msg`、卡保留、不刷新。
 * ⚠️ **在途互斥在这里也判一次**（承重）：双击会先弹出两个确认框，两个都点「确定」时点击期那道闸（在弹框**之前**）已经过了 ⇒ 只判一次就会发两发（桌面同一处置）。用**集合**而不是全局 `claiming`：peer 卡的在途只锁自己那张，不连坐别的卡。
 */
async function confirmPeerClaim(mirrorPk: number): Promise<void> {
  if (peerClaimsInFlight.value.has(mirrorPk) || peerClaimed.value.has(mirrorPk)) return
  peerClaimsInFlight.value.add(mirrorPk)
  try {
    await claimPeerMirror(mirrorPk)
    peerClaimed.value.add(mirrorPk)
    Toast.success(PEER_CLAIM_ACCEPTED_TEXT)
  } catch (e) {
    Toast.error(errText(e))
    if (e instanceof BizError && e.code === PEER_CLAIM_TAKEN_CODE) void refreshLoaded('conflict')
  } finally {
    peerClaimsInFlight.value.delete(mirrorPk)
  }
}

/* ── 空态（没有可选游戏）的退出 ── */

async function onLogout(): Promise<void> {
  await session.logout()
  void router.replace({ name: 'm-login' })
}

/* ── 自动刷新（桌面同参移植；参数与存储的出处见 `utils/hallAutoRefresh.ts` 文件头） ── */

const autoRefreshMs = ref(readStoredAutoRefreshMs())
const autoRefreshLabel = computed(() => AUTO_REFRESH_CHOICES.find(({ value }) => value === autoRefreshMs.value)?.label)
const autoRefreshOn = computed(() => autoRefreshMs.value !== AUTO_REFRESH_OFF)
const autoRefreshToggle = useTemplateRef<InstanceType<typeof F7Toggle>>('autoRefreshToggle')
const refreshList = useTemplateRef<InstanceType<typeof MobilePagedList>>('refreshList')
const refreshSweep = ref<{ top: string; height: string } | null>(null)
const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)')
let refreshCardAnimations: Animation[] = []
let refreshScroll: HTMLElement | null = null

function stopRefreshMotion(): void {
  refreshSweep.value = null
  refreshCardAnimations.forEach((animation) => animation.cancel())
  refreshCardAnimations = []
}

function playRefreshMotion(): void {
  if (document.hidden || !autoRefreshOn.value || reducedMotion?.matches || !visibleRows.value.length) return
  if (showHallMore.value || showHallFilter.value || showRefreshChoices.value || detailRow.value
    || document.querySelector(':is(.popup, .sheet-modal, .dialog, .actions-modal, .popover):is(.modal-in, .modal-out)')) return
  const list = refreshList.value?.$el as HTMLElement | undefined
  const scroll = refreshScroll
  if (!list?.isConnected || !scroll || typeof list.animate !== 'function') return
  if (scroll.matches('.ptr-pull-down, .ptr-pull-up, .ptr-refreshing, .ptr-closing')) return

  const listBounds = list.getBoundingClientRect()
  const scrollBounds = scroll.getBoundingClientRect()
  const toolbarBounds = scroll.parentElement?.querySelector('.m-tabbar')?.getBoundingClientRect()
  const viewport = window.visualViewport
  const viewportTop = viewport?.offsetTop ?? 0
  const top = Math.max(viewportTop, scrollBounds.top, listBounds.top)
  const bottom = Math.min(
    viewportTop + (viewport?.height ?? window.innerHeight),
    scrollBounds.bottom,
    listBounds.bottom,
    toolbarBounds?.height ? toolbarBounds.top : Infinity,
  )
  if (top >= bottom) return

  const cards = list.querySelectorAll<HTMLElement>(':scope > .m-hall-card')
  // Cards are laid out in order, so skip past earlier pages without measuring each card.
  let start = 0
  let end = cards.length
  while (start < end) {
    const mid = Math.floor((start + end) / 2)
    if (cards[mid]!.getBoundingClientRect().bottom <= top) start = mid + 1
    else end = mid
  }
  const onScreen: HTMLElement[] = []
  let sweepTop = top
  let sweepBottom = top
  for (let i = start; i < cards.length && onScreen.length < 8; i += 1) {
    const card = cards[i]!
    const bounds = card.getBoundingClientRect()
    if (bounds.top >= bottom) break
    if (!onScreen.length) sweepTop = Math.max(top, bounds.top)
    sweepBottom = Math.min(bottom, bounds.bottom)
    onScreen.push(card)
  }
  if (!onScreen.length) return

  refreshSweep.value = { top: `${sweepTop - listBounds.top}px`, height: `${sweepBottom - sweepTop}px` }
  const stagger = Math.min(100, 300 / Math.max(1, onScreen.length - 1))
  refreshCardAnimations = onScreen.map((card, index) => card.animate(
    [{ opacity: 1 }, { opacity: 0.52, offset: 0.25 }, { opacity: 1 }],
    { duration: 240, delay: 95 + index * stagger, easing: 'ease-out' },
  ))
}

watchPostEffect(() => {
  // The Framework7 wrapper does not pass attributes down to its checkbox.
  const root = autoRefreshToggle.value?.$el as HTMLElement | undefined
  const input = root?.querySelector('input')
  input?.setAttribute('role', 'switch')
  input?.setAttribute('aria-label', '自动刷新')
})
let autoTimer: number | null = null
/** 自动刷新那一发**是否在途**（只有本函数发的请求才算；手动刷新不打这个标记——桌面 :728-731）。 */
let autoInFlight = false
let autoIssuedAt = 0

/** 定时器**唯一写入口**（档位变化 / 变可见 / 卸载都经过它，桌面 :928-935 同一条纪律）。 */
function restartAutoRefresh(): void {
  if (autoTimer !== null) {
    window.clearInterval(autoTimer)
    autoTimer = null
  }
  if (autoRefreshMs.value === AUTO_REFRESH_OFF) return
  autoTimer = window.setInterval(onAutoRefreshTick, autoRefreshMs.value)
}

/**
 * 到点做什么——桌面 `onAutoRefreshTick` 的逐条移植，三道闸：① 页面不可见 ⇒ 跳过这一拍（后台标签页不该高频打查询）；② 上一发未回且未超活性兜底 ⇒ 不重复发；③ 手动全量重取在途 ⇒ 这一拍让路（桌面 layui 是 fire-and-forget 的 `table.reload` 没有这层，移植到受控取数后补上，语义同「不叠加两发」）。
 */
function onAutoRefreshTick(): void {
  if (document.hidden || showHallFilter.value) return
  if (autoInFlight && Math.abs(Date.now() - autoIssuedAt) <= AUTO_REFRESH_ALIVE_MS) return
  if (refreshBusy.value || listLoading.value) return
  autoInFlight = true
  autoIssuedAt = Date.now()
  void refreshLoaded('auto').finally(() => {
    autoInFlight = false
  })
}

/** 页面不可见 ⇒ 跳过；**变可见且已启用 ⇒ 立刻补刷一次**并把计时重置（桌面 :978-984 逐条移植）。 */
function onVisibilityChange(): void {
  if (document.hidden) {
    stopRefreshMotion()
    return
  }
  if (autoRefreshMs.value === AUTO_REFRESH_OFF) return
  restartAutoRefresh()
  onAutoRefreshTick()
}

/** 档位切换：合法化 → 记忆 → 按钮文案随 `autoRefreshMs` → 重开定时器（桌面 :955-965）。 */
function setAutoRefresh(value: number): void {
  const next = normalizeAutoRefreshMs(value)
  autoRefreshMs.value = next
  if (next === AUTO_REFRESH_OFF) stopRefreshMotion()
  writeStoredAutoRefreshMs(next)
  restartAutoRefresh()
}

function toggleAutoRefresh(event: Event): void {
  const input = event.target
  if (!(input instanceof HTMLInputElement) || input.checked === autoRefreshOn.value) return
  if (!input.checked) {
    setAutoRefresh(AUTO_REFRESH_OFF)
  } else {
    input.checked = false
    openFromMore('refresh')
  }
}

function onAutoRefreshSelect(value: number): void {
  setAutoRefresh(value)
  showRefreshChoices.value = false
}

onMounted(async () => {
  refreshScroll = hallPage.value?.closest<HTMLElement>('.m-shell__scroll') ?? null
  refreshScroll?.addEventListener('ptr:pullstart', stopRefreshMotion)
  let loaded: ConfigRow[] = []
  try {
    loaded = await fetchGames()
  } catch (e) {
    gamesState.value = 'error'
    gamesError.value = errText(e)
    return
  }
  if (unmounted) return
  games.value = loaded
  const firstGame = loaded[0]
  if (firstGame === undefined) {
    // 整页空态 + **零大厅请求**（任务书 T2：fetchGames 为空 → 空态、不发 hall）。
    gamesState.value = 'ready'
    return
  }
  gamesState.value = 'ready'
  await loadGame(firstGame.id)
  // 初值从 localStorage 读（构造时已读）；在这里才开定时器，首屏数据就绪后才有第一拍
  // （桌面 :986-989 同序：读完初值再 restartAutoRefresh）。
  if (!unmounted) restartAutoRefresh()
})

onBeforeUnmount(() => {
  unmounted = true
  queuedLoadMore = false
  stopRefreshMotion()
  refreshScroll?.removeEventListener('ptr:pullstart', stopRefreshMotion)
  refreshScroll = null
  closeClaimPreloader()
  reloadSeq += 1
  // 页面离开时同步关闭浮层，以便 Framework7 回收遮罩和打开状态。
  closeMore()
  refreshSheet?.close(false)
  filterSheet?.close(false)
  detailPopup?.close(false)
  quickClaimDialog?.close()
  // ⚠️ 清定时器 + 摘文档级监听 + 收掉防抖（桌面 :991-999；漏了就是「已卸载的页面继续发请求」）。
  if (autoTimer !== null) {
    window.clearInterval(autoTimer)
    autoTimer = null
  }
  if (keywordTimer !== null) {
    window.clearTimeout(keywordTimer)
    keywordTimer = null
  }
  document.removeEventListener('visibilitychange', onVisibilityChange)
  reducedMotion?.removeEventListener?.('change', stopRefreshMotion)
})

document.addEventListener('visibilitychange', onVisibilityChange)
reducedMotion?.addEventListener?.('change', stopRefreshMotion)

/** 失败**一律**原样显示错误文本（桌面 :566 的 `errText` 同口径）：业务码与 HTTP 403 的 msg
 * 都由 `api/http.ts` 的统一分支映射成 `BizError` / `HttpError` 的 `message`，本页不特判、不吞、不换文案。 */
function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

/** 模板里给「分流结果存在性」用的窄化（避免在模板里写 `!== null` 的长表达式）。
 *  ⛔ 灰卡（is_exited）在这里一并挡掉：不渲染抢单按钮（fail-closed，D-0296）。
 *  ⭐ peer 卡「已受理（等上家判决）」同样不画按钮（D-0303）：再点也不该发请求。 */
function hasClaimTarget(row: ClaimSourceRow & { is_exited?: boolean }): boolean {
  if (row.is_exited) return false
  if (isPeerClaimed(row)) return false
  return canClaim.value && claimTargetOf(row) !== null
}
</script>

<template>
  <section ref="hallPage" class="m-hall">
    <F7Block v-if="quickClaimHint" strong inset-ios class="m-hall__hint" role="status">{{ quickClaimHint }}</F7Block>
    <!-- 字典加载中 -->
    <F7Block v-if="gamesState === 'loading'" class="m-hall__loading" role="status"><F7Preloader :size="18" aria-hidden="true" /> 加载中…</F7Block>

    <!-- 字典失败：显示错误本身 + 退出（不是「未分配权限」那句——文案必须按实现写） -->
    <F7Block v-else-if="gamesState === 'error'" class="m-hall__empty" role="alert">
      <p class="m-hall__empty-text">{{ gamesError }}</p>
      <F7Button type="button" class="m-hall__exit" round outline small @click="onLogout">退出</F7Button>
    </F7Block>

    <!-- 没有可选游戏：整页空态 + 退出；这条路径**零大厅请求**（任务书 T2） -->
    <F7Block v-else-if="games.length === 0" class="m-hall__empty" role="status">
      <p class="m-hall__empty-text">当前账号没有可接的订单（未分配游戏权限）</p>
      <F7Button type="button" class="m-hall__exit" round outline small @click="onLogout">退出</F7Button>
    </F7Block>

    <template v-else>
      <div class="m-hall__toolbar">
        <MobileSearch v-model="keyword" placeholder="搜索订单" />
        <MobileSelect
          :model-value="selectedGame"
          :options="gameChoices"
          label="当前游戏"
          trigger-label="游戏"
          presentation="popover"
          data-testid="hall-game-filter"
          @update:model-value="onGameSelect"
        />
        <F7Button
          ref="moreTrigger"
          type="button"
          outline
          class="m-hall__more"
          :class="{ 'is-active': filterChips.length > 0 }"
          aria-label="更多操作"
          aria-haspopup="dialog"
          :aria-expanded="showHallMore"
          :aria-controls="moreMenuId"
          data-testid="hall-more-open"
          @click="openMore"
        >
          <F7Icon f7="ellipsis" :size="20" aria-hidden="true" />
        </F7Button>
      </div>
      <F7Block v-if="filterChips.length" class="m-hall__chips" aria-label="已应用的筛选">
        <F7Chip v-for="chip in filterChips" :key="chip" :text="chip" />
      </F7Block>

      <div v-if="autoRefreshOn" class="m-hall__auto-status">
        <span v-if="lastListRefresh">
          上次刷新 <time :datetime="lastListRefresh.toISOString()">{{ lastRefreshTime.format(lastListRefresh) }}</time>
        </span>
        <span v-else>自动刷新已开启</span>
        <span v-if="autoRefreshState === 'refreshing'" class="m-hall__auto-pending">
          <F7Preloader :size="12" aria-hidden="true" />
          正在刷新…
        </span>
        <span v-else-if="autoRefreshState === 'failed'" class="m-hall__auto-failure">刷新失败</span>
      </div>

      <F7Popover
        v-if="hallPage"
        :id="moreMenuId"
        ref="moreMenu"
        class="m-hall-more-menu"
        :container-el="hallPage ?? undefined"
        role="dialog"
        aria-modal="true"
        aria-label="更多操作"
        close-by-outside-click
        close-on-escape
        @keydown.tab="trapOverlayFocus"
        @keydown.esc.stop.prevent="closeMore"
        @popover:open="onMoreOpen"
        @popover:opened="focusMoreAction"
        @popover:closed="onMoreClosed"
      >
        <F7List class="m-hall-more-menu__choices">
          <F7ListItem>
            <F7Button ref="firstMoreAction" type="button" data-testid="hall-filter-open" @click="openFromMore('filter')">
              筛选
              <F7Badge v-if="filterChips.length">{{ filterChips.length }}</F7Badge>
            </F7Button>
          </F7ListItem>
          <F7ListItem title="自动刷新" class="m-hall-more-menu__switch">
            <template #after>
              <F7Toggle ref="autoRefreshToggle" class="m-hall-refresh-toggle" :checked="autoRefreshOn" @change="toggleAutoRefresh" />
            </template>
          </F7ListItem>
          <F7ListItem v-if="autoRefreshOn">
            <F7Button type="button" data-testid="hall-refresh-interval-open" @click="openFromMore('refresh')">
              刷新间隔
              <span class="m-hall-more-menu__status">{{ autoRefreshLabel }}</span>
            </F7Button>
          </F7ListItem>
        </F7List>
      </F7Popover>

      <F7Sheet
        v-model:opened="showRefreshChoices"
        class="m-hall-filter-sheet"
        :container-el="hallPage ?? undefined"
        :animate="false"
        backdrop
        close-by-backdrop-click
        close-on-escape
        @sheet:open="refreshSheet = $event"
        @sheet:opened="focusRefreshChoice"
        @sheet:closed="onRefreshClosed"
      >
        <div
          ref="refreshPanel"
          class="m-hall-refresh-panel"
          role="dialog"
          aria-modal="true"
          aria-label="刷新间隔"
          data-testid="hall-refresh-interval-panel"
          @keydown.tab="trapOverlayFocus"
        >
          <F7BlockTitle role="heading" aria-level="2">刷新间隔</F7BlockTitle>
          <F7List strong inset outline class="m-hall-refresh-panel__choices">
            <F7ListItem v-for="choice in AUTO_REFRESH_CHOICES" :key="choice.value">
              <F7Button
                type="button"
                :data-refresh-interval="choice.value"
                :aria-pressed="choice.value === autoRefreshMs"
                @click="onAutoRefreshSelect(choice.value)"
              >
                {{ choice.label }}
                <F7Icon v-if="choice.value === autoRefreshMs" f7="checkmark" :size="16" aria-hidden="true" />
              </F7Button>
            </F7ListItem>
          </F7List>
          <F7Button type="button" data-testid="hall-refresh-interval-cancel" @click="showRefreshChoices = false">取消</F7Button>
        </div>
      </F7Sheet>

      <F7Sheet
        v-model:opened="showHallFilter"
        class="m-hall-filter-sheet"
        :container-el="hallPage ?? undefined"
        swipe-to-close
        backdrop
        close-by-backdrop-click
        close-on-escape
        @sheet:open="onFilterSheetOpen"
        @sheet:opened="hallPage?.querySelector<HTMLElement>('[aria-label=关闭筛选]')?.focus()"
        @sheet:closed="focusMoreTrigger"
      >
        <form
          class="m-hall-filter-form"
          role="dialog"
          aria-modal="true"
          aria-labelledby="hall-filter-title"
          data-testid="hall-filter-form"
          @keydown.tab="trapOverlayFocus"
          @submit.prevent="saveHallFilter"
        >
          <header>
            <div>
              <h2 id="hall-filter-title">订单筛选</h2>
              <p>{{ gamesMap.get(selectedGame ?? 0) }}</p>
            </div>
            <F7Button type="button" aria-label="关闭筛选" @click="showHallFilter = false">关闭</F7Button>
          </header>
          <F7List strong inset outline class="m-hall-filter-form__fields">
            <F7ListInput v-model:value="filterDraft.min" input-id="hall-filter-price-min" name="price-min" inputmode="decimal" placeholder="不限">
              <template #label><label for="hall-filter-price-min">最低价（元）</label></template>
            </F7ListInput>
            <F7ListInput v-model:value="filterDraft.max" input-id="hall-filter-price-max" name="price-max" inputmode="decimal" placeholder="不限">
              <template #label><label for="hall-filter-price-max">最高价（元）</label></template>
            </F7ListInput>
            <F7ListInput v-model:value="filterDraft.include" input-id="hall-filter-keyword-include" name="keyword-include" placeholder="用逗号或空格隔开">
              <template #label><label for="hall-filter-keyword-include">包含关键词</label></template>
            </F7ListInput>
            <F7ListInput v-model:value="filterDraft.exclude" input-id="hall-filter-keyword-exclude" name="keyword-exclude" placeholder="用逗号或空格隔开">
              <template #label><label for="hall-filter-keyword-exclude">排除关键词</label></template>
            </F7ListInput>
          </F7List>
          <p v-if="filterError" class="m-hall-filter-form__error" role="alert">{{ filterError }}</p>
          <footer>
            <F7Button type="button" round outline @click="clearHallFilter">清除</F7Button>
            <F7Button type="submit" round fill>保存</F7Button>
          </footer>
        </form>
      </F7Sheet>

      <F7Block v-if="claimHint !== ''" strong inset-ios class="m-hall__hint" data-testid="claim-hint" role="status">{{ claimHint }}</F7Block>

      <MobilePullToRefresh :refreshing="refreshing" @refresh="onPullRefresh">
        <F7Block v-if="loadingFirstPage && !refreshing" class="m-hall__inline-loading" data-testid="hall-orders-loading" role="status">
          <F7Preloader :size="16" aria-hidden="true" />
          正在加载订单…
        </F7Block>
        <MobilePagedList ref="refreshList" class="m-hall__refresh-list" :async-loading="asyncLoading" :aria-busy="loadingFirstPage" @load-more="onLoadMore">
          <F7Card
            v-for="row in visibleRows"
            :key="hallCardKey(row)"
            outline-ios
            class="m-hall-card"
            :class="{
              'm-hall-card--exited': row.is_exited,
              'm-hall-card--peer-claimed': isPeerClaimed(row),
            }"
            :data-row-key="hallCardKey(row)"
            @click="openDetail(row, $event)"
          >
            <HallOrderHeading
              class="m-hall-card__head"
              :row="row"
              :game="gameNameOf(row)"
              :claimed="isPeerClaimed(row)"
              :meta="cardMetaOf(row)"
            >
              <template v-if="hasClaimTarget(row)" #action>
                <F7Button
                  type="button"
                  class="m-hall-card__claim" tonal round
                  :disabled.prop="claiming || isPeerClaimPending(row)"
                  @click.stop="onClaimClick(row)"
                >抢单</F7Button>
              </template>
            </HallOrderHeading>
            <button
              type="button"
              class="m-hall-card__project"
              @click.stop="openDetail(row, $event)"
            >
              <span class="m-hall-card__excerpt">
                <template v-if="row.project_text.trim()">{{ row.project_text }}<span v-if="hasDistinctRemark(row)" class="m-hall-card__remark"> · {{ row.remark }}</span></template>
                <template v-else>{{ row.remark.trim() || '查看订单' }}</template>
              </span>
            </button>
          </F7Card>
          <div v-if="refreshSweep" class="m-hall__refresh-sweep" :style="refreshSweep" data-testid="hall-refresh-sweep" aria-hidden="true">
            <div @animationend="refreshSweep = null"></div>
          </div>
        </MobilePagedList>

        <F7Block v-if="visibleRows.length > 0 && finished && !loadingFirstPage" class="m-hall__end">没有更多了</F7Block>
        <F7Block v-if="visibleRows.length === 0 && !loadingFirstPage && !refreshing && !listError && !hasMore" class="m-hall__no-results" role="status">
          <p>{{ filterChips.length || keywordQuery ? '没有符合条件的订单' : '暂无待抢订单' }}</p>
          <F7Button v-if="filterChips.length" type="button" @click="clearHallFilter">清除筛选</F7Button>
        </F7Block>
      </MobilePullToRefresh>

      <F7Block v-if="listError !== ''" class="m-hall__retry" role="alert">
        <p class="m-hall__retry-text">{{ listError }}</p>
        <F7Button type="button" class="m-hall__retry-btn" round outline small @click="onRetryClick">
          重试
        </F7Button>
      </F7Block>
    </template>

    <!-- 大厅单**全量详情浮层**（业务方裁定）：字段全部来自大厅列表已返回的载荷，⛔ 不跳详情页（未接单的详情接口必然 403）。遮罩点关闭，面板内点击不冒泡。
         ⭐ 唯一例外（D-0296）：原生在厅单的「参考图」段会现取 screenshots 接口（短时效签名，见 `loadDetailShots`）；其余字段仍然零请求。 -->
    <F7Popup
      v-if="hallPage"
      class="m-hall-detail"
      :opened="detailRow !== null"
      :container-el="hallPage"
      :animate="false"
      tablet-fullscreen
      backdrop
      close-by-backdrop-click
      @popup:open="detailPopup = $event"
      @popup:opened="focusDetailClose"
      @popup:closed="onDetailPopupClosed"
    >
      <section
        v-if="detailRow !== null"
        class="m-hall-detail__panel"
        role="dialog"
        aria-modal="true"
        aria-label="订单详情"
        data-testid="hall-detail-panel"
        @keydown.tab="trapOverlayFocus"
        @keydown.esc.stop.prevent="closeDetail"
      >
        <HallOrderHeading
          :row="detailRow"
          :game="gameNameOf(detailRow)"
          :claimed="isPeerClaimed(detailRow)"
        >
          <template #action>
            <F7Button
              class="m-hall-detail__close"
              type="button"
              round
              tonal
              aria-label="关闭订单详情"
              data-testid="hall-detail-close"
              @click="closeDetail"
            >
              <F7Icon f7="xmark" :size="16" aria-hidden="true" />
            </F7Button>
          </template>
        </HallOrderHeading>

        <div class="m-hall-detail__body" role="region" aria-label="订单内容" tabindex="0">
          <p v-if="detailRow.project_text.trim()" class="m-hall-detail__text">{{ detailRow.project_text }}</p>
          <div v-if="detailRow.remark !== ''" class="m-hall-detail__section">
            <p class="m-hall-detail__label">备注</p>
            <p class="m-hall-detail__text">{{ detailRow.remark }}</p>
          </div>

          <dl class="m-hall-detail__metadata">
            <div v-if="orderNoOf(detailRow).text">
              <dt>{{ orderNoOf(detailRow).label }}</dt>
              <dd>
                <F7Button
                  type="button"
                  class="m-hall-detail__copy"
                  aria-label="复制订单号"
                  data-testid="hall-detail-order-no"
                  @click="onCopyOrderNo(detailRow)"
                >{{ orderNoOf(detailRow).text }}</F7Button>
              </dd>
            </div>
            <div>
              <dt>游戏</dt>
              <dd>{{ gameNameOf(detailRow) }}</dd>
            </div>
            <div>
              <dt>区服</dt>
              <dd>{{ detailServerText(detailRow) }}</dd>
            </div>
            <div v-if="detailRow.time_requirement !== ''">
              <dt>时间要求</dt>
              <dd>{{ detailRow.time_requirement }}</dd>
            </div>
            <div v-if="detailRow.origin === 'peer' && peerTimeOf(detailRow) !== ''" data-testid="hall-detail-peer-time">
              <dt>进厅时间</dt>
              <dd>{{ peerTimeOf(detailRow) }}</dd>
            </div>
            <div v-if="detailRow.remote_hours_desc !== ''">
              <dt>上家时长</dt>
              <dd>{{ detailRow.remote_hours_desc }}</dd>
            </div>
          </dl>

          <div
            v-if="canViewShots && (detailShotsLoading || detailShots.length > 0)"
            class="m-hall-detail__section"
            data-testid="hall-detail-shots"
          >
            <p class="m-hall-detail__label">参考图</p>
            <div v-if="detailShotsLoading" class="m-hall-detail__shots-loading" data-testid="hall-shots-loading" role="status">
              <F7Preloader :size="16" aria-hidden="true" />
              正在加载参考图…
            </div>
            <div v-else class="m-hall-detail__shots">
              <F7Button
                v-for="(shot, i) in detailShots"
                :key="shot.id"
                type="button"
                class="m-hall-detail__shot"
                :aria-label="`查看第 ${i + 1} 张订单参考图`"
                @click="openShotPreview(i)"
              >
                <img :src="shot.thumbnail_url || shot.url" loading="lazy" alt="" />
              </F7Button>
            </div>
          </div>
        </div>
        <footer v-if="hasClaimTarget(detailRow)" class="m-hall-detail__foot">
          <F7Button
            type="button"
            round
            fill
            class="m-hall-detail__claim"
            :disabled.prop="claiming || isPeerClaimPending(detailRow)"
            data-testid="hall-detail-claim"
            @click="onDetailClaim"
          >抢单</F7Button>
        </footer>
      </section>
    </F7Popup>

    <!-- 参考图大图预览（照 OrderScreenshotsPage 的受控图片预览：点哪张从哪张看起）。 -->
    <MobilePhotoBrowser
      v-model:visible="viewerVisible"
      :images="viewerImages"
      :index="viewerIndex"
      :show-index="true"
      data-testid="hall-shot-viewer"
      @index-change="(i: number) => (viewerIndex = i)"
    />
  </section>
</template>

<style scoped>
.m-hall {
  --m-hall-card-radius: 16px;
}

.m-hall__toolbar {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 55px 44px;
  gap: 8px;
  margin-bottom: 14px;
}

.m-hall__toolbar :deep(.m-searchbar.searchbar) {
  --f7-searchbar-height: 44px;
  --f7-searchbar-input-height: 44px;
  --f7-searchbar-input-font-size: 17px;
}

.m-hall__toolbar :deep(.m-select .m-select__trigger) {
  --f7-button-height: 44px;
}

.m-hall .m-hall__more {
  --f7-button-height: 44px;
  --f7-button-border-radius: 12px;
  --f7-button-border-width: 1px;
  --f7-button-border-color: var(--m-border);
  --f7-button-text-color: var(--f7-text-color);
  padding-inline: 0;
}

.m-hall .m-hall__more.is-active {
  --f7-button-border-color: var(--m-accent-surface);
  --f7-button-text-color: var(--f7-theme-color);
  --f7-button-bg-color: var(--m-accent-surface);
}

.m-hall__chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: -4px 0 14px;
  padding-inline: 0;
}

.m-hall__chips .chip {
  --f7-chip-bg-color: var(--m-accent-surface);
  --f7-chip-text-color: var(--f7-theme-color);
  max-width: 100%;
}

.m-hall__chips :deep(.chip-label) {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.m-hall__auto-status {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 18px;
  margin: -6px 1px 8px;
  color: var(--m-muted);
  font-size: 12px;
  line-height: 18px;
  font-variant-numeric: tabular-nums;
}

.m-hall__auto-pending {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  /* Suppress a flash when the response arrives before the spinner is useful. */
  animation: m-hall-show-refresh 0s 120ms both;
}

@keyframes m-hall-show-refresh {
  from { opacity: 0; }
  to { opacity: 1; }
}

.m-hall__auto-failure {
  color: #ad352f;
}

.m-hall__refresh-list {
  position: relative;
  isolation: isolate;
}

.m-hall__refresh-sweep {
  position: absolute;
  z-index: 1;
  inset-inline: 0;
  overflow: hidden;
  overflow-anchor: none;
  pointer-events: none;
}

.m-hall__refresh-sweep > div {
  height: 68%;
  background: linear-gradient(to bottom, transparent 0%,
    color-mix(in srgb, var(--f7-theme-color) 13%, transparent) 42%,
    color-mix(in srgb, var(--f7-theme-color) 34%, transparent) 92%,
    color-mix(in srgb, var(--f7-theme-color) 64%, transparent) 98%, transparent 100%);
  animation: m-hall-refresh-sweep 480ms cubic-bezier(.32, .15, .3, .9) both;
}

@keyframes m-hall-refresh-sweep {
  from { transform: translateY(-100%); }
  to { transform: translateY(160%); }
}

@media (prefers-reduced-motion: reduce) {
  .m-hall__refresh-sweep { display: none; }
}

.m-hall-card {
  --f7-card-margin-vertical: 0px;
  --f7-card-outline-border-color: var(--m-border);
  --f7-card-border-radius: var(--m-hall-card-radius);
  box-sizing: border-box;
  container-type: inline-size;
  height: 144px;
  display: grid;
  grid-template-rows: 44px 76px;
  gap: 6px;
  padding: 8px 11px;
  margin-block-end: 8px;
  cursor: pointer;
}

.m-hall-card:is(.m-hall-card--exited, .m-hall-card--peer-claimed) {
  background: #f8f7fa;
}

.m-hall-card__project {
  display: flex;
  align-items: flex-start;
  min-width: 0;
  padding: 0;
  border: 0;
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: start;
  cursor: pointer;
}

.m-hall-card__project:focus-visible {
  outline: 2px solid var(--f7-theme-color);
  outline-offset: 2px;
  border-radius: 3px;
}

.m-hall-card__excerpt {
  display: -webkit-box;
  width: 100%;
  overflow: hidden;
  font-size: 14px;
  line-height: 19px;
  white-space: pre-line;
  overflow-wrap: anywhere;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 4;
}

.m-hall-card__remark {
  color: var(--m-muted);
}

.m-hall-card__claim {
  width: auto;
  min-width: 44px;
  min-height: 44px;
  padding-inline: 14px;
  --f7-button-border-radius: 13px;
}

@media (max-width: 360px) {
  .m-hall-card__claim { padding-inline: 8px; }
}

.m-hall-card__claim:disabled {
  opacity: .55;
}

.m-hall__no-results {
  margin-block: 0;
  padding: 42px 12px;
  text-align: center;
  color: var(--m-muted);
  font-size: 14px;
}

.m-hall__no-results .button {
  width: auto;
  margin-inline: auto;
}

.m-hall-filter-sheet.sheet-modal {
  --f7-sheet-border-radius: 26px;
  --f7-sheet-bg-color: #fff;
  width: min(100%, 540px);
  height: auto;
  max-height: 90dvh;
  left: 50%;
  margin-left: max(-50vw, -270px);
  overflow-y: auto;
}

.m-hall-filter-form {
  display: grid;
  gap: 13px;
  box-sizing: border-box;
  width: min(100%, 528px);
  max-height: 90dvh;
  margin-inline: auto;
  padding: 22px 16px max(18px, env(safe-area-inset-bottom));
  overflow-y: auto;
  color: #24212b;
}

.m-hall-filter-form header,
.m-hall-filter-form footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.m-hall-filter-form h2 {
  margin: 0;
  font-size: 20px;
  line-height: 1.3;
}

.m-hall-filter-form header p {
  margin: 4px 0 0;
  color: #686675;
  font-size: 13px;
}

.m-hall-filter-form__fields {
  --f7-list-margin-vertical: 0px;
  --f7-input-font-size: 16px;
  margin-inline: 0;
}

.m-hall-filter-form header button {
  width: auto;
  min-height: 44px;
}

.m-hall-filter-form footer button {
  flex: 1;
  height: 43px;
  font-size: 14px;
  font-weight: 600;
}

.m-hall-filter-form__error {
  margin: 0;
  color: #ad352f;
  font-size: 13px;
}

.m-hall-more-menu.popover {
  --f7-popover-width: min(238px, calc(100vw - 20px));
  position: fixed;
}

.m-hall-refresh-panel {
  padding: 18px 16px max(14px, env(safe-area-inset-bottom));
}

.m-hall-refresh-panel :deep(.block-title) {
  margin: 0 0 10px;
}

.m-hall-more-menu__choices,
.m-hall-refresh-panel__choices {
  --f7-list-margin-vertical: 0;
  --f7-list-inset-side-margin: 0;
  --f7-list-item-padding-horizontal: 0px;
  --f7-list-item-content-padding: 0px;
}

.m-hall-more-menu__choices .button,
.m-hall-refresh-panel__choices .button {
  --f7-button-height: 44px;
  --f7-button-text-color: var(--f7-text-color);
  justify-content: space-between;
  padding-inline: 14px;
}

.m-hall-more-menu__switch {
  --f7-list-item-padding-horizontal: 14px;
  --f7-list-item-min-height: 44px;
}

.m-hall-refresh-toggle :deep(input) {
  position: absolute;
  display: block;
  inset: -8px 0;
  width: 100%;
  height: 44px;
  margin: 0;
  opacity: 0;
  cursor: pointer;
}

.m-hall-refresh-toggle:has(:focus-visible) {
  outline: 2px solid var(--f7-theme-color);
  outline-offset: 2px;
}

.m-hall-refresh-panel__choices .button[aria-pressed="true"] {
  --f7-button-text-color: var(--f7-theme-color);
}

.m-hall-more-menu__status {
  color: var(--m-muted);
}

.m-hall__loading,
.m-hall__empty {
  padding: 48px 16px;
  text-align: center;
}

.m-hall__loading,
.m-hall__empty-text {
  margin: 0 0 12px;
  font-size: 14px;
  color: #6b7280;
}

.m-hall__inline-loading,
.m-hall-detail__shots-loading {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 28px;
  color: var(--m-muted);
  font-size: 13px;
}

.m-hall__inline-loading {
  justify-content: center;
  margin-block: 0 8px;
}

.m-hall__hint {
  margin: 0 0 8px;
  padding: 6px 10px;
  font-size: 13px;
  color: #2ba471;
  background: #e3f9e9;
  border-radius: 6px;
}

.m-hall-detail.popup {
  /* Leave the photo-browser popup above the order detail. */
  z-index: 10999;
  --m-hall-detail-max-height: min(620px, calc(100dvh - 32px));
  position: fixed;
  inset: 0;
  width: min(calc(100% - 32px), 480px);
  height: fit-content;
  max-height: var(--m-hall-detail-max-height);
  margin: auto;
  border: 1px solid var(--m-border);
  border-radius: var(--m-hall-card-radius);
}

.m-hall-detail__panel {
  box-sizing: border-box;
  container-type: inline-size;
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-height: calc(var(--m-hall-detail-max-height) - 2px);
  padding: 12px 14px;
}

.m-hall-detail__panel > header {
  flex: none;
}

.m-hall-detail__close.button {
  width: 44px;
  height: 44px;
  padding-inline: 0;
}

.m-hall-detail__body {
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.m-hall-detail__body:focus-visible {
  outline: 2px solid var(--f7-theme-color);
  outline-offset: -2px;
  border-radius: 4px;
}

.m-hall-detail__text {
  margin: 0;
  font-size: 14px;
  line-height: 19px;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.m-hall-detail__section {
  margin-top: 12px;
}

.m-hall-detail__label {
  margin: 0 0 4px;
  color: var(--m-muted);
  font-size: 12px;
}

.m-hall-detail__metadata {
  display: grid;
  gap: 4px;
  margin: 12px 0 0;
  padding-top: 8px;
  border-top: 1px solid var(--m-border);
  font-size: 13px;
}

.m-hall-detail__metadata > div {
  display: grid;
  grid-template-columns: 4em minmax(0, 1fr);
  align-items: baseline;
  gap: 8px;
}

.m-hall-detail__metadata dt {
  color: var(--m-muted);
}

.m-hall-detail__metadata dd {
  margin: 0;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.m-hall-detail__copy.button {
  width: fit-content;
  max-width: 100%;
  height: auto;
  min-height: 28px;
  padding-inline: 0;
  text-align: start;
  white-space: normal;
}

/* 参考图缩略图（D-0296）：横排小图，点开 ImageViewer 看大图。 */
.m-hall-detail__shots {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  white-space: normal;
}

.m-hall-detail__shot.button {
  flex: none;
  width: 64px;
  height: 64px;
  padding: 0;
  border-radius: 6px;
  border: 1px solid #e7e7e7;
  background: #f3f3f3;
  cursor: zoom-in;
}

.m-hall-detail__shot img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.m-hall-detail__foot {
  flex: none;
  padding-top: 10px;
  border-top: 1px solid var(--m-border);
}

.m-hall-detail__claim.button {
  width: 100%;
  min-height: 44px;
}

.m-hall__end {
  margin: 4px 0 0;
  font-size: 12px;
  color: #9ca3af;
  text-align: center;
}

.m-hall__retry {
  margin-top: 8px;
  text-align: center;
}

.m-hall__retry-text {
  margin: 0 0 6px;
  font-size: 13px;
  color: #d54941;
}
</style>
