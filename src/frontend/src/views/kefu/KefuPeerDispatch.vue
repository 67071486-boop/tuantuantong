<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'
import { f7Button as F7Button, f7ListInput as F7ListInput } from 'framework7-vue'
import {
  fetchPeerDirectAssignment, fetchPeerDraftStudios, searchPeerTakers,
  type KefuPeerSelection, type PeerDirectAssignment, type PeerStudio, type PeerTakerMatch,
} from '@/api/peerDirectAssignment'
import { useSessionStore } from '@/stores/session'
import KefuPicker from './KefuPicker.vue'

const model = defineModel<KefuPeerSelection | null>({ required: true })
const props = defineProps<{ gameId: number | null; originalGameId?: number | null; orderNo?: string; disabled?: boolean }>()
const session = useSessionStore()
const canChangePrice = computed(() => session.canNode(['order.submit_payout_price']))
const gameChanged = computed(() => props.originalGameId !== undefined && props.gameId !== props.originalGameId)
const studioId = ref<number | null>(null)
const query = ref('')
const price = ref('')
const studios = ref<PeerStudio[]>([])
const results = ref<PeerTakerMatch[]>([])
const selected = ref<PeerTakerMatch | null>(null)
const orderOptions = ref<PeerDirectAssignment | null>(null)
const error = ref('')
const loading = ref(false)
const searching = ref(false)
const searched = ref(false)
const refresh = ref(0)
const id = useId()
let searchGeneration = 0

watch([studioId, query], () => {
  searchGeneration += 1
  searching.value = false
  searched.value = false
  results.value = []
  selected.value = null
  error.value = ''
}, { flush: 'sync' })

watch(() => [props.gameId, props.orderNo, gameChanged.value, refresh.value] as const, async ([game, order, changed], _, onCleanup) => {
  let active = true
  onCleanup(() => { active = false })
  studioId.value = null
  query.value = ''
  price.value = ''
  studios.value = []
  orderOptions.value = null
  error.value = ''
  if (!order && game === null) { loading.value = false; return }
  loading.value = true
  try {
    if (order) {
      const data = changed && game !== null
        ? await fetchPeerDirectAssignment(order, game)
        : await fetchPeerDirectAssignment(order)
      if (!active) return
      orderOptions.value = data
      studios.value = game === null ? [] : data.studios
    } else if (game !== null) {
      const data = await fetchPeerDraftStudios(game)
      if (active) studios.value = data.studios
    }
  } catch (cause) {
    if (active) error.value = cause instanceof Error ? cause.message : '读取下家工作室失败'
  } finally {
    if (active) loading.value = false
  }
}, { immediate: true })

watch([studioId, selected, price, orderOptions], () => {
  const options = orderOptions.value
  if (options?.pending) {
    const { studio_id, account_name, price: expected_price } = options.pending
    model.value = { studio_id, account_name, expected_price }
  } else if (studioId.value !== null && selected.value) {
    model.value = {
      studio_id: studioId.value,
      ...selected.value,
      expected_price: options?.price ?? null,
      ...(props.orderNo && canChangePrice.value && price.value.trim() ? { price: price.value.trim() } : {}),
    }
  } else {
    model.value = null
  }
}, { immediate: true })

async function search(): Promise<void> {
  const game = props.gameId
  const studio = studioId.value
  const term = query.value.trim()
  if (props.disabled || searching.value || studio === null || game === null || !term) return
  const generation = ++searchGeneration
  const order = gameChanged.value ? undefined : props.orderNo
  searching.value = true
  searched.value = false
  results.value = []
  selected.value = null
  error.value = ''
  try {
    const data = await searchPeerTakers(order, { studio_id: studio, query: term, ...(order ? {} : { game_id: game }) })
    if (generation !== searchGeneration) return
    results.value = data.items
    searched.value = true
  } catch (cause) {
    if (generation === searchGeneration) error.value = cause instanceof Error ? cause.message : '搜索打手失败'
  } finally {
    if (generation === searchGeneration) searching.value = false
  }
}
</script>

<template>
  <div class="kefu-peer-dispatch">
    <p v-if="loading" class="kefu-hint" role="status">正在读取下家工作室…</p>
    <p v-if="orderOptions?.pending" class="kefu-hint" role="status">
      等待 {{ orderOptions.pending.studio_name }} 确认指派给 {{ orderOptions.pending.account_name }}，发单价 {{ orderOptions.pending.price }} 元。
    </p>
    <template v-else-if="studios.length">
      <div class="kefu-field">
        <label :for="`${id}-studio`">下家工作室</label>
        <KefuPicker v-model="studioId" :trigger-id="`${id}-studio`" label="下家工作室" :options="studios" placeholder="请选择" :disabled="disabled" />
      </div>
      <div class="kefu-field">
        <label :for="`${id}-account`">打手账号</label>
        <div class="kefu-peer-search">
          <F7ListInput
            v-model:value="query" :input-id="`${id}-account`" :wrap="false" class="kefu-peer-input"
            maxlength="150" placeholder="输入部分账号名称" :disabled="disabled || studioId === null"
            @keydown.enter.stop.prevent="search"
          />
          <F7Button type="button" tonal small :disabled.prop="disabled || studioId === null || !query.trim() || searching" @click="search">
            {{ searching ? '查找中…' : '查找' }}
          </F7Button>
        </div>
      </div>
      <div v-if="results.length" class="kefu-field">
        <span class="kefu-label">匹配打手</span>
        <div class="kefu-peer-results" role="group" aria-label="匹配打手">
          <F7Button
            v-for="result in results" :key="result.match_token" type="button" small tonal
            :fill="selected?.match_token === result.match_token" :aria-pressed="selected?.match_token === result.match_token"
            :disabled.prop="disabled" @click="selected = result"
          >{{ result.account_name }}</F7Button>
        </div>
      </div>
      <p v-else-if="searched" class="kefu-hint" role="status">没有匹配的可接单打手。</p>
      <div v-if="selected && orderNo && canChangePrice" class="kefu-field">
        <label :for="`${id}-price`">发单价</label>
        <F7ListInput
          v-model:value="price" :input-id="`${id}-price`" :wrap="false" class="kefu-peer-input"
          inputmode="decimal" :placeholder="orderOptions?.price == null ? '输入给下家的价格' : `留空沿用 ${orderOptions.price} 元`"
          :disabled="disabled"
        />
      </div>
    </template>
    <p v-else-if="orderOptions?.assignee" class="kefu-hint">当前下家接手人 {{ orderOptions.assignee.studio_name }} · {{ orderOptions.assignee.account_name ?? '暂无账号名称' }}</p>
    <p v-else-if="!loading && !error" class="kefu-hint">{{ gameId === null ? '请先选择游戏。' : '当前订单没有可直派的下家工作室。' }}</p>
    <p v-if="error" class="kefu-hint kefu-peer-error" role="alert">
      {{ error }}
      <button v-if="!studios.length" type="button" :disabled="disabled" @click="refresh += 1">重试</button>
    </p>
  </div>
</template>

<style scoped>
.kefu-peer-search { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 5px; }
.kefu-peer-input :deep(.item-inner), .kefu-peer-input :deep(.item-input-wrap) { min-width: 0; padding: 0; }
.kefu-peer-results { display: grid; gap: 3px; max-height: 150px; overflow-y: auto; }
.kefu-peer-results .button { overflow-wrap: anywhere; text-align: start; }
.kefu-peer-error { color: var(--kefu-error); }
</style>
