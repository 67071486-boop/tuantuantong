/**
 * 【绩效管理】页：按鸡蛋铺那一页复刻，并接上真数据。
 * 筛选栏、工具栏、20 列表格。
 *
 * ⚠️ 不要在注释里复述已经废掉的日期口径原文：页测试会把那个主词当语料搜，写回来会红。
 *
 * ## ⚠️ 已知会不同 / 未接（逐条给理由；表在文件头，不在代码注释里替代）
 *
 * | # | 项 | 处置与理由 |
 * |---|---|---|
 * | 0 | ⭐ **工具栏按业务方要求裁掉 4 颗**（刷新淘宝状态 / 批量改备注 / 修改订单状态 / 发送短信） | **有意裁剪**，不是漏做、更不是「尚未开放」。`parity.mjs` 的工具栏探针按**下标**比文案 ⇒ 跑 `parity` 必然报 bench 10 颗 vs ours 6 颗（可见的），那是**预期差异**。`changeStaBtn` 那条真接线（`/review/`）与 D-0071 冲突，已整条摘掉 |
 * | 1 | 对标点 `.setSett` 弹的是**子页** `/Emp/SettlementEmp?ids=<empId>`（「清算绩效」，`type:2` iframe 层） | `.setSett` 照对标开清算绩效子页（`layer.open({type:2})`，实参照 `settSta()`）。 |
 * | 2 | ~~`settingTime`（结账时间）/ `empPayId`（结账方式，隐藏列）**两列没有数据源**~~ **⇒ ⭐ 2026-09-22 已接上**（后端 T1 给绩效列表加了 `settled_at`/`settled_by`；`settlePerformances` 的结算端点不受影响） | `settingTime` 取 `settled_at`（未结账 ⇒ `—`）；`empPayId` 显示「**人工结账**」（判据 = `settled_at is not None`）。（`AGENTS.md` §4「不删历史记录」）：它描述的是 T1 之前的状态 |
 * | 3 | 结账的**粒度**：对标与我们都**按行**（一次只涉及被点的那一条绩效） | 对标 `data-id` 装的是 `res.empId`（= **绩效行主键**，见 `settleTagHtml` 的注释），`settSta()`（`emplist.js` L1212）把**这一行**交给子页；我们同样只发被点的那一行。**形态差异只有一处**：我们的接口 `/settlements/settle` 收的是 `performance_ids` 批量 ⇒ 一次只发**长度 1** 的列表 |
 * | 4 | ⭐ ~~**「没有归属人」的绩效行**（`user` 不是数字，`null` = 还没接单人）：点它的标签**前端直接提示、不发请求**~~ **⇒ ⭐ 第四轮 A：这种行现在**根本不会出现在本页**（后端 `performance_queryset` 排除 `user IS NULL`）**；在左（描述的是本轮之前的状态） | ⚠️ **真实理由（2026-09-22 A 线 D-0071/D-0074 之后）**：后端结账的判据是**订单已完成（`order.status=COMPLETED`）+ 有归属人（`user__isnull=False`）+ 未结账**——**不再是**「审核已通过」（结账**不再要求** `review_status`，D-0071 ③）。这类行必然过不了前两条（`Performance` 的 DB 约束 `performance_review_needs_user` 让它连审核状态都没有；没有接单人时订单也到不了完成）。<br>⭐ **第四轮 A 的落点**：业务方现场口径「只有订单关联的打手产生了绩效才可以生成到这里」⇒ 后端在**列表与导出共用的唯一筛选出口**排除 `user IS NULL`（`summary` 吃同一份 queryset ⇒ 三处自动一致）。⚠️ **行仍在库里**（派单 / 抢单 / `/adjust` 要用，P3a D-0010 的「录单即建绩效」不变）——只是不进本页。前端这一道「点了提示、不发请求」的拦截**保留**为**体验层兜底**（页面若因任何原因拿到这种行，仍不会发一次必然失败的请求）；⛔ **后端也拦**（见第 32 条）。 |
 * | 5 | 前端**不筛** `review_status`（**只**对「没有归属人」这一种行提前挡，见第 4 条） | 结账（`settle_performances`）的判据是**订单已完成 + 有归属人 + 未结账**，**与审核状态无关**（D-0071 ③ 去掉了 `review_status=已通过` 那道过滤）。⇒ 前端**没有任何基于 `review_status` 的拦截**：有归属人、订单已完成、未结账的行一律照发，成败由后端一处判。有 spec 用例钉住「有归属人 + `review_status=待审核` ⇒ 仍然发 `[id]`」 |
 * | 6 | **已结账的行也开子页** | `onSettleClick` 无条件开子页。`user` 不是数字（没有接单人）仍只提示、不开层。 |
 * | 7 | ⭐ **日期框填了不可识别的值时，我们**主动多弹一句**提示（对标没有这个行为）** | 症状：`#sDate`/`#eDate` 是非空的垃圾串时，`dateTimePart` 取不出日期 ⇒ `toPerformanceSearch` **把那个参数整个不发**，而请求照发 ⇒ 用户看到一批**比屏幕上写的条件更宽**的数据且**毫无迹象**（E-0021「静默丢掉筛选条件」那一族）。**对标也不拦**：证据件那两个框写的是 `lay-verify="datetime"`，而 layui 内建校验表里**没有 `datetime`**（只有 `required`/`phone`/`email`/`url`/`number`/`date`/`identity`）⇒ 那条属性等于没写。<br>**我们的处置**：`params()` 里加一句 `msg('「录单时间」不是可识别的日期，本次已忽略该筛选')`（搜索与刷新都会弹）。<br>**有意偏离的代价（如实）**：这是**对标没有的可见行为**（多一次 toast）——按 D-0025 的优先序，本来应「照对标 1:1」，这里选「宁可多一句提示，也不要静默放宽结果集」，因为本页是给财务对账的。**依据（真浏览器实测）**：<br>日期框填「不是日期」点「搜索」：**`date_from` 消失、请求照发、零提示**；根因（**读码**）：vendored `public/layui/layui.js` 的 `verify:{…}` 键 = `required,phone,email,url,number,date,identity`（**无 `datetime`**），`validate` 里是 `var a=d[t]; if(a){…}` ⇒ 未注册的规则直接跳过。<br>⚠️ 与（未修的）对标行为的差异**只在这一句提示**：请求参数一个都没多、也没少 |
 * | 8 | ⭐ **`exportBtn` 不照抄「导出后关掉当前页」，且改用「先请求再落 Blob」** | 对标 `exportEmp()`（`emplist.js` L995-999）是 `window.open('/Emp/ExportEmp?'+seachParam)` **紧接着 `layer.getFrameIndex(window.name)` + `layer.close(index)`**——它要把自己那个页签关掉（对标跑在管理壳的 iframe 里）。我们跑在 SPA 的 island iframe 里，照抄会把页面关掉/留下怪状态 ⇒ **不关当前页**。<br>**为什么不是 `window.open`**：我们的导出端点**同一个 URL 两种形状**（成功是 `.xlsx` 附件，两个工作表；失败仍是 HTTP 200 信封）。`window.open` 拿不到响应体，超限或参数非法时只会把信封摊开。走 `downloadPerformanceExport()`：信封经 `unwrap` 抛 `BizError`。文件先进内存；这条路径绕过 `request()` 的 401 处理。 |
 * | 9 | ~~「搜索2」（项目/旺旺）只按「项目」筛~~ **⇒ ⭐ 2026-09-22 那个控件已删**（业务方裁定裁掉「搜索2」） | 后端 `project` / `customer_ww` **仍然支持**，只是本页不再有发它们的入口；`toPerformanceSearch` **不再产生 `project`**，那句「只按「项目」筛选」的提示也随之删除。（不删历史记录）：它描述的是控件删除之前的状态 |
 * | 10 | ⭐ **`#popgameName` 换成 formselects 真控件**（对标是 xm-select 的人员树） | 见文件头「`#popgameName`」一节：库不在 vendor 里 ⇒ 用同页既有的 formselects 渲染多选，值送 `username`（逗号串）。折叠态外观与候选组织方式与对标不同（对标按游戏分组、走 xm-select）<br>⭐ **2026-10-04（「显示人统一成昵称」线，spec §2.3）——option 文本 = 显示名**：`display_name \|\| username`（昵称优先、空串回落登录账号）；**`option.value` 仍是 `username`**（后端 `PerformanceListSearchSerializer.username` 是逗号串、筛 `user__username__in`，⛔ 改成 id 会筛出恒空）。同一段循环里的 `#inputOrderKefu`（那一格在本页**同时**被填）跟着一起变<br>⭐ **2026-11-09（「打手/下家下拉全改远程关键词搜索」，业务方拍板「全改，v2 也带上」）——打手候选改远程搜**：不再预载 200 个账号本地搜（最大在役租户 729 人底集截断 200，第 201 位起搜不到；租户名不进 src，判据见 docs/plans/2026-11-09-orderlist-taker-search.md），改为在下拉自带搜索框输入关键词时经 `listAccounts({ key: <词>, page_size: 200 })` 远程取（后端 `key` 筛 username/昵称/QQ/手机号 icase 包含，零后端改动）；换候选时已选值保留（vendored formselects 远程分支不清 `fs.values`/隐藏 input）。**对标偏离（复刻登记）**：对标 = 全量 option + 本地搜索；我方 = 关键词远程搜索。「其它分组」两颗哨兵在每次远程回包后仍置顶（`playerSentinelRows`） |
 * | 11 | ⭐ **`#gameName`（选择游戏）的 option 换成我们自己的游戏** | 证据件里那 14 个 option 是**对标租户的游戏、不是产品逻辑**。⇒ 运行时按 `fetchGames()` 重渲染（`<option value="">全部游戏</option>` + 我们的游戏），值送**我们的** `game`（`min_value=1`）。「全部」= **不送该参数**（⛔ 不照抄对标那个 `21714` 的「全部」值——它会回 `1000`）。`parity.mjs` 只比 option **个数**、且两侧都 >0 时归 `data`（不算不达标） |
 * | 12 | ⭐ **`#orderSta`（订单状态）的 option 值换成我们自己的 8 个状态** | 对标的 `待上号`/`异常中` 在我方状态机里没有对应值，我方的 `未分配`/`待抢单` 对标没有。**option 个数仍是 9**（全部 + 8）⇒ `parity.mjs` 的筛选控件探针（只比 name/宽/x/个数）不会报差异。提交时把中文 label **反查**成枚举名送 `order_status`（复用 `@/config/orderStatus` 的 `ORDER_STATUS_LABELS`，不另建映射表） |
 * | 13 | ⭐ **「订单状态」列那颗标签（`.lableb setdlsta`）点了提示「尚未开放」** | 对标点它会开「更改订单状态」弹层（`changeOrderSta` → `POST /OrderAjax/SetOrderSta`）。那颗**按钮**已按业务方裁定删掉，而这一列我们只负责**显示**订单状态（D-0071 ③）⇒ 照对标渲染那颗标签（彩色 + `title`），点击收口成一句 `msg('「修改订单状态」尚未开放')`，⛔ 不发请求、不改行状态 |
 * | 14 | ⭐ **`refresh` 不读「鸡蛋铺余额」** | 对标 `reloadOrderList()` 顺带打 `GetJdpBalance`（`emplist.js` 的 `getJdpBalance`）把余额写进 `#jdpBalance2`。我方**没有这个数据**（那是鸡蛋铺自己的钱包）⇒ 只重取列表，余额那格照证据件是隐藏的（`payBalanceBtn` 带 `layui-hide`） |
 * | 15 | **`settBtn` 与 `.setSett` 都开清算绩效子页** | `settBtn` 与 `.setSett` 共用 `openSettlementDialog`。列上 `user` 不是数字时仍只提示、不开层。 |
 * | 16 | ⭐ **删 6 列**（`taskId` 排团号 / `imgCount` 传图数 / `taobaoPrice` 淘宝价 / `taobaoSta` 淘宝状态 / `applicationSett` 申请清算 / `empRemark` 备注说明） | 业务方 2026-09-22 点名删 5 列（排团号 / 传图数 / 淘宝状态 / 申请清算 / 备注说明），`taobaoPrice`（隐藏列）是**编排方多删的一列**（它同样没有数据源，留着会出现一列永远空）。判据：`Performance` / `Order` 两个模型里都没有对应字段。⛔ 不给它们编假数据 |
 * | 17 | ⭐ **取消 3 列隐藏**（`deductFee` / `netReceipts` / `empPayId`）⇒ 22 列全部可见 | 业务方 2026-09-22：「不止 24 个列吧，还有隐藏的，**默认全部展现出来**」。对标的 `hide` 是对标自己的 `deductFeeType` 决定的，不是产品必需。⚠️ **2026-09-22 第二条指令再删 2 列 ⇒ 现为 20 列**（见第 26 条） |
 * | 18 | ⭐ **删 4 个筛选控件**（`taobaoSta` 淘宝状态 / `isCert` 是否实名 / `key2` 搜索2 / `key3` 搜索3）——原句是「5 个」且含 `selectSeach1`，**第四轮 C 更正：`selectSeach1` 是误删、已恢复**（见第 30 条）； | 业务方 2026-09-22 逐条点名（`selectSeach1` 那一项是被**误当成「审核状态」连带删掉**的）。**证据件里连标签整条删除**（不是 `layui-hide`——留一个点了没用的控件比删掉更糟），并在原处留注释说明是**有意裁剪**。连带：不再发 `project`、`REVIEW_STATUS_MAP` 整表删除（`review_status` 的筛选入口确实不再有）。⚠️ 代价：按项目筛的入口随之消失（后端 `project` 参数仍在） |
 * | 19 | **\`payBtn\`「结算并直接打款」** | 按账单类别单独预览、确认制单。 |
 * | 20 | ⭐ **日期默认值改为运行期计算**（今天 − 3 个自然月 `00:00:00` ~ 今天 `23:59:59`） | 证据件里那两个 `value` 是**对标服务端按取数当天**渲染的死值（`2026-06-20` / `2026-09-20`）。照抄会让默认视图**永远停在过去**，而它们是**真的发给后端**的筛选 ⇒ **2026-09-20 之后录的单默认看不见**。实现与 `order-orderlist` / `emp-billsummary` **同一份**（`@/layui-island/dateRange`） |
 * | 21 | **\`empPayId\`「结账方式」** | 人工结账、在线结算和进行中的付款按结账记录显示。 |
 * | 22 | ⭐ ~~**`price`（订单金额）列不画收款状态色块**~~ **⇒ ⭐ 2026-09-22 业务方裁定：补上色块**（见右） | **原理由（描述的是本轮之前的状态）**：对标那一格是「`.lableb` 色块（未/中/收）+ 红字金额」，色块读 `res.furlStatus`——**我们没有「客户收款状态」这个字段**（`settle_status` 是**结算**状态，不是收款状态）⇒ 当时只照对标给金额红字，⛔ 不拿别的字段冒充（那会让财务读出一个不存在的口径）。<br>**⭐ 裁定后的实现（2026-09-22 第二条指令）**：颜色与单字**照抄对标**（`.lableb`；`#E6E6FA`+「未」/ `#B0E0E6`+「中」/ `#98FB98`+「收」）、顺序也照对标（**色块在前、红字金额在后**）。<br>⚠️ **判据与 `title` 是有意偏离**：对标那个色块的语义是「工作室与**上游老板/上家**的结算」（它 `title` 原文写着「注意：不是给打手的钱」）——**我们不用那个语义**，也**不许**拿我们的「结账状态」（给打手结账）去顶。业务方拍板口径：「按『**接单实收款**』：有钱=收(绿)、没有=未(浅紫)」⇒ 我们读 `net_receipts`（`order.received_price`）：**有值且 ≠ 0 ⇒ `#98FB98`+「收」**；`null` / `0` ⇒ `#E6E6FA`+「未」。`title` 照我们的口径自己拟（一句话说清判据 + 怎么改），⛔ 一个字都不抄对标那句「与老板或上家结算…（注意：不是给打手的钱）」。<br>⚠️ **对标三态、我们两态**：对标的「中」（`furlStatus=='结算中'`，`#B0E0E6`）在我们这里**没有对应状态** ⇒ 代码里**不造**这个分支（造了就是恒假分支） |
 * | 23 | **`orderId`（关联订单）列可点** | 点开 `order-orderview`。参数见第 34 条。 |
 * | 24 | ⭐ **`deduct_fee` 为 `null` 时显示 `—`**（不是 `0.00`） | 后端契约（`DeductFeeField`）：扣点越界那一行**刻意**回 `null`——「照常渲染、**不给 `0.00` 之类假数**、也不把该行藏掉」（假数会说假话、藏行会让财务看不见坏行）。`net_receipts` / `settled_at` 同理 |
 * | 25 | ⭐ **`refundFee` 的 `lay-tips` 只写金额** | 对标的 `title` 还拼了**退款方式 / 退款时间 / 操作人**（那些在 `OrderRefund` 表，不在绩效列表口径内）⇒ 本轮只写「退款金额：X元」（`>0` 时）/「没有退款」。同理不挂 `data-refundmodel`（挂了也是空值） |
 * | 26 | ⭐ ~~**仍然接不了的 3 列**：`completeTime`（完成时间）/ `exceedHours`（是否超时）/ 以及对标里 `price` 列的**收款状态色块**（见第 22 条）~~ **⇒ ⭐ 2026-09-22 业务方裁定后，这三条都不再是「接不了 / 留空」**（见右） | **原理由（描述的是本轮之前的状态）**：`Order` **没有**完成时间列（从 `OperationLog` 推要定义「完成的唯一动作」并承担每行子查询成本 ⇒ 留给下一轮）；`exceedHours` 除完成时间外还需要**结构化时效**，而 `Order.time_requirement` 是**自由文本** ⇒ 接不了。⛔ **一律留空 + 如实报告，不造假数据**。<br>**⭐ 裁定后的落点（2026-09-22 第二条指令）**：① `completeTime`（完成时间）/ `exceedHours`（是否超时）——业务方裁定「**这两列不做也不要了 ⇒ 已删列**」（**有意裁剪**，不是「尚未开放」、也不在「接不了」清单里留位置）；② `price` 列的**收款状态色块**——业务方拍板口径「按『接单实收款』：有钱=收(绿)、没有=未(浅紫)」⇒ **已补上**（判据与 `title` 是有意偏离，详见第 22 条） |
 * | 27 | ⭐ **`#total` 的「记录退款金额」槽数位形状随后端返回的字符串**（对标是整数形、我们是两位小数） | **算法与行为一致**：对标 `emplist.js:499` 是 `'记录退款金额' + res.total.TotalReFundAmount + '，'` —— **原样透传、没有 `toFixed`**；我们的 `summary.refund_fee` 是两位小数字符串（`"5.00"`）⇒ 产物是 `记录退款金额5.00，`，而对标在它自己的数据下是 `记录退款金额5，`。⚠️ 这是**数据形状**差异、不是算法差异：同一份 `.tools/parity.mjs` 的 `tmpl()` 判定下，把这一项换成整数形（`"5"`）时两侧**逐字同形**。⛔ **不给这一槽做 `toFixed(0)`/取整**（那会改掉钱的显示口径，比形状差异更糟）。有 spec 用例按真 `tmpl` 钉住「除这一槽外 5 槽同形」 |
 * | 28 | ⭐ **`userName`（系统用户）列的「删」徽标接不了** | 对标 `emplist.js:234-240` 在 `orderIsDel == "Y"` 时渲染一颗红底「删」（`#FF534D`，`title="该订单已被删除了"`）。我方**没有 `orderIsDel` 这个数据**（`Order` / `Performance` 都没有「订单已删除」这个语义）⇒ ⛔ **不编一个恒假的徽标**，该列保持纯文本。⚠️ `orderId` 列（`emplist.js:314-322`）那颗同源的「删」徽标同理不渲染 |
 * | 29 | **`orderId` 列是蓝色可点链**（`#0094ff`） | 可点链带 `class="orderdetails"`、`href`、`data-id`（系统单号）、`title="点击查看订单详情"`。单号仍 `escapeHtml`。属性顺序与对标相反，DOM 等价。 |
 * | 30 | ⭐ **恢复对标的「代练/下家 ⇒ 批量搜用户」**（`selectSeach1`）——上一轮**误删**，本轮是**纠错** | 上一轮按「删掉审核状态」把**整条 `layui-inline`** 删了，而那条里装的其实是对标的 `selectSeach1`（曾被我们复用成「审核状态」）⇒ 删掉的**控件**与要删的**语义**不是一回事。恢复依据：证据件 `.tools/recon/suqing1/html/Emp__emplist.html` L246-252（外层 `div.layui-input-inline` 宽 110px、**无 label**、两个 option「代练/下家」(0) /「批量搜用户」(10)）；行为照对标 `assets/JS2019/Emp/emplist.js` L525-560 **以及它调的 `reloadOrderList()`（L905-958）**。⚠️ **一处对标自带的「文案与行为不一致」**：`layer.prompt` 的**标题**承诺「该搜索会排除其它搜索条件」，但**对标自己的 JS 仍发全部筛选参数**（`seachParam` 里 `gameName`/`stime`/`etime`/`dateType`/`empType`/`settingSta`/`orderSta`/`taobaoSta`/`recUser`/`kefu`/… 一个不少）**外加 `recUserName`** ⇒ 我们**照 JS 的行为**（照常发全部筛选 + `username`）+ **照标题的文案**（逐字照抄）；「排除其它条件」只是对标标题里的话、「照做」二字只对**文案**成立。空内容提示「搜索项不能为空」且不发请求、选回 0 清空该条件。⛔ **`REVIEW_STATUS_MAP` 不复活**（「审核状态」语义已按 D-0071 删除）。**三处有意偏离（如实登记）**：① 对标在 `select(0)` 时只清变量、不重取；我们选回 0 就地重取（多一发请求，语义等价、恢复可见）；② 昵称串对标是 `value.replace(/\r?\n/g, ',')`（**不 trim、保留空行**：`a\n\nb` → `a,,b`），我们 `split/trim/filter` 后 join（`a\n\nb` → `a,b`）；③ 空值判据对标只有 `null/undefined/''`，我们把**纯空白**也当空。②③ 都是**更严/更干净**的方向，不是照抄 |
 * | 31 | ⭐ **`#popgameName` 空选项文案 =「请选择」**（原为「选择或输入」） | **依据（对标渲染值）**：对标那一格是 xm-select 的 `div.xm-select-demo`，而 **xm-select 的默认占位就是「请选择」**（`recon/emp-pages/sections/emplist.md` §3 的 `inputOrderKefu` 那格写的是 `选择或输入`——那是**另一个**控件；同表的 `popgameName` 一格是空容器，占位由库渲染）。我们用的是 formselects-v4（**不是** xm-select），它拿 `<select>` 的空值 option 文案当占位 ⇒ 按对标的**渲染值**把这一格对齐成「请选择」。⚠️ `#inputOrderKefu` 的「选择或输入」**保持不动**（那是对标 HTML 里的原文） |
 * | 32 | ⭐ **没有归属人的绩效行不进本页**（后端排除 `user IS NULL`） | 业务方现场：「只有订单关联的打手产生了绩效才可以生成到这里」。落点是**列表与导出共用的唯一筛选出口** `performance_queryset()`（`summary` 吃同一份 queryset）⇒ 三处自动一致。⚠️ **行仍在库里**（派单 / 抢单 / `/adjust` 要用，P3a D-0010「录单即建绩效」不变）——**不是**脏数据、也不是把建行逻辑去掉；⛔ 后来者不要因为「库里查得到、列表查不到」就去清数据。前端无需改动（见第 4 条） |
 * | 33 | ⭐ **`layui/island.ts` 的页签点击不再被吞**（共享改动。带活页签的 replica 页：9 个文件、13 个块、8 个带面板。唯一没有面板的是 `customer-hylevelsetting.html`。） | 根因/修法/读码依据全写在那个函数的注释里。对**本页**的可见后果：「销售客服提成账单」现在**真的能切**（点它会发 `emp_type=销售提成` 的取数请求、标题 `layui-this` 也跟着切）。⚠️ **同一次改动带出一条回归（I-1）**：`artifactPage` 的 catch-all 会向上命中**页签容器**的 `lay-filter` ⇒ 4 个 artifactPage 页点页签弹「尚未开放」；已在 catch-all 里显式排除 `.layui-tab-title`（本轮修，见 `artifactPage.ts` 注释与计划「第四轮」§3）。⛔ 这是**共享文件**——改动面与所跑 spec 见 `docs/plans/2026-09-22-emp-emplist-completion.md` 的「第四轮」一节 |
 * | 34 | ⭐ **「关联订单」列改回可点：点它**原地开层**装我们自己的订单详情复刻页**（T4，2026-09-22；业务方原话「关联订单可以点开查看订单详情」） | **对标出处是本页自己的** `assets/JS2019/Emp/emplist.js` **L968-983**：`.orderdetails` 委托 → `viewOrderDetails(orderid)` → `layer.open({title: orderid+"订单详情", shade:[0.3,'#333'], shadeClose:true, type:2, skin:'layui-layer-lan', anim:8, area:['90%','92%'], content:'/Order/OrderView?viewType=20&orderId='+orderid})`。我方逐项照抄**本页的**参数（⛔ **不抽共用模板**：同一个产品里订单列表 `85%×92%`、我的订单 PC `85%×92%`、我的绩效 `60%×92%`、绩效账单 `1125px\|80%×90%`，各页不同）；`content` 换成我们自己的复刻页 `<BASE_URL>layui-island/page.html?page=order-orderview&order_no=<单号>`（用 `import.meta.env.BASE_URL` 拼）。`title` = `<单号>订单详情`。**零 API 请求**（纯开层，数据由层里的 island 页自己取）。<br>⛔ **一处如实登记的偏离**：对标从**顶层窗口**开层（盖住顶栏整屏）；我们开在 **island iframe 内**——**只盖内容区**。原因：绩效页本身跑在 island iframe 里，而 Vue 外壳**没有 layui**（`layer.open` 只能在当前文档里建层）。⚠️ 不影响「能看到订单详情」这件事，但**观感与对标不同**（层小一圈、不盖顶栏）。<br>⛔ **开层前的「单号形状闸」（T4 评审 Minor-2）**：`data-id` 不匹配 `/^\d{20}$/`（与路由 `(\d{20})` 同源）⇒ **不开层**、只给固定文案提示。理由：`layer.open` 的 `title` 是 **HTML 上下文**，而 `dataset.id` 会把 attribute 实体解码回 `<`/`>` ⇒ templet 的 `escapeHtml` 在这条链上不构成保护（评审真浏览器探针实测 `<img onerror>` 会执行）。今天是**不可达时的安全兜底**（`order_no` 由 `generate_order_no()` 生成 20 位数字）。<br>⚠️ 第 23 / 29 条原句「仍不可点 / 不挂 `.orderdetails`」的**更正**已分别追加在那两行里 |
 * | 35 | **关联订单正文是来源单号** | 正文是 `source_order_no`，`data-id` 仍是系统单号 `order_no`。对标也是正文与 `data-id` 两个号。 |
 * | 36 | ⭐ **「系统用户」那一格多了一个可点入口：点用户名看该行人员的微信收款码**（图片存储线 ，2026-09-25；决策 `D-0151` 的 A1/A8） | **⚠️ 无对标证据（如实）**：`.tools/recon/emp-pages/**`（本页 HTML / JS 勘察件）与 `public/replica/emp-emplist.html` 里 `grep -in "收款码\|二维码\|qr"` **0 命中**；spec §12 只说对标那个入口在「在线结算打款」弹层里（该层在本页证据里渲染打手支付宝账号 / 姓名，**没有二维码元素**）⇒ 形态**沿用本页既有的行内入口**（`orderIdTemplet` / `onOrderDetailClick` 那一套：函数 templet 产 `<a>` + `root` 委托 + `layer.open`），**文案「查看收款码」与那句「该人员还没有上传微信收款码」都是我方自拟**，⛔ 不是照对标。**两处可见差异**：① 对标这一格是纯文本，我方是蓝链（`#0094ff`）；② 点开的是**收款码图片**，而对标那层里是支付宝账号文本。⚠️ 没有收款码时**只提示、不开空白层**（任务书原话）。⚠️ **权限**：页面路由本身已由 `finance.payroll` 把守（`config/routePermissions.ts` 的 `agents-performance`），本入口**不新开权限点、前端也不另判一次**——与文件头「权限：不在前端判」同一口径。⚠️ 数据侧：`wechat_qr_url` 由后端在**列表载荷**里签发（短时链接），本页**零新增请求**。 |
 * | 37 | ⭐ **筛选栏新增「订单分组」一颗（对标没有这个筛选项）**（2026-10-26，计划 `docs/plans/2026-10-26-order-source-groups.md` §5） | 整块 `.layui-inline` 由页面**运行期注入**（`injectSourceGroupFilter`，锚点 = 同排「订单来源」那一块之后），控件与 `#orderSoures` 同族（原生 `<select>` + `xm-select-radio` 等 formSelects 属性，`name="sourceGroup"`），首项「全部分组」。⚠️ **两账单共用一个值**：本页只有一张筛选表单、页签只切 `emp_type`（`onTabClick` 不动表单）⇒ 跨页签**保留**。⚠️ 候选取不到时只剩首项、**不拦页**（同 `fetchGames().catch(() => [])` 口径） |
 * | 38 | ⭐ **「收款状态」（`furlSta`）从「尚未开放」改为真筛选**（D-0365，2026-11-02；计划 `docs/plans/2026-11-02-furlsta-filter-and-income-functions.md` §2） | **判据是有意偏离（与第 22 条色块同一句话）**：对标按它**上游结算**语义筛（`furlStatus`，三态 未/中/收、`title` 自述「与老板或上家结算…不是给打手的钱」）；我们按**接单实收款** `order.received_price` 筛——「未结算」⇒ `net_receipts_state=unreceived`（`null`/`0`）、「已结算」⇒ `received`（有值且 ≠ 0，含负数），映射见 `NET_RECEIPTS_STATE_MAP`。⚠️ **对标四档、我们接得上两档**：「结算中」在我方没有对应列语义（色块同款两态、不造「中」）⇒ 选它**不发参**、并进「「结算中」尚未开放，本次已忽略」的点名提示（`usedNotOpenFilters`，`orderSta` 未映射档位同一条通道）。后端参数与分支见 `PerformanceListSearchSerializer.net_receipts_state` 与 `performance_queryset` 的同名分支（列表与导出**自动同生效**） |
 * | 39 | ⭐ **「打手多选」内挂「其它分组」两颗虚拟分组哨兵**（2026-11-07；原独立「打手范围」三态下拉已撤） | 「排除禁止结算的」发 `settle_forbidden_scope=excluded`、「排除总绩效为负的」发 `exclude_net_negative=1`；哨兵值 = `:exclude_settle_forbidden` / `:exclude_net_negative`（`:` 不在 Django 用户名字符集内 ⇒ 与真用户名结构性不碰撞，唯一出处 `api/performances.ts`）。⚠️ **「批量搜用户」只覆盖 `username`**（`params()` 的合并语义），两颗哨兵产出的参数**照发**——勾了排除再切批量搜用户，排除不丢。哨兵从 `username` 里剥掉的翻译 = `splitPlayerFilterSelection`（与 v2 共用一份） |
 * | 40 | ⭐ **「挂负账」红签**（2026-11-07 R3，对标没有——功能性有意偏离登记；2026-11-08 业务方拍板撤强制展示后本签保留） | 行级：归属人在挂负账打手集 ⇒ 「系统用户」列用户名旁追加 `.lableb.negDebtMark` 红签「挂负账」（红 = #ff0000，形态照 `settleRequestMark`）。**零查询语义副作用**（行级标记，列表是纯筛选）；原统计句注脚「含 N 名挂负账打手的全部未结账单」随强制展示一并撤除（2026-11-08），`summary` 已无 `negative_debt_users` 键 |
 */
