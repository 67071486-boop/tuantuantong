<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useSessionStore } from '@/stores/session'
import { formatAccountLabel } from '@/config/account'

// 工作室名和账号区在侧栏（AppSidebar），本组件只剩顶栏。宽屏时顶栏左区右移一个侧栏宽（侧栏 fixed）；≤991px 抽屉态偏移归零。
// 有登录态时右区渲染真实身份（display_name || username，拼上职位）。下拉固定三项：我的资料 / 修改密码 / 退出系统；无会话时右区留空、不回落假名字。
// 我的资料与修改密码进同一页，后者带 ?tab=password。登出先清本地再发请求。identity_plane 只许展示，禁止拿它做菜单 / 路由 / 权限判断。
defineProps<{ title: string }>()
const emit = defineEmits<{ toggle: [] }>()

const session = useSessionStore()
const router = useRouter()

// 顶栏是「昵称(职位)」：**半角圆括号**（对标顶栏那一处；侧栏那一处是方括号，见 AppAccountBlock）。
const who = computed(() =>
  formatAccountLabel(
    session.user?.display_name || session.user?.username || '',
    session.user?.role ?? '',
  ),
)

function goProfile(): void {
  void router.push({ name: 'accounts-profile' })
}
function goPassword(): void {
  void router.push({ name: 'accounts-profile', query: { tab: 'password' } })
}
async function onLogout() {
  await session.logout() // 先终结本地，再发请求；失败不回滚
  await router.replace({ name: 'login' })
}
</script>

<template>
  <header class="shell-topbar">
    <!-- ☰ 无可见文字，必须给无障碍名（读屏只念「按钮」等于没信息） -->
    <el-button text class="shell-topbar__toggle" aria-label="展开或收起菜单" @click="emit('toggle')">☰</el-button>
    <strong class="shell-topbar__title">{{ title }}</strong>
    <!-- ⚠️ `.shell-topbar__account` 必须留在**这一个 flex 子元素**上：右侧对齐是它自己的
         `margin-left: auto`（styles/shell.css）。把类挪到 `el-dropdown` 的根节点上会随 EP 版本改层级，
         包一层 span 是两种写法都稳的那个。 -->
    <span class="shell-topbar__account">
      <!-- 客服工具下载入口（D-0345；⭐ D-0346 挪进右区容器——线上错位修复）：**所有登录用户**可见（业务方拍板），未登录不渲染。目标 `/kefu-tools/` 是 public/ 静态下载页（随网页端发版，不进 SPA 路由表）⇒ 必须新标签 `<a>`：站内 router.push 没有这条路由，而把整个壳（含页签状态）跳丢更不可取。
           ⚠️ **为什么必须住在右区容器里**：顶栏的 toggle/title 用 `position: relative; left: var(--shell-sidebar-w)` 只是**画位偏移**，它们的 flex 布局盒仍留在最左 ⇒ 排在后面的兄弟节点会落进这个布局洞、被 logo 盖到只剩末字。塞进 `margin-left:auto` 的账号区容器，链接跟着整块顶到右上角，宽屏窄屏都不吃那个洞（位置由 `AppTopbar.spec.ts` 的位置钉看着）。 -->
      <a
        v-if="session.user"
        class="shell-topbar__tool-link"
        data-test="kefu-tool-entry"
        href="/kefu-tools/"
        target="_blank"
        rel="noopener"
      >客服工具</a>
      <el-dropdown v-if="who">
        <span class="topbar-user">{{ who }}</span>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item data-test="my-profile" @click="goProfile">我的资料</el-dropdown-item>
            <el-dropdown-item data-test="change-password" @click="goPassword">修改密码</el-dropdown-item>
            <el-dropdown-item divided data-test="logout" @click="onLogout">退出系统</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </span>
  </header>
</template>

<style scoped>
.topbar-user {
  cursor: pointer;
  margin-right: 12px;
}
/* 客服工具入口（D-0345）：顶栏白字（.shell-topbar 的 color 已是 --tt-shell-topbar-text，
 * inherit 即可——不发明新色值，shell.css 的字面量纪律对这里同样生效）。 */
.shell-topbar__tool-link {
  margin-right: 16px;
  color: inherit;
  text-decoration: none;
}
.shell-topbar__tool-link:hover {
  text-decoration: underline;
}
</style>
