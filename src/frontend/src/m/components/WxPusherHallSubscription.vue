<script setup lang="ts">
import { onMounted, ref } from 'vue'
import {
  f7Block as F7Block, f7BlockFooter as F7BlockFooter, f7BlockTitle as F7BlockTitle,
  f7Button as F7Button, f7List as F7List, f7ListInput as F7ListInput,
  f7ListItem as F7ListItem, f7Preloader as F7Preloader,
} from 'framework7-vue'
import { fetchGames, type ConfigRow } from '@/api/config'
import { request } from '@/api/http'
import MobileToggle from './MobileToggle.vue'

interface GameRule {
  game_id: number
  price_min: string | null
  price_max: string | null
  include_keywords: string[]
  exclude_keywords: string[]
}

interface Subscription {
  enabled: boolean
  games: GameRule[]
}

interface GameDraft {
  id: number
  name: string
  selected: boolean
  min: string
  max: string
  include: string
  exclude: string
}

const URL = '/api/auth/wxpusher/hall-subscription'
const PRICE = /^(?:[0-9]{1,10}(?:\.[0-9]{1,2})?|\.[0-9]{1,2})$/
const availableGames = ref<ConfigRow[]>([])
const drafts = ref<GameDraft[]>([])
const enabled = ref(false)
const expandedGame = ref<number | null>(null)
const loading = ref(true)
const loaded = ref(false)
const saving = ref(false)
const saved = ref(false)
const failure = ref('')

function applySubscription(subscription: Subscription): void {
  const configured = new Map(subscription.games.map((game) => [game.game_id, game]))
  enabled.value = subscription.enabled
  drafts.value = availableGames.value.map(({ id, name }) => {
    const rule = configured.get(id)
    return {
      id, name, selected: rule !== undefined,
      min: rule?.price_min ?? '', max: rule?.price_max ?? '',
      include: rule?.include_keywords.join('，') ?? '', exclude: rule?.exclude_keywords.join('，') ?? '',
    }
  })
}

async function loadSubscription(): Promise<void> {
  loading.value = true
  failure.value = ''
  try {
    const [games, subscription] = await Promise.all([
      fetchGames(), request<Subscription>({ url: URL, method: 'GET' }),
    ])
    availableGames.value = games.filter((game) => game.is_active)
    applySubscription(subscription)
    loaded.value = true
  } catch (error) {
    failure.value = error instanceof Error ? error.message : '订阅设置加载失败，请重试。'
  } finally {
    loading.value = false
  }
}

function keywords(value: string): string[] {
  return [...new Set(value.split(/[\s,，]+/u).filter(Boolean))]
}

function gameSummary(game: GameDraft): string {
  if (!game.selected) return '未订阅'
  return game.min || game.max || keywords(game.include).length || keywords(game.exclude).length ? '按规则推送' : '全部新单'
}

function changeGame(game: GameDraft, checked: boolean): void {
  game.selected = checked
  saved.value = false
  if (game.selected) expandedGame.value = game.id
  else if (expandedGame.value === game.id) expandedGame.value = null
}

function ruleFor(game: GameDraft): GameRule {
  const price_min = game.min || null
  const price_max = game.max || null
  if ([price_min, price_max].some((price) => price !== null && !PRICE.test(price))) {
    throw new Error(`${game.name}的价格须为非负金额，最多两位小数。`)
  }
  if (price_min !== null && price_max !== null && Number(price_min) > Number(price_max)) {
    throw new Error(`${game.name}的最低价不能高于最高价。`)
  }
  const include_keywords = keywords(game.include)
  const exclude_keywords = keywords(game.exclude)
  if ([include_keywords, exclude_keywords].some((group) => group.length > 20 || group.some((word) => [...word].length > 50))) {
    throw new Error(`${game.name}的包含和排除关键词各最多 20 个，每个词最多 50 个字。`)
  }
  return { game_id: game.id, price_min, price_max, include_keywords, exclude_keywords }
}

async function saveSubscription(): Promise<void> {
  if (saving.value || !loaded.value) return
  saved.value = false
  failure.value = ''
  try {
    const games = drafts.value.filter((game) => game.selected).map(ruleFor)
    saving.value = true
    const subscription = await request<Subscription>({ url: URL, method: 'PATCH', data: { enabled: enabled.value, games } })
    applySubscription(subscription)
    saved.value = true
  } catch (error) {
    failure.value = error instanceof Error ? error.message : '未能确认保存，请重试。'
  } finally {
    saving.value = false
  }
}

onMounted(loadSubscription)
</script>

