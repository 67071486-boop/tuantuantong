/**
 * 【第三方平台订单管理】页的 **layui 复刻页**（对标物 `/Order/CoreOrderCenter`「交易平台订单处理」）。
 *
 * 形状：**14 个状态页签** + **12 个筛选控件**（两排 `.layui-form-pane`）+ **15 颗工具栏按钮**
 * （`#toolbarDemo` 标记模板）+ **24 列**表格（含 1 个勾选列；对标原 25 列删 5 加 4，
 * 列定义见 `buildCols()`）+ **手写分页**（`layui.laypage.render`），表格本身 `page:false`。
 *
 * ## 表格接真数据（`GET /api/business/platform-pushes/`）
 *
 * 有数据源的列：平台交易单号（`serial_no`）/ 系统单号（`source_order_no`，空回退 `order_no`，
 * 见「需求 2」）/ 游戏区服 / 发单价 / 交易状态（综合状态，缺回读 ⇒「—」）/ 系统状态
 * （`order_status` 中文）/ 发单时间 + N7 那批（见「h6x N7」）。
 * ⛔ **没有数据源的列一个假值都不造**：接单价 / 利润 / 剩余时间 / 验收倒计 / 角色名 /
 * 旺旺 / 订单来源 / 接手时间 / 完单时间 / 录单时间 / 撤销时间 / 发单人 / 项目分类 /
 * 发单标题 / 订单备注恒显示「—」（后端契约里**根本没有这些键**）。
 * ⛔ 「交易状态」的色档唯一出处 = `platformPush.ts::DISPLAY_STATUS_BADGE_CLASSES`，
 * ⛔ 本页不自己维护第二张色表；「系统状态」文字色同理走 `ORDER_STATUS_TEXT_COLORS`
 * （`UNASSIGNED` 用它 switch 的 `default`，⛔ 不编新色；对标那一格是可点的行内入口，
 * 本版用 `<span>`，⛔ 不摆一个点了没反应的链接）。
 * ⛔ 工具栏「平台消单数据统计」的**直接隐藏**必须在 `table.render.done` 里做（那一刻 layui
 * 才把 `#toolbarDemo` 模板 laytpl 成真实按钮节点），每次重渲染盖一次；提前 `querySelector` 恒查不到。
 * `last_synced_at` / `state` / `last_error` **不占列**：只进「平台交易单号」格的
 * `title`（`最近同步：… · 状态：… · 错误：…`）。
 *
 * **14 个状态页签 → `status`**：`全部` = 不带；8 个有码页签按 `TAB_STATUS_MAP` 翻成
 * `remote_status`（计数取 `counts['<码>']`）。没有对应远端码的页签 ⇒ 零请求、空表 +
 * 「该分类暂无数据源」，⛔ 不拿别的码顶替（分类名会和数据对不上）。
 * 搜索框 → `keyword`；发单平台固定 `platform=10`（代练通）。其余筛选控件本版不能消费
 * ⇒ 逐条登记「后端未支持」（见「需求 6」）。
 * 行内动作按钮是 **4 颗接真**（编辑 / 撤回 / 协商撤销 / 加款，见「h6x ③」）；
 * ⛔ 不做重发 / 短信 / 改价 / 截图。
 *
 * ## ⭐ C1：工具栏 11 颗接真 + 1 颗隐藏
 *
 * 11 颗批量按钮（撤回 / 验收 / 仲裁 / 同步 / 改系统状态 / 记退款 / 改标签 / 改标记 /
 * 删标记 / 加急 / 改接单价）走**同一个批量端点** `POST /api/business/platform-pushes/actions/`
 * （`TOOLBAR_ACTION_FILTERS` 分派）；「刷新」接真（重取当前页）；「发送短信」「导出订单」
 * 点了「尚未开放」、零请求；「平台消单数据统计」**直接隐藏**（见 `HIDDEN_TOOLBAR_FILTERS`）。
 * 每颗都先过两道前端闸（业务判据一律在后端）：
 * ① 批量动作**必须先勾选**（`table.checkStatus('datatable').data` → 系统单号 `t_orderid`；
 *   没勾选 ⇒ 逐字提示「请选择要操作的记录！」、零弹层零请求）；
 * ② **先有确认框 / 输入框**（写清后果：撤回两档语义、验收扣款且不可撤销、仲裁**不提交平台**、
 *   退款是**累计总额**、加急只是本地标记……），点「确定」才发请求。
 * 提交中按钮加 `layui-btn-disabled` + `disabled`、并有在途闸（同一次只发一发）；成功 / 部分成功
 * 都重新取数（有成功的笔已改库），失败原样弹后端中文原文。
 * 本地动作落点：改系统状态 = 既有状态机（**已抛单且远端未结束 ⇒ 拒绝**，后端 `manual_status_denial`）；
 * 记退款 = 既有退款核心（**累计总额**）；改接单价 = 既有 `change_price`（写 `Order.total_price`）；
 * 标签 = `Order.order_label`（**没有标签字典** ⇒ 只能手填文本）；标记 = `Order.tag`；
 * 加急 = `Order.is_urgent`（**本地标记**，不改代练通的发布时间）。
 *
 * ## 证据（视觉值唯一出处）
 *
 *  - 标记与两段页内 `<style>` → `public/replica/order-coreordercenter.html`
 *    （源 `.tools/recon/suqing1/html/Order__CoreOrderCenter.html` L24-156 / L157-451；
 *    清洗口径逐条写在证据件头部注释里，**本文件一字未改证据件**）；
 *  - 列定义 → `.tools/recon/suqing1/assets/JS2019/Order/CoreOrderCenter.js:505-537`（对标 25 列；
 *    ⭐ N7 起本页是**24 列**：删 5 加 4，见文件头「h6x N7」）；
 *  - 表格参数 → 同 JS **L561-595**（`page:false` / `toolbar:'#toolbarDemo'` /
 *    `defaultToolbar:['filter', {…RESET_TABLE…}]` / `totalRow:true` / `limit:30` / `limits:[20,40]`
 *    ——⚠️ 后两个因 `page:false` **不生效**，真正生效的是手写 `laypage` 那份）；
 *  - 手写分页 → 同 JS **L703-728**：`limit:30`、`limits:[10,20,30,40,50,100]`、
 *    `layout:['count','prev','page','next','limit']`、`count<=limit` 时整块 `hide()`；
 *  - 日期区间 → 同 JS **L30-70**：`range:true` / `min:-180`、跨度 > 90 天清空并提示
 *    「日期最多选择90天」；
 *  - 页签点击 → 同 JS **L125-131** 的 `lay-id`；本版按 `TAB_STATUS_MAP` 翻成后端
 *    `remote_status` 后重取（没有对应码的页签零请求，见差异 16）。
 *
 * ## ⭐ g2：聚合折叠 / 系统单号 / 右键菜单 / 行内标签 / 页签计数 / 筛选登记
 *
 * - **需求 1（聚合折叠）**：按后端 `?group_by=order` 取数，一个订单一条折叠行（默认折叠、
 *   三角展开真子行，子单数据来自**同一发回包** ⇒ 展开零请求，主表与右侧固定列各插一遍）；
 *   没有 `pushes[]`（后端 `group_by` 未上线）⇒ 退回扁平渲染，页面照旧能用。
 *   ⛔ 不退化成「按页聚合」假聚合；每次重渲染先清子行 ⇒ 折叠状态复位。
 * - **需求 2（系统单号）**：显示来源单号（空回退内部单号）、蓝色 `#0094ff`；点它 `layer.open`
 *   装自己的订单详情岛页（配方同 `order-orderlist.ts::openOrderDetailLayer`：`85% × 92%` /
 *   `layui-layer-lan` / `anim:8` / `maxmin:false`）。开层前有单号形状闸 `/^\d{20}$/`
 *   （`layer.open` 的 `title` 是 HTML 上下文，⛔ 不把不可信输入拼进提示）。
 * - **需求 3（右键菜单）**：**恰好 4 项**、文案逐字（查看详情 / 复制系统订单编号 /
 *   复制原始订单内容 / 查看订单截图）；图标用 vendored layui 字形（对标的图标字体全仓取不到）。
 * - **需求 4（行内设标签）**：**已整条删除**（连同常量 / HTML / 点击处理 / 弹层 / 本会话兜底表），
 *   「平台交易单号」格只留单号文本（父行 = 平台数量）；工具栏批量「更改标签」**仍在**。
 * - **需求 5（页签数字一次加载）**：`GET …/counts/` 一次填齐有数据源的 9 个页签（其余保持「—」）；
 *   搜索 / 刷新 / 动作成功后各重取一次（仍是一次请求），⛔ 不「点一个查一个」。
 *   失败 ⇒ 全部「—」+ 一句 `页签数字加载失败：<后端中文原文>`（⛔ 不写 0、⛔ 不静默）。
 * - **需求 6（筛选登记）**：后端读面只收 `platform` / `status` / `keyword` / 分页（聚合再加
 *   `group_by`）⇒ 除「关键词 + 搜索按钮」外逐条登记「后端未支持」（控件 `title` 写原因 +
 *   改过再点搜索给一句中文提示），⛔ 不做「看起来能筛、实际没传参」的假控件。
 * - **需求 7（未读角标 / 平台交易详情）**：读面没有数据源 ⇒ **不造角标、不摆假链接**
 *   （平台单号仍是纯文本 + 同步信息 tooltip）。
 *
 * ## ⭐ h6x：单条平铺 / 父行带按钮 / 协商撤销真提交
 *
 * - **① 只有 1 条平台单的订单 ⇒ 正常平铺**（不出三角、不折叠）：`pushes.length >= 2` 才折叠；
 *   1 条按普通行铺（`toSinglePushDisplayRow`）。⛔ 0 条（后端契约异常）也不画三角、操作列画「—」。
 * - **② 折叠父行也挂操作按钮**：作用于该单「**最新一条**推送」——`groupStatusPush` 按
 *   `created_at` **降序**、`id` **降序**取第一条（⛔ **不看 `is_active`**；`latestActivePush`
 *   只是它的别名）。父行的发单平台 / 发单价 / 交易状态 / 发单时间全部取**同一条**推送
 *   （⛔ 不允许「平台取 A 单、价格取 B 单」）；推送行没有接单价 / 利润 / 剩余时间 ⇒ 仍「—」（⛔ 不编）。
 * - **③ 行内 4 颗按钮接真**（每颗只在它自己的状态上出现，判据见 `ROW_ACTION_STATUSES`，
 *   逐条取自后端实现）：编辑（只送改动项）/ 撤回（删除 + 即时退款）/ 协商撤销 / 加款。
 *   ⛔ **不做**重发、短信、导出、平台消单数据统计。每颗：先确认 / 先填表（撤回与协商撤销
 *   **写清后果**）→ 提交中禁用 + 在途闸 → 成功刷新列表（列表 + 页签数字各一次）→ 失败
 *   **原样显示后端中文**。「协商撤销」金额格缺 / 空串 ⇒ 默认 `0`（可直接提交），自动客服介入
 *   勾选框**默认不勾**、勾与不勾**都显式发**；**失败不关弹层**（改完可再提交）。
 * - 已知缺口：后端协商撤销今天只认 12/13/16 ⇒ 14/15/18 会回中文「不支持撤回」，
 *   页面放行、原样显示；列表 / 推送读面不下发编辑现值 ⇒ 编辑弹层**不预填**。
 *
 * ## 与对标**有意不同**（逐条给理由，别当成漏抄）
 *
 * | # | 项 | 处置与理由 |
 * |---|---|---|
 * | 1 | 表格数据 / 页签计数 | **表格接真**（`?group_by=order`，后端未升级时自动退化扁平）；页签数字**一次**用 `counts/` 填齐（见「需求 5」）。⚠️ 若后端 counts 仍是推送行口径，聚合形态下「全部(N)」与分页 `total`（订单数）会有差异，报告里点名。⛔ 没取到数一律「—」，不写 0 冒充 |
 * | 2 | 搜索 / 工具栏按钮 | **搜索接真**（`#key1` → `keyword`，并重取第 1 页）。工具栏见文件头「C1」一节：**11 颗接真**（一个批量端点）、**刷新**接真、**平台消单数据统计隐藏**；只有「发送短信」「导出订单」仍只提示「尚未开放」、零请求（导出那一颗：对标导的是平台单，我方导出端点列不同） |
 * | 3 | 列头排序事件 | **不做**。对标全文件没有 `table.on('sort')`（`grep` 实测），点表头不会重新取数；排序的唯一入口是筛选栏的 `orderBy` 下拉（本版该下拉也只会改变控件值、不发请求）。列上的 `sort:true` 是**复刻值**（表头小箭头），保留不动 |
 * | 4 | 第 2 个页签文案 | 证据件静态写「未接手」，对标首轮取数后 JS 改写成「待接单(N)」（`CoreOrderCenter.js:686`）⇒ **屏幕文案是「待接单」**。挂载后把该 `li` 的文本改成「待接单」，计数由 `counts/` 一次填齐（见「需求 5」） |
 * | 5 | 列 `field` 复用（对标缺陷） | 对标「验收倒计」与「录单时间」**都用 `field:'t_create'`**（`CoreOrderCenter.js:526,533`）——同 `field` 两列在 layui 里会互相污染。按标准 §5「对标自己的缺陷不复刻」：验收倒计改用 `t_exAccpetTime`（标题 / 宽度 / `sort` / `hide` 一字不改） |
 * | 6 | 行合并（`tableMerge`） | 对标 9 列带 `merge:'orderId'`，但那扩展是 layuiadmin 的、**不在 vendored layui 里**。业务方 2026-10-28 的聚合需求改由**后端按订单聚合 + 前端折叠行 / 展开子行**实现（见「需求 1」），⛔ 不抄 `tableMerge`、也⛔ 不按页假合并 |
 * | 7 | 列宽 / 隐藏列的本地缓存 | **不做**。对标把 `cols` / `ishide` 存 `layui.data('platmorderlistlocal')`，会让同一张表在不同人机器上宽度不同（对拍前要清缓存）；本版没有「拖列宽后要记住」的诉求 ⇒ 不落本地缓存。「重置表格排版」那颗只提示「尚未开放」 |
 * | 8 | 首屏 500ms 自动搜索 / 回车搜索 | **不做**。本页首屏由 `mount` 主动取第 1 页；对标那句 500ms 自动点击会造成「刚打开就连打两次」的重复请求。回车搜索也不接（采搜索按钮这一条明确入口）。 |
 * | 9 | 三个 `xm-select` 下拉的候选 | 订单来源 / 发单人 / 项目分类在证据件里**本来就是空壳**（候选由对标 JS 调接口灌入）。本版只按 formselects 渲染空壳：能看到控件、候选项为空（筛选本身不发请求） |
 * | 10 | 列 18「订单来源」的 `hide` | 对标是 `hide: isViewSoure == 'Y' ? false : true`，而本页 `isViewSoure='Y'` 写死在 JS L101 ⇒ **运行时可见**。本版按有效值写 `hide: false`（保留该键便于对拍） |
 * | 11 | 行内按钮 = **4 颗**（不是对标 7 颗） | 业务方 2026-10-28 **最终定案**：行内只留 **编辑 / 撤回 / 协商撤销 / 加款**，且**各自只在允许的状态上出现**（见「h6x ③」）。⛔ 对标那颗**重发**不做（正常走发单入口）、⛔ 短信不做、⛔ 改价 / 截图这两颗也不做（业务方给的按钮集里没有）。对标的两行排布是为 7 颗准备的；本版两组按钮（11 vs 12/13）**互斥**、最多同时 2 颗 ⇒ 不插换行。⚠️ 任务书里的「补时 / 留言」不在这页的 `btnfun` 里，⛔ 不凭空加按钮 |
 * | 12 | `#toolbarDemo` 里那条隐藏统计行 | 照证据件原样保留（父容器 `layui-hide`；对标取数后写「本次搜索共N笔平台单」）。本版**不写这条统计**（读面没有分类计数）⇒ 保持隐藏，⛔ 不拿「本页行数」冒充 |
 * | 13 | 筛选栏「**排序方序**」 | **逐字保留对标原文**（证据件 L326 / 对标 HTML L314 应为「排序方式」）。这是对标的错字，但复刻标准 §3 要求「一切取值来自对标产物」⇒ 不改错、不擅自纠正；⛔ 将来也别当漏抄修掉 |
 * | 14 | 标记里的 `lay-tips` | **逐字照抄**（值纪律），但 vendored layui 里 `lay-tips` 是**死属性**（不是 layuiadmin）⇒ 本页**不渲染 tooltip**；本页按钮都有可见文字，提示不是唯一入口，故**未加** `title` 兜底。同仓先例：`pages/order-orderlist.ts` 已知差异 6 / 24-V1（地球按钮因为只有图标，另拼 `title` 让悬停可见） |
 * | 15 | **「平台交易单号」格的 `title`** | 对标这一格只有单号，没有悬停提示。本版按业务方 2026-10-27 口径把 `last_synced_at` / `state` / `last_error` 放进 `title`（`最近同步：… · 状态：… · 错误：…`）⇒ **不占列**，像素对拍看不到差异，只有 DOM 多一个 `title` 属性 |
 * | 16 | **没有数据源的 6 个页签** | 仲裁中 / 已仲裁 / 已撤销 / 已下架 / 已删除 / 新交易消息在今天的读面（`remote_status` 只有 10–17）里**没有对应码** ⇒ **零请求**、空表 + 空态「该分类暂无数据源」。⛔ 不拿相近码顶替（那会让「仲裁中」显示异常单） |
 * | 17 | **筛选控件的消费面** | 本版读面只有 `platform` / `status` / `keyword` / 分页（聚合线再加 `group_by`）⇒ 只有**搜索框**接上 `keyword`。其余 9 组控件**登记为「后端未支持」**（控件 `title` + 点搜索时的中文提示，见「需求 6」），⛔ 不做「改了值就以为筛上了」的假控件；需要什么参数见报告 |
 * | 18 | **聚合折叠 / 单条平铺的可见差异** | 对标没有折叠行（它靠 `tableMerge` 合并单元格）；本版是「父行 + 展开的真子行」，且**只有 1 条平台单的订单平铺**（见「h6x ①」）。父行的平台数量画在「平台交易单号」格；父行的**发单平台 / 发单价 / 交易状态 / 发单时间取该单最新一条推送**（业务方 2026-10-28 口径，按 `created_at` / `id` 降序；与父行按钮同一套判据），接单价 / 利润 / 剩余时间仍然「—」（推送行没有这三个值）。**像素对拍会看到**这一差异，属于业务方 2026-10-28 新增需求的必然结果 |
 * | 19 | **右键菜单图标** | 对标用 `context-menu-icons` 字体（`paste`/`edit`/`copy`/`images`），该字体全仓取不到 ⇒ 用 vendored layui 字形（`layui-icon-form` / `edit` / `file` / `picture`），与订单列表页同一条处置 |
 * | 20 | **行内「设标签」控件已整条删除（⭐ N7，2026-10-28 业务方）** | 业务方原话：「那格下面的『设标签』按钮不需要了，碍眼」⇒ 常量 / HTML / 点击处理 / 弹层 / 本会话兜底表全部删掉，「平台交易单号」格**只留单号文本**（父行 = 平台数量）。⚠️ 工具栏那颗批量「更改标签」不受影响。（对标有这一档，删掉是有意差异，别当漏抄。） |
 * | 21 | **未读消息角标 / 平台交易详情** | 读面没有数据源（差距表 ❌15 / ❌16）⇒ 本版**不做**（⛔ 不造角标、不摆假链接），见「需求 7」 |

 *
 * ## ⭐ N5：右侧固定列与主表**逐行对齐**
 *
 * `fixed:'right'` 是**另一张表**（主表与固定列两份 `<tr>` 各自布局），本页行高不齐、
 * 插行后 layui 不会再对齐 ⇒ 客服会点错单。补法 = `syncFixedRowHeights(root)`：
 * 主表第 i 行多高，固定列第 i 行就多高（`style.height`），并把固定列格的 `.layui-table-cell`
 * 撑满 + flex 垂直居中（光撑 `<tr>` 不够）。调用点：`table.render.done` / 展开 / 收起 / resize。
 * ⛔ 量不到高度（0）不写 `0px`；⛔ 子行不写 `data-index`（不在 layui 数据缓存里）。
 *
 * ## ⭐ h6x N7：24 列（删 5 列 / 补 4 列 / 新增 4 列）
 *
 * **删掉**（整列移除）：订单备注 / 项目分类 / 发单标题 / 角色名 / 旺旺-WX；
 * **补上内容**：接单价（`accept_price`）/ 利润（接单价 − 发单价）/ 订单来源（`order_source`）/
 * 验收倒计（倒计时）；**新增**：接手人 / 完成单数 / 投诉·取消（> 0 标红）/ 接单时间；
 * 「平台交易单号」格下面的「设标签」按钮已随需求 4 删净。
 * 口径：**父行**（折叠）一律取「最新一笔」推送的这些字段（与 `groupStatusPush` 同一来源，⛔ 不串单）；
 * **子行**各显示自己那条；**缺键**（后端未升级）⇒「—」，利润缺一边就是「—」（⛔ 不拿 0 充数），
 * 负数照实显示并标红（`ROW_DANGER_COLOR`）。
 * ⛔ **验收倒计时 / 剩余时间只在页面里算**：本地 1 秒 tick（`VERIFY_TICK_MS`）**就地改文字 / 颜色**，
 * ⛔ 不重渲染表格（会收掉展开的子行）、⛔ **不发任何请求**（不轮询后端、更不调上游）。
 * 过期 ⇒「已超时 X 小时 Y 分」红色。列变少变多之后 `syncFixedRowHeights`（N5）照旧对齐固定列。
 *
 * ## ⭐ N8：订单来源直接显示 / 剩余时间用新键
 *
 * 后端只下发**中文名**（快照 → 字典名 → 空串，⛔ 不会再给数字 id）⇒ 页面**直接显示**收到的字符串，
 * 只有空 / 缺键才画「—」。⛔ 不再做「纯数字 ⇒ 画—」兜底——后端保证不下发 id 之后，
 * 留着它只会**吞掉**真实的来源名（比如某店铺名字面就是数字串）。
 * 「剩余时间」用后端新键 `remaining_seconds`（**整秒**）：`= 接单时间 + 时限 − 现在`；
 * 未接单 ⇒ `null`；已过期 ⇒ `0`。显示「剩 X 小时 Y 分」，超 1 天 ⇒「剩 N 天 X 小时」；
 * `0` / 负数 ⇒「**已超时**」（红色）；`null` / 缺键 ⇒「—」。页面只在本地每秒递减
 * （渲染时记 `base` + `at`），⛔ 不拿时间戳自己再算一遍、⛔ 不发任何请求。
 * ⚠️ 与「验收倒计」**不是同一列**：那个有绝对截止时间，这个只有后端给的秒数
 * ——两者共用同一个 tick / class，靠 `data-countdown-kind` 区分格式。
 *
 * ## 挂载顺序（E-0067：别调换）
 *
 * `await bootstrapIsland(...)` 挂样式 → 取证据件 → `root.innerHTML` → `initIslandElements(layui)`
 * → `table.render` / `form.render` / `laypage.render`。
 * 反了会先画出没穿样式的骨架（FOUC），或页签绑定落空。
 *
 * ## 挂载顺序（E-0067：别调换）
 *
 * `await bootstrapIsland(...)` 挂样式 → 取证据件 → `root.innerHTML` → `initIslandElements(layui)`
 * → `table.render` / `form.render` / `laypage.render`。
 * 反了会先画出没穿样式的骨架（FOUC），或页签绑定落空。
 */
