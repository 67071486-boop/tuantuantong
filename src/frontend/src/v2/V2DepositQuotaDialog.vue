<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「押金额度」弹层（v2 人员管理；系统人员批次 2026-11-05，计划 §3.1.2）。
 *
 * 旧页 = island 子页 `layui-island/pages/taker-deposit-quota.ts`（**自有功能**，对标没有）。
 * 单人（不做批量）；md 弹窗（旧 iframe 720×560，内容含工具行 + 三列长说明）。
 * 行为照旧页：
 * - 「启用个人押金额度」开关 + 说明文案随开关变（逐字）+ 灰字总闸提示逐字。
 * - 工具条「统一额度 [输入] 元 [一键填充][按全局额度填充]」；三列表（游戏/全局额度/个人额度，
 *   `amount === null` = 空框 placeholder 0）。
 * - **GET 回 `enabled=false` ⇒ 整页只读**：顶部红字 + 开关/输入/填充/保存全禁用
 *   （总闸的打开口在列表那一列，本页不提供第二条路）。
 * - 校验 `/^\d+(\.\d{1,2})?$/`（非法 ⇒ 提示且不发请求）；填充覆盖已有非空且不同值时
 *   confirm 一次；空框保存归一 `"0"`；保存 PUT `{enabled, amounts}`（键 = game_id 字符串、
 *   值 = 金额字符串原样）。成功关层刷新；失败弹后端 msg 不关层。
 */
import { computed, onMounted, ref } from 'vue'
import {
  fetchDepositQuota,
  saveDepositQuota,
  type DepositQuota,
} from '@/api/takerDepositQuota'

const props = defineProps<{
  userId: number
  notify: (text: string, kind?: 'info' | 'error') => void
}>()

const emit = defineEmits<{ (e: 'close'): void; (e: 'saved'): void }>()

const MSG = {
  fillEmpty: '请输入统一额度',
  invalidAmount: '额度必须是大于等于 0 的数字，最多两位小数',
  confirmOverwrite: '已填写的个人额度将被覆盖，确定继续吗？',
  saved: '押金额度已保存',
  saveFailed: '保存失败',
  disabledNotice: '未启用个人押金额度，请先在列表里打开',
  loadFailed: '押金额度加载失败',
} as const

/** 灰字总闸提示（逐字照 island `GATE_HINT_TEXT`）。 */
const GATE_HINT_TEXT =
  '提示：只有该租户 / 该客户端的押金闸已打开时，这里的额度才会真正冻结押金；闸关着时谁都不冻。'

/** 说明文案随开关变（逐字照 island `explanationText`）。 */
function explanationText(enabled: boolean): string {
  return enabled
    ? '开启后按下面填写的个人额度冻结押金；某款游戏没填 = 0 元（该游戏免押）。'
    : '关闭时按全局押金额度（下方「全局额度」一列）；开启后按下面填写的个人额度。'
}

function errText(e: unknown): string {
  return e instanceof Error && e.message.trim() !== '' ? e.message : MSG.saveFailed
}

/** 金额形状判据（非负、最多两位小数；0 合法 = 该游戏免押；island `isValidAmount` 逐字同式）。 */
function isValidAmount(value: string): boolean {
  return /^\d+(\.\d{1,2})?$/.test(value)
}

const quota = ref<DepositQuota | null>(null)
const loadError = ref('')
const enabled = ref(false)
/** 输入框值：game_id（字符串键）→ 输入串；`amount === null` 的游戏起手 = ''。 */
const amountInputs = ref<Record<string, string>>({})
const fillValue = ref('')
const saving = ref(false)
/** 覆盖确认弹层：挂起中的写入（确认后才落输入框）。 */
const pendingFill = ref<Map<number, string> | null>(null)

/** GET 回 `enabled=false` ⇒ 整页只读（以 GET 时刻为准，开层后不再变）。 */
const readonly = computed(() => quota.value !== null && quota.value.enabled === false)

async function load(): Promise<void> {
  try {
    const q = await fetchDepositQuota(props.userId)
    quota.value = q
    enabled.value = q.enabled
    const inputs: Record<string, string> = {}
    for (const g of q.games) inputs[String(g.game_id)] = g.amount === null ? '' : g.amount
    amountInputs.value = inputs
  } catch (e) {
    loadError.value = `${MSG.loadFailed}：${errText(e)}`
  }
}

const overwriteCount = computed(() => {
  if (pendingFill.value === null || quota.value === null) return 0
  return quota.value.games.filter((g) => {
    const next = pendingFill.value!.get(g.game_id)
    const current = (amountInputs.value[String(g.game_id)] ?? '').trim()
    return next !== undefined && current !== '' && current !== next
  }).length
})

/** 把挂起值写进输入框（确认后 / 无冲突时）。 */
function applyPending(): void {
  const values = pendingFill.value
  pendingFill.value = null
  if (values === null) return
  const inputs = { ...amountInputs.value }
  for (const [gameId, value] of values) inputs[String(gameId)] = value
  amountInputs.value = inputs
}

/** 填充共路：先查「会覆盖已有非空且不同值」的行数，有冲突 confirm 一次再写。 */
function fill(values: Map<number, string>): void {
  pendingFill.value = values
  if (overwriteCount.value === 0) {
    applyPending()
    return
  }
}

function onConfirmOverwrite(): void {
  applyPending()
}

/** 「一键填充」：统一额度写进所有输入框；空 / 非法 ⇒ 提示且不动输入框。 */
function onFillUniform(): void {
  if (readonly.value) return
  const raw = fillValue.value.trim()
  if (raw === '') {
    props.notify(MSG.fillEmpty)
    return
  }
  if (!isValidAmount(raw)) {
    props.notify(MSG.invalidAmount)
    return
  }
  const values = new Map<number, string>()
  for (const g of quota.value?.games ?? []) values.set(g.game_id, raw)
  fill(values)
}