import type { PayrollBatch } from '@/api/onlinePayroll'
import { closeHint, legacyPayrollUrl, mountLegacyPreview } from '../onlinePayroll'
import {
  bootstrapIsland,
  initIslandElements,
  islandMsg,
  type IslandContext,
  type IslandPage,
} from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'
import { backendErrorMessage } from '@/layui/backendError'
import { escapeHtml } from '@/layui-island/escape'
import { defaultDateRange } from '@/layui-island/dateRange'
import { getFormSelects, initFormSelects, type FormSelectsInstance } from '@/layui/vendor'
import { notifyError } from '@/api/notify'
import {
  adjustPerformance,
  deletePerformances,
  downloadPerformanceExport,
  EMP_TYPE,
  fetchAlipayDuplicates,
  PLAYER_OPTION_EXCLUDE_NET_NEGATIVE,
  PLAYER_OPTION_EXCLUDE_SETTLE_FORBIDDEN,
  PERFORMANCES_URL as PERFORMANCES_PATH,
  splitPlayerFilterSelection,
  type AlipayDuplicateGroup,
  type PerformanceListItem,
  type PerformanceSearchParams,
  type PerformanceSummary,
} from '@/api/performances'
import { fetchGames, type ConfigRow } from '@/api/config'
// ⭐ 子页的 island key **从声明它的那个模块 import**（`pages/emp-settlementemp.ts` 的 `PAGE_KEY`），
// ⛔ 不在这里手抄一份字符串：key = 文件名，抄错一个字 `layui-island/page.ts` 的注册表就查不到，
// 弹层里是白板（`system-user-list.ts` 取 `USER_FORM_PAGE_KEY` 是同一手法）。
import { PAGE_KEY as SETTLEMENT_PAGE_KEY } from './emp-settlementemp'
import {
  fetchOrderSourceGroups,
  fetchOrderSources,
  type OrderSourceGroupRow,
  type OrderSourceRow,
} from '@/api/orderSources'
import { listAccounts, type AccountRow } from '@/api/accounts'
import { ORDER_STATUS_LABELS, orderStatusLabel, type OrderStatus } from '@/config/orderStatus'
import type { ArtifactCol } from '@/layui/artifactPage'

