/**
 * 【交易平台发单配置】页的 layui island 复刻（对标 `/System/PlatformPushOrderConfig`，
 * 对标 `<title>交易平台发单配置</title>`）。
 *
 * ## 证据出处
 *
 * | 项 | 出处 |
 * |---|---|
 * | 页内 `<style>`（两段）、body 标记、工具栏/行操作模板、隐藏的 `.modifyParamDiv` 表单 | `public/replica/system-platformpushorderconfig.html` ← `.tools/recon/suqing1/html/System__PlatformPushOrderConfig.html` **L24-41 + L52-178**（逐字；只删了 `<script src>` 与 HTML 注释，PII 无） |
 * | 表头 `cols` | `.tools/recon/suqing1/assets/JS2019/System/PlatformPushOrderConfig.js` **L26-71** |
 * | `table.render` 参数 | 同上 **L20-72** |
 *
 * ## 渲染参数取值（逐条）
 *
 * - `defaultToolbar` 写了两次（L23 `[]` → L25 `['filter']`）⇒ **后者生效**。
 * - `page:false`（L72）；无 `height`、无 `cellMinWidth`（对标原文没有，别自己加）。
 * - 我们**恒空、不发请求**：对标 `url:'/SystemAjax/GetPlatformPushOrderConfigList'`。
 *
 * ## 表头逐条核过 `type`
 *
 * 本页 `cols` 里**只有第一列**带 `type`（`PlatformPushOrderConfig.js` L27
 * `{ type:'checkbox', fixed:'left', width:30 }`，已逐字抄）；其余列都是普通列
 * （`grep -n "type:" PlatformPushOrderConfig.js` 的其他命中全是 `layer.open({type:1})`
 * 与 ajax 的 `type:"Post"`，不是 cols）。
 * 这一列必须带 `type`：少了它会被 `cellMinWidth` 的 60px 下限抬到 61（对标 31），
 * 且**表头勾选框会静默消失**——而 `parity.mjs` 的表头比对只读「文案+宽度」，
 * 空文案的特殊列丢了它不会报（所以这里是人工核过、不是靠探针发现的）。
 *
 * ## 两处照抄、不要「修正」的原文
 *
 * 1. `pushPlatfromPriceScale` / `pushPlatfromBuilderPrivePhone` 的 **`Platfrom` 拼写**是对标原文
 *    （同页与 `pushPlatformSafeAmount` 混用两种拼法），改了就对不上他们的 `templet`。
 * 2. 隐藏弹层 `.modifyParamDiv` 里「默认发单平台」那格 `.layui-input-block` 在服务端产物里**是空的**
 *    （L116-119；对标 JS 期望的 `input[name="platformIds"]` 没渲染出来，说明该租户没有已对接的平台）。
 *    照抄空，不自己编平台选项。
 *
 * 列 `templet` 的语义（空表下不渲染，仅备查）：`pushPlatfromPriceScale` → `{值}%`（带 `lay-tips`）、
 * `isAutoWritePhone` → 「留空 / 保持和订单一致」、`pushPlatfromBuilderPrivePhone` →「生成 / 不生成」、
 * `pushPlatformRequire` 是文本；最后一列 `toolbar:'#baroperating'`（行内「配置参数 / 清空」）。
 */
import { artifactPage, type ArtifactCol } from '@/layui/artifactPage'
import type { IslandPage } from '@/layui/island'

const page = artifactPage({
  url: 'replica/system-platformpushorderconfig.html',
  tables: [
    {
      elem: '#tableList',
      // 逐条照抄 `PlatformPushOrderConfig.js` L26-71。
      cols: [
        // 对标 L27：`{ type:'checkbox', fixed:'left', width:30 }` —— **逐字抄**（含 `type`）。
        // `type:'checkbox'` 不可省也不可换成普通列：`initOpts` 会给特殊列关掉 `cellMinWidth`
        // 钳制（`setColsWidth` 的 `"normal"===t.type && (a<i && (t.width=a=i))`），
        // 少了 `type` 这一列会被 60px 下限抬到 61（对标渲染 31），后面每一列都跟着错位。
        { type: 'checkbox', fixed: 'left', width: 30 },
        { field: 'gameName', title: '游戏名称', width: 120, fixed: 'left' },
        { field: 'pushPlatformNames', title: '默认发单平台', width: 230, align: 'left' },
        { field: 'pushPlatfromPriceScale', title: '发单价百分比', width: 100, align: 'center' },
        { field: 'pushPlatformSafeAmount', title: '发单安全保证金', width: 100, align: 'center' },
        { field: 'pushPlatformEffAmount', title: '发单效率保证金', width: 100, align: 'center' },
        { field: 'pushPlatformPhone', title: '发单联系电话', width: 120, align: 'center' },
        { field: 'pushPlatformQQ', title: '发单联系QQ', width: 120, align: 'center' },
        { field: 'isAutoWritePhone', title: '保留玩家电话', width: 120, align: 'center' },
        { field: 'pushPlatfromBuilderPrivePhone', title: '自动生成虚拟号', width: 120, align: 'center' },
        // 对标 L67：这一列**没有 `width`**（自动列，吃掉定宽列之外的剩余宽度——实测渲染 318）。
        { field: 'pushPlatformRequire', title: '发单要求', align: 'left' },
        // 对标 L70：`fixed:''`（不固定）、宽 160、`unresize:true`。
        { title: '操作', width: 160, unresize: true },
      ] satisfies ArtifactCol[],
      toolbar: '#toolbarDemo',
      defaultToolbar: ['filter'],
      page: false,
    },
  ],
})

export default {
  mount: async (root, ctx) => {
    const unmount = await page.mount(root, ctx)
    document.title = '交易平台发单配置'
    return unmount
  },
} satisfies IslandPage
