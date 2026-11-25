import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import { installUnauthorizedHandler } from '@/router/unauthorized'
// 手机 UA 访问桌面端 ⇒ 引到 /m/（2026-10-12；为什么只能放前端入口见模块 docstring）。
import { readMobileRedirectSuppressed, redirectToMobileIfMobile } from './mobileRedirect'
import './styles/theme.css'
import './styles/layui-icons.css'
import './styles/shell.css'
// 前端 v2 试验线（D-0344）的炭火样式：全部选择器挂在 `.ttv2` / `cv2-*` 下，
// 对旧前端是惰性的（作用域由 `src/v2/__tests__/carbonTokens.spec.ts` 钉住）。
import './v2/carbon.css'
// 游戏维护三页的对标密度（决策 D-0023 / spec §3.5）。单独一份，别并进 order-table.css：
// 那是订单列表页的密度值，与这三页的 40px 行高不同源。

// ⛔ 必须在**任何渲染/取数之前**：手机用户不该看到桌面壳闪一下再被换走。
// ⚠️ 线 A（D-A3）：先读「抑制标记」再判——非打手刚被 `/m/` 的守卫请回桌面根
// （`m/router/guards.ts` 写了它），此时**不再**跳回 `/m/`，否则就是死循环。
// 标记住在 sessionStorage ⇒ 同一标签页内压住来回，换标签页/重开浏览器重新判定。
redirectToMobileIfMobile(
  navigator.userAgent,
  window.location.pathname,
  readMobileRedirectSuppressed(),
)

const app = createApp(App)
router.beforeEach((to, from) => {
  if (to.name !== 'kefu-console') return
  const href = router.resolve(to.fullPath).href
  if (from.name === 'login') window.location.replace(href)
  else window.location.assign(href)
  return false
})
app.use(createPinia())
app.use(router)

// 401 的"清会话 + 跳登录"唯一装配点（逻辑在 router/unauthorized.ts，那里可测）
installUnauthorizedHandler(router)

// 阻塞式引导：首次导航（守卫里 `await session.load()`）完成后再挂载，未认证不会渲染业务壳。
router.isReady().then(() => app.mount('#app'))
