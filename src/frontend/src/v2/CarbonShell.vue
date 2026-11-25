<script setup lang="ts">
/** `/v2` 炭火外壳（视觉 = `docs/FRONTEND-V2-DRAFT.md` §4）：220px 深炭侧栏（品牌块 + 顶部竖排用户块 + 两级菜单）+ 顶栏 + 标签页条 + 内容区 `<RouterView/>`（侧栏宽 220 = D-0351 业务方线上两轮定档）。侧栏菜单 = **照抄旧侧边栏**：数据流与 `layouts/AppSidebar.vue` 同一条（`useV2Menu()` → `useMenu()` → `config/menu.ts`），名称 / 顺序 / 分组 / 权限显隐 / 可点判定不在本组件判第二遍（特例映射见 `v2Menu.ts`）；同时只展开一个组、默认全收起（当前路由落在组内时该组展开）；图标沿用 `config/menuIcons.ts` 同一张表，查不到映射就不渲染；`disabled` 条目置灰不可点（路径由测试钉住）；登录态沿用旧全站机制（`router/guards.ts` 原样覆盖，会话快照读 `stores/session.ts`、缺席 `—` 兜底）。
 * 响应式（断点口径见 carbon.css 文件头；判定全在 CSS，组件里没有 JS 媒体查询）：≤1100px 侧栏收 72px 图标栏（组子菜单右浮层）；≤720px 侧栏转顶部抽屉（顶栏 ☰ 翻 `drawerOpen`，根类 `.cv2-shell--drawer-open` 驱动 CSS，点遮罩或选完菜单自动收）。顶栏：左 = 路由 `meta.title`；右 = 「客服工具」外链（新标签打开，与账号同住右区容器——旧壳那颗有布局洞、本壳结构上不存在）+ 账号区 + 退出（先 `session.logout()` 终结本地、再发请求、最后 `replace` 登录页）。标签页条**复用旧壳同一颗 composable** `usePageTabs.ts`（只读复用）：开签去重键 = `route.fullPath`、关闭先右后左、批量关。首签 `/v2` 由本壳保护；页签在关闭或主动刷新前保留各自的页面状态。 */
import { computed, defineAsyncComponent, reactive, ref, shallowReactive, watch } from 'vue'
import { useRoute, useRouter, type RouteLocationNormalizedLoaded } from 'vue-router'
import { useSessionStore } from '@/stores/session'
import { avatarDataUri } from '@/config/avatar'
import { BOTTOM_ICONS, GROUP_ICONS, TOP_LEVEL_ICONS } from '@/config/menuIcons'
import { usePageTabs } from '@/composables/usePageTabs'
import { useV2Menu, V2_HOME, type V2MenuEntry } from './v2Menu'
import OrderTabRoute from './OrderTabRoute.vue'
import { archiveAccess } from './wecomArchive'

const route = useRoute()
const router = useRouter()
const session = useSessionStore()
const { topLevel, groups, bottom } = useV2Menu()
const internalChatAccount = defineAsyncComponent(() => import('./internalChat/InternalChatAccount.vue'))
const hasInternalChat = computed(() => session.user?.internal_chat_enabled === true
  || (import.meta.env.DEV && router.hasRoute('v2-internal-chat')))
const isInternalChat = computed(() => import.meta.env.DEV && route.name === 'v2-internal-chat')

/** 工作室名（品牌块小字；`/api/auth/me` 的 `tenant_name`，缺席按 `—`）。 */
const tenantName = computed(() => session.user?.tenant_name || '—')
/** 用户块显示名：display_name 优先，回落 username（两者都空按 `—`）。 */
const displayName = computed(() => session.user?.display_name || session.user?.username || '—')
/** 单值职位（后端 `_user_payload.role`，无角色时是空串 ⇒ 兜「成员」；顶栏账号区用）。 */
const roleName = computed(() => session.user?.role || '成员')
/** 侧栏用户块的 `[职位]` 后缀：旧壳 `AppAccountBlock` 逐字同款——有职位才加方括号，无职位不加（不兜「成员」）。 */
const roleSuffix = computed(() => {
  const role = session.user?.role ?? ''
  return role === '' ? '' : `[${role}]`
})
/** 用户块头像：`/api/auth/me` 的 `avatar_url`（同旧壳 `AppSidebar`）；没上传过 ⇒ 回落
 * `config/avatar.ts` 按名字生成的色板头像（同一生成器 ⇒ 与旧壳/资料页同人同色）。 */
const avatarSrc = computed(() => session.user?.avatar_url ?? avatarDataUri(displayName.value, 60))

