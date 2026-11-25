<script setup lang="ts" generic="Value extends string | number | null">
import { nextTick, onUnmounted, ref, shallowRef, useId, watch } from 'vue'

type Choice = { value: Value; label: string; detail?: string }

const props = defineProps<{
  label: string
  placeholder: string
  selection?: string
  disabled?: boolean
  readOnly?: boolean
  always?: Choice
  search: (query: string) => Promise<Choice[]>
}>()
const emit = defineEmits<{ choose: [choice: Choice]; edit: [] }>()
const id = useId()
const text = ref(props.selection ?? '')
const menu = ref<HTMLElement>()
const open = ref(false)
const busy = ref(false)
const message = ref('')
const choices = shallowRef<Choice[]>([])
const highlighted = ref(-1)
let revision = 0
let timer: ReturnType<typeof setTimeout> | undefined

watch(() => props.selection, (next) => {
  if (next || !open.value) text.value = next ?? ''
})

function cancelSearch(): void {
  revision += 1
  clearTimeout(timer)
}

onUnmounted(cancelSearch)

function dismiss(): void {
  cancelSearch()
  open.value = false
  busy.value = false
  text.value = props.selection ?? ''
}

async function load(query: string, requested: number): Promise<void> {
  try {
    const matches = await props.search(query)
    if (requested === revision) choices.value = [...(props.always ? [props.always] : []), ...matches]
  } catch (cause) {
    if (requested === revision) message.value = cause instanceof Error ? cause.message : '搜索失败，请重试'
  } finally {
    if (requested === revision) busy.value = false
  }
}

function startSearch(query: string, delay = 0): void {
  cancelSearch()
  open.value = true
  busy.value = true
  message.value = ''
  highlighted.value = -1
  choices.value = props.always ? [props.always] : []
  const requested = revision
  if (delay > 0) timer = setTimeout(() => void load(query, requested), delay)
  else void load(query, requested)
}

function edit(event: Event): void {
  const input = event.target as HTMLInputElement
  text.value = input.value
  startSearch(text.value, 180)
  emit('edit')
}

function choose(choice: Choice): void {
  dismiss()
  text.value = choice.label
  emit('choose', choice)
}

function focus(): void {
  if (props.disabled || props.readOnly || open.value) return
  startSearch('')
}

function move(step: number): void {
  if (!open.value) focus()
  if (!choices.value.length) return
  const last = choices.value.length - 1
  const current = highlighted.value < 0 && step < 0 ? choices.value.length : highlighted.value
  highlighted.value = Math.max(0, Math.min(current + step, last))
  void nextTick(() => menu.value?.querySelector('[aria-selected="true"]')?.scrollIntoView?.({ block: 'nearest' }))
}

function confirm(): void {
  const choice = choices.value[highlighted.value]
  if (open.value && choice) choose(choice)
  else if (!props.readOnly && !props.disabled) startSearch(text.value)
}

function escape(event: KeyboardEvent): void {
  if (!open.value) return
  event.preventDefault()
  dismiss()
}

function onFocusOut(event: FocusEvent): void {
  if (!(event.currentTarget as HTMLElement).contains(event.relatedTarget as Node | null)) dismiss()
}
</script>

<template>
  <div class="penalty-lookup" @focusout="onFocusOut">
    <input
      :value="text" :placeholder="placeholder" :disabled="disabled" :readonly="readOnly"
      :aria-label="label" :aria-expanded="open" :aria-controls="id"
      :aria-activedescendant="open && highlighted >= 0 ? `${id}-${highlighted}` : undefined"
      aria-required="true" aria-autocomplete="list" autocomplete="off" role="combobox"
      @focus="focus" @input="edit" @keydown.enter.prevent="confirm" @keydown.esc="escape"
      @keydown.down.prevent="move(1)" @keydown.up.prevent="move(-1)"
    />
    <div v-if="open" class="penalty-lookup-popup">
      <ul :id="id" ref="menu" :aria-label="label" role="listbox">
        <li
          v-for="(choice, index) in choices" :id="`${id}-${index}`" :key="choice.value ?? '__none'"
          role="option" :aria-selected="index === highlighted"
          @mousedown.prevent @click="choose(choice)"
        >
          <span>{{ choice.label }}</span>
          <small v-if="choice.detail">{{ choice.detail }}</small>
        </li>
      </ul>
      <p v-if="busy" role="status">搜索中…</p>
      <p v-else-if="message" role="status">{{ message }}</p>
      <p v-else-if="!choices.length" role="status">没有匹配结果</p>
    </div>
  </div>
</template>

<style scoped>
.penalty-lookup { position: relative; min-width: 0; }
input { width: 100%; box-sizing: border-box; min-height: 32px; padding: 4px 8px; border: 1px solid var(--tt2-filter-line); border-radius: 4px; background: var(--tt2-panel); color: var(--tt2-ink); font: inherit; }
input:focus-visible { outline: 1px solid var(--tt2-primary); border-color: var(--tt2-primary); }
input:read-only { background: var(--tt2-btn-soft-bg); }
.penalty-lookup-popup { position: absolute; z-index: 2; top: calc(100% + 4px); left: 0; inset-inline-end: 0; padding: 3px; border: 1px solid var(--tt2-line); border-radius: 4px; background: var(--tt2-panel); box-shadow: 0 3px 12px var(--tt2-hovercard-shadow); }
ul { max-height: 176px; overflow: auto; margin: 0; padding: 0; list-style: none; }
li { display: flex; gap: 8px; align-items: baseline; padding: 5px 8px; overflow-wrap: anywhere; cursor: pointer; }
li:hover, li[aria-selected="true"] { background: var(--tt2-assignee-bg); color: var(--tt2-primary-deep); }
small, p { color: var(--tt2-dim); }
p { margin: 0; padding: 5px 8px; }
</style>
