<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import {
  fetchApprovalOptions, fetchApprovalTargets, submitPenalty,
  type ApprovalAccount, type ApprovalOptions,
} from '@/api/approvals'
import { fetchGames, type ConfigRow } from '@/api/config'
import { fetchAllOrderSources } from '@/api/orderSources'
import { fetchOrder, fetchOrders, type Order } from '@/api/orders'
import { trimLikePython, validateMoneyInput } from '@/config/money'
import OrderTabDialog from '@/v2/OrderTabDialog.vue'
import V2ApprovalEvidence from '@/v2/V2ApprovalEvidence.vue'
import V2PenaltyLookup from '@/v2/V2PenaltyLookup.vue'

type OriginalChoice = { value: string | null; label: string }

const props = defineProps<{ originalOrderNo?: string }>()
const emit = defineEmits<{ close: []; submitted: [] }>()

const options = ref<ApprovalOptions | null>(null)
const games = ref<ConfigRow[]>([])
const sources = ref<ApprovalAccount[]>([])
const sourceState = ref<'idle' | 'loading' | 'ready' | 'error'>('idle')
const sourceError = ref('')
const original = ref<OriginalChoice>()
const linked = ref<Order | null>(null)
const noOriginal = computed(() => original.value?.value === null)
const manualGame = ref<number>()
const game = computed(() => linked.value?.game ?? (noOriginal.value ? manualGame.value : undefined))
const gameLabel = computed(() => games.value.find((row) => row.id === linked.value?.game)?.name ?? '')
const orderType = ref('BOOST')
const source = ref<number>()
const totalPrice = ref('0')
const projectText = ref('')
const projectEdited = ref(false)
const target = ref<ApprovalAccount | null>(null)
const checkingTarget = ref(false)
let targetCheck = 0

const today = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'long', timeZone: 'Asia/Shanghai' }).format(new Date())
const amount = ref('')
const reason = ref('')
const objection = ref('')
const remark = ref('')
const approver = ref<ApprovalAccount | null>(null)
const choosingApprover = ref(false)
const approverQuery = ref('')
const cc = ref<number[]>([])
const imageKeys = ref<string[]>([])
const initialLoading = ref(true)
const linkedLoading = ref(false)
const saving = ref(false)
const uploading = ref(false)
const error = ref('')
const feedback = ref('')
const savingDisabled = computed(() => initialLoading.value || linkedLoading.value || checkingTarget.value
  || saving.value || uploading.value || options.value?.can_apply !== true)
const approvers = computed(() => {
  const query = approverQuery.value.trim().toLocaleLowerCase()
  return options.value?.approvers.filter((person) => person.name.toLocaleLowerCase().includes(query)) ?? []
})
const defaultCc = computed(() => options.value?.available_cc.filter((person) => options.value?.default_cc_ids.includes(person.id)) ?? [])
const optionalCc = computed(() => options.value?.available_cc.filter((person) => !options.value?.default_cc_ids.includes(person.id)) ?? [])
const projectLimit = 900
const projectShortened = computed(() => !projectEdited.value && Array.from(reason.value).length > projectLimit)

watch(reason, (value) => {
  if (!projectEdited.value) projectText.value = Array.from(value).slice(0, projectLimit).join('')
})

function message(cause: unknown, fallback: string): string {
  return cause instanceof Error && cause.message ? cause.message : fallback
}

async function searchOriginals(raw: string) {
  const keyword = raw.trim()
  if (!keyword) return []
  const paging = { page: 1, page_size: 20 }
  const exact = !keyword.includes(',') && Array.from(keyword).length <= 128
    ? (await fetchOrders({ batch_lookup_nos: keyword, ...paging })).items
    : []
  const normalized = keyword.toLowerCase()
  const matches = exact.length ? exact : (await fetchOrders({ keyword, ...paging })).items.filter((row) =>
    row.order_no.includes(normalized) || row.source_order_no.toLowerCase().includes(normalized))
  return matches.map((row) => ({
    value: row.order_no,
    label: row.source_order_no || row.order_no,
    detail: row.source_order_no && row.source_order_no !== row.order_no ? row.order_no : undefined,
  }))
}

