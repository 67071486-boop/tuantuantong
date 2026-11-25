<script setup lang="ts">
import { provide } from 'vue'
import { routeLocationKey, type RouteLocationNormalizedLoaded } from 'vue-router'

const props = defineProps<{ route: RouteLocationNormalizedLoaded }>()
// 每个页签读取自己的路由；切到别的签不能触发本签的深链筛选。
provide(routeLocationKey, props.route)
</script>

<template>
  <RouterView :route="route" />
</template>
