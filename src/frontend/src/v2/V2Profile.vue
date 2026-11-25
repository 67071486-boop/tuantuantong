<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/** 「我的资料」v2（`/v2/profile`；「我的相关」组 4 页 v2 化，D-0353）：island 复刻页 `user-myinfo.ts`（对标 `/User/myinfo`，三页签表单页）的炭火重建。基本资料 = 只读 3 格 + 可编辑 7 字段 `PATCH /api/auth/profile`（表单 2 列网格限宽 880，纯布局零字段改动；登录账号照旧只读；保存成功后用**响应那一行**回填）；⭐ **加载完成前不许编辑**（7 框 + 保存先 `disabled`，回填成功才解锁、取数失败保持锁定——防「打进去的字被服务端旧值静默覆盖」，`profileLoaded` / `bindingLoaded` 两道锁）；游戏权限只读格拿不到资料 = 自己的游戏库一个不勾。
 * 头像 / 微信收款码上传：10 MB 闸 → 客户端压缩 → 直传票据 → 认领 → 只刷那一格（复用 `layui-island/imageCompress` + `directUpload` 共享件，零 layui import）；收款码两态 + 短时链接 `onerror` 重取一次（v2 页不在 iframe 里 ⇒ 旧页那条 postMessage 给壳不需要）。实名认证真弹层同口径（`enabled && !isRealName` 才显入口、提交前必看计费提示、结果留屏、失败按业务码分开发话）。功能设置 = 下家鸡蛋铺绑定块**全量照搬**（同一批端点与纯函数语义：「②→① 切回补 `mode`」、「没探 ≠ 不可用」；②式选项 `disabled`——下家绑定放弃②式、存量②式行如实回显不假装是①式）；修改密码「两次输入不一致」同文案（权威判据在后端）；「绑定邮箱 / 绑定QQ」假控件**不搬**。数据零新增端点；⛔ 不 import island 页模块（绑定块纯函数 / 文案常量是带出处注记的本地重写，两边都有测试钉住、改口径两处同改）。 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { BizError } from '@/api/errors'
import {
  fetchMemberJidanpuBinding,
  saveMemberJidanpuBinding,
  testMemberJidanpuConnection,
  type JidanpuEndpointCheck,
  type JidanpuMemberBindingPayload,
  type JidanpuMemberBindingRow,
  type JidanpuSourceOption,
  SOURCE_NAME_DEFAULT,
} from '@/api/jidanpuMember'
import {
  changePassword,
  claimMedia,
  fetchMyProfile,
  patchMyProfile,
  requestMediaTicket,
  type MediaKind,
  type MyProfile,
  type MyProfilePatch,
} from '@/api/myProfile'
import { verifyRealName } from '@/api/realname'
import { fetchAllGames } from '@/api/config'
import { ErrorCode } from '@/config/errorCodes'
import { avatarDataUri } from '@/config/avatar'
import { putFileToStorage } from '@/layui-island/directUpload'
import {
  MAX_USER_MEDIA_BYTES,
  compressImage,
  mediaQuality,
  userMediaTicketExt,
} from '@/layui-island/imageCompress'

/* ── 文案（出处见 island 页同名常量；本地重写不引模块）──────────────────── */

/** 「加载完成前不许编辑」的两道锁共用的失败提示口径：错误照旧弹后端 msg（errText）。 */
const SAVE_SUCCESS_TEXT = '保存成功'
/** 实名弹层里两块没填全的提示（island 页 `REALNAME_MISSING_INPUT_TEXT`；我方自拟非后端文案）。 */
const REALNAME_MISSING_INPUT_TEXT = '请填写真实姓名与身份证号后再提交'
/** 提交前的计费提示（**一字不许省**：厂商按「查得」计费，不一致也扣 1 次；island 页同句）。 */
const REALNAME_BILLING_NOTICE = '本次核验将消耗 1 次，无论核验结果一致与否'
/** 「已通过实名认证」（island 页 `REALNAME_PASS_TEXT`）。 */
const REALNAME_PASS_TEXT = '已通过实名认证'
/** 核验没通过（后端 `result: "fail"`，这一次也照样扣了；island 页 `REALNAME_FAIL_TEXT`）。 */
const REALNAME_FAIL_TEXT = '实名认证未通过，请核对姓名与身份证号是否一致'
/** 后端「号码本地就不合法」的兜底（island 页 `REALNAME_INVALID_TEXT`）。 */
const REALNAME_INVALID_TEXT = '身份证号格式不正确，请核对后重新提交'
/** 收款码图片加载失败的固定提示（重试用尽后；island 页 `WECHAT_QR_FAILED_TEXT`）。 */
const WECHAT_QR_FAILED_TEXT = '收款码图片加载失败，请稍后重试'
/** 收款码按钮两态（island 页 `WECHAT_QR_IDLE_TEXT` / `WECHAT_QR_REUPLOAD_TEXT` 逐字）。 */
const WECHAT_QR_IDLE_TEXT = '上传收款码'
const WECHAT_QR_REUPLOAD_TEXT = '重新上传'
/** 头像 / 收款码的单张上限提示（10 MB；后端是权威，这一句只是前置早挡；island 页同句）。 */
const MEDIA_TOO_LARGE_TEXT = '头像与收款码最大支持10MB，请压缩后重试'
/** 绑定块的早挡与结果文案（island 页同名常量逐字）。 */
const MEMBER_MISSING_TEXT = '请先填团团通地址与账号'
const MEMBER_CLIENT_ID_REQUIRED_TEXT = '请填鸡蛋铺给你的 client_id'
const MEMBER_CLIENT_SECRET_REQUIRED_TEXT =
  '请填鸡蛋铺给你的 client_secret（只在保存时填一次，之后不会再显示）'
const MEMBER_TEST_BUTTON_TEXT = '测试连接（用已保存的配置）'
const MEMBER_TEST_EMPTY_RESULT_TEXT = '这次检测没有返回任何结论，请把这条反馈给管理员'
const MEMBER_CALLBACK_HELP_TEXT =
  '把下面这个地址填到鸡蛋铺那侧的「开放平台 → 回调通知地址」里。这个地址是这台系统专用的，请勿外传。'
const MEMBER_CALLBACK_PENDING_TEXT = '保存一次之后，这里会显示要填进鸡蛋铺那侧的回调地址。'
const MEMBER_CALLBACK_MISSING_REASON_TEXT =
  '回调地址暂时取不到（后端没给出原因），请把这条反馈给管理员'
const MEMBER_CALLBACK_COPY_OK_TEXT = '已复制回调地址'
const MEMBER_CALLBACK_COPY_FAIL_TEXT = '复制失败，请手动选中复制'
const MEMBER_CHECK_OK_TEXT = '可用'
const MEMBER_CHECK_FAIL_TEXT = '不可用'
const MEMBER_CHECK_NOT_PROBED_TEXT = '没探'
/** 「同步大厅订单」开关那格的提示（D-0299 任务书原文逐字）。 */
const MEMBER_HALL_SYNC_HINT =
  '关闭后：指派给你的单仍会同步到对方系统，但不会抛进对方的抢单大厅'
