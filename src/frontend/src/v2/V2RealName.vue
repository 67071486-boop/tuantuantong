<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「人员实名记录与配置」v2（`/v2/real-name`；系统人员批次 2026-11-05）。
 *
 * ## 旧页是什么、这里重建什么
 *
 * 旧页 = island 复刻页 `layui-island/pages/user-rnamecertlogandsetting.ts`（路由
 * `system-real-name`，对标 `/User/RnameCertLogAndSetting`）。功能照旧页重建为炭火页：
 * 筛选行（实名时间起止【默认近 30 天】/ 实名结果 / 三个搜索框 + 搜索）→ 工具条 3 颗
 * → 主表 11 列（勾选列 + 10 命名列，无操作列）→ 分页（limit 20 仅一档）。
 *
 * - **两段式取数**（照旧页）：首屏只发 `sdate`/`edate`；点「搜索」或「刷新列表」才带
 *   全部 6 参（`listRealNameLogs(params, includeFilters)` 的两档）。
 * - 年龄列 <18 或 >45 红字 + title「年龄异常，请谨慎录用」（null 不算异常）。
 * - 认证图片：先弹法律提示确认（原文逐字），确认后无图 toast「暂无认证图片」、有图开大图。
 * - 工具条「购买实名认证套餐包(当前余量0次)」运行期改写为「配置实名资源包(当前余量N次)」
 *   （`buyButtonLabel`，N 来自 `realname-quota`；0 / null / 失败三态提示逐字照旧页，
 *   余量读数失败不拖垮整页）。
 * - 设置弹窗（md）：四项逐字（三颗 radio 默认否、两个年龄默认 0=不限制）+ 运行期两态
 *   提示逐字；开层先 GET 回填、失败不开层；保存 PATCH 五键全带，失败不关层。
 * - 资源包弹窗（md）：现有密钥包（掩码列表）+ 添加密钥包表单；**secret 框任何响应不回显、
 *   成功失败都在 finally 清空**；失败弹后端 msg。
 *
 * ## 数据：零新增端点
 *
 * 全部走 `api/realname.ts` 既有 6 端点（`realname-logs` / `realname-setting` 读+写 /
 * `realname-quota` / `realname-packages` 读+写）。逐字文案出处 = island 页同名常量与
 * 证据件 `replica/user-rnamecertlogandsetting.html`，本地重写不引 island 模块（不把
 * layui 装载链拉进 v2 模块图）。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { formatCreatedAt } from '@/api/auditLogs'
import {
  REALNAME_STATUS_ALL,
  REALNAME_STATUS_FAIL,
  REALNAME_STATUS_PASS,
  buyButtonLabel,
  createRealNamePackage,
  getRealNameQuota,
  getRealNameSetting,
  listRealNameLogs,
  listRealNamePackages,
  updateRealNameSetting,
  type RealNameLogRow,
  type RealNamePackageRow,
} from '@/api/realname'

/* ── 文案（出处见 island 页同名常量；逐字照抄，⛔ 改写）──────────────────── */

/** 认证图片的法律提示原文（island `CERT_IMAGE_NOTICE`，对标 JS L168 逐字）。 */
const CERT_IMAGE_NOTICE =
  '郑重提示：根据相关法律法规，查看的认证图仅用于核实当事人真实性，切不可随意将图片转发、传播、滥用，甚至用于人身攻击、侮辱、诋毁以及非法使用，否则以此引发的所有后果由您负责！'