import {
  DAILIANTONG_PLATFORM,
  addOrderPlatformPushAmount,
  editOrderPlatformPush,
  fetchPlatformPushCounts,
  fetchPlatformPushGroups,
  fetchPlatformPushes,
  isGroupedPushItem,
  newPushActionRequestId,
  runPlatformPushAction,
  withdrawOrderPlatformPush,
  type DailiantongPushRow,
  type EditPlatformPushPayload,
  type PlatformPushBatchAction,
  type PlatformPushCounts,
  type PlatformPushGroupItem,
  type PlatformPushListItem,
  type PlatformPushListParams,
  type PlatformPushListRow,
  type PlatformPushSubRow,
} from '@/api/dailiantongPush'
import { BizError } from '@/api/errors'
import { fetchOrder } from '@/api/orders'
import {
  ORDER_STATUS_LABELS,
  ORDER_STATUS_VALUES,
  orderStatusLabel,
  type OrderStatus,
} from '@/config/orderStatus'
import { PLATFORMS } from '@/config/platforms'
// 右键复制走「clipboard API → execCommand 兜底」那份共用实现
// （与订单管理页同一份，⛔ 不在两个页面各写一遍）。
import { copyTextToClipboard } from '@/layui-island/clipboard'
import { loadConfigArtifact } from '@/layui-island/config-replica'
import { escapeHtml } from '@/layui-island/escape'
import {
  platformWriteAllowedNow,
  resolvePlatformWritePermission,
} from '@/layui-island/perms'
import {
  classifyPushError,
  orderStatusTextColor,
  platformDisplayStatus,
  platformPushStateLabel,
  platformPushStatusLabel,
  platformStatusBadgeClass,
} from '@/layui-island/platformPush'
import {
  bootstrapIsland,
  initIslandElements,
  type IslandContext,
  type IslandPage,
} from '@/layui/island'
import type { LayuiModule } from '@/layui/loader'
import { initFormSelects } from '@/layui/vendor'

/** 证据件名（= `public/replica/<key>.html`，也 = island 注册表的 key）。 */
export const ARTIFACT_KEY = 'order-coreordercenter'
export const TABLE_ELEM = '#datatable'
/** 表格的 `lay-filter`。⚠️ 本版没有 `table.on('sort'/'toolbar')` 绑定（见文件头第 3/7 条），留它是为了对拍时能认出这张表。 */
export const TABLE_FILTER = 'datatable'
/** 工具栏模板的选择器。 */
export const TOOLBAR_ELEM = '#toolbarDemo'
/** 分页容器的 `id`（`laypage.render` 的 `elem` 收**不带 `#`** 的 id）。 */
export const PAGE_ELEM_ID = 'page'
/** 同一个容器的 CSS 选择器（⚠️ 与 `PAGE_ELEM_ID` 分成两个常量，E-0061 ①）。 */
export const PAGE_ELEM_SELECTOR = '#page'
export const PAGE_LIMIT = 30
export const PAGE_LIMITS = [10, 20, 30, 40, 50, 100] as const
export const PAGE_LAYOUT = ['count', 'prev', 'page', 'next', 'limit'] as const

/** 「全部」页签的 `lay-id`（不带 `status`）。 */
export const ALL_TAB_ID = '0'

/**
 * 页签 `lay-id` → 后端 `remote_status`。
 * 取值口径见 `docs/DAILIANTONG-API.md` §5（含「已下架 18」，真机补的码）。
 * ⛔ 不在这张表里的页签 = **没有数据源**（见 `NO_DATA_SOURCE_TAB_IDS`），不要拿相近的码顶替。
 */
export const TAB_STATUS_MAP: Readonly<Record<string, number>> = {
  '10': 11,
  '20': 12,
  '25': 15,
  '30': 14,
  '40': 13,
  '50': 16,
  '60': 17,
  // 「已下架」接上读面：后端 `?status=18` 与 `counts['18']` 都支持（上游码表没列这一档，真机观测）。
  '75': 18,
}

/**
 * 今天**没有对应远端状态码**的页签（`lay-id`）：仲裁中 / 已仲裁 / 已撤销 / 已删除 / 新交易消息
 * （「已下架」页签 `75` 已接上 `TAB_STATUS_MAP['75'] = 18`，不在本表）。
 */
export const NO_DATA_SOURCE_TAB_IDS: readonly string[] = ['55', '56', '70', '80', '100']

/** 空态文案（没有数据源的分类）。 */
export const NO_DATA_SOURCE_TEXT = '该分类暂无数据源'
/** 正常空态文案（layui 默认值；后端 0 行时显示）。 */
export const EMPTY_TEXT = '无数据'
/** 加载失败时空态文案的前缀（后面拼错误消息）。 */
export const LOAD_FAILED_PREFIX = '加载失败：'

/** 本页固定发单平台：代练通（后端读面的默认平台）。 */
export const FIXED_PLATFORM = DAILIANTONG_PLATFORM

/* ── 行内按钮（编辑 / 撤回 / 协商撤销 / 加款）──────────
 * ⛔ 只做这 4 颗（⛔ 不做重发 / 短信 / 导出 / 平台消单数据统计）；每颗只在**它自己那几种
 * 交易状态**上出现（`ROW_ACTION_STATUSES`），⛔ 不许全都放行——那会点出一堆上游中文错误。
 * 动作键（`data-platform-action` 的值）是点击分派的唯一判据，⛔ 不认可见文字（改文案不该改行为）。
 */
export const ROW_ACTION_EDIT = 'edit'
export const ROW_ACTION_WITHDRAW = 'withdraw'
export const ROW_ACTION_NEGOTIATE = 'negotiate'
export const ROW_ACTION_AMOUNT = 'amount'

/**
 * 行内 4 颗按钮：**动作键 + 可见文字 + class**（class 沿用对标按钮配色；
 * 「协商撤销」对标没有这一颗 ⇒ 用同族 `layui-btn-warm`，配色属推断、已登记）。
 */
export const ROW_ACTION_BUTTONS = [
  { key: ROW_ACTION_EDIT, label: '编辑', cls: 'layui-btn-danger' },
  { key: ROW_ACTION_WITHDRAW, label: '撤回', cls: 'layui-btn-warm' },
  { key: ROW_ACTION_NEGOTIATE, label: '协商撤销', cls: 'layui-btn-warm' },
  { key: ROW_ACTION_AMOUNT, label: '加款', cls: '' },
] as const

/** 行内按钮的可见文字（顺序 = 渲染顺序）。 */
export const ROW_ACTION_LABELS = ROW_ACTION_BUTTONS.map((item) => item.label)

/**
 * 每颗按钮**在哪几种交易状态上出现**（键 = 综合状态 token，来自 `platformDisplayStatus`；
 * 判据逐条取自后端实现：edit 只放行 11；withdraw 走 `_withdraw_once` 的 11 分支、18 也放行
 * （后端拒时回中文，原样显示）；negotiate = 12/13/14/15（后端今天只认 12/13/16，其余原样显示
 * 其中文）；amount = 后端 `AMOUNT_STATUSES` {12, 13}）。
 * ⛔ 撤销中 / 已结算等终态与中间态**一颗都不出现**（操作列画「—」）——后端那几档都会拒绝。
 */
export const ROW_ACTION_STATUSES: Readonly<Record<string, readonly string[]>> = {
  [ROW_ACTION_EDIT]: ['unaccepted'],
  [ROW_ACTION_WITHDRAW]: ['unaccepted', 'off_shelf'],
  [ROW_ACTION_NEGOTIATE]: ['in_progress', 'waiting_review', 'abnormal', 'locked'],
  [ROW_ACTION_AMOUNT]: ['in_progress', 'waiting_review'],
}

/** 该交易状态下**该出现**的行内按钮（顺序 = `ROW_ACTION_BUTTONS`）；空 ⇒ 操作列画「—」（⛔ 不摆一排点了必被拒的按钮，也不置灰）。 */
export function rowActionKeysFor(token: string): string[] {
  return ROW_ACTION_BUTTONS.filter((item) =>
    (ROW_ACTION_STATUSES[item.key] ?? []).includes(token),
  ).map((item) => item.key)
}

/* ───────── 工具栏：11 颗接真 + 刷新 + 1 颗隐藏 + 2 颗「尚未开放」（C1） ───────── */

/** 没勾选就点批量按钮时的提示（**逐字**照对标原文）。 */
export const SELECT_REQUIRED_MSG = '请选择要操作的记录！'

/** 「刷新」那颗的 `lay-filter`（证据件 L344；点了就是重取当前页）。 */
export const REFRESH_FILTER = 'refresh'

/** 工具栏 `lay-filter` → 后端动作名（11 颗接真）。「对标端点 → 我方落点」见 `api/dailiantongPush.ts` 的 `PlatformPushBatchAction` 表；⛔ 不在这张表里的工具栏按钮 = 本版不做。 */
export const TOOLBAR_ACTION_FILTERS: Readonly<Record<string, PlatformPushBatchAction>> = {
  cancelOrderBtn: 'withdraw',
  accpetOrderBtn: 'accept',
  arbitOrderBtn: 'arbitrate',
  asyncOrderBtn: 'sync',
  changeStaBtn: 'change_status',
  refundOrderBtn: 'refund',
  setOrderLabelBtn: 'set_label',
  setOrderTipsBtn: 'set_tag',
  cleanOrderTipsBtn: 'clear_tag',
  setOrderJiajiBtn: 'urgent',
  changeOrderPriceBtn: 'change_taker_price',
}

/** 动作名 → 页面按钮名（提示语用；⛔ 不拿它做分支判据）。 */
export const TOOLBAR_ACTION_LABELS: Readonly<Record<PlatformPushBatchAction, string>> = {
  withdraw: '批量撤回',
  accept: '批量验收',
  arbitrate: '批量仲裁',
  sync: '同步交易状态',
  change_status: '改系统状态',
  refund: '记退款',
  set_label: '更改标签',
  set_tag: '改标记',
  clear_tag: '删标记',
  urgent: '加急',
  change_taker_price: '改接单价',
}

/** 本版**不做**的工具栏按钮（点了仍只提示「尚未开放」，零请求）；`viewPlatformReportBtn` 同时被隐藏——万一被脚本点到也只提示、不发请求（fail-closed）。 */
export const NOT_OPEN_TOOLBAR_FILTERS: readonly string[] = [
  'sendSmsBtn',
  'exportBtn',
  'viewPlatformReportBtn',
]

/** **隐藏**的工具栏按钮（平台消单数据统计）。⚠️ 隐藏而不删标记：证据件是像素级对拍物件 ⛔ 不改它；挂载后按 `lay-filter` 设 `display:none`（不依赖样式表加载）。 */
export const HIDDEN_TOOLBAR_FILTERS: readonly string[] = ['viewPlatformReportBtn']

/** 工具栏里属**交易平台写动作**的那几颗（渲染期挂 `order.platform_write`）。⛔ 改系统状态 / 记退款 / 改接单价是本地写动作、不在这张表；`asyncOrderBtn` 是读上游回写本地的对账动作、也不在（与升级前一致）。 */
export const PLATFORM_WRITE_TOOLBAR_FILTERS: readonly string[] = [
  'cancelOrderBtn',
  'accpetOrderBtn',
  'arbitOrderBtn',
  'setOrderLabelBtn',
  'setOrderTipsBtn',
  'cleanOrderTipsBtn',
  'setOrderJiajiBtn',
]

/** 没权限时把这些工具栏按钮**真摘掉**（不是置灰、不是 `display:none`；D-0154：点了会 403 的入口就不该出现）。⚠️ 必须放在 `table.render` 的 `done` 里（那时 layui 才把模板渲染成真实按钮节点）；幂等，每次重渲染盖一次。 */
export function hidePlatformWriteToolbarButtons(root: HTMLElement): void {
  if (platformWriteAllowedNow()) return
  for (const filter of PLATFORM_WRITE_TOOLBAR_FILTERS) {
    root.querySelector<HTMLElement>(`button[lay-filter="${filter}"]`)?.remove()
  }
}

/** 确认框文案：批量撤回（两档语义必须说清：未接手删除退款 / 已接手协商撤销）。 */
export const WITHDRAW_CONFIRM_TEXT =
  '未接手的单会直接删除并即时退款；已被接手的单只能申请协商撤销，能否撤回由对方决定。确定要撤回这些订单吗？'

/** 确认框文案：批量验收（花钱 + 本地状态不变，两件事都要说）。 */
export const ACCEPT_CONFIRM_TEXT =
  '确认完单会按订单价从代练通余额扣款，且不可撤销。代练通确认结算后，团团通会同步为已完成。确定要验收这些订单吗？'

/** 确认框文案：同步交易状态（只读远端、写回本地）。 */
export const SYNC_CONFIRM_TEXT =
  '会立即逐笔读取代练通的最新交易状态并写回本地（不改变远端）。确定要同步这些订单的交易状态吗？'

/** 确认框文案：加急（**本地标记**，⛔ 不改远端发布时间）。 */
export const URGENT_CONFIRM_TEXT =
  '加急只是本地标记，不会改动代练通上的发布时间。确定把这些订单设为加急吗？'

/** 确认框文案：删标记。 */
export const CLEAR_TAG_CONFIRM_TEXT = '确定要清除这些订单的自定义标记吗？'

/** 「改系统状态」弹层里那颗 `<select>` 的 id（⚠️ 与选择器分开写，E-0061 ①）。 */
export const STATUS_SELECT_ID = 'core-order-center-status-select'

/** 「改系统状态」弹层的说明（后果：只改本地；已抛单且远端未结束会被拒）。 */
export const STATUS_DIALOG_NOTE =
  '改系统状态只改本地，不会改代练通；已抛到代练通且远端尚未结束的订单会被拒绝。'

/** 退款输入框的标题（⚠️ 我们的接口是**累计总额**，不是本次新增——必须写清）。 */
export const REFUND_PROMPT_TITLE =
  '请输入累计退款总额（不是本次新增；只有「已完成」的订单能改，最多两位小数）'

/** 退款原因：对标弹窗有单独的备注框，我方接口的 `reason` 是必填 ⇒ 页面给一句固定说明。 */
export const REFUND_REASON = '第三方平台订单管理页记退款'

/** 接单价输入框标题（**逐字**照对标原文，字符串被页测试钉住）。 */
export const TAKER_PRICE_PROMPT_TITLE = '更改接单价，可为0，但不能为空'

/** 标记输入框标题（本仓列宽 128，上限取 128 并在文案里写明；对标原文是 50）。 */
export const TAG_PROMPT_TITLE = '请输入自定义标记（最多 128 字；要清空请用「删标记」）'

/** 标签输入框标题（对标是「下拉选一个预定义标签」；本仓没有标签字典 ⇒ 手填文本）。 */
export const LABEL_PROMPT_TITLE = '请输入订单标签（留空表示清除标签）'

/** 仲裁理由输入框标题（⚠️ 本版只在本地记录）。 */
export const ARBITRATION_PROMPT_TITLE =
  '请输入申请仲裁理由（最多 100 字；本版只在本地记「待仲裁」，不会提交到代练通）'

/** 仲裁确认框：再次说明「没提交平台」，⛔ 不让客服以为已经在代练通申请了。 */
export const ARBITRATION_CONFIRM_TEXT =
  '确定把这些订单标记为「待仲裁」吗？本版只在本地订单日志记一条，不会提交到代练通；请到代练通后台申请客服介入。'

/** 金额/接单价格式：非负、最多两位小数（「可为 0」）。 */
export function isValidMoneyText(value: string): boolean {
  return /^\d+(\.\d{1,2})?$/.test(value)
}

/** 加急/标签等本地列：把百分比等奇怪值挡在页面外（判据仍在后端）。 */
export function isValidTagText(value: string): boolean {
  return value.trim() !== '' && value.trim().length <= 128
}

/** 隐藏本版**不做**的工具栏按钮（只动 `display`，⛔ 不改证据件、⛔ 不删标记）。「平台消单数据统计」是明确裁掉的入口，隐藏而非置灰——留着会让人以为「没接好」。 */
export function hideUnsupportedToolbarButtons(root: HTMLElement): void {
  for (const filter of HIDDEN_TOOLBAR_FILTERS) {
    const button = root.querySelector<HTMLElement>(`button[lay-filter="${filter}"]`)
    if (button !== null) button.style.display = 'none'
  }
}

/** 「改系统状态」弹层的正文：8 个系统状态的静态下拉（`@/config/orderStatus` 镜像）。能不能改成某一档由后端判，⛔ 前端不镜像可达性（镜像了必然漂移，漂移的表现是「少一个选项」）。 */
export function statusDialogHtml(): string {
  const options = ORDER_STATUS_VALUES.map(
    (value) => `<option value="${value}">${ORDER_STATUS_LABELS[value]}</option>`,
  ).join('')
  return (
    `<p style="line-height:22px;margin-bottom:8px;">${STATUS_DIALOG_NOTE}</p>` +
    `<select id="${STATUS_SELECT_ID}" class="layui-input" style="height:38px;">${options}</select>`
  )
}

/** 本次加载的来源：正常取数 / 没有数据源 / 失败 / 已有加载在途被跳过。 */
export type LoadOutcome = 'loaded' | 'no-source' | 'failed' | 'busy'

/** 第 2 个页签的 `lay-id` 与**屏幕文案**（证据件静态写「未接手」，屏幕改成「待接单」，见文件头第 4 条）。 */
export const PENDING_TAB_ID = '10'
export const PENDING_TAB_LABEL = '待接单'
/** 工具栏「重置表格排版」的标题（defaultToolbar 的 `RESET_TABLE` 事件）。 */
export const RESET_TABLE_LABEL = '重置表格排版'
/** 「日期最多选择 90 天」的提示（**逐字**照对标，字符串被页测试钉住）。 */
export const DATE_SPAN_MSG = '日期最多选择90天'
/** 允许的最大日期跨度（天）。 */
export const DATE_SPAN_MAX_DAYS = 90

/** 列的宽松类型（layui 的 `cols` 是运行时对象，这里只按「键值对」用）。 */
export type CoreOrderCol = Record<string, unknown>

/** 表格一行的显示值（键 = 列的 `field`；全部是字符串，空数据统一「—」）。 */
export type CoreOrderDisplayRow = Record<string, string>

/** 后端 `created_at` / `last_synced_at` 的显示口径（与订单管理页 `createRow` 同形）。 */
export function formatDateTime(value: string | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  return value.replace('T', ' ').slice(0, 16)
}

/* ── ⭐ h6x N7：接单价 / 利润 / 订单来源 / 验收倒计时 ──────
 * 这一批全部**只做展示**（金额 / 来源 / 接手信息来自后端同期下发的新键）；
 * 缺键（后端未升级）一律降级「—」，⛔ 不报错、⛔ 不拿 0 / id 充数。
 */

/**
 * 金额字符串 → **整数分**（`''` / 非两位小数以内 ⇒ `null`）。
 * 用「分」算差值是为了避开浮点尾数（`0.1 + 0.2 !== 0.3` 这类误差会在「利润」列上被客服看见）。
 */
export function moneyToCents(value: string | null | undefined): number | null {
  const text = (value ?? '').trim()
  if (!/^-?\d+(\.\d{1,2})?$/.test(text)) return null
  const negative = text.startsWith('-')
  const digits = negative ? text.slice(1) : text
  const dot = digits.indexOf('.')
  const yuan = dot === -1 ? digits : digits.slice(0, dot)
  const frac = dot === -1 ? '' : digits.slice(dot + 1)
  const cents = Number(yuan) * 100 + Number((frac + '00').slice(0, 2))
  if (!Number.isFinite(cents)) return null
  return negative ? -cents : cents
}

/** 整数分 → 两位小数字符串（`-200` ⇒ `-2.00`）。 */
export function centsToMoney(cents: number): string {
  const sign = cents < 0 ? '-' : ''
  const abs = Math.abs(Math.round(cents))
  return `${sign}${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, '0')}`
}

/** **利润** = 接单价 − 发单价；两边都有值才算、缺一边 ⇒「—」（⛔ 不拿 0 充数）；负数**照实显示**（页面用 `isNegativeMoney` 标红），⛔ 不吞成 0。 */
export function profitTextOf(
  acceptPrice: string | null | undefined,
  pushPrice: string | null | undefined,
): string {
  const accept = moneyToCents(acceptPrice)
  const push = moneyToCents(pushPrice)
  if (accept === null || push === null) return EMPTY_VALUE
  return centsToMoney(accept - push)
}

/** 这个金额是不是负数（负数利润要标红）。 */
export function isNegativeMoney(text: string): boolean {
  return /^-\d/.test(text.trim())
}

/**
 * 「订单来源」显示什么：后端只下发**中文名**（快照 → 字典名 → 空串）⇒ **直接显示**这个字符串，
 * 只有空 / 缺键才画「—」。⛔ 不做「像数字就画—」兜底——会吞掉真实的来源名
 * （比如某店铺名字面就是数字串）。
 */
export function orderSourceText(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return EMPTY_VALUE
  const text = String(value).trim()
  return text === '' ? EMPTY_VALUE : text
}

/** 计数列（完成单数 / 投诉取消）的显示：`0` 照实显示 0，缺键 ⇒「—」。 */
export function countText(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? String(value) : EMPTY_VALUE
}

/** 投诉 / 取消次数 > 0 要标红（0 与缺键都不标）。 */
export function custCancelAlert(value: number | null | undefined): boolean {
  return typeof value === 'number' && value > 0
}

