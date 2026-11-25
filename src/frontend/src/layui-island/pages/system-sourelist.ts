/**
 * 【订单来源管理】页的 **layui 复刻页**（对标物 `/System/sourelist`，**证据件标记 + 真 API**）。
 *
 * 形状：**单表列表页**（工具栏 2 颗按钮 + 表格 + 一个 `display:none` 的来源编辑弹层）。
 * 本模块把表格接到后端 `/api/business/order-sources/`（spec §4），弹层照对标 `layer.open` 打开。
 *
 * 证据（值一律从对标取）：
 *  - 标记与页内 `<style>` → `public/replica/system-sourelist.html`
 *    （源 `.tools/recon/suqing1/html/System__sourelist.html` 的 L43-142）
 *  - 列定义与 templet → `.tools/recon/suqing1/assets/JS2019/System/sourelist.js` **L19-87**
 *  - 弹窗打开参数 → 同文件 **L168-177**；保存校验 → **L225-246**
 *  - 工具栏 / 行内按钮 / 开关模板 → 证据件 L88-100
 *
 * ## 为什么不走 `artifactPage`
 *
 * `artifactPage` 装了一个**捕获阶段**的「尚未开放」总闸（`src/layui/artifactPage.ts:259-269`），
 * 它把页内一切按钮/开关的点击先截走（`preventDefault + stopPropagation`）⇒ 真数据下工具栏与
 * 行内的 4 颗按钮、以及行内开关全部失效。本页改用「自取证据件 + 自有委托」（先例：
 * `order-insiderecorderlist.ts`），并**逐条接管**那 4 颗按钮与短信两列。
 * `artifactPage` 另有两处写死的取值与对标不同（`defaultToolbar: []`、`page: {limit: …}`），
 * 本页 `table.render` 一律显式给对标值（`defaultToolbar: ['filter']`、`page: false`）。
 *
 * ## 逐条接管的「尚未开放」清单位置（计划 §1.6 #7：漏一个就是死按钮）
 *
 * | 元素 | 处置 |
 * |---|---|
 * | 工具栏 `lay-event="refresh"` | ✅ 真刷新（`table.reload`） |
 * | 工具栏 `lay-event="addBtn"` | ✅ 真弹窗（新增） |
 * | 行内 `lay-event="submitEdit"` | ✅ 真弹窗（编辑，预填） |
 * | 行内 `lay-event="submitDel"` | ✅ 真删除（`layer.confirm` → DELETE） |
 * | `.modifySmsSign`（短信签名列） | ⏳ 捕获阶段拦截 → `layer.msg('「短信签名」尚未开放')` |
 * | `.layui-form-switch[lay-filter="doNotSendSms"]` | ⏳ 捕获阶段拦截 → `layer.msg('「订单是否可发送短信」尚未开放')` |
 *
 * ⚠️ **短信两列必须捕获阶段 + `stopPropagation`**：layui 的 `form` 模块自己给
 * `.layui-form-switch` 挂了点击处理（切换 `checked`），冒泡阶段拦已经晚了——开关会**真的切过去**
 * （= 假成功，标准 §5 禁止的形状）。表格工具栏/行内按钮同理：layui 的 `table` 模块对
 * `*[lay-event]` 也做了委托（先例注释见 `artifactPage.ts:250-256`）。
 *
 * ## 分页：`page: false` 但取数**必须带 `page_size=200`**
 *
 * 对标一次返回全量（`page:false`，`sourelist.js:87`），我们的接口有分页而
 * `EnvelopePagination` 默认 **20**（`api/base.py:25`）⇒ 不带参时**第 21 条来源静默消失**
 * （页面不报错，用户只是选不到）。`page=1&page_size=200` 写在**唯一取数出口**
 * `api/orderSources.ts:88-94` 的 `fetchOrderSources()` 里（spec §4 的分页契约），
 * 首屏与 reload 都走它 ⇒ 两处不会漂移；该出口由 `api/__tests__/orderSources.spec.ts:35-52` 钉住。
 *
 * ## 表格数据由**页面自己取**（house 口径），不走 layui 的 `url`
 *
 * `config-replica.ts:81`：统一信封（`{code,msg,data:{items,total}}`）与 CSRF/错误映射都在
 * `api/` 里，绕开它等于再造一套。本页同样：`fetchOrderSources()` → `table.reloadData({data})`。
 * 这同时修掉 T5 评审 Minor ③ 的**首屏双 GET**（原来是 `url` 与挂载时那次 fetch 各一次）；
 * 且读码可知 `url` 在时会破坏显式取数这条路（详见 `reload`/`table.render` 处的注释）。
 *
 * ## 与对标并排比对的差异（原因与证据等级）
 *
 * | # | 差异 | 原因 |
 * |---|---|---|
 * | 1 | 「短信签名」列恒空（**文本**与 `data-orginsmssign`/`lay-tips` 里的签名） | 后端不返回 `smsSign`（R8：不建字段）⇒ `.modifySmsSign` 取到空文本；它的 `data-id` **照抄对标**取**行 id**（`res.id`），与 `smsSign` 无关（读码 `sourelist.js:72-75`：`data-id="' + res.id + '"`，文本是 `res.smsSign`） |
 * | 2 | 「订单是否可发送短信」开关恒关 | 后端不返回 `doNotSendSms`；模板 `{{ d.doNotSendSms != 'N' ? '' : 'checked' }}` 在字段缺失时 `undefined != 'N'` 为真 ⇒ **不加 `checked`**（读码，证据件 L98-100） |
 * | 3 | 店铺默认勾**淘宝/天猫**（`0`）而不是闲鱼 | 逐字照对标 `addSoure(0,'','',0,0)`（`sourelist.js:99`）；spec §10 #4 登记为「未确认项，一行可改」 |
 * | 4 | 前端对非整数排序**静默折成 0**，后端回 `1000` | 前端比后端松（spec §4 明写）；只登记这一处 |
 * | 5 | 列表**不重排** | 顺序由后端 `ordering` 决定（spec §6.1 #7：不在前端再排一遍 = 同一事实两套判据） |
 * | 6 | 四个 `<a>`（名称/说明/店铺/排序）**不挂事件** | 对标也没挂（spec §2.2 的 📌）——本线只做真实渲染 |
 * | 7 | 开关模板 `#switchTpl1` 留在证据件里**不渲染** | 本页走 `table.render`，layui 不建 `<tbody>` ⇒ `{{d.id}}` 只在不可见的 `<script type="text/html">` 内；`templet: '#switchTpl1'` 仍逐字照抄（spec §2.2）。⚠️ 也不要把 `{{d.id}}` 当模板待填项去长扫描（E-0032 的形状） |
 * | 8 | 字段名映射在**页面**做（`soureName`→`name`、`orderBy`→`order_by`、`shopType`→`shop_type`） | 接口一律 snake_case（spec §4）；`cols[].field` 仍是对标的拼写（templet 以它取行），载荷走我们自己的键 |
 * | 9 | ⭐ **第 3 列「公司店铺分组」+ 弹层多一格自建下拉（对标无分组）**（2026-10-26，计划 `docs/plans/2026-10-26-order-source-groups.md` §3.3） | 对标没有分组概念 ⇒ 这一列与下拉**都不是照抄**：表头顺序插在「来源说明」之后（总列数 7 → 8，原「操作」列从 `cols[6]` 变 `cols[7]`），下拉由页面**运行期注入**（`injectGroupSelect`，注入点在 `root.innerHTML` 之后、`panelTemplate` 快照之前）。空值显示「未分组」 |
 * | 10 | ⭐ **同页新增只读区块「店铺归属」（对标无此区块）**（计划 §3.4） | 证据件**没有页签标记**、本页也没加载 `element` 模块 ⇒ 不做 Tab，改成来源表**之后**的一块 `.layui-card`：两个原生下拉（订单来源 / 订单分组）+ 一张店铺子表（`XgjShop`，「店铺」不是「来源字典」）。区块自己取数（`fetchOrderSources` + `fetchXgjShops`），与来源表**互不复用行数据** |
 *
 * ## PII / 凭据（红线）
 *
 * 本页证据件**没有**内联租户数据（弹层与列表都是空壳，行数据由接口回）。唯一含租户数据的位置是
 * 行数据本身，来自我们的 API——不落进仓库。
 */