async function loadSources(): Promise<void> {
  if (sourceState.value === 'ready' || sourceState.value === 'loading') return
  sourceState.value = 'loading'
  sourceError.value = ''
  try {
    sources.value = await fetchAllOrderSources()
    sourceState.value = 'ready'
  } catch (cause) {
    sourceState.value = 'error'
    sourceError.value = message(cause, '订单来源读取失败')
  }
}

function resetTargetCheck(): void {
  targetCheck += 1
  checkingTarget.value = false
}

function editOriginal(): void {
  if (original.value === undefined) return
  original.value = undefined
  linked.value = null
  feedback.value = ''
  resetTargetCheck()
}

async function chooseOriginal(choice: OriginalChoice): Promise<void> {
  original.value = choice
  linked.value = null
  feedback.value = ''
  resetTargetCheck()
  if (choice.value === null) {
    void loadSources()
    await checkTarget()
    return
  }
  linkedLoading.value = true
  try {
    linked.value = await fetchOrder(choice.value)
    await checkTarget(true)
  } catch (cause) {
    feedback.value = message(cause, '原订单读取失败')
  } finally {
    linkedLoading.value = false
  }
}

async function searchTargets(key: string) {
  const result = await fetchApprovalTargets(key.trim(), linked.value?.order_no, game.value)
  return result.items.map((person) => ({ value: person.id, label: person.name, detail: person.detail }))
}

async function checkTarget(suggest = false): Promise<void> {
  const selected = target.value
  if (!game.value || (!selected && !suggest)) return
  const revision = ++targetCheck
  checkingTarget.value = true
  try {
    const result = await fetchApprovalTargets(selected?.name ?? '', linked.value?.order_no, game.value)
    if (revision !== targetCheck) return
    if (!selected) {
      target.value = result.suggested
    } else if (!result.items.some((person) => person.id === selected.id)) {
      target.value = null
      feedback.value = '原选对象没有该游戏的接单权限，请重新选择罚款对象'
    }
  } catch (cause) {
    if (revision === targetCheck) feedback.value = message(cause, '罚款对象读取失败')
  } finally {
    if (revision === targetCheck) checkingTarget.value = false
  }
}

function chooseGame(event: Event): void {
  manualGame.value = Number((event.target as HTMLSelectElement).value) || undefined
  feedback.value = ''
  resetTargetCheck()
  void checkTarget()
}

function chooseTarget(choice: { value: number; label: string }): void {
  resetTargetCheck()
  feedback.value = ''
  target.value = { id: choice.value, name: choice.label }
}

function editTarget(): void {
  resetTargetCheck()
  target.value = null
}

function openApprovers(): void {
  approverQuery.value = ''
  choosingApprover.value = true
}

function chooseApprover(person: ApprovalAccount): void {
  approver.value = person
  choosingApprover.value = false
}

onMounted(async () => {
  const [approvalOptions, availableGames] = await Promise.allSettled([
    fetchApprovalOptions(), fetchGames(),
  ])
  if (approvalOptions.status === 'fulfilled') options.value = approvalOptions.value
  else error.value = message(approvalOptions.reason, '审批配置读取失败')
  if (availableGames.status === 'fulfilled') games.value = availableGames.value
  else feedback.value = message(availableGames.reason, '游戏列表读取失败')
  initialLoading.value = false
  if (options.value?.can_apply && props.originalOrderNo) {
    await chooseOriginal({ value: props.originalOrderNo, label: props.originalOrderNo })
  }
})

function close(): void {
  if (!saving.value && !uploading.value) emit('close')
}

