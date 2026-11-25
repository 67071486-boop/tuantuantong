<script setup lang="ts">
import { f7Button as F7Button } from 'framework7-vue'
import { computed } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { WXPUSHER_ENTRY } from '../utils/lastWorkerPage'
import { wxpusherSharedHallClaimPath, wxpusherSharedHallQuery, wxpusherSharedHallTarget } from '../utils/wxpusherOrderLink'

const router = useRouter()
const route = useRoute()
const sharedHall = computed(() => wxpusherSharedHallTarget(route.query, route.hash))
const passwordRoute = computed(() => sharedHall.value && ({
  name: 'm-login',
  query: { entry: WXPUSHER_ENTRY, redirect: wxpusherSharedHallClaimPath(sharedHall.value) },
}))

function retry(): void {
  const target = router.resolve({
    name: 'm-wxpusher-hall-entry',
    query: sharedHall.value ? { ...wxpusherSharedHallQuery(sharedHall.value), open: 'web' } : { entry: WXPUSHER_ENTRY },
    hash: route.hash,
  })
  const location = window.location
  const next = new URL(target.href, location.href)
  // 只有片段不同不会重新加载页面，守卫也就不会重试登录。
  if (next.pathname === location.pathname && next.search === location.search) location.reload()
  else location.replace(target.href)
}
</script>

<template>
  <main class="m-hall-entry">
    <h1>暂时无法打开这条抢单通知</h1>
    <p v-if="passwordRoute">你可以重新通过 WxPusher 打开，或用账号密码登录后继续查看这张单。</p>
    <p v-else>你可以重试，或回到 WxPusher 打开最新的抢单通知。</p>
    <F7Button type="button" outline @click="retry">重试</F7Button>
    <RouterLink v-if="passwordRoute" :to="passwordRoute">账号密码登录</RouterLink>
  </main>
</template>

<style scoped>
.m-hall-entry {
  display: grid;
  justify-items: center;
  gap: 12px;
  width: min(100%, 420px);
  margin: 48px auto;
  padding: 20px;
  text-align: center;
}

h1 {
  margin: 0;
  font-size: 20px;
}

p {
  margin: 0;
  color: #6b7280;
}
</style>
