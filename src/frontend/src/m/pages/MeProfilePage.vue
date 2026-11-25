<script setup lang="ts">
import {
  f7Badge as F7Badge, f7Block as F7Block, f7BlockFooter as F7BlockFooter, f7BlockTitle as F7BlockTitle,
  f7Button as F7Button, f7List as F7List, f7ListInput as F7ListInput, f7ListItem as F7ListItem,
  f7Preloader as F7Preloader,
} from 'framework7-vue'
import MobilePageBar from '../components/MobilePageBar.vue'
import { useWorkerBack } from '../router/useWorkerBack'
import { computed, onMounted, ref } from 'vue'
import { Toast } from '../framework7Ui'
import MobilePhotoBrowser from '../components/MobilePhotoBrowser.vue'
import {
  claimMedia, fetchMyProfile, patchMyProfile, requestMediaTicket,
  type MediaKind, type MyProfile, type MyProfilePatch,
} from '@/api/myProfile'
import { verifyRealName } from '@/api/realname'
import { BizError, HttpError } from '@/api/errors'
import { ErrorCode } from '@/config/errorCodes'
import { avatarDataUri } from '@/config/avatar'
import { MAX_USER_MEDIA_BYTES, compressImage, mediaQuality, userMediaTicketExt } from '@/layui-island/imageCompress'
import { putFileToStorage } from '@/layui-island/directUpload'
import {
  MEDIA_BAD_EXT_TEXT, MEDIA_FAILED_TEXT, MEDIA_TOO_LARGE_TEXT, MEDIA_UPLOADED_TEXT,
  PROFILE_LOAD_FAILED_TEXT, PROFILE_SAVED_TEXT, REALNAME_BILLING_NOTICE,
  REALNAME_FAIL_TEXT, REALNAME_FORM_NOTICE, REALNAME_INVALID_TEXT,
  REALNAME_MISSING_INPUT_TEXT, REALNAME_NAME_LABEL, REALNAME_NAME_PLACEHOLDER,
  REALNAME_NOT_TEXT, REALNAME_PASS_TEXT, REALNAME_SID_LABEL, REALNAME_SID_PLACEHOLDER,
  REALNAME_START_BUTTON_TEXT, REALNAME_SUBMIT_TEXT, WECHAT_QR_IDLE_TEXT,
  WECHAT_QR_REUPLOAD_TEXT,
} from '../texts/profile'

/**
 * 我的资料（手机端 `/m/me/profile`，spec §2）。端点与桌面同一套（`GET/PATCH /api/auth/profile` 只认登录；路由门 = `my_profile`）。**7 个可编辑输入框 + 「保存更改」在取数回填前 `disabled`，回填成功才解锁，取数失败保持锁定**——不锁的话窗口里打进去的字会被服务端旧值静默覆盖，再保存就把旧值写回去还报「保存成功」（桌面 `user-myinfo.ts::LOADING_LOCKED_CONTROLS` 同一条）。可编辑 7 键的 label / `maxlength` 逐字取桌面 `user-myinfo.html`；保存 = `PATCH` 全部 7 键（不分「改没改」），成功用**响应**回填 + toast。
 * 头像 / 微信收款码（10MB + 压缩，桌面 D-0153 ②③ 同一口径）：选文件 → 扩展名早挡 → **10MB 闸在票据请求之前**（后端在认领与直传两处是权威，真拒了原样显示它的 msg）→ `compressImage`（收款码 `max(四档, 0.8)`——给人扫的，压狠了扫不出来）→ `ticket ⇒ PUT ⇒ claim`（复用桌面线模块 ⛔ 不复制）→ toast「上传成功」→ **只刷新那一格**，刷新失败**静默**（认领已成功，不能让一句错误盖掉「上传成功」）。
 * 实名区块照桌面 `openRealNameCert`：已认证 ⇒ 绿色 + 打码号；未认证 ⇒ 红色「未实名」，**仅当** `realname.enabled`（`?? false`，fail-closed 不显示入口）显示「发起核验」→ 表单（**计费提示一字不许省**）→ `verifyRealName` 结果**留在屏幕上**（花了一次钱的动作）；业务码分支照桌面 `realNameErrorText`（1025/1026/1027/1017/1000），其余 msg 原样。
 */