/* ── 顶栏（D-0346）────────────────────────────────────────────────────── */

/** 顶栏页题 = 当前路由 `meta.title`；取不到回落路由名（旧壳 `resolveTitle` 同一条规则）。 */
const pageTitle = computed(() => {
  const t = route.meta.title
  return typeof t === 'string' && t !== '' ? t : String(route.name ?? '')
})

/** 客服工具的落点：`public/kefu-tools/` 静态下载页（不进 SPA 路由表）⇒ 只能新标签 `<a>`。 */
const KEFU_TOOLS_URL = '/kefu-tools/'

/** 退出 = 旧顶栏同一条序列：先终结本地（`logout()` 内部先 clear 再发请求），再回登录页。 */
async function logout(): Promise<void> {
  await session.logout()
  await router.replace({ name: 'login' })
}

/* ── 标签页条（D-0346）：composable 与旧壳同一颗（只读复用），炭火只重画签面 ────────── */

const { tabs, activeKey, openPageTab, closePageTab, closeAllPageTabs } = usePageTabs()
const tabOwner = computed(() => session.user && `${session.identityPlane}:${session.user.id}`)
const wecomArchiveAvailable = ref(false)
watch(tabOwner, (owner, _previous, onCleanup) => {
  wecomArchiveAvailable.value = false
  if (!owner || !router.hasRoute('v2-wecom-archive')) return
  let current = true
  onCleanup(() => { current = false })
  void archiveAccess().then(({ enabled }) => {
    if (current) wecomArchiveAvailable.value = enabled
  }).catch(() => {})
}, { immediate: true })
const tabRoutes = shallowReactive(new Map<string, RouteLocationNormalizedLoaded>())
const tabReloads = reactive(new Map<string, number>())
const tabAccess = computed(() => JSON.stringify([
  session.user?.username, session.isTenantAdmin, session.isAccountAdmin,
  [...session.menuPermissions].sort(), [...session.permissions].sort(),
  [...session.gameIds].sort((a, b) => a - b),
]))

function rememberCurrentTab(): void {
  const current = router.currentRoute.value
  if (tabOwner.value && current.matched.some((record) => record.name === 'v2')) {
    tabRoutes.set(current.fullPath, current)
  }
}

// 权限范围或账号改变后重新取数，避免显示上一份会话的数据。
watch([tabOwner, tabAccess], () => {
  tabRoutes.clear()
  tabReloads.clear()
  rememberCurrentTab()
})

const tabPages = computed(() => tabs.value.flatMap(({ key }) => {
  const tabRoute = tabRoutes.get(key)
  if (!tabOwner.value || !tabRoute || !session.canNode(tabRoute.meta.permissions ?? [])) return []
  if (tabRoute.meta.requiresAccountAdmin === true && !session.isAccountAdmin) return []
  return [{ key, route: tabRoute, cacheKey: `${tabOwner.value}:${key}:${tabAccess.value}:${tabReloads.get(key) ?? 0}` }]
}))

const tabScroll = new Map<string, number>()
watch(
  () => route.fullPath,
  (key, previous) => {
    if (previous && tabRoutes.has(previous)) tabScroll.set(previous, window.scrollY)
    const title = pageTitle.value
    if (title !== '') openPageTab({ key, title })
    rememberCurrentTab()
  },
  { immediate: true, flush: 'pre' },
)
watch(() => route.fullPath, (key) => {
  const top = tabScroll.get(key) ?? 0
  if (window.scrollY !== top) window.scrollTo(0, top)
}, { flush: 'post' })

/** v2 首页签的键与标题（`/v2`；composable 的「首签不可关」只认 `/`，见文件头偏离注记）。 */
const homeTab = router.resolve({ name: V2_HOME })
const homeTabPath = homeTab.path
const homeTabTitle =
  typeof homeTab.meta.title === 'string' && homeTab.meta.title !== ''
    ? homeTab.meta.title
    : V2_HOME

/** 页签动作 → 导航。键为空串 = 「可关页签已清空」⇒ 回 **v2 首页**（`/v2`；旧壳这一步回 `/`，
 * 因为它的首页是 `/`）。目标与当前 `fullPath` 相同时不重复 push（对标物点自己不做任何事）。 */
function goToTab(keyOrEmpty: string): void {
  const key = keyOrEmpty === '' ? homeTabPath : keyOrEmpty
  if (key === route.fullPath) return
  void router.push(key)
}

/** 点页签本身 = 切换。 */
function selectTab(key: string): void {
  goToTab(key)
}

