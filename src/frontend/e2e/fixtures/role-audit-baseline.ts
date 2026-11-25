/**
 * 职位对拍**基线**（spec §4）：**已知且已登记**的 403 白名单。
 *
 * ## 它解决什么问题
 * 审计（`specs/05-role-action-audit.spec.ts`）每条发现都是一条「职位 R + 页面 P + 控件 C ⇒ 403」。
 * 若"有 403 就报红"，这条线从第一天就是红的、然后没人看（`e2e-golden-path` §7 的同一课）。
 * 所以把**已知缺陷**冻在这里，判据是**双向相等**：
 *  - 跑出来的 403 集合 == 基线集合 ⇒ **绿**；
 *  - 出现**基线之外**的 403 ⇒ **红**（新缺陷 / 回归）；
 *  - 基线里某条**不再复现** ⇒ **也红**（文案：「基线陈旧：<条目> 不再复现；若缺陷已修，请删掉这一条」）。
 * ⇒ **修好缺陷的人会被这张网逼着把基线删掉**，基线因此只会缩。
 *
 * ## 三条纪律（都不是装饰）
 *  1. **每条必须有 `D-01xx` 编号**：基线是「已登记的已知缺陷」，不是豁免通道。没有编号的条目
 *     连基线都进不去——判据见本文件的 `validateBaseline()`（被 spec 的两条用例消费：
 *     一条断真基线全部合规，一条用**合成坏条目**证明校验器真的会拒）。
 *  2. ⛔ **不许拿「跑出来是空的」当通过**：`findings` 为空而基线非空 ⇒ 走上面的「基线陈旧」分支、**必须红**。
 *     （这是本线最容易被自己糊过去的方向：网瞎了与缺陷修好了，表现都是空。）
 *  3. **匹配按稳定前缀**：`control` 允许写成身份串的**稳定前缀**（`spec` §3.2 的串形状保证前段稳定、
 *     后段含动态文本）；`urlIncludes` 只用 URL 里稳定的一段。这样基线不会因为页面文案小改而假红。
 *
 * ## ⚠️ 本文件里的 `D-0145` 条目是**临时**的
 * `D-0145`（客服持有 `order.assign_taker`，但指派弹层的候选人名单挂在 `system_user.list` 上）
 * 正由**另一个会话**在修。它一修好，本条目对应的 403 就不再复现 ⇒ 本网会以
 * **「基线陈旧」**红——那是**设计如此**（逼人删条目），不是网坏了。
 * ⇒ 修完 `D-0145` 的那条线**必须同一次删掉本文件里对应的条目**。
 * ✅ **已应验（2026-09-24，合并 `main` 后）**：上面这两条已按这一句删掉，本文件现在**没有**
 *    `D-0145` 条目。经过见下面那段追加。
 *
 * ## 🔄 2026-09-24（合并 `main` 后）：基线清空——**本机制第一次在真实变更上生效**
 *
 * **为什么删**：`main` 落了 **`D-0146`**（`c635450f`）：指派弹层的候选名单改走**专用端点**
 * `GET /api/business/orders/assignee-options/`，**不再**打 `GET /api/accounts/`。
 * ⇒ 原先冻结的那两条 `D-0145`（`cs` / `supervisor` 在 `/orders` 的「指派打手」上打
 * `/api/accounts/` 的 403）**不再复现** ⇒ 本网真的报了「**基线陈旧**」（控制方实测原始输出）：
 *
 * ```text
 * Error: cs：基线陈旧：下列条目**不再复现**；若缺陷已修，请删掉 e2e/fixtures/role-audit-baseline.ts 里对应的条目（留着会让本网永远红）：
 *   - 基线陈旧：#0 cs ｜ /orders ｜ a.layui-btn.layui-btn-xs##指派打手 ｜ /api/accounts/ ｜ D-0145
 * Error: supervisor：基线陈旧：#1 supervisor ｜ /orders ｜ a.layui-btn.layui-btn-xs##指派打手 ｜ /api/accounts/ ｜ D-0145
 * ```
 *
 * 处置 = **删掉那两条**（缺陷已由 `D-0146` 修好），`ROLE_AUDIT_BASELINE` 因此是**空数组**。
 *
 * ⭐ **这一次和以前那些红不是一回事**：以前的红都是**人为变异**造出来的（spec §12.4 的 N4 就是
 * 临时把缺陷改回缺陷态）；这一次是**外部真实变更**（另一个会话在 `main` 上换掉了名单端点）触发的
 * ⇒ **本机制第一次在真实变更上生效**——「陈旧基线必须红」不是自证，它真的抓住了一次变更。
 *
 * ⚠️ 基线为空**只说明「此刻没有已登记的已知 403」**；将来会不会再需要它，取决于有没有新的
 * 已登记缺陷——本网对这一点没有预言能力。
 *
 * ⚠️ **订正（同一轮）**：本注释上面「三条纪律」第 3 条里那句「这样基线不会因为页面文案小改而假红」
 * **在实际用法下不成立**：`control` 身份串按 spec §3.2 含**文本前 12 字**，而冻结时写的是完整串
 * （如 `…##指派打手`）⇒ 按钮文案一改，`controlMatches()` 的前缀匹配就命中不了，同一条会**同时**
 * 进 `extra` 与 `stale`（判红）。前缀匹配只在**基线刻意只写稳定前缀**时才有那个效果，本文件今天
 * 没有那样写。详见 spec §11。
 */
