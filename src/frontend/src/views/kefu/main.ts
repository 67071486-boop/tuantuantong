import { createApp, h } from 'vue'
import { createPinia } from 'pinia'
import { isNavigationFailure, NavigationFailureType, RouterView } from 'vue-router'
import { f7App as F7App } from 'framework7-vue'
import './framework7'
import router from '@/router'
import { installUnauthorizedHandler } from '@/router/unauthorized'
import 'framework7/css/bundle'
import '@/v2/carbon.css'
import './kefu.css'

let leavingKefu = false
router.beforeEach((to) => {
  if (to.name === 'kefu-console') return
  leavingKefu = true
  window.location.replace(router.resolve(to.fullPath).href)
  return false
})

const root = document.getElementById('app')
if (!root) throw new Error('快捷录单挂载节点不存在')
root.classList.add('ttv2')
const primary = getComputedStyle(root).getPropertyValue('--tt2-primary').trim()
const app = createApp({
  render: () => h(F7App, { theme: 'md', colors: { primary } }, () => h(RouterView)),
})
app.use(createPinia())
app.use(router)
installUnauthorizedHandler(router)
void router.isReady().then(
  () => app.mount(root),
  (error: unknown) => {
    if (!leavingKefu || !isNavigationFailure(error, NavigationFailureType.aborted)) throw error
  },
)
