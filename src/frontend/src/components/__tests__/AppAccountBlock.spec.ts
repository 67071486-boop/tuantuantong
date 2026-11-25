// @vitest-environment jsdom
// ↑ 本仓库的测试默认 environment 是 `node`（vitest.config.ts：纯逻辑测试，不依赖 DOM），挂载组件需要 DOM ⇒ **只在本文件**显式覆盖（写法与 components/__tests__/AppIcon.spec.ts 一致）。
// 本文件只断言**结构与文本**，不断言任何视觉结果（本机无浏览器，见 FRONTEND-THEME §6 第 5 条）。
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import AppAccountBlock from '../AppAccountBlock.vue'

const props = {
  userName: '示例账号',
  role: '系统管理员',
  avatarUrl: null,
}

describe('AppAccountBlock', () => {
  it('渲染头像与账号名(职位)', () => {
    const w = mount(AppAccountBlock, { props })
    expect(w.find('.shell-account__avatar').exists()).toBe(true)
    // 侧栏这一处按对标物抓取页的方括号形式（顶栏那处是半角圆括号，见 config/account.ts）
    expect(w.get('.shell-account__user').text()).toBe('示例账号[系统管理员]')
  })

  it('不渲染工作室名（它由 .shell-logo 槽位渲染，同一文案不出现两次）', () => {
    const w = mount(AppAccountBlock, { props })
    expect(w.find('.shell-account__studio').exists()).toBe(false)
  })

  it('无上传头像时渲染**生成的**头像（纯前端 SVG data: URI，不引用外部图形）', () => {
    const w = mount(AppAccountBlock, { props })
    // 生成器是 `config/avatar.ts`：圆形由使用方 CSS 给，SVG 自己不画圆。
    expect(w.get('img').attributes('src')).toMatch(/^data:image\/svg\+xml/)
  })

  it('给了头像地址时渲染 <img> 并带上该地址', () => {
    const w = mount(AppAccountBlock, { props: { ...props, avatarUrl: '/x.png' } })
    expect(w.get('img').attributes('src')).toBe('/x.png')
  })
})
