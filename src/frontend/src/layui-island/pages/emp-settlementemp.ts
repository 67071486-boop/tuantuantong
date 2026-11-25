/**
 * 【清算绩效】弹层子页的 layui island 复刻（对标 `/Emp/SettlementEmp?ids=<逗号串>`，2026-10-03 ）。
 *
 * 父页是「打手与客服报酬」(`pages/emp-emplist.ts`)：勾选若干绩效行 ⇒ 工具栏 `settBtn`（或
 * 「结账状态」列那颗 `.setSett`）用 `layer.open({type:2, content:'…?page=emp-settlementemp&ids=…'})`
 * 打开**本页**（父页那侧见 ；对标触发点 `emplist.js` 的 `settSta()`）。
 *
 * ## 证据（**只读**）
 *
 * | 件 | 位置 | 用途 |
 * |---|---|---|
 * | 服务端 HTML | `public/replica/emp-settlementemp.html`（已提交；含 `ids=0` 的零态，**无 PII**） | 结构 / 类名 / 内联 style / 视觉值 |
 * | 页面 JS | `.tools/recon/settlementemp/js/JS2019__Emp__settlementemp.js`（97 行，**不进 git**） | 交互（radio 显隐 / 提交 / QR 渲染 / 大图） |
 *
 * 四格与 QR 的**取值口径**（对标实测见计划 §1.2）：笔数 = 命中行数、预计要结总额 = `Σ temp_amount`、
 * 实际需结总额 = `Σ payable_net`（⚠️ 有意偏离对标读数一处，见 `@/api/settlements` 的
 * `SettlementPreview` 注释）、结算对象 = 按人聚合的 `名字（金额）`。
 *
 * ## 结构：逐块对照证据件（行号 = `public/replica/emp-settlementemp.html`）
 *
 * | 证据件 | 行 | 本文件 |
 * |---|---|---|
 * | `<style> body{background-color:#fff} …` | L24-41 | `PAGE_STYLE`（⚠️ 只搬 `body` 那一条 + 本页的 `.qrCodeBox` 两条；`.layadmin-pagetabs` 是**外壳页签**的样式，不属于子页内容） |
 * | `<style> .qrCodeBox img{…} .qrCodeBox{…}` | L51-66 | `PAGE_STYLE`（值逐字，含 `display :flex` 那个空格） |
 * | `<form class="layui-form layui-form-pane" …>` | L67 | `settlementEmpFormHtml` |
 * | `<blockquote class="layui-elem-quote">` 提示语 | L68-70 | 同函数（**逐字**，含「注意：」那半句） |
 * | 内层 `.layui-form-pane` 的 `margin-*` | L71 | 同函数 |
 * | 5 组 `.layui-form-item`（所选订单笔数 / 预计要结总额 / 实际需结总额 / 结算对象 / 结算状态） | L72-102 | 同函数；四格 `color:#ff0000`、后三格另加 `font-weight:600`（L75/81/87/93） |
 * | 两个 `.expdiv`（`style="display:none"`）：同步到账本 / 微信收款码 | L103-116 | 同函数 + `EXPAND_CLASS` / `EXPAND_SELECTOR` |
 * | 居中的 `确定结算或修改`（`lay-submit="" lay-filter="savebtn"`，class 里 `layui-btn` 与 `layui-btn-normal` 之间**两个空格**） | L119-121 | 同函数 |
 * | 隐藏域 `#empIds name="empIds"` | L123 | 同函数（值由 query 的 `ids` 决定，见下） |
 * | QR 一项的标记（`.qrCodeItem` / `img.qrCodeImg[data-index] height="72"` / 两行名字） | 页面 JS L38-44 | `qrCodesMarkup` |
 *
 * ## 交互：逐条对照页面 JS（行号 = `JS2019__Emp__settlementemp.js`）
 *
 * | 对标 | 行 | 本文件 |
 * |---|---|---|
 * | `form.on('radio(sta)')`：`value == "Y"` ⇒ `$('.expdiv').show()`，否则 `.hide()` | L17-23 | `form.on('radio(sta)')` + `setExpandVisible` |
 * | 载入时取 QR 列表并渲染 `.qrCodeBox` | L24-51 | 一次 `fetchSettlementPreview(ids)` 填四格 + `qrCodesMarkup` |
 * | 点 `.qrCodeImg` ⇒ `layer.photos({photos:{title:'微信付款码', start, data}})` | L52-60 | 根容器上的点击委托 + `layer.photos`（`QR_PHOTOS_TITLE`） |
 * | `form.on('submit(savebtn)')`：`sta` 空 ⇒ `layer.msg('请选择要修改的清算状态', {time:1500})` 且 `return false` | L61-69 | 同（`MESSAGES.noStatus`，**零请求**） |
 * | 提交前用 `re` 布尔挡连点 | L70-72 | `inflight`（同名同义；失败后复位，可重试） |
 * | 成功 ⇒ `layer.msg(msg, {time:1000}, () => parent.tools.reloadOrderData())` | L82-87 | `showResultAndReload` + `reloadParentList` |
 *
 * ## 与对标**有意不同**的地方（每条都有理由，不是漏做）
 *
 * 1. **数据来自我们的 REST**：对标四格与「结算对象」是**服务端渲染**、QR 另走
 * `/EmpAjax/GetEmpQrCode`；island 没有服务端渲染 ⇒ 一次 `GET /api/business/settlements/preview`
 * 把两者一起取回（契约见计划 §3 T2.1）。取数失败时**保留证据件的零态**（`0笔 / 0元 / 0元 / 空`）
 * 并 `layer.msg` 后端文案，⛔ 不伪造数字。
 * 2. **`empIds` 的值由 `?ids=` 填**（对标是服务端写进去的），提交时按它发请求——与对标读同一个
 * 隐藏域（JS L63）同形。非法段丢掉（不产生 `NaN` 拼进请求，E-0010 同型）。
 * 3. **成功后刷新父页列表**：对标调 `parent.tools.reloadOrderData()`（JS L85）——⚠️ **读的是父窗口
 * （`window.parent`），⛔ 不是本页自己的 `window`**：那个 `tools` 挂在**列表页自己的 window** 上
 * （`emp-emplist.ts` 的 `toolsHost = window`，约 `:1836`），而本页是父页 `layer.open({type:2})`
 * 开的**独立 iframe 文档** ⇒ 本页的 `window.tools` **恒 `undefined`**（运行期实测：子页 `window`
 * 上没有 `tools`、`window.parent` 上有）。⛔ **不要把这里改回 `window`**——那正是真机缺陷
 * 「结算成功、库里那行确实变『已结账』，但弹层不关、父页列表一行不刷、用户眼前仍是『未结账』」
 * 的根因（那一版每次都掉进下面的兜底）。⚠️ 这条**在 jsdom 里测不出来**（`window.parent === window`）
 * ⇒ 单测必须显式把 `parent` 换成假窗口才区分得开（见 `__tests__/emp-settlementemp.spec.ts`）。
 * 容错：父窗口取不到 / 跨域读属性抛 / 父页没有这个函数 ⇒ 退回 `layer.closeAll()`（可见行为：
 * 关掉**本页文档**里的层），⛔ 不抛。**兜底的观感后果（如实登记）**：子页 iframe 里的 `layer`
 * 只在**子页文档**里找层 ⇒ 它**关不掉承载本页的那个 `type:2` 弹层**（那属于父页文档），
 * 用户要自己关层、再点父页的「刷新列表」——这正是「兜底不是等价实现」。
 * 4. **成功文案按后端**：N = `settled_performances` / `revoked_performances` 的 `.length`，
 * M = `skipped_performances` 的 `.length`，A = `settled_adjustments` /
 * `revoked_adjustments` 的 `.length`（**三个都是 id 列表、不是条数**，E-0060 同族）；
 * `N > 0` 报「已结账 N 条（其中 M 条本来…已跳过）」；`N === 0` 且 **`A > 0`** ⇒ 报
 * 「已结清调整记录 A 条」——那是「已结账绩效 + 后来追加的未结账调整记录」走**坏账出口**
 * 的那一轮：**钱动了**（`settled_adjustments` 非空）、`settled_performances` 却是空的，
 * 只有 `N === 0 && A === 0` 才是真的「所选记录已是该状态，无需修改」（业务方 2026-10-03 Q3=A）。
 * 调整记录被结掉、绩效行那一轮为空时，不能再说「无需修改」。
 * 5. **`.expdiv` 的显隐用行内 `style.display`**（对标用 jQuery 的 `.show()/.hide()`）：`''` 与
 * `'none'` 是同一语义的等价落点（元素的 `display` 由样式表决定，div 落到 `block`）。
 *
 * ## 出口安全（存储型 XSS 汇点，逐条登记）

本页有**两处**把租户数据拼成 HTML 的出口，两处都必须转义（`layui-island/escape.ts` 是唯一实现）：

| # | 出口 | 数据从哪来 | 落点 / 谁拼 HTML | 处置 |
|---|---|---|---|---|
| 1 | `qrCodesMarkup(...)` → `.qrCodeBox` 的 `innerHTML` | `SettlementPreview.qr_codes` 的 `username` / `name` / `url` | **我们自己**拼的 `<img … src="…">` / 两行名字 | 三个字段**全部**过 `escapeHtml`（2026-10-03 起） |
| 2 | `layer.photos({photos:{data:[{alt, src}]}})` | 同一批 `qr_codes` 的 `username`（`alt`）与 `url`（`src`） | **vendored layui 自己**把它拼进 `<img src="…" alt="…">`（`public/layui/layui.js` 的 layer 模块，读码逐字核过） | 两个字段**都**过 `escapeHtml`（⛔ 本轮之前是裸值：一条 Important 的存储型 XSS，已修） |

⚠️ 第 2 条**不是**第 1 条的重复：第 1 条是我方拼字符串（走 `innerHTML`），第 2 条把值交给
**第三方库**去拼属性——只在第 1 条转义会漏掉它。判据是
`pages/__tests__/emp-settlementemp.spec.ts` 的两条「喂 `x" onerror="alert(1)` 形态、
断言属性里没有裸引号」的用例（一条打 `qrCodesMarkup`、一条打 `layer.photos` 的实参）。

## 已知后果：完成单事后改价 / 退款时，「预计」会先动、「生效」不动（**以实际需结为准**）

> ⛔ **本节对「改价」那一半已过期**（真机实测：事后改价之后两格**相等**；
> 只有**部分退款**那一半仍成立）。按实现改准的完整版见本节**末尾的「⛔ 更正块」**；
> 下面整段原文**一字未删**，按 `AGENTS.md` §4 保留。

**一句话**：一张单**完成之后**再改发单价或发生退款时，弹窗上「预计要结总额」跟着变，
而「实际需结总额」（生效绩效）**停在完成那一刻**——两格不再相等。这不是 bug，是业务方
2026-10-03 Q1 定的「**完成时把同一个数冻结成生效**」的直接后果（冻结就意味着生效不跟着动）。

**怎么复现**（三步，全在「打手与客服报酬」页那一单上）：

1. 订单状态 → **已完成**：此刻打手行的 `temp_amount` 与 `bonus_amount` 被写成**同一个数**
 （`_activate_taker_performance` / `settlement_hooks.on_order_completed`）⇒ 弹窗两格相等。
2. 再点 `/hall-price/`（改发单价）或对这张单**退款**：`recompute_order_amounts`（或
 `refunds.on_order_refunded`）会把 MAIN 行/客服行的 **`temp_amount` 刷成新值**，
 而 `bonus_amount`（生效）**一个字节都不动**（那正是「冻结」）。
3. 回到本弹窗勾这一行：`预计要结总额` = `Σ temp_amount` 已经是新值，`实际需结总额` =
 `Σ payable_net` 还是旧值 ⇒ **预计 ≠ 生效**。

**以哪个为准**：**以「实际需结总额」为准**——`/settlements/settle` 结的就是 `payable_net`
（`bonus_amount − 扣点 + Σ未结账调整记录`），结算快照 `settled_amount` 记的也是它。
「预计」只是「此刻若完成会拿多少」的估算，不会自动追上已生效的旧值。
⚠️ 若业务方要的是「事后改价也把生效一起改」，那要先变更 Q1 的「冻结」裁定——⛔ 本轮**不改行为**
（`AGENTS.md` §5-C：不确定的事不自行拍板）。

### ⛔ 更正块（2026-10-04，按 `AGENTS.md` §5-C 按实现改准；上面原文一字未删）

上面整段把**改价**与**退款**混成了一件事。实测（真机 + 读码）两情形**已经分叉**，
逐条改准如下：

- **完成单事后改价（`/hall-price/`）⇒ 两格已经相等**（真机实测读数 `32.00 / 32.00`）。
 读码链：`order_writes.change_hall_price` ⇒ `performance.recompute_order_amounts`
 在订单是 `COMPLETED` 时把 MAIN 的 `temp_amount` 与 `bonus_amount` **一起**写成同一个新值；
 主挂点随即 `settlement_hooks.refresh_customer_estimate` 把客服行两列也
 **各按自己的减项一起**重算（预计减打手预计、生效减打手生效，`D-0187` 的 ②）
 —— MAIN 两值同值 ⇒ 客服两列同值 ⇒ 弹窗 `预计要结总额 = Σ temp_amount` 与
 `实际需结总额 = Σ payable_net` **相等**（无扣点、无未结账调整时）。
 ⇒ 原文「`bonus_amount`（生效）**一个字节都不动**」**对改价这条路径已不成立**。
- **部分退款 ⇒ 两格分歧仍然成立**（真机实测：`实际需结总额 = Σ payable_net` 里含那条
 **未结账**的 `PARTIAL_REFUND` 冲减链 `−20.00`）。读码链：
 `refunds._apply_sales_deductions` 写一条 `PARTIAL_REFUND`、`amount = −X`、`settle_status=未结账`；
 客服行的 `temp_amount` 与 `bonus_amount` 被重算成同一个数，但
 `payable_net = bonus_amount − 扣点 + Σ未结账调整记录` **额外减掉那条冲减链**
 ⇒ 两格差 = 该链（＋扣点，若有）。**「以实际需结总额为准」这句一个字不放宽**——
 `/settlements/settle` 结的就是 `payable_net`，结算快照 `settled_amount` 记的也是它。
- **顺带一条同族的**：**退单（→ `REFUNDED`）** 之后，未结账的客服行两列都被清 `0.00`、
 退款冲减链也归 `0.00`（`D-0187` 的 Q1/Q2）⇒ 两格**一起**是 `0.00`、不再分歧。

## 三条 island 硬规矩（`AGENTS.md` §5-G）
 *
 * ① 类名（`EXPAND_CLASS`）与选择器（`EXPAND_SELECTOR`）分两个常量；② 以 `(` 开头的语句前置分号；
 * ③ 先 `await bootstrapIsland(...)` 挂样式、再塞标记，而 `initIslandElements(layui)` 留在塞标记之后
 * （兜底网：`layui-island/__tests__/page-mount-order.spec.ts`）。
 */
