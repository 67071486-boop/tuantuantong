<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { wxpusherHomeAppLink } from '../utils/wxpusherOrderLink'

const route = useRoute()
const appLink = computed(() => wxpusherHomeAppLink(route.query.entry))
const webLink = computed(() => route.query.entry === 'wxpusher' && route.query.continue === 'confirm'
  ? '/api/auth/wxpusher/login'
  : '/m/?entry=wxpusher')
</script>

<template>
  <main class="m-wxpusher-open">
    <h1>团团通</h1>
    <a v-if="appLink" class="m-wxpusher-open__primary" :href="appLink">打开代代通</a>
    <a :href="webLink">继续使用网页版</a>
  </main>
</template>

<style scoped>
.m-wxpusher-open {
  display: grid;
  gap: 12px;
  width: min(100%, 420px);
  margin: 48px auto;
  padding: 20px;
  text-align: center;
}

h1 {
  margin: 0 0 4px;
  font-size: 22px;
}

a {
  padding: 12px;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  color: #1e293b;
  text-decoration: none;
}

.m-wxpusher-open__primary {
  background: var(--td-brand-color, #0052d9);
  border-color: transparent;
  color: #fff;
}
</style>
