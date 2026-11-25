/// <reference types="vite/client" />

/**
 * Vite 自带的 `ImportMetaEnv` 默认带一条索引签名（`[key: string]: any`），
 * 所以**未声明**的 `import.meta.env.XXX` 会被推断成 `any`。
 *
 * 下面这个标记接口把那条索引签名**关掉**：依据 `vite/types/importMeta.d.ts:11-12`——
 * `type ImportMetaEnvFallbackKey = 'strictImportMetaEnv' extends keyof ViteTypeOptions ? never : string`。
 * 一旦 `strictImportMetaEnv` 成为 `ViteTypeOptions` 的键，回退键类型就从 `string` 变成 `never`，
 * `ImportMetaEnv` 不再有索引签名。于是**变量名打错会在使用处报错**，
 * 把值当别的类型用也会报错。（Vite 自己注入的 `BASE_URL` / `MODE` / `DEV` / `PROD` / `SSR`
 * 在上游声明里，不受影响；但**任何新增的 `VITE_` 变量都必须在此声明**。）
 */
interface ViteTypeOptions {
  strictImportMetaEnv: unknown
}

interface ImportMetaEnv {
  /** API 前缀。开发期留空表示同源，交由 Vite 代理转发到 Django（见 vite.config.ts） */
  readonly VITE_API_BASE?: string
  /** 面向浏览器的独立聊天 HTTPS 来源，租户仍由同域签票决定。 */
  readonly VITE_CHAT_DIRECT_ORIGIN?: string
  readonly VITE_ORDER_FINANCE_DIRECT_ORIGIN?: string
  /** 本地开发期间启用内部通讯折叠方式预览。 */
  readonly VITE_INTERNAL_CHAT_PREVIEW?: string
  /** 使用本地真实会话接口。 */
  readonly VITE_INTERNAL_CHAT_LIVE?: string
  readonly VITE_INTERNAL_CHAT_ORDER_GROUPS?: string
  readonly VITE_INTERNAL_CHAT_WEBK_ORIGIN?: string
  /**
   * ⚠️ **只有 `vite.config.ts`（Node 侧）经 `loadEnv(mode, cwd, '')` 读它**，
   * 前端代码不读、也**不得**读：它会被打进客户端产物，等于把某个租户的域名烘进 bundle，
   * 与「租户由认证态决定」冲突（`AGENTS.md` §4）。声明在这里只是让 `.env.local` 的键集合可见。
   */
  readonly VITE_TENANT_HOST?: string
  readonly VITE_INTERNAL_CHAT_PREVIEW?: string
  readonly VITE_INTERNAL_CHAT_LIVE?: string
  readonly VITE_INTERNAL_CHAT_WEBK_ORIGIN?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