/** 关闭页签（先右后左的邻居规则在 composable 里）。**首签不可关**：关闭按钮已在签面摘除，
 *  这里再挡一道（结构钉——谁也不能绕过签面把首签删掉）。 */
function closeTab(key: string): void {
  if (key === homeTabPath) return
  tabRoutes.delete(key)
  tabReloads.delete(key)
  tabScroll.delete(key)
  goToTab(closePageTab(key))
}

/** 清空全部可关页签并停在首页签（旧壳「清空标签页」同一条语义）。
 *  ⚠️ composable 数据层会把 `/v2` 签也当可关删掉 ⇒ 清空后**无条件重建首签**，
 *  终态 = 只剩首页签、激活在它上面（在首页签上清空也不会落到零页签）。 */
/** 刷新只重挂当前页签，保留其它页签。 */
function reloadCurrentPage(): void {
  const key = route.fullPath
  tabReloads.set(key, (tabReloads.get(key) ?? 0) + 1)
  tabScroll.delete(key)
  if (window.scrollY !== 0) window.scrollTo(0, 0)
}

/** 清空可关页签，保留首页。 */
function clearTabs(): void {
  for (const key of tabRoutes.keys()) {
    if (key === homeTabPath) continue
    tabRoutes.delete(key)
    tabReloads.delete(key)
    tabScroll.delete(key)
  }
  goToTab(closeAllPageTabs())
  openPageTab({ key: homeTabPath, title: homeTabTitle })
}

const pendingTarget = ref<string | null>(null)
let navigationSerial = 0

function isActive(item: V2MenuEntry): boolean {
  return (pendingTarget.value ?? route.name) === item.target
}

/* ── 分组折叠：本地 ref，不持久化；同时只展开一个（同旧壳 el-menu 的 unique-opened，
 *    对标 layui-nav-tree 的 lay-shrink="all"）；当前路由落在某组内时该组默认展开
 *    （同旧壳 default-active 的行为；今天 /v2 内只有 v2-home 这条直挂项，
 *    该例外是给将来 v2 子页留的正确缺省）。 ───────────────────────────── */
const openGroup = ref<string | null>(
  groups.value.find((g) => g.items.some((i) => isActive(i)))?.name ?? null,
)

function isExpanded(name: string): boolean {
  return openGroup.value === name
}

/** 展开 B 即收起 A（unique-opened）；再点已展开的组头 = 收起。
 *  开合带 200ms 滑动过渡（CSS 类 `cv2-nav-group--open` 挂在组 li 上驱动动画，见 carbon.css）。 */
function toggleGroup(name: string): void {
  openGroup.value = openGroup.value === name ? null : name
}

/* ── 响应式外壳（断点口径见 carbon.css 文件头注释）──────────
 * ≤720px 侧栏转顶部抽屉：开合只是一个 ref + 根元素类（`.cv2-shell--drawer-open`），
 * 断点判定完全交给 CSS media query（宽档下类挂着也不渲染抽屉）——不写 JS 侦测，
 * jsdom/宽屏行为与生产逐字一致。选完菜单自动收（`go` 里统一收口）。 */
const drawerOpen = ref(false)

/* ── 导航出口 ─────────────────────────────────────────────────────────── */

/** 真实 href（中键 / 复制链接语义走浏览器默认；点击被下面的 handler 接管）。
 * 置灰条目的 target **不在路由表里**（isClickable=false 的定义就是 hasRoute 为假），resolve 会响亮抛错 ⇒ 兜 '#'：href 本就不承载行为（点击在 go() 拦截；旧壳的 el-menu-item 同样没有 href），这里只为中键/复制链接的真实条目服务。 */
function hrefOf(item: V2MenuEntry): string {
  return router.hasRoute(item.target) ? router.resolve({ name: item.target }).path : '#'
}

/** 点击 = `router.push`；置灰条目在此拦截（同旧侧边栏 `go` 的守卫）。
 *  抽屉档（≤720px）选完自动收：出口统一收口在这条（宽档下类无视觉效果，收了也无害）。 */
function go(item: V2MenuEntry, event: MouseEvent): void {
  event.preventDefault()
  if (item.disabled) return
  drawerOpen.value = false
  const serial = ++navigationSerial
  pendingTarget.value = item.target
  void router.push({ name: item.target }).finally(() => {
    if (serial === navigationSerial) pendingTarget.value = null
  }).catch(() => {})
}
</script>

