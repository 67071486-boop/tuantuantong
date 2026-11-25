/**
 * 【会员分析报表】页的 **layui 复刻页**（对标物 `/Customer/hyanalysisreport`，**证据件渲染**）。
 *
 * 形状：**ECharts 看板**——`body` 只有 ~0.9KB：两行 `.layui-row`（`#SourceType`/`#ConsumType` 两个
 * `.block`）+ 一行（`#HyRaise`）+ 一个 600×400 的 `#main`。**没有表格、没有筛选栏、没有工具栏**
 * （所以**不套**列表模板，`artifactPage` 的 `tables` 留空）。
 * 证据：
 *  - 标记与页内 `<style>` → `public/replica/customer-hyanalysisreport.html`
 *    （源 `.tools/recon/suqing1/html/Customer__hyanalysisreport.html` 的 L43-末尾，逐字；仅删非标记 `<script>`）
 *  - 容器尺寸/位置全部来自页内 `<style>` 的 `.block{width:98%;height:540px;…}` 与
 *    `#main` 的行内 `style="width:600px;height:400px"`（HTML L43）——**逐字搬，不自造**。
 *
 * ## 图表**不实现**（数据我们没有）
 *  对标由 `assets/JS2019/echarts.min.js` + `hyanalysisreport.js` 把 4 个容器渲染成饼图/柱图/折线。
 *  我们没有那些数据，也**不引 echarts**：容器与标题结构照对标渲染，**图表区域留空**；
 *  点这 4 个容器任意一处 → 统一提示「尚未开放」（不置灰、不隐藏）。
 *  `artifactPage` 自带的点击收口只认 `button/a.layui-btn/[lay-event]/…`，**不认普通 `div`**，
 *  所以本页在挂载后补一个捕获阶段监听（页内 JS，不改证据件标记）。
 */
import { artifactPage } from '@/layui/artifactPage'
import { bootstrapIsland, islandMsg, type IslandContext, type IslandPage } from '@/layui/island'

/** 证据件（对标页 `<style>` 段 + body 标记，零转义）。 */
const ARTIFACT_URL = 'replica/customer-hyanalysisreport.html'

/** 4 个图表容器（对标 `hyanalysisreport.js` 里 `echarts.init` 的 4 个 elem，HTML L32-51）。 */
const CHART_SELECTOR = '#SourceType, #ConsumType, #HyRaise, #main'

const base = artifactPage({ url: ARTIFACT_URL })

export default {
  mount: async (root: HTMLElement, ctx: IslandContext) => {
    // 对标 `<title>` 是服务端渲染的（`Customer__hyanalysisreport.html` L5）。
    document.title = '会员分析报表'
    const unmount = await base.mount(root, ctx)
    const layui = await bootstrapIsland(['layer'])
    const onClick = (event: Event): void => {
      const el = (event.target as HTMLElement).closest<HTMLElement>(CHART_SELECTOR)
      if (!el) return
      event.preventDefault()
      event.stopPropagation()
      islandMsg(layui.layer as Record<string, unknown>, '「会员分析图表」尚未开放')
    }
    root.addEventListener('click', onClick, true)
    return () => {
      root.removeEventListener('click', onClick, true)
      unmount()
    }
  },
} satisfies IslandPage