/** 证据件（页内 `<style>` + body 标记）。 */
const ARTIFACT_URL = 'replica/emp-emplist.html'
/** 主表 `elem` / `toolbar`。 */
export const TABLE_ELEM = '#tableList'
export const TOOLBAR_ELEM = '#toolbarDemo'
/** `layui.table` 的实例 id（`reload` / `checkStatus` 用的都是它，**不带 `#`**）。 */
export const TABLE_ID = 'tableList'
/** 查重弹层里那张 6 列子表的 `table.render` 目标。 */
export const TABLE3_ELEM = '#tableList3'
export const TABLE3_ID = 'tableList3'
/** 查重弹层的容器类名（`content` 借它给 `layer.open`，关闭后要放回来 + 重新隐藏）。 */
export const ALIPAY_PANEL_SELECTOR = '.checkAliPayAccDiv'
/** 分页 `limit`。 */
export const PAGE_LIMIT = 50
/** 分页 `limits`（8 档）。 */
export const PAGE_LIMITS = [10, 20, 30, 40, 50, 100, 200, 300]

/**
 * 对标的「录单时间」档（`#dateType` 的 `value="25"`）——本页的**默认档**。
 * ⚠️ 证据件里这一档已改选（`<option>` 只加 `selected`）：后端时间区间判的是 `created_at__date`
 * （= 录单时间），若默认停在「发单时间」而实际按录单时间筛，是两个事实不一致。
 */
export const RENTAL_DATE_TYPE = '25'

/**
 * 日期口径下拉（`#dateType`）的 option 值 → 后端 `date_type` 的自描述取值
 * （`PerformanceListSearchSerializer.date_type`）。⛔ 不是把对标的数字直接发给后端
 * （后端契约只认这五个自描述取值，对标的数字会回 `1000`）。
 *
 * | 档 | `#dateType` | `date_type` | 后端打的列 |
 * |---|---|---|---|
 * | 发单时间 | `10` | `published` | `Order.published_at`（**可空**） |
 * | 交单时间 | `20` | `submitted` | `Order.submitted_at`（**可空**） |
 * | 录单时间 | `25` | `created` | `Performance.created_at` |
 * | 验收时间 | `40` | `completed` | `Order.completed_at`（**可空**） |
 * | 结账时间 | `30` | `settled` | `Performance.settled_at`（**可空**） |
 *
 * ⚠️ **空值行为（后端口径，前端不额外提示）**：四个可空列 ⇒ 未发单 / 未交单 / 未完成 /
 * 未结账的行自然不出现，不是漏行；⛔ 本页不为它多弹一句。
 * ⚠️ 键是**字符串**：`form.val()` 读回来的是 `<option value>`（DOM 里恒为字符串）。
 */
export const DATE_TYPE_MAP: Record<
  string,
  'created' | 'published' | 'submitted' | 'completed' | 'settled'
> = {
  '10': 'published',
  '20': 'submitted',
  // ⚠️ 录单档的键直接引 `RENTAL_DATE_TYPE`（默认档的值只有一处定义）。
  [RENTAL_DATE_TYPE]: 'created',
  '40': 'completed',
  '30': 'settled',
}

/**
 * `#dateType` 的原始值 → 后端 `date_type`；**表外 / 空值 ⇒ `undefined`**（调用方据此「不发参数」）。
 * `toPerformanceSearch` 发不发、`filterWarnings` 提不提示**共用本函数**，⛔ 不各写一份查表。
 * ⚠️ 必须用 `Object.prototype.hasOwnProperty.call` 而不是裸查表：对象继承了 `constructor` /
 * `toString` 等**原型键**，裸查表会把它们当「已支持的档」、把继承来的函数塞进查询参数。
 */
function dateTypeOf(
  value: unknown,
): 'created' | 'published' | 'submitted' | 'completed' | 'settled' | undefined {
  const raw = text(value)
  return Object.prototype.hasOwnProperty.call(DATE_TYPE_MAP, raw)
    ? DATE_TYPE_MAP[raw]
    : undefined
}

/**
 * 主表 **20 列**（含勾选列），**全部可见**（列集与取舍见文件头「已知会不同」）。
 * ⚠️ `dlProject`（订单项目）**没有 `width`** ⇒ layui 眼里的自动列，**不要补**。
 * ⚠️ `edit: true` 只在 `tempAmount` / `bonusAmount` 两列（单元格直接编辑，
 * `table.on('edit(tableList)')` 接住）。
 */
export function buildCols(): Col<PerformanceRow>[] {
  return [
    { type: 'checkbox', fixed: 'left', width: 30 },
    // 「系统用户」格是函数 templet（可点链，见 `userNameTemplet`）；列宽 / align / fixed 既有取值照旧。
    {
      field: 'userName',
      title: '系统用户',
      fixed: 'left',
      width: 90,
      align: 'center',
      templet: (row) => userNameTemplet(row),
    },
    {
      field: 'tempAmount',
      title: '预计绩效',
      width: 80,
      align: 'center',
      edit: true,
      templet: (row) => performanceAmountTemplet(row, 'tempAmount'),
    },
    {
      field: 'bonusAmount',
      title: '生效绩效',
      width: 80,
      align: 'center',
      edit: true,
      templet: (row) => performanceAmountTemplet(row, 'bonusAmount'),
    },
    {
      field: 'deductFee',
      title: '扣点',
      width: 65,
      align: 'center',
      templet: (row) => deductFeeTemplet(row),
    },
    { field: 'price', title: '订单金额', width: 65, align: 'center', templet: (row) => priceTemplet(row) },
    { field: 'netReceipts', title: '实际收款', width: 65, align: 'center' },
    { field: 'refundFee', title: '退款', width: 55, align: 'center', templet: (row) => refundFeeTemplet(row) },
    { field: 'empType', title: '账单属性', width: 65, align: 'center', templet: (row) => empTypeTemplet(row) },
    { field: 'orderId', title: '关联订单', width: 110, templet: (row) => orderIdTemplet(row) },
    { field: 'gameRole', title: '角色名', width: 100, templet: (row) => gameRoleTemplet(row) },
    // 无 `width` ⇒ 自动列（**不要补**）
    { field: 'dlProject', title: '订单项目', align: 'left', templet: (row) => dlProjectTemplet(row) },
    { field: 'kefu', title: '销售客服', width: 60, align: 'center' },
    // 函数 templet：渲染那颗彩色的 `.lableb setdlsta` 订单状态标签，见 `orderStatusTagHtml`。
    {
      field: 'orderSta',
      title: '订单状态',
      width: 65,
      align: 'center',
      templet: (row) => orderStatusTagHtml(row),
    },
    { field: 'wangwang', title: '旺旺/YY', width: 60, align: 'center', templet: (row) => wangwangTemplet(row) },
    { field: 'createTime', title: '记录时间', width: 80, align: 'center', templet: (row) => spanTemplet(row.createTime) },
    // ⛔ `completeTime`（完成时间）/ `exceedHours`（是否超时）**已删列**（业务方裁定「不做也不要了」，
    // 文件头「已知会不同」第 26 条）。⛔ 不要以「尚未开放」的名义把它们加回来。
    { field: 'logUserName', title: '记录者', width: 70, align: 'center', templet: (row) => logUserNameTemplet(row) },
    // 函数 templet（有 spec 用例按枚举钉住）：渲染 `.lableb setSett` 那颗可点的标签，见 `settleTagHtml`。
    {
      field: 'settingSta',
      title: '结账状态',
      width: 60,
      align: 'center',
      templet: (row) => settleTagHtml(row),
    },
    { field: 'settingTime', title: '结账时间', width: 75, align: 'center', templet: (row) => spanTemplet(row.settingTime) },
    { field: 'empPayId', title: '结账方式', width: 60, align: 'center', templet: (row) => empPayIdTemplet(row) },
  ]
}

/** 表头成品——给 spec 用。 */
export const COLS: Col<PerformanceRow>[] = buildCols()

/**
 * `ArtifactCol.templet` 只收字符串选择器，本页「结账状态」列必须用**函数** templet ⇒ 换成函数签名；
 * 其余键原样继承 `ArtifactCol`（手法同 `emp-billsummary.ts` 的 `Col<T>`）。
 */
export type Col<T> = Omit<ArtifactCol, 'templet'> & { templet?: (row: T) => string }

/* ────────────────────── 单元格 templet ────────────────────── */

/**
 * 行值的字符串形态（`null`/`undefined` ⇒ `''`）。
 *
 * ⚠️ **不转义**：本函数只服务「拼进属性或文本之前还要做比较/加前后缀」的那几处
 * （金额、状态、链接），转义在各自的出口做（或由 `escapeHtml` 单独调）。
 */
function cellText(value: unknown): string {
  return value === null || value === undefined ? '' : String(value)
}

/**
 * 「可空金额」列的显示值：**`null` ⇒ `—`**，其余原样显示（后端契约：扣点越界就是 `null`，
 * ⛔ 不给 `0.00` 之类假数——「没这个数」与「这个数是 0」是两件事）。
 */
function amountOrDash(value: unknown): string {
  return value === null || value === undefined ? '—' : String(value)
}

/** `<span title="">值</span>`——对标 `gameRole` 列的产物（那个 `title` 是空的，照抄）。 */
export function gameRoleTemplet(row: PerformanceRow): string {
  return `<span title="">${escapeHtml(cellText(row.gameRole))}</span>`
}

/** `<span>值</span>`——对标 `dlProject` / `createTime` / `settingTime` 三列共用的产物。 */
export function spanTemplet(value: unknown): string {
  return `<span>${escapeHtml(cellText(value))}</span>`
}

export function dlProjectTemplet(row: PerformanceRow): string {
  return `<span>${escapeHtml(cellText(row.dlProject))}</span>`
}

/**
 * 「关联订单」列：正文 = **来源单号**（对标正文取 `res.jionOrder`、`data-id` 取 `res.orderId`，
 * 两个字段值不是一回事）；`data-id` 仍是**系统单号**（层里按 20 位系统单号取数，⛔ 别跟着正文改）。
 * ⚠️ 来源单号为空 ⇒ 回落成系统单号（D-0301：不再显示空）。
 * ⚠️ 正文与 `data-id` / `data-orderIdView` 都过 `escapeHtml`：来源单号是录入的自由文本。
 */
export function orderIdTemplet(row: PerformanceRow): string {
  // 正文 = 展示单号（来源号非空取它、空回退内置号）；data-id = 系统单号（寻址键，不变）。
  const viewNo = escapeHtml(cellText(row.sourceOrderNo) || cellText(row.orderId))
  const orderNo = escapeHtml(cellText(row.orderId))
  return (
    `<a class="orderdetails" href="javascript:void(0);" data-id="${orderNo}"` +
    ` data-orderIdView="${viewNo}"` +
    ` style="color:#0094ff" title="点击查看订单详情">${viewNo}</a>`
  )
}

/* ────────── 收款码入口 ────────── */

/**
 * 「查看收款码」入口的**类名**（不带点）。
 * ⚠️ 类名与选择器**必须分成两个常量**（`AGENTS.md` §5-G 陷阱①）：`class="${SELECTOR}"`
 * 会造出 `class=".viewWechatQr"`、选择器永远匹配不到（静默失败）。
 */
const WECHAT_QR_LINK_CLASS = 'viewWechatQr'
/** 上面的类名对应的**选择器**（`root` 上的事件委托用它）。 */
const WECHAT_QR_LINK_SELECTOR = '.viewWechatQr'

/** 没有收款码时的提示（我方自拟文案：没有可以逐字照抄的对标原文，⛔ 别写成「照对标」）。 */
export const NO_WECHAT_QR_TEXT = '该人员还没有上传微信收款码'

/**
 * 「系统用户」列的函数 templet：用户名渲染成可点链，点开看该行人员的微信收款码。
 * 形态沿用本页既有行内入口（`orderIdTemplet` 同构：函数 templet 的 `<a>` + `root` 委托），
 * 挂在人这一列（收款码是**人**的属性；列数 / 表头有用例钉住，不新开列）。
 * ⚠️ `data-id` 装**绩效行主键**（`row.id`），点击时回查整行读 `wechatQrUrl` ⇒ 签名 URL 不进 DOM。
 * 用户名是请求数据 ⇒ 过 `escapeHtml`。
 */
export function userNameTemplet(row: PerformanceRow): string {
  const name = escapeHtml(cellText(row.userName))
  const link =
    `<a class="${WECHAT_QR_LINK_CLASS}" href="javascript:void(0);" data-id="${row.id}"` +
    ` style="color:#0094ff" title="查看收款码">${name}</a>`
  // ⭐（2026-11-07 R3）「挂负账」红签（功能性有意偏离登记，文件头第 40 条）：
  // 形态照 `settleRequestMark` 同一颗 `.lableb`；红 = 本页红系 #ff0000（计划 §5.3-3）。
  const tag = row.negativeDebt
    ? `<span class="lableb negDebtMark" title="该打手名下还有未结账的负绩效行"` +
      ` style="background-color:#ff0000;margin-left:2px;">挂负账</span>`
    : ''
  return link + tag
}

/**
 * 「旺旺/YY」列：非空才渲染阿里旺旺对话链接，空值渲染成**空串**。
 * ⚠️ `uid` 过 `encodeURIComponent`：值来自用户输入，不编码会让 `&` / `?` 把 URL 拆坏。
 */
export function wangwangTemplet(row: PerformanceRow): string {
  const uid = cellText(row.wangwang)
  if (uid === '') return ''
  const href = `http://amos.alicdn.com/getcid.aw?v=2&uid=${encodeURIComponent(uid)}&site=cntaobao&s=2&groupid=0&charset=utf-8`
  return (
    `<a href="${href}" target="_blank" title="点击可以与买家对话">` +
    `${escapeHtml(uid)}</a>`
  )
}

/** 「记录者」列（`emplist.js` L445-448）：`<span lay-tips="由<X>记录此条账单(也是发单人)">`。 */
export function logUserNameTemplet(row: PerformanceRow): string {
  const name = cellText(row.logUserName)
  // 对标把名字同时放进 `lay-tips` 与正文；两处都转义（名字来自用户输入）。
  const safe = escapeHtml(name)
  return `<span lay-tips="由${safe}记录此条账单(也是发单人)">${safe}</span>`
}

/**
 * 「账单属性」列：把我们后端的 `emp_type` 真值折成对标那两个词的产物结构（判据取我们的真值）。
 */
export function empTypeTemplet(row: PerformanceRow): string {
  const value = cellText(row.empType)
  if (value === EMP_TYPE.MAIN) return '<span style="color:#5FB878">打手的</span>'
  if (value === EMP_TYPE.SALES) return '<span style="color:#5FB878">客服的</span>'
  return `<span style="color:#2F4056">${escapeHtml(value)}</span>`
}

/**
 * 「订单金额」列：**色块 + 红字金额**（色块在前）。颜色与单字照对标（`#E6E6FA`+「未」/ `#98FB98`+「收」）。
 * ⚠️ 判据是**有意偏离**（业务方拍板）：读**接单实收款** `net_receipts`——有值且 ≠ 0 ⇒ 收，
 * `null` / 0 ⇒ 未；对标色块的「与上游老板/上家结算」语义**不用**，⛔ 不拿「结账状态」去顶、
 * `title` 不抄对标原文。对标三态、我们两态 ⇒ **不造** `#B0E0E6`/「中」分支（恒假分支）。
 * ⚠️ 判据一律 `is not None` 语义（E-0021）：`'0.00'` 走「未」、负数按「有值且 ≠ 0」走「收」；
 * ⛔ 不能用真值判（`if (net)`）区分。判据读行载荷的 **`net_receipts` 原值**（不是 `—` 化的显示列）。
 * `title` 是字面常量不转义；`price` 是请求数据 ⇒ 过 `escapeHtml`。
 */
export function priceTemplet(row: PerformanceRow): string {
  const pvo = `<span style="color:#FF5722">${escapeHtml(cellText(row.price))}</span>`
  const net = row.net_receipts
  const received = net !== null && net !== undefined && Number(net) !== 0
  const color = received ? '#98FB98' : '#E6E6FA'
  const spanText = received ? '收' : '未'
  const title = received
    ? '本状态按「接单实收款」判定：订单已录入接单实收款（有值且不为 0）⇒ 收；要改请到订单管理里改价'
    : '本状态按「接单实收款」判定：订单未录入接单实收款（或为 0）⇒ 未；要改请到订单管理里改价'
  const vo = `<span class="lableb" style="background-color:${color}" title="${title}">${spanText}</span>`
  return vo + pvo
}

/**
 * 「退款」列：`refundFee > 0` ⇒ 红字 + 金额 `title`，否则灰 + `title="没有退款"`。
 * ⚠️ 对标 `title` 里还有退款方式 / 时间 / 操作人（在 `OrderRefund` 表，不在本列表口径）⇒ 只写金额；
 * 也不挂 `data-refundmodel`（挂了也是空值）。`null` / 空串按 0 处理。
 */
export function refundFeeTemplet(row: PerformanceRow): string {
  const fee = cellText(row.refundFee)
  const numeric = Number(fee)
  const positive = Number.isFinite(numeric) && numeric > 0
  const title = positive ? `退款金额：${fee}元` : '没有退款'
  const color = positive ? 'red' : '#999'
  return (
    `<a href="javascript:void(0)" class="refundFee" data-id="${escapeHtml(cellText(row.orderId))}"` +
    ` data-fee="${escapeHtml(fee)}" style="color:${color}" lay-tips="${escapeHtml(title)}">` +
    `${escapeHtml(fee)}</a>`
  )
}

/**
 * 提成说明：`logUserName` 含「固定」⇒ 按固定金额；含「接单价」⇒ 按接单价百分比；否则按利润百分比。
 * ⚠️ 判据是**子串包含**，不是相等。
 */