const CERT_IMAGE_CONFIRM_TEXT = '我已知晓，保证不滥用'
const CERT_IMAGE_CLOSE_TEXT = '关闭'
/** 图片空值时的提示（island `CERT_IMAGE_EMPTY_TEXT`）。 */
const CERT_IMAGE_EMPTY_TEXT = '暂无认证图片'
/** 认证图片格的 title（island `CERT_IMAGE_TITLE`，对标 JS L97 逐字）。 */
const CERT_IMAGE_TITLE = '认证图片只保存1年'
/** 身份证号列的 title（island `ID_CARD_TITLE`，对标 JS L49 逐字）。 */
const ID_CARD_TITLE = '不予完整展示，完整证件号请进入系统用户/用户管理，编辑子账号资料里查看'
/** 年龄异常格的 title（island `ABNORMAL_AGE_TIPS`，对标 JS L60 逐字）。 */
const ABNORMAL_AGE_TIPS = '年龄异常，请谨慎录用'
/** 设置弹窗底部两态提示（island `SETTINGS_NOT_APPLIED_NOTE`，D-0245 后的现行文案）。 */
const SETTINGS_NOT_APPLIED_NOTE = '「未实名不允许接单」已生效；「结算要求实名」暂仅保存、尚未启用'
/** 余量读到 0 的提示（island `QUOTA_EXHAUSTED_TEXT` 逐字；`0` 是合法读数，与「读不到」两态必须能区分）。 */
const QUOTA_EXHAUSTED_TEXT =
  '本租户实名认证余量已用完（当前余量 0 次），可在「配置实名资源包」中添加新密钥'
/** 余量读不到（null）的提示（island mount 同句逐字）。 */
const QUOTA_NULL_TEXT = '暂时读不到本租户的实名认证剩余次数，请稍后重试'
/** 余量请求失败的兜底文案。 */
const QUOTA_FAILED_TEXT = '实名认证剩余次数读取失败'
/** 添加密钥包成功提示（island `PKG_CREATED_TEXT`）。 */
const PKG_CREATED_TEXT = '密钥包已添加'
/** 设置读取失败的兜底文案（island openSettings 同句）。 */
const SETTINGS_LOAD_FAILED_TEXT = '实名认证设置读取失败'
/** 购买按钮的证据件字面量（quota 回来前显示；回来后运行期改写，同旧页行为）。 */
const BUY_LABEL_EVIDENCE = '购买实名认证套餐包(当前余量0次)'

/** 错误对象的可显示文本。**不自造文案**：`request()` 抛出的 message 就是后端 `msg` 原文。 */
function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

/* ── 提示（炭火 toast，info 1500ms / error 6000ms）──────────────────────── */

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

/* ── 筛选（控件名照证据件；默认日期 = 近 30 天，业务方拍板偏离证据件死值）──────── */

function pad2(v: number): string {
  return String(v).padStart(2, '0')
}
function toDateText(d: Date): string {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
}
/** 今天 − 29 天 ~ 今天（`dayRangeDaysAgo(29)` 的本地重写；⛔ 不引 island 模块）。 */
const TODAY = toDateText(new Date())
const TODAY_MINUS_29 = (() => {
  const d = new Date()
  d.setDate(d.getDate() - 29)
  return toDateText(d)
})()

const filters = reactive({
  sDate: TODAY_MINUS_29,
  eDate: TODAY,
  certStatus: REALNAME_STATUS_ALL,
  key1: '',
  key2: '',
  key3: '',
})

/* ── 取数（两段式：首屏只发日期；搜索/刷新才带全部 6 参）────────────────────── */

const PAGE_LIMIT = 20
const rows = ref<RealNameLogRow[]>([])
const total = ref(0)
const loading = ref(false)
const page = ref(1)
/** 点过「搜索 / 刷新」之后为真；翻页沿用当前段（首屏未搜索时翻页只发日期，照旧页 where 语义）。 */
let filtersInQuery = false

async function load(includeFilters: boolean): Promise<void> {
  loading.value = true
  try {
    const res = await listRealNameLogs(
      {
        sdate: filters.sDate.trim(),
        edate: filters.eDate.trim(),
        status: filters.certStatus,
        key1: filters.key1,
        key2: filters.key2,
        key3: filters.key3,
        page: page.value,
        limit: PAGE_LIMIT,
      },
      includeFilters,
    )
    rows.value = res.items
    total.value = res.total
  } catch (e) {
    rows.value = []
    total.value = 0
    showToast(errText(e), 'error')
  } finally {
    loading.value = false
  }
}

