<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「自动发消息到微信群设置」v2（`/v2/wechat-group-push`；系统维护批次 B 波，计划 §3 页 10）。
 * 旧页 = island 页 `layui-island/pages/system-orderpushgroupmsgset.ts`（自建页，文案权威在本页源码）。
 * 重建为炭火页：工具条（刷新列表→已刷新 / 新增一个设置红钮 / 共 N 条橙字）+ 9 列表
 * （发送时机三色改炭火 token：橙→amber、红→error、绿→doing；渠道显示 Webhook host 省略；
 * 启用状态行内开关失败重载回滚；操作列 测试 / 编辑 / 删除）+ 大编辑弹窗（渠道 radio、
 * 4 档发送时机带逐字 hint、@全员、消息类型、games checkbox 空态逐字）+ 校验四句 +
 * 删除确认逐字（按钮 确认删除/点错了）。取数固定 page_size=200（ORDER_PUSH_PAGE_SIZE），
 * 超量提示逐字。TIMING_LABELS / timingHint 从共享 api/orderPush.ts 取（api 模块非 island 页）；
 * 其余常量本地重写。⛔ 不 import island 页模块。
 */
import { computed, onMounted, ref } from 'vue'
import { fetchAllGames, type ConfigRow } from '@/api/config'
import {
  ORDER_PUSH_PAGE_SIZE,
  TIMING_LABELS,
  createOrderPushSetting,
  deleteOrderPushSetting,
  listOrderPushSettings,
  testOrderPushSetting,
  timingHint,
  updateOrderPushSetting,
  type OrderPushSetting,
  type PushChannel,
  type PushMsgType,
  type PushTiming,
} from '@/api/orderPush'

/* ── 逐字文案与常量（出处 = island 页同语义常量；本地重写不引模块）────────────── */

/** 页面上的渠道文案（与后端 `Channel` 的 label 逐字一致，island CHANNEL_LABELS）。 */
const CHANNEL_LABELS: Record<PushChannel, string> = {
  wecom: '企业微信群机器人',
  custom: '自定义 Webhook',
}
const MSG_TYPE_LABELS: Record<PushMsgType, string> = {
  markdown: 'Markdown',
  text: '文本',
}
const TIMINGS: PushTiming[] = [0, 10, 20, 30]
/** 弹窗里随渠道变化的两句提示（island channelHint 逐字）。 */
const CHANNEL_HINTS: Record<PushChannel, string> = {
  wecom: '企微群里「添加群机器人」后复制的 Webhook 地址；消息按企微原生 text / markdown 格式发送。',
  custom: '任意 HTTP(S) 地址。我们发通用 JSON（content + text/markdown 两套片段），由你方自行适配。',
}
/** 表格空态 / 超量 / 刷新成功 / 测试两句（island 逐字）。 */
const EMPTY_TEXT = '还没有配置过推送规则，点上方「新增一个设置」'
const REFRESHED_TEXT = '已刷新'
const TESTING_TEXT = '正在发送测试消息…'
const TEST_DONE_TEXT = '测试消息已发送'
/** games checkbox 空态（island dialogHtml 逐字）。 */
const GAMES_EMPTY_TEXT = '还没有游戏，请先到「游戏管理」新增'
/** 校验四句（island validatePushForm 逐字，顺序同）。 */
function validatePushForm(input: { webhookUrl: string; games: readonly number[]; timing: number | null }): string | null {
  const url = (input.webhookUrl ?? '').trim()
  if (!url) return 'Webhook 地址不能为空'
  if (!/^https?:\/\/[^\s]+$/i.test(url)) return 'Webhook 地址必须是 http:// 或 https:// 开头的完整地址'
  if (input.games.length === 0) return '至少要选择一个游戏'
  if (input.timing === null || !TIMINGS.includes(input.timing as PushTiming)) return '请选择发送时机'
  return null
}

/** 发送时机三色 → 炭火 token（橙 #ff6a00→amber、红 #FF5722→error、绿 #219227→doing；
 * 色值一律走 var，类在本页 scoped）。 */
function timingClass(timing: PushTiming): string {
  if (timing === 10) return 'wp-timing-amber'
  if (timing === 20) return 'wp-timing-error'
  return 'wp-timing-doing'
}