function commissionRemark(row: PerformanceRow): string {
  const logUserName = cellText(row.logUserName)
  if (logUserName.includes('固定')) return '按固定金额提成'
  if (logUserName.includes('接单价')) return '按接单价百分比提成'
  return '按利润百分比提成(退款部分已计算在内)'
}

/**
 * 「预计绩效」/「生效绩效」两列的 templet（产物逐字照对标）：
 * 亏损判据 `(price − tempAmount − refundFee) <= 0` 用 `Number()` 收口（`null` 参与运算等价 0）
 * ⇒ 加粗 + 追加「，这单貌似是亏损的」——那句**只在非 `empType==20`** 的那一支追加（对标原文如此）。
 * ⚠️ `href` 是 **`javascript:void(0);`（带分号）**——与本页另外两颗标签（不带分号）不同，
 * 照各自的产物写，别统一。
 */
export function performanceAmountTemplet(row: PerformanceRow, field: 'tempAmount' | 'bonusAmount'): string {
  const price = Number(cellText(row.price))
  const tempAmount = Number(cellText(row.tempAmount))
  const refundFee = Number(cellText(row.refundFee))
  const loss = price - tempAmount - refundFee <= 0
  const tempStyle = loss ? ';font-weight:bold;font-size:14px' : ''
  const remark = commissionRemark(row)
  const userName = cellText(row.userName)
  const tips =
    row.empType === EMP_TYPE.SALES
      ? `销售客服的提成账单，${remark}`
      : field === 'tempAmount'
        ? `发给${userName}时的预计发单价，结算时请以生效绩效为准，点击可以修改${loss ? '，这单貌似是亏损的' : ''}`
        : `最终需要给${userName}结算的金额，如果${userName}没亲自交单，这里就是0，点击可以修改${loss ? '，这单貌似是亏损的' : ''}`
  return (
    `<a href="javascript:void(0);" style="color:#FF5722${tempStyle}"` +
    ` lay-tips="${escapeHtml(tips)}">${escapeHtml(cellText(row[field]))}</a>`
  )
}

/**
 * 「扣点」列：`title` 写**扣点方式 + 参数值**（列名是「扣点（金额）」、显示的是算出来的金额）。
 * ⚠️ `deduct_fee` 为 `null`（扣点越界）⇒ 显示 `—`，⛔ 不显示 `0.00`；那种行的 `title` 只说
 * 「扣点越界，这一格没有数」，⛔ 不编一个参数值出来。
 */
export function deductFeeTemplet(row: PerformanceRow): string {
  const mode = cellText(row.deductMode)
  const value = cellText(row.deductValue)
  const stateText =
    mode === '0'
      ? `按比例 ${value}`
      : mode === '1'
        ? `固定金额 ${value}`
        : '未设置扣点'
  const hasValue = row.deductFee !== null && row.deductFee !== undefined
  const title = hasValue ? `扣点：${stateText}` : '扣点越界，这一格没有数'
  return (
    `<span style="color:#ff5722" title="${escapeHtml(title)}">` +
    `${escapeHtml(amountOrDash(row.deductFee))}</span>`
  )
}

export function empPayIdTemplet(row: PerformanceRow): string {
  const settled = row.settled_at !== null && row.settled_at !== undefined
  if (row.onlinePayrollBatchId && (row.settlementMethod === 'online' || row.onlinePayrollOccupied)) {
    const label = row.settlementMethod === 'online' ? '在线结算' : '付款处理中'
    return `<a href="${escapeHtml(legacyPayrollUrl(row.onlinePayrollBatchId))}" target="_top">${label}</a>`
  }
  return `<span class="lableb" style="background-color:#0094ff">${settled ? '人工结账' : ''}</span>`
}

/**
 * `#total` 的文案骨架：一份「骨架 + 6 个数字槽」的模板，槽位与形状即契约——
 * `条数`（裸整数）、五个金额槽（两位小数）、`记录退款金额`（**裸**，无小数）。
 * 数组里的 `'0'` / `'0.00'` 就是槽的占位值。对标自己的 `profit1` 笔误**照抄产物、不修**。
 */
export const TOTAL_TEXT = [
  '本次筛选条件下共计',
  '0',
  '条记录，总计【预计绩效',
  '0.00',
  '元，生效绩效',
  '0.00',
  '元，',
  '订单金额',
  '0.00',
  '元，记录退款金额',
  '0',
  '，预计差额利润',
  '0.00',
  '元，已生效差额利润',
  '0.00',
  '】，默认显示最近3个月数据，若需扩大时间范围，可手动选择起始日期',
].join('')

/**
 * 渲染 `#total` 的同一份骨架（数字换成本次筛选的真值；`summary` 的六个槽两位小数、
 * 退款槽**裸**）。⚠️ `summary` 省略时回落占位值（只给旧形状兜底，不是正常路径）；
 * ⛔ 不自己把 `null` / 缺项算成别的、也不伪造任何一项。
 * ⚠️ 数位形状**逐字不动**（两位小数 vs 裸 `0`）——⛔ 不要加千分位（形状变化会被 parity 网报差异）。
 */
export function totalText(total: number, summary?: PerformanceSummary | null): string {
  const n = Number.isFinite(total) ? total : 0
  // ⚠️ `null` 也走 fallback（金额族键可能是 null＝脱敏）；行为与改动前逐字相同。
  const slot = (value: string | null | undefined, fallback: string): string =>
    // 判据用 `undefined` / `null` / `''`（**不是真值**，E-0021）：`"0.00"` 是合法且常见的值。
    value === undefined || value === null || value === '' ? fallback : value
  // 槽位按出现顺序：前 5 个 `0.00` 是金额槽，另一个 `0.00` 由下面带上下文的替换单独吃掉。
  const base = replaceTotalSlots(TOTAL_TEXT, [
    slot(summary?.temp_amount, '0.00'),
    slot(summary?.bonus_amount, '0.00'),
    slot(summary?.order_price, '0.00'),
    slot(summary?.profit_expected, '0.00'),
    slot(summary?.profit_actual, '0.00'),
  ])
    .replace('共计0条记录', `共计${n}条记录`)
    .replace('元，记录退款金额0，', `元，记录退款金额${slot(summary?.refund_fee, '0')}，`)
  // （2026-11-08 撤：统计句注脚「含 N 名挂负账打手…」随强制展示功能一并取消，句尾回到骨架本体。）
  return base
}

/**
 * 把 `TOTAL_TEXT` 里整数位是 `0` 的槽按出现顺序换成给定值——骨架与数位形状一个字符都不动。
 * ⚠️ 只用 `String.prototype.replace`（第一个匹配）：本工程 `lib` 到 es2021，没有 `replaceAll`（TS2550）。
 * 槽识别：`0.00`（带小数）= 5 个金额槽；裸 `0` 一律跳过（条数 / 退款槽由别处处理）。
 */
function replaceTotalSlots(template: string, values: readonly string[]): string {
  let out = ''
  let rest = template
  for (const value of values) {
    const at = rest.indexOf('0.00')
    if (at < 0) break
    out += rest.slice(0, at) + value
    rest = rest.slice(at + '0.00'.length)
  }
  return out + rest
}

/**
 * 后端 `SettleStatus` 的两个真值——中文就是库里的值、`/performances/` 响应里的取值。
 */
export const SETTLE_STATUS = {
  UNSETTLED: '未结账',
  SETTLED: '已结账',
} as const

/** 结账状态：对标 `<option value="N|Y">` → 后端中文取值（`SettleStatus`）。 */
export const SETTLE_STATUS_MAP: Record<string, string> = {
  N: SETTLE_STATUS.UNSETTLED,
  Y: SETTLE_STATUS.SETTLED,
}

/**
 * 把表单原始值折成后端参数（**筛选的唯一出口**：列表、搜索、刷新、导出共用）。
 * 本函数不再产生 `review_status` 与 `project`（控件已裁，后端仍支持，只是没有入口）；
 * 「批量搜用户」的 `username` 由 `params()` 在本函数产物之外追加。
 * ⚠️ 值一律先过 `text()` 再判「有没有值」——判据是「空」而不是「假值」（E-0021：`0` 是合法值）。
 * ⚠️ **「全部」= 不送该参数**（⛔ 不照抄证据件的「全部」哨兵值：传 0 / 非法值会回 `1000`）。
 * ⚠️ `order_status` / `order_type` 送**枚举名**、`settle_status` 送**中文**——两类不要混。
 * 日期口径过 `DATE_TYPE_MAP` 折成 `date_type` 真进请求；两端原样透传（`YYYY-MM-DD` 与
 * `YYYY-MM-DD HH:mm:ss` 都发——date-only 维持「整天含端」、带时间按时刻生效，后端两种都收）。
 * ⭐「打手多选的两颗虚拟分组哨兵」（2026-11-07）：勾「排除禁止结算的」发
 * `settle_forbidden_scope=excluded`、勾「排除总绩效为负的」发 `exclude_net_negative=1`；
 * 哨兵先从 `username` 里剥掉（`splitPlayerFilterSelection`），剩余用户名空 ⇒ 不发 `username`。
 */
export function toPerformanceSearch(
  values: Record<string, unknown>,
  empType: string,
): PerformanceSearchParams {
  const out: PerformanceSearchParams = { emp_type: empType }
  const settle = SETTLE_STATUS_MAP[text(values.settingSta)]
  if (settle) out.settle_status = settle
  // 日期口径先折成 `date_type`（五档全在表里 ⇒ 正常控件值一定发得出去；表外怪值不传 = 后端默认录单）。
  // 它与两个日期端同一次进请求。
  const dateType = dateTypeOf(values.dateType)
  if (dateType !== undefined) out.date_type = dateType
  const from = dateTimePart(values.sDate)
  if (from) out.date_from = from
  const to = dateTimePart(values.eDate)
  if (to) out.date_to = to
  const game = positiveInt(values.gameName)
  if (game !== undefined) out.game = game
  // 收款状态两档真发参（判据 = 接单实收款，与色块同一句话）；
  // 「结算中」/ 怪值映射不到 ⇒ 不发参（档位名进 usedNotOpenFilters 点名提示）。
  const receiptsState = netReceiptsStateOf(values.furlSta)
  if (receiptsState !== undefined) out.net_receipts_state = receiptsState
  // 多选：逐 token 反查，已映射的码 `,` 拼串送 `order_status`；
  // 映射不到的档位不发（留给 usedNotOpenFilters 点名提示）；全部映射不到 ⇒ 不发键。
  const orderStatusCodes = orderStatusSelectionOfFilterValue(text(values.orderSta)).codes
  if (orderStatusCodes.length > 0) out.order_status = orderStatusCodes.join(',')
  const orderType = ORDER_TYPE_MAP[text(values.orderType)]
  if (orderType) out.order_type = orderType
  const orderSource = positiveInt(values.orderSoures)
  if (orderSource !== undefined) out.order_source = orderSource
  // 订单分组：没选（'' / 表外值）⇒ 不发键；两账单共用一个值（切页签不丢）。
  const sourceGroup = positiveInt(values.sourceGroup)
  if (sourceGroup !== undefined) out.source_group = sourceGroup
  // 「打手多选」两颗虚拟分组哨兵（2026-11-07）：先剥哨兵再拼 `username`；
  // 两颗哨兵各自发参，「批量搜用户」的 `username` 覆盖（`params()`）不影响它们照发。
  const playerSelection = splitPlayerFilterSelection(commaList(values.popgameName).split(','))
  const username = playerSelection.usernames.join(',')
  if (username) out.username = username
  if (playerSelection.excludeSettleForbidden) out.settle_forbidden_scope = 'excluded'
  if (playerSelection.excludeNetNegative) out.exclude_net_negative = '1'
  // `customer_type` 只在 `customer` 有值时才发（后端：单独传它不过滤）；
  // `0`（销售人）是**合法值**，所以这里判「空」而不是真值。
  const customer = commaList(values.inputOrderKefu)
  if (customer) {
    out.customer = customer
    out.customer_type = text(values.customerType) === '10' ? 10 : 0
  }
  // 「搜索1」= 订单号 ∪ 来源单号 ∪ 角色名的模糊关键词；切「批量搜单号」时不送（后端吃不了逗号串）。
  if (text(values.selectSeach2) !== '10') {
    const keyword = text(values.key1)
    if (keyword) out.keyword = keyword
  }
  // ⛔ **不再有「搜索2 ⇒ project」这一步**（2026-09-22 业务方裁掉那个控件，见本函数 docstring）。
  return out
}

/**
 * 筛选栏「订单分组」那颗（对标没有这个筛选项 ⇒ 运行期注入）。
 * ⚠️ **必须在 `initFormSelects` 之前**注入并填好 option（formSelects 在 `render()` 那一刻读 option）。
 * ⚠️ 两账单共用一个值：本页只有一张筛选表单、页签只切 `emp_type` ⇒ 跨页签保留。
 */
export const SOURCE_GROUP_SELECT_ID = 'sourceGroup'

