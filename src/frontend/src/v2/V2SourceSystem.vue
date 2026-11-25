<script setup lang="ts">
import { computed, onMounted, ref, useId } from 'vue'
import { fetchAllGames, type ConfigRow } from '@/api/config'
import ElDialog from '@/v2/OrderTabDialog.vue'
import { request } from '@/api/http'
import {
  createPeerBinding,
  deletePeerBinding,
  getPeerBinding,
  updatePeerBindingHallSettings,
  type PeerBindingRow,
} from '@/api/peerBindings'
import {
  createUpstreamBindingRow,
  deleteUpstreamBinding,
  getUpstreamBindingRow,
  updateUpstreamBindingRow,
} from '@/api/jidanpuUpstream'
import { fetchOrderSources, type OrderSourceRow } from '@/api/orderSources'
import { UPSTREAM_BINDINGS_URL, type UpstreamBindingRow, type UpstreamGameRules } from '@/api/upstreamBindings'

/* ── 逐字文案与常量（出处 = island 页同语义常量；本地重写不引模块）────────────── */

const SYSTEM_TYPE_JIDANPU = 'jidanpu'
const SYSTEM_TYPE_TUANTUANTONG = 'tuantuantong'
/** 下拉两项与列表「系统类型」列共用的中文（旧页 SYSTEM_TYPE_LABEL_* 逐字；机器值不上屏）。 */
const SYSTEM_TYPE_LABELS: ReadonlyArray<{ value: string; label: string }> = [
  { value: SYSTEM_TYPE_JIDANPU, label: '鸡蛋铺系统' },
  { value: SYSTEM_TYPE_TUANTUANTONG, label: '团团通系统' },
]
function systemTypeLabel(type: string): string {
  return SYSTEM_TYPE_LABELS.find((o) => o.value === type)?.label ?? '—'
}

/** 域名链接的 title（旧页 HOME_HOST_LINK_TITLE 逐字）。 */
const HOST_LINK_TITLE = '点击可以访问这个系统'
/** 四条校验文案（旧页 VALIDATION_MESSAGES 逐字）。 */
const VALIDATION_MESSAGES = {
  name: '请输入这个上家名称（可自定义名称）',
  url: '请输入这个上家的访问网址，例如http://d.jidanpu.com',
  acc: '请输入上家给你开的子账号',
  pwd: '请输入上家给你开的子账号登录密码，不然没法验证',
} as const
/** 大厅价格百分比的范围与非法提示（旧页 PEER_HALL_PERCENT_* 逐字）。 */
const PEER_HALL_PERCENT_MIN = 1
const PEER_HALL_PERCENT_MAX = 100
const PEER_HALL_PERCENT_DEFAULT = 100
const PEER_HALL_PERCENT_INVALID_TEXT = '大厅价格百分比需要是 1 到 100 之间的整数'
/** 大厅价格百分比下面那句人话（旧页 PEER_HALL_PERCENT_HINT 逐字）。 */
const PEER_HALL_PERCENT_HINT = '打手看到的价格 = 上家大厅价 × 这个百分比'
/** 删除确认两分支（旧页 UNBIND_CONFIRM_TEXT / JIDANPU_UNBIND_CONFIRM_TEXT 逐字）。 */
const PEER_UNBIND_CONFIRM_TEXT =
  '确定要解除与这个上家的绑定吗？注意：解绑后，上家指派过来的新订单无法同步，但在本系统解绑之前同步过来的订单不受影响'
const JIDANPU_UNBIND_CONFIRM_TEXT =
  '确定删除这条上家鸡蛋铺绑定吗？删除后，它名下还在跟踪的同步链路会一并删除（订单与上家单的对应关系不再保留），同步台账保留。'
/** 成功提示（后端成功信封 msg 为空串 ⇒ 前端文案，旧页同一处置）。 */
const BIND_OK_TEXT = '绑定成功'
const PEER_SAVE_OK_TEXT = '已保存'
const UNBIND_OK_TEXT = '已解绑'
/** 弹窗标题：新增照证据件原文；编辑两支照旧页 PEER_EDIT_DIALOG_TITLE / EDIT_DIALOG_TITLE。 */
const CREATE_DIALOG_TITLE = '绑定一个新的上家系统'
const PEER_EDIT_DIALOG_TITLE = '编辑上家团团通绑定'
const JIDANPU_EDIT_DIALOG_TITLE = '编辑上家鸡蛋铺绑定'
/** 「订单来源」下拉里「未指定」项（旧页 ORDER_SOURCE_NONE_* 逐字）。 */
const ORDER_SOURCE_NONE_VALUE = ''
const ORDER_SOURCE_NONE_LABEL = '不指定'
/** 空态照旧页 text.none。 */
const EMPTY_TEXT = '暂无数据'

