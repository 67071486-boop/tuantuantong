<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import {
  f7Block as F7Block, f7Button as F7Button, f7Preloader as F7Preloader,
} from 'framework7-vue'
import { channelImageUrl, fetchChannelArticle, type ChannelArticle, type ChannelArticleSpan } from '@/api/channel'
import { BizError } from '@/api/errors'
import { ErrorCode } from '@/config/errorCodes'
import MobilePageBar from '../components/MobilePageBar.vue'
import MobilePhotoBrowser from '../components/MobilePhotoBrowser.vue'
import { useWorkerBack } from '../router/useWorkerBack'
import { formatDateTime } from '../utils/datetime'

type ArticleSection =
  | { tag: 'p' | 'h3' | 'h4'; spans: ChannelArticleSpan[] }
  | { tag: 'ul'; items: ChannelArticleSpan[][] }
  | { tag: 'img'; url: string }

const route = useRoute()
const goBack = useWorkerBack(() => ({ name: 'm-channel' }))
const article = ref<ChannelArticle | null>(null)
const state = ref<'loading' | 'unavailable' | 'error' | 'ready'>('loading')
const previewUrl = ref('')
const previewOpen = ref(false)
let revision = 0

const textTags = { p: 'p', h2: 'h3', h3: 'h4' } as const
const coverUrl = computed(() => channelImageUrl(article.value?.cover_url ?? ''))
const dateText = computed(() => formatDateTime(article.value?.published_at).slice(0, 10))
const sections = computed(() => {
  const result: ArticleSection[] = []
  for (const block of article.value?.blocks ?? []) {
    switch (block.type) {
      case 'img': {
        const url = channelImageUrl(block.url)
        if (url) result.push({ tag: 'img', url })
        break
      }
      case 'li': {
        const list = result[result.length - 1]
        if (list?.tag === 'ul') list.items.push(block.spans)
        else result.push({ tag: 'ul', items: [block.spans] })
        break
      }
      case 'p':
      case 'h2':
      case 'h3':
        result.push({ tag: textTags[block.type], spans: block.spans })
    }
  }
  return result
})

async function load(): Promise<void> {
  const current = ++revision
  article.value = null
  previewOpen.value = false
  const id = route.params.articleId
  if (typeof id !== 'string' || !/^[1-9]\d*$/.test(id)) {
    state.value = 'unavailable'
    return
  }
  state.value = 'loading'
  try {
    const result = await fetchChannelArticle(id)
    if (current !== revision) return
    article.value = result
    state.value = 'ready'
  } catch (error) {
    if (current !== revision) return
    state.value = error instanceof BizError && error.code === ErrorCode.BUSINESS
      ? 'unavailable'
      : 'error'
  }
}

function openImage(url: string): void {
  previewUrl.value = url
  previewOpen.value = true
}

watch(() => route.params.articleId, load, { immediate: true })
</script>

<template>
  <section class="m-channel-article">
    <MobilePageBar title="文章详情" back-test-id="channel-article-back" @back="goBack" />

    <F7Block v-if="state === 'loading'" class="m-channel-article__state" role="status">
      <F7Preloader :size="20" aria-hidden="true" /> 正在加载文章…
    </F7Block>
    <F7Block v-else-if="state === 'unavailable'" class="m-channel-article__state" role="status">
      文章不存在或已下架
    </F7Block>
    <F7Block v-else-if="state === 'error'" class="m-channel-article__state" role="alert">
      <p>文章加载失败。</p>
      <F7Button type="button" small outline @click="load">重试</F7Button>
    </F7Block>
    <article v-else-if="article" class="m-channel-article__content">
      <header>
        <h2>{{ article.title }}</h2>
        <div class="m-channel-article__meta">
          <span v-if="article.studio_name">{{ article.studio_name }}</span>
          <time v-if="dateText" :datetime="article.published_at ?? undefined">{{ dateText }}</time>
        </div>
      </header>
      <button v-if="coverUrl" type="button" class="m-channel-article__image" aria-label="查看封面" @click="openImage(coverUrl)">
        <img :src="coverUrl" alt="" decoding="async">
      </button>
      <div class="m-channel-article__body">
        <template v-for="(section, index) in sections" :key="index">
          <button
            v-if="section.tag === 'img'" type="button" class="m-channel-article__image"
            aria-label="查看正文图片" @click="openImage(section.url)"
          >
            <img :src="section.url" alt="" loading="lazy" decoding="async">
          </button>
          <ul v-else-if="section.tag === 'ul'">
            <li v-for="(item, itemIndex) in section.items" :key="itemIndex">
              <span v-for="(span, spanIndex) in item" :key="spanIndex" :class="{ bold: span.bold, italic: span.italic }">{{ span.text }}</span>
            </li>
          </ul>
          <component :is="section.tag" v-else>
            <span v-for="(span, spanIndex) in section.spans" :key="spanIndex" :class="{ bold: span.bold, italic: span.italic }">{{ span.text }}</span>
          </component>
        </template>
      </div>
    </article>
    <MobilePhotoBrowser v-model:visible="previewOpen" :images="[previewUrl]" />
  </section>
</template>

<style scoped>
.m-channel-article {
  box-sizing: border-box;
  min-height: 100dvh;
  max-width: 560px;
  margin-inline: auto;
  background: var(--f7-page-bg-color);
}

.m-channel-article__content {
  padding: 16px 20px calc(28px + env(safe-area-inset-bottom, 0px));
  overflow-wrap: anywhere;
}

.m-channel-article__content h2 {
  margin: 0 0 10px;
  font-size: clamp(24px, 5.8vw, 30px);
  line-height: 1.35;
}

.m-channel-article__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 7px 14px;
  color: var(--m-muted);
  font-size: 13px;
}

.m-channel-article__body {
  margin-top: 22px;
  font-size: 16px;
  line-height: 1.75;
  white-space: pre-line;
}

.m-channel-article__body :is(p, ul, h3, h4) {
  margin: 0 0 14px;
}

.m-channel-article__body h3 {
  font-size: 20px;
}

.m-channel-article__body h4 {
  font-size: 17px;
}

.m-channel-article__body ul {
  padding-inline-start: 24px;
}

.m-channel-article__body .bold {
  font-weight: 700;
}

.m-channel-article__body .italic {
  font-style: italic;
}

.m-channel-article__image {
  display: block;
  width: 100%;
  margin: 18px 0;
  padding: 0;
  overflow: hidden;
  border: 0;
  border-radius: 12px;
  background: none;
  cursor: zoom-in;
}

.m-channel-article__image:focus-visible {
  outline: 2px solid var(--f7-theme-color);
  outline-offset: 2px;
}

.m-channel-article__image img {
  display: block;
  width: 100%;
}

.m-channel-article__state {
  color: var(--m-muted);
  text-align: center;
}

.m-channel-article__state .button {
  width: auto;
  margin-inline: auto;
}
</style>