import type { RoleName } from './auth'

/**
 * 基线里的一条：**职位 / 页面 / 控件身份串（或它的稳定前缀）/ URL 片段 / 依据编号 / 一行说明**。
 *
 * ⛔ `decision` 是**必填**（`string`，不是可选）：类型层面挡掉"忘了写编号"，
 * 运行期再由 `validateBaseline()` 按 `^D-\d{4}$` 挡掉"写了但不合规"（如 `D-145` / `D-0145（待定）`）。
 */
export type RoleAuditBaselineEntry = {
  /** 职位名（`fixtures/auth.ts` 的 `RoleName`，与报告里的 `row.role` 同一套取值）。 */
  role: RoleName
  /** 出事的页面路径（`reachable` 里的那一条，如 `/orders`）。 */
  page: string
  /**
   * **这条控件属于哪一层壳**（审计报告的 `findings[].surface`）：
   * 页内控件 = 页面路径；层内控件 = `layer:<层标题>`。
   *
   * ⚠️ **可选键，缺省 = 页内**（= `page`，与 `findings[].surface` 的页内取值同形）——
   * 这样既有条目（在本线扩张之前冻结的）一个字都不用改，语义也不变。
   *
   * ⛔ **`surface` 是匹配键的一部分**：同一个 `(page, control 身份串)`
   * 在**层内**与**页内**各有一条 403 时，**必须**各认各的条目。修之前 `surface` 只在给人看的
   * 文案里用、**不参与匹配** ⇒ 层内发现会被页内基线条目**静默吸收**（`extra` 与 `stale` 都为空、
   * 整轮全绿），而本线的目的恰恰是新增层内发现。判据见 `entriesFor()` / `stale` 与
   * `05-role-action-audit.spec.ts` 里的合成用例。
   */
  surface?: string
  /** 控件身份串（spec §3.2 形状），或它的**稳定前缀**（前缀匹配见 `controlMatches()`）。 */
  control: string
  /** 这条 403 的 URL 里必有的一段（如 `/api/accounts/`）——⛔ 不写 query（query 里有动态值）。 */
  urlIncludes: string
  /** 依据的决策编号：**必须**匹配 `DECISION_ID_PATTERN`。 */
  decision: string
  /** 一行说明：这条为什么是已知缺陷、以及"修好后要做什么"。 */
  note: string
}

/** 决策编号的形状（`AGENTS.md` §5F 的预留段 / 主线号都是四位）。 */
export const DECISION_ID_PATTERN = /^D-\d{4}$/