/** 搜索 / 刷新：同一条路（照旧页两者都调 `reloadOrderList`）——带全筛选、回第 1 页。 */
function onSearch(): void {
  filtersInQuery = true
  page.value = 1
  void load(true)
}

function onPageChange(p: number): void {
  page.value = p
  void load(filtersInQuery)
}

onMounted(() => {
  void load(false)
  // 余量：运行期改写购买按钮文案；0 / null / 失败三态提示互斥（`0` 是合法读数，判据 is not null）。
  void getRealNameQuota().then(
    (quota) => {
      buyLabel.value = buyButtonLabel(quota.remaining)
      if (quota.remaining === 0) showToast(QUOTA_EXHAUSTED_TEXT)
      else if (quota.remaining === null) showToast(QUOTA_NULL_TEXT)
    },
    (e: Error) => {
      showToast(e.message || QUOTA_FAILED_TEXT, 'error')
    },
  )
})

/* ── 表格（11 列 = 勾选列 + 10 命名列；徽章映射照 island certMethod/certResultBadge）── */

/** 年龄红字判据（island `isAbnormalAge` 同语义：null / undefined 不算异常）。 */
function isAbnormalAge(age: number | null): boolean {
  if (age === null) return false
  return age < 18 || age > 45
}

/** 认证方式：basic = 绿签「基础认证」；其余（含未知值）= 橙签「人脸加强」（默认橙那支照抄）。 */
function certMethodText(v: string): string {
  return v === 'basic' ? '基础认证' : '人脸加强'
}
function certMethodClass(v: string): string {
  return v === 'basic' ? 'cv2-st--doing' : 'cv2-st--paused'
}
/** 认证结果：pass = 绿签「通过认证」；其余 = 灰签「未通过认证」。 */
function certResultText(v: string): string {
  return v === 'pass' ? '通过认证' : '未通过认证'
}
function certResultClass(v: string): string {
  return v === 'pass' ? 'cv2-st--settled' : 'cv2-st--unassigned'
}

/* ── 勾选（旧页 layui 勾选列的形态；本页无批量动作，同旧页——勾选无消费方）────── */

const selectedIds = ref<number[]>([])

function toggleRow(row: RealNameLogRow): void {
  const i = selectedIds.value.indexOf(row.id)
  if (i === -1) selectedIds.value.push(row.id)
  else selectedIds.value.splice(i, 1)
}

const allOnPageSelected = computed(
  () => rows.value.length > 0 && rows.value.every((row) => selectedIds.value.includes(row.id)),
)

function toggleAllOnPage(): void {
  if (allOnPageSelected.value) {
    selectedIds.value = []
    return
  }
  selectedIds.value = rows.value.map((row) => row.id)
}

/* ── 认证图片：先弹法律提示确认 → 无图提示 / 有图开大图 ─────────────────────────── */

const certDlg = ref<{ img: string; userName: string } | null>(null)
const imgDlg = ref<{ img: string; userName: string } | null>(null)

function onCertImageClick(row: RealNameLogRow): void {
  certDlg.value = { img: row.cert_image ?? '', userName: row.user_name ?? '' }
}

/** 「我已知晓，保证不滥用」：关提示层 → 无图 toast「暂无认证图片」、有图开大图。 */
function confirmCertNotice(): void {
  const dlg = certDlg.value
  certDlg.value = null
  if (dlg === null) return
  if (dlg.img === '') {
    showToast(CERT_IMAGE_EMPTY_TEXT)
    return
  }
  imgDlg.value = { img: dlg.img, userName: dlg.userName }
}

/* ── 设置弹窗（md）：先 GET 回填再开层（失败不开层）；保存 PATCH 五键全带 ─────────── */

const settingsDlg = ref(false)
const settingsLoading = ref(false)
const settingsSaving = ref(false)
const settings = reactive({
  enabled: false,
  block_claim_without_cert: false,
  require_cert_for_settlement: false,
  min_age: 0,
  max_age: 0,
})