const goBack = useWorkerBack(() => ({ name: 'm-me' }))

/* ── 7 个可编辑字段（label / maxlength 逐字取桌面 user-myinfo.html） ── */
const FIELDS: ReadonlyArray<{ key: keyof MyProfilePatch; label: string; maxlength: number }> = [
  { key: 'alipay_account', label: '结算支付宝账号', maxlength: 32 },
  { key: 'alipay_name', label: '结算支付宝姓名', maxlength: 50 },
  { key: 'bank_name', label: '结算银行名称', maxlength: 32 },
  { key: 'bank_card_no', label: '结算银行卡号', maxlength: 100 },
  { key: 'qq', label: 'QQ/微信', maxlength: 20 },
  { key: 'mobile', label: '联系电话', maxlength: 11 },
  { key: 'live_url', label: '我的直播地址', maxlength: 500 },
]

const FIELD_GROUPS = [
  { label: '结算资料', fields: FIELDS.slice(0, 4) },
  { label: '联系信息', fields: FIELDS.slice(4) },
]

const EMPTY_FORM: Record<keyof MyProfilePatch, string> = {
  alipay_account: '', alipay_name: '', bank_name: '', bank_card_no: '',
  qq: '', mobile: '', live_url: '',
}

/* ── 取数与加载锁 ── */
const loadState = ref<'loading' | 'ready' | 'error'>('loading')
const loadError = ref('')
/** 加载锁：回填成功才解锁，取数失败保持锁定（见文件头；保存 / 上传不受它管）。 */
const locked = ref(true)

const form = ref<Record<keyof MyProfilePatch, string>>({ ...EMPTY_FORM })
/** 头像初值 = 「?」色块（取数回填后才换成真头像 / 昵称色块）。 */
const avatarSrc = ref(avatarDataUri('', 96))
const qrUrl = ref('')
/** 「是否实名」两态 + 那颗入口的显示条件（`realname.enabled`；键缺席按 false，fail-closed）。 */
const isRealName = ref(false)
const realNameEnabled = ref(false)
/**
 * 「已认证」时展示的身份证号 —— **前端打码**。`GET /api/auth/profile` 的 `id_card_no` 回的是**明文**
 * （后端 `profile_views` 没有脱敏；桌面把它塞进 `disabled` 输入框原样显示），后端零改动的红线内唯一路径 =
 * 前端按**后端自己的**脱敏规则打码后再显示（`src/apps/realname/views.py::mask_id_card`：前 3 位 + `***` + 后 2 位，短于 6 位 ⇒ `***`）。⛔ 只用于**展示**：库里仍存明文是后端已登记的残差，本页不做任何回传。
 */
const maskedIdCard = ref('')

/** 身份证号的**展示脱敏**（与后端 `mask_id_card` 同一条规则，⛔ 别改成别的形状）。 */
function maskIdCard(idCardNo: string): string {
  if (idCardNo.length < 6) return '***'
  return `${idCardNo.slice(0, 3)}***${idCardNo.slice(-2)}`
}

function applyProfile(profile: MyProfile): void {
  for (const field of FIELDS) {
    form.value[field.key] = String(profile[field.key] ?? '')
  }
  avatarSrc.value = profile.avatar_url
    ?? avatarDataUri(profile.display_name || profile.username, 96)
  qrUrl.value = profile.wechat_qr_url ?? ''
  isRealName.value = profile.is_real_name
  realNameEnabled.value = profile.realname?.enabled ?? false
  maskedIdCard.value = maskIdCard(profile.id_card_no ?? '')
}

/**
 * 取数（⛔ 加载期锁定的可见形态：**表单常驻**、锁定 = 输入框 + 保存 disabled——
 * 与桌面同款「失败也保持锁定」，不是错误分支整页替换；重试成功才解锁）。
 */
async function load(): Promise<void> {
  loadError.value = ''
  loadState.value = 'loading'
  locked.value = true
  try {
    applyProfile(await fetchMyProfile())
    loadState.value = 'ready'
    locked.value = false
  } catch (e) {
    loadState.value = 'error'
    loadError.value = errText(e)
    // ⛔ 保持 locked：拿不到旧值就不该在一个会被覆盖的表单上放行（桌面同口径）。
  }
}