/**
 * 冻结的已知 403。
 *
 * ⚠️ **今天为空（2026-09-24，合并 `main` 后）**。历史：曾冻结 `D-0145` 两条
 * （`cs` 与 `supervisor` 各一条，同一根因：**弹层打得开、名单是空的**）；
 * `D-0146` 把指派弹层的名单换成专用端点后那两条 403 **不再复现** ⇒ 按「陈旧基线必须红」
 * 的纪律删掉了（清空经过与实测原始输出见本文件开头的追加段）。
 * `taker` / `dev` 一条都没有（它们的菜单里到不了 `/orders`）；`admin` 是对照，
 * 它那一遍必须 0 条，否则是网自己坏了。
 *
 * ⛔ 空数组是**合法**基线（跑出来也是空的 ⇒ 两侧相等 ⇒ 绿）；它**不**表示"以后不用基线"。
 */
export const ROLE_AUDIT_BASELINE: readonly RoleAuditBaselineEntry[] = []

/** 一条基线不合规的具体原因（给报错文案用）。 */
export type BaselineEntryError = {
  /** 在 `ROLE_AUDIT_BASELINE` 里的下标（合成用例里就是传入数组的下标）。 */
  index: number
  /** 出问题的字段名。 */
  field: keyof RoleAuditBaselineEntry
  message: string
}

/**
 * 校验一条基线：**没有 `D-01xx` 编号的条目进不了基线**（spec §4 的判据）。
 *
 * ⛔ 这不是"防御性写法"：它就是 spec §4 那条判据本身，且被 spec 用**合成坏条目**证明过会拒
 * （见 `05-role-action-audit.spec.ts` 的「基线自检」用例）——否则一个只断言"真基线合规"的
 * 自检是**恒真**的（`E-0024`）。
 */
function validateEntry(entry: RoleAuditBaselineEntry, index: number): BaselineEntryError[] {
  const errors: BaselineEntryError[] = []
  const requireText = (field: keyof RoleAuditBaselineEntry, value: unknown): void => {
    if (typeof value !== 'string' || value.trim() === '') {
      errors.push({ index, field, message: `${String(field)} 必填且不能是空串` })
    }
  }
  requireText('role', entry.role)
  requireText('page', entry.page)
  if (typeof entry.page === 'string' && entry.page !== '' && !entry.page.startsWith('/')) {
    errors.push({ index, field: 'page', message: `page 必须是页面路径（以 / 开头），实际是「${entry.page}」` })
  }
  requireText('control', entry.control)
  requireText('urlIncludes', entry.urlIncludes)
  requireText('note', entry.note)
  if (typeof entry.decision !== 'string' || !DECISION_ID_PATTERN.test(entry.decision)) {
    errors.push({
      index,
      field: 'decision',
      message:
        `decision 必须是 D-01xx 形状的编号（${DECISION_ID_PATTERN}），实际是 ` +
        `${JSON.stringify(entry.decision)}——没有编号的条目**不许**进基线（基线是已登记的已知缺陷，不是豁免通道）`,
    })
  }
  return errors
}

/** 校验整份基线（逐条走 `validateEntry()`）。 */
export function validateBaseline(
  entries: readonly RoleAuditBaselineEntry[],
): BaselineEntryError[] {
  return entries.flatMap((entry, index) => validateEntry(entry, index))
}

/**
 * 控件身份串是否匹配：**相等**，或基线写的是实际串的**稳定前缀**。
 *
 * ⚠️ 只允许「基线 → 实际」这一个方向的前缀匹配：身份串的后段含动态文本（订单号 / 金额），
 * 页面文案微调就会让它变；而**反向**（实际串是基线的前缀）没有意义、还会掩盖误写。
 */
export function controlMatches(baselineControl: string, actualControl: string): boolean {
  return actualControl === baselineControl || actualControl.startsWith(baselineControl)
}