import {
  fetchSettlementPreview,
  revokeSettlements,
  settlePerformances,
  type SettlementPreview,
  type SettlementPreviewOwner,
  type SettlementPreviewQrCode,
} from '@/api/settlements'
import { escapeHtml } from '@/layui-island/escape'
import { bootstrapIsland, initIslandElements, type IslandContext, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

/**
 * island 页 key ＝ **文件名**（`layui-island/page.ts` 用 `import.meta.glob('./pages/*.ts')`
 * 按文件名注册）。 的父页拼的 URL（`page=emp-settlementemp&ids=…`）必须与它逐字一致
 * ——`config/__tests__/replicaRoutes.spec.ts` 把本常量收进 `LAYER_ONLY_PAGE_KEYS`（它没有对标菜单项）。
 */
export const PAGE_KEY = 'emp-settlementemp'

/** 页标题：对标 `emp-settlementemp.html` L5 的 `<title>清算绩效</title>`（**服务端渲染**的那个）。 */
export const PAGE_TITLE = '清算绩效'

/** 两个「展开区」的**类名**（拼标记用）。 */
const EXPAND_CLASS = 'expdiv'

/**
 * 同一个东西的**选择器**（查询用）。
 * ⚠️ 与上面的类名**必须是两个常量**：`class="${EXPAND_SELECTOR}"` 会造出 `class=".expdiv"`
 * ——永远匹配不到，而 `typecheck` / `lint` 都看不见（E-0061 / AGENTS §5-G 陷阱①）。
 */
const EXPAND_SELECTOR = `.${EXPAND_CLASS}`

/**
 * 几句用户可见文案：带「对标行号」的三句是**逐字**照抄，带「我方」的两句是本轮新增语义。
 * 抽成常量只为让 spec 能钉住字面值（改一个字就红）。
 */
export const MESSAGES = {
  /** 页面 JS L66 逐字：没选清算状态时的提示。 */
  noStatus: '请选择要修改的清算状态',
  /**
 * 业务方 2026-10-03 Q4=A：「同步到账本」对着的账本功能**我们还没有后端** ⇒ 提示「尚未开放」，
 * 但**不阻断**本次结算（照 §5「无后端的部分照对标渲染 + 点了提示尚未开放，不置灰」）。
 */
  syncNotOpen: '「同步到账本」尚未开放，本次只改结算状态',
  /** 业务方 2026-10-03 Q3=A：全批都是目标状态时的标题（`N === 0` 那一支）。 */
  alreadySameStatus: '所选记录已是该状态，无需修改',
  /**
 * 撤销结账的「原因」为空时的提示。出处就是本常量，不从别的页面 import
 * （页面模块互相 import 会把两张页面的加载图耦在一起）。
 * 走 `/settlements/revoke` 时 `reason` 非空，由后端写进订单日志。
 */
  reasonRequired: '请填写撤销结账的原因（会写进订单日志）',
  /** 我方兜底：请求本身失败时（后端 msg 取不到）的提示。 */
  failed: '操作失败',
} as const

/**
 * 撤销原因层的标题。字面值只由本常量钉住（页测试断言这个字符串），不从别的页面 import。
 */
export const REASON_PROMPT_TITLE = '仅修改结算状态'

/** 对标页面 JS L55 逐字：QR 大图的 `photos.title`。 */
export const QR_PHOTOS_TITLE = '微信付款码'

/**
 * 页内 `<style>`：**只搬证据件里属于本子页内容的那三条**（值逐字，含原文的空白与
 * `.qrCodeBox` 里 `display :flex;` 那个冒号前的空格）。
 *
 * ⚠️ 证据件 head 里与 `body` 同块的 `.layadmin-pagetabs{…}`（L28-40）是**外壳页签**的样式，
 * 不属于本页内容（island 的外壳自己管那一条）⇒ 不搬。
 *
 * ⚠️ `body{background-color:#fff}` 同时也是 `bootstrapIsland` 的全局覆盖里的一条；
 * 这里仍然照抄——判据是「页内 `<style>` 在文档顺序上晚于全局覆盖」（同 `system-setgames.ts`）。
 */
const PAGE_STYLE = `<style>
    body {
        background-color: #fff
    }
    .qrCodeBox img{
        width:98px;
        height:98px;
        margin-right:10px;
    }
    .qrCodeBox{
        display :flex;
        text-align:center;
        flex-wrap: wrap;
        color: red;
        font-size: 14px;
        line-height: 20px;
    }
</style>`

/** 四格的标签文案（**逐字**取自证据件 L73/79/85/91 的 `.layui-form-label`）。 */
export const CELL_LABELS = {
  count: '所选订单笔数',
  tempTotal: '预计要结总额',
  bonusTotal: '实际需结总额',
  owners: '结算对象',
} as const

/** 四格里 `<span>` 的公共内联 style 前缀（证据件 L75/81/87/93 的原文）。 */
const CELL_SPAN_STYLE = 'margin-left:10px;line-height:36px;color:#ff0000'

/**
 * 从 `window.location.search` 取 `ids`（island 的 `IslandContext` **不带** query 参数，
 * 与 `system-setgames.ts` 取 `uid` 同法，见计划 §4）。
 *
 * 多行时对标传的是**逗号串**（`/Emp/SettlementEmp?ids=126508,126394`）。非法段**丢掉**而不是
 * 变成 `NaN`：`Number('x')` 会产出 `NaN`，拼进 `performance_ids` 就是一次无意义的请求
 * （E-0010 同型：请求来源的值进类型化的位置之前一律先过判据）。
 */
export function parsePerformanceIds(raw: string | null): number[] {
  return (raw ?? '')
    .split(',')
    .map((s) => s.trim())
    .filter((s) => /^\d+$/.test(s))
    .map(Number)
}

/** 金额显示：两位小数（后端给的就是两位小数字符串；非数字回落 `0.00`，⛔ 不显示 `NaN`）。 */
export function formatMoney(value: string | number): string {
  const n = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(n) ? n.toFixed(2) : '0.00'
}

/** 笔数格：`0笔` / `2笔`（证据件 L75 的 `0笔`；实测读数见计划 §1.2 的 `2笔`）。 */
export function countText(count: number): string {
  return `${count}笔`
}

/**
 * 金额格：`count > 0` ⇒ `X.XX元`；`count === 0` ⇒ **`0元`**（证据件 L81/87 就是这么写的
 * ——**不是** `0.00元`；判据取的是**笔数**，不是金额本身，见计划 §4 的视觉值口径）。
 */
export function moneyText(count: number, total: string | number): string {
  return count > 0 ? `${formatMoney(total)}元` : '0元'
}

/**
 * 结算对象格：按人聚合的 `名字（金额）`，多人用**全角逗号**「，」隔开（对标实测形态：
 * `kk（0.00）`、`甲（4.00），乙（1.60）`；见计划 §1.2）。
 *
 * ⚠️ 返回的是**纯文本**（调用方用 `textContent` 落笔）⇒ 这里**不做** HTML 转义；
 * 名字是用户可控值，走 `innerHTML` 才需要转义（`qrCodesMarkup` 是那种）。
 * 归属人为空（后端那条待决口径）时名字位留空，金额照旧——不伪造成 0 行。
 *
 * ⭐ 2026-10-04（「显示人统一成昵称」线）：名字位取**显示名** `o.name`，并**回落**老键
 * `o.username`——防部署窗口（前端先上、后端还没上时 `name` 真的缺席），后端把两项都置
 * `null` 的「无归属人」那一项也靠最后那个 `?? ''` 落到与改动前逐字相同的空名字位。
 */
export function ownersText(owners: readonly SettlementPreviewOwner[]): string {
  return owners
    .map((o) => `${o.name ?? o.username ?? ''}（${formatMoney(o.amount)}）`)
    .join('，')
}

/**
 * `.qrCodeBox` 的内容——**逐字**照对标页面 JS L38-44 的模板（含 `height="72"` 与 `src` 前
 * **两个空格**、`data-index` 从 0 起）。
 *
 * ⚠️ 三个字段都来自租户数据（用户名 / 姓名 / 签名 URL）⇒ 一律过 `escapeHtml`
 * （存储型 XSS 的入口，见 `layui-island/escape.ts`）。`.qrCodeName` 为空时模板里就是空串
 * （对标是 `item.name ? item.name : ''`——同一结果）。
 */
export function qrCodesMarkup(qrCodes: readonly SettlementPreviewQrCode[]): string {
  return qrCodes
    .map(
      (item, index) => `
                        <div class="qrCodeItem">
                            <img data-index="${index}" class="qrCodeImg" height="72"  src="${escapeHtml(item.url)}" >
                            <div class="qrCodeUserName">${escapeHtml(item.username ?? '')}</div>
                            <div class="qrCodeName">${escapeHtml(item.name)}</div>
                        </div>
                    `,
    )
    .join('')
}

/**
 * 整页标记。**逐块**照证据件 L51-124（结构 / 类名 / 内联 style / 属性顺序 / 文案）。
 *
 * `ids` 进隐藏域 `#empIds`（对标由服务端写；提交时读的是同一个域，见文件头第 2 条）。
 *
 * ⚠️ **中间那两个金额格的口径不同（`M-3`）**：`预计要结总额` = `Σ temp_amount`，
 * `实际需结总额` = `Σ payable_net`（`bonus_amount − 扣点 + Σ未结账调整记录`）
 * ⇒ 两格在**两种情形**下不同：
 * ① **完成单事后改价**（`/hall-price/`）⇒ 两格**一起动、且相等**（MAIN 与客服行的
 * `temp` / `bonus` 都按新金额一起重算，`D-0187`）；
 * ② **部分退款** ⇒ `实际需结总额` 还要减掉那条**未结账**的 `PARTIAL_REFUND` 冲减链
 * （真机实测 `−20.00`）⇒ 两格**分歧**，以**「实际需结总额」为准**。
 */
export function settlementEmpFormHtml(ids: readonly number[]): string {
  return `${PAGE_STYLE}
<form class="layui-form layui-form-pane" action="" style="margin-left:10px;margin-right:10px;padding-bottom:20px">
    <blockquote class="layui-elem-quote">
        你正在进行给代练打手或销售人员进行账单结算或修改结算状态，请注意所勾选的绩效记录是否有误！注意：订单状态必须在待验收、已完成、已退单等时才可以改成已结算
    </blockquote>
    <div class="layui-form-pane" style="margin-top:10px;margin-right:10px;margin-bottom:10px">
        <div class="layui-form-item">
            <label class="layui-form-label">${CELL_LABELS.count}</label>
            <div class="layui-input-block">
                <span style="${CELL_SPAN_STYLE}">0笔</span>
            </div>
        </div>
        <div class="layui-form-item">
            <label class="layui-form-label">${CELL_LABELS.tempTotal}</label>
            <div class="layui-input-block">
                <span style="${CELL_SPAN_STYLE};font-weight:600">0元</span>
            </div>
        </div>
        <div class="layui-form-item">
            <label class="layui-form-label">${CELL_LABELS.bonusTotal}</label>
            <div class="layui-input-block">
                <span style="${CELL_SPAN_STYLE};font-weight:600">0元</span>
            </div>
        </div>
        <div class="layui-form-item">
            <label class="layui-form-label">${CELL_LABELS.owners}</label>
            <div class="layui-input-block">
                <span style="${CELL_SPAN_STYLE};font-weight:600"></span>
            </div>
        </div>
        <div class="layui-form-item">
            <label class="layui-form-label">结算状态</label>
            <div class="layui-input-block">
                <input type="radio" name="sta" value="N" title="未结算" lay-filter="sta">
                <input type="radio" name="sta" value="Y" title="已结算" lay-filter="sta">
            </div>
        </div>
        <div class="layui-form-item ${EXPAND_CLASS}" style="display:none">
            <label class="layui-form-label">同步到账本</label>
            <div class="layui-input-block">
                <input type="radio" name="syncexp" value="N" title="不需要" checked="checked">
                <input type="radio" name="syncexp" value="Y" title="需要（将会在财务运营/日常账本里新增一笔支出款的记录）">
            </div>

        </div>
        <div class="layui-form-item ${EXPAND_CLASS}" style="display:none">
            <label class="layui-form-label">微信收款码</label>
            <div class="layui-input-block">
                <div class="qrCodeBox"></div>
            </div>
        </div>
    </div>

    <div style="text-align:center;align-items:center">
        <button type="button" class="layui-btn  layui-btn-normal w-lg" lay-submit="" lay-filter="savebtn">确定结算或修改</button>
    </div>

    <input type="hidden" id="empIds" name="empIds" value="${ids.join(',')}" />
</form>`
}

/* ────────────────────────── 结果文案（Q3=A 如实报数）────────────────────────── */

/**
 * 「已结账」方向的文案。**三种形态**，判据只看两个 id 列表的长度：
 *
 * | `settled` | `adjustments` | 文案 |
 * |---|---|---|
 * | `> 0` | 任意 | `已结账 N 条`（`skipped > 0` 时补「（其中 M 条本来就是已结账，已跳过）」） |
 * | `0` | `> 0` | `已结清调整记录 A 条`（同上补 skipped）——**钱动了**，⛔ 不能说「无需修改」 |
 * | `0` | `0` | `所选记录已是该状态，无需修改` |
 *
 * ⚠️ 第二形态是「已结账绩效 + 后来追加的未结账调整记录」那一轮（后端走**坏账出口**）：
 * 该轮 `settled_performances` 为空、`settled_adjustments` 非空，旧实现命中 `N === 0`
 * ⇒ 弹「无需修改」而**钱其实被结掉了**（修复轮 I-2 如实登记的坑）。第三参数默认为 `0`
 * 只为不破坏既有调用形状，生产调用点**必须**传 `settled_adjustments.length`。
 */
export function settleResultText(
  settled: number,
  skipped: number,
  adjustments = 0,
): string {
  if (settled === 0 && adjustments === 0) return MESSAGES.alreadySameStatus
  const head =
    settled > 0 ? `已结账 ${settled} 条` : `已结清调整记录 ${adjustments} 条`
  return skipped > 0
    ? `${head}（其中 ${skipped} 条本来就是已结账，已跳过）`
    : head
}

/**
 * 「已撤销结账」方向：与上一条**同三形态**、换个词（Q3=A）。
 *
 * ⚠️ **可达性如实登记**：后端 `revoke_settlements` 的 `revoked_adjustments` **只**从
 * 命中的已结账绩效行派生（`pairs` 由 `revoked_rows` 构造）⇒ 今天**不存在**
 * 「`revoked_performances` 为空、`revoked_adjustments` 非空」那一轮。第三形态在这里是
 * **与结账方向对称的形状**（两个端点的返回体同形，文案函数不该长得不一样），
 * ⛔ 不要读成「撤销侧也观测到过这种轮次」。
 */
export function revokeResultText(
  revoked: number,
  skipped: number,
  adjustments = 0,
): string {
  if (revoked === 0 && adjustments === 0) return MESSAGES.alreadySameStatus
  const head =
    revoked > 0
      ? `已撤销结账 ${revoked} 条`
      : `已撤销结账的调整记录 ${adjustments} 条`
  return skipped > 0
    ? `${head}（其中 ${skipped} 条本来就是未结账，已跳过）`
    : head
}

/* ────────────────────────── 成功后刷新父页 ────────────────────────── */

/** 父窗口上那个自定义工具的最小形状（对标 `emplist.js:1965-1977` 的 `window.tools`）。 */
export type ParentToolsWindow = { tools?: { reloadOrderData?: () => void } }

/**
 * 取**父窗口**（`window.parent`）。
 *
 * ## 为什么必须是 `window.parent`（承载事实，⛔ 改回 `window` = 真机缺陷复发）
 *
 * 那个 `tools` 槽位挂在**列表页自己的 window** 上（`emp-emplist.ts` 的 `toolsHost = window`，约
 * `:1836`）；而本页是父页 `layer.open({type:2})` 开的**独立 iframe 文档** ⇒ 本页的 `window.tools`
 * **恒 `undefined`**（运行期实测：子页 `window` 上没有 `tools`、`window.parent` 上有）。父页把本页
 * 当 `parent`，所以本页必须往 `window.parent` 上读（同源，实测可读）。
 *
 * ⛔ **本函数（而不是在调用点上直接写 `window.parent`）的存在理由就是这条测试盲区**：jsdom 里
 * `window.parent === window` ⇒「读 `window`」与「读 `window.parent`」两条实现的读数**完全一样**，
 * 本页的真机缺陷正是这样漏过单测的。单测必须显式换掉 `parent`（`Object.defineProperty`），
 * 判据见 `pages/__tests__/emp-settlementemp.spec.ts` 的「成功后由提示的回调刷新父页」。
 *
 * 容错：父窗口取不到（被沙箱挡住 / 压根没有）⇒ 返回 `undefined`，⛔ 不抛；调用方照旧退回
 * `layer.closeAll()` 兜底（兜底的观感后果见 `reloadParentList`）。
 */
export function parentToolsWindow(): ParentToolsWindow | undefined {
  try {
    return window.parent as unknown as ParentToolsWindow | undefined
  } catch {
    return undefined
  }
}

/**
 * 成功后刷新父页列表。判据顺序（**本页对父页容错**，见文件头第 3 条）：
 *
 * 1. 父窗口有 `tools.reloadOrderData`（`emp-emplist.ts` 挂的）⇒ **按对标的调法**调它
 * （`this` 绑到 `tools`，与 `parent.tools.reloadOrderData()` 同形，JS L85）；
 * 2. 没有 ⇒ 退回 `layer.closeAll()`（**可见**行为：关掉本页文档里还开着的层），⛔ 不抛
 * ——`typecheck` / `lint` 都看不见「父页还没挂工具」，而抛出去会让成功提示后**什么都看不到**。
 *
 * ⚠️ `win` 是**父窗口**（调用方传 `parentToolsWindow()` 的结果，⛔ 不是本页的 `window`）——
 * 理由见 `parentToolsWindow` 的 docstring。
 *
 * ⚠️ 兜底**不是等价实现**：子页 iframe 里的 `layer` 只在**子页文档**里找层，所以它关不掉承载本页
 * 的那个 `type:2` 弹层（那属于父页文档）——父页工具缺失时用户要自己关层、再点父页的「刷新列表」。
 * 取舍已登记在文件头第 3 条与报告里。
 */
export function reloadParentList(
  win: ParentToolsWindow | undefined,
  layer: LayuiModule,
): void {
  let tools: ParentToolsWindow['tools']
  try {
    // ⚠️ 跨域父窗口上**读属性**就会抛 `SecurityError`（本页与父页同源、实测可读；这里仍按
    // 「取不到」处理 ⇒ 与「父页没有 tools」同一条兜底，⛔ 不抛给调用方）。
    tools = win?.tools
  } catch {
    tools = undefined
  }
  const reload = tools?.reloadOrderData
  if (typeof reload === 'function') {
    reload.call(tools)
    return
  }
  ;(layer.closeAll as (() => void) | undefined)?.()
}

/** 找到某一格（按 `.layui-form-label` 的文案）里的值 `<span>`。 */
function cellSpan(root: HTMLElement, label: string): HTMLElement | null {
  for (const item of Array.from(root.querySelectorAll<HTMLElement>('.layui-form-item'))) {
    if (item.querySelector('.layui-form-label')?.textContent?.trim() !== label) continue
    return item.querySelector<HTMLElement>('.layui-input-block > span')
  }
  return null
}

/** 写一格的值（`textContent`——名字来自租户数据，走文本节点就不用转义）。 */
function setCell(root: HTMLElement, label: string, text: string): void {
  const span = cellSpan(root, label)
  if (span) span.textContent = text
}

/**
 * 挂载页面。返回卸载函数（切路由 / 关弹层时调用）。
 *
 * 挂载顺序：**先 `bootstrapIsland`（挂样式）→ 塞标记 → `initIslandElements` → `form.render()`
 * → 取数填格 → 绑事件**。后两步的顺序不能反：标记是运行期插进 DOM 的，layui 的 `form` 模块
 * 不会自己重扫（同仓先例 `system-setgames.ts`）。
 */
export async function mountSettlementEmpPage(
  root: HTMLElement,
  _ctx: IslandContext,
): Promise<() => void> {
  const layui: LayuiNamespace = await bootstrapIsland(['form', 'layer'])
  const form = layui.form
  const layer = layui.layer

  const msg = (text: string): void => {
    ;(layer.msg as (t: string, o?: unknown) => void)(text, { time: 1500 })
  }

  // `ids` 只能从 URL 读（`IslandContext` 只带 `onNavigate`）。
  const ids = parsePerformanceIds(new URLSearchParams(window.location.search).get('ids'))

  document.title = PAGE_TITLE
  // 对标 `<body style="font-size:12px;" id="iosiframe">`：`font-size` 由 `bootstrapIsland` 补在
  // body 的行内样式上，这里补 id；卸载时还原（同一文档可能被别的页复用）。
  const hadBodyId = document.body.hasAttribute('id')
  if (!hadBodyId) document.body.id = 'iosiframe'

  root.innerHTML = settlementEmpFormHtml(ids)
  initIslandElements(layui)
  // ⚠️ **不带类型**调 `form.render()`：传 `'radio'` / `'checkbox'` 时只渲染那一种，
  // 本页两种控件都有 ⇒ 另一种会停在被 CSS 藏起来的原生 input 上（点不了）。
  ;(form.render as (type?: string) => void)()

  // ── 取数：一次预览端点填四格 + QR（对标是服务端渲染 + `GetEmpQrCode`）──
  let qrPhotos: { alt: string; pid: number; src: string }[] = []

  /** 一次预览填四格 + QR（首拉唯一填充路）。 */
  const applyPreview = (data: SettlementPreview): void => {
    setCell(root, CELL_LABELS.count, countText(data.count))
    setCell(root, CELL_LABELS.tempTotal, moneyText(data.count, data.temp_total))
    setCell(root, CELL_LABELS.bonusTotal, moneyText(data.count, data.bonus_total))
    setCell(root, CELL_LABELS.owners, ownersText(data.owners))
    const box = root.querySelector<HTMLElement>('.qrCodeBox')
    if (box) box.innerHTML = qrCodesMarkup(data.qr_codes)
    // ⚠️ **两个字段都必须过 `escapeHtml`**：vendored layui 的 `layer.photos` 是把它们
    // **字符串拼进 HTML 属性**的（`public/layui/layui.js` 的 layer 模块——读码逐字核过）。
    // 用户名来自租户数据 ⇒ 存储型 XSS 的入口。
    qrPhotos = (data.qr_codes ?? []).map((q, index) => ({
      alt: escapeHtml(q.username ?? ''),
      pid: index,
      src: escapeHtml(q.url),
    }))
  }

  let preview: SettlementPreview | null = null
  try {
    preview = await fetchSettlementPreview(ids)
  } catch (e) {
    // 失败**保留证据件的零态**并如实报错：⛔ 不把「取不到」画成 `0.00元`（E-0010 同型）。
    msg(e instanceof Error ? e.message : MESSAGES.failed)
  }
  if (preview) applyPreview(preview)

  /** 两个 `.expdiv` 的显隐（对标 JS L17-23 的 `$('.expdiv').show()/.hide()`）。 */
  const setExpandVisible = (visible: boolean): void => {
    root.querySelectorAll<HTMLElement>(EXPAND_SELECTOR).forEach((el) => {
      el.style.display = visible ? '' : 'none'
    })
  }

  // 对标 JS L52-60：点收款码看大图（委托挂在根容器上，卸载时一并摘掉）。
  const onQrClick = (event: Event): void => {
    const img = (event.target as HTMLElement | null)?.closest<HTMLElement>('.qrCodeImg')
    if (!img) return
    ;(layer.photos as (o: unknown) => void)({
      photos: {
        title: QR_PHOTOS_TITLE,
        // 对标读的是 `$(this).attr('data-index')`（**字符串**），照抄这个形状。
        start: img.getAttribute('data-index') ?? '',
        data: qrPhotos,
      },
    })
  }
  root.addEventListener('click', onQrClick)

  // 对标 JS L17-23。
  ;(form.on as (e: string, cb: (obj: { value?: string }) => void) => void)(
    'radio(sta)',
    (obj) => {
      setExpandVisible(obj?.value === 'Y')
    },
  )

  /** 提交前置：读隐藏域里的 id 串（对标 JS L63 读的也是这个域）。 */
  const submitIds = (): number[] =>
    parsePerformanceIds(root.querySelector<HTMLInputElement>('#empIds')?.value ?? '')

  /** 提示 + 到点后刷新父页（对标 JS L84-86：`layer.msg(msg, {time:1000}, () => parent.tools…)`）。 */
  const showResultAndReload = (text: string): void => {
    ;(layer.msg as (t: string, o: unknown, cb: () => void) => void)(text, { time: 1000 }, () => {
      // ⚠️ 传的是**父窗口**（`parentToolsWindow()` ⇒ `window.parent`）：`tools` 挂在 `emp-emplist`
      // 那个 iframe **自己的** window 上（`emp-emplist.ts` 的 `toolsHost = window`，约 `:1836`），
      // 也就是本页的 `window.parent`。⛔ 传本页的 `window` 会恒读到 `undefined` ⇒ 结算成功、
      // 库里已改，但弹层不关、父页列表一行不刷（真机缺陷，见文件头第 3 条与 `parentToolsWindow`）。
      reloadParentList(parentToolsWindow(), layer)
    })
  }

  const notifyError = (e: unknown): void => {
    msg(e instanceof Error ? e.message : MESSAGES.failed)
  }

  // 对标 JS L70-72 的 `re` 布尔：挡连点。⚠️ 失败后复位（对标 L80/L89 也是各复位一次），
  // 让用户能改一改重试；成功那一支父页会重新载入本页，不必复位。
  let inflight = false

  ;(form.on as (e: string, cb: () => boolean) => void)('submit(savebtn)', () => {
    const sta = root.querySelector<HTMLInputElement>('input[name="sta"]:checked')?.value ?? ''
    // 对标 JS L65-69：没选清算状态 ⇒ 提示且**零请求**。
    if (sta === '') {
      msg(MESSAGES.noStatus)
      return false
    }
    if (inflight) return false
    const performanceIds = submitIds()

    if (sta === 'Y') {
      // Q4=A：勾了「同步到账本 = 需要」只提示「尚未开放」，**不阻断**本次结算。
      const syncexp =
        root.querySelector<HTMLInputElement>('input[name="syncexp"]:checked')?.value ?? 'N'
      if (syncexp === 'Y') msg(MESSAGES.syncNotOpen)
      inflight = true
      void settlePerformances(performanceIds, '')
        .then((r) =>
          showResultAndReload(
            settleResultText(
              r.settled_performances.length,
              r.skipped_performances.length,
              // ⭐ 第三形态的判据：「钱动了但绩效 id 列表为空」那一轮（坏账出口）。
              // 取 `.length`（**id 列表的长度**），与既有口径一致。
              r.settled_adjustments.length,
            ),
          ),
        )
        .catch(notifyError)
        .finally(() => {
          inflight = false
        })
      return false
    }

    // Q2=A：选「未结算」⇒ **先弹原因层**（`layer.prompt({formType:2})`，与 `emp-emplist.ts`
    // 既有的原因层同款：读码确认过 `prompt` 的 `yes` 只把值交给回调、**不自动关层**
    // ⇒「原因空 ⇒ 不关层、不发请求」可行）。
    ;(layer.prompt as unknown as (o: unknown, cb: (value: string, index: number) => void) => void)(
      {
        formType: 2,
        title: REASON_PROMPT_TITLE,
        value: '',
        placeholder: `把勾选的 ${performanceIds.length} 条绩效从「已结账」改回「未结账」；原因必填（会写进订单日志）`,
        area: ['420px', '200px'],
      },
      (value: string, index: number) => {
        const reason = value.trim()
        if (reason === '') {
          msg(MESSAGES.reasonRequired)
          return
        }
        // 关掉原因层（对标 JS 的 `prompt` 不自动关层；关层与发请求的顺序照 `emp-emplist.ts` 同款）。
        ;(layer.close as (index: number) => void)(index)
        inflight = true
        void revokeSettlements(performanceIds, reason)
          .then((r) =>
            showResultAndReload(
              revokeResultText(
                r.revoked_performances.length,
                r.skipped_performances.length,
                r.revoked_adjustments.length,
              ),
            ),
          )
          .catch(notifyError)
          .finally(() => {
            inflight = false
          })
      },
    )
    return false
  })

  return () => {
    root.removeEventListener('click', onQrClick)
    if (!hadBodyId) document.body.removeAttribute('id')
    root.innerHTML = ''
  }
}

/**
 * 标准 island 页出口（`layui-island/page.ts` 的注册表按它调用）。
 * 本页不发起页面跳转（`onNavigate` 用不上）。
 */
export function mount(root: HTMLElement, ctx: IslandContext): Promise<() => void> {
  return mountSettlementEmpPage(root, ctx)
}

/** 注册表按 `import.meta.glob('./pages/*.ts')` 的**默认导出**取页面。 */
export default { mount } satisfies IslandPage
