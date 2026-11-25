/**
 * 「管理订单页」批量操作的**两步式共用流程**（spec §5.1 / §D12；Phase B · Task 3）。
 *
 * 一次调用 = 最多两发请求：
 *  1. `dry_run: true`（**只读**）——由**后端**回答「哪几笔合格、不合格的各自为什么」；
 *  2. 用户确认后 `dry_run: false`（执行）——`order_nos` **只能是上一发回来的 `eligible`**
 *     （混入不合格号会被后端**整批拒**，spec §D4 / Ruling 11）。
 *
 * ## 三条出口（spec §D12）
 *
 * - **全合格** ⇒ 管理订单页展示执行名单并等确认。缺省调用仍直接执行。
 * - **部分不合格** ⇒ 展示拦下的订单及原因。管理订单页同时展示执行名单。
 * - **全不合格**（`eligible` 为空）⇒ **只弹清单**（按钮 `['知道了']`），⛔ 不发 apply。
 *
 * ⛔ 本模块**不做业务判据**、⛔ 不镜像状态机（全局约束第 7 条）：能不能做、为什么不能做，
 * 一律是后端 `dry_run` 的答案，前端只负责**原样**呈现与确认。失败文案一律用后端 `msg` 原文。
 *
 * ## 失败口径（边界是「那一批有没有执行成功」）
 *
 * - **执行成功之前**的两类失败——**layui 加载失败**、**两发 `batchOrderAction` 任一失败**——
 *   一律「**报一句 + 返回 `false`**」，⛔ **不 reject**。`Promise<boolean>` 的「已执行 / 未执行」
 *   两态因此是可判的（E-0011：本注释与实现一致，`try` 的边界就在 `runBatchFlow` 里）。
 * - **执行成功之后**（那一批**已经成功**）：`onDone()` 与 `msg(successMessage(...))` 各自有
 *   **内层 `try/catch`**——它们若同步抛（例如 `reload()` 碰到 null 节点），只 **`logError` 记一笔**，
 *   ⛔ **不吞成「这一批没执行」**、仍返回 `true`（裁定：**执行成功就该报成功**）。
 *   `onDone()` 抛**不影响**紧随其后的成功提示（它照弹）；`successMessage()` 自身抛则取不到文案
 *   ⇒ 那一步**不 `msg`**（⛔ 不弹空话/半个串）。
 *
 * ⚠️ **仍能以 rejection 结束的那条路是「报告通道本身抛」**：注入的 `options.msg` **或**缺省时的
 * `islandMsg → layer.msg`（见 `src/frontend/src/layui/island.ts`）自身抛——失败分支里那句
 * `msg(errText(e))`、以及 layui 加载失败时退到 `options.msg` 那句，都在 `try` 之外。
 * ⇒ 本注释**不声称「承载路径一律不 reject」**，也**不声称逃逸口只有 `options.msg` 一个**
 * （E-0011：绝对断言必须被枚举证过；这两处调用点见 `runBatchFlow` 内）。
 * 两个**调用点**：① `let layer … catch` 末尾的 `report(errText(e))`；② 校验/执行失败分支的 `msg(errText(e))`。
 *
 * ## ⭐ T1/T2（2026-10-05，`fix/batch-feedback-affordance`）：点了就有反应 + 防重复提交
 *
 * 业务方现场：勾二三十笔点「批量验收」→ 点「确定」后**界面没有任何反馈**（不转圈、按钮还能点）⇒
 * 以为没点上又点了一遍 ⇒ 第二发的预检发现这些单**已经全是已完成**，弹出一串
 * `非法状态迁移：COMPLETED → COMPLETED`。根因两条，本模块一处收口（有 **9** 个调用点）：
 *
 * - **T1 loading**：进流程**立刻** `layer.load(1, { shade: false })`（对标
 *   `.tools/recon/p2/raw/setordersta.js:74` 的形状，⛔ 不进 git），并在**所有出口**关闭它。
 *   开关收敛到 `openLoading()` / `closeLoading()` 两个闭包 + 一个 `finally`（⛔ 不逐个出口手写
 *   `layer.close`）；**要弹 `layer.confirm` 之前先关**（否则 loading 与确认层叠层），
 *   用户确认之后、写请求之前**再开**。
 * - **T2 防重复提交闸**：模块级 `inFlight`——在飞期间第二次调用**零请求、零消息、返回 `false`**，
 *   且**不碰第一发的 loading**（它归第一发所有）。`try/finally` 保证成功 / 失败 / 取消 / 抛错
 *   **任一路径都复位**（⛔ 不许一次失败永久锁死页面）。
 *
 * ⚠️ 闸是**模块级**的（共享给 9 个入口；一页同时跑两个批量动作没有意义）。loading **归第一发所有**。
 *
 * ⚠️ **闸的最坏失败形态（如实登记，⛔ 本批不加看门狗）**：`inFlight` 只在 `finally` 复位 ⇒ 唯一
 * 能把它**永久占住**的是「确认层那个 Promise 永不 settle」。今天逐条查过**不可达**：关层走
 * `options.cancel`（X 分支）、两颗按钮各自接一个回调、未开 `shadeClose`、vendored layui 的 layer
 * 没有 ESC 分支、本页也没有 `closeAll` 调用。**但代价必须写清**：真发生时它会把这**整页 9 个批量
 * 入口一起锁死**（比改前「只坏那一个动作」更严重）⇒ 只登记后果，处置留给后续（⛔ 加看门狗超本批范围）。
 *
 * ## 两条「与页面的关系」——本批**实测后**的定案（报告 §⭐ 有逐条证据）
 *
 * - **`escapeHtml`**：它**不是**页面私有函数。今天它已在共享模块 `@/layui-island/escape`
 *   （`escape.ts:15`），页面在 `order-orderlist.ts:896` import 的是**同一份**
 *   ⇒ 本模块**直接复用**那一份。⛔ 不另抄一份：那会造出第二份实现（`AGENTS.md` §5
 *   「不重复同一事实」），且任务书里「选①自带一份」的前提（「它可能是页面私有」）已不成立。
 *   因此**无需改动页面**（选项②的代价在这一处不存在——共享模块**已经在了**）。
 * - **`layer`**：`@/layui/loader` **没有** `layui` 导出（只有 `loadLayui` 与类型，
 *   `loader.ts:14/74`）⇒ 用 `loadLayui(['layer'])` 取命名空间。它是幂等的（脚本文档只插一次、
 *   模块由 layui 自己缓存，`loader.ts:50-72`），与页面 `bootstrapIsland` 走的是同一条路。
 *   `msg` 缺省时用 `islandMsg(layer, text)`——即页面 `order-orderlist.ts:2880` 那句
 *   `const msg = (text) => islandMsg(layer, text)` 的**同一份实现、同一参数**（`{time:1500}`）。
 */
