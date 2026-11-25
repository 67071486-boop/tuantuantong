/** ⚠️ 已登记的重复点：断点值在本文件与 styles/shell.css 的 media query 里各出现一次
 *  （JS 需要判断窗口宽度来驱动抽屉/收起）。**改一处必须同时改另一处。**
 *  值出处：docs/FRONTEND-LAYOUT.md §6。 */
export const BREAKPOINTS = {
  compactDesktopMax: 1199,
  tabletMax: 991,
  mobileMax: 767,
} as const
