import { describe, expect, it } from 'vitest'
import { formatAccountLabel } from '../account'

// ⚠️ 账号区那个占位常量已于 2026-09-24（我的资料线）**整个删除**：工作室名走 `/api/auth/me` 的
// `tenant_name`、昵称/职位走 `display_name`/`role`、头像走 `config/avatar.ts` 的生成器。
// 本文件因此只剩 `formatAccountLabel` 这一条纯函数判据（「占位数据」那组随常量一起删）。
describe('formatAccountLabel', () => {
  it('按对标物截图的半角圆括号拼「账号名(职位)」', () => {
    expect(formatAccountLabel('示例工作', '系统管理员')).toBe('示例工作(系统管理员)')
  })

  it('职位为空时不留空括号', () => {
    expect(formatAccountLabel('示例工作', '')).toBe('示例工作')
  })
})
