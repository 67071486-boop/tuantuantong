/**
 * 【第三方平台】→「闲管家店铺对接」页（island key = `thirdparty-xgjshop`）。
 *
 * ## 这一页是**自有功能**，不是复刻
 *
 * 对标物（鸡蛋铺）里**没有**这一页，所以本页**没有证据件**、也不走 `mountConfigLike`
 * （那是「取 `public/replica/<key>.html` → 注入 → 灌数据」的复刻专用链路）。
 * 标记由本文件自己写，风格照 `system-pddshipsetting.ts` 那一页的 layui 惯例
 * （业务方 2026-09-22 四问里选定「layui island，与『其它平台店铺对接』同风格」）。
 *
 * ## 一页六件事
 *
 * 1. **四步图文引导**（业务方 R9）：照两张截图把应用建出来、把 AppKey/AppSecret 填过来；
 * 2. **版本声明**（业务方 R10 / spec per-source §3-D11）：店铺须为**铂金版**或 **ERP 专业版**
 * 才能识别订单——**只声明 + 提醒，不做程序判定**（上游不返回版本名，硬猜就是编）；
 * 3. **配置 + 测试连接 + 店铺同步**：`XgjShopBinding` 只管应用凭据（**没有**「对应订单来源」了：
 * 来源归**店铺**），保存后自动同步一次，行上还有「同步店铺」；
 * 4. **店铺表**：一个应用下的店铺 → 各自对应的订单来源（「一个来源 = 一家店」），
 * 订购异常的**标黄**；
 * 5. **录单识别的「待付款」开关**（线 F，业务方 2026-10-15）：业务方原话是「闲管家录单识别
 * 遇到待付款应该拒绝」，并追加「这是个开关、在那个页面可以人工配置」⇒ 开关落在**本页**
 * （【第三方平台】→「闲管家店铺对接」）。值走**现成的**系统开关端点
 * （`GET/PATCH /api/business/system-switches/`），⛔ 不新增端点、不新增权限节点。
 * ⛔ **fail-closed 渲染**（`D-0154` / `E-0098` 的既定手法，照 `index-console.ts::visibleShortcuts`）：
 * 没有**读**权限（`system.settings`）或没有**写**权限（`system.edit_settings`）⇒ **不画**这个控件
 * （⛔ 不置灰、⛔ 不画一个点了会 403 的控件）；身份取不到（401 / 网络）也按「没有节点」处理。
 * 6. **另一块开关**（与第 5 件同一套机制、同一道 fail-closed 节点门）：
 * 「大厅优选模拟单」（D-0296）与「接单大厅优选订单」（D-0328）——见下面各自的常量区注释。
 * ⛔ 曾有第四块「向下游同步上家大厅单」（D-0329），2026-10-29 业务方拍板
 * 「同步订单是一整个功能」**整块撤除**：该功能随发版自动生效、不设租户总闸，
 * 下家收不收由绑定自己的「启用同步 + 同步大厅订单」两开关管。别再往本页加回它。
 *
 * ## 四条红线（改这一页之前先读）
 *
 * 1. **`app_secret` 永不回显**（spec §9-S1）：列表只显示「已配置 / 未配置」，弹层密码框
 * **永远留空**、placeholder 写「留空表示不修改」。⚠️ 后端根本不回它（`write_only`）——
 * 别为了"方便核对"去把它接上。
 * 2. **两个推送地址是只读的**（spec §3-D5）：它们是**我们**暴露给闲管家的回调地址，由后端按
 * 租户域名生成。页面上**必须按实现写明「这列收什么、改了哪些数据」**（2026-10-04 起接收端已建，
 * 见 `docs/specs/2026-10-04-xgj-push-inbound-design.md`）：订单推送只在闲鱼那边把订单变成
 * 「已退款 / 已关闭」时，按**来源单号**把本系统的单改成「已退单」并写订单日志
 * （**状态变更 + 退单原因**，一次成功退单是**两条**日志，见 spec §D5），**已结账的单
 * 不改状态、只单独写一条日志**留给人工；商品推送**只做接收确认、一个字节都不写**。
 * ⛔ 两列**不许合成一句「都会入库」**（那是商品推送里不存在的行为）；⛔ 也不许写回旧文案那句
 * 「尚未启用」（旧 spec §12-1 的「推送会打到 404」已被本线更正，见该 spec 文末的更正块）。
 * 3. **查详情 / 测试连接 / 同步店铺都只按行 id 走**（spec §9-S3）：页面不持有、也不发送任何凭据。
 * 4. **标黄 ≠ 不是铂金版**（D11）：那一列显示的是**订购状态**，不是版本；文案不许写成
 * 「版本不对」，也不许拿它当"不让保存 / 不让对接"的闸门。
 *
 * ## 三条 layui 行为（都在 vendored 源码里核过，别按直觉改）
 *
 * 1. **弹层会把节点搬出 `<form>`**：`layer.open({type:1, content: $(node)})` 之后那个节点住在
 * `.layui-layer-content` 里，`form.on('submit(...)')` 的 `data.field` 取的是
 * 「**最近的 `.layui-form` 祖先**里的输入」（layui `form.js`：`a.parents(".layui-form")`）
 * ⇒ 节点被搬走后 `data.field` 会是**空的**。
 * 对策有两条，本页**都用**：① 弹层节点自己带 `layui-form` 类（搬走后仍是一个表单作用域）；
 * ② 保存时**直接从 DOM 读值**（照 `system-pddshipsetting.ts:475-478` 的先例）——
 * 这样即使 layui 的取值口径变了，本页也不会静默存进空表单。
 * ⚠️ 顺带核过：`form.render()` 的作用域是 `$('.layui-form')`（**整篇文档**，`form.js` 的
 * `a = O(h + (t ? '[lay-filter="…"]' : ""))`，`h = ".layui-form"`）⇒ 节点搬进 `.layui-layer-content`
 * 之后 `form.render()` **照样**找得到它，弹层里的 `<select>` 会被画出来。
 * 2. **`table` 实例必须先 `render` 一次**：`reloadData(id, {data})` 按实例 id 查表，
 * 没 render 过就只打一行 warn 然后什么都不做。且**不要**在 `render` 里给 `url`
 * （给了之后 `reloadData(id,{data})` 会走 ajax 分支、忽略我们传的 `data`）。
 * ⚠️ 顺带核过：`reloadData` 会**只保留** `data` / `templet` / `content` 三个键
 * （`table.js`：`new RegExp("^(data|templet|content)$")` 不匹配就 `delete`），其余键丢掉
 * ⇒ 别指望用 `reloadData` 顺手改别的渲染参数。
 * 3. **`layer.closeAll('page')` 关的是页面层**：它比对的是元素上的 `type` 属性，而那个属性存的是
 * **名字**（源码里有 `"page"===l.attr("type")` 的比较），不是 `1`；本仓
 * `system-roleauthority.ts:403` 用的也是同一句（先例）。
 *
 * ## 一个刻意的取舍：**标黄打在单元格上，不打在整行上**
 *
 * layui 的 `table` **没有**逐行 class 的口子（`rowClass` 之类不存在），要染整行只能去 `done`
 * 回调里手摸 DOM（`tr[data-index]`）。那是一条**测试盖不到**的路径（本仓 `E-0061` 的教训：
 * 看不见 DOM 操作的测试等于没测），所以这里把「订购异常」做成**订购状态列**里醒目的一格
 * （黄底 + 文案）。要改成整行染色，请连测试一起改（`thirdparty-xgjshop.spec.ts` 钉的是 templet）。
 *
 * ## 引导文案里的菜单名以**截图**为准
 *
 * 业务方口述的是「左侧**开发**平台」，而截图里闲管家工作台的实际菜单是「**开放**平台」
 * （2026-09-22 逐字核过截图上的字样）。引导是"照着图点"，所以这里写**开放平台**——
 * 写口述那个词会让租户在界面上找不到入口。
 */
