// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'
import V2WecomArchiveSearch from '../V2WecomArchiveSearch.vue'

vi.mock('../wecomArchive', async (importOriginal) => ({
  ...await importOriginal<typeof import('../wecomArchive')>(),
  archiveAccess: vi.fn().mockResolvedValue({ enabled: true }),
}))

describe('独立聊天搜索的来源单号', () => {
  it('搜索页保留挂载时，从另一笔订单返回仍会更新预填单号', async () => {
    const Empty = { template: '<div />' }
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/orders', name: 'order-detail', component: Empty },
        { path: '/v2/wecom-archive', name: 'v2-wecom-archive', component: Empty },
      ],
    })
    const enterFromOrder = (wecomArchiveKeyword: string) => router.push({
      name: 'v2-wecom-archive',
      state: { wecomArchiveKeyword },
    })
    await enterFromOrder('第一张来源单号')
    const wrapper = mount({
      components: { V2WecomArchiveSearch },
      template: '<V2WecomArchiveSearch v-show="$route.name === \'v2-wecom-archive\'" />',
    }, { global: { plugins: [router] } })
    await flushPromises()
    const query = () => wrapper.get<HTMLInputElement>('input[type=search]').element.value
    expect(query()).toBe('第一张来源单号')

    await router.push({ name: 'order-detail' })
    await enterFromOrder('下一张来源单号')
    await flushPromises()

    expect(query()).toBe('下一张来源单号')
    expect(router.currentRoute.value.fullPath).toBe('/v2/wecom-archive')
    wrapper.unmount()
  })
})
