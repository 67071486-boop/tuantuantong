<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  f7Block as F7Block,
  f7BlockTitle as F7BlockTitle,
  f7Button as F7Button,
  f7List as F7List,
  f7ListInput as F7ListInput,
  f7ListItem as F7ListItem,
  f7LoginScreenTitle as F7LoginScreenTitle,
  f7Preloader as F7Preloader,
} from 'framework7-vue'
import {
  fetchSelfRegisterScreen,
  submitSelfRegister,
  type SelfRegisterScreen,
} from '@/api/selfRegistration'

/**
 * 手机端**自助注册**页（spec §5，`docs/specs/2026-10-16-self-registration-links-design.md`）。**免登录公开页**（`requiresAuth: false`，唯一先例 `m-login`）；令牌从 query `?tk=` 读，有效性判据在**服务端**（首屏 `GET register/?tk=` 回 `valid`）。
 * **四态**：loading（校验中，⛔ 不渲染成失效态——空结果不等于没有数据）→ invalid（失效态，附「去登录」）→ form（有效首屏：工作室名 + 手机号 / 收款支付宝两栏 / 游戏勾选 + 注册）→ success（成功态三行：登录账号 / 账号名称 / 初始密码——全部来自响应，⛔ 前端不硬编码、不自拼编号；约 6 秒倒计时自动跳 `/m/login?notice=self-registered`，也可手动点「去登录」）。
 * 提交失败 ⇒ 展示**信封 msg 原样**（⛔ 不自编文案——E-0087 同族）；页面自身的三条预检文案**带字段名**（E-0074 口诀）。⛔ 不做「自设密码」「短信验证」等额外功能（spec §10 划定的边界）。
 */

/** 注册页四态（见文件头）。 */
type Phase = 'loading' | 'invalid' | 'form' | 'success'

const route = useRoute()
const router = useRouter()

const phase = ref<Phase>('loading')
const tenantName = ref('')
const games = ref<{ id: number; name: string }[]>([])
const tk = ref('')
/** 失效态的文案：默认是 spec §5 那句；首屏**取数失败**时换成真实原因（⛔ 不把网络错误说成"链接失效"）。 */
const invalidText = ref('链接已失效或已被使用，请联系分享链接的人事重新获取')

const mobile = ref('')
const alipayAccount = ref('')
const alipayName = ref('')
const chosen = ref<number[]>([])
function setGame(id: number, checked: boolean): void {
  chosen.value = checked ? [...chosen.value, id] : chosen.value.filter((selected) => selected !== id)
}

const submitting = ref(false)
/** 提交失败的信息（信封 msg 原样；成功/校验中为空串）。 */
const errorMsg = ref('')

const initialPassword = ref('')
/** 账号名称（= 响应里的 `display_name`：服务端算好的编号或支付宝姓名，spec §2.1）。 */
const accountDisplayName = ref('')
const successMobile = ref('')
const countdown = ref(6)
let timer: ReturnType<typeof setInterval> | null = null

/** 手机号形状 = 大陆 11 位（spec §3.3 的取舍原文；要放宽是后端一行事，前端同批改）。 */
const MOBILE_RE = /^1[3-9]\d{9}$/

function stopCountdown(): void {
  if (timer !== null) {
    clearInterval(timer)
    timer = null
  }
}

/** 去 m 端登录页（带 `notice=self-registered`，登录页据此出横幅）。 */
function goLogin(): void {
  stopCountdown()
  void router.replace({ path: '/login', query: { notice: 'self-registered' } })
}

/** 成功后的倒计时（约 6 秒，spec §5 原话「约 6 秒」）；到 0 自动跳。 */
function startCountdown(): void {
  stopCountdown()
  countdown.value = 6
  timer = setInterval(() => {
    countdown.value -= 1
    if (countdown.value <= 0) goLogin()
  }, 1000)
}

onUnmounted(stopCountdown)

onMounted(async () => {
  // query 里的值可能是数组（同名参数重复时）——只认字符串形态，其余按空令牌发（服务端判失效）。
  const token = typeof route.query.tk === 'string' ? route.query.tk : ''
  tk.value = token
  try {
    const screen: SelfRegisterScreen = await fetchSelfRegisterScreen(token)
    if (screen.valid) {
      phase.value = 'form'
      tenantName.value = screen.tenant_name
      games.value = screen.games
    } else {
      phase.value = 'invalid'
    }
  } catch (e) {
    // 首屏取数失败（网络/限流/500）：如实说失败原因，⛔ 不冒充"链接已失效"。
    phase.value = 'invalid'
    invalidText.value = e instanceof Error ? e.message : '链接校验失败，请稍后再试'
  }
})

/** 提交前的**体验层**预检（四条文案都带字段名；服务端 §3.3/§2.1 还会再判一次同样的规则）。 */
function precheckError(): string {
  if (!MOBILE_RE.test(mobile.value.trim())) return '手机号需为 11 位数字（1 开头）'
  if (alipayAccount.value.trim() === '') return '请填写收款支付宝账号'
  if (alipayName.value.trim() === '') return '请填写收款支付宝姓名（实名）'
  if (chosen.value.length === 0) return '请至少勾选一款游戏'
  return ''
}

