/**
 * HTML 转义的**唯一出处**（`AGENTS.md` §5「不重复同一事实」）。
 *
 * 用途：layui 的 `templet` 函数返回值、以及用 `innerHTML` 拼出来的选项/单元格——
 * 它们都被**当 HTML 插入**，而其中的名字 / 说明 / 项目文本来自用户输入。
 * 不转义就是存储型 XSS（对标自己也没转义，但那不是我们照抄缺陷的理由）。
 *
 * ## 为什么抽成独立模块（而不是页面互相 import）
 *
 * 原实现在 `pages/order-myorderlist.ts`，被整体评审 I-4 点名的另两处 sink
 * （`pages/system-sourelist.ts` 的来源名称/说明列、`pages/order-addorder.ts` 的来源 `<option>`）
 * 也需要同一份实现。让页面 A import 页面 B 会把两张页面的加载图耦在一起；
 * 抽到本模块后**只此一份**实现，`order-myorderlist.ts` 改为从这里 re-export（对外签名不变）。
 */
export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}