function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

/** ISO → `Y-m-d H:i`（旧页 formatDateTime 同式重写；取不到回空串，列侧再兜 `—`）。 */
function formatDateTime(value: unknown): string {
  if (typeof value !== 'string') return ''
  const m = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/.exec(value)
  return m ? `${m[1]} ${m[2]}` : ''
}

/* ── 提示（页面内自绘 toast，同 V2Notices；info 1500ms / error 6000ms）───────── */

const toast = ref<{ text: string; kind: 'info' | 'error' } | null>(null)
let toastTimer: ReturnType<typeof setTimeout> | null = null

function showToast(text: string, kind: 'info' | 'error' = 'info'): void {
  if (toastTimer !== null) clearTimeout(toastTimer)
  toast.value = { text, kind }
  toastTimer = setTimeout(() => {
    toast.value = null
    toastTimer = null
  }, kind === 'error' ? 6000 : 1500)
}

/* ── 统一列表（7 列；接口不分页一次取全；R20 失败红字 + A19 最近同步灰字照搬）────── */

/** 接口行 → 表格行：机器值换中文、缺名称/缺时间兜 `—`（旧页 toBindingRow 同口径）。 */
type BindingListRow = UpstreamBindingRow & { SystemType: string; HomeCompany: string; BindTimeStr: string }

const rows = ref<BindingListRow[]>([])
const loading = ref(false)

function toListRow(row: UpstreamBindingRow): BindingListRow {
  return {
    ...row,
    SystemType: systemTypeLabel(row.system_type),
    HomeCompany: row.system_name || '—',
    BindTimeStr: row.bound_at ? formatDateTime(row.bound_at) : '—',
  }
}

async function reload(): Promise<void> {
  loading.value = true
  try {
    const page = await request<{ items: UpstreamBindingRow[]; total: number }>({
      method: 'get',
      url: UPSTREAM_BINDINGS_URL,
    })
    rows.value = page.items.map(toListRow)
  } catch (e) {
    rows.value = []
    showToast(errText(e), 'error')
  } finally {
    loading.value = false
  }
}

/** 「绑定时间」表头排序（旧页 `sort:true` 的三态：升 → 降 → 不排；按展示串排，照 layui 语义）。 */
const sortDir = ref<'asc' | 'desc' | null>(null)

function toggleSort(): void {
  sortDir.value = sortDir.value === 'asc' ? 'desc' : sortDir.value === 'desc' ? null : 'asc'
}

const sortedRows = computed(() => {
  if (sortDir.value === null) return rows.value
  const dir = sortDir.value === 'asc' ? 1 : -1
  return [...rows.value].sort((a, b) => (a.BindTimeStr < b.BindTimeStr ? -dir : dir))
})

/* ── 失败红字（R20：只看 consecutive_failures>0；鸡蛋铺行恒 0 天然不计入）────────── */

const failedRows = computed(() => rows.value.filter((row) => row.consecutive_failures > 0))

const failureText = computed(() => {
  const failed = failedRows.value
  if (failed.length === 0) return ''
  const latest = failed.reduce((a, b) => ((b.last_error_at ?? '') > (a.last_error_at ?? '') ? b : a))
  const when = latest.last_error_at ? `，最近一次 ${formatDateTime(latest.last_error_at)}` : ''
  const reason = latest.last_error || '（对端没有回原因）'
  return `注意：${failed.length} 个上家系统的订单同步最近失败了（已连续失败 ${latest.consecutive_failures} 次${when}）：${reason}`
})

/* ── 订单来源字典（D-0261：下拉数据源 = 既有字典 API；fail-soft 取不到只剩「不指定」）── */

const dropdownTarget = ref<HTMLElement>()
const orderSources = ref<OrderSourceRow[]>([])
/** 编辑回填时库里那项不在字典里 ⇒ 补一个 option（防静默变「不指定」；旧页 ensureOrderSourceOption）。 */
const orderSourceFallback = ref<{ id: number; name: string } | null>(null)

const sourceOptions = computed<ReadonlyArray<{ id: number; name: string }>>(() => {
  const list: Array<{ id: number; name: string }> = orderSources.value.map((s) => ({ id: s.id, name: s.name }))
  const fb = orderSourceFallback.value
  if (fb !== null && !list.some((s) => s.id === fb.id)) list.push({ id: fb.id, name: fb.name })
  return list
})

