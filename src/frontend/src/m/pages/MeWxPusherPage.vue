<script setup lang="ts">
import { computed, nextTick, ref, useTemplateRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  f7Badge as F7Badge, f7Block as F7Block, f7BlockFooter as F7BlockFooter, f7BlockTitle as F7BlockTitle,
  f7Button as F7Button, f7Icon as F7Icon, f7List as F7List, f7ListInput as F7ListInput, f7ListItem as F7ListItem,
  f7Preloader as F7Preloader,
} from 'framework7-vue'
import { useWxPusherBinding } from '@/composables/useWxPusherBinding'
import { useSessionStore } from '@/stores/session'
import MobilePageBar from '../components/MobilePageBar.vue'
import { useWorkerBack } from '../router/useWorkerBack'
import WxPusherHallSubscription from '../components/WxPusherHallSubscription.vue'
import WxPusherPreferences from '../components/WxPusherPreferences.vue'
import { HALL_SUBSCRIPTION_HASH } from '../tabs'
import { openWxPusherSubscription } from '../utils/wxpusherSubscription'

const props = withDefaults(defineProps<{ orderDetails?: boolean }>(), { orderDetails: false })
const router = useRouter()
const route = useRoute()
const session = useSessionStore()
const canSubscribeToHall = computed(() => session.canNode(['grab_hall.take_order']))
const hallSubscriptionOpen = ref<boolean | null>(null)
const {
  guide, bindingMode, loading, failed, challenge, linkChallenge, expired, pendingAction, busy, bindingError,
  bindingCompleted, linkCodeReceipt, confirmingUnbind, testNoticeAccepted,
  loadGuide, startBinding, cancelBinding, sendLinkCode, confirmLinkCode, sendTestNotice, unbind,
} = useWxPusherBinding('/api/auth/wxpusher', () => !props.orderDetails, 'link')
const preferencesEnabled = computed(() => guide.value?.binding_enabled === true && !loading.value && !failed.value)

const linkRecoveryOpen = ref(false)
const linkUid = ref('')
const linkCode = ref('')
const uid = computed(() => linkUid.value.trim())
const validUid = computed(() => /^UID_[a-zA-Z0-9_-]{1,124}$/.test(uid.value))
const codeForUid = computed(() => linkCodeReceipt.value?.uid === uid.value)
const validCode = computed(() => /^[0-9]{6}$/.test(linkCode.value.trim()))

watch(() => linkChallenge.value?.attempt_id, () => {
  linkRecoveryOpen.value = false
  linkUid.value = ''
  linkCode.value = ''
})
watch(uid, (_current, previous) => {
  if (linkCodeReceipt.value?.uid === previous) linkCode.value = ''
})

const goBack = useWorkerBack(() => {
  if (props.orderDetails) return { name: 'm-me-notifications' }
  if (route.hash === HALL_SUBSCRIPTION_HASH && canSubscribeToHall.value) {
    const previous = router.options.history.state.back
    if (typeof previous === 'string' && router.resolve(previous).name === 'm-hall') return { name: 'm-hall' }
  }
  return { name: 'm-me' }
})

const page = useTemplateRef<HTMLElement>('page')
const hallSubscriptionToggle = useTemplateRef<InstanceType<typeof F7Button>>('hallSubscriptionToggle')
const guideUserId = session.user?.id
const guideIdentityPlane = session.identityPlane
const shouldFocusHallSubscription = computed(() => !props.orderDetails && route.hash === HALL_SUBSCRIPTION_HASH
  && session.status === 'ready' && session.isWorker && session.canNode(['my_profile']) && canSubscribeToHall.value
  && session.user?.id === guideUserId && session.identityPlane === guideIdentityPlane
  && preferencesEnabled.value && guide.value?.bound === true)

