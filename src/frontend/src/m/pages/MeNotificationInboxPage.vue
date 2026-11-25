<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import {
  f7Badge as F7Badge, f7Block as F7Block, f7BlockFooter as F7BlockFooter, f7Button as F7Button,
  f7Icon as F7Icon, f7Link as F7Link, f7List as F7List, f7ListItem as F7ListItem, f7Preloader as F7Preloader,
  f7Segmented as F7Segmented,
} from 'framework7-vue'
import MobilePageBar from '../components/MobilePageBar.vue'
import { request } from '@/api/http'
import { useWorkerBack } from '../router/useWorkerBack'
import { mRoutePathOf, sanitizeMRedirect } from '../utils/redirect'

type Category = '' | 'order' | 'settlement'
type Notice = {
  id: number
  title: string
  order_no: string
  lines: string[]
  target: string | null
  created_at: string
  read_at: string | null
}
type NoticePage = {
  items: Notice[]
  unread_count: number
  total: number
  page_size: number
}

const filters: { value: Category; label: string }[] = [
  { value: '', label: '全部' },
  { value: 'order', label: '订单' },
  { value: 'settlement', label: '结算' },
]
const inboxUrl = '/api/auth/wxpusher/inbox'
const goBack = useWorkerBack(() => ({ name: 'm-me-notifications' }))
const category = ref<Category>('')
const page = ref(1)
const data = ref<NoticePage | null>(null)
const loading = ref(false)
const loadFailed = ref(false)
const opened = ref<number | null>(null)
const reading = new Set<number>()
const readError = ref<number | null>(null)
let revision = 0
const totalPages = computed(() => data.value ? Math.max(1, Math.ceil(data.value.total / data.value.page_size)) : 1)
const dateFormat = new Intl.DateTimeFormat('zh-CN', {
  month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
})

function dateText(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : dateFormat.format(date)
}

async function load(): Promise<void> {
  const current = ++revision
  loading.value = true
  loadFailed.value = false
  try {
    const result = await request<NoticePage>({
      url: inboxUrl, method: 'GET', params: { category: category.value || undefined, page: page.value },
    })
    if (current === revision) data.value = result
  } catch {
    if (current === revision) loadFailed.value = true
  } finally {
    if (current === revision) loading.value = false
  }
}

function changePage(next: number): void {
  opened.value = null
  page.value = next
}

function selectFilter(next: Category): void {
  category.value = next
  changePage(1)
}

async function markRead(notice: Notice): Promise<void> {
  if (notice.read_at !== null || reading.has(notice.id)) return
  reading.add(notice.id)
  readError.value = null
  const started = revision
  try {
    const { read_at } = await request<{read_at: string}>({ url: `${inboxUrl}/${notice.id}/read`, method: 'POST' })
    if (started === revision && data.value) {
      notice.read_at = read_at
      data.value.unread_count = Math.max(0, data.value.unread_count - 1)
    } else {
      await load()
    }
  } catch {
    readError.value = notice.id
  } finally {
    reading.delete(notice.id)
  }
}

function toggleNotice(notice: Notice): void {
  opened.value = opened.value === notice.id ? null : notice.id
  if (opened.value === notice.id) void markRead(notice)
}

function targetPath(notice: Notice): string | null {
  const sitePath = sanitizeMRedirect(notice.target)
  return sitePath === null ? null : mRoutePathOf(sitePath)
}

watch([category, page], load, { immediate: true })
</script>

