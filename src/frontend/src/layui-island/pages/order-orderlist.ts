/**
 * 【订单管理中心】页的 **layui island 复刻 + 接真 API**（对标物 `/Order/orderlist`）。
 *
 * 形状：**21 个筛选控件**（3 组 `.layui-form-pane`）＋ **11 颗 `layui-btn-sm` 工具栏按钮**
 * （在 `.layui-btn-group` 里，**不是** layui 表格工具栏）＋ **1 张 23 列表格** ＋ 1 个 `#page` 分页容器
 * ＋ 6 个 `display:none` 的页内弹窗容器 + 11 个配置隐藏域 + 一张「职业统计」运行期建表。
 *
 * ⚠️ **这两个数字是删控件之后的现状**（业务方多轮裁定删控件，
 * 见「已知差异 18」「已知差异 50 / 52」）：
 * 对标那一版是 **33 个筛选控件 + 27 颗工具栏按钮**；我们累计删掉 **11 组筛选（12 个 `name`）** 与
 * **16 颗按钮** ⇒ 筛选剩 **21** 行、工具栏剩 **11** 颗（**0** 颗尚未开放 + **11** 颗真功能）。
 * ⚠️ **口径**：这里的「个筛选控件」数的是证据件筛选区（`<form class="layui-form">` 起到
 * `#toolbarDemo` 那段）里的 `name="…"` 属性（**实测**：本批删两个块 **23 → 21**；
 * 那个 `25` 是更早一次删控件之前的数，见「已知差异 18」）；
 * 若改数 `class="layui-inline`（**前缀匹配**）的**块**数，同一片区域实测 **18 → 16**——本批删的两个块里
 * 「派单人」那块写的是 `class="layui-inline layui-hide"`，只有前缀匹配才数得到它
 * ⇒ 两个数**不同口径**，别互相当成笔误。
 * 工具栏 11 颗全是真功能（尚未开放为 0）。这个数字由
 * `pages/__tests__/order-orderlist-phase1.spec.ts` 钉在证据件上。
 * ⛔ 不要照对标把删掉的控件「修」回来。
 *
 * 证据（全部离线，见 `.tools/impl-brief-ep.md` / `.tools/impl-brief.md` §1）：
 *  - 标记与页内 `<style>` → `public/replica/order-orderlist.html`
 *    （源 `.tools/recon/order-pages/orderlist.resolved.html`；页内 `<style>` 是 L24-41 + L53-147 原文）
 *  - 列定义 → `.tools/recon/order-pages/orderlist.js` 的 `vars:colsdata` **L2175-2216**
 *  - `treeTable.render` → 同 JS **L2241-2259**；`layPage.render` → 同 JS **L2687-2702**
 *  - 工具栏剩下的 11 颗按钮 → 同 HTML **L441-471**（对标 27 颗里删剩的；class 逐字，含 `layui-btn-sm ` 的尾空格）
 *
 * ## 与对标的**已知差异**（有意为之，逐条列出）
 *
 * 1. **`treeTable` → `table.render`**：对标源码用的是 layui **扩展**渲染器 `treeTable.render`
 *    （L2241，本项目不 vendored 扩展，且禁止擅自引运行时依赖），所以树形那一层我们不做：
 *    `treeTable` 会在 `treeColIndex:1`（订单号列）注入 `.layui-table-tree-flexIcon`（展开三角），
 *    并维护 `treeIdName/treePidName` 的父子关系——**我们不会渲染这个三角**。
 *    ⚠️ **但我没能证实对标页真的显示它**（按「声明 ≠ 渲染」的纪律如实标注）：对标产物里
 *    `orderlist.resolved.html` 的 `tree` 出现次数 **0**、页内 `<style>` 也没有 `.tree_btn`/`triangle`
 *    相关规则，三轮 parity 抓下来的 DOM 里也没有任何 `tree*` 字符串。对标的子单展开是**手写的**
 *    （`$('.tree_btn').on('click', …)` 手动 splice 数据，L2277 起）⇒ 很可能两侧都看不到树 UI。
 *    其余列、列宽、表头、工具栏、分页按普通表格复刻（逐项见下）。
 *    ⚠️ **列数已不是 28**：第二批列删 4、去重 1 ⇒ 现为 **23 列**，见「已知差异 22」。
 * 2. **分页**：对标主表 `page:false`（树表要一次拿全量），分页由**独立的 `layPage.render`** 渲染到
 *    `#page`（`limits:[10,20,30,40,50,100]`，L2690），数据分页靠 `/OrderAjax/GetOrderList` 自己带
 *    `limit/page`。我们是普通表格 + 服务端分页，直接用 `table.render` 的 `page`（**同一份 limits**）；
 *    `#page` 容器仍在标记里但不渲染。`page:false` 时那条 `limits:[20,40]`（L2257）是**死配置**。
 * 3. **列宽取桌面分支，且百分比原样透传**：对标 `width: device ? '12%' : 180`（`device` = android/ios），
 *    `isPc` 为真那一支是**百分比字符串**，不是像素——parity 实测对标的 `authored` 宽度就是
 *    `12%/5.5%/10.4%/5%/4.7%`。**换算成像素会差 15~20px**（实测：181 vs 196、164 vs 170、86 vs 82）。
 *    第 9 列「角色信息」的桌面分支是 **`''`（空串，= 自动列）**，不是 163。
 * 4. **`_change` 列写 `fixed: 'right'`**（对标 L2217 原文照抄）。⚠️ 这一条**更正**了本文件早先的写法：
 *    早先写「对标那颗 `fixed:'right'` 没生效（固定列表头为空）」——那是把「固定列**容器**里的 `th`
 *    不可见」读成了「容器不存在」。**实测两路**：
 *    ① `node .tools/parity.mjs --keys order-orderlist` 的 `sections.tables[0].headers.length`：
 *       对标 **2**（main + `.layui-table-fixed-r`）/ 我们不写时 **1**；
 *    ② DOM 探针：对标 `.layui-table-fixed-r` 的 header 有 **1 个 `th`（「操作」）**、body 有 **1 个 `td`**，
 *       两者都 `display:none`（`offsetParent === null`）；**我们补上 `fixed:'right'` 后形状完全一样**
 *       （`vis:false` / `nTh:1` / `tds:1`）。
 *    ⇒ 表头组数与对标同形，且不引入可见差异（容器里那个 `th` 被 parity 的 `vis` 过滤掉）。
 * 5. **第 26 列 title 写「重要备注」**：对标该列是 `title: i18nDataList.property`，而 `property`
 *    这个 key 在 zh-CN 字典里**不存在** → 对标页表头**空白**。按简报「不复刻对标缺陷」不照抄空白
 *    （这是一处**故意与对标渲染结果不同**的可见差异；同族差异还有本清单的 8 / 9 / 12 等，见报告）。
 * 6. **`lay-tips`**：对标 `layuiadmin` 扩展的属性，我们 vendored 的 layui 里 `grep -c lay-tips` = 0
 *    → 标记里**照抄保留**（值纪律），但不渲染 tooltip。
 *    ⭐ **一键发单按钮**：那颗地球按钮只有图标、悬停提示是它**唯一**的操作说明
 *    ⇒ 业务口径要求「悬停能看见」⇒ 这一颗**同时拼 `title`**（`lay-tips` 照抄 + `title` 可见，
 *    同「已知差异 24」的 V1 处置）；其余按钮不传 `title`、标记一字不变。⛔ 别把这条推广成
 *    「所有 `lay-tips` 都加 `title`」——只有「说明是唯一语义入口」的控件才需要。
 * 7. **`defaultToolbar` 出对标那两颗**（`['filter', {…'RESET_TABLE'…}]`，L2250）：parity 实测对标的
 *    `selfIcons` 就是「筛选列 + 重置表格排版」两颗，**只出前者会少一颗图标（判 struct）**。
 *    「重置表格排版」的复位逻辑在对标自己的 `table.on('toolbar')` 分支里，我们**没有** → 点了不会有反应
 *    （它只是 `table.render` 的代码常量，不影响我们自己的点击委托）。
 * 8. **区服筛选：控件已从证据件删除、`adaptServerField()` 一并删除**（业务方裁定）
 *    ⇒ 本页不再有区服筛选，这条差异不复存在。⚠️ `fetchServers()` **保留**：「角色信息」列仍靠
 *    `serverNames` 把列表载荷里的区服**主键**映射成名字。
 * 9. ⭐ **「阵营」与「排团团号」两块 + 3 颗排团按钮已**真删**，运行期显隐规则整条删除**
 *    （见「已知差异 50」）。背景：对标 `orderlist.js` L1173-1176 在这两款游戏之外对 `.group` / `.task`
 *    两块调 `$().hide()`（**运行期按游戏隐藏**，不是静态标记里没有——parity 里对标可见的
 *    `.layui-form-label` 是 13 个，恰好少的正是这两块）。两块**整块真删**后，原先复刻那条 `hide()` 的
 *    `applyGroupTaskVisibility` + `RUNTIME_STYLE_ID` 随之**整条删除**
 *    （spec §3-D10：`$(".group").hide()` 已是对**空集合**调用的死码，而死码 + 会对读者
 *    说假话的注释比没有更糟）。
 *    ⇒ 本页的 `.layui-form-label` 可见数**不再随游戏切换**：证据件筛选区里
 *    `class="layui-form-label"` **实测 11 → 7**（删「阵营」「排团团号」两个 label，
 *    再删「派单人 / 平台单号」两块，见「已知差异 52」）。
 *    ⚠️ 上面那个 13（以及本页的 11）是**对标那一份**的 parity 记录，**不是本页的期望值**——
 *    本页的数按证据件重数，两个数不同口径，别拿来对拍。
 * 10. **行内「操作」列渲染的是 4 颗纯图标按钮**（`btnfun`，L2136-2164，**不是** `script#baroperating`：
 *    本页 HTML 里没有 `#baroperating`，`grep -c baroperating Order__orderlist.html` = **0**）。逐颗见
 *    `buildOrderCols` 里 `btnfun` 的注释。
 * 11. **工具栏按钮的 `lay-filter` 改成了 `data-tb`**：对标是
 *    `<button … lay-filter="refresh" lay-submit="" type="button">`，我们的证据件写成 `data-tb="refresh"`
 *    且**去掉了 `lay-submit`**。原因：本页工具栏在 `<form class="layui-form">` 内（证据件
 *    L101 开、L573 闭，工具栏 L369-382 在其中），`lay-submit` 会触发 layui `form` 模块的 `submit`
 *    事件（`layui.js` 的 `t.on("click","*[lay-submit]",i)` 是**文档级**绑定），而 `submit()` 会对
 *    该表单里**所有** `*[lay-verify]` 跑一遍校验（读码：`n=a.parents(h).eq(0)`、
 *    `l=n.find("*[lay-verify]")`）⇒ 用户把**别的**控件填成非法值时，点任意一颗工具栏按钮都会蹦出
 *    与本次操作无关的校验提示。
 *    ⚠️ **哪几条规则真的有校验效力（更正：早先注释举的例子是错的）**：本表单里真正生效的是
 *    **3 个 `lay-verify="number"`**（`#minPrice` L164 / `#maxPirce` L168 / 退款弹窗的 `#groupFee` L478）；
 *    而原来这条注释举例的 `lay-verify="datetime"`（两个日期框 L146/L150）**根本没有校验效力**——
 *    layui 内建 `verify` 表只有 `required`/`phone`/`email`/`url`/`number`/`date`/`identity`，
 *    **没有 `datetime`**，`validate` 里 `var a=d[t]; if(a){…}` ⇒ 未注册的规则直接跳过。
 *    （`emp-emplist.ts` 那边同一句错理由也已改；真浏览器实测见该文件头「已知会不同」第 7 条——
 *    `lay-verify="datetime"` 的框里填垃圾值，点 `lay-submit` 按钮**照发请求、零校验提示**。）
 *    ⇒ **去掉 `lay-submit` 的目的仍然成立**（避免无关校验提示），只是生效的是 `number` 那三格，
 *    不是日期框。⛔ 别再拿 `datetime` 当例子（E-0003 同族：没读源码就断言第三方库的行为）。
 *    `class`/文案/顺序/toast 与对标逐字一致，只有这两个**不可见属性**不同（parity 也比不到属性）。
 * 12. **统计句与「订单状态」筛选下拉里的「待上号」都改成「待抢单」**
 *    （`public/replica/order-orderlist.html` 的 `#toolbarDemo` 那个 `.total` span，与
 *    `select[name=orderSta]` 里的 `<option>`，**文案与 value 都改**）：对标原文分别写
 *    「未分配0笔，**待上号**0笔…」和 `<option value="待上号">待上号</option>`，我们写「待抢单」。
 *    这是**有意的对标偏差**——业务方裁定「同一个状态在任何页面、任何可见文案里必须
 *    同一词」，而本页状态徽标的文案取 `@/config/orderStatus` 的「待抢单」⇒ 两处跟着改
 *    （否则同一屏里徽标说「待抢单」、统计句或筛选下拉说「待上号」）。
 *    ⚠️ 证据件那份 `.total` 是**静态兜底**（L338，数字恒 0）；真值由后端 `data.summary`
 *    实装（`renderSummaryText` / `renderSummary`，spec §7）——运行期改的是 layui 渲染出来的那份，
 *    模板文本本身不动。
 *    ⚠️ 筛选下拉这一处**已接后端**：`toOrderSearch` 用 `splitOrderStatusSelection`（逐 token
 *    反查 `ORDER_STATUS_LABELS`）把中文 label 换成后端枚举名（`WAITING_GRAB` 这类）拼串送
 *    `params.status`。多选也真送参：后端 `status` 收逗号串枚举码。
 *    ⚠️ 同一份证据件里那个隐藏弹窗的
 *    `<input type="radio" name="changeToDSH" … title="改成待上号">` **照对标不动**
 *    （它在恒不打开的弹窗里，本页没有任何代码读它）。
 * 13. **两条可见 `<a>` 补 `href`**（订单号蓝链 `orderIdRow` 与状态徽标 `staRow`）：对标
 *     `orderlist.js` 的这两条 `<a>` 都带 `href`，字面值**不同**——订单号那条是 `isNewOpen`
 *     变量（L1856 初值 `OrderView?orderId=…`，L1860 在 `isNewPage != 'Y'` 时改写成
 *     `javascript:void(0);`，**带尾分号**），状态徽标那条（L1971）直接写
 *     `javascript:void(0)`（**无尾分号**）。我们没有 `isNewPage` 那个新页模式（恒等价于
 *     `!= 'Y'` 分支）⇒ 逐字取该分支的两个字面值，两处的分号**不同**（不是抄漏）。
 *     vendored `layui.css` 只有
 *     `a{color:#333;text-decoration:none}` 这种裸规则、**没有** `cursor:pointer`，页内 `.lableb`
 *     样式块也没有 ⇒ 少了 `href` 这两处**没有手型光标、键盘也 focus 不到**（看着不像能点）。
 *     补上之后与对标一致。
 * 14. **「订单状态」筛选已实装多选**。对标那颗没有 `xm-select-radio`，下面还有
 *    「以下几个可多选」分组；formSelects 把选中项用逗号拼进隐藏 input。
 *    前端逐 token 把中文 label 反查成枚举码再拼串送 `status`；后端收逗号串、逐 token 校验。
 *    未映射的档位（`待完善` / `异常中`）不进 `status`，按「尚未开放」点名提示。
 *
 * 15. ⭐ **「记退款」弹窗：四处字段按裁定删除；单笔入口预填当前累计、批量入口开空**
 *    （⛔ 改准（D-0312）：输入口径从「本次增量」
 *    改成「**累计退款总额**」⇒ 预填规则随之翻面，见下）。
 *    对标那一版有**六个**输入项（`public/replica/order-orderlist.html` 原文 L509-553；
 *    提交见 `OrderList20191212.js` L5692-5717，端点 `/OrderAjax/AddOrderRefundLogs`）。
 *    **逐条登记**（我们的证据件已按此改到最终形状，见同文件 L509-528）：
 *
 *    | # | 字段 | 处置 | 依据 |
 *    |---|---|---|---|
 *    | ① | 「退款方式」`input[name=refundModel]`（5 个 radio） | **整行删除** | **业务方裁定**「退款方式不要了，按我们的退款接口来」；接口 `RefundRequestSerializer` 入参只有 `refund_total`/`reason`/`request_id` |
 *    | ② | 「支付单号」`input[name=paytranno]` | **整行删除** | 接口没有这个字段（`AddOrderRefundLogs` 的 `tranNo`） |
 *    | ③ | 「退款日期」`input[name=refundDate]` | **整行删除** | 接口没有这个字段（`sdate`；退款时间由后端 `OrderRefund.created_at` 自己记） |
 *    | ④ | 「同时改订单状态」`input[name=refundChangeToYTD]` | **整行删除** | 接口没有这个字段（`changeOrderStatus`）；**且绝不在这里偷调 `/transition/`**——见下 |
 *    | ⑤ | 「累计退款金额」`#refundFee` → `refund_total` | **保留**（必填 / `>= 0` 且 `<= 订单总价` 由**后端**判） | 接口字段；前端只做**形态**校验（空/非数字/超量级 ⇒ 对标的原文「请输入正确的金额」，不发请求） |
 *    | ⑥ | 「备注」`#refundRemark` → `reason` | **保留**（必填 / ≤255 由**后端**判） | 接口字段；前端**不自建**长度或必填校验，空备注照发、由后端 `msg` 说话 |
 *    | ⑦ | 「确定记录」`.saveReFundOrderBtn` | **摘掉 `data-notopen`，真提交** | 它此前被登记成「尚未开放」（`onNotOpenClick` 会拦），接上后必须摘掉——先例见发价弹层的 `saveSettpricebtn` |
 *
 *    ⚠️ **「备注」框的 `maxlength="100"` 是从证据件原文照抄的**（不是接口的 255）：100–255 字的
 *    原因在界面上**打不进去**。它是**前端更严**的方向（不会让后端以 `1000` 打回），代价就是
 *    这个窄口子。要不要放开到 255 属产品口径，**不动**（值纪律：控件属性从证据件取）。
 *
 *    ⚠️ **全额退款在我们这边怎么走**：**不在这条路上**。我们的 `POST …/refunds` 是
 *    **设置累计退款额**（后端 `refunds.refund_set_total`，要求订单是 `COMPLETED`，
 *    把 `refund_fee` **覆盖**成 `refund_total`）。**「全额退款」= 订单状态流转到「已退单」**
 *    （`/transition/` `target=REFUNDED`），后端在状态机那条边上带**全额退款副作用**
 *    （`refunds.on_order_refunded`：清绩效等）。⇒ 本弹窗**只记退款额**，**绝不在弹窗里替
 *    用户调 `/transition/`**（对标那个「同时改订单状态」radio 正是干这个的，被删掉的就是它）；
 *    要退整单请走「改状态 → 已退单」那条既有路径（工具栏「改状态」/ 点状态徽标）。
 *
 *    ⚠️ **预填规则（D-0312 改准）**：口径统一成「累计」之后，**行内单笔入口
 *    预填该行当前的 `refund_fee`**（`data-fee`，空/`0.00` 也照填），**工具栏批量入口一律
 *    开空**——它要对多张单各设一个累计额，没有一个「本单当前值」可填。
 *    预填值只来自**列表载荷**（`refund_fee` 恒回，spec §5.3 / C6，见
 *    `OrderListItem.refund_fee`），⛔ 不从本地记账推算。
 *    ⛔ 旧规则（「一律开空、预填会二次退款」）**已随语义变更作废**：那时 `amount` 是本次增量，
 *    预填累计确实会多退；现在是绝对值 ⇒ 预填当前累计正是「改小/改回去」的入口。
 *    不要把「一律开空」的旧规则写回注释或测试。批量已接，`oneChecked()` 已删除。
 *
 *    ⚠️ **「退款」列 `hide: true` 照对标不动**（`orderlist.js:2197` 原文
 *    `{…, field: '_refund', width: 60, hide: true, templet: orderRefundRow}`）：
 *    所以我们原来登记的 `hide:true` **是对的、不改**——这一列默认不显示，用户要从表格右上
 *    「筛选列」里打开它才点得到那个金额链接。**不是**漏接入口。
 *
 * 16. **「记退款」失败/金额错误的 `layer.msg` 停留时间取本页全局约定 `1500`，与对标不同**：
 *    对标 `OrderList20191212.js` 的金额形态错误是
 *    `layer.msg('请输入正确的金额', { time: 800 })`（**L5702**），业务失败支是
 *    `{ time: 3000 }`（**L5728**；成功的 `code>0` 支是 `time: 800`，**L5721**）。
 *    本页其余所有写动作（改价 / 改状态 / 抛单 / 撤回）一律 `{ time: 1500 }`，
 *    不为退款单独破这个全局约定 ⇒ 登记的差异是「**停留时间不同**」，
 *    **不是**文案不同（文案「请输入正确的金额」是逐字照抄的，见 `REFUND_INVALID_AMOUNT_MESSAGE`）。
 *    要逐字对齐对标就得改成本页两套阈值；那是产品口径，不动。
 *
 * 17. ⭐ **退款弹窗那句红字的「金额口径」：已改准回累计口径（D-0312）**。
 *    ⚠️ 接口 `amount` 一度是「本次增量」，证据件那句红字随之**有意**改成「本次」口径——
 *    与对标的「最终总共」口径相反，两种口径并存会让人照屏幕上的字多退钱；
 *    接口改成 `refund_total`（累计退款总额，绝对值）之后，两者口径重新一致。
 *    ⇒ 现在证据件 `public/replica/order-orderlist.html:417` 写的是
 *    「记录**累计退款金额**就行（**填多少就是累计退了多少，可以改小**）」，
 *    字段 label（同文件 `:419`）是「累计退款金额」；
 *    **样式与外壳一字不动**（`<blockquote class="layui-elem-quote" style="font-size:16px;color:#ff0000">` 原样），
 *    后半句「不要去改接单价…最终导致错账」照抄保留（两种口径下都成立）。
 *    ⚠️ 与对标原文的差别只剩**措辞**（对标「最终总共」/ 我们「累计」+「可以改小」）——
 *    语义相同，是按业务方口径写得更直白，⛔ 不是反向改义。
 *    ⛔ 不许再在任何地方（注释 / 提示 / 文案）留下旧「本次增量」口径的措辞。
 *
 * 18. ⛔ **删控件是业务方裁定、有意偏离 D-0025 §5**（D-0093）：
 *    7 组筛选（`gameServer` / `gameJob` / `projectType` / `projectSelect` / `key4Type` + `key4` /
 *    `orderLabel` / `gameAcc`，共 8 个 `name`）与 12 颗工具栏按钮
 *    （`setOrderThrowHomeBtn` / `cleanOrderThrowHomeBtn` / `copyToNewOrderBtn` /
 *    `insideOrderPushGroupMsgBtn` / `setOrderLabelBtn` / `setJiWei` / `setOrderTipsBtn` /
 *    `setOrderJiajiBtn` / `sendSmsBtn` / `setOrderSoureIdBtn` / `setOrderProjectBtn` /
 *    `setOrderRemarkBtn`）已从**证据件**里真删（不是隐藏、不是保留渲染）。
 *    ⇒ 静态标记与对标**不再逐像素一致**（后面的控件整体左移），这是**知道代价的取舍**。
 *    ⛔ **不要照对标把它们「修」回来**；行内的 `.throwEdit` / `.cancelThrowHome` 与「行内单笔发」
 *    的口径**保留**（业务方：单个发即可）。证据件顶部另有一段同样的登记。
 *
 * 19. ⭐ **「指派打手」是页内自建弹层（内容照对标那张单人版指派页的九区块），
 *    不是对标那个 85%×95% 的 iframe 子页面**（C2）：
 *    对标是 `layer.open({type:2, area:['85%','95%'], content:'assignation?orderId='+…})`
 *    （`orderlist.js:4032-4043`），我们没有那个子页面 ⇒ 照「改状态」的先例改成**页内弹层**
 *    （左列打手多选列表 + 右列「当前订单分配打手」一行 + 「该订单的打手账单」+ 底部两颗按钮；
 *    **只允许勾一个人**，见 `openAssignDialog` / spec §7.1）。⚠️ 尺寸仍取 `['85%','95%']` 与对标
 *    iframe 的**占比对齐**，但两个页内弹层的观感必然不同——这是 C2 登记的那条已知差异，
 *    本批新增的可见差异（多选限制、排序方式/保存筛选/智能派单/上号电话/百分比五块不做、
 *    默认职位等）从「30」起登记，本条不重复。
 *    「发单价」那一格显示并写入订单发单价 `Order.hall_price`，落成 `/assign/` 的 `price`。
 *    预计绩效由服务端按有效基价乘提成率重算，不再送 `fee`。
 *    ⚠️ 弹层里动态插入的 checkbox / 表格是**手工标记**（不是 layui `table.render`）⇒ 渲染后仍要
 *    `form.render('checkbox')`（同在运行期插入表单件那条坑上）。
 *    ⚠️ 后端在查看者无权看接手人时把 `taker_name` 回成常量 `无权查看`（spec §9）⇒ 那一格渲染纯文本、
 *    工具栏那条路只提示、**不进指派界面**；前端**不**自己判那个权限节点（镜像判据必然漂移）。
 *
 * 20. ⚠️ **订单号右键菜单的图标字形不是对标那套 `context-menu-icons` 字体**
 *    （spec §2 第 11 条 / §6）：对标那份菜单由 jQuery `contextMenu` 插件渲染，图标来自
 *    `@font-face{font-family:context-menu-icons;src:url(font/context-menu-icons.…)}`，而那几个字体文件
 *    **全仓（含 `.tools/recon/**`）取不到**（`find -name "context-menu-icons*"` 零命中）。
 *    ⇒ 处置：**类名照插件产出**（`context-menu-icon` + `context-menu-icon-{paste,edit,copy}`，
 *    §6 的 DOM 结构逐字），字形改用 vendored layui 图标字体里最接近的四个：
 *    `layui-icon-form`（对标 `paste`，查看详情）/ `layui-icon-edit`（对标 `edit`，同名同义）/
 *    `layui-icon-file`（对标 `copy`，复制订单内容）/ `layui-icon-template-1`（对标 `copy`，复制成新订单）。
 *    **与对标可见的差异，逐条枚举**（⛔ 别写成「可见差异只有图标」这类绝对句——E-0011 同族）：
 *    ① **图标字形**（上面那四个 layui 类；对标用 `context-menu-icons`）；
 *    ② **提示停留时长**：本页统一走 `islandMsg()` 的 `{time: 1500}`，对标 `orderlist.js` 是 `{time: 1000}`
 *      （文案本身逐字照抄，差的是时长）；
 *    ③ **屏幕边缘的翻转定位没做**：对标 `position` 在菜单超出视口时会把坐标往回减、并把坐标
 *      夹回 `≥ 0`（**逐字**原文：`o.top+c>s&&(o.top-=c),o.top<0&&(o.top=0),o.left+l>i&&(o.left-=l),o.left<0&&(o.left=0)`，
 *      读码 `jquery.contextmenu.min.js`；分隔符是 `,` 不是 `;`，末尾两处 `&&(x=0)` 是夹取），
 *      我们只按 spec §6 那句「在鼠标位置显示菜单」放在鼠标坐标上。
 *    位置 / 间距 / 底色 / 悬停反白**逐条**取自对标 CSS（见 `CONTEXT_MENU_STYLE_RULES` 的出处注释）。
 *    ⚠️ 另一处**有意**偏离：分隔线只带 `context-menu-separator`（插件那棵树还会给它
 *    `context-menu-item`）——spec §6 写的就是 `<li class="context-menu-separator">`，
 *    而「恰好 4 项」这个判据按 `.context-menu-item` 计数。
 *
 * 21. ⭐ **「进订单详情」不再是 vue-router 跳转，改成原地开层**：
 *    **两个入口一起改**——订单号蓝链 `.orderdetails` 与右键菜单的「查看详情」，
 *    因为**对标这两处调的是同一个 `viewOrderDetails(orderid, viewID, tid)`**
 *    （`OrderList20191212.js` **L3714-3719**（蓝链委托）与 **L3755-3767**（函数本体）；
 *    右键那条在 `$.contextMenu` 的 `viewDetails` 分支 **L5091-5093**）。
 *    参数**逐项取本页自己的值**（`OrderList20191212.js` L3756-3767）：
 *    `title: viewID + "订单详情"`、`shade:[0.3,'#333']`、`shadeClose:true`、**`maxmin:false`**、
 *    `type:2`、`skin:'layui-layer-lan'`、`anim:8`、`area:['85%','92%']`。
 *    ⛔ **`area` 是每页自己的值，不许抽共用常量**：同一个产品里我的订单 PC `85%×92%`、
 *    我的绩效 `60%×92%`、绩效账单 `1125px|80%×90%`、绩效管理页 `90%×92%`。
 *    `content` 换成**我们自己的**复刻页（`<BASE_URL>layui-island/page.html?page=order-orderview&order_no=<单号>`，
 *    基址用 `import.meta.env.BASE_URL`、单号 `encodeURIComponent`）。
 *    ⚠️ **对标 `content` 还带两个参数，我们不带**（如实登记）：`viewType=20`（对标硬编码）
 *    与 `tid`（平台子单号 `res.tid`）——我们的复刻页只认 `order_no`
 *    （`order-orderview.ts` 的 `ORDER_NO_PARAM`），且**我们不产子单**（对标那个 `.tid` 分支只在
 *    三方平台子单存在时才有值；本页文件头第 8 条已登记「子单分支不做」）⇒ 带了也没人读。
 *    ⚠️ **一处如实登记的偏离**：对标从**顶层窗口**开层（盖住顶栏整屏）；我们开在 **island iframe 内**
 *    （只盖内容区）——本页跑在 iframe 里，Vue 外壳没有 layui；与 T4 绩效页同一处偏离。
 *    ⛔ **开层前的单号形状闸** `/^\d{20}$/`（与 `/orders/:order_no(\d{20})` 路由约束、
 *    后端 `Order.order_no` 的 `lookup_value_regex` 同源）：不匹配 ⇒ **不开层** + 固定文案。
 *    理由（与 T4 绩效页同一口径）：`layer.open` 的 `title` 是 **HTML 上下文**，
 *    而 `dataset` 会把 attribute 实体**解码回** `<`/`>` ⇒ templet 里那层 `escapeHtml`
 *    在这条链上不构成保护。详见 `openOrderDetailLayer` 的注释。
 *    ⚠️ **`title` 前缀取的是「显示单号」不是系统单号**（对标 `viewID` = `data-viewId` =
 *    `res.orderIdView`，即那一格**渲染出来的**号；`orderIdRow` 的规则是「来源单号非空 ⇒ 显示来源单号」）
 *    ⇒ 前缀过 `escapeHtml`（来源单号是**录入的自由文本**，`Order.source_order_no` 是
 *    `CharField(max_length=128)`、没有任何字符集校验）。
 * 22. ⭐ **列集合第二批改动（业务方逐条裁定；**D-0094**；spec
 *    `docs/specs/2026-09-23-order-list-columns-design.md` §1/§2/§7）——有意偏离 D-0025 的
 *    「逐列对齐全套」**，逐条如下（⛔ **不要照对标把这些列「修」回来**）：
 *    - **删 4 列**（业务方裁定 1）：订单标签 `_orderLabel`、阵营 `_group`、机位/团长 `_jiwei`、
 *      日志 `_orderlog`（「日志删掉不需要，订单详情里面有日志可以看」）。⇒ 对标的 28 列里
 *      少掉这 4 列，**不是**漏抄。
 *    - **去重 1 列**（裁定 2）：对标「重要备注」是**成对的两列** `_remark2` / `_remark`，
 *      由 `remarkLocation` 配置决定显示哪一个；我们把两列都抄了 ⇒ 列筛选菜单里出现两条同名项。
 *      只留 `_remark`。
 *    - **4 列由 `hide:true` 翻成默认显示**（裁定 3）：实收款 `_netreceipts`、退款 `_refund`、
 *      发单人 `_assUserName`、录单人 `addOrderUserName`。重要备注 `_remark` **改动前就没有
 *      `hide`**（对标是 `_remark2` / `_remark` 成对配置，我们抄的那一支本来就默认显示）
 *      ⇒ 它属于「业务方点名要默认显示的 5 列」之一，但**不是**被「翻」出来的那 4 列。
 *    - **10 个 templet 补/改**（裁定 4「没实装的列一起实装」）：角色名 / 时间 / 旺旺来源 /
 *      发单价 / 实收款 / 销售客服 / 发单人 / 重要备注，**外加两个内联 templet：录单人
 *      （`created_by`）与完单时间（`completeTimeRow`）**。逐列取值与出处见 `buildOrderCols`
 *      里各自的注释。
 *    - 「时间」列 = **`time_requirement`**（录单页「时间要求」那段自由文本）：这是**刻意偏离**
 *      （spec 决策 C5）——对标 `orderHourDescRow` 读的是**服务端算好的时长描述**
 *      `orderHoursDescStr`，我们没有那个量。⚠️ 该键**不在 spec §5 原表的 9 个键里**（原表漏列），
 *      是**执行期补进列表载荷的第 10 个键**（`OrderListSerializer.Meta.fields`）——
 *      不补这一列只能恒空。后端契约清单见 `tests/test_order_list_columns.py` 的 `TEN_KEYS`。
 *    - 「发单价」列是两支改价链（有没有抛过大厅 = `hall_publisher_name` 是否为空），
 *      并接上 `/hall-price/`。金额颜色 `#ff6a00`。验收即自动结账之后，金额前按
 *      `taker_settled` 渲染「结」（`TAKER_SETTLED_MARK`：底色 `#5FB878`、白字；
 *      `#5FB878` 只归标记，不挪到金额上）。抛单次数分支与 `recFee > price` 转红仍不做
 *      （缺那两个字段）。取值口是 `OrderListSerializer.taker_settled`（相关子查询，不是逐行查）。
 *    - 「实收款」列**只读**（裁定 5）：「实收款是按照订单价和退款计算的」⇒ 值由后端算好放进
 *      载荷（`net_received`），前端**不算**也不给编辑入口（不做对标那条 `editNetReceiptsPrice`）。
 *    - 「旺旺/来源」列**不做**对标的 `/order/OpenWangwang` 外链（spec §9-4：那是指向**对标自家
 *      系统**的链接），只渲染纯文本。
 *    - 「订单截图」列**已实装**：两颗按钮各自开一个弹层
 *      （上传进度图 / 管理订单截图），层参数逐项取本页自己的 `OrderList20191212.js:4044-4069`
 *      ——见 `openScreenshotLayer`。⛔ 原先那句「保持占位（裁定 7）：点了仍提示尚未开放」
 *      已作废（原句按 `AGENTS.md` §4 保留在下方「已知差异」表里）。
 *    - 「完单时间」`_completeTime` **仍默认隐藏**（业务方只点名了 5 列要显示，它不在其中），
 *      但**内容已实装**：值取 `completed_at`（新字段），空则空串；格式沿用本页
 *      「录单时间」列那句 `replace('T', ' ').slice(0, 16)`（不引日期库）。
 *
 * 23. ⭐ **「重要备注」与「角色名」两个行内点击动作接上既有 PATCH**（**D-0094**；spec §7.4）：
 *    对标这两处**各有专用 AJAX 端点**（`/OrderAjax/ChangeOrderRemark` L4648、
 *    `/OrderAjax/ChangeOrderGameRoleNameByOrderId` L4238），我们**没有**它们 ⇒ 按 spec §7.4 改走
 *    **既有的** `PATCH /api/business/orders/<no>/`（`updateOrder`），一次**只送一个键**。逐条登记：
 *    - 标题 / 形态**取对标那两处 `layer.prompt` 的原文**：备注
 *      `{title:'修改订单重要备注', formType:0, value:当前值, maxlength:500}`（L4642）；角色名
 *      `{title:'编辑订单角色名，若没有请写无', formType:0, value:当前值, maxlength:25}`（L4232）。
 *      两处对标都**不设 `btn`**（用 layui 默认的「确定/取消」），我们照旧不设。
 *      ⚠️ spec §7.4 与计划的 Interfaces 行把角色名标题写成「修改角色名」——那是**概述**；
 *      按「值纪律」（文案从对标取）实现成对标原文，此处登记这处差异，别当成抄漏。
 *    - **两处 `maxlength` 都是「前端比后端严」**：`remark` 是 `TextField`（模型层无长度限制，
 *      `apps/business/models.py:360`）、`game_role_name` 是 `CharField(max_length=128)`（同文件 L276）
 *      ⇒ 超限的值在这条路上打不进去。要放开属产品口径，照对标不动
 *      （同「已知差异 15」那条退款备注 `maxlength=100`）。
 *    - 成功路径的顺序有意为 **`layer.close(index)` → `reload()` → `msg`**：`reload()` 的
 *      `collectSearch()` 可能弹筛选警告，而 `layer.msg` 后来的覆盖先前的 ⇒ 顺序反了用户看不到成功提示。
 *      失败路径**只弹后端 `msg`**、**不关** prompt（用户可在原框里改完重试）。
 *    - 成功那句 `msg` 是**页内自撰**：对标弹的是服务端 `data.msg`，我们成功信封的 `msg` 是空串
 *      （同「改类型 / 改价 / 改状态」几处）。
 *    - 对标的 `re` 重入闸（`if (!re) return false`，L4233/4643）**未复刻**：请求在途时再点一次「确定」
 *      会再发一次 PATCH；本页另一处 `layer.prompt`（改接单价）同样没有这道闸。要加属产品/一致性口径。
 *
 * 24. ⭐ **「接单价」列重写（spec `docs/specs/2026-09-23-order-price-and-payment-status-design.md`
 *    §7 第 1 条 / 决策 C3 / C5；对标 `orderlist.js:2058-2085`）**——有意偏离「逐列对齐全套」，逐条如下：
 *    - **收款状态三档标记**照对标 `<a href="javascript:void(0)" class="lableb jsSta">`：底色
 *      `#E6E6FA` / `#B0E0E6` / `#98FB98`、单字「未 / 中 / 收」、三句 `lay-tips` **逐字**取对标
 *      （`orderlist.js:2062-2073`，逐字差异注意 `未` 档那句是「与代练员或下家**结账**」、
 *      另两档是「…**结算**」）。对标的判据是**中文字符串** `isSettStatus == "结算中"` / `"已结算"`，
 *      我们按**码**取（`paymentStatusMark(code)`），**任何不在三档里的字符串**（含空串、`'BOGUS'`、
 *      原型链键 `'__proto__'`/`'constructor'`/`'toString'`）都落「未」档——与对标「其余落默认」同向。
 *      ⚠️ 取标记**不能**直接写 `PAYMENT_STATUS_MARKS[code] ?? 默认`（原型链键会命中真值、
 *      `??` 兜不住——实测反例）；判据见 `paymentStatusMark` 的注释与用例
 *      「非空未知码（原型链键）也落「未」档，不渲成 undefined」。
 *    - ⚠️ **可见差异 V1**：标记与金额提示都**同时拼 `title`**。本页 vendored layui 里 `lay-tips`
 *      不渲染（见「已知差异 6」）⇒ 只照抄 `lay-tips` 的话「悬停提示」根本悬不出来（裁定 4 要求能悬）。
 *      `lay-tips` 照抄是值纪律，`title` 是可见性。
 *    - **金额链接的 class 由本页旧名 `orderPrice` 改成对标的 `editOrderPrice`**（重命名：
 *      `onCellClick` 的选择器与用例同批改，不留半新半旧），点击**读 `data-fee`**
 *      （不再读 `textContent`——文本是格式化过的串，对标那支也读 `data-fee`，spec §7.3）。
 *      ⚠️ 这里**不是**照抄本页旧形状：旧 `<a class="orderPrice" style="color:#ff6a00;font-weight:600"
 *      title="你接手此单的金额：…">` 整条被替换。
 *    - ⚠️ **可见差异 V4**：金额文本走本页既有 `money()` ⇒ **两位小数**（对标是 `toFixed(1)`，
 *      `88.00` 不是 `88.0`）；**不拼**对标的「（原…元，已打…折）」段、**不拼**服务费、**不做**
 *      对标 `isAllowChangeOrderFee == "N"` 的行级门控（一律可点）——后三者依赖我们没有的列 / 开关。
 *    - **不做**对标的 `tid` 分支（平台单只显示保证金 / 服务费、且各链不可点，`orderlist.js:2059`）：
 *      我们不产子单（spec §9-7）。
 *    - `payment_method` 是**自由文本** ⇒ 进 `lay-tips` / `title` 两个属性前各过 `escapeHtml`。
 *      ⚠️ 对标的「支付方式」是 `payMethod` 数字码（`10`=花呗付款+服务费 / `20`=信用卡付款+服务费 /
 *      其余「默认」，`orderlist.js:2075-2082`）；我们的列是自由文本 ⇒ 直接拼原文，**不做**那套码表。
 *    - ⚠️ 标记上多带一个**不可见**属性 `data-sta="<码>"`：对标 `.jsSta` 的点击处理读 `data-id`
 *      并开收款状态弹窗（`orderlist.js:4113-4118`），我们那条链由 **T8** 接；`data-sta` 是给 T8
 *      的弹窗预勾选留的取值口（与 `takerRow` 补 `data-taker` 同一处置）。
 *
 * 25. ⭐ **发单价两支的可见差异（V2 / V3；spec §9 的 V2 / V3）**——
 *    ⚠️ V1（标记与金额提示靠 `title` 才可见）与 V4（接单价两位小数、缺「（原…元，已打…折）」段与
 *    `isAllowChangeOrderFee` 行级门控）已在「已知差异 24」逐条登记、本批仍成立，**不重复**。本条只登记 V2 / V3：
 *    - **V2：①支（`hall_publisher_name === ''`，判据是「从未抛过大厅」）不显示悬停提示**。对标那一支
 *      有一句 `lay-tips`（`该笔订单还未指派过接手人，可以直接修改抛单价作为记账用，点击修改`），
 *      但那句原文断言的是「还未指派过**接手人**」，而 `/assign/` 过的单在我们的判据里也落 ①
 *      （历史行无法区分「抛过没抛过」，残差 R1）⇒ 显示它会是一句**假话**。故只照抄 `lay-tips`、
 *      **不另加 `title`**（②支照补 `title`，见「已知差异 24」的 V1）。
 *    - **V3**：曾抛过且当前不在抛单状态时开绩效弹层。`TAKER_PERF_BRANCH` 回 `'dialog'`，
 *      点击调 `openPerformanceDialog`。判据先看有没有接手人：有人接手不看
 *      `hall_publisher_name`，一律开弹层；没接手人时才按抛没抛过分 `edit` / `prompt`。
 *
 * 26. **检索参数名 `payment_status` + 码（V5；spec §7 第 7 条 / §9 的 V5）**：
 *    对标筛选控件 `name="setSta"`、option `value` 是中文「未结算 / 结算中 / 已结算」；我们送
 *    `payment_status=UNSETTLED|SETTLING|SETTLED`——`toOrderSearch` 用 `PAYMENT_STATUS_BY_BENCHMARK_VALUE`
 *    把中文映射成码，**映射不到就不发**（与「订单状态」映射不到就不送 `status` 同形）。
 *    ⚠️ 两套中文并存（库/接口侧「未收款 / 结算中 / 已结款」、对标控件字面量「未结算 / 结算中 / 已结算」）
 *    是**有意**的（C2 / 全局约束），别「统一」掉任何一个。
 *
 * 27. **批量写动作走真批量；空勾选文案与对标不同**：
 *    0 笔提示「请选中要操作的记录」（对标是「请选择要操作的记录！」）。
 *    `oneChecked()` 已没有调用者，整条删除。
 *    11 颗里有 9 颗批量写动作走 `batchSelectionOrNull()`（勾 1 笔也是长度 1 的批量，多笔照发）：
 *    指派打手 / 改状态 / 改类型 / 记退款 / 改接单价 / 改发单价 / 收款状态 / 删除 / 重算客服提成。
 *    `refresh` 只重载。`exportBtn` 不走这道前置：没勾选也要能导出当前筛选的全部，
 *    而前置函数在 0 笔时会提示并返回空。
 *
 * 28. **`%` 与金额有服务端校验（V7；spec §9 的 V7）**：对标这条链**客户端零校验**、
 *    只有 `maxlength`；我们服务端加了三类判据（语法 / `HALL_PRICE_PERCENT_MAX = 1000%` 上限 /
 *    12 位金额闸），前端**不镜像**（不换算、不设上限，`50%` 原样送）——换算在服务端，前端镜像会漂移。
 *    ⚠️ 1000% 上界与三条错误码是我们定的（对标权威枚举未取到，残差 R3）。
 *    ⚠️ **补记**：上面「前端不镜像」不等于「前端零判据」——
 *    `openHallPricePrompt` 还有**一处更严的客户端判据**：空串时自己判「发单价不能为空」、**不发请求**；
 *    对标 `changeThrowPrice` **没有**这条（recon §4.3 逐字核过）。功能影响为零（后端本来就回 `1000`），
 *    但它是一条**比对标严**的可见差异，登记在此（spec §9 的 V7 同步补了同一条）。
 *
 * 29. **改收款状态的入参是码 + 两个端点都是单号路径（V8 / V9；spec §9 的 V8 / V9）**：
 *    对标改收款状态送中文 `sta`、走**单端点 + 逗号串**；我们送 `{ payment_status: <码> }`，且两个新动作
 *    （收款状态 `…/payment-status/`、发单价 `…/hall-price/`）都是 `…/orders/<单号>/…` **单号路径**
 *    （与既有一批写端点同形）。
 *
 * 30. ⭐ **指派打手 · 接手绩效（D-0142）的可见差异登记，从 30 起**——
 *    源 spec 是 `docs/specs/2026-09-23-order-assign-and-taker-price-design.md` §9 的 V1–V15。
 *    ⚠️ **编号要看清**：本条起各处的 `V*` 属于**那份 spec**；本文件早先条目 24–29 里的 `V*`
 *    属于**另一份 spec**（`2026-09-23-order-price-and-payment-status-design.md`），两套编号
 *    **互不相干**，别按号索骥。已有同族条目的一律**指回旧编号**、不重复登记（见 31 / 40）。
 *    - **V9（指派弹层这一半）**：弹层是**页内** `type:1`（`openAssignDialog` 的 `layer.open` 实参），
 *      对标是 `type:2` 的 iframe 子页（`assignation?orderId=…`）；`area:['85%','95%']` 只对齐
 *      **占比**，观感与 iframe 子页必然不同。⚠️ 形态这件事本身已在「已知差异 19」登记（含九区块
 *      与尺寸口径）⇒ **本条不重复**，只补 T8 复核时确认的 `type:1` 与 `anim:5` 两个实参。
 * 31. **V1：左列打手列表只能勾一个**（对标可勾多人）：点第二个时把**新点的那个弹回**、保留已选，
 *    提示 `ASSIGN_ONE_TAKER_MESSAGE`（用的是**已勾选那个人**的显示名，不是刚点的那个）。
 *    ⚠️ 「只允许勾一个人」这件事已在「已知差异 19」提过 ⇒ 本条只登记**提示文案是新拟的**这一处
 *    （对标能勾多人、没有这一支）。C1。
 * 32. **V4：`排序方式` 控件不渲染**（C4）：名单在**前端**按显示名升序排（`byDisplayName`），
 *    且只排**本次拉回的那一页**（`ASSIGN_PAGE_SIZE = 200`）——后端 `accounts/selectors.py` 是
 *    `order_by("id")`、没有排序参数 ⇒ 跨页顺序不保证。R4。
 * 33. **C5：`保存筛选` 按钮不渲染、也不落任何配置**（我们没有「保存筛选」这个配置位）。
 * 34. **C2 / C3：`开启智能派单` / `指定上号电话` / `按订单价格百分比设发单价` 三块不做**：
 *    我方 `Order` / `Performance` 都没有对应字段（spec §9 记「全仓零命中」）⇒ 删除代价 0；
 *    连带对标的列表「智」徽标（`isAutoAssignment` 一系）也不做。
 * 35. **V5：`用户职位` 默认选中「代练员」**（对标默认「全部」）：有意偏离——避免默认把客服 /
 *    管理员混进接手人名单（C16 代价 ②）；要拉全部时选「全部」，那一档**不送** `role`。
 * 36. **V11：`业务类型` 的 `0` 档落成「不筛」**：对标的 `0` 是它服务端的「代练+陪玩」档，而我方
 *    `business_type` 是**人身上的单选属性** ⇒ 送 `0` 只会剩下标了那一档的人（C16 代价 ③）。
 * 37. **V6：`搜索打手` 输入框加 `maxlength="64"`**（对标无该属性）：64 是 `AccountFilterSerializer.key`
 *    的上界（`ASSIGN_KEY_MAXLENGTH`）⇒ 前端在这一格上先拦住超长输入（与后端上界**一致**，不是更严）。
 * 38. **V12：发单价先看有没有接手人**（spec §7.2）：对标那一支只判「曾抛过 + 当前不在抛单状态」。
 *    直接指派、从未抛过大厅的单如果先看抛没抛过，会弹成「改发单价」。
 *    判据用订单状态，不用 `taker_name`：无权查看时载荷里它是哨兵 `无权查看`，池内单会开错绩效窗。
 *    `UNASSIGNED` / `WAITING_GRAB` ⇒ 没接手人，再由 `hall_publisher_name` 分 `edit` / `prompt`；
 *    其余状态 ⇒ `dialog`。池内两态在库里恒无接单人（D-0032）。
 * 39. **V13：`temp_amount` 的初值由服务端按提成算**（C8 代价 ①）：对标在前端按 `tichen` /
 *    `brokerageType` 算；我们不在前端镜像那条算式（镜像会漂移）。
 *    指派弹层那一格是订单发单价，载荷键是 `price`（非空即送）。见「已知差异 53」。
 * 40. **V9（绩效弹层这一半）**：`openPerformanceDialog` 也是**页内** `type:1`，对标那个
 *    `/Emp/ChangeRecPriceByOrder` 是 `type:2` 的 iframe 子页；`title` / `area:['60%','45%']` /
 *    `anim:8` / `skin` 逐值照抄对标实参。⚠️ 「本仓没有 iframe 子页运行面」这条路线已在
 *    「已知差异 19」写过，但它讲的是**指派弹层** ⇒ 绩效弹层这一处在本条单独登记。
 * 41. **V14：绩效弹层里改价显式送字段名**：两条路都送 `{amount_kind, amount}`
 *    （`POST /performances/<id>/adjust-amount/`）。行内编辑按被点的格子送
 *    `amount_kind:'temp'|'bonus'`，铅笔送 `amount_kind:'temp'`。
 *    不送对标那个 `type`。`{temp_amount}` / `{bonus_amount}` 是旧 `/adjust/` 的键，本页不送
 *    （`emp-emplist.ts` 仍走那个端点）。
 * 42. **V15：绩效弹层的行集固定 `emp_type=打单工资`**（`PERF_EMP_TYPE`）：客服提成行不出现；
 *    被清除过接单人的行（`user` 为空）也不可见（后端 `performance_queryset` 排除）——C13 代价 ①②。
 * 43. **V7：`记录人` 列取 `order.created_by`（录单人）**：对标 `logUserName` 的**真实取值来源未取到**
 *    （它绩效页的 tips 写「由X记录此条账单(也是发单人)」）；我们取本单的录单人，语义**近似**而非同义。
 * 44. **V2 + V2′：两处「清零」与对标「删掉绩效记录」的语义差**——
 *    - **V2（清除接单人的 `isDelEmp=Y`）**：我们落成**把该绩效金额清零**（记录保留、显示 `0.00`），
 *      对标按它的按钮文案是「删掉绩效记录」（其服务端真实行为**未取到**）。第二句确认语的两颗按钮
 *      文案**逐字照抄**对标（`CLEAR_TAKER_DEL_EMP_BUTTONS`），**行为**是我们的清零
 *      （C15；D-0072 ① + `PROTECT`）。
 *    - **V2′（绩效弹层 / 账单表那颗垃圾桶）**：软删该绩效行
 *      （`POST /performances/<id>/delete/`；该行就是当前接单人那条时同一步清接手人）。
 *      按钮 `title`（`PERF_DELETE_TITLE`）与确认文案（`PERF_DELETE_CONFIRM`）逐字照对标。
 *      同屏另一处「清零」（`isDelEmp=Y`，上面 V2）仍是金额归零、记录保留。
 * 45. **V10：昵称是可编辑自由文本 ⇒ 「昵称恰好是 `无权查看`」会撞上无权限哨兵**（C11 代价 ②）：
 *    那一格会被当成无权限（渲染纯文本、不开指派层、工具栏那条路只提示）。本批**登记不改**哨兵形状（R7）。
 *    ⚠️ 同族的**转义**缺陷（C11）**已在本批修掉**：`takerRow` 的 `data-taker` 属性与正文两处都过
 *    `escapeHtml`（spec §8 第 29 条 / T8 MF-1）。
 * 46. **V8：`保存指派(没改动请勿点)` 的「没改动」不做脏检查门控**（spec §7.1 区块 8）：文案逐字照抄
 *    对标，但行为**不门控**——只要右表有选中的人，点它就发 `/assign/`。对标那半句是**提示语**，
 *    不是我们的判据。发单价那一格没有脏标记：非空就随保存送出。
 * 47. **V3：绩效管理页「实际收款」列不跟随本批的新算式**（spec §4.2 第 7 行）：该列直读
 *    `serializers.py` 的 `net_receipts`（`source="order.received_price"`），而订单列表「实收款」走本批
 *    改过的 `net_received`（`total_price − refund_fee`）⇒ 两处从此**可能不一致**（录单隐藏框写 `0`
 *    时最明显）。⚠️ **这条差异的渲染面不在本页**，在**绩效管理页** `emp-emplist.ts` 的 `netReceipts`
 *    列；因 spec §9 的 V 清单以本批为单位，登记在此以保证 V1–V15 逐条可查（业务方只点名两处算式）。
 *
 * 48. ⭐ **删单接线的可见差异登记**——源 spec 是
 *    `docs/specs/2026-09-24-order-delete-and-filters-design.md` §7.1 / §2.1.2：
 *    - **`delOrderBtn` 接真**：从 `NOT_OPEN_TOOLBAR` **移出**（7 → 6 条），走
 *      `POST /api/business/orders/<单号>/delete/`（`api/orders.ts` 的 `deleteOrder`）；
 *      工具栏 15 颗里真功能 8 → **9**。
 *      ⚠️ **现值（实测）**：工具栏 **11** 颗、其中真功能 **10** 颗
 *      （上面那个「15 颗 / 8 → 9」是删单接线当时的数）。
 *    - **两处「文案与实现」的矛盾（照抄 + 登记，⛔ 不许修）**：① 第 1 跳说「将无法找回」，
 *      而本仓**确有**回收站页与还原端点（`/orders/recycle`）；② 第 2 跳说「会自动删除接单客服的
 *      接单提成记录」，而本批裁定把绩效行的处置**整体并入 `isDelEmp` 两档**。两句都逐字取对标
 *      （`orderlist.js:4392-4403`）⇒ 见 `DELETE_CONFIRM_TEXT` / `DELETE_PERFORMANCE_CONFIRM_TEXT`。
 *    - **一次一笔**：勾 0 笔、>1 笔都只提示、**零请求**（复用 `oneChecked()` 的既有文案）；对标是
 *      逗号串真批量 ⇒ 这是有意的可见差异（spec §5.5-#1）。`isDelEmp` 两档（`Y` = 该单全部绩效行
 *      一起标记、`N` = 一行不动）由**后端**落地，页面只负责把两颗按钮分到两档。
 *    - 成功提示用**页内常量** `DELETE_SUCCESS_MESSAGE`（不是响应信封里的 `msg`）：`request()` 已把
 *      信封解掉、`deleteOrder` 的返回体里没有 `msg`（spec §7.1-4 那句按实现改准）；失败那一支仍
 *      显示后端文案（`errText` 读 `BizError.message`）。
 *    - ⛔ **改准**：上面那条「**一次一笔**」**已不成立**——
 *      工具栏「删除」改走**真批量**（`runBatchFlow('delete')`，勾 1 笔也是长度 1 的批量；spec §D11）；
 *      两跳文案与按钮**一字未改**，`isDelEmp` 显式进 `payload`（整批一个值）。0 笔那一句不变。
 *
 * 49. ⭐ **两跳确认的关层：第 1 跳与对标一致，第 2 跳我们多关一层**：
 *    - **第 1 跳与对标一致**：对标 `orderlist.js:4392-4403` 把**第 1 跳的 `index`** 一路传进
 *      `delOrderPost(orderID, …, index)`（那边据此 `layer.close(index)`）⇒ 对标会关第 1 跳。
 *      我们照它的形状把 `firstIndex` 传给第 2 跳的两支回调并 `layer.close(firstIndex)`。
 *    - **第 2 跳对标自己不关**：对标 `btn: ['需要删除接手人绩效','不需要删除接手人绩效']` 的
 *      两个回调都**忽略了自己那一层的 `index`**（形参里就没有它）。而 vendored layui 的按钮处理器
 *      （`public/layui/layui.js` 偏移 178582）只在「**没有** `yes` / `btn1` 的第 1 颗按钮」和
 *      「第 2 颗按钮（回调未显式返回 `false`）」上调 `x.close(n.index)` ⇒
 *      **「需要删除接手人绩效」这一支（第 1 颗按钮 + 给了 `yes`）对标一层都不关**，
 *      第 2 跳的确认框会**留在屏幕上、挡住页面**（对标自己的缺陷）；「不需要…」那一支由框架自动关。
 *      **我们两支都显式 `layer.close(secondIndex)`** ⇒ 与对标在「需要…」这一支上有一处
 *      **有意的可见差异**，理由是残层会挡住页面（同族：不复刻对标缺陷，见本清单 5）。
 *      ⚠️ 「不需要…」那一支我们这一句与 layui 之后的自动关层重复，而 `x.close` 对已移除的层是
 *      no-op（`layui.js` 偏移 185435 处 `l[0] && (…)` 短路）⇒ 重复关层无害。
 *
 * 50. ⭐ **删控件：3 颗排团按钮 + 2 个筛选块 + 整条运行期显隐规则**（**D-0144**；
 *    spec `docs/specs/2026-09-24-order-delete-and-filters-design.md` §2.1.6 / §2.1.7 / §3-D10 / §7.2；
 *    计划 `docs/plans/2026-09-24-order-delete-and-filters.md` T7）。逐条：
 *    - **删的 5 个节点**（证据件 `public/replica/order-orderlist.html`）：`div.group`（「阵营」筛选，
 *      **整块**不是只删内层 `<select>`）、`div.task`（「排团团号」筛选）、三颗工具栏按钮
 *      `setOrderToTaskBtn` / `cleanOrderBtTaskBtn` / `assOrderToTaskMangerBtn`（`data-tb`）。
 *    - **实测计数**（判据钉在 `pages/__tests__/order-orderlist-phase1.spec.ts`）：证据件里 `class` 的
 *      **独立 token** `group` **4 → 0**、`task` **1 → 0**；`data-tb` 总数（含搜索那颗 `seachBtnOk`）
 *      **16 → 13**（即工具栏 15 → **12** 颗）；筛选区的 `name="…"` **25 → 23**；筛选区
 *      `class="layui-form-label"` **11 → 9**。
 *      ⚠️ **现值（实测）**：上面那条是删控件**当时**的数；
 *      今天 `data-tb` 总数 **12** = 工具栏组内 **11** 颗 + 组外 1 颗（搜索那颗 `seachBtnOk`）
 *      （组内 11 由 `pages/__tests__/order-orderlist-phase1.spec.ts` 钉在证据件上）。
 *      ⚠️ 再裁定删「派单人 / 平台单号」两块之后，同两条命令实测
 *      `name="…"` **23 → 21**、`class="layui-form-label"` **9 → 7**（见「已知差异 52」）。
 *      ⚠️ **必须按 token 比、不能按子串比**：`layui-btn-group`（工具栏那一组的外壳，**是另一个类名**）
 *      里含子串 `group`，`/class="[^"]*\bgroup\b/` 这种正则在 `-` 处也算词边界 ⇒ **会连它一起命中**
 *      （实测：删前命中 5 行 = 4 个真 `group` token + 工具栏外壳那一行）。对标用的 `$(".group")` 是
 *      类选择器、只认独立 token ⇒ 判据同口径。
 *    - **`NOT_OPEN_FILTERS` 15 → 13**（删 `gameGroup` / `taskSelect` 两条；`recType` / `popgameName`
 *      的移出归 T9，**已完成**——最终值见下面第 51 条）；**`NOT_OPEN_TOOLBAR` 6 → 3**（只剩 `exportBtn` / `asyncHomeOrderPriceBtn` /
 *      `builderSellEmpOrderBtn`）。
 *      ⚠️ 再裁定：`operator` / `keytid` 两块也真删 ⇒ 本表 **2 → 0**（见第 52 条）。
 *    - **`applyGroupTaskVisibility` + `RUNTIME_STYLE_ID` 整条删除**（含 `mountOrderListPage` 里的
 *      调用点与文件头「已知差异 9」的 parity 说明）：删完两个目标之后那条规则就是对**空集合**调
 *      `.hide()` 的死码（spec §3-D10 / E-0011）。
 *    - ⛔ **没删的**：① 表格列那条链的另一半 `taskman` / `#groupTh`——**证据件里 0 命中**，是对标
 *      侧的空转 no-op，没证据、别碰；② **行内**那颗「加入团」（`joinTask`，`buildOrderCols` 的
 *      `btnfun` 运行期渲染）——业务方删的是**工具栏**那三颗；③ 表格里原「阵营」列 `_group` 早在
 *      第二批列改动里就删了（D-0094，见「已知差异 22」）⇒ 证据件里既没有 `#col_group`
 *      也没有 `field: '_group'`（实测 `grep col_group` = 0 命中）。
 *    - ⚠️ **业务方删掉的能力无法通过回收站之类的路径找回**（spec §9-R6）：排团 / 阵营是**真删**，
 *      不是软删；本仓模型里本来也没有阵营字段与排团模型（spec §2.2 / §2.3 的可行性判据）。
 *
 * 51. ⭐ **T9 筛选接线：11 个筛选接上后端参数 + `#popgameName` 从占位换成真控件 + 两张清单收口**
 *    （**D-0144**；spec §5.4 / §5.6 / §7.5 / §7.6；计划
 *    `docs/plans/2026-09-24-order-delete-and-filters.md` T9）。逐条：
 *    - **`NOT_OPEN_FILTERS` 13 → 2**（只剩 `operator` / `keytid`）；工具栏那份不动（仍 **3**）。
 *      ⚠️ 再裁定（原话「平台单号这个删掉不需要了 派单人也去掉吧」）：这两块**整块真删**
 *      ⇒ 本表 **2 → 0**（今天为空数组），见「已知差异 52」。
 *      实装的 11 个：`orderSoures` / `bussType` / `inputOrderKefu` / `kefuType` / `viewLeakOrder` /
 *      `showPriceType` / `minPrice` / `maxPirce` / `selectSeach1` / `recType` / `popgameName`
 *      （逐控件映射与「值的处理」见 `toOrderSearch` 函数体下半段）。
 *    - **`#popgameName` 不再是占位 `div`**：`renderPopgameName` 把它换成真 `<select>`
 *      （`xm-select-radio` + `xm-select-search`），候选走新端点
 *      `GET /api/business/orders/filter-options/`（spec §5.6，挂本页自己的 `order.list` 节点）。
 *      ⛔ 原先那两条占位处置（`POPGAME_MARKUP` 折叠态标记 + `data-notopen="打手/发单人"`
 *      + `XMSELECT_STYLE_ID` 那 9 条手搓样式规则）**整段删除**。
 *    - **两条与对标的差异（如实登记）**：① 打手 / 发单人**按用户 id 精确筛**，对标发单人是
 *      **名字串**（§5.5-#9）⇒ 没有「改名后筛不到」的文本列降级；② 候选名单的**数据范围**是
 *      「本租户未删订单里实际出现过的人」（对标可能是「该游戏下全部账号」，U14 未取到）⇒
 *      可能出现「对标能选到、我方选不到」（如从未接过单的账号）。
 *    - **候选名单有上界**（`limit` 默认 200 / 上限 500）；`*_truncated` 为真时**不提示**
 *      （业务方拍板「不做提示，有搜索功能就行了」——原先自拟的截断提示
 *      `TAKER_OPTIONS_TRUNCATED_TEXT` 已随之删除；对标那一格走 `remoteSearch`、本就没有
 *      这个情形）。⚠️ 被截断的人在控件里本地搜不到（残差 §9-R17）。
 *      ⛔ **改准（2026-11-09，第 68 条）**：候选改**远程关键词搜索**后不再预载全量名单，
 *      「截断 + 本地搜不到」这组前提取消（§9-R17 随之作废，残差见第 68 条）。
 *    - **两处「控件 → 参数」的入口是本批才补上的**（否则那两个映射是死路径）：
 *      `#orderSoures` 的 option 用既有 `fetchOrderSources()` 填（此前恒空）；
 *      `selectSeach1=10` 走对标的 `layer.prompt`（标题 / 校验 / `换行→逗号` / 关层复位逐条照
 *      `orderlist.js:2713-2745`）⇒ 批量搜单号真能用。
 *    - ⚠️ **`#orderSoures` 那个控件今天的代价**：字典取不到时下拉为空 = 这一项筛不了
 *      （不报错、不拦页面），来源字典被删过的历史单按 FK id 筛不到（§9-R7）；
 *      「客服」（`inputOrderKefu`）的候选名单**已补上**候选源
 *      `GET /api/business/orders/kefu-options/`（值 = 用户主键、文本 = 显示名）——
 *      ⛔ 原登记「候选名单**仍然**是实装 3 的代价（§9-R8 / R16）」**作废**（见第 56 条）。
 *    - ⭐ **`showPriceType` 的 `3/4` 两档已实装**：`3 接发组合` ⇒
 *      弹层 `.seachMutPriceDiv` 那 4 格（接单价 / 发单价各一对区间）；`4 搜亏本单` ⇒ 只发档位本身
 *      （那一档**不使用**金额区间）。⛔ **改准本行原文**「前端把它们归到「尚未开放」并**不发**
 *      `show_price_type`（§9-R11）」——那个事实随实装作废，别再按它理解本页。
 *    - ⚠️ **parity 说明**：`#popgameName` 从占位 `div` 变真控件会**改变** `filters.controls`
 *      的序列（本文件头 `:64-65` 早就登记过那处错位）——本轮**没有**重跑 parity 探针，
 *      那段说明按此条更正：旧序列已失效，别再拿它当判据。
 *
 * 52. ⭐ **业务方再裁定：删掉「派单人」与「平台单号」两个筛选块**（原话「平台单号
 *    这个删掉不需要了 派单人也去掉吧」；承接第 51 条的收口口径）。逐条：
 *    - **删的 2 个节点**（证据件 `public/replica/order-orderlist.html`，**整块**不是只删内层控件）：
 *      「派单人」（`<div class="layui-inline layui-hide">` + `label` + `select[name=operator]`）、
 *      「平台单号」（`<div class="layui-inline">` + `label` + `input[name=keytid]`）。
 *      ⛔ **没删的**：`qainput`（「搜QA资料」那个 `layui-hide` 块）与 `orderSta` / `sesech` /
 *      `SeKey` 等其余块全不动；⛔ 也不要照对标把这两块「修」回来。
 *    - **`NOT_OPEN_FILTERS` 2 → 0**（今天为空数组）：本表是「证据件里还有这个控件、但后端吃不掉」
 *      的清单，两个控件都真删之后**没有条目可登记**——留空条目就是「登记了却没有控件」的假条目
 *      （第 51 条同族）。
 *    - **`usedNotOpenFilters` 的语义不变**：它不只遍历本表，还负责 ③ 价格口径 `4 搜亏本单` +
 *      主栏区间被改过（那一档**不使用**金额区间 ⇒ 说一句实话，⛔ 不许静默丢）与「订单状态映射不到」
 *      那一支 ⇒ 本表清空**不影响**这两条提示；判据钉在 `pages/__tests__/order-orderlist.spec.ts` 的
 *      `usedNotOpenFilters` 用例（档 `4` 的那句话与「订单状态 + 那句话」的合并提示）。
 *      ⚠️ **改准**：本行原写「③ 价格口径 `3/4`（后端不吃这两档 ⇒ 必须提示「尚未开放」
 *      且**不发** `show_price_type`，§9-R11）」——两档实装后**整支删除**，改准为上面这条。
 *    - **实测计数**（同一片区域 = `<form class="layui-form">` 起到 `#toolbarDemo` 那段；
 *      「删前」= 本提交的父提交 `7d79ff16`）：
 *      `name="…"` **23 → 21**、`<label class="layui-form-label">` **9 → 7**、
 *      `class="layui-inline`（**前缀匹配**，本批删的两块里「派单人」那块带 ` layui-hide`）
 *      **18 → 16**。三条命令见 `docs/specs/2026-09-24-order-delete-and-filters-design.md` §7.2 的
 *      ⛔ 更正块。
 *    - **代价（如实）**：这两个筛「我们没有能力」这件事本身**没变**（`operator` 对标从不送值、
 *      `keytid` 全仓无平台订单模型），变的只是**页面不再有那两个控件** ⇒ 与对标多一处
 *      「不再逐像素一致」的已知偏离（同族：D-0093 / 第 50 条）。
 *
 * 53. ⭐ **指派弹层保留块 1:1 补齐 + 发单价格语义（D-0147）**——源 spec 是
 *    `docs/specs/2026-09-24-order-throwprice-and-assign-dialog-design.md` §4.2 / §5-B。
 *    **只补保留块**，⛔ 不加回「保存筛选 / 排序方式 / 开启智能派单 / 指定上号电话」四块
 *    （第 19 条与那两条 `五处不做` 的用例仍在守）：
 *    - **发单价那一格换源换键（B12）**：显示 = **订单发单价** `Order.hall_price`（与列表「发单价」
 *      列同源，初值经行标记的 `data-hall-price` 带进来），载荷键 = **`price`**；
 *      ⚠️ **列表显示**那一格在有平台推送时优先显示推送行 `price`
 *      （见 `throwPriceRow`），但指派弹层/改价**写的仍是本地 `hall_price`**，这一条不变。
 *      ⛔ 不再送 `fee`（`AssignSerializer` 仍收 `fee`，本批前端不用，R-3 的契约清理留待下一批）。
 *      预计绩效**由服务端重算**（§4.1 的算式），前端不镜像它——见 V13 的更正小条。
 *    - **三格改行内编辑（B11）**：点 `<a>` 出 `<input>`，`blur`/`Enter` 提交、`Esc` 取消；
 *      **只改内存**（`cellValues`），必须再点「保存指派」才落库——对标
 *      `table.on('edit(tableList2)')` 也是这样。⛔ 原先的 `layer.prompt` 三条路已删。
 *    - **「用户职位」改读接口返回的 `roles`（B12b / R9）**：值 = `AssigneeOptions.roles`
 *      （本租户全部职位名）+ 第一项「全部」；默认仍选「代练员」。
 *      ⚠️ **V-G（静默回退）**：本租户职位表里没有「代练员」时，默认档落到「全部」——不弹提示、
 *      不为这件事写 console；回落之后按新档**重拉一次**名单（否则列表还是按一个不存在的职位筛的）。
 *    - **逐字值**（B1–B10）：`form.layui-form.layui-form-pane[style="padding:10px"]`、
 *      三处 `lay-filter`（`userRole` / `bussTypechange` / `seachBtn`）、搜索按钮外包两层 div、
 *      `div.user[style="margin-left:0px"]`、两个 `legend[style="font-size:14px"]` 与 legend 内的
 *      红字 `span[style="color:#ff0000;font-weight:600"]`、右表三格 `a.t0/.t1/.t2`
 *      （`t0` 带对标的 `color:#F3723A`）、「移除」的 `lay-event="delAss"`、两颗底部按钮的
 *      `id="saveBtn" lay-filter="saveBtn"` / `lay-filter="delBtn"`。
 *      ⚠️ 弹层壳的实参（`type:1` / `title` / `area` / `anim` / `shade` / `shadeClose` / `skin`）
 *      与两颗按钮的**文案**本批**一字未动**。
 *    - **可见差异（如实）**：本页仍是**页内弹层**（对标那个 85%×95% 的 iframe 子页，第 19 条）、
 *      `div.user` 不再带我们自加的 `max-height:360px;overflow:auto`（照对标只留 `margin-left:0px`；
 *      层内容器本身 `overflow:auto`，长名单仍可滚动）。
 *
 * 54. ⭐ **绩效弹层按 live 布局复刻 + 两处「与业务方截图不符」**——
 *    源 spec 是 `docs/specs/2026-09-25-perf-dialog-sync-and-complete-gate-design.md` §2.2；
 *    逐值权威是 `.tools/recon/order-pages/live-perfdialog/recon-perfdialog-layout.md`（不进 git）。
 *    - **落成的逐值**：包裹层换成 `form.layui-form` + `div[style="margin-left:10px;margin-top:10px;
 *      margin-right:10px"]`（对标页面 L79-L80）；七列的 `width` / `align` 走 `PERF_DIALOG_COLS`；
 *      颜色 `#006EE9` / `#F3723A` / `#666`；两颗按钮的 `class` / `title` / 图标逐字；
 *      40px 行高与 zdy.css 的单元格边框、padding 走 `PERF_DIALOG_STYLE`。
 *    - ⚠️ **两处与业务方截图不符，按「live 实测」落，等业务方点名再改**：
 *      ① **表头 live 是白底**（整个 layui.css 里没有 `thead` / `th` 的底色规则，报告 §6.1 实测）；
 *      截图里的**浅灰**没有复刻（要改只需一行）。
 *      ② **图标是铅笔 `layui-icon-edit` / 垃圾桶 `layui-icon-delete`**（页面字节 + 字体字形两路实证，
 *      报告 §5.2），**不是** ✔/✖（那套码位本页一个都没引用）。
 *    - **金额不补两位小数**（登记同 spec §3）：对标是 JSON 数字直接拼 HTML ⇒ `120` / `0` / `9.9`
 *      原样显示。⚠️ 与本页其它地方（`money()` 补两位）**不同**，是有意的。
 *    - **「记录时间」按 `MM-DD HH:mm` 显示**（`formatPerfRecordTime`；对标 `createTime` 的 11 字符
 *      形状）——与本页「录单时间」列的 `YYYY-MM-DD HH:mm` **形状不同**，也是照各自对标。
 *    - ⛔ **仍不做**：iframe 子页运行面、`table.render({url})` 第二条取数链、对标那个死选择器
 *      `$('.edittemp')`、hidden `#orderId`（第 40 条 / V9 不变）。行内编辑 / 铅笔 `layer.prompt` /
 *      垃圾桶软删三条**行为一字未改**（复刻的是布局）。
 *
 * 55. ⭐ **第 19 列「订单截图」的两颗按钮接了真弹层**——
 *    源任务书 `docs/plans/2026-09-25-media-storage.md` Task 8，取值权威是
 *    `.tools/recon/media-storage/Order__UploadPic.html` / `Order__PicMange.html`（不进 git）
 *    与**本页自己的** `OrderList20191212.js:4044-4069`。
 *    - **不再是「尚未开放」**：`.upImg` → `layer.open` 装 `order-screenshots-upload`、
 *      `.mangeImg` → `order-screenshots-manage`；层参数逐项取本页 PC 分支
 *      （`['70%','85%']`+`anim:8` / `['80%','85%']`+`anim:1`，`shade`/`shadeClose`/`type`/
 *      `skin` 同对标），见 `openScreenshotLayer` 的注释。该分支**必须排在 `[data-label]`
 *      兜底之前**（两颗按钮同时带着 `data-label`，否则会被「尚未开放」吃掉）。
 *    - **一颗不可见的新属性**：`data-order="<单号>"`（对标那一版是 `data-id="res.orderId"`，
 *      我们载荷里没有订单主键 ⇒ 与本页其余行内定位属性统一用 `order_no`，见差异 9/10）。
 *      ⚠️ 它 **+** `data-label` **+** `lay-tips` 三处都还在，`class` 与图标一字未改。
 *    - ⚠️ **如实登记的偏离**：对标从**顶层窗口**开层（盖住整栏），我们开在 island iframe 内
 *      （只盖内容区）——与「进订单详情」那一处同一个偏离（本页跑在 iframe 里、Vue 外壳没有 layui）。
 *    - ⚠️ 两个弹层内部的「与对标不同」逐条登记在各自的文件头
 *      （`order-screenshots-upload.ts` / `order-screenshots-manage.ts`），**不在这里重复**。
 *
 * 56. ⭐ **「显示人统一成昵称 + 销售客服候选」：「客服」（`inputOrderKefu`）
 *    那一格补上候选源**——源 spec 是 `docs/specs/2026-10-04-person-display-name-and-kefu-options-design.md`
 *    §2.1 / §2.2。三条与对标的差异，逐条登记：
 *    - ① **候选源是本线新增的 `GET /api/business/orders/kefu-options/`**（值 = 用户主键、
 *      文本 = 显示名；挂本页自己的 `order.list` 节点）。对标那一格由运行期 JS 灌候选
 *      （`getCustomerService()` 一族），我们此前**从来没填过** option ⇒ 下拉恒空
 *      （第 51 条那句原登记就此作废）。候选口径 = 内置角色「客服」+「客服主管」的未软删账号
 *      ∪ 当前登录人（业务方裁定，见 `apps/business/selectors.py::kefu_options` 的 docstring）。
 *    - ② **候选是平铺的**（formselects 单选），**不复刻**对标那个「**按游戏分组的人员树**」
 *      （xm-select）——与绩效页 `#popgameName` **同一处置**（见 `emp-emplist.ts`「已知差异」第 10 条）。
 *    - ③ **候选截断不提示**（业务方拍板「不做提示，有搜索功能就行了」——原先
 *      自拟的 `KEFU_OPTIONS_TRUNCATED_TEXT` 已删除；对标那一格走 `remoteSearch`、本就没有
 *      这个文案）：候选超过后端默认 200 人时**静默截断**、页面照常可用（同族的
 *      `TAKER_OPTIONS_TRUNCATED_TEXT` 见第 51 条，同一次拍板删除）。
 *
 * 57. ⭐ **统计句的状态段改成可点（业务方要求）**——业务方原话「上面那个统计值
 *    未分配 待抢单 代练中 鼠标选中的时候是可以点击的 类似这样筛选的功能，需要完善」。
 *    对标**运行期**那句的每个状态段就是可点的 `span.statustab`（出处
 *    `.tools/recon/js/OrderList20191212.js` L2595-2620：点它 ⇒ 清空「订单状态」再设成唯一一档
 *    ⇒ 点搜索），本批照做（`renderSummaryText` 上原「故意不做成可点」那句已按「只增不改」改准）。
 *    两处**有意差异**：
 *    - ① **有意新增 `title`**（与 `lay-tips` 同一句话）：本页 vendored layui 里 `lay-tips` 是
 *      **死属性**（见「已知差异 6」）⇒ 只照抄 `lay-tips` 的话悬停什么都不显示。同族处置见
 *      「已知差异 24」的 V1；⛔ 不做「看着能点、悬停却不显示提示」的假控件。
 *    - ② **点击只认**统计句那个 `.total` span 里的 `.statustab`（`data-status` 用**状态码**、
 *      中文由 `statusLabel()` 现算，⛔ 不抄第二张中文表）；实现见 `onSummaryStatusClick`。
 *      ⚠️ 顺带一处**属性名**的有意差异：对标运行期写 `data-tit="未分配"`（**中文**），我们按 spec 写
 *      `data-status="UNASSIGNED"` ⇒ 本页全文件 `data-tit` 出现 **0** 次。
 *    - ③ 统计句被隐藏（`display:none`）时点击**一律不响应**——判据是 `onSummaryStatusClick` 里那句
 *      `display === 'none'` 判断，⛔ 不是「靠 CSS 点不到」（`renderSummary` 的隐藏支不清 `innerHTML`，
 *      旧 `.statustab` 仍在 DOM 里，见那里的注释）。
 *
 * 58. ⭐ **工具栏「记退款」按钮加 `layui-btn-danger`（红，业务方要求）**——业务方
 *    原话「以后常用，需要明显看得见」（对标/证据件原文是 layui 默认绿
 *    `class="layui-btn layui-btn-sm"`）。改的是**我们仓库里的证据件副本**
 *    （`public/replica/order-orderlist.html` L296，先例 = D-0140 改 `#project` 的 `maxlength`），
 *    用的是 vendored layui 2.13.9 自带的警示档红色，**不引入新样式**。登记见
 *    `docs/FRONTEND-REPLICA-STANDARD.md` §7「已知会不同」；⛔ 不单独立决策（按复刻偏离登记）。
 *
 * 59. ⭐ **筛选栏新增「订单分组」一颗（对标没有这个筛选项）**（计划
 *    `docs/plans/2026-10-26-order-source-groups.md` §5）：整块 `.layui-inline` 由页面**运行期注入**
 *    （`injectSourceGroupFilter`，锚点 = 同排「订单来源」那一块之后；手法照 `renderPopgameName`
 *    的「运行期造控件」），控件与 `#orderSoures` **同族**（原生 `<select>` + `xm-select-radio`
 *    等 formSelects 属性，`name="sourceGroup"`），首项「全部分组」。
 *    ⚠️ **两套 name 别混**：本页筛选栏用 `sourceGroup`，来源管理页弹层那份叫 `soureGroup`。
 *    ⚠️ 注入与填 option 都必须在 `initFormSelects` **之前**（formSelects 在 `render()` 那一刻读
 *    option）；候选取不到时只剩首项、**不拦页**（同 `fetchGames().catch(() => [])` 口径）。
 * 60. ⭐ **工具栏多一颗「同步收款」（业务方拍板；计划
 *    `docs/plans/2026-10-26-receipt-sync-button.md`）**——对标那一排（证据件 L291-303 的 11 颗）
 *    **没有**这颗，是本线的**新增功能** ⇒ **证据件一个字不动**，按钮在
 *    `root.innerHTML = 证据件` 之后**运行期注入**（`injectReceiptSyncButton`，款式与旁边一致：
 *    `layui-btn layui-btn-sm` + `data-tb`）。三条如实登记：
 *    ① **按节点 `order.change_payment_status` 渲染（fail-closed）**：没这个节点就不画
 *      （⛔ 不渲染点了必 403 的按钮，D-0154）；它与后端 `ORDER_PERMISSIONS["xgj_receipt_sync"]`
 *      **同一个 code**（复用「改收款状态」这颗，⛔ 不新增权限点）。
 *    ② **弹层只收两个日期**（登录后默认「最近 7 天（含今天）」）；提交后端点**立刻**返回
 *      `{accepted: true}`，逐笔查询在后台线程里跑 ⇒ 页面**不轮询、不显示假进度**。
 *    ③ **「订单列表里已收款、收款明细页没有这笔」是已知行为**（业务方确认）：
 *      收款明细只读推送记账行 `XgjReceiptRecord`，手动同步不写那一张表（计划 §0 的 I6）。
 *
 * 61. ⭐ **P3 / P5 / P6 / P7（业务方真机反馈，计划 `…-dailiantong-qr-…`）**：
 *    - **P5 订单号紫色**：有**活跃**平台订单（`platform_push.is_active === true`）
 *      ⇒ 第 2 列订单号文字 `#D15FE8`；没有活跃推送（无推送 / 全部结束）⇒ `#0094ff`。两值**逐字**
 *      取对标 `orderlist.js:1868`。⭐ **B1 改准**：第 3 列「系统内置单号」**同样**转紫
 *      （对标 L1418 是同一条三元）；此处原先登记的「有意保留默认蓝」**已作废**。
 *      ⭐ **真机再改准**：判据从「有过推送行」收窄成 **`is_active`** ——
 *      平台订单全部结束（撤销完成 / 已结算）后要**恢复普通内部订单样式**，
 *      否则单号一直紫、看着像还在平台跑（判据统一在 `hasActivePlatformPush`）。
 *    - **P6 三角展开**：有推送行 ⇒ 订单号左侧一颗 `tree_btn` 三角（对标 `orderlist.js:1865`），点开在该
 *      行下方插**平台子订单的行**（每条推送行一行）。⭐ 三角**保留**（可查历史），
 *      但**颜色跟着 `is_active` 走**（无活跃 ⇒ 默认蓝）。
 *      ⭐ **B3（业务方「展开区太丑」）重做**：原来这里是「一行跨列 + 内嵌 5 列小表」，
 *      现在照对标改回**真行** —— 列 / 表头 / 列宽 / 行高 / 边框全部继承主表（对标是把它自己那套
 *      28 列的平台订单行 `splice` 进同一张表），子行白底 + 订单号格 20px 缩进 + 勾选格隐藏，
 *      平台侧取不到值的格子显示 `—`。详见 `platformChildCellHtml` 上面那段逐条对照表。
 *      ⛔ 子行不进 layui 数据缓存（批量 / 排序 / 翻页不认识它）；⛔ 无推送不画三角。
 *      「编辑」= 打开本系统订单详情（代练通页签里可改价 / 补时）；**平台子单的标题 / 内容本版不能编**。
 *    - ⭐ **业务方最终定案：只搬走三颗，撤回箭头保留** ——
 *      「加款」（`pushJdpAmount`）/「重发」（`pushJdpRepush`）/「协商撤销」（`pushJdpRevoke`）
 *      连同它们的弹层 / 常量 / 点击委托整组删除，**搬到「第三方平台订单管理」页**去做。
 *      ⛔ 短信早已删除。
 *      ✅ **保留**：P3「从所有平台撤回」绿箭头（`pushJdpCancel`，未接手 11 / 未知那档）
 *      —— 业务方补充：撤回不在搬迁清单里。⇒ 本页的平台写动作**只有撤回**。
 *      ⛔ **操作列不再追加任何状态文字**（业务方：「谁说要加这个了」）：
 *      原先那颗 `tt-push-terminal`（「正在代练」之类的灰字）连同样式 / 产出逻辑整段删除，
 *      单元格里**只留按钮**（状态看「状态」列）。旧说明里那句「不能再撤回或协商撤销」随之消失。
 *
 * 62. ⭐ **P4「抛单按钮常驻」（业务方真机 bug）**：那颗地球按钮不再按行数据
 *    （`platform_push.state` 能不能重发）隐藏——只要入口探针通过就**每行都有**。已抛过的单点进去，
 *    由发单弹窗里的 `can_push/reason` 说明为什么不能发（后端也会拒）。旧判据 `canRethrowPush`
 *    已整条删除。
 *
 * 63. ⭐ **B1–B5（业务方报的五个问题）**：逐条处置与证据
 *    - **B1 五列紫色**：订单号 / 系统内置单号 / 角色名 / 代练内容 / 角色信息 —— 有推送行时
 *      **五列都紫** `#D15FE8`（无推送保持默认：前两列蓝、后三列不打颜色）。逐列出处见
 *      `pushTextStyle` 的 docstring（对标 `orderlist.js` L1868 / L1418 / L1875 / L1896 / L1911）。
 *    - **B2 来源单号 + 右键复制**：**只改「第三方平台订单管理」页**那一列「系统单号」
 *      （`order-coreordercenter.ts::sourceOrderNoOf`）——业务方原话点名的是那一页；管理订单页
 *      「系统内置单号」按业务方既定规则**恒显示 `order_no`**，本批不动它。
 *      右键复制两条路（clipboard API → `execCommand` 兜底）落在 `layui-island/clipboard.ts`，
 *      两页共用。
 *    - **B3 展开区重做**：见上面 P6 那一条（真行，列宽/表头/行高/边框继承主表）。
 *    - **B4 操作列补两颗（⛔ 最终定案已整组删除）**：加款 / 重发曾与编辑 /
 *      撤回箭头 / 协商撤销 / 发单同列（对标 `CoreOrderCenter.js:502`）。最终定案把它们
 *      连同协商撤销一起搬到「第三方平台订单管理」页 ⇒ 本页的操作列只剩三颗。
 *    - **B5「点 × 卡死」根因与修法**：`layer.open({content: 裸 DOM 节点})` → ① vendored layui 对
 *      object 形状的 content 走 jQuery 分支（`r.parents(...)` 不存在 ⇒ TypeError）；② 即使包了
 *      `layui.$`，jQuery 的 `wrapAll` 对**游离节点**也不会把层插进文档 —— 而 `layer.open` 已经
 *      先把全屏 `.layui-layer-shade` 插进 body 了 ⇒ **只有遮罩、没有弹窗、点哪都没反应**（页面上
 *      看就是「卡死」）。修法 = `document.body.appendChild(panel)` → `layui.$(panel)` → `layer.open`。
 *      真浏览器复现/复验脚本在 `/tmp`（不进 git），用例见 `order-orderlist.spec.ts` 的「B5：」那条。
 *
 * 64. ⭐ **G1：差距表 §2 的另一批 ❌ 补上了**——逐条与对标位置：
 *    - **❌1 勾选合计**：勾行时把「接单价合计 / 发单价合计」写进页脚 `.selectTotal`
 *      （对标 `orderlist.js:2530-2539`，文案取 i18n `selectTotal1/2`，`:591-592`）。
 *      值取 `total_price` / `hall_price`，用「分」累加避免浮点尾数；两段都为 0 ⇒ 清空（同对标）。
 *    - **❌2 勾选范围**：列表取数时清空勾选，批量以本页可见的勾选为准。
 *    - **❌3「陪」徽标**：`order_type === 'COMPANION'` ⇒ `lableb layui-bg-orange` + tips
 *      「这是一笔陪玩订单」（对标 `1792-1794`；我们载荷没有 `levelMark`，用同义的订单类型）。
 *    - **❌4「标记」徽标 + 行内编辑**：`Order.tag` 非空 ⇒ `lableb changeTag` / 色 `#1E9FFF` /
 *      正文取前两个字符（`1805-1807`）；点它开 `layer.prompt`（标题「编辑订单标记」/上限 50）
 *      → `PATCH /orders/<no>/` `{tag}`（对标 `4265-4288` 的专用端点我们没有，走既有 PATCH）。
 *    - **❌7 代练/陪玩内容行内编辑**：那一格由 `<span>` 换成 `a.modityProject`（`1899-1901`），
 *      点它开 prompt（「请输入新的订单代练项目」/上限 100）→ `PATCH {project_text}`（`4781-4820`）。
 *      ⚠️ tips 少「项目分类：…」一段——列表载荷没有分类名，⛔ 不拼空段。
 *    - **❌8 单元格 `title`**：`done` 后给每个 `.layui-table-cell` 写 `title`=可见文本（`2657-2662`）。
 *    - **❌9 行点击高亮**：`table.on('row(datatable)')` ⇒ 只给被点那行加 `.selectTr`
 *      （对标 `2431-2436`；样式 `.selectTr{background-color:#f2f2f2!important}` 本就在证据件里）。
 *    - **❌10 双击行展开平台子单**：复用三角的 `togglePlatformChildren`（对标 `2359-2361`）。
 *
 * 65. ⚠️ **G1：以下四条**（差距表 ❌5 / ❌6 / ❌11）**仍不做，但已登记「缺什么」**——⛔ 不硬造假徽标：
 *    - **会员单徽标（「会」）**：对标 `orderlist.js:1796-1799` 的判据是 `res.customerId > 0`。
 *      我们**没有客户实体**：`Order` 上只有 `customer_phone` / `customer_qq` / `customer_ww` 三个
 *      自由文本列，**没有 `customer_id`**（`apps/business/models.py` 无客户表关联），列表载荷也没有
 *      任何「会员等级 / 会员单」字段 ⇒ 画不出来。
 *    - **未读徽标（「新」）**：对标 `1796-1798` 的 `res.isRead == 'N'`。我们**没有读态**：`Order`
 *      没有 `is_read` 列，也没有「客服已读」表 / 端点 ⇒ 每单都会被判「新」或「已读」，两者都是假话。
 *    - **黑名单图标**：对标 `roleNameRow`（`1875`）的 `res.backListId > 0`。我们**没有黑名单数据源**：
 *      `Order` 没有 `back_list_id`；「黑名单管理」页（`customer-backlist.ts`）目前只有复刻标记、
 *      **没有对应端点**（该页零 API 调用），也没有客户表可供关联 ⇒ 图标无从判断。
 *    - **职业统计弹层**：对标 `2626-2630 / 3356 / 3466-3505` 的 `.zyTotal`（按游戏的职业计数）。
 *      它依赖**排团 / 职业**那套数据，而排团入口已按 D-0144 **真删**（本页连 `.task` 筛选块都不在了）；
 *      后端也没有「按游戏统计职业」的读面 ⇒ 按钮保留、点击仍走 `notOpen`（「尚未开放」）。
 *
 * 66. ⭐ **G1 附带：❌3「陪」/ ❌4「标记」的前端已就绪，等待后端两键（已排期）**：
 *    两个徽标（templet + 行内编辑 + 测试）本线全部落成，判据是 `order_type === 'COMPANION'`
 *    与 `tag !== ''`。⚠️ **订单列表载荷在今天这一版里还没有这两个键**：
 *    `apps/business/api/serializers.py` 的 `OrderListSerializer.Meta.fields` 与
 *    `OrderSplitListSerializer.Meta.fields` 都没有 `order_type` / `tag`（后者只在**回收站**的
 *    `RecycleBinListSerializer` 里追加了 `tag`；`order_type` 只有**详情** `OrderSerializer` 有）。
 *    - 处置（业务方拍板 **A**）：**另派后端线**在 `OrderSplitListSerializer.Meta.fields`
 *      做纯加法追加这两个只读键（只影响订单列表这条读面；大厅 / 回收站 / 待归属的键集不动）。
 *      ⇒ 本线按「**有键就画、没键不画**」保留：后端上线后**不用改前端一行**，徽标自动可见。
 *    - `api/orders.ts` 把两键写成**可选**（纯类型加法），缺键 ⇒ 空串（同「无推送不画徽标」）；
 *      ⛔ 不为此逐行 `GET /orders/<no>/`（一页 20~100 发、还可能 403，是 N+1 + 噪声）。
 *    - ⛔ 这**不是**「缺数据源⇒登记不做」：数据源（模型列 `Order.order_type` / `Order.tag`）
 *      本来就有，缺的只是**列表读面的两个出口键**，且已排期补上。
 *
 * 67. ⭐ **G2 最终定案：操作列 = 三颗（编辑 / 退回大厅 / 发单）+ 未接手时撤回箭头**：
 *    - **每档的确切集合**（业务方定案后逐档确认，⛔ 别再写「恰好三颗」一刀切）：
 *      ① 未抛 / 无推送 ⇒ 编辑 + 退回大厅 + 发单（3）；② 已抛未接手（远端 11 / 未知）⇒
 *      那三颗 **+ 撤回箭头**（4）；③ 已抛已接手（12–15）⇒ 3 颗 + 一行说明（无箭头，协商撤销
 *      在第三方页）；④ 终态（17 / 16 / CancelStatus=16 / 本地 closed / 撤销在途）⇒ 3 颗 + 说明。
 *      其中「退回大厅」在 `WAITING_GRAB` 那档换成 `.cancelThrowHome`「从内部接单大厅撤回」。
 *    - **G2 的「协商撤销」删了**（业务方最终定案：它和加款 / 重发一起搬到
 *      「第三方平台订单管理」页）⇒ `pushJdpRevoke` / `openApplyCancelDialog` / 三格弹窗 /
 *      `auto_arbitrate` 勾选框**整组删除**。
 *    - **加款 / 重发**（B4）同样整组删除（标记 / 弹层 / 入参校验 / 提交收口 / 结果文案全清）。
 *    - ✅ **撤回箭头保留**（业务方补充：不在搬迁清单里）：`pushJdpCancel` 仍是
 *      唯一的平台写动作，只在 ① 之外且未接手时出现。⛔ 操作列**不再有状态文字**（见上一条）。
 *    - ⛔ 短信那颗早在 G2 就整条删掉（业务方「不要短信了」）：`sendsmsorderbtn` /
 *      「发送短信」/ `layui-icon-cellphone` 在本页**一个都不剩**。
 *    - 列宽仍是 **240**（B4 实测值）。本批只删三颗按钮、**不动列宽**（业务方没要求；真机可再收窄）。
 *    - ⭐ **「关层残留面板」的修法随面板一起搬走了**：`end: () => panel.remove()`
 *      针对的是 `content: layui.$(面板节点)`（**object** 内容——layui 关层只 unwrap、不删节点，
 *      于是每次开/关都在 body 末尾漏一张可见表单，真机看起来像列表下方一块黑灰区域）。
 *      G2 把三格面板**整组**搬到「第三方平台订单管理」页后，那边用的是**字符串 content**
 *      （`order-coreordercenter.ts` 的 `negotiateCancelDialogHtml()`）⇒ 层里的节点由 layui
 *      关层时一并删掉，⛔ 不再需要在 `end` 里清理。本页剩下的 5 处 `content: jq` 都是
 *      **页面里复用的静态节点**，各自在 `end` 里 `display='none'` + 搬回原 host。
 *
 * 68. ⭐ **打手 / 发单人下拉改「远程关键词搜索」+ 去预选**（业务方 2026-11-09 拍板「治本」；
 *    计划 `docs/plans/2026-11-09-orderlist-taker-search.md`；背景 = 最大在役租户底集 729 人、预载只取
 *    前 200，`sq1551` 位次 339 ⇒ 预载名单里搜不到）。逐条：
 *    - **两个下拉都改**：`#popgameName` / `#hallPublisherUser` 的 `xm-select-search` 属性值写
 *      端点 URL `/api/business/orders/filter-options/`（vendored formselects：属性**有值** =
 *      开远程搜索，空值 = 只在已加载 option 里本地过滤）⇒ 输入关键词防抖（`delay: 400`）请求
 *      `?search=<关键词>&game=<当前游戏>`，回包即候选；`searchName: 'search'`、`keyVal: 'id'`、
 *      `data: { game }`（当前游戏筛选，切游戏即更新）；`beforeSuccess` 拆统一信封——
 *      `#popgameName` 按**响应时刻**的 `recType` 取 `publishers`（`'10'`）或 `takers`，
 *      `#hallPublisherUser` 恒取 `publishers`；信封 `code≠0` / 形状不合 ⇒ 空数组
 *      （控件自己显示「无匹配项」/「服务异常」，不另造提示）。
 *      ⚠️ 另配 `beforeSearch` 闸（空/空白 ⇒ 不发）：vendor 在**每次 render** 对设了
 *      searchUrl 的控件自动发一次空关键词首搜（formselects-v4.js:313-322，`beforeSearch`
 *      是唯一闸 :358）——不闸住它，「去预选」会被挂载与每次重渲染打回原形。
 *      ⚠️ **改准（2026-11-09 扩面线，存量缺陷修正）**：闸的判定发生在 **render 那一刻**
 *      （`search()` 同步读 `ajaxs[id]`，之后的 setTimeout 不再回头判）⇒ 完整配置等到挂载
 *      收尾才并入的话，首发空关键词请求抢在闸之前发出去（线上「空关键词请求漏过闸」的根因）。
 *      修法 = 挂载早期（`initFormSelects` 之前）先把 `{beforeSearch}` 预埋进两份 `ajaxs[id]`
 *      （`filterSearchGate`），完整配置收尾再深并。
 *    - **去预选**：挂载 / 切游戏**不再**拉全量候选（`loadFilterOptions` 与 `filterOptions`
 *      state 已删）；切档位 / 切游戏把两个下拉**重置为仅占位 option**（清已选 + 重渲染；
 *      切游戏时独立发单人已有选 ⇒ 清空并提示 `PUBLISHER_GAME_SWITCHED_TEXT`）。
 *      空关键词不发请求（`beforeSearch` 闸；下拉显示占位）。
 *    - **对标偏离（复刻登记）**：对标 = 全量 option + 本地搜索；我方 = 关键词远程搜索
 *      （U14 截断的延伸修正，业务方 2026-11-09 拍板）。后端 `filter-options` 加**可选**
 *      `?search=`（显示名 icase 包含）；**不传时行为逐字不变** ⇒ v2 页（`V2OrderManage.vue`
 *      仍在预载调 `fetchFilterOptions`）零影响，`api/orders.ts` 的导出保留。
 *    - **残差（接受，不修）**：候选口径仍是「接过单的人」（从没接过单的账号依旧搜不到，
 *      选了必然筛 0 行）；游戏筛选仍会缩小候选（搜索结果 = 该游戏里接过单的人）。
 *
 * ## 功能：接我们真有的 API（只准用 `api/orders.ts` + `api/config.ts`）
 *
 * | 对标功能 | 我们的接口 |
 * |---|---|
 * | 主表取数 / 分页 / 筛选 | `GET /api/business/orders/`（`layui.table` 的 url + parseData） |
 * | 进订单详情（单号链接 `.orderdetails`） | **原地开层**：`layer.open` 装我们自己的复刻页（`order-orderview`），本页自己的 `85%×92%`——见「已知差异 21」与 `openOrderDetailLayer`。⛔ 不再走 `ctx.onNavigate` |
 * | 行内「编辑」`.submitEdit` | 对标那种 `type:2` 的 95%×95% iframe 大弹窗（`orderlist.js` L3703/L3733-3745），
 *   弹层里装的是我们的录单页（`order-addorder` 的**编辑态**：GET 回填；保存走
 *   `PATCH /api/business/orders/<no>/`，回来后关弹窗 + 刷新列表——见 `openEditDialog`）；
 *   打开这一刻同时上报读操作 `POST /api/audit/events/`（action=`order.open_edit`，spec §6.2） |
 * | 改接单价（工具栏「改接单价」/ 点接单价列） | `POST /api/business/orders/<no>/price/` |
 * | 改状态（工具栏「改状态」/ 点状态徽标 `.setdlsta`） | `POST /api/business/orders/<no>/transition/`（选项取详情里的 `allowed_transitions`）。⛔ **行数据 `platform_push.is_active === true`（还有活跃代练通订单）⇒ 不能手工改状态**：行内点徽标只弹「这张单已抛到代练通，状态由平台决定并自动同步，不能手工修改」（不开弹窗、零请求）；工具栏批量勾选里含任一活跃单 ⇒ **整体拒绝**并列出单号（`batchStatusPushedMessage`），零请求。⭐ **全部推送结束（无活跃）⇒ 恢复普通内部订单，改状态照常放行**（业务方拍板；后端 A2 的闸仍兜底） |
 * | 发到内部接单大厅（**行内** `.throwEdit`） | `POST /api/business/orders/<no>/throw-to-hall/`（`api/hall.ts` 的 `throwToHall`），
 *   形态是**页内那个 `.setprice` 发价弹层**（对标 `throwOrderToInternal`，见 `openThrowDialog`） |
 * | 从内部接单大厅撤回（**行内** `.cancelThrowHome`） | `POST /api/business/orders/<no>/withdraw/`（`withdrawThrow`），先走对标的 `layer.confirm` 确认语 |
 * | 记退款（工具栏「记退款」/ 点「退款」列的金额链接 `.refundFee`） | `POST /api/business/orders/<no>/refunds`（`partialRefund`，body `{refund_total, reason, request_id}`；`refund_total` = **累计退款总额**） |
 *   形态是**页内那个 `.setrefundOrderLog` 弹层**（对标 `refundOrderLogs`，见 `openRefundDialog`；形状与四处字段删除见「已知差异 15」） |
 * | 删除订单（工具栏「删除」`delOrderBtn`） | `POST /api/business/orders/<单号>/delete/`（`deleteOrder`，body `{isDelEmp: 'Y'\|'N'}`），
 *   两跳 `layer.confirm`（文案逐字取对标 `orderlist.js:4392-4403`）、**一次一笔**；成功后 `reload()` + 提示——见「已知差异 48」 |
 * | ⚠️ **上面「删除订单」那一行的现值（实测补记）** | 工具栏「删除」走**批量**端点 `POST /api/business/orders/batch/delete/`（`api/orders.ts` 的 `batchDeleteOrders`，body 含 `order_nos` + 必填 `dry_run` + `isDelEmp`）；上面那行早先写的 `POST /api/business/orders/<单号>/delete/`（`deleteOrder`）在本页今天**没有调用者**（`grep` 实测：`src/**` 里除定义处外只剩注释引用；回收站页的「还原」走 `restoreOrder`）。两跳 `layer.confirm` 的文案与按钮**一字未改**；「一次一笔」那句的更正块见「已知差异 48」 |
 * | 指派打手（工具栏 `setRecMan` / 行内「接手人」格的 `.jsr`） | `POST /api/business/orders/<no>/assign/`（`assignOrder`，body `{user_id, price?, hours?, project?}`；
 *    `price` = **订单发单价**（`Order.hall_price`，非空就送）、`fee` **本批已不再送**——见「已知差异 53」），
 *   形态是**页内自建弹层**（对标是个 iframe 子页面，九区块见 spec §7.1 / `openAssignDialog` / 「已知差异 19」） |
 * | 清除接单人（上面那个弹层里的 `data-act="assign-clear"`） | `POST /api/business/orders/<no>/clear-taker/`（`clearTaker`，body `{isDelEmp}`），
 *   先走对标的**两步** `layer.confirm`（第二句带 `需要删除接手人绩效` / `不需要删除接手人绩效` 两颗按钮） |
 * | 改类型（工具栏「改类型」`changetypesBtn`） | `PATCH /api/business/orders/<no>/`（`updateOrder`，body **只有 `{order_type}`**；金额在
 *   `UpdateOrderPayload` 类型上被 `Omit`，见 spec §5.2），形态是**证据件里那个 `.typebox` 弹层本身**（C4，见 `openTypeDialog`） |
 * | 订单号上右键（恰好 4 项，spec §6） | 查看详情 = **与点单号蓝链同一个 `openOrderDetailLayer`**（T5；对标这两处调的就是同一个 `viewOrderDetails`）；复制订单编号 = 复制**显示值**
 *   （`source_order_no` 非空则它，否则 `order_no`）；复制订单内容 = `fetchOrder` → `content`（为空则 `demand`，C8）；
 *   复制成新订单 = `POST /orders/<no>/copy`（`copyOrder`，Task 3 的端点）→ 刷新列表。逐项见 `CONTEXT_MENU_ITEMS` |
 * | 游戏下拉与字典 | `GET /api/business/games/`、`GET /api/business/servers/`（区服字典只喂「角色信息」列） |
 * | 行内「一键发单到交易平台」（`.pushJdpEdit`） | 列表侧**零 API**：`layer.open({type:2})` 装我们自己的复刻页 `order-pushjdp`（见 `openPushJdpLayer`），**真实内部订单号走 `order_no` 查询参数**传进去。渲染门 = `GET /api/business/platform-integrations/push-availability/` 的 `available`（登录 + `order.list`；fail-closed，见 `pushJdpAvailable`）。⭐ **P4 起按钮常驻**（不再按行数据隐藏，见「已知差异 62」）。**页内提交已接真**（T9d：只发价格 / 两个保证金 / 时限 / 区服 + `platform=10`，见 `order-pushjdp.ts`） |
 * | ⭐ **P3 行内「从所有第三方交易平台撤回」**（`.pushJdpCancel`，未接手的单） | `GET /api/business/orders/<no>/platform-push/`（列出推送行）→ **逐平台** `POST …/platform-push/withdraw/`（载荷 `{platform}`，见 `withdrawFromAllPlatforms`）；没有推送行 ⇒ 只提示、零写请求。⭐ 定案：**保留**（加款 / 重发 / 协商撤销才搬去第三方页） |
 * | ⛔ 行内**其余平台写动作**（加款 / 重发 / 协商撤销） | **最终定案：本页不提供**（搬到「第三方平台订单管理」页） |
 * | ⭐ **P6 订单号左侧三角**（`tree_btn`，有推送的单） | 点开：`GET /api/business/orders/<no>/platform-push/`（**一轮加载只取一次**，`loadPlatformPushRows` 缓存）→ 父行下方插**平台子订单的真行**（每条推送行一行，列宽/表头继承主表）；子行「编辑」= 打开本系统订单详情（`order-orderview`） |
 *
 * ⚠️ 行内的「加入团」**仍不接后端**（对标那颗没有对应端点或用不上）：与其余对标功能一样走
 * 「尚未开放」。⭐ **G2 起「发送短信」整颗删除**（业务方不要了），那句话不再有第二个对象。
 * 「发平台」**已开复刻页且页内提交接真**（`order-pushjdp`，T9d）；
 * 「发内部 / 撤内部」**只剩行内**那两处（工具栏那两颗
 * 已按 D-0093 删除，业务方「单个发即可」）；「记退款」已接上（T-P4-FE-3，见「已知差异 15」）。
 *
 * ## 「改状态」这条路：对标是一个 iframe 弹窗，我们改成了页内单选弹层
 *
 * 对标点状态徽标是 `layer.open({type:2, content:'SetOrderSta?orderId=…&type=1'})`（`orderlist.js` L3803、
 * `orderlist.popups.txt` #12）——那个子页面不在我们的复刻范围里，也没有对应端点。我们把同一件事搬到
 * **页内单选**：结构/尺寸/配色/标题/字号**逐项照对标**（逐项对照表见 `openStatusDialog` 的注释），
 * 选项换成详情 `allowed_transitions`（与 `/transition/` **同源**的节点判据），提交
 * `POST …/<no>/transition/`。实测过的两个坑（都会让业务方看成「改了没反应/没成功」）：
 *
 * 1. **选项必须手动 `form.render('radio')`**：这些 radio 是运行期插进 layer 的，layui 的 form 模块
 *    不会自动接管 ⇒ 不渲染时 `title` 不显示、用户看到的是几个**没有文字的圆圈**（点得中但不知道在选什么）。
 *    所以弹层内容包一层 `.layui-form` 并在 `layer.open` 的 `success` 里 `form.render('radio')`。
 * 2. **成功提示要被放在 `reload()` 之后**：`reload()` 里的 `collectSearch()` 可能弹
 *    「…筛选尚未开放」警告，而 `layer.msg` 后来的覆盖先前的 ⇒ 顺序反了用户只看到警告、看不到
 *    「状态已更新」。同时 `collectSearch` 的判据从「非空」改成「与首屏默认值不同」（见
 *    `usedNotOpenFilters`），否则每次 reload 都会弹那句警告。
 *
 * ⚠️ **弹窗里没有「当前状态：」这一行**（业务方 裁定第 1 条，与对标一致）：
 * 对标弹窗只有「要改成：」一行（raw HTML L52-89 的三个 label 里另两个还是 `display:none`），
 * 我们**删掉**了自己曾加的那一行。
 * ⚠️ 删行的同时**必须保留 radio 不预选**：业务方原先报的「永远显示待抢单中」真病根是旧版给
 * 第一个 radio 默认 `checked`（已修）——删行时若顺手预选第一个选项，那个现象会原样回来。
 *
 * 「不能改」现在只剩**一种**：`allowed_transitions` 为空数组 ⇒ **当前调用方没有可用目标**
 * （节点口径：`OrderSerializer.get_allowed_transitions` 按调用方持有的 RBAC 节点 code 过滤，
 * 无节点、或节点覆盖不了任何出边的类级门 / 边节点集 ⇒ 空集，fail-closed；与职位过滤无关），
 * 照旧提示「当前账号没有可执行的流转（职位权限可能不足）」。后端 `apply_transition` 再兜一次底。
 *
 * ⚠️ **`REFUNDED` 不再被特判**（业务方裁定第 3 条）：后端给它加了两条回池出边
 * （`REFUNDED → {UNASSIGNED, WAITING_GRAB}`）之后，状态机里**没有 0 出边的状态**了
 * ——此前前端那张 `TERMINAL_STATUSES = ['REFUNDED']` 表与「终态不给开弹窗」那段已整段删除。
 *
 * ⭐ **「已完成」这一档照旧列在下拉里，但当前不是「待验收」时只弹提示**：
 * 后端有意做成**读/写不对称**——`allowed_transitions` 在「不是 `COMPLETED` 且持节点 17」时
 * **仍然列出** `COMPLETED`，并用只读布尔键 `completed_requires_review` 标出「写下去会被拒」。
 * 前端选中它且该键为 `true` ⇒ 弹 `COMPLETED_REQUIRES_REVIEW_MESSAGE`（逐字取后端常量）、
 * **不发任何请求**、**不关弹层**；为 `false`（待验收）⇒ 照旧提交。⛔ 前端不自己镜像状态机补判据。
 * 判据：`__tests__/order-orderlist.spec.ts` 的 F3-U5 / U6 / U7 三条。
 *
 * **对标有、我们没有**的（再裁定删两块之后，筛选**一个都不剩** +
 * **1 颗工具栏按钮** + 行内 2 颗 + 32 个弹窗里的绝大多数）一律**照对标渲染、点了提示「尚未开放」**，
 * 不置灰、不隐藏（D-0025 §5）。
 * ⛔ **改准**：上面那句里的「**1 颗工具栏按钮**」现值为 **0 颗**
 * ——`exportBtn`（导出）接真后，这一档里**工具栏按钮一个都不剩**。
 * 逐条见 `NOT_OPEN_FILTERS`（**今天为空**）与 `NOT_OPEN_TOOLBAR`（**0** 条）。
 * ⚠️ 但其中 **11 组筛选与 16 颗工具栏按钮是「真删」**（按钮那侧 = D-0093 的 12 颗 + T7 的 3 颗排团
 * 按钮 + 删掉的「同步接单价」1 颗），是对上面那条全局口径的有意偏离
 * ——见「已知差异 18」与「已知差异 50 / 52」。
 *
 * ⚠️ 行内「1 颗」是**每行**的数：`joinTask`（加入团）只在魔兽 / 诛仙世界那支出现
 * （`isGroupGameForRowButtons`）。⭐ **最终定案：常用三颗**
 * （编辑 / 退回大厅 / 发单）；**已抛未接手时多一颗撤回箭头**（见「已知差异 67」）。
 * 短信 / 加款 / 重发 / 协商撤销都不在本页出现了。
 * 再加**按入口探针渲染**的 `pushJdpEdit`（登录 + `order.list` 才画；⭐ P4 起**每行都画**，
 * 见 `pushJdpAvailable`；对标是恒出）。
 */
import {
  assignOrderWithMessage,
  changeHallPrice,
  changePaymentStatus,
  changePrice,
  clearTaker,
  copyOrder,
  downloadOrderExport,
  fetchAssigneeOptions,
  // 改状态弹层的**批量**选项来源（spec §D10 的读取形态；Task 4b 新引入）。
  fetchBatchTransitionTargets,
  fetchKefuOptions,
  fetchOrder,
  fetchSplitMembers,
  partialRefund,
  startXgjReceiptSync,
  toLocalIso,
  transitionOrder,
  updateOrder,
  type AssigneeOption,
  type Order,
  type OrderListItem,
  type OrderSearchParams,
  type AssignOrderPayload,
  type UpdateOrderPayload,
} from '@/api/orders'
// 绩效弹层的读接口与两个写动作（改金额格 / 软删一条，D-0147 spec §4.1/§4.5/§4.6）；⛔ 不让 `table.render({url})` 自己发请求（会造出第二条取数链）。
import {
  adjustPerformanceAmount,
  deletePerformance,
  fetchPerformances,
  type PerformanceAmountKind,
  type PerformanceListItem,
} from '@/api/performances'
// 读操作上报（spec §6.2/§8-3）：触发点在 `openEditDialog()`；⛔ 别挪到详情页加载——那是「看」不是「改」，日志会撒谎。
import { reportOrderOpenEditQuietly } from '@/api/auditLogs'
// 退款失败按业务码分叉（1010 与其余码处置不同）⇒ 要类型化错误本身，不能只看 `errText`（见 `submitRefund`）。
import { BizError } from '@/api/errors'
// ⚠️ 跨页 import：编辑弹窗装的是录单页，保存成功由它 postMessage 通知本页 ⇒ 消息类型与成功文案必须只有一份定义（各抄一份字面量会在改名时静默断链）；代价是录单页并进本页 chunk（页面仍 lazy）。
import { EDIT_SAVE_SUCCESS_MESSAGE, ORDER_SAVED_MESSAGE_TYPE } from './order-addorder'
import { fetchOrderDictionaries, type OrderDictionaries } from '@/api/orderDictionaries'
// 权限节点判据：行内「指派打手」「订单截图」按节点渲染；`canNode` 用岛页共享那份（`perms.ts`，同 index-console 口径），⛔ 不另写一份（E-0021）。
import { fetchMe } from '@/api/auth'
import {
  applyPlatformWritePermission,
  canNode,
  platformWriteAllowedNow,
  type NodeViewerInput,
} from '@/layui-island/perms'
// 抛单 / 撤回两个动作的出口（两页共用一份，别在页里重写 URL）。
import { throwToHall, withdrawThrow } from '@/api/hall'
import { isBlankLikePython, validateMoneyInput } from '@/config/money'
import { ORDER_STATUS_LABELS, type OrderStatus } from '@/config/orderStatus'
import {
  bootstrapIsland,
  initIslandElements,
  islandMsg,
  type IslandContext,
  type IslandPage,
} from '@/layui/island'
import { defaultDateRange as sharedDefaultDateRange } from '@/layui-island/dateRange'
import { escapeHtml } from '@/layui-island/escape'
import { showBackendSuccessMessage } from '@/config/backendMessage'
// ⭐ B2：剪贴板两条路（clipboard API → execCommand 兜底）。两页共用一份实现。
import { copyTextToClipboard } from '@/layui-island/clipboard'
// ⭐ T11a：这单已抛到代练通 ⇒ 订单号列那颗绿色「通」徽标。
// 状态码 → 中文与徽标标记**只有一份**实现（两页共用，见 `layui-island/platformPush.ts`）。
import {
  platformDisplayStatus,
  platformPushBadgeHtml,
  platformPushStatusLabel,
  platformStatusBadgeClass,
  PUSH_JDP_SENT_MESSAGE_TYPE,
  PUSH_JDP_SENT_TEXT,
} from '@/layui-island/platformPush'
// ⭐ P3 / P6：「从所有平台撤回」与三角展开共用同一组读面与出口。
// ⚠️ 业务方最终定案：只删「加款 / 重发 / 协商撤销」（搬到「第三方平台订单管理」页）；
// 「从所有平台撤回」箭头**保留**（业务方 补充：撤回不在搬迁清单里）。
// ⛔ URL 只在 `api/dailiantongPush.ts` 拼装（本文件不手写单号段）。
import {
  fetchOrderPlatformPushes,
  withdrawOrderPlatformPush,
  type DailiantongPushRow,
  type PlatformPushSummary,
} from '@/api/dailiantongPush'
// 平台 id → 中文名：唯一出处（「指定发平台」的 13 个平台字典）。
import { PLATFORMS } from '@/config/platforms'
// 批量两步式共用流程（Phase B · Task 3；spec §5.1 / §D12）：校验（dry_run）→ 不合格清单确认 → 执行。
// ⛔ 本页只负责「把勾选与参数拼成批量端点」与「按 `kind` 分流」——业务判据全在后端。
import { runBatchFlow, type BatchFlowOptions } from '@/layui-island/orderBatch'
import { getFormSelects, initFormSelects, type FormSelectsInstance } from '@/layui/vendor'
import {
  fetchOrderSourceGroups,
  type OrderSourceGroupRow,
} from '@/api/orderSources'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

const API_BASE = import.meta.env.BASE_URL || '/'
/** 证据件（静态 HTML：页内 `<style>` 原文 + 处理过的 body 标记）。 */
export const EVIDENCE_URL = 'replica/order-orderlist.html'
const TABLE_ID = 'datatable'
const STATUS_DIALOG_ID = 'tt-orderlist-transition'

/** ⛔ 必填闸在前端（后端 reason 宽松可选）。 */
export const REASON_REQUIRED_STATUSES: ReadonlySet<string> = new Set([
  'ISSUE', // 问题单
  'PAUSED', // 已暂停
  'REFUNDED', // 已退单
])
export const REASON_REQUIRED_MESSAGE = '请输入原因'
export const REASON_LABEL_TEXT = '说明原因：'
/** ⛔ E-0061 陷阱①：类名与选择器分成两个常量。 */
export const REASON_ROW_CLASS = 'remark'
export const REASON_INPUT_ID = 'remark'
export const REASON_PLACEHOLDER = '请输入原因'
/** ⛔ 与后端 `transition_reason_field` 同一数字，别单边改。 */
export const REASON_MAX_LENGTH = 200

/**
 * ⛔ 已抛单不能手工改状态：点状态徽标只弹提示，不开弹窗、零请求（后端 A2 的闸兜底）。
 * 「已抛单」判据只看行数据 `platform_push`（终态行也算），⛔ 不看当前状态名。
 */
export const PLATFORM_STATUS_NOTICE =
  '这张单已抛到代练通，状态由平台决定并自动同步，不能手工修改'

/** ⛔ 勾选里含一张已抛单就整体拒绝并列出全部单号（不静默跳过），零请求。 */
export function batchStatusPushedMessage(orderNos: readonly string[]): string {
  return `以下订单已抛到代练通，状态由平台决定，不能批量修改：${orderNos.join('、')}`
}

// ── 「订单截图」列的两颗按钮 ────────────────────────────
// ⛔ E-0061 陷阱①：类名与选择器必须分成两个常量——写成 `class="${SELECTOR}"` 永远匹配不到，且静默。
/** 对标的类名（`OrderList20191212.js:2201`）。 */
export const SHOT_UPLOAD_CLASS = 'upImg'
/** 对标的类名（`OrderList20191212.js:2202`）。 */
export const SHOT_MANAGE_CLASS = 'mangeImg'
/** 行内点击委托的选择器（由上面两个类名拼出，⛔ 不许手抄一份字面量）。 */
const SHOT_BUTTONS = `a.${SHOT_UPLOAD_CLASS}, a.${SHOT_MANAGE_CLASS}`
/** 两个弹层的 island 页 key（文件名 = key，见 `layui-island/page.ts`）。 */
export const SHOT_UPLOAD_PAGE = 'order-screenshots-upload'
export const SHOT_MANAGE_PAGE = 'order-screenshots-manage'

// ── 权限门（D-0154 同一原则）──────────────────────────
// ⛔ 按节点渲染 fail-closed：节点不给 ⇒ 不渲染（不是置灰）；身份取不到 ⇒ 按没有。
// 元组门取后端 ORDER_PERMISSIONS 的并集：⛔ 不取单点（过度隐藏）、⛔ 不比后端更宽。
// 权限位住模块级 let + 导出 setter（行模板是导出纯函数拿不到闭包）；⛔ 业务代码除 mountOrderListPage 外别调 setter。
let canAssignTaker = false
let canViewOrderScreenshots = false
let canUploadOrderScreenshots = false
let canEditOrder = false
let canThrowToHall = false
let canWithdrawThrow = false
let canExportOrders = false
// ⛔ 与后端 `ORDER_PERMISSIONS["xgj_receipt_sync"]` 单点同宽；不许写 anyNode 并集（比后端更宽，点了必 403，D-0154）。
let canChangePaymentStatus = false

// ── 地球按钮入口探针 ──────────────
// 只问轻量只读探针 `GET /api/business/platform-integrations/push-availability/`（登录 + `order.list`）。
// ⛔ fail-closed：失败 / 超时 / 401 / 403 / 非信封 / `available !== true` 一律不画。
// ⛔ 按钮常驻：不许按「这一行抛过没有」隐藏（旧判据 `canRethrowPush` 已删），不可发由弹窗 `can_push/reason` 说明。
let pushJdpAvailable = false

/** ⛔ E-0061 陷阱①：类名与选择器分开；两处都别手抄字面量。 */
export const PUSH_JDP_CLASS = 'pushJdpEdit'
export const PUSH_JDP_SELECTOR = `a.${PUSH_JDP_CLASS}`

// ⭐ P3 / P5 / P6 / P7（业务方真机反馈）四组常量；类名与选择器分开（E-0061 ①）。

export const ORDER_NO_DEFAULT_COLOR = '#0094ff'
/** ⛔ 别用别的紫代替。 */
export const ORDER_NO_PUSHED_COLOR = '#D15FE8'

export const PUSH_JDP_CANCEL_CLASS = 'pushJdpCancel'
export const PUSH_JDP_CANCEL_TIP = '把这笔订单从所有第三方交易平台撤回'
export const PUSH_JDP_CANCEL_CONFIRM_TEXT = '确定将该订单从所有平台撤回吗？'
export const PUSH_JDP_CANCEL_OK_TEXT = '确定撤回'
export const PUSH_JDP_CANCEL_NO_TEXT = '我再想想'
/** ⛔ 不假装成功：撤回箭头与三角展开读面回空两处共用。 */
export const PUSH_JDP_NO_PLATFORM_MESSAGE = '这张单没有抛到任何平台'
export const PUSH_JDP_WITHDRAW_RESULT_TITLE = '从所有平台撤回：'
/** ⛔ 按「返回行」判真实结果，不复述后端的固定 msg。 */
export const PUSH_WITHDRAW_CLOSED_MESSAGE = '已撤回并退款'
export const PUSH_WITHDRAW_CANCEL_PENDING_MESSAGE = '已提交撤销申请，等打手/客服处理'
export const PUSH_WITHDRAW_UNVERIFIED_MESSAGE = '撤回请求已提交，请刷新核对'

export const PLATFORM_TREE_CLASS = 'tree_btn'
export const PLATFORM_TREE_SELECTOR = `i.${PLATFORM_TREE_CLASS}`
/** ⛔ 主表与右侧固定列两张表都要插。 */
export const PLATFORM_CHILD_ROW_CLASS = 'platformChildRow'
export const PLATFORM_CHILD_EDIT_CLASS = 'platformChildEdit'
export const PLATFORM_CHILD_EDIT_SELECTOR = `a.${PLATFORM_CHILD_EDIT_CLASS}`
/** ⛔ 与 P3 同口径，不假装成功。 */
export const PLATFORM_CHILD_EMPTY_MESSAGE = PUSH_JDP_NO_PLATFORM_MESSAGE
export const PLATFORM_CHILD_FAILED_PREFIX = '平台单读取失败：'

/** ⛔ 不是第二套状态机：码表唯一出处 `platformPush.ts`，真闸在后端 `dailiantong_actions.withdraw_push`；这里只做「露不露箭头」。 */
export type PushRowAction = 'withdraw' | 'none'

const PUSH_TAKEN_STATUSES: readonly number[] = [12, 13, 14, 15]
const PUSH_CANCEL_IN_FLIGHT: readonly number[] = [11, 14]

export function pushRowAction(
  push: PlatformPushSummary | null | undefined,
): PushRowAction {
  if (push === null || push === undefined) return 'none'
  if (push.state === 'closed') return 'none'
  // ⛔ `18`（已下架）是 h5 真机补进码表的一档：人工撤回必被后端拒（「本系统不代发撤回」）⇒ 与 16/17 同档不画箭头。
  if (push.remote_status === 17 || push.remote_status === 16) return 'none'
  if (push.remote_status === 18) return 'none'
  if (push.remote_cancel_status === 16) return 'none'
  // 撤销已在流程里 ⇒ 不再给动作（防重复提交）。
  if (
    typeof push.remote_cancel_status === 'number' &&
    PUSH_CANCEL_IN_FLIGHT.includes(push.remote_cancel_status)
  ) {
    return 'none'
  }
  // 已接手 ⇒ 不画箭头（协商撤销入口在第三方平台订单管理页）。
  if (
    typeof push.remote_status === 'number' &&
    PUSH_TAKEN_STATUSES.includes(push.remote_status)
  ) {
    return 'none'
  }
  // `11` / `null` / 未知码 ⇒ 撤回箭头（后端会按远端实况再判一次）。
  return 'withdraw'
}

/** ⛔ 唯一出处 `config/platforms.ts`，不在这里手抄第二份表。 */
function platformLabelOf(id: number): string {
  return PLATFORMS.find((item) => item.id === id)?.label ?? `平台 ${id}`
}

// ⭐ B3：展开区重做——平台子单插真行、克隆父行同下标 `<td>` 骨架（layui 的尺寸/省略号都在
// `.layui-table-cell` 上，继承即对齐）；读面只有 14 键，能映射的填真值、其余一律 `—`（⛔ 不编假值）。

/** 取不到的值统一显示这个（业务方口径）。 */
export const PLATFORM_CHILD_EMPTY_VALUE = '—'

/** ⛔ 所有来自接口/上游的文本都过 `escapeHtml`。 */
export function platformChildCellHtml(
  field: string,
  row: DailiantongPushRow,
  orderNo: string,
): string {
  switch (field) {
    case '_orderId0':
      return ''
    case '_orderid': {
      const serial = (row.serial_no ?? '').trim()
      const badge = platformPushBadgeHtml({
        platform: row.platform,
        serial_no: serial,
        state: row.state,
        remote_status: row.remote_status,
        remote_cancel_status: row.remote_cancel_status,
        is_active: row.state === 'sent' || row.state === 'pending',
      })
      const text = serial === '' ? PLATFORM_CHILD_EMPTY_VALUE : escapeHtml(serial)
      return (
        `<div class="orderLine sontr" style="margin-left:20px">` +
        badge +
        `<span style="color:${ORDER_NO_PUSHED_COLOR}">${text}</span>` +
        `</div>`
      )
    }
    case '_sta': {
      // ⛔ h1 真机 bug：必须把**整行**交给出口——出口要一起看 `remote_cancel_status` /
      // 后端 `display_status_label`（`Status=16` + `CancelStatus=12` 真实是「已撤销」，
      // 只传 `remote_status` 会一直显示「撤销中」）。
      const label = platformPushStatusLabel(row)
      if (label === '') return PLATFORM_CHILD_EMPTY_VALUE
      const cls = platformStatusBadgeClass(row)
      return `<span class="layui-badge ${escapeHtml(cls)}">${escapeHtml(label)}</span>`
    }
    case '_throw':
      return row.price === '' ? PLATFORM_CHILD_EMPTY_VALUE : escapeHtml(row.price)
    case '_change': {
      const target = orderNo !== '' ? orderNo : row.ex_order_no
      return (
        `<a href="javascript:void(0);" class="${PLATFORM_CHILD_EDIT_CLASS}"` +
        ` data-order="${escapeHtml(target)}"` +
        ` title="打开本系统订单详情（代练通页签里可改价 / 补时）">编辑</a>`
      )
    }
    default:
      // ⛔ 平台侧没有这一列的数据 ⇒ 如实「—」，不编 0 / 空串 / 别的号。
      return PLATFORM_CHILD_EMPTY_VALUE
  }
}

/** ⛔ 配置页的数据源；不是订单列表按钮的门。 */
export const PLATFORM_INTEGRATIONS_URL = '/api/business/platform-integrations/'

/** 「一键发单」入口可见性探针（后端 `PushAvailabilityView`）；权限=登录+`order.list`；⛔ 不读平台配置。 */
export const PUSH_AVAILABILITY_URL = '/api/business/platform-integrations/push-availability/'

/** 门的超时（毫秒）；超时按「拿不到」处理（fail-closed）。 */
export const PUSH_JDP_GATE_TIMEOUT_MS = 3000

export type PushAvailabilityProbe =
  | { state: 'available' }
  | { state: 'forbidden' }
  | { state: 'unavailable' }

/** 平台对接账号列表探针三态（配置页数据源，与订单列表按钮的门无关）。 */
export type PlatformIntegrationProbe =
  | { state: 'rows'; rows: unknown }
  | { state: 'forbidden' }
  | { state: 'unavailable' }

/** ⛔ 必须用裸 `fetch`：页测试把 `request()` 当响应队列（挂载期多发会抢走排好的响应，batch.spec 实测红）、`http.get` 是导出链出口（export.spec 按第 0 发断言）。超时按 `unavailable`；401 不触发 onUnauthorized（挂载期 `fetchMe` 那发承担）。 */
export async function fetchPlatformIntegrations(): Promise<PlatformIntegrationProbe> {
  const base = import.meta.env.VITE_API_BASE ?? ''
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), PUSH_JDP_GATE_TIMEOUT_MS)
  try {
    const res = await fetch(`${base}${PLATFORM_INTEGRATIONS_URL}`, {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      signal: controller.signal,
    })
    if (res.status === 403) return { state: 'forbidden' }
    if (!res.ok) return { state: 'unavailable' }
    const body = (await res.json()) as { code?: number; data?: unknown }
    return body?.code === 0 ? { state: 'rows', rows: body.data } : { state: 'unavailable' }
  } catch {
    return { state: 'unavailable' }
  } finally {
    clearTimeout(timer)
  }
}

/** ⛔ fail-closed：仅 `data.available === true`；不 `Boolean(data)`（对象恒真会让门失效）、不收 `1` / `'Y'`。 */
export function isPushAvailabilityAvailable(data: unknown): boolean {
  return (
    data !== null &&
    typeof data === 'object' &&
    (data as { available?: unknown }).available === true
  )
}

/** ⛔ 裸 `fetch`（同 `fetchPlatformIntegrations` 的理由）；一律 fail-closed 不画，调用方在控制台留原因。 */
export async function fetchPushAvailability(): Promise<PushAvailabilityProbe> {
  const base = import.meta.env.VITE_API_BASE ?? ''
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), PUSH_JDP_GATE_TIMEOUT_MS)
  try {
    const res = await fetch(`${base}${PUSH_AVAILABILITY_URL}`, {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      signal: controller.signal,
    })
    if (res.status === 403) return { state: 'forbidden' }
    if (!res.ok) return { state: 'unavailable' }
    const body = (await res.json()) as { code?: number; data?: unknown }
    return body?.code === 0 && isPushAvailabilityAvailable(body.data)
      ? { state: 'available' }
      : { state: 'unavailable' }
  } catch {
    return { state: 'unavailable' }
  } finally {
    clearTimeout(timer)
  }
}

/** 设「可发单」开关。⛔ 业务代码里除 `mountOrderListPage` 外不要调它（页测试需要驱动它）。 */
export function setPushJdpAvailable(value: boolean): void {
  pushJdpAvailable = value
}

/**
 * ⛔ `canRethrowPush` 已删（P4 真机 bug）：按钮常驻，不许靠隐藏按钮表达不可用（弹窗 `can_push/reason` 说明），判据在 `order-pushjdp.ts`；别加回来。
 */

/**
 * ⛔ 高度分档照对标 `throwToJdpTransction` 的 `area[1]` 桌面分支逐字：≤1080→95%、≤1440→90%、≤1600→80%、>1600→70%（1441..1600 落 80%，⛔ 别把边界写成 `>=`）。宽度恒 1280px。
 */
export function pushJdpLayerHeight(screenHeight: number = window.screen.height): string {
  if (screenHeight <= 1080) return '95%'
  if (screenHeight <= 1440) return '90%'
  if (screenHeight <= 1600) return '80%'
  return '70%'
}

/** ⛔ 后端 `ORDER_PERMISSIONS` 元组=any ⇒ 前端门取并集：单点会过度隐藏（实测红）、比后端更宽也不行（入口请求节点并集是上界）。 */
function anyNode(viewer: NodeViewerInput, codes: readonly string[]): boolean {
  return codes.some((code) => canNode(viewer, code))
}

/**
 * 按 `/api/auth/me` 设权限开关；节点权威出处 `order_views.py::ORDER_PERMISSIONS`（⛔ 别凭前端猜）。
 * ⛔ 默认全 false（fail-closed）。
 * ⛔ `canExportOrders` 是单点 `order.export`（后端没有元组）⇒ 别照其它开关写成 `anyNode`（比后端宽，D-0154）。
 */
export function setOrderListPermissions(me: NodeViewerInput | undefined): void {
  const viewer: NodeViewerInput = me ?? {}
  canAssignTaker = anyNode(viewer, [
    'order.assign_taker',
    'order.submit_payout_price',
    'order.clear_taker',
  ])
  canViewOrderScreenshots = canNode(viewer, 'order.view_screenshot')
  canUploadOrderScreenshots = canNode(viewer, 'order.upload_screenshot')
  // ⛔ 「编辑订单」门取弹层第一发 `GET /orders/<no>/` 的节点（retrieve 的 any 详情节点），不是保存那半 `add_edit`（只勾 add_edit 的角色点开必 403，D-0154）。
  // ⚠️ 残差：弹层内「保存」（any(`add_edit`,`submit_edit`)）未按节点渲染，归录单页（`order-addorder.ts`）管。
  canEditOrder = anyNode(viewer, ['order.view_detail', 'order.view_content'])
  // ⛔ 抛大厅独占节点：与后端 `ORDER_PERMISSIONS["throw_to_hall"]` 同宽（单点）；别改回 anyNode（点了必 403，D-0154）。
  canThrowToHall = canNode(viewer, 'order.send_to_grab_hall')
  canWithdrawThrow = canNode(viewer, 'order.withdraw_from_grab_hall')
  // ⛔ 单点 `order.export`（后端 `OrderExportView.get_permissions` 就这一个节点）；别写 anyNode。
  canExportOrders = canNode(viewer, 'order.export')
  // ⛔ 单点（后端 `ORDER_PERMISSIONS["xgj_receipt_sync"]` 同宽），fail-closed。
  canChangePaymentStatus = canNode(viewer, 'order.change_payment_status')
}

/** 摘掉证据件里带门的静态工具栏按钮（setRecMan 同 canAssignTaker、exportBtn 单点 canExportOrders）。⛔ 必须 `root.innerHTML = 证据件` 之后调；是真摘掉（remove）不是置灰（D-0154）。行内入口归 `buildOrderCols`，本函数不碰。 */
export function applyOrderListPermissionGates(root: HTMLElement): void {
  if (!canAssignTaker) root.querySelector('[data-tb="setRecMan"]')?.remove()
  if (!canExportOrders) root.querySelector('[data-tb="exportBtn"]')?.remove()
}

// ⭐ 工具栏「同步收款」（按录单时间范围逐笔查闲管家）：运行期注入工具栏静态组
// （`table.reload` 不重渲静态按钮，注入一次即可）。⛔ 按节点渲染 fail-closed——这颗是新增控件，`applyOrderListPermissionGates` 管不到。

/** 「同步收款」按钮的 `data-tb`（与处理器共用一个常量，⛔ 两边各抄一份字面值）。 */
export const RECEIPT_SYNC_EVENT = 'xgjReceiptSync'

/** 工具栏按钮文案（用户可见）。 */
export const RECEIPT_SYNC_LABEL = '同步收款'

/** 弹层的标题 / 成功提示（**自拟**：后端成功信封的 `msg` 是空串，照抄就什么都不显示）。 */
export const RECEIPT_SYNC_DIALOG_TITLE = '同步收款'
export const RECEIPT_SYNC_ACCEPTED_TEXT = '已开始同步收款，请稍后刷新查看'
/** 后端把范围截断在上限天数的判据（前后端各一份；后端是判据，前端只是先拦省一次往返）。 */
export const RECEIPT_SYNC_MAX_DAYS = 31

/** 注入到工具栏的按钮标记（款式与旁边那 11 颗一致）。 */
export const RECEIPT_SYNC_BUTTON_HTML =
  `<button class="layui-btn layui-btn-sm" data-tb="${RECEIPT_SYNC_EVENT}" type="button" ` +
  `lay-tips="按录单时间范围逐笔查询闲管家，交易成功的未收款单改成已结款">${RECEIPT_SYNC_LABEL}</button>`

/** 注入「同步收款」按钮；false = 没权限或找不到容器（安静降级）。⛔ 必须 `root.innerHTML = 证据件` 之后调。 */
export function injectReceiptSyncButton(root: HTMLElement): boolean {
  if (!canChangePaymentStatus) return false
  // ⛔ 用「刷新」那颗反查工具栏组，别取第一个 `.layui-btn-group`（弹层里也有同款容器，文档顺序一变就注错）。
  const container = root.querySelector<HTMLElement>('[data-tb="refresh"]')?.parentElement
  if (!container) return false
  container.insertAdjacentHTML('beforeend', RECEIPT_SYNC_BUTTON_HTML)
  return true
}

/** `YYYY-MM-DD` → UTC 的日序号（`Date.UTC` 起算，避开夏令时把「天数」算歪）。 */
function parseIsoDateMs(value: string): number | null {
  const matched = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (matched === null) return null
  const year = Number(matched[1])
  const month = Number(matched[2])
  const day = Number(matched[3])
  const ms = Date.UTC(year, month - 1, day)
  const roundTrip = new Date(ms)
  if (
    roundTrip.getUTCFullYear() !== year ||
    roundTrip.getUTCMonth() !== month - 1 ||
    roundTrip.getUTCDate() !== day
  ) {
    return null
  }
  return ms
}

/** 默认范围 = 最近 7 天（含今天），用本地日算。 */
export function defaultReceiptSyncRange(today: Date): { from: string; to: string } {
  const format = (value: Date): string =>
    `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(
      value.getDate(),
    ).padStart(2, '0')}`
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate())
  start.setDate(start.getDate() - 6)
  return { from: format(start), to: format(today) }
}

/** 范围校验（**纯函数**，导出给单测）；判据与后端 `XgjReceiptSyncSerializer.validate()` 同宽。⛔ 后端才是判据（前端可绕过），这里先拦只省一次往返。 */
export function receiptSyncRangeError(dateFrom: string, dateTo: string): string | null {
  if (dateFrom === '' || dateTo === '') return '请选择开始日期和结束日期'
  const from = parseIsoDateMs(dateFrom)
  const to = parseIsoDateMs(dateTo)
  if (from === null || to === null) return '日期格式不正确'
  if (from > to) return '开始日期不能晚于结束日期'
  const days = Math.round((to - from) / 86_400_000) + 1
  if (days > RECEIPT_SYNC_MAX_DAYS) {
    return `时间范围不能超过 ${RECEIPT_SYNC_MAX_DAYS} 天（当前 ${days} 天），请缩小时间范围`
  }
  return null
}
/** ⛔ 前端不自己拼判断依据：用详情只读布尔键 `completed_requires_review` 分支，文案逐字取后端 `COMPLETE_REQUIRES_REVIEW_MESSAGE`。 */
export const COMPLETED_REQUIRES_REVIEW_MESSAGE = '必须先把订单状态改成待验收才能改成已完成'
export const ASSIGN_DIALOG_ID = 'tt-order-assign'
/** ⛔ 哨兵用法：见到就渲染纯文本、不开指派界面；前端不自己判 `restrict_view_taker`（镜像权限判据必漂移）。 */
export const ASSIGN_NO_PERMISSION_TEXT = '无权查看'
export const ASSIGN_DIALOG_TITLE = '将订单指派给接手人/代练员'
/** 默认档「代练员」（有意不默认「全部」，避免客服/管理员混进名单）；本租户职位表没有它时静默回退「全部」档。 */
export const ASSIGN_ROLE_DEFAULT = '代练员'
/** 「全部」档 value=空串 ⇒ 这一档**不送** `role`（送空串会被后端拒成 1000）。 */
export const ASSIGN_ROLE_ALL_VALUE = ''
/** 「用户职位」组容器钩子：该组值来自接口响应、回来时整组重建，需要稳定挂点。 */
const ASSIGN_ROLE_OPTIONS_SELECTOR = 'div[data-role-options]'
/** ⛔ 只有 `0` 档不送 `business_type`：`business_type` 是人身上的单选属性，送 `0` 只剩标了「代练+陪玩」的人 ⇒ `0` 档落成「不筛」。 */
export const ASSIGN_BUSS_OPTIONS = [
  { value: '0', title: '代练+陪玩' },
  { value: '1', title: '代练' },
  { value: '2', title: '陪玩' },
] as const
/** 「搜索打手」输入框的 `maxlength`：`AccountFilterSerializer.key` 的上界（spec §9 V6）。 */
export const ASSIGN_KEY_MAXLENGTH = 64
/** ⛔ 提示里的名字取**已勾选那个人**的显示名，不是刚点的那个。 */
export const ASSIGN_ONE_TAKER_MESSAGE = (name: string): string =>
  `${name}已是本单接手人，一个订单只能有一个接手人，请先取消`
export const ASSIGN_HOURS_MIN_MESSAGE = '时间要求（小时）最小为1小时，请重输'
export const ASSIGN_PROJECT_EMPTY_MESSAGE = '项目要求不能为空，请输入'
export const ASSIGN_FEE_INVALID_PREFIX = '请输入正确的金额，默认为'
/** 名单一次拉 200 条（账号接口的 `page_size`；代练员通常远少于它）。 */
const ASSIGN_PAGE_SIZE = 200
/** 指派成功的提示（**页内自撰**：后端成功信封的 `msg` 是空串，没有对标原文可用）。 */
const ASSIGN_SUCCESS_MESSAGE = '指派成功'
/** 打手账单拉取失败的提示（**页内自撰**，同族于上面那句「名单拉取失败」）。 */
const ASSIGN_BILL_FAIL_MESSAGE = '打手账单拉取失败，请稍后重试'
export const CLEAR_TAKER_CONFIRM_TEXT = '提示：这个操作会清除该订单当前所有的接手人，是否继续？'
export const CLEAR_TAKER_DEL_EMP_TEXT = '是否需要同步删除代练接手人的绩效记录？'
/** ⚠️ 按钮文案照抄对标，语义由后端 `isDelEmp` 定义，别按文案改语义。 */
export const CLEAR_TAKER_DEL_EMP_BUTTONS = [
  '需要删除接手人绩效',
  '不需要删除接手人绩效',
] as const
/** 清除接单人成功的提示（**页内自撰**，同上）。 */
const CLEAR_TAKER_SUCCESS_MESSAGE = '已清除接单人'

/* ────────────────────── 工具栏「删除」按钮（spec §7.1 / §2.1.2） ────────────────────── */

/** ⛔ 文案与实现的矛盾是**有意照抄**（对标自己的文案缺陷）：「无法找回」本仓确有回收站、「自动删除接单客服提成」实际落在 `isDelEmp` 两档——⛔ 不许修文案、⛔ 不许据此加「无条件删客服绩效」实现。 */
export const DELETE_CONFIRM_TEXT = '真的删除吗？警告：一旦删除，将无法找回，请谨慎处理'
export const DELETE_PERFORMANCE_CONFIRM_TEXT =
  '删除订单会自动删除接单客服的接单提成记录，是否需要同步删除代练接手人的绩效记录？'
/** ⛔ 与 `CLEAR_TAKER_DEL_EMP_BUTTONS` 文案逐字相同但语义各归各的端点（E-0068），别合并成一份常量；左=Y / 右=N。 */
export const DELETE_PERFORMANCE_BUTTONS = ['需要删除接手人绩效', '不需要删除接手人绩效'] as const
/** ⛔ 成功文案必须页内常量：本仓 `request()` 已解信封，`deleteOrder` 返回体里没有 `msg`。 */
const DELETE_SUCCESS_MESSAGE = '删除成功'

/** ⛔ 对标 `<br/>` 与 `<br>` 两种写法**照抄同一串**（`layer.confirm` 支持 HTML）：不改字、不拆句、不「统一」成同一种标签。 */
export const RECOMPUTE_COMMISSION_CONFIRM_TEXT =
  '确定需要为选中的订单生成或更新客服销售绩效账单吗？<br/>提示：当确认这些订单没有客服销售绩效账单时再生成，一般情况下系统会自动生成。<br>如果系统发现账单存在，将会重新计算并更新该订单的客服销售账单'
/** 左「确定生成」⇒ 走批量端点；右「点错了」⇒ 什么都不做（layui 自己关层，零请求）。 */
export const RECOMPUTE_COMMISSION_CONFIRM_BUTTONS = ['确定生成', '点错了'] as const
/** ⛔ 逐字钉「已生成或无需更新」口径（对「行已存在、金额未动」两个动词都不准确的那档如实回报），⛔ 不许自己改字；多笔由 `batchSuccessMessage` 换成「已处理 N 笔订单」。 */
const RECOMPUTE_COMMISSION_SUCCESS_MESSAGE = '客服提成账单已生成或无需更新'

export const ASSIGN_TAKER_COLS_TITLES = [
  '当前接手人',
  '发单价(点击编辑)',
  '时间要求(小时)',
  '接手项目(点击可单独设定)',
  '操作',
] as const
export const ASSIGN_BILL_COLS_TITLES = [
  '用户名',
  '预计绩效',
  '生效绩效',
  '记录时间',
  '操作',
] as const

/* ────────────────────── T7：「查看或编辑订单接手绩效」弹层（spec §7.2 / §7.3） ────────────────────── */

/** ⛔ 判据顺序：先看「有没有接手人」（用 `status`，池内两态恒无接单人；`taker_name` 对无权角色是哨兵「无权查看」，⛔ 不用它），再看抛没抛过大厅——顺序反了会把直接指派的单弹成「改发单价」。 */
export const TAKER_PERF_BRANCH = (d: {
  hall_publisher_name: string
  status: string
}): 'edit' | 'prompt' | 'dialog' => {
  // 池内两态（UNASSIGNED / WAITING_GRAB）在库里恒无接单人（D-0032）⇒ 这一档才是「没接手人」
  const noTaker = d.status === 'UNASSIGNED' || d.status === 'WAITING_GRAB'
  if (!noTaker) return 'dialog'
  if (d.hall_publisher_name === '') return 'edit'
  return 'prompt'
}
export const PERF_DIALOG_TITLE = '查看或编辑订单接手绩效'
export const PERF_DIALOG_AREA = ['60%', '45%'] as const
export const PERF_DIALOG_ANIM = 8
/** 该弹层的 `emp_type` 过滤值（后端 `EmpType.MAIN` 的中文取值）。 */
export const PERF_EMP_TYPE = '打单工资'
/** ⛔ 第 4 列「接手项目」`width` 是空串（layui 把容器剩余宽度补给空宽列）⇒ 渲染 `<col>` 时空串**不写 style**，不是 `0`、不是算出来的 px；第 7 列对标就是数字 `100px`。 */
export const PERF_DIALOG_COLS = [
  { title: '用户名', width: '12%', align: 'left' },
  { title: '预计绩效', width: '10%', align: 'center' },
  { title: '生效绩效', width: '10%', align: 'center' },
  { title: '接手项目', width: '', align: 'left' },
  { title: '记录时间', width: '12%', align: 'center' },
  { title: '记录人', width: '12%', align: 'center' },
  { title: '操作', width: '100px', align: 'center' },
] as const

/** 绩效弹层的列标题（顺序 = `PERF_DIALOG_COLS`）。 */
export const PERF_DIALOG_COLS_TITLES = PERF_DIALOG_COLS.map((col) => col.title)

/** ⛔ 每条选择器都带 `table.tt-perf-rows` 前缀：这里是同文档页内层（没有 iframe），裸 `.layui-table-cell` 会命中同页别的表格；判据 = phase1 spec 的 F2-U3。 */
export const PERF_DIALOG_STYLE =
  'table.tt-perf-rows{table-layout:fixed}' +
  'table.tt-perf-rows td,table.tt-perf-rows th{' +
  'padding:6px 6px;border:1px solid #e2e2e2;font-size:12px}' +
  'table.tt-perf-rows .layui-table-cell{' +
  'height:40px;line-height:40px;padding:0 5px;font-size:12px;' +
  'position:relative;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;box-sizing:border-box}'

/** ⛔ 认不出 `YYYY-MM-DDTHH:mm` 形状的串**原样返回**（不硬编假时间）；输出 `MM-DD HH:mm` 与主表「录单时间」列形状不同是有意的（照的是绩效弹窗子页的形状）。 */
const PERF_RECORD_TIME_SHAPE = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/
export const formatPerfRecordTime = (value: string): string => {
  const m = PERF_RECORD_TIME_SHAPE.exec(value)
  return m === null ? value : `${m[2]}-${m[3]} ${m[4]}:${m[5]}`
}

/** ⛔ `class` / `lay-event` / 图标 `layui-icon-delete` 逐字照抄不动；动作是软删该绩效行。 */
export const PERF_DELETE_TITLE = '删除该绩效记录'
export const PERF_DELETE_CONFIRM = '确认将该笔绩效删除吗？删除后可能会影响订单当前接手人'
/** 软删成功的页内提示（**新拟**：对标成功只弹服务端 `msg`，我们拿不到）。 */
export const PERF_DELETE_SUCCESS = '已删除该绩效记录'
export const PERF_EDIT_PROMPT_TITLE = '修改预计/生效绩效'
export const PERF_AMOUNT_INVALID = (current: string): string => `请输入正确的金额，默认为${current}`
export const PERF_SAME_AMOUNT = '修改的金额与之前一样，无需修改'
/** 绩效行拉取失败的提示（**页内自撰**，同族于账单表那句「打手账单拉取失败」）。 */
const PERF_LOAD_FAIL_MESSAGE = '接手绩效拉取失败，请稍后重试'

/** 打开指派弹层的上下文（从订单行来）。`orderHallPrice` 是右表「发单价」初值——⛔ 是订单发单价 `hall_price`，不是绩效行的 `temp_amount`；后端脱敏成 null 带空串 ⇒ 该格空、保存不送 `price`。 */
export type AssignDialogContext = {
  orderNo: string
  currentTaker: string
  orderProject: string
  orderHallPrice: string
}
export const THROW_DIALOG_TITLE = '设定抛单价及保证金'
export const WITHDRAW_CONFIRM_TEXT =
  '确定从内部接单大厅撤销该订单吗？撤销后，内部接单大厅将看不到该订单'

export const REFUND_DIALOG_TITLE = '记录订单退款'

/** ⛔ 只覆盖前端能判的**形态**问题；`0` 与负数形态合法照常发出（金额列允许为负），范围（`>=0` 且 `<=订单总价`，判**新累计值**，D-0312）由后端回 `1008`。 */
export const REFUND_INVALID_AMOUNT_MESSAGE = '请输入正确的金额'

/** 退款成功的提示（**页内自撰**：后端成功信封的 `msg` 是空串，没有原文可用）。 */
export const REFUND_SUCCESS_MESSAGE = '已记录退款'

/** ⛔ 只在勾选 >1 笔时渲染（单笔一字不加）：`refund_total` 是**每一笔各设置成这个累计额**，防「以为是一共退这么多」的错账。 */
const BATCH_REFUND_NOTE_TEXT = '批量记退款时，每一笔各设置成这个累计额（可能调高也可能调低）。'
/** 运行期自建节点的定位属性：重开弹层靠它先清旧的；⛔ 不靠「第几个 blockquote」位置判据。 */
const BATCH_REFUND_NOTE_ATTR = 'data-batch-refund-note'
const BATCH_REFUND_NOTE_STYLE = 'font-size:16px;color:#ff0000'

export const ORDER_TYPE_DIALOG_TITLE = '修改订单类型'

/** 对标 `.typebox` 两个 radio 的 `value` → `OrderType` 枚举名；⛔ 别在页面里改写这两个键。 */
export const BUSS_TYPE_TO_ORDER_TYPE: Readonly<Record<string, 'BOOST' | 'COMPANION'>> = {
  '0': 'BOOST',
  '1': 'COMPANION',
}

export const ORDER_TYPE_REQUIRED_MESSAGE = '请选择业务类型'

/** 改类型成功的提示（**页内自撰**：对标回显服务端 `data.msg`，我们成功信封的 `msg` 是空串）。 */
const ORDER_TYPE_SUCCESS_MESSAGE = '订单类型已更新'

/** `maxlength` 前端比后端严（`Order.remark` 是 TextField 无限制 ⇒ 超 500 这条路打不进去）；放开属产品口径。 */
export const REMARK_PROMPT_TITLE = '修改订单重要备注'
export const REMARK_MAX_LENGTH = 500

/** `maxlength: 25` 前端比后端严（`game_role_name` 是 `CharField(max_length=128)`）。 */
export const ROLE_NAME_PROMPT_TITLE = '编辑订单角色名，若没有请写无'
export const ROLE_NAME_MAX_LENGTH = 25

/** 两句都**页内自撰**：对标弹服务端 `data.msg`，我们成功信封的 `msg` 是空串。 */
const REMARK_SUCCESS_MESSAGE = '重要备注已更新'
const ROLE_NAME_SUCCESS_MESSAGE = '角色名已更新'

// ⭐ G1：勾选合计 / 「陪·标记」徽标 / 行内编辑。
// ⚠️ 列表载荷缺 `order_type` / `tag` 键时两个徽标都不画（后端补键后自动可见），不是渲染 bug。
export const SELECTION_TOTAL_CLASS = 'selectTotal'
const SELECTION_TOTAL_SELECTOR = `.${SELECTION_TOTAL_CLASS}`
export const COMPANION_BADGE_TIP = '这是一笔陪玩订单'
export const ORDER_TAG_BADGE_COLOR = '#1E9FFF'
export const ORDER_TAG_BADGE_CLASS = 'changeTag'
const ORDER_TAG_BADGE_SELECTOR = `a.${ORDER_TAG_BADGE_CLASS}`
export const ORDER_TAG_PROMPT_TITLE = '编辑订单标记'
export const ORDER_TAG_MAX_LENGTH = 50
const ORDER_TAG_SUCCESS_MESSAGE = '订单标记已更新'
export const PROJECT_PROMPT_TITLE = '请输入新的订单代练项目'
export const PROJECT_MAX_LENGTH = 100
const PROJECT_SUCCESS_MESSAGE = '代练/陪玩项目已更新'
/** ⛔ 连对标的拼写错误一起照抄（`modityProject`），别「改对」——改了就匹配不上。 */
export const PROJECT_EDIT_CLASS = 'modityProject'
const PROJECT_EDIT_SELECTOR = `a.${PROJECT_EDIT_CLASS}`

/** 幂等键重复的业务码；⚠️ 处置与其余失败码不同（`1010` 要**刷新 + 关弹窗**，其余只弹 `msg`）⇒ 单列常量，⛔ 别内联。 */
export const REFUND_DUPLICATE_REQUEST_CODE = 1010

/** 弹窗幂等键（后端列宽 64）：安全上下文才有 `randomUUID`，否则回落「时间戳+随机」。只保证同一次弹窗内重试不产生第二笔；跨租户唯一性由后端唯一约束兜底（撞键回 `1010`）。 */
export function newRefundRequestId(): string {
  const c = globalThis.crypto as { randomUUID?: () => string } | undefined
  if (c && typeof c.randomUUID === 'function') return c.randomUUID()
  const stamp = Date.now().toString(36)
  const rand = Math.random().toString(36).slice(2, 10)
  return `refund-${stamp}-${rand}`
}

/** ⛔ 前端不需要（也不该有）一张与后端状态机漂移的「终态」表——后端给 `REFUNDED` 加回池出边后 0 出边状态已不存在；终态判据跟着后端状态机走。 */
/** 对标 `layPage.render` 的 `limits`（6 项）。 */
export const PAGE_LIMITS: readonly number[] = [10, 20, 30, 40, 50, 100]

/* ───────────────────────────── 筛选 ───────────────────────────── */

/**
 * 「尚未开放」筛选清单（`name` → 中文 label）：被填/选过 ⇒ 忽略并提示「尚未开放」，⛔ 不静默丢条件。
 * 今天为空数组：原条目已随控件真删或接真移出。
 * ⚠️ 证据件里 `layui-hide` 的 `qainput`（搜QA资料）本页从不读、从不送、从未进本表——别拿它当「漏登记」反例。
 * ⛔ 已删的「派单人 / 平台单号」两块别照对标修回来，也别因此动 `hall_publisher_user` 的映射（归 `popgameName` / `recType=10`）。
 */
export const NOT_OPEN_FILTERS: readonly { name: string; label: string }[] = [] as const

/**
 * 工具栏「尚未开放」清单（`data-tb`）：今天为空数组——11 颗都接真、各有自己的 `action ===` 分支。
 * ⛔ `onToolbarClick` 末端 `notOpen()` 出口保留（不再有按钮落到它，但出口本身不删）。
 * ⛔ 工具栏「发内部 / 撤内部」已删，别加回本表——行内 `throwEdit` / `cancelThrowHome` 仍在。
 * ⛔ 行内「加入团」（`joinTask`）是行内单笔按钮、由 `btnfun` 渲染，不随工具栏删除。
 */
export const NOT_OPEN_TOOLBAR: readonly string[] = [] as const

/** 勾选档的客户端闸：50 笔，超出只提示、零请求。 */
export const ORDER_EXPORT_CHECKED_MAX = 50

const ORDER_EXPORT_TOO_MANY_MESSAGE =
  '勾选的情况下导出订单，每次最多只支持50笔。如果需要导出全部查询结果，可以不用勾选'

/** 只判「有没有填」——`0` / `false` 都是**有效值**（口径同 EP 版 `orderFilters.ts` 的 `EMPTY_KEYS`）。 */
function filled(value: unknown): value is string {
  return typeof value === 'string' && value.trim() !== ''
}

/** 取「用户 id」类单选格的值：多选控件值是逗号串 ⇒ 取第一个；后端是 `IntegerField(min_value=1)` ⇒ 非正整数返回 `undefined`（不发参，省一次往返）。 */
function positiveInt(raw: string | undefined): number | undefined {
  if (!filled(raw)) return undefined
  const n = Number(raw.split(',')[0])
  return Number.isInteger(n) && n > 0 ? n : undefined
}

/** 「销售客服」那一格的真控件 id；⛔ 选择器与 id 分开写，别把 `.` 拼进 `id`。 */
export const KEFU_SELECT_ID = 'inputOrderKefu'

export const BATCH_ORDER_PROMPT_TITLE =
  '按单号批量查询，每行一笔单号。注意：该搜索会排除其它搜索条件'

export const BATCH_ORDER_INVALID_TEXT = '单号格式不符合要求'

/* ───────── ⭐ 价格口径线：`3 接发组合` / `4 搜亏本单` 两档实装 ───────── */

export const COMBO_PRICE_LAYER_TITLE = '接单价+发单价组合查询'

/** ⛔ 顺序与弹层 4 个金额框的 DOM 顺序一致；不要重排、不要「统一」成一句（用户要能从文案判断是哪一格错）。 */
export const COMBO_PRICE_INVALID_MESSAGES: readonly string[] = [
  '接单价最低价格格式不正确',
  '接单价最高价格格式不正确',
  '发单价最低价格格式不正确',
  '发单价最高价格格式不正确',
]

/** 弹层 4 个金额格的 `id`（证据件 HTML 上的原文），顺序同 `COMBO_PRICE_INVALID_MESSAGES`。 */
export const COMBO_PRICE_INPUT_IDS: readonly string[] = [
  'seachMutPrice_p1',
  'seachMutPrice_p2',
  'seachMutPrice_p3',
  'seachMutPrice_p4',
]

/** 弹层容器与「搜索」按钮的选择器（证据件里的类名）。 */
export const COMBO_PRICE_PANEL_SELECTOR = '.seachMutPriceDiv'
export const COMBO_PRICE_BTN_SELECTOR = '.seachMutPriceBtn'

/** ⛔ 这档已开放、只是算式不吃金额区间 ⇒ 必须是独立一句话，套「尚未开放」模板就是说假话；`4` 档压根不发区间参数。 */
export const LOSS_PRICE_RANGE_IGNORED_TEXT = '「搜亏本单」不使用金额区间，已忽略你填的区间'

/** ⛔ 档 `3` 口径在弹层那 4 格、主栏两框不参与 ⇒ 填了不生效要如实告知，⛔ 不许静默丢条件。
 *  ⛔ 判据与档 `4` 同款：「与首屏默认值不同」且当前值非空才报——不是「非空就报」（这两格自带非空默认值，那样每次 `reload()` 都弹）。 */
export const COMBO_PRICE_MAIN_RANGE_IGNORED_TEXT =
  '「接发组合」按弹层里的接单价/发单价区间筛，主栏的金额区间已忽略'

/** ⛔ 表里两条本身就是给用户看的完整文案（对应档已开放）；`notOpenMessage` 按此表分流，不套「尚未开放」模板。 */
export const FULL_SENTENCE_FILTER_HINTS: readonly string[] = [
  LOSS_PRICE_RANGE_IGNORED_TEXT,
  COMBO_PRICE_MAIN_RANGE_IGNORED_TEXT,
]

/** ⛔ 必须摘 `data-notopen`：root 级兜底命中它会把点击拦成「尚未开放」假话；时点必须在 `root.innerHTML = 证据件` 之后。 */
export function detachComboPriceNotOpen(root: HTMLElement): void {
  root
    .querySelector<HTMLElement>(COMBO_PRICE_BTN_SELECTOR)
    ?.removeAttribute('data-notopen')
}

/**
 * 返回需要提示「尚未开放，本次已忽略」的中文 label（并进同一条合并提示）：
 * ① `NOT_OPEN_FILTERS`（今天为空；本函数是其运行期唯一消费者，另被两个 spec import 做恰好集合断言）；
 * ② 「订单状态」未映射档位（多选已真送参 ⇒ 只报未映射档位、点名档位本身，⛔ 不带「（多选）」字样）；
 * ③/④ 档 `4`/`3` + 主栏金额区间被改过 ⇒ 报对应那句完整的话。
 * ⛔ 没有「价格口径 3/4 ⇒ 报尚未开放」这一支（3/4 真送参，再报就是说假话）。
 * ⛔ 判据是「与首屏默认值不同」不是「非空」（`bussType` / `kefuType` / `showPriceType` / `selectSeach1` = `'0'`、
 * `minPrice` = `'-999999'`、`maxPirce` = `'99999999'` 自带非空默认值，非空判会让每次 `reload()` 都弹警告）。
 */
export function usedNotOpenFilters(
  values: RawFilterValues,
  _checkedNames: readonly string[],
  initialValues: RawFilterValues,
): string[] {
  // ⛔ `_checkedNames` 今天不参与判据，但参数签名被 `order-orderlist.spec.ts` 既有用例按位置使用；用 `_` 前缀满足 eslint 约定，别写 eslint-disable。
  const used = NOT_OPEN_FILTERS.filter(
    (f) => filled(values[f.name]) && values[f.name] !== initialValues[f.name],
  ).map((f) => f.label)
  // ⛔ 档 `4`/`3` + 主栏区间被改过 ⇒ 那两格不参与本次查询，如实告知（不许静默丢）；
  // 判据同款：「与首屏默认值不同」且非空才报（不是「非空就报」，否则每次 reload 都弹）；
  // 只判区间不判档位本身——用户就在该档上改这两格同样是「填了不生效」。
  const mainRangeEdited =
    (filled(values.minPrice) && values.minPrice !== initialValues.minPrice) ||
    (filled(values.maxPirce) && values.maxPirce !== initialValues.maxPirce)
  const rangeIgnoredHint: Record<string, string | undefined> = {
    '3': COMBO_PRICE_MAIN_RANGE_IGNORED_TEXT,
    '4': LOSS_PRICE_RANGE_IGNORED_TEXT,
  }
  if (mainRangeEdited) {
    // `?? ''`：`noUncheckedIndexedAccess` 下 `Record` 取值是 `string | undefined`。
    const hint = rangeIgnoredHint[values.showPriceType ?? '']
    if (hint !== undefined) used.push(hint)
  }
  // 「订单状态」真接后端、多选也真送参 ⇒ 这里只报未映射档位（逐 token 反查），点名档位本身；⛔ 不带「（多选）」字样。
  if (filled(values.orderSta) && values.orderSta !== initialValues.orderSta) {
    used.push(...splitOrderStatusSelection(values.orderSta).unmapped)
  }
  return used
}

/** ⛔ 绝不发第二条 msg（`layer.msg` 后弹覆盖先弹，等于把别的反馈顶掉）；「尚未开放」类包模板、两句完整的话原样用，多句用 `；` 连成一条。分流表 = `FULL_SENTENCE_FILTER_HINTS`。 */
export function notOpenMessage(labels: readonly string[]): string {
  const fullSentences = labels.filter((label) => FULL_SENTENCE_FILTER_HINTS.includes(label))
  const stillNotOpen = labels.filter(
    (label) => !FULL_SENTENCE_FILTER_HINTS.includes(label),
  )
  const parts: string[] = []
  if (stillNotOpen.length > 0) parts.push(`「${stillNotOpen.join('、')}」筛选尚未开放，本次已忽略`)
  parts.push(...fullSentences)
  return parts.join('；')
}

/** 校验弹层 4 个金额格（档 `3`）。判据=本页唯一金额闸 `validateMoneyInput`（允许负数、两位小数），⛔ 不另写正则（两份判据必漂移）；空白=该侧不设界（放行、不发键）。返回 null=通过，否则是该弹的文案（下标同 `COMBO_PRICE_INVALID_MESSAGES`）。 */
export function comboPriceInvalidMessage(values: readonly string[]): string | null {
  for (let index = 0; index < COMBO_PRICE_INVALID_MESSAGES.length; index++) {
    const raw = values[index] ?? ''
    if (isBlankLikePython(raw)) continue
    if (!validateMoneyInput(raw).ok) return COMBO_PRICE_INVALID_MESSAGES[index] ?? ''
  }
  return null
}

/**
 * 「有没有在筛」——**空态文案靠它区分**（沿用 EP 版 `views/orders/orderFilters.ts` 的口径，
 * 那两条断言已迁移到 `pages/__tests__/order-orderlist.spec.ts`）。
 * 筛了却一条没有 = 「没有匹配的订单」；没筛就是没有 = 「还没有订单」。
 */
export function hasActiveFilters(where: Record<string, unknown>): boolean {
  return Object.keys(where).some((k) => k !== 'page' && k !== 'page_size')
}

export function emptyStateText(total: number, where: Record<string, unknown>): string {
  if (total > 0) return ''
  return hasActiveFilters(where) ? '没有匹配的订单' : '还没有订单'
}

/** 把对标的 `yyyy-MM-dd HH:mm:ss` 转成后端要的带时区 ISO 8601（口径同 EP 版 `toSearchParams`）。 */
export function toIsoDateTime(value: string, endOfDay: boolean): string | undefined {
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}):(\d{2}))?$/.exec(value.trim())
  if (!m) return undefined
  const [, y, mo, d, hh, mi, ss] = m
  if (hh === undefined) {
    const time = endOfDay ? '23:59:59' : '00:00:00'
    return toLocalIso(new Date(`${y}-${mo}-${d}T${time}`))
  }
  return toLocalIso(new Date(`${y}-${mo}-${d}T${hh}:${mi}:${ss}`))
}

/** 对标的筛选表单（`form.val('form')` 的产物）：`name → value`。 */
export type RawFilterValues = Record<string, string>

/**
 * 筛选表单 → 检索参数。「订单状态」value 是中文 label，映射在这里做（已映射拼串送 `status`、未映射由 `usedNotOpenFilters` 点名提示），调用方不必先清洗。
 * ⚠️ 没有 `server` / `tag` 映射（控件已删）；类型里保留它们是后端合法参数，类型不是控件清单的镜像。
 * ⚠️ 「收款状态」option value 是中文，映射不到就不发 `payment_status`。
 * 三条易踩：① `bussType='0'`（全部）不发 `order_type`；② `showPriceType='3'` 走弹层 4 格（⛔ 不带主栏两格）、`'4'` 只发档位（⛔ 不发区间参数）；③ `recType` 自己不产生参数，只决定 `popgameName` 送 `taker_user`（`0`）还是 `hall_publisher_user`（`10`）。
 * ⭐ 独立「发单人」下拉 `hallPublisherUser` 也送 `hall_publisher_user`（权威入口，两控件互相清空，有值优先）。
 */
export function toOrderSearch(
  values: RawFilterValues,
  checkedNames: readonly string[],
): OrderSearchParams {
  // ⛔ 别在这里放 `page` / `page_size`（踩过）：这份是交给 layui 的 `where`，layui 自己合入 `page.curr/limit`，`where` 同名键优先级更高 ⇒ 翻页永远发 `page=1`。
  const params: OrderSearchParams = {}
  // 游戏 option 的 value 是**我们的** id（mount 时按我们的 id 重建了 option）。
  if (filled(values.gameName)) params.game = Number(values.gameName)
  if (filled(values.key1)) params.keyword = values.key1.trim()
  else if (filled(values.key2)) params.project_text = values.key2.trim()
  if (filled(values.orderSta)) {
    // 多选：逐 token 反查，已映射的码 `,` 拼串送 `status`；映射不到的档位不发（留给合并提示点名）。
    const { codes } = splitOrderStatusSelection(values.orderSta)
    if (codes.length > 0) params.status = codes.join(',')
  }
  // 收款状态：option value 是中文，映射到三个码；映射不到就不发。
  // ⛔ 那张表是普通对象字面量：原型链键会被 `[]` 命中真值 ⇒ 按值类型判，只有码（字符串）才发。
  if (filled(values.setSta)) {
    const tier = PAYMENT_STATUS_BY_BENCHMARK_VALUE[values.setSta]
    if (typeof tier === 'string') params.payment_status = tier
  }
  if (checkedNames.includes('isJiajiCheck')) params.is_urgent = true

  /* ⭐ 7 条筛选映射：判据一律「空值丢弃、`0`/`false` 不丢」（E-0021）——`filled()` 只认空串/纯空白。 */

  // 订单来源（多选控件、单选用法 ⇒ 取第一个 id）。空 ⇒ 不发。
  const orderSource = positiveInt(values.orderSoures)
  if (orderSource !== undefined) params.order_source = orderSource
  // 订单分组：没选（`''` / 表外值）⇒ 不发键。
  const sourceGroup = positiveInt(values.sourceGroup)
  if (sourceGroup !== undefined) params.source_group = sourceGroup
  // 订单类型：`0` = 「全部」⇒ 不发。
  if (values.bussType === '1') params.order_type = 'BOOST'
  else if (values.bussType === '2') params.order_type = 'COMPANION'
  // 客服（单选用法 ⇒ 取第一个 id）。空 ⇒ 不发。
  const salesUser = positiveInt(values.inputOrderKefu)
  if (salesUser !== undefined) params.sales_user = salesUser
  // `kefu_type` 决定 `sales_user` 拿哪个列去比；单独送它不产生筛选 ⇒ 照原样送。
  if (values.kefuType === '0' || values.kefuType === '5' || values.kefuType === '10') {
    params.kefu_type = values.kefuType
  }
  // 未安排：checkbox（`form.val` 不回未勾选项）⇒ 勾了才发 `true`。
  if (checkedNames.includes('viewLeakOrder')) params.is_leak = true
  /* ⭐ 价格口径五档（唯一映射点）：`0`/`1`/`2` 送档位+主栏区间（原样）；`3` 送弹层 4 格
   * （⛔ 不带主栏两格）；`4` 只送档位（⛔ 一个区间参数都不发——该档不使用区间，后端对「档4+区间」响亮拒绝，
   * 前端不发为的是让正常用户走不到那条拒绝）；档位缺失只按 0/1/2 送主栏区间（`show_price_type` 由后端 default 兜住）。 */
  const tier = values.showPriceType
  if (tier === '3') {
    params.show_price_type = '3'
    if (filled(values.seachMutPrice_p1)) params.price_min = values.seachMutPrice_p1
    if (filled(values.seachMutPrice_p2)) params.price_max = values.seachMutPrice_p2
    if (filled(values.seachMutPrice_p3)) params.hall_price_min = values.seachMutPrice_p3
    if (filled(values.seachMutPrice_p4)) params.hall_price_max = values.seachMutPrice_p4
  } else if (tier === '4') {
    params.show_price_type = '4'
  } else {
    if (tier === '0' || tier === '1' || tier === '2') params.show_price_type = tier
    // 金额区间原样送；⚠️ 判据是「空」不是「真值」——`0` 与负数都是合法边界（D-0015/E-0021）。
    if (filled(values.minPrice)) params.price_min = values.minPrice
    if (filled(values.maxPirce)) params.price_max = values.maxPirce
  }
  // 批量搜单号：值来自页面 `layer.prompt` 收下的文本（换行换成逗号；对标 `orderlist.js:2736`）。
  // ⚠️ **不**再加一道 `selectSeach1 === '10'` 的闸：对标在 prompt 的 `end` 里把该 select 复位成
  // `0`（`orderlist.js:2763`）而 `batchOrderId` 仍非空 ⇒ 加那道闸会把刚输入的批量条件**静默丢掉**。
  if (filled(values.batchOrderId)) params.order_nos = values.batchOrderId.replace(/\r?\n/g, ',')
  // 打手 / 发单人：`recType` 是**读法开关**（自己不产生参数）；`popgameName` 的值按它二选一。
  // ⭐ 独立「发单人」下拉（`hallPublisherUser`）是 `hall_publisher_user` 的
  // **权威入口**——有值就只发它（旧组合控件的值在页面上已被互相清空，这里再兜一次优先级）。
  const dedicatedPublisher = positiveInt(values.hallPublisherUser)
  if (dedicatedPublisher !== undefined) {
    params.hall_publisher_user = dedicatedPublisher
  } else {
    const picked = positiveInt(values.popgameName)
    if (picked !== undefined) {
      if (values.recType === '10') params.hall_publisher_user = picked
      else params.taker_user = picked
    }
  }

  const from = filled(values.sDate) ? toIsoDateTime(values.sDate, false) : undefined
  const to = filled(values.eDate) ? toIsoDateTime(values.eDate, true) : undefined
  if (from !== undefined) params.date_from = from
  if (to !== undefined) params.date_to = to
  return params
}

/* ───────────────────────────── 列定义 ───────────────────────────── */

/** `layui.table` 的列对象形状（只列本页用到的键）。 */
export type OrderCol = {
  type?: 'checkbox'
  field?: string
  title?: string
  width?: number | string
  hide?: boolean
  align?: 'left' | 'center' | 'right'
  fixed?: 'left' | 'right'
  unresize?: boolean
  sort?: boolean
  templet?: (row: OrderListItem) => string
}

/** 状态徽标配色：15 个状态各自硬编码（`orderlist.js` 的 `orderStaRow`，**像素复刻关键值**）。 */
export const STATUS_COLORS: Record<string, string> = {
  待完善: '#E03636',
  待付款: '#E03636',
  待抢单: '#D15FE8',
  待上号: '#585858',
  代练中: '#219227',
  待验收: '#ff6a00',
  已完成: '#0094ff',
  已暂停: '#FF5722',
  问题单: '#ff0000',
  已退单: '#efaaaa',
  撤销中: '#ff5722',
  仲裁中: '#31bdec',
  已仲裁: '#c2c2c2',
  已撤销: '#c2c2c2',
  异常中: '#ffb800',
}

/** 徽标色；未知状态回落默认值 `'#b0aeae;'`（自带分号，别「修掉」）。 */
export function statusColor(label: string): string {
  return STATUS_COLORS[label] ?? '#b0aeae;'
}

/** ⛔ 「还有没有活跃平台订单」的唯一判据 = 行数据 `platform_push.is_active`；⛔ 不看状态名、不看 `state` 字符串。全结束 ⇒ 恢复普通内部单样式（历史推送行仍可展开查看，不删历史）。 */
export function hasActivePlatformPush(
  push: PlatformPushSummary | null | undefined,
): boolean {
  return push?.is_active === true
}

/** ⛔ 「已被代练通接走」判据只取 `platform_push.remote_status` ∈ 12/13/14/15（共用 `PUSH_TAKEN_STATUSES`，⛔ 不另抄一份数字），不看本地订单状态名；`state=closed` 不算被接手（那张平台单已删，拿它挡指派会让撤回的单永远换不了人）。 */
export function isTakenOnPlatform(
  push: PlatformPushSummary | null | undefined,
): boolean {
  if (push === null || push === undefined) return false
  if (push.state === 'closed') return false
  return (
    typeof push.remote_status === 'number' &&
    PUSH_TAKEN_STATUSES.includes(push.remote_status)
  )
}

/** 已被接手时点指派的**唯一**提示语（业务方原话，⛔ 别改写）；行内与工具栏两路共用、都零请求。 */
export const ASSIGN_PLATFORM_TAKEN_MESSAGE =
  '这张单已被代练通接单，不能再指派；要换人请先在代练通协商撤销'

/** ⭐ 已抛单的「状态」列显示平台综合状态（`platformDisplayStatus`），未抛单画本地 8 档。平台 token 映射到本页既有色档（复用 `STATUS_COLORS`，⛔ 不新造颜色）；未列出的先按中文名撞、再回落默认灰。 */
export const PLATFORM_STATUS_COLOR_LABEL: Readonly<Record<string, string>> = {
  temp_order: '待完善',
  unaccepted: '待抢单',
  in_progress: '代练中',
  waiting_review: '待验收',
  abnormal: '问题单',
  locked: '待上号',
  cancelling: '撤销中',
  settled: '已完成',
  revoked: '已撤销',
  force_revoked: '已撤销',
  arbitrating: '仲裁中',
  // 18 已下架 / 平台侧已关闭：本页既有的灰档（`已撤销` = `#c2c2c2`）。
  off_shelf: '已撤销',
  closed: '已撤销',
}

/** 状态徽标的文案：**已抛单 = 平台综合状态**；没有可判的平台状态 / 未抛单 ⇒ 本地状态。 */
export function orderStatusCellLabel(
  d: Pick<OrderListItem, 'status' | 'platform_push'>,
): string {
  // ⭐ 只有**活跃**平台订单才显示平台状态；全部推送结束 ⇒ 内部 8 档。
  const push = d.platform_push
  if (push === null || push === undefined || !hasActivePlatformPush(push)) {
    return statusLabel(d.status)
  }
  const platform = platformDisplayStatus(push)
  return platform === null ? statusLabel(d.status) : platform.label
}

/** 状态徽标的底色：平台状态映射到本页既有色档（未列出的 token 先按中文名撞，再回落默认值）。 */
export function orderStatusCellColor(
  d: Pick<OrderListItem, 'status' | 'platform_push'>,
): string {
  const push = d.platform_push
  if (push === null || push === undefined || !hasActivePlatformPush(push)) {
    return statusColor(statusLabel(d.status))
  }
  const platform = platformDisplayStatus(push)
  if (platform === null) return statusColor(statusLabel(d.status))
  const localLabel = PLATFORM_STATUS_COLOR_LABEL[platform.token] ?? platform.label
  return statusColor(localLabel)
}

export const SPLIT_CHILD_ROW_CLASS = 'splitChildRow'

/** 订单状态中文：唯一来源 `@/config/orderStatus`（后端 choices 镜像）。⛔ 别再加页面级 label 覆盖（如 `WAITING_GRAB: '待上号'` 那种已被裁定删除——同一状态任何页面必须同一文案；要区分的是色值不是文案）。 */
export function statusLabel(status: string): string {
  return ORDER_STATUS_LABELS[status as OrderStatus] ?? status
}

/** 「订单状态」筛选的单个 option value（中文 label）→ 后端枚举名；反查 `ORDER_STATUS_LABELS`，⛔ 不另建第二张映射表。多选拼串不是单个 label ⇒ 回 `undefined`（拆分在 `splitOrderStatusSelection`）。 */
function statusOfFilterValue(value: string): OrderStatus | undefined {
  const hit = (Object.entries(ORDER_STATUS_LABELS) as [OrderStatus, string][]).find(
    ([, label]) => label === value,
  )
  return hit?.[0]
}

/** 「订单状态」多选拼串 → `{ 已映射枚举码, 未映射档位 }`：已映射由 `toOrderSearch` 真送参、未映射留给合并提示点名。⚠️ token 不清洗（不 trim、不去空项）——怪串反查不到 ⇒ 进「未映射」路，原样露出。 */
function splitOrderStatusSelection(value: string): {
  codes: OrderStatus[]
  unmapped: string[]
} {
  const codes: OrderStatus[] = []
  const unmapped: string[] = []
  for (const token of value.split(',')) {
    const mapped = statusOfFilterValue(token)
    if (mapped === undefined) unmapped.push(token)
    else codes.push(mapped)
  }
  return { codes, unmapped }
}

const money = (value: string | null): string => (value === null || value === '' ? '0.00' : value)

/** 勾选行「接单价/发单价合计」文案；两段都为 0 ⇒ 空串。`hall_price` 被脱敏成 null 按 0 计（如实反映看到的数，⛔ 不从别处推算）。用「分」累加再除 100（浮点直加出尾数）；金额允许为负（D-0015），⛔ 别用 `Math.abs` / 真值判断。 */
export function formatSelectionTotal(
  rows: readonly Pick<OrderListItem, 'total_price' | 'hall_price'>[],
): string {
  const toCents = (value: string | null | undefined): number => {
    // 空 / 非数字一律按 0：载荷契约是两位小数字符串，这里是防御（⛔ 不让一个坏格把整句变成 NaN）。
    if (value === null || value === undefined || value === '') return 0
    const n = Number(value)
    return Number.isFinite(n) ? Math.round(n * 100) : 0
  }
  let recCents = 0
  let hallCents = 0
  for (const row of rows) {
    recCents += toCents(row.total_price)
    hallCents += toCents(row.hall_price)
  }
  if (recCents === 0 && hallCents === 0) return ''
  return `(勾选统计接单价：${String(recCents / 100)}，发单价：${String(hallCents / 100)})`
}

/* ─────────────── 「发单价」列的「结」标记（钱包发薪线） ─────────────── */

/** 「结」标记：该单**打手**绩效已结账（判据 = 载荷 `taker_settled`）。形状复用页内 `.lableb`（⛔ 不另写内联尺寸）；同时拼 `title`（本页 `lay-tips` 不渲染）；`#5FB878` 只归标记，金额色不受影响。 */
export const TAKER_SETTLED_MARK = {
  color: '#5FB878',
  text: '结',
  tips: '该订单已与打手结算了（详细的结算明细请到财务运营/绩效账单里查询）',
}

/* ─────────────── 「接单价」列的收款状态三档标记（spec §7 第 1 条 / 决策 C3） ─────────────── */

/** 三档码 → 标记。⚠️ `UNSETTLED` 那句提示是「结账」、另两档是「结算」，逐字不要「统一」掉。
 *  ⛔ 取标记一律走 `paymentStatusMark(code)`，别直接 `PAYMENT_STATUS_MARKS[code]`——普通对象字面量的原型链键会被 `[]` 命中真值、`??` 兜不住（实测反例）。
 *  标记同时拼 `title`（本页 `lay-tips` 不渲染）。 */
export const PAYMENT_STATUS_MARKS: Record<string, { color: string; text: string; tips: string }> = {
  UNSETTLED: {
    color: '#E6E6FA',
    text: '未',
    tips: '该订单在实际中还未收到款（注意：与代练员或下家结账请到财务运营-绩效管理中操作，此处是用来与发单方/上家进行结算）',
  },
  SETTLING: {
    color: '#B0E0E6',
    text: '中',
    tips: '该订单在实际中处于正在结算中（注意：与代练员或下家结算请到财务运营-绩效管理中操作，此处是用来与发单方/上家进行结算）',
  },
  SETTLED: {
    color: '#98FB98',
    text: '收',
    tips: '该订单实际中已收款（注意：与代练员或下家结算请到财务运营-绩效管理中操作，此处是用来与发单方/上家进行结算）',
  },
}

/** 未知码落「未」那一档。 */
export const PAYMENT_STATUS_DEFAULT_MARK = PAYMENT_STATUS_MARKS.UNSETTLED!

/** ⛔ 不能写成 `PAYMENT_STATUS_MARKS[code] ?? 默认`：普通对象字面量的原型链键（`__proto__`/`constructor`/`toString`）会经 `[]` 命中 `Object.prototype` 真值成员、`??` 不兜底 ⇒ 渲出 "undefined" 标记；必须用自有属性（`hasOwnProperty`）检查。 */
export function paymentStatusMark(code: string): { color: string; text: string; tips: string } {
  return Object.prototype.hasOwnProperty.call(PAYMENT_STATUS_MARKS, code)
    ? PAYMENT_STATUS_MARKS[code]!
    : PAYMENT_STATUS_DEFAULT_MARK
}

/** 证据件中文字面量 → 码（本页唯一映射表）。⚠️ 与 `PaymentStatus` 的 label 不是同一套中文，别「统一」；⛔ 反查别另写镜像表（改状态弹窗预勾选按这张表逐项比，多一张会漂移）。 */
export const PAYMENT_STATUS_BY_BENCHMARK_VALUE: Record<string, string> = {
  未结算: 'UNSETTLED',
  结算中: 'SETTLING',
  已结算: 'SETTLED',
}

/* ─────────────────── 工具栏统计值（spec §7 / 决策 C17） ─────────────────── */

/** `GET /api/business/orders/` 的 `data.summary` 形状。⚠️ 金额是两位小数字符串（出口形状，前端不再格式化）——`'0.00'` 是合法金额，⛔ 别真值判空；金额允许为负。 */
export type OrderListSummary = {
  count: number
  total_hall_price: string
  total_price: string
  total_refund_fee: string
  diff: string
  refunded_diff: string
  status_counts?: Partial<Record<OrderStatus, number>>
}

/** 统计句状态段的显示顺序（照证据件那句：已暂停在问题单前）；⛔ 与 `ORDER_STATUS_VALUES` 声明顺序不同，别改用它。只排顺序，中文仍取 `ORDER_STATUS_LABELS`（本页不抄第二份中文表）。 */
const SUMMARY_STATUS_ORDER: readonly OrderStatus[] = [
  'UNASSIGNED',
  'WAITING_GRAB',
  'IN_PROGRESS',
  'PENDING_REVIEW',
  'COMPLETED',
  'PAUSED',
  'ISSUE',
  'REFUNDED',
]

/** ⛔ E-0061 陷阱①：class 名与选择器分开（选择器由类名派生，别并列写两份字面值）。 */
const SUMMARY_STATUS_TAB_CLASS = 'statustab'
const SUMMARY_STATUS_TAB_SELECTOR = `.${SUMMARY_STATUS_TAB_CLASS}`

const ORDER_STA_SELECT_ID = 'orderSta'

/** 工具栏那颗搜索按钮（`onToolbarClick` 的 `action === 'seachBtnOk'` ⇒ `reload()`）。 */
const SEARCH_BUTTON_SELECTOR = '[data-tb="seachBtnOk"]'

/** 状态段悬停文案（状态名用我们的叫法，`statusLabel()` 现算）。 */
function summaryStatusTip(status: OrderStatus): string {
  return `点击可以快速筛选${statusLabel(status)}的订单`
}

/**
 * 工具栏统计句；模板取证据件 `#toolbarDemo` 的 `.total`，只把笔数/金额/状态计数换成实值。
 * ⛔ 数字一律来自后端聚合，不许拿当前页那几行自己算。
 * ⛔ 状态段可点（业务方裁定）：`class="statustab"` + `data-status=<码>` + `lay-tips`/`title`（中文由 `statusLabel()` 现算；⛔ 不硬编码第二张中文表、不改 option 清单）。
 * ⚠️ 写值链隐含前提：`dd[lay-value="<中文>"]` 必须能在证据件 option 里命中；命中不了 formSelects 静默不写 ⇒ 筛选被悄悄清掉、返回全量。判据 = phase1 spec「8 个中文 label 都在证据件 option 里」。
 */
export function renderSummaryText(summary: OrderListSummary): string {
  const counts = summary.status_counts ?? {}
  const statusText = SUMMARY_STATUS_ORDER.map((status) => {
    const tip = escapeHtml(summaryStatusTip(status))
    return (
      `<span style="cursor:pointer" class="${SUMMARY_STATUS_TAB_CLASS}" ` +
      `data-status="${status}" lay-tips="${tip}" title="${tip}">` +
      `${statusLabel(status)}${escapeHtml(String(counts[status] ?? 0))}笔</span>`
    )
  }).join('，')
  // ⛔ 动态值（笔数/金额/悬停文案）一律过 escapeHtml；金额先 String()——后端万一回数字会在 escapeHtml 的 replace 里抛 TypeError，别只给笔数加。
  return (
    `本次搜索统计共${escapeHtml(String(summary.count))}笔，` +
    `预计总发单价${escapeHtml(String(summary.total_hall_price))}元(不含接单提成)，` +
    `总接单价${escapeHtml(String(summary.total_price))}元，` +
    `总退款${escapeHtml(String(summary.total_refund_fee))}元，` +
    `预计利润${escapeHtml(String(summary.diff))}元(含已退单部分${escapeHtml(String(summary.refunded_diff))}元、含已退款部分${escapeHtml(String(summary.total_refund_fee))}元)。其中：` +
    statusText
  )
}

/** 统计句 span 真身：`#toolbarDemo` 是 `<script type="text/html">` 模板（文本、非子元素 ⇒ `#toolbarDemo .total` 恒空），真身在 layui 渲染出的 `.layui-table-tool-temp` 里；兜底 `.total`。 */
function summaryTotalSpan(root: HTMLElement): HTMLElement | null {
  return (
    root.querySelector<HTMLElement>('.layui-table-tool-temp .total') ??
    root.querySelector<HTMLElement>('.total')
  )
}

/** 把统计句写进 `.total` span。`summary === null`（无统计权限）⇒ 隐藏、拿到真对象 ⇒ 恢复显示（同一会话换账号/筛选可能又可见）。⚠️ 隐藏只改 display、不清 innerHTML ⇒ 旧 `.statustab` 仍在 DOM，所以「隐藏后不响应点击」必须由 `onSummaryStatusClick` 的 display 判断兜住，⛔ 不能只靠 CSS。 */
function renderSummary(root: HTMLElement, summary: OrderListSummary | null): void {
  const el = summaryTotalSpan(root)
  if (!el) return
  if (summary === null) {
    el.style.display = 'none'
    return
  }
  // 动态值已在 renderSummaryText 里过 escapeHtml，这一层不再有未转义插值。
  el.innerHTML = renderSummaryText(summary)
  // 复位成空串（不是 `'block'`）：证据件那个 span 原本没有 display 声明，别替它选一个。
  el.style.display = ''
}

/**
 * **23 列**（对标 28 列删 4 去重 1 后的现状，其余保持原有相对位置，⛔ 不许重排）。
 * `getGameName` / `getServerName`：后端回的是 FK **主键** ⇒ 用字典翻成名字。
 * `opts.isGroupGame` 传取值函数而非布尔：列模板每次重载都重跑，必须读「当前」选中的游戏。
 */
export function buildOrderCols(
  getGameName: (id: number) => string,
  getServerName: (id: number | null) => string,
  opts?: { isGroupGame?: () => boolean; splitChild?: boolean },
): OrderCol[] {
  /**
   * 第 2 列「订单号」：来源单号非空 ⇒ 显示来源单号，否则 `order_no`；第 3 列恒显示 `order_no`（⛔ 别把两列合并）。
   * ⛔ 链接 `data-order` 与容器 `id` 仍是 `order_no`（对外标识）：来源单号在租户内不唯一，绝不能改成展示值。
   * ⛔ 展示值过 `escapeHtml`（来源单号是录入的自由文本）；`data-viewId` = 展示单号（开层 title 前缀取它）。
   */
  /**
   * ⛔ 有活跃平台订单 ⇒「角色名 / 代练内容 / 角色信息」三列文字转紫（判据同 `hasActivePlatformPush`）；
   * 非活跃那一支是「不加属性」（空串），⛔ 不是换蓝色。单号 / 系统单号两列不走本函数（各自拼三元）。
   */
  const pushTextStyle = (d: OrderListItem): string =>
    hasActivePlatformPush(d.platform_push) ? ` style="color:${ORDER_NO_PUSHED_COLOR}"` : ''

  const orderIdRow = (d: OrderListItem): string => {
    // 「陪」徽标：判据 = `order_type === 'COMPANION'`（载荷缺键 ⇒ 不画，后端补上自动可见）。
    const companionBadge =
      d.order_type === 'COMPANION'
        ? `<span class="lableb layui-bg-orange" lay-tips="${COMPANION_BADGE_TIP}">陪</span>`
        : ''
    const badges = d.is_urgent
      ? '<span class="lableb" style="background-color:#FF5722">急</span>'
      : ''
    // 「标记」徽标：`tag` 是自由文本 ⇒ `data-tag` / `lay-tips` / 正文三处都过 escapeHtml。
    const tag = d.tag ?? ''
    const safeTag = escapeHtml(tag)
    const tagBadge =
      tag === ''
        ? ''
        : `<a href="javascript:void(0);" class="lableb ${ORDER_TAG_BADGE_CLASS}" ` +
          `style="background-color:${ORDER_TAG_BADGE_COLOR}" data-order="${d.order_no}" ` +
          `data-tag="${safeTag}" lay-tips="标记：${safeTag}，点击可以编辑">` +
          `${escapeHtml(tag.substring(0, 2))}</a>`
    // 「通」徽标：`platform_push` 非空才画（历史推送行也算）；列定义一字不动，只在既有 templet 里加。
    const pushBadge = platformPushBadgeHtml(d.platform_push)
    const shown = d.source_order_no !== '' ? d.source_order_no : d.order_no
    // ⛔ 紫色只在**还有活跃平台订单**时才有（判据 `platform_push.is_active`，不看状态名）；全部结束 ⇒ 默认蓝。
    const activePush = hasActivePlatformPush(d.platform_push)
    const orderNoColor = activePush ? ORDER_NO_PUSHED_COLOR : ORDER_NO_DEFAULT_COLOR
    // 展开三角：无推送不画（不占位）；三角保留（可查历史推送行），颜色跟「有没有活跃平台订单」走。
    const tree = d.platform_push
      ? `<i class="${PLATFORM_TREE_CLASS} layui-icon layui-icon-triangle-r"` +
        ` data-order="${d.order_no}" data-open="N" style="color:${orderNoColor}"` +
        ` title="点击可查看该订单下所有平台的子订单"></i>`
      : ''
    const lineClass = opts?.splitChild === true ? 'orderLine sontr' : 'orderLine'
    const splitSlot = '<span class="split-toggle-slot" style="display:inline-flex;align-items:center;justify-content:center;flex:0 0 14px;width:14px;height:18px"></span>'
    const connector = opts?.splitChild === true
      ? '<span class="split-child-connector" aria-hidden="true"></span>'
      : ''
    return (
      `<div class="${lineClass}" id="order_${d.order_no}"` +
      ((d.split_members ?? 0) > 1
        ? ` data-split-id="${escapeHtml(d.split_id ?? '')}" data-split-kind="${escapeHtml(d.split_kind ?? '')}"`
        : '') +
      '>' +
      `${splitSlot}${connector}${tree}${companionBadge}${tagBadge}${pushBadge}<a href="javascript:void(0);" class="orderdetails" data-order="${d.order_no}"` +
      ` data-viewId="${escapeHtml(shown)}" style="color:${orderNoColor}">${escapeHtml(shown)}</a>` +
      `${badges}` +
      `</div>`
    )
  }
  /**
   * 「角色名」列。显示值：空 ⇒「无」，`data-rolename` 与 tips 里拼的仍是原值（prompt 预填读它）。
   * ⛔ `game_role_name` 是录入的自由文本 ⇒ 属性 / tips / 正文三处都过 `escapeHtml`（实体由 dataset 解码回原值，点击处理读到的仍是原文）。
   */
  const roleNameRow = (d: OrderListItem): string => {
    const role = d.game_role_name
    const safeRole = escapeHtml(role)
    const shown = role === '' ? '无' : safeRole
    return (
      `<a href="javascript:void(0)" class="changeRoleName" data-order="${d.order_no}" ` +
      `data-rolename="${safeRole}"${pushTextStyle(d)} ` +
      `lay-tips="角色名：${safeRole}，点击可以快速编辑">${shown}</a>`
    )
  }
  /**
   * 第 3 列「系统内置单号」：值**恒取 `order_no`**（后端生成、恒有值）。
   * ⛔ 别改成与第 2 列同源——第 2 列在来源单号非空时改显来源单号，本列永远显示 `order_no`。
   */
  const sysOrderIdRow = (d: OrderListItem): string => {
    const color = hasActivePlatformPush(d.platform_push)
      ? ORDER_NO_PUSHED_COLOR
      : ORDER_NO_DEFAULT_COLOR
    return (
      `<span class="orderIDcopy" data-clipboard-text="${d.order_no}" ` +
      `style="cursor:pointer;color:${color}" ` +
      `lay-tips="这是系统内置单号，每笔单号不一样，单击可复制">${d.order_no}</span>`
    )
  }
  /** 「角色信息」列 = `[游戏名]区服名`；两段是租户自维护字典值、字面可带 `<`/`"` ⇒ 都过 `escapeHtml`。有活跃平台订单时整格转紫（无推送不加 span）。 */
  const infoRow = (d: OrderListItem): string => {
    const text = `[${escapeHtml(getGameName(d.game))}]${escapeHtml(getServerName(d.server))}`
    return hasActivePlatformPush(d.platform_push)
      ? `<span style="color:${ORDER_NO_PUSHED_COLOR}">${text}</span>`
      : text
  }
  const staRow = (d: OrderListItem): string => {
    const label = orderStatusCellLabel(d)
    const color = orderStatusCellColor(d)
    // ⛔ 改状态入口只看「有没有活跃平台订单」（`is_active === true`）：全部推送结束 ⇒ 恢复普通单 ⇒ 允许手工改状态。无活跃那一档不加属性。
    const pushed = hasActivePlatformPush(d.platform_push) ? ' data-push="Y"' : ''
    // title 写清「已抛单显示平台状态」——那一档改状态入口本来就不给用，旧句「点击可以更改订单状态」是假话。
    const title = d.platform_push
      ? `${d.order_no}：已抛平台的单显示平台状态（状态由代练通同步，不能手工修改）`
      : `${d.order_no}点击可以更改订单状态`
    return `<a href="javascript:void(0)" class="lableb setdlsta" data-order="${d.order_no}"${pushed} style="background-color:${color};background:${color}" title="${title}">${label}</a>`
  }
  const createRow = (d: OrderListItem): string =>
    `<span>${d.created_at.replace('T', ' ').slice(0, 16)}</span>`
  /** 「完单时间」列（仍 `hide:true`）：可空 ⇒ 空串；非空按本页既有形状格式化（ISO 直取，不引日期库）。 */
  const completeTimeRow = (d: OrderListItem): string =>
    d.completed_at === null ? '' : `<span>${d.completed_at.replace('T', ' ').slice(0, 16)}</span>`
  /**
   * 「时间」列：用我们的 `time_requirement`（自由文本，非空列）⇒ 是对标的刻意偏离，颜色取对标那句绿。
   * ⛔ 空值 ⇒ 空串（不渲染空壳 `<span>`）；正文过 `escapeHtml`。
   */
  const hourRow = (d: OrderListItem): string => {
    const text = d.time_requirement
    return text === '' ? '' : `<span style="color:#5BB75B">${escapeHtml(text)}</span>`
  }
  /** 「旺旺/来源」列：纯文本；`customer_ww` 是录入自由文本 ⇒ 过 `escapeHtml`。 */
  const wangwangRow = (d: OrderListItem): string => `<span>${escapeHtml(d.customer_ww)}</span>`
  /**
   * 「接手人」列三态：`''` ⇒ 「指派打手」按钮；用户名 ⇒ 可点名字（点击=改派）；`无权查看` ⇒ 纯文本（⛔ 不带 jsr——无权进指派界面，点了没反应是对的）。
   * ⭐ `taker_is_platform === true`（代练通占位接手人）⇒ 名字紫色（复用 `ORDER_NO_PUSHED_COLOR`，⛔ 不另写色值）；缺键/false ⇒ 默认色、不炸。
   * ⚠️ 空态补 `data-order` / `data-taker` 不可见属性：点击处理要知道哪张单、当前有没有接单人。
   */
  const takerRow = (d: OrderListItem): string => {
    /**
     * ⛔ 安全级：`taker_name` 是昵称优先的自由文本 ⇒ 不过 `escapeHtml` 就是存储型 XSS；属性（`data-taker`）与正文两处都必须转义。
     * ⚠️ 哨兵比较用未转义的 `d.taker_name`（判据是原值，`name` 只是渲染产物）。
     */
    const name = escapeHtml(d.taker_name)
    const project = escapeHtml(d.project_text)
    // `data-hall-price` 取**原值**（不取 money() 展示串）：改价发的是原值；null（脱敏）落空串 ⇒ 弹层该格空、保存不送 price。
    const hallPrice = d.hall_price ?? ''
    /**
     * ⛔ 代练通占位接手人的判据是后端布尔键 `taker_is_platform`，⛔ 不是前端拿 `taker_name === '代练通'` 比串（显示名会随业务口径改）；
     * 色值复用 `ORDER_NO_PUSHED_COLOR`；`=== true` 刻意——缺键 ⇒ undefined ⇒ 默认色、不炸。
     */
    const platformStyle =
      d.taker_is_platform === true ? ` style="color:${ORDER_NO_PUSHED_COLOR}"` : ''
    if (d.taker_name === ASSIGN_NO_PERMISSION_TEXT) return `<span>${name}</span>`
    // ⛔ 权限门（fail-closed）：没有 `assignee_options` 那组任一节点 ⇒ 不渲染入口（空态返空串；有名字退化成纯文本）。
    if (!canAssignTaker)
      return d.taker_name === '' ? '' : `<span${platformStyle}>${name}</span>`
    // 已被代练通接手 ⇒ 入口仍画（客服点得到才知道为什么），点击只提示一句、零请求；判据经不可见属性带到点击侧（⛔ 不是新状态机）。
    const takenAttr = isTakenOnPlatform(d.platform_push) ? ' data-push-taken="1"' : ''
    if (d.taker_name === '') {
      return (
        `<a class="layui-btn layui-btn-xs layui-btn-normal jsr" data-order="${d.order_no}" ` +
        `data-taker="" data-project="${project}" data-hall-price="${hallPrice}"${takenAttr} data-label="指派打手">指派打手</a>`
      )
    }
    return (
      `<a href="javascript:void(0)" class="jsr" data-order="${d.order_no}" ` +
      `data-taker="${name}" data-project="${project}" data-hall-price="${hallPrice}"` +
      `${takenAttr}${platformStyle}>${name}</a>`
    )
  }
  /** 「接单价」列 = 收款状态标记＋金额链接。⛔ 点击读 `data-fee`（原值）而不是文本（文本是 money() 格式化过的）；`payment_method` 进 tips/title 属性 ⇒ 过 `escapeHtml`。 */
  const priceRow = (d: OrderListItem): string => {
    const mark = paymentStatusMark(d.payment_status)
    const amount = money(d.total_price)
    const tip =
      `该笔订单的接单金额：${amount}元` +
      (d.payment_method === '' ? '' : `，支付方式：${escapeHtml(d.payment_method)}`)
    return (
      `<a href="javascript:void(0)" class="lableb jsSta" data-order="${d.order_no}" ` +
      `data-sta="${d.payment_status}" ` +
      `style="background-color:${mark.color}" lay-tips="${mark.tips}" title="${mark.tips}">${mark.text}</a>` +
      `<a href="javascript:void(0)" class="editOrderPrice" data-order="${d.order_no}" ` +
      `data-fee="${d.total_price}" style="color:#E03636;" lay-tips="${tip}" title="${tip}">${amount}</a>`
    )
  }
  /**
   * 「发单价」列：有活跃平台订单 ⇒ 显示推送行 `price`（键缺席/null 脱敏 ⇒ 回退本地 `hall_price`）。
   * ⛔ 显示用推送价、改价仍写本地价（`data-fee` = `hall_price`，别把平台价灌进去）。
   * ⛔ 三支判据取自 `TAKER_PERF_BRANCH(d)`：「有没有接手人」由订单状态判（池内两态恒无接单人），`taker_name` 对无权角色是哨兵 ⇒ 判据不读它。
   * ⚠️ ①支 `lay-tips` 照抄且**不另加 `title`**：那句断言「还未指派过接手人」，对 /assign 过的单会是假话。
   * ⚠️ `data-fee` 取 `d.hall_price ?? ''`（不是格式化后的 amount：请求体要发原值）；`data-branch` 是判据结果的不可见属性（常量串，不需转义）。
   */
  const throwPriceRow = (d: OrderListItem): string => {
    // ⛔ 只有**活跃**平台订单才显示平台价；全部结束 ⇒ 回本地 `hall_price`（历史推送价在三角展开区看）。
    const pushPrice = hasActivePlatformPush(d.platform_push)
      ? d.platform_push?.price
      : undefined
    const shownPrice =
      typeof pushPrice === 'string' && pushPrice !== '' ? pushPrice : d.hall_price
    const amount = money(shownPrice)
    const branch = TAKER_PERF_BRANCH(d)
    const order = `data-order="${d.order_no}" data-fee="${d.hall_price ?? ''}" data-branch="${branch}"`
    const settledMark = d.taker_settled
      ? `<a href="javascript:void(0)" class="lableb" style="background-color:${TAKER_SETTLED_MARK.color}" ` +
        `lay-tips="${TAKER_SETTLED_MARK.tips}" title="${TAKER_SETTLED_MARK.tips}">${TAKER_SETTLED_MARK.text}</a>`
      : ''
    if (branch === 'edit') {
      return (
        settledMark +
        `<a href="javascript:void(0)" class="changeThrowFeeEdit" ${order} ` +
        `style="color:#ff6a00" lay-tips="该笔订单还未指派过接手人，可以直接修改抛单价作为记账用，点击修改">${amount}</a>`
      )
    }
    const tip = `预计该笔订单总抛单金额：${amount}元，点击可以修改抛单价`
    return (
      settledMark +
      `<a href="javascript:void(0)" class="changeThrowFee" ${order} ` +
      `style="color:#ff6a00" lay-tips="${tip}" title="${tip}">${amount}</a>`
    )
  }
  /** 「实收款」列：直接渲染载荷 `net_received`，⛔ 前端不算这个数（算式在后端 `settlement_amount.net_received()`）；不可点（派生值，渲染 `<span>`）。 */
  const netReceiptsRow = (d: OrderListItem): string => `<span>${d.net_received}</span>`
  /**
   * 「退款」列：金额链接。无退款那支 tips 逐字照抄；有退款那支只拼「退款金额：X元，点击可以快速编辑退款」——
   * ⛔ 退款方式/时间/操作人三段我们载荷里没有，**整段不拼**，不照抄成空段。
   * ⛔ 不要在前端把退款金额累加到本地行（会显示「只算本次会话」的假数）；列上读的就是后端真值。
   */
  const refundRow = (d: OrderListItem): string => {
    const fee = d.refund_fee
    const refunded = Number(fee) > 0
    const title = refunded
      ? `退款金额：${fee}元，点击可以快速编辑退款`
      : '没有退款，点击可以快速记录退款'
    return `<a href="javascript:void(0)" class="refundFee" data-order="${d.order_no}" data-fee="${fee}" style="color:${refunded ? 'red' : '#999'}" lay-tips="${title}">${fee}</a>`
  }
  /** 「订单截图」列：两颗按钮按节点渲染（fail-closed，D-0154）；两颗都没有 ⇒ 整格空串（⛔ 不留空 `.layui-btn-group` 壳）。 */
  const imgRow = (d: OrderListItem): string => {
    const no = escapeHtml(d.order_no)
    const buttons =
      (canUploadOrderScreenshots
        ? `<a href="javascript:void(0)" class="layui-btn layui-btn-xs layui-btn-normal ${SHOT_UPLOAD_CLASS}" data-order="${no}" data-label="上传截图" lay-tips="上传截图"><i class="layui-icon">&#xe681;</i></a>`
        : '') +
      (canViewOrderScreenshots
        ? `<a href="javascript:void(0)" class="layui-btn layui-btn-xs layui-btn-normal ${SHOT_MANAGE_CLASS}" data-order="${no}" data-label="图片管理" lay-tips="图片管理"><i class="layui-icon">&#xe60d;</i></a>`
        : '')
    return buttons === '' ? '' : `<div class="layui-btn-group">${buttons}</div>`
  }
  /** 「销售客服」列：纯文本；`sales_agent` 是名字快照（录入可自由填）⇒ tips 与正文都过 `escapeHtml`。空值不造「无」。 */
  const kefuRow = (d: OrderListItem): string => {
    const name = escapeHtml(d.sales_agent)
    return `<span lay-tips="${name}">${name}</span>`
  }
  /** 「发单人」列：空 ⇒「无」；名字来自 `request.user.username`，仍与其余自由文本同形过 `escapeHtml`（纵深防御）。 */
  const assUserNameRow = (d: OrderListItem): string => {
    const shown = d.hall_publisher_name === '' ? '无' : d.hall_publisher_name
    const name = escapeHtml(shown)
    return `<span lay-tips="${name}">${name}</span>`
  }
  /**
   * 「重要备注」列：空值并入「没有备注」档（tips 换成「点击可以编辑订单重要备注」，否则会拼出 `lay-tips=""`）。
   * ⛔ `remark` 是录入自由文本 ⇒ `data-remark` / tips / 正文三处都过 `escapeHtml`（dataset 解码回原文，点击处理读到的仍是原文）。
   */
  const remarkRow = (d: OrderListItem): string => {
    const remark = d.remark
    const has = remark !== ''
    const safe = escapeHtml(remark)
    const tip = has ? safe : '点击可以编辑订单重要备注'
    return (
      `<a href="javascript:void(0)" class="orderRemark" data-order="${d.order_no}" ` +
      `data-remark="${safe}" style="color:#E03636" lay-tips="${tip}">${has ? safe : '无'}</a>`
    )
  }
  /**
   * 末列「操作」：常用三颗（编辑 / 退回大厅 / 发单）+ 已抛未接手时一颗撤回箭头；
   * 短信 / 加款 / 重发 / 协商撤销都不在本页（后三颗已搬到「第三方平台订单管理」页）。
   * ⛔ 对标两颗按钮带的 `data-fee` / `data-recMan` 我们载荷没有 ⇒ 不编值；⛔ 不发 `lay-submit`（会在表单里触发无关校验提示）。
   */
  const OP_BTN = (
    cls: string,
    tips: string,
    icon: string,
    orderNo: string,
    // `viewNo`：展示单号（编辑弹层标题用），缺省内置号；`data-id` 恒内置号（寻址键）。
    // `title`：本页 lay-tips 不渲染，只给明确要「悬停可见」的按钮同时拼 title，其余不传。
    viewNo: string = orderNo,
    title = '',
  ): string =>
    `<a class="layui-btn layui-btn-xs ${cls}" style="margin-right: 0px;margin-bottom: 0px;" ` +
    `lay-tips="${tips}"${title === '' ? '' : ` title="${title}"`} data-id="${orderNo}" data-viewId="${viewNo}">` +
    `<i class="layui-icon ${icon}"></i></a>`
  const btnfun = (d: OrderListItem): string => {
    // ⛔ 权限门：没有详情节点（any(`view_detail`,`view_content`)）⇒ 不渲染「编辑订单」（门是弹层第一发 retrieve，不是保存那半 add_edit）。
    const edit = canEditOrder
      ? OP_BTN(
          'layui-btn-danger submitEdit',
          '编辑订单',
          'layui-icon-edit',
          d.order_no,
          // 展示单号：来源号非空取它、空回退内置号；拼进 HTML 属性 ⇒ 过 escapeHtml。
          escapeHtml(d.source_order_no || d.order_no),
        )
      : ''
    // 「退回 / 撤回」等价判据是状态（`WAITING_GRAB` = 已抛未接）；⛔ 用严格相等；
    // cancelThrowHome 那颗 `style` 只有 `margin-bottom`（对标就没有 `margin-right`，别顺手统一）。
    // ⛔ 权限门：两分支各按自己那组节点渲染（独占闸）；它们是写路径、写模式探针抓不到，只有这道渲染门能守。
    const throwBtn =
      d.status === 'WAITING_GRAB'
        ? canWithdrawThrow
          ? `<a class="layui-btn layui-btn-xs layui-btn-warm cancelThrowHome" style="margin-bottom: 0px;" ` +
            `lay-tips="从内部接单大厅撤回" data-id="${d.order_no}" data-viewId="${d.order_no}">` +
            `<i class="layui-icon layui-icon-prev"></i></a>`
          : ''
        : canThrowToHall
          ? OP_BTN('layui-btn-warm throwEdit', '发到内部接单大厅', 'layui-icon-home', d.order_no)
          : ''
    const joinTask = OP_BTN('joinTask', '将订单加入到排团', 'layui-icon-template-1', d.order_no)
    // 地球按钮：渲染门 = 入口探针 + 平台写节点；⛔ 按钮常驻，别再把按行状态判「可重发」的门加回来（真机 bug：抛过一次按钮就消失、单关掉也不回来）。
    const PUSH_JDP_TIP = '把这笔订单一键发到各大交易平台'
    const pushJdp = pushJdpAvailable && platformWriteAllowedNow()
      ? OP_BTN(
          `layui-btn-normal ${PUSH_JDP_CLASS}`,
          PUSH_JDP_TIP,
          'layui-icon-website',
          d.order_no,
          // 标题用显示单号（同「编辑订单」口径）；`data-id` 仍是内部单号。
          escapeHtml(d.source_order_no || d.order_no),
          PUSH_JDP_TIP,
        )
      : ''
    // 撤回箭头：已抛未接手（远端 11/未知）才出；无活跃平台订单按未抛单档处理（不给箭头）；
    // ⛔ 加款/重发/协商撤销已整组搬走，别加回（不留点了没反应的死链）；同为平台写动作 ⇒ 同一道节点门。
    const pushAction =
      pushJdpAvailable &&
      platformWriteAllowedNow() &&
      hasActivePlatformPush(d.platform_push)
        ? pushRowAction(d.platform_push)
        : 'none'
    const pushCancel =
      pushAction === 'withdraw'
        ? `<a class="layui-btn layui-btn-xs layui-btn-normal ${PUSH_JDP_CANCEL_CLASS}"` +
          ` style="margin-right: 0px;margin-bottom: 0px;"` +
          ` lay-tips="${PUSH_JDP_CANCEL_TIP}" title="${PUSH_JDP_CANCEL_TIP}"` +
          ` data-id="${d.order_no}" data-viewId="${escapeHtml(d.source_order_no || d.order_no)}">` +
          `<i class="layui-icon layui-icon-return"></i></a>`
        : ''
    // ⛔ 不往这一列追加任何状态文字（业务方否决过）：单元格里只留按钮，状态有「状态」列。
    const groupGame = opts?.isGroupGame?.() ?? false
    return edit + throwBtn + (groupGame ? joinTask : '') + pushCancel + pushJdp
  }

  return [
    // 1. 对标 `{ unresize: false, field: '_orderId0', type: 'checkbox', width: 35 }`
    { type: 'checkbox', field: '_orderId0', width: 35, unresize: false },
    { field: '_orderid', title: '订单号', width: '12%', unresize: false, templet: orderIdRow },
    { field: 'orderId', title: '系统内置单号', width: 142, hide: true, unresize: false, templet: sysOrderIdRow },
    { field: '_role', title: '角色名', width: '5.5%', unresize: false, templet: roleNameRow },
    {
      field: '_project',
      title: '代练/陪玩内容',
      width: '10.4%',
      unresize: false,
      // ⛔ title 与正文都过 escapeHtml（project_text 是录入自由文本）；有活跃平台订单转紫（无推送不加 style）。
      // ⛔ tips 只拼真有的那一段：载荷没有项目分类名，不拼空段。
      templet: (d) => {
        const project = escapeHtml(d.project_text)
        return (
          `<a href="javascript:void(0)" class="${PROJECT_EDIT_CLASS}" data-order="${d.order_no}" ` +
          `data-project="${project}"${pushTextStyle(d)} ` +
          `lay-tips="代练/陪玩项目：${project}，点击可快速编辑" title="${project}">${project}</a>`
        )
      },
    },
    { field: '_info', title: '角色信息', unresize: false, templet: infoRow },
    { field: '_sta', title: '状态', width: 60, align: 'center', unresize: false, templet: staRow },
    { field: '_hour', title: '时间', width: 80, align: 'center', unresize: false, templet: hourRow },
    { field: '_ww', title: '旺旺/来源', width: '5%', align: 'center', unresize: false, templet: wangwangRow },
    // ⛔ 订单来源默认可见（对标默认隐藏，等权限位接上再改按权限显隐）；读 `source_name`（字典当前名，没挂字典由**后端**回落 `source` 快照）——⛔ 前端不做第二套兜底、数据库 `source` 一字不改。
    { field: 'source_name', title: '订单来源', width: 100, align: 'center', unresize: false },
    {
      field: '_rec',
      title: '接手人',
      width: '4.7%',
      align: 'center',
      unresize: false,
      // 三态见 `takerRow`（空态仍是那个 `layui-btn-xs layui-btn-normal` 的「指派打手」按钮）。
      templet: takerRow,
    },
    { field: '_throw', title: '发单价', width: 70, align: 'center', unresize: false, templet: throwPriceRow },
    { field: '_price', title: '接单价', width: 70, align: 'center', unresize: false, templet: priceRow },
    { field: '_netreceipts', title: '实收款', width: 78, align: 'center', unresize: false, templet: netReceiptsRow },
    { field: '_refund', title: '退款', width: 60, align: 'center', unresize: false, templet: refundRow },
    { field: '_img', title: '订单截图', width: 64, align: 'center', unresize: false, templet: imgRow },
    { field: '_kefu', title: '销售客服', width: 60, align: 'center', unresize: false, templet: kefuRow },
    { field: '_assUserName', title: '发单人', width: 60, align: 'center', unresize: false, templet: assUserNameRow },
    // 列宽 150：createRow 是定长 16 字符（YYYY-MM-DD HH:mm），75 放不下会截断。
    { field: '_create', title: '录单时间', width: 150, align: 'center', unresize: false, templet: createRow },
    { field: '_completeTime', title: '完单时间', width: 150, hide: true, align: 'center', unresize: false, templet: completeTimeRow },
    // 录单人：值来自 `created_by`（优先显示账号名称，回落账号快照）；空值不造「无」；⛔ 自由文本 ⇒ 过 escapeHtml。
    { field: 'addOrderUserName', title: '录单人', width: 60, align: 'center', unresize: false, templet: (d) => escapeHtml(d.created_by_name || d.created_by) },
    // 重要备注：只保留这一列（对标成对的 `_remark2` / `_remark` 去重后留 `_remark`）。
    { field: '_remark', title: '重要备注', width: 80, unresize: false, templet: remarkRow },
    // 列宽 240：历史上按钮更多时 130px 会「看得见、点不到」；现在只剩三颗、240 偏宽，但改宽度属可见偏离，不动。
    { field: '_change', title: '操作', width: 240, unresize: false, templet: btnfun, fixed: 'right' },
  ]
}

/* ───────────────────────────── 挂载 ───────────────────────────── */

/**
 * ⛔ 本页「进订单详情」两处入口都是原地开层 ⇒ 没有任何跳路由入口，注入口 `onNavigate` 已删；
 * ⛔ 别留空占位参数（下一个读者会以为还有导航需求）。将来真有跳路由需求再按 `IslandContext` 加回来。
 */

/** 把游戏字典灌进「游戏筛选」下拉（value 用**我们的** id，文案是游戏名）。 */
function fillGameSelect(root: HTMLElement, games: OrderDictionaries["games"]): void {
  const select = root.querySelector<HTMLSelectElement>('select#gameName')
  if (!select) return
  // 首项空值 +「请选择」：对标首项无 selected，layui 无选中时显示 placeholder「请选择」（parity 实测）；空值 = 不按游戏筛。
  select.innerHTML =
    '<option value="">请选择</option>\n' +
    games.map((g) => `<option value="${g.id}">${escapeHtml(g.name)}</option>`).join('\n')
}

/** 填「订单来源」那个空 `<select>`。⛔ 不写占位首项（formSelects 单选壳加空值 option 反而多出一个可选项）；⛔ 必须在 `initFormSelects` 之前（render() 那一刻读 option 建 `<dl>`）。 */
function fillOrderSourceSelect(
  root: HTMLElement,
  sources: readonly { id: number; name: string }[],
): void {
  const select = root.querySelector<HTMLSelectElement>('select#orderSoures')
  if (!select) return
  for (const source of sources) {
    const option = document.createElement('option')
    option.value = String(source.id)
    option.textContent = source.name
    select.appendChild(option)
  }
}

/** 筛选栏「订单分组」控件 id（对标没有这个筛选，整块运行期新造）。⛔ 必须在 `initFormSelects` 之前注入并填好 option；name 用 `sourceGroup`（来源管理页那份叫 `soureGroup`，两套别混）。 */
export const SOURCE_GROUP_SELECT_ID = 'sourceGroup'

/** 注入「订单分组」那一块；锚点是同排「订单来源」的 `.layui-inline`（结构变了返回 false）。 */
export function injectSourceGroupFilter(root: HTMLElement): boolean {
  if (root.querySelector(`#${SOURCE_GROUP_SELECT_ID}`) !== null) return true
  const anchor = root.querySelector('select#orderSoures')?.closest('.layui-inline') ?? null
  if (anchor === null) return false
  const html =
    '<div class="layui-inline">' +
    '<label class="layui-form-label" style="width: 91px">订单分组</label>' +
    '<div class="layui-input-inline" style="">' +
    `<select name="${SOURCE_GROUP_SELECT_ID}" id="${SOURCE_GROUP_SELECT_ID}"` +
    ' xm-select-radio lay-search="" ' +
    `xm-select="${SOURCE_GROUP_SELECT_ID}" xm-select-search=""` +
    ' xm-select-search-type="title" xm-select-skin="default" xm-select-height="36px"' +
    ' class="" title="">' +
    '<option value="">全部分组</option>' +
    '</select>' +
    '</div></div>'
  anchor.insertAdjacentHTML('afterend', html)
  return true
}

/** 把分组字典写进「订单分组」下拉。⛔ 只 append、不重写（首项 `value=""` 的 option 就是折叠态显示的「全部分组」）；字典取不到传空数组 ⇒ 只剩首项，页面照常可用。 */
export function fillSourceGroupSelect(
  root: HTMLElement,
  groups: readonly { id: number; name: string }[],
): void {
  const select = root.querySelector<HTMLSelectElement>(`#${SOURCE_GROUP_SELECT_ID}`)
  if (select === null) return
  for (const group of groups) {
    const option = document.createElement('option')
    option.value = String(group.id)
    option.textContent = group.name
    select.appendChild(option)
  }
}

/** 把「销售客服」候选写进那一格 `<select>`。⛔ 只 append、不重写（首项空值 option 是 placeholder）；⛔ 必须在 `initFormSelects` 之前调；值=用户主键（原样送 `sales_user`）；候选取不到传空数组（⛔ 不弹警告）。 */
export function fillKefuSelect(
  root: HTMLElement,
  items: readonly { id: number; name: string }[],
): void {
  const select = root.querySelector<HTMLSelectElement>(`select#${KEFU_SELECT_ID}`)
  if (!select) return
  for (const item of items) {
    const option = document.createElement('option')
    option.value = String(item.id)
    option.textContent = item.name
    select.appendChild(option)
  }
}

/** ⛔ 运行期显隐规则 `applyGroupTaskVisibility` 已整条删除：`.group`/`.task` 两块与三颗排团按钮已真删（phase1 spec 钉住），再留就是对空集合调 `.hide()` 的死码；⛔ 别照对标修回来。 */

/**
 * 两个日期筛选的默认值：`yyyy-MM-dd HH:mm:ss`，近 3 个月（今天 − 3 个自然月 00:00:00 ~ 今天 23:59:59）。
 * ⛔ 「−3 个月、日号保留」是单样本实测 + 同族规律推断的口径，没有更硬证据别改成「−90 天」。
 * 算式已搬到 `@/layui-island/dateRange`（唯一实现），这里是再导出，签名不变（既有单测钉同一份实现）。
 */
export const defaultDateRange = sharedDefaultDateRange

/** 把默认值写进两个输入框（对标是服务端 `value` 属性；我们没有服务端渲染，只能在挂载时填）。 */
function applyDefaultDateRange(root: HTMLElement): void {
  const { sDate, eDate } = defaultDateRange()
  const s = root.querySelector<HTMLInputElement>('input#sDate')
  const e = root.querySelector<HTMLInputElement>('input#eDate')
  if (s) s.value = sDate
  if (e) e.value = eDate
}

/** ⛔ 「服务器/平台」筛选改造（`adaptServerField`）已整段删除（D-0093）；⛔ `fetchServers()` 与 `serverNames` 必须留着——「角色信息」列拿到的仍是区服**主键**，要靠它映射成名字。 */

/** `#popgameName` 那个**真控件**的 id（选择器与类名分开写：别把 `.` 拼进 `id`，AGENTS.md §5-G 坑①）。 */
export const POPGAME_SELECT_ID = 'popgameName'

/** `#popgameName` 折叠态的占位文案（照对标运行期渲染值）。 */
export const POPGAME_PLACEHOLDER = '请选择'

/** 把空占位 `div` 换成真 `<select>`（`recType` 切档 + 远程关键词搜索）。⛔ 必须在 `initFormSelects('select[xm-select]')` 之前调（render() 那一刻读 option 建 `<dl>`）；它是真控件替换，不是「尚未开放」占位。 */
export function renderPopgameName(root: HTMLElement): void {
  const host = root.querySelector<HTMLElement>(`#${POPGAME_SELECT_ID}`)
  if (!host) return
  const select = document.createElement('select')
  select.id = POPGAME_SELECT_ID
  // `name` 是 `form.val('form')` 取值用的键（真控件要能被表单读到）；`xm-select` 是 formSelects
  // 的挂载约定（`initFormSelects` 按 `select[xm-select]` 找它）。
  select.setAttribute('name', POPGAME_SELECT_ID)
  select.setAttribute('xm-select', POPGAME_SELECT_ID)
  select.setAttribute('xm-select-radio', '')
  // `xm-select-search` 的**属性值** = 远程搜索的 URL（vendored formselects：有值即开远程搜索，
  // 空值 = 只在已加载 option 里本地过滤；2026-11-09 起走远程，见「已知差异 68」）。
  select.setAttribute('xm-select-search', FILTER_OPTIONS_SEARCH_URL)
  select.setAttribute('xm-select-skin', 'default')
  select.setAttribute('xm-select-height', '36px')
  appendPlaceholderOption(select)
  host.replaceWith(select)
}

/** 独立「发单人」下拉 id（**权威入口**）：与旧组合控件指向同一个后端参数 `hall_publisher_user`，两控件互相清空、`toOrderSearch` 是唯一出口。 */
export const HALL_PUBLISHER_SELECT_ID = 'hallPublisherUser'

/**
 * 切游戏重取候选后，旧已选 id 已不在新名单里 ⇒ 清空并提示重选（保留旧 id 只会筛出空集，
 * 且页面上看不出原因）。
 */
export const PUBLISHER_GAME_SWITCHED_TEXT = '游戏已切换，请重选发单人'

/** 运行期注入独立「发单人」下拉（证据件一字不动，排在旧组合控件之后）。⛔ 必须在 `initFormSelects` 之前调；候选 = 输入关键词时远程搜（不再预载名单）。 */
export function renderHallPublisherFilter(root: HTMLElement): void {
  if (root.querySelector(`#${HALL_PUBLISHER_SELECT_ID}`)) return
  const legacy = root.querySelector<HTMLElement>(`#${POPGAME_SELECT_ID}`)
  const anchor = legacy?.closest('.layui-inline') ?? null

  const inline = document.createElement('div')
  inline.className = 'layui-inline'
  const label = document.createElement('label')
  label.className = 'layui-form-label'
  label.style.width = '80px'
  label.textContent = '发单人'
  const wrap = document.createElement('div')
  wrap.className = 'layui-input-inline'
  wrap.style.width = '140px'

  const select = document.createElement('select')
  select.id = HALL_PUBLISHER_SELECT_ID
  // `name` 是 `form.val('form')` 取值用的键；`xm-select` 是 formSelects 的挂载约定。
  select.setAttribute('name', HALL_PUBLISHER_SELECT_ID)
  select.setAttribute('xm-select', HALL_PUBLISHER_SELECT_ID)
  select.setAttribute('xm-select-radio', '')
  // 属性值 = 远程搜索 URL（同 `renderPopgameName`，「已知差异 68」）。
  select.setAttribute('xm-select-search', FILTER_OPTIONS_SEARCH_URL)
  select.setAttribute('xm-select-skin', 'default')
  select.setAttribute('xm-select-height', '36px')
  appendPlaceholderOption(select)

  wrap.appendChild(select)
  inline.append(label, wrap)
  if (anchor?.parentElement) anchor.after(inline)
  else (root.querySelector('form.layui-form') ?? root).appendChild(inline)
}

/* ═══════════ 打手 / 发单人下拉的远程关键词搜索（「已知差异 68」，2026-11-09） ═══════════ */

/** 两个下拉远程搜索共用的端点 URL（`xm-select-search` 属性值 = vendored formselects 的 searchUrl）。 */
export const FILTER_OPTIONS_SEARCH_URL = '/api/business/orders/filter-options/'

/** 远程搜索关键词防抖毫秒（对标默认 500，我方取 400——计划拍板值；vendored `ajax.delay`）。 */
export const FILTER_SEARCH_DELAY_MS = 400

/** 远程搜索回包的一行（后端每项**恰好** `{"id","name"}`，与 `api/orders.ts::Option` 同形）。 */
export type FilterOptionRow = { id: number; name: string }

/** 给下拉塞「仅占位」那条 option（`value=""` + `POPGAME_PLACEHOLDER`；两个下拉同一文案）。 */
function appendPlaceholderOption(select: HTMLSelectElement): void {
  const placeholder = document.createElement('option')
  placeholder.value = ''
  placeholder.textContent = POPGAME_PLACEHOLDER
  select.appendChild(placeholder)
}

/** 远程请求的固定参数 `data`：当前游戏筛选；没选游戏 ⇒ **不带键**（发空串会被后端判 1000）。 */
export function buildFilterSearchData(game: number | null): Record<string, number> {
  return game === null ? {} : { game }
}

/** 统一信封 → 当前档位的候选行（`#popgameName` 用）。⚠️ `recType` 由调用方在**响应时刻**读进来（切档瞬间的在途回包不错位）。 */
export function popgameRowsFromEnvelope(res: unknown, recType: string): FilterOptionRow[] {
  return optionRowsFromEnvelope(res, recType === '10' ? 'publishers' : 'takers')
}

/** 统一信封 → 发单人候选行（`#hallPublisherUser` 恒取 `publishers`）。 */
export function publisherRowsFromEnvelope(res: unknown): FilterOptionRow[] {
  return optionRowsFromEnvelope(res, 'publishers')
}

/** 信封拆包：`code≠0` / 形状不合 ⇒ 空数组（控件自己显示「无匹配项」，不另造提示）。 */
function optionRowsFromEnvelope(
  res: unknown,
  pick: 'takers' | 'publishers',
): FilterOptionRow[] {
  if (typeof res !== 'object' || res === null) return []
  const body = res as { code?: unknown; data?: unknown }
  if (body.code !== 0) return []
  if (typeof body.data !== 'object' || body.data === null) return []
  const list = (body.data as Record<string, unknown>)[pick]
  if (!Array.isArray(list)) return []
  return list.filter(
    (row): row is FilterOptionRow =>
      typeof row === 'object' &&
      row !== null &&
      typeof (row as FilterOptionRow).id === 'number' &&
      typeof (row as FilterOptionRow).name === 'string',
  )
}

/**
 * formselects 远程搜索的**空关键词闸**（空/空白 ⇒ 不发）。
 * ⚠️ **承重闸**：vendor 每次 render 对设了 searchUrl 的控件自动发一次空关键词首搜
 * （formselects-v4.js:313-322，行号证据与推导见页测试的直调用例），`beforeSearch` 是它唯一的闸
 * （`:358`：返回假值 ⇒ search 分支短路、ajax 不发）。
 * ⚠️ 且闸的判定发生在 **render 那一刻**（`search()` 同步读 `ajaxs[id]`，空关键词的请求只是
 * 被它排进 10ms 的 setTimeout，不会再回头判一次）⇒ 配置必须在 `initFormSelects` **之前**并入
 * `ajaxs[id]`（挂载收尾才配 = 首发空关键词请求抢在闸之前发出去，线上实测复现过）。
 */
export function filterSearchGate(_id: unknown, _url: unknown, keyword: unknown): boolean {
  // 空 / 空白 ⇒ 不发（vendor 在 search() 的 :343 已 trim，这里再 trim 是给非字符串兜底）。
  return typeof keyword === 'string' && keyword.trim() !== ''
}

/** 给一个下拉注入 formselects 远程搜索 ajax 配置（深合并进该 id 的 `ajaxs[id]`；jsdom 无 formselects ⇒ no-op）。⚠️ 无闭包依赖 ⇒ 挂载早期（render 之前）也能调。 */
function configFilterSearch(id: string, config: Record<string, unknown>): void {
  const fs = getFormSelects() as
    | (FormSelectsInstance & {
        config?: (configId: string, ajaxConfig: Record<string, unknown>) => unknown
      })
    | undefined
  fs?.config?.(id, config)
}

/**
 * formselects 远程搜索这份 ajax 配置「怎么造」（纯函数：jsdom 里 formSelects 未渲染，页测试直调它断言各键）。
 * ⚠️ `beforeSearch` 闸见 `filterSearchGate`；完整配置必须在**响应时刻**读的档位经 `toRows` 进来。
 */
export function buildFilterSearchConfig(input: {
  game: number | null
  toRows: (res: unknown) => FilterOptionRow[]
}): {
  searchName: string
  keyVal: string
  delay: number
  data: Record<string, number>
  beforeSearch: (id: unknown, url: unknown, keyword: unknown) => boolean
  beforeSuccess: (id: unknown, url: unknown, inputValue: unknown, res: unknown) => FilterOptionRow[]
} {
  return {
    searchName: 'search',
    keyVal: 'id',
    delay: FILTER_SEARCH_DELAY_MS,
    data: buildFilterSearchData(input.game),
    beforeSearch: filterSearchGate,
    beforeSuccess: (_id, _url, _input, res) => input.toRows(res),
  }
}

/* ═══════════ 订单号右键菜单（spec §6 / Task 7） ═══════════ */

/** 注入右键菜单样式表的元素 id（照 `renderPopgameName` 早先注入页内 `<style>` 的先例：页面自带 `<style>`，按 id 去重）。 */
export const CONTEXT_MENU_STYLE_ID = 'tt-orderlist-contextmenu-style'

/** 右键菜单**恰好 4 项**（业务方裁定其余不做；测试按整数组断言）。`label` 与顺序逐字取对标；`icon` 是我们挑的 layui 字形（对标那套字体全仓取不到）。 */
export const CONTEXT_MENU_ITEMS = [
  { key: 'viewDetails', label: '查看详情', icon: 'layui-icon-form' },
  { key: 'copyOrderID', label: '复制订单编号', icon: 'layui-icon-edit' },
  { key: 'copyOrder', label: '复制订单内容', icon: 'layui-icon-file' },
  { key: 'copyOrderToNewOrder', label: '复制成新订单', icon: 'layui-icon-template-1' },
] as const

/** 对标插件给这四项的 icon 名；插件拼成两个类 `context-menu-icon context-menu-icon-<name>` ⇒ 本页 DOM 照它写（`copyOrder`/`copyOrderToNewOrder` 对标共用 `copy`，按图标名分不开 ⇒ 字形按 `data-key` 指认）。 */
const CONTEXT_MENU_ICON_NAMES = {
  viewDetails: 'paste',
  copyOrderID: 'edit',
  copyOrder: 'copy',
  copyOrderToNewOrder: 'copy',
} as const

/** 菜单根 `<ul>` 的**类名**（选择器另拼——别把 `.` 拼进 `class`，AGENTS.md §5 / E-0061）。 */
const CONTEXT_MENU_ROOT_CLASS = 'context-menu-list'
/** 菜单项 `<li>` 的类名。 */
const CONTEXT_MENU_ITEM_CLASS = 'context-menu-item'
/** 分隔线 `<li>` 的类名。 */
const CONTEXT_MENU_SEPARATOR_CLASS = 'context-menu-separator'
/** 悬停态类名（`:hover` 不在插件 CSS 里，只能由 JS 加/摘）。 */
const CONTEXT_MENU_HOVER_CLASS = 'context-menu-hover'
/** 订单号格的选择器；运行期 templet 渲染、挂载时 DOM 里没有 ⇒ 只能委托在 `root` 上。 */
const ORDER_LINE_SELECTOR = '.orderLine'
/** 那一格的 id 前缀（单号从这里取，**不是**从显示文本取）。 */
const ORDER_LINE_ID_PREFIX = 'order_'

/** 右键菜单样式：前段逐字抄对标插件 CSS；末尾五条是本仓实现（对标字体取不到 ⇒ 换 vendored layui 字体、字形按 `data-key` 指认）。⚠️ 原文那三段 `content:"\EA0x"` 不再生效但**留着为了逐字可对拍**，别当死代码删掉。 */
export const CONTEXT_MENU_STYLE_RULES =
  '.context-menu-list{position:absolute;display:inline-block;min-width:13em;max-width:26em;padding:.25em 0;margin:.3em;font-family:inherit;font-size:inherit;list-style-type:none;background:#fff;border:1px solid #bebebe;border-radius:.2em;-webkit-box-shadow:0 2px 5px rgba(0,0,0,.5);box-shadow:0 2px 5px rgba(0,0,0,.5)}\n' +
  '.context-menu-item{position:relative;-webkit-box-sizing:content-box;-moz-box-sizing:content-box;box-sizing:content-box;padding:.2em 2em;color:#2f2f2f;-webkit-user-select:none;-moz-user-select:none;-ms-user-select:none;user-select:none;background-color:#fff}\n' +
  '.context-menu-item.context-menu-hover{color:#fff;cursor:pointer;background-color:#2980b9}\n' +
  '.context-menu-separator{padding:0;margin:.35em 0;border-bottom:1px solid #e6e6e6}\n' +
  '.context-menu-icon::before{position:absolute;top:50%;left:0;width:2em;font-family:context-menu-icons;font-size:1em;font-style:normal;font-weight:400;line-height:1;color:#2980b9;text-align:center;-webkit-transform:translateY(-50%);-ms-transform:translateY(-50%);-o-transform:translateY(-50%);transform:translateY(-50%);-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}\n' +
  '.context-menu-icon.context-menu-hover:before{color:#fff}\n' +
  '.context-menu-icon-copy:before{content:"\\EA02"}\n' +
  '.context-menu-icon-edit:before{content:"\\EA05"}\n' +
  '.context-menu-icon-paste:before{content:"\\EA07"}\n' +
  '.context-menu-icon::before{font-family:layui-icon}\n' +
  '.context-menu-item[data-key="viewDetails"]::before{content:"\\e63c"}\n' +
  '.context-menu-item[data-key="copyOrderID"]::before{content:"\\e642"}\n' +
  '.context-menu-item[data-key="copyOrder"]::before{content:"\\e621"}\n' +
  '.context-menu-item[data-key="copyOrderToNewOrder"]::before{content:"\\e656"}'

/**
 * 把右键菜单的样式注入页根（照 `renderPopgameName` 早先注入页内 `<style>` 的先例：按 id 去重、
 * 卸载时随 `root.innerHTML = ''` 一起走）。必须在 `root.innerHTML = 证据件` **之后**调。
 */
export function renderContextMenuStyle(root: HTMLElement): void {
  if (document.getElementById(CONTEXT_MENU_STYLE_ID)) return
  const style = document.createElement('style')
  style.id = CONTEXT_MENU_STYLE_ID
  style.textContent = CONTEXT_MENU_STYLE_RULES
  root.prepend(style)
}

/** 订单号标题与行内固定的三角槽对齐；表头仍保留普通文本标题供 layui 排序与测试识别。 */
function renderOrderIdHeaderStyle(root: HTMLElement): void {
  if (document.getElementById('tt-order-id-header-style')) return
  const style = document.createElement('style')
  style.id = 'tt-order-id-header-style'
  style.textContent = `
    .layui-table-view .layui-table-header th[data-field="_orderid"] .layui-table-cell{padding-left:29px}
    .layui-table-view td[data-split-line]{position:relative;--split-line-left:calc(18px + .5ch);font-size:12px}
    .layui-table-view td[data-split-line]::before{position:absolute;inset:0 auto -1px var(--split-line-left);border-left:1px solid #83aaa5;pointer-events:none;content:''}
    .layui-table-view td[data-split-line="start"]::before{top:calc(50% + 8px)}
    .layui-table-view td[data-split-line="end"]::before{bottom:50%}
    .layui-table-view td:is([data-split-line="branch"],[data-split-line="end"])::after{position:absolute;top:50%;left:var(--split-line-left);width:calc(1.25ch - 2px);border-top:1px solid #83aaa5;pointer-events:none;content:''}
    .split-child-connector{display:inline-flex;width:1.75ch;flex:0 0 1.75ch;vertical-align:middle}
  `
  root.prepend(style)
}

/* ⭐ 展开区样式：尺寸全部继承主表（子单是同一张表里的真行），只剩三条规则。
 * ⛔ 旧的 `.tt-platform-children-*` 三条自拟规则已整条删除（内嵌小表被真行取代）；只影响我们注入的节点。
 */
/** 子行订单号格缩进 px。 */
export const PLATFORM_CHILD_INDENT_PX = 20
export const PLATFORM_CHILDREN_STYLE_ID = 'tt-platform-children-style'
export const PLATFORM_CHILDREN_STYLE_RULES =
  `.${PLATFORM_CHILD_ROW_CLASS}{background-color:#fff}\n` +
  `.${PLATFORM_CHILD_ROW_CLASS} .sontr{margin-left:${PLATFORM_CHILD_INDENT_PX}px}\n` +
  `.${PLATFORM_CHILD_EDIT_CLASS}{color:#1e9fff}`

/** P6：注入展开区样式（同一份，按 id 去重；卸载随 `root.innerHTML=''` 一起走）。 */
export function renderPlatformChildrenStyle(root: HTMLElement): void {
  if (document.getElementById(PLATFORM_CHILDREN_STYLE_ID)) return
  const style = document.createElement('style')
  style.id = PLATFORM_CHILDREN_STYLE_ID
  style.textContent = PLATFORM_CHILDREN_STYLE_RULES
  root.prepend(style)
}

/** 剪贴板三条提示的文案（「已成功复制订单号：<号>到剪贴板」在调用点拼）。 */
const COPY_ORDER_ID_FAIL_MESSAGE = '复制订单号失败了，请手动复制'
const COPY_ORDER_CONTENT_FAIL_MESSAGE = '复制订单内容失败了，请手动复制'
const COPY_ORDER_CONTENT_OK_MESSAGE = '已成功复制订单内容到剪贴板'
/** 「复制成新订单」成功文案（我方 `/copy` 回的 `msg` 是空串 ⇒ 只能由前端给一句，不是照抄）。 */
const COPY_ORDER_SUCCESS_MESSAGE = '已复制成新订单'

type BatchSelection = {
  kind: 'batch'
  rows: OrderListItem[]
  orderNos: string[]
  sourceOrderNos: ReadonlyMap<string, string>
  isSelectionCurrent: () => boolean
}

/** 工具栏勾一笔也走批量确认，行内操作仍用单笔端点。 */
export type BatchTarget = { kind: 'single'; orderNo: string } | BatchSelection

/** 批量成功文案（spec §D12）：**恰好 1 笔**沿用该动作原有文案，多笔用 `已处理 N 笔订单`。 */
function batchSuccessMessage(single: string) {
  return (appliedCount: number): string =>
    appliedCount === 1 ? single : `已处理 ${appliedCount} 笔订单`
}

export async function mountOrderListPage(root: HTMLElement): Promise<() => void> {
  // ⛔ 别把 'formSelects' 写进这个模块列表：它不是 layui 内置模块，会去取 modules/formSelects.js → 404，且那个 loader 静默挂住（页面白屏、连错误都不抛）。
  const layui: LayuiNamespace = await bootstrapIsland(['form', 'table', 'layer', 'laydate'])
  const form = layui.form
  const table = layui.table as LayuiModule
  const layer = layui.layer as LayuiModule
  const laydate = layui.laydate as LayuiModule

  // 字典与候选一起取（失败了也照常渲染：下拉为空、区服显示主键；辅助筛选取不到**不拦页面、不弹警告**）。
  const [dictionaries, orderGroups, kefuItems] = await Promise.all([
    fetchOrderDictionaries({ scopedGames: true })
      .catch(() => ({ games: [], servers: [], order_sources: [] })),
    fetchOrderSourceGroups()
      .then((page) => page?.items ?? [])
      .catch(() => [] as OrderSourceGroupRow[]),
    // 销售客服候选：必须赶在 initFormSelects 之前填进 <select>（见 fillKefuSelect），所以在这里发起；
    // 逐键兜底 items ?? []（形状不合当没有候选，⛔ 不掀整页）；截断不再弹提示（业务方拍板），truncated 不进页面。
    fetchKefuOptions()
      .then((opts) => opts.items ?? [])
      .catch(() => [] as { id: number; name: string }[]),
  ])
  const { games, servers, order_sources: orderSources } = dictionaries
  const gameNames = new Map(games.map((g) => [g.id, g.name] as const))
  const serverNames = new Map(servers.map((s) => [s.id, s.name] as const))

  // 权限门：入口按节点渲染；⛔ fail-closed——fetchMe() 失败 ⇒ 全 false ⇒ 入口不渲染（别让它拦页面：
  // 取不到只是少几个入口，渲染出点了必 403 的入口才是缺陷）。
  // ⚠️ 用 try/catch 而不是 .catch()：桩把 request() 换成返回 undefined 时，fetchMe 里那次 .then 是**同步抛**的，.catch() 接不到。
  let viewer: NodeViewerInput | undefined
  try {
    viewer = await fetchMe()
  } catch {
    viewer = undefined
  }
  setOrderListPermissions(viewer)
  // ⭐ **平台写动作**（行内发单地球 / 撤回箭头）挂 `order.platform_write`。
  // 降级口径（后端该节点未上线时按 `order.list`、身份取不到时照画 = 升级前行为）只在
  // `@/layui-island/perms` 一处（见 `canPlatformWrite` 的块注释）。
  applyPlatformWritePermission(viewer)

  // 入口探针：后端答 `{available: true}` 才画；⛔ fail-closed——读失败绝不当成「可用」，在控制台留原因方便排障。
  // ⚠️ 必须在 `table.render` 之前定下来：行模板读的是模块级开关。
  {
    const probe = await fetchPushAvailability()
    setPushJdpAvailable(probe.state === 'available')
    if (probe.state !== 'available') {
      console.info(
        `[order-orderlist] 一键发单到交易平台按钮未渲染：入口探针 ${probe.state}（fail-closed）`,
      )
    }
  }

  document.title = '订单管理中心'

  const res = await fetch(`${import.meta.env.BASE_URL}${EVIDENCE_URL}`)
  if (!res.ok) throw new Error(`证据件取不到（HTTP ${res.status}）：${EVIDENCE_URL}`)
  root.innerHTML = await res.text()
  // ⛔ 必须在塞证据件之后：工具栏 `setRecMan` 是静态 DOM，之前根本不存在。
  applyOrderListPermissionGates(root)
  // 「同步收款」是新增按钮 ⇒ 运行期注入；⛔ 必须在塞证据件之后，且按节点 fail-closed。
  injectReceiptSyncButton(root)

  fillGameSelect(root, games)
  // 「订单来源」证据件里是空 `<select>`，这里用我们的字典填——⛔ 必须在 `initFormSelects` 之前。
  fillOrderSourceSelect(root, orderSources)
  // 注入「订单分组」：注入与填 option 都必须在 `initFormSelects` 之前。
  injectSourceGroupFilter(root)
  fillSourceGroupSelect(root, orderGroups)
  // 「销售客服」空壳填候选；⛔ 必须在 `initFormSelects` 之前。
  fillKefuSelect(root, kefuItems)
  // ⛔ 这里曾有 `applyGroupTaskVisibility` / `adaptServerField`——都随对应筛选块真删（见上方墓碑注释）；`servers` 仍用于 `serverNames` 映射。
  renderPopgameName(root)
  // 独立「发单人」下拉；⛔ 必须在 `initFormSelects` 之前。
  renderHallPublisherFilter(root)
  // ⭐ 空关键词闸的**预埋**（2026-11-09 存量缺陷修正）：vendored formselects 在 render 那一刻就
  // 同步判一次闸并只把请求排进 setTimeout（不再回头判）⇒ 完整配置等到挂载收尾的 `wireFilterSearch()`
  // 才并入的话，首发空关键词请求会抢在闸之前发出去（formselects-v4.js:313-322 / :343-368，
  // 推导见 `filterSearchGate` 与页测试）。`config()` 先把配置深并进 `ajaxs[id]`、对未渲染的控件
  // 才顺手 render ⇒ 此刻预埋，render 时刻的自动首搜就被挡住；其余键挂载收尾再深并（其余键原样保留）。
  for (const id of [POPGAME_SELECT_ID, HALL_PUBLISHER_SELECT_ID]) {
    configFilterSearch(id, { beforeSearch: filterSearchGate })
  }
  // ⛔ 运行期摘掉「接发组合」搜索按钮的 `data-notopen`（root 兜底会拦成「尚未开放」假话）；必须在塞证据件之后。
  detachComboPriceNotOpen(root)
  // 右键菜单样式；必须在塞证据件之后。
  renderContextMenuStyle(root)
  renderOrderIdHeaderStyle(root)
  // 展开区样式（同样必须在塞证据件之后）。
  renderPlatformChildrenStyle(root)
  applyDefaultDateRange(root)
  // 多选下拉交给对标的 formselects-v4（它读 `xm-select` 属性）。
  initFormSelects('select[xm-select]')
  // 再 render 一次是幂等保险（不是承重逻辑——去掉它判据照样绿）；保证「候选已填」在填完之后总有一次显式 render 对着它。
  getFormSelects()?.render(KEFU_SELECT_ID)
  // 页签/折叠面板（本页没有，但口径与其它 island 页一致，成本极低）。
  initIslandElements(layui)
  // 日期控件只配 elem+type：默认值不走 JS，是 HTML 上的服务端 value（上面 `applyDefaultDateRange` 补的就是它）。
  // ⛔ 别给 `#sDate` 加 `min`（那是别的页的东西，会静默禁掉旧日期的选择）。
  ;(laydate.render as (o: unknown) => void)({ elem: '#sDate', type: 'datetime' })
  ;(laydate.render as (o: unknown) => void)({ elem: '#eDate', type: 'datetime' })

  const msg = (text: string): void => islandMsg(layer, text)
  /** ⭐ 指派 / 抢单成功后的提示出口（后端 `msg` 优先，含「未能撤回」警告）；判定表三页共用 `@/config/backendMessage`，⛔ 不在这里另写一套。 */
  const backendMessageActions = {
    toast: (text: string, durationMs?: number): void => {
      ;(layer.msg as (t: string, o?: unknown) => void)(text, { time: durationMs ?? 1500 })
    },
    alert: (text: string): void => {
      ;(layer.alert as (t: string, o?: unknown) => void)(text, {
        title: '注意：代练通订单未能撤回',
        icon: 0,
        shadeClose: false,
      })
    },
  }
  const notOpen = (label: string): void => msg(`「${label}」尚未开放`)
  const errText = (e: unknown): string => (e instanceof Error ? e.message : '操作失败')

  /** 最近一发请求实际带出去的筛选（空态文案判「有没有在筛」用它，不用再读一次表单）。 */
  let lastWhere: Record<string, unknown> = {}
  let firstDone = false
  /** 最近一发响应的统计块（`parseData` 写、`done` 读——layui 只认 `{code,msg,count,data}` 四个键，统计块塞不进去）。 */
  let lastSummary: OrderListSummary | null = null

  /** 末列「加入团」的游戏判据：严格相等「魔兽世界」/「诛仙世界」（⛔ 不是 indexOf——与另一条 `.group` 规则不同，别合并）；取**当前值**（列模板每次重载都跑）。 */
  const isGroupGameForRowButtons = (): boolean => {
    const sel = root.querySelector<HTMLSelectElement>('select#gameName')
    const name = sel?.selectedOptions?.[0]?.textContent?.trim() ?? ''
    return name === '魔兽世界' || name === '诛仙世界'
  }

  const mainRows = new Map<string, OrderListItem>()
  const splitRows = new Map<string, OrderListItem>()
  let selectionVersion = 0
  const mainTableSelector = '.layui-table-main'
  const mainRowSelector = `${mainTableSelector} > table > tbody > tr`
  const tableCheckboxSelector = 'input[type="checkbox"][name="layTableCheckbox"]'

  const checkedRows = (): OrderListItem[] => {
    const rows = new Map<string, OrderListItem>()
    for (const tr of root.querySelectorAll<HTMLTableRowElement>(mainRowSelector)) {
      const isSplit = tr.classList.contains(SPLIT_CHILD_ROW_CLASS)
      const box = tr.querySelector<HTMLInputElement>(
        isSplit ? 'input[type="checkbox"][lay-ignore]' : tableCheckboxSelector,
      )
      if (!box?.checked || box.disabled) continue
      // 普通复选框由 layui 画外皮，原始 input 本身不可见。
      let visible = true
      for (let node = box.parentElement; node && node !== root; node = node.parentElement) {
        const style = getComputedStyle(node)
        if (node.hidden || style.display === 'none' || ['hidden', 'collapse'].includes(style.visibility)) {
          visible = false
          break
        }
      }
      if (!visible) continue
      const orderNo = tr.querySelector<HTMLElement>(ORDER_LINE_SELECTOR)?.id.slice(ORDER_LINE_ID_PREFIX.length)
      if (!orderNo) continue
      const row = (isSplit ? splitRows : mainRows).get(orderNo)
      if (row) rows.set(orderNo, row)
    }
    return [...rows.values()]
  }

  const renderSelectionTotal = (): void => {
    const el =
      root.querySelector<HTMLElement>(`.layui-table-tool-temp ${SELECTION_TOTAL_SELECTOR}`) ??
      root.querySelector<HTMLElement>(SELECTION_TOTAL_SELECTOR)
    if (el) el.textContent = formatSelectionTotal(checkedRows())
  }

  const clearSelection = (): void => {
    selectionVersion += 1
    mainRows.clear()
    splitRows.clear()
    if (root.querySelector(mainTableSelector)) {
      // 同时清 layui 的行缓存和表头，避免下次渲染时全选状态回流。
      ;(table.setRowChecked as ((id: string, options: { index: 'all'; checked: boolean }) => void) | undefined)
        ?.(TABLE_ID, { index: 'all', checked: false })
    }
    for (const box of root.querySelectorAll<HTMLInputElement>(
      `${mainRowSelector} input[type="checkbox"], .layui-table-header ${tableCheckboxSelector}`,
    )) {
      box.checked = false
      box.indeterminate = false
    }
    renderSelectionTotal()
    ;(form.render as (type?: string) => void)('checkbox')
  }

  /** 给每个单元格补 `title`（悬停显示全文）。读 `textContent.trim()`（`innerText` 在 jsdom 没实现）；空格子也写 `title=""`（⛔ 别只挑非空）。 */
  const applyCellTitles = (): void => {
    for (const cell of Array.from(root.querySelectorAll<HTMLElement>('.layui-table-cell'))) {
      cell.setAttribute('title', (cell.textContent ?? '').trim())
    }
  }

  const onTableCheckbox = (event: { type?: 'one' | 'all'; checked?: boolean }): void => {
    selectionVersion += 1
    if (event.type === 'all' && event.checked !== true) {
      for (const box of root.querySelectorAll<HTMLInputElement>(
        `${mainRowSelector}.${SPLIT_CHILD_ROW_CLASS} input[type="checkbox"]`,
      )) box.checked = false
    }
    renderSelectionTotal()
  }
  ;(table.on as (e: string, cb: typeof onTableCheckbox) => void)('checkbox(datatable)', onTableCheckbox)

  /** 把 layui 事件里的 `obj.tr` 解成真 `<tr>`（真身是 jQuery 形状 `{0: tr}`；页测试也传裸节点）。 */
  const resolveTableTr = (trLike: unknown): HTMLTableRowElement | null => {
    if (trLike instanceof HTMLTableRowElement) return trLike
    if (trLike !== null && typeof trLike === 'object' && '0' in trLike) {
      const first = (trLike as Record<string, unknown>)['0']
      if (first instanceof HTMLTableRowElement) return first
    }
    return null
  }

  /** 行点击高亮（对标 `table.on('row(datatable)')`，L2431-2436）。 */
  const onTableRow = (obj: { tr?: unknown }): void => {
    const tr = resolveTableTr(obj?.tr)
    if (tr === null) return
    for (const other of Array.from(
      root.querySelectorAll<HTMLTableRowElement>('.layui-table-box .layui-table-body tr.selectTr'),
    )) {
      other.classList.remove('selectTr')
    }
    tr.classList.add('selectTr')
  }
  ;(table.on as (e: string, cb: (obj: { tr?: unknown }) => void) => void)(
    'row(datatable)',
    onTableRow,
  )

  const batchSelectionOrNull = (): BatchSelection | null => {
    const rows = checkedRows()
    if (rows.length === 0) {
      msg('请选中要操作的记录')
      return null
    }
    const version = selectionVersion
    const orderNos = rows.map((row) => row.order_no)
    return {
      kind: 'batch',
      rows,
      orderNos,
      sourceOrderNos: new Map(rows.map((row) => [row.order_no, row.source_order_no])),
      isSelectionCurrent: () => {
        if (version !== selectionVersion) return false
        const current = new Set(checkedRows().map((row) => row.order_no))
        return current.size === orderNos.length && orderNos.every((no) => current.has(no))
      },
    }
  }

  const runSelectedBatch = (
    selection: BatchSelection,
    actionLabel: string,
    options: Omit<BatchFlowOptions, 'orderNos' | 'confirmation'>,
  ): Promise<boolean> => runBatchFlow({
    ...options,
    orderNos: selection.orderNos,
    confirmation: {
      actionLabel,
      sourceOrderNos: selection.sourceOrderNos,
      isSelectionCurrent: selection.isSelectionCurrent,
    },
  })

  const formValues = (): RawFilterValues => (form.val as (f: string) => RawFilterValues)('form')

  /** 表单里被勾选的 checkbox（layui 的 `form.val` 不回未勾选项，勾选项回 `'on'`）。 */
  const checkedFilterNames = (): string[] =>
    Array.from(root.querySelectorAll<HTMLInputElement>('input[type=checkbox][name]'))
      .filter((el) => el.checked && !el.closest('.layui-table-view'))
      .map((el) => el.name)

  /**
   * 首屏（用户还没动过任何控件时）的筛选值快照。
   * ⛔ 必须有它：证据件几个控件自带非空默认值（`bussType` 等 = `0`、`minPrice` = `-999999`…），
   * 只用 `filled()` 判会把默认值当「用户填过」⇒ 每次 `reload()` 都弹警告、把成功提示顶掉。
   * 今天唯一消费点 = `collectSearch` 交给 `usedNotOpenFilters`（实际在比较的只剩「档 3/4 + 主栏区间」「订单状态」）。
   * ⛔ `dateType` 不看这份快照：它在 `collectSearch` 里直接与字面量 `'10'` 比，别混。
   */
  const initialFilterValues: RawFilterValues = formValues()

  /* ⭐ 打手 / 发单人两个下拉 = **远程关键词搜索**（「已知差异 68」）：候选不再预载
   * （挂载 / 切游戏都不拉全量名单），输入关键词时 formselects 防抖请求
   * `?search=<关键词>&game=<当前游戏>`，回包即候选；空关键词不发请求（下拉只剩占位 option）。 */
  /** 批量搜单号（`layer.prompt` 收下的文本）；`null` = 没有这个条件。 */
  let batchOrderIds: string | null = null

  const popgameSelect = (): HTMLSelectElement | null =>
    root.querySelector<HTMLSelectElement>(`select#${POPGAME_SELECT_ID}`)

  const hallPublisherSelect = (): HTMLSelectElement | null =>
    root.querySelector<HTMLSelectElement>(`select#${HALL_PUBLISHER_SELECT_ID}`)

  /** 独立「发单人」下拉的当前已选值（`''` = 没选）。⚠️ 真浏览器里 formselects 把 `name` 摘到隐藏 input ⇒ 原 `<select>.value` 不再是真值，优先读 formselects、未渲染才回落 DOM。 */
  const hallPublisherValue = (): string => {
    const fromFormSelects = getFormSelects()?.value(HALL_PUBLISHER_SELECT_ID, 'valStr')
    if (typeof fromFormSelects === 'string') return fromFormSelects
    return hallPublisherSelect()?.value ?? ''
  }

  /** 清空旧「打手 / 发单人」组合控件。⚠️ 必须**同时**清 DOM value 与 formselects 内部值——只清一处会留下「看得见但仍发出去」的幽灵值。 */
  const clearLegacyPublisherSelection = (): void => {
    const select = popgameSelect()
    if (select) select.value = ''
    getFormSelects()?.value(POPGAME_SELECT_ID, [])
  }

  /** 清空独立「发单人」下拉（同上：DOM value + formselects 内部值两处一起清）。 */
  const clearHallPublisherSelection = (): void => {
    const select = hallPublisherSelect()
    if (select) select.value = ''
    getFormSelects()?.value(HALL_PUBLISHER_SELECT_ID, [])
  }

  /** 把独立「发单人」下拉重置为**仅占位 option** + 清已选 + 重渲染（切游戏时调用；候选改远程搜索后没有「名单」可写）。切游戏前已选了发单人 ⇒ 清空 + 提示重选（保留旧 id 只会筛出空集且页面看不出原因）。 */
  const applyPublisherOptions = (): void => {
    const select = hallPublisherSelect()
    if (!select) return
    const hadSelection = hallPublisherValue() !== ''
    resetFilterSelect(select, HALL_PUBLISHER_SELECT_ID)
    if (hadSelection) {
      msg(PUBLISHER_GAME_SWITCHED_TEXT)
    }
    // ⚠️ 没有已选值时不弹：那条文案是「请重选」，没东西可选时弹它只是噪音。
  }

  /** 新旧两控件指向同一个 `hall_publisher_user` ⇒ 「谁改谁生效」，表单里任何时刻最多一个值（单一出口不出现「两张嘴」）。⚠️ 旧控件不触发 layui 的 `form.on('select')` ⇒ 用 formselects 自己的 `on(id, cb)`。 */
  const bindPublisherExclusivity = (): void => {
    const fs = getFormSelects() as
      | (FormSelectsInstance & {
          on?: (id: string, cb: (...args: unknown[]) => unknown) => unknown
        })
      | undefined
    fs?.on?.(HALL_PUBLISHER_SELECT_ID, clearLegacyPublisherSelection)
    fs?.on?.(POPGAME_SELECT_ID, clearHallPublisherSelection)
  }

  /** 把 `#popgameName` 重置为**仅占位 option** + 清已选 + 重渲染（切档位 / 切游戏时调用；候选改远程搜索后没有「名单」可写）。 */
  const applyPopgameOptions = (): void => {
    const select = popgameSelect()
    if (!select) return
    resetFilterSelect(select, POPGAME_SELECT_ID)
  }

  /** 下拉重置三件套：仅占位 option + 清 DOM value 与 formselects 内部值 + 重渲染（换名单的旧路没了，候选全部来自关键词远程搜索）。⚠️ `initFormSelects()` 幂等（已渲染就跳过）⇒ 必须显式 `render(id)`；jsdom 里 formSelects 是 undefined ⇒ 那步是 no-op，option 本身仍换（页测试断言的就是 option）。 */
  const resetFilterSelect = (select: HTMLSelectElement, id: string): void => {
    select.innerHTML = ''
    appendPlaceholderOption(select)
    select.value = ''
    getFormSelects()?.value(id, [])
    getFormSelects()?.render(id)
  }

  /** 当前「游戏」筛选的 id（`null` = 没选 ⇒ 搜索请求不带 `game`）。 */
  const currentGameId = (): number | null => {
    const rawGame = formValues().gameName
    return filled(rawGame) ? Number(rawGame) : null
  }

  /** 挂载期给两个下拉接上远程关键词搜索（配置含 `data: { game }` 与 `beforeSuccess` 拆信封；`#popgameName` 的档位在**响应时刻**读——切档瞬间的在途回包不错位）。⚠️ 空关键词闸已在此前的 `primeFilterSearchGate` 预埋（必须在 render 之前，见 `filterSearchGate`）。 */
  const wireFilterSearch = (): void => {
    const game = currentGameId()
    configFilterSearch(
      POPGAME_SELECT_ID,
      buildFilterSearchConfig({
        game,
        toRows: (res) => popgameRowsFromEnvelope(res, formValues().recType ?? ''),
      }),
    )
    configFilterSearch(
      HALL_PUBLISHER_SELECT_ID,
      buildFilterSearchConfig({ game, toRows: publisherRowsFromEnvelope }),
    )
  }

  /** 切游戏 ⇒ 只更新两份搜索配置里的 `data.game`（formselects.config 深合并 ⇒ 其余键原样保留）+ 把两个下拉重置回仅占位。 */
  const updateFilterSearchData = (): void => {
    const data = buildFilterSearchData(currentGameId())
    configFilterSearch(POPGAME_SELECT_ID, { data })
    configFilterSearch(HALL_PUBLISHER_SELECT_ID, { data })
  }

  /** 取 `3 接发组合` 弹层那块 `.seachMutPriceDiv`。⚠️ **文档级优先**：`layer.open` 会把整块搬进 `.layui-layer-content`（root 之外）、`end` 再搬回——root 级查找第二次打开就取不到。 */
  function comboPricePanel(): HTMLElement | null {
    return (
      document.querySelector<HTMLElement>(`.layui-layer-content > ${COMBO_PRICE_PANEL_SELECTOR}`) ??
      root.querySelector<HTMLElement>(COMBO_PRICE_PANEL_SELECTOR)
    )
  }

  /** 把弹层那块搬回 `.layui-form`（⛔ 不搬回来的话第二次打开、以及 `form.val` 都取不到它）。 */
  function restoreComboPricePanel(node: HTMLElement): void {
    const host = root.querySelector<HTMLElement>('form.layui-form') ?? root
    if (node.parentElement !== host) host.appendChild(node)
  }

  /** 已打开的 `3 接发组合` 弹层索引；`null` = 当前没开。 */
  let comboPriceLayerIndex: number | null = null

  /**
   * 弹层那颗「搜索」的处理函数（绑定见 `bindComboPriceSearch` / `unbindComboPriceSearch`）。
   * 不合法 ⇒ `msg` + `return`（⛔ 一个请求都不发）；合法 ⇒ 关层 + 重取。
   * ⚠️ 绑在按钮**元素本身**且懒绑定（开层绑、关层摘），⛔ 不是挂载期文档级委托：
   * 结构网钉着「挂载期 document 监听恰好三个关菜单监听」；DOM 搬移时元素自身监听跟着走，绑按钮在哪儿都有效；先 remove 再 add ⇒ 幂等不叠加请求。
   */
  const onComboPriceSearchClick = (event: Event): void => {
    event.preventDefault()
    // 拦住冒泡：root 级兜底不许同时弹「尚未开放」（layer.msg 后来的会覆盖先前的）。
    event.stopPropagation()
    const node = comboPricePanel()
    const raw = COMBO_PRICE_INPUT_IDS.map(
      (id) => node?.querySelector<HTMLInputElement>(`#${id}`)?.value ?? '',
    )
    const invalid = comboPriceInvalidMessage(raw)
    if (invalid !== null) {
      msg(invalid)
      return
    }
    closeComboPriceLayer()
    reload()
  }

  /** 弹层那颗「搜索」按钮（层开着时它在 `.layui-layer-content` 里）。 */
  function comboPriceButton(): HTMLElement | null {
    return comboPricePanel()?.querySelector<HTMLElement>(COMBO_PRICE_BTN_SELECTOR) ?? null
  }

  /** 懒绑定：开层时挂在这一颗按钮**自身**上（幂等，见 `onComboPriceSearchClick` 的注释）。 */
  function bindComboPriceSearch(): void {
    const btn = comboPriceButton()
    if (!btn) return
    btn.removeEventListener('click', onComboPriceSearchClick)
    btn.addEventListener('click', onComboPriceSearchClick)
  }

  /** 关层时摘掉（⛔ 不摘 = 每开一次多挂一份；这正是「关层后再开仍要有效」那条判据钉的东西）。 */
  function unbindComboPriceSearch(): void {
    comboPriceButton()?.removeEventListener('click', onComboPriceSearchClick)
  }

  /**
   * 打开 `3 接发组合` 弹层。
   * ⛔ **不复刻对标「选到 3 就把 4 个框重置成默认值」**：重置会把用户上次填的值静默抹掉；
   * 「框里显示什么就发什么」才是判据 ⇒ 默认值只来自证据件 HTML 的 `value=`，⛔ 不写进代码。
   * ⚠️ `end` 里不再调一次 `layer.close(index)`（冗余，留着让人以为有承重作用）。
   */
  function openComboPriceLayer(): void {
    const node = comboPricePanel()
    if (!node) {
      msg('接单价+发单价组合查询弹层打不开，请刷新页面后重试')
      return
    }
    node.style.display = ''
    // 运行期再摘一次 `data-notopen`（保底不留在 DOM 上）。
    node.querySelector<HTMLElement>('[data-notopen]')?.removeAttribute('data-notopen')
    bindComboPriceSearch()
    const jq = (layui as unknown as { $: (el: Element) => unknown }).$(node)
    comboPriceLayerIndex = (layer.open as (o: unknown) => number)({
      title: COMBO_PRICE_LAYER_TITLE,
      shade: [0.3, '#333'],
      shadeClose: true,
      type: 1,
      skin: 'layui-layer-lan',
      anim: 5,
      area: ['560px', '250px'],
      content: jq,
      end: () => {
        comboPriceLayerIndex = null
        unbindComboPriceSearch()
        node.style.display = 'none'
        restoreComboPricePanel(node)
      },
    })
  }

  /** 切到别的档位 ⇒ 收掉弹层、把块隐藏并搬回表单（对标 else 支同形）。 */
  function closeComboPriceLayer(): void {
    unbindComboPriceSearch()
    const node = comboPricePanel()
    if (node) {
      node.style.display = 'none'
      restoreComboPricePanel(node)
    }
    if (comboPriceLayerIndex !== null) {
      const index = comboPriceLayerIndex
      comboPriceLayerIndex = null
      ;(layer.close as (i: number) => void)(index)
    }
  }

  /** 组装只含筛选的 `where`（页码/页长交给 layui）。⚠️ `where` 里绝不能放 `page`/`page_size`：它会被记进实例配置且优先级更高 ⇒ 翻到第 2 页仍发 `page=1`（实测踩过）；`request:{pageName,limitName}` 才是页码出口。 */
  const collectSearch = (): OrderSearchParams => {
    const values: RawFilterValues = { ...formValues() }
    // 批量搜单号来自 `layer.prompt`（不是表单控件，form.val 读不到）⇒ 在这里并进 values；真送参仍在 `toOrderSearch`（单一映射来源）。
    if (batchOrderIds !== null) values.batchOrderId = batchOrderIds
    // ⭐ 档 `3` 的 4 个金额格：无条件以 DOM 为准（`layer.open` 会把整块搬进弹层、`form.val` 读不到；
    // 块搬回来时 form.val 又读得到 ⇒ 两处各读一次会让「看到的」与「发出去的」分叉）。别的档位那 4 格与查询无关。
    if (values.showPriceType === '3') {
      const combo = comboPricePanel()
      for (const id of COMBO_PRICE_INPUT_IDS) {
        const input = combo?.querySelector<HTMLInputElement>(`#${id}`)
        if (input) values[id] = input.value
      }
    }
    const checked = checkedFilterNames()
    // 「订单状态」的映射在 `toOrderSearch` 里做；这里不替它清值，只让 `usedNotOpenFilters` 把未映射档位并进合并提示（单独再 msg 一次会被后来的 layer.msg 盖掉）。
    const used = usedNotOpenFilters(values, checked, initialFilterValues)
    if (used.length > 0) msg(notOpenMessage(used))
    if (filled(values.dateType) && values.dateType !== '10') {
      msg('「录单时间」以外的日期口径尚未开放，本次按录单时间筛选')
    }
    return toOrderSearch(values, checked)
  }

  /** 重新取数（第一参是不带 `#` 的实例 id）。⚠️ 每页条数取 layui 自己维护的**实时值**（`getOptions(id).page.limit` 是 laypage 就地写的），⛔ 别退回常量 `20`——传常量会把用户选的「条/页」顶掉（实测缺陷）。 */
  const reload = (page = 1, size?: number): void => {
    const where = collectSearch()
    lastWhere = where as Record<string, unknown>
    currentPage = page
    const liveLimit =
      (table.getOptions as
        | ((id: string) => { page?: { limit?: number } } | null)
        | undefined)?.(TABLE_ID)?.page?.limit ?? 20
    ;(table.reload as (id: string, o: unknown) => void)(TABLE_ID, {
      where,
      page: { curr: page, limit: size ?? liveLimit },
    })
  }

  /**
   * 当前页码，只用于「原地刷新」（编辑保存后 / 撤回抛单后）；其余调用点有意回到第 1 页（别改成默认值）。
   * 来源：`reload()` 自己写 + `done` 交回的 `curr`（vendored 2.13.9 里没有 `table.on('page')` 事件）。
   * ⛔ 页码不在 `where` 里（放进去会盖掉 layui 自己那份，翻页永远发 `page=1`），必须由本页显式记着。
   */
  let currentPage = 1

  /* —— 真功能 1：改状态（工具栏「改状态」/ 点状态徽标） —— */
  /**
   * 「更改订单状态」弹窗。
   * ⛔ 还有**活跃**代练通订单（`platform_push.is_active === true`）时不能手工改状态：不开弹窗、只 `msg` 一句、零请求；无活跃 ⇒ 正常开弹窗。工具栏批量同判据（含任一活跃单 ⇒ 整体拒绝并列单号，⛔ 不静默跳过）；后端 A2 闸兜底。
   * ⚠️ 弹窗里没有「当前状态」这一行（业务方裁定后整行删除）；三处有意不同（body 字号并进 form、按钮图标换对勾、`lay-submit`/`lay-filter` 换成 `data-act`）都写明了理由，别当抄漏。
   * ⛔ radio **不预选**（对标 13 个 radio 无 `checked`）：删「当前状态」行时必须同时保住这条——「永远显示待抢单中」的病根就是旧版预选第一个 radio。
   * ⛔ 对 `REFUNDED` 不再特判（后端加了回池出边 ⇒ 有 2 个可改目标，弹窗照常开）；「不给开」只剩 `allowed_transitions` 为空一种。
   */
  function openStatusDialog(
    orderNo: string,
    target: BatchTarget = { kind: 'single', orderNo },
    /** ⭐ 行数据有**活跃**平台订单（`platform_push.is_active === true`）⇒ true。 */
    pushed = false,
  ): void {
    // ⛔ 还有活跃平台订单 ⇒ 入口整体不可用（判据只看 `platform_push.is_active`，不看状态名、不看历史推送行）；后端 A2 闸兜底。批量支无单行数据，由调用侧先拦。
    if (pushed && target.kind === 'single') {
      msg(PLATFORM_STATUS_NOTICE)
      return
    }
    // 批量：可选项只能来自后端读取形态（不传 target ⇒ 只回并集）；⛔ 前端不镜像状态机，批量档由服务端 `dry_run` 判定。
    if (target.kind === 'batch') {
      void fetchBatchTransitionTargets(target.orderNos).then(
        (outcome) => {
          const targets = outcome.targets ?? []
          if (targets.length === 0) {
            msg('当前账号没有可执行的流转（职位权限可能不足）')
            return
          }
          renderStatusDialog(targets, null, target, '')
        },
        (e: unknown) => msg(errText(e)),
      )
      return
    }
    void fetchOrder(orderNo).then(
      (order) => {
        const targets = order.allowed_transitions
        if (targets.length === 0) {
          // 状态机已无 0 出边状态 ⇒ 空数组来自节点判据（边节点 ∩ 调用方节点 = ∅，fail-closed），与职位过滤无关。
          msg('当前账号没有可执行的流转（职位权限可能不足）')
          return
        }
        renderStatusDialog(targets, order, target, orderNo)
      },
      (e: unknown) => msg(errText(e)),
    )
  }

  /**
   * 渲染并打开改状态弹层（**行内单笔**与**工具栏批量**两条入口共用同一份标记与 `layer.open` 参数）。
   *
   * - `order`：单笔那一支的详情（只用来读 `completed_requires_review` 那道双保险）；批量那一支传 `null`。
   * - `target`：提交时按 `kind` 分流（批量走两步式，见 `save`）。
   * - `orderNo`：**单笔**那一支的单号（批量那一支传空串——它没有「本单」）。
   */
  function renderStatusDialog(
    targets: string[],
    order: Order | null,
    target: BatchTarget,
    orderNo: string,
  ): void {
    const body =
      `<form class="layui-form layui-form-pane" id="${STATUS_DIALOG_ID}" action="" ` +
      'style="padding-top:20px;margin-left:10px;margin-right:10px;font-size:12px">' +
      // ⚠️ 内联形态会继承本页对 label 的覆盖 ⇒ 按 id 还原成 layui 默认值（只作用本弹窗，不动页面其他表单）。
      `<style>#${STATUS_DIALOG_ID} .layui-form-label{padding:8px 15px;background-color:#fafafa}</style>` +
      // 「要改成：」+「说明原因：」两行；`.remark` 行默认 display:none（三状态时显示）；⛔「同步淘宝发货」不复刻（业务方只要说明原因行）。
      '<div class="layui-form-item">' +
      '<label class="layui-form-label">要改成：</label>' +
      '<div class="layui-input-block">' +
      targets
        .map(
          (t) =>
            `<input type="radio" name="status" value="${t}" title="${statusLabel(t)}" lay-filter="status">`,
        )
        .join('') +
      '</div>' +
      '</div>' +
      `<div class="layui-form-item ${REASON_ROW_CLASS}" style="display:none">` +
      `<label class="layui-form-label">${REASON_LABEL_TEXT}</label>` +
      '<div class="layui-input-block">' +
      `<input type="text" name="remark" id="${REASON_INPUT_ID}" autocomplete="off" ` +
      `placeholder="${REASON_PLACEHOLDER}" class="layui-input layui-input-shadow" maxlength="${REASON_MAX_LENGTH}">` +
      '</div>' +
      '</div>' +
      '<div class="layui-form-item" style="margin-top:20px">' +
      '<div class="layui-input-block">' +
      '<button class="layui-btn layui-btn-normal savebtn" type="button" data-act="status-save">' +
      '<i class="layui-icon">&#xe605;</i>确定</button>' +
      '</div>' +
      '</div>' +
      '</form>'
    // ⛔ body 必须是 `.layui-form`，且 `success` 里必须 `form.render('radio')`（踩过）：运行期插进 layer 的 radio 不手动 render 就没有文字，用户看到几个没有文字的圆圈。
    const save = (index: number, dialog: HTMLElement): void => {
      // 读值从 DOM 取（layui 包一层后 `:checked` 仍在原始 input 上）。
      const picked = dialog.querySelector<HTMLInputElement>('input[name=status]:checked')
      if (!picked) {
        msg('请选择要改变的状态')
        return
      }
      // ⛔ 双保险只对行内单笔成立（批量走服务端 dry_run）：承重门是 `order !== null`——批量支恒传 null 在这里被短路；⛔ 别再加「`target.kind === 'single'` 也缺一不可」（冗余防御）。选中「已完成」但非待验收 ⇒ 只弹提示、零请求、不关层。
      if (order !== null && picked.value === 'COMPLETED' && order.completed_requires_review) {
        ;(layer.alert as (t: string) => void)(COMPLETED_REQUIRES_REVIEW_MESSAGE)
        return
      }
      // ⛔ 三状态必填「说明原因」（必填闸在前端）：未填 ⇒ 弹提示、不关层、不发请求；值从 DOM 取并 trim。
      const reasonRequired = REASON_REQUIRED_STATUSES.has(picked.value)
      let reason = ''
      if (reasonRequired) {
        reason = (dialog.querySelector<HTMLInputElement>(`#${REASON_INPUT_ID}`)?.value ?? '').trim()
        if (reason === '') {
          msg(REASON_REQUIRED_MESSAGE)
          return
        }
      }
      // 批量 ⇒ 两步式端点：成功才关层（失败留层让用户改选）；reason 只在三状态时带上（非三状态载荷与旧形状一致）。
      if (target.kind === 'batch') {
        const label = picked.value === 'COMPLETED' ? '批量验收（改为已完成）' : `批量改状态为${statusLabel(picked.value)}`
        void runSelectedBatch(target, label, {
          action: 'transition',
          payload: reasonRequired ? { target: picked.value, reason } : { target: picked.value },
          successMessage: batchSuccessMessage('状态已更新'),
          // 注入页面现成的 `msg` ⇒ layui 加载失败那一支也弹得出来（⛔ 不再静默退到 `logError`）。
          msg: (text) => msg(text),
          onDone: () => {
            ;(layer.close as (i: number) => void)(index)
            reload()
          },
        })
        return
      }
      ;(layer.close as (i: number) => void)(index)
      void transitionOrder(
        orderNo,
        reasonRequired ? { target: picked.value, reason } : { target: picked.value },
      ).then(
        () => {
          // ⚠️ 先 `reload()` 再报成功：collectSearch 可能弹警告，`layer.msg` 后来的覆盖先前的——顺序反了用户就看不到「状态已更新」。
          reload()
          msg('状态已更新')
        },
        (e: unknown) => msg(errText(e)),
      )
    }
    ;(layer.open as (o: unknown) => void)({
      type: 1,
      title: '更改订单状态',
      area: ['1000px', '300px'],
      skin: 'layui-layer-lan',
      shade: [0.3, '#333'],
      shadeClose: true,
      anim: 5,
      content: body,
      // layui 的实参是 `(layero, index, instance)`（`layui.js` 的 `i.pt.callback`：
      // `o.success(a, n.index, n)`），且 `layero` 是 **jQuery 对象**（同处 `o.css(...)`/`outerWidth()`）。
      success: (layero: { find: (s: string) => { 0?: HTMLElement } }, index: number) => {
        ;(form.render as (type?: string) => void)('radio')
        const dialog = layero.find(`#${STATUS_DIALOG_ID}`)[0]
        const saveBtn = dialog?.querySelector<HTMLElement>('[data-act="status-save"]')
        saveBtn?.addEventListener('click', () => {
          if (dialog) save(index, dialog)
        })
        // ⭐ 选三状态 ⇒ 显示「说明原因」行并 focus；选别的档 ⇒ 隐藏（不清空已填的值）。
        // ⛔ 监听用弹窗内原生 click 委托，⛔ 不用 `form.on('radio(status)')`——那是全局注册且 layui 没有 off，每次开层都会再注册一份、回调执行多次；委托在弹窗根上，关层时 DOM 随层销毁、监听不残留。
        const reasonRow = dialog?.querySelector<HTMLElement>(`.${REASON_ROW_CLASS}`)
        const reasonInput = dialog?.querySelector<HTMLInputElement>(`#${REASON_INPUT_ID}`)
        dialog?.addEventListener('click', () => {
          if (!reasonRow || !reasonInput) return
          const picked = dialog.querySelector<HTMLInputElement>('input[name=status]:checked')
          if (picked !== null && REASON_REQUIRED_STATUSES.has(picked.value)) {
            reasonRow.style.display = ''
            reasonInput.focus()
          } else {
            reasonRow.style.display = 'none'
          }
        })
      },
    })
  }

  /* —— 真功能 2：改接单价（工具栏「改接单价」/ 点接单价） —— */
  function openPricePrompt(
    orderNo: string,
    current: string | null,
    target: BatchTarget = { kind: 'single', orderNo },
  ): void {
    ;(layer.prompt as (o: unknown) => void)({
      formType: 0,
      value: current ?? '',
      title: '更改接单价，可为0，但不能为空',
      maxlength: 12,
      btn: ['保存', '取消'],
      // ⚠️⚠️ **实测的 `layer.prompt` 回调形状**（踩过，别按记忆写）：自定义 `yes` 会覆盖掉 layui 自己把输入值传进来的实现 ⇒ 回调收不到文本；
      // 实参是 `(index, layero, instance)`——「第一参是 value」的写法拿到的是层索引；读值必须从 DOM 取 `.layui-layer-prompt .layui-layer-input`（jQuery 与原生两种形态都兜）。
      yes: (index: number, layero: { find: (s: string) => { val?: () => string; 0?: HTMLInputElement } }) => {
        const priced = layero?.find?.('.layui-layer-input')
        const raw = priced?.val ? priced.val() : (priced?.[0]?.value ?? '')
        const price = String(raw ?? '').trim()
        if (price === '' || Number.isNaN(Number(price))) {
          msg('请输入合法金额')
          return
        }
        ;(layer.close as (i: number) => void)(index)
        // 工具栏来的（批量，spec §5.2）⇒ 走两步式批量端点；⛔ 单笔那一支一字不改。
        if (target.kind === 'batch') {
          void runSelectedBatch(target, `批量改接单价为 ${price}`, {
            action: 'price',
            payload: { total_price: price },
            successMessage: batchSuccessMessage('接单价已更新'),
            // 注入页面现成的 `msg` ⇒ layui 加载失败那一支也弹得出来（⛔ 不再静默退到 `logError`）。
            msg: (text) => msg(text),
            onDone: reload,
          })
          return
        }
        // 改价**只能走 `/price/`**：PATCH 载荷里的金额会被后端静默忽略（见 `api/orders.ts`）。
        void changePrice(orderNo, { total_price: price }).then(
          () => {
            // 顺序理由同改状态：先 reload（可能弹筛选警告）再报成功。
            reload()
            msg('接单价已更新')
          },
          (e: unknown) => msg(errText(e)),
        )
      },
    })
  }

  /* —— 真功能 2.6：改发单价（工具栏「改发单价」/ 发单价列两支） —— */
  /**
   * 改发单价的 prompt（`maxlength` 取 10，别抄成接单价那支的 12；不设 `btn` ⇒ 回调是 layui 默认 `yes` 调的 `(value, index, layero)`，直接收 value）。
   * ⛔ 不在前端换算 `%`、不设上限：换算在服务端，前端镜像会漂移；空值这条是客户端「不能为空」。
   */
  function openHallPricePrompt(
    orderNo: string,
    fee: string | null,
    title: string,
    target: BatchTarget = { kind: 'single', orderNo },
  ): void {
    ;(layer.prompt as (o: unknown, cb?: unknown) => void)(
      {
        formType: 0,
        value: fee ?? '',
        title,
        maxlength: 10,
      },
      (value: string, index: number) => {
        const price = String(value ?? '').trim()
        if (price === '') {
          msg('发单价不能为空')
          return
        }
        // 工具栏来的（批量，spec §5.2）⇒ 两步式批量端点；⛔ 单笔那一支一字不改。
        // 关层时机照单笔那一支（**成功才关**，失败留层让用户改完重试）。
        if (target.kind === 'batch') {
          void runSelectedBatch(target, `批量改发单价为 ${price}`, {
            action: 'hall-price',
            payload: { price },
            successMessage: batchSuccessMessage('发单价已更新'),
            // 注入页面现成的 `msg` ⇒ layui 加载失败那一支也弹得出来（⛔ 不再静默退到 `logError`）。
            msg: (text) => msg(text),
            onDone: () => {
              ;(layer.close as (i: number) => void)(index)
              reload()
            },
          })
          return
        }
        void changeHallPrice(orderNo, { price }).then(
          () => {
            // 成功 ⇒ 关层 + reload + msg；失败 ⇒ 只弹后端 msg、**不关层**（spec §7.4）。
            ;(layer.close as (i: number) => void)(index)
            // 顺序同改价：先 reload（可能弹筛选警告）再报成功。
            reload()
            msg('发单价已更新')
          },
          (e: unknown) => msg(errText(e)),
        )
      },
    )
  }

  /* —— 真功能 2.7：改收款状态（工具栏「收款状态」/ 点接单价列的标记 `.jsSta`） —— */

  /** 弹层层索引与当前单号（`end` 里清空；与发价 / 退款 / 改类型三个弹层同形）。 */
  let settLayerIndex: number | null = null
  let settOrderNo = ''
  /** 目标描述（spec §D11）：工具栏＝批量、行内＝单笔。`null` = 弹层没开。 */
  let settTarget: BatchTarget | null = null
  let settSaveBound = false

  /** 取「收款状态」弹层节点（`.setDiv`）。⚠️ **文档级优先**：`layer.open` 会把节点搬进 `.layui-layer-content`、`end` 再搬回——root 级查找第二次打开就取不到、提交时也会读到脱开文档的旧节点。 */
  function settPanel(): HTMLElement | null {
    return (
      document.querySelector<HTMLElement>('.layui-layer-content > .setDiv') ??
      root.querySelector<HTMLElement>('.setDiv')
    )
  }

  /** 保存按钮在弹层里、`.layui-layer` 挂在 `document.body` ⇒ click 冒泡不到 `root`：用文档级捕获接管，且只在弹层打开期间挂上。 */
  const onSettSaveClick = (event: Event): void => {
    const btn = (event.target as HTMLElement).closest<HTMLElement>('[lay-filter="saveSettbtn"]')
    if (!btn) return
    event.preventDefault()
    event.stopPropagation()
    submitPaymentStatus()
  }

  /** 提交收款状态：中文 value → 码只走 `PAYMENT_STATUS_BY_BENCHMARK_VALUE`（一处定义）；未选 ⇒ 逐字弹「请选择一种结算状态！」；成功 ⇒ 关弹窗+reload+msg，失败 ⇒ 只弹后端 msg、弹窗不关。 */
  function submitPaymentStatus(): void {
    const node = settPanel()
    const picked = node?.querySelector<HTMLInputElement>('input[name="jsst"]:checked') ?? null
    if (picked === null) {
      ;(layer.alert as (t: string, o: unknown) => void)('请选择一种结算状态！', { title: '提示' })
      return
    }
    const code = PAYMENT_STATUS_BY_BENCHMARK_VALUE[picked.value]
    // 映射不到只可能是标记被改过 ⇒ 不猜码、不发请求（弹窗没开同理）。
    const target = settTarget
    if (code === undefined || target === null) return
    // 工具栏来的（批量，spec §5.2）⇒ 两步式批量端点；⛔ 单笔那一支一字不改。
    // 关层时机照单笔那一支（**成功才关**，失败留层让用户改完重试）。
    if (target.kind === 'batch') {
      void runSelectedBatch(target, '批量改收款状态', {
        action: 'payment-status',
        payload: { payment_status: code },
        successMessage: batchSuccessMessage('收款状态已更新'),
        // 注入页面现成的 `msg` ⇒ layui 加载失败那一支也弹得出来（⛔ 不再静默退到 `logError`）。
        msg: (text) => msg(text),
        onDone: () => {
          if (settLayerIndex !== null) (layer.close as (i: number) => void)(settLayerIndex)
          reload()
        },
      })
      return
    }
    if (settOrderNo === '') return
    const index = settLayerIndex
    void changePaymentStatus(settOrderNo, { payment_status: code }).then(
      () => {
        if (index !== null) (layer.close as (i: number) => void)(index)
        reload()
        msg('收款状态已更新')
      },
      (e: unknown) => msg(errText(e)),
    )
  }

  /**
   * 「收款状态」弹窗（`.setDiv` 开层）。
   * ⚠️ 保存按钮的 `data-notopen` 运行期也摘一次（属性被谁加回来时不静默）。
   * ⛔ 预勾选必须在 `layer.open` **之前**：搬进 `.layui-layer-content` 后节点已不在 `.layui-form` 里，那时 `form.render('radio')` 够不到它。
   */
  function openPaymentStatusDialog(
    orderNo: string,
    current: string,
    target: BatchTarget = { kind: 'single', orderNo },
  ): void {
    const node = settPanel()
    if (!node) {
      msg('收款状态弹层打不开，请刷新页面后重试')
      return
    }
    settOrderNo = orderNo
    settTarget = target
    node.style.display = ''
    node.querySelector<HTMLElement>('[data-notopen]')?.removeAttribute('data-notopen')
    // 用该行当前的码预勾选（中文 → 码的**反向**查表：按映射表逐项比，不另建一张码→中文表）。
    for (const radio of Array.from(node.querySelectorAll<HTMLInputElement>('input[name="jsst"]'))) {
      radio.checked = PAYMENT_STATUS_BY_BENCHMARK_VALUE[radio.value] === current
    }
    ;(form.render as (t?: string) => void)('radio')
    const host = root.querySelector<HTMLElement>('form.layui-form') ?? root
    const jq = (layui as unknown as { $: (el: Element) => unknown }).$(node)
    if (!settSaveBound) {
      document.addEventListener('click', onSettSaveClick, true)
      settSaveBound = true
    }
    settLayerIndex = (layer.open as (o: unknown) => number)({
      title: '更改与发单方的结算状态',
      skin: 'layui-layer-lan',
      shade: [0.3, '#333'],
      shadeClose: true,
      type: 1,
      anim: 5,
      area: ['70%', '260px'],
      content: jq,
      success: () => {
        ;(form.render as (t?: string) => void)('radio')
      },
      end: () => {
        settLayerIndex = null
        settOrderNo = ''
        settTarget = null
        node.style.display = 'none'
        if (node.parentElement !== host) host.appendChild(node)
        if (settSaveBound) {
          document.removeEventListener('click', onSettSaveClick, true)
          settSaveBound = false
        }
      },
    })
  }

  /* —— 真功能 2.5：改「重要备注」/「角色名」（两个行内 `<a>` → 既有 PATCH 出口） —— */

  /**
   * 「重要备注」/「角色名」共用的编辑动作：`layer.prompt` → `updateOrder(no, 单键载荷)`（走既有 PATCH 出口）。
   * ⚠️ `layer.prompt` 回调收 `(value, index, layero)`；⛔ 别在 opts 里传 `yes`——那会覆盖 layui 把输入值传出来的实现（同 `openPricePrompt` 的坑）。
   * 顺序有意为 close → reload → msg（reload 可能弹筛选警告，msg 后来的覆盖先前的）；失败只弹后端 msg、不关 prompt。
   */
  function openEditPrompt(
    orderNo: string,
    current: string,
    title: string,
    maxlength: number,
    okText: string,
    buildPayload: (value: string) => Partial<UpdateOrderPayload>,
  ): void {
    ;(layer.prompt as (o: unknown, cb?: unknown) => void)(
      { formType: 0, value: current, title, maxlength },
      (value: string, index: number) => {
        void updateOrder(orderNo, buildPayload(value)).then(
          () => {
            ;(layer.close as (i: number) => void)(index)
            reload()
            msg(okText)
          },
          (e: unknown) => msg(errText(e)),
        )
      },
    )
  }

  /** 「重要备注」列那条 `<a.orderRemark>` 的点击动作（标题 / 上限照对标 L4642）。 */
  function openRemarkPrompt(orderNo: string, current: string): void {
    openEditPrompt(
      orderNo,
      current,
      REMARK_PROMPT_TITLE,
      REMARK_MAX_LENGTH,
      REMARK_SUCCESS_MESSAGE,
      (remark) => ({ remark }),
    )
  }

  /** 「角色名」列那条 `<a.changeRoleName>` 的点击动作（标题 / 上限照对标 L4232）。 */
  function openRoleNamePrompt(orderNo: string, current: string): void {
    openEditPrompt(
      orderNo,
      current,
      ROLE_NAME_PROMPT_TITLE,
      ROLE_NAME_MAX_LENGTH,
      ROLE_NAME_SUCCESS_MESSAGE,
      (game_role_name) => ({ game_role_name }),
    )
  }

  /** 「标记」徽标点击动作：走既有 `PATCH /orders/<no>/` 的 `tag` 键（无专用端点），失败只弹后端 msg、不关 prompt。 */
  function openTagPrompt(orderNo: string, current: string): void {
    openEditPrompt(
      orderNo,
      current,
      ORDER_TAG_PROMPT_TITLE,
      ORDER_TAG_MAX_LENGTH,
      ORDER_TAG_SUCCESS_MESSAGE,
      (tag) => ({ tag }),
    )
  }

  /** 代练/陪玩内容格点击动作：写走既有 `PATCH /orders/<no>/` 的 `project_text` 键，⛔ 不另造端点。 */
  function openProjectPrompt(orderNo: string, current: string): void {
    openEditPrompt(
      orderNo,
      current,
      PROJECT_PROMPT_TITLE,
      PROJECT_MAX_LENGTH,
      PROJECT_SUCCESS_MESSAGE,
      (project_text) => ({ project_text }),
    )
  }

  /* —— 真功能 3：编辑订单（对标是一个 95%×95% 的 iframe 大弹窗） —— */

  /** 编辑弹窗的 layer 索引（关闭时用）。`end` 回调把它复位，避免关过之后拿到一个悬空索引。 */
  let editLayerIndex: number | null = null

  /**
   * 「保存成功」消息的落地：关弹窗 + 提示 + 刷新列表（弹窗是 `type:2` iframe，保存按钮在子页面里 ⇒ 子页面 postMessage 通知本页，消息类型两端同一份定义）。
   * ⚠️ 顺序先 `reload()` 再 `msg()`（reload 可能弹筛选警告，`layer.msg` 后来的覆盖先前的）；页码取 `currentPage`（编辑保存后 / 撤回抛单后保留页码，其余 reload 回第 1 页）。
   */
  const onOrderSavedMessage = (event: MessageEvent): void => {
    if (event.origin !== window.location.origin) return
    const data = event.data as { type?: string; msg?: unknown } | undefined
    if (!data || data.type !== ORDER_SAVED_MESSAGE_TYPE) return
    if (editLayerIndex !== null) {
      ;(layer.close as (i: number) => void)(editLayerIndex)
      editLayerIndex = null
    }
    reload(currentPage)
    // ⭐ H1：优先原样显示后端成功信封的 `msg`（保存成功可能顺手撤代练通订单，撤不掉的警告只在 msg 里 ⇒ 带「未能撤回」走 `layer.alert` 人工确认）；判定只在 `@/config/backendMessage` 一处。
    const backendMsg = typeof data.msg === 'string' ? data.msg : ''
    showBackendSuccessMessage(
      backendMessageActions,
      backendMsg,
      EDIT_SAVE_SUCCESS_MESSAGE,
    )
  }

  window.addEventListener('message', onOrderSavedMessage)

  /* ── 发单页（order-pushjdp）发单成功 → 关弹窗 + 刷新列表 + 提示 ── */

  /** 发单弹层的 layer 索引（`openPushJdpLayer` 打开时记下，`end` 里复位）。 */
  let pushJdpLayerIndex: number | null = null

  /** 发单成功 postMessage 收口（同源 + type 白名单；先关窗、再刷新、最后提示——顺序反了筛选警告会盖掉提示）。 */
  const onPushJdpSentMessage = (event: MessageEvent): void => {
    if (event.origin !== window.location.origin) return
    const data = event.data as { type?: string } | undefined
    if (!data || data.type !== PUSH_JDP_SENT_MESSAGE_TYPE) return
    if (pushJdpLayerIndex !== null) {
      ;(layer.close as (i: number) => void)(pushJdpLayerIndex)
      pushJdpLayerIndex = null
    }
    reload(currentPage)
    msg(PUSH_JDP_SENT_TEXT)
  }

  window.addEventListener('message', onPushJdpSentMessage)

  /** 编辑订单大弹窗（`type:2` iframe 装我们自己的录单页 order-addorder，表单渲染与校验复用那一份；编辑态回填/保存/成功 postMessage 由录单页处理）。 */
  function openEditDialog(orderNo: string, viewNo: string): void {
    if (orderNo === '') return
    // 「进入编辑订单页面」的留痕（前端上报腿）：在打开编辑弹窗这一刻上报；可被绕过/伪造，失败不阻塞业务（静默包装）。
    reportOrderOpenEditQuietly(orderNo)
    const base = API_BASE.replace(/\/$/, '')
    const index = (layer.open as (o: unknown) => unknown)({
      // ⛔ 标题前缀（展示单号）是自由文本 ⇒ dataset 已把 templet 那层转义解码回原文，这里再转一次。
      title: `${escapeHtml(viewNo !== '' ? viewNo : orderNo)}编辑订单`,
      shade: [0.3, '#333'],
      shadeClose: true,
      maxmin: true,
      type: 2,
      skin: 'layui-layer-lan',
      anim: 8,
      area: ['95%', '95%'],
      content: `${base}/layui-island/page.html?page=order-addorder&order_no=${encodeURIComponent(orderNo)}`,
      end: () => {
        editLayerIndex = null
      },
    })
    editLayerIndex = typeof index === 'number' ? index : null
  }

  /**
   * 一键发单弹层（`type:2` iframe 装岛页 order-pushjdp）。
   * ⭐ 页内提交已接真 ⇒ `order_no` 是唯一寻址值；⛔ 标题里的 `viewNo`（展示单号）只用于显示，绝不进请求。
   * ⛔ `title` 是 HTML 上下文：dataset 解码回原文 ⇒ 这里再转义一次。
   * ⛔ 开层前不加 `/^\d{20}$/` 形状闸（单号为空则不开层即可）。
   */
  function openPushJdpLayer(orderNo: string, viewNo: string): void {
    if (orderNo === '') return
    const base = API_BASE.replace(/\/$/, '')
    const index = (layer.open as (o: unknown) => unknown)({
      title: `${escapeHtml(viewNo !== '' ? viewNo : orderNo)}发单到交易平台：请确认发单内容`,
      shade: [0.3, '#333'],
      shadeClose: true,
      maxmin: true,
      type: 2,
      skin: 'layui-layer-lan',
      anim: 8,
      area: ['1280px', pushJdpLayerHeight()],
      content:
        `${base}/layui-island/page.html?page=order-pushjdp&order_no=${encodeURIComponent(orderNo)}`,
      // 用户自己关掉弹窗 / 发单成功由本页关掉时都复位索引（同 `editLayerIndex` 的 `end`）。
      end: () => {
        pushJdpLayerIndex = null
      },
    })
    pushJdpLayerIndex = typeof index === 'number' ? index : null
  }

  /* —— 真功能 4：发到内部接单大厅（发价弹层）/ 从内部接单大厅撤回 —— */

  /** 发价弹层：装的是证据件里那个 `.setprice` 节点本身（字段只有一处定义）。 */
  let throwLayerIndex: number | null = null
  /** 弹层打开期间是否已挂上文档级保存按钮委托（避免重复 add/remove）。 */
  let throwSaveBound = false

  /** 取弹层节点。⚠️ `layer.open` 会把节点搬进 `.layui-layer-content`、关闭再搬回 ⇒ 查找必须文档级优先，否则第二次打开就取不到。 */
  function throwPanel(): HTMLElement | null {
    return (
      document.querySelector<HTMLElement>('.layui-layer-content > .setprice') ??
      root.querySelector<HTMLElement>('.setprice')
    )
  }

  /** 弹层字段复位。⚠️ `#setprice` 置空、不照抄对标的 `'0'`——我们载荷没有 `recFee`，留空与证据件静态 `value=""` 一致（0 与负数都是有意义金额边界，别编）。 */
  function resetThrowPanel(node: HTMLElement): void {
    const setPrice = node.querySelector<HTMLInputElement>('#setprice')
    const setBPrice = node.querySelector<HTMLInputElement>('#setbprice')
    const setId = node.querySelector<HTMLInputElement>('#setorderid')
    if (setPrice) setPrice.value = ''
    if (setBPrice) setBPrice.value = '0'
    if (setId) setId.value = ''
    for (const r of Array.from(
      node.querySelectorAll<HTMLInputElement>('input[name="pushPriceMode"]'),
    )) {
      r.checked = r.value === '10'
    }
    ;(form.render as (t?: string) => void)('radio')
  }

  /** 发价弹层「保存」在弹层里 ⇒ click 冒泡不到 `root`，用文档级捕获接管（先于 layui 的 `*[lay-submit]` 委托与本页 `onNotOpenClick`）。⚠️ 只在弹层打开期间挂上、`end` 摘掉——常驻会让每份实例都收到同一次点击、卸载后仍持闭包。 */
  const onThrowSaveClick = (event: Event): void => {
    const btn = (event.target as HTMLElement).closest<HTMLElement>(
      '[lay-filter="saveSettpricebtn"]',
    )
    if (!btn) return
    event.preventDefault()
    event.stopPropagation()
    submitThrow()
  }

  function openThrowDialog(orderNo: string): void {
    const node = throwPanel()
    if (!node) {
      msg('发价弹层打不开，请刷新页面后重试')
      return
    }
    resetThrowPanel(node)
    const setId = node.querySelector<HTMLInputElement>('#setorderid')
    if (setId) setId.value = orderNo
    const host = root.querySelector<HTMLElement>('form.layui-form') ?? root
    const jq = (layui as unknown as { $: (el: Element) => unknown }).$(node)
    if (!throwSaveBound) {
      document.addEventListener('click', onThrowSaveClick, true)
      throwSaveBound = true
    }
    throwLayerIndex = (layer.open as (o: unknown) => number)({
      title: THROW_DIALOG_TITLE,
      shade: [0.3, '#333'],
      shadeClose: true,
      type: 1,
      skin: 'layui-layer-lan',
      anim: 5,
      area: ['510px', '350px'],
      content: jq,
      end: () => {
        throwLayerIndex = null
        node.style.display = 'none'
        if (node.parentElement !== host) host.appendChild(node)
        if (throwSaveBound) {
          document.removeEventListener('click', onThrowSaveClick, true)
          throwSaveBound = false
        }
        resetThrowPanel(node)
      },
    })
  }

  /**
   * 保存（发价弹层）。
   * ⛔ 「空」判据是 `isBlankLikePython`（Python 空白集，不是 JS `.trim()`——JS 多剥 U+FEFF，会把后端按 Python 口径拒掉的值放行）；判空后不再做前端裁剪，原样交后端 strip。
   * ⛔ 「保证金是不是 0」按**数值**判（`'0.00'`/`'00'` 用字符串相等会误判成非零）；`'0'` 是合法金额不是空值。
   * ⚠️ 「百分比模式」与「最低保证金」两控件照渲染、用到时提示尚未开放、不提交（不置灰、不假装成功）。
   */
  function submitThrow(): void {
    const node = throwPanel()
    if (!node) return
    const orderNo = node.querySelector<HTMLInputElement>('#setorderid')?.value ?? ''
    const price = node.querySelector<HTMLInputElement>('#setprice')?.value ?? ''
    const bprice = node.querySelector<HTMLInputElement>('#setbprice')?.value ?? ''
    const mode =
      node.querySelector<HTMLInputElement>('input[name="pushPriceMode"]:checked')?.value ?? '10'
    if (isBlankLikePython(price) || isBlankLikePython(bprice)) {
      msg('请填写金额和保证金，默认为0')
      return
    }
    // 格式判据用项目自己的金额校验（与后端 DecimalField 的差异已枚举），提示用原文。
    if (!validateMoneyInput(price).ok) {
      msg(mode === '20' ? '请输入正确的百分比数值(不需要带%)' : '请输入正确的金额，默认为0')
      return
    }
    if (!validateMoneyInput(bprice).ok) {
      msg('请输入正确的金额，填0代表不限制')
      return
    }
    if (mode === '20') {
      notOpen('按接单价的百分比设定')
      return
    }
    if (Number(bprice) !== 0) {
      notOpen('用户最低缴纳保证金')
      return
    }
    void throwToHall(orderNo, price).then(
      () => {
        if (throwLayerIndex !== null) {
          ;(layer.close as (i: number) => void)(throwLayerIndex)
          throwLayerIndex = null
        }
        // 顺序：先 `reload()`（可能弹「尚未开放筛选」警告）再报成功（同本文件改状态/改价/编辑保存的口径）。
        reload()
        msg('已发到内部接单大厅')
      },
      // 失败**不关弹层**；msg 是后端原文——不单开 403 分支、不改写文案。
      (e: unknown) => msg(errText(e)),
    )
  }

  /** 撤回抛单：`layer.confirm` 确认 → `withdrawThrow`；刷新用 `reload(currentPage)`（保留当前页）。 */
  function withdrawFromHall(orderNo: string): void {
    ;(layer.confirm as (t: string, cb: (index: number) => void) => void)(
      WITHDRAW_CONFIRM_TEXT,
      (index: number) => {
        ;(layer.close as (i: number) => void)(index)
        void withdrawThrow(orderNo).then(
          () => {
            reload(currentPage)
            msg('已从内部接单大厅撤回')
          },
          (e: unknown) => msg(errText(e)),
        )
      },
    )
  }

  /* —— 真功能 5：记退款（工具栏「记退款」/ 点「退款」列的金额链接） —— */

  /**
   * 「记录订单退款」弹层（装证据件里的 `.setrefundOrderLog` 节点本身，字段只有一处定义；工具栏与「退款」列金额链接两个入口开同一个弹层）。
   * ⛔ 预填口径（D-0312）：行内单笔把该行当前 `refund_fee`（data-fee）预填进框——「累计」口径下预填当前累计正是「改小/改回去」的入口；批量入口一律开空。
   * ⛔ 预填值只来自列表载荷；不从本地记账推算，也不在成功后把金额累加到本地行。
   */
  let refundLayerIndex: number | null = null
  /** 本次弹窗的幂等键：**打开时生成一次**、弹窗内重复提交**复用同一个**（关掉再开才换新）。 */
  let refundRequestId = ''
  /** 本次弹窗对应的订单号（提交时用；两个入口都写它）。 */
  let refundOrderNo = ''
  /** 目标描述（spec §D11）：工具栏＝批量、行内＝单笔。`null` = 弹层没开。 */
  let refundTarget: BatchTarget | null = null
  /**
   * 批量：**单号 → 幂等键**（spec §D13）——开弹层时按 `orderNos` 各生成一个
   * （与单笔「开一次弹层一个键」同一生命周期）。执行时按合格子集过滤 ⇒ `request_ids`
   * 与 `order_nos` **等长且同序**（后端会拒不等长）。
   */
  let refundBatchRequestIds = new Map<string, string>()
  /** 弹层打开期间是否已挂上文档级保存按钮委托（避免重复 add/remove）。 */
  let refundSaveBound = false

  /** 取弹层节点。⚠️ layui 打开弹层时会把节点**搬进 `.layui-layer-content`**（`root` 之外），
   *  所以查找必须**文档级优先**，否则第二次打开就取不到节点（同 `throwPanel` 的实测现场）。 */
  function refundPanel(): HTMLElement | null {
    return (
      document.querySelector<HTMLElement>('.layui-layer-content > .setrefundOrderLog') ??
      root.querySelector<HTMLElement>('.setrefundOrderLog')
    )
  }

  /** 弹窗字段复位（对标 `form.val("form", {refundFee: yfee, …})`）：
   *  金额框按 `prefill` 填（**只**行内单笔入口给值、批量入口/关层复位传空）、备注清空。 */
  function resetRefundPanel(node: HTMLElement, prefill = ''): void {
    const fee = node.querySelector<HTMLInputElement>('#refundFee')
    const remark = node.querySelector<HTMLInputElement>('#refundRemark')
    if (fee) fee.value = prefill
    if (remark) remark.value = ''
  }

  /** 批量口径说明的渲染/清除：只在 `orderNos.length > 1` 时渲染一句；⛔ 单笔一个字都不加；每次开弹层先清旧的（批量→关→单笔不残留）。落点紧跟既有那句 `.layui-elem-quote`，形状取值照它，⛔ 不新造配色。 */
  function renderRefundBatchNote(node: HTMLElement, orderNos: string[]): void {
    node.querySelector(`[${BATCH_REFUND_NOTE_ATTR}]`)?.remove()
    if (orderNos.length <= 1) return
    const note = document.createElement('blockquote')
    note.className = 'layui-elem-quote'
    note.setAttribute('style', BATCH_REFUND_NOTE_STYLE)
    note.setAttribute(BATCH_REFUND_NOTE_ATTR, '')
    note.textContent = BATCH_REFUND_NOTE_TEXT
    const quote = node.querySelector('blockquote.layui-elem-quote')
    const parent = quote?.parentElement
    if (quote && parent) parent.insertBefore(note, quote.nextSibling)
    else node.prepend(note)
  }

  /** 关掉退款弹层（`refundLayerIndex` 为 `null` 时是 no-op；状态清理在 `layer.open` 的 `end` 里）。 */
  function closeRefundDialog(): void {
    if (refundLayerIndex !== null) {
      ;(layer.close as (i: number) => void)(refundLayerIndex)
      refundLayerIndex = null
    }
  }

  /** 弹层里的「确定记录」在 `.layui-layer-content` 里 ⇒ click 冒泡不到 `root`：文档级捕获接管（同发价弹层），只在弹层打开期间挂上。 */
  const onRefundSaveClick = (event: Event): void => {
    const btn = (event.target as HTMLElement).closest<HTMLElement>('.saveReFundOrderBtn')
    if (!btn) return
    event.preventDefault()
    event.stopPropagation()
    submitRefund()
  }

  function openRefundDialog(
    orderNo: string,
    target: BatchTarget = { kind: 'single', orderNo },
    /** 行内单笔入口传该行当前 `refund_fee`；批量入口传 `null`（开空）。 */
    prefill: string | null = null,
  ): void {
    // 行内单笔入口照旧要求单号非空；工具栏批量那一支没有「本单」，显式传 `''` + `{kind:'batch'}`。
    if (target.kind === 'single' && orderNo === '') return
    const node = refundPanel()
    if (!node) {
      msg('退款弹层打不开，请刷新页面后重试')
      return
    }
    resetRefundPanel(node, prefill ?? '')
    refundTarget = target
    refundOrderNo = orderNo
    // ⛔ 幂等键只在打开时生成：同弹窗内重试复用同一个（1010 兜底不会产生第二笔）；关掉再开是新键（= 新的一笔）。批量每笔各一个键，`request_ids` 与合格子集等长且同序（后端会拒不等长）。
    if (target.kind === 'batch') {
      refundRequestId = ''
      refundBatchRequestIds = new Map(
        target.orderNos.map((no) => [no, newRefundRequestId()] as const),
      )
    } else {
      refundRequestId = newRefundRequestId()
      refundBatchRequestIds = new Map()
    }
    // 勾 >1 笔时多渲染一句口径说明（⛔ 单笔一个字都不加）。
    renderRefundBatchNote(node, target.kind === 'batch' ? target.orderNos : [])
    const host = root.querySelector<HTMLElement>('form.layui-form') ?? root
    const jq = (layui as unknown as { $: (el: Element) => unknown }).$(node)
    if (!refundSaveBound) {
      document.addEventListener('click', onRefundSaveClick, true)
      refundSaveBound = true
    }
    refundLayerIndex = (layer.open as (o: unknown) => number)({
      title: REFUND_DIALOG_TITLE,
      shade: [0.3, '#333'],
      shadeClose: true,
      type: 1,
      skin: 'layui-layer-lan',
      anim: 5,
      // 对标 `area: ['720px', '600px']`（L3908，无桌面/移动分支）。
      area: ['720px', '600px'],
      content: jq,
      end: () => {
        refundLayerIndex = null
        refundOrderNo = ''
        refundRequestId = ''
        refundTarget = null
        refundBatchRequestIds = new Map()
        node.style.display = 'none'
        if (node.parentElement !== host) host.appendChild(node)
        if (refundSaveBound) {
          document.removeEventListener('click', onRefundSaveClick, true)
          refundSaveBound = false
        }
        resetRefundPanel(node)
        // 批量口径说明是**运行期自建节点** ⇒ 关层时清掉（下次开层按新的目标重渲）。
        renderRefundBatchNote(node, [])
      },
    })
  }

  /**
   * 提交记退款。三条判据：
   * ⛔ 金额形态：空/非数字/超量级 ⇒ 用原文提示、一次请求都不发（判据 `isBlankLikePython`+`validateMoneyInput`，提交用户原始输入、前端不裁剪）。
   * ⛔ `refund_total >= 0` 且 `<= 订单总价` 由后端判（现判**新累计值**，D-0312）：`0` 形态合法（=清空）照发；负数照发、后端回 `1008`。
   * ⛔ 备注必填、≤255 由后端判 ⇒ 前端不自建校验，空备注照发、后端 msg 原样弹。
   * 成功：关弹窗 → `reload(currentPage)`（保持当前页与筛选）→ 提示；差额为 0 也走这条（后端回 `{changed:false}`，前端不区分）。
   * ⛔ 失败分两类：`1010`（幂等键重复，服务端已落账）⇒ 提示+刷新+关弹窗（留着同键只会再拿 1010）；其余 ⇒ 只弹后端 msg 原文、不关弹窗不重取。
   * ⚠️ 1010 那条保证只覆盖「弹窗不关、键不换」的重试——响应丢失后手动关掉重开就是新键、可能真退第二笔。
   */
  function submitRefund(): void {
    const node = refundPanel()
    if (!node) return
    const refundTotal = node.querySelector<HTMLInputElement>('#refundFee')?.value ?? ''
    const reason = node.querySelector<HTMLInputElement>('#refundRemark')?.value ?? ''
    if (isBlankLikePython(refundTotal) || !validateMoneyInput(refundTotal).ok) {
      msg(REFUND_INVALID_AMOUNT_MESSAGE)
      return
    }
    // 工具栏来的（批量，spec §D13）⇒ 两步式批量端点；⛔ 单笔那一支一字不改（含 `1010` 的单独处置）。
    const target = refundTarget
    if (target === null) return
    if (target.kind === 'batch') {
      // 两个列表**一起**按合格子集过滤（`order_nos` 由 `runBatchFlow` 收窄成 `eligible`）
      // ⇒ `request_ids` 与它等长且同序（后端会拒不等长）。
      const idsFor = (orderNos: string[]): string[] =>
        orderNos.map((no) => refundBatchRequestIds.get(no) ?? '')
      void runSelectedBatch(target, `批量记退款，累计退款额 ${refundTotal}`, {
        action: 'refunds',
        payload: {
          refund_total: refundTotal,
          reason,
          request_ids: idsFor(target.orderNos),
        },
        applyPayload: (eligible) => ({
          refund_total: refundTotal,
          reason,
          request_ids: idsFor(eligible),
        }),
        successMessage: batchSuccessMessage(REFUND_SUCCESS_MESSAGE),
        // 注入页面现成的 `msg` ⇒ layui 加载失败那一支也弹得出来（⛔ 不再静默退到 `logError`）。
        msg: (text) => msg(text),
        // 关层时机照单笔那一支（**成功才关**、失败留层让用户改完重试），顺序同单笔：
        // 关弹窗 → `reload(currentPage)` → `msg`（`runBatchFlow` 保证 `onDone` 先于 `msg`）。
        onDone: () => {
          closeRefundDialog()
          reload(currentPage)
        },
      })
      return
    }
    void partialRefund(refundOrderNo, {
      refund_total: refundTotal,
      reason,
      request_id: refundRequestId,
    }).then(
      () => {
        closeRefundDialog()
        reload(currentPage)
        msg(REFUND_SUCCESS_MESSAGE)
      },
      (e: unknown) => {
        // `1010` 与其余失败码**分开处置**：这一笔在服务端已落账，留着同键只会再拿 `1010`
        // ⇒ 提示 + 刷新 + 关弹窗（顺序同成功路径）。msg 仍是后端原文（含 403 的
        // 「你没有订单退款权限」）。
        if (e instanceof BizError && e.code === REFUND_DUPLICATE_REQUEST_CODE) {
          closeRefundDialog()
          reload(currentPage)
        }
        msg(errText(e))
      },
    )
  }

  /* —— T7：发单价列第三支 → 「查看或编辑订单接手绩效」页内弹层（spec §7.2 / §7.3） —— */

  /**
   * 当前打开的「绩效视图」重载器（指派弹层的账单表 / 绩效弹层各注册一个，关层时注销）。
   * 改金额或软删成功后要把**所有**开着的视图刷新（对标两处各自 reload 自己的那张表）。
   */
  const perfViewReloaders = new Set<() => void>()
  const refreshPerfViews = (): void => {
    for (const reloadView of perfViewReloaders) reloadView()
  }

  /** 改一个金额格的出口（绩效弹层行内编辑与铅笔两路共用）。载荷 = `{amount_kind, amount}`；⛔ 不送 `reason`（后端可选，U1 判据按「body 恰好两个键」断言）。成功 ⇒ 刷新所有开着的绩效视图；返回值 = 本次写是否成功（行内编辑据此恢复原格）。 */
  const submitPerformanceAmount = (
    id: number,
    kind: PerformanceAmountKind,
    value: string,
  ): Promise<boolean> =>
    adjustPerformanceAmount(id, { amount_kind: kind, amount: value }).then(
      () => {
        refreshPerfViews()
        reload()
        return true
      },
      (e: unknown) => {
        msg(errText(e))
        return false
      },
    )

  /**
   * 垃圾桶：**软删该绩效行**（该行就是当前接单人那条时，服务端同一步清接手人；spec §4.5）。
   * 一处实现——绩效弹层与指派弹层的账单表都调它。
   */
  const deletePerformanceRow = (id: number): Promise<void> =>
    deletePerformance(id).then(
      () => {
        refreshPerfViews()
        reload()
        msg(PERF_DELETE_SUCCESS)
      },
      (e: unknown) => msg(errText(e)),
    )

  /** 改金额的 `layer.prompt`（铅笔那条路；与行内编辑两条路并存）：预填原值、金额形态校验、同值守卫，交给 `submitPerformanceAmount`。⚠️ 空值也拒（校验正则不含空串）。 */
  function promptPerformanceAmount(
    id: number,
    kind: PerformanceAmountKind,
    current: string,
  ): void {
    ;(layer.prompt as (o: unknown, cb?: unknown) => void)(
      {
        title: PERF_EDIT_PROMPT_TITLE,
        shade: [0.3, '#333'],
        shadeClose: true,
        formType: 0,
        anim: 2,
        value: current,
      },
      (value: string, index: number) => {
        const next = String(value ?? '').trim()
        if (isBlankLikePython(next) || !validateMoneyInput(next).ok) {
          msg(PERF_AMOUNT_INVALID(current))
          return
        }
        if (next === current) {
          msg(PERF_SAME_AMOUNT)
          return
        }
        // ⭐ D1（R9）：**成功才关层、失败留层**。`submitPerformanceAmount` 的 `false` 分支
        //    已经弹过后端 `msg` ⇒ 这里只决定关不关。
        //    ⛔ `layer.close` **只能加在这里**：同一个出口还被行内编辑共用
        //    （`startInlineEdit` 的 `commit` 那条路上**没有层、没有 index**）。
        void submitPerformanceAmount(id, kind, next).then((ok) => {
          if (ok) (layer.close as (i: number) => void)(index)
        })
      },
    )
  }

  /**
   * 垃圾桶的确认（一处实现）：文案**逐字**取对标 ⇒ 软删该绩效行（spec §4.7 / §5-A2）。
   * 绩效弹层与指派弹层的账单表共用它。
   */
  function confirmDeletePerformance(id: number): void {
    ;(layer.confirm as (t: string, cb: (i: number) => void) => void)(
      PERF_DELETE_CONFIRM,
      (index: number) => {
        ;(layer.close as (i: number) => void)(index)
        void deletePerformanceRow(id)
      },
    )
  }

  /**
   * 当前打开的绩效弹层（`openPerformanceDialog` 打开时写、`end` 里清空）。
   *
   * ⚠️ 弹层节点由 layui 插到 `document.body`（`root` 之外）⇒ click 冒泡不到 `root`，要用**文档级**
   *    委托；监听器在开层时挂、`end` 里摘（同一个函数引用 ⇒ 重复 `open` 不会叠出第二份）。
   * ⚠️ `table` 那一格是**归属判据**：只有点击落在本层那张表上才算本层的动作。没有它，
   *    文档级委托会被同页里其它 `table.tt-perf-rows`（例如一个已经脱开文档、却还没摘监听器的
   *    旧层）抢答——测试里每个用例各挂一次页面，这条判据同时也是「旧挂载不干扰新挂载」的保证。
   */
  let activePerfDialog: {
    state: {
      rows: PerformanceListItem[]
      /** 正在提交的行 id：请求没回来之前**再点同一格不重开输入框**（防重复提交，spec §5-A1）。 */
      pending: Set<number>
    }
    /** 本层那张表的节点（`renderRows` 拿到后写）；点击落在别的表上 ⇒ 不是本层的事。 */
    table: HTMLElement | null
    /**
     * 点某一格 ⇒ 格内换成 `<input>`（行内编辑）。实现放在 `openPerformanceDialog` 里：
     * 那里能整表重渲染、也拿得到本层的 `state`。
     */
    startInlineEdit: (cell: HTMLElement, kind: PerformanceAmountKind) => void
  } | null = null

  /** 绩效弹层里的行内动作：铅笔（预计绩效）/ 垃圾桶（软删）/ 两格金额的行内编辑。 */
  const onPerfDialogClick = (event: Event): void => {
    const active = activePerfDialog
    if (active === null) return
    const target = event.target as HTMLElement
    // 归属判据：点击必须落在**本层**那张表里（否则是同页里另一层 / 已关掉的旧层留下的委托）。
    const table = target.closest<HTMLElement>('table.tt-perf-rows')
    if (table === null || table !== active.table) return
    const editEl = target.closest<HTMLElement>('[data-act="perf-edit"]')
    if (editEl) {
      const id = Number(editEl.dataset.id ?? '')
      const row = active.state.rows.find((r) => r.id === id)
      if (row !== undefined) promptPerformanceAmount(id, 'temp', row.temp_amount ?? '')
      return
    }
    const clearEl = target.closest<HTMLElement>('[data-act="perf-clear"]')
    if (clearEl) {
      const id = Number(clearEl.dataset.id ?? '')
      if (Number.isFinite(id)) confirmDeletePerformance(id)
      return
    }
    const cell = target.closest<HTMLElement>('[data-cell]')
    if (cell) {
      const key = cell.dataset.cell
      if (key === 'temp_amount') active.startInlineEdit(cell, 'temp')
      else if (key === 'bonus_amount') active.startInlineEdit(cell, 'bonus')
    }
  }

  /** 「查看或编辑订单接手绩效」弹层（`type:1` 手工 `<table>` 标记——layui 表格在 jsdom 里不产行，手工标记让判据打在真实 DOM 上；数据走既有读端点，⛔ 不让 `table.render({url})` 自己发请求）。点击走文档级委托，开层挂、`end` 摘。 */
  function openPerformanceDialog(orderNo: string): void {
    const state: { rows: PerformanceListItem[]; pending: Set<number> } = {
      rows: [],
      pending: new Set<number>(),
    }

    /** 一行的标记。⚠️ 每格内容包一层 `.layui-table-cell`（40px 行高规则打在它身上，`align` 也落在它的属性上）；生效绩效格 `style` 不带 `cursor:pointer`（只有预计绩效格带），但点击行为仍在。 */
    const rowHtml = (row: PerformanceListItem): string => {
      const temp = row.temp_amount ?? ''
      const bonus = row.bonus_amount ?? ''
      const project = row.taker_project ?? ''
      return (
        `<tr data-id="${row.id}">` +
        '<td><div class="layui-table-cell" align="left">' +
        `<span style="color:#006EE9">${escapeHtml(row.display_name ?? '')}</span></div></td>` +
        '<td><div class="layui-table-cell" align="center">' +
        `<span data-cell="temp_amount" data-id="${row.id}" data-fee="${escapeHtml(temp)}" ` +
        `style="color:#F3723A;cursor:pointer;" title="预计给该接手人的抛单金额">${escapeHtml(temp)}</span></div></td>` +
        '<td><div class="layui-table-cell" align="center">' +
        `<span data-cell="bonus_amount" data-id="${row.id}" data-fee="${escapeHtml(bonus)}" ` +
        `style="color:#F3723A" title="该接手人实际收入的绩效金额">${escapeHtml(bonus)}</span></div></td>` +
        '<td><div class="layui-table-cell" align="left">' +
        `<span style="color:#666" title="该接手人的项目：${escapeHtml(project)}">${escapeHtml(project)}</span></div></td>` +
        '<td><div class="layui-table-cell" align="center">' +
        `<span title="">${escapeHtml(formatPerfRecordTime(row.created_at ?? ''))}</span></div></td>` +
        '<td><div class="layui-table-cell" align="center">' +
        `<span title="">${escapeHtml(row.log_user_name ?? '')}</span></div></td>` +
        '<td><div class="layui-table-cell" align="center">' +
        `<a class="layui-btn layui-btn-xs layui-btn-warm" style="margin-right:0px" ` +
        `lay-event="editEmp" data-act="perf-edit" data-id="${row.id}" title="修改该记录的预计绩效金额">` +
        '<i class="layui-icon layui-icon-edit"></i></a>' +
        `<a class="layui-btn layui-btn-xs layui-btn-danger" style="margin-right:0px" ` +
        `lay-event="delEmp" data-act="perf-clear" data-id="${row.id}" title="${PERF_DELETE_TITLE}">` +
        '<i class="layui-icon layui-icon-delete"></i></a>' +
        '</div></td></tr>'
      )
    }

    const body =
      `<style>${PERF_DIALOG_STYLE}</style>` +
      '<form class="layui-form" action="">' +
      '<div style="margin-left:10px;margin-top:10px;margin-right:10px">' +
      '<table class="tt-perf-rows layui-table" style="margin:0">' +
      '<colgroup>' +
      PERF_DIALOG_COLS.map((col) =>
        col.width === '' ? '<col>' : `<col style="width:${col.width}">`,
      ).join('') +
      '</colgroup>' +
      '<thead><tr>' +
      PERF_DIALOG_COLS.map(
        (col) => `<th><div class="layui-table-cell" align="${col.align}">${col.title}</div></th>`,
      ).join('') +
      '</tr></thead><tbody></tbody></table>' +
      '</div></form>'

    const renderRows = (): void => {
      const tbody = document.querySelector<HTMLElement>('table.tt-perf-rows tbody')
      if (!tbody) return
      tbody.innerHTML = state.rows.map(rowHtml).join('')
      if (activePerfDialog !== null && activePerfDialog.state === state)
        activePerfDialog.table = tbody.closest<HTMLElement>('table')
    }

    const reloadPerfDialog = (): void => {
      void fetchPerformances({ order_no: orderNo, emp_type: PERF_EMP_TYPE }).then(
        (page) => {
          state.rows = page.items ?? []
          renderRows()
        },
        () => msg(PERF_LOAD_FAIL_MESSAGE),
      )
    }

    /**
     * 行内编辑：点一下把格子换成 `<input>`，`blur`/`Enter` 提交、`Esc` 取消；提交走 `adjust-amount`（与铅笔同载荷）。
     * ⛔ 不弹二级窗（这两格是行内格；铅笔仍走 `layer.prompt`）；请求进行中 ⇒ `state.pending` 上再点不重开输入框；「与之前一样」⇒ 不发请求；提交失败 ⇒ 恢复原格。
     */
    const startInlineEdit = (cell: HTMLElement, kind: PerformanceAmountKind): void => {
      if (cell.dataset.editing === '1') return
      const id = Number(cell.dataset.id ?? '')
      if (!Number.isFinite(id) || state.pending.has(id)) return
      const current = cell.dataset.fee ?? ''
      const original = cell.innerHTML
      const input = document.createElement('input')
      input.type = 'text'
      input.inputMode = 'decimal'
      input.className = 'tt-perf-amount-input'
      input.value = current
      cell.dataset.editing = '1'
      cell.textContent = ''
      cell.appendChild(input)
      input.focus()
      input.select()

      let settled = false
      const finish = (): void => {
        if (settled) return
        settled = true
        delete cell.dataset.editing
      }
      const commit = (): void => {
        if (settled) return
        finish()
        const next = input.value.trim()
        if (isBlankLikePython(next) || !validateMoneyInput(next).ok) {
          msg(PERF_AMOUNT_INVALID(current))
          cell.innerHTML = original
          return
        }
        if (next === current) {
          cell.innerHTML = original
          return
        }
        state.pending.add(id)
        // 提交期间禁掉再点：输入框留在格内并置灰（本层重新拉数后整行重建，它随之消失）。
        input.disabled = true
        void submitPerformanceAmount(id, kind, next).then((ok) => {
          state.pending.delete(id)
          if (!ok) cell.innerHTML = original
        })
      }
      input.addEventListener('blur', commit)
      input.addEventListener('keydown', (event: KeyboardEvent) => {
        if (event.key === 'Enter') {
          event.preventDefault()
          commit()
        } else if (event.key === 'Escape') {
          event.preventDefault()
          finish()
          cell.innerHTML = original
        }
      })
    }

    /** 行内动作：由文档级委托 `onPerfDialogClick` 处理（它读 `activePerfDialog`，且只认本层那张表）。 */
    perfViewReloaders.add(reloadPerfDialog)
    activePerfDialog = { state, table: null, startInlineEdit }
    document.addEventListener('click', onPerfDialogClick)
    ;(layer.open as (o: unknown) => void)({
      type: 1,
      title: PERF_DIALOG_TITLE,
      area: PERF_DIALOG_AREA,
      anim: PERF_DIALOG_ANIM,
      skin: 'layui-layer-lan',
      shade: [0.3, '#333'],
      shadeClose: true,
      content: body,
      // layui 插好内容后补渲染一次（两条链谁先回来都可能；幂等）。
      success: () => {
        renderRows()
      },
      end: () => {
        // 只有「当前这一层」是自己时才清空（防止后开的那层被先关的这层误清）。
        if (activePerfDialog !== null && activePerfDialog.state === state) activePerfDialog = null
        document.removeEventListener('click', onPerfDialogClick)
        perfViewReloaders.delete(reloadPerfDialog)
      },
    })
    reloadPerfDialog()
  }

  /* —— 真功能 6：指派打手 / 清除接单人（`/assign/` + `/clear-taker/`，spec §5.1 / §7.1） —— */

  /** 「按显示名升序」的确定性比较器：用**码点升序**——`localeCompare(…,'zh-Hans-CN')` 在本仓 Node/ICU 下是拼音序（实测与计划判据字面值相反），码点序既与判据一致也不依赖运行环境 ICU。 */
  const byDisplayName = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0)
  /** 候选人显示名直接用端点算好的 `name`；⛔ 别在本页自己拼 `display_name || username`（第二套口径，两处会静默分叉）。 */
  const accountDisplayName = (a: AssigneeOption): string => a.name

  /**
   * 指派弹层（页内自建九区块；工具栏「指派打手」与行内 `.jsr` 两个入口）。
   * ⛔ 发单价格送 `price`（订单发单价 `hall_price`），⛔ 不送 `fee`（那是直写绩效 `temp_amount` 的另一件事）。
   * ⛔ 拉名单失败也要能开弹层（只提示一句，别「点了没反应」）；取值一律走 `panel()`（文档级优先——`layer.open` 会把节点搬进弹层）。
   * ⛔ 「对标有、我方不做」的五处（上号电话/智能派单/百分比发单价/排序方式/保存筛选）一个都不渲染。
   * ⚠️ 弹层里的表是手工 `<table>` 标记（layui 表格在 jsdom 不产行，手工标记让判据打在真实 DOM 上）。
   */
  function openAssignDialog(
    context: AssignDialogContext,
    // ⚠️ 形参名必须是 `batchTarget`（不是 `target`）：函数体两个回调各有 `const target = event.target`（事件源）会遮蔽同名形参——叫 `target` 时读到的 `kind` 恒 undefined（实测踩过）。
    batchTarget: BatchTarget = { kind: 'single', orderNo: context.orderNo },
  ): void {
    const { orderNo, currentTaker, orderProject, orderHallPrice } = context
    /** 本次弹层的名单（拉回来后按显示名升序）、右表那**唯一一行**的人、账单行与「当前接手人」那一行。 */
    const state: {
      accounts: AssigneeOption[]
      selected: AssigneeOption | null
      perfs: PerformanceListItem[]
      existing: PerformanceListItem | null
      /** 接口回的「本租户职位名」全集（B12b）；空数组 = 还没回来 ⇒ 那一组只剩「全部」。 */
      roles: string[]
      /** 当前选中档（`''` = 「全部」）。默认 `ASSIGN_ROLE_DEFAULT`，租户没有它时回退（V-G）。 */
      role: string
    } = {
      accounts: [],
      selected: null,
      perfs: [],
      existing: null,
      roles: [],
      role: ASSIGN_ROLE_DEFAULT,
    }
    /** 右表那一行的三格值（只住内存，点「保存指派」才落库）。初值：发单价 = 订单 `hall_price`（⛔ 不是绩效行的 `temp_amount`）、项目 = 该单 `project_text`；时间要求留空（读接口回来才填）。 */
    const cellValues: { price: string; hours: string; project: string } = {
      price: orderHallPrice,
      hours: '',
      project: orderProject,
    }

    const body =
      `<form class="layui-form layui-form-pane" id="${ASSIGN_DIALOG_ID}" style="padding:10px">` +
      // ── 区块 1：用户职位（单选；「全部」+ **接口返回的 roles**，默认「代练员」——**有意偏离**对标默认「全部」，V5）──
      //    ⚠️ 选项值来自响应（B12b / R9）⇒ 这里先留空容器，响应回来时 `renderRoleOptions()` 整组重建。
      '<div class="layui-form-item"><label class="layui-form-label">用户职位</label>' +
      '<div class="layui-input-block" data-role-options></div></div>' +
      // ── 区块 2：业务类型（三档，value/title 逐字取对标）──
      '<div class="layui-form-item"><label class="layui-form-label">业务类型</label>' +
      '<div class="layui-input-block">' +
      ASSIGN_BUSS_OPTIONS.map(
        (item) =>
          `<input type="radio" name="bussType" value="${item.value}" title="${item.title}" ` +
          `lay-filter="bussTypechange"${item.value === '0' ? ' checked' : ''}>`,
      ).join('') +
      '</div></div>' +
      // ── 区块 3「排序方式」**不渲染**（C4）──
      // ── 区块 4：搜索打手（`maxlength` 是我们 `key` 的上界，V6；按钮外包两层 div 照对标）──
      '<div class="layui-form-item"><label class="layui-form-label">搜索打手</label>' +
      '<div class="layui-input-inline">' +
      `<input type="text" name="key1" id="key1" placeholder="用户名/名称/标记" ` +
      `maxlength="${ASSIGN_KEY_MAXLENGTH}" class="layui-input">` +
      '</div>' +
      '<div class="layui-inline"><div class="layui-input-inline">' +
      '<button type="button" class="layui-btn layui-btn-normal sechbtn" lay-filter="seachBtn">搜索</button>' +
      '</div></div>' +
      '</div>' +
      // ── 区块 5/6/7：左列多选列表 + 右列两张表 ──
      '<div style="display:flex;gap:10px">' +
      '<div style="flex:1;min-width:240px"><div class="user" style="margin-left:0px"></div></div>' +
      '<div style="flex:1;min-width:320px">' +
      '<fieldset class="layui-elem-field"><legend style="font-size:14px">当前订单分配打手</legend>' +
      '<table class="tt-assign-takers layui-table" style="margin:0">' +
      '<thead><tr>' +
      ASSIGN_TAKER_COLS_TITLES.map((title) => `<th>${title}</th>`).join('') +
      '</tr></thead><tbody></tbody></table></fieldset>' +
      '<fieldset class="layui-elem-field">' +
      '<legend style="font-size:14px">该订单的打手账单' +
      '<span style="color:#ff0000;font-weight:600">【↓中途换打手请从下面列表中把没用的账单删除，这关系到后面与代练结算↓】</span>' +
      '</legend>' +
      '<table class="tt-assign-bills layui-table" style="margin:0">' +
      '<thead><tr>' +
      ASSIGN_BILL_COLS_TITLES.map((title) => `<th>${title}</th>`).join('') +
      '</tr></thead><tbody></tbody></table></fieldset>' +
      '</div></div>' +
      // ── 区块 8：底部两颗按钮（文案逐字；对标两颗恒在 ⇒ 没有接单人也照渲染）──
      '<div class="layui-form-item" style="margin-top:12px">' +
      '<button type="button" class="layui-btn layui-btn-normal w-lg" id="saveBtn" lay-filter="saveBtn" data-act="assign-save">保存指派(没改动请勿点)</button>' +
      '<button type="button" class="layui-btn layui-btn-danger w-lg" style="margin-left:10px" lay-filter="delBtn" data-act="assign-clear">清除当前所有接手人</button>' +
      '</div></form>'

    /** 弹层节点：**文档级优先**（见函数头注释；与 `settPanel()` 同一判据）。 */
    const panel = (): HTMLElement | null =>
      document.querySelector<HTMLElement>(`.layui-layer-content #${ASSIGN_DIALOG_ID}`) ??
      document.querySelector<HTMLElement>(`#${ASSIGN_DIALOG_ID}`)

    /**
     * 渲染「用户职位」那一组（B12b / R9）：值 = `[「全部」, ...state.roles]`，勾 `state.role`。
     *
     * ⚠️ 值来自**接口响应**（`AssigneeOptions.roles`）⇒ body 里只留一个空容器，响应回来时整组重建；
     *    重建后要重新 `form.render('radio')`，否则新插进去的 radio 只剩没有文字的圆圈
     *    （与 `success` 里那条注释同一个坑）。
     * ⚠️ 幂等：`success` 与响应回调都会调它（谁先到都可能）。重建按 `state.role` 恢复勾选 ⇒
     *    用户切过的那一档不会丢。`state.role` 是**选择状态**：随 `change` 委托同步
     *    （勾哪一档就写哪一档；响应回调里的 V-G 回退也改它），读它的有两处——本函数的
     *    `role === state.role` 与 `fetchAssignCandidates` 的 `roleInput?.value ?? state.role`。
     */
    const renderRoleOptions = (): void => {
      const box = panel()?.querySelector<HTMLElement>(ASSIGN_ROLE_OPTIONS_SELECTOR)
      if (!box) return
      box.innerHTML = [ASSIGN_ROLE_ALL_VALUE, ...state.roles]
        .map((role) => {
          const title = role === ASSIGN_ROLE_ALL_VALUE ? '全部' : role
          return (
            `<input type="radio" name="userRole" value="${escapeHtml(role)}" ` +
            `title="${escapeHtml(title)}" lay-filter="userRole"` +
            `${role === state.role ? ' checked' : ''}>`
          )
        })
        .join('')
      ;(form.render as (t?: string) => void)('radio')
    }

    /** 把 `state.accounts` 灌进左列 `.user` 并预勾选 `state.selected`（找不到节点静默返回）。 */
    const renderUserList = (): void => {
      const box = panel()?.querySelector<HTMLElement>('.user')
      if (!box) return
      box.innerHTML = state.accounts
        .map((a) => {
          const name = accountDisplayName(a)
          // ⛔ `role`/`tag`/`remark` 都是自由文本：缺字段 `?? ''` 兜底（别让 `escapeHtml(undefined)` 有机会被调）；插值全部过 `escapeHtml`。
          const role = a.role ?? ''
          const tag = a.tag ?? ''
          const remark = a.remark ?? ''
          const tip = `${name}（职位：${role}，标记：${tag}，备注：${remark}）`
          const badge =
            tag === ''
              ? ''
              : `<span class="layui-badge" title="${escapeHtml(tag)}">${escapeHtml(tag.slice(0, 1))}</span>`
          const checked = state.selected !== null && state.selected.id === a.id ? ' checked' : ''
          return (
            '<div class="layui-input-inline" style="width:185px;">' +
            `<a href="javascript:void(0)" lay-tips="${escapeHtml(tip)}">` +
            `<input type="checkbox" name="userList" lay-skin="primary" title="${escapeHtml(name)}" ` +
            `value="${a.id}" userName="${escapeHtml(name)}" lay-filter="userList"${checked}>` +
            badge +
            '</a></div>'
          )
        })
        .join('')
      // checkbox 也是 layui 表单件（它的视图是照原生 input 另建的一份）。
      ;(form.render as (t?: string) => void)('checkbox')
    }

    /** 右表三格的 `<a>` 标记（整行渲染与单格重渲染共用一份）；值取 `cellValues`（未保存的内存态，重渲染不丢改动）。 */
    const takerCellAnchorHtml = (field: string, name: string): string => {
      if (field === 'price') {
        return (
          `<a class="t0" style="color:#F3723A" href="javascript:void(0)" ` +
          `title="预计给${escapeHtml(name)}的发单价，点击可以编辑">${escapeHtml(cellValues.price)}</a>`
        )
      }
      if (field === 'hours') {
        return (
          `<a class="t1" href="javascript:void(0)" ` +
          `title="要求在${escapeHtml(cellValues.hours)}小时内完成，点击可以编辑小时">${escapeHtml(cellValues.hours)}</a>`
        )
      }
      return (
        `<a class="t2" href="javascript:void(0)" ` +
        `title="该接手人的具体项目：${escapeHtml(cellValues.project)}">${escapeHtml(cellValues.project)}</a>`
      )
    }

    /**
     * 右表整行渲染（恰好一行）。⚠️ 后三格渲染成 `<a>`（`data-cell` 供行内编辑认格）；显示名/项目是自由文本 ⇒ 正文与 title 都过 `escapeHtml`。
     * ⛔ 行内提交**不要**走这里（整段 `tbody.innerHTML` 重建会吞掉鼠标那一次点击）：见 `renderTakerCell`。
     */
    const fillTakerRow = (): void => {
      const tbody = panel()?.querySelector<HTMLElement>('table.tt-assign-takers tbody')
      if (!tbody) return
      const a = state.selected
      if (a === null) {
        tbody.innerHTML = ''
        return
      }
      const name = accountDisplayName(a)
      tbody.innerHTML =
        `<tr data-id="${a.id}">` +
        `<td>${escapeHtml(name)}</td>` +
        `<td data-cell="price">${takerCellAnchorHtml('price', name)}</td>` +
        `<td data-cell="hours">${takerCellAnchorHtml('hours', name)}</td>` +
        `<td data-cell="project">${takerCellAnchorHtml('project', name)}</td>` +
        '<td><a class="layui-btn layui-btn-xs layui-btn-danger" style="margin-right:1px" ' +
        'lay-event="delAss" data-act="assign-remove" title="移除该接手人的指派">移除</a></td>' +
        '</tr>'
    }

    /** 只重渲染右表某一格（值真的变了时）。⛔ 这格不能换成 `fillTakerRow()`：整段重建会把 mousedown 已落在其上的下一格在 click 之前摘出文档 ⇒ 浏览器不再派发 click，从一格点另一格要点两次；值没变时提交只还原原文、不调它。 */
    const renderTakerCell = (cell: HTMLElement): void => {
      const a = state.selected
      if (a === null) return
      cell.innerHTML = takerCellAnchorHtml(cell.dataset.cell ?? '', accountDisplayName(a))
    }

    /** 打开时按读接口回来的那行预勾选并预填（名单与账单两条并行请求，做成幂等、两边回来都调一次）。⚠️ 发单价格**不在这里填**——它显示的是订单发单价（开层时已定），`temp_amount` 是预计绩效，两者不是一回事；`hours` 可空 ⇒ 缺就是空串（保存时不送）。 */
    const applyExisting = (): void => {
      const row = state.existing
      if (currentTaker === '' || row === null) return
      const rowName = row.display_name || row.username || ''
      const account =
        state.accounts.find((a) => accountDisplayName(a) === rowName) ??
        state.accounts.find((a) => row.user !== null && a.id === row.user) ??
        null
      if (account !== null) state.selected = account
      cellValues.hours = row.hours === null || row.hours === undefined ? '' : String(row.hours)
      cellValues.project = row.taker_project ?? ''
      renderUserList()
      fillTakerRow()
    }

    /** ISO 时刻串 → `YYYY-MM-DD HH:mm`（绩效子表「记录时间」列用）。⚠️ 后端 `created_at` 已是项目时区（+08:00）的 ISO 串 ⇒ 截断安全。 */
    const sliceDateTime = (value: string | null | undefined): string => {
      if (value === null || value === undefined || value.length < 16) {
        return value ?? ''
      }
      return value.substring(0, 16).replace('T', ' ')
    }

    /** 区块 7「该订单的打手账单」表（与绩效弹层同一数据源的两处视图；垃圾桶动作软删该绩效行，与绩效弹层共用一处实现）。 */
    const renderBillTable = (): void => {
      const tbody = panel()?.querySelector<HTMLElement>('table.tt-assign-bills tbody')
      if (!tbody) return
      tbody.innerHTML = state.perfs
        .map(
          (p) =>
            `<tr data-id="${p.id}">` +
            `<td>${escapeHtml(p.display_name || p.username || '')}</td>` +
            `<td>${escapeHtml(p.temp_amount ?? '')}</td>` +
            `<td>${escapeHtml(p.bonus_amount ?? '')}</td>` +
            `<td>${escapeHtml(sliceDateTime(p.created_at))}</td>` +
            '<td>' +
            `<a class="layui-btn layui-btn-xs layui-btn-normal" style="margin-right:1px" ` +
            `lay-event="upEmp" data-act="perf-edit" data-id="${p.id}" title="更改该绩效金额">` +
            '<i class="layui-icon layui-icon-edit"></i></a>' +
            `<a class="layui-btn layui-btn-xs layui-btn-danger" style="margin-right:1px" ` +
            `lay-event="delEmp" data-act="perf-clear" data-id="${p.id}" title="${PERF_DELETE_TITLE}">` +
            '<i class="layui-icon layui-icon-delete"></i></a>' +
            '</td></tr>',
        )
        .join('')
    }

    /**
     * 拉打手名单（默认按「代练员」筛；选「全部」时这个键整个去掉）。
     * ⛔ 数据来源是专用端点 `/orders/assignee-options/`，别换回 `GET /api/accounts/`（它判 `system_user.list`，客服拉名单会 403）。
     * ⛔ `role` 是请求期逐个查表校验的（任一不存在整条 1000）⇒ `roles` 还没拿到时**不筛职位**，拿到后再按选中档补一趟——否则第一趟就被整条拒，`roles` 永远拿不到、回退永远不会发生。
     * ⚠️ 「全部」在后端没有对应值 ⇒ 不送这个键（送空串会被拒成 1000）；`business_type` 的 `0` 档落成「不筛」。
     * ⚠️ 排序在前端（只排本次拉回的 ≤200 条）。
     */
    const fetchAssignCandidates = (): void => {
      const scope = panel()
      const roleInput = scope?.querySelector<HTMLInputElement>('input[name="userRole"]:checked')
      const bussInput = scope?.querySelector<HTMLInputElement>('input[name="bussType"]:checked')
      const keyInput = scope?.querySelector<HTMLInputElement>('input#key1')
      const params: {
        role?: string
        business_type?: number
        key?: string
        limit: number
      } = { limit: ASSIGN_PAGE_SIZE }
      // 控件已渲染 ⇒ 以勾选为准；还没渲染（roles 还没回来）⇒ 以 `state.role` 为准。
      const role = roleInput?.value ?? state.role
      // ⚠️「全部」在我们的后端**没有对应值**（没有 `role=全部` 这个取值）⇒ 不送这个键
      //    （送空串会被 `MultiRoleField` 拒成 1000）；`roles` 未知时也先不送（上面那段理由）。
      if (state.roles.length > 0 && role !== ASSIGN_ROLE_ALL_VALUE) params.role = role
      // ⚠️ 对标的 `0` 档是「代练+陪玩」这一档，而我们的 `business_type` 是**人身上的单选属性**
      //    ⇒ 送 `0` 会只留「标了代练+陪玩」的人；`0` 档落成「不筛」（V11）。
      const buss = bussInput?.value ?? '0'
      if (buss !== '0') params.business_type = Number(buss)
      if (keyInput && keyInput.value.trim() !== '') params.key = keyInput.value.trim()
      // D-0146 方案②：换到**不判 `system_user.list`** 的专用名单端点；参数（含 200 的上界）逐字未变。
      void fetchAssigneeOptions(params).then(
        (page) => {
          // roles 是**这一趟**才拿到的（之前为空）⇒ 这一趟的名单没按职位筛，可能还要补一趟。
          const firstPass = state.roles.length === 0
          state.roles = page.roles ?? []
          const wanted = roleInput?.value ?? state.role
          // ⭐ V-G：选中的那一档（默认档「代练员」）在本租户职位表里不存在 ⇒ **静默**回落到
          //    「全部」（不弹提示、不为这件事写 console 噪声）。
          state.role =
            wanted !== ASSIGN_ROLE_ALL_VALUE && !state.roles.includes(wanted)
              ? ASSIGN_ROLE_ALL_VALUE
              : wanted
          // 这一趟没筛职位、而选中的那一档**确实存在** ⇒ 补一趟把筛子加上（否则展示的是全量，
          // 与界面上勾着的档自相矛盾）。只在「roles 刚拿到」这一趟可能发生：补的那一趟
          // `state.roles` 已非空 ⇒ 不会再来一趟。
          if (firstPass && state.role !== ASSIGN_ROLE_ALL_VALUE) {
            renderRoleOptions()
            fetchAssignCandidates()
            return
          }
          // ⚠️ 排序在**前端**（后端 `order_by("id")`，没有排序参数，C4）：只排本次拉回的 ≤200 条（V4）。
          state.accounts = [...(page.items ?? [])].sort((a, b) =>
            byDisplayName(accountDisplayName(a), accountDisplayName(b)),
          )
          renderRoleOptions()
          renderUserList()
          applyExisting()
        },
        () => msg('代练员名单拉取失败，请稍后重试'),
      )
    }

    /** 拉该订单的打手账单并据此预填；「当前接手人」那一行取 `currentTaker` 那条（找不到退第一行）；没人接 ⇒ 不预填不预勾选。 */
    const loadExistingPerformance = (): void => {
      void fetchPerformances({ order_no: orderNo, emp_type: PERF_EMP_TYPE }).then(
        (page) => {
          state.perfs = page.items ?? []
          state.existing =
            currentTaker === ''
              ? null
              : ((state.perfs.find((p) => (p.display_name ?? '') === currentTaker) ??
                  state.perfs[0]) ??
                null)
          renderBillTable()
          applyExisting()
        },
        () => msg(ASSIGN_BILL_FAIL_MESSAGE),
      )
    }

    /** 账单表的重取（铅笔/垃圾桶成功后刷新本表）。⚠️ 与 `loadExistingPerformance` 分开：这里**不**重跑 `applyExisting`——那会把右表用户已改的值按旧值重置。 */
    const reloadBill = (): void => {
      void fetchPerformances({ order_no: orderNo, emp_type: PERF_EMP_TYPE }).then(
        (page) => {
          state.perfs = page.items ?? []
          renderBillTable()
        },
        () => msg(ASSIGN_BILL_FAIL_MESSAGE),
      )
    }

    /**
     * 三格的行内编辑：点一下换 `<input>`，blur/Enter 提交、Esc 取消；提交只写 `cellValues` 并重渲染那一格——⛔ 不发请求、不弹二级窗（必须再点「保存指派」才落库，与绩效弹窗「点一下立刻发请求」是两套语义，别互抄）。
     * 校验：金额走本页既有校验、`hours` 正整数 ≥1、`project` 不能为空；不过 ⇒ 提示 + 还原原标记（不把半截输入留在格子里）。
     */
    const editCell = (cell: HTMLElement): void => {
      const field = cell.dataset.cell ?? ''
      // 同一格已经在编辑中 ⇒ 再点不重开（否则会叠出第二个 input）。
      if (cell.dataset.editing === '1') return
      const current =
        field === 'price'
          ? cellValues.price
          : field === 'hours'
            ? cellValues.hours
            : field === 'project'
              ? cellValues.project
              : ''
      const original = cell.innerHTML
      const input = document.createElement('input')
      input.type = 'text'
      // 金额格给一个十进制软键盘提示（对标那格是 layui 的 `edit:true`，没有 inputmode 可抄）。
      if (field === 'price') input.inputMode = 'decimal'
      input.className = 'tt-assign-cell-input'
      input.value = current
      cell.dataset.editing = '1'
      cell.textContent = ''
      cell.appendChild(input)
      input.focus()
      input.select()

      let settled = false
      const finish = (): void => {
        if (settled) return
        settled = true
        delete cell.dataset.editing
      }
      const cancel = (): void => {
        finish()
        cell.innerHTML = original
      }
      const commit = (): void => {
        if (settled) return
        finish()
        const raw = input.value
        const next = raw.trim()
        if (field === 'price') {
          if (isBlankLikePython(next) || !validateMoneyInput(next).ok) {
            // **逐字**取对标 `changeEmp` 那一句（账单表铅笔，`assignation.js:790`）。
            msg(`${ASSIGN_FEE_INVALID_PREFIX}${current}`)
            cell.innerHTML = original
            return
          }
          // 值没变 ⇒ 只还原这一格（⛔ 不重渲染：整段重建会吞掉鼠标点到下一格的那一次点击）。
          if (next === current) {
            cell.innerHTML = original
            return
          }
          cellValues.price = next
        } else if (field === 'hours') {
          if (!/^\d+$/.test(next) || Number(next) < 1) {
            msg(ASSIGN_HOURS_MIN_MESSAGE)
            cell.innerHTML = original
            return
          }
          if (next === current) {
            cell.innerHTML = original
            return
          }
          cellValues.hours = next
        } else if (field === 'project') {
          if (next === '') {
            msg(ASSIGN_PROJECT_EMPTY_MESSAGE)
            cell.innerHTML = original
            return
          }
          if (raw === current) {
            cell.innerHTML = original
            return
          }
          // 项目是自由文本：只判「非空」，按**原样**存（不 trim —— 对标存的是 `obj.value`）。
          cellValues.project = raw
        } else {
          cell.innerHTML = original
          return
        }
        // 值真的变了 ⇒ **只**重渲染这一格（⛔ 不是 `fillTakerRow()`：见 `renderTakerCell` 注释）。
        renderTakerCell(cell)
      }
      input.addEventListener('blur', commit)
      input.addEventListener('keydown', (event: KeyboardEvent) => {
        if (event.key === 'Enter') {
          event.preventDefault()
          commit()
        } else if (event.key === 'Escape') {
          event.preventDefault()
          cancel()
        }
      })
    }

    /** 「移除」：清空右表那一行并**取消勾选**（对标 `delAss` 的「删除选中 check」同形）。 */
    const removeSelected = (): void => {
      const selected = state.selected
      if (selected === null) return
      const box = panel()?.querySelector<HTMLInputElement>(
        `input[name="userList"][value="${selected.id}"]`,
      )
      if (box) box.checked = false
      state.selected = null
      fillTakerRow()
    }

    /** 提交指派。⚠️ 成功才关弹层：失败只弹后端 msg 原文、弹层留着；成功路径 `reload()` 必须在 `msg` 之前（筛选警告会盖掉提示）。 */
    const submitAssign = (index: number): void => {
      const box = panel()?.querySelector<HTMLInputElement>('input[name="userList"]:checked')
      if (!box) {
        msg('请选择代练员')
        return
      }
      // 交叉类型让同一对象既能喂单笔 `assignOrder` 又能喂 `runBatchFlow`；⛔ 不两处各拼一份载荷。
      const payload: AssignOrderPayload & Record<string, unknown> = { user_id: Number(box.value) }
      // ⛔ 发单价格送 `price`（后端写 `hall_price` 再按提成率重算）；空 ⇒ 整个键不送，别送空串（后端当非法金额拒）。
      if (cellValues.price !== '') payload.price = cellValues.price
      if (cellValues.hours !== '') payload.hours = Number(cellValues.hours)
      if (cellValues.project.trim() !== '') payload.project = cellValues.project
      // 工具栏来的（批量，spec §5.2）⇒ 两步式批量端点。关层时机照单笔那一支
      // （**成功才关**，失败留层让用户改）。
      if (batchTarget.kind === 'batch') {
        void runSelectedBatch(batchTarget, '批量指派打手', {
          action: 'assign',
          payload,
          successMessage: batchSuccessMessage(ASSIGN_SUCCESS_MESSAGE),
          // 注入页面现成的 `msg` ⇒ layui 加载失败那一支也弹得出来（⛔ 不再静默退到 `logError`）。
          msg: (text) => msg(text),
          onDone: () => {
            ;(layer.close as (i: number) => void)(index)
            reload()
          },
        })
        return
      }
      void assignOrderWithMessage(orderNo, payload).then(
        ({ message }) => {
          ;(layer.close as (i: number) => void)(index)
          reload()
          // ⭐ 后端成功 `msg` 可能带「代练通订单未能撤回」警告（撤不掉时仍是成功码）
          // ⇒ 原文显示；空串才回退默认「指派成功」。带警告时用 `layer.alert`，⛔ 不一闪而过。
          showBackendSuccessMessage(backendMessageActions, message, ASSIGN_SUCCESS_MESSAGE)
        },
        (e: unknown) => msg(errText(e)),
      )
    }

    /** 清除接单人（`isDelEmp` 两档**共用**一段「关确认层 → 打端点 → 关弹层 + reload + msg」）。 */
    const doClearTaker = (isDelEmp: 'Y' | 'N', confirmIndex: number, dialogIndex: number): void => {
      ;(layer.close as (i: number) => void)(confirmIndex)
      void clearTaker(orderNo, { isDelEmp }).then(
        () => {
          ;(layer.close as (i: number) => void)(dialogIndex)
          reload()
          msg(CLEAR_TAKER_SUCCESS_MESSAGE)
        },
        (e: unknown) => msg(errText(e)),
      )
    }

    /**
     * 清除接单人：破坏性动作，两步 `layer.confirm`。
     * ⚠️ 第二步的第二个按钮是 `layer.confirm(text, options, yes, btn2)` 的**第 4 个参数**（两个按钮各走一个回调，`Y`/`N` 分别送 `isDelEmp`）。
     */
    const confirmClearTaker = (index: number): void => {
      ;(layer.confirm as (t: string, cb: (i: number) => void) => void)(
        CLEAR_TAKER_CONFIRM_TEXT,
        (firstIndex: number) => {
          ;(layer.close as (i: number) => void)(firstIndex)
          ;(
            layer.confirm as (
              t: string,
              o: unknown,
              cb: (i: number) => void,
              cb2: (i: number) => void,
            ) => void
          )(
            CLEAR_TAKER_DEL_EMP_TEXT,
            { btn: [...CLEAR_TAKER_DEL_EMP_BUTTONS] },
            (secondIndex: number) => doClearTaker('Y', secondIndex, index),
            (secondIndex: number) => doClearTaker('N', secondIndex, index),
          )
        },
      )
    }

    // 弹层先开（内容已造好），名单与账单两条请求各自回来自己渲染——
    // 「拉失败也要能开弹层」靠这个顺序（见函数头注释）。
    fetchAssignCandidates()
    loadExistingPerformance()
    // T7：本表的账单在「绩效弹层 / 本表」任一处改金额或软删之后要重取（关层时注销）。
    perfViewReloaders.add(reloadBill)
    ;(layer.open as (o: unknown) => void)({
      type: 1,
      title: ASSIGN_DIALOG_TITLE,
      // 尺寸按本批内容放大（对标那个 iframe 是整页 85%×95%，占比对齐，spec §7.1「弹层壳」）。
      area: ['85%', '95%'],
      skin: 'layui-layer-lan',
      shade: [0.3, '#333'],
      shadeClose: true,
      anim: 5,
      content: body,
      end: () => {
        perfViewReloaders.delete(reloadBill)
      },
      success: (layero: { find: (s: string) => { 0?: HTMLElement } }, index: number) => {
        const dialog = layero.find(`#${ASSIGN_DIALOG_ID}`)[0]
        if (!dialog) return
        // ⛔ 必须手动 `form.render('radio')`（同一坑）：本层两组 radio 是运行期插进 layer 的，搬进弹层后已不是页面那个被初始化过的 `.layui-form` 的子孙——不 render 时 title 不会变成可见文案（用户看到没有文字的圆圈）。
        ;(form.render as (t?: string) => void)('radio')
        // 两条请求谁先回来都可能：先把 `state` 里已有的补渲染一次（幂等）。
        renderRoleOptions()
        renderUserList()
        renderBillTable()
        applyExisting()

        dialog.addEventListener('change', (event) => {
          const target = event.target as HTMLInputElement
          if (target.name === 'userRole') {
            // 选中档是状态：响应回来重建那一组要照它恢复勾选，重拉名单也要照它取参。
            state.role = target.value
            fetchAssignCandidates()
            return
          }
          if (target.name === 'bussType') {
            fetchAssignCandidates()
            return
          }
          if (target.name !== 'userList') return
          const account = state.accounts.find((a) => String(a.id) === target.value) ?? null
          if (!target.checked) {
            // 取消勾选 ⇒ 右表那一行清空（spec §7.1 区块 5）。
            if (account !== null && state.selected !== null && state.selected.id === account.id) {
              state.selected = null
              fillTakerRow()
            }
            return
          }
          if (account === null) return
          if (state.selected !== null && state.selected.id !== account.id) {
            // 单人版核心约束：勾第二个 ⇒ 不改已有选择、弹回新的那个、提示用**已勾选那个人**的名字。
            target.checked = false
            msg(ASSIGN_ONE_TAKER_MESSAGE(accountDisplayName(state.selected)))
            return
          }
          state.selected = account
          fillTakerRow()
        })

        dialog.addEventListener('click', (event) => {
          const target = event.target as HTMLElement
          if (target.closest('.sechbtn')) {
            fetchAssignCandidates()
            return
          }
          if (target.closest('[data-act="assign-save"]')) {
            submitAssign(index)
            return
          }
          if (target.closest('[data-act="assign-clear"]')) {
            // ⛔ 批量档没有「清除接手人」的批量端点 ⇒ 走「尚未开放」出口，连两跳确认都不进（否则白确认两次后才弹内部校验错）；
            // ⛔ 不删按钮、不置灰；单笔支一字不改。
            if (batchTarget.kind === 'batch') {
              notOpen('批量清除接手人')
              return
            }
            confirmClearTaker(index)
            return
          }
          if (target.closest('[data-act="assign-remove"]')) {
            removeSelected()
            return
          }
          // T7：账单表那两颗图标按钮接上**与绩效弹层同一套**动作（一处实现，两个视图；MF-3）。
          const billEditEl = target.closest<HTMLElement>('[data-act="perf-edit"]')
          if (billEditEl) {
            const id = Number(billEditEl.dataset.id ?? '')
            const row = state.perfs.find((p) => p.id === id)
            if (row !== undefined) promptPerformanceAmount(id, 'temp', row.temp_amount ?? '')
            return
          }
          const billClearEl = target.closest<HTMLElement>('[data-act="perf-clear"]')
          if (billClearEl) {
            const id = Number(billClearEl.dataset.id ?? '')
            if (Number.isFinite(id)) confirmDeletePerformance(id)
            return
          }
          const cell = target.closest<HTMLElement>('[data-cell]')
          if (cell) editCell(cell)
        })

        // 回车 = 点「搜索」（spec §7.1 区块 4）。
        dialog
          .querySelector<HTMLInputElement>('input#key1')
          ?.addEventListener('keydown', (event) => {
            if ((event as KeyboardEvent).key === 'Enter') {
              event.preventDefault()
              fetchAssignCandidates()
            }
          })
      },
    })
  }

  /* —— 真功能 7：改类型（证据件里那个 `.typebox` 弹层，spec §5.2 / C4） —— */

  /**
   * 弹层打开期间是否已挂上文档级保存委托（同 `throwSaveBound` / `refundSaveBound`）。
   */
  let typeSaveBound = false
  /** 弹层里当前那一单的单号（对标用一个全局 `IDX` 数组装它；我们单笔操作，只留一个单号）。 */
  let typeDialogOrderNo: string | null = null
  /** 目标描述（spec §D11）：工具栏＝批量、行内＝单笔。`null` = 弹层没开。 */
  let typeTarget: BatchTarget | null = null
  /** 弹层实例下标（`layer.close` 要它；`end` 与成功路径都会清空）。 */
  let typeLayerIndex: number | null = null

  /** 取「改类型」弹层节点。⚠️ 文档级优先（`layer.open` 搬节点进弹层、关闭再搬回，root 级第二次打开取不到）。 */
  function typePanel(): HTMLElement | null {
    return (
      document.querySelector<HTMLElement>('.layui-layer-content > .typebox') ??
      root.querySelector<HTMLElement>('.typebox')
    )
  }

  /** 复位弹层里那一次选择。⚠️ 必须在 `layer.open` 把节点搬走**之前**调用：搬进 `.layui-layer-content` 后已不是 `.layui-form` 的子孙，那时 `form.render('radio')` 够不到它。 */
  function resetTypePanel(node: HTMLElement): void {
    for (const radio of Array.from(
      node.querySelectorAll<HTMLInputElement>('input[name="selbussType"]'),
    )) {
      radio.checked = false
    }
    ;(form.render as (t?: string) => void)('radio')
  }

  /**
   * `.settypes` 在**弹层里**（layui 把 `.typebox` 搬进 `.layui-layer-content`），而
   * `.layui-layer` 挂在 `document.body` 上 ⇒ click 冒泡不到 `root`（同发价 / 退款弹层）。
   * 所以这一个按钮用**文档级捕获**接管，只在弹层打开期间挂上、`end` 里摘掉。
   */
  const onTypeSaveClick = (event: Event): void => {
    const btn = (event.target as HTMLElement).closest<HTMLElement>('.settypes')
    if (!btn) return
    event.preventDefault()
    event.stopPropagation()
    submitOrderType()
  }

  /** 打开「改类型」弹层：内容就是证据件里的 `.typebox` 节点本身；`end` 里把节点搬回 `form.layui-form`（下次打开取得到、复位时 render 够得到）。 */
  function openTypeDialog(orderNo: string, target: BatchTarget = { kind: 'single', orderNo }): void {
    const node = typePanel()
    if (!node) {
      msg('改类型弹层打不开，请刷新页面后重试')
      return
    }
    typeDialogOrderNo = orderNo
    typeTarget = target
    resetTypePanel(node)
    const host = root.querySelector<HTMLElement>('form.layui-form') ?? root
    const jq = (layui as unknown as { $: (el: Element) => unknown }).$(node)
    if (!typeSaveBound) {
      document.addEventListener('click', onTypeSaveClick, true)
      typeSaveBound = true
    }
    typeLayerIndex = (layer.open as (o: unknown) => number)({
      type: 1,
      title: ORDER_TYPE_DIALOG_TITLE,
      area: ['700px', '300px'],
      skin: 'layui-layer-lan',
      shade: [0.3, '#333'],
      shadeClose: true,
      anim: 5,
      content: jq,
      end: () => {
        typeLayerIndex = null
        typeDialogOrderNo = null
        typeTarget = null
        node.style.display = 'none'
        if (node.parentElement !== host) host.appendChild(node)
        if (typeSaveBound) {
          document.removeEventListener('click', onTypeSaveClick, true)
          typeSaveBound = false
        }
        resetTypePanel(node)
      },
    })
  }

  /**
   * 提交改类型：PATCH 载荷只有 `{ order_type }`。
   * ⛔ 金额一律不许夹带（`UpdateOrderPayload` 类型上已 Omit 金额键；改价只能走 `/price/` 单独留痕）。
   * ⚠️ 成功才关弹层（失败只弹后端 msg 原文、弹层留着）；成功路径 `reload()` 在 `msg` 之前。
   */
  function submitOrderType(): void {
    const node = typePanel()
    const target = typeTarget
    if (!node || target === null) return
    const picked = node.querySelector<HTMLInputElement>('input[name="selbussType"]:checked')
    if (!picked) {
      msg(ORDER_TYPE_REQUIRED_MESSAGE)
      return
    }
    const orderType = BUSS_TYPE_TO_ORDER_TYPE[picked.value]
    // radio `value` 是 0/1；查不到映射说明标记被改过 ⇒ 不猜类型、不发请求。
    if (orderType === undefined) {
      msg(ORDER_TYPE_REQUIRED_MESSAGE)
      return
    }
    // 工具栏来的（批量，spec §5.2）⇒ 两步式批量端点；⛔ 单笔那一支一字不改。
    // 关层时机照单笔那一支（**成功才关**，失败留层让用户重选）。
    if (target.kind === 'batch') {
      void runSelectedBatch(target, '批量改订单类型', {
        action: 'types',
        payload: { order_type: orderType },
        successMessage: batchSuccessMessage(ORDER_TYPE_SUCCESS_MESSAGE),
        // 注入页面现成的 `msg` ⇒ layui 加载失败那一支也弹得出来（⛔ 不再静默退到 `logError`）。
        msg: (text) => msg(text),
        onDone: () => {
          if (typeLayerIndex !== null) {
            ;(layer.close as (i: number) => void)(typeLayerIndex)
            typeLayerIndex = null
          }
          reload()
        },
      })
      return
    }
    const orderNo = typeDialogOrderNo ?? ''
    if (orderNo === '') return
    void updateOrder(orderNo, { order_type: orderType }).then(
      () => {
        if (typeLayerIndex !== null) {
          ;(layer.close as (i: number) => void)(typeLayerIndex)
          typeLayerIndex = null
        }
        reload()
        msg(ORDER_TYPE_SUCCESS_MESSAGE)
      },
      (e: unknown) => msg(errText(e)),
    )
  }

  /* —— 真功能 8：删除订单（工具栏「删除」`delOrderBtn`，spec §7.1 / §2.1.2） —— */

  /**
   * 第 2 跳之后发批量删除（工具栏唯一入口）。
   * ⛔ `isDelEmp` 必须显式进 payload（整批一个值 = 两跳的答案）：后端有默认值，漏传会**静默**落 `'N'`（不标记绩效行删除），用户看不见差别——不许靠后端默认值。
   * 顺序：`runBatchFlow` 保证先 `onDone()`（reload）再报成功。
   */
  const postDeleteOrder = (selection: BatchSelection, isDelEmp: 'Y' | 'N'): void => {
    void runSelectedBatch(selection, `批量删除（${isDelEmp === 'Y' ? '同步删除' : '保留'}接手人绩效）`, {
      action: 'delete',
      payload: { isDelEmp },
      successMessage: batchSuccessMessage(DELETE_SUCCESS_MESSAGE),
      // 注入页面现成的 `msg` ⇒ layui 加载失败那一支也弹得出来（⛔ 不再静默退到 `logError`）。
      msg: (text) => msg(text),
      onDone: reload,
    })
  }

  /**
   * 两跳确认（逐字文案）。⚠️ 第 2 跳的第 2 颗按钮是 `layer.confirm(text, options, yes, btn2)` 的第 4 个参数。
   * ⛔ **两跳都要自己关层**：vendored layui 的按钮处理器只对「无 yes/btn1 的第 1 颗」和「第 2 颗」自动关——第 1 跳给了 `yes` 就一层都不会被框架关。重复 `x.close` 对已移除的层是 no-op，无害。
   */
  const confirmDeleteOrder = (selection: BatchSelection): void => {
    ;(layer.confirm as (t: string, cb: (i: number) => void) => void)(
      DELETE_CONFIRM_TEXT,
      (firstIndex: number) => {
        ;(
          layer.confirm as (
            t: string,
            o: unknown,
            cb: (i: number) => void,
            cb2: (i: number) => void,
          ) => void
        )(
          DELETE_PERFORMANCE_CONFIRM_TEXT,
          { btn: [...DELETE_PERFORMANCE_BUTTONS] },
          (secondIndex: number) => {
            ;(layer.close as (i: number) => void)(secondIndex)
            ;(layer.close as (i: number) => void)(firstIndex)
            postDeleteOrder(selection, 'Y')
          },
          (secondIndex: number) => {
            ;(layer.close as (i: number) => void)(secondIndex)
            ;(layer.close as (i: number) => void)(firstIndex)
            postDeleteOrder(selection, 'N')
          },
        )
      },
    )
  }

  /** 删除的工具栏入口：真批量（勾 0 笔由前置提示、零请求）。⚠️ 可见行为对齐、形状按我方批量端点（「一次一笔」已不成立）。 */
  const onDeleteOrder = (): void => {
    const selection = batchSelectionOrNull()
    if (!selection) return
    confirmDeleteOrder(selection)
  }

  /* —— 真功能 9：重算客服提成（工具栏 `builderSellEmpOrderBtn`，spec §5.2 / §D15 / §5.5） —— */

  /** 「重算客服提成」入口：先弹确认层（文案逐字、⛔ 不改字不拆句）再走批量两步式。⚠️ 「点错了」与右上角 X 都零请求；节点门在后端判（403 由后端 msg 说话）。 */
  const onRecomputeCommission = (selection: BatchSelection): void => {
    ;(layer.confirm as (t: string, o: unknown, cb: (index: number) => void) => void)(
      RECOMPUTE_COMMISSION_CONFIRM_TEXT,
      { btn: [...RECOMPUTE_COMMISSION_CONFIRM_BUTTONS] },
      (index: number) => {
        void runSelectedBatch(selection, '批量重算客服提成', {
          action: 'recompute-commission',
          // 这颗按钮不带任何业务值（spec §D15：载荷只有 `order_nos` / `dry_run` 两个键）。
          payload: {},
          successMessage: batchSuccessMessage(RECOMPUTE_COMMISSION_SUCCESS_MESSAGE),
          // 注入页面现成的 `msg` ⇒ layui 加载失败那一支也弹得出来（⛔ 不再静默退到 `logError`）。
          msg: (text) => msg(text),
          // ⭐ D1（R9）：`onDone` **只在那一批已执行成功**时跑（`runBatchFlow` 的返回语义）
          //    ⇒ 它就是「成功才关层」的那个位置。失败（layui 加载失败 / 两发任一失败 /
          //    用户在清单层点取消）⇒ 不关层，用户能重试。
          onDone: () => {
            ;(layer.close as (i: number) => void)(index)
            reload()
          },
        })
      },
    )
  }

  /**
   * 触发一次导出：先请求拿字节、再落盘（⛔ 不用 `window.open`，理由见 `downloadOrderExport`）。
   * 失败原样弹后端 `msg`（`errText` 读 `BizError/HttpError.message`）。
   */
  const runOrderExport = (params: OrderSearchParams): void => {
    void downloadOrderExport(params)
      .then(({ blob, filename }) => {
        const url = URL.createObjectURL(blob)
        const link = document.createElement('a')
        link.href = url
        link.download = filename
        document.body.appendChild(link) // ⚠️ 必须插进文档再 click（Firefox 对游离节点不触发下载）
        link.click()
        link.remove()
        window.setTimeout(() => URL.revokeObjectURL(url), 0)
      })
      .catch((e: unknown) => msg(errText(e)))
  }

  /**
   * 工具栏「导出」（spec §2-1 两档）：
   * - **勾选档**：只送 `order_nos`（勾选行的 `order_no` 逗号串），⛔ 不叠加其余筛选
   *   （筛选控件可在勾选之后被改动 ⇒ 合取会让已勾的行静默消失）；> 50 笔 ⇒ 只弹提示、**零请求**。
   * - **不勾选档**：送 `collectSearch()` 的产物（与列表**同一份**筛选）⇒ 导出集合 = 该筛选的结果集。
   * ⛔ 两档都**不发** `page` / `page_size`（`collectSearch()` 本来就不含它们）。
   */
  const exportOrders = (): void => {
    const rows = checkedRows()
    if (rows.length > 0) {
      if (rows.length > ORDER_EXPORT_CHECKED_MAX) {
        msg(ORDER_EXPORT_TOO_MANY_MESSAGE)
        return
      }
      runOrderExport({ order_nos: rows.map((row) => row.order_no).join(',') })
      return
    }
    runOrderExport(collectSearch())
  }

  /* —— 工具栏 11 颗按钮：全部真功能（「尚未开放」0 颗） —— */
  // ⚠️ 工具栏「发内部 / 撤内部」分支已删（按钮真删）；`openThrowDialog` / `withdrawFromHall` 本身不删——行内那两颗仍走它们。
  const onToolbarClick = (event: Event): void => {
    const el = (event.target as HTMLElement).closest<HTMLElement>('[data-tb]')
    if (!el) return
    const action = el.dataset.tb ?? ''
    if (action === 'refresh' || action === 'seachBtnOk') {
      reload()
      return
    }
    if (action === 'setRecMan') {
      const selection = batchSelectionOrNull()
      if (!selection) return
      // 「无权查看」那道闸（spec §9 / §5.2）：**任一行**命中就沿用单笔那套处置
      // —— 提示「无权查看接手人」、**不开层**（fail-closed）。判据仍只看**后端给的字段**
      // （`taker_name` 的哨兵值），⛔ 不镜像权限节点。
      if (selection.rows.some((row) => row.taker_name === ASSIGN_NO_PERMISSION_TEXT)) {
        msg('无权查看接手人')
        return
      }
      // ⭐ 业务方：勾选里**只要含一张已被代练通接手的单**（行数据 `platform_push`
      // 的远端 12–15）⇒ **整体拒绝**并只提示那句、零请求（⛔ 不静默跳过它只指派剩下的——
      // 那会让人以为都指了）。与行内 `.jsr` 同一个判据 `isTakenOnPlatform`。
      if (selection.rows.some((row) => isTakenOnPlatform(row.platform_push))) {
        msg(ASSIGN_PLATFORM_TAKEN_MESSAGE)
        return
      }
      // ⚠️ 批量时三个「从本单取值」的初值**一律传空**（spec §5.2）：N 笔没有「本单」可言，
      //    拿第一行（或任何一行）去预填都会让人以为只对那一笔生效。
      openAssignDialog(
        { orderNo: '', currentTaker: '', orderProject: '', orderHallPrice: '' },
        selection,
      )
      return
    }
    if (action === 'changetypesBtn') {
      // 工具栏＝批量两步式（勾 1 笔也是批量）；⛔ 行内那支没有，改类型只有这一个入口。
      const selection = batchSelectionOrNull()
      if (!selection) return
      openTypeDialog('', selection)
      return
    }
    if (action === 'changeStaBtn') {
      // 工具栏＝批量两步式（spec §D11）；弹层里的可选项来自后端读取形态（见 `openStatusDialog`）。
      const selection = batchSelectionOrNull()
      if (!selection) return
      // ⭐ 业务方拍板：勾选里**只要含一张还有活跃平台订单的单**
      // （行数据 `platform_push.is_active === true`）⇒ **整体拒绝**并列出被挡单号，
      // ⛔ 不静默跳过已抛单只改剩下的（那会让人以为都改了）。
      // 全部推送结束 ⇒ 恢复普通内部订单 ⇒ 照常走批量流程（与行内入口同一个判据）。
      // 后端 A2 的闸仍兜底。
      const pushedOrderNos = selection.rows
        .filter((row) => hasActivePlatformPush(row.platform_push))
        .map((row) => row.order_no)
      if (pushedOrderNos.length > 0) {
        msg(batchStatusPushedMessage(pushedOrderNos))
        return
      }
      openStatusDialog('', selection)
      return
    }
    if (action === 'changeOrderPriceBtn') {
      const selection = batchSelectionOrNull()
      if (!selection) return
      // 批量没有「本单当前价」⇒ 预填留空（`current = null`）。
      openPricePrompt('', null, selection)
      return
    }
    if (action === 'changeOrderThrowPriceBtn') {
      const selection = batchSelectionOrNull()
      if (!selection) return
      // 对标工具栏那颗传 `fee = 0`（`orderlist.js:3569`）⇒ 默认值就是 `'0'`（批量同样保留它）。
      openHallPricePrompt('', '0', '改发单价，可为0，但不能为空，带%号代表按接单价比例发单', selection)
      return
    }
    if (action === 'setOrderJSBtn') {
      // 批量没有「本单当前码」⇒ 不预勾选（`current = ''`）。
      const selection = batchSelectionOrNull()
      if (!selection) return
      openPaymentStatusDialog('', '', selection)
      return
    }
    if (action === 'refundOrderBtn') {
      const selection = batchSelectionOrNull()
      if (!selection) return
      openRefundDialog('', selection)
      return
    }
    if (action === 'delOrderBtn') {
      onDeleteOrder()
      return
    }
    if (action === 'builderSellEmpOrderBtn') {
      // 先弹确认再走批量；⛔ 权限不在这里判（后端节点门，403 由后端说话）。
      const selection = batchSelectionOrNull()
      if (!selection) return
      onRecomputeCommission(selection)
      return
    }
    if (action === 'exportBtn') {
      exportOrders()
      return
    }
    if (action === RECEIPT_SYNC_EVENT) {
      // 按**录单时间**范围手动跑一轮「同步收款」。弹层只收两个日期；
      // 提交后端点**立刻**返回 `{accepted: true}`，逐笔查询在后台线程里跑（不阻塞页面）。
      openReceiptSyncDialog()
      return
    }
    notOpen((el.textContent ?? '').trim().replace(/\s+/g, ' ') || action)
  }

  /**
   * 「同步收款」弹层：默认最近 7 天；提交前 `receiptSyncRangeError` 先拦（⛔ 不镜像后端别的判据——500 单上限/正在运行由后端回 msg 原样显示）；成功只提示一句（页面不轮询、不显示假进度）。
   */
  function openReceiptSyncDialog(): void {
    const { from, to } = defaultReceiptSyncRange(new Date())
    // ⛔ `content` 一律给 HTML 字符串（线上实测）：传 HTMLElement ⇒ `content.parents is not a function` 弹层不出现；传 `layui.$(node)` ⇒ 只出阴影内容不显示。`type:1`+字符串由 layer 自己建 DOM。
    // ⚠️ 因此事件不能在 open 之前绑 ⇒ open 之后按 `success` 回调取节点绑。
    const html =
      '<div data-receipt-sync style="padding:18px 20px 6px">' +
      '<div class="layui-form-item">' +
      '<label class="layui-form-label">开始日期</label>' +
      `<div class="layui-input-inline"><input type="date" class="layui-input" data-field="date-from" value="${from}"></div>` +
      '</div>' +
      '<div class="layui-form-item">' +
      '<label class="layui-form-label">结束日期</label>' +
      `<div class="layui-input-inline"><input type="date" class="layui-input" data-field="date-to" value="${to}"></div>` +
      '</div>' +
      `<div style="color:#999;margin:0 0 12px 110px">按录单时间统计；单次最多 ${RECEIPT_SYNC_MAX_DAYS} 天。` +
      '查询在后台进行，开始后可以先做别的事。</div>' +
      '<div style="text-align:right;padding-bottom:6px">' +
      '<button class="layui-btn layui-btn-primary layui-btn-sm" type="button" data-receipt-action="cancel">取消</button>' +
      '<button class="layui-btn layui-btn-sm" type="button" data-receipt-action="start">开始同步</button>' +
      '</div>' +
      '</div>'

    let index: number | null = null
    const close = (): void => {
      if (index !== null) (layer.close as (i: number) => void)(index)
    }

    /** open 之后从**弹层自己的 DOM**里取控件并绑事件（⛔ 不缓存 open 之前的引用）。 */
    const bind = (): void => {
      const dialog = document.querySelector<HTMLElement>('[data-receipt-sync]')
      if (!dialog) return
      const fromInput = dialog.querySelector<HTMLInputElement>('[data-field="date-from"]')
      const toInput = dialog.querySelector<HTMLInputElement>('[data-field="date-to"]')
      dialog
        .querySelector<HTMLElement>('[data-receipt-action="cancel"]')
        ?.addEventListener('click', close)
      dialog
        .querySelector<HTMLElement>('[data-receipt-action="start"]')
        ?.addEventListener('click', () => {
          const dateFrom = fromInput?.value ?? ''
          const dateTo = toInput?.value ?? ''
          const error = receiptSyncRangeError(dateFrom, dateTo)
          if (error !== null) {
            msg(error)
            return
          }
          // ⚠️ 前端不写「正在跑来着」的本地状态：真正的单例在后端（advisory lock + 1037），再判一次只会与后端漂移。
          startXgjReceiptSync({ date_from: dateFrom, date_to: dateTo })
            .then(() => {
              close()
              msg(RECEIPT_SYNC_ACCEPTED_TEXT)
            })
            .catch((error: unknown) => {
              // 后端 `msg` 优先（1037/1000 都是人话），页面原样显示、不自己拼。
              msg(errText(error))
            })
        })
    }

    index = (layer.open as (o: unknown) => number)({
      title: RECEIPT_SYNC_DIALOG_TITLE,
      skin: 'layui-layer-lan',
      shade: [0.3, '#333'],
      shadeClose: true,
      type: 1,
      anim: 5,
      area: ['460px', 'auto'],
      content: html,
      success: () => {
        bind()
      },
      end: () => {
        index = null
      },
    })
  }

  /* —— 表体/行内链接 —— */

  /**
   * 「进订单详情」：原地开层装我们自己的复刻页（`type:2` iframe）。
   * ⛔ `area` 是每页自己的值，不许抽共用常量（同产品各页尺寸各不相同）。
   * ⛔ 开层前的单号形状闸 `/^\d{20}$/`（与路由约束同源）**必须保留**：`layer.open` 的 `title` 是 HTML 上下文，`dataset` 会把 attribute 实体解码回 `<`/`>` ⇒ templet 那层转义在这条链上不构成保护；不匹配 ⇒ 不开层、只给固定文案（⛔ 不把不可信输入拼进提示）。
   * ⛔ `viewNo`（title 前缀，来源单号是自由文本）单独过 `escapeHtml`——HTML 上下文边界上转义才有效。
   */
  const openOrderDetailLayer = (orderNo: string, viewNo: string): void => {
    if (!/^\d{20}$/.test(orderNo)) {
      // 固定文案（⛔ 不拼 `orderNo`——它此刻正是「形状不可信」的那份输入）。
      msg('订单号格式不正确，无法打开订单详情')
      return
    }
    ;(layer.open as (o: unknown) => void)({
      title: `${escapeHtml(viewNo !== '' ? viewNo : orderNo)}订单详情`,
      shade: [0.3, '#333'],
      shadeClose: true,
      maxmin: false,
      type: 2,
      skin: 'layui-layer-lan',
      anim: 8,
      area: ['85%', '92%'],
      content:
        `${import.meta.env.BASE_URL}layui-island/page.html?page=order-orderview` +
        `&order_no=${encodeURIComponent(orderNo)}`,
    })
  }

  /**
   * 「上传截图」/「图片管理」两颗按钮 → 原地开层装我们的两个岛页。
   * ⛔ 两页 `anim` 不同（8 与 1），别抽共用常量；⛔ 不带对标的 `viewType=20`（岛页只认 `order_no`）；⛔ 手机分支不接。
   * ⛔ 开层前的单号形状闸 `/^\d{20}$/` 必须保留（`title` 是 HTML 上下文、dataset 解码实体，templet 那层转义不构成保护）；不匹配 ⇒ 不开层 + 固定文案。
   */
  const openScreenshotLayer = (
    page: string, title: string, anim: number, area: [string, string], orderNo: string,
  ): void => {
    if (!/^\d{20}$/.test(orderNo)) {
      msg('订单号格式不正确，无法打开订单截图')
      return
    }
    ;(layer.open as (o: unknown) => void)({
      title,
      shade: [0.3, '#333'],
      shadeClose: true,
      type: 2,
      skin: 'layui-layer-lan',
      anim,
      area,
      content:
        `${import.meta.env.BASE_URL}layui-island/page.html?page=${page}` +
        `&order_no=${encodeURIComponent(orderNo)}`,
    })
  }

  /**
   * 本页**唯一**的列定义出口：主表 `table.render` 与「拆单展开时插的那几行」共用它
   * ⇒ 子行的 23 格与主行**逐列同源**（⛔ 不在插行那段另写一份列定义，两份必然漂移）。
   *
 * `splitChild = true` ⇒ 子行保留 `sontr` 标记，并与父行共用订单号前的固定槽位。
   */
  const buildRowCols = (splitChild = false): OrderCol[] =>
    buildOrderCols(
      (id) => gameNames.get(id) ?? String(id),
      (id) => (id === null ? '' : (serverNames.get(id) ?? String(id))),
      { isGroupGame: isGroupGameForRowButtons, splitChild },
    )

  /* ── 拆单组成员跟在列表入口下方 ────────
   * 取数 = `GET /api/business/orders/?split_id=<组>`（回来的是整组含主单）——⛔ 不从当前页那几行里凑（同组成员可能被筛选挡住，那正是「展开」要解决的事）。
   * ⛔ 两张表都要插（操作列 `fixed:'right'` ⇒ layui 另画一张固定列表，只插主表会错行）。
   * ⚠️ 插入的行**不进 layui 数据缓存**（有意）：批量/排序/翻页仍按主表走；子单自己的入口一个不动。
   */
  /** 点中的那一行所在的两个表体 + 固定列表里对应的那一行（同一张表实例内）。 */
  function splitBodies(tr: HTMLTableRowElement): {
    fixed: HTMLElement | null
    fixedTr: HTMLTableRowElement | null
  } {
    const view = tr.closest('.layui-table-view')
    const index = tr.dataset.index ?? ''
    if (view === null || index === '') return { fixed: null, fixedTr: null }
    const fixed = view.querySelector<HTMLElement>('.layui-table-fixed-r tbody')
    const fixedTr =
      fixed === null
        ? null
        : fixed.querySelector<HTMLTableRowElement>(`tr[data-index="${index}"]`)
    return { fixed, fixedTr }
  }

  /** 插入子行时继承对应父行 td 与 layui-cell 骨架，保证列宽、间距与固定列一致。 */
  function buildInjectedRow(
    row: OrderListItem,
    cols: OrderCol[],
    parentTr: HTMLTableRowElement,
  ): HTMLTableRowElement {
    const tr = document.createElement('tr')
    tr.className = SPLIT_CHILD_ROW_CLASS
    tr.dataset.order = row.order_no
    const parentCells = Array.from(parentTr.children).filter(
      (cell): cell is HTMLTableCellElement => cell instanceof HTMLTableCellElement,
    )
    cols.forEach((col, index) => {
      const parentTd = parentCells[index]
      const td = document.createElement('td')
      if (parentTd !== undefined) {
        td.className = parentTd.className
        const parentStyle = parentTd.getAttribute('style')
        if (parentStyle !== null) td.setAttribute('style', parentStyle)
      }
      if (typeof col.field === 'string') td.dataset.field = col.field
      if (col.hide) td.classList.add('layui-hide')
      const raw = (row as unknown as Record<string, unknown>)[col.field ?? '']
      const text = raw === null || raw === undefined ? '' : String(raw)
      const inner = document.createElement('div')
      const parentInner = parentTd?.firstElementChild
      inner.className =
        parentInner instanceof HTMLElement && parentInner.className !== ''
          ? parentInner.className
          : 'layui-table-cell'
      const alignment = parentInner?.getAttribute('align') ?? col.align
      if (alignment) inner.setAttribute('align', alignment)
      if (col.type === 'checkbox') {
        // 子行没有进 layui 表格缓存，原生复选框直接更新页面的勾选集合。
        const checkbox = document.createElement('input')
        checkbox.type = 'checkbox'
        checkbox.setAttribute('lay-ignore', '')
        checkbox.setAttribute('aria-label', `勾选 ${row.source_order_no || row.order_no}`)
        checkbox.style.cssText = 'display:inline-block;width:16px;height:16px;vertical-align:middle;cursor:pointer'
        checkbox.addEventListener('click', (event) => event.stopPropagation())
        checkbox.addEventListener('change', () => {
          onTableCheckbox({ type: 'one', checked: checkbox.checked })
        })
        inner.appendChild(checkbox)
      } else {
        inner.innerHTML = col.templet ? col.templet(row) : escapeHtml(text)
      }
      td.appendChild(inner)
      tr.appendChild(td)
    })
    return tr
  }

  async function showSplitMembers(line: HTMLElement): Promise<void> {
    const splitId = line.dataset.splitId ?? ''
    const tr = line.closest<HTMLTableRowElement>('tr')
    if (splitId === '' || tr === null || line.dataset.splitLoaded) return
    line.dataset.splitLoaded = 'true'
    const groupRow = mainRows.get(line.id.slice(ORDER_LINE_ID_PREFIX.length))
    let members: OrderListItem[]
    try {
      members = await fetchSplitMembers(splitId)
    } catch (e) {
      delete line.dataset.splitLoaded
      msg(e instanceof Error ? e.message : '拆单成员取数失败')
      return
    }
    if (!root.contains(line) || !groupRow || mainRows.get(groupRow.order_no) !== groupRow) return
    const orderNo = groupRow.order_no
    const others = members.filter((row) => row.order_no !== orderNo)
    if (others.length === 0) return
    const childCols = buildRowCols(true)
    const injectedRows = others.map((row) => buildInjectedRow(row, childCols, tr))
    const orderCell = line.closest<HTMLElement>('td')
    if (orderCell) orderCell.dataset.splitLine = 'start'
    injectedRows.forEach((row, index) => {
      const cell = row.querySelector<HTMLElement>('td[data-field="_orderid"]')
      if (cell) cell.dataset.splitLine = index === injectedRows.length - 1 ? 'end' : 'branch'
    })
    const mainFrag = document.createDocumentFragment()
    for (const member of others) splitRows.set(member.order_no, member)
    for (const row of injectedRows) mainFrag.appendChild(row)
    tr.after(mainFrag)
    const { fixed, fixedTr } = splitBodies(tr)
    if (fixed !== null && fixedTr !== null) {
      // 固定列那张表**只画固定列**（本页只有「操作」那一列）⇒ 按同一批列定义过滤。
      const fixedCols = childCols.filter((col) => col.fixed === 'right')
      const fixedFrag = document.createDocumentFragment()
      const insertedFixedRows = others.map((row) => buildInjectedRow(row, fixedCols, fixedTr))
      for (const row of insertedFixedRows) fixedFrag.appendChild(row)
      fixedTr.after(fixedFrag)
      injectedRows.forEach((mainRow, rowIndex) => {
        const fixedRow = insertedFixedRows[rowIndex]
        if (fixedRow === undefined) return
        const rowHeight = mainRow.getBoundingClientRect().height
        if (rowHeight > 0) fixedRow.style.height = `${rowHeight}px`
      })
    }
  }

  /* ── ⭐ P3 / P6 / P7：平台推送行的读取 / 三角展开 / 两个动作 ─────────
   *
   * 数据只有一处来源：`GET /api/business/orders/<order_no>/platform-push/`（三件事共用）。
   * ⚠️ **一轮加载只取一次**（业务方口径「缓存一轮」）：`platformRowsCache` 按单号缓存，
   * `platformRowsInFlight` 让同一单的并发点击共用同一个 Promise；`done`（每次取数）清空缓存。
   * ⛔ 不要改成每次点击都发请求：三角 → 申请撤销 → 再展开 会在同一屏打三发。
   */
  let platformRowsCache = new Map<string, DailiantongPushRow[]>()
  let platformRowsInFlight = new Map<string, Promise<DailiantongPushRow[]>>()

  /** 取该单全部推送行（缓存 + 在途合并）。失败原样抛出，由调用方给中文提示。 */
  function loadPlatformPushRows(orderNo: string): Promise<DailiantongPushRow[]> {
    const cached = platformRowsCache.get(orderNo)
    if (cached !== undefined) return Promise.resolve(cached)
    const pending = platformRowsInFlight.get(orderNo)
    if (pending !== undefined) return pending
    const task = fetchOrderPlatformPushes(orderNo).then(
      (rows) => {
        platformRowsInFlight.delete(orderNo)
        const list = Array.isArray(rows) ? rows : []
        platformRowsCache.set(orderNo, list)
        return list
      },
      (error: unknown) => {
        platformRowsInFlight.delete(orderNo)
        throw error
      },
    )
    platformRowsInFlight.set(orderNo, task)
    return task
  }


  /** 删掉紧跟在这一行后面的平台子行（主表 / 固定列各一次）。 */
  function removePlatformChildRows(tr: HTMLTableRowElement): void {
    let next = tr.nextElementSibling
    while (
      next instanceof HTMLTableRowElement &&
      next.classList.contains(PLATFORM_CHILD_ROW_CLASS)
    ) {
      const doomed = next
      next = next.nextElementSibling
      doomed.remove()
    }
  }

  /** 把某个三角收起（图标 / `data-open` / 两张表的子行一起复原）。 */
  function collapsePlatformChildren(
    tr: HTMLTableRowElement,
    triangle: HTMLElement,
  ): void {
    removePlatformChildRows(tr)
    const { fixed, fixedTr } = splitBodies(tr)
    if (fixed !== null && fixedTr !== null) removePlatformChildRows(fixedTr)
    triangle.classList.remove('layui-icon-triangle-d')
    triangle.classList.add('layui-icon-triangle-r')
    triangle.dataset.open = 'N'
  }

  /** ⭐ B3：照父行自己的单元格骨架造一条平台子单真行——layui 的 padding/行高/省略号都在 `.layui-table-cell`（含表号/列号），照父行复制 ⇒ 列宽/行高/边框/对齐全继承，⛔ 一个像素都不用猜。`data-index` 故意不写（子行不进 layui 数据缓存）。 */
  function buildPlatformChildRow(
    parentTr: HTMLTableRowElement,
    row: DailiantongPushRow,
    orderNo: string,
  ): HTMLTableRowElement {
    const tr = document.createElement('tr')
    tr.className = PLATFORM_CHILD_ROW_CLASS
    tr.dataset.order = orderNo
    for (const parentTd of Array.from(parentTr.children)) {
      if (!(parentTd instanceof HTMLTableCellElement)) continue
      const td = document.createElement('td')
      // 骨架三件套：class / data-field / style 逐项照抄（样式里可能有 layui 写的列宽/对齐）。
      td.className = parentTd.className
      if (parentTd.dataset.field !== undefined) td.dataset.field = parentTd.dataset.field
      const style = parentTd.getAttribute('style')
      if (style !== null) td.setAttribute('style', style)
      const inner = parentTd.firstElementChild
      const cell = document.createElement('div')
      // 内层 `.layui-table-cell` 的类名同样照抄（它带着表号/列号，CSS 只认那一份）。
      cell.className =
        inner instanceof HTMLElement && inner.className !== ''
          ? inner.className
          : 'layui-table-cell'
      cell.innerHTML = platformChildCellHtml(parentTd.dataset.field ?? '', row, orderNo)
      td.appendChild(cell)
      tr.appendChild(td)
    }
    return tr
  }

  /** P6/B3：点三角 = 取推送行 → 父行下方插真行；再点收起。⛔ 两张表都要插（`fixed:'right'` 另画一张固定列表，只插主表会错行），各按自己那张父行的骨架造；⛔ 不动 layui 数据缓存；同一颗三角在途再点不重入。 */
  async function togglePlatformChildren(triangle: HTMLElement): Promise<void> {
    const orderNo = triangle.dataset.order ?? ''
    const tr = triangle.closest<HTMLTableRowElement>('tr')
    if (orderNo === '' || tr === null) return
    if (triangle.dataset.open === 'Y') {
      collapsePlatformChildren(tr, triangle)
      return
    }
    // 同一颗三角在途时再点 ⇒ 不重入（否则会插两份一样的子行）。
    if (triangle.dataset.busy === 'Y') return
    triangle.dataset.busy = 'Y'
    let rows: DailiantongPushRow[]
    try {
      rows = await loadPlatformPushRows(orderNo)
    } catch (e) {
      msg(`${PLATFORM_CHILD_FAILED_PREFIX}${errText(e)}`)
      return
    } finally {
      triangle.dataset.busy = ''
    }
    if (rows.length === 0) {
      // 列表摘要说「有推送」但读面回空（并发删除 / 权限收窄）⇒ 如实说，⛔ 不装作已展开。
      msg(PLATFORM_CHILD_EMPTY_MESSAGE)
      return
    }
    // 对标一次只展开一个（点另一行时先把别的收起）。
    for (const other of Array.from(
      root.querySelectorAll<HTMLElement>(`${PLATFORM_TREE_SELECTOR}[data-open="Y"]`),
    )) {
      if (other === triangle) continue
      const otherTr = other.closest<HTMLTableRowElement>('tr')
      if (otherTr !== null) collapsePlatformChildren(otherTr, other)
    }
    // 主表：每条推送行一条真行（顺序照读面 = 新在前）。
    const mainFrag = document.createDocumentFragment()
    for (const row of rows) mainFrag.appendChild(buildPlatformChildRow(tr, row, orderNo))
    tr.after(mainFrag)
    // 右侧固定列表：**照那张表自己的父行**再造一遍（它只有「操作」一格）。
    const { fixed, fixedTr } = splitBodies(tr)
    if (fixed !== null && fixedTr !== null) {
      const fixedFrag = document.createDocumentFragment()
      for (const row of rows) fixedFrag.appendChild(buildPlatformChildRow(fixedTr, row, orderNo))
      fixedTr.after(fixedFrag)
    }
    triangle.classList.remove('layui-icon-triangle-r')
    triangle.classList.add('layui-icon-triangle-d')
    triangle.dataset.open = 'Y'
  }

  /** 撤回的**真实结果**文案（按返回行判，⛔ 不复述后端的固定 msg）。 */
  function pushWithdrawResultMessage(row: DailiantongPushRow): string {
    if (row.state === 'closed') return PUSH_WITHDRAW_CLOSED_MESSAGE
    if (typeof row.remote_cancel_status === 'number' && row.remote_cancel_status !== 10) {
      return PUSH_WITHDRAW_CANCEL_PENDING_MESSAGE
    }
    return PUSH_WITHDRAW_UNVERIFIED_MESSAGE
  }

  /** 正在「从所有平台撤回」的单号（连点闸：一次确认只走一轮）。 */
  const withdrawingPlatforms = new Set<string>()

  /**
   * P3：从所有第三方交易平台撤回（未接手的单）：逐平台 `POST …/withdraw/`、逐平台回结果（部分成功如实报、失败行显示后端中文原文）；有一台成功 ⇒ 刷新当前页。
   * ⛔ 一行推送都没有 ⇒ 提示、零写请求、不假装成功。
   * ⛔ 按平台去重：同一平台只发一次（撤回是「单+平台」一个动作、后端选最新有效行，重复发会撞「没有有效的推送记录」）。
   */
  async function withdrawFromAllPlatforms(orderNo: string): Promise<void> {
    if (withdrawingPlatforms.has(orderNo)) return
    withdrawingPlatforms.add(orderNo)
    try {
      let rows: DailiantongPushRow[]
      try {
        rows = await loadPlatformPushRows(orderNo)
      } catch (e) {
        msg(errText(e))
        return
      }
      // 「抛到平台」= 有平台单号且链路还活着（`failed` / `closed` 没有可撤的远端单）。
      const active = rows.filter(
        (row) =>
          (row.serial_no ?? '').trim() !== '' &&
          row.state !== 'closed' &&
          row.state !== 'failed',
      )
      if (active.length === 0) {
        msg(PUSH_JDP_NO_PLATFORM_MESSAGE)
        return
      }
      const byPlatform = new Map<number, DailiantongPushRow>()
      for (const row of active) {
        if (!byPlatform.has(row.platform)) byPlatform.set(row.platform, row)
      }
      const lines: string[] = []
      let anyOk = false
      for (const [platform, row] of byPlatform) {
        const label = platformLabelOf(platform)
        const serial = (row.serial_no ?? '').trim() || '—'
        try {
          const updated = await withdrawOrderPlatformPush(orderNo, { platform })
          anyOk = true
          lines.push(`「${label}」${serial}：${pushWithdrawResultMessage(updated)}`)
        } catch (e) {
          lines.push(`「${label}」${serial}：${errText(e)}`)
        }
      }
      if (anyOk) {
        platformRowsCache.delete(orderNo)
        reload(currentPage)
      }
      // 对标成功/失败都 `layer.alert(data.msg)`（`orderlist.js:4716-4726`）⇒ 这里合并成一块。
      ;(layer.alert as (text: string) => void)(
        `${escapeHtml(PUSH_JDP_WITHDRAW_RESULT_TITLE)}<br>` +
          lines.map((line) => escapeHtml(line)).join('<br>'),
      )
    } finally {
      withdrawingPlatforms.delete(orderNo)
    }
  }

  /** P3：确认语 + 两个按钮逐字照对标（`orderlist.js:4702-4704`），确认后才发请求。 */
  function confirmWithdrawAll(orderNo: string): void {
    if (orderNo === '' || withdrawingPlatforms.has(orderNo)) return
    ;(
      layer.confirm as unknown as (
        text: string,
        options: unknown,
        onConfirm: (index: number) => void,
      ) => void
    )(
      PUSH_JDP_CANCEL_CONFIRM_TEXT,
      { btn: [PUSH_JDP_CANCEL_OK_TEXT, PUSH_JDP_CANCEL_NO_TEXT] },
      (index: number) => {
        ;(layer.close as (i: number) => void)(index)
        void withdrawFromAllPlatforms(orderNo)
      },
    )
  }

  const onCellClick = (event: Event): void => {
    const target = event.target as HTMLElement
    // ⭐ P6：订单号左侧三角 ⇒ 展开 / 收起该单的平台子订单。
    const treeEl = target.closest<HTMLElement>(PLATFORM_TREE_SELECTOR)
    if (treeEl) {
      void togglePlatformChildren(treeEl)
      return
    }
    // ⭐ G1（差距表 ❌4 / ❌7）：两个行内编辑入口。
    //  - 「标记」徽标（`a.changeTag`）⇒ 编辑订单标记；
    //  - 代练/陪玩内容（`a.modityProject`）⇒ 编辑项目。
    // 两条都必须在下面那些 `[data-label]` / 通用兜底**之前**（它们不带 data-label，
    // 但顺序写清楚免得以后被别的分支吃掉）。
    const tagBadgeEl = target.closest<HTMLElement>(ORDER_TAG_BADGE_SELECTOR)
    if (tagBadgeEl) {
      const orderNo = tagBadgeEl.dataset.order ?? ''
      if (orderNo !== '') openTagPrompt(orderNo, tagBadgeEl.dataset.tag ?? '')
      return
    }
    const projectEl = target.closest<HTMLElement>(PROJECT_EDIT_SELECTOR)
    if (projectEl) {
      const orderNo = projectEl.dataset.order ?? ''
      if (orderNo !== '') openProjectPrompt(orderNo, projectEl.dataset.project ?? '')
      return
    }
    // ⭐ P6：子行里的「编辑」⇒ 打开本系统订单详情（代练通页签里可改价 / 补时）。
    //    ⛔ 不假装能编辑平台子单的标题 / 内容（对标那颗编辑的端点本节没有）。
    const childEditEl = target.closest<HTMLElement>(PLATFORM_CHILD_EDIT_SELECTOR)
    if (childEditEl) {
      const orderNo = childEditEl.dataset.order ?? ''
      if (orderNo !== '') {
        const shown =
          root.querySelector<HTMLElement>(`#order_${orderNo} .orderdetails`)?.dataset.viewid
            ?.trim() || orderNo
        openOrderDetailLayer(orderNo, shown)
      }
      return
    }
    // ⭐ P3：箭头 = 从所有第三方交易平台撤回（确认后才发请求）。
    //    ⚠️ 定案后本页**只有这一颗平台写动作**（协商撤销 / 加款 / 重发已搬走）。
    const pushCancelEl = target.closest<HTMLElement>(`a.${PUSH_JDP_CANCEL_CLASS}`)
    if (pushCancelEl) {
      confirmWithdrawAll(pushCancelEl.dataset.id ?? '')
      return
    }
    const openRow = (orderNo: string, viewNo: string): void => {
      if (orderNo !== '') openOrderDetailLayer(orderNo, viewNo)
    }
    const statusEl = target.closest<HTMLElement>('.setdlsta')
    if (statusEl) {
      const orderNo = statusEl.dataset.order ?? ''
      // ⭐ 有**活跃**平台订单才禁用入口（只弹提示）；全部结束 ⇒ 正常开弹窗。
      if (orderNo !== '') {
        openStatusDialog(orderNo, { kind: 'single', orderNo }, statusEl.dataset.push === 'Y')
      }
      return
    }
    // 「接单价」列上的收款状态标记（`.jsSta`）：**行级动作、不走工具栏那条批量前置**
    // （即 `batchSelectionOrNull()`；Task 4b 之前那句写的是已删除的 `oneChecked()`）
    // （与 `.setdlsta` → `openStatusDialog` 同形：点哪一行就开哪一行；对标 `orderlist.js:4113-4118`
    // 也是 `$(document).on('click','.jsSta',…)`）。
    // 当前档从标记上那个**不可见**属性 `data-sta` 读（T7 的 `priceRow` 产出，
    // 与 `takerRow` 的 `data-taker` 同一处置——行内点击拿不到整行对象）。
    const staEl = target.closest<HTMLElement>('.jsSta')
    if (staEl) {
      const orderNo = staEl.dataset.order ?? ''
      if (orderNo !== '') openPaymentStatusDialog(orderNo, staEl.dataset.sta ?? '')
      return
    }
    // 「接单价」列金额链接：⛔ 读 `data-fee` 不读 `textContent`（文本是格式化过的串，请求要原值）。
    const priceEl = target.closest<HTMLElement>('a.editOrderPrice')
    if (priceEl) {
      const orderNo = priceEl.dataset.order ?? ''
      if (orderNo !== '') openPricePrompt(orderNo, priceEl.dataset.fee ?? null)
      return
    }
    // 「发单价」列三支（判据取自 `TAKER_PERF_BRANCH`，写在 `data-branch`）：①②走同一 `/hall-price/` 出口（服务端是同一个动作，`action` 不引入）；③开绩效弹层。与 `.editOrderPrice` 同源：读 `data-fee`（原值）。
    const throwEditEl = target.closest<HTMLElement>('a.changeThrowFeeEdit')
    if (throwEditEl) {
      const orderNo = throwEditEl.dataset.order ?? ''
      if (orderNo !== '')
        openHallPricePrompt(orderNo, throwEditEl.dataset.fee ?? null, '更改发单价，可为0，但不能为空')
      return
    }
    const throwFeeEl = target.closest<HTMLElement>('a.changeThrowFee')
    if (throwFeeEl) {
      const orderNo = throwFeeEl.dataset.order ?? ''
      if (orderNo !== '') {
        if (throwFeeEl.dataset.branch === 'dialog') openPerformanceDialog(orderNo)
        else
          openHallPricePrompt(
            orderNo,
            throwFeeEl.dataset.fee ?? null,
            '改发单价，可为0，但不能为空，带%号代表按接单价比例发单',
          )
      }
      return
    }
    // 「退款」列上的金额链接（`.refundFee`；该列**默认显示**，见「已知差异 22」）。
    // 与工具栏「记退款」开**同一个**弹层。
    // ⚠️ **行内单笔入口预填当前累计**（`data-fee`）：口径统一成「累计退款总额」后，
    // 预填它正是「改小 / 改回去」的入口（见 `openRefundDialog` 与「已知差异 15/17」）。
    // ⛔ 工具栏批量入口**不**传预填（多张单没有「本单当前值」）。
    const refundEl = target.closest<HTMLElement>('a.refundFee')
    if (refundEl) {
      const orderNo = refundEl.dataset.order ?? ''
      if (orderNo !== '') openRefundDialog(orderNo, undefined, refundEl.dataset.fee ?? '')
      return
    }
    const actEl = target.closest<HTMLElement>('[data-act]')
    if (actEl) {
      const orderNo = actEl.dataset.order ?? ''
      const act = actEl.dataset.act ?? ''
      // ⚠️ 这条分支今天不可达（全仓没有产出 `data-act="edit"` 的地方；真正行内编辑走 `.submitEdit`）。不删——将来真有人产出时它与「跳订单详情」同义。
      if (act === 'edit') {
        openRow(orderNo, actEl.dataset.viewid ?? '')
        return
      }
      if (act === 'price' && orderNo !== '') {
        openPricePrompt(orderNo, null)
        return
      }
      notOpen(actEl.dataset.label ?? (actEl.textContent ?? '').trim())
      return
    }
    // 末列「操作」图标按钮按 class 分派；其余对标有、我们没有 → 「尚未开放」。
    const throwEl = target.closest<HTMLElement>('a.throwEdit, a.cancelThrowHome')
    if (throwEl) {
      const orderNo = throwEl.dataset.id ?? ''
      if (orderNo === '') return
      if (throwEl.classList.contains('throwEdit')) openThrowDialog(orderNo)
      else withdrawFromHall(orderNo)
      return
    }
    // 地球按钮：必须排在通用 `opEl` 之前，否则会被「尚未开放」分支先吃掉。
    const pushEl = target.closest<HTMLElement>(PUSH_JDP_SELECTOR)
    if (pushEl) {
      openPushJdpLayer(pushEl.dataset.id ?? '', pushEl.dataset.viewid ?? '')
      return
    }
    // ⭐ G2：`sendsmsorderbtn` 已从操作列整颗删除 ⇒ 也不在这里的兜底选择器里（否则留一条死路径）。
    const opEl = target.closest<HTMLElement>('a.submitEdit, a.joinTask')
    if (opEl) {
      const orderNo = opEl.dataset.id ?? ''
      if (opEl.classList.contains('submitEdit')) {
        // ⭐ D-0301：第二参 = 展示单号（弹层标题用；`data-id` 是寻址值、照旧内置号）。
        openEditDialog(orderNo, opEl.dataset.viewid ?? '')
        return
      }
      // 文案取 `lay-tips`（对标这些图标按钮**没有可见文案**，文字只在 tips 里）：
      // 现在只剩「将订单加入到排团」。
      notOpen((opEl.getAttribute('lay-tips') ?? '').trim() || orderNo)
      return
    }
    // 第 15 列「接手人」格（`.jsr`）：没人接→「指派打手」、有人接→名字（点击=改派）；「无权查看」是纯文本不带 `jsr` ⇒ 点它匹配不到这里（正是对的）。
    const takerEl = target.closest<HTMLElement>('a.jsr')
    if (takerEl) {
      const orderNo = takerEl.dataset.order ?? ''
      if (orderNo !== '') {
        // ⛔ 已被代练通接手 ⇒ 不提供指派（只提示、零请求）；判据由渲染期写进 `data-push-taken`，⛔ 这里不重新判状态、不镜像后端状态机。
        if (takerEl.dataset.pushTaken === '1') {
          msg(ASSIGN_PLATFORM_TAKEN_MESSAGE)
          return
        }
        openAssignDialog({
          orderNo,
          currentTaker: takerEl.dataset.taker ?? '',
          // `data-project` 是本批补的不可见属性（T6）⇒ 旧标记缺它时回落空串（不猜初值）。
          orderProject: takerEl.dataset.project ?? '',
          // `data-hall-price`（批 E / B12）同理：那一格显示订单发单价，缺它时留空、保存不送 `price`。
          orderHallPrice: takerEl.dataset.hallPrice ?? '',
        })
      }
      return
    }
    const detailEl = target.closest<HTMLElement>('.orderdetails')
    if (detailEl) {
      openRow(detailEl.dataset.order ?? '', detailEl.dataset.viewid ?? '')
      return
    }
    // 「重要备注」列（`.orderRemark`）与「角色名」列（`.changeRoleName`）：
    // 两个行内 `<a>` 各开一个 `layer.prompt`，预填各自的 `data-*` 原值，确认后走既有 PATCH 出口。
    const remarkEl = target.closest<HTMLElement>('a.orderRemark')
    if (remarkEl) {
      const orderNo = remarkEl.dataset.order ?? ''
      if (orderNo !== '') openRemarkPrompt(orderNo, remarkEl.dataset.remark ?? '')
      return
    }
    const roleNameEl = target.closest<HTMLElement>('a.changeRoleName')
    if (roleNameEl) {
      const orderNo = roleNameEl.dataset.order ?? ''
      if (orderNo !== '') openRoleNamePrompt(orderNo, roleNameEl.dataset.rolename ?? '')
      return
    }
    // 「订单截图」列的两颗按钮：**必须在下面那条 `[data-label]` 兜底之前**，
    // 否则会被「尚未开放」吃掉（它们同时带着 `data-label`）。
    const shotEl = target.closest<HTMLElement>(SHOT_BUTTONS)
    if (shotEl) {
      const orderNo = shotEl.dataset.order ?? ''
      if (orderNo !== '') {
        if (shotEl.classList.contains(SHOT_UPLOAD_CLASS)) {
          openScreenshotLayer(
            SHOT_UPLOAD_PAGE, '上传订单截图', 8, ['70%', '85%'], orderNo,
          )
        } else {
          openScreenshotLayer(
            SHOT_MANAGE_PAGE, '查看订单截图', 1, ['80%', '85%'], orderNo,
          )
        }
      }
      return
    }
    const labelEl = target.closest<HTMLElement>('[data-label]')
    if (labelEl) notOpen(labelEl.dataset.label ?? '')
  }

  /**
   * 双击整行展开/收起平台子订单。走 root 的 `dblclick` 委托（能看见 `event.target`，从而跳过三角本身——三角单击已 toggle，双击再补一次会「展开→收起→展开」抖动）。
   * 复用 `togglePlatformChildren`（含在途闸/缓存/两张表插行），⛔ 不另写一条展开链；无三角 ⇒ 零请求。
   */
  const onRowDoubleClick = (event: MouseEvent): void => {
    const target = event.target as HTMLElement | null
    if (target === null) return
    // 三角：单击已负责，双击不重复。
    if (target.closest(PLATFORM_TREE_SELECTOR) !== null) return
    // 工具栏 / 分页条 / 表单控件不属于「这一行」。
    if (
      target.closest(
        '.layui-table-tool, .layui-table-page, input, button, select, textarea',
      ) !== null
    ) {
      return
    }
    const tr = target.closest<HTMLTableRowElement>('tr')
    if (tr === null) return
    // 平台子行 / 拆单子行不是可展开的行，双击它们不再往下钻。
    if (tr.classList.contains(PLATFORM_CHILD_ROW_CLASS) || tr.classList.contains(SPLIT_CHILD_ROW_CLASS)) {
      return
    }
    if (tr.closest('.layui-table-body') === null) return
    const triangle = tr.querySelector<HTMLElement>(PLATFORM_TREE_SELECTOR)
    if (triangle === null) return
    void togglePlatformChildren(triangle)
  }

  /** 页内 6 个隐藏弹窗容器里的保存/清除按钮（我们没有后端）＋「查看职业统计」。 */
  const onNotOpenClick = (event: Event): void => {
    const el = (event.target as HTMLElement).closest<HTMLElement>('[data-notopen], .zyTotal')
    if (!el) return
    notOpen(el.dataset.notopen ?? (el.textContent ?? '').trim())
  }

  /**
   * 统计句状态段点击：清空→设唯一项→点工具栏搜索按钮（走既有 `seachBtnOk` 分支）。⛔ 不自己调 `reload()`（那是闭包内部函数，绕过去就脱离工具栏既有链路）。
   * ⛔ 必须走 root 点击委托：统计句每次 reload 都被 `innerHTML` 整段重写，逐个 addEventListener 下次刷新就没了。
   * ⚠️ 只认统计句那一段（`closest` 命中后须确认落在统计句 span 内，`.statustab` 别处也可能出现）；统计句被隐藏时直接返回——隐藏只改 display 不清 innerHTML，旧的 `.statustab` 仍在 DOM，程序化冒泡 click 会命中（判据、不是防御性写法：去掉这行「隐藏后旧状态段不许再改筛选」那条用例会红）。
   */
  const onSummaryStatusClick = (event: Event): void => {
    const tab = (event.target as HTMLElement).closest<HTMLElement>(SUMMARY_STATUS_TAB_SELECTOR)
    if (!tab) return
    const totalEl = summaryTotalSpan(root)
    if (!totalEl || !totalEl.contains(tab)) return
    if (totalEl.style.display === 'none') return
    const status = tab.dataset.status ?? ''
    if (status === '') return
    // 中文 label 由 `statusLabel()` 现算（`data-status` 里存的是状态码；⛔ 不硬编码中文）。
    const label = statusLabel(status)
    const fs = getFormSelects()
    fs?.value(ORDER_STA_SELECT_ID, [])
    fs?.value(ORDER_STA_SELECT_ID, [label])
    root.querySelector<HTMLElement>(SEARCH_BUTTON_SELECTOR)?.click()
  }

  root.addEventListener('click', onToolbarClick)
  root.addEventListener('click', onCellClick)
  // G1（❌10）：双击整行展开 / 收起平台子单。
  root.addEventListener('dblclick', onRowDoubleClick)
  root.addEventListener('click', onNotOpenClick)
  root.addEventListener('click', onSummaryStatusClick)

  /* —— 真功能 7：订单号右键菜单（4 项，spec §6） —— */

  /**
   * 当前挂着的菜单节点与它承载的那一行。
   * ⚠️ 菜单挂 `document.body`（插件默认）而不是 `root`：`root` 的祖先可能带 `overflow:hidden`，挂进去会被裁掉。
   */
  let contextMenu: HTMLElement | null = null
  let contextMenuTarget: { orderNo: string; shownNo: string } | null = null

  function closeContextMenu(): void {
    contextMenu?.remove()
    contextMenu = null
    contextMenuTarget = null
  }

  /**
   * 复制到剪贴板并按结果给一句话：clipboard API 优先、失败/不存在兜底 `execCommand`（实现两页共用一份）。
   * ⛔ 不能只走 `navigator.clipboard`：租户站点有 `http://` 内网访问形态（非安全上下文里它整个不存在）⇒「右键复制」会永远失败。失败文案给一句，⛔ 不静默。
   */
  function copyToClipboard(text: string, okText: string, failText: string): void {
    void copyTextToClipboard(text).then(
      (ok) => msg(ok ? okText : failText),
      // `copyTextToClipboard` 自己保证不 reject；这里兜一层纯属「就算 reject 也有话说」。
      () => msg(failText),
    )
  }

  /** 「复制订单内容」：先取详情，复制 `content`（为空则 `demand`，spec C8）。 */
  function copyOrderContent(orderNo: string): void {
    void fetchOrder(orderNo).then(
      (order) => {
        copyToClipboard(
          order.content !== '' ? order.content : order.demand,
          COPY_ORDER_CONTENT_OK_MESSAGE,
          COPY_ORDER_CONTENT_FAIL_MESSAGE,
        )
      },
      (e: unknown) => msg(errText(e)),
    )
  }

  /** 「复制成新订单」：`POST /orders/<no>/copy`（Task 3 的端点）→ 刷新列表 + 提示。 */
  function copyToNewOrder(orderNo: string): void {
    void copyOrder(orderNo).then(
      () => {
        // 顺序照本页既有口径：先 `reload()`（它可能弹「尚未开放筛选」警告）再报成功，
        // 反了的话 `layer.msg` 会把成功那句顶掉（见 `openStatusDialog` 那两条坑）。
        reload()
        msg(COPY_ORDER_SUCCESS_MESSAGE)
      },
      (e: unknown) => msg(errText(e)),
    )
  }

  function runContextMenuCommand(key: string): void {
    const target = contextMenuTarget
    if (!target) return
    if (key === 'viewDetails') {
      // 与点订单号蓝链**同一个出口**（T5；对标这两处调的就是同一个 `viewOrderDetails`，
      // 见本文件「已知差异 21」）：层里是我们自己的复刻页，`title` 前缀取显示单号。
      openOrderDetailLayer(target.orderNo, target.shownNo)
      return
    }
    if (key === 'copyOrderID') {
      // 复制**显示值**：来源单号非空就是它（`orderIdRow` 的渲染规则，spec §2 第 13 条）。
      copyToClipboard(
        target.shownNo,
        `已成功复制订单号：${target.shownNo}到剪贴板`,
        COPY_ORDER_ID_FAIL_MESSAGE,
      )
      return
    }
    if (key === 'copyOrder') {
      copyOrderContent(target.orderNo)
      return
    }
    if (key === 'copyOrderToNewOrder') copyToNewOrder(target.orderNo)
  }

  /** 造一份菜单 DOM（每次打开重建——插件同样是 `create` 出一棵新的）。 */
  function buildContextMenu(): HTMLElement {
    const list = document.createElement('ul')
    // `context-menu-root` 是插件给根节点加的那个类（`r.$node || r.$menu…addClass("context-menu-root")`）。
    list.className = `${CONTEXT_MENU_ROOT_CLASS} context-menu-root`
    for (const item of CONTEXT_MENU_ITEMS) {
      if (list.childElementCount > 0) {
        const separator = document.createElement('li')
        // ⚠️ 分隔线**不带** `context-menu-item`（与插件那棵树差这一个类，**有意**）：
        // spec §6 写的就是 `<li class="context-menu-separator">`；带上它「恰好 4 项」那个
        // `.context-menu-item` 计数会变成 8，判据就没法判了。
        separator.className = CONTEXT_MENU_SEPARATOR_CLASS
        list.appendChild(separator)
      }
      const node = document.createElement('li')
      node.className =
        `${CONTEXT_MENU_ITEM_CLASS} context-menu-icon ` +
        `context-menu-icon-${CONTEXT_MENU_ICON_NAMES[item.key]} ${item.icon}`
      node.setAttribute('data-key', item.key)
      // 插件把项名放进一个 `<span>`（`d(o).appendTo(e)`）——照它，别把文字直接塞进 `<li>`。
      const label = document.createElement('span')
      label.textContent = item.label
      node.appendChild(label)
      // 悬停反白：`.context-menu-item.context-menu-hover` 那条规则要生效就得有人加这个类
      // （对标没有 `:hover` 规则，是插件在 `mouseenter`/`mouseleave` 上加的）。
      node.addEventListener('mouseenter', () => node.classList.add(CONTEXT_MENU_HOVER_CLASS))
      node.addEventListener('mouseleave', () => node.classList.remove(CONTEXT_MENU_HOVER_CLASS))
      node.addEventListener('click', () => runContextMenuCommand(item.key))
      list.appendChild(node)
    }
    return list
  }

  function showContextMenu(
    pageX: number,
    pageY: number,
    orderNo: string,
    shownNo: string,
  ): void {
    // 先关掉上一个：右键另一行时不能同时挂着两份菜单。
    closeContextMenu()
    const list = buildContextMenu()
    // 坐标用 `pageX/pageY`：`.context-menu-list` 是 `position:absolute`、挂在 body 下，
    // 对标 `position` 取的也正是 `pageX/pageY`。
    list.style.left = `${pageX}px`
    list.style.top = `${pageY}px`
    document.body.appendChild(list)
    contextMenu = list
    contextMenuTarget = { orderNo, shownNo }
  }

  /**
   * 订单号那一格上右键 → 出菜单。
   *
   * ⚠️ **必须委托在 `root` 上**：`div.orderLine#order_<no>` 是列 templet **运行期**渲染出来的，
   * 挂载那一刻 DOM 里没有它 ⇒ 直接绑静态节点绑不上。
   */
  const onContextMenu = (event: MouseEvent): void => {
    const target = event.target
    if (!(target instanceof Element)) return
    const line = target.closest<HTMLElement>(ORDER_LINE_SELECTOR)
    if (!line) return
    const orderNo = line.id.startsWith(ORDER_LINE_ID_PREFIX)
      ? line.id.slice(ORDER_LINE_ID_PREFIX.length)
      : ''
    if (orderNo === '') return
    event.preventDefault()
    // 显示值 = 那一格里**渲染出来的那个号**（来源单号非空就是来源单号），
    // 取不到才回落到 `order_no`——两处的口径因此只有一处（`orderIdRow`）。
    // ⚠️ **T5 起改读 `data-viewId` 而不是锚点的 `textContent`**：开层时 `title` 的前缀用的是
    //    同一个属性（对标 `viewID` = `res.orderIdView`），两处**必须是同一个来源**，否则
    //    「右键看一下、再点蓝链」会得到两个不同的号（同一事实两套判据，E-0021 那族）。
    //    ⚠️ 属性名 HTML 解析后是**小写**（`data-viewid`）⇒ dataset 的键是 `viewid`，不是 `viewId`。
    const shownNo =
      line.querySelector<HTMLElement>('.orderdetails')?.dataset.viewid?.trim() || orderNo
    showContextMenu(event.pageX, event.pageY, orderNo, shownNo)
  }

  /** 关菜单的三个触发（spec §6）：点任意处 / `Esc` / 滚动。 */
  const onContextMenuCloseClick = (): void => closeContextMenu()
  const onContextMenuCloseKeydown = (event: KeyboardEvent): void => {
    if (event.key === 'Escape' || event.key === 'Esc') closeContextMenu()
  }
  const onContextMenuCloseScroll = (): void => closeContextMenu()

  root.addEventListener('contextmenu', onContextMenu)
  document.addEventListener('click', onContextMenuCloseClick)
  document.addEventListener('keydown', onContextMenuCloseKeydown)
  // `scroll` **不冒泡**：要收到页内任意容器的滚动，只能带 `capture`。
  document.addEventListener('scroll', onContextMenuCloseScroll, true)

  // 筛选框回车 = 搜索（对标 `$(document).keydown` 的 Enter 分支，L681 附近）。
  const onKeydown = (event: KeyboardEvent): void => {
    if (event.key !== 'Enter') return
    if ((event.target as HTMLElement).tagName !== 'INPUT') return
    event.preventDefault()
    reload()
  }
  root.addEventListener('keydown', onKeydown)

  // 主表：**普通 `table.render`**（对标是 `treeTable.render`，差异见文件头「已知差异 1」）。
  // `url` 直连我们的订单接口；`parseData` 吃统一信封。
  ;(table.render as (o: unknown) => void)({
    elem: `#${TABLE_ID}`,
    id: TABLE_ID,
    url: `${API_BASE.replace(/\/$/, '')}/api/business/orders/`,
    method: 'get',
    request: { pageName: 'page', limitName: 'page_size' },
    parseData: (
      r: {
        code: number
        msg: string
        data?: { items?: OrderListItem[]; total?: number; summary?: OrderListSummary | null }
      },
    ) => {
      // 统计块走闭包带给 `done`，不进返回值：layui 只认 `{code,msg,count,data}`，塞返回值里 `done` 拿不到。响应缺 `summary` 键与 `null` 同义。
      lastSummary = r.data?.summary ?? null
      return {
        code: r.code,
        msg: r.msg,
        count: r.data?.total ?? 0,
        data: r.data?.items ?? [],
      }
    },
    toolbar: '#toolbarDemo',
    defaultToolbar: [
      'filter',
      { title: '重置表格排版', layEvent: 'RESET_TABLE', icon: 'layui-icon-fonts-clear' },
    ],
    page: { limit: 20, limits: [...PAGE_LIMITS] },
    cols: [
      // ⭐ 列定义走唯一出口 `buildRowCols()`（展开插行时也调它）。
      buildRowCols(),
    ],
    text: { none: '暂无数据' },
    before: clearSelection,
    // ⚠️ `done` 是 layui 交回页码的真实出口（`table.on('page(...)')` 在 vendored 2.13.9 里不存在）：实参 `(res, curr, count, origin)`，每次取数后都调，`curr` 就是刚加载的那一页。
    done: (
      res: { count?: number; data?: OrderListItem[] } | undefined,
      curr?: number,
      count?: number,
    ) => {
      ;(form.render as () => void)()
      // 平台子订单的取数一轮加载只缓存一轮（每次取数都清，三角再点重新取）。
      platformRowsCache = new Map<string, DailiantongPushRow[]>()
      platformRowsInFlight = new Map<string, Promise<DailiantongPushRow[]>>()
      // 批量搜单号是一次性条件：取完这一发就清掉（否则「批量」会粘住整页）。
      batchOrderIds = null
      clearSelection()
      for (const row of res?.data ?? []) mainRows.set(row.order_no, row)
      const header = root.querySelector<HTMLInputElement>(`.layui-table-header ${tableCheckboxSelector}`)
      if (header) {
        header.setAttribute('aria-label', '勾选本页普通单和拆单组首行')
        header.title = '勾选本页普通单和拆单组首行，取消时清除本页勾选'
      }
      window.setTimeout(() => {
        root.querySelectorAll<HTMLElement>('.orderLine[data-split-id]:not(.sontr)').forEach((line) => {
          void showSplitMembers(line)
        })
      }, 0)
      applyCellTitles()
      // 空态文案；`count`（done 第 3 参）= layui 交回的 `res[countName]`，保留 `res?.count` 回落。
      const none = root.querySelector<HTMLElement>('.layui-table-view .layui-none')
      if (none) {
        const text = emptyStateText(count ?? res?.count ?? 0, lastWhere)
        if (text !== '') none.textContent = text
      }
      // 首屏：首帧渲染完成后按当前筛选补一次取数（与对标的默认日期区间+自动搜索同口径）。
      if (!firstDone) {
        firstDone = true
        reload(1)
      }
      // 统计句：值来自后端聚合（不拿当前页自己算）；受限时那句隐藏。
      renderSummary(root, lastSummary)
      // ⚠️ 页码赋值放在首帧 `reload(1)` 之后：reload 会把 currentPage 写成它拿到的 page，冲突时以 `curr` 为准。
      if (typeof curr === 'number') currentPage = curr
    },
  })

  // ⚠️ 这里曾有 `table.on('page(...)')` —— 已删：vendored 2.13.9 里该事件不存在、一次都不会发；页码由 `done` 交回的 `curr` 记。
  ;(form.on as (e: string, cb: (obj: { elem?: HTMLInputElement }) => void) => void)(
    'checkbox(viewIsJiaJi)',
    () => reload(),
  )
  // 「未安排」已实装成 `is_leak` ⇒ 勾/取消都就地重取。
  ;(form.on as (e: string, cb: (obj: { elem?: HTMLInputElement }) => void) => void)(
    'checkbox(viewLeakOrder)',
    () => reload(),
  )
  // `recType` 自己不产生参数，只切候选名单数据源；切档也算「改了旧组合控件」⇒ 独立发单人下拉让位（互相清空）。
  ;(form.on as (e: string, cb: (d: { value: string }) => void) => void)(
    'select(recType)',
    () => {
      clearHallPublisherSelection()
      applyPopgameOptions()
    },
  )
  // 改游戏筛选 ⇒ 远程搜索换一份 `data.game`（下一次关键词请求就带新游戏）+ 两个下拉重置回仅占位
  // （旧已选 id 可能不属于新游戏 ⇒ 发单人有已选时清空并提示重选）。
  ;(form.on as (e: string, cb: (d: { value: string }) => void) => void)(
    'select(selectGame)',
    () => {
      updateFilterSearchData()
      applyPopgameOptions()
      applyPublisherOptions()
    },
  )
  /**
   * 价格口径切档：选 `3` ⇒ 弹层装 `.seachMutPriceDiv`；选别的 ⇒ 收掉弹层。
   * ⛔ 不复刻「重置 4 个框默认值」（理由见 `openComboPriceLayer`）；本 handler 不产生任何参数（3/4 送参全在 `toOrderSearch`）。
   */
  ;(form.on as (e: string, cb: (d: { value: string }) => void) => void)(
    'select(showPriceType)',
    (data: { value: string }) => {
      if (data.value === '3') openComboPriceLayer()
      else closeComboPriceLayer()
    },
  )
  // 弹层那颗「搜索」是懒绑定（开层挂按钮元素、关层摘）——⛔ 不在这里挂文档级监听（挂载期 document 监听面由结构网钉成「恰好三个关菜单监听」）。
  /**
   * 「搜索口径」切到批量搜单号 ⇒ 弹输入层：空 / 长度 <5 ⇒ 提示、关层、不发请求；通过 ⇒ 换行→逗号存进闭包并重取；关层 ⇒ `end` 把这一格复位成 `0`。
   * ⚠️ 复位时点在 `end`、值走闭包 ⇒ `toOrderSearch` 里⛔ 不能再加 `selectSeach1 === '10'` 的闸（会把刚输入的条件静默丢掉）。
   */
  const resetBatchOrderSelect = (): void => {
    const select = root.querySelector<HTMLSelectElement>('select#selectSeach1')
    if (select) select.value = '0'
    ;(form.render as (type?: string) => void)('select')
  }
  ;(form.on as (e: string, cb: (d: { value: string }) => void) => void)(
    'select(selectSeach1)',
    (data: { value: string }) => {
      if (data.value !== '10') return
      ;(
        layer.prompt as unknown as (
          o: unknown,
          cb: (value: string, index: number) => void,
        ) => void
      )(
        {
          formType: 2,
          value: '',
          btn: ['搜索', '关闭'],
          title: BATCH_ORDER_PROMPT_TITLE,
          area: ['520px', '300px'],
          end: resetBatchOrderSelect,
        },
        (value: string, index: number) => {
          // 判据逐字取对标（`value == null || undefined || '' || value.length < 5`）。
          if (value === null || value === undefined || value === '' || value.length < 5) {
            msg(BATCH_ORDER_INVALID_TEXT)
            ;(layer.close as (i: number) => void)(index)
            return
          }
          batchOrderIds = value.replace(/\r?\n/g, ',')
          ;(layer.close as (i: number) => void)(index)
          reload()
        },
      )
    },
  )

  ;(form.render as () => void)()

  // ⭐ 两个控件指向同一个 `hall_publisher_user` ⇒ 互相清空（谁改谁生效）。
  // ⚠️ 必须放在这里（所有闭包都已初始化）：旧控件是 formselects 渲染的，用它的 `on(id, cb)`
  //    监听选人，⛔ 不指望 layui 的 `form.on('select(popgameName)')`（formselects 不触发它）。
  bindPublisherExclusivity()

  // ⭐ 给两个下拉接上远程关键词搜索（2026-11-09「已知差异 68」：候选**不再预载**，
  //    挂载零 `filter-options` 请求；输入关键词时 formselects 自己按 `xm-select-search`
  //    的 URL 防抖取数）。放在最后：依赖的 `formValues` / `#popgameName` 都已就位。
  wireFilterSearch()

  return () => {
    root.removeEventListener('click', onToolbarClick)
    root.removeEventListener('click', onCellClick)
    root.removeEventListener('dblclick', onRowDoubleClick)
    root.removeEventListener('click', onNotOpenClick)
    root.removeEventListener('click', onSummaryStatusClick)
    root.removeEventListener('keydown', onKeydown)
    // ⭐ 价格口径 `3` 弹层那颗「搜索」：它是**懒绑定在按钮元素上**的（不在 document 上）⇒
    // 兜底摘一次（层没关就切页时，那个元素还在 `.layui-layer-content` 里、仍持着闭包）。
    unbindComboPriceSearch()
    // 右键菜单：委托监听与三个 document 级关菜单监听都要摘（否则切页后仍持有闭包），
    // 挂在 body 上的那份菜单 DOM 也要收掉（`root.innerHTML = ''` 管不到它）。
    root.removeEventListener('contextmenu', onContextMenu)
    document.removeEventListener('click', onContextMenuCloseClick)
    document.removeEventListener('keydown', onContextMenuCloseKeydown)
    document.removeEventListener('scroll', onContextMenuCloseScroll, true)
    closeContextMenu()
    window.removeEventListener('message', onOrderSavedMessage)
    window.removeEventListener('message', onPushJdpSentMessage)
    // 兜底：弹层没关就切页时也要摘掉文档级捕获（否则卸载后仍持有闭包）。
    if (throwSaveBound) {
      document.removeEventListener('click', onThrowSaveClick, true)
      throwSaveBound = false
    }
    if (refundSaveBound) {
      document.removeEventListener('click', onRefundSaveClick, true)
      refundSaveBound = false
    }
    // 改类型弹层同理：`layer.open` 的替身/真身不一定走 `end`，卸载时兜底摘掉。
    if (typeSaveBound) {
      document.removeEventListener('click', onTypeSaveClick, true)
      typeSaveBound = false
    }
    // 「收款状态」弹窗同理（T8）。
    if (settSaveBound) {
      document.removeEventListener('click', onSettSaveClick, true)
      settSaveBound = false
    }
    root.innerHTML = ''
  }
}

/** 标准 island 页出口（`layui-island/page.ts` 的注册表按它调用）。 */
export function mount(root: HTMLElement, _ctx: IslandContext): Promise<() => void> {
  // ⛔ 本页**不用** `ctx`（T5 起两个「进订单详情」入口都是原地开层，没有跳路由的入口）；
  //    参数保留是 `IslandPage.mount` 的签名位置（与其它 island 页同形），下划线 = 有意未用。
  return mountOrderListPage(root)
}

/** 注册表按 `import.meta.glob('./pages/*.ts')` 的**默认导出**取页面。 */
export default { mount } satisfies IslandPage
