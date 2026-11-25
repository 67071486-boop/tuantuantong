<script setup lang="ts">
import { f7Button as F7Button, f7Preloader as F7Preloader } from 'framework7-vue'

defineProps<{ asyncLoading?: 'load-more' | 'loading' }>()
defineEmits<{ loadMore: [] }>()
</script>

<template>
  <div>
    <slot />
    <div v-if="asyncLoading === 'loading'" class="m-paged-list__loading" role="status" aria-label="加载中">
      <F7Preloader :size="20" />
    </div>
    <F7Button v-else-if="asyncLoading === 'load-more'" round class="m-paged-list__more" type="button" @click="$emit('loadMore')">
      加载更多
    </F7Button>
  </div>
</template>

<style scoped>
.m-paged-list__loading,
.m-paged-list__more {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 42px;
}
</style>
