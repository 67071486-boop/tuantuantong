<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import {
  f7Block as F7Block, f7Button as F7Button, f7Card as F7Card,
  f7Icon as F7Icon, f7Preloader as F7Preloader,
} from 'framework7-vue'
import {
  channelImageUrl, fetchChannelArticles, fetchChannelProfile,
  type ChannelArticleSummary, type ChannelProfile,
} from '@/api/channel'
import MobilePagedList from '../components/MobilePagedList.vue'
import MobilePullToRefresh from '../components/MobilePullToRefresh.vue'
import { formatDateTime } from '../utils/datetime'

const profile = ref<ChannelProfile | null>(null)
const profileLoading = ref(false)
const profileFailed = ref(false)
const articles = ref<ChannelArticleSummary[]>([])
const loading = ref(true)
const loadingMore = ref(false)
const failedPage = ref<number | null>(null)
const hasMore = ref(false)
let page = 0
let revision = 0

async function loadProfile(): Promise<void> {
  if (profileLoading.value) return
  profileLoading.value = true
  profileFailed.value = false
  try {
    profile.value = await fetchChannelProfile()
  } catch {
    profileFailed.value = true
  } finally {
    profileLoading.value = false
  }
}

async function loadArticles(nextPage: number): Promise<void> {
  const current = ++revision
  loading.value = nextPage === 1
  loadingMore.value = nextPage > 1
  failedPage.value = null
  try {
    const result = await fetchChannelArticles(nextPage)
    if (current !== revision) return
    const knownIds = new Set(articles.value.map(({ id }) => id))
    articles.value = nextPage === 1
      ? result.items
      : [...articles.value, ...result.items.filter(({ id }) => !knownIds.has(id))]
    page = result.page
    hasMore.value = page * result.page_size < result.total
  } catch {
    if (current === revision) failedPage.value = nextPage
  } finally {
    if (current === revision) {
      loading.value = false
      loadingMore.value = false
    }
  }
}

function refresh(): void {
  if (!loading.value) void loadArticles(1)
  void loadProfile()
}

function loadMore(): void {
  if (!loading.value && !loadingMore.value && hasMore.value) void loadArticles(page + 1)
}

onMounted(() => {
  void loadArticles(1)
  void loadProfile()
})
</script>

<template>
  <MobilePullToRefresh :refreshing="loading || profileLoading" @refresh="refresh">
    <section class="m-channel" aria-label="工作室频道">
      <F7Card class="m-channel__profile">
        <span class="m-channel__avatar" aria-hidden="true">{{ profile?.avatar_text || '工' }}</span>
        <div class="m-channel__about">
          <h2>{{ profile?.name || '工作室' }}</h2>
          <p v-if="profile">{{ profile.article_count }} 篇文章</p>
          <p v-if="profile?.intro" class="m-channel__intro">{{ profile.intro }}</p>
          <F7Button v-if="profileFailed" type="button" small class="m-channel__retry" @click="loadProfile">
            简介加载失败，重试
          </F7Button>
        </div>
        <F7Button
          type="button" class="m-channel__refresh" aria-label="刷新频道"
          :disabled.prop="loading || profileLoading" @click="refresh"
        >
          <F7Icon f7="arrow_clockwise" :size="20" aria-hidden="true" />
        </F7Button>
      </F7Card>

      <h2 class="m-channel__heading">最新文章</h2>
      <F7Block v-if="loading && articles.length === 0" class="m-channel__state" role="status">
        <F7Preloader :size="20" aria-hidden="true" /> 正在加载文章…
      </F7Block>
      <MobilePagedList
        v-if="articles.length"
        :async-loading="loadingMore ? 'loading' : !loading && failedPage === null && hasMore ? 'load-more' : undefined"
        @load-more="loadMore"
      >
        <ul class="m-channel__articles">
          <li v-for="article in articles" :key="article.id">
            <F7Card outline class="m-channel__card">
              <RouterLink
                class="m-channel__link external"
                :to="{ name: 'm-channel-article', params: { articleId: article.id } }"
              >
                <div class="m-channel__summary">
                  <h3>{{ article.title }}</h3>
                  <p v-if="article.intro">{{ article.intro }}</p>
                  <time v-if="article.published_at" :datetime="article.published_at">
                    {{ formatDateTime(article.published_at).slice(0, 10) }}
                  </time>
                </div>
                <img
                  v-if="channelImageUrl(article.cover_url)" class="m-channel__cover"
                  :src="channelImageUrl(article.cover_url)" alt="" loading="lazy" decoding="async"
                >
              </RouterLink>
            </F7Card>
          </li>
        </ul>
      </MobilePagedList>
      <F7Block v-if="failedPage !== null" class="m-channel__state" role="alert">
        <span>{{ failedPage === 1 ? '文章列表加载失败。' : '更多文章加载失败。' }}</span>
        <F7Button type="button" small outline @click="loadArticles(failedPage)">重试</F7Button>
      </F7Block>
      <F7Block v-else-if="!loading && !articles.length" class="m-channel__state">
        工作室还没有发布文章
      </F7Block>
    </section>
  </MobilePullToRefresh>
</template>

<style scoped>
.m-channel__profile {
  display: flex;
  align-items: flex-start;
  gap: 13px;
  padding: 16px;
  background: var(--m-accent-surface);
}

.m-channel__avatar {
  display: grid;
  flex: none;
  width: 48px;
  height: 48px;
  place-items: center;
  border-radius: 50%;
  background: var(--f7-theme-color);
  color: #fff;
  font-size: 22px;
  font-weight: 700;
}

.m-channel__about {
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
}

.m-channel__about h2 {
  margin: 1px 0 3px;
  font-size: 18px;
  line-height: 1.4;
}

.m-channel__about p {
  margin: 0;
  color: var(--m-muted);
  font-size: 13px;
}

.m-channel__about .m-channel__intro {
  margin-top: 7px;
  white-space: pre-line;
}

.m-channel__refresh.button {
  width: 44px;
  height: 44px;
  margin: -6px -6px 0 0;
  padding: 0;
}

.m-channel__retry.button {
  width: fit-content;
  padding-inline: 0;
}

.m-channel__heading {
  margin: 24px 0 12px;
  font-size: 18px;
}

.m-channel__articles {
  display: grid;
  gap: 10px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.m-channel__card.card {
  --f7-card-outline-border-color: var(--m-border);
}

.m-channel__link {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px;
  border-radius: var(--f7-card-border-radius);
  color: inherit;
  text-decoration: none;
}

.m-channel__link:focus-visible {
  outline: 2px solid var(--f7-theme-color);
  outline-offset: 2px;
}

.m-channel__link:active {
  background: var(--m-accent-surface);
}

.m-channel__summary {
  min-width: 0;
  overflow-wrap: anywhere;
}

.m-channel__summary h3 {
  margin: 0;
  font-size: 16px;
  line-height: 1.4;
}

.m-channel__summary p {
  display: -webkit-box;
  overflow: hidden;
  margin: 4px 0 0;
  color: var(--m-muted);
  font-size: 13px;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.m-channel__summary time {
  display: block;
  margin-top: 6px;
  color: var(--m-muted);
  font-size: 12px;
}

.m-channel__cover {
  flex: none;
  width: min(32%, 105px);
  aspect-ratio: 16 / 10;
  border-radius: 9px;
  object-fit: cover;
}

.m-channel__state {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--m-muted);
  text-align: center;
}

.m-channel__state .button {
  width: auto;
}
</style>
