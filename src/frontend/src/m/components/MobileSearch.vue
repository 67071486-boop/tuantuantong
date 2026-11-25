<script setup lang="ts">
import { f7Searchbar as F7Searchbar } from 'framework7-vue'

defineProps<{ placeholder: string }>()
const keyword = defineModel<string>({ required: true })
</script>

<template>
  <F7Searchbar
    v-model:value="keyword"
    class="m-searchbar"
    :placeholder="placeholder"
    :disable-button="false"
    role="search"
    :aria-label="placeholder"
    custom-search
    @submit.prevent
    @click:clear="keyword = ''"
  />
</template>