/** 「订单来源名」提示位三态（island 页 `MEMBER_SOURCE_HINT_*` 逐字）。 */
const MEMBER_SOURCE_HINT_IDLE = '录单时写进鸡蛋铺的来源名；点「测试连接」可拉取对方来源名单后在这里选。'
const MEMBER_SOURCE_HINT_UNREADABLE = '这次没读到对方的来源名单（不影响保存）；可再点一次「测试连接」。'

/** 对接方式两态（island 页 `MEMBER_MODE_*`；②式选项 disabled = 2026-10-24 业务方裁定）。 */
const MEMBER_MODE_PASSWORD = 'password'
const MEMBER_MODE_OPENAPI = 'openapi'
const MODE_OPTIONS: readonly { value: string; label: string; disabled?: boolean }[] = [
  { value: MEMBER_MODE_PASSWORD, label: '账号 + 密码' },
  { value: MEMBER_MODE_OPENAPI, label: '官方开放平台 API', disabled: true },
]

/** ②式「测试连接」逐条表的四列（island 页 `MEMBER_CHECK_COLUMNS` 逐字）。 */
const MEMBER_CHECK_COLUMNS: readonly string[] = ['接口', '结果', '原因', '该做什么']

/** 允许上传的扩展名（与后端 `ALLOWED_EXTS` 同口径；前端只是早挡）。 */
const ALLOWED_UPLOAD_EXTS = ['jpg', 'jpeg', 'png'] as const

/** 错误对象的可显示文本（island 页 `errorText` 同式）。 */
function errText(e: unknown): string {
  return e instanceof Error && e.message ? e.message : '请求失败，请稍后重试'
}

/* ── 提示（炭火 toast；info 1500ms / error 6000ms——绑定块的长提示要看清）────────── */

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

/* ── 页签 ─────────────────────────────────────────────────────────────── */

const route = useRoute()
const activeTab = ref<'basic' | 'features' | 'password'>('basic')
watch(() => route.query.tab, (tab) => {
  activeTab.value = tab === 'password' ? 'password' : 'basic'
}, { immediate: true })
const guideStep = ref(0)
const guideSteps = ['创建客服账号', '新增订单来源', '保存并测试', '启用同步']

/** 静态资源前缀（向导两张示意图；⛔ 不写死 `/guide/`——部署基址会变，island 页同一处置）。 */
const guideBase = import.meta.env.BASE_URL || '/'

/* ══ 基本资料 ════════════════════════════════════════════════════════════ */

/** 可编辑 7 字段（与后端 `EDITABLE_FIELDS` / island 页 `EDITABLE_SELECTORS` 一一对应）。 */
const form = reactive<MyProfilePatch>({
  alipay_account: '',
  alipay_name: '',
  bank_name: '',
  bank_card_no: '',
  qq: '',
  mobile: '',
  live_url: '',
})

/** 资料载荷的只读展示面（登录账号 / 身份证号 / 底薪 / 实名 / 头像 / 收款码）。 */
const profile = ref<MyProfile | null>(null)
/** 「加载完成前不许编辑」：取数成功才解锁（失败保持锁定——旧页同一口径）。 */
const profileLoaded = ref(false)

const isRealName = computed(() => profile.value?.is_real_name === true)
/** 实名入口的显示条件（旧页 renderRealName 第三参；键缺席按 false，fail-closed）。 */
const realNameEnabled = computed(() => profile.value?.realname?.enabled ?? false)

/** 头像：传过用服务端短时 URL；没传回落按昵称哈希的色块（⛔ 前端不拼 URL）。 */
const avatarSrc = computed(() => {
  const p = profile.value
  if (!p) return avatarDataUri('?', 240)
  return p.avatar_url ?? avatarDataUri(p.display_name || p.username, 240)
})

/** 微信收款码的短时链接（null = 未上传 ⇒ 「已上传」那颗不显示）。 */
const wechatQrUrl = ref<string | null>(null)

/** 游戏权限（fetchAllGames × profile.games；取数失败 = 空库 + 一个不勾——诚实）。 */
const games = ref<{ id: number; name: string }[]>([])
const gamesAllChecked = computed(() => {
  const owned = new Set(profile.value?.games ?? [])
  return games.value.length > 0 && games.value.every((g) => owned.has(g.id))
})
function gameChecked(id: number): boolean {
  return (profile.value?.games ?? []).includes(id)
}

/** 底薪显示：`x.xx`（island 页 `fillProfile` 同式；null ⇒ '0.00'）。 */
const baseSalaryText = computed(() => {
  const raw = profile.value?.base_salary
  return raw == null ? '0.00' : Number(raw).toFixed(2)
})

async function saveProfile(): Promise<void> {
  try {
    const updated = await patchMyProfile({ ...form })
    profile.value = updated
    fillProfileForm(updated)
    showToast(SAVE_SUCCESS_TEXT)
  } catch (e) {
    showToast(errText(e), 'error') // 只回显服务端 msg，前端不造文案
  }
}

/** 载荷 → 表单（island 页 `fillProfile` 的映射表）。 */
function fillProfileForm(data: MyProfile): void {
  form.alipay_account = data.alipay_account
  form.alipay_name = data.alipay_name
  form.bank_name = data.bank_name
  form.bank_card_no = data.bank_card_no
  form.qq = data.qq
  form.mobile = data.mobile
  form.live_url = data.live_url
}

/* ── 头像 / 收款码上传（选文件 → 10MB 闸 → 压缩 → 票据 → 直传 → 认领 → 只刷那一格）──── */

/** 传完**只刷自己那一格**（⛔ 不整页重取——会把用户正在填的其它字段静默丢掉，island 页同口径）。 */
async function handleMediaUpload(kind: MediaKind): Promise<void> {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = 'image/jpeg,image/png'
  input.style.display = 'none'
  document.body.appendChild(input)
  input.addEventListener('change', () => {
    void (async () => {
      const file = input.files?.[0]
      if (!file) return
      const ext = (file.name.split('.').pop() ?? '').toLowerCase()
      if (!(ALLOWED_UPLOAD_EXTS as readonly string[]).includes(ext)) {
        showToast('只支持 jpg / jpeg / png')
        return
      }
      try {
        // ① 10 MB 上限：发起票据请求之前拦（早反馈、零请求；后端是权威）。
        if (file.size > MAX_USER_MEDIA_BYTES) {
          showToast(MEDIA_TOO_LARGE_TEXT, 'error')
          return
        }
        // ② 压缩：头像四档；收款码 max(四档, 0.8)（给人扫的）。失败回落原图。
        const prepared = await compressImage(file, mediaQuality(kind, file.size))
        // ③ 票据 ext 用压缩后的形态（成功一律 jpg；回落原图取原扩展名）。
        const ticket = await requestMediaTicket(kind, userMediaTicketExt(file, prepared))
        await putFileToStorage(ticket.upload_url, prepared)
        await claimMedia(kind, ticket.key)
        showToast('上传成功')
        // 刷新这一跳单独 try/catch：认领已成功 ⇒ 刷新失败**不提示**（静默；下次进页面会再取）。
        try {
          const fresh = await fetchMyProfile()
          if (kind === 'avatar') {
            profile.value = fresh
          } else {
            wechatQrUrl.value = fresh.wechat_qr_url
          }
        } catch {
          // 刻意留空（理由见上）。
        }
      } catch (error) {
        showToast(errText(error), 'error')
      } finally {
        input.remove()
      }
    })()
  })
  input.click()
}