async function submit(): Promise<void> {
  if (submitting.value) return
  const problem = precheckError()
  if (problem !== '') {
    errorMsg.value = problem
    return
  }
  submitting.value = true
  errorMsg.value = ''
  try {
    const done = await submitSelfRegister({
      tk: tk.value,
      mobile: mobile.value.trim(),
      alipay_account: alipayAccount.value.trim(),
      alipay_name: alipayName.value.trim(),
      game_ids: [...chosen.value],
    })
    initialPassword.value = done.initial_password
    successMobile.value = done.username
    accountDisplayName.value = done.display_name
    phase.value = 'success'
    startCountdown()
  } catch (e) {
    // ⛔ 信封 msg 原样展示（该手机号已注册 / 链接已失效 / 限流……），不自编。
    errorMsg.value = e instanceof Error ? e.message : '注册失败'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="m-self-register">
    <F7LoginScreenTitle class="m-self-register__heading">
      <h1>团团通 · 打手注册</h1>
    </F7LoginScreenTitle>

    <F7Block v-if="phase === 'loading'" class="m-self-register__loading" role="status">
      <F7Preloader :size="18" aria-hidden="true" />
      正在校验链接…
    </F7Block>

    <F7Block v-else-if="phase === 'invalid'" class="m-self-register__state">
      <p role="alert">{{ invalidText }}</p>
      <F7Button type="button" round fill large @click="goLogin">去登录</F7Button>
    </F7Block>

    <form v-else-if="phase === 'form'" novalidate @submit.prevent="submit">
      <F7Block class="m-self-register__studio">{{ tenantName }}</F7Block>
      <F7List inset strong outline dividers class="m-self-register__fields">
        <F7ListInput
          input-id="m-register-mobile"
          v-model:value="mobile"
          name="mobile"
          type="text"
          inputmode="numeric"
          maxlength="11"
          placeholder="请输入 11 位手机号"
          autocomplete="tel"
          :disabled="submitting"
        >
          <template #label><label for="m-register-mobile">手机号（注册后就是登录账号）</label></template>
        </F7ListInput>
        <F7ListInput
          input-id="m-register-alipay-account"
          v-model:value="alipayAccount"
          name="alipay_account"
          type="text"
          maxlength="64"
          placeholder="用于结算打款的支付宝账号"
          :disabled="submitting"
        >
          <template #label><label for="m-register-alipay-account">收款支付宝账号</label></template>
        </F7ListInput>
        <F7ListInput
          input-id="m-register-alipay-name"
          v-model:value="alipayName"
          name="alipay_name"
          type="text"
          maxlength="64"
          placeholder="支付宝实名，用于核对收款人"
          :disabled="submitting"
        >
          <template #label><label for="m-register-alipay-name">收款支付宝姓名（实名）</label></template>
        </F7ListInput>
      </F7List>
      <F7BlockTitle class="m-self-register__games-title">游戏（至少勾一款，注册后按勾选开通）</F7BlockTitle>
      <F7List inset strong outline dividers class="m-self-register__fields m-self-register__games">
        <F7ListItem
          v-for="game in games"
          :key="game.id"
          :title="game.name"
          checkbox
          :value="game.id"
          :checked="chosen.includes(game.id)"
          :disabled="submitting"
          @update:checked="setGame(game.id, $event)"
        />
      </F7List>
      <p v-if="errorMsg !== ''" class="m-self-register__error" role="alert">{{ errorMsg }}</p>
      <F7Button round fill large type="submit" :disabled.prop="submitting" @click="submit">
        {{ submitting ? '注册中…' : '注册' }}
      </F7Button>
    </form>

    <F7Block v-else inset strong outline class="m-self-register__state">
      <p class="m-self-register__success-title">注册成功！</p>
      <!-- 成功页三行（spec §2.1）：账号名称 = 服务端算好的编号（未配置编号时是支付宝姓名），
           前端照响应渲染，⛔ 不自己拼编号。 -->
      <p class="m-self-register__password-line">
        登录账号：<b class="m-self-register__password">{{ successMobile }}</b>
      </p>
      <p class="m-self-register__password-line">
        账号名称：<b class="m-self-register__password">{{ accountDisplayName }}</b>
      </p>
      <p class="m-self-register__password-line">
        初始密码：<b class="m-self-register__password">{{ initialPassword }}</b>
      </p>
      <p class="m-self-register__hint">登录后可在『我的-修改密码』修改</p>
      <F7Button type="button" round fill large @click="goLogin">
        去登录（{{ countdown }} 秒后自动跳转）
      </F7Button>
    </F7Block>
  </div>
</template>

<style scoped>
.m-self-register {
  width: min(100%, 440px);
  min-height: 100vh;
  min-height: 100dvh;
  display: flex;
  flex-direction: column;
  justify-content: center;
  padding: 24px;
  box-sizing: border-box;
}

.m-self-register__heading {
  margin: 0 0 20px;
}

.m-self-register__heading h1 {
  margin: 0;
  font: inherit;
}

.m-self-register__studio {
  margin: 0 0 20px;
  color: var(--m-muted);
  text-align: center;
}

.m-self-register__fields {
  margin: 0 0 16px;
  --f7-input-font-size: 16px;
  --f7-list-outline-inset-border-color: var(--m-border);
}

.m-self-register__games-title {
  margin: 0 0 8px;
  white-space: normal;
}

.m-self-register__games {
  --f7-list-item-title-white-space: normal;
}

.m-self-register__loading {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 8px;
  margin: 0;
  color: var(--m-muted);
}

.m-self-register__error {
  margin: 0 0 12px;
  color: var(--f7-color-red);
}

.m-self-register__state {
  margin: 0;
}

.m-self-register__success-title {
  margin: 0 0 8px;
  font-size: 16px;
  font-weight: 600;
}

.m-self-register__password-line {
  margin: 0 0 8px;
  font-size: 14px;
}

.m-self-register__password {
  font-size: 18px;
  letter-spacing: 1px;
}

.m-self-register__hint {
  margin: 0 0 16px;
  font-size: 13px;
  color: var(--m-muted);
}
</style>