/** 注入「订单分组」那一块；锚点是同排「订单来源」的 `.layui-inline`（结构变了返回 false）。 */
export function injectSourceGroupFilter(root: HTMLElement): boolean {
  if (root.querySelector(`#${SOURCE_GROUP_SELECT_ID}`) !== null) return true
  const anchor = root.querySelector('select#orderSoures')?.closest('.layui-inline') ?? null
  if (anchor === null) return false
  const html =
    '<div class="layui-inline">' +
    '<label class="layui-form-label" style="width: 80px">订单分组</label>' +
    '<div class="layui-input-inline" style="width:140px">' +
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

/** 把分组字典写进「订单分组」下拉（保留首项「全部分组」；只 append、不重写）。 */
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

/**
 * 打手多选下拉（`#popgameName`）里的「其它分组」虚拟组头 + 两颗哨兵 option
 * （2026-11-07 拍板的功能性筛选，对标筛选区没有这一组——有意偏离登记，非视觉复刻项；
 * 原独立「打手范围」三态下拉已撤）。两颗文案 = 业务方拍板的字面。
 * ⚠️ 必须在候选 append 之后、`initFormSelects` 之前调（formSelects 在 `render()` 那一刻读
 * option；formselects 原生支持 `<optgroup>` ⇒ 渲染成 `<dt>` 组头，库零改动）。
 */
export const PLAYER_VIRTUAL_GROUP_LABEL = '其它分组'

/**
 * 把虚拟分组**置顶**到 `#popgameName`（2026-11-08 业务方：排打手列表最前面；幂等；
 * 找不到该 select 时静默跳过——辅助筛选不拦页）。vendor 渲染顺序 = DOM 顺序
 * （`$(select).find('*').each`，formselects-v4.js L732-741）⇒ 插在第一个打手候选之前即渲染在最上；
 * ⛔ 不能插成 select 第一个子节点：首项空值 option 是 formselects 的 placeholder
 * （vendor 只跳过遍历首项的空值 option，L733），挪位会渲染出一个可点空白项。
 */
export function prependVirtualGroupOptions(root: HTMLElement): void {
  const select = root.querySelector<HTMLSelectElement>('#popgameName')
  if (!select) return
  if (select.querySelector('optgroup') !== null) return
  const html =
    `<optgroup label="${PLAYER_VIRTUAL_GROUP_LABEL}">` +
    `<option value="${PLAYER_OPTION_EXCLUDE_SETTLE_FORBIDDEN}">排除禁止结算的</option>` +
    `<option value="${PLAYER_OPTION_EXCLUDE_NET_NEGATIVE}">排除总绩效为负的</option>` +
    '</optgroup>'
  const anchor = Array.from(select.children).find(
    (child): child is HTMLOptionElement =>
      child.tagName === 'OPTION' && (child as HTMLOptionElement).value !== '',
  )
  if (anchor) anchor.insertAdjacentHTML('beforebegin', html)
  else select.insertAdjacentHTML('beforeend', html)
}

/* ═══════════ `#popgameName` 的远程关键词搜索（2026-11-09，业务方拍板「全改」） ═══════════ */

/** 打手多选控件的 formselects id（= 证据件里的 `xm-select="popgameName"`；`formSelects.config` 按它定向）。 */
export const PLAYER_SELECT_ID = 'popgameName'

/** 打手候选远程搜索的端点 URL（`xm-select-search` 属性值 = vendored formselects 的 searchUrl）。 */
export const PLAYER_SEARCH_URL = '/api/accounts/'

/** 关键词防抖毫秒（与管理订单页同款拍板值；vendored `ajax.delay`）。 */
export const PLAYER_SEARCH_DELAY_MS = 400

/** 打手候选远程请求的每页条数（预载时代的同款上界；只是把「取哪 200 个」从固定前 200 改成命中关键词的前 200）。 */
export const PLAYER_SEARCH_PAGE_SIZE = 200

/** 远程回包的一行（`keyName='name'`/`keyVal='value'` 是 vendored 默认键 ⇒ 行形状就是 `{name, value}`；`value` = username）。 */
export type PlayerOptionRow = { name: string; value: string }

/** 远程回包里的组头行（vendored `renderSelect` 对 `type==='optgroup'` 渲染 `<dt>` 组头，formselects-v4.js L735-737）。 */
export type PlayerGroupRow = { type: 'optgroup'; name: string }

/**
 * 「其它分组」组头 + 两颗哨兵行：每次远程回包都会**整体替换**下拉内容
 * （vendored `renderData` 的远程分支按回包重建 `<dl>`，formselects-v4.js L483-523），
 * 静态 option 里的哨兵会被冲掉 ⇒ `beforeSuccess` 把它们**置顶**塞回回包数组，
 * 勾选状态由 vendor 自己保留（`data[id].values` 不清，已选值重新打上选中样式）。
 */
export function playerSentinelRows(): Array<PlayerGroupRow | PlayerOptionRow> {
  return [
    { type: 'optgroup', name: PLAYER_VIRTUAL_GROUP_LABEL },
    { name: '排除禁止结算的', value: PLAYER_OPTION_EXCLUDE_SETTLE_FORBIDDEN },
    { name: '排除总绩效为负的', value: PLAYER_OPTION_EXCLUDE_NET_NEGATIVE },
  ]
}

/**
 * 账号列表信封 → 打手候选行（`code≠0` / `items` 形状不合 ⇒ 空数组，控件自己显示空态）。
 * 文本 = `display_name || username`（与预载时代同款回落）；`value` = username（后端筛 `user__username__in`，⛔ 不可换成 id）；
 * `username` 为空的行跳过（预载时代同款口径）。
 */
export function playerRowsFromAccountsEnvelope(res: unknown): PlayerOptionRow[] {
  if (typeof res !== 'object' || res === null) return []
  const body = res as { code?: unknown; data?: unknown }
  if (body.code !== 0) return []
  if (typeof body.data !== 'object' || body.data === null) return []
  const items = (body.data as Record<string, unknown>).items
  if (!Array.isArray(items)) return []
  return items.flatMap((row): PlayerOptionRow[] => {
    if (typeof row !== 'object' || row === null) return []
    const { username, display_name } = row as { username?: unknown; display_name?: unknown }
    if (typeof username !== 'string' || username === '') return []
    const name =
      typeof display_name === 'string' && display_name !== '' ? display_name : username
    return [{ name, value: username }]
  })
}

/**
 * formselects 远程搜索这份 ajax 配置「怎么造」（纯函数：jsdom 里 formselects 未渲染，页测试直调它断言各键）。
 * ⚠️ `beforeSearch` 是**承重闸**（空/空白 ⇒ 不发）：vendor 每次 render 对设了 searchUrl 的控件
 * 自动发一次空关键词首搜（formselects-v4.js:313-322，`beforeSearch` 是唯一闸 :358；行号证据与
 * 「闸必须在 render 之前预埋」的推导见 order-orderlist 页的 `filterSearchGate` 与它的页测试）。
 */
export function buildPlayerSearchConfig(): {
  searchName: string
  keyVal: string
  delay: number
  data: Record<string, number>
  beforeSearch: (id: unknown, url: unknown, keyword: unknown) => boolean
  beforeSuccess: (
    id: unknown,
    url: unknown,
    inputValue: unknown,
    res: unknown,
  ) => Array<PlayerGroupRow | PlayerOptionRow>
} {
  return {
    searchName: 'key',
    keyVal: 'value',
    delay: PLAYER_SEARCH_DELAY_MS,
    data: { page_size: PLAYER_SEARCH_PAGE_SIZE },
    beforeSearch: (_id, _url, keyword) => typeof keyword === 'string' && keyword.trim() !== '',
    // 返回**数组**（vendored 对数组自动包成 `{code:0, data:数组}`，formselects-v4.js L444-451）
    // ——哨兵置顶 + 关键词命中行；两条出路（返回数组 / 返回改形信封）只能占一条，本页占「数组」。
    beforeSuccess: (_id, _url, _input, res) => [
      ...playerSentinelRows(),
      ...playerRowsFromAccountsEnvelope(res),
    ],
  }
}

/** 给 `#popgameName` 注入远程搜索 ajax 配置（深合并进该 id 的 `ajaxs[id]`；jsdom 无 formselects ⇒ no-op）。 */
function configPlayerSearch(config: Record<string, unknown>): void {
  const fs = getFormSelects() as
    | (FormSelectsInstance & {
        config?: (configId: string, ajaxConfig: Record<string, unknown>) => unknown
      })
    | undefined
  fs?.config?.(PLAYER_SELECT_ID, config)
}

/**
 * 打手多选下拉面板加高（业务方 2026-11-08「对标形态=面板够高」；vendor 默认 300px：
 * `formselects-v4.css` 的 `.xm-select-parent .xm-form-select dl`）。窄选择器钉「相邻兄弟」形状
 * （formSelects 把 `.xm-select-parent` 插在原 `<select>` 之后）⇒ 同页其余 formselects 下拉不动。
 */
export const PLAYER_DROPDOWN_HEIGHT_STYLE_ID = 'tt-emplist-player-dropdown-height'
export const PLAYER_DROPDOWN_HEIGHT_STYLE =
  'select#popgameName + .xm-select-parent .xm-form-select dl { max-height: 420px; }'

/** 把任意表单值折成去空白后的字符串（非字符串一律 `''`——判据是「空」，不是「假值」）。 */
export function text(value: unknown): string {
  if (typeof value === 'string') return value.trim()
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  return ''
}

/**
 * 逗号串参数（`customer` / `username`）：逐项去空白、丢空项、拼回逗号串。
 * ⚠️ **不排序、不去重**：去重会掩盖「同一个人选了两遍」这类客户端异常。没有有效项 ⇒ `''`（不发参）。
 */
export function commaList(value: unknown): string {
  return text(value)
    .split(',')
    .map((item) => item.trim())
    .filter((item) => item !== '')
    .join(',')
}

/**
 * `game` / `order_source` 这类主键参数：正整数才返回，其余一律 `undefined` = 不发该参数。
 * ⚠️ `0` 也当「不发」：后端那两类参数 `min_value=1`，送 0 会回 `1000`（`0` 本来就不是合法筛选值）。
 */
export function positiveInt(value: unknown): number | undefined {
  const raw = text(value)
  if (!/^\d+$/.test(raw)) return undefined
  const n = Number(raw)
  return n >= 1 ? n : undefined
}

/**
 * 对标的 `orderType` `<option>` 值 → 我们的 `OrderType` 枚举名（`-1` 全部 = 不发参，
 * 后端没有 `-1`、传了回 `1000`）。
 */
export const ORDER_TYPE_MAP: Record<string, string> = {
  '0': 'BOOST',
  '1': 'COMPANION',
}

/**
 * 「订单状态」筛选的 option 值（中文 label）→ 后端 `OrderStatus` 枚举名。
 * 反查 `ORDER_STATUS_LABELS`（唯一一份中文表），⛔ 不另建第二张映射表。
 * ⚠️ 只看**单个** token；多选拆分在 `orderStatusSelectionOfFilterValue`。
 */
export function orderStatusOfFilterValue(value: string): OrderStatus | undefined {
  const hit = (Object.entries(ORDER_STATUS_LABELS) as [OrderStatus, string][]).find(
    ([, label]) => label === value,
  )
  return hit?.[0]
}

/**
 * 「订单状态」多选拼串 → `{ 已映射的枚举码, 未映射的档位 }`。
 * 已映射的码由 `toPerformanceSearch` 拼成 `order_status=码1,码2` 真送参；未映射的留给
 * `usedNotOpenFilters` 点名提示。⚠️ token **不清洗**（formselects 拼的是 option 原文）。
 */
export function orderStatusSelectionOfFilterValue(value: string): {
  codes: OrderStatus[]
  unmapped: string[]
} {
  const codes: OrderStatus[] = []
  const unmapped: string[] = []
  for (const token of value.split(',')) {
    const mapped = orderStatusOfFilterValue(token)
    if (mapped === undefined) unmapped.push(token)
    else codes.push(mapped)
  }
  return { codes, unmapped }
}

/**
 * 日期/时刻筛选端**原样透传**：`YYYY-MM-DD` 与 `YYYY-MM-DD HH:mm:ss` 都放行（返回 trim 后的原值），
 * 其余（含 `2026-6-2` 缺零补位）返回 `''`，让参数不发出。
 * ⚠️ 两格式与后端 `PerformanceListSearchSerializer` 的收参判据同口径。
 */
export function dateTimePart(value: unknown): string {
  if (typeof value !== 'string') return ''
  const trimmed = value.trim()
  return /^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2})?$/.test(trimmed) ? trimmed : ''
}

/**
 * 两个日期框里有没有「非空但取不出日期/时刻」的输入（`true` = 有，且那个参数会被静默丢掉）。
 * ⚠️ 判据**复用 `dateTimePart` 本身**，不重写一份正则（两处各写一份必然漂移）。
 * ⚠️ 「空」判据用 `!== ''` 而不是真值（E-0021）：空框是用户主动清空、不该提示。
 */
export function hasUnparsableDate(values: Record<string, unknown>): boolean {
  for (const key of ['sDate', 'eDate'] as const) {
    const raw = values[key]
    if (raw === undefined || raw === null) continue
    const text = typeof raw === 'string' ? raw.trim() : String(raw)
    if (text !== '' && dateTimePart(text) === '') return true
  }
  return false
}

/**
 * 收款状态筛选（`#furlSta`）的 `<option>` 值 → 后端 `net_receipts_state` 两档
 * （判据 = 接单实收款，与 `priceTemplet` 色块同一句话）。
 * ⚠️ 键是证据件的 option **value** 原文（`未结算` / `已结算`；页面**文案**是「未收款 / 已结款」）。
 * ⚠️ 「结算中」不在表里（我方没有对应列语义）⇒ 不发参、进 `usedNotOpenFilters` 点名提示。
 * ⚠️ 查表必须走 `netReceiptsStateOf`（`hasOwnProperty` 判据）：裸查表会把原型键当成已映射。
 */
export const NET_RECEIPTS_STATE_MAP: Record<string, 'received' | 'unreceived'> = {
  未结算: 'unreceived',
  已结算: 'received',
}

/** `#furlSta` 的原始值 → 后端两档；空值 / 「结算中」/ 任意怪值 ⇒ `undefined`（不发参）。 */
export function netReceiptsStateOf(value: unknown): 'received' | 'unreceived' | undefined {
  const raw = text(value)
  return Object.prototype.hasOwnProperty.call(NET_RECEIPTS_STATE_MAP, raw)
    ? NET_RECEIPTS_STATE_MAP[raw]
    : undefined
}

/**
 * 后端**没有**对应筛选参数、但证据件里**保留着**的控件（今天剩：申请结算的 / 排团团号）。
 * ⚠️ 默认值都是空（首个 option `value=""`、无 `selected`）⇒ 判据用「非空」即可，
 * 不需要跟「首屏默认值」比对。`label` 是提示里给用户看的名字。
 */
export const NOT_OPEN_FILTERS: ReadonlyArray<{ name: string; label: string }> = [
  { name: 'applicationSetSta', label: '申请结算的' },
  { name: 'taskSelect', label: '排团团号' },
]

/**
 * 用户**主动用过**、但本次会被忽略的筛选控件名（合并成一条提示用）。
 * 三类都收：`NOT_OPEN_FILTERS`；「订单状态」里映射不到的档位（多选已实装 ⇒ 只报未映射档位、
 * 点名档位本身，不带「（多选）」）；「收款状态」里映射不到的档位（今天可达的只有「结算中」）。
 */
export function usedNotOpenFilters(values: Record<string, unknown>): string[] {
  const used = NOT_OPEN_FILTERS.filter((f) => text(values[f.name]) !== '').map((f) => f.label)
  const furlSta = text(values.furlSta)
  if (furlSta !== '' && netReceiptsStateOf(furlSta) === undefined) {
    used.push(furlSta)
  }
  const orderSta = text(values.orderSta)
  if (orderSta !== '') {
    used.push(...orderStatusSelectionOfFilterValue(orderSta).unmapped)
  }
  return used
}

/**
 * 本次取数要弹给用户的**全部警告**（按顺序拼；空数组 = 没什么要说的）。
 * ⚠️ 合并成一次 `msg()`：`layer.msg` 后来的会覆盖先前的，分多次弹用户只看得到最后一句。
 */
export function filterWarnings(values: Record<string, unknown>): string[] {
  const out: string[] = []
  // ⚠️ 这里**没有**日期口径「尚未开放」提示（五档全在 DATE_TYPE_MAP 表里，那条提示的下游
  // 没有任何可达档位 ⇒ 已整条删除）。⛔ `dateTypeOf` 本体**保留**：它同时是发不发 `date_type` 的闸。
  // ⚠️ 不复述被删文案的原文（spec 有判据把那个主词当语料搜，写回来会红）。
  const ignored = usedNotOpenFilters(values)
  if (ignored.length > 0) out.push(`「${ignored.join('、')}」尚未开放，本次已忽略`)
  if (hasUnparsableDate(values)) {
    out.push('「录单时间」不是可识别的日期，本次已忽略该筛选')
  }
  // ⚠️ 「搜索2」的控件已整条删除（有意裁剪）⇒ 它不属于「尚未开放」，别给它编提示。
  if (text(values.selectSeach2) === '10' && text(values.key1) !== '') {
    out.push('「批量搜单号」尚未开放，本次已忽略「搜索1」')
  }
  return out
}

/**
 * 「订单状态」列那颗标签的**色板**（键是中文 label，不是我们的枚举名 ⇒ 映射前先过 `ORDER_STATUS_LABELS`）。
 */
export const ORDER_STATUS_COLORS: Record<string, string> = {
  待上号: '#585858',
  代练中: '#219227',
  待验收: '#ff6a00',
  已完成: '#0094ff',
  已暂停: '#FF5722',
  问题单: '#ff0000',
  已退单: '#efaaaa',
}

/** 未知状态的兜底色（对标 `let colorStr = '#b0aeae;'` 的**原文**，含那个多余分号）。 */
export const ORDER_STATUS_DEFAULT_COLOR = '#b0aeae;'

/**
 * 「订单状态」列的 templet（产物逐字照对标那颗 `.lableb setdlsta` 标签）。
 * ⚠️ `data-id` 装的是**订单号**（不是绩效行主键，与同页 `.setSett` 的 `data-id` 语义**不同**，别混）。
 * ⚠️ `label` 与 `orderId` 来自请求数据、要进 `title` / `data-id` / 正文三个 HTML 上下文
 * ⇒ 一律过 `escapeHtml`（不转义就是存储型 XSS）；配色 / `title` 骨架 / `class` 不动。
 * `color` 只来自常量表，不转义。
 */
export function orderStatusTagHtml(row: PerformanceRow): string {
  const label = String(row.orderSta ?? '')
  const color = ORDER_STATUS_COLORS[label] ?? ORDER_STATUS_DEFAULT_COLOR
  const safeLabel = escapeHtml(label)
  const safeOrderId = escapeHtml(String(row.orderId ?? ''))
  return (
    `<a href="javascript:void(0)" class="lableb setdlsta" data-id="${safeOrderId}"` +
    ` title="当前订单状态：${safeLabel}" style="background-color:${color};">${safeLabel}</a>`
  )
}

/**
 * 列表行 → 表格列的字段映射（**只映射后端确有的字段**，其余列留空——不造数据）。
 *
 * | 列 `field` | 右值（后端 key） |
 * |---|---|
 * | `userName` | `display_name`（昵称优先、空串回落登录账号）；`?? item.username` 只是部署窗口兜底 |
 * | `tempAmount` / `bonusAmount` | 同名下划线字段（两位小数字符串） |
 * | `deductFee` | `deduct_fee`（后端算好的扣点金额；越界 ⇒ `null`） |
 * | `orderId` | `order_no`——只作开层链接的 `data-id`（层里按系统单号取数），不是列正文 |
 * | `sourceOrderNo` | `source_order_no`——「关联订单」列**正文**；空 ⇒ 渲染空，⛔ 不回落 `orderId` |
 * | `empType` | `emp_type`（`打单工资` / `销售提成`） |
 * | `orderSta` | `order_status` → 中文 label（`orderStatusLabel()`） |
 * | `settingSta` | `settle_status` |
 * | `createTime` | `created_at`（格式化） |
 * | `price` | `order_price` |
 * | `netReceipts` | `net_receipts`（可 `null`，显示走 `amountOrDash`） |
 * | `refundFee` | `refund_fee` |
 * | `gameRole` | `game_role` |
 * | `dlProject` | `dl_project` |
 * | `kefu` | `kefu` |
 * | `wangwang` | `wangwang` |
 * | `logUserName` | `log_user_name` |
 * | `settingTime` | `settled_at`（格式化；可 `null`） |
 * | \`empPayId\` | 按结账方式或付款单状态渲染。 |
 *
 * ⚠️ **`null` 两条口径不同**：金额类（`net_receipts` / `deduct_fee` / `settingTime`）走
 * `amountOrDash`（`null` ⇒ `—`，后端契约不给假数）；文本类 `?? ''`（空串就是空单元格）。
 * ⚠️ 另有**不在列上、只给动作读**的字段：`id`（主键）、`user`（归属人，结账闸门）、
 * `settled_at`（empPayId 判据）、`deductMode` / `deductValue`（扣点 title）、
 * `net_receipts` 原值（色块判据，`is not None` 族）。
 */