/** 比对用的一条发现（= 审计报告里 `findings` 的一条，只取比对需要的字段）。 */
export type BaselineFinding = {
  page: string
  control: string
  urls: string[]
  /**
   * **这条控件属于哪一层壳**（spec §2 的归属条）：页内控件 = 页面路径；层内控件 = `layer:<标题>`。
   *
   * ⚠️ **它是可选键**（T1 扩张时加的）：不给时按**页内**处理（`surface` 取 `page`），
   * 所以既有调用方照旧能比。
   * ⛔ **它是匹配键的一部分**：
   * 同一个 `(page, control)` 的层内发现**不许**被页内基线条目认领（也不能反过来），
   * 判据见本文件的 `baselineSurfaceOf()` / `findingSurfaceOf()` 与两个合成用例。
   */
  surface?: string
}

/** 双向比对的结果：`extra` = 基线之外的新 403；`stale` = 基线里不再复现的条目。 */
export type BaselineDiff = {
  /**
   * 有 403、但**至少有一条 URL 没被任何基线条目覆盖**（新缺陷 / 回归）⇒ **红**。
   *
   * ⚠️ `uncoveredUrls` 就是那些**没人认领**的 URL：文案必须点名它们——否则读报告的人
   * 只看到「页面 ｜ 控件」，还得自己去翻是哪条端点越界了。
   */
  extra: { finding: BaselineFinding; uncoveredUrls: string[]; label: string }[]
  /** 基线里有、但这次没跑出来 ⇒ **红**（文案写「基线陈旧…」）。 */
  stale: { entry: RoleAuditBaselineEntry; label: string; index: number }[]
}

/** 给报错文案指认一条基线（职位 / 页面 / 控件 / 编号）。 */
export function describeBaselineEntry(entry: RoleAuditBaselineEntry, index: number): string {
  return `#${index} ${entry.role} ｜ ${entry.page} ｜ ${entry.control} ｜ ${entry.urlIncludes} ｜ ${entry.decision}`
}

/**
 * 一条基线条目的 `surface`（**缺省 = 页内**，页内控件的 `surface` 就是页面路径）。
 *
 * ⛔ 与 `findingSurfaceOf()` 成对存在、必须用**同一套归一化**（`E-0083`：只归一化一侧会让
 * 判据看起来更可靠）。页内默认值在这里只写一次，⛔ 不在 `entriesFor()` / `stale` 各写一遍。
 */
function baselineSurfaceOf(entry: RoleAuditBaselineEntry): string {
  return entry.surface ?? entry.page
}

/** 一条发现的 `surface`（**缺省 = 页内**；兼容不传 `surface` 的既有调用方）。 */
function findingSurfaceOf(finding: BaselineFinding): string {
  return finding.surface ?? finding.page
}

/**
 * 给报错文案指认一条**基线之外**的发现：页面 / 控件 / **具体哪条 URL 没被覆盖**。
 *
 * ⛔ 不要把发现里**所有** URL 都铺出来（旧 `describeFinding()` 那样）：同一条发现里可能**既有**
 * 已知的 403、又有新冒出来的 403——读的人必须一眼看出**越界的是哪一条**，否则这条收紧白做。
 */
export function describeUncoveredFinding(
  finding: BaselineFinding,
  uncoveredUrls: readonly string[],
): string {
  const which =
    uncoveredUrls.length > 0
      ? `未被基线覆盖的 403 URL：${uncoveredUrls.join(' , ')}`
      : '（这条发现里没有任何 403 URL）'
  // ⚠️ `surface` 只在**它存在**时打（T1 扩张加的键）：页内控件那条发现今天是
  // `surface === page`，逐条打出来是噪声；层内控件（`layer:<标题>`）必须打——否则同一个
  // (页面, 控件身份串) 在层内与页内各有一条时，报错文案读不出是哪一条越界了。
  const where = finding.surface && finding.surface !== finding.page ? ` ｜ surface=${finding.surface}` : ''
  return `${finding.page}${where} ｜ ${finding.control} ｜ ${which}`
}

