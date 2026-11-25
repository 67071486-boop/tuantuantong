<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「清算绩效」弹窗（v2 组件，⛔ iframe；财务族第二批 2026-11-06，计划 §2.1）。
 *
 * 旧载体 = island 子页 `layui-island/pages/emp-settlementemp.ts`（对标 `/Emp/SettlementEmp`，
 * 父页 `layer.open({type:2})` 打开）。本组件把同一功能落成炭火 el-dialog：
 * 顶部引用块逐字 → 五格（笔数 / 预计要结总额 / 实际需结总额 / 结算对象 / 结算状态 radio）
 * → 选「已结算」展开「同步到账本」与「微信收款码」区（QR 随 `GET settlements/preview`
 * 一起取回，图下红字姓名行；点图开大图弹窗）→「确定结算或修改」。
 * 金额格口径 count>0 ⇒ `X.XX元`、0 ⇒ `0元`（照旧页 moneyText）；撤销方向先原因弹窗
 * （title「仅修改结算状态」、placeholder 逐字、空值不发请求）；结果文案两方向各三形态逐字
 * （settleResultText / revokeResultText 同语义本地重写）；inflight 挡连点。
 * 成功 → toast + emit('settled')（父页关窗并就地刷新）。
 */
import { computed, onMounted, ref } from 'vue'
import {
  fetchSettlementPreview,
  revokeSettlements,
  settlePerformances,
  type SettlementPreview,
  type SettlementPreviewOwner,
  type SettlementPreviewQrCode,
} from '@/api/settlements'

const props = defineProps<{
  /** 绩效行主键（ Performance.id ）——预览 / 结账 / 撤销同一套标识。 */
  ids: number[]
}>()

const emit = defineEmits<{ (e: 'close'): void; (e: 'settled'): void }>()

/* ── 逐字文案（出处 = island 页同名常量，本地重写不引模块）────────────────── */

const QUOTE_TEXT =
  '你正在进行给代练打手或销售人员进行账单结算或修改结算状态，请注意所勾选的绩效记录是否有误！' +
  '注意：订单状态必须在待验收、已完成、已退单等时才可以改成已结算'
const NO_STATUS_TEXT = '请选择要修改的清算状态'
const SYNC_NOT_OPEN_TEXT = '「同步到账本」尚未开放，本次只改结算状态'
const ALREADY_SAME_TEXT = '所选记录已是该状态，无需修改'
const REASON_REQUIRED_TEXT = '请填写撤销结账的原因（会写进订单日志）'
const REASON_TITLE = '仅修改结算状态'
const QR_PHOTOS_TITLE = '微信付款码'
const FAILED_TEXT = '操作失败'

/** 金额两位小数（后端字符串；非数字回落 0.00，⛔ 不显示 NaN）。 */
function formatMoney(value: string | number): string {
  const n = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(n) ? n.toFixed(2) : '0.00'
}

/** 笔数格：`0笔` / `2笔`。 */
function countText(count: number): string {
  return `${count}笔`
}

/** 金额格：count > 0 ⇒ `X.XX元`；0 ⇒ `0元`（判据取笔数，照旧页 moneyText）。 */
function moneyText(count: number, total: string | number): string {
  return count > 0 ? `${formatMoney(total)}元` : '0元'
}

/** 结算对象格：按人聚合 `名字（金额）`，多人全角逗号隔开；名字 = 显示名回落登录账号。 */
function ownersText(owners: readonly SettlementPreviewOwner[]): string {
  return owners
    .map((o) => `${o.name ?? o.username ?? ''}（${formatMoney(o.amount)}）`)
    .join('，')
}

/**
 * 「已结账」方向结果文案（三形态 + skipped 补句；判据只看 id 列表长度——
 * settled_adjustments 非空 = 坏账出口那一轮「钱动了」，⛔ 不能说「无需修改」）。
 */
function settleResultText(settled: number, skipped: number, adjustments: number): string {
  if (settled === 0 && adjustments === 0) return ALREADY_SAME_TEXT
  const head = settled > 0 ? `已结账 ${settled} 条` : `已结清调整记录 ${adjustments} 条`
  return skipped > 0 ? `${head}（其中 ${skipped} 条本来就是已结账，已跳过）` : head
}

/** 「已撤销结账」方向：与上一条同三形态、换个词（Q3=A）。 */
function revokeResultText(revoked: number, skipped: number, adjustments: number): string {
  if (revoked === 0 && adjustments === 0) return ALREADY_SAME_TEXT
  const head =
    revoked > 0 ? `已撤销结账 ${revoked} 条` : `已撤销结账的调整记录 ${adjustments} 条`
  return skipped > 0 ? `${head}（其中 ${skipped} 条本来就是未结账，已跳过）` : head
}

/* ── toast（父页传入则复用父页的；本组件也自带一份炭火 toast 落点）──────────── */

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

/* ── 预览取数（一次端点填五格 + QR；失败保留零态并如实报错）────────────────── */