import {
  createXgjBinding,
  deleteXgjBinding,
  fetchXgjBindings,
  fetchXgjShops,
  queryXgjOrderDetail,
  syncXgjShops,
  testXgjConnection,
  updateXgjBinding,
  type XgjOrderDetail,
  type XgjShopBindingRow,
  type XgjShopRow,
  type XgjSyncResult,
} from '@/api/xgjBindings'
import { fetchMe } from '@/api/auth'
// 线 F（业务方 2026-10-15）：录单识别的「待付款」开关走**现成的**系统开关端点
// （`api/systemSwitches.ts`；⛔ 本页不自建第三个出口、也不新增权限节点）。
import { fetchSystemSwitches, updateSystemSwitch } from '@/api/systemSwitches'
import { escapeHtml } from '@/layui-island/escape'
import { canNode, type NodeViewerInput } from '@/layui-island/perms'
import { bootstrapIsland, initIslandElements, islandMsg, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

/** island key（= 文件名 = 路由 `meta.islandPage`，三处必须逐字一致）。 */
export const PAGE_KEY = 'thirdparty-xgjshop'
export const TITLE = '闲管家店铺对接'

/** 线 F 那条开关的 key（与后端 `apps/business/system_switches.py` 的注册表同源）。 */
export const UNPAID_GUARD_SWITCH_KEY = 'reject_unpaid_xgj_order'
/** 控件容器的 **class 名**（拼进标记）与**选择器**（查节点）——两者不能混（E-0061 ①）。 */
export const UNPAID_GUARD_BLOCK_CLASS = 'xgjUnpaidGuardBlock'
export const UNPAID_GUARD_BLOCK_SELECTOR = `.${UNPAID_GUARD_BLOCK_CLASS}`
/**
 * 承载控件的**占位容器**（标记里只有一个空壳）。
 *
 * ⚠️ 这是 fail-closed 渲染的落点：没权限时它**永远是空的**（一个字节的控件都不进 DOM），
 * ⛔ 不是"先画出来再隐藏/置灰"（那正是 `D-0154` / `E-0098` 要修的形状）。
 */
export const UNPAID_GUARD_HOST_ID = 'xgjUnpaidGuardHost'
export const UNPAID_GUARD_HOST_SELECTOR = `#${UNPAID_GUARD_HOST_ID}`
/** 复选框的 `name`（保存时按它从 DOM 读选中态——本页的既定纪律：不信 layui 的表单取值）。 */
export const UNPAID_GUARD_INPUT_NAME = 'reject_unpaid_xgj_order'
/** 保存按钮的 `lay-filter`。 */
export const UNPAID_GUARD_SAVE_FILTER = 'xgjUnpaidGuardSave'
/**
 * 控件要的两个 RBAC 节点（**与后端两个端点同源**，不是本页另立的判据）：
 * `GET /api/business/system-switches/` 要 `system.settings`、
 * `PATCH .../<key>/` 要 `system.edit_settings`。
 */
export const SWITCHES_READ_NODE = 'system.settings'
export const SWITCHES_WRITE_NODE = 'system.edit_settings'

/* ── 「大厅优选模拟单」开关（D-0296，2026-10-24）────────────────────────
 *
 * 与「录单识别设置」那块同一套机制（`api/systemSwitches.ts` 的现成端点 +
 * checkbox `lay-skin="switch"` + 保存按钮），只是 key / 容器 / 事件名各有一套常量。
 * ⚠️ 三块**互不拖累**：各自的 mount 各自查值、各自上屏、保存失败各自回滚——
 * 一块出任何意外都不许把另一块（或本页原有的四件事）一起拖垮。
 */

/** 开关 key（与后端 `system_switches.py` 注册表逐字同源；key 恰为此串，默认 N）。 */
export const HALL_SIM_SWITCH_KEY = 'hall_simulated_orders'
/** 控件容器的 class 名与选择器（两者不能混，E-0061 ①）。 */
export const HALL_SIM_BLOCK_CLASS = 'hallSimSwitchBlock'
export const HALL_SIM_BLOCK_SELECTOR = `.${HALL_SIM_BLOCK_CLASS}`
/** 占位容器（fail-closed 渲染的落点，语义与 `UNPAID_GUARD_HOST_*` 同一条）。 */
export const HALL_SIM_HOST_ID = 'hallSimSwitchHost'
export const HALL_SIM_HOST_SELECTOR = `#${HALL_SIM_HOST_ID}`
/** 复选框的 `name`（保存时按它从 DOM 读选中态）。 */
export const HALL_SIM_INPUT_NAME = 'hall_simulated_orders'
/** 保存按钮的 `lay-filter`。 */
export const HALL_SIM_SAVE_FILTER = 'hallSimSwitchSave'

/* ── 「接单大厅优选订单」开关（D-0328，2026-10-29 业务方「把优选关掉」）──────
 *
 * 与上面两块同一套机制（`api/systemSwitches.ts` 现成端点 + `lay-skin="switch"`
 * + 保存按钮），key / 容器 / 事件名另有一套常量。⚠️ 三块**互不拖累**：各自
 * mount 各自查值、各自上屏、保存失败各自回滚——一块出任何意外都不许把别的
 * 块（或本页原有的四件事）一起拖垮。
 */

/** 开关 key（与后端 `system_switches.py` 注册表逐字同源；默认 Y = 存量行为）。 */
export const HALL_PREFERRED_SWITCH_KEY = 'hall_preferred_orders'
/** 控件容器的 class 名与选择器（两者不能混，E-0061 ①）。 */
export const HALL_PREFERRED_BLOCK_CLASS = 'hallPreferredSwitchBlock'
export const HALL_PREFERRED_BLOCK_SELECTOR = `.${HALL_PREFERRED_BLOCK_CLASS}`
/** 占位容器（fail-closed 渲染的落点，语义与 `HALL_SIM_HOST_*` 同一条）。 */
export const HALL_PREFERRED_HOST_ID = 'hallPreferredSwitchHost'
export const HALL_PREFERRED_HOST_SELECTOR = `#${HALL_PREFERRED_HOST_ID}`
/** 复选框的 `name`（保存时按它从 DOM 读选中态）。 */
export const HALL_PREFERRED_INPUT_NAME = 'hall_preferred_orders'
/** 保存按钮的 `lay-filter`。 */
export const HALL_PREFERRED_SAVE_FILTER = 'hallPreferredSwitchSave'

/* ⛔ 曾有第四块「向下游同步上家大厅单」开关（D-0329，2026-10-28），2026-10-29 整块撤除
 * （业务方拍板「同步订单是一整个功能」：该功能随发版自动生效、不设租户总闸；后端闸改
 * 常量水位，见 `upstream_hall_shadow.UPSTREAM_THROW_SINCE`）。别再往本页加回它。 */

/** 应用表 `elem` / `id`：layui 的 `reloadData` / `on` 要的是 **id**（带 `#` 是静默 no-op）。 */
export const TABLE_ELEM = '#xgjTableList'
export const TABLE_ID = 'xgjTableList'
/** 下方**店铺表**的 elem / id（与应用表是两个独立实例）。 */
export const SHOP_TABLE_ELEM = '#xgjShopTableList'
export const SHOP_TABLE_ID = 'xgjShopTableList'
/** 编辑弹层的容器 **class 名**（拼进标记用）与**选择器**（查节点用）——两者不能混。 */
export const PANEL_CLASS = 'xgjEditDiv'
export const PANEL_SELECTOR = `.${PANEL_CLASS}`
/** 查详情弹层的容器 class 名 / 选择器。 */
export const DETAIL_PANEL_CLASS = 'xgjDetailDiv'
export const DETAIL_PANEL_SELECTOR = `.${DETAIL_PANEL_CLASS}`

/**
 * 静态资源前缀。
 *
 * `public/` 下的文件随部署基址变（`import.meta.env.BASE_URL`，可能挂在 `/console/` 这种子路径），
 * 所以**不能**写死 `/guide/...`——写死之后换基址就静默 404（图上是空白，页面不报错）。
 */
const ASSET_BASE = import.meta.env.BASE_URL || '/'

/** 引导图：业务方 2026-09-22 给的两张闲管家界面截图（AppSecret 已打码，spec §3-D10）。 */
export const GUIDE_IMAGES = [
  {
    src: `${ASSET_BASE}guide/xgj-add-app.png`,
    alt: '闲管家工作台：左侧「开放平台 → 应用」，右上角「添加应用」',
    caption: '① 左侧「开放平台 → 应用」，右上角「添加应用」',
  },
  {
    src: `${ASSET_BASE}guide/xgj-app-key.png`,
    alt: '闲管家「应用概况」：应用类型=进销存、应用模式=自研系统，AppKey / AppSecret，右上角「确认上线」',
    caption: '② 「应用概况」里的 AppKey / AppSecret，右上角「确认上线」',
  },
] as const

/**
 * 控件该不该画（**做成本模块的纯函数**：页测试直接驱动它，判据只有这一处）。
 *
 * ⚠️ **两个节点缺一不可**：读（`GET` 列表）与写（`PATCH` 保存）是两个端点、两个节点
 * ⇒ 只给其一都不画（只给读 = 画一个点了 403 的保存键；只给写 = 画一个勾选态不明的开关）。
 * ⚠️ 身份取不到时调用方传 `{}` ⇒ 两个 `canNode` 都假 ⇒ 不画（**fail-closed**）。
 */
export function canManageSwitchBlock(me: NodeViewerInput): boolean {
  return canNode(me, SWITCHES_READ_NODE) && canNode(me, SWITCHES_WRITE_NODE)
}

/**
 * 那一块「录单识别设置」的标记（**纯函数，便于测试**）。
 *
 * `value` 是**后端**给的当前值（`"Y"` ⇒ 勾上）。只有 `mount` 在权限门 + 取值都成功之后才
 * 生成它并塞进占位容器；没权限 / 没读到值 ⇒ 这段标记**根本不会被生成**
 * （所以⛔ 不存在"先画一个勾选态不明的开关、再撤掉"那种瞬时态）。
 */
export function unpaidGuardBlockHtml(value: 'Y' | 'N'): string {
  const checked = value === 'Y' ? ' checked' : ''
  return `
<div class="${UNPAID_GUARD_BLOCK_CLASS} layui-form">
  <blockquote class="layui-elem-quote layui-quote-nm">
    <div style="font-size:14px;font-weight:700;margin-bottom:6px">录单识别设置</div>
    <div class="layui-form-item" style="margin-bottom:0">
      <label class="layui-form-label" style="width:auto;padding-left:0">录单识别闲管家订单时，待付款状态直接拒绝</label>
      <div class="layui-input-block" style="margin-left:0">
        <input type="checkbox" name="${UNPAID_GUARD_INPUT_NAME}" lay-skin="switch" lay-text="开|关"${checked}>
      </div>
    </div>
    <div style="color:#666;margin:6px 0">
      开启后：录单页读到「<b>待付款</b>」的闲管家订单会直接拒绝识别（只拦明确是待付款的；
      上游没给订单状态的一律照读）。<b>只对闲管家那条腿生效</b>——阿奇索的订单状态码语义不同、不受本开关管。
    </div>
    <div class="layui-form-item" style="margin-bottom:0">
      <button type="button" class="layui-btn layui-btn-sm" lay-submit lay-filter="${UNPAID_GUARD_SAVE_FILTER}">保存</button>
    </div>
  </blockquote>
</div>`
}

/**
 * 「大厅优选模拟单」那块的标记（**纯函数**，形状与 `unpaidGuardBlockHtml` 同一族）。
 *
 * `value` 是后端给的当前值（`"Y"` ⇒ 开）。标题与说明文案是业务方给的：
 * 标题「大厅优选模拟单」、说明「开启后每款游戏自动挂 2~3 张模拟优选单，仅在大厅展示」。
 * ⚠️ 只有 mount 在权限门 + 取值都成功之后才会生成它；没权限 / 没读到值 ⇒ 根本不会生成。
 */
export function hallSimBlockHtml(value: 'Y' | 'N'): string {
  const checked = value === 'Y' ? ' checked' : ''
  return `
<div class="${HALL_SIM_BLOCK_CLASS} layui-form">
  <blockquote class="layui-elem-quote layui-quote-nm">
    <div style="font-size:14px;font-weight:700;margin-bottom:6px">大厅优选模拟单</div>
    <div class="layui-form-item" style="margin-bottom:0">
      <label class="layui-form-label" style="width:auto;padding-left:0">接单大厅挂模拟优选单（演示卡）</label>
      <div class="layui-input-block" style="margin-left:0">
        <input type="checkbox" name="${HALL_SIM_INPUT_NAME}" lay-skin="switch" lay-text="开|关"${checked}>
      </div>
    </div>
    <div style="color:#666;margin:6px 0">
      开启后每款游戏自动挂 2~3 张模拟优选单，仅在大厅展示（不建真实订单；打手点接手会提示已被接走）。
    </div>
    <div class="layui-form-item" style="margin-bottom:0">
      <button type="button" class="layui-btn layui-btn-sm" lay-submit lay-filter="${HALL_SIM_SAVE_FILTER}">保存</button>
    </div>
  </blockquote>
</div>`
}

/**
 * 「接单大厅优选订单」那块的标记（**纯函数**，形状与 `hallSimBlockHtml` 同一族）。
 *
 * `value` 是后端给的当前值（`"Y"` ⇒ 开，存量行为）。说明文案写清**两向语义**
 * （D-0328）：开 = 角标 + 完成数闸 + 优选强制押金；关 = 不标优选、不闸、高价单
 * 按普通规则接（押金总闸开着时照常拦）。
 * ⚠️ 只有 mount 在权限门 + 取值都成功之后才会生成它；没权限 / 没读到值 ⇒ 根本不会生成。
 */
export function hallPreferredBlockHtml(value: 'Y' | 'N'): string {
  const checked = value === 'Y' ? ' checked' : ''
  return `
<div class="${HALL_PREFERRED_BLOCK_CLASS} layui-form">
  <blockquote class="layui-elem-quote layui-quote-nm">
    <div style="font-size:14px;font-weight:700;margin-bottom:6px">接单大厅优选订单</div>
    <div class="layui-form-item" style="margin-bottom:0">
      <label class="layui-form-label" style="width:auto;padding-left:0">接单大厅优选订单（高价标记与接单闸）</label>
      <div class="layui-input-block" style="margin-left:0">
        <input type="checkbox" name="${HALL_PREFERRED_INPUT_NAME}" lay-skin="switch" lay-text="开|关"${checked}>
      </div>
    </div>
    <div style="color:#666;margin:6px 0">
      开启后：发单价 &gt; 200 的大厅单亮<b>优选</b>角标；个人打手接优选单要先<b>完成过订单</b>，优选单<b>强制冻结押金</b>（不依赖押金总闸）。<br>
      关闭后：大厅不再标优选、「只看优选」筛选清空；接单不再要求完成过订单、也不强制押金——高价单按普通规则接（押金总闸开着时照常拦；模拟单照常生成）。
    </div>
    <div class="layui-form-item" style="margin-bottom:0">
      <button type="button" class="layui-btn layui-btn-sm" lay-submit lay-filter="${HALL_PREFERRED_SAVE_FILTER}">保存</button>
    </div>
  </blockquote>
</div>`
}

/**
 * 上游订单字段 → 中文标签（**只给显示用**）。
 *
 * ⚠️ 字段名出自官方接口文档的 `order_detail` schema（spec §2.2），不是猜的。
 * ⚠️ **未登记的字段照样显示**，标签回落到英文原名（见 `renderDetailRows`）——
 * 上游加字段时这一页不会漏显示，只是标签是英文。**别**把未登记字段丢掉：
 * 「查详情」这一页的价值就是"上游给了什么就看得见什么"。
 */
export const DETAIL_LABELS: Record<string, string> = {
  order_no: '闲鱼订单号',
  order_status: '订单状态原码',
  order_time: '买家下单时间',
  total_amount: '订单下单金额（分）',
  pay_amount: '订单实付金额（分）',
  pay_time: '支付时间',
  pay_no: '支付宝交易号',
  refund_status: '退款状态',
  refund_amount: '已退款金额（分）',
  refund_time: '退款时间',
  receiver_name: '收货人',
  receiver_mobile: '收货人电话',
  prov_name: '收货省份',
  city_name: '收货城市',
  area_name: '收货地区',
  town_name: '收货街道',
  address: '收货详细地址',
  waybill_no: '快递单号',
  express_code: '快递公司代码',
  express_name: '快递公司',
  express_fee: '运费（分）',
  consign_time: '发货时间',
  consign_type: '发货类型',
  confirm_time: '成交时间',
  cancel_reason: '取消原因',
  cancel_time: '取消时间',
  create_time: '订单创建时间',
  update_time: '订单更新时间',
  buyer_nick: '买家昵称',
  buyer_eid: '买家标识',
  seller_name: '卖家会员名',
  seller_eid: '卖家标识',
  seller_remark: '卖家备注',
  order_type: '订单类型原码',
  idle_biz_type: '子业务类型',
  pin_group_status: '拼团状态',
  is_tax_included: '是否含税',
  is_privacy: '是否隐私号订单',
}

/** `goods` 是**嵌套对象**：展平成与顶层同级显示（`renderDetailRows`）。 */
const GOODS_LABELS: Record<string, string> = {
  title: '商品标题',
  price: '商品单价（分）',
  quantity: '购买数量',
  outer_id: '商家编码',
  sku_id: '管家SKU规格ID',
  sku_outer_id: '商家SKU编码',
  sku_text: 'SKU规格',
  product_id: '管家商品ID',
  item_id: '闲鱼商品ID',
  service_support: '商品服务项',
  images: '商品主图',
}

/**
 * 页面标记。
 *
 * ⚠️ 两个弹层**自带 `layui-form` 类**：它们在打开时会被 layui 搬进 `.layui-layer-content`，
 * 带上表单作用域之后 `form.render()` 才画得到里面的 `<select>`（见文件头坑 1）。
 * ⚠️ 工具栏与行操作走 layui 的**模板 `id`**（`toolbar: '#xgjToolbar'` / 列上 `toolbar: '#xgjBar'`）：
 * 自己在 DOM 里拼一个按钮条**不会**触发 `table.on('toolbar(...)')`——那条事件是 layui 给
 * 它自己渲染出来的工具栏发的。
 */
function buildHtml(): string {
  const figures = GUIDE_IMAGES.map(
    (img) => `
    <figure style="margin:0;flex:1 1 300px;min-width:260px">
      <img src="${img.src}" alt="${escapeHtml(img.alt)}"
           style="width:100%;border:1px solid #e6e6e6;border-radius:2px">
      <figcaption style="color:#666;font-size:12px;margin-top:4px">${escapeHtml(img.caption)}</figcaption>
    </figure>`,
  ).join('')

  return `
<script type="text/html" id="xgjToolbar">
  <button class="layui-btn layui-btn-sm" lay-event="add">新增对接</button>
  <button class="layui-btn layui-btn-sm layui-btn-primary" lay-event="query">查询订单详情</button>
  <button class="layui-btn layui-btn-sm layui-btn-primary" lay-event="refresh">刷新</button>
</script>
<script type="text/html" id="xgjBar">
  <a class="layui-btn layui-btn-xs" lay-event="edit">修改</a>
  <a class="layui-btn layui-btn-xs layui-btn-primary" lay-event="sync">同步店铺</a>
  <a class="layui-btn layui-btn-xs layui-btn-primary" lay-event="test">测试连接</a>
  <a class="layui-btn layui-btn-xs layui-btn-danger" lay-event="del">删除</a>
</script>

<blockquote class="layui-elem-quote layui-quote-nm" id="xgjGuide">
  <div style="font-size:15px;font-weight:700;margin-bottom:6px">四步接好闲管家（照着下面两张图点）</div>
  <ol style="margin:0 0 10px 18px;line-height:24px">
    <li>打开<b>闲管家工作台</b>，左侧菜单点「<b>开放平台</b>」→「<b>应用</b>」。</li>
    <li>点右上角「<b>添加应用</b>」：应用模式选「<b>我有自研系统</b>」，
        应用类型选「<b>进销存</b>」，应用名称可填你工作室的名称。
        <span style="color:#999">（「添加应用」表单里的那一项叫「我有自研系统」；
        建好之后在「应用概况」里会显示成「自研系统」——两张截图只拍到了后者。）</span></li>
    <li>添加成功后，在「应用概况」里复制 <b>AppKey</b> 与 <b>AppSecret</b>，
        回到本页点「<b>新增对接</b>」把它们填进来。</li>
    <li>回到闲管家点「<b>确认上线</b>」；再回本页点这一行的「<b>同步店铺</b>」，
        把店铺同步成本系统的订单来源。</li>
  </ol>
  <div style="display:flex;gap:12px;flex-wrap:wrap">${figures}
  </div>
</blockquote>

<blockquote class="layui-elem-quote" id="xgjVersionNotice"
            style="border-left:5px solid #FFB800;background:#FFFBF0">
  <b style="color:#FF5722">店铺版本要求：店铺必须是「铂金版」或「ERP 专业版」，才能识别订单。</b>
  <br>
  低于这两个版本的店铺，闲管家开放平台<b>读不到</b>它的订单——这不是团团通能绕过的限制，
  请先到闲管家把店铺版本升到铂金版或 ERP 专业版。
  <br>
  <span style="color:#666">
    下面店铺表的「订购状态」标黄，只表示「订购看起来无效或已过期」，
    <b>标黄 ≠ 不是铂金版</b>；反过来，不标黄的店也<b>不保证</b>一定能读到订单
    （最终以闲管家接口返回为准）。
  </span>
</blockquote>

<blockquote class="layui-elem-quote layui-quote-nm">
  在这里登记<b>你自己</b>在闲管家开放平台创建的「自研应用」：填 <b>AppKey / AppSecret</b>。
  保存后会自动按<b>店铺名</b>生成订单来源（<b>一家店 = 一个订单来源</b>），
  录单时填了单号就自动判定是哪家店。
  <br>
  <span style="color:#FF5722">
    一个订单来源只绑一边：同一个来源如果在「其它平台店铺对接」里也绑了阿奇索，
    <b>录单会走阿奇索</b>，这里配的这一行<b>不会生效</b>。
  </span>
  <br>
  <span style="color:#FF5722">
    推送接收已启用（下面两个地址由本系统按当前域名生成）：<b>订单推送</b>只在闲鱼那边把订单变成
    「<b>已退款</b>」或「<b>已关闭</b>」时，按<b>来源单号</b>找到本系统的订单改成「<b>已退单</b>」
    并写订单日志（状态变更 + 退单原因）；<b>已结账</b>的单不改状态，只单独写一条日志留给人工处理；其他状态不做任何事。
    <b>商品推送只做接收确认，不改任何数据。</b>
  </span>
</blockquote>

<!-- 录单识别设置（线 F）的**占位容器**：里面一个字节的控件都没有——有读+写权限时
     mount 才把 unpaidGuardBlockHtml() 的标记塞进来（没权限时它永远是空的，fail-closed）。 -->
<div id="${UNPAID_GUARD_HOST_ID}"></div>

<!-- 「大厅优选模拟单」开关（D-0296）的**占位容器**：同上，fail-closed 空壳。 -->
<div id="${HALL_SIM_HOST_ID}"></div>

<!-- 「接单大厅优选订单」开关（D-0328）的**占位容器**：同上，fail-closed 空壳。 -->
<div id="${HALL_PREFERRED_HOST_ID}"></div>

<!-- ⛔ 曾有第四块「向下游同步上家大厅单」开关（D-0329）的占位容器，2026-10-29 撤除
     （业务方拍板「同步订单是一整个功能」：随发版自动生效、不设租户总闸）。 -->

<table id="${TABLE_ID}" lay-filter="${TABLE_ID}"></table>

<div style="font-size:14px;font-weight:700;margin:16px 0 6px">
  店铺（一个应用最多 30 家店；每家店对应一个订单来源）
</div>
<table id="${SHOP_TABLE_ID}" lay-filter="${SHOP_TABLE_ID}"></table>

<div class="${PANEL_CLASS} layui-form" style="display:none;padding:20px 25px 0 0">
  <div class="layui-form-item">
    <label class="layui-form-label">应用备注名</label>
    <div class="layui-input-block">
      <input type="text" name="shop_name" class="layui-input" maxlength="64"
             placeholder="自己起的名字，例如：主应用">
    </div>
  </div>
  <div class="layui-form-item">
    <label class="layui-form-label">AppKey</label>
    <div class="layui-input-block">
      <input type="text" name="app_key" class="layui-input" maxlength="64"
             placeholder="闲管家开放平台 → 应用 → 应用概况里的 AppKey">
    </div>
  </div>
  <div class="layui-form-item">
    <label class="layui-form-label">AppSecret</label>
    <div class="layui-input-block">
      <input type="password" name="app_secret" class="layui-input" maxlength="128"
             autocomplete="new-password" placeholder="留空表示不修改">
      <div class="layui-form-mid layui-word-aux">
        为了安全，保存后不再回显；要换就直接填新的。
      </div>
    </div>
  </div>
  <div class="layui-form-item">
    <label class="layui-form-label">备注说明</label>
    <div class="layui-input-block">
      <input type="text" name="remark" class="layui-input" maxlength="200">
    </div>
  </div>
  <div class="layui-form-item">
    <div class="layui-input-block">
      <button type="button" class="layui-btn" lay-submit lay-filter="xgjSave">保存</button>
    </div>
  </div>
</div>

<div class="${DETAIL_PANEL_CLASS} layui-form" style="display:none;padding:20px 25px 0 0">
  <div class="layui-form-item">
    <label class="layui-form-label">应用</label>
    <div class="layui-input-block">
      <select name="binding"></select>
    </div>
  </div>
  <div class="layui-form-item">
    <label class="layui-form-label">订单号</label>
    <div class="layui-input-block">
      <input type="text" name="order_no" class="layui-input" maxlength="32"
             placeholder="闲鱼订单号（纯数字）">
    </div>
  </div>
  <div class="layui-form-item">
    <div class="layui-input-block">
      <button type="button" class="layui-btn" lay-submit lay-filter="xgjQuery">查询</button>
    </div>
  </div>
  <div class="xgjDetailResult" style="padding:0 0 10px 110px"></div>
</div>`
}

/** 弹层借走节点后要能找回来（layui 会把节点搬进 `.layui-layer-content`）。 */
function findPanel(selector: string): HTMLElement | null {
  return (
    document.querySelector<HTMLElement>(`.layui-layer-content > ${selector}`) ??
    document.querySelector<HTMLElement>(selector)
  )
}

/** 弹层关闭后把节点收回页面（否则第二次打开时取不到节点、弹层静默不出现）。 */
function reopenGuard(node: HTMLElement, host: HTMLElement): void {
  if (node.parentElement !== host) host.appendChild(node)
}

/** 取表格一行的原始数据（layui 把行数据挂在 `obj.data` 上）。 */
function rowOf(obj: unknown): XgjShopBindingRow {
  return (obj as { data: XgjShopBindingRow }).data
}

/** 在弹层节点里取一个输入（含 `<select>`）；节点不在文档里时回 `null`。 */
function fieldOf(panel: HTMLElement, name: string): string {
  const el = panel.querySelector<HTMLInputElement | HTMLSelectElement>(`[name="${name}"]`)
  return el?.value ?? ''
}

/**
 * 保存之后那句提示（**纯函数，便于测试**）。
 *
 * ⚠️ 后端「同步失败**不影响保存成功**」是有意的（凭据写错了要能改），
 * 所以 `code === 0` 也可能带 `sync.error`——**必须说出来**，否则租户会以为店铺已经同步好了
 * （那正是"点了没反应"的静默失败形状）。
 */
export function saveMessage(sync?: XgjSyncResult): string {
  if (!sync) return '已保存'
  if (sync.skipped) return `已保存；但${sync.skipped}，店铺还没同步`
  if (sync.error) {
    const code = sync.code === undefined ? '' : `（错误码 ${sync.code}）`
    return `已保存，但${sync.error}${code}——请检查 AppKey / AppSecret 是否复制完整`
  }
  return (
    `已保存，已同步 ${sync.created_shops ?? 0} 家新店铺、更新 ${sync.updated_shops ?? 0} 家、` +
    `新建 ${sync.created_sources ?? 0} 个订单来源`
  )
}

/**
 * 订购状态那一格（**纯函数，便于测试**）。
 *
 * ⚠️ **这不是版本判定**（D11）：上游不返回版本名。黄条只说"订购看起来无效或已过期"，
 * 页面上必须同时写着「标黄 ≠ 不是铂金版」——**不要**把它改成"版本不对"或拿它当闸门。
 *
 * ⚠️ **三态**：`is_valid` 与 `valid_end_time` 都为 `null` 表示"**还没同步到订购状态**"
 * （或上游这次没给这个键）⇒ 显示「—」而**不是**「正常」。把"没数据"渲染成"正常"是一句
 * 没有依据的断言（AGENTS.md §5 / E-0011 同族：页面会替上游担保一件它其实不知道的事）。
 */
export function subscriptionCell(row: XgjShopRow): string {
  if (row.subscription_warning) {
    return (
      '<span style="background:#FFF3CD;color:#8A6D3B;padding:0 4px;border:1px solid #FAEBCC">' +
      '订购异常：可能识别不到订单</span>'
    )
  }
  if (row.is_valid === null && row.valid_end_time === null) {
    return '<span style="color:#999">—（还没同步到订购状态）</span>'
  }
  return '<span style="color:#009688">正常</span>'
}

/**
 * 订单详情 → 「标签 / 值」两列（**纯函数，便于测试**）。
 *
 * `goods` 展平：嵌套表格在这一页没有价值，而漏显示会让人以为上游没给。
 * **未登记的字段照样显示**（标签回落到英文原名）。
 */
export function renderDetailRows(detail: XgjOrderDetail): Array<[string, string]> {
  const rows: Array<[string, string]> = []
  for (const [key, value] of Object.entries(detail)) {
    if (key === 'goods' && typeof value === 'object' && value !== null) {
      for (const [gKey, gValue] of Object.entries(value as Record<string, unknown>)) {
        rows.push([GOODS_LABELS[gKey] ?? `商品.${gKey}`, formatDetailValue(gValue)])
      }
      continue
    }
    rows.push([DETAIL_LABELS[key] ?? key, formatDetailValue(value)])
  }
  return rows
}

/** 值 → 显示串。`null`/`undefined` 显示空（**不是** `"null"`），对象/数组转 JSON。 */
function formatDetailValue(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

/**
 * 详情渲染成两列表格。⚠️ 值来自**第三方** ⇒ 一律 `escapeHtml` 后再拼
 * （买家昵称、商品标题都可能是任意文本）。
 */
export function detailRowsToHtml(rows: Array<[string, string]>): string {
  const body = rows
    .map(
      ([label, value]) =>
        `<tr><td style="width:150px;color:#666">${escapeHtml(label)}</td>` +
        `<td>${escapeHtml(value)}</td></tr>`,
    )
    .join('')
  return `<table class="layui-table" lay-size="sm" style="margin:0"><tbody>${body}</tbody></table>`
}

/** 纯函数出口（测试直接用；不依赖 layui）。 */
export const __testables = {
  renderDetailRows,
  detailRowsToHtml,
  buildHtml,
  saveMessage,
  subscriptionCell,
}

export default {
  mount: async (root) => {
    const layui: LayuiNamespace = await bootstrapIsland(['form', 'table', 'layer', 'util'])
    const form = layui.form as LayuiModule
    const table = layui.table as LayuiModule
    const layer = layui.layer as LayuiModule
    const $ = (layui as unknown as { $: (el: Element) => unknown }).$
    const msg = (text: string) => islandMsg(layui.layer as unknown as Record<string, unknown>, text)
    const render = () => (form.render as (type?: string) => void)()
    const reloadData = (id: string, data: unknown) =>
      (table.reloadData as (i: string, o: unknown) => void)(id, { data })

    document.title = TITLE
    root.innerHTML = buildHtml()
    initIslandElements(layui)

    /** 应用表数据（查详情弹层的下拉要用它）。 */
    let rows: XgjShopBindingRow[] = []
    /** 店铺表数据（跨应用拼起来的一张总表）。 */
    let shopRows: XgjShopRow[] = []
    /** 正在编辑的行 id（`null` = 新增）。 */
    let editing: number | null = null
    /**
 * 那个开关的**后端最后已知值**（`null` = 控件没画 / 还没读到）。
 * ⚠️ 保存失败时回滚到它——⛔ 不是回滚到"提交瞬间的 DOM 值"，那个等于用户刚点的值、等于没滚。
 */
    let guardLoaded: 'Y' | 'N' | null = null

    /**
 * 录单识别开关那块（线 F）：**先过权限门，再取后端值，都成了才上屏**。
 *
 * ⚠️ 顺序是承重的：
 * 1. `fetchMe()` 失败（401 / 网络）⇒ **按"没有节点"处理**（与 `order-orderlist.ts` 同口径）；
 * 2. 没有读**或**没有写节点 ⇒ **直接返回**：占位容器保持空的（⛔ 不画一个点了会 403 的控件，
 * ⛔ 也不去发那一条注定 403 的 `GET`）；
 * 3. 权限齐了才取值。**取不到值 ⇒ 一个字节都不画**（⛔ 不留一个勾选态不明的开关上屏），
 * 并把失败说出来（本页对取数失败一律 `msg`，不静默）。
 * ⚠️ 勾选态**只来自后端**：`value === 'Y'` ⇒ 勾上（注册表默认 `"Y"` ⇒ 无覆盖行时到这儿就是勾上的）。
 */
    async function mountUnpaidGuardBlock(): Promise<void> {
      const host = root.querySelector<HTMLElement>(UNPAID_GUARD_HOST_SELECTOR)
      if (!host) return
      let viewer: NodeViewerInput = {}
      try {
        viewer = await fetchMe()
      } catch {
        // fail-closed：身份取不到 ⇒ 不画（用户至少能正常用本页其它四件事）。
        return
      }
      if (!canManageSwitchBlock(viewer)) return

      let value: 'Y' | 'N'
      try {
        const { items } = await fetchSystemSwitches()
        const row = items.find((item) => item.key === UNPAID_GUARD_SWITCH_KEY)
        // 找不到那一条 = 前后端对不上（key 被改过 / 后端没上这个开关）⇒ 响亮地说出来，别静默画成"关"。
        if (!row) throw new Error('后端开关列表里没有「待付款拒绝识别」那一条')
        value = row.value
      } catch (e) {
        msg((e as Error).message)
        return
      }
      // 先把"后端最后已知值"记下来（保存失败时的回滚目标），再上屏 —— 顺序不能反。
      guardLoaded = value
      host.innerHTML = unpaidGuardBlockHtml(value)
      render()
    }

    /** 「大厅优选模拟单」开关的**后端最后已知值**（语义与 `guardLoaded` 同一条：保存失败回滚到它）。 */
    let hallSimLoaded: 'Y' | 'N' | null = null

    /**
 * 「大厅优选模拟单」开关块（D-0296）：闸门、顺序、失败处置与 `mountUnpaidGuardBlock`
 * **逐条同族**（fail-closed 两节点门 → 后端取值 → 都成了才上屏）。
 * ⚠️ 两块**各自 mount 各自查值**（不共用一次 fetch）：一块取数失败/没权限，另一块照常，
 * 也互不拖累本页原有的四件事。
 */
    async function mountHallSimBlock(): Promise<void> {
      const host = root.querySelector<HTMLElement>(HALL_SIM_HOST_SELECTOR)
      if (!host) return
      let viewer: NodeViewerInput = {}
      try {
        viewer = await fetchMe()
      } catch {
        // fail-closed：身份取不到 ⇒ 不画。
        return
      }
      if (!canManageSwitchBlock(viewer)) return

      let value: 'Y' | 'N'
      try {
        const { items } = await fetchSystemSwitches()
        const row = items.find((item) => item.key === HALL_SIM_SWITCH_KEY)
        // 找不到那一条 = 前后端对不上（后端还没上这个开关 / key 被改过）⇒ 响亮地说出来。
        if (!row) throw new Error('后端开关列表里没有「大厅优选模拟单」那一条')
        value = row.value
      } catch (e) {
        msg((e as Error).message)
        return
      }
      // 先记"后端最后已知值"（回滚目标），再上屏——顺序不能反。
      hallSimLoaded = value
      host.innerHTML = hallSimBlockHtml(value)
      render()
    }

    /** 「接单大厅优选订单」开关的**后端最后已知值**（语义与 `hallSimLoaded` 同一条）。 */
    let hallPreferredLoaded: 'Y' | 'N' | null = null

    /**
 * 「接单大厅优选订单」开关块（D-0328）：闸门、顺序、失败处置与
 * `mountHallSimBlock` **逐条同族**（fail-closed 两节点门 → 后端取值 → 都成了才上屏）。
 * ⚠️ 三块各自 mount 各自查值（不共用一次 fetch）：一块取数失败 / 没权限，另两块照常。
 */
    async function mountHallPreferredBlock(): Promise<void> {
      const host = root.querySelector<HTMLElement>(HALL_PREFERRED_HOST_SELECTOR)
      if (!host) return
      let viewer: NodeViewerInput = {}
      try {
        viewer = await fetchMe()
      } catch {
        // fail-closed：身份取不到 ⇒ 不画。
        return
      }
      if (!canManageSwitchBlock(viewer)) return

      let value: 'Y' | 'N'
      try {
        const { items } = await fetchSystemSwitches()
        const row = items.find((item) => item.key === HALL_PREFERRED_SWITCH_KEY)
        // 找不到那一条 = 前后端对不上（后端还没上这个开关 / key 被改过）⇒ 响亮地说出来。
        if (!row) throw new Error('后端开关列表里没有「接单大厅优选订单」那一条')
        value = row.value
      } catch (e) {
        msg((e as Error).message)
        return
      }
      // 先记"后端最后已知值"（回滚目标），再上屏——顺序不能反。
      hallPreferredLoaded = value
      host.innerHTML = hallPreferredBlockHtml(value)
      render()
    }

    function fillDetailBindingSelect(): void {
      const panel = findPanel(DETAIL_PANEL_SELECTOR)
      const select = panel?.querySelector<HTMLSelectElement>('select[name="binding"]')
      if (!select) return
      select.innerHTML = rows
        .map((r) => `<option value="${r.id}">${escapeHtml(r.shop_name || `#${r.id}`)}</option>`)
        .join('')
    }

    /** 「AppSecret」列：**只回答配没配**（spec §9-S1）。 */
    const secretCell = (row: XgjShopBindingRow): string =>
      row.app_secret_set
        ? '<span style="color:#009688">已配置</span>'
        : '<span style="color:#FF5722">未配置</span>'

    /**
 * 推送地址那一格 + 这列**按实现**收什么。
 *
 * ⚠️ 两列的说法**不一样**（spec §3-D5 / §12-1 已更正）：订单推送会**改单**（已退款 / 已关闭 ⇒
 * 已退单 + 订单日志，一次成功退单是**状态变更 + 退单原因**两条；已结账的单只写日志、不改状态），
 * 商品推送**只做接收确认**、一个字节都不写。
 * ⛔ 不许合成一句「都会入库」——那是商品推送里**不存在**的行为。
 */
    const ORDER_PUSH_NOTE = '已启用：按来源单号把已退款 / 已关闭的单改成已退单并写订单日志（状态变更 + 退单原因）'
    const PRODUCT_PUSH_NOTE = '已启用：只做接收确认，不改任何数据'
    const pushCell = (url: string, note: string): string =>
      url
        ? `<div style="word-break:break-all">${escapeHtml(url)}</div>` +
          `<div style="color:#666">（${note}）</div>`
        : ''

    ;(table.render as (o: unknown) => void)({
      elem: TABLE_ELEM,
      // ⚠️ 先 render 一次（哪怕空数据）：实例是在 render 时注册的；且**不给 `url`**（见文件头坑 2）。
      data: [] as XgjShopBindingRow[],
      toolbar: '#xgjToolbar',
      defaultToolbar: [],
      cols: [
        [
          { field: 'shop_name', title: '应用备注名', width: 140 },
          { field: 'app_key', title: 'AppKey', width: 180 },
          { title: 'AppSecret', width: 100, templet: (d: XgjShopBindingRow) => secretCell(d) },
          {
            title: '订单消息推送URL',
            minWidth: 240,
            templet: (d: XgjShopBindingRow) => pushCell(d.order_push_url, ORDER_PUSH_NOTE),
          },
          {
            title: '商品消息推送URL',
            minWidth: 240,
            templet: (d: XgjShopBindingRow) => pushCell(d.product_push_url, PRODUCT_PUSH_NOTE),
          },
          { field: 'remark', title: '备注说明', width: 130 },
          { title: '操作', width: 260, align: 'center', toolbar: '#xgjBar' },
        ],
      ],
      page: false,
      text: { none: '还没有配置闲管家店铺对接' },
    })
    ;(table.render as (o: unknown) => void)({
      elem: SHOP_TABLE_ELEM,
      data: [] as XgjShopRow[],
      defaultToolbar: [],
      cols: [
        [
          { field: 'shop_name', title: '店铺名称', minWidth: 160 },
          { field: 'user_name', title: '会员名', minWidth: 130 },
          {
            title: '对应来源',
            minWidth: 150,
            templet: (d: XgjShopRow) =>
              d.order_source_name
                ? escapeHtml(d.order_source_name)
                : '<span style="color:#FF5722">还没建（点「同步店铺」）</span>',
          },
          { field: 'binding_name', title: '所属应用', width: 140 },
          {
            title: '订购状态',
            width: 220,
            templet: (d: XgjShopRow) => subscriptionCell(d),
          },
        ],
      ],
      page: false,
      text: { none: '还没有同步到店铺（先保存应用，或点「同步店铺」）' },
    })
    render()

    /** 拉一次店铺表：**按应用逐个取再拼**（应用表不带店铺：一个应用最多 30 家店，一行放不下）。 */
    async function loadShops(): Promise<void> {
      if (rows.length === 0) {
        shopRows = []
        reloadData(SHOP_TABLE_ID, shopRows)
        return
      }
      try {
        const lists = await Promise.all(rows.map((r) => fetchXgjShops(r.id)))
        shopRows = lists.flatMap((l) => l.items)
      } catch (e) {
        // 店铺表取不到不该把整个页面弄成空白：**如实说出来**，表清空。
        shopRows = []
        msg((e as Error).message)
      }
      reloadData(SHOP_TABLE_ID, shopRows)
    }

    async function reload(): Promise<void> {
      const page = await fetchXgjBindings()
      rows = page.items
      reloadData(TABLE_ID, rows)
      fillDetailBindingSelect()
      await loadShops()
    }

    /**
 * 打开「新增 / 修改」弹层。
 *
 * ⚠️ **顺序是硬要求：「先预填、再 open」**——`layer.open` 会把节点搬出 `<form>`，
 * 先 open 再找输入就会找不到（先例 `system-pddshipsetting.ts:414-418`）。
 */
    function openEdit(isNew: boolean, row?: XgjShopBindingRow): void {
      editing = isNew ? null : (row?.id ?? null)
      const panel = findPanel(PANEL_SELECTOR)
      if (!panel) return
      const set = (name: string, value: string) => {
        const el = panel.querySelector<HTMLInputElement>(`[name="${name}"]`)
        if (el) el.value = value
      }
      set('shop_name', row?.shop_name ?? '')
      set('app_key', row?.app_key ?? '')
      // ⚠️ **永远留空**：后端不回 `app_secret`（spec §9-S1）。placeholder 说明「留空=不修改」。
      set('app_secret', '')
      set('remark', row?.remark ?? '')
      ;(layer.open as (o: unknown) => number)({
        type: 1,
        title: isNew ? '新增闲管家店铺对接' : '修改闲管家店铺对接',
        area: ['620px', '470px'],
        content: $(panel),
        end: () => reopenGuard(panel, root),
      })
      render()
    }

    ;(table.on as (event: string, cb: (obj: unknown) => void) => void)(
      `tool(${TABLE_ID})`,
      (obj) => {
        const row = rowOf(obj)
        const ev = (obj as { event: string }).event
        if (ev === 'edit') return openEdit(false, row)
        if (ev === 'del') {
          // ⚠️ `layer.confirm` 的 content 按 **HTML** 渲染（vendored layui 共用 dialog 模板，
          // E-0303）：备注名 / AppKey 是自由文本 ⇒ 逐插值过 `escapeHtml`（F-01）。
          ;(layer.confirm as (text: string, opts: unknown, ok: () => void) => void)(
            `确定删除「${escapeHtml(row.shop_name || row.app_key)}」这条对接吗？`,
            {},
            () => {
              void deleteXgjBinding(row.id)
                .then(() => reload())
                .then(() => msg('已删除'))
                .catch((e: Error) => msg(e.message))
            },
          )
          return
        }
        if (ev === 'sync') {
          // ⚠️ 同步**不改应用行**，只改店铺/来源 ⇒ 成功后就地刷店铺表，不必整页 reload。
          // 失败由 `.catch` 说出来（后端把上游失败翻成业务码，`request()` 抛 `BizError`）。
          void syncXgjShops(row.id)
            .then((result) =>
              loadShops().then(() =>
                msg(
                  `同步完成：新增店铺 ${result.created_shops ?? 0} 家、` +
                    `更新 ${result.updated_shops ?? 0} 家、新建来源 ${result.created_sources ?? 0} 个`,
                ),
              ),
            )
            .catch((e: Error) => msg(e.message))
          return
        }
        if (ev === 'test') {
          void testXgjConnection(row.id)
            .then(({ shops }) => {
              if (shops.length === 0) {
                // ⚠️ 空列表是**合法结果**（应用有效但一个店都没授权），与「调用失败」不是一回事
                return msg('连接成功，但这个应用还没有绑定任何闲鱼店铺')
              }
              const names = shops
                .map((s) =>
                  escapeHtml(String(s.shop_name ?? s.user_nick ?? s.user_name ?? '（无名）')),
                )
                .join('、')
              ;(layer.alert as (text: string, opts: unknown) => void)(
                `这个应用能操作 ${shops.length} 家店：<br>${names}`,
                { title: '测试连接' },
              )
            })
            .catch((e: Error) => msg(e.message))
        }
      },
    )

    ;(table.on as (event: string, cb: (obj: unknown) => void) => void)(
      `toolbar(${TABLE_ID})`,
      (obj) => {
        const ev = (obj as { event: string }).event
        if (ev === 'add') return openEdit(true)
        if (ev === 'refresh') {
          void reload()
            .then(() => msg('已刷新'))
            .catch((e: Error) => msg(e.message))
        }
        if (ev === 'query') {
          const panel = findPanel(DETAIL_PANEL_SELECTOR)
          if (!panel) return
          fillDetailBindingSelect()
          panel.querySelector('.xgjDetailResult')!.innerHTML = ''
          ;(layer.open as (o: unknown) => number)({
            type: 1,
            title: '查询闲管家订单详情',
            area: ['740px', '660px'],
            content: $(panel),
            end: () => reopenGuard(panel, root),
          })
          render()
        }
      },
    )

    /**
 * 保存。**直接从 DOM 读值**（照 `system-pddshipsetting.ts:475-478` 的先例）：
 * 弹层节点被搬出 `<form>` 后 layui 的 `data.field` 会是空的（见文件头坑 1）。
 */
    ;(form.on as (event: string, cb: () => void) => void)('submit(xgjSave)', () => {
      const panel = findPanel(PANEL_SELECTOR)
      if (!panel) return
      const payload = {
        shop_name: fieldOf(panel, 'shop_name').trim(),
        app_key: fieldOf(panel, 'app_key').trim(),
        // ⚠️ 空串照传：后端把空串当「不改」（spec §9-S4）。**不要**在这里过滤掉又换成"清空"的语义。
        app_secret: fieldOf(panel, 'app_secret').trim(),
        remark: fieldOf(panel, 'remark').trim(),
      }
      const done =
        editing === null ? createXgjBinding(payload) : updateXgjBinding(editing, payload)
      void done
        .then((saved) => {
          ;(layer.closeAll as (type?: string) => void)('page')
          // ⚠️ 保存会**自动同步一次店铺**（R5）⇒ 两张表都要重拉，提示里也要带上同步结果。
          return reload().then(() => msg(saveMessage(saved.sync)))
        })
        .catch((e: Error) => msg(e.message))
    })

    /** 查详情。入参同理由前端先挡一次（后端也会挡 ⇒ `1000`，但白跑一趟没有意义）。 */
    ;(form.on as (event: string, cb: () => void) => void)('submit(xgjQuery)', () => {
      const panel = findPanel(DETAIL_PANEL_SELECTOR)
      if (!panel) return
      const binding = Number(fieldOf(panel, 'binding'))
      const orderNo = fieldOf(panel, 'order_no').trim()
      const result = panel.querySelector<HTMLElement>('.xgjDetailResult')
      if (!/^[0-9]+$/.test(orderNo)) return msg('订单号只能是数字')
      if (!binding) return msg('请先选择应用')
      if (result) result.textContent = '查询中…'
      void queryXgjOrderDetail(binding, orderNo)
        .then(({ detail }) => {
          if (result) result.innerHTML = detailRowsToHtml(renderDetailRows(detail))
        })
        .catch((e: Error) => {
          if (result) result.innerHTML = ''
          msg(e.message)
        })
    })

    /**
 * 保存那个开关（线 F）：**从 DOM 读选中态**（本页既定纪律，见文件头坑 1），
 * 走**现成的** `PATCH /api/business/system-switches/<key>/`（⛔ 不新增端点）。
 *
 * ⚠️ 失败时把勾选态**回滚到后端最后已知值**（`guardLoaded`）：不回滚会把"没保存上"
 * 伪装成"已保存"（勾选态是这一块唯一的反馈面——先例 `system-systemsetting.ts::submit`）。
 * ⚠️ 这块没画出来时（没权限）这个处理器**永远收不到事件**，注册本身是无害的 no-op。
 */
    ;(form.on as (event: string, cb: () => void) => void)(
      `submit(${UNPAID_GUARD_SAVE_FILTER})`,
      () => {
        const input = root.querySelector<HTMLInputElement>(
          `${UNPAID_GUARD_BLOCK_SELECTOR} input[name="${UNPAID_GUARD_INPUT_NAME}"]`,
        )
        if (!input) return
        const value: 'Y' | 'N' = input.checked ? 'Y' : 'N'
        void updateSystemSwitch(UNPAID_GUARD_SWITCH_KEY, value)
          .then((saved) => {
            // 勾选态以**后端回的那一行**为准（不是我们刚发出去的那个值）。
            guardLoaded = saved.value
            input.checked = saved.value === 'Y'
            msg('已保存')
          })
          .catch((e: Error) => {
            if (guardLoaded !== null) input.checked = guardLoaded === 'Y'
            msg(e.message)
          })
      },
    )

    /**
 * 保存「大厅优选模拟单」开关（D-0296）：形状与上面那颗保存**逐条同族**——
 * 从 DOM 读选中态、走现成的 PATCH、失败回滚到 `hallSimLoaded`。
 * ⚠️ 这块没画出来时（没权限）这个处理器**永远收不到事件**，注册本身是无害的 no-op。
 */
    ;(form.on as (event: string, cb: () => void) => void)(
      `submit(${HALL_SIM_SAVE_FILTER})`,
      () => {
        const input = root.querySelector<HTMLInputElement>(
          `${HALL_SIM_BLOCK_SELECTOR} input[name="${HALL_SIM_INPUT_NAME}"]`,
        )
        if (!input) return
        const value: 'Y' | 'N' = input.checked ? 'Y' : 'N'
        void updateSystemSwitch(HALL_SIM_SWITCH_KEY, value)
          .then((saved) => {
            // 勾选态以后端回的那一行为准（不是刚发出去的那个值）。
            hallSimLoaded = saved.value
            input.checked = saved.value === 'Y'
            msg('已保存')
          })
          .catch((e: Error) => {
            if (hallSimLoaded !== null) input.checked = hallSimLoaded === 'Y'
            msg(e.message)
          })
      },
    )

    /**
 * 保存「接单大厅优选订单」开关（D-0328）：形状与上面两颗保存**逐条同族**——
 * 从 DOM 读选中态、走现成的 PATCH、失败回滚到 `hallPreferredLoaded`。
 * ⚠️ 这块没画出来时（没权限）这个处理器**永远收不到事件**，注册本身是无害的 no-op。
 */
    ;(form.on as (event: string, cb: () => void) => void)(
      `submit(${HALL_PREFERRED_SAVE_FILTER})`,
      () => {
        const input = root.querySelector<HTMLInputElement>(
          `${HALL_PREFERRED_BLOCK_SELECTOR} input[name="${HALL_PREFERRED_INPUT_NAME}"]`,
        )
        if (!input) return
        const value: 'Y' | 'N' = input.checked ? 'Y' : 'N'
        void updateSystemSwitch(HALL_PREFERRED_SWITCH_KEY, value)
          .then((saved) => {
            // 勾选态以后端回的那一行为准（不是刚发出去的那个值）。
            hallPreferredLoaded = saved.value
            input.checked = saved.value === 'Y'
            msg('已保存')
          })
          .catch((e: Error) => {
            if (hallPreferredLoaded !== null) input.checked = hallPreferredLoaded === 'Y'
            msg(e.message)
          })
      },
    )

    // ⚠️ 两条 **分开** 的 try：开关块是附加物，它出任何意外都⛔ **不许**把本页原有的
    // 四件事（引导 / 版本声明 / 配置+测试连接 / 两张表）一起拖垮（线 F 的判据 ④）。
    /**
 * 开关块与**本页自己的取数**并行跑，⛔ 开关块不许挡住两张表。
 *
 * ⚠️ 为什么这样排（评审 P2-2 实测的时序问题）：开关块要走两次请求
 * （`/api/auth/me`，有权限时再走一次 `/system-switches/`），而两张表是这一页的**主内容**。
 * 早先写成 `await mountUnpaidGuardBlock(); await reload()` ⇒ 表要等**串行两次**请求才开始拉
 * （多余的等待，且 `/system-switches/` 那一次与表毫无关系）。
 * 现在：先**起跑**（`mountUnpaidGuardBlock()` 一调用就发出 `/api/auth/me`，不 await），
 * 紧接着去 `reload()`，最后才回来收它的尾。
 * ⚠️ 起跑时必须**立刻**接住它的失败（`.catch`）：只 `await` 到结尾的话，中间一旦 reject
 * 就会被当成"未处理的 rejection"报出来。
 * ⚠️ 两条腿各自 `msg` 各自报错，互不拖累——本页原有的四件事（引导 / 版本声明 /
 * 配置+测试连接 / 两张表）行为一个字节都不变。
 */
    const guardBlock = mountUnpaidGuardBlock().catch((e: Error) => msg(e.message))
    // 「大厅优选模拟单」开关块（D-0296）同一起跑、各自收尾：三块互不拖累，也都不挡两张表。
    const hallSimBlock = mountHallSimBlock().catch((e: Error) => msg(e.message))
    // 「接单大厅优选订单」开关块（D-0328）第三条同款：互不拖累、不挡两张表。
    // （⛔ 曾有第四块「向下游同步上家大厅单」（D-0329），2026-10-29 撤除——见文件头。）
    const hallPreferredBlock = mountHallPreferredBlock().catch((e: Error) => msg(e.message))

    try {
      await reload()
    } catch (e) {
      msg((e as Error).message)
    }

    // 收尾：等各块开关画完（没权限 / 取不到值 ⇒ 它们自己什么都不画）。它们不会抛（上面已接住）。
    await guardBlock
    await hallSimBlock
    await hallPreferredBlock

    return () => {
      ;(layer.closeAll as (type?: string) => void)('page')
      root.innerHTML = ''
    }
  },
} satisfies IslandPage