/** 年龄输入 → 整数：空串 / 非法输入都归 0（`0` = 不限制，是合法值；island `ageInputToNumber` 同语义）。 */
function toAgeNumber(v: number | string): number {
  const n = typeof v === 'number' ? v : Number(String(v).trim())
  return Number.isFinite(n) ? n : 0
}

async function openSettings(): Promise<void> {
  settingsLoading.value = true
  try {
    const setting = await getRealNameSetting()
    settings.enabled = setting.enabled
    settings.block_claim_without_cert = setting.block_claim_without_cert
    settings.require_cert_for_settlement = setting.require_cert_for_settlement
    settings.min_age = setting.min_age
    settings.max_age = setting.max_age
    settingsDlg.value = true
  } catch (e) {
    showToast(e instanceof Error ? e.message : SETTINGS_LOAD_FAILED_TEXT, 'error')
  } finally {
    settingsLoading.value = false
  }
}

async function saveSettings(): Promise<void> {
  settingsSaving.value = true
  try {
    // 五键全带：PATCH 是部分更新，缺席 = 不改；这是一份完整表单，用户看到什么就提交什么。
    await updateRealNameSetting({
      enabled: settings.enabled,
      block_claim_without_cert: settings.block_claim_without_cert,
      require_cert_for_settlement: settings.require_cert_for_settlement,
      min_age: toAgeNumber(settings.min_age),
      max_age: toAgeNumber(settings.max_age),
    })
    showToast('保存成功')
    settingsDlg.value = false
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    settingsSaving.value = false
  }
}

/* ── 资源包弹窗（md）：先取数铺列表再开层；secret 框 finally 清空不回显 ───────────── */

const packagesDlg = ref(false)
const packagesOpening = ref(false)
const pkgRows = ref<RealNamePackageRow[] | null>(null)
const pkgError = ref('')
const pkgLoading = ref(false)
const pkgSubmitting = ref(false)
const pkgForm = reactive({ keyId: '', keySecret: '', totalQuota: '', expiresAt: '', remark: '' })

/** 购买按钮文案（quota 回来前 = 证据件字面量，回来后运行期改写）。 */
const buyLabel = ref(BUY_LABEL_EVIDENCE)

/** 有效期列：null =「不过期」，否则 `YYYY-MM-DD HH:mm`（island `pkgExpiresText` 同语义）。 */
function pkgExpiresText(value: string | null): string {
  if (value === null) return '不过期'
  return value.replace('T', ' ').slice(0, 16)
}

async function reloadPackages(): Promise<void> {
  pkgLoading.value = true
  try {
    pkgRows.value = await listRealNamePackages()
    pkgError.value = ''
  } catch (e) {
    pkgRows.value = null
    pkgError.value = errText(e)
  } finally {
    pkgLoading.value = false
  }
}

async function openPackages(): Promise<void> {
  if (packagesOpening.value) return
  packagesOpening.value = true
  try {
    await reloadPackages()
    packagesDlg.value = true
  } finally {
    packagesOpening.value = false
  }
}

async function submitPackage(): Promise<void> {
  if (pkgSubmitting.value) return
  pkgSubmitting.value = true
  try {
    await createRealNamePackage({
      key_id: pkgForm.keyId.trim(),
      key_secret: pkgForm.keySecret,
      total_quota: Number(pkgForm.totalQuota),
      ...(pkgForm.expiresAt.trim() === '' ? {} : { expires_at: pkgForm.expiresAt.trim() }),
      ...(pkgForm.remark.trim() === '' ? {} : { remark: pkgForm.remark.trim() }),
    })
    showToast(PKG_CREATED_TEXT)
    // 成功清全部输入框（表单回到空白，不是留着旧值）；secret 在 finally 里再清一次兜底。
    pkgForm.keyId = ''
    pkgForm.keySecret = ''
    pkgForm.totalQuota = ''
    pkgForm.expiresAt = ''
    pkgForm.remark = ''
    await reloadPackages()
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    // ⛔ secret 框任何响应不回显：成功失败都在 finally 清空（失败时也不留它活过一次往返）。
    pkgForm.keySecret = ''
    pkgSubmitting.value = false
  }
}
</script>