<template>
  <section class="m-notification-inbox">
    <MobilePageBar title="通知记录" back-test-id="notification-inbox-back" @back="goBack" />

    <F7Block inset class="m-notification-inbox__controls">
      <F7Segmented strong tag="nav" aria-label="通知分类">
        <F7Button
          v-for="filter in filters"
          :key="filter.value"
          type="button"
          :active="category === filter.value"
          :aria-pressed="category === filter.value"
          @click="selectFilter(filter.value)"
        >{{ filter.label }}</F7Button>
      </F7Segmented>
    </F7Block>
    <F7BlockFooter v-if="data" class="m-notification-inbox__unread" aria-live="polite">
      {{ data.unread_count ? `未读 ${data.unread_count} 条` : '暂无未读通知' }}
    </F7BlockFooter>

    <F7Block v-if="loading" inset class="m-notification-inbox__state" role="status">
      <F7Preloader size="20" aria-hidden="true" /> 正在加载通知…
    </F7Block>
    <F7Block v-else-if="loadFailed" inset class="m-notification-inbox__state" role="alert">
      <p>暂时无法加载通知。</p>
      <F7Button small outline type="button" @click="load">重试</F7Button>
    </F7Block>
    <F7Block v-else-if="!data?.items.length" inset class="m-notification-inbox__state">暂无通知</F7Block>
    <F7List v-else strong inset outline dividers class="m-notification-inbox__list">
      <F7ListItem v-for="notice in data.items" :key="notice.id">
        <F7Button type="button" class="m-notification-inbox__headline" :aria-expanded="opened === notice.id"
          :aria-controls="`notification-${notice.id}`" @click="toggleNotice(notice)">
          <span class="m-notification-inbox__summary">
            <span class="m-notification-inbox__title">
              <F7Badge v-if="notice.read_at === null" class="m-notification-inbox__dot" aria-label="未读" />
              {{ notice.title }}
            </span>
            <span class="m-notification-inbox__meta">
              <span v-if="notice.order_no">订单 {{ notice.order_no }}</span>
              <time :datetime="notice.created_at">{{ dateText(notice.created_at) }}</time>
            </span>
          </span>
          <F7Icon :f7="opened === notice.id ? 'chevron_up' : 'chevron_down'" :size="12"
            class="m-notification-inbox__chevron" aria-hidden="true" />
        </F7Button>
        <template #root>
          <F7Block v-show="opened === notice.id" :id="`notification-${notice.id}`" class="m-notification-inbox__body">
            <p v-for="(line, index) in notice.lines" :key="index">{{ line }}</p>
            <p v-if="readError === notice.id" role="alert">
              未能标记已读。<F7Button type="button" small @click="markRead(notice)">重试</F7Button>
            </p>
            <RouterLink v-if="targetPath(notice)" v-slot="{ href, navigate }" custom :to="targetPath(notice) ?? ''">
              <F7Link :href="href" external data-testid="notification-inbox-target" @click="navigate">前往查看</F7Link>
            </RouterLink>
          </F7Block>
        </template>
      </F7ListItem>
    </F7List>

    <F7Block v-if="!loading && !loadFailed && totalPages > 1" inset>
      <nav class="m-notification-inbox__paging" aria-label="通知翻页">
        <F7Button type="button" round outline :disabled.prop="page === 1" @click="changePage(page - 1)">上一页</F7Button>
        <span>第 {{ page }} / {{ totalPages }} 页</span>
        <F7Button type="button" round outline :disabled.prop="page >= totalPages" @click="changePage(page + 1)">下一页</F7Button>
      </nav>
    </F7Block>
  </section>
</template>

<style scoped>
.m-notification-inbox {
  box-sizing: border-box;
  min-height: 100dvh;
  max-width: 560px;
  margin-inline: auto;
  padding-bottom: 24px;
  background: var(--f7-page-bg-color, #fff);
}

.m-notification-inbox__controls {
  margin-block-end: 8px;
}

.m-notification-inbox__unread {
  margin-block: 0 12px;
}

.m-notification-inbox__list.list {
  --f7-list-item-border-color: var(--m-border);
  --f7-list-outline-border-color: var(--m-border);
  --f7-list-inset-border-radius: 18px;
  margin-block-start: 0;
}

.m-notification-inbox__paging {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
}

.m-notification-inbox__paging .button {
  width: auto;
}

.m-notification-inbox__state {
  color: var(--m-muted);
  text-align: center;
}

.m-notification-inbox__headline.button {
  --f7-button-text-color: var(--f7-text-color);
  --f7-button-padding-horizontal: 0px;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 12px;
  min-height: 56px;
  padding-block: 6px;
  height: auto;
  text-align: start;
  white-space: normal;
  overflow-wrap: anywhere;
}

.m-notification-inbox__summary {
  display: grid;
  gap: 5px;
}

.m-notification-inbox__chevron {
  color: var(--m-muted);
}

.m-notification-inbox__title,
.m-notification-inbox__meta {
  display: flex;
  align-items: center;
  gap: 8px;
}

.m-notification-inbox__meta {
  flex-wrap: wrap;
  justify-content: space-between;
  color: var(--m-muted);
  font-size: 12px;
}

.m-notification-inbox__meta time {
  flex: none;
}

.m-notification-inbox__dot.badge {
  --f7-badge-size: 8px;
  --f7-badge-bg-color: var(--f7-theme-color);
  flex: none;
  padding: 0;
}

.m-notification-inbox__body {
  margin-block: 0 12px;
  padding-block: 1px 12px;
  background: var(--m-accent-surface);
  font-size: 14px;
  overflow-wrap: anywhere;
}
</style>
