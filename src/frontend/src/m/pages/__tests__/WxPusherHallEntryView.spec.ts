// @vitest-environment jsdom
import { enableAutoUnmount, mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'
import WxPusherHallEntryView from '../WxPusherHallEntryView.vue'

enableAutoUnmount(afterEach)
afterEach(() => vi.restoreAllMocks())

const routePath = '/wxpusher/hall-entry?entry=wxpusher'
const hallHash = '#hall=.synthetic:ticket:a_b-c'

async function clickRetry(query = '', hash = hallHash) {
  const router = createRouter({
    history: createMemoryHistory('/m/'),
    routes: [
      { path: '/wxpusher/hall-entry', name: 'm-wxpusher-hall-entry', component: WxPusherHallEntryView },
      { path: '/login', name: 'm-login', component: { template: '<div />' } },
    ],
  })
  const reload = vi.fn()
  const replace = vi.fn()
  const href = `https://example.test/m${routePath}${query}${hash}`
  const { pathname, search } = new URL(href)
  vi.spyOn(window, 'location', 'get').mockReturnValue({ href, pathname, search, reload, replace } as unknown as Location)
  await router.push(routePath + query + hash)
  const wrapper = mount(WxPusherHallEntryView, { global: { plugins: [router] } })

  await wrapper.get('button').trigger('click')
  return { reload, replace, wrapper }
}

it.each([hallHash, '#hall=.synthetic%3Aticket%3Aa_b-c'])('原地址只有票据 %s 时重新加载网页', async (hash) => {
  const { reload, replace } = await clickRetry('', hash)
  expect(reload).toHaveBeenCalledOnce()
  expect(replace).not.toHaveBeenCalled()
})

it.each(['&retry=1', '&source=peer&ref=23'])('重试时清除 %s，保留票据并在网页继续', async (query) => {
  const { reload, replace } = await clickRetry(query)
  expect(replace).toHaveBeenCalledExactlyOnceWith('/m' + routePath + hallHash)
  expect(reload).not.toHaveBeenCalled()
})

it('共用链接的重试和密码登录都保留同一订单', async () => {
  const shared = '&shared=1&source=peer&ref=23'
  const { reload, replace, wrapper } = await clickRetry(shared + '&retry=1', '')
  expect(replace).toHaveBeenCalledExactlyOnceWith('/m' + routePath + shared + '&open=web')
  expect(reload).not.toHaveBeenCalled()

  const password = wrapper.get('a')
  expect(password.text()).toBe('账号密码登录')
  const href = new URL(password.attributes('href') ?? '', 'https://example.test')
  expect(href.pathname).toBe('/m/login')
  expect(href.searchParams.get('redirect')).toBe('/m/hall?claimSource=peer&claimRef=23')
})
