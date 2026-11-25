import { describe, expect, it } from 'vitest'
import { MENU_GROUPS } from '@/config/menu'

/**
 * 【系统人员】菜单名的冻结断言。
 *
 * 原文件断言的是 7 个 Element Plus 占位页 schema 的转录计数
 * （列数 / 按钮数 / 勾选列）。那套占位页机制已按 D-0026 换成 layui island 1:1 复刻页并被删除
 * （见 `router/index.ts` 里 7 条带 `meta.islandPage` 的 `/system-users/*` 路由），
 * 所以计数、勾选、列数那些断言随机制一起去掉。
 *
 * **保留下来的**是其中仍然承重的一半：这些菜单名确实在冻结 IA（`config/menu.ts`）的
 * 【系统人员】组里——菜单名是冻结项，出处 `docs/FRONTEND-LAYOUT.md` §4。
 * 2026-09-21 业务裁剪（D-0036）删掉 3 条（邀请大神注册管理 / 打卡记录 / 打卡分析），
 * 本清单随之从 7 条收窄到 4 条。
 */
const SYSTEM_USER_PAGE_LABELS = [
  '登录限制设置',
  '人员黑名单',
  '人员实名记录与配置',
  '员工电子合同管理',
]

describe('【系统人员】菜单名（冻结 IA）', () => {
  it('4 个复刻页的 label 都在【系统人员】组里（菜单名是冻结项）', () => {
    const group = MENU_GROUPS.find((g) => g.name === '系统人员')
    expect(group).toBeDefined()
    const labels = new Set(group!.items.map((i) => i.label))
    for (const label of SYSTEM_USER_PAGE_LABELS) {
      expect(labels.has(label), `菜单里没有「${label}」`).toBe(true)
    }
  })
})