/* ── 收款码看图层（短时链接 300s：onerror 重取一次再试，仍失败关层 + 固定提示）──────── */

const qrDlg = ref(false)
const qrDlgSrc = ref('')
let qrRetried = false

function openWechatQr(): void {
  const src = wechatQrUrl.value ?? ''
  if (src === '') {
    // 防御（正常不可达：未上传时那颗按钮不显示）——真被点到也不开空白层。
    showToast(WECHAT_QR_FAILED_TEXT)
    return
  }
  qrRetried = false
  qrDlgSrc.value = src
  qrDlg.value = true
}

async function onQrImgError(): Promise<void> {
  if (qrRetried) {
    qrDlg.value = false
    showToast(WECHAT_QR_FAILED_TEXT)
    return
  }
  qrRetried = true
  try {
    const fresh = await fetchMyProfile()
    const next = fresh.wechat_qr_url
    if (typeof next !== 'string' || next === '') {
      qrDlg.value = false
      showToast(WECHAT_QR_FAILED_TEXT)
      return
    }
    wechatQrUrl.value = next
    qrDlgSrc.value = next
  } catch {
    qrDlg.value = false
    showToast(WECHAT_QR_FAILED_TEXT)
  }
}

/* ── 实名认证弹层（enabled && !isRealName 才有入口；结果留在弹层里，不用一闪而过的 toast）── */

const rnDlg = ref(false)
const rnName = ref('')
const rnIdCardNo = ref('')
const rnResult = ref('')

function openRealNameCert(): void {
  rnName.value = ''
  rnIdCardNo.value = ''
  rnResult.value = ''
  rnDlg.value = true
}

/** 业务码 → 人话（island 页 `realNameErrorText` 的逐条重写；⛔ 不用一句「核验失败」糊过去）。 */
function realNameErrorText(e: unknown): string {
  if (e instanceof BizError) {
    switch (e.code) {
      case ErrorCode.REALNAME_DISABLED:
        return '本系统尚未启用实名认证功能，请联系管理员开启'
      case ErrorCode.REALNAME_QUOTA_EXHAUSTED:
        return '实名认证余量不足，请联系客服购买'
      case ErrorCode.REALNAME_NO_PACKAGE:
        return '平台侧暂无可用的实名认证密钥包，请联系管理员补充'
      case ErrorCode.REALNAME_RATE_LIMITED:
        return '尝试过于频繁，请稍后再试'
      case 1017:
        return '实名认证服务暂时不可用（上游网络异常），请稍后重试'
      case ErrorCode.VALIDATION:
        return e.message || REALNAME_INVALID_TEXT
      default:
        return e.message
    }
  }
  return errText(e)
}

async function submitRealNameCert(): Promise<void> {
  const name = rnName.value.trim()
  const idCardNo = rnIdCardNo.value.trim()
  if (name === '' || idCardNo === '') {
    rnResult.value = REALNAME_MISSING_INPUT_TEXT
    return
  }
  try {
    const result = await verifyRealName({ name, id_card_no: idCardNo })
    if (result.result === 'pass') {
      rnResult.value =
        result.remaining === null
          ? REALNAME_PASS_TEXT
          : `${REALNAME_PASS_TEXT}（剩余核验次数 ${result.remaining} 次）`
      // 页面那一格当场变绿——用户不必刷新就能看到自己核过了（余量沿用这次响应的读数）。
      profile.value = profile.value === null ? null : { ...profile.value, is_real_name: true }
      return
    }
    // fail：这一次也照样扣了；页面状态不动（没通过就是没通过）。
    rnResult.value =
      result.remaining === null
        ? REALNAME_FAIL_TEXT
        : `${REALNAME_FAIL_TEXT}（剩余核验次数 ${result.remaining} 次）`
  } catch (e) {
    rnResult.value = realNameErrorText(e)
  }
}

/* ══ 功能设置：下家鸡蛋铺绑定 ═════════════════════════════════════════════ */

const b = reactive({
  mode: MEMBER_MODE_PASSWORD,
  baseUrl: '',
  clientId: '',
  clientSecret: '',
  account: '',
  password: '',
  sourceName: SOURCE_NAME_DEFAULT,
  throwPercent: '100',
  hallSync: true,
  enabled: false,
})

/** 块自己的那一行（null = 还没绑定 / 这次没读到）。 */
const bindingRow = ref<JidanpuMemberBindingRow | null>(null)
const bindingLoaded = ref(false)
/** 两个「配没配」布尔（只回答配没配；⛔ 拿它回填输入框就是往框里写 true/false）。 */
const passwordSet = ref(false)
const clientSecretSet = ref(false)

/** 密码 / 密钥输入框的占位（回填后按「配没配」换文案；island 页 `fillMemberBinding` 同口径）。 */
const passwordPlaceholder = computed(() =>
  passwordSet.value ? '已配置，留空表示不修改' : '请输入鸡蛋铺账号的密码',
)
const clientSecretPlaceholder = computed(() =>
  clientSecretSet.value ? '已配置，留空表示不修改' : '请输入鸡蛋铺给你的 client_secret',
)

const isPasswordMode = computed(() => b.mode !== MEMBER_MODE_OPENAPI)
const isOpenapiMode = computed(() => b.mode === MEMBER_MODE_OPENAPI)

/* ── 「订单来源名」下拉（D-0177）：对方字典 + 三态提示位 ─────────────────────────── */

type MemberSourceDict = { options: JidanpuSourceOption[] | null; ok: boolean }

const sourceDict = reactive<MemberSourceDict>({ options: null, ok: false })

/** 下拉的选项 = 字典全量 + 当前值不在字典里时**保留并选中**（那一格永远有非空选中值：
 *  两处 PUT 对空串的语义不同，本页那条会把空串落回默认名——一旦能提交空值，
 *  「什么都没动」的一次保存就把自定义来源名静默改回默认，island 页同一登记）。 */
const sourceOptions = computed<string[]>(() => {
  const labels = (sourceDict.options ?? []).map((item) => item.label)
  const wanted = b.sourceName.trim() === '' ? SOURCE_NAME_DEFAULT : b.sourceName
  return labels.includes(wanted) ? labels : [...labels, wanted]
})

/** 提示位三支（island 页 `memberSourceHint` 同式重写）：同名多条 / 对方已无此来源 / 没读到。 */
const sourceHint = computed<{ text: string; warn: boolean }>(() => {
  if (sourceDict.options === null) return { text: MEMBER_SOURCE_HINT_IDLE, warn: false }
  if (!sourceDict.ok) return { text: MEMBER_SOURCE_HINT_UNREADABLE, warn: true }
  const labels = sourceDict.options.map((item) => item.label)
  const counted = new Map<string, number>()
  for (const label of labels) counted.set(label, (counted.get(label) ?? 0) + 1)
  const duplicated = Array.from(counted.values()).filter((count) => count > 1)
  const worst = duplicated.length === 0 ? 0 : Math.max(...duplicated)
  const lines: string[] = []
  if (worst > 1) {
    lines.push(`对方系统里有 ${worst} 个同名来源，我们无法区分它们；请到对方系统改成不同名字后再选`)
  }
  const current = b.sourceName.trim()
  if (current !== '' && !labels.includes(current)) lines.push('对方已无此来源，请重选')
  return lines.length === 0
    ? { text: MEMBER_SOURCE_HINT_IDLE, warn: false }
    : { text: lines.join('；'), warn: true }
})

