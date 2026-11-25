<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  f7Block as F7Block,
  f7Button as F7Button,
  f7List as F7List,
  f7ListInput as F7ListInput,
  f7LoginScreenTitle as F7LoginScreenTitle,
} from 'framework7-vue'
import { Toast } from '../framework7Ui'
import { useSessionStore } from '@/stores/session'
import { fetchSiteName } from '@/api/site'
import { WXPUSHER_ENTRY } from '../utils/lastWorkerPage'
import { mRoutePathOf, sanitizeMRedirect } from '../utils/redirect'
import {
  forgetWxPusherContinuation,
  rememberWxPusherContinuation,
  WXPUSHER_ORDER_PASSWORD_NOTICE,
  requestWxPusherContinueUrl,
  wxpusherChatPathFromRedirect,
  wxpusherOrderNoFromRedirect,
} from '../utils/wxpusherOrderLink'

/**
 * 手机端登录页（spec §5.1）。**行为**照桌面 `views/LoginView.vue` 的口径，**视觉**是手机端自有设计（D-0222：本线无复刻义务）——不抄桌面那套对标实测值。
 * username / password 两字段；提交中禁用；回车 = 提交。失败 toast **后端 msg 原样**（⛔ 不自编文案——E-0087 同族）；成功 → `redirect` query 过 `sanitizeMRedirect`（限 `/m/` 前缀内）→ `mRoutePathOf` 转回路由路径后 `router.replace`，无 redirect / 被拒 → `m-home`；csrf 引导由 `request()` 自动完成。
 * ⛔ 不做「记住我」等额外功能（与桌面同一条理由：后端没有 remember-me）。
 */
const session = useSessionStore()
const router = useRouter()
const route = useRoute()

const studioName = ref('')
onMounted(async () => {
  studioName.value = (await fetchSiteName()).trim()
})

const username = ref('')
const password = ref('')
const submitting = ref(false)
const continuing = ref(false)
const signingIn = computed(() => submitting.value || continuing.value)
const wxpusherOrderNo = computed(() => wxpusherOrderNoFromRedirect(route.query.redirect, route.query.entry))
const wxpusherChatPath = computed(() => wxpusherChatPathFromRedirect(route.query.redirect))
const continuation = computed(() => {
  const orderNo = wxpusherOrderNo.value
  const chatPath = wxpusherChatPath.value
  if (orderNo !== null) return { orderNo }
  if (chatPath !== null && !chatPath.endsWith('&open=web')) return { chatPath }
  return null
})
const showWxPusherPasswordNotice = computed(() =>
  wxpusherOrderNo.value !== null && route.query.notice === WXPUSHER_ORDER_PASSWORD_NOTICE,
)

/**
 * 自助注册回流提示（spec §5）：`/m/register` 注册成功跳 `?notice=self-registered` ⇒ 顶部横幅「注册成功！请用手机号登录（密码见注册完成页）」。
 * ⭐ **只看值**：query 的键在不在与值真假是两件事（E-0021 同族）——带这个键但值不对就不出横幅。桌面 `LoginView` **不动**（注册流程不回桌面）。
 */
const showSelfRegisterNotice = route.query.notice === 'self-registered'

const canSubmit = computed(() => username.value !== '' && password.value !== '' && !signingIn.value)

async function continueWithWxPusher(): Promise<void> {
  const pending = continuation.value
  if (pending === null || signingIn.value) return
  continuing.value = true
  try {
    const target = await requestWxPusherContinueUrl(pending)
    if (continuation.value !== pending) return
    if (!rememberWxPusherContinuation(pending)) {
      const subject = pending.orderNo === undefined ? '这个群聊' : '这张订单'
      throw new Error(`当前网页无法记住${subject}，请用账号密码登录`)
    }
    window.location.assign(target)
  } catch (e) {
    if (continuation.value !== pending) return
    forgetWxPusherContinuation()
    Toast.error(e instanceof Error ? e.message : '暂时无法通过 WxPusher 继续，请用账号密码登录')
  } finally {
    continuing.value = false
  }
}