<template>
  <div class="cv2-page" data-v2-page="real-name">
    <!-- 筛选行：控件与 placeholder 逐字照证据件；改筛选不自动取数（同旧页：搜索/刷新才发） -->
    <section class="cv2-panel cv2-filter" aria-label="筛选" data-v2="filters">
      <div class="cv2-filter-row">
        <label class="cv2-field">
          <span class="cv2-field-label">实名时间</span>
          <span class="rn-daterange">
            <input v-model="filters.sDate" class="cv2-input" type="date" data-v2-filter="sdate" />
            <span class="rn-dash">-</span>
            <input v-model="filters.eDate" class="cv2-input" type="date" data-v2-filter="edate" />
          </span>
        </label>
        <label class="cv2-field">
          <span class="cv2-field-label">实名结果</span>
          <el-select v-model="filters.certStatus" :teleported="false" data-v2-filter="cert-status">
            <el-option :value="REALNAME_STATUS_ALL" label="全部" />
            <el-option :value="REALNAME_STATUS_FAIL" label="实名失败" />
            <el-option :value="REALNAME_STATUS_PASS" label="实名通过" />
          </el-select>
        </label>
        <label class="cv2-field">
          <span class="cv2-field-label">用户名搜索</span>
          <el-input v-model="filters.key1" placeholder="系统用户名称" data-v2-filter="key1" />
        </label>
        <label class="cv2-field">
          <span class="cv2-field-label">姓名搜索</span>
          <el-input v-model="filters.key2" placeholder="姓名" data-v2-filter="key2" />
        </label>
        <label class="cv2-field">
          <span class="cv2-field-label">身份证号</span>
          <el-input
            v-model="filters.key3"
            placeholder="身份证号搜索"
            :maxlength="18"
            data-v2-filter="key3"
          />
        </label>
        <div class="cv2-filter-actions">
          <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--find cv2-btn--fill" data-v2="search" :disabled="loading" @click="onSearch">
            搜索
          </button>
        </div>
      </div>
    </section>

    <!-- 工具条 3 颗：刷新（带全筛选）/ 配置实名认证（设置弹窗）/ 购买按钮（运行期改写文案 → 资源包弹窗） -->
    <div class="cv2-toolbar" data-v2="toolbar">
      <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--find" data-v2="refresh" :disabled="loading" @click="onSearch">
        刷新列表
      </button>
      <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--commit" data-v2="settings" :disabled="settingsLoading" @click="openSettings">
        配置实名认证
      </button>
      <button type="button" class="cv2-btn cv2-btn--tool" data-v2="packages" @click="openPackages">
        {{ buyLabel }}
      </button>
      <span class="cv2-toolbar-count" data-v2="total-count">共 {{ total }} 条</span>
    </div>

    <!-- 主表：勾选列 + 10 命名列（列名逐字照 island buildCols），无操作列 -->
    <section class="cv2-panel cv2-table-panel" aria-label="实名认证记录列表">
      <table class="cv2-table">
        <thead>
          <tr>
            <th class="cv2-th-check">
              <input
                type="checkbox"
                data-v2="check-all"
                aria-label="全选本页"
                :checked="allOnPageSelected"
                @change="toggleAllOnPage"
              />
            </th>
            <th class="cv2-th-center">系统用户名称</th>
            <th class="cv2-th-center">真实姓名</th>
            <th class="cv2-th-center">身份证号</th>
            <th class="cv2-th-center">性别</th>
            <th class="cv2-th-center">年龄</th>
            <th class="cv2-th-center">认证方式</th>
            <th class="cv2-th-center">认证结果</th>
            <th class="cv2-th-center">申请时间</th>
            <th class="cv2-th-center">认证时间</th>
            <th class="cv2-th-center">认证图片</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id" class="cv2-row" :data-v2-row="row.id">
            <td>
              <input
                type="checkbox"
                :aria-label="`勾选 ${row.user_name}`"
                :data-v2-check="row.id"
                :checked="selectedIds.includes(row.id)"
                @change="toggleRow(row)"
              />
            </td>
            <td class="cv2-td-center">{{ row.user_name }}</td>
            <td class="cv2-td-center">{{ row.name }}</td>
            <td class="cv2-td-center" :title="ID_CARD_TITLE" :data-v2-idcard="row.id">{{ row.id_card_no }}</td>
            <td class="cv2-td-center">{{ row.gender }}</td>
            <td class="cv2-td-center">
              <span
                v-if="isAbnormalAge(row.age)"
                class="rn-age-bad"
                :title="ABNORMAL_AGE_TIPS"
                :data-v2-age="row.id"
              >{{ row.age }}</span>
              <template v-else>{{ row.age !== null ? row.age : '' }}</template>
            </td>
            <td class="cv2-td-center">
              <span class="cv2-st" :class="certMethodClass(row.cert_method)" :data-v2-cert-method="row.id">
                <i class="cv2-dot" aria-hidden="true"></i>{{ certMethodText(row.cert_method) }}
              </span>
            </td>
            <td class="cv2-td-center">
              <span class="cv2-st" :class="certResultClass(row.result)" :data-v2-cert-result="row.id">
                <i class="cv2-dot" aria-hidden="true"></i>{{ certResultText(row.result) }}
              </span>
            </td>
            <td class="cv2-td-center cv2-td-time">{{ formatCreatedAt(row.applied_at) }}</td>
            <td class="cv2-td-center cv2-td-time">{{ row.certified_at !== null ? formatCreatedAt(row.certified_at) : '' }}</td>
            <td class="cv2-td-center">
              <a
                href="#"
                class="cv2-order-link"
                :title="CERT_IMAGE_TITLE"
                data-v2="cert-img"
                @click.prevent="onCertImageClick(row)"
              >点击查看</a>
            </td>
          </tr>
          <tr v-if="rows.length === 0">
            <td colspan="11" class="cv2-empty" data-v2="empty">
              <template v-if="loading">正在加载…</template>
              <template v-else>暂无数据</template>
            </td>
          </tr>
        </tbody>
      </table>
      <div class="cv2-pager" data-v2="pager">
        <span class="cv2-pager-total" data-v2="pager-total">共 {{ total }} 条</span>
        <el-pagination
          :current-page="page"
          :page-size="PAGE_LIMIT"
          :total="total"
          layout="prev, pager, next"
          background
          @current-change="onPageChange"
        />
      </div>
    </section>

    <!-- 认证图片：法律提示确认（原文逐字；两个按钮逐字） -->
    <el-dialog
      :model-value="certDlg !== null"
      title="查看认证图片"
      class="cv2-dialog cv2-dialog--sm"
      data-v2="dlg-cert-notice"
      @update:model-value="certDlg = null"
    >
      <p class="cv2-dlg-text rn-notice" data-v2="cert-notice-text">{{ CERT_IMAGE_NOTICE }}</p>
      <div class="cv2-dlg-actions">
        <button type="button" class="cv2-btn" data-v2="cert-notice-close" @click="certDlg = null">
          {{ CERT_IMAGE_CLOSE_TEXT }}
        </button>
        <button type="button" class="cv2-btn cv2-btn--primary" data-v2="cert-notice-confirm" @click="confirmCertNotice">
          {{ CERT_IMAGE_CONFIRM_TEXT }}
        </button>
      </div>
    </el-dialog>

    <!-- 认证图片大图（标题照 island photos：`<系统用户名称>的认证图`） -->
    <el-dialog
      :model-value="imgDlg !== null"
      :title="imgDlg === null ? '' : `${imgDlg.userName}的认证图`"
      class="cv2-dialog cv2-dialog--lg"
      destroy-on-close
      data-v2="dlg-cert-image"
      @update:model-value="imgDlg = null"
    >
      <img
        v-if="imgDlg !== null"
        :src="imgDlg.img"
        :alt="`${imgDlg.userName}的认证图片`"
        class="rn-certimg"
        data-v2="cert-image"
      />
    </el-dialog>

    <!-- 实名认证相关设置（md）：四项逐字（问题文案照证据件 .settingDiv）+ 两态提示 + 保存设置 -->
    <el-dialog
      :model-value="settingsDlg"
      title="实名认证相关设置"
      class="cv2-dialog cv2-dialog--md"
      data-v2="dlg-settings"
      @update:model-value="settingsDlg = false"
    >
      <div class="rn-item" data-v2-setting-item="enabled">
        <p class="rn-q">1、是否启用系统子账号人脸识别实名认证功能？（默认不启用，只有启用了子账号那边才能发起实名认证）</p>
        <el-radio-group v-model="settings.enabled" data-v2-setting="enabled">
          <el-radio :value="false">不启用</el-radio>
          <el-radio :value="true">启用</el-radio>
        </el-radio-group>
      </div>
      <div class="rn-item" data-v2-setting-item="block_claim_without_cert">
        <p class="rn-q">2、是否不允许没有通过实名认证的子账号在内部抢单大厅接单？（默认允许）</p>
        <el-radio-group v-model="settings.block_claim_without_cert" data-v2-setting="block_claim_without_cert">
          <el-radio :value="false">允许</el-radio>
          <el-radio :value="true">不允许</el-radio>
        </el-radio-group>
      </div>
      <div class="rn-item" data-v2-setting-item="require_cert_for_settlement">
        <p class="rn-q">3、通过系统给子账号在线结算打款时是否要求该用户必须通过了实名认证？（默认不要求此条件）</p>
        <el-radio-group v-model="settings.require_cert_for_settlement" data-v2-setting="require_cert_for_settlement">
          <el-radio :value="false">不要求</el-radio>
          <el-radio :value="true">要求实名</el-radio>
        </el-radio-group>
      </div>
      <div class="rn-item" data-v2-setting-item="ages">
        <p class="rn-q">4、只允许指定范围区间的年龄用户通过实名，不符合年龄范围的将会被拒绝通过(为空或填0代表不限制)</p>
        <span class="rn-ages">
          <input v-model.number="settings.min_age" class="cv2-input" type="number" min="0" step="1" data-v2-setting="min-age" />
          <span class="rn-dash">-</span>
          <input v-model.number="settings.max_age" class="cv2-input" type="number" min="0" step="1" data-v2-setting="max-age" />
        </span>
      </div>
      <p class="cv2-field-hint rn-note" data-v2="settings-note">{{ SETTINGS_NOT_APPLIED_NOTE }}</p>
      <div class="cv2-dlg-actions">
        <button type="button" class="cv2-btn cv2-btn--submit cv2-btn--commit" data-v2="settings-save" :disabled="settingsSaving" @click="saveSettings">
          保存设置
        </button>
      </div>
    </el-dialog>

    <!-- 配置实名资源包（md）：现有密钥包（密钥只显示前 4 位）+ 添加密钥包表单 -->
    <el-dialog
      :model-value="packagesDlg"
      title="配置实名资源包"
      class="cv2-dialog cv2-dialog--md"
      data-v2="dlg-packages"
      @update:model-value="packagesDlg = false"
    >
      <p class="rn-q" data-v2="pkg-list-title">现有密钥包（密钥只显示前 4 位）</p>
      <table class="cv2-table" data-v2="pkg-table">
        <thead>
          <tr>
            <th>密钥 Id</th>
            <th class="cv2-th-center">余量</th>
            <th class="cv2-th-center">状态</th>
            <th class="cv2-th-center">有效期至</th>
            <th>备注</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in pkgRows" :key="p.id" :data-v2-pkg-row="p.id">
            <td>{{ p.key_id }}</td>
            <td class="cv2-td-center">{{ p.total_quota - p.used_quota }}/{{ p.total_quota }}</td>
            <td class="cv2-td-center">{{ p.is_active ? '启用' : '停用' }}</td>
            <td class="cv2-td-center">{{ pkgExpiresText(p.expires_at) }}</td>
            <td>{{ p.remark || '（无备注）' }}</td>
          </tr>
          <tr v-if="pkgRows === null && pkgLoading">
            <td colspan="5" class="cv2-empty">正在加载…</td>
          </tr>
          <tr v-else-if="pkgRows !== null && pkgRows.length === 0">
            <td colspan="5" class="cv2-empty" data-v2="pkg-empty">还没有录入任何密钥包</td>
          </tr>
          <tr v-else-if="pkgRows === null && pkgError !== ''">
            <td colspan="5" class="cv2-empty" data-v2="pkg-error">{{ pkgError }}</td>
          </tr>
        </tbody>
      </table>

      <p class="pkg-block-title" data-v2="pkg-form-title">添加密钥包</p>
      <div class="pkg-form" data-v2="pkg-form">
        <label class="pkg-form-label" for="rn-pkg-key-id">密钥 Id</label>
        <el-input id="rn-pkg-key-id" v-model="pkgForm.keyId" data-v2-pkg="key-id" />
        <label class="pkg-form-label" for="rn-pkg-key-secret">密钥 Key</label>
        <el-input
          id="rn-pkg-key-secret"
          v-model="pkgForm.keySecret"
          type="password"
          autocomplete="new-password"
          data-v2-pkg="key-secret"
        />
        <label class="pkg-form-label" for="rn-pkg-total">总次数</label>
        <input
          id="rn-pkg-total"
          v-model="pkgForm.totalQuota"
          class="cv2-input"
          type="number"
          min="1"
          step="1"
          data-v2-pkg="total-quota"
        />
        <label class="pkg-form-label" for="rn-pkg-expires">有效期至</label>
        <el-input
          id="rn-pkg-expires"
          v-model="pkgForm.expiresAt"
          placeholder="如 2027-10-10，留空 = 不过期"
          data-v2-pkg="expires-at"
        />
        <label class="pkg-form-label" for="rn-pkg-remark">备注</label>
        <el-input id="rn-pkg-remark" v-model="pkgForm.remark" placeholder="选填" data-v2-pkg="remark" />
      </div>
      <div class="cv2-dlg-actions pkg-actions">
        <button type="button" class="cv2-btn cv2-btn--submit cv2-btn--commit" data-v2="pkg-submit" :disabled="pkgSubmitting" @click="submitPackage">
          添加
        </button>
      </div>
    </el-dialog>

    <!-- 炭火 toast -->
    <div
      v-if="toast"
      class="cv2-toast"
      :class="{ 'cv2-toast--error': toast.kind === 'error' }"
      role="status"
      data-v2="toast"
    >
      {{ toast.text }}
    </div>
  </div>
</template>

<style scoped>
/* 弹层与筛选的排版补齐（间距/布局；色全部走 cv2 类与 token，零裸色值） */
.rn-daterange {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.rn-dash {
  color: var(--tt2-dim);
}
.rn-age-bad {
  color: var(--tt2-st-error);
  font-weight: 600;
}
.rn-q {
  margin: 0 0 4px;
  font-size: 14px;
}
.rn-item {
  margin: 0 0 16px;
}
.rn-note {
  margin: 4px 0 0;
}
.rn-notice {
  line-height: 1.7;
}
.rn-ages {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.rn-ages .cv2-input {
  width: 100px;
}
.rn-certimg {
  display: block;
  max-width: 100%;
  margin: 0 auto;
}
.pkg-block-title {
  margin: 18px 0 8px;
  font-size: 14px;
  font-weight: 600;
}
.pkg-form {
  display: grid;
  grid-template-columns: 88px minmax(0, 1fr);
  column-gap: 10px;
  row-gap: 10px;
  align-items: center;
  margin-top: 10px;
}
.pkg-form-label {
  font-size: 13px;
  color: var(--tt2-dim);
}
.pkg-actions {
  margin-top: 12px;
}
</style>
