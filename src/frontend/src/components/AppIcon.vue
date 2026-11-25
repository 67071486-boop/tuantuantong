<script setup lang="ts">
import type { GlyphIconName } from '@/config/layuiIconNames'

// 允许 null/undefined 并**在组件内部**吃掉：调用方 `GROUP_ICONS[g.name]` 在 noUncheckedIndexedAccess 下是 `GlyphIconName | undefined`，这样调用点无需 `as`/`!`，也不必自造「兜底图标」（自造默认值＝发明数据）——查不到就不渲染。
// 用 `GlyphIconName` 而不是 `LayuiIconName`：后者含 `indeterminate` 这类**没有字体字形**的名字，写进来会「有名字、没图形」，类型上就不收。
defineProps<{ name: GlyphIconName | null | undefined }>()

// 模板必须把字形包在 `<el-icon>` 里：Element Plus 菜单图标的对齐/定宽规则只作用于 `[class^=el-icon]`（见 node_modules/element-plus/theme-chalk/el-menu.css 的 `.el-menu-item [class^=el-icon]` 与收起态两条；`font-size:18px` 是 EP 默认、被 `styles/shell.css` 覆盖成对标物的 16px——这里要的是定宽/居中/间距那几条）。直接放 `<i class="layui-icon">` 两条都命中不了，60px 收起栏里会歪。
// 这条理由**写在 script 里而不是模板里**：模板根节点上一旦有 HTML 注释，组件在 **dev/test 下**就变成多根（fragment）——`@vue/test-utils` 的 `wrapper.element` 会变成包壳 DIV、`html()` 里多一个注释节点（`__tests__/AppIcon.spec.ts` 实测抓到）。
// **修它的理由不是「attribute 透传会失效」**（那句已被推翻）：Vue 3.5.43 的 `filterSingleRoot()` 判据——注释节点**不参与**「谁是根」，但 `v-if` 占位注释**有意**保留为根 ⇒ 传进来的属性照样落在唯一元素上；生产编译还会把根注释整条丢掉（`comments: false`），修复前后**产物逐字节相同**。真正坏掉的是**测试可见的根节点形状**。随时可重跑：`pnpm vitest run src/components/__tests__/AppIcon.spec.ts`（它用 `wrapper.element.tagName === 'I'` 钉住根是那唯一的 `<i>`；探针与产物比对的原始记录在被 gitignore 的草稿区，不作证据指向）。
</script>

<template>
  <el-icon v-if="name" aria-hidden="true">
    <i class="layui-icon" :class="`layui-icon-${name}`" />
  </el-icon>
</template>