import { batchOrderAction, type BatchAction, type BatchEffect, type BatchIssue, type BatchOutcome } from '@/api/orders'
import { escapeHtml } from '@/layui-island/escape'
import { islandMsg } from '@/layui/island'
import { loadLayui, type LayuiModule } from '@/layui/loader'

/** `runBatchFlow` 的入参，默认使用旧版确认弹层。 */
export type BatchFlowOptions = {
  orderNos: string[]
  confirmation?: {
    actionLabel: string
    sourceOrderNos: ReadonlyMap<string, string>
    isSelectionCurrent: () => boolean
  }
  action: BatchAction
  /** 不含 `order_nos` / `dry_run`（这两个键由本模块补）。 */
  payload: Record<string, unknown>
  /** 退款用它按合格子集过滤 `request_ids`（spec §D13）；其余动作不传。 */
  applyPayload?: (eligible: string[]) => Record<string, unknown>
  successMessage: (appliedCount: number) => string
  /** 成功后的 `reload()`。 */
  onDone: () => void
  /** 便于测试注入；缺省用页面同款的 `islandMsg(layer, text)`。 */
  msg?: (text: string) => void
  confirm?: (preview: BatchPreview) => Promise<boolean>
}

export type BatchPreview = {
  actionLabel: string
  applying: string[]
  extras: string[]
  skipped: string[]
}

/** `layer` 的最小面（本模块只用这四处）：四参形态见下方 `confirmList` 的说明。 */
type MessageLayer = {
  confirm: (
    content: string,
    options: { btn: string[]; title: string; cancel: () => void; end: () => void },
    yes: (index: number) => void,
    btn2?: () => void,
  ) => unknown
  msg: (text: string, options?: unknown) => void
  /** 对标形状 `layer.load(1, { shade: false })` ⇒ 返回层索引（关闭时要用）。 */
  load: (type?: number, options?: unknown) => number
  /** 与 `load` 配对：`layer.close(index)` 关掉那一层 loading。 */
  close: (index: number) => void
}

