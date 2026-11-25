<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { f7Block as F7Block, f7Button as F7Button } from 'framework7-vue'
import {
  checkScreenshotBrowser, exchangeScreenshotBrowser, startScreenshotBrowser,
} from '@/api/screenshotBrowser'
import { HttpError, NetworkError } from '@/api/errors'
import { isWxPusherAndroid } from '../utils/wxpusherOrderLink'
import OrderScreenshotsPage from './OrderScreenshotsPage.vue'

const route = useRoute()
const router = useRouter()
const orderNo = computed(() => String(route.params.orderNo ?? ''))
const embedded = isWxPusherAndroid()
const hasChallenge = computed(() => /^#[A-Za-z0-9_-]{43}$/.test(route.hash))
const state = ref<'loading' | 'ready' | 'retry' | 'error'>('loading')
const problem = ref('')
const regenerating = ref(false)
const retryCount = ref(0)
let pendingChallenge = ''

function fail(message: string): void {
  pendingChallenge = ''
  problem.value = message
  state.value = 'error'
}

function failedRequest(error: unknown): void {
  fail(error instanceof Error && error.message
    ? error.message
    : '上传链接已失效，请回到 WxPusher 重新生成')
}

async function regenerate(): Promise<void> {
  if (regenerating.value) return
  regenerating.value = true
  problem.value = ''
  const currentOrder = orderNo.value
  try {
    const { challenge } = await startScreenshotBrowser(currentOrder)
    if (route.name !== 'm-order-screenshot-browser' || currentOrder !== orderNo.value) return
    await router.replace({ hash: `#${challenge}` })
  } catch (error) {
    failedRequest(error)
  } finally {
    regenerating.value = false
  }
}

async function openGrant(currentOrder: string, challenge: string, retrying: boolean): Promise<void> {
  if (retrying || !challenge) {
    try {
      await checkScreenshotBrowser(currentOrder, challenge || undefined)
      return
    } catch (error) {
      if (!challenge || !(error instanceof HttpError) || error.status !== 403 || error.csrfRefreshed) throw error
    }
  }
  await exchangeScreenshotBrowser(currentOrder, challenge)
}

if (!embedded) {
  watch([orderNo, () => route.hash, retryCount], async ([currentOrder, hash, count], previous, onCleanup) => {
    const retrying = currentOrder === previous?.[0] && count !== previous?.[2]
    if (!hash && currentOrder === previous?.[0] && !retrying && state.value !== 'loading') return
    if (!retrying) pendingChallenge = hash.slice(1)
    let pending = true
    onCleanup(() => { pending = false })
    state.value = 'loading'
    try {
      await openGrant(currentOrder, pendingChallenge, retrying)
      if (!pending) return
      pendingChallenge = ''
      state.value = 'ready'
    } catch (error) {
      if (!pending) return
      if (error instanceof NetworkError
        || (error instanceof HttpError && (error.csrfRefreshed || error.status >= 500))) {
        problem.value = error.message
        state.value = 'retry'
      } else {
        failedRequest(error)
      }
    }
    if (hash) await router.replace({ hash: '' })
  }, { immediate: true })
}
</script>

<template>
  <section class="m-browser-shot">
    <F7Block strong inset>
      <h1>上传进度截图</h1>
      <p>订单号 {{ orderNo }}</p>
      <template v-if="embedded">
        <p v-if="hasChallenge" data-testid="shot-browser-instructions">
          点右上角「在浏览器中打开」，就可以给这笔订单上传截图，无需再次登录。
        </p>
        <p v-if="problem" role="alert">{{ problem }}</p>
        <F7Button type="button" round outline :disabled.prop="regenerating" @click="regenerate">
          {{ regenerating ? '正在准备…' : hasChallenge ? '链接过期？重新生成' : '生成上传链接' }}
        </F7Button>
      </template>
      <p v-else-if="state === 'loading'" role="status">正在打开上传页…</p>
      <template v-else-if="state === 'retry'">
        <p role="alert">{{ problem }}</p>
        <F7Button v-if="!route.hash" type="button" round outline
          data-testid="shot-browser-retry" @click="retryCount += 1">重试</F7Button>
      </template>
      <p v-else-if="state === 'error'" role="alert">{{ problem }}</p>
    </F7Block>
    <OrderScreenshotsPage v-if="!embedded && !route.hash && state === 'ready'" :key="orderNo"
      browser-upload @browser-expired="fail" />
  </section>
</template>

<style scoped>
.m-browser-shot {
  min-height: 100vh;
  padding: 12px;
  background: var(--f7-page-bg-color);
}

.m-browser-shot h1 {
  margin: 0 0 8px;
  font: var(--m-type-title, 600 20px/1.3 system-ui);
}

.m-browser-shot p {
  margin: 8px 0;
  overflow-wrap: anywhere;
}
</style>