/* ── 回调地址那一格（值只来自后端；算不出来显示后端给的原因；没保存过显示那句人话）────── */

const callbackCell = computed<{ url: string; reason: string }>(() => {
  const row = bindingRow.value
  if (row === null) return { url: '', reason: MEMBER_CALLBACK_PENDING_TEXT }
  const url = row.callback_url ?? ''
  if (url !== '') return { url, reason: '' }
  return { url: '', reason: row.callback_url_reason || MEMBER_CALLBACK_MISSING_REASON_TEXT }
})

function copyCallbackUrl(): void {
  const text = callbackCell.value.url
  if (text === '') {
    showToast(MEMBER_CALLBACK_COPY_FAIL_TEXT)
    return
  }
  const clipboard: { writeText?: (value: string) => Promise<void> } | undefined =
    navigator.clipboard
  if (!clipboard || typeof clipboard.writeText !== 'function') {
    showToast(MEMBER_CALLBACK_COPY_FAIL_TEXT)
    return
  }
  try {
    void Promise.resolve(clipboard.writeText.call(clipboard, text)).then(
      () => showToast(MEMBER_CALLBACK_COPY_OK_TEXT),
      () => showToast(MEMBER_CALLBACK_COPY_FAIL_TEXT),
    )
  } catch {
    showToast(MEMBER_CALLBACK_COPY_FAIL_TEXT)
  }
}

/* ── 健康状态（island 页 `memberHealthHtml` 的本地重写；空态要如实）──────────────── */

function memberMoment(value: string): string {
  return value.replace('T', ' ').slice(0, 16)
}

const healthRows = computed<{ label: string; value: string }[]>(() => {
  const row = bindingRow.value
  if (row === null) return [{ label: '绑定状态', value: '还没绑定' }]
  const run = row.last_run_at === null ? '—（还没跑过同步）' : memberMoment(row.last_run_at)
  const error =
    row.last_error === ''
      ? '—（暂无失败记录）'
      : row.last_error + (row.last_error_at === null ? '' : `（${memberMoment(row.last_error_at)}）`)
  const rows_: { label: string; value: string }[] = [
    { label: '绑定状态', value: row.enabled ? '已启用' : '未启用' },
    { label: '上次同步', value: run },
    { label: '当前间隔', value: `快路 ${row.fast_poll_seconds} 秒 / 对账 ${row.reconcile_seconds} 秒` },
    { label: '大厅抛单百分比', value: `${row.throw_price_percent}%` },
    { label: '上次失败原因', value: error },
  ]
  // 回调消费 = 后端算好的一句人话（原样透传；没有就一个字不编，那一格不出）。
  if (row.consumer_state.text) rows_.push({ label: '回调消费', value: row.consumer_state.text })
  return rows_
})

/* ── ②式「测试连接」逐条表（`probed` 必须先判：没探 ≠ 不可用）────────────────────── */

const testResult = ref<{ messages: string[]; checks: JidanpuEndpointCheck[] } | null>(null)

function checkResultText(check: Pick<JidanpuEndpointCheck, 'probed' | 'ok'>): string {
  if (!check.probed) return MEMBER_CHECK_NOT_PROBED_TEXT
  return check.ok ? MEMBER_CHECK_OK_TEXT : MEMBER_CHECK_FAIL_TEXT
}

/* ── ①式载荷要不要补 `mode: 'password'`（island 页 `memberPasswordModePatch` 逐条重写）── */

function memberPasswordModePatch(storedMode: string, submittedMode: string): { mode?: string } {
  if (submittedMode === MEMBER_MODE_OPENAPI) return {}
  return storedMode === MEMBER_MODE_OPENAPI ? { mode: MEMBER_MODE_PASSWORD } : {}
}

/** 表单 → ①式载荷（DOM/响应式状态是唯一真相；密码**不 trim**——空格是密码的一部分）。
 *  ⛔ 普通①式保存**刻意不带 `mode`**（后端起 token 按库里那一格走；补法只归 `memberPasswordModePatch`）。 */
function readPasswordPayload(): JidanpuMemberBindingPayload {
  return {
    jidanpu_base_url: b.baseUrl.trim(),
    account: b.account.trim(),
    password: b.password,
    source_name: b.sourceName.trim(),
    throw_price_percent: Number(b.throwPercent || '100'),
    hall_sync_enabled: b.hallSync,
    enabled: b.enabled,
  }
}

/** 表单 → ②式载荷（⛔ 没有 `password`；`client_secret` 不 trim、空串 = 不改；
 *  `account` 照旧带上——那一格只是不显示，值还在，两套凭据允许同时留着）。 */
function readOpenapiPayload(): JidanpuMemberBindingPayload {
  return {
    mode: MEMBER_MODE_OPENAPI,
    jidanpu_base_url: b.baseUrl.trim(),
    account: b.account.trim(),
    client_id: b.clientId.trim(),
    client_secret: b.clientSecret,
    source_name: b.sourceName.trim(),
    throw_price_percent: Number(b.throwPercent || '100'),
    hall_sync_enabled: b.hallSync,
    enabled: b.enabled,
  }
}

async function saveMemberBinding(): Promise<void> {
  const payload = isPasswordMode.value ? readPasswordPayload() : readOpenapiPayload()
  if (payload.jidanpu_base_url === '') {
    showToast(MEMBER_MISSING_TEXT)
    return
  }
  if (isOpenapiMode.value) {
    if ((payload.client_id ?? '') === '') {
      showToast(MEMBER_CLIENT_ID_REQUIRED_TEXT)
      return
    }
    // 密钥只在**从没存过**时必填（空串 = 不改是正当用法）。
    if (clientSecretSet.value !== true && (payload.client_secret ?? '') === '') {
      showToast(MEMBER_CLIENT_SECRET_REQUIRED_TEXT)
      return
    }
  } else if (payload.account === '') {
    showToast(MEMBER_MISSING_TEXT)
    return
  }
  try {
    const saved = await saveMemberJidanpuBinding({
      ...payload,
      // ⭐ ②→① 切回来那一跳补 `mode: 'password'`（不补 = 静默走错方式；②式那一支绝不能补）。
      ...memberPasswordModePatch(bindingRow.value?.mode ?? '', b.mode),
    })
    bindingRow.value = saved
    fillMemberBinding(saved)
    showToast(SAVE_SUCCESS_TEXT)
  } catch (e) {
    showToast(errText(e), 'error') // 后端逐条点名的长提示，6000ms
  }
}