/**
 * 错误 → 展示文案。口径 = spec §D12「沿用 `errText(e)`（`BizError.message`）」，实现与页面
 * `order-orderlist.ts:2882` **逐字同形**（`e instanceof Error ? e.message : '操作失败'`）。
 *
 * ⚠️ 计划里写「Consumes `@/api/http` 的 `errText`」——**实测该模块没有这个导出**
 * （`http.ts` 的导出清单里没有它），`errText` 今天是**若干页面各自本地**的一句（本页
 * `:2882`、`order-orderview.ts:632`、`emp-billsummary.ts:793` …）。本模块**不 import 页面**
 * （那会把两张页面的加载图耦合在一起），也不擅自改 Task 2 已过审的 `api/http.ts`
 * ⇒ 这里按同一口径自带一句（报告 §⭐ 第 3 条）。
 */
const errText = (e: unknown): string => (e instanceof Error ? e.message : '操作失败')

/**
 * 不合格清单（spec §D12 的「单号 + 原因」，一行一条）。
 *
 * ⛔ 单号与后端 `msg` **一律过 `escapeHtml`**（spec §5.1 末句）：两者都会进 `layer.confirm`
 * 的 HTML 正文，直接拼就是注入。
 */
function ineligibleListHtml(issues: BatchIssue[]): string {
  return issues
    .map(
      // ⭐ D-0301：逐条显示**展示单号**（来源号非空取它、空回退内置号；
      // 老后端载荷没有该键或取不到行时是 `undefined` / 空串 ⇒ 都回退 `order_no`）。
      (issue) =>
        `单号 ${escapeHtml(issue.source_order_no || issue.order_no)}：${escapeHtml(issue.msg)}`,
    )
    .join('<br>')
}

function batchOrderLabel(orderNo: string, sourceOrderNo?: string): string {
  return sourceOrderNo && sourceOrderNo !== orderNo
    ? `来源单号 ${sourceOrderNo}（系统单号 ${orderNo}）`
    : `单号 ${orderNo}`
}

function readBatchEffects(raw: unknown): BatchEffect[] {
  if (!Array.isArray(raw)) throw new Error('未能取得联动订单名单，请刷新页面后重试')
  const effects: BatchEffect[] = []
  const orderNos = new Set<string>()
  for (const effect of raw as Array<Partial<BatchEffect> | null>) {
    if (!effect || typeof effect.order_no !== 'string' || !effect.order_no ||
        typeof effect.source_order_no !== 'string' || typeof effect.operation !== 'string' ||
        !effect.operation || orderNos.has(effect.order_no)) {
      throw new Error('联动订单名单有误，请刷新页面后重试')
    }
    orderNos.add(effect.order_no)
    effects.push({
      order_no: effect.order_no,
      source_order_no: effect.source_order_no,
      operation: effect.operation,
    })
  }
  return effects
}

function batchPreview(
  confirmation: NonNullable<BatchFlowOptions['confirmation']>,
  eligible: string[],
  ineligible: BatchIssue[],
  effects: BatchEffect[],
): BatchPreview {
  const direct = new Set(eligible)
  const related = new Map(effects.map((effect) => [effect.order_no, effect]))
  const applying = eligible.map((no) => {
    const effect = related.get(no)
    const label = batchOrderLabel(no, confirmation.sourceOrderNos.get(no) || effect?.source_order_no)
    return effect ? `${label}（联动：${effect.operation}）` : label
  })
  const issues = new Map(ineligible.map((issue) => [issue.order_no, issue]))
  const extras = effects.filter((effect) => !direct.has(effect.order_no)).map((effect) => {
    const label = batchOrderLabel(effect.order_no, effect.source_order_no)
    const issue = issues.get(effect.order_no)
    const reason = issue ? `（不能直接操作：${issue.msg}）` : ''
    return `${label}：${effect.operation}${reason}`
  })
  const skipped = ineligible.filter((issue) => !related.has(issue.order_no)).map((issue) => {
    const label = batchOrderLabel(issue.order_no, issue.source_order_no || confirmation.sourceOrderNos.get(issue.order_no))
    return `${label}：${issue.msg}`
  })
  return { actionLabel: confirmation.actionLabel, applying, extras, skipped }
}

function confirmationHtml({ actionLabel, applying, extras, skipped }: BatchPreview): string {
  return [
    `<p><strong>${escapeHtml(actionLabel)} · 将处理 ${applying.length} 笔订单</strong></p>`,
    applying.map(escapeHtml).join('<br>'),
    extras.length > 0 ? `<p><strong>另有 ${extras.length} 笔联动订单</strong></p>${extras.map(escapeHtml).join('<br>')}` : '',
    skipped.length > 0
      ? `<p><strong>不会处理 ${skipped.length} 笔订单</strong></p>${skipped.map(escapeHtml).join('<br>')}`
      : '',
  ].join('')
}