export function toRow(item: PerformanceListItemWithWechatQr): PerformanceRow {
  return {
    // 显示名取 display_name（昵称优先、空串回落登录账号）；?? username 是部署窗口兜底，不是第二套口径。
    userName: item.display_name ?? item.username ?? '',
    // ⭐（2026-11-07 R3）「挂负账」红签（后端恒在键；缺键 = 旧后端部署窗口 ⇒ fail-closed false）。
    negativeDebt: item.negative_debt === true,
    tempAmount: item.temp_amount,
    bonusAmount: item.bonus_amount,
    deductFee: amountOrDash(item.deduct_fee),
    deductMode: item.deduct_mode,
    deductValue: item.deduct_value,
    orderId: item.order_no,
    // 「关联订单」列正文取来源单号（orderId 键仍是 data-id 的来源 ⇒ 开层按系统单号取数）。
    // ?? '' 是部署窗口兜底；⛔ 不是「空就回落系统单号」——空串就是空串。
    sourceOrderNo: item.source_order_no ?? '',
    empType: item.emp_type,
    orderSta: orderStatusLabel(item.order_status ?? ''),
    settingSta: item.settle_status,
    createTime: formatDateTime(item.created_at),
    price: amountOrDash(item.order_price),
    netReceipts: amountOrDash(item.net_receipts),
    refundFee: amountOrDash(item.refund_fee),
    gameRole: item.game_role ?? '',
    dlProject: item.dl_project ?? '',
    kefu: item.kefu ?? '',
    wangwang: item.wangwang ?? '',
    logUserName: item.log_user_name ?? '',
    settingTime: item.settled_at === null ? '—' : formatDateTime(item.settled_at),
    settled_at: item.settled_at,
    settlementMethod: item.settlement_method,
    onlinePayrollBatchId: item.online_payroll_batch_id,
    onlinePayrollOccupied: item.online_payroll_occupied,
    net_receipts: item.net_receipts,
    takerPrice: item.taker_price ?? '',
    id: item.id,
    user: item.user,
    // 微信收款码：后端只读键（短时签名 URL）；null 与 '' 都当「没有」（判据 is not None 族）。
    wechatQrUrl: item.wechat_qr_url ?? null,
    // 提前结算申请时刻；「结账状态」列据此渲染「已申请」标记。
    // ⚠️ 保留原值（判据 is not None 族）：格式化会把 null 变成 ''/—，两件事就分不开了。
    settleRequestedAt: item.settle_requested_at ?? null,
  }
}

/**
 * `toRow` 的入参形状：在 `PerformanceListItem` 之上局部承认两个后端已上线的只读键
 * （`wechat_qr_url` / `settle_requested_at`）。不改 `@/api/performances` 的共享类型
 * （改动面限定在本文件）；在别处按旧类型读这些键会**编译不过**（不会静默漂移）。
 */
export type PerformanceListItemWithWechatQr = PerformanceListItem & {
  /** 微信收款码的短时效读链接；没传过 / 没有档案 ⇒ `null`（字段缺席按 `null` 处理）。 */
  wechat_qr_url?: string | null
  /**
 * 提前结算申请的申请时刻（`PerformanceListSerializer.settle_requested_at`，
 * 2026-10-23）。没申请 ⇒ `null`；字段缺席（旧后端）同样按 `null` 处理。
 */
  settle_requested_at?: string | null
}

/**
 * 表格行的字段（`toRow` 的产物）。只有这几个键是动作 / 模板真的读的。
 */
export type PerformanceRow = Record<string, unknown> & {
  /** 绩效行主键。对标 `.setSett` 的 `data-id` 装的就是这个（`res.empId` = 绩效行，不是人）。 */
  id: number
  /** 归属人主键（`Performance.user`，`null` = 还没接单人）。 */
  user: number | null
  /** 归属人用户名（显示用）。 */
  userName: string
  /** ⭐（2026-11-07 R3）「挂负账」红签判据（后端恒在布尔键；行级标记，零查询语义副作用）。 */
  negativeDebt: boolean
  /** 结账状态：`未结账` / `已结账`。 */
  settingSta: string
    /**
   * 本行的提前结算申请时刻（原值；`null` = 没申请过）。
   * ⚠️ 判据 `is not None` 族：格式化会把 `null` 变成 `''` / `—`；后端只按**该行自己**取。
   */
  settleRequestedAt: string | null
    /**
   * 「关联订单」列**正文** = 来源单号。
   * ⚠️ 与 `orderId` 是两个键、别合并：`orderId` 装系统单号、只作开层 `data-id`；本键只作正文。
   * ⚠️ 空串 ⇒ 正文渲染空，⛔ 不回落成 `orderId`。
   */
  sourceOrderNo: string
    /**
   * 结账时间（ISO 原值，`null` = 还没结账）。保留原值：`empPayIdTemplet` 判「结过账没有」用。
   */
  settlementMethod?: PerformanceListItem['settlement_method']
  onlinePayrollBatchId?: string | null
  onlinePayrollOccupied?: boolean
  settled_at: string | null
    /**
   * 接单实收款的原值（`null` = 没填）。保留原值：`priceTemplet` 判「有没有收到钱」用——
   * `null` 与 `'0.00'` 必须区分得开（`amountOrDash` 会把 `null` 变 `—`）。
   */
  net_receipts: string | null
    /**
   * 微信收款码的短时签名读链接。`null` / `''` 都按「没有」处理 ⇒ 点击时只提示、不开层。
   * ⚠️ 这里不校验 URL 形状（链接由后端签发）；真正要防的是进 HTML 上下文时的转义（在出口做）。
   */
  wechatQrUrl: string | null
}

/**
 * 「结账状态」列的 templet（产物逐字照对标那颗 `.lableb setSett` 标签；色值 / `title` / `href`
 * 一律从证据件取，`href` **没有**尾分号）。⚠️ `data-id` 填 `row.id`（**绩效行主键**，不是人的 id）；
 * 未知 `settingSta` 渲染成「未结账 + `#CDC8B1`」（默认分支同形）。
 * ⚠️ 申请过提前结算 ⇒ 本格内**追加**一颗「已申请」标记（列数 / 列宽 / 位置一字不动——
 * `COLS` 恰好 20 列的集合断言钉着）；没申请时产出的 HTML 与加标记之前**逐字节相同**。
 * 申请时刻走 `escapeHtml`（进 HTML 属性上下文）。
 */
export function settleTagHtml(row: PerformanceRow): string {
  const settled = row.settingSta === SETTLE_STATUS.SETTLED
  const text = settled ? SETTLE_STATUS.SETTLED : SETTLE_STATUS.UNSETTLED
  const title = settled ? '该笔订单已结账过' : '该笔订单还未进行结账'
  const color = settled ? '#9ACD32' : '#CDC8B1'
  const tag =
    `<a href="javascript:void(0)" class="lableb setSett" data-id="${row.id}"` +
    ` title="${title}" style="background-color:${color};">${text}</a>`
  const requestedAt = row.settleRequestedAt
  // `null` / `undefined` = 「没申请」；空串同样按「没有」处理（不该冒出一颗空标记）。
  if (requestedAt === null || requestedAt === undefined || requestedAt === '') {
    return tag
  }
  const shown = formatDateTime(requestedAt) || requestedAt
  const markTitle = `该笔绩效已申请提前结算（申请时间：${shown}）`
  return (
    tag +
    `<span class="lableb settleRequestMark" title="${escapeHtml(markTitle)}"` +
    ` style="background-color:#6A5ACD;margin-left:2px;">已申请</span>`
  )
}

/** 后端回 ISO 8601（`2026-09-20T10:00:00+08:00`）；对标的这一列是 `Y-m-d H:i` 文本。 */
export function formatDateTime(value: unknown): string {
  if (typeof value !== 'string') return ''
  const m = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/.exec(value)
  return m ? `${m[1]} ${m[2]}` : ''
}

/**
 * 工具栏 `lay-event` → 按钮文案。
 * ⚠️ 已裁的 4 个 event **不在**表里——留着会让兜底分支给屏幕上没有的按钮编出文案（有 spec 钉住）。
 */
export const TOOLBAR_LABEL: Record<string, string> = {
  refresh: '刷新列表',
  settBtn: '仅修改结算状态',
  checkIdenticalAlipayAccBtn: '检测重复收款支付宝',
  payBtn: '结算并直接打款',
  payBalanceBtn: '鸡蛋铺余额充值',
  exportBtn: '导出记录',
  delEmpBtn: '删除记录',
}

/** ⭐ P4FU-D 按业务方裁定**裁剪掉**的 4 个 `lay-event`（留成常量，好让测试与注释同源）。 */
export const REMOVED_TOOLBAR_EVENTS = [
  'refrshTaobaoBtn',
  'changeRemarkBtn',
  'changeStaBtn',
  'sendSmsBtn',
] as const

/** 取按钮文案；未知 event 回退成 event 本身（提示里仍能看出点了哪颗）。 */
export function toolbarLabel(event: string | undefined): string {
  if (!event) return '该功能'
  return TOOLBAR_LABEL[event] ?? event
}

/**
 * 「清算绩效」子页弹层的标题（对标 `settSta()` 的 `layer.open({title:"清算绩效"})`）。
 * 对标点 `.setSett` / 工具栏 `settBtn` 开的是同一个子页；「选哪个状态 / 要不要写原因」都在子页里做。
 */
export const SETTLE_DIALOG_TITLE = '清算绩效'

/**
 * island 静态页的基址（`layer.open({type:2})` 的 `content` 前缀）。
 * ⚠️ **与接口基址分开**：页面是构建产物里的静态资源，走 `BASE_URL`；子路径部署下拿接口基址拼
 * 页 URL 会**整页 404**。剥尾斜杠再拼（`BASE_URL` 按 Vite 约定总带尾斜杠）。
 */
const PAGE_BASE = import.meta.env.BASE_URL.replace(/\/$/, '')

/**
 * 拼 island 子页 URL。`base` 可注入只为测试能换基址验证拼接（生产只传第一个实参）；
 * 注入的 `base` 也按同一规则剥尾斜杠（否则测试路径与生产路径不同形）。
 */
export function islandPageUrl(query: string, base: string = PAGE_BASE): string {
  return `${base.replace(/\/$/, '')}/layui-island/page.html?${query}`
}

/**
 * 把一批绩效行主键折成 URL 的 `ids` 段——**只留正整数**（白名单化）。
 * ⚠️ 这串会被 layui 拼进 iframe 的 `src` 属性（HTML 属性上下文）⇒ 形状不对的 id 丢掉，
 * 产物只可能含数字与逗号（注入汇点的出口兜底）。
 */
export function settlementIdsParam(ids: readonly number[]): string {
  return ids.filter((id) => Number.isSafeInteger(id) && id > 0).join(',')
}

/**
 * 「清算绩效」子页的弹层 `content`。`page=` 的值从子页模块 import（⛔ 不手抄字符串——
 * key 就是文件名，抄错弹层里就是白板）。
 */
export function settlementDialogContent(
  ids: readonly number[],
  base: string = PAGE_BASE,
): string {
  return islandPageUrl(`page=${SETTLEMENT_PAGE_KEY}&ids=${settlementIdsParam(ids)}`, base)
}

/** 支付宝查重弹层的标题（逐字照对标）。 */
export const ALIPAY_DIALOG_TITLE = '重复收款支付宝子账号检测'

/** 删除确认层的正文（逐字照对标）。 */
export const DELETE_CONFIRM_TEXT =
  '真的删除吗？警告：一旦删除，将无法找回，请谨慎处理，请勿随意删除绩效'

/** 勾 0 行时的提示——**逐字**取自对标（`settBtn`/`delEmpBtn`/`changeStaBtn` 三处同句）。 */
export const NEED_SELECTION_TEXT = '请选中要操作的记录'

/**
 * 「批量搜用户」弹层的标题（逐字照对标，含「注意：该搜索会排除其它搜索条件」半句）。
 * ⚠️ **只是照抄文案、不是照做行为**：对标自己的 JS 并不排除其它条件——我们照 JS 的行为
 * （全部筛选 + `username`）、照标题的文案。别把这条读成「我们真的排除了」。
 */
export const BATCH_USER_PROMPT_TITLE =
  '按系统用户昵称批量查询，每行一条。注意：该搜索会排除其它搜索条件'

/** 「批量搜用户」内容为空时的提示——**逐字**取自对标 `emplist.js` L546。 */
export const BATCH_USER_EMPTY_TEXT = '搜索项不能为空'

/**
 * 支付宝查重子表的一行（后端「按账号分组」的树要先摊平）。字段名照抄对标的列定义
 * （`str1`/`str2`/`str3`/`id2`/`str4`），列定义可与对标逐条对齐。
 */
export type AlipayRow = {
    /**
   * 子账号名称（人名）：取显示名（昵称优先、空串回落登录账号），并回落老键 `username`
   * （防部署窗口）。⛔ 不回落成空串。
   */
  str1: string
  /** 支付宝账号（后端 `duplicates[i].alipay_account`，**完整值**）。 */
  str2: string
  /** 职位（后端 `users[i].job`，多个用「、」连接；没有职位是空串）。 */
  str3: string
  /** 重复数量（后端 `duplicates[i].count`，≥2）。 */
  id2: number
  /** 当前状态（后端 `users[i].is_active` → `'true'`/`'false'`，照对标 `res.str4 == 'true'` 的字符串比较）。 */
  str4: string
}

/**
 * 把后端「按支付宝账号分组」的树摊平成子表的行。
 * ⚠️ `str4` 用字符串 `'true'`/`'false'` 而不是布尔：对标的 templet 是宽松相等 + 字符串，
 * 照抄这个判据，免得后端形状改后两边不一致。
 */
export function alipayRows(data: { duplicates?: AlipayDuplicateGroup[] } | null | undefined): AlipayRow[] {
  const rows: AlipayRow[] = []
  for (const group of data?.duplicates ?? []) {
    for (const user of group.users ?? []) {
      rows.push({
        // 「子账号名称」是人名 ⇒ 取显示名；display_name 缺席时回落 username（⛔ 不回落空串）。
        str1: user.display_name || user.username,
        str2: group.alipay_account,
        str3: user.job ?? '',
        id2: group.count,
        str4: user.is_active ? 'true' : 'false',
      })
    }
  }
  return rows
}

/**
 * 查重子表的 6 列（列宽 / 对齐 / `sort` / templet 产物照抄）。
 * ⚠️ 对标 5 个数据列都写了 `unresize: false`（等于没写）⇒ 不搬那个死属性。
 */
export function buildAlipayCols(): Col<AlipayRow>[] {
  return [
    { type: 'checkbox', fixed: 'left', width: 30 },
    { field: 'str1', title: '子账号名称', width: 110 },
    { field: 'str2', title: '支付宝账号', width: 170, align: 'left' },
    { field: 'str3', title: '职位', width: 85, align: 'center' },
    {
      field: 'id2',
      title: '重复数量',
      width: 85,
      align: 'center',
      sort: true,
      templet: (row) =>
        `<span lay-tips="此收款支付宝账号存在${row.id2}个，该子账号是其中一个">${row.id2}</span>`,
    },
    {
      field: 'str4',
      title: '当前状态',
      width: 85,
      align: 'center',
      sort: true,
      templet: (row) =>
        row.str4 === 'true'
          ? '<span class="layui-badge layui-bg-blue">正常</span>'
          : '<span class="layui-badge layui-bg-gray">禁用</span>',
    },
  ]
}

/** 表头成品——给 spec 用。 */
export const ALIPAY_COLS: Col<AlipayRow>[] = buildAlipayCols()

/**
 * `#gameName`（选择游戏）的 option 列表——纯函数，便于单测。
 * ⚠️ 首项取 `请选择`（**空值**）而不是「全部游戏」：「全部」= 不送 `game` 参数
 * （⛔ 不照抄对标的 `21714` 哨兵值——会回 `1000`）；且 layui 在首项有值时显示自己的
 * placeholder，首项 `value=""` ⇒ 文案必须是 `请选择`（否则 parity 网报 placeholder 差异）。
 * 证据件里的 option 是对标租户的游戏 ⇒ 运行时整个重写。
 */
export function gameOptionsHtml(games: Array<{ id: number; name: string }>): string {
  return [
    '<option value="">请选择</option>',
    ...games.map((game) => `<option value="${game.id}">${game.name}</option>`),
  ].join('\n')
}

/**
 * `layer.confirm(content, options, yes)` 的签名——第二参是 options，`yes(index)` 的 index 交给 `layer.close`。
 */
type SettleConfirm = (
  content: string,
  options: unknown,
  yes: (index: number) => void,
) => void

