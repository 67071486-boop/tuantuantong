<script setup lang="ts">
import { f7Button as F7Button } from 'framework7-vue'
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { fetchWxPusherGuide } from '@/composables/useWxPusherBinding'
import { useSessionStore } from '@/stores/session'
import MobileEntryPrompt from './MobileEntryPrompt.vue'

const props = defineProps<{ deferDisplay?: boolean }>()
const checkedKey = 'tt:wxpusher-binding-guide-checked'
const route = useRoute()
const router = useRouter()
const session = useSessionStore()
const offerBinding = ref(false)
const navigating = ref(false)
const worker = computed(() => session.status === 'ready' && session.isWorker && session.user
  ? `${session.identityPlane}:${session.user.id}`
  : null)
const onBindingPage = computed(() => route.name === 'm-me-notifications')
const canOffer = computed(() => route.meta.requiresAuth !== false && session.canNode(['my_profile']))
const show = computed(() => offerBinding.value && canOffer.value && !onBindingPage.value && !props.deferDisplay)
let checked = false
const loginGeneration = ref(0)
let checkGeneration = 0

watch([worker, () => session.status === 'guest'], ([identity, guest], [previous]) => {
  if (guest || (previous && previous !== identity)) {
    try {
      window.sessionStorage.removeItem(checkedKey)
    } catch {
      // Browsers that block storage still use the in-memory check.
    }
  }
  checked = false
  loginGeneration.value += 1
  checkGeneration += 1
  offerBinding.value = false
}, { immediate: true, flush: 'sync' })

watch(onBindingPage, (alreadyThere) => {
  if (!alreadyThere) return
  checkGeneration += 1
  offerBinding.value = false
}, { flush: 'sync' })

watch([loginGeneration, canOffer, onBindingPage], async ([, allowed, alreadyThere]) => {
  const identity = worker.value
  if (!identity || !allowed || checked) return
  checked = true
  try {
    if (window.sessionStorage.getItem(checkedKey) === identity) return
    window.sessionStorage.setItem(checkedKey, identity)
  } catch {
    // Browsers that block storage still use the in-memory check.
  }
  // The binding page reads this same guide when the user starts there.
  if (alreadyThere) return
  const generation = checkGeneration
  try {
    const guide = await fetchWxPusherGuide('/api/auth/wxpusher', true)
    if (generation === checkGeneration && !onBindingPage.value) {
      offerBinding.value = guide.binding_prompt_enabled === true && guide.binding_enabled === true && guide.bound === false
    }
  } catch {
    // A background check must not interrupt the login or its destination.
  }
}, { immediate: true })

async function goToBinding(): Promise<void> {
  if (navigating.value) return
  navigating.value = true
  try {
    await router.push({ name: 'm-me-notifications' })
  } finally {
    navigating.value = false
  }
}
</script>

<template>
  <MobileEntryPrompt
    v-if="show"
    title="建议绑定 WxPusher"
    description="强烈建议绑定 WxPusher，开启实时和离线通知，及时接收聊天消息和新订单推送。"
    @dismiss="offerBinding = false"
  >
    <F7Button type="button" large fill :disabled.prop="navigating" @click="goToBinding">去绑定</F7Button>
    <F7Button type="button" large tonal @click="offerBinding = false">稍后再说</F7Button>
  </MobileEntryPrompt>
</template>
