<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppAccountBlock from '@/components/AppAccountBlock.vue'
import AppIcon from '@/components/AppIcon.vue'
import { GROUP_ICONS, BOTTOM_ICONS, TOP_LEVEL_ICONS } from '@/config/menuIcons'
import { useMenu } from '@/composables/useMenu'
import { useSessionStore } from '@/stores/session'

// 收起态由 AppShell 统一判定（窗口宽度只有一个 owner、一个 resize 监听），本组件只收结果，
// 不再自己读 window.innerWidth——两处状态会漂移。
const props = defineProps<{ open: boolean; collapsed: boolean }>()
const router = useRouter()
const route = useRoute()
const session = useSessionStore()
const { groups, topLevel, bottom } = useMenu()

// 工作室名走**会话真数据**（`/api/auth/me` 的 `tenant_name`）：占位常量已于 2026-09-24
// （我的资料线）删除。会话未就绪 = 空串，**不回落假名**（spec §7.1）；代价是加载完成前 logo 短暂为空，
// 比「闪一个示例工作室」好。昵称/职位同理（账号区在下面直接从 `session.user` 取）。
const studioName = computed(() => session.user?.tenant_name ?? '')

// 头像走**会话真数据**（`/api/auth/me` 的 `avatar_url`，顶栏头像同步线 2026-09-25）：
// 模板里原先写死 `:avatar-url="null"` ⇒ 业务方传了头像、右上角仍是空。没有上传过时是 `null`，
// 账号区自己回落到生成的色板头像（`AppAccountBlock` 的既有分支）。
const avatarUrl = computed(() => session.user?.avatar_url ?? null)

/**
 * 选中态。`el-menu` 的 `:index` 用的是条目 **label**（EP 把 `index` 声明为必填 String，见 node_modules 的 `menu-item.mjs`），所以这里要把当前路由 name 映射回 label；映射不上（如 404 页）就返回空串、不选中。
 * `default-active` 是响应式的（`menu.mjs` 有 `watch(() => props.defaultActive, …)`），路由变化会跟着更新。
 */
const activeIndex = computed(() => {
  const items = [...topLevel.value, ...groups.value.flatMap((g) => g.items), ...bottom.value]
  return items.find((i) => i.routeName !== undefined && i.routeName === route.name)?.label ?? ''
})

/** 置灰条目不可点，也不进任何快捷入口（FRONTEND-LAYOUT §8 硬要求）。 */
function go(item: { routeName?: string; disabled: boolean }) {
  if (item.disabled || !item.routeName) return
  void router.push({ name: item.routeName })
}
</script>

<template>
  <aside class="shell-sidebar" :class="{ 'shell-sidebar--open': props.open }">
    <!-- logo 区：对标物的 `.layui-logo` 在 `.layui-side > .layui-side-scroll` 里（`home.html:224`），账号区紧随其后（`:225` 是工作室名的 `<span>`、`:227` 是账号区）。样式在 styles/shell.css 的 `.shell-logo`；这里挪的是它的 DOM 归属（从顶栏到侧栏）。
         ⚠️ 槽位里放**工作室名**：该串在抓取页里出现 9 次 / 6 行，但**侧栏壳标记里只有一处**——`.layui-logo` 的 `<span>`（`:225`），`:227` 的 `.photo` 块里没有它（`:2009` 的 `$('.layui-logo span').text()` 是**内部沟通弹层**的标题，不是页面标题）⇒ 下面的账号区**不再**渲染工作室名（只渲染头像与账号名[职位]）。
         文字仍必须包在 <span> 里：收起态靠 `.shell--collapsed .shell-logo > span{display:none}` 藏它，60px 图标栏上不会溢出文字（见 styles/shell.css 那两条收起态规则）。 -->
    <div class="shell-logo"><span>{{ studioName }}</span></div>
    <AppAccountBlock
      v-if="session.user"
      :user-name="session.user.display_name || session.user.username"
      :role="session.user.role ?? ''"
      :avatar-url="avatarUrl"
    />
    <!-- unique-opened：同时只展开一个分组。对标物在 `<ul class="layui-nav layui-nav-tree" lay-shrink="all">` 上做的就是这个（`.tools/recon/home.html:236`），EP 的对应 prop 即 unique-opened（读码 `element-plus/es/components/menu/src/menu.mjs`：声明 `uniqueOpened: Boolean`、用它只保留当前这一条展开链），不自造实现。 -->
    <el-menu :collapse="props.collapsed" :default-active="activeIndex" unique-opened>
      <el-menu-item
        v-for="item in topLevel"
        :key="item.label"
        :index="item.label"
        :disabled="item.disabled"
        @click="go(item)"
      >
        <!-- Task 3 的图标：在 <span> 之前；查不到映射时 AppIcon 自己渲染空（不做断言）。 -->
        <AppIcon :name="TOP_LEVEL_ICONS[item.label]" />
        <span>{{ item.label }}</span>
        <el-tag v-if="item.disabled" size="small" type="info" style="margin-left: 6px">即将开放</el-tag>
      </el-menu-item>

      <el-sub-menu v-for="g in groups" :key="g.name" :index="g.name">
        <template #title>
          <!-- 同上：在 <span> 之前。 -->
          <AppIcon :name="GROUP_ICONS[g.name]" />
          <span>{{ g.name }}</span>
        </template>
        <el-menu-item
          v-for="item in g.items"
          :key="item.label"
          :index="item.label"
          :disabled="item.disabled"
          @click="go(item)"
        >
          <span>{{ item.label }}</span>
          <el-tag v-if="item.disabled" size="small" type="info" style="margin-left: 6px">即将开放</el-tag>
        </el-menu-item>
      </el-sub-menu>

      <!-- 侧栏**最底部**的直挂条目（`config/menu.ts` 的 `BOTTOM_ITEMS`；与 `topLevel` 同款：图标 + 文字 + 置灰 tag）。业务方指令「直接排到最下面的菜单」⇒ 位置就在 `el-sub-menu` 之后、`</el-menu>` 之前，⛔ 不新开分组。
           可见性与 `topLevel` **同一个 `keepItem` 收口**（见 `useMenu.ts` 的注释）。 -->
      <el-menu-item
        v-for="item in bottom"
        :key="item.label"
        :index="item.label"
        :disabled="item.disabled"
        @click="go(item)"
      >
        <AppIcon :name="BOTTOM_ICONS[item.label]" />
        <span>{{ item.label }}</span>
        <el-tag v-if="item.disabled" size="small" type="info" style="margin-left: 6px">即将开放</el-tag>
      </el-menu-item>
    </el-menu>
  </aside>
</template>