async function loadOrderSources(): Promise<void> {
  try {
    orderSources.value = (await fetchOrderSources()).items
  } catch {
    orderSources.value = []
  }
}

/* ── 编辑弹窗（双分支；编辑态五条行为照旧页）────────────────────────────────────── */

type BindForm = {
  editingId: number | null
  systemType: string
  name: string
  baseUrl: string
  account: string
  password: string
  hallPricePercent: number | null
  enabled: boolean
  orderSourceId: string
  peerHallSync: boolean
  peerHallPercent: number | null
  originalPeerHall?: ReturnType<typeof peerHallSettingsOf>
  peerDirectEnabled?: boolean
  originalPeerDirectEnabled?: boolean
  restrictGames: boolean
  gameRules: GameRuleDraft[]
  originalGameRules?: UpstreamGameRules
}

type GameRuleDraft = { gameId: number; percent: string }

const bindDlg = ref<BindForm | null>(null)
const gameScopeName = useId()
const saving = ref(false)
/** 编辑回填的 GET 在途（防重复点击行内「编辑」叠弹窗）。 */
const detailLoading = ref(false)

const isPeerBranch = computed(() => bindDlg.value?.systemType === SYSTEM_TYPE_TUANTUANTONG)
const isPeerEdit = computed(() => isPeerBranch.value && bindDlg.value?.editingId !== null)
const showGameRules = computed(() => !isPeerBranch.value || isPeerEdit.value)
const gameOptions = ref<ConfigRow[]>([])
const gamesLoading = ref(false)
const gamesLoadFailed = ref(false)
let gamesLoaded = false

function gameRuleForm(rules?: UpstreamGameRules): Pick<BindForm, 'restrictGames' | 'gameRules' | 'originalGameRules'> {
  return {
    restrictGames: rules != null,
    gameRules: (rules ?? []).map((rule) => ({
      gameId: rule.game_id, percent: rule.hall_price_percent === null ? '' : String(rule.hall_price_percent),
    })),
    originalGameRules: rules,
  }
}

async function loadGameOptions(): Promise<void> {
  if (gamesLoaded || gamesLoading.value) return
  gamesLoading.value = true
  gamesLoadFailed.value = false
  try {
    gameOptions.value = await fetchAllGames()
    gamesLoaded = true
  } catch {
    gamesLoadFailed.value = true
  } finally {
    gamesLoading.value = false
  }
}

const selectedGameIds = computed(() => bindDlg.value?.gameRules.map((rule) => rule.gameId) ?? [])

function setRestrictedGames(restrict: boolean): void {
  if (bindDlg.value === null) return
  bindDlg.value.restrictGames = restrict
  if (restrict) void loadGameOptions()
}

function setSelectedGameIds(ids: number[]): void {
  const dlg = bindDlg.value
  if (dlg === null) return
  const current = new Map(dlg.gameRules.map((rule) => [rule.gameId, rule]))
  dlg.gameRules = ids.map((id) => current.get(id) ?? { gameId: id, percent: '' })
}

function gameName(id: number): string {
  return gameOptions.value.find((game) => game.id === id)?.name ?? `游戏 #${id}`
}

function validHallPercent(percent: number): boolean {
  return Number.isInteger(percent) && percent >= PEER_HALL_PERCENT_MIN && percent <= PEER_HALL_PERCENT_MAX
}

function bindDlgTitle(): string {
  const dlg = bindDlg.value
  if (dlg === null) return ''
  if (dlg.editingId === null) return CREATE_DIALOG_TITLE
  return dlg.systemType === SYSTEM_TYPE_TUANTUANTONG ? PEER_EDIT_DIALOG_TITLE : JIDANPU_EDIT_DIALOG_TITLE
}

/** 老数据两格按默认值兜底（旧页 peerHallSettingsOf：严格 true / 整数 1–100，其余回关/100）。 */
function peerHallSettingsOf(detail: { hall_sync_enabled?: boolean; hall_price_percent?: number | null } | null): {
  hall_sync_enabled: boolean
  hall_price_percent: number
} {
  const raw = detail?.hall_price_percent
  const percent =
    typeof raw === 'number' && validHallPercent(raw)
      ? raw
      : PEER_HALL_PERCENT_DEFAULT
  return { hall_sync_enabled: detail?.hall_sync_enabled === true, hall_price_percent: percent }
}