watch(shouldFocusHallSubscription, async (shouldFocus) => {
  if (!shouldFocus) return
  hallSubscriptionOpen.value = true
  await nextTick()
  if (!shouldFocusHallSubscription.value) return
  const target = hallSubscriptionToggle.value?.$el as HTMLElement | undefined
  target?.scrollIntoView?.({ block: 'start' })
  target?.focus({ preventScroll: true })
}, { flush: 'post' })

async function startSubscription(): Promise<void> {
  const attempt = await startBinding()
  if (!attempt) return
  await nextTick()
  if (page.value && !props.orderDetails && !expired.value && linkChallenge.value?.attempt_id === attempt.attempt_id) {
    openWxPusherSubscription(attempt.subscribe_url)
  }
}

let summaryScroll = 0
watch(() => props.orderDetails, async (isDetail) => {
  const scroll = page.value?.closest<HTMLElement>('#framework7-root')
  if (isDetail) summaryScroll = scroll?.scrollTop ?? 0
  await nextTick()
  if (scroll && props.orderDetails === isDetail) scroll.scrollTop = isDetail ? 0 : summaryScroll
})
</script>

<template>
  <section ref="page" class="m-wxpusher">
    <MobilePageBar :title="orderDetails ? '订单状态变更' : '消息通知'" back-test-id="wxpusher-guide-back" @back="goBack" />

    <template v-if="!orderDetails">
      <F7BlockTitle role="heading" aria-level="2">绑定 WxPusher</F7BlockTitle>
      <section :aria-busy="loading || busy">
        <F7List v-if="guide && !loading && !failed" strong inset outline>
          <F7ListItem title="绑定状态">
            <template #after>
              <F7Badge :color="guide.bound ? 'green' : 'gray'" data-testid="wxpusher-binding-status" role="status">
                {{ guide.bound ? '已绑定' : '未绑定' }}
              </F7Badge>
            </template>
          </F7ListItem>
        </F7List>

        <F7Block v-if="loading" inset role="status">
          <F7Preloader size="20" aria-hidden="true" /> 正在读取绑定状态…
        </F7Block>
        <F7Block v-else-if="failed" inset>
          <p role="status">绑定状态加载失败。</p>
          <F7Button type="button" round outline data-testid="retry-wxpusher-guide" @click="loadGuide">重试</F7Button>
        </F7Block>
        <template v-else-if="guide">
          <F7Block v-if="guide.bound" inset>
            <div v-if="!confirmingUnbind" class="m-wxpusher__actions">
              <F7Button v-if="guide.binding_enabled" type="button" round fill :disabled.prop="busy"
                data-testid="wxpusher-send-test" @click="sendTestNotice">
                {{ pendingAction === 'test' ? '正在发送…' : '发送测试消息' }}
              </F7Button>
              <F7Button v-if="guide.binding_enabled && bindingMode === 'link' && !linkChallenge" type="button" round outline :disabled.prop="busy"
                data-testid="wxpusher-rebind" @click="startSubscription">
                {{ pendingAction === 'start' ? '正在获取链接…' : '换绑 WxPusher' }}
              </F7Button>
              <F7Button type="button" round outline :disabled.prop="busy" data-testid="wxpusher-unbind"
                @click="confirmingUnbind = true">解绑</F7Button>
            </div>
            <template v-else>
              <p>解绑会移除当前团团通账号的通知关联。WxPusher 里的订阅会保留，可以随时重新绑定。</p>
              <div class="m-wxpusher__actions">
                <F7Button type="button" round outline :disabled.prop="busy" @click="confirmingUnbind = false; bindingError = ''">
                  取消
                </F7Button>
                <F7Button type="button" round outline color="red" :disabled.prop="busy" data-testid="wxpusher-unbind-confirm" @click="unbind">
                  {{ pendingAction === 'unbind' ? '正在解绑…' : '确认解绑' }}
                </F7Button>
              </div>
            </template>
            <F7BlockFooter v-if="testNoticeAccepted" role="status">已交给 WxPusher，请在手机上查看</F7BlockFooter>
          </F7Block>
          <F7Block v-if="!guide.bound && !guide.binding_enabled" inset>绑定暂未开放，请联系工作室。</F7Block>
          <F7Block v-else-if="guide.binding_enabled && (!guide.bound || linkChallenge)" inset>
            <template v-if="!challenge || expired">
              <p v-if="expired" role="status">
                {{ linkChallenge ? '订阅链接已过期，请重新获取链接。' : '绑定二维码已过期，请重新发起绑定。' }}
              </p>
              <p v-else>{{ bindingMode === 'link' ? '点击后打开 WxPusher，订阅团团通通知。' : '用 WxPusher 扫描个人绑定二维码。' }}</p>
              <F7Button type="button" round fill :disabled.prop="busy" data-testid="wxpusher-bind" @click="startSubscription">
                <template v-if="bindingMode === 'link'">{{ pendingAction === 'start' ? '正在打开 WxPusher…' : '获取订阅链接' }}</template>
                <template v-else>{{ pendingAction === 'start' ? '正在生成二维码…' : '获取绑定二维码' }}</template>
              </F7Button>
            </template>
            <template v-else-if="linkChallenge">
              <p v-if="guide.bound">完成换绑前，旧绑定仍可接收通知。</p>
              <div class="m-wxpusher__actions">
                <F7Button :href="linkChallenge.subscribe_url" rel="noreferrer" external round fill
                  data-testid="wxpusher-subscribe-link">打开 WxPusher 订阅</F7Button>
                <F7Button type="button" round outline :disabled.prop="busy" data-testid="wxpusher-cancel-link" @click="cancelBinding">
                  {{ pendingAction === 'cancel' ? '正在取消…' : guide.bound ? '取消换绑' : '取消绑定' }}
                </F7Button>
              </div>
              <p role="status">未安装时按链接提示下载，登录并订阅后回到本页。若未自动绑定，再点一次原链接。</p>
              <F7Button type="button" small data-testid="wxpusher-link-recovery" :aria-expanded="linkRecoveryOpen"
                aria-controls="wxpusher-link-recovery-content" @click="linkRecoveryOpen = !linkRecoveryOpen">
                已订阅但还没绑定？
              </F7Button>
              <div v-if="linkRecoveryOpen" id="wxpusher-link-recovery-content">
                <p>在 WxPusher App 的「我的」里复制 UID 来获取验证码。已有验证码可直接验证。</p>
                <F7List strong inset outline dividers>
                  <F7ListInput
                    v-model:value="linkUid" type="text" input-id="wxpusher-link-uid" name="wxpusher-uid"
                    placeholder="UID_…" autocomplete="off" autocapitalize="off" spellcheck="false" :maxlength="128" :disabled="busy"
                  >
                    <template #label><label for="wxpusher-link-uid">WxPusher UID</label></template>
                  </F7ListInput>
                  <F7ListInput
                    v-model:value="linkCode" type="text" input-id="wxpusher-link-code" name="wxpusher-code"
                    placeholder="请输入 6 位验证码" inputmode="numeric" autocomplete="one-time-code" :maxlength="6" :disabled="busy"
                  >
                    <template #label><label for="wxpusher-link-code">验证码</label></template>
                  </F7ListInput>
                </F7List>
                <div class="m-wxpusher__actions">
                  <F7Button type="button" round fill :disabled.prop="busy || !validCode"
                    data-testid="wxpusher-confirm-link-code" @click="confirmLinkCode(linkCode.trim())">
                    {{ pendingAction === 'confirm-link-code' ? '正在验证…' : '验证并绑定' }}
                  </F7Button>
                  <F7Button type="button" round outline :disabled.prop="busy || !validUid"
                    data-testid="wxpusher-send-link-code" @click="sendLinkCode(uid)">
                    {{ pendingAction === 'send-link-code' ? '正在发送…' : codeForUid ? '重新发送验证码' : '发送验证码' }}
                  </F7Button>
                </div>
                <p v-if="codeForUid" role="status">
                  {{ linkCodeReceipt?.deliveryUnconfirmed ? '暂未确认验证码是否送达，收到后可以直接验证。' : '发送请求已受理，请到 WxPusher 查看验证码。' }}
                </p>
              </div>
            </template>
            <template v-else-if="challenge.qr_url">
              <img class="m-wxpusher__qr" :src="challenge.qr_url" alt="WxPusher 个人绑定二维码" width="160" height="160" referrerpolicy="no-referrer">
              <p>用 WxPusher 扫个人绑定码。只有一台手机时，可以长按保存，再到 WxPusher 从相册选择。</p>
              <p role="status">扫完回到本页，团团通会自动绑定。二维码有效期为 5 分钟。</p>
              <F7Button type="button" round outline :disabled.prop="busy" @click="startSubscription">
                {{ pendingAction === 'start' ? '正在获取绑定信息…' : '换一个个人绑定码' }}
              </F7Button>
            </template>
          </F7Block>
        </template>
        <F7BlockFooter v-if="bindingCompleted" role="status">{{ bindingCompleted }}</F7BlockFooter>
        <F7BlockFooter v-if="bindingError" class="text-color-red" role="alert">{{ bindingError }}</F7BlockFooter>
      </section>
    </template>

    <F7Block v-if="orderDetails && !preferencesEnabled" inset>
      <div v-if="loading" role="status"><F7Preloader size="20" aria-hidden="true" /> 正在读取推送设置…</div>
      <template v-else-if="failed">
        <p role="alert">推送设置加载失败，请重试。</p>
        <F7Button type="button" round outline @click="loadGuide">重试</F7Button>
      </template>
      <p v-else>手机推送暂未开放，请联系工作室。</p>
    </F7Block>

    <WxPusherPreferences
      v-if="!orderDetails || preferencesEnabled"
      :bound="guide?.bound === true"
      :enabled="preferencesEnabled"
      :order-details="orderDetails"
    >
      <template v-if="!orderDetails" #entries>
        <F7ListItem
          link="#" external title="通知记录" data-testid="wxpusher-notification-inbox"
          @click.prevent="router.push({ name: 'm-me-notification-inbox' })"
        />
        <template v-if="canSubscribeToHall">
          <F7ListItem class="m-push-preferences__disclosure">
            <template #title>
              <F7Button
                id="hall-subscription"
                ref="hallSubscriptionToggle"
                type="button"
                class="m-push-preferences__detail"
                data-testid="wxpusher-hall-subscription"
                :aria-expanded="hallSubscriptionOpen === true"
                aria-controls="wxpusher-hall-subscription-content"
                @click="hallSubscriptionOpen = !hallSubscriptionOpen"
              >
                <span>抢单订阅</span>
                <F7Icon :f7="hallSubscriptionOpen ? 'chevron_down' : 'chevron_right'" :size="14" aria-hidden="true" />
              </F7Button>
            </template>
          </F7ListItem>
          <li
            id="wxpusher-hall-subscription-content"
            v-show="hallSubscriptionOpen === true"
            aria-labelledby="hall-subscription"
          >
            <WxPusherHallSubscription v-if="hallSubscriptionOpen !== null" />
          </li>
        </template>
      </template>
    </WxPusherPreferences>
  </section>
</template>

<style scoped>
.m-wxpusher {
  min-height: 100dvh;
  padding-bottom: 24px;
}

.m-wxpusher__actions {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(120px, 100%), 1fr));
  gap: 8px;
}

.m-wxpusher__qr {
  display: block;
  max-width: 100%;
  height: auto;
  margin-inline: auto;
}

#hall-subscription {
  scroll-margin-top: calc(var(--f7-navbar-height, 44px) + var(--f7-safe-area-top, 0px) + 12px);
}
</style>
