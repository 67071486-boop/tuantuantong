import { fileURLToPath } from 'node:url'
import { mergeConfig, defineConfig, configDefaults } from 'vitest/config'
import { createViteConfig } from './vite.config.ts'

// vite.config.ts 的默认导出是**配置函数**（它要用 `mode` 调 loadEnv），
// 而 mergeConfig 只接受配置对象，所以这里调工厂、传 vitest 自己的 mode。
export default mergeConfig(
  createViteConfig('test'),
  defineConfig({
    test: {
      // 显式声明：本仓库的测试默认是纯逻辑测试，不依赖 DOM。
      // 不依赖 vitest 的默认值——默认值变了我们不会收到任何信号。
      // 例外是挂载组件的测试（`src/components/__tests__/AppIcon.spec.ts`、
      // `src/views/orders/__tests__/OrderNewView.spec.ts` 等）：它们在文件头用
      // `// @vitest-environment jsdom` 单独覆盖，**不改**这里的全局默认。
      environment: 'node',
      server: {
        deps: {
          // 挂载用到 EP 组件的 SFC 会牵出 `element-plus/es/components/*/style/css`（一个 `.css` 入口）。
          // vitest 默认把 node_modules 里的依赖外部化、交给 Node 的 ESM 加载器，而它不认 `.css`
          // （实测报 `TypeError: Unknown file extension ".css"`；`css: false` 只对经 Vite 管道处理的
          // 模块生效）。把 element-plus 内联到 Vite 管道后，样式导入按默认行为被当空模块处理——
          // 本仓库的组件测试只断言行为、结构与渲染出的文本/类名，不断言 EP 的样式与内部行为。
          inline: ['element-plus'],
        },
      },
      exclude: [...configDefaults.exclude, 'e2e/**'],
      root: fileURLToPath(new URL('./', import.meta.url)),
      // passWithNoTests 已在 Task 2 移除：它只是脚手架无测试文件时的临时措施。
      // 「没有测试却退出 0」会让门禁假通过；移除后无测试文件时 vitest 以 exit 1 结束
      // （已用反向探针验证），这才是门禁该有的行为。
    },
  }),
)
