/**
 * island（iframe）↔ Vue 壳之间的**消息类型常量**。
 *
 * ⚠️ **两端只有这一份定义**（照 `layui-island/pages/order-addorder.ts::ORDER_SAVED_MESSAGE_TYPE`
 * 的做法）：各写一份字面量会在改名时**静默断链**——两边各自的测试都自洽，合起来不工作
 * （本仓最忌讳的「看起来一致、实际分叉」；反例就在 `island:reload` 那对字面量上）。
 *
 * ⚠️ **为什么不放在发消息的岛页模块里**（`order` 那对的放法）：那一对的收发双方都在
 * **island 包**里；本对的收方是 **Vue 壳**（`components/LayuiIslandHost.vue`）。壳静态 import
 * 一个岛页模块会把整页（及其 layui 依赖）拖进壳的急切图，破坏 D-0025 的
 * 「island 是独立文档、与壳物理隔离」⇒ 收口到这个只放常量的中立模块。
 */

/**
 * 头像**认领成功后**由岛页发给壳：壳据此重新取一次会话用户（右上角随即换成新头像）。
 *
 * ⚠️ 载荷**只有 `type`**（不带 key / URL）：壳的取数走会话 store 既有的 `refresh()`
 * → `fetchMe()`，⛔ 不由岛页把链接塞进来（否则壳就要吃一个来自 iframe 的字段）。
 */
export const AVATAR_UPDATED_MESSAGE_TYPE = 'island:avatar-updated'