async function testMemberBinding(): Promise<void> {
  try {
    const isOpenapi = isOpenapiMode.value
    const password = isOpenapi ? '' : b.password
    const sourceName = isOpenapi ? '' : b.sourceName.trim() || SOURCE_NAME_DEFAULT
    const result = await testMemberJidanpuConnection(password, sourceName)
    // 字典来自这次响应（只在真的有 `source_options` 时才动——②式的响应里没有这三个键）。
    if (result.source_options !== undefined) {
      sourceDict.options = result.source_options
      sourceDict.ok = result.source_options_ok === true
    }
    if (isOpenapi) {
      testResult.value = { messages: result.messages, checks: result.checks ?? [] }
    }
    if (result.ok) {
      showToast('连接成功')
      return
    }
    // ②式下那张表已经逐条说清了 ⇒ 不再叠一句笼统的话；①式把后端点名的那几句原样弹出来。
    if (isOpenapi) return
    showToast(result.messages.join('；') || '测试连接没通过，请检查上面填的地址与账号', 'error')
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

/** 载荷 → 表单（island 页 `fillMemberBinding` 同口径；密码/密钥框**永远留空**）。 */
function fillMemberBinding(row: JidanpuMemberBindingRow | null): void {
  bindingRow.value = row
  b.baseUrl = row?.jidanpu_base_url ?? ''
  b.account = row?.account ?? ''
  b.clientId = row?.client_id ?? ''
  b.clientSecret = ''
  clientSecretSet.value = row?.client_secret_set === true
  passwordSet.value = row?.password_set === true
  b.password = ''
  // 可见性跟着库里那一行的方式走（⛔ 不无脑回默认值——否则②式用户一保存就把自己切回①式）。
  b.mode = row?.mode === MEMBER_MODE_OPENAPI ? MEMBER_MODE_OPENAPI : MEMBER_MODE_PASSWORD
  // 百分比：库里那个值；新行 / 空值给后端默认的 100。
  b.throwPercent = String(row?.throw_price_percent ?? 100)
  // D-0299：开关回填库里那一格（新行 / 取不到 ⇒ 开——与后端模型默认同一口径）。
  b.hallSync = row?.hall_sync_enabled ?? true
  b.enabled = row?.enabled ?? false
  // 来源名：库里那个名字（空串给默认名的显示副本；后端也会把空串落到同一个默认值）。
  b.sourceName = (row?.source_name ?? '').trim() === '' ? SOURCE_NAME_DEFAULT : row!.source_name
}

/* ── 修改密码 ════════════════════════════════════════════════════════════ */

const oldPwd = ref('')
const newPwd = ref('')
const renewPwd = ref('')

async function savePassword(): Promise<void> {
  // 即时校验只省一次往返；权威判据在后端。
  if (newPwd.value !== renewPwd.value) {
    showToast('两次输入的新密码不一致')
    return
  }
  try {
    await changePassword({
      old_password: oldPwd.value,
      new_password: newPwd.value,
      confirm_password: renewPwd.value,
    })
    oldPwd.value = ''
    newPwd.value = ''
    renewPwd.value = ''
    showToast('修改成功')
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

/* ── 装载：三条取数并发、各自独立回落（旧页同一口径：一张挂了不拖整页）──────────────── */

onMounted(async () => {
  const [profileRes, , bindingRes] = await Promise.all([
    fetchMyProfile()
      .then((data) => ({ ok: true as const, data }))
      .catch((error: unknown) => ({ ok: false as const, message: errText(error) })),
    fetchAllGames()
      .then((rows) => {
        games.value = rows.map(({ id, name }) => ({ id, name }))
      })
      .catch(() => {
        games.value = []
      }),
    // 成功回 null 是**合法结果**（还没绑定）⇒ 必须与「失败」分开：失败时不解锁。
    fetchMemberJidanpuBinding().then(
      (row) => ({ ok: true as const, row }),
      (error: unknown) => ({ ok: false as const, message: errText(error) }),
    ),
  ])
  if (profileRes.ok) {
    profile.value = profileRes.data
    fillProfileForm(profileRes.data)
    wechatQrUrl.value = profileRes.data.wechat_qr_url
    profileLoaded.value = true // 回填落地 ⇒ 解锁（只在成功这一支）
  } else {
    showToast(profileRes.message, 'error')
  }
  if (bindingRes.ok) {
    fillMemberBinding(bindingRes.row)
    bindingLoaded.value = true
  } else {
    showToast(bindingRes.message, 'error')
  }
})
</script>

<template>
  <div class="cv2-page" data-v2-page="profile">
    <!-- 页内三页签（同旧页 IA：基本资料 / 功能设置 / 修改密码） -->
    <div class="cv2-ptabs" data-v2="tabs">
      <button
        type="button"
        class="cv2-ptab"
        :class="{ 'cv2-ptab--on': activeTab === 'basic' }"
        data-v2-tab="basic"
        @click="activeTab = 'basic'"
      >
        基本资料
      </button>
      <button
        type="button"
        class="cv2-ptab"
        :class="{ 'cv2-ptab--on': activeTab === 'features' }"
        data-v2-tab="features"
        @click="activeTab = 'features'"
      >
        功能设置
      </button>
      <button
        type="button"
        class="cv2-ptab"
        :class="{ 'cv2-ptab--on': activeTab === 'password' }"
        data-v2-tab="password"
        @click="activeTab = 'password'"
      >
        修改密码
      </button>
    </div>

    <!-- ══ 基本资料 ══ -->
    <template v-if="activeTab === 'basic'">
      <section class="cv2-panel" aria-label="头像与收款码" data-v2="media">
        <div class="cv2-media-row">
          <img class="cv2-avatar-lg" :src="avatarSrc" alt="头像" data-v2="avatar" />
          <button type="button" class="cv2-btn cv2-btn--commit" data-v2="upload-avatar" @click="handleMediaUpload('avatar')">
            更换头像
          </button>
          <div class="cv2-media-qr">
            <span class="cv2-field-label">微信收款码</span>
            <div class="cv2-media-actions">
              <button
                v-if="wechatQrUrl"
                type="button"
                class="cv2-actlink"
                data-v2="look-qr"
                @click="openWechatQr"
              >
                已上传
              </button>
              <button type="button" class="cv2-btn" data-v2="upload-qr" @click="handleMediaUpload('wechat-qr')">
                {{ wechatQrUrl ? WECHAT_QR_REUPLOAD_TEXT : WECHAT_QR_IDLE_TEXT }}
              </button>
            </div>
          </div>
        </div>
      </section>

      <section class="cv2-panel" aria-label="基本资料表单">
        <div class="cv2-form-grid">
          <label class="cv2-field profile-linked">
            <span class="cv2-field-label">登录账号</span>
            <el-input  type="text" :model-value="profile?.username ?? ''" disabled data-v2-field="loginAcc" />
          </label>
          <label class="cv2-field profile-linked">
            <span class="cv2-field-label">身份证号</span>
            <el-input  type="text" :model-value="profile?.id_card_no ?? ''" disabled data-v2-field="sid" />
          </label>
          <label class="cv2-field profile-linked">
            <span class="cv2-field-label">底薪</span>
            <el-input  type="text" :model-value="baseSalaryText" disabled data-v2-field="basicSalary" />
          </label>
          <div class="cv2-field profile-linked">
            <span class="cv2-field-label">是否实名</span>
            <div class="cv2-realname-row" data-v2="realname">
              <span
                class="cv2-st"
                :class="isRealName ? 'cv2-st--doing' : 'cv2-st--error'"
                data-v2="realname-state"
              ><i class="cv2-dot" aria-hidden="true"></i>{{ isRealName ? '已通过实名认证' : '未实名' }}</span>
              <button
                v-if="realNameEnabled && !isRealName"
                type="button"
                class="cv2-actlink"
                data-v2="to-realname"
                @click="openRealNameCert"
              >
                点击进行实名认证
              </button>
            </div>
          </div>
          <label class="cv2-field profile-linked">
            <span class="cv2-field-label">支付宝账号</span>
            <el-input v-model="form.alipay_account"  type="text" :disabled="!profileLoaded" placeholder="请输入支付宝账号" data-v2-field="alipay_account" />
          </label>
          <label class="cv2-field profile-linked">
            <span class="cv2-field-label">支付宝姓名</span>
            <el-input v-model="form.alipay_name"  type="text" :disabled="!profileLoaded" placeholder="请输入支付宝姓名" data-v2-field="alipay_name" />
          </label>
          <label class="cv2-field profile-linked">
            <span class="cv2-field-label">银行名称</span>
            <el-input v-model="form.bank_name"  type="text" :disabled="!profileLoaded" placeholder="请输入银行名称" data-v2-field="bank_name" />
          </label>
          <label class="cv2-field profile-linked">
            <span class="cv2-field-label">银行卡号</span>
            <el-input v-model="form.bank_card_no"  type="text" :disabled="!profileLoaded" placeholder="请输入银行卡号" data-v2-field="bank_card_no" />
          </label>
          <label class="cv2-field profile-linked">
            <span class="cv2-field-label">QQ号</span>
            <el-input v-model="form.qq"  type="text" :disabled="!profileLoaded" placeholder="请输入QQ号" data-v2-field="qq" />
          </label>
          <label class="cv2-field profile-linked">
            <span class="cv2-field-label">手机号</span>
            <el-input v-model="form.mobile"  type="text" :disabled="!profileLoaded" placeholder="请输入手机号" data-v2-field="mobile" />
          </label>
          <label class="cv2-field cv2-field--wide profile-linked">
            <span class="cv2-field-label">直播间地址</span>
            <el-input v-model="form.live_url"  type="text" :disabled="!profileLoaded" placeholder="请输入直播间地址" data-v2-field="live_url" />
          </label>
          <div class="cv2-field cv2-field--wide profile-linked">
            <span class="cv2-field-label">游戏权限</span>
            <div class="cv2-chips" data-v2="games">
              <span
                class="cv2-chip"
                :class="{ 'cv2-chip--on': gamesAllChecked }"
                data-v2-game="all"
              >全部游戏</span>
              <span
                v-for="g in games"
                :key="g.id"
                class="cv2-chip"
                :class="{ 'cv2-chip--on': gameChecked(g.id) }"
                :data-v2-game="g.name"
              >{{ g.name }}</span>
            </div>
          </div>
        </div>
        <div class="cv2-form-actions">
          <button
            type="button"
            class="cv2-btn cv2-btn--primary cv2-btn--commit"
            data-v2="save-profile"
            :disabled="!profileLoaded"
            @click="saveProfile"
          >
            保存更改
          </button>
        </div>
      </section>
    </template>

    <!-- ══ 功能设置：下家鸡蛋铺绑定（全量照搬旧页那一块）══ -->
    <template v-else-if="activeTab === 'features'">
      <p class="cv2-note cv2-note--alert" data-v2="binding-intro">
        绑定你自己那家下家鸡蛋铺的账号：绑定并启用之后，本系统指派给你的单会同步过去，两边的订单状态会互相同步。
      </p>

      <!-- 「一步步开通（3 分钟）」向导（默认收起；顺序承重：先保存 → 再测试 → 再选来源 → 再保存） -->
      <section class="profile-guide" data-v2="guide" aria-label="一步步开通">
        <div class="profile-guide-head"><b>一步步开通</b><span>约 3 分钟</span></div>
        <div class="profile-guide-steps">
          <button v-for="(step, index) in guideSteps" :key="step" type="button" class="profile-guide-step"
            :class="{ 'profile-guide-step--active': guideStep === index + 1 }" :aria-expanded="guideStep === index + 1"
            :data-v2-guide-step="index + 1" @click="guideStep = guideStep === index + 1 ? 0 : index + 1">
            <span>{{ index + 1 }}</span>{{ step }}
          </button>
        </div>
        <div v-if="guideStep" class="profile-guide-detail" data-v2="guide-detail">
          <div v-if="guideStep === 1">
            在鸡蛋铺建一个<b>客服账号</b>（建议专开一个，别用老板号），要有「订单管理」基础功能；另外勾上「<b>系统日志</b>」——它只用来两边对账同步，不做别的。
            <figure>
              <img :src="`${guideBase}guide/jidanpu-perm-system-log.png`" alt="鸡蛋铺「角色职位权限 → 修改权限」里「系统操作日志」的勾选位置" />
              <figcaption>① 系统维护 → 角色职位权限 → 修改权限 → 勾上「系统操作日志」</figcaption>
            </figure>
          </div>
          <div v-if="guideStep === 2">
            鸡蛋铺 → 系统维护 → <b>订单来源管理 → 新增来源</b>：名称和描述都填「<b>团团通系统</b>」，平台选「<b>其它平台</b>」。
            <figure>
              <img :src="`${guideBase}guide/jidanpu-source-add.png`" alt="鸡蛋铺「订单来源管理 → 新增来源」：名称与描述填「团团通系统」、平台选「其它平台」" />
              <figcaption>② 系统维护 → 订单来源管理 → 新增一个来源（名称/描述「团团通系统」、平台「其它平台」）</figcaption>
            </figure>
          </div>
          <div v-if="guideStep === 3">回到团团通：选「<b>账号 + 密码</b>」→ 填网址和账号密码 → <b>先保存</b> → <b>再测试连接</b> → <b>再选来源</b>（选刚建的那个）→ <b>再保存</b>。</div>
          <div v-if="guideStep === 4">「大厅抛单百分比」填 <b>100</b>，勾上「<b>启用</b>」，保存。</div>
          <button v-if="guideStep < guideSteps.length" type="button" class="cv2-btn" data-v2="guide-next" @click="guideStep++">下一步</button>
        </div>
      </section>

      <section class="cv2-panel" aria-label="下家鸡蛋铺绑定">
        <div class="cv2-form-grid">
          <label class="cv2-field profile-linked">
            <span class="cv2-field-label">对接方式</span>
            <select v-model="b.mode" class="cv2-input" data-v2-field="member-mode" :disabled="!bindingLoaded">
              <option v-for="o in MODE_OPTIONS" :key="o.value" :value="o.value" :disabled="o.disabled" :hidden="o.value === MEMBER_MODE_OPENAPI">
                {{ o.label }}
              </option>
            </select>
          </label>
          <label class="cv2-field cv2-field--wide profile-linked">
            <span class="cv2-field-label">鸡蛋铺地址</span>
            <el-input
              v-model="b.baseUrl"

              type="text"
              maxlength="255"
              placeholder="请输入鸡蛋铺地址（https://）"
              :disabled="!bindingLoaded"
              data-v2-field="member-base-url"
            />
          </label>

          <!-- ②式那三格（紧随地址之后）：只切可见性（v-show），⛔ 不清值 -->
          <label v-show="isOpenapiMode" class="cv2-field profile-linked">
            <span class="cv2-field-label">client_id</span>
            <el-input
              v-model="b.clientId"

              type="text"
              maxlength="150"
              placeholder="鸡蛋铺给你的 client_id"
              :disabled="!bindingLoaded"
              data-v2-field="member-client-id"
            />
          </label>
          <label v-show="isOpenapiMode" class="cv2-field profile-linked">
            <span class="cv2-field-label">client_secret</span>
            <el-input
              v-model="b.clientSecret"

              type="password"
              maxlength="255"
              autocomplete="new-password"
              :placeholder="clientSecretPlaceholder"
              :disabled="!bindingLoaded"
              data-v2-field="member-client-secret"
            />
          </label>
          <div v-show="isOpenapiMode" class="cv2-field cv2-field--wide">
            <span class="cv2-field-label">回调地址</span>
            <span class="cv2-field-hint">{{ MEMBER_CALLBACK_HELP_TEXT }}</span>
            <div v-if="callbackCell.url !== ''" class="cv2-callback-row" data-v2="callback-row">
              <el-input  type="text" readonly :model-value="callbackCell.url" data-v2="callback-url" />
              <button type="button" class="cv2-btn cv2-btn--quiet" data-v2="copy-callback" @click="copyCallbackUrl">复制</button>
            </div>
            <span v-if="callbackCell.reason !== ''" class="cv2-field-hint cv2-field-hint--warn" data-v2="callback-reason">
              {{ callbackCell.reason }}
            </span>
          </div>

          <!-- ①式：账号 / 密码 -->
          <label v-show="isPasswordMode" class="cv2-field profile-linked">
            <span class="cv2-field-label">账号</span>
            <el-input
              v-model="b.account"

              type="text"
              maxlength="150"
              placeholder="请输入鸡蛋铺专用账号（登录名）"
              :disabled="!bindingLoaded"
              data-v2-field="member-account"
            />
          </label>
          <label v-show="isPasswordMode" class="cv2-field profile-linked">
            <span class="cv2-field-label">密码</span>
            <el-input
              v-model="b.password"

              type="password"
              maxlength="255"
              autocomplete="new-password"
              :placeholder="passwordPlaceholder"
              :disabled="!bindingLoaded"
              data-v2-field="member-password"
            />
          </label>

          <!-- 订单来源名（下拉，D-0177；只在①式显示——②式探针不返回来源字典） -->
          <label v-show="isPasswordMode" class="cv2-field profile-linked">
            <span class="cv2-field-label">订单来源名</span>
            <select v-model="b.sourceName" class="cv2-input" data-v2-field="member-source" :disabled="!bindingLoaded">
              <option v-for="label in sourceOptions" :key="label" :value="label">{{ label }}</option>
            </select>
            <span v-if="sourceHint.warn" class="cv2-field-hint cv2-field-hint--warn" data-v2="source-hint">
              {{ sourceHint.text }}
            </span>
          </label>

          <label class="cv2-field profile-linked">
            <span class="cv2-field-label">大厅抛单百分比</span>
            <el-input
              v-model="b.throwPercent"

              type="number"
              min="1"
              max="100"
              step="1"
              placeholder="1–100，仅影响大厅镜像"
              title="仅影响大厅镜像；指派或自行接单不打折，范围 1–100"
              :disabled="!bindingLoaded"
              data-v2-field="member-throw-percent"
            />
          </label>

          <div class="cv2-field cv2-field--wide profile-linked">
            <span class="cv2-field-label">同步大厅订单</span>
            <label class="cv2-check">
              <input v-model="b.hallSync" type="checkbox" :disabled="!bindingLoaded" data-v2-field="member-hall-sync" />
              {{ b.hallSync ? '开' : '关' }}
            </label>
            <span class="cv2-field-hint">{{ MEMBER_HALL_SYNC_HINT }}</span>
          </div>
          <div class="cv2-field profile-linked">
            <span class="cv2-field-label">启用</span>
            <label class="cv2-check">
              <input v-model="b.enabled" type="checkbox" :disabled="!bindingLoaded" data-v2-field="member-enabled" />
              {{ b.enabled ? '启用' : '停用' }}
            </label>
          </div>
        </div>

        <div class="cv2-form-actions">
          <button
            type="button"
            class="cv2-btn cv2-btn--primary cv2-btn--commit"
            data-v2="member-save"
            :disabled="!bindingLoaded"
            @click="saveMemberBinding"
          >
            保存
          </button>
          <button
            type="button"
            class="cv2-btn"
            data-v2="member-test"
            :disabled="!bindingLoaded"
            @click="testMemberBinding"
          >
            {{ MEMBER_TEST_BUTTON_TEXT }}
          </button>
        </div>

        <!-- ②式「测试连接」的逐条权限表（没点过就什么都不显示；拿不到 checks 退回逐条 messages） -->
        <div v-if="isOpenapiMode && testResult !== null" class="cv2-test-result" data-v2="member-test-result">
          <table v-if="testResult.checks.length > 0" class="cv2-table">
            <thead>
              <tr>
                <th v-for="title in MEMBER_CHECK_COLUMNS" :key="title">{{ title }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="check in testResult.checks" :key="check.step" :data-v2-check="check.step">
                <td>{{ check.label }}</td>
                <td :data-v2-check-result="check.step">{{ checkResultText(check) }}</td>
                <td>
                  {{ check.reason }}
                  <div v-if="check.message !== ''">{{ check.message }}</div>
                </td>
                <td>{{ check.action }}</td>
              </tr>
            </tbody>
          </table>
          <template v-else>
            <p v-for="(text, i) in testResult.messages" :key="i" class="cv2-field-hint">{{ text }}</p>
            <p v-if="testResult.messages.length === 0" class="cv2-field-hint cv2-field-hint--warn">
              {{ MEMBER_TEST_EMPTY_RESULT_TEXT }}
            </p>
          </template>
        </div>

        <!-- 健康状态（空态要如实：还没跑过同步 ≠ 正常） -->
        <dl class="cv2-info-list" data-v2="member-health">
          <div v-for="cell in healthRows" :key="cell.label" class="cv2-info-row">
            <dt class="cv2-info-label">{{ cell.label }}</dt>
            <dd class="cv2-info-value"><b>{{ cell.value }}</b></dd>
          </div>
        </dl>
      </section>
    </template>

    <!-- ══ 修改密码 ══ -->
    <template v-else>
      <section class="cv2-panel" aria-label="修改密码">
        <div class="cv2-form-grid">
          <label class="cv2-field profile-linked">
            <span class="cv2-field-label">原密码</span>
            <el-input v-model="oldPwd"  type="password" autocomplete="current-password" placeholder="请输入原密码" data-v2-field="oldPwd" />
          </label>
          <label class="cv2-field profile-linked">
            <span class="cv2-field-label">新密码</span>
            <el-input v-model="newPwd"  type="password" autocomplete="new-password" placeholder="请输入新密码" data-v2-field="newPwd" />
          </label>
          <label class="cv2-field profile-linked">
            <span class="cv2-field-label">确认新密码</span>
            <el-input v-model="renewPwd"  type="password" autocomplete="new-password" placeholder="请输入确认新密码" data-v2-field="renewPwd" />
          </label>
        </div>
        <div class="cv2-form-actions">
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="save-password" @click="savePassword">
            确认更改
          </button>
        </div>
      </section>
    </template>

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

    <!-- 收款码看图（短时链接 onerror 重取一次再试，仍失败关层 + 固定提示）；⭐ 弹窗档 = sm 480 -->
    <el-dialog
      v-model="qrDlg"
      title="微信收款码"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-qr"
      @update:model-value="qrDlg = false"
    >
      <div style="text-align: center">
        <img
          v-if="qrDlgSrc"
          :src="qrDlgSrc"
          alt="微信收款码"
          style="max-width: 460px; max-height: 460px"
          data-v2="qr-img"
          @error="onQrImgError"
        />
      </div>
    </el-dialog>

    <!-- 实名认证弹层（提交前必看计费提示；结果留在屏上——这是花了一次钱的动作）；⭐ 弹窗档 = md 640。
         ⭐ 2026-11-01 第二轮「弹窗全局统一」：主行动按钮收进 footer 右下（.cv2-dlg-actions），结果行留在体末。 -->
    <el-dialog
      v-model="rnDlg"
      title="实名认证"
      class="cv2-dialog cv2-dialog--md"
      destroy-on-close
      data-v2="dlg-realname"
      @update:model-value="rnDlg = false"
    >
      <p class="cv2-note" style="margin-top: 0">
        请填写本人真实信息，虚假信息将可能导致商家无法给你结算，只支持中国大陆身份
      </p>
      <div class="cv2-form-grid">
        <label class="cv2-field profile-linked">
          <span class="cv2-field-label">真实姓名</span>
          <el-input v-model="rnName"  type="text" placeholder="您的真实姓名" data-v2-field="rn-name" />
        </label>
        <label class="cv2-field profile-linked">
          <span class="cv2-field-label">身份证号</span>
          <el-input v-model="rnIdCardNo"  type="text" placeholder="您的身份证号" data-v2-field="rn-sid" />
        </label>
      </div>
      <p class="cv2-field-hint cv2-field-hint--warn" data-v2="rn-billing">{{ REALNAME_BILLING_NOTICE }}</p>
      <p v-if="rnResult !== ''" class="cv2-note" style="font-weight: 600" data-v2="rn-result">{{ rnResult }}</p>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="rn-submit" @click="submitRealNameCert">
            开始认证
          </button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.profile-linked { grid-template-columns: 124px minmax(0, 1fr); align-items: stretch; gap: 0; min-width: 0; }
.profile-linked > .cv2-field-label { display: flex; align-items: center; min-height: 38px; padding: 0 10px; border: 1px solid var(--tt2-filter-line); border-right: 0; border-radius: 4px 0 0 4px; background: var(--tt2-filter-label-bg); color: var(--tt2-dim); font-size: 13px; font-weight: 400; white-space: nowrap; }
.profile-linked > .cv2-input { min-width: 0; min-height: 38px; border-color: var(--tt2-filter-line); border-radius: 0 4px 4px 0; }
.profile-linked > :deep(.el-input) { min-width: 0; --el-component-size: 38px; }
.profile-linked > :deep(.el-input .el-input__wrapper) { border-radius: 0 4px 4px 0; }
.profile-linked > :is(.cv2-realname-row, .cv2-chips, .cv2-check) { min-width: 0; min-height: 38px; padding: 0 12px; border: 1px solid var(--tt2-filter-line); border-radius: 0 4px 4px 0; background: var(--tt2-panel); }
.profile-linked > .cv2-field-hint { grid-column: 1 / -1; margin-top: 4px; }
@media (max-width: 360px) {
  .profile-linked { grid-template-columns: minmax(0, 1fr); }
  .profile-linked > .cv2-field-label { border-right: 1px solid var(--tt2-filter-line); border-radius: 4px 4px 0 0; }
  .profile-linked > :not(.cv2-field-label) { grid-column: 1; }
  .profile-linked > .cv2-input, .profile-linked > :is(.cv2-realname-row, .cv2-chips, .cv2-check), .profile-linked > :deep(.el-input) { margin-top: -1px; }
  .profile-linked > .cv2-input, .profile-linked > :is(.cv2-realname-row, .cv2-chips, .cv2-check) { border-radius: 0 0 4px 4px; }
  .profile-linked > :deep(.el-input .el-input__wrapper) { border-radius: 0 0 4px 4px; }
}
.cv2-form-grid .cv2-field :deep(.el-input) { width: 100%; min-width: 0; }
.cv2-form-grid .cv2-field :deep(.el-input__wrapper) { width: 100%; box-sizing: border-box; }
.cv2-form-grid .cv2-field :deep(.el-input__inner) { width: 100%; min-width: 0; }
.profile-guide { margin: 14px 0; padding: 14px 16px; border: 1px solid var(--tt2-line); border-radius: 4px; max-width: 880px; }
.profile-guide-head { display: flex; align-items: baseline; gap: 10px; color: var(--tt2-ink); }
.profile-guide-head span { color: var(--tt2-dim); font-size: 12px; }
.profile-guide-steps { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
.profile-guide-step { display: inline-flex; align-items: center; gap: 7px; padding: 6px 10px; border: 1px solid var(--tt2-line); border-radius: 4px; color: var(--tt2-ink); background: var(--tt2-panel); cursor: pointer; }
.profile-guide-step span { display: grid; place-items: center; width: 22px; height: 22px; border-radius: 50%; background: var(--tt2-btn-soft-bg); color: var(--tt2-dim); }
.profile-guide-step--active { border-color: var(--tt2-primary); color: var(--tt2-primary-deep); }
.profile-guide-step--active span { background: var(--tt2-primary); color: var(--tt2-panel); }
.profile-guide-detail { margin-top: 12px; padding: 12px; border-radius: 4px; background: var(--tt2-btn-soft-bg); color: var(--tt2-ink); line-height: 1.6; }
.profile-guide-detail figure { margin: 10px 0; }
.profile-guide-detail img { max-width: min(100%, 520px); max-height: 260px; object-fit: contain; border: 1px solid var(--tt2-line); }
.profile-guide-detail figcaption { color: var(--tt2-dim); font-size: 12px; }
.profile-guide-detail button { margin-top: 10px; }
.cv2-chips .cv2-chip { cursor: default; padding: 5px 12px; }
.cv2-chips .cv2-chip:not(.cv2-chip--on):hover { border-color: var(--tt2-line); }
</style>