import {
  createOrderSource,
  createOrderSourceGroup,
  fetchOrderSourceGroups,
  fetchOrderSources,
  removeOrderSource,
  removeOrderSourceGroup,
  updateOrderSource,
  updateOrderSourceGroup,
  type OrderSourceGroupRow,
  type OrderSourceRow,
} from '@/api/orderSources'
import { fetchXgjShopsList, type XgjShopsListRow } from '@/api/xgjBindings'
import { findPanel, openPanelLayer } from '@/layui-island/config-replica'
import { escapeHtml } from '@/layui-island/escape'
import {
  bootstrapIsland,
  initIslandElements,
  islandMsg,
  type IslandContext,
  type IslandPage,
} from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

/** 证据件（页内 `<style>` + body 标记，零转义，与 `order-insiderecorderlist.ts` 自取证据件同形）。 */
export const SOURELIST_ARTIFACT = 'replica/system-sourelist.html'
/** 对标服务端渲染的 `<title>`（`System__sourelist.html` L5）。 */
export const TITLE = '订单来源管理'
/** 表格 `elem`（`sourelist.js` L20）。 */
export const TABLE_ELEM = '#tableList'
/** `table.reloadData` 的第一参要的是 **id**（不带 `#`），传 `#id` 是静默 no-op。 */
export const TABLE_ID = 'tableList'
/** 弹层容器（对标 `$('.change')`，`sourelist.js:176`）。 */
export const PANEL_SELECTOR = '.change'

/** 弹窗 9 个 radio 的 `value/title`——**逐字**取证据件 L125-133（spec §2.3 的「弹窗」那列）。 */
export const SHOP_TYPE_RADIOS: readonly { value: number; title: string }[] = [
  { value: 0, title: '淘宝/天猫' },
  { value: 10, title: '拼多多' },
  { value: 15, title: '京东' },
  { value: 20, title: '闲鱼' },
  { value: 30, title: '抖音' },
  { value: 40, title: '快手' },
  { value: 50, title: '唯品会' },
  { value: 60, title: '小红书' },
  { value: 70, title: '其它平台' },
]

/**
 * 列表「哪种店铺」列的文案（`sourelist.js:41-67` 的 `switch`）。
 * ⚠️ `0` 落到 `default`：**非店铺/淘宝/天猫**，与弹窗的 `淘宝/天猫` **刻意不同**
 * （spec §2.3 的 📌：两套文案逐字都照抄，不要「统一」）。
 */
export function shopTypeLabel(value: number): string {
  switch (value) {
    case 10:
      return '拼多多'
    case 15:
      return '京东'
    case 20:
      return '闲鱼'
    case 30:
      return '抖音'
    case 40:
      return '快手'
    case 50:
      return '唯品会'
    case 60:
      return '小红书'
    case 70:
      return '其它平台'
    default:
      return '非店铺/淘宝/天猫'
  }
}

/**
 * 排序数值的前端容错：对标 `VerifyInt` 失败 ⇒ **静默按 0**（`sourelist.js:243-245`）。
 * **只接受非负整数**（对标 `VerifyInt` 的正则 `/^\+?[0-9]*$/`，`common.js:18-24`）；
 * 空串由 `validateSoureForm` 先拦（「排序空」是四条校验里的第二条，不是容错分支）。
 */
export function parseOrderBy(raw: string): number {
  const text = raw.trim()
  return /^\+?\d+$/.test(text) ? Number(text) : 0
}

/**
 * 保存校验的**四条**（spec §2.5 / §8 #13）。纯函数 ⇒ 直接单测，不靠 grep 源码自证。
 *
 * 返回归一化后的值 + `error`（`''` = 通过）。两条「容错」不是错误：
 *  - 店铺未选 ⇒ `shopType = 0`（对标 `sourelist.js:238-240` 静默按 0）
 *  - 排序非整数 ⇒ `orderBy = 0`（对标 `sourelist.js:243-245`）
 * ⚠️ 这两条是**前端容错**，与后端「非整数 → `1000`」**刻意不同**（spec §4 已登记该差异）。
 */
export function validateSoureForm(input: {
  name: string
  orderByRaw: string
  shopType: number | null
}): { error: string; orderBy: number; shopType: number } {
  if (input.name.trim() === '') {
    // 文案逐字取对标（`sourelist.js:232-234`）。
    return { error: '请输入来源名称', orderBy: 0, shopType: input.shopType ?? 0 }
  }
  if (input.orderByRaw.trim() === '') {
    // 文案逐字取对标（`sourelist.js:236-238`）。
    return { error: '请输入排序数值，可以写0', orderBy: 0, shopType: input.shopType ?? 0 }
  }
  return {
    error: '',
    orderBy: parseOrderBy(input.orderByRaw),
    shopType: input.shopType ?? 0,
  }
}

/** 弹窗打开参数——**逐项**照 `sourelist.js:168-177`（spec §2.1）。 */
export function soureDialogOptions(editing: boolean, content: unknown): Record<string, unknown> {
  return {
    title: editing ? '编辑来源' : '新增一个来源',
    shade: [0.3, '#333'],
    shadeClose: true,
    // `type: 1` 由 `openPanelLayer` 自己补（它要给 `content` 包成 jQuery 对象）。
    skin: 'layui-layer-lan',
    anim: 8,
    // `isPc` 的三元取**桌面分支**（我们只做桌面后台，与大厅页的 `area` 处置同例）。
    area: ['820px', '470px'],
    content,
  }
}

/** 一行的 templet 入参是 layui 交回来的当前行（**键名是对标的拼写**，见文件头差异 #8）。 */
export type SoureCellRow = Record<string, unknown> & {
  id?: number
  soureName?: string
  remark?: string
  shopType?: number
  orderBy?: number
  /** 公司店铺分组名（未分组 = 空串；`templet` 负责显示「未分组」）。 */
  groupName?: string
  /** 公司店铺分组 id（未分组 = null）；弹层预填要用它。 */
  group?: number | null
}