function freshForm(): BindForm {
  return {
    editingId: null,
    systemType: SYSTEM_TYPE_JIDANPU,
    name: '',
    baseUrl: 'http://',
    account: '',
    password: '',
    hallPricePercent: PEER_HALL_PERCENT_DEFAULT,
    enabled: false,
    orderSourceId: ORDER_SOURCE_NONE_VALUE,
    peerHallSync: false,
    peerHallPercent: PEER_HALL_PERCENT_DEFAULT,
    ...gameRuleForm(),
  }
}

/** 团团通新增先完成绑定，游戏规则随后在编辑中设置。 */
function openCreateDlg(): void {
  orderSourceFallback.value = null
  bindDlg.value = freshForm()
}

/** 行内「编辑」：先 GET 详情回填（编辑态④：列表行没有 enabled/order_source 现值，不读就提交会把它们静默打回默认值）。 */
async function openEditDlg(row: UpstreamBindingRow): Promise<void> {
  // 分流判据 fail-closed：契约外的类型不开弹窗（照旧页 tool 事件分支）。
  if (row.system_type !== SYSTEM_TYPE_JIDANPU && row.system_type !== SYSTEM_TYPE_TUANTUANTONG) return
  if (detailLoading.value) return
  detailLoading.value = true
  try {
    // 团团通走 peer-bindings 详情，鸡蛋铺走行级 GET（旧页 openEditForm 同一分流）。
    if (row.system_type === SYSTEM_TYPE_TUANTUANTONG) {
      const detail: PeerBindingRow = await getPeerBinding(row.id)
      const hall = peerHallSettingsOf(detail)
      bindDlg.value = {
        editingId: row.id,
        systemType: SYSTEM_TYPE_TUANTUANTONG,
        name: detail.peer_name,
        baseUrl: detail.peer_base_url,
        account: detail.peer_account,
        password: '',
        hallPricePercent: PEER_HALL_PERCENT_DEFAULT,
        enabled: false,
        orderSourceId: ORDER_SOURCE_NONE_VALUE,
        peerHallSync: hall.hall_sync_enabled,
        peerHallPercent: hall.hall_price_percent,
        originalPeerHall: hall,
        peerDirectEnabled: detail.direct_taker_assignment_enabled,
        originalPeerDirectEnabled: detail.direct_taker_assignment_enabled,
        ...gameRuleForm(detail.game_rules),
      }
    } else {
      const detail = await getUpstreamBindingRow(row.id)
      orderSourceFallback.value = detail.order_source === null ? null : detail.order_source
      bindDlg.value = {
        editingId: row.id,
        systemType: SYSTEM_TYPE_JIDANPU,
        name: detail.name,
        baseUrl: detail.jidanpu_base_url,
        account: detail.account,
        // 密码永远留空（后端不回显；留空 = 不改，编辑态③）。
        password: '',
        hallPricePercent: detail.hall_price_percent ?? PEER_HALL_PERCENT_DEFAULT,
        enabled: detail.enabled === true,
        orderSourceId: detail.order_source === null ? ORDER_SOURCE_NONE_VALUE : String(detail.order_source.id),
        peerHallSync: false,
        peerHallPercent: PEER_HALL_PERCENT_DEFAULT,
        ...gameRuleForm(detail.game_rules),
      }
    }
    if (bindDlg.value.restrictGames) void loadGameOptions()
  } catch (e) {
    // 读失败（行已被别人删了）原样显示后端那句，不开空弹窗（照旧页）。
    showToast(errText(e), 'error')
  } finally {
    detailLoading.value = false
  }
}

