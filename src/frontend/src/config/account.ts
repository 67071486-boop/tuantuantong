/**
 * 顶栏右侧的「账号名(职位)」。
 *
 * 占位数据已删除（2026-09-24 我的资料线）：工作室名走 `/api/auth/me` 的 `tenant_name`、
 * 昵称/职位走 `display_name`/`role`、头像走 `config/avatar.ts`。
 * （那个常量留着等于留一条永远为假的路，spec §7.5。⚠️ 本文件按 spec §7.5 的「不留半新半旧」判据
 * 在其**标识符本身**上被搜过（`grep -rn` 应为空），所以这里只称「账号区占位常量」、不复写它的标识符。）
 *
 * 括号形状取自业务方截图（形如「示例工作(系统管理员)」，具体租户名不入库）——
 * 侧栏账号区那一处对标物用的是方括号（机器人01[机器人专用]），两处各按自己的实测，
 * 故本函数只服务顶栏，不要拿它去渲染侧栏。
 */
export function formatAccountLabel(userName: string, role: string): string {
  return role === '' ? userName : `${userName}(${role})`
}