/** 列骨架（顺序/标题/宽度/对齐/`fixed`/`toolbar`）——逐条照 `sourelist.js` L21-87。 */
export const COLS: Record<string, unknown>[] = [
  { field: 'soureName', title: '来源名称', fixed: 'left', width: 130, align: 'left' },
  { field: 'remark', title: '来源说明', align: 'left' },
  // ⭐ 第 3 列「公司店铺分组」（2026-10-26，计划 §3.3 第 1 条）：插在「来源说明」**之后**
  //    ⇒ 数组索引 2、总列数 7 → 8（原「操作」列从 `cols[6]` 变 `cols[7]`）。
  //    ⚠️ 这一列**没有对标物**（对标没有分组概念），宽度留空、左对齐，与「来源说明」同款。
  { field: 'groupName', title: '公司店铺分组', align: 'left' },
  { field: 'shopType', title: '哪种店铺', width: 115, align: 'center' },
  { field: 'smsSign', title: '短信签名', width: 200, align: 'left' },
  {
    field: 'doNotSendSms',
    // title 原文**含 HTML**（那颗 `layui-bangzhu` 帮助图标），逐字照抄。
    title:
      '订单是否可发送短信<i class="layui-icon layui-bangzhu" style="color:#FF5722" title="正常(绿色)：代表该来源的订单会根据系统设置的短信业务来发送短信通知；禁用(灰色)：表示该来源的订单，将禁止发送任何短信通知" />',
    width: 180,
    align: 'center',
    templet: '#switchTpl1',
  },
  { field: 'orderBy', title: '排序(数值越大越排前)', width: 160, align: 'center' },
  { title: '操作', width: 160, toolbar: '#baroperating' },
]

/**
 * 7 列的**真实渲染**声明：骨架来自 `COLS`（唯一出处），`templet` 只按 `field` 挂数据渲染。
 *
 * ⚠️ 四个 `<a>` 的形态逐字照 `sourelist.js`（含来源名称列那个**空 `class=""`**——对标原文就有，
 * 不是我们的疏漏）；「短信签名」列只读我们**有**的 `id`（`data-id`），不引用后端不返回的
 * `smsSign` ⇒ 文本与签名相关属性恒空；「订单是否可发送短信」列同理（差异表 #1/#2）。
 */
export function buildSourelistCols(): Record<string, unknown>[] {
  const cells: Record<string, (row: SoureCellRow) => string> = {
    // L30-33：`<a href="javascript:void(0);" class="" title="<name>"><name></a>`（空 class 照抄）。
    // ⚠️ I-4：`templet` 的返回值被 layui **当 HTML 插入**，`soureName` 是用户输入 ⇒ 转义。
    soureName: (res) => {
      const name = escapeHtml(String(res.soureName ?? ''))
      return `<a href="javascript:void(0);" class="" title="${name}">${name}</a>`
    },
    // L36-39：同形，取 `res.remark`（I-4：同上转义）。
    remark: (res) => {
      const text = escapeHtml(String(res.remark ?? ''))
      return `<a href="javascript:void(0);" style="" title="${text}">${text}</a>`
    },
    // ⭐ 公司店铺分组：空串显示「未分组」；有名字时**转义**（用户输入，
    //    `templet` 的返回值被 layui 当 HTML 插入 ⇒ 与 soureName/remark 同一条纪律）。
    //    ⚠️ 「未分组」是页面自拟文案（对标无此列），不是从后端拿的。
    groupName: (res) => {
      const name = String(res.groupName ?? '')
      return name === '' ? '未分组' : escapeHtml(name)
    },
    // L41-70：`switch(res.shopType)` 映射（`0` 落 default，与弹窗那套文案不同）。
    shopType: (res) => shopTypeLabel(Number(res.shopType ?? 0)),
    // L72-75：短信签名那颗 `<a>`；`data-id` **照抄对标**取**行 id**（`sourelist.js:73`
    // 写的是 `data-id="' + res.id + '"`，与 `smsSign` 无关）。
    // `res.smsSign` 我们**没有** ⇒ `data-orginsmssign` / `lay-tips` / 文本为空（差异 #1）。
    smsSign: (res) =>
      `<a class="modifySmsSign" data-id="${String(res.id ?? '')}" data-orginsmssign="" href="javascript:void(0);" style="" ` +
      'lay-tips="如果系统有发短信，则该来源的订单短信发出去的签名是【】，单击可以修改(前提条件是本系统已开通自定义短信签名服务)"></a>',
    // L77 走 `#switchTpl1`（`templet` 已在 `COLS` 里写死），这里不覆盖。
    // L80-83：`<a title="<orderBy>"><orderBy></a>`。
    orderBy: (res) => {
      const value = String(res.orderBy ?? '')
      return `<a href="javascript:void(0);" style="" title="${value}">${value}</a>`
    },
  }
  return COLS.map((c) => {
    const field = typeof c.field === 'string' ? c.field : ''
    const cell = cells[field]
    return cell ? { ...c, templet: cell } : { ...c }
  })
}

/** 一行的字段与后端 `OrderSourceSerializer` 一致；这里只挑页面用到的（页面不引用 `smsSign`）。 */
function toRow(row: OrderSourceRow): SoureCellRow {
  return {
    id: row.id,
    soureName: row.name,
    remark: row.remark,
    shopType: row.shop_type,
    orderBy: row.order_by,
    groupName: row.group_name,
    // ⭐ 线上踩过的坑：弹层预填按**本行的键**读（`soureName` / `orderBy` / `shopType` /
    //    `group`），此前 `openForm` 读的是接口键（`row.name` 等）⇒ 编辑弹窗永远空白、
    //    排序显示字面量 `undefined`、分组显示「未分组」。这里把 `group` 也带上（id，不是名字）。
    group: row.group,
  }
}

/* ─────────────────── 公司店铺分组：列 / 弹层下拉 / 分组管理（2026-10-26） ───────────────────
 *
 * 逐条对应计划 `docs/plans/2026-10-26-order-source-groups.md` §3.3：
 *  1. 第 3 列「公司店铺分组」（见 `COLS`）——空串显示「未分组」；
 *  2. 弹层一格**原生** `<select name="soureGroup">`（弹层里只有原生 input + radio、没有 xm-select
 *     ⇒ 与弹层同族；照 `order-orderlist.ts::renderPopgameName` 的「运行期造控件」手法）；
 *  3. 工具栏一颗「分组管理」按钮（运行期注入 `#toolbarDemo` 模板，先例
 *     `order-myorderlist.ts::injectSyncToolbarButton`），`layer` 内增 / 改名 / 删。
 *
 * ⚠️ **两套 name 别混**：弹层用 `soureGroup`，三个筛选页用 `sourceGroup`
 * （计划 §3.3 第 2 条明写；混了的表现是「保存了但没保存上」）。
 */