const preview = ref<SettlementPreview | null>(null)
const countLabel = ref('0笔')
const tempLabel = ref('0元')
const bonusLabel = ref('0元')
const ownersLabel = ref('')
const qrCodes = ref<SettlementPreviewQrCode[]>([])

/** 一次预览填五格 + QR（onMounted 首拉的唯一填充路）。 */
function applyPreview(data: SettlementPreview): void {
  preview.value = data
  countLabel.value = countText(data.count)
  tempLabel.value = moneyText(data.count, data.temp_total)
  bonusLabel.value = moneyText(data.count, data.bonus_total)
  ownersLabel.value = ownersText(data.owners)
  qrCodes.value = data.qr_codes
}

onMounted(() => {
  void fetchSettlementPreview(props.ids)
    .then(applyPreview)
    .catch((e: unknown) => {
      showToast(e instanceof Error ? e.message : FAILED_TEXT, 'error')
    })
})

/* ── 状态与提交 ────────────────────────────────────────────────────────── */

/** 未结算 N / 已结算 Y（照旧页 radio 值）。 */
const sta = ref<'' | 'N' | 'Y'>('')
/** 同步到账本：不需要 N（默认）/ 需要 Y。 */
const syncExp = ref<'N' | 'Y'>('N')
/** 挡连点（失败后复位可重试）。 */
const inflight = ref(false)

/** 撤销原因小弹窗（N 方向先弹；空值不发请求）。 */
const reasonOpen = ref(false)
const reason = ref('')

/** QR 大图弹窗。 */
const bigQr = ref<{ url: string; name: string } | null>(null)

function onQrClick(item: SettlementPreviewQrCode): void {
  bigQr.value = { url: item.url, name: item.username ?? '' }
}

function onSubmit(): void {
  if (sta.value === '') {
    // 没选清算状态 ⇒ 提示且零请求（对标 JS 同款）。
    showToast(NO_STATUS_TEXT)
    return
  }
  if (inflight.value) return
  if (sta.value === 'Y') {
    if (syncExp.value === 'Y') showToast(SYNC_NOT_OPEN_TEXT)
    inflight.value = true
    void settlePerformances([...props.ids], '')
      .then((r) => finish(settleResultText(r.settled_performances.length, r.skipped_performances.length, r.settled_adjustments.length)))
      .catch((e: unknown) => showToast(e instanceof Error ? e.message : FAILED_TEXT, 'error'))
      .finally(() => {
        inflight.value = false
      })
    return
  }
  // N 方向：先原因弹窗（placeholder 逐字；空值不发请求）。
  reason.value = ''
  reasonOpen.value = true
}

function onReasonSubmit(): void {
  const trimmed = reason.value.trim()
  if (trimmed === '') {
    showToast(REASON_REQUIRED_TEXT)
    return
  }
  reasonOpen.value = false
  if (inflight.value) return
  inflight.value = true
  void revokeSettlements([...props.ids], trimmed)
    .then((r) => finish(revokeResultText(r.revoked_performances.length, r.skipped_performances.length, r.revoked_adjustments.length)))
    .catch((e: unknown) => showToast(e instanceof Error ? e.message : FAILED_TEXT, 'error'))
    .finally(() => {
      inflight.value = false
    })
}

/** 成功：toast（info 短显）→ 关窗并让父页刷新。 */
function finish(text: string): void {
  showToast(text)
  emit('settled')
}

const revokePlaceholder = computed(
  () =>
    `把勾选的 ${props.ids.length} 条绩效从「已结账」改回「未结账」；原因必填（会写进订单日志）`,
)
</script>

