<script setup lang="ts">
import { computed, ref } from 'vue'
import { f7Button as F7Button, f7Searchbar as F7Searchbar } from 'framework7-vue'
import KefuDialog from './KefuDialog.vue'

const model = defineModel<number | null>({ required: true })
const props = defineProps<{
  label: string
  options: readonly { id: number; name: string }[]
  placeholder: string
  emptyLabel?: string
  disabled?: boolean
  triggerId: string
  triggerClass?: string
  loading?: boolean
  notice?: string
}>()

const open = ref(false)
const search = ref('')
const selected = computed(() => props.options.find((option) => option.id === model.value))
const filtered = computed(() => {
  const term = search.value.trim().toLowerCase()
  return props.options.filter((option) => option.name.toLowerCase().includes(term))
})

function openPicker(): void {
  if (props.disabled) return
  search.value = ''
  open.value = true
}

function select(value: number | null): void {
  model.value = value
  open.value = false
}
</script>

<template>
  <F7Button
    :id="triggerId"
    type="button"
    class="kefu-picker-button"
    :class="triggerClass"
    :title="selected?.name ?? placeholder"
    :disabled.prop="disabled"
    :aria-disabled="disabled || undefined"
    :tabindex="disabled ? -1 : undefined"
    aria-haspopup="dialog"
    :aria-expanded="open"
    @click="openPicker"
  >
    <span>{{ selected?.name ?? placeholder }}</span>
  </F7Button>
  <KefuDialog v-model:open="open" :title="`选择${label}`">
    <F7Searchbar
      v-model:value="search"
      class="kefu-picker-search"
      custom-search
      :form="false"
      :outline="false"
      :disable-button="false"
      :backdrop="false"
      role="search"
      :aria-label="`搜索${label}`"
      placeholder="按名称搜索"
    />
    <div class="kefu-picker-options">
      <F7Button
        v-if="emptyLabel"
        type="button"
        class="kefu-picker-option" tonal
        :active="model === null"
        :aria-pressed="model === null"
        data-option-id="empty"
        @click="select(null)"
      >
        {{ emptyLabel }}
      </F7Button>
      <F7Button
        v-for="option in filtered"
        :key="option.id"
        type="button"
        class="kefu-picker-option" tonal
        :active="model === option.id"
        :aria-pressed="model === option.id"
        :data-option-id="option.id"
        @click="select(option.id)"
      >
        {{ option.name }}
      </F7Button>
      <p v-if="loading" class="kefu-hint" role="status">候选加载中…</p>
      <p v-else-if="filtered.length === 0 && !emptyLabel" class="kefu-hint">没有匹配选项</p>
    </div>
    <p v-if="notice" class="kefu-hint">{{ notice }}</p>
    <F7Button type="button" class="kefu-dialog-close" @click="open = false">取消</F7Button>
  </KefuDialog>
</template>

<style scoped>
.kefu-picker-search.searchbar { margin: 0; background-color: transparent; }
.kefu-picker-search::before,
.kefu-picker-search::after { display: none; }
.kefu-picker-search :deep(.searchbar-inner) { padding-inline: 0; }
.kefu-picker-options { display: grid; gap: 4px; max-height: 48vh; overflow-y: auto; margin: 6px 0; }
.kefu-picker-option { justify-content: flex-start; overflow-wrap: anywhere; }
.kefu-dialog-close { width: auto; margin-inline-start: auto; }
.kefu-hint { margin: 4px 0; color: var(--kefu-muted); font-size: 12px; overflow-wrap: anywhere; }
</style>