/** 弹层里分组下拉的 `name`（⚠️ 与三页筛选栏的 `sourceGroup` 不同，见上）。 */
export const GROUP_SELECT_NAME = 'soureGroup'
/** 分组管理弹层的容器选择器（与 `findPanel` / `openPanelLayer` 的约定一致）。 */
export const GROUP_MANAGER_SELECTOR = '.sourceGroupManager'
/** 工具栏「分组管理」按钮的 `lay-event`（标记与处理器共用一个常量）。 */
export const GROUP_TOOLBAR_EVENT = 'manageGroups'
/** 工具栏按钮的类名（⚠️ 不带点 —— E-0061 陷阱①）。 */
export const GROUP_TOOLBAR_BUTTON_CLASS = 'manageSourceGroupsBtn'
/** 新增分组时排序框的初值（与来源弹层同口径：`0` 是合法默认值）。 */
export const GROUP_ORDER_BY_DEFAULT = '0'

/** 工具栏模板里那颗按钮的标记（形状照证据件里同排的 `layui-btn-xs`）。 */
const GROUP_TOOLBAR_BUTTON_HTML =
  `<button class="layui-btn layui-btn-xs layui-btn-normal ${GROUP_TOOLBAR_BUTTON_CLASS}"` +
  ` lay-event="${GROUP_TOOLBAR_EVENT}" type="button" title="新增 / 改名 / 删除公司店铺分组">` +
  `<i class="layui-icon layui-icon-set"></i>分组管理</button>`

/**
 * 把「分组管理」按钮注入工具栏模板（`#toolbarDemo` 那段 `<script type="text/html">`）。
 *
 * ⚠️ **注入模板、不塞渲染结果**：layui 每次 `table.reload` 都重渲工具栏，塞进
 * `.layui-table-tool` 的节点会在下一次刷新后消失（先例注释见 `order-myorderlist.ts`）。
 * ⚠️ 解析成 DOM 再改（⛔ 不做字符串替换）：字符串锚点与证据件漂移时是**静默**不生效。
 * @returns 真的注进去了没有。
 */
export function injectGroupToolbarButton(root: HTMLElement): boolean {
  const template = root.querySelector<HTMLScriptElement>('#toolbarDemo')
  if (template === null) return false
  const holder = document.createElement('div')
  holder.innerHTML = template.textContent ?? ''
  const container = holder.querySelector<HTMLElement>('.layui-btn-container')
  if (container === null) return false
  container.insertAdjacentHTML('beforeend', GROUP_TOOLBAR_BUTTON_HTML)
  template.textContent = holder.innerHTML
  return true
}

/**
 * 在弹层里注入「公司店铺分组」下拉（**注入时点是硬要求**）。
 *
 * 必须在 `root.innerHTML = 证据件` **之后**、`panelTemplate` 快照 **之前**：
 *  - 在 `innerHTML` 之前：节点还不存在；
 *  - 在快照之后：快照里没有这一格 ⇒ `ensurePanel()` 补出来的旧副本没有下拉，
 *    而 `done()` 会拿被重建的节点**覆盖**快照 ⇒ 下一次「新增」静默少一格。
 *
 * 位置：插在「哪种店铺」那个 `.layui-form-item` **之前**（即排序数值之后），
 * 与其余 `<input>` 同族；首项「未分组」= `value=""`。
 *
 * @returns 真的注进去了没有（`false` = 证据件结构变了）。
 */
export function injectGroupSelect(root: HTMLElement): boolean {
  const panel = root.querySelector<HTMLElement>(PANEL_SELECTOR)
  if (panel === null) return false
  if (panel.querySelector(`select[name="${GROUP_SELECT_NAME}"]`) !== null) return true
  const html =
    '<div class="layui-form-item" data-tt-injected="source-group">' +
    '<label class="layui-form-label">公司店铺分组</label>' +
    '<div class="layui-input-block">' +
    `<select name="${GROUP_SELECT_NAME}" id="${GROUP_SELECT_NAME}">` +
    '<option value="">未分组</option>' +
    '</select>' +
    '</div></div>'
  const anchor = panel.querySelector('[name="shopType"]')?.closest('.layui-form-item') ?? null
  if (anchor !== null) anchor.insertAdjacentHTML('beforebegin', html)
  else panel.insertAdjacentHTML('afterbegin', html)
  return true
}

/** 把分组字典写进一个分组下拉（先清成只剩「未分组」，再按后端顺序 append）。 */
export function fillGroupSelect(
  select: HTMLSelectElement,
  groups: readonly OrderSourceGroupRow[],
  /**
   * 当前行自己的分组（`id` + 名字）。⚠️ **降级路径的护栏**（2026-10-27 评审 M1）：
   * 分组字典取失败（`groupItems=[]`）或分组数超过页大小（>200，静默截断）时，
   * 本行的分组**不在候选里** ⇒ `form.val` 落成 `''` ⇒ 保存时送 `group: null`，
   * 把已有分组**静默清掉**。这里把它补成一条 option，保证「打开什么、保存出去还是什么」。
   * 业务方 2026-10-27 明确要求：**分组不能被这次改动打乱**。
   */
  current?: { id: number; name: string } | null,
): void {
  select.innerHTML = '<option value="">未分组</option>'
  const seen = new Set<string>()
  for (const group of groups) {
    const option = document.createElement('option')
    option.value = String(group.id)
    option.textContent = group.name
    select.appendChild(option)
    seen.add(String(group.id))
  }
  if (current != null && !seen.has(String(current.id))) {
    const option = document.createElement('option')
    option.value = String(current.id)
    option.textContent = current.name === '' ? '（当前分组）' : current.name
    select.appendChild(option)
  }
}

/**
 * 分组管理弹层的内容（**每次打开重渲**：列表要跟着最新数据走）。
 *
 * ⚠️ 只有「名称 + 排序 + 新增」与每行两颗按钮；来源数（`source_count`）只是**提示**，
 * 真正决定显示哪个数字的是数据里给的值（拿不到就不写「N 个来源」）。
 */
export function groupManagerHtml(groups: readonly OrderSourceGroupRow[]): string {
  const rows =
    groups.length === 0
      ? '<div style="color:#999;padding:8px 0">暂无分组</div>'
      : groups
          .map((group) => {
            // ⚠️ 名字是用户输入、这里拼进 HTML ⇒ 必须转义。
            const name = escapeHtml(group.name)
            const count =
              typeof group.source_count === 'number'
                ? `<span style="color:#999">${group.source_count} 个来源</span>`
                : ''
            return (
              '<div style="display:flex;align-items:center;gap:8px;padding:6px 0;' +
              'border-bottom:1px solid #f2f2f2">' +
              `<span style="flex:1">${name}</span>${count}` +
              `<button type="button" class="layui-btn layui-btn-xs"` +
              ` data-group-action="rename" data-group-id="${group.id}">改名</button>` +
              `<button type="button" class="layui-btn layui-btn-xs layui-btn-danger"` +
              ` data-group-action="delete" data-group-id="${group.id}">删除</button>` +
              '</div>'
            )
          })
          .join('')
  return (
    '<div style="padding:10px 12px">' +
    '<div style="display:flex;gap:8px;align-items:center;margin-bottom:10px">' +
    '<input type="text" id="groupNameInput" class="layui-input" style="flex:1"' +
    ' maxlength="20" placeholder="分组名称（≤20 字）">' +
    `<input type="text" id="groupOrderByInput" class="layui-input" style="width:90px"` +
    ` maxlength="3" value="${GROUP_ORDER_BY_DEFAULT}" placeholder="排序">` +
    '<button type="button" class="layui-btn layui-btn-sm layui-btn-normal" id="groupAddBtn">新增</button>' +
    '</div>' +
    `<div class="groupManagerList">${rows}</div>` +
    '</div>'
  )
}