<template>
  <F7Block v-if="loading" inset role="status">
    <F7Preloader size="20" aria-hidden="true" /> 正在读取订阅设置…
  </F7Block>
  <form v-else-if="loaded" :aria-busy="saving" @input="saved = false" @submit.prevent="saveSubscription">
    <F7List strong inset outline>
      <F7ListItem title="新单推送">
        <template #after>
          <MobileToggle
            label="新单推送" immediate :checked="enabled" :disabled="saving"
            @update:checked="enabled = $event; saved = false"
          />
        </template>
      </F7ListItem>
    </F7List>
    <F7BlockFooter>绑定 WxPusher 后接收所选游戏的新单。</F7BlockFooter>

    <F7BlockTitle id="hall-push-games" role="heading" aria-level="2">订阅游戏</F7BlockTitle>
    <F7BlockFooter>选中游戏后，留空规则会推送该游戏的全部新单。</F7BlockFooter>
    <F7Block v-if="drafts.length === 0" inset>当前账号没有可订阅的游戏。</F7Block>
    <F7BlockFooter v-else-if="enabled && !drafts.some((game) => game.selected)" role="status">
      请先选择要订阅的游戏。未选择游戏时不会发送新单。
    </F7BlockFooter>
    <F7List v-if="drafts.length" strong inset outline dividers role="group" aria-labelledby="hall-push-games">
      <template v-for="game in drafts" :key="game.id">
        <F7ListItem
          :checked="game.selected" checkbox checkbox-icon="start"
          class="m-hall-subscription__game-choice"
          :name="`hall-push-game-${game.id}`" :title="game.name" :footer="gameSummary(game)"
          :disabled="saving" @update:checked="changeGame(game, $event)"
        />
        <F7ListItem v-if="game.selected">
          <F7Button
            :id="`hall-push-game-${game.id}-filter`" type="button" round
            :aria-label="`${game.name}的筛选规则`" :aria-expanded="expandedGame === game.id" :disabled.prop="saving"
            @click="expandedGame = expandedGame === game.id ? null : game.id"
          >{{ expandedGame === game.id ? '收起规则' : '筛选规则' }}</F7Button>
        </F7ListItem>

        <template v-if="game.selected && expandedGame === game.id">
          <F7ListItem media-item text="建议尽量设置筛选规则，消息过多容易错过内部沟通消息。" />
          <F7ListInput
            :input-id="`hall-push-price-min-${game.id}`" :value="game.min"
            inputmode="decimal" placeholder="不限" :disabled="saving" @update:value="game.min = $event.trim()"
          >
            <template #label><label :for="`hall-push-price-min-${game.id}`">最低价（元）</label></template>
          </F7ListInput>
          <F7ListInput
            :input-id="`hall-push-price-max-${game.id}`" :value="game.max"
            inputmode="decimal" placeholder="不限" :disabled="saving" @update:value="game.max = $event.trim()"
          >
            <template #label><label :for="`hall-push-price-max-${game.id}`">最高价（元）</label></template>
          </F7ListInput>
          <F7ListInput
            v-model:value="game.include" :input-id="`hall-push-include-${game.id}`"
            placeholder="匹配任一词即可" :disabled="saving"
          >
            <template #label><label :for="`hall-push-include-${game.id}`">包含关键词</label></template>
          </F7ListInput>
          <F7ListInput
            v-model:value="game.exclude" :input-id="`hall-push-exclude-${game.id}`"
            placeholder="匹配任一词就不推送" info="多个词用逗号或空格分隔。" :disabled="saving"
          >
            <template #label><label :for="`hall-push-exclude-${game.id}`">排除关键词</label></template>
          </F7ListInput>
        </template>
      </template>
    </F7List>

    <F7BlockFooter v-if="failure" class="text-color-red" role="alert">{{ failure }}</F7BlockFooter>
    <F7BlockFooter v-if="saved" class="text-color-green" role="status">订阅设置已保存。</F7BlockFooter>
    <F7Block inset>
      <F7Button type="submit" large round fill :disabled.prop="saving">
        {{ saving ? '正在保存…' : '保存订阅' }}
      </F7Button>
    </F7Block>
  </form>

  <F7Block v-else inset>
    <p class="text-color-red" role="alert">{{ failure }}</p>
    <F7Button type="button" round outline @click="loadSubscription">重试</F7Button>
  </F7Block>
</template>

<style scoped>
/* Keep Framework7's hidden checkbox operable by keyboard and assistive technology. */
.m-hall-subscription__game-choice :deep(.item-checkbox > input) {
  position: absolute;
  display: block;
  opacity: 0;
}

.m-hall-subscription__game-choice :deep(input:focus-visible + .icon-checkbox) {
  outline: 2px solid var(--f7-theme-color);
  outline-offset: 2px;
}
</style>