/** 危险色（负数利润 / 已超时 / 投诉取消 > 0 三处共用）；取「问题单」那档红，与 `platformPush.ts::ORDER_STATUS_TEXT_COLORS.ISSUE` 同值，⛔ 不新造色。 */
export const ROW_DANGER_COLOR = '#ff0000'

/* ── 验收倒计时：**只在页面里算**（⛔ 不轮询后端、⛔ 不调上游） ─────────────── */

/** 倒计时节点的类名（类名与选择器分开写，E-0061 ①）。 */
export const VERIFY_COUNTDOWN_CLASS = 'tt-verify-countdown'
export const VERIFY_COUNTDOWN_SELECTOR = `.${VERIFY_COUNTDOWN_CLASS}`
/** 本地刷新间隔：1 秒。 */
export const VERIFY_TICK_MS = 1000

/**
 * 「剩余时间」列的显示（**直接用后端给的秒数**，⛔ 不从时间戳算）：
 * `null` / 缺键 ⇒「—」；`<= 0` ⇒「已超时」（后端把过期归一成 0，负数也一起兜）；
 * **超过 1 天** ⇒「剩 N 天 X 小时」；其余 ⇒「剩 X 小时 Y 分」。
 */
export function formatShoursRemaining(seconds: number | null | undefined): string {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return EMPTY_VALUE
  if (seconds <= 0) return '已超时'
  const total = Math.floor(seconds)
  if (total > 86400) {
    const days = Math.floor(total / 86400)
    const hours = Math.floor((total % 86400) / 3600)
    return `剩 ${days} 天 ${hours} 小时`
  }
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  return `剩 ${hours} 小时 ${minutes} 分`
}

/** 倒计时节点的「口径」标记：`shours` = 剩余时间列，其余（默认）= 验收倒计。 */
export const COUNTDOWN_KIND_ATTR = 'data-countdown-kind'
export const COUNTDOWN_KIND_SOURS = 'shours'

/**
 * 「剩余时间」格的倒计时节点（与验收倒计共用同一个 tick / class，靠 `data-countdown-kind` 区分格式）。
 * tick 时用 `seconds − (now − at)` **递减**，⛔ 不去读任何时间戳重算。
 */
export function shoursCountdownHtml(
  seconds: number | null | undefined,
  at: string,
  nowMs: number,
): string {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return EMPTY_VALUE
  const base = String(Math.floor(seconds))
  const remaining = verifyRemainingSeconds('', seconds, at, nowMs)
  const text = formatShoursRemaining(remaining)
  const overdue = remaining !== null && remaining <= 0
  const color = overdue ? ` style="color:${ROW_DANGER_COLOR}"` : ''
  return (
    `<span class="${VERIFY_COUNTDOWN_CLASS}"` +
    ` ${COUNTDOWN_KIND_ATTR}="${COUNTDOWN_KIND_SOURS}"` +
    ' data-verify-deadline=""' +
    ` data-verify-base="${escapeHtml(base)}"` +
    ` data-verify-at="${escapeHtml(at)}"${color}>${escapeHtml(text)}</span>`
  )
}

/**
 * 剩余秒数 → 显示文案：`null`（后端没给截止时间也没给快照）⇒「—」；
 * `<= 0` ⇒ 已超时（0 也算超时）；分钟向下取整。
 */
export function formatVerifyCountdown(remainingSeconds: number | null): string {
  if (remainingSeconds === null || !Number.isFinite(remainingSeconds)) return EMPTY_VALUE
  const overdue = remainingSeconds <= 0
  const abs = Math.abs(Math.trunc(remainingSeconds))
  const hours = Math.floor(abs / 3600)
  const minutes = Math.floor((abs % 3600) / 60)
  return overdue
    ? `已超时 ${hours} 小时 ${minutes} 分`
    : `剩 ${hours} 小时 ${minutes} 分`
}

/** `data-*` 里的数字：空串 / 非数字 ⇒ `null`。 */
function nullableNumberOf(value: string | null | undefined): number | null {
  const text = (value ?? '').trim()
  if (text === '') return null
  const parsed = Number(text)
  return Number.isFinite(parsed) ? parsed : null
}

/**
 * 现在还剩多少秒（**纯函数**，`nowMs` 由调用方给 ⇒ 可测）：
 * ① 有 `verify_deadline_at`（绝对截止时间）⇒ 直接拿它减「现在」（最准）；
 * ② 只有 `verify_remaining_seconds`（抓取那一刻的剩余秒数）⇒ 用它减「抓取到现在过了多久」；
 * ③ 都没有 ⇒ `null`（画「—」）。
 */
export function verifyRemainingSeconds(
  deadline: string | null | undefined,
  baseSeconds: number | null | undefined,
  fetchedAtMs: string | null | undefined,
  nowMs: number,
): number | null {
  const deadlineText = (deadline ?? '').trim()
  if (deadlineText !== '') {
    const deadlineMs = Date.parse(deadlineText)
    if (!Number.isNaN(deadlineMs)) return Math.round((deadlineMs - nowMs) / 1000)
  }
  if (typeof baseSeconds === 'number' && Number.isFinite(baseSeconds)) {
    const fetchedAt = nullableNumberOf(fetchedAtMs)
    const elapsed = fetchedAt === null ? 0 : (nowMs - fetchedAt) / 1000
    return Math.round(baseSeconds - elapsed)
  }
  return null
}

/**
 * 倒计时那一格的 HTML：一个带 `data-*` 的 `<span>`，便于**就地**改文字（不重渲染整表）。
 * `style="color:…"` 只在**已超时**时加（红色）。
 */
export function verifyCountdownHtmlParts(
  deadline: string,
  base: string,
  at: string,
  nowMs: number,
): string {
  const remaining = verifyRemainingSeconds(deadline, nullableNumberOf(base), at, nowMs)
  if (remaining === null) return EMPTY_VALUE
  const text = formatVerifyCountdown(remaining)
  const color = remaining <= 0 ? ` style="color:${ROW_DANGER_COLOR}"` : ''
  return (
    `<span class="${VERIFY_COUNTDOWN_CLASS}"` +
    ` data-verify-deadline="${escapeHtml(deadline)}"` +
    ` data-verify-base="${escapeHtml(base)}"` +
    ` data-verify-at="${escapeHtml(at)}"${color}>${escapeHtml(text)}</span>`
  )
}

/** 一行（父行 / 扁平行）的倒计时格：原料从显示值里取。 */
export function verifyCountdownHtml(row: CoreOrderDisplayRow, nowMs: number): string {
  return verifyCountdownHtmlParts(
    row['t_verifyDeadline'] ?? '',
    row['t_verifyBase'] ?? '',
    row['t_verifyAt'] ?? '',
    nowMs,
  )
}

/** 一条推送的倒计时格（**子行**用它，原料直接从推送行取）。 */
export function verifyCountdownHtmlOfPush(
  push: PlatformPushSubRow,
  nowMs: number,
): string {
  const base =
    typeof push.verify_remaining_seconds === 'number' ? String(push.verify_remaining_seconds) : ''
  return verifyCountdownHtmlParts(
    (push.verify_deadline_at ?? '').trim(),
    base,
    String(nowMs),
    nowMs,
  )
}

/**
 * 本地倒计时刷新：按「现在」重算每个倒计时节点，**就地**改文字与颜色。
 * ⚠️ 只动文本 / 颜色，⛔ **不重渲染表格**（重渲染会收掉用户展开的子行）；
 * ⛔ **不发任何请求**（只在页面里倒计时）。
 */
export function tickVerifyCountdowns(root: HTMLElement, nowMs: number): void {
  for (const el of Array.from(root.querySelectorAll<HTMLElement>(VERIFY_COUNTDOWN_SELECTOR))) {
    const remaining = verifyRemainingSeconds(
      el.dataset['verifyDeadline'] ?? '',
      nullableNumberOf(el.dataset['verifyBase']),
      el.dataset['verifyAt'] ?? '',
      nowMs,
    )
    const kind = el.getAttribute(COUNTDOWN_KIND_ATTR) ?? ''
    const text =
      kind === COUNTDOWN_KIND_SOURS
        ? formatShoursRemaining(remaining)
        : formatVerifyCountdown(remaining)
    if (el.textContent !== text) el.textContent = text
    const overdue = remaining !== null && remaining <= 0
    // 回来时（理论上不会，但数据被重取时可能）去掉红色，⛔ 不留一个假的红。
    if (overdue) el.style.color = ROW_DANGER_COLOR
    else if (el.style.color !== '') el.style.color = ''
  }
}

/**
 * 「系统单号」这一格显示的颜色。两页（本页与订单列表页）是**两个独立的 island 文档**，
 * 本仓今天没有「颜色常量」共享模块 ⇒ 各留一份字面量，改色时**两处一起改**
 * （`order-orderlist.ts` 的 `ORDER_NO_DEFAULT_COLOR`）。
 */
export const SOURCE_ORDER_NO_COLOR = '#0094ff'

/** 系统单号那一格的容器类名（**右键菜单**只认它；E-0061 ①：类名与选择器分开写）。 */
export const ORDER_LINE_CLASS = 'orderLine'
export const ORDER_LINE_SELECTOR = `.${ORDER_LINE_CLASS}`
/** 那一格里「点一下开详情」的蓝链类名。 */
export const ORDER_DETAIL_LINK_CLASS = 'orderdetails'
export const ORDER_DETAIL_LINK_SELECTOR = `.${ORDER_DETAIL_LINK_CLASS}`
/** 折叠三角的类名（聚合行才有；对标订单列表页 P6 同一套字形）。 */
export const ORDER_TREE_CLASS = 'tt-order-tree'
export const ORDER_TREE_SELECTOR = `.${ORDER_TREE_CLASS}`
/** 展开后插在父行下面的平台子行类名（DOM 插行，不进 layui 数据缓存）。 */
export const ORDER_CHILD_ROW_CLASS = 'tt-order-child-row'
export const ORDER_CHILD_ROW_SELECTOR = `.${ORDER_CHILD_ROW_CLASS}`
/** 折叠行第二行的「系统单号：…」小字类名（只在来源单号与内部单号不同的时候画）。 */
export const ORDER_INNER_NO_CLASS = 'tt-order-inner-no'

/** 单元格里「没有数据」的统一写法（与 `toDisplayRow` 同口径）。 */
export const EMPTY_VALUE = '—'

/* ══ 行内按钮接真 + 「协商撤销」真提交 ═════════════════════
 * 4 颗的端点与载荷见 `ROW_ACTION_STATUSES` 与各 `open*` / `onRow*` 函数；
 * 每颗：先确认 / 先填表（撤回、协商撤销**写清后果**）→ 提交中禁用 + 在途闸（连点只发一次）
 * → 成功刷新列表（列表 + 页签数字各一次）→ 失败**原样显示后端中文**（⛔ 不自造文案）。
 * 「成功」的判据 = 端点的业务码（`request()` 不抛 = `code=0`）；撤回 / 协商撤销还要按
 * **返回行**说结果（可能只是「已提交协商撤销」）。
 */

/** 行内按钮点了但这一行没有可定位的推送（理论上不该发生；⛔ 不静默、⛔ 不猜单号）。 */
export const ROW_TARGET_MISSING_MESSAGE = '该行没有关联的平台单，请刷新后再试'

/**
 * 行内写动作「**结果未知**」时补的那句话。
 * 「加款」是增量动作、没有幂等键（`amount/` 只收一个 `amount`）⇒ 网络断 / 超时 / 5xx 时
 * **不知道上游到底写没写**；只甩「网络异常」客服的下一步就是**再点一次** ⇒ 重复加钱。
 * ⇒ 未知档统一：报出后端原文 + 这句，并**强制刷新列表**（客服能自己判断要不要重做）；
 * 业务失败仍按原文显示、**不刷新**——什么都没变，不必刷一屏。
 */
export const ROW_WRITE_UNKNOWN_NOTE =
  '已发出，请刷新核对；不要重复提交 —— 加款是增量，重复提交会重复加钱'

/** 撤回（未接手 ⇒ 直接删除）的确认语：写清「删除 + 即时退款 + 不可恢复」。 */
export const ROW_WITHDRAW_CONFIRM_TEXT =
  '未接手的平台单会直接从代练通删除并即时退款；删掉之后不可恢复。确定要撤回这笔平台单吗？'

/** 加款的输入框标题（后端只收**整数元 ≥1**；代练通不接受小数）。 */
export const ROW_AMOUNT_PROMPT_TITLE = '请输入加款金额（整数元，至少 1 元）'
export const ROW_AMOUNT_INVALID_MESSAGE = '加款金额必须是不小于 1 的整数元'
/** 加款成功文案（`price` = 后端回读的新发单价；空串就不拼第二段，⛔ 不编）。 */
export function rowAmountSuccessMessage(amount: string, price: string): string {
  return price === '' ? `已加款 ${amount} 元` : `已加款 ${amount} 元，平台发单价现为 ${price} 元`
}
/** 加款的确认语（写明后果：抬高平台发单价、不可撤销）。 */
export function rowAmountConfirmText(amount: string): string {
  return `确认给这笔平台单加款 ${amount} 元吗？加款会直接抬高代练通上的发单价，且不可撤销。`
}

/* ── 编辑（`POST …/platform-push/edit/`；表单只送改动项） ──────────────── */

export const ROW_EDIT_DIALOG_TITLE = '编辑代练通订单'
export const ROW_EDIT_TITLE_ID = 'core-order-center-edit-title'
export const ROW_EDIT_MEMO_ID = 'core-order-center-edit-memo'
export const ROW_EDIT_REQUIREMENT_ID = 'core-order-center-edit-requirement'
export const ROW_EDIT_LABEL1_ID = 'core-order-center-edit-label1'
export const ROW_EDIT_OK_MESSAGE = '已提交修改'
export const ROW_EDIT_EMPTY_MESSAGE = '请至少填写一项要修改的内容（留空 = 不修改）'
/** 编辑弹层的说明（写清「留空不提交」+ 金额不从这儿改 + 为什么不预填）。 */
export const ROW_EDIT_DIALOG_NOTE =
  '只有填写了的项会提交（留空 = 不改这一项）；价格请走「改价 / 加款」。' +
  '代练通详情里的现值不在本页读面里，所以这里不预填，请只填你要改的那一项。'

/**
 * 编辑弹层的正文（四个**文本类**字段）。
 * 已知缺口：后端 `edit/` 还收 `zone_server_id` / `groups` / `label2`，但前两个要区服字典 /
 * 渠道枚举、本页拿不到候选 ⇒ 只做这四个文本项；读面不下发 `title` / `memo` / `requirement`
 * 现值 ⇒ 输入框不预填（⛔ 不拿空串冒充「现在就是空的」，说明写在弹层里）。
 */
export function editPushDialogHtml(): string {
  const item = (label: string, inner: string): string =>
    `<div class="layui-form-item"><label class="layui-form-label">${label}</label>` +
    `<div class="layui-input-block">${inner}</div></div>`
  const textarea = (id: string, placeholder: string): string =>
    `<textarea id="${id}" class="layui-textarea" placeholder="${placeholder}"></textarea>`
  const input = (id: string, placeholder: string): string =>
    `<input type="text" id="${id}" class="layui-input" autocomplete="off"` +
    ` placeholder="${placeholder}">`
  return (
    `<div class="layui-form" style="padding:10px 16px 0">` +
    item('发单标题', input(ROW_EDIT_TITLE_ID, '留空 = 不修改标题')) +
    item('代练内容', textarea(ROW_EDIT_MEMO_ID, '留空 = 不修改代练内容')) +
    item('代练要求', textarea(ROW_EDIT_REQUIREMENT_ID, '留空 = 不修改代练要求')) +
    item('标签', input(ROW_EDIT_LABEL1_ID, '留空 = 不修改标签')) +
    `<blockquote class="layui-elem-quote" style="margin:0 0 10px">${ROW_EDIT_DIALOG_NOTE}</blockquote>` +
    `</div>`
  )
}

/** 把四个输入框的值收敛成 `edit/` 的载荷：**只带非空的键**（⛔ 不送空串覆盖上游现值）。 */
export function editPushPayloadOf(fields: {
  title: string
  memo: string
  requirement: string
  label1: string
}): EditPlatformPushPayload {
  const payload: EditPlatformPushPayload = {}
  const title = fields.title.trim()
  const memo = fields.memo.trim()
  const requirement = fields.requirement.trim()
  const label1 = fields.label1.trim()
  if (title !== '') payload.title = title
  if (memo !== '') payload.memo = memo
  if (requirement !== '') payload.requirement = requirement
  if (label1 !== '') payload.label1 = label1
  return payload
}

/* ── 协商撤销（三格 + 自动客服介入勾选框；`POST …/platform-push/withdraw/`） ── */

export const ROW_NEGOTIATE_TITLE = '协商撤销代练通订单'
export const ROW_NEGOTIATE_PAY_ID = 'core-order-center-cancel-pay'
export const ROW_NEGOTIATE_ENSURE_ID = 'core-order-center-cancel-ensure'
export const ROW_NEGOTIATE_COMMENT_ID = 'core-order-center-cancel-comment'
export const ROW_NEGOTIATE_AUTO_ID = 'core-order-center-cancel-auto'
export const ROW_NEGOTIATE_SUBMIT = '提交申请'
/**
 * 自动客服介入勾选框的可见文案。
 * ⚠️ 与订单管理页（`order-orderlist.ts::REVOKE_AUTO_ARBITRATE_LABEL`）**默认值不同**：
 * 那边默认**勾选**、本页默认**不勾**（两个页面各自的口径，⛔ 别统一）。
 */
export const ROW_NEGOTIATE_AUTO_LABEL = '协商 1 小时后自动申请客服介入'
/** 弹层底部说明（后果 + 密码由后端带 + 默认值出处）。 */
export const ROW_NEGOTIATE_NOTE =
  '提交后只能等打手 / 客服协商，能否撤销由对方决定；支付密码由后端自动带，无需填写。' +
  '两个金额默认取该推送行的代练通现值；拿不到现值时默认 0，请核对后再提交。'
/**
 * 两个金额框**没有现值时默认填 `"0"`**：`isValidMoneyText` 与后端 `_optional_money` 都放行，
 * 填 0 能**直接提交**（不会卡在「请填写…」那一步）。
 * ⚠️ 后端下发现值（`pay_level_bal` / `rep_ensure_bal`）时**一律用现值**，只有缺 / 空串才回落到它。
 */
export const ROW_NEGOTIATE_DEFAULT_BALANCE = '0'
export const ROW_NEGOTIATE_PAY_INVALID = '请填写「支付代练费」（不小于 0，最多两位小数）'
export const ROW_NEGOTIATE_ENSURE_INVALID = '请填写「赔保证金」（不小于 0，最多两位小数）'
/** 撤回 / 协商撤销成功后按**返回行**说结果（⛔ 不复述后端固定 msg）。 */
export const PUSH_WITHDRAW_CLOSED_MESSAGE = '已撤回并退款'
export const PUSH_WITHDRAW_CANCEL_PENDING_MESSAGE = '已提交协商撤销，等打手/客服处理'

/**
 * 协商撤销弹层的正文：三格 + 自动客服介入勾选框（**默认不勾**）。
 * 默认值：后端下发了该推送行的现值 ⇒ **用现值**；缺 / `null` / 空串 ⇒ 填
 * **`ROW_NEGOTIATE_DEFAULT_BALANCE`（`0`）**。⚠️ 密码（`PayPass`）由后端从对接配置里带，
 * 页面⛔ 不碰、⛔ 不显示。
 */
export function negotiateCancelDialogHtml(push: PlatformPushSubRow): string {
  const balanceOf = (value: string | null | undefined): string => {
    const text = (value ?? '').trim()
    return text === '' ? ROW_NEGOTIATE_DEFAULT_BALANCE : text
  }
  const pay = balanceOf(push.pay_level_bal)
  const ensure = balanceOf(push.rep_ensure_bal)
  const item = (label: string, inner: string): string =>
    `<div class="layui-form-item"><label class="layui-form-label">${label}</label>` +
    `<div class="layui-input-block">${inner}</div></div>`
  return (
    `<div class="layui-form" style="padding:10px 16px 0">` +
    item(
      '支付代练费',
      `<input type="text" id="${ROW_NEGOTIATE_PAY_ID}" class="layui-input"` +
        ` autocomplete="off" value="${escapeHtml(pay)}"` +
        ' placeholder="支付给接单打手的金额（有现值用现值，没有默认 0）">',
    ) +
    item(
      '赔保证金',
      `<input type="text" id="${ROW_NEGOTIATE_ENSURE_ID}" class="layui-input"` +
        ` autocomplete="off" value="${escapeHtml(ensure)}"` +
        ' placeholder="接单打手赔偿给发单方的金额（有现值用现值，没有默认 0）">',
    ) +
    `<div class="layui-form-item layui-form-text">` +
    `<label class="layui-form-label">撤销理由</label>` +
    `<div class="layui-input-block"><textarea id="${ROW_NEGOTIATE_COMMENT_ID}"` +
    ` class="layui-textarea" placeholder="撤销原因（打手 / 客服会看到）"></textarea></div></div>` +
    `<div class="layui-form-item"><div class="layui-input-block">` +
    `<input type="checkbox" id="${ROW_NEGOTIATE_AUTO_ID}" lay-skin="primary"` +
    ` title="${ROW_NEGOTIATE_AUTO_LABEL}">` +
    `</div></div>` +
    `<blockquote class="layui-elem-quote" style="margin:0 0 10px">${ROW_NEGOTIATE_NOTE}</blockquote>` +
    `</div>`
  )
}

/**
 * 「撤回 / 协商撤销」成功后说什么 —— 按**返回行**判（⛔ 不复述「成功」两个字）。
 * `state=closed` ⇒ 平台那边已删除（钱已退）；其余 ⇒ 只是**提交了协商撤销**，
 * 还得等打手 / 客服处理（这一步不保证撤销成功）。
 */
export function rowWithdrawVerdict(updated: DailiantongPushRow): { ok: boolean; message: string } {
  return updated.state === 'closed'
    ? { ok: true, message: PUSH_WITHDRAW_CLOSED_MESSAGE }
    : { ok: true, message: PUSH_WITHDRAW_CANCEL_PENDING_MESSAGE }
}

/** 加款金额的格式闸：**整数元、≥1**（后端 `_integer_yuan_amount` 的口径；判据仍在后端）。 */
export function isValidAmountYuan(value: string): boolean {
  if (!/^\d+$/.test(value)) return false
  return Number(value) >= 1
}

/* ── 右键菜单 4 项 ─────────────── */

