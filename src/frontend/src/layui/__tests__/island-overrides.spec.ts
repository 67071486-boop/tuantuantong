import { describe, expect, it } from 'vitest'
import { ISLAND_OVERRIDES } from '../island'

/**
 * `ISLAND_OVERRIDES` 是**共享**的对标全局样式注入（`island.ts` 的取值表说明它的收录判据）。
 * 本文件只钉「首页快捷操作那一排依赖的 5 条到底在不在、是不是对标原文」——
 * 反复出问题的地方正是「某页漏搬了一条全局规则，看起来只是小一号」（见 D-0135 的复刻线）。
 *
 * ⚠️ 期望值写**字面量**（E-0024：不许拿被断言的同一个常量当期望），且逐字等于
 * `.tools/recon/css/admin.css` 里的原文。
 */
describe('ISLAND_OVERRIDES 里的快捷操作规则', () => {
  it('4 条 .layadmin-shortcut 规则逐字在位（尺寸/排版全靠它们）', () => {
    expect(ISLAND_OVERRIDES).toContain('.layadmin-shortcut li{text-align:center}')
    expect(ISLAND_OVERRIDES).toContain(
      '.layadmin-shortcut li .layui-icon{display:inline-block;width:100%;height:60px;line-height:60px;text-align:center;border-radius:2px;font-size:30px;background-color:#F8F8F8;color:#333;transition:all .3s;-webkit-transition:all .3s}',
    )
    expect(ISLAND_OVERRIDES).toContain(
      '.layadmin-shortcut li cite{position:relative;top:2px;display:block;color:#666;text-overflow:ellipsis;overflow:hidden;white-space:nowrap;font-size:14px}',
    )
    expect(ISLAND_OVERRIDES).toContain('.layadmin-shortcut li:hover .layui-icon{background-color:#f2f2f2}')
  })

  it('全站手型光标那条也在（对标 admin.css 的 [lay-href],[lay-tips],[layadmin-event]）', () => {
    expect(ISLAND_OVERRIDES).toContain('[lay-href],[lay-tips],[layadmin-event]{cursor:pointer}')
  })
})