/** 保存（PATCH 全部 7 键，桌面同款）；成功用**响应**回填（后端会归一化）。 */
const saving = ref(false)

onMounted(() => {
  void load()
})
async function save(): Promise<void> {
  if (saving.value || locked.value) return
  saving.value = true
  try {
    const payload: Partial<MyProfilePatch> = {}
    for (const field of FIELDS) payload[field.key] = form.value[field.key]
    applyProfile(await patchMyProfile(payload))
    Toast(PROFILE_SAVED_TEXT)
  } catch (e) {
    Toast.error(errText(e))
  } finally {
    saving.value = false
  }
}

/* ── 头像 / 收款码上传（10MB 闸 → 压缩 → ticket ⇒ PUT ⇒ claim → 只刷那一格） ── */

const ALLOWED_EXTS = ['jpg', 'jpeg', 'png'] as const
const avatarInput = ref<HTMLInputElement | null>(null)
const qrInput = ref<HTMLInputElement | null>(null)
/** 上传在途闸（两种媒体共用一条链路，串行防重复选同一张）。 */
const uploading = ref(false)

function extOf(name: string): string {
  return (name.split('.').pop() ?? '').toLowerCase()
}

function errText(e: unknown): string {
  return e instanceof Error && e.message ? e.message : PROFILE_LOAD_FAILED_TEXT
}

async function handleMediaPick(kind: MediaKind, event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  try {
    const file = input.files?.[0]
    if (!file || uploading.value) return
    const ext = extOf(file.name)
    if (!(ALLOWED_EXTS as readonly string[]).includes(ext)) {
      Toast(MEDIA_BAD_EXT_TEXT)
      return
    }
    // ① 10MB 闸在**发起票据请求之前**（早反馈、零请求）；后端两处是权威，拒了走下面的 catch。
    if (file.size > MAX_USER_MEDIA_BYTES) {
      Toast(MEDIA_TOO_LARGE_TEXT)
      return
    }
    uploading.value = true
    // ② 压缩：头像四档；收款码 max(四档, 0.8)（给人扫的）。失败回落原图（同一个 File）。
    const prepared = await compressImage(file, mediaQuality(kind, file.size))
    // ③ 票据 ext 用**压缩后**的形态（成功一律 JPEG ⇒ jpg；回落原图 ⇒ 原扩展名）。
    const ticket = await requestMediaTicket(kind, userMediaTicketExt(file, prepared))
    await putFileToStorage(ticket.upload_url, prepared)
    await claimMedia(kind, ticket.key)
    Toast(MEDIA_UPLOADED_TEXT)
    // 只刷新那一格（⛔ 不整页重取——7 个输入框里用户可能正在填）；刷新失败**静默**：
    // 认领已成功，不能让一句错误把「上传成功」盖掉。
    try {
      const fresh = await fetchMyProfile()
      if (kind === 'avatar') {
        if (fresh.avatar_url) avatarSrc.value = fresh.avatar_url
      } else {
        qrUrl.value = fresh.wechat_qr_url ?? ''
      }
    } catch {
      // 刻意留空（桌面同款）：这一次刷新只是省掉手刷的优化。
    }
  } catch (e) {
    Toast.error(e instanceof Error && e.message ? e.message : MEDIA_FAILED_TEXT)
  } finally {
    input.value = ''
    uploading.value = false
  }
}

/* ── 收款码查看（两态；已上传才可见，点击放大） ── */
const qrViewerVisible = ref(false)

const uploadedQr = computed(() => qrUrl.value !== '')

/* ── 实名区块（桌面 openRealNameCert 同口径） ── */

const realNameFormVisible = ref(false)
const rnName = ref('')
const rnSid = ref('')
/** 结果行：**留在屏幕上**（花了一次钱的动作，用户要能核对）；空串 = 还没提交过。 */
const rnResult = ref('')
const rnSubmitting = ref(false)

function toggleRealNameForm(): void {
  realNameFormVisible.value = !realNameFormVisible.value
  rnResult.value = ''
}

