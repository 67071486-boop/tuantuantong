/**
 * **iframe 子页关掉承载自己的那个 `layer` 弹层**的共享工具。
 *
 * 为什么是独立文件、且**不放在 `pages/` 下**：`layui-island/page.ts` 用
 * `import.meta.glob('./pages/*.ts')` 把 `pages/` 下**每个 `.ts` 都注册成一个 island 页面**
 * （key = 文件名）——把共享工具放进 `pages/` 会凭空多出一个页面 key，
 * `config/__tests__/replicaRoutes.spec.ts` 的全站网会因此变红。
 *
 * 出处：本文件的内容原本长在 `pages/system-setgames.ts`；Task 13 的
 * `pages/user-adduser.ts` 也要用它做「保存后回列表」的双态判据，于是整体搬到这里。
 * `system-setgames.ts` 保留同名 re-export，既有 spec 的 import 一个字都不用改。
 */

/** 关弹层时读写**父窗口**的最小形状（父页的 layui 把 `layer` 挂成了 `window.layer`）。 */
export type LayerHostWindow = {
  name: string
  parent: unknown
}

/**
 * 关掉承载本页的那个 `layer` 弹层（标准 layui iframe 关闭法）。
 *
 * 为什么在**父窗口**上调：`layer` 的查找走 `layui.$`，只在**当前文档**里找 `.layui-layer`，
 * 而弹层属于父页那个文档 ⇒ 子页自己调 `layer.close` 是 no-op（同仓先例与机制记录见
 * `pages/system-roleauthority-edit.ts` 的注释）。
 *
 * 判据用「索引是不是**非空**」而不是真值：`getFrameIndex` 回的 `.attr('times')` 是**字符串**
 * （`'7'`），而 `'0'` 这种以 0 开头的层索引不存在于 layui（层索引从 1 起），所以真值判断在这里
 * 也不会误伤；显式写出来是为了让下一个人不必去查 layui 的索引起点。
 * 直接打开本页（不在弹层里）时 `parent.layer.getFrameIndex('')` 回 `undefined` ⇒ 静默 no-op。
 *
 * **返回值**（Task 13 加的）：`true` = 确实关掉了自己（即本页跑在 `layer.open({type:2})` 的
 * iframe 里）；`false` = 关不到（父窗口没有 `layer`，或拿不到层索引）。调用方靠它区分
 * 「在弹层里」与「我是整页路由」——`pages/user-adduser.ts` 的 `backToList()` 就是这么用的。
 */
export function closeIslandLayer(win: LayerHostWindow): boolean {
  const parent = win.parent as
    | { layer?: { getFrameIndex?: (name: string) => unknown; close?: (index: number | string) => void } }
    | null
    | undefined
  const layer = parent?.layer
  if (!layer) return false
  const index = layer.getFrameIndex?.(win.name)
  if (index === undefined || index === null || index === '') return false
  layer.close?.(index as number)
  return true
}