/**
 * 把**某个职位**的发现与基线做双向比对。
 *
 * ⛔ 只比 `role` 这一层：基线是**按职位**冻结的（spec §4 的条目就是"职位 / 页面 / 控件 / URL"），
 * 不按职位过滤会把 `cs` 的那条算到 `taker` 头上。
 *
 * ⛔ **覆盖判据是 URL 级的「每一条都要有人认领」**（收口轮收紧，原实现是 `urls.some(...)`）：
 * 旧写法下「同一个控件**换成另一条** `403` URL」会被旧条目**静默吸收**——`extra` 与 `stale`
 * 都为空、整轮全绿。真实形态见 `D-0146`（名单端点 `/api/accounts/` → `assignee-options/`）：
 * 若那次是**换端点**而不是**修权限**，旧基线就会把新的 403 吸收掉。⇒ 现在**该控件的每一条 URL
 * 都必须被某条同页同控件的基线条目覆盖**，没被覆盖的那些进 `extra`（判红，文案点名是哪条 URL）。
 *
 * ⚠️ `findings` 为空而基线非空时：`extra` 为空、`stale` 非空 ⇒ 调用方**必须**把 `stale` 判红
 * （spec §4：不许把"跑出来是空的"当成通过）。
 */
export function diffBaseline(
  role: RoleName | string,
  findings: readonly BaselineFinding[],
  baseline: readonly RoleAuditBaselineEntry[] = ROLE_AUDIT_BASELINE,
): BaselineDiff {
  const entries = baseline
    .map((entry, index) => ({ entry, index }))
    .filter(({ entry }) => entry.role === role)
  /** 与这条发现**同页、同 surface、同控件**的基线条目（URL 的覆盖必须落在这一组里）。 */
  const entriesFor = (finding: BaselineFinding): RoleAuditBaselineEntry[] =>
    entries
      .filter(
        ({ entry }) =>
          entry.page === finding.page &&
          // ⛔ `surface` 参与匹配：层内发现不许被页内条目认领、反之亦然。
          baselineSurfaceOf(entry) === findingSurfaceOf(finding) &&
          controlMatches(entry.control, finding.control),
      )
      .map(({ entry }) => entry)
  /** 这条发现里**没有任何基线条目认领**的 URL。 */
  const uncoveredUrlsOf = (finding: BaselineFinding): string[] => {
    const candidates = entriesFor(finding)
    return finding.urls.filter((url) => !candidates.some((entry) => url.includes(entry.urlIncludes)))
  }

  const extra = findings
    .map((finding) => ({ finding, uncoveredUrls: uncoveredUrlsOf(finding) }))
    // ⚠️ 一条 URL 都没有的发现（理论上不会出现：发现只在 `fresh403.length > 0` 时才建）也判 `extra`，
    //    ⛔ 不让它因为「没有 URL 可判」而静默消失（旧的 `.some()` 会把它判红，这个方向不能放宽）。
    .filter(({ finding, uncoveredUrls }) => uncoveredUrls.length > 0 || finding.urls.length === 0)
    .map(({ finding, uncoveredUrls }) => ({
      finding,
      uncoveredUrls,
      label: describeUncoveredFinding(finding, uncoveredUrls),
    }))
  // `stale` 仍按「这条基线有没有**任意一条** URL 命中」判——与上面那条**不同轴**：
  //    上面问「有没有 URL 没人认领」，这里问「有没有条目没人认领」。
  // ⚠️ 但「同页同控件」这一层过滤**必须**与 `entriesFor()` 用同一把尺（含 `surface`）：
  //    否则「层内发现 + 页内基线条目」会同时进 `extra`（没人认领 URL）与保持不 stale——
  //    两处判据对同一件事给出不一致的读数（`E-0083`）。
  const stale = entries
    .filter(
      ({ entry }) =>
        !findings.some(
          (finding) =>
            entry.page === finding.page &&
            baselineSurfaceOf(entry) === findingSurfaceOf(finding) &&
            controlMatches(entry.control, finding.control) &&
            finding.urls.some((url) => url.includes(entry.urlIncludes)),
        ),
    )
    .map(({ entry, index }) => ({ entry, index, label: describeBaselineEntry(entry, index) }))
  return { extra, stale }
}
