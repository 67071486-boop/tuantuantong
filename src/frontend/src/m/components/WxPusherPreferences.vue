<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  f7Block as F7Block, f7BlockFooter as F7BlockFooter, f7BlockTitle as F7BlockTitle, f7Button as F7Button,
  f7Icon as F7Icon, f7List as F7List, f7ListItem as F7ListItem, f7Preloader as F7Preloader,
} from 'framework7-vue'
import { request } from '@/api/http'
import MobileToggle from './MobileToggle.vue'

const ORDER_EVENTS = [
  { key: 'assigned', label: '新指派给我' },
  { key: 'unassigned', label: '撤销指派' },
  { key: 'review_rejected', label: '交单被退回' },
  { key: 'completed', label: '订单已完成' },
  { key: 'paused_resumed', label: '订单暂停或恢复' },
  { key: 'issue', label: '订单被标为问题单' },
  { key: 'refunded', label: '订单已退单' },
] as const

type OrderEvent = typeof ORDER_EVENTS[number]['key']

interface PushPreferences {
  order_enabled: boolean
  settlement_enabled: boolean
  group_chat_enabled?: boolean
  order_events: Record<OrderEvent, boolean>
}

type PreferenceChange = Partial<Pick<PushPreferences, 'order_enabled' | 'settlement_enabled'>> & {
  order_events?: Partial<PushPreferences['order_events']>
}

const PREFERENCES_URL = '/api/auth/wxpusher/preferences'

const props = defineProps<{ bound: boolean; enabled: boolean; orderDetails: boolean }>()
const preferences = ref<PushPreferences | null>(null)
const orderDetailsOpen = ref(false)
const loading = ref(true)
const saving = ref(false)
const failure = ref('')
const enabledCount = computed(() => ORDER_EVENTS.filter(({ key }) => preferences.value?.order_events[key]).length)

async function loadPreferences(): Promise<void> {
  loading.value = true
  failure.value = ''
  try {
    preferences.value = await request<PushPreferences>({ url: PREFERENCES_URL, method: 'GET' })
  } catch {
    failure.value = '推送设置加载失败，请重试。'
  } finally {
    loading.value = false
  }
}

async function savePreference(data: PreferenceChange): Promise<void> {
  if (saving.value || !preferences.value) return
  saving.value = true
  failure.value = ''
  try {
    preferences.value = await request<PushPreferences>({ url: PREFERENCES_URL, method: 'PATCH', data })
  } catch {
    failure.value = '未能确认保存，请再试一次。'
  } finally {
    saving.value = false
  }
}

watch(() => props.enabled, (enabled) => {
  if (enabled) void loadPreferences()
}, { immediate: true })
</script>

<template>
  <article class="m-push-preferences" data-testid="wxpusher-preferences" aria-label="推送设置">
    <F7BlockTitle v-if="!orderDetails" role="heading" aria-level="2">推送设置</F7BlockTitle>
    <F7BlockFooter v-if="!orderDetails && enabled && !bound">绑定 WxPusher 后接收手机推送。</F7BlockFooter>

    <F7List v-if="$slots.entries || (enabled && !loading && preferences)" strong inset outline dividers>
      <slot name="entries" />
      <template v-if="enabled && !loading && preferences">
        <F7ListItem v-if="!orderDetails" class="m-push-preferences__disclosure">
          <template #title>
            <F7Button
              type="button" class="m-push-preferences__detail" data-testid="wxpusher-order-details"
              :aria-expanded="orderDetailsOpen"
              :aria-controls="orderDetailsOpen ? 'wxpusher-order-events' : undefined"
              @click="orderDetailsOpen = !orderDetailsOpen"
            >
              <span class="m-push-preferences__label">
                <span>订单状态变更</span>
                <small class="item-footer">
                  {{ preferences.order_enabled ? `已选 ${enabledCount} 项` : `已关闭 · 保留 ${enabledCount} 项选择` }}
                </small>
              </span>
              <F7Icon :f7="orderDetailsOpen ? 'chevron_down' : 'chevron_right'" :size="14" aria-hidden="true" />
            </F7Button>
          </template>
          <template #after>
            <MobileToggle
              label="订单状态变更"
              :checked="preferences.order_enabled"
              :disabled="saving"
              @toggle="savePreference({ order_enabled: !preferences.order_enabled })"
            />
          </template>
        </F7ListItem>
        <li v-if="orderDetails || orderDetailsOpen">
          <F7List id="wxpusher-order-events" class="m-push-preferences__events" dividers aria-label="订单通知类型" role="group">
            <F7ListItem
              v-if="!preferences.order_enabled" media-item text="总开关已关闭，以下选择会在重新开启后生效。"
            />
            <F7ListItem v-for="event in ORDER_EVENTS" :key="event.key" :title="event.label">
              <template #after>
                <MobileToggle
                  :label="event.label"
                  :checked="preferences.order_events[event.key]"
                  :disabled="saving"
                  @toggle="savePreference({ order_events: { [event.key]: !preferences.order_events[event.key] } })"
                />
              </template>
            </F7ListItem>
          </F7List>
        </li>
        <F7ListItem v-if="!orderDetails" title="结算结果">
          <template #after>
            <MobileToggle
              label="结算结果"
              :checked="preferences.settlement_enabled"
              :disabled="saving"
              @toggle="savePreference({ settlement_enabled: !preferences.settlement_enabled })"
            />
          </template>
        </F7ListItem>
        <F7ListItem v-if="!orderDetails && preferences.group_chat_enabled" title="聊天提醒" :after="bound ? '已开启' : '绑定后生效'" />
      </template>
    </F7List>
    <F7Block v-if="enabled && loading" inset role="status">
      <F7Preloader size="20" aria-hidden="true" /> 正在读取推送设置…
    </F7Block>
    <F7BlockFooter v-if="enabled && saving" role="status">正在保存…</F7BlockFooter>
    <F7BlockFooter v-if="enabled && failure" class="text-color-red" role="alert">{{ failure }}</F7BlockFooter>
    <F7Block v-if="enabled && !loading && !preferences" inset>
      <F7Button type="button" round outline data-testid="retry-wxpusher-preferences" @click="loadPreferences">重试</F7Button>
    </F7Block>
  </article>
</template>

<style scoped>
.m-push-preferences :deep(.m-push-preferences__disclosure) {
  --f7-list-item-padding-vertical: 4px;
  --f7-list-item-after-padding: 12px;
}

.m-push-preferences :deep(.m-push-preferences__disclosure .item-title) {
  flex: 1;
  overflow: visible;
}

.m-push-preferences :deep(.m-push-preferences__detail.button) {
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  min-height: 44px;
  height: auto;
  padding: 0;
  color: inherit;
  font: inherit;
  text-align: start;
}

.m-push-preferences :deep(.m-push-preferences__detail .icon) {
  flex: none;
  color: var(--f7-list-chevron-icon-color);
}

.m-push-preferences__label {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  column-gap: 6px;
  min-width: 0;
  white-space: normal;
}

.m-push-preferences__events {
  --f7-list-bg-color: transparent;

  margin: 0;
}
</style>