/** 点击确认时关本层。取消、右上角和关闭回调都能解除等待。 */
function confirmList(layer: MessageLayer, html: string, btn: string[]): Promise<boolean> {
  return new Promise((resolve) => {
    layer.confirm(
      html,
      { btn, title: '批量操作', cancel: () => resolve(false), end: () => resolve(false) },
      (index) => {
        resolve(true)
        if (Number.isInteger(index)) layer.close(index)
      },
      () => resolve(false),
    )
  })
}

/**
 * 兜底留痕：写 `console.error('[orderBatch] …')`。
 *
 * ⚠️ **中性描述**（原文写「layui 都起不来时的最后兜底」——已不准确）：它现在有**两类**调用点——
 * ① layui 加载失败且未注入 `msg`（那时页面上确实没有 UI 可弹）；② **layui 好好的**、只是
 * 「那一批已执行成功之后」的 `onDone()` / `successMessage()` 同步抛（只为留一笔痕迹，成功提示照弹）。
 * ⛔ 别把它读成「只在没有 UI 时才走到」。
 */
function logError(text: string): void {
  console.error(`[orderBatch] ${text}`)
}

/**
 * 模块级**防重复提交闸**（T2）：同一页面同一时刻只允许一个批量流程在飞。
 *
 * ⚠️ 判据/复位路径见模块头注释。它必须住在模块顶层（共享给 9 个入口）——住在某一页里
 * 会让另外 8 个入口各自为政（同一页两个批量动作没有意义）。
 */
let inFlight = false

/**
 * → `true` = 已执行；`false` = **未执行**（用户取消 / 无合格项 / layui 加载失败 / 请求失败 /
 * **闸拦下了重复提交**）。
 *
 * ⚠️ 口径见模块头注释：**执行成功之前**的两类失败（layui 加载失败 / 两发请求失败）一律
 * 「报一句 + `false`」（⛔ 不 reject）；**执行成功之后**的 `onDone()` / `successMessage()` / `msg()`
 * 各自被**内层 `try/catch`** 护住——抛了只 `logError` 记一笔，仍返回 `true`（`onDone()` 抛不挡
 * 成功提示；`successMessage()` 抛则那一步不 `msg`）。仍能以 rejection 结束的那条路是
 * **报告通道本身抛**：注入的 `options.msg` **或**缺省时的 `islandMsg → layer.msg`
 * （见模块头注释）。
 *
 * ⭐ T2：闸的**检查与置位是同步的**（`runBatchFlow` 的第一个 await 之前），否则两次同 tick 的
 * 点击会同时通过。置位之后无论内层怎么结束（成功 / 失败 / 取消 / 抛错）都由 `finally` 复位。
 */
export async function runBatchFlow(options: BatchFlowOptions): Promise<boolean> {
  // v2 按页签各自锁住提交，保留隔离页自己的防重闸。
  if (options.confirm && options.confirmation) return runBatchFlowOnce(options)
  // ⛔ 在飞期间第二次调用：零请求、零消息、返回 false，且**不碰第一发的 loading**。
  if (inFlight) return false
  inFlight = true
  try {
    return await runBatchFlowOnce(options)
  } finally {
    inFlight = false
  }
}