/**
 * 删除分组的确认文案（计划 §3.3 第 5 条：**N 必须来自数据**）。
 *
 * `source_count` 拿不到（`undefined`）或为 0 ⇒ **不写这句话**（⛔ 不写死「N」、不凭感觉承诺）。
 */
export function groupDeleteConfirmText(name: string, sourceCount?: number): string {
  const base = `确定删除分组「${name}」吗？`
  if (typeof sourceCount !== 'number' || sourceCount === 0) return base
  return `${base}该分组下的 ${sourceCount} 个来源将变成未分组。`
}

/* ─────────────────── 店铺归属（同页独立区块，2026-10-26） ───────────────────
 *
 * 计划 §3.4：**同页独立区块，不做 Tab**。证据件内容区没有页签标记，本页也没加载
 * `element` 模块 ⇒ 硬上 layui tab 要另引模块 + 造页签标记 + 处理选中态，成本与风险都更高。
 * 做法：mount 时在来源表容器之后追加一块 `.layui-card`（先例 `user-myinfo.ts` 的运行期追加块）。
 *
 * ⛔ **与来源行不是一回事**：来源行是字典一件，这里是「闲管家店铺」（`XgjShop`）一件；
 * 绑定了但还没同步出店铺的来源**不会**出现在这里，孤儿店铺（`binding IS NULL`）会出现、
 * 列里显示空。
 */

/** 区块根节点的**类名**（⚠️ 不带点）与**选择器**（E-0061 陷阱①）。 */
export const SHOPS_BLOCK_CLASS = 'sourceShopsBlock'
export const SHOPS_BLOCK_SELECTOR = `.${SHOPS_BLOCK_CLASS}`
/** 区块子表的实例 id（`table.reloadData` 的第一参要的是 id、不带 `#`）。 */
export const SHOPS_TABLE_ID = 'xgjShopsTable'
/** 两个下拉的 id（原生 `<select>`：本区块不是复刻面，与来源页的筛选口径无关）。 */
export const SHOPS_SOURCE_SELECT_ID = 'xgjShopsSource'
export const SHOPS_GROUP_SELECT_ID = 'xgjShopsGroup'
/** 子表空态文案（自建区块，没有对标原文可抄）。 */
export const SHOPS_EMPTY_TEXT = '暂无店铺'

/** 「店铺归属」区块的标记（含标题 + 两个下拉 + 子表容器）。 */
export function shopsBlockHtml(): string {
  return (
    `<div class="layui-card ${SHOPS_BLOCK_CLASS}" style="margin-top:15px">` +
    '<div class="layui-card-header">店铺归属</div>' +
    '<div class="layui-card-body" style="padding:10px 15px">' +
    '<div class="layui-form" style="margin-bottom:8px">' +
    '<div class="layui-inline">' +
    '<label class="layui-form-label" style="width:80px">订单来源</label>' +
    '<div class="layui-input-inline" style="width:180px">' +
    `<select id="${SHOPS_SOURCE_SELECT_ID}" name="${SHOPS_SOURCE_SELECT_ID}">` +
    '<option value="">全部来源</option></select>' +
    '</div></div>' +
    '<div class="layui-inline">' +
    '<label class="layui-form-label" style="width:80px">订单分组</label>' +
    '<div class="layui-input-inline" style="width:180px">' +
    `<select id="${SHOPS_GROUP_SELECT_ID}" name="${SHOPS_GROUP_SELECT_ID}">` +
    '<option value="">全部分组</option></select>' +
    '</div></div>' +
    '</div>' +
    `<table id="${SHOPS_TABLE_ID}" lay-filter="${SHOPS_TABLE_ID}"></table>` +
    '</div></div>'
  )
}

/** 子表的列（展示用；分组空串显示「未分组」，与来源表那一列同一句文案）。 */
export function shopsTableCols(): Record<string, unknown>[] {
  return [
    { field: 'shop_name', title: '店铺名称', minWidth: 180 },
    { field: 'user_name', title: '会员名', width: 140 },
    { field: 'order_source_name', title: '订单来源', width: 160 },
    {
      field: 'group_name',
      title: '公司店铺分组',
      width: 160,
      templet: (row: XgjShopsListRow): string =>
        row.group_name === '' ? '未分组' : escapeHtml(row.group_name),
    },
    { field: 'binding_name', title: '所属应用', width: 160 },
  ]
}

/**
 * 把下拉的当前选中值折成筛选参数：**没选 ⇒ 不发键**（计划 §3.4 / §4 的口径）。
 *
 * ⚠️ `?source_group=` 空串会被后端判非法整数（`1000`）⇒ 一定要走 `undefined`。
 */
export function shopsFilterParams(
  root: HTMLElement,
): { order_source?: number; source_group?: number } {
  const source = root.querySelector<HTMLSelectElement>(`#${SHOPS_SOURCE_SELECT_ID}`)?.value ?? ''
  const group = root.querySelector<HTMLSelectElement>(`#${SHOPS_GROUP_SELECT_ID}`)?.value ?? ''
  const params: { order_source?: number; source_group?: number } = {}
  const sourceId = Number(source)
  if (source !== '' && Number.isFinite(sourceId)) params.order_source = sourceId
  const groupId = Number(group)
  if (group !== '' && Number.isFinite(groupId)) params.source_group = groupId
  return params
}

/* ────────────────────────────── 挂载 ────────────────────────────── */

