// @vitest-environment jsdom
import { createPinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { request } from '@/api/http'
import ChannelPage from '../ChannelPage.vue'
import ChannelArticlePage from '../ChannelArticlePage.vue'
import { installFramework7ForTest } from '../../styles/__tests__/framework7Setup'

vi.mock('@/api/http', () => ({ request: vi.fn() }))

installFramework7ForTest()
enableAutoUnmount(afterEach)
beforeEach(() => vi.resetAllMocks())

async function open(path: string) {
  const router = createRouter({
    history: createMemoryHistory('/m/'),
    routes: [
      { path: '/channel', name: 'm-channel', component: ChannelPage },
      { path: '/channel/articles/:articleId', name: 'm-channel-article', component: ChannelArticlePage },
    ],
  })
  await router.push(path)
  const wrapper = mount(RouterView, {
    attachTo: document.getElementById('framework7-root')!,
    global: { plugins: [createPinia(), router] },
  })
  await flushPromises()
  return { wrapper, router }
}

const profile = { name: '样例频道甲', intro: '用于测试的频道介绍', article_count: 2, avatar_text: '例' }
const article = {
  id: 42, title: '十月工作室动态', intro: '本月合集已发布',
  cover_url: '/api/media/blob/current/users/1/article-media.cover.jpg',
  published_at: '2026-10-01T22:30:00Z',
}

it('打开频道中已发布的文章，按北京时间展示日期和安全的正文块，并可返回频道', async () => {
  vi.mocked(request).mockImplementation(async ({ url, method }) => {
    if (method !== 'GET') throw new Error(`unexpected ${method}`)
    if (url === '/api/channel/profile/') return profile
    if (url === '/api/channel/articles/') return { items: [article], total: 1, page: 1, page_size: 10 }
    if (url === '/api/channel/articles/42/') {
      return {
        ...article, studio_name: profile.name,
        blocks: [
          { type: 'h2', spans: [{ text: '正文小节', bold: true, italic: false }] },
          { type: 'p', spans: [{ text: '<img onerror=alert(1)>', bold: false, italic: true }] },
          { type: 'img', url: '//example.invalid/image.png' },
          { type: 'img', url: 'https://test.cos.ap-test.myqcloud.com/signed.png?signature=abc' },
          { type: 'script', spans: [{ text: '未知块', bold: false, italic: false }] },
        ],
      }
    }
    throw new Error(`unexpected ${url}`)
  })

  const { wrapper, router } = await open('/channel')
  expect(wrapper.text()).toContain('用于测试的频道介绍')
  expect(wrapper.text()).toContain('本月合集已发布')
  expect(wrapper.get('time').text()).toBe('2026-10-02')
  expect(wrapper.get('img').attributes('src')).toBe(article.cover_url)
  await wrapper.get('a[href="/m/channel/articles/42"]').trigger('click')
  await vi.waitFor(() => expect(router.currentRoute.value.path).toBe('/channel/articles/42'))
  await flushPromises()
  expect(request).toHaveBeenLastCalledWith({ method: 'GET', url: '/api/channel/articles/42/' })
  expect(wrapper.get('article h2').text()).toBe(article.title)
  expect(wrapper.get('article h3').text()).toBe('正文小节')
  expect(wrapper.text()).toContain('<img onerror=alert(1)>')
  expect(wrapper.text()).not.toContain('未知块')
  expect(wrapper.findAll('article img').map((image) => image.attributes('src'))).toEqual([
    article.cover_url, 'https://test.cos.ap-test.myqcloud.com/signed.png?signature=abc',
  ])

  await wrapper.get('[data-testid="channel-article-back"]').trigger('click')
  await vi.waitFor(() => expect(router.currentRoute.value.path).toBe('/channel'))
})

it('下一页加载失败后可重试，刷新后以第一页替换旧文章', async () => {
  let nextPageAttempt = 0
  let firstPageAttempt = 0
  const secondPageArticle = { ...article, id: 43, title: '上一期动态' }
  const delayedArticle = { ...article, id: 44, title: '更早的动态' }
  const delayedPage = { items: [delayedArticle], total: 21, page: 3, page_size: 10 }
  let deliverDelayedPage!: (value: typeof delayedPage) => void
  const delayed = new Promise<typeof delayedPage>((resolve) => { deliverDelayedPage = resolve })
  vi.mocked(request).mockImplementation(async ({ url, params }) => {
    if (url === '/api/channel/profile/') return profile
    if (url !== '/api/channel/articles/') throw new Error(`unexpected ${url}`)
    if (params.page === 2) {
      nextPageAttempt += 1
      if (nextPageAttempt === 1) throw new Error('offline')
      return { items: [secondPageArticle], total: 21, page: 2, page_size: 10 }
    }
    if (params.page === 3) return delayed
    firstPageAttempt += 1
    if (firstPageAttempt === 1) return { items: [article], total: 21, page: 1, page_size: 10 }
    return { items: [], total: 0, page: 1, page_size: 10 }
  })
  const { wrapper } = await open('/channel')
  const loadMore = () => {
    const button = wrapper.findAll('button').find((candidate) => candidate.text() === '加载更多')
    expect(button).toBeDefined()
    return button!.trigger('click')
  }
  await loadMore()
  await flushPromises()
  expect(wrapper.get('[role="alert"]').text()).toContain('更多文章加载失败。')
  expect(wrapper.text()).toContain(article.title)
  expect(wrapper.text()).not.toContain('工作室还没有发布文章')

  await wrapper.get('[role="alert"] button').trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain(secondPageArticle.title)

  await loadMore()
  expect(request).toHaveBeenLastCalledWith({ method: 'GET', url: '/api/channel/articles/', params: { page: 3 } })
  await wrapper.get('button[aria-label="刷新频道"]').trigger('click')
  await flushPromises()
  deliverDelayedPage(delayedPage)
  await flushPromises()
  expect(wrapper.text()).not.toContain(secondPageArticle.title)
  expect(wrapper.text()).not.toContain(delayedArticle.title)
  expect(wrapper.text()).not.toContain('加载更多')
  expect(wrapper.text()).toContain('工作室还没有发布文章')
})
