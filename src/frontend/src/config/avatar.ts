/**
 * 「按昵称生成默认头像」——**纯前端、不落库**（spec §8；业务方裁定 D3）。
 *
 * ⚠️ 对标物**没有**这个功能（`grep` 全语料无出处）⇒ 色板是**本线自选**、不进像素对拍。
 * ⚠️ 为什么按**哈希**取色而不是每次随机：同一账号跨刷新 / 跨页面 / 跨 iframe（island）必须同色，
 * 否则侧栏与「我的资料」页会各显示一个颜色，看起来像两个人。
 *
 * 圆形由**使用方**的 CSS 给（`.shell-account__avatar{border-radius:100%}` / 证据件的
 * `layui-circle`）——SVG 自己不画圆，同一张图在两个位置才都对。
 */

/** 12 个**纯色**（无渐变），亮度对齐主色 `#009688` 量级，白字可读。顺序即取色顺序。 */
export const AVATAR_PALETTE = [
  '#009688', '#1E9FFF', '#5FB878', '#FF5722', '#FFB800', '#f76b6b',
  '#a37bd4', '#4a90d9', '#3b9c8f', '#d98b3a', '#8a7fd1', '#2f9e6e',
] as const

/** 首字：中文取首个字符，拉丁转大写，空 ⇒ `'?'`。 */
export function avatarInitial(name: string): string {
  const trimmed = (name ?? '').trim()
  if (trimmed === '') return '?'
  // 按**码点**取（`Array.from` 而不是 `[0]`）：后者会把代理对切成半个字符。
  const first = Array.from(trimmed)[0] as string
  return /[a-z]/i.test(first) ? first.toUpperCase() : first
}

/** 32 位 djb2 变体哈希 → 取模取色。 */
export function avatarColor(name: string): string {
  let hash = 5381
  for (const ch of name ?? '') {
    hash = ((hash << 5) + hash + (ch.codePointAt(0) ?? 0)) >>> 0
  }
  return AVATAR_PALETTE[hash % AVATAR_PALETTE.length] as string
}

/**
 * XML 文本转义。⚠️ **刻意不 import `@/layui-island/escape`**：那是 island 层的模块，
 * 而本文件同时被 Vue 壳（`components/AppAccountBlock.vue`）使用——共享工具不该反向依赖页面层。
 */
function escapeXml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/**
 * 内联 SVG 的 data URI（`size` 同时是宽高与字号基准）。
 *
 * ## 字形纵向定位：`dy="0.35em"` 已删（真浏览器实测）
 *
 * 原实现同时写了 `dominant-baseline="middle"` **和** `dy="0.35em"`（双重下移）。真浏览器里
 * 把本函数产出的 SVG 解出来**内联**进文档、用 `getBBox()` 量（`<img>` 里的 SVG 取不到内部 DOM，
 * ⛔ jsdom 没有 `getBBox()`），实测（Chromium，macOS，字体栈 Helvetica Neue/PingFang SC）：
 *
 * | size | 带 `dy` 的偏移 | 删掉 `dy` 后 | 两者都删 |
 * |---|---|---|---|
 * | 72（壳侧栏） | **+7.97px（+11.06%）** | −3.23px（−4.49%） | −11.50px（−15.97%） |
 * | 240（资料页） | **+25.72px（+10.72%）** | −12.08px（−5.03%） | −40.00px（−16.67%） |
 *
 * 偏移 = **量到的盒**中心 − 方框中心（正 = 偏下）；阈值 = 方框高度的 5%。带 `dy` 时两档都超阈值
 * ⇒ 按「保留 `dominant-baseline="middle"`、删掉多余的 `dy`」修；删后回到阈值内
 * （240px 那一档 −5.03% 正好压线）。
 *
 * ⚠️ **量测口径（对上面那个「压线」的结论有直接影响，别只看报告）**：Chromium 的 `getBBox()`
 * 对 `<text>` 返回的是**字体盒（font box）**、**不是墨迹盒（ink box）**——实测同一 `font-size`
 * 下不同字形的**高度恒等**（32px 字号恒 `h=37.00`、108px 恒 `h=126.00`），只有宽度随字符变
 * （CJK 32.00 / `A` 21.18 / `?` 17.80）。所以这里判的是**字体度量的居中**，不等于肉眼墨迹居中；
 * 换字体栈或换浏览器时这个数字会变。
 *
 * ⛔ **不要为这个位置加常驻测试**：`getBBox()` 只在真浏览器里有值，判据就是上面那次一次性量测
 * （原始探针已按约定删除）。
 */
export function avatarDataUri(name: string, size: number): string {
  const letter = escapeXml(avatarInitial(name))
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" ` +
    `viewBox="0 0 ${size} ${size}">` +
    `<rect width="${size}" height="${size}" fill="${avatarColor(name)}"/>` +
    `<text x="50%" y="50%" text-anchor="middle" dominant-baseline="middle" ` +
    `font-size="${Math.round(size * 0.45)}" fill="#ffffff" ` +
    `font-family="Helvetica Neue,Helvetica,PingFang SC,Tahoma,Arial,sans-serif">${letter}</text>` +
    `</svg>`
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`
}