<template>
  <div class="ttv2 cv2-shell" :class="{ 'cv2-shell--drawer-open': drawerOpen }">
    <!-- 抽屉遮罩（≤720px 且抽屉开着才显示；点它收抽屉；宽档 CSS 直接 display:none）。 -->
    <div class="cv2-scrim" data-v2="nav-scrim" @click="drawerOpen = false"></div>
    <aside id="v2-side-nav" class="cv2-side">
      <div class="cv2-brand">
        <span class="cv2-brand-mark" aria-hidden="true"></span>
        <div>
          <p class="cv2-brand-name">团团通</p>
          <p class="cv2-brand-tenant">{{ tenantName }}</p>
        </div>
      </div>
      <!-- 顶部用户块 = 旧壳 AppAccountBlock 同款竖排形态（业务方拍板：品牌块下方、菜单上方）：
           居中大圆头像（真 avatar_url，缺省回落生成头像）+ 下方居中「名称[职位]」。 -->
      <component
        :is="internalChatAccount"
          :production="session.user?.internal_chat_enabled === true"
        v-if="hasInternalChat"
        :display-name="displayName"
        :role-name="roleName"
        :avatar-src="avatarSrc"
        @navigate="drawerOpen = false"
      />
      <div v-else class="cv2-side-user">
        <img class="cv2-avatar" :src="avatarSrc" alt="" />
        <p class="cv2-user-line"><b>{{ displayName }}</b>{{ roleSuffix }}</p>
      </div>
      <nav class="cv2-nav" aria-label="主导航">
        <ul class="cv2-nav-list">
          <!-- 顶部直挂条目（后台首页 / 指派给我的订单 / 内部抢单大厅）——与旧侧边栏同源同序 -->
          <li v-for="item in topLevel" :key="item.label" class="cv2-nav-item">
            <a
              class="cv2-nav-link"
              :class="{ 'cv2-nav-link--active': isActive(item), 'cv2-nav-link--disabled': item.disabled }"
              :href="hrefOf(item)"
              :title="item.label"
              :aria-current="isActive(item) ? 'page' : undefined"
              :aria-disabled="item.disabled || undefined"
              :data-v2-menu="item.label"
              @click="go(item, $event)"
            >
              <i
                v-if="TOP_LEVEL_ICONS[item.label]"
                class="cv2-nav-icon layui-icon"
                :class="`layui-icon-${TOP_LEVEL_ICONS[item.label]}`"
                aria-hidden="true"
              ></i>
              <span>{{ item.label }}</span>
            </a>
          </li>

          <!-- 分组：组头可点折叠/展开（开合类挂组 li，驱动 200ms 滑动动画）；
               子条目由 useMenu 按权限过滤（与旧侧边栏同源）。
               ⭐ ≤1100px 图标栏档：只显图标，title = 组名（原生 tooltip 口径）。 -->
          <li
            v-for="g in groups"
            :key="g.name"
            class="cv2-nav-group"
            :class="{ 'cv2-nav-group--open': isExpanded(g.name) }"
            :data-v2-group="g.name"
          >
            <button
              type="button"
              class="cv2-nav-head"
              :aria-expanded="isExpanded(g.name)"
              :title="g.name"
              :data-v2-group-head="g.name"
              @click="toggleGroup(g.name)"
            >
              <i
                v-if="GROUP_ICONS[g.name]"
                class="cv2-nav-icon layui-icon"
                :class="`layui-icon-${GROUP_ICONS[g.name]}`"
                aria-hidden="true"
              ></i>
              <span>{{ g.name }}</span>
              <svg class="cv2-nav-arrow" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <path d="M9.5 6.5 15 12l-5.5 5.5" />
              </svg>
            </button>
            <!-- 滑动动画层（grid 0fr↔1fr）：子列表常驻 DOM，收起 = 行高 0 + visibility:hidden -->
            <div class="cv2-sub-wrap">
              <ul class="cv2-sub-list" :data-v2-sublist="g.name">
                <li v-for="item in g.items" :key="item.label">
                  <a
                    class="cv2-sub-link"
                    :class="{ 'cv2-sub-link--active': isActive(item), 'cv2-sub-link--disabled': item.disabled }"
                    :href="hrefOf(item)"
                    :aria-current="isActive(item) ? 'page' : undefined"
                    :aria-disabled="item.disabled || undefined"
                    :data-v2-menu="item.label"
                    @click="go(item, $event)"
                  >
                    <span>{{ item.label }}</span>
                  </a>
                </li>
              </ul>
            </div>
          </li>

          <!-- 底部直挂条目（问题反馈）——与旧侧边栏同款：分组之后、用户块之前 -->
          <li v-for="item in bottom" :key="item.label" class="cv2-nav-item">
            <a
              class="cv2-nav-link"
              :class="{ 'cv2-nav-link--active': isActive(item), 'cv2-nav-link--disabled': item.disabled }"
              :href="hrefOf(item)"
              :title="item.label"
              :aria-current="isActive(item) ? 'page' : undefined"
              :aria-disabled="item.disabled || undefined"
              :data-v2-menu="item.label"
              @click="go(item, $event)"
            >
              <i
                v-if="BOTTOM_ICONS[item.label]"
                class="cv2-nav-icon layui-icon"
                :class="`layui-icon-${BOTTOM_ICONS[item.label]}`"
                aria-hidden="true"
              ></i>
              <span>{{ item.label }}</span>
            </a>
          </li>
        </ul>
      </nav>
    </aside>
    <header class="cv2-topbar">
      <!-- ☰：≤720px 抽屉档专属开关（宽档 CSS display:none）。排在页题之前 = 抽屉开在左侧的
           常规位；kefu 链接仍住右区容器（D-0346 的结构保证不变）。 -->
      <button
        type="button"
        class="cv2-burger"
        data-v2="nav-burger"
        :aria-expanded="drawerOpen ? 'true' : 'false'"
        aria-controls="v2-side-nav"
        aria-label="开关导航菜单"
        @click="drawerOpen = !drawerOpen"
      >
        <span class="cv2-burger-bar" aria-hidden="true"></span>
        <span class="cv2-burger-bar" aria-hidden="true"></span>
        <span class="cv2-burger-bar" aria-hidden="true"></span>
      </button>
      <strong class="cv2-topbar-title">{{ pageTitle }}</strong>
      <div class="cv2-topbar-right">
        <!-- 客服工具：/kefu-tools/ 是 public/ 静态下载页（不进 v2 路由）⇒ 新标签打开。
             位置钉在右区容器（D-0346：旧壳那颗落错位到顶栏最左——本壳从结构上保证它在右上角）。 -->
        <a
          class="cv2-kefu-link"
          data-v2="kefu-link"
          :href="KEFU_TOOLS_URL"
          target="_blank"
          rel="noopener"
        >客服工具</a>
        <div v-if="hasInternalChat" id="cv2-topbar-chat" style="display: contents"></div>
        <RouterLink v-if="wecomArchiveAvailable" class="cv2-kefu-link" :to="{ name: 'v2-wecom-archive' }">
          查找聊天记录
        </RouterLink>
        <span class="cv2-topbar-user">{{ roleName }} · <b>{{ displayName }}</b></span>
        <button type="button" class="cv2-logout" data-v2="logout" @click="logout">退出</button>
      </div>
    </header>
    <!-- 标签页条：状态与规则 = 旧壳同一颗 usePageTabs（只读复用），这里只画签面。 -->
    <nav class="cv2-tabs" aria-label="页签">
      <button
        type="button"
        class="cv2-tabs-clear"
        data-v2="tabs-clear"
        title="清空所有标签页"
        @click="clearTabs"
      >
        清空标签页
      </button>
      <!-- ⭐ 业务方「正常应该是图 2 那样」：刷新钮紧挨清空钮右侧（对标形态）。 -->
      <button
        type="button"
        class="cv2-tabs-refresh"
        data-v2="tabs-refresh"
        title="刷新当前页"
        aria-label="刷新当前页"
        @click="reloadCurrentPage"
      >
        <i class="layui-icon layui-icon-refresh-1" aria-hidden="true"></i>
      </button>
      <ul class="cv2-tabs-list">
        <li
          v-for="tab in tabs"
          :key="tab.key"
          class="cv2-tab"
          :class="{ 'cv2-tab--active': tab.key === activeKey }"
          :data-v2-tab="tab.key"
          @click="selectTab(tab.key)"
        >
          <span class="cv2-tab-label">{{ tab.title }}</span>
          <!-- 首签（/v2）不画关闭按钮（旧壳首签不可关的同款 UX；数据层的兜底见 closeTab） -->
          <button
            v-if="tab.closable && tab.key !== homeTabPath"
            type="button"
            class="cv2-tab-close"
            :aria-label="`关闭 ${tab.title}`"
            @click.stop="closeTab(tab.key)"
          >
            ×
          </button>
        </li>
      </ul>
    </nav>
    <main class="cv2-main" :class="{ 'cv2-main--chat': isInternalChat }">
      <div
        v-for="tab in tabPages"
        v-show="route.fullPath === tab.key"
        :key="tab.cacheKey"
        :inert.attr="route.fullPath !== tab.key || undefined"
      >
        <OrderTabRoute :route="tab.route" />
      </div>
    </main>
  </div>
</template>
