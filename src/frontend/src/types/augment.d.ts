export {} // 必须：让本文件成为模块，下面的 declare module 才是"增强"而不是"替换整个模块的类型"

declare module 'axios' {
  export interface AxiosRequestConfig {
    /**
     * 不触发 401 处理器（见 spec §5.4）。**默认 false = fail-closed**。
     * 传 true 的调用点只有三个：登录、引导 `me`、CSRF 重取（都由 `api/auth.ts` 发起）。
     */
    skipUnauthorizedHandler?: boolean
  }
}

declare module 'vue-router' {
  export interface RouteMeta {
    /** 默认 true（fail-closed）：漏声明 = 要登录；`/login` 显式 false。 */
    requiresAuth?: boolean
    /**
     * 准入判据：**RBAC 节点 code** 数组（D-0020 结案；取值表见 `config/routePermissions.ts`）。
     * `[]` ≡ 未声明（白名单路由与 `requiresAccountAdmin` 单独承担准入的那些）；
     * 形态 `"<module>"`（栏目级）或 `"<module>.<action>"`（功能点）。
     * 判据由 `guards.ts` 交 `session.canNode()` 执行——管理员（tenant/account）短路为真。
     */
    permissions?: string[]
    /** `shell`（默认，带顶栏侧栏）/ `blank`（登录页）。 */
    layout?: 'shell' | 'blank'
    /**
     * 该路由只对**账号管理员**开放（`is_superuser` 或持「系统管理员」Group）。
     * 与 `permissions`（节点 code 交集）是两条独立判据，由 `guards.ts` 各自执行；
     * 判据值来自 `/api/auth/me` 的 `is_account_admin`。
     */
    requiresAccountAdmin?: boolean
    /**
     * **layui island 页的注册表 key**（`src/layui-island/page.ts` 的 `REGISTRY`）。
     * 声明了它的路由用 `components/LayuiIslandHost.vue` 渲染（1:1 复刻页，D-0025）。
     */
    islandPage?: string
  }
}
