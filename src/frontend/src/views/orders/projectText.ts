/**
 * 「代练项目」文本的两条共享规则（录单岛页与客服精简工作台**共用**这一份，两处各写一份必然漂移）。拼/删那条的由来：子串删有**真实的误删形态**——项目 A「星耀」是 B「星耀上王者」的前缀，文本 `星耀/星耀上王者` 取消 A 时会把 B 改成 `上王者`（评审实测）。
 * 字节上限闸（E-0242）收进来的原因：「详情 → 二次编辑」上线后**两条录入路径都会把这段文本同步出去**，闸必须两边都有。⛔ 不让 Vue 组件反过来 import 岛页拿它——岛页顶层拖着一整条 layui / 请求依赖，进组件的测试图会把 spec 的模块桩打爆；岛页**再导出**，既有测试的 import 路径不变（先例：`rememberAssigneeValue` ⇠ `views/orders/assigneePreference.ts`）。
 */
export function toggleProjectName(text: string, name: string, checked: boolean): string {
  if (name === '') return text
  const parts = text.split('/')
  if (checked) {
    if (parts.includes(name)) return text
    return text === '' ? name : `${text}/${name}`
  }
  return parts.filter((part) => part !== name).join('/')
}

/**
 * 鸡蛋铺同步通道对「代练项目」（`#project`）自由文本的接受上限，按 **UTF-8 字节**计（不是字符数）。依据：生产取证（kexing，`docs/ERRORS.md` **E-0242**）——同步失败的 6 张订单 `project` 文本**全部 ≥734 字节**、成功的 438 张**全部 ≤732 字节**、零重叠 ⇒ 对方实际接受上限在 **≈732/733 字节**一带；业务方裁定：录单前端加**字节上限闸**，超限**警告并阻止保存**（不做静默截断），取 **700** 留出余量。
 * ⚠️ 与 `#project` 的 `maxlength="900"`（**D-0218** 的内部口径，900 **字符**）**并存不冲突**：那道是「我们库里存不存得下」的第一层，这一道是「能不能同步出去」的第二层（900 个汉字 = 2700 字节，远超对方上限，只按字符数卡不住）。计字节用 `new TextEncoder().encode(text).length`。偏离登记：`docs/FRONTEND-REPLICA-STANDARD.md` §7。
 */
export const PROJECT_TEXT_SYNC_MAX_BYTES = 700

/**
 * 「代练项目」字节上限闸的判据（供录单岛页 `onSave`、客服工作台 `submit` 的校验链与测试直接调）。超限（> `PROJECT_TEXT_SYNC_MAX_BYTES` 字节）返回**人话文案**，否则返回 `''`；按 **UTF-8 字节**计不按字符数——`TextEncoder` 恒产出 UTF-8，`.length` 即字节数。
 * ⚠️ 文案带字段名「代练项目」（E-0074 口径），且由业务方给定口径，勿改写。
 */
export function projectTextSyncLimitError(text: string): string {
  const bytes = new TextEncoder().encode(text).length
  if (bytes <= PROJECT_TEXT_SYNC_MAX_BYTES) return ''
  return `代练项目超出鸡蛋铺同步上限（当前 ${bytes} 字节，上限 ${PROJECT_TEXT_SYNC_MAX_BYTES} 字节），请精简后再保存；完整细节可放订单截图`
}