async function submit(): Promise<void> {
  if (savingDisabled.value) return
  error.value = ''
  if (!original.value || (!noOriginal.value && !linked.value)) {
    error.value = '请选择关联订单号，或选择“无”'
    return
  }
  if (!target.value) { error.value = '请选择罚款对象'; return }
  const amountText = trimLikePython(amount.value)
  const checkedAmount = validateMoneyInput(amountText)
  if (!checkedAmount.ok) { error.value = `罚款金额：${checkedAmount.msg}`; return }
  if (Number(amountText) <= 0) { error.value = '请输入大于 0 的罚款金额'; return }
  const reasonText = trimLikePython(reason.value)
  if (!reasonText) { error.value = '请填写罚款原因'; return }
  if (approver.value === null) { error.value = '请选择一位审批人'; return }

  const project = trimLikePython(projectText.value)
  if (!project) { error.value = noOriginal.value ? '请填写代练内容' : '请填写项目内容'; return }
  let order: { project_text: string; game?: number; order_source?: number; order_type?: string; total_price?: string } = { project_text: project }
  if (noOriginal.value) {
    if (manualGame.value === undefined) { error.value = '请选择游戏'; return }
    if (source.value === undefined) { error.value = '请选择订单来源'; return }
    const price = trimLikePython(totalPrice.value) || '0'
    const checkedPrice = validateMoneyInput(price)
    if (!checkedPrice.ok) { error.value = `接单价：${checkedPrice.msg}`; return }
    order = { ...order, game: manualGame.value, order_source: source.value, order_type: orderType.value, total_price: price }
  }
  saving.value = true
  try {
    await submitPenalty({
      original_order_no: original.value.value,
      target_id: target.value.id,
      amount: amountText,
      reason: reasonText,
      objection: objection.value.trim(),
      remark: remark.value.trim(),
      approver_id: approver.value.id,
      cc_ids: cc.value,
      image_keys: imageKeys.value,
      order,
    })
    emit('submitted')
  } catch (cause) {
    error.value = message(cause, '提交失败，请重试')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <OrderTabDialog
    :model-value="true"
    title="发起罚款审批"
    width="min(630px, calc(100% - 24px))"
    :close-on-click-modal="false"
    :close-on-press-escape="!saving && !uploading"
    :show-close="!saving && !uploading"
    @update:model-value="close"
  >
    <form class="penalty-form" novalidate @submit.prevent="submit">
      <p v-if="initialLoading" class="penalty-feedback" role="status">正在读取审批配置…</p>
      <p v-else-if="options && !options.can_apply" class="penalty-feedback" role="status">当前账号没有发起罚款的权限。</p>

      <fieldset class="penalty-grid" aria-label="罚款信息" :disabled="saving || linkedLoading">
        <div class="penalty-field">
          <span><i class="penalty-required" aria-hidden="true">*</i>关联订单号</span>
          <V2PenaltyLookup
            label="关联订单号" placeholder="输入系统单号或来源单号搜索"
            :selection="original?.label ?? originalOrderNo" :read-only="Boolean(originalOrderNo)"
            :always="{ value: null, label: '无' }" :search="searchOriginals"
            @choose="chooseOriginal" @edit="editOriginal"
          />
        </div>

        <div class="penalty-field">
          <span><i class="penalty-required" aria-hidden="true">*</i>罚款对象</span>
          <V2PenaltyLookup
            :key="linked?.order_no ?? game" label="罚款对象" placeholder="输入姓名搜索"
            :selection="target?.name" :search="searchTargets"
            @choose="chooseTarget" @edit="editTarget"
          />
        </div>

        <label class="penalty-field">
          <span><i class="penalty-required" aria-hidden="true">*</i>罚款金额（元）</span>
          <input v-model="amount" aria-label="罚款金额（元）" required inputmode="decimal" maxlength="13" placeholder="输入正数" />
        </label>
        <div class="penalty-field">
          <span><i class="penalty-required" aria-hidden="true">*</i>审批人</span>
          <button
            type="button" class="penalty-approver" aria-label="选择审批人" aria-haspopup="dialog"
            :disabled="initialLoading || !options?.approvers.length" @click="openApprovers"
          >{{ approver?.name ?? (options?.approvers.length ? '点击选择审批人' : '暂无可选审批人') }}</button>
        </div>

        <label class="penalty-field penalty-wide">
          <span><i class="penalty-required" aria-hidden="true">*</i>罚款原因</span>
          <textarea v-model="reason" aria-label="罚款原因" required rows="2" maxlength="3000"></textarea>
        </label>
        <div class="penalty-field penalty-wide">
          <span>证据截图 <small>选填</small></span>
          <V2ApprovalEvidence :disabled="saving" @change="imageKeys = $event" @busy-change="uploading = $event" />
        </div>

        <label class="penalty-field">
          <span>罚款对象是否异议 <small>选填</small></span>
          <input v-model="objection" aria-label="罚款对象是否异议" maxlength="3000" />
        </label>
        <label class="penalty-field">
          <span>备注 <small>选填</small></span>
          <input v-model="remark" aria-label="备注" maxlength="3000" />
        </label>
        <div v-if="defaultCc.length || optionalCc.length" class="penalty-field penalty-wide">
          <span>抄送人 <small v-if="!defaultCc.length">选填</small></span>
          <div class="penalty-cc">
            <span v-for="person in defaultCc" :key="person.id" class="penalty-cc-fixed">{{ person.name }} <small>默认</small></span>
            <el-select v-if="optionalCc.length" v-model="cc" multiple filterable collapse-tags collapse-tags-tooltip
              aria-label="追加抄送人" placeholder="追加抄送人（选填）">
              <el-option v-for="person in optionalCc" :key="person.id" :value="person.id" :label="person.name" />
            </el-select>
          </div>
        </div>
      </fieldset>

      <fieldset v-if="linked || noOriginal" class="penalty-grid penalty-record" :aria-label="noOriginal ? '录单信息' : '关联订单信息'" :disabled="saving || linkedLoading">
        <legend v-if="noOriginal">录单信息</legend>
        <p v-else-if="gameLabel" class="penalty-linked-game penalty-wide">游戏 <strong>{{ gameLabel }}</strong></p>
        <label v-if="noOriginal" class="penalty-field">
          <span><i class="penalty-required" aria-hidden="true">*</i>游戏</span>
          <select :value="manualGame ?? ''" aria-label="游戏" required @change="chooseGame">
            <option value="">请选择游戏</option>
            <option v-for="item in games" :key="item.id" :value="item.id">{{ item.name }}</option>
          </select>
        </label>
        <div v-if="noOriginal" class="penalty-field">
          <span><i class="penalty-required" aria-hidden="true">*</i>订单来源</span>
          <select v-model="source" aria-label="订单来源" :disabled="sourceState !== 'ready'" required>
            <option :value="undefined">{{ sourceState === 'loading' ? '正在读取来源…' : '请选择来源' }}</option>
            <option v-for="item in sources" :key="item.id" :value="item.id">{{ item.name }}</option>
          </select>
          <div v-if="sourceError" class="penalty-source-error">
            <span class="penalty-error" role="alert">{{ sourceError }}</span>
            <button type="button" class="cv2-btn cv2-btn--quiet" @click="loadSources">重试</button>
          </div>
        </div>
        <label v-if="noOriginal" class="penalty-field">
          <span>订单类型 <small>选填</small></span>
          <select v-model="orderType" aria-label="订单类型">
            <option value="BOOST">代练</option>
            <option value="COMPANION">陪玩</option>
          </select>
        </label>
        <label v-if="noOriginal" class="penalty-field">
          <span>接单价（元） <small>选填</small></span>
          <input v-model="totalPrice" aria-label="接单价（元）" inputmode="decimal" maxlength="14" />
        </label>
        <label class="penalty-field penalty-wide">
          <span><i class="penalty-required" aria-hidden="true">*</i>{{ noOriginal ? '代练内容' : '项目内容' }}</span>
          <textarea
            v-model="projectText" :aria-label="noOriginal ? '代练内容' : '项目内容'" required rows="2" :maxlength="projectLimit"
            @input="projectEdited = true"
          ></textarea>
          <small v-if="projectShortened">已带入罚款原因前 {{ projectLimit }} 字，罚款原因保留全文</small>
        </label>
      </fieldset>

      <p v-if="feedback" class="penalty-feedback" role="status">{{ feedback }}</p>
      <p v-if="error" class="penalty-error" role="alert">{{ error }}</p>
      <div class="penalty-actions">
        <span class="penalty-date">日期 {{ today }}</span>
        <button type="button" class="cv2-btn cv2-btn--quiet" :disabled="saving || uploading" @click="close">取消</button>
        <button type="submit" class="cv2-btn cv2-btn--submit" :disabled="savingDisabled">{{ saving ? '提交中…' : '提交审批' }}</button>
      </div>
    </form>
  </OrderTabDialog>

  <OrderTabDialog
    v-if="choosingApprover" :model-value="true" title="选择审批人"
    width="min(400px, calc(100% - 24px))" @update:model-value="choosingApprover = false"
  >
    <div class="penalty-approver-picker">
      <input v-model="approverQuery" aria-label="搜索审批人" placeholder="输入姓名搜索" />
      <ul>
        <li v-for="person in approvers" :key="person.id">
          <button
            type="button" class="penalty-approver" :aria-pressed="person.id === approver?.id"
            @click="chooseApprover(person)"
          >{{ person.name }}</button>
        </li>
      </ul>
      <p v-if="!approvers.length" class="penalty-feedback" role="status">没有匹配的审批人</p>
    </div>
  </OrderTabDialog>
</template>

<style scoped>
.penalty-form { display: grid; gap: 9px; }
.penalty-grid { min-inline-size: 0; margin: 0; padding: 0; border: 0; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px 12px; }
.penalty-record { border-top: 1px solid var(--tt2-line); padding-top: 7px; }
.penalty-record legend { padding-right: 6px; color: var(--tt2-ink); font-weight: 600; }
.penalty-field { min-width: 0; display: grid; align-content: start; gap: 3px; }
.penalty-field > :is(input, textarea, select), .penalty-approver, .penalty-approver-picker > input { box-sizing: border-box; min-width: 0; min-height: 32px; padding: 4px 8px; border: 1px solid var(--tt2-filter-line); border-radius: 4px; background: var(--tt2-panel); color: var(--tt2-ink); font: inherit; }
.penalty-field > :is(input, select), .penalty-approver, .penalty-approver-picker > input { height: 34px; }
.penalty-field > textarea { resize: vertical; }
.penalty-field > :is(input, textarea, select):focus-visible, .penalty-approver:focus-visible, .penalty-approver-picker > input:focus-visible { outline: 1px solid var(--tt2-primary); border-color: var(--tt2-primary); }
.penalty-required { margin-right: 3px; color: var(--tt2-st-error); font-style: normal; }
.penalty-wide { grid-column: 1 / -1; }
.penalty-linked-game { display: flex; gap: 8px; margin: 0; color: var(--tt2-dim); }
.penalty-linked-game strong { color: var(--tt2-ink); }
.penalty-source-error { display: flex; align-items: baseline; gap: 4px; }
.penalty-approver { text-align: start; cursor: pointer; }
.penalty-approver:disabled { color: var(--tt2-dim); cursor: default; }
.penalty-approver:hover:not(:disabled), .penalty-approver[aria-pressed="true"] { background: var(--tt2-assignee-bg); }
.penalty-approver-picker { display: grid; gap: 7px; }
.penalty-approver-picker ul { display: grid; gap: 4px; max-height: 260px; overflow: auto; margin: 0; padding: 0; list-style: none; }
.penalty-approver-picker .penalty-approver { width: 100%; }
.penalty-cc { display: flex; flex-wrap: wrap; align-items: center; gap: 5px; }
.penalty-cc .el-select { width: min(220px, 100%); }
.penalty-cc-fixed { background: var(--tt2-assignee-bg); color: var(--tt2-assignee-ink); padding: 2px 6px; border-radius: 4px; }
.penalty-actions { display: flex; align-items: center; justify-content: flex-end; gap: 8px; }
.penalty-date { margin-inline-end: auto; }
.penalty-date, .penalty-feedback, small { color: var(--tt2-dim); }
.penalty-feedback, .penalty-error { margin: 0; }
.penalty-error { color: var(--tt2-st-error); }
@media (max-width: 500px) { .penalty-grid { grid-template-columns: minmax(0, 1fr); } }
</style>