/** 表格里 Webhook 只显示 host + 省略号（URL 里带 key，等于凭据；island webhookHost 同式）。 */
function webhookHost(url: string): string {
  const match = /^https?:\/\/([^/?#]+)/i.exec(url ?? '')
  return match ? `${match[1]}/…` : '—'
}

/** 规则里勾的游戏名（按全量游戏顺序；查不到 id 就跳过；island gamesLabel 同式）。 */
const games = ref<ConfigRow[]>([])

function gamesLabel(ids: readonly number[]): string {
  const names = games.value.filter((g) => ids.includes(g.id)).map((g) => g.name)
  return names.length > 0 ? names.join('、') : '—'
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

/* ── 提示（页面内自绘 toast；info 1500ms / error 6000ms）────────────────────── */

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

/* ── 列表（page_size=200 封顶；超量提示逐字）────────────────────────────────── */

const rows = ref<OrderPushSetting[]>([])
const total = ref(0)
const loading = ref(false)

async function reload(): Promise<boolean> {
  loading.value = true
  try {
    const page = await listOrderPushSettings({ page: 1, page_size: ORDER_PUSH_PAGE_SIZE })
    rows.value = page.items
    total.value = page.total
    if (page.total > page.items.length) {
      showToast(`规则太多，只显示了前 ${page.items.length} 条（共 ${page.total} 条）`)
    }
    return true
  } catch (e) {
    showToast(errText(e), 'error')
    return false
  } finally {
    loading.value = false
  }
}

function onRefresh(): void {
  void reload().then((ok) => {
    if (ok) showToast(REFRESHED_TEXT)
  })
}

/* ── 行内开关（成功就地改 + 已启用/已停用；失败重载回滚照旧）──────────────────── */

async function onActiveSwitch(row: OrderPushSetting): Promise<void> {
  try {
    await updateOrderPushSetting(row.id, { is_active: row.is_active })
    showToast(row.is_active ? '已启用' : '已停用')
  } catch (e) {
    showToast(errText(e), 'error')
    await reload() // 失败重载回滚（island toggle 失败分支同口径）
  }
}

/* ── 测试（两句逐字；失败原样后端 msg）────────────────────────────────────── */

async function onTest(row: OrderPushSetting): Promise<void> {
  showToast(TESTING_TEXT)
  try {
    await testOrderPushSetting(row.id)
    showToast(TEST_DONE_TEXT)
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

/* ── 新增 / 编辑弹窗（渠道 radio、4 档时机、@全员、消息类型、games checkbox）────── */

type PushForm = {
  id: number | null
  name: string
  channel: PushChannel
  webhookUrl: string
  gameIds: number[]
  timing: number | null
  callAll: boolean
  msgType: PushMsgType
  remark: string
}
const pushDlg = ref<PushForm | null>(null)
const saving = ref(false)

function openPushForm(row: OrderPushSetting | null): void {
  pushDlg.value = {
    id: row?.id ?? null,
    name: row?.name ?? '',
    channel: row?.channel ?? 'wecom',
    webhookUrl: row?.webhook_url ?? '',
    gameIds: row ? [...row.games] : [],
    timing: row ? row.timing : 0,
    callAll: row?.call_all === true,
    msgType: row?.msg_type ?? 'markdown',
    remark: row?.remark ?? '',
  }
}

/** 提交（校验四句顺序照旧；is_active 编辑保原值、新增默认 true）。 */
async function submitPush(): Promise<void> {
  const dlg = pushDlg.value
  if (dlg === null || saving.value) return
  const invalid = validatePushForm({ webhookUrl: dlg.webhookUrl, games: dlg.gameIds, timing: dlg.timing })
  if (invalid !== null) {
    showToast(invalid)
    return
  }
  saving.value = true
  const input = {
    name: dlg.name.trim(),
    channel: dlg.channel,
    webhook_url: dlg.webhookUrl.trim(),
    games: dlg.gameIds,
    timing: dlg.timing as PushTiming,
    msg_type: dlg.msgType,
    call_all: dlg.callAll,
    remark: dlg.remark.trim(),
    is_active: dlg.id !== null ? (rows.value.find((r) => r.id === dlg.id)?.is_active ?? true) : true,
  }
  try {
    if (dlg.id !== null) await updateOrderPushSetting(dlg.id, input)
    else await createOrderPushSetting(input)
  } catch (e) {
    saving.value = false
    showToast(errText(e), 'error')
    return
  }
  saving.value = false
  pushDlg.value = null
  showToast('保存成功')
  await reload()
}

/* ── 删除确认（文案逐字：有名字带「」，按钮 确认删除/点错了）──────────────────── */

const delDlg = ref<OrderPushSetting | null>(null)

const delConfirmText = computed(() => {
  const row = delDlg.value
  if (row === null) return ''
  return `是否确认删除这个设置${row.name ? `「${row.name}」` : ''}？`
})

async function confirmDel(): Promise<void> {
  const row = delDlg.value
  if (row === null) return
  delDlg.value = null
  try {
    await deleteOrderPushSetting(row.id)
    showToast('删除成功')
    await reload()
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

/** 空态 colspan = 9 列。 */
const COLSPAN_ALL = 9

onMounted(() => {
  void (async () => {
    // 游戏字典取不到：表格「限定游戏」列显示 —，弹窗空态逐字（island 同口径降级）。
    games.value = await fetchAllGames().catch((): ConfigRow[] => [])
    await reload()
  })()
})
</script>

<template>
  <div class="cv2-page" data-v2-page="wechat-group-push">
    <!-- 工具条：刷新列表 + 新增一个设置（红钮照旧）+ 共 N 条（橙字照旧） -->
    <div class="cv2-toolbar" data-v2="toolbar">
      <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--find" data-v2="refresh" :disabled="loading" @click="onRefresh">刷新列表</button>
      <button type="button" class="cv2-btn cv2-btn--commit" data-v2="add" @click="openPushForm(null)">新增一个设置</button>
      <span class="wp-total" data-v2="total">共 {{ total }} 条</span>
    </div>

    <!-- 9 列表（列头照旧 buildCols；发送时机三色 token；启用状态列头 title 逐字） -->
    <section class="cv2-panel cv2-table-panel" aria-label="推送规则列表">
      <table class="cv2-table">
        <colgroup>
          <col style="width: 120px" />
          <col style="width: 200px" />
          <col style="width: 150px" />
          <col />
          <col style="width: 100px" />
          <col style="width: 90px" />
          <col style="width: 120px" />
          <col style="width: 105px" />
          <col style="width: 200px" />
        </colgroup>
        <thead>
          <tr>
            <th>规则名称</th>
            <th class="cv2-th-center">发送时机</th>
            <th class="cv2-th-center">渠道</th>
            <th>限定游戏</th>
            <th class="cv2-th-center">消息类型</th>
            <th class="cv2-th-center">呼叫全员</th>
            <th>追加说明</th>
            <th class="cv2-th-center">
              <span title="绿色代表启用时" data-v2="active-tip">启用状态</span>
            </th>
            <th class="cv2-th-center">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id" class="cv2-row" :data-v2-row="row.id">
            <td :title="row.name">{{ row.name || '—' }}</td>
            <td class="cv2-td-center">
              <span :class="timingClass(row.timing)" title="发送时机" :data-v2-timing="row.timing">{{ TIMING_LABELS[row.timing] ?? String(row.timing) }}</span>
            </td>
            <td class="cv2-td-center">
              <span>{{ CHANNEL_LABELS[row.channel] ?? row.channel }}</span>
              <span class="wp-host" :title="row.webhook_url" :data-v2-host="row.id">{{ webhookHost(row.webhook_url) }}</span>
            </td>
            <td class="wp-games" :title="gamesLabel(row.games)" :data-v2-games="row.id">{{ gamesLabel(row.games) }}</td>
            <td class="cv2-td-center">{{ MSG_TYPE_LABELS[row.msg_type] ?? row.msg_type }}</td>
            <td class="cv2-td-center">{{ row.call_all ? '是' : '否' }}</td>
            <td class="wp-remark" :title="row.remark">{{ row.remark || '' }}</td>
            <td class="cv2-td-center">
              <el-switch v-model="row.is_active" :data-v2-active-switch="row.id" @change="onActiveSwitch(row)" />
            </td>
            <td class="cv2-td-center wp-actions">
              <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--sm cv2-btn--find" title="发一条测试消息" :data-v2-test="row.id" @click="onTest(row)">测试</button>
              <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--sm cv2-btn--commit" title="编辑参数" :data-v2-edit="row.id" @click="openPushForm(row)">编辑</button>
              <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--sm" title="删除设置" :data-v2-del="row.id" @click="delDlg = row">删除</button>
            </td>
          </tr>
          <tr v-if="rows.length === 0">
            <td :colspan="COLSPAN_ALL" class="cv2-empty" data-v2="empty">
              <template v-if="loading">正在加载…</template>
              <template v-else>{{ EMPTY_TEXT }}</template>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 编辑弹窗（标题照旧 编辑参数/新增一个设置；hint 随选项变化） -->
    <el-dialog
      :model-value="pushDlg !== null"
      :title="pushDlg !== null && pushDlg.id !== null ? '编辑参数' : '新增一个设置'"
      class="cv2-dialog cv2-dialog--lg"
      destroy-on-close
      data-v2="dlg-push"
      @update:model-value="pushDlg = null"
    >
      <template v-if="pushDlg !== null">
        <div class="cv2-field">
          <span class="cv2-field-label">规则名称</span>
          <el-input v-model="pushDlg.name" :maxlength="50" placeholder="不填也行，多条规则时用来区分" data-v2="push-name" />
        </div>
        <div class="cv2-field">
          <span class="cv2-field-label">渠道类型</span>
          <div class="cv2-dlg-radios wp-radios" data-v2="push-channel">
            <label class="cv2-radio">
              <input v-model="pushDlg.channel" type="radio" name="v2-push-channel" value="wecom" />
              企业微信群机器人
            </label>
            <label class="cv2-radio">
              <input v-model="pushDlg.channel" type="radio" name="v2-push-channel" value="custom" />
              自定义 Webhook
            </label>
          </div>
          <p class="wp-hint" data-v2="channel-hint">{{ CHANNEL_HINTS[pushDlg.channel] }}</p>
        </div>
        <div class="cv2-field">
          <span class="cv2-field-label">Webhook 地址</span>
          <el-input v-model="pushDlg.webhookUrl" :maxlength="500" placeholder="企微群机器人地址，或你方自己的 HTTP(S) 接收地址" data-v2="push-webhook" />
        </div>
        <div class="cv2-field">
          <span class="cv2-field-label">限定游戏</span>
          <div class="wp-games-box" data-v2="push-games">
            <template v-if="games.length > 0">
              <el-checkbox-group v-model="pushDlg.gameIds">
                <el-checkbox v-for="g in games" :key="g.id" :value="g.id">{{ g.name }}</el-checkbox>
              </el-checkbox-group>
            </template>
            <template v-else>{{ GAMES_EMPTY_TEXT }}</template>
          </div>
        </div>
        <div class="cv2-field">
          <span class="cv2-field-label">发送时机</span>
          <div class="cv2-dlg-radios wp-radios" data-v2="push-timing">
            <label v-for="t in TIMINGS" :key="t" class="cv2-radio">
              <input v-model="pushDlg.timing" type="radio" name="v2-push-timing" :value="t" />
              {{ TIMING_LABELS[t] }}
            </label>
          </div>
          <p class="wp-hint" :class="{ 'wp-hint--warn': pushDlg.timing === 20 }" data-v2="timing-hint">
            {{ pushDlg.timing === null ? '' : timingHint(pushDlg.timing as PushTiming) }}
          </p>
        </div>
        <div class="cv2-field">
          <span class="cv2-field-label">是否@全员</span>
          <div class="cv2-dlg-radios wp-radios" data-v2="push-callall">
            <label class="cv2-radio">
              <input v-model="pushDlg.callAll" type="radio" name="v2-push-callall" :value="true" />
              需要
            </label>
            <label class="cv2-radio">
              <input v-model="pushDlg.callAll" type="radio" name="v2-push-callall" :value="false" />
              不需要
            </label>
          </div>
          <p class="wp-hint">Markdown 模式下企微机器人不支持 @全员；要真提醒请选文本模式</p>
        </div>
        <div class="cv2-field">
          <span class="cv2-field-label">消息类型</span>
          <div class="cv2-dlg-radios wp-radios" data-v2="push-msgtype">
            <label class="cv2-radio">
              <input v-model="pushDlg.msgType" type="radio" name="v2-push-msgtype" value="markdown" />
              Markdown 模式
            </label>
            <label class="cv2-radio">
              <input v-model="pushDlg.msgType" type="radio" name="v2-push-msgtype" value="text" />
              文本模式
            </label>
          </div>
          <p class="wp-hint">自定义渠道两种都会收到，按你方适配器取用</p>
        </div>
        <div class="cv2-field">
          <span class="cv2-field-label">追加说明</span>
          <el-input v-model="pushDlg.remark" :maxlength="100" data-v2="push-remark" />
        </div>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="pushDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="push-save" :disabled="saving" @click="void submitPush()">保存设置</button>
        </div>
      </template>
    </el-dialog>

    <!-- 删除确认（文案逐字 + 按钮 确认删除/点错了） -->
    <el-dialog
      :model-value="delDlg !== null"
      title="提示"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-del"
      @update:model-value="delDlg = null"
    >
      <p class="cv2-dlg-text">{{ delConfirmText }}</p>
      <p class="cv2-dlg-text">删除后它不会再发任何消息。</p>
      <div class="cv2-dlg-actions">
        <button type="button" class="cv2-btn cv2-btn--quiet" @click="delDlg = null">点错了</button>
        <button type="button" class="cv2-btn cv2-btn--danger" data-v2="del-ok" @click="void confirmDel()">确认删除</button>
      </div>
    </el-dialog>

    <!-- 炭火 toast -->
    <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
      {{ toast.text }}
    </div>
  </div>
</template>

<style scoped>
/* 本页新增的形状收在这里；色一律走 carbon.css 的 .ttv2 token（类或 var 引用，⛔ 不写裸色值）。 */

/* 发送时机三色（旧页字面橙/红/绿三色的炭火 token 同档：amber/error/doing）。 */
.wp-timing-amber {
  color: var(--tt2-tint-amber);
}

.wp-timing-error {
  color: var(--tt2-st-error);
}

.wp-timing-doing {
  color: var(--tt2-st-doing);
}

/* 工具条「共 N 条」= 原版橙（--tt2-summary-orange）。 */
.wp-total {
  margin-left: 8px;
  color: var(--tt2-summary-orange);
  font-size: 13px;
}

/* 渠道格第二行：Webhook host 省略（灰）。 */
.wp-host {
  display: block;
  color: var(--tt2-dim);
  font-size: 12px;
}

/* 限定游戏 / 追加说明列长文本单行截断（44px 行高家族口径）。 */
.wp-games,
.wp-remark {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.wp-actions {
  white-space: nowrap;
}

.wp-actions .cv2-btn--sm + .cv2-btn--sm {
  margin-left: 6px;
}

/* 面板限高内部滚动。 */
.cv2-table-panel {
  max-height: calc(100vh - 330px);
  min-height: 260px;
  overflow: auto;
}

/* ── 编辑弹窗。── */
.wp-radios {
  flex-wrap: wrap;
  row-gap: 10px;
  min-height: 34px;
  padding: 6px 12px;
  border: 1px solid var(--tt2-filter-line);
  box-sizing: border-box;
  background: var(--tt2-panel);
}

.wp-radios .cv2-radio {
  white-space: nowrap;
}

/* 弹窗里的随选项提示与警色（timing 20 那档「配了也不生效」）。 */
.wp-hint {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--tt2-dim);
  line-height: 22px;
}

.wp-hint--warn {
  color: var(--tt2-st-error);
}

/* games checkbox 组（island push-games 的炭火版：限高滚动）。 */
.wp-games-box {
  max-height: 130px;
  overflow: auto;
  padding: 6px 10px;
  border: 1px solid var(--tt2-filter-line);
  font-size: 13px;
}

.wp-games-box :deep(.el-checkbox) {
  margin-right: 16px;
  height: 26px;
}

/* 弹层表单行距（尺寸非色值）。 */
.cv2-dialog .cv2-field {
  grid-template-columns: 104px minmax(0, 1fr);
  gap: 0;
  align-items: stretch;
  margin-bottom: 12px;
  overflow: hidden;
  border-radius: 4px;
}
.cv2-dialog .cv2-field-label { display: flex; align-items: center; padding: 0 10px; border: 1px solid var(--tt2-filter-line); border-right: 0; background: var(--tt2-filter-label-bg); color: var(--tt2-dim); font-size: 13px; font-weight: 400; }
.cv2-dialog .cv2-field :deep(.el-input__wrapper) { border-radius: 0; }
.cv2-dialog .cv2-field :deep(input::placeholder) { color: var(--tt2-filter-ph); }
</style>
