/** 镜像 src/apps/core/error_codes.py（后端是唯一出处；改动必须两边同步）。
 *
 * 2026-09-20 更新（账号线并入后）：
 *  1. 实现是扁平 0/1000/1001/1002/1003/1004；D-0005 提案的 1xxx/2xxx/3xxx/9xxx 分段并未实现。
 *  2. `CONFLICT=1001` **已被使用**：账号新增/编辑的「登录账号已存在」用它
 *     （`apps/accounts/views.py::_conflict_response`）。旧注释「已声明、未使用」已作废。
 *  3. `LAST_ADMIN` / `SELF_LOCKOUT` 由账号线引入（最后管理员保护）：前者 = 该操作会让租户
 *     没有可登录的管理员；后者 = 不能对自己执行停用/降级。
 */
export const ErrorCode = {
  OK: 0,
  VALIDATION: 1000,
  CONFLICT: 1001,
  BUSINESS: 1002,
  LAST_ADMIN: 1003,
  SELF_LOCKOUT: 1004,
  /**
   * ⭐ **`1022 ORDER_CREATED_ASSIGN_FAILED`**（2026-10-03，D-0179「录单页快速指派」）。
   *
   * 语义特殊：`POST /orders/` 带 `assignee` 时订单**已经建好并落库**，只有「指派」那一步没成
   * ⇒ 页面必须把它与「请求被拒」分开（不然用户再点一次保存就是**又建一张单**）。
   *
   * ⚠️ **本镜像只登记「前端真的有分支」的那几个 code**（`ErrorCode` 不是后端码表的全量镜像）：
   * 平台对接的 `1015`–`1017`、上下游的 `1019`–`1021` 都按各自页面的既有做法**在用处**写数字
   * 并注明出处（例：`order-addorder.ts` 的 `if (e.code === 1015)`）。
   * 评审 Minor-4 的处置：`1022` **补进镜像**（而不是继续写字面量）——因为本线在页面上
   * 有一个**真的分支**，同一个数字两个出处迟早漂移。
   */
  ORDER_CREATED_ASSIGN_FAILED: 1022,
  /**
   * ⭐ **`1023 ORDER_CREATED_THROW_FAILED`**（2026-10-08，`feat/hall-throw-autorefresh` ——
   * 录单页弹层的「内部抢单」那一档：建单 + 同一次请求抛进内部抢单大厅）。
   *
   * 语义与 `1022` **同形**：订单**已经建好并落库**（`UNASSIGNED`、`hall_price` 已写上），
   * 只有「抛进大厅」那一步没成 ⇒ 页面必须把它与「请求被拒」分开，并**复位表单**
   * （否则用户再点一次保存就是又建一张单）。
   *
   * ⚠️ **登记进镜像而不是在用处写数字**，理由与上面 `1022` 逐字相同：本页对它有**真的分支**。
   * 后端出处 = `src/apps/core/error_codes.py` 的 `ORDER_CREATED_THROW_FAILED = 1023`
   * （由同批的后端任务登记；本镜像照「后端是唯一出处」的规矩跟同一批加）。
   */
  ORDER_CREATED_THROW_FAILED: 1023,
  /**
   * ⭐ **实名认证三档**（2026-10-10，`feat/realname` —— 子账号在「我的资料」页自助核验）。
   *
   * 后端出处 = `src/apps/core/error_codes.py:105-107`（`REALNAME_DISABLED` /
   * `REALNAME_QUOTA_EXHAUSTED` / `REALNAME_NO_PACKAGE`）。**三档各自对应「该找谁」**，
   * 所以⛔ 不许在页面上合成一句「核验失败」（那会让用户去找一个没错的地方）：
   *  - `1025` 本租户没启用 ⇒ 找**管理员**开总开关；
   *  - `1026` 本租户余量不足 ⇒ 找**客服购买**；
   *  - `1027` 平台侧没有可用密钥包 ⇒ 找**平台管理员**补包（用户自己做什么都没用）。
   *
   * ⚠️ 登记进镜像而不是在用处写数字，理由与上面 `1022` / `1023` 逐字相同：本线在页面上
   * 对这三档**各有一个真的分支**。
   */
  REALNAME_DISABLED: 1025,
  REALNAME_QUOTA_EXHAUSTED: 1026,
  REALNAME_NO_PACKAGE: 1027,
  /**
   * ⭐ **`1029 REALNAME_RATE_LIMITED`**（2026-10-18，D-0251「实名核验按人限流」）。
   *
   * 同一用户 1 小时内提交核验超过阈值（默认 5 次）⇒ 这一档。与 `1025`–`1027` 同一条纪律：
   * 它是一条**确定的业务拒绝**，⛔ 不许拿既有码顶替（`1026` 会让人去催客服**买次数**、
   * `1000` 会让人去改一个**没写错**的号码）。**不锁定**——窗口滚过自动恢复，文案只说「稍后再试」。
   * 后端出处 = `src/apps/core/error_codes.py` 的 `REALNAME_RATE_LIMITED = 1029`
   * （登记进镜像的理由与上面 `1025`–`1027` 逐字相同：两个页面对它**各有一个真的分支**）。
   */
  REALNAME_RATE_LIMITED: 1029,
} as const

export type ErrorCodeValue = (typeof ErrorCode)[keyof typeof ErrorCode]
