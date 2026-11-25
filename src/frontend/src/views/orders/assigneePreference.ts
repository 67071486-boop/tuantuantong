/**
 * 「默认指派此人」那一次提交该带什么（**四态**；见 `api/orders.ts::CreateOrderPayload.remember_assignee`）。单独一个模块的原因：这条判据有**两个**页面消费——录单 island 页（`order-addorder.ts` **再导出**本函数，既有测试的 import 路径不变）与客服精简工作台（`views/kefu/KefuOrderForm.vue`），两份实现必然漂移，表现为「同一格勾选、两条录入路径发给后端的偏好不同」——静默且难发现。
 * 四态（逐条有用例；⛔ 别读成「把三态放宽」——原来的三态在 `isHall=false` 时**逐字不变**）：勾着且选中了人 ⇒ `'Y'`；勾着但**没选中人**（如默认接手人已不在候选名单）⇒ `undefined`（**不动**：这次没有可记的人，⛔ 不把已有偏好悄悄清掉）；没勾且服务端本来有 ⇒ `'N'`（清除，照对标 tip 语义）；本来没有 ⇒ `undefined`。`'hall'` 档：本单是「内部抢单」且勾着 ⇒ `'hall'`（记的是**档位**不是某个人，`selectedId` 这一档恒 `null`），没勾 ⇒ 同上面的 `'N'` / `undefined`。
 * `isHall` = **这一单**的档位、`initialIsHall` = **服务端**记着的档位（`defAssIsHall`）。「服务端本来有」= `initialDefaultAssigneeId > 0` **或** `initialIsHall`——后端那两列互斥，但写成 `||` 是为了**不依赖**那条互斥来读对，也不给「两列同时为真」留一条静默丢偏好的路。
 */
export function rememberAssigneeValue(
  selectedId: number | null,
  checked: boolean,
  initialDefaultAssigneeId: number,
  isHall = false,
  initialIsHall = false,
): 'Y' | 'N' | 'hall' | undefined {
  const hadSomething = initialDefaultAssigneeId > 0 || initialIsHall
  if (isHall) {
    if (checked) return 'hall'
    return hadSomething ? 'N' : undefined
  }
  if (checked) {
    if (selectedId !== null) return 'Y'
    return undefined
  }
  return hadSomething ? 'N' : undefined
}