async function submit(): Promise<void> {
  if (!canSubmit.value) return
  submitting.value = true
  forgetWxPusherContinuation()
  try {
    await session.login(username.value, password.value)
    const target = sanitizeMRedirect(route.query.redirect)
    await router.replace(target !== null ? mRoutePathOf(target) : {
      name: 'm-home',
      query: route.query.entry === WXPUSHER_ENTRY ? { entry: WXPUSHER_ENTRY } : undefined,
    })
  } catch (e) {
    // 只显示服务端 msg 原样（与桌面同口径；非 Error 的失败兜一句自拟的）。
    Toast.error(e instanceof Error ? e.message : '登录失败')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="m-login">
    <F7LoginScreenTitle class="m-login__heading">
      <h1>{{ studioName || (wxpusherChatPath === null ? '打手登录' : '聊天登录') }}</h1>
    </F7LoginScreenTitle>
    <F7Block v-if="wxpusherChatPath === null" class="m-login__info">代练接单工作台</F7Block>

    <!-- 自助注册回流横幅（spec §5；桌面登录页没有这一位——注册流程不回桌面）。 -->
    <F7Block v-if="showSelfRegisterNotice" inset strong class="m-login__notice" role="status">
      注册成功！请用手机号登录（密码见注册完成页）
    </F7Block>
    <F7Block v-if="showWxPusherPasswordNotice" inset strong class="m-login__notice" role="status">
      请先用团团通账号密码登录，登录后会打开这张订单。
    </F7Block>

    <form novalidate @submit.prevent="submit">
      <F7List inset strong outline dividers class="m-login__fields">
        <F7ListInput
          input-id="m-login-username"
          v-model:value="username"
          name="username"
          type="text"
          placeholder="请输入登录账号"
          autocomplete="username"
          :disabled="signingIn"
        >
          <template #label><label for="m-login-username">账号</label></template>
        </F7ListInput>
        <F7ListInput
          input-id="m-login-password"
          v-model:value="password"
          name="password"
          type="password"
          placeholder="请输入登录密码"
          autocomplete="current-password"
          :disabled="signingIn"
        >
          <template #label><label for="m-login-password">密码</label></template>
        </F7ListInput>
      </F7List>
      <!-- F7Button 的 disabled 属性只加样式，显式转给 HTML 按钮保证键盘和表单行为。 -->
      <F7Button fill large round type="submit" :disabled.prop="!canSubmit" @click="submit">
        {{ submitting ? '登录中…' : '登录' }}
      </F7Button>
      <F7Button
        v-if="continuation !== null"
        class="m-login__continue"
        large round outline
        type="button"
        :data-testid="wxpusherChatPath === null ? 'wxpusher-continue-order' : 'wxpusher-continue-chat'"
        :disabled.prop="signingIn"
        @click="continueWithWxPusher"
      >
        {{ continuing ? '正在打开 WxPusher…' : '通过 WxPusher 继续' }}
      </F7Button>
    </form>
  </div>
</template>

<style scoped>
.m-login {
  width: min(100%, 440px);
  box-sizing: border-box;
  margin-inline: auto;
  min-height: 100vh;
  min-height: 100dvh;
  display: flex;
  flex-direction: column;
  justify-content: center;
  padding: 0 24px;
}

.m-login__heading {
  margin: 0;
}

.m-login__heading h1 {
  margin: 0;
  overflow-wrap: anywhere;
  font: inherit;
}

.m-login__info {
  margin: 4px 0 24px;
  color: var(--m-muted);
  text-align: center;
}

.m-login__notice {
  --f7-block-strong-bg-color: #e3f9e9;
  margin: 0 0 16px;
  color: #176640;
}

.m-login__continue {
  margin-top: 12px;
}

.m-login__fields {
  margin: 0 0 16px;
  --f7-input-font-size: 16px;
  --f7-list-outline-inset-border-color: var(--m-border);
}
</style>