async function saveBind(): Promise<void> {
  const dlg = bindDlg.value
  if (dlg === null || saving.value) return
  const rules: UpstreamGameRules = dlg.restrictGames ? [] : null
  if (rules !== null) {
    if (dlg.gameRules.length === 0) {
      showToast('请至少选择一个同步游戏', 'error')
      return
    }
    for (const rule of dlg.gameRules) {
      const text = rule.percent.trim()
      const percent = text === '' ? null : Number(text)
      if (percent !== null && (!/^\d+$/.test(text) || !validHallPercent(percent))) {
        showToast(`${gameName(rule.gameId)}的大厅价比例需要是 1 到 100 之间的整数`, 'error')
        return
      }
      rules.push({ game_id: rule.gameId, hall_price_percent: percent })
    }
  }
  // 旧绑定里若保留着已删除游戏，修改其他字段时不重新提交旧规则。
  const gameSettings = JSON.stringify(rules) === JSON.stringify(dlg.originalGameRules ?? null)
    ? {}
    : { game_rules: rules }

  if (dlg.systemType !== SYSTEM_TYPE_TUANTUANTONG) {
    /* 鸡蛋铺分支：三条校验（url → acc → pwd；名称选填不判）。 */
    if (dlg.baseUrl.trim() === '') {
      showToast(VALIDATION_MESSAGES.url)
      return
    }
    if (dlg.account.trim() === '') {
      showToast(VALIDATION_MESSAGES.acc)
      return
    }
    // 新增密码必填；编辑留空 = 不改（编辑态③），占位跳过判据（照旧页 submit 的占位串手法）。
    if (dlg.editingId === null && dlg.password === '') {
      showToast(VALIDATION_MESSAGES.pwd)
      return
    }
    saving.value = true
    try {
      const payload = {
        jidanpu_base_url: dlg.baseUrl.trim(),
        account: dlg.account.trim(),
        name: dlg.name.trim(),
        password: dlg.password,
        hall_price_percent: dlg.hallPricePercent ?? PEER_HALL_PERCENT_DEFAULT,
        ...gameSettings,
        enabled: dlg.enabled,
        order_source_id: dlg.orderSourceId === ORDER_SOURCE_NONE_VALUE ? null : Number(dlg.orderSourceId),
      }
      if (dlg.editingId === null) await createUpstreamBindingRow(payload)
      else await updateUpstreamBindingRow(dlg.editingId, payload)
      bindDlg.value = null
      showToast(BIND_OK_TEXT)
      await reload()
    } catch (e) {
      // 失败原样显示后端 msg，不关弹窗可重试（照旧页）。
      showToast(errText(e), 'error')
    } finally {
      saving.value = false
    }
    return
  }

  if (dlg.editingId !== null) {
    const percent = dlg.peerHallPercent ?? PEER_HALL_PERCENT_DEFAULT
    if (!validHallPercent(percent)) {
      showToast(PEER_HALL_PERCENT_INVALID_TEXT)
      return
    }
    saving.value = true
    try {
      const directChanged = dlg.peerDirectEnabled !== dlg.originalPeerDirectEnabled
      await updatePeerBindingHallSettings(dlg.editingId, {
        ...(directChanged && dlg.peerHallSync === dlg.originalPeerHall?.hall_sync_enabled ? {} : { hall_sync_enabled: dlg.peerHallSync }),
        ...(directChanged && percent === dlg.originalPeerHall?.hall_price_percent ? {} : { hall_price_percent: percent }),
        ...(directChanged ? { direct_taker_assignment_enabled: dlg.peerDirectEnabled } : {}),
        ...gameSettings,
      })
      bindDlg.value = null
      showToast(PEER_SAVE_OK_TEXT)
      await reload()
    } catch (e) {
      showToast(errText(e), 'error')
    } finally {
      saving.value = false
    }
    return
  }

  /* 团团通新增：四格校验（名称 → 网址 → 账号 → 密码，照旧页 validateBindingForm 顺序）。 */
  if (dlg.name.trim() === '') {
    showToast(VALIDATION_MESSAGES.name)
    return
  }
  if (dlg.baseUrl.trim() === '') {
    showToast(VALIDATION_MESSAGES.url)
    return
  }
  if (dlg.account.trim() === '') {
    showToast(VALIDATION_MESSAGES.acc)
    return
  }
  if (dlg.password === '') {
    showToast(VALIDATION_MESSAGES.pwd)
    return
  }
  saving.value = true
  try {
    await createPeerBinding({
      peer_name: dlg.name.trim(),
      peer_base_url: dlg.baseUrl.trim(),
      peer_account: dlg.account.trim(),
      password: dlg.password,
    })
    bindDlg.value = null
    showToast(BIND_OK_TEXT)
    await reload()
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    saving.value = false
  }
}

/* ── 删除绑定（两分支确认逐字；共用一道闸与同一成功提示）────────────────────────── */

const delDlg = ref<UpstreamBindingRow | null>(null)
const deleting = ref(false)

const delConfirmText = computed(() =>
  delDlg.value?.system_type === SYSTEM_TYPE_JIDANPU ? JIDANPU_UNBIND_CONFIRM_TEXT : PEER_UNBIND_CONFIRM_TEXT,
)

async function confirmDelete(): Promise<void> {
  const row = delDlg.value
  if (row === null || deleting.value) return
  deleting.value = true
  try {
    if (row.system_type === SYSTEM_TYPE_JIDANPU) await deleteUpstreamBinding(row.id)
    else await deletePeerBinding(row.id)
    delDlg.value = null
    showToast(UNBIND_OK_TEXT)
    await reload()
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    deleting.value = false
  }
}

onMounted(() => {
  void reload()
  void loadOrderSources()
})
</script>