/**
 * 右键菜单**恰好 4 项**：查看详情 / 复制系统订单编号 / 复制原始订单内容 / 查看订单截图。
 * `label` 与 `key` 都逐字照对标；`icon` 是我们挑的 vendored layui 字形
 * （对标的 `context-menu-icons` 字体全仓取不到）。
 */
export const CONTEXT_MENU_ITEMS = [
  { key: 'viewDetails', label: '查看详情', icon: 'layui-icon-form' },
  { key: 'copyOrderID', label: '复制系统订单编号', icon: 'layui-icon-edit' },
  { key: 'copyOrder', label: '复制原始订单内容', icon: 'layui-icon-file' },
  { key: 'viewPic', label: '查看订单截图', icon: 'layui-icon-picture' },
] as const

/** 菜单根 `<ul>` / 菜单项 `<li>` / 分隔线 `<li>` 的类名（与对标插件同名）。 */
export const CONTEXT_MENU_ROOT_CLASS = 'context-menu-list'
export const CONTEXT_MENU_ITEM_CLASS = 'context-menu-item'
export const CONTEXT_MENU_SEPARATOR_CLASS = 'context-menu-separator'
export const CONTEXT_MENU_HOVER_CLASS = 'context-menu-hover'
/** 注入菜单样式表的元素 id（页面自带 `<style>`，按 id 去重）。 */
export const CONTEXT_MENU_STYLE_ID = 'tt-coreordercenter-contextmenu-style'

/**
 * 菜单样式（根容器 / 项 / 悬停反白 / 分隔线）；`z-index` 压过 layui 的弹层基线（`19891014`）。
 * ⚠️ 图标不用对标的 `::before` 字形（字体取不到），改成菜单项里一个 vendored layui `<i>`
 * + 一条间距规则 ⇒ 视觉上仍是「左图标 + 文字」。
 */
export const CONTEXT_MENU_STYLE_RULES =
  `.${CONTEXT_MENU_ROOT_CLASS}{position:absolute;display:inline-block;min-width:13em;max-width:26em;padding:.25em 0;margin:.3em;font-family:inherit;font-size:inherit;list-style-type:none;background:#fff;border:1px solid #bebebe;border-radius:.2em;box-shadow:0 2px 5px rgba(0,0,0,.5);z-index:19891016}` +
  `.${CONTEXT_MENU_ITEM_CLASS}{position:relative;box-sizing:content-box;padding:.2em 2em;color:#2f2f2f;user-select:none;background-color:#fff;cursor:pointer}` +
  `.${CONTEXT_MENU_ITEM_CLASS}.${CONTEXT_MENU_HOVER_CLASS}{color:#fff;background-color:#2980b9}` +
  `.${CONTEXT_MENU_SEPARATOR_CLASS}{padding:0;margin:.35em 0;border-bottom:1px solid #e6e6e6}` +
  `.${CONTEXT_MENU_ITEM_CLASS} .layui-icon{margin-right:.55em}`

/** 「复制系统订单编号」成功 / 失败（**逐字**照对标原文）。 */
export const COPY_ORDER_ID_SUCCESS_PREFIX = '已成功复制订单号：'
export const COPY_ORDER_ID_SUCCESS_SUFFIX = '到剪贴板'
export const COPY_ORDER_ID_FAILED_MESSAGE = '复制订单号失败了，请手动复制'
/** 「复制原始订单内容」成功 / 失败（**逐字**照对标原文）。 */
export const COPY_ORDER_CONTENT_SUCCESS_MESSAGE = '已成功复制订单内容到剪贴板'
export const COPY_ORDER_CONTENT_FAILED_MESSAGE = '复制订单内容失败了，请手动复制'
/** 单号形状不对（开层前的闸，与订单列表页同一句）。 */
export const ORDER_NO_INVALID_MESSAGE = '订单号格式不正确，无法打开订单详情'
export const SCREENSHOT_ORDER_NO_INVALID_MESSAGE = '订单号格式不正确，无法打开订单截图'

/** 订单截图（管理）弹层的岛页 key / 标题 / 尺寸（逐项取订单列表页的同一颗入口）。 */
export const SCREENSHOT_MANAGE_PAGE = 'order-screenshots-manage'
export const SCREENSHOT_LAYER_TITLE = '查看订单截图'
export const SCREENSHOT_LAYER_AREA: readonly [string, string] = ['80%', '85%']
export const SCREENSHOT_LAYER_ANIM = 1

/* ── 行内「设标签」控件已整条删除：常量 / HTML / 点击处理 / 弹层 / 兜底表全删净，
 * 那一格**只留单号文本**（父行 = 平台数量，子行 = 自己的单号）。
 * ⚠️ 工具栏那颗「更改标签」（批量 `set_label`）**不受影响**，仍在。
 */

/* ── 聚合折叠（需求 1） ─────────────────────────────── */

/** 折叠行「平台数量」那一格的标题（说明三角可展开）。 */
export function groupCountTitle(count: number): string {
  return `该订单下有 ${count} 条平台单，点左侧三角展开`
}
/** 平台 id → 中文（字典唯一出处 `config/platforms.ts`；未知 id 原样标「平台 N」）。 */
export function platformLabelOf(id: number): string {
  return PLATFORMS.find((item) => item.id === id)?.label ?? `平台 ${id}`
}
/**
 * 折叠行「平台交易单号」格里的平台数量文案：`代练通 2 条`（多个平台用「、」）。
 * 这是本页的**加法**（对标没有聚合折叠），格式按业务方口径「平台数量」定，
 * ⛔ 不去猜一份不存在的对标原文。
 */
export function groupPlatformCountText(pushes: readonly PlatformPushSubRow[]): string {
  if (pushes.length === 0) return EMPTY_VALUE
  const labels: string[] = []
  for (const push of pushes) {
    const label = platformLabelOf(push.platform)
    if (!labels.includes(label)) labels.push(label)
  }
  return `${labels.join('、')} ${pushes.length} 条`
}

/**
 * 「系统单号」这一格**显示什么**：来源单号非空用它，否则回退内部 `order_no`
 * （与管理订单页「订单号」列同一条规则，见 `order-orderlist.ts::orderIdRow`）。
 */
export function sourceOrderNoOf(row: {
  order_no: string
  source_order_no?: string
}): string {
  const source = (row.source_order_no ?? '').trim()
  return source !== '' ? source : row.order_no
}

/**
 * 「系统单号」格的 templet：`div.orderLine > a.orderdetails` 形状（蓝链 + 右键菜单锚点）；
 * 聚合行在**号码左侧**加三角，来源单号 ≠ 内部单号时补一行灰色小字「系统单号：<内部号>」。
 * `title` 逐字；`data-orderidview` 是右键「复制系统订单编号」要复制的那个串（显示值本身）。
 */
export function sourceOrderTemplet(row: CoreOrderDisplayRow): string {
  const orderNo = row['t_orderid'] ?? ''
  const view = row['t_orderidView']
  const shown = view === undefined || view === '' ? orderNo : view
  const text = shown === '' ? EMPTY_VALUE : shown
  const groupSize = row['t_groupSize']
  const tree =
    groupSize === undefined
      ? ''
      : `<i class="layui-icon layui-icon-triangle-r ${ORDER_TREE_CLASS}"` +
        ` data-order="${escapeHtml(orderNo)}" data-open="N" title="展开 / 收起该订单的平台单"></i>`
  const innerNo =
    groupSize !== undefined && text !== orderNo
      ? `<br/><span class="${ORDER_INNER_NO_CLASS}">系统单号：${escapeHtml(orderNo)}</span>`
      : ''
  return (
    `<div class="${ORDER_LINE_CLASS}"` +
    ` id="order_${escapeHtml(orderNo)}"` +
    ` data-id="${escapeHtml(orderNo)}"` +
    ` data-orderidview="${escapeHtml(text)}"` +
    ` title="鼠标右键看看~">` +
    tree +
    `<a href="javascript:void(0);" class="${ORDER_DETAIL_LINK_CLASS}"` +
    ` data-id="${escapeHtml(orderNo)}"` +
    ` data-viewid="${escapeHtml(text)}"` +
    ` style="color:${SOURCE_ORDER_NO_COLOR}"` +
    ` title="点击查看系统订单详情，也可以鼠标右键试试看">${escapeHtml(text)}</a>` +
    innerNo +
    `</div>`
  )
}

/** 「最新一条推送」这个目标的键（聚合父行用；子行 / 扁平行用 `p:<…>`）。 */
export const ROW_TARGET_LATEST = 'latest'

/**
 * 聚合父行「交易状态 / 发单平台 / 发单价 / 发单时间」该取**哪一条**推送：**最新的一笔**
 * （`created_at` **降序**、`id` **降序**取第一条，⛔ **不看 `is_active`**——按「活跃优先」会选中
 * 最早那条失败的，父行就显示「发送失败」）。⛔ **不许回退到更早的成功单**（那会让页面说假话）；
 * 空组才回 `null`（父行画「—」；组内至少一条推送时这里明明有数据，「—」只该表示「没有数据源」）。
 * 时间缺失：全缺 ⇒ 保持后端给的顺序（组内 `pushes` 由后端按 `-created_at, -id` 排好）；
 * 只有一条有时间 ⇒ 有时间的赢；只有 `id` 能比 ⇒ 比 `id`。
 */
export function groupStatusPush(
  pushes: readonly PlatformPushSubRow[],
): PlatformPushSubRow | null {
  if (pushes.length === 0) return null
  const pool = pushes
  const timeOf = (push: PlatformPushSubRow): number => {
    const parsed = push.created_at === undefined ? Number.NaN : Date.parse(push.created_at)
    return Number.isNaN(parsed) ? Number.NaN : parsed
  }
  // `Array.prototype.sort` 是稳定排序（ES2019）⇒ 两边都没时间戳时保持后端顺序。
  return (
    [...pool].sort((a, b) => {
      const left = timeOf(a)
      const right = timeOf(b)
      const byId = (b.id ?? 0) - (a.id ?? 0)
      // 时间都没有 ⇒ 用 `id` 决（= 后端 `-created_at, -id` 的同序；id 也没有 ⇒ 稳定排序保持原序）。
      if (Number.isNaN(left) && Number.isNaN(right)) return byId
      if (Number.isNaN(left)) return 1
      if (Number.isNaN(right)) return -1
      if (left !== right) return right - left
      return byId
    })[0] ?? null
  )
}

/**
 * 聚合行 → 表格一行的**显示值**（后端 `group_by=order` 生效时才调用）。
 * 折叠行显示 订单号 / 系统单号 / 游戏区服 / 订单状态 / 平台数量（⛔ 不新增列）；
 * 发单平台 / 发单价 / 交易状态 / 发单时间取该单**最新一条**推送（与父行按钮同一套判据、
 * 整行同一个来源，⛔ 不允许「平台取 A 单、价格取 B 单」）；
 * 接单价 / 利润 / 剩余时间推送行没有 ⇒「—」（⛔ 不编）；其余列一律「—」（⛔ 不拿某一条子单的值冒充整单）。
 */
export function toGroupDisplayRow(group: PlatformPushGroupItem): CoreOrderDisplayRow {
  const server =
    group.server === null || group.server === '' ? group.game : `${group.game} / ${group.server}`
  // 父行这几个格子与那几颗行内按钮作用于**同一条**推送 ⇒ 只算一次、整行同一个来源；
  // 选择器统一到 `groupStatusPush`，`latestActivePush` 是它的别名（行为一模一样）。
  const activePush = groupStatusPush(group.pushes)
  /** 父行那一批「取最新一笔推送」的格子（接单价 / 利润 / 接手信息 / 倒计时也在内）。 */
  const push = activePush
  const verifyDeadline = (push?.verify_deadline_at ?? '').trim()
  const verifyBase =
    typeof push?.verify_remaining_seconds === 'number' ? String(push.verify_remaining_seconds) : ''
  const verifyAt = String(Date.now())
  return {
    t_tid: groupPlatformCountText(group.pushes),
    t_orderid: group.order_no,
    t_orderidView: sourceOrderNoOf(group),
    t_groupSize: String(group.pushes.length),
    t_gameServer: server,
    t_pushPltm: push === null ? EMPTY_VALUE : platformLabelOf(push.platform),
    t_throw: push === null || push.price === '' ? EMPTY_VALUE : push.price,
    // 接单价 = 该单本地接单价；利润 = 接单价 − 发单价（同一条推送，⛔ 不串单）。
    t_safeeff: acceptPriceText(push?.accept_price),
    profit: profitTextOf(push?.accept_price, push?.price),
    // 剩余时间取**最新一笔**推送的 `remaining_seconds`（同一来源）。
    ...(() => {
      const seconds =
        typeof push?.remaining_seconds === 'number' ? push.remaining_seconds : null
      const at = String(Date.now())
      return {
        t_shours: formatShoursRemaining(seconds),
        t_shoursBase: seconds === null ? '' : String(Math.floor(seconds)),
        t_shoursAt: at,
      }
    })(),
    t_pltSta: push === null ? EMPTY_VALUE : platformPushStatusLabel(push),
    t_pltStaClass: push === null ? '' : platformStatusBadgeClass(push),
    // 新增四列也取**同一条**推送。
    t_acceptUser: acceptUserText(push?.accept_user_name),
    t_acceptUid: (push?.accept_uid ?? '').trim(),
    t_acceptOver: countText(push?.accept_over_count),
    t_custCancel: countText(push?.cust_cancel_count),
    t_remoteAccept: formatDateTime(push?.remote_accepted_at),
    t_sta: group.order_status === '' ? EMPTY_VALUE : orderStatusLabel(group.order_status),
    t_staColor: orderStatusTextColor(group.order_status),
    t_verifyDeadline: verifyDeadline,
    t_verifyBase: verifyBase,
    t_verifyAt: verifyAt,
    t_exAccpetTime: formatVerifyCountdown(
      verifyRemainingSeconds(verifyDeadline, nullableNumberOf(verifyBase), verifyAt, Date.now()),
    ),
    t_soure: orderSourceText(push?.order_source),
    t_hour: push === null ? EMPTY_VALUE : formatDateTime(push.created_at),
    t_lastSynced: EMPTY_VALUE,
    t_state: EMPTY_VALUE,
    t_lastError: '',
    t_recTime: EMPTY_VALUE,
    t_complte: EMPTY_VALUE,
    t_create: EMPTY_VALUE,
    t_endTime: EMPTY_VALUE,
    t_pushMan: EMPTY_VALUE,
    // 父行按钮的目标 = 最新一条推送（与上面交易状态同一条）。
    t_pushKey: push === null ? '' : ROW_TARGET_LATEST,
    t_pushToken: push === null ? '' : (platformDisplayStatus(push)?.token ?? ''),
  }
}

/**
 * **`groupStatusPush` 的旧名字（别名）** —— 判据只有一处，⛔ 别再写第二份。
 * 名字里的「active」已过时（判据是**最新的一笔**、**不看 `is_active`**）；
 * 保留这个导出只是为了不动调用点 / 别的分支，**新代码请用 `groupStatusPush`**。
 */
export function latestActivePush(
  pushes: readonly PlatformPushSubRow[],
): PlatformPushSubRow | null {
  return groupStatusPush(pushes)
}

/**
 * **只有一条平台单**的订单 ⇒ 当普通行渲染（复用 `toDisplayRow` 的显示口径，
 * ⛔ 不在这里另写一份「哪列显示什么」）。
 * 0 条推送（后端契约异常）⇒ 照订单级事实平铺、不画三角、操作列不挂按钮（`t_pushKey` 为空）。
 */
export function toSinglePushDisplayRow(group: PlatformPushGroupItem): CoreOrderDisplayRow {
  const push = group.pushes[0]
  if (push === undefined) {
    const row = toGroupDisplayRow(group)
    // 0 条 ⇒ 不是「可折叠的组」（三角 / 按钮都没有对象可操作）。
    delete row['t_groupSize']
    row['t_pushKey'] = ''
    return row
  }
  return {
    ...toDisplayRow({
      id: push.id ?? 0,
      platform: push.platform,
      serial_no: push.serial_no,
      order_no: group.order_no,
      source_order_no: group.source_order_no,
      game: group.game,
      server: group.server,
      price: push.price,
      state: push.state,
      remote_status: push.remote_status,
      remote_cancel_status: push.remote_cancel_status,
      display_status: push.display_status,
      display_status_label: push.display_status_label,
      order_status: group.order_status,
      created_at: push.created_at ?? '',
      last_synced_at: push.last_synced_at ?? null,
      last_error: push.last_error ?? '',
      pay_level_bal: push.pay_level_bal,
      rep_ensure_bal: push.rep_ensure_bal,
      is_active: push.is_active,
    }),
    // 组内只有这一条 ⇒ 键固定 `p:0`（注册目标表时用同一个键）。
    t_pushKey: 'p:0',
  }
}

/**
 * 聚合子行（一条推送一行）**每一格**的内部 HTML。
 * 复用主表列定义：调用方按父行 `<td>` 的 `data-field` 逐格取值 ⇒ 列宽 / 对齐 / 隐藏列全部
 * 继承主表（与 `order-orderlist.ts::platformChildCellHtml` 同一条工程判断）。
 * 子行只放**这条推送自己的事实**，订单级信息（游戏区服、系统状态）留在父行，⛔ 不重复。
 */
export function childCellHtml(
  field: string,
  push: PlatformPushSubRow,
  target?: RowActionTarget,
): string {
  switch (field) {
    case 't_tid': {
      const serial = push.serial_no === null || push.serial_no === '' ? EMPTY_VALUE : push.serial_no
      return escapeHtml(serial)
    }
    case 't_pushPltm':
      return escapeHtml(platformLabelOf(push.platform))
    case 't_pltSta': {
      const text = platformPushStatusLabel(push)
      if (text === EMPTY_VALUE || text === '') return EMPTY_VALUE
      const cls = platformStatusBadgeClass(push)
      return `<span class="${escapeHtml(cls)}" title="该笔订单在平台的交易状态">${escapeHtml(text)}</span>`
    }
    case 't_throw':
      return push.price === '' ? EMPTY_VALUE : escapeHtml(push.price)
    // 子行各显示**自己那条**的接单价 / 利润 / 接手信息 / 倒计时 / 订单来源。
    case 't_safeeff':
      return escapeHtml(acceptPriceText(push.accept_price))
    case 'profit': {
      const text = profitTextOf(push.accept_price, push.price)
      return isNegativeMoney(text)
        ? `<span style="color:${escapeHtml(ROW_DANGER_COLOR)}">${escapeHtml(text)}</span>`
        : escapeHtml(text)
    }
    case 't_acceptUser': {
      const text = acceptUserText(push.accept_user_name)
      const uid = (push.accept_uid ?? '').trim()
      const title = uid === '' ? '' : ` title="${escapeHtml(`平台 UID：${uid}`)}"`
      return `<span${title}>${escapeHtml(text)}</span>`
    }
    case 't_acceptOver':
      return escapeHtml(countText(push.accept_over_count))
    case 't_custCancel': {
      const text = countText(push.cust_cancel_count)
      return custCancelAlert(push.cust_cancel_count)
        ? `<span style="color:${escapeHtml(ROW_DANGER_COLOR)}">${escapeHtml(text)}</span>`
        : escapeHtml(text)
    }
    case 't_remoteAccept':
      return escapeHtml(formatDateTime(push.remote_accepted_at))
    case 't_soure':
      return escapeHtml(orderSourceText(push.order_source))
    case 't_exAccpetTime':
      // 与父行**同一个**倒计时节点（带 `tt-verify-countdown` 才会被本地每秒刷新到）。
      return verifyCountdownHtmlOfPush(push, Date.now())
    case 't_shours':
      // 子行的剩余时间也用它自己那条的 `remaining_seconds`（同样每秒递减）。
      return shoursCountdownHtml(push.remaining_seconds, String(Date.now()), Date.now())
    case 't_change':
      // 与主表操作列**同一套 4 颗按钮**，目标是**这条推送自己**；没有目标 ⇒ 画「—」：
      // ⛔ 不摆一排点了只会报错的按钮。
      if (target === undefined) return EMPTY_VALUE
      return actionRowTemplate(target, platformDisplayStatus(push)?.token ?? '')
    default:
      return EMPTY_VALUE
  }
}

/**
 * 后端推送行 → 表格一行的**显示值**。
 * ⛔ **没有数据源的列一律「—」，不造 0 / 空串冒充**（`AGENTS.md` §5-E）——后端契约里没有那些键。
 * 判据写 `is not None` 而不是真值：`price` 是金额字符串、`server` 是 `null` 或名字，
 * 两者都可能是合法边界（`AGENTS.md` §5-E）。
 */