/**
 * 业务码 → 人话（照桌面 `user-myinfo.ts::realNameErrorText` **逐字**；按码分开发话，
 * ⛔ 不用一句「核验失败」把「该找谁」抹掉）。1017 是兜底（这条路径今天走不到）。
 */
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
        // D-0251：同一用户提交过于频繁；不锁定，窗口滚过自动恢复。
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

/** 结果行文案带余量（`null` = 读不到 ⇒ 不假装是 0，E-0021；桌面同一条三目）。 */
function resultWithRemaining(text: string, remaining: number | null): string {
  return remaining === null ? text : `${text}（剩余核验次数 ${remaining} 次）`
}

/** 前端只做「填没填全」早挡（格式 / 校验位一律后端判，⛔ 不抄身份证正则）。 */
async function submitRealName(): Promise<void> {
  if (rnSubmitting.value) return
  const name = rnName.value.trim()
  const idCardNo = rnSid.value.trim()
  if (name === '' || idCardNo === '') {
    rnResult.value = REALNAME_MISSING_INPUT_TEXT
    return
  }
  rnSubmitting.value = true
  try {
    const result = await verifyRealName({ name, id_card_no: idCardNo })
    if (result.result === 'pass') {
      // 徽标就地翻已认证；表单**不收起**（结果必须留在屏幕上让人核对——花了一次钱的动作，
      // 桌面层也不关）。「点击进行实名认证」入口会因 isRealName=true 自动消失。
      isRealName.value = true
      rnName.value = ''
      rnSid.value = ''
      rnResult.value = resultWithRemaining(REALNAME_PASS_TEXT, result.remaining)
      return
    }
    // fail：这次也照样扣了；页面状态不动（没通过就是没通过）。
    rnResult.value = resultWithRemaining(REALNAME_FAIL_TEXT, result.remaining)
  } catch (e) {
    if (e instanceof HttpError && e.status === 403) {
      // 后端 403 时 `request()` 不带业务码（无节点 / 平面门）⇒ 原样显示它那句。
      rnResult.value = errText(e)
      return
    }
    rnResult.value = realNameErrorText(e)
  } finally {
    rnSubmitting.value = false
  }
}
</script>