<template>
  <div class="cv2-page" data-v2-page="source-system">
    <!-- 工具栏两键（证据件 #toolbarDemo 逐字：刷新列表 / 绑定一个新的上家系统） -->
    <div class="cv2-toolbar" data-v2="toolbar">
      <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--find" data-v2="refresh" :disabled="loading" @click="void reload()">刷新列表</button>
      <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--commit" data-v2="add" @click="openCreateDlg">绑定一个新的上家系统</button>
    </div>

    <!-- 失败红字注意条（R20：有失败行才点亮，全好收起） -->
    <div v-if="failureText !== ''" class="ss-failure" data-v2="failure-notice">{{ failureText }}</div>

    <!-- 主表：7 列照旧页 COLS（系统类型 / 名称 / 域名新窗链接 / 子账号 / 操作人 / 绑定时间 sort / 操作） -->
    <section class="cv2-panel cv2-table-panel" aria-label="上家绑定列表">
      <table class="cv2-table">
        <thead>
          <tr>
            <th class="cv2-th-center ss-type-col">系统类型</th>
            <th>上家系统名称</th>
            <th>上家系统域名</th>
            <th>上家系统子账号</th>
            <th>绑定操作人</th>
            <th class="cv2-th-center">
              <button type="button" class="cv2-sort" data-v2="sort-bound" @click="toggleSort">
                绑定时间<span v-if="sortDir === 'asc'">↑</span><span v-else-if="sortDir === 'desc'">↓</span>
              </button>
            </th>
            <th class="cv2-th-center ss-actions-col">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in sortedRows" :key="`${row.system_type}-${row.id}`" class="cv2-row" :data-v2-row="row.id">
            <td class="cv2-td-center">{{ row.SystemType }}</td>
            <td :title="row.HomeCompany">{{ row.HomeCompany }}</td>
            <td>
              <a :href="row.base_url" target="_blank" rel="noopener" class="cv2-order-link" :title="HOST_LINK_TITLE" :data-v2-host="row.id">{{ row.base_url }}</a>
            </td>
            <td :title="row.account">{{ row.account }}</td>
            <td :title="row.bound_by_name">{{ row.bound_by_name }}</td>
            <td class="cv2-td-center cv2-td-time">{{ row.BindTimeStr }}</td>
            <td class="cv2-td-center ss-actions">
              <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--commit" :data-v2-edit-btn="row.id" title="编辑这条上家绑定" @click="void openEditDlg(row)">编辑</button>
              <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--sm" :data-v2-del-btn="row.id" title="删除该上家与本系统的绑定" @click="delDlg = row">删除绑定</button>
            </td>
          </tr>
          <tr v-if="rows.length === 0">
            <td colspan="7" class="cv2-empty" data-v2="empty">
              <template v-if="loading">正在加载…</template>
              <template v-else>{{ EMPTY_TEXT }}</template>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <div ref="dropdownTarget" />

    <!-- 编辑弹窗（双分支：鸡蛋铺 / 团团通；编辑态「系统类型」格收起——v-if 只在新增时画） -->
    <el-dialog
      :model-value="bindDlg !== null"
      :title="bindDlgTitle()"
      class="cv2-dialog cv2-dialog--md"
      destroy-on-close
      data-v2="dlg-bind"
      @update:model-value="bindDlg = null"
    >
      <template v-if="bindDlg !== null">
        <div v-if="bindDlg.editingId === null" class="cv2-field ss-form-field">
          <span class="cv2-field-label">系统类型</span>
          <el-select v-model="bindDlg.systemType" data-v2="system-type" :append-to="dropdownTarget">
            <el-option v-for="o in SYSTEM_TYPE_LABELS" :key="o.value" :value="o.value" :label="o.label" />
          </el-select>
        </div>
        <div class="cv2-field ss-form-field">
          <span class="cv2-field-label">上家系统名称</span>
          <el-input
            v-model="bindDlg.name"
            :maxlength="20"
            placeholder="可以自定义一个上家系统名称"
            :readonly="isPeerEdit"
            data-v2="name-input"
          />
        </div>
        <div class="cv2-field ss-form-field">
          <span class="cv2-field-label">上家系统网址</span>
          <el-input
            v-model="bindDlg.baseUrl"
            :maxlength="70"
            placeholder="例如：http://d.jidanpu.com"
            :readonly="isPeerEdit"
            data-v2="url-input"
          />
        </div>
        <div class="cv2-field ss-form-field">
          <span class="cv2-field-label">登录上家系统账号</span>
          <el-input
            v-model="bindDlg.account"
            :maxlength="32"
            placeholder="填上能登录上家系统的子账号"
            :readonly="isPeerEdit"
            data-v2="acc-input"
          />
        </div>
        <div class="cv2-field ss-form-field">
          <span class="cv2-field-label">登录上家系统密码</span>
          <el-input
            v-model="bindDlg.password"
            type="password"
            :maxlength="32"
            placeholder="填上能登录上家系统的密码"
            :readonly="isPeerEdit"
            data-v2="pwd-input"
          />
        </div>
        <!-- 鸡蛋铺分支三格（团团通分支隐藏，照旧页 applySystemType 的 display 分流） -->
        <template v-if="!isPeerBranch">
          <div class="cv2-field ss-form-field">
            <span class="cv2-field-label">默认发单价比例（%）</span>
            <el-input-number
              v-model="bindDlg.hallPricePercent"
              :min="1"
              :max="100"
              :step="1"
              placeholder="100"
              data-v2="jidanpu-percent"
            />
          </div>
          <div class="cv2-field ss-form-field">
            <span class="cv2-field-label">启用</span>
            <el-switch
              v-model="bindDlg.enabled"
              active-text="启用"
              inactive-text="停用"
              data-v2="enabled-switch"
            />
          </div>
          <div class="cv2-field ss-form-field">
            <span class="cv2-field-label">订单来源</span>
            <el-select v-model="bindDlg.orderSourceId" data-v2="order-source" :append-to="dropdownTarget">
              <el-option :value="ORDER_SOURCE_NONE_VALUE" :label="ORDER_SOURCE_NONE_LABEL" />
              <el-option v-for="s in sourceOptions" :key="s.id" :value="String(s.id)" :label="s.name" />
            </el-select>
          </div>
        </template>
        <template v-if="isPeerEdit">
          <div v-if="bindDlg.peerDirectEnabled !== undefined" class="cv2-field ss-form-field">
            <span class="cv2-field-label ss-direct-label">允许该上家直接指派我方打手</span>
            <el-switch
              v-model="bindDlg.peerDirectEnabled"
              aria-label="允许该上家直接指派我方打手"
              active-text="开启"
              inactive-text="关闭"
              data-v2="peer-direct-assignment-switch"
            />
          </div>
          <div class="cv2-field ss-form-field">
            <span class="cv2-field-label">同步上家抢单大厅</span>
            <el-switch
              v-model="bindDlg.peerHallSync"
              active-text="开启"
              inactive-text="关闭"
              data-v2="peer-hall-sync"
            />
          </div>
          <div class="cv2-field ss-form-field">
            <span class="cv2-field-label">默认大厅价比例（%）</span>
            <el-input-number
              v-model="bindDlg.peerHallPercent"
              :min="1"
              :max="100"
              :step="1"
              data-v2="peer-hall-percent"
            />
            <span class="cv2-field-hint" data-v2="peer-hall-hint">{{ PEER_HALL_PERCENT_HINT }}</span>
          </div>
        </template>
        <fieldset v-if="showGameRules" class="ss-game-rules" data-v2="game-rules" :disabled="saving">
          <legend>同步游戏与价格</legend>
          <div class="ss-game-scope">
            <label><input type="radio" :name="gameScopeName" :checked="!bindDlg.restrictGames" data-v2="sync-all-games" @change="setRestrictedGames(false)" /> 全部游戏</label>
            <label><input type="radio" :name="gameScopeName" :checked="bindDlg.restrictGames" data-v2="sync-selected-games" @change="setRestrictedGames(true)" /> 仅同步所选游戏</label>
          </div>
          <div v-if="bindDlg.restrictGames" class="ss-game-selection">
            <el-select
              :model-value="selectedGameIds"
              :loading="gamesLoading"
              multiple
              filterable
              collapse-tags
              placeholder="选择游戏"
              aria-label="选择同步游戏"
              data-v2="sync-game-picker"
              :append-to="dropdownTarget"
              @update:model-value="setSelectedGameIds"
            >
              <el-option v-for="game in gameOptions" :key="game.id" :label="game.name" :value="game.id" />
              <template v-for="rule in bindDlg.gameRules" :key="rule.gameId">
                <el-option v-if="!gameOptions.some((game) => game.id === rule.gameId)" :label="gameName(rule.gameId)" :value="rule.gameId" />
              </template>
            </el-select>
            <p v-if="gamesLoadFailed" class="ss-game-hint" role="status">
              游戏列表读取失败。
              <button type="button" class="cv2-btn cv2-btn--sm" @click="void loadGameOptions()">重试</button>
            </p>
            <label v-for="rule in bindDlg.gameRules" :key="rule.gameId" class="ss-game-rule">
              <span>{{ gameName(rule.gameId) }}</span>
              <el-input
                v-model="rule.percent"
                inputmode="numeric"
                :aria-label="`${gameName(rule.gameId)}大厅价比例（%）`"
                :data-v2-game-percent="rule.gameId"
                placeholder="沿用默认比例"
              />
              <span>%</span>
            </label>
          </div>
          <p class="ss-game-hint">规则用于新同步的订单。已同步的订单保留原价。</p>
        </fieldset>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="bindDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="bind-save" :disabled="saving" @click="void saveBind()">验证并保存绑定</button>
        </div>
      </template>
    </el-dialog>

    <!-- 删除绑定确认（两分支问句逐字；按钮照 layer.confirm 默认 确定 / 取消） -->
    <el-dialog
      :model-value="delDlg !== null"
      title="提示"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-delete"
      @update:model-value="delDlg = null"
    >
      <p class="cv2-dlg-text" data-v2="del-question">{{ delConfirmText }}</p>
      <div class="cv2-dlg-actions">
        <button type="button" class="cv2-btn cv2-btn--quiet" @click="delDlg = null">取消</button>
        <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--commit" data-v2="del-confirm" :disabled="deleting" @click="void confirmDelete()">确定</button>
      </div>
    </el-dialog>

    <!-- 炭火 toast（居中深条） -->
    <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
      {{ toast.text }}
    </div>
  </div>