<template>
  <el-dialog
    model-value
    title="清算绩效"
    class="cv2-dialog cv2-dialog--md"
    data-v2="dlg-settle"
    @update:model-value="emit('close')"
  >
    <!-- 顶部引用块（逐字） -->
    <blockquote class="stl-quote" data-v2="quote">{{ QUOTE_TEXT }}</blockquote>

    <!-- 五格（前四格读 preview；金额格口径 count>0 ⇒ X.XX元、0 ⇒ 0元） -->
    <div class="stl-cells">
      <div class="stl-cell">
        <span class="stl-label">所选订单笔数</span>
        <span class="stl-value" data-v2="cell-count">{{ countLabel }}</span>
      </div>
      <div class="stl-cell">
        <span class="stl-label">预计要结总额</span>
        <span class="stl-value stl-value--strong" data-v2="cell-temp">{{ tempLabel }}</span>
      </div>
      <div class="stl-cell">
        <span class="stl-label">实际需结总额</span>
        <span class="stl-value stl-value--strong" data-v2="cell-bonus">{{ bonusLabel }}</span>
      </div>
      <div class="stl-cell">
        <span class="stl-label">结算对象</span>
        <span class="stl-value stl-value--strong" data-v2="cell-owners">{{ ownersLabel }}</span>
      </div>
      <div class="stl-cell">
        <span class="stl-label">结算状态</span>
        <el-radio-group v-model="sta" class="stl-radios" data-v2="sta-radios">
          <el-radio value="N" data-v2-radio="N">未结算</el-radio>
          <el-radio value="Y" data-v2-radio="Y">已结算</el-radio>
        </el-radio-group>
      </div>
      <!-- 选「已结算」才展开的两区（照旧页 .expdiv 显隐） -->
      <template v-if="sta === 'Y'">
        <div class="stl-cell" data-v2="sync-block">
          <span class="stl-label">同步到账本</span>
          <el-radio-group v-model="syncExp" class="stl-radios" data-v2="sync-radios">
            <el-radio value="N">不需要</el-radio>
            <el-radio value="Y">需要（将会在财务运营/日常账本里新增一笔支出款的记录）</el-radio>
          </el-radio-group>
        </div>
        <div class="stl-cell" data-v2="qr-block">
          <span class="stl-label">微信收款码</span>
          <div class="stl-qrbox" data-v2="qr-box">
            <button
              v-for="item in qrCodes"
              :key="item.user_id"
              type="button"
              class="stl-qr-item"
              :data-v2-qr="item.user_id"
              @click="onQrClick(item)"
            >
              <img :src="item.url" alt="微信收款码" class="stl-qr-img" />
              <span class="stl-qr-user">{{ item.username ?? '' }}</span>
              <span class="stl-qr-name">{{ item.name }}</span>
            </button>
            <span v-if="qrCodes.length === 0" class="stl-qr-empty">没有可显示的收款码</span>
          </div>
        </div>
      </template>
    </div>

    <div class="stl-actions">
      <button
        type="button"
        class="cv2-btn cv2-btn--primary cv2-btn--commit"
        data-v2="submit"
        :disabled="inflight"
        @click="onSubmit"
      >
        确定结算或修改
      </button>
    </div>

    <!-- 撤销原因小弹窗（N 方向先弹；title / placeholder / 空值提示逐字） -->
    <el-dialog
      v-model="reasonOpen"
      :title="REASON_TITLE"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-reason"
    >
      <el-input
        v-model="reason"
        type="textarea"
        :rows="4"
        :placeholder="revokePlaceholder"
        data-v2="revoke-reason"
      />
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--quiet" data-v2="revoke-cancel" @click="reasonOpen = false">关闭</button>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="revoke-submit" :disabled="inflight" @click="onReasonSubmit">提交</button>
        </div>
      </template>
    </el-dialog>

    <!-- QR 大图弹窗（title 逐字「微信付款码」） -->
    <el-dialog
      :model-value="bigQr !== null"
      :title="QR_PHOTOS_TITLE"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-qr-big"
      @update:model-value="bigQr = null"
    >
      <div v-if="bigQr !== null" class="stl-qr-big">
        <img :src="bigQr.url" alt="微信收款码" data-v2="qr-big-img" />
      </div>
    </el-dialog>

    <div
      v-if="toast"
      class="cv2-toast"
      :class="{ 'cv2-toast--error': toast.kind === 'error' }"
      role="status"
      data-v2="toast"
    >
      {{ toast.text }}
    </div>
  </el-dialog>
</template>

<style scoped>
/* 本页形状收在这里；色一律走 --tt2-* token（⛔ 裸色值）。 */

/* 顶部引用块（旧页 blockquote 同位）。 */
.stl-quote {
  margin: 0 0 12px;
  padding: 8px 12px;
  border-left: 3px solid var(--tt2-primary);
  background: var(--tt2-tool-bg);
  font-size: 13px;
  color: var(--tt2-ink);
}

/* 五格：标签右缘 + 红值（旧页四格 color:#ff0000 的炭火映射 = money-red）。 */
.stl-cells {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.stl-cell {
  display: flex;
  align-items: baseline;
  gap: 12px;
  flex-wrap: wrap;
}
.stl-label {
  flex: none;
  min-width: 96px;
  font-size: 13px;
  color: var(--tt2-dim);
}
.stl-value {
  color: var(--tt2-money-red);
}
.stl-value--strong {
  font-weight: 600;
}
.stl-radios {
  flex-wrap: wrap;
}

/* 微信收款码区：图 + 图下两行（登录账号行 + 红字姓名行 14px，照旧页 .qrCodeBox）。 */
.stl-qrbox {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}
.stl-qr-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  border: 0;
  background: transparent;
  padding: 0;
  cursor: zoom-in;
  font: inherit;
  line-height: 20px;
}
.stl-qr-img {
  width: 98px;
  height: 98px;
}
.stl-qr-user {
  font-size: 12px;
  color: var(--tt2-dim);
}
.stl-qr-name {
  font-size: 14px;
  color: var(--tt2-money-red);
}
.stl-qr-empty {
  color: var(--tt2-dim);
}

.stl-actions {
  margin-top: 14px;
  text-align: center;
}

/* 大图。 */
.stl-qr-big {
  text-align: center;
}
.stl-qr-big img {
  max-width: 300px;
  max-height: 300px;
}
</style>