/** `runBatchFlow` 的本体（闸已在外面置位）。 */
async function runBatchFlowOnce(options: BatchFlowOptions): Promise<boolean> {
  const { orderNos, action, payload, applyPayload, successMessage, onDone, confirmation, confirm } = options

  let layer: MessageLayer | undefined
  if (!confirm || !confirmation) {
    try {
      layer = (await loadLayui(['layer'])).layer as unknown as MessageLayer
    } catch (e) {
      const report = options.msg ?? logError
      report(errText(e))
      return false
    }
  }

  const msg = options.msg ?? (layer ? (text: string) => islandMsg(layer as unknown as LayuiModule, text) : logError)

  // ⭐ T1：loading 的「开 / 关」**收敛到两个闭包**（⛔ 不逐个出口手写 `layer.close`）。
  // `loadIndex === null` 同时表达「当前没有在转的 loading」⇒ `closeLoading()` 天然幂等，
  // 重复调也不会误关别人（例如闸拦下的第二发）。
  let loadIndex: number | null = null
  const openLoading = (): void => {
    loadIndex = layer?.load(1, { shade: false }) ?? null
  }
  const closeLoading = (): void => {
    if (loadIndex === null) return
    const index = loadIndex
    loadIndex = null
    layer?.close(index)
  }

  // ⛔ `try` 只圈「执行成功之前」的调用（校验发 + 确认层 + 执行发）：这三步里的任何失败
  //    都意味着**这一批没有执行**，报后端 `msg` 原文（含整批回滚那句）+ 返回 false 是对的。
  // ⭐ T1：`finally` 是**所有出口**的关 loading 点（成功 / dry_run 抛 / write 抛 / 全不合格 /
  //    部分不合格取消 都经过它）——这是「让漏关不可能」的机械落实。
  let applied: BatchOutcome
  try {
    const ensureSelectionCurrent = (): void => {
      if (confirmation && !confirmation.isSelectionCurrent()) {
        throw new Error('订单列表或勾选已变化，请重新勾选后再操作')
      }
    }
    ensureSelectionCurrent()
    openLoading()
    const confirmEffects = !!confirmation && (action === 'price' || action === 'delete' || action === 'delete-v2')
    const dry = await batchOrderAction(action, {
      ...payload,
      ...(confirmEffects ? { confirmed_effects: [] } : {}),
      order_nos: orderNos,
      dry_run: true,
    })
    const previewEffects = confirmEffects ? readBatchEffects(dry.effects) : []
    const effects = dry.eligible.length > 0 ? previewEffects : []
    if (confirmation && dry.eligible.some((no) => !orderNos.includes(no))) {
      throw new Error('批量检查返回了未选中的订单，请重新操作')
    }
    ensureSelectionCurrent()

    if (confirmation || dry.ineligible.length > 0 || dry.eligible.length === 0) {
      closeLoading()
      const preview = confirmation ? batchPreview(confirmation, dry.eligible, dry.ineligible, effects) : null
      const count = dry.eligible.length
      const total = new Set([...dry.eligible, ...effects.map((effect) => effect.order_no)]).size
      const confirmText = confirmation ? `确认处理 ${total} 笔` : `继续处理其余 ${count} 笔`
      const go = layer
        ? await confirmList(
          layer,
          preview ? confirmationHtml(preview) : ineligibleListHtml(dry.ineligible),
          count === 0 ? ['知道了'] : [confirmText, '取消'],
        )
        : preview !== null && confirm !== undefined && await confirm(preview)
      if (count === 0 || !go) return false
      ensureSelectionCurrent()
      openLoading()
    }

    applied = await batchOrderAction(action, {
      ...(applyPayload?.(dry.eligible) ?? payload),
      ...(confirmEffects ? { confirmed_effects: effects } : {}),
      // ⛔ 只能送上一发的 `eligible`（spec §D4 / Ruling 11）。
      order_nos: dry.eligible,
      dry_run: false,
    })
  } catch (e) {
    // 失败：只弹后端 `msg` 原文（含整批回滚那句「本批未做任何修改」），⛔ **不调 `onDone`**。
    msg(errText(e))
    return false
  } finally {
    closeLoading()
  }

  // ⚠️ **到这里那一批已经执行成功** ⇒ 接下来无论谁抛，都**不能**改口说「没执行」。
  //
  // ⛔ `onDone()` 与 `msg(successMessage(...))` 都留在上面那个 `try` **之外**（那一支是「未执行」
  // 的失败口径），但各自加一层**内层 `try/catch`**：抛了只 `logError` 记一笔 ⛔ 不吞成失败。
  // 理由（裁定「执行成功就该报成功」）：`onDone()` 同步抛（例如 `reload()` 碰到 null 节点）时，
  // 那一批**在后端已经落库**——若让它以 rejection 结束，成功提示不会弹、调用方也看不出成败。
  //
  // 顺序仍是 **先 `onDone()` 再 `msg`**（`reload()` 里可能弹筛选警告，`layer.msg` 后来的会覆盖
  // 先前的；反了用户只看得到警告——同页既有实测口径）⇒ 内层两段，⛔ 顺序不变。
  try {
    onDone()
  } catch (e) {
    logError(errText(e))
  }

  // `successMessage(...)` 也挡在这里（同一条思路）：它自身抛不该以 rejection 逃出 `Promise<boolean>`。
  try {
    msg(successMessage(applied.applied.length))
  } catch (e) {
    logError(errText(e))
  }

  return true
}
