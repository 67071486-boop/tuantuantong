/** 「指定发平台」的 13 个平台字典。**唯一出处**。
 *
 * 值与文案**照抄对标物**「订单项目分类管理」弹窗的复选框（读码，快照
 * `.tools/recon/gameconfig/OrderTypsSetting.html`，DOM 顺序即此顺序）：
 * `8 全能代 · 10 代练通 · 30 代练妈妈 · 50 咕噜代练 · 60 易代练 · 70 代练宝 · 80 代练丸子 ·
 *  90 趣代练 · 91 代练多 · 92 代游通 · 93 天玩代练 · 94 通天代练 · 95 享代练`。
 *
 * ⚠️ 数据库里 `ProjectCategory.assigned_platforms` **只存 id**（逗号拼接），展示名由这里映射。
 * 所以改文案只改本文件，不用回填历史行；反过来，**id 不能改**（改了等于把已配好的分类指到别的平台）。
 *
 * ⚠️ 这些平台**目前没有消费方**：它们只在「发单到交易平台」时有意义，而渠道对接是 M6/三期
 * （决策 D-0023 的代价）。本文件是**先按对标落数据与展示**，不要据此认为发单已经按平台分流。
 */
export type Platform = { id: number; label: string }

export const PLATFORMS: readonly Platform[] = [
  { id: 8, label: '全能代' },
  { id: 10, label: '代练通' },
  { id: 30, label: '代练妈妈' },
  { id: 50, label: '咕噜代练' },
  { id: 60, label: '易代练' },
  { id: 70, label: '代练宝' },
  { id: 80, label: '代练丸子' },
  { id: 90, label: '趣代练' },
  { id: 91, label: '代练多' },
  { id: 92, label: '代游通' },
  { id: 93, label: '天玩代练' },
  { id: 94, label: '通天代练' },
  { id: 95, label: '享代练' },
]

const BY_ID = new Map(PLATFORMS.map((p) => [p.id, p.label]))

/** 逗号拼接的 id 串 → id 数组（丢掉空段与非法段，保持字典顺序）。 */
export function parsePlatformIds(raw: string | null | undefined): number[] {
  if (!raw) return []
  const ids = raw
    .split(',')
    .map((part) => Number.parseInt(part.trim(), 10))
    .filter((id) => Number.isInteger(id) && BY_ID.has(id))
  // 去重 + 按字典顺序：**必须有确定性**，否则「勾一样的东西」会写出两种不同的字符串，
  // 前端测试与将来的对拍都会飘。
  return PLATFORMS.filter((p) => ids.includes(p.id)).map((p) => p.id)
}

/** id 数组 → 逗号拼接串（存库形态）。 */
export function serializePlatformIds(ids: readonly number[]): string {
  const known = PLATFORMS.filter((p) => ids.includes(p.id)).map((p) => p.id)
  return known.join(',')
}

/** 逗号拼接的 id 串 → 展示名串（对标该列显示的 `assPlatName`）。
 *
 * ⚠️ **分隔符「、」是推断**（等级：推断，不得当依据引用）：本次拿到的该租户
 * `GetAllOrderTypeList` 是**空集**，快照里取不到 `assPlatName` 的原文，所以「多个平台之间用什么分隔」
 * 没有实测依据。取「、」只是因为它是中文列举的常规写法。等有非空数据时**必须实测复核**，
 * 届时这条注释要改成实测结论。 */
export function platformNames(raw: string | null | undefined): string {
  return parsePlatformIds(raw)
    .map((id) => BY_ID.get(id) ?? '')
    .filter((label) => label !== '')
    .join('、')
}
