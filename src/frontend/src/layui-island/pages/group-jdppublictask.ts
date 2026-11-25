/**
 * 【查找公共排团坑位】页的 **layui 复刻页**（对标物 `/Group/jdppublictask`，**证据件渲染**）。
 *
 * 形状：**卡片/看板页**（筛选栏 5 个控件 + 7 组职业快捷按钮 4 列 + 空卡片列表 + 分页 + 1 个隐藏弹层）。
 * 证据：
 *  - 标记与页内 `<style>` → `public/replica/group-jdppublictask.html`
 *    （源 `.tools/recon/suqing1/html/Group__jdppublictask.html` L24-326）
 *  - 本页**没有 `table.render`**（草稿 `tables: []`）；卡片列表由 `jdppublictask.js` L55-114 用
 *    `$.each` 拼字符串生成（无后端 ⇒ 我们保持 `.taskList` 为空）
 *  - 分页 → `jdppublictask.js` **L115-126**：`laypage.render({elem:'pages', count:data.count, limit:10, curr:page})`
 *
 * ## 分页为什么照渲染（`count: 0`）
 *
 * 对标的分页是 `laypage.render` 渲染进 `#pages`（HTML L307），与表格无关。我们无后端 ⇒ `count` 取 0，
 * layui 的 `laypage` 在 `count<1` 时只是**页码组为空**（`layui.js` 里 `if(n.count<1)return""`），
 * 外层 `div.layui-laypage` 仍会生成、仍可见（上一页/下一页禁用态，标测宽 135px）——
 * 与对标观测一致（`.tools/parity-all.json`：对标 `pagination = {y:271, w:135}`，
 * 只差 y，属数据/行数差异）。**不编造条数**。
 *
 * ## `xm-select` 的已知差异（**不在本页自造插件**）
 *
 * 筛选栏里若有 `xm-select` 属主的控件（本页没有），我们的 island 只把它当普通 `<select>` 渲染
 * （见 `impl-brief.md` §「特别提醒」5）。本页 5 个控件全是 `radio`/`input`/`button`，**不受影响**。
 */
import { artifactPage } from '@/layui/artifactPage'
import { bootstrapIsland, type IslandContext, type IslandPage } from '@/layui/island'

const ARTIFACT_URL = 'replica/group-jdppublictask.html'

/** 对标 `laypage.render` 的 `elem`（`jdppublictask.js` L116；HTML L307 的 `id="pages"`）。 */
const PAGES_ELEM = 'pages'

const base = artifactPage({ url: ARTIFACT_URL })

export default {
  mount: async (root: HTMLElement, ctx: IslandContext) => {
    // 对标 `<title>` 是服务端渲染的（`Group__jdppublictask.html` L5）。
    document.title = '查找公共排团坑位'
    const unmount = await base.mount(root, ctx)
    const layui = await bootstrapIsland(['form', 'laypage'])
    ;(layui as unknown as { laypage: { render: (o: unknown) => void } }).laypage.render({
      elem: PAGES_ELEM,
      // 无后端 ⇒ 条数 0（**不编造**）；`limit`/`curr` 照对标 L118-119。
      count: 0,
      limit: 10,
      curr: 1,
    })
    return unmount
  },
} satisfies IslandPage