export function toDisplayRow(row: PlatformPushListRow): CoreOrderDisplayRow {
  const serial = row.serial_no === null || row.serial_no === '' ? '—' : row.serial_no
  const server =
    row.server === null || row.server === '' ? row.game : `${row.game} / ${row.server}`
  // 倒计时的三个原料（绝对截止 / 抓取时刻的快照秒数 / 抓取时刻本身）。
  const verifyDeadline = (row.verify_deadline_at ?? '').trim()
  const verifyBase =
    typeof row.verify_remaining_seconds === 'number'
      ? String(row.verify_remaining_seconds)
      : ''
  const verifyAt = String(Date.now())
  return {
    t_tid: serial,
    // ⚠️ `t_orderid` **恒是内部单号**：批量动作（勾选 → `order_nos`）读的就是它，
    // 见下方 `selectedOrderNos`。⛔ 不要把它改成来源单号（会让整批动作打错单）。
    t_orderid: row.order_no,
    // 「系统单号」这一格**显示**来源单号，空则回退内部单号（与管理订单页同一条回退规则）。
    t_orderidView: sourceOrderNoOf(row),
    t_gameServer: server,
    t_pushPltm: row.platform === FIXED_PLATFORM ? '代练通' : '—',
    t_throw: row.price === '' ? '—' : row.price,
    // 接单价 = 本地接单价；利润 = 接单价 − 发单价（缺一边「—」，负数照实显示）。
    t_safeeff: acceptPriceText(row.accept_price),
    profit: profitTextOf(row.accept_price, row.price),
    // 剩余时间直接用后端 `remaining_seconds`（⛔ 不从时间戳算）；页面本地每秒递减。
    ...(() => {
      const seconds =
        typeof row.remaining_seconds === 'number' ? row.remaining_seconds : null
      const at = String(Date.now())
      return {
        t_shours: formatShoursRemaining(seconds),
        t_shoursBase: seconds === null ? '' : String(Math.floor(seconds)),
        t_shoursAt: at,
      }
    })(),
    // 与订单详情页同一个出口：closed → 已关闭、远端码 → 中文、否则本地状态中文。
    // ⛔ failed / unknown 不再显示「—」——那等于这单没有状态。
    t_pltSta: platformPushStatusLabel(row),
    // 同一格还要带**底色档**（填充色块标签，不是纯文字）。
    t_pltStaClass: platformStatusBadgeClass(row),
    // 新增的四列（接手人 / 完成单数 / 投诉取消 / 接单时间）。
    t_acceptUser: acceptUserText(row.accept_user_name),
    t_acceptUid: (row.accept_uid ?? '').trim(),
    t_acceptOver: countText(row.accept_over_count),
    t_custCancel: countText(row.cust_cancel_count),
    t_remoteAccept: formatDateTime(row.remote_accepted_at),
    t_sta: row.order_status === '' ? '—' : orderStatusLabel(row.order_status),
    // 「系统状态」是**着色文字**；色值与「交易状态」的填充色档同住 `platformPush.ts`（只出现一次）。
    t_staColor: orderStatusTextColor(row.order_status),
    // 验收倒计时（页面本地算，见 `verifyRemainingSeconds`）。
    t_verifyDeadline: verifyDeadline,
    t_verifyBase: verifyBase,
    t_verifyAt: verifyAt,
    t_exAccpetTime: formatVerifyCountdown(
      verifyRemainingSeconds(verifyDeadline, nullableNumberOf(verifyBase), verifyAt, Date.now()),
    ),
    t_soure: orderSourceText(row.order_source),
    t_hour: formatDateTime(row.created_at),
    // 下面两格**不是列**，只服务「平台交易单号」那一格的 tooltip（`platformOrderHint`）。
    t_lastSynced: formatDateTime(row.last_synced_at),
    t_state: platformPushStateLabel(row.state),
    t_lastError: row.last_error,
    t_recTime: '—',
    t_complte: '—',
    t_create: '—',
    t_endTime: '—',
    t_pushMan: '—',
    // 这一行行内按钮作用于**这一条推送**（键由同一次取数的目标表解析；
    // 扁平一屏里同一订单可能有多行 ⇒ 键必须带推送 id，⛔ 不能只用订单号）。
    t_pushKey: `p:${row.id}`,
    // 行内按钮的**可见性判据**：这条推送的综合状态 token（见 `ROW_ACTION_STATUSES`）。
    t_pushToken: platformDisplayStatus(row)?.token ?? '',
  }
}

/** 接单价的显示：空 / 缺键 ⇒「—」（金额 0 是合法值，⛔ 不用真值判空）。 */
export function acceptPriceText(value: string | null | undefined): string {
  const text = (value ?? '').trim()
  return text === '' ? EMPTY_VALUE : text
}

/** 接手人的显示：空 / 缺键 ⇒「—」。 */
export function acceptUserText(value: string | null | undefined): string {
  const text = (value ?? '').trim()
  return text === '' ? EMPTY_VALUE : text
}

/** 扁平推送行 → 聚合子行的形状（行内动作要的那几个键；缺键按原值带过去，不补假值）。 */
export function subRowOfListRow(row: PlatformPushListRow): PlatformPushSubRow {
  return {
    platform: row.platform,
    serial_no: row.serial_no,
    state: row.state,
    remote_status: row.remote_status,
    remote_cancel_status: row.remote_cancel_status,
    display_status: row.display_status,
    display_status_label: row.display_status_label,
    price: row.price,
    pay_level_bal: row.pay_level_bal,
    rep_ensure_bal: row.rep_ensure_bal,
    is_active: row.is_active,
    id: row.id,
    created_at: row.created_at,
    last_synced_at: row.last_synced_at,
    last_error: row.last_error,
  }
}

/** 一个字段格子的 templet：正文过 `escapeHtml`（游戏名 / 区服名 / 单号都是接口回来的文本）。 */
function textTemplet(field: string): (row: CoreOrderDisplayRow) => string {
  return (row) => escapeHtml(row[field] ?? '—')
}

/**
 * 「交易状态」格：填充色块标签
 * `<span class="layui-badge layui-bg-XXX" title="该笔订单在平台的交易状态">文字</span>`。
 * 没有状态可显示（`—`）时**只画 `—`**：给一个灰标签会让「没有数据」看起来像一种状态。
 */
function statusTemplet(row: CoreOrderDisplayRow): string {
  const text = row['t_pltSta'] ?? '—'
  if (text === '—' || text === '') return '—'
  const cls = row['t_pltStaClass'] ?? ''
  const classAttr = cls === '' ? '' : ` class="${escapeHtml(cls)}"`
  return `<span${classAttr} title="该笔订单在平台的交易状态">${escapeHtml(text)}</span>`
}

/**
 * 「系统状态」格：**着色文字** `<span style="color:<色值>">文字</span>`；
 * 色值来自 `platformPush.ts::ORDER_STATUS_TEXT_COLORS`（与本页「交易状态」的填充色档同住一处）。
 * ⚠️ 那一格在对标里是可点的行内入口，本版没有（改系统状态走工具栏批量那颗）⇒ 用 `<span>`，
 * ⛔ 不摆一个点了没反应的链接。没有状态可显示（`—`）时只画「—」：⛔ 不给「没有数据」上色。
 */
function systemStatusTemplet(row: CoreOrderDisplayRow): string {
  const text = row['t_sta'] ?? '—'
  if (text === '—' || text === '') return '—'
  const color = row['t_staColor'] ?? ''
  if (color === '') return escapeHtml(text)
  return `<span style="color:${escapeHtml(color)}">${escapeHtml(text)}</span>`
}

/**
 * 「平台交易单号」那一格的悬停提示（**不新增列**）。
 * 内容 = `最近同步：<last_synced_at>` + `状态：<本地推送状态>`，`last_error` 非空时再拼
 * `错误：<原文>`。三者都是接口真值；没有同步时间 ⇒「—」，⛔ 不编一个时间。
 * ⚠️ 这条提示是对标没有的加法 ⇒ 像素对拍看不到，但 DOM 里多一个 `title` 属性（文件头差异 15）。
 */
export function platformOrderHint(row: CoreOrderDisplayRow): string {
  const parts = [
    `最近同步：${row['t_lastSynced'] ?? '—'}`,
    `状态：${row['t_state'] ?? '—'}`,
  ]
  const lastError = row['t_lastError'] ?? ''
  if (lastError !== '') parts.push(`错误：${lastError}`)
  return parts.join(' · ')
}

/**
 * 「平台交易单号」格：正文是单号（父行是**平台数量**），附加信息只进 `title`。
 * ⚠️ 不新增「最近同步」列（见 `platformOrderHint`）；聚合折叠行显示平台数量、悬停说明可展开；
 * 单号下面的「设标签」按钮已整条删除 ⇒ 这一格只有文本。
 */
function serialTemplet(row: CoreOrderDisplayRow): string {
  const groupSize = row['t_groupSize']
  const hint =
    groupSize === undefined
      ? platformOrderHint(row)
      : groupCountTitle(Number.parseInt(groupSize, 10) || 0)
  return `<span title="${escapeHtml(hint)}">${escapeHtml(row['t_tid'] ?? '—')}</span>`
}

/** 行内按钮要作用的**目标推送**（挂在按钮的 `data-*` 上；点击时按它回查同一次取数的那条推送）。 */
export type RowActionTarget = {
  /** 本地订单号（后端写动作的路径参数，⛔ 不是平台单号）。 */
  orderNo: string
  /** 目标推送的键（`latest` = 该单最新一条推送；`p:<i>` = 组内第 i 条 / 扁平行的 id）。 */
  pushKey: string
}

/**
 * 「操作」列的行内按钮（4 颗，**按这条推送的交易状态决定出现哪几颗**）。
 * `target` 带 `data-order` / `data-push`，点击委托才能定位到要写的那一单那一条；
 * `token` = 该推送的综合状态（可见性判据见 `ROW_ACTION_STATUSES`）；
 * 一颗都不该出现 ⇒ 返回 `—`（⛔ 不摆点了必被拒的按钮、⛔ 不打「尚未开放」的假入口）。
 * ⛔ 不接 `lay-submit`（会被 layui 的 form 委托吞掉）；`href="javascript:void(0)"` 与对标一致。
 */
export function actionRowTemplate(target: RowActionTarget, token: string): string {
  // 这 4 颗全是**平台写动作** ⇒ 挂 `order.platform_write`（没权限画「—」，fail-closed）；
  // 判据见 `@/layui-island/perms::canPlatformWrite`（含后端未上线节点时的降级）。
  if (!platformWriteAllowedNow()) return EMPTY_VALUE
  const visible = ROW_ACTION_BUTTONS.filter((item) =>
    rowActionKeysFor(token).includes(item.key),
  )
  if (visible.length === 0) return EMPTY_VALUE
  const attrs =
    ` data-order="${escapeHtml(target.orderNo)}"` +
    ` data-push="${escapeHtml(target.pushKey)}"`
  // 「编辑 + 撤回」（11）与「协商撤销 + 加款」（12/13）**互斥** ⇒ 最多同时 2 颗，
  // 一行就够（⛔ 不为了「看起来像对标」插一个换行把两颗按钮挤成两行）。
  return visible
    .map(
      (item) =>
        `<a href="javascript:void(0)" class="layui-btn layui-btn-xs ${item.cls}"` +
        ' style="margin-right: 0px;margin-bottom: 0px;"' +
        ` data-platform-action="${item.key}"${attrs}>${item.label}</a>`,
    )
    .join('')
}

/**
 * **24 列**（改列后的最终集合；与对标 25 列的差异见文件头「h6x N7」）。
 * 刻意保留的取舍：
 *  - `merge:'orderId'` 不抄：`tableMerge` 扩展不在 vendored layui 里；
 *  - 「验收倒计」用 `t_exAccpetTime`（对标「验收倒计」与「录单时间」同 `field` 会互相污染，缺陷改掉）；
 *  - 有数据的列补 `templet`：正文过 `escapeHtml`（游戏名 / 区服名 / 单号是接口文本）；
 *  - 操作列 4 颗按钮由 `actionRowTemplate()` 渲染（按交易状态出现，见 `ROW_ACTION_STATUSES`）。
 */
export function buildCols(): CoreOrderCol[] {
  return [
    // 勾选列（`type:'checkbox'`，无标题）。
    { unresize: false, field: 't_orderId0', type: 'checkbox', width: 35 },
    {
      unresize: false,
      title: '平台交易单号',
      field: 't_tid',
      align: 'center',
      width: 180,
      // 附加信息（最近同步 / 推送状态 / 最后错误）只进这一格的 `title`，⛔ 不新增列。
      templet: serialTemplet,
    },
    {
      unresize: false,
      title: '系统单号',
      field: 't_orderid',
      align: 'left',
      width: 140,
      // 显示**来源单号**（空则回退内部单号）、蓝色、右键可复制。
      templet: sourceOrderTemplet,
    },
    { unresize: false, title: '游戏区服', field: 't_gameServer', align: 'center', width: 120, templet: textTemplet('t_gameServer') },
    { unresize: false, title: '发单平台', field: 't_pushPltm', align: 'center', width: 70 },
    {
      unresize: false,
      title: '发单价',
      align: 'center',
      field: 't_throw',
      width: 70,
      sort: true,
      templet: textTemplet('t_throw'),
    },
    // 接单价 / 利润（负数标红）；利润 = 接单价 − 发单价（缺一边「—」）。
    {
      unresize: false,
      title: '接单价',
      align: 'center',
      field: 't_safeeff',
      width: 70,
      sort: true,
      templet: dangerMoneyTemplet('t_safeeff'),
    },
    {
      unresize: false,
      title: '利润',
      align: 'center',
      field: 'profit',
      width: 80,
      sort: true,
      // 合计行文案 + 列级 `totalRow`。
      totalRowText: '本页合计',
      totalRow: true,
      templet: dangerMoneyTemplet('profit'),
    },
    {
      unresize: false,
      title: '剩余时间',
      align: 'center',
      field: 't_shours',
      width: 90,
      sort: true,
      // 本地每秒递减的倒计时节点（⛔ 不轮询后端）。
      templet: (row: CoreOrderDisplayRow) =>
        shoursCountdownHtml(
          row['t_shoursBase'] === undefined || row['t_shoursBase'] === ''
            ? null
            : Number(row['t_shoursBase']),
          row['t_shoursAt'] ?? String(Date.now()),
          Date.now(),
        ),
    },
    {
      unresize: false,
      title: '交易状态',
      align: 'center',
      field: 't_pltSta',
      width: 70,
      // 填充色块标签，不是纯文字。
      templet: statusTemplet,
    },
    // 新增四列（展示在「交易状态」附近）。
    { unresize: false, title: '接手人', align: 'center', field: 't_acceptUser', width: 90, templet: acceptUserTemplet },
    { unresize: false, title: '完成单数', align: 'center', field: 't_acceptOver', width: 80 },
    {
      unresize: false,
      title: '投诉/取消',
      align: 'center',
      field: 't_custCancel',
      width: 90,
      templet: alertCountTemplet('t_custCancel'),
    },
    {
      unresize: false,
      title: '接单时间',
      align: 'center',
      field: 't_remoteAccept',
      width: 130,
      sort: true,
      templet: textTemplet('t_remoteAccept'),
    },
    {
      unresize: false,
      title: '系统状态',
      align: 'center',
      field: 't_sta',
      width: 70,
      // 按状态着色文字。
      templet: systemStatusTemplet,
    },
    {
      unresize: false,
      title: '验收倒计',
      align: 'center',
      field: 't_exAccpetTime',
      width: 110,
      sort: true,
      // 页面本地倒计时（每秒就地刷新，⛔ 不轮询后端）。
      templet: verifyCountdownTemplet,
    },
    // 这一列**默认可见**（文件头第 10 条）。
    { unresize: false, title: '订单来源', align: 'center', field: 't_soure', width: 80, hide: false },
    {
      unresize: false,
      title: '发单时间',
      align: 'center',
      field: 't_hour',
      width: 80,
      sort: true,
      templet: textTemplet('t_hour'),
    },
    { unresize: false, title: '接手时间', align: 'center', field: 't_recTime', width: 80, sort: true },
    { unresize: false, title: '完单时间', align: 'center', field: 't_complte', width: 80, sort: true },
    // 录单时间：默认不显示——照抄，不摊开。
    { unresize: false, title: '录单时间', align: 'center', field: 't_create', width: 80, hide: true, sort: true },
    { unresize: false, title: '撤销时间', align: 'center', field: 't_endTime', width: 80, sort: true },
    { unresize: false, title: '发单人', align: 'center', field: 't_pushMan', width: 80 },
    // 操作列：右侧固定，**每行都挂**（折叠父行也挂）。
    {
      unresize: false,
      field: 't_change',
      title: '操作',
      width: 140,
      fixed: 'right',
      // 两颗判据：
      //  ① `t_pushKey` 为空 = 这一行没有可操作的推送（例如聚合行 0 条子单）⇒ 画「—」；
      //  ② `t_pushToken` = 该推送的综合状态 ⇒ 只画**这个状态允许的那几颗**
      //     （全都不适用时同样画「—」，⛔ 不摆点了必被拒的按钮）。
      templet: (row: CoreOrderDisplayRow) => {
        const pushKey = row['t_pushKey'] ?? ''
        if (pushKey === '') return EMPTY_VALUE
        return actionRowTemplate(
          { orderNo: row['t_orderid'] ?? '', pushKey },
          row['t_pushToken'] ?? '',
        )
      },
    },
  ]
}

/** 「钱」那一格的 templet：**负数标红**（利润可能为负），其余照常；危险色取 `ROW_DANGER_COLOR`。 */
function dangerMoneyTemplet(field: string): (row: CoreOrderDisplayRow) => string {
  return (row) => {
    const text = row[field] ?? EMPTY_VALUE
    if (!isNegativeMoney(text)) return escapeHtml(text)
    return `<span style="color:${escapeHtml(ROW_DANGER_COLOR)}">${escapeHtml(text)}</span>`
  }
}

/** 计数列（投诉/取消）：**> 0 标红**，其余照常。 */
function alertCountTemplet(field: string): (row: CoreOrderDisplayRow) => string {
  return (row) => {
    const text = row[field] ?? EMPTY_VALUE
    const value = Number.parseInt(text, 10)
    if (!Number.isFinite(value) || value <= 0) return escapeHtml(text)
    return `<span style="color:${escapeHtml(ROW_DANGER_COLOR)}">${escapeHtml(text)}</span>`
  }
}

/** 「接手人」格：显示名 + 悬停 `title` 显示平台 UID（没有 UID 就不加 `title`）。 */
function acceptUserTemplet(row: CoreOrderDisplayRow): string {
  const text = row['t_acceptUser'] ?? EMPTY_VALUE
  const uid = (row['t_acceptUid'] ?? '').trim()
  const title = uid === '' ? '' : ` title="${escapeHtml(`平台 UID：${uid}`)}"`
  return `<span${title}>${escapeHtml(text)}</span>`
}

/** 「验收倒计」格：页面本地倒计时节点（`data-*` 供 `tickVerifyCountdowns` 就地刷新）。 */
function verifyCountdownTemplet(row: CoreOrderDisplayRow): string {
  return verifyCountdownHtml(row, Date.now())
}

/** 导出给单测对拍表头（与页面真正交给 layui 的那一份同源，不是另抄一遍）。 */
export const COLS_FOR_TEST: CoreOrderCol[] = buildCols()

/**
 * `table.render` 里**不随取数变化**的那部分。
 * 单测钉它、`mount` 里展开它 ⇒ 这几个「静默出错」的键（`page`/`toolbar`/`defaultToolbar`/
 * `totalRow`）不是只在测试里对，就是线上那一份。
 */
export const TABLE_OPTIONS: Record<string, unknown> = {
  elem: TABLE_ELEM,
  // ⚠️ **不在这里放 `data`**：每一屏的 `data` 由取数回包传入（`{...TABLE_OPTIONS, data: rows}`）。
  // ⛔ 不写死空数组——那会让「没取到数据」与「后端真的 0 行」看起来一样。
  // `totalRow` = 表底合计行（0 行时 layui 会带 `layui-hide-v`，仍占位）。
  totalRow: true,
  toolbar: TOOLBAR_ELEM,
  // 内置「筛选列」+ 一颗自定义「重置表格排版」。
  defaultToolbar: [
    'filter',
    { title: RESET_TABLE_LABEL, layEvent: 'RESET_TABLE', icon: 'layui-icon-fonts-clear' },
  ],
  cols: [buildCols()],
  // `page: false` ⇒ 分页栏是**手写 layPage**（`#page`），不是表格自带那条。
  page: false,
  // ⚠️ 这两个值**因 `page:false` 不生效**；真正生效的是 `PAGE_LIMIT`/`PAGE_LIMITS`。
  limit: 30,
  limits: [20, 40],
}

/**
 * 页签文案的「屏幕值」修正：第 2 个页签证据件写「未接手」，屏幕上是「待接单」（文件头第 4 条）。
 * 只改**文本节点**，`lay-id` / `class` / `lay-tips` 一律不动（`lay-id` 是筛选值、`class` 是
 * 计数回填的锚点）。找不到该 `li` 时静默返回——那是证据件结构变了，由页测试报，不在这里抛。
 */
export function applyPendingTabLabel(root: HTMLElement): void {
  const li = root.querySelector<HTMLElement>(
    `.layui-tab[lay-filter="staTab"] > .layui-tab-title > li[lay-id="${PENDING_TAB_ID}"]`,
  )
  if (li) li.textContent = PENDING_TAB_LABEL
}

/** 按钮上「给人看的那个名字」：取可见文字并折叠空白（导出订单那颗的 `<i>` 没有文字）。 */
function clickLabel(el: HTMLElement): string {
  return (el.textContent ?? '').trim().replace(/\s+/g, ' ')
}

/**
 * 日期区间：`#datetime` 是 laydate 的 **range** 输入，`#sDate` / `#eDate` 两个隐藏域由回调回填。
 * 口径：跨度 > 90 天 ⇒ 清空输入 + 逐字提示「日期最多选择90天」；合法区间 ⇒ 回填两个隐藏域
 * （本版没有搜索，这两个值当前**没有消费方**；留着是为了将来接后端时不再改动这层）。
 * ⚠️ 本版**不触发搜索**（文件头第 8 条）：选完日期只是控件值变化。
 */
export function initDateRange(
  root: HTMLElement,
  laydate: LayuiModule,
  msg: (text: string) => void,
): void {
  const hiddenStart = root.querySelector<HTMLInputElement>('input#sDate')
  const hiddenEnd = root.querySelector<HTMLInputElement>('input#eDate')
  const visible = root.querySelector<HTMLInputElement>('input#datetime')
  ;(laydate.render as (o: unknown) => void)({
    elem: '#datetime',
    range: true,
    min: -180,
    done: (value: string) => {
      if (hiddenStart) hiddenStart.value = ''
      if (hiddenEnd) hiddenEnd.value = ''
      const [start, end] = String(value ?? '').split(' - ')
      if (start === undefined || end === undefined) return
      const startMs = Date.parse(start)
      const endMs = Date.parse(end)
      if (Number.isNaN(startMs) || Number.isNaN(endMs)) return
      const spanDays = (endMs - startMs) / (1000 * 60 * 60 * 24)
      if (spanDays > DATE_SPAN_MAX_DAYS) {
        // 清掉输入框里的区间（对标清空按钮的效果）。
        if (visible) visible.value = ''
        msg(DATE_SPAN_MSG)
        return
      }
      if (hiddenStart) hiddenStart.value = start
      if (hiddenEnd) hiddenEnd.value = end
    },
  })
}

/**
 * 手写分页。
 *  - `count` = 服务端回来的真实总数（`data.total`）；**不编条数**；
 *  - `count <= limit` 时把 `#page` 整块隐藏 ⇒ 0 行时看不到分页栏；
 *  - `jump` 更新本地页码 / 每页条数后调 `onJump`（页面重取那一页）。
 */
export function renderPager(
  root: HTMLElement,
  laypage: LayuiModule,
  state: { curr: number; limit: number },
  count: number,
  onJump?: () => void,
): void {
  const pageEl = root.querySelector<HTMLElement>(PAGE_ELEM_SELECTOR)
  if (pageEl) pageEl.style.display = count <= state.limit ? 'none' : ''
  ;(laypage.render as (o: unknown) => void)({
    elem: PAGE_ELEM_ID,
    limit: state.limit,
    limits: [...PAGE_LIMITS],
    curr: state.curr,
    count,
    layout: [...PAGE_LAYOUT],
    jump: (obj: { curr?: unknown; limit?: unknown }) => {
      const nextCurr = typeof obj.curr === 'number' ? obj.curr : state.curr
      const nextLimit = typeof obj.limit === 'number' ? obj.limit : state.limit
      // ⚠️ laypage 在 `count=0` / 重新 render 时会用**原值**回调一次 `jump`；照旧无脑
      // `onJump()` 就会「load → render → jump → load」递归把页面卡死。
      const changed = nextCurr !== state.curr || nextLimit !== state.limit
      state.curr = nextCurr
      state.limit = nextLimit
      if (!changed) return
      onJump?.()
    },
  })
}