<template>
  <section class="m-meprofile">
    <MobilePageBar title="我的资料" back-test-id="profile-back" @back="goBack" />

    <F7Block v-if="loadState === 'loading'" inset class="m-meprofile__status" role="status" data-testid="profile-loading">
      <F7Preloader size="20" aria-hidden="true" /> 加载中…
    </F7Block>
    <F7Block v-else-if="loadState === 'error'" inset class="m-meprofile__status" data-testid="profile-error">
      <p role="alert">{{ loadError }}</p>
      <F7Button type="button" round outline small data-testid="profile-retry" @click="load">重试</F7Button>
    </F7Block>

    <F7BlockTitle>头像与收款码</F7BlockTitle>
    <F7List strong inset outline dividers class="m-meprofile__media" data-testid="profile-media-card">
      <F7ListItem title="头像">
        <template #media>
          <img class="m-meprofile__avatar" :src="avatarSrc" alt="头像" data-testid="profile-avatar" />
        </template>
        <template #after>
          <F7Button type="button" small tonal round :disabled.prop="uploading" data-testid="profile-avatar-btn" @click="avatarInput?.click()">
            更换头像
          </F7Button>
          <input ref="avatarInput" type="file" accept="image/jpeg,image/png" hidden data-testid="profile-avatar-input"
            @change="handleMediaPick('avatar', $event)" />
        </template>
      </F7ListItem>
      <F7ListItem title="微信收款码">
        <template #after>
          <F7Button v-if="uploadedQr" type="button" small data-testid="profile-qr-view" @click="qrViewerVisible = true">
            已上传
          </F7Button>
          <F7Button type="button" small tonal round :disabled.prop="uploading" data-testid="profile-qr-btn" @click="qrInput?.click()">
            {{ uploadedQr ? WECHAT_QR_REUPLOAD_TEXT : WECHAT_QR_IDLE_TEXT }}
          </F7Button>
          <input ref="qrInput" type="file" accept="image/jpeg,image/png" hidden data-testid="profile-qr-input"
            @change="handleMediaPick('wechat-qr', $event)" />
        </template>
      </F7ListItem>
    </F7List>

    <section data-testid="profile-realname-card">
      <F7BlockTitle>实名认证</F7BlockTitle>
      <F7List strong inset outline dividers>
        <F7ListItem title="是否实名">
          <template #after>
            <F7Badge :color="isRealName ? 'green' : 'orange'" data-testid="profile-realname-state">
              {{ isRealName ? REALNAME_PASS_TEXT : REALNAME_NOT_TEXT }}
            </F7Badge>
          </template>
        </F7ListItem>
        <F7ListItem v-if="isRealName" title="身份证号" :after="maskedIdCard" data-testid="profile-realname-sid" />
        <F7ListItem v-if="realNameEnabled && !isRealName">
          <F7Button type="button" data-testid="profile-realname-open" @click="toggleRealNameForm">
            {{ REALNAME_START_BUTTON_TEXT }}
          </F7Button>
        </F7ListItem>
      </F7List>

      <section v-if="realNameFormVisible" data-testid="profile-realname-form">
        <F7Block inset data-testid="profile-realname-notice">{{ REALNAME_FORM_NOTICE }}</F7Block>
        <F7List strong inset outline dividers>
          <F7ListInput v-model:value="rnName" input-id="m-profile-realname-name" type="text" autocomplete="off"
            :placeholder="REALNAME_NAME_PLACEHOLDER" data-testid="profile-realname-name">
            <template #label><label for="m-profile-realname-name">{{ REALNAME_NAME_LABEL }}</label></template>
          </F7ListInput>
          <F7ListInput v-model:value="rnSid" input-id="m-profile-realname-sid" type="text" autocomplete="off"
            :placeholder="REALNAME_SID_PLACEHOLDER" maxlength="18" data-testid="profile-realname-sid">
            <template #label><label for="m-profile-realname-sid">{{ REALNAME_SID_LABEL }}</label></template>
          </F7ListInput>
        </F7List>
        <F7Block inset>
          <F7BlockFooter class="m-meprofile__billing" data-testid="profile-realname-billing">{{ REALNAME_BILLING_NOTICE }}</F7BlockFooter>
          <F7Button type="button" round fill :disabled.prop="rnSubmitting" data-testid="profile-realname-submit" @click="submitRealName">
            {{ REALNAME_SUBMIT_TEXT }}
          </F7Button>
          <F7BlockFooter v-if="rnResult !== ''" data-testid="profile-realname-result" role="status">{{ rnResult }}</F7BlockFooter>
        </F7Block>
      </section>
    </section>

    <section data-testid="profile-form-card">
      <template v-for="group in FIELD_GROUPS" :key="group.label">
        <F7BlockTitle>{{ group.label }}</F7BlockTitle>
        <F7List strong inset outline dividers>
          <F7ListInput
            v-for="field in group.fields"
            :key="field.key"
            v-model:value="form[field.key]"
            :input-id="`m-profile-${field.key}`"
            type="text"
            :maxlength="field.maxlength"
            :disabled="locked"
            :data-testid="`profile-field-${field.key}`"
          >
            <template #label><label :for="`m-profile-${field.key}`">{{ field.label }}</label></template>
          </F7ListInput>
        </F7List>
      </template>
      <F7Block inset>
        <F7Button type="button" large round fill :disabled.prop="locked || saving" data-testid="profile-save" @click="save">
          保存更改
        </F7Button>
      </F7Block>
    </section>

    <MobilePhotoBrowser v-model:visible="qrViewerVisible" :images="[qrUrl]" data-testid="profile-qr-viewer" />
  </section>
</template>

<style scoped>
.m-meprofile {
  min-height: 100dvh;
  padding-bottom: 16px;
}

.m-meprofile__status {
  text-align: center;
  color: var(--m-muted);
}

.m-meprofile__billing {
  margin: 0 0 12px;
}

.m-meprofile__media :deep(.item-after) {
  gap: 6px;
}

.m-meprofile__media :deep(.item-after .button) {
  flex: none;
  inline-size: auto;
}

.m-meprofile__avatar {
  display: block;
  width: 48px;
  height: 48px;
  border-radius: 50%;
  object-fit: cover;
}
</style>
