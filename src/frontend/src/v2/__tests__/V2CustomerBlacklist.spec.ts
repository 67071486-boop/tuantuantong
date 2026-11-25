// @vitest-environment jsdom
// ↑ 挂载组件需要 DOM（本仓 vitest 默认 environment 是 node，只在本文件覆盖）。
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

/**
 * 「黑名单管理」（客户黑名单）v2 占位页（2026-11-11 客户线第七批 §1.4）的判据：
 * 说明与空态都钉「功能建设中」、零假数据、页内无链接（不跳 v1——菜单侧由
 * `V2_ROUTE_OVERRIDES['customers-blacklist']` 接管，CarbonShell.spec 钉映射、
 * v2Routes.spec 钉生产路由与权限门）。
 */
import V2CustomerBlacklist from '../V2CustomerBlacklist.vue'

describe('V2CustomerBlacklist（黑名单管理 v2 占位）', () => {
  it('占位页：说明 + 空态都钉「功能建设中」，零假数据、无 v1 链接', () => {
    const wrapper = mount(V2CustomerBlacklist)
    expect(wrapper.find('[data-v2-page="customer-blacklist"]').exists()).toBe(true)
    expect(wrapper.find('[data-v2="explain"]').text()).toContain('功能建设中')
    expect(wrapper.find('[data-v2="empty"]').text()).toBe('功能建设中')
    // 页内一个链接都没有：跳 v1 的旧路径已被菜单映射替换，这里兜住页面自身。
    expect(wrapper.findAll('a')).toHaveLength(0)
  })
})