/** 页签 `li` 的列表（⚠️ 选择器与 `lay-id` 取值分成常量，E-0061 ①）。 */
function tabItems(root: HTMLElement): HTMLElement[] {
  return Array.from(
    root.querySelectorAll<HTMLElement>(
      '.layui-tab[lay-filter="staTab"] > .layui-tab-title > li[lay-id]',
    ),
  )
}

/** 页签 `lay-id` → 要发给后端的 `status`；`全部` 与「没有数据源」的档回 `undefined`。 */
export function statusForTab(tabId: string): number | undefined {
  return TAB_STATUS_MAP[tabId]
}

/** 该页签是否**没有数据源**（点了零请求、空表 + 提示）。 */
export function isNoDataSourceTab(tabId: string): boolean {
  return NO_DATA_SOURCE_TAB_IDS.includes(tabId)
}

/**
 * 把计数写进页签文本：`全部(3)` / `待接单(—)`。
 * ⚠️ **拿不到就是「—」，⛔ 不写 0**（0 是「查询过且确实为 0」，与「没查过」是两件事）。
 * 基准文案存在 `dataset.baseLabel`（挂载时记住，避免每轮在已带计数的文本上再拼一层）。
 */
function setTabCount(li: HTMLElement, count: number | null): void {
  const base = li.dataset.baseLabel ?? (li.textContent ?? '').trim()
  li.textContent = `${base}(${count === null ? '—' : String(count)})`
}

/** 挂载时记住 14 个页签的**基准文案**（已做过「未接手 → 待接单」的屏幕值修正）。 */
function rememberTabLabels(root: HTMLElement): void {
  for (const li of tabItems(root)) {
    li.dataset.baseLabel = (li.textContent ?? '').trim()
    setTabCount(li, null)
  }
}

/** 把所有页签计数复位成「—」（搜索条件变了 ⇒ 旧计数不再可信）。 */
function resetTabCounts(root: HTMLElement): void {
  for (const li of tabItems(root)) setTabCount(li, null)
}

/* ── 需求 5：页签数字一次加载（`GET /platform-pushes/counts/`） ───────── */

/** 页签数字加载失败的前缀（后面拼后端中文原文；⛔ 不静默、⛔ 不写 0）。 */
export const TAB_COUNTS_FAILED_PREFIX = '页签数字加载失败：'

/**
 * 把 `counts/` 的一次回包写进 14 个页签。
 * 映射（与 `TAB_STATUS_MAP` 同源）：`全部` ⇒ `total`；8 个有远端码的页签 ⇒
 * `counts["<status>"]`（缺桶/非数字 ⇒「—」，⛔ 不补 0）；5 个没有数据源的页签 ⇒「—」
 * （⛔ 拿相近码顶替）。`counts === null` = 这次没取到 ⇒ 全部复位成「—」。
 */
export function applyTabCounts(
  root: HTMLElement,
  counts: PlatformPushCounts | null,
): void {
  for (const li of tabItems(root)) {
    const tabId = li.getAttribute('lay-id') ?? ''
    let count: number | null = null
    if (counts !== null) {
      if (tabId === ALL_TAB_ID) {
        count = typeof counts.total === 'number' ? counts.total : null
      } else {
        const status = statusForTab(tabId)
        if (status !== undefined) {
          const raw = counts.counts?.[String(status)]
          count = typeof raw === 'number' ? raw : null
        }
      }
    }
    setTabCount(li, count)
  }
}

/* ── 需求 6：10 个筛选控件的「后端未支持」登记 ─────────────────────── */

/** 一条「后端未支持」的筛选控件（`id` = 证据件里的控件 id）。 */
export type UnsupportedFilterHit = { id: string; label: string; reason: string }
type UnsupportedFilter = UnsupportedFilterHit & {
  /** 用户是否把它改成了「非默认值」（默认值 = 证据件里第一个 option，不写死租户数据）。 */
  isChanged: (root: HTMLElement) => boolean
}

/** 下拉的「改过」判据 = **选中的不是第一个 option**（默认值就是第一项，不写死 value）。 */
function selectChanged(id: string): (root: HTMLElement) => boolean {
  return (root) => {
    const el = root.querySelector<HTMLSelectElement>(`select#${id}`)
    return el !== null && el.selectedIndex > 0
  }
}

/** 日期区间的「改过」判据：输入框 / 两个隐藏域任一非空。 */
function dateRangeChanged(root: HTMLElement): boolean {
  const read = (selector: string): string =>
    (root.querySelector<HTMLInputElement>(selector)?.value ?? '').trim()
  return (
    read('input#datetime') !== '' ||
    read('input#sDate') !== '' ||
    read('input#eDate') !== ''
  )
}

/**
 * 本页**改了值也不会影响取数**的筛选控件（⛔ 不做「看起来能筛、实际没传参」的假控件）。
 * 判据来源：后端 `PlatformPushQuerySerializer` 只收 `platform` / `status` / `keyword` /
 * `page` / `page_size`（聚合再加 `group_by`）⇒ 9 个控件登记「后端未支持」
 * （悬停 title + 点搜索时一句中文提示）。
 * ⚠️ `发单人`（`#operator`）在证据件里本身是 `layui-hide`（用户改不到）⇒ 不登记。
 */
export const UNSUPPORTED_FILTERS: readonly UnsupportedFilter[] = [
  {
    id: 'gameName',
    label: '游戏',
    reason: '读面没有游戏参数',
    isChanged: selectChanged('gameName'),
  },
  {
    id: 'dateType',
    label: '发单时间',
    reason: '读面没有时间区间参数（发单 / 接手 / 完单 / 撤销时间都不支持）',
    isChanged: dateRangeChanged,
  },
  {
    id: 'orderSoures',
    label: '订单来源',
    reason: '读面没有订单来源参数',
    isChanged: selectChanged('orderSoures'),
  },
  {
    id: 'pushPlatm',
    label: '发单平台',
    reason: '读面 platform 缺省固定为代练通(10)，没有「全部平台」的合并口径',
    isChanged: selectChanged('pushPlatm'),
  },
  {
    id: 'orderSta',
    label: '系统状态',
    reason: '读面没有 order_status 参数',
    isChanged: selectChanged('orderSta'),
  },
  {
    id: 'projectType',
    label: '项目分类',
    reason: '读面没有项目分类参数',
    isChanged: selectChanged('projectType'),
  },
  {
    id: 'seachType',
    label: '搜索类型',
    reason: 'keyword 同时匹配系统单号与平台单号，不按搜索类型区分',
    isChanged: selectChanged('seachType'),
  },
  {
    id: 'orderLabel',
    label: '订单标签',
    reason: '读面没有订单标签参数（列表也不回 order_label）',
    isChanged: selectChanged('orderLabel'),
  },
  {
    id: 'orderBy',
    label: '排序方序',
    reason: '读面固定按发单时间倒序（-created_at, -id）',
    isChanged: selectChanged('orderBy'),
  },
]

/** 页面上「后端未支持」登记的固定前缀（title / 提示共用，改口径只改这里）。 */
export const UNSUPPORTED_FILTER_PREFIX = '后端未支持'

/** 哪些「后端未支持」的控件被改成了非默认值（点搜索时用来给一句中文提示）。 */
export function unsupportedFilterHits(root: HTMLElement): UnsupportedFilterHit[] {
  return UNSUPPORTED_FILTERS.filter((filter) => filter.isChanged(root)).map(
    ({ id, label, reason }) => ({ id, label, reason }),
  )
}

/** 搜索时的提示语（写明「本次没按它筛」，⛔ 不让人以为已经生效）。 */
export function unsupportedFiltersMessage(hits: readonly UnsupportedFilterHit[]): string {
  return `「${hits.map((hit) => hit.label).join('」「')}」筛选${
    UNSUPPORTED_FILTER_PREFIX
  }，本次搜索未按它筛选`
}

/**
 * 把「后端未支持」登记到控件上：给每个控件加 `title`（悬停可见）。
 * ⚠️ 日期那一组有两个可见控件（`#dateType` / `#datetime`），两个都要登记；
 * 其它控件只登记自己那个 id。⛔ 不改 `value` / option / 其它属性（证据件一字不改）。
 */
export function registerUnsupportedFilters(root: HTMLElement): void {
  for (const filter of UNSUPPORTED_FILTERS) {
    const ids = filter.id === 'dateType' ? ['dateType', 'datetime'] : [filter.id]
    for (const id of ids) {
      const el = root.querySelector<HTMLElement>(`#${id}`)
      if (el !== null) el.title = `${UNSUPPORTED_FILTER_PREFIX}：${filter.reason}`
    }
  }
}

/* ── 需求 1：按订单聚合折叠（后端 `group_by=order` 生效时才画三角） ──────── */

/** 三角点了但本页拿不到该单的子单数据（理论上不该发生；⛔ 不静默）。 */
export const GROUP_PUSHES_MISSING_MESSAGE = '该订单的平台单数据缺失，请刷新后再试'

/**
 * 找右侧固定列表里**同一行**的副本（「操作」列是 `fixed:'right'`，layui 另画一张表）。
 * 判据：同一个 `.layui-table-view` 内、`tr[data-index]` 与主表行相同。拿不到 ⇒ 只插主表
 * （不抛错：`fixed` 列被隐藏 / 版本差异时表仍要能展开）。
 */
export function findFixedRow(tr: HTMLTableRowElement): {
  body: HTMLElement | null
  row: HTMLTableRowElement | null
} {
  const view = tr.closest('.layui-table-view')
  const index = tr.dataset.index ?? ''
  if (view === null || index === '') return { body: null, row: null }
  const body = view.querySelector<HTMLElement>('.layui-table-fixed-r tbody')
  const row =
    body === null
      ? null
      : body.querySelector<HTMLTableRowElement>(`tr[data-index="${index}"]`)
  return { body, row }
}

/** 删掉紧跟在这一行后面、**连贯**的平台子行（⛔ 不全局按 class 删：别的组可能也开着）。 */
export function removeOrderChildRows(tr: HTMLTableRowElement): void {
  let next = tr.nextElementSibling
  while (
    next instanceof HTMLTableRowElement &&
    next.classList.contains(ORDER_CHILD_ROW_CLASS)
  ) {
    const doomed = next
    next = next.nextElementSibling
    doomed.remove()
  }
}

/** 清掉页面上所有聚合子行（每次重渲染前调 ⇒ 翻页 / 刷新 / 搜索后**折叠状态复位**）。 */
export function clearOrderChildRows(root: HTMLElement): void {
  for (const row of Array.from(
    root.querySelectorAll<HTMLTableRowElement>(ORDER_CHILD_ROW_SELECTOR),
  )) {
    row.remove()
  }
}

/* ── N5：右侧固定列与主表逐行对齐 ────────────
 * `fixed:'right'` 是**另一张表**（两份 `<tr>` 各自布局），本页插行后 layui 自己的
 * `calcFixedRowHeight()` 只在 render 末尾跑一次 ⇒ 没有人再对齐（见文件头 N5）。
 * 补法：行高逐行同步 + 固定列格撑满并垂直居中；调用点在 done / 展开 / 收起 / resize。
 */

/** 主表 body 那层（layui 把 `.layui-table-main` 画在 body 上，行都在这层里）。 */
export const TABLE_MAIN_CLASS = 'layui-table-main'
/** 固定列容器（左 / 右各一个，类名再带 `-l` / `-r`）。 */
export const TABLE_FIXED_CLASS = 'layui-table-fixed'
/** 固定列里的 body 层（`calcFixedRowHeight` 也是从这里取行）。 */
export const TABLE_FIXED_BODY_CLASS = 'layui-table-body'
/** 整张表的根（`table.render` 的产物）。 */
export const TABLE_VIEW_CLASS = 'layui-table-view'

/** 某个 body 层里的行（只要**直接子级** `<tr>`：`tbody > tr`）。 */
function bodyRowsOf(container: Element | null): HTMLTableRowElement[] {
  if (container === null) return []
  const tbody = container.querySelector('tbody')
  if (tbody === null) return []
  return Array.from(tbody.children).filter(
    (el): el is HTMLTableRowElement => el instanceof HTMLTableRowElement,
  )
}

/**
 * 把主表每一行的高度**逐行**同步给固定列（左 / 右都同步），并让固定列格里的内容垂直居中。
 * 判据（真浏览器实测）：每行按钮垂直中心与该行文字垂直中心偏差 ≤ 2px。
 * ⚠️ 行是按**行序**配对的（layui 两张表的行序一致；本页插子行时也是两边同序插入）。
 * ⚠️ 子行**不写 `data-index`**（它们不在 layui 的数据缓存里，乱填 index 会让 layui 的行事件
 * 指到错的记录）——对齐只靠高度。
 */
export function syncFixedRowHeights(root: HTMLElement): void {
  for (const view of Array.from(root.querySelectorAll<HTMLElement>(`.${TABLE_VIEW_CLASS}`))) {
    const mainRows = bodyRowsOf(view.querySelector(`.${TABLE_MAIN_CLASS}`))
    if (mainRows.length === 0) continue
    const heights = mainRows.map((tr) => Math.round(tr.getBoundingClientRect().height))
    for (const fixed of Array.from(
      view.querySelectorAll<HTMLElement>(`.${TABLE_FIXED_CLASS}`),
    )) {
      const fixedRows = bodyRowsOf(fixed.querySelector(`.${TABLE_FIXED_BODY_CLASS}`))
      fixedRows.forEach((tr, index) => {
        const height = heights[index]
        // 量不到（还没布局 / 被隐藏）⇒ 不动，⛔ 不写 0px 把行压扁。
        if (height === undefined || height <= 0) return
        tr.style.height = `${height}px`
        for (const cell of Array.from(tr.querySelectorAll<HTMLElement>('.layui-table-cell'))) {
          cell.style.height = '100%'
          cell.style.display = 'flex'
          cell.style.alignItems = 'center'
          cell.style.flexWrap = 'wrap'
        }
      })
    }
  }
}

/**
 * 照**父行自己的单元格骨架**造一条平台子行（列宽 / 对齐 / 隐藏列全部继承主表）。
 * 与 `order-orderlist.ts::buildPlatformChildRow` 同一条工程判断：照父行复制 ⇒
 * 逐格对齐是**继承来的**，⛔ 不自己量尺寸。
 * `data-index` 故意不写：子行不进 layui 的数据缓存（批量勾选 / 翻页不认识它）。
 */
export function buildOrderChildRow(
  parentTr: HTMLTableRowElement,
  push: PlatformPushSubRow,
  orderNo: string,
  pushKey = '',
): HTMLTableRowElement {
  const tr = document.createElement('tr')
  tr.className = ORDER_CHILD_ROW_CLASS
  tr.dataset.order = orderNo
  for (const parentTd of Array.from(parentTr.children)) {
    if (!(parentTd instanceof HTMLTableCellElement)) continue
    const td = document.createElement('td')
    td.className = parentTd.className
    if (parentTd.dataset.field !== undefined) td.dataset.field = parentTd.dataset.field
    const style = parentTd.getAttribute('style')
    if (style !== null) td.setAttribute('style', style)
    const inner = parentTd.firstElementChild
    const cell = document.createElement('div')
    cell.className =
      inner instanceof HTMLElement && inner.className !== ''
        ? inner.className
        : 'layui-table-cell'
    cell.innerHTML = childCellHtml(
      parentTd.dataset.field ?? '',
      push,
      pushKey === '' ? undefined : { orderNo, pushKey },
    )
    td.appendChild(cell)
    tr.appendChild(td)
  }
  return tr
}

/** 把右键菜单的样式表挂进页内（按 id 去重；⛔ 不改证据件）。 */
export function ensureContextMenuStyle(root: HTMLElement): void {
  if (root.querySelector(`#${CONTEXT_MENU_STYLE_ID}`) !== null) return
  const style = document.createElement('style')
  style.id = CONTEXT_MENU_STYLE_ID
  style.textContent = CONTEXT_MENU_STYLE_RULES
  root.prepend(style)
}

