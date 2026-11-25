// @vitest-environment jsdom
// ↑ 本仓库的测试默认 environment 是 `node`（vitest.config.ts：纯逻辑测试，不依赖 DOM），挂载组件需要 DOM ⇒ **只在本文件**显式覆盖，而不是把全局环境改成 jsdom。
// 本文件只断言**结构与类名**，不断言任何视觉结果（本机无浏览器，见 FRONTEND-THEME §6 第 5 条）。
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import AppIcon from '../AppIcon.vue'

describe('AppIcon', () => {
  it('没有图标名（null / undefined）时不渲染任何元素', () => {
    for (const name of [null, undefined]) {
      const wrapper = mount(AppIcon, { props: { name } })
      expect(wrapper.find('i').exists(), `name=${String(name)} 不该渲染元素`).toBe(false)
      expect(wrapper.html(), `name=${String(name)} 应什么都不渲染`).toBe('<!--v-if-->')
    }
  })

  it('有图标名时外层是 <el-icon>（class 以 el-icon 开头），内层带 layui-icon + layui-icon-<名字>', () => {
    const wrapper = mount(AppIcon, { props: { name: 'home' } })

    // 外层必须是 <el-icon>：EP 的菜单图标对齐/定宽规则选择器是 [class^=el-icon]（class 属性以
    // el-icon 开头，见 element-plus/theme-chalk/el-menu.css），下面这条断言就是那个前提。
    expect(wrapper.element.tagName).toBe('I')
    expect(wrapper.attributes('class')?.startsWith('el-icon')).toBe(true)
    expect(wrapper.attributes('aria-hidden')).toBe('true')

    const inner = wrapper.find('i.layui-icon')
    expect(inner.exists()).toBe(true)
    expect(inner.attributes('class')).toBe('layui-icon layui-icon-home')
  })

  it('对外层类名不追加任何东西（多一个类就不会再以 el-icon 开头）', () => {
    const wrapper = mount(AppIcon, { props: { name: 'set' } })
    expect(wrapper.attributes('class')).toBe('el-icon')
    expect(wrapper.find('i.layui-icon').attributes('class')).toBe('layui-icon layui-icon-set')
  })
})