export default {
  mount: async (root: HTMLElement, ctx: IslandContext): Promise<() => void> => {
    void ctx
    // 对标 `<title>` 是服务端渲染的「绩效管理」（`Emp__emplist.html` L5）。
    document.title = '绩效管理'
    const layui: LayuiNamespace = await bootstrapIsland(['form', 'table', 'layer', 'laydate'])
    const form = layui.form as FormModule
    const table = layui.table as TableModule
    const layer = layui.layer as LayerModule
    const laydate = layui.laydate as LayuiModule

    const artifact = await fetch(`${import.meta.env.BASE_URL}${ARTIFACT_URL}`)
    if (!artifact.ok) throw new Error(`证据件取不到（HTTP ${artifact.status}）：${ARTIFACT_URL}`)
    root.innerHTML = await artifact.text()

    // 打手多选下拉面板加高（业务方 2026-11-08）；幂等：重挂载先摘旧的再插。
    document.getElementById(PLAYER_DROPDOWN_HEIGHT_STYLE_ID)?.remove()
    const playerDropdownStyle = document.createElement('style')
    playerDropdownStyle.id = PLAYER_DROPDOWN_HEIGHT_STYLE_ID
    playerDropdownStyle.textContent = PLAYER_DROPDOWN_HEIGHT_STYLE
    document.head.appendChild(playerDropdownStyle)

        /**
     * ⚠️ 两个日期框的默认值必须在挂载时重算（今天 − 3 个自然月 ~ 今天 23:59:59）：
     * 证据件里的 `value` 是对标服务端按取数当天渲染的死值，而它们是**真的发给后端的筛选**
     * ⇒ 照抄会让默认视图永远停在过去、快照之后录的单默认看不见。
     * ⚠️ 必须在 `root.innerHTML = …` **之后**：这两个 `input` 是证据件里的节点。
         */
    const { sDate, eDate } = defaultDateRange()
    const sDateInput = root.querySelector<HTMLInputElement>('input#sDate')
    const eDateInput = root.querySelector<HTMLInputElement>('input#eDate')
    if (sDateInput) sDateInput.value = sDate
    if (eDateInput) eDateInput.value = eDate

    // ⚠️ 默认值不走 JS：`laydate` 在建实例那一刻读 `elem` 的现值 ⇒ **必须先填默认值、再 render**（顺序承重）。
    // ⛔ 不给这两个框加 `min` / `max`（对标没有；加了会静默禁掉更早的日期）。
    ;(laydate.render as (o: unknown) => void)({ elem: '#sDate', type: 'datetime' })
    ;(laydate.render as (o: unknown) => void)({ elem: '#eDate', type: 'datetime' })

    // ── 三个筛选控件的候选来自我们的数据 ──
    // ⚠️ 必须在 `initFormSelects` 之前把 option 填好（formselects 在 render() 那一刻读 option 建 <dl>）。
    // ⚠️ 字典取不到不报错也不拦页（辅助筛选）；⚠️ `page_size: 200`（后端上限现为 300，本页未顶格）
    // ⇒ 超过 200 个账号的租户，第 201 个起选不到（已知边界，静默）。
    // ⭐ 这份预载现在只喂 `#inputOrderKefu`（销售人/发单人那一格；非打手/下家，不在本轮范围）——
    // 打手多选已改远程关键词搜索（见「`#popgameName` 的远程关键词搜索」一节），不再吃它。
    const [games, orderSources, orderGroups, accounts] = await Promise.all([
      fetchGames().catch(() => [] as ConfigRow[]),
      fetchOrderSources()
        .then((page) => page.items)
        .catch(() => [] as OrderSourceRow[]),
      // ⭐ 「订单分组」那颗的候选（同一口径：取不到不拦页、空下拉 = 这一项筛不了）。
      fetchOrderSourceGroups()
        .then((page) => page.items ?? [])
        .catch(() => [] as OrderSourceGroupRow[]),
      listAccounts({ page_size: 200 })
        .then((page) => page.items)
        .catch(() => [] as AccountRow[]),
    ])
    const gameSelect = root.querySelector<HTMLSelectElement>('#gameName')
    if (gameSelect) gameSelect.innerHTML = gameOptionsHtml(games)
    const orderSourceSelect = root.querySelector<HTMLSelectElement>('#orderSoures')
    if (orderSourceSelect) {
      for (const source of orderSources) {
        const option = document.createElement('option')
        option.value = String(source.id)
        option.textContent = source.name
        orderSourceSelect.appendChild(option)
      }
    }
    // 注入与填 option 都必须在 `initFormSelects` 之前。
    injectSourceGroupFilter(root)
    fillSourceGroupSelect(root, orderGroups)
    // ⚠️ 值 = `username`、文本 = 显示名：后端筛 `user__username__in` ⇒ ⛔ `option.value` 不许改成 id
    // （改了筛出来恒空）；显示名 = `display_name || username`（显式回落）。
    const candidates = accounts.filter((account) => account.username !== '')
    const kefuSelect = root.querySelector<HTMLSelectElement>('#inputOrderKefu')
    if (kefuSelect) {
      // ⚠️ 只 append、**不重写**：首项那个空值 option 是 formselects 的 placeholder
      // （`值="" 的 text` 就是折叠态显示的文案），证据件里本来就有。
      for (const account of candidates) {
        const option = document.createElement('option')
        option.value = account.username
        option.textContent = account.display_name || account.username
        kefuSelect.appendChild(option)
      }
    }
    // 「打手多选」的两颗虚拟分组哨兵：`initFormSelects` 之前置顶注入（formSelects 在 render
    // 时刻读 option；`<optgroup>` 由库原生支持）。打手**账号**候选不再预载（改远程关键词搜索）。
    prependVirtualGroupOptions(root)
    // ⭐ 打手多选开远程搜索（2026-11-09）：`xm-select-search` 属性值 = searchUrl（vendored：
    // 有值即远程搜索，空值 = 只在已加载 option 里本地过滤）。配置（含空关键词闸）必须在
    // `initFormSelects` 之前 `formSelects.config` 进 `ajaxs[id]`——vendor 在 render 那一刻就
    // 判闸并排定首发空关键词请求（之后不再回头判），晚了闸不住（推导见 order-orderlist
    // 页的 `filterSearchGate`；`config()` 对未渲染的控件顺手 render，此处的 render 已带闸）。
    const playerSelect = root.querySelector<HTMLSelectElement>(`#${PLAYER_SELECT_ID}`)
    playerSelect?.setAttribute('xm-select-search', PLAYER_SEARCH_URL)
    configPlayerSearch(buildPlayerSearchConfig())
    // 6 个 `select[xm-select]` 由 formselects-v4 渲染（打手那颗已在上一句 config 时渲染 ⇒ 这里跳过）。
    initFormSelects('select[xm-select]')
    // 页签类名切换由 `initIslandElements` 负责；⚠️ 必须在 `root.innerHTML` 之后调。
    initIslandElements(layui)

    const msg = (text: string): void =>
      islandMsg(layer as unknown as Record<string, unknown>, text)
    /** 当前页签对应的收入类型（`lay-id` 10 = 代练员、20 = 销售客服；见 spec §2.2）。 */
    let currentEmpType: string = EMP_TYPE.MAIN

    const formValues = (): Record<string, unknown> => form.val('eps')

        /**
     * 当前勾选的行（允许多选）。勾 0 行 ⇒ 弹对标原话「请选中要操作的记录」并回空数组。
     * 返回的是 layui 行（`toRow()` 产物）：`settBtn` 要把每行的 `id` 拼串交给子页。
         */
    const checkedRows = (): PerformanceRow[] => {
      const rows = table.checkStatus(TABLE_ID).data ?? []
      if (rows.length === 0) {
        msg(NEED_SELECTION_TEXT)
        return []
      }
      return rows
    }

        /**
     * 当前页码：写入点是 `table.render` 的 `done(res, curr, …)`；读取点是 `reload()`（就地刷新不跳回第 1 页）。
         */
    let currentPage = 1
        /**
     * 当前已加载的表格行（最近一次响应经 `parseData` → `toRow` 的产物）。
     * 写入点只有 `parseData` 一处（layui 对每次 ajax 响应都会调）⇒ 与表格渲染的是同一批对象；
     * 结账动作靠它把 `data-id` 回查成整行。
         */
    let pageRows: PerformanceRow[] = []

        /**
     * 「批量搜用户」模式的用户名逗号串（`null` = 没开这一档，走正常筛选）。
     * 非 `null` = 在全部筛选之外多加一个 `username`。
         */
    let batchUsername: string | null = null

        /**
     * 当前筛选折成后端参数（列表 / 搜索 / 刷新 / 导出共用这一个出口）。
     * 「筛选被悄悄改掉」的判据与文案全在 `filterWarnings()`（纯函数、逐条可测），这里只合并成一次 `msg()`。
     * ⚠️ 批量搜用户：我们照 JS 的行为（照常发全部筛选 + `username`）、照标题的文案（逐字）——
     * 对标标题承诺「排除其它条件」但它自己的 JS 并不这么做。
         */
    const params = (): PerformanceSearchParams => {
      const values = formValues()
      const warnings = filterWarnings(values)
      if (warnings.length > 0) msg(warnings.join('；'))
      const base = toPerformanceSearch(values, currentEmpType)
      return batchUsername !== null ? { ...base, username: batchUsername } : base
    }
        /**
     * 按当前筛选就地重新取数（筛选照当前控件、页码照当前页）。
     * `page.curr` 显式带上当前页（判据在测试里可见）；⚠️ **不带 `limit`**——塞常量会把
     * 用户选的「条/页」盖回去。
         */
    const reload = (): void => {
      table.reload(TABLE_ID, { where: params(), page: { curr: currentPage } })
    }

        /**
     * 父页给子页弹层用的工具出口：挂在 `window.tools` 上，供「清算绩效」子页调
     * `parent.tools.reloadOrderData()`（关所有层 + 就地刷新）。
     * ⚠️ 只挂这一个方法、不放别的内部状态：这是跨文档（iframe）的接口面，面越大越容易被悄悄依赖。
         */
    const tools = {
      reloadOrderData(): void {
        // 关闭现有所有弹层。
        layer.closeAll()
        reload()
      },
    }
    /** `window` 上的这个槽位名（`parent.tools`）与对标逐字一致，子页那一侧按它取。 */
    const toolsHost = window as unknown as { tools?: typeof tools }
    toolsHost.tools = tools

        /**
     * 筛选栏「搜索」⇒ 回到第 1 页 + 按当前控件重新取数。
     * ⚠️ **只用已有的 `reload()` 出口**，⛔ 不另写一套拼参逻辑——否则「搜索」与「刷新列表」
     * 各有一套参数来源，改名时必然分叉。
     * ⚠️ `lay-verify="datetime"` **拦不住任何东西**（layui 内建校验表没有 `datetime`，未注册的
     * 规则直接跳过）⇒ 日期填错时请求照发、`date_from` 被静默丢掉——靠 `params()` 的提示如实
     * 告诉用户，⛔ 别写「校验不通过时 layui 自己拦住」这种没实测过的话（E-0003 同族）。
         */
    ;(form.on as (e: string, cb: () => void) => void)('submit(seachBtn)', () => {
      currentPage = 1
      reload()
    })

        /**
     * 筛选栏 `selectSeach1`（「代练/下家」/「批量搜用户」）。
     * 选「批量搜用户」(10) ⇒ `layer.prompt` 收每行一个昵称（标题逐字照抄），提交后按行拆、
     * 连同 `username` 一起发；文本域为空 ⇒ 提示且**不发请求**；选回 0 ⇒ 清空该条件、就地重取。
     * ⚠️ 与「审核状态」语义无关（已按 D-0071 删除，`REVIEW_STATUS_MAP` 不复活）。
         */
    ;(
      form.on as (e: string, cb: (data: { value: string }) => void) => void
    )('select(selectSeach1)', (data: { value: string }) => {
      if (data.value !== '10') {
        batchUsername = null
        currentPage = 1
        reload()
        return
      }
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
          title: BATCH_USER_PROMPT_TITLE,
          area: ['520px', '300px'],
        },
        (value: string, index: number) => {
          const usernames = text(value)
            .split(/\r?\n/)
            .map((line) => line.trim())
            .filter((line) => line !== '')
          if (usernames.length === 0) {
            // 对标：提示后**关层 + 不发请求**（`emplist.js` L544-549）。
            msg(BATCH_USER_EMPTY_TEXT)
            layer.close(index)
            return
          }
          batchUsername = usernames.join(',')
          layer.close(index)
          currentPage = 1
          reload()
        },
      )
    })

    /* —— 真动作 1：改金额（`POST …/adjust/`）—— */
        /**
     * 提交一行绩效的接单价改动。⚠️ **没有二次确认弹窗**（对标就是改完单元格直接提交）；
     * 真正的准入全在后端，前端只负责把 msg 显示出来。
         */
    const submitTakerPrice = (row: PerformanceListItem, value: string): void => {
      void adjustPerformance(row.id, {
        taker_price: value,
        reason: '绩效管理页行内改金额',
      })
        .then(() => {
          msg('已提交，按新接单价重算')
          // ⚠️ 必须重新取数：`temp_amount`/`bonus_amount` 由后端按提成比例算出来，
          // 不一定等于刚输入的那个数——不刷新用户会以为「改了没反应」。
          reload()
        })
        .catch(notifyError)
    }

    /* —— 真动作 2：「仅修改结算状态」= **开「清算绩效」子页弹窗**（选状态 / 原因 / 提交都在子页里） —— */

        /**
     * `settBtn`（工具栏）与「结账状态」列那颗 `.setSett` 共用的出口：开「清算绩效」子页弹窗
     * （选状态 / 原因 / 提交都在子页里）。本页从此不发 `settle` / `revoke` 请求。
     * ⚠️ `content` 里的 `ids` 走数值白名单（会被拼进 iframe 的 `src` 属性 ⇒ HTML 属性上下文），
     * ⛔ 不手拼未转义的 id；拼 URL 只此一处（`settlementDialogContent`）。
         */
    const openSettlementDialog = (ids: readonly number[]): void => {
      const query = settlementIdsParam(ids)
      if (query === '') {
        // 形状闸把整批 id 都挡掉时不开层（出口兜底，今天不可达；有 spec 用例钉住这道闸）。
        msg('没有可清算的绩效记录')
        return
      }
      layer.open({
        title: SETTLE_DIALOG_TITLE,
        shade: [0.3, '#333'],
        shadeClose: true,
        type: 2,
        skin: 'layui-layer-lan',
        anim: 8,
        area: ['80%', '50%'],
        // ⛔ 拼 URL 只此一处（内部再走 islandPageUrl + 形状白名单），不在这里手拼第二遍。
        content: settlementDialogContent(ids),
      })
    }

    /* —— 真动作 3：删除记录（`POST /performances/delete`，D-0072 ①）—— */

        /**
     * 删除确认 + 提交。确认层只传 content + yes（标题 / 按钮走 layui 默认）。
     * ⚠️ 拒绝（已结账 / MAIN 行 / 有调整记录）全在后端判 ⇒ 前端不预判、失败原样弹、不给假成功。
         */
    const confirmDelete = (ids: number[]): void => {
      ;(layer.confirm as unknown as (c: string, cb: (index: number) => void) => void)(
        DELETE_CONFIRM_TEXT,
        (index: number) => {
          layer.close(index)
          void deletePerformances(ids)
            .then((result) => {
              // ⚠️ `deleted` 也是 **id 的列表**（brief §8.3），要条数得取 `.length`。
              msg(`已删除 ${result.deleted.length} 条绩效记录`)
              reload()
            })
            .catch(notifyError)
        },
      )
    }

    /* —— 真动作 4：导出记录（`GET /performances/export`，与列表同一份筛选）—— */

        /**
     * 导出当前筛选下的记录（不看勾选，用与列表同一份 `params()`；不关当前页——
     * 对标导出后关掉自己那个页签是管理壳 iframe 的用法，照抄会把 island 页关掉）。
         */
    const exportCurrent = (): void => {
      void downloadPerformanceExport(params())
        .then(({ blob, filename }) => {
          const url = URL.createObjectURL(blob)
          const link = document.createElement('a')
          link.href = url
          link.download = filename
          // ⚠️ 必须插进文档再 `click()`：Firefox 对游离节点不触发下载
          // （`URL.revokeObjectURL` 要等浏览器读完，所以放在下一轮任务里）。
          document.body.appendChild(link)
          link.click()
          link.remove()
          window.setTimeout(() => URL.revokeObjectURL(url), 0)
        })
        .catch(notifyError)
    }

    /* —— 真动作 5：检测重复收款支付宝（`GET /performances/alipay-duplicates`，只读弹层）—— */

    /** 子表是否已经渲染过（layui 的 `render` 会**再插一个** `.layui-table-view` ⇒ 只渲染一次）。 */
    let alipayRendered = false

    /** 拉一次查重数据并渲染/刷新子表。失败原样弹后端 msg（空表照渲染，不建假行）。 */
    const loadAlipayDuplicates = (): void => {
      void fetchAlipayDuplicates()
        .then((data) => {
          const rows = alipayRows(data)
          if (!alipayRendered) {
            alipayRendered = true
            table.render({
              elem: TABLE3_ELEM,
              id: TABLE3_ID,
              // 对标 `defaultToolbar: []` 后又写 `defaultToolbar: ['filter']`（**后写生效**，L1887/L1890）。
              defaultToolbar: ['filter'],
              toolbar: '#toolbarDemo3',
              data: rows,
              cols: [ALIPAY_COLS],
              // 对标 `page: false`（这是个小结果集）。
              page: false,
            })
            return
          }
          table.reload(TABLE3_ID, { data: rows, page: false })
        })
        .catch(notifyError)
    }

        /**
     * 打开「重复收款支付宝子账号检测」弹层（桌面分支：shadeClose / closeBtn:0 / 38%×90%）。
     * ⚠️ `content` 必须是 **jQuery 对象**（传裸 `HTMLElement` 会让 layui 抛错、弹层不出来）。
     * layui 会把节点**搬进**弹层 body、关闭后**不放回** ⇒ `end` 里放回 `<form>` 并重新隐藏
     * （不放回的话第二次点这颗按钮找不到容器、弹层静默不出现）。
     * ⚠️ 不看勾选（对标这颗按钮不读 `checkStatus`）。
         */
    const openAlipayDialog = (): void => {
      const node = root.querySelector<HTMLElement>(ALIPAY_PANEL_SELECTOR)
      if (!node) {
        msg('查重弹层容器缺失，请刷新页面后重试')
        return
      }
      const host = root.querySelector<HTMLElement>('form.layui-form') ?? root
      const jq = (layui as unknown as { $: (el: Element) => unknown }).$(node)
      layer.open({
        title: ALIPAY_DIALOG_TITLE,
        shade: [0.3, '#333'],
        shadeClose: true,
        type: 1,
        skin: 'layui-layer-lan',
        anim: 8,
        closeBtn: 0,
        maxmin: false,
        area: ['38%', '90%'],
        content: jq,
        cancel: (): void => {
          node.style.display = 'none'
        },
        end: (): void => {
          if (node.parentElement !== host) host.appendChild(node)
          node.style.display = 'none'
        },
      })
      loadAlipayDuplicates()
    }

        /**
     * 「结账状态」列那颗 `.setSett` 标签的点击。粒度 = **被点的那一行**（`data-id` 是绩效行主键，
     * 我方传长度 1 的列表）。⛔ **无条件开层**：已结账的行也开同一个子页（要改成哪个状态由用户在子页选）。
     * 前置闸门（都不开层、零请求）：① `data-id` 回查不到行 ⇒ 提示刷新；② 没有归属人（`user`
     * 不是数字）⇒ 提示「还没有接单人，不能结账」——后端结账判据含「有归属人」，这里提前挡掉
     * 是**体验层**（省一次必然失败的往返），⛔ 后端也拦、前端不是唯一防线。
     * ⚠️ 闸门 2 只挡「没有归属人」这一种；前端**没有任何**基于 `review_status` 的拦截（判据在后端一处）。
         */
    const onSettleClick = (event: Event): void => {
      const link = (event.target as HTMLElement).closest<HTMLElement>('.setSett')
      if (!link) return
      // `data-id` 装的是**绩效行主键**（见 `settleTagHtml`），不是人、也不是订单号。
      const perfId = Number(link.dataset.id)
      const clicked = pageRows.find((row) => row.id === perfId)
      if (!clicked) {
        msg('该行数据已失效，请刷新列表后重试')
        return
      }
      // 闸门 2：没有归属人 ⇒ 必然过不了后端的「有归属人」判据；提前如实提示、不开层。
      if (typeof clicked.user !== 'number') {
        msg('该笔绩效还没有接单人，不能结账')
        return
      }
      // ⭐ 只把这**被点的一行**交给子页（`ids` 段是长度 1 的逗号串）。
      openSettlementDialog([clicked.id])
    }

    let closePayroll: (() => void) | undefined
    let payrollLayer: number | undefined
    let unmounted = false
    let resumeHint: HTMLElement | undefined
    function openPayrollDialog(selected: PerformanceRow[]): void {
      if (closePayroll) return
      const category = selected[0]?.empType === EMP_TYPE.SALES ? 'customer_service' : 'taker'
      const empType = category === 'customer_service' ? EMP_TYPE.SALES : EMP_TYPE.MAIN
      if (selected.some((row) =>
        row.empType !== empType || row.orderSta !== ORDER_STATUS_LABELS.COMPLETED ||
        row.settled_at || row.settingSta !== '未结账'
      )) {
        msg('只能对已完成订单的未结账报酬发起在线结算，请按报酬类别分别选择')
        return
      }
      const node = document.createElement('div')
      node.className = 'layui-card-body'
      root.append(node)
      const jq = (layui as unknown as { $: (el: Element) => unknown }).$(node)
      let active: PayrollBatch | undefined
      let ended = false
      const showResume = (batch: PayrollBatch): void => {
        if (unmounted) return
        reload()
        if (!batch.active) return
        resumeHint?.remove()
        resumeHint = closeHint(batch)
        root.prepend(resumeHint)
      }
      closePayroll = mountLegacyPreview(node, {
        performance_ids: selected.map((row) => row.id),
        category,
      }, (batch) => {
        active = batch
        if (ended) showResume(batch)
      })
      payrollLayer = layer.open({
        title: '结算并直接打款',
        type: 1,
        skin: 'layui-layer-lan',
        area: ['80%', '70%'],
        shadeClose: false,
        content: jq,
        end: (): void => {
          ended = true
          closePayroll?.()
          closePayroll = undefined
          payrollLayer = undefined
          node.remove()
          if (active) showResume(active)
        },
      }) as number | undefined
    }

    /* —— 绩效工具栏 —— */
    table.on(`toolbar(${TABLE_ID})`, (obj: { event?: string }) => {
        switch (obj.event) {
          case 'refresh':
            // 对标 `refresh` 顺带读「鸡蛋铺余额」——我方无此数据，不做（「已知会不同」第 14 条）。
            reload()
            return
          case 'settBtn': {
            // 勾 0 行 ⇒ `checkedRows()` 已弹过提示并回空数组（零请求、不开层）。
            const rows = checkedRows()
            if (rows.length === 0) return
            // 对标把每行 `empId` 拼成逗号串交给**同一个** `settSta()`；选「未结算 / 已结算」在子页里做。
            openSettlementDialog(rows.map((row) => row.id))
            return
          }
          case 'checkIdenticalAlipayAccBtn':
            openAlipayDialog()
            return
          case 'exportBtn':
            exportCurrent()
            return
          case 'delEmpBtn': {
            const rows = checkedRows()
            if (rows.length === 0) return
            confirmDelete(rows.map((row) => row.id))
            return
          }
          case 'payBtn': {
            const rows = checkedRows()
            if (rows.length) openPayrollDialog(rows)
            return
          }
          default:
          msg(`「${toolbarLabel(obj.event)}」尚未开放`)
        }
    })

    /* —— 查重子表的工具栏：**只有** `refresh2` 一颗（证据件 `#toolbarDemo3`）—— */
    table.on(`toolbar(${TABLE3_ID})`, (obj: { event?: string }) => {
      if (obj.event !== 'refresh2') return
      loadAlipayDuplicates()
    })

    /* —— 行内编辑（`tempAmount` / `bonusAmount`）—— 对标 L507-524，接的是 `/adjust/` —— */
    table.on(
      `edit(${TABLE_ID})`,
      (obj: { field?: string; value?: string; data?: PerformanceListItem }) => {
        if (obj.field !== 'tempAmount' && obj.field !== 'bonusAmount') return
        const row = obj.data
        if (!row) return
        const value = String(obj.value ?? '').trim()
        // 只挡明显不是数的输入；下界不判（D-0015：金额可以为 0 / 负数）。不发请求，只提示。
        if (value === '' || !/^-?\d+(\.\d+)?$/.test(value)) {
          msg('金额格式不正确，此次编辑无效')
          return
        }
        submitTakerPrice(row, value)
      },
    )

    /* —— 页签：切「代练员报酬账单 / 销售客服提成账单」⇒ 换 `emp_type` 并重新取数 —— */
    const onTabClick = (event: Event): void => {
      const li = (event.target as HTMLElement).closest<HTMLElement>('.layui-tab-title > li')
      if (!li) return
      const layId = li.getAttribute('lay-id') ?? '10'
      currentEmpType = layId === '20' ? EMP_TYPE.SALES : EMP_TYPE.MAIN
      // 类名切换由 `initIslandElements` 的捕获阶段处理器负责；这里只响应取数。
      reload()
    }
        /**
     * 「订单状态」列那颗 `.lableb setdlsta` 标签的点击：对标点它会开「更改订单状态」弹层，
     * 但那颗按钮已按业务方裁定删掉、这一列只负责显示 ⇒ 点击收口成一句提示：
     * 不发请求、不改行状态。`data-id` 是订单号，本处理器不读它。
         */
    const onOrderStatusClick = (event: Event): void => {
      const link = (event.target as HTMLElement).closest<HTMLElement>('.setdlsta')
      if (!link) return
      msg('「修改订单状态」尚未开放')
    }
        /**
     * 「关联订单」列那颗 `.orderdetails` 蓝链的点击：开层装我们自己的订单详情复刻页
     * （`page=order-orderview`，弹层参数逐项取本页自己的值，⛔ 不抽共用模板——各页 area 都不同）。
     * ⛔ **开层前的「单号形状闸」** `/^\d{20}$/`：`layer.open` 的 `title` 是 HTML 上下文，
     * 而 `dataset.id` 会把 attribute 实体解码回 `<` / `>` ⇒ templet 的 `escapeHtml` 在这条链上
     * 不构成保护（实测 `<img onerror>` 能执行）。不匹配 ⇒ 不开层、只给固定文案提示
     * （提示里不拼任何请求数据）。今天不可达，是**不可达时的安全兜底**。
         */
    const onOrderDetailClick = (event: Event): void => {
      const link = (event.target as HTMLElement).closest<HTMLElement>('.orderdetails')
      if (!link) return
      const orderNo = link.dataset.id ?? ''
      if (!/^\d{20}$/.test(orderNo)) {
        // 固定文案（⛔ 不把 `orderNo` 拼进来——它此刻正是「形状不可信」的那份输入）。
        msg('订单号格式不正确，无法打开订单详情')
        return
      }
      // 标题前缀 = 展示单号（缺属性时回落内置号）；title 是 HTML 上下文 ⇒ 边界上再转一次。
      const viewNo = link.dataset.orderidview ?? orderNo
      layer.open({
        title: `${escapeHtml(viewNo)}订单详情`,
        shade: [0.3, '#333'],
        shadeClose: true,
        type: 2,
        skin: 'layui-layer-lan',
        anim: 8,
        area: ['90%', '92%'],
        content:
          `${import.meta.env.BASE_URL}layui-island/page.html?page=order-orderview` +
          `&order_no=${encodeURIComponent(orderNo)}`,
      })
    }
        /**
     * 「查看收款码」入口的点击。`data-id`（绩效行主键）回查整行 ⇒ 签名 URL 不进 DOM；
     * 无码（`null` / `''`）⇒ 只提示、**不开空白层**；回查不到行 ⇒ 提示刷新。
     * 有链接 ⇒ `layer.open({type:1})` 装一张 `<img>`；⚠️ `content` / `title` 都是 HTML 上下文
     * ⇒ 两处插值都过 `escapeHtml`。
     * ⛔ 零 API 请求（链接由后端在列表载荷里签好，前端不新增取数、不自己拼 URL）。
         */
    const onViewWechatQrClick = (event: Event): void => {
      const link = (event.target as HTMLElement).closest<HTMLElement>(WECHAT_QR_LINK_SELECTOR)
      if (!link) return
      const perfId = Number(link.dataset.id)
      const clicked = pageRows.find((row) => row.id === perfId)
      if (!clicked) {
        msg('该行数据已失效，请刷新列表后重试')
        return
      }
      const url = clicked.wechatQrUrl
      if (typeof url !== 'string' || url === '') {
        // 没有收款码 —— 提示而不是空白层（任务书硬要求）。
        msg(NO_WECHAT_QR_TEXT)
        return
      }
      const owner = clicked.userName === '' ? '未分配' : String(clicked.userName)
      layer.open({
        type: 1,
        title: `${escapeHtml(owner)}的微信收款码`,
        content:
          `<div style="padding:10px;text-align:center">` +
          `<img src="${escapeHtml(url)}" alt="微信收款码"` +
          ` style="max-width:260px;max-height:260px;display:block"></div>`,
      })
    }
    root.addEventListener('click', onTabClick)
    // 结账标签是函数 templet 产物 ⇒ 走 root 上的事件委托。
    root.addEventListener('click', onSettleClick)
    root.addEventListener('click', onOrderStatusClick)
    // 「关联订单」蓝链同上（函数 templet 产物）。
    root.addEventListener('click', onOrderDetailClick)
    // 「查看收款码」入口（同样是函数 templet 产物）。
    root.addEventListener('click', onViewWechatQrClick)

    /* —— 主表：`url` 直连绩效列表；`parseData` 吃统一信封 —— */
    let firstDone = false
    /** 最近一次取数失败的文案（`error` 回调写、`done` 回调用——layui 会重渲染 `text.none`）。 */
    let pendingNoneText: string | null = null
    table.render({
      elem: TABLE_ELEM,
      id: TABLE_ID,
      url: `${import.meta.env.BASE_URL.replace(/\/$/, '')}${PERFORMANCES_PATH}`,
      method: 'get',
      request: { pageName: 'page', limitName: 'page_size' },
      parseData: (r: {
        code: number
        msg: string
        data?: { items?: PerformanceListItem[]; total?: number; summary?: PerformanceSummary | null }
      }) => {
        // 同一批映射结果留一份给结账动作回查（与交给 layui 渲染的是同一批对象）。
        const rows = (r.data?.items ?? []).map(toRow)
        pageRows = rows
        // ⚠️ `summary` 必须在这里带走：交给 done() 的就是本函数的返回值；
        // 不带上它，done 里只剩行数据与 count，汇总行会退回占位值。
        return {
          code: r.code,
          msg: r.msg,
          count: r.data?.total ?? 0,
          data: rows,
          summary: r.data?.summary ?? null,
        }
      },
      toolbar: TOOLBAR_ELEM,
      // 对标同对象里 `defaultToolbar` 写了两次，后写的 `['filter']` 生效。
      defaultToolbar: ['filter'],
      // 对标 `limit` / `limits`（L485-486）。
      page: { limit: PAGE_LIMIT, limits: PAGE_LIMITS },
      cols: [COLS],
      text: { none: '暂无数据' },
            /**
       * ⚠️ HTTP 层失败（401/403/5xx）必须把后端 msg 显出来：layui 默认只写一句它自己的
       * 「请求异常…」，会把 403 的中文权限提示吞掉。这里把后端 msg 原样放进 `text.none` 并弹一次。
           */
      error: (res: unknown, msg: unknown): void => {
        const text = backendErrorMessage(res) || String(msg ?? '') || '请求失败'
        pendingNoneText = text
        const none = root.querySelector<HTMLElement>('.layui-table-view .layui-none')
        if (none) none.textContent = text
        notifyError(new Error(text))
      },
      done: (
        r: { count?: number; data?: unknown[]; summary?: PerformanceSummary | null },
        curr: number,
        count: number,
      ): void => {
        // 页码钩子是真实的 done(res, curr, …)：真人翻页时把 curr 记下来，
        // reload() 才能就地刷新而不是跳回第 1 页。
        if (typeof curr === 'number') currentPage = curr
        form.render()
        const el = document.getElementById('total')
        // 汇总行六个金额槽取 data.summary（缺席时 totalText 退回占位值）。
        if (el) el.textContent = totalText(r.count ?? count ?? 0, r.summary)
        if (pendingNoneText !== null) {
          const none = root.querySelector<HTMLElement>('.layui-table-view .layui-none')
          if (none) none.textContent = pendingNoneText
          pendingNoneText = null
        }
        // 首屏：table.render 已发过一发不带 where 的请求，这里按默认筛选再取一次，
        // 保证首屏口径与界面上的控件一致。
        if (!firstDone) {
          firstDone = true
          reload()
        }
      },
    })

    return () => {
      unmounted = true
      closePayroll?.()
      if (typeof payrollLayer === 'number') layer.close(payrollLayer)
      root.removeEventListener('click', onTabClick)
      root.removeEventListener('click', onSettleClick)
      root.removeEventListener('click', onOrderStatusClick)
      root.removeEventListener('click', onOrderDetailClick)
      root.removeEventListener('click', onViewWechatQrClick)
      // 卸载时让出 `window.tools`——只在**还是我们挂的那一个**时删（别把同页另一个实例的出口顺手删掉）。
      if (toolsHost.tools === tools) delete toolsHost.tools
      root.innerHTML = ''
    }
  },
} satisfies IslandPage