/** 「按全局额度填充」：每款游戏把全局额度写进对应输入框。 */
function onFillGlobal(): void {
  if (readonly.value) return
  const values = new Map<number, string>()
  for (const g of quota.value?.games ?? []) values.set(g.game_id, g.global_amount)
  fill(values)
}

async function onSave(): Promise<void> {
  if (saving.value || readonly.value || quota.value === null) return
  const amounts: Record<string, string> = {}
  for (const g of quota.value.games) {
    const key = String(g.game_id)
    const raw = (amountInputs.value[key] ?? '').trim()
    const value = raw === '' ? '0' : raw
    if (!isValidAmount(value)) {
      props.notify(MSG.invalidAmount)
      return
    }
    amounts[key] = value
  }
  saving.value = true
  try {
    await saveDepositQuota(props.userId, { enabled: enabled.value, amounts })
  } catch (e) {
    // 失败原样弹后端 msg，不关层。
    props.notify(errText(e), 'error')
    saving.value = false
    return
  }
  saving.value = false
  props.notify(MSG.saved)
  emit('saved')
}

onMounted(() => {
  void load()
})
</script>

<template>
  <el-dialog
    model-value
    title="押金额度"
    class="cv2-dialog cv2-dialog--md"
    data-v2="dlg-deposit-quota"
    @update:model-value="emit('close')"
  >
    <p v-if="loadError !== ''" class="dq-error" data-v2="dq-load-error">{{ loadError }}</p>
    <template v-else-if="quota !== null">
      <p v-if="readonly" class="dq-error" data-v2="dq-disabled-notice">{{ MSG.disabledNotice }}</p>
      <div class="dq-switchrow">
        <span class="dq-label">启用个人押金额度</span>
        <el-switch v-model="enabled" :disabled="readonly" data-v2="dq-switch" />
      </div>
      <p class="dq-explain" data-v2="dq-explain">{{ explanationText(enabled) }}</p>
      <p class="cv2-field-hint dq-gate" data-v2="dq-gate-hint">{{ GATE_HINT_TEXT }}</p>
      <p class="dq-who" data-v2="dq-who">正在配置：{{ quota.display_name }}（{{ quota.username }}）</p>
      <div class="dq-tools" data-v2="dq-tools">
        统一额度
        <input
          v-model="fillValue"
          class="cv2-input dq-fill-input"
          type="text"
          placeholder="0"
          autocomplete="off"
          data-v2="dq-fill-value"
          :disabled="readonly"
        />
        元
        <button
          type="button"
          class="cv2-btn cv2-btn--tool cv2-btn--sm cv2-btn--commit"
          data-v2="dq-fill-uniform"
          :disabled="readonly"
          @click="onFillUniform"
        >一键填充</button>
        <button
          type="button"
          class="cv2-btn cv2-btn--tool cv2-btn--sm cv2-btn--commit"
          data-v2="dq-fill-global"
          :disabled="readonly"
          @click="onFillGlobal"
        >按全局额度填充</button>
      </div>
      <table class="cv2-table dq-table" data-v2="dq-table">
        <thead>
          <tr>
            <th style="width: 40%">游戏</th>
            <th style="width: 30%" class="cv2-th-center">全局额度</th>
            <th style="width: 30%" class="cv2-th-center">个人额度</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="g in quota.games" :key="g.game_id" :data-v2-dq-row="g.game_id">
            <td>{{ g.game_name }}</td>
            <td class="cv2-td-center cv2-td-num">{{ g.global_amount }} 元</td>
            <td class="cv2-td-center">
              <input
                v-model="amountInputs[String(g.game_id)]"
                class="cv2-input dq-amount-input"
                type="text"
                placeholder="0"
                autocomplete="off"
                :data-v2-dq-amount="g.game_id"
                :disabled="readonly"
              />
            </td>
          </tr>
        </tbody>
      </table>
      <div class="cv2-dlg-actions">
        <button
          type="button"
          class="cv2-btn cv2-btn--primary cv2-btn--commit"
          data-v2="dq-save"
          :disabled="readonly || saving"
          @click="onSave"
        >保存</button>
      </div>
    </template>

    <!-- 覆盖确认（有冲突时 confirm 一次；逐字文案 + N 款游戏数那句） -->
    <el-dialog
      :model-value="pendingFill !== null"
      title="确认覆盖"
      class="cv2-dialog cv2-dialog--sm"
      width="380px"
      data-v2="dq-overwrite-confirm"
      @update:model-value="pendingFill = null"
    >
      <p class="cv2-dlg-text">
        {{ MSG.confirmOverwrite }}（{{ overwriteCount }} 款游戏的个人额度会被覆盖）
      </p>
      <div class="cv2-dlg-actions">
        <button type="button" class="cv2-btn cv2-btn--quiet" @click="pendingFill = null">取消</button>
        <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="dq-overwrite-ok" @click="onConfirmOverwrite">确定</button>
      </div>
    </el-dialog>
  </el-dialog>
</template>

<style scoped>
.dq-error {
  margin: 0 0 10px;
  color: var(--tt2-st-error);
}
.dq-switchrow {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}
.dq-label {
  font-size: 13px;
}
.dq-explain {
  margin: 0 0 8px;
  font-size: 13px;
  color: var(--tt2-ink);
}
.dq-gate {
  margin: 0 0 8px;
}
.dq-who {
  margin: 0 0 8px;
  font-size: 13px;
}
.dq-tools {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 13px;
}
.dq-fill-input {
  width: 100px;
}
.dq-table {
  margin-top: 4px;
}
.dq-amount-input {
  width: 110px;
  text-align: center;
}
</style>
