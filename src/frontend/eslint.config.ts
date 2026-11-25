import { globalIgnores } from 'eslint/config'
import { defineConfigWithVueTs, vueTsConfigs } from '@vue/eslint-config-typescript'
import pluginVue from 'eslint-plugin-vue'
import pluginVitest from '@vitest/eslint-plugin'

// To allow more languages other than `ts` in `.vue` files, uncomment the following lines:
// import { configureVueProject } from '@vue/eslint-config-typescript'
// configureVueProject({ scriptLangs: ['ts', 'tsx'] })
// More info at https://github.com/vuejs/eslint-config-typescript/#advanced-setup

export default defineConfigWithVueTs(
  {
    name: 'app/files-to-lint',
    files: ['**/*.{vue,ts,mts,tsx}'],
  },

  // `public/layui/**` 与 `public/vendor/**` 是 **vendored 的第三方发行版**：前者是官方 layui 2.13.9
  // （MIT，服务【系统人员】那 9 页的 iframe island），后者是对标物的 formselects-v4
  // （见 `src/layui/vendor.ts` 文件头注释）。压缩/转译产物不该、也不能按本仓库的规则 lint
  // ——2026-09-20 实测：不排除 `public/layui/**`，`pnpm lint` 直接报 1281 个 error；
  // 同日加入 formselects-v4.js 后不排除 `public/vendor/**` 会报 74 个 error。
  // `public/` 下除了这两处没有别的代码；按目录收窄，别顺手 ignore 整个 `public/`。
  globalIgnores([
    '**/dist/**',
    '**/dist-ssr/**',
    '**/coverage/**',
    'public/layui/**',
    'public/vendor/**',
  ]),

  ...pluginVue.configs['flat/essential'],
  vueTsConfigs.recommended,

  {
    ...pluginVitest.configs.recommended,
    files: ['src/**/__tests__/*'],
  },

  // 下划线前缀 = 「有意保留、暂未使用」的参数，这是本仓库的显式约定。
  // 用例：composables/useMenu.ts 的 visibleItems / visibleGroups 的 _hasRoute——
  // 保留它是为了让签名与 isClickable 一致、将来接服务端过滤时不必改调用方。
  // 注意：管这件事的是 ESLint（本工程 tsconfig 并未开 noUnusedParameters），
  // 而 @typescript-eslint/no-unused-vars 默认不认下划线，所以这里把约定显式告诉它。
  // 规则仍是 error，只豁免下划线开头的参数——不写 eslint-disable、不放宽整个规则。
  {
    name: 'app/no-unused-vars-underscore-convention',
    files: ['**/*.{vue,ts,mts,tsx}'],
    rules: {
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
    },
  },
)
