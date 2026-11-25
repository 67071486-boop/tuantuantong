import { createApp } from 'vue'
import { createPinia } from 'pinia'
import './framework7'
import App from './App.vue'
import mRouter from './router'
import { installMUnauthorizedHandler } from './router/unauthorized'
import 'framework7/css/bundle'
import 'framework7-icons/css/framework7-icons.css'
import './styles/framework7.css'

const app = createApp(App)
app.use(createPinia())
app.use(mRouter)

// 401 的「清会话 + 跳 m-login」唯一装配点（逻辑在 m/router/unauthorized.ts，那里可测；
// 与桌面装配点互不干扰——两个入口各自加载自己的 main.ts，永不同处一个文档）。
installMUnauthorizedHandler(mRouter)

// 阻塞式引导：首次导航（守卫里 `await session.load()`）完成后再挂载（与桌面同构）。
mRouter.isReady().then(() => app.mount('#app'))
