<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import AppIcon from '@/components/AppIcon.vue'
import { UI_ICONS } from '@/config/menuIcons'
import type { PageTab } from '@/composables/usePageTabs'

/**
 * 内容区上方的页签条。对标物的**结构**（`.tools/recon/home.html:894-915`，`#LAY_app_tabs`）：刷新控件、▾（三个 close* 菜单项）、页签列表——按同一形状实现，但只放**一个**刷新控件（第二个 refresh-3 是对标物自己的重复入口，没有独立语义）。
 * 三种机制的行为出处（都在 `.tools/recon/` 与它线上同源的 layuiadmin 库里核过）：增/切 = `layuiadmin_index.js` 的 `openTabsPage`（去重键 = 完整 url）；关闭 = layui `element` 模块的 `tabDelete`（先右后左）；关闭当前/其它/全部 + 首页不可关 = layuiAdmin `lib/admin.js` 的三个处理器。纯逻辑在 `composables/usePageTabs.ts`，本组件**只渲染与发事件**。刷新：只发 `refresh` 事件、不自己调 `window.location` 或 `router.go(0)`，由 `AppShell` 累加计数、`AppTabbedContent` **只丢弃当前页签的缓存并重挂**（D-0220）。
 * ⚠️ 两处**字形用文字、不是字体图标**（已登记交付报告）：① 关闭按钮 `×`——对标物是字体图标，但 `styles/layui-icons.css` 里**没有** `.layui-icon-close::before` 规则，而该文件与 `config/menuIcons.ts` 被 `menuIcons.spec.ts` 的「CSS 规则集合 == 映射表值集合」断言绑死（补规则必须同时登记映射表，超出文字字形兜底的范围）→ 用文字字形；码点本身有：`config/layuiIconNames.ts` 的 `'close': '\u1006'`。② 下拉的 `▾`（对标 `.layui-icon-down`，码点 `\ue61a`，同上原因）。
 */
const props = defineProps<{ tabs: readonly PageTab[]; activeKey: string }>()

const emit = defineEmits<{
  refresh: []
  /** 点页签本身（不含 ×）。参数是 `PageTab.key`。 */
  select: [key: string]
  close: [key: string]
  closeOthers: [key: string]
  closeAll: []
}>()

/** ▾ 的展开态。纯 UI 状态，不进 composable（它不参与页签的增删切）。 */
const menuOpen = ref(false)
const moreRoot = ref<HTMLElement | null>(null)
const listRef = ref<HTMLElement | null>(null)

/** 菜单项点完就收起（对标 `admin.js` 的 `element.on('nav(layadmin-pagetabs-nav)')` 里那两行收起）。 */
function runMenu(action: () => void): void {
  menuOpen.value = false
  action()
}

function onDocumentPointerDown(event: MouseEvent): void {
  if (!menuOpen.value) return
  if (moreRoot.value?.contains(event.target as Node)) return
  menuOpen.value = false
}
onMounted(() => document.addEventListener('pointerdown', onDocumentPointerDown))
onBeforeUnmount(() => document.removeEventListener('pointerdown', onDocumentPointerDown))

/**
 * 激活页签滚进可视区。对标物是 `admin.js` 的 `events.rollPage('auto', index)`（切页签时调用）：对标 `.layui-tab-title` 被 JS 改 `left` 偏移，我们用原生滚动 + 这条 `scrollIntoView` 达到同一效果。
 * ⚠️ 判据写成 `typeof … === 'function'` 而不是直接调：jsdom（组件测试的环）**没有实现** `Element.prototype.scrollIntoView`，直接调会 `TypeError`——那会让组件测试全红，而这不是被测行为（本机测试不测视觉，见 `__tests__/AppTabsBar.spec.ts` 的文件头）。
 */
watch(
  () => props.activeKey,
  async () => {
    await nextTick()
    const active = listRef.value?.querySelector('.shell-tabs__item--active')
    if (active && typeof active.scrollIntoView === 'function') {
      active.scrollIntoView({ block: 'nearest', inline: 'nearest' })
    }
  },
)
</script>

<template>
  <div class="shell-tabs" @keydown.esc="menuOpen = false">
    <!-- 业务方新增（A8）：一键清空页签。**对标物没有这一颗**——它对标的三个批量关闭藏在右侧 ▾ 里（`home.html:902-904`），业务方原话「我没注意到 你放到最左边来 放大点」⇒ 明确要求一个一眼可见的入口。
         语义**复用**既有 `closeAll`（`usePageTabs.closeAllPageTabs()`：只留不可关的后台首页并停在它上面），本组件不新增任何清理规则。 -->
    <button
      class="shell-tabs__clear"
      type="button"
      title="清空所有标签页"
      @click="emit('closeAll')"
    >
      清空标签页
    </button>

    <button class="shell-tabs__refresh" type="button" aria-label="刷新当前页" @click="emit('refresh')">
      <AppIcon :name="UI_ICONS.refresh" />
    </button>

    <ul ref="listRef" class="shell-tabs__list">
      <li
        v-for="tab in props.tabs"
        :key="tab.key"
        class="shell-tabs__item"
        :class="{
          'shell-tabs__item--active': tab.key === props.activeKey,
          'shell-tabs__item--closable': tab.closable,
        }"
        @click="emit('select', tab.key)"
      >
        <span class="shell-tabs__label">{{ tab.title }}</span>
        <button
          v-if="tab.closable"
          class="shell-tabs__close"
          type="button"
          :aria-label="`关闭 ${tab.title}`"
          @click.stop="emit('close', tab.key)"
        >
          ×
        </button>
      </li>
    </ul>

    <div ref="moreRoot" class="shell-tabs__more">
      <button
        class="shell-tabs__more-toggle"
        type="button"
        aria-haspopup="menu"
        :aria-expanded="menuOpen"
        aria-label="页签操作"
        @click="menuOpen = !menuOpen"
      >
        ▾
      </button>
      <!-- 文案逐字取对标：`home.html:902-904` 的三个 `layadmin-event` + i18n 字典
           （`.tools/recon/i18n_zh.json` 的 `topNav.closeThisTabs` /
           `topNav.closeOtherTabs` / `topNav.closeAllTabs`）。注意是「其**它**」不是「其他」。 -->
      <ul v-if="menuOpen" class="shell-tabs__menu" role="menu">
        <li role="none">
          <button
            class="shell-tabs__menu-item"
            type="button"
            role="menuitem"
            @click="runMenu(() => emit('close', props.activeKey))"
          >
            关闭当前标签页
          </button>
        </li>
        <li role="none">
          <button
            class="shell-tabs__menu-item"
            type="button"
            role="menuitem"
            @click="runMenu(() => emit('closeOthers', props.activeKey))"
          >
            关闭其它标签页
          </button>
        </li>
        <li role="none">
          <button
            class="shell-tabs__menu-item"
            type="button"
            role="menuitem"
            @click="runMenu(() => emit('closeAll'))"
          >
            关闭全部标签页
          </button>
        </li>
      </ul>
    </div>
  </div>
</template>
