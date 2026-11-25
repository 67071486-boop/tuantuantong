<script setup lang="ts">
import { useRouter } from 'vue-router'
import { f7Block as F7Block, f7Button as F7Button, f7Page as F7Page } from 'framework7-vue'

import { useSessionStore } from '@/stores/session'

/**
 * 空态页（spec §4.1 路由表最后一行）：「当前账号没有可用的移动端功能」+ 退出。
 * 三个到达路径共用这一页：`m-empty` 直达、两格全无（`MobileTabLayout` 的第二条保险）、未知路径兜底（`m-not-found` 重定向过来）。
 */
const session = useSessionStore()
const router = useRouter()

async function onLogout(): Promise<void> {
  await session.logout()
  void router.replace({ name: 'm-login' })
}
</script>

<template>
  <F7Page class="m-empty" :page-content="false">
    <F7Block class="m-empty__content">
      <p>当前账号没有可用的移动端功能</p>
      <F7Button type="button" large round outline @click="onLogout">退出登录</F7Button>
    </F7Block>
  </F7Page>
</template>

<style scoped>
.m-empty {
  position: relative;
  min-height: 100vh;
  min-height: 100dvh;
  display: grid;
  place-items: center;
}

.m-empty__content {
  color: var(--m-muted);
  text-align: center;
}
</style>