export async function mountSourelistPage(root: HTMLElement, _ctx: IslandContext): Promise<() => void> {
  const layui: LayuiNamespace = await bootstrapIsland(['form', 'table', 'layer'])
  const form = layui.form as LayuiModule
  const table = layui.table as LayuiModule
  const layer = layui.layer as LayuiModule

  document.title = TITLE

  const msg = (text: string): void => islandMsg(layer, text)
  const errText = (e: unknown): string => (e instanceof Error ? e.message : '操作失败')

  // 分组字典先取（与证据件并行）：取不到**不拦页**（口径同 `fetchGames().catch(() => [])`）——
  // 弹层那一格只剩「未分组」，页面照常可用。⚠️ 必须赶在 `injectGroupSelect` 之前拿到。
  const groupDictPromise = fetchOrderSourceGroups()
    .then((page) => page.items ?? [])
    .catch(() => [] as OrderSourceGroupRow[])

  const res = await fetch(`${import.meta.env.BASE_URL}${SOURELIST_ARTIFACT}`)
  if (!res.ok) throw new Error(`证据件取不到（HTTP ${res.status}）：${SOURELIST_ARTIFACT}`)
  root.innerHTML = await res.text()
  initIslandElements(layui)

  /** 分组字典（写操作的刷新点会更新它；弹层下拉在每次 `openForm` 时按它重建）。 */
  let groupItems: OrderSourceGroupRow[] = await groupDictPromise
  // ⚠️ **注入时点是硬要求**：`root.innerHTML` 之后（节点已存在）、`panelTemplate` 快照之前
  //    （否则快照里没有这一格，`ensurePanel()` 补出来的旧副本会少一个下拉）。
  injectGroupSelect(root)
  injectGroupToolbarButton(root)

  /* ── 弹层节点 ──
   *
   * ⚠️ 证据件里 `.change` 是**真实存在的**（L102-143，`display:none`），但它可能被 `table.reload`
   * 那次渲染连带重建/摘掉（layui 会重填工具栏那块）。所以这里留一个**模板快照**：节点被摘掉时
   * 从快照补一个**干净副本**回 `<form>`——没有这一步，重载后「新增」会静默打不开
   * （`openPanelLayer` 找不到节点、回 `null`）。有快照时**不覆盖**现存的节点（否则会把用户
   * 正在编辑的值冲掉）。
   */
  let panelTemplate = root.querySelector<HTMLElement>(PANEL_SELECTOR)
  if (panelTemplate) panelTemplate = panelTemplate.cloneNode(true) as HTMLElement

  function panelHost(): HTMLElement | null {
    return root.querySelector<HTMLElement>('form.layui-form') ?? root.querySelector('form')
  }

  /** 取回可用的弹层节点；被 layui 摘走后从快照补一个。 */
  function ensurePanel(): HTMLElement | null {
    const existing = findPanel(root, PANEL_SELECTOR)
    if (existing) return existing
    const host = panelHost()
    if (!host || !panelTemplate) return null
    const fresh = panelTemplate.cloneNode(true) as HTMLElement
    host.appendChild(fresh)
    return fresh
  }

  /** 弹层里的输入（按 `name` 在**弹层节点内**找——不写文档级选择器，免得撞上父窗口/别的 popup）。 */
  function input(panel: HTMLElement | null, name: string): HTMLInputElement | null {
    return panel?.querySelector<HTMLInputElement>(`[name="${name}"]`) ?? null
  }

  /** 当前选中的店铺 radio（`null` = 未选，第四条校验的判据）。 */
  function checkedShopType(panel: HTMLElement | null): number | null {
    const el = panel?.querySelector<HTMLInputElement>('[name="shopType"]:checked') ?? null
    if (!el) return null
    const value = Number(el.value)
    return Number.isFinite(value) ? value : null
  }

  /** 当前正在编辑的行（`null` = 新增）。 */
  let editing: SoureCellRow | null = null
  let layerIndex: number | null = null

  /**
   * 弹窗打开。⚠️ **顺序是硬要求**：「先预填、再 open」。
   *
   * `layer.open({type:1, content: jq})` 会把 `.change` 节点**搬出 `<form>`** 塞进
   * `.layui-layer-content`；而 `form.val('form', …)` 是按 `<form lay-filter="form">` 的**作用域**
   * 找输入的 ⇒ 先 open 再预填，`form.val` 就找不到这些 `<input>`（编辑时打开是空表单）。
   * `config-replica.openPanelLayer` 的 `end` 回调负责关闭后把节点放回，否则第二次打不开。
   */
  function openForm(row: SoureCellRow | null): void {
    editing = row
    if (layerIndex !== null) {
      ;(layer.close as (i: number) => void)(layerIndex)
      layerIndex = null
    }
    const panel = ensurePanel()
    if (!panel) {
      // 节点与快照都没有：只提示，不假装打开了（绝不静默）。
      msg('弹窗内容缺失，请联系管理员')
      return
    }
    // 分组下拉的候选**在这里重建**（而不是只在 mount 填一次）：分组增删改之后、或
    // `ensurePanel` 补出来的是快照克隆，那份 clone 里的 option 可能已经过期。
    const groupSelect = panel.querySelector<HTMLSelectElement>(
      `select[name="${GROUP_SELECT_NAME}"]`,
    )
    if (groupSelect !== null) {
      fillGroupSelect(
        groupSelect,
        groupItems,
        row !== null && row.group != null
          ? { id: row.group, name: String(row.groupName ?? '') }
          : null,
      )
    }
    // 新增：三输入为空 + 排序 `'0'`（R5）+ 店铺 `0`（淘宝/天猫，spec §10 #4）+ `soureId` 0
    // + 分组空（未分组；`''` 是 `<option value="">` 的那一项）。
    ;(form.val as (filter: string, data: Record<string, unknown>) => void)('form', {
      soureName: row && row.soureName != null ? String(row.soureName) : '',
      soureRemark: row && row.remark != null ? String(row.remark) : '',
      soureOrderBy: row && row.orderBy != null ? String(row.orderBy) : '0',
      soureId: row && row.id != null ? row.id : 0,
      shopType: row && row.shopType != null ? row.shopType : 0,
      soureGroup: row && row.group != null ? String(row.group) : '',
    })
    ;(form.render as () => void)()
    layerIndex = openPanelLayer(
      root,
      layui,
      layer,
      soureDialogOptions(row !== null, panel),
      PANEL_SELECTOR,
    )
  }

  /** 保存（对标 `form.on('submit(savebtn)')`，`sourelist.js:225-265`）。 */
  async function submit(): Promise<void> {
    const panel = findPanel(root, PANEL_SELECTOR)
    const name = input(panel, 'soureName')?.value ?? ''
    const orderByRaw = input(panel, 'soureOrderBy')?.value ?? ''
    const remark = input(panel, 'soureRemark')?.value ?? ''
    const validated = validateSoureForm({ name, orderByRaw, shopType: checkedShopType(panel) })
    if (validated.error !== '') {
      msg(validated.error)
      return
    }
    // 分组：选中 ⇒ 数字 id、未分组 ⇒ **`null`**（⛔ 不把 `0` 当有效 id——后端会判非法主键）。
    // `''` 是首项「未分组」的 value；`Number` 只在非空时用。
    const groupRaw =
      panel?.querySelector<HTMLSelectElement>(`select[name="${GROUP_SELECT_NAME}"]`)?.value ?? ''
    const payload = {
      name,
      remark,
      order_by: validated.orderBy,
      shop_type: validated.shopType,
      group: groupRaw === '' ? null : Number(groupRaw),
    }
    try {
      // `editing` 是**当前行**（不是 `soureId` 的字符串）——判据用「有没有这一行」，不用真值。
      if (editing && editing.id != null) await updateOrderSource(editing.id, payload)
      else await createOrderSource(payload)
    } catch (e) {
      // 失败**原样**显示后端 `msg`（业务码由 `api/http.ts` 的统一分支映射），不换文案、不吞。
      msg(errText(e))
      return
    }
    // 成功：关弹窗 + 刷新列表（后端成功 `msg` 是空串，`core/envelope.py:8` ⇒ 用前端文案）。
    if (layerIndex !== null) {
      ;(layer.close as (i: number) => void)(layerIndex)
      layerIndex = null
    }
    editing = null
    reload()
    msg('保存成功')
  }

  /** 删除（`layer.confirm` → DELETE → 刷新）。 */
  function remove(row: SoureCellRow): void {
    ;(layer.confirm as (t: string, cb: (index: number) => void) => void)(
      '确定删除该来源吗？',
      (index: number) => {
        ;(layer.close as (i: number) => void)(index)
        if (row.id == null) return
        void removeOrderSource(row.id).then(
          () => {
            reload()
            msg('删除成功')
          },
          (e: unknown) => msg(errText(e)),
        )
      },
    )
  }

  /**
   * 取数：`fetchOrderSources()`（`api/orderSources.ts`）→ `table.reloadData`。
   *
   * ⚠️ **分页契约在取数出口里，不在表格配置里**：`page=1&page_size=200` 写在
   * `api/orderSources.ts:88-94`，首屏与 reload **走同一个函数** ⇒ 两处不会漂移。
   * 那个出口由 `api/__tests__/orderSources.spec.ts:35-52` 钉住（不带就是默认 20 ⇒ 第 21 条
   * 静默消失，spec §4 / 评审 Critical 1）。
   * 这里**不再**给 `reloadData` 传 `where`：表格没有 `url`，layui 不会用它拼任何请求（死配置）。
   */
  function reload(): void {
    void fetchOrderSources().then(
      (page) => {
        ;(table.reloadData as (id: string, o: unknown) => void)(TABLE_ID, {
          data: page.items.map(toRow),
        })
      },
      (e: unknown) => msg(errText(e)),
    )
  }

  /* ── 表格 ──
   *
   * ⚠️ **不带 `url`**（house 口径，`config-replica.ts:81`「表格数据由页面自己取，不走 layui 的
   * `url`」；同口径的 `game-projectsetting.ts:135-143` 的 tableSpec 也没有 `url`）。两条理由：
   *  1. **首屏只发一次 GET**（T5 评审 Minor ③）：原来 `url` 与挂载时那次 `fetchOrderSources()`
   *     各发一次，且第一个响应被第二个覆盖。
   *  2. 读 layui 2.13.9 源码（`public/layui/layui.js` 的 table `pullData`，分支顺序是
   *     `l.renderData ? … : o.url ? ajax : 本地 data`）可知：**只要 config 里还有 `url`，
   *     `table.reloadData(id, {data})` 就走 ajax 分支、忽略我们传的 `data`** ⇒ 「显式取数 +
   *     reloadData」那条路根本落不到表格上（每次刷新还会再去请求一次 `url`）。去掉 `url` 后
   *     `reloadData` 才真正渲染我们取回的行。绕开 layui 的 ajax 也意味着统一信封、CSRF 与
   *     错误映射仍只在 `api/` 一处（`config-replica.ts:81` 的同一理由）。
   *
   * `data: []` 先渲染一张空表（`config-replica.ts:116-122` 的「必须先 render 一次」同因：
   * `reloadData` 按实例 id 查表，不先 `render` 会静默失败）。
   * `parseData` / `method` / `request` / `where` 只服务于 layui 自己的 ajax ⇒ 一并去掉。
   */
  ;(table.render as (o: unknown) => void)({
    elem: TABLE_ELEM,
    id: TABLE_ID,
    data: [],
    toolbar: '#toolbarDemo',
    // 对标同一对象里两次赋值（L27 `[]`、L28 `['filter']`）——**后者生效**，照抄结果值。
    defaultToolbar: ['filter'],
    // 对标 `page:false`（L87）：一次取全，没有分页控件。
    page: false,
    height: 'full',
    cellMinWidth: 100,
    cols: [buildSourelistCols()],
    text: { none: '暂无数据' },
    done: () => {
      // 表格每次重绘都会重建 `.change`/`#switchTpl1` 那一带：把弹层节点的快照留在手里，
      // 重载后 `ensurePanel()` 才有干净副本可用（见其注释）。
      const current = root.querySelector<HTMLElement>(PANEL_SELECTOR)
      if (current) panelTemplate = current.cloneNode(true) as HTMLElement
      ;(form.render as () => void)()
    },
  })

  /* ── 交互：工具栏 / 行内按钮（layui 的 `table.on`） ── */
  ;(table.on as (e: string, cb: (obj: unknown) => void) => void)(
    `toolbar(${TABLE_ID})`,
    (obj: unknown) => {
      const event = (obj as { event?: string }).event
      if (event === 'refresh') reload()
      else if (event === 'addBtn') openForm(null)
      else if (event === GROUP_TOOLBAR_EVENT) openGroupManager()
    },
  )
  ;(table.on as (e: string, cb: (obj: unknown) => void) => void)(
    `tool(${TABLE_ID})`,
    (obj: unknown) => {
      const typed = obj as { event?: string; data?: OrderSourceRow }
      const row = typed.data
      if (!row) return
      if (typed.event === 'submitEdit') openForm(row)
      else if (typed.event === 'submitDel') remove(row)
    },
  )
  // 保存按钮（对标 `lay-submit lay-filter="savebtn"`）。
  ;(form.on as (e: string, cb: () => void) => void)('submit(savebtn)', () => {
    void submit()
  })

  /* ── 短信两列：捕获阶段拦截（见文件头「必须捕获阶段」） ── */
  const onSmsClick = (event: Event): void => {
    const target = event.target as HTMLElement
    const sign = target.closest('.modifySmsSign')
    if (sign) {
      event.preventDefault()
      event.stopPropagation()
      msg('「短信签名」尚未开放')
      return
    }
    const sw = target.closest('.layui-form-switch') ?? target.closest('[lay-filter="doNotSendSms"]')
    if (sw) {
      event.preventDefault()
      event.stopPropagation()
      msg('「订单是否可发送短信」尚未开放')
    }
  }
  root.addEventListener('click', onSmsClick, true)

  /* ── 分组管理弹层（工具栏那颗按钮；`layer` 内增 / 改名 / 删） ──
   *
   * ⚠️ **监听器挂在节点自己身上**（不是 `root`）：`openPanelLayer` 打开时会把节点**搬进**
   * `.layui-layer-content`（脱离 root）⇒ 挂在 root 上的委托会收不到弹层里的点击。
   * ⚠️ 弹层里改完一律 `reloadGroups()`：它重取分组字典、重渲弹层列表、刷新来源表
   * （来源那列显示组名）与「店铺归属」的分组下拉。
   */
  let groupManagerNode: HTMLElement | null = null
  let groupManagerLayerIndex: number | null = null

  function ensureGroupManager(): HTMLElement | null {
    const existing = findPanel(root, GROUP_MANAGER_SELECTOR)
    if (existing) return existing
    const host = panelHost()
    if (!host) return null
    const node = document.createElement('div')
    node.className = GROUP_MANAGER_SELECTOR.slice(1)
    node.style.display = 'none'
    node.addEventListener('click', onGroupManagerClick)
    host.appendChild(node)
    return node
  }

  function renderGroupManager(): void {
    if (groupManagerNode !== null) groupManagerNode.innerHTML = groupManagerHtml(groupItems)
  }

  /** 重取分组字典并刷新四处：弹层列表 / 来源表（组名列）/ 区块的分组下拉 / 区块行。 */
  async function reloadGroups(): Promise<void> {
    const page = await fetchOrderSourceGroups().catch(() => null)
    if (page !== null) groupItems = page.items ?? []
    renderGroupManager()
    fillShopsSelect(SHOPS_GROUP_SELECT_ID, '全部分组', groupItems)
    reload()
    void loadShops()
  }

  function openGroupManager(): void {
    const node = ensureGroupManager()
    if (node === null) {
      msg('弹窗内容缺失，请联系管理员')
      return
    }
    groupManagerNode = node
    renderGroupManager()
    if (groupManagerLayerIndex !== null) {
      ;(layer.close as (i: number) => void)(groupManagerLayerIndex)
      groupManagerLayerIndex = null
    }
    groupManagerLayerIndex = openPanelLayer(
      root,
      layui,
      layer,
      {
        title: '分组管理',
        shade: [0.3, '#333'],
        shadeClose: true,
        skin: 'layui-layer-lan',
        anim: 8,
        area: ['560px', '480px'],
      },
      GROUP_MANAGER_SELECTOR,
    )
  }

  async function addGroup(): Promise<void> {
    const node = groupManagerNode
    if (node === null) return
    const name = node.querySelector<HTMLInputElement>('#groupNameInput')?.value.trim() ?? ''
    const orderByRaw = node.querySelector<HTMLInputElement>('#groupOrderByInput')?.value ?? '0'
    if (name === '') {
      msg('请输入分组名称')
      return
    }
    try {
      await createOrderSourceGroup({ name, order_by: parseOrderBy(orderByRaw) })
    } catch (e) {
      msg(errText(e))
      return
    }
    await reloadGroups()
    msg('保存成功')
  }

  function renameGroup(group: OrderSourceGroupRow): void {
    ;(layer.prompt as
      | ((o: unknown, cb: (value: string, index: number) => void) => void)
      | undefined)?.(
      { title: '修改分组名称', value: group.name, formType: 0 },
      (value: string, index: number) => {
        const name = String(value ?? '').trim()
        if (name === '') {
          msg('请输入分组名称')
          return
        }
        ;(layer.close as (i: number) => void)(index)
        void updateOrderSourceGroup(group.id, { name }).then(
          () => {
            void reloadGroups()
            msg('保存成功')
          },
          (e: unknown) => msg(errText(e)),
        )
      },
    )
  }

  function deleteGroup(group: OrderSourceGroupRow): void {
    ;(layer.confirm as (t: string, cb: (index: number) => void) => void)(
      groupDeleteConfirmText(group.name, group.source_count),
      (index: number) => {
        ;(layer.close as (i: number) => void)(index)
        void removeOrderSourceGroup(group.id).then(
          () => {
            void reloadGroups()
            msg('删除成功')
          },
          (e: unknown) => msg(errText(e)),
        )
      },
    )
  }

  function onGroupManagerClick(event: Event): void {
    const target = event.target as HTMLElement
    if (target.closest('#groupAddBtn') !== null) {
      void addGroup()
      return
    }
    const button = target.closest<HTMLElement>('[data-group-action]')
    if (button === null) return
    const id = Number(button.dataset.groupId)
    const group = groupItems.find((item) => item.id === id)
    if (group === undefined) return
    if (button.dataset.groupAction === 'rename') renameGroup(group)
    else if (button.dataset.groupAction === 'delete') deleteGroup(group)
  }

  /* ── 「店铺归属」区块（同页独立区块，见文件头差异 #10） ──
   *
   * ⚠️ 插在来源表**之后**（`form` 的 `afterend`；layui 把渲染出来的 `.layui-table-view`
   * 放在 `<table id="tableList">` 后面、仍在 form 内 ⇒ 视觉上区块就在来源表下方）。
   * ⚠️ 子表 `page:false`、不设 `height:'full'`（跟随内容，与主表同页共存）。
   * ⚠️ 区块**自己取数**（计划 §3.4）：`fetchOrderSources()` 取来源下拉候选 +
   * `fetchXgjShopsList()` 取店铺行；与来源表**互不复用行数据**。
   */
  const shopsHost = panelHost()
  shopsHost?.insertAdjacentHTML('afterend', shopsBlockHtml())

  function fillShopsSelect(
    id: string,
    firstLabel: string,
    rows: readonly { id: number; name: string }[],
  ): void {
    const select = root.querySelector<HTMLSelectElement>(`#${id}`)
    if (select === null) return
    const current = select.value
    select.innerHTML = ''
    const first = document.createElement('option')
    first.value = ''
    first.textContent = firstLabel
    select.appendChild(first)
    for (const row of rows) {
      const option = document.createElement('option')
      option.value = String(row.id)
      option.textContent = row.name
      select.appendChild(option)
    }
    // 重填后尽量保留用户已选（分组增删改触发的那次重填不该把筛选清掉）。
    if (current !== '') select.value = current
  }

  async function loadShops(): Promise<void> {
    try {
      const page = await fetchXgjShopsList(shopsFilterParams(root))
      ;(table.reloadData as (id: string, o: unknown) => void)(SHOPS_TABLE_ID, {
        data: page.items,
      })
    } catch (e) {
      msg(errText(e))
    }
  }

  async function initShopsBlock(): Promise<void> {
    if (root.querySelector(SHOPS_BLOCK_SELECTOR) === null) return
    ;(table.render as (o: unknown) => void)({
      elem: `#${SHOPS_TABLE_ID}`,
      id: SHOPS_TABLE_ID,
      data: [],
      defaultToolbar: [],
      page: false,
      cols: [shopsTableCols()],
      text: { none: SHOPS_EMPTY_TEXT },
    })
    const sourcesPage = await fetchOrderSources().catch(() => null)
    if (sourcesPage !== null) {
      fillShopsSelect(SHOPS_SOURCE_SELECT_ID, '全部来源', sourcesPage.items ?? [])
    }
    fillShopsSelect(SHOPS_GROUP_SELECT_ID, '全部分组', groupItems)
    for (const id of [SHOPS_SOURCE_SELECT_ID, SHOPS_GROUP_SELECT_ID] as const) {
      root.querySelector<HTMLSelectElement>(`#${id}`)?.addEventListener('change', () => {
        void loadShops()
      })
    }
    await loadShops()
  }

  reload()
  // 区块在来源表之后初始化（主表的取数先发起 ⇒ `table.reloadData` 的调用顺序稳定：
  // 主表在前、店铺子表在后；页测试按 id 取，不靠下标）。
  void initShopsBlock()

  return () => {
    root.removeEventListener('click', onSmsClick, true)
    if (layerIndex !== null) (layer.close as (i: number) => void)(layerIndex)
    root.innerHTML = ''
  }
}

export function mount(root: HTMLElement, ctx: IslandContext): Promise<() => void> {
  return mountSourelistPage(root, ctx)
}

export default { mount } satisfies IslandPage