/** 挂载整页；返回卸载函数。 */
export async function mountCoreOrderCenterPage(
  root: HTMLElement,
  _ctx: IslandContext,
): Promise<() => void> {
  // ⚠️ **先挂样式再塞标记**（E-0067）；`element` 由 `bootstrapIsland` 自己补（页签切换要用）。
  const layui = await bootstrapIsland(['form', 'table', 'layer', 'laydate'])
  const form = layui.form as LayuiModule
  const table = layui.table as LayuiModule
  const layer = layui.layer as LayuiModule
  const laydate = layui.laydate as LayuiModule
  /** `laypage` 由 `table` 的依赖带入（vendored all-in-one）；类型表里没有它，按需收窄。 */
  const laypage = (layui as unknown as { laypage: LayuiModule }).laypage

  /**
   * 本页的**平台写动作**（行内 4 颗 / 工具栏那批平台写按钮）挂
   * `order.platform_write` 节点，**没权限不画**（fail-closed）。
   * ⚠️ 必须在**首次渲染之前**定下来：行内按钮是渲染期用 `actionRowTemplate` 产出的，
   * 事后纠正会让按钮闪一下再消失。判据全在
   * `@/layui-island/perms::resolvePlatformWritePermission`。
   */
  await resolvePlatformWritePermission()

  // 页面文档标题照证据件原文（与菜单名「第三方平台订单管理」不是同一个东西）。
  document.title = '交易平台订单处理'
  root.innerHTML = await loadConfigArtifact(ARTIFACT_KEY)
  // 屏幕文案修正（文件头第 4 条）要在页签绑定之前做完，避免用户看到「未接手」闪一下。
  applyPendingTabLabel(root)
  // 页签的类名切换由 `initIslandElements` 负责；⚠️ **必须在标记注入之后调**。
  initIslandElements(layui)
  // 平台消单数据统计**直接隐藏**（不置灰、不删标记；证据件一字不改）。
  // ⚠️ **不能在这里调**：此时工具栏还只是 `<script id="toolbarDemo">` 里的模板文本，
  // `querySelector` 恒查不到 ⇒ 隐藏空转、按钮还在。真正的时机是 layui 把模板渲染进 DOM
  // 之后的 `table.render.done`（见 `renderRows`）。
  // 计数基准文案要在「待接单」修正之后记；初始计数一律「—」（还没查到数）。
  rememberTabLabels(root)
  // 右键菜单的样式进页内（按 id 去重，不改证据件）。
  ensureContextMenuStyle(root)
  // 「后端未支持」的筛选控件逐条在控件上登记（悬停可见；点搜索时再给一句中文提示，
  // ⛔ 不让人以为已经筛上了）。
  registerUnsupportedFilters(root)

  const msg = (text: string): void => {
    ;(layer.msg as (t: string, o?: unknown) => void)(text, { time: 1500 })
  }
  /** 失败提示：6000ms（与订单详情页的 `errMsg` 同值——失败信息要来得及读完）。 */
  const errMsg = (text: string): void => {
    ;(layer.msg as (t: string, o?: unknown) => void)(text, { time: 6000 })
  }
  /** 可显示的错误文本。**不自造文案**：`request()` 抛出的 `message` 就是后端 `msg` 原文。 */
  const errText = (e: unknown): string => (e instanceof Error ? e.message : '操作失败')

  /**
   * 当前勾选行的系统单号。
   * ⚠️ 从**页面真正交给 layui 的那一份 `data`** 里取：行对象是 `CoreOrderDisplayRow`，
   * 系统单号在 `t_orderid`（见 `toDisplayRow`）。空值 / 「—」不进请求（后端会整批回原因，
   * 但那是白跑一趟）。
   */
  const selectedOrderNos = (): string[] => {
    const check = (
      table as unknown as {
        checkStatus?: (id: string) => { data?: CoreOrderDisplayRow[] }
      }
    ).checkStatus
    if (typeof check !== 'function') return []
    const rows = check.call(table, TABLE_FILTER)?.data ?? []
    return rows
      .map((row) => row['t_orderid'] ?? '')
      .filter((orderNo) => orderNo !== '' && orderNo !== '—')
  }

  /** 提交中禁用那颗按钮（C3：提交中禁用；⚠️ 必须复位，否则一次失败就永久锁死）。 */
  const setButtonBusy = (button: HTMLElement | null, busy: boolean): void => {
    if (button === null) return
    if (busy) {
      button.classList.add('layui-btn-disabled')
      button.setAttribute('disabled', 'disabled')
    } else {
      button.classList.remove('layui-btn-disabled')
      button.removeAttribute('disabled')
    }
  }

  /** 在途闸：同一次批量动作只允许一发（连点不会重复扣款 / 重复退款）。 */
  let actionBusy = false
  /** 弹层闸：确认层 / 输入层最多一层（避免两层 confirm 叠着、连点开两个）。 */
  let dialogOpen = false
  const closeDialog = (): void => {
    dialogOpen = false
  }

  /**
   * 发一次批量动作并如实报结果。
   * 全部成功 ⇒ `msg` + **重新取数**；有失败 ⇒ `errMsg`（带第一笔的单号与后端原因）+
   * 也重新取数（有成功的笔已改库，页面不能停在旧值上）；
   * 请求本身失败（形状 / 权限 / 网络）⇒ `errMsg` 后端原文，**不刷新**。
   */
  const runBatchAction = async (
    button: HTMLElement | null,
    action: PlatformPushBatchAction,
    orderNos: string[],
    payload: Record<string, unknown>,
    onSuccess?: () => void,
  ): Promise<void> => {
    if (actionBusy) return
    actionBusy = true
    setButtonBusy(button, true)
    const label = TOOLBAR_ACTION_LABELS[action]
    try {
      const outcome = await runPlatformPushAction(action, orderNos, payload)
      if (outcome.fail_count === 0) {
        msg(`${label}已完成：成功 ${outcome.ok_count} 笔`)
        // `onSuccess` 在 `load()` **之前**调（写进状态的值才带得上重渲染）。
        onSuccess?.()
      } else {
        const first = outcome.items.find((item) => !item.ok)
        errMsg(
          `${label}：成功 ${outcome.ok_count} 笔，失败 ${outcome.fail_count} 笔` +
            (first === undefined
              ? ''
              : `（${first.order_no}：${first.msg}）`),
        )
      }
      await load()
      // 数据变了 ⇒ 页签数字重取一次（仍是一次请求填 14 个页签，⛔ 不点一个查一个）。
      void loadCounts()
    } catch (error) {
      errMsg(errText(error))
    } finally {
      setButtonBusy(button, false)
      actionBusy = false
    }
  }

  /** 确认层（四参形态与 `layer.confirm` 一致：第 4 位是 `btn2`，关层走 `options.cancel`）。 */
  const askConfirm = (text: string, title: string, onYes: () => void): void => {
    if (dialogOpen) return
    dialogOpen = true
    ;(
      layer.confirm as (
        content: string,
        options: unknown,
        yes: (index: number) => void,
        btn2: () => void,
      ) => void
    )(
      text,
      // ⛔ `end` 是**任何**关闭路径都会回调的那个钩子（外部 closeAll 只走 `end`、
      // 不保证走 `cancel`）⇒ 闸必须在 `end` 里放。
      { title, btn: ['确定', '取消'], cancel: closeDialog, end: closeDialog },
      (index: number) => {
        closeDialog()
        ;(layer.close as (i: number) => void)(index)
        onYes()
      },
      closeDialog,
    )
  }

  /**
   * 输入层（`layer.prompt`）。`onValue` 拿到的值已 `trim()`；
   * `initialValue` 是**预填值**（不传 = 空串，与旧行为一字不差）。
   */
  const askPrompt = (
    title: string,
    formType: number,
    maxlength: number,
    onValue: (value: string) => void,
    initialValue = '',
  ): void => {
    if (dialogOpen) return
    dialogOpen = true
    ;(layer.prompt as (o: unknown, yes: (value: string, index: number) => void) => void)(
      {
        formType,
        value: initialValue,
        maxlength,
        title,
        btn: ['确定', '取消'],
        btn2: closeDialog,
        cancel: closeDialog,
        // ⛔ 同上——外部 close / 自动关层只走 `end`，不保证走 `cancel`。
        end: closeDialog,
      },
      (value: string, index: number) => {
        closeDialog()
        ;(layer.close as (i: number) => void)(index)
        onValue((value ?? '').trim())
      },
    )
  }

  /**
   * 11 颗工具栏按钮的动作。
   * 每颗都**先有确认框**（写清后果），且批量动作**必须先勾选**（`SELECT_REQUIRED_MSG`，
   * 逐字照对标）；参数类动作是「输入 → 页面校验 → 确认 → 提交」。
   * 能不能做、为什么不能做一律由后端答（前端只挡格式，⛔ 不镜像业务判据）。
   */
  const onToolbarAction = (button: HTMLElement, action: PlatformPushBatchAction): void => {
    const orderNos = selectedOrderNos()
    if (orderNos.length === 0) {
      msg(SELECT_REQUIRED_MSG)
      return
    }
    switch (action) {
      case 'withdraw':
        askConfirm(WITHDRAW_CONFIRM_TEXT, TOOLBAR_ACTION_LABELS.withdraw, () => {
          void runBatchAction(button, action, orderNos, {})
        })
        return
      case 'accept':
        askConfirm(ACCEPT_CONFIRM_TEXT, TOOLBAR_ACTION_LABELS.accept, () => {
          void runBatchAction(button, action, orderNos, {})
        })
        return
      case 'sync':
        askConfirm(SYNC_CONFIRM_TEXT, TOOLBAR_ACTION_LABELS.sync, () => {
          void runBatchAction(button, action, orderNos, {})
        })
        return
      case 'urgent':
        askConfirm(URGENT_CONFIRM_TEXT, TOOLBAR_ACTION_LABELS.urgent, () => {
          void runBatchAction(button, action, orderNos, { urgent: true })
        })
        return
      case 'clear_tag':
        askConfirm(CLEAR_TAG_CONFIRM_TEXT, TOOLBAR_ACTION_LABELS.clear_tag, () => {
          void runBatchAction(button, action, orderNos, {})
        })
        return
      case 'arbitrate':
        askPrompt(ARBITRATION_PROMPT_TITLE, 2, 100, (value) => {
          askConfirm(
            ARBITRATION_CONFIRM_TEXT,
            TOOLBAR_ACTION_LABELS.arbitrate,
            () => {
              void runBatchAction(button, action, orderNos, { comment: value })
            },
          )
        })
        return
      case 'change_status': {
        if (dialogOpen) return
        dialogOpen = true
        ;(
          layer.confirm as (
            content: string,
            options: unknown,
            yes: (index: number) => void,
            btn2: () => void,
          ) => void
        )(
          statusDialogHtml(),
          {
            title: TOOLBAR_ACTION_LABELS.change_status,
            btn: ['确定', '取消'],
            cancel: closeDialog,
            // ⛔ 这一层必须挂 `end`：迟到的自动关层只回调 `end`（不走 `cancel` / `btn2`）
            // ⇒ 漏挂会让 `dialogOpen` 永真、本页之后**所有**按钮点了没反应（直到刷新）。
            // 与同页 `askConfirm` / `askPrompt` / 两个表单弹层同口径：任何关闭路径都放闸。
            end: closeDialog,
          },
          (index: number) => {
            // ⚠️ **先取值再关层**：`layer.close(index)` 会同步移除弹层内容，
            //    关掉之后 `document.getElementById` 拿到的是 `null`（永远提示「请选择」）。
            const select = document.getElementById(STATUS_SELECT_ID) as
              | HTMLSelectElement
              | null
            const target = select?.value ?? ''
            closeDialog()
            ;(layer.close as (i: number) => void)(index)
            if (!ORDER_STATUS_VALUES.includes(target as OrderStatus)) {
              errMsg('请选择要改成的系统状态')
              return
            }
            void runBatchAction(button, action, orderNos, { target })
          },
          closeDialog,
        )
        return
      }
      case 'refund': {
        // 幂等键：**每次打开退款弹窗生成一个**（弹窗内复用）——重复点「确定」不会记两笔。
        const requestId = newPushActionRequestId()
        askPrompt(REFUND_PROMPT_TITLE, 0, 12, (value) => {
          if (!isValidMoneyText(value)) {
            errMsg('请输入正确的退款金额（累计总额，最多两位小数）')
            return
          }
          askConfirm(
            `确认把这些订单的累计退款额记为 ${value} 元吗？后续客服提成会按这个金额冲减。`,
            TOOLBAR_ACTION_LABELS.refund,
            () => {
              void runBatchAction(button, action, orderNos, {
                refund_total: value,
                reason: REFUND_REASON,
                request_id: requestId,
              })
            },
          )
        })
        return
      }
      case 'set_label':
        askPrompt(LABEL_PROMPT_TITLE, 0, 64, (value) => {
          askConfirm(
            value === ''
              ? '确认清除这些订单的标签吗？'
              : `确认把这些订单的标签改为「${value}」吗？`,
            TOOLBAR_ACTION_LABELS.set_label,
            () => {
              void runBatchAction(button, action, orderNos, { label: value })
            },
          )
        })
        return
      case 'set_tag':
        askPrompt(TAG_PROMPT_TITLE, 0, 128, (value) => {
          if (!isValidTagText(value)) {
            errMsg('标记内容不能为空，且不能超过 128 个字符')
            return
          }
          askConfirm(
            `确认把这些订单的自定义标记改为「${value}」吗？`,
            TOOLBAR_ACTION_LABELS.set_tag,
            () => {
              void runBatchAction(button, action, orderNos, { tag: value })
            },
          )
        })
        return
      case 'change_taker_price':
        askPrompt(TAKER_PRICE_PROMPT_TITLE, 0, 12, (value) => {
          if (!isValidMoneyText(value)) {
            errMsg('请输入正确的金额（非负、最多两位小数）')
            return
          }
          askConfirm(
            `确认把这些订单的接单价改为 ${value} 元吗？`,
            TOOLBAR_ACTION_LABELS.change_taker_price,
            () => {
              void runBatchAction(button, action, orderNos, { taker_price: value })
            },
          )
        })
        return
    }
  }


  /* ── 行内 4 颗按钮接真（父行 / 扁平行 / 子行同一套）── */

  /** 读弹层里的输入值（弹层正文是**字符串** content ⇒ 走 `document.getElementById`，与改状态弹窗同一手法）。 */
  const readDialogInput = (id: string): string => {
    const el = document.getElementById(id) as HTMLInputElement | HTMLTextAreaElement | null
    return (el?.value ?? '').trim()
  }
  /** 弹层里勾选框的当前值（缺席 = 没渲染出来 ⇒ `false`，与「默认不勾」一致）。 */
  const readDialogCheckbox = (id: string): boolean =>
    (document.getElementById(id) as HTMLInputElement | null)?.checked === true

  /**
   * 把**弹层自己的提交按钮**置灰 / 复位（「提交中禁用」这条要求的两半之一）。
   * layui 在 `yes(index, layero)` 里把层的 jQuery 对象递进来，按钮是 `.layui-layer-btn0`。
   * ⚠️ 单测里 `layer.open` 是替身 ⇒ `layero` 可能缺席 / 不是 jQuery ⇒ **静默跳过**
   * （功能上的「连点只发一次」由 `submitting` 闸 + `actionBusy` 兜着，不靠这颗按钮的视觉态）。
   */
  const setLayerSubmitBusy = (layero: unknown, busy: boolean): void => {
    type LayerButton = {
      addClass?: (cls: string) => void
      removeClass?: (cls: string) => void
      attr?: (name: string, value: string) => void
      removeAttr?: (name: string) => void
    }
    const wrapper = layero as { find?: (selector: string) => LayerButton | null } | null
    const node = wrapper?.find?.('.layui-layer-btn0')
    if (node === undefined || node === null) return
    if (busy) {
      node.addClass?.('layui-btn-disabled')
      node.attr?.('disabled', 'disabled')
    } else {
      node.removeClass?.('layui-btn-disabled')
      node.removeAttr?.('disabled')
    }
  }

  /**
   * 行内写动作的统一收口。
   * **在途闸**（`actionBusy`，与批量动作共用）连点只发一发；**提交中禁用**那颗按钮、
   * `finally` 里复位（⛔ 一次失败就永久锁死）；成功 ⇒ `msg` + `onSuccess` + **刷新列表**
   * + 重取页签数字；请求失败 ⇒ `errMsg` 后端中文原文，**不刷新**（什么都没变）。
   */
  const runRowWrite = async (
    button: HTMLElement | null,
    call: () => Promise<DailiantongPushRow>,
    judge: (updated: DailiantongPushRow) => { ok: boolean; message: string },
    hooks: { onSuccess?: () => void } = {},
  ): Promise<void> => {
    if (actionBusy) return
    actionBusy = true
    setButtonBusy(button, true)
    try {
      const updated = await call()
      const verdict = judge(updated)
      if (verdict.ok) {
        msg(verdict.message)
        hooks.onSuccess?.()
      } else {
        errMsg(verdict.message)
      }
      await load()
      void loadCounts()
    } catch (error) {
      // ⛔ **分档**：`unknown`（网络 / 超时 / 5xx）⇒ 可能已经写进去了 ⇒ 必须刷新核对；
      // `failed`（后端业务码 / 4xx）⇒ 明确没成 ⇒ 原样显示后端中文、不刷新（数据没变）。
      if (classifyPushError(error) === 'unknown') {
        errMsg(`${errText(error)}（${ROW_WRITE_UNKNOWN_NOTE}）`)
        await load()
        void loadCounts()
      } else {
        errMsg(errText(error))
      }
    } finally {
      setButtonBusy(button, false)
      actionBusy = false
    }
  }

  /**
   * 编辑（`POST …/platform-push/edit/`）：**只送改动项**。
   * 表单里留空的键**不提交**（后端据此保留上游现值；⛔ 不送空串把标题 / 内容清空）；
   * 一项都没填 ⇒ 先提示、不发请求；失败**不关弹层**（操作人可以改完再提交）。
   * ⚠️ 状态闸在**按钮可见性**上（`ROW_ACTION_STATUSES`：只有未接手出现）——后端写前也只放行 11。
   */
  const openEditDialog = (
    button: HTMLElement | null,
    target: { orderNo: string; push: PlatformPushSubRow },
  ): void => {
    if (dialogOpen) return
    dialogOpen = true
    let submitting = false
    ;(layer.open as (o: unknown) => void)({
      type: 1,
      title: ROW_EDIT_DIALOG_TITLE,
      content: editPushDialogHtml(),
      area: ['560px', '430px'],
      shade: [0.3, '#333'],
      // ⚠️ 点遮罩**不关**：`shadeClose` 不会回调 `cancel`，
      // 会让 `dialogOpen` 一直为真、把整页后续弹层全部锁死。
      shadeClose: false,
      skin: 'layui-layer-lan',
      anim: 5,
      btn: ['提交修改', '取消'],
      yes: (index: number, layero?: unknown) => {
        if (submitting) return
        const payload = editPushPayloadOf({
          title: readDialogInput(ROW_EDIT_TITLE_ID),
          memo: readDialogInput(ROW_EDIT_MEMO_ID),
          requirement: readDialogInput(ROW_EDIT_REQUIREMENT_ID),
          label1: readDialogInput(ROW_EDIT_LABEL1_ID),
        })
        if (Object.keys(payload).length === 0) {
          errMsg(ROW_EDIT_EMPTY_MESSAGE)
          return
        }
        submitting = true
        setLayerSubmitBusy(layero, true)
        void runRowWrite(
          button,
          () => editOrderPlatformPush(target.orderNo, payload),
          () => ({ ok: true, message: ROW_EDIT_OK_MESSAGE }),
          {
            onSuccess: () => {
              closeDialog()
              ;(layer.close as (i: number) => void)(index)
            },
          },
        ).finally(() => {
          submitting = false
          setLayerSubmitBusy(layero, false)
        })
      },
      btn2: closeDialog,
      cancel: closeDialog,
      // ⛔ 任何关闭路径（含外部 closeAll）都要释放闸。
      end: closeDialog,
    })
  }

  /**
   * **协商撤销**（三格 + 自动客服介入勾选框，默认不勾）。
   * 载荷**恰好** 5 个键；`auto_arbitrate` 勾与不勾**都显式发**（后端需要一个明确的 false，
   * ⛔ 不能省略成「老客户端没这个字段」）。失败**不关弹层**（改完可再提交）；
   * 成功后关层 + 刷新列表 + 按返回行说结果。
   */
  const openNegotiateDialog = (
    button: HTMLElement | null,
    target: { orderNo: string; push: PlatformPushSubRow },
  ): void => {
    if (dialogOpen) return
    dialogOpen = true
    let submitting = false
    ;(layer.open as (o: unknown) => void)({
      type: 1,
      title: ROW_NEGOTIATE_TITLE,
      content: negotiateCancelDialogHtml(target.push),
      area: ['560px', '440px'],
      shade: [0.3, '#333'],
      shadeClose: false,
      skin: 'layui-layer-lan',
      anim: 5,
      btn: [ROW_NEGOTIATE_SUBMIT, '取消'],
      // 勾选框是运行期才插进层的 ⇒ 必须手动 render，否则真浏览器只看到一个裸方块。
      success: () => {
        ;(form.render as (type?: string) => void)('checkbox')
      },
      yes: (index: number, layero?: unknown) => {
        if (submitting) return
        const pay = readDialogInput(ROW_NEGOTIATE_PAY_ID)
        const ensure = readDialogInput(ROW_NEGOTIATE_ENSURE_ID)
        if (!isValidMoneyText(pay)) {
          errMsg(ROW_NEGOTIATE_PAY_INVALID)
          return
        }
        if (!isValidMoneyText(ensure)) {
          errMsg(ROW_NEGOTIATE_ENSURE_INVALID)
          return
        }
        submitting = true
        setLayerSubmitBusy(layero, true)
        void runRowWrite(
          button,
          () =>
            withdrawOrderPlatformPush(target.orderNo, {
              pay_level_bal: pay,
              rep_ensure_bal: ensure,
              comment: readDialogInput(ROW_NEGOTIATE_COMMENT_ID),
              auto_arbitrate: readDialogCheckbox(ROW_NEGOTIATE_AUTO_ID),
              platform: target.push.platform,
            }),
          rowWithdrawVerdict,
          {
            onSuccess: () => {
              closeDialog()
              ;(layer.close as (i: number) => void)(index)
            },
          },
        ).finally(() => {
          submitting = false
          setLayerSubmitBusy(layero, false)
        })
      },
      btn2: closeDialog,
      cancel: closeDialog,
      // ⛔ 任何关闭路径（含外部 closeAll）都要释放闸。
      end: closeDialog,
    })
  }

  /** 加款（`POST …/platform-push/amount/`）：整数元 ≥1，输入 → 确认 → 提交。 */
  const openAmountDialog = (
    button: HTMLElement | null,
    target: { orderNo: string; push: PlatformPushSubRow },
  ): void => {
    askPrompt(ROW_AMOUNT_PROMPT_TITLE, 0, 10, (value) => {
      if (!isValidAmountYuan(value)) {
        errMsg(ROW_AMOUNT_INVALID_MESSAGE)
        return
      }
      askConfirm(rowAmountConfirmText(value), '加款', () => {
        void runRowWrite(
          button,
          () => addOrderPlatformPushAmount(target.orderNo, value),
          (updated) => ({
            ok: true,
            message: rowAmountSuccessMessage(value, updated.price),
          }),
        )
      })
    })
  }

  /** 撤回（`POST …/platform-push/withdraw/`）：未接手 ⇒ 直接删除 + 即时退款（先确认后果）。 */
  const onRowWithdraw = (
    button: HTMLElement | null,
    target: { orderNo: string; push: PlatformPushSubRow },
  ): void => {
    askConfirm(ROW_WITHDRAW_CONFIRM_TEXT, '撤回', () => {
      void runRowWrite(
        button,
        () => withdrawOrderPlatformPush(target.orderNo, { platform: target.push.platform }),
        rowWithdrawVerdict,
      )
    })
  }

  /**
   * 行内按钮的总分派（`data-platform-action` = 动作键）。
   * 目标推送按 `data-order` + `data-push` 从**同一次取数**的目标表里回查；查不到就明确报错
   * （⛔ 不猜一个单号去写）。哪几颗按钮会出现由 `ROW_ACTION_STATUSES` 在渲染期决定
   * （后端仍会各自再判一次 ⇒ 这里不写第二套业务状态机）。
   */
  const onRowAction = (element: HTMLElement): void => {
    const key = element.dataset.platformAction ?? ''
    const orderNo = element.dataset.order ?? ''
    const pushKey = element.dataset.push ?? ''
    const target =
      orderNo === '' || pushKey === ''
        ? undefined
        : pushTargets.get(targetKeyOf(orderNo, pushKey))
    if (target === undefined) {
      errMsg(ROW_TARGET_MISSING_MESSAGE)
      return
    }
    switch (key) {
      case ROW_ACTION_EDIT:
        openEditDialog(element, target)
        return
      case ROW_ACTION_WITHDRAW:
        onRowWithdraw(element, target)
        return
      case ROW_ACTION_NEGOTIATE:
        openNegotiateDialog(element, target)
        return
      case ROW_ACTION_AMOUNT:
        openAmountDialog(element, target)
        return
      default:
        // 渲染侧只会画这 4 颗；走到这里说明标记被改过 ⇒ 明确说一句、零请求。
        errMsg(ROW_TARGET_MISSING_MESSAGE)
    }
  }

  // —— 三个 `xm-select` 下拉（订单来源 / 发单人 / 项目分类）：证据件里是空壳，按
  //    formselects 渲染出来（候选为空，筛选不发请求）。
  initFormSelects('select[xm-select]')

  // —— 日期区间（纯前端控件；本版读面不消费日期，见文件头「有意不同」）。
  initDateRange(root, laydate, msg)

  const pagerState = { curr: 1, limit: PAGE_LIMIT }
  let activeTabId: string = ALL_TAB_ID
  /** 只让最后一轮加载落地（切页签 / 连点搜索时，慢的旧响应不许覆盖新结果）。 */
  let loadSeq = 0
  /** 聚合折叠行的子单（order_no → pushes）；后端 `group_by` 未生效时恒为空。 */
  const groupedPushes = new Map<string, PlatformPushSubRow[]>()
  /**
   * **行内按钮的目标表** —— `「<order_no>\0<pushKey>」 → 那条推送 + 它属于哪一单`。
   * 每次取数重建（与 `groupedPushes` 同生命周期）：按钮的 `data-push` 是渲染期算出来的键，
   * 点击时按它回查 ⇒ 父行 = `latest`（最新一条推送），子行 / 扁平行 = `p:<i>` / `p:<id>`。
   * ⚠️ **必须带订单号做前缀**：扁平一屏里同一个订单可能出现多行（一条推送一行），
   * 只按推送键会串单。
   */
  const pushTargets = new Map<string, { orderNo: string; push: PlatformPushSubRow }>()
  const targetKeyOf = (orderNo: string, pushKey: string): string => `${orderNo}\u0000${pushKey}`
  const registerTarget = (
    orderNo: string,
    pushKey: string,
    push: PlatformPushSubRow,
  ): void => {
    pushTargets.set(targetKeyOf(orderNo, pushKey), { orderNo, push })
  }
  /** 清空两张取数期的表（每次取数开头调，与折叠状态一起复位）。 */
  const resetRowCaches = (): void => {
    groupedPushes.clear()
    pushTargets.clear()
  }
  /**
   * 渲染一屏数据。
   * `noneText` 是空态文案：正常 0 行 =「无数据」；没有数据源 =「该分类暂无数据源」；
   * 取数失败 =「加载失败：<原因>」⇒ **三种空表看得见区别**（⛔ 不许静默空表）。
   * 每次重渲染前先清掉上次展开的平台子行 ⇒ 翻页 / 刷新 / 搜索 / 切页签后**折叠状态复位**。
   */
  const renderRows = (rows: CoreOrderDisplayRow[], noneText: string): void => {
    clearOrderChildRows(root)
    ;(table.render as (o: unknown) => void)({
      ...TABLE_OPTIONS,
      data: rows,
      text: { none: noneText },
      done: () => {
        ;(form.render as (type?: string) => void)()
        // ⛔ 隐藏必须放在 `done`：这一刻 layui 才把 `#toolbarDemo` 渲染成真实的按钮节点。
        // 每次重渲染都盖一次（切页签 / 翻页 / 刷新都会重建工具栏）。
        hideUnsupportedToolbarButtons(root)
        // 平台写按钮按 `order.platform_write` **真摘掉**（没权限不画）。同一刻机。
        hidePlatformWriteToolbarButtons(root)
        // 这一刻两份 `<tr>` 才都在 DOM 里 ⇒ 顺手把固定列的行高对齐（否则展开后错位）。
        syncFixedRowHeights(root)
      },
    })
  }

  /**
   * 页签数字**一次**取齐（挂载 / 搜索条件变化 / 每次批量与行内动作成功后各一次 ⇒ 14 个页签
   * 一起填，⛔ 不「点一个查一个」）；失败 ⇒ 全部保持「—」+ 一句 6000ms 的中文原因
   * （后端 `msg` 原文，⛔ 不编）。
   */
  let countsSeq = 0
  const loadCounts = async (): Promise<void> => {
    const seq = ++countsSeq
    const keyword = root.querySelector<HTMLInputElement>('input#key1')?.value.trim() ?? ''
    try {
      const data = await fetchPlatformPushCounts({
        platform: FIXED_PLATFORM,
        ...(keyword === '' ? {} : { keyword }),
      })
      if (seq !== countsSeq) return
      applyTabCounts(root, data)
    } catch (error) {
      if (seq !== countsSeq) return
      applyTabCounts(root, null)
      errMsg(`${TAB_COUNTS_FAILED_PREFIX}${errText(error)}`)
    }
  }

  /**
   * 取一页数据：**优先**走后端按订单聚合（`group_by=order`），后端还没升级时退回扁平推送行。
   * 回包 `items[]` 每条都带 `pushes` ⇒ 聚合形态；第一发因业务码失败（后端连这个键都拒）⇒
   * 再用扁平端点取一次（`BizError` 才算这种；网络 / 权限错误照旧大声失败，
   * ⛔ 不拿第二次请求把真事故盖住）。
   */
  const fetchPage = async (
    params: PlatformPushListParams,
  ): Promise<{ items: PlatformPushListItem[]; total: number; grouped: boolean }> => {
    try {
      const page = await fetchPlatformPushGroups(params)
      const items = page.items as PlatformPushListItem[]
      const grouped = items.length > 0 && items.every(isGroupedPushItem)
      return { items, total: page.total, grouped }
    } catch (error) {
      if (!(error instanceof BizError)) throw error
      const page = await fetchPlatformPushes(params)
      return { items: page.items, total: page.total, grouped: false }
    }
  }

  /**
   * 取当前页签 + 搜索框 + 分页状态，打一次 `GET /api/business/platform-pushes/`。
   * `platform` 固定代练通；`status` 只在映射到的页签带（全部 = 不带）；`keyword` 只在非空时带；
   * `page` / `page_size` 取分页控件当前值（**聚合形态下这两个数按订单算** ⇒
   * 「同一单的两条推送被拆到两页」不会发生）。
   * ⚠️ 同一时刻只允许一个在跑（`loadInFlight`），但**在途时的新触发要排队**、不能静默丢弃
   * （丢弃会造成「新页签高亮 + 旧页签数据」，而同一页签又被「没变化」挡着不能自救）。
   * 队列只留一个「待补发」标记：补发那一刻读的是**最新**页签 / 页码，多次连点只需一次。
   * 与 `loadSeq` 分工不同——那个只丢弃**过期响应**，这个保证最新状态最终一定取一次。
   */
  let loadInFlight = false
  let loadQueued = false
  const runLoad = async (): Promise<LoadOutcome> => {
    const seq = ++loadSeq
    const tabId = activeTabId

    if (isNoDataSourceTab(tabId)) {
      // ⛔ 没有对应远端码的页签：零请求、空表 + 明确的空态文案（不拿相近码顶替）。
      resetRowCaches()
      renderRows([], NO_DATA_SOURCE_TEXT)
      renderPager(root, laypage, pagerState, 0, () => {
        void load()
      })
      return 'no-source'
    }

    const keyword =
      root.querySelector<HTMLInputElement>('input#key1')?.value.trim() ?? ''
    const params: PlatformPushListParams = {
      platform: FIXED_PLATFORM,
      page: pagerState.curr,
      page_size: pagerState.limit,
    }
    const status = statusForTab(tabId)
    if (status !== undefined) params.status = status
    if (keyword !== '') params.keyword = keyword

    try {
      const page = await fetchPage(params)
      // 过期响应（用户已经切了页签 / 又搜了一次）⇒ 丢弃，不覆盖新一屏。
      if (seq !== loadSeq) return 'loaded'
      resetRowCaches()
      if (page.grouped) {
        const groups = page.items as PlatformPushGroupItem[]
        for (const group of groups) {
          groupedPushes.set(group.order_no, group.pushes)
          // 子行按**组内下标**注册（后端不一定下发 `id`）；父行另外注册 `latest`。
          group.pushes.forEach((push, index) => {
            registerTarget(group.order_no, `p:${index}`, push)
          })
          const latest = latestActivePush(group.pushes)
          if (latest !== null) registerTarget(group.order_no, ROW_TARGET_LATEST, latest)
        }
        // ⭐ 只有 1 条平台单的订单当普通行铺（不出三角），≥ 2 条才折叠。
        renderRows(
          groups
            .map((group) =>
              group.pushes.length >= 2
                ? toGroupDisplayRow(group)
                : toSinglePushDisplayRow(group),
            ),
          EMPTY_TEXT,
        )
      } else {
        const rows = page.items as PlatformPushListRow[]
        for (const row of rows) {
          registerTarget(row.order_no, `p:${row.id}`, subRowOfListRow(row))
        }
        renderRows(rows.map(toDisplayRow), EMPTY_TEXT)
      }
      renderPager(root, laypage, pagerState, page.total, () => {
        void load()
      })
      return 'loaded'
    } catch (error) {
      if (seq !== loadSeq) return 'failed'
      const reason = error instanceof Error && error.message !== '' ? error.message : '未知错误'
      const text = `${LOAD_FAILED_PREFIX}${reason}`
      resetRowCaches()
      renderRows([], text)
      renderPager(root, laypage, pagerState, 0, () => {
        void load()
      })
      // 空态文案 + 顶部提示双保险：表格被隐藏 / 空态样式异常时也看得见失败。
      msg(text)
      return 'failed'
    }
  }

  const load = async (): Promise<LoadOutcome> => {
    if (loadInFlight) {
      loadQueued = true
      return 'busy'
    }
    loadInFlight = true
    try {
      let outcome = await runLoad()
      // 排队的一发：读最新的 `activeTabId` / `pagerState`，把在途期间用户的操作补上。
      while (loadQueued) {
        loadQueued = false
        outcome = await runLoad()
      }
      return outcome
    } finally {
      loadInFlight = false
    }
  }

  /**
   * 「查看详情」：`layer.open` 装**我们自己的**订单详情岛页（`order-orderview`）。
   * 参数逐项取 `order-orderlist.ts::openOrderDetailLayer`（`85% × 92%` / `layui-layer-lan` /
   * `anim:8` / `maxmin:false` / `shadeClose`）——两处是**两个独立的 island 文档**，那个函数是
   * 订单列表页 mount 里的局部闭包（不是导出模块），跨页 import 会把对方的加载图拖进来
   * ⇒ 这里按同一份配方复写，改值时两处一起改。
   * ⛔ **开层前的形状闸** `/^\d{20}$/`：`layer.open` 的 `title` 是 **HTML 上下文**，
   * 不匹配就只给一句**固定文案**、不开层（⛔ 不把不可信输入拼进提示）。
   */
  const openOrderDetailLayer = (orderNo: string, viewNo: string): void => {
    if (!/^\d{20}$/.test(orderNo)) {
      msg(ORDER_NO_INVALID_MESSAGE)
      return
    }
    // ⚠️ **本层不占 `dialogOpen` 闸**（它是 `type:2` 的只读详情 iframe，不与本页的写动作弹层
    // 互斥）⇒ 不需要 `end`：没有任何闸要放。
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

  /** 需求 3「查看订单截图」：开我们自己的截图管理岛页（尺寸 / `anim` 取订单列表页同一颗入口）。 */
  const openScreenshotLayer = (orderNo: string): void => {
    if (!/^\d{20}$/.test(orderNo)) {
      msg(SCREENSHOT_ORDER_NO_INVALID_MESSAGE)
      return
    }
    // ⚠️ 同 `openOrderDetailLayer`：`type:2` 的只读 iframe，**不占 `dialogOpen` 闸** ⇒ 不需要 `end`。
    ;(layer.open as (o: unknown) => void)({
      title: SCREENSHOT_LAYER_TITLE,
      shade: [0.3, '#333'],
      shadeClose: true,
      type: 2,
      skin: 'layui-layer-lan',
      anim: SCREENSHOT_LAYER_ANIM,
      area: [...SCREENSHOT_LAYER_AREA],
      content:
        `${import.meta.env.BASE_URL}layui-island/page.html?page=${SCREENSHOT_MANAGE_PAGE}` +
        `&order_no=${encodeURIComponent(orderNo)}`,
    })
  }

  /**
   * 点三角**展开 / 收起**该订单的平台子行。
   * 子单数据来自**同一发聚合回包**（`groupedPushes`）⇒ 展开是**零请求**的。
   * 主表与右侧固定列表（「操作」列 `fixed:'right'`，layui 另画一张表）各插一遍，
   * 否则固定列会错行。收起 = 删掉紧跟其后的连贯子行 + 三角换回向右。
   */
  const toggleOrderChildren = (triangle: HTMLElement): void => {
    const orderNo = triangle.dataset.order ?? ''
    const tr = triangle.closest<HTMLTableRowElement>('tr')
    if (orderNo === '' || tr === null) return
    if (triangle.dataset.open === 'Y') {
      removeOrderChildRows(tr)
      const fixedTr = findFixedRow(tr).row
      if (fixedTr !== null) removeOrderChildRows(fixedTr)
      // 收起后两边行数变了 ⇒ 重新逐行对齐。
      syncFixedRowHeights(root)
      triangle.dataset.open = 'N'
      triangle.classList.remove('layui-icon-triangle-d')
      triangle.classList.add('layui-icon-triangle-r')
      return
    }
    const pushes = groupedPushes.get(orderNo)
    if (pushes === undefined) {
      msg(GROUP_PUSHES_MISSING_MESSAGE)
      return
    }
    const fragment = document.createDocumentFragment()
    pushes.forEach((push, index) => {
      fragment.appendChild(buildOrderChildRow(tr, push, orderNo, `p:${index}`))
    })
    tr.after(fragment)
    const fixedTr = findFixedRow(tr).row
    if (fixedTr !== null) {
      const fixedFragment = document.createDocumentFragment()
      pushes.forEach((push, index) => {
        fixedFragment.appendChild(buildOrderChildRow(fixedTr, push, orderNo, `p:${index}`))
      })
      fixedTr.after(fixedFragment)
    }
    // 插完子行**立刻**把固定列逐行对齐（layui 不会自己再算一次）。
    syncFixedRowHeights(root)
    triangle.dataset.open = 'Y'
    triangle.classList.remove('layui-icon-triangle-r')
    triangle.classList.add('layui-icon-triangle-d')
  }

  /* 行内「设标签 / 移除标签」已整条删除（连同它的弹层与本会话兜底表）。
   * 那一格现在只有单号文本；工具栏那颗批量「更改标签」不受影响。 */

  /**
   * 页内交互的**统一收口**：页签 / 搜索 / 行内动作 / 工具栏 / 「重置表格排版」。
   * 用**捕获阶段**并 `stopPropagation`：layui 的 form 模块对 `*[lay-submit]` 有 document 级
   * 委托、table 模块对 `.layui-table-tool *[lay-event]` 也有委托（两者都会 `return false`），
   * 冒泡阶段的处理器会被它们抢先收口 ⇒ 必须捕获。
   * ⚠️ 页签**不拦事件**：`initIslandElements` 还要靠冒泡切 `layui-this`。
   */
  const onClick = (event: Event): void => {
    const target = event.target as HTMLElement

    const tab = target.closest<HTMLElement>(
      '.layui-tab[lay-filter="staTab"] > .layui-tab-title > li[lay-id]',
    )
    if (tab !== null && root.contains(tab)) {
      const tabId = tab.getAttribute('lay-id') ?? ALL_TAB_ID
      if (tabId !== activeTabId) {
        activeTabId = tabId
        pagerState.curr = 1
        void load()
      }
      return
    }

    // 需求 1：折叠三角（必须在下面 `.orderdetails` 之前判——它在同一格里）。
    const tree = target.closest<HTMLElement>(ORDER_TREE_SELECTOR)
    if (tree !== null && root.contains(tree)) {
      event.preventDefault()
      event.stopPropagation()
      toggleOrderChildren(tree)
      return
    }

    // 点系统单号 = 打开订单详情。
    const detailLink = target.closest<HTMLElement>(ORDER_DETAIL_LINK_SELECTOR)
    if (detailLink !== null && root.contains(detailLink)) {
      event.preventDefault()
      event.stopPropagation()
      openOrderDetailLayer(detailLink.dataset.id ?? '', detailLink.dataset.viewid ?? '')
      return
    }

    const search = target.closest<HTMLElement>('button.sechbtn')
    if (search !== null && root.contains(search)) {
      event.preventDefault()
      event.stopPropagation()
      // 需求 6：改了「后端未支持」的筛选时，如实说一句「本次没按它筛」（⛔ 不装作生效）。
      const unsupportedHits = unsupportedFilterHits(root)
      if (unsupportedHits.length > 0) msg(unsupportedFiltersMessage(unsupportedHits))
      // 搜索条件变了 ⇒ 之前各页签的计数不再可信：先复位成「—」，再用一次 counts 请求填齐。
      resetTabCounts(root)
      pagerState.curr = 1
      void load()
      void loadCounts()
      return
    }

    // 行内动作按钮（见 `onRowAction`）。
    const rowAction = target.closest<HTMLElement>('[data-platform-action]')
    if (rowAction !== null && root.contains(rowAction)) {
      event.preventDefault()
      event.stopPropagation()
      onRowAction(rowAction)
      return
    }

    const reset = target.closest<HTMLElement>('[lay-event="RESET_TABLE"]')
    if (reset !== null && root.contains(reset)) {
      event.preventDefault()
      event.stopPropagation()
      msg(`「${RESET_TABLE_LABEL}」尚未开放`)
      return
    }

    const button = target.closest<HTMLElement>('button[lay-filter]')
    if (button === null || !root.contains(button)) return
    event.preventDefault()
    event.stopPropagation()
    const filter = button.getAttribute('lay-filter') ?? ''

    // 刷新：重取当前页（属于工具栏，但不需要勾选、也不是「尚未开放」）。
    if (filter === REFRESH_FILTER) {
      void load()
      void loadCounts()
      return
    }

    // 11 颗接真的按钮（先判「有没有勾选」，再按动作走确认 / 输入层）。
    const action = TOOLBAR_ACTION_FILTERS[filter]
    if (action !== undefined) {
      onToolbarAction(button, action)
      return
    }

    // 其余（发送短信 / 导出订单 / 被隐藏的平台消单数据统计）：照旧只提示、零请求。
    const label = clickLabel(button)
    msg(`「${label === '' ? '该功能' : label}」尚未开放`)
  }
  root.addEventListener('click', onClick, true)

  /** 窗口一改宽高，主表格子的换行就变（行高跟着变）⇒ 重新对齐固定列。 */
  const onWindowResize = (): void => syncFixedRowHeights(root)
  window.addEventListener('resize', onWindowResize)

  /**
   * 验收倒计时**只在页面本地**跑：每秒就地改那几个 `<span>` 的文字 / 颜色
   * —— ⛔ **不重渲染表格**（重渲染会收掉用户展开的子行）、⛔ **不发任何请求**。
   */
  const verifyTicker = window.setInterval(() => {
    tickVerifyCountdowns(root, Date.now())
  }, VERIFY_TICK_MS)

  /* ── 需求 3：右键菜单 4 项 ─────── */

  /** 当前挂着的菜单节点与它承载的那一行（点菜单项时读它取单号 / 显示值）。 */
  let contextMenu: HTMLElement | null = null
  let contextMenuTarget: { orderNo: string; shownNo: string } | null = null

  const closeContextMenu = (): void => {
    contextMenu?.remove()
    contextMenu = null
    contextMenuTarget = null
  }

  /** 复制一段文本并按结果给一句话（两条路都在 `copyTextToClipboard` 里；⛔ 不静默）。 */
  const copyWithMessage = (text: string, okText: string, failText: string): void => {
    if (text === '') {
      msg(failText)
      return
    }
    void copyTextToClipboard(text).then(
      (ok) => msg(ok ? okText : failText),
      () => msg(failText),
    )
  }

  /** 「复制原始订单内容」：先取订单详情，复制 `content`（为空则 `demand`，与订单列表页同口径）。 */
  const copyRawOrderContent = (orderNo: string): void => {
    void fetchOrder(orderNo).then(
      (order) =>
        copyWithMessage(
          order.content !== '' ? order.content : order.demand,
          COPY_ORDER_CONTENT_SUCCESS_MESSAGE,
          COPY_ORDER_CONTENT_FAILED_MESSAGE,
        ),
      (error: unknown) => errMsg(errText(error)),
    )
  }

  const runContextMenuCommand = (key: string): void => {
    const target = contextMenuTarget
    if (target === null) return
    closeContextMenu()
    if (key === 'viewDetails') {
      // 与点单号蓝链**同一个出口**。
      openOrderDetailLayer(target.orderNo, target.shownNo)
      return
    }
    if (key === 'copyOrderID') {
      // 复制**看到的那个号**（来源单号；没有则回退内部单号）。
      copyWithMessage(
        target.shownNo,
        `${COPY_ORDER_ID_SUCCESS_PREFIX}${target.shownNo}${COPY_ORDER_ID_SUCCESS_SUFFIX}`,
        COPY_ORDER_ID_FAILED_MESSAGE,
      )
      return
    }
    if (key === 'copyOrder') {
      copyRawOrderContent(target.orderNo)
      return
    }
    if (key === 'viewPic') openScreenshotLayer(target.orderNo)
  }

  /** 造一份菜单 DOM（每次打开重建，与对标插件的 `create` 同形）。 */
  const buildContextMenu = (): HTMLElement => {
    const list = document.createElement('ul')
    list.className = `${CONTEXT_MENU_ROOT_CLASS} context-menu-root`
    for (const item of CONTEXT_MENU_ITEMS) {
      if (list.childElementCount > 0) {
        const separator = document.createElement('li')
        separator.className = CONTEXT_MENU_SEPARATOR_CLASS
        list.appendChild(separator)
      }
      const node = document.createElement('li')
      node.className = CONTEXT_MENU_ITEM_CLASS
      node.setAttribute('data-key', item.key)
      const icon = document.createElement('i')
      icon.className = `layui-icon ${item.icon}`
      const text = document.createElement('span')
      text.textContent = item.label
      node.append(icon, text)
      // 悬停反白：`.context-menu-item.context-menu-hover` 那条规则要生效就得有人加这个类
      //（`:hover` 伪类不在样式表里，靠 mouseenter/leave 加）。
      node.addEventListener('mouseenter', () => node.classList.add(CONTEXT_MENU_HOVER_CLASS))
      node.addEventListener('mouseleave', () => node.classList.remove(CONTEXT_MENU_HOVER_CLASS))
      node.addEventListener('click', () => runContextMenuCommand(item.key))
      list.appendChild(node)
    }
    return list
  }

  const showContextMenu = (
    pageX: number,
    pageY: number,
    orderNo: string,
    shownNo: string,
  ): void => {
    closeContextMenu()
    const list = buildContextMenu()
    // 坐标用 `pageX/pageY`：`.context-menu-list` 是 `position:absolute`、挂在 body 下。
    list.style.left = `${pageX}px`
    list.style.top = `${pageY}px`
    document.body.appendChild(list)
    contextMenu = list
    contextMenuTarget = { orderNo, shownNo }
  }

  /**
   * 「系统单号」格上右键 → 出 4 项菜单。
   * ⚠️ **必须委托在 `root` 上**：`.orderLine` 是列 templet 运行期渲染的，挂载那一刻 DOM 里
   * 没有它 ⇒ 直接绑静态节点绑不上。只拦**这一格**的浏览器菜单（别处右键保持原样）。
   */
  const onContextMenu = (event: MouseEvent): void => {
    const target = event.target
    if (!(target instanceof Element)) return
    const line = target.closest<HTMLElement>(ORDER_LINE_SELECTOR)
    if (line === null || !root.contains(line)) return
    const orderNo = (line.dataset.id ?? '').trim()
    if (orderNo === '') return
    event.preventDefault()
    const shownNo = (line.dataset.orderidview ?? '').trim() || orderNo
    showContextMenu(event.pageX, event.pageY, orderNo, shownNo)
  }
  root.addEventListener('contextmenu', onContextMenu)

  /** 关菜单的三个触发（与订单列表页同一套）：点任意处 / `Esc` / 滚动。 */
  const onMenuCloseClick = (): void => closeContextMenu()
  const onMenuCloseKeydown = (event: KeyboardEvent): void => {
    if (event.key === 'Escape' || event.key === 'Esc') closeContextMenu()
  }
  const onMenuCloseScroll = (): void => closeContextMenu()
  document.addEventListener('click', onMenuCloseClick)
  document.addEventListener('keydown', onMenuCloseKeydown)
  // `scroll` **不冒泡**：要收到页内任意容器的滚动，只能带 `capture`。
  document.addEventListener('scroll', onMenuCloseScroll, true)

  // 首屏：全部 + 第 1 页 + 每页 30；页签数字用 counts 一次填齐。
  // 失败由 `load` / `loadCounts` 自己出可见提示，⛔ 不在这里 reject（那会让整个岛页白屏）。
  await Promise.all([load(), loadCounts()])
  ;(form.render as (type?: string) => void)()

  return () => {
    root.removeEventListener('click', onClick, true)
    window.removeEventListener('resize', onWindowResize)
    // ⛔ 定时器必须在卸载时清掉（E-0198：假定时器 / 泄漏的老坑）。
    window.clearInterval(verifyTicker)
    root.removeEventListener('contextmenu', onContextMenu)
    document.removeEventListener('click', onMenuCloseClick)
    document.removeEventListener('keydown', onMenuCloseKeydown)
    document.removeEventListener('scroll', onMenuCloseScroll, true)
    closeContextMenu()
    // 只清我们注入的标记：`layer` 的弹层挂在 `document.body`，由 layui 自己收。
    root.innerHTML = ''
  }
}

export default {
  mount: mountCoreOrderCenterPage,
} satisfies IslandPage