</template>

<style scoped>
/* 本页新增的形状收在这里；色一律走 carbon.css 的 token（⛔ 不写裸色值）。 */

/* 表格内锚点动作链：基态不带全局链接下划线、悬停补回（V2UserList 同款）。 */
a.cv2-order-link {
  text-decoration: none;
}

a.cv2-order-link:hover {
  text-decoration: underline;
}

/* 域名列：新窗链接长 URL 单行截断，全名靠 title。 */
.ss-host {
  display: block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

th.ss-type-col {
  width: 110px;
}

th.ss-actions-col {
  width: 200px;
}

td.ss-actions {
  white-space: nowrap;
}

td.ss-actions .cv2-btn--sm + .cv2-btn--sm {
  margin-left: -1px;
}

/* 失败红字注意条（R20）：旧页自拟视觉 → 炭火 token（error 族）。 */
.ss-failure {
  margin: 10px 0;
  padding: 8px 12px;
  border: 1px solid var(--tt2-st-error);
  border-radius: 3px;
  background: var(--tt2-st-refund-bg);
  color: var(--tt2-st-error);
  font-size: 13px;
  line-height: 20px;
}

/* 弹窗表单格：与 filter 面的窄输入区分开（gp-form-field 同款手法）。 */
.ss-form-field {
  grid-template-columns: 135px 1fr;
  margin-bottom: 12px;
}

.ss-direct-label { white-space: normal; }

/* 大厅百分比那句人话占满第二列（cv2-field 的 > :not(label) 已收第二列）。 */
.ss-form-field .cv2-field-hint {
  grid-column: 2;
}

/* 控件拉满第二列（业务方回验：carbon.css 筛选行的 160/172px 定宽在编辑弹窗里太窄）——
 * 输入/下拉/数字框与弹窗内容区同宽成列，新增与编辑两分支同一形态。 */
.cv2-page :deep(.ss-form-field .el-input),
.cv2-page :deep(.ss-form-field .el-select),
.cv2-page :deep(.ss-form-field .el-input-number) {
  width: 100%;
}

.ss-game-rules {
  min-width: 0;
  margin: 0 0 16px;
  padding: 12px;
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
}

.ss-game-rules legend { padding: 0 4px; font-weight: 600; }

.ss-game-scope {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
}

.ss-game-scope label { display: flex; align-items: center; gap: 4px; }
.ss-game-scope input { accent-color: var(--tt2-primary); }

.ss-game-selection {
  display: grid;
  gap: 8px;
  margin-top: 10px;
}

.ss-game-rule {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(105px, 150px) min-content;
  align-items: center;
  gap: 8px;
}

.ss-game-rule :deep(.el-input) { width: 100%; }

.ss-game-hint {
  margin: 8px 0 0;
  color: var(--tt2-dim);
  font-size: 12px;
}

/* 全局提示 = 对标 layer.msg 居中深条（V2Notices 同款；覆盖 carbon.css 顶部白卡片形态）。 */
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
</style>