/**
 * `layui` 的模块 API 是运行期动态的（`LayuiModule = Record<string, unknown> & ((...) => unknown)`，
 * 见 `layui/loader.ts`）。这里按**本页真正用到的**方法收窄，而不是把整包断言成 `any`——
 * 少写一个方法名时是编译期错误，不是运行期 `undefined is not a function`。
 */
type TableModule = LayuiModule & {
  render: (o: unknown) => void
  reload: (id: string, o: unknown) => void
  on: (event: string, cb: (obj: never) => void) => void
  checkStatus: (id: string) => { data?: PerformanceRow[] }
}

type FormModule = LayuiModule & {
  val: (filter: string) => Record<string, unknown>
  render: (type?: string) => void
  /** 只在本页「搜索」那处用（`submit(seachBtn)`）；签名照 `emp-billsummary.ts` 的既有写法。 */
  on: (event: string, cb: () => void) => void
}

type LayerModule = LayuiModule & {
  open: (o: unknown) => void
  close: (index: number) => void
  /**
 * 挂在 `window.tools.reloadOrderData` 里的那一跳（对标 `_tools` 的 `layer.closeAll()`）：
 * 关掉**本页文档**里所有层。⚠️ 子页 iframe 里调的 `layer` 只关子页自己的层，所以承载子页的
 * 那个 `type:2` 层是由**这一处**关掉的（子页 ← `parent.tools.reloadOrderData()`）。
 */
  closeAll: () => void
  /** 结账 / 删除两处：`options` 那份签名见 `SettleConfirm` 的注释。 */
  confirm: SettleConfirm
    /**
   * `layer.prompt(options, callback)`——「批量搜用户」那处收昵称清单（`formType: 2` 多行）。
   * ⚠️ 这里不写成 `LayuiModule` 的既有签名：少写参数名时是编译期错误而不是运行期 undefined。
     */
  prompt: (options: unknown, callback: (value: string, index: number) => void) => void
}
