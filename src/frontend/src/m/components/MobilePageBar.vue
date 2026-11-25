<script setup lang="ts">
import { f7Link as F7Link, f7Navbar as F7Navbar } from 'framework7-vue'

defineProps<{ title: string; backTestId: string }>()
defineEmits<{ back: [] }>()
</script>

<template>
  <F7Navbar class="m-detail-navbar">
    <template #left>
      <F7Link
        href="#"
        class="external"
        back
        icon="icon-back"
        icon-only
        aria-label="返回"
        :data-testid="backTestId"
        @click.prevent="$emit('back')"
      />
    </template>
    <template #title><h1 class="m-detail-navbar__title">{{ title }}</h1></template>
    <template v-if="$slots.right" #right><slot name="right" /></template>
  </F7Navbar>
</template>
